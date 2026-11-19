/* newui/js/fine/ring-progress.js — 「SVG进度环/播放头」自定义空间模块（源自 ui-core.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'ring-progress',
  source: 'ui-core.js',
  space: 'SVG进度环/播放头',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  /* ---------- 外围环绕进度（移植原 computeFramePath） ---------- */
  const framePath=$('frame-path'), frameProg=$('frame-progress'), frameGlow=$('frame-glow'),
        frameBloom=$('frame-bloom'), frameBloom2=$('frame-bloom2'), frameHit=$('frame-hit'),
        playhead=$('frame-playhead'), playheadCore=$('frame-playhead-core');
  let pathD='', frameLen=0;

  function computeFramePath(){
    const r = $('dock').getBoundingClientRect();
    const x=0, y=0, w=r.width, h=r.height, rad=h*.45;
    /* 起点：底部边框正中，逆时针 下→左→上→右→回底部中点 */
    pathD = \`M \${x + w/2} \${y + h}
      L \${x + rad} \${y + h}
      A \${rad} \${rad} 0 0 1 \${x} \${y + h - rad}
      L \${x} \${y + rad}
      A \${rad} \${rad} 0 0 1 \${x + rad} \${y}
      L \${x + w - rad} \${y}
      A \${rad} \${rad} 0 0 1 \${x + w} \${y + rad}
      L \${x + w} \${y + h - rad}
      A \${rad} \${rad} 0 0 1 \${x + w - rad} \${y + h}
      L \${x + w/2} \${y + h}\`;
    [framePath, frameProg, frameGlow, frameBloom, frameBloom2, frameHit]
      .forEach(el => el && el.setAttribute('d', pathD));
    try { frameLen = framePath.getTotalLength(); } catch(_) { frameLen = 0; }
    _frameProgLen = -1;   /* 路径变了 → 强制 updateFrameProgress 重新写入一次（原 UI L2732） */
  }
  /* 上次写入的进度长度；<0 表示需要强制刷新（路径重算 / 首次）。 */
  let _frameProgLen = -1;
  /* ★ 2026-09-19：换曲时"把进度环补满并多留 260ms"的定时器（见 ui-view.js track_changed） */
  let ringHoldTimer = 0;

  /* ⚠ 播放头「靠近渐显 + 播放态呼吸」—— 原 UI L2757-2763 + L2798-2806。
     进度条"动画不对"的主因就是缺了这套：原 UI 的播放头不是常显，
     而是**鼠标靠近才出现**，播放时做正弦呼吸（.55 ± .13）。
     ⚠ 监听器**不在这里绑定**：dock 是本文件后面用 const 声明的（L1030），
     在此处立即执行会撞 TDZ。绑定统一放到下方「交互」区的 bindFrameHeadProximity()。 */
  let frameHeadNear = false;
  const HEAD_NEAR = 46;
  function bindFrameHeadProximity(){
    dock.addEventListener('mousemove', e => {
      const r = dock.getBoundingClientRect();
      const mx = e.clientX - r.left, my = e.clientY - r.top;
      const cx = parseFloat(playhead.getAttribute('cx')) || 0;
      const cy = parseFloat(playhead.getAttribute('cy')) || 0;
      frameHeadNear = Math.hypot(mx - cx, my - cy) < HEAD_NEAR;
    });
    const dh = $('dock-hit');
    if (dh) dh.addEventListener('mouseleave', () => { frameHeadNear = false; });
  }

  function updateFrameProgress(){
    if (!frameLen) return;
    const ratio = (st.duration > 0) ? Math.min(1, st.position / st.duration) : 0;
    const len = frameLen * ratio;
    /* ⚠ 节流：进度没实质变化（<0.5px）就不改写 stroke-dasharray。
       本函数每帧调用，无脑改写等于每帧重绘 4 条 SVG 路径
       （其中 bloom 还带 blur 滤镜）→ 疯狂闪烁（原 UI L2740-2743 同款坑）。 */
    if (_frameProgLen < 0 || Math.abs(len - _frameProgLen) >= 0.5){
      _frameProgLen = len;
      /* ⚠ dasharray 必须是 \`\${len} \${frameLen}\`（dash=已播实线, gap=足够长保证后面全空）
         + dashoffset=0。早期单值 \`frameLen\` + offset 换算是错的（原 UI L2746-2751）。 */
      const da = \`\${len} \${frameLen}\`;
      [frameProg, frameGlow, frameBloom, frameBloom2].forEach(el => {
        if (!el) return;
        el.style.strokeDasharray = da;
        el.style.strokeDashoffset = 0;
      });
      try {
        const p = framePath.getPointAtLength(Math.max(1, len));
        playhead.setAttribute('cx', p.x); playhead.setAttribute('cy', p.y);
        playheadCore.setAttribute('cx', p.x); playheadCore.setAttribute('cy', p.y);
      } catch(_) {}
    }
    /* 靠近时渐显：播放态呼吸，离开渐变消失（与几何解耦，每帧都要跑） */
    try {
      if (frameHeadNear || seeking){
        if (st.playing){
          const breathe = .55 + Math.sin(performance.now() * .0021) * .13;
          playhead.style.opacity = breathe;
        } else {
          playhead.style.opacity = 1;
        }
      } else {
        playhead.style.opacity = 0;
      }
    } catch(_) {}
  }

`;
    const __epi = `  for (const __n of ["framePath", "pathD", "computeFramePath", "_frameProgLen", "ringHoldTimer", "frameHeadNear", "HEAD_NEAR", "bindFrameHeadProximity", "updateFrameProgress"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
