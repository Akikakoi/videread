# 动效改进实施计划

由 `improve-animations` 审计生成（基线 commit：`72a4272`）。
审计范围：`src/videread/web/static/`（styles.css / app.js / index.html，纯静态控制台）。

## 计划清单

| # | 标题 | 严重度 | 状态 |
| --- | --- | --- | --- |
| 001 | [确认弹窗与报告预览浮层入场动画](001-modal-preview-entry.md) | HIGH | DONE |
| 002 | [导出菜单从触发按钮位置展开](002-export-menu-entry.md) | MEDIUM | DONE |
| 003 | [报告卡片悬停抬升加触屏门控](003-run-card-hover-gate.md) | MEDIUM | DONE |
| 004 | [进度条改用 transform: scaleX 驱动](004-progress-scale-x.md) | HIGH | DONE |
| 005 | [三份报告模板补 prefers-reduced-motion](005-report-templates-reduced-motion.md) | MEDIUM | DONE |
| 006 | [按压反馈修正：时长、缓动与幅度](006-press-feedback.md) | MEDIUM | DONE |

## 推荐执行顺序

**001 → 002 → 003**（均已完成）

- 001 与 002 都只改 `styles.css`，且 002 依赖 001 引入的 `--ease-out` token
  （002 的步骤 1 会在 token 缺失时兜底添加，但按顺序执行可避免重复）。
- 003 与 001/002 无依赖，可独立执行；但它与 001 都会改动第 967 行的
  reduced-motion 块，按顺序执行可减少冲突。

**004 → 005 → 006**（来自 2026-10-15 全项目复审，基线同为 `72a4272`；均已实施，改动在工作区未提交）

- 004：进度条 scaleX 化，改 `styles.css` 进度规则 + `app.js` 一行；全应用最高频动画，优先做。
- 005：三份报告模板各插一个 reduce 媒体块；与控制台文件零重叠，可独立执行。
- 006：按压反馈 4 处小编辑；其中第 4 步动 `styles.css:1000` 的 reduce 块，
  与 004 的进度规则（494-498 行的 reduce 块）不相邻，无冲突。
- 三者互不依赖，可按任意顺序执行；按上表顺序可让「高频问题 → 无障碍 → 细节」依次落地。

## 备注

- 001–003 均只涉及 `src/videread/web/static/styles.css`，不需要构建步骤，
  验证方式为浏览器手感检查（各计划的 Verification 一节含具体观察点）。
- 004/005/006 来自 2026-10-15 全项目复审（范围扩大到 `src/videread/templates/`
  三份报告模板）。复审中未选入本批的发现（QA 气泡入场、错误提示入场、
  弹窗/预览退场、pill 换色、picker 缓动与原点、报告页平滑滚动、链接 hover
  过渡等）与错失机会，可按需后续补 007+ 计划。
