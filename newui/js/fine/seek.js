/* newui/js/fine/seek.js — 「进度seek/120段采样」自定义空间模块（源自 ui-dock.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'seek',
  source: 'ui-dock.js',
  space: '进度seek/120段采样',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  let seeking = false;
  /* ⚠⚠ 后端 position 一律**不直接覆盖** st.position（R46-5「进度条没和播放百分百链接」的真凶）
     主循环（rAF）已在本地逐帧插值推进，而后端值天然滞后：
       2s 轮询的 /api/playback/state 返回的是**上一拍**的位置、WS progress 也有往返延迟。
     原来 applyState() 无条件 \`st.position = d.position\` → 每 2 秒把进度条拽回旧值，
     表现为"拖动完又弹回去""进度条一跳一跳地回退""进度和声音对不上"。
     两条规则：
       ① 刚 seek 过（seekGuardUntil 守卫期内）或正在拖动 → 完全忽略后端；
       ② 其余时候只有偏差 > REMOTE_TOL_MS 才采信 —— 真·切歌/跳转必然远超阈值，
          正常播放的 1~2s 抖动则被平滑掉。 */
  const REMOTE_TOL_MS = 1200;
  let seekGuardUntil = 0;            // performance.now() 时间轴
  let _seekPostAt = 0;
  function adoptRemotePosition(ms, tol){
    if (typeof ms !== 'number') return;
    if (seeking || performance.now() < seekGuardUntil) return;
    /* ★ 2026-09-19：临近结尾（≤1.5s）**无条件采信**后端位置。
       正常播放走 1200ms 容差（用来平滑抖动），但收尾时这个容差会让进度条落后一小截，
       切歌瞬间表现为"歌播完了、条还剩一小条就跳走"。结尾窗口直接对齐后端即可。 */
    /* ★ 2026-09-19：结尾窗口 1500 → **4000ms**（收尾相位滞后可达 1~2s，
       1.5s 窗会漏采 ⇒ 环差最后一点。窗口放宽后更早采信后端真值。） */
    if (st.duration > 0 && ms >= st.duration - 4000){
      st.position = Math.max(0, Math.min(st.duration, ms));
      if (ms > 0) st.posLive = true;
      return;
    }
    if (Math.abs(ms - st.position) > (tol || REMOTE_TOL_MS)) st.position = ms;
    /* ★ 2026-09-18：后端报过 >0 位置 = 音频真的开始了 → 放开本地插值推进闸门。
       装载期后端恒报 0 → 闸门保持关 → 进度条停在 0 不动（不再"先走再跳回"）。 */
    if (ms > 0) st.posLive = true;
  }
  /* 统一的"跳到某个毫秒"入口：进度条点击/拖动 + 歌词点击走同一条路
     · doPost=false → 只更新视觉（拖动过程），不发请求
     · force=true   → 跳过 140ms 节流（按下 / 松手这一次必须发） */
  function seekToMs(ms, doPost, force){
    if (!(st.duration > 0)) return;
    st.position = Math.max(0, Math.min(st.duration, ms));
    seekGuardUntil = performance.now() + 900;   // 守卫：这段时间内后端旧值一律不采信
    _frameProgLen = -1;            // 强制进度条重绘一次（绕开 0.5px 节流）
    updateFrameProgress();
    if (doPost === false) return;
    const now = performance.now();
    if (!force && now - _seekPostAt < 140) return;   // 节流：mousemove 每帧都会来
    _seekPostAt = now;
    post('/seek', 'ms=' + Math.round(st.position));
  }
  function dockSeek(e, immediate){
    if (!frameLen || !st.duration) return;
    const r = dock.getBoundingClientRect();
    const pt = { x: e.clientX - r.left, y: e.clientY - r.top };
    const len = frameLen;
    let best = 0, bestD = 1e9;
    const steps = 120;
    for (let i = 0; i <= steps; i++){
      const L = len * i / steps;
      const p = framePath.getPointAtLength(L);
      const dx = p.x - pt.x, dy = p.y - pt.y;
      const d = dx * dx + dy * dy;
      if (d < bestD){ bestD = d; best = L; }
    }
    const frac = best / len;
    seekToMs(frac * st.duration, true, !!immediate);
  }

  function beginSeek(e){
    /* 视觉态先挂上：用户看到"正在操作"反馈。
       真正 seek 只在已加载时长时执行，否则撤回动作但视觉态保留 120ms 后撤。 */
    dock.classList.add('seeking');
    if (!st.duration){
      setTimeout(function(){ dock.classList.remove('seeking'); }, 120);
      return;
    }
    seeking = true;
    dockSeek(e, true);              // 按下即跳（force，跳过节流）
    e.preventDefault();
  }
  if (frameHit){
    frameHit.addEventListener('mousedown', beginSeek);
  } else {
    framePath.addEventListener('mousedown', beginSeek);
  }
  /* 拖动中：只做视觉 + 140ms 节流的真实 seek；松手再 force 提交一次最终位置。
     ⚠ 松手必须 force —— 否则最后一次落点可能正落在节流窗口里被丢掉，
        表现为"拖到 3:20 松手，实际跳到 3:12"（进度条与播放对不上）。 */
  window.addEventListener('mousemove', e => { if (seeking) dockSeek(e, false); });
  window.addEventListener('mouseup', () => {
    if (seeking){
      seeking = false;
      dock.classList.remove('seeking');
      seekToMs(st.position, true, true);
    }
  });

  /* ═══════════════════════════════════════════════════════════════
     Orbit 导航（原 ui/ai-radio-player.html L748-755 / L1838-1990）
     三按钮在 R1 圆弧上轮转；隐藏靠 #orbits 原位透明度（**不位移**），
     触发区：以 O 为圆心、R2 = R1+99 为半径的圆弧区。
     ═══════════════════════════════════════════════════════════════ */
`;
    const __epi = `  for (const __n of ["seeking", "REMOTE_TOL_MS", "seekGuardUntil", "_seekPostAt", "adoptRemotePosition", "seekToMs", "dockSeek", "beginSeek"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
