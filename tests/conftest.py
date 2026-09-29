"""离线用例的公共夹具。

pipeline.run() 启动即预检 LLM 密钥（避免下载 / ASR 成本白花后才发现缺配置），
因此所有用例统一注入哑密钥，保证测试不依赖本机 .env 的真实内容。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _dummy_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "test-llm-key")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-dashscope-key")
