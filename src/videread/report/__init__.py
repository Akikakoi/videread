"""生成层：两段式 LLM 调用（结构规划 → 逐节写作）。"""

from __future__ import annotations

from .llm import LlmClient, Usage, strip_code_fence
from .outline import Outline, OutlineSection, load, plan_outline, validate, write
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
    "BriefLengthReport",
    "LlmClient",
    "Outline",
    "OutlineSection",
    "Usage",
    "assemble_section",
    "check_brief_length",
    "count_text_chars",
    "extract_text",
    "load",
    "plan_outline",
    "sanitize_section_html",
    "strip_code_fence",
    "strip_markdown_markers",
    "validate",
    "write",
    "write_sections",
]