"""多语言输出（REPORT_LANG / --lang）：prompt 语言规则、词数口径、chrome 翻译与缓存失效。"""

from __future__ import annotations

from pathlib import Path

from videread import pipeline as pipeline_mod
from videread import render
from videread.config import lang_display_name
from videread.download import VideoMeta
from videread.errors import UsageError
from videread.report import load as load_outline
from videread.report import write as write_outline
from videread.report import prompts
from videread.report.outline import Outline, plan_outline, validate
from videread.report.writer import check_brief_length, count_text_words
from videread.trace import TraceWriter
from videread.transcript import TranscriptUnit


# ---------------------------------------------------------------- prompt 层


def test_language_rule_empty_for_zh():
    assert prompts.language_rule("") == ""
    assert prompts.language_rule("zh") == ""
    assert prompts.language_rule("zh-CN") == ""


def test_language_rule_names_target_language():
    rule = prompts.language_rule("en")
    assert "English" in rule and "输出语言" in rule
    assert "日本語" in prompts.language_rule("ja")


def test_lang_display_name_falls_back_to_code():
    assert lang_display_name("en") == "English"
    assert lang_display_name("xx") == "xx"  # 未收录代码原样透传给 LLM


def test_writer_system_zh_keeps_char_counts():
    system = prompts.writer_system("standard", "zh")
    assert "300-700 字" in system
    assert "输出语言" not in system  # zh 不加额外约束，历史行为不变


def test_writer_system_en_switches_to_words():
    system = prompts.writer_system("standard", "en")
    assert "150-350 词" in system
    assert "English" in system
    brief = prompts.writer_system("brief", "en")
    assert "800–1100 words" in brief  # Brief 词数硬约束生效
    assert "1600–2200 字" not in brief


def test_qa_system_follows_question_language():
    system = prompts.qa_system("ja")
    assert "提问所用的语言" in system and "日本語" in system


# ---------------------------------------------------------------- 计数口径


def test_count_text_words_ignores_tags():
    assert count_text_words('<p data-x="1">hello<br>world</p>') == 2


def test_check_brief_length_uses_chars_for_zh():
    # 100 个汉字：低于中文 Brief 目标区间（1600-2200），应报低于下限
    html = "<p>" + "字" * 100 + "</p>"
    report = check_brief_length([html], lang="zh")
    assert report.chars == 100
    assert any("低于目标区间下限 1600" in issue for issue in report.issues)
    assert not report.over_hard


def test_check_brief_length_uses_words_for_en():
    # 50 个英文单词：低于英文 Brief 目标区间（800-1100），报"词"
    html = "<p>" + "word " * 50 + "</p>"
    report = check_brief_length([html], lang="en")
    assert report.chars == 50
    assert any("词" in issue for issue in report.issues)


# ---------------------------------------------------------------- 大纲层


def _units(n: int = 4) -> list[TranscriptUnit]:
    return [
        TranscriptUnit(id=f"u{i:04d}", start=float(i * 10), end=float(i * 10 + 8), text=f"第{i}句")
        for i in range(1, n + 1)
    ]


def _outline_data() -> dict:
    return {
        "title": "Test Title",
        "subtitle": "Sub",
        "lead": "Lead.",
        "profile": "argument",
        "sections": [
            {"id": "s1", "heading": "One", "source_ids": ["u0001", "u0002"]},
            {"id": "s2", "heading": "Two", "source_ids": ["u0003"]},
        ],
    }


def test_outline_round_trips_lang():
    outline = validate(_outline_data(), _units(), lang="en")
    assert outline.lang == "en"
    data = outline.to_dict()
    assert data["lang"] == "en"
    assert Outline.from_dict(data).lang == "en"
    # 历史产物没有 lang 字段：回落 zh
    legacy = dict(data)
    legacy.pop("lang")
    assert Outline.from_dict(legacy).lang == "zh"


def test_plan_outline_injects_language_rule(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, str] = {}

    class _FakeClient:
        def __init__(self, *_args: object) -> None:
            pass

        def complete_json(self, *, system: str, user: str, model: str):
            captured["system"] = system
            return _outline_data(), "{}"

    meta = VideoMeta(
        bvid="BV1xx411c7mD", title="T", uploader="U", duration=60.0,
        url="https://www.bilibili.com/video/BV1xx411c7mD", cover="", description="",
    )
    outline = plan_outline(
        meta, _units(), lang="en", client=_FakeClient(),
        settings=__import__("videread.config", fromlist=["get_settings"]).get_settings(),
    )
    assert "English" in captured["system"]
    assert outline.lang == "en"


# ---------------------------------------------------------------- 渲染层


def _meta() -> VideoMeta:
    return VideoMeta(
        bvid="BV1xx411c7mD", title="T", uploader="U", duration=60.0,
        url="https://www.bilibili.com/video/BV1xx411c7mD", cover="", description="desc",
    )


def _settings() -> object:
    return __import__("videread.config", fromlist=["get_settings"]).get_settings()


def test_render_context_zh_keeps_chinese_chrome():
    ctx = render.context(_meta(), validate(_outline_data(), _units()), [], _units(), _settings(), mode="standard", subtitle_first=False)  # type: ignore[arg-type]
    assert ctx["DOC_TITLE"] == "视频阅读报告"
    assert "UP主" in ctx["ATTRIBUTION"]


def test_render_context_en_translates_chrome():
    outline = validate(_outline_data(), _units(), lang="en")
    ctx = render.context(_meta(), outline, [], _units(), _settings(), mode="standard", subtitle_first=False, lang="en")  # type: ignore[arg-type]
    assert ctx["DOC_TITLE"] == "Video Reading Report"
    assert ctx["DESC_TITLE"] == "Video description"
    assert ctx["SOURCES_TITLE"] == "Sources & processing notes"
    assert "Uploader" in ctx["ATTRIBUTION"]
    assert "Source video" in ctx["SOURCES"]
    # 未收录语言回落英文 chrome
    ctx_xx = render.context(_meta(), outline, [], _units(), _settings(), mode="standard", subtitle_first=False, lang="xx")  # type: ignore[arg-type]
    assert ctx_xx["DOC_TITLE"] == "Video Reading Report"


# ---------------------------------------------------------------- 缓存失效


def _prepare_run(tmp_path: Path, outline: Outline) -> object:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_outline(outline, run_dir / "outline.json")
    trace = TraceWriter(run_dir / "run.trace.jsonl")
    meta = _meta()
    state = pipeline_mod._RunState(
        url=meta.url, mode="standard", out_root=tmp_path, use_cache=True,
        keep_audio=False, force_asr=False, force_report=False, fresh_dir=False,
        lang="en", settings=_settings(), progress=lambda _msg: None,
    )
    state.run_dir = run_dir
    state.trace = trace
    state.meta = meta
    state.units = _units()
    return state


def test_stage_outline_recomputes_when_lang_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state = _prepare_run(tmp_path, validate(_outline_data(), _units(), lang="zh"))

    def _fake_plan(meta, units, *, mode, lang, client, settings, progress):
        return validate(_outline_data(), units, lang=lang)

    monkeypatch.setattr(pipeline_mod, "plan_outline", _fake_plan)
    pipeline_mod._stage_outline(state)
    assert state.force_report is True  # 正文与渲染必须重出
    assert state.outline is not None and state.outline.lang == "en"
    reloaded = load_outline(state.run_dir / "outline.json")
    assert reloaded.lang == "en"


def test_stage_outline_keeps_cache_when_lang_matches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state = _prepare_run(tmp_path, validate(_outline_data(), _units(), lang="en"))

    def _boom(*_args, **_kwargs):
        raise AssertionError("语言一致时不应重新规划")

    monkeypatch.setattr(pipeline_mod, "plan_outline", _boom)
    pipeline_mod._stage_outline(state)
    assert state.force_report is False
    assert state.outline is not None and state.outline.lang == "en"
