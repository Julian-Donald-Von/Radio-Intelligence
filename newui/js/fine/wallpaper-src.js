/* newui/js/fine/wallpaper-src.js — 「封面/logo/壁纸偏好恢复」自定义空间模块（源自 ui-skinset.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'wallpaper-src',
  source: 'ui-skinset.js',
  space: '封面/logo/壁纸偏好恢复',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  /* ═══ R61：切回「歌曲封面」═══
     自定义壁纸一旦导入（wp-active），render() 就不再喂封面 —— 用户切不回封面背景。
     摘除 wp-active / 清 #bgimg 的壁纸 src 后，让 render() 重新以 st.cover 喂 bgimg。
     这里提供 useCoverSkin()：body+img 摘 wp-active，且 bgimg src 先置空、摘 has-bg
     （避免旧壁纸残留一帧），再按当前效果模式决定是否立即补封面。 */
  function useCoverSkin(){
    /* R62 #10：切回封面 → 停掉视频壁纸 */
    try { hideVideoWallpaper(); } catch (_) {}
    const bd = document.body, bgimg = $('bgimg');
    bd.classList.remove('wp-active');
    uiWallpaper = 'cover';               /* R78：背景内容偏好回「跟随封面」 */
    if (bgimg){
      bgimg.classList.remove('wp-active');
      bgimg.removeAttribute('src');        /* 逼 render() 用新封面重新喂 */
      bgimg.classList.remove('has-bg');
    }
    /* 当前曲封面（st.cover）即时补上 —— 走统一路径（同时喂 Poster 氛围参考）。
       无封面 → ensureDefaultSkin 程序化底（render 内部逻辑一致）。 */
    if (st && st.cover){
      try {
        if (bgimg){
          bgimg.setAttribute('src', st.cover);
          bgimg.classList.add('has-bg');
        }
        try { if (window.Poster) window.Poster.setCover(st.cover); } catch (_) {}
        try { if (window.LiquidBg) window.LiquidBg.setSource(st.cover); } catch (_) {}
      } catch (_) {}
    } else {
      try { ensureDefaultSkin(); } catch (_) {}
    }
  }
  /* ═══ R67-5：Radio Intelligence 玻璃 logo 作为内置默认背景 ═══
     与 useCoverSkin 一样：摘 wp-active、停视频壁纸、设 bgimg src。
     区别：src 是 /brand/logo.png（项目自带，不入用户壁纸库，不走后端）。
     有歌曲时也会被切到默认 logo（用户原话：默认选项就是这个图片作为壁纸），
     所以这是「皮肤」而非「封面」—— 永远展示 logo，无视 st.cover。 */
  function applyBrandSkin(){
    try { hideVideoWallpaper(); } catch (_) {}
    const bd = document.body, bgimg = $('bgimg');
    bd.classList.add('wp-active');
    uiWallpaper = 'default';             /* R78：持久化镜像 + 切歌不被覆盖判据 */
    if (bgimg){
      bgimg.classList.add('wp-active');
      bgimg.classList.add('has-bg');
      bgimg.setAttribute('src', '/brand/logo.png');
    }
    try { if (window.Poster) window.Poster.setCover(''); } catch (_) {}
    try { if (window.LiquidBg) window.LiquidBg.setSource(''); } catch (_) {}
    persistPrefs({ wallpaper: 'default' });
  }
  /* ═══ R78：按持久化偏好恢复背景内容（启动 / 偏好回填时调用）═══
     uiWallpaper 三态：
       · default  → applyBrandSkin()：固定 logo 壁纸，wp-active，切歌不被封面盖
       · custom   → 用户导入的自定义壁纸：保持当前 bgimg src（刷新后背景内容由
                    /api/ui/prefs 之外的自定义壁纸机制另行恢复，这里不覆盖）；
                    若 bgimg 无 src（视频壁纸等未持久化场景）→ 兜底 ensureDefaultSkin
       · cover    → 跟随封面：交给 render() / ensureDefaultSkin 处理，无需干预
     仅 default 是**纯偏好驱动**（logo 项目内置、无需后端），因此启动恢复只对它
     负责；custom 壁纸是图片文件，其"刷新后仍在"由后端 wallpaper 导入保证。 */
  function applyWallpaperPref(){
    try {
      if (uiWallpaper === 'default'){ applyBrandSkin(); return; }
      if (uiWallpaper === 'custom'){
        const bgi = $('bgimg');
        if (bgi && !bgi.getAttribute('src')) ensureDefaultSkin();
        return;
      }
      /* cover：当前曲封面（若有）由 render() 喂；无封面 ensureDefaultSkin 垫底 */
    } catch (_) {}
  }
  function bindWallpaper(){
    const typeRow = $('wp-type-row');
    if (typeRow) typeRow.querySelectorAll('.wp-type').forEach(b => {
      b.addEventListener('click', () => {
        typeRow.querySelectorAll('.wp-type').forEach(x => x.classList.remove('active'));
        b.classList.add('active'); wpType = b.dataset.wp;
        const inp = $('wp-input');
        if (inp) inp.placeholder = {
          cover:'当前播放歌曲封面（无需导入）',
          image:'粘贴图片 URL，或点「选择文件」',
          link:'粘贴壁纸直链 URL',
          video:'粘贴视频直链 URL，或点「选择文件」',
          wallpaper:'粘贴 Steam 创意工坊链接 / 壁纸 ID，或点「选择文件」上传 .zip 工程包',
          /* R67-5：默认 = Radio Intelligence 玻璃 logo（项目自带，不入用户壁纸库） */
          default:'Radio Intelligence 玻璃 logo（项目内置默认）'
        }[wpType] || inp.placeholder;
        if (wpType === 'cover') useCoverSkin();   /* R66：切封面即时可见，不再 toast */
        if (wpType === 'default') applyBrandSkin();   /* R67-5：内置默认背景 */
      });
    });
    const imp = $('wp-import'), inp = $('wp-input'), pick = $('wp-pick'), file = $('wp-file');
    /* ═══ R57-2：本地图片百分百可用 ═══
       http 页面不能读 file:// 路径 → 旧「粘贴本地路径」在浏览器里必失败。
       新增「选择文件」：FileReader 把图片读成 dataURL → 直接喂背景层，
       无需后端 WallpaperImporter。拖放同样优先走 FileReader。 */
    function readFileAsWallpaper(f){
      if (!f) return;
      /* R65：wallpaper 类型 + .zip → 上传后端解包导入（壁纸引擎工程包 /
         Steam 导出 zip）。zip 不是图片/视频，须在图片校验前拦截。 */
      if (wpType === 'wallpaper' && /\\.zip$/i.test(f.name || '')){
        uploadWallpaperZip(f);
        return;
      }
      /* R62 #10：video 类型接受视频文件（其它一律按图片校验） */
      const isVideo = wpType === 'video'
        && (/^video\\//.test(f.type || '')
            || /\\.(mp4|webm|ogg|ogv|mov)$/i.test(f.name || ''));
      if (!isVideo && f.type && f.type.indexOf('image/') !== 0){
        toast('仅支持图片 / 视频文件'); return;
      }
      /* ═══ R63：图片文件导入 = 真实后端持久化 ═══
         本地图片（image/link/wallpaper 类型选的文件）POST 原字节到
         /api/wallpapers/import → SkinManager 落 plugins/skins + assets，
         重启仍在。仅 video 保留 FileReader dataURL 即时预览（视频壁纸
         网页端不持久化，属诚实降级）。
         R66：导入成功后不再刷「我的壁纸」网格（该网格已随冗余功能移除），
         直接用响应里的 /skin-assets URL 应用为背景。 */
      if (!isVideo){
        uploadWallpaperFile(f);
        return;
      }
      const rd = new FileReader();
      rd.onload = () => {
        applyWallpaperVideo(String(rd.result)); toast('视频壁纸已应用');
      };
      rd.onerror = () => toast('读取文件失败');
      rd.readAsDataURL(f);
    }
    if (pick && file){
      pick.addEventListener('click', () => { file.value = ''; file.click(); });
      file.addEventListener('change', () => {
        const f = file.files && file.files[0];
        if (f){ readFileAsWallpaper(f); }
      });
    }
    if (imp && inp) imp.addEventListener('click', () => {
      const src = inp.value.trim();
      if (!src){ toast('请先粘贴 URL / 路径，或点「选择文件」'); return; }
      try { importWallpaperSrc(wpType, src); } catch (_) {}
    });
    /* 拖放：R57-2 优先 FileReader 读真实文件（次选填路径文本） */
    const dz = $('wp-input') || document.body;
    dz.addEventListener('dragover', e => e.preventDefault());
    dz.addEventListener('drop', e => {
      e.preventDefault();
      const f = e.dataTransfer.files && e.dataTransfer.files[0];
      if (f){
        /* R62 #10：video 类型也放行 —— readFileAsWallpaper 内部按 wpType 分流 */
        readFileAsWallpaper(f);
      }
    });
  }

`;
    const __epi = `  for (const __n of ["useCoverSkin", "applyBrandSkin", "applyWallpaperPref", "bindWallpaper"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
