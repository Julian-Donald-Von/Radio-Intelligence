/* newui/js/fine/video-wallpaper.js — 「视频壁纸」自定义空间模块（源自 ui-skinset.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'video-wallpaper',
  source: 'ui-skinset.js',
  space: '视频壁纸',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  /* ═══ R62 #10：视频壁纸（#bgvideo）═══
     与 #bgimg 同为皮肤内容位、互斥显示：
       · 设 src → #bgvideo.has-bg 显示、#bgimg 隐藏（has-bg 摘除）；
       · body.wp-active = 自定义壁纸优先（切歌不被封面覆盖）。
     src 支持：远程直链 URL / FileReader dataURL（本地文件）。
     ⚠ 视频自动播放策略：muted+loop+playsinline+autoplay（HTML 属性已设），
     play() 失败（浏览器策略）时静默 —— 壁纸是装饰，不能因播放策略报错。 */
  function applyWallpaperVideo(src){
    const v = $('bgvideo'); if (!v) return;
    if (!src){ return; }
    v.src = src;
    v.classList.add('has-bg', 'wp-active');
    document.body.classList.add('wp-active');
    uiWallpaper = 'custom';              /* R78：自定义视频壁纸（非 cover/default） */
    /* 隐藏图片壁纸（若有）—— 皮肤内容位同一时刻只有一个可见 */
    const bgimg = $('bgimg');
    if (bgimg){
      bgimg.classList.remove('wp-active', 'has-bg');
      bgimg.removeAttribute('src');
    }
    try { v.play().catch && v.play().catch(() => {}); } catch (_) {}
    /* 氛围参考同步（Poster/LiquidBg 吃不到视频帧，仅清掉旧图引用） */
    try { if (window.Poster) window.Poster.setCover(''); } catch (_) {}
    try { if (window.LiquidBg) window.LiquidBg.setSource(''); } catch (_) {}
  }
  function hideVideoWallpaper(){
    const v = $('bgvideo');
    if (v){
      v.pause && v.pause();
      v.removeAttribute('src');
      v.classList.remove('has-bg', 'wp-active');
    }
  }
  function applyWallpaperImage(src){
    /* R62 #10：图壁纸激活 → 停掉视频壁纸（皮肤内容位互斥，同一时刻仅一个可见） */
    try { hideVideoWallpaper(); } catch (_) {}
    /* ═══ R57-2：导入壁纸"百分百可见"重构 ═══
       旧实现只设 #bgimg.src，而 #bgimg 默认 opacity:0、仅 acrylic 模式
       （body.bgmode-acrylic #bgimg）才可见 → poster 模式导入"没反应"。
       新模型：#bgimg 是唯一背景壁纸位（见 R57-1 CSS），有 src 即显示；
       这里设 src + body.wp-active（自定义壁纸优先：切歌不被封面覆盖、
       锐利显示不做亚克力模糊）。歌词海报画布透明叠上，壁纸即时可见。
       ⚠ R59：wp-active **不再**强制 filter:none（CSS 覆盖已删）——
       明亮亚克力 blur 对自定义壁纸同样生效（用户原话：亚克力是背景效果，
       自定义壁纸/封面只是背景内容，两者正交）。 */
    const bgimg = $('bgimg');
    const bd = document.body;
    if (bgimg){
      bgimg.src = src;
      bgimg.classList.add('has-bg', 'wp-active');
      bd.classList.add('wp-active');
      uiWallpaper = 'custom';            /* R78：自定义图片壁纸（非 cover/default） */
    }
    /* 画布氛围色参考（透明海报下局部光晕跟随壁纸） */
    try { if (window.Poster) window.Poster.setCover(src); } catch (_) {}
    try { if (window.LiquidBg) window.LiquidBg.setSource(src); } catch (_) {}
  }
  /* 切歌时若存在自定义壁纸则不被封面覆盖 —— render() 只在 body 无 wp-active 时喂封面 */
`;
    const __epi = `  for (const __n of ["applyWallpaperVideo", "hideVideoWallpaper", "applyWallpaperImage"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
