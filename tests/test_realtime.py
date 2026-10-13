"""realtime 后端用例（全部离线）：短音频单段路径、分段缓存与推流节拍。

回归背景：重构曾把 `_transcribe_chunk` 内联进长音频循环但漏改短音频分支，
导致 ≤30 分钟的音频一进实时通道就 AttributeError。这里用 monkeypatch 掉
`_stream`（不碰真实 WebSocket）锁住该路径的行为。

第二个回归背景：推流循环原本不限速，把整段音频以本地磁盘读取速度灌进
服务端。服务端来不及消费、迟迟不回 pong，客户端心跳判定连接假死，
长视频稳定复现 `ConnectionClosedError: sent 1011 ... keepalive ping timeout`。
`test_stream_paces_frames_against_wall_clock` 用假 WebSocket 锁住匀速节拍。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import wave
from dataclasses import replace
from pathlib import Path

import pytest

from videread.asr import realtime as realtime_mod
from videread.asr.base import AsrSegment
from videread.asr.realtime import DashScopeRealtimeAsr, _pcm_probe
from videread.config import get_settings
from videread.errors import AsrError


def make_backend(tmp_path: Path) -> DashScopeRealtimeAsr:
    return DashScopeRealtimeAsr(get_settings(), cache_dir=tmp_path)


SEGMENTS = [AsrSegment(start=0.0, end=2.0, text="第一句"), AsrSegment(start=2.0, end=4.0, text="第二句")]


def test_short_audio_streams_and_writes_part_cache(tmp_path: Path):
    backend = make_backend(tmp_path)
    monkey_stream = lambda _audio: list(SEGMENTS)  # noqa: E731 - 测试替身
    backend._stream = monkey_stream  # type: ignore[method-assign]

    result = backend.transcribe(Path("fake.wav"), duration=100.0)

    assert [(s.start, s.text) for s in result] == [(0.0, "第一句"), (2.0, "第二句")]
    assert (tmp_path / "asr.part.000.0-100.jsonl").is_file()


def test_short_audio_cache_hit_skips_stream(tmp_path: Path):
    backend = make_backend(tmp_path)
    backend._write_part(0, SEGMENTS, 0.0, 100.0)

    def _boom(_audio: Path) -> list[AsrSegment]:
        raise AssertionError("缓存命中时不应再推流")

    backend._stream = _boom  # type: ignore[method-assign]
    result = backend.transcribe(Path("fake.wav"), duration=100.0)

    assert [(s.start, s.text) for s in result] == [(0.0, "第一句"), (2.0, "第二句")]


def test_short_audio_empty_result_raises(tmp_path: Path):
    backend = make_backend(tmp_path)
    backend._stream = lambda _audio: []  # type: ignore[method-assign]

    with pytest.raises(AsrError, match="空结果"):
        backend.transcribe(Path("fake.wav"), duration=100.0)
    # 失败不落缓存
    assert not (tmp_path / "asr.part.000.0-100.jsonl").exists()


# --------------------------------------------------------------- 推流节拍


class _FakeWs:
    """够用的假 WebSocket：记录发出的帧，按脚本回放服务端事件。

    接收协程在 run-task 之后必须一直挂着——真实连接在推流期间不会返回任何
    东西，正是这段「服务端沉默期」把客户端饿死到心跳超时。所以这里用
    `_finished` 事件把 `__anext__` 挂住，直到收到 finish-task 才收尾。
    """

    def __init__(self) -> None:
        self.sent: list[bytes | str] = []
        self._queue: list[str] = []
        self._finished = asyncio.Event()

    async def send(self, message: bytes | str) -> None:
        self.sent.append(message)
        text = message if isinstance(message, str) else message.decode("utf-8", "ignore")
        if '"run-task"' in text:
            self._queue.append(json.dumps({"header": {"event": "task-started"}}))
        elif '"finish-task"' in text:
            self._queue.append(
                json.dumps(
                    {
                        "header": {"event": "result-generated"},
                        "payload": {
                            "output": {
                                "sentence": {
                                    "text": "识别结果",
                                    "begin_time": 0,
                                    "end_time": 1000,
                                    "sentence_end": True,
                                }
                            }
                        },
                    }
                )
            )
            self._queue.append(json.dumps({"header": {"event": "task-finished"}}))
            self._finished.set()

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        while not self._queue:
            await self._finished.wait()
        return self._queue.pop(0)


class _FakeConnect:
    def __init__(self, ws: _FakeWs) -> None:
        self.ws = ws

    def __call__(self, *args: object, **kwargs: object):  # noqa: ANN204 - 替身
        return self

    async def __aenter__(self) -> _FakeWs:
        return self.ws

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _write_pcm(path: Path, seconds: float, rate: int = 16000) -> Path:
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        fh.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


def test_stream_paces_frames_against_wall_clock(tmp_path: Path, monkeypatch):
    """核心回归：限速推流时，墙钟耗时必须接近音频时长，而不是本地读取耗时。"""
    audio = _write_pcm(tmp_path / "a.wav", seconds=2.0)
    ws = _FakeWs()
    monkeypatch.setattr(realtime_mod, "ws_connect", _FakeConnect(ws))

    # 显式钉住 1.0 倍速：节拍断言以「音频时长」为基准，不随 .env 的倍速配置漂移
    settings = replace(get_settings(), asr_realtime_speedup=1.0)
    backend = DashScopeRealtimeAsr(settings, cache_dir=tmp_path)
    started = time.monotonic()
    segments = backend.transcribe(audio, duration=2.0)
    elapsed = time.monotonic() - started

    assert [s.text for s in segments] == ["识别结果"]
    # 2 秒音频按 1.0 倍速推，墙钟至少要 2 秒；给 0.6s 容差容纳调度抖动
    assert elapsed >= 1.4, f"推流过快：2s 音频只用了 {elapsed:.2f}s，未做匀速节拍"
    # 每帧 100ms，2 秒音频 = 20 帧
    binary = [s for s in ws.sent if isinstance(s, bytes)]
    assert len(binary) == 20


def test_stream_speedup_shortens_wall_clock(tmp_path: Path, monkeypatch):
    """倍速 >1 时墙钟耗时按比例缩短，且时长仍被正确识别。"""
    audio = _write_pcm(tmp_path / "b.wav", seconds=2.0)
    ws = _FakeWs()
    monkeypatch.setattr(realtime_mod, "ws_connect", _FakeConnect(ws))

    settings = replace(get_settings(), asr_realtime_speedup=4.0)
    backend = DashScopeRealtimeAsr(settings, cache_dir=tmp_path)
    started = time.monotonic()
    backend.transcribe(audio, duration=2.0)
    elapsed = time.monotonic() - started

    # 4 倍速推 2 秒音频约需 0.5s；给上下界，既证明提速生效又不会被抖动误伤
    assert elapsed < 1.4, f"倍速未生效：4x 推 2s 音频仍用了 {elapsed:.2f}s"


def test_pcm_probe_rejects_non_mono(tmp_path: Path):
    stereo = tmp_path / "stereo.wav"
    with wave.open(str(stereo), "wb") as fh:
        fh.setnchannels(2)
        fh.setsampwidth(2)
        fh.setframerate(16000)
        fh.writeframes(b"\x00\x00" * 3200)

    with pytest.raises(AsrError, match="单声道"):
        _pcm_probe(stereo)


def test_pcm_probe_returns_rate_and_duration(tmp_path: Path):
    audio = _write_pcm(tmp_path / "c.wav", seconds=1.5)
    rate, seconds = _pcm_probe(audio)

    assert rate == 16000
    assert seconds == pytest.approx(1.5, abs=0.01)


def test_realtime_segment_limit_defaults_short(tmp_path: Path, monkeypatch):
    """实时通道的单段上限必须显著短于异步通道（1800s），否则长视频又是长连接。"""
    monkeypatch.delenv("ASR_REALTIME_SEGMENT_SECONDS", raising=False)
    settings = get_settings()

    assert settings.asr_realtime_segment_seconds <= 600.0

    audio = _write_pcm(tmp_path / "d.wav", seconds=1.0)
    backend = DashScopeRealtimeAsr(settings, cache_dir=tmp_path)
    calls: list[Path] = []

    def _fake_stream(path: Path) -> list[AsrSegment]:
        calls.append(path)
        return [AsrSegment(start=0.0, end=1.0, text="x")]

    backend._stream = _fake_stream  # type: ignore[method-assign]
    # 640s 超过实时通道的单段上限，必须走切段而不是单连接直推
    backend.transcribe(audio, duration=640.0)

    assert len(calls) > 1, "640s 音频应被切成多段顺序提交"


def test_speedup_floor_is_one(tmp_path: Path):
    """倍速小于 1 会被夹到 1.0：放慢推流只会拉长连接、放大超时风险。"""
    backend = DashScopeRealtimeAsr(get_settings(), cache_dir=tmp_path)
    assert backend._speedup() >= 1.0


# ------------------------------------------------------- 并行切段与重试


def _parallel_backend(tmp_path: Path) -> DashScopeRealtimeAsr:
    """3 段 × 100s 的配置：duration=300 恰好切出 3 段，3 路并行。"""
    settings = replace(
        get_settings(),
        asr_realtime_segment_seconds=100.0,
        asr_max_concurrency=3,
    )
    return DashScopeRealtimeAsr(settings, cache_dir=tmp_path)


def test_long_audio_streams_chunks_in_parallel(tmp_path: Path):
    """多段必须同时在推：3 段在 3 个工作线程里碰同一道栅栏。

    顺序提交时第二段开始推的时候第一段已经结束，栅栏永远凑不齐 3 方，
    会在超时后抛 BrokenBarrierError → 全部段记为失败 → 断言失败。
    """
    audio = _write_pcm(tmp_path / "par.wav", seconds=1.0)
    backend = _parallel_backend(tmp_path)
    barrier = threading.Barrier(3)

    def _fake_stream(_audio: Path) -> list[AsrSegment]:
        barrier.wait(timeout=30)
        return [AsrSegment(start=0.0, end=1.0, text="x")]

    backend._stream = _fake_stream  # type: ignore[method-assign]
    result = backend.transcribe(audio, duration=300.0)

    assert [(s.start, s.text) for s in result] == [
        (0.0, "x"),
        (100.0, "x"),
        (200.0, "x"),
    ]
    assert backend.failures == []


def test_chunk_failure_is_isolated(tmp_path: Path):
    """单段失败只丢该段：其余段照常合并，失败记录进 failures。"""
    audio = _write_pcm(tmp_path / "iso.wav", seconds=1.0)
    backend = _parallel_backend(tmp_path)

    def _fake_stream(path: Path) -> list[AsrSegment]:
        # 按切段文件名定位故障段，与并发下的调用顺序无关
        if "chunk.001" in path.name:
            raise AsrError("实时 ASR 任务失败：InvalidApiKey")  # 不可重试，测试提速
        return [AsrSegment(start=0.0, end=1.0, text="ok")]

    backend._stream = _fake_stream  # type: ignore[method-assign]
    result = backend.transcribe(audio, duration=300.0)

    assert [(s.start, s.text) for s in result] == [(0.0, "ok"), (200.0, "ok")]
    assert len(backend.failures) == 1
    assert backend.failures[0]["chunk"] == 1


def test_cached_chunks_skip_cut_and_stream(tmp_path: Path):
    """缓存命中的段不切段也不推流：重跑只补未完成的段。"""
    audio = _write_pcm(tmp_path / "cache.wav", seconds=1.0)
    backend = _parallel_backend(tmp_path)
    # 预写第 0、2 段的缓存（段内相对时间戳）
    backend._write_part(0, [AsrSegment(start=0.0, end=1.0, text="c0")], 0.0, 100.0)
    backend._write_part(2, [AsrSegment(start=0.0, end=1.0, text="c2")], 200.0, 300.0)
    streamed: list[str] = []

    def _fake_stream(path: Path) -> list[AsrSegment]:
        streamed.append(path.name)
        return [AsrSegment(start=0.0, end=1.0, text="live")]

    backend._stream = _fake_stream  # type: ignore[method-assign]
    result = backend.transcribe(audio, duration=300.0)

    assert [(s.start, s.text) for s in result] == [
        (0.0, "c0"),
        (100.0, "live"),
        (200.0, "c2"),
    ]
    assert streamed == ["chunk.001.wav"]


def test_transient_stream_failure_is_retried(tmp_path: Path):
    """连接级失败指数退避重推：第二次成功则整段不丢。"""
    backend = make_backend(tmp_path)
    calls = {"n": 0}

    def _flaky(_audio: Path) -> list[AsrSegment]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise AsrError("实时 ASR 连接失败：ConnectionClosedError keepalive ping timeout")
        return list(SEGMENTS)

    backend._stream = _flaky  # type: ignore[method-assign]
    result = backend.transcribe(Path("fake.wav"), duration=100.0)

    assert calls["n"] == 2
    assert [s.text for s in result] == ["第一句", "第二句"]


def test_auth_failure_is_not_retried(tmp_path: Path):
    """鉴权类错误重试无意义：立即失败，不空烧三次推流。"""
    backend = make_backend(tmp_path)
    calls = {"n": 0}

    def _boom(_audio: Path) -> list[AsrSegment]:
        calls["n"] += 1
        raise AsrError("实时 ASR 任务失败：InvalidApiKey")

    backend._stream = _boom  # type: ignore[method-assign]
    with pytest.raises(AsrError, match="InvalidApiKey"):
        backend.transcribe(Path("fake.wav"), duration=100.0)

    assert calls["n"] == 1
