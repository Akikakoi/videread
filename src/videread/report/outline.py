"""结构规划：一次 LLM 调用产出 JSON 大纲（对应开发文档 §5.4 / §6.7）。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from ..config import Settings, get_settings
from ..download import VideoMeta
from ..errors import LlmError
from ..transcript import TranscriptUnit, format_time, units_to_markdown
from . import prompts
from .llm import LlmClient

PROFILES = ("mechanism", "procedure", "evidence", "argument", "narrative")
_DEFAULT_PROFILE = "argument"
_MAX_SECTIONS = 8
_MIN_SECTIONS = 1
_MAX_SOURCE_IDS = 40


@dataclass
class OutlineSection:
    id: str
    heading: str
    time_range: str
    intent: str
    source_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Outline:
    title: str
    subtitle: str
    lead: str
    profile: str
    sections: list[OutlineSection] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "subtitle": self.subtitle,
            "lead": self.lead,
            "profile": self.profile,
            "sections": [section.to_dict() for section in self.sections],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Outline":
        return cls(
            title=str(data.get("title", "")),
            subtitle=str(data.get("subtitle", "") or ""),
            lead=str(data.get("lead", "")),
            profile=str(data.get("profile", _DEFAULT_PROFILE)),
            sections=[
                OutlineSection(
                    id=str(raw.get("id", f"s{i + 1}")),
                    heading=str(raw.get("heading", "")),
                    time_range=str(raw.get("time_range", "")),
                    intent=str(raw.get("intent", "")),
                    source_ids=[str(x) for x in (raw.get("source_ids") or [])],
                )
                for i, raw in enumerate(data.get("sections") or [])
            ],
        )

    def source_ids(self) -> set[str]:
        return {unit_id for section in self.sections for unit_id in section.source_ids}


def write(outline: Outline, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(outline.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def load(path: Path) -> Outline:
    try:
        return Outline.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise LlmError(f"{path} 不是合法 outline.json：{exc}") from exc


def meta_summary(meta: VideoMeta) -> str:
    lines = [
        f"标题：{meta.title}",
        f"UP主：{meta.uploader or '未知'}",
        f"时长：{format_time(meta.duration)}",
        f"原视频：{meta.url}",
    ]
    if meta.description:
        description = meta.description.strip()
        lines.append(f"简介：{description[:500]}")
    return "\n".join(lines)


def validate(data: dict, units: list[TranscriptUnit]) -> Outline:
    """剔除非法 source_ids，重编号章节，并从转写单元回算真实 time_range。"""
    order = {unit.id: index for index, unit in enumerate(units)}
    valid_ids = set(order)
    sections: list[OutlineSection] = []

    for raw in data.get("sections") or []:
        if not isinstance(raw, dict):
            continue
        seen: list[str] = []
        for unit_id in raw.get("source_ids") or []:
            unit_id = str(unit_id)
            if unit_id in valid_ids and unit_id not in seen:
                seen.append(unit_id)
        if not seen:
            continue
        seen.sort(key=order.__getitem__)
        seen = seen[:_MAX_SOURCE_IDS]

        start = min(units[order[unit_id]].start for unit_id in seen)
        end = max(units[order[unit_id]].end for unit_id in seen)
        sections.append(
            OutlineSection(
                id=f"s{len(sections) + 1}",
                heading=str(raw.get("heading") or f"第 {len(sections) + 1} 节").strip(),
                time_range=f"{format_time(start)}-{format_time(end)}",
                intent=str(raw.get("intent") or "").strip(),
                source_ids=seen,
            )
        )
        if len(sections) >= _MAX_SECTIONS:
            break

    if len(sections) < _MIN_SECTIONS:
        raise LlmError("大纲没有任何一节含有合法的 source_ids，无法进入逐节写作")

    profile = str(data.get("profile") or "").strip().lower()
    if profile not in PROFILES:
        profile = _DEFAULT_PROFILE

    title = str(data.get("title") or "").strip()
    lead = str(data.get("lead") or "").strip()
    if not title:
        raise LlmError("大纲缺少 title")

    return Outline(
        title=title,
        subtitle=str(data.get("subtitle") or "").strip(),
        lead=lead,
        profile=profile,
        sections=sections,
    )


def plan_outline(
    meta: VideoMeta,
    units: list[TranscriptUnit],
    *,
    mode: str = "standard",
    client: LlmClient | None = None,
    settings: Settings | None = None,
    progress: Callable[[str], None] | None = None,
) -> Outline:
    """规划章节结构；JSON 非法或校验失败时回灌报错重试，最多 2 次（共 3 次调用）。"""
    if not units:
        raise LlmError("转写单元为空，无法规划结构")
    settings = settings or get_settings()
    client = client or LlmClient(settings)
    progress = progress or (lambda _msg: None)

    base_user = prompts.fill(
        prompts.OUTLINE_USER,
        meta=meta_summary(meta),
        transcript_md=units_to_markdown(units),
    )

    user = base_user
    raw = ""
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            data, raw = client.complete_json(
                system=prompts.OUTLINE_SYSTEM,
                user=user,
                model=settings.llm_model_plan,
            )
            return validate(data, units)
        except LlmError as exc:
            last_error = exc
            if attempt == 2:
                break
            progress(f"大纲输出不可用，回灌报错重试（第 {attempt + 2} 次）")
            user = prompts.fill(
                prompts.OUTLINE_REPAIR_USER,
                error=str(exc)[:1500],
                raw=raw[:4000],
            )
    raise LlmError(f"结构规划失败（重试已耗尽）：{last_error}")