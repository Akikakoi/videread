"""任务队列 SQLite 落盘用例（全部离线）。

服务重启后的恢复约定：
- queued 任务 → 重新入队并继续执行；
- running 任务 → 进程已消失，置为 cancelled 终态并提示断点续跑。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from videread.web import jobs

URL = "https://www.bilibili.com/video/BV1xx411c7mD"
RUN_ID = "BV1xx411c7mD-aaaaaaaa"
REPORT_HTML = "<!DOCTYPE html><html><body><p>report</p></body></html>"


def wait_terminal(job: jobs.Job, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if job.terminal:
            return
        time.sleep(0.01)
    raise AssertionError(f"任务未在 {timeout}s 内结束：state={job.state}")


def fake_pipeline():
    def _run(url: str, *, out_root: Path, progress=print, **_kwargs) -> Path:
        progress("[1/8] 下载 ✓ meta.json")
        run_dir = Path(out_root) / RUN_ID
        run_dir.mkdir(parents=True, exist_ok=True)
        report = run_dir / "report.html"
        report.write_text(REPORT_HTML, encoding="utf-8")
        progress("[8/8] 渲染 ✓ report.html")
        return report

    return _run


def blocking_pipeline(gate: threading.Event):
    """接到 cancel_check 抛出或 gate 放行前一直挂着，模拟执行中的长任务。"""

    def _run(url: str, *, progress=print, cancel_check=None, **_kwargs) -> Path:
        progress("[1/8] 下载 ✓ meta.json")
        for _ in range(500):
            if cancel_check is not None:
                cancel_check()
            if gate.wait(timeout=0.01):
                break
        raise AssertionError("测试应在此之前结束任务")

    return _run


def test_queued_job_survives_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """重启恢复：排队任务被新的 JobManager 接手并跑完，id 不变。"""
    gate = threading.Event()
    monkeypatch.setattr(jobs, "pipeline_run", blocking_pipeline(gate))
    first = jobs.JobManager(tmp_path)
    running = first.submit(url=URL)
    deadline = time.monotonic() + 5
    while running.state != "running" and time.monotonic() < deadline:
        time.sleep(0.01)
    queued = first.submit(url=URL, mode="brief")
    assert queued.state == "queued"

    # 服务重启：新的 JobManager 打开同一个 SQLite
    monkeypatch.setattr(jobs, "pipeline_run", fake_pipeline())
    second = jobs.JobManager(tmp_path)

    restored = second.get(queued.id)
    assert restored is not None, "排队任务应从 SQLite 恢复"
    assert restored.mode == "brief"
    wait_terminal(restored)
    assert restored.state == "done"
    assert restored.run_id == RUN_ID

    # running 任务无法确认死在哪一步：标记为 cancelled 终态
    interrupted = second.get(running.id)
    assert interrupted is not None
    assert interrupted.state == "cancelled"
    assert "续跑" in (interrupted.hint or "")
    gate.set()


def test_running_job_marked_cancelled_without_zombie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """恢复的 running 任务不进入队列、不占 active 坑位。"""
    monkeypatch.setattr(jobs, "pipeline_run", blocking_pipeline(threading.Event()))
    first = jobs.JobManager(tmp_path)
    running = first.submit(url=URL)
    deadline = time.monotonic() + 5
    while running.state != "running" and time.monotonic() < deadline:
        time.sleep(0.01)

    second = jobs.JobManager(tmp_path)
    restored = second.get(running.id)
    assert restored.state == "cancelled"
    # 恢复不重跑：running 任务只被标记，不会被第二个 manager 再次执行
    assert restored not in second._queue  # noqa: SLF001 - 用例内窥队列
    assert second._active is None  # noqa: SLF001
    # 终态任务不应阻塞新提交
    monkeypatch.setattr(jobs, "pipeline_run", fake_pipeline())
    fresh = second.submit(url=URL)
    wait_terminal(fresh)
    assert fresh.state == "done"


def test_logs_and_progress_persisted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """恢复的任务保留已产生的日志，工单页重开可回放。"""
    gate = threading.Event()
    monkeypatch.setattr(jobs, "pipeline_run", blocking_pipeline(gate))
    manager = jobs.JobManager(tmp_path)
    running = manager.submit(url=URL)
    deadline = time.monotonic() + 5
    while running.state != "running" and time.monotonic() < deadline:
        time.sleep(0.01)

    second = jobs.JobManager(tmp_path)
    restored = second.get(running.id)
    assert any("下载" in line for line in restored.logs)
    gate.set()


def test_terminal_jobs_kept_but_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """终态任务留在库里备查且有上限，不会无限膨胀。"""
    monkeypatch.setattr(jobs, "pipeline_run", fake_pipeline())
    manager = jobs.JobManager(tmp_path)
    for index in range(jobs.JobStore._KEEP_TERMINAL + 20):  # noqa: SLF001
        job = manager.submit(url=f"{URL}&t={index}")
        wait_terminal(job)

    with_persist = jobs.JobManager(tmp_path)
    rows = with_persist._store.load_all()  # noqa: SLF001
    assert len(rows) <= jobs.JobStore._KEEP_TERMINAL  # noqa: SLF001


def test_store_corruption_degrades_to_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """SQLite 文件损坏时退化为纯内存队列，任务照常可跑。"""
    (tmp_path / ".jobs.sqlite3").write_bytes(b"not a database")
    monkeypatch.setattr(jobs, "pipeline_run", fake_pipeline())
    manager = jobs.JobManager(tmp_path)
    assert manager._store is None  # noqa: SLF001 - 用例内窥降级路径

    job = manager.submit(url=URL)
    wait_terminal(job)
    assert job.state == "done"
