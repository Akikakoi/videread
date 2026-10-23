"""用户在 Web 界面填写的 LLM 配置（自定义 API Key / 网关 / 模型）。

存放在项目根的 `.llm-settings.json`（已加入 .gitignore，不入库）。
配置分层优先级：CLI 显式参数 > 本文件 > `.env` / 环境变量 > 内置默认。

key 以明文落盘：服务仅绑定本机、无鉴权（与 `.env` 同一信任边界），
任何 API 都不得把完整 key 回传给前端（见 web/app.py 的脱敏逻辑）。
"""

from __future__ import annotations

import json
import re
import threading

from .config import PROJECT_ROOT
from .errors import UsageError

SETTINGS_FILE = PROJECT_ROOT / ".llm-settings.json"

# 允许落盘的字段白名单；多余键一律丢弃，避免把任意内容写进 JSON
_ALLOWED_FIELDS = ("llm_api_key", "llm_base_url", "llm_model_plan", "llm_model_write")

_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)

_lock = threading.Lock()


def mask_key(key: str) -> str:
    """key 脱敏展示：只留末 4 位；过短或为空给占位符。"""
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return key[:3] + "****" + key[-4:]


def load_user_llm_settings() -> dict[str, str]:
    """读取用户配置；文件缺失或损坏返回空 dict（回退 .env，不抛错）。"""
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        k: str(raw[k]).strip()
        for k in _ALLOWED_FIELDS
        if str(raw.get(k, "")).strip()
    }


def save_user_llm_settings(
    *,
    llm_api_key: str = "",
    llm_base_url: str = "",
    llm_model_plan: str = "",
    llm_model_write: str = "",
) -> dict[str, str]:
    """整体覆盖式保存（前端每次提交完整表单）；空值字段不写入。

    api_key 传 "keep" 表示沿用已保存的 key（前端「留空保持不变」语义
    由路由层翻译成 keep，这里不感知）。
    """
    updates: dict[str, str] = {}
    if llm_api_key and llm_api_key != "keep":
        updates["llm_api_key"] = llm_api_key.strip()
    for name, value in (
        ("llm_base_url", llm_base_url),
        ("llm_model_plan", llm_model_plan),
        ("llm_model_write", llm_model_write),
    ):
        value = (value or "").strip()
        if not value:
            continue
        if name == "llm_base_url" and not _URL_RE.match(value):
            raise UsageError(f"Base URL 必须以 http(s):// 开头：{value!r}")
        updates[name] = value

    with _lock:
        if not updates:
            return load_user_llm_settings()
        current = load_user_llm_settings()
        current.update(updates)
        tmp = SETTINGS_FILE.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(SETTINGS_FILE)  # 原子替换，避免半写文件被读到
    return load_user_llm_settings()


def clear_user_llm_settings() -> bool:
    """删除用户配置文件，回退 .env / 内置默认；文件不存在返回 False。"""
    with _lock:
        try:
            SETTINGS_FILE.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise UsageError(f"无法删除用户配置文件：{exc}") from exc
