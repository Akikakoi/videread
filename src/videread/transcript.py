"""转写规范化：ASR 分段 → 转写单元（对应开发文档 §5.3 / §6.6）。

转写是唯一事实来源：本模块只做合并、切分与来源标记，不改写文字内容。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .asr.base import AsrSegment
from .config import TRANSCRIPT_MAX_CHARS
from .errors import VidereadError

# 句末标点优先在此断句；未命中时退化到逗号类停顿
_SENTENCE_END = "。！？!?；;"
_CLAUSE_END = "，,、：:"
_ALL_PUNCT = _SENTENCE_END + _CLAUSE_END

# 转写单元派生规则版本。build_units / parse_srt 的时间轴或拼接语义变化时
# 递增；pipeline 发现 run 里的旧 transcript.jsonl 版本不同就重建一次
# （只重算本地文本，不重新调用 ASR）。
TRANSCRIPT_FORMAT_VERSION = 2


@dataclass(frozen=True)
class TranscriptUnit:
    """规范化后的最小引用单位（§5.3）。"""

    id: str
    start: float
    end: float
    text: str
    source: str = "asr"


def format_time(seconds: float) -> str:
    """秒 → `mm:ss`（超过 1 小时为 `h:mm:ss`）。"""
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _is_cjk(char: str) -> bool:
    """是否属于中日韩文字（这些语言相邻文字之间不需要空格）。"""
    code = ord(char)
    return (
        0x3040 <= code <= 0x30FF      # 平假名 / 片假名
        or 0x3400 <= code <= 0x4DBF   # 扩展 A
        or 0x4E00 <= code <= 0x9FFF   # 基本汉字
        or 0xAC00 <= code <= 0xD7AF   # 韩文音节
        or 0xF900 <= code <= 0xFAFF   # 兼容汉字
    )


def _join_parts(parts: list[str]) -> str:
    """拼接文本片段：中日韩相邻不加空格，拉丁文字之间补一个空格。

    ASR 分段与字幕 cue 通常各自已去掉首尾空白，直接 `"".join` 会把英文
    "Hello." / "World" 粘成 "Hello.World"。
    """
    out = ""
    for part in parts:
        if not part:
            continue
        if not out:
            out = part
            continue
        if out[-1].isspace() or part[0].isspace() or _is_cjk(out[-1]) or _is_cjk(part[0]):
            out += part
        else:
            out += " " + part
    return out


def _split_text(text: str, max_chars: int) -> list[str]:
    """按标点把长文本切成不超过 max_chars 的片段；无标点可依时硬切。"""
    parts: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if len(buf) < max_chars:
            continue
        cut = max((buf.rfind(p) for p in _ALL_PUNCT), default=-1)
        if cut >= max_chars // 2:
            parts.append(buf[: cut + 1])
            buf = buf[cut + 1 :]
        else:
            parts.append(buf)
            buf = ""
    if buf:
        parts.append(buf)
    return parts


def _split_with_time(
    text: str, start: float, end: float, max_chars: int
) -> list[tuple[str, float, float]]:
    """切分文本，并按字符数比例分配时间，保证时间戳单调不减。"""
    parts = _split_text(text, max_chars)
    if len(parts) <= 1:
        return [(text, start, end)]

    total_chars = sum(len(p) for p in parts) or 1
    span = max(0.0, end - start)
    pieces: list[tuple[str, float, float]] = []
    cursor = start
    for index, part in enumerate(parts):
        piece_end = end if index == len(parts) - 1 else cursor + span * (len(part) / total_chars)
        piece_end = max(cursor, piece_end)
        pieces.append((part, cursor, piece_end))
        cursor = piece_end
    return pieces


def build_units(
    segments: list[AsrSegment], *, max_chars: int = TRANSCRIPT_MAX_CHARS
) -> list[TranscriptUnit]:
    """合并过短的 ASR 分段、切分过长的分段，产出连续编号的转写单元。"""
    if max_chars < 16:
        raise VidereadError(f"max_chars 过小：{max_chars}")

    pieces: list[tuple[str, float, float]] = []
    cursor_end = 0.0
    for segment in segments:
        text = re.sub(r"\s+", " ", segment.text or "").strip()
        if not text:
            continue
        end = max(segment.end, segment.start)
        for piece_text, start, piece_end in _split_with_time(
            text, segment.start, end, max_chars
        ):
            # ASR 偶尔给出互相重叠的分段（尤其尾部幻觉会连续输出同一起止的
            # 多条）。跨段钳制起点，保证单元时间轴单调不重叠。
            start = max(start, cursor_end)
            piece_end = max(piece_end, start)
            cursor_end = piece_end
            pieces.append((piece_text, start, piece_end))

    units: list[TranscriptUnit] = []
    buffer_text: list[str] = []
    buffer_chars = 0
    buffer_start = 0.0
    buffer_end = 0.0

    def flush() -> None:
        nonlocal buffer_text, buffer_chars
        if not buffer_text:
            return
        units.append(
            TranscriptUnit(
                id=f"u{len(units) + 1:04d}",
                start=round(buffer_start, 1),
                end=round(max(buffer_end, buffer_start), 1),
                text=_join_parts(buffer_text),
            )
        )
        buffer_text = []
        buffer_chars = 0

    for text, start, end in pieces:
        if buffer_text and buffer_chars + len(text) > max_chars:
            flush()
        if not buffer_text:
            buffer_start = start
        buffer_text.append(text)
        buffer_chars += len(text)
        buffer_end = end
        # 已接近容量且句意完整时提前断句，提升可读性
        if buffer_chars >= max_chars * 0.6 and text[-1:] in _SENTENCE_END:
            flush()

    flush()
    return units


def _srt_time_to_seconds(raw: str) -> float:
    match = re.match(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})", raw.strip())
    if not match:
        raise VidereadError(f"无法解析 SRT 时间戳：{raw!r}")
    hours, minutes, secs, millis = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + int(secs) + int(millis.ljust(3, "0")) / 1000


def parse_srt(path: Path) -> list[tuple[float, float, str]]:
    """解析 SRT 为 (start, end, text) 列表。"""
    cues: list[tuple[float, float, str]] = []
    content = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    for block in re.split(r"\n{2,}", content):
        lines = [line.strip() for line in block.strip().splitlines() if line.strip()]
        if not lines:
            continue
        time_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if time_index is None:
            continue
        left, _, right = lines[time_index].partition("-->")
        # 多行 cue 按语言拼接：中日韩不加空格，拉丁文字之间补空格，
        # 否则 "Hello\nworld" 会粘成 "Helloworld"
        text = _join_parts(
            [line.strip() for line in lines[time_index + 1 :] if line.strip()]
        )
        if not text:
            continue
        cues.append((_srt_time_to_seconds(left), _srt_time_to_seconds(right), text))
    return cues


def segments_from_srt(path: Path) -> list[AsrSegment]:
    """字幕优先：把 SRT 直接转成转写分段，跳过 ASR（§6.3）。"""
    return [
        AsrSegment(start=start, end=max(end, start), text=text)
        for start, end, text in parse_srt(path)
    ]


def _covered_texts(
    start: float, end: float, cues: list[tuple[float, float, str]]
) -> tuple[float, list[str]]:
    """某个时间段与字幕的总重叠时长，以及重叠到的字幕文本。"""
    covered = 0.0
    texts: list[str] = []
    for cue_start, cue_end, cue_text in cues:
        overlap = min(end, cue_end) - max(start, cue_start)
        if overlap <= 0:
            continue
        covered += overlap
        texts.append(cue_text)
    return covered, texts


def subtitle_coverage(srt: Path, duration: float) -> float:
    """字幕覆盖的视频时长占比（0~1）。

    只按时间轴计算，不需要 ASR 结果即可判断，因此能在下载音频、调用 ASR
    之前就判定这份字幕是否完整。阈值取 0.5 偏保守：宁可白跑一次 ASR，
    也不要用零星字幕生成一份大面积缺内容的报告。
    """
    cues = parse_srt(srt)
    if not cues or duration <= 0:
        return 0.0
    covered = sum(max(0.0, end - start) for start, end, _ in cues)
    return min(1.0, covered / duration)


def merge_subtitles(units: list[TranscriptUnit], srt: Path) -> list[TranscriptUnit]:
    """有平台字幕时，用字幕文本替换同时间段 ASR 文本，`source` 标为 subtitle。

    单元 id 与时间戳保持不变（id 永不重编号）。
    """
    cues = parse_srt(srt)
    if not cues:
        return list(units)

    merged: list[TranscriptUnit] = []
    for unit in units:
        covered, texts = _covered_texts(unit.start, unit.end, cues)
        span = max(1e-6, unit.end - unit.start)
        if texts and covered / span >= 0.5:
            merged.append(
                TranscriptUnit(
                    id=unit.id,
                    start=unit.start,
                    end=unit.end,
                    text=_join_parts(
                        [re.sub(r"\s+", " ", chunk).strip() for chunk in texts]
                    ),
                    source="subtitle",
                )
            )
        else:
            merged.append(unit)
    return merged


def write_jsonl(units: list[TranscriptUnit], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for unit in units:
            fh.write(
                json.dumps(
                    {
                        "id": unit.id,
                        "start": round(unit.start, 1),
                        "end": round(unit.end, 1),
                        "text": unit.text,
                        "source": unit.source,
                    },
                    ensure_ascii=False,
                )
            )
            fh.write("\n")


def load_units(path: Path) -> list[TranscriptUnit]:
    units: list[TranscriptUnit] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                units.append(
                    TranscriptUnit(
                        id=str(row["id"]),
                        start=float(row["start"]),
                        end=float(row["end"]),
                        text=str(row["text"]),
                        source=str(row.get("source", "asr")),
                    )
                )
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise VidereadError(f"{path} 第 {lineno} 行不是合法转写单元：{exc}") from exc
    return units


def units_to_markdown(units: list[TranscriptUnit]) -> str:
    """把给定单元渲染成 markdown 片段，供 prompt 注入与落盘复用。"""
    return "\n\n".join(
        f"## {unit.id} [{format_time(unit.start)}-{format_time(unit.end)}]\n{unit.text}"
        for unit in units
    )


def write_markdown(units: list[TranscriptUnit], path: Path) -> None:
    """人 / 模型可读形态（§5.3）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(units_to_markdown(units) + "\n", encoding="utf-8", newline="\n")