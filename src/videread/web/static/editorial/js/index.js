"use strict";

/* ================= 环境 ================= */
const API = (location.protocol === "file:") ? "http://127.0.0.1:8765" : "";
/* outline.profile 是结构规划选出的信息结构（机器字段），五种合法值见 report/outline.py */
const STRUCTURE_ZH = {mechanism:"机理拆解",procedure:"步骤流程",evidence:"论据实证",argument:"观点论述",narrative:"叙事纪事"};
const structureZh = p => STRUCTURE_ZH[p] || (p ? p : "—");
let OFFLINE = false;

function toast(msg){
  const t = $("#toast");
  t.textContent = msg;
  clearTimeout(toast._h);
  clearTimeout(toast._out);
  t.classList.remove("out");
  t.style.display = "block";
  void t.offsetWidth; /* 强制重排，保证连续触发时入场动画重播 */
  toast._h = setTimeout(() => {
    t.classList.add("out"); /* 上浮 + 淡出退场 */
    toast._out = setTimeout(() => {
      t.style.display = "none";
      t.classList.remove("out");
    }, 280);
  }, 3600);
}

/* ================= 确认弹窗（替代原生 confirm） ================= */
let dlgResolve = null;
let dlgKind = "run";
/* 同一个 #veil 承担两种删除确认：整期与单封来信，文案随场景切换 */
const DLG_COPY = {
  run: {title: "撕掉这一期？", body: "该期的转写缓存、正文报告与问答历史将一并清除，删除后不可恢复。", cancel: "已取消删除，这一期先留着"},
  letter: {title: "撕掉这封来信？", body: "这封来信与编辑部的回信将从本期档案中移除，删除后不可恢复。", cancel: "已取消删除，这封来信先留着"}
};
function askDelete(kind, subject){
  const copy = DLG_COPY[kind] || DLG_COPY.run;
  dlgKind = DLG_COPY[kind] ? kind : "run";
  $("#dlg-title").textContent = copy.title;
  $("#dlg-body").textContent = copy.body;
  /* 弹窗里只示视频标题 / 来信问题，run_id 仅作兜底 */
  $("#dlg-run").textContent = subject || "";
  $("#veil").classList.add("open");
  $("#veil").setAttribute("aria-hidden", "false");
  $("#dlg-confirm").focus();
  return new Promise(res => { dlgResolve = res; });
}
function closeDlg(v){
  const veil = $("#veil");
  if(!veil.classList.contains("open")) return;
  veil.classList.remove("open");
  veil.setAttribute("aria-hidden", "true");
  if(dlgResolve){ dlgResolve(v); dlgResolve = null; }
  if(!v) toast((DLG_COPY[dlgKind] || DLG_COPY.run).cancel);
}
$("#dlg-cancel").addEventListener("click", () => closeDlg(false));
$("#dlg-confirm").addEventListener("click", () => closeDlg(true));
/* 仅当「按下」也发生在遮罩上才关闭：按住弹窗内文本拖到弹窗外松开时，
   click 会被派发到按下/松开点的公共祖先（遮罩），不做此判断弹窗会被误关 */
let veilPressed = false;
$("#veil").addEventListener("mousedown", e => { veilPressed = (e.target === e.currentTarget); });
$("#veil").addEventListener("click", e => {
  if(e.target.id === "veil" && veilPressed) closeDlg(false);
  veilPressed = false;
});
addEventListener("keydown", e => { if(e.key === "Escape") closeDlg(false); });

async function jfetch(path, opts){
  try{
    const r = await fetch(API + path, opts);
    if(!r.ok){
      let detail = r.status;
      try{ const j = await r.json(); if(j && j.detail) detail = j.detail; }catch(e){}
      throw new Error(detail);
    }
    if(OFFLINE){ OFFLINE = false; $("#offline").style.display = "none"; refreshAll(); }
    return await r.json();
  }catch(err){
    if(!OFFLINE && (err instanceof TypeError || (typeof err.message === "string" && err.message === "Failed to fetch"))){
      OFFLINE = true;
      $("#offline").style.display = "block";
      $("#hl-api").textContent = "演示数据（离线）";
    }
    throw err;
  }
}
const post = (path, body) => jfetch(path, {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify(body || {})
});
const del = path => jfetch(path, {method: "DELETE"});

/* ================= 演示数据（离线兜底） ================= */
const SAMPLE = {
  total: 4, pages: 1,
  runs: [
    {run_id:"BV1xx411c7mD", title:"一场关于深度阅读的漫谈", uploader:"不非常读书俱乐部", bvid:"BV1xx411c7mD",
     duration_text:"47:12", profile:"standard", sections:6, has_report:true, has_frames:false,
     generated_at:"2026-10-16 14:02", favorite:true, cursor:"a"},
    {run_id:"BV1ab411c2Ef", title:"从零理解扩散模型", uploader:"极客厨房", bvid:"BV1ab411c2Ef",
     duration_text:"32:40", profile:"deep", sections:5, has_report:true, has_frames:false,
     generated_at:"2026-10-15 21:47", favorite:false, cursor:"b"},
    {run_id:"BV1cd411c3Gh", title:"城市漫步指南：老城区的声音地图", uploader:"街角录音室", bvid:"BV1cd411c3Gh",
     duration_text:"58:03", profile:"standard", sections:7, has_report:true, has_frames:true,
     generated_at:"2026-10-14 10:31", favorite:false, cursor:"c"},
    {run_id:"BV1ef411c4Ij", title:"十分钟看懂央行利率", uploader:"经济小灶", bvid:"BV1ef411c4Ij",
     duration_text:"12:18", profile:"brief", sections:3, has_report:true, has_frames:false,
     generated_at:"2026-10-12 19:20", favorite:false, cursor:"d"}
  ],
  details: {
    "BV1xx411c7mD": {lead:"整理这件事，从来不是删繁就简那么简单。本期特稿从一段四十七分钟的访谈开始：语音转写、语义切分、要点提炼、引文回链，四个环节全部由流水线自动完成。",
      sections_detail:[
        {id:1, heading:"开场：我们为什么害怕长视频", time_range:"00:00 - 08:14", intent:"引入", units:34},
        {id:2, heading:"转写的艺术：从声音到文字", time_range:"08:14 - 19:16", intent:"讲解", units:58},
        {id:3, heading:"速度与深度：一对老冤家", time_range:"19:16 - 39:03", intent:"论述", units:76},
        {id:4, heading:"工具与方法", time_range:"39:03 - 43:22", intent:"讲解", units:22},
        {id:5, heading:"回答听众来信", time_range:"43:22 - 45:40", intent:"问答", units:12},
        {id:6, heading:"尾声：阅读的下一站", time_range:"45:40 - 47:12", intent:"收束", units:9}]},
    "BV1ab411c2Ef": {lead:"把扩散模型拆成三件事：加噪、学逆、采样。本期用极客厨房式的比喻把数学直觉讲透。",
      sections_detail:[
        {id:1, heading:"噪声与噪声的反对者", time_range:"00:00 - 07:10", intent:"引入", units:26},
        {id:2, heading:"前向过程：一步一步弄脏它", time_range:"07:10 - 14:52", intent:"讲解", units:41},
        {id:3, heading:"反向过程：把直觉还给概率", time_range:"14:52 - 24:05", intent:"论述", units:52},
        {id:4, heading:"采样与加速", time_range:"24:05 - 30:18", intent:"讲解", units:33},
        {id:5, heading:"尾声与延伸阅读", time_range:"30:18 - 32:40", intent:"收束", units:10}]},
    "BV1cd411c3Gh": {lead:"戴上耳机走过一条老街，你会发现城市的声音和它的砖墙一样有年代。",
      sections_detail:[
        {id:1, heading:"清晨的菜市场", time_range:"00:00 - 09:30", intent:"叙事", units:40},
        {id:2, heading:"修车铺的节奏", time_range:"09:30 - 19:12", intent:"叙事", units:44},
        {id:3, heading:"午后茶馆", time_range:"19:12 - 31:45", intent:"叙事", units:52},
        {id:4, heading:"黄昏的桥洞", time_range:"31:45 - 44:20", intent:"叙事", units:48},
        {id:5, heading:"夜市开场", time_range:"44:20 - 52:00", intent:"叙事", units:36},
        {id:6, heading:"收音师访谈", time_range:"52:00 - 55:30", intent:"问答", units:18},
        {id:7, heading:"尾声", time_range:"55:30 - 58:03", intent:"收束", units:8}]},
    "BV1ef411c4Ij": {lead:"加息、降息与你的钱包，十二分钟讲清楚传导链条。",
      sections_detail:[
        {id:1, heading:"利率是什么", time_range:"00:00 - 04:02", intent:"讲解", units:16},
        {id:2, heading:"传导链条", time_range:"04:02 - 08:44", intent:"论述", units:22},
        {id:3, heading:"总结", time_range:"08:44 - 12:18", intent:"收束", units:9}]}
  },
  statsDays: [
    {date:"2026-10-16", runs:1, total_ms:412000, tokens:84210},
    {date:"2026-10-15", runs:2, total_ms:926000, tokens:173400},
    {date:"2026-10-14", runs:1, total_ms:1043000, tokens:210800},
    {date:"2026-10-12", runs:1, total_ms:233000, tokens:39500}
  ],
  qa: {
    "BV1xx411c7mD": {history:[
      {question:"嘉宾提到的「三遍阅读法」具体是哪三遍？",
       answer:"嘉宾把三遍概括为：第一遍通读抓结构，第二遍带问题精读并做标注，第三遍只读自己标注的部分串联成自己的话。他强调第三遍不是重复，而是输出。",
       cites:"转写稿 19:16 - 24:05"}]}
  }
};

function sampleQa(runId){ return SAMPLE.qa[runId] || {history: []}; }

/* ================= 首屏统计 ================= */
function fmtDur(ms){
  const s = Math.round(ms / 1000);
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  return h ? h + " h " + m + " min" : m + " min";
}
function fmtNum(n){ return Number(n || 0).toLocaleString("en-US"); }

async function loadHero(){
  try{
    const r = await jfetch("/api/runs?page=1&page_size=50");
    const total = r.total != null ? r.total : (r.runs ? r.runs.length : 0);
    $("#hl-total").textContent = fmtNum(total) + " 期";
    const first = r.runs && r.runs[0];
    $("#hl-last").textContent = first ? (first.generated_at || "—") : "暂无";
    if(!OFFLINE) $("#hl-api").textContent = (location.protocol === "file:" ? API || "同源" : "同源") + " /api/runs";
    $("#vol").textContent = "LIVE — 在库 " + fmtNum(total) + " 期";
  }catch(e){
    $("#hl-total").textContent = fmtNum(SAMPLE.total) + " 期";
    $("#hl-last").textContent = SAMPLE.runs[0].generated_at;
    $("#hl-api").textContent = "演示数据（离线）";
  }
  const d = new Date();
  const week = "日一二三四五六"[d.getDay()];
  $("#kicker-date").textContent = d.getFullYear() + " 年 " + (d.getMonth() + 1) + " 月 " + d.getDate() + " 日 · 星期" + week;
}

/* ================= 特稿 ================= */
let currentRunId = null;
let currentVideoUrl = "";

async function openFeature(runId){
  currentRunId = runId;
  const body = $("#feature-body");
  body.innerHTML = '<p class="skeleton">正在装订这一期…</p>';
  const fhint = $("#feature-hint");
  if(fhint) fhint.textContent = "";
  let d;
  try{
    d = await jfetch("/api/runs/" + encodeURIComponent(runId));
  }catch(e){
    if(!OFFLINE){ toast("读取详情失败：" + e.message); return; }
    const s = SAMPLE.runs.find(x => x.run_id === runId);
    d = Object.assign({}, s, SAMPLE.details[runId] || {sections_detail: []});
  }
  const qa = sampleQa(runId);
  try{
    const h = await jfetch("/api/runs/" + encodeURIComponent(runId) + "/qa");
    if(h && h.history) qa.history = h.history;
  }catch(e){ if(OFFLINE) qa.history = sampleQa(runId).history; }

  const sections = d.sections_detail || [];
  currentVideoUrl = d.url || "";
  body.innerHTML =
    '<article class="article">' +
      '<header class="art-head">' +
        '<h2>' + esc(d.title_full || d.title) + '</h2>' +
        (d.subtitle ? '<div class="art-sub">' + esc(d.subtitle) + '</div>' : "") +
        '<div class="art-byline">' +
          (d.uploader ? '<span>文 / <a href="' + (d.uploader_id ? "https://space.bilibili.com/" + esc(d.uploader_id) : "https://search.bilibili.com/upuser?keyword=" + encodeURIComponent(d.uploader)) + '" target="_blank" rel="noopener">' + esc(d.uploader) + '</a></span>' : "") +
          (d.bvid ? '<span><a href="' + esc(currentVideoUrl || ("https://www.bilibili.com/video/" + d.bvid)) + '" target="_blank" rel="noopener">' + esc(d.bvid) + '</a></span>' : "") +
          (d.duration_text ? '<span>' + esc(d.duration_text) + '</span>' : "") +
          (d.generated_at ? '<span>刊于 ' + esc(String(d.generated_at).split(" ")[0]) + '</span>' : "") +
        '</div>' +
      '</header>' +
      '<p class="lead' + (d.lead ? "" : " empty") + '">' + esc(d.lead || "") + '</p>' +
      (sections.length ?
        '<ul class="chapters">' + sections.map((s, i) =>
          '<li data-sec="' + (i + 1) + '" style="cursor:pointer" title="翻开这一章">' +
          '<span class="no">' + String(i + 1).padStart(2, "0") + '</span>' +
          '<div class="crow"><span class="t">' + esc(s.heading) + '</span>' +
          '<span class="dur">' + esc((s.time_range || "").replace("-", " – ")) + '</span></div>' +
          (s.intent ? '<div class="desc">' + esc(s.intent) + '</div>' : "") +
          '</li>').join("") + '</ul>' : "") +
      '<div class="actions">' +
        (d.has_report ? '<button class="act" id="btn-read" data-run="' + esc(runId) + '">翻开全文<span>→</span></button>' : "") +
        (d.has_report ? '<a class="act" href="' + API + '/report/' + encodeURIComponent(runId) + '" target="_blank" rel="noopener">原样打开</a>' : "") +
        (d.has_report && !d.has_frames ? '<a class="act" href="' + API + '/api/runs/' + encodeURIComponent(runId) + '/markdown">导出 MD</a>' : "") +
        (d.has_report ? '<a class="act" href="' + API + '/api/runs/' + encodeURIComponent(runId) + '/pdf">导出 PDF</a>' : "") +
        (d.has_report ? '<a class="act" href="' + API + '/api/runs/' + encodeURIComponent(runId) + '/png">导出 PNG</a>' : "") +
        '<button class="act" id="btn-rename" data-run="' + esc(runId) + '">改名</button>' +
        '<button class="act" id="btn-fav" data-fav="' + (d.favorite ? 1 : 0) + '">收藏本文<span id="fav-mark">' + (d.favorite ? "★" : "☆") + '</span></button>' +
      '</div>' +
      '<div class="letters">' +
        '<h4>LETTERS · 致编辑部</h4>' +
        '<div id="letters-list">' + (qa.history.length ? qa.history.map(renderLetter).join("") : LETTERS_EMPTY) + '</div>' +
        '<div class="askline"><input type="text" id="ask-input" placeholder="就本期内容提问，答案只依据转写稿…" aria-label="提问"><button id="btn-ask">寄出 →</button></div>' +
      '</div>' +
    '</article>';

  const favBtn = $("#btn-fav");
  favBtn.addEventListener("click", () => toggleFav(runId, favBtn));
  const renameBtn = $("#btn-rename");
  if(renameBtn) renameBtn.addEventListener("click", () => startRename(runId));
  const readBtn = $("#btn-read");
  if(readBtn) readBtn.addEventListener("click", () => openReaderPage(runId));
  document.querySelectorAll("#feature-body .chapters li[data-sec]").forEach(li => {
    li.addEventListener("click", () => openReaderPage(runId, parseInt(li.dataset.sec, 10)));
  });
  const askBtn = $("#btn-ask");
  /* 事件委托：ask() 只重绘 #letters-list 内部，容器上的监听不会失效 */
  $("#letters-list").addEventListener("click", e => {
    const btn = e.target.closest(".l-del");
    if(btn) deleteLetter(runId, btn);
  });
  askBtn.addEventListener("click", () => ask(runId, askBtn));
  $("#ask-input").addEventListener("keydown", e => { if(e.key === "Enter") ask(runId, askBtn); });
  /* 板块标题旁标注正在展示的是哪一期；目录里同步「本期特稿」标记 */
  const hint = $("#feature-hint");
  if(hint){
    hint.textContent = "正在展示 · " + (d.title_full || d.title || "");
    hint.title = hint.textContent;
  }
  syncFeatureMarks();
}

const LETTERS_EMPTY = '<p class="letters-empty">还没有来信。就这一期提个问题吧。</p>';

function renderLetter(l, i){
  /* 下标即 GET /qa 返回 history 的顺序，删除接口按同一下标定位；
     离线演示插入的记录没有真实下标，不渲染删除钮 */
  const del = Number.isInteger(i)
    ? '<button class="l-del" data-letter="' + i + '" title="删除这封来信" aria-label="删除这封来信">✕</button>'
    : "";
  return '<div class="letter">' +
    '<div class="l-head"><p class="q">' + esc(l.question || "") + '</p>' + del + '</div>' +
    /* 与原控制台同口径：answer_html 由服务端 sanitize_section_html 清洗后走 innerHTML */
    '<div class="a">' + (l.answer_html || "") + '</div>' +
    '</div>';
  /* 引文胶囊（l.citations 的 §N 定位原文 / ▶ 时间戳）按需求暂时不渲染，需要时在此恢复 */
}

async function deleteLetter(runId, btn){
  if(OFFLINE){ toast("离线演示模式，暂不支持删除"); return; }
  const index = Number(btn.dataset.letter);
  const letter = btn.closest(".letter");
  const question = letter && letter.querySelector(".q") ? letter.querySelector(".q").textContent : "";
  const ok = await askDelete("letter", question);
  if(!ok) return;
  btn.disabled = true;
  try{
    await del("/api/runs/" + encodeURIComponent(runId) + "/qa/" + index);
    if(letter) letter.remove();
    const list = $("#letters-list");
    if(list && !list.querySelector(".letter")) list.innerHTML = LETTERS_EMPTY;
    /* 服务端已重排下标；剩下的来信按 DOM 顺序回填，避免下一次删错条目 */
    else if(list) list.querySelectorAll(".l-del").forEach((b, i) => { b.dataset.letter = i; });
    toast("已撕掉这封来信");
  }catch(err){
    btn.disabled = false;
    toast("删除失败：" + err.message);
  }
}

async function toggleFav(runId, btn){
  const on = btn.dataset.fav === "1";
  try{
    if(on) await del("/api/runs/" + encodeURIComponent(runId) + "/favorite");
    else await post("/api/runs/" + encodeURIComponent(runId) + "/favorite");
    btn.dataset.fav = on ? "0" : "1";
    $("#fav-mark").textContent = on ? "☆" : "★";
    toast(on ? "已取消收藏" : "已收藏");
    loadRuns(state.page, true);
  }catch(e){
    toast(OFFLINE ? "离线演示模式，暂不支持收藏" : "操作失败：" + e.message);
  }
}

let asking = false;
async function ask(runId, btn){
  const input = $("#ask-input");
  const q = (input.value || "").trim();
  if(!q || asking) return;
  asking = true; btn.disabled = true; btn.textContent = "回信中…";
  try{
    await post("/api/runs/" + encodeURIComponent(runId) + "/qa", {question: q});
    const h = await jfetch("/api/runs/" + encodeURIComponent(runId) + "/qa");
    const list = $("#letters-list");
    const prev = list.querySelectorAll(".letter").length;
    list.innerHTML = (h.history || []).map(renderLetter).join("") || list.innerHTML;
    /* 只给新到的那封一次性入场（历史回放不重播）；历史追加在尾部，新信即最后一封 */
    const fresh = list.querySelectorAll(".letter");
    if(fresh.length > prev) fresh[fresh.length - 1].classList.add("enter");
    input.value = "";
    toast("编辑部已回信");
  }catch(e){
    if(OFFLINE){
      const list = $("#letters-list");
      if(list.querySelector("p")) list.innerHTML = "";
      list.insertAdjacentHTML("afterbegin", renderLetter({question: q, answer_html: "（演示模式）这里会显示只依据转写稿生成的回答，并附上可点击的章节定位与时间戳引文。"}));
      list.firstElementChild.classList.add("enter");
      input.value = "";
    }else{
      toast("提问失败：" + e.message);
    }
  }finally{
    asking = false; btn.disabled = false; btn.textContent = "寄出 →";
  }
}

/* ================= 目录（报告库） ================= */
const state = {page: 1, pages: 1, q: "", fav: false, full: false, scrollAfterLoad: false};

function tagOf(item){
  if(!item.has_report) return '<span class="tag gray">生成中</span>';
  if(item.favorite) return '<span class="tag">收藏</span>';
  return '<span class="tag gray">' + esc(structureZh(item.profile)) + '</span>';
}

function setPager(page, pages, total, enabled){
  $("#pg-pages").textContent = pages;
  $("#pg-total").textContent = total;
  const jump = $("#jump-page");
  jump.value = page;
  jump.max = pages;
  jump.disabled = !enabled;
}

async function loadRuns(page, silent){
  if(page) state.page = page;
  const list = $("#toc-list");
  if(!silent) list.innerHTML = '<li><p class="skeleton">正在翻检档案…</p></li>';

  const note = $("#search-note");
  if(state.full && state.q.trim().length >= 2){
    let res;
    try{ res = await jfetch("/api/search?q=" + encodeURIComponent(state.q.trim())); }
    catch(e){ if(!OFFLINE){ toast("检索失败：" + e.message); return; } res = null; }
    let results = res ? res.results : [];
    if(OFFLINE){
      note.style.display = "block";
      note.innerHTML = "演示模式 · 全文检索在启动本地服务后可用";
      list.innerHTML = SAMPLE.runs.map((r, i) => rowOf(r, i)).join("");
      $("#toc-hint").textContent = "共 " + SAMPLE.runs.length + " 期";
      setPager(1, 1, SAMPLE.runs.length, false);
      $("#prev-page").disabled = $("#next-page").disabled = true;
      return;
    }
    note.style.display = "block";
    note.innerHTML = results.length
      ? '在 <b>' + results.length + '</b> 期的转写稿里找到「' + esc(state.q.trim()) + '」'
      : '转写稿里没有找到「' + esc(state.q.trim()) + '」，试试更短的关键词';
    list.innerHTML = results.length ? results.map((r, i) =>
      '<li><a href="#" data-run="' + esc(r.run_id) + '">' +
        '<span class="no">' + String(i + 1).padStart(2, "0") + '</span>' +
        '<span class="t">' + esc(r.title || r.run_id) +
          (r.count != null ? '<small>命中 ' + r.count + ' 处 · ' + esc(r.uploader || "") + '</small>' : "") +
        '</span><span class="dots"></span>' +
        (r.snippet ? '<span class="dur" style="max-width:340px;white-space:normal">' + esc(r.snippet) + '</span>' : "") +
      '</a></li>').join("") :
      '<li><p class="skeleton">没有匹配的期数</p></li>';
    $("#toc-hint").textContent = "全文检索";
    setPager("", "—", "—", false);
    $("#prev-page").disabled = $("#next-page").disabled = true;
    $("#jump-page").disabled = true;
    bindToc();
    return;
  }
  note.style.display = "none";

  let r;
  try{
    r = await jfetch("/api/runs?page=" + state.page + "&page_size=10&q=" + encodeURIComponent(state.q) + (state.fav ? "&fav=1" : ""));
  }catch(e){
    if(!OFFLINE){ toast("读取目录失败：" + e.message); return; }
    let rows = SAMPLE.runs;
    if(state.fav) rows = rows.filter(x => x.favorite);
    if(state.q) rows = rows.filter(x => (x.title + x.uploader + x.bvid).toLowerCase().includes(state.q.toLowerCase()));
    list.innerHTML = rows.length ? rows.map((x, i) => rowOf(x, i)).join("") : '<li><p class="skeleton">没有匹配的期数</p></li>';
    $("#toc-hint").textContent = "演示数据";
    setPager(1, 1, rows.length, false);
    $("#prev-page").disabled = $("#next-page").disabled = true;
    $("#jump-page").disabled = true;
    bindToc();
    return;
  }
  const runs = r.runs || [];
  list.innerHTML = runs.length ? runs.map((x, i) => rowOf(x, i)).join("") :
    '<li><p class="skeleton">' + (state.fav ? "还没有收藏任何一期" : "报告库还是空的，先去「投印新篇」解析一个视频") + '</p></li>';
  $("#toc-hint").textContent = "共 " + fmtNum(r.total) + " 期";
  setPager(r.page, r.pages, fmtNum(r.total), true);
  $("#prev-page").disabled = r.page <= 1;
  $("#next-page").disabled = r.page >= r.pages;
  state.pages = r.pages;
  bindToc();
  if(state.scrollAfterLoad){
    state.scrollAfterLoad = false;
    /* 翻页后回到目录板块顶部，让新列表从头看起 */
    requestAnimationFrame(() => document.getElementById("toc").scrollIntoView({behavior: scrollBehavior(), block: "start"}));
  }
}

function rowOf(item, i){
  /* 期号跨页连续：第 N 页从 (N-1)*10+1 续编，而不是每页都从 01 重来 */
  const no = (state.page - 1) * 10 + i + 1;
  /* 正在作为「本期特稿」展示的那一期：行加标记类 + 实心徽标，方便一眼定位 */
  const isFeature = item.run_id === currentRunId;
  return '<li' + (isFeature ? ' class="feature-now"' : "") + '><a href="#" data-run="' + esc(item.run_id) + '">' +
    '<span class="no">' + String(no).padStart(2, "0") + '</span>' +
    '<span class="t">' + esc(item.title) + '<small>' + esc(item.uploader || "—") + ' · ' + esc(item.generated_at || "") + '</small></span>' +
    '<span class="dots"></span>' +
    '<span class="dur">' + esc(item.duration_text || "—") + '</span>' +
    (isFeature ? '<span class="tag hot" title="这一期正在下方特稿区展示">本期特稿</span>' : "") +
    tagOf(item) +
    '<button class="fav' + (item.favorite ? " on" : "") + '" data-fav-run="' + esc(item.run_id) + '" data-on="' + (item.favorite ? 1 : 0) + '" title="收藏">' + (item.favorite ? "★" : "☆") + '</button>' +
    '<button class="del" data-del-run="' + esc(item.run_id) + '" data-del-title="' + esc(item.title || item.run_id) + '" title="删除这一期">✕</button>' +
    '</a></li>';
}

/* 特稿切换后同步目录标记：只动现有 DOM（行类与徽标），不重新拉列表。
   openFeature 初次渲染时 loadRuns 可能尚未完成或已用旧 currentRunId 画过，
   统一在这里收口，保证目录里「本期特稿」永远指向当前展示的那一期。 */
function syncFeatureMarks(){
  document.querySelectorAll("#toc-list a[data-run]").forEach(a => {
    const isFeature = a.dataset.run === currentRunId;
    const li = a.closest("li");
    if(li) li.classList.toggle("feature-now", isFeature);
    const tag = a.querySelector(".tag.hot");
    if(isFeature && !tag){
      const fresh = document.createElement("span");
      fresh.className = "tag hot";
      fresh.title = "这一期正在下方特稿区展示";
      fresh.textContent = "本期特稿";
      const dots = a.querySelector(".dots");
      if(dots) dots.insertAdjacentElement("afterend", fresh);
      else a.appendChild(fresh);
    }else if(!isFeature && tag){
      tag.remove();
    }
  });
}

/* 删除退场动画，对应确认弹窗的文案「撕掉这一期？」，两条 WAAPI 串行（约 1.1s）：
   1) 0–0.40s  CSS .removing 类：3px 朱红横线从左向右划掉整行（勾销感），
               同时 fade 拍把整行褪成 35% 弱化态；
   2) 0.42–0.78s 撕落：整行逆时针倾斜 1.8°、向下脱落 8px、淡出——纸页被撕掉；
   3) 0.80–1.10s 收拢：行高与底线收到 0，后续行自然上移补位。
   拆成两条动画是因为单条多关键帧会把 transform/height 的插值摊满全时段
   （实测倾斜从 0.2s 就开始爬），串行执行每拍边界才干净。
   遵守 prefers-reduced-motion：命中时返回 null，调用方走瞬时重绘。 */
function collapseRow(row){
  if(!row || !row.animate || matchMedia("(prefers-reduced-motion: reduce)").matches) return null;
  row.style.overflow = "hidden";
  const link = row.querySelector("a");
  const ease = "cubic-bezier(.22,.61,.36,1)";
  const fade = row.animate(
    [{opacity: 1}, {opacity: .35}],
    {duration: 400, easing: ease, fill: "forwards"}
  );
  const tear = row.animate(
    [{opacity: .35, transform: "none"}, {opacity: 0, transform: "rotate(-1.8deg) translateY(8px)"}],
    {duration: 360, delay: 420, easing: ease, fill: "forwards"}
  );
  const collapse = row.animate(
    [
      {height: row.offsetHeight + "px", borderBottomColor: getComputedStyle(row).borderBottomColor},
      {height: "0px", borderBottomColor: "transparent"}
    ],
    {duration: 320, delay: 800, easing: ease, fill: "forwards"}
  );
  if(link){
    link.classList.add("removing");
    const unstyle = () => link.classList.remove("removing");
    fade.oncancel = unstyle;
    tear.oncancel = unstyle;
    collapse.oncancel = unstyle;
  }
  /* 返回带 finished/cancel 的薄封装：调用方 await finished、失败时 cancel 全部 */
  return {
    finished: Promise.all([fade.finished, tear.finished, collapse.finished]).then(() => {}),
    cancel(){
      for(const a of [fade, tear, collapse]) a.cancel();
    },
  };
}

function bindToc(){
  document.querySelectorAll("#toc-list a[data-run]").forEach(a => {
    a.addEventListener("click", e => {
      if(e.target.closest(".fav") || e.target.closest(".del")) return;
      e.preventDefault();
      openFeature(a.dataset.run);
      document.getElementById("feature").scrollIntoView({behavior: scrollBehavior()});
    });
  });
  document.querySelectorAll("#toc-list .fav").forEach(b => {
    b.addEventListener("click", async e => {
      e.preventDefault();
      const runId = b.dataset.favRun, on = b.dataset.on === "1";
      try{
        if(on) await del("/api/runs/" + encodeURIComponent(runId) + "/favorite");
        else await post("/api/runs/" + encodeURIComponent(runId) + "/favorite");
        b.dataset.on = on ? "0" : "1";
        b.textContent = on ? "☆" : "★";
        b.classList.toggle("on", !on);
        if(OFFLINE){ toast(on ? "已取消收藏（演示）" : "已收藏（演示）"); return; }
        toast(on ? "已取消收藏" : "已收藏");
        if(currentRunId === runId) openFeature(runId);
        loadRuns(null, true);
      }catch(err){ toast("操作失败：" + err.message); }
    });
  });
  document.querySelectorAll("#toc-list .del").forEach(b => {
    b.addEventListener("click", async e => {
      e.preventDefault();
      const runId = b.dataset.delRun;
      const title = b.dataset.delTitle || runId;
      if(OFFLINE){ toast("离线演示模式，暂不支持删除"); return; }
      const ok = await askDelete("run", title);
      if(!ok) return;
      const fade = collapseRow(b.closest("li"));
      try{
        /* 接口与退场动画并行：网络再快动画也完整走完，慢了也不额外等待 */
        await Promise.all([del("/api/runs/" + encodeURIComponent(runId)), fade ? fade.finished : Promise.resolve()]);
        toast("已删除：" + title);
        if(currentRunId === runId){
          currentRunId = null;
          try{
            const r = await jfetch("/api/runs?page=1&page_size=1");
            if(r.runs && r.runs[0]) openFeature(r.runs[0].run_id);
          }catch(err2){}
        }
        loadRuns(null, true);
      }catch(err){
        /* 删除失败：撤销动画，行原样恢复 */
        if(fade) fade.cancel();
        toast("删除失败：" + err.message);
      }
    });
  });
}

/* ================= 投印（提交任务） ================= */
let mode = "standard";

document.querySelectorAll("#modes .mode[data-mode]").forEach(b => {
  /* 只绑模式钮：语言下拉钮同为 .mode 但无 data-mode，误绑会覆盖提交模式 */
  b.addEventListener("click", () => {
    document.querySelectorAll("#modes .mode").forEach(x => x.classList.remove("on"));
    b.classList.add("on");
    mode = b.dataset.mode;
  });
});

let probeTimer = null;
$("#url").addEventListener("blur", e => {
  /* 焦点移入预检行（如点分P 下拉）时不预检，避免下拉被重绘打断 */
  if(e.relatedTarget && e.relatedTarget.closest && e.relatedTarget.closest("#probeline")) return;
  doProbe();
});
$("#url").addEventListener("input", () => {
  clearTimeout(probeTimer);
  probeTimer = setTimeout(doProbe, 900);
});

let probePages = [];   /* 预检拿到的分P列表（多P 视频才有值） */
let probeBlocked = false; /* 预检判定为付费内容（充电专属/付费课程）时禁用投递 */
let pageSelKey = null; /* 分P 列表对应的输入源，换源即失效 */

let selPageNum = null; /* 当前选中的分P（null = P1/未选） */
function selectedPage(){ return selPageNum || 1; }

/* 分P 自绘下拉：触发钮 + 浮层（样式与搜索历史下拉同源） */
function renderPPick(){
  const cur = probePages.find(x => x.page === selPageNum) || probePages[0];
  const btn = '<button type="button" id="page-btn" class="ppick-btn" aria-haspopup="listbox" aria-expanded="false">' +
    'P' + cur.page + ' · ' + esc(cur.duration_text) + ' ▾</button>';
  const items = probePages.map(it =>
    '<button type="button" class="pp-item' + (it.page === selPageNum ? " on" : "") + '" data-page="' + it.page + '">' +
    '<span class="pp-no">P' + it.page + '</span>' +
    '<span class="pp-t">' + esc((it.title || "").slice(0, 30)) + '</span>' +
    '<span class="pp-d">' + esc(it.duration_text) + '</span></button>').join("");
  return btn + '<span class="ppick" id="page-pick">' + items + '</span>';
}
function wirePPick(){
  const btn = document.getElementById("page-btn");
  const pick = document.getElementById("page-pick");
  if(!btn || !pick) return;
  btn.addEventListener("click", e => {
    e.stopPropagation();
    pick.classList.toggle("open");
    btn.setAttribute("aria-expanded", pick.classList.contains("open") ? "true" : "false");
  });
  pick.querySelectorAll(".pp-item").forEach(it => it.addEventListener("click", () => {
    selPageNum = parseInt(it.dataset.page, 10) || 1;
    const item = probePages.find(x => x.page === selPageNum) || probePages[0];
    btn.innerHTML = "P" + item.page + " · " + esc(item.duration_text) + ' ▾';
    pick.querySelectorAll(".pp-item").forEach(x => x.classList.toggle("on", Number(x.dataset.page) === selPageNum));
    pick.classList.remove("open");
    btn.setAttribute("aria-expanded", "false");
  }));
}
document.addEventListener("click", e => {
  const pick = document.getElementById("page-pick");
  if(pick && pick.classList.contains("open") && !e.target.closest(".ppick-wrap")) pick.classList.remove("open");
  const langPick = document.getElementById("lang-pick");
  if(langPick && langPick.classList.contains("open") && !e.target.closest(".lang-wrap")){
    langPick.classList.remove("open");
    document.getElementById("lang-btn").setAttribute("aria-expanded", "false");
  }
});

/* 语言自绘下拉：触发钮复用 .mode 胶囊（与模式按钮同一套样式），浮层同分P 选择器 */
(function wireLangPick(){
  const btn = document.getElementById("lang-btn");
  const pick = document.getElementById("lang-pick");
  if(!btn || !pick) return;
  btn.addEventListener("click", e => {
    e.stopPropagation();
    const open = pick.classList.toggle("open");
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  });
  pick.querySelectorAll(".lang-item").forEach(it => it.addEventListener("click", () => {
    pick.querySelectorAll(".lang-item").forEach(x => x.classList.toggle("on", x === it));
    btn.dataset.lang = it.dataset.lang;
    btn.dataset.label = it.dataset.label;
    btn.textContent = "语言 · " + it.dataset.label;
    pick.classList.remove("open");
    btn.setAttribute("aria-expanded", "false");
  }));
})();

/* 把所选分P 写进提交链接：P1 保持裸链接（与裸 BV 提交同目录），
   其他分P 追加 ?p=N；已有 p 参数则替换。裸 BV 号先补全成标准链接。 */
function withPage(source, page){
  const url = /^BV[0-9A-Za-z]{10}$/i.test(source)
    ? "https://www.bilibili.com/video/" + source
    : source;
  const queryAt = url.indexOf("?");
  if(queryAt < 0) return page > 1 ? url + "?p=" + page : url;
  const base = url.slice(0, queryAt);
  const params = url.slice(queryAt + 1).split("&").filter(pair => pair.split("=")[0] !== "p");
  if(page > 1) params.push("p=" + page);
  return params.length ? base + "?" + params.join("&") : base;
}

let lastProbedUrl = null; /* 已预检过的输入：blur 重复触发同一输入时直接跳过 */

async function doProbe(){
  const url = $("#url").value.trim();
  if(url === lastProbedUrl) return; /* 同一输入不重复预检（点分P 下拉触发 blur 的场景） */
  const line = $("#probeline");
  if(!url){
    probePages = []; pageSelKey = null; lastProbedUrl = null; probeBlocked = false;
    $("#btn-submit").disabled = false; line.innerHTML = ""; return;
  }
  line.innerHTML = "预检中…";
  try{
    const p = await post("/api/probe", {url});
    lastProbedUrl = url;
    /* 付费内容：禁用投递按钮，提示暂不支持（流水线侧同样会拒绝） */
    if(p.paywalled){
      probeBlocked = true;
      $("#btn-submit").disabled = true;
      line.innerHTML = '<span class="warn">' + esc(p.message) + '</span>';
      return;
    }
    probeBlocked = false;
    $("#btn-submit").disabled = false;
    /* 同一输入的重复预检保留用户已选的分P */
    const keepPage = (pageSelKey === url) ? selectedPage() : null;
    let html = "预检 · <b>" + esc(p.title || "未知标题") + "</b>";
    if(p.pages && p.pages.length > 1){
      /* 多P：时长/超长判定跟随所选分P（选项里带各P时长），不显示整包时长 */
      probePages = p.pages; pageSelKey = url;
      selPageNum = keepPage || (p.current_page > 1 ? p.current_page : 1);
      html += ' · 分P <span class="ppick-wrap">' + renderPPick() + '</span>';
    } else {
      probePages = []; pageSelKey = url; selPageNum = null;
      html += " · 时长 <b>" + esc(p.duration_text || "—") + "</b>";
    }
    if(p.uploader) html += " · " + esc(p.uploader);
    if(p.too_long) html += ' · <span class="warn">超出 ' + esc(p.limit_text || "") + ' 上限，将被拒绝</span>';
    if(p.existing_run) html += ' · <span class="warn">已解析过（' + esc(p.existing_run.generated_at) + '）</span> · <a href="' + p.existing_run.report_url + '" target="_blank" rel="noopener">查看上次报告</a>';
    line.innerHTML = html;
    wirePPick();
  }catch(e){
    probePages = []; pageSelKey = null; /* 预检失败放行原文，过期分P一并失效 */
    lastProbedUrl = url; probeBlocked = false;
    $("#btn-submit").disabled = false;
    /* 付费内容类拒绝是确定性的（流水线会再次拦截），不追加「仍可提交」 */
    const rejected = String(e.message || "").indexOf("已拒绝解析") >= 0;
    line.innerHTML = OFFLINE ? "演示模式 · 预检在启动本地服务后可用"
      : rejected ? '<span class="warn">' + esc(e.message) + '</span>'
      : '<span class="warn">预检失败：' + esc(e.message) + '（仍可提交）</span>';
  }
}

let stepperSeen = new Set();
function renderStepper(current, doneSet){
  const ul = $("#stepper");
  ul.innerHTML = STAGES.map((name, i) => {
    const no = String(i + 1).padStart(2, "0");
    if(doneSet.has(i)) {
      /* 只对本次新完成的阶段弹一次 ✓，重绘不重播 */
      const pop = !stepperSeen.has(i) && stepperSeen.size > 0 ? " pop" : "";
      return '<li class="done' + pop + '"><span class="no">' + no + '</span><span class="t">' + name + '</span><span class="mark">✓</span></li>';
    }
    if(i === current) return '<li class="cur"><span class="no">' + no + '</span><span class="t">' + name + '</span><span class="mark">●</span></li>';
    return '<li><span class="no">' + no + '</span><span class="t">' + name + '</span><span class="mark">·</span></li>';
  }).join("");
  stepperSeen = new Set(doneSet);
}

async function submitJob(){
  const url = $("#url").value.trim();
  if(!url){ toast("先填一个 B 站链接或 BV 号"); return; }
  if(probeBlocked){ toast("这类付费视频暂不支持解析"); return; }
  const btn = $("#btn-submit");
  btn.disabled = true; btn.textContent = "排片中…";
  $("#logbox").style.display = "block";
  $("#logbox").textContent = "";
  $("#submeta").innerHTML = "";
  const doneSet = new Set();
  renderStepper(-1, doneSet);
  try{
    const frames = $("#frames-switch") && $("#frames-switch").checked ? true : null;
    const lang = ($("#lang-btn") || {}).dataset ? ($("#lang-btn").dataset.lang || null) : null;
    /* 投印策略固定：忽略缓存强制重跑、不留音频文件、转写引擎按 .env 配置（不传即"自动"） */
    /* 多P 视频：仅当分P 列表仍对应当前输入时才改写链接，避免过期分P 误选 */
    const target = (probePages.length && pageSelKey === url) ? withPage(url, selectedPage()) : url;
    const job = await post("/api/jobs", {url: target, mode, frames, lang, no_cache: true, keep_audio: false});
    localStorage.setItem("videread.activeJob", job.job_id);
    /* 提交成功即跳工单页：步进器、排印日志与取消操作都在那边完整呈现 */
    location.href = "/editorial/job/" + encodeURIComponent(job.job_id);
  }catch(e){
    renderStepper(-1, doneSet);
    $("#submeta").innerHTML = '<span style="color:var(--accent)">' + (OFFLINE ? "演示模式 · 提交任务在启动本地服务后可用" : "提交失败：" + esc(e.message)) + '</span>';
    toast(OFFLINE ? "离线演示模式，暂不支持提交" : "提交失败：" + e.message);
    btn.disabled = false; btn.textContent = "投印 →";
  }
}
$("#btn-submit").addEventListener("click", submitJob);
$("#url").addEventListener("keydown", e => { if(e.key === "Enter") submitJob(); });

/* 行内改名：标题原地变输入框，回车保存 / Esc 或失焦取消 */
function startRename(runId){
  const h2 = document.querySelector("#feature-body .article h2");
  if(!h2 || h2.dataset.renaming) return;
  const current = h2.textContent;
  h2.dataset.renaming = "1";
  const input = document.createElement("input");
  input.className = "rename-input";
  input.value = current;
  input.maxLength = 80;
  h2.innerHTML = "";
  h2.appendChild(input);
  input.focus(); input.select();
  let settled = false;
  const finish = save => {
    if(settled) return;
    settled = true;
    delete h2.dataset.renaming;
    const val = input.value.trim();
    if(!save || !val || val === current){ h2.textContent = current; return; }
    h2.textContent = val; /* 乐观更新，失败回滚 */
    post("/api/runs/" + encodeURIComponent(runId) + "/title", {title: val})
      .then(() => { toast("已改名"); loadRuns(state.page); })
      .catch(err => { h2.textContent = current; toast("改名失败：" + err.message); });
  };
  input.addEventListener("keydown", e => {
    if(e.key === "Enter"){ e.preventDefault(); finish(true); }
    if(e.key === "Escape"){ e.preventDefault(); finish(false); }
  });
  input.addEventListener("blur", () => finish(false));
}

/* ================= 印务统计 ================= */
async function loadStats(){
  const body = $("#stats-body");
  let days;
  try{
    const r = await jfetch("/api/stats");
    days = r.days || [];
  }catch(e){
    if(!OFFLINE){ body.innerHTML = '<p class="skeleton">统计读取失败</p>'; return; }
    days = SAMPLE.statsDays;
  }
  const recent = days.slice(-7).reverse();
  if(!recent.length){
    body.innerHTML = '<p class="skeleton">还没有台账记录，投印第一期后这里会出现按日汇总。</p>';
    return;
  }
  body.innerHTML =
    '<table class="stats-table"><thead><tr>' +
    '<th>日期</th><th>期数</th><th>总耗时</th><th>TOKEN</th></tr></thead><tbody>' +
    recent.map(d =>
      '<tr><td><span class="d">' + esc(d.date) + '</span></td>' +
      '<td class="num">' + d.runs + '</td>' +
      '<td class="num">' + fmtDur(d.total_ms || 0) + '</td>' +
      '<td class="num">' + fmtNum(d.tokens || 0) + '</td></tr>').join("") +
    '</tbody></table>';
}

/* ================= 翻阅全文（新标签页打开） ================= */
function openReaderPage(runId, sec){
  const u = API + "/read/" + encodeURIComponent(runId) + (sec ? "?sec=" + sec : "");
  window.open(u, "_blank");
}

/* ================= 搜索历史（localStorage + 自定义下拉） ================= */
const HIST_KEY = "videread.searchHistory";
const HIST_MAX = 8;

function loadHist(){
  try{
    const v = JSON.parse(localStorage.getItem(HIST_KEY) || "[]");
    return Array.isArray(v) ? v.slice(0, HIST_MAX) : [];
  }catch(e){ return []; }
}
function saveHist(q){
  q = (q || "").trim();
  if(q.length < 2) return;
  const list = loadHist().filter(x => x.q !== q);
  list.unshift({q: q, t: new Date().toISOString().slice(0, 10)});
  try{ localStorage.setItem(HIST_KEY, JSON.stringify(list.slice(0, HIST_MAX))); }catch(e){}
}
function renderHist(){
  const box = $("#hist");
  const list = loadHist();
  box.innerHTML =
    '<div class="h-head"><span>RECENT · 最近搜索</span>' +
    (list.length ? '<button id="hist-clear">清空</button>' : "") + '</div>' +
    (list.length
      ? '<ul>' + list.map((x, i) =>
          '<li class="h-row">' +
          '<button class="h-item" data-i="' + i + '">' +
          '<span>' + esc(x.q) + '</span><span class="h-time">' + esc(x.t || "") + '</span>' +
          '</button>' +
          '<button class="h-del" data-i="' + i + '" title="删除这条记录">✕</button>' +
          '</li>').join("") + '</ul>'
      : '<p style="padding:12px 14px;font-size:12.5px;color:var(--faint);margin:0;">暂无搜索记录</p>');
  box.classList.add("open");
  box.setAttribute("aria-hidden", "false");
  box.querySelectorAll(".h-item").forEach(b => b.addEventListener("click", () => {
    const list = loadHist();
    $("#q").value = list[Number(b.dataset.i)].q;
    state.q = $("#q").value;
    closeHist();
    loadRuns(1);
  }));
  box.querySelectorAll(".h-del").forEach(b => b.addEventListener("click", e => {
    e.stopPropagation();
    const list = loadHist();
    list.splice(Number(b.dataset.i), 1);
    try{ localStorage.setItem(HIST_KEY, JSON.stringify(list)); }catch(err){}
    renderHist(); /* 重渲染保持面板展开 */
  }));
  const clear = $("#hist-clear");
  if(clear) clear.addEventListener("click", () => {
    try{ localStorage.removeItem(HIST_KEY); }catch(e){}
    renderHist();
  });
}
function closeHist(){
  $("#hist").classList.remove("open");
  $("#hist").setAttribute("aria-hidden", "true");
}
$("#q").addEventListener("focus", renderHist);
$("#q").addEventListener("keydown", e => { if(e.key === "Escape") closeHist(); });
document.addEventListener("click", e => {
  if(!e.target.closest(".qwrap")) closeHist();
});

/* ================= 其它 ================= */
let qTimer = null;
let qComposing = false;
$("#q").addEventListener("compositionstart", () => { qComposing = true; });
$("#q").addEventListener("compositionend", () => {
  /* 拼音上屏才算一次真正的输入：立即搜索并写入历史 */
  qComposing = false;
  state.q = $("#q").value;
  saveHist(state.q);
  loadRuns(1);
});
$("#q").addEventListener("input", e => {
  /* 输入法组合中（拼音未上屏）跳过，避免 s'c 这类中间串混进历史 */
  if(qComposing || e.isComposing) return;
  clearTimeout(qTimer);
  qTimer = setTimeout(() => {
    state.q = $("#q").value;
    saveHist(state.q);
    loadRuns(1);
  }, 350);
});
$("#fav-switch").addEventListener("change", e => { state.fav = e.target.checked; loadRuns(1); });
$("#full-switch").addEventListener("change", e => {
  state.full = e.target.checked;
  $("#q").placeholder = state.full ? "在所有转写稿里搜关键词（至少 2 个字）…" : "按标题 / UP主 / BV 号过滤，或切到「全文」搜转写稿…";
  loadRuns(1);
});
$("#prev-page").addEventListener("click", () => { state.scrollAfterLoad = true; loadRuns(state.page - 1); });
$("#next-page").addEventListener("click", () => { state.scrollAfterLoad = true; loadRuns(state.page + 1); });

/* 跳页输入框：回车执行，change 时夹回有效范围 */
$("#jump-page").addEventListener("keydown", e => {
  if(e.key !== "Enter") return;
  const v = parseInt(e.target.value, 10);
  if(!Number.isFinite(v)) return;
  e.target.blur();
  state.scrollAfterLoad = true;
  loadRuns(Math.min(Math.max(1, v), state.pages));
});
$("#jump-page").addEventListener("change", e => {
  const v = parseInt(e.target.value, 10);
  if(Number.isFinite(v)) e.target.value = Math.min(Math.max(1, v), state.pages);
});

let progTicking = false;
function updateProgress(){
  const h = document.documentElement;
  $("#progress").style.width = (h.scrollTop / (h.scrollHeight - h.clientHeight) * 100) + "%";
  progTicking = false;
}
addEventListener("scroll", () => {
  if(progTicking) return;
  progTicking = true;
  requestAnimationFrame(updateProgress);
}, {passive: true});

/* ================= 右侧悬浮目录（scrollspy） ================= */
const spyIds = ["submit", "feature", "toc", "stats"];
const spyEls = spyIds.map(id => document.getElementById(id));
function updateSpy(){
  const doc = document.documentElement;
  let cur = "";
  const pos = scrollY + innerHeight * 0.35;
  spyEls.forEach(s => { if(s && s.offsetTop <= pos) cur = s.id; });
  /* 页面已滚到底时，最后一个区块可能够不到判定线（下方空间不足），
     直接点亮末项，保证「印务统计」可高亮可定位。 */
  if(scrollY + innerHeight >= doc.scrollHeight - 4) cur = spyIds[spyIds.length - 1];
  document.querySelectorAll(".side-nav a[data-spy]").forEach(a =>
    a.classList.toggle("on", a.dataset.spy === cur));
}
addEventListener("scroll", updateSpy, {passive: true});
addEventListener("resize", updateSpy);
updateSpy();

const io = new IntersectionObserver(es => {
  es.forEach(e => { if(e.isIntersecting){ e.target.classList.add("in"); io.unobserve(e.target); } });
}, {threshold: .1});
document.querySelectorAll(".reveal").forEach(el => io.observe(el));

function refreshAll(){
  loadHero(); loadRuns(1, true); loadStats();
  if(currentRunId) openFeature(currentRunId);
  updateConfFootnote();
}

/* 页脚版权页的转写/执笔两行（模型设置保存后也要即时刷新） */
function updateConfFootnote(){
  jfetch("/api/config").then(c => {
    const asr = $("#conf-asr");
    if(asr) asr.textContent = (c.asr_backend || "—") + (c.asr_model ? " · " + c.asr_model : "");
    const llm = $("#conf-llm");
    if(llm){
      const models = Array.from(new Set([c.llm_model_plan, c.llm_model_write].filter(Boolean)));
      llm.textContent = models.length ? models.join(" / ") : "—";
    }
  }).catch(() => {
    const asr = $("#conf-asr"), llm = $("#conf-llm");
    if(asr) asr.textContent = "—";
    if(llm) llm.textContent = "—";
  });
}

/* 有进行中的工单时，在投印区给一个进入工单页的入口（终态/不存在则清标记） */
function showJobline(id){
  $("#jobline").innerHTML = '投印中 · 工单 <b>' + esc(id) + '</b> · <a href="/editorial/job/' + encodeURIComponent(id) + '" target="_blank" rel="noopener">进入工单页盯进度 →</a>';
}
function clearJobline(){
  localStorage.removeItem("videread.activeJob");
  $("#jobline").innerHTML = "";
}
/* 提交后留在主页的代价是主页不知道工单何时终态——15s 轻量轮询兜底：
   终态即清行，完成时 toast + 刷新目录（工单页才有的实时反馈，这里只要个结果） */
let jobPoll = null;
function watchActiveJob(){
  if(jobPoll) return;
  jobPoll = setInterval(async () => {
    const id = localStorage.getItem("videread.activeJob");
    if(!id){ clearInterval(jobPoll); jobPoll = null; $("#jobline").innerHTML = ""; return; }
    try{
      const job = await jfetch("/api/jobs/" + encodeURIComponent(id));
      if(JOB_TERMINAL.has(job.state)){
        clearJobline();
        clearInterval(jobPoll); jobPoll = null;
        if(job.state === "done"){ toast("新的一期印好了"); loadRuns(1, true); }
      }
    }catch(e){ /* 服务暂时下线等：保留现状，下轮再看 */ }
  }, 15000);
}
async function checkActiveJob(){
  const id = localStorage.getItem("videread.activeJob");
  if(!id) return;
  try{
    const job = await jfetch("/api/jobs/" + encodeURIComponent(id));
    if(JOB_TERMINAL.has(job.state)){
      localStorage.removeItem("videread.activeJob");
      return;
    }
    showJobline(id);
    watchActiveJob();
  }catch(err){
    /* 404 = 工单确实没了才清标记；服务暂时下线时保留 */
    if(String(err.message) === "404") localStorage.removeItem("videread.activeJob");
  }
}

/* ================= 模型设置（自定义 API Key / 模型） ================= */
const LLM_PRESETS = {
  deepseek:   {base_url: "https://api.deepseek.com/v1", plan: "deepseek-chat", write: "deepseek-chat"},
  dashscope:  {base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1", plan: "qwen-plus", write: "qwen-max"},
  moonshot:   {base_url: "https://api.moonshot.cn/v1", plan: "moonshot-v1-8k", write: "moonshot-v1-32k"},
  openai:     {base_url: "https://api.openai.com/v1", plan: "gpt-4o-mini", write: "gpt-4o"},
  openrouter: {base_url: "https://openrouter.ai/api/v1", plan: "openai/gpt-4o-mini", write: "openai/gpt-4o"},
  ollama:     {base_url: "http://127.0.0.1:11434/v1", plan: "qwen2.5", write: "qwen2.5"}
};

function openSettings(){
  if(OFFLINE){ toast("离线演示模式，暂不支持设置"); return; }
  $("#settings-veil").classList.add("open");
  $("#settings-veil").setAttribute("aria-hidden", "false");
  setStNote("");
  syncGeneralPane(); /* 通用页签：主题与投印默认值随开随显 */
  jfetch("/api/settings").then(s => {
    stEffectiveBaseUrl = s.base_url || "";
    $("#st-base-url").value = s.base_url || "";
    $("#st-model-plan").value = s.model_plan || "";
    $("#st-model-write").value = s.model_write || "";
    const keyInput = $("#st-api-key");
    keyInput.value = "";
    keyInput.placeholder = s.has_key
      ? (s.source === "custom" ? "已保存 " + s.api_key_masked + "（留空保持不变）" : ".env 中的 Key " + s.api_key_masked + "（留空保持不变）")
      : "尚未设置，报告写作需要 Key";
    /* 当前端点能对上预设就选中它（用户只见 Key 输入框）；
       对不上说明是自定义网关，才露出 Base URL 一栏 */
    const preset = detectPreset(s.base_url || "");
    $("#st-preset").value = preset;
    setBaseVisible(preset === "custom");
    fetchModels(); /* 已保存过 Key 时，打开即带出可选模型 */
  }).catch(e => { closeSettings(); toast("读取设置失败：" + e.message); });
}
function closeSettings(){
  const veil = $("#settings-veil");
  if(!veil.classList.contains("open")) return;
  veil.classList.remove("open");
  veil.setAttribute("aria-hidden", "true");
}
function setStNote(html, ok){
  const note = $("#st-note");
  note.innerHTML = html;
  note.classList.toggle("ok", ok === true);
  note.classList.toggle("bad", ok === false);
}
function settingsForm(){
  return {
    api_key: $("#st-api-key").value.trim(),
    base_url: $("#st-base-url").value.trim(),
    model_plan: $("#st-model-plan").value.trim(),
    model_write: $("#st-model-write").value.trim()
  };
}

/* 按当前 Key / 网关拉取可用模型列表；模型输入框点开即出下拉面板 */
let stModelCache = [];      /* 最近一次拉到的模型 id */
let stModelsSeq = 0;        /* 请求序号：快速切换厂商时丢弃过期响应 */
let stSuggestFor = null;    /* 当前展开建议面板的输入框 id */
let stEffectiveBaseUrl = "";/* 打开弹窗时的生效端点（判断回退 Key 是否匹配） */
let stTypedQuery = false;   /* 本次聚焦后用户是否输入过；没输入过则展开全部、不过滤 */

function fetchModels(){
  const form = settingsForm();
  if(!form.base_url) return;
  /* 没填 Key 且端点已换：回退的旧 Key 对新厂商必然失败，别浪费这次请求 */
  if(!form.api_key && form.base_url !== stEffectiveBaseUrl){
    setStNote("填好 API Key 后会自动拉取该服务的模型列表");
    return;
  }
  const seq = ++stModelsSeq;
  setStNote("正在拉取可用模型列表…");
  post("/api/settings/models", {api_key: form.api_key, base_url: form.base_url}).then(r => {
    if(seq !== stModelsSeq) return;
    stModelCache = r.models || [];
    setStNote("拉到 " + stModelCache.length + " 个可用模型：点模型栏直接选择，也可手动输入", true);
    applySuggest();
  }).catch(e => {
    if(seq !== stModelsSeq) return;
    stModelCache = [];
    applySuggest();
    setStNote("模型列表拉取失败（不影响保存，可手动填模型名）：" + esc(e.message), false);
  });
}
$("#st-api-key").addEventListener("blur", fetchModels);
$("#st-base-url").addEventListener("change", fetchModels);

/* Key 输入框显示/隐藏；不支持 CSS 遮罩的浏览器（Firefox）退回 type=password */
if(!CSS.supports("-webkit-text-security", "disc")){
  $("#st-api-key").type = "password";
}
$("#st-key-toggle").addEventListener("click", () => {
  const input = $("#st-api-key");
  const masked = input.classList.toggle("st-key-masked");
  $("#st-key-toggle").textContent = masked ? "显示" : "隐藏";
});

function applySuggest(){
  /* 渲染建议面板。过滤只对「用户本次聚焦后输入的内容」生效：
     打开面板时输入框里的值多半是预设/已保存的模型名，拿它过滤会把
     刚拉到的列表滤成空（面板看起来「什么都没有」）——所以未输入前
     一律展开全部，输入后才按内容过滤 */
  document.querySelectorAll(".st-suggest").forEach(panel => {
    const input = $("#" + panel.dataset.target);
    if(!input) return;
    const q = stTypedQuery ? input.value.trim().toLowerCase() : "";
    const hits = stModelCache.filter(m => !q || m.toLowerCase().includes(q)).slice(0, 200);
    panel.innerHTML = hits.length
      ? hits.map(m => '<button type="button" class="st-opt" data-v="' + esc(m) + '">' + esc(m) + '</button>').join("")
      : '<div class="st-empty">' + (stModelCache.length ? "无匹配项" : "（未拉到模型列表，手动输入即可）") + '</div>';
    panel.style.display = (stSuggestFor === input.id) ? "block" : "none";
  });
}
function openSuggest(input){
  stSuggestFor = input.id;
  stTypedQuery = false; /* 重新展开 = 回到「显示全部」状态 */
  if(!stModelCache.length) fetchModels(); /* 缓存为空则现场拉取 */
  applySuggest();
}
function closeSuggest(){
  stSuggestFor = null;
  applySuggest();
}

["st-model-plan", "st-model-write"].forEach(id => {
  const input = document.getElementById(id);
  /* mousedown 先于 focus：已聚焦时再点击也能重新展开面板 */
  input.addEventListener("mousedown", () => {
    if(stSuggestFor !== input.id) openSuggest(input);
  });
  input.addEventListener("focus", () => { input.select(); openSuggest(input); });
  input.addEventListener("input", () => { stTypedQuery = true; applySuggest(); });
  /* blur 先于选项 click 触发，延迟收起让点击能落上；
     收起前确认面板没有被另一个输入框重新打开（切栏时旧定时器会迟到） */
  input.addEventListener("blur", () => {
    const from = id;
    setTimeout(() => { if(stSuggestFor === from) closeSuggest(); }, 150);
  });
});
/* mousedown 抢在 blur 之前完成选中；preventDefault 避免焦点闪烁 */
document.addEventListener("mousedown", e => {
  const opt = e.target.closest(".st-opt");
  if(!opt) return;
  e.preventDefault();
  const panel = opt.closest(".st-suggest");
  const input = $("#" + panel.dataset.target);
  if(input){
    input.value = opt.dataset.v;
    input.dispatchEvent(new Event("input"));
  }
  closeSuggest();
});

function detectPreset(base){
  /* 当前 Base URL 对上哪家预设就显示哪家；对不上 = 自定义网关 */
  for(const [key, p] of Object.entries(LLM_PRESETS)){
    if(p.base_url === base) return key;
  }
  return "custom";
}
function setBaseVisible(show){
  $("#st-base-field").style.display = show ? "" : "none";
}

$("#nav-settings").addEventListener("click", e => { e.preventDefault(); openSettings(); });
/* 同 #veil：按下在弹窗内、拖出遮罩松开的 click 不应关闭（详见上方注释） */
let stVeilPressed = false;
$("#settings-veil").addEventListener("mousedown", e => { stVeilPressed = (e.target === e.currentTarget); });
$("#settings-veil").addEventListener("click", e => {
  if(e.target.id === "settings-veil" && stVeilPressed) closeSettings();
  stVeilPressed = false;
});
addEventListener("keydown", e => { if(e.key === "Escape") closeSettings(); });

$("#st-preset").addEventListener("change", e => {
  const v = e.target.value;
  const p = LLM_PRESETS[v];
  if(p){
    /* 选预设 = Base URL 与模型名自动带出，用户只填 Key */
    $("#st-base-url").value = p.base_url;
    $("#st-model-plan").value = p.plan;
    $("#st-model-write").value = p.write;
    setBaseVisible(false);
    fetchModels(); /* 换厂商后按新端点刷新模型列表 */
  }else{
    /* 自定义网关或未选：露出 Base URL 手动填 */
    setBaseVisible(true);
  }
});

$("#st-save").addEventListener("click", async () => {
  const btn = $("#st-save");
  const form = settingsForm();
  if(!form.base_url){ setStNote("Base URL 不能为空", false); return; }
  btn.disabled = true;
  try{
    await post("/api/settings", form);
    setStNote("已保存，之后投印的报告都会用这套模型", true);
    toast("模型设置已保存");
    updateConfFootnote();
    setTimeout(closeSettings, 700);
  }catch(e){
    setStNote("保存失败：" + esc(e.message), false);
  }finally{
    btn.disabled = false;
  }
});

$("#st-test").addEventListener("click", async () => {
  const btn = $("#st-test");
  btn.disabled = true; btn.textContent = "测试中…";
  setStNote("正在向 " + esc($("#st-base-url").value.trim() || "当前网关") + " 发送测试请求…");
  try{
    const r = await post("/api/settings/test", settingsForm());
    setStNote(r.note
      ? "连接成功 · " + esc(r.model) + "：" + esc(r.note)
      : "连接成功 · " + esc(r.model) + " 回复：" + esc(r.reply || "（空）"), true);
  }catch(e){
    setStNote("连接失败：" + esc(e.message), false);
  }finally{
    btn.disabled = false; btn.textContent = "测试连接";
  }
});

$("#st-reset").addEventListener("click", async () => {
  try{
    await del("/api/settings");
    toast("已恢复默认（.env / 内置配置）");
    updateConfFootnote();
    openSettings(); /* 重新拉取回填 */
  }catch(e){
    setStNote("恢复失败：" + esc(e.message), false);
  }
});

/* ================= 设置弹窗：页签 ================= */
document.querySelectorAll(".st-tab").forEach(tab => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".st-tab").forEach(x => x.classList.toggle("on", x === tab));
    document.querySelectorAll(".st-pane").forEach(p => p.classList.toggle("on", p.id === "st-pane-" + tab.dataset.tab));
  });
});

/* ================= 设置 · 通用页签（主题 + 投印默认值） ================= */
/* 投印默认值存 localStorage，打开页面时套用到投印表单；表单里仍可按次覆盖 */
function storedDefaults(){
  try{ return JSON.parse(localStorage.getItem("videread.defaults") || "{}") || {}; }
  catch(e){ return {}; }
}

function applyDefaultsToForm(){
  const d = storedDefaults();
  if(d.mode && document.querySelector('#modes .mode[data-mode="' + d.mode + '"]')){
    document.querySelectorAll("#modes .mode").forEach(x => x.classList.remove("on"));
    document.querySelector('#modes .mode[data-mode="' + d.mode + '"]').classList.add("on");
    mode = d.mode;
  }
  if(typeof d.lang === "string"){
    const btn = $("#lang-btn"), pick = $("#lang-pick");
    /* 历史默认值里的空串（旧「跟随配置」）统一视作中文 */
    const item = pick ? pick.querySelector('.lang-item[data-lang="' + (d.lang || "zh") + '"]') : null;
    if(btn && item){
      pick.querySelectorAll(".lang-item").forEach(x => x.classList.toggle("on", x === item));
      btn.dataset.lang = item.dataset.lang;
      btn.dataset.label = item.dataset.label;
      btn.textContent = "语言 · " + item.dataset.label;
    }
  }
  if(typeof d.frames === "boolean"){
    const fs = $("#frames-switch");
    if(fs) fs.checked = d.frames;
  }
}

function syncThemeSeg(){
  const t = (window.videreadTheme ? window.videreadTheme.get() : document.documentElement.dataset.theme) || "light";
  document.querySelectorAll("#st-theme-seg .mode").forEach(x => x.classList.toggle("on", x.dataset.theme === t));
}

function syncGeneralPane(){
  syncThemeSeg();
  const d = storedDefaults();
  document.querySelectorAll("#st-mode-seg .mode").forEach(x => x.classList.toggle("on", (x.dataset.dmode || "") === (d.mode || "standard")));
  document.querySelectorAll("#st-lang-seg .mode").forEach(x => x.classList.toggle("on", (x.dataset.dlang || "") === (d.lang || "zh")));
  const fs = $("#st-def-frames");
  if(fs) fs.checked = !!d.frames;
  const note = $("#st-gen-note");
  if(note){ note.textContent = ""; note.className = "st-note"; }
}

/* 主题分段钮：点击立即生效并持久化（与旧页头滑块同语义），不用等保存 */
document.querySelectorAll("#st-theme-seg .mode").forEach(b => {
  b.addEventListener("click", () => {
    if(window.videreadTheme) window.videreadTheme.set(b.dataset.theme);
    syncThemeSeg();
  });
});

/* 默认模式 / 默认语言分段钮：组内单选，点选后待保存 */
document.querySelectorAll("#st-mode-seg, #st-lang-seg").forEach(seg => {
  seg.querySelectorAll(".mode").forEach(b => {
    b.addEventListener("click", () => {
      seg.querySelectorAll(".mode").forEach(x => x.classList.toggle("on", x === b));
    });
  });
});

$("#st-gen-save").addEventListener("click", () => {
  const mb = document.querySelector("#st-mode-seg .mode.on");
  const lb = document.querySelector("#st-lang-seg .mode.on");
  const d = {
    mode: mb ? mb.dataset.dmode : "standard",
    lang: lb ? lb.dataset.dlang : "zh",
    label: lb ? (lb.dataset.dlabel || "中文") : "中文",
    frames: !!($("#st-def-frames") && $("#st-def-frames").checked),
  };
  try{ localStorage.setItem("videread.defaults", JSON.stringify(d)); }catch(e){}
  applyDefaultsToForm();
  const note = $("#st-gen-note");
  if(note){ note.textContent = "已保存，投印表单已套用这些默认值"; note.className = "st-note ok"; }
  toast("设置已保存");
});

applyDefaultsToForm(); /* 打开页面即套用保存的默认值 */

refreshAll();
checkActiveJob();
jfetch("/api/runs?page=1&page_size=1").then(r => {
  if(r.runs && r.runs[0]) openFeature(r.runs[0].run_id);
}).catch(() => openFeature(SAMPLE.runs[0].run_id));
