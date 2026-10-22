"""配置与环境（对应开发文档 §10）。

配置分层：`.env` → 环境变量 → CLI 参数（后者覆盖前者）。
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv

from .errors import UsageError

# src/videread/config.py -> 项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"
BIN_DIR = PROJECT_ROOT / "bin"

DEFAULT_OUT_ROOT = PROJECT_ROOT / "runs"

# --- 超时与上限（§12 Windows 落地清单）---
LLM_TIMEOUT_SEC = 180.0
ASR_SEGMENT_TIMEOUT_SEC = 600.0
PIPELINE_TIMEOUT_SEC = 20 * 60
MAX_VIDEO_DURATION_SEC = 2 * 3600

# 单次提交云 ASR 的音频时长上限；超过则按静音点切段并行提交（§6.5）
ASR_SEGMENT_SECONDS = 1800.0
# 云 ASR 并发提交的默认路数（ASR_MAX_CONCURRENCY 环境变量可覆盖）
ASR_MAX_CONCURRENCY = 3

# 实时（WebSocket）通道的单段时长上限，比异步通道短得多（ASR_REALTIME_SEGMENT_SECONDS 覆盖）。
# 实时通道是「一条连接推完一整段」的长连接：段越长，单次失败要重推的音频越多、
# 连接被服务端回收的风险越高。取 5 分钟让单次失败的重推代价可控，
# 且每段结束即落盘，中断后重跑只补未完成的段；多段之间按 ASR_MAX_CONCURRENCY 并行推流。
ASR_REALTIME_SEGMENT_SECONDS = 300.0

# 实时通道的推流倍速（ASR_REALTIME_SPEEDUP 覆盖）。1.0 = 按音频真实时长匀速推，
# 与官方示例一致；服务端按实时速率解码，推快了就来不及消费、缓冲堆压，
# 最终表现为 keepalive ping 超时（连接被判定假死而断开）。
# 实测 4.0 稳定（90s 音频 32s、完整 300s 段 90s 出结果），8.0 收益见顶（瓶颈
# 变为服务端收尾处理），不建议超过 4.0。
ASR_REALTIME_SPEEDUP = 1.0

# 逐节写作的并发路数。各节互相独立，串行调用是整个流水线最长的一段（§6.8）
LLM_WRITE_CONCURRENCY = 3

# 转写单元上限字符数（§5.3）
TRANSCRIPT_MAX_CHARS = 160

# 字幕优先：视频带平台字幕时直接用它构造转写稿，跳过音频下载与 ASR（§6.3）
SUBTITLE_FIRST = True

# 报告输出语言：默认 zh（简体中文，历史行为不变）。CLI --lang 或 .env 的
# REPORT_LANG 可覆盖。语言名见 LANG_NAMES，未收录的代码按原样透传给 LLM。
REPORT_LANG = "zh"

# 报告语言 → 展示名。名字直接写进 prompt，用各语言的「自称」而非中文译名，
# LLM 对自称的语种遵循度最好。zh 之外的语言按「词」计数篇幅。
LANG_NAMES = {
    "zh": "简体中文",
    "en": "English",
    "ja": "日本語",
    "ko": "한국어",
    "fr": "Français",
    "de": "Deutsch",
    "es": "Español",
    "pt": "Português",
    "ru": "Русский",
    "it": "Italiano",
}


def lang_display_name(code: str) -> str:
    """语言代码 → prompt 用的展示名；未收录的代码原样返回。"""
    return LANG_NAMES.get((code or "").strip().lower(), (code or "").strip())

# 截图（纯展示模式）：按大纲节定点拉取视频小段抽帧，以 base64 内嵌报告。
# 默认关：--frames 显式开启，或 .env 设 FRAMES=1。
FRAMES_ENABLED = False
FRAME_MAX_TOTAL = 16      # 全片截图上限，超出按节顺序截断
FRAME_CLIP_SECONDS = 5.0  # 每个截图点定点拉取的视频时长
FRAME_SOURCE_HEIGHT = 720  # 拉取的视频轨最大高度（抽帧后缩到 FRAME_WIDTH 需要 ≥960 源宽）
FRAME_WIDTH = 960          # 输出图片宽度（等比缩放）
FRAME_JPEG_QUALITY = 4     # ffmpeg mjpeg -q:v（2 最优、数值越大越差，4 ≈ JPEG 80）
FRAME_LEAD_IN = 1.0        # 定点片段向前多取的秒数，用于抵消关键帧对齐误差

# PDF 导出：用本机 Edge / Chrome 无头打印，不引入额外依赖。
PDF_TIMEOUT_SEC = 120.0

# DashScope 接入点；专属/私有化（MaaS）部署可用 DASHSCOPE_BASE_URL 覆盖（§6.5）
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/api/v1"


@dataclass(frozen=True)
class Settings:
    asr_backend: str
    subtitle_first: bool
    frames_enabled: bool
    dashscope_api_key: str
    dashscope_base_url: str
    dashscope_model: str
    # 提交给 Paraformer 的 language_hints；设为 auto 时不传该参数（服务端自动识别）
    dashscope_language: str
    # 云 ASR 并发提交路数（dashscope / openai / dashscope-realtime 切段后并行；local 不用）
    asr_max_concurrency: int
    # 实时通道单段时长上限与推流倍速（asr_realtime_segment_seconds / asr_realtime_speedup）
    asr_realtime_segment_seconds: float
    asr_realtime_speedup: float
    llm_base_url: str
    llm_api_key: str
    llm_model_plan: str
    llm_model_write: str
    llm_write_concurrency: int
    llm_timeout: float
    proxy: str | None
    cookies_from_browser: str | None
    cookies_file: str | None
    ffmpeg: str | None
    ffprobe: str | None
    pdf_browser: str | None
    # 报告输出语言（结构规划 / 逐节写作 / 问答的自然语言产出；zh 之外按词计篇幅）
    report_lang: str

    def require_dashscope(self) -> str:
        if not self.dashscope_api_key:
            raise UsageError(
                "缺少 DASHSCOPE_API_KEY，请复制 .env.example 为 .env 并填写密钥"
            )
        return self.dashscope_api_key

    def require_llm(self) -> str:
        if not self.llm_api_key:
            raise UsageError("缺少 LLM_API_KEY，请在 .env 中填写")
        return self.llm_api_key


def load_env() -> None:
    """载入 .env（不覆盖已存在的环境变量），并确保项目根可被导入。"""
    load_dotenv(ENV_FILE, override=False)


def resolve_binary(env_key: str, name: str) -> str | None:
    """外部程序解析顺序：环境变量 → 项目 bin/ → 系统 PATH（§6.4）。"""
    explicit = os.environ.get(env_key, "").strip()
    if explicit:
        return explicit

    candidates = [f"{name}.exe", name] if sys.platform == "win32" else [name]
    for candidate in candidates:
        local = BIN_DIR / candidate
        if local.is_file():
            return str(local)

    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:  # 参数校验失败不重试（§11.2）
        raise UsageError(f"环境变量 {key} 不是合法数字：{raw!r}") from exc


def _env_int(key: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:  # 参数校验失败不重试（§11.2）
        raise UsageError(f"环境变量 {key} 不是合法整数：{raw!r}") from exc
    if value < minimum:
        raise UsageError(f"环境变量 {key} 不能小于 {minimum}：{raw!r}")
    return value


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise UsageError(f"环境变量 {key} 不是合法开关：{raw!r}")


def get_settings(**overrides: object) -> Settings:
    """读取配置；`overrides` 中非 None 的值覆盖环境变量（CLI 参数优先级最高）。"""
    load_env()
    proxy = (
        os.environ.get("HTTPS_PROXY", "").strip()
        or os.environ.get("HTTP_PROXY", "").strip()
        or None
    )
    cookies_file = os.environ.get("YTDLP_COOKIES_FILE", "").strip() or None
    if cookies_file:
        # 相对路径以项目根为基准，避免 yt-dlp 按 CWD 解析导致找不到文件
        path = Path(cookies_file).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        cookies_file = str(path) if path.is_file() else None
    settings = Settings(
        asr_backend=(os.environ.get("ASR_BACKEND", "dashscope").strip() or "dashscope"),
        subtitle_first=_env_bool("SUBTITLE_FIRST", SUBTITLE_FIRST),
        frames_enabled=_env_bool("FRAMES", FRAMES_ENABLED),
        dashscope_api_key=os.environ.get("DASHSCOPE_API_KEY", "").strip(),
        dashscope_base_url=(
            os.environ.get("DASHSCOPE_BASE_URL", "").strip() or DASHSCOPE_BASE_URL
        ),
        dashscope_model=(os.environ.get("DASHSCOPE_MODEL", "").strip() or "paraformer-v2"),
        dashscope_language=(
            os.environ.get("DASHSCOPE_LANGUAGE", "").strip() or "zh"
        ),
        asr_max_concurrency=_env_int("ASR_MAX_CONCURRENCY", ASR_MAX_CONCURRENCY),
        asr_realtime_segment_seconds=_env_float(
            "ASR_REALTIME_SEGMENT_SECONDS", ASR_REALTIME_SEGMENT_SECONDS
        ),
        # 倍速必须 >= 1.0：小于 1 会把音频放慢推，等于让服务端按更长的真实时间
        # 去解码同一段音频，白白拉长连接时长，反而更容易踩超时
        asr_realtime_speedup=max(
            1.0, _env_float("ASR_REALTIME_SPEEDUP", ASR_REALTIME_SPEEDUP)
        ),
        llm_base_url=(
            os.environ.get("LLM_BASE_URL", "").strip() or "https://api.deepseek.com/v1"
        ),
        llm_api_key=os.environ.get("LLM_API_KEY", "").strip(),
        llm_model_plan=(os.environ.get("LLM_MODEL_PLAN", "").strip() or "deepseek-chat"),
        llm_model_write=(os.environ.get("LLM_MODEL_WRITE", "").strip() or "deepseek-chat"),
        llm_write_concurrency=_env_int("LLM_WRITE_CONCURRENCY", LLM_WRITE_CONCURRENCY),
        llm_timeout=_env_float("LLM_TIMEOUT", LLM_TIMEOUT_SEC),
        proxy=proxy,
        cookies_from_browser=(
            os.environ.get("YTDLP_COOKIES_FROM_BROWSER", "").strip() or None
        ),
        cookies_file=cookies_file,
        ffmpeg=resolve_binary("FFMPEG_BIN", "ffmpeg"),
        ffprobe=resolve_binary("FFPROBE_BIN", "ffprobe"),
        pdf_browser=(os.environ.get("PDF_BROWSER_BIN", "").strip() or None),
        report_lang=(os.environ.get("REPORT_LANG", "").strip().lower() or REPORT_LANG),
    )
    applied = {k: v for k, v in overrides.items() if v is not None}
    return replace(settings, **applied) if applied else settings


def configure_console() -> None:
    """避免 Windows 控制台中文乱码（§12）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass