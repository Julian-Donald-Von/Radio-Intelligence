/* ══════════════════════════════════════════════════════════
   ui-skinset.js — Radio Intelligence 前端模块 · THEMES/主题/偏好持久化 · 双抽屉弹簧透视 · 壁纸导入 · 设置面板
   自 webapi/static/index.html 内嵌 <script> L3473-L3904 拆出（R58-7 #46）
   与原 index.html 内所有 ui-*.js 共享顶层作用域 → 加载顺序即执行顺序，勿打乱
   ══════════════════════════════════════════════════════════ */
  const THEMES = [
    { key:'default', name:'深空紫', primary:'#6a5cff', secondary:'#ff6ec7', accent:'#ffd166', dark:'#14102e', light:'#c9b8ff' },
    { key:'teal',    name:'青碧',   primary:'#0fb9b1', secondary:'#ffe066', accent:'#ff6b6b', dark:'#0d1f2d', light:'#9be8e2' },
    { key:'sunset',  name:'落日',   primary:'#ff7b54', secondary:'#ffd166', accent:'#7bdff2', dark:'#241311', light:'#ffc4a3' },
    { key:'royal',   name:'皇家蓝', primary:'#3a86ff', secondary:'#8338ec', accent:'#ffbe0b', dark:'#101528', light:'#a9c6ff' },
    { key:'moss',    name:'苔原',   primary:'#38b000', secondary:'#a7c957', accent:'#ffe066', dark:'#12210f', light:'#c9f0a2' },
    { key:'orchid',  name:'幽兰',   primary:'#9d4edd', secondary:'#ff70a6', accent:'#ffd166', dark:'#1a1030', light:'#e0b8ff' },
    { key:'ember',   name:'余烬',   primary:'#fb5607', secondary:'#ff006e', accent:'#ffe066', dark:'#221014', light:'#ffb28f' },
  ];
  const DEF_THEME = THEMES[0];
  /* 偏好状态（本地镜像；与后端 /api/ui/prefs 双向同步）
     R56：bgmode 只存 poster|acrylic（去掉 flow 流动水样） */
  let uiThemeKey  = 'default';
  let uiAccent    = '';          // 用户高亮色覆盖（空 = 跟随主题/封面动态）
  let uiBgMode    = 'poster';    // poster（歌词海报）| acrylic（亚克力封面）
  /* ═══ R78：背景内容偏好 —— wallpaper 来源 ═══
     'cover'  = 跟随歌曲封面（默认；无封面时 ensureDefaultSkin logo 临时垫底）
     'default'= 固定 Radio Intelligence 玻璃 logo（applyBrandSkin，切歌不被封面覆盖）
     与 body.wp-active 语义一致，但**显式持久化** —— 旧实现只临时挂 class、
     persistPrefs 白名单里又没有 wallpaper → 选完刷新即失、重启即丢。
     ⚠ 区别于 theme/bgmode：wallpaper 只决定"背景内容"，不参与 CSS 变量。 */
  let uiWallpaper  = 'cover';    // cover（跟随封面）| default（内置 logo 壁纸）

  /* ═══ R93 · 叠层效果（on-top effect，P4 契约）═══
     与「背景效果」（bgmode）**并列的独立维度**：ontop-effect 叠在背景效果之上，
     两者可同时生效（路线 A）。
       · 'none'  = 无效果（常驻卡；等价于关水面）
       · 'water' = 水面（首个 ontop-effect 插件，渲染在 ui-water.js 的 __water 里）
     ⚠ 旧 ui_prefs.water 保留为**只读兼容**：两向同步（任一变化都写回 water），
       避免破坏 verify_r86_water / verify_water_r70 的既有契约。 */
  let uiOnTop = 'water';
  /* 水面参数组（选中「水面」时才可编辑；整组写盘 → 后端白名单透传，不做嵌套合并）。
     specExp（S1 波纹细度）在 ui-water.js 暴露 setSpecExp 之前 UI 保持 disabled。 */
  let uiWaterOpts = {
    physics: true, eventMode: 'emotion', bank: 'bank',
    /* ★★ R110-②（2026-09-13）：`semStrength` 0.6 → **1.0**（与 `ui-water.js` 的
       `let semStrength` 同步）。⚠ 这两处 + `ui-water.js::resetOpts()` 是**同一个值的
       3 个定义点**，且**还有 `localStorage['r54.ui']` 持久化**——
       只改 `ui-water.js` 会被本文件在 init 时用 `setSemStrength` 覆盖回去（实测踩过）。
       ⇒ 值的三处定义 + `ui-init.js` 的一次性迁移（`semOptV`）必须一起改。 */
    semStrength: 1.0, semPulse: false, tier: 'Q3',
    /* ★★ R110（2026-09-13）：**偏好迁移标记** —— 只增不改。
       本组会被**原样存盘**（localStorage + 后端），且恢复逻辑是"存档里有的就覆盖"
       ⇒ 用户上一次存的 `semStrength:0.6` 会**静默盖掉新默认**（实测踩到：
       只改 `ui-water.js` 的 `let semStrength` 时，真机 C5 纹丝不动）。
       `ui-init.js::migrateSemOpts()` 据此**只强制刷一次**，之后用户手动调的值照常被尊重。 */
    semOptV: 1,
    specExp: -2.0
  };
  const WATER_OPTS_DEF = JSON.parse(JSON.stringify(uiWaterOpts));

  /* 高亮色是否被用户锁死（主题或手动取色）—— 锁死时封面动态取色不再覆盖 */
  let accentLocked = false;
  function lockAccent(locked){ accentLocked = !!locked; }

  /* ═══ R94：偏好回传**加固**（合并 + 去抖 + 单调序号）═══
     事故（用户报"拖到 −1.5 再拖回 −2.0，水没变舒缓"）：
     滑杆的每个 `input` 都直接 POST 整组 waterOpts；一次拖拽 = 几十个并发 POST。
     后端 R93 的锁只保证**文件不会被写坏**，**不保证"最后动作者胜"** —— 线程池
     拿锁顺序与用户操作顺序无关 → 落盘可能停在**中间态**（本机实测 5 轮中 2 轮：
     拖回 −2.0 却存成 −1.8333 / −1.9231 / −1.5256）。F5 或重启后滑杆与水面一起
     停在中间值上，表现就是"拖回去了但没变舒缓"。
     三重加固：
       ① **合并**：同一短窗内的多次改动并成一次 POST（拖拽 40 次 → 1~2 次）；
       ② **单调序号**：每次 POST 带页面会话 id `__sid` + 自增 `__seq`，后端丢弃过期写；
       ③ **落盘时机**：滑杆拖动中只下发不落盘，`change`（松手）才落盘（与「情绪强度」同款）。 */
  const PREFS_DEBOUNCE_MS = 300;
  const PREFS_SID = 'p' + Date.now().toString(36)
                        + Math.random().toString(36).slice(2, 8);
  let prefsSeq = 0, prefsTimer = 0, prefsPending = {};

  /* 保存偏好：localStorage 即时（本地镜像，同步且廉价）+ 后端合并去抖写盘（静默失败）。
     ⚠ immediate=true 用于「松手/点选」这类**终态**动作 —— 跳过去抖，立刻落盘。 */
  function persistPrefs(patch, immediate){
    try {
      const f = JSON.parse(localStorage.getItem('r54.ui') || '{}');
      for (const k in patch) f[k] = patch[k];
      localStorage.setItem('r54.ui', JSON.stringify(f));
    } catch (_) {}
    for (const k in patch) prefsPending[k] = patch[k];
    if (immediate){ flushPrefs(); return; }
    if (!prefsTimer) prefsTimer = setTimeout(flushPrefs, PREFS_DEBOUNCE_MS);
  }
  /* 把累积的改动合成**一次** POST（带会话 id + 单调序号；后端按序号丢弃过期写） */
  function flushPrefs(){
    if (prefsTimer){ clearTimeout(prefsTimer); prefsTimer = 0; }
    const patch = prefsPending; prefsPending = {};
    const body = {};
    if ('theme' in patch) body.theme = patch.theme;
    if ('accent' in patch) body.accent = patch.accent;
    if ('bgmode' in patch) body.bgmode = patch.bgmode;
    if ('beat' in patch) body.beat = patch.beat;
    if ('water' in patch) body.water = patch.water;   // R70 背景水面开关（旧键兼容）
    if ('wallpaper' in patch) body.wallpaper = patch.wallpaper;  // R78 背景内容偏好
    if ('ontop' in patch) body.ontop = patch.ontop;              // R93 叠层效果（on-top）
    if ('waterOpts' in patch) body.waterOpts = patch.waterOpts;  // R93 水面参数组
    if (!Object.keys(body).length) return;
    body.__sid = PREFS_SID;
    body.__seq = ++prefsSeq;
    fetch('/api/ui/prefs', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)
    }).catch(() => {});
  }
  /* 离开页面时把还押在去抖窗里的改动补发（best-effort，失败也不阻塞关闭） */
  window.addEventListener('pagehide', () => { try { flushPrefs(); } catch (_) {} });

  /* ═══ 音频律动开关（body.beat-on 门控 beatTick 封面呼吸）═══ */
  let uiBeat = true;   // 默认开（与后端 _prefs_defaults.beat 一致）
  function setBeat(on){
    uiBeat = !!on;
    document.body.classList.toggle('beat-on', uiBeat);
    persistPrefs({ beat: uiBeat }, true);
    syncBeatUI();
    if (!uiBeat){
      /* 关闭时立即清掉封面残留 transform（beatTick 不再动它） */
      try { const ci = $('dc-cover-img'); if (ci) ci.style.transform = ''; } catch (_) {}
    }
  }
  function syncBeatUI(){
    const t = $('beat-toggle');
    if (t) t.checked = uiBeat;
  }

  /* 把主题/高亮应用到 CSS 变量（老 applyTheme L1323） */
  function applyThemePalette(pal, withAccent){
    const r = document.documentElement.style;
    const p = pal || DEF_THEME;
    r.setProperty('--primary',   p.primary);
    r.setProperty('--secondary', p.secondary);
    r.setProperty('--dark',      p.dark);
    r.setProperty('--light',     p.light);
    if (withAccent !== false) r.setProperty('--accent', p.accent);
  }
  function hexToRgb(h){
    let v = String(h || '').replace('#','');
    if (v.length === 3) v = v.split('').map(c => c + c).join('');
    const n = parseInt(v.slice(0,6), 16);
    return (Number.isNaN(n)) ? null : [(n>>16)&255, (n>>8)&255, n&255];
  }
  /* 把偏好（主题 key + 高亮覆盖）整体应用到 UI */
  function applyPrefsTheme(){
    const t = THEMES.find(x => x.key === uiThemeKey) || DEF_THEME;
    if (uiAccent && hexToRgb(uiAccent)){
      applyThemePalette(t, false);
      document.documentElement.style.setProperty('--accent', uiAccent);
      document.documentElement.style.setProperty('--primary', uiAccent);
      lockAccent(true);
    } else {
      applyThemePalette(t);
      /* 默认主题 + 无高亮覆盖 → 放开关封动态取色（默认行为） */
      lockAccent(uiThemeKey !== 'default');
      if (uiThemeKey === 'default'){
        try { applyCoverPalette(st.cover); } catch (_) {}
      }
    }
    syncSkinUI();
  }
  /* 皮肤列表点亮态同步 */
  function syncSkinUI(){
    const box = $('skin-list');
    if (!box) return;
    box.querySelectorAll('.skin-card').forEach(c =>
      c.classList.toggle('active', c.dataset.key === uiThemeKey));
    const picker = $('fx-accent-picker');
    if (picker && uiAccent) picker.value = uiAccent;
  }

  /* ---- R56 · 双抽屉（设置卡 + 换肤卡）开合 + 主界面推挤透视 ----
     任一卡 .open → body.drawer-open（驱动 #cards 左推透视）；
     两卡互斥：开一个自动关另一个。

     ═══ R57-9：推挤改**阻尼弹簧**驱动（用户原话："右侧设置没推动主界面
     透视…要阻尼感…滚轮隔离"）═══
     旧：body.drawer-open #cards 靠 CSS transition .55s 一次性滑到位。
     新：推挤 translateX / rotateY 由弹簧物理（阻尼振子）逐帧驱动 ——
       开抽屉 = 弹簧冲 1（主界面被推开 + 轻微过冲回弹），
       关抽屉 = 弹簧回 0（阻尼滑回，不硬切）。
       CSS body.drawer-open #cards 规则保留作**无 JS 兜底**；
       JS 运行时写内联 transform（transition:none）覆盖之，
       settle 后交还 CSS（清内联），关闭态不残留位移。 */
  let drawerSpr = { p:0, v:0, target:0, raf:0 };
  function applyDrawerTransform(){
    const el = $('cards'); if (!el) return;
    const p = drawerSpr.p;
    if (p <= 0.0006 && drawerSpr.target <= 0){
      /* settle 到关闭：清内联，交还 CSS 兜底（transform:''） */
      el.style.transition = '';
      el.style.transform = '';
      return;
    }
    const pushMax = Math.min(innerWidth * 0.09, 132);
    el.style.transition = 'none';
    el.style.transform = 'perspective(1500px) translateX(' +
      (-pushMax * p).toFixed(2) + 'px) rotateY(' + (6.5 * p).toFixed(3) + 'deg)';
  }
  function drawerSetTarget(t){
    drawerSpr.target = t;
    if (drawerSpr.raf) return;               // 已有 rAF 在跑：只改目标
    let last = performance.now();
    const tick = function(now){
      const dt = Math.min(0.05, (now - last) / 1000); last = now;
      const k = 165, d = 20;                 // 刚度/阻尼（轻微欠阻尼 → 一次小回弹）
      const a = (drawerSpr.target - drawerSpr.p) * k - drawerSpr.v * d;
      drawerSpr.v += a * dt;
      drawerSpr.p += drawerSpr.v * dt;
      if (Math.abs(drawerSpr.target - drawerSpr.p) < 0.0006 &&
          Math.abs(drawerSpr.v) < 0.004){
        drawerSpr.p = drawerSpr.target; drawerSpr.v = 0; drawerSpr.raf = 0;
      } else {
        drawerSpr.raf = requestAnimationFrame(tick);
      }
      applyDrawerTransform();
    };
    drawerSpr.raf = requestAnimationFrame(tick);
    applyDrawerTransform();                   // 首帧同步写一次（不押 rAF）
  }
  function openDrawer(which){
    const sp = $('settings-panel'), tp = $('theme-panel');
    const self = (which === 'theme') ? tp : sp;
    const other = (which === 'theme') ? sp : tp;
    if (!self) return;
    if (other) other.classList.remove('open');
    self.classList.add('open');
    document.body.classList.add('drawer-open');
    drawerSetTarget(1);            // R57-9：弹簧把主界面推出去（阻尼）
    refreshSettings();
  }
  function closeDrawer(which){
    const p = (which === 'theme') ? $('theme-panel') : $('settings-panel');
    if (!p) return;
    p.classList.remove('open');
    /* 两卡都关 → 解除推挤透视（R57-9：弹簧阻尼滑回 0） */
    if (!$('settings-panel').classList.contains('open') &&
        !$('theme-panel').classList.contains('open')){
      document.body.classList.remove('drawer-open');
      drawerSetTarget(0);
    }
  }
  function anyDrawerOpen(){
    return !!($('settings-panel') && $('settings-panel').classList.contains('open')) ||
           !!($('theme-panel') && $('theme-panel').classList.contains('open'));
  }
  function openSettings(){ openDrawer('settings'); }
  function closeSettings(){ closeDrawer('settings'); }
  function openTheme(){ openDrawer('theme'); }
  function closeTheme(){ closeDrawer('theme'); }
  function refreshSettings(){
    renderSkinList();
    syncBgModeUI();
    syncBeatUI();
    syncOnTopUI();          // R93：叠层效果卡片点亮态 + 参数区展开态
    syncWaterOptsUI();      // R93：水面参数区控件回填（外部改过 prefs 也不失真）
    renderDislikeList();
    /* R66：原「我的壁纸」网格已移除（用户判定冗余）→ 不再拉 /api/wallpapers 列表。
       本地图片导入仍走后端持久化，只是不再有"壁纸管理"UI。 */
    try { if (typeof renderSettingsGroups === 'function') renderSettingsGroups(); } catch (_) {}
  }

  /* 皮肤网格 */
  function renderSkinList(){
    const box = $('skin-list'); if (!box) return;
    box.innerHTML = THEMES.map(t =>
      `<div class="skin-card${t.key===uiThemeKey?' active':''}" data-key="${t.key}">
         <div class="sc-prev" style="background:linear-gradient(135deg, ${t.primary}, ${t.secondary})"></div>
         <div class="sc-name">${esc(t.name)}</div>
       </div>`).join('');
    box.querySelectorAll('.skin-card').forEach(card => {
      card.addEventListener('click', () => {
        const key = card.dataset.key;
        uiThemeKey = key;
        /* 手动换肤 = 锁高亮（不再被封面取色覆盖）；除非点默认主题且无自定义 accent */
        uiAccent = '';
        persistPrefs({ theme: key, accent: '' }, true);
        applyPrefsTheme();
        /* R66 精简提示：皮肤卡选中态 + 全局配色即时变化，肉眼可见 → 不再 toast 复述。 */
      });
    });
  }

  /* 背景效果：poster（皮肤原样，默认）/ acrylic（亚克力 blur）。
     ⚠ R58-4 (#43) 语义（用户本轮回溯澄清）：
       皮肤（歌词海报 / 自定义图）= 可换**底层**（#bgimg 承载）；
       亚克力 = 在皮肤之上叠加的**可选明亮 blur 效果**（非暗色 brightness(.6)），
       无效果也是一种效果；歌词 / 操作 UI / 气泡全在效果层之上。
     · bgmode=poster（默认）：皮肤锐利显示 + 海报文字层（按钮就叫「无效果」）
     · bgmode=acrylic：皮肤明亮 blur（CSS body.bgmode-acrylic 驱动 #bgimg）*/
  function setBgMode(mode){
    uiBgMode = (mode === 'acrylic') ? 'acrylic' : 'poster';
    document.body.classList.toggle('bgmode-acrylic', uiBgMode === 'acrylic');
    persistPrefs({ bgmode: uiBgMode }, true);
    syncBgModeUI();
    try { if (window.Poster && window.Poster.setMode) window.Poster.setMode(uiBgMode); } catch (_) {}
    /* R59：切到任意背景效果时若还没有任何背景内容（无封面/无自定义壁纸）
       就补程序化默认皮肤 —— 否则 poster/acrylic 切换在无封面期看不出差异；
       有 src（封面/壁纸）时 ensureDefaultSkin 内部直接跳过。 */
    try {
      const bgi = $('bgimg');
      if (bgi && !bgi.getAttribute('src')) ensureDefaultSkin();
    } catch (_) {}
    /* R66 精简提示：按钮已高亮 + 背景即时变化 → 不再 toast 复述。 */
  }
  function syncBgModeUI(){
    const row = $('bg-mode-row'); if (!row) return;
    row.querySelectorAll('.wp-type').forEach(b =>
      b.classList.toggle('active', b.dataset.bgmode === uiBgMode));
  }

  /* ═══ R93 · 叠层效果（on-top effect）选择器 + 水面参数区 ═══
     形态与「背景效果」如出一辙：分段卡 n 选一（复用 .wp-type）+ 常驻「无效果」卡；
     选中「水面」时下方 #water-opts 展开（P4 :107 = 每个 ontop 插件自带 1 个参数组）。

     职责边界（避免两处各写一份点亮态而分叉）：
       · 本文件 = #ontop-row / 参数区的**唯一拥有者**（UI 镜像 + 偏好落盘）；
       · ui-water.js = `enabled` 的**唯一拥有者**（setEnabled 仍是唯一启停入口）；
       · 桥梁 = setOnTop() 调 __water.setEnabled()，ui-water 的 syncToggle() 反调 syncOnTopUI()。 */
  const ONTOP_IDS = ['none', 'water'];
  function onTopNormalize(v){ return (ONTOP_IDS.indexOf(v) >= 0) ? v : 'none'; }
  /* 唯一入口：卡片选中 → 启停水面 + 落盘（ontop 新键 / water 旧键两向同步） */
  function setOnTop(id, persist){
    uiOnTop = onTopNormalize(id);
    syncOnTopUI();
    /* ⚠ persist=false：本函数自己会把 ontop + water 一次落盘。若让 __water.setEnabled
       也落盘（默认 true），同一次点击会发**两个并发 POST** → 后端「读-改-写」互踩，
       把 ui_prefs.json 写成坏文件（R93 实测 → 全部偏好静默回默认）。 */
    try { if (window.__water && window.__water.setEnabled)
            window.__water.setEnabled(uiOnTop === 'water', false); } catch (_) {}
    if (uiOnTop === 'water') applyWaterOpts();     // 切回水面 → 按存档重新下发（切档保场）
    if (persist !== false) persistPrefs({ ontop: uiOnTop, water: uiOnTop === 'water' }, true);
  }
  function syncOnTopUI(){
    const row = $('ontop-row');
    if (row) row.querySelectorAll('.wp-type').forEach(b =>
      b.classList.toggle('active', b.dataset.ontop === uiOnTop));
    const box = $('water-opts');
    if (box) box.classList.toggle('open', uiOnTop === 'water');
  }
  /* 把 uiWaterOpts 下发给 __water（每个 key 独立 try —— 某 API 未实现不影响其余） */
  function applyWaterOpts(){
    const w = window.__water; if (!w) return;
    const o = uiWaterOpts;
    const call = (fn, arg) => { try { if (typeof w[fn] === 'function') w[fn](arg); } catch (_) {} };
    call('setPhysics',        o.physics === false ? 'off' : 'on');
    call('setEventMode',      o.eventMode);
    call('setWaveBank',       o.bank);
    call('setSemStrength',    o.semStrength);
    call('setTransitionPulse', !!o.semPulse);
    call('setTier',           o.tier);
    /* specExp → 「波纹细度」：R92-S1 已落地（`ui-water.js` 暴露 setSpecExp/specExp，
       范围与控件 min/max 严格一致 [−2.0, −1.5]，不存在拖不动/被静默夹掉的死区） */
    call('setSpecExp',        o.specExp);
    syncWaterOptsUI();
  }
  /* 参数区控件回填（展开 / 换档 / 恢复默认 / 启动恢复偏好后都要刷） */
  function syncWaterOptsUI(){
    const o = uiWaterOpts;
    /* ⚠ R94：只在**真的不同**时才写回控件 —— 否则拖动中的 `input → apply → 回填`
       会把滑杆自身再写一次，与指针抢同一个元素（值相等时写回纯属自找麻烦）。 */
    const set = (id, v) => {
      const el = $(id);
      if (el && el.value !== String(v)) el.value = String(v);
    };
    const txt = (id, v) => { const el = $(id); if (el) el.textContent = v; };
    set('wo-specexp', o.specExp);
    txt('wo-specexp-v', Number(o.specExp).toFixed(1));
    /* 谱形标签跟着指数走（自解释）：−2.0 = Phillips/Tessendorf 物理谱（a ∝ k⁻²）；
       越往 −1.5 越接近饱和/平衡区那一支文献（a ∝ k^−1.5，更细） */
    txt('wo-specexp-tag', Number(o.specExp) <= -1.99 ? 'k⁻² 物理谱' : 'k⁻¹·⁵ 饱和区');
    set('wo-sem', o.semStrength);
    txt('wo-sem-v', Number(o.semStrength).toFixed(2));
    const ph = $('wo-physics'); if (ph) ph.checked = o.physics !== false;
    const pu = $('wo-pulse');   if (pu) pu.checked = !!o.semPulse;
    segSync('wo-eventmode', o.eventMode);
    segSync('wo-bank', o.bank);
    segSync('wo-tier', o.tier);
  }
  function segSync(id, val){
    const box = $(id); if (!box) return;
    box.querySelectorAll('.wp-type').forEach(b =>
      b.classList.toggle('active', b.dataset.v === String(val)));
  }
  /* 改一个/多个参数 → 落盘 + 立即下发（参数区改动即时可听可见，与画质档同款）
     ⚠ R94：`immediate=true` —— 参数区控件都是**离散终态**动作，跳过去抖直接落盘
     （避免"点完 300ms 内 F5"丢掉这次改动）；滑杆自己走 input/change 分流。 */
  function waterOptsPatch(patch){
    for (const k in patch) uiWaterOpts[k] = patch[k];
    persistPrefs({ waterOpts: uiWaterOpts }, true);
    applyWaterOpts();
  }
  function bindSeg(id, cb){
    const box = $(id); if (!box) return;
    box.querySelectorAll('.wp-type').forEach(b =>
      b.addEventListener('click', () => cb(b.dataset.v)));
  }
  function bindWaterOpts(){
    /* ⚠ R94：拖动中**只下发、不落盘**，`change`（松手）时才一次性落盘 ——
       与下面「情绪强度」同款。旧实现每个 input 都 POST 整组 waterOpts，
       一次拖拽几十个并发写，落盘顺序不可控 → 停在中间态（见 persistPrefs 注释）。 */
    const se = $('wo-specexp');
    if (se){
      const mirror = () => {
        uiWaterOpts.specExp = Number(se.value);
        const v = $('wo-specexp-v'); if (v) v.textContent = Number(se.value).toFixed(1);
        const t = $('wo-specexp-tag');
        if (t) t.textContent = Number(se.value) <= -1.99 ? 'k⁻² 物理谱' : 'k⁻¹·⁵ 饱和区';
      };
      se.addEventListener('input', () => {
        mirror();
        try { if (window.__water && window.__water.setSpecExp)
                window.__water.setSpecExp(uiWaterOpts.specExp); } catch (_) {}
      });
      se.addEventListener('change', () => {
        mirror();
        persistPrefs({ waterOpts: uiWaterOpts }, true);   // 松手：跳过去抖，立刻落盘
        applyWaterOpts();                                 // 再确认一次终值（防拖动末帧丢失）
      });
    }
    /* 情绪强度滑杆：拖动中只轻量下发（不落盘/不整组刷 UI，避免拖动卡顿），
       release（change）时一次性持久化。 */
    const sm = $('wo-sem');
    if (sm){
      sm.addEventListener('input', () => {
        uiWaterOpts.semStrength = Number(sm.value);
        const v = $('wo-sem-v'); if (v) v.textContent = Number(sm.value).toFixed(2);
        try { if (window.__water && window.__water.setSemStrength)
                window.__water.setSemStrength(uiWaterOpts.semStrength); } catch (_) {}
      });
      sm.addEventListener('change', () => persistPrefs({ waterOpts: uiWaterOpts }, true));
    }
    const ph = $('wo-physics');
    if (ph) ph.addEventListener('change', () => waterOptsPatch({ physics: ph.checked }));
    const pu = $('wo-pulse');
    if (pu) pu.addEventListener('change', () => waterOptsPatch({ semPulse: pu.checked }));
    bindSeg('wo-eventmode', v => waterOptsPatch({ eventMode: v }));
    bindSeg('wo-bank',      v => waterOptsPatch({ bank: v }));
    bindSeg('wo-tier',      v => waterOptsPatch({ tier: v }));
    /* 换一批波形：确定性重掷随机银行（rndSeed 同种子 → 逐位可复现） */
    const rer = $('wo-reroll');
    if (rer) rer.addEventListener('click', () => {
      let s = 1;
      try { s = (Date.now() % 100000) | 0; } catch (_) {}
      try { if (window.__water && window.__water.rndSeed) window.__water.rndSeed(s); } catch (_) {}
      toast('波形已重掷');
    });
    /* 恢复默认：本地参数组回出厂 + __water 回观测基线（SEM_BASE / 默认档位） */
    const rst = $('wo-reset');
    if (rst) rst.addEventListener('click', () => {
      uiWaterOpts = JSON.parse(JSON.stringify(WATER_OPTS_DEF));
      persistPrefs({ waterOpts: uiWaterOpts }, true);
      try { if (window.__water && window.__water.resetOpts) window.__water.resetOpts(); } catch (_) {}
      applyWaterOpts();
      toast('水面参数已恢复默认');
    });
    const adv = $('wo-adv'), advBtn = $('wo-adv-btn');
    if (adv && advBtn) advBtn.addEventListener('click', () => adv.classList.toggle('open'));
  }

  /* ---- 自定义壁纸导入（老 UI wp-type-row / wp-input / importWallpaper 移植）
       支持图片/视频/链接/壁纸引擎；前端即时把源喂给背景（bgimg 亚克力或
       Poster 层），并经后端 WallpaperImporter 持久化。 */
  let wpType = 'image';
  /* cover=歌曲封面 | image=图片 | link=链接 | video=视频壁纸 | wallpaper=壁纸引擎工程
     ⚠ R62 #10：恢复 video / wallpaper 两型（R61 曾移除）。用户原话：作为按钮
     放皮肤自定义设置里，**共用一个导入框**。video 前端直接可用（#bgvideo +
     FileReader/dataURL/直链）。
     ⚠ R65：wallpaper 真导入 —— 后端 WallpaperImporter 本就能解析工程
     project.json / Steam 创意工坊链接 / zip：scene 3D 壁纸退化为 preview 图、
     video/web 场景正常渲染（core/skin.py WallpaperEngineSkin.render_descriptor），
     并非「必须桌面端」。前端接 REST：Steam 链接/ID → import-engine；本地工程
     zip → import-zip 上传。 */
  /* ═══ R61：切回「歌曲封面」═══
     自定义壁纸一旦导入（wp-active），render() 就不再喂封面 —— 用户切不回封面背景。
     摘除 wp-active / 清 #bgimg 的壁纸 src 后，让 render() 重新以 st.cover 喂 bgimg。
     这里提供 useCoverSkin()：body+img 摘 wp-active，且 bgimg src 先置空、摘 has-bg
     （避免旧壁纸残留一帧），再按当前效果模式决定是否立即补封面。 */
  function useCoverSkin(){
    /* R62 #10：切回封面 → 停掉视频壁纸 */
    try { hideVideoWallpaper(); } catch (_) {}
    const bd = document.body, bgimg = $('bgimg');
    bd.classList.remove('wp-active');
    uiWallpaper = 'cover';               /* R78：背景内容偏好回「跟随封面」 */
    if (bgimg){
      bgimg.classList.remove('wp-active');
      bgimg.removeAttribute('src');        /* 逼 render() 用新封面重新喂 */
      bgimg.classList.remove('has-bg');
    }
    /* 当前曲封面（st.cover）即时补上 —— 走统一路径（同时喂 Poster 氛围参考）。
       无封面 → ensureDefaultSkin 程序化底（render 内部逻辑一致）。 */
    if (st && st.cover){
      try {
        if (bgimg){
          bgimg.setAttribute('src', st.cover);
          bgimg.classList.add('has-bg');
        }
        try { if (window.Poster) window.Poster.setCover(st.cover); } catch (_) {}
        try { if (window.LiquidBg) window.LiquidBg.setSource(st.cover); } catch (_) {}
      } catch (_) {}
    } else {
      try { ensureDefaultSkin(); } catch (_) {}
    }
  }
  /* ═══ R67-5：Radio Intelligence 玻璃 logo 作为内置默认背景 ═══
     与 useCoverSkin 一样：摘 wp-active、停视频壁纸、设 bgimg src。
     区别：src 是 /brand/logo.png（项目自带，不入用户壁纸库，不走后端）。
     有歌曲时也会被切到默认 logo（用户原话：默认选项就是这个图片作为壁纸），
     所以这是「皮肤」而非「封面」—— 永远展示 logo，无视 st.cover。 */
  function applyBrandSkin(){
    try { hideVideoWallpaper(); } catch (_) {}
    const bd = document.body, bgimg = $('bgimg');
    bd.classList.add('wp-active');
    uiWallpaper = 'default';             /* R78：持久化镜像 + 切歌不被覆盖判据 */
    if (bgimg){
      bgimg.classList.add('wp-active');
      bgimg.classList.add('has-bg');
      bgimg.setAttribute('src', '/brand/logo.png');
    }
    try { if (window.Poster) window.Poster.setCover(''); } catch (_) {}
    try { if (window.LiquidBg) window.LiquidBg.setSource(''); } catch (_) {}
    persistPrefs({ wallpaper: 'default' });
  }
  /* ═══ R78：按持久化偏好恢复背景内容（启动 / 偏好回填时调用）═══
     uiWallpaper 三态：
       · default  → applyBrandSkin()：固定 logo 壁纸，wp-active，切歌不被封面盖
       · custom   → 用户导入的自定义壁纸：保持当前 bgimg src（刷新后背景内容由
                    /api/ui/prefs 之外的自定义壁纸机制另行恢复，这里不覆盖）；
                    若 bgimg 无 src（视频壁纸等未持久化场景）→ 兜底 ensureDefaultSkin
       · cover    → 跟随封面：交给 render() / ensureDefaultSkin 处理，无需干预
     仅 default 是**纯偏好驱动**（logo 项目内置、无需后端），因此启动恢复只对它
     负责；custom 壁纸是图片文件，其"刷新后仍在"由后端 wallpaper 导入保证。 */
  function applyWallpaperPref(){
    try {
      if (uiWallpaper === 'default'){ applyBrandSkin(); return; }
      if (uiWallpaper === 'custom'){
        const bgi = $('bgimg');
        if (bgi && !bgi.getAttribute('src')) ensureDefaultSkin();
        return;
      }
      /* cover：当前曲封面（若有）由 render() 喂；无封面 ensureDefaultSkin 垫底 */
    } catch (_) {}
  }
  function bindWallpaper(){
    const typeRow = $('wp-type-row');
    if (typeRow) typeRow.querySelectorAll('.wp-type').forEach(b => {
      b.addEventListener('click', () => {
        typeRow.querySelectorAll('.wp-type').forEach(x => x.classList.remove('active'));
        b.classList.add('active'); wpType = b.dataset.wp;
        const inp = $('wp-input');
        if (inp) inp.placeholder = {
          cover:'当前播放歌曲封面（无需导入）',
          image:'粘贴图片 URL，或点「选择文件」',
          link:'粘贴壁纸直链 URL',
          video:'粘贴视频直链 URL，或点「选择文件」',
          wallpaper:'粘贴 Steam 创意工坊链接 / 壁纸 ID，或点「选择文件」上传 .zip 工程包',
          /* R67-5：默认 = Radio Intelligence 玻璃 logo（项目自带，不入用户壁纸库） */
          default:'Radio Intelligence 玻璃 logo（项目内置默认）'
        }[wpType] || inp.placeholder;
        if (wpType === 'cover') useCoverSkin();   /* R66：切封面即时可见，不再 toast */
        if (wpType === 'default') applyBrandSkin();   /* R67-5：内置默认背景 */
      });
    });
    const imp = $('wp-import'), inp = $('wp-input'), pick = $('wp-pick'), file = $('wp-file');
    /* ═══ R57-2：本地图片百分百可用 ═══
       http 页面不能读 file:// 路径 → 旧「粘贴本地路径」在浏览器里必失败。
       新增「选择文件」：FileReader 把图片读成 dataURL → 直接喂背景层，
       无需后端 WallpaperImporter。拖放同样优先走 FileReader。 */
    function readFileAsWallpaper(f){
      if (!f) return;
      /* R65：wallpaper 类型 + .zip → 上传后端解包导入（壁纸引擎工程包 /
         Steam 导出 zip）。zip 不是图片/视频，须在图片校验前拦截。 */
      if (wpType === 'wallpaper' && /\.zip$/i.test(f.name || '')){
        uploadWallpaperZip(f);
        return;
      }
      /* R62 #10：video 类型接受视频文件（其它一律按图片校验） */
      const isVideo = wpType === 'video'
        && (/^video\//.test(f.type || '')
            || /\.(mp4|webm|ogg|ogv|mov)$/i.test(f.name || ''));
      if (!isVideo && f.type && f.type.indexOf('image/') !== 0){
        toast('仅支持图片 / 视频文件'); return;
      }
      /* ═══ R63：图片文件导入 = 真实后端持久化 ═══
         本地图片（image/link/wallpaper 类型选的文件）POST 原字节到
         /api/wallpapers/import → SkinManager 落 plugins/skins + assets，
         重启仍在。仅 video 保留 FileReader dataURL 即时预览（视频壁纸
         网页端不持久化，属诚实降级）。
         R66：导入成功后不再刷「我的壁纸」网格（该网格已随冗余功能移除），
         直接用响应里的 /skin-assets URL 应用为背景。 */
      if (!isVideo){
        uploadWallpaperFile(f);
        return;
      }
      const rd = new FileReader();
      rd.onload = () => {
        applyWallpaperVideo(String(rd.result)); toast('视频壁纸已应用');
      };
      rd.onerror = () => toast('读取文件失败');
      rd.readAsDataURL(f);
    }
    if (pick && file){
      pick.addEventListener('click', () => { file.value = ''; file.click(); });
      file.addEventListener('change', () => {
        const f = file.files && file.files[0];
        if (f){ readFileAsWallpaper(f); }
      });
    }
    if (imp && inp) imp.addEventListener('click', () => {
      const src = inp.value.trim();
      if (!src){ toast('请先粘贴 URL / 路径，或点「选择文件」'); return; }
      try { importWallpaperSrc(wpType, src); } catch (_) {}
    });
    /* 拖放：R57-2 优先 FileReader 读真实文件（次选填路径文本） */
    const dz = $('wp-input') || document.body;
    dz.addEventListener('dragover', e => e.preventDefault());
    dz.addEventListener('drop', e => {
      e.preventDefault();
      const f = e.dataTransfer.files && e.dataTransfer.files[0];
      if (f){
        /* R62 #10：video 类型也放行 —— readFileAsWallpaper 内部按 wpType 分流 */
        readFileAsWallpaper(f);
      }
    });
  }

  /* ═══ 本地图片 / 壁纸引擎上传（后端持久化导入）═══
     R66：原「我的壁纸」网格（loadMyWallpapers / renderWallpaperGrid）已移除 ——
     与上方「自定义壁纸」重复。上传仍走后端 SkinManager 落盘（重启仍在），
     收尾只做一件事：把返回的 /skin-assets URL 直接应用为背景。 */
  function uploadWallpaperFile(f){
    if (!f) return;
    const okType = /^image\//.test(f.type || '')
      || /\.(png|jpe?g|gif|webp|bmp)$/i.test(f.name || '');
    if (!okType){ toast('仅支持图片文件（持久化导入）'); return; }
    toast('正在导入壁纸…');
    fetch('/api/wallpapers/import?filename=' + encodeURIComponent(f.name), {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: f
    }).then(r => r.json()).then(d => {
      if (!d || !d.ok){ toast('导入失败：' + ((d && d.error) || '未知错误')); return; }
      /* 应用为背景（/skin-assets URL 喂 #bgimg → 自定义壁纸 wp-active） */
      if (d.url) applyWallpaperImage(d.url);
      toast('壁纸已导入并应用');
    }).catch(() => toast('导入失败（后端未连接？）'));
  }
  /* ═══ R65：壁纸引擎 zip 上传导入（场景/网页壁纸工程包）═══
     之前 wallpaper 类型只有「需桌面端解析」toast；现在 zip 直传后端解包。
     后端 _import_zip 找 project.json（scene→preview 图 / video / web）或首个
     媒体文件导入。前端收尾同图片：应用背景 + 刷网格。 */
  function uploadWallpaperZip(f){
    if (!f) return;
    toast('正在导入壁纸引擎工程…');
    fetch('/api/wallpapers/import-zip?filename=' + encodeURIComponent(f.name), {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: f
    }).then(r => r.json()).then(d => {
      if (!d || !d.ok){ toast('导入失败：' + ((d && d.error) || '未知错误')); return; }
      if (d.url) applyWallpaperImage(d.url);
      toast('壁纸引擎已导入');
    }).catch(() => toast('导入失败（后端未连接？）'));
  }
  function importWallpaperSrc(type, src){
    /* 图片/链接 → 背景图；歌曲封面 → useCoverSkin；视频 → #bgvideo；
       壁纸引擎 → 真导入（R65：此前只 toast 引导「需桌面端解析」，用户反馈
       「场景和网页都没办法导入」。后端 WallpaperImporter 本就能解析工程 /
       Steam 链接 / zip —— scene 退化为 preview 图、video/web 正常渲染，
       无需桌面端。现在接 REST 导入并把返回的 /skin-assets URL 应用为背景）。 */
    if (type === 'cover'){ useCoverSkin(); return; }   /* R66：不再 toast 复述 */
    if (type === 'video'){
      applyWallpaperVideo(src);
      return;
    }
    if (type === 'wallpaper'){
      /* Steam 创意工坊链接 / 数字 ID → 后端定位本地订阅并导入 */
      const isSteam = /steamcommunity\.com\/(sharedfiles|workshop)/i.test(src)
        || /^https?:\/\//i.test(src) || /^\d{4,}$/.test(src.trim());
      if (isSteam){
        importWallpaperEngine(src);
        return;
      }
      /* 本地盘符 / file:// 路径 → 浏览器读不到 → 引导上传 zip */
      toast('网页无法直接读取本地工程文件夹，请用「选择文件」上传壁纸引擎导出的 .zip');
      const file = $('wp-file'); if (file) file.click();
      return;
    }
    const looksLocal = /^[a-zA-Z]:[\\/]|^file:\/\//.test(src);
    if (looksLocal){
      toast('浏览器无法直接读本地路径，请点「选择文件」上传');
      const file = $('wp-file'); if (file) file.click();
      return;
    }
    if (type === 'image' || type === 'link'){
      applyWallpaperImage(src);   /* R66：背景即时变化，不再 toast */
    } else {
      toast('暂不支持该类型');
    }
  }
  function importWallpaperEngine(src){
    /* ⚠ R65：真正走后端 WallpaperImporter（不再是「需桌面端」降级）。
       scene 3D 壁纸退化为 preview 图 / video / web 场景由渲染层解析。 */
    fetch('/api/wallpapers/import-engine?source=' + encodeURIComponent(src)
          + '&name=wallpaper', { method: 'POST' })
      .then(r => r.json()).then(d => {
        if (!d || !d.ok){ toast('导入失败：' + ((d && d.error) || '后端未连接')); return; }
        /* R66：原 activateWallpaper(key) 靠"重拉网格找 url"，网格已移除
           → 直接用导入响应里的 url 应用背景。 */
        if (d.url) applyWallpaperImage(d.url);
        else useCoverSkin();
        toast('壁纸引擎已导入');
      }).catch(() => toast('导入失败（后端未连接？）'));
  }
  /* ═══ R62 #10：视频壁纸（#bgvideo）═══
     与 #bgimg 同为皮肤内容位、互斥显示：
       · 设 src → #bgvideo.has-bg 显示、#bgimg 隐藏（has-bg 摘除）；
       · body.wp-active = 自定义壁纸优先（切歌不被封面覆盖）。
     src 支持：远程直链 URL / FileReader dataURL（本地文件）。
     ⚠ 视频自动播放策略：muted+loop+playsinline+autoplay（HTML 属性已设），
     play() 失败（浏览器策略）时静默 —— 壁纸是装饰，不能因播放策略报错。 */
  function applyWallpaperVideo(src){
    const v = $('bgvideo'); if (!v) return;
    if (!src){ return; }
    v.src = src;
    v.classList.add('has-bg', 'wp-active');
    document.body.classList.add('wp-active');
    uiWallpaper = 'custom';              /* R78：自定义视频壁纸（非 cover/default） */
    /* 隐藏图片壁纸（若有）—— 皮肤内容位同一时刻只有一个可见 */
    const bgimg = $('bgimg');
    if (bgimg){
      bgimg.classList.remove('wp-active', 'has-bg');
      bgimg.removeAttribute('src');
    }
    try { v.play().catch && v.play().catch(() => {}); } catch (_) {}
    /* 氛围参考同步（Poster/LiquidBg 吃不到视频帧，仅清掉旧图引用） */
    try { if (window.Poster) window.Poster.setCover(''); } catch (_) {}
    try { if (window.LiquidBg) window.LiquidBg.setSource(''); } catch (_) {}
  }
  function hideVideoWallpaper(){
    const v = $('bgvideo');
    if (v){
      v.pause && v.pause();
      v.removeAttribute('src');
      v.classList.remove('has-bg', 'wp-active');
    }
  }
  function applyWallpaperImage(src){
    /* R62 #10：图壁纸激活 → 停掉视频壁纸（皮肤内容位互斥，同一时刻仅一个可见） */
    try { hideVideoWallpaper(); } catch (_) {}
    /* ═══ R57-2：导入壁纸"百分百可见"重构 ═══
       旧实现只设 #bgimg.src，而 #bgimg 默认 opacity:0、仅 acrylic 模式
       （body.bgmode-acrylic #bgimg）才可见 → poster 模式导入"没反应"。
       新模型：#bgimg 是唯一背景壁纸位（见 R57-1 CSS），有 src 即显示；
       这里设 src + body.wp-active（自定义壁纸优先：切歌不被封面覆盖、
       锐利显示不做亚克力模糊）。歌词海报画布透明叠上，壁纸即时可见。
       ⚠ R59：wp-active **不再**强制 filter:none（CSS 覆盖已删）——
       明亮亚克力 blur 对自定义壁纸同样生效（用户原话：亚克力是背景效果，
       自定义壁纸/封面只是背景内容，两者正交）。 */
    const bgimg = $('bgimg');
    const bd = document.body;
    if (bgimg){
      bgimg.src = src;
      bgimg.classList.add('has-bg', 'wp-active');
      bd.classList.add('wp-active');
      uiWallpaper = 'custom';            /* R78：自定义图片壁纸（非 cover/default） */
    }
    /* 画布氛围色参考（透明海报下局部光晕跟随壁纸） */
    try { if (window.Poster) window.Poster.setCover(src); } catch (_) {}
    try { if (window.LiquidBg) window.LiquidBg.setSource(src); } catch (_) {}
  }
  /* 切歌时若存在自定义壁纸则不被封面覆盖 —— render() 只在 body 无 wp-active 时喂封面 */
  function isCustomWallpaper(){ return document.body.classList.contains('wp-active'); }

  /* 不喜欢的音乐列表（设置面板只读 + 移除） */
  function renderDislikeList(){
    const list = $('dislike-list'); if (!list) return;
    fetch('/api/dislikes').then(r => r.json()).then(d => {
      const items = (d && Array.isArray(d.entries)) ? d.entries
                  : (d && Array.isArray(d.items))  ? d.items : [];
      const cnt = $('dislike-count'); if (cnt) cnt.textContent = items.length + ' 首';
      if (!items.length){
        list.innerHTML = '<div class="dl-empty">还没有不喜欢的歌曲 —— AI 会避开它们。</div>';
        return;
      }
      list.innerHTML = items.map(it => {
        /* R67-4：API 不再预兜底"未知标题" —— 现在由前端统一处理空值。 */
        const t = it.title || '未知歌曲';
        const a = it.artist || '未知歌手';
        return `<div class="dl-row" data-key="${esc(it.key || '')}">
           <span class="dl-name" title="${esc(t)}">${esc(t)}</span>
           <span class="dl-artist">${esc(a)}</span>
           <button class="dl-rm" title="移除" data-key="${esc(it.key || '')}">✕</button>
         </div>`;
      }).join('');
      list.querySelectorAll('.dl-rm').forEach(btn => {
        btn.addEventListener('click', ev => {
          ev.stopPropagation();
          const key = btn.getAttribute('data-key'); if (!key) return;
          fetch('/api/dislike/remove?key=' + encodeURIComponent(key), { method:'POST' })
            .then(r => r.json()).catch(() => {})
            .then(() => { renderDislikeList(); toast('已移出「不喜欢的音乐」'); });
        });
      });
    }).catch(() => {
      const cnt = $('dislike-count'); if (cnt) cnt.textContent = '0 首';
      list.innerHTML = '<div class="dl-empty">后端未连接</div>';
    });
  }

  /* ---- R56 · 事件绑定（齿轮→设置卡 / 衣架→换肤卡 / 关闭 / 外点 / Esc / 角部 hover） ---- */
  function bindSettingsEvents(){
    /* ═══ R62 #8：设置分组折叠（移植老 UI .set-fold 折叠机制）═══
       点 data-foldable 分组的 h4 → 整组 collapsed 折叠/展开。
       委派在 document 上：动态渲染的分组（如不喜欢列表刷新）也生效。 */
    document.addEventListener('click', ev => {
      const h = ev.target && ev.target.closest
        ? ev.target.closest('.set-group[data-foldable] > h4') : null;
      if (!h) return;
      const grp = h.parentElement;
      if (grp) grp.classList.toggle('collapsed');
    });
    const gear   = $('settings-gear');
    /* ═══ R62 #9：本地曲库导入 / 重新扫描 ═══
       导入文件夹：POST /api/library/import/folder?folder=…（后端扫描入库）。
       重新扫描：POST /api/library/scan（扫配置根目录）。成功后刷新曲库列表
       （若侧边面板正开在曲库 tab）并 toast 结果。 */
    const libImp = $('lib-import-folder'), libInp = $('lib-folder-input');
    if (libImp && libInp) libImp.addEventListener('click', () => {
      const folder = libInp.value.trim();
      if (!folder){ toast('请先输入文件夹路径'); return; }
      fetch('/api/library/import/folder?folder=' + encodeURIComponent(folder),
            { method:'POST' })
        .then(r => r.json()).then(d => {
          if (d && d.ok) toast('已导入 ' + (d.added || 0) + ' 首到曲库');
          else toast((d && d.error) || '导入失败');
        }).catch(() => toast('导入失败：后端未连接'));
    });
    const libScan = $('lib-scan');
    if (libScan) libScan.addEventListener('click', () => {
      fetch('/api/library/scan', { method:'POST' })
        .then(r => r.json()).then(d => {
          if (d && d.ok) toast('扫描完成，新增 ' + (d.added || 0) + ' 首');
          else toast((d && d.error) || '扫描失败');
        }).catch(() => toast('扫描失败：后端未连接'));
    });
    const hanger = $('theme-hanger');
    const setClose = $('set-close');
    const themeClose = $('theme-close');
    if (gear) gear.addEventListener('click', () => {
      if ($('settings-panel') && $('settings-panel').classList.contains('open')) closeSettings();
      else openSettings();
    });
    if (hanger) hanger.addEventListener('click', () => {
      if ($('theme-panel') && $('theme-panel').classList.contains('open')) closeTheme();
      else openTheme();
    });
    /* R67-2：原「打开换肤面板」入口（设置卡内 appeal section 的按钮）随整个
       外观 section 一并删除 —— 用户原话"皮肤界面在换肤按钮里，设置页保留=冗余"。
       换肤卡仍由 #theme-hanger 衣架钮 / openTheme() 走原路径。 */
    if (setClose) setClose.addEventListener('click', closeSettings);
    if (themeClose) themeClose.addEventListener('click', closeTheme);
    /* 外点关闭：点任一打开的卡 + 两按钮之外 → 全关 */
    document.addEventListener('click', ev => {
      if (!anyDrawerOpen()) return;
      const t = ev.target;
      if (!t || !t.closest) return;
      if (t.closest('#settings-panel') || t.closest('#theme-panel') ||
          t.closest('#settings-gear') || t.closest('#theme-hanger')) return;
      if ($('settings-panel') && $('settings-panel').classList.contains('open')) closeSettings();
      if ($('theme-panel') && $('theme-panel').classList.contains('open')) closeTheme();
    });
    /* Esc 优先关抽屉（先关设置卡，再关换肤卡） */
    document.addEventListener('keydown', ev => {
      if (ev.key !== 'Escape') return;
      if ($('settings-panel') && $('settings-panel').classList.contains('open')){
        closeSettings(); ev.preventDefault();
      } else if ($('theme-panel') && $('theme-panel').classList.contains('open')){
        closeTheme(); ev.preventDefault();
      }
    });
    /* 背景模式按钮 */
    const bgRow = $('bg-mode-row');
    if (bgRow) bgRow.querySelectorAll('.wp-type').forEach(b => {
      b.addEventListener('click', () => setBgMode(b.dataset.bgmode));
    });
    /* 叠层效果卡片（R93）：点击即切换（segmented card switcher；沿用 dock 铁则
       「简单点亮、禁 :active scale」，无滑块）+ 参数区控件绑定与首次回填。 */
    const onTopRow = $('ontop-row');
    if (onTopRow) onTopRow.querySelectorAll('.wp-type').forEach(b => {
      b.addEventListener('click', () => setOnTop(b.dataset.ontop));
    });
    bindWaterOpts();
    syncOnTopUI();
    applyWaterOpts();
    /* 音频律动开关 */
    const beatToggle = $('beat-toggle');
    if (beatToggle){
      beatToggle.addEventListener('change', () => setBeat(beatToggle.checked));
      beatToggle.checked = uiBeat;
    }
    /* 高亮色 picker / reset
       ⚠ R94：`input`（拖动中连发）保去抖；`change`（松手/关闭取色器）立即落盘收尾。 */
    const picker = $('fx-accent-picker');
    if (picker){
      picker.addEventListener('input', () => {
        uiAccent = picker.value || '';
        persistPrefs({ accent: uiAccent, theme: uiThemeKey });
        applyPrefsTheme();
      });
      picker.addEventListener('change', () => {
        uiAccent = picker.value || '';
        persistPrefs({ accent: uiAccent, theme: uiThemeKey }, true);
        applyPrefsTheme();
      });
    }
    const reset = $('fx-accent-reset');
    if (reset) reset.addEventListener('click', () => {
      uiAccent = '';
      persistPrefs({ accent: '', theme: uiThemeKey }, true);
      applyPrefsTheme();
      /* R66：取色器即时回到主题色，肉眼可见 → 不再 toast。 */
    });
    bindWallpaper();
  }

  /* ---- R57-6 · 左下按钮 hover 显现：驻留 15% 透明，靠近本体/热区即复原 ----
     齿轮(#settings-gear)与衣架(#theme-hanger)在左下角 dock 左外侧纵向排列。
     指针距任一按钮中心 < 72px（大于按钮半径 26 → 感应区比按钮大一圈）
     或正悬停其上 → 摘 .dim（opacity 1）；离开 → 加 .dim（15%）。
     避免按钮在低透明下点不中：pointer-events 恒 auto。 */
  function bindCornerButtons(){
    const btns = [$('settings-gear'), $('theme-hanger')].filter(Boolean);
    if (!btns.length) return;
    /* 初始即 dim（除非已 hover —— 见下方 mousemove 首帧） */
    btns.forEach(b => b.classList.add('dim'));
    document.addEventListener('mousemove', ev => {
      const mx = ev.clientX, my = ev.clientY;
      let near = false;
      for (const b of btns){
        const r = b.getBoundingClientRect();
        const cx = r.left + r.width/2, cy = r.top + r.height/2;
        const dist = Math.hypot(mx - cx, my - cy);
        const hitR = Math.max(36, r.width/2 + 22);   /* 比按钮大一圈 */
        if (dist <= hitR){ near = true; break; }
      }
      btns.forEach(b => b.classList.toggle('dim', !near));
    });
    document.addEventListener('mouseleave', () => btns.forEach(b => b.classList.add('dim')));
  }

  /* ---- 顶部搜索框（老 initSearch L5806-5920 移植） ---- */
