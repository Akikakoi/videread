"""faster-whisper 本地实现：离线、免 API 费用（对应开发文档 §6.5 的可换 adapter）。

定位：不想为试看 / 长视频付 ASR 费时的兜底通道。faster-whisper（CTranslate2）
是**可选依赖**——装了才可用，未安装时给出明确的安装指引而不是裸 ImportError。

环境变量：
- ``LOCAL_ASR_MODEL``         模型规格：tiny/base/small/medium/large-v3（默认 small）
- ``LOCAL_ASR_DEVICE``        auto / cpu / cuda（默认 auto）
- ``LOCAL_ASR_COMPUTE_TYPE``  int8 / float16 / auto（默认 auto，CPU 上压到 int8）
- ``LOCAL_ASR_LANGUAGE``      语言提示，auto 表示交给模型自动识别（默认 auto）

模型首次运行会从 HuggingFace 下载；国内网络可设 ``HF_ENDPOINT=https://hf-mirror.com`` 走镜像。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from ..config import Settings
from ..errors import AsrError, CancelledError, UsageError
from .base import AsrSegment, sort_segments

#: 每 N 段向进度回调汇报一次，避免长视频把控制台日志刷爆
_PROGRESS_EVERY = 50

_INSTALL_HINT = (
    "本地转写需要安装 faster-whisper："
    'pip install -e ".[local]"（或 pip install faster-whisper）'
)


def _env(key: str, default: str) -> str:
    return os.environ.get(key, "").strip() or default


class LocalWhisperAsr:
    """faster-whisper 本地实现（模型懒加载：构造时不下载，首次转写才拉）。"""

    name = "local"

    def __init__(
        self,
        settings: Settings,
        *,
        cache_dir: Path | None = None,
        progress: Callable[[str], None] | None = None,
        cancel_check: Callable[[], None] | None = None,
    ) -> None:
        self.settings = settings
        self.cache_dir = cache_dir
        self.progress = progress or (lambda _msg: None)
        self.cancel_check = cancel_check
        self.failures: list[dict[str, object]] = []
        self.model_name = _env("LOCAL_ASR_MODEL", "small")
        self.device = _env("LOCAL_ASR_DEVICE", "auto")
        self.compute_type = _env("LOCAL_ASR_COMPUTE_TYPE", "auto")
        self.language = _env("LOCAL_ASR_LANGUAGE", "auto")
        self._model = None

    # ------------------------------------------------------------------ 模型

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # 可选依赖缺失：给安装指引而非裸 traceback
            raise UsageError(_INSTALL_HINT) from exc

        self.progress(
            f"加载本地模型 {self.model_name}（device={self.device}，"
            "首次运行需下载，可用 HF_ENDPOINT=https://hf-mirror.com 走镜像）"
        )
        try:
            self._model = WhisperModel(
                self.model_name,
                device=self.device,
                compute_type=self.compute_type,
            )
        except Exception as exc:  # noqa: BLE001 - 下载失败 / CUDA 不可用等统一归类
            raise AsrError(
                f"本地模型 {self.model_name} 加载失败：{type(exc).__name__}: {exc}"
                "（首次运行需从 HuggingFace 下载，国内网络可设 "
                "HF_ENDPOINT=https://hf-mirror.com）"
            ) from exc
        return self._model

    # ------------------------------------------------------------------ 接口

    def transcribe(
        self, audio: Path, *, duration: float | None = None
    ) -> list[AsrSegment]:
        model = self._load()
        self.progress(
            f"本地转写开始：{audio.name}"
            + (f"  时长 {duration:.0f}s" if duration else "")
        )

        # 2 小时音频一次性交给 VAD + 解码流，无需切段；取消检查点按段粒度执行
        segments_iter, info = model.transcribe(
            str(audio),
            language=None if self.language == "auto" else self.language,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
        )
        self.progress(
            f"检测到语音时长 {info.duration:.0f}s"
            f"（language={info.language}，probability={info.language_probability:.2f}）"
        )

        segments: list[AsrSegment] = []
        for segment in segments_iter:
            if self.cancel_check is not None:
                self.cancel_check()  # 本地转写免费：取消直接丢掉未完成部分
            text = segment.text.strip()
            if not text:
                continue
            segments.append(
                AsrSegment(start=float(segment.start), end=float(segment.end), text=text)
            )
            if len(segments) % _PROGRESS_EVERY == 0:
                self.progress(f"本地转写进行中：已出 {len(segments)} 段")

        if not segments:
            raise AsrError(f"本地转写没有产出任何分段：{audio.name}")
        return sort_segments(segments)
