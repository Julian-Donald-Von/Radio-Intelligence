/* newui/js/fine/theme-apply.js — 「主题应用/皮肤网格」自定义空间模块（源自 ui-skinset.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'theme-apply',
  source: 'ui-skinset.js',
  space: '主题应用/皮肤网格',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  /* 把主题/高亮应用到 CSS 变量（老 applyTheme L1323） */
  function applyThemePalette(pal, withAccent){
    const r = document.documentElement.style;
    const p = pal || DEF_THEME;
    r.setProperty('--primary',   p.primary);
    r.setProperty('--secondary', p.secondary);
    r.setProperty('--dark',      p.dark);
    r.setProperty('--light',     p.light);
    if (withAccent !== false) r.setProperty('--accent', p.accent);
  }
  function hexToRgb(h){
    let v = String(h || '').replace('#','');
    if (v.length === 3) v = v.split('').map(c => c + c).join('');
    const n = parseInt(v.slice(0,6), 16);
    return (Number.isNaN(n)) ? null : [(n>>16)&255, (n>>8)&255, n&255];
  }
  /* 把偏好（主题 key + 高亮覆盖）整体应用到 UI */
  function applyPrefsTheme(){
    const t = THEMES.find(x => x.key === uiThemeKey) || DEF_THEME;
    if (uiAccent && hexToRgb(uiAccent)){
      applyThemePalette(t, false);
      document.documentElement.style.setProperty('--accent', uiAccent);
      document.documentElement.style.setProperty('--primary', uiAccent);
      lockAccent(true);
    } else {
      applyThemePalette(t);
      /* 默认主题 + 无高亮覆盖 → 放开关封动态取色（默认行为） */
      lockAccent(uiThemeKey !== 'default');
      if (uiThemeKey === 'default'){
        try { applyCoverPalette(st.cover); } catch (_) {}
      }
    }
    syncSkinUI();
  }
  /* 皮肤列表点亮态同步 */
  function syncSkinUI(){
    const box = $('skin-list');
    if (!box) return;
    box.querySelectorAll('.skin-card').forEach(c =>
      c.classList.toggle('active', c.dataset.key === uiThemeKey));
    const picker = $('fx-accent-picker');
    if (picker && uiAccent) picker.value = uiAccent;
  }

`;
    const __epi = `  for (const __n of ["applyThemePalette", "hexToRgb", "applyPrefsTheme", "syncSkinUI"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
