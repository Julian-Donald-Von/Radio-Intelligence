/* newui/js/layout-ui.js — 播放器布局预设/微调的 UI 绑定（P2）。
 * 依赖 main.js 已 layout.install()（window.__layout 就绪）。
 * 默认零变化；预设/微调写入 r54.layout；恢复默认一键回滚。
 */
import layout from './layout.js';

const q = id => document.getElementById(id);

function reflect(L){
  // 预设按钮高亮（找与当前值最匹配的预设）
  const seg = q('ly-presets');
  if (seg) seg.querySelectorAll('[data-preset]').forEach(b =>
    b.classList.toggle('active', b.getAttribute('data-preset') === L._preset));
  const set=(id,v)=>{ const el=q(id); if(el) el.value=v; };
  const txt=(id,v)=>{ const el=q(id); if(el) el.textContent=v; };
  set('ly-dock-bottom',L.dock.bottom); txt('ly-dock-bottom-v',L.dock.bottom);
  set('ly-dock-height',L.dock.height); txt('ly-dock-height-v',L.dock.height);
  set('ly-aibar-x',L.aibar.offsetX);  txt('ly-aibar-x-v',L.aibar.offsetX);
  set('ly-card-arc',L.cards.arcStep); txt('ly-card-arc-v',Number(L.cards.arcStep).toFixed(3));
  // 让 CSS 变量与 dock 尺寸同步（computeFramePath 读 getBoundingClientRect 会自动重算）
  const bd=document.body;
  if (bd){
    bd.style.setProperty('--layout-dock-bottom', L.dock.bottom+'px');
    bd.style.setProperty('--layout-dock-height', L.dock.height+'px');
  }
}

function reflow(){
  // 触发已有 resize 监听器 → positionAIBar / positionToast / computeFramePath 重锚
  try { window.dispatchEvent(new Event('resize')); } catch (_e) {}
}

export function bootLayoutUI(){
  const L = layout.get();
  // 记当前预设名（存在 layout 的临时字段，不入 localStorage 也能工作）
  let presetName = 'classic';
  try { presetName = JSON.parse(localStorage.getItem('r54.layout')||'{}')._preset || 'classic'; } catch(_e){}
  reflect({ ...L, _preset: presetName });

  const seg = q('ly-presets');
  if (seg) seg.querySelectorAll('[data-preset]').forEach(b =>
    b.addEventListener('click', () => {
      const name = b.getAttribute('data-preset');
      if (!layout.applyPreset(name)) return;
      try { const o = JSON.parse(localStorage.getItem('r54.layout')||'{}'); o._preset=name;
            localStorage.setItem('r54.layout', JSON.stringify(o)); } catch(_e){}
      reflect({ ...layout.get(), _preset: name });
      reflow();
    }));

  const bindRange = (id, path, fmt) => {
    const el = q(id); if (!el) return;
    el.addEventListener('input', () => {
      const v = Number(el.value);
      const patch = {};
      const parts = path.split('.');
      patch[parts[0]] = { [parts[1]]: v };
      layout.set(patch);
      const vEl = q(id+'-v'); if (vEl) vEl.textContent = fmt ? fmt(v) : v;
      // 手动微调后预设高亮取消（已成为自定义布局）
      if (seg) seg.querySelectorAll('[data-preset]').forEach(b=>b.classList.remove('active'));
      reflow();
    });
  };
  bindRange('ly-dock-bottom','dock.bottom');
  bindRange('ly-dock-height','dock.height');
  bindRange('ly-aibar-x','aibar.offsetX');
  bindRange('ly-card-arc','cards.arcStep', v=>Number(v).toFixed(3));

  const rst = q('ly-reset');
  if (rst) rst.addEventListener('click', () => {
    layout.reset();
    try { const o=JSON.parse(localStorage.getItem('r54.layout')||'{}'); o._preset='classic';
          localStorage.setItem('r54.layout', JSON.stringify(o)); } catch(_e){}
    reflect({ ...layout.get(), _preset:'classic' });
    reflow();
  });

  window.__layoutUI = { reflect: () => reflect(layout.get()), presets: () => layout.PRESETS };
  return window.__layoutUI;
}

/* 不自动 boot——ESM import 静态提升会让本模块顶层早于 main.js 的 layout.install() 执行；
   由 main.js 在 install 之后显式调用 bootLayoutUI()。 */
export default bootLayoutUI;
