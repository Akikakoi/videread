# videread 设计规范（design.md）

> 本文档总结项目现有的「编辑部 / 印刷间」设计体系，来源：
> `src/videread/web/static/editorial/css/base.css`（主题令牌）、`index.css`、`job.css`、`reader.css`。
>
> **工作规则：每做一个新页面，先读本文件，严格按以下令牌与规则实现，保证全站风格统一。**
> 新页面必须复用 `:root` 主题令牌（`var(--paper)` 等），禁止硬编码色值。

设计定位：报纸 / 杂志印刷风（editorial print style）。关键词：纸面暖白底、墨色文字、朱红印章色点缀、衬线大标题、等宽字标签、细线（hairline）分隔、方角控件、双细线（3px double）报头规线。克制、无大圆角、无重阴影、无彩色渐变。

---

## 1. 配色

全部通过 CSS 变量使用，禁止直接写死。

### 1.1 浅色主题（默认，`:root`）

| 令牌 | 色值 | 用途 |
|---|---|---|
| `--paper` | `#FAF7F1` | 页面背景（主纸面） |
| `--paper-deep` | `#F3EEE3` | 次级背景：日志框、折叠块、输入槽、hover 行 |
| `--ink` | `#17140E` | 主文字 / 图标 / 选中态墨底 |
| `--lead` | `#2E2A20` | 导语文字 |
| `--body` | `#26221A` | 正文文字 |
| `--soft` | `#3A352A` | 次级正文（如问答答案） |
| `--muted` | `#6E675A` | 辅助说明文字、meta 信息 |
| `--faint` | `#A39B8A` | 最弱文字：编号、占位符、禁用态 |
| `--hairline` | `#D9D2C2` | 细分隔线、边框（1px hairline） |
| `--accent` | `#A6331E` | 强调红（印章色）：链接、hover、眉题、进度、错误 |
| `--ok` | `#2E6B4F` | 成功 / 完成态（墨绿） |

### 1.2 深色主题（`html[data-theme="dark"]`，整体翻转配方）

| 令牌 | 色值 |
|---|---|
| `--paper` | `#14120E` |
| `--paper-deep` | `#1C1914` |
| `--ink` | `#EDE6D6` |
| `--lead` | `#E2D9C4` |
| `--body` | `#DCD3BE` |
| `--soft` | `#B8AF9C` |
| `--muted` | `#A89F8C` |
| `--faint` | `#6E675A` |
| `--hairline` | `#3A352B` |
| `--accent` | `#D0563C`（提亮保对比） |
| `--ok` | `#6FB98C` |

### 1.3 用色规则

- 强调红是唯一的「品牌色」，只用于：链接与 hover、眉题 kicker、进度线 / 游标、错误与警告、印章元素。**禁止大面积铺红**。
- 选中态两种语言：墨底反白（`background:var(--ink); color:var(--paper)`，如模式胶囊）或 变红（文字 / 边框转 accent）。
- 半透明遮罩用墨色：`rgba(23,20,14,.42)`。
- 警示黄仅离线横幅用：`#E8B44A`；深色错误提示 `#B23A2F`。

---

## 2. 字体

### 2.1 字体族（三轨制）

| 令牌 | 字体栈 | 用途 |
|---|---|---|
| `--serif` | `"Fraunces","Noto Serif SC","Source Han Serif SC","Songti SC","STZhongsong","SimSun",serif` | 标题、正文强调、品牌字 |
| `--sans` | `"PingFang SC","Microsoft YaHei","Noto Sans SC",sans-serif` | 正文、界面文字 |
| `--mono` | `"IBM Plex Mono",ui-monospace,"SFMono-Regular",Consolas,monospace` | 标签、眉题、编号、meta、按钮文字、日志 |

规则：**中文正文用 sans，标题用 serif，一切「机器感 / 档案感」文字（眉题、标签、编号、时长、meta）一律 mono + 字距（letter-spacing 1–4px）**。

### 2.2 标题阶梯

| 层级 | 字号 | 字重 | 行高 | 备注 |
|---|---|---|---|---|
| 页面主标题 h1（主页 hero） | `clamp(40px, 7vw, 80px)` | 600 | 1.14 | 强调词用 italic + accent，字重 500 |
| 工单页 h1 | 40px（≤860px 为 30px） | 600 | 1.15 | |
| 阅读页 h1 | `clamp(30px, 5vw, 42px)` | 600 | 1.3 | |
| 区块 / 文章 h2 | 26–30px | 500–600 | 1.4–1.45 | 阅读页 h2 上方压 `border-top:2px solid var(--ink)` |
| 阅读页 h3 | 18px | 500 | — | 上边距 30px |
| 列表条目标题（目录 / 章节 / 来信） | 16–19px | 500 | — | serif |
| 品牌字 wordmark | 22–26px | 600 | — | 斜体强调词用 accent |

### 2.3 正文与辅助文字

| 层级 | 字号 | 行高 | 颜色 |
|---|---|---|---|
| 正文基准（body） | 16px | 1.75 | `--ink` |
| 阅读页段落 | 16px，两端对齐 | 1.95 | `--body` |
| 导语 lead | 16.5px | 1.9–1.95 | `--lead` |
| hero 副标题 deck | 18px | 1.9 | `--muted` |
| 说明 / 次级文字 | 13.5–15px | 1.7–1.8 | `--muted` / `--soft` |
| mono 标签 / meta | 10–13px | — | `--muted` / `--faint`，letter-spacing 1–4px |
| mono 日志 | 11.5px | 1.9 | `--muted` |

---

## 3. 间距

### 3.1 版面容器

- 主容器 `.wrap`：`max-width:1080px; margin:0 auto; padding:0 32px`（≤860px 收窄为 20px）。
- 阅读页正文栏：`max-width:760px; padding:56px 32px 120px`。
- 工单页容器：`max-width:1016px`。

### 3.2 常用间距档位

| 场景 | 值 |
|---|---|
| 组件内小间距（图标与文字、行内 gap） | 4–10px |
| 列表行内边距（上下） | 9–16px（紧凑列表 9–11px，目录行 16–20px） |
| 卡片 / 面板内边距 | 14–18px（弹窗 26–28px） |
| 模块上下留白（section.block） | 56px 顶部；特稿 spread 44px |
| hero 区 | 上 64px / 下 48px |
| 标题与内容的间隔 | 14–32px（大标题下 24–32px） |
| 章节导航 hover 位移 | translateX(10px) |
| 页脚与正文间隔 | margin-top 120px（≤860px 为 80px），页脚内 padding 56px 0 44px |
| 悬浮组件（toast / 下拉）距边 | 24px（toast），下拉距触发件 6–8px |
| 弹窗遮罩内边距 | 24px |

### 3.3 间距规则

- 纵向节奏靠「区块 + 顶部压线」组织：新模块先用 `border-top: 2px solid var(--ink)` 或 hairline 分隔，再配 16–56px 间距，不依赖大色块。
- 相关元素 gap 8–16px，无关联模块间 40px+。

---

## 4. 组件样式

### 4.1 圆角总规则

**默认方角（border-radius:0）。** 仅两档例外：
- 小圆角 `2px`：主按钮、分页按钮、模式胶囊、印章、开关方块。
- 全圆 `99px`：仅主题开关轨道、滑块小圆点等「开关类」小件；旧式胶囊标签已弃用。

### 4.2 按钮

| 类型 | 样式 |
|---|---|
| 主按钮（如 `#btn-submit`、`.dlg-btn.solid`） | 透明底 + `1.5px solid var(--accent)` 边框 + accent 文字，mono 13px / letter-spacing 2px，padding 约 `12px 24px`，radius 2px；hover 反色：accent 底 + paper 字 |
| 次按钮（如 `#btn-fav`、`.pager button`） | 透明底 + `1px solid var(--hairline)` + `--ink` 文字，mono 12px；hover 边框与文字转 accent（或 `--muted`→`--ink`）；disabled `--faint` |
| 选中态按钮（`.mode.on`） | 墨底反白：`background:var(--ink); color:var(--paper)` |
| 文字链接按钮（`.act`、ghost） | 无边框无底色，`--ink` 文字 + `1.5px` 下边线（或 mono 小字）；hover 文字与线转 accent，可上浮 `translateY(-3px)` |
| 通用交互 | 所有按钮 `cursor:pointer`，过渡 `var(--t)`，禁用态 `--faint` + `cursor:default` |

### 4.3 卡片 / 面板 / 弹窗

| 组件 | 规则 |
|---|---|
| 卡片（阅读页 `.cards > *`） | `1px solid var(--hairline)` 边框，无圆角，透明底，padding 16px；grid 3 列 gap 14px（≤700px 两列，≤520px 单列） |
| 面板（日志框 / 折叠块 / callout） | `--paper-deep` 底 + `1px solid var(--hairline)`；callout / 校样命令额外 `border-left:3px solid var(--accent)` 或 `2px` 左红线 |
| 弹窗 `.dialog` | `--paper` 底，`1px solid var(--hairline)`，顶部 `3px double var(--ink)` 规线，padding `26px 28px 24px`，阴影 `0 18px 50px rgba(23,20,14,.18)` |
| 浮层（下拉 / 历史面板） | `--paper` 底，`1px solid var(--hairline)` + `border-top:2px solid var(--ink)`，阴影 `0 14px 34px rgba(23,20,14,.12)`，条目间 hairline 分隔，hover 行 `--paper-deep` |
| toast | 墨底反白（`--ink` 底 `--paper` 字），mono 12.5px，左侧 `3px solid var(--accent)`，固定右下 24px |

### 4.4 导航栏 / 报头

- 报头 `.masthead`：serif 600 品牌字（强调词 italic + accent）+ mono 卷号 + 导航链接，**底部压 `3px double var(--ink)` 双细线**（这是全站报头 / 页脚的签名规线）。
- 导航链接 13px；主页版 hover 时底部 1.5px 红线从左向右展开（`background-size` 动画）；工单版 hover 变红即可。
- 阅读页顶栏 `.bar`：sticky 置顶，`--paper` 底 + 底部 1px hairline，返回钮 mono 13px。
- 悬浮章节导航（side-nav / report-nav）：mono 11px + 圆点 / **右侧** 2px 指示线，激活态 accent。（2026-10-27 由左侧移到右侧：导轨与指示线镜像为 border-right，贴着正文一侧）
- 阅读进度条：固定顶部，高 2px，accent 底。

### 4.5 输入控件

- 输入框默认**无框无线**：透明底，仅 `border-bottom:1.5px solid var(--hairline)`，focus 转墨色；提交槽（`.subline`）为 `--paper-deep` 底 + `2px solid var(--ink)`，focus-within 转 accent。
- 下拉 / 设置表单输入：`1px solid var(--hairline)`，radius 0，focus 转 accent。
- 复选开关：13px 方块 + 1px 边框，选中态填 accent（印章式），radius 2px。

### 4.6 分隔线体系

| 线型 | 用途 |
|---|---|
| `3px double var(--ink)` | 报头、页脚、弹窗顶（最重的规线） |
| `2px solid var(--ink)` | 区块标题底线 / 步进器顶线 / h2 顶线 |
| `1px solid var(--hairline)` | 列表行分隔、卡片边框（最常用） |
| `1px dotted var(--hairline)` | 目录引导点线 |
| `1px solid var(--ink)` | 强引用 blockquote 上下线、keyline |

### 4.7 动效令牌

- 缓动：`--ease: cubic-bezier(.22,.61,.36,1)`。
- 时长：`--t-fast:.18s`（小件）/ `--t:.28s`（常规）/ `--t-slow:.8s`（reveal 入场）。
- 入场动画统一为「淡入 + 轻位移」：opacity 0→1 配 translateY(8–24px)。
- **必须遵守 `prefers-reduced-motion: reduce`**：所有动画与过渡提供降级（直接呈现终态）。
- 主题切换时加 `html.theme-switching` 屏蔽过渡，避免闪变。

### 4.8 其他签名细节

- 选区（::selection）：accent 底 + paper 字。
- 页面滚动条：轨道为打孔圆点（装订边），滑块 accent 红，hover 墨色；面板内细滚条 8px，thumb 用 hairline/muted。
- 状态色语义：运行中 = accent 红，完成 = `--ok` 绿，等待/取消 = `--faint`，错误 = accent 红 + mono 标注。

---

## 附录：报告模板独立色板

`src/videread/templates/_shared/base.css`（report.html 等三个导出模板）使用冷灰蓝体系，仅限报告导出页：
`--paper:#fff`、`--page:#f2f4f6`、`--ink:#23292e`、`--ink-soft:#4b555d`、`--ink-mute:#7a848c`、`--line:#e3e7ea`、`--accent:#0f6f8c`、`--accent-soft:#eef6f8`。
**新建站点页面不要使用该色板**，一律走 editorial 主题令牌。
