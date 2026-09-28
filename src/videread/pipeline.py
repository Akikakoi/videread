"""流水线编排：阶段调度 + 断点续跑 + 异常归类（对应开发文档 §6.2 / §11.3）。

7 个阶段（§3.1）：下载 → 音频处理 → 转写 → 规范化 → 结构规划 → 逐节写作 → 渲染。
每阶段执行前先检查产物；命中则跳过（`use_cache=True`），阶段边界写入 `run.trace.jsonl`。
"""

from __future__ import annotations

import webbrowser
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Callable

from . import audio as audio_mod
from . import download, render
from .asr import get_backend, load_raw_jsonl, write_raw_jsonl
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


def _say(progress: Callable[[str], None], index: int, detail: str) -> None:
    progress(f"[{index}/7] {_STAGE_LABELS[index - 1]} ✓ {detail}")


def _sections_complete(out_dir: Path, outline: Outline) -> bool:
    directory = section_dir(out_dir)
    return all(_nonempty(directory / f"{section.id}.html") for section in outline.sections)


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

    current = _STAGE_LABELS[0]
    produced: list[Path] = []
    trace: TraceWriter | None = None

    try:
        # ------------------------------------------------------------ [1/7] 下载
        local_path = download.local_path(url)
        if local_path is None and not (url or "").strip().lower().startswith(
            ("http://", "https://")
        ):
            raise UsageError(f"参数既不是有效链接、也不是存在的本地文件：{url}")
        is_local = local_path is not None

        # 字幕优先只对远程视频生效：本地文件没有平台字幕（§6.3）
        subtitle_path: Path | None = None

        if is_local:
            assert local_path is not None
            run_dir = out_root / download.make_run_id(local_path.resolve().as_uri(), "local")
            run_dir.mkdir(parents=True, exist_ok=True)
            trace = TraceWriter(run_dir / "run.trace.jsonl")
            meta_path = run_dir / "meta.json"
            with trace.stage("download") as stage:
                cached = use_cache and _nonempty(meta_path)
                if cached:
                    meta = download.read_meta(meta_path)
                    stage.detail["cached"] = True
                    detail = _cached(f"meta.json  {meta.title}  时长 {format_time(meta.duration)}")
                else:
                    duration = audio_mod.probe_duration(local_path, settings)
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
            source_audio: Path | None = local_path
            produced.append(meta_path)
        else:
            url_text = (url or "").strip()
            guess_dir = out_root / download.make_run_id(url_text, download.extract_bvid(url_text))
            meta_cached = use_cache and _nonempty(guess_dir / "meta.json")
            if meta_cached:
                meta = download.read_meta(guess_dir / "meta.json")
                if download.canonical_url(meta.url) != download.canonical_url(url_text):
                    progress(
                        f"提示：{guess_dir.name} 的 meta.json 属于 {meta.url}，"
                        "与本次请求不一致，已重新抓取"
                    )
                    meta_cached = False
            if meta_cached:
                run_dir = guess_dir
            else:
                meta = download.fetch_meta(url_text, settings)
                run_dir = out_root / download.make_run_id(meta.url, meta.bvid)
            run_dir.mkdir(parents=True, exist_ok=True)
            trace = TraceWriter(run_dir / "run.trace.jsonl")
            wav_path = run_dir / "audio.wav"
            did_download = False
            with trace.stage("download") as stage:
                if not meta_cached:
                    download.write_meta(meta, run_dir / "meta.json")
                    subtitles = download.download_subtitles(meta.url, run_dir, settings)
                    if subtitles:
                        meta.subtitles = subtitles
                        download.write_meta(meta, run_dir / "meta.json")
                    stage.detail["subtitles"] = len(meta.subtitles)
                else:
                    stage.detail["cached"] = True

                # 字幕优先：字幕够完整就不再下载音频、不再调用 ASR
                if settings.subtitle_first and not force_asr:
                    candidate = download.pick_subtitle(meta.subtitles, run_dir)
                    if candidate is None:
                        if meta.subtitles:
                            progress(
                                f"提示：meta.json 记录的字幕（{len(meta.subtitles)} 份）"
                                "已不在磁盘上，回退到下载音频 + ASR"
                            )
                    else:
                        coverage = subtitle_coverage(candidate, meta.duration)
                        stage.detail["subtitle_coverage"] = round(coverage, 3)
                        if coverage >= _SUBTITLE_MIN_COVERAGE:
                            subtitle_path = candidate
                        else:
                            progress(
                                f"提示：{candidate.name} 只覆盖约 {coverage:.0%} 的视频时长，"
                                "判为不完整，回退到下载音频 + ASR"
                            )

                source_audio = download.find_audio(run_dir)
                if (
                    subtitle_path is None
                    and source_audio is None
                    and not (use_cache and _nonempty(wav_path))
                ):
                    source_audio = download.download_audio(meta.url, run_dir, settings)
                    did_download = True
                    stage.detail["audio"] = source_audio.name
                if subtitle_path is not None:
                    stage.detail["subtitle_first"] = subtitle_path.name
            detail = f"meta.json  {meta.bvid}  时长 {format_time(meta.duration)}"
            if subtitle_path is not None:
                detail += f"  字幕优先 {subtitle_path.name}（跳过音频与 ASR）"
            elif meta_cached and not did_download:
                detail = _cached(detail)
            produced.append(run_dir / "meta.json")
        _say(progress, 1, detail)

        assert trace is not None

        # -------------------------------------------------------- [2/7] 音频处理
        current = _STAGE_LABELS[1]
        wav = run_dir / "audio.wav"
        with trace.stage("audio") as stage:
            if subtitle_path is not None:
                stage.detail["skipped"] = "字幕优先，无需音频"
                detail = "字幕优先，跳过音频处理"
            elif use_cache and _nonempty(wav):
                stage.detail["cached"] = True
                detail = _cached("16kHz 单声道 wav")
            else:
                if source_audio is None or not _nonempty(source_audio):
                    source_audio = download.find_audio(run_dir) or wav
                if not _nonempty(source_audio):
                    raise AudioError(f"缺少可转码的音频源：{run_dir}")
                audio_mod.to_wav_16k_mono(source_audio, wav, settings)
                stage.detail["source"] = source_audio.name
                detail = "16kHz 单声道 wav"
                if not is_local and not keep_audio and source_audio.parent == run_dir:
                    source_audio.unlink(missing_ok=True)
                    stage.detail["removed_source"] = source_audio.name
                produced.append(wav)
        _say(progress, 2, detail)

        # ------------------------------------------------------------ [3/7] 转写
        current = _STAGE_LABELS[2]
        raw_path = run_dir / "asr.raw.jsonl"
        with trace.stage("asr") as stage:
            if use_cache and _nonempty(raw_path):
                segments = load_raw_jsonl(raw_path)
                stage.detail.update({"segments": len(segments), "cached": True})
                detail = _cached(f"{len(segments)} 段")
            elif subtitle_path is not None:
                # 直接由平台字幕构造分段，完全不经过 ASR
                segments = segments_from_srt(subtitle_path)
                if not segments:
                    raise AsrError(f"字幕未解析出任何内容：{subtitle_path.name}")
                write_raw_jsonl(segments, raw_path)
                stage.detail.update({"segments": len(segments), "source": "subtitle"})
                detail = f"{len(segments)} 段（平台字幕）"
            else:
                backend = get_backend(settings, cache_dir=run_dir, progress=progress)
                segments = backend.transcribe(wav, duration=meta.duration)
                write_raw_jsonl(segments, raw_path)
                failures = list(getattr(backend, "failures", []) or [])
                if failures:
                    stage.detail["failures"] = failures
                stage.detail["segments"] = len(segments)
                detail = f"{len(segments)} 段"
        produced.append(raw_path)
        _say(progress, 3, detail)

        # ---------------------------------------------------------- [4/7] 规范化
        current = _STAGE_LABELS[3]
        jsonl_path = run_dir / "transcript.jsonl"
        markdown_path = run_dir / "transcript.md"
        with trace.stage("transcript") as stage:
            if use_cache and _nonempty(jsonl_path) and _nonempty(markdown_path):
                units = load_units(jsonl_path)
                stage.detail.update({"units": len(units), "cached": True})
                detail = _cached(f"{len(units)} 单元  u0001-u{len(units):04d}")
            else:
                units = build_units(segments)
                # 与字幕优先共用同一套语言优先级，避免出现「用中文字幕建稿、
                # 又被英文字幕覆盖」这类前后不一致
                srt = download.pick_subtitle(meta.subtitles, run_dir)
                if srt is not None:
                    units = merge_subtitles(units, srt)
                    stage.detail["subtitle"] = srt.name
                if not units:
                    raise AsrError("转写规范化后没有任何可用单元")
                write_jsonl(units, jsonl_path)
                write_markdown(units, markdown_path)
                stage.detail["units"] = len(units)
                detail = f"{len(units)} 单元  u0001-u{len(units):04d}"
        produced.extend([jsonl_path, markdown_path])
        _say(progress, 4, detail)

        # -------------------------------------------------------- [5/7] 结构规划
        current = _STAGE_LABELS[4]
        outline_path = run_dir / "outline.json"
        client: LlmClient | None = None
        with trace.stage("outline") as stage:
            if use_cache and _nonempty(outline_path):
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
                client = LlmClient(settings)
                outline = plan_outline(
                    meta, units, mode=mode, client=client, settings=settings, progress=progress
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
        produced.append(outline_path)
        _say(progress, 5, detail)

        # -------------------------------------------------------- [6/7] 逐节写作
        current = _STAGE_LABELS[5]
        with trace.stage("sections") as stage:
            complete = use_cache and not force_report and _sections_complete(run_dir, outline)
            if not complete and client is None:
                client = LlmClient(settings)
            parts = write_sections(
                outline,
                units,
                mode=mode,
                out_dir=run_dir,
                client=client,
                settings=settings,
                progress=progress,
                force=force_report,
                allowed_classes=render.template_classes(render.template_for(mode)),
            )
            stage.detail["sections"] = len(parts)
            detail = f"{len(parts)}/{len(outline.sections)} 节"
            if complete:
                stage.detail["cached"] = True
                detail = _cached(detail)
            elif client is not None:
                stage.detail["tokens"] = client.usage.total
                detail += f"  {client.usage.describe()}"
            if mode == "brief":
                length = check_brief_length(parts)
                stage.detail["brief_chars"] = length.chars
                detail += f"  Brief {length.chars} 字"
                for issue in length.issues:
                    progress(f"  Brief 字数提示：{issue}")
                if length.over_hard:
                    raise LlmError(
                        f"Brief 正文 {length.chars} 字，超过硬上限 {BRIEF_HARD_MAX} 字；"
                        "删除 runs/<run-id>/sections/ 后重跑可重新生成，或改用 --mode standard"
                    )
        _say(progress, 6, detail)

        # ------------------------------------------------------------ [7/7] 渲染
        current = _STAGE_LABELS[6]
        report_path = run_dir / "report.html"
        with trace.stage("render") as stage:
            if use_cache and not force_report and _nonempty(report_path):
                html = report_path.read_text(encoding="utf-8")
                stage.detail["cached"] = True
                detail = _cached("report.html  自包含检查通过")
            else:
                ctx = _context(
                    meta,
                    outline,
                    parts,
                    units,
                    settings,
                    mode=mode,
                    subtitle_first=subtitle_path is not None,
                )
                render.render_report(template=render.template_for(mode), ctx=ctx, out=report_path)
                html = report_path.read_text(encoding="utf-8")
                detail = "report.html  自包含检查通过"
            violations = render.check_self_contained(html)
            stage.detail["bytes"] = len(html.encode("utf-8"))
            stage.detail["self_contained"] = not violations
            if violations:
                raise RenderError("自包含检查未通过：" + "；".join(violations))
        produced.append(report_path)
        _say(progress, 7, detail)

        # 跑完自动清理中间产物。audio.wav 是这里体积最大的一项（2h 视频约 230MB，
        # 实测占整个 run 目录的 99.9%），而它只是 ASR 的输入——ASR 结果已落盘到
        # asr.raw.jsonl，后续阶段不再读它，故删除不影响报告与各级缓存。
        # 代价：将来重跑 ASR（如换用别的转写通道）需要重新下载并转码一次音频。
        # 字幕文件不在此列：体积小，且是「字幕优先」判断覆盖率所必需。
        if not keep_audio and _nonempty(wav):
            freed = wav.stat().st_size / 1024 / 1024
            wav.unlink(missing_ok=True)
            progress(f"已清理中间产物 audio.wav，释放 {freed:.1f} MB")

        if open_report:
            webbrowser.open(report_path.resolve().as_uri())
        return report_path
    except VidereadError as exc:
        progress(f"[错误] 阶段「{current}」失败：{type(exc).__name__}")
        if produced:
            progress("已生成的产物（可据此续跑）：")
            for path in produced:
                progress(f"  - {path}")
        raise


def _attribution(meta: VideoMeta) -> str:
    platform = "本地文件" if meta.bvid == "local" else "Bilibili"
    bits = [platform]
    if meta.uploader:
        bits.append(f"UP主：{escape(meta.uploader)}")
    if meta.title:
        bits.append(f"《{escape(meta.title)}》")
    url_text = escape(meta.url)
    return f"{' · '.join(bits)}<br><a href=\"{url_text}\">{url_text}</a>"


def _sources(
    meta: VideoMeta,
    outline: Outline,
    units: list[TranscriptUnit],
    settings: Settings,
    *,
    mode: str,
    subtitle_first: bool,
) -> str:
    counts: dict[str, int] = {}
    for unit in units:
        counts[unit.source] = counts.get(unit.source, 0) + 1
    breakdown = " / ".join(f"{key} {value}" for key, value in sorted(counts.items()))
    asr_label = settings.asr_backend
    if asr_label == "dashscope":
        asr_label = f"dashscope / {settings.dashscope_model}"
    if subtitle_first and units and all(unit.source == "subtitle" for unit in units):
        # 本次完全由平台字幕建稿 = 没调用 ASR，报告里必须如实说明
        asr_label = "平台字幕（字幕优先，未调用 ASR）"
    generated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    url_text = escape(meta.url)
    rows = [
        f"原视频：<a href=\"{url_text}\">{url_text}</a>",
        f"阅读模式：{'Brief' if mode == 'brief' else 'Standard'}；信息结构：{outline.profile}",
        f"转写：{asr_label}；转写单元 {len(units)} 个（{breakdown or '无'}）",
        f"LLM：结构规划 {settings.llm_model_plan}，逐节写作 {settings.llm_model_write}",
        f"生成时间：{generated_at}",
        "正文中 data-source-units 属性指向 transcript.jsonl 的转写单元 id，可据此回溯原文。",
        "本报告由转写稿重组生成，可能存在转写或理解误差；关键信息请以原视频为准。",
    ]
    return "\n".join(f"<p>{row}</p>" for row in rows)


def _context(
    meta: VideoMeta,
    outline: Outline,
    parts: list[str],
    units: list[TranscriptUnit],
    settings: Settings,
    *,
    mode: str,
    subtitle_first: bool,
) -> dict[str, str]:
    """组装模板上下文；LLM 输出按 §6.10 不转义，平台元信息转义后再注入。"""
    return {
        "TITLE": outline.title,
        "SUBTITLE": outline.subtitle,
        "LEAD": outline.lead,
        "ATTRIBUTION": _attribution(meta),
        "VIDEO_DESCRIPTION": escape(meta.description) if meta.description else "",
        "BODY": "\n".join(parts),
        "SOURCES": _sources(
            meta,
            outline,
            units,
            settings,
            mode=mode,
            subtitle_first=subtitle_first,
        ),
    }