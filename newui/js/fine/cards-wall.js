/* newui/js/fine/cards-wall.js — 「弹簧卡片墙」自定义空间模块（源自 ui-cards.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'cards-wall',
  source: 'ui-cards.js',
  space: '弹簧卡片墙',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `/* ══════════════════════════════════════════════════════════
   ui-cards.js — Radio Intelligence 前端模块 · 卡片架 shelf/Spring/__cards · 推荐/曲库/歌单卡片渲染
   自 webapi/static/index.html 内嵌 <script> L2862-L3310 拆出（R58-7 #46）
   与原 index.html 内所有 ui-*.js 共享顶层作用域 → 加载顺序即执行顺序，勿打乱
   ══════════════════════════════════════════════════════════ */
  const CARD_W = 372, CARD_H = 120;
  const SHELF_CARD_HALF = CARD_W * 1.06 / 2;   // 中心卡半宽（含居中 1.06 放大）
  /* 相邻卡的圆心角（老 UI SHELF_ARC_STEP = 0.135，原样值）。
     相邻卡纵向间距 ≈ R_CARD × sin(ARC_STEP)，R_CARD≈808 → 约 109px，
     卡高 120 → 邻卡**边缘叠压约 11px**：斜向收进中心卡下方，配 z-index 递减
     + 景深模糊形成分层堆叠。调小=更密叠压更多，调大=弧更弯更松。
     ⚠ 旧值 0.30 是"居中弧心"那套的配套参数，同心弧下会稀疏到 239px（几乎不叠压）。 */
  const ARC_STEP = 0.135;
  /* 景深 Z：卡片沿 pz 后退会经 perspective(1500px) 投影把卡片**拉离真弧**，
     老 UI 实测半径从 677 漂到 624 → 压到 40，保留立体感又贴合弧线。 */
  const STEP_Z   = 40;
  const VISIBLE  = 5.6;    // 中心 ±5.6 张可见，其余隐藏（省渲染）
  /* ═══ shelfCenterX()（老 UI L4267-4280 原样移植）═══
     卡片右缘与「中间位 orbit 按钮左缘」留 **22px** 间隙 —— 这是老 UI 解决
     "卡片被 orbit 按钮压住 → 悬停命中按钮而非卡片" 的几何约束。
     ⚠ 绝不能读 #o-wander 的实时位置：切栏目时三按钮沿弧轮转，谁在中间会变，
       卡片架的水平基准跟着变 → 各栏目卡片不在同一条竖线上。
       必须走几何常量：中间位按钮左缘 = O.x - R1 - ORBIT_R。 */
  function shelfCenterX(){
    const z = orbitOC(), g = orbitGeo();
    const obLeft = z.x - g.R1 - ORBIT_R;
    const box = $('cards');
    const shelfW = (box && box.clientWidth) || innerWidth;
    const cx = Math.min(shelfW - 280, obLeft - 22 - SHELF_CARD_HALF);
    /* ⚠ R49-28：用户要求"卡片整体左移 10px" —— 中心 x 再减 10，
       卡片架整体（含弧心 R_CARD / 触发带 cardsInZone）都随 shelfCenterX 左移，
       保持同心几何一致（不破坏触发区与布局联动）。
       ⚠⚠ R57-8：用户要求"所有卡片架整体**再**左移 10px" —— 在 R49-28 的
       -10 基础上累计到 **-20**（任务 #38 原话）。中心卡更靠左，留出右侧呼吸。 */
    return Math.max(8 + SHELF_CARD_HALF, cx - 20);
  }

  /* 弹簧（阻尼振子）：原 UI 的 shelf.spring，用于卡片轮转的跟手回弹 */
  function Spring(p){ this.p = p||0; this.v = 0; this.target = this.p; }
  Spring.prototype.setTarget = function(t){ this.target = t; };
  Spring.prototype.update = function(dt){
    const k = 180, d = 26;                       // 刚度 / 阻尼
    const a = (this.target - this.p) * k - this.v * d;
    this.v += a * dt; this.p += this.v * dt;
    if (Math.abs(this.target - this.p) < 0.001 && Math.abs(this.v) < 0.01){
      this.p = this.target; this.v = 0;
    }
  };

  const spring = new Spring(0);
  let cards = [], cardItems = [], hoverIdx = -1, openAt = 0, lastT = 0, raf = 0;
  /* ⚠ 数据签名：轮询每 5s 会回调 buildCards，若无条件重建 DOM 会
       ① spring 被重置到 0 → "自动跳到第一个"
       ② 重放进场动画 → "反复刷新闪烁"
     只有条目 id / 当前曲**真正变化**时才重建。 */
  let lastSig = '';
  let lastCurId = null;
  /* ⚠⚠ 歌单条目**根本没有 id 字段**（后端 _pl_meta_to_dict 只给
     platform / ext_id / name / cover / creator / track_count）。
     旧签名一律取 e.id → 每个歌单都签成空串，只要**数量相同**签名就相同
     → buildCards 直接 return，切音源时**老卡片原封不动**
     （用户报的"点切换音源不会自动刷新，得把功能换掉才出现不同音源的歌单"）。 */
  function keyOf(e){
    if (!e) return '';
    if (curView === 'plist') return (e.platform || '') + ':' + (e.ext_id || '');
    return String(e.id == null ? '' : e.id);
  }
  function sigOf(list){
    return (list || []).map(e => keyOf(e) + (e && e.current ? ':c' : '')).join('|');
  }
  /* 切栏目 / 切音源 → 下一批数据到达时必须**复位到第一张**
     （老 UI 的 shelf 进出场语义）。只靠签名相同就 return 会停在旧位置。 */
  let pendingReset = false;

  /* 把当前播放的卡滚到中心（原 UI：切歌后卡片架跟随当前曲） */
  function scrollToCurrent(force){
    const i = cardItems.findIndex(e => e && e.current);
    if (i < 0) return;
    const id = cardItems[i] && cardItems[i].id;
    if (force || id !== lastCurId){ lastCurId = id; spring.setTarget(i); }
  }

  function esc(s){
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
      {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  const PAL = ['#7c6cff','#3ddc97','#ff9f6e','#6ec7ff','#c9a8ff','#ffd166'];

  function buildCards(entries){
    /* 签名相同**且**没要求复位 → 数据没变，直接返回
       （不动 DOM、不动 spring、不重放动画） */
    const sig = sigOf(entries);
    if (sig === lastSig && cards.length && !pendingReset) return;
    const isFirst = !cards.length;
    const reset = pendingReset || isFirst;   // 切栏目/切音源 = 复位，不是"保留位置"
    pendingReset = false;
    lastSig = sig;

    const box = $('cards');
    box.innerHTML = '';
    /* ⚠ 保留浏览位置（原 UI 的 keepPos 语义），否则续拉/刷新后弹回开头；
       但**切栏目/切音源必须归零**，否则新栏目的卡片停在旧栏目滚到的位置。 */
    const keepTarget = reset ? 0 : Math.max(0, Math.round(spring.target));
    if (reset){
      spring.p = 0; spring.v = 0; spring.target = 0;
      /* ⚠ 复位后必须把"当前曲"基准同步到**新栏目**的当前曲，否则下一次 5s 轮询
         会因为 lastCurId 变化又把卡片拽到当前曲 —— 等于复位白做。 */
      const cur0 = (entries || []).find(e => e && e.current);
      if (cur0) lastCurId = (cur0.id == null ? null : cur0.id);
    }
    cards = []; cardItems = entries || [];
    hoverIdx = -1;
    /* 进场错峰动画：首次 + 切栏目/切音源都播；**轮询刷新不重放**（避免反复闪烁） */
    openAt = reset ? performance.now() : 0;
    (entries || []).forEach((e, i) => {
      const el = document.createElement('div');
      const isPlist = (curView === 'plist');
      el.className = isPlist ? 'scard is-plist' : 'scard';
      /* ⚠ R49-29：current 卡**立即**落 .playing，不等 layout 帧。
         进场揭示期首帧 rev≤0 会 \`continue\` 跳过中心卡；offscreen/低帧率下
         rAF 可能迟迟不来 → 高亮延迟甚至永不落（用户要"最醒目"）。 */
      if (e && e.current) el.classList.add('playing');
      el.dataset.i = i;
      /* ⚠ 默认调色板先上（避免封面异步到达前 .sc-prog / ::before 都是黑灰），
         封面 img.onload 后会被 sampleColor 覆盖（cross-origin 失败时仍用 PAL）。 */
      const toneA = PAL[i % PAL.length];
      const toneB = PAL[(i + 2) % PAL.length];
      el.style.setProperty('--tone-a', toneA);
      el.style.setProperty('--tone-b', toneB);
      const name  = isPlist ? (e.name  || '未命名歌单') : (e.title || '未知曲目');
      const sub   = isPlist
        ? ((e.creator ? e.creator + ' · ' : '') + (e.track_count || 0) + ' 首')
        : (e.artist || '未知艺术家');
      const badge = isPlist
        ? (e.platform === 'qqmusic' ? 'QQ音乐' : e.platform === 'netease' ? '网易云' : '本地')
        : (e.reason || '');
      /* ⚠ 渐变底板**恒定**铺上，封面 img 叠在其上：
         否则一旦 URL 加载失败（坏图/防盗链 403/69 字节的残缺缓存），
         onerror 把 img 藏掉后就是一块**纯黑空白**（没有兜底色）。
         底板恒在 → img 失败自然露出程序化封面（与原 UI coverOfSong 的
         PALETTES 兜底同语义）。 */
      const bg = 'background:linear-gradient(140deg,'
        + PAL[i % PAL.length] + ',rgba(20,18,34,.9))';
      /* R47：行操作 + 渐变（取样）进度条 —— 与漫游卡片一致；
         plist 卡片是容器（点开看曲目），不挂行操作。 */
      /* R49：行操作补回「喜欢 ♥」（老 UI shelfActsHTML L4359：推荐卡=播放+喜欢+不喜欢+删除），
         点亮态来自后端 favorited（bridge._mark_fav 按 fav_key 填充）；plist 卡不挂行操作。 */
      const favOn = !!e.favorited;
      const acts = isPlist ? '' :
        '<div class="sc-acts">'
        + '<button data-act="play" title="播放"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg></button>'
        + '<button data-act="fav" title="喜欢（收藏到曲库）"' + (favOn ? ' class="on"' : '') + '><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20s-7-4.5-9.5-9A5.4 5.4 0 0 1 12 6.4 5.4 5.4 0 0 1 21.5 11C19 15.5 12 20 12 20z"/></svg></button>'
        + '<button data-act="dislike" title="不喜欢（AI 将避开）"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20s-7-4.5-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.5-7 10-7 10z"/><line x1="5" y1="5" x2="19" y2="19"/></svg></button>'
        + '<button data-act="delete" title="删除"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="6" y1="6" x2="18" y2="18"/><line x1="6" y1="18" x2="18" y2="6"/></svg></button>'
        + '</div>';
      const prog = isPlist ? '' :
        '<div class="sc-prog"><span class="fill"></span></div>';
      el.innerHTML =
        '<div class="sc-cover" style="' + bg + '">'
          + (e.cover ? '<img src="' + esc(e.cover) + '" alt="" '
                       + 'onerror="this.style.display=\\'none\\'">' : '') + '</div>'
        + '<div class="sc-info">'
        +   '<div class="sc-name">' + esc(name) + '</div>'
        +   '<div class="sc-sub">' + esc(sub) + '</div>'
        + '</div>'
        + (badge ? '<span class="sc-chip">' + esc(badge) + '</span>' : '')
        + '<div class="sc-eq"><i></i><i></i><i></i><i></i><i></i></div>'
        + acts + prog;
      /* 悬停浮起（原 UI 的 floatMix） */
      el.addEventListener('mouseenter', () => { hoverIdx = i; });
      el.addEventListener('mouseleave', () => { if (hoverIdx === i) hoverIdx = -1; });
      el.addEventListener('click', ev => {
        /* ⚠ R50：行操作按钮（play/fav/dislike/delete）优先 —— 事件冒泡**由内向外**，
           .scard 的监听**先于** #cards 容器委托执行；不在这里排除按钮，
           点喜欢/不喜欢会先触发"滚到中心/播放当前卡"（用户实测"点不喜欢变成播放"）。
           容器委托里的 stopPropagation 太晚 —— 内层监听那时已经跑完了。
           ⚠ 参数名必须避开外层 forEach 的条目 e（否则 e.id 变成 event.id）。 */
        if (ev.target && ev.target.closest && ev.target.closest('button[data-act]')) return;
        const d = Math.abs(i - spring.target);
        if (d >= 0.5){ spring.setTarget(i); return; }   // 非居中卡先滚到中心
        if (isPlist){
          /* 歌单卡 → 拉歌单详情曲目（QQ/网易在线歌单） */
          openPlaylist(e.platform, e.ext_id, e.name);
          return;
        }
        /* ⚠ 点播接口按栏目选：
           推荐/队列 在推荐货架里 → play_from_reco（快）
           历史/曲库 **不在货架里** → 必须回库查 Track 再 start_radio，
           用 play_from_reco 会找不到 → "曲库点播放没反应"（踩过）。 */
        const url = (curView === 'reco' || curView === 'queue')
          ? ('/api/reco/play?track_id=' + encodeURIComponent(e.id))
          : ('/api/play/track/' + encodeURIComponent(e.id));
        fetch(url, {method:'POST'})
          .then(() => setTimeout(loadCards, 400)).catch(() => {});
      });
      box.appendChild(el);
      cards.push({ el: el, lift: 0, vis: false, _z:'', _op:'', _tf:'', _filt:'' });
      /* ⚠ 取样封面颜色（drawImage 2×1 取两端主色）→ 覆盖默认调色板 →
         .sc-prog 的 --tone-a/--tone-b 与 .scard::before 的色洗随之变成封面色。
         跨域图会 SecurityError → 保留默认 PAL[i]。 */
      if (e.cover && !isPlist){
        const imgEl = el.querySelector('.sc-cover img');
        if (imgEl) sampleCoverColor(imgEl, (c1, c2) => {
          if (c1) el.style.setProperty('--tone-a', c1);
          if (c2) el.style.setProperty('--tone-b', c2);
        });
      }
    });
    if (!cards.length){
      box.innerHTML = '<div style="position:absolute;left:50%;top:50%;'
        + 'transform:translate(-50%,-50%);color:rgba(232,230,242,.45);font-size:13px">'
        + 'music has not arrived.</div>';
    }
    /* 恢复浏览位置（上限为末张）；首次构建则滚到**当前播放的卡** */
    const maxI = Math.max(0, cards.length - 1);
    spring.setTarget(Math.min(keepTarget, maxI));
    spring.p = spring.target;
    /* 定位到当前播放卡：
       ⚠ R49-29 曲库当前播放卡要"最醒目 + 自动定位" —— 用户切到曲库/推荐栏目
       时希望直接看到正在播的卡在中间，而不是停在第一张。
       · 首次构建（isFirst）：必滚当前曲；
       · 切栏目/切音源（reset）且**数据里确实有当前曲**：也滚到当前曲
         （此前刻意不滚是怕"切栏目弹走"，但用户要看的是正在播的那首，
         数据里没有 current 才保留第一张）;
       · 普通轮询刷新（!reset）：仅当 current id 变化才拽过去（scrollToCurrent(false)）。 */
    if (isFirst || (reset && (entries || []).some(e => e && e.current))){
      scrollToCurrent(true);
    } else if (!reset) scrollToCurrent(false);
    if (!raf) raf = requestAnimationFrame(layout);
    /* 进度条立即对齐到当前 position（卡片刚渲染完就显示对的位置） */
    updateCardProgress();
    /* ★★ 2026-11-17 修「副标题数字串栏目」：**数据到达后必须刷新栏目头**。
       此前 syncColumnHead() 只在 onOrbitModeChange()/ui-init 里调用，而 loadView()
       内部是 \`fetch().then()\` 异步 ⇒ 调用那一刻 cardItems 还是**上一个栏目**的数据：
         · 切到曲库 → 副标题显示上一栏（漫游）的数字；
         · 切到漫游 → 显示上一栏（可能歌单 limit=30）的数字。
       用户实测症状：曲库实际 84 首却显示「3 首歌曲」、漫游实际 5 首却显示「30 首推荐」。
       cardItems 已在本函数 :112 赋值为 entries ⇒ 此处才是刷新副标题的正确时机。
       ⚠ syncColumnHead 定义在 ui-column.js（更早加载的全局函数声明），可安全调用；
         仍用 typeof 兜底，避免加载顺序变化时报错。 */
    try { if (typeof syncColumnHead === 'function') syncColumnHead(); } catch (_) {}
  }
  /* 封面取色（drawImage 2×1 像素）—— 跨域图会被 SecurityError 吞掉，自动保留 PAL 兜底 */
  function sampleCoverColor(img, cb){
    function _try(){
      try {
        const cv = document.createElement('canvas');
        cv.width = 2; cv.height = 1;
        const ctx = cv.getContext('2d');
        ctx.drawImage(img, 0, 0, 2, 1);
        const d = ctx.getImageData(0, 0, 2, 1).data;
        const c1 = 'rgb(' + d[0] + ',' + d[1] + ',' + d[2] + ')';
        const c2 = 'rgb(' + d[4] + ',' + d[5] + ',' + d[6] + ')';
        cb(c1, c2);
      } catch (_) { cb(null, null); }
    }
    if (img.complete && img.naturalWidth) _try();
    else img.addEventListener('load', _try, { once: true });
  }
  /* 同步当前曲卡片的渐变进度条宽度（主循环每帧调） */
  function updateCardProgress(){
    if (!cards.length || !(st.duration > 0)) return;
    const pct = Math.max(0, Math.min(1, st.position / st.duration));
    const pctW = (pct * 100).toFixed(2) + '%';
    for (let i = 0; i < cards.length; i++){
      const c = cards[i];
      const e = cardItems[i];
      if (!c || !e) continue;
      const fill = c.el.querySelector('.sc-prog .fill');
      if (!fill) continue;
      if (e.current){
        if (c._pw !== pctW){ fill.style.width = pctW; c._pw = pctW; }
      } else if (c._pw !== '0%'){ fill.style.width = '0%'; c._pw = '0%'; }
    }
  }
  /* ═══ 卡片行操作事件委托（R47：play / dislike / delete）═══
     全部挂在 #cards 容器上 —— 避免每张卡都挂 listener，且 buildCards 重建也不丢事件。
     ⚠ 必须 e.stopPropagation()：否则点击按钮会冒泡到 .scard 的 click 处理 →
     又触发"播放当前卡"（用户只想要删除，点完就播了 → "怎么删了还播出来"）。
     ⚠ dislike key 用 (source || '') + ':' + ext_id，与 #d-dislike / 侧边面板保持一致
     （e.id 是本地 db id，可能与 dislike 表里的 ext_id 重复 → 重复删/加）。 */
  $('cards').addEventListener('click', e => {
    const btn = e.target.closest && e.target.closest('button[data-act]');
    if (!btn) return;
    e.stopPropagation();
    const cardEl = btn.closest('.scard');
    if (!cardEl) return;
    const i = +cardEl.dataset.i;
    const entry = cardItems[i];
    if (!entry) return;
    const act = btn.dataset.act;
    if (act === 'play'){
      const url = (curView === 'reco' || curView === 'queue')
        ? ('/api/reco/play?track_id=' + encodeURIComponent(entry.id))
        : ('/api/play/track/' + encodeURIComponent(entry.id));
      fetch(url, {method: 'POST'})
        .then(() => setTimeout(loadCards, 400)).catch(() => {});
    } else if (act === 'fav'){
      /* R49：喜欢/取消喜欢指定曲目 → /api/favorite/track（稳定键 source+ext_id / db id）。
         点亮瞬间放礼花（老 UI L4408 卡片 ♥ 同款）；失败回滚点亮态。
         ⚠ R72：卡片 ♥ 的本质是「把这首歌收进曲库」。条目上就有完整元数据
         （后端 _norm_entry 已带 title/artist/album/cover），必须**一并 POST** ——
         没播放过的在线曲在库里没有行，后端要靠这些字段 + source/ext_id 拼出
         占位 path 才能真入库；只传 source/ext_id/track_id 时后端落库读不到
         path → 红心点亮但曲库永远不出现（用户实测 bug）。R62 dislike 同款解法。 */
      const was = btn.classList.contains('on');
      const want = !was;
      btn.classList.toggle('on', want);
      if (want){ try { spawnBurst(btn); } catch (_) {} }
      const qs = 'source=' + encodeURIComponent(entry.source || '')
               + '&ext_id=' + encodeURIComponent(entry.ext_id != null ? entry.ext_id : '')
               + (entry.id != null ? '&track_id=' + encodeURIComponent(entry.id) : '')
               + '&title=' + encodeURIComponent(entry.title || '')
               + '&artist=' + encodeURIComponent(entry.artist || '')
               + '&album=' + encodeURIComponent(entry.album || '')
               + '&cover=' + encodeURIComponent(entry.cover || '');
      fetch('/api/favorite/track?' + qs, {method: 'POST'})
        .then(r => r.json())
        .then(d => {
          if (d && d.ok){ btn.classList.toggle('on', !!d.favorited); }
          else { btn.classList.toggle('on', was); toast((d && d.error) || '操作失败'); }
        })
        .catch(() => { btn.classList.toggle('on', was); });
    } else if (act === 'dislike'){
      /* ⚠ R50：key 规则必须与 dock/#d-dislike、radio.fav_key 一致 ——
         'e:<source>:<ext_id>'（在线）/ 'i:<db id>'（本地）。
         旧写法拼 'netease:xxx' / '123'（无前缀）→ 与后端判定键（fav_key）
         永不相等 → 卡片 dislike 永不生效（AI 不避雷、点亮态不同步）。
         ⚠ R62 #23：卡片上就有完整条目 —— 把 title/artist 一并 POST，
         在线歌落 dislike 库才有真实歌名（否则后端按 key 解析不到 Track →
         title/artist 为 NULL → 不喜欢列表显示「未知歌手/未知歌曲」）。 */
      let dkey = '';
      if (entry.source && String(entry.source) !== 'local'
          && entry.ext_id != null && entry.ext_id !== ''){
        dkey = 'e:' + entry.source + ':' + entry.ext_id;
      } else if (entry.id != null){
        dkey = 'i:' + entry.id;
      }
      if (!dkey) return;
      const on = !btn.classList.contains('on');
      btn.classList.toggle('on', on);
      const qs = 'key=' + encodeURIComponent(dkey)
               + '&title=' + encodeURIComponent(entry.title || '')
               + '&artist=' + encodeURIComponent(entry.artist || '');
      fetch('/api/dislike/' + (on ? 'add' : 'remove') + '?' + qs,
            {method: 'POST'})
        .catch(() => { btn.classList.toggle('on', !on); });
    } else if (act === 'delete'){
      const id = entry.id;
      if (id == null) return;
      let url = null;
      if      (curView === 'library')  url = '/api/library/delete?id=' + encodeURIComponent(id);
      else if (curView === 'reco')     url = '/api/reco/remove?track_id=' + encodeURIComponent(id);
      else if (curView === 'queue')    url = '/api/queue/remove?track_id=' + encodeURIComponent(id);
      else if (curView === 'history')  url = '/api/history/' + encodeURIComponent(id);
      else return;
      /* 乐观移除：滑出 + 淡出 → 失败时回滚；不在此 loadCards（要等动画走完才看着不刺眼） */
      const oldTf = cardEl.style.transform;
      const oldOp = cardEl.style.opacity;
      cardEl.style.transition = 'transform .25s ease, opacity .25s ease';
      cardEl.style.transform = (oldTf || '') + ' translateX(40px)';
      cardEl.style.opacity = '0';
      setTimeout(() => fetch(url, {method: 'POST'}).then(r => {
        if (!r.ok) throw new Error(r.status);
        setTimeout(loadCards, 220);
      }).catch(() => {
        cardEl.style.transition = '';
        cardEl.style.transform = oldTf;
        cardEl.style.opacity = oldOp;
      }), 60);
    }
  });

  /* 逐帧布局：极坐标 → 直角坐标，脏值比较后写 DOM（原 UI shelfTick） */
  function layout(now){
    raf = requestAnimationFrame(layout);
    if (!cards.length) return;
    const dt = Math.min(0.05, (now - lastT) / 1000 || 0.016);
    lastT = now;
    spring.update(dt);
    const center = spring.p;
    const box = $('cards');
    const W = box.clientWidth || 800, H = box.clientHeight || 400;
    /* ═══ 同心弧（老 UI L4680-4684 原样）：与 orbit **共用同一个弧心 O** ═══
       中心卡落在 (OX - R_CARD, OY) = shelfCenterX()，恰好贴住 orbit 按钮左侧 22px；
       δ<0 在上、δ>0 在下，|δ| 越大越往右收（弧的自然弯曲，不是直线堆叠）。
       ⚠ 坐标换算：#cards 是 inset:18px 0 132px，容器原点**不是**视口原点，
         半径在视口坐标系里算（与 shelfCenterX 同系），再减容器偏移画卡片。 */
    const oc = orbitOC();
    const R_CARD = Math.max(160, oc.x - shelfCenterX());
    const rb = box.getBoundingClientRect();
    const OX = oc.x - rb.left, OY = oc.y - rb.top;
    for (let i = 0; i < cards.length; i++){
      const c = cards[i], delta = i - center, absD = Math.abs(delta);
      if (absD > VISIBLE){
        if (c.vis){ c.el.style.visibility = 'hidden'; c.vis = false; }
        continue;
      }
      if (!c.vis){ c.el.style.visibility = 'visible'; c.vis = true; }
      /* 悬停浮起：不对称插值（进入快 0.20 / 退出慢 0.13） */
      const liftT = (hoverIdx === i) ? 1 : 0;
      c.lift += (liftT - c.lift) * (liftT > c.lift ? 0.20 : 0.13);
      /* 错峰揭示：距中心越远越晚出现，smoothstep 缓动 */
      let rev = 1;
      if (openAt){
        const raw = (now - openAt - absD * 70) / 420;
        rev = raw <= 0 ? 0 : raw >= 1 ? 1 : raw * raw * (3 - 2 * raw);
        if (rev <= 0){ c.el.style.opacity = 0; continue; }
      }
      const e = cardItems[i] || {};
      c.el.classList.toggle('center',  absD < 0.5);
      c.el.classList.toggle('playing', !!e.current);
      const scale0 = absD < 0.5 ? 1.06 : Math.max(0.62, 0.98 - absD * 0.12);
      const scale  = scale0 * (1 + c.lift * 0.06) * (0.6 + 0.4 * rev);
      const op     = (absD < 0.5 ? 1 : Math.max(0.25, 1 - absD * 0.28)) * rev;
      /* 景深模糊只给紧邻中央的（absD<3），上限 2px（全加会闪烁） */
      const blur   = (absD > 0.5 && absD < 3) ? Math.min(2, (absD - 0.5) * 0.7) : 0;
      const arcA   = delta * ARC_STEP;
      const px = OX - R_CARD * Math.cos(arcA) + c.lift * 8;
      const py = OY + R_CARD * Math.sin(arcA) - c.lift * 14;
      const pz = -absD * STEP_Z + c.lift * 36;
      const ry = Math.max(-4.5, Math.min(4.5, delta * 2.2)) - c.lift * 1.5;
      /* 脏值比较：静止时不写 DOM，避免无谓失效导致闪烁 */
      const zS = String(200 - Math.round(absD * 10));
      if (c._z !== zS){ c.el.style.zIndex = zS; c._z = zS; }
      const opS = op.toFixed(3);
      if (c._op !== opS){ c.el.style.opacity = opS; c._op = opS; }
      const filtS = blur > 0.05 ? 'blur(' + blur.toFixed(2) + 'px)' : 'none';
      if (c._filt !== filtS){ c.el.style.filter = filtS; c._filt = filtS; }
      const tfS = 'translate3d(' + (px - CARD_W/2).toFixed(1) + 'px,'
        + (py - CARD_H/2).toFixed(1) + 'px,' + pz.toFixed(1) + 'px) rotateY('
        + ry.toFixed(2) + 'deg) scale(' + scale.toFixed(4) + ')';
      if (c._tf !== tfS){ c.el.style.transform = tfS; c._tf = tfS; }
    }
  }

  /* 滚轮 / 方向键：轮转卡片（原 UI shelfStep） */
  function step(dir){
    if (!cards.length) return;
    const t = Math.round(spring.target);
    spring.setTarget(Math.max(0, Math.min(cards.length - 1, t + dir)));
  }
  const cardsBox = $('cards');
  /* ⚠ 这里**不要**绑 wheel：
     老 UI 的滚轮路由在 window 上用 elementFromPoint 精确判定
     （见上方「滚轮路由」块）。若在 #cards 容器上绑 preventDefault 的 wheel，
     会把落在 orbit 圆弧内部的滚轮也吞掉 → orbit 永远转不动。 */
  window.addEventListener('keydown', ev => {
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowRight') step(1);
    if (ev.key === 'ArrowUp'   || ev.key === 'ArrowLeft')  step(-1);
  });
  /* 自检钩子（与 window.__orbit 同性质）：验证套件据此断言
     "切栏目/切音源后卡片换成新数据且复位到第一张"。
     ⚠ 这里**不**暴露 layout()：它会无条件再排一个 rAF，手动调用会滚雪球；
        卡片是否居中由 spring.target 直接反映，不需要排帧。 */
  window.__cards = {
    state: function(){
      return {
        n: cards.length,
        target: spring.target,          // 弹簧目标位（0 = 第一张）
        view: (typeof curView === 'string') ? curView : '',
        first: cardItems[0] ? String(cardItems[0].name || cardItems[0].title || '') : ''
      };
    }
  };

  /* ---------- 栏目切换：推荐 / 队列 / 历史 / 曲库 / 歌单 ---------- */
  const VIEWS = {
    /* ⚠ 推荐页**固定**读推荐货架（/api/recommendations 后端已改为只读
       get_reco_entries），绝不会被播放队列顶替。播放队列是独立入口 queue。 */
    reco:    '/api/recommendations',
    queue:   '/api/queue',
    history: '/api/history',
    library: '/api/library',
    /* 歌单：QQ音乐 / 网易云在线歌单，返回 items（不是 entries），
       且带音源参数（plistSource）。 */
    plist:   '/api/playlists',
  };
`;
    const __epi = `  for (const __n of ["CARD_W", "SHELF_CARD_HALF", "ARC_STEP", "STEP_Z", "VISIBLE", "shelfCenterX", "Spring", "spring", "cards", "lastSig", "lastCurId", "keyOf", "sigOf", "pendingReset", "scrollToCurrent", "esc", "PAL", "buildCards", "sampleCoverColor", "updateCardProgress", "layout", "step", "cardsBox", "VIEWS"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
