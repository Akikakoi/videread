"""ASR 抽象层（对应开发文档 §6.5）。"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

from ..audio import cut_segment, detect_silence, plan_chunks, probe_duration
from ..config import ASR_SEGMENT_SECONDS, Settings
from ..errors import AsrError, CancelledError


@dataclass(frozen=True)
class AsrSegment:
    """一段 ASR 输出，不合并、不改写（§5.2）。"""

    start: float
    end: float
    text: str


@runtime_checkable
class AsrBackend(Protocol):
    def transcribe(self, audio: Path, *, duration: float | None = None) -> list[AsrSegment]:
        ...


def offset_segments(segments: list[AsrSegment], offset: float) -> list[AsrSegment]:
    """把段内相对时间戳加上该段的全局偏移（§6.5 时间戳修正）。"""
    if offset == 0:
        return list(segments)
    return [
        AsrSegment(start=s.start + offset, end=s.end + offset, text=s.text)
        for s in segments
    ]


def sort_segments(segments: list[AsrSegment]) -> list[AsrSegment]:
    return sorted(segments, key=lambda s: (s.start, s.end))


def write_raw_jsonl(segments: list[AsrSegment], path: Path) -> None:
    """结果立即落盘，避免重试导致重复付费（§6.5）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for segment in segments:
            fh.write(
                json.dumps(
                    {
                        "start": round(segment.start, 2),
                        "end": round(segment.end, 2),
                        "text": segment.text,
                    },
                    ensure_ascii=False,
                )
            )
            fh.write("\n")


def load_raw_jsonl(path: Path) -> list[AsrSegment]:
    segments: list[AsrSegment] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                segments.append(
                    AsrSegment(
                        start=float(row["start"]),
                        end=float(row["end"]),
                        text=str(row["text"]),
                    )
                )
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise AsrError(f"{path} 第 {lineno} 行不是合法 ASR 分段：{exc}") from exc
    return segments


class PartCacheMixin:
    """按段缓存转写结果，重跑时命中即跳过付费调用（§6.5）。"""

    cache_dir: Path | None

    def _part_path(self, index: int, start: float, end: float) -> Path | None:
        if self.cache_dir is None:
            return None
        # 文件名带切段边界：静音点漂移导致切段点变化后，旧缓存自动失效，
        # 不会把上一个音频的段错位拼进新结果
        return self.cache_dir / f"asr.part.{index:03d}.{start:.0f}-{end:.0f}.jsonl"

    def _read_part(self, index: int, start: float, end: float) -> list[AsrSegment] | None:
        path = self._part_path(index, start, end)
        if path is None or not path.is_file():
            return None
        try:
            segments = load_raw_jsonl(path)
        except AsrError:
            return None
        return segments or None

    def _write_part(
        self, index: int, segments: list[AsrSegment], start: float, end: float
    ) -> None:
        path = self._part_path(index, start, end)
        if path is not None:
            write_raw_jsonl(segments, path)


class ChunkedAsr(PartCacheMixin):
    """通用「按静音点切段 → 并行转写 → 合并」骨架。

    子类只需实现 `_transcribe_chunk(audio, index, start, end)`；其余钩子按需覆盖：
    `_segment_limit`（单段上限）、`_chunk_progress`（切段提示）、
    `_use_part_cache`（是否按段复用缓存）、`_warn_partial`（部分失败是否提示）。
    """

    settings: Settings
    progress: Callable[[str], None]
    cancel_check: Callable[[], None] | None
    failures: list[dict[str, object]]
    cache_dir: Path | None

    def transcribe(
        self, audio: Path, *, duration: float | None = None
    ) -> list[AsrSegment]:
        total = duration if duration is not None else probe_duration(audio, self.settings)
        chunks = self._chunk_boundaries(audio, total)
        if len(chunks) == 1:
            start, end = chunks[0]
            return self._transcribe_chunk(audio, 0, start, end)

        directory = self._chunk_dir(audio)
        directory.mkdir(parents=True, exist_ok=True)
        results: dict[int, list[AsrSegment]] = {}
        tasks: list[tuple[int, float, float, Path]] = []
        try:
            for index, (start, end) in enumerate(chunks):
                self._checkpoint()
                if self._use_part_cache():
                    cached = self._read_part(index, start, end)
                    if cached is not None:
                        self.progress(f"第 {index + 1} 段转写命中缓存，跳过")
                        results[index] = offset_segments(cached, start)
                        continue
                dest = directory / f"chunk.{index:03d}.wav"
                cut_segment(audio, dest, start, end, self.settings)
                tasks.append((index, start, end, dest))

            with ThreadPoolExecutor(max_workers=self._concurrency()) as pool:
                futures = {
                    pool.submit(self._transcribe_chunk, path, index, start, end): index
                    for index, start, end, path in tasks
                }
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        self._checkpoint()
                        results[index] = future.result()
                    except CancelledError:
                        # 撤销还没开始的段；已完成段的结果已落盘（asr.part.*），
                        # 正在推流的段跑完当前连接后自然收尾，重跑可续
                        for pending in futures:
                            pending.cancel()
                        raise
                    except Exception as exc:  # noqa: BLE001 - 单段失败不阻塞其他段
                        self.failures.append(
                            {"chunk": index, "error": f"{type(exc).__name__}: {exc}"}
                        )
        finally:
            for _index, _start, _end, path in tasks:
                path.unlink(missing_ok=True)

        return self._merge_results(chunks, results)

    # ------------------------------------------------------------------ 钩子

    def _segment_limit(self) -> float:
        """单段时长上限（秒）；实时通道会覆盖成更短的值。"""
        return ASR_SEGMENT_SECONDS

    def _chunk_progress(self, count: int, total: float) -> str:
        return f"长音频切段：{count} 段（并发 {self._concurrency()}）"

    def _use_part_cache(self) -> bool:
        return True

    def _warn_partial(self) -> bool:
        return True

    def _concurrency(self) -> int:
        return max(1, int(self.settings.asr_max_concurrency or 1))

    def _chunk_dir(self, audio: Path) -> Path:
        return (self.cache_dir or audio.parent) / "asr_chunks"

    def _checkpoint(self) -> None:
        if self.cancel_check is not None:
            self.cancel_check()

    # ------------------------------------------------------------------ 内部

    def _chunk_boundaries(
        self, audio: Path, total: float
    ) -> list[tuple[float, float]]:
        limit = self._segment_limit()
        if total <= limit:
            return [(0.0, total)]
        self._checkpoint()
        silences = detect_silence(audio, settings=self.settings)
        chunks = plan_chunks(total, silences, limit=limit)
        self.progress(self._chunk_progress(len(chunks), total))
        return chunks

    def _merge_results(
        self, chunks: list[tuple[float, float]], results: dict[int, list[AsrSegment]]
    ) -> list[AsrSegment]:
        missing = [index for index in range(len(chunks)) if index not in results]
        if missing and not results:
            raise AsrError(
                f"ASR 全部切段失败（{len(missing)}/{len(chunks)}）：{self.failures}"
            )
        if missing and self._warn_partial():
            self.progress(f"警告：{len(missing)} 段 ASR 失败，报告将缺少这部分内容")

        merged: list[AsrSegment] = []
        for index in sorted(results):
            merged.extend(results[index])
        return sort_segments(merged)