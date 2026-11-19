/* newui/js/fine/volume.js — 「音量」自定义空间模块（源自 ui-dock.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'volume',
  source: 'ui-dock.js',
  space: '音量',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  function setVolFromEvent(e){
    const r = volEl.getBoundingClientRect();
    st.volume = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
    st.muted = false;
    render();
    post('/volume', 'vol=' + st.volume);
  }
  volEl.addEventListener('mousedown', e => {
    const move = ev => setVolFromEvent(ev);
    const up = () => {
      window.removeEventListener('mousemove', move);
      window.removeEventListener('mouseup', up);
    };
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', up);
    setVolFromEvent(e);
  });

  $('vol-icon').onclick = () => {
    st.muted = !st.muted;
    render();
    post('/volume', 'vol=' + (st.muted ? 0 : st.volume));
  };

  // ⚠ 外围进度条精确 seek（原 ui/ai-radio-player.html L2807-2854）
  // 关键：不按 ratio 换算，而是把路径离散采样 120 段，逐点算到鼠标的欧氏距离平方，
  // 取最小者对应的弧长 best → frac2 = best / len。
  // 这是用户反馈"进度条点不准"的根因修复。
`;
    const __epi = `  for (const __n of ["setVolFromEvent"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
