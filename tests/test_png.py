"""长图导出用例（离线）：两趟截图、缓存、产物校验、端点与流水线接线。

真实调用 Edge / Chrome 的截图不在离线范围，浏览器行为全部以桩替代：
--dump-dom 返回带测高标记的 DOM，--screenshot 落地 PNG 魔数文件。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from videread import png as png_mod
from videread.config import get_settings
from videread.errors import PngError
from videread.web.app import create_app

URL = "https://www.bilibili.com/video/BV1xx411c7mD"

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake" * 64


def _settings() -> "Settings":  # noqa: F821
    return get_settings()


def _stub_browser(monkeypatch: pytest.MonkeyPatch, calls: list, dom: str = "<html><head><title>VRH:2400</title></head><body></body></html>") -> None:
    """伪造无头浏览器：dump-dom 回测高标记，screenshot 落地 PNG 魔数文件。"""

    def _run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if "--dump-dom" in cmd:
            return subprocess.CompletedProcess(cmd, 0, dom, "")
        for arg in cmd:
            if arg.startswith("--screenshot="):
                Path(arg.split("=", 1)[1]).write_bytes(_PNG_BYTES)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(png_mod.execution, "run_command", _run)


def _make_html(tmp_path: Path) -> Path:
    html = tmp_path / "report.html"
    html.write_text("<html><body>报告</body></html>", encoding="utf-8")
    return html


def test_export_png_two_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """第一趟测高、第二趟按测得高度开窗截图。"""
    html = _make_html(tmp_path)
    out = tmp_path / "report.png"
    calls: list = []
    _stub_browser(monkeypatch, calls)

    result = png_mod.export_png(html, out, _settings())

    assert result == out and out.is_file()
    assert len(calls) == 2
    assert "--dump-dom" in calls[0]
    screenshot_cmd = calls[1]
    assert any(arg.startswith("--screenshot=") for arg in screenshot_cmd)
    assert "--window-size=900,2400" in screenshot_cmd


def test_export_png_falls_back_without_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """测高标记缺失（注入被破坏 / 浏览器过老）时退回兜底高度而不是失败。"""
    html = _make_html(tmp_path)
    out = tmp_path / "report.png"
    calls: list = []
    _stub_browser(monkeypatch, calls, dom="")

    png_mod.export_png(html, out, _settings())

    assert "--window-size=900,16000" in calls[1]


def test_export_png_cache_skips_second_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    html = _make_html(tmp_path)
    out = tmp_path / "report.png"
    calls: list = []
    _stub_browser(monkeypatch, calls)

    png_mod.export_png(html, out, _settings())
    png_mod.export_png(html, out, _settings())
    assert len(calls) == 2  # 缓存命中，第二次不再调浏览器

    # HTML 更新后缓存失效，重新导出（PNG 在 HTML 之后生成，增量需明显大过生成间隔）
    import os

    stat = html.stat()
    os.utime(html, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000_000))
    png_mod.export_png(html, out, _settings())
    assert len(calls) == 4


def test_export_png_rejects_invalid_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    html = _make_html(tmp_path)

    def _run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--dump-dom" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "<title>VRH:2400</title>", "")
        for arg in cmd:
            if arg.startswith("--screenshot="):
                Path(arg.split("=", 1)[1]).write_bytes(b"not a png at all........")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(png_mod.execution, "run_command", _run)
    with pytest.raises(PngError, match="有效"):
        png_mod.export_png(html, tmp_path / "report.png", _settings())


def test_export_png_reports_missing_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    html = _make_html(tmp_path)
    monkeypatch.setattr(png_mod, "find_browser", lambda settings: None)

    with pytest.raises(PngError, match="PDF_BROWSER_BIN"):
        png_mod.export_png(html, tmp_path / "report.png", _settings())


# ---------------------------------------------------------------- Web 端点


def test_web_png_endpoint_uses_cached_export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from tests.test_web import make_run, RUN_ID

    make_run(tmp_path, RUN_ID)

    def _fake_export(html_path: Path, out_png: Path, settings: object = None) -> Path:
        out_png.write_bytes(_PNG_BYTES)
        return out_png

    monkeypatch.setattr("videread.web.app.png_mod.export_png", _fake_export)

    client = TestClient(create_app(tmp_path))
    response = client.get(f"/api/runs/{RUN_ID}/png")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert "attachment" in response.headers["content-disposition"]
    assert response.content.startswith(b"\x89PNG")


def test_web_png_endpoint_maps_failure_to_400(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from tests.test_web import make_run, RUN_ID

    make_run(tmp_path, RUN_ID)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise PngError("未找到 Edge / Chrome，无法导出长图")

    monkeypatch.setattr("videread.web.app.png_mod.export_png", _boom)

    client = TestClient(create_app(tmp_path))
    response = client.get(f"/api/runs/{RUN_ID}/png")
    assert response.status_code == 400
    assert "长图" in response.json()["detail"]


def test_web_png_endpoint_404_without_report(tmp_path: Path):
    from tests.test_web import make_run, RUN_ID

    make_run(tmp_path, RUN_ID, report=False)
    client = TestClient(create_app(tmp_path))
    response = client.get(f"/api/runs/{RUN_ID}/png")
    assert response.status_code == 404


# ---------------------------------------------------------------- 流水线接线


def test_pipeline_export_png_and_md_wires_after_render(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from videread import download, pipeline

    run_dir = tmp_path / download.make_run_id(URL, "BV1xx411c7mD")
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "bvid": "BV1xx411c7mD",
                "title": "t",
                "uploader": "u",
                "duration": 12.0,
                "url": URL,
                "cover": "",
                "description": "",
                "subtitles": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (run_dir / "audio.wav").write_text("fake", encoding="utf-8")
    (run_dir / "asr.raw.jsonl").write_text('{"start": 0.0, "end": 12.0, "text": "hi"}\n', encoding="utf-8")
    (run_dir / "transcript.jsonl").write_text(
        json.dumps({"id": "u0001", "start": 0.0, "end": 12.0, "text": "hi", "source": "asr"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (run_dir / "transcript.md").write_text("hi\n", encoding="utf-8")
    (run_dir / "outline.json").write_text(
        json.dumps(
            {
                "title": "t",
                "subtitle": "",
                "lead": "l",
                "profile": "mechanism",
                "sections": [
                    {"id": "s1", "heading": "h", "time_range": "00:00-00:12", "intent": "i", "source_ids": ["u0001"]}
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (run_dir / "sections").mkdir()
    (run_dir / "sections" / "s1.html").write_text("<p>正文</p>\n", encoding="utf-8")

    exported: list = []

    def _fake_export_png(html_path: Path, out_png: Path, settings: object = None) -> Path:
        exported.append(out_png)
        out_png.write_bytes(_PNG_BYTES)
        return out_png

    monkeypatch.setattr(pipeline.png_mod, "export_png", _fake_export_png)

    lines: list[str] = []
    report = pipeline.run(URL, out_root=tmp_path, export_png=True, export_md=True, progress=lines.append)

    assert report.is_file()
    assert exported and exported[0] == run_dir / "report.png"
    assert (run_dir / "report.png").is_file()
    assert (run_dir / "report.md").is_file()
    assert "已导出长图" in "\n".join(lines)
    assert "已导出 Markdown" in "\n".join(lines)
