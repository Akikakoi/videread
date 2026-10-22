"""模型设置（用户自定义 API Key / 模型）用例：文件读写、优先级、Web API。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from videread import usersettings
from videread.config import get_settings
from videread.web.app import create_app


@pytest.fixture
def settings_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把配置文件指到临时目录，避免碰真实项目根的 .llm-settings.json。"""
    target = tmp_path / ".llm-settings.json"
    monkeypatch.setattr(usersettings, "SETTINGS_FILE", target)
    return target


# ----------------------------------------------------------------- 文件读写

def test_save_load_roundtrip(settings_file: Path):
    usersettings.save_user_llm_settings(
        llm_api_key="sk-abc123456789",
        llm_base_url="https://api.example.com/v1",
        llm_model_plan="plan-model",
        llm_model_write="write-model",
    )
    data = usersettings.load_user_llm_settings()
    assert data == {
        "llm_api_key": "sk-abc123456789",
        "llm_base_url": "https://api.example.com/v1",
        "llm_model_plan": "plan-model",
        "llm_model_write": "write-model",
    }
    # 落盘内容可读且为合法 JSON
    assert json.loads(settings_file.read_text(encoding="utf-8"))["llm_api_key"].startswith("sk-")


def test_clear_removes_file(settings_file: Path):
    usersettings.save_user_llm_settings(llm_base_url="https://api.example.com/v1")
    assert usersettings.clear_user_llm_settings() is True
    assert usersettings.load_user_llm_settings() == {}
    assert usersettings.clear_user_llm_settings() is False  # 再删一次


def test_save_rejects_bad_base_url(settings_file: Path):
    with pytest.raises(Exception, match="http"):
        usersettings.save_user_llm_settings(llm_base_url="api.example.com")
    assert not settings_file.exists()


def test_save_keep_keeps_existing_key(settings_file: Path):
    usersettings.save_user_llm_settings(llm_api_key="sk-keep-me-1234")
    usersettings.save_user_llm_settings(llm_model_plan="new-model", llm_api_key="keep")
    data = usersettings.load_user_llm_settings()
    assert data["llm_api_key"] == "sk-keep-me-1234"
    assert data["llm_model_plan"] == "new-model"


# ----------------------------------------------------------------- 优先级

def test_get_settings_user_overrides_env(
    settings_file: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("LLM_MODEL_PLAN", "env-plan")
    monkeypatch.setenv("LLM_BASE_URL", "https://env.example.com/v1")
    usersettings.save_user_llm_settings(
        llm_base_url="https://user.example.com/v1",
        llm_model_plan="user-plan",
    )
    s = get_settings()
    assert s.llm_base_url == "https://user.example.com/v1"
    assert s.llm_model_plan == "user-plan"
    # 未被用户配置覆盖的字段仍读环境
    assert s.llm_model_write  # env / 默认值


def test_get_settings_cli_beats_user(settings_file: Path):
    usersettings.save_user_llm_settings(llm_model_plan="user-plan")
    s = get_settings(llm_model_plan="cli-plan")
    assert s.llm_model_plan == "cli-plan"


# ----------------------------------------------------------------- 脱敏

def test_mask_key():
    assert usersettings.mask_key("") == ""
    assert usersettings.mask_key("short") == "****"
    assert usersettings.mask_key("sk-abcdefghij1234") == "sk-****1234"


# ----------------------------------------------------------------- Web API

def _client():
    return TestClient(create_app(Path("_out") / "unused"))


def test_api_settings_roundtrip(settings_file: Path, tmp_path: Path):
    with TestClient(create_app(tmp_path)) as client:
        r = client.post(
            "/api/settings",
            json={
                "api_key": "sk-web-1234567890",
                "base_url": "https://api.example.com/v1",
                "model_plan": "plan-x",
                "model_write": "write-y",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "custom"
        assert body["has_key"] is True
        assert body["api_key_masked"] == "sk-****7890"
        # 完整 key 永不出现在响应里
        assert "sk-web-1234567890" not in r.text

        r = client.get("/api/settings")
        assert r.status_code == 200
        assert r.json()["base_url"] == "https://api.example.com/v1"

        r = client.delete("/api/settings")
        assert r.status_code == 200
        assert r.json()["source"] == "env"


def test_api_settings_rejects_bad_base_url(settings_file: Path, tmp_path: Path):
    with TestClient(create_app(tmp_path)) as client:
        r = client.post("/api/settings", json={"base_url": "not-a-url"})
        assert r.status_code == 400
        assert "http" in r.json()["detail"]


def test_api_settings_test_requires_url(settings_file: Path, tmp_path: Path):
    with TestClient(create_app(tmp_path)) as client:
        r = client.post("/api/settings/test", json={"base_url": "nope", "model_plan": "m"})
        assert r.status_code == 400


# ------------------------------------------------------------- 模型列表

class _FakeModel:
    def __init__(self, mid: str) -> None:
        self.id = mid


class _FakeModels:
    def list(self):  # openai SDK 的分页对象；测试里直接当可迭代用
        return [_FakeModel("b-model"), _FakeModel("a-model"), _FakeModel("a-model")]


class _FakeOpenAI:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.models = _FakeModels()


def test_api_list_models(settings_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import openai

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    with TestClient(create_app(tmp_path)) as client:
        r = client.post("/api/settings/models", json={"base_url": "https://api.example.com/v1"})
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 2  # 去重
        assert body["models"] == ["a-model", "b-model"]  # 排序


def test_api_list_models_bad_url(settings_file: Path, tmp_path: Path):
    with TestClient(create_app(tmp_path)) as client:
        r = client.post("/api/settings/models", json={"base_url": "ftp://x"})
        assert r.status_code == 400
        assert "http" in r.json()["detail"]


# ------------------------------------------------------------- 连通测试

def test_api_test_settings_reasoning_fallback(
    settings_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """推理型模型正文为空但有思维链：应判连接成功并说明原因，而非（空）。"""
    from types import SimpleNamespace

    import openai

    def fake_openai(**kwargs):
        return SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **kw: SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                message=SimpleNamespace(
                                    content="", reasoning_content="思考链…"
                                )
                            )
                        ]
                    )
                )
            )
        )

    monkeypatch.setattr(openai, "OpenAI", fake_openai)
    with TestClient(create_app(tmp_path)) as client:
        r = client.post(
            "/api/settings/test",
            json={"base_url": "https://api.example.com/v1", "model_plan": "m"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["reply"] == "思考链…"
        assert "推理型" in body["note"]
