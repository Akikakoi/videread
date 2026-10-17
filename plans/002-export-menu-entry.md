# 002 — 导出菜单从触发按钮位置展开

- **Status**: DONE
- **Commit**: 72a4272
- **Severity**: MEDIUM
- **Category**: 8. Missed opportunities（空间相连的 UI 无来源动效）
- **Estimated scope**: 1 文件（styles.css），约 12 行

## Problem

报告卡片上的「导出 ▾」按钮点击后浮出格式菜单（PDF / PNG / Markdown），菜单在空间上
与按钮直接相连，却瞬间出现——用户看不出它「从按钮那里来」。同一仓库里的原生
`<select>` 弹出层（`styles.css:205`）反而有 `translateY(-6px) scale(.97)` 的入场，
两套下拉手感不一致。

当前代码（`src/videread/web/static/styles.css:701` 起，逐字摘录）：

```css
/* src/videread/web/static/styles.css:701 — current */
.export-menu {
  position: fixed;
  z-index: 50;
  min-width: 132px;
  padding: 6px;
  border-radius: 12px;
  /* 浮在小字上方的紧凑浮层：比大面板（.72）更实的一层，模糊后的
     残影只剩淡淡的色块，菜单文字不被底下卡片内容干扰 */
  background: rgba(255, 255, 255, .92);
  border: 1px solid var(--glass-border);
  box-shadow: var(--sheen), var(--shadow-raised);
  -webkit-backdrop-filter: blur(var(--glass-blur)) saturate(150%);
  backdrop-filter: blur(var(--glass-blur)) saturate(150%);
}
```

菜单的显示由 `app.js:1052–1065` 控制：打开时挂到 `body`、按触发按钮位置设
`top/left`，再 `menu.hidden = false`；关闭时 `closeExportMenus()` 直接
`hidden = true`。配合 `styles.css:7` 的 `[hidden] { display: none !important; }`，
显示瞬间正是 `@starting-style` 的触发时机。

## Target

入场：`translateY(-4px) scale(0.97) → 1` + 淡入，150ms，强 ease-out；
变换原点在菜单左上角（菜单从触发按钮左边缘向下展开）。
出场保持瞬时（下拉菜单关闭属于高频动作，越快越好）。

```css
/* target —— .export-menu 追加三个声明（其余现有声明全部不动） */
.export-menu {
  /* …现有声明保持不动… */
  transform-origin: top left;
  transition: transform 150ms var(--ease-out), opacity 150ms var(--ease-out);
}
@starting-style {
  .export-menu { opacity: 0; transform: translateY(-4px) scale(0.97); }
}
```

```css
/* target —— 并入 styles.css:967 已有的 reduced-motion 块 */
@media (prefers-reduced-motion: reduce) {
  .btn, .run-card, .stepper li { transition: none; }
  .export-menu { transition: none; }
}
```

数值依据（AUDIT.md 第 2 节）：dropdowns/selects 时长预算 150–250ms，取 150ms
（下拉要快）；入场 ease-out；`--ease-out: cubic-bezier(0.23, 1, 0.32, 1)` 逐字取自
AUDIT.md。scale 用 0.97（AUDIT.md 第 3 节：不用 scale(0)，入场 scale(0.9–0.97)）。
`transform-origin` 按第 3 节「popovers/dropdowns 从触发器缩放」——菜单左上角即
触发按钮左下角对齐点，故 `top left`。

## Repo conventions to follow

- **依赖**：本计划使用 token `--ease-out: cubic-bezier(0.23, 1, 0.32, 1);`，
  由计划 001 添加进 `styles.css` 的 `:root`（第 9–39 行）。若执行时 `:root` 里
  还没有 `--ease-out`（001 未执行），先按 001 的步骤 1 添加这一行，再继续。
- 本仓库已有同款入场模式可照抄：`styles.css:201–210` 原生下拉的
  `transition: opacity .16s ease, transform .16s ease;` +
  `@starting-style { …opacity: 0; transform: translateY(-6px) scale(.97); }`，
  注释明确写了「展开动画只写在 @starting-style，关闭态不设 opacity」——遵循同一模式。
- 注释用中文、说明「为什么」，与文件现有注释风格一致。

## Steps

1. 确认 `src/videread/web/static/styles.css` 的 `:root` 内存在
   `--ease-out: cubic-bezier(0.23, 1, 0.32, 1);`；不存在则在 `--shadow-inset`
   行之后添加（与计划 001 步骤 1 相同）。
2. 给 `.export-menu` 规则（第 701 行）追加两行：
   ```css
   transform-origin: top left;
   transition: transform 150ms var(--ease-out), opacity 150ms var(--ease-out);
   ```
3. 在 `.export-menu` 规则之后（`.export-item` 之前）新增：
   ```css
   /* 展开动画只写 @starting-style：关闭态不设初值，菜单不会因动画失效而不可见 */
   @starting-style {
     .export-menu { opacity: 0; transform: translateY(-4px) scale(0.97); }
   }
   ```
4. 修改第 967 行的 `@media (prefers-reduced-motion: reduce)` 块：
   ```css
   @media (prefers-reduced-motion: reduce) {
     .btn, .run-card, .stepper li { transition: none; }
     .export-menu { transition: none; }
   }
   ```
   （若计划 001 已执行、块内已有 `.modal, .modal-box, .preview, .preview-box` 行，
   保留该行并再加 `.export-menu` 行。）

## Boundaries

- 只改 `src/videread/web/static/styles.css`。不碰 `app.js`（菜单挂载/定位逻辑）、
  `index.html`。
- 不改菜单的定位、背景、阴影——只新增 transition、`@starting-style`、
  `transform-origin` 和 reduced-motion 条目。
- 不做出场动画：`closeExportMenus()` 的立即 `hidden = true` 保持原样。
- 不加依赖、不加 `@keyframes`。
- 若代码与 commit 72a4272 漂移（`.export-menu` 结构变化、`hidden` 机制被改等），
  停下并报告。

## Verification

- **Mechanical**: 无构建步骤；浏览器加载控制台页面，DevTools Console 无 CSS 解析错误。
- **Feel check**: 运行 UI，触发以下交互，确认：
  - 点某条有报告记录的「导出 ▾」→ 菜单从按钮位置向下轻浮出现（-4px 上落 + 微放大 + 淡入），
    150ms 内到位，不是凭空闪现。
  - 菜单打开状态下点页面别处 / 滚动 → 菜单瞬时收起（预期，无出场动画）。
  - 连点「导出 ▾」开-关-开 → 每次打开都完整播放入场，不出现「卡在半透明」的中间态
    （`@starting-style` 模式的正确性检查）。
  - DevTools Animations 面板 10% 回放：确认菜单以左上角为原点缩放（顶边贴着按钮，
    不是从菜单正中心鼓出来）。
  - Rendering 面板切换 `prefers-reduced-motion: reduce` → 菜单瞬时出现，位置正确。
- **Done when**: 导出菜单每次打开都有 150ms 的来源动效、原点在触发侧，关闭仍瞬时，
  Console 无 CSS 错误。
