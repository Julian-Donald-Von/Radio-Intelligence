/* newui/js/effects-boot.js — 效果插件装配入口。
 * 注册所有可插拔效果（叠层水面 / 背景亚克力），接管换肤页 UI 与参数区。
 * 加新效果 = 在这里多一行 import + install()，UI 自动从注册表生成。
 */
import effects from './effects.js';
import { install as installWater } from './effects/water.js';
import { install as installAcrylic } from './effects/acrylic.js';
import { install as installSonic } from './effects/sonic.js';

let installed = false;
export function bootEffects(){
  if (installed) return effects.snapshot();
  installed = true;
  installAcrylic();          // 背景效果：poster / acrylic
  installWater();            // 叠层效果：water
  installSonic();            // 叠层效果：sonic（音域回响 3D 地形，情绪驱动）
  effects.restore();         // 恢复上次选择（插件自己的偏好）
  /* ★ 2026-11-19 Qt 记忆修复：Qt WebEngine 退出时 localStorage(r54.effects) 会丢失，
     但后端 /api/ui/prefs 存盘不丢。若本地存档为空，用后端值补恢复 ontop/bg。
     runtime.js 只认 none|water，sonic/reactorcore 靠这里恢复。 */
  try {
    if (!localStorage.getItem('r54.effects')) {
      fetch('/api/ui/prefs', { cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          const p = (d && d.prefs) ? d.prefs : null;
          if (!p) return;
          if (p.ontop && effects.getActive('ontop') === 'none' &&
              effects.list('ontop').some(function (x) { return x.id === p.ontop; })) {
            effects.setActive('ontop', p.ontop, { persist: false });
          }
          if ((p.bgmode === 'poster' || p.bgmode === 'acrylic') &&
              effects.getActive('bg') !== p.bgmode) {
            effects.setActive('bg', p.bgmode, { persist: false });
          }
        })
        .catch(function () {});
    }
  } catch (_e) {}
  // T13：叠层初值与水体实际状态对齐——水体 loadPref 默认开，若插件 restore 到 none
  // 会调 setEnabled(false) 把水关掉（用户看到"没水"的真因）。
  // 没有明确 ontop 偏好但水体开着时，跟随水体选 water。
  try {
    const raw = localStorage.getItem('r54.effects');
    const saved = raw ? JSON.parse(raw) : null;
    const w = window.__water;
    if ((!saved || typeof saved.ontop !== 'string') && w && typeof w.enabled === 'function' && w.enabled()
        && effects.getActive('ontop') === 'none'){
      effects.setActive('ontop', 'water', { persist: false });
    }
    /* 迁移：存档里是已撤掉的插件（如旧的 fuelrod）时，回退到水体，
       避免出现「水面实际开着、但高亮停在无效果」的错位。 */
    if (saved && typeof saved.ontop === 'string'
        && saved.ontop !== 'reactorcore'   /* 2026-11-19：堆芯是异步注册，不是已撤插件 */
        && !effects.list('ontop').some((p) => p.id === saved.ontop)
        && w && typeof w.enabled === 'function' && w.enabled()){
      effects.setActive('ontop', 'water', { persist: true });
    }
  } catch (_e) {}
  effects.mountUI({
    ontopRow:  document.getElementById('ontop-row'),
    bgRow:     document.getElementById('bg-mode-row'),
    paramsHost: document.getElementById('water-opts'),
  });
  // T15：水面 canvas 尺寸修复——水体 classic script 在 DOM 布局前同步加载，
  // resize() 拿到 clientWidth=0 → 兜底 1280×720 缓冲，铺满真机视口时缩放采样
  // → "细密像素纹理"（老 UI 水体在 ui-eq 之后加载，DOM 已布局所以没事）。
  // DOM 就绪后强制重算一次：tune({drawDpr: 当前值}) 内部会 resize()。
  function refitWater(){
    try {
      const w = window.__water;
      if (!w || typeof w.tune !== 'function' || typeof w.tuneState !== 'function') return;
      const dpr = w.tuneState().drawDpr;
      /* T16：恢复老 UI 11-17 已修的「越播越细密」结构性修复。
         kEst 未归一化 → 幅度累积 → 高波数阻尼被放大 → 细纹被吃光只剩固定花样。
         A/B 口 kEstNorm=0.25（ui-water.js 已接线，无需重载）；ESM 拼接时丢了
         老 UI 的下发调用，这里补回。drawDpr 同传以触发 resize 重算画布尺寸。 */
      /* 2026-11-18：kEstNorm 统一由 /water-config.js 下发（两 UI 共用一套调参），这里只保留 resize。 */
            const patch = {};
      if (typeof dpr === 'number') patch.drawDpr = dpr;
      w.tune(patch);
    } catch (_e) {}
  }
  if (typeof requestAnimationFrame === 'function') requestAnimationFrame(refitWater);
  else setTimeout(refitWater, 0);

  // 堆芯是插件效果：只有已安装（/api/plugins/all 报告）才注册，换肤页才出现选项。
  fetch('/api/plugins/all', { cache: 'no-store' })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      var art = (d && d.art && d.art.plugins) || [];
      var hasCore = art.some(function (p) { return p.id === 'local.reactorcore'; });
      if (hasCore) {
        import('./effects/reactorcore.js').then(function (m) {
          m.install();
          /* 2026-11-19 记忆修复：restore() 早于本模块，此处补回堆芯选中态。
             Qt WebEngine 退出会丢 localStorage → 回退读后端 /api/ui/prefs。 */
          function activateCoreIfSaved() {
            try {
              const s = JSON.parse(localStorage.getItem('r54.effects') || '{}');
              if (s.ontop === 'reactorcore') { effects.setActive('ontop', 'reactorcore', { persist: false }); return true; }
            } catch (_e) {}
            return false;
          }
          if (!activateCoreIfSaved()) {
            fetch('/api/ui/prefs', { cache: 'no-store' })
              .then(function (r) { return r.json(); })
              .then(function (pd) {
                if (pd && pd.prefs && pd.prefs.ontop === 'reactorcore')
                  effects.setActive('ontop', 'reactorcore', { persist: false });
              }).catch(function () {});
          }
          // 堆芯是异步注册的：注册晚于首次 mountUI，需重建 tab 才会出现在换肤页。
          effects.mountUI({
            ontopRow:  document.getElementById('ontop-row'),
            bgRow:     document.getElementById('bg-mode-row'),
            paramsHost: document.getElementById('water-opts'),
          });
        });
      }
    })
    .catch(function () {});

  window.__newUIFeatures = window.__newUIFeatures || {};
  window.__newUIFeatures.effects = effects;
  return effects.snapshot();
}

// DOM 就绪后装配（main.js 是 module，天然 defer；但 UI 容器在 index.html 里已存在）
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', bootEffects, { once: true });
} else {
  bootEffects();
}

export default effects;
