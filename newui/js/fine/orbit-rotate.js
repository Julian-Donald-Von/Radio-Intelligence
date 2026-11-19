/* newui/js/fine/orbit-rotate.js — 「圆盘轮转/动画」自定义空间模块（源自 ui-orbit.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'orbit-rotate',
  source: 'ui-orbit.js',
  space: '圆盘轮转/动画',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  /* ⚠ POS_LIST 是**位置数组**（不是 mode→坐标 的映射），每个 mode 用
     curPos[m]（0/1/2）索引进去 —— 这是老 UI 轮转算法的核心数据结构。
     [0] 中间位（屏幕右缘、视觉中心）  [1] 上  [2] 下
     轮转 = 全体 curPos += delta 再 %3，谁落到 0 谁就是当前栏目。 */
  const ORBIT_POS_LIST = [
    { x: () => innerWidth + orbitGeo().dx - ORBIT_R - orbitGeo().R1 + ORBIT_O_SHIFT, y: () => innerHeight / 2 },
    { x: () => innerWidth - ORBIT_R + ORBIT_O_SHIFT, y: () => innerHeight / 2 - orbitGeo().dy },
    { x: () => innerWidth - ORBIT_R + ORBIT_O_SHIFT, y: () => innerHeight / 2 + orbitGeo().dy },
  ];

  let orbitActive = 'wander';
  let orbitShown = false;
  let orbitAnim = { active:false, from:{}, to:{}, t0:0, dur:600 };
  /* 行进中的装饰性自转（原 UI L1880 rot[] + L1930 rotate(rot[m]*15deg)）。
     ⚠⚠ 2026-09-06 用户实测："图标转过来没有回正，还是歪的"。
     根因有两层：
       ① 我移植时**漏了老 UI L1966 的 \`rot[newMode] = 0\`** —— 那一行就是"回正"。
       ② 就算补上，老 UI 的 rot 是 \`(rot ± 1 + 6) % 6\` 的**累加器**，
          停在上下位的按钮会一路歪到 5×15° = **75°**，本身就很难看。
     所以本实现改为**瞬时摆动**：行进途中随进度摆出一个倾角，
     到中点最大、**落位必定回到 0°** —— 静止时三个图标永远正着。 */
  const ORBIT_SPIN_DEG = 20;   // 行进中途的最大倾角
  let orbitSpinDir = 1;        // +1 顺时针 / -1 逆时针，跟随轮转方向
  function orbitSpinDeg(m){
    if (!orbitAnim.active) return 0;                 // 静止 → 恒为 0（正）
    /* 原 UI L1966 \`rot[newMode] = 0\`：正在转进来的那一个**全程保持正着**
       （它是焦点，倾斜会影响可读性），只有让位的另外两个滚。 */
    if (m === orbitActive) return 0;
    const t = Math.min(1, (performance.now() - orbitAnim.t0) / orbitAnim.dur);
    /* sin(πt)：t=0 → 0，t=0.5 → 峰值，t=1 → 0。两头都是 0 = 必定回正 */
    return Math.sin(Math.PI * t) * ORBIT_SPIN_DEG * orbitSpinDir;
  }
  /* curPos[mode] = 该按钮当前在 POS_LIST 中的槽位（0=中间/当前栏目） */
  const orbitCurPos = { wander:0, fav:1, plist:2 };
  function orbitAngOf(px, py){
    const oc = orbitOC();
    return Math.atan2(py - oc.y, px - oc.x);
  }
  function orbitArcPoint(a){
    const oc = orbitOC();
    return { x: oc.x + orbitGeo().R1 * Math.cos(a), y: oc.y + orbitGeo().R1 * Math.sin(a) };
  }
  function orbitPlace(m, cx, cy){
    const el = orbitEls[m];
    if (!el) return;
    const active = orbitActive === m;
    el.style.zIndex = active ? 60 : 30;
    el.style.opacity = active ? 1 : .72;
    /* 视觉零阈值：sin(π·1) ≈ 1.2e-16，浮点残值会让 transform 里永远挂着
       \`rotate(0.00deg)\`（无害但没必要），低于 0.05° 一律当正。 */
    const spin = orbitSpinDeg(m);
    const useSpin = Math.abs(spin) > 0.05 ? spin : 0;
    el.style.transform = \`translate(\${cx - ORBIT_R}px, \${cy - ORBIT_R}px)\`
      + (useSpin ? \` rotate(\${useSpin.toFixed(2)}deg)\` : '');
    el.classList.toggle('active', active);
  }
  /* 当前角度：动画中取插值，静止时取 POS_LIST[curPos] 的角度（老 UI L1911-1919） */
  function orbitCurrentAng(m){
    if (orbitAnim.active){
      const t = Math.min(1, (performance.now() - orbitAnim.t0) / orbitAnim.dur);
      const e = 1 - Math.pow(1 - t, 3);
      return orbitAnim.from[m] + (orbitAnim.to[m] - orbitAnim.from[m]) * e;
    }
    const p = ORBIT_POS_LIST[orbitCurPos[m]];
    return orbitAngOf(p.x(), p.y());
  }
  /* ⚠⚠ 致命坑（2026-09-06 修复，表现为「orbit 按钮完全不动」）：
     旧写法 \`if (!orbitLayoutRAF) orbitLayoutRAF = requestAnimationFrame(orbitLayout)\`
     —— **rAF 返回的是正整数 id，永远 truthy**，所以 \`!orbitLayoutRAF\` 恒为 false，
     第一帧之后再也不会排下一帧：既没有轮转动画，resize 后也不重排。
     必须在回调**入口把 id 清零**，否则整条 rAF 链只跑一次。 */
  let orbitLayoutRAF = 0;
  function orbitRequestLayout(){
    if (!orbitLayoutRAF) orbitLayoutRAF = requestAnimationFrame(orbitLayout);
  }
  function orbitLayout(){
    orbitLayoutRAF = 0;                 // ⚠ 先清零，否则下一帧永远排不上
    if (orbitAnim.active){
      const t = Math.min(1, (performance.now() - orbitAnim.t0) / orbitAnim.dur);
      if (t >= 1){
        /* ⚠ 收尾必须**先**置 false 再走下面的静态分支：
           若 rAF 在半路被掐断（切标签页/掉帧），按钮会永远停在倾角上。
           落到静态分支 = 精确槽位 + spin 恒 0 = 必定回正。 */
        orbitAnim.active = false;
      } else {
        const e = 1 - Math.pow(1 - t, 3);
        ORBIT_MODE_ORDER.forEach(m => {
          const a = orbitAnim.from[m] + (orbitAnim.to[m] - orbitAnim.from[m]) * e;
          const p = orbitArcPoint(a);
          orbitPlace(m, p.x, p.y);
        });
      }
    }
    if (!orbitAnim.active){
      ORBIT_MODE_ORDER.forEach(m => {
        const p = ORBIT_POS_LIST[orbitCurPos[m]];
        orbitPlace(m, p.x(), p.y());
      });
    }
    /* 动画期间持续排帧；静止时按需（resize / rotate）触发，不做空转 rAF */
    if (orbitAnim.active) orbitRequestLayout();
  }
  window.addEventListener('resize', orbitRequestLayout);
  /* ⚠ 必须**同步**跑一次首帧：rAF 在页面未被合成时（后台标签/离屏/首帧前）
     可能根本不触发，只靠 rAF 会出现"按钮停在 transform 未设置的原位"。
     同步一次保证加载完立刻就位，rAF 只负责后续动画。 */
  orbitLayout();
  orbitRequestLayout();
  /* 调试/自检钩子（对齐老 UI L1877 的 window.__orbitZone）：
     __orbitZone 供卡片架取同心弧心；__orbit 供验证套件读状态与手动排帧。 */
  window.__orbitZone = function(){
    const oc = orbitOC();
    return { x: oc.x, y: oc.y, r: orbitGeo().R2 };
  };
  window.__orbit = {
    state: function(){
      return {
        curPos: { wander:orbitCurPos.wander, fav:orbitCurPos.fav, plist:orbitCurPos.plist },
        active: orbitActive,
        animating: !!orbitAnim.active,
        /* 每个按钮**实际被应用**的倾角（度）。old UI 的 newMode 恒为 0。 */
        spin: {
          wander: +orbitSpinDeg('wander').toFixed(2),
          fav:    +orbitSpinDeg('fav').toFixed(2),
          plist:  +orbitSpinDeg('plist').toFixed(2)
        }
      };
    },
    layout: function(){ orbitLayout(); },
    /* 卡片架中心 x（老 UI shelfCenterX）—— 验证套件据此断言"卡片贴住 orbit 左缘 22px" */
    shelfCenterX: function(){ return shelfCenterX(); }
  };

  /* ═══════════ 轮转 rotate(delta) —— 老 UI L1959-1992 原样移植 ═══════════
     delta=1：下面那个转到中间（内容上滑）→ 角度逆时针增
     delta=2：上面那个转到中间（内容下滑）→ 角度顺时针减
     做法：全体 curPos += delta 后 %3，谁落到槽位 0 谁成为当前栏目。 */
  function orbitRotate(delta){
    delta = ((delta % 3) + 3) % 3;
    if (delta === 0) return;
    const from = {};
    ORBIT_MODE_ORDER.forEach(m => { from[m] = orbitCurrentAng(m); });
    ORBIT_MODE_ORDER.forEach(m => { orbitCurPos[m] = (orbitCurPos[m] + delta) % 3; });
    const newMode = ORBIT_MODE_ORDER.find(m => orbitCurPos[m] === 0);
    if (newMode) orbitActive = newMode;
    /* 方向一致性：下滑(delta=2)统一顺时针(角减小)，上滑(delta=1)统一逆时针(角增大) */
    const cw = (delta === 2);
    /* 装饰自转的摆动方向跟随轮转方向（clockwise → 正向摆） */
    orbitSpinDir = cw ? 1 : -1;
    const to = {};
    ORBIT_MODE_ORDER.forEach(m => {
      const p = ORBIT_POS_LIST[orbitCurPos[m]];
      let d = orbitAngOf(p.x(), p.y()) - from[m];
      if (cw){ while (d > 0) d -= 2 * Math.PI; }
      else   { while (d < 0) d += 2 * Math.PI; }
      to[m] = from[m] + d;
    });
    orbitAnim = { active:true, from, to, t0:performance.now(), dur:600 };
    orbitRequestLayout();
    /* 切栏目 → 刷新内容（与老 UI refreshColumn() 同语义） */
    if (newMode) onOrbitModeChange(newMode);
  }
  /* 点击：点上面的转下来、点下面的转上去、点中间无动作（老 UI L2004-2010） */
  ORBIT_MODE_ORDER.forEach(m => {
    orbitEls[m].addEventListener('click', () => {
      const delta = ((0 - orbitCurPos[m]) % 3 + 3) % 3;
      if (delta !== 0) orbitRotate(delta);
    });
  });
  /* orbitInZone(x,y): 以 O 为圆心、R2 为半径的**完整圆盘**（含圆弧内部，老 UI L1999-2003） */
  function orbitInZone(x, y){
    const oc = orbitOC();
    const dx = x - oc.x, dy = y - oc.y;
    const r = orbitGeo().R2;
    return dx * dx + dy * dy <= r * r;
  }
  /* cardsInZone(x,y): 以 O 为圆心、R_CARD 为半径、宽 CARD_W 的**同心圆环带**（R47）
     "鼠标进入卡片宽度所在的那个同心圆弧" —— 与 shelfCenterX()/R_CARD 完全同源。
     ⚠ 必须用 |dist - R_CARD| ≤ CARD_W/2 的**环形**而非整圆：
       否则半径 R_CARD 上**屏幕外**的一段弧也会误触发（用户从屏幕左边缘划过也算"在卡片上"）。
     ⚠ 还要限制 dy 落在可见弧带内（sin 角度分量 ≤ 0.85 ≈ 5.6 张卡 ±58°），
       否则屏幕角落的点（dy 大、dx 大）也能凑出 |dist - R_CARD| ≤ CARD_W/2。 */
  function cardsInZone(x, y){
    const oc = orbitOC();
    const dx = x - oc.x, dy = y - oc.y;
    const dist = Math.sqrt(dx * dx + dy * dy);
    const R_CARD = Math.max(160, oc.x - shelfCenterX());
    if (Math.abs(dist - R_CARD) > CARD_W / 2) return false;
    return Math.abs(dy) <= R_CARD * 0.85;
  }
  /* ═══ 3 秒自动隐藏（R47：5s → R57-8：3s → R65：**区内静止也算**）═══
     触发区域 = orbit 圆盘 ∪ 卡片同心圆环。

     ⚠ R65 语义变更（用户原话"对于 orbit 和歌曲卡片，鼠标在识别区三秒钟
       无动作也会自动隐藏"）：
       旧：只有**离开**触发区才起 3s 计时；鼠标一直停在区内 → 永不隐藏。
       新：**区内任何动作**（移动 / 滚轮 / 点击）都算"有动作"→ 立即显示并重开
           3s 空闲计时；一旦静止满 3s **即便鼠标仍停在区内**也隐藏。
           离开触发区仍是 3s 后隐藏，且**区外移动不刷新计时**
           （否则"在外面乱动就永远不隐藏"）。 */
  const AUTO_HIDE_MS = 3000;
  let autoHideTimer = 0;
  function armAutoHide(){
    if (autoHideTimer) clearTimeout(autoHideTimer);
    autoHideTimer = setTimeout(() => {
      autoHideTimer = 0;
      $('orbits').classList.remove('show');
      $('cards').classList.add('autohide');
    }, AUTO_HIDE_MS);
  }
  function pokeShow(){
    $('orbits').classList.add('show');
    $('cards').classList.remove('autohide');
    armAutoHide();           // ⚠ 区内也起计时：3s 无动作即隐藏
  }
  function pokeHide(){
    if (autoHideTimer) return;   // 区外：离开那刻起一次，不随区外移动刷新
    armAutoHide();
  }
  /* ═══ 左侧栏目提示（R49-30 重做）═══
     ⚠ 用户原话："当 orbit 滚到『我的曲库』对应栏目时，提示应出现约 3 秒后消失
       （勿保持永久隐藏/永久可见）。"
     旧实现（R46）是"鼠标靠近 column-panel 就淡入"—— 那是 hover 面板交互，
     不是"orbit 落到某栏目"的提示，而且播放中会在你扫过屏幕左边时冒出来挡路。
     现在：**只在 orbit 轮转/点击落到新栏目时**闪示约 3 秒，然后自动淡出。
     未播放的空闲态保持显示（信息层），一旦开播立即淡出、只随栏目切换闪示。 */
  let colTimer = 0;
  function pokeColumn(ms){
    const el = $('column-panel');
    if (!el) return;
    el.classList.add('show');
    if (colTimer){ clearTimeout(colTimer); colTimer = 0; }
    colTimer = setTimeout(() => {
      colTimer = 0;
      /* 未播放时空闲态恒显（信息层不打扰）；播放中栏目提示到期即淡出 */
      if (!st.playing) return;
      el.classList.remove('show');
    }, ms || 3000);
  }
  function syncColumnPanel(){
    const el = $('column-panel');
    if (!el) return;
    /* 空闲（未播放）恒显作为信息层；播放中由 pokeColumn（orbit 切栏目）控制 */
    if (!st.playing){ el.classList.add('show'); }
  }
  /* 鼠标移动 → ① 负责 orbits + cards 的 5 秒触发区自动隐藏；
     ② **R53：鼠标靠近左侧栏目提示（column-panel）本身 → 立即复活**。
        R49-30 把提示改成"只在 orbit 落到栏目时闪 3s"，结果用户鼠标离开再
        靠近（想再瞄一眼"我的曲库/歌单中心"标题）永远不再出现 —— 用户点名。
        修复：column-panel 进入其包围盒热区（含周边 12px 手感）就 pokeColumn，
        但**不打断**正在播放的 3s 闪示节奏：接近即延长/重现，离开由各自逻辑收走。 */
  function columnProbeZone(x, y){
    const el = $('column-panel');
    if (!el) return false;
    try {
      const r = el.getBoundingClientRect();
      if (!r.width && !r.height) return false;
      return x >= r.left - 16 && x <= r.right + 16
          && y >= r.top - 12 && y <= r.bottom + 12;
    } catch (_) { return false; }
  }
  window.addEventListener('mousemove', e => {
    const inZ = orbitInZone(e.clientX, e.clientY) || cardsInZone(e.clientX, e.clientY);
    if (inZ) pokeShow(); else pokeHide();
    /* R53：鼠标重新靠近左侧栏目提示 → 立即复活。
       · 播放中：pokeColumn(3000) 闪示 3s 后自动淡出（不打断 R49-30 节奏）
       · 空闲中：pokeColumn 只加 show 不移除（空闲态本就恒显，无副作用）
       ⚠ 不能加 st.playing 门控：离开又回来时若无 show 类，靠几何区域判定复活。 */
    try {
      if (columnProbeZone(e.clientX, e.clientY)) pokeColumn(3000);
    } catch (_) {}
  });
  /* R65：点击也算「有动作」—— 区内点一下立即复活并重开 3s 空闲计时。
     （捕获阶段 + 只读几何判定，不拦截任何事件） */
  window.addEventListener('pointerdown', e => {
    if (orbitInZone(e.clientX, e.clientY) || cardsInZone(e.clientX, e.clientY)) pokeShow();
  }, true);
  /* orbit 落到某栏目（点击 orbit 按钮 / 滚轮轮转）→ 左侧栏目提示闪示约 3s */
  function columnFlashOnMode(){
    if (st.playing) pokeColumn(3000);
    else syncColumnPanel();        // 空闲态：恒显信息层
  }
  /* 开始播放 → 空闲态信息层淡出；播放中若没在闪示周期内则隐藏 */
  function hideColumnIfIdle(){
    const el = $('column-panel');
    if (!el) return;
    if (colTimer) return;          // 正在 3s 闪示：交给计时器收走
    el.classList.remove('show');
  }
  /* 播放状态变化（WS playing / 轮询）→ 同步 column-panel：
     · 停止播放 → 空闲信息层恒显
     · 开始播放 → 隐藏（除非正在 3s 闪示周期内） */
  function syncColumnByPlaying(playing){
    if (!playing){ syncColumnPanel(); }
    else { hideColumnIfIdle(); }
  }

  /* ═══════════ 滚轮路由（老 UI L4157-4177 原样移植） ═══════════
     优先级（R46-5 在最前面加了 ⓪ 歌词选择）：
       ⓪ 歌词选择态（鼠标停在歌词带内）→ 上下换行
       ① 鼠标真正悬停在卡片架某张卡(.scard)上 → 浏览卡片
       ② orbit 大圆盘（**包含按钮围成的圆弧内部**）→ 轮转三按钮
       ③ 其余区域 → **不拦截**，允许设置/歌词/队列/详情等面板内部原生滚动
     ⚠ 绝不能用 #shelf 容器包围盒判定：它铺满右缘全高，会吞掉圆弧内部的滚轮。
       必须用 document.elementFromPoint 精确到卡片元素。 */
  /* ═══ R65：独立交互区（面板 / 弹层）═══
     指针落在这些面板内部时，滚轮与点击**只归面板自己**：
     绝不翻歌词、绝不步进卡片、绝不轮转 orbit。
     ⚠ 这是「设置页一滚，后面的 orbit 和卡片也跟着转」的根因防护：
       orbitInZone / cardsInZone 是**纯几何**判定，右侧 380px 的设置抽屉在几何上
       正好压在 orbit 圆盘上 → 光靠"指针在抽屉内就放行"仍会漏到 orbitRotate。
       故统一用一个前置闸门：命中独立交互区（或抽屉开着）→ 直接 return。 */
  const PANEL_SEL = '#side-panel, #settings-panel, #theme-panel, #qr-modal, #aibar, #eq-panel';
  function isolatedZone(x, y){
    /* 抽屉开着 → 抽屉内外都不许动主界面（用户点名"设置页滚动别带背景"） */
    try { if (anyDrawerOpen()) return true; } catch (_) {}
    try {
      const el = document.elementFromPoint(x, y);
      return !!(el && el.closest(PANEL_SEL));
    } catch (_) { return false; }
  }
  function shelfCardZone(x, y){
    const el = document.elementFromPoint(x, y);
    return !!el && !!el.closest('.scard');
  }
  /* 歌词带：海报上歌词所在的横带（poster.js 画在 H*0.52 附近）。
     ⚠ #bg / canvas 是 pointer-events:none，只能按几何判定，不能靠 elementFromPoint。
     ⚠ 横向要**避开卡片架**，否则鼠标在卡片上滚会同时翻歌词。
     ⚠ R67-6：识别区缩到「3 行歌词刚好包住」的矩形 + 5% 上下边距（旧实现是
       0.68×0.46 视口百分比 → 暗区也算歌词带，按钮外一圈能跳歌）。 */
  function lyricZone(x, y){
    /* ⚠⚠ R67-6 修复：const P 曾只声明在下方第一段 try{} 里 —— const 是
       块级作用域，第二段 try{} 读 P 直接 ReferenceError → catch → 恒 false
       → 歌词识别区**完全失效**（wheel/click 全不响应，且不报错，藏得深）。
       提到函数顶层，两段共用。 */
    let P = null;
    try { P = window.Poster; } catch (_) {}
    try {
      if (!P || !P.isLyricOn || !P.isLyricOn() || !P.lyricCount || P.lyricCount() < 1) return false;
    } catch (_) { return false; }
    /* R67-6：几何用 Poster 暴露的「3 行宽高 + 中心」+ 5% 边距。
       _lastBoxW/H 是画布坐标系 → 经 #poster-canvas 的 getBoundingClientRect 转到屏幕坐标。 */
    let boxW, boxH, boxCx, boxCy;
    try {
      boxW = P.lyricBoxW(); boxH = P.lyricBoxH();
      boxCx = P.lyricBoxCx(); boxCy = P.lyricBoxCy();
    } catch (_) { return false; }
    if (!boxW || !boxH) return false;   /* 还没画过第一帧 */
    const cv = document.getElementById('poster-canvas');
    if (!cv) return false;
    const r = cv.getBoundingClientRect();
    /* 画布 → 屏幕：scaleX = r.width/W, scaleY = r.height/H；offset = r.left/top */
    const sx = (r.width || 0) > 0 ? r.width : 1;
    const sy = (r.height || 0) > 0 ? r.height : 1;
    const W = (cv.width  || sx);
    const H = (cv.height || sy);
    const scCx = r.left + (boxCx / W) * sx;
    const scCy = r.top  + (boxCy / H) * sy;
    const scW  = (boxW / W) * sx * 1.05;   /* +5% 横向（左右各 2.5%）*/
    const scH  = (boxH / H) * sy * 1.05;   /* +5% 上下 */
    if (x < scCx - scW / 2 || x > scCx + scW / 2) return false;
    if (y < scCy - scH / 2 || y > scCy + scH / 2) return false;
    /* ⚠⚠ R53 独立识别区：side-panel 打开时整块面板是**独立交互区**，
       其关闭叉/滚动条/行按钮绝不能被当成"歌词带"——
       否则点 sp-close 会命中 lyricHitTest → seek 到某句歌词（用户点名）。
       先在元素层排除（面板是实 DOM），再退几何。 */
    try {
      const el = document.elementFromPoint(x, y);
      /* ⚠ R56：右侧两张玻璃抽屉（设置卡/换肤卡）也是独立识别区，与歌词带互斥。
         ⚠ R65：统一用 isolatedZone 的面板清单（含 #qr-modal），
           歌单详情面板放大居中后必然压住歌词带 —— 元素层排除是唯一可靠手段
           （几何层排除不了：它就是盖在歌词上面）。 */
      if (el && el.closest(PANEL_SEL)) return false;
    } catch (_) {}
    try { if (x > shelfCenterX() - 170) return false; } catch (_) {}
    return true;
  }
  window.addEventListener('wheel', e => {
    /* ⓪ R65：独立交互区（设置/换肤抽屉、侧边面板、扫码弹层、口播条）→
       只交浏览器原生滚动，绝不触发翻歌词 / 卡片步进 / 圆盘轮转。
       ⚠ 旧实现写成「抽屉开着且指针不在抽屉内才 return」，指针**在抽屉内**时
         会继续往下走 —— 而 orbit 识别区是纯几何判定，抽屉在几何上压着 orbit，
         于是在设置页滚动时背景 orbit 照样转（用户点名）。现在一律闸门拦截。 */
    if (isolatedZone(e.clientX, e.clientY)) return;
    /* ⓪' 歌词浏览（R49-22）：滚轮换行（浏览行放大），**2 秒无操作自动回正**。
       ⚠ 浏览只由滚轮驱动 —— 鼠标悬停不换行不放大（用户原话"通过滚轮调节，
       而不是鼠标放上去就能调节"）。 */
    if (lyricZone(e.clientX, e.clientY)){
      const P = window.Poster;
      P.wheelLyric(e.deltaY > 0 ? 1 : -1);
      return;
    }
    if (shelfCardZone(e.clientX, e.clientY)){
      pokeShow();                         // R65：区内活动 → 重开 3s 空闲计时
      step(e.deltaY > 0 ? 1 : -1);        // 卡片架浏览：下滑下一张
      return;
    }
    if (orbitInZone(e.clientX, e.clientY)){
      pokeShow();                         // R65：同上（滚轮也是"动作"）
      orbitRotate(e.deltaY > 0 ? 2 : 1);  // 下滑：上面转到中间；上滑：下面转到中间
      return;
    }
    /* 其余区域：不切歌，交给浏览器原生滚动 */
  }, { passive: true });

  /* 点击歌词带 → 跳到那一行并立即回正（"点击歌词跳到对应位置"）。force=true 立即生效。
     ⚠ R49-22：**没有** mousemove 悬停联动 —— 换行只认滚轮（wheelLyric），
     鼠标靠近歌词只放大视觉不变行；点击是唯一鼠标驱动 seek。 */
  window.addEventListener('click', e => {
    const P = window.Poster;
    if (!P || !P.lyricHitTest) return;
    /* R65：与滚轮同一道闸门 —— 面板/弹层上、或抽屉开着时，点击不落到歌词 seek */
    if (isolatedZone(e.clientX, e.clientY)) return;
    if (!lyricZone(e.clientX, e.clientY)) return;
    const i = P.lyricHitTest(e.clientY);
    if (i < 0) return;
    const ms = P.lyricTimeMs(i);
    if (ms == null) return;
    seekToMs(ms, true, true);
    P.setLyricHover(-1);
    toast('跳到歌词：' + (P.getLyric().lines[i] || {}).text);
  });

  /* 栏目元数据（R45 修正语义：fav=曲库，plist=歌单）
     ⚠ 老 UI 是 fav=收藏/plist=歌单，但新架构 favorites 已废弃，
        用户指定 fav 这个 orbit 位接**曲库**，plist 接**在线歌单**。 */
`;
    const __epi = `  for (const __n of ["ORBIT_POS_LIST", "orbitActive", "orbitShown", "orbitAnim", "ORBIT_SPIN_DEG", "orbitSpinDir", "orbitSpinDeg", "orbitCurPos", "orbitAngOf", "orbitArcPoint", "orbitPlace", "orbitCurrentAng", "orbitLayoutRAF", "orbitRequestLayout", "orbitLayout", "orbitRotate", "orbitInZone", "cardsInZone", "AUTO_HIDE_MS", "autoHideTimer", "armAutoHide", "pokeShow", "pokeHide", "colTimer", "pokeColumn", "syncColumnPanel", "columnProbeZone", "columnFlashOnMode", "hideColumnIfIdle", "syncColumnByPlaying", "PANEL_SEL", "isolatedZone", "shelfCardZone", "lyricZone"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
