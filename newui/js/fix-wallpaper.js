/* fix-wallpaper.js — 换肤页「壁纸内容」记忆 + 歌曲封面自动切换（2026-11-18）
 *
 * 修两个用户反馈的问题（刷新页面即生效，无需重启应用）：
 *   1) 自定义壁纸（图片/视频）**不记忆**：运行时只在 applyWallpaperImage 里写
 *      localStorage r54.wpchoice，视频/封面没写、且重启后缺少统一恢复路径；
 *   2) 选「歌曲封面」后，**第一次播放不会自动切到封面**：render() 喂背景被
 *      "dock 封面图已更新"的条件挡住（ci.src 相同就跳过整块），且启动时无封面。
 *
 * 本脚本不依赖 runtime 内部变量，只做三件事：
 *   · 记忆：监听 #bgimg / #bgvideo 的 src 变化 → 写 r54.wpchoice（+ 后端 prefs.wallpaper）；
 *   · 恢复：页面加载后按 r54.wpchoice 重新应用上次的图片/视频壁纸；
 *   · 跟随：选择「歌曲封面」时，轮询 /api/playback/state 的 current.cover，
 *          首播/切歌都自动把 #bgimg 换成封面（不覆盖自定义/默认壁纸）。
 */
(function () {
  'use strict';
  var KEY = 'r54.wpchoice';
  var POLL_MS = 900;

  function getChoice() {
    try { return JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch (e) { return {}; }
  }
  function setChoice(patch) {
    try {
      var o = getChoice();
      Object.keys(patch).forEach(function (k) { o[k] = patch[k]; });
      localStorage.setItem(KEY, JSON.stringify(o));
    } catch (e) { /* quota 忽略 */ }
  }
  function prefs() {
    try { return JSON.parse(localStorage.getItem('r54.ui') || '{}') || {}; } catch (e) { return {}; }
  }
  function postPrefs(patch) {
    try {
      fetch('/api/ui/prefs', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch)
      }).catch(function () {});
    } catch (e) { /* 忽略 */ }
  }
  function $(id) { return document.getElementById(id); }

  /* 当前"背景内容"意向：显式 tab 选择 > 后端/本地偏好 > 默认跟随封面 */
  function mode() {
    var c = getChoice();
    if (c.type === 'cover' || c.type === 'default' || c.type === 'image' ||
        c.type === 'link' || c.type === 'video' || c.type === 'wallpaper') return c.type;
    var p = prefs().wallpaper;
    if (p === 'cover' || p === 'default' || p === 'custom') return p === 'custom' ? 'image' : p;
    return 'cover';
  }

  function markCustom(on) {
    try {
      document.body.classList.toggle('wp-active', !!on);
      var bi = $('bgimg');
      if (bi) bi.classList.toggle('wp-active', !!on);
    } catch (e) {}
  }

  function applyImage(url, persist) {
    if (!url) return;
    try {
      var bi = $('bgimg');
      if (!bi) return;
      /* 先停视频壁纸，避免两层同显 */
      var v = $('bgvideo');
      if (v && v.src) { try { v.pause(); } catch (_) {} v.removeAttribute('src'); v.classList.remove('has-bg', 'wp-active'); }
      bi.setAttribute('src', url);
      bi.classList.add('has-bg');
      markCustom(true);
      try { if (window.Poster) window.Poster.setCover(url); } catch (_) {}
      try { if (window.LiquidBg) window.LiquidBg.setSource(url); } catch (_) {}
      if (persist !== false) {
        setChoice({ type: 'image', url: url });
        postPrefs({ wallpaper: 'custom' });
      }
    } catch (e) {}
  }

  function applyVideo(url, persist) {
    if (!url) return;
    try {
      var v = $('bgvideo');
      if (!v) return;
      v.src = url;
      v.classList.add('has-bg', 'wp-active');
      v.muted = true; v.loop = true; v.autoplay = true; v.playsInline = true;
      try { v.play() && v.play().catch && v.play().catch(function () {}); } catch (_) {}
      markCustom(true);
      var bi = $('bgimg');
      if (bi) { bi.classList.remove('wp-active', 'has-bg'); bi.removeAttribute('src'); }
      if (persist !== false) {
        setChoice({ type: 'video', videoUrl: url });
        postPrefs({ wallpaper: 'custom' });
      }
    } catch (e) {}
  }

  /* 跟随封面：把 bgimg 换成当前曲封面（只在 cover 模式下调用） */
  function showCover(cover) {
    if (!cover) return;
    try {
      var bi = $('bgimg');
      if (!bi) return;
      var v = $('bgvideo');
      if (v && v.src) { try { v.pause(); } catch (_) {} v.removeAttribute('src'); v.classList.remove('has-bg', 'wp-active'); }
      if (bi.getAttribute('src') !== cover) bi.setAttribute('src', cover);
      bi.classList.add('has-bg');
      bi.classList.remove('wp-active');
      markCustom(false);
      try { if (window.Poster) window.Poster.setCover(cover); } catch (_) {}
      try { if (window.LiquidBg) window.LiquidBg.setSource(cover); } catch (_) {}
    } catch (e) {}
  }

  /* ── 记忆：监听 src 变化（用户导入图片/视频时写入）── */
  function observe() {
    var bi = $('bgimg'), v = $('bgvideo');
    try {
      if (bi) {
        new MutationObserver(function () {
          var src = bi.getAttribute('src') || '';
          if (!src || src.indexOf('/brand/logo') === 0) return;
          if (!document.body.classList.contains('wp-active')) return;   /* 仅自定义壁纸态 */
          setChoice({ type: 'image', url: src });
          postPrefs({ wallpaper: 'custom' });
        }).observe(bi, { attributes: true, attributeFilter: ['src'] });
      }
      if (v) {
        new MutationObserver(function () {
          var src = v.getAttribute('src') || '';
          if (!src) return;
          setChoice({ type: 'video', videoUrl: src });
          postPrefs({ wallpaper: 'custom' });
        }).observe(v, { attributes: true, attributeFilter: ['src'] });
      }
    } catch (e) {}
  }

  /* ── 恢复 + 跟随（轮询；先恢复一次，再持续跟随封面）── */
  function tick() {
    var c = getChoice();
    var m = mode();
    /* 1) 自定义图片/链接：恢复或重新落到 bgimg */
    if (m === 'image' || m === 'link') {
      var url = c.url || '';
      if (url) {
        var bi = $('bgimg');
        if (bi && (bi.getAttribute('src') !== url || !document.body.classList.contains('wp-active'))) {
          applyImage(url, false);
        }
      }
      return;
    }
    /* 2) 自定义视频 */
    if (m === 'video') {
      if (c.videoUrl) {
        var v = $('bgvideo');
        if (v && v.getAttribute('src') !== c.videoUrl) applyVideo(c.videoUrl, false);
      }
      return;
    }
    /* 3) 默认 logo：交给运行时（applyBrandSkin），这里不抢 */
    if (m === 'default') return;
    /* 4) 跟随封面：需要当前曲封面（首播时也能补上） */
    if (m === 'cover') {
      fetch('/api/playback/state', { cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          var cur = d && d.current;
          var cover = cur && cur.cover;
          if (!cover) return;
          var bi = $('bgimg');
          if (bi && bi.getAttribute('src') !== cover && !document.body.classList.contains('wp-active')) {
            showCover(cover);
          }
        })
        .catch(function () {});
    }
  }

  function boot() {
    if (!document.body) return;
    observe();
    /* 首次恢复（等运行时初始化 ~1.2s，避免被它的默认皮肤覆盖） */
    setTimeout(tick, 1200);
    setInterval(tick, POLL_MS);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }
})();
