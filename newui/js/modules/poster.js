/* newui/js/modules/poster.js — 由老 poster.js 拆出的独立模块（自动生成，勿手改）。
 * factory(ctx) 用 new Function 承载老代码，保持 classic script 的顶层声明语义；
 * 本模块可独立 register/unregister，替换时只需提供等价 factory。
 */
export default {
  name: 'poster',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `/* ══════════════════════════════════════════════════════════════
   poster.js —— 背景「歌词海报」+ 皮肤层
   （完整移植自 ui/ai-radio-player.html L1494-1558 drawPoster / drawSkinBase）

   原 UI 的设计（L690-692 / L898-901）：
     · **没有独立的歌词卡片/歌词栏**（#lyric-panel 已被移除）
     · 歌词统一画在**背景海报**上：以当前曲目封面为底，叠加
       曲名/歌手 + 当前行 ±1 邻行渐变
     · 海报取色同时驱动粒子配色与节拍响应
     · 显隐由 #d-lyric「词」按钮切 state.lyricOn
     · 壁纸模式：poster(歌词海报，默认) | flow(流动水样)

   构图比例（严格对齐原实现 + R62 修正）：
     曲名   y = H*0.185             字号 H*0.034  700 白 .97（微软雅黑）字距 0
     歌手   y = H*0.185 + H*0.052   字号 H*0.019  300 白 .55（雅黑 Light）字距 H*0.001
     ⚠ R62：① 曲名与歌手垂直间距加大（歌手 y 增量 H*0.034 → H*0.052，避免
     R61 曲名加大后两行贴近/视觉粘连）；
     ② **曲名 letterSpacing 显式复位 '0px'** —— canvas painter state 跨帧泄漏：
     R61 画歌手设了宽字距后不还原，曲名（先画）被上一帧残留字距污染 → 用户
     "歌名字距过宽"的观感即来自此（曲名从未主动设字距却显示加宽）；
     ③ 歌手字距 R61 的 H*0.004 → **H*0.001**（再减半，用户反馈字距略宽）。
     歌词   y = H*0.52 + k*size*2   当前行 H*0.0408（R62：原 H*0.048 × 0.85 缩小）
                                    邻  行 H*0.022（原 .026 再调小）白 .20（R62 更淡）
     ⚠ R60：全部文字零阴影（曲名/歌手/歌词都不要字阴影）
     ⚠ R61：曲名再大一点（0.03→0.034）；歌手加字间距（美观），
             canvas letterSpacing 仅在受支持浏览器生效，否则自动忽略。
     ⚠ R62：① drawPoster 开头 **ctx.clearRect 整幅清屏** —— 此前画布从不清屏，
       每帧半透明白字叠画 = 歌词/曲名/歌手互相"堆叠 + 毛边残影"的直接根源；
       ② 歌词改**平滑滚动**（不做 R59 的"一帧硬切行"），行随进度上下滚动。 
   ══════════════════════════════════════════════════════════════ */
window.Poster = (function () {
  var cv = null, ctx = null, W = 0, H = 0;
  var lyricLines = [], lyricCur = -1;
  /* ═══ 歌词视图（R49-22 重做）═══
     · **恒显 3 句**（上一句 / 当前句 / 下一句）—— 绘制窗口严格 center±1
       （滑动过渡瞬间最多 4 行重叠：新行淡入 + 旧行淡出，保证不突兀跳变）
     · 浏览**只由滚轮驱动**（wheelLyric ±1）—— 用户原话"通过滚轮调节，
       而不是鼠标放上去就能调节"：悬停**不**换行、**不**放大、不重置计时
     · 滚轮停在非播放行 **2 秒未操作 → 自动回正**到正在播放的行
     · 行切换 = viewPos 浮点缓动 → **滑动过渡**，不是突兀一跳
     · 无任何背板 / 行高亮块 / 提示文字 */
  var viewPos = 0;            /* 视图中心行（浮点，平滑滚动向目标行缓动） */
  var viewPosInit = false;
  var hoverIdx = -1;          /* 滚轮浏览所指的行，-1 = 无（跟随播放行） */
  var hoverAt = 0;            /* 滚轮换行时刻（2 秒回正计时） */
  var HOVER_RETURN_MS = 2000;
  /* ═══ R62：歌词平滑滚动速度 ═══
     R59 曾改成 viewPos=target「一帧硬切行」来消除拖影 —— 但用户要求的是
     **滚动式过渡**（播过的上滚出、正在播的下滚入），硬切没有滚动感。
     新实现：viewPos 向 target 做**指数缓动**（每帧逼近，速度与距离成正比）。
       距离大（整行切换）→ 起始快、收尾慢 → 平滑滑入无突兀；
       正在播放时逐帧推进 → 行缓慢随歌词"滚"过中心，而非跳变。
     LYRIC_EASE 越大滚得越快；0.16 约 200~300ms 完成一行切换。
     ⚠ 帧率感知：缓动在 rAF 主循环每帧推进（ui-init.js mainLoop 驱动
     Poster.loop），本地插值 st.position 也每帧驱动 seekLyric —— 行推进
     与实际播放进度解耦（seekLyric 只改 target，不动 viewPos 本身），
     因此不会出现"卡顿一下跳一行"（R59 硬切的旧观感）。 */
  /* ═══ R62：行槽距随 0.85× 小字号同步收窄（原 2× H*0.048 → 2× H*0.0408），
     否则滚动时行距过大、歌词显得稀。下限 30px 防过密。 */
  function LH() { return Math.max(30, H * 0.082); }   /* ≈ 2 × 焦点字号(0.0408) */
  function viewTarget() {
    if (hoverIdx >= 0) return hoverIdx;
    return lyricCur < 0 ? 0 : lyricCur;
  }
  var state = { lyricOn: false, bgMode: 'poster' };
  var FONT = '"Microsoft YaHei","PingFang SC",system-ui,sans-serif';
  var lyricUrl = '';

  function init() {
    cv = document.getElementById('poster-canvas');
    if (!cv) return false;
    ctx = cv.getContext('2d');
    resize();
    window.addEventListener('resize', resize);
    requestAnimationFrame(loop);
    return true;
  }

  function resize() {
    if (!cv) return;
    var dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    W = Math.max(2, Math.floor(cv.clientWidth * dpr));
    H = Math.max(2, Math.floor(cv.clientHeight * dpr));
    if (cv.width !== W || cv.height !== H) { cv.width = W; cv.height = H; }
  }

  /* ---- 皮肤层：画布**全透明**（R59） ----
     ⚠ R57-1 已把壁纸交给 #bgimg（封面/自定义壁纸，锐利或亚克力 blur 由 CSS 控制）；
     R58-4 曾保留「底部渐隐 + 顶部压暗」保证文字可读 —— 但用户实测这段
     rgba(6,5,16,…) 渐隐/压暗在亮壁纸上叠出**一整块灰**（"播放音乐时一大块灰"）。
     R59：彻底去掉画布上的一切蒙层 —— 画布只画文字，背景 100% 由 #bgimg 承担。
     R61：连「海报底氛围光」posterBuf 一并移除（否则锐利/亚克力都被叠一层
     雾状模糊 → "点无效果和点亚克力看起来一样"）。文字可读性由 #bgimg 的
     灰调（R61 暗化）+ 文字本身透明度层级保证。 */
  function drawSkinBase() { /* no-op：不再画任何蒙层 */ }

  function drawPoster(now) {
    drawSkinBase();
    /* ═══ R62（堆叠/毛边真凶修复）：画布**每帧整幅清屏** ═══
       ⚠⚠ 此前 drawPoster 从不 clearRect —— 画布是透明层，fillText 叠画
       半透明白字，上一帧的字永远擦不掉：切行后旧行残影仍在 → 观感
       "下一句歌词堆到上一句上面" + 所有文字毛边/发虚（半透明叠半透明）。
       R59「干脆切行」掩盖了它（硬切整行互换，残影不明显），一旦改回平滑
       滚动就必须先清屏，否则滚动轨迹糊成一条带。 */
    ctx.clearRect(0, 0, W, H);
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';

    /* 曲名 / 歌手 */
    var song = window.PosterState && window.PosterState.song;
    if (song) {
      /* 曲名 / 歌手
         ⚠ R60（用户原话回退）："歌曲和歌手的那个格式怎么还给改了，要变回去，
         不要字的阴影；歌曲名称的字号略微大一点，还是之前那种纯白色的即可；
         歌手名字号小一点、略微细一点 —— 比如标题用微软雅黑，歌手名用雅黑 Light。"
         · 曲名：微软雅黑 700、H*0.034（R61 在 0.03 基础上再大一点）、纯白 .97、**零阴影**；
         · 歌手：'Microsoft YaHei Light' 300、H*0.019（小于曲名、更细）、白 .55、
           字距 H*0.001（R62 再缩：R61 的 H*0.004 用户觉得"过宽"）、零阴影。
         ⚠ R62：曲名与歌手的垂直间距拉大 —— 曲名 y=H*0.185，歌手
         y=H*0.185+H*0.052（原 H*0.034 是 R60 曲名小字号时代的增量，曲名
         加大后两行几乎贴在一起 → "挨得太近"）。 */
      ctx.fillStyle = 'rgba(255,255,255,.97)';
      ctx.font = '700 ' + Math.round(H * 0.034) + 'px ' + FONT;
      /* ⚠ R62（painter state 泄漏修复）：歌手段会设 ctx.letterSpacing，
         若曲名段不显式复位，上一帧的歌手宽字距会残留到曲名 —— 这正是
         "歌名字距看起来宽"但代码里曲名从没设字距的原因。每次画曲名前
         强制 '0px'；再切回歌手段自己的字距。 */
      try { ctx.letterSpacing = '0px'; } catch (_) {}
      ctx.fillText(String(song.name || ''), W / 2, H * 0.185);
      ctx.fillStyle = 'rgba(255,255,255,.55)';
      /* ⚠ R61→R62：歌手字距 —— 原 H*0.004px 偏宽（用户反馈"过宽"）；
         R62 缩为 H*0.001px（曲名则 0）。canvas ctx.letterSpacing 仅
         Chromium 99+ 支持，不支持引擎 try/catch 静默忽略（无副作用）。 */
      ctx.font = '300 ' + Math.round(H * 0.019) + 'px "Microsoft YaHei Light","Microsoft YaHei",sans-serif';
      try { ctx.letterSpacing = Math.max(0, Math.round(H * 0.001)) + 'px'; } catch (_) {}
      ctx.fillText(String(song.artist || ''), W / 2, H * 0.185 + H * 0.052);
    }

    /* 当前歌词 + 上下邻行：恒 3 句 + 滑动过渡（R49） */
    if (state.lyricOn && lyricLines.length && lyricCur >= 0) {
      drawLyricView(now);
    }
  }

  /* ═══ 歌词视图：滚动式渲染（R62 重做，替代 R59 的"一帧硬切行"）═══
     用户原话：「歌词的问题是不要做切换那种，要做滚动，播过的滚到上面去，
     正在播的从下面滚上来，过渡要平滑，而且帧率要上去，不然还是有拖尾，
     然后所有的歌词字号整体小一些（85% 左右吧），没有在播放的歌词还要再淡一点。」
     设计：
       · **清屏先行**（drawPoster 开头 clearRect）→ 无叠画残影；
       · viewPos 向 target **指数缓动**（非硬切）：整行切换时旧行平滑上滚、
         新行从下方滚入；播放中逐帧微调 → 行随歌词"滚过"中心。
         每帧都执行（rAF 驱动 + seekLyric 每帧同步 target）→ 高帧率无拖尾；
       · 字号整体 ×0.85：焦点行 H*0.048→H*0.0408，浏览放大行 ×1.32 → 正常
         邻行 H*0.026→H*0.022；
       · 邻行（非播放）透明度 .32 → **.20**（再淡一点，主次更分明）；
       · 焦点行恒 1.0 纯白、零阴影（同 R59 契约）；
       · 滚轮浏览仍可用：浏览行放大 + 2s 无操作自动回正（语义保留）。 */
  var LYRIC_EASE = 0.16;      /* 每帧向目标逼近比例：越大滚动越快 */
  /* ═══ R67-6：歌词识别区缩到三行宽高+5% ═══
     每帧 drawLyricView 测 3 可见行的最大文本宽度（measureText）→ 写入模块级
     _lastBoxW/_lastBoxH。外部通过 lyricBoxW()/lyricBoxH()/lyricBoxCenter()
     拿到「刚好包住 3 行歌词」的矩形 → lyricZone 改为命中这个矩形 + 5% 边距，
     而不再是固定 68%×46% 视口百分比（太大，按钮以外的暗区也算歌词区）。 */
  var _lastBoxW = 0, _lastBoxH = 0, _lastBoxCx = 0, _lastBoxCy = 0;
  function drawLyricView(now) {
    var cy = H * 0.52, lh = LH();
    var target = viewTarget();
    if (!viewPosInit){ viewPos = target; viewPosInit = true; }
    else {
      /* 指数缓动：距离大滚动快、接近时慢收 —— 平滑无跳变。
         已到目标（|Δ|<1e-4）直接咬死，避免浮点余振。 */
      var dv = target - viewPos;
      if (Math.abs(dv) > 1e-4) viewPos += dv * LYRIC_EASE;
      else viewPos = target;
    }
    var center = Math.round(viewPos);
    var focus = hoverIdx;                 /* 放大/置顶的行：滚轮浏览行优先 */
    var maxW = 0;
    for (var i = center - 1; i <= center + 1; i++) {
      if (i < 0 || i >= lyricLines.length) continue;
      var dist = Math.abs(i - viewPos);
      var y = cy + (i - viewPos) * lh;
      var isFocus = (i === focus);
      /* ⚠ R62：整体字号 ×0.85 —— 焦点行（播放/浏览）H*0.048×0.85≈H*0.0408；
         浏览行再 ×1.32（R59 语义：只有滚轮浏览行放大）；邻行 H*0.026×0.85≈0.022。 */
      var size = Math.round(isFocus ? H * 0.0408 * 1.32
                            : (dist < 0.5 ? H * 0.0408 : H * 0.022));
      /* ⚠ R62：透明度再降 —— 焦点行 1.0 纯白；邻行 .32 → **.20**（更淡）。 */
      var alpha = (dist < 0.5 || isFocus) ? 1 : 0.20;
      ctx.font = '700 ' + size + 'px ' + FONT;
      ctx.fillStyle = 'rgba(255,255,255,' + alpha.toFixed(3) + ')';
      /* ⚠ R59/R62：零阴影 —— 白字直接写（清屏后无残影，不需要阴影压边） */
      var txt = String(lyricLines[i].text || '');
      ctx.fillText(txt, W / 2, y);
      /* R67-6：测本行实际像素宽度 → 3 行取最大；首字符比 %s 显示的还宽时 margin 也算进 */
      var w = ctx.measureText(txt).width;
      if (w > maxW) maxW = w;
    }
    /* 高 = 3 行 × lh（焦点行 1.32× 行高但字本身接近 lh）—— 保守取 3*lh 包含浏览放大 */
    _lastBoxW = maxW;
    _lastBoxH = 3 * lh;
    _lastBoxCx = W / 2;
    _lastBoxCy = cy;
  }

  function loop() {
    requestAnimationFrame(loop);
    if (!ctx || !W || !H) return;
    var now = performance.now();
    /* 2 秒回正：滚轮浏览停在非播放行超过 HOVER_RETURN_MS 且未操作 → 自动跳回播放行 */
    if (hoverIdx >= 0 && hoverIdx !== lyricCur && hoverAt
        && now - hoverAt > HOVER_RETURN_MS){
      hoverIdx = -1; hoverAt = 0;
    }
    drawPoster(now);
  }

  /* ---- LRC 解析（多时间标签 + 行时长推断，默认 4.8s，clamp 0.45-12s）---- */
  function parseLRC(text) {
    var out = [];
    var re = /\\[(\\d{1,2}):(\\d{1,2})(?:\\.(\\d{1,3}))?\\]/g;
    String(text || '').split(/\\r?\\n/).forEach(function (raw) {
      re.lastIndex = 0;
      var tags = [], m;
      while ((m = re.exec(raw))) tags.push(m);
      if (!tags.length) return;
      var txt = raw.replace(re, '').trim();
      if (!txt) return;
      tags.forEach(function (tg) {
        var mm = parseInt(tg[1], 10) * 60 + parseInt(tg[2], 10);
        var frac = tg[3] ? parseFloat('0.' + tg[3]) : 0;
        out.push({ time: mm + frac, text: txt });
      });
    });
    out.sort(function (a, b) { return a.time - b.time; });
    for (var i = 0; i < out.length; i++) {
      var dur = (i + 1 < out.length) ? (out[i + 1].time - out[i].time) : 4.8;
      out[i].dur = Math.max(0.45, Math.min(12, dur));
    }
    return out;
  }

  return {
    init: init,
    /* 供主脚本调用 */
    /* ⚠⚠ R61：setCover 变为 no-op（保留 API 契约，主脚本多处仍调用）。
       旧实现载入封面画进 posterBuf 铺氛围光 —— R61 已移除该绘制（它是
       "锐利/亚克力看起来一样"的伪亚克力来源）；#bgimg 才是皮肤唯一内容位，
       歌词海报画布只画文字，不再需要任何封面图像引用。 */
    setCover: function () { return; },
    setLyric: function (text) {
      lyricLines = parseLRC(text);
      lyricCur = lyricLines.length ? 0 : -1;
      viewPos = 0; viewPosInit = false;   /* 切歌 → 视图直接对齐（不播滑动） */
      hoverIdx = -1; hoverAt = 0;
    },
    /* 后端 /api/lyrics 返回**已解析**的 [{time(秒), text}] —— 直接吃，不要二次 LRC 解析 */
    setLyricLines: function (lines) {
      var arr = Array.isArray(lines) ? lines : [];
      lyricLines = arr
        .filter(function (l) { return l && l.text != null; })
        .map(function (l) { return { time: +l.time || 0, text: String(l.text), dur: +l.dur || 4.8 }; })
        .sort(function (a, b) { return a.time - b.time; });
      lyricCur = lyricLines.length ? 0 : -1;
      viewPos = 0; viewPosInit = false;   /* 切歌 → 视图直接对齐（不播滑动） */
      hoverIdx = -1; hoverAt = 0;
      return lyricLines.length;
    },
    /* 按播放进度推进当前行（供每帧/每次轮询调用） */
    seekLyric: function (ms) {
      if (!lyricLines.length) { lyricCur = -1; return; }
      var t = ms / 1000, idx = -1;
      for (var i = 0; i < lyricLines.length; i++) {
        if (lyricLines[i].time <= t) idx = i; else break;
      }
      lyricCur = idx;
    },
    getLyric: function () {
      return { lines: lyricLines, cur: lyricCur };
    },
    setLyricOn: function (on) {
      state.lyricOn = !!on;
      if (!state.lyricOn) { hoverIdx = -1; hoverAt = 0; }
    },
    isLyricOn: function () { return state.lyricOn; },
    /* ═══ 滚轮浏览（R49-22 重做，替代 R48 的 pickOn 浏览态）═══
       · 浏览**只由滚轮驱动**：index.html wheel → wheelLyric(±1)
       · 浏览行**放大**（不改色、无背板）；播放行跟随时不放大当前行
       · 停在非播放行 2 秒未操作 → loop() 自动回正（HOVER_RETURN_MS=2000）
       · 点击 → lyricHitTest(clientY) 行号 → seekToMs
       · ⚠ 鼠标悬停不换行（用户原话"通过滚轮调节，而不是鼠标放上去就能调节"） */
    lyricCount: function () { return lyricLines.length; },
    /* R67-6：暴露「3 行歌词刚好包住」的矩形中心点 + 宽高 —— 给 ui-orbit.js
       的 lyricZone 用（缩到三行宽+5%，不再用 0.68×0.46 视口百分比）。
       返回 cx/cy 是画布坐标，外部自行按 #poster-canvas 的 getBoundingClientRect
       转屏幕坐标。 */
    lyricBoxW:   function () { return _lastBoxW; },
    lyricBoxH:   function () { return _lastBoxH; },
    lyricBoxCx:  function () { return _lastBoxCx; },
    lyricBoxCy:  function () { return _lastBoxCy; },
    setLyricHover: function (i) {
      var n = (i == null || i < 0 || i >= lyricLines.length) ? -1 : i;
      if (n !== hoverIdx){
        hoverIdx = n;
        hoverAt = n >= 0 ? performance.now() : 0;   /* 每次换行都重置 2 秒计时 */
      } else if (n >= 0){
        /* 停在同一行（滚轮被边界 clamp）：不刷新计时 —— 停留超时照样回正 */
      }
      return hoverIdx;
    },
    lyricHover: function () { return hoverIdx; },
    /* 滚轮换行：以当前浏览行（无则播放行）为基准 ±1；2 秒无操作自动回正 */
    wheelLyric: function (d) {
      if (!lyricLines.length) return -1;
      var base = hoverIdx >= 0 ? hoverIdx : (lyricCur < 0 ? 0 : lyricCur);
      return this.setLyricHover(Math.max(0, Math.min(lyricLines.length - 1, base + (d | 0))));
    },
    /* clientY → 行号：以**视图中心行**（浮点 viewPos）为基准换算，滑动态下也准。
       ⚠ R49-22 只用于点击 seek 命中测试，不再驱动换行。 */
    lyricHitTest: function (clientY) {
      if (!cv || !lyricLines.length) return -1;
      var r = cv.getBoundingClientRect();
      if (!r.height) return -1;
      var y = (clientY - r.top) / r.height * H;
      var i = Math.round(viewPos + (y - H * 0.52) / LH());
      return (i >= 0 && i < lyricLines.length) ? i : -1;
    },
    /* 该行的起始时间（毫秒）；越界返回 null */
    lyricTimeMs: function (i) {
      var ln = lyricLines[i];
      if (!ln) return null;
      return Math.max(0, Math.round((+ln.time || 0) * 1000));
    },
    setMode: function (m) { state.bgMode = m; },
    parseLRC: parseLRC
  };
})();
`;
    /* preamble：ctx 既是共享对象，也让老代码里的裸标识符可解析。
       new Function 非严格模式，with 可用 —— 这是"老代码零改动"的关键。 */
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + "  /* \u628a\u672c\u6a21\u5757\u9876\u5c42\u58f0\u660e\u53d1\u5e03\u5230 ctx\uff08\u4f9b\u540e\u7eed\u6a21\u5757 use\uff09 */\n  const __defs = [\"cv\", \"lyricLines\", \"viewPos\", \"viewPosInit\", \"hoverIdx\", \"hoverAt\", \"HOVER_RETURN_MS\", \"LH\", \"viewTarget\", \"state\", \"FONT\", \"lyricUrl\", \"init\", \"resize\", \"drawSkinBase\", \"drawPoster\", \"LYRIC_EASE\", \"_lastBoxW\", \"drawLyricView\", \"loop\", \"parseLRC\"];\n  for (const __n of __defs) {\n    try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {}\n  }" + '\n}');
    return __run(ctx);
  },
};
