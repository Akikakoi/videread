"""用户在 Web 界面填写的模型配置（自定义 API Key / 网关 / 模型 / 转写引擎）。

存放在项目根的 `.llm-settings.json`（已加入 .gitignore，不入库）。
配置分层优先级：CLI 显式参数 > 本文件 > `.env` / 环境变量 > 内置默认。

API Key 按端点（Base URL）分别保存：切换服务商不会覆盖别家的 Key，
切回去时留空即可沿用该端点之前保存的 Key。

key 以明文落盘：服务仅绑定本机、无鉴权（与 `.env` 同一信任边界），
任何 API 都不得把完整 key 回传给前端（见 web/app.py 的脱敏逻辑）。
"""

from __future__ import annotations

import json
import os
import re
import threading

from .config import DEFAULT_LLM_BASE_URL, PROJECT_ROOT
from .errors import UsageError

SETTINGS_FILE = PROJECT_ROOT / ".llm-settings.json"

# 顶层允许落盘的标量字段；API Key 单独存在 llm_api_keys 映射里
_SCALAR_FIELDS = (
    "llm_base_url",
    "llm_model_plan",
    "llm_model_write",
    "asr_backend",
)

#: 转写引擎白名单：与 asr 工厂支持的后端一一对应（空值 = 跟随 .env，不落盘）
_ASR_BACKENDS = ("funasr", "dashscope", "dashscope-realtime", "openai", "local")

_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)

_lock = threading.Lock()


def normalize_base_url(url: str) -> str:
    """Key 映射的索引键：忽略首尾空白与末尾斜杠，同一端点不同写法共用一把 Key。"""
    return (url or "").strip().rstrip("/")


def _env_base_url() -> str:
    """未保存 Base URL 时按环境变量推断端点（仅用于旧格式 Key 的归属迁移）。"""
    return os.environ.get("LLM_BASE_URL", "").strip() or DEFAULT_LLM_BASE_URL


def mask_key(key: str) -> str:
    """key 脱敏展示：只留末 4 位；过短或为空给占位符。"""
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return key[:3] + "****" + key[-4:]


def _read_raw() -> dict:
    """读取配置文件原文；文件缺失或损坏返回空 dict（回退 .env，不抛错）。"""
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _keys_from_raw(raw: dict) -> dict[str, str]:
    """读取 Key 映射；兼容旧格式的单 llm_api_key（归到当时的 Base URL 名下）。"""
    keys: dict[str, str] = {}
    saved = raw.get("llm_api_keys")
    if isinstance(saved, dict):
        for base, key in saved.items():
            base_text = normalize_base_url(str(base))
            key_text = str(key or "").strip()
            if base_text and key_text:
                keys[base_text] = key_text
    legacy_key = str(raw.get("llm_api_key", "")).strip()
    if legacy_key:
        legacy_raw_base = str(raw.get("llm_base_url", "")).strip()
        legacy_base = normalize_base_url(legacy_raw_base or _env_base_url())
        keys.setdefault(legacy_base, legacy_key)
    return keys


def load_api_keys() -> dict[str, str]:
    """全部端点已保存的完整 Key；只在后端内部用，前端只能拿到 mask_key 结果。"""
    return _keys_from_raw(_read_raw())


def key_for_base_url(base_url: str) -> str:
    """该端点已保存的 Key；没保存过返回空串。"""
    return load_api_keys().get(normalize_base_url(base_url), "")


def load_user_llm_settings() -> dict[str, str]:
    """读取用户配置；`llm_api_key` 解析为当前生效端点对应的那把 Key。

    文件缺失或损坏返回空 dict（回退 .env，不抛错）。
    """
    raw = _read_raw()
    data: dict[str, str] = {}
    for name in _SCALAR_FIELDS:
        value = str(raw.get(name, "")).strip()
        if value:
            data[name] = value
    base = data.get("llm_base_url") or _env_base_url()
    key = _keys_from_raw(raw).get(normalize_base_url(base), "")
    if key:
        data["llm_api_key"] = key
    return data


def save_user_llm_settings(
    *,
    llm_api_key: str = "",
    llm_base_url: str = "",
    llm_model_plan: str = "",
    llm_model_write: str = "",
    asr_backend: str | None = None,
) -> dict[str, str]:
    """整体覆盖式保存（前端每次提交完整表单）；空值字段不写入。

    Key 按端点分开记：填了就更新当前 Base URL 名下的 Key；留空或 "keep"
    表示沿用该端点已保存的 Key（切换服务商不会覆盖另一家的 Key）。
    asr_backend 传 None 表示本次不涉及（调用方未提供该字段）；
    传空串表示「跟随 .env」（删除已保存的覆盖值）；
    传具体后端名则校验白名单后落盘。
    """
    updates: dict[str, str] = {}
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

    removals: list[str] = []
    if asr_backend is not None:
        backend = asr_backend.strip().lower()
        if not backend:
            removals.append("asr_backend")  # 跟随 .env：撤掉用户覆盖
        elif backend in _ASR_BACKENDS:
            updates["asr_backend"] = backend
        else:
            raise UsageError(
                "未知的转写引擎："
                f"{asr_backend!r}（可选 {' / '.join(_ASR_BACKENDS)}，留空跟随 .env）"
            )

    new_key = (llm_api_key or "").strip()
    if new_key == "keep":
        new_key = ""

    with _lock:
        if not updates and not removals and not new_key:
            return load_user_llm_settings()
        raw = _read_raw()
        current: dict[str, object] = {
            name: str(raw[name]).strip()
            for name in _SCALAR_FIELDS
            if str(raw.get(name, "")).strip()
        }
        keys = _keys_from_raw(raw)
        if new_key:
            base = normalize_base_url(
                updates.get("llm_base_url")
                or str(current.get("llm_base_url", ""))
                or _env_base_url()
            )
            keys[base] = new_key
        for key in removals:
            current.pop(key, None)
        current.update(updates)
        if keys:
            current["llm_api_keys"] = keys
        tmp = SETTINGS_FILE.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(SETTINGS_FILE)  # 原子替换，避免半写文件被读到
    return load_user_llm_settings()


def clear_user_llm_settings(*, keep_asr: bool = False) -> bool:
    """删除用户配置，回退 .env / 内置默认；文件不存在返回 False。

    keep_asr=True 时只清 LLM 字段、保留转写引擎的页面选择——
    「写作模型恢复默认」不该顺手重置另一个页签的配置。
    """
    with _lock:
        if keep_asr:
            raw = _read_raw()
            asr = str(raw.get("asr_backend", "")).strip()
            if not asr:
                try:
                    SETTINGS_FILE.unlink()
                    return True
                except FileNotFoundError:
                    return False
            tmp = SETTINGS_FILE.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps({"asr_backend": asr}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(SETTINGS_FILE)
            return True
        try:
            SETTINGS_FILE.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise UsageError(f"无法删除用户配置文件：{exc}") from exc
