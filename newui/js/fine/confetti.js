/* newui/js/fine/confetti.js — 「礼花粒子」自定义空间模块（源自 ui-dock.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'confetti',
  source: 'ui-dock.js',
  space: '礼花粒子',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  /* ---------- 礼花纸动效（原 ui/ai-radio-player.html L2961 spawnBurst） ----------
     收藏点亮红心时，从按钮中心迸发 16 颗彩色粒子（略微上抛 + 发光）。
     ⚠ 只在"从未收藏 → 点亮"时放；取消收藏不放（沿用原 UI 的 if (!wasFav) 语义）。 */
  function spawnBurst(el){
    if (!el) return;
    const r = el.getBoundingClientRect();
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    const colors = ['#ff5c7a', '#ffd166', '#7bf1a8', '#6ec7ff', '#c9a8ff', '#ff9f6e', '#ff8fa3'];
    const N = 16;
    for (let i = 0; i < N; i++){
      const p = document.createElement('span');
      const ang  = (i / N) * Math.PI * 2 + Math.random() * 0.4;
      const dist = 26 + Math.random() * 34;
      const size = 3 + Math.random() * 4;
      const col  = colors[i % colors.length];
      p.style.cssText = 'position:fixed;left:' + cx + 'px;top:' + cy + 'px;'
        + 'width:' + size + 'px;height:' + size + 'px;border-radius:50%;'
        + 'background:' + col + ';pointer-events:none;z-index:400;'
        + 'box-shadow:0 0 8px ' + col;
      document.body.appendChild(p);
      const dx = Math.cos(ang) * dist, dy = Math.sin(ang) * dist - 16;   // 略微上抛
      p.animate([
        { transform: 'translate(-50%,-50%) scale(1)', opacity: 1 },
        { transform: 'translate(' + (dx - size/2) + 'px,' + (dy - size/2) + 'px) scale(.15)', opacity: 0 }
      ], { duration: 650 + Math.random() * 250, easing: 'cubic-bezier(.16,1,.3,1)' })
       .onfinish = function(){ p.remove(); };
    }
  }

`;
    const __epi = `  for (const __n of ["spawnBurst"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
