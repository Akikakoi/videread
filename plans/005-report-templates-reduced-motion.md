# 005 — 三份报告模板补 prefers-reduced-motion

- **Status**: TODO
- **Commit**: 72a4272
- **Severity**: MEDIUM
- **Category**: 无障碍
- **Estimated scope**: 3 个文件，各插入 1 个媒体块（6 行）

## Problem

三份报告模板（`src/videread/templates/report.html`、`brief-report.html`、`outline-report.html`）是自包含单文件产物，整仓 `prefers-reduced-motion` 只存在于控制台 `styles.css`（4 处），模板里一处都没有。用户在系统里开了「减少动态效果」后，报告里的 chevron 旋转照常播放：

```css
/* src/videread/templates/report.html:106-115 — 当前（brief/outline 同址同文） */
.meta-fold > summary::before,
.sources > summary::before {
  content: "▸";
  display: inline-block;
  margin-right: 8px;
  color: var(--ink-mute);
  transition: transform .16s ease;
}
.meta-fold[open] > summary::before,
.sources[open] > summary::before { transform: rotate(90deg); }
```

按减少动态的处理原则：保留辅助理解的反馈（颜色、透明度），去掉位移。chevron 旋转是位移，reduce 下应瞬切（`transition: none` 后状态切换瞬间完成，开/关指示不丢失）。`report.html:472-480` 的 `.report-nav-link` 只有 color / border-color 过渡，属颜色反馈，reduce 下保留，**不动**。

## Target

三个文件插入完全相同的块（选择器名三份模板一致），位置统一放在各自 `@media print` 块之后、`/* ── 视频截图` 注释之前：

```css
/* target — 插入点见 Steps */
/* ── 动效偏好：减少动态时保留状态切换、去掉位移 ── */
@media (prefers-reduced-motion: reduce) {
  .meta-fold > summary::before,
  .sources > summary::before { transition: none; }
}
```

## Repo conventions to follow

- 控制台 `styles.css` 的 reduce 块用中文分段注释 + 选择器逐行列出（例：`styles.css:231-233`、`566-568`、`999-1002`）。本块沿用同款注释口吻。
- 模板是离线自包含单文件，不能抽共享样式表，三份各自持有同一份块——这是本仓既定结构（色板等已三份重复），不违反 DRY 原则的范围。

## Steps

1. `src/videread/templates/report.html`：在 `@media print { ... }` 块（520-543 行）之后、`/* ── 视频截图（--frames 开启时由程序注入，非 LLM 输出）── */`（545 行）之前，插入 Target 中的块。
2. `src/videread/templates/brief-report.html`：同样插在其 `@media print` 块（511-527 行）之后、`/* ── 视频截图` 注释（529 行）之前。
3. `src/videread/templates/outline-report.html`：同样插在其 `@media print` 块（198-210 行）之后、`/* ── 视频截图` 注释（212 行）之前。
4. 校验：`grep -c "prefers-reduced-motion" src/videread/templates/*.html` 三个文件都应输出 1。

## Boundaries

- 只插入上述媒体块。不改 chevron 的 `.16s ease` 旋转本身（时长/缓动不在本计划范围），不动 `.report-nav-link`、`.ts-jump` 等其他规则。
- 不碰 `src/videread/web/static/styles.css`——控制台的 reduce 覆盖已完备。
- 不新增文件、不改模板结构。
- 若插入点代码已漂移，停下报告。

## Verification

- **机械**：`grep -c "prefers-reduced-motion" src/videread/templates/*.html` 三文件均为 1；各文件仍可被 `python -m videread.web` 正常渲染（模板是 Jinja 风格占位符替换，纯 CSS 插入不影响占位符）。
- **手感**：三种模式各生成一份报告（或直接用 `runs/` 下已生成的 report.html），浏览器打开：
  - 默认（无 reduce）：展开「视频简介」「来源与处理说明」，chevron 仍旋转 90°。
  - DevTools Rendering 面板勾选 `prefers-reduced-motion: reduce` 后刷新：chevron 瞬切到 90°，无旋转过程；折叠/展开功能与箭头指示正常。
  - 打印预览（`@media print`）不受影响。
- **Done when**：reduce 偏好下三份模板的 chevron 均无旋转动画，默认模式下观感与改前完全一致。
