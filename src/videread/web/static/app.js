/* videread 控制台：表单提交、SSE 进度、报告库与预览
   所有来自服务端 / 视频元信息的内容都用 textContent 写入，避免标题里的标记被执行。 */

(function () {
  "use strict";

  var STAGES = ["下载", "音频处理", "转写", "规范化", "结构规划", "截图", "逐节写作", "渲染"];
  var STAGE_COUNT = STAGES.length;
  // 逐节写作在 STAGES 里的序号（1 起）：只有这个阶段有「第 k/n 节」的子进度
  var WRITER_STAGE = STAGES.indexOf("逐节写作") + 1;
  var SECTION_RE = /第\s*(\d+)\/(\d+)\s*节/;
  // 服务端日志里带 LLM 用量读数（累计值，如 "12.3k tokens"）
  var TOKEN_RE = /(\d+(?:\.\d+)?)k tokens/;
  var STORE_KEY = "videread.job";

  var el = {
    form: document.getElementById("job-form"),
    url: document.getElementById("url"),
    probeHint: document.getElementById("probe-hint"),
    pageGroup: document.getElementById("page-group"),
    page: document.getElementById("page"),
    mode: document.getElementById("mode"),
    asr: document.getElementById("asr-backend"),
    modeHint: document.getElementById("mode-hint"),
    asrHint: document.getElementById("asr-hint"),
    noCache: document.getElementById("no-cache"),
    keepAudio: document.getElementById("keep-audio"),
    frames: document.getElementById("frames"),
    submit: document.getElementById("submit"),
    formError: document.getElementById("form-error"),
    progress: document.getElementById("progress"),
    pill: document.getElementById("state-pill"),
    spinner: document.getElementById("spinner"),
    elapsed: document.getElementById("elapsed"),
    tokens: document.getElementById("tokens"),
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
  var tokenText = null;    // LLM 累计用量读数（来自日志，服务端给的已是累计值）
  var currentState = "queued";
  var startedAt = 0;
  var timerId = null;
  var runsCache = [];
  var probeCache = { key: null, promise: null };  // 同一输入值只预检一次
  var probeTooLong = false;                        // 当前输入是否已被判超长
  var probeTimer = null;                           // 输入防抖：粘贴后自动预检的定时器
  var PROBE_DEBOUNCE_MS = 500;                     // 停止输入半秒后再探测，逐字输入不刷请求
  var probePages = [];                             // 预检拿到的分P列表（多P视频才有值）
  var probeLimitSeconds = 7200;                    // 服务端下发的时长上限

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

  /* LLM 用量：日志里出现读数就显示，没读到就不显示。
     服务端给的是同一个 client 的累计值，所以取最后一次即可覆盖全流程。 */
  function trackTokens(line) {
    var match = TOKEN_RE.exec(line || "");
    if (!match) return;
    tokenText = match[1] + "k";
    renderTokens();
  }

  function renderTokens() {
    if (tokenText === null) {
      el.tokens.hidden = true;
      return;
    }
    el.tokens.hidden = false;
    el.tokens.textContent = "token " + tokenText;
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
    tokenText = null;
    currentState = "queued";
    startedAt = 0;
    stopTimer();
    el.log.textContent = "";
    el.spinner.hidden = true;
    el.elapsed.hidden = true;
    el.elapsed.textContent = "已用 00:00";
    renderTokens();
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

  /* ──────────────── 提交前预检：时长与 2 小时上限 ──────────────── */

  function currentSource() {
    return el.url.value.trim();
  }

  function showProbeHint(text, tone) {
    el.probeHint.textContent = text || "";
    if (tone) {
      el.probeHint.dataset.tone = tone;
    } else {
      delete el.probeHint.dataset.tone;
    }
    show(el.probeHint, Boolean(text));
  }

  /* 预检进行中：转圈 + 文案。textContent 一旦被结果覆盖，spinner 自然移除 */
  function showProbeLoading() {
    el.probeHint.textContent = "";
    delete el.probeHint.dataset.tone;
    el.probeHint.appendChild(node("span", "spinner probe-spinner"));
    el.probeHint.appendChild(document.createTextNode(" 正在获取视频信息…"));
    show(el.probeHint, true);
  }

  /* 两个下拉下方的一句话说明。用大白话，避免只看到选项名不知差别在哪 */
  var FIELD_HINTS = {
    mode: {
      standard: "内容完整，篇幅随视频信息量伸缩，适合想深入理解时选。",
      brief: "只留主干结论，严格限制字数，几分钟就能读完。"
    },
    asr: {
      "": "用项目默认设置。视频自带字幕时会直接用字幕，不走转写。",
      dashscope: "阿里云标准通道，稳定；需账号开通云存储权限，未开通会失败。",
      "dashscope-realtime": "阿里云实时通道，不需要云存储权限；上一项报错时改用这个。",
      openai: "调用 OpenAI 转写，需要另外配置 OpenAI 密钥。"
    }
  };

  function syncFieldHints() {
    el.modeHint.textContent = FIELD_HINTS.mode[el.mode.value] || "";
    el.asrHint.textContent = FIELD_HINTS.asr[el.asr.value] || "";
  }

  function setProbeGate(tooLong) {
    probeTooLong = tooLong;
    el.submit.disabled = tooLong;
  }

  /* ──────────────── 分P 选择：多P 视频在提交前选好集数 ──────────────── */

  function hidePageSelector() {
    probePages = [];
    pageSelectorKey = null;
    el.page.textContent = "";
    show(el.pageGroup, false);
  }

  function selectedPage() {
    return parseInt(el.page.value, 10) || 1;
  }

  function selectedPageDuration() {
    var page = selectedPage();
    for (var i = 0; i < probePages.length; i++) {
      if (probePages[i].page === page) return probePages[i].duration;
    }
    return 0;
  }

  /* 把所选分P 写进提交链接：P1 保持裸链接（缓存与裸 BV 提交同目录），
     其他分P 追加 ?p=N；已有 p 参数则替换。裸 BV 号先补全成标准链接。 */
  function withPage(source, page) {
    var url = /^BV[0-9A-Za-z]{10}$/i.test(source)
      ? "https://www.bilibili.com/video/" + source
      : source;
    var queryAt = url.indexOf("?");
    if (queryAt < 0) return page > 1 ? url + "?p=" + page : url;
    var base = url.slice(0, queryAt);
    var params = url.slice(queryAt + 1).split("&").filter(function (pair) {
      return pair.split("=")[0] !== "p";
    });
    if (page > 1) params.push("p=" + page);
    return params.length ? base + "?" + params.join("&") : base;
  }

  function renderPageSelector(pages, current, rebuild) {
    probePages = pages;
    if (rebuild) {
      // 仅在换了一个输入源时重建选项；同视频的重复预检必须保留用户已选的分P
      el.page.textContent = "";
      pages.forEach(function (item) {
        var label = "P" + item.page + " " + (item.title || "") +
          " · " + fmtClock(item.duration);
        var option = node("option", null, label);
        option.value = String(item.page);
        el.page.appendChild(option);
      });
      el.page.value = String(current > 1 ? current : 1);
      if (el.page.selectedIndex < 0) el.page.selectedIndex = 0;
    }
    show(el.pageGroup, true);
  }

  // 分P 视频的时长/超长判定跟随所选分P；单P 视频走服务端给的 duration
  function applyDurationHint(data) {
    var durationText = data.duration_text;
    if (probePages.length) {
      var pageDuration = selectedPageDuration();
      if (!pageDuration) {
        showProbeHint("分P信息已变化，请重新预检", "bad");
        setProbeGate(true);
        return true;
      }
      durationText = fmtClock(pageDuration);
      if (pageDuration > probeLimitSeconds) {
        showProbeHint(
          "该分P时长 " + durationText + "，超过 " + data.limit_text +
          "上限，已禁止提交。", "bad");
        setProbeGate(true);
        return true;
      }
    } else if (data.too_long) {
      showProbeHint(
        "该视频时长 " + data.duration_text + "，超过 " + data.limit_text +
        "上限，已禁止提交。", "bad");
      setProbeGate(true);
      return true;
    }
    var parts = ["时长 " + durationText];
    if (data.uploader) parts.push("UP主 " + data.uploader);
    if (data.title) parts.push(data.title);
    var text = parts.join(" · ");
    // blur 重放防护：点击提示条上的按钮会先让输入框失焦，失焦复用缓存
    // 重放这条渲染路径；若此时重写 textContent，「查看上次报告 / 重新解析」
    // 会被拆掉重建，mousedown 与 mouseup 落在不同节点上，click 永远不触发。
    // 同一内容直接沿用现有 DOM。
    if (el.probeHint.textContent === text && !el.probeHint.hidden) {
      setProbeGate(false);
      return false;
    }
    showProbeHint(text);
    renderExistingHint(data.existing_run);
    setProbeGate(false);
    return false;
  }

  el.page.addEventListener("change", function () {
    if (probePages.length) applyDurationHint(lastProbeData);
  });

  /* 已解析过的视频：提示 + 「查看上次报告 / 重新解析」入口。
     普通提交仍走缓存（不覆盖旧报告）；重新解析落 -rN 新目录全量重跑。 */
  function renderExistingHint(existing) {
    if (!existing) return;
    var box = el.probeHint;
    // 幂等守卫：动作按钮已在位就不重建（配合 applyDurationHint 的重放防护）
    if (box.querySelector("button.probe-action")) return;
    box.appendChild(document.createTextNode(" · 已解析过（" + existing.generated_at + "）"));
    var view = node("a", "probe-action", "查看上次报告");
    view.href = existing.report_url;
    view.target = "_blank";
    view.rel = "noopener";
    var retry = node("button", "probe-action", "重新解析");
    retry.type = "button";
    retry.addEventListener("click", function () {
      var source = currentSource();
      if (!source || el.submit.disabled) return;
      submitJob({
        url: probePages.length ? withPage(source, selectedPage()) : source,
        mode: el.mode.value,
        no_cache: true,
        keep_audio: el.keepAudio.checked,
        frames: el.frames && el.frames.checked ? true : null,
        asr_backend: el.asr.value || null,
        retry: true
      });
    });
    box.appendChild(view);
    box.appendChild(retry);
  }

  // 同一输入值只探测一次：失焦给出即时反馈，提交时复用同一次结果
  function ensureProbe(value) {
    if (probeCache.key === value && probeCache.promise) return probeCache.promise;
    var promise = fetch("/api/probe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: value })
    })
      .then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (data) {
          if (!response.ok) return { ok: false, detail: data.detail };
          return { ok: true, data: data };
        });
      })
      .catch(function () { return { ok: false }; });
    probeCache = { key: value, promise: promise };
    return promise;
  }

  function renderProbeHint(result, probedValue) {
    if (!result.ok) {
      // 服务端返回了 400 + 具体原因（输入既不是链接也不是存在的本地文件、
      // 链接已失效等）：这是确定性失败，流水线跑到下载阶段也会同样失败，
      // 直接拦下并展示原因，不浪费一次提交。没有 detail（网络抖动等）
      // 才降级放行，交给执行时校验。
      if (result.detail) {
        showProbeHint("无法预检：" + result.detail, "bad");
        setProbeGate(true);
        hidePageSelector();
        pageSelectorKey = null;
        return true;
      }
      showProbeHint("无法预检时长，将在执行时校验");
      setProbeGate(false);
      hidePageSelector();
      pageSelectorKey = null;
      return false;
    }
    var data = result.data;
    lastProbeData = data;
    probeLimitSeconds = data.limit_seconds || 7200;
    var pages = data.pages || [];
    // 换输入源才重建选项；同一视频重复预检（如提交时复用）保留已选分P
    var rebuild = pageSelectorKey !== probedValue;
    pageSelectorKey = probedValue;
    if (pages.length > 1) {
      renderPageSelector(pages, data.current_page || 1, rebuild);
    } else {
      hidePageSelector();
    }
    return applyDurationHint(data);
  }

  // 最近一次预检的完整数据：切换分P时免二次请求即可刷新时长提示
  var lastProbeData = null;
  // 选项当前对应的输入源：区分「换视频」与「同视频重复预检」
  var pageSelectorKey = null;

  // 返回 Promise<是否超长>；输入为空时不拦截（交给提交处的空值校验）
  function runProbe(value) {
    if (!value) {
      showProbeHint("");
      setProbeGate(false);
      return Promise.resolve(false);
    }
    showProbeLoading();
    return ensureProbe(value).then(function (result) {
      if (currentSource() !== value) return false;  // 输入已改，丢弃过期结果
      return renderProbeHint(result, value);
    });
  }

  // 取消尚未触发的防抖预检（失焦/提交时会立即探测，不需要定时器再跑一次）
  function cancelProbeTimer() {
    if (probeTimer !== null) {
      window.clearTimeout(probeTimer);
      probeTimer = null;
    }
  }

  el.url.addEventListener("blur", function () {
    cancelProbeTimer();
    runProbe(currentSource());
  });
  el.url.addEventListener("change", function () { runProbe(currentSource()); });
  // 输入变化即失效旧结论，避免拿着上一个视频的判定挡住新输入；
  // 停止输入半秒后自动预检——粘贴整段链接后马上就能看到加载动画，
  // 不必等失焦，逐字输入也不会每个字符刷一次请求。
  el.url.addEventListener("input", function () {
    cancelProbeTimer();
    if (currentSource() !== probeCache.key) {
      showProbeHint("");
      setProbeGate(false);
      hidePageSelector();
    }
    var value = currentSource();
    if (!value) return;  // 空输入不预检（交给提交处的空值校验）
    probeTimer = window.setTimeout(function () {
      probeTimer = null;
      runProbe(value);
    }, PROBE_DEBOUNCE_MS);
  });

  el.mode.addEventListener("change", syncFieldHints);
  el.asr.addEventListener("change", syncFieldHints);

  function submitJob(payload) {
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
        el.submit.disabled = probeTooLong;
      });
  }

  el.form.addEventListener("submit", function (event) {
    event.preventDefault();
    var source = currentSource();
    // 回车提交不经过失焦：取消挂起的防抖预检，避免任务开跑后提示闪烁
    cancelProbeTimer();
    var payload = {
      url: source,
      mode: el.mode.value,
      no_cache: el.noCache.checked,
      keep_audio: el.keepAudio.checked,
      frames: el.frames && el.frames.checked ? true : null,
      asr_backend: el.asr.value || null
    };
    if (!source) {
      setFormError("请先填写 BV 号、视频链接或本地音视频路径。");
      return;
    }
    setFormError("");
    el.submit.disabled = true;
    // 点击提交会先触发失焦预检，但结果未必已回来；这里复用同一次探测，
    // 保证「超长」一定在创建任务之前被拦下。
    runProbe(source).then(function (tooLong) {
      if (tooLong) return;
      // 仅在分P选择器可见（多P 视频预检成功）时改写链接；
      // 其余情况（含手动带 ?p=N 的短链）提交原文，避免误改用户输入。
      payload.url = probePages.length ? withPage(source, selectedPage()) : source;
      submitJob(payload);
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
      trackTokens(line);
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
        (snapshot.logs || []).forEach(trackTokens);
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
        syncFieldHints();
        attachJob(jobId, true);
      })
      .catch(function () { sessionStorage.removeItem(STORE_KEY); });
  }

  /* ───────────────────────────── 报告库 ───────────────────────────── */

  function loadRuns() {
    fetch("/api/runs")
      .then(function (response) { return response.json(); })
      .then(function (data) {
        runsCache = data.runs || [];
        renderRuns();
      })
      .catch(function () {});
  }

  var WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

  // 按自然日归组：同一天解析的记录聚在一起，便于按时间翻历史
  function dayKey(ms) {
    var d = new Date(ms);
    return d.getFullYear() + "-" + (d.getMonth() + 1) + "-" + d.getDate();
  }

  function dayLabel(ms) {
    var d = new Date(ms);
    var now = new Date();
    var today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    var day = new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
    var text = (d.getMonth() + 1) + "月" + d.getDate() + "日";
    if (day === today) return "今天 · " + text;
    if (day === today - 86400000) return "昨天 · " + text;
    return text + " " + WEEKDAYS[d.getDay()];
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

    // 列表本身已按生成时间倒序，顺序切段即可得到「最新在前」的日期分组
    var groups = [];
    var seen = {};
    visible.forEach(function (run) {
      var key = run.mtime ? dayKey(run.mtime * 1000) : "unknown";
      if (!seen[key]) {
        seen[key] = { label: run.mtime ? dayLabel(run.mtime * 1000) : "时间未知", runs: [] };
        groups.push(seen[key]);
      }
      seen[key].runs.push(run);
    });

    groups.forEach(function (group) {
      var box = node("div", "run-group");
      box.appendChild(node("p", "run-group-title", group.label));
      group.runs.forEach(function (run) { box.appendChild(runCard(run)); });
      el.runs.appendChild(box);
    });
  }

  /* 收起所有展开的导出菜单：点其他位置、滚动或改窗口时调用
     （菜单是视口定位，页面一动位置就失效，直接收起最干净） */
  function closeExportMenus() {
    var open = document.querySelectorAll(".export-menu:not([hidden])");
    for (var i = 0; i < open.length; i++) open[i].hidden = true;
  }
  document.addEventListener("click", closeExportMenus);
  window.addEventListener("scroll", closeExportMenus, true);
  window.addEventListener("resize", closeExportMenus);

  function runCard(run) {
    var card = node("div", "run-card");

    card.appendChild(node("h3", "run-title", run.title || run.run_id));

    var meta = node("p", "run-meta");
    // 只留给人看的信息：profile / 节数是机器字段与内部统计，不在此处暴露
    [run.uploader ? "UP主 " + run.uploader : "",
     "时长 " + run.duration_text,
     run.bvid && run.bvid !== "local" ? run.bvid : "本地文件",
     run.generated_at || ""
    ].filter(Boolean).forEach(function (text) {
      meta.appendChild(node("span", null, text));
    });
    card.appendChild(meta);

    var actions = node("div", "run-actions");

    // 原视频只对远程链接可跳转；本地文件的 file:// 会被浏览器拦截
    if (/^https?:\/\//i.test(run.url || "")) {
      var origin = node("a", "btn btn-small", "查看原视频");
      origin.href = run.url;
      origin.target = "_blank";
      origin.rel = "noopener";
      actions.appendChild(origin);
    }

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

      // 导出统一收进下拉：PDF / PNG 长图 / Markdown 三种格式
      var dropdown = node("div", "export-dropdown");
      var trigger = node("button", "btn btn-small", "导出 ▾");
      trigger.type = "button";
      trigger.setAttribute("aria-haspopup", "true");
      var menu = node("div", "export-menu");
      menu.hidden = true;
      // 导出统一收进下拉：PDF / PNG 长图 / Markdown 三种格式。
      // 带截图的报告不给 MD：截图引用离开 run 目录就裂图（服务端同样拦截）
      var EXPORTS = [{ label: "PDF 文档", ext: "pdf" }, { label: "PNG 长图", ext: "png" }];
      if (!run.has_frames) EXPORTS.push({ label: "Markdown", ext: "markdown" });
      EXPORTS.forEach(function (item) {
        var link = node("a", "export-item", item.label);
        link.href = "/api/runs/" + encodeURIComponent(run.run_id) + "/" + item.ext;
        menu.appendChild(link);
      });
      if (run.has_frames) {
        menu.appendChild(node("p", "export-note", "带截图报告不提供 MD"));
      }
      trigger.addEventListener("click", function (event) {
        event.stopPropagation();
        var willOpen = menu.hidden;
        closeExportMenus();
        if (willOpen) {
          // 玻璃卡片的 backdrop-filter 会困住菜单的模糊采样与 z-index：
          // 打开时挂到 body 下，按触发按钮的位置做视口定位
          document.body.appendChild(menu);
          var rect = trigger.getBoundingClientRect();
          menu.style.top = rect.bottom + 6 + "px";
          menu.style.left = rect.left + "px";
          menu.hidden = false;
        }
      });
      dropdown.appendChild(trigger);
      dropdown.appendChild(menu);
      actions.appendChild(dropdown);
    } else {
      actions.appendChild(node("span", "tag", "无 report.html"));
    }

    var detailButton = node("button", "btn btn-small", "阶段耗时");
    detailButton.type = "button";
    actions.appendChild(detailButton);

    var deleteButton = node("button", "btn btn-small btn-danger", "删除");
    deleteButton.type = "button";
    deleteButton.addEventListener("click", function () { confirmDelete(run); });
    actions.appendChild(deleteButton);
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

    return card;
  }

  /* 删除记录：二次确认里说明后果（转写缓存一并删除，重新生成要重新付费）。
     确认后调删除接口，无论成败都刷新列表——删掉了列表变短，没删掉保持原状。 */
  function confirmDelete(run) {
    var label = run.title || run.run_id;
    var message = "确定删除「" + label + "」吗？\n\n" +
      "该记录的全部产物会被删除且无法恢复；\n" +
      "重新生成需要重新下载与转写（产生 ASR 费用）。";
    if (!window.confirm(message)) return;
    fetch("/api/runs/" + encodeURIComponent(run.run_id), { method: "DELETE" })
      .then(function (response) {
        if (!response.ok) {
          window.alert("删除失败（HTTP " + response.status + "），请重试。");
        }
        loadRuns();
      })
      .catch(function () {
        window.alert("无法连接本地服务，删除未执行。");
        loadRuns();
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
        // 耗时 / token 两列是右对齐数字，表头必须同样右对齐才不会与数据错位
        var HEAD_COLUMNS = [
          { label: "阶段", numeric: false },
          { label: "耗时", numeric: true },
          { label: "token", numeric: true },
          { label: "状态", numeric: false }
        ];
        HEAD_COLUMNS.forEach(function (column) {
          head.appendChild(node("th", column.numeric ? "num" : null, column.label));
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
  syncFieldHints();
  loadRuns();
  var saved = sessionStorage.getItem(STORE_KEY);
  if (saved) resumeJob(saved);
})();