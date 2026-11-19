/*!
 * ui-art-gate.js — 美术插件门禁（UMD，宿主与离线 verify 共用同一份判据）
 * 职责：在插件加载前静态拒绝一切触碰冻结核心的声明与源码。
 * 权威决策：docs/UI_ARCH_DECISION_20261117_084816.md §3.1 / §3.2
 * 新增日期：2026-11-17。本文件为新增文件，不修改任何冻结核心。
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ArtGate = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var GATE_VERSION = '1.1.0';
  var HOST_API_MIN = '1.0.0';

  // 冻结核心槽：永不开放注册（用户拍板 2026-11-17 08:48）
  var FORBIDDEN_SLOTS = ['water', 'bg-effect'];

  // 可注册槽：仅冷层装饰特效
  var REGISTRABLE_SLOTS = ['breathing', 'ripple', 'confetti', 'progress-glow', 'lens'];

  var ALLOWED_TYPES = ['art-effect', 'ornament'];
  // 2026-11-19 用户拍板：为堆芯插件开放 hot-limited（自持 rAF + 只读 __mood）。
  // cold 仍保持原禁令；hot-limited 的额外权限由 checkSource(src, execution) 精细放行。
  var ALLOWED_EXECUTION = ['cold', 'hot-limited'];
  var HOT_LIMITED_CAP = 'hot-limited';

  var ALLOWED_CAPABILITIES = [
    'dom:body', 'dom:#dock', 'dom:#aibar', 'dom:#cards',
    'css.vars', 'assets',
    'bus:mood@2hz', 'bus:beats@event', 'bus:track_changed@event',
    'bus:favorite@event', 'bus:art.energy@10hz', 'bus:plugins.changed@event'
  ];

  // 冻结核心全局面：插件源码不得引用（读也不行，避免隐式耦合）
  var FORBIDDEN_GLOBALS = [
    { re: /window\s*\.\s*Water\b/, why: 'frozen window.Water' },
    { re: /window\s*\.\s*__water\b/, why: 'frozen window.__water' },
    { re: /window\s*\.\s*LiquidGlass\b/, why: 'frozen window.LiquidGlass' },
    { re: /window\s*\.\s*LiquidBg\b/, why: 'frozen window.LiquidBg' },
    { re: /window\s*\.\s*Poster\b/, why: 'frozen window.Poster' },
    { re: /window\s*\.\s*PosterState\b/, why: 'frozen window.PosterState' },
    { re: /window\s*\.\s*__feat\b/, why: 'raw snapshot window.__feat' },
    { re: /window\s*\.\s*__spectrum\b/, why: 'raw snapshot window.__spectrum' },
    { re: /window\s*\.\s*__mood\s*=/, why: 'must not WRITE window.__mood' },
    { re: /window\s*\.\s*__beats\b/, why: 'raw snapshot window.__beats' }
  ];
  // hot-limited 可只读情绪快照（堆芯需要每帧 14 维轴），但仍禁止写、禁止 __feat/__spectrum。
  var HOT_FORBIDDEN_GLOBALS = [
    { re: /window\s*\.\s*__feat\b/, why: 'raw snapshot window.__feat' },
    { re: /window\s*\.\s*__spectrum\b/, why: 'raw snapshot window.__spectrum' },
    { re: /window\s*\.\s*__mood\s*=/, why: 'must not WRITE window.__mood' },
    { re: /window\s*\.\s*__beats\b/, why: 'raw snapshot window.__beats' }
  ];

  // 冻结 canvas / 背景节点：插件不得查询或操作
  var FORBIDDEN_SELECTORS = [
    { re: /['"]#water['"]/, why: 'frozen canvas #water' },
    { re: /['"]#bgliquid['"]/, why: 'frozen canvas #bgliquid' },
    { re: /['"]#poster-canvas['"]/, why: 'frozen canvas #poster-canvas' }
  ];

  // 即使出现裸标识符，也禁止写快照 / 禁止进入 rAF 链
  var FORBIDDEN_CALLS = [
    { re: /requestAnimationFrame\s*\(/, why: 'plugin must not own a rAF (cold layer only)', coldOnly: true },
    { re: /transferControlToOffscreen\s*\(/, why: 'OffscreenCanvas not authorized' }
  ];

  function result(ok, code, reason) {
    return { ok: !!ok, code: code || (ok ? 'OK' : 'E_REJECTED'), reason: reason || '' };
  }

  function checkManifest(m) {
    m = m || {};
    if (!m.id || !/^[a-z0-9][a-z0-9._-]{2,63}$/.test(m.id)) {
      return result(false, 'E_BAD_ID', 'id must match ^[a-z0-9][a-z0-9._-]{2,63}$');
    }
    if (!m.name || typeof m.version !== 'string') {
      return result(false, 'E_BAD_META', 'name and version are required');
    }
    if (FORBIDDEN_SLOTS.indexOf(m.slot) >= 0) {
      return result(false, 'E_FORBIDDEN_SLOT', 'slot "' + m.slot + '" is frozen (water/bg-effect)');
    }
    if (REGISTRABLE_SLOTS.indexOf(m.slot) < 0) {
      return result(false, 'E_UNKNOWN_SLOT', 'unknown slot "' + m.slot + '"');
    }
    if (ALLOWED_TYPES.indexOf(m.type) < 0) {
      return result(false, 'E_BAD_TYPE', 'type must be one of ' + ALLOWED_TYPES.join('/'));
    }
    if (ALLOWED_EXECUTION.indexOf(m.execution) < 0) {
      return result(false, 'E_EXECUTION_NOT_OPEN', 'only cold execution is open in P1; got "' + m.execution + '"');
    }
    if (!m.entry || !/^[^\\/]+\.js$/.test(m.entry)) {
      return result(false, 'E_BAD_ENTRY', 'entry must be a plain .js filename in plugin dir');
    }
    var caps = m.capabilities || [];
    if (!Array.isArray(caps)) return result(false, 'E_BAD_CAPABILITIES', 'capabilities must be an array');
    for (var i = 0; i < caps.length; i++) {
      if (ALLOWED_CAPABILITIES.indexOf(caps[i]) < 0) {
        return result(false, 'E_BAD_CAPABILITY', 'capability not allowed: ' + caps[i]);
      }
    }
    // hostApi 仅做前缀兼容检查：接受 ">=1.0.0" 或 "1.x"
    if (m.hostApi && !/(>=?\s*)?1\./.test(String(m.hostApi))) {
      return result(false, 'E_HOST_API', 'incompatible hostApi: ' + m.hostApi);
    }
    return result(true, 'OK', 'manifest accepted');
  }

  function checkSource(src, execution) {
    src = String(src || '');
    var isHot = execution === HOT_LIMITED_CAP;
    var globals = isHot ? HOT_FORBIDDEN_GLOBALS : FORBIDDEN_GLOBALS;
    var i, hit;
    for (i = 0; i < globals.length; i++) {
      hit = globals[i];
      if (hit.re.test(src)) return result(false, 'E_FROZEN_GLOBAL', hit.why);
    }
    for (i = 0; i < FORBIDDEN_SELECTORS.length; i++) {
      hit = FORBIDDEN_SELECTORS[i];
      if (hit.re.test(src)) return result(false, 'E_FROZEN_DOM', hit.why);
    }
    for (i = 0; i < FORBIDDEN_CALLS.length; i++) {
      hit = FORBIDDEN_CALLS[i];
      if (hit.coldOnly && isHot) continue;   // hot-limited 允许自持 rAF
      if (hit.re.test(src)) return result(false, 'E_HOT_PATH', hit.why);
    }
    return result(true, 'OK', 'source accepted');
  }

  function checkPair(m, src) {
    var a = checkManifest(m);
    if (!a.ok) return a;
    var b = checkSource(src, m && m.execution);
    if (!b.ok) return b;
    return result(true, 'OK', 'manifest + source accepted');
  }

  return {
    GATE_VERSION: GATE_VERSION,
    HOST_API_MIN: HOST_API_MIN,
    FORBIDDEN_SLOTS: FORBIDDEN_SLOTS,
    REGISTRABLE_SLOTS: REGISTRABLE_SLOTS,
    ALLOWED_CAPABILITIES: ALLOWED_CAPABILITIES,
    checkManifest: checkManifest,
    checkSource: checkSource,
    checkPair: checkPair
  };
});
