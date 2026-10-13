"""FastAPI 应用：把 7 阶段流水线包成可观测的本地 Web 控制台。

路由见方案文件；只读接口 + SSE 进度 + 报告回放。仅绑本机，无鉴权。
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import markdown
from .. import pdf as pdf_mod
from .. import png as png_mod
from .. import frames as frames_mod
from ..config import get_settings
from ..errors import VidereadError
from ..report import qa
from . import background as bg_mod
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


class QaRequest(BaseModel):
    question: str


class TitleRequest(BaseModel):
    title: str = ""


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
            if item.get("type") in ("done", "error", "cancelled"):
                return
    finally:
        job.unsubscribe(channel)


def create_app(out_root: Path) -> FastAPI:
    out_root = Path(out_root)
    app = FastAPI(title="videread", docs_url=None, redoc_url=None)
    manager = JobManager(out_root)

    # 每个 run 一把问答锁：串行化同一报告的问答，避免历史交叉写入；
    # 锁表本身也用一把 guard 保护 setdefault，否则并发首问会各建一把锁。
    qa_locks: dict[str, threading.Lock] = {}
    qa_locks_guard = threading.Lock()

    def qa_lock(run_id: str) -> threading.Lock:
        with qa_locks_guard:
            return qa_locks.setdefault(run_id, threading.Lock())

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ------------------------------------------------------------------ 页面

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        page = STATIC_DIR / "index.html"
        if not page.is_file():
            raise HTTPException(status_code=500, detail=f"缺少控制台页面：{page}")
        html = page.read_text(encoding="utf-8")
        # 给静态资源引用注入 mtime 版本号：JS/CSS 改动后浏览器立即拉新版，
        # 避免「新 HTML 配旧 JS」导致按钮存在但事件处理器缺失（点击无反应）
        for name in ("app.js", "styles.css"):
            asset = STATIC_DIR / name
            stamp = int(asset.stat().st_mtime) if asset.is_file() else 0
            html = html.replace(f"/static/{name}", f"/static/{name}?v={stamp}")
        return HTMLResponse(html)

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
    def api_runs(
        page: int = 1,
        page_size: int = library.DEFAULT_PAGE_SIZE,
        q: str = "",
        fav: int = 0,
    ) -> dict:
        """报告库分页查询：page 从 1 起，q 过滤标题 / UP主 / BV 号，fav=1 只看收藏。

        page_size 只接受 5/10/20/50，其余回落到默认值；越界页码会被夹到有效范围。
        每条结果都带 favorite 标记；只看收藏时按收藏先后排列。
        """
        result = library.query_runs(
            out_root,
            page=page,
            page_size=page_size,
            keyword=q,
            favorites_only=bool(fav),
        )
        return {"out_root": str(out_root), **result}

    @app.get("/api/search")
    def api_search(q: str = "") -> dict:
        """全文检索转写稿：q 过短返回空，前端据此回退到标题过滤。"""
        return {"query": q, "results": library.search_runs(out_root, q)}

    @app.get("/api/stats")
    def api_stats() -> dict:
        """按日汇总各 run 的耗时与 token 用量（成本面板数据源）。"""
        return {"days": library.usage_stats(out_root)}

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

    # ------------------------------------------------------------------ 收藏

    @app.post("/api/runs/{run_id}/favorite")
    def api_add_favorite(run_id: str) -> dict:
        """收藏一个 run（写入 out_root 下的 .favorites.json）。"""
        if library.safe_run_dir(out_root, run_id) is None:
            raise HTTPException(status_code=404, detail="未找到该 run")
        library.set_favorite(out_root, run_id, True)
        return {"run_id": run_id, "favorite": True}

    @app.delete("/api/runs/{run_id}/favorite")
    def api_remove_favorite(run_id: str) -> dict:
        """取消收藏；重复取消同样返回成功，前端不必先查状态。"""
        if library.safe_run_dir(out_root, run_id) is None:
            raise HTTPException(status_code=404, detail="未找到该 run")
        library.set_favorite(out_root, run_id, False)
        return {"run_id": run_id, "favorite": False}

    # ------------------------------------------------------------- 标题改名

    @app.post("/api/runs/{run_id}/title")
    def api_rename_run(run_id: str, payload: TitleRequest) -> dict:
        """改报告库的展示标题（写入 out_root 下的 .titles.json 覆盖表）。

        标题非空白即覆盖，空白视为恢复原标题（meta.json 与 report.html
        始终原样保留）。列表、检索与导出文件名都从同一条路径取标题，
        改一处全部生效。
        """
        title = (payload.title or "").strip()
        if len(title) > library.TITLE_MAX_CHARS:
            raise HTTPException(
                status_code=400,
                detail=f"标题最长 {library.TITLE_MAX_CHARS} 字（当前 {len(title)} 字）",
            )
        result = library.set_title(out_root, run_id, title)
        if result is None:
            raise HTTPException(status_code=404, detail="未找到该 run")
        return result

    # ------------------------------------------------------------- 报告内问答

    @app.post("/api/runs/{run_id}/qa")
    def ask_question(run_id: str, payload: QaRequest) -> dict:
        """就一份报告提问：检索转写稿 → 模型作答 → 追加 qa.jsonl。

        同步 def 路由由 FastAPI 丢线程池执行，不会阻塞 SSE 事件循环；
        同一 run 加锁串行，避免并发问答交叉写历史。
        """
        run_dir = library.safe_run_dir(out_root, run_id)
        if run_dir is None:
            raise HTTPException(status_code=404, detail="未找到该 run")
        if not (run_dir / "transcript.jsonl").is_file():
            raise HTTPException(status_code=409, detail="该 run 没有可用的转写稿，无法问答")

        question = (payload.question or "").strip()
        try:
            with qa_lock(run_id):
                answer = qa.answer_question(run_dir, question)
                qa.append_history(run_dir, question, answer)
        except VidereadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        detail = library.run_detail(out_root, run_id) or {}
        return {"run_id": run_id, "url": detail.get("url"), **answer.to_dict()}

    @app.get("/api/runs/{run_id}/qa")
    def qa_history(run_id: str) -> dict:
        """回放该 run 的历史问答；qa.jsonl 不存在时返回空列表。"""
        run_dir = library.safe_run_dir(out_root, run_id)
        if run_dir is None:
            raise HTTPException(status_code=404, detail="未找到该 run")
        return {"run_id": run_id, "history": qa.load_history(run_dir)}

    # ------------------------------------------------------------------ 背景

    @app.get("/api/ui/background")
    def get_background() -> dict:
        """当前背景：url 为 None 表示默认背景；overlay 为遮罩风格名。"""
        return {"url": bg_mod.background_url(), "overlay": bg_mod.overlay_style()}

    @app.post("/api/ui/background")
    async def upload_background(request: Request) -> dict:
        """上传自定义背景图（原始字节流，走魔数校验，不依赖 python-multipart）。"""
        data = await request.body()
        try:
            url = bg_mod.save_background(data)
        except VidereadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"url": url, "overlay": bg_mod.overlay_style()}

    @app.post("/api/ui/background/overlay")
    async def set_background_overlay(request: Request) -> dict:
        """设置遮罩风格：JSON {"name": default / strong / dark / none}。"""
        try:
            payload = json.loads((await request.body()) or b"{}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="请求体不是合法 JSON") from exc
        try:
            name = bg_mod.set_overlay(str(payload.get("name", "")))
        except VidereadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"overlay": name}

    @app.delete("/api/ui/background")
    def reset_background() -> dict:
        bg_mod.reset_background()
        return {"url": None}

    @app.get("/api/ui/background/image")
    def background_image() -> Response:
        path = bg_mod.background_path()
        if path is None:
            raise HTTPException(status_code=404, detail="未设置自定义背景")
        ext = path.suffix.lstrip(".").lower()
        return FileResponse(path, media_type=bg_mod.mime_for(ext))

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

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict:
        """请求取消任务：在最近的检查点停止，已落盘产物保留（可续跑）。"""
        result = manager.cancel(job_id)
        if result is None:
            raise HTTPException(status_code=404, detail="未找到该任务")
        if result is False:
            raise HTTPException(status_code=409, detail="任务已结束，无需取消")
        return {"job_id": job_id, "cancel_requested": True}

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str) -> dict:
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="未找到该任务")
        snap = job.snapshot()
        snap["queue_position"] = manager.queue_position(job_id)
        return snap

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