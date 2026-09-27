"""§13.1：大纲校验用例（离线）。"""

from __future__ import annotations

import pytest

from videread.errors import LlmError
from videread.report.outline import validate
from videread.transcript import TranscriptUnit


def _units() -> list[TranscriptUnit]:
    return [
        TranscriptUnit(id="u0001", start=0.0, end=10.0, text="a"),
        TranscriptUnit(id="u0002", start=10.0, end=20.0, text="b"),
        TranscriptUnit(id="u0003", start=65.0, end=75.0, text="c"),
    ]


def test_validate_drops_illegal_ids_and_renumbers_sections():
    data = {
        "title": "标题",
        "subtitle": "",
        "lead": "导语",
        "profile": "not-a-profile",
        "sections": [
            {
                "id": "x9",
                "heading": "第一节",
                "intent": "讲清 A",
                "time_range": "99:99-99:99",
                "source_ids": ["u0001", "u9999", "u0001"],
            },
            {
                "id": "x8",
                "heading": "第二节",
                "intent": "讲清 B",
                "time_range": "",
                "source_ids": ["u0003", "u0002"],
            },
            {"heading": "无有效来源", "source_ids": ["u9999"]},
        ],
    }

    outline = validate(data, _units())

    assert [section.id for section in outline.sections] == ["s1", "s2"]
    assert outline.sections[0].source_ids == ["u0001"]
    assert outline.sections[0].time_range == "00:00-00:10"
    assert outline.sections[1].source_ids == ["u0002", "u0003"]
    assert outline.sections[1].time_range == "00:10-01:15"
    assert outline.profile == "argument"


def test_validate_keeps_valid_profile():
    data = {
        "title": "标题",
        "lead": "导语",
        "profile": "mechanism",
        "sections": [{"heading": "一", "source_ids": ["u0001"]}],
    }
    assert validate(data, _units()).profile == "mechanism"


def test_validate_requires_title():
    with pytest.raises(LlmError):
        validate({"title": "", "sections": []}, _units())


def test_validate_requires_at_least_one_section_with_sources():
    with pytest.raises(LlmError):
        validate({"title": "标题", "sections": [{"source_ids": ["u9999"]}]}, _units())