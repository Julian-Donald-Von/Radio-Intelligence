/* newui/js/registry.js — 模块注册表（自动生成，勿手改）。
 * register(name, deps, factory) → 依赖拓扑序 boot。
 */
import { ctx, def } from './ctx.js';

const registry = new Map();
const booted = new Set();

export function register(name, deps, factory){
  if (registry.has(name)) return false;
  registry.set(name, { name, deps: deps || [], factory });
  return true;
}
export function unregister(name){ const r = registry.get(name); if (r) deleteRegistry(name); return !!r; }
function deleteRegistry(name){ registry.delete(name); booted.delete(name); }

export function list(){ return Array.from(registry.keys()); }
export function isBooted(name){ return booted.has(name); }

export async function boot(){
  const visiting = new Set();
  async function run(name){
    if (booted.has(name)) return;
    if (visiting.has(name)) throw new Error('cyclic dep: ' + name);
    const m = registry.get(name);
    if (!m) throw new Error('unknown module: ' + name);
    visiting.add(name);
    for (const d of m.deps) await run(d);
    visiting.delete(name);
    const api = await m.factory(ctx);
    def(name, api);
    booted.add(name);
  }
  for (const name of registry.keys()) await run(name);
  return { booted: Array.from(booted), total: registry.size };
}

export default { register, unregister, boot, list, isBooted };
