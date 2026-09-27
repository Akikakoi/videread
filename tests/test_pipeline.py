"""§13.1：流水线断点续跑用例（离线，不联网、不调用 ffmpeg / LLM）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from videread import download, pipeline
from videread.errors import AsrError, LlmError, UsageError, VidereadError
from videread.pipeline import run

URL = "https://www.bilibili.com/video/BV1xx411c7mD"
BVID = "BV1xx411c7mD"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _seed_run(out_root: Path) -> Path:
    """铺满全部中间产物，使每个阶段的缓存判据都命中。"""
    run_dir = out_root / download.make_run_id(URL, BVID)
    _write(
        run_dir / "meta.json",
        json.dumps(
            {
                "bvid": BVID,
                "title": "缓存预热讲清楚",
                "uploader": "某UP主",
                "duration": 12.0,
                "url": URL,
                "cover": "",
                "description": "",
                "subtitles": [],
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    _write(run_dir / "audio.wav", "fake-wav")
    _write(
        run_dir / "asr.raw.jsonl",
        json.dumps({"start": 0.0, "end": 12.0, "text": "我们今天讲缓存预热。"}, ensure_ascii=False)
        + "\n",
    )
    _write(
        run_dir / "transcript.jsonl",
        json.dumps(
            {"id": "u0001", "start": 0.0, "end": 12.0, "text": "我们今天讲缓存预热。", "source": "asr"},
            ensure_ascii=False,
        )
        + "\n",
    )
    _write(run_dir / "transcript.md", "## u0001 [00:00-00:12]\n我们今天讲缓存预热。\n")
    _write(
        run_dir / "outline.json",
        json.dumps(
            {
                "title": "缓存预热讲清楚",
                "subtitle": "",
                "lead": "导语。",
                "profile": "mechanism",
                "sections": [
                    {
                        "id": "s1",
                        "heading": "预热的作用",
                        "time_range": "00:00-00:12",
                        "intent": "讲清预热与冷启动延迟的关系",
                        "source_ids": ["u0001"],
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    _write(run_dir / "sections" / "s1.html", '<div data-source-units="u0001"><p>正文</p></div>\n')
    _write(run_dir / "report.html", "<html><body>已有报告</body></html>")
    return run_dir


def test_pipeline_skips_all_stages_when_cached(tmp_path: Path):
    run_dir = _seed_run(tmp_path)
    lines: list[str] = []

    result = run(URL, out_root=tmp_path, progress=lines.append)

    assert result == run_dir / "report.html"
    assert result.is_file()
    joined = "\n".join(lines)
    assert joined.count("缓存命中，跳过") >= 5
    assert "report.html" in joined

    stages = [
        json.loads(line)["stage"]
        for line in (run_dir / "run.trace.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert {"download", "audio", "asr", "transcript", "outline", "sections", "render"} <= set(stages)


def test_pipeline_reports_produced_artifacts_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # sections/s1.html 缺失 → 进入逐节写作，此处用假 LlmClient 模拟 LLM 失败
    run_dir = _seed_run(tmp_path)
    (run_dir / "sections" / "s1.html").unlink()

    class _BrokenLlm:
        def __init__(self, _settings: object) -> None:
            raise LlmError("模拟 LLM 不可用")

    monkeypatch.setattr(pipeline, "LlmClient", _BrokenLlm)
    lines: list[str] = []

    with pytest.raises(VidereadError) as excinfo:
        run(URL, out_root=tmp_path, progress=lines.append)

    assert excinfo.value.exit_code == 4
    joined = "\n".join(lines)
    assert "逐节写作" in joined
    assert "已生成的产物" in joined


def test_pipeline_rejects_missing_local_file(tmp_path: Path):
    with pytest.raises(UsageError):
        run(str(tmp_path / "not-here.m4a"), out_root=tmp_path, progress=lambda _msg: None)


def test_pipeline_rejects_unknown_mode(tmp_path: Path):
    with pytest.raises(UsageError):
        run(URL, mode="epic", out_root=tmp_path, progress=lambda _msg: None)


def test_pipeline_refetches_meta_belonging_to_another_video(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """§11.3：run 目录里的 meta.json 若不属于本次请求，必须重新抓取而不是直接复用。"""
    run_dir = tmp_path / download.make_run_id(URL, BVID)
    run_dir.mkdir(parents=True)
    download.write_meta(
        download.VideoMeta(
            bvid=BVID,
            title="别的视频",
            uploader="x",
            duration=1.0,
            url="https://www.bilibili.com/video/BV1zz411c7mE",
        ),
        run_dir / "meta.json",
    )

    fetched: list[str] = []

    def _fake_fetch(url: str, settings: object) -> None:
        fetched.append(url)
        raise UsageError("到此为止：能走到这里说明确实重新抓取了")

    monkeypatch.setattr(download, "fetch_meta", _fake_fetch)

    with pytest.raises(UsageError):
        run(URL, out_root=tmp_path, progress=lambda _msg: None)

    assert fetched == [URL]


def test_dashscope_base_url_is_configurable(monkeypatch: pytest.MonkeyPatch):
    """§6.5：专属/私有化 MaaS 部署可用 DASHSCOPE_BASE_URL 覆盖接入点。"""
    monkeypatch.setenv(
        "DASHSCOPE_BASE_URL", "https://ws-example.cn-beijing.maas.aliyuncs.com/api/v1"
    )
    monkeypatch.setenv("DASHSCOPE_API_KEY", "probe")

    from videread.asr.dashscope import DashScopeAsr
    from videread.config import get_settings

    backend = DashScopeAsr(get_settings())
    # 不真正发请求，只校验拼接结果：base_url 必须以 / 结尾，否则会拼成 /api/v1uploads
    request = backend._client.build_request("GET", "/uploads")
    assert str(request.url) == (
        "https://ws-example.cn-beijing.maas.aliyuncs.com/api/v1/uploads"
    )


def test_realtime_url_is_derived_from_base_url():
    """§6.5：实时后端地址由异步接入点推导（/api/v1 → /api-ws/v1/inference, https → wss）。"""
    from videread.asr.realtime import realtime_url

    assert realtime_url("https://dashscope.aliyuncs.com/api/v1") == (
        "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
    )
    assert realtime_url("https://ws-example.cn-beijing.maas.aliyuncs.com/api/v1") == (
        "wss://ws-example.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference"
    )


def test_realtime_backend_is_selectable(monkeypatch: pytest.MonkeyPatch):
    """§6.5：ASR_BACKEND=dashscope-realtime 可用；模型名按 realtime 关键字推导。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "probe")
    monkeypatch.setenv("DASHSCOPE_MODEL", "paraformer-v2")

    from videread.asr import get_backend
    from videread.config import get_settings

    backend = get_backend(get_settings(asr_backend="dashscope-realtime"))
    assert backend.name == "dashscope-realtime"
    assert backend.model == "paraformer-realtime-v2"

    monkeypatch.setenv("DASHSCOPE_MODEL", "paraformer-realtime-v1")
    assert get_backend(get_settings(asr_backend="realtime")).model == (
        "paraformer-realtime-v1"
    )


def test_realtime_rejects_non_mono_wav(tmp_path: Path):
    """§6.5：实时接口只接受单声道 16bit PCM，双声道应直接报错而不是发错数据。"""
    import wave

    from videread.asr.realtime import _read_pcm_frames

    stereo = tmp_path / "stereo.wav"
    with wave.open(str(stereo), "wb") as fh:
        fh.setnchannels(2)
        fh.setsampwidth(2)
        fh.setframerate(16000)
        fh.writeframes(b"\x00" * 400)

    with pytest.raises(AsrError):
        _read_pcm_frames(stereo)