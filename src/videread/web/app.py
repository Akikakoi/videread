"""FastAPI 应用：把 7 阶段流水线包成可观测的本地 Web 控制台。

路由见方案文件；只读接口 + SSE 进度 + 报告回放。仅绑本机，无鉴权。
"""

from __future__ import annotations

import asyncio
import json
import queue
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..errors import VidereadError
from . import library
from .jobs import Job, JobBusy, JobManager

STATIC_DIR = Path(__file__).resolve().parent / "static"
HEARTBEAT_SEC = 15.0
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


class JobRequest(BaseModel):
    url: str
    mode: str = "standard"
    no_cache: bool = False
    keep_audio: bool = False
    asr_backend: str | None = None


def _frame(item: dict) -> str:
    """SSE 帧：event + data + 空行（缺空行浏览器不派发）。"""
    data = json.dumps(item, ensure_ascii=False)
    return f"event: {item.get('type', 'message')}\ndata: {data}\n\n"


async def _event_stream(job: Job, request: Request, since: int):
    """回放历史事件后持续推送；心跳既防超时也让 `is_disconnected` 有机会生效。

    `queue.get` 必须走 `asyncio.to_thread`：生成器运行在事件循环线程里，
    直接阻塞会挂住整个服务的其他请求。
    """
    channel, backlog = job.subscribe(since)
    try:
        for item in backlog:
            yield _frame(item)
        if job.terminal:
            return
        while True:
            if await request.is_disconnected():
                return
            try:
                item = await asyncio.to_thread(channel.get, True, HEARTBEAT_SEC)
            except queue.Empty:
                yield ": keep-alive\n\n"
                continue
            yield _frame(item)
            if item.get("type") in ("done", "error"):
                return
    finally:
        job.unsubscribe(channel)


def create_app(out_root: Path) -> FastAPI:
    out_root = Path(out_root)
    app = FastAPI(title="videread", docs_url=None, redoc_url=None)
    manager = JobManager(out_root)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ------------------------------------------------------------------ 页面

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        page = STATIC_DIR / "index.html"
        if not page.is_file():
            raise HTTPException(status_code=500, detail=f"缺少控制台页面：{page}")
        return HTMLResponse(page.read_text(encoding="utf-8"))

    @app.get("/report/{run_id}", response_class=HTMLResponse)
    def report(run_id: str) -> HTMLResponse:
        run_dir = library.safe_run_dir(out_root, run_id)
        if run_dir is None:
            raise HTTPException(status_code=404, detail="未找到该 run")
        path = run_dir / "report.html"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="该 run 还没有 report.html")
        return HTMLResponse(path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ 报告库

    @app.get("/api/runs")
    def api_runs() -> dict:
        return {"out_root": str(out_root), "runs": library.list_runs(out_root)}

    @app.get("/api/runs/{run_id}")
    def api_run(run_id: str) -> dict:
        detail = library.run_detail(out_root, run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="未找到该 run")
        return detail

    # ------------------------------------------------------------------ 任务

    @app.post("/api/jobs")
    def create_job(payload: JobRequest) -> dict:
        try:
            job = manager.submit(
                url=payload.url,
                mode=payload.mode,
                no_cache=payload.no_cache,
                keep_audio=payload.keep_audio,
                asr_backend=(payload.asr_backend or "").strip() or None,
            )
        except JobBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except VidereadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"job_id": job.id, "state": job.state}

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str) -> dict:
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="未找到该任务")
        return job.snapshot()

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: str, request: Request, since: int = 0):
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="未找到该任务")
        return StreamingResponse(
            _event_stream(job, request, since),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    return app