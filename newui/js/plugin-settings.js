/*!
 * plugin-settings.js — 设置页「LAB」栏内的本地插件管理（2026-11-19）
 *
 * 用户要求：扫描入口只放 LAB 栏，其它地方不加。
 *  - 插件列表整合进 LAB 栏（原 AI 大模型），风格与设置面板一致；
 *  - 音源插件启用后进搜索音源；
 *  - 不往「音源」tab 注入任何设置项、不放扫码按钮、不碰 /api/settings。
 *  - 侵权合规：插件名等字样一律来自 /api/plugins/all（已安装插件），静态前端不写死。
 *
 * 纯增量：本文件为新文件，挂到 index.html；不改 runtime.js 逻辑、不碰冻结核心。
 * © 2026 Mr. Unpunctual. All rights reserved.（代码骨架 + 算法）
 */
(function () {
  'use strict';

  var ROOT = null, LIST = null, STATUS = null, SCAN_BTN = null;
  var loaded = false;
  var NAME_BORDER = 'display:inline-block;padding:1px 7px;border-radius:7px;' +
    'border:1px solid var(--accent,#6a5cff);color:var(--accent,#6a5cff);font-weight:500';

  function esc(s) {
    return String(s == null ? '' : s).replace(/[<>&"]/g, function (c) {
      return { '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;' }[c];
    });
  }

  // ---- LAB 栏插件列表（唯一入口）----
  function render(listing) {
    if (!LIST) return;
    var src = (listing && listing.sources && listing.sources.plugins) || [];
    var art = (listing && listing.art && listing.art.plugins) || [];
    var rows = [];

    // 功能插件（音源）
    if (src.length) {
      rows.push('<div class="fx-subhead">功能插件</div>');
      src.forEach(function (p) {
        var conflict = !!p.builtin_conflict;
        var dis = conflict ? ' disabled title="内置音源 key，不可覆盖"' : '';
        // 扫码按钮紧贴开关左侧；仅启用且有扫码能力时显示。标准按钮样式，风格与其他设置按钮一致。
        var qrBtn = (p.enabled && p.qr && !conflict)
          ? '<button class="set-mini-btn fx-btn" type="button" data-qr="' + esc(p.id) +
            '" title="扫码登录" style="margin-right:10px">扫码登录</button>'
          : '';
        rows.push(
          '<div class="set-row" style="justify-content:space-between;align-items:center">' +
            '<span style="' + NAME_BORDER + '">' + esc(p.name) + '</span>' +
            '<span style="display:flex;align-items:center">' + qrBtn +
              '<label class="fx-toggle" style="margin-bottom:0">' +
                '<input type="checkbox" class="plug-src-toggle" data-key="' + esc(p.id) + '"' +
                  (p.enabled ? ' checked' : '') + dis + '><span class="tk"></span>' +
              '</label>' +
            '</span>' +
          '</div>'
        );
      });
    }

    // 美术插件（效果）
    if (art.length) {
      rows.push('<div class="fx-subhead" style="margin-top:14px">美术插件</div>');
      art.forEach(function (p) {
        rows.push(
          '<div class="set-row" style="justify-content:space-between;align-items:center">' +
            '<span style="' + NAME_BORDER + '">' + esc(p.name) + '</span>' +
          '</div>'
        );
      });
    }

    LIST.innerHTML = rows.length ? rows.join('') : '';
    Array.prototype.forEach.call(LIST.querySelectorAll('.plug-src-toggle'), function (cb) {
      cb.addEventListener('change', function () { toggleSource(cb); });
    });
    Array.prototype.forEach.call(LIST.querySelectorAll('button[data-qr]'), function (btn) {
      btn.addEventListener('click', function () { startQr(btn.getAttribute('data-qr')); });
    });
  }

  function startQr(id) {
    // 后端 source 约定 netease | qq（不是 qqmusic）
    var source = (id === 'qqmusic') ? 'qq' : id;
    var st = document.querySelector('[data-qr-status="' + id + '"]');
    // runtime.js 已有完整扫码弹层 + 轮询（__r54.startQrLogin），复用它保持风格统一
    if (window.__r54 && typeof window.__r54.startQrLogin === 'function') {
      window.__r54.startQrLogin(source);
      if (st) st.textContent = '已弹出二维码';
      return;
    }
    if (st) st.textContent = '获取二维码…';
    fetch('/api/settings/qr/start?source=' + encodeURIComponent(source), { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.ok) { if (st) st.textContent = '失败：' + (d.err || '未知'); return; }
        var modal = document.getElementById('qr-modal');
        var img = document.getElementById('qr-img');
        if (img && d.img) img.src = d.img;
        if (modal) modal.classList.add('open');
        if (st) st.textContent = '请用手机扫码';
        pollQr(source, d.key);   // 轮询扫码结果
      })
      .catch(function (e) { if (st) st.textContent = '失败：' + e.message; });
  }

  function pollQr(source, key) {
    var modal = document.getElementById('qr-modal');
    var status = document.getElementById('qr-status');
    var n = 0;
    var timer = setInterval(function () {
      if (!modal || !modal.classList.contains('open') || n++ > 60) { clearInterval(timer); return; }
      fetch('/api/settings/qr/poll?source=' + encodeURIComponent(source) + '&key=' + encodeURIComponent(key))
        .then(function (r) { return r.json(); })
        .then(function (d) {
          var s = (d && d.status) || '';
          if (status) status.textContent = d.msg || ('状态：' + s);
          if (s === 'ok') {
            clearInterval(timer);
            if (modal) modal.classList.remove('open');
            load();   // 登录成功 → 刷新插件状态
          } else if (s === 'expired' || s === 'error') {
            clearInterval(timer);
            if (status) status.textContent = '二维码已失效，请重新扫码';
          }
        })
        .catch(function () {});
    }, 1500);
  }

  function toggleSource(cb) {
    var key = cb.getAttribute('data-key'), want = cb.checked;
    if (STATUS) STATUS.textContent = '设置中…';
    fetch('/api/plugins/source/enable', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: key, enabled: want })
    }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        if (STATUS) STATUS.textContent = res.d.ok
          ? ('已' + (want ? '启用' : '停用') + '：' + key)
          : ('失败：' + (res.d.reason || res.d.error || '未知'));
        if (!res.d.ok) cb.checked = !want;
      })
      .catch(function (e) { if (STATUS) STATUS.textContent = '请求失败：' + e.message; cb.checked = !want; });
  }

  function load() {
    loaded = true;
    if (STATUS) STATUS.textContent = '扫描中…';
    fetch('/api/plugins/all', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        render(data);
        var n = (data.sources && data.sources.count || 0) + (data.art && data.art.count || 0);
        if (STATUS) STATUS.textContent = '已发现 ' + n + ' 个插件';
      })
      .catch(function (e) {
        if (LIST) LIST.innerHTML = '<div class="set-hint">插件清单加载失败：' + esc(e.message) + '</div>';
        if (STATUS) STATUS.textContent = '扫描失败';
      });
  }

  function autoOpenLab() {
    if (!/[?&]settings=lab/.test(location.search)) return;
    var attempt = 0;
    (function go() {
      var api = window.__r54;
      if (api && typeof api.openSettings === 'function') {
        api.openSettings();
        var tab = document.querySelector('#settings-tabs .stab[data-tab="ai"]');
        if (tab) tab.click();
        return;   // init() 已自动扫描，不重复 load
      }
      if (attempt++ < 40) setTimeout(go, 50);
    })();
  }

  function init() {
    ROOT = document.getElementById('local-plugins-group') ||
           document.querySelector('#settings-panel #local-plugins-group');
    if (!ROOT) return;
    LIST = ROOT.querySelector('#local-plugins-list');
    STATUS = ROOT.querySelector('#local-plugins-status');
    SCAN_BTN = ROOT.querySelector('#local-plugins-scan');
    if (SCAN_BTN) SCAN_BTN.addEventListener('click', load);
    // 每次打开软件即自动扫描一次（不依赖面板是否可见）；启用状态来自 config，带记忆。
    load();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
    document.addEventListener('DOMContentLoaded', autoOpenLab, { once: true });
  } else {
    init();
    autoOpenLab();
  }

  window.__pluginSettings = {
    load: load, render: render,
    probe: function () { return { loaded: loaded, hasRoot: !!ROOT, hasList: !!LIST }; }
  };
})();
