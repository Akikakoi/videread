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

# 覆盖整段 12s 视频的字幕；另一份只覆盖开头 1s，用于验证不完整字幕会回退
SRT_FULL = "1\n00:00:00,000 --> 00:00:12,000\n我们今天讲缓存预热。\n"
SRT_PARTIAL = "1\n00:00:00,000 --> 00:00:01,000\n开场白。\n"


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


def _seed_subtitle_run(out_root: Path, srt_text: str) -> Path:
    """只铺 meta.json + 平台字幕，不含音频与 ASR 产物 —— 字幕优先的起点。"""
    run_dir = out_root / download.make_run_id(URL, BVID)
    _write(run_dir / "subtitle.zh-CN.srt", srt_text)
    _write(
        run_dir / "meta.json",
        json.dumps(
            {
                "bvid": BVID,
                "title": "字幕优先讲清楚",
                "uploader": "某UP主",
                "duration": 12.0,
                "url": URL,
                "cover": "",
                "description": "",
                "subtitles": [{"lang": "zh-CN", "path": "subtitle.zh-CN.srt"}],
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    return run_dir


def _read_units(run_dir: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (run_dir / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _stop_at_outline(monkeypatch: pytest.MonkeyPatch) -> None:
    """让流水线在结构规划阶段停下：能走到这里说明转写稿已经建好。"""

    class _StopLlm:
        def __init__(self, _settings: object) -> None:
            raise LlmError("模拟 LLM 不可用")

    monkeypatch.setattr(pipeline, "LlmClient", _StopLlm)


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


def test_subtitle_first_skips_audio_and_asr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """§6.3：平台字幕够完整时，不下载音频、不调用 ASR，直接由字幕建转写稿。"""
    run_dir = _seed_subtitle_run(tmp_path, SRT_FULL)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("字幕优先路径不应触碰下载音频 / ASR")

    monkeypatch.setattr(download, "download_audio", _boom)
    monkeypatch.setattr(pipeline, "get_backend", _boom)
    _stop_at_outline(monkeypatch)
    lines: list[str] = []

    with pytest.raises(LlmError):
        run(URL, out_root=tmp_path, progress=lines.append)

    joined = "\n".join(lines)
    assert "字幕优先" in joined
    assert "跳过音频处理" in joined
    # 音频阶段的耗时条目仍在，但标记为跳过
    stages = [
        json.loads(line)
        for line in (run_dir / "run.trace.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ends = {item["stage"]: item for item in stages if item["event"] == "end"}
    assert ends["audio"]["detail"]["skipped"]
    assert ends["asr"]["detail"]["source"] == "subtitle"

    units = _read_units(run_dir)
    assert units and all(unit["source"] == "subtitle" for unit in units)
    assert "缓存预热" in units[0]["text"]


def test_subtitle_first_falls_back_when_subtitle_is_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """字幕只覆盖一小段视频时判为不完整，回退到下载音频 + ASR。"""
    run_dir = _seed_subtitle_run(tmp_path, SRT_PARTIAL)
    _write(run_dir / "audio.wav", "fake-wav")
    _write(
        run_dir / "asr.raw.jsonl",
        json.dumps({"start": 0.0, "end": 12.0, "text": "ASR 文本。"}, ensure_ascii=False) + "\n",
    )
    _stop_at_outline(monkeypatch)
    lines: list[str] = []

    with pytest.raises(LlmError):
        run(URL, out_root=tmp_path, progress=lines.append)

    joined = "\n".join(lines)
    assert "判为不完整" in joined
    assert "字幕优先" not in joined
    # 字幕覆盖率不足，转写单元应保持 ASR 来源
    assert all(unit["source"] == "asr" for unit in _read_units(run_dir))


def test_subtitle_first_can_be_disabled_by_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """SUBTITLE_FIRST=0 时，即使有完整字幕也照旧下载音频走 ASR。"""
    monkeypatch.setenv("SUBTITLE_FIRST", "0")
    run_dir = _seed_subtitle_run(tmp_path, SRT_FULL)
    _write(run_dir / "audio.wav", "fake-wav")
    _write(
        run_dir / "asr.raw.jsonl",
        json.dumps({"start": 0.0, "end": 12.0, "text": "ASR 文本。"}, ensure_ascii=False) + "\n",
    )
    _stop_at_outline(monkeypatch)
    lines: list[str] = []

    with pytest.raises(LlmError):
        run(URL, out_root=tmp_path, progress=lines.append)

    assert "字幕优先" not in "\n".join(lines)


def test_force_asr_overrides_subtitle_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """--force-asr 无视配置与字幕，坚持走 ASR。"""
    run_dir = _seed_subtitle_run(tmp_path, SRT_FULL)
    _write(run_dir / "audio.wav", "fake-wav")
    _write(
        run_dir / "asr.raw.jsonl",
        json.dumps({"start": 0.0, "end": 12.0, "text": "ASR 文本。"}, ensure_ascii=False) + "\n",
    )
    _stop_at_outline(monkeypatch)
    lines: list[str] = []

    with pytest.raises(LlmError):
        run(URL, out_root=tmp_path, force_asr=True, progress=lines.append)

    assert "字幕优先" not in "\n".join(lines)


def test_pick_subtitle_prefers_chinese_and_checks_existence(tmp_path: Path):
    """多字幕并存时中文优先；meta 里记录的文件已删除则视为没有字幕。"""
    _write(tmp_path / "subtitle.en.srt", "1\n00:00:00,000 --> 00:00:01,000\nhello\n")
    _write(tmp_path / "subtitle.zh-CN.srt", SRT_FULL)
    subtitles = [
        {"lang": "en", "path": "subtitle.en.srt"},
        {"lang": "zh-CN", "path": "subtitle.zh-CN.srt"},
    ]

    picked = download.pick_subtitle(subtitles, tmp_path)
    assert picked is not None and picked.name == "subtitle.zh-CN.srt"
    assert download.pick_subtitle([{"lang": "zh-CN", "path": "gone.srt"}], tmp_path) is None
    assert download.pick_subtitle([], tmp_path) is None


def test_sources_label_is_honest_about_skipping_asr():
    """报告来源说明必须如实：字幕建稿时不得宣称调用过 ASR。"""
    from types import SimpleNamespace

    from videread.config import get_settings
    from videread.transcript import TranscriptUnit

    units = [
        TranscriptUnit(id="u0001", start=0.0, end=1.0, text="你好", source="subtitle"),
    ]
    meta = download.VideoMeta(bvid=BVID, title="t", uploader="u", duration=1.0, url=URL)
    outline = SimpleNamespace(profile="mechanism")
    settings = get_settings()

    subtitle_first = pipeline._sources(
        meta, outline, units, settings, mode="standard", subtitle_first=True
    )
    assert "未调用 ASR" in subtitle_first

    # 走 ASR 的路径即便字幕覆盖了全部单元，也不能说成「未调用 ASR」
    asr_path = pipeline._sources(
        meta, outline, units, settings, mode="standard", subtitle_first=False
    )
    assert "未调用 ASR" not in asr_path


def test_pipeline_rejects_missing_local_file(tmp_path: Path):
    with pytest.raises(UsageError):
        run(str(tmp_path / "not-here.m4a"), out_root=tmp_path, progress=lambda _msg: None)


def test_pipeline_rejects_over_long_local_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """本地文件此前不校验时长；统一到与远程一致的 2 小时上限。"""
    local = tmp_path / "long.m4a"
    local.write_bytes(b"fake")
    monkeypatch.setattr(pipeline.audio_mod, "probe_duration", lambda _p, _s: 3 * 3600)

    with pytest.raises(UsageError) as excinfo:
        run(str(local), out_root=tmp_path, progress=lambda _msg: None)

    assert "2 小时上限" in str(excinfo.value)
    # 被拒的视频不应在报告库留下 meta.json 条目
    assert list(tmp_path.glob("*/meta.json")) == []


def test_pipeline_rejects_unknown_mode(tmp_path: Path):
    with pytest.raises(UsageError):
        run(URL, mode="epic", out_root=tmp_path, progress=lambda _msg: None)


def test_normalize_source_expands_bare_bvid():
    assert download.normalize_source(BVID) == URL
    assert download.normalize_source(f"  {BVID}  ") == URL


def test_normalize_source_keeps_url_and_local_path(tmp_path: Path):
    assert download.normalize_source(URL) == URL
    local = tmp_path / "sample.m4a"
    assert download.normalize_source(f" {local} ") == str(local)
    # 不是完整 BV 号（长度不符）时不做补全
    assert download.normalize_source("BV1xx") == "BV1xx"


def test_bare_bvid_hits_cache_written_by_full_url():
    """裸 BV 号与完整链接必须落到同一 run 目录，否则缓存会分裂。"""
    assert download.make_run_id(download.normalize_source(BVID), BVID) == download.make_run_id(
        URL, BVID
    )


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


def test_wav_cleaned_up_after_successful_run(tmp_path: Path):
    """跑完自动清理 audio.wav —— 它占 run 目录体积的 99.9%，且 ASR 之后没人再读。"""
    run_dir = _seed_run(tmp_path)

    report = run(URL, out_root=tmp_path)

    assert report.exists()
    assert not (run_dir / "audio.wav").exists()
    # 清理只针对 wav：报告与各级缓存必须原样保留，否则重跑要重新付费
    assert (run_dir / "asr.raw.jsonl").exists()
    assert (run_dir / "transcript.jsonl").exists()
    assert (run_dir / "outline.json").exists()
    assert (run_dir / "sections" / "s1.html").exists()


def test_keep_audio_preserves_wav(tmp_path: Path):
    """勾选保留音频时不清理，便于排查问题。"""
    run_dir = _seed_run(tmp_path)

    run(URL, out_root=tmp_path, keep_audio=True)

    assert (run_dir / "audio.wav").exists()


def test_wav_kept_when_run_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """失败时保留 wav：错误回显会列出已生成产物供续跑，删了就续不上。"""
    run_dir = _seed_run(tmp_path)
    (run_dir / "sections" / "s1.html").unlink()  # 逼逐节写作真的跑起来

    class _BrokenLlm:
        def __init__(self, _settings: object) -> None:
            raise LlmError("模拟 LLM 不可用")

    monkeypatch.setattr(pipeline, "LlmClient", _BrokenLlm)

    with pytest.raises(VidereadError):
        run(URL, out_root=tmp_path)

    assert (run_dir / "audio.wav").exists()