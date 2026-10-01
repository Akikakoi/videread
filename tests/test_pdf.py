"""PDF 导出用例（离线）：浏览器探测、缓存、产物校验、端点与流水线接线。

真实调用 Edge / Chrome 的打印不在离线范围，浏览器行为全部以桩替代。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from videread import pdf as pdf_mod
from videread.config import Settings, get_settings
from videread.errors import PdfError
from videread.web.app import create_app

URL = "https://www.bilibili.com/video/BV1xx411c7mD"


def _settings(pdf_browser: str | None = None) -> Settings:
    return get_settings(pdf_browser=pdf_browser)


def test_find_browser_prefers_explicit_and_validates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PDF_BROWSER_BIN 优先；指向不存在的文件时视为无效继续回退。"""
    fake = tmp_path / "browser.exe"
    fake.write_bytes(b"fake")
    assert pdf_mod.find_browser(_settings(str(fake))) == str(fake)

    missing = tmp_path / "nope.exe"
    monkeypatch.setattr(pdf_mod, "_CANDIDATES", ())
    monkeypatch.setattr(pdf_mod.shutil, "which", lambda _name: None)
    assert pdf_mod.find_browser(_settings(str(missing))) is None


def test_find_browser_falls_back_to_known_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = tmp_path / "msedge.exe"
    candidate.write_bytes(b"fake")
    monkeypatch.setattr(pdf_mod, "_CANDIDATES", (str(candidate),))
    monkeypatch.setattr(pdf_mod.shutil, "which", lambda _name: None)
    assert pdf_mod.find_browser(_settings()) == str(candidate)


def _stub_browser(monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    """伪造无头浏览器：验证命令行参数并产出合法 PDF 魔数文件。"""

    def _run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        for arg in cmd:
            if arg.startswith("--print-to-pdf="):
                Path(arg.split("=", 1)[1]).write_bytes(b"%PDF-1.4 " + b"fake" * 32)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(pdf_mod.execution, "run_command", _run)


def test_expand_details_adds_open_attribute() -> None:
    """PDF / 长图静态渲染没有点击交互，折叠块必须强制展开（幂等）。"""
    html = (
        '<html><body>'
        '<details class="meta-fold"><summary>视频简介</summary><div>简介</div></details>'
        '<details class="sources" open><summary>来源</summary><div>来源</div></details>'
        '</body></html>'
    )
    expanded = pdf_mod.expand_details(html)
    assert '<details open class="meta-fold">' in expanded
    # 已有 open 的不重复加
    assert expanded.count("open") == 2  # 属性各出现一次，无叠加
    assert pdf_mod.expand_details(expanded) == expanded


def test_export_pdf_builds_headless_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    html = tmp_path / "report.html"
    html.write_text("<html><body>报告</body></html>", encoding="utf-8")
    out = tmp_path / "report.pdf"
    calls: list = []
    _stub_browser(monkeypatch, calls)

    result = pdf_mod.export_pdf(html, out, _settings())

    assert result == out and out.is_file()
    cmd = calls[0]
    assert "--headless" in cmd and "--no-pdf-header-footer" in cmd
    assert any(arg.startswith("--print-to-pdf=") for arg in cmd)
    assert cmd[-1].startswith("file:///")


def test_export_pdf_cache_skips_second_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    html = tmp_path / "report.html"
    html.write_text("<html><body>报告</body></html>", encoding="utf-8")
    out = tmp_path / "report.pdf"
    calls: list = []
    _stub_browser(monkeypatch, calls)

    pdf_mod.export_pdf(html, out, _settings())
    pdf_mod.export_pdf(html, out, _settings())
    assert len(calls) == 1

    # HTML 更新后缓存失效，重新导出（PDF 在 HTML 之后生成，mtime 天然更新，
    # 增量需明显大于两者生成间隔才能翻转比较结果）
    import os

    stat = html.stat()
    os.utime(html, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000_000))
    pdf_mod.export_pdf(html, out, _settings())
    assert len(calls) == 2


def test_export_pdf_rejects_invalid_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    html = tmp_path / "report.html"
    html.write_text("<html></html>", encoding="utf-8")

    def _run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        for arg in cmd:
            if arg.startswith("--print-to-pdf="):
                Path(arg.split("=", 1)[1]).write_bytes(b"not a pdf at all........")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(pdf_mod.execution, "run_command", _run)
    with pytest.raises(PdfError, match="有效"):
        pdf_mod.export_pdf(html, tmp_path / "report.pdf", _settings())


def test_export_pdf_reports_missing_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    html = tmp_path / "report.html"
    html.write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(pdf_mod, "_CANDIDATES", ())
    monkeypatch.setattr(pdf_mod.shutil, "which", lambda _name: None)

    with pytest.raises(PdfError, match="PDF_BROWSER_BIN"):
        pdf_mod.export_pdf(html, tmp_path / "report.pdf", _settings())


# ---------------------------------------------------------------- Web 端点


def test_web_pdf_endpoint_uses_cached_export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from tests.test_web import make_run, RUN_ID, REPORT_HTML

    make_run(tmp_path, RUN_ID)
    generated: list = []

    def _fake_export(html_path: Path, out_pdf: Path, settings: object = None) -> Path:
        generated.append(html_path)
        out_pdf.write_bytes(b"%PDF-1.4 from web")
        return out_pdf

    monkeypatch.setattr("videread.web.app.pdf_mod.export_pdf", _fake_export)

    client = TestClient(create_app(tmp_path))
    response = client.get(f"/api/runs/{RUN_ID}/pdf")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "attachment" in response.headers["content-disposition"]
    assert response.content.startswith(b"%PDF-1.4")
    assert len(generated) == 1


def test_web_pdf_endpoint_maps_failure_to_400(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from tests.test_web import make_run, RUN_ID

    make_run(tmp_path, RUN_ID)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise PdfError(
            "未找到 Edge / Chrome，无法导出 PDF。"
            "请安装 Microsoft Edge，或用 PDF_BROWSER_BIN 指定浏览器路径"
        )

    monkeypatch.setattr("videread.web.app.pdf_mod.export_pdf", _boom)

    client = TestClient(create_app(tmp_path))
    response = client.get(f"/api/runs/{RUN_ID}/pdf")
    assert response.status_code == 400
    assert "PDF_BROWSER_BIN" in response.json()["detail"]


def test_web_pdf_endpoint_404_without_report(tmp_path: Path):
    from tests.test_web import make_run, RUN_ID

    make_run(tmp_path, RUN_ID, report=False)
    client = TestClient(create_app(tmp_path))
    response = client.get(f"/api/runs/{RUN_ID}/pdf")
    assert response.status_code == 404


# ---------------------------------------------------------------- 流水线接线


def test_pipeline_export_pdf_wires_after_render(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
    for name, content in {
        "audio.wav": "fake",
        "asr.raw.jsonl": '{"start": 0.0, "end": 12.0, "text": "hi"}\n',
    }.items():
        (run_dir / name).write_text(content, encoding="utf-8")
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
    monkeypatch.setattr(
        pipeline.pdf_mod,
        "export_pdf",
        lambda html_path, out_pdf, settings=None: (
            exported.append(out_pdf),
            out_pdf.write_bytes(b"%PDF-1.4 fake"),
            out_pdf,
        )[2],
    )

    lines: list[str] = []
    report = pipeline.run(URL, out_root=tmp_path, export_pdf=True, progress=lines.append)

    assert report.is_file()
    assert exported and exported[0] == run_dir / "report.pdf"
    assert (run_dir / "report.pdf").is_file()
    assert "已导出 PDF" in "\n".join(lines)
