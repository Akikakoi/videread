"""报告内问答：在转写稿上做检索，回答带来源引用。

问答只在本地控制台侧栏使用（报告 HTML 本身保持自包含），数据全部来自
run 目录的 `transcript.jsonl` / `outline.json`，不触碰流水线其余阶段。

检索采取务实混合策略：转写总字符不超过预算时整份送模型（最准，省去检索误差），
超预算时按"问题字符 2-gram 与单元文本的重合度"筛选相关单元。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from ..config import Settings, get_settings
from ..errors import LlmError, UsageError
from ..transcript import TranscriptUnit, format_time, load_units, units_to_markdown
from . import prompts
from .llm import LlmClient
from .outline import Outline
from .outline import load as load_outline
from .writer import sanitize_section_html

#: 整份转写的字符预算：不超过就直接全量送模型，避免检索漏召
FULL_CONTEXT_BUDGET = 24000
#: 超预算时选取的单元数上限
TOP_K = 60
#: 问题长度上限（字符）
MAX_QUESTION_CHARS = 500

#: 问答答案允许使用的组件 class（其余按"未定义 class"剔除）
_QA_CLASSES = {"keyline", "callout"}

_SOURCE_UNITS = re.compile(r'data-source-units\s*=\s*"([^"]*)"', re.IGNORECASE)
_SNIPPET_CHARS = 60


@dataclass
class Citation:
    """答案里引用到的转写单元：用于在报告与视频之间跳转。"""

    unit_id: str
    seconds: float
    time_text: str
    snippet: str
    section_id: str | None
    section_index: int | None


@dataclass
class Answer:
    answer_html: str
    citations: list[Citation]
    usage_total: int

    def to_dict(self) -> dict:
        return {
            "answer_html": self.answer_html,
            "citations": [asdict(citation) for citation in self.citations],
            "usage_total": self.usage_total,
        }


# ------------------------------------------------------------------ 检索


def _bigrams(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", text)
    if len(compact) < 2:
        return {compact} if compact else set()
    return {compact[index : index + 2] for index in range(len(compact) - 1)}


def _score(unit: TranscriptUnit, question_grams: set[str]) -> int:
    """问题 2-gram 与单元文本的重合个数（越长越可能顺手命中，按长度平方根惩罚）。"""
    text = re.sub(r"\s+", "", unit.text)
    if len(text) < 2:
        return 0
    grams = {text[index : index + 2] for index in range(len(text) - 1)}
    overlap = len(grams & question_grams)
    if overlap == 0:
        return 0
    return int(overlap * 100 / (len(grams) ** 0.5))


def retrieve(
    units: list[TranscriptUnit],
    question: str,
    *,
    budget_chars: int = FULL_CONTEXT_BUDGET,
    top_k: int = TOP_K,
) -> list[TranscriptUnit]:
    """选出送模型的转写单元；短转写全量返回，长转写按相关度筛选。"""
    if sum(len(unit.text) for unit in units) <= budget_chars:
        return list(units)

    grams = _bigrams(question)
    scored = sorted(units, key=lambda unit: _score(unit, grams), reverse=True)

    picked: list[TranscriptUnit] = []
    used = 0
    for unit in scored:
        if _score(unit, grams) <= 0:
            break  # 已按分数降序，后面只会更不相关
        if used + len(unit.text) > budget_chars:
            continue
        picked.append(unit)
        used += len(unit.text)
        if len(picked) >= top_k:
            break

    if not picked:
        # 无任何词面命中：均匀采样，至少给模型一点上下文
        step = max(1, len(units) // max(1, top_k))
        picked = units[::step][:top_k]

    picked.sort(key=lambda unit: (unit.start, unit.end))
    return picked


# ------------------------------------------------------------------ 读取


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _load_units(run_dir: Path) -> list[TranscriptUnit]:
    path = run_dir / "transcript.jsonl"
    if not path.is_file():
        return []
    try:
        return load_units(path)
    except (OSError, ValueError):
        return []


def _load_outline(run_dir: Path) -> Outline | None:
    path = run_dir / "outline.json"
    if not path.is_file():
        return None
    try:
        return load_outline(path)
    except (OSError, ValueError, TypeError):
        return None


# ------------------------------------------------------------------ 引用


def _citations(
    html: str, units: list[TranscriptUnit], outline: Outline | None
) -> list[Citation]:
    """从答案 HTML 的 data-source-units 反解出引用单元、时刻与所属章节。"""
    by_id = {unit.id: unit for unit in units}
    section_of: dict[str, tuple[str, int]] = {}
    if outline is not None:
        for index, section in enumerate(outline.sections, start=1):
            for unit_id in section.source_ids:
                section_of.setdefault(unit_id, (section.id, index))

    ordered: list[str] = []
    for attr in _SOURCE_UNITS.findall(html):
        for unit_id in attr.split(","):
            unit_id = unit_id.strip()
            if unit_id and unit_id in by_id and unit_id not in ordered:
                ordered.append(unit_id)

    citations: list[Citation] = []
    for unit_id in ordered:
        unit = by_id[unit_id]
        text = unit.text.strip()
        section = section_of.get(unit_id)
        citations.append(
            Citation(
                unit_id=unit_id,
                seconds=round(unit.start, 1),
                time_text=format_time(unit.start),
                snippet=text[:_SNIPPET_CHARS] + ("…" if len(text) > _SNIPPET_CHARS else ""),
                section_id=section[0] if section else None,
                section_index=section[1] if section else None,
            )
        )
    return citations


# ------------------------------------------------------------------ 问答


def answer_question(
    run_dir: Path,
    question: str,
    *,
    settings: Settings | None = None,
    client: LlmClient | None = None,
) -> Answer:
    """就一份报告提问；返回清洗后的 HTML 答案与来源引用。"""
    run_dir = Path(run_dir)
    question = (question or "").strip()
    if not question:
        raise UsageError("请输入问题")
    if len(question) > MAX_QUESTION_CHARS:
        raise UsageError(f"问题过长（上限 {MAX_QUESTION_CHARS} 字）")

    units = _load_units(run_dir)
    if not units:
        raise UsageError("该 run 没有可用的转写稿，无法问答")

    settings = settings or get_settings()
    settings.require_llm()
    outline = _load_outline(run_dir)

    selected = retrieve(units, question)
    llm = client or LlmClient(settings)
    raw = llm.complete(
        system=prompts.QA_SYSTEM,
        user=prompts.fill(
            prompts.QA_USER,
            question=question,
            context_md=units_to_markdown(selected),
        ),
        model=settings.llm_model_write,
    )

    answer_html, _violations = sanitize_section_html(
        raw, {unit.id for unit in units}, _QA_CLASSES
    )
    if not answer_html.strip():
        raise LlmError("模型未返回有效回答")

    usage = getattr(llm, "usage", None)
    return Answer(
        answer_html=answer_html,
        citations=_citations(answer_html, units, outline),
        usage_total=int(getattr(usage, "total", 0) or 0),
    )


# ------------------------------------------------------------------ 历史


def _now() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def append_history(run_dir: Path, question: str, answer: Answer) -> Path:
    """把一次问答追加到 `qa.jsonl`，便于复盘与重新打开面板时回放。"""
    path = Path(run_dir) / "qa.jsonl"
    record = {"ts": _now(), "question": question, **answer.to_dict()}
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def load_history(run_dir: Path) -> list[dict]:
    """读取 `qa.jsonl`；损坏行跳过，不影响其余记录。"""
    path = Path(run_dir) / "qa.jsonl"
    if not path.is_file():
        return []
    items: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            items.append(item)
    return items
