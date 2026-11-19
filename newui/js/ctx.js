/* newui/js/ctx.js — 模块共享上下文（自动生成，勿手改）。
 * 老 UI 的 classic script 靠共享全局作用域通信（st/toast/render 等 120 个符号）。
 * 这里用一个可变对象承载它们：模块 factory 从 ctx 取用、把定义写回 ctx。
 */
const _ctxTarget = Object.create(null);
export let ctx = _ctxTarget;  // 下方包装为 Proxy（with(ctx) 需 Proxy 兜底 DOM 裸标识符）

const _waiters = Object.create(null);
export function def(name, value){
  ctx[name] = value;
  const ws = _waiters[name];
  if (ws){ delete _waiters[name]; ws.forEach(fn => { try { fn(value); } catch (_) {} }); }
  return value;
}
export function use(name){
  if (name in ctx) return ctx[name];
  return new Promise(res => { (_waiters[name] || (_waiters[name] = [])).push(res); });
}
export function has(name){ return name in ctx; }

/* 共享 DOM 裸标识符兜底：老代码里 dock/frameHit/dc-cover 等靠共享全局可见，
   拆模块后用 Proxy 让任何未定义的标识符自动解析为同 id 的元素（或空函数），
   避免跨模块引用漏一个就崩。这是"with(ctx) 模拟共享作用域"的关键补件。 */
const _domCache = Object.create(null);
const _ctxProxy = new Proxy(_ctxTarget, {
  has(t, k){
    if (typeof k !== 'string') return Reflect.has(t, k);
    if (Reflect.has(t, k)) return true;
    // 常见 DOM 裸标识符：以 dock/frame/sp/dc/o/sc/rs 开头，或是已知 id
    if (/^(dock|frame|d-|dc-|o-|sp-|sc-|rs-|eq-|water|bg|aibar|toast|poster|frameHit|framePath|playhead|layer)/.test(k)) return true;
    return false;
  },
  get(t, k){
    if (Reflect.has(t, k)) return Reflect.get(t, k);
    if (typeof k !== 'string') return undefined;
    if (/^(dock|frame|d-|dc-|o-|sp-|sc-|rs-|eq-|water|bg|aibar|toast|poster|frameHit|framePath|playhead|layer)/.test(k)) {
      if (!_domCache[k]) _domCache[k] = (typeof document !== 'undefined' && document.getElementById)
        ? (document.getElementById(k) || document.getElementById(k.replace(/[A-Z]/g, m => '-' + m.toLowerCase()))) : null;
      return _domCache[k];
    }
    return undefined;
  },
});
ctx = _ctxProxy;
export default ctx;

const _hooks = Object.create(null);
export function on(evt, fn){ (_hooks[evt] || (_hooks[evt] = [])).push(fn); return () => {
  const a = _hooks[evt]; const i = a && a.indexOf(fn); if (i >= 0) a.splice(i, 1);
}; }
export function emit(evt, payload){ (_hooks[evt] || []).slice().forEach(fn => {
  try { fn(payload); } catch (_) {} }); }
