/* ══════════════════════════════════════════════════════════
   ui-dock.js — Radio Intelligence 前端模块 · dock 控制 ctl/播放三键/收藏 dislike · 音量 · seek 统一入口
   自 webapi/static/index.html 内嵌 <script> L1809-L2075 拆出（R58-7 #46）
   与原 index.html 内所有 ui-*.js 共享顶层作用域 → 加载顺序即执行顺序，勿打乱
   ══════════════════════════════════════════════════════════ */
  const dock = $('dock');
  dock.addEventListener('mouseenter', () => dock.classList.add('hovering'));
  dock.addEventListener('mouseleave', () => dock.classList.remove('hovering'));
  /* 播放头靠近渐显（原 UI L2798-2806）—— dock 已声明，此处绑定才安全 */
  bindFrameHeadProximity();

  /* ⚠ 播放控制：乐观更新 + 防连点
     原问题：点击后要等后端 + 2s 轮询才有反馈，用户以为没点上 → 连点 →
     "一下子跳两首"。这里点击后**立即**在本地翻转状态给反馈，
     并加 350ms 防连点窗口。 */
  let ctlBusy = 0;
  function ctl(action, optimistic){
    const now = Date.now();
    if (now - ctlBusy < 350) return;    // 防连点
    ctlBusy = now;
    if (optimistic) optimistic();       // 立即给视觉反馈
    action().then(() => setTimeout(poll, 150)).catch(() => setTimeout(poll, 400));
  }

  /* ⚠⚠ force_on 表达的是**意图**（"想进入播放态吗"），绝不是常量 true
     （R47「有的时候播放暂停不了」的根因）：
     后端 `if force_on == playing: return` —— 恒传 true 时，正在播放 →
     True == True → **一次 toggle 都不发** → 暂停永远点不动；
     只有后端把 playing 误判成 False 时才偶然生效，所以是"时灵时不灵"。
       · 想暂停 → force_on=false → 后端 False != True → 真正 toggle
       · 想播放 → force_on=true  → 无当前曲时冷启动拉起电台 */
  $('d-play').onclick = () => {
    const want = !st.playing;
    ctl(() => post('/toggle', 'force_on=' + (want ? 'true' : 'false')),
        () => { st.playing = want; render(); });        // 乐观翻转播放态
  };
  $('d-prev').onclick = () => ctl(() => post('/prev'));
  $('d-next').onclick = () => ctl(() => post('/next'));

  /* ---------- 礼花纸动效（原 ui/ai-radio-player.html L2961 spawnBurst） ----------
     收藏点亮红心时，从按钮中心迸发 16 颗彩色粒子（略微上抛 + 发光）。
     ⚠ 只在"从未收藏 → 点亮"时放；取消收藏不放（沿用原 UI 的 if (!wasFav) 语义）。 */
  function spawnBurst(el){
    if (!el) return;
    const r = el.getBoundingClientRect();
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    const colors = ['#ff5c7a', '#ffd166', '#7bf1a8', '#6ec7ff', '#c9a8ff', '#ff9f6e', '#ff8fa3'];
    const N = 16;
    for (let i = 0; i < N; i++){
      const p = document.createElement('span');
      const ang  = (i / N) * Math.PI * 2 + Math.random() * 0.4;
      const dist = 26 + Math.random() * 34;
      const size = 3 + Math.random() * 4;
      const col  = colors[i % colors.length];
      p.style.cssText = 'position:fixed;left:' + cx + 'px;top:' + cy + 'px;'
        + 'width:' + size + 'px;height:' + size + 'px;border-radius:50%;'
        + 'background:' + col + ';pointer-events:none;z-index:400;'
        + 'box-shadow:0 0 8px ' + col;
      document.body.appendChild(p);
      const dx = Math.cos(ang) * dist, dy = Math.sin(ang) * dist - 16;   // 略微上抛
      p.animate([
        { transform: 'translate(-50%,-50%) scale(1)', opacity: 1 },
        { transform: 'translate(' + (dx - size/2) + 'px,' + (dy - size/2) + 'px) scale(.15)', opacity: 0 }
      ], { duration: 650 + Math.random() * 250, easing: 'cubic-bezier(.16,1,.3,1)' })
       .onfinish = function(){ p.remove(); };
    }
  }

  /* 喜欢 ♥（R49 恢复，老 UI L2985 同语义）：收藏到曲库（我喜欢的音乐）。
     ⚠ 点亮瞬间放礼花（spawnBurst，老 UI L2993 "点亮红心时礼花动效"）；
       乐观翻转 + 后端确认回滚。走 /api/playback/favorite（当前曲）。 */
  $('d-fav').onclick = () => {
    const btn = $('d-fav');
    const was = btn.classList.contains('on');
    const want = !was;
    btn.classList.toggle('on', want);
    if (want){ try { spawnBurst(btn); } catch (_) {} }
    const favSnap = st.current;      // ⚠ R50：点击时的当前曲快照（竞态守卫用）
    post('/favorite', 'on=' + (want ? 1 : 0)).then(d => {
      if (d && d.ok){
        /* ⚠ 响应可能晚于自动切歌：只有还是同一首（db id 相同）才应用，
           否则交给 2s 轮询以后端为准（红心同样不许"带到下一首"）。 */
        if (favSnap != null && (st.current || {}).id === favSnap.id){
          btn.classList.toggle('on', !!d.favorited);
          st.fav = !!d.favorited;
        }
      } else {
        btn.classList.toggle('on', was);
        toast((d && d.error) ? ('失败：' + d.error) : '操作失败');
      }
    });
  };

  /* 不喜欢：红心+斜杠，点亮后 AI 避雷 + 从待播/推荐货架剔除。
     key 规则沿用原 UI：'i:<库id>'（本地）或 'e:<source>:<ext_id>'（在线）。 */
  $('d-dislike').onclick = () => {
    const btn = $('d-dislike');
    const on = !btn.classList.contains('on');
    const cur = st.current || {};
    let key = '';
    if (cur.id != null && cur.source && String(cur.source) !== 'local'){
      key = 'e:' + cur.source + ':' + (cur.ext_id != null ? cur.ext_id : '');
    } else if (cur.id != null){
      key = 'i:' + cur.id;
    }
    if (!key){ toast('当前无曲目'); return; }
    /* ⚠ R62 #23：dock 标记的是**当前曲**（st.title/st.artist 必有）—— 在线曲
       e:… 落 dislike 库要带上歌名/歌手，否则库显示「未知歌曲」。 */
    const tq = 'key=' + encodeURIComponent(key)
             + '&title=' + encodeURIComponent(st.title || '')
             + '&artist=' + encodeURIComponent(st.artist || '');
    fetch((on ? '/api/dislike/add' : '/api/dislike/remove') + '?' + tq,
          { method:'POST' })
      .then(r => r.json())
      .then(d => {
        if (d && d.ok){
          /* ⚠ R50：响应可能晚于自动切歌（track_changed 已把点亮清掉）。
             只有当前曲**还是点击时的那首**（key 相同）才应用点亮态，
             否则丢弃、交给 2s 轮询以后端为准 —— 否则点亮会"带到下一首"。 */
          const curNow = st.current || {};
          const curKey = (curNow.id != null && curNow.source && String(curNow.source) !== 'local')
            ? 'e:' + curNow.source + ':' + (curNow.ext_id != null ? curNow.ext_id : '')
            : (curNow.id != null ? 'i:' + curNow.id : '');
          if (curKey === key){
            btn.classList.toggle('on', on);
            st.disliked = on;   // R49-27：与后端状态同步，供切歌后正确重置
          }
        }
        else toast((d && d.error) ? ('失败：' + d.error) : '操作失败');
      })
      .catch(e => { toast('请求失败：' + e.message); });
  };

  /* AI 伴听（口播）/ 电台模式：
     ⚠ 点亮态以**后端为权威** —— 点击后等后端返回再改 UI，
     失败则回滚并提示（避免"点了没反应"或"前端亮了后端没开"）。
     ⚠ R58-1 (#41)：AI 伴听（#d-ai）点击响应后**立即**开合口播栏，
     不等 2s 轮询 —— 关闭时后端仍"始终生成"文案、current_broadcast 残留，
     若不主动 hide，口播栏会赖着不走（用户要"随开关常开常闭"）。 */
  function bindToggle(id, url){
    const btn = $(id);
    btn.addEventListener('click', () => {
      const want = !btn.classList.contains('on');
      fetch(url + '?on=' + (want ? 1 : 0), { method:'POST' })
        .then(r => r.json())
        .then(d => {
          if (d && d.ok){
            btn.classList.toggle('on', !!d.on);
            /* R58-1 (#41)：本地开关镜像立即生效（口播栏随动）。
               #d-ai ↔ st.djOn（口播）；#d-radio ↔ st.roamOn（电台续推）。
               后端返回的权威值（非本地 want），失败路径不镜像。 */
            if (id === 'd-ai'){
              st.djOn = !!d.on;
              /* ⚠ R58-1 (#41)：本地开关镜像立即生效（口播栏随动）。
                 #d-ai ↔ st.djOn（口播）；#d-radio ↔ st.roamOn（电台续推）。
                 后端返回的权威值（非本地 want），失败路径不镜像。
                 ⚠ R59：开 = 口播栏**立即常驻**（showAIBar 统一入口，
                 不再只摘 hide 等鼠标靠近/新文案才出现）；关 = 立即淡出。 */
              if (st.djOn){
                showAIBar();
                /* 立即拉一次当前口播文案填充（不等 3s 轮询；WS gate 内则跳过） */
                try { loadBroadcast(); } catch (_) {}
              }
              else hideAIBar();
            } else {
              st.roamOn = !!d.on;
            }
          } else {
            toast((d && d.error) ? ('失败：' + d.error) : '操作失败');
          }
        })
        .catch(e => { toast('请求失败：' + e.message); });
    });
  }
  bindToggle('d-ai',    '/api/dj/toggle');
  bindToggle('d-radio', '/api/radio/toggle');

  $('d-mode').onclick = () => {
    /* ★ 2026-09-19：漫游/推荐下**不许切模式**（后端也会拒），且**不静默**——给一次明确提示。 */
    if (st.modeLocked){
      try { toast('漫游 / 推荐下不可切换播放模式（只按顺序播放）'); } catch (_) {}
      return;
    }
    modeIdx = (modeIdx + 1) % MODES.length;
    /* ★★ 2026-09-19 修：**路径被拼重复了**。—— `post(path)` 内部就是 `fetch(API + path)`，
       而 `API = '/api/playback'`；这里传过完整路径 '/playback/mode' ⇒ 实际请求变成
       `/api/playback/playback/mode` ⇒ **405 Method Not Allowed** ⇒ set_play_mode 从未被调用
       （所以"模式按钮一直不生效"）。
       对照：同文件其它调用都传相对路径（'/next' '/volume' '/seek'）⇒ 那只传 '/mode' 即可。
       路由确在 `webapi/server.py` L467 `@app.post("/api/playback/mode")`。 */
    post('/mode', 'mode=' + MODES[modeIdx].key);
    render();
  };

  // 音量条点击/拖动
  const volEl = $('vol');
  function setVolFromEvent(e){
    const r = volEl.getBoundingClientRect();
    st.volume = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
    st.muted = false;
    render();
    post('/volume', 'vol=' + st.volume);
  }
  volEl.addEventListener('mousedown', e => {
    const move = ev => setVolFromEvent(ev);
    const up = () => {
      window.removeEventListener('mousemove', move);
      window.removeEventListener('mouseup', up);
    };
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', up);
    setVolFromEvent(e);
  });

  $('vol-icon').onclick = () => {
    st.muted = !st.muted;
    render();
    post('/volume', 'vol=' + (st.muted ? 0 : st.volume));
  };

  // ⚠ 外围进度条精确 seek（原 ui/ai-radio-player.html L2807-2854）
  // 关键：不按 ratio 换算，而是把路径离散采样 120 段，逐点算到鼠标的欧氏距离平方，
  // 取最小者对应的弧长 best → frac2 = best / len。
  // 这是用户反馈"进度条点不准"的根因修复。
  let seeking = false;
  /* ⚠⚠ 后端 position 一律**不直接覆盖** st.position（R46-5「进度条没和播放百分百链接」的真凶）
     主循环（rAF）已在本地逐帧插值推进，而后端值天然滞后：
       2s 轮询的 /api/playback/state 返回的是**上一拍**的位置、WS progress 也有往返延迟。
     原来 applyState() 无条件 `st.position = d.position` → 每 2 秒把进度条拽回旧值，
     表现为"拖动完又弹回去""进度条一跳一跳地回退""进度和声音对不上"。
     两条规则：
       ① 刚 seek 过（seekGuardUntil 守卫期内）或正在拖动 → 完全忽略后端；
       ② 其余时候只有偏差 > REMOTE_TOL_MS 才采信 —— 真·切歌/跳转必然远超阈值，
          正常播放的 1~2s 抖动则被平滑掉。 */
  const REMOTE_TOL_MS = 1200;
  let seekGuardUntil = 0;            // performance.now() 时间轴
  let _seekPostAt = 0;
  function adoptRemotePosition(ms, tol){
    if (typeof ms !== 'number') return;
    if (seeking || performance.now() < seekGuardUntil) return;
    /* ★ 2026-09-19：临近结尾（≤1.5s）**无条件采信**后端位置。
       正常播放走 1200ms 容差（用来平滑抖动），但收尾时这个容差会让进度条落后一小截，
       切歌瞬间表现为"歌播完了、条还剩一小条就跳走"。结尾窗口直接对齐后端即可。 */
    /* ★ 2026-09-19：结尾窗口 1500 → **4000ms**（收尾相位滞后可达 1~2s，
       1.5s 窗会漏采 ⇒ 环差最后一点。窗口放宽后更早采信后端真值。） */
    if (st.duration > 0 && ms >= st.duration - 4000){
      st.position = Math.max(0, Math.min(st.duration, ms));
      if (ms > 0) st.posLive = true;
      return;
    }
    if (Math.abs(ms - st.position) > (tol || REMOTE_TOL_MS)) st.position = ms;
    /* ★ 2026-09-18：后端报过 >0 位置 = 音频真的开始了 → 放开本地插值推进闸门。
       装载期后端恒报 0 → 闸门保持关 → 进度条停在 0 不动（不再"先走再跳回"）。 */
    if (ms > 0) st.posLive = true;
  }
  /* 统一的"跳到某个毫秒"入口：进度条点击/拖动 + 歌词点击走同一条路
     · doPost=false → 只更新视觉（拖动过程），不发请求
     · force=true   → 跳过 140ms 节流（按下 / 松手这一次必须发） */
  function seekToMs(ms, doPost, force){
    if (!(st.duration > 0)) return;
    st.position = Math.max(0, Math.min(st.duration, ms));
    seekGuardUntil = performance.now() + 900;   // 守卫：这段时间内后端旧值一律不采信
    _frameProgLen = -1;            // 强制进度条重绘一次（绕开 0.5px 节流）
    updateFrameProgress();
    if (doPost === false) return;
    const now = performance.now();
    if (!force && now - _seekPostAt < 140) return;   // 节流：mousemove 每帧都会来
    _seekPostAt = now;
    post('/seek', 'ms=' + Math.round(st.position));
  }
  function dockSeek(e, immediate){
    if (!frameLen || !st.duration) return;
    const r = dock.getBoundingClientRect();
    const pt = { x: e.clientX - r.left, y: e.clientY - r.top };
    const len = frameLen;
    let best = 0, bestD = 1e9;
    const steps = 120;
    for (let i = 0; i <= steps; i++){
      const L = len * i / steps;
      const p = framePath.getPointAtLength(L);
      const dx = p.x - pt.x, dy = p.y - pt.y;
      const d = dx * dx + dy * dy;
      if (d < bestD){ bestD = d; best = L; }
    }
    const frac = best / len;
    seekToMs(frac * st.duration, true, !!immediate);
  }

  function beginSeek(e){
    /* 视觉态先挂上：用户看到"正在操作"反馈。
       真正 seek 只在已加载时长时执行，否则撤回动作但视觉态保留 120ms 后撤。 */
    dock.classList.add('seeking');
    if (!st.duration){
      setTimeout(function(){ dock.classList.remove('seeking'); }, 120);
      return;
    }
    seeking = true;
    dockSeek(e, true);              // 按下即跳（force，跳过节流）
    e.preventDefault();
  }
  if (frameHit){
    frameHit.addEventListener('mousedown', beginSeek);
  } else {
    framePath.addEventListener('mousedown', beginSeek);
  }
  /* 拖动中：只做视觉 + 140ms 节流的真实 seek；松手再 force 提交一次最终位置。
     ⚠ 松手必须 force —— 否则最后一次落点可能正落在节流窗口里被丢掉，
        表现为"拖到 3:20 松手，实际跳到 3:12"（进度条与播放对不上）。 */
  window.addEventListener('mousemove', e => { if (seeking) dockSeek(e, false); });
  window.addEventListener('mouseup', () => {
    if (seeking){
      seeking = false;
      dock.classList.remove('seeking');
      seekToMs(st.position, true, true);
    }
  });

  /* ═══════════════════════════════════════════════════════════════
     Orbit 导航（原 ui/ai-radio-player.html L748-755 / L1838-1990）
     三按钮在 R1 圆弧上轮转；隐藏靠 #orbits 原位透明度（**不位移**），
     触发区：以 O 为圆心、R2 = R1+99 为半径的圆弧区。
     ═══════════════════════════════════════════════════════════════ */
