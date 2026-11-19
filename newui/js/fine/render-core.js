/* newui/js/fine/render-core.js — 「render/默认皮肤」自定义空间模块（源自 ui-core.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'render-core',
  source: 'ui-core.js',
  space: 'render/默认皮肤',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  /* ---------- 渲染 ---------- */
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

`;
    const __epi = `  for (const __n of ["_defSkin", "ensureDefaultSkin", "render"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
