"use strict";
/* 编辑风三页共用：DOM 查询、HTML 转义、阶段名、任务终态与滚动偏好。 */
const $ = s => document.querySelector(s);
const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const STAGES = ["下载","音频处理","转写","规范化","结构规划","截图","逐节写作","渲染"];
const JOB_TERMINAL = new Set(["done","cancelled","failed","error"]);
/* JS 触发的滚动不受 CSS scroll-behavior:auto 管，reduce 下须显式降级为瞬间跳转 */
const scrollBehavior = () => matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";

/* 刷新一律从顶部开始：浏览器恢复的滚动位置会与异步渲染后的内容高度错位，
   表现为每次刷新都停在页面的不同位置。不支持的浏览器（如 Safari）由
   pageshow 兜底；页面内锚点与阅读页 ?sec= 深链仍保留原跳转行为。 */
if("scrollRestoration" in history) history.scrollRestoration = "manual";
addEventListener("pageshow", ev => {
  if(ev.persisted || location.hash || new URLSearchParams(location.search).has("sec")) return;
  const html = document.documentElement;
  html.style.scrollBehavior = "auto";
  scrollTo(0, 0);
  html.style.scrollBehavior = "";
});
