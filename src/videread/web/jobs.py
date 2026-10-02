"""Web 控制台的任务层：单任务串行 + 后台线程执行流水线 + 订阅队列。

`pipeline.run()` 是同步阻塞函数（子进程、WebSocket、SDK 调用），**不能在事件循环
线程里直接调用**，因此每个任务起一个 daemon 线程跑它；进度通过 `progress` 回调
转成事件，推给所有 SSE 订阅者。
"""

from __future__ import annotations

import queue
import re
import threading
import traceback
import uuid
from datetime import datetime
from pathlib import Path

from .. import failures
from ..errors import (
    EXIT_CANCELLED,
    EXIT_OK,
    EXIT_USAGE,
    CancelledError,
    VidereadError,
)
from ..pipeline import run as pipeline_run

_MODES = ("standard", "brief")
_STAGE_RE = re.compile(r"^\[(\d)/8\]")
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
        asr_backend: str | None,
        frames: bool | None = None,
        retry: bool = False,
    ) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.url = url
        self.mode = mode
        # 重新解析隐含忽略缓存：否则只会照抄旧产物，「再解析一次」就不成立了
        self.no_cache = no_cache or retry
        self.retry = retry
        self.keep_audio = keep_audio
        self.asr_backend = asr_backend
        self.frames = frames
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
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()

    # ---------------------------------------------------------------- 事件分发

    def emit(self, event_type: str, **payload: object) -> None:
        item: Event = {"type": event_type, **payload}
        with self._lock:
            self._events.append(item)
            subscribers = list(self._subscribers)
        for channel in subscribers:
            channel.put(item)

    def subscribe(self, since: int = 0) -> tuple[queue.Queue, list[Event]]:
        """注册订阅者并返回其历史回放；两者在锁内完成，不会漏事件或重复。"""
        channel: queue.Queue = queue.Queue()
        with self._lock:
            backlog = self._events[max(0, since) :]
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
        if match:
            index = int(match.group(1))
            if index != self.stage_index:
                self.stage_index = index
                self.emit("stage", index=index)
        self.logs.append(line)
        self.emit("log", line=line)

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
            self.emit("log", line="[取消] 已请求取消，将在最近的检查点停止…")
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
        return {
            "id": self.id,
            "url": self.url,
            "mode": self.mode,
            "state": self.state,
            "stage_index": self.stage_index,
            "logs": list(self.logs),
            "run_id": self.run_id,
            "report_url": f"/report/{self.run_id}" if self.run_id else None,
            "exit_code": self.exit_code,
            "error": self.error,
            "error_hint": self.hint,
            "cancel_requested": self.cancel_requested,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


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
        asr_backend: str | None = None,
        frames: bool | None = None,
        retry: bool = False,
    ) -> Job:
        url = (url or "").strip()
        mode = (mode or "standard").strip().lower()
        if not url:
            raise VidereadError("请输入 B 站链接或本地音视频路径")
        if mode not in _MODES:
            raise VidereadError(f"未知模式：{mode!r}（可选 standard / brief）")

        with self._lock:
            busy = self._active is not None and not self._active.terminal
            if busy and len(self._queue) >= self.MAX_QUEUE:
                raise JobBusy(f"队列已满（{self.MAX_QUEUE} 个），请等前面的任务消化完")
            job = Job(
                url=url,
                mode=mode,
                no_cache=no_cache,
                keep_audio=keep_audio,
                asr_backend=asr_backend,
                frames=frames,
                retry=retry,
            )
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
        try:
            report = pipeline_run(
                job.url,
                mode=job.mode,
                out_root=self.out_root,
                use_cache=not job.no_cache,
                keep_audio=job.keep_audio,
                asr_backend=job.asr_backend,
                open_report=False,
                frames=job.frames,
                fresh_dir=job.retry,
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
        except CancelledError:
            # 取消是独立终态（不是失败）：已落盘产物保留，重新提交可断点续跑
            job.exit_code = EXIT_CANCELLED
            self._finish(job, "cancelled")
            job.emit("cancelled", state="cancelled", message="任务已取消")
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

    @staticmethod
    def _finish(job: Job, state: str) -> None:
        job.state = state
        job.finished_at = _now()