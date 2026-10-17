# 003 — 报告卡片悬停抬升加触屏门控

- **Status**: DONE
- **Commit**: 72a4272
- **Severity**: MEDIUM
- **Category**: 6. Accessibility（未门控的 hover 位移）
- **Estimated scope**: 1 文件（styles.css），约 6 行改动（包裹现有 hover 规则）

## Problem

报告卡片 `.run-card:hover` 会抬升 1px 并加深阴影。这个 hover 没有
`@media (hover: hover) and (pointer: fine)` 门控——在触屏设备上，点按任意卡片
（如点「预览」按钮）会先触发 hover，卡片跳一下，手指抬起后又落回去，产生误触发的
视觉抖动。AUDIT.md 第 6 节明确把「ungated `:hover` motion」列为审计目标。

当前代码（`src/videread/web/static/styles.css:641` 起，逐字摘录）：

```css
/* src/videread/web/static/styles.css:641 — current */
.run-card {
  padding: 13px 15px;
  border: 1px solid var(--glass-border);
  border-radius: var(--radius);
  /* 卡片叠在同样半透明的面板上，两层白会叠成实心，故这里用更透的一层
     （--glass-3），并加大模糊与饱和度，让背后的花瓣真正透进来 */
  background: var(--glass-3);
  box-shadow: var(--sheen), var(--shadow-soft);
  -webkit-backdrop-filter: blur(var(--glass-blur)) saturate(160%);
  backdrop-filter: blur(var(--glass-blur)) saturate(160%);
  transition: border-color .2s ease, box-shadow .2s ease, transform .2s ease;
}
.run-card:hover {
  border-color: var(--accent-line);
  box-shadow: var(--sheen), var(--shadow-raised);
  transform: translateY(-1px);
}
```

## Target

只在真正有精细指针（鼠标/触控板）的设备上启用 hover 位移；触屏设备上卡片
完全静止。`transition` 声明留在 `.run-card` 本体上不动（没有 hover 就没有属性变化，
transition 不会无端运行）。

```css
/* target —— 把 .run-card:hover 整块包进媒体查询，规则内容不变 */
@media (hover: hover) and (pointer: fine) {
  .run-card:hover {
    border-color: var(--accent-line);
    box-shadow: var(--sheen), var(--shadow-raised);
    transform: translateY(-1px);
  }
}
```

reduced-motion 无需额外处理：`styles.css:967` 已有
`.btn, .run-card, .stepper li { transition: none; }`，hover 变色仍然瞬时生效、
只丢过渡（符合 AUDIT.md 第 6 节「丢位移、保反馈」的方向）。

## Repo conventions to follow

- 本仓库已有媒体查询组织先例：`styles.css:107`（`max-width: 1080px` 断点）、
  `styles.css:491` / `563` / `967`（`prefers-reduced-motion`）——媒体查询紧贴
  被它修饰的规则放置，不集中到文件末尾。故 `@media (hover: hover)` 就包在
  `.run-card:hover` 原位置。
- 注释用中文、说明「为什么」，与文件现有注释风格一致。

## Steps

1. 在 `src/videread/web/static/styles.css` 中定位 `.run-card:hover` 规则
   （第 653–657 行）。
2. 用以下内容整体替换该规则（三条声明原样保留，仅加媒体查询外壳与注释）：
   ```css
   /* 触屏点按会误触发 hover，抬升位移只在有精细指针的设备上启用 */
   @media (hover: hover) and (pointer: fine) {
     .run-card:hover {
       border-color: var(--accent-line);
       box-shadow: var(--sheen), var(--shadow-raised);
       transform: translateY(-1px);
     }
   }
   ```
3. 确认 `.run-card` 基础规则（第 641 行）与其 `transition` 声明未被改动。

## Boundaries

- 只改 `src/videread/web/static/styles.css` 中 `.run-card:hover` 这一处。不碰
  `.btn:hover`、`.run-card` 基础规则、`app.js`、`index.html`。
- 不改 hover 的视觉效果（颜色/阴影/位移值原样保留）——只加媒体查询门控。
- 不加依赖、不改任何时长或曲线。
- 若代码与 commit 72a4272 漂移（`.run-card:hover` 已不存在或内容不同），停下并报告。

## Verification

- **Mechanical**: 无构建步骤；浏览器加载控制台页面，DevTools Console 无 CSS 解析错误。
- **Feel check**: 运行 UI，确认：
  - 桌面浏览器悬停报告卡片 → 仍正常抬升 1px + 阴影加深，手感与改前一致。
  - DevTools 设备模拟选一个触屏设备（如 iPad），开启触摸模拟后点按卡片 →
    卡片不抬升、不抖动（媒体查询在模拟器中生效，hover/pointer 查询结果可在
    Rendering 面板的 Media query 状态里核对）。
  - Rendering 面板切换 `prefers-reduced-motion: reduce` → 悬停仍变色（反馈保留），
    只是无过渡动画。
- **Done when**: 桌面 hover 行为不变，触屏模拟下点按卡片零位移，Console 无 CSS 错误。
