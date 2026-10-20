/* ── 主题三档切换：浅色 → 深色 → 跟随系统 ── */
(function(){
  const ORDER = ["light","dark","system"];
  const LABEL = {light:"浅色",dark:"深色",system:"自动"};
  let current;
  try{ current = localStorage.getItem("videread.theme") || "system"; }catch(e){ current = "system"; }
  const btn = document.getElementById("theme-toggle");
  const label = () => { if(btn){ btn.textContent = LABEL[current]; btn.setAttribute("aria-label","主题：" + LABEL[current]); } };
  label();
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", e => {
    let t; try{ t = localStorage.getItem("videread.theme"); }catch(err){}
    if((t || "system") !== "system") return;
    document.documentElement.dataset.theme = e.matches ? "dark" : "light";
  });
  if(btn) btn.addEventListener("click", () => {
    current = ORDER[(ORDER.indexOf(current) + 1) % ORDER.length];
    try{ localStorage.setItem("videread.theme", current); }catch(e){}
    document.documentElement.classList.add("theme-switching");
    const dark = current === "dark" || (current === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    label();
    /* 禁过渡窗口要盖住最长的 --t-slow=.8s，否则切换瞬间恰有 reveal 在播会被截断加速 */
    setTimeout(() => document.documentElement.classList.remove("theme-switching"), 850);
  });
})();
