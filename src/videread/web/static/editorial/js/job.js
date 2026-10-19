"use strict";
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const STAGES = ["下载","音频处理","转写","规范化","结构规划","截图","逐节写作","渲染"];
const STATE_ZH = {queued:"排队中",running:"印制中",done:"印毕",cancelled:"已取消",failed:"失败",error:"失败"};
const TERMINAL = new Set(["done","cancelled","failed","error"]);
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
async function getSnapshot(){
  const r = await fetch("/api/jobs/" + encodeURIComponent(job_id));
  if(!r.ok){
    let detail = r.status;
    try{ const j = await r.json(); if(j && j.detail) detail = j.detail; }catch(e){}
    throw new Error(detail);
  }
  return await r.json();
}
async function postCancel(){
  const r = await fetch("/api/jobs/" + encodeURIComponent(job_id) + "/cancel", {method: "POST"});
  if(!r.ok){
    let detail = r.status;
    try{ const j = await r.json(); if(j && j.detail) detail = j.detail; }catch(e){}
    throw new Error(detail);
  }
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
    if(i === current) return '<li class="cur"><span class="no">' + no + '</span><span class="t">' + esc(name) + '</span><span class="mark">●</span></li>';
    return '<li><span class="no">' + no + '</span><span class="t">' + esc(name) + '</span><span class="mark">·</span></li>';
  }).join("");
  stepperSeen = new Set(doneSet);
}

function pushLog(line, isErr){
  const box = $("#log");
  const el = document.createElement("span");
  el.className = "line" + (isErr ? " err" : "");
  el.textContent = line;
  box.appendChild(el);
  box.scrollTop = box.scrollHeight;
}

function stateZh(s){ return STATE_ZH[s] || s || "—"; }

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

function renderJob(job){
  if(TERMINAL.has(job.state) && localStorage.getItem("videread.activeJob") === job.id){
    localStorage.removeItem("videread.activeJob"); /* 终态：清掉主页的进行中标记 */
  }
  $("#job-state").textContent = stateZh(job.state);
  $("#job-url").textContent = job.url || "—";
  const meta = [];
  meta.push("工单号 <b>" + esc(job.id) + "</b>");
  if(job.mode) meta.push("模式 <b>" + esc(job.mode) + "</b>");
  if(job.created_at) meta.push("提交 <b>" + esc(job.created_at) + "</b>");
  if(job.state === "queued" && typeof job.queue_position === "number" && job.queue_position > 0)
    meta.push('<span class="q">队列第 ' + (job.queue_position + 1) + " 位</span>");
  if(job.finished_at) meta.push("结束 <b>" + esc(job.finished_at) + "</b>");
  $("#job-meta").innerHTML = meta.map(s => "<span>" + s + "</span>").join("");

  const idx = Math.min(Math.max(0, job.stage_index || 0), STAGES.length);
  const doneSet = new Set();
  if(job.state === "done"){ for(let i = 0; i < STAGES.length; i++) doneSet.add(i); renderStepper(STAGES.length, doneSet); }
  else if(job.state === "queued"){ renderStepper(-1, doneSet); }
  else { for(let i = 0; i < idx; i++) doneSet.add(i); renderStepper(job.state === "running" ? idx : -1, doneSet); }

  const log = $("#log");
  log.innerHTML = "";
  (job.logs || []).forEach(l => pushLog(l, false));
  if(job.error){
    $("#job-err").textContent = "失败：" + job.error + (job.error_hint ? " · " + job.error_hint : "");
    pushLog("[错误] " + job.error, true);
  }
  renderActions(job);
}

/* ---------- SSE 订阅 + 快照兜底轮询 ---------- */
let es = null, pollTimer = null;
function subscribe(){
  es = new EventSource("/api/jobs/" + encodeURIComponent(job_id) + "/events");
  es.addEventListener("status", ev => {
    const d = JSON.parse(ev.data);
    $("#job-state").textContent = stateZh(d.state);
  });
  es.addEventListener("stage", ev => {
    const d = JSON.parse(ev.data);
    const doneSet = new Set();
    for(let i = 0; i < d.index; i++) doneSet.add(i);
    renderStepper(d.index, doneSet);
  });
  es.addEventListener("log", ev => pushLog(JSON.parse(ev.data).line || "", false));
  es.addEventListener("done", async ev => {
    const d = JSON.parse(ev.data);
    pushLog("[完成] " + (d.run_id || ""));
    es.close(); es = null;
    toast("新的一期印好了");
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
      if(TERMINAL.has(job.state)){ clearInterval(pollTimer); pollTimer = null; }
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
    $(".proof-h").style.display = "none";
    return;
  }
  renderJob(job);
  /* 终态任务只渲染快照；进行中的才订阅事件流 */
  if(!TERMINAL.has(job.state) && job.state !== "queued") subscribe();
  else if(job.state === "queued") subscribe(); /* 排队中也要等 running/done 事件 */
})();
