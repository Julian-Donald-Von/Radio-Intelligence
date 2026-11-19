/* newui/js/fine/settings-fold.js — 「折叠委托」自定义空间模块（源自 ui-settings.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'settings-fold',
  source: 'ui-settings.js',
  space: '折叠委托',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  /* ═══ 折叠委托（高级选项 .set-fold-head + 动态分组 h4）═══
     ⚠ 只接管 **动态区**（#settings-dynamic 内、非 data-foldable）的分组，
     避免与 ui-skinset.js 里 data-foldable 静态分组的委托重复触发。 */
  function initSettingsFolds(){
    if (initSettingsFolds._done) return;
    initSettingsFolds._done = true;
    try { buildFxWater(); } catch (_) {}   // 2026-09-19：高级·视觉效果（水面调参）
    document.addEventListener('click', function(e){
      const head = e.target && e.target.closest ? e.target.closest('.set-fold-head') : null;
      if (head){
        const fold = head.parentElement;
        if (!fold) return;
        fold.classList.toggle('open');
        if (fold.dataset.fold){
          try {
            localStorage.setItem('setfold:' + fold.dataset.fold,
                                 fold.classList.contains('open') ? '1' : '0');
          } catch (_) {}
        }
        return;
      }
      const h4 = e.target && e.target.closest
        ? e.target.closest('.set-group[data-gid]:not([data-foldable]) > h4') : null;
      if (h4){
        const g = h4.parentElement;
        if (!g) return;
        g.classList.toggle('collapsed');
        if (g.dataset.gid){
          try {
            localStorage.setItem('setgroup:' + g.dataset.gid,
                                 g.classList.contains('collapsed') ? '1' : '0');
          } catch (_) {}
        }
      }
    });
    const qc = $('qr-close');
    if (qc) qc.addEventListener('click', closeQrModal);
    const qm = $('qr-modal');
    if (qm) qm.addEventListener('click', function(e){ if (e.target === qm) closeQrModal(); });
  }

`;
    const __epi = `  for (const __n of ["initSettingsFolds"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
