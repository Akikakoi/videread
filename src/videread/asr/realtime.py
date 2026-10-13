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

长音频按静音点切段，多段在独立连接上**并行**推流（路数共用 ASR_MAX_CONCURRENCY，
与异步后端同一把旋钮）；每段结果立即落盘，重跑不重复付费。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import WebSocketException

from ..audio import cut_segment, detect_silence, plan_chunks, probe_duration
from ..config import ASR_REALTIME_SEGMENT_SECONDS, Settings
from ..errors import AsrError, CancelledError
from ..retry import retry_call
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
# 单帧发送后的让行间隔（秒）。取 0.1ms —— 远小于一帧 100ms 的真实时长，
# 足以把控制帧（ping / pong）与数据帧交错发出，又不至于明显拖慢推流；
# 真正的墙钟节拍由下面的 drift 计算统一控制，这里只负责让出事件循环。
_FRAME_YIELD_SEC = 0.0001
# 收尾预算（秒）：推完最后一帧后，服务端还要解码尾包并补发 task-finished。
# 这段不计入推流节拍的 deadline，单独给。
_DRAIN_TIMEOUT_SEC = 30.0
# 单段连接的墙钟预算余量（秒）：推流节拍之外的连接建立、解码排队都算在里面
_CONNECT_MARGIN_SEC = 60.0


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
        cancel_check: Callable[[], None] | None = None,
    ) -> None:
        self.settings = settings
        self.api_key = settings.require_dashscope()
        configured = settings.dashscope_model
        self.model = configured if "realtime" in configured else _DEFAULT_MODEL
        self.url = realtime_url(settings.dashscope_base_url)
        self.cache_dir = cache_dir
        self.progress = progress or (lambda _msg: None)
        self.cancel_check = cancel_check
        self.failures: list[dict[str, object]] = []

    def _checkpoint(self) -> None:
        """切段之间的取消检查点：抛 CancelledError 由上层归类。"""
        if self.cancel_check is not None:
            self.cancel_check()

    # ------------------------------------------------------------------ 接口

    def transcribe(
        self, audio: Path, *, duration: float | None = None
    ) -> list[AsrSegment]:
        total = duration if duration is not None else probe_duration(audio, self.settings)

        # 实时通道用自己（更短）的单段上限：一条连接要推完整段，
        # 段越长单次失败的重推代价越大
        segment_limit = (
            self.settings.asr_realtime_segment_seconds or ASR_REALTIME_SEGMENT_SECONDS
        )
        # 并行路数与异步后端共用 ASR_MAX_CONCURRENCY：每段是独立连接、
        # 独立任务，服务端按连接限流（RPM 充裕），并行互不干扰
        concurrency = max(1, int(self.settings.asr_max_concurrency or 1))

        if total <= segment_limit:
            chunks = [(0.0, total)]
        else:
            self._checkpoint()
            silences = detect_silence(audio, settings=self.settings)
            chunks = plan_chunks(total, silences, limit=segment_limit)
            self.progress(
                f"长音频切段：{len(chunks)} 段（{concurrency} 路并行 × "
                f"{self._speedup():g}x 倍速，"
                f"约 {self._estimate_seconds(total) / concurrency / 60:.1f} 分钟）"
            )

        if len(chunks) == 1:
            start, end = chunks[0]
            return self._transcribe_chunk(audio, 0, start, end)

        chunk_dir = (self.cache_dir or audio.parent) / "asr_chunks"
        chunk_dir.mkdir(parents=True, exist_ok=True)

        # 缓存命中的段不切段也不推流；未命中的先在本地切好（纯 ffmpeg 操作，
        # 串行足够快），再交给线程池并行推流
        results: dict[int, list[AsrSegment]] = {}
        tasks: list[tuple[int, float, float, Path]] = []
        for index, (start, end) in enumerate(chunks):
            self._checkpoint()
            cached = self._read_part(index, start, end)
            if cached is not None:
                self.progress(f"第 {index + 1} 段转写命中缓存，跳过")
                results[index] = offset_segments(cached, start)
                continue
            dest = chunk_dir / f"chunk.{index:03d}.wav"
            cut_segment(audio, dest, start, end, self.settings)
            tasks.append((index, start, end, dest))

        try:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = {
                    pool.submit(self._transcribe_chunk, path, index, start, end): index
                    for index, start, end, path in tasks
                }
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        self._checkpoint()
                        results[index] = future.result()
                    except CancelledError:
                        # 撤销还没开始的段；已完成段的结果已落盘（asr.part.*），
                        # 正在推流的段跑完当前连接后自然收尾，重跑可续
                        for pending in futures:
                            pending.cancel()
                        raise
                    except Exception as exc:  # noqa: BLE001 - 单段失败不阻塞其他段
                        self.failures.append(
                            {"chunk": index, "error": f"{type(exc).__name__}: {exc}"}
                        )
        finally:
            for _index, _start, _end, path in tasks:
                path.unlink(missing_ok=True)

        missing = [i for i in range(len(chunks)) if i not in results]
        if missing and not results:
            raise AsrError(
                f"ASR 全部切段失败（{len(missing)}/{len(chunks)}）：{self.failures}"
            )
        if missing:
            self.progress(f"警告：{len(missing)} 段 ASR 失败，报告将缺少这部分内容")

        merged: list[AsrSegment] = []
        for index in sorted(results):
            merged.extend(results[index])
        return sort_segments(merged)

    # ------------------------------------------------------------------ 单段

    def _transcribe_chunk(
        self, audio: Path, index: int, start: float, end: float
    ) -> list[AsrSegment]:
        """推流一段并落盘缓存；在并发工作线程里执行。

        缓存写段内相对时间戳（与异步后端同一套语义），返回值已加段偏移；
        短音频路径 index=0、start=0.0，偏移是恒等变换，缓存文件名不变。
        """
        cached = self._read_part(index, start, end)
        if cached is not None:
            self.progress(f"第 {index + 1} 段转写命中缓存，跳过")
            return offset_segments(cached, start)

        segments = self._stream_with_retry(audio, index)
        if not segments:
            raise AsrError(f"第 {index + 1} 段 ASR 返回空结果：{audio.name}")

        self._write_part(index, segments, start, end)
        return offset_segments(segments, start)

    def _stream_with_retry(self, audio: Path, index: int) -> list[AsrSegment]:
        """单段推流带指数退避：连接级失败（keepalive 超时、连接被重置）多为
        瞬时抖动，重推该段即可；鉴权 / 欠费 / 参数错误由 is_retryable 拦下。
        重推按整段计费，attempts=3 与异步后端对齐，不让瞬时抖动丢整段内容。
        """

        def _once() -> list[AsrSegment]:
            return self._stream(audio)

        return retry_call(
            _once,
            attempts=3,
            base=1.0,
            on_retry=lambda attempt, delay, exc: self.progress(
                f"第 {index + 1} 段推流失败（{type(exc).__name__}），"
                f"{delay:.1f}s 后重试"
            ),
        )

    def _stream(self, audio: Path) -> list[AsrSegment]:
        sample_rate, total_seconds = _pcm_probe(audio)
        return asyncio.run(self._stream_async(audio, sample_rate, total_seconds, audio.name))

    def _speedup(self) -> float:
        return max(1.0, self.settings.asr_realtime_speedup or 1.0)

    def _estimate_seconds(self, audio_seconds: float) -> float:
        """按当前倍速估算推完这段音频的墙钟耗时（用于提示与超时预算）。"""
        return audio_seconds / self._speedup()

    async def _stream_async(
        self, audio: Path, sample_rate: int, total_seconds: float, name: str
    ) -> list[AsrSegment]:
        task_id = uuid.uuid4().hex
        collected: list[AsrSegment] = []
        failure: list[str] = []
        started = asyncio.Event()
        # 音频在句子中途结束时，服务端不会补发 sentence_end=true，
        # 这里记住最后一条中间结果，任务结束后兜底落盘，避免丢尾巴。
        tail: list[AsrSegment] = []
        speedup = self._speedup()
        # 推流节拍的预算：按倍速推完音频本身要这么久，再加连接与解码余量。
        # 关键点是这个预算必须随音频时长放大——固定 600s 的 ASR_SEGMENT_TIMEOUT_SEC
        # 在 5 分钟以上的段上必然不够，那正是 keepalive 超时的直接成因。
        budget = (
            self._estimate_seconds(total_seconds)
            + _CONNECT_MARGIN_SEC
            + _DRAIN_TIMEOUT_SEC
        )
        # 推流起点单独记一个墙钟基准，节拍与超时都相对它计算，避免相互纠缠
        push_started = time.monotonic()
        deadline = push_started + budget

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
                # 心跳间隔放大到 30s：默认 20s 在长段推流里过于敏感，
                # 服务端解码排队时偶发的 pong 延迟就会误判为假死连接。
                # 配合下面的匀速推流，这里放宽是安全的
                ping_interval=30.0,
                ping_timeout=90.0,
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

                # 边读边推：每次只从磁盘读 _BLOCK_SECONDS 秒再切成帧发送，
                # 2 小时音频不再整段载入内存（约 230MB）
                loop = asyncio.get_running_loop()
                step = max(2, int(sample_rate * _FRAME_SECONDS) * 2)  # 16bit 单声道
                block_frames = int(sample_rate * _BLOCK_SECONDS)
                # 匀速推流：服务端按真实时间消费音频，一次性灌完会让它的
                # 接收缓冲堆压、迟迟不回 pong，最终被判定连接假死而断开
                # （ConnectionClosedError: keepalive ping timeout）。
                # 节拍用「已推音频时长 / 倍速」换算成墙钟目标时刻，
                # 每帧都对着同一个基准算差值，误差不会逐帧累积。
                pushed = 0.0
                with wave.open(str(audio), "rb") as fh:
                    while True:
                        block = await loop.run_in_executor(
                            None, fh.readframes, block_frames
                        )
                        if not block:
                            break
                        for offset in range(0, len(block), step):
                            await ws.send(block[offset : offset + step])
                            pushed += _FRAME_SECONDS
                            # 每帧都让出事件循环：接收协程要在此期间读服务端的
                            # pong 与识别结果，整轮阻塞就没法回应心跳了
                            await asyncio.sleep(_FRAME_YIELD_SEC)
                            # 追平节拍：落后就立刻补发，超前则等到点再发
                            drift = pushed / speedup - (time.monotonic() - push_started)
                            if drift > 0:
                                await asyncio.sleep(drift)
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
    return _pcm_probe(audio)[0]


def _pcm_probe(audio: Path) -> tuple[int, float]:
    """校验 PCM 格式，返回 (采样率, 时长秒)。

    时长取自 wav 文件头的帧数而非重新探测：推流预算要按音频真实长度算，
    而这段音频刚从磁盘切出来，帧数就是最准的口径。
    """
    try:
        with wave.open(str(audio), "rb") as fh:
            channels = fh.getnchannels()
            width = fh.getsampwidth()
            rate = fh.getframerate()
            frames = fh.getnframes()
    except wave.Error as exc:
        raise AsrError(f"实时 ASR 只接受 PCM wav，读取失败：{audio.name}：{exc}") from exc

    if channels != 1 or width != 2:
        raise AsrError(
            f"实时 ASR 需要单声道 16bit PCM：{audio.name} "
            f"（当前 {channels} 声道 / {width * 8}bit）"
        )
    return rate, frames / float(rate)