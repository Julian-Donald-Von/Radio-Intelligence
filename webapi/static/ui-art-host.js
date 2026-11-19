/*!
 * ui-art-host.js — 美术插件宿主 / 可注册接口（P1 骨架）
 * 新增文件；不修改任何冻结核心。加载顺序：ui-art-gate.js → ui-art-bus.js → ui-art-host.js。
 * 职责：发现/校验/加载/启停插件；暴露 window.ArtHost 与只读诊断面 window.__art。
 * P1 阶段：registry 为空骨架；不自动启用任何插件（零视觉变化）。
 */
(function () {
  'use strict';

  var gate = window.ArtGate;
  var bus = window.ArtBus;
  if (!gate || !bus) { console.error('[ArtHost] gate/bus missing'); return; }

  var REGISTRY = '/art-plugins/registry.json';
  var plugins = Object.create(null);   // id -> { manifest, impl, state, ctx, hooks }
  var errors = [];
  var maxCallbackMs = 0;

  function record(code, detail) {
    errors.push({ at: new Date().toISOString(), code: code, detail: String(detail || '') });
    if (errors.length > 50) errors.shift();
  }

  /**
   * 可注册接口：外部脚本/插件可调用。
   * @param {object} manifest 必须符合 gate.checkManifest
   * @param {object} impl     { mount(ctx), unmount(), onConfig?(next), onBus?(t,p) }
   * @param {string} source   插件源码文本（门禁静态检查用）
   * @returns {{ok:boolean, code:string, reason:string}}
   */
  function register(manifest, impl, source) {
    var g = gate.checkPair(manifest, source);
    if (!g.ok) { record(g.code, g.reason); return g; }
    if (plugins[manifest.id]) {
      return { ok: false, code: 'E_DUPLICATE', reason: 'id already registered' };
    }
    if (typeof impl !== 'object' || typeof impl.mount !== 'function' || typeof impl.unmount !== 'function') {
      return { ok: false, code: 'E_BAD_IMPL', reason: 'impl must provide mount(ctx) and unmount()' };
    }
    plugins[manifest.id] = {
      manifest: manifest,
      impl: impl,
      state: 'registered',
      ctx: null,
      hooks: []
    };
    return { ok: true, code: 'OK', reason: 'registered' };
  }

  function enable(id, ctxOverride) {
    var p = plugins[id];
    if (!p) return { ok: false, code: 'E_NOT_FOUND', reason: id };
    if (p.state === 'activated') return { ok: true, code: 'OK', reason: 'already active' };
    var ctx = ctxOverride || {
      root: document.body,
      bus: bus,
      prefs: (p.manifest.prefs || []).reduce(function (o, x) { o[x.key] = x.default; return o; }, {}),
      host: { version: gate.HOST_API_MIN }
    };
    try {
      var t0 = performance.now();
      var res = p.impl.mount(ctx);
      var dt = performance.now() - t0;
      if (dt > maxCallbackMs) maxCallbackMs = dt;
      p.ctx = ctx;
      p.res = res;
      p.state = 'activated';
      // 订阅声明的 bus 主题（限流由 bus 负责）
      (p.manifest.capabilities || []).forEach(function (c) {
        var m = /^bus:([a-z.]+)@/.exec(c);
        if (m && typeof p.impl.onBus === 'function') {
          p.hooks.push(bus.on(m[1], function (payload) {
            var t1 = performance.now();
            try { p.impl.onBus(m[1], payload); }
            catch (e) { record('E_PLUGIN_CALLBACK', m[1] + ': ' + e); }
            var d = performance.now() - t1;
            if (d > maxCallbackMs) maxCallbackMs = d;
            if (d > 8) { p.state = 'broken'; record('E_SLOW_CALLBACK', id + ' ' + d.toFixed(2) + 'ms'); }
          }));
        }
      });
      return { ok: true, code: 'OK', reason: 'activated in ' + dt.toFixed(2) + 'ms' };
    } catch (e) {
      p.state = 'broken';
      record('E_MOUNT', id + ': ' + e);
      return { ok: false, code: 'E_MOUNT', reason: String(e) };
    }
  }

  function disable(id) {
    var p = plugins[id];
    if (!p) return { ok: false, code: 'E_NOT_FOUND', reason: id };
    if (p.state !== 'activated') return { ok: true, code: 'OK', reason: 'not active' };
    try {
      if (p.res && typeof p.res.unmount === 'function') p.res.unmount();
      else if (typeof p.impl.unmount === 'function') p.impl.unmount(p.ctx);
    } catch (e) { record('E_UNMOUNT', id + ': ' + e); }
    p.hooks.forEach(function (off) { try { off(); } catch (_) {} });
    p.hooks = [];
    p.ctx = null;
    p.state = 'deactivated';
    return { ok: true, code: 'OK', reason: 'deactivated' };
  }

  function list() {
    return Object.keys(plugins).map(function (id) {
      return {
        id: id,
        state: plugins[id].state,
        slot: plugins[id].manifest.slot,
        execution: plugins[id].manifest.execution
      };
    });
  }

  function probe() {
    return {
      registered: Object.keys(plugins).length,
      errors: errors.slice(-10),
      maxCallbackMs: maxCallbackMs,
      bus: bus.stats()
    };
  }

  /**
   * 源码加载：fetch → gate.checkSource → 注入经典脚本执行 → 取 window.ArtPlugins[id]。
   * 先门禁后执行，冻结核心引用/RAF/hot 执行一律在运行前被拒。
   * 插件是 IIFE（非 ESM），故用 script 注入而非 import()，与"共享顶层作用域"现状兼容。
   */
  function hydrate(id) {
    var p = plugins[id];
    if (!p || !p.lazy || p.state === 'loaded' || p.state === 'activated') {
      return Promise.resolve({ ok: false, code: 'E_SKIP', reason: 'no lazy source or already loaded' });
    }
    return fetch(p.lazy, { cache: 'no-store' })
      .then(function (r) {
        if (!r.ok) throw new Error('source HTTP ' + r.status);
        return r.text();
      })
      .then(function (src) {
        var g = gate.checkSource(src, p.manifest && p.manifest.execution);
        if (!g.ok) { record(g.code, id + ': ' + g.reason); p.state = 'broken'; return g; }
        var s = document.createElement('script');
        s.textContent = src;
        document.head.appendChild(s);
        var impl = window.ArtPlugins && window.ArtPlugins[id];
        if (!impl || typeof impl.mount !== 'function') {
          p.state = 'broken';
          record('E_NO_IMPL', id + ': window.ArtPlugins[' + id + '] missing');
          return { ok: false, code: 'E_NO_IMPL', reason: 'plugin did not expose mount()' };
        }
        p.impl = impl;
        p.source = src;
        p.state = 'loaded';
        return { ok: true, code: 'OK', reason: 'source loaded + gated' };
      })
      .catch(function (e) {
        p.state = 'broken';
        record('E_HYDRATE', id + ': ' + e);
        return { ok: false, code: 'E_HYDRATE', reason: String(e) };
      });
  }

  function loadRegistry() {
    return fetch(REGISTRY, { cache: 'no-store' })
      .then(function (r) {
        if (!r.ok) throw new Error('registry HTTP ' + r.status);
        return r.json();
      })
      .then(function (reg) {
        (reg.plugins || []).forEach(function (entry) {
          var id = entry.id || 'unknown';
          var m = entry.manifest;
          var g = gate.checkManifest(m);
          if (!g.ok) { record(g.code, id + ': ' + g.reason); return; }
          plugins[id] = {
            manifest: m,
            impl: { mount: function () { return { unmount: function () {} }; }, unmount: function () {} },
            state: 'discovered',
            ctx: null,
            hooks: [],
            lazy: entry.entry ? '/art-plugins/' + (entry.dir || id) + '/' + entry.entry : null
          };
        });
        bus.emit('plugins.changed', { count: Object.keys(plugins).length });
      })
      .catch(function (e) { record('E_REGISTRY', e); });
  }

  window.ArtHost = {
    register: register,
    enable: enable,
    disable: disable,
    list: list,
    probe: probe,
    reload: loadRegistry,
    hydrate: hydrate,
    forbiddenSlots: function () { return gate.FORBIDDEN_SLOTS.slice(); },
    registrableSlots: function () { return gate.REGISTRABLE_SLOTS.slice(); }
  };
  // 只读诊断面：对齐 __water / __r54 黑盒约定（D-7 保持钩子）
  window.__art = { probe: probe, list: list, hydrate: hydrate };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', loadRegistry);
  } else {
    loadRegistry();
  }
})();
