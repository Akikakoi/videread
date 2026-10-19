"use strict";
const API = (location.protocol === "file:") ? "http://127.0.0.1:8765" : "";
const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

const params = new URLSearchParams(location.search);
let runId = params.get("run_id") || "";
if(!runId){
  const seg = location.pathname.split("/").filter(Boolean).pop() || "";
  if(seg !== "read" && seg !== "editorial-reader.html") runId = decodeURIComponent(seg);
}
const sec = parseInt(params.get("sec") || "0", 10);

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
    document.getElementById("reader-title").textContent = h1 ? h1.textContent.trim() : "";
    document.getElementById("reader-run").textContent = runId;
    document.getElementById("back").href = "/editorial";
    if(location.protocol === "file:") document.getElementById("back").href = "about:blank";
    if(sec) jump(sec);
  }catch(err){
    page.innerHTML = '<p class="skeleton">全文读取失败（' + esc(String(err.message || err)) +
      '）。这份报告可能还没有生成 report.html，回编辑部看看其它期。</p>';
  }
}

function jump(sec){
  const el = page.querySelector("#sec-" + sec);
  if(el) requestAnimationFrame(() => el.scrollIntoView({behavior: "smooth", block: "start"}));
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
