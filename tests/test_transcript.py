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


def test_build_units_keeps_latin_word_spacing():
    """英文分段之间不能粘成 Hello.World。"""
    units = build_units(
        [
            AsrSegment(start=0.0, end=1.0, text="Hello."),
            AsrSegment(start=1.0, end=2.0, text="World."),
        ],
        max_chars=160,
    )
    assert units[0].text == "Hello. World."


def test_merge_subtitles_keeps_latin_word_spacing(tmp_path: Path):
    srt = tmp_path / "en.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nHello.\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\nWorld.\n",
        encoding="utf-8",
    )
    units = build_units([AsrSegment(start=0.0, end=2.0, text="x")])
    merged = merge_subtitles(units, srt)
    assert merged[0].text == "Hello. World."


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


def test_build_units_clamps_overlapping_asr_segments():
    """ASR 分段互相重叠时，跨段钳制起点，单元时间轴不能交叠。"""
    segments = [
        AsrSegment(start=0.0, end=10.0, text="第一段内容。" * 6),
        AsrSegment(start=9.5, end=12.0, text="第二段内容。" * 3),
        AsrSegment(start=11.8, end=12.0, text="第三段内容。" * 2),
    ]

    units = build_units(segments, max_chars=20)

    for previous, current in zip(units, units[1:]):
        assert current.start >= previous.end
    assert all(unit.end >= unit.start for unit in units)


def test_parse_srt_joins_multiline_cues_with_language_aware_spacing(tmp_path: Path):
    srt = tmp_path / "multi.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nHello\nworld\n\n"
        "2\n00:00:02,000 --> 00:00:04,000\n你好\n世界\n",
        encoding="utf-8",
    )

    cues = parse_srt(srt)

    assert cues[0][2] == "Hello world"
    assert cues[1][2] == "你好世界"


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