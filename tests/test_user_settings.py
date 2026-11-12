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
    # Key 按端点落盘；旧版顶层 llm_api_key 不再写入
    raw = json.loads(settings_file.read_text(encoding="utf-8"))
    assert raw["llm_api_keys"] == {"https://api.example.com/v1": "sk-abc123456789"}
    assert "llm_api_key" not in raw


def test_clear_removes_file(settings_file: Path):
    usersettings.save_user_llm_settings(llm_base_url="https://api.example.com/v1")
    assert usersettings.clear_user_llm_settings() is True
    assert usersettings.load_user_llm_settings() == {}
    assert usersettings.clear_user_llm_settings() is False  # 再删一次


def test_clear_keep_asr_preserves_engine_choice(settings_file: Path):
    """「写作模型恢复默认」不应顺手重置转写引擎的页面选择。"""
    usersettings.save_user_llm_settings(
        llm_base_url="https://api.example.com/v1", asr_backend="funasr"
    )
    assert usersettings.clear_user_llm_settings(keep_asr=True) is True
    data = usersettings.load_user_llm_settings()
    assert data == {"asr_backend": "funasr"}


def test_clear_keep_asr_without_asr_removes_file(settings_file: Path):
    usersettings.save_user_llm_settings(llm_base_url="https://api.example.com/v1")
    assert usersettings.clear_user_llm_settings(keep_asr=True) is True
    assert not settings_file.exists()


def test_save_rejects_bad_base_url(settings_file: Path):
    with pytest.raises(Exception, match="http"):
        usersettings.save_user_llm_settings(llm_base_url="api.example.com")
    assert not settings_file.exists()


def test_save_keep_keeps_existing_key(settings_file: Path):
    base = "https://api.example.com/v1"
    usersettings.save_user_llm_settings(llm_api_key="sk-keep-me-1234", llm_base_url=base)
    usersettings.save_user_llm_settings(
        llm_model_plan="new-model", llm_api_key="keep", llm_base_url=base
    )
    data = usersettings.load_user_llm_settings()
    assert data["llm_api_key"] == "sk-keep-me-1234"
    assert data["llm_model_plan"] == "new-model"


# --------------------------------------------------------- 多端点 Key 保留

def test_keys_kept_per_endpoint(settings_file: Path):
    """切换服务商不会覆盖别家 Key，切回来留空即可沿用。"""
    base_a = "https://api.deepseek.com/v1"
    base_b = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    usersettings.save_user_llm_settings(
        llm_api_key="sk-aaa-11112222", llm_base_url=base_a
    )
    usersettings.save_user_llm_settings(
        llm_api_key="sk-bbb-33334444", llm_base_url=base_b
    )
    assert get_settings().llm_api_key == "sk-bbb-33334444"
    assert usersettings.load_api_keys() == {
        base_a: "sk-aaa-11112222",
        base_b: "sk-bbb-33334444",
    }

    # 切回 A：留空（keep）沿用 A 的 Key，B 的 Key 也不会被覆盖
    usersettings.save_user_llm_settings(llm_api_key="keep", llm_base_url=base_a)
    assert get_settings().llm_api_key == "sk-aaa-11112222"
    assert usersettings.load_api_keys()[base_b] == "sk-bbb-33334444"


def test_blank_key_on_new_endpoint_keeps_others(settings_file: Path, monkeypatch):
    base_a = "https://api.deepseek.com/v1"
    base_b = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    monkeypatch.setenv("LLM_API_KEY", "sk-env-key-0000")
    usersettings.save_user_llm_settings(
        llm_api_key="sk-aaa-11112222", llm_base_url=base_a
    )
    # 新端点留空保存：不把 A 的 Key 当作 B 的 Key
    usersettings.save_user_llm_settings(llm_base_url=base_b, llm_model_plan="qwen-plus")
    assert usersettings.key_for_base_url(base_b) == ""
    assert get_settings().llm_api_key == "sk-env-key-0000"
    assert usersettings.load_api_keys() == {base_a: "sk-aaa-11112222"}


def test_base_url_trailing_slash_shares_key(settings_file: Path):
    usersettings.save_user_llm_settings(
        llm_api_key="sk-slash-12345678", llm_base_url="https://api.example.com/v1"
    )
    usersettings.save_user_llm_settings(
        llm_api_key="", llm_base_url="https://api.example.com/v1/"
    )
    assert usersettings.load_user_llm_settings()["llm_api_key"] == "sk-slash-12345678"


def test_legacy_single_key_migrates_to_map(settings_file: Path):
    base = "https://legacy.example.com/v1"
    settings_file.write_text(
        json.dumps(
            {
                "llm_api_key": "sk-legacy-9999",
                "llm_base_url": base,
                "llm_model_plan": "m",
            }
        ),
        encoding="utf-8",
    )
    assert usersettings.load_user_llm_settings()["llm_api_key"] == "sk-legacy-9999"
    assert usersettings.key_for_base_url(base) == "sk-legacy-9999"

    # 任意一次保存即把旧格式迁移成映射，旧字段不再写回
    usersettings.save_user_llm_settings(llm_model_write="w")
    raw = json.loads(settings_file.read_text(encoding="utf-8"))
    assert "llm_api_key" not in raw
    assert raw["llm_api_keys"] == {base: "sk-legacy-9999"}
    assert usersettings.load_user_llm_settings()["llm_api_key"] == "sk-legacy-9999"


# ------------------------------------------------------------- 转写引擎

def test_save_asr_backend_roundtrip(settings_file: Path):
    usersettings.save_user_llm_settings(asr_backend="dashscope-realtime")
    assert usersettings.load_user_llm_settings()["asr_backend"] == "dashscope-realtime"


def test_save_asr_backend_none_leaves_untouched(settings_file: Path):
    """asr_backend 不传（None）= 本次调用与转写引擎无关，不动已存值。"""
    usersettings.save_user_llm_settings(asr_backend="funasr")
    usersettings.save_user_llm_settings(llm_model_plan="plan-x")
    assert usersettings.load_user_llm_settings()["asr_backend"] == "funasr"
    assert usersettings.load_user_llm_settings()["llm_model_plan"] == "plan-x"


def test_save_asr_backend_empty_follows_env(settings_file: Path):
    """空串 = 「跟随 .env」：撤掉已保存的覆盖值。"""
    usersettings.save_user_llm_settings(asr_backend="funasr")
    usersettings.save_user_llm_settings(asr_backend="")
    assert "asr_backend" not in usersettings.load_user_llm_settings()
    # 本来就没存过时空串也是无害的
    usersettings.save_user_llm_settings(asr_backend="")
    assert "asr_backend" not in usersettings.load_user_llm_settings()


def test_save_asr_backend_rejects_unknown(settings_file: Path):
    with pytest.raises(Exception, match="未知的转写引擎"):
        usersettings.save_user_llm_settings(asr_backend="epic")
    assert not settings_file.exists() or "asr_backend" not in json.loads(
        settings_file.read_text(encoding="utf-8")
    )


def test_get_settings_user_asr_overrides_env(
    settings_file: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("ASR_BACKEND", "funasr")
    usersettings.save_user_llm_settings(asr_backend="dashscope-realtime")
    assert get_settings().asr_backend == "dashscope-realtime"


def test_get_settings_cli_asr_beats_user(settings_file: Path):
    usersettings.save_user_llm_settings(asr_backend="funasr")
    assert get_settings(asr_backend="local").asr_backend == "local"


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
                "asr_backend": "funasr",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "custom"
        assert body["has_key"] is True
        assert body["api_key_masked"] == "sk-****7890"
        assert body["saved_keys"] == {
            "https://api.example.com/v1": "sk-****7890"
        }
        assert body["asr_backend"] == "funasr"
        assert body["asr_backend_custom"] == "funasr"
        # 完整 key 永不出现在响应里
        assert "sk-web-1234567890" not in r.text

        r = client.get("/api/settings")
        assert r.status_code == 200
        assert r.json()["base_url"] == "https://api.example.com/v1"
        assert r.json()["asr_backend_custom"] == "funasr"

        # 选「跟随 .env」= 撤掉覆盖
        r = client.post("/api/settings", json={"asr_backend": ""})
        assert r.status_code == 200
        assert r.json()["asr_backend_custom"] == ""

        # 非法后端 400
        r = client.post("/api/settings", json={"asr_backend": "epic"})
        assert r.status_code == 400
        assert "未知的转写引擎" in r.json()["detail"]

        # 「恢复默认」只清 LLM：先存引擎，再 DELETE，引擎保留
        client.post("/api/settings/asr", json={"backend": "funasr"})
        client.delete("/api/settings")
        assert client.get("/api/settings").json()["asr_backend_custom"] == "funasr"
        assert client.get("/api/settings").json()["source"] == "env"


def test_api_settings_saved_keys_are_masked_per_endpoint(
    settings_file: Path, tmp_path: Path
):
    """多个服务商的 Key 分别记住；响应只给脱敏值，完整 Key 不出后端。"""
    base_a = "https://api.deepseek.com/v1"
    base_b = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    with TestClient(create_app(tmp_path)) as client:
        client.post(
            "/api/settings", json={"api_key": "sk-aaa-11112222", "base_url": base_a}
        )
        client.post(
            "/api/settings", json={"api_key": "sk-bbb-33334444", "base_url": base_b}
        )
        r = client.get("/api/settings")
        body = r.json()
        assert body["saved_keys"] == {
            base_a: "sk-****2222",
            base_b: "sk-****4444",
        }
        assert "sk-aaa-11112222" not in r.text
        assert "sk-bbb-33334444" not in r.text
        assert body["source"] == "custom"

        # 切回 A、Key 留空保存：沿用 A 已保存的 Key
        r = client.post(
            "/api/settings", json={"base_url": base_a, "model_plan": "deepseek-chat"}
        )
        assert r.status_code == 200
        assert r.json()["saved_keys"][base_a] == "sk-****2222"
        assert r.json()["api_key_masked"] == "sk-****2222"
        assert r.json()["source"] == "custom"


def test_api_settings_llm_save_leaves_asr_untouched(
    settings_file: Path, tmp_path: Path
):
    """两个页签互不干扰：保存写作模型不带 asr_backend 字段时，引擎配置不动。"""
    with TestClient(create_app(tmp_path)) as client:
        client.post("/api/settings/asr", json={"backend": "dashscope-realtime"})
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
        assert body["asr_backend_custom"] == "dashscope-realtime"
        assert body["asr_backend"] == "dashscope-realtime"


def test_api_settings_asr_route(settings_file: Path, tmp_path: Path):
    """转写引擎独立端点：只碰 asr_backend，LLM 配置不受影响。"""
    with TestClient(create_app(tmp_path)) as client:
        client.post(
            "/api/settings",
            json={"base_url": "https://api.example.com/v1", "model_plan": "plan-x"},
        )
        r = client.post("/api/settings/asr", json={"backend": "funasr"})
        assert r.status_code == 200
        body = r.json()
        assert body["asr_backend_custom"] == "funasr"
        assert body["base_url"] == "https://api.example.com/v1"

        # 空值 / 非法值都 400
        r = client.post("/api/settings/asr", json={"backend": "  "})
        assert r.status_code == 400
        assert "请选择" in r.json()["detail"]
        r = client.post("/api/settings/asr", json={"backend": "epic"})
        assert r.status_code == 400
        assert "未知的转写引擎" in r.json()["detail"]

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


def test_api_list_models_uses_key_saved_for_selected_endpoint(
    settings_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """表单没填 Key 时按所选端点取它保存过的 Key，而不是当前生效端点的。"""
    import openai

    captured = {}

    class Recorder:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)
            self.models = _FakeModels()

    monkeypatch.setattr(openai, "OpenAI", Recorder)
    base_a = "https://api.deepseek.com/v1"
    base_b = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    with TestClient(create_app(tmp_path)) as client:
        client.post(
            "/api/settings", json={"api_key": "sk-aaa-11112222", "base_url": base_a}
        )
        client.post(
            "/api/settings", json={"api_key": "sk-bbb-33334444", "base_url": base_b}
        )
        # 生效端点是 B，但这次要拉 A 的模型列表
        r = client.post("/api/settings/models", json={"base_url": base_a})
        assert r.status_code == 200
        assert captured["api_key"] == "sk-aaa-11112222"
        assert captured["base_url"] == base_a


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
