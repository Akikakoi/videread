"""ASR 抽象层（对应开发文档 §6.5）。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..errors import AsrError


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