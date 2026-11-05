"""写作 Prompt 规则消融实验。

设计：
- 消融对象：逐节写作 system prompt 的四个规则组（忠实性 / 转述层剥离 /
  禁止的写法 / 来源绑定）+ 组件清单注入（user prompt）。
- 控制变量：大纲固定复用 runs/ 里现成的 outline.json（所有变体共享同一份
  大纲与转写），只有写作 prompt 不同；不重新下载、不重新转写。
- 评估：确定性指标（禁用句式命中、转述层引导语、来源绑定数、自创 class、
  篇幅）+ LLM-as-judge 三维评分（忠实性 / 口径保留 / 表达质量）。
- 产物：out/<run_id>/<variant>/sN.html（清洗后的片段）、sN.judge.json、
  results.json（全部明细）、report.md（汇总表）。

运行：.venv/Scripts/python.exe experiments/ablation/run_ablation.py
"""

from __future__ import annotations

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from videread.config import get_settings  # noqa: E402
from videread.report import prompts  # noqa: E402
from videread.report.llm import LlmClient  # noqa: E402
from videread.report.outline import load as load_outline  # noqa: E402
from videread.report.writer import (  # noqa: E402
    FORBIDDEN_STYLE,
    NARRATION_VOICE,
    SOURCE_UNITS,
    count_text_chars,
    sanitize_section_html,
)
from videread.render import template_classes  # noqa: E402
from videread.transcript import load_units, units_to_markdown  # noqa: E402

RUNS_ROOT = ROOT / "runs"
OUT_ROOT = ROOT / "experiments" / "ablation" / "out"
TEMPLATES = ROOT / "src" / "videread" / "templates" / "report.html"

RUN_IDS = [
    "BV1Nrn86JEou-dfd6a622",  # mechanism，7 节
    "BV1PznU6YEZE-21ea7f4f",  # procedure，8 节
    "BV1HFaz6bEKK-35eafb02",  # argument，6 节
]

WORKERS = 4

# ---------------------------------------------------------------- 变体构造 ---

BLOCK_SPLIT = re.compile(r"(?=\n\n【)")


def _split_blocks(system: str) -> list[str]:
    return BLOCK_SPLIT.split(system)


def _drop_block(system: str, header: str) -> str:
    return "".join(
        seg for seg in _split_blocks(system) if not seg.lstrip("\n").startswith(f"【{header}】")
    )


def _base_system() -> str:
    return prompts.writer_system("standard", "zh")


NO_COMPONENTS_RULE11 = (
    "11. 不要自创 class，也不要用未定义的标题标签\n"
    "    （<h4>/<h5>/<h6>），卡内小标题一律用 <strong>。"
)

NO_COMPONENTS_LIST = "（本次未提供组件清单，请用朴素的 <p> / <ol>/<ul> / <table> 组织内容）"


def build_variant_system(name: str) -> str:
    system = _base_system()
    if name == "baseline":
        return system
    if name == "no_faithfulness":
        return _drop_block(system, "忠实性")
    if name == "no_narrative":
        return _drop_block(system, "转述层剥离")
    if name == "no_forbidden":
        return _drop_block(system, "禁止的写法")
    if name == "no_source_binding":
        return _drop_block(system, "来源绑定")
    if name == "no_components":
        return system.replace(
            "11. 严格按下方组件清单给出的结构书写；不要自创 class，也不要用未定义的标题标签\n"
            "    （<h4>/<h5>/<h6>），卡内小标题一律用 <strong>。",
            NO_COMPONENTS_RULE11,
        )
    raise ValueError(name)


def build_component_list(name: str) -> str:
    if name == "no_components":
        return NO_COMPONENTS_LIST
    if name == "no_source_binding":
        return "\n".join(
            line
            for line in prompts.components_for("standard").splitlines()
            if not line.startswith("来源绑定属性")
        )
    return prompts.components_for("standard")


VARIANTS = ["baseline", "no_faithfulness", "no_narrative", "no_forbidden", "no_source_binding", "no_components"]

VARIANT_LABELS = {
    "baseline": "完整规则（对照）",
    "no_faithfulness": "− 忠实性（规则 1-4）",
    "no_narrative": "− 转述层剥离（规则 5-6）",
    "no_forbidden": "− 禁止的写法（规则 7-9）",
    "no_source_binding": "− 来源绑定（规则 13）",
    "no_components": "− 组件清单注入",
}

# ------------------------------------------------------------ 确定性指标 ---

PATTERNS = {
    "禁用句式": FORBIDDEN_STYLE,
    "转述层引导语": NARRATION_VOICE,
}
_TAG = re.compile(r"<[^>]+>")
_STRONG = re.compile(r"<strong>([^<]{16,})</strong>")


def det_metrics(html: str) -> dict:
    text = _TAG.sub("", html)
    hits: dict[str, list[str]] = {}
    for group, pat in PATTERNS.items():
        found = list(dict.fromkeys(pat.findall(text)))
        if found:
            hits[group] = found[:5]
    return {
        "chars": count_text_chars(html),
        "source_bindings": len(SOURCE_UNITS.findall(html)),
        "long_bold_count": len(_STRONG.findall(html)),
        "pattern_hits": hits,
    }


# ---------------------------------------------------------------- judge ---


JUDGE_SYSTEM = """你是严格的评审，评估根据转写片段写成的报告片段。只依据给定转写判断，不要使用外部知识。

评分维度（均为 1-5 整数）：
- faithfulness 忠实性：5=没有任何转写之外的信息；3=有少量常识性补充；1=出现转写中没有的数字、结论或归因
- grounding 口径保留：5=关键数字、条件、单位与"约/可能"等限定完整保留；1=数字与条件大量丢失或口径错误
- style 表达质量：5=直接呈现判断，无旁观者引导语、无"不是…而是…"类套路句式、无空泛拔高；1=大量引导语或套路

只输出 JSON：{"faithfulness": 整数, "grounding": 整数, "style": 整数, "issues": "不超过50字的问题清单，无问题给空串"}"""


def judge_section(client: LlmClient, model: str, units_md: str, html: str) -> dict:
    user = f"转写片段：\n{units_md}\n\n报告片段（HTML）：\n{html}"
    try:
        data, _raw = client.complete_json(system=JUDGE_SYSTEM, user=user, model=model)
    except Exception:
        raw = client.complete(system=JUDGE_SYSTEM, user=user, model=model)
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m is None:
            raise
        data = json.loads(m.group(0))
    out = {}
    for key in ("faithfulness", "grounding", "style"):
        out[key] = max(1, min(5, int(data.get(key, 3))))
    out["issues"] = str(data.get("issues", ""))[:120]
    return out


# ------------------------------------------------------------------ 主流程 ---


def load_run(run_id: str):
    run_dir = RUNS_ROOT / run_id
    units = load_units(run_dir / "transcript.jsonl")
    outline = load_outline(run_dir / "outline.json")
    return run_dir, units, outline


def run_one_section(client: LlmClient, model: str, allowed_classes: set[str],
                    run_id: str, variant: str, section, unit_map: dict,
                    system: str, component_list: str, out_dir: Path) -> dict:
    """生成 + 清洗 + 确定性指标 + judge；产物落盘，支持断点续跑。"""
    html_path = out_dir / f"{section.id}.html"
    judge_path = out_dir / f"{section.id}.judge.json"

    sec_units = [unit_map[u] for u in section.source_ids if u in unit_map]
    if not sec_units:
        return {"run": run_id, "variant": variant, "section": section.id, "error": "无可用转写单元"}

    user = prompts.fill(
        prompts.WRITER_USER,
        heading=section.heading,
        intent=section.intent,
        time_range=section.time_range,
        section_units_md=units_to_markdown(sec_units),
        component_list=component_list,
    )

    if html_path.exists() and html_path.stat().st_size > 0:
        html = html_path.read_text(encoding="utf-8")
        if not (judge_path.exists() and judge_path.stat().st_size > 0):
            scores = judge_section(client, model, units_to_markdown(sec_units), html)
            judge_path.write_text(json.dumps(scores, ensure_ascii=False, indent=1), encoding="utf-8")
    else:
        raw = client.complete(system=system, user=user, model=model)
        html, violations = sanitize_section_html(raw, set(section.source_ids), allowed_classes)
        html_path.write_text(html, encoding="utf-8")
        scores = judge_section(client, model, units_to_markdown(sec_units), html)
        judge_path.write_text(json.dumps(scores, ensure_ascii=False, indent=1), encoding="utf-8")
        if violations:
            print(f"  [{run_id}/{variant}/{section.id}] 清洗：{';'.join(violations)}", flush=True)

    metrics = det_metrics(html)
    scores = json.loads(judge_path.read_text(encoding="utf-8"))
    print(f"[{run_id}/{variant}/{section.id}] chars={metrics['chars']} "
          f"bind={metrics['source_bindings']} "
          f"j=({scores['faithfulness']}/{scores['grounding']}/{scores['style']})", flush=True)
    return {"run": run_id, "variant": variant, "section": section.id,
            "metrics": metrics, "judge": scores}


def main() -> None:
    settings = get_settings()
    model = settings.llm_model_write
    client = LlmClient(settings)
    allowed_classes = template_classes(TEMPLATES)
    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    systems = {v: build_variant_system(v) for v in VARIANTS}
    comp_lists = {v: build_component_list(v) for v in VARIANTS}

    (OUT_ROOT / "variants").mkdir(exist_ok=True)
    for v in VARIANTS:
        (OUT_ROOT / "variants" / f"{v}.system.txt").write_text(systems[v], encoding="utf-8")

    runs = {rid: load_run(rid) for rid in RUN_IDS}

    tasks = []
    for rid, (_dir, units, outline) in runs.items():
        unit_map = {u.id: u for u in units}
        for variant in VARIANTS:
            out_dir = OUT_ROOT / rid / variant
            out_dir.mkdir(parents=True, exist_ok=True)
            for section in outline.sections:
                tasks.append((rid, variant, section, unit_map))

    print(f"共 {len(tasks)} 个（run × 变体 × 节）任务，模型 {model}", flush=True)
    results, failures = [], []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {
            pool.submit(
                run_one_section, client, model, allowed_classes,
                rid, variant, section, unit_map,
                systems[variant], comp_lists[variant], OUT_ROOT / rid / variant,
            ): (rid, variant, section.id)
            for rid, variant, section, unit_map in tasks
        }
        for i, future in enumerate(as_completed(futures), 1):
            key = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001
                failures.append({**dict(zip(("run", "variant", "section"), key)), "error": repr(exc)})
                print(f"FAILED {key}: {exc!r}", flush=True)
            if i % 20 == 0:
                print(f"进度 {i}/{len(tasks)}  耗时 {time.time()-t0:.0f}s  tokens={client.usage.total}", flush=True)

    (OUT_ROOT / "results.json").write_text(
        json.dumps({"results": results, "failures": failures,
                    "usage": {"prompt": client.usage.prompt_tokens,
                              "completion": client.usage.completion_tokens}},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"完成：成功 {len(results)}，失败 {len(failures)}，"
          f"tokens prompt={client.usage.prompt_tokens} completion={client.usage.completion_tokens}",
          flush=True)


if __name__ == "__main__":
    main()
