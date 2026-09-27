"""OpenAI whisper-1 备用实现（对应开发文档 §6.5）。

与 DashScope 后端共用同一份抽象；长音频同样按静音点切段并并行提交。
需要独立的 OPENAI_API_KEY / OPENAI_BASE_URL（未配置时回退 LLM_* 变量）。
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from ..audio import cut_segment, detect_silence, plan_chunks, probe_duration
from ..config import (
    ASR_MAX_CONCURRENCY,
    ASR_SEGMENT_SECONDS,
    ASR_SEGMENT_TIMEOUT_SEC,
    Settings,
)
from ..errors import AsrError, UsageError
from .base import AsrSegment, offset_segments, sort_segments

_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_MODEL = "whisper-1"


class OpenAiWhisperAsr:
    """OpenAI `whisper-1` 实现。"""

    name = "openai"

    def __init__(
        self,
        settings: Settings,
        *,
        cache_dir: Path | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self.cache_dir = cache_dir
        self.progress = progress or (lambda _msg: None)
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

    def transcribe(
        self, audio: Path, *, duration: float | None = None
    ) -> list[AsrSegment]:
        total = duration if duration is not None else probe_duration(audio, self.settings)
        if total <= ASR_SEGMENT_SECONDS:
            return self._transcribe_file(audio)

        silences = detect_silence(audio, settings=self.settings)
        chunks = plan_chunks(total, silences, limit=ASR_SEGMENT_SECONDS)
        self.progress(f"长音频切段：{len(chunks)} 段（并发 {ASR_MAX_CONCURRENCY}）")

        chunk_dir = (self.cache_dir or audio.parent) / "asr_chunks"
        chunk_dir.mkdir(parents=True, exist_ok=True)

        results: dict[int, list[AsrSegment]] = {}
        with ThreadPoolExecutor(max_workers=ASR_MAX_CONCURRENCY) as pool:
            futures = {}
            for index, (start, end) in enumerate(chunks):
                dest = chunk_dir / f"chunk.{index:03d}.wav"
                cut_segment(audio, dest, start, end, self.settings)
                futures[pool.submit(self._transcribe_file, dest)] = (index, start, dest)
            for future, (index, start, dest) in futures.items():
                try:
                    results[index] = offset_segments(future.result(), start)
                except Exception as exc:  # noqa: BLE001 - 单段失败不阻塞其他段
                    self.failures.append(
                        {"chunk": index, "error": f"{type(exc).__name__}: {exc}"}
                    )
                finally:
                    dest.unlink(missing_ok=True)

        if not results:
            raise AsrError(f"ASR 全部切段失败：{self.failures}")

        merged: list[AsrSegment] = []
        for index in sorted(results):
            merged.extend(results[index])
        return sort_segments(merged)

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