/* newui/js/settings.js — 设置面板：AI/TTS/音源/通用（读白名单 + 点路径写）。
 * 对接 /api/settings、/api/settings/set、/api/settings/test、/api/settings/qr/start。
 * 敏感字段后端只回"是否设置"标记，本模块只展示标记，不要求明文。
 */
import { get, postQuery } from './api.js';

// 展示用的字段定义（path, 标签, 类型），只列常用项；完整 schema 由后端白名单决定
const FIELDS = [
  { group: 'AI 大模型', items: [
    { path: 'ai.backend', label: '后端', type: 'select', options: [['llamacpp','本机 llama.cpp'],['api','远程 API'],['ollama','Ollama（已退役）']] },
    { path: 'ai.model', label: '模型', type: 'text' },
    { path: 'ai.llamacpp_host', label: 'llama.cpp 地址', type: 'text' },
    { path: 'ai.thinking', label: '思考模式', type: 'bool' },
    { path: 'ai.web_search', label: '联网检索', type: 'bool' },
  ]},
  { group: '语音 TTS', items: [
    { path: 'tts.engine', label: '引擎', type: 'text' },
    { path: 'tts.qwen3tts_voice', label: '音色', type: 'text' },
  ]},
  { group: '音源', items: [
    { path: 'search.online_provider', label: '在线音源', type: 'text' },
    { path: 'search.engine', label: '检索引擎', type: 'text' },
  ]},
  { group: '通用', items: [
    { path: 'geo.city', label: '城市', type: 'text' },
    { path: 'roam_strategy', label: '漫游策略', type: 'text' },
    { path: 'auto_play', label: '自动播放', type: 'bool' },
  ]},
];
const TABS = ['AI 大模型', '语音 TTS', '音源', '通用'];
export function tabs() { return TABS.slice(); }
export function fields() { return FIELDS.map(g => ({ group: g.group, items: g.items.slice() })); }

let values = Object.create(null);
let activeTab = TABS[0];
let listeners = new Set();
function notify() { listeners.forEach(f => { try { f(); } catch (_e) {} }); }
export function onChange(fn) { listeners.add(fn); return () => listeners.delete(fn); }
export function snapshot() { return { values: { ...values }, activeTab }; }
export function setTab(t) { if (TABS.includes(t)) { activeTab = t; notify(); } }

export async function boot() {
  try {
    const d = await get('/api/settings');
    values = (d && d.values) || {};
  } catch (_e) { values = {}; }
  notify();
  return snapshot();
}

export function valueOf(path) {
  // 支持点路径 ai.model → values.ai.model
  let cur = values;
  for (const seg of String(path).split('.')) {
    if (cur && typeof cur === 'object' && seg in cur) cur = cur[seg];
    else return '';
  }
  return cur == null ? '' : cur;
}

export async function setValue(path, value) {
  const boolLike = typeof value === 'boolean';
  const v = boolLike ? (value ? '1' : '0') : String(value);
  try {
    // 敏感值走 JSON body，不进 URL/access log（后端 settings_set 已支持 body）
    const d = await fetch('/api/settings/set', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path, value: v })
    }).then(r => r.json());
    if (d && d.ok !== false) {
      // 本地镜像更新（点路径）
      const segs = String(path).split('.');
      let cur = values;
      for (let i = 0; i < segs.length - 1; i++) {
        if (!cur[segs[i]] || typeof cur[segs[i]] !== 'object') cur[segs[i]] = {};
        cur = cur[segs[i]];
      }
      cur[segs[segs.length - 1]] = v;
      notify();
    }
    return d;
  } catch (_e) { return { ok: false }; }
}

export async function test(kind) { return postQuery('/api/settings/test', { kind }); }
export async function qrStart(source) { return postQuery('/api/settings/qr/start', { source }); }
export async function qrPoll(source, key) {
  return get('/api/settings/qr/poll?source=' + encodeURIComponent(source) + '&key=' + encodeURIComponent(key));
}

export const _diag = { keys: () => Object.keys(values), tab: () => activeTab };
