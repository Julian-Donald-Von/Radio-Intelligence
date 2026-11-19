/* newui/js/modules/init.js — 由老 ui-init.js 拆出的独立模块（自动生成，勿手改）。
 * factory(ctx) 用 new Function 承载老代码，保持 classic script 的顶层声明语义；
 * 本模块可独立 register/unregister，替换时只需提供等价 factory。
 */
export default {
  name: 'init',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `/* ══════════════════════════════════════════════════════════
   ui-init.js — Radio Intelligence 前端模块 · initSearchR54/initR54/__r54 · 启动 bootstrap/__r53/mainLoop
   自 webapi/static/index.html 内嵌 <script> L3905-L4158 拆出（R58-7 #46）
   与原 index.html 内所有 ui-*.js 共享顶层作用域 → 加载顺序即执行顺序，勿打乱
   ══════════════════════════════════════════════════════════ */
  function initSearchR54(){
    const box = $('search-box'), input = $('search-input'),
          clear = $('search-clear'), suggest = $('search-suggest');
    if (!box || !input || !suggest) return;
    let cur = -1, items = [], searchTimer = 0;

    /* ═══ R86 · 切换搜索源开关（搜索框内右侧）═══
       交互 = 点击循环切换可用音源（2 个时即等价于 on/off 开关），选择持久化到
       ui_prefs.searchSource；样式见 index.html 的 .ss-src（透明轨道 + 渐变发光滑块）。 */
    const srcWrap = $('ss-src'), srcLb = $('ss-src-lb'), srcCb = $('ss-src-cb');
    let srcList = [], srcIdx = 0, srcKey = '';

    function renderSrc(){
      if (!srcWrap) return;
      if (!srcList.length){ srcWrap.style.display = 'none'; return; }
      srcWrap.style.display = '';
      const curSrc = srcList[srcIdx] || srcList[0];
      srcKey = curSrc.key || '';
      /* 「可切换」= 除「自动」外还有 ≥2 个真实音源；否则压暗禁用（避免点了没反应） */
      const realCount = srcList.filter(s => !!s.key).length;
      const switchable = realCount >= 2;
      if (srcLb) srcLb.textContent = curSrc.short || curSrc.key || '自动';
      if (srcCb) srcCb.checked = srcIdx > 0;
      srcWrap.classList.toggle('solo', !switchable);
      srcWrap.classList.toggle('on', srcIdx > 0);
      srcWrap.title = '切换搜索源：' + srcList.map(s => s.label || s.key || '自动').join(' / ')
                    + (switchable ? '' : '（仅一个可用音源）');
    }
    function loadSearchSources(){
      fetch('/api/search/sources').then(r => r.json()).then(d => {
        const all = (d && Array.isArray(d.sources)) ? d.sources : [];
        /* ⚠ R86-fix：只保留「已启用」音源 —— 后端旧进程可能仍把模板插件
           （key=example，短名兜底显示成「EXAM」）也列进来；此处兜底过滤，
           改完刷新页面即生效，无需重启后端。 */
        srcList = all.filter(s => s && s.enabled !== false && s.searchable !== false);
        const want = (d && d.current) || '';
        const i = srcList.findIndex(s => s.key === want);
        srcIdx = i >= 0 ? i : 0;
        renderSrc();
      }).catch(() => { if (srcWrap) srcWrap.style.display = 'none'; });
    }
    if (srcCb) srcCb.addEventListener('change', () => {
      if (srcList.filter(s => !!s.key).length < 2){ renderSrc(); return; }
      srcIdx = (srcIdx + 1) % srcList.length;
      renderSrc();
      fetch('/api/ui/prefs', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ searchSource: srcKey }),
      }).catch(() => {});
      toast('搜索源：' + (srcList[srcIdx].label || srcKey || '自动'));
      /* 切源即重搜当前关键词（无关键词则等下次输入） */
      const kw = input.value.trim().toLowerCase();
      if (kw){ clearTimeout(searchTimer); backendSearch(kw); }
    });
    loadSearchSources();

    function escS(v){ return esc(v); }

    function render(){
      const kw = input.value.trim().toLowerCase();
      box.classList.toggle('has-text', input.value.length > 0);
      if (!kw){
        suggest.innerHTML = ''; box.classList.remove('show-suggest');
        items = []; cur = -1; return;
      }
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => backendSearch(kw), 300);
    }
    function backendSearch(kw){
      /* ⚠ R86：带上当前搜索源（srcKey 为空 = 后端自动竞速，谁先返回用谁）。
         切换音源后必须重搜，否则用户切了源却仍看到别家的结果。 */
      const u = '/api/search?q=' + encodeURIComponent(kw)
              + (srcKey ? '&source=' + encodeURIComponent(srcKey) : '');
      fetch(u).then(r => r.json()).then(d => {
        /* 过期结果丢弃 */
        if (input.value.trim().toLowerCase() !== kw) return;
        items = (d && Array.isArray(d.entries)) ? d.entries : [];
        if (!items.length){
          suggest.innerHTML = '<div class="ss-empty">未找到相关歌曲（可换关键词）</div>';
          box.classList.add('show-suggest'); cur = -1; return;
        }
        suggest.innerHTML = items.map((s, i) =>
          \`<div class="ss-item\${i===cur?' cur':''}" data-i="\${i}">
             <span class="ss-name">\${escS(s.title || '')}</span>
             <span class="ss-artist">\${escS((s.artist||'') + (s.source ? ' · ' + s.source : ''))}</span>
           </div>\`).join('');
        box.classList.add('show-suggest');
      }).catch(() => {
        suggest.innerHTML = '<div class="ss-empty">搜索失败（后端未连接？）</div>';
        box.classList.add('show-suggest');
      });
    }
    function playItem(s){
      if (!s) return;
      const src = s.source || '', ext = s.ext_id || '';
      const id = Number(s.id);
      /* ⚠ 在线曲无本地 db id → 走 /api/search/play（source+ext_id）；
         本地曲 → /api/play/track/{id}。绝不用队列下标。 */
      let url = null;
      if (src && ext){
        const q = new URLSearchParams();
        q.set('source', src); q.set('ext_id', ext);
        if (s.title) q.set('title', s.title);
        if (s.artist) q.set('artist', s.artist);
        if (s.album) q.set('album', s.album);
        if (s.cover) q.set('cover', s.cover);
        /* ⚠ R86：duration 必须透传 —— 缺它时 seed 曲目时长=0，
           后端 state().duration=0 → 进度条永远不动（且转场口播被跳过）。 */
        if (s.duration > 0) q.set('duration', String(s.duration));
        url = '/api/search/play?' + q.toString();
      } else if (Number.isFinite(id) && id > 0){
        url = '/api/play/track/' + id;
      }
      if (url) fetch(url, { method:'POST' }).then(r => r.json()).catch(() => {});
      toast('♪ ' + (s.title || '') + (s.artist ? ' — ' + s.artist : ''));
      box.classList.remove('show-suggest','has-text');
      input.value = ''; items = []; cur = -1; input.blur();
    }
    function pick(i){
      if (i >= 0 && i < items.length) playItem(items[i]);
    }
    input.addEventListener('input', () => { cur = -1; render(); });
    input.addEventListener('keydown', ev => {
      ev.stopPropagation();          // 阻止冒泡到全局卡片方向键
      if (ev.key === 'ArrowDown'){ ev.preventDefault(); cur = Math.min(cur + 1, items.length - 1); if (box.classList.contains('show-suggest')) highlight(); }
      else if (ev.key === 'ArrowUp'){ ev.preventDefault(); cur = Math.max(cur - 1, -1); if (box.classList.contains('show-suggest')) highlight(); }
      else if (ev.key === 'Enter'){ ev.preventDefault(); if (cur >= 0) pick(cur); }
      else if (ev.key === 'Escape'){ suggest.innerHTML=''; box.classList.remove('show-suggest'); input.blur(); }
    });
    function highlight(){
      suggest.querySelectorAll('.ss-item').forEach((el, i) =>
        el.classList.toggle('cur', i === cur));
      const el = suggest.querySelector('.ss-item.cur');
      if (el && el.scrollIntoView) el.scrollIntoView({ block:'nearest' });
    }
    if (clear) clear.addEventListener('click', () => {
      input.value = ''; render(); input.focus();
    });
    /* 建议点选（mousedown 避免 input blur 抢先） */
    suggest.addEventListener('mousedown', ev => {
      const item = ev.target.closest('.ss-item'); if (!item) return;
      ev.preventDefault();
      const idx = +item.getAttribute('data-i');
      if (Number.isFinite(idx)) pick(idx);
    });
    /* ═══ R56：鼠标靠近显现 / 远离消失 —— 触发范围比搜索栏本体大一圈 ═══
       老逻辑只在顶部 130px 竖向热区 + 中心窄带内才显示；
       R56 改为**包围盒外扩**：以搜索栏为中心、横向/纵向都放大约 1.6 倍矩形
       （含其正上方到顶部的空档），指针一靠近就显现，离开才消失。
       移入搜索栏本体 / 输入聚焦时**绝不消失**（保证可搜索）。 */
    /* ═══ R62：输入中恒显 —— 鼠标远离/移出窗口也不能藏 ═══
       用户原话：「搜索栏在打字的时候不能隐藏，要一直显现。哪怕鼠标不在触发
       范围，在打字搜索的时候也要显现。」
       keepVisible = 聚焦 或 有非空输入内容。满足任一条件时 mousemove /
       mouseleave / blur 一律不得摘 .visible（blur 只在失焦且内容为空时允许藏）。 */
    function searchKeepVisible(){
      return document.activeElement === input
          || !!(input.value && input.value.trim().length > 0)
          || box.classList.contains('show-suggest');
    }
    document.addEventListener('mousemove', ev => {
      if (box.classList.contains('ui-hidden')) return;
      const r = box.getBoundingClientRect();
      /* 触发区 = 包围盒外扩（比栏大一圈）：宽 ×1.5、高含顶部空档 60px */
      const padX = Math.max(70, r.width * 0.3);
      const padY = Math.max(46, 40 + r.height * 0.4);
      const nearX = ev.clientX >= r.left - padX && ev.clientX <= r.right + padX;
      const nearY = ev.clientY >= Math.max(0, r.top - padY) && ev.clientY <= r.bottom + 26;
      const near = (nearX && nearY) || box.contains(ev.target);
      const vis = box.classList.contains('visible');
      if (near && !vis) box.classList.add('visible');
      else if (!near && vis && !box.contains(ev.target)
               && !searchKeepVisible()) box.classList.remove('visible');
    });
    /* ⚠ R62：鼠标整体移出窗口时，若仍在输入（聚焦/有内容/建议开着）不得隐藏 */
    document.addEventListener('mouseleave', () => {
      if (!searchKeepVisible()) box.classList.remove('visible');
    });
    input.addEventListener('focus', () => box.classList.add('visible'));
    input.addEventListener('input', () => box.classList.add('visible'));
    input.addEventListener('blur', () => {
      requestAnimationFrame(() => {
        /* 焦点在输入框/建议列表内 → 保持可见 */
        if (box.contains(document.activeElement) || box.contains(document.querySelector(':focus'))) return;
        /* ⚠ R62：内容非空（用户打完字点外面看建议/刚要复制）也保持可见；
           只有「失焦 + 内容已清空」才收起。真正清空动作由 clear 按钮/
           playItem 完成（它们摘 .visible 前会先清 input.value）。 */
        if (searchKeepVisible()) return;
        box.classList.remove('visible');
      });
    });
    /* 点外部收起建议 */
    document.addEventListener('click', ev => {
      if (!box.contains(ev.target)) box.classList.remove('show-suggest');
    });
  }

  /* ★★ R110（2026-09-13）一次性偏好迁移：语义强度档默认 **0.6 → 1.0**（配 ① 加权聚合）。
     为什么必须做：本文件的恢复逻辑是「**存档里有的就覆盖**」
     ⇒ 用户上一次存的 \`semStrength:0.6\` 会**静默盖掉新默认**。
     实测踩到：只改 \`ui-water.js\` 的 \`let semStrength = 1.0\` 时，真机端到端
     （\`verify_r95b\` C5）**纹丝不动（仍是 2.06×）** —— 因为 \`ui-skinset.js\` 在 init 时
     用 \`setSemStrength(0.6)\` 又推了回去；\`resetOpts()\` 里还有第三个 0.6。
     ⇒ 用 \`semOptV\` 标记**只强制刷一次**；之后用户手动调的值照常被尊重。
     ⚠ **两条恢复路径（localStorage / 后端 prefs）都要调** —— 只改一条会漏另一半。 */
  const SEM_OPT_V = 1;
  function migrateSemOpts(src){
    if (Number((src && src.semOptV) || 0) >= SEM_OPT_V) return;
    uiWaterOpts.semStrength = 1.0;          // 新默认（三处定义须同步）
    uiWaterOpts.semOptV = SEM_OPT_V;
    try { persistPrefs({ waterOpts: uiWaterOpts }, true); } catch (_) {}
  }

  /* ---- 初始化：加载偏好 → 应用 + 绑事件 ---- */
  (function initR54(){
    /* 先绑事件（本地兜底可用），再异步拉后端偏好 */
    bindSettingsEvents();
    bindCornerButtons();
    initSearchR54();
    /* 本地镜像先应用（offline/演示也有主题可用） */
    try {
      const f = JSON.parse(localStorage.getItem('r54.ui') || '{}');
      if (f.theme && THEMES.find(t => t.key === f.theme)) uiThemeKey = f.theme;
      if (typeof f.accent === 'string') uiAccent = f.accent;
      /* 旧值 flow → 归并到 poster（R56 去掉流动水样） */
      if (f.bgmode === 'acrylic') uiBgMode = 'acrylic';
      if (typeof f.beat === 'boolean') uiBeat = f.beat;
      if (typeof f.wallpaper === 'string') uiWallpaper = f.wallpaper;  // R78
      /* R93：叠层效果（ontop 主键 / water 旧键）+ 水面参数组 */
      if (f.ontop === 'none' || f.ontop === 'water') uiOnTop = f.ontop;
      else if (typeof f.water === 'boolean') uiOnTop = f.water ? 'water' : 'none';
      if (f.waterOpts && typeof f.waterOpts === 'object'){
        for (const k in f.waterOpts){ if (k in uiWaterOpts) uiWaterOpts[k] = f.waterOpts[k]; }
      }
      migrateSemOpts(f.waterOpts);
    } catch (_) {}
    /* ═══ R78：首帧即按本地 wallpaper 偏好定背景内容 ═══
       必须在第一次 render() 之前执行 —— 用户上次选「默认」logo 壁纸时,
       首帧就挂 wp-active + logo;否则 render() 按默认 cover 语义处理,
       随后 2s 轮询带回 st.cover 会先把封面铺上,再等 fetch 回填才纠正 →
       闪一下封面(用户报"皮肤默认,切歌后被封面取代"的另一半根因)。 */
    try { if (uiWallpaper === 'default') applyWallpaperPref(); } catch (_) {}
    /* 音频律动 body 初值（beatTick 据此门控封面呼吸） */
    document.body.classList.toggle('beat-on', uiBeat);
    /* 后端偏好为准（皮肤/高亮/背景） */
    fetch('/api/ui/prefs').then(r => r.json()).then(d => {
      const p = (d && d.ok && d.prefs) ? d.prefs : null;
      if (!p) return;
      if (p.theme && THEMES.find(t => t.key === p.theme)) uiThemeKey = p.theme;
      if (typeof p.accent === 'string') uiAccent = p.accent;
      if (p.bgmode === 'acrylic') uiBgMode = 'acrylic';
      if (typeof p.beat === 'boolean') uiBeat = p.beat;
      if (typeof p.wallpaper === 'string') uiWallpaper = p.wallpaper;   // R78
      /* ═══ R93：叠层效果 + 水面参数组 ═══
         ontop 为主键（'none'|'water'），water 为旧键回退 —— 与 ui-water.js 的
         _prefOn 同一优先级，保证卡片显示与实际启停同源。 */
      if (p.ontop === 'none' || p.ontop === 'water') uiOnTop = p.ontop;
      else if (typeof p.water === 'boolean') uiOnTop = p.water ? 'water' : 'none';
      if (p.waterOpts && typeof p.waterOpts === 'object'){
        for (const k in p.waterOpts){ if (k in uiWaterOpts) uiWaterOpts[k] = p.waterOpts[k]; }
      }
      migrateSemOpts(p.waterOpts);
      applyPrefsTheme();
      syncBgModeUI();
      syncBeatUI();
      syncOnTopUI();
      applyWaterOpts();     // ui-init 在 ui-water 之后加载 → window.__water 已就绪
      /* ⚠⚠ R57-1：成功分支也必须挂 body 类 —— 否则后端 prefs 是 acrylic 时
         亚克力层 #bgimg 永远 opacity:0（此前只在 .catch/初值挂，静默失效，
         实测 uiBgMode=acrylic 而 body.className 为空）。 */
      document.body.classList.toggle('bgmode-acrylic', uiBgMode === 'acrylic');
      document.body.classList.toggle('beat-on', uiBeat);
      /* ═══ R78：恢复背景内容偏好（默认 logo 壁纸 / 跟随封面）═══
         必须在 render() 之前做 —— 若用户上次选了「默认」壁纸，首帧就挂
         wp-active + logo，避免被随后到达的 st.cover / 轮询 render 覆盖成封面。 */
      applyWallpaperPref();
      try { if (window.Poster && window.Poster.setMode) window.Poster.setMode(uiBgMode); } catch (_) {}
      try { localStorage.setItem('r54.ui', JSON.stringify({ theme:uiThemeKey, accent:uiAccent, bgmode:uiBgMode, beat:uiBeat, wallpaper:uiWallpaper, ontop:uiOnTop, waterOpts:uiWaterOpts })); } catch (_) {}
    }).catch(() => {
      /* 后端不可用：本地镜像直接生效 */
      applyPrefsTheme();
      document.body.classList.toggle('bgmode-acrylic', uiBgMode === 'acrylic');
      document.body.classList.toggle('beat-on', uiBeat);
      applyWallpaperPref();
      syncOnTopUI();       // R93：本地镜像下的卡片点亮态 + 参数区展开态
      applyWaterOpts();
    });
    /* 背景模式 body class 初值（CSS 驱动 #bgimg 显隐） */
    document.body.classList.toggle('bgmode-acrylic', uiBgMode === 'acrylic');
    try { if (window.Poster && window.Poster.setMode) window.Poster.setMode(uiBgMode); } catch (_) {}
  })();

  /* R54/R56 验证钩子（offscreen 黑盒断言用）：
     theme()/accent()/bgmode()/setTheme(key)/setAccent(c)/setBgMode(m)
     openSettings()/closeSettings()/openTheme()/closeTheme()/isSettingsOpen()/isThemeOpen()
     searchInput(v)/suggestCount()/playFirst()/lastPlayReq()/skinCount()
     importWallpaper(type, src) / drawerOpen() */
  window.__r54 = {
    theme:    function(){ return uiThemeKey; },
    accent:   function(){ return uiAccent; },
    bgmode:   function(){ return uiBgMode; },
    locked:   function(){ return accentLocked; },
    /* ⚠ R94：换肤/换高亮是**离散终态**动作 → immediate=true 立刻落盘
       （跳过去抖，避免"点完 300ms 内 F5"丢掉这次改动；也保证黑盒面能看到 POST）。
       唯一保留去抖的是颜色取色器的 \`input\`（拖动中连发），它另有 change 收尾。 */
    setTheme: function(k){ uiThemeKey = k; uiAccent=''; persistPrefs({ theme:k, accent:'' }, true); applyPrefsTheme(); return true; },
    setAccent:function(c){ uiAccent = c || ''; persistPrefs({ accent:uiAccent, theme:uiThemeKey }, true); applyPrefsTheme(); return true; },
    setBgMode:function(m){ setBgMode(m); return true; },
    beatOn:   function(){ return uiBeat; },
    setBeat:  function(on){ setBeat(!!on); return uiBeat; },
    /* ═══ R93 黑盒面：叠层效果（on-top effect）═══ */
    ontop:       function(){ return uiOnTop; },
    setOnTop:    function(v){ setOnTop(v); return uiOnTop; },
    waterOpts:   function(){ return JSON.parse(JSON.stringify(uiWaterOpts)); },
    setWaterOpts:function(p){ waterOptsPatch(p || {}); return JSON.parse(JSON.stringify(uiWaterOpts)); },
    waterEnabled:function(){
      try { return !!(window.__water && window.__water.enabled && window.__water.enabled()); }
      catch (_) { return null; }
    },
    spectrum: function(a){ if (Array.isArray(a) && a.length >= 8){ window.__spectrum = a; window.__specAt = performance.now(); } return true; },
    /* ⚠ offscreen rAF 零帧 + setInterval 节流到 ~1s：beatTick/eqTick 可能
       长时间不触发 → 手动排一帧。eqTick 内部会带 beatPulse，测封面也用它。
       rAF/interval 正常时此调用幂等（beatPulse/EQ 写均 memo 化不抖动）。 */
    tickBeat: function(){ try { return window.beatPulse ? window.beatPulse() : 0; } catch (_) { return 0; } },
    tickEq:   function(){ try { if (window.eqTick){ window.eqTick(); return true; } return false; } catch (_) { return false; } },
    openSettings:  function(){ openSettings(); return $('settings-panel').classList.contains('open'); },
    closeSettings: function(){ closeSettings(); return !$('settings-panel').classList.contains('open'); },
    isSettingsOpen:function(){ return !!( $('settings-panel') && $('settings-panel').classList.contains('open') ); },
    openTheme:  function(){ openTheme(); return $('theme-panel').classList.contains('open'); },
    closeTheme: function(){ closeTheme(); return !$('theme-panel').classList.contains('open'); },
    isThemeOpen:function(){ return !!( $('theme-panel') && $('theme-panel').classList.contains('open') ); },
    drawerOpen: function(){ return document.body.classList.contains('drawer-open'); },
    importWallpaper: function(type, src){ importWallpaperSrc(type || 'image', src || ''); return true; },
    wpActive: function(){ return isCustomWallpaper(); },
    /* ═══ R78 黑盒面：默认 logo 壁纸 / 封面背景偏好（供 offscreen 断言）═══ */
    setDefaultSkin: function(){ try { applyBrandSkin(); return true; } catch (e) { return String(e); } },
    setCoverSkin:   function(){ try { useCoverSkin(); return true; } catch (e) { return String(e); } },
    wallpaperPref:  function(){ return typeof uiWallpaper === 'string' ? uiWallpaper : null; },
    bgWallpaperState: function(){
      const b = $('bgimg'); if (!b) return null;
      return { src: b.getAttribute('src'), hasBg: b.classList.contains('has-bg'),
               wp: b.classList.contains('wp-active'), bodyWp: document.body.classList.contains('wp-active') };
    },
    /* 模拟切歌：喂 st.cover 并跑 render() 的封面更新段（不依赖后端） */
    simulateCoverChange: function(url){
      st.cover = url || '/covers/fake.jpg';
      try { render(); } catch (e) { return String(e); }
      const b = $('bgimg');
      return b ? (b.getAttribute('src') || '') : '';
    },
    /* R66：原「我的壁纸」网格黑盒面（wallpapers / reloadWallpapers）随功能
       一并移除 —— 网格 DOM 与 loadMyWallpapers 都已不存在，留着会 ReferenceError。 */
    /* R61：切回「歌曲封面」背景（摘 wp-active，重新喂 st.cover） */
    useCoverSkin: function(){ useCoverSkin(); return true; },
    bgSrc: function(){ const b = $('bgimg'); return b ? (b.getAttribute('src') || '') : ''; },
    /* R57-1/2 黑盒面：壁纸层 src/class、acrylic body 态、画布透明度 */
    bgWallpaper: function(){ const b = $('bgimg'); return b ? { src: b.getAttribute('src'), hasBg: b.classList.contains('has-bg'), wp: b.classList.contains('wp-active') } : null; },
    bgWallpaperVisible: function(){
      const b = $('bgimg'); if (!b) return false;
      const cs = getComputedStyle(b);
      if ((parseFloat(cs.opacity) || 0) > 0.05) return true;
      /* offscreen/合成器暂停时 opacity .8s transition 停在起点 0 —— 按规则目标判定：
         has-bg（无 liquid-canvas 覆盖）时 CSS 目标 opacity=.9，视为可见 */
      return b.classList.contains('has-bg') && !b.classList.contains('liquid-canvas');
    },
    acrylicOn: function(){ return document.body.classList.contains('bgmode-acrylic'); },
    posterCanvasOpacity: function(){ const p = $('poster-canvas'); return p ? getComputedStyle(p).opacity : null; },
    cornerDim: function(id){ const el = $(id); return el ? el.classList.contains('dim') : null; },
    /* R59 黑盒面：默认皮肤 / acrylic 滤镜 / 口播栏常驻
       ⚠ R67-5：默认皮肤从程序化 dataURL 改为 /brand/logo.png（玻璃 logo）。
       R78：识别 logo 数据源（兼容旧 data:image/png 判定已无意义）。 */
    bgHasDefaultSkin: function(){
      const b = $('bgimg'); if (!b) return false;
      const s = b.getAttribute('src') || '';
      return !!s && (s.indexOf('data:image/png') === 0
                     || s === '/brand/logo.png');
    },
    bgFilter: function(){
      const b = $('bgimg'); if (!b) return '';
      try { return (getComputedStyle(b).filter || ''); } catch (_) { return ''; }
    },
    aibarShow: function(){
      const b = $('aibar'); if (!b) return false;
      return b.classList.contains('show') && !b.classList.contains('hide');
    },
    aiText: function(){
      const t = $('ai-text'); return t ? String(t.textContent || '') : '';
    },
    skinCount:function(){ return THEMES.length; },
    themeOf:  function(k){ const t = THEMES.find(x=>x.key===k); return t ? t.name : ''; },
    /* 搜索 */
    searchFocus: function(){ const i = $('search-input'); if (i){ i.focus(); return true; } return false; },
    searchType:  function(v){ const i = $('search-input'); if (!i) return false; i.value = v; i.dispatchEvent(new Event('input', { bubbles:true })); return true; },
    suggestItems:function(){ return Array.prototype.map.call(
      document.querySelectorAll('#search-suggest .ss-item'),
      el => el.textContent || ''); },
    searchVisible:function(){ return $('search-box').classList.contains('visible'); },
    clickSug: function(i){
      const el = document.querySelectorAll('#search-suggest .ss-item')[i];
      if (!el) return false;
      el.dispatchEvent(new MouseEvent('mousedown', { bubbles:true, cancelable:true }));
      return true;
    },
  };

  computeFramePath();
  window.addEventListener('resize', () => { computeFramePath(); updateFrameProgress(); });
  poll();
  loadView();                        // Phase 3/4：首次拉当前栏目
  /* ⚠ 2026-11-17 纠正：这里是**唯一**的显式调用已不可靠 —— loadView() 是异步 fetch，
     此刻 cardItems 仍为空/上一栏目，副标题会显示错数字（用户实测"曲库 84 首显示 3 首"）。
     真实数字由 buildCards() 末尾的 syncColumnHead() 在数据落地后刷新；
     这里保留一次只为让 kicker/title 立即可见（不等网络）。 */
  syncColumnHead();
  /* R49-30：初始显隐 —— 未播放时空闲信息层恒显；播放中（恢复会话）隐藏 */
  try { syncColumnByPlaying(!!st.playing); } catch (_) {}
  /* R49-31：漫游滑块初始显隐（默认栏目 wander 时显示） */
  try { syncRecMode(); } catch (_) {}
  loadBroadcast();                   // AI 口播
  setInterval(poll, 2000);
  setInterval(loadCards, 5000);      // 推荐变动兜底刷新（信号丢失自愈）
  setInterval(loadBroadcast, 3000);  // 口播文案轮询
  render();
  /* R59：首帧即补默认皮肤 —— 不等 2s 轮询的 render() 触发（render 内部有
     st.cover 门控，但此处显式调一次保证无封面开屏不是纯黑底）。 */
  try { ensureDefaultSkin(); } catch (_) {}
  /* ═══ R59：AI 伴听开启 → 口播栏启动即常驻 ═══
     st.djOn 初值 true（与后端默认一致）。等待首条 broadcast 文案
     （首帧 loadBroadcast + 3s 轮询）到达自动显示；若后端已关伴听，
     下一次轮询 applyState 会 hideAIBar 收走。 */

  /* R64：个性化设置折叠委托（高级选项 .set-fold-head + 动态分组 h4）+ 扫码弹层关闭。
     委托挂在 document 上，动态重渲染无需重绑。 */
  try { if (typeof initSettingsFolds === 'function') initSettingsFolds(); } catch (_) {}
  /* R67-2：4 tab 切换（AI / TTS / 音源 / 通用），点切一次只看一组。tab 状态不持久化。 */
  try { if (typeof bindSettingsTabs === 'function') bindSettingsTabs(); } catch (_) {}

  /* R53 验证钩子（offscreen 黑盒断言用，最小只读面）：
     wsText()        = 模拟 WS dj_speech 推送（viaWs=true 记 lastDjAt）
     pollBroadcast() = 手动触发一次轮询回填（验证 outro 门控）
     lastDjAt()      = 读上次 WS 推送时间戳
     ⚠ R58-1 (#41) 增补：
     wsText 应用与 WS case 同款 st.djOn 门控（关闭时不得弹口播栏）；
     djSet(on) = 从后端权威 applyState 喂 dj_on（黑盒驱动开/关路径）；
     djGet()   = 读开关镜像（断言随动）。 */
  window.__r53 = {
    lastDjAt: function(){ return lastDjAt; },
    wsText:   function(t){ if (!st.djOn) return false; return setAIBarText(String(t), true); },
    pollBroadcast: function(){ return loadBroadcast(); },
    djSet:    function(on){ applyState({ dj_on: !!on }); return st.djOn; },
    djGet:    function(){ return st.djOn; },
    /* ★ R119-7 增补（offscreen 黑盒用）：
       newTrack() = 等价一次 track_changed（解锁 WS 钉住 + 亮加载圈）
       holdOn()   = 读「WS 文案钉住」状态（验证轮询是否被正确门控）
       thinkOn()  = 读圈圈是否点亮（验证"加载中"可见性） */
    newTrack: function(){ clearDjHold(); setAIWaiting(true); return true; },
    holdOn:   function(){ return djHold; },
    thinkOn:  function(){ var e = document.getElementById('ai-bar-think');
                          return !!(e && e.classList.contains('on')); }
  };

  /* ═══════════════════════════════════════════════════════════════
     主循环（rAF）—— 原 UI 把 updateFrameProgress() 放在 mainLoop 里**每帧**跑。
     ⚠ 之前缺失这一环，进度条只在 2s 轮询的 progress 事件里更新，
     表现为：进度条两秒跳一格、播放头呼吸动画完全不动
     （sin(performance.now()) 需要逐帧重写 opacity 才看得出呼吸）。
     这里补上：播放中本地插值推进 position，后端 progress 事件负责校正。
     ═══════════════════════════════════════════════════════════════ */
  let _lastTick = performance.now();
  (function mainLoop(now){
    const dt = Math.min(250, now - _lastTick);   // 卡顿/切后台时钳位，避免进度暴冲
    _lastTick = now;
    /* ★ 2026-09-18：加 \`st.posLive\` 闸门 —— 只有后端报过真实播放位置(>0)才本地插值。
       否则"起播装载期"（is_playing_intent 已置 playing=true，但音频尚未开始）会乐观推进，
       随后后端回 0 → 进度条跳回（用户点名的"走过一块再回去才能正常播放"）。 */
    if (st.playing && !seeking && st.duration > 0 && st.posLive){
      st.position = Math.min(st.duration, st.position + dt);
    }
    updateFrameProgress();
    updateCardProgress();                          // R47：曲目卡底部渐变进度条每帧对齐
    try { beatTick(); } catch (_) {}               // 音频律动（真实频谱驱动封面，beat-on 才动）
    /* ⚠ R59：歌词当前行**每帧**推进 —— 旧实现只在 2s 轮询的 applyState 里
       seekLyric，行高亮会滞后整 2 秒（歌词"不跟唱"）。主循环已本地插值
       st.position，每帧驱动一次即可（内部二分/线性扫到 break，开销极小）。 */
    try { if (window.Poster) window.Poster.seekLyric(st.position); } catch (_) {}
    requestAnimationFrame(mainLoop);
  })(performance.now());
`;
    /* preamble：ctx 既是共享对象，也让老代码里的裸标识符可解析。
       new Function 非严格模式，with 可用 —— 这是"老代码零改动"的关键。 */
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + "  /* \u628a\u672c\u6a21\u5757\u9876\u5c42\u58f0\u660e\u53d1\u5e03\u5230 ctx\uff08\u4f9b\u540e\u7eed\u6a21\u5757 use\uff09 */\n  const __defs = [\"initSearchR54\", \"SEM_OPT_V\", \"migrateSemOpts\", \"_lastTick\"];\n  for (const __n of __defs) {\n    try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {}\n  }" + '\n}');
    return __run(ctx);
  },
};
