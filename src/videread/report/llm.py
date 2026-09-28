"""OpenAI 兼容接口的 LLM 客户端。

规划与写作共用一套调用逻辑：退避重试、JSON 模式、token 统计。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass

from ..config import Settings
from ..errors import LlmError
from ..retry import retry_call

_JSON_FENCE = "```"


@dataclass
class Usage:
    """累计 token 用量。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens

    def describe(self) -> str:
        return f"{self.total / 1000:.1f}k tokens"


def strip_code_fence(text: str) -> str:
    """模型偶尔仍会加 Markdown 代码块，这里剥掉外层围栏。"""
    stripped = text.strip()
    if not stripped.startswith(_JSON_FENCE):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith(_JSON_FENCE):
        lines = lines[1:]
    if lines and lines[-1].strip() == _JSON_FENCE:
        lines = lines[:-1]
    return "\n".join(lines).strip()


class LlmClient:
    """一次构造，多次调用；`usage` 累计两条 prompt 的总用量。

    逐节写作会并发调用同一个实例，因此 SDK 懒加载与 token 累加都要加锁：
    前者避免并发各建一个客户端，后者避免 `+=` 读改写丢计数。
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.api_key = settings.require_llm()
        self.usage = Usage()
        self._client = None
        self._lock = threading.Lock()

    def _sdk(self):
        with self._lock:
            if self._client is None:
                from openai import OpenAI

                self._client = OpenAI(
                    api_key=self.api_key,
                    base_url=self.settings.llm_base_url,
                    timeout=self.settings.llm_timeout,
                )
            return self._client

    def complete(
        self,
        *,
        system: str,
        user: str,
        model: str,
        json_mode: bool = False,
    ) -> str:
        """单次对话补全，退避重试 3 次；JSON 模式非法仍抛错由上层回灌重试。"""

        def _do() -> str:
            kwargs: dict[str, object] = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            response = self._sdk().chat.completions.create(**kwargs)
            usage = getattr(response, "usage", None)
            if usage is not None:
                with self._lock:
                    self.usage.add(
                        Usage(
                            prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                            completion_tokens=int(
                                getattr(usage, "completion_tokens", 0) or 0
                            ),
                        )
                    )
            choices = getattr(response, "choices", None) or []
            if not choices:
                raise LlmError("LLM 返回空 choices")
            content = getattr(choices[0].message, "content", None)
            if not content:
                raise LlmError("LLM 返回空内容")
            return str(content)

        return retry_call(_do, attempts=3, base=1.0)

    def complete_json(
        self, *, system: str, user: str, model: str
    ) -> tuple[dict, str]:
        """返回 (解析后的对象, 原始文本)；解析失败抛 LlmError（携带原文）。"""
        raw = self.complete(system=system, user=user, model=model, json_mode=True)
        try:
            data = json.loads(strip_code_fence(raw))
        except json.JSONDecodeError as exc:
            raise LlmError(f"LLM 输出不是合法 JSON：{exc}\n原文：{raw[:1000]}") from exc
        if not isinstance(data, dict):
            raise LlmError(f"LLM 输出了非对象 JSON：{type(data).__name__}")
        return data, raw