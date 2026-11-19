/* newui/js/ring.js — SVG 环绕进度环，逐位移植自 ui-core.js:46-125 + ui-dock.js:271-287。
 * 关键坑全部保留：rad=h*.45 / sweep=1 / dasharray 双值+offset0 / 0.5px 节流 /
 * 播放头靠近才显 + 3s 正弦呼吸 / 120 段采样 seek。
 */
import { onChange, snapshot } from './player.js';

let path, prog, glow, bloom, bloom2, playhead, playheadCore;
let frameLen = 0, _progLen = -1, headNear = false, seeking = false;
let onSeek = null;

export function mount({ seek } = {}) {
  onSeek = seek || null;
  path = document.getElementById('ring-path');
  prog = document.getElementById('ring-progress');
  glow = document.getElementById('ring-glow');
  bloom = document.getElementById('ring-bloom');
  bloom2 = document.getElementById('ring-bloom2');
  playhead = document.getElementById('ring-playhead');
  playheadCore = document.getElementById('ring-playhead-core');
  if (!path) return false;
  compute();
  window.addEventListener('resize', () => { compute(); update(); });
  onChange(update);
  return true;
}

/* 照抄 ui-core.js:46-64 */
export function compute(){
  const host = document.getElementById('ring-host');
  if (!host || !path) return;
  const r = host.getBoundingClientRect();
  const w = r.width, h = r.height, rad = h * 0.45;
  // ui-core.js:50-59：底边正中起笔，下→左→上→右→回中点；四段 A rad rad 0 0 1
  const d = [
    'M ' + (w/2) + ',' + h,
    'L ' + rad + ',' + h,
    'A ' + rad + ' ' + rad + ' 0 0 1 0,' + (h - rad),
    'L 0,' + rad,
    'A ' + rad + ' ' + rad + ' 0 0 1 ' + rad + ',0',
    'L ' + (w - rad) + ',0',
    'A ' + rad + ' ' + rad + ' 0 0 1 ' + w + ',' + rad,
    'L ' + w + ',' + (h - rad),
    'A ' + rad + ' ' + rad + ' 0 0 1 ' + (w - rad) + ',' + h,
    'L ' + (w/2) + ',' + h,
  ].join(' ');
  [path, prog, glow, bloom, bloom2].forEach(el => el && el.setAttribute('d', d));
  try { frameLen = path.getTotalLength(); } catch (_e) { frameLen = 0; }
  _progLen = -1;                                    // :63 强制下一帧重写
}

/* 照抄 ui-core.js:89-125 */
export function update(){
  if (!path || !frameLen) return;
  const s = snapshot();
  const ratio = s.duration > 0 ? Math.min(1, s.position / s.duration) : 0;
  const len = frameLen * ratio;
  if (_progLen < 0 || Math.abs(len - _progLen) >= 0.5){   // :93-95 0.5px 节流
    _progLen = len;
    const da = len + ' ' + frameLen;                       // :101 双值
    [prog, glow, bloom, bloom2].forEach(el => {
      if (!el) return;
      el.style.strokeDasharray = da;
      el.style.strokeDashoffset = 0;
    });
    try {
      const p = path.getPointAtLength(Math.max(1, len));   // :107
      [playhead, playheadCore].forEach(el => {
        if (!el) return;
        el.setAttribute('cx', p.x); el.setAttribute('cy', p.y);
      });
    } catch (_e) {}
  }
  // :113-123 靠近才显 + 呼吸 / 暂停常亮
  if (headNear || seeking){
    playhead && (playhead.style.opacity = s.playing
      ? (0.55 + Math.sin(performance.now() * 0.0021) * 0.13).toFixed(3)
      : '1');
  } else if (playhead) playhead.style.opacity = '0';
}

/** 播放头靠近检测（ui-core.js:76-87） */
export function bindProximity(){
  const host = document.getElementById('ring-host');
  if (!host) return;
  host.addEventListener('mousemove', e => {
    if (!playhead) return;
    const r = host.getBoundingClientRect();
    const mx = e.clientX - r.left, my = e.clientY - r.top;
    const cx = parseFloat(playhead.getAttribute('cx') || '0');
    const cy = parseFloat(playhead.getAttribute('cy') || '0');
    headNear = Math.hypot(mx - cx, my - cy) < 46;          // :76 HEAD_NEAR
  });
  host.addEventListener('mouseleave', () => { headNear = false; });
}

/* 120 段采样 seek（ui-dock.js:271-287 照抄） */
export function seekAt(clientX, clientY, immediate){
  if (!frameLen || !snapshot().duration || !path || !onSeek) return;
  const host = document.getElementById('ring-host');
  const r = host.getBoundingClientRect();
  const pt = { x: clientX - r.left, y: clientY - r.top };
  let best = 0, bestD = 1e9;
  const steps = 120;
  try {
    for (let i = 0; i <= steps; i++){
      const L = frameLen * i / steps;
      const p = path.getPointAtLength(L);
      const dx = p.x - pt.x, dy = p.y - pt.y;
      const d = dx*dx + dy*dy;
      if (d < bestD){ bestD = d; best = L; }
    }
  } catch (_e) { return; }
  seeking = immediate === true;
  _progLen = -1;
  onSeek(best / frameLen * snapshot().duration);
}

export const _diag = {
  frameLen: () => frameLen,
  dash: () => prog ? prog.style.strokeDasharray : '',
  headOpacity: () => playhead ? playhead.style.opacity : '',
  headNear: () => headNear,
};
