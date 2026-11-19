/* newui/js/fine/init-bridge.js — 「初始化迁移/桥接」自定义空间模块（源自 ui-init.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'init-bridge',
  source: 'ui-init.js',
  space: '初始化迁移/桥接',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  function migrateSemOpts(src){
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
`;
    const __epi = `  for (const __n of ["migrateSemOpts"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
