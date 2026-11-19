/* newui/js/data.js — 数据层：栏目/推荐/队列/曲库/历史/歌单/搜索/不喜欢/口播。
 * 全部走现有 /api/*（webapi/server.py 已注册的路由），不改后端。
 */
import { get, postQuery, onEvent } from './api.js';

/* ---------- 通用列表 state（每个栏目一份，带 in-flight 去重） ---------- */
const lists = Object.create(null);
const listeners = new Set();
const inflight = new Set();

export function getList(view) { return (lists[view] || []).slice(); }
export function onChange(fn) { listeners.add(fn); return () => listeners.delete(fn); }
function notify() { listeners.forEach(f => { try { f(); } catch (_e) {} }); }

const VIEW_API = {
  wander: '/api/reco',        // 漫游/推荐货架
  library: '/api/library',
  history: '/api/history',
  queue: '/api/queue',
};
export const VIEWS = ['wander', 'library', 'history', 'queue', 'playlist'];

/** 归一化各种返回结构（不同端点字段名不一：entries/tracks/items/数组） */
function normalize(d) {
  if (Array.isArray(d)) return d;
  if (!d) return [];
  for (const k of ['entries', 'tracks', 'items', 'songs']) {
    if (Array.isArray(d[k])) return d[k];
  }
  return [];
}

/** 拉某栏目列表（playlist 需额外 group/source 参数） */
export async function load(view, opts = {}) {
  const key = view + ':' + JSON.stringify(opts || {});
  if (inflight.has(key)) return getList(view);
  inflight.add(key);
  try {
    let url = VIEW_API[view];
    if (view === 'playlist') {
      const group = opts.group || 'discover';
      url = '/api/playlists?group=' + encodeURIComponent(group)
          + '&offset=' + encodeURIComponent(opts.offset || 0)
          + '&limit=' + encodeURIComponent(opts.limit || 60);
      if (opts.source) url += '&source=' + encodeURIComponent(opts.source);
    }
    if (!url) return getList(view);
    const d = await get(url);
    lists[view] = normalize(d);
    if (view === 'playlist' && d && typeof d.active_key === 'string') lists['playlist:active'] = d.active_key;
    notify();
    return getList(view);
  } catch (_e) { return getList(view); }
  finally { inflight.delete(key); }
}

/** 拉歌单详情曲目（platform + ext_id） */
export async function loadPlaylistTracks(platform, extId) {
  const d = await get('/api/playlists/tracks?platform=' + encodeURIComponent(platform)
                    + '&ext_id=' + encodeURIComponent(extId));
  return normalize(d);
}

/* ---------- 播放来源操作（身份起播，避免"点A播B"） ---------- */
export async function playEntry(view, entry) {
  const id = entry && (entry.id != null ? entry.id : entry.track_id);
  if (view === 'wander') {
    return postQuery('/api/reco/play', { track_id: id });
  }
  if (view === 'queue' || view === 'library' || view === 'history') {
    return postQuery('/api/play/track/' + encodeURIComponent(id));
  }
  if (view === 'playlist') {
    const platform = entry.platform || entry.source || 'netease';
    const extId = entry.ext_id || entry.id;
    const index = parseInt(entry.index || entry.idx || 0, 10);
    return postQuery('/api/playlists/play', { platform, ext_id: extId, index });
  }
  return null;
}
export async function playNext(entry) {
  return postQuery('/api/queue/play-next', { track_id: entry.id });
}
export async function removeFrom(view, entry) {
  const id = entry && (entry.id != null ? entry.id : entry.track_id);
  if (view === 'queue') return postQuery('/api/queue/remove', { track_id: id });
  if (view === 'history') return fetch('/api/history/' + encodeURIComponent(id), { method: 'DELETE' }).then(r => r.json());
  if (view === 'library') return postQuery('/api/library/delete', { id: String(id) });
  if (view === 'wander') return postQuery('/api/reco/remove', { track_id: id });
  return null;
}
export async function playAll(view, opts = {}) {
  if (view === 'wander') return postQuery('/api/reco/play-all');
  if (view === 'library') return postQuery('/api/library/play-all');
  return null;
}

/* ---------- 收藏 / 不喜欢 ---------- */
export async function favoriteTrack(entry, on) {
  return postQuery('/api/favorite/track', {
    track_id: entry.id, source: entry.source || '', ext_id: entry.ext_id || '',
    title: entry.title || '', artist: entry.artist || '', album: entry.album || '',
    cover: entry.cover || '',
  });
}
export async function listDislikes() { return get('/api/dislikes'); }
export async function addDislike(entry) {
  return postQuery('/api/dislike/add', {
    key: entry.key || ('i:' + entry.id), title: entry.title || '', artist: entry.artist || '',
  });
}
export async function removeDislike(key) { return postQuery('/api/dislike/remove', { key }); }

/* ---------- 搜索 ---------- */
let searchSeq = 0;
export async function search(q, source) {
  const my = ++searchSeq;
  const url = '/api/search?q=' + encodeURIComponent(q)
            + (source ? '&source=' + encodeURIComponent(source) : '');
  const d = await get(url);
  if (my !== searchSeq) return [];
  return normalize(d);
}
export async function searchSources() {
  const d = await get('/api/search/sources');
  return { sources: (d && d.sources) || [], current: (d && d.current) || '' };
}
export async function playSearch(entry) {
  return postQuery('/api/search/play', {
    source: entry.source || '', ext_id: entry.ext_id || '', title: entry.title || '',
    artist: entry.artist || '', album: entry.album || '', cover: entry.cover || '',
    duration: entry.duration || 0,
  });
}

/* ---------- 口播文案 ---------- */
let broadcast = '';
export function getBroadcast() { return broadcast; }
export async function loadBroadcast() {
  const d = await get('/api/dj/broadcast');
  if (d && typeof d.text === 'string' && d.text) broadcast = d.text;
  return broadcast;
}
export function setBroadcastFromWS(text) { broadcast = String(text || ''); notify(); }

/* ---------- WS 事件 → 数据刷新 ---------- */
onEvent('playlist_updated', () => { load('wander'); load('queue'); });
onEvent('reco_updated', () => load('wander'));
onEvent('favorite', () => { load('library'); load('wander'); });
onEvent('track_changed', () => { load('history'); load('queue'); });
onEvent('toast', msg => { try { window.__newuiToast && window.__newuiToast(String(msg)); } catch (_e) {} });
onEvent('radio_error', p => { try { window.__newuiToast && window.__newuiToast('电台错误：' + ((p && p.message) || '')); } catch (_e) {} });

export const _diag = { views: () => VIEWS.slice(), counts: () => Object.fromEntries(Object.entries(lists).map(([k, v]) => [k, v.length])) };
