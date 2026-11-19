/* newui/js/fine/orbit-geo.js — 「圆盘几何」自定义空间模块（源自 ui-orbit.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'orbit-geo',
  source: 'ui-orbit.js',
  space: '圆盘几何',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  /* [头部公共段并入] */
  const ORBIT_MODE_ORDER = ['wander', 'fav', 'plist'];
  const ORBIT_IDS = { wander:'o-wander', fav:'o-fav', plist:'o-playlist' };
  const orbitEls = {};
  ORBIT_MODE_ORDER.forEach(m => { orbitEls[m] = $(ORBIT_IDS[m]); });

  // 几何参数：baseDy/baseDx/baseR1 是 1080p 视口下的基准值，按 innerHeight/2 缩放
  const ORBIT_R = 54;          // 按钮半径
  const ORBIT_EDGE = 36;       // 上下按钮的露边距（越小露出越少）
  const baseDy = 464.8, baseDx = 265, baseR1 = 535;
  const ORBIT_O_SHIFT = 27;
  function orbitGeo(){
    const k = Math.min(1, (innerHeight / 2 - ORBIT_EDGE) / baseDy);
    return { k, R1: baseR1 * k, dx: baseDx * k, dy: baseDy * k, R2: baseR1 * k + 99 };
  }
  function orbitOC(){
    const g = orbitGeo();
    return { x: innerWidth + g.dx - ORBIT_R + ORBIT_O_SHIFT, y: innerHeight / 2 };
  }
`;
    const __epi = `  for (const __n of ["ORBIT_MODE_ORDER", "ORBIT_IDS", "orbitEls", "ORBIT_R", "ORBIT_EDGE", "baseDy", "ORBIT_O_SHIFT", "orbitGeo", "orbitOC"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
