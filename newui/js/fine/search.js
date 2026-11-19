/* newui/js/fine/search.js — 「搜索/建议/切源」自定义空间模块（源自 ui-init.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'search',
  source: 'ui-init.js',
  space: '搜索/建议/切源',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  /* [头部公共段并入] */
  function initSearchR54(){
    const box = $('search-box'), input = $('search-input'),
          clear = $('search-clear'), suggest = $('search-suggest');
    if (!box || !input || !suggest) return;
    let cur = -1, items = [], searchTimer = 0;

    /* ═══ R86 · 切换搜索源开关（搜索框内右侧）═══
       交互 = 点击循环切换可用音源（2 个时即等价于 on/off 开关），选择持久化到
       ui_prefs.searchSource；样式见 index.html 的 .ss-src（透明轨道 + 渐变发光滑块）。 */
    const srcWrap = $('ss-src'), srcLb = $('ss-src-lb'), srcCb = $('ss-src-cb');
    let srcList = [], srcIdx = 0, srcKey = '';

    function renderSrc(){
      if (!srcWrap) return;
      if (!srcList.length){ srcWrap.style.display = 'none'; return; }
      srcWrap.style.display = '';
      const curSrc = srcList[srcIdx] || srcList[0];
      srcKey = curSrc.key || '';
      /* 「可切换」= 除「自动」外还有 ≥2 个真实音源；否则压暗禁用（避免点了没反应） */
      const realCount = srcList.filter(s => !!s.key).length;
      const switchable = realCount >= 2;
      if (srcLb) srcLb.textContent = curSrc.short || curSrc.key || '自动';
      if (srcCb) srcCb.checked = srcIdx > 0;
      srcWrap.classList.toggle('solo', !switchable);
      srcWrap.classList.toggle('on', srcIdx > 0);
      srcWrap.title = '切换搜索源：' + srcList.map(s => s.label || s.key || '自动').join(' / ')
                    + (switchable ? '' : '（仅一个可用音源）');
    }
    function loadSearchSources(){
      fetch('/api/search/sources').then(r => r.json()).then(d => {
        const all = (d && Array.isArray(d.sources)) ? d.sources : [];
        /* ⚠ R86-fix：只保留「已启用」音源 —— 后端旧进程可能仍把模板插件
           （key=example，短名兜底显示成「EXAM」）也列进来；此处兜底过滤，
           改完刷新页面即生效，无需重启后端。 */
        srcList = all.filter(s => s && s.enabled !== false && s.searchable !== false);
        const want = (d && d.current) || '';
        const i = srcList.findIndex(s => s.key === want);
        srcIdx = i >= 0 ? i : 0;
        renderSrc();
      }).catch(() => { if (srcWrap) srcWrap.style.display = 'none'; });
    }
    if (srcCb) srcCb.addEventListener('change', () => {
      if (srcList.filter(s => !!s.key).length < 2){ renderSrc(); return; }
      srcIdx = (srcIdx + 1) % srcList.length;
      renderSrc();
      fetch('/api/ui/prefs', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ searchSource: srcKey }),
      }).catch(() => {});
      toast('搜索源：' + (srcList[srcIdx].label || srcKey || '自动'));
      /* 切源即重搜当前关键词（无关键词则等下次输入） */
      const kw = input.value.trim().toLowerCase();
      if (kw){ clearTimeout(searchTimer); backendSearch(kw); }
    });
    loadSearchSources();

    function escS(v){ return esc(v); }

    function render(){
      const kw = input.value.trim().toLowerCase();
      box.classList.toggle('has-text', input.value.length > 0);
      if (!kw){
        suggest.innerHTML = ''; box.classList.remove('show-suggest');
        items = []; cur = -1; return;
      }
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => backendSearch(kw), 300);
    }
    function backendSearch(kw){
      /* ⚠ R86：带上当前搜索源（srcKey 为空 = 后端自动竞速，谁先返回用谁）。
         切换音源后必须重搜，否则用户切了源却仍看到别家的结果。 */
      const u = '/api/search?q=' + encodeURIComponent(kw)
              + (srcKey ? '&source=' + encodeURIComponent(srcKey) : '');
      fetch(u).then(r => r.json()).then(d => {
        /* 过期结果丢弃 */
        if (input.value.trim().toLowerCase() !== kw) return;
        items = (d && Array.isArray(d.entries)) ? d.entries : [];
        if (!items.length){
          suggest.innerHTML = '<div class="ss-empty">未找到相关歌曲（可换关键词）</div>';
          box.classList.add('show-suggest'); cur = -1; return;
        }
        suggest.innerHTML = items.map((s, i) =>
          \`<div class="ss-item\${i===cur?' cur':''}" data-i="\${i}">
             <span class="ss-name">\${escS(s.title || '')}</span>
             <span class="ss-artist">\${escS((s.artist||'') + (s.source ? ' · ' + s.source : ''))}</span>
           </div>\`).join('');
        box.classList.add('show-suggest');
      }).catch(() => {
        suggest.innerHTML = '<div class="ss-empty">搜索失败（后端未连接？）</div>';
        box.classList.add('show-suggest');
      });
    }
    function playItem(s){
      if (!s) return;
      const src = s.source || '', ext = s.ext_id || '';
      const id = Number(s.id);
      /* ⚠ 在线曲无本地 db id → 走 /api/search/play（source+ext_id）；
         本地曲 → /api/play/track/{id}。绝不用队列下标。 */
      let url = null;
      if (src && ext){
        const q = new URLSearchParams();
        q.set('source', src); q.set('ext_id', ext);
        if (s.title) q.set('title', s.title);
        if (s.artist) q.set('artist', s.artist);
        if (s.album) q.set('album', s.album);
        if (s.cover) q.set('cover', s.cover);
        /* ⚠ R86：duration 必须透传 —— 缺它时 seed 曲目时长=0，
           后端 state().duration=0 → 进度条永远不动（且转场口播被跳过）。 */
        if (s.duration > 0) q.set('duration', String(s.duration));
        url = '/api/search/play?' + q.toString();
      } else if (Number.isFinite(id) && id > 0){
        url = '/api/play/track/' + id;
      }
      if (url) fetch(url, { method:'POST' }).then(r => r.json()).catch(() => {});
      toast('♪ ' + (s.title || '') + (s.artist ? ' — ' + s.artist : ''));
      box.classList.remove('show-suggest','has-text');
      input.value = ''; items = []; cur = -1; input.blur();
    }
    function pick(i){
      if (i >= 0 && i < items.length) playItem(items[i]);
    }
    input.addEventListener('input', () => { cur = -1; render(); });
    input.addEventListener('keydown', ev => {
      ev.stopPropagation();          // 阻止冒泡到全局卡片方向键
      if (ev.key === 'ArrowDown'){ ev.preventDefault(); cur = Math.min(cur + 1, items.length - 1); if (box.classList.contains('show-suggest')) highlight(); }
      else if (ev.key === 'ArrowUp'){ ev.preventDefault(); cur = Math.max(cur - 1, -1); if (box.classList.contains('show-suggest')) highlight(); }
      else if (ev.key === 'Enter'){ ev.preventDefault(); if (cur >= 0) pick(cur); }
      else if (ev.key === 'Escape'){ suggest.innerHTML=''; box.classList.remove('show-suggest'); input.blur(); }
    });
    function highlight(){
      suggest.querySelectorAll('.ss-item').forEach((el, i) =>
        el.classList.toggle('cur', i === cur));
      const el = suggest.querySelector('.ss-item.cur');
      if (el && el.scrollIntoView) el.scrollIntoView({ block:'nearest' });
    }
    if (clear) clear.addEventListener('click', () => {
      input.value = ''; render(); input.focus();
    });
    /* 建议点选（mousedown 避免 input blur 抢先） */
    suggest.addEventListener('mousedown', ev => {
      const item = ev.target.closest('.ss-item'); if (!item) return;
      ev.preventDefault();
      const idx = +item.getAttribute('data-i');
      if (Number.isFinite(idx)) pick(idx);
    });
    /* ═══ R56：鼠标靠近显现 / 远离消失 —— 触发范围比搜索栏本体大一圈 ═══
       老逻辑只在顶部 130px 竖向热区 + 中心窄带内才显示；
       R56 改为**包围盒外扩**：以搜索栏为中心、横向/纵向都放大约 1.6 倍矩形
       （含其正上方到顶部的空档），指针一靠近就显现，离开才消失。
       移入搜索栏本体 / 输入聚焦时**绝不消失**（保证可搜索）。 */
    /* ═══ R62：输入中恒显 —— 鼠标远离/移出窗口也不能藏 ═══
       用户原话：「搜索栏在打字的时候不能隐藏，要一直显现。哪怕鼠标不在触发
       范围，在打字搜索的时候也要显现。」
       keepVisible = 聚焦 或 有非空输入内容。满足任一条件时 mousemove /
       mouseleave / blur 一律不得摘 .visible（blur 只在失焦且内容为空时允许藏）。 */
    function searchKeepVisible(){
      return document.activeElement === input
          || !!(input.value && input.value.trim().length > 0)
          || box.classList.contains('show-suggest');
    }
    document.addEventListener('mousemove', ev => {
      if (box.classList.contains('ui-hidden')) return;
      const r = box.getBoundingClientRect();
      /* 触发区 = 包围盒外扩（比栏大一圈）：宽 ×1.5、高含顶部空档 60px */
      const padX = Math.max(70, r.width * 0.3);
      const padY = Math.max(46, 40 + r.height * 0.4);
      const nearX = ev.clientX >= r.left - padX && ev.clientX <= r.right + padX;
      const nearY = ev.clientY >= Math.max(0, r.top - padY) && ev.clientY <= r.bottom + 26;
      const near = (nearX && nearY) || box.contains(ev.target);
      const vis = box.classList.contains('visible');
      if (near && !vis) box.classList.add('visible');
      else if (!near && vis && !box.contains(ev.target)
               && !searchKeepVisible()) box.classList.remove('visible');
    });
    /* ⚠ R62：鼠标整体移出窗口时，若仍在输入（聚焦/有内容/建议开着）不得隐藏 */
    document.addEventListener('mouseleave', () => {
      if (!searchKeepVisible()) box.classList.remove('visible');
    });
    input.addEventListener('focus', () => box.classList.add('visible'));
    input.addEventListener('input', () => box.classList.add('visible'));
    input.addEventListener('blur', () => {
      requestAnimationFrame(() => {
        /* 焦点在输入框/建议列表内 → 保持可见 */
        if (box.contains(document.activeElement) || box.contains(document.querySelector(':focus'))) return;
        /* ⚠ R62：内容非空（用户打完字点外面看建议/刚要复制）也保持可见；
           只有「失焦 + 内容已清空」才收起。真正清空动作由 clear 按钮/
           playItem 完成（它们摘 .visible 前会先清 input.value）。 */
        if (searchKeepVisible()) return;
        box.classList.remove('visible');
      });
    });
    /* 点外部收起建议 */
    document.addEventListener('click', ev => {
      if (!box.contains(ev.target)) box.classList.remove('show-suggest');
    });
  }

  /* ★★ R110（2026-09-13）一次性偏好迁移：语义强度档默认 **0.6 → 1.0**（配 ① 加权聚合）。
     为什么必须做：本文件的恢复逻辑是「**存档里有的就覆盖**」
     ⇒ 用户上一次存的 \`semStrength:0.6\` 会**静默盖掉新默认**。
     实测踩到：只改 \`ui-water.js\` 的 \`let semStrength = 1.0\` 时，真机端到端
     （\`verify_r95b\` C5）**纹丝不动（仍是 2.06×）** —— 因为 \`ui-skinset.js\` 在 init 时
     用 \`setSemStrength(0.6)\` 又推了回去；\`resetOpts()\` 里还有第三个 0.6。
     ⇒ 用 \`semOptV\` 标记**只强制刷一次**；之后用户手动调的值照常被尊重。
     ⚠ **两条恢复路径（localStorage / 后端 prefs）都要调** —— 只改一条会漏另一半。 */
  const SEM_OPT_V = 1;
`;
    const __epi = `  for (const __n of ["initSearchR54", "SEM_OPT_V"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
