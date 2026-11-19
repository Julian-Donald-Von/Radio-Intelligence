/* newui/js/fine/ontop-water.js — 「叠层水面+参数组」自定义空间模块（源自 ui-skinset.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'ontop-water',
  source: 'ui-skinset.js',
  space: '叠层水面+参数组',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  /* ═══ R93 · 叠层效果（on-top effect）选择器 + 水面参数区 ═══
     形态与「背景效果」如出一辙：分段卡 n 选一（复用 .wp-type）+ 常驻「无效果」卡；
     选中「水面」时下方 #water-opts 展开（P4 :107 = 每个 ontop 插件自带 1 个参数组）。

     职责边界（避免两处各写一份点亮态而分叉）：
       · 本文件 = #ontop-row / 参数区的**唯一拥有者**（UI 镜像 + 偏好落盘）；
       · ui-water.js = \`enabled\` 的**唯一拥有者**（setEnabled 仍是唯一启停入口）；
       · 桥梁 = setOnTop() 调 __water.setEnabled()，ui-water 的 syncToggle() 反调 syncOnTopUI()。 */
  const ONTOP_IDS = ['none', 'water'];
  function onTopNormalize(v){ return (ONTOP_IDS.indexOf(v) >= 0) ? v : 'none'; }
  /* 唯一入口：卡片选中 → 启停水面 + 落盘（ontop 新键 / water 旧键两向同步） */
  function setOnTop(id, persist){
    uiOnTop = onTopNormalize(id);
    syncOnTopUI();
    /* ⚠ persist=false：本函数自己会把 ontop + water 一次落盘。若让 __water.setEnabled
       也落盘（默认 true），同一次点击会发**两个并发 POST** → 后端「读-改-写」互踩，
       把 ui_prefs.json 写成坏文件（R93 实测 → 全部偏好静默回默认）。 */
    try { if (window.__water && window.__water.setEnabled)
            window.__water.setEnabled(uiOnTop === 'water', false); } catch (_) {}
    if (uiOnTop === 'water') applyWaterOpts();     // 切回水面 → 按存档重新下发（切档保场）
    if (persist !== false) persistPrefs({ ontop: uiOnTop, water: uiOnTop === 'water' }, true);
  }
  function syncOnTopUI(){
    const row = $('ontop-row');
    if (row) row.querySelectorAll('.wp-type').forEach(b =>
      b.classList.toggle('active', b.dataset.ontop === uiOnTop));
    const box = $('water-opts');
    if (box) box.classList.toggle('open', uiOnTop === 'water');
  }
  /* 把 uiWaterOpts 下发给 __water（每个 key 独立 try —— 某 API 未实现不影响其余） */
  function applyWaterOpts(){
    const w = window.__water; if (!w) return;
    const o = uiWaterOpts;
    const call = (fn, arg) => { try { if (typeof w[fn] === 'function') w[fn](arg); } catch (_) {} };
    call('setPhysics',        o.physics === false ? 'off' : 'on');
    call('setEventMode',      o.eventMode);
    call('setWaveBank',       o.bank);
    call('setSemStrength',    o.semStrength);
    call('setTransitionPulse', !!o.semPulse);
    call('setTier',           o.tier);
    /* specExp → 「波纹细度」：R92-S1 已落地（\`ui-water.js\` 暴露 setSpecExp/specExp，
       范围与控件 min/max 严格一致 [−2.0, −1.5]，不存在拖不动/被静默夹掉的死区） */
    call('setSpecExp',        o.specExp);
    syncWaterOptsUI();
  }
  /* 参数区控件回填（展开 / 换档 / 恢复默认 / 启动恢复偏好后都要刷） */
  function syncWaterOptsUI(){
    const o = uiWaterOpts;
    /* ⚠ R94：只在**真的不同**时才写回控件 —— 否则拖动中的 \`input → apply → 回填\`
       会把滑杆自身再写一次，与指针抢同一个元素（值相等时写回纯属自找麻烦）。 */
    const set = (id, v) => {
      const el = $(id);
      if (el && el.value !== String(v)) el.value = String(v);
    };
    const txt = (id, v) => { const el = $(id); if (el) el.textContent = v; };
    set('wo-specexp', o.specExp);
    txt('wo-specexp-v', Number(o.specExp).toFixed(1));
    /* 谱形标签跟着指数走（自解释）：−2.0 = Phillips/Tessendorf 物理谱（a ∝ k⁻²）；
       越往 −1.5 越接近饱和/平衡区那一支文献（a ∝ k^−1.5，更细） */
    txt('wo-specexp-tag', Number(o.specExp) <= -1.99 ? 'k⁻² 物理谱' : 'k⁻¹·⁵ 饱和区');
    set('wo-sem', o.semStrength);
    txt('wo-sem-v', Number(o.semStrength).toFixed(2));
    const ph = $('wo-physics'); if (ph) ph.checked = o.physics !== false;
    const pu = $('wo-pulse');   if (pu) pu.checked = !!o.semPulse;
    segSync('wo-eventmode', o.eventMode);
    segSync('wo-bank', o.bank);
    segSync('wo-tier', o.tier);
  }
  function segSync(id, val){
    const box = $(id); if (!box) return;
    box.querySelectorAll('.wp-type').forEach(b =>
      b.classList.toggle('active', b.dataset.v === String(val)));
  }
  /* 改一个/多个参数 → 落盘 + 立即下发（参数区改动即时可听可见，与画质档同款）
     ⚠ R94：\`immediate=true\` —— 参数区控件都是**离散终态**动作，跳过去抖直接落盘
     （避免"点完 300ms 内 F5"丢掉这次改动）；滑杆自己走 input/change 分流。 */
  function waterOptsPatch(patch){
    for (const k in patch) uiWaterOpts[k] = patch[k];
    persistPrefs({ waterOpts: uiWaterOpts }, true);
    applyWaterOpts();
  }
  function bindSeg(id, cb){
    const box = $(id); if (!box) return;
    box.querySelectorAll('.wp-type').forEach(b =>
      b.addEventListener('click', () => cb(b.dataset.v)));
  }
  function bindWaterOpts(){
    /* ⚠ R94：拖动中**只下发、不落盘**，\`change\`（松手）时才一次性落盘 ——
       与下面「情绪强度」同款。旧实现每个 input 都 POST 整组 waterOpts，
       一次拖拽几十个并发写，落盘顺序不可控 → 停在中间态（见 persistPrefs 注释）。 */
    const se = $('wo-specexp');
    if (se){
      const mirror = () => {
        uiWaterOpts.specExp = Number(se.value);
        const v = $('wo-specexp-v'); if (v) v.textContent = Number(se.value).toFixed(1);
        const t = $('wo-specexp-tag');
        if (t) t.textContent = Number(se.value) <= -1.99 ? 'k⁻² 物理谱' : 'k⁻¹·⁵ 饱和区';
      };
      se.addEventListener('input', () => {
        mirror();
        try { if (window.__water && window.__water.setSpecExp)
                window.__water.setSpecExp(uiWaterOpts.specExp); } catch (_) {}
      });
      se.addEventListener('change', () => {
        mirror();
        persistPrefs({ waterOpts: uiWaterOpts }, true);   // 松手：跳过去抖，立刻落盘
        applyWaterOpts();                                 // 再确认一次终值（防拖动末帧丢失）
      });
    }
    /* 情绪强度滑杆：拖动中只轻量下发（不落盘/不整组刷 UI，避免拖动卡顿），
       release（change）时一次性持久化。 */
    const sm = $('wo-sem');
    if (sm){
      sm.addEventListener('input', () => {
        uiWaterOpts.semStrength = Number(sm.value);
        const v = $('wo-sem-v'); if (v) v.textContent = Number(sm.value).toFixed(2);
        try { if (window.__water && window.__water.setSemStrength)
                window.__water.setSemStrength(uiWaterOpts.semStrength); } catch (_) {}
      });
      sm.addEventListener('change', () => persistPrefs({ waterOpts: uiWaterOpts }, true));
    }
    const ph = $('wo-physics');
    if (ph) ph.addEventListener('change', () => waterOptsPatch({ physics: ph.checked }));
    const pu = $('wo-pulse');
    if (pu) pu.addEventListener('change', () => waterOptsPatch({ semPulse: pu.checked }));
    bindSeg('wo-eventmode', v => waterOptsPatch({ eventMode: v }));
    bindSeg('wo-bank',      v => waterOptsPatch({ bank: v }));
    bindSeg('wo-tier',      v => waterOptsPatch({ tier: v }));
    /* 换一批波形：确定性重掷随机银行（rndSeed 同种子 → 逐位可复现） */
    const rer = $('wo-reroll');
    if (rer) rer.addEventListener('click', () => {
      let s = 1;
      try { s = (Date.now() % 100000) | 0; } catch (_) {}
      try { if (window.__water && window.__water.rndSeed) window.__water.rndSeed(s); } catch (_) {}
      toast('波形已重掷');
    });
    /* 恢复默认：本地参数组回出厂 + __water 回观测基线（SEM_BASE / 默认档位） */
    const rst = $('wo-reset');
    if (rst) rst.addEventListener('click', () => {
      uiWaterOpts = JSON.parse(JSON.stringify(WATER_OPTS_DEF));
      persistPrefs({ waterOpts: uiWaterOpts }, true);
      try { if (window.__water && window.__water.resetOpts) window.__water.resetOpts(); } catch (_) {}
      applyWaterOpts();
      toast('水面参数已恢复默认');
    });
    const adv = $('wo-adv'), advBtn = $('wo-adv-btn');
    if (adv && advBtn) advBtn.addEventListener('click', () => adv.classList.toggle('open'));
  }

  /* ---- 自定义壁纸导入（老 UI wp-type-row / wp-input / importWallpaper 移植）
       支持图片/视频/链接/壁纸引擎；前端即时把源喂给背景（bgimg 亚克力或
       Poster 层），并经后端 WallpaperImporter 持久化。 */
  let wpType = 'image';
  /* cover=歌曲封面 | image=图片 | link=链接 | video=视频壁纸 | wallpaper=壁纸引擎工程
     ⚠ R62 #10：恢复 video / wallpaper 两型（R61 曾移除）。用户原话：作为按钮
     放皮肤自定义设置里，**共用一个导入框**。video 前端直接可用（#bgvideo +
     FileReader/dataURL/直链）。
     ⚠ R65：wallpaper 真导入 —— 后端 WallpaperImporter 本就能解析工程
     project.json / Steam 创意工坊链接 / zip：scene 3D 壁纸退化为 preview 图、
     video/web 场景正常渲染（core/skin.py WallpaperEngineSkin.render_descriptor），
     并非「必须桌面端」。前端接 REST：Steam 链接/ID → import-engine；本地工程
     zip → import-zip 上传。 */
`;
    const __epi = `  for (const __n of ["ONTOP_IDS", "onTopNormalize", "setOnTop", "syncOnTopUI", "applyWaterOpts", "syncWaterOptsUI", "segSync", "waterOptsPatch", "bindSeg", "bindWaterOpts", "wpType"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
