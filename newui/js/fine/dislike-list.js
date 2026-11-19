/* newui/js/fine/dislike-list.js — 「不喜欢列表」自定义空间模块（源自 ui-skinset.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'dislike-list',
  source: 'ui-skinset.js',
  space: '不喜欢列表',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  function renderDislikeList(){
    const list = $('dislike-list'); if (!list) return;
    fetch('/api/dislikes').then(r => r.json()).then(d => {
      const items = (d && Array.isArray(d.entries)) ? d.entries
                  : (d && Array.isArray(d.items))  ? d.items : [];
      const cnt = $('dislike-count'); if (cnt) cnt.textContent = items.length + ' 首';
      if (!items.length){
        list.innerHTML = '<div class="dl-empty">还没有不喜欢的歌曲 —— AI 会避开它们。</div>';
        return;
      }
      list.innerHTML = items.map(it => {
        /* R67-4：API 不再预兜底"未知标题" —— 现在由前端统一处理空值。 */
        const t = it.title || '未知歌曲';
        const a = it.artist || '未知歌手';
        return \`<div class="dl-row" data-key="\${esc(it.key || '')}">
           <span class="dl-name" title="\${esc(t)}">\${esc(t)}</span>
           <span class="dl-artist">\${esc(a)}</span>
           <button class="dl-rm" title="移除" data-key="\${esc(it.key || '')}">✕</button>
         </div>\`;
      }).join('');
      list.querySelectorAll('.dl-rm').forEach(btn => {
        btn.addEventListener('click', ev => {
          ev.stopPropagation();
          const key = btn.getAttribute('data-key'); if (!key) return;
          fetch('/api/dislike/remove?key=' + encodeURIComponent(key), { method:'POST' })
            .then(r => r.json()).catch(() => {})
            .then(() => { renderDislikeList(); toast('已移出「不喜欢的音乐」'); });
        });
      });
    }).catch(() => {
      const cnt = $('dislike-count'); if (cnt) cnt.textContent = '0 首';
      list.innerHTML = '<div class="dl-empty">后端未连接</div>';
    });
  }

  /* ---- R56 · 事件绑定（齿轮→设置卡 / 衣架→换肤卡 / 关闭 / 外点 / Esc / 角部 hover） ---- */
`;
    const __epi = `  for (const __n of ["renderDislikeList"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
