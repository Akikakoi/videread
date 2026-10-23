"""§13：报告内问答用例（离线，LLM 用桩替身）。

覆盖检索选段、答案清洗、引用反解与历史持久化四件事；不联网、不调真实模型。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from videread import transcript
from videread.errors import LlmError, UsageError
from videread.report import qa
from videread.transcript import TranscriptUnit

RUN_ID = "BV1xx411c7mD-aaaaaaaa"


# --------------------------------------------------------------------- 工具


def unit(index: int, start: float, text: str, source: str = "asr") -> TranscriptUnit:
    return TranscriptUnit(
        id=f"u{index:04d}", start=start, end=start + 10.0, text=text, source=source
    )


def write_run(
    tmp_path: Path,
    units: list[TranscriptUnit],
    *,
    outline: bool = True,
    sections: list[dict] | None = None,
) -> Path:
    """构造最小 run：transcript.jsonl（+ 可选 outline.json）。"""
    run_dir = tmp_path / RUN_ID
    run_dir.mkdir(parents=True, exist_ok=True)
    transcript.write_jsonl(units, run_dir / "transcript.jsonl")
    if outline:
        payload = {
            "title": "示例视频",
            "subtitle": "",
            "lead": "导语",
            "profile": "mechanism",
            "sections": sections
            or [
                {
                    "id": "s1",
                    "heading": "第一章",
                    "time_range": "00:00-00:30",
                    "intent": "讲清机制",
                    "source_ids": ["u0001", "u0002"],
                }
            ],
        }
        (run_dir / "outline.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    return run_dir


class StubSettings:
    """answer_question 只用到这两个接口，用桩避免真读 .env。"""

    llm_model_write = "stub-write-model"

    def require_llm(self) -> str:
        return "sk-stub"


class StubClient:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict] = []
        self.usage = SimpleNamespace(total=42)

    def complete(self, *, system: str, user: str, model: str) -> str:
        self.calls.append({"system": system, "user": user, "model": model})
        return self.reply


# --------------------------------------------------------------------- 检索


def test_retrieve_returns_all_units_when_under_budget():
    units = [unit(1, 0.0, "第一段"), unit(2, 10.0, "第二段"), unit(3, 20.0, "第三段")]

    picked = qa.retrieve(units, "随便问问", budget_chars=1000)

    assert [item.id for item in picked] == ["u0001", "u0002", "u0003"]


def test_retrieve_filters_by_relevance_over_budget():
    filler = "这是与问题无关的填充内容，仅用于把转写总长度撑过预算上限。" * 3
    units = [unit(index, float(index), filler) for index in range(1, 9)]
    units.insert(4, unit(20, 5.0, "量子纠缠的机制与判定条件"))

    picked = qa.retrieve(units, "量子纠缠是怎么成立的", budget_chars=300, top_k=5)

    assert [item.id for item in picked] == ["u0020"]


def test_retrieve_samples_uniformly_when_no_term_hits():
    filler = "这是一段与提问毫无字面交集的转写内容，用来验证兜底采样。" * 3
    units = [unit(index, float(index), filler) for index in range(1, 9)]

    picked = qa.retrieve(units, "ZZZ", budget_chars=300, top_k=4)

    assert picked, "无字面命中时也必须给模型一点上下文"
    assert len(picked) <= 4
    assert [item.start for item in picked] == sorted(item.start for item in picked)


# --------------------------------------------------------------------- 回答


def test_answer_question_sanitizes_and_maps_citations(tmp_path: Path):
    units = [
        unit(1, 0.0, "第一个前提是数据必须连续可测。" * 3),
        unit(2, 12.0, "第二个前提是样本量足够大。" * 3),
        unit(3, 24.0, "与结论无关的一段。" * 3),
    ]
    run_dir = write_run(tmp_path, units)
    reply = '<div data-source-units="u0001,u0002,u0099"><p>结论如下。</p></div>'
    client = StubClient(reply)

    answer = qa.answer_question(
        run_dir, "作者给出的前提是什么", settings=StubSettings(), client=client
    )

    assert "u0099" not in answer.answer_html, "越界 id 必须被清洗掉"
    assert 'data-source-units="u0001,u0002"' in answer.answer_html
    assert [cite.unit_id for cite in answer.citations] == ["u0001", "u0002"]
    assert answer.citations[0].seconds == 0.0
    assert answer.citations[0].time_text == "00:00"
    assert answer.citations[0].section_id == "s1"
    assert answer.citations[0].section_index == 1
    assert answer.citations[1].time_text == "00:12"
    assert answer.usage_total == 42


def test_answer_question_passes_question_and_model_to_llm(tmp_path: Path):
    run_dir = write_run(tmp_path, [unit(1, 0.0, "转写正文")])
    client = StubClient("<p>回答</p>")

    qa.answer_question(run_dir, "这个问题是什么", settings=StubSettings(), client=client)

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["model"] == "stub-write-model"
    assert "这个问题是什么" in call["user"]
    assert "u0001" in call["user"], "送模型的上下文必须带单元 id 便于引用"


def test_answer_question_empty_question_raises(tmp_path: Path):
    run_dir = write_run(tmp_path, [unit(1, 0.0, "转写正文")])

    with pytest.raises(UsageError, match="请输入问题"):
        qa.answer_question(run_dir, "   ", settings=StubSettings(), client=StubClient("x"))


def test_answer_question_too_long_raises(tmp_path: Path):
    run_dir = write_run(tmp_path, [unit(1, 0.0, "转写正文")])

    with pytest.raises(UsageError, match="问题过长"):
        qa.answer_question(
            run_dir,
            "字" * (qa.MAX_QUESTION_CHARS + 1),
            settings=StubSettings(),
            client=StubClient("x"),
        )


def test_answer_question_without_transcript_raises(tmp_path: Path):
    run_dir = tmp_path / RUN_ID
    run_dir.mkdir(parents=True)

    with pytest.raises(UsageError, match="没有可用的转写稿"):
        qa.answer_question(run_dir, "问题", settings=StubSettings(), client=StubClient("x"))


def test_answer_question_rejects_blank_model_output(tmp_path: Path):
    run_dir = write_run(tmp_path, [unit(1, 0.0, "转写正文")])

    with pytest.raises(LlmError, match="未返回有效回答"):
        qa.answer_question(
            run_dir, "问题内容", settings=StubSettings(), client=StubClient("   ")
        )


# --------------------------------------------------------------------- 历史


def test_history_roundtrip_and_skips_broken_lines(tmp_path: Path):
    run_dir = write_run(tmp_path, [unit(1, 0.0, "转写正文")])
    answer = qa.answer_question(
        run_dir,
        "第一个问题",
        settings=StubSettings(),
        client=StubClient("<p>回答</p>"),
    )

    path = qa.append_history(run_dir, "第一个问题", answer)
    assert path == run_dir / "qa.jsonl"

    with path.open("a", encoding="utf-8") as handle:
        handle.write("{ 这不是 JSON\n")

    history = qa.load_history(run_dir)

    assert len(history) == 1
    assert history[0]["question"] == "第一个问题"
    assert history[0]["answer_html"] == "<p>回答</p>"
    assert history[0]["usage_total"] == 42


def test_load_history_without_file_returns_empty(tmp_path: Path):
    run_dir = tmp_path / RUN_ID
    run_dir.mkdir(parents=True)

    assert qa.load_history(run_dir) == []


def test_delete_history_removes_only_the_target(tmp_path: Path):
    run_dir = write_run(tmp_path, [unit(1, 0.0, "转写正文")])
    for question in ("第一问", "第二问", "第三问"):
        qa.append_history(
            run_dir,
            question,
            qa.Answer(answer_html=f"<p>{question}</p>", citations=[], usage_total=0),
        )

    removed = qa.delete_history(run_dir, 1)

    assert removed is not None
    assert removed["question"] == "第二问"
    assert [item["question"] for item in qa.load_history(run_dir)] == ["第一问", "第三问"]
    assert not (run_dir / "qa.jsonl.tmp").exists()


def test_delete_history_counts_only_valid_records(tmp_path: Path):
    run_dir = write_run(tmp_path, [unit(1, 0.0, "转写正文")])
    path = run_dir / "qa.jsonl"
    path.write_text(
        '{"ts": "t", "question": "第一问", "answer_html": "<p>a</p>"}\n'
        "{ 这不是 JSON\n"
        '{"ts": "t", "question": "第二问", "answer_html": "<p>b</p>"}\n',
        encoding="utf-8",
    )

    removed = qa.delete_history(run_dir, 1)

    assert removed is not None
    assert removed["question"] == "第二问"
    assert "这不是 JSON" in path.read_text(encoding="utf-8"), "损坏行原样保留，不参与下标"
    assert [item["question"] for item in qa.load_history(run_dir)] == ["第一问"]


def test_delete_history_out_of_range_returns_none(tmp_path: Path):
    run_dir = write_run(tmp_path, [unit(1, 0.0, "转写正文")])

    assert qa.delete_history(run_dir, 0) is None, "没有 qa.jsonl 时视为不存在"

    qa.append_history(
        run_dir,
        "唯一一问",
        qa.Answer(answer_html="<p>a</p>", citations=[], usage_total=0),
    )

    assert qa.delete_history(run_dir, 1) is None
    assert qa.delete_history(run_dir, -1) is None
    assert len(qa.load_history(run_dir)) == 1
