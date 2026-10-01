"""HTML → Markdown 导出（`videread.markdown`）的用例。

样例取自报告真实产出的标签形态：`<div>` 嵌在 `<p>` 内、组件容器、表格、details。
"""

from __future__ import annotations

from videread.markdown import html_to_markdown


def test_headings_and_paragraphs():
    html = (
        "<head><title>视频阅读报告</title></head>"
        '<h1>标题</h1><p class="lead">导语</p>'
        '<h2><span class="num">1</span>第一章<span class="section-time">00:00</span></h2>'
        "<div data-source-units=\"u0001\"><p>正文一段</p></div>"
    )

    out = html_to_markdown(html)

    assert out.startswith("# 标题")
    assert "视频阅读报告" not in out          # <title> 是文档头，不进正文
    # 序号 span 丢掉，时间戳补空格，不与标题文字粘连
    assert "## 第一章 00:00" in out
    assert "导语" in out
    assert "正文一段" in out


def test_full_document_head_is_skipped():
    """回归：`<meta>` 没有结束标签，若按标签栈计数，正文会被整篇吞掉。"""
    html = (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width">'
        "<title>视频阅读报告</title><style>h1{color:red}</style></head>"
        "<body><p>正文</p></body></html>"
    )

    out = html_to_markdown(html)

    assert out == "正文\n"


def test_inline_marks_and_links():
    out = html_to_markdown('<p>含<strong>重点</strong>与<em>斜体</em>和<a href="https://e.com">链接</a>。</p>')

    assert "含**重点**与*斜体*和[链接](https://e.com)。" in out


def test_lists_and_steps():
    out = html_to_markdown('<ol class="steps"><li>第一步</li><li>第二步</li></ol><ul><li>要点</li></ul>')

    assert "1. 第一步" in out
    assert "2. 第二步" in out
    assert "- 要点" in out


def test_table_becomes_markdown_table():
    html = (
        '<table class="table-details"><thead><tr><th>项</th><th>值</th></tr></thead>'
        "<tbody><tr><td>耗时</td><td>3m</td></tr></tbody></table>"
    )

    out = html_to_markdown(html)

    assert "| 项 | 值 |" in out
    assert "| --- | --- |" in out
    assert "| 耗时 | 3m |" in out


def test_table_cell_pipe_is_escaped():
    out = html_to_markdown("<table><tr><th>a</th></tr><tr><td>甲|乙</td></tr></table>")

    assert r"甲\|乙" in out


def test_script_style_nav_and_svg_are_dropped():
    html = (
        "<nav class=\"report-nav\"></nav><style>h1{color:red}</style>"
        '<div class="cycle-map"><svg><path d="M0 0"/></svg></div>'
        "<script>var x = 1;</script><p>留下的正文</p>"
    )

    out = html_to_markdown(html)

    assert out == "留下的正文\n"


def test_details_summary_becomes_bold_line():
    out = html_to_markdown(
        '<details class="meta-fold"><summary>视频简介</summary>'
        '<div class="meta-fold-body">简介正文</div></details>'
    )

    assert "**视频简介**" in out
    assert "简介正文" in out


def test_entities_are_decoded():
    out = html_to_markdown("<p>AI &amp; 提示工程 &lt;工具&gt;</p>")

    assert "AI & 提示工程 <工具>" in out


def test_empty_html_yields_blank_line():
    assert html_to_markdown("") == "\n"

