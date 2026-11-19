/* newui/js/eq.js — EQ 面板：三模式（manual 10 段 / preset 10 预设 / dynamic）+ 总开关。
 * 对接现有 /api/eq（webapi/server.py:570-576），参数展开 b0..b9（ui-eq.js:43-54 同口径）。
 */
import { get, postQuery } from './api.js';

const PRESETS = ['flat', 'rock', 'pop', 'jazz', 'classical', 'electronic', 'vocal', 'bass', 'treble', 'lod'];
const FREQS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000];

let st = { ok: false, enabled: false, source: 'manual', preset: 'flat', bands: Array(10).fill(0), dyn: 60, applied: false };
let listeners = new Set();
function notify() { listeners.forEach(f => { try { f(snapshot()); } catch (_e) {} }); }
export function onChange(fn) { listeners.add(fn); return () => listeners.delete(fn); }
export function snapshot() { return { ...st, bands: st.bands.slice() }; }
export function presets() { return PRESETS.slice(); }
export function freqs() { return FREQS.slice(); }

export async function boot() {
  try {
    const d = await get('/api/eq');
    if (d) {
      st.ok = d.ok !== false;
      st.enabled = !!d.enabled;
      st.source = d.source || 'manual';
      st.preset = d.preset || 'flat';
      if (Array.isArray(d.bands) && d.bands.length === 10) st.bands = d.bands.map(x => parseInt(x, 10) || 0);
      st.dyn = typeof d.dyn === 'number' ? d.dyn : 60;
      st.applied = !!d.applied;
    }
  } catch (_e) {}
  notify();
  return snapshot();
}

let saveTimer = 0;
function saveSoon() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => { saveTimer = 0; save(); }, 220);   // 拖动防抖，避免并发堆积
}
/* 页面隐藏/关闭时立即 flush，避免最后一次拖动落在 220ms 防抖窗内而丢失 */
if (typeof document !== 'undefined') {
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden' && saveTimer) { clearTimeout(saveTimer); saveTimer = 0; save(); }
  });
  window.addEventListener('pagehide', () => { if (saveTimer) { clearTimeout(saveTimer); saveTimer = 0; save(); } });
}
async function save() {
  const p = { enabled: st.enabled ? 1 : 0, source: st.source, preset: st.preset, dyn: st.dyn };
  if (st.source === 'manual') { for (let i = 0; i < 10; i++) p['b' + i] = st.bands[i]; }
  try { const d = await postQuery('/api/eq', p); if (d && typeof d.ok === 'boolean') st.ok = d.ok; } catch (_e) {}
  notify();
}
export async function setEnabled(on) { st.enabled = !!on; notify(); await save(); }
export async function setSource(src) {
  if (!['manual', 'preset', 'dynamic'].includes(src)) return;
  st.source = src; notify(); await save();
}
export async function setPreset(name) {
  if (!PRESETS.includes(name)) return;
  st.preset = name; st.source = 'preset'; notify(); await save();
}
export async function setBand(i, db) {
  if (i < 0 || i > 9) return;
  st.bands[i] = Math.max(-12, Math.min(12, parseInt(db, 10) || 0));
  st.source = 'manual'; notify(); saveSoon();
}
export async function setDyn(v) {
  st.dyn = Math.max(0, Math.min(100, parseInt(v, 10) || 0));
  st.source = 'dynamic'; notify(); saveSoon();
}
export async function reset() {
  st.bands = Array(10).fill(0); st.preset = 'flat'; st.source = 'manual';
  notify(); await save();
}
export const _diag = { st: () => snapshot() };
