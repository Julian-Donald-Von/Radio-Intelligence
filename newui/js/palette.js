/* newui/js/palette.js — 封面取色，逐位移植自 ui-core.js:205-232。
 * 32×32 canvas → 忽略 alpha<128 → RGB 均值 → --primary=+30 / --accent=+70。
 */
let lastUrl = '';

export function applyCoverPalette(url){
  if (!url || url === lastUrl) return;
  lastUrl = url;
  try {
    const img = new Image();
    img.crossOrigin = 'anonymous';                    // :206
    img.onload = () => {
      try {
        const cv = document.createElement('canvas');
        cv.width = cv.height = 32;                    // :208
        const g = cv.getContext('2d');
        g.drawImage(img, 0, 0, 32, 32);
        const d = g.getImageData(0, 0, 32, 32).data;
        let r = 0, gg = 0, b = 0, n = 0;
        for (let i = 0; i < d.length; i += 4){
          if (d[i+3] < 128) continue;                 // :212 忽略透明像素
          r += d[i]; gg += d[i+1]; b += d[i+2]; n++;
        }
        if (!n) return;
        r = Math.round(r/n); gg = Math.round(gg/n); b = Math.round(b/n);
        const hex = (x, y, z) => '#' + [x,y,z].map(v =>
          Math.max(0, Math.min(255, v)).toString(16).padStart(2,'0')).join('');  // :221-223
        const root = document.documentElement.style;
        root.setProperty('--primary', hex(r+30, gg+30, b+30));   // :226
        root.setProperty('--accent',  hex(r+70, gg+70, b+70));   // :227
      } catch (_e) { /* 跨域取色失败保留默认 */ }
    };
    img.src = url;
  } catch (_e) {}
}

export const _diag = { lastUrl: () => lastUrl };
