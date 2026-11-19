/* newui/js/wallpaper.js — 背景内容（壁纸）持久化。
 *
 * ★ 用户硬需求（2026-11-18 原话）：「自定义壁纸上一次点啥了，下一次就还得是啥，
 *   不能自动跳到默认的东西」。旧 UI 的根因（ui-skinset.js:25-26 自认）：
 *   只临时挂 class、没进 prefs 白名单 ⇒ 刷新即失、重启即丢。
 *
 * 新 UI 的做法（三层，缺一不可）：
 *   ① 选择即写后端 prefs（wallpaper + customWallpaperKey），带 __sid/__seq；
 *   ② 启动时**先 await 后端回填**再应用，绝不先渲染默认再回填（那会闪一下默认）；
 *   ③ 自定义壁纸同时激活后端 active_key（重启后资源仍在）；
 *   ④ 只有"用户没选过"才用默认（DEFAULT），任何已保存值都不覆盖。
 */

import { prefs, wallpapers } from './api.js';

export const DEFAULT_WALLPAPER = 'cover';          // 跟随当前曲封面（真正的"未选择"态）
const VALID = new Set(['cover', 'brand', 'custom']);

let current = DEFAULT_WALLPAPER;
let currentKey = '';
let items = [];
let listeners = new Set();
let booted = false;

function notify() { listeners.forEach(fn => { try { fn(snapshot()); } catch (_e) {} }); }
export function onChange(fn) { listeners.add(fn); return () => listeners.delete(fn); }
export function snapshot() { return { mode: current, key: currentKey }; }

/** 启动：先读后端 prefs，再应用。返回实际应用的模式。 */
export async function boot() {
  if (booted) return snapshot();
  booted = true;
  let p = {};
  try {
    const r = await prefs.get();
    p = (r && r.prefs) || {};
  } catch (_e) { /* 网络失败也不能跳默认：保留当前值 */ }

  // 已保存的值一律尊重；只有陌生值才回退到"未选择"（cover），绝不回退到 brand logo
  const savedMode = VALID.has(p.wallpaper) ? p.wallpaper : DEFAULT_WALLPAPER;
  current = savedMode;
  currentKey = typeof p.customWallpaperKey === 'string' ? p.customWallpaperKey : '';

  try {
    const r = await wallpapers.list();
    items = (r && r.items) || [];
  } catch (_e) { items = []; }

  // custom 但 key 已不存在（被删）：此时才降级到 cover，并落盘纠正
  if (current === 'custom' && !items.some(x => x.key === currentKey)) {
    current = DEFAULT_WALLPAPER; currentKey = '';
    prefs.set({ wallpaper: current, customWallpaperKey: '' }).catch(() => {});
  }
  apply();
  return snapshot();
}

/** 选择背景内容：立即应用 + 立刻写后端（不等待 UI 渲染） */
export async function select(mode, key = '') {
  if (!VALID.has(mode)) return snapshot();
  if (mode === 'custom') {
    if (!key) return snapshot();                    // 没有 key 不接受 custom
    currentKey = key;
    // 同步激活后端 active_key（重启后壁纸资源仍在）
    try { await wallpapers.activate(key); } catch (_e) {}
  } else {
    currentKey = '';
  }
  current = mode;
  apply();
  // ★ 关键：写的是后端白名单键，跨刷新/重启；带 __sid/__seq 防过期写覆盖用户终值
  try { await prefs.set({ wallpaper: mode, customWallpaperKey: currentKey }); } catch (_e) {}
  return snapshot();
}

/** 把当前模式映射到背景元素的 src / class（由 ui.js 注册 renderer） */
let renderer = null;
export function setRenderer(fn) { renderer = fn; }
function apply() { if (renderer) { try { renderer(snapshot(), resolveSrc()); } catch (_e) {} } notify(); }

export function resolveSrc() {
  if (current === 'brand') return '/brand/logo.png';
  if (current === 'custom') {
    const it = items.find(x => x.key === currentKey);
    if (it) {
      // 后端 list_wallpapers 给 url / preview / background 三种形态，按可用性取
      const src = it.url || it.preview || it.background;
      if (src) return src.startsWith('/') ? src : '/skin-assets/' + String(src).split('/').pop();
    }
  }
  return '';                                        // cover：由 ui.js 用当前曲封面填充
}

export function listItems() { return items.slice(); }
export const _diag = { isBooted: () => booted, valid: () => Array.from(VALID) };
