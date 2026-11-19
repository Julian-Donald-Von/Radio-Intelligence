/* newui/js/effects/water.js — 叠层效果插件：水面（含换肤页参数区）。
 * 冻结核心 ui-water.js 不改源码；本插件只通过 window.__water API 接线。
 * 整体可插拔：registerEffect 一次，UI/启停/参数区/持久化全由 effects.js 接管。
 */
import { registerEffect } from '../effects.js';

const P = window.__water || {};
const call = (fn, ...a) => { try { if (typeof P[fn] === 'function') P[fn](...a); } catch (_e) {} };

/* 水面参数默认值（与 ui-skinset.js 对齐；不写死第三方值） */
const KEY = 'r54.effects.water.params';
function loadParams(){
  try { return JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch (_e) { return {}; }
}
function saveParams(o){ try { localStorage.setItem(KEY, JSON.stringify(o)); } catch (_e) {} }

const SPEC = [
  { key: 'semStrength', label: '情绪强度', min: 0, max: 1, step: 0.05, value: 1.0 },
  { key: 'specExp',     label: '波纹细度', min: -3, max: -1, step: 0.1,  value: -2.0 },
];
let params = { ...Object.fromEntries(SPEC.map(s => [s.key, s.value])), ...loadParams() };

/* 环境微激励：水体 P1 是透明材质，且波形只靠音乐 onset 注入——静音时完全透明，
   看起来"没有水"。这里按低频定时注入极低幅涟漪，让静音也有轻微可见流动。
   不改冻结核心源码，只走公开 __water.drop()。
   ★★ 2026-11-18 修正（用户："新 UI 波动太夸张/冻结太狠"）：
     · 原来**播放音乐时也在跑**（注释说"播放时可关掉"但没实现）⇒ 在音乐 onset 之上
       每 1.8s 额外砸一次 `drop(...,0.55)`（=默认冲击幅度）⇒ 场能量 +51%；
     · 现在：**只有不在播放时才滴**，幅度 0.55→0.20，间隔 1.8s→2.5s。 */
const AMBIENT_MS = 2500;
const AMBIENT_AMP = 0.20;
let ambientTimer = 0;
let ambientPlaying = null;          // null=未知；用于避免每 tick 都请求
function startAmbient(){
  stopAmbient();
  ambientTimer = setInterval(() => {
    try {
      const w = window.__water;
      if (!w || typeof w.drop !== 'function') return;
      if (typeof w.enabled === 'function' && !w.enabled()) return;
      /* 播放中不滴：以 /api/playback/state 的 playing 为准（2.5s 一次，成本可忽略） */
      fetch('/api/playback/state', { cache: 'no-store' })
        .then(r => r.json())
        .then(s => {
          ambientPlaying = !!(s && s.playing);
          if (ambientPlaying) return;             // 有音乐 ⇒ 不叠加
          try { w.drop(0.15 + Math.random() * 0.7, 0.15 + Math.random() * 0.7, AMBIENT_AMP); } catch (_e) {}
        })
        .catch(() => {});
    } catch (_e) {}
  }, AMBIENT_MS);
}
function stopAmbient(){ if (ambientTimer){ clearInterval(ambientTimer); ambientTimer = 0; } }

/* "无效果"基线插件——叠层效果关掉水面的入口（老 UI 的 none 选项，插件化） */
export const noneEffect = {
  id: 'none', type: 'ontop', name: '无效果',
  onEnable(){ call('setEnabled', false, false); },
  onDisable(){},
};

export const waterEffect = {
  id: 'water', type: 'ontop', name: '水面',
  params: SPEC.map(s => ({ ...s, value: params[s.key] })),
  onEnable(){
    call('setEnabled', true, false);
    // 按存档下发所有参数（切档保场）
    for (const s of SPEC) call(s.key === 'specExp' ? 'setSpecExp' : 'setSemStrength', params[s.key]);
    startAmbient();          // 环境微激励：静音也让水面轻微可见
  },
  onDisable(){ stopAmbient(); call('setEnabled', false, false); },
  applyParams(values){
    params = { ...params, ...values }; saveParams(params);
    if ('semStrength' in values) call('setSemStrength', params.semStrength);
    if ('specExp' in values) call('setSpecExp', params.specExp);
  },
};

export function install(){
  registerEffect(noneEffect);      // 基线：关闭叠层
  return registerEffect(waterEffect);
}
export const _diag = { params: () => ({ ...params }), id: waterEffect.id, type: waterEffect.type };
