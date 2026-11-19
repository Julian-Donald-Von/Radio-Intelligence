/* newui/js/fine/settings-api.js — 「工具/后端通道」自定义空间模块（源自 ui-settings.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'settings-api',
  source: 'ui-settings.js',
  space: '工具/后端通道',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  /* ---- 小工具 ---- */
  function setEsc(s){
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function setPathGet(obj, path){
    return String(path || '').split('.').reduce(function(o, k){
      return (o == null) ? undefined : o[k];
    }, obj);
  }
  /* 折叠状态：localStorage 记忆（组=setgroup:<gid>；高级折叠=setfold:<fold>） */
  function restoreFoldState(scope){
    (scope || document).querySelectorAll('.set-fold[data-fold]').forEach(function(f){
      try { if (localStorage.getItem('setfold:' + f.dataset.fold) === '1') f.classList.add('open'); } catch (_) {}
    });
    (scope || document).querySelectorAll('.set-group[data-gid]').forEach(function(g){
      try {
        const v = localStorage.getItem('setgroup:' + g.dataset.gid);
        if (v === '1') g.classList.add('collapsed');
        else if (v === '0') g.classList.remove('collapsed');
      } catch (_) {}
    });
  }

  /* ---- 后端通道（fetch 版，替换老架构 QWebChannel mi.*） ---- */
  function setApiGet(){
    return fetch('/api/settings').then(function(r){ return r.json(); });
  }
  function setApiSet(path, value){
    // ★ 2026-11-19：敏感值(api_key/cookie)改走 JSON body，不进 URL/access log。
    return fetch('/api/settings/set', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: path, value: String(value) })
    }).then(function(r){ return r.json(); });
  }
  function setApiTest(kind){
    return fetch('/api/settings/test?kind=' + encodeURIComponent(kind), { method: 'POST' })
      .then(function(r){ return r.json(); });
  }
  function setStatus(action, msg){
    const el = document.querySelector('[data-status="' + action + '"]');
    if (el) el.textContent = msg;
  }
  /* 后端部分文案自带 ✅/❌/⚠（如 ai.model.test_connection），前端别再叠一个前缀 */
  function setMark(ok, msg){
    const m = String(msg == null ? '' : msg);
    if (/^[\\u2705\\u274C\\u26A0\\uD83D\\uDD0A]/.test(m)) return m;
    return (ok ? '✅ ' : '⚠ ') + (m || '无返回');
  }

`;
    const __epi = `  for (const __n of ["setEsc", "setPathGet", "restoreFoldState", "setApiGet", "setApiSet", "setApiTest", "setStatus", "setMark"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
