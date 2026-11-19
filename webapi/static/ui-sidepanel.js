/* ══════════════════════════════════════════════════════════
   ui-sidepanel.js — Radio Intelligence 前端模块 · 侧边面板 side-panel 队列/历史/曲库 行操作
   自 webapi/static/index.html 内嵌 <script> L2613-L2861 拆出（R58-7 #46）
   与原 index.html 内所有 ui-*.js 共享顶层作用域 → 加载顺序即执行顺序，勿打乱
   ══════════════════════════════════════════════════════════ */
  let sideOpen = false;
  let sideTab = 'queue';
  const HEART_SVG  = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z"/></svg>';
  const BROKEN_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z"/><path d="M4 4l16 16"/></svg>';
  const PLAY_SVG   = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>';
  const NEXT_SVG   = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h13M12 6l6 6-6 6"/></svg>';
  const RECO_SVG   = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>';
  const DEL_SVG    = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 6l12 12M18 6l-12 12"/></svg>';

  /* actions 集合（原 UI rowSide L3130-1333 的 actions 三态） */
  function rowSideActions(){
    if (sideTab === 'history') return ['rm'];
    if (sideTab === 'queue')   return ['play', 'fav', 'dislike', 'rm'];
    if (sideTab === 'library') return ['play', 'next', 'reco', 'del'];
    /* 歌单详情：在线曲目无本地 db id，只能整单起播（/api/playlists/play） */
    if (sideTab === 'plist')   return ['play'];
    return [];
  }
  /* i = 行下标（歌单详情模式用作 play_playlist 的 index） */
  function drowHtml(s, i){
    const idx = (i == null) ? '' : (' data-pl="' + i + '"');
    const acts = rowSideActions();
    const favOn = !!s._fav, disOn = !!s._dis;
    const playing = !!s.current;
    const inl = '<img src="' + esc(s.cover || '') + '" alt="" onerror="this.style.display=\'none\'">'
      + '<div class="dm"><div class="dn">' + esc(s.title || '未知曲目') + '</div>'
      + '<div class="da">' + esc(s.artist || '') + '</div></div>';
    let btns = '';
    if (acts.length){
      const b = (act, title, inner, on) =>
        '<button class="dact" data-act="' + act + '" title="' + title + '"'
        + (on ? ' data-on="1"' : '') + '>' + inner + '</button>';
      btns = '<div class="dacts">';
      if (acts.includes('play'))    btns += b('play',    '播放', PLAY_SVG, false);
      if (acts.includes('next'))    btns += b('next',    '下一首播放', NEXT_SVG, false);
      if (acts.includes('reco'))    btns += b('reco',    '加入推荐', RECO_SVG, false);
      if (acts.includes('fav'))     btns += b('fav',     '收藏', HEART_SVG, favOn);
      if (acts.includes('dislike')) btns += b('dislike', '不喜欢', BROKEN_SVG, disOn);
      if (acts.includes('del'))     btns += b('del',     '从曲库删除', DEL_SVG, false);
      if (acts.includes('rm'))      btns += b('rm',      '不播放（仅移除）', DEL_SVG, false);
      btns += '</div>';
    }
    return '<div class="drow' + (playing ? ' playing' : '') + '" data-id="' + esc(s.id) + '"'
      + idx + '>'
      + inl + btns + '</div>';
  }
  function _disKey(s){
    if (s.source && String(s.source) !== 'local'){
      return 'e:' + s.source + ':' + (s.ext_id != null ? s.ext_id : '');
    }
    return s.id != null ? ('i:' + s.id) : '';
  }
  /* 行内按钮点击事件代理 */
  $('sp-body').addEventListener('click', e => {
    const btn = e.target.closest('.dact');
    const row = e.target.closest('.drow');
    if (!row) return;
    const id = row.dataset.id;
    /* 歌单详情行：在线曲目没有本地 db id，/api/play/track/{id} 要求 int 会直接 422。
       → 整单入队 + 后端按 index 跳（内部修正 jump_to(index-1)）。 */
    const plIdx = (sideTab === 'plist' && row.dataset.pl) ? parseInt(row.dataset.pl, 10) : -1;
    const playRow = () => {
      if (plIdx >= 0 && sidePlist){
        toast('正在播放歌单…');
        fetch('/api/playlists/play?platform=' + encodeURIComponent(sidePlist.platform)
              + '&ext_id=' + encodeURIComponent(sidePlist.ext_id)
              + '&index=' + encodeURIComponent(plIdx), { method: 'POST' })
          .then(r => r.json()).then(d => {
            if (d && !d.ok) toast('播放失败：' + ((d && d.error) || ''));
          }).catch(() => { toast('播放失败'); });
        return;
      }
      fetch('/api/play/track/' + encodeURIComponent(id), { method: 'POST' })
        .then(r => r.json()).then(d => {
          if (d && d.ok) { /* 播放成功，无需提示 */ }
          else toast('播放失败');
        }).catch(() => {});
    };
    if (btn){
      e.stopPropagation();
      const act = btn.dataset.act;
      if (act === 'play'){
        playRow();
      } else if (act === 'next'){
        /* ⚠ R44 曾接到 /api/playback/next —— 那是**切下一首**，语义相反。
           插入待播首位走 /api/queue/play-next。 */
        fetch('/api/queue/play-next?track_id=' + encodeURIComponent(id), { method: 'POST' })
          .then(r => r.json()).then(() => loadSidePanel()).catch(() => {});
      } else if (act === 'reco'){
        fetch('/api/reco/play?track_id=' + encodeURIComponent(id), { method: 'POST' })
          .then(r => r.json()).then(() => loadSidePanel()).catch(() => {});
      } else if (act === 'del'){
        fetch('/api/library/delete?id=' + encodeURIComponent(id), { method: 'POST' })
          .then(r => r.json()).then(() => loadSidePanel()).catch(() => {});
      } else if (act === 'rm'){
        if (sideTab === 'history'){
          fetch('/api/history/' + encodeURIComponent(id), { method: 'DELETE' })
            .then(r => r.json()).then(() => loadSidePanel()).catch(() => {});
        } else {
          fetch('/api/queue/remove?track_id=' + encodeURIComponent(id), { method: 'POST' })
            .then(r => r.json()).then(() => loadSidePanel()).catch(() => {});
        }
      } else if (act === 'dislike'){
        /* 「不喜欢」= AI 避雷 + 从待播/推荐货架剔除。
           ⚠ 只有 dislike 有后端接口（/api/dislike/add|remove）。
           ⚠ R62 #23：entry 带完整条目 → 在线歌落库带 title/artist。 */
        const entry = sideItems.find(it => String(it.id) === String(id));
        if (!entry) return;
        const key = _disKey(entry);
        const on = (btn.dataset.on === '1');
        const tq = 'key=' + encodeURIComponent(key)
                 + '&title=' + encodeURIComponent(entry.title || '')
                 + '&artist=' + encodeURIComponent(entry.artist || '');
        fetch('/api/dislike/' + (on ? 'remove' : 'add') + '?' + tq,
              { method: 'POST' })
          .then(r => r.json()).then(d => {
            if (d && d.ok){
              btn.dataset.on = on ? '' : '1';
              btn.classList.toggle('on', !on);
              toast(on ? '已取消不喜欢' : '已加入不喜欢');
            }
          }).catch(() => {});
      } else if (act === 'fav'){
        /* ⚠ favorites 整套已废弃（后端无 /api/favorite 接口）。
           原 UI 的 ♥ 语义 = 收藏到曲库；曲库(library) 是**独立**入口，
           列表项收藏无后端支持 → 不请求，只给提示（避免 404 噪音）。
           保留按钮仅为视觉对齐原 UI。 */
        toast('收藏已并入曲库页管理');
      }
    } else {
      /* 行点击 = 播放 */
      playRow();
    }
  });

  let sideItems = [];
  /* 歌单详情模式：sideTab === 'plist' 时按 (platform, ext_id) 拉曲目 */
  let sidePlist = null;      // { platform, ext_id, name }
  function loadSidePanel(){
    const body = $('sp-body');
    if (sideTab === 'plist'){
      if (!sidePlist){ body.innerHTML = '<div class="sp-empty">未选择歌单</div>'; return; }
      body.innerHTML = '<div class="sp-empty">加载歌单曲目…</div>';
      fetch('/api/playlists/tracks?platform=' + encodeURIComponent(sidePlist.platform)
            + '&ext_id=' + encodeURIComponent(sidePlist.ext_id))
        .then(r => r.json()).then(d => {
          const entries = (d && d.ok) ? (d.entries || []) : [];
          sideItems = entries;
          if (!entries.length){
            body.innerHTML = '<div class="sp-empty">该歌单暂无曲目</div>';
            return;
          }
          body.innerHTML = entries.map((s, i) => drowHtml(s, i)).join('');
        }).catch(() => {
          body.innerHTML = '<div class="sp-empty">歌单曲目加载失败</div>';
        });
      return;
    }
    const url = sideTab === 'history' ? '/api/history'
              : sideTab === 'library' ? '/api/library'
              : '/api/queue';
    fetch(url).then(r => r.json()).then(d => {
      if (!d || !d.ok){ body.innerHTML = '<div class="sp-empty">加载失败</div>'; return; }
      const entries = d.entries || d.tracks || d.history || [];
      sideItems = entries;
      if (!entries.length){
        body.innerHTML = '<div class="sp-empty">' + (
          sideTab === 'history' ? '暂无播放历史'
          : sideTab === 'library' ? '曲库为空'
          : '队列为空' ) + '</div>';
        return;
      }
      body.innerHTML = entries.map(drowHtml).join('');
    }).catch(() => {
      $('sp-body').innerHTML = '<div class="sp-empty">加载失败</div>';
    });
  }
  /* 点歌单卡 → 侧边面板展示该歌单曲目。
     ⚠ R65：歌单详情是**居中大面板**（.plist-mode：屏幕正中 + 1.5× 尺寸），
       与队列/历史/曲库那种"贴着触发图标上方"的小面板是两套定位：
       · 进 plist → 清掉上一次可能残留的 inline left/bottom/top/right
         （positionSidePanel 写过内联样式，会盖掉 CSS 的居中），再挂 .plist-mode；
       · 切回其它 tab / 关面板 → 摘掉 .plist-mode，交还 positionSidePanel 定位。 */
  function openPlaylist(platform, ext_id, name){
    if (!platform || !ext_id) return;
    sidePlist = { platform: platform, ext_id: String(ext_id), name: name || '歌单' };
    sideTab = 'plist';
    sideOpen = true;
    const p = $('side-panel');
    if (p){
      p.style.left = ''; p.style.right = ''; p.style.top = ''; p.style.bottom = '';
      p.classList.add('plist-mode');
    }
    $('sp-title').textContent = sidePlist.name;
    /* R72：歌单详情头部「播放全部」仅 plist-mode 显示（其它 tab 无此语义） */
    const spa = $('sp-playall');
    if (spa) spa.style.display = '';
    if (p) p.classList.add('open');
    loadSidePanel();
  }
  /* 模式名（面板标题）—— 与老 UI openSidePanel 里的三元式同义 */
  const SIDE_TITLE = { queue:'播放列表', history:'播放历史', library:'曲库', plist:'歌单' };
  /* ⚠ 一个入口 = 一个模式，面板里**没有 tab**（R46-4）。
     播放列表就只装播放列表，绝不混入历史/曲库（老 UI 原样：#d-queue / #d-history 分开）。
     R49：面板**在触发图标正上方弹出**（positionSidePanel 按图标实时定位），
     且每个入口有自己的触发空间（图标 + 面板本体），互不重叠。 */
  function positionSidePanel(){
    const p = $('side-panel');
    const btn = $(sideTab === 'history' ? 'd-history'
               : sideTab === 'queue'   ? 'd-queue' : null);
    if (!p || !btn) return;
    const r = btn.getBoundingClientRect();
    /* 面板水平中心 = 图标中心；底边 = 图标顶边上方 14px（"图标正上方"） */
    p.style.left = Math.max(8, Math.min(innerWidth - 348,
                     r.left + r.width / 2 - 170)) + 'px';
    p.style.right = 'auto';
    p.style.bottom = (innerHeight - r.top + 14) + 'px';
  }
  function openSidePanel(tab){
    if (tab) sideTab = tab;
    if (sideTab !== 'plist') sidePlist = null;
    sideOpen = true;
    /* R65：非歌单详情 → 摘掉居中大面板模式（回到"图标上方"定位） */
    const sp = $('side-panel');
    if (sp) sp.classList.remove('plist-mode');
    positionSidePanel();
    $('sp-title').textContent = SIDE_TITLE[sideTab] || '播放列表';
    /* R72：非歌单模式隐藏「播放全部」（只有歌单详情才有整单播放按钮） */
    const spa = $('sp-playall');
    if (spa) spa.style.display = 'none';
    if (sp) sp.classList.add('open');
    loadSidePanel();
  }
  function closeSidePanel(){
    sideOpen = false;
    const sp = $('side-panel');
    if (sp) sp.classList.remove('open');
    /* ⚠ 延迟摘 .plist-mode：立刻摘会让面板在淡出动画途中瞬移回图标上方。
       等 .35s 过渡走完再摘（与基座 transition 同一时长）。 */
    if (sp && sp.classList.contains('plist-mode')){
      setTimeout(() => {
        if (!sideOpen) sp.classList.remove('plist-mode');
      }, 380);
    }
  }
  $('sp-close').addEventListener('click', closeSidePanel);
  /* ═══ R72：歌单详情「播放全部」—— 整单入队从第一首开播（不走逐行播放）。
     与卡片行 play（带 index 跳转）同后端 /api/playlists/play；index=0 = 第一首起播。
     ⚠ stopPropagation：防止冒泡到 window click（歌词 seek）或点外部关闭。 */
  const spPlayAll = $('sp-playall');
  if (spPlayAll){
    spPlayAll.addEventListener('click', e => {
      e.stopPropagation();
      if (!sidePlist) return;
      toast('正在播放歌单…');
      fetch('/api/playlists/play?platform=' + encodeURIComponent(sidePlist.platform)
            + '&ext_id=' + encodeURIComponent(sidePlist.ext_id) + '&index=0',
            { method: 'POST' })
        .then(r => r.json()).then(d => {
          if (d && d.ok){ setTimeout(poll, 260); }
          else toast('播放失败：' + ((d && d.error) || ''));
        }).catch(() => { toast('播放失败'); });
    });
  }
  /* ⚠⚠ R69 修复：点 sp-close 关面板的**同一记 click** 会继续冒泡到
     ui-orbit.js 的 window click（歌词 seek）。那个 handler 用 elementFromPoint
     复查命中 —— 但 closeSidePanel 先把 .open 摘了（面板 pointer-events 立即
     失效），elementFromPoint 已穿透到背景歌词带 → 关个面板把歌 seek 了（用户点名）。
     设置卡没这问题：抽屉按钮/关闭钮与歌词带不重叠，且 anyDrawerOpen 全程兜底。
     这里照设置卡的处理思路：在关闭钮上**截断**整条点击链路 —— stopPropagation
     掐掉 window 冒泡段；stopImmediatePropagation 连 sp-close 同元素其它监听也
     不放过（防未来加逻辑被穿透）；同时再补一个 document 捕获层闸门 —— 面板
     **刚关（open 摘下 / plist-mode 未摘）的那一瞬**，后续元素/全局监听照样
     可能用 elementFromPoint 误判，凡"正在淡出面板内按下的 click"一律归零。 */
  const spClose = $('sp-close');
  if (spClose && !spClose._r69Bound){
    spClose._r69Bound = true;
    spClose.addEventListener('click', e => {
      e.stopPropagation();
      e.stopImmediatePropagation();
    });
  }
  window.addEventListener('resize', () => { if (sideOpen) positionSidePanel(); });
  /* R53 触发空间改造（用户原话）：
     "dock 栏中，播放列表和播放历史要做成**单击出现**的而不是鼠标靠近出现。
      然后你要给这个窗口做**单独的识别区**，不然点击它们关闭的叉，
      会触发到歌词的识别区导致歌词被点击。"
     → ① 移除 mouseenter 即开 + 450ms 自动收起（hover 弹窗彻底下线）；
       ② 单击按钮开合（toggle）；点面板**外部任意处**关闭；
       ③ 面板区域 = 独立识别区：歌词点击/滚轮识别（lyricZone）必须把它排除，
          否则 sp-close / 面板内部滚动会误触发歌词 seek 或翻行。 */
  function bindSideBtn(id, tab){
    const btn = $(id);
    if (!btn) return;
    btn.addEventListener('click', ev => {
      ev.stopPropagation();
      if (sideOpen && sideTab === tab){ closeSidePanel(); return; }
      openSidePanel(tab);
    });
  }
  bindSideBtn('d-queue',   'queue');
  bindSideBtn('d-history', 'history');
  /* 点面板外部任意处关闭（单击语义；捕获阶段判断，避免面板内点击误关） */
  document.addEventListener('click', ev => {
    if (!sideOpen) return;
    const t = ev.target;
    if (!t || !t.closest) return;
    if (t.closest('#side-panel') || t.closest('#dock')) return;
    closeSidePanel();
  }, true);
  /* ⚠ 初始化栏目头部**不要在这里调用**！
     syncColumnHead() 内部读 cardItems（本文件后面用 let 声明），
     在此处同步调用会撞上 TDZ（Cannot access 'cardItems' before initialization）。
     统一挪到文件末尾初始化区（与 loadView() 一起）。 */

  /* ---------- Phase 3：推荐货架（3D 弧排布 + 弹簧物理，沿用原 #shelf 动画） ---------- */
