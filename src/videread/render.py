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
OPTIONAL_PLACEHOLDERS = ("SUBTITLE", "VIDEO_DESCRIPTION", "TIMEMAP")

_PLACEHOLDER = re.compile(r"\{\{([A-Z0-9_]+)\}\}")

#: 模板共享片段：用例化注释引用，渲染前展开，产物仍是自包含 HTML
_INCLUDES = {
    "SHARED_BASE_CSS": "_shared/base.css",
    "SHARED_EXTERNAL_LINKS": "_shared/external-links.html",
    "SHARED_TS_JUMP": "_shared/ts-jump.html",
    "SHARED_FAVICON": "_shared/favicon.html",
}
_INCLUDE = re.compile(r"<!--#include:([A-Z0-9_]+)-->")

# 可选占位符为空时整块删除（§8.1：空则整行 / 整块删除）
_OPTIONAL_BLOCKS = {
    "SUBTITLE": re.compile(r"[ \t]*<p class=\"subtitle\">\{\{SUBTITLE\}\}</p>[ \t]*\n?"),
    "VIDEO_DESCRIPTION": re.compile(
        r"[ \t]*<details class=\"meta-fold\">.*?</details>[ \t]*\n?", re.DOTALL
    ),
    "TIMEMAP": re.compile(r"[ \t]*\{\{TIMEMAP\}\}[ \t]*\n?"),
}

#: 转写单元 id 形如 `u0001`（`transcript.build_units` 顺序生成，`merge_subtitles` 保持）
_UNIT_ID = re.compile(r"^u(\d+)$")

_TEMPLATES = {
    "standard": "report.html",
    "deep": "report.html",  # 深度精读：复用 standard 模板，差异只在写作篇幅
    "brief": "brief-report.html",
    "outline": "outline-report.html",
}

#: 报告来源区块里的模式标签
_MODE_LABELS = {"standard": "Standard", "deep": "Deep", "brief": "Brief", "outline": "Outline"}

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


def _expand_includes(text: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        name = _INCLUDES.get(match.group(1))
        if name is None:
            raise RenderError(f"未知模板片段：{match.group(1)}")
        path = TEMPLATE_DIR / name
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RenderError(f"读取模板片段失败：{path}（{exc}）") from exc

    return _INCLUDE.sub(_replace, text)


def template_for(mode: str) -> Path:
    """按阅读模式定位模板文件。"""
    name = _TEMPLATES.get((mode or "").strip().lower())
    if name is None:
        raise RenderError(
            f"未知模式：{mode!r}（可选 standard / deep / brief / outline）"
        )
    path = TEMPLATE_DIR / name
    if not path.is_file():
        raise RenderError(f"模板文件不存在：{path}")
    return path


def render_report(*, template: Path, ctx: dict[str, str], out: Path) -> None:
    """把 ctx 填入模板并写盘；必需占位符缺失时抛 RenderError（提示具体名称）。"""
    try:
        html = _expand_includes(template.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RenderError(f"读取模板失败：{template}（{exc}）") from exc

    present = set(_PLACEHOLDER.findall(html))
    missing = [
        name
        for name in (*REQUIRED_PLACEHOLDERS, *_CHROME_REQUIRED)
        if name not in present
    ]
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

    for name in (*REQUIRED_PLACEHOLDERS, *_CHROME_REQUIRED, *OPTIONAL_PLACEHOLDERS):
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
        text = _expand_includes(template.read_text(encoding="utf-8"))
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

#: 报告 chrome 文案（模板固定字符串 + 来源区块行标签）按语言翻译。
#: zh 保持历史文案；未收录的语言回落英文。
_CHROME = {
    "zh": {
        "doc_title": "视频阅读报告",
        "description_title": "视频简介",
        "sources_title": "来源与处理说明",
        "source_video": "原视频",
        "mode": "阅读模式",
        "structure": "信息结构",
        "transcript": "转写",
        "units": "转写单元",
        "llm": "LLM",
        "generated_at": "生成时间",
        "note_1": "正文中 data-source-units 属性指向 transcript.jsonl 的转写单元 id，可据此回溯原文。",
        "note_2": "本报告由转写稿重组生成，可能存在转写或理解误差；关键信息请以原视频为准。",
        "local_file": "本地文件",
        "uploader": "UP主",
        "subtitle_first": "平台字幕（字幕优先，未调用 ASR）",
        "no_units": "无",
    },
    "en": {
        "doc_title": "Video Reading Report",
        "description_title": "Video description",
        "sources_title": "Sources & processing notes",
        "source_video": "Source video",
        "mode": "Reading mode",
        "structure": "Structure profile",
        "transcript": "Transcript",
        "units": "transcript units",
        "llm": "LLM",
        "generated_at": "Generated at",
        "note_1": "The data-source-units attributes in the text point to transcript unit ids in transcript.jsonl for tracing back to the source.",
        "note_2": "This report is generated by restructuring the transcript; transcription or interpretation errors may exist. Refer to the original video for critical information.",
        "local_file": "Local file",
        "uploader": "Uploader",
        "subtitle_first": "Platform subtitles (subtitle-first, no ASR used)",
        "no_units": "none",
    },
    "ja": {
        "doc_title": "動画リーディングレポート",
        "description_title": "動画の説明",
        "sources_title": "出典と処理の説明",
        "source_video": "元動画",
        "mode": "閲読モード",
        "structure": "情報構造",
        "transcript": "文字起こし",
        "units": "文字起こしユニット",
        "llm": "LLM",
        "generated_at": "生成日時",
        "note_1": "本文中の data-source-units 属性は transcript.jsonl の転写ユニット id を指し、原文を遡れます。",
        "note_2": "本レポートは文字起こしの再構成により生成されており、転写や解釈の誤りが含まれる可能性があります。重要な情報は元動画をご確認ください。",
        "local_file": "ローカルファイル",
        "uploader": "投稿者",
        "subtitle_first": "プラットフォーム字幕（字幕優先、ASR 未使用）",
        "no_units": "なし",
    },
}

# 模板固定中文的三处 chrome 占位符：DOC_TITLE / DESC_TITLE / SOURCES_TITLE
# （必填，render_report 缺失会报错；三个模板同步加占位符）
_CHROME_REQUIRED = ("DOC_TITLE", "DESC_TITLE", "SOURCES_TITLE")


def _chrome(lang: str) -> dict[str, str]:
    lang = (lang or "zh").strip().lower()
    if lang in {"", "zh", "zh-cn", "zh-hans"}:
        return _CHROME["zh"]
    return _CHROME.get(lang) or _CHROME["en"]


def attribution(meta: VideoMeta, lang: str = "zh") -> str:
    chrome = _chrome(lang)
    platform = chrome["local_file"] if meta.bvid == "local" else "Bilibili"
    bits = [platform]
    if meta.uploader:
        bits.append(f"{chrome['uploader']}：{escape(meta.uploader)}")
    if meta.title:
        bits.append(f"《{escape(meta.title)}》")
    url_text = escape(meta.url)
    return (
        f"{' · '.join(bits)}<br>"
        f"<a href=\"{url_text}\" target=\"_blank\" rel=\"noopener\">{url_text}</a>"
    )


def sources(
    meta: VideoMeta,
    outline: Outline,
    units: list[TranscriptUnit],
    settings: Settings,
    *,
    mode: str,
    subtitle_first: bool,
    lang: str = "zh",
) -> str:
    chrome = _chrome(lang)
    counts: dict[str, int] = {}
    for unit in units:
        counts[unit.source] = counts.get(unit.source, 0) + 1
    breakdown = " / ".join(f"{key} {value}" for key, value in sorted(counts.items()))
    asr_label = settings.asr_backend
    if asr_label == "dashscope":
        asr_label = f"dashscope / {settings.dashscope_model}"
    if subtitle_first and units and all(unit.source == "subtitle" for unit in units):
        # 本次完全由平台字幕建稿 = 没调用 ASR，报告里必须如实说明
        asr_label = chrome["subtitle_first"]
    generated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    url_text = escape(meta.url)
    rows = [
        f"{chrome['source_video']}：<a href=\"{url_text}\" target=\"_blank\" rel=\"noopener\">{url_text}</a>",
        f"{chrome['mode']}：{_MODE_LABELS.get(mode, 'Standard')}；"
        f"{chrome['structure']}：{outline.profile}",
        f"{chrome['transcript']}：{asr_label}；{chrome['units']} {len(units)}"
        f"（{breakdown or chrome['no_units']}）",
        f"{chrome['llm']}：{settings.llm_model_plan} / {settings.llm_model_write}",
        f"{chrome['generated_at']}：{generated_at}",
        chrome["note_1"],
        chrome["note_2"],
    ]
    return "\n".join(f"<p>{row}</p>" for row in rows)


def timemap_script(units: list[TranscriptUnit]) -> str:
    """产出段落级时间戳跳转所需的紧凑时间映射（报告内联脚本，纯离线）。

    转写单元 id 由 `transcript.build_units` 顺序生成（`u0001` 起），
    `merge_subtitles` 也保持 id 不变，因此按数值序号即可还原每个 id 的起始秒：
    `window.__VTS[i-1]` 即 `u{i:04d}` 的 start 秒，`-1` 表示该序号缺号。

    本地文件（无平台字幕 / 无在线视频）不注入——`context()` 里按 bvid 判定。
    任何 id 不符预期时返回空串：宁可不跳转，也不产出会错位的时间。
    """
    if not units:
        return ""
    starts: dict[int, float] = {}
    max_index = 0
    for unit in units:
        match = _UNIT_ID.match(unit.id)
        if match is None:
            return ""
        index = int(match.group(1))
        starts[index] = unit.start
        max_index = max(max_index, index)
    if max_index == 0:
        return ""
    values = [
        f"{starts[index]:.1f}" if index in starts else "-1"
        for index in range(1, max_index + 1)
    ]
    return f"<script>window.__VTS=[{','.join(values)}];</script>"


def context(
    meta: VideoMeta,
    outline: Outline,
    parts: list[str],
    units: list[TranscriptUnit],
    settings: Settings,
    *,
    mode: str,
    subtitle_first: bool,
    lang: str = "zh",
) -> dict[str, str]:
    """组装模板上下文；LLM 输出按 §6.10 不转义，平台元信息转义后再注入。"""
    chrome = _chrome(lang)
    return {
        "TITLE": outline.title,
        "SUBTITLE": outline.subtitle,
        "LEAD": outline.lead,
        "ATTRIBUTION": attribution(meta, lang),
        "VIDEO_DESCRIPTION": escape(meta.description) if meta.description else "",
        "DOC_TITLE": chrome["doc_title"],
        "DESC_TITLE": chrome["description_title"],
        "SOURCES_TITLE": chrome["sources_title"],
        "BODY": "\n".join(parts),
        "TIMEMAP": "" if meta.bvid == "local" else timemap_script(units),
        "SOURCES": sources(
            meta,
            outline,
            units,
            settings,
            mode=mode,
            subtitle_first=subtitle_first,
            lang=lang,
        ),
    }