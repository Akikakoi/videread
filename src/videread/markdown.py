"""报告 HTML → Markdown 转换，供控制台「导出 MD」使用。

只覆盖报告实际会产出的标签子集：标题、段落、列表、引用、表格、行内强调与链接。
报告自有的 CSS 组件（.callout / .cards / .stats / SVG 示意图等）一律当作透明容器，
只保留其中的文字层次 —— Markdown 表达不了它们，导出面向粘贴到笔记软件的场景，
不追求视觉还原。

用标准库 `html.parser` 而不是正则：报告正文里存在 `<div>` 嵌在 `<p>` 内的非法嵌套，
逐标签的栈式处理比正则拼接更不容易把内容吃掉。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

__all__ = ["html_to_markdown"]

#: 整块丢弃：文档头、脚本、样式、章节导航、SVG 示意图
_DROP_TAGS = frozenset({"head", "title", "script", "style", "svg", "nav"})

#: 没有结束标签的标签：丢弃区间按「深度」计数时必须跳过，
#: 否则 `<meta charset="utf-8">` 这类标签会让计数永远回不到 0，正文被整篇吞掉
_VOID_TAGS = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }
)

#: 透明容器：自身不产出标记，只让内部内容独立成块
_BLOCK_TAGS = frozenset(
    {"body", "div", "section", "article", "header", "footer", "main", "figure"}
)

_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}

#: 行内标记 → 成对的 Markdown 定界符
_INLINE_MARKS = {"strong": "**", "b": "**", "em": "*", "i": "*"}

#: 节标题里这两个 span 紧挨标题文字，导出时需特殊处理（见 `_section_heading_span`）
_NUM_CLASS = "num"
_TIME_CLASS = "section-time"

_WS = re.compile(r"\s+")


def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
    for name, value in attrs:
        if name == "class" and value:
            return set(value.split())
    return set()


class _MarkdownConverter(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.inline: list[str] = []
        self.dropped: int = 0                   # 丢弃区间剩余深度，0 表示正常输出
        self.heading_level: int | None = None
        self.list_stack: list[dict] = []        # [{ordered, index}]
        self.link_href: list[str] = []
        self.table: list[list[str]] | None = None
        self.row: list[str] | None = None

    # ------------------------------------------------------------ 输出

    def result(self) -> str:
        self._flush()
        return "\n\n".join(self.blocks).strip() + "\n"

    def _emit(self, text: str) -> None:
        text = _WS.sub(" ", text).strip()
        if text:
            self.blocks.append(text)

    def _flush(self) -> None:
        """把行内缓冲整理成块；表格内不产出块，文本由单元格自行收集。"""
        if self.table is not None:
            return
        self._emit("".join(self.inline))
        self.inline = []

    def _flush_item(self) -> None:
        """结束一个 `<li>`：按所在列表的层级与序号加上前缀。"""
        text = _WS.sub(" ", "".join(self.inline)).strip()
        self.inline = []
        if not text:
            return
        if self.list_stack:
            entry = self.list_stack[-1]
            entry["index"] += 1
            prefix = f"{entry['index']}. " if entry["ordered"] else "- "
            indent = "  " * (len(self.list_stack) - 1)
        else:
            prefix, indent = "- ", ""
        self.blocks.append(f"{indent}{prefix}{text}")

    def _emit_table(self) -> None:
        rows = [row for row in (self.table or []) if any(cell for cell in row)]
        if not rows:
            return
        width = max(len(row) for row in rows)
        rows = [row + [""] * (width - len(row)) for row in rows]
        lines = [
            "| " + " | ".join(rows[0]) + " |",
            "| " + " | ".join(["---"] * width) + " |",
        ]
        lines.extend("| " + " | ".join(row) + " |" for row in rows[1:])
        self.blocks.append("\n".join(lines))

    # ------------------------------------------------------------ 行内

    def _open_inline(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _INLINE_MARKS:
            self.inline.append(_INLINE_MARKS[tag])
        elif tag == "code":
            self.inline.append("`")
        elif tag == "a":
            self.link_href.append(dict(attrs).get("href") or "")
            self.inline.append("[")
        elif tag == "summary":
            self.inline.append("**")
        elif tag == "span" and _TIME_CLASS in _classes(attrs):
            # 时间戳 span 紧贴标题文字，补一个空格避免与前一串文字粘连
            self.inline.append(" ")
        # 其余标签（span / img / 表格结构等）不产出标记，仅保留内部文字

    def _close_inline(self, tag: str) -> None:
        if tag in _INLINE_MARKS:
            self.inline.append(_INLINE_MARKS[tag])
        elif tag == "code":
            self.inline.append("`")
        elif tag == "a":
            href = self.link_href.pop() if self.link_href else ""
            self.inline.append(f"]({href})" if href else "]")
        elif tag == "summary":
            self.inline.append("**")

    # ------------------------------------------------------------ 标签

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.dropped:
            if tag not in _VOID_TAGS:
                self.dropped += 1
            return
        if tag in _DROP_TAGS:
            self.dropped = 1
            return
        if tag == "span" and _NUM_CLASS in _classes(attrs):
            # 节标题的手写序号 <span class="num">：导出后由 Markdown 标题层次表达顺序
            self.dropped = 1
            return

        if tag == "table":
            self._flush()
            self.table = []
            return
        if self.table is not None:
            if tag == "tr":
                self.row = []
            elif tag in ("td", "th"):
                self.inline = []
            elif tag == "br":
                self.inline.append(" ")
            else:
                self._open_inline(tag, attrs)
            return

        if tag in _HEADINGS:
            self._flush()
            self.heading_level = _HEADINGS[tag]
        elif tag in ("ul", "ol"):
            self._flush()
            self.list_stack.append({"ordered": tag == "ol", "index": 0})
        elif tag == "li":
            self._flush()
        elif tag == "hr":
            self._flush()
            self._emit("---")
        elif tag == "br":
            self.inline.append(" ")
        elif tag == "p" or tag in _BLOCK_TAGS or tag in ("details", "blockquote"):
            self._flush()
        else:
            self._open_inline(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if self.dropped:
            if tag not in _VOID_TAGS:
                self.dropped -= 1
            return

        if tag == "table":
            self._flush()
            self._emit_table()
            self.table = None
            self.row = None
            return
        if self.table is not None:
            if tag == "tr":
                if self.row is not None:
                    self.table.append(self.row)
                    self.row = None
            elif tag in ("td", "th"):
                cell = _WS.sub(" ", "".join(self.inline)).strip().replace("|", "\\|")
                self.inline = []
                if self.row is not None:
                    self.row.append(cell)
            else:
                self._close_inline(tag)
            return

        if tag in _HEADINGS:
            text = _WS.sub(" ", "".join(self.inline)).strip()
            self.inline = []
            level = self.heading_level or 1
            if text:
                self._emit("#" * level + " " + text)
            self.heading_level = None
        elif tag == "li":
            self._flush_item()
        elif tag in ("ul", "ol"):
            self._flush()
            if self.list_stack:
                self.list_stack.pop()
        elif tag == "p" or tag in _BLOCK_TAGS or tag in ("details", "blockquote"):
            self._flush()
        else:
            self._close_inline(tag)

    def handle_data(self, data: str) -> None:
        if self.dropped:
            return
        self.inline.append(data)


def html_to_markdown(html: str) -> str:
    """把报告 HTML 转成 Markdown 文本（结尾带换行）。"""
    converter = _MarkdownConverter()
    converter.feed(html)
    converter.close()
    return converter.result()
