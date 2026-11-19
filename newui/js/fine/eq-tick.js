/* newui/js/fine/eq-tick.js — 「EQ频谱条」自定义空间模块（源自 ui-core.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'eq-tick',
  source: 'ui-core.js',
  space: 'EQ频谱条',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  /* ---------- 均衡器（播放时跳动） ----------
     ═══ 音频响应：有真实频谱（__spectrum，后端 10Hz 推 16 段）优先驱动；
     无频谱（后端没开/未播放）退回原来的随机模拟。EQ 是 dock 装饰，常跳。 */
  let eqTimer = null;
  /* EQ 条刷新体（interval 与 offscreen 手动 tick 共用）。
     抽出来供 __r54.tickEq() 黑盒手动排帧 —— offscreen 下 setInterval 被
     节流到 ~1s，probe 靠 pump(400ms) 赌 tick 会 flaky。 */
  function eqTick(){
    const bars = Array.from(document.querySelectorAll('#eq i'));
    if (bars.length){
      const sp = window.__spectrum;
      if (sp && sp.length >= 5 && st.playing){
        for (let i = 0; i < bars.length; i++){
          const idx = Math.min(sp.length - 1, Math.floor((i + 0.5) * sp.length / bars.length));
          const v = Math.max(0, Math.min(1, Number(sp[idx]) || 0));
          bars[i].style.height = (4 + v * 16) + 'px';
        }
      } else {
        bars.forEach(b => { b.style.height = (4 + Math.random()*14) + 'px'; });
      }
    }
    /* ⚠ 封面律动保底心跳：rAF mainLoop 在 offscreen/后台页被节流到零帧，
       beatTick 只挂 rAF 会永远不驱动封面 → 这里随 EQ tick 同步驱动一次
       beatPulse（内部幂等 memo，与 rAF 双写不冲突）。 */
    try { beatPulse(); } catch (_) {}
  }
  function startEq(){
    if (eqTimer) return;
    eqTimer = setInterval(eqTick, 100);
  }
  function stopEq(){
    if (eqTimer){ clearInterval(eqTimer); eqTimer = null; }
    document.querySelectorAll('#eq i').forEach(b => { b.style.height = '4px'; });
    /* 停止播放 → 封面律动归零（残留 scale 清掉） */
    try { const ci = $('dc-cover-img'); if (ci && ci.style.transform) ci.style.transform = ''; } catch (_) {}
  }

`;
    const __epi = `  for (const __n of ["eqTimer", "eqTick", "startEq", "stopEq"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
