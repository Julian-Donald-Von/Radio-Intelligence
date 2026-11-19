/* newui/js/fine/skinset-bind.js — 「设置事件绑定」自定义空间模块（源自 ui-skinset.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'skinset-bind',
  source: 'ui-skinset.js',
  space: '设置事件绑定',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  function bindSettingsEvents(){
    /* ═══ R62 #8：设置分组折叠（移植老 UI .set-fold 折叠机制）═══
       点 data-foldable 分组的 h4 → 整组 collapsed 折叠/展开。
       委派在 document 上：动态渲染的分组（如不喜欢列表刷新）也生效。 */
    document.addEventListener('click', ev => {
      const h = ev.target && ev.target.closest
        ? ev.target.closest('.set-group[data-foldable] > h4') : null;
      if (!h) return;
      const grp = h.parentElement;
      if (grp) grp.classList.toggle('collapsed');
    });
    const gear   = $('settings-gear');
    /* ═══ R62 #9：本地曲库导入 / 重新扫描 ═══
       导入文件夹：POST /api/library/import/folder?folder=…（后端扫描入库）。
       重新扫描：POST /api/library/scan（扫配置根目录）。成功后刷新曲库列表
       （若侧边面板正开在曲库 tab）并 toast 结果。 */
    const libImp = $('lib-import-folder'), libInp = $('lib-folder-input');
    if (libImp && libInp) libImp.addEventListener('click', () => {
      const folder = libInp.value.trim();
      if (!folder){ toast('请先输入文件夹路径'); return; }
      fetch('/api/library/import/folder?folder=' + encodeURIComponent(folder),
            { method:'POST' })
        .then(r => r.json()).then(d => {
          if (d && d.ok) toast('已导入 ' + (d.added || 0) + ' 首到曲库');
          else toast((d && d.error) || '导入失败');
        }).catch(() => toast('导入失败：后端未连接'));
    });
    const libScan = $('lib-scan');
    if (libScan) libScan.addEventListener('click', () => {
      fetch('/api/library/scan', { method:'POST' })
        .then(r => r.json()).then(d => {
          if (d && d.ok) toast('扫描完成，新增 ' + (d.added || 0) + ' 首');
          else toast((d && d.error) || '扫描失败');
        }).catch(() => toast('扫描失败：后端未连接'));
    });
    const hanger = $('theme-hanger');
    const setClose = $('set-close');
    const themeClose = $('theme-close');
    if (gear) gear.addEventListener('click', () => {
      if ($('settings-panel') && $('settings-panel').classList.contains('open')) closeSettings();
      else openSettings();
    });
    if (hanger) hanger.addEventListener('click', () => {
      if ($('theme-panel') && $('theme-panel').classList.contains('open')) closeTheme();
      else openTheme();
    });
    /* R67-2：原「打开换肤面板」入口（设置卡内 appeal section 的按钮）随整个
       外观 section 一并删除 —— 用户原话"皮肤界面在换肤按钮里，设置页保留=冗余"。
       换肤卡仍由 #theme-hanger 衣架钮 / openTheme() 走原路径。 */
    if (setClose) setClose.addEventListener('click', closeSettings);
    if (themeClose) themeClose.addEventListener('click', closeTheme);
    /* 外点关闭：点任一打开的卡 + 两按钮之外 → 全关 */
    document.addEventListener('click', ev => {
      if (!anyDrawerOpen()) return;
      const t = ev.target;
      if (!t || !t.closest) return;
      if (t.closest('#settings-panel') || t.closest('#theme-panel') ||
          t.closest('#settings-gear') || t.closest('#theme-hanger')) return;
      if ($('settings-panel') && $('settings-panel').classList.contains('open')) closeSettings();
      if ($('theme-panel') && $('theme-panel').classList.contains('open')) closeTheme();
    });
    /* Esc 优先关抽屉（先关设置卡，再关换肤卡） */
    document.addEventListener('keydown', ev => {
      if (ev.key !== 'Escape') return;
      if ($('settings-panel') && $('settings-panel').classList.contains('open')){
        closeSettings(); ev.preventDefault();
      } else if ($('theme-panel') && $('theme-panel').classList.contains('open')){
        closeTheme(); ev.preventDefault();
      }
    });
    /* 背景模式按钮 */
    const bgRow = $('bg-mode-row');
    if (bgRow) bgRow.querySelectorAll('.wp-type').forEach(b => {
      b.addEventListener('click', () => setBgMode(b.dataset.bgmode));
    });
    /* 叠层效果卡片（R93）：点击即切换（segmented card switcher；沿用 dock 铁则
       「简单点亮、禁 :active scale」，无滑块）+ 参数区控件绑定与首次回填。 */
    const onTopRow = $('ontop-row');
    if (onTopRow) onTopRow.querySelectorAll('.wp-type').forEach(b => {
      b.addEventListener('click', () => setOnTop(b.dataset.ontop));
    });
    bindWaterOpts();
    syncOnTopUI();
    applyWaterOpts();
    /* 音频律动开关 */
    const beatToggle = $('beat-toggle');
    if (beatToggle){
      beatToggle.addEventListener('change', () => setBeat(beatToggle.checked));
      beatToggle.checked = uiBeat;
    }
    /* 高亮色 picker / reset
       ⚠ R94：\`input\`（拖动中连发）保去抖；\`change\`（松手/关闭取色器）立即落盘收尾。 */
    const picker = $('fx-accent-picker');
    if (picker){
      picker.addEventListener('input', () => {
        uiAccent = picker.value || '';
        persistPrefs({ accent: uiAccent, theme: uiThemeKey });
        applyPrefsTheme();
      });
      picker.addEventListener('change', () => {
        uiAccent = picker.value || '';
        persistPrefs({ accent: uiAccent, theme: uiThemeKey }, true);
        applyPrefsTheme();
      });
    }
    const reset = $('fx-accent-reset');
    if (reset) reset.addEventListener('click', () => {
      uiAccent = '';
      persistPrefs({ accent: '', theme: uiThemeKey }, true);
      applyPrefsTheme();
      /* R66：取色器即时回到主题色，肉眼可见 → 不再 toast。 */
    });
    bindWallpaper();
  }

  /* ---- R57-6 · 左下按钮 hover 显现：驻留 15% 透明，靠近本体/热区即复原 ----
     齿轮(#settings-gear)与衣架(#theme-hanger)在左下角 dock 左外侧纵向排列。
     指针距任一按钮中心 < 72px（大于按钮半径 26 → 感应区比按钮大一圈）
     或正悬停其上 → 摘 .dim（opacity 1）；离开 → 加 .dim（15%）。
     避免按钮在低透明下点不中：pointer-events 恒 auto。 */
`;
    const __epi = `  for (const __n of ["bindSettingsEvents"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
