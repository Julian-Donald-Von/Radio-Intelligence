/* newui/js/fine/bgmode.js — 「背景效果 poster/acrylic」自定义空间模块（源自 ui-skinset.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'bgmode',
  source: 'ui-skinset.js',
  space: '背景效果 poster/acrylic',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  /* 背景效果：poster（皮肤原样，默认）/ acrylic（亚克力 blur）。
     ⚠ R58-4 (#43) 语义（用户本轮回溯澄清）：
       皮肤（歌词海报 / 自定义图）= 可换**底层**（#bgimg 承载）；
       亚克力 = 在皮肤之上叠加的**可选明亮 blur 效果**（非暗色 brightness(.6)），
       无效果也是一种效果；歌词 / 操作 UI / 气泡全在效果层之上。
     · bgmode=poster（默认）：皮肤锐利显示 + 海报文字层（按钮就叫「无效果」）
     · bgmode=acrylic：皮肤明亮 blur（CSS body.bgmode-acrylic 驱动 #bgimg）*/
  function setBgMode(mode){
    uiBgMode = (mode === 'acrylic') ? 'acrylic' : 'poster';
    document.body.classList.toggle('bgmode-acrylic', uiBgMode === 'acrylic');
    persistPrefs({ bgmode: uiBgMode }, true);
    syncBgModeUI();
    try { if (window.Poster && window.Poster.setMode) window.Poster.setMode(uiBgMode); } catch (_) {}
    /* R59：切到任意背景效果时若还没有任何背景内容（无封面/无自定义壁纸）
       就补程序化默认皮肤 —— 否则 poster/acrylic 切换在无封面期看不出差异；
       有 src（封面/壁纸）时 ensureDefaultSkin 内部直接跳过。 */
    try {
      const bgi = $('bgimg');
      if (bgi && !bgi.getAttribute('src')) ensureDefaultSkin();
    } catch (_) {}
    /* R66 精简提示：按钮已高亮 + 背景即时变化 → 不再 toast 复述。 */
  }
  function syncBgModeUI(){
    const row = $('bg-mode-row'); if (!row) return;
    row.querySelectorAll('.wp-type').forEach(b =>
      b.classList.toggle('active', b.dataset.bgmode === uiBgMode));
  }

`;
    const __epi = `  for (const __n of ["setBgMode", "syncBgModeUI"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
