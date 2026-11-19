/* newui/js/effects.js — 视觉效果插件注册表（新架构）。
 *
 * 用户 2026-11-18：「水的整体以及水在前端的换肤设置所有页面、叠层效果水面整体
 * 是一个模块；叠层效果可以通过插件导入；背景效果的亚克力同理」。
 *
 * 两类效果槽（与老 UI R93 契约一致，但从硬编码改成注册表驱动）：
 *   type 'ontop'  — 叠层效果，叠在背景效果之上（none / water / 未来任意插件）
 *   type 'bg'     — 背景效果，作用于背景皮肤（poster / acrylic / 未来任意插件）
 *
 * 每个插件 = { id, type, name, params?, onEnable, onDisable, applyParams? }
 *   · params 描述设置页参数区（可选）——"每个 ontop 插件自带一个参数组"
 *   · UI 从注册表自动生成 tab + 参数区，不再写死 ONTOP_IDS / bgmode
 *   · 切换 = onDisable(旧) → onEnable(新) → 持久化 → 发 'effectchange' 事件
 */
const registry = new Map();          // id -> plugin
const active = { ontop: 'none', bg: 'poster' };
const listeners = new Set();
const KEY = 'r54.effects';

export function onChange(fn){ listeners.add(fn); return () => listeners.delete(fn); }
function emit(evt){ listeners.forEach(f => { try { f(evt, snapshot()); } catch (_e) {} }); }
export function snapshot(){ return { active: { ...active }, plugins: list() }; }
export function list(type){
  const all = Array.from(registry.values());
  return (type ? all.filter(p => p.type === type) : all).map(p => ({
    id: p.id, type: p.type, name: p.name, hasParams: !!(p.params && p.params.length),
  }));
}

export function registerEffect(plugin){
  if (!plugin || !plugin.id || !plugin.type) throw new Error('effect needs id + type');
  if (registry.has(plugin.id)) return false;
  registry.set(plugin.id, plugin);
  emit('register');
  return true;
}
export function unregisterEffect(id){
  const p = registry.get(id);
  if (!p) return false;
  if (active[p.type] === id){
    // 先调被卸插件的 onDisable，再把 active 重置为基线 id（基线可能未注册，
    // 不能走 setActive 的注册检查），最后清掉残留。
    try { p.onDisable && p.onDisable(); } catch (_e) {}
    active[p.type] = p.type === 'ontop' ? 'none' : 'poster';
    save();
  }
  registry.delete(id);
  emit('unregister');
  return true;
}

export function setActive(type, id, { persist = true } = {}){
  const next = registry.get(id);
  if (!next || next.type !== type) return false;
  if (active[type] === id) return true;
  const prev = registry.get(active[type]);
  try { prev && prev.onDisable && prev.onDisable(); } catch (_e) {}
  active[type] = id;
  try { next.onEnable && next.onEnable(); } catch (_e) {}
  if (persist) save();
  emit('active');
  return true;
}
export function getActive(type){ return active[type]; }
export function applyParams(id, values){
  const p = registry.get(id);
  if (!p || !p.applyParams) return;
  try { p.applyParams(values); } catch (_e) {}
}

function save(){
  try { localStorage.setItem(KEY, JSON.stringify({ ...active })); } catch (_e) {}
  /* 2026-11-19 记忆修复：同步水体真正读取的源（ui-water.loadPref 读 r54.ui + /api/ui/prefs）。 */
  try {
    const f = JSON.parse(localStorage.getItem('r54.ui') || '{}');
    f.ontop = active.ontop;
    f.water = active.ontop === 'water';
    localStorage.setItem('r54.ui', JSON.stringify(f));
  } catch (_e) {}
  try {
    const body = { ontop: active.ontop, water: active.ontop === 'water' };
    /* 2026-11-19 Qt 记忆修复：bg 效果（poster/acrylic）也落后端，别只依赖 localStorage。 */
    if (active.bg === 'poster' || active.bg === 'acrylic') body.bgmode = active.bg;
    fetch('/api/ui/prefs', { method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body) }).catch(()=>{});
  } catch (_e) {}
}
export function restore(){
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch (_e) {}
  for (const type of ['ontop', 'bg']){
    const id = saved[type];
    if (id && registry.has(id)) {
      const p = registry.get(id);
      try { p.onEnable && p.onEnable(); } catch (_e) {}
      active[type] = id;
    }
  }
  emit('restore');
  return snapshot();
}

/* 从注册表生成设置页的 tab 与参数区（挂到指定容器）。
   容器不存在时静默跳过——兼容还没接 UI 的场景。 */
export function mountUI({ ontopRow, bgRow, paramsHost } = {}){
  // 保存两排容器 + 属性名，供 syncTabs 刷新高亮（用户 11-18：选中态被扔了）
  const rows = [];
  function fill(row, type, attr){
    if (!row) return;
    const plugins = list(type);
    row.innerHTML = plugins.map(p =>
      '<button class="wp-type" data-' + attr + '="' + p.id + '">' + p.name + '</button>').join('');
    row.querySelectorAll('[data-' + attr + ']').forEach(btn =>
      btn.addEventListener('click', () => setActive(type, btn.getAttribute('data-' + attr))));
    rows.push({ row, type, attr });
  }
  fill(typeof ontopRow === 'string' ? document.querySelector(ontopRow) : ontopRow, 'ontop', 'ontop');
  fill(typeof bgRow === 'string' ? document.querySelector(bgRow) : bgRow, 'bg', 'bgmode');
  // ★ 高亮同步：老 UI 的 syncOnTopUI/syncBgModeUI 因 innerHTML 被重写而失效，
  //   这里成为新的唯一同步点——任何 active 变化都刷新两排按钮的 active 类。
  function syncTabs(snap){
    const s = snap || snapshot();
    for (const { row, type, attr } of rows){
      const id = s.active[type];
      row.querySelectorAll('[data-' + attr + ']').forEach(btn =>
        btn.classList.toggle('active', btn.getAttribute('data-' + attr) === id));
    }
  }
  syncTabs();                                   // 初次装配就点亮当前选择
  renderParams(paramsHost);                     // 只切参数区显隐，不碰老 UI 内容
  onChange((_evt, s) => { syncTabs(s); renderParams(paramsHost, s); });
}
/* 参数区显隐（用户 11-18：情绪强度/波纹细度等被冲掉了）。
   ★ 不再 innerHTML 重写——老 UI 的 #water-opts 里有完整参数区
   （波纹细度/情绪强度/画质档位/换一批波形/恢复默认/高级），且有 bindWaterOpts
   完整绑定。这里只按当前 ontop 插件是否有参数决定显示/隐藏，内容交给老 UI。 */
function renderParams(host, snap){
  const p = registry.get((snap || snapshot()).active.ontop);
  const id = p ? p.id : 'none';
  // 水面参数区（老 UI 完整绑定）：仅 water 显示
  if (host) host.classList.toggle('open', id === 'water');
  // T23：音域回响参数区（独立 DOM，不冲掉水面参数）
  const sonic = document.getElementById('sonic-opts');
  if (sonic) sonic.classList.toggle('open', id === 'sonic');
}

/* ★ 保留备用（用户 11-18「不用就不用，不要删掉，不然容易出问题」）：
   注册表驱动的参数区生成器——按当前 ontop 插件的 params 描述生成滑块并绑定。
   新 UI 当前不调用它（水面参数区交给老 UI 的完整 #water-opts DOM + bindWaterOpts），
   但代码原样留着：若将来某个 ontop 插件要自带参数区，一行切回即可。
   ⚠ 不要删除此函数；它与老 UI 参数区是两条并存路径，用哪条由调用方决定。 */
function renderParamsFromRegistry(host, snap){
  if (!host) return;
  const p = registry.get((snap || snapshot()).active.ontop);
  if (!p || !p.params || !p.params.length){ host.innerHTML = ''; host.classList.remove('open'); return; }
  host.classList.add('open');
  host.innerHTML = p.params.map(par =>
    '<div class="wo-row"><label class="set-label">' + par.label + '</label>'
    + '<input class="wo-range" type="range" min="' + (par.min ?? 0) + '" max="' + (par.max ?? 1)
    + '" step="' + (par.step ?? 0.01) + '" value="' + (par.value ?? 0) + '" data-k="' + par.key + '">'
    + '<span class="wo-val">' + (par.value ?? 0) + '</span></div>').join('');
  host.querySelectorAll('input[data-k]').forEach(inp => inp.addEventListener('input', () => {
    const k = inp.getAttribute('data-k');
    const val = parseFloat(inp.value);
    const out = {}; out[k] = val;
    inp.nextElementSibling.textContent = inp.value;
    applyParams(p.id, out);
  }));
}

export default { registerEffect, unregisterEffect, setActive, getActive, applyParams,
                 list, snapshot, onChange, restore, mountUI, renderParamsFromRegistry };
