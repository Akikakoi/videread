# 视读 · 开发文档

| 项    | 内容                                |
| ---- | --------------------------------- |
| 文档版本 | v1.0                              |
| 编写日期 | 2026-09-27                        |
| 项目代号 | videread                         |
| 使用形态 | 个人本地工具（单机、无服务器、无账号）               |
| 目标平台 | Windows 10/11（同时兼容 macOS / Linux） |

---

## 1. 项目概述

### 1.1 目标

输入一个 Bilibili 视频链接或 BV 号（或本地音视频文件），自动完成 **下载 → 转写 → 内容编辑 → 生成一份自包含的 HTML 阅读报告**，双击即可阅读，也可导出为长图分享。

报告不是"逐句摘要"，而是**按信息结构重新组织的阅读材料**：保留关键条件、数字、公式、归因与不确定性，让没看过视频的人也能理解主要内容。

### 1.2 非目标（明确不做）

- 不做服务端、不做账号体系、不做多用户额度与并发调度
- 不支持 Bilibili 之外的平台解析（小红书/抖音等需要额外解析，稳定性差）
- 不做多 Agent 流水线（不引入 planner / critic / 逐步审批）
- 不做"逐句摘要"或"时间轴流水账"
- 不生成 Markdown 作为最终交付物（HTML 才是交付形态）

### 1.3 验收标准

| 编号 | 标准                                          |
| -- | ------------------------------------------- |
| A1 | `videread <url>` 一条命令跑通，中间无需人工干预           |
| A2 | 输出单个 `report.html`，无外部依赖（无 CDN、无外链图片、无外链字体） |
| A3 | 同一视频第二次运行，跳过下载与转写，不产生重复的 ASR 费用             |
| A4 | 报告中每个核心判断可通过 `data-source-units` 追溯到转写单元    |
| A5 | 报告中不出现"作者认为""不是……而是……"等转述层与套路句式             |
| A6 | 1 小时视频端到端耗时不超过 15 分钟                        |

---

## 2. 技术选型

| 环节      | 选型                                        | 理由                                          |
| ------- | ----------------------------------------- | ------------------------------------------- |
| 语言 / 环境 | Python 3.12 + `uv`                        | Windows 友好，依赖隔离干净，`uv run` 免手动建环境           |
| 视频下载    | `yt-dlp`                                  | B 站支持最好，可同时拿到标题 / UP主 / 时长 / 封面 / 字幕        |
| 音视频处理   | `ffmpeg` + `ffprobe`                      | 抽音频、静音点检测、时长探测                              |
| ASR     | 通义 Paraformer（阿里百炼）｜备用 OpenAI `whisper-1` | 云端、免显卡、按量付费；用 adapter 抽象便于替换                |
| LLM     | DeepSeek / 通义千问（OpenAI 兼容接口）              | 中文表现好、成本低、`openai` SDK 直接调用                 |
| HTTP    | `httpx`                                   | 超时与重试控制比 `requests` 更清晰                     |
| 模板渲染    | 纯字符串占位符替换（**不用 Jinja2**）                  | 模板本身是完整 HTML，替换 `{{BODY}}` 即可，避免 HTML 被二次转义 |
| 配置      | `python-dotenv` + 环境变量                    | 个人工具，够用                                     |
| 测试      | `pytest`                                  | 标准选择                                        |

> **为什么不引入 Coding Agent（如 Pi / Claude Code）**：本项目的目标是可复现的确定性管道。引入外部 Agent 会让报告质量被 Agent 自身行为绑定，且无法精确定位失败环节。报告质量通过 prompt 迭代来控制。

---

## 3. 整体架构

### 3.1 流水线

```
Bilibili URL / BV 号
   │
   ├─[1] 下载      yt-dlp ──→ meta.json + audio.m4a
   │
   ├─[2] 音频处理  ffmpeg ──→ 16kHz 单声道 wav + 静音点
   │
   ├─[3] 转写      云 ASR ──→ asr.raw.jsonl（带时间戳）
   │
   ├─[4] 规范化    ────────→ transcript.jsonl + transcript.md   ← 唯一事实来源
   │
   ├─[5] 结构规划  LLM #1 ─→ outline.json（章节 + intent + source_ids）
   │
   ├─[6] 逐节写作  LLM #2 ─→ 各节 HTML 片段
   │
   └─[7] 渲染      ────────→ report.html
```

### 3.2 核心设计原则

1. **转写是唯一事实来源**  
   报告只引用、不改写。LLM 永远只拿到 `transcript.md`，不接触原始音频。
2. **转写与报告彻底解耦**  
   所有中间产物落盘。重跑报告、换模式、换模型都**不重新下载、不重新付费转写**。
3. **报告分两步生成**  
   先"规划结构"（输出 JSON 大纲），再"逐节写内容"。  
   一次性让 LLM 吐 5000 字长文，会导致：结构松散、长度不可控、来源绑定漂移、失败无法局部重试。
4. **HTML 是最终形态**  
   模板自带内联 CSS 与组件类，LLM 只负责填充 `{{BODY}}` 内部的 HTML 片段。

### 3.3 分层

| 层   | 模块                                                    | 职责             |
| --- | ----------------------------------------------------- | -------------- |
| 接口层 | `cli.py`                                              | 参数解析、进度输出、退出码  |
| 编排层 | `pipeline.py`                                         | 阶段调度、断点续跑、异常归类 |
| 领域层 | `download` / `audio` / `asr` / `transcript`           | 确定性数据处理，无 LLM  |
| 生成层 | `report/outline` / `report/writer` / `report/prompts` | 两段式 LLM 调用     |
| 呈现层 | `render.py` + `templates/`                            | 模板填充与落盘        |

---

## 4. 目录结构

```
videread/
├─ pyproject.toml
├─ run.bat                    # 一键启动：自检环境 → 建 .venv 装依赖 → 跑流水线
├─ .env                       # 密钥（不入库）
├─ .env.example
├─ README.md
├─ bin/                       # 可选：ffmpeg.exe / ffprobe.exe 静态包
├─ src/videread/
│  ├─ __init__.py
│  ├─ cli.py
│  ├─ pipeline.py
│  ├─ config.py
│  ├─ download.py
│  ├─ audio.py
│  ├─ errors.py               # 退出码常量与异常分类
│  ├─ execution.py            # 外部命令统一执行（yt-dlp / ffmpeg 共用）
│  ├─ failures.py             # 退出码 → 失败类别 / 用户文案（纯函数）
│  ├─ asr/
│  │  ├─ __init__.py
│  │  ├─ base.py
│  │  ├─ dashscope.py
│  │  ├─ realtime.py          # 实时 WebSocket 后端（不经 OSS 桶）
│  │  └─ openai_whisper.py
│  ├─ transcript.py
│  ├─ report/
│  │  ├─ __init__.py
│  │  ├─ outline.py
│  │  ├─ writer.py
│  │  └─ prompts.py
│  ├─ render.py
│  ├─ trace.py
│  └─ templates/
│     ├─ report.html          # Standard 模板
│     └─ brief-report.html    # Brief 模板
├─ runs/                      # 运行产物（gitignore）
│  └─ <run-id>/
└─ tests/
   ├─ fixtures/
   └─ test_*.py
```

### `runs/<run-id>/` 产物约定

| 文件                 | 说明                       | 可否复用           |
| ------------------ | ------------------------ | -------------- |
| `meta.json`        | 标题 / UP主 / 时长 / URL / 封面 | 是              |
| `audio.m4a`        | 原始音频（`--keep-audio` 时保留） | 是              |
| `audio.wav`        | 16kHz 单声道，ASR 输入         | 可删             |
| `asr.raw.jsonl`    | ASR 原始输出，带时间戳            | **是（最贵，务必保留）** |
| `transcript.jsonl` | 规范化转写单元，带 id             | 是              |
| `transcript.md`    | 同上，人 / 模型可读              | 是              |
| `outline.json`     | 结构规划结果                   | 是              |
| `sections/`        | 各节 HTML 片段，便于单节重试        | 是              |
| `report.html`      | 最终交付                     | —              |
| `run.trace.jsonl`  | 分阶段耗时 / 用量 / 错误          | 是              |

> `run-id` 建议用 `{bvid}-{hash8}`，保证同一视频多次运行落到同一目录，天然实现缓存复用。

---

## 5. 数据契约

> 以下 schema 是模块之间的接口，**变更需同步本文件**。

### 5.1 `meta.json`

```json
{
  "bvid": "BV1xx411c7mD",
  "title": "视频完整标题",
  "uploader": "UP主名称",
  "duration": 2137.0,
  "url": "https://www.bilibili.com/video/BV1xx411c7mD",
  "cover": "https://...jpg",
  "description": "视频简介（可空）",
  "subtitles": [
    {"lang": "zh-CN", "path": "subtitle.zh-CN.srt"}
  ]
}
```

### 5.2 `asr.raw.jsonl`

每行一个 ASR 分段（**不合并、不改写**，便于回溯）：

```json
{"start": 135.20, "end": 142.85, "text": "这段讲的是……"}
```

### 5.3 `transcript.jsonl` / `transcript.md`

规范化后的**转写单元**，是报告的引用单位：

```json
{"id": "u0012", "start": 135.2, "end": 142.8, "text": "……", "source": "asr"}
```

| 字段              | 说明                                   |
| --------------- | ------------------------------------ |
| `id`            | `u` + 4 位序号，从 `u0001` 连续编号，**永不重编号** |
| `start` / `end` | 秒，保留 1 位小数                           |
| `text`          | 该单元正文；`max_chars` 默认 160，过长按标点切分     |
| `source`        | `asr` / `subtitle` / `ocr`，便于区分来源可靠性 |

`transcript.md` 格式：

```markdown
## u0012 [02:15-02:22]
这段讲的是……

## u0013 [02:22-02:30]
……
```

### 5.4 `outline.json`

```json
{
  "title": "主标题",
  "subtitle": "副标题（可空）",
  "lead": "导语，一段话",
  "profile": "mechanism",
  "sections": [
    {
      "id": "s1",
      "heading": "章节标题",
      "time_range": "02:15-08:40",
      "intent": "本节要讲清 X 与 Y 的作用关系",
      "source_ids": ["u0012", "u0013", "u0014"]
    }
  ]
}
```

- `profile` 取值：`mechanism` / `procedure` / `evidence` / `argument` / `narrative`
- `time_range` 必须是**重组后实际使用的来源范围**，不得沿用重组前时间，不得凭篇幅估计
- `source_ids` 是逐节写作时**唯一允许引用的转写范围**

### 5.5 `run.trace.jsonl`

```json
{"ts": "2026-09-27T10:12:03+08:00", "stage": "asr", "event": "end", "dur_ms": 48210, "cost_cny": 0.62, "detail": {"segments": 412}}
```

---

## 6. 模块设计

### 6.1 `cli.py`

职责：参数解析、进度输出、退出码映射。

```bash
# 命令：videread <url> [options]
#   --mode {standard,brief}   阅读模式，默认 standard
#   --out DIR                 产物根目录，默认 ./runs
#   --no-cache                忽略已有产物，全部重跑
#   --keep-audio              保留 audio.m4a
#   --open                    生成后自动用浏览器打开
#   --asr NAME                临时覆盖 ASR 后端
```

**退出码约定**（便于脚本化）：

| 码 | 含义                          |
| - | --------------------------- |
| 0 | 成功                          |
| 1 | 参数错误                        |
| 2 | 下载失败（含 B 站反爬 / 地区限制 / 付费视频） |
| 3 | 音频处理或 ASR 失败                |
| 4 | LLM 调用失败（含 JSON 解析失败重试耗尽）   |
| 5 | 渲染或写盘失败                     |

退出码常量与异常分类定义在 `errors.py`；每个码对应的**失败类别与用户文案**由 `failures.py`
用纯函数提供（`group()` / `hint()`），CLI 与 Web 控制台共用这一份，前端不再各自维护码表。
新增退出码时必须同步在 `failures.py` 登记，`tests/test_failures.py` 会挡住漏登记。

### 6.2 `pipeline.py`

职责：阶段调度 + 断点续跑 + 异常归类。

```python
def run(url: str, *, mode: str = "standard", out_root: Path,
        use_cache: bool = True, keep_audio: bool = False) -> Path:
    """执行完整流水线，返回 report.html 路径。"""
```

要点：

- 每个阶段执行前**先检查产物是否存在**，存在则跳过（`use_cache=True` 时）
- 阶段边界记录 `run.trace.jsonl`
- 将底层异常归类为上面 5 类退出码
- **不吞异常**，失败时打印出错阶段与已生成产物路径（便于手工续跑）

### 6.3 `download.py`

```python
def fetch_meta(url: str) -> VideoMeta
def download_audio(url: str, dest: Path, *, keep: bool = False) -> Path
def download_subtitles(url: str, dest: Path) -> list[dict]
def pick_subtitle(subtitles: list[dict], run_dir: Path) -> Path | None
```

要点：

- 先用 `yt-dlp --dump-json` 拿元信息（不下载），失败即归类为下载失败
- 音频格式优先 `m4a`；不支持时回退 `bestaudio`
- 若视频自带字幕，一并下载到 `subtitle.<lang>.srt`
- 多字幕并存时按 `zh-CN → zh-Hans → zh → zh-Hant → en` 取用（`pick_subtitle`）；`--sub-langs` 只做过滤，不决定取哪一份
- **字幕优先**（`SUBTITLE_FIRST`，默认开）：字幕覆盖视频时长 ≥ 50% 时判为完整，直接由字幕建转写稿，跳过音频下载、音频处理与 ASR；覆盖不足或字幕文件缺失时回退到「下载音频 + ASR」。CLI 可用 `--force-asr` 单次强制走 ASR
- 校验：`duration > 0` 且 `duration <= 2 * 3600`（超 2 小时拒绝，避免 ASR 成本失控）；本地文件在流水线阶段 1 走同一上限
- 起子进程一律走 `execution.run_command()`：统一 utf-8 / 文本模式 / 超时，并把「程序缺失」「超时」归一化为 `DownloadError`（本模块与 §6.4 的 ffmpeg 封装共用同一入口）

### 6.4 `audio.py`

```python
def to_wav_16k_mono(src: Path, dest: Path) -> None
def probe_duration(src: Path) -> float
def detect_silence(src: Path, *, noise_db: int = -35,
                   min_silence: float = 0.6) -> list[float]
```

要点：

- 统一输出 `pcm_s16le` / 16000Hz / 单声道，这是绝大多数云 ASR 的推荐输入
- `detect_silence` 返回静音区中点列表，供长音频切段使用
- `ffmpeg` / `ffprobe` 解析顺序：环境变量 `FFMPEG_BIN` → 项目 `bin/` → 系统 PATH

### 6.5 `asr/`

```python
# asr/base.py
@dataclass(frozen=True)
class AsrSegment:
    start: float
    end: float
    text: str

class AsrBackend(Protocol):
    def transcribe(self, audio: Path, *,
                   duration: float | None = None) -> list[AsrSegment]: ...
```

```python
# asr/dashscope.py
class DashScopeAsr:
    """通义 Paraformer 实现。"""

# asr/realtime.py
class DashScopeRealtimeAsr:
    """通义 Paraformer 实时（WebSocket）实现：本地音频直推，不经 OSS。"""
```

要点：

- **接入点可配置**：默认 `https://dashscope.aliyuncs.com/api/v1`；阿里云专属/私有化（MaaS）部署用
  `DASHSCOPE_BASE_URL` 覆盖即可（协议同构：`/uploads?action=getPolicy` → OSS 直传 → `/services/audio/asr/transcription` → `/tasks/{id}` 轮询）。
  注意各地域的 API Key 不通用，Key 需与控制台所选地域一致
- **上传与提交**：`/uploads?action=getPolicy` 拿 OSS 直传凭证 → 直传 → 提交异步任务时必须带
  `X-DashScope-OssResourceResolve: enable`（声明 `file_urls` 是上传接口返回的 `oss://` 资源），
  否则服务端读不到文件、只回一个含糊的 `SERVER_ERROR`
- **实时（WebSocket）备选后端**：`ASR_BACKEND=dashscope-realtime`。把本地 wav 切成 100ms 的 PCM
  二进制帧直接推给 `{base}/api-ws/v1/inference`（地址由 `DASHSCOPE_BASE_URL` 推导：去掉 `/api/v1` 或
  `/compatible-mode/v1`，`https` → `wss`，再拼 `/api-ws/v1/inference`）。事件顺序为
  `run-task`（JSON）→ 二进制音频帧 → `finish-task`（JSON），服务端回
  `task-started` / `result-generated` / `task-finished` / `task-failed`。只取 `sentence_end` 为真的句子
  （时间戳由毫秒换算为秒），任务结束时以最后一条中间结果兜底，避免丢掉句尾。
  **这条路径不读写 OSS 上传桶**，适用于只发放工作空间 `sk-ws-` 凭据、异步上传路径报
  `Resource.AccessDenied (ownership check failed)` 的账号。收发必须并发，故用 asyncio 版 WebSocket 客户端
  （同步 API 跨线程收发会卡住）。模型取 `DASHSCOPE_MODEL`（含 `realtime` 时）否则 `paraformer-realtime-v2`
- **长音频切段**：云 ASR 通常有单次时长上限。用 `detect_silence` 找到接近上限的静音点切分；异步后端切段后**并行提交**（建议并发 2–3），实时后端因单连接顺序流式，**顺序提交**，最后按 `start` 排序拼接
- 时间戳修正：每段 ASR 返回的时间戳是**段内相对时间**，需要加上该段的全局偏移
- 失败重试：指数退避，最多 3 次；单段失败不影响其他段，失败段写进 `run.trace.jsonl`
- 结果**立即落盘** `asr.raw.jsonl`，避免重试导致重复付费

### 6.6 `transcript.py`

```python
def build_units(segments: list[AsrSegment], *,
                max_chars: int = 160) -> list[TranscriptUnit]
def merge_subtitles(units: list[TranscriptUnit], srt: Path) -> list[TranscriptUnit]
def write_jsonl(units: list[TranscriptUnit], path: Path) -> None
def write_markdown(units: list[TranscriptUnit], path: Path) -> None
def load_units(path: Path) -> list[TranscriptUnit]
```

要点：

- 按标点与 `max_chars` 合并过短的 ASR 分段，减少单元数量、提升可读性
- **不修改文字内容**（不改错别字、不补标点以外的内容）
- 有平台字幕时，优先用字幕文本替换同时间段 ASR 文本，`source` 标记为 `subtitle`

### 6.7 `report/outline.py`

```python
def plan_outline(meta: VideoMeta, units: list[TranscriptUnit], *,
                 mode: str) -> Outline
```

要点：

- 一次 LLM 调用，**要求返回严格 JSON**，用 `response_format={"type": "json_object"}`
- 输入是**完整** `transcript.md`（不预压缩、不先摘要）
- JSON 解析失败：把报错信息回灌给模型重试，最多 2 次；仍失败则退出码 4
- 校验：`sections[].source_ids` 必须都是真实存在的 `id`，非法 id 直接剔除

### 6.8 `report/writer.py`

```python
def write_sections(outline: Outline, units: list[TranscriptUnit], *,
                   mode: str, out_dir: Path) -> list[str]
```

要点：

- **每节一次独立调用**，只喂该节 `source_ids` 对应的转写片段
- 每节输出写入 `sections/s1.html` 等，**单节失败可单独重试**，已成功的节不重跑
- 组装顺序：`sections` 数组顺序即为正文顺序
- 输出片段禁止出现 `<html>` / `<body>` / `<style>` / `<script>`，只允许模板已定义的组件类
- 节标题 `<h2>` 不由模型输出：`sections/sN.html` 只存 LLM 片段，标题在拼装时确定性注入（§8.4）
- 片段中偶发的 Markdown 强调标记（`**` / `__`）会被确定性去除，字面标记不进入报告
- 片段中模板未定义的 class 会被剔除并打印告警，允许清单由 `render.template_classes()` 提供（§6.10）
- 命中缓存的分节同样经过标题注入、标记去除与 class 剔除，因此仅重渲染即可修正这三类失真

### 6.9 `report/prompts.py`

所有 prompt 集中在此文件，便于迭代对比。详见第 7 章。

### 6.10 `render.py`

```python
def render_report(*, template: Path, ctx: dict[str, str], out: Path) -> None
def check_self_contained(html: str) -> list[str]   # 返回违规项列表
def template_classes(template: Path) -> set[str]   # 模板已定义的 class 名
```

要点：

- 纯 `str.replace` 替换占位符，**不做 HTML 转义**（LLM 输出本身就是 HTML 片段）
- `check_self_contained` 扫描并拒绝：`<link rel=stylesheet>`、`<script src=`、`http(s)://` 图片、外链字体
- `template_classes` 从模板自身提取 `<style>` 选择器与静态 `class` 属性，供 `report.writer` 剔除自创 class（§6.8），避免清单与模板两边手工维护而漂移
- 包含 `--open` 时用 `webbrowser.open(out.resolve().as_uri())` 打开

---

## 7. 报告生成规范（Prompt）

### 7.1 结构规划 Prompt（LLM #1）

**System**

```
你是一名内容编辑。你的任务是把一份视频转写稿规划成阅读报告的章节结构。

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
               "intent": str, "source_ids": [str]}]}
```


**User**

```
视频信息：{meta}

完整转写：
{transcript_md}
```

### 7.2 逐节写作 Prompt（LLM #2）

**System**

```
你是一名内容编辑。你会拿到一节报告所需的全部原始转写，把它写成可直接嵌入 HTML 的正文片段。

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
14. {length_rule}
```

**User**

```
本节标题：{heading}
本节意图：{intent}
本节时间范围：{time_range}

本节原始转写：
{section_units_md}

可用组件：
{component_list}
```

`{length_rule}` 按模式填充：

- Standard：`本节正文 300-700 字，视信息量而定；不要为了凑长度重复已说过的内容。`
- Brief：`本节正文不超过 350 字；宁可少讲一个次要例子，也不要超出。`

### 7.3 全局写作规则速查

| 规则 | 要求 | 反例 |
|---|---|---|
| 转述层剥离 | 直接给判断 | ❌ 作者认为，延迟主要来自 GC |
| 禁用句式 | 分别交代适用范围 | ❌ 关键不是缓存，而是预热 |
| 数字口径 | 保留单位 / 基准 / 约 | ❌ 提升了 30%（未说明基准） |
| 因果 | 不把先后当因果 | ❌ 会议之后，事故率下降 |
| 推广排除 | 推广内容不进正文 | ❌ 该产品能省 80% 时间 |
| 加粗 | 句内一处，每节最多一处 | ❌ 整句加粗 |
| 标题 | 不加无依据转折 / 对仗 | ❌ 慢即是快：重新理解缓存 |

### 7.4 Brief 模式的附加硬约束

- 正文总字数 **1600–2200 字，上限 2400**（写完实际提取 HTML 文本计数，不能凭估计声明达标）
- 单段 1–3 行、最多 4 行；相邻独立正文最多两段、合计不超过 6 行
- 章节编号必须保留（由模板 CSS 计数器自动生成）
- 正文**不显示时间戳**

---

## 8. 模板规范

### 8.1 占位符

两份模板共用 7 个占位符：

| 占位符 | 内容 | 必需 |
|---|---|---|
| `{{TITLE}}` | 主标题 | 是 |
| `{{SUBTITLE}}` | 副标题 | 否（空则整行删除） |
| `{{LEAD}}` | 导语，一段话 | 是 |
| `{{ATTRIBUTION}}` | 平台；UP主；《原片完整标题》；原视频 URL（URL 同时作可见文本与 `href`） | 是 |
| `{{VIDEO_DESCRIPTION}}` | 视频简介折叠块，可空 | 否 |
| `{{BODY}}` | 各节 HTML 片段拼接结果 | 是 |
| `{{SOURCES}}` | 页尾折叠的"来源与处理说明" | 是 |

### 8.2 Standard 模板组件清单

| 类名 | 用途 |
|---|---|
| `.callout` | 浅蓝解释框 |
| `.keyline` | 浅黄归纳框 |
| `.cards` / `.comparison` / `.categories` | 两列并列 |
| `.categories-detailed` | 单列分类，配 `data-tone` |
| `.steps` | 步骤序列（自动计数器） |
| `.stats` / `.tiles` | 关键数字块 |
| `.bars` | 柱状对比 |
| `.table-details` / `.table-compact` | 表格（明细 / 紧凑） |
| `.cycle-map` | SVG 循环示意图 |

`data-tone` 取值：默认（蓝青）/ `rust` / `ochre` / `plum` / `sage` / `rose`。

### 8.3 Brief 模板组件清单

| 类名 | 用途 |
|---|---|
| `.brief-branch-group` | 条件分支 |
| `.brief-change` | 前后变化 |
| `.brief-open-card` | 留白卡 |
| `.brief-case-card` | 参数算例 |
| `.brief-fold-card` | 折角便签 |
| `.brief-timeline` | 时间轴（`data-layout="alternating"` 变中轴） |
| `.brief-facts` / `.brief-pair` | 事实条 / 对照 |
| `.brief-dimension-group` | 维度对照（按子项数自动 1/2/3 列，纯 CSS） |
| `.brief-table` | 表格（`data-layout="labels"` 为固定四列） |

**组件契约的执行方式**：注入 prompt 的组件清单除类名外还给出结构骨架，凡有严格内部结构的组件
（如 `.bars` 的 `span.bar-track` > `span.bar-fill`、`.stats` 的 `span.stat-value` / `span.stat-label`、
`.steps` 的 `ol > li`）都在清单里写清；随后 `report.writer` 在清洗阶段**剔除模板未定义的 class**并打印告警
（模板已定义 class 的清单由 `render.template_classes()` 从模板自身提取，见 §6.10），
避免"写了组件却没生效"的静默失真。卡内小标题统一用 `<strong>`，不使用 `<h4>` 等未定义标题标签。

### 8.4 排版基线

| 项 | Standard | Brief |
|---|---|---|
| 画布 | 860px，圆角 8px，正文 17px | 900px，圆角 26px，正文 18px |
| 章节编号 | 手写 `<span class="num">` | CSS 计数器自动 |
| 时间戳 | `h2` 右侧 `.section-time`，12px / `#666666`，无底色无边框 | 正文不显示 |
| 导航 | `.report-nav` 固定左侧页外（含滚动高亮脚本） | 无 |
| 脚本 | 有（仅滚动高亮） | 无 |

节标题 `<h2>` **不由模型输出**，由程序按大纲在拼装时确定性注入：Standard 为
`<h2><span class="num">N</span>标题<span class="section-time">时间范围</span></h2>`，
Brief 为 `<h2>标题</h2>`（编号交给 CSS 计数器）。`sections/sN.html` 只存 LLM 片段，
标题在拼装时生成，因此重跑仅重渲染也能补齐标题，无需重新调用模型。

---

## 9. CLI 设计

```bash
# 最常用
videread "https://www.bilibili.com/video/BV1xx411c7mD"

# 指定模式与产物目录，生成后自动打开
videread "<url>" --mode brief --out ./runs --open

# 忽略缓存全部重跑
videread "<url>" --no-cache --keep-audio

# 只重跑报告生成（转写已在 runs/ 中，不会重复付费）
videread "<url>"
```

运行输出示例：

```
[1/7] 下载            ✓ meta.json  BV1xx411c7mD  时长 35:37
[2/7] 音频处理        ✓ 16kHz 单声道  (缓存命中，跳过)
[3/7] 转写            ✓ 412 段  (缓存命中，跳过)
[4/7] 规范化          ✓ 268 单元  u0001-u0268
[5/7] 结构规划        ✓ 6 节  profile=mechanism  1.8k tokens
[6/7] 逐节写作        ✓ 6/6 节  14.2k tokens
[7/7] 渲染            ✓ report.html  自包含检查通过
完成 → runs/BV1xx411c7mD-3f9a2c11/report.html
```

---

## 10. 配置与环境

`.env.example`：

```dotenv
# --- ASR ---
ASR_BACKEND=dashscope
DASHSCOPE_API_KEY=
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/api/v1
DASHSCOPE_MODEL=paraformer-v2

# --- 备用 ASR（可选）：把 ASR_BACKEND 改为 openai 时生效 ---
OPENAI_API_KEY=
OPENAI_BASE_URL=
OPENAI_ASR_MODEL=whisper-1

# --- LLM ---
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=
LLM_MODEL_PLAN=deepseek-chat
LLM_MODEL_WRITE=deepseek-chat
LLM_TIMEOUT=180

# --- 外部程序（可选，留空则自动探测）---
FFMPEG_BIN=
FFPROBE_BIN=

# --- 网络 ---
HTTP_PROXY=
HTTPS_PROXY=
```

配置分层：`.env` → 环境变量 → CLI 参数（后者覆盖前者）。

---

## 11. 错误处理、重试与断点续跑

### 11.1 错误分类

| 类别 | 典型原因 | 处理 |
|---|---|---|
| 下载失败 | 反爬、地区限制、付费视频、链接失效 | 立即失败，提示换链接；不做无意义重试 |
| 音频处理失败 | ffmpeg 缺失、文件损坏 | 立即失败，提示检查 ffmpeg |
| ASR 失败 | 密钥无效、余额不足、单段超时 | 指数退避重试 3 次；单段失败不阻塞其他段 |
| LLM 失败 | 超时、限流、JSON 非法 | 退避重试 3 次；JSON 非法时回灌报错重试 2 次 |
| 渲染失败 | 模板占位符缺失、磁盘满 | 立即失败，提示具体缺失的占位符 |

### 11.2 重试策略

```python
# 指数退避：1s → 2s → 4s，带 ±20% 抖动
def retry(fn, *, attempts=3, base=1.0, jitter=0.2): ...
```

**不重试**的两种情况：密钥 / 余额类错误（重试无意义）、参数校验失败。

### 11.3 断点续跑对照表

| 阶段 | 检查的产物 | 命中则 |
|---|---|---|
| 下载 | `meta.json` + `audio.m4a` | 跳过下载 |
| 音频处理 | `audio.wav` | 跳过转码 |
| 转写 | `asr.raw.jsonl` | 跳过 ASR（**省钱关键**） |
| 规范化 | `transcript.jsonl` | 跳过规范化 |
| 结构规划 | `outline.json` | 跳过 LLM #1 |
| 逐节写作 | `sections/s*.html` 全部存在 | 跳过 LLM #2；部分存在则只补缺失的节 |
| 渲染 | `report.html` | 跳过渲染 |

> 远端阶段 1 命中 `meta.json` 前，会先校验其中 `url` 与本次请求的规范 URL 是否一致；不一致（例如 run 目录被别的视频复用、或同名样本残留）则视为未命中，重新抓取元信息并落到新 run 目录，避免张冠李戴。

---

## 12. Windows 落地清单

- **一键启动**：`run.bat` 先自检 Python / 虚拟环境 / ffmpeg / `.env` 四项；首次运行自动建 `.venv`、`pip install -e .`、并把 ffmpeg 静态包下载解压到 `bin/`。无参数时默认启动本地 Web 控制台并自动打开浏览器（等价于 `run.bat web`），带参数时原样透传给 CLI
- **ffmpeg**：`winget install Gyan.FFmpeg`，或将静态包解压到项目 `bin/`，代码优先探测 `bin/ffmpeg.exe`
- **路径**：统一 `pathlib.Path`，禁止字符串拼接路径
- **编码**：所有文件读写显式 `encoding="utf-8"`；子进程输出捕获显式指定 `encoding="utf-8", errors="replace"`
- **控制台**：启动时执行 `sys.stdout.reconfigure(encoding="utf-8")`，避免中文乱码
- **超时**：ASR 单段 10 分钟、LLM 单次 180 秒、整条流水线 20 分钟
- **长路径**：`runs/` 目录尽量浅，必要时启用系统长路径支持
- **打开报告**：`webbrowser.open(out.resolve().as_uri())`
- **代理**：`httpx` 与 `yt-dlp` 都需读取 `HTTPS_PROXY`

---

## 13. 测试策略

### 13.1 单元测试（不打网络）

| 目标 | 用例 |
|---|---|
| `transcript.build_units` | 过短分段正确合并；`id` 连续不重复；`max_chars` 生效 |
| `transcript` 时间戳 | 切分后 `start` / `end` 单调不减 |
| `render.render_report` | 7 个占位符全部替换；缺失占位符报错 |
| `render.check_self_contained` | 能识别外链 CSS / 外链图片 / 外链字体 |
| `report.outline` | 非法 `source_ids` 被剔除；JSON 修复分支生效 |
| `pipeline` 缓存 | 产物存在时对应阶段被跳过 |

### 13.2 集成测试（打网络，标记 `@pytest.mark.network`）

- 下载一个 3 分钟以内的公开 B 站视频 → 断言拿到 `meta.json` 且 `duration > 0`
- 完整跑通一次 → 断言 `report.html` 存在且自包含检查通过

### 13.3 黄金样本（离线）

在 `tests/fixtures/` 放 **3 份固定转写稿**，覆盖三种内容类型（技术讲解 / 操作教程 / 事件叙事），每次改 prompt 后跑一遍，人工比对报告质量。

> 注意：自动化检查只能验证"程序没崩"与"结构合规"，**不能证明内容忠实**。忠实性最终依赖人工比对。

---

## 14. 实施路线

### P1 · 打通最小链路

**范围**：本地音频文件 → 云 ASR → 一次性生成 HTML → 渲染

| 任务 | 产出 |
|---|---|
| 建项目骨架 | `pyproject.toml`、`uv` 环境、`config.py` |
| 实现 ASR adapter | `asr/base.py` + `asr/dashscope.py` |
| 实现规范化 | `transcript.py`（含 `transcript.md` 输出） |
| 写一份**简化模板** | `templates/report.html`（先只支持 `.callout` / `.cards` / 表格） |
| 一次性生成（跳过 outline） | 先验证 prompt 效果，不做分节 |
| CLI 骨架 | `cli.py`：`videread ./local.m4a` |

**验收**：本地音频能出报告；结构不理想可接受。

### P2 · 接入 B 站与缓存

| 任务 | 产出 |
|---|---|
| 实现下载 | `download.py`（yt-dlp + 元信息 + 字幕） |
| 实现音频处理 | `audio.py`（16kHz 单声道 + 静音检测） |
| 长音频切段并行 | `asr/dashscope.py` 内实现 |
| 中间产物落盘与断点续跑 | `pipeline.py` + `trace.py` |

**验收**：传 URL 即出报告；第二次运行不重复转写（满足 A3）。

### P3 · 提升报告质量

| 任务 | 产出 |
|---|---|
| 拆成两段式 | `report/outline.py` + `report/writer.py` |
| 沉淀 prompt 规范 | `report/prompts.py`（第 7 章内容完整落地） |
| 来源绑定 | `data-source-units` 注入与校验 |
| 组件清单注入 | 把模板可用组件以文本形式喂给 LLM |

**验收**：报告结构清晰；不出现转述层与套路句式（满足 A4 / A5）。

### P4 · 打磨与扩展

| 任务 | 产出 |
|---|---|
| 打磨 Standard 模板 | 对齐 `data-tone`、`.cycle-map`、表格规范 |
| 自包含检查 | `render.check_self_contained` |
| 长图导出（可选） | 用 Playwright 截图长图 |
| Brief 模式 | `brief-report.html` + 字数 / 行数硬约束 |

**验收**：报告可直接分享；Brief 模式字数达标。

---

## 15. 成本估算

| 项 | 单价（参考） | 1 小时视频约 |
|---|---|---|
| 云 ASR | 约 ¥0.3–1.5 / 小时音频 | ¥0.5–1.5（**只付一次**） |
| LLM 结构规划 | 输入约 8k tokens | ¥0.01–0.05 |
| LLM 逐节写作 | 输入约 20k + 输出约 4k | ¥0.05–0.2 |
| **合计** | — | **约 ¥0.6–2** |

成本控制手段：

1. `asr.raw.jsonl` 落盘后永不重复转写（最大头）
2. 结构规划用便宜模型，逐节写作用强模型
3. 调试模板时依赖缓存，只花 LLM 的钱

---

## 16. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| B 站反爬 / 接口变更 | 下载失败 | 锁定 `yt-dlp` 版本区间并保留升级通道；失败给出明确提示而非静默重试 |
| 云 ASR 长音频限制 | 转写中断 | 按静音点切段 + 并行 + 单段落盘，失败只重跑单段 |
| LLM 输出非法 JSON | 规划失败 | `json_object` 模式 + 报错回灌重试 + 严格校验 `source_ids` |
| LLM 幻觉补充内容 | 报告不忠实 | prompt 强约束"只用给定转写"；保留 `data-source-units` 便于人工抽查 |
| 转写专有名词错误 | 报告出现错词 | 允许用户提供 `terms.txt` 词表，注入 prompt 作为"术语参考"（不作为事实来源） |
| 报告过长导致不可读 | 质量下降 | Standard 分节字数建议；Brief 硬上限 + 实测计数 |

---

## 17. 附录

### 17.1 参考项目

`imexlovery/video-report-agent` —— 本项目的主要参考来源，尤其是：

- `skills/video-report/SKILL.md`：报告写作规范（转述层剥离、禁用句式、推广排除、来源绑定、加粗克制）——**第 7 章的规则直接吸收自此**
- `modes/standard.md` / `modes/brief.md`：两种阅读模式的篇幅与表达约束
- `assets/report-template.html` / `assets/brief-report-template.html`：模板结构与组件设计
- `docs/DECISIONS.md`：设计取舍的推理过程

**不照搬的部分**：Pi RPC / Coding Agent 依赖、完整 Worker 进程组监督、单任务 30 分钟期限、两套模板并行支持。

### 17.2 术语表

| 术语 | 含义 |
|---|---|
| 转写单元（unit） | 规范化后的最小引用单位，带 `id` 与时间戳，如 `u0012` |
| Profile | 报告的信息结构类型：mechanism / procedure / evidence / argument / narrative |
| Standard / Brief | 两种阅读模式：自适应精读 / 字数硬预算速览 |
| 来源绑定 | 用 `data-source-units` 把报告中的判断关联回转写单元 |
| 自包含 | HTML 不依赖任何外部资源（CSS / JS / 图片 / 字体全部内联或内嵌） |
| 断点续跑 | 通过检查已落盘产物跳过已完成阶段，避免重复付费 |

---

## 18. 下一步

建议按 P1 开始：先搭骨架跑通「本地音频 → HTML 报告」，验证 prompt 效果后再逐步接入下载、缓存与分节生成。

**P1 的最小任务是**：

1. 建 `pyproject.toml`（依赖：`yt-dlp`、`httpx`、`openai`、`python-dotenv`、`pytest`）
2. 写 `asr/base.py` + `asr/dashscope.py`
3. 写 `transcript.py`
4. 写一份简化 `templates/report.html`
5. 写 `cli.py` + 一次性生成的 prompt