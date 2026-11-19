/* desktop-titlebar.js — 桌面壳的自绘外框 + "像应用而不是网页"（2026-11-18 v2）
 *
 * v2 改动（用户反馈）：
 *   · 关闭栏从**横贯全宽**改成右上角**悬浮胶囊**（只占 logo+三个按钮，不再遮挡主界面），
 *     去掉 body 顶部 34px 占位（不再推挤布局）；
 *   · 新增桌面化行为：禁用文本选中、禁用右键菜单（网页式"重新加载"）、禁图片拖拽。
 *
 * 只在桌面壳里启用标题栏（Qt 注入 window.__MUSICAI_DESKTOP__=true；?titlebar=1 可预览）；
 * 桌面化行为始终启用（这是本地应用，不是网页）。
 * 桥接：fetch('/api/window/action') → FastAPI → Qt 主线程（webapi/window_ctl.py）。
 */
(function () {
  'use strict';
  var DESKTOP = !!window.__MUSICAI_DESKTOP__ || /[?&]titlebar=1/.test(location.search);

  /* ══════════ ① 桌面化行为：禁选中 / 禁右键菜单 / 禁拖拽 ══════════ */
  function installShellFeel() {
    var st = document.createElement('style');
    st.id = 'mi-shell-feel';
    st.textContent = ''
      /* 全局禁选中（输入框/可编辑区除外），并禁掉原生元素拖拽 */
      + 'html, body { -webkit-user-select: none; user-select: none; }'
      + 'input, textarea, select, [contenteditable="true"], [contenteditable=""] {'
      + '  -webkit-user-select: text; user-select: text; }'
      + 'img, a, svg { -webkit-user-drag: none; user-drag: none; }'
      /* 滚动条也按应用风格（细、暗）——存在感更低 */
      + '::-webkit-scrollbar { width: 8px; height: 8px; }'
      + '::-webkit-scrollbar-thumb { background: rgba(255,255,255,.14); border-radius: 4px; }'
      + '::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,.24); }'
      + '::-webkit-scrollbar-track { background: transparent; }';
    (document.head || document.documentElement).appendChild(st);

    /* 右键菜单：非输入框/可编辑区一律吞掉（避免出现"重新加载/检查"等网页菜单） */
    document.addEventListener('contextmenu', function (e) {
      var t = e.target;
      if (t && t.closest && t.closest('input, textarea, select, [contenteditable="true"], [contenteditable=""]')) return;
      e.preventDefault();
    }, true);

    /* 原生拖拽（拖拽图片/链接会显示网页式"幽灵图"） */
    document.addEventListener('dragstart', function (e) {
      var t = e.target;
      if (t && t.closest && t.closest('input, textarea, [contenteditable="true"], [contenteditable=""]')) return;
      e.preventDefault();
    }, true);

    /* Ctrl+滚轮 = 网页式整页缩放；应用里禁用（普通滚轮不受影响） */
    document.addEventListener('wheel', function (e) {
      if (e.ctrlKey) e.preventDefault();
    }, { passive: false, capture: true });

    /* 双击/拖选时的文本选中（双保险：某些元素会局部覆盖 user-select） */
    document.addEventListener('selectstart', function (e) {
      var t = e.target;
      if (t && t.closest && t.closest('input, textarea, select, [contenteditable="true"], [contenteditable=""]')) return;
      e.preventDefault();
    }, true);
  }
  installShellFeel();

  /* 播放状态镜像到 <html class="mi-playing">：黑胶唱片据此旋转/停转
     （不额外请求后端，直接看 dock 播放按钮的 .playing 类，由 runtime 每帧 render 维护） */
  (function syncPlayingClass() {
    function sync() {
      try {
        document.documentElement.classList.toggle(
          'mi-playing', !!document.querySelector('#d-play.playing'));
      } catch (e) { /* 忽略 */ }
    }
    sync();
    setInterval(sync, 400);
  })();

  if (!DESKTOP) return;

  /* ══════════ ② 悬浮胶囊标题栏（不遮挡主界面） ══════════ */
  var EDGES = { L: 1, T: 2, R: 4, B: 8 };

  function act(action, edges) {
    try {
      fetch('/api/window/action', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: action, edges: edges || 0 })
      });
    } catch (e) { /* 忽略 */ }
  }

  function icon(name) {
    var p = {
      min: '<svg width="10" height="10" viewBox="0 0 10 10"><path d="M2 5h6v1.3H2z" fill="currentColor"/></svg>',
      max: '<svg width="10" height="10" viewBox="0 0 10 10"><path d="M2 2h6v6H2z" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>',
      restore: '<svg width="10" height="10" viewBox="0 0 10 10"><path d="M3.2 1.6h5.2v5.2M1.6 3.2h5.2v5.2" fill="none" stroke="currentColor" stroke-width="1.1"/></svg>',
      close: '<svg width="10" height="10" viewBox="0 0 10 10"><path d="M2.4 2.4l5.2 5.2M7.6 2.4L2.4 7.6" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>'
    };
    return p[name] || '';
  }

  var CSS = ''
    /* 胶囊本体：**左上角**悬浮（用户：左上最不挡），不占布局、不遮挡内容 */
    + '#mi-titlebar{position:fixed;top:10px;left:10px;height:32px;z-index:2147483000;'
    + 'display:flex;align-items:center;gap:2px;padding:0 4px 0 9px;border-radius:11px;'
    + 'background:linear-gradient(180deg,rgba(24,27,34,.88),rgba(15,17,22,.80));'
    + 'backdrop-filter:blur(16px) saturate(140%);-webkit-backdrop-filter:blur(16px) saturate(140%);'
    + 'border:1px solid rgba(255,255,255,.10);'
    + 'box-shadow:0 6px 20px rgba(0,0,0,.38), inset 0 1px 0 rgba(255,255,255,.10);'
    + 'color:rgba(236,239,245,.9);font:11px/1 -apple-system,"Segoe UI","Microsoft YaHei",system-ui,sans-serif;'
    + 'user-select:none;-webkit-user-select:none;cursor:default;}'
    + '#mi-titlebar .mi-brand{display:flex;align-items:center;gap:7px;height:100%;padding-right:6px;cursor:grab;}'
    + '#mi-titlebar .mi-brand:active{cursor:grabbing;}'
    + '#mi-titlebar .mi-logo{width:16px;height:16px;border-radius:4px;object-fit:cover;'
    + 'box-shadow:0 0 0 1px rgba(255,255,255,.14);}'
    + '#mi-titlebar .mi-name{font-weight:600;letter-spacing:.07em;text-transform:uppercase;font-size:10px;opacity:.82;}'
    + '#mi-titlebar .mi-sep{width:1px;height:14px;background:rgba(255,255,255,.10);margin:0 3px;}'
    + '#mi-titlebar .mi-btn{width:30px;height:24px;border:0;border-radius:7px;background:transparent;'
    + 'color:rgba(236,239,245,.78);display:flex;align-items:center;justify-content:center;cursor:pointer;'
    + 'transition:background .12s ease,color .12s ease;padding:0;}'
    + '#mi-titlebar .mi-btn:hover{background:rgba(255,255,255,.10);color:#fff;}'
    + '#mi-titlebar .mi-btn.mi-close:hover{background:#e81123;color:#fff;}'
    /* 右侧抽屉（设置/换肤）打开时，胶囊自动淡出——避免挡住抽屉右上角的关闭按钮。
       抽屉与胶囊都是 body 的直接子节点且胶囊在后 ⇒ 通用兄弟选择器可命中。 */
    + '#mi-titlebar{transition:opacity .18s ease, transform .18s ease;}'
    + '#mi-titlebar.mi-idle{opacity:0; pointer-events:none; transform:translateY(-8px);}'
    + 'body.drawer-open #mi-titlebar,'
    + '#settings-panel.open ~ #mi-titlebar,'
    + '#theme-panel.open ~ #mi-titlebar'
    + '{opacity:0; pointer-events:none; transform:translateY(-8px);}'
    /* 窗口缩放热区（保持无边框窗口可拉边） */
    + '#mi-resize{position:fixed;inset:0;z-index:2147482999;pointer-events:none;}'
    + '#mi-resize i{position:fixed;pointer-events:auto;display:block;}'
    + '#mi-resize .h{left:8px;right:8px;height:4px;}'
    + '#mi-resize .v{top:8px;bottom:8px;width:4px;}'
    + '#mi-resize .n{top:0;} #mi-resize .s{bottom:0;}'
    + '#mi-resize .w{left:0;} #mi-resize .e{right:0;}'
    + '#mi-resize .nw,#mi-resize .ne,#mi-resize .sw,#mi-resize .se{width:8px;height:8px;}'
    + '#mi-resize .nw{top:0;left:0;cursor:nwse-resize;} #mi-resize .se{bottom:0;right:0;cursor:nwse-resize;}'
    + '#mi-resize .ne{top:0;right:0;cursor:nesw-resize;} #mi-resize .sw{bottom:0;left:0;cursor:nesw-resize;}'
    + '#mi-resize .n,#mi-resize .s{cursor:ns-resize;} #mi-resize .w,#mi-resize .e{cursor:ew-resize;}';

  function boot() {
    if (document.getElementById('mi-titlebar')) return;
    var st = document.createElement('style');
    st.textContent = CSS;
    document.head.appendChild(st);

    var bar = document.createElement('div');
    bar.id = 'mi-titlebar';
    bar.innerHTML =
      '<div class="mi-brand" title="拖动移动窗口 / 双击最大化">'
      + '<img class="mi-logo" src="/brand/logo_small.png" alt="">'
      + '<span class="mi-name">Radio Intelligence</span>'
      + '</div>'
      + '<span class="mi-sep"></span>'
      + '<button class="mi-btn" data-a="minimize" title="最小化">' + icon('min') + '</button>'
      + '<button class="mi-btn" data-a="maximize" title="最大化">' + icon('max') + '</button>'
      + '<button class="mi-btn mi-close" data-a="close" title="关闭">' + icon('close') + '</button>';
    document.body.appendChild(bar);

    /* 拖拽：在品牌区按下 → 交给 Qt startSystemMove；双击最大化 */
    var brand = bar.querySelector('.mi-brand');
    brand.addEventListener('mousedown', function (e) {
      if (e.button !== 0 || e.detail === 2) return;
      act('move');
    });
    brand.addEventListener('dblclick', function () { act('maximize'); });

    bar.querySelectorAll('.mi-btn').forEach(function (b) {
      b.addEventListener('mousedown', function (e) { e.stopPropagation(); });
      b.addEventListener('click', function (e) {
        e.stopPropagation();
        act(b.getAttribute('data-a'));
      });
    });

    /* ★ 2026-11-18：无操作 5s 自动隐藏；鼠标靠近左上角（或胶囊本身）出现。
       "操作" = 鼠标移动/点击/滚轮/按键；离开 5s 后淡出，避免常驻遮挡。 */
    var idleTimer = 0;
    function resetIdle() {
      if (idleTimer) clearTimeout(idleTimer);
      idleTimer = setTimeout(function () {
        try { if (!bar.matches(':hover')) bar.classList.add('mi-idle'); } catch (_) {}
      }, 5000);
    }
    function wakePill() {
      bar.classList.remove('mi-idle');
      resetIdle();
    }
    function onActivity(e) {
      try {
        var x = (e && typeof e.clientX === 'number') ? e.clientX : -1;
        var y = (e && typeof e.clientY === 'number') ? e.clientY : -1;
        /* 靠近区：左上角 380×90（含胶囊自身及周围留白） */
        if (x >= 0 && y >= 0 && x <= 380 && y <= 90) wakePill();
        else resetIdle();
      } catch (_) {}
    }
    document.addEventListener('mousemove', onActivity, { passive: true });
    document.addEventListener('mousedown', onActivity, { passive: true });
    document.addEventListener('wheel', onActivity, { passive: true });
    document.addEventListener('keydown', function () { wakePill(); });
    resetIdle();

    /* ★ 键位：F11 全屏（无边框窗口走 /api/window/action；浏览器里退回 HTML 全屏）；
       F5 / Ctrl+R 重新加载（QWebEngine 没有浏览器快捷键，之前"按了没反应"）。 */
    document.addEventListener('keydown', function (e) {
      try {
        if (e.key === 'F11') {
          e.preventDefault();
          act('fullscreen');
          if (!window.__MUSICAI_DESKTOP__ && document.documentElement.requestFullscreen) {
            document.documentElement.requestFullscreen().catch(function () {});
          }
          return;
        }
        var k = (e.key || '').toLowerCase();
        if (e.key === 'F5' || ((e.ctrlKey || e.metaKey) && k === 'r')) {
          e.preventDefault();
          location.reload();
        }
      } catch (_) {}
    });

    /* 最大化图标同步 */
    var maxBtn = bar.querySelector('[data-a="maximize"]');
    function syncMax() {
      try {
        fetch('/api/window/state', { cache: 'no-store' })
          .then(function (r) { return r.json(); })
          .then(function (s) {
            if (!s || s.maximized == null) return;
            maxBtn.innerHTML = s.maximized ? icon('restore') : icon('max');
            maxBtn.title = s.maximized ? '还原' : '最大化';
          })
          .catch(function () {});
      } catch (e) { /* 忽略 */ }
    }
    syncMax();
    setInterval(syncMax, 1500);

    /* 缩放热区 */
    var rz = document.createElement('div');
    rz.id = 'mi-resize';
    rz.innerHTML = '<i class="h n"></i><i class="h s"></i><i class="v w"></i><i class="v e"></i>'
      + '<i class="nw"></i><i class="ne"></i><i class="sw"></i><i class="se"></i>';
    document.body.appendChild(rz);
    var map = { n: 'T', s: 'B', w: 'L', e: 'R', nw: 'LT', ne: 'RT', sw: 'LB', se: 'RB' };
    rz.querySelectorAll('i').forEach(function (h) {
      var cls = ['n', 's', 'w', 'e', 'nw', 'ne', 'sw', 'se'].find(function (c) { return h.classList.contains(c); });
      var code = map[cls].split('').reduce(function (acc, ch) { return acc | (EDGES[ch] || 0); }, 0);
      h.addEventListener('mousedown', function (e) {
        if (e.button !== 0) return;
        e.preventDefault();
        act('resize', code);
      });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }
})();
