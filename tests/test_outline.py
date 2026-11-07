"""§13.1：大纲校验用例（离线）。"""

from __future__ import annotations

import re

import pytest

from videread.config import get_settings
from videread.download import VideoMeta
from videread.errors import LlmError
from videread.report.outline import (
    OUTLINE_CONTEXT_BUDGET,
    plan_outline,
    validate,
)
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


def test_plan_outline_samples_long_transcript_within_budget():
    """超长转写按时间抽样后再送模型，避免 2 小时视频直接撑爆上下文。"""
    units = [
        TranscriptUnit(
            id=f"u{index:04d}",
            start=float(index * 10),
            end=float(index * 10 + 10),
            text="很长的转写内容" * 20,
        )
        for index in range(1, 401)
    ]
    captured: list[str] = []

    class _FakeClient:
        def complete_json(self, *, system: str, user: str, model: str):
            captured.append(user)
            ids = re.findall(r"## (u\d+)", user)
            return (
                {
                    "title": "长视频",
                    "lead": "导语",
                    "profile": "mechanism",
                    "sections": [
                        {"id": "s1", "heading": "一", "intent": "讲清", "source_ids": [ids[0]]}
                    ],
                },
                "{}",
            )

    meta = VideoMeta(
        bvid="BV1xx411c7mD",
        title="长视频",
        uploader="某UP主",
        duration=4000.0,
        url="https://www.bilibili.com/video/BV1xx411c7mD",
    )

    outline = plan_outline(
        meta,
        units,
        client=_FakeClient(),  # type: ignore[arg-type]
        settings=get_settings(),
        progress=lambda _msg: None,
    )

    prompt = captured[0]
    assert "抽样" in prompt
    assert len(prompt) < OUTLINE_CONTEXT_BUDGET + 2000
    assert outline.sections and outline.sections[0].source_ids