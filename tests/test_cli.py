"""§6.1：CLI 参数与退出码用例（离线）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from videread.cli import main

URL = "https://www.bilibili.com/video/BV1xx411c7mD"


def test_cli_missing_local_file_returns_usage_code(tmp_path: Path):
    assert main([str(tmp_path / "not-here.m4a"), "--out", str(tmp_path)]) == 1


def test_cli_invalid_mode_exits_with_usage_code():
    with pytest.raises(SystemExit) as excinfo:
        main([URL, "--mode", "epic"])
    assert excinfo.value.code == 1