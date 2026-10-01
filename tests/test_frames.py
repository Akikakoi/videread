"""截图模块用例（离线）：选点、注入、片段边界。抽帧本身依赖 ffmpeg，不在离线范围。"""

from __future__ import annotations

from pathlib import Path

from videread.config import FRAME_MAX_TOTAL
from videread.frames import clip_bounds, inject_figures, plan_points
from videread.report import Outline, OutlineSection
from videread.transcript import TranscriptUnit


def _units() -> list[TranscriptUnit]:
    return [
        TranscriptUnit(id="u0001", start=0.0, end=10.0, text="一", source="asr"),
        TranscriptUnit(id="u0002", start=10.0, end=30.0, text="二", source="asr"),
        TranscriptUnit(id="u0003", start=30.0, end=50.0, text="三", source="asr"),
        TranscriptUnit(id="u0004", start=50.0, end=60.0, text="四", source="asr"),
    ]


def _outline(sections: list[OutlineSection]) -> Outline:
    return Outline(title="t", subtitle="", lead="l", profile="mechanism", sections=sections)


def test_plan_points_picks_section_midpoints() -> None:
    outline = _outline(
        [
            OutlineSection(id="s1", heading="a", time_range="x", intent="", source_ids=["u0001", "u0002"]),
            OutlineSection(id="s2", heading="b", time_range="x", intent="", source_ids=["u0003"]),
        ]
    )
    points = plan_points(outline, _units())
    # s1 覆盖 0-30s，中点 15；s2 覆盖 30-50s，中点 40
    assert [(sid, round(t, 1)) for sid, t, _ in points] == [("s1", 15.0), ("s2", 40.0)]
    assert [label for _, _, label in points]  # 展示时间非空


def test_plan_points_caps_total() -> None:
    sections = [
        OutlineSection(id=f"s{i}", heading="h", time_range="x", intent="", source_ids=["u0001"])
        for i in range(1, FRAME_MAX_TOTAL + 5)
    ]
    assert len(plan_points(_outline(sections), _units())) == FRAME_MAX_TOTAL


def test_clip_bounds_offsets_lead_in() -> None:
    # 常规时间点：向前多取 1s，片段内抽帧位置恰好回到绝对时间
    assert clip_bounds(83.0) == (82.0, 1.0)
    # 开头处：起点钳到 0，抽帧位置等于绝对时间
    assert clip_bounds(0.5) == (0.0, 0.5)


def test_inject_figures_inserts_after_h2(tmp_path: Path) -> None:
    outline = _outline(
        [
            OutlineSection(id="s1", heading="预热", time_range="00:10-00:20", intent="", source_ids=["u0001"]),
            OutlineSection(id="s2", heading="验证", time_range="00:20-00:30", intent="", source_ids=["u0002"]),
        ]
    )
    parts = ['<h2><span class="num">1</span>预热</h2>\n<p>正文一</p>', "<h2>验证</h2>\n<p>正文二</p>"]

    frame = tmp_path / "s1.jpg"
    frame.write_bytes(b"\xff\xd8fakejpeg")
    # 传入精确截图点：图注展示该时刻（s1 覆盖 0-30s，中点 15s）
    injected = inject_figures(parts, outline, {"s1": frame}, {"s1": 15.0})

    assert injected[0].count("<figure") == 1
    assert injected[0].index("</h2>") < injected[0].index("<figure")
    assert "data:image/jpeg;base64," in injected[0]
    assert "视频画面 · 00:15" in injected[0]
    # 没有截图的节原样返回
    assert injected[1] == parts[1]
    # 原始 parts 不被修改
    assert "<figure" not in parts[0]

    # 未传入截图点时回退到节的时间区间
    fallback = inject_figures(parts, outline, {"s1": frame})
    assert "视频画面 · 00:10-00:20" in fallback[0]


def test_has_frames(tmp_path):
    """frames/ 下有 JPEG 才算带截图；空目录 / 无目录 / 降级失败都算纯文本。"""
    from videread.frames import has_frames

    assert not has_frames(tmp_path)                      # 没跑截图

    frames = tmp_path / "frames"
    frames.mkdir()                                       # 截图阶段降级失败：目录在但没有产物
    assert not has_frames(tmp_path)

    (frames / "s1.jpg").write_bytes(b"\xff\xd8\xff")  # 有效产物
    assert has_frames(tmp_path)
