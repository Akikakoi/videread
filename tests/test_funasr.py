"""FunASR 本地转写后端用例（全部离线，不下载模型）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from videread.config import get_settings
from videread.errors import AsrError, UsageError

from videread.asr import get_backend
from videread.asr.funasr_backend import FunAsrAsr


def _backend() -> FunAsrAsr:
    return FunAsrAsr(get_settings())


# ------------------------------------------------------------------- 工厂


@pytest.mark.parametrize("name", ["funasr", "local-funasr", "paraformer-local"])
def test_factory_resolves_funasr_aliases(name: str):
    settings = get_settings(asr_backend=name)
    assert isinstance(get_backend(settings), FunAsrAsr)


def test_factory_error_lists_funasr():
    with pytest.raises(UsageError) as excinfo:
        get_backend(get_settings(asr_backend="nope"))
    assert "funasr" in str(excinfo.value)


# ------------------------------------------------------- 未安装依赖的指引


def test_missing_package_gives_install_hint(monkeypatch: pytest.MonkeyPatch):
    """funasr 未安装时给安装指引，而不是裸 ImportError。"""
    monkeypatch.setitem(sys.modules, "funasr", None)  # 让 import funasr 失败
    with pytest.raises(UsageError) as excinfo:
        _backend()._load()
    assert "funasr" in str(excinfo.value)


# ------------------------------------------------------------ 输出解析


def test_sentence_info_maps_milliseconds_to_seconds():
    backend = _backend()
    results = [
        {
            "text": "你好世界。第二句。",
            "sentence_info": [
                {"text": "你好世界。", "start": 100, "end": 1500},
                {"text": "第二句。", "start": 1600, "end": 3200},
            ],
        }
    ]
    segments = backend._to_segments(results, Path("audio.wav"))  # noqa: SLF001
    assert [(s.start, s.end, s.text) for s in segments] == [
        (0.1, 1.5, "你好世界。"),
        (1.6, 3.2, "第二句。"),
    ]


def test_empty_text_sentences_skipped():
    backend = _backend()
    results = [
        {
            "sentence_info": [
                {"text": "  ", "start": 0, "end": 100},
                {"text": "正文", "start": 100, "end": 900},
            ]
        }
    ]
    segments = backend._to_segments(results, Path("audio.wav"))  # noqa: SLF001
    assert len(segments) == 1 and segments[0].text == "正文"


def test_missing_sentence_info_raises_instead_of_faking_timestamps():
    """时间戳是回指原视频的锚点：缺了宁可不产出，不能凭空造。"""
    backend = _backend()
    with pytest.raises(AsrError) as excinfo:
        backend._to_segments([{"text": "只有全文没有时间戳"}], Path("audio.wav"))  # noqa: SLF001
    assert "时间戳" in str(excinfo.value)


# ------------------------------------------------- timestamp 对齐与切句


def test_align_and_split_by_punctuation():
    """token 级时间戳按标点切成句级段落，标点归入前句。"""
    backend = _backend()
    text = "你好。世界！"
    timestamps = [[0, 100], [100, 200], [200, 300], [300, 400]]
    segments = backend._align_and_split(text, timestamps, Path("a.wav"))  # noqa: SLF001
    assert [(s.start, s.end, s.text) for s in segments] == [
        (0.0, 0.2, "你好。"),
        (0.2, 0.4, "世界！"),
    ]


def test_align_and_split_latin_run_counts_as_one_unit():
    """英文/数字串（KV、16k）按词对齐，不按字符数。"""
    backend = _backend()
    text = "使用KV存储16k。"
    # 单元：使 用 KV 存 储 16k → 6 个，timestamp 给 6 个
    timestamps = [[i * 100, i * 100 + 80] for i in range(6)]
    segments = backend._align_and_split(text, timestamps, Path("a.wav"))  # noqa: SLF001
    assert len(segments) == 1
    assert segments[0].text == "使用KV存储16k。"
    assert segments[0].start == 0.0
    assert segments[0].end == 0.58


def test_align_and_split_absorbs_small_drift():
    """token 比单元多 / 少一两个时顺序吸收：多出的时间归末段，仍单调。"""
    backend = _backend()
    text = "一句话。两句话。"
    # 单元 5 个（一 句 话 两 句 话 = 6? 一/句/话/两/句/话 → 6），给 7 个 token
    timestamps = [[i * 100, i * 100 + 90] for i in range(7)]
    segments = backend._align_and_split(text, timestamps, Path("a.wav"))  # noqa: SLF001
    assert len(segments) == 2
    starts = [s.start for s in segments]
    assert starts == sorted(starts), "时间戳必须单调"
    assert segments[-1].end == 0.69  # 多余 token 的时间归入最后一段


def test_align_and_split_warns_on_large_drift():
    """偏差超过容差时给进度警告，但不失败。"""
    warnings: list[str] = []
    backend = _backend()
    backend.progress = warnings.append  # noqa: SLF001
    text = "你好。"  # 2 个单元
    timestamps = [[i * 100, i * 100 + 90] for i in range(20)]  # 偏差 18，超容差
    segments = backend._align_and_split(text, timestamps, Path("a.wav"))  # noqa: SLF001
    assert len(segments) == 1
    assert any("偏差" in w for w in warnings)


def test_transcribe_uses_injected_model_and_checks_cancel():
    calls: list[str] = []

    class FakeModel:
        def generate(self, *, input, batch_size_s):  # noqa: A002 - 对齐 funasr 签名
            calls.append(input)
            return [
                {
                    "sentence_info": [
                        {"text": "第二段", "start": 2000, "end": 3000},
                        {"text": "第一段", "start": 0, "end": 1800},
                    ]
                }
            ]

    checkpoints: list[bool] = []

    def cancel_check() -> None:
        checkpoints.append(True)

    backend = _backend()
    backend._model = FakeModel()  # noqa: SLF001 - 注入假模型，免下载
    backend.cancel_check = cancel_check
    segments = backend.transcribe(Path("audio.wav"), duration=3.0)

    assert calls == ["audio.wav"]
    # 输出按起始时间排序，不按模型返回顺序
    assert [s.text for s in segments] == ["第一段", "第二段"]
    # 调用边界各检查一次取消
    assert len(checkpoints) >= 2


def test_transcribe_wraps_model_errors_as_asr_error():
    class BoomModel:
        def generate(self, **_kwargs):
            raise RuntimeError("CUDA out of memory")

    backend = _backend()
    backend._model = BoomModel()  # noqa: SLF001
    with pytest.raises(AsrError) as excinfo:
        backend.transcribe(Path("audio.wav"))
    assert "CUDA out of memory" in str(excinfo.value)
