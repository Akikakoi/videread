"""§13.1：转写规范化用例（离线）。"""

from __future__ import annotations

from pathlib import Path

from videread.asr import load_raw_jsonl
from videread.asr.base import AsrSegment
from videread.transcript import build_units, merge_subtitles, parse_srt

FIXTURES = Path(__file__).parent / "fixtures"


def test_build_units_merges_short_segments():
    segments = [
        AsrSegment(start=0.0, end=1.0, text="第一句。"),
        AsrSegment(start=1.0, end=2.0, text="第二句。"),
    ]
    units = build_units(segments, max_chars=160)
    assert len(units) == 1
    assert units[0].text == "第一句。第二句。"
    assert units[0].id == "u0001"


def test_build_units_ids_are_continuous_and_unique():
    segments = [
        AsrSegment(start=i * 2.0, end=i * 2.0 + 2.0, text=f"这是第{i}段比较长的一句话。")
        for i in range(1, 12)
    ]
    units = build_units(segments, max_chars=30)
    assert len(units) > 1
    ids = [unit.id for unit in units]
    assert ids == [f"u{index:04d}" for index in range(1, len(units) + 1)]
    assert len(set(ids)) == len(ids)


def test_build_units_respects_max_chars():
    segments = [AsrSegment(start=0.0, end=300.0, text="很长的一段内容" * 12)]
    units = build_units(segments, max_chars=50)
    assert units
    assert max(len(unit.text) for unit in units) <= 50


def test_build_units_timestamps_are_monotonic():
    segments = [
        AsrSegment(start=0.0, end=60.0, text="第一段内容。" * 8),
        AsrSegment(start=60.0, end=120.0, text="第二段内容。" * 8),
        AsrSegment(start=120.0, end=180.0, text="第三段内容。" * 8),
    ]
    units = build_units(segments, max_chars=40)
    assert len(units) > 1
    for previous, current in zip(units, units[1:]):
        assert current.start >= previous.start
        assert current.end >= previous.end
    assert all(unit.end >= unit.start for unit in units)
    assert 0.0 <= units[0].start and units[-1].end <= 180.0


def test_load_raw_jsonl_and_merge_subtitles_keep_ids():
    segments = load_raw_jsonl(FIXTURES / "asr_sample.jsonl")
    assert len(segments) == 3

    units = build_units(segments)
    assert units

    cues = parse_srt(FIXTURES / "subtitle_sample.srt")
    assert len(cues) == 3

    merged = merge_subtitles(units, FIXTURES / "subtitle_sample.srt")
    assert [unit.id for unit in merged] == [unit.id for unit in units]
    assert all(unit.source == "subtitle" for unit in merged)