"""截图（纯展示模式）：按大纲节定点抽帧，以 base64 内嵌进报告。

数据流：结构规划产出各节 time_range → 本模块选点（节时间范围中点）→
远程视频定点拉取小段（`download.download_video_section`）或本地文件直取
→ ffmpeg 抽帧缩放到统一宽度 → `frames/sN.jpg` 落盘（断点续跑的缓存判据）
→ 渲染前把 `<figure>` 注入各节标题之后。

截图是增强能力：任何一节失败只降级跳过，不阻断报告生成。
"""

from __future__ import annotations

import base64
from pathlib import Path

from . import execution
from .config import (
    FRAME_CLIP_SECONDS,
    FRAME_JPEG_QUALITY,
    FRAME_LEAD_IN,
    FRAME_MAX_TOTAL,
    FRAME_WIDTH,
    Settings,
)
from .errors import FramesError
from .report import Outline
from .transcript import TranscriptUnit, format_time


def frame_dir(out_dir: Path) -> Path:
    return out_dir / "frames"


def section_moments(outline: Outline, units: list[TranscriptUnit]) -> dict[str, float]:
    """每节选一个截图点：该节来源单元时间范围的中点（绝对秒）。

    返回 `{section_id: 中点秒}`，按大纲顺序。
    """
    by_id = {unit.id: unit for unit in units}
    moments: dict[str, float] = {}
    for section in outline.sections:
        starts = [
            by_id[unit_id].start
            for unit_id in section.source_ids
            if unit_id in by_id
        ]
        ends = [by_id[unit_id].end for unit_id in section.source_ids if unit_id in by_id]
        if not starts or not ends:
            continue
        moments[section.id] = (min(starts) + max(ends)) / 2
    return moments


def plan_points(
    outline: Outline, units: list[TranscriptUnit]
) -> list[tuple[str, float, str]]:
    """截图点列表 `[(section_id, 绝对秒, 展示时间)]`，按大纲顺序、
    超过 `FRAME_MAX_TOTAL` 时截断。"""
    points = [
        (section_id, moment, format_time(moment))
        for section_id, moment in section_moments(outline, units).items()
    ]
    return points[:FRAME_MAX_TOTAL]


def clip_bounds(t: float) -> tuple[float, float]:
    """由绝对时间 t 推导定点片段：`[start, start + FRAME_CLIP_SECONDS]` 与
    片段内抽帧的相对位置。向前多取 `FRAME_LEAD_IN` 秒，抽帧位置恰好落在 t 上
    （配合 `--force-keyframes-at-cuts` 的精确切点）。"""
    start = max(0.0, t - FRAME_LEAD_IN)
    return start, t - start


def clip_path(dest_dir: Path, index: int) -> Path | None:
    """定位第 index 个定点片段（yt-dlp 按模板命名，扩展名取决于所选格式）。"""
    for path in sorted(dest_dir.glob(f"clip.{index:03d}.*")):
        if path.suffix.lower() not in {".jpg", ".json", ".jsonl"}:
            return path
    return None


def extract_frame(
    src: Path, dest: Path, *, at: float, settings: Settings | None = None
) -> None:
    """从视频文件抽出第 `at` 秒的一帧，缩放到统一宽度后存为 JPEG。"""
    from .audio import ffmpeg_bin

    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_bin(settings),
        "-hide_banner",
        "-nostdin",
        "-y",
        "-ss",
        f"{max(0.0, at):.3f}",
        "-i",
        str(src),
        "-frames:v",
        "1",
        "-vf",
        f"scale={FRAME_WIDTH}:-2",
        "-q:v",
        str(FRAME_JPEG_QUALITY),
        str(dest),
    ]
    result = execution.run_command(
        cmd,
        timeout=300,
        error_cls=FramesError,
        missing_message=lambda program: f"无法执行外部程序：{program}",
        timeout_message=lambda seconds, program: f"抽帧超时（{seconds:.0f}s）：{program}",
    )
    if result.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        raise FramesError(
            f"抽帧失败：{src.name} @ {at:.1f}s\n{result.stderr.strip()[-800:]}"
        )


def to_data_uri(path: Path) -> str:
    """JPEG 文件 → `data:image/jpeg;base64,...`（报告自包含要求的内嵌形式）。"""
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def inject_figures(
    parts: list[str],
    outline: Outline,
    frame_files: dict[str, Path],
    moments: dict[str, float] | None = None,
) -> list[str]:
    """把各节截图 `<figure>` 插到对应节标题（第一个 `</h2>`）之后。

    没有截图的节原样返回；`parts` 与 `outline.sections` 按位置一一对应。
    `moments` 为各节截图点（绝对秒），图注展示该精确时刻；缺省时回退
    到节的时间区间。
    """
    from html import escape

    injected: list[str] = []
    for section, part in zip(outline.sections, parts):
        path = frame_files.get(section.id)
        if path is None:
            injected.append(part)
            continue
        if moments and section.id in moments:
            stamp = format_time(moments[section.id])
        else:
            stamp = section.time_range
        figure = (
            '<figure class="report-figure">'
            f'<img src="{to_data_uri(path)}" alt="视频截图：{escape(section.heading)}" />'
            f"<figcaption>视频画面 · {escape(stamp)}</figcaption>"
            "</figure>"
        )
        injected.append(part.replace("</h2>", "</h2>\n" + figure, 1))
    return injected


def write_manifest(
    out_dir: Path,
    outline: Outline,
    frame_files: dict[str, Path],
    moments: dict[str, float] | None = None,
) -> Path:
    """写 `frames.jsonl`：每行一节的截图记录（含精确时刻），供排查与后续 VLM 复用。"""
    path = out_dir / "frames.jsonl"
    lines = []
    for section in outline.sections:
        file = frame_files.get(section.id)
        if file is None:
            continue
        if moments and section.id in moments:
            stamp = format_time(moments[section.id])
        else:
            stamp = section.time_range
        lines.append(
            f'{{"section": "{section.id}", "file": "frames/{file.name}", '
            f'"time": "{stamp}"}}'
        )
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8", newline="\n")
    return path


def has_frames(out_dir: Path) -> bool:
    """该 run 是否产出了截图（frames/ 下至少一张 JPEG）。

    导出策略的判据：带截图的报告不提供 Markdown 导出（相对路径引用
    离开 run 目录就裂图，base64 内嵌又会让 .md 膨胀到几百 KB，干脆
    不给这个出口）。截图阶段降级失败的 run 没有 frames/，照常可导出。
    """
    frames = frame_dir(out_dir)
    return frames.is_dir() and any(frames.glob("*.jpg"))
