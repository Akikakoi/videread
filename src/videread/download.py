"""视频下载与元信息（对应开发文档 §6.3 / §5.1）。

一律通过 `python -m yt_dlp` 调用，避免依赖 PATH 上的可执行文件。
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from . import execution
from .config import MAX_VIDEO_DURATION_SEC, Settings, get_settings
from .errors import DownloadError
from .transcript import format_time

_BVID_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
# 只影响分享来源、不影响内容的追踪参数
_TRACKING_PARAMS = {
    "vd_source",
    "spm_id_from",
    "csource",
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

# B 站 AI 生成字幕的语言代码是 ai-zh / ai-en（UP 主手传 CC 为 zh-CN / zh-Hans）；
# 不在请求列表里 yt-dlp 就不会下载，这正是 B 站视频"明明有字幕却走了 ASR"的主因
_SUBTITLE_LANGS = "zh-CN,zh-Hans,zh,zh-Hant,ai-zh,ai-en,en"

# B 站分P探测用的公开 view 接口：无需登录，一次请求带回全部分P 的标题与时长
_VIEW_API = "https://api.bilibili.com/x/web-interface/view"
_VIEW_TIMEOUT_SEC = 8.0

# player wbi/v2 接口：字幕权威来源。必须带 wbi 签名——未签名（yt-dlp 现状）
# 恒返回空；老版非签名 /x/player/v2 虽有返回但内容会与视频错乱（实测会把
# 别的视频的 AI 字幕张冠李戴），两者都不可用
_PLAYER_API = "https://api.bilibili.com/x/player/wbi/v2"
_NAV_API = "https://api.bilibili.com/x/web-interface/nav"
_PLAYER_TIMEOUT_SEC = 10.0

# wbi 签名的固定混淆表（B 站前端 JS 里的 WBI_MIXIN_KEY_ENC_TAB，公开知识）
_WBI_MIXIN_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62,
    11, 36, 20, 34, 44, 52,
)

# 充电专属/付费内容拦截（§6.3）：两类信号都对所有人公开——
#   充电专属：view data 顶层 `is_upower_exclusive`（rights 里没有对应位；
#     注意 view/yt-dlp 报告的时长是试看预览的时长，拿不到完整时长基准）；
#   付费稿件：rights.ugc_pay；
#   付费课程（Cheese）：pugv season API 的每集 playable（见 _cheese_season）。
# 2026-10-19 决策：这两类内容一律不支持解析，预检即拒绝（无登录态例外），
# 流水线还有 ffprobe 实测时长的兜底防线（_guard_preview_audio）。
_PAYWALL_RIGHTS = ("ugc_pay",)
PREVIEW_DURATION_RATIO = 0.8


def paywall_reject_text(kind: str, title: str = "") -> str:
    """付费内容拒绝的用户提示：全程大白话。"""
    name = f"《{title}》" if title else "这个链接指向的内容"
    if kind == "cheese":
        return f"{name}是 B 站上的付费课程（需要购买后才能观看），暂不支持解析这类视频。"
    return f"{name}是「充电专属」视频（需要给 UP 主充电后才能观看），暂不支持解析这类视频。"


def _preview_reject_text(actual: float, full: float) -> str:
    """时长比对命中的试看预览提示。"""
    return (
        f"只下载到了 {format_time(actual)} 的试看内容，远短于这个视频完整的 "
        f"{format_time(full)}——它应该是需要付费观看的内容（充电专属或付费课程），"
        "当前账号没有观看权限，已停止解析，没有进行转写，不会产生费用。\n"
        "如果你有观看权限，请按 README 的「B 站登录」一节配置账号信息后重试。"
    )

# B 站付费课程（Cheese）：URL 无 BV 号，元数据走 pugv 接口（对所有人公开，
# 含每集的 playable 标志 = 当前观看者能否看全集：免费试看集为 true，付费集
# 未购买为 false）。yt-dlp 原生支持（BilibiliCheese），未购买时报
# "You need to purchase the course"。
_CHEESE_EP_RE = re.compile(r"bilibili\.com/cheese/play/ep(\d+)")
_CHEESE_SEASON_API = "https://api.bilibili.com/pugv/view/web/season"

# 多字幕并存时的取用优先级：中文优先，避免双语视频误选英文字幕；
# 人手 CC 排在 AI 字幕之前（质量通常更好），ai-zh 排在 en 之前
# （yt-dlp 的 --sub-langs 只做过滤，不决定我们取哪一份）
_SUBTITLE_PRIORITY = ("zh-CN", "zh-Hans", "zh", "zh-Hant", "ai-zh", "en", "ai-en")


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
    uploader_id: str = ""
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
            uploader_id=str(data.get("uploader_id", "") or ""),
            subtitles=list(data.get("subtitles") or []),
        )


@dataclass(frozen=True)
class VideoPage:
    """B 站多 P 视频中的一个分P（view API 的 `pages` 项）。"""

    page: int
    title: str
    duration: float


@dataclass(frozen=True)
class VideoInfo:
    """B 站 view API 返回的视频概要：主标题、UP主与分P列表。"""

    title: str
    uploader: str
    pages: list[VideoPage]
    charging: bool = False


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
    """`{bvid}-{hash8}`：同一视频多次运行落到同一目录（§4）。

    无 BV 号的付费课程链接用 `ep{集号}` 做前缀；本地文件回落 `local`。
    """
    digest = hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()[:8]
    ep_id = _cheese_ep_id(url)
    safe_id = bvid or (f"ep{ep_id}" if ep_id else "local")
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
    use_cookies = bool(settings.cookies_file or settings.cookies_from_browser)
    result = _run_yt_dlp(args, settings, timeout=timeout, use_cookies=use_cookies)
    if result.returncode != 0 and use_cookies and "cookie" in (result.stderr or "").lower():
        # Cookie 读取失败（浏览器未关闭锁库、加密不兼容、文件失效等）不应
        # 拖垮整个流水线：去掉 Cookie 重试，代价只是拿不到登录态内容（如 B 站 AI 字幕）
        result = _run_yt_dlp(args, settings, timeout=timeout, use_cookies=False)
    return result


def _run_yt_dlp(
    args: list[str],
    settings: Settings,
    *,
    timeout: float,
    use_cookies: bool,
) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-playlist"]
    if settings.proxy:
        cmd += ["--proxy", settings.proxy]
    if use_cookies:
        # B 站 AI 字幕等登录态内容：优先用 cookies.txt 文件（浏览器直读会被
        # 新版 Chrome/Edge 的 App-Bound 加密挡住），其次从本机浏览器读
        if settings.cookies_file:
            cmd += ["--cookies", settings.cookies_file]
        else:
            cmd += ["--cookies-from-browser", settings.cookies_from_browser]
    if settings.ffmpeg:
        # yt-dlp 自己只在 PATH 上找 ffmpeg；项目 bin/ 的静态包必须显式指路，
        # 否则分段下载（--download-sections）等功能会报 "ffmpeg is not installed"
        cmd += ["--ffmpeg-location", settings.ffmpeg]
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


def page_url(url: str, page: int) -> str:
    """给链接指定分P：追加或替换 `p` 参数；page <= 1 时移除该参数。

    移除而非写 `p=1` 是有意的：裸链接与 `?p=1` 的 canonical_url 不同，
    会算出两个 run-id 把同一份缓存劈成两半；裸链接 yt-dlp 本来就取 P1。
    非 B 站链接原样返回，由调用方保证只在拿到 BV 号时才调用。
    """
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != "p"]
    if page <= 1:
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
    query.append(("p", str(page)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def current_page(url: str) -> int:
    """读出链接里的 `p` 参数；没有或不合法时按第 1 个分P 处理。"""
    for key, value in parse_qsl(urlsplit(url).query):
        if key == "p":
            try:
                return max(1, int(value))
            except ValueError:
                return 1
    return 1


def _view_data(url: str, settings: Settings | None = None) -> dict | None:
    """B 站 view API 的 data 节点；任何失败返回 None（调用方自行降级）。"""
    bvid = extract_bvid(url)
    if not bvid:
        return None
    settings = settings or get_settings()
    try:
        with httpx.Client(
            timeout=httpx.Timeout(_VIEW_TIMEOUT_SEC),
            proxy=settings.proxy,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (videread)"},
        ) as client:
            response = client.get(_VIEW_API, params={"bvid": bvid})
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("code") != 0:
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else None


def _charging_flag(data: dict) -> bool:
    """view data 是否为付费内容（充电专属或付费稿件）。

    充电专属的标志在 view data **顶层** `is_upower_exclusive`——rights 里没有
    对应位（实测：充电专属视频该字段为 True，普通视频为 False，且 rights 里
    根本不存在 is_charging_arc）。付费稿件走 rights.ugc_pay。
    """
    for key in ("is_upower_exclusive",):
        try:
            if int(data.get(key) or 0) == 1:
                return True
        except (TypeError, ValueError):
            continue
    rights = data.get("rights")
    if not isinstance(rights, dict):
        return False
    for key in _PAYWALL_RIGHTS:
        try:
            if int(rights.get(key) or 0) == 1:
                return True
        except (TypeError, ValueError):
            continue
    return False


def charging_exclusive(url: str, settings: Settings | None = None) -> bool | None:
    """该稿件是否为充电专属/付费内容。

    True = 是（未充电用户只能拿到试看预览）；False = 确认不是；
    None = 无法判定（非 B 站链接 / view API 失败），调用方不应阻断。
    """
    data = _view_data(url, settings)
    if data is None:
        return None
    return _charging_flag(data)


def _cheese_ep_id(url: str) -> str | None:
    """B 站付费课程（Cheese）单集链接的 ep 号；非课程链接返回 None。"""
    match = _CHEESE_EP_RE.search(url or "")
    return match.group(1) if match else None


def _cheese_season(url: str, settings: Settings | None = None) -> dict | None:
    """pugv season 接口的 data 节点：课程信息 + 每集的 playable / duration。

    公开接口、无需登录；`playable` 反映**当前观看者**的权限——免费试看集
    匿名也是 true，付费集未购买为 false。失败返回 None。
    """
    ep_id = _cheese_ep_id(url)
    if not ep_id:
        return None
    settings = settings or get_settings()
    try:
        with httpx.Client(
            timeout=httpx.Timeout(_VIEW_TIMEOUT_SEC),
            proxy=settings.proxy,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (videread)",
                "Referer": "https://www.bilibili.com/",
            },
        ) as client:
            response = client.get(_CHEESE_SEASON_API, params={"ep_id": ep_id})
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("code") != 0:
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else None


def paywall_snapshot(url: str, settings: Settings | None = None) -> dict | None:
    """汇总两类付费内容的信号：paywalled（当前观看者能否看全集）与完整时长。

    返回 `{paywalled, full_duration, kind, title}`；kind = charging / cheese。
    与两类 API 都无关（普通视频 / 本地文件 / 探测失败）时返回 None。
    """
    if extract_bvid(url):
        data = _view_data(url, settings)
        if data is None:
            return None
        return {
            "paywalled": _charging_flag(data),
            "full_duration": _view_page_duration(url, data),
            "kind": "charging",
            "title": str(data.get("title") or "").strip(),
        }
    ep_id = _cheese_ep_id(url)
    if ep_id:
        season = _cheese_season(url, settings)
        if season is None:
            return None
        episode = next(
            (
                item
                for item in season.get("episodes") or []
                if isinstance(item, dict) and str(item.get("id")) == ep_id
            ),
            None,
        )
        if episode is None:
            return None
        try:
            duration = float(episode.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0
        return {
            # playable 反映当前观看者的权限：未购买 False，免费试看集 True
            "paywalled": not bool(episode.get("playable")),
            "full_duration": duration,
            "kind": "cheese",
            "title": str(season.get("title") or "").strip(),
        }
    return None


def _view_page_duration(url: str, data: dict) -> float:
    """view data 里的完整时长；多P 时取链接所指分P 的时长。

    yt-dlp -J 对带 `p` 参数的链接返回的是单P 时长，对照基准必须同口径，
    否则多P 视频会被误判成试看预览。
    """
    target = current_page(url)
    items = data.get("pages")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                if int(item.get("page", -1)) == target:
                    return float(item.get("duration") or 0)
            except (TypeError, ValueError):
                continue
    try:
        return float(data.get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0


def fetch_video_info(url: str, settings: Settings | None = None) -> VideoInfo | None:
    """用 B 站 view API 拉视频概要（主标题 / UP主 / 分P列表 / 充电标志）。

    任何失败都返回 None（按单P处理，不阻断预检）：预检只是给前端一个
    「让用户选分P、看清标题作者」的机会，探测失败不应比现状更糟——
    真正的链接有效性仍由 fetch_meta / 流水线的下载阶段兜底。
    """
    data = _view_data(url, settings)
    if data is None:
        return None
    pages: list[VideoPage] = []
    for item in data.get("pages") or []:
        if not isinstance(item, dict):
            continue
        try:
            pages.append(
                VideoPage(
                    page=int(item.get("page", 0)),
                    title=str(item.get("part") or "").strip(),
                    duration=float(item.get("duration") or 0),
                )
            )
        except (TypeError, ValueError):
            continue
    owner = data.get("owner")
    return VideoInfo(
        title=str(data.get("title") or "").strip(),
        uploader=str((owner or {}).get("name") or "").strip()
        if isinstance(owner, dict)
        else "",
        pages=[item for item in pages if item.page >= 1],
        charging=_charging_flag(data),
    )


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
    传入 `subtitle_dir` 时顺带下载平台字幕到该目录。字幕必须单独一次
    不带 `-J` 的调用：`-J` 隐含 simulate 模式，`--write-subs` 不落文件。
    """
    settings = settings or get_settings()

    # 充电专属/付费课程拦截：view / pugv API 的标志对所有人公开，预检即可识别。
    # 这两类内容暂不支持解析（2026-10-19 用户决策）：未付费拿不到完整内容，
    # 试看预览生成的报告没有意义还白烧 ASR/LLM 费用，因此不做登录态例外。
    paywall = paywall_snapshot(url, settings)
    if paywall is not None:
        if paywall["paywalled"]:
            raise DownloadError(paywall_reject_text(paywall["kind"], paywall["title"]))
        # 第二层对照基准：页面完整时长（多P 取所选分P，与 yt-dlp 的单P 口径一致）
        view_duration = paywall["full_duration"]
    else:
        view_duration = 0.0

    result = _yt_dlp(["-J", "--skip-download", url], settings, timeout=180.0)
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
    if (
        view_duration > 0
        and duration > 0
        and duration < view_duration * PREVIEW_DURATION_RATIO
    ):
        raise DownloadError(_preview_reject_text(duration, view_duration))
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
        uploader_id=str(info.get("uploader_id") or info.get("channel_id") or "").strip(),
        subtitles=[],
    )
    if not meta.title:
        raise DownloadError("视频标题为空，链接可能无效")
    if subtitle_dir is not None:
        meta.subtitles = _download_subtitles_into(url, subtitle_dir, settings)
    return meta


def _cookies_dict(settings: Settings) -> dict[str, str]:
    """Netscape cookies.txt → 名值字典（供 httpx 直连 B 站 API 时携带）。"""
    if not settings.cookies_file:
        return {}
    path = Path(settings.cookies_file)
    if not path.is_file():
        return {}
    cookies: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7 and parts[5] and parts[6].strip():
                cookies[parts[5]] = parts[6].strip()
    except OSError:
        return {}
    return cookies


def _srt_timecode(seconds: float) -> str:
    """字幕 JSON 的秒数 → SRT 时间码（HH:MM:SS,mmm）。"""
    seconds = max(seconds, 0.0)
    hours = int(seconds // 3600)
    minutes = int(seconds % 3600 // 60)
    whole = seconds - hours * 3600 - minutes * 60
    return f"{hours:02d}:{minutes:02d}:{int(whole):02d},{int(round(whole % 1 * 1000)):03d}"


def _subtitle_json_to_srt(body: list) -> str:
    """AI 字幕 JSON（body: from/to/content）→ SRT 文本，与 yt-dlp 的 json2srt 同构。"""
    lines: list[str] = []
    for index, item in enumerate(body):
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "")
        if not content:
            continue
        try:
            start, end = float(item.get("from") or 0), float(item.get("to") or 0)
        except (TypeError, ValueError):
            continue
        lines.append(
            f"{index + 1}\n{_srt_timecode(start)} --> {_srt_timecode(end)}\n{content}\n"
        )
    return "\n".join(lines)


def _wbi_mixin_key(settings: Settings) -> str:
    """nav 接口取 wbi_img 双 key，按混淆表重排得 32 位混入密钥。

    密钥随 B 站下发（通常每日轮换），每次字幕下载现取现用即可，不值得缓存。
    nav 接口未登录也能访问（游客也有 wbi_img），这里带 cookies 只是顺手。
    """
    try:
        with httpx.Client(
            timeout=httpx.Timeout(_PLAYER_TIMEOUT_SEC),
            proxy=settings.proxy,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (videread)"},
        ) as client:
            payload = client.get(_NAV_API, cookies=_cookies_dict(settings)).json()
        wbi_img = (payload.get("data") or {}).get("wbi_img") or {}
        img_url, sub_url = str(wbi_img.get("img_url") or ""), str(wbi_img.get("sub_url") or "")
        img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
        sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
    except (httpx.HTTPError, ValueError):
        return ""
    combined = img_key + sub_key
    return "".join(combined[i] for i in _WBI_MIXIN_TAB if i < len(combined))[:32]


def _wbi_sign(params: dict, mixin_key: str) -> dict:
    """按 B 站 wbi 规则签名：加 wts → 排序 → 过滤字符 → md5 拼 w_rid。"""
    filtered = {
        key: "".join(c for c in str(value) if c not in "!'()*")
        for key, value in sorted({**params, "wts": int(time.time())}.items())
    }
    w_rid = hashlib.md5((urlencode(filtered) + mixin_key).encode()).hexdigest()
    return {**filtered, "w_rid": w_rid}


def _subtitle_span_ok(body: list, duration: float) -> bool:
    """字幕末段时间是否合理：远超视频时长的字幕是接口错乱数据，判否。

    容忍 50% + 10s 的余量（片尾曲、字幕轨自身的小偏差）。
    """
    if duration <= 0 or not body:
        return True
    try:
        last_to = max(float(seg.get("to") or 0) for seg in body if isinstance(seg, dict))
    except (TypeError, ValueError):
        return True
    return last_to <= duration * 1.5 + 10


def _api_subtitles(url: str, dest_dir: Path, settings: Settings) -> list[dict]:
    """签名 wbi/v2 接口直接拉平台字幕（含 yt-dlp 拿不到的 AI 字幕）。

    需要登录态（SESSDATA）：AI 字幕对未登录请求不可见，接口返回空列表。
    多P 链接按 current_page 选取对应分P 的 cid。任何失败返回空列表，
    由调用方回退 yt-dlp 路径。返回与 collect_subtitles 同构的记录。

    防御：接口偶尔会对请求返回**别的视频**的字幕（实测见过 34s 视频配
    562s 亲子内容），因此按分P 时长校验字幕末段时间，超出 50% 以上整体
    丢弃，宁走 ASR 也不能把别人的稿子写进报告。
    """
    bvid = extract_bvid(url)
    if not bvid:
        return []
    settings = settings or get_settings()
    view = _view_data(url, settings)
    if view is None:
        return []
    # 多P：页码从 1 起；view data 的 cid/duration 是 P1，pages[N-1] 才是所选分P
    page = max(current_page(url), 1)
    pages = view.get("pages") or []
    page_item = pages[page - 1] if page <= len(pages) else {}
    if not isinstance(page_item, dict):
        page_item = {}
    cid = page_item.get("cid") or view.get("cid")
    duration = float(page_item.get("duration") or view.get("duration") or 0)
    if not cid:
        return []
    mixin_key = _wbi_mixin_key(settings)
    if not mixin_key:
        return []
    try:
        with httpx.Client(
            timeout=httpx.Timeout(_PLAYER_TIMEOUT_SEC),
            proxy=settings.proxy,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (videread)",
                "Referer": "https://www.bilibili.com/",
            },
        ) as client:
            response = client.get(
                _PLAYER_API,
                params=_wbi_sign({"bvid": bvid, "cid": cid}, mixin_key),
                cookies=_cookies_dict(settings),
            )
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("code") != 0:
                return []
            subs = (payload.get("data") or {}).get("subtitle") or {}
            items = subs.get("subtitles") or []
            dest_dir.mkdir(parents=True, exist_ok=True)
            written = 0
            for item in items:
                if not isinstance(item, dict):
                    continue
                lan, sub_url = str(item.get("lan") or ""), str(item.get("subtitle_url") or "")
                if not lan or not sub_url:
                    continue
                if sub_url.startswith("//"):
                    sub_url = "https:" + sub_url
                body = client.get(sub_url).json().get("body") or []
                srt = _subtitle_json_to_srt(body)
                if not srt.strip():
                    continue
                if not _subtitle_span_ok(body, duration):
                    continue  # 张冠李戴的错乱字幕，整体丢弃
                (dest_dir / f"subtitle.{lan}.srt").write_text(srt, encoding="utf-8")
                written += 1
    except (httpx.HTTPError, ValueError, OSError):
        return []
    if written == 0:
        return []
    return collect_subtitles(dest_dir)


def _download_subtitles_into(
    url: str, subtitle_dir: Path, settings: Settings
) -> list[dict]:
    """下载平台字幕到 `subtitle.<lang>.srt`；没有字幕时返回空列表。

    优先走 player v2 接口（登录态下能拿到 AI 字幕；yt-dlp 的 wbi/v2 通道
    已拿不到）；接口失败或非 BV 链接（如付费课程）回退 yt-dlp。
    """
    api_subs = _api_subtitles(url, subtitle_dir, settings)
    if api_subs:
        return api_subs
    subtitle_dir.mkdir(parents=True, exist_ok=True)
    _yt_dlp(
        [
            "--skip-download",
            "--write-subs",
            "--sub-langs",
            _SUBTITLE_LANGS,
            "--convert-subs",
            "srt",
            "-o",
            str(subtitle_dir / "subtitle.%(ext)s"),
            url,
        ],
        settings,
        timeout=300.0,
    )
    return collect_subtitles(subtitle_dir)


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


def download_video_section(
    url: str,
    dest_dir: Path,
    index: int,
    start: float,
    duration: float,
    settings: Settings | None = None,
    *,
    max_height: int = 720,
) -> Path:
    """定点拉取视频的一个片段（仅画面轨，不含音轨），供抽帧使用。

    `--force-keyframes-at-cuts` 保证切点精确（切点处重编码），片段起点
    即请求的 `start`，抽帧位置可据此换算。多个片段分多次调用而不是合并成
    一次：输出按 `clip.{index:03d}.%(ext)s` 命名，失败可单段重试。
    """
    settings = settings or get_settings()
    dest_dir.mkdir(parents=True, exist_ok=True)
    result = _yt_dlp(
        [
            "-f",
            f"bestvideo[height<={max_height}][ext=mp4]/bestvideo[height<={max_height}]/bestvideo",
            "--download-sections",
            f"*{start:.3f}-{start + duration:.3f}",
            "--force-keyframes-at-cuts",
            "-o",
            str(dest_dir / f"clip.{index:03d}.%(ext)s"),
            url,
        ],
        settings,
        timeout=600,
    )
    if result.returncode != 0:
        raise _fail("下载视频片段", result)
    from .frames import clip_path

    clip = clip_path(dest_dir, index)
    if clip is None:
        raise DownloadError(f"片段下载完成但未找到文件：{dest_dir} clip.{index:03d}.*")
    return clip


def collect_subtitles(dest_dir: Path) -> list[dict]:
    """收集目录中的 `subtitle.<lang>.srt` 为 meta 记录（lang + 相对文件名）。"""
    return [
        {"lang": path.name[len("subtitle.") : -len(".srt")], "path": path.name}
        for path in sorted(dest_dir.glob("subtitle.*.srt"))
    ]


def cookies_have_login(cookies_file: str | None) -> bool:
    """cookies.txt 是否含非空 SESSDATA（B 站登录态的唯一判据）。

    AI 生成字幕（ai-zh / ai-en）只对登录用户可见，设备 cookie（buvid3 等）
    不算登录。Netscape 格式每行 7 列 tab 分隔，第 6 列是名字、第 7 列是值。
    """
    if not cookies_file:
        return False
    path = Path(cookies_file)
    if not path.is_file():
        return False
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7 and parts[5] == "SESSDATA" and parts[6].strip():
                return True
    except OSError:
        return False
    return False


def subtitle_login_hint(settings: Settings) -> str | None:
    """本期没抓到字幕时，给一条「为什么 / 怎么办」的提示；已具备登录态则 None。

    只做静态判断（cookies 文件里有没有 SESSDATA / 是否配置浏览器直读），
    不发任何网络请求。`cookies-from-browser` 视为已登录——文件锁与加密问题
    由 _yt_dlp 的去 cookie 重试兜底，这里无法也不必廉价验证。
    """
    if settings.cookies_from_browser or cookies_have_login(settings.cookies_file):
        return None
    if settings.cookies_file:
        return (
            "cookies 文件里没有 SESSDATA（B 站登录态），AI 生成字幕对未登录"
            "用户不可见——本期未抓到字幕。用已登录的浏览器重新导出一份 "
            "cookies.txt（需包含 SESSDATA）可让字幕优先路径生效，"
            "省去音频下载与 ASR 费用。"
        )
    return (
        "本期未抓到平台字幕。B 站 AI 生成字幕（常见于口播/知识类视频）需登录态"
        "才可见：在 .env 里配置 YTDLP_COOKIES_FILE（已登录浏览器导出的 "
        "cookies.txt）后，带 AI 字幕的视频可由字幕直接建稿，跳过音频下载与 ASR。"
    )


def download_subtitles(
    url: str, dest_dir: Path, settings: Settings | None = None
) -> list[dict]:
    """下载平台自带字幕到 `subtitle.<lang>.srt`；没有字幕时返回空列表。"""
    settings = settings or get_settings()
    return _download_subtitles_into(url, dest_dir, settings)


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