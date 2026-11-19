/* newui/js/fine/fav-dislike.js — 「喜欢/不喜欢」自定义空间模块（源自 ui-dock.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'fav-dislike',
  source: 'ui-dock.js',
  space: '喜欢/不喜欢',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  /* 喜欢 ♥（R49 恢复，老 UI L2985 同语义）：收藏到曲库（我喜欢的音乐）。
     ⚠ 点亮瞬间放礼花（spawnBurst，老 UI L2993 "点亮红心时礼花动效"）；
       乐观翻转 + 后端确认回滚。走 /api/playback/favorite（当前曲）。 */
  $('d-fav').onclick = () => {
    const btn = $('d-fav');
    const was = btn.classList.contains('on');
    const want = !was;
    btn.classList.toggle('on', want);
    if (want){ try { spawnBurst(btn); } catch (_) {} }
    const favSnap = st.current;      // ⚠ R50：点击时的当前曲快照（竞态守卫用）
    post('/favorite', 'on=' + (want ? 1 : 0)).then(d => {
      if (d && d.ok){
        /* ⚠ 响应可能晚于自动切歌：只有还是同一首（db id 相同）才应用，
           否则交给 2s 轮询以后端为准（红心同样不许"带到下一首"）。 */
        if (favSnap != null && (st.current || {}).id === favSnap.id){
          btn.classList.toggle('on', !!d.favorited);
          st.fav = !!d.favorited;
        }
      } else {
        btn.classList.toggle('on', was);
        toast((d && d.error) ? ('失败：' + d.error) : '操作失败');
      }
    });
  };

  /* 不喜欢：红心+斜杠，点亮后 AI 避雷 + 从待播/推荐货架剔除。
     key 规则沿用原 UI：'i:<库id>'（本地）或 'e:<source>:<ext_id>'（在线）。 */
  $('d-dislike').onclick = () => {
    const btn = $('d-dislike');
    const on = !btn.classList.contains('on');
    const cur = st.current || {};
    let key = '';
    if (cur.id != null && cur.source && String(cur.source) !== 'local'){
      key = 'e:' + cur.source + ':' + (cur.ext_id != null ? cur.ext_id : '');
    } else if (cur.id != null){
      key = 'i:' + cur.id;
    }
    if (!key){ toast('当前无曲目'); return; }
    /* ⚠ R62 #23：dock 标记的是**当前曲**（st.title/st.artist 必有）—— 在线曲
       e:… 落 dislike 库要带上歌名/歌手，否则库显示「未知歌曲」。 */
    const tq = 'key=' + encodeURIComponent(key)
             + '&title=' + encodeURIComponent(st.title || '')
             + '&artist=' + encodeURIComponent(st.artist || '');
    fetch((on ? '/api/dislike/add' : '/api/dislike/remove') + '?' + tq,
          { method:'POST' })
      .then(r => r.json())
      .then(d => {
        if (d && d.ok){
          /* ⚠ R50：响应可能晚于自动切歌（track_changed 已把点亮清掉）。
             只有当前曲**还是点击时的那首**（key 相同）才应用点亮态，
             否则丢弃、交给 2s 轮询以后端为准 —— 否则点亮会"带到下一首"。 */
          const curNow = st.current || {};
          const curKey = (curNow.id != null && curNow.source && String(curNow.source) !== 'local')
            ? 'e:' + curNow.source + ':' + (curNow.ext_id != null ? curNow.ext_id : '')
            : (curNow.id != null ? 'i:' + curNow.id : '');
          if (curKey === key){
            btn.classList.toggle('on', on);
            st.disliked = on;   // R49-27：与后端状态同步，供切歌后正确重置
          }
        }
        else toast((d && d.error) ? ('失败：' + d.error) : '操作失败');
      })
      .catch(e => { toast('请求失败：' + e.message); });
  };

`;
    const __epi = `  for (const __n of []) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
