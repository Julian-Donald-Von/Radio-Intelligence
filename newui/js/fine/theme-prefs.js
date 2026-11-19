/* newui/js/fine/theme-prefs.js — 「主题/偏好状态/高亮」自定义空间模块（源自 ui-skinset.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'theme-prefs',
  source: 'ui-skinset.js',
  space: '主题/偏好状态/高亮',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  /* [头部公共段并入] */
  const THEMES = [
    { key:'default', name:'深空紫', primary:'#6a5cff', secondary:'#ff6ec7', accent:'#ffd166', dark:'#14102e', light:'#c9b8ff' },
    { key:'teal',    name:'青碧',   primary:'#0fb9b1', secondary:'#ffe066', accent:'#ff6b6b', dark:'#0d1f2d', light:'#9be8e2' },
    { key:'sunset',  name:'落日',   primary:'#ff7b54', secondary:'#ffd166', accent:'#7bdff2', dark:'#241311', light:'#ffc4a3' },
    { key:'royal',   name:'皇家蓝', primary:'#3a86ff', secondary:'#8338ec', accent:'#ffbe0b', dark:'#101528', light:'#a9c6ff' },
    { key:'moss',    name:'苔原',   primary:'#38b000', secondary:'#a7c957', accent:'#ffe066', dark:'#12210f', light:'#c9f0a2' },
    { key:'orchid',  name:'幽兰',   primary:'#9d4edd', secondary:'#ff70a6', accent:'#ffd166', dark:'#1a1030', light:'#e0b8ff' },
    { key:'ember',   name:'余烬',   primary:'#fb5607', secondary:'#ff006e', accent:'#ffe066', dark:'#221014', light:'#ffb28f' },
  ];
  const DEF_THEME = THEMES[0];
  /* 偏好状态（本地镜像；与后端 /api/ui/prefs 双向同步）
     R56：bgmode 只存 poster|acrylic（去掉 flow 流动水样） */
  let uiThemeKey  = 'default';
  let uiAccent    = '';          // 用户高亮色覆盖（空 = 跟随主题/封面动态）
  let uiBgMode    = 'poster';    // poster（歌词海报）| acrylic（亚克力封面）
  /* ═══ R78：背景内容偏好 —— wallpaper 来源 ═══
     'cover'  = 跟随歌曲封面（默认；无封面时 ensureDefaultSkin logo 临时垫底）
     'default'= 固定 Radio Intelligence 玻璃 logo（applyBrandSkin，切歌不被封面覆盖）
     与 body.wp-active 语义一致，但**显式持久化** —— 旧实现只临时挂 class、
     persistPrefs 白名单里又没有 wallpaper → 选完刷新即失、重启即丢。
     ⚠ 区别于 theme/bgmode：wallpaper 只决定"背景内容"，不参与 CSS 变量。 */
  let uiWallpaper  = 'cover';    // cover（跟随封面）| default（内置 logo 壁纸）

  /* ═══ R93 · 叠层效果（on-top effect，P4 契约）═══
     与「背景效果」（bgmode）**并列的独立维度**：ontop-effect 叠在背景效果之上，
     两者可同时生效（路线 A）。
       · 'none'  = 无效果（常驻卡；等价于关水面）
       · 'water' = 水面（首个 ontop-effect 插件，渲染在 ui-water.js 的 __water 里）
     ⚠ 旧 ui_prefs.water 保留为**只读兼容**：两向同步（任一变化都写回 water），
       避免破坏 verify_r86_water / verify_water_r70 的既有契约。 */
  let uiOnTop = 'water';
  /* 水面参数组（选中「水面」时才可编辑；整组写盘 → 后端白名单透传，不做嵌套合并）。
     specExp（S1 波纹细度）在 ui-water.js 暴露 setSpecExp 之前 UI 保持 disabled。 */
  let uiWaterOpts = {
    physics: true, eventMode: 'emotion', bank: 'bank',
    /* ★★ R110-②（2026-09-13）：\`semStrength\` 0.6 → **1.0**（与 \`ui-water.js\` 的
       \`let semStrength\` 同步）。⚠ 这两处 + \`ui-water.js::resetOpts()\` 是**同一个值的
       3 个定义点**，且**还有 \`localStorage['r54.ui']\` 持久化**——
       只改 \`ui-water.js\` 会被本文件在 init 时用 \`setSemStrength\` 覆盖回去（实测踩过）。
       ⇒ 值的三处定义 + \`ui-init.js\` 的一次性迁移（\`semOptV\`）必须一起改。 */
    semStrength: 1.0, semPulse: false, tier: 'Q3',
    /* ★★ R110（2026-09-13）：**偏好迁移标记** —— 只增不改。
       本组会被**原样存盘**（localStorage + 后端），且恢复逻辑是"存档里有的就覆盖"
       ⇒ 用户上一次存的 \`semStrength:0.6\` 会**静默盖掉新默认**（实测踩到：
       只改 \`ui-water.js\` 的 \`let semStrength\` 时，真机 C5 纹丝不动）。
       \`ui-init.js::migrateSemOpts()\` 据此**只强制刷一次**，之后用户手动调的值照常被尊重。 */
    semOptV: 1,
    specExp: -2.0
  };
  const WATER_OPTS_DEF = JSON.parse(JSON.stringify(uiWaterOpts));

  /* 高亮色是否被用户锁死（主题或手动取色）—— 锁死时封面动态取色不再覆盖 */
  let accentLocked = false;
  function lockAccent(locked){ accentLocked = !!locked; }

`;
    const __epi = `  for (const __n of ["THEMES", "DEF_THEME", "uiThemeKey", "uiAccent", "uiBgMode", "uiWallpaper", "uiOnTop", "uiWaterOpts", "WATER_OPTS_DEF", "accentLocked", "lockAccent"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
