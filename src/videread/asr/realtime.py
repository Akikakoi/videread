"""Paraformer 实时（WebSocket）实现（对应开发文档 §6.5）。

与 `dashscope.py` 的异步实现互补：直接把本地音频以二进制帧推给服务端，
**不经过 OSS 上传桶**，因此不受「对象归属校验」限制，适用于只发放工作空间
`sk-ws-` 凭据、异步上传路径不可用的账号。

协议要点：
- 握手头 `Authorization: Bearer <key>`；
- 依次发送 `run-task`（JSON）→ 二进制音频帧（PCM）→ `finish-task`（JSON）；
- 服务端事件：`task-started` / `result-generated` / `task-finished` / `task-failed`；
- 只取 `sentence_end` 为真的句子，时间戳由毫秒换算为秒。

收发必须并发（一边推流一边收识别结果），因此这里用 asyncio 版 WebSocket 客户端。

长音频按静音点切段并**顺序**提交；每段结果立即落盘，重跑不重复付费。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
import wave
from pathlib import Path
from typing import Callable

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import WebSocketException

from ..audio import cut_segment, detect_silence, plan_chunks, probe_duration
from ..config import ASR_SEGMENT_SECONDS, ASR_SEGMENT_TIMEOUT_SEC, Settings
from ..errors import AsrError
from .base import AsrSegment, offset_segments, sort_segments, write_raw_jsonl

# 实时接口与异步接口不在同一个路径下：/api/v1 → /api-ws/v1/inference
_ASYNC_PATH_SUFFIX = "/api/v1"
_WS_PATH_SUFFIX = "/api-ws/v1/inference"

_DEFAULT_MODEL = "paraformer-realtime-v2"

# 每个二进制帧承载的音频时长（秒）；100ms 是官方示例的粒度
_FRAME_SECONDS = 0.1
# 每次从磁盘读取的块时长（秒）；块内再切成帧，避免整段 PCM 常驻内存
_BLOCK_SECONDS = 5.0
# 等待 task-started 的上限（秒）
_START_TIMEOUT_SEC = 30.0


def realtime_url(base_url: str) -> str:
    """由异步接入点推导 WebSocket 地址（§6.5）。"""
    base = base_url.strip().rstrip("/")
    for suffix in (_ASYNC_PATH_SUFFIX, "/compatible-mode/v1"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    return f"{base}{_WS_PATH_SUFFIX}"


class DashScopeRealtimeAsr:
    """通义 Paraformer 实时（WebSocket）实现。"""

    name = "dashscope-realtime"

    def __init__(
        self,
        settings: Settings,
        *,
        cache_dir: Path | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self.api_key = settings.require_dashscope()
        configured = settings.dashscope_model
        self.model = configured if "realtime" in configured else _DEFAULT_MODEL
        self.url = realtime_url(settings.dashscope_base_url)
        self.cache_dir = cache_dir
        self.progress = progress or (lambda _msg: None)
        self.failures: list[dict[str, object]] = []

    # ------------------------------------------------------------------ 接口

    def transcribe(
        self, audio: Path, *, duration: float | None = None
    ) -> list[AsrSegment]:
        total = duration if duration is not None else probe_duration(audio, self.settings)

        if total <= ASR_SEGMENT_SECONDS:
            # 短音频单段直推；缓存语义与下方长音频切段一致，
            # 分段文件名为 asr.part.000.<0>-<total>，重跑免重复付费
            cached = self._read_part(0, 0.0, total)
            if cached is not None:
                self.progress("转写命中缓存，跳过")
                return cached
            segments = self._stream(audio)
            if not segments:
                raise AsrError(f"ASR 返回空结果：{audio.name}")
            self._write_part(0, segments, 0.0, total)
            return segments

        silences = detect_silence(audio, settings=self.settings)
        chunks = plan_chunks(total, silences, limit=ASR_SEGMENT_SECONDS)
        self.progress(f"长音频切段：{len(chunks)} 段（实时接口顺序提交）")

        chunk_dir = (self.cache_dir or audio.parent) / "asr_chunks"
        chunk_dir.mkdir(parents=True, exist_ok=True)

        merged: list[AsrSegment] = []
        for index, (start, end) in enumerate(chunks):
            dest = chunk_dir / f"chunk.{index:03d}.wav"
            try:
                # 缓存判断只做一次：命中则跳过切段与推流，未命中才落到磁盘
                cached = self._read_part(index, start, end)
                if cached is not None:
                    self.progress(f"第 {index + 1} 段转写命中缓存，跳过")
                    merged.extend(offset_segments(cached, start))
                    continue
                cut_segment(audio, dest, start, end, self.settings)
                segments = self._stream(dest)
                if not segments:
                    raise AsrError(f"第 {index + 1} 段 ASR 返回空结果：{dest.name}")
                self._write_part(index, segments, start, end)
                merged.extend(offset_segments(segments, start))
            except Exception as exc:  # noqa: BLE001 - 单段失败不阻塞其他段
                self.failures.append(
                    {"chunk": index, "error": f"{type(exc).__name__}: {exc}"}
                )
            finally:
                dest.unlink(missing_ok=True)

        if not merged:
            raise AsrError(
                f"ASR 全部切段失败（{len(self.failures)}/{len(chunks)}）：{self.failures}"
            )
        if self.failures:
            self.progress(f"警告：{len(self.failures)} 段 ASR 失败，报告将缺少这部分内容")
        return sort_segments(merged)

    # ------------------------------------------------------------------ 单段

    def _stream(self, audio: Path) -> list[AsrSegment]:
        sample_rate = _pcm_sample_rate(audio)
        return asyncio.run(self._stream_async(audio, sample_rate, audio.name))

    async def _stream_async(
        self, audio: Path, sample_rate: int, name: str
    ) -> list[AsrSegment]:
        task_id = uuid.uuid4().hex
        collected: list[AsrSegment] = []
        failure: list[str] = []
        started = asyncio.Event()
        # 音频在句子中途结束时，服务端不会补发 sentence_end=true，
        # 这里记住最后一条中间结果，任务结束后兜底落盘，避免丢尾巴。
        tail: list[AsrSegment] = []

        async def _receive(ws) -> None:  # noqa: ANN001 - websockets 连接对象
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                header = message.get("header") or {}
                event = header.get("event")
                if event == "task-started":
                    started.set()
                elif event == "result-generated":
                    payload = message.get("payload") or {}
                    sentence = (payload.get("output") or {}).get("sentence") or {}
                    text = str(sentence.get("text", "")).strip()
                    if not text:
                        continue
                    # 中间结果（sentence_end=false）的 end_time 往往是 None，
                    # 不能直接 float()；缺失时退化为 begin_time，保证 end >= start。
                    start = _seconds(sentence.get("begin_time"))
                    segment = AsrSegment(
                        start=start,
                        end=max(start, _seconds(sentence.get("end_time"))),
                        text=text,
                    )
                    tail.clear()
                    if sentence.get("sentence_end"):
                        collected.append(segment)
                    else:
                        tail.append(segment)
                elif event == "task-failed":
                    failure.append(str(header.get("error_message") or "task-failed"))
                    return
                elif event == "task-finished":
                    return

        try:
            async with ws_connect(
                self.url,
                additional_headers={"Authorization": f"Bearer {self.api_key}"},
                max_size=None,
                open_timeout=_START_TIMEOUT_SEC,
                close_timeout=5.0,
            ) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "header": {
                                "action": "run-task",
                                "task_id": task_id,
                                "streaming": "duplex",
                            },
                            "payload": {
                                "task_group": "audio",
                                "task": "asr",
                                "function": "recognition",
                                "model": self.model,
                                "parameters": {"format": "pcm", "sample_rate": sample_rate},
                                "input": {},
                            },
                        }
                    )
                )
                receiver = asyncio.create_task(_receive(ws))
                try:
                    await asyncio.wait_for(started.wait(), timeout=_START_TIMEOUT_SEC)
                except asyncio.TimeoutError as exc:
                    receiver.cancel()
                    raise AsrError(f"实时 ASR 未收到 task-started：{name}") from exc

                deadline = time.monotonic() + ASR_SEGMENT_TIMEOUT_SEC
                # 边读边推：每次只从磁盘读 _BLOCK_SECONDS 秒再切成帧发送，
                # 2 小时音频不再整段载入内存（约 230MB）
                loop = asyncio.get_running_loop()
                step = max(2, int(sample_rate * _FRAME_SECONDS) * 2)  # 16bit 单声道
                block_frames = int(sample_rate * _BLOCK_SECONDS)
                with wave.open(str(audio), "rb") as fh:
                    while True:
                        block = await loop.run_in_executor(
                            None, fh.readframes, block_frames
                        )
                        if not block:
                            break
                        for offset in range(0, len(block), step):
                            await ws.send(block[offset : offset + step])
                            if time.monotonic() > deadline:
                                receiver.cancel()
                                raise AsrError(f"实时 ASR 推流超时：{name}")

                await ws.send(
                    json.dumps(
                        {
                            "header": {
                                "action": "finish-task",
                                "task_id": task_id,
                                "streaming": "duplex",
                            },
                            "payload": {"input": {}},
                        }
                    )
                )
                try:
                    await asyncio.wait_for(
                        receiver, timeout=max(1.0, deadline - time.monotonic())
                    )
                except asyncio.TimeoutError as exc:
                    receiver.cancel()
                    raise AsrError(f"实时 ASR 未收到 task-finished：{name}") from exc
        except AsrError:
            raise
        except WebSocketException as exc:
            raise AsrError(f"实时 ASR 连接失败：{type(exc).__name__}: {exc}") from exc

        if failure:
            raise AsrError(f"实时 ASR 任务失败：{failure[0]}")
        return collected + tail

    # ------------------------------------------------------------------ 缓存

    def _part_path(self, index: int, start: float, end: float) -> Path | None:
        if self.cache_dir is None:
            return None
        # 文件名带切段边界：静音点漂移导致切段点变化后，旧缓存自动失效
        return self.cache_dir / f"asr.part.{index:03d}.{start:.0f}-{end:.0f}.jsonl"

    def _read_part(self, index: int, start: float, end: float) -> list[AsrSegment] | None:
        path = self._part_path(index, start, end)
        if path is None or not path.is_file():
            return None
        from .base import load_raw_jsonl

        try:
            segments = load_raw_jsonl(path)
        except AsrError:
            return None
        return segments or None

    def _write_part(
        self, index: int, segments: list[AsrSegment], start: float, end: float
    ) -> None:
        path = self._part_path(index, start, end)
        if path is not None:
            write_raw_jsonl(segments, path)


def _seconds(value: float | int | str | None) -> float:
    """服务端时间戳（毫秒）→ 秒；中间结果里可能是 None，此时返回 0.0。"""
    if value is None:
        return 0.0
    try:
        return float(value) / 1000.0
    except (TypeError, ValueError):
        return 0.0


def _pcm_sample_rate(audio: Path) -> int:
    """校验 wav 为单声道 16bit PCM 并返回采样率（只读文件头，不载入数据）。"""
    try:
        with wave.open(str(audio), "rb") as fh:
            channels = fh.getnchannels()
            width = fh.getsampwidth()
            rate = fh.getframerate()
    except wave.Error as exc:
        raise AsrError(f"实时 ASR 只接受 PCM wav，读取失败：{audio.name}：{exc}") from exc

    if channels != 1 or width != 2:
        raise AsrError(
            f"实时 ASR 需要单声道 16bit PCM：{audio.name} "
            f"（当前 {channels} 声道 / {width * 8}bit）"
        )
    return rate