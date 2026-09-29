"""模板填充与自包含检查（对应开发文档 §6.10 / §8.1）。

模板本身是完整 HTML，只做占位符的纯 `str.replace` 替换，**不做 HTML 转义**。
"""

from __future__ import annotations

import re
from datetime import datetime
from html import escape
from pathlib import Path

from .config import Settings
from .download import VideoMeta
from .errors import RenderError
from .report import Outline
from .transcript import TranscriptUnit

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

REQUIRED_PLACEHOLDERS = ("TITLE", "LEAD", "ATTRIBUTION", "BODY", "SOURCES")
OPTIONAL_PLACEHOLDERS = ("SUBTITLE", "VIDEO_DESCRIPTION")

_PLACEHOLDER = re.compile(r"\{\{([A-Z0-9_]+)\}\}")

# 可选占位符为空时整块删除（§8.1：空则整行 / 整块删除）
_OPTIONAL_BLOCKS = {
    "SUBTITLE": re.compile(r"[ \t]*<p class=\"subtitle\">\{\{SUBTITLE\}\}</p>[ \t]*\n?"),
    "VIDEO_DESCRIPTION": re.compile(
        r"[ \t]*<details class=\"meta-fold\">.*?</details>[ \t]*\n?", re.DOTALL
    ),
}

_TEMPLATES = {"standard": "report.html", "brief": "brief-report.html"}

# --- 模板已定义 class 的清单：用于剔除 LLM 自创的 class -----------------------
_STYLE_BLOCK = re.compile(r"<style[^>]*>(.*?)</style>", re.IGNORECASE | re.DOTALL)
_CSS_CLASS = re.compile(r"\.([A-Za-z][A-Za-z0-9_-]*)")
_CLASS_ATTR_IN_TEMPLATE = re.compile(r"class\s*=\s*\"([^\"]*)\"", re.IGNORECASE)

# --- 自包含检查：只拒绝外部资源，不拒绝指向原视频的可见链接 ------------------
_EXTERNAL_CSS = re.compile(r"<\s*link\b[^>]*\brel\s*=\s*[\"']?\s*stylesheet", re.IGNORECASE)
_SCRIPT_SRC = re.compile(r"<\s*script\b[^>]*\bsrc\s*=", re.IGNORECASE)
_EXTERNAL_MEDIA = re.compile(
    r"<\s*(?:img|source|video|audio)\b[^>]*\b(?:src|srcset)\s*=\s*[\"']?\s*https?://",
    re.IGNORECASE,
)
_CSS_IMPORT = re.compile(r"@import\s+(?:url\(\s*)?[\"']?\s*https?://", re.IGNORECASE)
_CSS_URL = re.compile(r"url\(\s*[\"']?\s*https?://", re.IGNORECASE)
_FONT_FACE = re.compile(r"@font-face", re.IGNORECASE)


def template_for(mode: str) -> Path:
    """按阅读模式定位模板文件。"""
    name = _TEMPLATES.get((mode or "").strip().lower())
    if name is None:
        raise RenderError(f"未知模式：{mode!r}（可选 standard / brief）")
    path = TEMPLATE_DIR / name
    if not path.is_file():
        raise RenderError(f"模板文件不存在：{path}")
    return path


def render_report(*, template: Path, ctx: dict[str, str], out: Path) -> None:
    """把 ctx 填入模板并写盘；必需占位符缺失时抛 RenderError（提示具体名称）。"""
    try:
        html = template.read_text(encoding="utf-8")
    except OSError as exc:
        raise RenderError(f"读取模板失败：{template}（{exc}）") from exc

    present = set(_PLACEHOLDER.findall(html))
    missing = [name for name in REQUIRED_PLACEHOLDERS if name not in present]
    if missing:
        raise RenderError(
            "模板缺少必需占位符："
            + "、".join(f"{{{{{name}}}}}" for name in missing)
        )

    text = html
    for name, pattern in _OPTIONAL_BLOCKS.items():
        value = str(ctx.get(name, "") or "").strip()
        if value:
            continue
        stripped, count = pattern.subn("", text)
        if count == 0:
            # 模板结构与预期不符时退化为清空占位符，避免留下空标签
            stripped = text.replace(f"{{{{{name}}}}}", "")
        text = stripped

    for name in (*REQUIRED_PLACEHOLDERS, *OPTIONAL_PLACEHOLDERS):
        text = text.replace(f"{{{{{name}}}}}", str(ctx.get(name, "") or ""))

    leftover = sorted(set(_PLACEHOLDER.findall(text)))
    if leftover:
        raise RenderError("模板存在未替换的占位符：" + "、".join(leftover))

    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        out.write_text(text, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise RenderError(f"写入报告失败：{out}（{exc}）") from exc


def template_classes(template: Path) -> set[str]:
    """收集模板已定义的 class 名（`<style>` 内的选择器 + 静态 HTML 的 class 属性）。

    供 `report.writer` 剔除模型自创的 class —— 未定义的 class 不会带来任何样式，
    留着只会让人误以为生效。
    """
    try:
        text = template.read_text(encoding="utf-8")
    except OSError as exc:
        raise RenderError(f"读取模板失败：{template}（{exc}）") from exc

    names: set[str] = set()
    for block in _STYLE_BLOCK.findall(text):
        names.update(_CSS_CLASS.findall(block))
    for attr in _CLASS_ATTR_IN_TEMPLATE.findall(text):
        names.update(attr.split())
    return names


def check_self_contained(html: str) -> list[str]:
    """扫描外部依赖，返回违规项描述列表；空列表表示自包含（§1.3 A2）。"""
    violations: list[str] = []

    def _flag(pattern: re.Pattern[str], description: str) -> None:
        match = pattern.search(html)
        if match:
            violations.append(f"{description}：{match.group(0).strip()[:120]}")

    if _FONT_FACE.search(html) and _CSS_URL.search(html):
        _flag(_CSS_URL, "外链字体（@font-face 引用远程字体文件）")

    _flag(_EXTERNAL_CSS, "外链样式表 <link rel=stylesheet>")
    _flag(_SCRIPT_SRC, "外链脚本 <script src=...>")
    _flag(_EXTERNAL_MEDIA, "外链图片 / 媒体")
    _flag(_CSS_IMPORT, "外链样式导入 @import http(s)://")
    _flag(_CSS_URL, "内联样式引用了外链资源 url(http(s)://...)")
    return violations


# --- 报告来源区块：平台元信息转义后注入，LLM 输出不转义（§6.10）---------------


def attribution(meta: VideoMeta) -> str:
    platform = "本地文件" if meta.bvid == "local" else "Bilibili"
    bits = [platform]
    if meta.uploader:
        bits.append(f"UP主：{escape(meta.uploader)}")
    if meta.title:
        bits.append(f"《{escape(meta.title)}》")
    url_text = escape(meta.url)
    return f"{' · '.join(bits)}<br><a href=\"{url_text}\">{url_text}</a>"


def sources(
    meta: VideoMeta,
    outline: Outline,
    units: list[TranscriptUnit],
    settings: Settings,
    *,
    mode: str,
    subtitle_first: bool,
) -> str:
    counts: dict[str, int] = {}
    for unit in units:
        counts[unit.source] = counts.get(unit.source, 0) + 1
    breakdown = " / ".join(f"{key} {value}" for key, value in sorted(counts.items()))
    asr_label = settings.asr_backend
    if asr_label == "dashscope":
        asr_label = f"dashscope / {settings.dashscope_model}"
    if subtitle_first and units and all(unit.source == "subtitle" for unit in units):
        # 本次完全由平台字幕建稿 = 没调用 ASR，报告里必须如实说明
        asr_label = "平台字幕（字幕优先，未调用 ASR）"
    generated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    url_text = escape(meta.url)
    rows = [
        f"原视频：<a href=\"{url_text}\">{url_text}</a>",
        f"阅读模式：{'Brief' if mode == 'brief' else 'Standard'}；信息结构：{outline.profile}",
        f"转写：{asr_label}；转写单元 {len(units)} 个（{breakdown or '无'}）",
        f"LLM：结构规划 {settings.llm_model_plan}，逐节写作 {settings.llm_model_write}",
        f"生成时间：{generated_at}",
        "正文中 data-source-units 属性指向 transcript.jsonl 的转写单元 id，可据此回溯原文。",
        "本报告由转写稿重组生成，可能存在转写或理解误差；关键信息请以原视频为准。",
    ]
    return "\n".join(f"<p>{row}</p>" for row in rows)


def context(
    meta: VideoMeta,
    outline: Outline,
    parts: list[str],
    units: list[TranscriptUnit],
    settings: Settings,
    *,
    mode: str,
    subtitle_first: bool,
) -> dict[str, str]:
    """组装模板上下文；LLM 输出按 §6.10 不转义，平台元信息转义后再注入。"""
    return {
        "TITLE": outline.title,
        "SUBTITLE": outline.subtitle,
        "LEAD": outline.lead,
        "ATTRIBUTION": attribution(meta),
        "VIDEO_DESCRIPTION": escape(meta.description) if meta.description else "",
        "BODY": "\n".join(parts),
        "SOURCES": sources(
            meta,
            outline,
            units,
            settings,
            mode=mode,
            subtitle_first=subtitle_first,
        ),
    }