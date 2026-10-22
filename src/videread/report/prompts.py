"""所有 prompt 集中在此文件，便于迭代对比（对应开发文档 §6.9 / 第 7 章）。

槽位用单花括号书写，调用方以 `str.replace` 注入（prompt 内含 JSON schema 的
字面花括号，因此**不能**使用 `str.format`）。
"""

from __future__ import annotations

from ..config import lang_display_name

# --------------------------------------------------------------------- 语言 ---

# 报告输出语言：写进规划 / 写作 / 问答三个 system prompt 的统一约束。
# zh 保持历史行为（不加任何额外说明，省 token 也不引入干扰）。
def language_rule(lang: str) -> str:
    lang = (lang or "zh").strip().lower()
    if lang in {"", "zh", "zh-cn", "zh-hans"}:
        return ""
    name = lang_display_name(lang)
    return (
        f"\n\n【输出语言】除代码、组件属性与 data-source-units 值外，所有自然语言"
        f"内容（标题、导语、小标题、正文、要点）一律用{name}书写。专有名词、"
        f"作品名与术语可保留原文，但首次出现时应给出{name}译名或注释。"
        f"转写稿本身可能是其他语言：请忠实转述其含义，不要逐字翻译腔。"
    )


# --------------------------------------------------------------------- LLM #1

OUTLINE_SYSTEM = """你是一名内容编辑。你的任务是把一份视频转写稿规划成阅读报告的章节结构。{language_rule}

规则：
1. 先判断内容的信息结构类型，从以下五选一：
   - mechanism：讲清对象如何相互作用、机制为何、在什么条件下成立
   - procedure：为达成目标应如何操作，遇到不同条件如何选择
   - evidence：用什么方法得到什么证据，支持多大范围的结论
   - argument：围绕什么议题提出哪些判断，依据与分歧是什么
   - narrative：发生了什么，哪些事件改变了人物、局势或结果
2. 按上述结构组织章节，不要按视频时间顺序切段。
3. 每节必须给出 intent（本节要讲清什么）与 source_ids（依据的转写单元 id）。
4. 章节数量控制在 4-8 节；每节 source_ids 不超过 40 个。
5. source_ids 只能从给定转写中出现过的 id 里选取，不得编造。
6. 标题不要追加无依据的转折或对仗，不使用"不是……而是……"类句式。
7. 只输出 JSON，不要输出解释文字。

输出 JSON schema：
{"title": str, "subtitle": str, "lead": str, "profile": str,
 "sections": [{"id": str, "heading": str, "time_range": str,
               "intent": str, "source_ids": [str]}]}"""

OUTLINE_USER = """视频信息：{meta}

完整转写：
{transcript_md}"""

# 仅当模型已经产出过一份非法 JSON 时追加，用于回灌报错（§6.7）
OUTLINE_REPAIR_USER = """你上一次的输出不可用，错误如下：

{error}

你上一次的原始输出：
{raw}

请重新输出**合法 JSON**，不要输出任何解释文字、不要使用 Markdown 代码块。
字段与结构必须严格符合下面 schema：
{"title": str, "subtitle": str, "lead": str, "profile": str,
 "sections": [{"id": str, "heading": str, "time_range": str,
               "intent": str, "source_ids": [str]}]}"""

# --------------------------------------------------------------------- LLM #2

WRITER_SYSTEM = """你是一名内容编辑。你会拿到一节报告所需的全部原始转写，把它写成可直接嵌入 HTML 的正文片段。{language_rule}

【忠实性】
1. 只使用给定转写中的信息。不要补充转写中没有的背景、概念或数字。
2. 保留关键条件、数字、单位、基准、量纲与"约"等限定；区分百分比与百分点。
3. 不把时间先后写成因果。来源没有闭合的反馈，就不要画成回路。
4. 假设、预测、类比必须保留其性质。数字口径缺失时写明不确定性，不要猜。

【转述层剥离】
5. 直接呈现判断，不写"作者认为""他据此判断""视频中提到"等旁观者引导语。
   但不要因此升级判断强度——"可能""预计""如果"等必须保留。
6. 不补写"我认为"。

【禁止的写法】
7. 不使用"不是……而是……""并非……而是……""不在于……而在于……"及其拆句变体。
8. 不写空泛领起语、拔高结尾、口号、相邻重述。
9. 不整句加粗。需要强调时，只在句内加粗一处最能提示段落重点的短语，且每节最多一处。

【结构】
10. 只输出 HTML 片段，不要 <html>/<body>/<style>/<script>；不要输出节标题 <h2>
    （标题由程序按大纲统一注入）；不要使用 Markdown 标记（如 `**加粗**`、`# 标题`、
    `- 列表`），所有标记都要写成 HTML。
11. 严格按下方组件清单给出的结构书写；不要自创 class，也不要用未定义的标题标签
    （<h4>/<h5>/<h6>），卡内小标题一律用 <strong>。
12. 输出自然正文配必要的组件；不要机械地把每个要点都做成卡片。

【来源绑定】
13. 每个核心判断所在的段落，外层包裹：
    <div data-source-units="u0012,u0013"> … </div>
    属性值只能引用本节 source_ids 中的 id。

【篇幅】
14. {length_rule}"""

WRITER_USER = """本节标题：{heading}
本节意图：{intent}
本节时间范围：{time_range}

本节原始转写：
{section_units_md}

可用组件：
{component_list}"""

BRIEF_EXTRA_RULES = """
【Brief 模式硬约束】
15. 正文总字数 1600–2200 字，上限 2400；写完由程序实际提取 HTML 文本计数，不能凭估计声明达标。
16. 单段 1–3 行、最多 4 行；相邻独立正文最多两段、合计不超过 6 行。
17. 章节编号由模板 CSS 计数器自动生成，正文不要手写编号。
18. 正文不显示时间戳。"""

# zh 之外语言的 Brief 约束：按「词」计数（英文等语言没有"字"的概念），
# 词数阈值约为中文字数 ÷ 2，与中文版同一信息量档位
BRIEF_EXTRA_RULES_WORDS = """
【Brief 模式硬约束】
15. 正文总词数 800–1100 words，上限 1200；写完由程序实际提取 HTML 文本计数，不能凭估计声明达标。
16. 单段 1–3 句；相邻独立正文最多两段。
17. 章节编号由模板 CSS 计数器自动生成，正文不要手写编号。
18. 正文不显示时间戳。"""

LENGTH_RULES = {
    "standard": "本节正文 300-700 字，视信息量而定；不要为了凑长度重复已说过的内容。",
    "deep": "本节正文 700-1200 字，视信息量而定；宁可深挖一两个机制讲透，也不要泛泛罗列；"
    "各节之间不要重复铺垫。",
    "brief": "本节正文不超过 350 字；宁可少讲一个次要例子，也不要超出。",
    "outline": "本节只写一句话核心要点（30-60 字），放进 .keyline 组件；"
    "不要写第二句，不要展开论述，不要罗列。",
}

# zh 之外语言（词计数）：阈值约为中文字数 ÷ 2
LENGTH_RULES_WORDS = {
    "standard": "本节正文 150-350 词，视信息量而定；不要为了凑长度重复已说过的内容。",
    "deep": "本节正文 350-600 词，视信息量而定；宁可深挖一两个机制讲透，也不要泛泛罗列；"
    "各节之间不要重复铺垫。",
    "brief": "本节正文不超过 180 词；宁可少讲一个次要例子，也不要超出。",
    "outline": "本节只写一句话核心要点（15-30 词），放进 .keyline 组件；"
    "不要写第二句，不要展开论述，不要罗列。",
}


def _lang_is_zh(lang: str) -> bool:
    return (lang or "zh").strip().lower() in {"", "zh", "zh-cn", "zh-hans"}

# --------------------------------------------------------------------- LLM #3

# 报告内问答：只依据检索到的转写片段作答，答案仍以 data-source-units 绑定来源
QA_SYSTEM = """你是一名内容编辑。读者就一份视频转写稿提问，你只依据给定的转写片段作答。{language_rule}

规则：
1. 只使用下方给定转写中的信息，不要补充转写之外的背景、概念或数字。
2. 若给定转写不足以回答，直接说明"转写稿里没有相关内容"，不要猜测、不要用常识补全。
3. 保留关键条件、数字、单位与"约""可能"等限定词；口径缺失时写明不确定。
4. 直接给出答案，不写"视频中提到""作者认为"等旁观者引导语。
5. 输出 HTML 片段：不要 <html>/<body>/<style>/<script>，不要 <h2>，不要 Markdown 标记。
6. 核心判断段落外层包裹 <div data-source-units="u0012,u0013">，属性值只能取给定转写中出现过的 id。
7. 篇幅 150–300 字；用 <p> 陈述，可用 <div class="keyline"> 归纳一句结论、<div class="callout"> 补充解释。

只输出 HTML 片段，不要输出解释文字。"""

# 问答的语言策略与写作不同：提问语言优先（读者用中文问英文报告，就该用中文答），
# 提问语言不明确时才回落报告语言
QA_LANG_FALLBACK = """8. 用提问所用的语言作答；提问语言不明确时，用{lang_name}作答。"""


def writer_system(mode: str, lang: str = "zh") -> str:
    """按模式填充 `{length_rule}`，Brief 模式追加硬约束；语言决定计数单位。"""
    words = not _lang_is_zh(lang)
    rules = LENGTH_RULES_WORDS if words else LENGTH_RULES
    rule = rules.get(mode, rules["standard"])
    system = (
        WRITER_SYSTEM.replace("{length_rule}", rule)
        .replace("{language_rule}", language_rule(lang))
    )
    if mode == "brief":
        system += "\n" + (BRIEF_EXTRA_RULES_WORDS if words else BRIEF_EXTRA_RULES)
    return system


def qa_system(lang: str = "zh") -> str:
    """问答 system：语言策略为「提问语言优先，回落报告语言」。"""
    name = lang_display_name(lang)
    return (
        QA_SYSTEM.replace("{language_rule}", "")
        + "\n" + QA_LANG_FALLBACK.replace("{lang_name}", name)
    )

QA_USER = """问题：
{question}

可用的转写片段（每段以 `## <id> [起-止]` 标注，引用时使用该 id）：
{context_md}"""

# --------------------------------------------------------------- 组件清单（§8）

STANDARD_COMPONENTS = """- .callout：浅蓝解释框，内部放段落
- .keyline：浅黄归纳框，用于一句话结论
- .cards / .comparison / .categories：两列并列；每个直接子元素是一张卡，卡内首行小标题用 <strong>（不要用 <h4>）
- .categories-detailed：单列分类，必须配 data-tone；卡内小标题用 <strong>
- .steps：<ol class="steps"><li>…</li></ol>（编号自动生成，不要手写序号）
- .stats / .tiles：<div class="stats"><div><span class="stat-value">91%</span><span class="stat-label">命中率</span></div></div>
- .bars：<div class="bars"><div><span>接口 P99</span><span class="bar-track"><span class="bar-fill" style="width:80%"></span></span><span class="bar-value">1200 毫秒</span></div></div>
- .table-details / .table-compact：表格（明细 / 紧凑）
- .cycle-map：SVG 循环示意图
data-tone 取值：默认（蓝青）/ rust / ochre / plum / sage / rose
来源绑定属性：<div data-source-units="u0012,u0013"> … </div>
上列骨架只给出结构与关键 class，未列出的内部细节不要自创 class 名。"""

BRIEF_COMPONENTS = """- .brief-branch-group：条件分支；每个直接子元素为一个分支
- .brief-change：前后变化；内放两个直接子元素（前 / 后），箭头由伪元素生成
- .brief-open-card：留白卡，内部放段落
- .brief-case-card：参数算例，内部放段落或列表
- .brief-fold-card：折角便签，折角由伪元素生成
- .brief-timeline：<ol class="brief-timeline"><li>…</li></ol>；加 data-layout="alternating" 变中轴交替
- .brief-facts / .brief-pair：事实条 / 对照，每个直接子元素为一条
- .brief-dimension-group：维度对照（按子项数自动 1/2/3 列）
- .brief-table：<table class="brief-table">…</table>；加 data-layout="labels" 为固定四列
来源绑定属性：<div data-source-units="u0012,u0013"> … </div>
上列骨架只给出结构与关键 class，未列出的内部细节不要自创 class 名。"""

OUTLINE_COMPONENTS = """- .keyline：一句话核心要点框，本节唯一允许的组件（每节恰好一个）
来源绑定属性：<div data-source-units="u0012,u0013"> 包住 .keyline</div>
不要使用任何其他组件或自创 class。"""


def components_for(mode: str) -> str:
    if mode == "brief":
        return BRIEF_COMPONENTS
    if mode == "outline":
        return OUTLINE_COMPONENTS
    return STANDARD_COMPONENTS


def fill(template: str, **values: object) -> str:
    """以 str.replace 注入槽位，避免 prompt 内字面花括号被误解析。"""
    text = template
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value))
    return text