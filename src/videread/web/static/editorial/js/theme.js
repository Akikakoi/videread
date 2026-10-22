/* ── 主题两档切换：浅色 ↔ 深色（滑块开关） ──
   历史 system 档已废弃：首次加载按当时系统外观迁移到显式档位并写回。 */
(function(){
  const ORDER = ["light","dark"];
  const LABEL = {light:"浅色",dark:"深色"};
  let current = (function(){
    let t; try{ t = localStorage.getItem("videread.theme"); }catch(e){}
    if(t === "light" || t === "dark") return t;
    const resolved = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    try{ localStorage.setItem("videread.theme", resolved); }catch(e){}
    return resolved;
  })();
  const btn = document.getElementById("theme-toggle");
  const labelEl = btn ? btn.querySelector(".theme-label") : null;
  const label = () => {
    if(!btn) return;
    btn.dataset.appearance = current;
    if(labelEl) labelEl.textContent = LABEL[current];
    btn.setAttribute("aria-label","主题：" + LABEL[current]);
  };
  label();
  if(btn) btn.addEventListener("click", () => {
    current = ORDER[(ORDER.indexOf(current) + 1) % ORDER.length];
    try{ localStorage.setItem("videread.theme", current); }catch(e){}
    document.documentElement.classList.add("theme-switching");
    document.documentElement.dataset.theme = current;
    label();
    /* 禁过渡窗口要盖住最长的 --t-slow=.8s，否则切换瞬间恰有 reveal 在播会被截断加速 */
    setTimeout(() => document.documentElement.classList.remove("theme-switching"), 850);
  });
})();
