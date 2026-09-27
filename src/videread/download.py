"""视频下载与元信息（对应开发文档 §6.3 / §5.1）。

一律通过 `python -m yt_dlp` 调用，避免依赖 PATH 上的可执行文件。
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import MAX_VIDEO_DURATION_SEC, Settings, get_settings
from .errors import DownloadError

_BVID_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
# 只影响分享来源、不影响内容的追踪参数
_TRACKING_PARAMS = {
    "vd_source",
    "spm_id_from",
    "from",
    "se_from",
    "t",
    "share_source",
    "share_medium",
    "bbid",
    "ts",
    "unique_k",
    "broadcast_type",
    "is_room_feed",
}

_SUBTITLE_LANGS = "zh-CN,zh-Hans,zh,zh-Hant,en"


@dataclass
class VideoMeta:
    """§5.1 meta.json。"""

    bvid: str
    title: str
    uploader: str
    duration: float
    url: str
    cover: str = ""
    description: str = ""
    subtitles: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "VideoMeta":
        return cls(
            bvid=str(data.get("bvid", "")),
            title=str(data.get("title", "")),
            uploader=str(data.get("uploader", "")),
            duration=float(data.get("duration", 0) or 0),
            url=str(data.get("url", "")),
            cover=str(data.get("cover", "") or ""),
            description=str(data.get("description", "") or ""),
            subtitles=list(data.get("subtitles") or []),
        )


def canonical_url(url: str) -> str:
    """去掉分享追踪参数，保证同一视频稳定映射到同一 run-id。"""
    parts = urlsplit(url.strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key not in _TRACKING_PARAMS
    ]
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path,
            urlencode(query),
            "",
        )
    )


def extract_bvid(url: str) -> str:
    """从链接中提取 BV 号；提取不到（如短链）时返回空串。

    用于在尚未拿到元信息时先算出 run-id，从而命中已有产物目录。
    """
    match = _BVID_RE.search(url or "")
    return match.group(1) if match else ""


def make_run_id(url: str, bvid: str) -> str:
    """`{bvid}-{hash8}`：同一视频多次运行落到同一目录（§4）。"""
    digest = hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()[:8]
    safe_id = bvid or "local"
    return f"{safe_id}-{digest}"


def write_meta(meta: VideoMeta, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(meta.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def read_meta(path: Path) -> VideoMeta:
    try:
        return VideoMeta.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise DownloadError(f"{path} 不是合法 meta.json：{exc}") from exc


def local_meta(path: Path, duration: float) -> VideoMeta:
    """本地音视频文件没有平台元信息，构造一份等价 meta。"""
    resolved = path.resolve()
    return VideoMeta(
        bvid="local",
        title=resolved.stem,
        uploader="",
        duration=duration,
        url=resolved.as_uri(),
        cover="",
        description="",
        subtitles=[],
    )


def _yt_dlp(args: list[str], settings: Settings, *, timeout: float = 900) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-playlist"]
    if settings.proxy:
        cmd += ["--proxy", settings.proxy]
    cmd += args
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise DownloadError("yt-dlp 未安装：请执行 `pip install yt-dlp`") from exc
    except subprocess.TimeoutExpired as exc:
        raise DownloadError(f"下载超时（{timeout:.0f}s）") from exc


def _fail(action: str, result: subprocess.CompletedProcess[str]) -> DownloadError:
    tail = (result.stderr or result.stdout or "").strip()[-800:]
    return DownloadError(f"{action}失败：{tail}")


def fetch_meta(url: str, settings: Settings | None = None) -> VideoMeta:
    """先拿元信息（不下载），失败即归类为下载失败。"""
    settings = settings or get_settings()
    result = _yt_dlp(["-J", "--skip-download", url], settings, timeout=180)
    if result.returncode != 0:
        raise _fail("获取视频元信息", result)
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DownloadError(f"yt-dlp 输出不是合法 JSON：{exc}") from exc
    if not isinstance(info, dict):
        raise DownloadError("yt-dlp 未返回单个视频信息（可能是合集链接）")

    duration = float(info.get("duration") or 0)
    if duration <= 0:
        raise DownloadError("视频时长不可用（可能是直播回放或付费视频）")
    if duration > MAX_VIDEO_DURATION_SEC:
        raise DownloadError(
            f"视频时长 {duration / 3600:.1f} 小时，超过 "
            f"{MAX_VIDEO_DURATION_SEC / 3600:.0f} 小时上限，为避免 ASR 成本失控已拒绝"
        )

    bvid_match = _BVID_RE.search(str(info.get("id", ""))) or _BVID_RE.search(url)
    meta = VideoMeta(
        bvid=bvid_match.group(1) if bvid_match else str(info.get("id", "")),
        title=str(info.get("title") or "").strip(),
        uploader=str(info.get("uploader") or info.get("channel") or "").strip(),
        duration=duration,
        url=str(info.get("webpage_url") or url),
        cover=str(info.get("thumbnail") or ""),
        description=str(info.get("description") or ""),
        subtitles=[],
    )
    if not meta.title:
        raise DownloadError("视频标题为空，链接可能无效")
    return meta


def download_audio(url: str, dest_dir: Path, settings: Settings | None = None) -> Path:
    """下载音频到 `dest_dir/audio.<ext>`，优先 m4a。"""
    settings = settings or get_settings()
    dest_dir.mkdir(parents=True, exist_ok=True)
    existing = find_audio(dest_dir)
    if existing is not None:
        return existing

    result = _yt_dlp(
        [
            "-f",
            "bestaudio[ext=m4a]/bestaudio/best",
            "-o",
            str(dest_dir / "audio.%(ext)s"),
            url,
        ],
        settings,
    )
    if result.returncode != 0:
        raise _fail("下载音频", result)

    produced = find_audio(dest_dir)
    if produced is None:
        raise DownloadError(f"下载完成但未找到音频文件：{dest_dir}")
    return produced


def find_audio(dest_dir: Path) -> Path | None:
    """在 run 目录中定位音频源文件（m4a 优先）。"""
    candidates = [
        path
        for path in sorted(dest_dir.glob("audio.*"))
        if path.suffix.lower() not in {".wav", ".jsonl", ".json", ".md"}
    ]
    if not candidates:
        return None
    for path in candidates:
        if path.suffix.lower() == ".m4a":
            return path
    return candidates[0]


def download_subtitles(
    url: str, dest_dir: Path, settings: Settings | None = None
) -> list[dict]:
    """下载平台自带字幕到 `subtitle.<lang>.srt`；没有字幕时返回空列表。"""
    settings = settings or get_settings()
    dest_dir.mkdir(parents=True, exist_ok=True)
    result = _yt_dlp(
        [
            "--skip-download",
            "--write-subs",
            "--sub-langs",
            _SUBTITLE_LANGS,
            "--convert-subs",
            "srt",
            "-o",
            str(dest_dir / "subtitle.%(ext)s"),
            url,
        ],
        settings,
        timeout=300,
    )
    if result.returncode != 0:
        return []

    subtitles: list[dict] = []
    for path in sorted(dest_dir.glob("subtitle.*.srt")):
        lang = path.name[len("subtitle.") : -len(".srt")]
        subtitles.append({"lang": lang, "path": path.name})
    return subtitles