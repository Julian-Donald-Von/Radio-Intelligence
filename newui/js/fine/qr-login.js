/* newui/js/fine/qr-login.js — 「扫码登录」自定义空间模块（源自 ui-settings.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'qr-login',
  source: 'ui-settings.js',
  space: '扫码登录',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  /* ═══ 扫码登录（音源设置 · 老 UI L4098-4150） ═══ */
  function startQrLogin(source){
    const name = source === 'netease' ? '网易云' : 'QQ音乐';
    setStatus('qr-' + source, '正在获取二维码…');
    fetch('/api/settings/qr/start?source=' + encodeURIComponent(source), { method: 'POST' })
      .then(function(r){ return r.json(); })
      .then(function(r){
        if (!r || !r.ok){
          setStatus('qr-' + source, '⚠ ' + ((r && r.err) || '获取失败'));
          return;
        }
        openQrModal(name, r);
        pollQrLoop(source, r.key);
      })
      .catch(function(){ setStatus('qr-' + source, '⚠ 后端未连接'); });
  }
  function openQrModal(name, r){
    const modal = $('qr-modal'), img = $('qr-img'), status = $('qr-status');
    if (!modal || !img) return;
    img.src = r.img || '';
    if (status) status.textContent = '请使用手机扫描二维码并在手机上确认登录';
    const t = $('qr-title'); if (t) t.textContent = name + ' 扫码登录';
    modal.classList.add('open');
  }
  function pollQrLoop(source, key){
    const timer = setInterval(function(){
      const modal = $('qr-modal');
      if (!modal || !modal.classList.contains('open')){ clearInterval(timer); return; }
      fetch('/api/settings/qr/poll?source=' + encodeURIComponent(source)
            + '&key=' + encodeURIComponent(key))
        .then(function(r){ return r.json(); })
        .then(function(r){
          if (!r) return;
          const status = $('qr-status');
          if (r.status === 'ok'){
            clearInterval(timer);
            if (status) status.textContent = '✅ 登录成功，已启用音源并保存登录态';
            setStatus('qr-' + source, '✅ 登录成功');
            setTimeout(function(){
              const m = $('qr-modal'); if (m) m.classList.remove('open');
              renderSettingsGroups();

            }, 1200);
          } else if (r.status === 'expired'){
            clearInterval(timer);
            if (status) status.textContent = '⚠ 二维码已过期，请重新获取';
            setStatus('qr-' + source, '⚠ 二维码已过期');
          } else if (r.status === 'scanned'){
            if (status) status.textContent = '已扫码，请在手机上确认';
          } else if (r.status === 'error'){
            clearInterval(timer);
            if (status) status.textContent = '⚠ ' + (r.msg || '轮询失败');
          } else {
            if (status) status.textContent = '等待扫码…';
          }
        }).catch(function(){});
    }, 1800);
  }
  function closeQrModal(){
    const m = $('qr-modal'); if (m) m.classList.remove('open');
  }

`;
    const __epi = `  for (const __n of ["startQrLogin", "openQrModal", "pollQrLoop", "closeQrModal"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
