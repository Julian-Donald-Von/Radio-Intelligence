/* newui/js/effects/sonic.js — 叠层效果插件：音域回响（Sonic Topography 情绪版）。
 *
 * 视觉核心移植自 yin-yizhen/sonic-topography（作者 AJIN），个人非商业学习使用。
 * ★ 与原作最大区别：原作用 Web Audio 频谱驱动，本插件**只用情绪**（window.__mood.axes
 *   14 维），AudioEngine 完全不引入——用户原话「不要音频响应，要情绪响应」。
 *
 * 插件契约见 effects.js：{ id, type:'ontop', name, onEnable, onDisable }
 * 启停/持久化/换肤页 tab 由注册表接管。与 water/fuelrod 互斥（同属 ontop 槽）。
 * ★ three.module.js（648KB）用动态 import 懒加载——不阻塞 effects 装配与其它插件。
 *
 * 诊断：window.__sonic.enabled() / state() / debugMood(axes|null)
 */
import { registerEffect } from '../effects.js';

let scene = null;
let Loading = null;          // 动态 import promise
let forcedMood = null;
let moodBackup = null;

function ensureScene(){
  if (scene) return Promise.resolve(scene);
  if (!Loading){
    Loading = Promise.all([
      import('./sonic/sonic-renderer.js'),
    ]).then(([mod]) => {
      let canvas = document.getElementById('sonic-canvas');
      if (!canvas){
        canvas = document.createElement('canvas');
        canvas.id = 'sonic-canvas'; canvas.className = 'sonic-canvas';
        (document.getElementById('bg') || document.body).appendChild(canvas);
      }
      scene = new mod.default(canvas);
      scene._diag = mod._diag;
      return scene;
    }).catch(e => {
      Loading = null;
      console.warn('[sonic] load failed', e);
      // WebGL 不可用（老显卡/禁用硬件加速）→ 可见提示，不让用户以为"点了没反应"
      try { if (typeof toast === 'function') toast('音域回响需要 WebGL，当前环境不可用'); } catch (_e) {}
      throw e;
    });
  }
  return Loading;
}

/* T23：参数区控件 → scene.tune()（主题色/密度/跳动倍率/流星/涟漪/浮空块）。
   偏好存 localStorage r54.sonic.params；只绑一次（_bound 标记）。 */
const PARAM_KEY = 'r54.sonic.params';
let controlsBound = false;
function loadSonicParams(){
  try { return JSON.parse(localStorage.getItem(PARAM_KEY) || '{}') || {}; } catch (_e) { return {}; }
}
function saveSonicParams(p){ try { localStorage.setItem(PARAM_KEY, JSON.stringify(p)); } catch (_e) {} }
function bindControls(s){
  const saved = loadSonicParams();
  if (Object.keys(saved).length) s.tune(saved);
  const q = id => document.getElementById(id);
  const theme=q('so-theme'), density=q('so-density'), densityV=q('so-density-v');
  const tempo=q('so-tempo'), tempoV=q('so-tempo-v');
  const meteors=q('so-meteors'), ripples=q('so-ripples'), floating=q('so-floating');
  // 回填存档值
  if (theme && saved.theme) theme.value = saved.theme;
  if (density && saved.density != null){ density.value = saved.density; if(densityV) densityV.textContent = saved.density; }
  if (tempo && saved.tempoScale != null){ tempo.value = Math.round(saved.tempoScale*100); if(tempoV) tempoV.textContent = saved.tempoScale.toFixed(2)+'×'; }
  if (meteors && saved.meteors != null) meteors.checked = !!saved.meteors;
  if (ripples && saved.ripples != null) ripples.checked = !!saved.ripples;
  if (floating && saved.floating != null) floating.checked = !!saved.floating;
  const apply = () => {
    const p = {
      theme: theme ? theme.value : undefined,
      density: density ? Number(density.value) : undefined,
      tempoScale: tempo ? Number(tempo.value)/100 : undefined,
      meteors: meteors ? meteors.checked : undefined,
      ripples: ripples ? ripples.checked : undefined,
      floating: floating ? floating.checked : undefined,
    };
    Object.keys(p).forEach(k => p[k] === undefined && delete p[k]);
    s.tune(p); saveSonicParams({ ...loadSonicParams(), ...p });
    if (densityV && density) densityV.textContent = density.value;
    if (tempoV && tempo) tempoV.textContent = (Number(tempo.value)/100).toFixed(2)+'×';
  };
  if (controlsBound) return;
  controlsBound = true;
  [theme,density,tempo,meteors,ripples,floating].forEach(el => {
    if (el) el.addEventListener(el.tagName === 'SELECT' || el.type === 'checkbox' ? 'change' : 'input', apply);
  });
}

export const sonicEffect = {
  id: 'sonic', type: 'ontop', name: '音域回响',
  onEnable(){
    ensureScene().then(s => {
      s.start();
      bindControls(s);   // T23：参数区控件 → scene.tune()
      window.__sonic = {
        enabled: () => !!s.running,
        state: () => ({
          running: s.running, gridSize: s.gridSize, smoothed: { ...s.sm },
          moodSource: forcedMood ? 'debug' : ((window.__mood && window.__mood.axes) ? 'live' : 'idle'),
          energy: s._diag ? s._diag.moodToEnergy((forcedMood || (window.__mood && window.__mood.axes) || {})) : null,
        }),
        debugMood: (axes) => {
          if (axes == null){
            if (moodBackup && window.__mood) window.__mood.axes = moodBackup;
            moodBackup = null; forcedMood = null;
          } else {
            if (!moodBackup && window.__mood) moodBackup = window.__mood.axes;
            forcedMood = { ...axes };
            if (!window.__mood) window.__mood = {};
            window.__mood.axes = forcedMood;
          }
          return forcedMood;
        },
        refreshTheme: () => s.refreshTheme(),
        tune: (p) => s.tune(p),
        params: () => ({ ...s.opts, tempoScale: s.tempoScale || 1 }),
        stop: () => s.stop(),
      };
    }).catch(() => {
      /* ★ 装配失败**不回退 active/tab 高亮**——用户选了 sonic 就该保持选中，
         渲染失败是另一回事（toast 已在 ensureScene 里给过）。之前回退 none
         会 emit('active') → syncTabs 把高亮打回 none =「选中没高亮/关掉再开就回去」。 */
    });
    return true;   // 异步装配，立即返回成功（避免阻塞注册表）
  },
  onDisable(){
    if (scene){ scene.stop(); }
    try { delete window.__sonic; } catch (_e) { window.__sonic = undefined; }
    if (moodBackup && window.__mood) window.__mood.axes = moodBackup;
    moodBackup = null; forcedMood = null;
  },
};

export function install(){ return registerEffect(sonicEffect); }
export const _diag = { ensureScene };
