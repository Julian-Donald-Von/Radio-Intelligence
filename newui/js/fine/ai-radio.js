/* newui/js/fine/ai-radio.js — 「AI伴听/电台开关」自定义空间模块（源自 ui-dock.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'ai-radio',
  source: 'ui-dock.js',
  space: 'AI伴听/电台开关',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  /* AI 伴听（口播）/ 电台模式：
     ⚠ 点亮态以**后端为权威** —— 点击后等后端返回再改 UI，
     失败则回滚并提示（避免"点了没反应"或"前端亮了后端没开"）。
     ⚠ R58-1 (#41)：AI 伴听（#d-ai）点击响应后**立即**开合口播栏，
     不等 2s 轮询 —— 关闭时后端仍"始终生成"文案、current_broadcast 残留，
     若不主动 hide，口播栏会赖着不走（用户要"随开关常开常闭"）。 */
  function bindToggle(id, url){
    const btn = $(id);
    btn.addEventListener('click', () => {
      const want = !btn.classList.contains('on');
      fetch(url + '?on=' + (want ? 1 : 0), { method:'POST' })
        .then(r => r.json())
        .then(d => {
          if (d && d.ok){
            btn.classList.toggle('on', !!d.on);
            /* R58-1 (#41)：本地开关镜像立即生效（口播栏随动）。
               #d-ai ↔ st.djOn（口播）；#d-radio ↔ st.roamOn（电台续推）。
               后端返回的权威值（非本地 want），失败路径不镜像。 */
            if (id === 'd-ai'){
              st.djOn = !!d.on;
              /* ⚠ R58-1 (#41)：本地开关镜像立即生效（口播栏随动）。
                 #d-ai ↔ st.djOn（口播）；#d-radio ↔ st.roamOn（电台续推）。
                 后端返回的权威值（非本地 want），失败路径不镜像。
                 ⚠ R59：开 = 口播栏**立即常驻**（showAIBar 统一入口，
                 不再只摘 hide 等鼠标靠近/新文案才出现）；关 = 立即淡出。 */
              if (st.djOn){
                showAIBar();
                /* 立即拉一次当前口播文案填充（不等 3s 轮询；WS gate 内则跳过） */
                try { loadBroadcast(); } catch (_) {}
              }
              else hideAIBar();
            } else {
              st.roamOn = !!d.on;
            }
          } else {
            toast((d && d.error) ? ('失败：' + d.error) : '操作失败');
          }
        })
        .catch(e => { toast('请求失败：' + e.message); });
    });
  }
  bindToggle('d-ai',    '/api/dj/toggle');
  bindToggle('d-radio', '/api/radio/toggle');

  $('d-mode').onclick = () => {
    /* ★ 2026-09-19：漫游/推荐下**不许切模式**（后端也会拒），且**不静默**——给一次明确提示。 */
    if (st.modeLocked){
      try { toast('漫游 / 推荐下不可切换播放模式（只按顺序播放）'); } catch (_) {}
      return;
    }
    modeIdx = (modeIdx + 1) % MODES.length;
    /* ★★ 2026-09-19 修：**路径被拼重复了**。—— \`post(path)\` 内部就是 \`fetch(API + path)\`，
       而 \`API = '/api/playback'\`；这里传过完整路径 '/playback/mode' ⇒ 实际请求变成
       \`/api/playback/playback/mode\` ⇒ **405 Method Not Allowed** ⇒ set_play_mode 从未被调用
       （所以"模式按钮一直不生效"）。
       对照：同文件其它调用都传相对路径（'/next' '/volume' '/seek'）⇒ 那只传 '/mode' 即可。
       路由确在 \`webapi/server.py\` L467 \`@app.post("/api/playback/mode")\`。 */
    post('/mode', 'mode=' + MODES[modeIdx].key);
    render();
  };

  // 音量条点击/拖动
  const volEl = $('vol');
`;
    const __epi = `  for (const __n of ["bindToggle", "volEl"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
