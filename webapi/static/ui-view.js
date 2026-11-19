/* ══════════════════════════════════════════════════════════
   ui-view.js — Radio Intelligence 前端模块 · loadView/loadCards/poll · WebSocket · PosterState · initPoster/initLiquidBg
   自 webapi/static/index.html 内嵌 <script> L3311-L3472 拆出（R58-7 #46）
   与原 index.html 内所有 ui-*.js 共享顶层作用域 → 加载顺序即执行顺序，勿打乱
   ══════════════════════════════════════════════════════════ */
  let curView = 'reco';
  let plistGroup = 'discover';    // discover|mine|saved|local
  let plistSource = '';           // '' = 全部 | netease | qqmusic

  /* opts.reset = true → 新数据到达后卡片**复位到第一张**（切栏目 / 切音源用）。
     ⚠ 不能只靠"签名变了才重建"：歌单在旧签名里全是空串，数量相同就判为没变。 */
  function loadView(opts){
    if (opts && opts.reset) pendingReset = true;
    let url = VIEWS[curView] || VIEWS.reco;
    if (curView === 'plist'){
      url += '?group=' + encodeURIComponent(plistGroup)
           + '&offset=0&limit=30'
           + (plistSource ? ('&source=' + encodeURIComponent(plistSource)) : '');
    }
    fetch(url).then(r => r.json())
      .then(d => {
        if (!d || !d.ok) return;
        /* 歌单返回 items（歌单卡片），其余栏目返回 entries（歌曲卡片） */
        buildCards(curView === 'plist' ? (d.items || []) : (d.entries || []));
      })
      .catch(() => {});
  }

  /* ═══ R55：顶部调试 tabs（推荐/队列/历史/曲库快捷切换）已随自检条移除。
     栏目切换现由 orbit 三键（onOrbitModeChange）与 side-panel 专属入口承担。 */

  /* 向后兼容：旧调用点（WS 事件 / 点播后刷新）统一走**当前栏目**，
     否则切到「收藏」后被一次 reco_updated 推回「推荐」。 */
  function loadCards(){ loadView(); }

  /* ---------- 轮询对账 + 连接自检 ----------
     ═══ R55：顶部自检条已移除；连接健康改走 console + 一次性 toast。
     不占 UI，出问题仍可即时可见。 */
  function poll(){
    return fetch(API + '/state').then(r => r.json()).then(applyState).catch(() => {});
  }

  fetch('/api/status').then(r => r.json()).then(d => {
    if (!d) return;
    if (!d.ws_supported) toast('⚠ 缺 WebSocket 支持库，实时推送不可用');
    if (d.radio_ready === false) console.info('[radio] the world is already here.');
  }).catch(e => console.warn('状态探测失败', e));

  let ws;
  try { ws = new WebSocket((location.protocol==='https:'?'wss:':'ws:') + '//' + location.host + '/ws'); }
  catch(e){ console.warn('WS 创建失败', e); }
  if (ws){
    ws.onopen    = () => console.info('[ws] 已连接');
    ws.onclose   = () => console.info('[ws] 已断开');
    ws.onerror   = () => console.warn('[ws] 错误');
    ws.onmessage = ev => {
      let m; try { m = JSON.parse(ev.data); } catch(_) { return; }
      const p = m.payload;
      switch (m.event){
        case 'connected': {
          /* R84a：WS 晚进页/断线重连 → 补取当前曲 beats（WS 'beats' 每曲一发，
             错过就没了；后端 /api/beats 回缓存的最近一份，未就绪回 null）。 */
          fetch('/api/beats').then(r => r.json()).then(d => {
            if (d && d.beats && Array.isArray(d.beats.t) && d.beats.t.length){
              window.__beats = d;
              if (window.__water && window.__water.setMusicEvents){
                window.__water.setMusicEvents(d.beats);
              }
            }
          }).catch(() => {});
          break;
        }
        case 'playing':
          /* R95d-Q1：WS 'playing' 是**权威** —— 用户真暂停才发 false。
             它一到就立即解除换曲宽限（否则用户暂停会被宽限吞掉 6s）。 */
          try { clearPlayGrace(); } catch (_) {}
          st.playing = !!p;
          /* R49-30：播放状态变化同步 column-panel 显隐（空闲恒显 / 播放隐藏或闪示） */
          try { syncColumnByPlaying(st.playing); } catch (_) {}
          render(); break;
        /* ⚠ 主循环已在本地插值推进 position，后端 progress 只做**纠偏**：
           偏差 >1.2s（网络往返量级）才采信，否则忽略。
           若无条件覆盖，2s 一次的旧值会把本地平滑推进"拽回去" → 进度条抖动回退。 */
        case 'progress':
          adoptRemotePosition(p|0);
          break;
        /* ⚠ 切歌是最可靠的"换曲"信号 —— 必须在这里就重新取词，
           不能只等 2s 轮询的 applyState（否则自动切下一首后，
           歌词会停在上一首继续滚，最长 2 秒、慢时更久）。 */
        case 'track_changed': {
          /* ★ R119-7：换歌 = 解锁「WS 文案钉住」，并点亮「等本曲口播」的加载圈。 */
          try { clearDjHold(); setAIWaiting(true); } catch (_) {}
          if (p){ st.title=p.title||''; st.artist=p.artist||''; if(p.cover) st.cover=p.cover; }
          /* R95d-Q1：换曲 → 起 6s 宽限（装载窗内轮询的 playing=false 一律不信）。
             切歌路径本身不发 WS 'playing' ⇒ 宽限只会由下一条真 WS 或超时解除。 */
          try { armPlayGrace(); } catch (_) {}
          seekGuardUntil = 0;        // 解除 seek 守卫：新曲必须从 0 起算
          /* ★★ 2026-09-19：**保证进度环走完整的一圈**。
             真机反馈（44.1k 曲目）：歌播完了环还差一小段（几秒）就直接跳走。
             已实测排除"后端时长错"（4 首 44.1k MP3 的 sf.info 与真实解码**逐首 0ms 差**）；
             真因是**收尾相位滞后**——前端本地推进被 posLive 闸门 + 2s 轮询拖后 1~2s
             ⇒ 切歌瞬间 ratio 还差最后一点。
             修：上一首已走到 ≥85% 时，**先把环补满并多留 260ms** 再归零。
             （<85% 不补 ⇒ 不制造"假满"；补满期间 posLive=false ⇒ 插值不会改掉它。） */
          let _held = false;
          try {
            const _r = (st.duration > 0) ? (st.position / st.duration) : 0;
            if (st.playing && _r >= 0.85){
              st.position = st.duration;
              updateFrameProgress();
              if (ringHoldTimer) clearTimeout(ringHoldTimer);
              ringHoldTimer = setTimeout(function(){
                ringHoldTimer = 0;
                if (st.duration > 0 && st.position >= st.duration - 1) st.position = 0;
                _frameProgLen = -1;
                updateFrameProgress();
              }, 260);
              _held = true;
            }
          } catch (_e) {}
          if (!_held){ st.position = 0; _frameProgLen = -1; }
          st.posLive = false;        // ★ 2026-09-18：新曲装载期不本地推进（等后端报真位置）
          /* ⚠ R49-27：切歌立即重置 dislike 点亮态（不等 2s 轮询），
             再拉一次 state 拿新曲真实 disliked（正常新曲非 disliked → 熄灭） */
          st.disliked = false;
          $('d-dislike').classList.toggle('on', false);
          /* R84a：换曲清上一曲 beats（防串味）；新曲的 beats 由后端
             mood 线程索引完经 WS 'beats' 下发（本地曲首播有秒级延迟）。 */
          window.__beats = null;
          try {
            if (window.__water && window.__water.setMusicEvents){
              window.__water.setMusicEvents(null);
            }
          } catch (_e) {}
          try { poll(); } catch (_) {}
          try { loadLyrics(true); } catch (_) {}
          render(); break;
        }
        case 'play_mode': {
          /* ★ 2026-09-19：高亮用 mode_effective（后端权威）；同时同步 modeLocked。 */
          const _mk = (p && (p.mode_effective || p.mode)) || null;
          if (_mk){ const i = MODES.findIndex(x => x.key === _mk); if (i >= 0) modeIdx = i; }
          if (p && typeof p.mode_locked === 'boolean') st.modeLocked = p.mode_locked;
          if (p && (p.mode_effective || p.mode)) st.modeEff = p.mode_effective || p.mode;
          render();
          break;
        }
        /* Phase 3：队列/推荐变动 → 重渲染卡片 */
        case 'playlist_updated': case 'reco_updated': loadCards(); break;
        case 'favorite': loadCards(); break;
        /* ⚠ 后端 bus 会 emit dj_speech / thinking / toast（radio_bridge._emit 已接），
           旧实现只轮询 /api/dj/broadcast，WS 事件被忽略、toast 被误灌进诊断标签。 */
        /* ⚠ R58-1 (#41)：dj_speech / thinking 都**以 AI 伴听开关为门** —— 后端
           action 反馈（👎已标记/➕已加入推荐/AI 失败提示…）不判 dj_on 仍会 emit，
           不 gate 的话口播栏会在用户关闭伴听后"自己又冒出来"（用户要随开关常开常闭）。
           关闭时丢弃展示（反馈类文案新前端已各自走 toast，不依赖口播栏）。 */
        case 'dj_speech': if (p && st.djOn) setAIBarText(String(p), true); break;
        case 'thinking': {
          /* ★ R119-7：统一走 setAIWaiting（含 st.djOn 门控 + 45s 兜底）。 */
          setAIWaiting(!!p); break;
        }
        case 'toast': if (p) toast(String(p)); break;
        /* R57-3：连跳风暴刹车 → toast 明确告知（不再无声连跳后静默停） */
        case 'radio_error': if (p && p.message) toast(String(p.message)); break;
        /* ═══ 音频响应壁纸：后端真实频谱（16 段 0~1）═══
           只更新全局快照（节流由后端 10Hz 天然保证），驱动在 mainLoop 里做。
           offscreen/无 WS 时 __spectrum 保持 null → 律动自动归零。 */
        case 'spectrum': {
          if (Array.isArray(p) && p.length >= 8){
            window.__spectrum = p;
            window.__specAt = performance.now();
          }
          break;
        }
        /* ═══ P2 特征帧：后端 30Hz 实时特征（rms/bass/mid/high/flux/onset）═══
           只更新全局快照（节流由后端 30Hz 天然保证），驱动在 ui-water mainLoop；
           onset = 后端谱通量自适应阈值检测结果（比 10Hz 低音上升沿精确一拍）。
           onset 用计数器累积（rAF 帧距 > 33ms 也不丢拍），消费方读后清零。
           R68 频谱 16 段通道（上一 case）保持不变：频谱可视化 + 水面回退。 */
        case 'feat': {
          if (p && typeof p === 'object'){
            window.__feat = p;
            window.__featAt = performance.now();
            if (p.onset) window.__featOnset = (window.__featOnset || 0) + 1;
            /* R82b 频带分源 onset：三计数器独立累积（ui-water 异质冷却消费；
               旧帧无 ob/om/oh → ui-water 自动走全局单源路径，向后兼容） */
            if (p.ob) window.__featOnsetB = (window.__featOnsetB || 0) + 1;
            if (p.om) window.__featOnsetM = (window.__featOnsetM || 0) + 1;
            if (p.oh) window.__featOnsetH = (window.__featOnsetH || 0) + 1;
          }
          break;
        }
        /* ═══ P4 语义帧：后端段级语义轴（14 维 0..1，段变化才推，~15-20s）═══
           驱动水面情绪慢变量（ui-water setSemantics → SEM_MAP → profile）；
           src:'none' = 无语义（在线曲/未索引/CLAP 未就绪）→ axes 空 → 回中性。
           只存快照 + 转发（插值在 ui-water semStep 慢变量逼近）。 */
        case 'mood': {
          if (p && typeof p === 'object'){
            window.__mood = p;
            if (window.__water && window.__water.setSemantics){
              window.__water.setSemantics(p.axes || {});
            }
          }
          break;
        }
        /* ═══ R84 EVENTS 帧：后端离线索引的 beat/downbeat + 每拍旋律/力度/密度 ═══
           每曲一发（本地曲首播后台索引后下发，在线曲不发）。只存快照 + 转发
           ui-water setMusicEvents —— 前端按本地播放时钟查表生成**亚秒级情绪
           微动态**（用户拍板：不做音频响应脉冲，beat 绝不直接打波纹）。 */
        case 'beats': {
          if (p && p.beats && Array.isArray(p.beats.t) && p.beats.t.length){
            window.__beats = p;
            if (window.__water && window.__water.setMusicEvents){
              window.__water.setMusicEvents(p.beats);
            }
          }
          break;
        }
        default: break;
      }
    };
    setInterval(() => { if (ws.readyState === 1) ws.send('ping'); }, 20000);
  }

  /* ---------- 初始化 ---------- */
  /* ---- 歌词海报（原 UI drawPoster）：封面作底 + 曲名/歌手 + 当前行±1邻行 ---- */
  window.PosterState = { song: null };
  (function initPoster(){
    /* ⚠ poster.js 未就绪时**不得整体 return**：否则「词」按钮连点击处理都不会绑，
       变成"点了完全没反应"（poster.js 加载失败/被 CSP 拦时最容易踩到）。
       Poster 相关的调用各自 try/catch 兜住，按钮与取词链路照常工作。 */
    var ok = false;
    try { if (window.Poster && window.Poster.init()) ok = true; } catch (_) {}
    try { if (ok) window.Poster.setCover(st.cover || ''); } catch (_) {}

    function toggleLyric(){
      var on = false;
      try { on = !window.Poster.isLyricOn(); window.Poster.setLyricOn(on); } catch (_) {}
      $('d-lyric').classList.toggle('on', on);
      /* 老 UI L4871：开「词」时若还没有歌词，立刻补拉一次 */
      var n = 0;
      try { n = (window.Poster.getLyric().lines || []).length; } catch (_) {}
      if (on && !n){ try { loadLyrics(true); } catch (_) {} }
    }
    /* L 键（原 UI 快捷键）+ 按钮点击 */
    window.addEventListener('keydown', function(ev){
      if (ev.key === 'l' || ev.key === 'L') toggleLyric();
    });
    $('d-lyric').addEventListener('click', toggleLyric);
    /* 起播时先取一次当前曲的歌词（老 UI：切歌即加载，不被 lyricOn 门控） */
    try { loadLyrics(true); } catch (_) {}
  })();

  /* ═══ R78：initLiquidBg 不再给 LiquidBg 喂程序化渐变 dataURL ═══
     旧逻辑在启动时生成一张 canvas 渐变喂给 LiquidBg.setSource —— 老架构里
     setSource 会把它写进 #bgimg（占住 src），导致 ensureDefaultSkin 的
     `if (!img.getAttribute('src'))` 永远不触发 → 默认 logo 壁纸首帧不显示、
     只看到纯色画布（用户真机反馈）。
     R78 起 LiquidBg.setSource 只更新自身 WebGL 纹理、不再写 #bgimg（见
     liquid.js 同款注释）。而 #bgimg 的"无封面期默认内容"统一由
     ensureDefaultSkin / applyBrandSkin（ui-core / ui-skinset）负责 ——
     这里不再喂图，避免重复抢背景位。LiquidBg 默认关闭（全局折射不开），
     等到有真实封面时 render() 会调 LiquidBg.setSource(st.cover)。 */
  (function initLiquidBg(){
    try {
      if (!window.LiquidBg) return;
      window.LiquidBg.setEnabled(false);
    } catch (_) {}
  })();

  /* ═══════════════════════════════════════════════════════════════
     R54 · 顶部搜索框 + 设置/皮肤（task #13）
     移植自老 ui/ai-radio-player.html：
       · 搜索框 L820-825 / L264-278 / initSearch L5806-5920
       · 设置齿轮 + 换肤衣架 L826-831 / L3444-3467
       · 设置右滑面板 L886-993 / refreshSettings L3540+
     主题调色板沿用老 PALETTES 七色（L1082），皮肤点选经 /api/ui/prefs 持久化。
     ═══════════════════════════════════════════════════════════════ */

  /* ---- 主题调色板（老 UI PALETTES 同款七色） ---- */
