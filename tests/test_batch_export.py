"""批量导出用例（离线）：ZIP 内容、文件名、带截图跳过、PDF 桩与参数校验。

真实调用浏览器打印 PDF 不在离线范围，PDF 以桩替代；Markdown 走真实转换。
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from videread.web import batch_export
from videread.web.app import create_app

URL = "https://www.bilibili.com/video/BV1xx411c7mD"
RUN_A = "BV1aa411c7mD-aaaaaaaa"
RUN_B = "BV1bb411c7mD-bbbbbbbb"
REPORT_HTML = "<!DOCTYPE html><html><body><h1>报告标题</h1><p>正文内容</p></body></html>"


def make_run(
    root: Path, run_id: str, title: str, *, report: bool = True, framed: bool = False
) -> Path:
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "bvid": run_id.split("-")[0],
                "title": title,
                "uploader": "某UP主",
                "duration": 600.0,
                "url": URL,
                "cover": "",
                "description": "",
                "subtitles": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    if report:
        (run_dir / "report.html").write_text(REPORT_HTML, encoding="utf-8")
    if framed:
        frames = run_dir / "frames"
        frames.mkdir()
        (frames / "s1.jpg").write_bytes(b"\xff\xd8\xff fake")
    return run_dir


def export(client: TestClient, run_ids: list[str], fmt: str = "md"):
    return client.post("/api/runs/export", json={"run_ids": run_ids, "format": fmt})


def zip_names(response) -> list[str]:
    return zipfile.ZipFile(io.BytesIO(response.content)).namelist()


def test_batch_export_markdown_zip(tmp_path: Path):
    make_run(tmp_path, RUN_A, "第一篇")
    make_run(tmp_path, RUN_B, "第二篇")

    with TestClient(create_app(tmp_path)) as client:
        response = export(client, [RUN_A, RUN_B])

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
    assert "videread-md-" in response.headers["content-disposition"]
    assert response.headers["x-exported"] == "2"

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert sorted(archive.namelist()) == ["第一篇.md", "第二篇.md"]
    assert "正文内容" in archive.read("第一篇.md").decode("utf-8")


def test_batch_export_sanitizes_and_dedupes_names(tmp_path: Path):
    """标题里的非法字符要清掉；同名报告补 run_id 前缀，不能互相覆盖。"""
    make_run(tmp_path, RUN_A, 'A/B: "C"?')
    make_run(tmp_path, RUN_B, 'A/B: "C"?')

    with TestClient(create_app(tmp_path)) as client:
        response = export(client, [RUN_A, RUN_B])

    assert response.status_code == 200
    assert sorted(zip_names(response)) == ["A B C (BV1bb411).md", "A B C.md"]


def test_batch_export_markdown_rejects_framed_runs(tmp_path: Path):
    """带截图的报告与单份导出同规则：Markdown 整批拒绝并点名，不静默丢文件。"""
    make_run(tmp_path, RUN_A, "带截图", framed=True)
    make_run(tmp_path, RUN_B, "纯文本")

    with TestClient(create_app(tmp_path)) as client:
        response = export(client, [RUN_A, RUN_B])

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "带截图" in detail
    assert "PDF" in detail


def test_batch_export_pdf_uses_exporter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """PDF 不受截图限制：带截图的期照常导出。"""
    make_run(tmp_path, RUN_A, "带截图", framed=True)
    make_run(tmp_path, RUN_B, "第二篇")
    printed: list[str] = []

    def fake_export(html_path: Path, out_pdf: Path, _settings=None) -> Path:
        printed.append(html_path.parent.name)
        out_pdf.write_bytes(b"%PDF-1.4 fake batch export")
        return out_pdf

    monkeypatch.setattr(batch_export.pdf_mod, "export_pdf", fake_export)

    with TestClient(create_app(tmp_path)) as client:
        response = export(client, [RUN_A, RUN_B], fmt="pdf")

    assert response.status_code == 200
    assert printed == [RUN_A, RUN_B]
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert sorted(archive.namelist()) == ["带截图.pdf", "第二篇.pdf"]
    assert archive.read("带截图.pdf").startswith(b"%PDF")


def test_batch_export_png_uses_exporter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    make_run(tmp_path, RUN_A, "带截图", framed=True)
    make_run(tmp_path, RUN_B, "第二篇")
    printed: list[str] = []

    def fake_export(html_path: Path, out_png: Path, _settings=None) -> Path:
        printed.append(html_path.parent.name)
        out_png.write_bytes(b"\x89PNG\r\n\x1a\nfake batch export")
        return out_png

    monkeypatch.setattr(batch_export.png_mod, "export_png", fake_export)

    with TestClient(create_app(tmp_path)) as client:
        response = export(client, [RUN_A, RUN_B], fmt="png")

    assert response.status_code == 200
    assert printed == [RUN_A, RUN_B]
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert sorted(archive.namelist()) == ["带截图.png", "第二篇.png"]
    assert archive.read("带截图.png").startswith(b"\x89PNG")


def test_batch_export_png_prefers_reader_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """配置了 self_base 时与单份导出同口径：截编辑风阅读页而不是 report.html。"""
    make_run(tmp_path, RUN_A, "第一篇")
    targets: list[str] = []

    def fake_reader(page_url: str, out_png: Path, _settings=None, **_kwargs) -> Path:
        targets.append(page_url)
        out_png.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        return out_png

    monkeypatch.setattr(batch_export.png_mod, "export_reader_png", fake_reader)

    app = create_app(tmp_path, self_base="http://127.0.0.1:8765")
    with TestClient(app) as client:
        response = export(client, [RUN_A], fmt="png")

    assert response.status_code == 200
    assert targets == [f"http://127.0.0.1:8765/read/{RUN_A}?png=1"]


def test_batch_export_rejects_empty_and_unknown_format(tmp_path: Path):
    with TestClient(create_app(tmp_path)) as client:
        empty = export(client, [])
        bad_format = export(client, [RUN_A], fmt="docx")

    assert empty.status_code == 400
    assert bad_format.status_code == 400


def test_batch_export_rejects_unknown_run_and_missing_report(tmp_path: Path):
    make_run(tmp_path, RUN_A, "还没渲染", report=False)
    with TestClient(create_app(tmp_path)) as client:
        unknown = export(client, ["BV1xx411c7mD-ffffffff"])
        no_report = export(client, [RUN_A])

    assert unknown.status_code == 404
    assert no_report.status_code == 404


def test_batch_export_caps_selection_size(tmp_path: Path):
    ids = [f"BV1aa411c7mD-{i:08d}" for i in range(batch_export.MAX_BATCH + 1)]
    with TestClient(create_app(tmp_path)) as client:
        response = export(client, ids)

    assert response.status_code == 400
    assert str(batch_export.MAX_BATCH) in response.json()["detail"]
