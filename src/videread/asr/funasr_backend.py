"""FunASR 本地实现：paraformer-zh 中文专用、离线、免 API 费用。

定位：中文视频的本地转写首选。对比 faster-whisper（通用多语模型）：
- 中文准确率更高，标点由 ct-punc 模型补全（whisper 靠模型猜）；
- 自带 VAD（fsmn-vad）+ 时间戳，长音频内部自动切分，无需外部切段；
- 模型经 ModelScope 国内源下载，不依赖 HuggingFace 镜像。

FunASR 是**可选依赖**——装了才可用，未安装时给出明确的安装指引而不是裸 ImportError：
pip install -e ".[funasr]"

环境变量：
- ``FUNASR_MODEL``       主模型（默认 paraformer-zh；ModelScope 模型名）
- ``FUNASR_VAD_MODEL``   VAD 模型（默认 fsmn-vad；设为空串关闭）
- ``FUNASR_PUNC_MODEL``  标点模型（默认 ct-punc；设为空串关闭）
- ``FUNASR_DEVICE``      auto / cpu / cuda（默认 auto）

注意：FunASR 的 ``generate()`` 是单次阻塞调用，取消只能在调用边界生效
（调用前 / 返回后各检查一次），不像云端切段可以段间停——本地转写免费，
最坏情况只是多等一会儿。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable

from ..config import PROJECT_ROOT, Settings
from ..errors import AsrError, UsageError
from .base import AsrSegment, sort_segments

#: 模型缓存默认放项目目录（.models/modelscope）：跟项目走、不散落用户目录、
#: 也不进 git（.gitignore 已排除）。用户设了 MODELSCOPE_CACHE / FUNASR_CACHE_DIR
#: 时以其为准。modelscope 在 import 时读取 MODELSCOPE_CACHE，
#: 因此必须在 `from funasr import ...` 之前落环境变量。
DEFAULT_MODEL_CACHE = PROJECT_ROOT / ".models" / "modelscope"

_INSTALL_HINT = (
    "本地转写需要安装 FunASR："
    'pip install -e ".[funasr]"（或 pip install funasr modelscope torch torchaudio）'
)


def _env(key: str, default: str) -> str:
    return os.environ.get(key, "").strip() or default


class FunAsrAsr:
    """FunASR 本地实现（模型懒加载：构造时不下载，首次转写才拉）。"""

    name = "funasr"

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
        self.model_name = _env("FUNASR_MODEL", "paraformer-zh")
        self.vad_model = _env("FUNASR_VAD_MODEL", "fsmn-vad")
        self.punc_model = _env("FUNASR_PUNC_MODEL", "ct-punc")
        self.device = _env("FUNASR_DEVICE", "auto")
        self._model = None

    # ------------------------------------------------------------------ 模型

    def _resolve_device(self) -> str:
        """FunASR 只认 cpu / cuda 等具体设备名，auto 在这里解析掉。"""
        if self.device != "auto":
            return self.device
        try:
            import torch

            return "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            return "cpu"

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from funasr import AutoModel
        except ImportError as exc:  # 可选依赖缺失：给安装指引而非裸 traceback
            raise UsageError(_INSTALL_HINT) from exc

        cache_dir = _env("FUNASR_CACHE_DIR", str(DEFAULT_MODEL_CACHE))
        if not os.environ.get("MODELSCOPE_CACHE"):
            os.environ["MODELSCOPE_CACHE"] = cache_dir

        device = self._resolve_device()
        self.progress(
            f"加载本地模型 {self.model_name}"
            + (f" + {self.vad_model}" if self.vad_model else "")
            + (f" + {self.punc_model}" if self.punc_model else "")
            + f"（device={device}，首次运行经 ModelScope 下载）"
        )
        try:
            self._model = AutoModel(
                model=self.model_name,
                vad_model=self.vad_model or None,
                vad_kwargs={"max_single_segment_time": 30000},
                punc_model=self.punc_model or None,
                device=device,
                disable_update=True,
                disable_pbar=True,
            )
        except Exception as exc:  # noqa: BLE001 - 下载失败 / CUDA 不可用等统一归类
            raise AsrError(
                f"本地模型 {self.model_name} 加载失败：{type(exc).__name__}: {exc}"
                "（首次运行经 ModelScope 下载，请检查网络）"
            ) from exc
        return self._model

    # ------------------------------------------------------------------ 接口

    def transcribe(
        self, audio: Path, *, duration: float | None = None
    ) -> list[AsrSegment]:
        if self.cancel_check is not None:
            self.cancel_check()  # 调用边界检查点
        model = self._load()
        self.progress(
            f"本地转写开始：{audio.name}"
            + (f"  时长 {duration:.0f}s" if duration else "")
        )
        try:
            results = model.generate(
                input=str(audio),
                batch_size_s=300,
            )
        except Exception as exc:  # noqa: BLE001 - 统一归类为 ASR 失败
            raise AsrError(
                f"FunASR 转写失败：{type(exc).__name__}: {exc}"
            ) from exc
        if self.cancel_check is not None:
            self.cancel_check()

        segments = self._to_segments(results, audio)
        if not segments:
            raise AsrError(f"本地转写没有产出任何分段：{audio.name}")
        return sort_segments(segments)

    # ------------------------------------------------------------------ 解析

    #: 句末 / 句中停顿标点：切分转写段的锚点
    _STRONG_PUNCT = "。！？；!?;…"
    _WEAK_PUNCT = "，、：,:"
    _PUNCT_ALL = set(_STRONG_PUNCT + _WEAK_PUNCT + "、·—“”\"'（）()《》<>【】[]{} ")
    #: 英数串算一个对齐单元（BPE 里一个 token 常覆盖整个词，如 "KV" / "16k"）
    _UNIT_RE = re.compile(r"[A-Za-z0-9]+|[^A-Za-z0-9\s]")

    def _to_segments(self, results: object, audio: Path) -> list[AsrSegment]:
        """FunASR 输出 → AsrSegment 列表。

        funasr ≥1.4 的 paraformer 全家桶返回 ``text``（标点模型补全后的全文）
        + ``token`` 级 ``timestamp``（毫秒 [start, end] 数组），没有现成的
        sentence_info：这里自己把 token 时间戳顺序对齐到文本、按标点切句。
        英数串按词对齐；数量差在容差内时吸收偏差（时间戳保持单调），宁可
        轻微漂移也不凭空造时间。
        """
        segments: list[AsrSegment] = []
        for item in results or []:  # type: ignore[union-attr]
            if not isinstance(item, dict):
                continue
            if item.get("sentence_info"):
                segments.extend(self._from_sentence_info(item["sentence_info"]))
            elif item.get("text") and item.get("timestamp"):
                segments.extend(
                    self._align_and_split(item["text"], item["timestamp"], audio)
                )
        if not segments:
            raise AsrError(
                f"FunASR 未返回可用的时间戳（{audio.name}）："
                "请保持 FUNASR_VAD_MODEL / FUNASR_PUNC_MODEL 开启，"
                "报告需要句级时间戳"
            )
        return segments

    def _from_sentence_info(self, sentences: list) -> list[AsrSegment]:
        """旧版输出路径：sentence_info 已是句级时间戳（毫秒）。"""
        segments: list[AsrSegment] = []
        for sentence in sentences:
            text = str(sentence.get("text", "")).strip()
            if not text:
                continue
            segments.append(
                AsrSegment(
                    start=float(sentence.get("start", 0)) / 1000.0,
                    end=float(sentence.get("end", 0)) / 1000.0,
                    text=text,
                )
            )
        return segments

    def _align_and_split(
        self, text: str, timestamps: list, audio: Path
    ) -> list[AsrSegment]:
        """token 时间戳顺序对齐文本，按标点切成句级段落。

        时间戳数组对应标点补全**之前**的 token 序列：punc 模型只插入标点、
        不改写文字，因此按「非标点单元」顺序对齐即可。英文/数字串是 BPE
        合并的（一个 token 覆盖整个词），按词计一个单元；个别 token 覆盖
        两个字符时会有 ±1 级偏差，顺序吸收，时间戳始终单调。
        """
        units = [
            (match.group(0), match.start(), match.end())
            for match in self._UNIT_RE.finditer(text)
            if match.group(0)[0] not in self._PUNCT_ALL
        ]
        if not units or not timestamps:
            return []

        drift = abs(len(units) - len(timestamps))
        if drift > max(2, len(units) // 50):
            self.progress(
                f"警告：时间戳与文本对齐存在 {drift} 个 token 偏差，"
                "段落时间可能有轻微漂移"
            )

        segments: list[AsrSegment] = []
        seg_start: float | None = None
        seg_end = 0.0
        parts: list[str] = []
        cursor = 0
        for index, (unit, start, end) in enumerate(units):
            token = timestamps[min(index, len(timestamps) - 1)]
            token_start, token_end = float(token[0]) / 1000.0, float(token[1]) / 1000.0
            if seg_start is None:
                seg_start = token_start
            seg_end = token_end
            parts.append(text[cursor:end])
            cursor = end
            # 单元后紧跟标点 → 顺势收句（标点归入当前句文本）
            nxt = text[end : end + 1]
            if seg_start is not None and (
                nxt in self._STRONG_PUNCT or nxt in self._WEAK_PUNCT
            ):
                segments.append(
                    AsrSegment(
                        start=seg_start,
                        end=seg_end,
                        text=("".join(parts) + nxt).strip(),
                    )
                )
                seg_start, parts = None, []
                cursor = end + 1
        # 收尾 1：循环外还有未收句的文本（无句末标点的尾巴）
        if seg_start is not None and parts:
            segments.append(
                AsrSegment(start=seg_start, end=seg_end, text="".join(parts).strip())
            )
        # 收尾 2：token 比单元多时，多余时间归入最后一段（保持覆盖到音频末尾）
        if segments and len(timestamps) > len(units):
            last = segments[-1]
            segments[-1] = AsrSegment(
                start=last.start,
                end=max(last.end, float(timestamps[-1][1]) / 1000.0),
                text=last.text,
            )
        return [s for s in segments if s.text]
