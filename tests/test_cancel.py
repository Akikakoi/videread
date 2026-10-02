"""任务取消：流水线检查点与逐节写作的断点保留用例（离线）。"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from videread import pipeline
from videread.config import get_settings
from videread.errors import CancelledError
from videread.pipeline import run
from videread.report.outline import Outline, OutlineSection
from videread.report.writer import write_sections
from videread.transcript import TranscriptUnit

from test_pipeline import _seed_run  # 复用铺满缓存的夹具构造

URL = "https://www.bilibili.com/video/BV1xx411c7mD"


def test_cancel_check_raises_at_first_checkpoint(tmp_path: Path):
    """取消检查点在任何阶段开始前触发：直接抛 CancelledError，退出码 130。"""
    lines: list[str] = []

    def _cancel() -> None:
        raise CancelledError("用户取消了任务")

    with pytest.raises(CancelledError) as excinfo:
        run(
            URL,
            out_root=tmp_path,
            cancel_check=_cancel,
            progress=lines.append,
        )

    assert excinfo.value.exit_code == 130
    joined = "\n".join(lines)
    assert "任务已取消" in joined
    # 下载都没开始，不留任何 run 目录
    assert list(tmp_path.iterdir()) == []


def test_cancel_mid_pipeline_keeps_produced_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """在逐节写作入口处取消（LlmClient 构造时抛）：前面阶段的产物保留。"""
    run_dir = _seed_run(tmp_path)
    (run_dir / "report.html").unlink()
    (run_dir / "sections" / "s1.html").unlink()

    # 缓存命中一路跳到逐节写作；让 LlmClient 构造时先触发取消
    class _CancelOnPlan:
        def __init__(self, _settings: object) -> None:
            raise CancelledError("用户取消了任务")

    monkeypatch.setattr(pipeline, "LlmClient", _CancelOnPlan)
    lines: list[str] = []

    with pytest.raises(CancelledError):
        run(URL, out_root=tmp_path, progress=lines.append)

    joined = "\n".join(lines)
    assert "任务已取消" in joined
    # 已落盘产物没被动过：transcript 等缓存照常在
    assert (run_dir / "transcript.jsonl").is_file()


# ---------------------------------------------------------- 逐节写作的取消


def _units() -> list[TranscriptUnit]:
    return [TranscriptUnit(id="u0001", start=0.0, end=10.0, text="正文", source="asr")]


def _outline(count: int) -> Outline:
    return Outline(
        title="t",
        subtitle="",
        lead="l",
        profile="mechanism",
        sections=[
            OutlineSection(
                id=f"s{i}",
                heading=f"第 {i} 章",
                time_range="00:00-00:10",
                intent="讲清机制",
                source_ids=["u0001"],
            )
            for i in range(1, count + 1)
        ],
    )


def test_write_sections_cancel_keeps_completed_parts(tmp_path, monkeypatch):
    """取消后：已完成的节照常落盘，未开始的节撤销，抛 CancelledError。"""
    outline = _outline(3)
    first_done = threading.Event()  # 第 1 节的 LLM 调用已返回
    release = threading.Event()     # 后续节的放行闸（测试里从不打开）

    class _SlowLlm:
        def __init__(self, _settings: object) -> None:
            pass

        def complete(self, *, user: str, **_kwargs: object) -> str:
            if "第 1 章" in user:
                first_done.set()
                return '<p data-source-units="u0001">正文</p>'
            # 后续节挂住，模拟「取消发生时还有在途调用」
            if not release.wait(2.0):
                raise RuntimeError("取消后此结果不再被需要")
            return '<p data-source-units="u0001">正文</p>'

    monkeypatch.setattr("videread.report.writer.LlmClient", _SlowLlm)

    def _cancel() -> None:
        if first_done.is_set():
            raise CancelledError("用户取消了任务")

    # 串行（并发 1）保证确定性：s1 完成落盘 → 检查点取消
    with pytest.raises(CancelledError):
        write_sections(
            outline,
            _units(),
            out_dir=tmp_path,
            mode="standard",
            settings=get_settings(llm_write_concurrency=1),
            cancel_check=_cancel,
        )

    # s1 已落盘保留；其余节未写
    assert (tmp_path / "sections" / "s1.html").is_file()
    assert not (tmp_path / "sections" / "s2.html").exists()
    assert not (tmp_path / "sections" / "s3.html").exists()
    release.set()  # 兜底：万一还有挂住的在途调用，尽快放它走


# ---------------------------------------------------------- 本地 ASR 后端


def test_local_whisper_backend_is_selectable(monkeypatch: pytest.MonkeyPatch):
    """ASR_BACKEND=local 可选；模型规格与语言可经环境变量覆盖（懒加载不装包也能构造）。"""
    from videread.asr import get_backend
    from videread.config import get_settings

    monkeypatch.setenv("LOCAL_ASR_MODEL", "base")
    backend = get_backend(get_settings(asr_backend="local"))
    assert backend.name == "local"
    assert backend.model_name == "base"

    monkeypatch.setenv("ASR_BACKEND", "faster-whisper")
    assert get_backend(get_settings()).name == "local"


def test_local_whisper_missing_dependency_gives_install_hint(monkeypatch: pytest.MonkeyPatch):
    """未安装 faster-whisper时报安装指引，而不是裸 ImportError。"""
    import builtins

    from videread.asr.local_whisper import LocalWhisperAsr
    from videread.config import get_settings
    from videread.errors import UsageError

    backend = LocalWhisperAsr(get_settings())
    real_import = builtins.__import__

    def _no_faster_whisper(name, *args, **kwargs):
        if name == "faster_whisper":
            raise ImportError("No module named 'faster_whisper'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_faster_whisper)
    with pytest.raises(UsageError, match="faster-whisper"):
        backend._load()


# ---------------------------------------------------------- 本地 ASR 后端


def test_local_whisper_backend_is_selectable(monkeypatch: pytest.MonkeyPatch):
    """ASR_BACKEND=local 可选；模型规格与语言可经环境变量覆盖（懒加载不装包也能构造）。"""
    from videread.asr import get_backend
    from videread.config import get_settings

    monkeypatch.setenv("LOCAL_ASR_MODEL", "base")
    backend = get_backend(get_settings(asr_backend="local"))
    assert backend.name == "local"
    assert backend.model_name == "base"

    monkeypatch.setenv("ASR_BACKEND", "faster-whisper")
    assert get_backend(get_settings()).name == "local"


def test_local_whisper_missing_dependency_gives_install_hint(monkeypatch: pytest.MonkeyPatch):
    """未安装 faster-whisper时报安装指引，而不是裸 ImportError。"""
    import builtins

    from videread.asr.local_whisper import LocalWhisperAsr
    from videread.config import get_settings
    from videread.errors import UsageError

    backend = LocalWhisperAsr(get_settings())
    real_import = builtins.__import__

    def _no_faster_whisper(name, *args, **kwargs):
        if name == "faster_whisper":
            raise ImportError("No module named 'faster_whisper'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_faster_whisper)
    with pytest.raises(UsageError, match="faster-whisper"):
        backend._load()
