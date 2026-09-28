/* videread 控制台：表单提交、SSE 进度、报告库与预览
   所有来自服务端 / 视频元信息的内容都用 textContent 写入，避免标题里的标记被执行。 */

(function () {
  "use strict";

  var STAGES = ["下载", "音频处理", "转写", "规范化", "结构规划", "逐节写作", "渲染"];
  var STAGE_COUNT = STAGES.length;
  // 逐节写作在 STAGES 里的序号（1 起）：只有这个阶段有「第 k/n 节」的子进度
  var WRITER_STAGE = STAGES.indexOf("逐节写作") + 1;
  var SECTION_RE = /第\s*(\d+)\/(\d+)\s*节/;
  var STORE_KEY = "videread.job";

  var el = {
    form: document.getElementById("job-form"),
    url: document.getElementById("url"),
    mode: document.getElementById("mode"),
    asr: document.getElementById("asr-backend"),
    noCache: document.getElementById("no-cache"),
    keepAudio: document.getElementById("keep-audio"),
    submit: document.getElementById("submit"),
    formError: document.getElementById("form-error"),
    progress: document.getElementById("progress"),
    pill: document.getElementById("state-pill"),
    spinner: document.getElementById("spinner"),
    elapsed: document.getElementById("elapsed"),
    stepper: document.getElementById("stepper"),
    progressBar: document.getElementById("progress-bar"),
    progressFill: document.getElementById("progress-fill"),
    progressNote: document.getElementById("progress-note"),
    progressPct: document.getElementById("progress-pct"),
    log: document.getElementById("log"),
    result: document.getElementById("result"),
    openReport: document.getElementById("open-report"),
    previewReport: document.getElementById("preview-report"),
    jobError: document.getElementById("job-error"),
    outRoot: document.getElementById("out-root"),
    runs: document.getElementById("runs"),
    runsCount: document.getElementById("runs-count"),
    runsEmpty: document.getElementById("runs-empty"),
    filter: document.getElementById("filter"),
    refresh: document.getElementById("refresh-runs"),
    preview: document.getElementById("preview"),
    previewBoxFrame: document.getElementById("preview-frame"),
    previewTitle: document.getElementById("preview-title"),
    previewOpen: document.getElementById("preview-open"),
    previewClose: document.getElementById("preview-close")
  };

  var stream = null;
  var stageIndex = 0;      // 已完成的阶段数：k 表示 k 阶段已过、k+1 正在跑
  var sectionDone = 0;     // 逐节写作：已完成的节数
  var sectionTotal = 0;    // 逐节写作：总节数（来自日志）
  var currentState = "queued";
  var startedAt = 0;
  var timerId = null;
  var runsCache = [];

  /* ───────────────────────────── 工具 ───────────────────────────── */

  function fmtMs(ms) {
    if (ms === null || ms === undefined) return "—";
    if (ms < 1000) return ms + "ms";
    var s = ms / 1000;
    if (s < 60) return s.toFixed(1) + "s";
    var m = Math.floor(s / 60);
    var rest = Math.round(s % 60);
    return m + "m" + (rest < 10 ? "0" : "") + rest + "s";
  }

  function fmtBytes(n) {
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    return (n / 1048576).toFixed(1) + " MB";
  }

  function fmtClock(seconds) {
    var total = Math.max(0, Math.floor(seconds));
    var minutes = Math.floor(total / 60);
    var rest = total % 60;
    return (minutes < 10 ? "0" : "") + minutes + ":" + (rest < 10 ? "0" : "") + rest;
  }

  function node(tag, className, text) {
    var item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined && text !== null) item.textContent = text;
    return item;
  }

  function show(target, visible) {
    target.hidden = !visible;
  }

  /* ───────────────────────────── 进度区 ───────────────────────────── */

  function buildStepper() {
    el.stepper.textContent = "";
    STAGES.forEach(function (label, index) {
      var item = node("li", null, label);
      item.dataset.index = String(index + 1);
      item.dataset.state = "pending";
      el.stepper.appendChild(item);
    });
  }

  function setStage(index) {
    stageIndex = index;
    var items = el.stepper.children;
    for (var i = 0; i < items.length; i++) {
      var position = i + 1;
      // 服务端在阶段「结束后」才上报 index，所以 index 表示已完成数，
      // 活跃项是 index + 1 —— 长阶段期间才看得到动的东西。
      if (position <= index) {
        items[i].dataset.state = "done";
      } else if (position === index + 1) {
        items[i].dataset.state = "active";
      } else if (items[i].dataset.state !== "error") {
        items[i].dataset.state = "pending";
      }
    }
    renderProgress();
  }

  function completeStages() {
    stageIndex = STAGE_COUNT;
    for (var i = 0; i < el.stepper.children.length; i++) {
      el.stepper.children[i].dataset.state = "done";
    }
  }

  function failStages(index) {
    for (var i = 0; i < el.stepper.children.length; i++) {
      var position = i + 1;
      if (position === index) {
        el.stepper.children[i].dataset.state = "error";
      } else if (position < index) {
        el.stepper.children[i].dataset.state = "done";
      }
    }
  }

  /* 进度条：以 7 个阶段为刻度；「逐节写作」阶段再按 第 k/n 节 细分，
     这样最长的那个阶段里数字也会持续往前走，而不是卡住不动。 */
  function progressPercent() {
    if (currentState === "done") return 100;
    if (currentState === "error") return null;
    var completed = Math.min(stageIndex, STAGE_COUNT);
    var fraction = 0;
    if (completed + 1 === WRITER_STAGE && sectionTotal > 0) {
      fraction = Math.min(1, sectionDone / sectionTotal);
    }
    // 未结束前一格不满，避免出现"100% 却还没完"的错觉
    return Math.min(99, Math.round(((completed + fraction) / STAGE_COUNT) * 100));
  }

  function progressNoteText() {
    if (currentState === "done") return "全部阶段完成";
    if (currentState === "error") return "已中断";
    if (currentState === "queued") return "等待执行…";
    if (stageIndex >= STAGE_COUNT) return "收尾…";
    var label = STAGES[Math.min(stageIndex, STAGE_COUNT - 1)];
    if (stageIndex + 1 === WRITER_STAGE && sectionTotal > 0) {
      return label + " · " + sectionDone + "/" + sectionTotal + " 节";
    }
    return label;
  }

  function renderProgress() {
    var percent = progressPercent();
    if (percent === null) {
      el.progressBar.dataset.running = "0";
      el.progressPct.textContent = "—";
    } else {
      el.progressFill.style.width = percent + "%";
      el.progressBar.setAttribute("aria-valuenow", String(percent));
      el.progressBar.dataset.running =
        (currentState === "running" || currentState === "queued") ? "1" : "0";
      el.progressPct.textContent = percent + "%";
    }
    el.progressPct.dataset.state = currentState;
    el.progressNote.textContent = progressNoteText();
  }

  function tickElapsed() {
    if (!startedAt) return;
    el.elapsed.textContent = "已用 " + fmtClock((Date.now() - startedAt) / 1000);
  }

  function startTimer() {
    startedAt = Date.now();
    el.elapsed.hidden = false;
    tickElapsed();
    if (timerId === null) timerId = window.setInterval(tickElapsed, 1000);
  }

  function stopTimer() {
    if (timerId !== null) {
      window.clearInterval(timerId);
      timerId = null;
    }
    tickElapsed();
  }

  function setState(state) {
    currentState = state;
    var labels = { queued: "排队中", running: "执行中", done: "已完成", error: "失败" };
    el.pill.textContent = labels[state] || state;
    el.pill.dataset.state = state;
    el.spinner.hidden = !(state === "queued" || state === "running");
    if (state === "running") {
      if (timerId === null) startTimer();
    } else if (state === "queued") {
      el.elapsed.hidden = true;
    } else {
      stopTimer();
    }
    renderProgress();
  }

  function trackSections(line) {
    var match = SECTION_RE.exec(line || "");
    if (!match) return;
    sectionDone = parseInt(match[1], 10);
    sectionTotal = parseInt(match[2], 10);
    renderProgress();
  }

  function appendLog(line) {
    var atBottom = el.log.scrollTop + el.log.clientHeight >= el.log.scrollHeight - 24;
    el.log.textContent += (el.log.textContent ? "\n" : "") + line;
    if (atBottom) el.log.scrollTop = el.log.scrollHeight;
  }

  function resetProgress() {
    stageIndex = 0;
    sectionDone = 0;
    sectionTotal = 0;
    currentState = "queued";
    startedAt = 0;
    stopTimer();
    el.log.textContent = "";
    el.spinner.hidden = true;
    el.elapsed.hidden = true;
    el.elapsed.textContent = "已用 00:00";
    buildStepper();
    setStage(0);  // 第 1 个阶段立刻置为活跃，避免开头看起来没有任何动静
    show(el.result, false);
    show(el.jobError, false);
    el.jobError.textContent = "";
    el.openReport.setAttribute("href", "#");
  }

  /* ───────────────────────────── 任务流转 ───────────────────────────── */

  function setFormError(message) {
    el.formError.textContent = message || "";
    show(el.formError, Boolean(message));
  }

  el.form.addEventListener("submit", function (event) {
    event.preventDefault();
    var payload = {
      url: el.url.value.trim(),
      mode: el.mode.value,
      no_cache: el.noCache.checked,
      keep_audio: el.keepAudio.checked,
      asr_backend: el.asr.value || null
    };
    if (!payload.url) {
      setFormError("请先填写 BV 号、视频链接或本地音视频路径。");
      return;
    }
    setFormError("");
    el.submit.disabled = true;
    fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    })
      .then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (data) {
          return { ok: response.ok, status: response.status, data: data };
        });
      })
      .then(function (result) {
        if (!result.ok) {
          setFormError(result.data.detail || "提交失败（HTTP " + result.status + "）");
          return;
        }
        attachJob(result.data.job_id, true);
      })
      .catch(function (error) {
        setFormError("无法连接本地服务：" + error.message);
      })
      .finally(function () {
        el.submit.disabled = false;
      });
  });

  function attachJob(jobId, reset) {
    sessionStorage.setItem(STORE_KEY, jobId);
    if (reset) resetProgress();
    show(el.progress, true);
    if (stream) { stream.close(); stream = null; }

    stream = new EventSource("/api/jobs/" + jobId + "/events");
    stream.addEventListener("status", function (event) {
      setState(JSON.parse(event.data).state);
    });
    stream.addEventListener("stage", function (event) {
      setStage(JSON.parse(event.data).index);
    });
    stream.addEventListener("log", function (event) {
      var line = JSON.parse(event.data).line;
      appendLog(line);
      trackSections(line);
    });
    stream.addEventListener("done", function (event) {
      finishJob(JSON.parse(event.data));
    });
    stream.addEventListener("error", function (event) {
      if (event.data) {
        failJob(JSON.parse(event.data));
        return;
      }
      // EventSource 自身的连接错误没有 data：改用快照兜底
      if (stream) { stream.close(); stream = null; }
      pollSnapshot(jobId);
    });
  }

  function finishJob(data) {
    sessionStorage.removeItem(STORE_KEY);
    completeStages();
    setState("done");
    if (stream) { stream.close(); stream = null; }
    if (data.report_url) {
      el.openReport.setAttribute("href", data.report_url);
      show(el.result, true);
      openPreview(data.report_url, "刚刚生成的报告");
    }
    loadRuns();
  }

  function failJob(data) {
    sessionStorage.removeItem(STORE_KEY);
    setState("error");
    if (stream) { stream.close(); stream = null; }
    // stageIndex 是已完成数，出错的阶段是它后面那一个
    failStages(Math.min(stageIndex + 1, STAGE_COUNT));
    // hint 由服务端 failures 下发，前端不再维护退出码对照表
    el.jobError.textContent = "任务失败（退出码 " + data.exit_code + "）" +
      (data.hint ? "：" + data.hint : "") + "\n" + (data.message || "");
    show(el.jobError, true);
    loadRuns();
  }

  function pollSnapshot(jobId) {
    fetch("/api/jobs/" + jobId)
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (snapshot) {
        if (!snapshot) { sessionStorage.removeItem(STORE_KEY); return; }
        el.log.textContent = (snapshot.logs || []).join("\n");
        el.log.scrollTop = el.log.scrollHeight;
        if (snapshot.stage_index) setStage(snapshot.stage_index);
        if (snapshot.state === "done") {
          finishJob({ report_url: snapshot.report_url });
        } else if (snapshot.state === "error") {
          failJob({ exit_code: snapshot.exit_code, hint: snapshot.error_hint, message: snapshot.error || "" });
        } else {
          // 进度仍在推进但事件流已断：退化为轮询直到终态
          window.setTimeout(function () { pollSnapshot(jobId); }, 2000);
        }
      })
      .catch(function () { sessionStorage.removeItem(STORE_KEY); });
  }

  function resumeJob(jobId) {
    fetch("/api/jobs/" + jobId)
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (snapshot) {
        if (!snapshot) { sessionStorage.removeItem(STORE_KEY); return; }
        el.url.value = snapshot.url || el.url.value;
        el.mode.value = snapshot.mode || "standard";
        attachJob(jobId, true);
      })
      .catch(function () { sessionStorage.removeItem(STORE_KEY); });
  }

  /* ───────────────────────────── 报告库 ───────────────────────────── */

  function loadRuns() {
    fetch("/api/runs")
      .then(function (response) { return response.json(); })
      .then(function (data) {
        el.outRoot.textContent = data.out_root || "—";
        el.outRoot.title = data.out_root || "";
        runsCache = data.runs || [];
        renderRuns();
      })
      .catch(function () {
        el.outRoot.textContent = "无法连接服务";
      });
  }

  function renderRuns() {
    var keyword = el.filter.value.trim().toLowerCase();
    var visible = runsCache.filter(function (run) {
      if (!keyword) return true;
      return [run.title, run.uploader, run.bvid, run.run_id]
        .join(" ")
        .toLowerCase()
        .indexOf(keyword) >= 0;
    });

    el.runs.textContent = "";
    el.runsCount.textContent = runsCache.length ? "共 " + runsCache.length + " 份" : "";
    show(el.runsEmpty, runsCache.length === 0);
    if (!visible.length) {
      if (runsCache.length) el.runs.appendChild(node("p", "muted empty", "没有匹配的报告。"));
      return;
    }

    visible.forEach(function (run) {
      var card = node("div", "run-card");

      card.appendChild(node("h3", "run-title", run.title || run.run_id));

      var meta = node("p", "run-meta");
      [run.uploader ? "UP主 " + run.uploader : "",
       "时长 " + run.duration_text,
       run.profile || "",
       run.sections ? run.sections + " 节" : "",
       run.generated_at || "",
       run.bvid && run.bvid !== "local" ? run.bvid : "本地文件"
      ].filter(Boolean).forEach(function (text) {
        meta.appendChild(node("span", null, text));
      });
      card.appendChild(meta);

      var actions = node("div", "run-actions");
      if (run.has_report) {
        var view = node("button", "btn btn-small", "预览");
        view.type = "button";
        view.addEventListener("click", function () { openPreview(run.report_url, run.title); });
        actions.appendChild(view);

        var fresh = node("a", "btn btn-small", "新窗口打开");
        fresh.href = run.report_url;
        fresh.target = "_blank";
        fresh.rel = "noopener";
        actions.appendChild(fresh);
      } else {
        actions.appendChild(node("span", "tag", "无 report.html"));
      }

      var detailButton = node("button", "btn btn-small", "阶段耗时");
      detailButton.type = "button";
      actions.appendChild(detailButton);
      card.appendChild(actions);

      var detail = node("div", "run-detail");
      show(detail, false);
      card.appendChild(detail);
      detailButton.addEventListener("click", function () {
        if (!detail.hidden) { show(detail, false); return; }
        show(detail, true);
        if (detail.dataset.loaded === "1") return;
        renderDetail(detail, run.run_id);
      });

      el.runs.appendChild(card);
    });
  }

  function renderDetail(container, runId) {
    container.textContent = "读取中…";
    fetch("/api/runs/" + encodeURIComponent(runId))
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (detail) {
        container.textContent = "";
        if (!detail) {
          container.appendChild(node("p", "detail-note bad", "读取失败。"));
          return;
        }
        container.dataset.loaded = "1";

        var trace = detail.trace || { stages: [], errors: [], total_ms: 0, tokens: 0 };
        var table = node("table", "trace");
        var head = node("tr");
        ["阶段", "耗时", "token", "状态"].forEach(function (label) {
          head.appendChild(node("th", null, label));
        });
        table.appendChild(head);

        trace.stages.forEach(function (stage) {
          var row = node("tr");
          row.appendChild(node("td", null, stage.label));
          row.appendChild(node("td", "num", fmtMs(stage.dur_ms)));
          row.appendChild(node("td", "num", stage.tokens === null ? "—" : String(stage.tokens)));
          var state = node("td");
          state.appendChild(node("span", stage.cached ? "tag cached" : "tag",
            stage.cached ? "缓存命中" : "已执行"));
          row.appendChild(state);
          table.appendChild(row);
        });
        container.appendChild(table);

        var total = node("p", "detail-note",
          "合计 " + fmtMs(trace.total_ms) + "，token " + trace.tokens +
          "，产物 " + (detail.files || []).length + " 个");
        container.appendChild(total);

        (trace.errors || []).forEach(function (item) {
          container.appendChild(node("p", "detail-note bad",
            item.stage + " 阶段曾报错：" + item.message));
        });

        if (detail.lead) {
          container.appendChild(node("p", "detail-note", "导语：" + detail.lead));
        }
      })
      .catch(function () {
        container.textContent = "";
        container.appendChild(node("p", "detail-note bad", "读取失败。"));
      });
  }

  el.filter.addEventListener("input", renderRuns);
  el.refresh.addEventListener("click", loadRuns);

  /* ───────────────────────────── 预览浮层 ───────────────────────────── */

  function openPreview(url, title) {
    el.previewBoxFrame.setAttribute("src", url);
    el.previewOpen.setAttribute("href", url);
    el.previewTitle.textContent = title || "报告预览";
    show(el.preview, true);
  }

  function closePreview() {
    show(el.preview, false);
    el.previewBoxFrame.removeAttribute("src");
  }

  el.previewClose.addEventListener("click", closePreview);
  el.preview.addEventListener("click", function (event) {
    if (event.target === el.preview) closePreview();
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !el.preview.hidden) closePreview();
  });
  el.previewReport.addEventListener("click", function () {
    var href = el.openReport.getAttribute("href");
    if (href && href !== "#") openPreview(href, "刚刚生成的报告");
  });

  /* ───────────────────────────── 启动 ───────────────────────────── */

  buildStepper();
  loadRuns();
  var saved = sessionStorage.getItem(STORE_KEY);
  if (saved) resumeJob(saved);
})();