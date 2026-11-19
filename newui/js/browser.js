/* newui/js/browser.js — 浏览层：栏目导航 + 卡片列表 + 搜索 + 播放来源操作。
 * 基于现有 UI 版图（ui-cards.js / ui-orbit.js / ui-column.js / ui-init.js）重新设计，
 * 用扁平列表替代弧形卡片墙（新 UI 不追求像素复刻，追求清晰与可用）。
 */
import * as data from './data.js';
import { get, postQuery } from './api.js';

const VIEWS = [
  { key: 'wander', name: '漫游', desc: 'AI 推荐货架' },
  { key: 'library', name: '曲库', desc: '本地音乐' },
  { key: 'history', name: '历史', desc: '最近播放' },
  { key: 'queue', name: '队列', desc: '当前待播' },
  { key: 'playlist', name: '歌单', desc: '在线歌单' },
];

let active = 'wander';
let searchResults = [];
let searchSources = [];
let currentSource = '';
let listeners = new Set();
function notify() { listeners.forEach(f => { try { f(snapshot()); } catch (_e) {} }); }
export function onChange(fn) { listeners.add(fn); return () => listeners.delete(fn); }
export function snapshot() {
  return { active, items: data.getList(active), searchResults: searchResults.slice(),
           sources: searchSources.slice(), currentSource };
}
export function views() { return VIEWS.slice(); }

export async function boot() {
  await data.load(active);
  try { const s = await data.searchSources(); searchSources = s.sources; currentSource = s.current; } catch (_e) {}
  data.onChange(notify);
  notify();
}

export async function setView(v) {
  if (!VIEWS.some(x => x.key === v)) return;
  active = v;
  await data.load(v);
  notify();
}
export async function refresh() { await data.load(active); notify(); }

/** 身份起播（点哪首播哪首，不靠全局 state）；成功后再让 player 刷新 */
export async function play(entry) {
  const d = await data.playEntry(active, entry);
  notify();
  return d;
}
export async function playNext(entry) { const d = await data.playNext(entry); notify(); return d; }
export async function remove(entry) { const d = await data.removeFrom(active, entry); await refresh(); return d; }
export async function playAll() { const d = await data.playAll(active); await refresh(); return d; }
export async function fav(entry, on) {
  const d = await data.favoriteTrack(entry, on);
  await refresh();
  return d;
}

/* ---------- 搜索 ---------- */
export async function doSearch(q) {
  if (!q || !q.trim()) { searchResults = []; notify(); return []; }
  searchResults = await data.search(q.trim(), currentSource);
  notify();
  return searchResults;
}
export async function setSource(src) {
  currentSource = src;
  // 持久化（走 prefs 白名单键 searchSource）
  try {
    const { prefs } = await import('./api.js');
    await prefs.set({ searchSource: src });
  } catch (_e) {}
  notify();
}
export async function playSearch(entry) {
  const d = await data.playSearch(entry);
  notify();
  return d;
}
export const _diag = { active: () => active, searchCount: () => searchResults.length };
