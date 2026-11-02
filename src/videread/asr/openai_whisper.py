"""OpenAI whisper-1 备用实现（对应开发文档 §6.5）。

与 DashScope 后端共用同一份抽象；长音频同样按静音点切段并并行提交。
需要独立的 OPENAI_API_KEY / OPENAI_BASE_URL（未配置时回退 LLM_* 变量）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from ..config import ASR_SEGMENT_TIMEOUT_SEC, Settings
from ..errors import AsrError, UsageError
from .base import AsrSegment, ChunkedAsr, offset_segments

_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_MODEL = "whisper-1"


class OpenAiWhisperAsr(ChunkedAsr):
    """OpenAI `whisper-1` 实现。"""

    name = "openai"

    def __init__(
        self,
        settings: Settings,
        *,
        cache_dir: Path | None = None,
        progress: Callable[[str], None] | None = None,
        cancel_check: Callable[[], None] | None = None,
    ) -> None:
        self.settings = settings
        self.cache_dir = cache_dir
        self.progress = progress or (lambda _msg: None)
        self.cancel_check = cancel_check
        self.model = os.environ.get("OPENAI_ASR_MODEL", "").strip() or _DEFAULT_MODEL
        self.base_url = (
            os.environ.get("OPENAI_BASE_URL", "").strip() or _DEFAULT_BASE_URL
        )
        self.api_key = (
            os.environ.get("OPENAI_API_KEY", "").strip() or settings.llm_api_key
        )
        if not self.api_key:
            raise UsageError("使用 openai 后端需要配置 OPENAI_API_KEY")
        self.failures: list[dict[str, object]] = []
        self._client = None

    def _sdk(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=ASR_SEGMENT_TIMEOUT_SEC,
            )
        return self._client

    # whisper 接口按文件计费、无分段缓存；部分段失败时静默降级（原行为）
    def _use_part_cache(self) -> bool:
        return False

    def _warn_partial(self) -> bool:
        return False

    def _transcribe_chunk(
        self, audio: Path, index: int, start: float, end: float
    ) -> list[AsrSegment]:
        return offset_segments(self._transcribe_file(audio), start)

    def _transcribe_file(self, audio: Path) -> list[AsrSegment]:
        from ..retry import retry_call

        def _do() -> list[AsrSegment]:
            with audio.open("rb") as fh:
                response = self._sdk().audio.transcriptions.create(
                    model=self.model,
                    file=fh,
                    response_format="verbose_json",
                    timestamp_granularities=["segment"],
                )
            raw_segments = getattr(response, "segments", None) or []
            segments = [
                AsrSegment(
                    start=float(item.start),
                    end=float(item.end),
                    text=str(item.text).strip(),
                )
                for item in raw_segments
                if str(getattr(item, "text", "")).strip()
            ]
            if not segments:
                raise AsrError(f"whisper 返回空转写：{audio.name}")
            return segments

        return retry_call(_do, attempts=3, base=1.0)