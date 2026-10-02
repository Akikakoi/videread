"""Web 控制台用例（全部离线）：报告库扫描、路由校验、任务流转与 SSE。

`pipeline.run` 一律 monkeypatch 掉，不联网、不调 ffmpeg / LLM。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from videread import download, failures
from videread.errors import DownloadError
from videread.web import jobs, library, probe
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
        frames: bool | None = None,
        fresh_dir: bool = False,
        cancel_check=None,
        progress=print,
    ) -> Path:
        progress("[1/8] 下载 ✓ meta.json  BV1xx411c7mD  时长 35:37")
        progress("[3/8] 转写 ✓ 412 段")
        run_dir = Path(out_root) / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        report = run_dir / "report.html"
        report.write_text(report_body, encoding="utf-8")
        progress("[8/8] 渲染 ✓ report.html  自包含检查通过")
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


def test_list_runs_cache_invalidates_on_new_report(tmp_path: Path):
    """条目缓存按产物 mtime 失效：补生成 report 后列表要立即反映新状态。"""
    make_run(tmp_path, RUN_ID, report=False)
    runs = library.list_runs(tmp_path)
    assert runs[0]["has_report"] is False

    (tmp_path / RUN_ID / "report.html").write_text(REPORT_HTML, encoding="utf-8")
    runs = library.list_runs(tmp_path)
    assert runs[0]["has_report"] is True
    assert runs[0]["report_url"] == f"/report/{RUN_ID}"


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
    # 控制台「查看原视频」依赖列表里带出原视频链接
    assert body["runs"][0]["url"] == URL


def test_report_route_serves_html_and_404s_unknown(tmp_path: Path):
    make_run(tmp_path, RUN_ID)
    with TestClient(create_app(tmp_path)) as client:
        hit = client.get(f"/report/{RUN_ID}")
        miss = client.get("/report/BV1xx411c7mD-ffffffff")

    assert hit.status_code == 200
    assert "report" in hit.text
    assert miss.status_code == 404


def test_markdown_export_downloads_report(tmp_path: Path):
    make_run(tmp_path, RUN_ID, title="示例视频")
    with TestClient(create_app(tmp_path)) as client:
        response = client.get(f"/api/runs/{RUN_ID}/markdown")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    # 下载文件名：ASCII 回退 + RFC 5987 的中文标题
    assert f'filename="{RUN_ID}.md"' in response.headers["content-disposition"]
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    assert "report" in response.text


def test_markdown_export_404s_without_report(tmp_path: Path):
    make_run(tmp_path, RUN_ID, report=False)
    with TestClient(create_app(tmp_path)) as client:
        no_report = client.get(f"/api/runs/{RUN_ID}/markdown")
        unknown = client.get("/api/runs/BV1xx411c7mD-ffffffff/markdown")

    assert no_report.status_code == 404
    assert unknown.status_code == 404


def test_report_route_rejects_traversal(tmp_path: Path):
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    with TestClient(create_app(tmp_path)) as client:
        response = client.get("/report/..%2f..%2fsecret.txt")

    assert response.status_code != 200


def test_run_detail_route_404s_unknown(tmp_path: Path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/runs/BV1xx411c7mD-ffffffff").status_code == 404


# ------------------------------------------------------------- 删除记录


def test_delete_run_removes_directory(tmp_path: Path):
    make_run(tmp_path, RUN_ID)
    with TestClient(create_app(tmp_path)) as client:
        response = client.delete(f"/api/runs/{RUN_ID}")

    assert response.status_code == 200
    assert response.json() == {"deleted": RUN_ID}
    assert not (tmp_path / RUN_ID).exists()
    # 删除后列表立即反映状态（条目缓存按 mtime 失效，不残留已删目录）
    assert client.get("/api/runs").json()["runs"] == []


def test_delete_run_404s_unknown(tmp_path: Path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.delete("/api/runs/BV1xx411c7mD-ffffffff").status_code == 404


def test_delete_run_rejects_traversal(tmp_path: Path):
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    with TestClient(create_app(tmp_path)) as client:
        assert client.delete("/api/runs/..%2f..%2fsecret.txt").status_code != 200
    assert (tmp_path / "secret.txt").is_file()


# ------------------------------------------------------------------- 预检


def _fake_meta(duration: float, title: str = "示例视频"):
    def _fetch(url, settings=None, *, enforce_limit=True):
        return download.VideoMeta(
            bvid="BV1xx411c7mD",
            title=title,
            uploader="某UP主",
            duration=duration,
            url=url,
        )

    return _fetch


def _fake_pages(pages: list[download.VideoPage]):
    def _fetch(url, settings=None):
        return download.VideoInfo(title="系列课", uploader="某UP主", pages=pages)

    return _fetch


def _patch_pages_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """预检默认不打网络：概要探测一律短路为 None（按单P处理）。"""
    monkeypatch.setattr(probe.download, "fetch_video_info", lambda *_a, **_k: None)


def test_probe_reports_short_video(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(probe.download, "fetch_meta", _fake_meta(1234.0))
    _patch_pages_off(monkeypatch)
    with TestClient(create_app(tmp_path)) as client:
        response = client.post("/api/probe", json={"url": URL})

    assert response.status_code == 200
    body = response.json()
    assert body["too_long"] is False
    assert body["duration_text"] == "20:34"
    assert body["limit_seconds"] == 7200
    assert body["source_kind"] == "remote"


def test_probe_flags_over_long_video(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(probe.download, "fetch_meta", _fake_meta(2 * 3600 + 60))
    _patch_pages_off(monkeypatch)
    with TestClient(create_app(tmp_path)) as client:
        body = client.post("/api/probe", json={"url": URL}).json()

    assert body["too_long"] is True
    assert body["limit_text"] == "2 小时"


def test_probe_handles_local_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    local = tmp_path / "sample.m4a"
    local.write_bytes(b"fake")
    monkeypatch.setattr(probe.audio, "probe_duration", lambda _path, _settings: 2 * 3600 + 1)

    with TestClient(create_app(tmp_path)) as client:
        body = client.post("/api/probe", json={"url": str(local)}).json()

    assert body["source_kind"] == "local"
    assert body["too_long"] is True


def test_probe_rejects_invalid_input(tmp_path: Path):
    with TestClient(create_app(tmp_path)) as client:
        empty = client.post("/api/probe", json={"url": "   "})
        missing = client.post("/api/probe", json={"url": str(tmp_path / "nope.m4a")})

    assert empty.status_code == 400
    assert missing.status_code == 400


def test_probe_maps_fetch_failure_to_400(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def boom(*args, **kwargs):
        raise DownloadError("获取视频元信息失败")

    monkeypatch.setattr(probe.download, "fetch_meta", boom)
    with TestClient(create_app(tmp_path)) as client:
        response = client.post("/api/probe", json={"url": URL})

    assert response.status_code == 400


# --------------------------------------------------------- 预检 · 分P 探测

PAGES = [
    download.VideoPage(page=1, title="第一集", duration=158.0),
    download.VideoPage(page=2, title="第二集", duration=154.6),
    download.VideoPage(page=3, title="第三集", duration=200.0),
]


def test_probe_returns_pages_for_multi_p(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(probe.download, "fetch_meta", _fake_meta(158.0))
    monkeypatch.setattr(probe.download, "fetch_video_info", _fake_pages(PAGES))
    with TestClient(create_app(tmp_path)) as client:
        body = client.post("/api/probe", json={"url": URL}).json()

    assert [item["page"] for item in body["pages"]] == [1, 2, 3]
    assert body["pages"][0]["title"] == "第一集"
    assert body["pages"][0]["duration_text"] == "02:38"
    assert body["current_page"] == 1
    # 概要探测成功时展示主标题与 UP主（主标题优先于 yt-dlp 的「xxx p01 xxx」）
    assert body["uploader"] == "某UP主"
    assert body["title"] == "系列课"


def test_probe_reads_current_page_from_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(probe.download, "fetch_meta", _fake_meta(154.6))
    monkeypatch.setattr(probe.download, "fetch_video_info", _fake_pages(PAGES))
    with TestClient(create_app(tmp_path)) as client:
        body = client.post("/api/probe", json={"url": URL + "?p=2"}).json()

    assert body["current_page"] == 2
    assert len(body["pages"]) == 3


def test_probe_omits_pages_for_single_p(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(probe.download, "fetch_meta", _fake_meta(158.0))
    monkeypatch.setattr(probe.download, "fetch_video_info", _fake_pages(PAGES[:1]))
    with TestClient(create_app(tmp_path)) as client:
        body = client.post("/api/probe", json={"url": URL}).json()

    assert "pages" not in body
    assert "current_page" not in body
    assert body["uploader"] == "某UP主"  # meta 兜底，概要探测不成功也有 UP主


def test_probe_survives_pages_probe_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """概要探测失败只降级为单P，不能拖垮预检本身。"""

    def boom(*args, **kwargs):
        raise DownloadError("网络不可用")

    monkeypatch.setattr(probe.download, "fetch_meta", _fake_meta(158.0))
    monkeypatch.setattr(probe.download, "fetch_video_info", boom)
    with TestClient(create_app(tmp_path)) as client:
        response = client.post("/api/probe", json={"url": URL})

    assert response.status_code == 200
    body = response.json()
    assert "pages" not in body
    assert body["uploader"] == "某UP主"  # 仍来自 fetch_meta


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
    assert job.stage_index == 8


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


def test_cancel_running_job_reaches_cancelled_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """取消是独立终态：状态 cancelled、退出码 130、不产生 report。"""

    def long_pipeline(*_args, cancel_check=None, progress=None, **_kwargs):
        progress("[1/8] 下载 ✓ meta.json")
        for _ in range(500):
            if cancel_check is not None:
                cancel_check()  # 在检查点抛 CancelledError
            time.sleep(0.01)
        raise AssertionError("取消检查点应在此之前终止流水线")

    monkeypatch.setattr(jobs, "pipeline_run", long_pipeline)
    manager = jobs.JobManager(tmp_path)

    job = manager.submit(url=URL)
    deadline = time.monotonic() + 5
    while job.state != "running" and time.monotonic() < deadline:
        time.sleep(0.01)

    assert manager.cancel(job.id) is True
    wait_terminal(job)

    assert job.state == "cancelled"
    assert job.cancel_requested is True
    assert job.exit_code == 130
    snapshot = job.snapshot()
    assert snapshot["state"] == "cancelled"
    assert snapshot["cancel_requested"] is True
    # cancelled 已是终态：不允许重复取消
    assert manager.cancel(job.id) is False
    # 任务结束后可再次提交（取消不占坑）
    monkeypatch.setattr(jobs, "pipeline_run", fake_pipeline())
    next_job = manager.submit(url=URL)
    wait_terminal(next_job)
    assert next_job.state == "done"


def test_cancel_unknown_job_returns_none(tmp_path: Path):
    manager = jobs.JobManager(tmp_path)
    assert manager.cancel("deadbeef") is None


def test_cancel_route_maps_unknown_and_terminal_to_http_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(jobs, "pipeline_run", fake_pipeline())
    with TestClient(create_app(tmp_path)) as client:
        assert client.post("/api/jobs/deadbeef/cancel").status_code == 404

        created = client.post("/api/jobs", json={"url": URL})
        job_id = created.json()["job_id"]
        # 等任务自然结束后再取消 → 409
        deadline = time.monotonic() + 5
        while client.get(f"/api/jobs/{job_id}").json()["state"] != "done":
            if time.monotonic() > deadline:
                break
            time.sleep(0.01)
        response = client.post(f"/api/jobs/{job_id}/cancel")

    assert response.status_code == 409


def test_job_manager_queues_concurrent_submits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """忙碌时新任务排队而非拒绝：第一个结束后第二个自动开跑。"""
    gate = threading.Event()
    order: list[str] = []
    inner = fake_pipeline()

    def gated(*args, progress=None, **kwargs):
        gate.wait(5)
        order.append(kwargs["url"] if "url" in kwargs else args[0] if args else "?")
        return inner(*args, **kwargs)

    monkeypatch.setattr(jobs, "pipeline_run", gated)
    manager = jobs.JobManager(tmp_path)

    first = manager.submit(url=URL)
    # 第一个还在跑：第二个任务应被受理（排队），不再抛 JobBusy
    second = manager.submit(url=URL + "?p=2")
    assert second.state == "queued"
    assert manager.queue_position(first.id) == 0
    assert manager.queue_position(second.id) == 1

    gate.set()
    wait_terminal(first)
    wait_terminal(second)

    assert first.state == "done" and second.state == "done"
    # FIFO：先提交的先执行
    assert order[0] == URL and order[1] == URL + "?p=2"


def test_cancel_queued_job_never_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """排队中的任务可直接取消：置为 cancelled 终态，轮到它时跳过。"""
    gate = threading.Event()
    inner = fake_pipeline()

    def gated(*args, **kwargs):
        gate.wait(5)
        return inner(*args, **kwargs)

    monkeypatch.setattr(jobs, "pipeline_run", gated)
    manager = jobs.JobManager(tmp_path)

    first = manager.submit(url=URL)
    second = manager.submit(url=URL + "?p=2")
    assert second.state == "queued"

    assert manager.cancel(second.id) is True
    assert second.state == "cancelled"
    assert second.exit_code == 130

    gate.set()
    wait_terminal(first)
    wait_terminal(second)
    # 第二个任务从未真正执行：没有 report 产物
    assert second.report_path is None


def test_queue_full_rejects_with_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """排队超出上限直接拒绝（JobBusy → 409），避免任务无限堆积。"""
    gate = threading.Event()
    inner = fake_pipeline()

    def gated(*args, **kwargs):
        gate.wait(5)
        return inner(*args, **kwargs)

    monkeypatch.setattr(jobs, "pipeline_run", gated)
    manager = jobs.JobManager(tmp_path)

    first = manager.submit(url=URL)
    try:
        for _ in range(jobs.JobManager.MAX_QUEUE):
            manager.submit(url=URL)
        with pytest.raises(jobs.JobBusy):
            manager.submit(url=URL)
    finally:
        gate.set()
        wait_terminal(first)


def test_search_runs_finds_transcript_text(tmp_path: Path):
    """全文检索：命中转写稿正文，返回带关键词的摘要。"""
    make_run(tmp_path, RUN_ID, title="示例视频")
    (tmp_path / RUN_ID / "transcript.md").write_text(
        "## u0001 [00:00-00:12]\n我们今天讲缓存预热的三种姿势。\n",
        encoding="utf-8",
    )

    hits = library.search_runs(tmp_path, "缓存预热")

    assert len(hits) == 1
    assert hits[0]["run_id"] == RUN_ID
    assert hits[0]["count"] == 1
    assert "缓存预热" in hits[0]["snippet"]


def test_search_runs_case_insensitive_and_miss(tmp_path: Path):
    make_run(tmp_path, RUN_ID, title="Redis Deep Dive")
    (tmp_path / RUN_ID / "transcript.md").write_text(
        "## u0001 [00:00-00:12]\nUse lazy loading to avoid cache stampsede.\n",
        encoding="utf-8",
    )

    assert [item["run_id"] for item in library.search_runs(tmp_path, "REDIS")] == [RUN_ID]
    assert library.search_runs(tmp_path, "kubernetes") == []
    # 过短的关键词不进检索（避免全库刷屏）
    assert library.search_runs(tmp_path, "R") == []


def test_usage_stats_aggregates_by_day(tmp_path: Path):
    """统计：同一天的两个 run 聚合成一行，耗时与 token 相加。"""
    make_run(tmp_path, RUN_ID)
    second = make_run(tmp_path, "BV1xx411c7mD-bbbbbbbb")
    # 追加第二个 stage 让 total 不同，验证聚合而非覆盖
    with (second / "run.trace.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": "t", "stage": "render", "event": "end", "dur_ms": 500}) + "\n")

    days = library.usage_stats(tmp_path)

    assert len(days) == 1
    assert days[0]["runs"] == 2
    # 两个 run 的 make_run 模板各 4768+1000，第二个再追加 render 500
    assert days[0]["total_ms"] == (4768 + 1000) * 2 + 500
    assert days[0]["tokens"] == 1234 * 2  # 每份 run 的 outline 阶段各记 1234


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


def test_web_main_uses_default_out_root(monkeypatch: pytest.MonkeyPatch):
    """回归：移除 Settings.out_root 字段后，控制台默认目录仍能解析启动。

    曾因 `settings.out_root` 被删而在启动时抛 AttributeError。
    """

    class _StubUvicorn:
        @staticmethod
        def run(*_args: object, **_kwargs: object) -> None:
            return None

    from videread.web import __main__ as web_main

    monkeypatch.setitem(sys.modules, "uvicorn", _StubUvicorn)
    assert web_main.main([]) == 0


def test_events_replay_since_returns_tail_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(jobs, "pipeline_run", fake_pipeline())
    manager = jobs.JobManager(tmp_path)

    job = manager.submit(url=URL)
    wait_terminal(job)

    _, backlog = job.subscribe(since=len(job._events) - 1)  # noqa: SLF001 - 用例内窥
    assert [event["type"] for event in backlog] == ["done"]


def test_markdown_endpoint_blocked_for_framed_runs(tmp_path: Path):
    """带截图的报告不提供 MD 导出：409 + 指路 PDF/PNG；纯文本报告不受影响。"""
    make_run(tmp_path, RUN_ID)
    frames = tmp_path / RUN_ID / "frames"
    frames.mkdir()
    (frames / "s1.jpg").write_bytes(b"\xff\xd8\xff fake")

    client = TestClient(create_app(tmp_path))
    blocked = client.get(f"/api/runs/{RUN_ID}/markdown")
    assert blocked.status_code == 409
    assert "PDF / PNG" in blocked.json()["detail"]

    (frames / "s1.jpg").unlink()  # 截图产物清空即视为纯文本，照常导出
    ok = client.get(f"/api/runs/{RUN_ID}/markdown")
    assert ok.status_code == 200


def test_probe_reports_existing_run(tmp_path: Path):
    """已解析过的视频：probe 带上 existing_run（含最新一份的报告入口）。"""
    from videread import download as dl

    run_dir = tmp_path / dl.make_run_id(URL, "BV1xx411c7mD")
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "bvid": "BV1xx411c7mD",
                "title": "示例视频",
                "uploader": "某UP主",
                "duration": 1234.0,
                "url": URL,
                "cover": "",
                "description": "",
                "subtitles": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (run_dir / "report.html").write_text("<html><body>r</body></html>", encoding="utf-8")

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(probe.download, "fetch_video_info", lambda *_a, **_k: None)
        with TestClient(create_app(tmp_path)) as client:
            response = client.post("/api/probe", json={"url": URL})
    finally:
        monkeypatch.undo()

    body = response.json()
    assert body["existing_run"]["run_id"] == run_dir.name
    assert body["existing_run"]["report_url"] == f"/report/{run_dir.name}"
    assert body["existing_run"]["generated_at"]

    # 没有报告产物（只跑了一半）不算已解析
    (run_dir / "report.html").unlink()
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(probe.download, "fetch_video_info", lambda *_a, **_k: None)
        with TestClient(create_app(tmp_path)) as client:
            response = client.post("/api/probe", json={"url": URL})
    finally:
        monkeypatch.undo()
    assert "existing_run" not in response.json()


def test_probe_reports_existing_run(tmp_path: Path):
    """已解析过的视频：probe 带上 existing_run（含最新一份的报告入口）。"""
    from videread import download as dl

    run_dir = tmp_path / dl.make_run_id(URL, "BV1xx411c7mD")
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "bvid": "BV1xx411c7mD",
                "title": "示例视频",
                "uploader": "某UP主",
                "duration": 1234.0,
                "url": URL,
                "cover": "",
                "description": "",
                "subtitles": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (run_dir / "report.html").write_text("<html><body>r</body></html>", encoding="utf-8")

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(probe.download, "fetch_video_info", lambda *_a, **_k: None)
        with TestClient(create_app(tmp_path)) as client:
            response = client.post("/api/probe", json={"url": URL})
    finally:
        monkeypatch.undo()

    body = response.json()
    assert body["existing_run"]["run_id"] == run_dir.name
    assert body["existing_run"]["report_url"] == f"/report/{run_dir.name}"
    assert body["existing_run"]["generated_at"]

    # 没有报告产物（只跑了一半）不算已解析
    (run_dir / "report.html").unlink()
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(probe.download, "fetch_video_info", lambda *_a, **_k: None)
        with TestClient(create_app(tmp_path)) as client:
            response = client.post("/api/probe", json={"url": URL})
    finally:
        monkeypatch.undo()
    assert "existing_run" not in response.json()


# ------------------------------------------------------------- 自定义背景

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def test_background_upload_serve_and_reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """上传 → 带缓存戳的 URL 可取回原图 → 恢复默认后 404。"""
    from videread.web import background as bg_mod

    monkeypatch.setattr(bg_mod, "BG_DIR", tmp_path / ".ui")
    png = PNG_MAGIC + b"fake-png-body"

    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/ui/background").json()["url"] is None

        created = client.post("/api/ui/background", content=png)
        assert created.status_code == 200
        url = created.json()["url"]
        assert url.startswith("/api/ui/background/image?v=")

        image = client.get(url)
        assert image.status_code == 200
        assert image.headers["content-type"].startswith("image/png")
        assert image.content == png

        # 列表接口与 GET 一致
        assert client.get("/api/ui/background").json()["url"] == url

        # 再传一张 jpg：覆盖旧图（只保留一份）
        jpg = b"\xff\xd8\xff" + b"fake-jpeg"
        url2 = client.post("/api/ui/background", content=jpg).json()["url"]
        assert client.get(url2).headers["content-type"].startswith("image/jpeg")

        # 恢复默认
        assert client.delete("/api/ui/background").json()["url"] is None
        assert client.get("/api/ui/background").json()["url"] is None
        assert client.get("/api/ui/background/image").status_code == 404


def test_background_rejects_invalid_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """魔数不对 / 空内容一律 400，不落盘。"""
    from videread.web import background as bg_mod

    monkeypatch.setattr(bg_mod, "BG_DIR", tmp_path / ".ui")

    with TestClient(create_app(tmp_path)) as client:
        assert client.post("/api/ui/background", content=b"not-an-image").status_code == 400
        assert client.post("/api/ui/background", content=b"").status_code == 400

    assert not (tmp_path / ".ui").exists() or not list((tmp_path / ".ui").glob("background.*"))


# ------------------------------------------------------------- 自定义背景

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def test_background_upload_serve_and_reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """上传 → 带缓存戳的 URL 可取回原图 → 恢复默认后 404。"""
    from videread.web import background as bg_mod

    monkeypatch.setattr(bg_mod, "BG_DIR", tmp_path / ".ui")
    png = PNG_MAGIC + b"fake-png-body"

    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/ui/background").json()["url"] is None

        created = client.post("/api/ui/background", content=png)
        assert created.status_code == 200
        url = created.json()["url"]
        assert url.startswith("/api/ui/background/image?v=")

        image = client.get(url)
        assert image.status_code == 200
        assert image.headers["content-type"].startswith("image/png")
        assert image.content == png

        # 列表接口与 GET 一致
        assert client.get("/api/ui/background").json()["url"] == url

        # 再传一张 jpg：覆盖旧图（只保留一份）
        jpg = b"\xff\xd8\xff" + b"fake-jpeg"
        url2 = client.post("/api/ui/background", content=jpg).json()["url"]
        assert client.get(url2).headers["content-type"].startswith("image/jpeg")

        # 恢复默认
        assert client.delete("/api/ui/background").json()["url"] is None
        assert client.get("/api/ui/background").json()["url"] is None
        assert client.get("/api/ui/background/image").status_code == 404


def test_background_rejects_invalid_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """魔数不对 / 空内容一律 400，不落盘。"""
    from videread.web import background as bg_mod

    monkeypatch.setattr(bg_mod, "BG_DIR", tmp_path / ".ui")

    with TestClient(create_app(tmp_path)) as client:
        assert client.post("/api/ui/background", content=b"not-an-image").status_code == 400
        assert client.post("/api/ui/background", content=b"").status_code == 400

    assert not (tmp_path / ".ui").exists() or not list((tmp_path / ".ui").glob("background.*"))


def test_background_overlay_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """遮罩独立保存：设置/读取/非法名 400，换背景图不丢遮罩选择。"""
    from videread.web import background as bg_mod

    monkeypatch.setattr(bg_mod, "BG_DIR", tmp_path / ".ui")

    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/ui/background").json()["overlay"] == "default"

        ok = client.post(
            "/api/ui/background/overlay", json={"name": "dark"}
        )
        assert ok.status_code == 200 and ok.json()["overlay"] == "dark"
        assert client.get("/api/ui/background").json()["overlay"] == "dark"

        # 换背景图不丢遮罩
        url = client.post(
            "/api/ui/background", content=PNG_MAGIC + b"x"
        ).json()["url"]
        assert client.get("/api/ui/background").json()["overlay"] == "dark"
        assert client.get("/api/ui/background").json()["url"] == url

        # 非法名 / 非法 JSON
        assert client.post("/api/ui/background/overlay", json={"name": "epic"}).status_code == 400
        assert client.post(
            "/api/ui/background/overlay", content=b"not-json",
            headers={"Content-Type": "application/json"},
        ).status_code == 400

        # 恢复默认背景不影响遮罩选择
        client.delete("/api/ui/background")
        assert client.get("/api/ui/background").json()["overlay"] == "dark"
