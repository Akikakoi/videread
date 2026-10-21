"use strict";
const API = (location.protocol === "file:") ? "http://127.0.0.1:8765" : "";
const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
/* JS 触发的滚动不受 CSS scroll-behavior:auto 管，reduce 下须显式降级为瞬间跳转 */
const scrollBehavior = () => matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";

const params = new URLSearchParams(location.search);
let runId = params.get("run_id") || "";
if(!runId){
  const seg = location.pathname.split("/").filter(Boolean).pop() || "";
  if(seg !== "read" && seg !== "editorial-reader.html") runId = decodeURIComponent(seg);
}
const sec = parseInt(params.get("sec") || "0", 10);
/* 长图导出模式（?png=1）：无头浏览器经由此页截编辑风长图。
   渲染完成后把内容高度写进 document.title（VRH: 标记）供 --dump-dom 测高。 */
const PNG_MODE = params.has("png");
if(PNG_MODE) document.documentElement.classList.add("png-mode");

const page = document.getElementById("reader-page");
const progress = document.getElementById("progress");
let progTicking = false;
function updateProgress(){
  const h = document.documentElement;
  progress.style.width = (h.scrollTop / (h.scrollHeight - h.clientHeight) * 100) + "%";
  progTicking = false;
}
addEventListener("scroll", () => {
  if(progTicking) return;
  progTicking = true;
  requestAnimationFrame(updateProgress);
}, {passive: true});

if(!runId){
  page.innerHTML = '<p class="skeleton">缺少 run_id —— 请从编辑部目录进入某一期。</p>';
}else{
  open(runId, sec);
}

async function open(runId, sec){
  try{
    const res = await fetch(API + "/report/" + encodeURIComponent(runId));
    if(!res.ok) throw new Error("HTTP " + res.status);
    const doc = new DOMParser().parseFromString(await res.text(), "text/html");
    const head = doc.querySelector(".report-head");
    const body = doc.querySelector(".report-body");
    if(!body) throw new Error("no body");
    /* 先从被剥离的脚本里抢救时间映射 window.__VTS（u0001 → start 秒），
       阅读器自己负责注入段落级时间戳跳转胶囊（原报告由内嵌脚本完成）。 */
    let vts = null;
    doc.querySelectorAll("script").forEach(s => {
      const m = /window\.__VTS\s*=\s*\[([^\]]*)\]\s*;/.exec(s.textContent || "");
      if(m && vts === null){
        const arr = m[1].split(",").map(x => parseFloat(x.trim()));
        vts = arr.every(Number.isFinite) ? arr : null;
      }
    });
    body.querySelectorAll("script,style,noscript").forEach(n => n.remove());
    let i = 0;
    body.querySelectorAll("h2").forEach(h => { i++; h.id = "sec-" + i; });

    page.innerHTML = "";
    const frag = document.createDocumentFragment();
    if(head){
      ["h1", ".subtitle", ".lead", ".attribution"].forEach(sel => {
        const n = head.querySelector(sel);
        if(n) frag.appendChild(n);
      });
    }
    frag.appendChild(body);
    /* report-body 之外的伴生内容：视频简介折叠块（head 内）与
       report-foot 里的「来源与处理说明」——不搬会整块丢失。 */
    if(head){
      const mfold = head.querySelector(".meta-fold");
      if(mfold) frag.appendChild(mfold);
    }
    const rfoot = doc.querySelector(".report-foot");
    if(rfoot) frag.appendChild(rfoot);
    page.appendChild(frag);
    injectTsJumps(vts);
    const h1 = page.querySelector("h1");
    document.title = (h1 ? h1.textContent.trim() + " — " : "") + "VIDEREAD 视读";
    document.getElementById("reader-run").textContent = runId;
    document.getElementById("back").href = "/editorial";
    if(location.protocol === "file:") document.getElementById("back").href = "about:blank";
    if(sec) jump(sec);
    buildNav();
    if(PNG_MODE) document.title = "VRH:" + document.documentElement.scrollHeight;
  }catch(err){
    page.innerHTML = '<p class="skeleton">全文读取失败（' + esc(String(err.message || err)) +
      '）。这份报告可能还没有生成 report.html，回编辑部看看其它期。</p>';
    /* 失败也要给测高标记：截图趟拿到的是错误页，但导出不至于整体挂死 */
    if(PNG_MODE) document.title = "VRH:" + document.documentElement.scrollHeight;
  }
}

function jump(sec){
  const el = page.querySelector("#sec-" + sec);
  if(el) requestAnimationFrame(() => el.scrollIntoView({behavior: scrollBehavior(), block: "start"}));
}

/* 左侧悬浮章节导航：原版报告由内嵌脚本生成，阅读页拼装时脚本被剥离、
   nav 元素也不在搬运清单里，这里按同一套交互重建——h2 锚点 + 滚动高亮。 */
function buildNav(){
  const heads = Array.from(page.querySelectorAll("h2"));
  if(!heads.length) return;
  const nav = document.createElement("nav");
  nav.className = "report-nav";
  nav.setAttribute("aria-label", "章节导航");
  const list = document.createElement("ol");
  list.className = "report-nav-list";
  const links = heads.map((h, i) => {
    if(!h.id) h.id = "sec-" + (i + 1);
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.className = "report-nav-link";
    a.href = "#" + h.id;
    a.textContent = h.textContent.trim();
    li.appendChild(a);
    list.appendChild(li);
    return a;
  });
  nav.appendChild(list);
  document.body.appendChild(nav);
  const activate = id => links.forEach(a => a.classList.toggle("is-active", a.getAttribute("href") === "#" + id));
  if("IntersectionObserver" in window){
    const io = new IntersectionObserver(entries => {
      entries.forEach(e => { if(e.isIntersecting) activate(e.target.id); });
    }, {rootMargin: "-10% 0px -70% 0px", threshold: 0});
    heads.forEach(h => io.observe(h));
  }else{
    const onScroll = () => {
      let cur = heads[0];
      heads.forEach(h => { if(h.getBoundingClientRect().top <= 120) cur = h; });
      activate(cur.id);
    };
    addEventListener("scroll", onScroll, {passive: true});
  }
}

/* 段落级时间戳跳转：逻辑与原报告内嵌脚本一致——取段落
   data-source-units 引用的转写单元中最早的 start 秒，生成
   "▷ mm:ss" 胶囊，新标签打开原视频对应时刻。 */
function injectTsJumps(vts){
  if(!vts || !vts.length) return;
  const origin = page.querySelector('.attribution a[href^="http"]');
  if(!origin) return;
  const base = origin.getAttribute("href");
  const pad = n => (n < 10 ? "0" : "") + n;
  const stamp = sec => {
    const t = Math.max(0, Math.round(sec));
    const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
    return (h ? h + ":" + pad(m) : pad(m)) + ":" + pad(s);
  };
  page.querySelectorAll("[data-source-units]").forEach(block => {
    const ids = (block.getAttribute("data-source-units") || "").split(",");
    let best = -1;
    ids.forEach(id => {
      const m = /^u(\d+)$/.exec(id.trim());
      if(!m) return;
      const sec = vts[parseInt(m[1], 10) - 1];
      if(typeof sec === "number" && sec >= 0 && (best < 0 || sec < best)) best = sec;
    });
    if(best < 0) return;
    const a = document.createElement("a");
    a.className = "ts-jump";
    a.href = base + (base.indexOf("?") >= 0 ? "&" : "?") + "t=" + Math.round(best);
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = "▷ " + stamp(best);
    block.appendChild(a);
  });
}
