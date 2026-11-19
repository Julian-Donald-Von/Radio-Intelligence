/* newui/js/fine/cover-palette.js — 「封面取色」自定义空间模块（源自 ui-core.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'cover-palette',
  source: 'ui-core.js',
  space: '封面取色',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  /* ---------- 封面取色：更新 --primary / --accent（音量条/进度随之变色） ---------- */
  function applyCoverPalette(url){
    if (!url) return;
    const img = new Image();
    img.crossOrigin = 'anonymous';
    img.onload = () => {
      try {
        const cv = document.createElement('canvas');
        cv.width = cv.height = 32;
        const g = cv.getContext('2d');
        g.drawImage(img, 0, 0, 32, 32);
        const d = g.getImageData(0, 0, 32, 32).data;
        let r=0, gg=0, b=0, n=0;
        for (let i=0; i<d.length; i+=4){
          if (d[i+3] < 128) continue;
          r+=d[i]; gg+=d[i+1]; b+=d[i+2]; n++;
        }
        if (!n) return;
        r=Math.round(r/n); gg=Math.round(gg/n); b=Math.round(b/n);
        const hex = (x,y,z) => '#' + [x,y,z].map(v =>
          Math.max(0, Math.min(255, v)).toString(16).padStart(2,'0')).join('');
        // primary = 取样色（提亮），accent = 取样色（再提亮）
        document.documentElement.style.setProperty('--primary', hex(r+30, gg+30, b+30));
        document.documentElement.style.setProperty('--accent',  hex(r+70, gg+70, b+70));
      } catch(_) { /* 跨域取色失败则保留默认色 */ }
    };
    img.onerror = () => {};
    img.src = url;
  }

`;
    const __epi = `  for (const __n of ["applyCoverPalette"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
