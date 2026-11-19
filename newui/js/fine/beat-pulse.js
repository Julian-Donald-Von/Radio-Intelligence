/* newui/js/fine/beat-pulse.js — 「封面音频律动」自定义空间模块（源自 ui-core.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'beat-pulse',
  source: 'ui-core.js',
  space: '封面音频律动',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  /* ---------- 封面音频律动（body.beat-on 时启用） ----------
     bass = 频谱前 3 段均值 → 驱动 dock 封面（dc-cover-img）scale 微胀
     （老 UI L2977 dcCoverImg beatPulse 同款语义，但用**真实频谱**）。
     ⚠ 只动 dc-cover-img 的 transform —— 它是 dock 内静态封面，无其它
       写入方（老 UI 已这么干）。绝不碰：
         · #bgimg/#bgvideo filter/transform（acrylic/translateY 三层模型）
         · .scard transform（ui-cards layout 每帧写，覆盖即失效/闪跳）
     归零：停止播放 / 无频谱 2s / 关闭开关 → 恢复原状。 */
  function beatLevel(){
    const sp = window.__spectrum;
    if (!sp || !st.playing) return 0;
    if (performance.now() - (window.__specAt || 0) > 2000) return 0;  // 频谱过期
    let s = 0, n = 0;
    for (let i = 0; i < Math.min(4, sp.length); i++){ s += Number(sp[i]) || 0; n++; }
    if (!n) return 0;
    return Math.max(0, Math.min(1, s / n));
  }
  /* 封面律动实际执行体（幂等 memo：transform 不变就不写，避免每帧扰动）。
     rAF mainLoop 与 EQ interval 心跳双入口调用 —— offscreen/后台页 rAF
     零帧时靠 interval 保底驱动。返回当前电平 v（供断言/测试）。 */
  function beatPulse(){
    if (!document.body.classList.contains('beat-on')) return 0;
    const v = beatLevel();
    try {
      const ci = $('dc-cover-img');
      if (ci){
        if (v > 0.01){
          const s = (1 + v * 0.025).toFixed(4);        // ≤2.5% 微胀
          if (ci.style.transform !== 'scale(' + s + ')') ci.style.transform = 'scale(' + s + ')';
        } else if (ci.style.transform){
          ci.style.transform = '';
        }
      }
    } catch (_) {}
    return v;
  }
  function beatTick(){
    return beatPulse();
  }

`;
    const __epi = `  for (const __n of ["beatLevel", "beatPulse", "beatTick"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
