# 001 — 给确认弹窗与报告预览浮层加入场动画

- **Status**: DONE
- **Commit**: 72a4272
- **Severity**: HIGH
- **Category**: 8. Missed opportunities（浮层硬切出现）
- **Estimated scope**: 1 文件（styles.css），约 25 行

## Problem

确认弹窗（删除 / 取消任务的二次确认）与报告预览浮层是全应用最主要的两层浮层，
但它们整层瞬间弹出、瞬间消失，零过渡。用户点「删除」后整个遮罩 + 面板硬切到脸上，
是最扎眼的动效缺失。

当前代码（`src/videread/web/static/styles.css:771` 起，逐字摘录）：

```css
/* src/videread/web/static/styles.css:771 — current */
.modal {
  position: fixed;
  inset: 0;
  z-index: 60;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(24, 38, 50, .32);
}
```

```css
/* src/videread/web/static/styles.css:780 — current */
.modal-box {
  width: min(400px, calc(100vw - 48px));
  background: var(--glass-2);
  border: 1px solid var(--glass-border);
  border-radius: var(--radius);
  padding: 20px 22px;
  box-shadow: var(--sheen), var(--shadow-raised);
  -webkit-backdrop-filter: blur(var(--glass-blur)) saturate(150%);
  backdrop-filter: blur(var(--glass-blur)) saturate(150%);
}
```

```css
/* src/videread/web/static/styles.css:794 — current */
.preview {
  position: fixed;
  inset: 0;
  z-index: 50;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: rgba(24, 38, 50, .30);
  -webkit-backdrop-filter: blur(6px);
  backdrop-filter: blur(6px);
}
```

```css
/* src/videread/web/static/styles.css:806 — current */
.preview-box {
  display: flex;
  flex-direction: row;
  width: min(1360px, 100%);
  height: min(880px, 100%);
  background: var(--glass-2);
  border: 1px solid var(--glass-border);
  border-radius: var(--radius);
  overflow: hidden;
  box-shadow: var(--sheen), var(--shadow-raised);
  -webkit-backdrop-filter: blur(22px) saturate(150%);
  backdrop-filter: blur(22px) saturate(150%);
}
```

显示/隐藏由 JS 用 `hidden` 属性完成（app.js 的 `show()` → `target.hidden = !visible`），
配合 `styles.css:7` 的 `[hidden] { display: none !important; }`——元素从 `display:none`
变为可见正是 `@starting-style` 的触发时机。

## Target

入场动效：遮罩淡入 + 面板 `scale(0.97) → 1` 淡入，200ms，强 ease-out 曲线。
出场保持瞬时（`hidden` 直接隐藏，无需 JS 也不做退出动画——弹窗关闭是用户主动动作，
瞬时关闭更干脆）。弹窗居中出现，`transform-origin` 用 center 是正确的，无需设置。

```css
/* target —— 新增动效 token（放在 :root 现有变量之后，styles.css:38 附近） */
:root {
  /* …现有变量保持不动… */
  --ease-out: cubic-bezier(0.23, 1, 0.32, 1);  /* 强 ease-out，用于 UI 入场 */
}
```

```css
/* target —— .modal 追加 transition，同块新增 @starting-style */
.modal {
  position: fixed;
  inset: 0;
  z-index: 60;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(24, 38, 50, .32);
  transition: opacity 200ms var(--ease-out);
}
@starting-style {
  .modal { opacity: 0; }
}
```

```css
/* target —— .modal-box 追加 transition 与 @starting-style（可与上面合并进同一个 @starting-style 块） */
.modal-box {
  /* …现有声明全部不动… */
  transition: transform 200ms var(--ease-out), opacity 200ms var(--ease-out);
}
@starting-style {
  .modal-box { opacity: 0; transform: scale(0.97); }
}
```

```css
/* target —— .preview 与 .preview-box 同样处理 */
.preview {
  /* …现有声明全部不动… */
  transition: opacity 200ms var(--ease-out);
}
.preview-box {
  /* …现有声明全部不动… */
  transition: transform 200ms var(--ease-out), opacity 200ms var(--ease-out);
}
@starting-style {
  .preview { opacity: 0; }
  .preview-box { opacity: 0; transform: scale(0.97); }
}
```

```css
/* target —— 并入 styles.css:967 已有的 reduced-motion 块 */
@media (prefers-reduced-motion: reduce) {
  .btn, .run-card, .stepper li { transition: none; }
  .modal, .modal-box, .preview, .preview-box { transition: none; }
}
```

数值依据（AUDIT.md 第 2 节）：入场用 ease-out；Modals 时长预算 200–500ms，取下限 200ms；
UI 动画总预算 <300ms；`--ease-out: cubic-bezier(0.23, 1, 0.32, 1)` 逐字取自 AUDIT.md。
scale 用 0.97，符合「不用 scale(0)，入场 scale(0.9–0.97) + opacity: 0」。

## Repo conventions to follow

- 本仓库没有独立 token 文件——所有变量集中在 `styles.css` 的 `:root`（styles.css:9–39），
  新 token 就加在那里，紧挨现有玻璃变量。
- 本仓库已有 `@starting-style` 先例：`styles.css:205–210` 下拉菜单的展开动画
  「只写在 @starting-style：关闭态不设 opacity，菜单不会因动画失效而不可见」——
  照抄这个模式：入场值写 `@starting-style`，基础态不写 opacity 初值。
- reduced-motion 不是关掉一切：保持属性瞬时到位即可（`transition: none`），
  仓库在 styles.css:491、563、967 已有三处这样的写法，追加到最后一处。
- 注释用中文、说明「为什么」，与文件现有注释风格一致。

## Steps

1. 在 `src/videread/web/static/styles.css` 的 `:root` 块（第 9–39 行）末尾、
   `--shadow-inset` 之后追加一行：
   `  --ease-out: cubic-bezier(0.23, 1, 0.32, 1); /* 强 ease-out，用于 UI 入场 */`
2. 给 `.modal` 规则（第 771 行）追加
   `transition: opacity 200ms var(--ease-out);`，
   并在其后新增块：
   ```css
   @starting-style {
     .modal { opacity: 0; }
   }
   ```
3. 给 `.modal-box` 规则（第 780 行）追加
   `transition: transform 200ms var(--ease-out), opacity 200ms var(--ease-out);`，
   把 `.modal-box { opacity: 0; transform: scale(0.97); }` 合并进第 2 步的
   同一个 `@starting-style` 块。
4. 给 `.preview` 规则（第 794 行）追加
   `transition: opacity 200ms var(--ease-out);`；给 `.preview-box` 规则（第 806 行）
   追加 `transition: transform 200ms var(--ease-out), opacity 200ms var(--ease-out);`。
   新增（或合并进）一个 `@starting-style` 块包含
   `.preview { opacity: 0; }` 与 `.preview-box { opacity: 0; transform: scale(0.97); }`。
5. 修改第 967 行已有的 `@media (prefers-reduced-motion: reduce)` 块，改为：
   ```css
   @media (prefers-reduced-motion: reduce) {
     .btn, .run-card, .stepper li { transition: none; }
     .modal, .modal-box, .preview, .preview-box { transition: none; }
   }
   ```

## Boundaries

- 只改 `src/videread/web/static/styles.css`。不碰 `app.js`、`index.html`。
- 不改任何布局/结构/颜色声明——只新增 transition、`@starting-style`、token 和
  reduced-motion 条目。
- 不做出场（关闭）动画：`hidden` 直接隐藏的行为保持原样，不要引入 JS 延迟隐藏。
- 不加任何依赖、不加 `@keyframes`（入场用 transition + `@starting-style` 足够）。
- 如果代码与 commit 72a4272 相比已漂移（行号对不上、`@starting-style` 已被移除等），
  停下并报告，不要即兴发挥。

## Verification

- **Mechanical**: 无构建步骤。用本地服务打开控制台页面（`styles.css` 无语法错误即可：
  在 DevTools Console 确认无 CSS parse error，或运行
  `powershell -Command "node -e \"...\""` 之类不适用——直接以浏览器加载为准）。
- **Feel check**: 运行 UI，触发以下交互，确认：
  - 点报告库某条记录的「删除」→ 弹窗遮罩淡入、面板从 0.97 放大淡入，200ms 内到位，
    不再瞬间糊脸。
  - 点某条记录的「预览」→ 预览浮层同样淡入 + 微放大；遮罩先亮、面板跟随，无跳变。
  - 关闭弹窗/预览（点遮罩或 Esc）→ 仍是瞬时消失（本计划有意不做出场）。
  - 在 DevTools Animations 面板把回放调到 10%：确认入场是「快速出现、缓缓稳住」
    的 ease-out 曲线，不是匀速也不是先慢后快。
  - Rendering 面板切换 `prefers-reduced-motion: reduce` → 弹窗瞬时出现但内容完整、
    位置正确（丢位移、保反馈）。
- **Done when**: 两个浮层入场均可观察到 200ms 淡入 + scale(0.97→1)，reduced-motion
  下瞬时到位，Console 无 CSS 错误。
