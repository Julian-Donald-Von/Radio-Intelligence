/* newui/js/fine/drawers.js — 「双抽屉推挤透视」自定义空间模块（源自 ui-skinset.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'drawers',
  source: 'ui-skinset.js',
  space: '双抽屉推挤透视',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  /* ---- R56 · 双抽屉（设置卡 + 换肤卡）开合 + 主界面推挤透视 ----
     任一卡 .open → body.drawer-open（驱动 #cards 左推透视）；
     两卡互斥：开一个自动关另一个。

     ═══ R57-9：推挤改**阻尼弹簧**驱动（用户原话："右侧设置没推动主界面
     透视…要阻尼感…滚轮隔离"）═══
     旧：body.drawer-open #cards 靠 CSS transition .55s 一次性滑到位。
     新：推挤 translateX / rotateY 由弹簧物理（阻尼振子）逐帧驱动 ——
       开抽屉 = 弹簧冲 1（主界面被推开 + 轻微过冲回弹），
       关抽屉 = 弹簧回 0（阻尼滑回，不硬切）。
       CSS body.drawer-open #cards 规则保留作**无 JS 兜底**；
       JS 运行时写内联 transform（transition:none）覆盖之，
       settle 后交还 CSS（清内联），关闭态不残留位移。 */
  let drawerSpr = { p:0, v:0, target:0, raf:0 };
  function applyDrawerTransform(){
    const el = $('cards'); if (!el) return;
    const p = drawerSpr.p;
    if (p <= 0.0006 && drawerSpr.target <= 0){
      /* settle 到关闭：清内联，交还 CSS 兜底（transform:''） */
      el.style.transition = '';
      el.style.transform = '';
      return;
    }
    const pushMax = Math.min(innerWidth * 0.09, 132);
    el.style.transition = 'none';
    el.style.transform = 'perspective(1500px) translateX(' +
      (-pushMax * p).toFixed(2) + 'px) rotateY(' + (6.5 * p).toFixed(3) + 'deg)';
  }
  function drawerSetTarget(t){
    drawerSpr.target = t;
    if (drawerSpr.raf) return;               // 已有 rAF 在跑：只改目标
    let last = performance.now();
    const tick = function(now){
      const dt = Math.min(0.05, (now - last) / 1000); last = now;
      const k = 165, d = 20;                 // 刚度/阻尼（轻微欠阻尼 → 一次小回弹）
      const a = (drawerSpr.target - drawerSpr.p) * k - drawerSpr.v * d;
      drawerSpr.v += a * dt;
      drawerSpr.p += drawerSpr.v * dt;
      if (Math.abs(drawerSpr.target - drawerSpr.p) < 0.0006 &&
          Math.abs(drawerSpr.v) < 0.004){
        drawerSpr.p = drawerSpr.target; drawerSpr.v = 0; drawerSpr.raf = 0;
      } else {
        drawerSpr.raf = requestAnimationFrame(tick);
      }
      applyDrawerTransform();
    };
    drawerSpr.raf = requestAnimationFrame(tick);
    applyDrawerTransform();                   // 首帧同步写一次（不押 rAF）
  }
  function openDrawer(which){
    const sp = $('settings-panel'), tp = $('theme-panel');
    const self = (which === 'theme') ? tp : sp;
    const other = (which === 'theme') ? sp : tp;
    if (!self) return;
    if (other) other.classList.remove('open');
    self.classList.add('open');
    document.body.classList.add('drawer-open');
    drawerSetTarget(1);            // R57-9：弹簧把主界面推出去（阻尼）
    refreshSettings();
  }
  function closeDrawer(which){
    const p = (which === 'theme') ? $('theme-panel') : $('settings-panel');
    if (!p) return;
    p.classList.remove('open');
    /* 两卡都关 → 解除推挤透视（R57-9：弹簧阻尼滑回 0） */
    if (!$('settings-panel').classList.contains('open') &&
        !$('theme-panel').classList.contains('open')){
      document.body.classList.remove('drawer-open');
      drawerSetTarget(0);
    }
  }
  function anyDrawerOpen(){
    return !!($('settings-panel') && $('settings-panel').classList.contains('open')) ||
           !!($('theme-panel') && $('theme-panel').classList.contains('open'));
  }
  function openSettings(){ openDrawer('settings'); }
  function closeSettings(){ closeDrawer('settings'); }
  function openTheme(){ openDrawer('theme'); }
  function closeTheme(){ closeDrawer('theme'); }
  function refreshSettings(){
    renderSkinList();
    syncBgModeUI();
    syncBeatUI();
    syncOnTopUI();          // R93：叠层效果卡片点亮态 + 参数区展开态
    syncWaterOptsUI();      // R93：水面参数区控件回填（外部改过 prefs 也不失真）
    renderDislikeList();
    /* R66：原「我的壁纸」网格已移除（用户判定冗余）→ 不再拉 /api/wallpapers 列表。
       本地图片导入仍走后端持久化，只是不再有"壁纸管理"UI。 */
    try { if (typeof renderSettingsGroups === 'function') renderSettingsGroups(); } catch (_) {}
  }

`;
    const __epi = `  for (const __n of ["drawerSpr", "applyDrawerTransform", "drawerSetTarget", "openDrawer", "closeDrawer", "anyDrawerOpen", "openSettings", "closeSettings", "openTheme", "closeTheme", "refreshSettings"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
