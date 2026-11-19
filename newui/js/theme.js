/* newui/js/theme.js — 主题/外观：皮肤高亮色、背景效果、音频律动、叠层水面。
 * 对接 /api/ui/prefs（webapi/radio_bridge.py 白名单键），与 wallpaper.js 写同一份 prefs，
 * 用 api.js 的 __sid/__seq 保证并发写不互踩。
 */
import { prefs } from './api.js';

const ACCENTS = [
  { key: '', name: '默认', color: '#8b7dff' },
  { key: '#ff5c7a', name: '绯红', color: '#ff5c7a' },
  { key: '#ffd166', name: '暖金', color: '#ffd166' },
  { key: '#7bf1a8', name: '青绿', color: '#7bf1a8' },
  { key: '#6ec7ff', name: '天蓝', color: '#6ec7ff' },
  { key: '#d3a4ff', name: '淡紫', color: '#d3a4ff' },
];
const BG_MODES = [
  { key: 'poster', name: '歌词海报' },
  { key: 'acrylic', name: '亚克力' },
];
const ONTOP = [
  { key: 'none', name: '无效果' },
  { key: 'water', name: '水面' },
];

let st = { theme: 'default', accent: '', bgmode: 'acrylic', beat: true, ontop: 'water' };
let listeners = new Set();
function notify() { listeners.forEach(f => { try { f(snapshot()); } catch (_e) {} }); }
export function onChange(fn) { listeners.add(fn); return () => listeners.delete(fn); }
export function snapshot() { return { ...st }; }
export function accents() { return ACCENTS.slice(); }
export function bgModes() { return BG_MODES.slice(); }
export function ontopModes() { return ONTOP.slice(); }

export async function boot() {
  try {
    const p = (await prefs.get()).prefs || {};
    st.theme = p.theme || 'default';
    st.accent = typeof p.accent === 'string' ? p.accent : '';
    st.bgmode = p.bgmode || 'acrylic';
    st.beat = p.beat !== false;
    // ontop 优先，旧 water 键兼容（radio_bridge.py:3942-3943 迁移逻辑同款）
    st.ontop = p.ontop || (p.water === false ? 'none' : 'water');
  } catch (_e) {}
  apply(); notify();
  return snapshot();
}

function apply() {
  const root = document.documentElement;
  if (!root) return;
  root.style.setProperty('--accent', st.accent || '#8b7dff');
  document.body.classList.toggle('beat-on', st.beat);
  document.body.dataset.bgmode = st.bgmode;
  document.body.dataset.ontop = st.ontop;
}

async function set(patch) { st = { ...st, ...patch }; apply(); notify(); try { await prefs.set(patch); } catch (_e) {} }
export function setAccent(v) { return set({ accent: v || '' }); }
export function setBgMode(v) { return BG_MODES.some(x => x.key === v) ? set({ bgmode: v }) : Promise.resolve(); }
export function setBeat(on) { return set({ beat: !!on }); }
export function setOntop(v) {
  if (!ONTOP.some(x => x.key === v)) return Promise.resolve();
  // 与旧 UI 同款两向同步（ui-skinset.js:337）：ontop + water 一起落盘
  return set({ ontop: v, water: v === 'water' });
}
export const _diag = { st: () => snapshot() };
