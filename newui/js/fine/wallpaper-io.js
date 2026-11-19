/* newui/js/fine/wallpaper-io.js — 「壁纸上传导入」自定义空间模块（源自 ui-skinset.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'wallpaper-io',
  source: 'ui-skinset.js',
  space: '壁纸上传导入',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  /* ═══ 本地图片 / 壁纸引擎上传（后端持久化导入）═══
     R66：原「我的壁纸」网格（loadMyWallpapers / renderWallpaperGrid）已移除 ——
     与上方「自定义壁纸」重复。上传仍走后端 SkinManager 落盘（重启仍在），
     收尾只做一件事：把返回的 /skin-assets URL 直接应用为背景。 */
  function uploadWallpaperFile(f){
    if (!f) return;
    const okType = /^image\\//.test(f.type || '')
      || /\\.(png|jpe?g|gif|webp|bmp)$/i.test(f.name || '');
    if (!okType){ toast('仅支持图片文件（持久化导入）'); return; }
    toast('正在导入壁纸…');
    fetch('/api/wallpapers/import?filename=' + encodeURIComponent(f.name), {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: f
    }).then(r => r.json()).then(d => {
      if (!d || !d.ok){ toast('导入失败：' + ((d && d.error) || '未知错误')); return; }
      /* 应用为背景（/skin-assets URL 喂 #bgimg → 自定义壁纸 wp-active） */
      if (d.url) applyWallpaperImage(d.url);
      toast('壁纸已导入并应用');
    }).catch(() => toast('导入失败（后端未连接？）'));
  }
  /* ═══ R65：壁纸引擎 zip 上传导入（场景/网页壁纸工程包）═══
     之前 wallpaper 类型只有「需桌面端解析」toast；现在 zip 直传后端解包。
     后端 _import_zip 找 project.json（scene→preview 图 / video / web）或首个
     媒体文件导入。前端收尾同图片：应用背景 + 刷网格。 */
  function uploadWallpaperZip(f){
    if (!f) return;
    toast('正在导入壁纸引擎工程…');
    fetch('/api/wallpapers/import-zip?filename=' + encodeURIComponent(f.name), {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: f
    }).then(r => r.json()).then(d => {
      if (!d || !d.ok){ toast('导入失败：' + ((d && d.error) || '未知错误')); return; }
      if (d.url) applyWallpaperImage(d.url);
      toast('壁纸引擎已导入');
    }).catch(() => toast('导入失败（后端未连接？）'));
  }
  function importWallpaperSrc(type, src){
    /* 图片/链接 → 背景图；歌曲封面 → useCoverSkin；视频 → #bgvideo；
       壁纸引擎 → 真导入（R65：此前只 toast 引导「需桌面端解析」，用户反馈
       「场景和网页都没办法导入」。后端 WallpaperImporter 本就能解析工程 /
       Steam 链接 / zip —— scene 退化为 preview 图、video/web 正常渲染，
       无需桌面端。现在接 REST 导入并把返回的 /skin-assets URL 应用为背景）。 */
    if (type === 'cover'){ useCoverSkin(); return; }   /* R66：不再 toast 复述 */
    if (type === 'video'){
      applyWallpaperVideo(src);
      return;
    }
    if (type === 'wallpaper'){
      /* Steam 创意工坊链接 / 数字 ID → 后端定位本地订阅并导入 */
      const isSteam = /steamcommunity\\.com\\/(sharedfiles|workshop)/i.test(src)
        || /^https?:\\/\\//i.test(src) || /^\\d{4,}$/.test(src.trim());
      if (isSteam){
        importWallpaperEngine(src);
        return;
      }
      /* 本地盘符 / file:// 路径 → 浏览器读不到 → 引导上传 zip */
      toast('网页无法直接读取本地工程文件夹，请用「选择文件」上传壁纸引擎导出的 .zip');
      const file = $('wp-file'); if (file) file.click();
      return;
    }
    const looksLocal = /^[a-zA-Z]:[\\\\/]|^file:\\/\\//.test(src);
    if (looksLocal){
      toast('浏览器无法直接读本地路径，请点「选择文件」上传');
      const file = $('wp-file'); if (file) file.click();
      return;
    }
    if (type === 'image' || type === 'link'){
      applyWallpaperImage(src);   /* R66：背景即时变化，不再 toast */
    } else {
      toast('暂不支持该类型');
    }
  }
  function importWallpaperEngine(src){
    /* ⚠ R65：真正走后端 WallpaperImporter（不再是「需桌面端」降级）。
       scene 3D 壁纸退化为 preview 图 / video / web 场景由渲染层解析。 */
    fetch('/api/wallpapers/import-engine?source=' + encodeURIComponent(src)
          + '&name=wallpaper', { method: 'POST' })
      .then(r => r.json()).then(d => {
        if (!d || !d.ok){ toast('导入失败：' + ((d && d.error) || '后端未连接')); return; }
        /* R66：原 activateWallpaper(key) 靠"重拉网格找 url"，网格已移除
           → 直接用导入响应里的 url 应用背景。 */
        if (d.url) applyWallpaperImage(d.url);
        else useCoverSkin();
        toast('壁纸引擎已导入');
      }).catch(() => toast('导入失败（后端未连接？）'));
  }
`;
    const __epi = `  for (const __n of ["uploadWallpaperFile", "uploadWallpaperZip", "importWallpaperSrc", "importWallpaperEngine"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
