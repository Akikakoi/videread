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
MAX_VIDEO_DURATION_SEC = 4 * 3600

# 单次提交云 ASR 的音频时长上限；超过则按静音点切段并行提交（§6.5）
ASR_SEGMENT_SECONDS = 1800.0
ASR_MAX_CONCURRENCY = 3

# 转写单元上限字符数（§5.3）
TRANSCRIPT_MAX_CHARS = 160

# DashScope 接入点；专属/私有化（MaaS）部署可用 DASHSCOPE_BASE_URL 覆盖（§6.5）
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/api/v1"


@dataclass(frozen=True)
class Settings:
    asr_backend: str
    dashscope_api_key: str
    dashscope_base_url: str
    dashscope_model: str
    llm_base_url: str
    llm_api_key: str
    llm_model_plan: str
    llm_model_write: str
    llm_timeout: float
    proxy: str | None
    ffmpeg: str | None
    ffprobe: str | None
    out_root: Path

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


def get_settings(**overrides: object) -> Settings:
    """读取配置；`overrides` 中非 None 的值覆盖环境变量（CLI 参数优先级最高）。"""
    load_env()
    proxy = (
        os.environ.get("HTTPS_PROXY", "").strip()
        or os.environ.get("HTTP_PROXY", "").strip()
        or None
    )
    settings = Settings(
        asr_backend=(os.environ.get("ASR_BACKEND", "dashscope").strip() or "dashscope"),
        dashscope_api_key=os.environ.get("DASHSCOPE_API_KEY", "").strip(),
        dashscope_base_url=(
            os.environ.get("DASHSCOPE_BASE_URL", "").strip() or DASHSCOPE_BASE_URL
        ),
        dashscope_model=(os.environ.get("DASHSCOPE_MODEL", "").strip() or "paraformer-v2"),
        llm_base_url=(
            os.environ.get("LLM_BASE_URL", "").strip() or "https://api.deepseek.com/v1"
        ),
        llm_api_key=os.environ.get("LLM_API_KEY", "").strip(),
        llm_model_plan=(os.environ.get("LLM_MODEL_PLAN", "").strip() or "deepseek-chat"),
        llm_model_write=(os.environ.get("LLM_MODEL_WRITE", "").strip() or "deepseek-chat"),
        llm_timeout=_env_float("LLM_TIMEOUT", LLM_TIMEOUT_SEC),
        proxy=proxy,
        ffmpeg=resolve_binary("FFMPEG_BIN", "ffmpeg"),
        ffprobe=resolve_binary("FFPROBE_BIN", "ffprobe"),
        out_root=DEFAULT_OUT_ROOT,
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