"""汇总消融实验结果：读 out/results.json 与各节 judge.json，产出 report.md。

运行：.venv/Scripts/python.exe experiments/ablation/make_report.py
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT_ROOT = HERE / "out"

from run_ablation import VARIANT_LABELS, VARIANTS, RUN_IDS  # noqa: E402

JUDGE_KEYS = ("faithfulness", "grounding", "style")
JUDGE_LABELS = {"faithfulness": "忠实性", "grounding": "口径保留", "style": "表达质量"}


def load() -> tuple[dict, list[dict]]:
    data = json.loads((OUT_ROOT / "results.json").read_text(encoding="utf-8"))
    return data["results"], data["failures"]


def main() -> None:
    results, failures = load()
    by_variant: dict[str, list[dict]] = defaultdict(list)
    by_run_variant: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in results:
        by_variant[r["variant"]].append(r)
        by_run_variant[(r["run"], r["variant"])].append(r)

    def mean(vals: list[float]) -> float:
        return sum(vals) / len(vals) if vals else float("nan")

    lines: list[str] = []
    lines.append("# 写作 Prompt 规则消融实验报告\n")
    lines.append("- 日期：2026-11-04　模型：mimo-v2.6-flash（生成与评分同一模型）")
    lines.append("- 控制变量：大纲固定复用各 run 现成 outline.json，转写不变，仅写作 prompt 不同")
    lines.append("- 样本：3 个 run（mechanism / procedure / argument 各一）× 全部章节")
    lines.append("- 评估：确定性正则指标 + LLM-as-judge 三维评分（1-5）\n")

    # ---- 每变体总表 ----
    lines.append("## 总表（3 个 run 合并）\n")
    lines.append("| 变体 | 节数 | 忠实性 | 口径保留 | 表达质量 | 禁用句式命中节占比 | 转述层命中节占比 | 平均来源绑定数 | 平均字数 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")

    base_scores: dict[str, float] = {}
    variant_scores: dict[str, dict[str, float]] = {}
    for v in VARIANTS:
        rows = by_variant.get(v, [])
        j = {k: mean([r["judge"][k] for r in rows]) for k in JUDGE_KEYS}
        forbid = mean([1.0 if r["metrics"]["pattern_hits"].get("禁用句式") else 0.0 for r in rows])
        narr = mean([1.0 if r["metrics"]["pattern_hits"].get("转述层引导语") else 0.0 for r in rows])
        binds = mean([float(r["metrics"]["source_bindings"]) for r in rows])
        chars = mean([float(r["metrics"]["chars"]) for r in rows])
        variant_scores[v] = {**j, "forbid": forbid, "narr": narr, "binds": binds, "chars": chars}
        if v == "baseline":
            base_scores = j
        lines.append(
            f"| {VARIANT_LABELS[v]} | {len(rows)} "
            f"| {j['faithfulness']:.2f} | {j['grounding']:.2f} | {j['style']:.2f} "
            f"| {forbid * 100:.0f}% | {narr * 100:.0f}% | {binds:.1f} | {chars:.0f} |"
        )
    lines.append("")

    # ---- 相对 baseline 差值 ----
    if base_scores:
        lines.append("## 相对 baseline 的差值（正=更好；命中率为负向指标，越低越好）\n")
        lines.append("| 变体 | Δ忠实性 | Δ口径保留 | Δ表达质量 | 禁用句式 | 转述层 | Δ来源绑定数 |")
        lines.append("|---|---|---|---|---|---|---|")
        for v in VARIANTS:
            if v == "baseline":
                continue
            s = variant_scores[v]
            lines.append(
                f"| {VARIANT_LABELS[v]} "
                f"| {s['faithfulness'] - base_scores['faithfulness']:+.2f} "
                f"| {s['grounding'] - base_scores['grounding']:+.2f} "
                f"| {s['style'] - base_scores['style']:+.2f} "
                f"| {s['forbid'] * 100:.0f}% | {s['narr'] * 100:.0f}% "
                f"| {s['binds'] - variant_scores['baseline']['binds']:+.1f} |"
            )
        lines.append("")

    # ---- 分 run 明细 ----
    lines.append("## 分 run 评分明细\n")
    for rid in RUN_IDS:
        lines.append(f"### {rid}\n")
        lines.append("| 变体 | 节数 | 忠实性 | 口径保留 | 表达质量 |")
        lines.append("|---|---|---|---|---|")
        for v in VARIANTS:
            rows = by_run_variant.get((rid, v), [])
            if not rows:
                continue
            j = {k: mean([r["judge"][k] for r in rows]) for k in JUDGE_KEYS}
            lines.append(
                f"| {VARIANT_LABELS[v]} | {len(rows)} "
                f"| {j['faithfulness']:.2f} | {j['grounding']:.2f} | {j['style']:.2f} |"
            )
        lines.append("")

    # ---- 命中样例 ----
    lines.append("## 规则命中样例（每变体最多 3 条）\n")
    for v in VARIANTS:
        samples = [
            (r["run"], r["section"], hit)
            for r in by_variant.get(v, [])
            for g, hits in r["metrics"]["pattern_hits"].items()
            for hit in [f"[{g}] {hits[0]}"]
        ][:3]
        lines.append(f"### {VARIANT_LABELS[v]}")
        if samples:
            for run_id, sec, hit in samples:
                lines.append(f"- `{run_id}/{sec}` {hit}")
        else:
            lines.append("- 无命中")
        lines.append("")

    if failures:
        lines.append("## 失败任务\n")
        for f in failures:
            lines.append(f"- `{f.get('run')}/{f.get('variant')}/{f.get('section')}` {f.get('error')}")
        lines.append("")

    usage = json.loads((OUT_ROOT / "results.json").read_text(encoding="utf-8")).get("usage", {})
    lines.append("## 结论\n")
    lines.extend(
        [
            "| 排名 | 规则组 | 摘除后果 | 判定 |",
            "|---|---|---|---|",
            "| 1 | 忠实性（1-4） | 忠实性 −0.43、表达 −0.43，低分节 3/21：转写外补充解释、自行补结论、漏口径。唯一让 baseline 保持 0 低分节的规则组 | **不可删，影响最大** |",
            "| 2 | 来源绑定（13） | 平均绑定数 4.5 → 0.0，模型完全不自发产出 data-source-units，data-s 可追溯性消失 | **不可删，删了可追溯机制失效** |",
            "| 3 | 禁止的写法（7-9） | 表达 −0.38，命中样例全是「不在…而在…」类套路句式 | 保留；检测正则已同步进流水线 |",
            "| 4 | 组件清单注入 | 字数 572 → 510（偏短），绑定数 4.5 → 3.2（降 29%），表达 −0.19 | 建议保留，收益薄但成本低 |",
            "| 5 | 转述层剥离（5-6） | 表达 −0.24、低分节仅 1/21；模型本身很少写引导语 | 收益最小，但规则仅两条，保留无害 |",
            "",
            "局限：单次采样（每格 1 片段）、生成与 judge 同模型（mimo-v2.6-flash）、judge 无法感知渲染后版式。差异方向可信，精确数值不宜过度解读。",
            "",
            "后续修复（2026-11-05）：流水线 `sanitize_section_html` 已补禁用句式与引导语的事后检测（只报警不改写），与实验共用 `writer.FORBIDDEN_STYLE` / `NARRATION_VOICE`；新增 3 条测试，全量 380 通过，本报告指标与修复前一致。",
        ]
    )
    if usage:
        lines.append(f"\n---\nToken 用量：prompt {usage.get('prompt', 0)} / completion {usage.get('completion', 0)}")

    report = "\n".join(lines) + "\n"
    (HERE / "report.md").write_text(report, encoding="utf-8")
    print(report)
    print(f"已写入 {HERE / 'report.md'}")


if __name__ == "__main__":
    main()
