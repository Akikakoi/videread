"""报告库扫描：聚合 `runs/` 下的产物，供控制台列表与详情使用。

单个 run 目录解析失败（损坏 JSON、缺失文件）只跳过该条，不影响整体列表。
"""

from __future__ import annotations

import json
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
        "uploader_id": getattr(meta, "uploader_id", "") or "",
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
    """列出 out_root 下所有可识别的 run，按生成时间倒序。

    自定义标题在此统一套用：列表、检索、详情与导出文件名都从这条路径取
    标题，改一处全部生效。覆盖只落在副本上，不污染 _ENTRY_CACHE。
    """
    root = Path(out_root)
    if not root.is_dir():
        return []
    titles = load_titles(root)
    items: list[dict] = []
    for child in root.iterdir():
        if not child.is_dir() or not _RUN_ID_RE.match(child.name):
            continue
        entry = _entry(child)
        if entry is None:
            continue
        custom = titles.get(entry["run_id"], "")
        if custom and custom != entry["title"]:
            entry = dict(entry)
            entry["title_original"] = entry["title"]
            entry["title"] = custom
        items.append(entry)
    # mtime 相同时按 run_id 兜底：run_id 唯一，整份顺序完全确定，翻页窗口
    # 不会因目录枚举顺序的波动而错位。仅按 mtime 排序时，并列项的先后随
    # 文件系统与目录变动而变，同一条会同时出现在上一页末尾与下一页开头。
    items.sort(key=lambda item: (item["mtime"], item["run_id"]), reverse=True)
    return items


#: 报告库每页条数可选项；接口收到其它值时回落到默认值
RUN_PAGE_SIZES = (5, 10, 20, 50)
DEFAULT_PAGE_SIZE = 20

# ---------------------------------------------------------------- 收藏

#: 收藏表文件名，存于 out_root 下；扫描 run 时按「非目录」跳过，不会混入列表
FAVORITES_FILE = ".favorites.json"
_FAVORITES_LOCK = threading.Lock()


def _now_text() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def load_favorites(out_root: Path) -> dict[str, str]:
    """读取收藏表：`run_id → 收藏时间`（时间形如 `YYYY-MM-DD HH:MM:SS`，可直接字典序比较）。

    文件缺失或损坏时一律视为「没有任何收藏」，坏数据不拖垮整份列表。
    """
    path = Path(out_root) / FAVORITES_FILE
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        key: value
        for key, value in data.items()
        if isinstance(key, str)
        and isinstance(value, str)
        and _RUN_ID_RE.match(key)
    }


def set_favorite(out_root: Path, run_id: str, favorite: bool) -> bool:
    """收藏 / 取消收藏一个 run，返回变更后的状态；run 是否存在由调用方校验。"""
    with _FAVORITES_LOCK:
        favorites = load_favorites(out_root)
        if favorite:
            favorites.setdefault(run_id, _now_text())  # 重复收藏保留最初的先后次序
        else:
            favorites.pop(run_id, None)
        path = Path(out_root) / FAVORITES_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(favorites, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return favorite


# ---------------------------------------------------------------- 标题改名

#: 自定义标题表文件名，存于 out_root 下；与收藏表一样不会混进 run 列表
TITLES_FILE = ".titles.json"
#: 自定义标题长度上限：够写一句有信息量的概括，又不至于把卡片撑成两行以上
TITLE_MAX_CHARS = 200
_TITLES_LOCK = threading.Lock()


def load_titles(out_root: Path) -> dict[str, str]:
    """读取自定义标题表：`run_id → 标题`。

    文件缺失 / 损坏 / 条目非法一律按「没有改名」处理——改名只是展示层
    的偏好，坏数据不值得拖垮整份报告库。
    """
    path = Path(out_root) / TITLES_FILE
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        key: value.strip()
        for key, value in data.items()
        if isinstance(key, str)
        and _RUN_ID_RE.match(key)
        and isinstance(value, str)
        and value.strip()
    }


def set_title(out_root: Path, run_id: str, title: str) -> dict | None:
    """改报告库展示标题；run 不存在或 meta 损坏返回 None。

    - 标题非空白：写入覆盖表，列表 / 检索 / 详情 / 导出文件名从此用新标题
    - 标题空白：从覆盖表摘掉该条，回落到 meta.json 的原标题
      （存空串会让每处展示都得再判一层空值，摘掉更干净）

    meta.json 与 report.html 原样保留：改的是「陈列名」，不是报告本身。
    返回 {run_id, title, title_original, custom} 供前端就地更新。
    """
    run_dir = safe_run_dir(out_root, run_id)
    if run_dir is None:
        return None
    base = _entry(run_dir)
    if base is None:
        return None
    original = str(base["title"])
    cleaned = (title or "").strip()
    with _TITLES_LOCK:
        titles = load_titles(out_root)
        if cleaned:
            titles[run_id] = cleaned[:TITLE_MAX_CHARS]
        else:
            titles.pop(run_id, None)
        path = Path(out_root) / TITLES_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(titles, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return {
        "run_id": run_id,
        "title": titles.get(run_id, original),
        "title_original": original,
        "custom": run_id in titles,
    }


def query_runs(
    out_root: Path,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    keyword: str = "",
    favorites_only: bool = False,
) -> dict:
    """报告库分页查询：先按关键词过滤，再按页切片。

    关键词命中标题 / UP主 / BV 号 / run_id（忽略大小写），为空则不过滤；
    page 越界会被夹进有效范围，前端拿到的 page / pages 始终自洽。
    每条都带 `favorite` 标记；`favorites_only` 时只留收藏，并按收藏先后
    排列。全程数字分页：本机单人服务，翻页瞬间新报告入库的窗口平移概率
    可忽略，游标分页已于 2026-10-18 移除。
    """
    favorites = load_favorites(out_root)
    # 缓存条目只读：写收藏标记一律落在副本上，避免污染 _ENTRY_CACHE
    items: list[dict] = []
    for entry in list_runs(out_root):
        item = dict(entry)
        item["favorite"] = entry["run_id"] in favorites
        item["favorited_at"] = favorites.get(entry["run_id"], "")
        items.append(item)

    if favorites_only:
        items = [item for item in items if item["favorite"]]
        # favorited_at 只精确到秒，连续收藏多条必然并列；按 run_id 兜底
        # 保证翻页顺序稳定，同一项不会在相邻两页重复出现
        items.sort(key=lambda item: (item["favorited_at"], item["run_id"]))  # 先收藏的在前

    query = (keyword or "").strip().lower()
    if query:
        items = [
            item
            for item in items
            if query
            in " ".join(
                (item["title"], item["uploader"], item["bvid"], item["run_id"])
            ).lower()
        ]

    size = page_size if page_size in RUN_PAGE_SIZES else DEFAULT_PAGE_SIZE
    pages = max(1, (len(items) + size - 1) // size)

    start = (min(max(1, page), pages) - 1) * size

    current = min(max(1, start // size + 1), pages)
    window = items[start : start + size]
    return {
        "runs": [dict(item) for item in window],
        "total": len(items),
        "page": current,
        "page_size": size,
        "pages": pages,
        "query": keyword or "",
    }


def delete_run(out_root: Path, run_id: str) -> bool:
    """删除整个 run 目录（meta / 音频 / 转写缓存 / 报告全部产物）。

    报告库的「删除记录」走这里：safe_run_dir 先挡住路径穿越，
    只允许删 out_root 的直接子目录；删除失败或目录不存在返回 False。
    删除成功后顺手摘掉收藏，避免留下指向空目录的悬空标记。
    """
    run_dir = safe_run_dir(out_root, run_id)
    if run_dir is None:
        return False
    shutil.rmtree(run_dir, ignore_errors=True)
    if run_dir.exists():
        return False
    with _FAVORITES_LOCK:
        favorites = load_favorites(out_root)
        if favorites.pop(run_id, None) is not None:
            path = Path(out_root) / FAVORITES_FILE
            path.write_text(
                json.dumps(favorites, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    # 改名记录一并清掉：删除的语义是「这条记录从报告库彻底消失」，
    # 留着悬空标题会让重新生成的同名 run 意外继承旧名字
    with _TITLES_LOCK:
        titles = load_titles(out_root)
        if titles.pop(run_id, None) is not None:
            path = Path(out_root) / TITLES_FILE
            path.write_text(
                json.dumps(titles, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    return True


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

    # 与列表同源：详情里也展示自定义标题（title_original 留着给前端做提示）
    custom = load_titles(out_root).get(run_id, "")
    if custom and custom != entry["title"]:
        entry["title_original"] = entry["title"]
        entry["title"] = custom

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

# ---------------------------------------------------------- 全文检索 / 统计

#: 检索结果里每个匹配项展示的摘要半径（字符）
_SNIPPET_RADIUS = 60
#: 全文检索的返回上限
_SEARCH_LIMIT = 20


def _snippet(text: str, query: str) -> str:
    """取第一处命中（忽略大小写）前后的文本作为摘要，命中词以原文大小写保留。"""
    lowered = text.lower()
    pos = lowered.find(query.lower())
    if pos < 0:
        return text[: _SNIPPET_RADIUS * 2] + ("…" if len(text) > _SNIPPET_RADIUS * 2 else "")
    start = max(0, pos - _SNIPPET_RADIUS)
    end = min(len(text), pos + len(query) + _SNIPPET_RADIUS)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end].replace("\n", " ") + suffix


def search_runs(out_root: Path, query: str) -> list[dict]:
    """全文检索：在转写稿（transcript.md）与标题里找关键词，带摘要与命中次数。

    个人工具不做索引——runs/ 是几十份文本，逐文件扫一遍足够快；
    转写稿是报告的来源事实，比扫 report.html 更小也更"干净"。
    """
    query = (query or "").strip()
    if len(query) < 2:
        return []
    root = Path(out_root)
    results: list[dict] = []
    for entry in list_runs(root):
        run_dir = root / entry["run_id"]
        text = ""
        transcript = run_dir / "transcript.md"
        if transcript.is_file():
            try:
                text = transcript.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
        haystack = f"{entry['title']}\n{text}".lower()
        count = haystack.count(query.lower())
        if count == 0:
            continue
        results.append(
            {
                "run_id": entry["run_id"],
                "title": entry["title"],
                "uploader": entry["uploader"],
                "bvid": entry["bvid"],
                "url": entry["url"],
                "duration_text": entry["duration_text"],
                "report_url": entry["report_url"],
                "generated_at": entry["generated_at"],
                "count": count,
                "snippet": _snippet(text or entry["title"], query),
            }
        )
        if len(results) >= _SEARCH_LIMIT:
            break
    return results


def usage_stats(out_root: Path) -> list[dict]:
    """按自然日汇总所有 run 的总耗时与 token 用量（读各自的 run.trace.jsonl）。

    「成本面板」的现实版本：ASR/LLM 的钱没有统一价目，trace 里的耗时与
    token 是每次运行都真实落盘的可信读数，按日聚合够回答"最近用得凶不凶"。
    """
    per_day: dict[str, dict] = {}
    for entry in list_runs(out_root):
        trace_path = Path(out_root) / entry["run_id"] / "run.trace.jsonl"
        if not trace_path.is_file():
            continue
        day = (entry["generated_at"] or "")[:10]
        if not day:
            continue
        summary = trace_summary(trace_path)
        agg = per_day.setdefault(day, {"date": day, "runs": 0, "total_ms": 0, "tokens": 0})
        agg["runs"] += 1
        agg["total_ms"] += summary["total_ms"]
        agg["tokens"] += summary["tokens"]
    return sorted(per_day.values(), key=lambda item: item["date"], reverse=True)
