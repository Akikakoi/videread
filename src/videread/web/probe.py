"""提交前预检：拿到视频时长，供控制台在提交前提示并拦截超长视频。

时长本来只有流水线「下载」阶段才知道，等知道时任务已经跑起来了。这里提供一条
轻量探测路径，让前端在失焦时就能给出「时长 h:mm:ss / 超过上限」的提示。

复用流水线阶段 1 的判定顺序：本地文件走 ffprobe，远程视频优先复用已生成的
`meta.json`（同视频免一次网络请求），否则才调 yt-dlp。
"""

from __future__ import annotations

from pathlib import Path

from .. import audio, download
from ..config import MAX_VIDEO_DURATION_SEC, Settings, get_settings
from ..errors import UsageError, VidereadError
from ..transcript import format_time

__all__ = ["probe_source"]


def _cached_meta(source: str, out_root: Path) -> download.VideoMeta | None:
    """若该视频此前跑过，直接读缓存 meta.json，省掉一次 yt-dlp 调用。"""
    bvid = download.extract_bvid(source)
    if not bvid:
        # 短链等拿不到 BV 号时无法在抓取前算出 run-id，直接走网络
        return None
    run_dir = out_root / download.make_run_id(source, bvid)
    meta_path = run_dir / "meta.json"
    if not meta_path.is_file():
        return None
    try:
        meta = download.read_meta(meta_path)
    except (VidereadError, OSError, ValueError, TypeError):
        # 损坏 meta 不应阻断预检，回落网络抓取
        return None
    if download.canonical_url(meta.url) != download.canonical_url(source):
        return None
    return meta


def probe_source(
    source: str,
    *,
    out_root: Path,
    settings: Settings | None = None,
) -> dict:
    """探测输入源的时长。

    返回 `{duration, duration_text, title, too_long, limit_seconds, limit_text, source_kind}`；
    输入非法或抓取失败时抛 `VidereadError`。
    """
    settings = settings or get_settings()
    url = download.normalize_source(source)

    local = download.local_path(url)
    if local is not None:
        duration = audio.probe_duration(local, settings)
        title = local.stem
        source_kind = "local"
    elif url.lower().startswith(("http://", "https://")):
        meta = _cached_meta(url, Path(out_root))
        if meta is None:
            # enforce_limit=False：拿到真实时长后由这里判定，才能区分超长与抓取失败
            meta = download.fetch_meta(url, settings, enforce_limit=False)
        duration = meta.duration
        title = meta.title
        source_kind = "remote"
    else:
        raise UsageError(f"参数既不是有效链接、也不是存在的本地文件：{url}")

    return {
        "duration": duration,
        "duration_text": format_time(duration),
        "title": title,
        "too_long": duration > MAX_VIDEO_DURATION_SEC,
        "limit_seconds": MAX_VIDEO_DURATION_SEC,
        "limit_text": f"{MAX_VIDEO_DURATION_SEC / 3600:.0f} 小时",
        "source_kind": source_kind,
    }