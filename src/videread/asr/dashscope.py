"""通义 Paraformer 实现（对应开发文档 §6.5）。

流程：本地文件上传 OSS → 提交异步转写任务 → 轮询 → 拉取转写 JSON。
长音频按静音点切段并并行提交；每段结果**立即落盘**，重跑不重复付费。
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

import httpx

from ..audio import cut_segment, detect_silence, plan_chunks, probe_duration
from ..config import (
    ASR_MAX_CONCURRENCY,
    ASR_SEGMENT_SECONDS,
    ASR_SEGMENT_TIMEOUT_SEC,
    DASHSCOPE_BASE_URL,
    Settings,
)
from ..errors import AsrError
from ..retry import retry_call
from .base import AsrSegment, offset_segments, sort_segments, write_raw_jsonl

_UPLOAD_POLICY_PATH = "/uploads"
_TRANSCRIPTION_PATH = "/services/audio/asr/transcription"
_TASK_PATH = "/tasks"
_POLL_INTERVAL = 2.0
_POLL_MAX_INTERVAL = 10.0


class DashScopeAsr:
    """通义 Paraformer 实现。"""

    name = "dashscope"

    def __init__(
        self,
        settings: Settings,
        *,
        cache_dir: Path | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self.api_key = settings.require_dashscope()
        self.model = settings.dashscope_model
        self.language = settings.dashscope_language
        self.cache_dir = cache_dir
        self.progress = progress or (lambda _msg: None)
        self.failures: list[dict[str, object]] = []
        self._client = httpx.Client(
            base_url=settings.dashscope_base_url.rstrip("/") + "/",
            timeout=httpx.Timeout(60.0, read=120.0),
            proxy=settings.proxy,
            follow_redirects=True,
        )

    # ------------------------------------------------------------------ 接口

    def transcribe(
        self, audio: Path, *, duration: float | None = None
    ) -> list[AsrSegment]:
        total = duration if duration is not None else probe_duration(audio, self.settings)

        if total <= ASR_SEGMENT_SECONDS:
            chunks = [(0.0, total)]
        else:
            silences = detect_silence(audio, settings=self.settings)
            chunks = plan_chunks(total, silences, limit=ASR_SEGMENT_SECONDS)
            self.progress(f"长音频切段：{len(chunks)} 段（并发 {ASR_MAX_CONCURRENCY}）")

        if len(chunks) == 1:
            return self._transcribe_chunk(audio, 0, 0.0, total)

        chunk_dir = self._chunk_dir(audio)
        chunk_files: list[tuple[int, float, float, Path]] = []
        for index, (start, end) in enumerate(chunks):
            dest = chunk_dir / f"chunk.{index:03d}.wav"
            cut_segment(audio, dest, start, end, self.settings)
            chunk_files.append((index, start, end, dest))

        results: dict[int, list[AsrSegment]] = {}
        with ThreadPoolExecutor(max_workers=ASR_MAX_CONCURRENCY) as pool:
            futures = {
                pool.submit(self._transcribe_chunk, path, index, start, end): index
                for index, start, end, path in chunk_files
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    results[index] = future.result()
                except Exception as exc:  # noqa: BLE001 - 单段失败不阻塞其他段
                    self.failures.append(
                        {"chunk": index, "error": f"{type(exc).__name__}: {exc}"}
                    )

        for _index, _start, _end, path in chunk_files:
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
        cached = self._read_part(index, start, end)
        if cached is not None:
            self.progress(f"第 {index + 1} 段转写命中缓存，跳过")
            return offset_segments(cached, start)

        segments = self._transcribe_file(audio)
        if not segments:
            raise AsrError(f"第 {index + 1} 段 ASR 返回空结果：{audio.name}")

        self._write_part(index, segments, start, end)
        return offset_segments(segments, start)

    def _transcribe_file(self, audio: Path) -> list[AsrSegment]:
        file_url = self._upload(audio)
        task_id = self._submit(file_url)
        transcription_url = self._await_task(task_id)
        return self._fetch_transcription(transcription_url)

    # ------------------------------------------------------------------ 缓存

    def _chunk_dir(self, audio: Path) -> Path:
        directory = (self.cache_dir or audio.parent) / "asr_chunks"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _part_path(self, index: int, start: float, end: float) -> Path | None:
        if self.cache_dir is None:
            return None
        # 文件名带切段边界：静音点漂移导致切段点变化后，旧缓存自动失效，
        # 不会把上一个音频的段错位拼进新结果
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
        if path is None:
            return
        write_raw_jsonl(segments, path)

    # ------------------------------------------------------------------ HTTP

    def _headers(self, *, async_task: bool = False) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if async_task:
            headers["X-DashScope-Async"] = "enable"
        return headers

    def _check(self, response: httpx.Response, action: str) -> dict:
        if response.status_code >= 400:
            raise AsrError(
                f"DashScope {action} 失败（HTTP {response.status_code}）："
                f"{response.text[:500]}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise AsrError(f"DashScope {action} 返回非 JSON：{response.text[:200]}") from exc

    def _upload(self, audio: Path) -> str:
        def _do() -> str:
            policy_response = self._client.get(
                _UPLOAD_POLICY_PATH,
                params={"action": "getPolicy", "model": self.model},
                headers=self._headers(),
            )
            payload = self._check(policy_response, "getPolicy").get("data") or {}
            upload_host = payload.get("upload_host")
            if not upload_host:
                raise AsrError(f"DashScope getPolicy 未返回 upload_host：{payload}")

            key = f"{payload.get('upload_dir', '')}{audio.name}"
            form = {
                "key": key,
                "policy": payload.get("policy", ""),
                "OSSAccessKeyId": payload.get("oss_access_key_id", ""),
                "signature": payload.get("signature", ""),
                "success_action_status": "200",
                "x-oss-object-acl": payload.get("x_oss_object_acl", ""),
                "x-oss-forbid-overwrite": payload.get("x_oss_forbid_overwrite", ""),
            }
            with audio.open("rb") as fh:
                upload_response = self._client.post(
                    upload_host,
                    data=form,
                    files={"file": (audio.name, fh, "application/octet-stream")},
                )
            if upload_response.status_code >= 400:
                raise AsrError(
                    f"DashScope 文件上传失败（HTTP {upload_response.status_code}）："
                    f"{upload_response.text[:300]}"
                )
            return f"oss://{key}"

        return retry_call(_do, attempts=3, base=1.0)

    def _task_parameters(self) -> dict[str, object]:
        # 语言提示可经 DASHSCOPE_LANGUAGE 覆盖；auto 表示交给服务端自动识别，
        # 此时完全不传 language_hints（避免英文视频被强行按中文识别）
        parameters: dict[str, object] = {"channel_id": [0]}
        if self.language != "auto":
            parameters["language_hints"] = [self.language]
        return parameters

    def _submit(self, file_url: str) -> str:
        def _do() -> str:
            response = self._client.post(
                _TRANSCRIPTION_PATH,
                headers={
                    **self._headers(async_task=True),
                    "Content-Type": "application/json",
                    # 声明 file_urls 是上传接口返回的 oss:// 资源；缺这个头服务端读不到文件
                    "X-DashScope-OssResourceResolve": "enable",
                },
                json={
                    "model": self.model,
                    "input": {"file_urls": [file_url]},
                    "parameters": self._task_parameters(),
                },
            )
            output = self._check(response, "提交转写任务").get("output") or {}
            task_id = output.get("task_id")
            if not task_id:
                raise AsrError(f"DashScope 未返回 task_id：{output}")
            return str(task_id)

        return retry_call(_do, attempts=3, base=1.0)

    def _await_task(self, task_id: str) -> str:
        deadline = time.monotonic() + ASR_SEGMENT_TIMEOUT_SEC
        interval = _POLL_INTERVAL
        while True:
            response = self._client.get(f"{_TASK_PATH}/{task_id}", headers=self._headers())
            output = self._check(response, "查询转写任务").get("output") or {}
            status = output.get("task_status")

            if status == "SUCCEEDED":
                results = output.get("results") or []
                for item in results:
                    if item.get("subtask_status") == "SUCCEEDED" and item.get("transcription_url"):
                        return str(item["transcription_url"])
                raise AsrError(f"DashScope 任务成功但没有转写结果：{output}")
            if status in {"FAILED", "CANCELED"}:
                raise AsrError(
                    f"DashScope 转写任务 {status}："
                    f"{output.get('code')} {output.get('message')}"
                )
            if time.monotonic() > deadline:
                raise AsrError(
                    f"DashScope 转写任务超时（{ASR_SEGMENT_TIMEOUT_SEC:.0f}s）：{task_id}"
                )
            time.sleep(interval)
            interval = min(_POLL_MAX_INTERVAL, interval * 1.5)

    def _fetch_transcription(self, url: str) -> list[AsrSegment]:
        response = self._client.get(url)
        if response.status_code >= 400:
            raise AsrError(
                f"下载转写结果失败（HTTP {response.status_code}）：{response.text[:300]}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise AsrError("转写结果不是合法 JSON") from exc

        segments: list[AsrSegment] = []
        for transcript in payload.get("transcripts") or []:
            sentences = transcript.get("sentences") or []
            for sentence in sentences:
                text = str(sentence.get("text", "")).strip()
                if not text:
                    continue
                segments.append(
                    AsrSegment(
                        start=_to_seconds(sentence, "begin_time", "start"),
                        end=_to_seconds(sentence, "end_time", "end"),
                        text=text,
                    )
                )
            if not sentences and transcript.get("text"):
                duration = float(transcript.get("content_duration_in_milliseconds", 0)) / 1000
                segments.append(
                    AsrSegment(start=0.0, end=duration, text=str(transcript["text"]).strip())
                )
        if not segments:
            raise AsrError(f"转写结果没有可用分段：{json.dumps(payload)[:300]}")
        return segments


def _to_seconds(sentence: dict, ms_key: str, sec_key: str) -> float:
    if sentence.get(ms_key) is not None:
        return float(sentence[ms_key]) / 1000
    if sentence.get(sec_key) is not None:
        return float(sentence[sec_key])
    return 0.0