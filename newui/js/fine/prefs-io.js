/* newui/js/fine/prefs-io.js — 「偏好落盘/去抖/序号」自定义空间模块（源自 ui-skinset.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'prefs-io',
  source: 'ui-skinset.js',
  space: '偏好落盘/去抖/序号',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  /* ═══ R94：偏好回传**加固**（合并 + 去抖 + 单调序号）═══
     事故（用户报"拖到 −1.5 再拖回 −2.0，水没变舒缓"）：
     滑杆的每个 \`input\` 都直接 POST 整组 waterOpts；一次拖拽 = 几十个并发 POST。
     后端 R93 的锁只保证**文件不会被写坏**，**不保证"最后动作者胜"** —— 线程池
     拿锁顺序与用户操作顺序无关 → 落盘可能停在**中间态**（本机实测 5 轮中 2 轮：
     拖回 −2.0 却存成 −1.8333 / −1.9231 / −1.5256）。F5 或重启后滑杆与水面一起
     停在中间值上，表现就是"拖回去了但没变舒缓"。
     三重加固：
       ① **合并**：同一短窗内的多次改动并成一次 POST（拖拽 40 次 → 1~2 次）；
       ② **单调序号**：每次 POST 带页面会话 id \`__sid\` + 自增 \`__seq\`，后端丢弃过期写；
       ③ **落盘时机**：滑杆拖动中只下发不落盘，\`change\`（松手）才落盘（与「情绪强度」同款）。 */
  const PREFS_DEBOUNCE_MS = 300;
  const PREFS_SID = 'p' + Date.now().toString(36)
                        + Math.random().toString(36).slice(2, 8);
  let prefsSeq = 0, prefsTimer = 0, prefsPending = {};

  /* 保存偏好：localStorage 即时（本地镜像，同步且廉价）+ 后端合并去抖写盘（静默失败）。
     ⚠ immediate=true 用于「松手/点选」这类**终态**动作 —— 跳过去抖，立刻落盘。 */
  function persistPrefs(patch, immediate){
    try {
      const f = JSON.parse(localStorage.getItem('r54.ui') || '{}');
      for (const k in patch) f[k] = patch[k];
      localStorage.setItem('r54.ui', JSON.stringify(f));
    } catch (_) {}
    for (const k in patch) prefsPending[k] = patch[k];
    if (immediate){ flushPrefs(); return; }
    if (!prefsTimer) prefsTimer = setTimeout(flushPrefs, PREFS_DEBOUNCE_MS);
  }
  /* 把累积的改动合成**一次** POST（带会话 id + 单调序号；后端按序号丢弃过期写） */
  function flushPrefs(){
    if (prefsTimer){ clearTimeout(prefsTimer); prefsTimer = 0; }
    const patch = prefsPending; prefsPending = {};
    const body = {};
    if ('theme' in patch) body.theme = patch.theme;
    if ('accent' in patch) body.accent = patch.accent;
    if ('bgmode' in patch) body.bgmode = patch.bgmode;
    if ('beat' in patch) body.beat = patch.beat;
    if ('water' in patch) body.water = patch.water;   // R70 背景水面开关（旧键兼容）
    if ('wallpaper' in patch) body.wallpaper = patch.wallpaper;  // R78 背景内容偏好
    if ('ontop' in patch) body.ontop = patch.ontop;              // R93 叠层效果（on-top）
    if ('waterOpts' in patch) body.waterOpts = patch.waterOpts;  // R93 水面参数组
    if (!Object.keys(body).length) return;
    body.__sid = PREFS_SID;
    body.__seq = ++prefsSeq;
    fetch('/api/ui/prefs', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)
    }).catch(() => {});
  }
  /* 离开页面时把还押在去抖窗里的改动补发（best-effort，失败也不阻塞关闭） */
  window.addEventListener('pagehide', () => { try { flushPrefs(); } catch (_) {} });

`;
    const __epi = `  for (const __n of ["PREFS_DEBOUNCE_MS", "PREFS_SID", "prefsSeq", "persistPrefs", "flushPrefs"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
