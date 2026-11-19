/* newui/js/fine/main-loop.js — 「主循环/轮询」自定义空间模块（源自 ui-init.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'main-loop',
  source: 'ui-init.js',
  space: '主循环/轮询',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  /* ═══════════════════════════════════════════════════════════════
     主循环（rAF）—— 原 UI 把 updateFrameProgress() 放在 mainLoop 里**每帧**跑。
     ⚠ 之前缺失这一环，进度条只在 2s 轮询的 progress 事件里更新，
     表现为：进度条两秒跳一格、播放头呼吸动画完全不动
     （sin(performance.now()) 需要逐帧重写 opacity 才看得出呼吸）。
     这里补上：播放中本地插值推进 position，后端 progress 事件负责校正。
     ═══════════════════════════════════════════════════════════════ */
  let _lastTick = performance.now();
  (function mainLoop(now){
    const dt = Math.min(250, now - _lastTick);   // 卡顿/切后台时钳位，避免进度暴冲
    _lastTick = now;
    /* ★ 2026-09-18：加 \`st.posLive\` 闸门 —— 只有后端报过真实播放位置(>0)才本地插值。
       否则"起播装载期"（is_playing_intent 已置 playing=true，但音频尚未开始）会乐观推进，
       随后后端回 0 → 进度条跳回（用户点名的"走过一块再回去才能正常播放"）。 */
    if (st.playing && !seeking && st.duration > 0 && st.posLive){
      st.position = Math.min(st.duration, st.position + dt);
    }
    updateFrameProgress();
    updateCardProgress();                          // R47：曲目卡底部渐变进度条每帧对齐
    try { beatTick(); } catch (_) {}               // 音频律动（真实频谱驱动封面，beat-on 才动）
    /* ⚠ R59：歌词当前行**每帧**推进 —— 旧实现只在 2s 轮询的 applyState 里
       seekLyric，行高亮会滞后整 2 秒（歌词"不跟唱"）。主循环已本地插值
       st.position，每帧驱动一次即可（内部二分/线性扫到 break，开销极小）。 */
    try { if (window.Poster) window.Poster.seekLyric(st.position); } catch (_) {}
    requestAnimationFrame(mainLoop);
  })(performance.now());
`;
    const __epi = `  for (const __n of ["_lastTick"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
