"""音视频处理：ffmpeg / ffprobe 封装（对应开发文档 §6.4）。"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import execution
from .config import Settings, get_settings
from .errors import AudioError

_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


def _require(binary: str | None, env_key: str, name: str) -> str:
    if binary:
        return binary
    raise AudioError(
        f"未找到 {name}。请执行 `winget install Gyan.FFmpeg`，"
        f"或把静态包解压到项目 bin/，或用环境变量 {env_key} 指定绝对路径。"
    )


def ffmpeg_bin(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    return _require(settings.ffmpeg, "FFMPEG_BIN", "ffmpeg")


def ffprobe_bin(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    return _require(settings.ffprobe, "FFPROBE_BIN", "ffprobe")


def _run(cmd: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    return execution.run_command(
        cmd,
        timeout=timeout,
        error_cls=AudioError,
        missing_message=lambda program: f"无法执行外部程序：{program}",
        timeout_message=lambda seconds, program: f"外部程序超时（{seconds:.0f}s）：{program}",
    )


def probe_duration(src: Path, settings: Settings | None = None) -> float:
    """返回媒体时长（秒）。"""
    result = _run(
        [
            ffprobe_bin(settings),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(src),
        ],
        timeout=60,
    )
    if result.returncode != 0:
        raise AudioError(f"ffprobe 读取时长失败：{src}\n{result.stderr.strip()}")
    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise AudioError(f"ffprobe 返回的时长无法解析：{result.stdout.strip()!r}") from exc
    if duration <= 0:
        raise AudioError(f"媒体时长异常：{duration}")
    return duration


def to_wav_16k_mono(src: Path, dest: Path, settings: Settings | None = None) -> None:
    """统一输出 pcm_s16le / 16000Hz / 单声道。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = _run(
        [
            ffmpeg_bin(settings),
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i",
            str(src),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            str(dest),
        ],
        timeout=3600,
    )
    if result.returncode != 0 or not dest.is_file():
        raise AudioError(f"ffmpeg 转码失败：{src}\n{result.stderr.strip()[-2000:]}")


def detect_silence(
    src: Path,
    *,
    noise_db: int = -35,
    min_silence: float = 0.6,
    settings: Settings | None = None,
) -> list[float]:
    """返回静音区中点列表（秒），供长音频切段使用。"""
    result = _run(
        [
            ffmpeg_bin(settings),
            "-hide_banner",
            "-nostats",
            "-i",
            str(src),
            "-af",
            f"silencedetect=noise={noise_db}dB:d={min_silence}",
            "-f",
            "null",
            "-",
        ],
        timeout=1800,
    )
    stderr = result.stderr or ""
    if result.returncode != 0 and "silence_start" not in stderr:
        raise AudioError(f"ffmpeg 静音检测失败：{src}\n{stderr.strip()[-2000:]}")

    mids: list[float] = []
    pending: float | None = None
    for line in stderr.splitlines():
        start_match = _SILENCE_START.search(line)
        if start_match:
            start = float(start_match.group(1))
            pending = max(0.0, start)
            continue
        end_match = _SILENCE_END.search(line)
        if end_match and pending is not None:
            end = float(end_match.group(1))
            if end > pending:
                mids.append((pending + end) / 2)
            pending = None
    return mids


def plan_chunks(
    duration: float,
    silences: list[float],
    *,
    limit: float,
) -> list[tuple[float, float]]:
    """按静音点把 [0, duration] 切成若干段，每段不超过 limit。

    找不到合适静音点时退回硬切，保证不会产出超长段。
    """
    if duration <= 0:
        raise AudioError(f"无法为时长 {duration} 规划切段")
    if duration <= limit:
        return [(0.0, duration)]

    points: list[float] = []
    cursor = 0.0
    while duration - cursor > limit:
        wanted = cursor + limit
        # 只接受落在 [cursor + limit/2, wanted] 的静音点，避免产出过短段
        candidates = [
            s for s in silences if cursor + limit / 2 <= s <= wanted and s > cursor
        ]
        cut = max(candidates) if candidates else wanted
        points.append(cut)
        cursor = cut

    bounds = [0.0, *points, duration]
    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]


def cut_segment(
    src: Path,
    dest: Path,
    start: float,
    end: float,
    settings: Settings | None = None,
) -> None:
    """无损切出一段音频（wav 拷贝，便于云 ASR 提交）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = _run(
        [
            ffmpeg_bin(settings),
            "-hide_banner",
            "-nostdin",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{max(0.0, end - start):.3f}",
            "-i",
            str(src),
            "-c",
            "copy",
            str(dest),
        ],
        timeout=1800,
    )
    if result.returncode != 0 or not dest.is_file():
        raise AudioError(f"ffmpeg 切段失败：{src} [{start}-{end}]\n{result.stderr.strip()[-1000:]}")