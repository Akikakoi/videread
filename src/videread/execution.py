"""外部命令执行：子进程调用的统一入口（对应开发文档 §6.3 下载 / §6.4 音频处理）。

下载（yt-dlp）与音频处理（ffmpeg / ffprobe）都要起外部进程，编码、超时与
"程序不存在"的处理完全一样。这里把这部分收敛成一处：

- 统一 `subprocess.run` 的参数（文本模式、utf-8、不抛 CalledProcessError）
- 把两类进程级故障归一化为调用方指定的分类异常（`DownloadError` / `AudioError`）

调用方仍负责构造命令与判断返回码；命令语义、失败文案留在各自模块里，
本层不理解 yt-dlp / ffmpeg 的任何细节。
"""

from __future__ import annotations

import subprocess
from typing import Callable

from .errors import VidereadError

#: 程序缺失时的文案构造：入参是命令的第一个元素
MissingMessage = Callable[[str], str]
#: 超时时的文案构造：入参是超时秒数与命令的第一个元素
TimeoutMessage = Callable[[float, str], str]


def run_command(
    cmd: list[str],
    *,
    timeout: float,
    error_cls: type[VidereadError],
    missing_message: MissingMessage,
    timeout_message: TimeoutMessage,
) -> subprocess.CompletedProcess[str]:
    """执行一条外部命令。

    只处理两类进程级故障，其余一律交给调用方看返回码：

    - 找不到可执行文件 → `error_cls(missing_message(program))`
    - 超过 `timeout` 秒 → `error_cls(timeout_message(timeout, program))`
    """
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise error_cls(missing_message(cmd[0])) from exc
    except subprocess.TimeoutExpired as exc:
        raise error_cls(timeout_message(timeout, cmd[0])) from exc