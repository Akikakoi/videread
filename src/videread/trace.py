"""分阶段耗时 / 用量 / 错误记录（对应开发文档 §5.5）。"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class Stage:
    """单个阶段的记录句柄：可写入 detail 与 cost_cny。"""

    name: str
    detail: dict[str, object] = field(default_factory=dict)
    cost_cny: float | None = None


class TraceWriter:
    """向 `run.trace.jsonl` 追加事件。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(
        self,
        stage: str,
        event: str,
        *,
        dur_ms: int | None = None,
        cost_cny: float | None = None,
        detail: dict[str, object] | None = None,
    ) -> None:
        row: dict[str, object] = {"ts": _now(), "stage": stage, "event": event}
        if dur_ms is not None:
            row["dur_ms"] = dur_ms
        if cost_cny is not None:
            row["cost_cny"] = cost_cny
        if detail:
            row["detail"] = detail
        with self.path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(row, ensure_ascii=False))
            fh.write("\n")

    @contextmanager
    def stage(self, name: str) -> Iterator[Stage]:
        import time

        record = Stage(name=name)
        started = time.monotonic()
        self.emit(name, "start")
        try:
            yield record
        except BaseException as exc:
            self.emit(
                name,
                "error",
                dur_ms=int((time.monotonic() - started) * 1000),
                detail={**record.detail, "error": f"{type(exc).__name__}: {exc}"},
            )
            raise
        self.emit(
            name,
            "end",
            dur_ms=int((time.monotonic() - started) * 1000),
            cost_cny=record.cost_cny,
            detail=record.detail or None,
        )

    def read(self) -> list[dict]:
        if not self.path.is_file():
            return []
        rows: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return rows