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
from ..errors import EXIT_OK, EXIT_USAGE, VidereadError
from ..pipeline import run as pipeline_run

_MODES = ("standard", "brief")
_STAGE_RE = re.compile(r"^\[(\d)/8\]")
_TERMINAL = ("done", "error")

#: SSE 事件的类型：status / stage / log / done / error
Event = dict


def _now() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


class JobBusy(RuntimeError):
    """已有任务在执行；个人工具按串行处理，避免争用同一 run 目录与重复付费。"""


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

        self._events: list[Event] = []
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()

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
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


class JobManager:
    """同一时刻只保留一个任务；新任务在旧任务结束后才允许提交。"""

    def __init__(self, out_root: Path) -> None:
        self.out_root = Path(out_root)
        self._lock = threading.Lock()
        self._job: Job | None = None

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            if self._job is not None and self._job.id == job_id:
                return self._job
            return None

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
            active = self._job
            if active is not None and not active.terminal:
                raise JobBusy(f"已有任务在执行（{active.id}），请等它结束")
            job = Job(
                url=url,
                mode=mode,
                no_cache=no_cache,
                keep_audio=keep_audio,
                asr_backend=asr_backend,
                frames=frames,
                retry=retry,
            )
            self._job = job

        thread = threading.Thread(
            target=self._execute,
            args=(job,),
            name=f"videread-job-{job.id}",
            daemon=True,
        )
        thread.start()
        return job

    # ---------------------------------------------------------------- worker

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
                progress=job.progress,
            )
        except VidereadError as exc:
            self._fail(job, int(exc.exit_code), str(exc))
        except BaseException as exc:  # 兜底：未归类异常也要有终态，避免前端挂住
            error = f"{type(exc).__name__}: {exc}"
            job.emit("log", line=f"[错误] 未归类异常：{error}")
            job.emit("log", line=traceback.format_exc(limit=3).strip())
            self._fail(job, EXIT_USAGE, error, hint="未归类异常，请查看运行日志")
        else:
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