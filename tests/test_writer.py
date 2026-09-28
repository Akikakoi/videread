"""§13.1 / §7.4：分节片段清洗与 Brief 字数用例（离线）。"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from videread.config import get_settings
from videread.report import writer
from videread.report.outline import Outline, OutlineSection
from videread.report.writer import (
    BRIEF_HARD_MAX,
    BRIEF_TARGET_MAX,
    BRIEF_TARGET_MIN,
    assemble_section,
    check_brief_length,
    sanitize_section_html,
    write_sections,
)
from videread.transcript import TranscriptUnit

_SECTION = OutlineSection(
    id="s1",
    heading="预热的作用",
    time_range="00:00-00:12",
    intent="讲清预热与冷启动延迟的关系",
    source_ids=["u0001"],
)


def test_sanitize_drops_out_of_range_source_ids():
    cleaned, violations = sanitize_section_html(
        '<div data-source-units="u0001,u0002">核心判断</div>', {"u0001"}
    )
    assert 'data-source-units="u0001"' in cleaned
    assert "u0002" not in cleaned
    assert violations


def test_sanitize_removes_empty_source_attribute():
    cleaned, _ = sanitize_section_html(
        '<div data-source-units="u9999">核心判断</div>', {"u0001"}
    )
    assert "data-source-units" not in cleaned
    assert "核心判断" in cleaned


def test_sanitize_strips_script_style_and_structural_tags():
    raw = "<html><body><style>p{}</style><p>正文</p><script>1</script></body></html>"
    cleaned, violations = sanitize_section_html(raw, set())
    assert "<script" not in cleaned
    assert "<style" not in cleaned
    assert "<html" not in cleaned and "<body" not in cleaned
    assert "<p>正文</p>" in cleaned
    assert len(violations) >= 1


def test_sanitize_strips_markdown_code_fence():
    cleaned, _ = sanitize_section_html("```html\n<p>正文</p>\n```", set())
    assert cleaned == "<p>正文</p>"


def test_check_brief_length_in_target_range():
    report = check_brief_length(["<p>" + "字" * ((BRIEF_TARGET_MIN + BRIEF_TARGET_MAX) // 2) + "</p>"])
    assert report.chars == (BRIEF_TARGET_MIN + BRIEF_TARGET_MAX) // 2
    assert report.issues == []
    assert not report.over_hard


def test_check_brief_length_flags_too_short_and_too_long():
    short = check_brief_length(["<p>" + "字" * (BRIEF_TARGET_MIN - 1) + "</p>"])
    assert short.issues and not short.over_hard

    long = check_brief_length(["<p>" + "字" * (BRIEF_TARGET_MAX + 1) + "</p>"])
    assert long.issues and not long.over_hard

    over = check_brief_length(["<p>" + "字" * (BRIEF_HARD_MAX + 1) + "</p>"])
    assert over.over_hard and over.issues


def test_assemble_section_standard_adds_num_and_time():
    html = assemble_section(_SECTION, 3, "<p>正文</p>", mode="standard")
    assert (
        '<h2><span class="num">3</span>预热的作用'
        '<span class="section-time">00:00-00:12</span></h2>' in html
    )
    assert html.endswith("<p>正文</p>")


def test_assemble_section_brief_has_plain_h2():
    html = assemble_section(_SECTION, 1, "<p>正文</p>", mode="brief")
    assert html.startswith("<h2>预热的作用</h2>")
    assert "num" not in html and "section-time" not in html


def test_sanitize_strips_markdown_markers():
    cleaned, violations = sanitize_section_html("<p>持续**三到十分钟**，属于__固定成本__。</p>", set())
    assert cleaned == "<p>持续三到十分钟，属于固定成本。</p>"
    assert any("Markdown" in item for item in violations)


def test_sanitize_strips_unknown_classes():
    cleaned, violations = sanitize_section_html(
        '<div class="callout comparison-item"><p>x</p></div>', set(), {"callout"}
    )
    assert cleaned == '<div class="callout"><p>x</p></div>'
    assert any("comparison-item" in item for item in violations)


def test_sanitize_drops_class_attribute_when_all_classes_unknown():
    cleaned, violations = sanitize_section_html(
        '<div class="bar-item"><p>x</p></div>', set(), {"bars"}
    )
    assert cleaned == "<div><p>x</p></div>"
    assert violations


def test_sanitize_keeps_classes_when_no_allowlist_given():
    cleaned, _ = sanitize_section_html('<div class="whatever">x</div>', set())
    assert 'class="whatever"' in cleaned


def test_write_sections_injects_heading_from_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """命中缓存的分节也要补上程序注入的 <h2>，且不应构造 LlmClient。"""

    class _NoLlm:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("命中缓存时不应构造 LlmClient")

    monkeypatch.setattr(writer, "LlmClient", _NoLlm)
    outline = Outline(
        title="t", subtitle="", lead="l", profile="mechanism", sections=[_SECTION]
    )
    (tmp_path / "sections").mkdir()
    (tmp_path / "sections" / "s1.html").write_text(
        '<div data-source-units="u0001"><p>正文</p></div>\n', encoding="utf-8"
    )

    parts = write_sections(outline, [], out_dir=tmp_path, mode="standard")

    assert len(parts) == 1
    assert '<h2><span class="num">1</span>预热的作用' in parts[0]
    assert "<p>正文</p>" in parts[0]
    # 落盘文件只存 LLM 片段，标题不写回缓存
    assert "<h2>" not in (tmp_path / "sections" / "s1.html").read_text(encoding="utf-8")


# ------------------------------------------------------------ 并发逐节写作


def _units() -> list[TranscriptUnit]:
    return [TranscriptUnit(id="u0001", start=0.0, end=10.0, text="正文", source="asr")]


def _outline(count: int) -> Outline:
    return Outline(
        title="t",
        subtitle="",
        lead="l",
        profile="mechanism",
        sections=[
            OutlineSection(
                id=f"s{i}",
                heading=f"第 {i} 章",
                time_range="00:00-00:10",
                intent="讲清机制",
                source_ids=["u0001"],
            )
            for i in range(1, count + 1)
        ],
    )


def test_write_sections_parallel_keeps_order_and_writes_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """并发只是加速，返回顺序必须仍与大纲一致，且每节都落盘。"""
    outline = _outline(4)
    guard = threading.Lock()
    active = {"now": 0, "peak": 0}

    class _FakeLlm:
        def __init__(self, _settings: object) -> None:
            pass

        def complete(self, **_kwargs: object) -> str:
            with guard:
                active["now"] += 1
                active["peak"] = max(active["peak"], active["now"])
            time.sleep(0.05)  # 给其他线程重叠的机会
            with guard:
                active["now"] -= 1
            return '<p data-source-units="u0001">正文</p>'

    monkeypatch.setattr(writer, "LlmClient", _FakeLlm)

    parts = write_sections(
        outline,
        _units(),
        out_dir=tmp_path,
        mode="standard",
        settings=get_settings(llm_write_concurrency=3),
    )

    assert len(parts) == 4
    for index in range(1, 5):
        assert f'<span class="num">{index}</span>第 {index} 章' in parts[index - 1]
        assert (tmp_path / "sections" / f"s{index}.html").is_file()
    assert active["peak"] >= 2, "并发没有生效"


def test_write_sections_keeps_successes_when_one_section_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """单节失败不应丢掉其他节的产物：已成功的节照样落盘，最后抛出该失败。"""
    outline = _outline(3)

    class _FlakyLlm:
        def __init__(self, _settings: object) -> None:
            pass

        def complete(self, *, user: str, **_kwargs: object) -> str:
            if "第 2 章" in user:
                raise RuntimeError("第 2 节炸了")
            return '<p data-source-units="u0001">正文</p>'

    monkeypatch.setattr(writer, "LlmClient", _FlakyLlm)

    with pytest.raises(RuntimeError, match="第 2 节炸了"):
        write_sections(
            outline,
            _units(),
            out_dir=tmp_path,
            mode="standard",
            settings=get_settings(llm_write_concurrency=3),
        )

    assert (tmp_path / "sections" / "s1.html").is_file()
    assert (tmp_path / "sections" / "s3.html").is_file()
    assert not (tmp_path / "sections" / "s2.html").exists()