/* newui/js/fine/corner-buttons.js — 「角落按钮」自定义空间模块（源自 ui-skinset.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'corner-buttons',
  source: 'ui-skinset.js',
  space: '角落按钮',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  function bindCornerButtons(){
    const btns = [$('settings-gear'), $('theme-hanger')].filter(Boolean);
    if (!btns.length) return;
    /* 初始即 dim（除非已 hover —— 见下方 mousemove 首帧） */
    btns.forEach(b => b.classList.add('dim'));
    document.addEventListener('mousemove', ev => {
      const mx = ev.clientX, my = ev.clientY;
      let near = false;
      for (const b of btns){
        const r = b.getBoundingClientRect();
        const cx = r.left + r.width/2, cy = r.top + r.height/2;
        const dist = Math.hypot(mx - cx, my - cy);
        const hitR = Math.max(36, r.width/2 + 22);   /* 比按钮大一圈 */
        if (dist <= hitR){ near = true; break; }
      }
      btns.forEach(b => b.classList.toggle('dim', !near));
    });
    document.addEventListener('mouseleave', () => btns.forEach(b => b.classList.add('dim')));
  }

  /* ---- 顶部搜索框（老 initSearch L5806-5920 移植） ---- */
`;
    const __epi = `  for (const __n of ["bindCornerButtons"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
