"""逐节写作：每节一次独立调用，产物落盘便于单节重试（对应开发文档 §6.8 / §7.2）。"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Callable

from ..config import Settings, get_settings
from ..errors import CancelledError, LlmError
from ..transcript import TranscriptUnit, units_to_markdown
from . import prompts
from .llm import LlmClient
from .outline import Outline, OutlineSection

_SECTION_FILE = re.compile(r"^s\d+\.html$")
_SCRIPT_OR_STYLE = re.compile(
    r"<\s*(script|style)\b[^>]*>.*?(?:<\s*/\s*\1\s*>|$)", re.IGNORECASE | re.DOTALL
)
_STRUCTURAL_TAG = re.compile(r"<\s*/?\s*(html|head|body)\b[^>]*>", re.IGNORECASE)
#: 正文里绑定转写单元的属性；写作清洗与问答引用共用同一份解析规则
SOURCE_UNITS = re.compile(r'data-source-units\s*=\s*"([^"]*)"', re.IGNORECASE)
_CODE_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")
# 分节片段首行的语言标记：换语言重出时据此判定旧片段失效（§ 缓存与语言绑定）
_LANG_MARK = re.compile(r"^<!--videread:lang=([a-z-]+)-->$")
_ANY_TAG = re.compile(r"<[^>]+>")
# 模型偶发输出的 Markdown 强调标记：模板只认 HTML，字面 `**` 会原样显示给读者
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_BOLD_UNDERSCORE = re.compile(r"__(.+?)__", re.DOTALL)
_CLASS_ATTR = re.compile(r'(\s*)class\s*=\s*"([^"]*)"', re.IGNORECASE)
# 事件属性 / javascript: 链接 / 可交互标签：模型输出可能被转写内容诱导，
# 报告与问答都在本机 localhost 同源页面里渲染，必须在写盘前剥掉。
_EVENT_ATTR = re.compile(
    r"""\s+on[a-z]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.IGNORECASE
)
_JS_URL = re.compile(
    r"""\s+(?:href|xlink:href|src)\s*=\s*(?:"[^"]*(?:javascript|data:text/html)[^"]*"|'[^']*(?:javascript|data:text/html)[^']*'|[^\s>]*(?:javascript|data:text/html)[^\s>]*)""",
    re.IGNORECASE,
)
_DANGEROUS_TAG = re.compile(
    r"<\s*/?\s*(?:iframe|object|embed|form|input|button|textarea|select|meta|link)\b[^>]*>",
    re.IGNORECASE,
)
# 写作规则 7「不是/并非/不在于……而是/而在于」的拆句变体实测会出现「不在…而在」，
# 单独列出（2026-11-05 消融实验：摘除规则 7-9 后模型用该变体绕开窄匹配）
FORBIDDEN_STYLE = re.compile(
    r"不是[^。？！\n]{1,30}而是"
    r"|并非[^。？！\n]{1,30}而是"
    r"|不在于[^。？！\n]{1,30}而在于"
    r"|不在[^。？！\n]{1,30}而在"
)
# 写作规则 5-6 的旁观者引导语
NARRATION_VOICE = re.compile(r"作者认为|视频中提到|他据此判断|笔者认为|我认为")

BRIEF_TARGET_MIN = 1600
BRIEF_TARGET_MAX = 2200
BRIEF_HARD_MAX = 2400
BRIEF_SECTION_MAX = 350

# zh 之外语言按「词」计数：阈值约为中文字数 ÷ 2，同一信息量档位
BRIEF_TARGET_MIN_WORDS = 800
BRIEF_TARGET_MAX_WORDS = 1100
BRIEF_HARD_MAX_WORDS = 1200
BRIEF_SECTION_MAX_WORDS = 180


def _norm_lang(lang: str) -> str:
    """归一化语言代码：zh 变体折叠为 zh，其余小写透传（落标记用）。"""
    lang = (lang or "").strip().lower()
    return "zh" if lang in {"", "zh-cn", "zh-hans"} else lang


def _is_zh(lang: str) -> bool:
    return _norm_lang(lang) == "zh"


def _brief_limits(lang: str) -> tuple[int, int, int, int]:
    """(target_min, target_max, hard_max, section_max)，zh 按字、其他按词。"""
    if _is_zh(lang):
        return BRIEF_TARGET_MIN, BRIEF_TARGET_MAX, BRIEF_HARD_MAX, BRIEF_SECTION_MAX
    return (
        BRIEF_TARGET_MIN_WORDS,
        BRIEF_TARGET_MAX_WORDS,
        BRIEF_HARD_MAX_WORDS,
        BRIEF_SECTION_MAX_WORDS,
    )


@dataclass
class BriefLengthReport:
    chars: int
    issues: list[str] = field(default_factory=list)
    over_hard: bool = False
    unit: str = "字"


def extract_text(html: str) -> str:
    """提取 HTML 文本，用于实测字数（§7.4「不能凭估计声明达标」）。"""
    return _ANY_TAG.sub("", html)


def count_text_chars(html: str) -> int:
    """去掉全部空白与标签后的字符数。"""
    return len(re.sub(r"\s+", "", extract_text(html)))


def count_text_words(html: str) -> int:
    """去标签后的空白分词数（英文等按空格分词语言的篇幅计量）。

    标签替换成空格而不是直接删除：<br> / </p> 一类标签本身承担分词职责，
    删掉会把 `hello<br>world` 粘成 `helloworld` 而少计一词。
    """
    return len(re.sub(r"<[^>]+>", " ", html).split())


def check_brief_length(html_parts: list[str], lang: str = "zh") -> BriefLengthReport:
    if _is_zh(lang):
        total = count_text_chars("".join(html_parts))
        unit = "字"
    else:
        total = count_text_words("".join(html_parts))
        unit = "词"
    target_min, target_max, hard_max, _section = _brief_limits(lang)
    issues: list[str] = []
    if total > hard_max:
        issues.append(f"Brief 正文 {total} {unit}，超过硬上限 {hard_max} {unit}")
    elif total > target_max:
        issues.append(f"Brief 正文 {total} {unit}，超出目标区间上限 {target_max} {unit}")
    elif total < target_min:
        issues.append(f"Brief 正文 {total} {unit}，低于目标区间下限 {target_min} {unit}")
    return BriefLengthReport(chars=total, issues=issues, over_hard=total > hard_max, unit=unit)


def brief_hard_max(lang: str) -> int:
    """Brief 正文硬上限：zh 按字计数，其他语言按词计数。"""
    return _brief_limits(lang)[2]


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

    without_handlers = _EVENT_ATTR.sub("", cleaned)
    if without_handlers != cleaned:
        violations.append("包含事件属性（on*），已移除")
    cleaned = without_handlers

    without_js = _JS_URL.sub("", cleaned)
    if without_js != cleaned:
        violations.append("包含 javascript: / data:text/html 链接，已移除")
    cleaned = without_js

    without_tags = _DANGEROUS_TAG.sub("", cleaned)
    if without_tags != cleaned:
        violations.append("包含 iframe / form 等可交互标签，已移除")
    cleaned = without_tags

    def _fix_attr(match: re.Match[str]) -> str:
        ids = [item.strip() for item in match.group(1).split(",") if item.strip()]
        kept = [item for item in ids if item in allowed_ids]
        dropped = [item for item in ids if item not in allowed_ids]
        if dropped:
            violations.append(f"越界 source id 已剔除：{','.join(dropped)}")
        if not kept:
            return ""
        return f'data-source-units="{",".join(kept)}"'

    cleaned = SOURCE_UNITS.sub(_fix_attr, cleaned)
    cleaned = re.sub(r"\s+data-source-units=\"\"", "", cleaned)

    # 规则 5-9 的事后检测：只报警不改写（改写无法确定性判断原意，交给规则约束与复核）
    text = _ANY_TAG.sub("", cleaned)
    style_hits = list(dict.fromkeys(FORBIDDEN_STYLE.findall(text)))
    if style_hits:
        shown = "；".join(f"「{hit}」" for hit in style_hits[:3])
        violations.append(f"疑似禁用句式：{shown}{'等' if len(style_hits) > 3 else ''}")
    voice_hits = list(dict.fromkeys(NARRATION_VOICE.findall(text)))
    if voice_hits:
        violations.append(f"疑似旁观者引导语：{'、'.join(voice_hits[:5])}")

    return cleaned.strip(), violations


def assemble_section(
    section: OutlineSection, index: int, fragment: str, *, mode: str = "standard"
) -> str:
    """把 LLM 片段与本节 `<h2>` 节标题拼装为完整一节（§8.4）。

    节标题由程序确定性生成：模板样式与 `.report-nav` 脚本都依赖 `<h2>`，
    不能交给模型自由发挥。Standard/Deep 带手写编号与右侧时间戳；
    Brief/Outline 只给纯标题（编号交给模板 CSS 计数器，正文不显示时间戳）。
    """
    heading = escape(section.heading.strip())
    if mode in ("brief", "outline"):
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


def existing_sections(out_dir: Path, lang: str = "zh") -> dict[str, str]:
    """读取已完成的分节产物（单节失败可单独重试，已成功的节不重跑）。

    片段与语言绑定：落盘文件首行带 `<!--videread:lang=xx-->` 标记，与请求
    语言不符的节视为不存在——换语言重出时正文必须重写，否则会拼出英文标题
    配中文正文的报告。历史产物没有标记，只按 zh 对待。
    """
    directory = section_dir(out_dir)
    if not directory.is_dir():
        return {}
    found: dict[str, str] = {}
    for path in sorted(directory.glob("s*.html")):
        if not (_SECTION_FILE.match(path.name) and path.stat().st_size > 0):
            continue
        text = path.read_text(encoding="utf-8")
        head, _sep, tail = text.partition("\n")
        mark = _LANG_MARK.match(head.strip())
        if mark is not None:
            if mark.group(1) != lang:
                continue  # 语言不同：这一节必须按新语言重写
            text = tail  # 返回给调用方的片段不含标记行
        elif not _is_zh(lang):
            continue  # 无标记的历史产物只可能是 zh
        found[path.stem] = text
    return found


def _store_section(directory: Path, section_id: str, html: str, lang: str) -> None:
    """片段落盘：首行带语言标记，供换语言重出时判定缓存失效。"""
    (directory / f"{section_id}.html").write_text(
        f"<!--videread:lang={lang}-->\n{html}\n", encoding="utf-8", newline="\n"
    )


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
    lang: str = "zh",
    out_dir: Path,
    client: LlmClient | None = None,
    settings: Settings | None = None,
    progress: Callable[[str], None] | None = None,
    force: bool = False,
    allowed_classes: set[str] | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> list[str]:
    """逐节生成正文片段，返回按大纲顺序排好的 HTML 列表。

    各节互相独立：只读自己那节的转写单元、只写自己的 `sections/sN.html`，
    因此除缓存命中的节先按序快速处理外，其余各节用有界线程池并发调用，
    把串行的 LLM 等待时间压到最短；返回顺序始终与大纲一致。

    `cancel_check` 在每节完成后的检查点被调用（Web 控制台取消任务用）：
    抛 `CancelledError` 即终止。已收到结果的节照常落盘——钱已经花了，
    产物不该丢；未开始的节全部撤销，重跑时从断点续写。

    返回值是**已完成标题拼装**的整节 HTML（标题由 `assemble_section` 注入）。
    落盘的 `sections/sN.html` 只存 LLM 片段（首行是语言标记），标题在拼装时
    确定性生成，因此重跑只重渲染也能补齐标题，不必重新调用模型。
    """
    if not outline.sections:
        raise LlmError("大纲为空，无法逐节写作")
    settings = settings or get_settings()
    progress = progress or (lambda _msg: None)

    def _checkpoint() -> None:
        if cancel_check is not None:
            cancel_check()

    directory = section_dir(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    lang = _norm_lang(lang or getattr(outline, "lang", "") or "")
    cached = {} if force else existing_sections(out_dir, lang)
    unit_map = {unit.id: unit for unit in units}
    system = prompts.writer_system(mode, lang)
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

    _checkpoint()  # 缓存处理期间也可能已请求取消：不再提交新的 LLM 调用
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
            words = not _is_zh(lang)
            count = count_text_words(html) if words else count_text_chars(html)
            unit = "词" if words else "字"
            _tmin, _tmax, _hard, section_max = _brief_limits(lang)
            if count > section_max * 1.2:
                progress(f"第 {section.id} 节 {count} {unit}，明显超出 Brief 单节建议上限")
        return html

    # 单节失败不打断其他节：已成功的节照常落盘，最后统一抛出最先失败的那一节
    failures: list[tuple[int, BaseException]] = []
    cancelled: CancelledError | None = None
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
                _checkpoint()
                html = future.result()
            except CancelledError as exc:
                cancelled = exc
                break
            except Exception as exc:  # noqa: BLE001 - 收集后统一抛出；用户中断不在此列
                failures.append((index, exc))
                continue
            _store_section(directory, section.id, html, lang)
            done = counter.next()
            progress(f"第 {done}/{total} 节完成")
            parts[section.id] = assemble_section(section, index, html, mode=mode)

    if cancelled is not None:
        # 撤销未开始的节；已经拿到结果的节照常落盘（调用已付费，产物不丢）
        for future in futures:
            future.cancel()
        for future, (index, section) in futures.items():
            if not future.done() or future.cancelled():
                continue
            if future.exception() is not None:
                continue
            html = future.result()
            _store_section(directory, section.id, html, lang)
            parts[section.id] = assemble_section(section, index, html, mode=mode)
        saved = len(parts)
        progress(f"已取消：已完成的 {saved}/{total} 节已保留，重跑将从断点续写")
        raise cancelled

    if failures:
        _index, first = min(failures, key=lambda item: item[0])
        raise first
    return [parts[section.id] for section in outline.sections]