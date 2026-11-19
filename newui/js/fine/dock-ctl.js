/* newui/js/fine/dock-ctl.js — 「dock控制/播放三键」自定义空间模块（源自 ui-dock.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'dock-ctl',
  source: 'ui-dock.js',
  space: 'dock控制/播放三键',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  /* [头部公共段并入] */
  const dock = $('dock');
  dock.addEventListener('mouseenter', () => dock.classList.add('hovering'));
  dock.addEventListener('mouseleave', () => dock.classList.remove('hovering'));
  /* 播放头靠近渐显（原 UI L2798-2806）—— dock 已声明，此处绑定才安全 */
  bindFrameHeadProximity();

  /* ⚠ 播放控制：乐观更新 + 防连点
     原问题：点击后要等后端 + 2s 轮询才有反馈，用户以为没点上 → 连点 →
     "一下子跳两首"。这里点击后**立即**在本地翻转状态给反馈，
     并加 350ms 防连点窗口。 */
  let ctlBusy = 0;
  function ctl(action, optimistic){
    const now = Date.now();
    if (now - ctlBusy < 350) return;    // 防连点
    ctlBusy = now;
    if (optimistic) optimistic();       // 立即给视觉反馈
    action().then(() => setTimeout(poll, 150)).catch(() => setTimeout(poll, 400));
  }

  /* ⚠⚠ force_on 表达的是**意图**（"想进入播放态吗"），绝不是常量 true
     （R47「有的时候播放暂停不了」的根因）：
     后端 \`if force_on == playing: return\` —— 恒传 true 时，正在播放 →
     True == True → **一次 toggle 都不发** → 暂停永远点不动；
     只有后端把 playing 误判成 False 时才偶然生效，所以是"时灵时不灵"。
       · 想暂停 → force_on=false → 后端 False != True → 真正 toggle
       · 想播放 → force_on=true  → 无当前曲时冷启动拉起电台 */
  $('d-play').onclick = () => {
    const want = !st.playing;
    ctl(() => post('/toggle', 'force_on=' + (want ? 'true' : 'false')),
        () => { st.playing = want; render(); });        // 乐观翻转播放态
  };
  $('d-prev').onclick = () => ctl(() => post('/prev'));
  $('d-next').onclick = () => ctl(() => post('/next'));

`;
    const __epi = `  for (const __n of ["dock", "ctlBusy", "ctl"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
