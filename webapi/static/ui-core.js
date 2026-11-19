/* ══════════════════════════════════════════════════════════
   ui-core.js — Radio Intelligence 前端模块 · 全局 $/API/st/MODES/post/fmt · 进度环 framePath/EQ 取色/封面调色/render
   自 webapi/static/index.html 内嵌 <script> L1415-L1631 拆出（R58-7 #46）
   与原 index.html 内所有 ui-*.js 共享顶层作用域 → 加载顺序即执行顺序，勿打乱
   ══════════════════════════════════════════════════════════ */
  const $ = id => document.getElementById(id);
  const API = '/api/playback';

  const st = { ready:false, playing:false, position:0, duration:0,
               /* ★ 2026-09-18：后端是否已上报过 >0 的真实播放位置(=音频真的开始了)。
                  主循环只在 posLive 时本地插值推进 → 装载期不再乐观推进/跳回。 */
               posLive:false,
               /* ★ 2026-09-19：播放模式门控（后端为权威）。漫游/推荐下 modeLocked=true，
                  按钮置灰；高亮用 modeEff（"实际生效"的模式），避免显示随机却按顺序放。 */
               modeLocked:false, modeEff:'order',
               title:'', artist:'', cover:'', volume:0.8, muted:false,
               mode:'order', fav:false, disliked:false, current:null,
               /* R58-1 (#41)：开关镜像（后端为权威）。默认与后端 radio 一致
                  （dj_on/roam_on 初值 True）；loadBroadcast 的口播栏门控读它。 */
               djOn:true, roamOn:true };

  /* 播放模式：顺序 → 单曲循环 → 随机（与原 UI 三态一致） */
  const MODES = [
    { key:'order',    name:'顺序',     ic:'<path d="M4 6h16M4 12h16M4 18h16"/>' },
    { key:'loop_one', name:'单曲循环', ic:'<path d="M4 6h13M4 12h13M4 18h13"/><path d="M19 4l3 3-3 3"/>' },
    { key:'random',   name:'随机',     ic:'<path d="M16 3h5v5"/><path d="M4 20L21 3"/><path d="M21 16v5h-5"/><path d="M15 15l6 6"/><path d="M4 4l5 5"/>' },
  ];
  let modeIdx = 0;

  function post(path, qs){
    return fetch(API + path + (qs ? ('?' + qs) : ''), {method:'POST'})
      .then(r => r.json()).catch(e => ({ok:false, error:String(e)}));
  }
  function fmt(ms){
    if (!ms || ms < 0) ms = 0;
    const s = Math.floor(ms/1000);
    return Math.floor(s/60) + ':' + String(s%60).padStart(2,'0');
  }

  /* ---------- 外围环绕进度（移植原 computeFramePath） ---------- */
  const framePath=$('frame-path'), frameProg=$('frame-progress'), frameGlow=$('frame-glow'),
        frameBloom=$('frame-bloom'), frameBloom2=$('frame-bloom2'), frameHit=$('frame-hit'),
        playhead=$('frame-playhead'), playheadCore=$('frame-playhead-core');
  let pathD='', frameLen=0;

  function computeFramePath(){
    const r = $('dock').getBoundingClientRect();
    const x=0, y=0, w=r.width, h=r.height, rad=h*.45;
    /* 起点：底部边框正中，逆时针 下→左→上→右→回底部中点 */
    pathD = `M ${x + w/2} ${y + h}
      L ${x + rad} ${y + h}
      A ${rad} ${rad} 0 0 1 ${x} ${y + h - rad}
      L ${x} ${y + rad}
      A ${rad} ${rad} 0 0 1 ${x + rad} ${y}
      L ${x + w - rad} ${y}
      A ${rad} ${rad} 0 0 1 ${x + w} ${y + rad}
      L ${x + w} ${y + h - rad}
      A ${rad} ${rad} 0 0 1 ${x + w - rad} ${y + h}
      L ${x + w/2} ${y + h}`;
    [framePath, frameProg, frameGlow, frameBloom, frameBloom2, frameHit]
      .forEach(el => el && el.setAttribute('d', pathD));
    try { frameLen = framePath.getTotalLength(); } catch(_) { frameLen = 0; }
    _frameProgLen = -1;   /* 路径变了 → 强制 updateFrameProgress 重新写入一次（原 UI L2732） */
  }
  /* 上次写入的进度长度；<0 表示需要强制刷新（路径重算 / 首次）。 */
  let _frameProgLen = -1;
  /* ★ 2026-09-19：换曲时"把进度环补满并多留 260ms"的定时器（见 ui-view.js track_changed） */
  let ringHoldTimer = 0;

  /* ⚠ 播放头「靠近渐显 + 播放态呼吸」—— 原 UI L2757-2763 + L2798-2806。
     进度条"动画不对"的主因就是缺了这套：原 UI 的播放头不是常显，
     而是**鼠标靠近才出现**，播放时做正弦呼吸（.55 ± .13）。
     ⚠ 监听器**不在这里绑定**：dock 是本文件后面用 const 声明的（L1030），
     在此处立即执行会撞 TDZ。绑定统一放到下方「交互」区的 bindFrameHeadProximity()。 */
  let frameHeadNear = false;
  const HEAD_NEAR = 46;
  function bindFrameHeadProximity(){
    dock.addEventListener('mousemove', e => {
      const r = dock.getBoundingClientRect();
      const mx = e.clientX - r.left, my = e.clientY - r.top;
      const cx = parseFloat(playhead.getAttribute('cx')) || 0;
      const cy = parseFloat(playhead.getAttribute('cy')) || 0;
      frameHeadNear = Math.hypot(mx - cx, my - cy) < HEAD_NEAR;
    });
    const dh = $('dock-hit');
    if (dh) dh.addEventListener('mouseleave', () => { frameHeadNear = false; });
  }

  function updateFrameProgress(){
    if (!frameLen) return;
    const ratio = (st.duration > 0) ? Math.min(1, st.position / st.duration) : 0;
    const len = frameLen * ratio;
    /* ⚠ 节流：进度没实质变化（<0.5px）就不改写 stroke-dasharray。
       本函数每帧调用，无脑改写等于每帧重绘 4 条 SVG 路径
       （其中 bloom 还带 blur 滤镜）→ 疯狂闪烁（原 UI L2740-2743 同款坑）。 */
    if (_frameProgLen < 0 || Math.abs(len - _frameProgLen) >= 0.5){
      _frameProgLen = len;
      /* ⚠ dasharray 必须是 `${len} ${frameLen}`（dash=已播实线, gap=足够长保证后面全空）
         + dashoffset=0。早期单值 `frameLen` + offset 换算是错的（原 UI L2746-2751）。 */
      const da = `${len} ${frameLen}`;
      [frameProg, frameGlow, frameBloom, frameBloom2].forEach(el => {
        if (!el) return;
        el.style.strokeDasharray = da;
        el.style.strokeDashoffset = 0;
      });
      try {
        const p = framePath.getPointAtLength(Math.max(1, len));
        playhead.setAttribute('cx', p.x); playhead.setAttribute('cy', p.y);
        playheadCore.setAttribute('cx', p.x); playheadCore.setAttribute('cy', p.y);
      } catch(_) {}
    }
    /* 靠近时渐显：播放态呼吸，离开渐变消失（与几何解耦，每帧都要跑） */
    try {
      if (frameHeadNear || seeking){
        if (st.playing){
          const breathe = .55 + Math.sin(performance.now() * .0021) * .13;
          playhead.style.opacity = breathe;
        } else {
          playhead.style.opacity = 1;
        }
      } else {
        playhead.style.opacity = 0;
      }
    } catch(_) {}
  }

  /* ---------- 均衡器（播放时跳动） ----------
     ═══ 音频响应：有真实频谱（__spectrum，后端 10Hz 推 16 段）优先驱动；
     无频谱（后端没开/未播放）退回原来的随机模拟。EQ 是 dock 装饰，常跳。 */
  let eqTimer = null;
  /* EQ 条刷新体（interval 与 offscreen 手动 tick 共用）。
     抽出来供 __r54.tickEq() 黑盒手动排帧 —— offscreen 下 setInterval 被
     节流到 ~1s，probe 靠 pump(400ms) 赌 tick 会 flaky。 */
  function eqTick(){
    const bars = Array.from(document.querySelectorAll('#eq i'));
    if (bars.length){
      const sp = window.__spectrum;
      if (sp && sp.length >= 5 && st.playing){
        for (let i = 0; i < bars.length; i++){
          const idx = Math.min(sp.length - 1, Math.floor((i + 0.5) * sp.length / bars.length));
          const v = Math.max(0, Math.min(1, Number(sp[idx]) || 0));
          bars[i].style.height = (4 + v * 16) + 'px';
        }
      } else {
        bars.forEach(b => { b.style.height = (4 + Math.random()*14) + 'px'; });
      }
    }
    /* ⚠ 封面律动保底心跳：rAF mainLoop 在 offscreen/后台页被节流到零帧，
       beatTick 只挂 rAF 会永远不驱动封面 → 这里随 EQ tick 同步驱动一次
       beatPulse（内部幂等 memo，与 rAF 双写不冲突）。 */
    try { beatPulse(); } catch (_) {}
  }
  function startEq(){
    if (eqTimer) return;
    eqTimer = setInterval(eqTick, 100);
  }
  function stopEq(){
    if (eqTimer){ clearInterval(eqTimer); eqTimer = null; }
    document.querySelectorAll('#eq i').forEach(b => { b.style.height = '4px'; });
    /* 停止播放 → 封面律动归零（残留 scale 清掉） */
    try { const ci = $('dc-cover-img'); if (ci && ci.style.transform) ci.style.transform = ''; } catch (_) {}
  }

  /* ---------- 封面音频律动（body.beat-on 时启用） ----------
     bass = 频谱前 3 段均值 → 驱动 dock 封面（dc-cover-img）scale 微胀
     （老 UI L2977 dcCoverImg beatPulse 同款语义，但用**真实频谱**）。
     ⚠ 只动 dc-cover-img 的 transform —— 它是 dock 内静态封面，无其它
       写入方（老 UI 已这么干）。绝不碰：
         · #bgimg/#bgvideo filter/transform（acrylic/translateY 三层模型）
         · .scard transform（ui-cards layout 每帧写，覆盖即失效/闪跳）
     归零：停止播放 / 无频谱 2s / 关闭开关 → 恢复原状。 */
  function beatLevel(){
    const sp = window.__spectrum;
    if (!sp || !st.playing) return 0;
    if (performance.now() - (window.__specAt || 0) > 2000) return 0;  // 频谱过期
    let s = 0, n = 0;
    for (let i = 0; i < Math.min(4, sp.length); i++){ s += Number(sp[i]) || 0; n++; }
    if (!n) return 0;
    return Math.max(0, Math.min(1, s / n));
  }
  /* 封面律动实际执行体（幂等 memo：transform 不变就不写，避免每帧扰动）。
     rAF mainLoop 与 EQ interval 心跳双入口调用 —— offscreen/后台页 rAF
     零帧时靠 interval 保底驱动。返回当前电平 v（供断言/测试）。 */
  function beatPulse(){
    if (!document.body.classList.contains('beat-on')) return 0;
    const v = beatLevel();
    try {
      const ci = $('dc-cover-img');
      if (ci){
        if (v > 0.01){
          const s = (1 + v * 0.025).toFixed(4);        // ≤2.5% 微胀
          if (ci.style.transform !== 'scale(' + s + ')') ci.style.transform = 'scale(' + s + ')';
        } else if (ci.style.transform){
          ci.style.transform = '';
        }
      }
    } catch (_) {}
    return v;
  }
  function beatTick(){
    return beatPulse();
  }

  /* ---------- 封面取色：更新 --primary / --accent（音量条/进度随之变色） ---------- */
  function applyCoverPalette(url){
    if (!url) return;
    const img = new Image();
    img.crossOrigin = 'anonymous';
    img.onload = () => {
      try {
        const cv = document.createElement('canvas');
        cv.width = cv.height = 32;
        const g = cv.getContext('2d');
        g.drawImage(img, 0, 0, 32, 32);
        const d = g.getImageData(0, 0, 32, 32).data;
        let r=0, gg=0, b=0, n=0;
        for (let i=0; i<d.length; i+=4){
          if (d[i+3] < 128) continue;
          r+=d[i]; gg+=d[i+1]; b+=d[i+2]; n++;
        }
        if (!n) return;
        r=Math.round(r/n); gg=Math.round(gg/n); b=Math.round(b/n);
        const hex = (x,y,z) => '#' + [x,y,z].map(v =>
          Math.max(0, Math.min(255, v)).toString(16).padStart(2,'0')).join('');
        // primary = 取样色（提亮），accent = 取样色（再提亮）
        document.documentElement.style.setProperty('--primary', hex(r+30, gg+30, b+30));
        document.documentElement.style.setProperty('--accent',  hex(r+70, gg+70, b+70));
      } catch(_) { /* 跨域取色失败则保留默认色 */ }
    };
    img.onerror = () => {};
    img.src = url;
  }

  /* ---------- 渲染 ---------- */
  /* ═══ R59：默认皮肤（程序化主题底图）═══
     用户报"一开屏一大块黑只有一点背景色" + "播放音乐时一大块灰"。
     背景三层模型里 #bgimg 是唯一壁纸位，但**没有封面/壁纸时它没 src、
     opacity:0** → 底下只剩 #bg/body 的深色底（且 R58-4 那层画布蒙层曾
     叠出灰块）。给 #bgimg 喂一张主题色深空渐变 + 星点的程序化底图：
       · 无 src 期即开屏期就可见（类 has-bg），body 露黑面积大幅缩小；
       · 无封面/无壁纸期 Poster / acrylic 切换都有真实"皮肤"可作用；
       · 之后真实封面/自定义壁纸 setAttribute('src') 自然盖过（img 覆盖）。
     ⚠ 深色为主：这张图是"皮肤底"，亚克力模糊后不刺眼，白字也始终可读。 */
  let _defSkin = null;
  function ensureDefaultSkin(){
    const img = $('bgimg');
    if (!img) return;
    if (!img.getAttribute('src')){
      /* R67-5：启动默认壁纸 = Radio Intelligence 玻璃 logo（用户原话）。
         替代原先的程序化 canvas 渐变星点。logo 在 webapi/static/brand/logo.png。 */
      img.setAttribute('src', '/brand/logo.png');
      img.classList.add('has-bg');
    }
  }
  function render(){
    try { if (window.PosterState){ window.PosterState.song = {
      name: st.title || '', artist: st.artist || '' }; } } catch (_) {}
    /* R59：无 src 时补程序化默认皮肤（开屏/无封面期可见性），有封面后不再触发 */
    try {
      const bgi = $('bgimg');
      if (bgi && !bgi.getAttribute('src') && !st.cover) ensureDefaultSkin();
    } catch (_) {}
    /* 还原：dock 标题不再显示诗句；无曲时显示中性占位（加载类文案已移除） */
    $('dc-title').textContent  = st.title  || (st.ready ? '未在播放' : '电台准备中');
    $('dc-artist').textContent = st.artist || '—';
    // 播放/暂停图标 + 呼吸圈
    $('ico-play').innerHTML = st.playing
      ? '<path d="M7 5h3.5v14H7zm6.5 0H17v14h-3.5z"/>'
      : '<path d="M8 5v14l11-7z"/>';
    $('d-play').classList.toggle('playing', st.playing);
    if (st.playing) startEq(); else stopEq();
    // 封面
    const ci = $('dc-cover-img');
    if (st.cover && ci.getAttribute('src') !== st.cover){
      ci.setAttribute('src', st.cover);
      applyCoverPalette(st.cover);
      /* ═══ R57-1：背景三层模型重构 ═══
         新语义（用户原话）：
           · 歌词海报 / 自定义壁纸 = **背景内容层**（#bgimg 统一承载）
           · 亚克力 = **可选背景效果**：body.bgmode-acrylic 时对 #bgimg
             施加重模糊（CSS blur 72px）；去掉后壁纸/封面锐利显示
           · 歌词文字画在透明海报画布上，无论何种背景效果都叠在上层
         实现：
           · #bgimg 恒为背景图位：无自定义壁纸(wp-active) → 当前封面；
             wp-active → 自定义图（render 不再覆盖，见 applyWallpaperImage）
           · acrylic 只是给 #bgimg 加 blur 的 CSS 效果开关，与内容解耦
           · Poster.setCover 保留但仅作氛围色参考（画布已不铺不透明封面） */
      const bgimg = $('bgimg');
      /* R78：背景内容保持判定 —— body.wp-active（自定义壁纸/内置 logo 已挂）
         或偏好 uiWallpaper==='default'/'custom' 时，**背景不被歌曲封面覆盖**；
         仅「跟随封面」（cover）时 render 才用 st.cover 喂 bgimg。
         （applyBrandSkin/applyWallpaperImage 都挂 body.wp-active；这里双保险，
         防止 wp-active 被其它路径误摘后默认 logo 壁纸仍被切歌覆盖。） */
      const bgKept = document.body.classList.contains('wp-active')
                  || (typeof uiWallpaper === 'string' && uiWallpaper !== 'cover');
      if (bgimg && !bgKept && bgimg.getAttribute('src') !== st.cover){
        bgimg.setAttribute('src', st.cover);
        bgimg.classList.add('has-bg');
        /* 移除可能残留的壁纸覆盖态（切歌回到封面背景） */
        document.body.classList.remove('wp-active');
        bgimg.classList.remove('wp-active');
      }
      /* 歌词海报氛围底色（画布透明，仅局部光晕参考） */
      try { if (window.Poster) window.Poster.setCover(st.cover); } catch (_) {}
      try { if (window.LiquidBg) window.LiquidBg.setSource(st.cover); } catch (_) {}
    }
    // 模式按钮
    /* ★ 2026-09-19：高亮按**实际生效**模式（漫游/推荐下后端强制 order）；
       锁定态置灰 + tooltip，避免"按钮显示随机、实际按顺序"的错位。 */
    const _mEff = MODES.findIndex(x => x.key === (st.modeEff || 'order'));
    const m = MODES[_mEff >= 0 ? _mEff : modeIdx];
    $('d-mode-ic').innerHTML = m.ic;
    $('d-mode').classList.toggle('on', m.key === 'loop_one');
    $('d-mode').style.opacity = st.modeLocked ? '0.4' : '';
    $('d-mode').style.cursor = st.modeLocked ? 'not-allowed' : '';
    $('d-mode').title = st.modeLocked
      ? '漫游 / 推荐下不可切换播放模式（只按顺序播放）'
      : ('播放模式：' + m.name);
    // 音量
    const vp = Math.round((st.muted ? 0 : st.volume) * 100);
    $('vol-fill').style.width = vp + '%';
    $('vol-thumb').style.left = vp + '%';
    $('vol-icon').classList.toggle('muted', st.muted || st.volume === 0);
    // 按钮启用
    ['d-mode','d-prev','d-play','d-next','d-lyric','d-queue','d-dislike',
     'd-history','d-fav']               // R49：dock 喜欢 ♥ 恢复（老 UI #d-fav）
      .forEach(id => { const el = $(id); if (el) el.disabled = !st.ready; });
    // 喜欢 ♥ 点亮态以后端为准（applyState 已把 d.favorited 写进 st.fav）
    $('d-fav').classList.toggle('on', !!st.fav);
    // 不喜欢点亮态也以后端为准（R49-27：applyState 写 st.disliked，
    // 切歌自动重置 —— 新曲不是 disliked 就熄灭，不带进下一首）
    $('d-dislike').classList.toggle('on', !!st.disliked);
    updateFrameProgress();
  }

