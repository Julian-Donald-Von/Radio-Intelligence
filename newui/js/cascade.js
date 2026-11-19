/* newui/js/cascade.js — 弹簧弧形卡片墙，逐位移植自 ui-cards.js（用户要求"照着旧 UI 抄"）。
 * 常量/公式/参数全部来自 ui-cards.js 原文，注释标 file:line。
 * 差异：新 UI 不依赖 ui-orbit.js 的弧心（无 orbit 圆盘），弧心改由卡片容器右缘推算，
 *       几何公式本身（同心弧 + 弹簧 + 缩放/透明/模糊）完全照抄。
 */

import { snapshot as playerState } from './player.js';

const CARD_W = 372, CARD_H = 120;                 // ui-cards.js:6
const SHELF_CARD_HALF = CARD_W * 1.06 / 2;        // :7
const ARC_STEP = 0.135;                            // :13
const STEP_Z = 40;                                 // :16
const VISIBLE = 5.6;                               // :17
const PAL = ['#7c6cff','#3ddc97','#ff9f6e','#6ec7ff','#c9a8ff','#ffd166'];  // :88

/* 弹簧（ui-cards.js:38-49 逐位照抄） */
function Spring(p){ this.p = p||0; this.v = 0; this.target = this.p; }
Spring.prototype.setTarget = function(t){ this.target = t; };
Spring.prototype.update = function(dt){
  const k = 180, d = 26;                            // :41
  const a = (this.target - this.p) * k - this.v * d;
  this.v += a * dt; this.p += this.v * dt;
  if (Math.abs(this.target - this.p) < 0.001 && Math.abs(this.v) < 0.01){
    this.p = this.target; this.v = 0;
  }
};

let spring = new Spring(0);
let cards = [];          // {el, lift, vis, _z,_op,_tf,_filt}
let items = [];
let lastT = 0, raf = 0, openAt = 0;
let hoverIdx = -1;
let onActivate = null;   // 点卡回调（由 ui.js 注入）

const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/** 封面取色（ui-cards.js:245-260 逐位照抄）：2×1 两像素 → --tone-a / --tone-b */
function sampleCoverColor(img, cb){
  function _try(){
    try {
      const cv = document.createElement('canvas');
      cv.width = 2; cv.height = 1;
      const ctx = cv.getContext('2d');
      ctx.drawImage(img, 0, 0, 2, 1);
      const d = ctx.getImageData(0, 0, 2, 1).data;
      cb('rgb(' + d[0] + ',' + d[1] + ',' + d[2] + ')',
         'rgb(' + d[4] + ',' + d[5] + ',' + d[6] + ')');
    } catch (_e) { cb(null, null); }
  }
  if (img.complete && img.naturalWidth) _try();
  else img.addEventListener('load', _try, { once: true });
}

/** 构建卡片（照抄 ui-cards.js:117-209 的 DOM 结构） */
export function build(list, { activate, reveal = true } = {}) {
  items = list || [];
  onActivate = activate || onActivate;
  const box = document.getElementById('cascade-cards');
  if (!box) return;
  box.innerHTML = '';
  cards = [];
  const cur = playerState().title;
  openAt = reveal ? performance.now() : 0;

  items.forEach((e, i) => {
    const el = document.createElement('div');
    el.className = 'scard' + (e.current || (cur && e.title === cur) ? ' playing' : '');
    el.dataset.i = i;
    el.style.setProperty('--tone-a', PAL[i % PAL.length]);
    el.style.setProperty('--tone-b', PAL[(i + 2) % PAL.length]);
    const bg = 'background:linear-gradient(140deg,' + PAL[i % PAL.length] + ',rgba(20,18,34,.9))';  // :143
    const badge = e.reason ? '<span class="sc-chip">' + esc(e.reason) + '</span>' : '';
    el.innerHTML =
      '<div class="sc-cover" style="' + bg + '">'
      + (e.cover ? '<img src="' + esc(e.cover) + '" alt="" onerror="this.style.display=\'none\'">' : '') + '</div>'
      + '<div class="sc-info"><div class="sc-name">' + esc(e.title || '未知曲目') + '</div>'
      + '<div class="sc-sub">' + esc(e.artist || '未知艺术家') + '</div></div>'
      + badge
      + '<div class="sc-eq"><i></i><i></i><i></i><i></i><i></i></div>'
      + '<div class="sc-prog"><span class="fill"></span></div>';
    box.appendChild(el);
    cards.push({ el, lift:0, vis:false, _z:'', _op:'', _tf:'', _filt:'' });  // :198
    if (e.cover) {
      const imgEl = el.querySelector('.sc-cover img');
      if (imgEl) sampleCoverColor(imgEl, (c1, c2) => {
        if (c1) el.style.setProperty('--tone-a', c1);
        if (c2) el.style.setProperty('--tone-b', c2);
      });
    }
    el.addEventListener('click', () => { if (onActivate) onActivate(e, i); });
    el.addEventListener('mouseenter', () => { hoverIdx = i; });
    el.addEventListener('mouseleave', () => { if (hoverIdx === i) hoverIdx = -1; });
  });

  spring.p = spring.target = 0;
  lastT = performance.now();
  if (!raf) raf = requestAnimationFrame(layout);
}

/** 弧心（替代 ui-orbit.js:orbitOC）：取容器右缘外一点，使中心卡贴右 */
function arcCenter(box){
  const rb = box.getBoundingClientRect();
  return { x: rb.width - 20, y: rb.height / 2 };
}

/* 每帧布局（ui-cards.js:375-436 逐位照抄公式） */
function layout(now){
  raf = requestAnimationFrame(layout);
  if (!cards.length) return;
  const dt = Math.min(0.05, (now - lastT) / 1000 || 0.016);   // :379
  lastT = now;
  spring.update(dt);
  const center = spring.p;
  const box = document.getElementById('cascade-cards');
  if (!box) return;
  const oc = arcCenter(box);
  const R_CARD = Math.max(160, oc.x - SHELF_CARD_HALF - 8);   // :391 等效

  for (let i = 0; i < cards.length; i++){
    const c = cards[i], e = items[i];
    const delta = i - center, absD = Math.abs(delta);
    if (absD > VISIBLE){                                      // :396
      if (c.vis){ c.el.style.visibility = 'hidden'; c.vis = false; }
      continue;
    }
    if (!c.vis){ c.el.style.visibility = 'visible'; c.vis = true; }

    const liftT = (hoverIdx === i) ? 1 : 0;
    c.lift += (liftT - c.lift) * (liftT > c.lift ? 0.20 : 0.13);   // :403

    let rev = 1;
    if (openAt){
      const raw = (now - openAt - absD * 70) / 420;            // :406
      rev = raw <= 0 ? 0 : raw >= 1 ? 1 : raw * raw * (3 - 2 * raw);
      if (rev <= 0){ c.el.style.opacity = 0; continue; }
    }

    const scale0 = absD < 0.5 ? 1.06 : Math.max(0.62, 0.98 - absD * 0.12);  // :414
    const scale  = scale0 * (1 + c.lift * 0.06) * (0.6 + 0.4 * rev);          // :415
    const op     = (absD < 0.5 ? 1 : Math.max(0.25, 1 - absD * 0.28)) * rev;  // :416
    const blur   = (absD > 0.5 && absD < 3) ? Math.min(2, (absD - 0.5) * 0.7) : 0;  // :418

    const arcA = delta * ARC_STEP;                             // :419
    const px = oc.x - R_CARD * Math.cos(arcA) + c.lift * 8;    // :420
    const py = oc.y + R_CARD * Math.sin(arcA) - c.lift * 14;   // :421
    const pz = -absD * STEP_Z + c.lift * 36;                   // :422
    const ry = Math.max(-4.5, Math.min(4.5, delta * 2.2)) - c.lift * 1.5;   // :423
    const zS = String(200 - Math.round(absD * 10));            // :425

    c.el.classList.toggle('center', absD < 0.5);
    c.el.classList.toggle('playing', !!(e && (e.current || (playerState().title && e.title === playerState().title))));

    const tf = 'translate3d(' + (px - CARD_W/2).toFixed(1) + 'px,'
      + (py - CARD_H/2).toFixed(1) + 'px,' + pz.toFixed(1) + 'px) rotateY('
      + ry.toFixed(2) + 'deg) scale(' + scale.toFixed(4) + ')';   // :428-430
    if (c._tf !== tf){ c.el.style.transform = tf; c._tf = tf; }
    if (c._op !== op.toFixed(3)){ c.el.style.opacity = op.toFixed(3); c._op = op.toFixed(3); }
    const filt = blur > 0 ? 'blur(' + blur.toFixed(2) + 'px)' : '';
    if (c._filt !== filt){ c.el.style.filter = filt; c._filt = filt; }
    if (c._z !== zS){ c.el.style.zIndex = zS; c._z = zS; }
  }
  updateCardProgress();
}

/* 进度（ui-cards.js:262-276 照抄） */
function updateCardProgress(){
  const s = playerState();
  if (!cards.length || !(s.duration > 0)) return;
  const pctW = (Math.max(0, Math.min(1, s.position / s.duration)) * 100).toFixed(2) + '%';
  for (let i = 0; i < cards.length; i++){
    const c = cards[i], e = items[i];
    if (!c || !e) continue;
    const fill = c.el.querySelector('.sc-prog .fill');
    if (!fill) continue;
    const want = (e.current || (s.title && e.title === s.title)) ? pctW : '0%';
    if (c._pw !== want){ fill.style.width = want; c._pw = want; }
  }
}

/** 轮转（ui-cards.js:439-443 照抄） */
export function step(dir){
  if (!cards.length) return;
  spring.setTarget(Math.max(0, Math.min(cards.length - 1, Math.round(spring.target) + dir)));
}
export function scrollToCurrent(){
  const i = items.findIndex(e => e && (e.current || (playerState().title && e.title === playerState().title)));
  if (i >= 0) spring.setTarget(i);
}

export function destroy(){ if (raf) cancelAnimationFrame(raf); raf = 0; cards = []; items = []; }
export const _diag = {
  spring: () => ({ p: spring.p, v: spring.v, target: spring.target }),
  cards: () => cards.length,
  constants: () => ({ CARD_W, CARD_H, ARC_STEP, STEP_Z, VISIBLE }),
};
