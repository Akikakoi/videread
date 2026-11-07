"""流水线编排：阶段调度 + 断点续跑 + 异常归类（对应开发文档 §6.2 / §11.3）。

8 个阶段（§3.1）：下载 → 音频处理 → 转写 → 规范化 → 结构规划 → 截图 → 逐节写作 → 渲染。
每阶段执行前先检查产物；命中则跳过（`use_cache=True`），阶段边界写入 `run.trace.jsonl`。

每个阶段是独立的 `_stage_*` 函数，跨阶段状态集中在 `_RunState`；
`run()` 只负责参数校验、预检与顺序编排。
"""

from __future__ import annotations

import json
import re
import shutil
import uuid
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import audio as audio_mod
from . import download, frames as frames_mod, markdown as markdown_mod, pdf as pdf_mod
from . import png as png_mod
from . import render
from .asr import get_backend, load_raw_jsonl, write_raw_jsonl
from .asr.base import AsrSegment
from .config import (
    FRAME_CLIP_SECONDS,
    MAX_VIDEO_DURATION_SEC,
    REPORT_MODES,
    STAGE_LABELS,
    Settings,
    get_settings,
)
from .download import VideoMeta
from .errors import (
    AsrError,
    AudioError,
    CancelledError,
    LlmError,
    RenderError,
    UsageError,
    VidereadError,
)
from .report import (
    LlmClient,
    Outline,
    check_brief_length,
    plan_outline,
    write_sections,
)
from .report import load as load_outline
from .report import write as write_outline
from .report.writer import brief_hard_max, existing_sections
from .trace import TraceWriter
from .transcript import (
    TRANSCRIPT_FORMAT_VERSION,
    TranscriptUnit,
    build_units,
    format_time,
    load_units,
    merge_subtitles,
    segments_from_srt,
    subtitle_coverage,
    write_jsonl,
    write_markdown,
)

_STAGE_LABELS = STAGE_LABELS

# 字幕至少覆盖视频时长的这个比例，才允许用它替代 ASR（§6.3）
_SUBTITLE_MIN_COVERAGE = 0.5


def _nonempty(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def _resolve_run_dir(out_root: Path, base_id: str, fresh: bool) -> Path:
    """定位本次运行的目录；`fresh`（重新解析）时避开已有目录，另起 `-rN` 后缀。

    重新解析的产物落新目录：之前那份报告原样保留，报告库里两次解析各占
    一条。后缀从 `-r2` 起递增取第一个不存在的名字；`-rN` 刻意沿用 run_id
    白名单字符（报告库正则可识别），而 `~` 等符号会让新报告从列表里消失。
    """
    candidate = out_root / base_id
    if not fresh:
        return candidate
    index = 2
    while candidate.exists():
        candidate = out_root / f"{base_id}-r{index}"
        index += 1
    return candidate


def _cached(text: str) -> str:
    return f"{text}  (缓存命中，跳过)"


_ASR_PARTIAL = "asr.partial.json"
_TRANSCRIPT_VERSION = "transcript.version"


def _transcript_version_ok(run_dir: Path) -> bool:
    """缓存里的转写是否由当前派生规则生成。

    旧 run 没有版本文件；build_units / parse_srt 修复后需要重建一次
    （只重算本地文本，不重新调用 ASR）。
    """
    try:
        value = (run_dir / _TRANSCRIPT_VERSION).read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return value == str(TRANSCRIPT_FORMAT_VERSION)


def _clear_frames(run_dir: Path) -> bool:
    """删除该 run 的截图产物；返回删除前是否真的存在。

    大纲或模式变化后，旧截图对应的是旧章节；继续复用会把不相干的画面
    塞进新报告。封面式缓存（report.html）是否重渲染由调用方决定。
    """
    had = (run_dir / "frames").is_dir() or (run_dir / "frames.jsonl").is_file()
    shutil.rmtree(run_dir / "frames", ignore_errors=True)
    (run_dir / "frames.jsonl").unlink(missing_ok=True)
    return had


def _clear_overwritten_artifacts(run_dir: Path) -> None:
    """覆盖重跑前清掉不会随本次运行自动重写的旧展示产物。

    `qa.jsonl` 绑定旧转写稿的问答历史，`frames/` 与 `frames.jsonl` 绑定旧大纲的
    截图；ASR 分段缓存（`asr.part.*` 与 `asr_chunks/`）若不删，`--no-cache`
    仍会复用旧转写结果，"全量重跑"只是名义上的。旧导出的 PDF / PNG / MD
    会因 report.html 变新而自动重出或按需重出。
    """
    (run_dir / "qa.jsonl").unlink(missing_ok=True)
    (run_dir / _ASR_PARTIAL).unlink(missing_ok=True)
    for part in run_dir.glob("asr.part.*.jsonl"):
        part.unlink(missing_ok=True)
    shutil.rmtree(run_dir / "asr_chunks", ignore_errors=True)
    _clear_frames(run_dir)


_MODE_MARKER = "run.mode"

# 报告来源区块记录了渲染时使用的阅读模式（模式名固定英文）。旧 run 没有
# `run.mode` 标记，切换模式时无法判断正文属于哪种模式；从报告反推一次，
# 既能迁移旧缓存，又不会让「同模式重跑」误付一次正文重写。
_MODE_LABEL = re.compile(
    r"(?:阅读模式|Reading mode|閲読モード)\s*[：:]\s*(Standard|Deep|Brief|Outline)",
    re.IGNORECASE,
)


def _infer_report_mode(run_dir: Path) -> str:
    """从已有 report.html 反推阅读模式；没有报告或旧模板无标记时返回空串。"""
    path = run_dir / "report.html"
    try:
        html = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = _MODE_LABEL.search(html)
    return match.group(1).strip().lower() if match else ""


def _sync_mode(run_dir: Path, mode: str) -> bool:
    """记录本次阅读模式，返回是否发生了模式切换。

    正文片段只按语言做了缓存标记，不会区分 standard / brief / outline；
    换模式时若直接复用旧片段，会拼出旧篇幅与旧组件的正文。因此模式变化时
    先清空 `sections/`，让调用方置 `force_report` 重写正文与重渲染。

    标记缺失（历史产物）时从报告反推模式：反推得到旧模式就按它比较；
    反推不出（无报告 / 旧模板）且本次要求非默认模式，则视为一次模式变化。
    这样同模式重跑旧 run 不会白付一次 LLM，切换模式又能真正生效。
    """
    marker = run_dir / _MODE_MARKER
    try:
        previous = marker.read_text(encoding="utf-8").strip().lower()
    except OSError:
        previous = ""
    if previous not in REPORT_MODES:
        previous = _infer_report_mode(run_dir)
    changed = bool(previous) and previous != mode
    if not previous and mode != "standard":
        changed = True
    if changed:
        shutil.rmtree(run_dir / "sections", ignore_errors=True)
    try:
        marker.write_text(mode + "\n", encoding="utf-8", newline="\n")
    except OSError:
        # 标记写失败不该阻断报告；下次按首次运行处理
        pass
    return changed


def _section_lang(lang: str) -> str:
    """与 `report.writer._norm_lang` 同口径：zh 变体折叠为 zh。

    片段首行标记按折叠后的语言写入；这里不折叠会把 zh-CN 的完整缓存
    误判为缺失，导致每次重跑都空转一次逐节写作判定。
    """
    value = (lang or "").strip().lower()
    return "zh" if value in {"", "zh-cn", "zh-hans"} else value


def _sections_complete(out_dir: Path, outline: Outline, lang: str) -> bool:
    """全部小节都有**同语言**的非空片段才算完整（片段与语言绑定，
    见 existing_sections：换语言后旧片段必须重写而非复用）。"""
    found = existing_sections(out_dir, _section_lang(lang))
    return all(found.get(section.id) for section in outline.sections)


def _report_is_stale(run_dir: Path, report_path: Path) -> bool:
    """报告是否落后于它的大纲 / 正文片段（mtime 兜底）。

    `force_report` 已覆盖正常路径；这里再按文件时间兜一次，防止外部修改、
    上次运行在渲染前中断等场景把旧报告当成新结果返回。
    """
    try:
        report_mtime = report_path.stat().st_mtime_ns
    except OSError:
        return False
    candidates = [run_dir / "outline.json"]
    directory = run_dir / "sections"
    if directory.is_dir():
        candidates.extend(directory.glob("s*.html"))
    for path in candidates:
        try:
            if path.is_file() and path.stat().st_mtime_ns > report_mtime:
                return True
        except OSError:
            continue
    return False


def _asr_cached(use_cache: bool, run_dir: Path, *, force_asr: bool = False) -> bool:
    """ASR 原始结果是否可作为完整缓存复用。

    部分切段失败的 raw 会带 `asr.partial.json` 标记：它足以生成当前报告，
    但不能算完成，下次运行必须重试缺失段；`force_asr` 同样跳过缓存。
    """
    return (
        use_cache
        and not force_asr
        and _nonempty(run_dir / "asr.raw.jsonl")
        and not (run_dir / _ASR_PARTIAL).is_file()
    )


@dataclass
class _RunState:
    """单次流水线运行中跨阶段共享的状态。"""

    url: str
    mode: str
    out_root: Path
    use_cache: bool
    keep_audio: bool
    force_asr: bool
    force_report: bool
    settings: Settings
    progress: Callable[[str], None]
    cancel_check: Callable[[], None] | None = None

    is_local: bool = False
    # 本次 ASR 是否真的重跑过（不是缓存命中）：转写与大纲都必须跟着重建
    asr_fresh: bool = False
    # 只重渲染、不重写正文（关闭截图后去掉已内嵌的图片）
    force_render: bool = False
    fresh_dir: bool = False
    lang: str = "zh"
    run_dir: Path | None = None
    trace: TraceWriter | None = None
    meta: VideoMeta | None = None
    subtitle_path: Path | None = None
    source_audio: Path | None = None
    wav: Path | None = None
    segments: list[AsrSegment] | None = None
    units: list[TranscriptUnit] | None = None
    outline: Outline | None = None
    parts: list[str] | None = None
    client: LlmClient | None = None
    frame_files: dict[str, Path] = field(default_factory=dict)
    frame_moments: dict[str, float] = field(default_factory=dict)
    produced: list[Path] = field(default_factory=list)

    def say(self, index: int, detail: str) -> None:
        self.progress(
            f"[{index}/{len(_STAGE_LABELS)}] {_STAGE_LABELS[index - 1]} ✓ {detail}"
        )


def run(
    url: str,
    *,
    mode: str = "standard",
    out_root: Path,
    use_cache: bool = True,
    keep_audio: bool = False,
    asr_backend: str | None = None,
    force_asr: bool = False,
    open_report: bool = False,
    force_report: bool = False,
    frames: bool | None = None,
    export_pdf: bool = False,
    export_png: bool = False,
    export_md: bool = False,
    fresh_dir: bool = False,
    report_lang: str | None = None,
    cancel_check: Callable[[], None] | None = None,
    progress: Callable[[str], None] = print,
) -> Path:
    """执行完整流水线，返回 report.html 路径。

    `frames=None` 表示跟随配置（.env 的 FRAMES / 默认关）；
    显式 True / False 覆盖配置。`export_pdf` / `export_png` / `export_md`
    为 True 时在渲染后分别导出 report.pdf / report.png / report.md
    （PDF 与长图由本机 Edge / Chrome 无头打印 / 截图，已存在且较新则复用）。
    `fresh_dir=True`（重新解析）落 `-rN` 新目录，不覆盖已有报告；
    调用方应同时给 `use_cache=False`，否则等于照抄旧产物没有意义。
    `use_cache=False` 且未另起新目录时视为覆盖：先清掉旧 `qa.jsonl` 与截图，
    避免新报告继续挂载上一版的问答历史与旧大纲截图。

    `cancel_check` 是取消检查点回调（Web 控制台取消任务用）：在每个阶段
    边界与长阶段内部（ASR 切段之间、逐节写作各节之间）被调用，抛
    `CancelledError` 即终止；已落盘产物照常保留，重跑可续。
    """
    mode = (mode or "").strip().lower()
    if mode not in REPORT_MODES:
        raise UsageError(f"未知模式：{mode!r}（可选 standard / deep / brief / outline）")
    url = download.normalize_source(url)
    out_root = Path(out_root)
    settings = get_settings(asr_backend=asr_backend, frames_enabled=frames)
    # 报告语言：CLI --lang > .env 的 REPORT_LANG > zh。转写稿保持源语言不动，
    # 语言只作用于结构规划、逐节写作与问答的自然语言产出。
    state_lang = (report_lang or settings.report_lang or "zh").strip().lower()
    # 启动即校验 LLM 密钥：结构规划在下载与 ASR 之后，等到第 5 阶段才
    # 发现缺 key，前面的下载与转写成本就白花了。ASR 密钥不在此列：
    # 字幕优先路径不经过 ASR，其校验留在后端构造时进行。
    settings.require_llm()

    state = _RunState(
        url=url,
        mode=mode,
        out_root=out_root,
        use_cache=use_cache,
        keep_audio=keep_audio,
        force_asr=force_asr,
        force_report=force_report,
        fresh_dir=fresh_dir,
        lang=state_lang,
        settings=settings,
        progress=progress,
        cancel_check=cancel_check,
    )

    def _checkpoint() -> None:
        """阶段边界的取消检查点：抛 CancelledError 时已完成阶段不受影响。"""
        if cancel_check is not None:
            cancel_check()

    current = _STAGE_LABELS[0]
    try:
        _checkpoint()
        _stage_download(state)
        current = _STAGE_LABELS[1]
        _checkpoint()
        _stage_audio(state)
        current = _STAGE_LABELS[2]
        _checkpoint()
        _stage_asr(state)
        current = _STAGE_LABELS[3]
        _checkpoint()
        _stage_transcript(state)
        current = _STAGE_LABELS[4]
        _checkpoint()
        _stage_outline(state)
        current = _STAGE_LABELS[5]
        _checkpoint()
        _stage_frames(state)
        current = _STAGE_LABELS[6]
        _checkpoint()
        _stage_sections(state)
        current = _STAGE_LABELS[7]
        _checkpoint()
        report_path = _stage_render(state)

        # 跑完自动清理中间产物。audio.wav 是这里体积最大的一项（2h 视频约 230MB，
        # 实测占整个 run 目录的 99.9%），而它只是 ASR 的输入——ASR 结果已落盘到
        # asr.raw.jsonl，后续阶段不再读它，故删除不影响报告与各级缓存。
        # 代价：将来重跑 ASR（如换用别的转写通道）需要重新下载并转码一次音频。
        # 字幕文件不在此列：体积小，且是「字幕优先」判断覆盖率所必需。
        # ASR 有失败分段时保留 wav：下次只需补失败段，避免重新下载整段音频。
        partial_asr = (state.run_dir / _ASR_PARTIAL).is_file()
        if (
            state.wav is not None
            and not keep_audio
            and not partial_asr
            and _nonempty(state.wav)
        ):
            freed = state.wav.stat().st_size / 1024 / 1024
            state.wav.unlink(missing_ok=True)
            progress(f"已清理中间产物 audio.wav，释放 {freed:.1f} MB")

        if export_pdf:
            pdf_path = pdf_mod.export_pdf(report_path, state.run_dir / "report.pdf", settings)
            state.produced.append(pdf_path)
            progress(f"已导出 PDF → {pdf_path.name}")

        if export_png:
            png_path = png_mod.export_png(report_path, state.run_dir / "report.png", settings)
            state.produced.append(png_path)
            progress(f"已导出长图 → {png_path.name}")

        if export_md:
            if frames_mod.has_frames(state.run_dir):
                raise UsageError(
                    "带截图的报告无法导出 Markdown：截图以相对路径引用会离开 run 目录"
                    "就失效，请改用 --pdf / --png，或去掉 --frames 重新解析"
                )
            md_path = state.run_dir / "report.md"
            md_path.write_text(
                markdown_mod.html_to_markdown(report_path.read_text(encoding="utf-8")),
                encoding="utf-8",
            )
            state.produced.append(md_path)
            progress(f"已导出 Markdown → {md_path.name}")

        if open_report:
            webbrowser.open(report_path.resolve().as_uri())
        return report_path
    except CancelledError:
        # 取消不是失败：照常列出已生成产物，提示可以续跑
        progress("任务已取消，已生成产物保留（可据此续跑）：")
        for path in state.produced:
            progress(f"  - {path}")
        raise
    except VidereadError as exc:
        progress(f"[错误] 阶段「{current}」失败：{type(exc).__name__}")
        if state.produced:
            progress("已生成的产物（可据此续跑）：")
            for path in state.produced:
                progress(f"  - {path}")
        raise


# ------------------------------------------------------------ [1/7] 下载


def _stage_download(state: _RunState) -> None:
    local_path = download.local_path(state.url)
    if local_path is None and not (state.url or "").strip().lower().startswith(
        ("http://", "https://")
    ):
        raise UsageError(f"参数既不是有效链接、也不是存在的本地文件：{state.url}")
    state.is_local = local_path is not None

    if state.is_local:
        detail = _download_local(state, local_path)
    else:
        detail = _download_remote(state)

    state.say(1, detail)


def _download_local(state: _RunState, local_path: Path) -> str:
    """本地文件：探测时长、构造 meta、统一时长上限。"""
    assert local_path is not None
    run_dir = _resolve_run_dir(
        state.out_root,
        download.make_run_id(local_path.resolve().as_uri(), "local"),
        state.fresh_dir,
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    if not state.use_cache and not state.fresh_dir:
        _clear_overwritten_artifacts(run_dir)
    if _sync_mode(run_dir, state.mode):
        state.force_report = True
    trace = TraceWriter(run_dir / "run.trace.jsonl")
    meta_path = run_dir / "meta.json"
    with trace.stage("download") as stage:
        cached = state.use_cache and _nonempty(meta_path)
        if cached:
            meta = download.read_meta(meta_path)
            stage.detail["cached"] = True
            detail = _cached(f"meta.json  {meta.title}  时长 {format_time(meta.duration)}")
        else:
            duration = audio_mod.probe_duration(local_path, state.settings)
            meta = download.local_meta(local_path, duration)
            stage.detail.update({"mode": "local", "duration": duration})
            detail = f"meta.json  {local_path.name}  时长 {format_time(duration)}"
        if meta.duration > MAX_VIDEO_DURATION_SEC:
            # 远程由 fetch_meta 拦截，本地此前不校验；统一到同一上限（§6.6）
            # 校验放在写 meta.json 之前，避免被拒的视频在报告库留下条目
            raise UsageError(
                f"视频时长 {meta.duration / 3600:.1f} 小时，超过 "
                f"{MAX_VIDEO_DURATION_SEC / 3600:.0f} 小时上限，"
                "为避免 ASR 成本失控已拒绝"
            )
        if not cached:
            download.write_meta(meta, meta_path)
    state.run_dir = run_dir
    state.trace = trace
    state.meta = meta
    state.source_audio = local_path
    state.produced.append(meta_path)
    return detail


def _guard_preview_audio(source_audio: Path, meta_duration: float, settings: Settings) -> None:
    """试看预览兜底：音频远短于元信息时长 = 登录态无完整观看权限
    （充电专属/付费内容）。在进 ASR 前中止，避免白烧转写费用；对缓存命中
    的音频同样生效，能把历史遗留的预览缓存也拦下来。"""
    if meta_duration <= 0:
        return
    actual = audio_mod.probe_duration(source_audio, settings)
    if actual < meta_duration * download.PREVIEW_DURATION_RATIO:
        raise download.DownloadError(
            f"只下载到了 {format_time(actual)} 的试看内容，远短于完整的 "
            f"{format_time(meta_duration)}——这个视频需要付费观看"
            "（充电专属或付费课程），暂不支持解析这类视频，"
            "已停止处理，未进行转写。"
        )


def _download_remote(state: _RunState) -> str:
    """远程视频：抓取元信息与字幕（合并为一次 yt-dlp 调用），按需下载音频。"""
    url_text = (state.url or "").strip()
    guess_dir = _resolve_run_dir(
        state.out_root,
        download.make_run_id(url_text, download.extract_bvid(url_text)),
        state.fresh_dir,
    )
    meta_cached = state.use_cache and _nonempty(guess_dir / "meta.json")
    if meta_cached:
        meta = download.read_meta(guess_dir / "meta.json")
        if download.canonical_url(meta.url) != download.canonical_url(url_text):
            state.progress(
                f"提示：{guess_dir.name} 的 meta.json 属于 {meta.url}，"
                "与本次请求不一致，已重新抓取"
            )
            meta_cached = False
    if meta_cached:
        run_dir = guess_dir
    else:
        run_dir, meta = _fetch_meta_with_subtitles(state, url_text)
    run_dir.mkdir(parents=True, exist_ok=True)
    if not state.use_cache and not state.fresh_dir:
        _clear_overwritten_artifacts(run_dir)
    if _sync_mode(run_dir, state.mode):
        state.force_report = True
    trace = TraceWriter(run_dir / "run.trace.jsonl")
    wav_path = run_dir / "audio.wav"
    did_download = False
    with trace.stage("download") as stage:
        if not meta_cached:
            download.write_meta(meta, run_dir / "meta.json")
            stage.detail["subtitles"] = len(meta.subtitles)
            if not meta.subtitles:
                # 静默空字幕最伤「字幕优先」：用户不知道这条省 ASR 的路被堵了。
                # 只在本次真实抓取时提示（缓存命中重跑不再念叨）。
                # 静态提示覆盖「没配置/没导出登录态」；cookies 看似齐全但服务端
                # 会话已失效的情况靠实时校验兜住（nav 接口一次请求，失败即静默）。
                hint = download.subtitle_login_hint(state.settings)
                if hint:
                    state.progress(f"提示：{hint}")
                elif download.extract_bvid(url_text):
                    expired = download.sessdata_expired_hint(state.settings)
                    if expired:
                        state.progress(f"提示：{expired}")
        else:
            stage.detail["cached"] = True

        # 字幕优先：字幕够完整就不再下载音频、不再调用 ASR
        if state.settings.subtitle_first and not state.force_asr:
            candidate = download.pick_subtitle(meta.subtitles, run_dir)
            if candidate is None:
                if meta.subtitles:
                    state.progress(
                        f"提示：meta.json 记录的字幕（{len(meta.subtitles)} 份）"
                        "已不在磁盘上，回退到下载音频 + ASR"
                    )
            else:
                coverage = subtitle_coverage(candidate, meta.duration)
                stage.detail["subtitle_coverage"] = round(coverage, 3)
                if coverage >= _SUBTITLE_MIN_COVERAGE:
                    state.subtitle_path = candidate
                else:
                    state.progress(
                        f"提示：{candidate.name} 只覆盖约 {coverage:.0%} 的视频时长，"
                        "判为不完整，回退到下载音频 + ASR"
                    )

        # 音频的唯一消费方是 ASR：字幕优先或 ASR 已缓存时，下载与转码都应跳过，
        # 否则缓存命中的重跑也会把刚删掉的音频重新下载一遍
        need_audio = state.subtitle_path is None and not _asr_cached(
            state.use_cache, run_dir, force_asr=state.force_asr
        )
        source_audio = download.find_audio(run_dir) if need_audio else None
        if (
            need_audio
            and source_audio is None
            and not (state.use_cache and _nonempty(wav_path))
        ):
            source_audio = download.download_audio(meta.url, run_dir, state.settings)
            did_download = True
            stage.detail["audio"] = source_audio.name
        if source_audio is not None:
            _guard_preview_audio(source_audio, meta.duration, state.settings)
        if state.subtitle_path is not None:
            stage.detail["subtitle_first"] = state.subtitle_path.name
    detail = f"meta.json  {meta.bvid}  时长 {format_time(meta.duration)}"
    if state.subtitle_path is not None:
        detail += f"  字幕优先 {state.subtitle_path.name}（跳过音频与 ASR）"
    elif meta_cached and not did_download:
        detail = _cached(detail)
    state.run_dir = run_dir
    state.trace = trace
    state.meta = meta
    state.source_audio = source_audio
    state.produced.append(run_dir / "meta.json")
    return detail


def _fetch_meta_with_subtitles(state: _RunState, url_text: str) -> tuple[Path, VideoMeta]:
    """一次 yt-dlp 调用拿元信息 + 平台字幕。

    run 目录名依赖 meta（短链要等元信息才知道 BV 号），因此字幕先落到
    out_root 下的暂存目录，拿到 run 目录后再移入。
    """
    staging = state.out_root / f".staging-{uuid.uuid4().hex[:8]}"
    try:
        meta = download.fetch_meta(url_text, state.settings, subtitle_dir=staging)
        run_dir = _resolve_run_dir(
            state.out_root, download.make_run_id(meta.url, meta.bvid), state.fresh_dir
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        if staging.is_dir():
            for srt in sorted(staging.glob("subtitle.*.srt")):
                srt.replace(run_dir / srt.name)
            meta.subtitles = download.collect_subtitles(run_dir)
        return run_dir, meta
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# -------------------------------------------------------- [2/7] 音频处理


def _stage_audio(state: _RunState) -> None:
    assert state.run_dir is not None and state.trace is not None
    wav = state.run_dir / "audio.wav"
    state.wav = wav
    with state.trace.stage("audio") as stage:
        if state.subtitle_path is not None:
            stage.detail["skipped"] = "字幕优先，无需音频"
            detail = "字幕优先，跳过音频处理"
        elif _asr_cached(
            state.use_cache, state.run_dir, force_asr=state.force_asr
        ):
            # ASR 结果已缓存时音频没有消费方：不转码；残留的旧 wav
            # 由跑完后的统一清理兜底删除
            stage.detail["skipped"] = "ASR 已缓存，无需音频"
            detail = "ASR 已缓存，跳过音频处理"
        elif state.use_cache and _nonempty(wav):
            stage.detail["cached"] = True
            detail = _cached("16kHz 单声道 wav")
        else:
            source_audio = state.source_audio
            if source_audio is None or not _nonempty(source_audio):
                source_audio = download.find_audio(state.run_dir) or wav
            if not _nonempty(source_audio):
                raise AudioError(f"缺少可转码的音频源：{state.run_dir}")
            audio_mod.to_wav_16k_mono(source_audio, wav, state.settings)
            stage.detail["source"] = source_audio.name
            detail = "16kHz 单声道 wav"
            if (
                not state.is_local
                and not state.keep_audio
                and source_audio.parent == state.run_dir
            ):
                source_audio.unlink(missing_ok=True)
                stage.detail["removed_source"] = source_audio.name
            state.produced.append(wav)
    state.say(2, detail)


# ------------------------------------------------------------ [3/7] 转写


def _stage_asr(state: _RunState) -> None:
    assert state.run_dir is not None and state.trace is not None and state.meta is not None
    raw_path = state.run_dir / "asr.raw.jsonl"
    with state.trace.stage("asr") as stage:
        if not state.use_cache:
            # 覆盖重跑：旧 raw 先作废。否则本次 ASR 失败后，旧 raw 会在下一次
            # 普通重跑里被当成完整缓存，跳过转写并用旧报告「假成功」。
            raw_path.unlink(missing_ok=True)
            (state.run_dir / _ASR_PARTIAL).unlink(missing_ok=True)
        if _asr_cached(
            state.use_cache, state.run_dir, force_asr=state.force_asr
        ):
            segments = load_raw_jsonl(raw_path)
            stage.detail.update({"segments": len(segments), "cached": True})
            detail = _cached(f"{len(segments)} 段")
        elif state.subtitle_path is not None:
            # 直接由平台字幕构造分段，完全不经过 ASR
            segments = segments_from_srt(state.subtitle_path)
            if not segments:
                raise AsrError(f"字幕未解析出任何内容：{state.subtitle_path.name}")
            write_raw_jsonl(segments, raw_path)
            (state.run_dir / _ASR_PARTIAL).unlink(missing_ok=True)
            state.asr_fresh = True
            stage.detail.update({"segments": len(segments), "source": "subtitle"})
            detail = f"{len(segments)} 段（平台字幕）"
        else:
            assert state.wav is not None
            backend = get_backend(
                state.settings,
                cache_dir=state.run_dir,
                progress=state.progress,
                cancel_check=state.cancel_check,
            )
            segments = backend.transcribe(state.wav, duration=state.meta.duration)
            write_raw_jsonl(segments, raw_path)
            state.asr_fresh = True
            failures = list(getattr(backend, "failures", []) or [])
            # 部分段失败仍可出报告，但不能当成完整缓存：留标记让下次只补失败段
            marker = state.run_dir / _ASR_PARTIAL
            if failures:
                marker.write_text(
                    json.dumps(failures, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                stage.detail["failures"] = failures
            else:
                marker.unlink(missing_ok=True)
            stage.detail["segments"] = len(segments)
            detail = f"{len(segments)} 段"
    state.produced.append(raw_path)
    state.segments = segments
    state.say(3, detail)


# ---------------------------------------------------------- [4/7] 规范化


def _stage_transcript(state: _RunState) -> None:
    assert (
        state.run_dir is not None
        and state.trace is not None
        and state.meta is not None
        and state.segments is not None
    )
    jsonl_path = state.run_dir / "transcript.jsonl"
    markdown_path = state.run_dir / "transcript.md"
    with state.trace.stage("transcript") as stage:
        if (
            state.use_cache
            and not state.force_asr
            and not state.asr_fresh
            and _nonempty(jsonl_path)
            and _nonempty(markdown_path)
            and _transcript_version_ok(state.run_dir)
        ):
            units = load_units(jsonl_path)
            stage.detail.update({"units": len(units), "cached": True})
            detail = _cached(f"{len(units)} 单元  u0001-u{len(units):04d}")
        else:
            units = build_units(state.segments)
            # 与字幕优先共用同一套语言优先级，避免出现「用中文字幕建稿、
            # 又被英文字幕覆盖」这类前后不一致
            srt = download.pick_subtitle(state.meta.subtitles, state.run_dir)
            if srt is not None:
                units = merge_subtitles(units, srt)
                stage.detail["subtitle"] = srt.name
            if not units:
                raise AsrError("转写规范化后没有任何可用单元")
            write_jsonl(units, jsonl_path)
            write_markdown(units, markdown_path)
            (state.run_dir / _TRANSCRIPT_VERSION).write_text(
                f"{TRANSCRIPT_FORMAT_VERSION}\n", encoding="utf-8", newline="\n"
            )
            stage.detail["units"] = len(units)
            detail = f"{len(units)} 单元  u0001-u{len(units):04d}"
    state.produced.extend([jsonl_path, markdown_path])
    state.units = units
    state.say(4, detail)


# -------------------------------------------------------- [5/7] 结构规划


def _stage_outline(state: _RunState) -> None:
    assert state.run_dir is not None and state.trace is not None
    outline_path = state.run_dir / "outline.json"
    client: LlmClient | None = None
    with state.trace.stage("outline") as stage:
        outline: Outline | None = None
        detail = ""
        if (
            state.use_cache
            and not state.force_asr
            and not state.asr_fresh
            and _nonempty(outline_path)
        ):
            cached_outline = load_outline(outline_path)
            if cached_outline.lang == state.lang:
                outline = cached_outline
                stage.detail.update(
                    {
                        "sections": len(outline.sections),
                        "profile": outline.profile,
                        "lang": outline.lang,
                        "cached": True,
                    }
                )
                detail = _cached(f"{len(outline.sections)} 节  profile={outline.profile}")
            else:
                # 语言不同即缓存未命中：大纲与正文都要按新语言重出。
                # 转写稿与 ASR 缓存不受影响（它们保持源语言）。
                stage.detail["lang_changed"] = f"{cached_outline.lang} -> {state.lang}"
                state.force_report = True
        if outline is None:
            assert state.units is not None and state.meta is not None
            client = LlmClient(state.settings)
            outline = plan_outline(
                state.meta,
                state.units,
                lang=state.lang,
                client=client,
                settings=state.settings,
                progress=state.progress,
            )
            # 大纲重出意味着章节来源可能变了：旧截图与旧正文都不再可信。
            # 放在规划成功之后再清，规划失败时旧报告仍保持完整。
            _clear_frames(state.run_dir)
            shutil.rmtree(state.run_dir / "sections", ignore_errors=True)
            state.force_report = True
            write_outline(outline, outline_path)
            stage.detail.update(
                {
                    "sections": len(outline.sections),
                    "profile": outline.profile,
                    "lang": state.lang,
                    "tokens": client.usage.total,
                }
            )
            detail = (
                f"{len(outline.sections)} 节  profile={outline.profile}  "
                f"{client.usage.describe()}"
            )
    state.produced.append(outline_path)
    state.outline = outline
    state.client = client
    state.say(5, detail)


# ------------------------------------------------------------ [6/8] 截图


def _stage_frames(state: _RunState) -> None:
    """按大纲节定点抽帧（纯展示模式）。任何失败降级为纯文字报告。"""
    assert state.run_dir is not None and state.trace is not None
    assert state.outline is not None and state.units is not None and state.meta is not None
    with state.trace.stage("frames") as stage:
        if not state.settings.frames_enabled:
            # 关闭截图时清掉旧帧，否则新报告会被旧画面与「带截图」标记污染
            if _clear_frames(state.run_dir):
                state.force_render = True
            stage.detail["skipped"] = "未开启截图"
            detail = "截图未开启（--frames 或 FRAMES=1 可开启）"
        else:
            try:
                points = frames_mod.plan_points(state.outline, state.units)
                state.frame_moments = {
                    section_id: moment for section_id, moment, _ in points
                }
                directory = frames_mod.frame_dir(state.run_dir)
                for index, (section_id, moment, label) in enumerate(points):
                    # 取消不属于「可降级失败」：必须直接向上抛，不能被吞成降级
                    if state.cancel_check is not None:
                        state.cancel_check()
                    dest = directory / f"{section_id}.jpg"
                    if _nonempty(dest):
                        state.frame_files[section_id] = dest
                        state.progress(f"第 {section_id} 节截图缓存命中，跳过")
                        continue  # 断点续跑：已有的帧不重复抽
                    if state.is_local:
                        # 本地文件本身就是完整视频，直接按绝对时间抽帧
                        assert state.source_audio is not None
                        frames_mod.extract_frame(
                            state.source_audio, dest, at=moment, settings=state.settings
                        )
                    else:
                        start, offset = frames_mod.clip_bounds(moment)
                        clip = download.download_video_section(
                            state.meta.url,
                            state.run_dir,
                            index,
                            start,
                            FRAME_CLIP_SECONDS,
                            state.settings,
                            max_height=720,
                        )
                        try:
                            frames_mod.extract_frame(
                                clip, dest, at=offset, settings=state.settings
                            )
                        finally:
                            clip.unlink(missing_ok=True)
                    state.frame_files[section_id] = dest
                    state.progress(f"第 {section_id} 节截图完成 @ {label}")
                stage.detail["frames"] = len(state.frame_files)
                detail = f"{len(state.frame_files)}/{len(points)} 节"
            except CancelledError:
                raise
            except VidereadError as exc:
                # 截图是增强能力：失败不阻断报告，已成功的帧照常内嵌
                stage.detail["degraded"] = str(exc)[:300]
                detail = f"截图失败已降级：{str(exc)[:80]}"
                state.progress(f"提示：截图阶段降级，报告将以纯文字为主（{exc}）")
    state.produced.extend(state.frame_files.values())
    state.say(6, detail)


# -------------------------------------------------------- [7/8] 逐节写作


def _stage_sections(state: _RunState) -> None:
    assert state.run_dir is not None and state.trace is not None
    assert state.outline is not None and state.units is not None
    with state.trace.stage("sections") as stage:
        complete = (
            state.use_cache
            and not state.force_report
            and _sections_complete(state.run_dir, state.outline, state.lang)
        )
        if not complete and state.client is None:
            state.client = LlmClient(state.settings)
        parts = write_sections(
            state.outline,
            state.units,
            mode=state.mode,
            lang=state.lang,
            out_dir=state.run_dir,
            client=state.client,
            settings=state.settings,
            progress=state.progress,
            force=state.force_report,
            allowed_classes=render.template_classes(render.template_for(state.mode)),
            cancel_check=state.cancel_check,
        )
        if not complete:
            # 本次至少有一节被重写（或分节缓存被判缺失）：旧报告不再可信。
            # 放在写作之后，避免把「只补缺失节」升级成整篇重写。
            state.force_report = True
        stage.detail["sections"] = len(parts)
        detail = f"{len(parts)}/{len(state.outline.sections)} 节"
        if complete:
            stage.detail["cached"] = True
            detail = _cached(detail)
        elif state.client is not None:
            stage.detail["tokens"] = state.client.usage.total
            detail += f"  {state.client.usage.describe()}"
        if state.mode == "brief":
            length = check_brief_length(parts, state.lang)
            stage.detail["brief_chars"] = length.chars
            detail += f"  Brief {length.chars} {length.unit}"
            for issue in length.issues:
                state.progress(f"  Brief 字数提示：{issue}")
            if length.over_hard:
                raise LlmError(
                    f"Brief 正文 {length.chars} {length.unit}，超过硬上限 "
                    f"{brief_hard_max(state.lang)} {length.unit}；"
                    "删除 runs/<run-id>/sections/ 后重跑可重新生成，或改用 --mode standard"
                )
    state.parts = parts
    state.say(7, detail)


# ------------------------------------------------------------ [8/8] 渲染


def _stage_render(state: _RunState) -> Path:
    assert state.run_dir is not None and state.trace is not None
    assert (
        state.meta is not None
        and state.outline is not None
        and state.parts is not None
        and state.units is not None
    )
    report_path = state.run_dir / "report.html"
    with state.trace.stage("render") as stage:
        cached_ok = (
            state.use_cache
            and not state.force_report
            and not state.force_render
            and _nonempty(report_path)
        )
        if cached_ok:
            html = report_path.read_text(encoding="utf-8")
            # 截图晚于报告生成时（如先跑完文字、再开 --frames 重跑），
            # 老报告里的帧数少于本次持有数，必须重新渲染而不是吃缓存
            if state.frame_files:
                figures_in_html = html.count("data:image/jpeg;base64,")
                if figures_in_html < len(state.frame_files):
                    cached_ok = False
                    stage.detail["rerender_for_frames"] = True
            # 大纲 / 正文片段比报告新（分节重写、渲染前中断、外部修改）：
            # 旧报告不能代表当前产物
            if _report_is_stale(state.run_dir, report_path):
                cached_ok = False
                stage.detail["rerender_for_sections"] = True
        if cached_ok:
            stage.detail["cached"] = True
            detail = _cached("report.html  自包含检查通过")
        else:
            # 截图注入在渲染前：落盘的分节片段不含 figure，
            # 每次真实渲染都从 frame_files 现注入
            if state.frame_files:
                parts = frames_mod.inject_figures(
                    state.parts, state.outline, state.frame_files, state.frame_moments
                )
            else:
                parts = state.parts
            ctx = render.context(
                state.meta,
                state.outline,
                parts,
                state.units,
                state.settings,
                mode=state.mode,
                subtitle_first=state.subtitle_path is not None,
                lang=state.lang,
            )
            render.render_report(
                template=render.template_for(state.mode), ctx=ctx, out=report_path
            )
            html = report_path.read_text(encoding="utf-8")
            detail = "report.html  自包含检查通过"
        violations = render.check_self_contained(html)
        stage.detail["bytes"] = len(html.encode("utf-8"))
        stage.detail["self_contained"] = not violations
        if violations:
            raise RenderError("自包含检查未通过：" + "；".join(violations))
    state.produced.append(report_path)
    state.say(8, detail)
    return report_path
