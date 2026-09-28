"""Web 控制台用例（全部离线）：报告库扫描、路由校验、任务流转与 SSE。

`pipeline.run` 一律 monkeypatch 掉，不联网、不调 ffmpeg / LLM。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from videread import failures
from videread.web import jobs, library
from videread.web.app import create_app

URL = "https://www.bilibili.com/video/BV1xx411c7mD"
RUN_ID = "BV1xx411c7mD-aaaaaaaa"
BAD_RUN_ID = "BV1xx411c7mD-bbbbbbbb"
REPORT_HTML = "<!DOCTYPE html><html><body><p>report</p></body></html>"


# --------------------------------------------------------------------- 工具


def make_run(root: Path, run_id: str, *, title: str = "示例视频", report: bool = True) -> Path:
    """构造一份最小可识别的 run 目录。"""
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "bvid": run_id.split("-")[0],
                "title": title,
                "uploader": "某UP主",
                "duration": 2137.0,
                "url": URL,
                "cover": "",
                "description": "",
                "subtitles": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (run_dir / "outline.json").write_text(
        json.dumps(
            {
                "title": title,
                "subtitle": "",
                "lead": "导语一段",
                "profile": "mechanism",
                "sections": [
                    {
                        "id": "s1",
                        "heading": "第一章",
                        "time_range": "00:00-01:00",
                        "intent": "讲清机制",
                        "source_ids": ["u0001", "u0002"],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    if report:
        (run_dir / "report.html").write_text(REPORT_HTML, encoding="utf-8")
    (run_dir / "run.trace.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"ts": "t", "stage": "download", "event": "start"}),
                json.dumps(
                    {
                        "ts": "t",
                        "stage": "download",
                        "event": "end",
                        "dur_ms": 4768,
                        "detail": {"cached": True},
                    }
                ),
                json.dumps(
                    {
                        "ts": "t",
                        "stage": "outline",
                        "event": "end",
                        "dur_ms": 1000,
                        "detail": {"tokens": 1234},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return run_dir


def wait_terminal(job: jobs.Job, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if job.terminal:
            return
        time.sleep(0.01)
    raise AssertionError(f"任务未在 {timeout}s 内结束：state={job.state}")


def fake_pipeline(report_body: str = REPORT_HTML, run_id: str = RUN_ID):
    """替代 pipeline.run：走一遍 progress 回调并落一份 report.html。"""

    def _run(
        url: str,
        *,
        mode: str = "standard",
        out_root: Path,
        use_cache: bool = True,
        keep_audio: bool = False,
        asr_backend: str | None = None,
        open_report: bool = False,
        progress=print,
    ) -> Path:
        progress("[1/7] 下载 ✓ meta.json  BV1xx411c7mD  时长 35:37")
        progress("[3/7] 转写 ✓ 412 段")
        run_dir = Path(out_root) / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        report = run_dir / "report.html"
        report.write_text(report_body, encoding="utf-8")
        progress("[7/7] 渲染 ✓ report.html  自包含检查通过")
        return report

    return _run


# ----------------------------------------------------------------- 报告库


def test_list_runs_skips_malformed_meta(tmp_path: Path):
    make_run(tmp_path, RUN_ID)
    broken = tmp_path / BAD_RUN_ID
    broken.mkdir()
    (broken / "meta.json").write_text("{ not json", encoding="utf-8")

    runs = library.list_runs(tmp_path)

    assert [item["run_id"] for item in runs] == [RUN_ID]
    assert runs[0]["title"] == "示例视频"
    assert runs[0]["sections"] == 1
    assert runs[0]["profile"] == "mechanism"
    assert runs[0]["report_url"] == f"/report/{RUN_ID}"


def test_list_runs_missing_root_returns_empty(tmp_path: Path):
    assert library.list_runs(tmp_path / "nope") == []


def test_trace_summary_orders_stages_and_totals(tmp_path: Path):
    make_run(tmp_path, RUN_ID)

    summary = library.trace_summary(tmp_path / RUN_ID / "run.trace.jsonl")

    assert [stage["name"] for stage in summary["stages"]] == ["download", "outline"]
    assert summary["stages"][0]["cached"] is True
    assert summary["total_ms"] == 5768
    assert summary["tokens"] == 1234


def test_run_detail_includes_sections_and_files(tmp_path: Path):
    make_run(tmp_path, RUN_ID)

    detail = library.run_detail(tmp_path, RUN_ID)

    assert detail is not None
    assert detail["sections_detail"][0]["heading"] == "第一章"
    assert detail["sections_detail"][0]["units"] == 2
    assert any(item["name"] == "report.html" for item in detail["files"])
    assert detail["lead"] == "导语一段"


# ------------------------------------------------------------- 路径穿越防护


@pytest.mark.parametrize(
    "run_id",
    ["", ".", "..", "../etc", "a/b", "a\\b", "..\\..\\windows", "/abs", "sneaky/../.."],
)
def test_safe_run_dir_rejects_traversal(tmp_path: Path, run_id: str):
    assert library.safe_run_dir(tmp_path, run_id) is None


def test_safe_run_dir_accepts_direct_child(tmp_path: Path):
    make_run(tmp_path, RUN_ID)
    assert library.safe_run_dir(tmp_path, RUN_ID) == (tmp_path / RUN_ID).resolve()


# ------------------------------------------------------------------- 路由


def test_index_and_static_are_served(tmp_path: Path):
    with TestClient(create_app(tmp_path)) as client:
        index = client.get("/")
        css = client.get("/static/styles.css")

    assert index.status_code == 200
    assert "videread" in index.text
    assert css.status_code == 200
    assert "--accent" in css.text


def test_api_runs_lists_existing_runs(tmp_path: Path):
    make_run(tmp_path, RUN_ID)
    with TestClient(create_app(tmp_path)) as client:
        response = client.get("/api/runs")

    assert response.status_code == 200
    body = response.json()
    assert body["out_root"] == str(tmp_path)
    assert [item["run_id"] for item in body["runs"]] == [RUN_ID]


def test_report_route_serves_html_and_404s_unknown(tmp_path: Path):
    make_run(tmp_path, RUN_ID)
    with TestClient(create_app(tmp_path)) as client:
        hit = client.get(f"/report/{RUN_ID}")
        miss = client.get("/report/BV1xx411c7mD-ffffffff")

    assert hit.status_code == 200
    assert "report" in hit.text
    assert miss.status_code == 404


def test_report_route_rejects_traversal(tmp_path: Path):
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    with TestClient(create_app(tmp_path)) as client:
        response = client.get("/report/..%2f..%2fsecret.txt")

    assert response.status_code != 200


def test_run_detail_route_404s_unknown(tmp_path: Path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/runs/BV1xx411c7mD-ffffffff").status_code == 404


# ------------------------------------------------------------------- 任务


def test_create_job_rejects_bad_input(tmp_path: Path):
    with TestClient(create_app(tmp_path)) as client:
        empty = client.post("/api/jobs", json={"url": "   "})
        bad_mode = client.post("/api/jobs", json={"url": URL, "mode": "epic"})

    assert empty.status_code == 400
    assert bad_mode.status_code == 400


def test_job_manager_runs_pipeline_and_emits_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(jobs, "pipeline_run", fake_pipeline())
    manager = jobs.JobManager(tmp_path)

    job = manager.submit(url=URL, mode="brief")
    wait_terminal(job)

    assert job.state == "done"
    assert job.exit_code == 0
    assert job.run_id == RUN_ID
    assert job.report_path is not None and job.report_path.is_file()
    assert job.snapshot()["report_url"] == f"/report/{RUN_ID}"

    kinds = [event["type"] for event in job._events]  # noqa: SLF001 - 用例内窥事件序列
    assert "stage" in kinds and "log" in kinds
    assert kinds[-1] == "done"
    assert job.stage_index == 7


def test_job_manager_maps_videread_error_to_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from videread.errors import AsrError

    def boom(*args, **kwargs):
        raise AsrError("转写失败：余额不足")

    monkeypatch.setattr(jobs, "pipeline_run", boom)
    manager = jobs.JobManager(tmp_path)

    job = manager.submit(url=URL)
    wait_terminal(job)

    assert job.state == "error"
    assert job.exit_code == 3
    assert "余额不足" in (job.error or "")
    assert job.hint == failures.hint(3)  # 文案统一来自 failures，前端不再自带码表
    assert job.snapshot()["error_hint"] == job.hint
    assert job.snapshot()["report_url"] is None


def test_job_manager_hint_for_unclassified_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """兜底分支不能复用退出码 1 的「参数错误」文案，否则会误导用户。"""

    def boom(*args, **kwargs):
        raise ValueError("内部炸了")

    monkeypatch.setattr(jobs, "pipeline_run", boom)
    manager = jobs.JobManager(tmp_path)

    job = manager.submit(url=URL)
    wait_terminal(job)

    assert job.state == "error"
    assert job.hint == "未归类异常，请查看运行日志"


def test_job_manager_serialises_concurrent_submits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    gate = threading.Event()
    inner = fake_pipeline()

    def gated(*args, **kwargs):
        gate.wait(5)
        return inner(*args, **kwargs)

    monkeypatch.setattr(jobs, "pipeline_run", gated)
    manager = jobs.JobManager(tmp_path)

    first = manager.submit(url=URL)
    try:
        with pytest.raises(jobs.JobBusy):
            manager.submit(url=URL)
    finally:
        gate.set()
    wait_terminal(first)

    assert first.state == "done"


def test_job_status_and_events_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(jobs, "pipeline_run", fake_pipeline())
    app = create_app(tmp_path)

    with TestClient(app) as client:
        created = client.post("/api/jobs", json={"url": URL})
        assert created.status_code == 200
        job_id = created.json()["job_id"]

        stream = client.get(f"/api/jobs/{job_id}/events")
        snapshot = client.get(f"/api/jobs/{job_id}")
        missing = client.get("/api/jobs/deadbeef")

    assert stream.status_code == 200
    assert "event: stage" in stream.text
    assert "event: log" in stream.text
    assert "event: done" in stream.text

    body = snapshot.json()
    assert body["state"] == "done"
    assert body["report_url"] == f"/report/{RUN_ID}"

    assert missing.status_code == 404


def test_events_replay_since_returns_tail_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(jobs, "pipeline_run", fake_pipeline())
    manager = jobs.JobManager(tmp_path)

    job = manager.submit(url=URL)
    wait_terminal(job)

    _, backlog = job.subscribe(since=len(job._events) - 1)  # noqa: SLF001 - 用例内窥
    assert [event["type"] for event in backlog] == ["done"]