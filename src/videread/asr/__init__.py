"""ASR 后端工厂。"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..config import Settings
from ..errors import UsageError
from .base import AsrBackend, AsrSegment, load_raw_jsonl, offset_segments, sort_segments
from .base import write_raw_jsonl

__all__ = [
    "AsrBackend",
    "AsrSegment",
    "get_backend",
    "load_raw_jsonl",
    "offset_segments",
    "sort_segments",
    "write_raw_jsonl",
]

_DASHSCOPE_ALIASES = {"dashscope", "paraformer", "paraformer-v2", "qwen"}
_DASHSCOPE_REALTIME_ALIASES = {
    "dashscope-realtime",
    "realtime",
    "paraformer-realtime",
    "paraformer-realtime-v2",
}
_OPENAI_ALIASES = {"openai", "openai-whisper", "whisper", "whisper-1"}


def get_backend(
    settings: Settings,
    *,
    cache_dir: Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> AsrBackend:
    name = (settings.asr_backend or "dashscope").strip().lower()
    if name in _DASHSCOPE_ALIASES:
        from .dashscope import DashScopeAsr

        return DashScopeAsr(settings, cache_dir=cache_dir, progress=progress)
    if name in _DASHSCOPE_REALTIME_ALIASES:
        from .realtime import DashScopeRealtimeAsr

        return DashScopeRealtimeAsr(settings, cache_dir=cache_dir, progress=progress)
    if name in _OPENAI_ALIASES:
        from .openai_whisper import OpenAiWhisperAsr

        return OpenAiWhisperAsr(settings, cache_dir=cache_dir, progress=progress)
    raise UsageError(
        "未知的 ASR_BACKEND："
        f"{settings.asr_backend!r}（可选 dashscope / dashscope-realtime / openai）"
    )