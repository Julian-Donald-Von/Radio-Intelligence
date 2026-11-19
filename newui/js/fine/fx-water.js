/* newui/js/fine/fx-water.js — 「高级视觉调参」自定义空间模块（源自 ui-settings.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'fx-water',
  source: 'ui-settings.js',
  space: '高级视觉调参',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  /* ═══ 2026-09-19：高级设置 · 视觉效果（水面调参，绑定 __water.tune）═══
     设计：schema 驱动 —— **接入新的效果层只需往数组里加一项**，不必再动 HTML/事件。
     每一项的语义（与 ui-water.js 内 TUNE 注释一致）：
       bright  整体亮度倍率（乘在最终水色上，不改透明度）
       interf  干涉等值线强度（去掉直流后的波幅等高线；0=关）
       interfN 等值线密度（每单位 ampN 的线数，越大越密）
       foam    坡度亮线增益（旧「波峰窄亮线」，保留作一般波纹轮廓）
       gSat    高波数额外阻尼（越小短波活得越久 ⇒ 波纹越细 ⇒ 干涉越可辨）
       rain    低密度欢快雨（真实涟漪；0=关）
       drawDpr DRAW 分辨率上限（性能；越小越省 GPU，给音频留余量） */
  const FX_WATER_SCHEMA = [
    { k: 'bright',  label: '整体亮度',        min: 0.30, max: 1.00, step: 0.01, dp: 2 },
    { k: 'refractMul', label: '横向扰动（折射）★', min: 0.5, max: 10, step: 0.1, dp: 1 },
    { k: 'foam',    label: '坡度亮线增益',    min: 0,    max: 2,    step: 0.05, dp: 2 },
    { k: 'softAmp', label: '源衰减（0=关·默认）', min: 0, max: 5, step: 0.1, dp: 1 },
    { k: 'advect',  label: '微涟漪平流（0=关·会平移）', min: 0, max: 1.0, step: 0.02, dp: 2 },
    { k: 'fineDens', label: '细波纹随激动变密',    min: 0, max: 2, step: 0.05, dp: 2 },
    { k: 'freezeDamp', label: '凝结阻尼（0=关）',  min: 0, max: 3, step: 0.05, dp: 2 },
    { k: 'subGrid', label: '亚网格耗散（消像素块）', min: 0, max: 4, step: 0.05, dp: 2 },
    { k: 'purgeCalm', label: '换曲平静（0=不压）', min: 0, max: 1, step: 0.05, dp: 2 },
    { k: 'freeze',  label: '凝结强度（安静时冻住）', min: 0, max: 1, step: 0.05, dp: 2 },
    { k: 'autoTier', label: '自动调档（0=关·固定Q3）', min: 0, max: 1, step: 1, dp: 0 },
    { k: 'debugAmp', label: '〔调试〕幅度 X 光', min: 0, max: 1, step: 1, dp: 0 },
    { k: 'ug',      label: '水平动量系数',        min: 0, max: 3, step: 0.05, dp: 2 },
    { k: 'interf',  label: '〔实验〕等高线·强度', min: 0, max: 0.25, step: 0.005, dp: 3 },
    { k: 'interfN', label: '〔实验〕等高线·密度', min: 0.5, max: 6,  step: 0.1,  dp: 1 },
    { k: 'gSat',    label: '短波耗散（越小越细）', min: 0, max: 1.5, step: 0.05, dp: 2 },
    { k: 'rain',    label: '欢快雨（0=关）',  min: 0,    max: 2,    step: 0.05, dp: 2 },
    { k: 'drawDpr', label: '性能·分辨率上限', min: 0.6,  max: 1.5,  step: 0.05, dp: 2 }
  ];
  /* ★ 2026-09-19 09:3x：目标容器搬到**皮肤卡**（换肤 → 叠层效果 → 水面参数 → 高级）。
     版式**复用皮肤卡既有的 \`.wo-row / .wo-range / .wo-val\`**（不再自造 .fxw-* 类），
     档位也不再自建下拉 —— 皮肤卡本来就有「画质档位」#wo-tier 分段。 */
  function buildFxWater(){
    const host = document.getElementById('wo-fx-body');
    if (!host) return;
    const w = window.__water;
    const st = (w && w.tuneState) ? (w.tuneState() || {}) : {};
    host.innerHTML = FX_WATER_SCHEMA.map(function(it){
      const raw = st[it.k];
      const v = (typeof raw === 'number' && isFinite(raw)) ? raw : it.min;
      return '<div class="wo-row">'
        + '<label class="set-label">' + it.label + '</label>'
        + '<input type="range" class="wo-range" data-fxw="' + it.k + '" min="' + it.min
        + '" max="' + it.max + '" step="' + it.step + '" value="' + v + '">'
        + '<span class="wo-val" data-k="' + it.k + '">' + v.toFixed(it.dp) + '</span>'
        + '</div>';
    }).join('')
      + '<div class="wo-btns"><button class="set-mini-btn" id="wo-fx-reset">恢复默认</button></div>';
    if (host._fxBound) return;
    host._fxBound = true;
    host.addEventListener('input', function(e){
      const el = e.target;
      const k = el && el.getAttribute ? el.getAttribute('data-fxw') : null;
      if (!k || !window.__water || !window.__water.tune) return;
      const v = parseFloat(el.value);
      if (!isFinite(v)) return;
      const o = {}; o[k] = v;
      try { window.__water.tune(o); } catch (_) {}
      const it = FX_WATER_SCHEMA.filter(function(x){ return x.k === k; })[0];
      const out = host.querySelector('.wo-val[data-k="' + k + '"]');
      if (out) out.textContent = v.toFixed(it ? it.dp : 3);
    });
    const rb = document.getElementById('wo-fx-reset');
    if (rb) rb.addEventListener('click', function(){
      try { if (window.__water && window.__water.resetOpts) window.__water.resetOpts(); } catch (_) {}
      host._fxBound = false;
      buildFxWater();
    });
  }

  /* 黑盒自检面（offscreen 验证用）：暴露渲染/刷新/大纲，供外部断言 */
  window.__r64 = {
    schema: function(){ return SETTING_SCHEMA; },
    groups: function(){
      /* R67-2：动态分组渲染进 .settings-dynamic[data-gid]（4 tab 结构） */
      return Array.prototype.map.call(
        document.querySelectorAll('.settings-dynamic .set-group'),
        function(g){ return g.dataset.gid; });
    },
    /* R67-2：切 tab（黑盒验证用；等于点 .stab） */
    tab: function(k){
      const b = document.querySelector('#settings-tabs .stab[data-tab="' + k + '"]');
      if (!b) return false;
      b.click();
      return true;
    },
    activeTab: function(){
      const p = document.querySelector('#settings-panel .stab-panel.active');
      return p ? p.dataset.tab : '';
    },
    render: function(){ renderSettingsGroups(); },
    qrModalOpen: function(){
      const m = $('qr-modal'); return !!(m && m.classList.contains('open'));
    },
  };
`;
    const __epi = `  for (const __n of ["FX_WATER_SCHEMA", "buildFxWater"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
