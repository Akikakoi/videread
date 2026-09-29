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

from . import execution
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

# 多字幕并存时的取用优先级：中文优先，避免双语视频误选英文字幕
# （yt-dlp 的 --sub-langs 只做过滤，不决定我们取哪一份）
_SUBTITLE_PRIORITY = ("zh-CN", "zh-Hans", "zh", "zh-Hant", "en")


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


def normalize_source(text: str) -> str:
    """把裸 BV 号补成标准视频链接；链接 / 本地路径原样返回（仅去首尾空白）。

    统一在这里归一化，保证同一个视频无论用 BV 号还是完整链接提交，
    都算出同一个 run-id，缓存不会因为两种写法而分裂。
    """
    value = (text or "").strip()
    if _BVID_RE.fullmatch(value):
        return f"https://www.bilibili.com/video/{value}"
    return value


def local_path(text: str) -> Path | None:
    """把输入解析为已存在的本地文件路径；链接或不存在则返回 None。

    流水线与提交前预检共用这一份判断，避免两处实现漂移。
    """
    value = (text or "").strip()
    if not value or value.lower().startswith(("http://", "https://")):
        return None
    try:
        path = Path(value).expanduser()
    except (OSError, ValueError):
        return None
    return path if path.is_file() else None


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
    return execution.run_command(
        cmd,
        timeout=timeout,
        error_cls=DownloadError,
        missing_message=lambda _program: "yt-dlp 未安装：请执行 `pip install yt-dlp`",
        timeout_message=lambda seconds, _program: f"下载超时（{seconds:.0f}s）",
    )


def _fail(action: str, result: subprocess.CompletedProcess[str]) -> DownloadError:
    tail = (result.stderr or result.stdout or "").strip()[-800:]
    return DownloadError(f"{action}失败：{tail}")


def fetch_meta(
    url: str,
    settings: Settings | None = None,
    *,
    enforce_limit: bool = True,
    subtitle_dir: Path | None = None,
) -> VideoMeta:
    """先拿元信息（不下载），失败即归类为下载失败。

    `enforce_limit=False` 时不校验时长上限，供提交前预检拿到真实时长后自行判定
    「超长」与「抓取失败」——流水线仍按默认 `True` 调用。
    传入 `subtitle_dir` 时顺带下载平台字幕到该目录：与元信息共用一次 yt-dlp
    调用，省一次解释器启动；无字幕时 yt-dlp 仅告警不报错。
    """
    settings = settings or get_settings()
    args = ["-J", "--skip-download"]
    timeout = 180.0
    if subtitle_dir is not None:
        subtitle_dir.mkdir(parents=True, exist_ok=True)
        args += [
            "--write-subs",
            "--sub-langs",
            _SUBTITLE_LANGS,
            "--convert-subs",
            "srt",
            "-o",
            str(subtitle_dir / "subtitle.%(ext)s"),
        ]
        timeout = 300.0
    result = _yt_dlp([*args, url], settings, timeout=timeout)
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
    if enforce_limit and duration > MAX_VIDEO_DURATION_SEC:
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
    if subtitle_dir is not None:
        meta.subtitles = collect_subtitles(subtitle_dir)
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


def collect_subtitles(dest_dir: Path) -> list[dict]:
    """收集目录中的 `subtitle.<lang>.srt` 为 meta 记录（lang + 相对文件名）。"""
    return [
        {"lang": path.name[len("subtitle.") : -len(".srt")], "path": path.name}
        for path in sorted(dest_dir.glob("subtitle.*.srt"))
    ]


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
    return collect_subtitles(dest_dir)


def pick_subtitle(subtitles: list[dict], run_dir: Path) -> Path | None:
    """按语言优先级挑出首个真实存在的字幕文件；没有可用字幕时返回 None。

    meta.json 里记录的字幕可能在后续运行中被删除，因此这里必须落到 is_file 校验。
    """
    if not subtitles:
        return None

    def rank(item: dict) -> tuple[int, str]:
        lang = str(item.get("lang", ""))
        order = (
            _SUBTITLE_PRIORITY.index(lang)
            if lang in _SUBTITLE_PRIORITY
            else len(_SUBTITLE_PRIORITY)
        )
        return order, lang

    for item in sorted(subtitles, key=rank):
        path = run_dir / str(item.get("path", ""))
        if path.is_file() and path.stat().st_size > 0:
            return path
    return None