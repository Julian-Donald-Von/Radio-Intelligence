/* newui/js/effects/acrylic.js — 背景效果插件：亚克力。
 * 控制 body.bgmode-acrylic（对背景皮肤做明亮 blur 磨砂玻璃），poster 为无效果基线。
 * 整体可插拔：registerEffect 一次即可，与叠层效果正交可同时生效。
 */
import { registerEffect } from '../effects.js';

function apply(mode){
  const b = document.body;
  if (!b) return;
  b.classList.toggle('bgmode-acrylic', mode === 'acrylic');
  try {
    const el = document.getElementById('bgimg');
    if (el) el.classList.toggle('acrylic', mode === 'acrylic');
  } catch (_e) {}
  try { if (window.Poster && window.Poster.setMode) window.Poster.setMode(mode); } catch (_e) {}
}

/* 无效果基线也注册成插件，这样注册表里 poster/acrylic 都是可替换的项 */
export const posterEffect = {
  id: 'poster', type: 'bg', name: '歌词海报',
  onEnable(){ apply('poster'); },
  onDisable(){},
};
export const acrylicEffect = {
  id: 'acrylic', type: 'bg', name: '亚克力',
  onEnable(){ apply('acrylic'); },
  onDisable(){ apply('poster'); },
};

export function install(){
  registerEffect(posterEffect);
  return registerEffect(acrylicEffect);
}
export const _diag = { apply };
