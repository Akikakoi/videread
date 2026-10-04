"""生成层：两段式 LLM 调用（结构规划 → 逐节写作）。"""

from __future__ import annotations

from .llm import LlmClient, Usage, strip_code_fence
from .outline import Outline, OutlineSection, load, plan_outline, validate, write
from .qa import (
    Answer,
    Citation,
    answer_question,
    append_history,
    load_history,
    retrieve,
)
from .writer import (
    BriefLengthReport,
    assemble_section,
    check_brief_length,
    count_text_chars,
    extract_text,
    sanitize_section_html,
    strip_markdown_markers,
    write_sections,
)

__all__ = [
    "Answer",
    "BriefLengthReport",
    "Citation",
    "LlmClient",
    "Outline",
    "OutlineSection",
    "Usage",
    "answer_question",
    "append_history",
    "assemble_section",
    "check_brief_length",
    "count_text_chars",
    "extract_text",
    "load",
    "load_history",
    "plan_outline",
    "retrieve",
    "sanitize_section_html",
    "strip_code_fence",
    "strip_markdown_markers",
    "validate",
    "write",
    "write_sections",
]