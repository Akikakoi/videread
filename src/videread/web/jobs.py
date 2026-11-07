"""Web 控制台的任务层：单任务串行 + 后台线程执行流水线 + 订阅队列。

`pipeline.run()` 是同步阻塞函数（子进程、WebSocket、SDK 调用），**不能在事件循环
线程里直接调用**，因此每个任务起一个 daemon 线程跑它；进度通过 `progress` 回调
转成事件，推给所有 SSE 订阅者。
"""

from __future__ import annotations

import json
import queue
import re
import sqlite3
import threading
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

from .. import failures
from ..errors import (
    EXIT_CANCELLED,
    EXIT_OK,
    EXIT_USAGE,
    CancelledError,
    VidereadError,
)
from ..config import REPORT_MODES, STAGE_COUNT
from ..pipeline import run as pipeline_run

_STAGE_RE = re.compile(rf"^\[(\d)/{STAGE_COUNT}\]")
_TERMINAL = ("done", "error", "cancelled")

#: SSE 事件的类型：status / stage / log / done / error
Event = dict


def _now() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


class JobBusy(RuntimeError):
    """队列已满；个人工具的排队上限，避免任务无限堆积。"""


class Job:
    """一次流水线运行的可观测句柄。"""

    def __init__(
        self,
        *,
        url: str,
        mode: str,
        no_cache: bool,
        keep_audio: bool,
        out_root: Path,
        frames: bool | None = None,
        retry: bool = False,
        lang: str | None = None,
    ) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.url = url
        self.mode = mode
        # 重新解析隐含忽略缓存：否则只会照抄旧产物，「再解析一次」就不成立了
        self.no_cache = no_cache or retry
        self.retry = retry
        self.keep_audio = keep_audio
        self.out_root = Path(out_root)
        self.frames = frames
        self.lang = (lang or "").strip().lower() or None
        self.state = "queued"
        self.stage_index = 0
        self.logs: list[str] = []
        self.run_id: str | None = None
        self.report_path: Path | None = None
        self.exit_code: int | None = None
        self.error: str | None = None
        self.hint: str | None = None
        self.created_at = _now()
        self.finished_at: str | None = None
        self.cancel_requested = False

        self._events: list[Event] = []
        self._subscribers: list[queue.Queue] = []
        # RLock：progress() 需要在同一临界区里追加日志并发出事件
        self._lock = threading.RLock()
        # 事件序号：SSE 客户端带上快照里的 seq，只回放其后的增量，
        # 避免「快照已渲染的日志 + SSE 历史回放」重复显示
        self._seq = 0
        self._cancel_event = threading.Event()
        # 落盘钩子由 JobManager 注入（Job 自身不知道存储层的存在）：
        # 阶段推进 / 定期 / 终态时把快照写进 SQLite，服务重启后可恢复
        self.persist_hook: Callable[[], None] | None = None
        self._last_persist_len = 0

    # ---------------------------------------------------------------- 事件分发

    def emit(self, event_type: str, **payload: object) -> None:
        with self._lock:
            self._seq += 1
            item: Event = {"type": event_type, **payload, "seq": self._seq}
            self._events.append(item)
            subscribers = list(self._subscribers)
        for channel in subscribers:
            channel.put(item)

    def subscribe(self, after: int = 0) -> tuple[queue.Queue, list[Event]]:
        """注册订阅者并返回其历史回放。

        `after` 是客户端快照里的 `seq`：回放只含该序号之后的事件，避免与
        快照里已有的日志重复。注册与回放筛选在同一临界区完成，不会漏事件
        或把回放期间的新事件挤进历史。
        """
        channel: queue.Queue = queue.Queue()
        with self._lock:
            backlog = [
                event
                for event in self._events
                if int(event.get("seq", 0) or 0) > after
            ]
            self._subscribers.append(channel)
        return channel, backlog

    def unsubscribe(self, channel: queue.Queue) -> None:
        with self._lock:
            if channel in self._subscribers:
                self._subscribers.remove(channel)

    # ------------------------------------------------------------------ 进度回调

    def progress(self, line: str) -> None:
        """传给 `pipeline.run(progress=...)`；在主线程之外被调用。"""
        match = _STAGE_RE.match(line)
        stage_changed = False
        with self._lock:
            if match:
                index = int(match.group(1))
                if index != self.stage_index:
                    self.stage_index = index
                    stage_changed = True
                    self.emit("stage", index=index)
            # 日志追加与事件发出在同一临界区：快照的 seq 一定覆盖快照里的
            # 全部日志，SSE 带 after=seq 时不会把旧日志再回放一遍。
            self.logs.append(line)
            self.emit("log", line=line)
        # 落盘节流：阶段边界必存，日志每 25 行存一次，终态由调用方兜底保存
        if self.persist_hook is not None and (
            stage_changed or len(self.logs) - self._last_persist_len >= 25
        ):
            self._last_persist_len = len(self.logs)
            self.persist_hook()

    # -------------------------------------------------------------------- 取消

    def request_cancel(self) -> bool:
        """请求取消当前任务；返回 False 表示任务已结束，取消无效。

        排队中的任务直接置为 cancelled 终态（还没开始跑，无需检查点）；
        执行中的任务置位事件，由流水线在最近的检查点停止。
        """
        if self.terminal:
            return False
        self.cancel_requested = True
        self._cancel_event.set()
        if self.state == "queued":
            self.exit_code = EXIT_CANCELLED
            self.state = "cancelled"
            self.finished_at = _now()
            self.emit("cancelled", state="cancelled", message="已取消排队任务")
        else:
            self.progress("[取消] 已请求取消，将在最近的检查点停止…")
        return True

    def cancel_check(self) -> None:
        """传给 `pipeline.run(cancel_check=...)`：请求取消时在检查点抛出。"""
        if self._cancel_event.is_set():
            raise CancelledError("用户取消了任务")

    # -------------------------------------------------------------------- 快照

    @property
    def terminal(self) -> bool:
        return self.state in _TERMINAL

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "url": self.url,
                "mode": self.mode,
                "state": self.state,
                "stage_index": self.stage_index,
                "logs": list(self.logs),
                "seq": self._seq,
                "run_id": self.run_id,
                "report_url": f"/report/{self.run_id}" if self.run_id else None,
                "exit_code": self.exit_code,
                "error": self.error,
                "error_hint": self.hint,
                "cancel_requested": self.cancel_requested,
                "created_at": self.created_at,
                "finished_at": self.finished_at,
            }

    @classmethod
    def from_row(cls, row: dict, *, out_root: Path) -> "Job":
        """从 SQLite 行还原任务句柄（服务重启后的队列恢复用）。"""
        job = cls(
            url=str(row["url"]),
            mode=str(row["mode"]),
            no_cache=bool(row["no_cache"]),
            keep_audio=bool(row["keep_audio"]),
            out_root=out_root,
            frames=None if row["frames"] is None else bool(row["frames"]),
            retry=bool(row["retry"]),
            lang=row["lang"],
        )
        job.id = str(row["id"])
        job.created_at = str(row["created_at"] or job.created_at)
        job.state = str(row["state"])
        job.stage_index = int(row["stage_index"] or 0)
        job.run_id = row["run_id"]
        job.exit_code = row["exit_code"]
        job.error = row["error"]
        job.hint = row["hint"]
        job.cancel_requested = bool(row["cancel_requested"])
        job.finished_at = row["finished_at"]
        try:
            job.logs = list(json.loads(row["logs"] or "[]"))
        except json.JSONDecodeError:
            pass
        job._last_persist_len = len(job.logs)
        return job


class JobStore:
    """任务队列的 SQLite 落盘。

    服务重启后：queued 任务重新入队（流水线本身支持断点续跑），running 任务
    标记为 cancelled 终态并提示续跑方式。落盘失败一律静默降级——内存态仍是
    权威，存储层坏了不能拖垮任务执行。
    """

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        created_at TEXT,
        finished_at TEXT,
        url TEXT,
        mode TEXT,
        no_cache INTEGER,
        keep_audio INTEGER,
        frames INTEGER,
        retry INTEGER,
        lang TEXT,
        state TEXT,
        stage_index INTEGER,
        run_id TEXT,
        exit_code INTEGER,
        error TEXT,
        hint TEXT,
        cancel_requested INTEGER,
        logs TEXT
    )
    """

    #: 终态任务在 SQLite 里保留的条数上限（备查历史，超出清最旧的）
    _KEEP_TERMINAL = 100

    def __init__(self, path: Path) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(self._SCHEMA)
        self._conn.commit()

    def save(self, job: Job) -> None:
        """整行快照覆盖写；任何 SQLite 错误都吞掉（见类注释）。"""
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO jobs VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        job.id,
                        job.created_at,
                        job.finished_at,
                        job.url,
                        job.mode,
                        int(job.no_cache),
                        int(job.keep_audio),
                        job.frames,
                        int(job.retry),
                        job.lang,
                        job.state,
                        job.stage_index,
                        job.run_id,
                        job.exit_code,
                        job.error,
                        job.hint,
                        int(job.cancel_requested),
                        json.dumps(job.logs[-400:], ensure_ascii=False),
                    ),
                )
                self._conn.commit()
        except sqlite3.Error:
            pass

    def load_all(self) -> list[dict]:
        """按提交顺序取全部任务行；损坏的库当作空表处理。"""
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM jobs ORDER BY rowid"
                ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error:
            return []

    def prune_terminal(self) -> None:
        try:
            with self._lock:
                stale = [
                    row["id"]
                    for row in self._conn.execute(
                        "SELECT id FROM jobs WHERE state IN ('done','error','cancelled') "
                        "ORDER BY rowid DESC"
                    ).fetchall()[self._KEEP_TERMINAL:]
                ]
                for job_id in stale:
                    self._conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
                self._conn.commit()
        except sqlite3.Error:
            pass

    def close(self) -> None:
        try:
            with self._lock:
                self._conn.close()
        except sqlite3.Error:
            pass


class JobManager:
    """FIFO 队列：同一时刻只执行一个任务，其余任务排队等待。

    串行执行是刻意取舍（避免争用同一 run 目录与重复付费）；排队替代了旧的
    「忙碌即 409」——批量丢一批链接进来，服务端按序消化。
    """

    #: 排队上限：个人工具的兜底，超出直接拒绝
    MAX_QUEUE = 10
    #: 会话内保留的任务记录上限（超出清掉最早的已终态任务）
    _MAX_JOBS = 50

    def __init__(self, out_root: Path) -> None:
        self.out_root = Path(out_root)
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._queue: list[Job] = []
        self._active: Job | None = None
        self._store = self._open_store()
        self._recover()

    def _open_store(self) -> JobStore | None:
        """SQLite 不可用（路径只读 / 文件损坏等）时退化为纯内存队列。"""
        try:
            self.out_root.mkdir(parents=True, exist_ok=True)
            return JobStore(self.out_root / ".jobs.sqlite3")
        except (sqlite3.Error, OSError):
            return None

    def _persist(self, job: Job) -> None:
        if self._store is not None:
            self._store.save(job)

    def _recover(self) -> None:
        """服务重启后的队列恢复（对应 README「支持取消」的断点续跑约定）。

        - queued：重新入队，流水线会从已落盘的产物续跑；
        - running：进程没了，任务无法确认死在哪一步，置为 cancelled 终态并
          指引用户重新提交（重新提交命中已有产物即续跑，不重复付费）。
        """
        if self._store is None:
            return
        for row in self._store.load_all():
            state = row["state"]
            if state not in ("queued", "running"):
                continue  # 终态任务留在库里备查，不占会话内存
            job = Job.from_row(row, out_root=self.out_root)
            with self._lock:
                self._jobs[job.id] = job
                if state == "queued":
                    self._queue.append(job)
            if state == "queued":
                job.progress("[恢复] 服务重启，任务已从队列恢复，等待执行")
            else:
                job.state = "cancelled"
                job.exit_code = EXIT_CANCELLED
                job.error = "服务重启，任务中断"
                job.hint = "已完成的阶段产物保留；重新提交同一视频可断点续跑"
                job.finished_at = _now()
                job.emit(
                    "cancelled",
                    state="cancelled",
                    message="服务重启，任务中断；重新提交可断点续跑",
                )
            self._persist(job)
        # 恢复的排队任务不会经过 submit()，这里手动放行 worker
        self._start_next()

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def queue_position(self, job_id: str) -> int:
        """任务在队列中的位次：执行中 / 不在队列返回 0，排队中返回 1 起的序号。"""
        with self._lock:
            if self._active is not None and self._active.id == job_id:
                return 0
            for index, job in enumerate(self._queue):
                if job.id == job_id:
                    return index + 1
            return 0

    def cancel(self, job_id: str) -> bool | None:
        """请求取消任务；任务不存在返回 None，已结束返回 False，受理返回 True。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job in self._queue:
                self._queue.remove(job)
            was_active = self._active is job
        accepted = job.request_cancel()
        if accepted:
            self._persist(job)  # 排队任务被秒取消时这里是它的终态落盘点
        if accepted and was_active and job.terminal:
            # 执行中的任务被秒取消（如还在排队检查点前）：立即放行下一个
            self._start_next()
        return accepted

    def submit(
        self,
        *,
        url: str,
        mode: str = "standard",
        no_cache: bool = False,
        keep_audio: bool = False,
        frames: bool | None = None,
        retry: bool = False,
        lang: str | None = None,
    ) -> Job:
        url = (url or "").strip()
        mode = (mode or "standard").strip().lower()
        if not url:
            raise VidereadError("请输入 B 站链接或本地音视频路径")
        if mode not in REPORT_MODES:
            raise VidereadError(f"未知模式：{mode!r}（可选 standard / deep / brief / outline）")

        with self._lock:
            busy = self._active is not None and not self._active.terminal
            if busy and len(self._queue) >= self.MAX_QUEUE:
                raise JobBusy(f"队列已满（{self.MAX_QUEUE} 个），请等前面的任务消化完")
            job = Job(
                url=url,
                mode=mode,
                no_cache=no_cache,
                keep_audio=keep_audio,
                out_root=self.out_root,
                frames=frames,
                retry=retry,
                lang=lang,
            )
            job.persist_hook = lambda j=job: self._persist(j)
            self._jobs[job.id] = job
            self._prune_jobs_locked()
            self._queue.append(job)
            queue_position = len(self._queue) if busy else 0
            if not busy:
                self._start_next_locked()
        if queue_position:
            job.emit(
                "log",
                line=f"[排队] 已加入队列，当前排在第 {queue_position} 位"
                f"（共 {queue_position} 个等待）",
            )
        self._persist(job)
        return job

    def _prune_jobs_locked(self) -> None:
        """会话记录兜底：只保留最近 _MAX_JOBS 个，优先清最早的已终态任务。"""
        if len(self._jobs) <= self._MAX_JOBS:
            return
        for job in sorted(self._jobs.values(), key=lambda j: j.created_at):
            if len(self._jobs) <= self._MAX_JOBS:
                break
            if job.terminal and (self._active is not job):
                self._jobs.pop(job.id, None)

    # ---------------------------------------------------------------- worker

    def _start_next(self) -> None:
        with self._lock:
            self._start_next_locked()

    def _start_next_locked(self) -> None:
        """从队列取出下一个待执行任务并启动（调用方须持有锁）。"""
        if self._active is not None and not self._active.terminal:
            return
        while self._queue:
            job = self._queue.pop(0)
            if job.terminal:
                continue  # 排队期间被取消的任务直接跳过
            self._active = job
            threading.Thread(
                target=self._execute,
                args=(job,),
                name=f"videread-job-{job.id}",
                daemon=True,
            ).start()
            return
        self._active = None

    def _execute(self, job: Job) -> None:
        job.state = "running"
        job.emit("status", state="running")
        self._persist(job)
        try:
            report = pipeline_run(
                job.url,
                mode=job.mode,
                out_root=self.out_root,
                use_cache=not job.no_cache,
                keep_audio=job.keep_audio,
                open_report=False,
                frames=job.frames,
                fresh_dir=job.retry,
                report_lang=job.lang,
                cancel_check=job.cancel_check,
                progress=job.progress,
            )
            # 成功收尾也放在 try 里：万一这里出错（如产物路径异常），
            # 兜底分支同样能给出终态，任务不会永远卡在 running
            job.run_id = report.parent.name
            job.report_path = report
            job.exit_code = EXIT_OK
            self._finish(job, "done")
            job.emit(
                "done",
                state="done",
                run_id=job.run_id,
                report_url=f"/report/{job.run_id}",
            )
            self._persist_terminal(job)
        except CancelledError:
            # 取消是独立终态（不是失败）：已落盘产物保留，重新提交可断点续跑
            job.exit_code = EXIT_CANCELLED
            self._finish(job, "cancelled")
            job.emit("cancelled", state="cancelled", message="任务已取消")
            self._persist_terminal(job)
        except VidereadError as exc:
            self._fail(job, int(exc.exit_code), str(exc))
        except BaseException as exc:  # 兜底：未归类异常也要有终态，避免前端挂住
            error = f"{type(exc).__name__}: {exc}"
            job.emit("log", line=f"[错误] 未归类异常：{error}")
            job.emit("log", line=traceback.format_exc(limit=3).strip())
            self._fail(job, EXIT_USAGE, error, hint="未归类异常，请查看运行日志")
        finally:
            # 无论成败都立即放行下一个排队任务
            self._start_next()

    def _fail(self, job: Job, exit_code: int, error: str, *, hint: str | None = None) -> None:
        """失败出口：文案统一来自 failures，前端不再自己维护一份退出码表。"""
        job.exit_code = exit_code
        job.error = error
        job.hint = hint or failures.hint(exit_code)
        self._finish(job, "error")
        job.emit("error", exit_code=exit_code, state="error", message=error, hint=job.hint)
        self._persist_terminal(job)

    def _finish(self, job: Job, state: str) -> None:
        """终态收尾：先置状态与事件（订阅者/轮询立即看到终态），再落盘裁剪。

        落盘放在 emit 之后是有意的：SQLite commit 有毫秒级耗时，若夹在
        「置终态」和「emit 终态」之间，轮询方会看到 state=done 但事件流
        还没到 done 的窗口（test_web 的 kinds[-1] 断言就是这么被打破的）。
        """
        job.state = state
        job.finished_at = _now()

    def _persist_terminal(self, job: Job) -> None:
        """终态落盘 + 终态历史裁剪（在终态事件 emit 之后调用）。"""
        self._persist(job)
        if self._store is not None:
            self._store.prune_terminal()