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

# 充电专属/付费内容拦截（§6.3）：view API 的 rights 对所有人公开，即使未充电也
# 能拿到「这是充电专属稿件」的标志与完整时长——而 yt-dlp 对未充电用户只会下到
# 几分钟的试看预览流。两个信号配合使用：
#   rights 标志 + 未配登录态 → 直接拒绝（此时下载只会白烧 ASR/LLM 钱）；
#   页面完整时长 vs yt-dlp 实际时长（< 80% 判为预览）→ 兜底拦截「配了 cookies
#   但没给该 UP 充电」的场景。
_PAYWALL_RIGHTS = ("is_charging_arc", "ugc_pay")
PREVIEW_DURATION_RATIO = 0.8


def _paywall_reject_text(kind: str, has_cookies: bool) -> str:
    """付费内容拒绝的用户提示：全程大白话，具体配置方法见 README「B 站登录」。"""
    if kind == "cheese":
        opener = "这是 B 站上的付费课程"
        condition = "购买这门课程后"
        confirm = "如果你已经购买这门课程"
    else:
        opener = "这是「充电专属」视频"
        condition = "给这位 UP 主充电后"
        confirm = "如果你已经给这位 UP 主充电"
    if has_cookies:
        return (
            f"{opener}：当前配置的 B 站账号没有观看权限，已停止解析。\n"
            f"{confirm}，请确认配置里填的是观看时使用的那个账号的登录信息；"
            "如果是别的账号，换好之后重试即可（配置方法见 README 的「B 站登录」一节）。"
        )
    return (
        f"{opener}：只有{condition}才能观看完整内容，已停止解析。\n"
        "如果你有观看权限，请按 README 的「B 站登录」一节，把 B 站账号的登录信息"
        "填入配置文件后重试。"
    )


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
    """view data 的 rights 里是否带「需要充电/付费才能看全片」的位。"""
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


def _paywall_snapshot(url: str, settings: Settings | None = None) -> dict | None:
    """汇总两类付费内容的信号：paywalled（当前观看者能否看全集）与完整时长。

    返回 `{paywalled, full_duration, kind}`；kind = charging / cheese。
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

    # 充电专属/付费课程拦截（第一层）：view / pugv API 的标志对所有人公开。
    # 未配登录态时 yt-dlp 只会下到试看预览（或直接报需要购买），直接拒绝，
    # 不白烧 ASR/LLM 费用；配了登录态则放行——已充电/已购买用户可完整解析，
    # 未购买由第二层时长比对与 yt-dlp 的购买校验兜底。
    has_cookies = bool(settings.cookies_file or settings.cookies_from_browser)
    paywall = _paywall_snapshot(url, settings)
    if paywall is not None:
        if paywall["paywalled"] and not has_cookies:
            raise DownloadError(_paywall_reject_text(paywall["kind"], has_cookies))
        # 第二层对照基准：页面完整时长（多P 取所选分P，与 yt-dlp 的单P 口径一致）
        view_duration = paywall["full_duration"]
    else:
        view_duration = 0.0

    result = _yt_dlp(["-J", "--skip-download", url], settings, timeout=180.0)
    if result.returncode != 0:
        # 配了登录态但未购买（课程）/未充电（充电专属）时，yt-dlp 的报错是
        # 英文购买提示——快照已判定付费内容，统一换成大白话拒绝文案。
        if paywall is not None and paywall["paywalled"]:
            raise DownloadError(_paywall_reject_text(paywall["kind"], has_cookies))
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


def _download_subtitles_into(
    url: str, subtitle_dir: Path, settings: Settings
) -> list[dict]:
    """单独一次 yt-dlp 调用下载平台字幕；没有可用字幕时返回空列表。"""
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