"""FastAPI 应用：把 7 阶段流水线包成可观测的本地 Web 控制台。

路由见方案文件；只读接口 + SSE 进度 + 报告回放。仅绑本机，无鉴权。
"""

from __future__ import annotations

import asyncio
import json
import queue
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import markdown
from .. import pdf as pdf_mod
from .. import png as png_mod
from .. import frames as frames_mod
from ..config import get_settings
from ..errors import VidereadError
from . import library, probe
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
    frames: bool | None = None
    retry: bool = False


class ProbeRequest(BaseModel):
    url: str


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

    @app.get("/api/runs/{run_id}/markdown")
    def run_markdown(run_id: str) -> Response:
        """把 report.html 转成 Markdown 下载（仅纯文本报告）。

        Markdown 表达不了报告里的 CSS 组件，导出只保留文字层次（见 `markdown` 模块）。
        文件名同时给 ASCII 回退与 RFC 5987 的 UTF-8 形式，中文标题在各浏览器都能落对。
        """
        run_dir = library.safe_run_dir(out_root, run_id)
        if run_dir is None:
            raise HTTPException(status_code=404, detail="未找到该 run")
        path = run_dir / "report.html"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="该 run 还没有 report.html")
        if frames_mod.has_frames(run_dir):
            raise HTTPException(
                status_code=409,
                detail="该报告带视频截图，不提供 Markdown 导出（截图离开 run 目录无法显示）；"
                "请改用 PDF / PNG 长图导出",
            )

        detail = library.run_detail(out_root, run_id) or {}
        title = str(detail.get("title") or "").strip() or run_id
        return Response(
            content=markdown.html_to_markdown(path.read_text(encoding="utf-8")),
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{run_id}.md"; '
                    f"filename*=UTF-8''{quote(title + '.md')}"
                )
            },
        )

    # ------------------------------------------------------------------ 报告库

    @app.get("/api/runs/{run_id}/pdf")
    def run_pdf(run_id: str) -> Response:
        """导出 report.pdf（Edge / Chrome 无头打印，产物缓存于 run 目录）。

        首次导出调用一次浏览器；report.html 更新后重新导出。
        浏览器缺失或打印失败转 400，前端展示后端给出的具体原因。
        """
        run_dir = library.safe_run_dir(out_root, run_id)
        if run_dir is None:
            raise HTTPException(status_code=404, detail="未找到该 run")
        html_path = run_dir / "report.html"
        if not html_path.is_file():
            raise HTTPException(status_code=404, detail="该 run 还没有 report.html")

        detail = library.run_detail(out_root, run_id) or {}
        title = str(detail.get("title") or "").strip() or run_id
        try:
            pdf_path = pdf_mod.export_pdf(html_path, run_dir / "report.pdf", get_settings())
        except VidereadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return Response(
            content=pdf_path.read_bytes(),
            media_type="application/pdf",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{run_id}.pdf"; '
                    f"filename*=UTF-8''{quote(title + '.pdf')}"
                )
            },
        )

    @app.get("/api/runs/{run_id}/png")
    def run_png(run_id: str) -> Response:
        """导出竖长 report.png（Edge / Chrome 无头截图，产物缓存于 run 目录）。

        两趟浏览器调用：先测内容高度再按高开窗截一整张。失败转 400。
        """
        run_dir = library.safe_run_dir(out_root, run_id)
        if run_dir is None:
            raise HTTPException(status_code=404, detail="未找到该 run")
        html_path = run_dir / "report.html"
        if not html_path.is_file():
            raise HTTPException(status_code=404, detail="该 run 还没有 report.html")

        detail = library.run_detail(out_root, run_id) or {}
        title = str(detail.get("title") or "").strip() or run_id
        try:
            png_path = png_mod.export_png(html_path, run_dir / "report.png", get_settings())
        except VidereadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return Response(
            content=png_path.read_bytes(),
            media_type="image/png",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{run_id}.png"; '
                    f"filename*=UTF-8''{quote(title + '.png')}"
                )
            },
        )

    @app.get("/api/runs")
    def api_runs() -> dict:
        return {"out_root": str(out_root), "runs": library.list_runs(out_root)}

    @app.get("/api/runs/{run_id}")
    def api_run(run_id: str) -> dict:
        detail = library.run_detail(out_root, run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="未找到该 run")
        return detail

    @app.delete("/api/runs/{run_id}")
    def api_delete_run(run_id: str) -> dict:
        """删除一个 run 的全部产物；报告库前端有二次确认，这里不静默兜底。"""
        if not library.delete_run(out_root, run_id):
            raise HTTPException(status_code=404, detail="未找到该 run")
        return {"deleted": run_id}

    # ------------------------------------------------------------------ 预检

    @app.post("/api/probe")
    def api_probe(payload: ProbeRequest) -> dict:
        """提交前探测时长；超长由前端拦截，异常统一转 400 让前端降级放行。"""
        try:
            return probe.probe_source(payload.url, out_root=out_root)
        except VidereadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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
                frames=payload.frames,
                retry=payload.retry,
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