# 004 — 进度条改用 transform: scaleX 驱动

- **Status**: TODO
- **Commit**: 72a4272
- **Severity**: HIGH
- **Category**: 性能 / 缓动与时长
- **Estimated scope**: 1 条 CSS 规则 + 1 行 JS（2 个文件）

## Problem

进度条填充是控制台里更新最频繁的动画：任务执行期间每次阶段推进 / 轮询（约每 2s）都会更新一次，长任务里持续数分钟。当前实现有三个问题：

1. 动画属性是 `width`——布局属性，触发 layout + paint，不走合成层。
2. 时长 `.5s` 超出 UI 动画 300ms 预算；且百分比数字（`.progress-pct`）是瞬切的，条却要拖 500ms 才追上，数字与条长期不同步。
3. 手写曲线 `cubic-bezier(.22, .61, .36, 1)` 与仓库已有的 `--ease-out` token（`styles.css:41`，值 `cubic-bezier(0.23, 1, 0.32, 1)`）几乎相同，属重复定义。

```css
/* src/videread/web/static/styles.css:436-445 — 当前 */
.progress-fill {
  position: relative;
  height: 100%;
  width: 0;
  overflow: hidden;
  border-radius: 999px;
  background: linear-gradient(90deg, #0b5a72 0%, var(--accent) 40%, #1d9cbd 75%, #3ec3d8 100%);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, .5);
  transition: width .5s cubic-bezier(.22, .61, .36, 1);
}
```

```js
/* src/videread/web/static/app.js:220 — 当前（renderProgress 内） */
el.progressFill.style.width = percent + "%";
```

## Target

改用 `transform: scaleX()` 驱动，只改合成属性；时长收敛到 300ms，曲线改用既有 token。`border-radius` 从填充上移除——父级 `.progress-bar`（`styles.css:418-430`）已有 `border-radius: 999px` + `overflow: hidden`，圆角由父级裁剪提供，且 scaleX 下填充自身的 999px 圆角会被压成椭圆（小百分比时左端变形），必须去掉。

```css
/* target — src/videread/web/static/styles.css:436-445 */
.progress-fill {
  position: relative;
  height: 100%;
  width: 100%;
  overflow: hidden;
  background: linear-gradient(90deg, #0b5a72 0%, var(--accent) 40%, #1d9cbd 75%, #3ec3d8 100%);
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, .5);
  transform-origin: left center;
  transform: scaleX(0);
  transition: transform .3s var(--ease-out);
}
```

```js
/* target — src/videread/web/static/app.js:220 */
el.progressFill.style.transform = "scaleX(" + (percent / 100) + ")";
```

行为等价性：`percent` 为 0 时 `scaleX(0)` 与旧 `width: 0` 同样不可见；`renderProgress` 在 `percent === null`（error/cancelled）时提前返回、不触碰填充，条冻结在最后值——与旧行为一致，无需额外处理。

已知取舍：运行中的流动斜纹 `.progress-bar[data-running="1"] .progress-fill::after`（`styles.css:456-468`，16px 条纹）是填充的子元素，scaleX 会把条纹横向压缩（小百分比时条纹变细密）。属可接受的装饰性衰减，不额外处理；验证时确认压缩后不出现视觉瑕疵即可。

## Repo conventions to follow

- 动效 token 已存在于 `src/videread/web/static/styles.css:41`：`--ease-out: cubic-bezier(0.23, 1, 0.32, 1);`（注释「强 ease-out，用于 UI 入场」）。本计划直接引用，不新增 token。
- 时长参考同文件既有值：弹窗/预览 200ms、导出菜单 150ms、`.progress-pct` 颜色 `.3s ease`（`styles.css:489`）。进度填充取 300ms，与 `.progress-pct` 同级。
- 反移动偏好块已覆盖本规则：`styles.css:494-498` 的 `.progress-fill { transition: none; }` 选择器不变即生效，无需改动。

## Steps

1. 编辑 `src/videread/web/static/styles.css`，将 436-445 行的 `.progress-fill` 规则整体替换为 Target 中的 CSS（注意：删掉 `border-radius: 999px;`，`width: 0` 改 `width: 100%`，新增 `transform-origin` / `transform`，`transition` 改为 `transform .3s var(--ease-out)`）。
2. 编辑 `src/videread/web/static/app.js`，将第 220 行 `el.progressFill.style.width = percent + "%";` 替换为 Target 中的 JS 一行。
3. 全仓确认无残留：`grep -rn "progressFill" src/videread/web/static/app.js` 应只剩 transform 一处赋值；`grep -n "transition: width" src/videread/web/static/styles.css` 应无输出。

## Boundaries

- 只改上述两条规则/一行 JS。不碰 `.progress-bar`、`.progress-pct`、`.stepper`、斜纹 `::after`、reduced-motion 块。
- 不改任何 markup 与 class 名，不新增依赖。
- 不顺手把 `.progress-bar` 的 `box-shadow .3s ease`（`styles.css:429`）一起改——不在本计划范围。
- 若代码与上面摘录已漂移（commit 戳对不上），停下报告，不要自行发挥。

## Verification

- **机械**：本仓无构建步骤（静态文件直服）。`grep` 检查见 Steps 第 3 步。可选：`python -m compileall src/videread/web/static/app.js` 无意义（JS），跳过。
- **手感**：启动控制台（`run.bat` 或 `python -m videread.web`），提交一个短任务（如几分钟的视频、standard 模式）：
  - 进度推进时，条的追赶应明显变跟手：数字跳变后 300ms 内条到位，不再拖 500ms 尾。
  - 小百分比时左端圆角保持圆润（父级裁剪），不出椭圆；右端是平头，符合进度条观感。
  - 运行中斜纹仍在流动；压缩后不出现肉眼可见的条纹破裂。
  - DevTools Animations 面板设 10% 播放，确认动画属性为 `transform`（Composited 属性，无 layout 标记）。
  - 反复刷新页面 / 续跑任务让条多次更新，确认每次都从当前值平滑接续（transition 自动重定向），不出现「回零再走」。
  - Rendering 面板打开 `prefers-reduced-motion: reduce`，确认条瞬切到目标值。
- **Done when**：进度条在所有阶段推进中与百分比数字同步到位，DevTools 中该动画无 layout/paint 开销。
