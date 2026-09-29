"""逐节写作：每节一次独立调用，产物落盘便于单节重试（对应开发文档 §6.8 / §7.2）。"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..config import Settings, get_settings
from ..errors import LlmError
from ..transcript import TranscriptUnit, units_to_markdown
from . import prompts
from .llm import LlmClient
from .outline import Outline, OutlineSection

_SECTION_FILE = re.compile(r"^s\d+\.html$")
_SCRIPT_OR_STYLE = re.compile(
    r"<\s*(script|style)\b[^>]*>.*?(?:<\s*/\s*\1\s*>|$)", re.IGNORECASE | re.DOTALL
)
_STRUCTURAL_TAG = re.compile(r"<\s*/?\s*(html|head|body)\b[^>]*>", re.IGNORECASE)
_SOURCE_UNITS = re.compile(r'data-source-units\s*=\s*"([^"]*)"', re.IGNORECASE)
_CODE_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")
_ANY_TAG = re.compile(r"<[^>]+>")
# 模型偶发输出的 Markdown 强调标记：模板只认 HTML，字面 `**` 会原样显示给读者
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_BOLD_UNDERSCORE = re.compile(r"__(.+?)__", re.DOTALL)
_CLASS_ATTR = re.compile(r'(\s*)class\s*=\s*"([^"]*)"', re.IGNORECASE)

BRIEF_TARGET_MIN = 1600
BRIEF_TARGET_MAX = 2200
BRIEF_HARD_MAX = 2400
BRIEF_SECTION_MAX = 350


@dataclass
class BriefLengthReport:
    chars: int
    issues: list[str] = field(default_factory=list)

    @property
    def over_hard(self) -> bool:
        return self.chars > BRIEF_HARD_MAX


def extract_text(html: str) -> str:
    """提取 HTML 文本，用于实测字数（§7.4「不能凭估计声明达标」）。"""
    return _ANY_TAG.sub("", html)


def count_text_chars(html: str) -> int:
    """去掉全部空白与标签后的字符数。"""
    return len(re.sub(r"\s+", "", extract_text(html)))


def check_brief_length(html_parts: list[str]) -> BriefLengthReport:
    total = count_text_chars("".join(html_parts))
    issues: list[str] = []
    if total > BRIEF_HARD_MAX:
        issues.append(f"Brief 正文 {total} 字，超过硬上限 {BRIEF_HARD_MAX} 字")
    elif total > BRIEF_TARGET_MAX:
        issues.append(f"Brief 正文 {total} 字，超出目标区间上限 {BRIEF_TARGET_MAX} 字")
    elif total < BRIEF_TARGET_MIN:
        issues.append(f"Brief 正文 {total} 字，低于目标区间下限 {BRIEF_TARGET_MIN} 字")
    return BriefLengthReport(chars=total, issues=issues)


def strip_markdown_markers(html: str) -> str:
    """去掉模型偶发输出的 Markdown 强调标记，只保留文字本身。"""
    return _MD_BOLD_UNDERSCORE.sub(r"\1", _MD_BOLD.sub(r"\1", html))


def strip_unknown_classes(html: str, allowed: set[str]) -> tuple[str, list[str]]:
    """剔除模板未定义的 class，保留元素与其余 class（§6.8 / §8.2）。

    未定义的 class 不会带来任何样式，留着只会让报告看起来"用了组件"却没生效。
    """
    dropped: list[str] = []

    def _fix(match: re.Match[str]) -> str:
        kept: list[str] = []
        for name in match.group(2).split():
            if name in allowed:
                kept.append(name)
            else:
                dropped.append(name)
        if not kept:
            # 连同属性前的空白一起吞掉，避免留下 `<p >` 之类的悬空空格；
            # 也因此不再需要全局 `\s+>` 清理——那会误改正文里的字面 `>`。
            return ""
        return f'{match.group(1)}class="{" ".join(kept)}"'

    cleaned = _CLASS_ATTR.sub(_fix, html)
    return cleaned, list(dict.fromkeys(dropped))


def sanitize_section_html(
    html: str, allowed_ids: set[str], allowed_classes: set[str] | None = None
) -> tuple[str, list[str]]:
    """清洗单节片段：剥掉代码块与结构性标签，剔除越界的 source id 与自创 class。"""
    violations: list[str] = []
    cleaned = _CODE_FENCE.sub("", html.strip())

    without_md = strip_markdown_markers(cleaned)
    if without_md != cleaned:
        violations.append("包含 Markdown 强调标记，已去除")
    cleaned = without_md

    if allowed_classes is not None:
        without_classes, dropped = strip_unknown_classes(cleaned, allowed_classes)
        if dropped:
            violations.append(f"未定义的 class 已剔除：{','.join(dropped)}")
        cleaned = without_classes

    without_blocks = _SCRIPT_OR_STYLE.sub("", cleaned)
    if without_blocks != cleaned:
        violations.append("包含 <script> / <style>，已移除")
    cleaned = without_blocks

    without_structure = _STRUCTURAL_TAG.sub("", cleaned)
    if without_structure != cleaned:
        violations.append("包含 <html> / <body> / <head>，已移除")
    cleaned = without_structure

    def _fix_attr(match: re.Match[str]) -> str:
        ids = [item.strip() for item in match.group(1).split(",") if item.strip()]
        kept = [item for item in ids if item in allowed_ids]
        dropped = [item for item in ids if item not in allowed_ids]
        if dropped:
            violations.append(f"越界 source id 已剔除：{','.join(dropped)}")
        if not kept:
            return ""
        return f'data-source-units="{",".join(kept)}"'

    cleaned = _SOURCE_UNITS.sub(_fix_attr, cleaned)
    cleaned = re.sub(r"\s+data-source-units=\"\"", "", cleaned)
    return cleaned.strip(), violations


def assemble_section(
    section: OutlineSection, index: int, fragment: str, *, mode: str = "standard"
) -> str:
    """把 LLM 片段与本节 `<h2>` 节标题拼装为完整一节（§8.4）。

    节标题由程序确定性生成：模板样式与 `.report-nav` 脚本都依赖 `<h2>`，
    不能交给模型自由发挥。Standard 带手写编号与右侧时间戳；Brief 只给纯标题
    （编号交给模板 CSS 计数器，正文不显示时间戳）。
    """
    heading = section.heading.strip()
    if mode == "brief":
        title = f"<h2>{heading}</h2>"
    else:
        time_html = (
            f'<span class="section-time">{section.time_range}</span>'
            if section.time_range
            else ""
        )
        title = f'<h2><span class="num">{index}</span>{heading}{time_html}</h2>'
    body = fragment.strip()
    return f"{title}\n{body}" if body else title


def section_dir(out_dir: Path) -> Path:
    return out_dir / "sections"


def existing_sections(out_dir: Path) -> dict[str, str]:
    """读取已完成的分节产物（单节失败可单独重试，已成功的节不重跑）。"""
    directory = section_dir(out_dir)
    if not directory.is_dir():
        return {}
    found: dict[str, str] = {}
    for path in sorted(directory.glob("s*.html")):
        if _SECTION_FILE.match(path.name) and path.stat().st_size > 0:
            found[path.stem] = path.read_text(encoding="utf-8")
    return found


def _section_units(section: OutlineSection, unit_map: dict[str, TranscriptUnit]) -> list[TranscriptUnit]:
    return [unit_map[unit_id] for unit_id in section.source_ids if unit_id in unit_map]


class _Counter:
    """并发下的完成计数：只增不减，供「第 k/n 节」进度文案使用。"""

    def __init__(self, start: int = 0) -> None:
        self._value = start
        self._lock = threading.Lock()

    def next(self) -> int:
        with self._lock:
            self._value += 1
            return self._value


def write_sections(
    outline: Outline,
    units: list[TranscriptUnit],
    *,
    mode: str = "standard",
    out_dir: Path,
    client: LlmClient | None = None,
    settings: Settings | None = None,
    progress: Callable[[str], None] | None = None,
    force: bool = False,
    allowed_classes: set[str] | None = None,
) -> list[str]:
    """逐节生成正文片段，返回按大纲顺序排好的 HTML 列表。

    各节互相独立：只读自己那节的转写单元、只写自己的 `sections/sN.html`，
    因此除缓存命中的节先按序快速处理外，其余各节用有界线程池并发调用，
    把串行的 LLM 等待时间压到最短；返回顺序始终与大纲一致。

    返回值是**已完成标题拼装**的整节 HTML（标题由 `assemble_section` 注入）。
    落盘的 `sections/sN.html` 只存 LLM 片段，标题在拼装时确定性生成，
    因此重跑只重渲染也能补齐标题，不必重新调用模型。
    """
    if not outline.sections:
        raise LlmError("大纲为空，无法逐节写作")
    settings = settings or get_settings()
    progress = progress or (lambda _msg: None)

    directory = section_dir(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    cached = {} if force else existing_sections(out_dir)
    unit_map = {unit.id: unit for unit in units}
    system = prompts.writer_system(mode)
    component_list = prompts.components_for(mode)
    total = len(outline.sections)

    parts: dict[str, str] = {}
    pending: list[tuple[int, OutlineSection]] = []
    done = 0

    # 先按大纲顺序处理缓存命中的节；全部命中时不应构造 LlmClient
    for index, section in enumerate(outline.sections, start=1):
        fragment = cached.get(section.id)
        if fragment is None:
            if not _section_units(section, unit_map):
                raise LlmError(f"第 {section.id} 节没有可用的转写单元")
            pending.append((index, section))
            continue
        fragment = strip_markdown_markers(fragment)
        if allowed_classes is not None:
            fragment, dropped = strip_unknown_classes(fragment, allowed_classes)
            if dropped:
                progress(f"第 {section.id} 节：未定义的 class 已剔除：{','.join(dropped)}")
        done += 1
        progress(f"第 {done}/{total} 节命中缓存，跳过")
        parts[section.id] = assemble_section(section, index, fragment, mode=mode)

    if not pending:
        return [parts[section.id] for section in outline.sections]

    llm = client or LlmClient(settings)
    counter = _Counter(done)

    def _generate(section: OutlineSection) -> str:
        section_units = _section_units(section, unit_map)
        user = prompts.fill(
            prompts.WRITER_USER,
            heading=section.heading,
            intent=section.intent,
            time_range=section.time_range,
            section_units_md=units_to_markdown(section_units),
            component_list=component_list,
        )
        raw = llm.complete(system=system, user=user, model=settings.llm_model_write)
        html, violations = sanitize_section_html(
            raw, set(section.source_ids), allowed_classes
        )
        for violation in violations:
            progress(f"第 {section.id} 节：{violation}")
        if mode == "brief":
            chars = count_text_chars(html)
            if chars > BRIEF_SECTION_MAX * 1.2:
                progress(f"第 {section.id} 节 {chars} 字，明显超出 Brief 单节建议上限")
        return html

    # 单节失败不打断其他节：已成功的节照常落盘，最后统一抛出最先失败的那一节
    failures: list[tuple[int, BaseException]] = []
    workers = max(1, min(settings.llm_write_concurrency, len(pending)))
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="videread-writer"
    ) as pool:
        futures = {
            pool.submit(_generate, section): (index, section)
            for index, section in pending
        }
        for future in as_completed(futures):
            index, section = futures[future]
            try:
                html = future.result()
            except Exception as exc:  # noqa: BLE001 - 收集后统一抛出；用户中断不在此列
                failures.append((index, exc))
                continue
            path = directory / f"{section.id}.html"
            path.write_text(html + "\n", encoding="utf-8", newline="\n")
            done = counter.next()
            progress(f"第 {done}/{total} 节完成")
            parts[section.id] = assemble_section(section, index, html, mode=mode)

    if failures:
        _index, first = min(failures, key=lambda item: item[0])
        raise first
    return [parts[section.id] for section in outline.sections]