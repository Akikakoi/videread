"""failures：退出码 → 类别 / 文案 的纯函数映射（§6.1）。"""

from __future__ import annotations

from videread import errors, failures


def _declared_exit_codes() -> set[int]:
    return {
        value
        for name, value in vars(errors).items()
        if name.startswith("EXIT_") and isinstance(value, int)
    }


def test_every_declared_exit_code_is_registered():
    """errors.py 里新增退出码却忘了登记文案，必须在这里被挡住。"""
    declared = _declared_exit_codes()
    assert len(declared) == 7  # 防止取到空集导致假绿
    assert declared - set(failures.GROUPS) == set()
    assert declared - {errors.EXIT_OK} - set(failures.HINTS) == set()


def test_group_mapping_matches_documented_semantics():
    assert failures.group(errors.EXIT_OK) == "ok"
    assert failures.group(errors.EXIT_USAGE) == "input"
    assert failures.group(errors.EXIT_DOWNLOAD) == "external"
    assert failures.group(errors.EXIT_LLM) == "external"
    assert failures.group(errors.EXIT_AUDIO_OR_ASR) == "processing"
    assert failures.group(errors.EXIT_RENDER) == "processing"
    assert failures.group(errors.EXIT_CANCELLED) == "runtime"


def test_unknown_exit_code_falls_back():
    assert failures.group(99) == "unknown"
    assert failures.hint(99) == "未知错误"


def test_exception_classes_are_all_covered():
    """异常类自带的 exit_code 必须能在 failures 里查到，不允许出现 unknown。"""
    classes = [
        obj
        for obj in vars(errors).values()
        if isinstance(obj, type) and issubclass(obj, errors.VidereadError)
    ]
    assert classes
    for cls in classes:
        assert failures.group(cls.exit_code) != "unknown", cls.__name__
        assert failures.hint(cls.exit_code) != "未知错误", cls.__name__