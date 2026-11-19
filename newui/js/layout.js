/* newui/js/layout.js — 播放器布局模块（P1：位置 + 大小）。
 *
 * 用户 2026-11-19：「orbit/歌词卡片/口播栏/播放栏/换肤设置等 UI，大小位置内容
 * 能做成模块不？一定要小心拆装」。
 *
 * 设计纪律（小心拆装）：
 *   1. DEFAULT 的每一个值 = 现状硬编码值 ⇒ 模块接入后**默认零视觉变化**
 *   2. 不删旧代码——4 个定位函数保留原逻辑，只把常数换成 layout.get() 的字段
 *   3. 可回滚——window.__layout.reset() 一键回 DEFAULT；关闭模块 = 不 import
 *   4. 与 effects 插件正交：layout 管"在哪/多大"，effects 管"显示什么"
 *
 * 持久化：localStorage r54.layout（与后端 prefs 解耦，先本地验证）
 */
const KEY = 'r54.layout';

/* 默认值 = 现状硬编码（改这里之前先确认与 runtime/index.html 原值一致） */
export const DEFAULT = {
  dock:  { bottom: 24, height: 88, widthMax: 1180 },   // CSS: bottom:24px / height:88px / min(82vw,1180px,...)
  aibar: { offsetX: 48, gap: 14, maxWidth: 'min(430px, 62vw)' },
  side:  { gap: 14, width: 348, halfWidth: 170 },                      // positionSidePanel: 图标上方 14px
  toast: { gapRec: 14, gapSlider: 40, gap: 12 },       // dockTop-14-40-12
  cards: { arcStep: 0.135 },                           // ARC_STEP（相邻卡圆心角）
};

export const PRESETS = {
  classic:   {},                                                        // 现状
  immersive: { dock:{bottom:12,height:80,widthMax:1320}, aibar:{offsetX:24,gap:10},
               side:{gap:10,width:348,halfWidth:174}, toast:{gapRec:10,gapSlider:32,gap:8}, cards:{arcStep:0.12} },
  minimal:   { dock:{bottom:18,height:72,widthMax:960}, aibar:{offsetX:20,gap:8},
               side:{gap:8,width:348,halfWidth:174}, toast:{gapRec:8,gapSlider:28,gap:6}, cards:{arcStep:0.15} },
  wide:      { dock:{bottom:24,height:88,widthMax:1600}, aibar:{offsetX:64,gap:16},
               side:{gap:16,width:348,halfWidth:174}, toast:{gapRec:14,gapSlider:40,gap:12}, cards:{arcStep:0.11} },
};

let current = null;                 // 深拷贝 DEFAULT + 存档覆盖
const listeners = new Set();

function deepMerge(base, patch){
  const out = {};
  for (const k of Object.keys(base)){
    if (base[k] && typeof base[k] === 'object' && !Array.isArray(base[k])){
      out[k] = deepMerge(base[k], (patch && patch[k]) || {});
    } else {
      out[k] = (patch && Object.prototype.hasOwnProperty.call(patch, k)) ? patch[k] : base[k];
    }
  }
  return out;
}
function clone(o){ return JSON.parse(JSON.stringify(o)); }

export function get(){ return current || (current = load()); }
function load(){
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch (_e) {}
  return deepMerge(DEFAULT, saved);
}
function save(){ try { localStorage.setItem(KEY, JSON.stringify(current)); } catch (_e) {} }
function emit(){ listeners.forEach(f => { try { f(get()); } catch (_e) {} }); }

export function set(patch, { persist = true } = {}){
  current = deepMerge(get(), patch || {});
  if (persist) save();
  emit();
  return clone(current);
}
export function applyPreset(name){
  if (!Object.prototype.hasOwnProperty.call(PRESETS, name)) return false;
  current = deepMerge(DEFAULT, PRESETS[name]);
  save(); emit();
  return true;
}
export function reset(){ current = clone(DEFAULT); save(); emit(); return clone(current); }
export function onChange(fn){ listeners.add(fn); return () => listeners.delete(fn); }
export function snapshot(){ return clone(get()); }

/* 调试/验证句柄 */
export function install(){
  window.__layout = {
    get: snapshot,
    set: (p) => set(p),
    preset: (n) => applyPreset(n),
    reset: () => reset(),
    defaults: () => clone(DEFAULT),
    presets: () => Object.keys(PRESETS),
  };
  return window.__layout;
}

export default { DEFAULT, PRESETS, get, set, applyPreset, reset, onChange, snapshot, install };
