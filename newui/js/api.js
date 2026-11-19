/* newui/js/api.js — API 层：统一 fetch + in-flight 去重 + 单调序号 + WS 重连。
 * 设计目标（用户 2026-11-18 点名）：
 *   · API 并发：同 key 请求在飞时跳过重复；写操作带 __sid/__seq 保证"最后一次动作胜"
 *   · 前后端连接：WS 断线指数退避重连，连上后补一次状态
 */

const BASE = '';                                   // 同源，复用现有 /api/*
const SID = 'nu_' + Math.random().toString(36).slice(2);   // 页面会话 id
let seq = 0;                                       // 单调写序号

const inflight = new Map();                        // key -> Promise

/** GET：同 key 在飞时复用同一个 Promise（不叠加请求） */
export function get(path, { dedupe = true } = {}) {
  const key = 'G:' + path;
  if (dedupe && inflight.has(key)) return inflight.get(key);
  const p = fetch(BASE + path, { headers: { Accept: 'application/json' } })
    .then(r => r.json())
    .finally(() => inflight.delete(key));
  if (dedupe) inflight.set(key, p);
  return p;
}

/** POST 查询参数型（现有 /api/playback/* 的标量参数都是 query，body 会 422） */
export function postQuery(path, params = {}, { dedupe = true } = {}) {
  const key = 'P:' + path + ':' + JSON.stringify(params);
  if (dedupe && inflight.has(key)) return inflight.get(key);
  const qs = Object.entries(params).map(([k, v]) =>
    encodeURIComponent(k) + '=' + encodeURIComponent(String(v))).join('&');
  const p = fetch(BASE + path + (qs ? '?' + qs : ''), { method: 'POST' })
    .then(r => r.json()).finally(() => inflight.delete(key));
  if (dedupe) inflight.set(key, p);
  return p;
}

/** POST JSON 型（仅 /api/ui/prefs 这类收 dict 的端点；带单调序号防过期写） */
export function postJSON(path, body = {}, { dedupe = true } = {}) {
  const key = 'J:' + path;
  if (dedupe && inflight.has(key)) return inflight.get(key);
  const payload = { ...body, __sid: SID, __seq: ++seq };
  const p = fetch(BASE + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }).then(r => r.json()).finally(() => inflight.delete(key));
  if (dedupe) inflight.set(key, p);
  return p;
}

/** 播放控制：一律走现有 /api/playback/*（后端零重复造轮子，参数是 query） */
export const playback = {
  state: () => get('/api/playback/state'),
  toggle: (on) => postQuery('/api/playback/toggle', { force_on: on }),
  next:   () => postQuery('/api/playback/next'),
  prev:   () => postQuery('/api/playback/prev'),
  mode:   (mode) => postQuery('/api/playback/mode', { mode }),
  volume: (vol) => postQuery('/api/playback/volume', { vol }),
  seek:   (ms)  => postQuery('/api/playback/seek', { ms }),
  favorite: (on)=> postQuery('/api/playback/favorite', { on: on ? 1 : 0 }),
};

/** 偏好读写：壁纸持久化的唯一通道（走后端白名单，跨刷新/重启） */
export const prefs = {
  get: () => get('/api/ui/prefs'),
  set: (patch) => postJSON('/api/ui/prefs', patch),
};

/** 壁纸：列已导入项 / 激活指定 key（后端 query 参数，持久化 active_key） */
export const wallpapers = {
  list: () => get('/api/wallpapers'),
  activate: (key) => postQuery('/api/wallpapers/activate', { key }),
};

/* ---------- WS：事件源 + 指数退避重连 ---------- */
let ws = null;
let wsAttempt = 0;
let wsTimer = 0;
const handlers = new Map();

export function onEvent(type, fn) {
  if (!handlers.has(type)) handlers.set(type, new Set());
  handlers.get(type).add(fn);
  return () => handlers.get(type)?.delete(fn);
}

function dispatch(type, payload) {
  handlers.get(type)?.forEach(fn => { try { fn(payload); } catch (_e) {} });
}

export function connectWS() {
  if (ws && (ws.readyState === 0 || ws.readyState === 1)) return;
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  try {
    ws = new WebSocket(proto + '//' + location.host + '/ws');
  } catch (_e) {
    scheduleReconnect(); return;
  }
  ws.onopen = () => { wsAttempt = 0; dispatch('connected', {}); };
  ws.onmessage = (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch (_e) { return; }
    dispatch(m.event, m.payload);
  };
  ws.onerror = () => { /* onclose 会接管重连 */ };
  ws.onclose = () => { dispatch('disconnected', {}); scheduleReconnect(); };
}

function scheduleReconnect() {
  clearTimeout(wsTimer);
  const delay = Math.min(15000, 1000 * Math.pow(2, wsAttempt++));
  wsTimer = setTimeout(connectWS, delay);
}

/* 暴露测试钩（只读） */
export const _diag = {
  sid: SID,
  getSeq: () => seq,
  inflightKeys: () => Array.from(inflight.keys()),
  wsState: () => ws ? ws.readyState : -1,
  wsAttempt: () => wsAttempt,
};
