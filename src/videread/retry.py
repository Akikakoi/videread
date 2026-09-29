"""指数退避重试（对应开发文档 §11.2）。

密钥 / 余额类错误与参数校验失败**不重试**。
"""

from __future__ import annotations

import random
import time
from typing import Callable, TypeVar

T = TypeVar("T")

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

# 重试无意义的标记：鉴权、欠费、模型不存在、参数非法
_NON_RETRYABLE_MARKERS = (
    "invalidapikey",
    "invalid_api_key",
    "incorrect api key",
    "authenticationerror",
    "permissiondenied",
    "accessdenied",
    "unauthorized",
    "arrearage",
    "insufficient",
    "quota",
    "modelnotexist",
    "invalidparameter",
    "invalid_request_error",
)


def is_retryable(exc: BaseException) -> bool:
    """判断异常是否值得重试。"""
    status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    if isinstance(status, int) and status not in RETRYABLE_STATUS:
        return False

    message = f"{type(exc).__name__}: {exc}".lower()
    if any(marker in message for marker in _NON_RETRYABLE_MARKERS):
        return False
    return True


def retry_call(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base: float = 1.0,
    jitter: float = 0.2,
    on_retry: Callable[[int, float, BaseException], None] | None = None,
) -> T:
    """指数退避：base → 2·base → 4·base，带 ±jitter 抖动。"""
    if attempts < 1:
        raise ValueError("attempts 必须 >= 1")

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 由 is_retryable 决定是否继续
            # 只捕获 Exception：KeyboardInterrupt / SystemExit 属于用户意图，
            # 必须立即终止，否则 Ctrl+C 会被当成可重试错误继续休眠重跑。
            last_error = exc
            if attempt >= attempts or not is_retryable(exc):
                raise
            delay = base * (2 ** (attempt - 1))
            delay *= 1 + random.uniform(-jitter, jitter)
            if on_retry is not None:
                on_retry(attempt, delay, exc)
            time.sleep(max(0.0, delay))
    assert last_error is not None
    raise last_error