/* newui/js/fine/state-apply.js — 「applyState/歌词/toast」自定义空间模块（源自 ui-state.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'state-apply',
  source: 'ui-state.js',
  space: 'applyState/歌词/toast',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `/* ══════════════════════════════════════════════════════════
   ui-state.js — Radio Intelligence 前端模块 · applyState · AI 口播 aibar/thinking · toast · 歌词 loadLyrics
   自 webapi/static/index.html 内嵌 <script> L1632-L1808 拆出（R58-7 #46）
   与原 index.html 内所有 ui-*.js 共享顶层作用域 → 加载顺序即执行顺序，勿打乱
   ══════════════════════════════════════════════════════════ */

  /* ═══ R95d-Q1：换曲宽限（轮询误报 \`playing=false\` 的兜底） ═══
     根因（\`docs/water_physics_research.md\` §10.1）：切歌装载窗内后端引擎瞬时 STOPPED，
     2s 轮询 \`/api/state\` 会灌进 \`playing=false\` ⇒ \`calm\` 0.28s 冲顶、h 场 0.5s 掉 79%
     （"水波像被集体清掉"）。而**切歌路径从不发 WS \`playing=false\`**（只有用户真暂停
     才发）⇒ 宽限期内"轮询说停"一律不信；"WS 说停"才是权威、立即解除宽限。
     Q0（后端报意图）已掐掉源头，这里是**纯前端保险**（后端未升级/降级路径也安全）。 */
  const PLAY_GRACE_MS = 6000;
  let _playGraceUntil = 0;
  function armPlayGrace(ms){
    _playGraceUntil = performance.now() + (typeof ms === 'number' ? ms : PLAY_GRACE_MS);
    return _playGraceUntil;
  }
  function clearPlayGrace(){ _playGraceUntil = 0; }
  function inPlayGrace(){ return performance.now() < _playGraceUntil; }
  /* 黑盒验证钩（offscreen 断言用，只读 + 手动 arm/clear） */
  window.__playGrace = {
    ms:  function(){ return PLAY_GRACE_MS; },
    arm: function(ms){ return armPlayGrace(ms); },
    clear: function(){ clearPlayGrace(); return 0; },
    in:  function(){ return inPlayGrace(); },
    left: function(){ return Math.max(0, _playGraceUntil - performance.now()); }
  };

  function applyState(d){
    if (!d) return;
    /* ⚠⚠ prevTitle 必须在**任何** st.title 写入之前抓取（R47「切歌了歌词还是上一首」的根因）：
       下面既有 \`d.title\` 直写、又有 \`d.current.title\` 兜底写入，
       若在这两处之后才取 prevTitle，取到的已经是新标题 → 判定永远"没切歌" → 永不重新取词。 */
    const prevTitle = st.title;
    const prevPlaying = !!st.playing;
    if (typeof d.ready    === 'boolean') st.ready   = d.ready;
    /* ⚠ R95d-Q1：换曲宽限 —— 只**吞掉**轮询里的 false，从不吞 true。
       宽限内 \`st.playing\` 保持原值（真值），WS 'playing' 事件一到立即解除宽限。 */
    if (typeof d.playing  === 'boolean'){
      if (d.playing){ clearPlayGrace(); st.playing = true; }
      else if (!inPlayGrace()) st.playing = false;
    }
    adoptRemotePosition(d.position);
    /* 歌词随播放进度推进当前行 */
    try { if (window.Poster) window.Poster.seekLyric(st.position); } catch (_) {}
    if (typeof d.duration === 'number')  st.duration = d.duration;
    if (d.title  != null) st.title  = d.title;
    if (d.artist != null) st.artist = d.artist;
    if (typeof d.mode === 'string' && d.mode){
      const i = MODES.findIndex(m => m.key === d.mode);
      if (i >= 0) modeIdx = i;
    }
    const cur = d.current || {};
    /* ⚠ R49：必须把整包 current 存下 —— dock 的不喜欢/喜欢按钮靠它算
       fav_key（st.current 此前从未被赋值，点红心永远"当前无曲目"）。 */
    if (d.current) st.current = d.current;
    if (typeof d.favorited === 'boolean') st.fav = d.favorited;
    /* ⚠ R49-27：不喜欢状态以**后端为权威**，切歌后必须重置 —— 旧实现只在
       用户点击时 toggle，上一首点亮的不喜欢会带到下一首（下一首明明没标
       不喜欢，dock 斜杠红心却亮着）。get_state 现在带 disliked，轮询/初始化
       自动把 #d-dislike 同步成当前曲真实状态（新曲不是 disliked → 熄灭）。 */
    if (typeof d.disliked === 'boolean'){
      st.disliked = d.disliked;
      $('d-dislike').classList.toggle('on', d.disliked);
    }
    st.title  = cur.title  || st.title;
    st.artist = cur.artist || st.artist;
    if (cur.cover) st.cover = cur.cover;
    /* 切歌 → 重新取词（老 UI L4844：切歌即加载，不被 lyricOn 门控） */
    if (cur.title && cur.title !== prevTitle){
      try { loadLyrics(true); } catch (_) {}
      /* ★ 2026-11-16：自动切歌排水兜底。排水原本只靠 WS 'track_changed'
         （ui-view.js:132 → setMusicEvents(null)）；自动路径的并发守卫（_advance
         非阻塞锁 / 世代 seq）偶尔会吞掉那次 emit ⇒ 前端收不到 ⇒ 旧歌水波不排、
         机械堆叠。这里复用已存在的标题变化判定，在 2s 轮询里补一次排水：
         · prevTitle 非空守卫 → 初始化不误触发；
         · WS 正常到达时 title 已被更新 → 此处 prevTitle===cur.title，天然去重。 */
      if (prevTitle){
        try {
          if (window.__water && window.__water.setMusicEvents){
            window.__water.setMusicEvents(null);
          }
        } catch (_e) {}
      }
    }
    /* #d-radio / #d-ai 点亮态以**后端为权威**（初始化与轮询都同步） */
    if (typeof d.roam_on === 'boolean'){
      st.roamOn = d.roam_on;
      $('d-radio').classList.toggle('on', d.roam_on);
    }
    if (typeof d.dj_on   === 'boolean'){
      st.djOn = d.dj_on;
      $('d-ai').classList.toggle('on', d.dj_on);
      /* ★ R119-7：伴听关闭 ⇒ 圈圈恒熄（否则会留一个永远转的圈） */
      if (!d.dj_on) { try { setAIWaiting(false); } catch (_) {} }
    }
    /* R58-1 (#41)：AI 伴听关闭 → 口播栏立即淡出（原 UI setAI(false)）。
       ⚠ applyState 从 2s 轮询来，本身就带后端权威 dj_on —— 这里**无条件**
       收口口播栏（开：常驻显示，不依赖鼠标靠近/新文案；关：hide）。 */
    if (d.dj_on === true){
      /* R59：打开 = **立即常驻** —— 摘下 hide + 加 show（不再等鼠标靠近 /
         WS 文案才显形）；文字保留上一句，broadcast 轮询 3s 内会更新。 */
      showAIBar();
    } else if (d.dj_on === false){
      hideAIBar();
    }
    /* ⚠ R49-30：开始播放 → 空闲态"恒显"的栏目提示淡出（之后只随 orbit
       切栏目闪示 3s）；若当前显示是 orbit 切换触发的 3s 闪示则保留由计时器收走。 */
    if (d.playing === true && !prevPlaying){ try { hideColumnIfIdle(); } catch (_) {} }
    render();  }

  /* AI 口播栏：统一写入口（带 swap 换字动画 + 自动展开）。
     与 WS dj_speech / 轮询 loadBroadcast 共用，避免两处各写一份文案逻辑。
     原 UI L5485-5490：dj_speech 到达时 textContent 更新 + swap 重放 + 自动展开。 */
  /* ⚠⚠ R53 outro 展示修复：
     口播有两条到达路径 —— ① 轮询 /api/dj/broadcast（当前曲 intro）；
     ② WS dj_speech（**intro 与 outro 都走这条**：intro = 新歌开场导听，
     outro = 旧歌收尾的转场告别，见 ai/transition.py _begin_transition emit）。
     bug：outro 经 WS 显示后，≤3s 就被 loadBroadcast 轮询拉回的 current_broadcast
     （仍是当前曲 intro）覆盖 → 用户永远看不到 outro。
     修复：WS 刚推送过 dj_speech 的 3s 内，轮询**不得覆盖**文案 ——
     outro 得以停留到切歌；切歌后 track_changed/poll 自然换新歌 intro。 */
  let lastDjAt = 0;
  /* ★ R119-7 · A：**WS 推送锁定到下一次换歌**（取代"3 秒时间窗"）。
     为什么 3 秒窗不够（用户 2026-09-21 报的真 bug）：
       转场(outro)在**上一首结尾**播，那一刻 \`current_broadcast\` 仍是**这首歌的 intro**；
       3 秒窗一过轮询就把 outro 覆盖回同曲 intro
       ⇒ 用户看到"显示一会 outro 就一下子跳回该首的 intro"。
     新规则：WS 推送过文案 ⇒ 钉住，**只由换歌（track_changed）解锁**。
     轮询仍保留它原本的作用（页面刷新 / WS 掉线后的兜底回填，那时 hold 为假）。 */
  let djHold = false;
  function clearDjHold(){ djHold = false; }

  /* ★ R119-7 · B：口播栏「加载中」圈圈 = **本曲口播还没到位**（不是"正在朗读"）。
     旧行为为什么看不见：圈圈只由后端 \`thinking\` 事件驱动，而 (a) \`_speak_intro\`
     是**先发 thinking=False、再去播音频**，(b) 口播现在**预生成**（歌一开播文案
     就已就绪）⇒ True→False 只隔几毫秒。
     新行为：换曲即亮「等本曲口播」，文案一到达就熄；后端 thinking 仍可覆盖。
     ⚠ 兜底：45s 自动熄（口播彻底失败时不留一个永远转的圈）。 */
  let aiWaitTimer = 0;
  function setAIWaiting(on){
    const tb = $('ai-bar-think');
    if (!tb) return;
    /* ★ 2026-11-17（用户要求）：口播栏**不再显示"加载中"转圈**。
       理由：兜底音频/文案现在都是即时的（预生成 + 落盘），这个圈没有任何
       信息量，亮着反而丑。事件仍然收，只是不再点亮。 */
    clearTimeout(aiWaitTimer);
    aiWaitTimer = 0;
    tb.classList.remove('on');
    return;
    /* 以下为旧实现，保留作参考（永不执行） */
    if (on && st.djOn){
      tb.classList.add('on');
      clearTimeout(aiWaitTimer);
      aiWaitTimer = setTimeout(function(){ tb.classList.remove('on'); }, 45000);
    } else {
      clearTimeout(aiWaitTimer);
      aiWaitTimer = 0;
      tb.classList.remove('on');
    }
  }
  /* ═══ R55：口播栏锚到左侧栏目提示（column-panel）正上方 ═══
     口播栏与栏目提示同处左中，两元素都是 fixed；若各写死 top 值，
     一旦 column-panel 高度/位置变化就会互相叠压或离太远。
     这里读 column-panel 真实包围盒，把口播栏底边贴到它顶边上方 14px、
     左缘对齐 —— 无论栏目提示多高都精确"在其正上方"。
     口播栏本身 .show 前是不可见（opacity:0），锚定不产生跳变。

     ═══ R57-7：水平**固定**，不再实时追 cr.left ═══
     用户原话：「口播栏有时左右跳动，固定住，让它最左侧与 column-panel
     展开后的最左侧平齐」。
     根因：column-panel 收起/展开动画走 translateX(-26px↔0)（见 #column-panel
     CSS transform），旧实现 bar.style.left = cr.left 会把 transform 位移量
     （收起时 -26px → cr.left≈22px；展开后 →48px）实时写进口播栏 = 左右乱跳。
     修复：取 column-panel 的 **computed 静态 left（不含 transform）** =
     它展开后（translateX(0)）的最左侧 48px，据此固定。column-panel 的
     translateY(-50%) 是恒定垂直居中、动画不改高度 → cr.top 稳定，
     垂直方向保留实时锚（仅内容高度变化时微调，不会跳）。 */
  function positionAIBar(){
    const bar = $('aibar'), cp = $('column-panel');
    if (!bar || !cp) return;
    const cr = cp.getBoundingClientRect();
    if (!cr.height && !cr.width) return;   // 尚未布局，等下次调用
    const vh = window.innerHeight;
    /* 展开态最左侧 = computed left（静态定位值，transform 动画不改它） */
    const cssLeft = parseFloat(getComputedStyle(cp).left);
    bar.style.left = (Number.isFinite(cssLeft) ? cssLeft : 48) + 'px';
    /* fixed 的 bottom = 视口高 − 目标上缘；目标上缘 = 面板顶 − 间距 */
    const gap = 14;
    bar.style.top = 'auto';
    bar.style.bottom = Math.max(10, Math.round(vh - (cr.top - gap))) + 'px';
    bar.style.transform = '';
    bar.style.maxWidth = 'min(430px, 62vw)';
  }
  /* ═══ R59：口播栏「开即常驻」统一入口 ═══
     摘下 hide（若有）—— 打开 AI 伴听后面板**常驻显示**，不再需要鼠标靠近
     才出现（用户点名）。
     ⚠ 显形条件：ai-text 已有内容才直接 add show；空栏（启动初期尚无任何
     口播文案）保持隐藏态，等首条文案（setAIBarText 保底 add show）自然亮起，
     避免启动时左上角飘一张空玻璃卡。 */
  function showAIBar(){
    const bar = $('aibar');
    if (!bar) return;
    hideToken = 0;                 // 清除在途关闭收尾，防止旧 hide 尾巴把 show 摘掉
    bar.classList.remove('hide');
    const tx = $('ai-text');
    if (tx && String(tx.textContent || '').trim()){
      bar.classList.add('show');
      try { positionAIBar(); } catch (_) {}
    }
  }
  /* 文案变化 / 窗口缩放 / 面板显隐都重锚（R55：column-panel 靠近弹出时也要跟随） */
  window.addEventListener('resize', function(){
    try { positionAIBar(); } catch(_){}
    /* R62 #7：toast 固定锚随窗口高度变化（dock 底部 24px 是视口相对值） */
    const tv = $('toast');
    if (tv && tv.classList.contains('show')) { try { positionToast(); } catch(_){} }
  });
  function setAIBarText(text, viaWs){
    const bar = $('aibar'), tx = $('ai-text');
    if (!bar || !tx) return;
    if (viaWs){
      lastDjAt = Date.now();
      djHold = true;      // ★ R119-7：WS 文案钉到下一次换歌（轮询不得回填覆盖）
    }
    tx.classList.remove('swap'); void tx.offsetWidth; tx.classList.add('swap');
    tx.textContent = text;
    try { setAIWaiting(false); } catch (_) {}   // ★ R119-7：文案到了 ⇒ 熄「加载中」圈
    /* R59：有文案即常驻显形（移除时不再重复弹 show，setAIBarText 不再自动 add show
       以外的隐藏态管理 —— 显隐只由 showAIBar/hideAIBar 负责；此处保底 show 一次
       防止"开伴听后首条文案到达但栏仍是初始隐藏态"。 */
    if (!bar.classList.contains('show')) bar.classList.add('show');
    bar.classList.remove('hide');
    try { positionAIBar(); } catch (_) {}
  }
  /* 关闭 AI 伴听 → 口播栏淡出（原 UI setAI(false)：add hide 后 400ms 摘 show）。
     ⚠ R58-1 (#41)：hide 是异步收尾 —— 用 hideToken 快照，400ms 内若重新打开
     （摘 hide + 加 show），到点的旧定时器**不得**把 show 摘掉（否则开完又被
     上一轮关闭的尾巴藏起来，表现为"打开不显示"）。 */
  let hideToken = 0;
  function hideAIBar(){
    const bar = $('aibar'); if (!bar) return;
    const myTok = ++hideToken;
    bar.classList.add('hide');
    setTimeout(() => {
      if (myTok !== hideToken) return;   // 期间被重新打开过 → 放弃收尾
      bar.classList.remove('show');
    }, 400);
  }

  /* AI 口播文案：轮询后端 current_broadcast，有则淡入显示（WS dj_speech 兜底）。
     ⚠ R53：轮询到但刚被 WS dj_speech 推送过（3s 内）→ 跳过，
     否则 outro（WS 推送）会被 current_broadcast（intro）立即覆盖。
     ⚠ R58-1 (#41)：AI 伴听关闭（st.djOn=false）时**不拉也不显示**——
     后端 dj_on 只门控音频输出、文案仍"始终生成"，current_broadcast 残留
     旧文案；不 gate 的话用户关掉伴听后口播栏还会被 3s 轮询再次弹出。 */
  function loadBroadcast(){
    if (!st.djOn) return;
    /* ★ R119-7：从"3 秒时间窗"改为"WS 钉住直到换歌"—— 时间窗过短会被
       同曲 intro 覆盖（见 setAIWaiting/djHold 处注释）。 */
    if (djHold) return;
    fetch('/api/dj/broadcast').then(r => r.json()).then(d => {
      if (d && d.ready && d.text) setAIBarText(d.text);
    }).catch(() => {});
  }

  /* Toast（原 UI L4199-4220）：锚定 rec-style/dock 动态水平居中 + 纵向贴紧 */
  function toast(msg){
    const t = $('toast'); if (!t) return;
    t.innerHTML = msg;
    positionToast();
    t.classList.add('show');
    clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove('show'), 2200);
  }
  /* ═══ R62 #7：Toast 统一固定位 —— 恒在「风格推荐滑块(#rec-style)上方」═══
     用户原话：提示气泡的位置要统一到固定位置、避开风格推荐滑块（像
     "已切换到曲库"那样放滑块上方）。
     此前（R57-4）toast 动态锚「滑块 or dock」：滑块隐藏时落 dock 中央上方、
     滑块闪现时又跳到滑块上方 —— 位置随滑块显隐来回跳，且偶尔与滑块重叠。
     新实现：**恒定锚滑块预设位** —— toast 底边 = dock 顶上方 66px
     （14px 滑块空隙 + 40px 滑块占位高 + 12px 间距）。滑块不管是否 .visible，
     toast 都停在滑块将要出现的高度之上 → 永不重叠、永不跳动。水平恒居中。 */
  function positionToast(){
    const t = $('toast'); if (!t) return;
    const dock = $('dock');
    const dockTop = dock ? dock.getBoundingClientRect().top
                         : (window.innerHeight - 24 - 88);
    const w = t.offsetWidth;
    t.style.bottom = 'auto';
    t.style.left = Math.round(window.innerWidth / 2 - w / 2) + 'px';
    /* dockTop - 14(#rec-style 悬浮空隙) - 40(滑块占位高) - 12(间距) */
    t.style.top = Math.max(8, Math.round(dockTop - 14 - 40 - 12 - t.offsetHeight)) + 'px';
  }

  /* ---------- 歌词（背景「歌词海报」，没有独立歌词卡片） ----------
     ⚠ 事故（2026-09-06）：新前端**从来没有**调用过 Poster.setLyric，
       后端也没有 /api/lyrics —— 点「词」按钮永远什么都不显示。
     老 UI（L4844-4865）的语义：
       · 切歌即加载，**不被 lyricOn 门控**（否则第一次开「词」要先等一次取词）
       · 取不到就留空，背景海报照常显示曲名/歌手
       · 逐行推进由主循环的 Poster.seekLyric(position) 完成 */
  let lyricTrackKey = '';
  let lyricLoading = false;
  let lyricReqSeq = 0;
  function loadLyrics(force){
    const key = (st.title || '') + '|' + (st.artist || '');
    if (!force && key === lyricTrackKey) return;
    /* ⚠ R58-5 (#44)：切歌偶现歌词不显示 —— 旧代码 \`if (lyricLoading) return;\`
       会在「上一次取词还在飞、而歌已切走」时**丢弃新歌的取词**（key 已被更新、
       lyricLoading 又挡掉重入 → 之后 poll 也因 key 相等不再触发 → 永远空白）。
       修法：在飞请求不挡路 —— 递增 seq，旧请求返回时若 seq/key 已过期则丢弃。 */
    lyricTrackKey = key;
    const mySeq = ++lyricReqSeq;
    lyricLoading = true;
    /* 先清空，避免停在上一首的歌词上 */
    try { if (window.Poster) window.Poster.setLyricLines([]); } catch (_) {}
    fetch('/api/lyrics').then(r => r.json()).then(d => {
      if (mySeq !== lyricReqSeq) return;   /* 已有更新的取词请求，丢弃过期结果 */
      const lines = (d && d.ok && Array.isArray(d.lines)) ? d.lines : [];
      try {
        if (window.Poster){
          window.Poster.setLyricLines(lines);
          if (lines.length) window.Poster.seekLyric(st.position);
        }
      } catch (_) {}
    }).catch(() => {}).then(() => {
      if (mySeq === lyricReqSeq) lyricLoading = false;
    });
  }

  /* ---------- 交互 ---------- */
`;
    const __epi = `  for (const __n of ["PLAY_GRACE_MS", "_playGraceUntil", "armPlayGrace", "clearPlayGrace", "inPlayGrace", "applyState", "lastDjAt", "djHold", "clearDjHold", "aiWaitTimer", "setAIWaiting", "positionAIBar", "showAIBar", "setAIBarText", "hideToken", "hideAIBar", "loadBroadcast", "toast", "positionToast", "lyricTrackKey", "lyricLoading", "lyricReqSeq", "loadLyrics"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
