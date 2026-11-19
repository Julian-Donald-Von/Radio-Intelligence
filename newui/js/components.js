/* newui/js/components.js — 共享 UI 组件（v1 两个，后续可注册更多）。
 *
 * 复用盘点（用户 2026-11-18 点名"参考哪些能复用，精简代码"）：
 *   ① AudioBars  ← 现有 ui-core.js:134-142 eqTick 的频谱映射（__spectrum 10Hz/16段）
 *   ② CloseX     ← 现有 index.html:1320-1329 .set-close（圆底 + hover rotate(90deg)）
 *                   + :1992-1994 的 SVG 路径
 * 两者都是**照搬形态与逻辑到新 UI**，不改现有 UI 一个字。
 *
 * 插件接口：registerComponent(name, factory) —— 新组件可注册进来，宿主统一挂载。
 */

import { onEvent } from './api.js';

/* ═══════════════ 组件注册表（插件接口） ═══════════════ */
const registry = new Map();
export function registerComponent(name, factory) {
  if (typeof name !== 'string' || typeof factory !== 'function') return false;
  registry.set(name, factory);
  return true;
}
export function hasComponent(name) { return registry.has(name); }
/** 把已注册组件挂到 root 下带 data-component 的节点（含未来动态插入的节点需再调） */
export function mountAll(root = document) {
  let n = 0;
  root.querySelectorAll('[data-component]').forEach(el => {
    if (el.__componentMounted) return;
    const name = el.dataset.component;
    const f = registry.get(name);
    if (!f) return;
    try { f(el, el.dataset); el.__componentMounted = true; n++; } catch (_e) {}
  });
  return n;
}

/* ═══════════════ ① AudioBars：频谱响应条 ═══════════════
 * 现有逻辑（ui-core.js:137-142）逐条复用：
 *   idx = floor((i+0.5) * sp.length / bars.length)
 *   v   = clamp(Number(sp[idx])||0, 0, 1)
 *   h   = 4 + v*16 px
 * 差别：不依赖 window.__spectrum 全局，改由 WS 'spectrum' 事件喂（api.js），
 *       同一份频谱可同时驱动任意多组条（EQ + 歌词卡片），不重复订阅。
 */
let spectrum = null;
let specAt = 0;
const barsGroups = new Set();
const SPEC_TTL_MS = 2000;                       // 与 ui-core.js:175 同款 2s 过期归零

onEvent('spectrum', p => {
  if (Array.isArray(p) && p.length >= 8) { spectrum = p; specAt = performance.now(); }
});

function paintBars() {
  const fresh = spectrum && (performance.now() - specAt) < SPEC_TTL_MS;
  barsGroups.forEach(g => {
    const { el, bars, minH, span } = g;
    if (!bars.length) return;
    if (fresh) {
      for (let i = 0; i < bars.length; i++) {
        const idx = Math.min(spectrum.length - 1, Math.floor((i + 0.5) * spectrum.length / bars.length));
        const v = Math.max(0, Math.min(1, Number(spectrum[idx]) || 0));
        bars[i].style.height = (minH + v * span) + 'px';
      }
    } else {
      bars.forEach(b => { b.style.height = minH + 'px'; });
    }
  });
}
// 单一 100ms 定时器（与 ui-core.js:155 eqTick 同频），驱动所有注册条组
setInterval(paintBars, 100);

export function makeAudioBars(el, ds = {}) {
  const count = Math.max(1, parseInt(ds.bars || '5', 10) || 5);
  const minH = parseFloat(ds.min || '4') || 4;
  const span = parseFloat(ds.span || '16') || 16;
  el.classList.add('ab-bars');
  el.innerHTML = Array.from({ length: count }, () => '<i></i>').join('');
  const group = { el, bars: Array.from(el.querySelectorAll('i')), minH, span };
  barsGroups.add(group);
  el.__audioBarsDispose = () => barsGroups.delete(group);
  return group;
}
registerComponent('audio-bars', makeAudioBars);

/* ═══════════════ ② CloseX：统一关闭叉 ═══════════════
 * 复用 index.html:1320-1329：30px 圆底、rgba(255,255,255,.08)、
 *   hover → .16 + color:#fff + transform:rotate(90deg)，--ease-outback 回弹；
 *   15px SVG（M6 6l12 12M18 6L6 18）。
 * 统一出口：所有关闭按钮都用它，不再各写一份。
 */
const CLOSE_PATH = 'M6 6l12 12M18 6L6 18';
export function makeCloseX(el, ds = {}) {
  const size = Math.max(18, parseInt(ds.size || '30', 10) || 30);
  el.classList.add('close-x');
  el.style.width = el.style.height = size + 'px';
  el.setAttribute('aria-label', ds.label || '关闭');
  el.setAttribute('title', ds.label || '关闭');
  el.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    + 'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    + '<path d="' + CLOSE_PATH + '"/></svg>';
  el.__closeX = true;
  return el;
}
registerComponent('close-x', makeCloseX);

/** 统一绑定关闭动作：点叉 → 收起宿主面板（宿主传 onClose） */
export function bindClose(el, onClose) {
  if (!el) return () => {};
  const fn = e => { e.stopPropagation(); try { onClose(); } catch (_e) {} };
  el.addEventListener('click', fn);
  return () => el.removeEventListener('click', fn);
}

export const _diag = {
  components: () => Array.from(registry.keys()),
  barGroups: () => barsGroups.size,
  hasSpectrum: () => !!spectrum,
  spectrumFresh: () => !!(spectrum && (performance.now() - specAt) < SPEC_TTL_MS),
};
