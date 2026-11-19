/* ══════════════════════════════════════════════════════════════════
   ui-interaction-fix.js — 交互层后置补丁（2026-11-18 18:3x，用户放行"改 UI 单起一个文件"）
   ──────────────────────────────────────────────────────────────────
   职责（只动交互，不碰水体和任何视觉效果）：
     F1 模式按钮三态点亮（原只 loop_one 亮，random 永远不亮）
     F2 后端状态回填（原 applyState 不读 mode_effective/mode_locked）
     F3 按钮提前可用（原 disabled=!st.ready，启动期一直点不动）
     F4 禁用网页右键菜单（输入框放行，不影响复制粘贴）
     F5 禁用左键长按的文本选中高亮（输入框放行）
     F6 poll / loadCards in-flight 去重（API 并发）
     F7 WS 断线指数退避重连（前后端连接速度/稳定性）
   纪律：不改任何冻结核心源码；只包装全局函数 + 注入一张 style。
        删掉 index.html 那一行 <script> 即完全复原。
        纯函数挂在 window.__uiFix，Node 可 require 测试。
   ══════════════════════════════════════════════════════════════════ */
(function (root, factory) {
  var api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.__uiFix = api;
})(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this), function (_root) {
  'use strict';

  /* ⚠ 浏览器 classic script 的全局对象就是 window；Node 测试无 window，回退 globalThis。
     工厂参数 _root 必须显式接住（曾漏接导致浏览器侧 root 未定义 → 整个补丁失效）。 */
  var root = _root || (typeof window !== 'undefined' ? window
            : (typeof globalThis !== 'undefined' ? globalThis : null));

  var VERSION = '1.0.0';
  var MODE_NAMES = { order: '顺序播放', loop_one: '单曲循环', random: '随机播放' };
  var VALID_MODES = ['order', 'loop_one', 'random'];
  var UNLOCK_IDS = ['d-mode', 'd-play', 'd-prev', 'd-next',
                    'd-fav', 'd-dislike', 'd-lyric', 'd-queue', 'd-history'];
  var WS_MAX_BACKOFF = 15000;

  /* ---------- 纯函数（Node 可测，无 DOM 依赖） ---------- */

  // F1+F3：算按钮应有视觉。三态都点亮（各自 data-mode + 色相区分），locked 置灰。
  function modeVisual(eff, locked) {
    var key = VALID_MODES.indexOf(eff) >= 0 ? eff : 'order';
    var name = MODE_NAMES[key];
    return {
      on: true,
      dataMode: key,
      opacity: locked ? '0.4' : '',
      cursor: locked ? 'not-allowed' : '',
      title: locked
        ? '漫游 / 推荐下不可切换播放模式（只按顺序播放）'
        : ('播放模式：' + name)
    };
  }

  // F2：后端权威模式。mode_effective 优先（漫游下被强制为 order），其次 mode，否则保留原值。
  function effFromState(d, prev) {
    if (!d) return prev;
    var v = d.mode_effective || d.mode;
    return VALID_MODES.indexOf(v) >= 0 ? v : (VALID_MODES.indexOf(prev) >= 0 ? prev : 'order');
  }

  // F7：指数退避，封顶 15s。
  function wsBackoff(attempt) {
    var n = Math.max(0, attempt | 0);
    var ms = 1000 * Math.pow(2, n);
    return ms > WS_MAX_BACKOFF ? WS_MAX_BACKOFF : ms;
  }

  function unlockTargets() { return UNLOCK_IDS.slice(); }

  // 把一个视觉状态写到按钮上（含 F3 解锁）。纯逻辑，mock 可测。
  function applyVisual(btn, v) {
    if (!btn) return false;
    try {
      if (btn.classList && btn.classList.toggle) btn.classList.toggle('on', !!v.on);
      if (btn.dataset) btn.dataset.mode = v.dataMode;
      else if (btn.setAttribute) btn.setAttribute('data-mode', v.dataMode);
      if (btn.style) { btn.style.opacity = v.opacity || ''; btn.style.cursor = v.cursor || ''; }
      if (btn.setAttribute) btn.setAttribute('title', v.title);
      btn.disabled = false;               // F3：任何时候都不因 st.ready 卡住
      return true;
    } catch (e) { return false; }
  }

  var api = {
    version: VERSION,
    modeVisual: modeVisual,
    effFromState: effFromState,
    wsBackoff: wsBackoff,
    unlockTargets: unlockTargets,
    applyVisual: applyVisual,
    restore: function () { return typeof api._restore === 'function' ? api._restore() : false; }
  };

  /* ---------- 浏览器侧：后置包装（Node 无 document 时跳过） ---------- */
  if (typeof document === 'undefined') return api;

  (function initBrowser() {
    var doc = document;
    var installed = [];   // 记录被包装的函数，供 restore 复原

    function wrapGlobal(name, maker) {
      var base = root[name];
      if (typeof base !== 'function') return null;
      var wrapped = maker(base);
      root[name] = wrapped;
      installed.push({ name: name, base: base });
      return wrapped;
    }

    /* -- F4+F5：注入 CSS（右键菜单由事件拦截；文本选中由 CSS） -- */
    (function injectStyle() {
      if (doc.getElementById('ui-interaction-fix-style')) return;
      var s = doc.createElement('style');
      s.id = 'ui-interaction-fix-style';
      s.textContent = [
        '/* ui-interaction-fix: 只禁选中/右键，不改配色与布局 */',
        'html,body{-webkit-user-select:none;user-select:none}',
        'input,textarea,[contenteditable="true"]{-webkit-user-select:text!important;user-select:text!important}',
        '#d-mode[data-mode="order"]{color:var(--accent)}',
        '#d-mode[data-mode="loop_one"]{color:#7ec8ff;filter:drop-shadow(0 0 8px rgba(126,200,255,.65))}',
        '#d-mode[data-mode="random"]{color:#d3a4ff;filter:drop-shadow(0 0 8px rgba(211,164,255,.65))}'
      ].join('\n');
      (doc.head || doc.documentElement).appendChild(s);
    })();

    /* -- F4：禁网页右键，输入框/可编辑区放行 -- */
    doc.addEventListener('contextmenu', function (e) {
      var t = e.target;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
      e.preventDefault();
    }, true);

    /* -- F2：applyState 先回填 modeEff / modeLocked，再走原逻辑 -- */
    wrapGlobal('applyState', function (base) {
      return function (d) {
        try {
          var prev = (typeof st !== 'undefined' && st.modeEff) || 'order';
          var eff = effFromState(d, prev);
          if (typeof st !== 'undefined') {
            st.modeEff = eff;
            if (d && typeof d.mode_locked === 'boolean') st.modeLocked = d.mode_locked;
          }
        } catch (_e) {}
        return base.apply(this, arguments);
      };
    });

    /* -- F1+F3：render 之后修正三态点亮 + 解锁按钮 -- */
    function patchButtons() {
      var eff = (typeof st !== 'undefined' && st.modeEff) || 'order';
      var locked = (typeof st !== 'undefined' && !!st.modeLocked);
      var v = modeVisual(eff, locked);
      var b = doc.getElementById('d-mode');
      if (b) applyVisual(b, v);
      unlockTargets().forEach(function (id) {
        var el = doc.getElementById(id);
        if (el) el.disabled = false;
      });
    }
    wrapGlobal('render', function (base) {
      return function () {
        var r = base.apply(this, arguments);
        try { patchButtons(); } catch (_e) {}
        return r;
      };
    });

    /* -- F6：poll / loadCards in-flight 去重（不堆积并发请求） -- */
    wrapGlobal('poll', function (base) {
      var inflight = false;
      return function () {
        if (inflight) return (typeof Promise !== 'undefined') ? Promise.resolve() : undefined;
        inflight = true;
        var ret;
        try { ret = base.apply(this, arguments); }
        catch (e) { inflight = false; throw e; }
        if (ret && typeof ret.then === 'function') {
          return ret.then(function (v) { inflight = false; return v; },
                          function (e) { inflight = false; throw e; });
        }
        inflight = false;
        return ret;
      };
    });
    wrapGlobal('loadCards', function (base) {
      var inflight = false;
      return function () {
        if (inflight) return;
        inflight = true;
        try { return base.apply(this, arguments); }
        finally { inflight = false; }
      };
    });

    /* -- F7：WS 断线指数退避重连，复用原 handlers -- */
    (function wsReconnect() {
      var w;
      try { w = (typeof ws === 'undefined') ? null : ws; } catch (_e) { w = null; }
      if (!w) return;
      var H = { onopen: w.onopen, onclose: w.onclose, onerror: w.onerror, onmessage: w.onmessage };
      var attempt = 0, timer = null, stopped = false;

      function onClose(ev) {
        try { if (H.onclose) H.onclose(ev); } catch (_e) {}
        if (stopped) return;
        timer = setTimeout(reconnect, wsBackoff(attempt++));
      }
      function reconnect() {
        if (stopped) return;
        try {
          var proto = (location.protocol === 'https:' ? 'wss:' : 'ws:');
          var nw = new WebSocket(proto + '//' + location.host + '/ws');
          nw.onopen = H.onopen;
          nw.onmessage = H.onmessage;
          nw.onerror = H.onerror;
          nw.onclose = onClose;
          ws = nw;                                   // 更新全局词法绑定（ping 定时器引用同一绑定）
          attempt = 0;                               // 连上后退避归零
          setTimeout(function () { try { root.poll && root.poll(); } catch (_e) {} }, 120);
        } catch (_e) {
          timer = setTimeout(reconnect, wsBackoff(attempt++));
        }
      }
      w.onclose = onClose;
      api._stopReconnect = function () { stopped = true; if (timer) clearTimeout(timer); };
    })();

    /* -- 首屏立即补一次（不等 2s 轮询） -- */
    try { patchButtons(); } catch (_e) {}

    api._restore = function () {
      api._stopReconnect && api._stopReconnect();
      installed.slice().reverse().forEach(function (x) { root[x.name] = x.base; });
      installed.length = 0;
      var s = doc.getElementById('ui-interaction-fix-style');
      if (s && s.parentNode) s.parentNode.removeChild(s);
      return true;
    };
  })();

  return api;
});
