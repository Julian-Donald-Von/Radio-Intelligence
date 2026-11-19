/* newui/js/fine/beat-switch.js — 「音频律动开关」自定义空间模块（源自 ui-skinset.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'beat-switch',
  source: 'ui-skinset.js',
  space: '音频律动开关',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  /* ═══ 音频律动开关（body.beat-on 门控 beatTick 封面呼吸）═══ */
  let uiBeat = true;   // 默认开（与后端 _prefs_defaults.beat 一致）
  function setBeat(on){
    uiBeat = !!on;
    document.body.classList.toggle('beat-on', uiBeat);
    persistPrefs({ beat: uiBeat }, true);
    syncBeatUI();
    if (!uiBeat){
      /* 关闭时立即清掉封面残留 transform（beatTick 不再动它） */
      try { const ci = $('dc-cover-img'); if (ci) ci.style.transform = ''; } catch (_) {}
    }
  }
  function syncBeatUI(){
    const t = $('beat-toggle');
    if (t) t.checked = uiBeat;
  }

`;
    const __epi = `  for (const __n of ["uiBeat", "setBeat", "syncBeatUI"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
