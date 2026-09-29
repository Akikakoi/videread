"""流水线编排：阶段调度 + 断点续跑 + 异常归类（对应开发文档 §6.2 / §11.3）。

7 个阶段（§3.1）：下载 → 音频处理 → 转写 → 规范化 → 结构规划 → 逐节写作 → 渲染。
每阶段执行前先检查产物；命中则跳过（`use_cache=True`），阶段边界写入 `run.trace.jsonl`。

每个阶段是独立的 `_stage_*` 函数，跨阶段状态集中在 `_RunState`；
`run()` 只负责参数校验、预检与顺序编排。
"""

from __future__ import annotations

import shutil
import uuid
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import audio as audio_mod
from . import download, render
from .asr import get_backend, load_raw_jsonl, write_raw_jsonl
from .asr.base import AsrSegment
from .config import MAX_VIDEO_DURATION_SEC, Settings, get_settings
from .download import VideoMeta
from .errors import AsrError, AudioError, LlmError, RenderError, UsageError, VidereadError
from .report import (
    LlmClient,
    Outline,
    check_brief_length,
    plan_outline,
    write_sections,
)
from .report import load as load_outline
from .report import write as write_outline
from .report.writer import BRIEF_HARD_MAX, section_dir
from .trace import TraceWriter
from .transcript import (
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

_MODES = ("standard", "brief")
_STAGE_LABELS = ("下载", "音频处理", "转写", "规范化", "结构规划", "逐节写作", "渲染")

# 字幕至少覆盖视频时长的这个比例，才允许用它替代 ASR（§6.3）
_SUBTITLE_MIN_COVERAGE = 0.5


def _nonempty(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def _cached(text: str) -> str:
    return f"{text}  (缓存命中，跳过)"


def _sections_complete(out_dir: Path, outline: Outline) -> bool:
    directory = section_dir(out_dir)
    return all(_nonempty(directory / f"{section.id}.html") for section in outline.sections)


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

    is_local: bool = False
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
    produced: list[Path] = field(default_factory=list)

    def say(self, index: int, detail: str) -> None:
        self.progress(f"[{index}/7] {_STAGE_LABELS[index - 1]} ✓ {detail}")


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
    progress: Callable[[str], None] = print,
) -> Path:
    """执行完整流水线，返回 report.html 路径。"""
    mode = (mode or "").strip().lower()
    if mode not in _MODES:
        raise UsageError(f"未知模式：{mode!r}（可选 standard / brief）")
    url = download.normalize_source(url)
    out_root = Path(out_root)
    settings = get_settings(asr_backend=asr_backend)
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
        settings=settings,
        progress=progress,
    )

    current = _STAGE_LABELS[0]
    try:
        _stage_download(state)
        current = _STAGE_LABELS[1]
        _stage_audio(state)
        current = _STAGE_LABELS[2]
        _stage_asr(state)
        current = _STAGE_LABELS[3]
        _stage_transcript(state)
        current = _STAGE_LABELS[4]
        _stage_outline(state)
        current = _STAGE_LABELS[5]
        _stage_sections(state)
        current = _STAGE_LABELS[6]
        report_path = _stage_render(state)

        # 跑完自动清理中间产物。audio.wav 是这里体积最大的一项（2h 视频约 230MB，
        # 实测占整个 run 目录的 99.9%），而它只是 ASR 的输入——ASR 结果已落盘到
        # asr.raw.jsonl，后续阶段不再读它，故删除不影响报告与各级缓存。
        # 代价：将来重跑 ASR（如换用别的转写通道）需要重新下载并转码一次音频。
        # 字幕文件不在此列：体积小，且是「字幕优先」判断覆盖率所必需。
        if state.wav is not None and not keep_audio and _nonempty(state.wav):
            freed = state.wav.stat().st_size / 1024 / 1024
            state.wav.unlink(missing_ok=True)
            progress(f"已清理中间产物 audio.wav，释放 {freed:.1f} MB")

        if open_report:
            webbrowser.open(report_path.resolve().as_uri())
        return report_path
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
    run_dir = state.out_root / download.make_run_id(local_path.resolve().as_uri(), "local")
    run_dir.mkdir(parents=True, exist_ok=True)
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


def _download_remote(state: _RunState) -> str:
    """远程视频：抓取元信息与字幕（合并为一次 yt-dlp 调用），按需下载音频。"""
    url_text = (state.url or "").strip()
    guess_dir = state.out_root / download.make_run_id(
        url_text, download.extract_bvid(url_text)
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
    trace = TraceWriter(run_dir / "run.trace.jsonl")
    wav_path = run_dir / "audio.wav"
    did_download = False
    with trace.stage("download") as stage:
        if not meta_cached:
            download.write_meta(meta, run_dir / "meta.json")
            stage.detail["subtitles"] = len(meta.subtitles)
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

        source_audio = download.find_audio(run_dir)
        if (
            state.subtitle_path is None
            and source_audio is None
            and not (state.use_cache and _nonempty(wav_path))
        ):
            source_audio = download.download_audio(meta.url, run_dir, state.settings)
            did_download = True
            stage.detail["audio"] = source_audio.name
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
        run_dir = state.out_root / download.make_run_id(meta.url, meta.bvid)
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
        if state.use_cache and _nonempty(raw_path):
            segments = load_raw_jsonl(raw_path)
            stage.detail.update({"segments": len(segments), "cached": True})
            detail = _cached(f"{len(segments)} 段")
        elif state.subtitle_path is not None:
            # 直接由平台字幕构造分段，完全不经过 ASR
            segments = segments_from_srt(state.subtitle_path)
            if not segments:
                raise AsrError(f"字幕未解析出任何内容：{state.subtitle_path.name}")
            write_raw_jsonl(segments, raw_path)
            stage.detail.update({"segments": len(segments), "source": "subtitle"})
            detail = f"{len(segments)} 段（平台字幕）"
        else:
            assert state.wav is not None
            backend = get_backend(
                state.settings, cache_dir=state.run_dir, progress=state.progress
            )
            segments = backend.transcribe(state.wav, duration=state.meta.duration)
            write_raw_jsonl(segments, raw_path)
            failures = list(getattr(backend, "failures", []) or [])
            if failures:
                stage.detail["failures"] = failures
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
        if state.use_cache and _nonempty(jsonl_path) and _nonempty(markdown_path):
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
        if state.use_cache and _nonempty(outline_path):
            outline = load_outline(outline_path)
            stage.detail.update(
                {
                    "sections": len(outline.sections),
                    "profile": outline.profile,
                    "cached": True,
                }
            )
            detail = _cached(f"{len(outline.sections)} 节  profile={outline.profile}")
        else:
            assert state.units is not None and state.meta is not None
            client = LlmClient(state.settings)
            outline = plan_outline(
                state.meta,
                state.units,
                mode=state.mode,
                client=client,
                settings=state.settings,
                progress=state.progress,
            )
            write_outline(outline, outline_path)
            stage.detail.update(
                {
                    "sections": len(outline.sections),
                    "profile": outline.profile,
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


# -------------------------------------------------------- [6/7] 逐节写作


def _stage_sections(state: _RunState) -> None:
    assert state.run_dir is not None and state.trace is not None
    assert state.outline is not None and state.units is not None
    with state.trace.stage("sections") as stage:
        complete = (
            state.use_cache
            and not state.force_report
            and _sections_complete(state.run_dir, state.outline)
        )
        if not complete and state.client is None:
            state.client = LlmClient(state.settings)
        parts = write_sections(
            state.outline,
            state.units,
            mode=state.mode,
            out_dir=state.run_dir,
            client=state.client,
            settings=state.settings,
            progress=state.progress,
            force=state.force_report,
            allowed_classes=render.template_classes(render.template_for(state.mode)),
        )
        stage.detail["sections"] = len(parts)
        detail = f"{len(parts)}/{len(state.outline.sections)} 节"
        if complete:
            stage.detail["cached"] = True
            detail = _cached(detail)
        elif state.client is not None:
            stage.detail["tokens"] = state.client.usage.total
            detail += f"  {state.client.usage.describe()}"
        if state.mode == "brief":
            length = check_brief_length(parts)
            stage.detail["brief_chars"] = length.chars
            detail += f"  Brief {length.chars} 字"
            for issue in length.issues:
                state.progress(f"  Brief 字数提示：{issue}")
            if length.over_hard:
                raise LlmError(
                    f"Brief 正文 {length.chars} 字，超过硬上限 {BRIEF_HARD_MAX} 字；"
                    "删除 runs/<run-id>/sections/ 后重跑可重新生成，或改用 --mode standard"
                )
    state.parts = parts
    state.say(6, detail)


# ------------------------------------------------------------ [7/7] 渲染


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
        if state.use_cache and not state.force_report and _nonempty(report_path):
            html = report_path.read_text(encoding="utf-8")
            stage.detail["cached"] = True
            detail = _cached("report.html  自包含检查通过")
        else:
            ctx = render.context(
                state.meta,
                state.outline,
                state.parts,
                state.units,
                state.settings,
                mode=state.mode,
                subtitle_first=state.subtitle_path is not None,
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
    state.say(7, detail)
    return report_path
