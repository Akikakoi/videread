"""报告库扫描：聚合 `runs/` 下的产物，供控制台列表与详情使用。

单个 run 目录解析失败（损坏 JSON、缺失文件）只跳过该条，不影响整体列表。
"""

from __future__ import annotations

import re
import shutil
import threading
from datetime import datetime
from pathlib import Path

from ..download import VideoMeta, read_meta
from ..errors import VidereadError
from ..frames import has_frames
from ..report import Outline
from ..report import load as load_outline
from ..transcript import format_time
from ..trace import TraceWriter

#: run 目录名的合法字符（`download.make_run_id` 产出 `{bvid}-{hash8}`）
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# 条目缓存：key 含各产物的 mtime_ns，任何文件变动都会换 key，
# 因此不需要显式失效；上限兜底防止长期驻留的服务进程缓慢累积。
_ENTRY_CACHE: dict[tuple[str, int, int, int], dict] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX_ENTRIES = 512

_STAGE_LABELS = {
    "download": "下载",
    "audio": "音频处理",
    "asr": "转写",
    "transcript": "规范化",
    "outline": "结构规划",
    "frames": "截图",
    "sections": "逐节写作",
    "render": "渲染",
}
_STAGE_ORDER = tuple(_STAGE_LABELS)


def safe_run_dir(out_root: Path, run_id: str) -> Path | None:
    """把 run_id 解析为 out_root 的直接子目录；任何穿越尝试一律返回 None。"""
    if not run_id or not _RUN_ID_RE.match(run_id):
        return None
    root = Path(out_root).resolve()
    candidate = (root / run_id).resolve()
    if candidate.parent != root or not candidate.is_dir():
        return None
    return candidate


def _read_meta(path: Path) -> VideoMeta | None:
    if not path.is_file():
        return None
    try:
        return read_meta(path)
    except (VidereadError, OSError, ValueError, TypeError):
        return None


def _read_outline(path: Path) -> Outline | None:
    if not path.is_file():
        return None
    try:
        return load_outline(path)
    except (VidereadError, OSError, ValueError, TypeError):
        return None


def trace_summary(path: Path) -> dict:
    """汇总 `run.trace.jsonl`：逐阶段耗时 / token 用量 / 错误。"""
    try:
        rows = TraceWriter(path).read()
    except (OSError, ValueError):
        rows = []

    ends: dict[str, dict] = {}
    errors: list[dict] = []
    for row in rows:
        stage = str(row.get("stage", ""))
        event = row.get("event")
        if event == "end":
            ends[stage] = row
        elif event == "error":
            detail = row.get("detail") or {}
            errors.append({"stage": stage, "message": str(detail.get("error", ""))})

    stages: list[dict] = []
    total_ms = 0
    tokens = 0
    for name in _STAGE_ORDER:
        row = ends.get(name)
        if row is None:
            continue
        detail = row.get("detail") or {}
        dur_ms = row.get("dur_ms")
        stage_tokens = detail.get("tokens")
        stages.append(
            {
                "name": name,
                "label": _STAGE_LABELS[name],
                "dur_ms": dur_ms if isinstance(dur_ms, int) else None,
                "cached": bool(detail.get("cached")),
                "tokens": stage_tokens if isinstance(stage_tokens, int) else None,
            }
        )
        if isinstance(dur_ms, int):
            total_ms += dur_ms
        if isinstance(stage_tokens, int):
            tokens += stage_tokens

    return {"stages": stages, "total_ms": total_ms, "tokens": tokens, "errors": errors}


def _mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _entry(run_dir: Path) -> dict | None:
    """聚合单个 run 的基础信息；命中缓存时免读 meta.json / outline.json。"""
    meta_path = run_dir / "meta.json"
    outline_path = run_dir / "outline.json"
    report = run_dir / "report.html"
    key = (
        str(run_dir),
        _mtime_ns(meta_path),
        _mtime_ns(outline_path),
        _mtime_ns(report),
    )
    with _CACHE_LOCK:
        cached = _ENTRY_CACHE.get(key)
    if cached is not None:
        return cached

    meta = _read_meta(meta_path)
    if meta is None:
        return None

    outline = _read_outline(outline_path)
    source = report if report.is_file() else meta_path
    try:
        mtime = source.stat().st_mtime
    except OSError:
        mtime = 0.0

    entry = {
        "run_id": run_dir.name,
        "title": meta.title,
        "uploader": meta.uploader,
        "bvid": meta.bvid,
        "url": meta.url,
        "duration": meta.duration,
        "duration_text": format_time(meta.duration),
        "profile": outline.profile if outline else "",
        "sections": len(outline.sections) if outline else 0,
        "has_report": report.is_file(),
        "has_frames": has_frames(run_dir),
        "report_url": f"/report/{run_dir.name}" if report.is_file() else None,
        "generated_at": (
            datetime.fromtimestamp(mtime).astimezone().strftime("%Y-%m-%d %H:%M")
            if mtime
            else ""
        ),
        "mtime": mtime,
    }
    with _CACHE_LOCK:
        if len(_ENTRY_CACHE) >= _CACHE_MAX_ENTRIES:
            _ENTRY_CACHE.clear()
        _ENTRY_CACHE[key] = entry
    return entry


def list_runs(out_root: Path) -> list[dict]:
    """列出 out_root 下所有可识别的 run，按生成时间倒序。"""
    root = Path(out_root)
    if not root.is_dir():
        return []
    items: list[dict] = []
    for child in root.iterdir():
        if not child.is_dir() or not _RUN_ID_RE.match(child.name):
            continue
        entry = _entry(child)
        if entry is not None:
            items.append(entry)
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return items


def delete_run(out_root: Path, run_id: str) -> bool:
    """删除整个 run 目录（meta / 音频 / 转写缓存 / 报告全部产物）。

    报告库的「删除记录」走这里：safe_run_dir 先挡住路径穿越，
    只允许删 out_root 的直接子目录；删除失败或目录不存在返回 False。
    """
    run_dir = safe_run_dir(out_root, run_id)
    if run_dir is None:
        return False
    shutil.rmtree(run_dir, ignore_errors=True)
    return not run_dir.exists()


def run_detail(out_root: Path, run_id: str) -> dict | None:
    """单个 run 的详情：基础信息 + 大纲章节 + trace 摘要 + 产物清单。"""
    run_dir = safe_run_dir(out_root, run_id)
    if run_dir is None:
        return None
    base = _entry(run_dir)
    if base is None:
        return None
    # 缓存条目只读；后续 update 一律落在副本上，避免污染缓存
    entry = dict(base)

    outline = _read_outline(run_dir / "outline.json")
    sections = (
        [
            {
                "id": section.id,
                "heading": section.heading,
                "time_range": section.time_range,
                "intent": section.intent,
                "units": len(section.source_ids),
            }
            for section in outline.sections
        ]
        if outline
        else []
    )

    files: list[dict] = []
    for path in sorted(run_dir.iterdir(), key=lambda p: p.name):
        if not path.is_file():
            continue
        try:
            files.append({"name": path.name, "bytes": path.stat().st_size})
        except OSError:
            continue

    entry.update(
        {
            "title_full": outline.title if outline else entry["title"],
            "subtitle": outline.subtitle if outline else "",
            "lead": outline.lead if outline else "",
            "sections_detail": sections,
            "trace": trace_summary(run_dir / "run.trace.jsonl"),
            "files": files,
        }
    )
    return entry