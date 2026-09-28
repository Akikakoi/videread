"""execution：外部命令执行的失败归一化（离线，只跑本机 python 子进程）。"""

from __future__ import annotations

import sys

import pytest

from videread import execution
from videread.errors import EXIT_AUDIO_OR_ASR, EXIT_DOWNLOAD, AudioError, DownloadError

_MISSING = "videread-no-such-binary-xyz"


def _missing_message(program: str) -> str:
    return f"未安装：{program}"


def _timeout_message(seconds: float, program: str) -> str:
    return f"外部程序超时（{seconds:.0f}s）：{program}"


def test_run_command_returns_completed_process():
    result = execution.run_command(
        [sys.executable, "-c", "print('ok')"],
        timeout=30,
        error_cls=AudioError,
        missing_message=_missing_message,
        timeout_message=_timeout_message,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "ok"


def test_run_command_maps_missing_program_to_given_class():
    with pytest.raises(DownloadError) as excinfo:
        execution.run_command(
            [_MISSING],
            timeout=5,
            error_cls=DownloadError,
            missing_message=_missing_message,
            timeout_message=_timeout_message,
        )
    # 文案由调用方给，异常类型也由调用方定：本层不猜
    assert str(excinfo.value) == f"未安装：{_MISSING}"
    assert excinfo.value.exit_code == EXIT_DOWNLOAD


def test_run_command_maps_timeout_to_given_class():
    with pytest.raises(AudioError) as excinfo:
        execution.run_command(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            timeout=0.4,
            error_cls=AudioError,
            missing_message=_missing_message,
            timeout_message=_timeout_message,
        )
    assert "超时" in str(excinfo.value)
    assert excinfo.value.exit_code == EXIT_AUDIO_OR_ASR


def test_run_command_does_not_raise_on_nonzero_returncode():
    """返回码交给调用方判断，本层不把「命令失败」当异常。"""
    result = execution.run_command(
        [sys.executable, "-c", "import sys; sys.exit(3)"],
        timeout=30,
        error_cls=AudioError,
        missing_message=_missing_message,
        timeout_message=_timeout_message,
    )
    assert result.returncode == 3