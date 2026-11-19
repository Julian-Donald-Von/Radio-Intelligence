/* newui/js/fine/ai-bar.js — 「AI口播栏常驻」自定义空间模块（源自 ui-init.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'ai-bar',
  source: 'ui-init.js',
  space: 'AI口播栏常驻',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  /* ═══ R59：AI 伴听开启 → 口播栏启动即常驻 ═══
     st.djOn 初值 true（与后端默认一致）。等待首条 broadcast 文案
     （首帧 loadBroadcast + 3s 轮询）到达自动显示；若后端已关伴听，
     下一次轮询 applyState 会 hideAIBar 收走。 */

  /* R64：个性化设置折叠委托（高级选项 .set-fold-head + 动态分组 h4）+ 扫码弹层关闭。
     委托挂在 document 上，动态重渲染无需重绑。 */
  try { if (typeof initSettingsFolds === 'function') initSettingsFolds(); } catch (_) {}
  /* R67-2：4 tab 切换（AI / TTS / 音源 / 通用），点切一次只看一组。tab 状态不持久化。 */
  try { if (typeof bindSettingsTabs === 'function') bindSettingsTabs(); } catch (_) {}

  /* R53 验证钩子（offscreen 黑盒断言用，最小只读面）：
     wsText()        = 模拟 WS dj_speech 推送（viaWs=true 记 lastDjAt）
     pollBroadcast() = 手动触发一次轮询回填（验证 outro 门控）
     lastDjAt()      = 读上次 WS 推送时间戳
     ⚠ R58-1 (#41) 增补：
     wsText 应用与 WS case 同款 st.djOn 门控（关闭时不得弹口播栏）；
     djSet(on) = 从后端权威 applyState 喂 dj_on（黑盒驱动开/关路径）；
     djGet()   = 读开关镜像（断言随动）。 */
  window.__r53 = {
    lastDjAt: function(){ return lastDjAt; },
    wsText:   function(t){ if (!st.djOn) return false; return setAIBarText(String(t), true); },
    pollBroadcast: function(){ return loadBroadcast(); },
    djSet:    function(on){ applyState({ dj_on: !!on }); return st.djOn; },
    djGet:    function(){ return st.djOn; },
    /* ★ R119-7 增补（offscreen 黑盒用）：
       newTrack() = 等价一次 track_changed（解锁 WS 钉住 + 亮加载圈）
       holdOn()   = 读「WS 文案钉住」状态（验证轮询是否被正确门控）
       thinkOn()  = 读圈圈是否点亮（验证"加载中"可见性） */
    newTrack: function(){ clearDjHold(); setAIWaiting(true); return true; },
    holdOn:   function(){ return djHold; },
    thinkOn:  function(){ var e = document.getElementById('ai-bar-think');
                          return !!(e && e.classList.contains('on')); }
  };

`;
    const __epi = `  for (const __n of []) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
