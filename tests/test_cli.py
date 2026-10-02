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

# ---------------------------------------------------------------- 批量模式


def test_cli_batch_runs_each_entry_and_collects_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """批量列表逐个顺序解析；失败不中断，最终退出码取第一个失败的错误码。"""
    import videread.cli as cli_mod
    from videread.errors import DownloadError

    list_file = tmp_path / "urls.txt"
    list_file.write_text(
        "# 注释行\n"
        "BV1xx411c7mD\n"
        "\n"
        "BV1yy411c7mE\n",
        encoding="utf-8",
    )

    calls: list[str] = []

    def fake_run(url, **_kwargs):
        calls.append(url)
        if url.endswith("BV1yy411c7mE"):
            raise DownloadError("模拟下载失败")
        return tmp_path / "report.html"

    monkeypatch.setattr(cli_mod, "run", fake_run)

    code = cli_mod.main([str(list_file), "--out", str(tmp_path)])

    assert calls == [
        "https://www.bilibili.com/video/BV1xx411c7mD",
        "https://www.bilibili.com/video/BV1yy411c7mE",
    ]
    assert code == 2  # 第一个失败的退出码


def test_cli_batch_empty_list_returns_usage_code(tmp_path: Path):
    list_file = tmp_path / "urls.txt"
    list_file.write_text("# 只有注释\n\n", encoding="utf-8")
    from videread.cli import main

    assert main([str(list_file), "--out", str(tmp_path)]) == 1


def test_cli_single_file_path_still_treated_as_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """非 .txt 的位置参数不进批量模式；不存在的 .txt 也按普通来源处理。"""
    import videread.cli as cli_mod

    calls: list[str] = []

    def fake_run(url, **_kwargs):
        calls.append(url)
        return tmp_path / "report.html"

    monkeypatch.setattr(cli_mod, "run", fake_run)

    # 不存在的 .txt：按普通来源走单任务（流水线会报文件不存在，这里被 mock 掉）
    assert cli_mod.main([str(tmp_path / "ghost.txt")]) == 0
    assert calls == [str(tmp_path / "ghost.txt")]
