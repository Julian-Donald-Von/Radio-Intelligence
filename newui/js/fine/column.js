/* newui/js/fine/column.js — 「栏目头/漫游滑块」自定义空间模块（源自 ui-column.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'column',
  source: 'ui-column.js',
  space: '栏目头/漫游滑块',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `/* ══════════════════════════════════════════════════════════
   ui-column.js — Radio Intelligence 前端模块 · 栏目头 COL_META/syncColumnHead · ORBIT_API · rs 熟悉度滑块 · 播放全部/音源切换
   自 webapi/static/index.html 内嵌 <script> L2457-L2612 拆出（R58-7 #46）
   与原 index.html 内所有 ui-*.js 共享顶层作用域 → 加载顺序即执行顺序，勿打乱
   ══════════════════════════════════════════════════════════ */
  const COL_META = {
    wander: { kicker: 'RADIO · 漫游',    title: '星际漫游' },
    fav:    { kicker: 'LIBRARY · 曲库',  title: '我的曲库' },
    plist:  { kicker: 'PLAYLISTS · 歌单', title: '歌单中心' },
  };
  const modeName = m => m === 'wander' ? '漫游' : m === 'fav' ? '曲库' : '歌单';
  function syncColumnHead(){
    const meta = COL_META[orbitActive];
    if (!meta) return;
    const head = $('column-head');
    head.classList.remove('swap'); void head.offsetWidth; head.classList.add('swap');
    $('ch-kicker').textContent = meta.kicker;
    $('ch-title').textContent  = meta.title;
    /* 副标题显示**当前栏目的卡片数**（不是待播队列长度）。
       ⚠ 2026-11-17 修文案错接：R44 时代定的单位规则「歌单 N 个歌单，其余待播 N 首」
       在 R45 纠正数据源映射（fav 从 queue→library、plist→library→playlists）后没同步改，
       于是「曲库」显示「待播 N 首」（其实是曲库总数）、「漫游」显示「待播 N 首」
       （其实是推荐货架数）——把栏目卡片数谎称成待播队列。数据源本身是对的，只错文案。
       改为各栏目真实单位，不再出现「待播」二字（待播队列是独立 /api/queue 入口）。
       ⚠ cardItems 由后文 let 声明：若在声明前调用本函数会撞 TDZ，
       且 **typeof 对 TDZ 中的 let/const 变量同样抛 ReferenceError**（不是返回 undefined）。
       必须用 try/catch 兜住。 */
    let n = 0;
    try { n = Array.isArray(cardItems) ? cardItems.length : 0; } catch(_) { n = 0; }
    const SUB_UNIT = {
      wander: '首推荐',   // 推荐货架数（/api/recommendations）
      fav:    '首歌曲',   // 曲库曲目数（/api/library），不是收藏、不是待播
      plist:  '个歌单',   // 在线歌单数（/api/playlists）
    };
    $('ch-sub').textContent = n + ' ' + (SUB_UNIT[orbitActive] || '首');
    /* 音源切换 tabs：仅 plist（歌单）栏目显示 —— 歌单才有音源概念 */
    const ps = $('plist-src');
    if (ps) ps.style.display = (orbitActive === 'plist') ? 'flex' : 'none';
    /* 播放全部按钮：wander/fav/plist 都有意义 ——
       ⚠ R72：歌单栏目也显示。语义 = 播放**当前居中那张歌单卡**对应的歌单内容
       （用户拍板：「当前卡片对应歌单内容入队」，等价于进详情点播放全部，但不展开面板）。
       只有歌单卡片是"容器"（一卡一歌单），漫游/曲库才是整列数据源。
       按钮文案/title 随栏目切换：plist 用「播放歌单」避免误读成"全部歌单都播"。 */
    const pa = $('col-playall');
    if (pa){
      const isPlist = (orbitActive === 'plist');
      pa.style.display = (orbitActive === 'wander' || orbitActive === 'fav' || isPlist) ? '' : 'none';
      const tx = pa.querySelector('.pmc-tx');
      if (tx) tx.textContent = isPlist ? '播放歌单' : '播放全部';
      pa.title = isPlist ? '播放当前歌单全部曲目' : '播放全部';
    }
  }

  /* ═══════════ 切栏目 → 刷新内容（老 UI refreshColumn 同语义） ═══════════
     R45 正确语义（用户指定）：
       wander = 漫游/推荐  → /api/recommendations
       fav    = 曲库       → /api/library      （不是收藏！favorites 已废弃）
       plist  = 歌单       → /api/playlists    （QQ音乐 / 网易云在线歌单） */
  const ORBIT_API = { wander:'reco', fav:'library', plist:'plist' };
  function onOrbitModeChange(mode){
    curView = ORBIT_API[mode] || 'reco';
    syncColumnHead();
    loadView({ reset: true });
    toast('已切换到「' + modeName(mode) + '」');
    /* R49-30：orbit 落到栏目（轮转/点击）→ 左侧提示闪示约 3s 后消失 */
    try { columnFlashOnMode(); } catch (_) {}
    /* R49-31：漫游滑块随栏目显隐 —— 仅 wander(漫游) 栏目显示 */
    try { syncRecMode(); } catch (_) {}
  }

  /* ═══ R49-31 漫游滑块（移植老 UI L3023-3070）═══
     仅「漫游」栏目显示；切到漫游闪现 ~5s，鼠标靠近滑块常显、离开淡出。
     档位 0..3 → /api/roam/familiarity POST（radio.set_familiarity）。 */
  const RS_LABELS = ['更熟悉', '略熟悉', '略陌生', '更陌生'];
  const rsSlider = $('rs-slider'), rsVal = $('rs-val'), rsBox = $('rec-style');
  let rsTimer = 0, rsHovering = false, rsAutoPending = false;
  function rsShow(){ if (rsBox) rsBox.classList.add('visible'); }
  function rsHide(){ if (rsBox) rsBox.classList.remove('visible'); }
  function rsAutoShow(){
    if (!rsBox) return;
    rsShow();
    clearTimeout(rsTimer); rsTimer = 0;
    rsAutoPending = true;
    rsTimer = setTimeout(() => {
      rsTimer = 0; rsAutoPending = false;
      if (!rsHovering) rsHide();
    }, 5000);
  }
  function syncRecMode(){
    if (!rsBox) return;
    const on = orbitActive === 'wander';
    rsBox.classList.toggle('on', on);
    if (on) rsAutoShow();
    else { rsHide(); clearTimeout(rsTimer); rsTimer = 0; rsHovering = false; rsAutoPending = false; }
  }
  /* 初始化：本地缓存立即回填（零闪烁，R57-4「刷新后先默认后跳回」根因：GET 是
     异步的，fetch 回来前滑块显示 HTML 默认 value=1 → 先默认后跳记忆值）。
     三通道：localStorage 立即首帧 → /api/roam/familiarity 后端权威 → /api/settings
     user_config.rec_pref 校正（跨设备/重启的最终持久化层）。 */
  (function initRs(){
    /* ① localStorage 同步回填 —— 不等网络，首帧即上次档位 */
    try {
      const f = JSON.parse(localStorage.getItem('r54.ui') || '{}');
      if (f && typeof f.roamFam === 'number' && rsSlider){
        const v = Math.max(0, Math.min(3, f.roamFam | 0));
        rsSlider.value = v;
        if (rsVal) rsVal.textContent = RS_LABELS[v];
      }
    } catch (_) {}
    /* ② /api/roam/familiarity 内存态校正（同时反映给后端算法用） */
    fetch('/api/roam/familiarity').then(r => r.json()).then(d => {
      if (d && d.ok && rsSlider){
        const v = Math.max(0, Math.min(3, +(d.value || 1)));
        rsSlider.value = v;
        if (rsVal) rsVal.textContent = (d.labels && d.labels[v]) || RS_LABELS[v];
        try {
          const f = JSON.parse(localStorage.getItem('r54.ui') || '{}');
          f.roamFam = v; localStorage.setItem('r54.ui', JSON.stringify(f));
        } catch (_) {}
      }
    }).catch(() => {});
    /* ③ R67-3：user_config.rec_pref 跨重启持久化层校正（设置卡里 rec_pref
       控件已删，这里就是它的唯一读写入口）。后端启动时把 rec_pref 同步到
       radio.roam_familiarity，启动 = 上次滑块位置。 */
    fetch('/api/settings').then(r => r.json()).then(d => {
      if (!d || !d.ok) return;
      const v = d.values && typeof d.values.rec_pref === 'number' ? d.values.rec_pref : null;
      if (v == null || !rsSlider) return;
      const clamped = Math.max(0, Math.min(3, v | 0));
      rsSlider.value = clamped;
      if (rsVal) rsVal.textContent = RS_LABELS[clamped];
      try {
        const f = JSON.parse(localStorage.getItem('r54.ui') || '{}');
        f.roamFam = clamped; localStorage.setItem('r54.ui', JSON.stringify(f));
      } catch (_) {}
    }).catch(() => {});
  })();
  if (rsSlider){
    rsSlider.addEventListener('input', () => {
      const v = +rsSlider.value;
      if (rsVal) rsVal.textContent = RS_LABELS[v];
      try {
        const f = JSON.parse(localStorage.getItem('r54.ui') || '{}');
        f.roamFam = v; localStorage.setItem('r54.ui', JSON.stringify(f));
      } catch (_) {}
      fetch('/api/roam/familiarity?level=' + v, { method:'POST' }).catch(() => {});
      /* R67-3：同步写 user_config.rec_pref —— 跨重启持久化（设置卡里 rec_pref 控件已删） */
      fetch('/api/settings/set?path=rec_pref&value=' + encodeURIComponent(String(v)),
            { method: 'POST' }).catch(() => {});
    });
  }
  window.addEventListener('mousemove', e => {
    if (!rsBox || !rsBox.classList.contains('on')) return;
    const r = rsBox.getBoundingClientRect();
    const inZone = e.clientX >= r.left - 8 && e.clientX <= r.right + 8
                && e.clientY >= r.top - 8 && e.clientY <= r.bottom;
    rsHovering = inZone;
    if (inZone){ clearTimeout(rsTimer); rsTimer = 0; rsAutoPending = false; rsShow(); }
    else if (!rsAutoPending) rsHide();
  });
  /* ═══ 播放全部：推荐与曲库是**两个不同入口**（R46-5 用户点名别弄混）═══
       wander(漫游/推荐) → /api/reco/play-all     数据源 = 推荐货架
       fav(曲库)         → /api/library/play-all  数据源 = 本地曲库
     两者在后端都走「替换播放队列 + 从第一首开播」语义（upcoming 整体重写，
     不是追加）。⚠ 旧实现**恒定**打 reco → 在曲库栏目点播放全部播的是推荐。

     ⚠ R72：歌单栏目也接入 —— 用户拍板语义 = 「当前卡片对应歌单内容入队」。
     歌单卡片是"一卡一歌单"的容器，卡片架当前居中位（spring.target 取整）
     对应的就是用户正在看的那张歌单 → 走 /api/playlists/play 整单入队
     （后端 play_playlist 内部 play_tracks + 从第 index 首开播，index=0 从第一首）。 */
  const PLAYALL_API = { wander: '/api/reco/play-all', fav: '/api/library/play-all' };
  $('col-playall').addEventListener('click', () => {
    if (orbitActive === 'plist'){
      /* 取卡片架当前居中卡 = 用户当前聚焦的歌单卡（spring.target 取整） */
      let pl = null;
      try {
        const i = Math.max(0, Math.min(cardItems.length - 1, Math.round(spring.target)));
        const e = cardItems[i];
        if (e && e.platform && e.ext_id != null) pl = e;
      } catch (_) { pl = null; }
      if (!pl){
        toast('请先选中一个歌单');
        return;
      }
      toast('正在播放歌单…');
      fetch('/api/playlists/play?platform=' + encodeURIComponent(pl.platform)
            + '&ext_id=' + encodeURIComponent(pl.ext_id) + '&index=0', { method: 'POST' })
        .then(r => r.json()).then(d => {
          if (d && d.ok){ setTimeout(poll, 260); }
          else toast('播放失败：' + ((d && d.error) || ''));
        }).catch(() => toast('播放失败'));
      return;
    }
    const url = PLAYALL_API[orbitActive];
    if (!url) return;                       // 其它无意义栏目隐藏
    fetch(url, { method: 'POST' })
      .then(r => r.json())
      .then(d => {
        if (!d || !d.ok) return;
        /* 队列已被整体替换 → 立刻拉一次播放状态，让进度条/卡片对上新曲 */
        setTimeout(poll, 260);
        setTimeout(() => loadView({ reset: true }), 420);
      })
      .catch(() => {});
  });
  /* 音源切换 tabs（仅 plist 栏目展示）：切换后按音源重新拉歌单 */
  $('plist-src').querySelectorAll('button').forEach(btn => {
    btn.addEventListener('click', () => {
      $('plist-src').querySelectorAll('button').forEach(b => b.classList.remove('on'));
      btn.classList.add('on');
      plistSource = btn.dataset.s || '';
      /* ⚠ 必须带 reset：否则签名判定/位置保留会让老卡片原封不动 */
      if (curView === 'plist') loadView({ reset: true });
      toast(plistSource
        ? ('歌单音源：' + (plistSource === 'qqmusic' ? 'QQ音乐' : '网易云'))
        : '歌单音源：全部');
    });
  });

  /* ═══════════════════════════════════════════════════════════════
     侧边面板（#side-panel）—— 队列/历史/曲库 三 tab + 行操作
     原 ui/ai-radio-player.html L533-577 / L3130-3333
     ═══════════════════════════════════════════════════════════════ */
`;
    const __epi = `  for (const __n of ["COL_META", "modeName", "syncColumnHead", "ORBIT_API", "onOrbitModeChange", "RS_LABELS", "rsSlider", "rsTimer", "rsShow", "rsHide", "rsAutoShow", "syncRecMode", "PLAYALL_API"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
