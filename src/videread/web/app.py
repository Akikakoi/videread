"""FastAPI 应用：把 7 阶段流水线包成可观测的本地 Web 控制台。

路由见方案文件；只读接口 + SSE 进度 + 报告回放。仅绑本机，无鉴权。
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import threading
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import markdown
from .. import pdf as pdf_mod
from .. import png as png_mod
from .. import frames as frames_mod
from .. import download
from ..config import MAX_VIDEO_DURATION_SEC, get_settings
from ..errors import UsageError, VidereadError
from ..report import qa
from .. import usersettings
from . import library, probe
from .jobs import Job, JobBusy, JobManager

STATIC_DIR = Path(__file__).resolve().parent / "static"
#: 编辑风正式版页面目录（随包分发；原 demos/ 实验页已随 P4 退役删除，历史在 git）
EDITORIAL_DIR = STATIC_DIR / "editorial"
EDITORIAL_ASSETS = (
    "css/base.css", "css/index.css", "css/reader.css", "css/job.css",
    "js/common.js", "js/index.js", "js/reader.js", "js/job.js", "js/theme.js",
    "fonts/fraunces/index.css", "fonts/fraunces/500.css", "fonts/fraunces/600-italic.css",
    "fonts/noto-serif-sc/index.css", "fonts/noto-serif-sc/500.css", "fonts/noto-serif-sc/600.css",
    "fonts/ibm-plex-mono/index.css",
)
HEARTBEAT_SEC = 15.0
_BATCH_ID_RE = re.compile(r"^[a-f0-9]{12}$")
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
    lang: str | None = None


class ProbeRequest(BaseModel):
    url: str


class FavoriteResolveRequest(BaseModel):
    url: str


class BatchRequest(BaseModel):
    """收藏夹批量投递：items 为空表示全选，否则按 bvid/链接筛选。"""

    url: str
    items: list[str] | None = None
    mode: str = "standard"
    lang: str | None = None
    frames: bool | None = None
    skip_existing: bool = True
    limit: int = 0
    no_cache: bool = False


class QaRequest(BaseModel):
    question: str


class TitleRequest(BaseModel):
    title: str = ""


class LlmSettingsRequest(BaseModel):
    """模型设置表单：api_key 留空 = 沿用已保存的 key（前端语义）。"""

    api_key: str = ""
    base_url: str = ""
    model_plan: str = ""
    model_write: str = ""


class ModelListRequest(BaseModel):
    """拉取可用模型列表：key / 网关留空则回退当前生效配置。"""

    api_key: str = ""
    base_url: str = ""


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


def create_app(out_root: Path, self_base: str | None = None) -> FastAPI:
    """`self_base`：本服务自身的基址（如 http://127.0.0.1:8765）。

    提供后「导出 PNG」走编辑风阅读页长图（无头浏览器截 /read/{run_id}?png=1）；
    为 None（离线测试等场景）时回退为直接截 report.html 的原始样式。
    """
    out_root = Path(out_root)
    app = FastAPI(title="videread", docs_url=None, redoc_url=None)
    # demo 页面禁缓存：开发迭代时浏览器必须每次拿到最新 HTML
    _NO_CACHE = {"Cache-Control": "no-cache"}
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

    @app.get("/")
    def index() -> RedirectResponse:
        """磨砂玻璃控制台已退役（P4，见 plans/007）：根路径 302 到编辑风前端。"""
        return RedirectResponse("/editorial", status_code=302)

    def _editorial_page(filename: str) -> HTMLResponse:
        """读取编辑风页面；静态资源注入 mtime 版本号（与控制台同策略，
        改动后浏览器立即拉新版，不再依赖 no-cache 每次回源）。"""
        page = EDITORIAL_DIR / filename
        if not page.is_file():
            raise HTTPException(status_code=500, detail=f"缺少编辑风页面：{page}")
        html = page.read_text(encoding="utf-8")
        for asset in EDITORIAL_ASSETS:
            plain = f"/static/editorial/{asset}"
            if plain not in html:
                continue
            full = EDITORIAL_DIR / asset
            stamp = int(full.stat().st_mtime) if full.is_file() else 0
            html = html.replace(plain, f"{plain}?v={stamp}")
        return HTMLResponse(html, headers=_NO_CACHE)

    @app.get("/editorial", response_class=HTMLResponse)
    def editorial() -> HTMLResponse:
        """编辑风主页（正式版）：杂志排版的报告库 + 投印入口，与控制台双轨过渡。"""
        return _editorial_page("index.html")

    @app.get("/read/{run_id}", response_class=HTMLResponse)
    def editorial_read(run_id: str) -> HTMLResponse:
        """编辑风全文阅读页（新标签页打开）：抓取 report.html 重排为杂志版式。"""
        return _editorial_page("reader.html")

    @app.get("/editorial/job/{job_id}", response_class=HTMLResponse)
    def editorial_job(job_id: str) -> HTMLResponse:
        """编辑风投印工单页：八阶段步进器 + 排印日志 + 取消，SSE 实时刷新。"""
        return _editorial_page("job.html")

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
        """导出竖长 PNG（Edge / Chrome 无头截图，产物缓存于 run 目录）。

        配置了 self_base 时优先截编辑风阅读页（/read/{run_id}?png=1），
        失败转 400，前端展示后端给出的具体原因。
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
            if self_base:
                # 编辑风长图：缓存判据仍是 report.html 的新旧
                png_path = png_mod.export_reader_png(
                    f"{self_base}/read/{quote(run_id)}?png=1",
                    run_dir / "report.png",
                    get_settings(),
                    cache_key=html_path,
                )
            else:
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

    @app.get("/api/config")
    def api_config() -> dict:
        """非密钥配置摘要：ASR 后端与模型名、LLM 模型名（编辑风页脚版权页展示用）。

        只吐后端类型与模型名，任何密钥与网关地址都不经过此接口。
        """
        s = get_settings()
        asr_model = ""
        if s.asr_backend.startswith("dashscope"):
            asr_model = s.dashscope_model
        elif s.asr_backend == "openai":
            asr_model = os.environ.get("OPENAI_ASR_MODEL", "").strip() or "whisper-1"
        elif s.asr_backend == "local":
            asr_model = os.environ.get("LOCAL_ASR_MODEL", "").strip() or "faster-whisper/small"
        return {
            "asr_backend": s.asr_backend,
            "asr_model": asr_model,
            "llm_model_plan": s.llm_model_plan,
            "llm_model_write": s.llm_model_write,
        }

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

    @app.delete("/api/runs/{run_id}/qa/{index}")
    def delete_qa_record(run_id: str, index: int) -> dict:
        """删除一条历史问答；index 与 GET 返回的 history 下标一致（从 0 起）。

        与提问共用同一把 run 锁，避免删除与追加并发时互相覆盖文件。
        """
        run_dir = library.safe_run_dir(out_root, run_id)
        if run_dir is None:
            raise HTTPException(status_code=404, detail="未找到该 run")
        with qa_lock(run_id):
            removed = qa.delete_history(run_dir, index)
        if removed is None:
            raise HTTPException(status_code=404, detail="未找到这条提问记录")
        return {"run_id": run_id, "deleted": index}

    # --------------------------------------------------------- 模型设置

    def _settings_payload() -> dict:
        """设置弹窗数据：生效配置 + 脱敏 key；完整 key 永不出后端。"""
        s = get_settings()
        user = usersettings.load_user_llm_settings()
        return {
            "base_url": s.llm_base_url,
            "model_plan": s.llm_model_plan,
            "model_write": s.llm_model_write,
            "api_key_masked": usersettings.mask_key(s.llm_api_key),
            "has_key": bool(s.llm_api_key),
            "source": "custom" if user.get("llm_api_key") else "env",
        }

    @app.get("/api/settings")
    def api_get_settings() -> dict:
        return _settings_payload()

    @app.post("/api/settings")
    def api_save_settings(payload: LlmSettingsRequest) -> dict:
        """保存模型设置；空 base_url / 模型名不覆盖已有值。"""
        try:
            usersettings.save_user_llm_settings(
                llm_api_key=(payload.api_key or "").strip() or "keep",
                llm_base_url=payload.base_url,
                llm_model_plan=payload.model_plan,
                llm_model_write=payload.model_write,
            )
        except UsageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _settings_payload()

    @app.delete("/api/settings")
    def api_clear_settings() -> dict:
        """删除用户配置，回退 .env / 内置默认。"""
        usersettings.clear_user_llm_settings()
        return _settings_payload()

    @app.post("/api/settings/test")
    def api_test_settings(payload: LlmSettingsRequest) -> dict:
        """用表单配置（缺省回退生效配置）发一次极小补全，验证连通性。

        表单里没填 key 时沿用已保存/环境里的 key；都没有则用占位串，
        让 Ollama 这类不需要 key 的本地网关也能测。
        """
        s = get_settings()
        base_url = (payload.base_url or "").strip() or s.llm_base_url
        model = (payload.model_plan or "").strip() or s.llm_model_plan
        api_key = _resolve_llm_key(payload.api_key)
        if not base_url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="Base URL 必须以 http(s):// 开头")
        if not model:
            raise HTTPException(status_code=400, detail="请先填写规划模型名")

        from openai import OpenAI

        try:
            client = OpenAI(api_key=api_key, base_url=base_url, timeout=20.0)
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "请只回复两个字：连通"}],
                max_tokens=64,
            )
            choices = getattr(response, "choices", None) or []
            message = getattr(choices[0], "message", None) if choices else None
            content = (getattr(message, "content", None) or "").strip()
            reasoning = (getattr(message, "reasoning_content", None) or "").strip()
        except Exception as exc:  # 连网失败是预期路径，统一转成可读文案
            detail = str(exc)
            raise HTTPException(
                status_code=400, detail=f"连接失败：{detail[:300]}"
            ) from exc
        reply = content or reasoning
        note = ""
        if not content and reasoning:
            note = "连接正常；该模型是推理型（先思维链后正文），测试上限内只产出了思维链"
        elif not reply:
            note = "连接正常（Key 与模型名均有效）；该模型在测试上限内没产出文本，正式写作无此限制"
        return {"ok": True, "model": model, "reply": reply[:50], "note": note}

    def _resolve_llm_key(form_key: str) -> str:
        """表单 key → 已保存/环境 key → 占位串（Ollama 等免 key 网关可用）。"""
        s = get_settings()
        key = (form_key or "").strip() or s.llm_api_key
        return key or "sk-placeholder"

    @app.post("/api/settings/models")
    def api_list_models(payload: ModelListRequest) -> dict:
        """按表单配置调 GET /models，返回该 key 可用的模型 id 列表。

        供前端把「规划/写作模型」变成可下拉选择；拉取失败转 400，
        前端降级为手动输入。列表去重排序后截断到 500 条兜底。
        """
        s = get_settings()
        base_url = (payload.base_url or "").strip() or s.llm_base_url
        api_key = _resolve_llm_key(payload.api_key)
        if not base_url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="Base URL 必须以 http(s):// 开头")

        from openai import OpenAI

        try:
            client = OpenAI(api_key=api_key, base_url=base_url, timeout=15.0)
            ids = [str(m.id) for m in client.models.list()]
        except Exception as exc:
            raise HTTPException(
                status_code=400, detail=f"拉取模型列表失败：{str(exc)[:300]}"
            ) from exc
        models = sorted({i for i in ids if i})[:500]
        return {"models": models, "count": len(models)}

    # ------------------------------------------------------------------ 预检

    @app.post("/api/probe")
    def api_probe(payload: ProbeRequest) -> dict:
        """提交前探测时长；超长由前端拦截，异常统一转 400 让前端降级放行。"""
        try:
            return probe.probe_source(payload.url, out_root=out_root)
        except VidereadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ------------------------------------------------------------- 收藏夹批次

    @app.post("/api/favorites/resolve")
    def api_resolve_favorites(payload: FavoriteResolveRequest) -> dict:
        """读取收藏夹清单（不下载），并标出哪些视频已有报告。"""
        try:
            favorites = download.resolve_favorites(payload.url)
        except VidereadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        for item in favorites["items"]:
            item["existing_report"] = library.existing_report(
                out_root, item["url"], item["bvid"]
            )
        return favorites

    @app.post("/api/batches")
    def api_create_batch(payload: BatchRequest) -> dict:
        """把收藏夹（或其中勾选的部分）一次性投递成一个批次。

        已生成报告 / 超长 / 失效的条目不会进入流水线，而是以 skipped 状态
        留在批次里，页面上仍能看到原因与已有报告入口。
        """
        try:
            favorites = download.resolve_favorites(payload.url)
        except VidereadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        wanted = {value.strip() for value in (payload.items or []) if value.strip()}
        chosen = (
            favorites["items"]
            if not wanted
            else [
                item
                for item in favorites["items"]
                if item["bvid"] in wanted or item["url"] in wanted
            ]
        )
        unique: list[dict] = []
        seen: set[str] = set()
        for item in chosen:
            if item["bvid"] in seen:
                continue
            seen.add(item["bvid"])
            unique.append(item)
        if payload.limit and payload.limit > 0:
            unique = unique[: payload.limit]

        items: list[dict] = []
        for item in unique:
            entry = {
                "url": item["url"],
                "bvid": item["bvid"],
                "title": item["title"],
                "duration": item["duration"],
                "duration_text": item["duration_text"],
                "state": "pending",
            }
            existing = library.existing_report(out_root, item["url"], item["bvid"])
            if not item["available"]:
                entry.update(state="skipped", reason="视频已失效或不可用")
            elif item["too_long"]:
                entry.update(
                    state="skipped",
                    reason=f"超过 {MAX_VIDEO_DURATION_SEC // 3600} 小时上限",
                )
            elif payload.skip_existing and existing:
                entry.update(
                    state="skipped",
                    reason="已有报告",
                    run_id=existing["run_id"],
                    report_url=existing["report_url"],
                )
            items.append(entry)

        runnable = sum(1 for item in items if item["state"] == "pending")
        if runnable == 0:
            raise HTTPException(
                status_code=400,
                detail="选中项里没有需要新解析的视频（已有报告、超长或失效的都已跳过）",
            )

        try:
            job = manager.submit_batch(
                items=items,
                url=payload.url,
                batch_title=favorites["title"],
                mode=payload.mode,
                no_cache=payload.no_cache,
                asr_backend=None,
                frames=payload.frames,
                lang=(payload.lang or "").strip() or None,
            )
        except JobBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except VidereadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "job_id": job.id,
            "state": job.state,
            "batch_title": favorites["title"],
            "total": len(items),
            "runnable": runnable,
            "skipped": len(items) - runnable,
        }

    @app.get("/api/batches/{batch_id}")
    def api_batch_manifest(batch_id: str) -> dict:
        """读取落盘的批次清单：服务重启后工单页仍能看到上一批的篇目与结果。"""
        if not _BATCH_ID_RE.match(batch_id):
            raise HTTPException(status_code=404, detail="未找到该批次")
        path = out_root / "_batches" / f"{batch_id}.json"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="未找到该批次")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="批次清单已损坏") from exc
        data.setdefault("logs", [])
        data.setdefault("batch", True)
        data.setdefault("stage_index", 0)
        return data

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
                lang=(payload.lang or "").strip() or None,
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