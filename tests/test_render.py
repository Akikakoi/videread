"""§13.1：模板填充与自包含检查用例（离线）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from videread.errors import RenderError
from videread.render import (
    OPTIONAL_PLACEHOLDERS,
    REQUIRED_PLACEHOLDERS,
    check_self_contained,
    render_report,
    template_classes,
    template_for,
)


def _ctx(**overrides: str) -> dict[str, str]:
    ctx = {
        "TITLE": "主标题",
        "SUBTITLE": "副标题",
        "LEAD": "导语一段话。",
        "ATTRIBUTION": "Bilibili ·《原片》<br><a href=\"https://example.com/v\">https://example.com/v</a>",
        "VIDEO_DESCRIPTION": "视频简介",
        "BODY": '<div data-source-units="u0001"><p>正文</p></div>',
        "SOURCES": "<p>来源与处理说明</p>",
    }
    ctx.update(overrides)
    return ctx


@pytest.mark.parametrize("mode", ["standard", "brief"])
def test_render_replaces_all_seven_placeholders(tmp_path: Path, mode: str):
    out = tmp_path / f"{mode}.html"
    render_report(template=template_for(mode), ctx=_ctx(), out=out)
    html = out.read_text(encoding="utf-8")
    assert "{{" not in html
    for value in ("主标题", "副标题", "导语一段话。", "<p>来源与处理说明</p>"):
        assert value in html


def test_render_raises_on_missing_required_placeholder(tmp_path: Path):
    template = tmp_path / "broken.html"
    template.write_text("<h1>{{TITLE}}</h1>{{BODY}}", encoding="utf-8")
    with pytest.raises(RenderError) as excinfo:
        render_report(template=template, ctx=_ctx(), out=tmp_path / "out.html")
    message = str(excinfo.value)
    for name in ("LEAD", "ATTRIBUTION", "SOURCES"):
        assert name in message


def test_render_raises_on_leftover_placeholder(tmp_path: Path):
    template = tmp_path / "typo.html"
    body = "".join(f"{{{{{name}}}}}" for name in REQUIRED_PLACEHOLDERS)
    template.write_text(body + "{{UNKNOWN}}", encoding="utf-8")
    with pytest.raises(RenderError) as excinfo:
        render_report(template=template, ctx=_ctx(), out=tmp_path / "out.html")
    assert "UNKNOWN" in str(excinfo.value)


def test_render_removes_empty_optional_blocks(tmp_path: Path):
    out = tmp_path / "empty.html"
    render_report(
        template=template_for("standard"),
        ctx=_ctx(SUBTITLE="", VIDEO_DESCRIPTION=""),
        out=out,
    )
    html = out.read_text(encoding="utf-8")
    for name in OPTIONAL_PLACEHOLDERS:
        assert f"{{{{{name}}}}}" not in html
    assert '<p class="subtitle">' not in html
    assert '<details class="meta-fold">' not in html


def test_render_does_not_escape_html_fragments(tmp_path: Path):
    out = tmp_path / "raw.html"
    render_report(template=template_for("standard"), ctx=_ctx(), out=out)
    assert 'data-source-units="u0001"' in out.read_text(encoding="utf-8")


def test_check_self_contained_flags_external_resources():
    assert check_self_contained('<link rel="stylesheet" href="https://cdn/a.css">')
    assert check_self_contained('<script src="https://cdn/a.js"></script>')
    assert check_self_contained('<img src="https://cdn/a.png">')
    assert check_self_contained('@import url("https://cdn/a.css");')
    fonts = check_self_contained(
        '<style>@font-face{font-family:x;src:url("https://cdn/a.woff2") format("woff2");}</style>'
    )
    assert any("字体" in item for item in fonts)
    assert check_self_contained('<p style="background:url(https://cdn/a.png)"></p>')


def test_check_self_contained_allows_visible_video_link():
    html = '<p class="attribution">原视频：<a href="https://www.bilibili.com/video/BV1">链接</a></p>'
    assert check_self_contained(html) == []


@pytest.mark.parametrize("mode", ["standard", "brief"])
def test_shipped_templates_are_self_contained(mode: str):
    html = template_for(mode).read_text(encoding="utf-8")
    assert check_self_contained(html) == []


def test_template_classes_lists_defined_components():
    classes = template_classes(template_for("standard"))
    # 组件骨架里点名的关键 class 必须能提取到，否则会被当作自创 class 误剔除
    assert {"callout", "keyline", "steps", "bars", "bar-track", "bar-fill", "bar-value"} <= classes
    assert {"stats", "tiles", "stat-value", "stat-label", "report-body", "num"} <= classes
    # 模板未定义的 class 不应出现
    assert "bar-item" not in classes
    assert "comparison-item" not in classes


def test_template_classes_for_brief_template():
    classes = template_classes(template_for("brief"))
    assert {"brief-timeline", "brief-pair", "brief-table"} <= classes
    assert "comparison-item" not in classes