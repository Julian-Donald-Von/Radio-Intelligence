/* newui/js/player.js — 播放核心：状态快照 + 三态模式 + 控制。
 * 三态按钮全部点亮（用户点名"随机播放不亮"），高亮取后端 mode_effective。
 */
import { playback, onEvent, connectWS } from './api.js';

const MODES = [
  { key: 'order',    name: '顺序播放', short: '序' },
  { key: 'loop_one', name: '单曲循环', short: '单' },
  { key: 'random',   name: '随机播放', short: '随' },
];

let st = {
  ready: false, playing: false, position: 0, duration: 0,
  title: '', artist: '', cover: '',
  mode: 'order', modeEff: 'order', modeLocked: false,
  volume: 0.8, favorited: false, disliked: false,
};
let listeners = new Set();
function notify() { listeners.forEach(fn => { try { fn(snapshot()); } catch (_e) {} }); }
export function onChange(fn) { listeners.add(fn); return () => listeners.delete(fn); }
export function snapshot() { return { ...st }; }
export function modes() { return MODES.map(m => ({ ...m })); }

/** 用后端快照刷新（mode_effective 为权威） */
export function applyState(d) {
  if (!d) return;
  if (typeof d.ready === 'boolean') st.ready = d.ready;
  if (typeof d.playing === 'boolean') st.playing = d.playing;
  if (typeof d.position === 'number') st.position = d.position;
  if (typeof d.duration === 'number') st.duration = d.duration;
  if (typeof d.title === 'string') st.title = d.title;
  if (typeof d.artist === 'string') st.artist = d.artist;
  /* ⚠ 后端 get_state()（radio_bridge.py:2323-2352）**不返回 volume**，
     所以这里不能靠 d.volume 回填；音量以本地设定为准，静音 = vol 0。 */

  if (typeof d.favorited === 'boolean') st.favorited = d.favorited;
  if (typeof d.disliked === 'boolean') st.disliked = d.disliked;
  if (typeof d.mode === 'string') st.mode = d.mode;
  if (typeof d.mode_effective === 'string') st.modeEff = d.mode_effective;
  else if (typeof d.mode === 'string') st.modeEff = d.mode;
  if (typeof d.mode_locked === 'boolean') st.modeLocked = d.mode_locked;
  const cur = d.current || {};
  if (cur.title) st.title = cur.title;
  if (cur.artist) st.artist = cur.artist;
  if (cur.cover) st.cover = cur.cover;
  notify();
}

/** 启动：先拉一次完整状态，再接 WS 事件 */
export async function boot() {
  try { applyState(await playback.state()); } catch (_e) {}
  connectWS();
  onEvent('play_mode', p => {
    if (!p) return;
    if (p.mode) st.mode = p.mode;
    st.modeEff = p.mode_effective || p.mode || st.modeEff;
    if (typeof p.mode_locked === 'boolean') st.modeLocked = p.mode_locked;
    notify();
  });
  onEvent('playing', p => {
    if (p && typeof p.playing === 'boolean') { st.playing = p.playing; notify(); }
    else refresh();
  });
  onEvent('track_changed', () => refresh());
  onEvent('progress', p => {                        // WS 进度纠偏（现有 UI ui-view.js:84-86 同款）
    if (typeof p === 'number' && p >= 0) { st.position = p; notify(); }
  });
  setInterval(() => { refresh(); }, 2000);          // 轮询兜底（WS 掉线也能自愈）
  return snapshot();
}

let refreshInFlight = false;
export async function refresh() {
  if (refreshInFlight) return;                       // in-flight 去重
  refreshInFlight = true;
  try { applyState(await playback.state()); } catch (_e) {}
  finally { refreshInFlight = false; }
}

/** 控制：乐观更新 + 失败回滚 */
export async function toggle() {
  const want = !st.playing;
  st.playing = want; notify();
  try { await playback.toggle(want); } catch (_e) { st.playing = !want; notify(); }
  refresh();
}
export async function next() { try { await playback.next(); } catch (_e) {} refresh(); }
export async function prev() { try { await playback.prev(); } catch (_e) {} refresh(); }
export async function setMode(mode) {
  if (!MODES.some(m => m.key === mode) || st.modeLocked) return;
  st.modeEff = mode; st.mode = mode; notify();        // 先点亮（用户要即时反馈）
  try { await playback.mode(mode); } catch (_e) {} refresh();
}
export async function setVolume(v) {
  st.volume = Math.max(0, Math.min(1, v)); notify();
  try { await playback.volume(st.volume); } catch (_e) {}
}
export async function seek(ms) {
  try { await playback.seek(Math.round(ms)); } catch (_e) {} refresh();
}
export async function favorite(on) {
  st.favorited = !!on; notify();
  try { await playback.favorite(on); } catch (_e) {}
}

export const _diag = { st: () => ({ ...st }), refreshInFlight: () => refreshInFlight };
