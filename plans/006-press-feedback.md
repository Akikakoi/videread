# 006 — 按压反馈修正：时长、缓动与幅度

- **Status**: TODO
- **Commit**: 72a4272
- **Severity**: MEDIUM
- **Category**: 物理性 / 缓动与时长
- **Estimated scope**: 1 个文件（styles.css），4 处小编辑

## Problem

按压反馈（`:active`）是最高频的交互动画——按钮每次点击都走一遍。当前两处实现都不合格：

1. 复选框方框 `transform` 过渡 `.1s ease`、按压幅度 `scale(.9)`：幅度过猛（标准区间 0.95–0.98），bare ease 起步慢。
2. 按钮 `transform` 过渡 `.18s ease`：超出按压反馈 100–160ms 预算，且 bare ease 起步慢——按压要的是「一按就有反应」。

```css
/* src/videread/web/static/styles.css:263-275 — 当前（摘录） */
.check .box {
  ...
  transition: background .14s ease, border-color .14s ease, transform .1s ease;
}
```

```css
/* src/videread/web/static/styles.css:297 — 当前 */
.check:active .box { transform: scale(.9); }
```

```css
/* src/videread/web/static/styles.css:303-321 — 当前（摘录） */
.btn {
  ...
  transition: background .18s ease, border-color .18s ease, transform .18s ease;
}
```

```css
/* src/videread/web/static/styles.css:323 — 当前 */
.btn:active { transform: translateY(1px); }
```

按钮的 `translateY(1px)` 物理下沉是本仓既有风格，观感成立，**保留不动**；本计划只修 transform 通道的时长与缓动。复选框的 `scale(.9)` 必须收敛幅度。

## Target

按压反馈统一为：幅度 0.95–0.98 区间、时长 100–160ms、缓动 `var(--ease-out)`（即 `cubic-bezier(0.23, 1, 0.32, 1)`，token 定义于 `styles.css:41`）。

```css
/* target — styles.css:274 */
  transition: background .14s ease, border-color .14s ease, transform 140ms var(--ease-out);
```

```css
/* target — styles.css:297 */
.check:active .box { transform: scale(.96); }
```

```css
/* target — styles.css:320 */
  transition: background .18s ease, border-color .18s ease, transform 140ms var(--ease-out);
```

```css
/* target — styles.css:1000 — reduce 块补上方框（按压位移在 reduce 下同样去掉） */
  .btn, .run-card, .stepper li, .check .box { transition: none; }
```

`.btn:active`（323 行）与 `.btn:disabled { transform: none }`（324 行）保持不变。

## Repo conventions to follow

- `--ease-out` token 位于 `styles.css:41`，001/002 计划引入后 modal / preview / export-menu 均已引用；本计划继续沿用，不新增 token。
- 时长取 140ms：按压预算 100–160ms 的中值，比 `.check .box` 颜色通道 `.14s` 对齐、比 `.btn` 颜色通道 `.18s` 更快——按压是即时反馈，理应快于悬停换色。
- 反移动偏好块既有写法：`styles.css:999-1002`，选择器逐行逗号排列，本计划只往第 1000 行追加 `.check .box`。

## Steps

1. `src/videread/web/static/styles.css` 第 274 行：`transform .1s ease` 改为 `transform 140ms var(--ease-out)`（background / border-color 两个通道保持 `.14s ease` 不变）。
2. 同文件第 297 行：`transform: scale(.9)` 改为 `transform: scale(.96)`。
3. 同文件第 320 行：`transform .18s ease` 改为 `transform 140ms var(--ease-out)`（background / border-color 两个通道保持 `.18s ease` 不变）。
4. 同文件第 1000 行：选择器列表末尾追加 `, .check .box`，使 reduce 下方框按压也不再位移（颜色反馈保留）。
5. 校验：`grep -n "transform 140ms var(--ease-out)" src/videread/web/static/styles.css` 应命中 2 处；`grep -n "scale(.9)" src/videread/web/static/styles.css` 应无输出。

## Boundaries

- 只改上述 4 行。不碰 `.btn-primary` 悬停阴影、`.check` 颜色体系、`.run-card`、`::picker(select)`（其缓动与原点问题另有计划）、`.probe-action`。
- 不改 markup，不新增依赖，不动 `.btn:active` 的 `translateY(1px)` 既有风格。
- 若代码与摘录漂移，停下报告。

## Verification

- **机械**：`grep` 检查见 Steps 第 5 步；无构建步骤。
- **手感**：启动控制台，打开「新建任务」表单：
  - 勾选/取消复选框：方框下沉幅度明显变浅（接近「轻按」而非「按瘪」），回弹跟手，无慢启动。
  - 按住「开始生成」或任一按钮：下沉在按下的瞬间就开始（ease-out 起步快），140ms 内完成；松手回弹同样跟手。
  - DevTools Animations 面板 10% 播放：transform 过渡曲线前段陡峭（ease-out 特征），全程无 layout 标记。
  - 连续快速点击按钮 10 次：每次按压都从当前状态平滑接续，无卡顿或跳变。
  - Rendering 面板开 `prefers-reduced-motion: reduce`：按钮与方框均无下沉位移，颜色/背景反馈仍在。
- **Done when**：复选框与按钮的按压反馈在默认模式下「轻、快、跟手」，reduce 模式下位移消失而色彩反馈保留。
