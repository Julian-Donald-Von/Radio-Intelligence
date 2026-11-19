/* newui/js/fine/state-core.js — 「状态/模式/post/工具」自定义空间模块（源自 ui-core.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'state-core',
  source: 'ui-core.js',
  space: '状态/模式/post/工具',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  /* [头部公共段并入] */
  const $ = id => document.getElementById(id);
  const API = '/api/playback';

  const st = { ready:false, playing:false, position:0, duration:0,
               /* ★ 2026-09-18：后端是否已上报过 >0 的真实播放位置(=音频真的开始了)。
                  主循环只在 posLive 时本地插值推进 → 装载期不再乐观推进/跳回。 */
               posLive:false,
               /* ★ 2026-09-19：播放模式门控（后端为权威）。漫游/推荐下 modeLocked=true，
                  按钮置灰；高亮用 modeEff（"实际生效"的模式），避免显示随机却按顺序放。 */
               modeLocked:false, modeEff:'order',
               title:'', artist:'', cover:'', volume:0.8, muted:false,
               mode:'order', fav:false, disliked:false, current:null,
               /* R58-1 (#41)：开关镜像（后端为权威）。默认与后端 radio 一致
                  （dj_on/roam_on 初值 True）；loadBroadcast 的口播栏门控读它。 */
               djOn:true, roamOn:true };

  /* 播放模式：顺序 → 单曲循环 → 随机（与原 UI 三态一致） */
  const MODES = [
    { key:'order',    name:'顺序',     ic:'<path d="M4 6h16M4 12h16M4 18h16"/>' },
    { key:'loop_one', name:'单曲循环', ic:'<path d="M4 6h13M4 12h13M4 18h13"/><path d="M19 4l3 3-3 3"/>' },
    { key:'random',   name:'随机',     ic:'<path d="M16 3h5v5"/><path d="M4 20L21 3"/><path d="M21 16v5h-5"/><path d="M15 15l6 6"/><path d="M4 4l5 5"/>' },
  ];
  let modeIdx = 0;

  function post(path, qs){
    return fetch(API + path + (qs ? ('?' + qs) : ''), {method:'POST'})
      .then(r => r.json()).catch(e => ({ok:false, error:String(e)}));
  }
  function fmt(ms){
    if (!ms || ms < 0) ms = 0;
    const s = Math.floor(ms/1000);
    return Math.floor(s/60) + ':' + String(s%60).padStart(2,'0');
  }

`;
    const __epi = `  for (const __n of ["$", "API", "st", "MODES", "modeIdx", "post", "fmt"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
