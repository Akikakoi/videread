# videread

把 B 站视频 / 本地音视频的语音转写成结构化**自包含 HTML 阅读报告**：一条命令跑完下载、转写、结构规划、逐节写作与渲染，产出单个可以直接丢进浏览器的 `report.html`（无外链 CSS / 图片 / 字体）。

- **7 阶段流水线**：下载 → 音频处理 → 转写 → 规范化 → 结构规划 → 逐节写作 → 渲染
- **断点续跑**：每个阶段的产物落盘即为缓存判据，中断后重跑不重复付费
- **可追溯**：正文段落通过 `data-source-units` 回指转写单元 id
- **可观测**：每次运行写出 `run.trace.jsonl`，逐阶段记录耗时与 token 用量

完整的接口契约、数据 schema 与验收标准见 [videread-开发文档.md](videread-开发文档.md)。

---

## 快速开始

### 一键启动（Windows）

```
run.bat
```

双击或在命令行执行即可。脚本会依次自检四项：

| 步骤 | 行为 |
|---|---|
| Python | 依次尝试 `py -3` / `python`，缺失则提示安装 Python 3.11+ |
| 虚拟环境 | 无 `.venv` 时创建并 `pip install -e .`，已存在则跳过 |
| ffmpeg | 优先项目 `bin\`，其次系统 PATH；都没有则从 npmmirror 下载静态包解压到 `bin\` |
| 配置 | 无 `.env` 时从 `.env.example` 复制并提示填写密钥 |

无参数时默认启动本地 Web 控制台并自动打开浏览器（见下方「Web 控制台」）；带参数时原样透传给 CLI：

```
run.bat "https://www.bilibili.com/video/BV1xx411c7mD" --mode brief --open
```

### 手动安装

```powershell
py -3 -m venv .venv
.venv\Scripts\activate
pip install -e .
```

`src` 布局，未安装时也可用 `$env:PYTHONPATH="src"` 直接运行。

---

## 配置

复制 `.env.example` 为 `.env`（该文件不入库），按需填写：

| 变量 | 必填 | 说明 |
|---|---|---|
| `ASR_BACKEND` | 是 | `dashscope` / `dashscope-realtime` / `openai` |
| `DASHSCOPE_API_KEY` | 用 `dashscope*` 时 | 阿里云百炼（Model Studio）API Key |
| `DASHSCOPE_BASE_URL` | 否 | 默认 `https://dashscope.aliyuncs.com/api/v1`；私有化 MaaS 部署可覆盖 |
| `DASHSCOPE_MODEL` | 否 | 默认 `paraformer-v2`；实时后端未含 `realtime` 关键字时自动改用 `paraformer-realtime-v2` |
| `LLM_BASE_URL` / `LLM_API_KEY` | 是 | 任意 OpenAI 兼容网关 |
| `LLM_MODEL_PLAN` / `LLM_MODEL_WRITE` | 否 | 结构规划与逐节写作所用的模型名 |
| `LLM_WRITE_CONCURRENCY` | 否 | 逐节写作并发路数，默认 3；各节互相独立，加大可缩短总耗时，注意接口限流 |
| `LLM_TIMEOUT` | 否 | 单次 LLM 调用超时秒数，默认 180 |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_ASR_MODEL` | 用 `openai` 后端时 | 未配置时回退复用 `LLM_*` |
| `FFMPEG_BIN` / `FFPROBE_BIN` | 否 | 留空则按 `bin/` → PATH 顺序探测 |
| `HTTP_PROXY` / `HTTPS_PROXY` | 否 | 同时作用于 `httpx` 与 `yt-dlp` |

优先级：`.env` → 环境变量 → CLI 参数（后者覆盖前者）。

---

## 用法

```powershell
python -m videread.cli BV1xx411c7mD
```

> 注意入口是 `videread.cli`，项目没有 `__main__.py`，`python -m videread` 会报 `No module named videread.__main__`。
> 安装后也可直接使用 `videread` 命令（`pyproject.toml` 中的 console script）。

| 参数 | 默认 | 说明 |
|---|---|---|
| `url` | — | Bilibili 视频链接或 BV 号（如 `BV1xx411c7mD`），或本地音视频文件路径 |
| `--mode` | `standard` | `standard` / `brief`，对应两套模板 |
| `--out` | 项目根 `runs/` | 产物根目录 |
| `--no-cache` | 关 | 忽略已有产物，全部重跑 |
| `--keep-audio` | 关 | 保留中间产物 `audio.m4a` |
| `--open` | 关 | 生成后用浏览器打开报告 |
| `--asr` | 取自 `.env` | 临时覆盖 ASR 后端 |
| `--version` | — | 打印版本 |

### 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 参数 / 用法错误（含缺少密钥） |
| 2 | 下载失败（反爬、地区限制、链接失效） |
| 3 | 音频处理或 ASR 失败 |
| 4 | LLM 调用失败（超时、限流、JSON 非法且重试耗尽） |
| 5 | 渲染或写盘失败 |

---

## Web 控制台

不想敲命令时用本地控制台：浏览器里填链接、看 7 阶段实时进度、完成后就地预览报告，并浏览 `runs/` 里已生成的全部报告。

```powershell
run.bat                          # Windows 一键启动（无参数即起控制台并开浏览器）
run.bat web                      # 同上，显式写法
python -m videread.web --open    # 等价的手动方式
videread-web                     # 安装后的控制台命令
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--host` | `127.0.0.1` | 只绑本机；服务无鉴权，请勿改为对外地址 |
| `--port` | `8765` | 监听端口 |
| `--out` | 项目根 `runs/` | 产物根目录，与 CLI 的 `--out` 是同一个 |
| `--open` | 关 | 启动后用浏览器打开控制台 |

页面分三块：左侧提交任务，下面是 7 阶段步进器与实时日志；右侧是报告库（标题 / UP主 / 时长 / profile / 节数 / 生成时间），点「阶段耗时」展开该次运行各阶段耗时与 token；报告可就地浮层预览或新窗口打开。

约定与限制：

- **同一时刻只跑一个任务**：上一个任务结束前再次提交返回 409。串行既是个人工具的取舍，也避免争用同一 run 目录、重复付费。
- **不支持中断**：流水线没有取消钩子，关掉页面不会停止任务，退出服务进程才会终止。
- **改 `.env` 后需重启控制台**：`.env` 以 `override=False` 加载，已在进程里生效的键改值不会立刻刷新（新增的键可以）。

---

## 产物

输出到 `<out>/<视频标识>-<路径哈希>/`：

| 文件 | 说明 | 可否复用 |
|---|---|---|
| `meta.json` | 标题 / UP主 / 时长 / URL / 封面 | 是 |
| `audio.m4a` | 原始音频（`--keep-audio` 时保留） | 是 |
| `audio.wav` | 16kHz 单声道，ASR 输入 | 可删 |
| `asr.raw.jsonl` | ASR 原始输出，带时间戳（实时后端为 `asr.part.NNN.jsonl`） | **是（最贵，务必保留）** |
| `transcript.jsonl` | 规范化转写单元，带 `id` | 是 |
| `transcript.md` | 同上，人 / 模型可读 | 是 |
| `outline.json` | 结构规划结果 | 是 |
| `sections/sN.html` | 逐节正文片段 | 是 |
| `report.html` | 最终自包含报告 | 是（存在即跳过渲染） |
| `run.trace.jsonl` | 逐阶段耗时 / token 埋点 | — |

阶段 1 命中 `meta.json` 前会先校验其中的 `url` 与本次请求的规范 URL 是否一致，不一致即视为未命中，避免 run 目录被别的视频复用。

---

## ASR 后端

| 后端 | 通道 | 适用场景 |
|---|---|---|
| `dashscope` | 异步：`getPolicy` → OSS 直传 → 提交任务 → 轮询 | 账号对 `dashscope-instant` 即时桶有授权时，可并行切段 |
| `dashscope-realtime` | 实时：WebSocket 二进制帧直推 | 不经 OSS 桶，**不受对象归属校验限制**；长音频按静音点切段后顺序提交 |
| `openai` | OpenAI 兼容 `audio/transcriptions` | 自备 whisper 类服务 |

若异步后端报 `403 Resource.AccessDenied`（提示 `OSS Resource oss://dashscope-instant/... access denied`），属账号对即时桶无读取授权，换 Key 不解决，改用 `dashscope-realtime` 即可。

---

## 目录结构

```
videread/
├─ run.bat                    # 一键启动（无参数起控制台；带参数透传 CLI）
├─ .env / .env.example        # 密钥 / 模板
├─ bin/                       # ffmpeg.exe / ffprobe.exe（可选）
├─ src/videread/
│  ├─ cli.py                  # 参数解析、进度输出、退出码
│  ├─ pipeline.py             # 7 阶段编排、断点续跑
│  ├─ config.py               # 配置分层与外部程序探测
│  ├─ download.py audio.py transcript.py
│  ├─ execution.py failures.py  # 外部命令统一执行 / 退出码归类文案
│  ├─ asr/                    # dashscope / realtime / openai_whisper
│  ├─ report/                 # outline / writer / prompts
│  ├─ render.py trace.py      # 模板填充与埋点
│  ├─ web/                    # 本地控制台（FastAPI + 原生前端）
│  └─ templates/              # standard / brief 两套模板
├─ runs/                      # 运行产物（不入库）
└─ tests/                     # 离线用例
```

---

## 测试

```powershell
pytest -q
```

45 条用例全部离线，不联网、不调用 ffmpeg 与 LLM。`pyproject.toml` 预留了 `network` marker，并默认 `-m 'not network'`，后续加入联网集成测试时会自动被排除。

---

## 常见问题

**提示找不到 ffmpeg** — 把 `ffmpeg.exe` / `ffprobe.exe` 放进项目 `bin\`，或 `winget install Gyan.FFmpeg`，或在 `.env` 里用 `FFMPEG_BIN` / `FFPROBE_BIN` 指定绝对路径。

**控制台中文乱码** — CLI 已在启动时把 stdout/stderr 重配为 UTF-8；用 `run.bat` 时脚本会先执行 `chcp 65001`。

**转写结果为空** — 该音频区间确实没检测到人声（例如音乐视频的长片头），不是接口故障。

**想换一个视频重跑** — 直接换 URL 即可，会落到新的 run 目录；同一视频想强制刷新则加 `--no-cache`。