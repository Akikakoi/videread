"""retry_call 的重试边界用例（离线）。

密钥 / 参数类错误不重试已在 §11.2 覆盖；这里验证用户中断（KeyboardInterrupt /
SystemExit）必须立即穿透，不允许被当成可重试错误继续休眠重跑。
"""

from __future__ import annotations

import pytest

from videread.retry import retry_call


def test_retry_call_retries_retryable_errors() -> None:
    calls = {"n": 0}

    def _flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("暂时不可用")
        return "ok"

    assert retry_call(_flaky, attempts=3, base=0.0) == "ok"
    assert calls["n"] == 3


def test_retry_call_does_not_swallow_keyboard_interrupt() -> None:
    """Ctrl+C 必须立刻抛出，不能被退避重试吞掉。"""
    calls = {"n": 0}

    def _interrupted() -> None:
        calls["n"] += 1
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        retry_call(_interrupted, attempts=3, base=0.0)
    assert calls["n"] == 1


def test_retry_call_does_not_swallow_system_exit() -> None:
    calls = {"n": 0}

    def _exits() -> None:
        calls["n"] += 1
        raise SystemExit(2)

    with pytest.raises(SystemExit):
        retry_call(_exits, attempts=3, base=0.0)
    assert calls["n"] == 1
