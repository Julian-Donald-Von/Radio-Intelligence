/* newui/js/lyric-canvas.js — 歌词海报画布，逐位移植自 poster.js。
 * 只画文字（封面/模糊由 CSS 背景层承担）；指数缓动滚动、3 行窗口、DPR 封顶 1.5、零阴影。
 */
const FONT = '"Microsoft YaHei","PingFang SC",system-ui,sans-serif';    // :66
const LYRIC_EASE = 0.16;          // :158
const HOVER_RETURN_MS = 2000;     // :46
let cv, ctx, W = 0, H = 0;
let lines = [], lyricCur = -1, viewPos = 0, viewPosInit = false;
let hoverIdx = -1, hoverAt = 0, raf = 0;

export function mount(canvas){
  cv = canvas || document.getElementById('poster-canvas');
  if (!cv) return false;
  ctx = cv.getContext('2d');
  resize();
  window.addEventListener('resize', resize);
  if (!raf) raf = requestAnimationFrame(loop);
  return true;
}
function resize(){                                        // :77-85
  if (!cv) return;
  const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
  W = Math.max(2, Math.floor(cv.clientWidth * dpr));
  H = Math.max(2, Math.floor(cv.clientHeight * dpr));
  if (cv.width !== W || cv.height !== H){ cv.width = W; cv.height = H; }
}
const LH = () => Math.max(30, H * 0.082);                 // :60

export function setLyricLines(arr){                       // :258-268
  lines = (arr || []).filter(l => l && l.text != null)
    .map(l => ({ time: +l.time || 0, text: String(l.text), dur: +l.dur || 4.8 }))
    .sort((a, b) => a.time - b.time);
  lyricCur = 0; viewPos = 0; viewPosInit = false; hoverIdx = -1; hoverAt = 0;
}
export function seekLyric(ms){                            // :270-277
  if (!lines.length){ lyricCur = -1; return; }
  const t = ms / 1000;
  let idx = -1;
  for (let i = 0; i < lines.length; i++){
    if (lines[i].time <= t) idx = i; else break;
  }
  lyricCur = idx;                                         // 只改目标，不碰 viewPos
}
export function wheelLyric(d){                            // :313-317
  if (!lines.length) return;
  const base = hoverIdx >= 0 ? hoverIdx : (lyricCur < 0 ? 0 : lyricCur);
  const i = Math.max(0, Math.min(lines.length - 1, base + (d|0)));
  if (i !== hoverIdx){ hoverIdx = i; hoverAt = performance.now(); }
}

function loop(){
  raf = requestAnimationFrame(loop);
  if (!ctx || !W || !H) return;
  const now = performance.now();
  if (hoverIdx >= 0 && hoverIdx !== lyricCur && hoverAt && now - hoverAt > HOVER_RETURN_MS){
    hoverIdx = -1; hoverAt = 0;                           // :210-212 2s 回正
  }
  draw();
}
function draw(){
  ctx.clearRect(0, 0, W, H);                              // :105 每帧清屏（不可省）
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  if (!lines.length) return;
  const target = hoverIdx >= 0 ? hoverIdx : (lyricCur < 0 ? 0 : lyricCur);  // :61-64
  if (!viewPosInit){ viewPos = target; viewPosInit = true; }
  else {
    const dv = target - viewPos;
    if (Math.abs(dv) > 1e-4) viewPos += dv * LYRIC_EASE; // :171 指数缓动
    else viewPos = target;                                // :172 咬死防浮点余振
  }
  const center = Math.round(viewPos);
  const lh = LH(), cy = H * 0.52;                         // :166
  for (let i = center - 1; i <= center + 1; i++){         // :179 恒 3 行
    if (i < 0 || i >= lines.length) continue;
    const dist = Math.abs(i - viewPos);
    const y = cy + (i - viewPos) * lh;
    const isFocus = (i === hoverIdx);
    const size = Math.round(isFocus ? H * 0.0408 * 1.32
                    : (dist < 0.5 ? H * 0.0408 : H * 0.022));       // :186-187
    const alpha = (dist < 0.5 || isFocus) ? 1 : 0.20;               // :189
    ctx.font = '700 ' + size + 'px ' + FONT;
    ctx.fillStyle = 'rgba(255,255,255,' + alpha.toFixed(3) + ')';    // :192 零阴影
    ctx.fillText(String(lines[i].text || ''), W/2, y);
  }
}
export function destroy(){ if (raf) cancelAnimationFrame(raf); raf = 0; }
export const _diag = {
  lines: () => lines.length, cur: () => lyricCur, viewPos: () => viewPos,
  size: () => ({ W, H }),
};
