"""realtime 后端用例（全部离线）：短音频单段路径与分段缓存。

回归背景：重构曾把 `_transcribe_chunk` 内联进长音频循环但漏改短音频分支，
导致 ≤30 分钟的音频一进实时通道就 AttributeError。这里用 monkeypatch 掉
`_stream`（不碰真实 WebSocket）锁住该路径的行为。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from videread.asr.base import AsrSegment
from videread.asr.realtime import DashScopeRealtimeAsr
from videread.config import get_settings
from videread.errors import AsrError


def make_backend(tmp_path: Path) -> DashScopeRealtimeAsr:
    return DashScopeRealtimeAsr(get_settings(), cache_dir=tmp_path)


SEGMENTS = [AsrSegment(start=0.0, end=2.0, text="第一句"), AsrSegment(start=2.0, end=4.0, text="第二句")]


def test_short_audio_streams_and_writes_part_cache(tmp_path: Path):
    backend = make_backend(tmp_path)
    monkey_stream = lambda _audio: list(SEGMENTS)  # noqa: E731 - 测试替身
    backend._stream = monkey_stream  # type: ignore[method-assign]

    result = backend.transcribe(Path("fake.wav"), duration=100.0)

    assert [(s.start, s.text) for s in result] == [(0.0, "第一句"), (2.0, "第二句")]
    assert (tmp_path / "asr.part.000.0-100.jsonl").is_file()


def test_short_audio_cache_hit_skips_stream(tmp_path: Path):
    backend = make_backend(tmp_path)
    backend._write_part(0, SEGMENTS, 0.0, 100.0)

    def _boom(_audio: Path) -> list[AsrSegment]:
        raise AssertionError("缓存命中时不应再推流")

    backend._stream = _boom  # type: ignore[method-assign]
    result = backend.transcribe(Path("fake.wav"), duration=100.0)

    assert [(s.start, s.text) for s in result] == [(0.0, "第一句"), (2.0, "第二句")]


def test_short_audio_empty_result_raises(tmp_path: Path):
    backend = make_backend(tmp_path)
    backend._stream = lambda _audio: []  # type: ignore[method-assign]

    with pytest.raises(AsrError, match="空结果"):
        backend.transcribe(Path("fake.wav"), duration=100.0)
    # 失败不落缓存
    assert not (tmp_path / "asr.part.000.0-100.jsonl").exists()
