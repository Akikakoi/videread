"use strict";
const STATE_ZH = {queued:"排队中",running:"印制中",done:"印毕",cancelled:"已取消",failed:"失败",error:"失败",pending:"待印",skipped:"已跳过"};
const job_id = decodeURIComponent(location.pathname.split("/").pop() || "");

/* ---------- toast ---------- */
let toastH = null, toastOutH = null;
function toast(msg){
  const t = $("#toast");
  t.textContent = msg;
  clearTimeout(toastH); clearTimeout(toastOutH);
  t.classList.remove("out");
  t.style.display = "block";
  void t.offsetWidth;
  toastH = setTimeout(() => {
    t.classList.add("out");
    toastOutH = setTimeout(() => { t.style.display = "none"; t.classList.remove("out"); }, 280);
  }, 3600);
}

/* ---------- 数据获取 ---------- */
async function fetchJson(path){
  const r = await fetch(path);
  if(!r.ok){
    let detail = r.status;
    try{ const j = await r.json(); if(j && j.detail) detail = j.detail; }catch(e){}
    const err = new Error(detail);
    err.status = r.status;
    throw err;
  }
  return await r.json();
}
async function getSnapshot(){
  try{
    return await fetchJson("/api/jobs/" + encodeURIComponent(job_id));
  }catch(err){
    if(err.status !== 404) throw err;
    /* 服务重启后内存里的工单没了：退回落盘的批次清单，篇目与结果仍可查看 */
    const data = await fetchJson("/api/batches/" + encodeURIComponent(job_id));
    if(!JOB_TERMINAL.has(data.state)){
      data.state = "error";
      data.error = "服务已重启，这一批没有继续执行";
      data.error_hint = "篇目清单仍保留，可回到编辑部重新投递";
    }
    data.logs = data.logs || [];
    return data;
  }
}
async function postCancel(){
  const r = await fetch("/api/jobs/" + encodeURIComponent(job_id) + "/cancel", {method: "POST"});
  if(!r.ok){
    let detail = r.status;
    try{ const j = await r.json(); if(j && j.detail) detail = j.detail; }catch(e){}
    throw new Error(detail);
  }
}

/* ---------- 运行氛围：打字机日志 / 空闲提示 ---------- */
const ACTIVE_STATES = new Set(["queued", "running"]);
const reduceMotion = () => window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
let currentState = "queued";
let typeQueue = [], typeTimer = null, idleTimer = null, idleEl = null;

function scrollLog(){
  const box = $("#log");
  box.scrollTop = box.scrollHeight;
}
function removeIdle(){
  if(idleEl){ idleEl.remove(); idleEl = null; }
}
function showIdle(){
  if(!ACTIVE_STATES.has(currentState) || typeQueue.length || idleEl) return;
  idleEl = document.createElement("span");
  idleEl.className = "line idle";
  idleEl.innerHTML = (currentState === "queued" ? "等待上机" : "正在排印") + '<span class="ell"><i></i><i></i><i></i></span>';
  $("#log").appendChild(idleEl);
  scrollLog();
}
function scheduleIdle(){
  clearTimeout(idleTimer);
  removeIdle();
  if(!ACTIVE_STATES.has(currentState)) return;
  idleTimer = setTimeout(showIdle, 2000);
}
function typeStep(){
  const cur = typeQueue[0];
  if(!cur){ typeTimer = null; return; }
  if(!cur.el.classList.contains("typing")) cur.el.classList.add("typing");
  cur.i++;
  cur.el.textContent = cur.text.slice(0, cur.i);
  scrollLog();
  if(cur.i >= cur.text.length){
    cur.el.classList.remove("typing");
    typeQueue.shift();
    typeTimer = setTimeout(typeStep, 90);
    if(!typeQueue.length) scheduleIdle();
  }else{
    typeTimer = setTimeout(typeStep, 18);
  }
}
function flushTyping(){
  clearTimeout(typeTimer); typeTimer = null;
  typeQueue.forEach(item => { item.el.textContent = item.text; item.el.classList.remove("typing"); });
  typeQueue = [];
}
function resetAmbience(){
  clearTimeout(typeTimer); typeTimer = null;
  clearTimeout(idleTimer); idleTimer = null;
  typeQueue = []; idleEl = null;
}

/* ---------- 渲染 ---------- */
let stepperSeen = new Set();
function renderStepper(current, doneSet){
  const ul = $("#stepper");
  ul.innerHTML = STAGES.map((name, i) => {
    const no = String(i + 1).padStart(2, "0");
    if(doneSet.has(i)){
      const pop = !stepperSeen.has(i) && stepperSeen.size > 0 ? " pop" : "";
      return '<li class="done' + pop + '"><span class="no">' + no + '</span><span class="t">' + esc(name) + '</span><span class="mark">✓</span></li>';
    }
    if(i === current) return '<li class="cur"><span class="no">' + no + '</span><span class="t">' + esc(name) + '</span><span class="mark dots"><i></i><i></i><i></i></span></li>';
    return '<li><span class="no">' + no + '</span><span class="t">' + esc(name) + '</span><span class="mark">·</span></li>';
  }).join("");
  stepperSeen = new Set(doneSet);
}

function setProgress(value){
  const fill = $("#stepper-fill");
  if(!fill) return;
  fill.style.width = (Math.max(0, Math.min(1, value)) * 100) + "%";
}

function pushLog(line, isErr, live){
  removeIdle();
  const box = $("#log");
  const el = document.createElement("span");
  el.className = "line" + (isErr ? " err" : "") + (live ? " new" : "");
  box.appendChild(el);
  if(live && !reduceMotion()){
    typeQueue.push({el, text: line, i: 0});
    if(typeQueue.length > 6) flushTyping();
    if(!typeTimer) typeStep();
  }else{
    el.textContent = line;
    scrollLog();
  }
  if(live) scheduleIdle();
}

function stateZh(s){ return STATE_ZH[s] || s || "—"; }

/* 标题状态：运行中在“印制中”后补一组闪烁省略号；状态变化时整行淡入 */
let lastLabelState = "";
function setStateLabel(state){
  const dots = state === "running"
    ? '<span class="state-dots" aria-hidden="true"><i></i><i></i><i></i></span>'
    : "";
  const el = $("#job-state");
  el.innerHTML = esc(stateZh(state)) + dots;
  if(lastLabelState && lastLabelState !== state && !reduceMotion()){
    el.classList.remove("state-in");
    void el.offsetWidth; /* 重启动画 */
    el.classList.add("state-in");
  }
  lastLabelState = state;
}

function renderActions(job){
  const box = $("#job-actions");
  box.innerHTML = "";
  const mk = (label, href, cls) => {
    const a = document.createElement("a");
    a.className = "act" + (cls ? " " + cls : "");
    a.href = href;
    a.textContent = label;
    return a;
  };
  if(job.state === "done" && job.run_id){
    box.appendChild(mk("翻开全文 →", "/read/" + encodeURIComponent(job.run_id)));
    box.appendChild(mk("原样打开", "/report/" + encodeURIComponent(job.run_id)));
  }
  if(job.state === "queued" || job.state === "running"){
    const b = document.createElement("button");
    b.className = "act danger";
    b.type = "button";
    b.textContent = "取消投印";
    b.addEventListener("click", async () => {
      b.disabled = true;
      try{
        await postCancel();
        toast("已请求取消，将在检查点停止");
      }catch(err){
        toast("取消失败：" + err.message);
        b.disabled = false;
      }
    });
    box.appendChild(b);
  }
  box.appendChild(mk("回到编辑部", "/editorial"));
}

/* 批次篇目：每条视频的状态、跳过原因与已完成报告入口 */
function renderBatch(job){
  const wrap = $("#batch-wrap");
  if(!job || !job.batch){ wrap.hidden = true; return; }
  wrap.hidden = false;
  $("#batch-list").innerHTML = (job.items || []).map((it, i) => {
    const st = it.state || "pending";
    const tail = [];
    if(st === "skipped" && it.reason) tail.push('<span>' + esc(it.reason) + '</span>');
    if(st === "error" && it.error) tail.push('<span class="warn">' + esc(it.error) + '</span>');
    if(it.report_url) tail.push('<a href="' + esc(it.report_url) + '" target="_blank" rel="noopener">翻开 →</a>');
    return '<li class="batch-item ' + esc(st) + '">' +
      '<span class="bi-no">' + String(i + 1).padStart(2, "0") + '</span>' +
      '<span class="bi-t">' + esc(it.title || it.url || "") + '</span>' +
      '<span class="bi-st">' + esc(stateZh(st)) + '</span>' +
      '<span class="bi-x">' + tail.join("") + '</span>' +
      '</li>';
  }).join("");
}

function renderMeta(job){
  const meta = [];
  meta.push("工单号 <b>" + esc(job.id) + "</b>");
  if(job.mode) meta.push("模式 <b>" + esc(job.mode) + "</b>");
  if(job.batch_title) meta.push("收藏夹 <b>" + esc(job.batch_title) + "</b>");
  if(job.created_at) meta.push("提交 <b>" + esc(job.created_at) + "</b>");
  if(job.state === "queued" && typeof job.queue_position === "number" && job.queue_position > 0)
    meta.push('<span class="q">队列第 ' + (job.queue_position + 1) + " 位</span>");
  if(job.batch){
    const items = job.items || [];
    const d = items.filter(it => it.state === "done").length;
    const e = items.filter(it => it.state === "error").length;
    const s = items.filter(it => it.state === "skipped").length;
    meta.push("篇目 <b>" + d + "/" + items.length + "</b> 完成" +
      (e ? " · 失败 " + e : "") + (s ? " · 跳过 " + s : ""));
  }
  if(job.finished_at) meta.push("结束 <b>" + esc(job.finished_at) + "</b>");
  $("#job-meta").innerHTML = meta.map(s => "<span>" + s + "</span>").join("");
}

let lastJob = null;

function renderJob(job){
  lastJob = job;
  currentState = job.state;
  document.body.classList.toggle("job-done", job.state === "done");
  document.body.classList.toggle("job-running", job.state === "running");
  if(JOB_TERMINAL.has(job.state) && localStorage.getItem("videread.activeJob") === job.id){
    localStorage.removeItem("videread.activeJob"); /* 终态：清掉主页的进行中标记 */
  }
  setStateLabel(job.state);
  $("#job-url").textContent = job.url || "—";
  renderMeta(job);
  renderBatch(job);

  const idx = Math.min(Math.max(0, job.stage_index || 0), STAGES.length);
  const doneSet = new Set();
  if(job.state === "done"){ for(let i = 0; i < STAGES.length; i++) doneSet.add(i); renderStepper(STAGES.length, doneSet); }
  else if(job.state === "queued"){ renderStepper(-1, doneSet); }
  else { for(let i = 0; i < idx; i++) doneSet.add(i); renderStepper(job.state === "running" ? idx : -1, doneSet); }

  const progressed = job.state === "done" ? 1
    : (job.state === "running" || JOB_TERMINAL.has(job.state)) ? idx / STAGES.length : 0;
  setProgress(progressed);

  const log = $("#log");
  resetAmbience();
  log.innerHTML = "";
  (job.logs || []).forEach(l => pushLog(l, false));
  if(job.error){
    $("#job-err").textContent = "失败：" + job.error + (job.error_hint ? " · " + job.error_hint : "");
    pushLog("[错误] " + job.error, true);
  }
  renderActions(job);
  if(!JOB_TERMINAL.has(job.state)) scheduleIdle();
}

/* ---------- SSE 订阅 + 快照兜底轮询 ---------- */
let es = null, pollTimer = null;
function subscribe(){
  es = new EventSource("/api/jobs/" + encodeURIComponent(job_id) + "/events");
  es.addEventListener("status", ev => {
    const d = JSON.parse(ev.data);
    currentState = d.state;
    document.body.classList.toggle("job-running", d.state === "running");
    setStateLabel(d.state);
    if(ACTIVE_STATES.has(d.state)) scheduleIdle();
    else { clearTimeout(idleTimer); removeIdle(); }
  });
  es.addEventListener("stage", ev => {
    const d = JSON.parse(ev.data);
    const doneSet = new Set();
    for(let i = 0; i < d.index; i++) doneSet.add(i);
    renderStepper(d.index, doneSet);
    setProgress(d.index / STAGES.length);
  });
  es.addEventListener("log", ev => pushLog(JSON.parse(ev.data).line || "", false, true));
  es.addEventListener("item", ev => {
    const d = JSON.parse(ev.data);
    /* 只重绘篇目区，不打断日志的打字机与步进器 */
    if(lastJob && lastJob.items && lastJob.items[d.index]){
      lastJob.items[d.index] = d.item;
      renderBatch(lastJob);
      renderMeta(lastJob);
    }
  });
  es.addEventListener("done", async ev => {
    const d = JSON.parse(ev.data);
    if(d.batch){
      pushLog("[批次完成] 成功 " + (d.done || 0) + " 条" + (d.failed ? "，失败 " + d.failed + " 条" : ""));
      toast("这一批印好了");
    }else{
      pushLog("[完成] " + (d.run_id || ""));
      toast("新的一期印好了");
    }
    es.close(); es = null;
    renderJob(await getSnapshot()); /* 终态以快照为准，一次渲染收敛 */
  });
  es.addEventListener("cancelled", async ev => {
    const d = JSON.parse(ev.data);
    pushLog("[取消] " + (d.message || "已在最近的检查点停止，已落盘产物保留"));
    es.close(); es = null;
    toast("任务已取消");
    renderJob(await getSnapshot());
  });
  es.addEventListener("error", async ev => {
    if(ev.data){
      const d = JSON.parse(ev.data);
      pushLog("[错误] " + (d.message || "任务失败") + (d.hint ? " · " + d.hint : ""), true);
      es.close(); es = null;
      renderJob(await getSnapshot());
      return;
    }
    /* 连接层错误：改用快照轮询兜底 */
    if(es){ es.close(); es = null; }
    startPolling();
  });
}
function startPolling(){
  if(pollTimer) return;
  pollTimer = setInterval(async () => {
    try{
      const job = await getSnapshot();
      renderJob(job);
      if(JOB_TERMINAL.has(job.state)){ clearInterval(pollTimer); pollTimer = null; }
    }catch(e){ /* 服务暂不可达：下个周期再试 */ }
  }, 2500);
}

/* ---------- 启动 ---------- */
(async function init(){
  let job;
  try{
    job = await getSnapshot();
  }catch(err){
    $("#job-state").textContent = "工单不存在";
    $("#job-url").textContent = "工单号 " + job_id + " · " + err.message;
    $("#stepper").outerHTML = '<p style="color:var(--muted);font-size:14px;">链接可能已过期，或工单号有误。<a href="/editorial" style="color:var(--accent)">回到编辑部重新投印 →</a></p>';
    $("#log").style.display = "none";
    document.querySelectorAll(".proof-h").forEach(el => { el.style.display = "none"; });
    return;
  }
  renderJob(job);
  /* 终态任务只渲染快照；进行中的才订阅事件流 */
  if(!JOB_TERMINAL.has(job.state) && job.state !== "queued") subscribe();
  else if(job.state === "queued") subscribe(); /* 排队中也要等 running/done 事件 */
})();
