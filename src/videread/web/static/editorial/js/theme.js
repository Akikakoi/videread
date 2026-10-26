/* ── 主题三挡：自动（跟随系统） / 浅色 / 深色 ──
   「自动」按 prefers-color-scheme 实时解析，系统外观变化时立即跟随；
   显式档位照旧。页头滑块（工单页保留）点一下从当前解析结果切到相反的
   显式档位；设置弹窗的分段钮经 window.videreadTheme.get()/set() 复用。
   页头没有 #theme-toggle 时所有 DOM 操作安全跳过。 */
(function(){
  const LABEL = {light:"浅色",dark:"深色"};
  const system = matchMedia("(prefers-color-scheme: dark)");
  function stored(){
    try{
      const t = localStorage.getItem("videread.theme");
      return (t === "light" || t === "dark" || t === "auto") ? t : null;
    }catch(e){ return null; }
  }
  /* 无存储视为「自动」并写回，之后系统换外观无需再猜 */
  let pref = stored();
  if(!pref){
    pref = "auto";
    try{ localStorage.setItem("videread.theme", pref); }catch(e){}
  }
  const btn = document.getElementById("theme-toggle");
  const labelEl = btn ? btn.querySelector(".theme-label") : null;
  const resolved = () => (pref === "auto" ? (system.matches ? "dark" : "light") : pref);
  const render = () => {
    const r = resolved();
    document.documentElement.dataset.theme = r;
    if(btn){
      btn.dataset.appearance = r;
      if(labelEl) labelEl.textContent = LABEL[r];
      btn.setAttribute("aria-label", "主题：" + LABEL[r] + (pref === "auto" ? "（跟随系统）" : ""));
    }
  };
  let switchTimer = null;
  function apply(t){
    if((t !== "auto" && t !== "light" && t !== "dark") || t === pref) return;
    pref = t;
    try{ localStorage.setItem("videread.theme", pref); }catch(e){}
    document.documentElement.classList.add("theme-switching");
    render();
    /* 禁过渡窗口要盖住最长的 --t-slow=.8s，否则切换瞬间恰有 reveal 在播会被截断加速 */
    clearTimeout(switchTimer);
    switchTimer = setTimeout(() => document.documentElement.classList.remove("theme-switching"), 850);
  }
  render();
  if(btn) btn.addEventListener("click", () => apply(resolved() === "dark" ? "light" : "dark"));
  /* 自动挡下系统外观切换实时跟随；显式挡不监听 */
  const onSystemChange = () => { if(pref === "auto") render(); };
  if(system.addEventListener) system.addEventListener("change", onSystemChange);
  else if(system.addListener) system.addListener(onSystemChange); /* 旧 Safari/Edge 兜底 */
  window.videreadTheme = { get: () => pref, set: apply };
})();
