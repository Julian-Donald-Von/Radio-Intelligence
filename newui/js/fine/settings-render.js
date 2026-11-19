/* newui/js/fine/settings-render.js — 「设置控件渲染」自定义空间模块（源自 ui-settings.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'settings-render',
  source: 'ui-settings.js',
  space: '设置控件渲染',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `  /* ---- 渲染：单个控件 → HTML（老 UI L3994-4020） ---- */
  function setControl(it, cfg){
    const sens = cfg._sensitive || {};
    const val = it.path ? setPathGet(cfg, it.path) : undefined;
    if (it.type === 'toggle'){
      return '<label class="fx-toggle"><input type="checkbox" data-path="' + it.path + '"'
        + (val ? ' checked' : '') + '><span class="tk"></span><span class="ft-label">'
        + it.label + '</span></label>';
    }
    if (it.type === 'select'){
      return '<div class="fx-select-row"><span class="ft-label">' + it.label + '</span>'
        + '<select data-path="' + it.path + '">'
        + it.options.map(function(o){
            return '<option value="' + o[0] + '"'
              + (String(val) === String(o[0]) ? ' selected' : '') + '>' + o[1] + '</option>';
          }).join('')
        + '</select></div>';
    }
    if (it.type === 'range'){
      return '<div class="fx-range-row"><span class="ft-label">' + it.label
        + ' <b>' + (val == null ? '' : val) + '</b></span>'
        + '<input type="range" data-path="' + it.path + '" min="' + it.min + '" max="' + it.max
        + '" step="' + (it.step || 1) + '" value="' + (val == null ? '' : val) + '"></div>';
    }
    if (it.type === 'password'){
      const isSet = !!sens[it.path];
      return '<div class="fx-pass-row"><span class="ft-label">' + it.label + '</span>'
        + '<input type="password" data-path="' + it.path + '" placeholder="'
        + (isSet ? '已设置 · 留空保持不变' : '未设置') + '" autocomplete="new-password">'
        + (isSet ? '<button class="fx-clear" data-clear="' + it.path + '" title="清除">✕</button>' : '')
        + '</div>';
    }
    if (it.type === 'textarea'){
      return '<div class="fx-area-row"><span class="ft-label">' + it.label + '</span>'
        + '<textarea data-path="' + it.path + '" rows="' + (it.rows || 3) + '" placeholder="'
        + (it.ph || '') + '" spellcheck="false">'
        + setEsc(val == null ? '' : val) + '</textarea></div>';
    }
    if (it.type === 'button'){
      return '<div class="fx-btn-row"><button class="set-mini-btn fx-btn" data-action="'
        + it.action + '">' + it.label + '</button><span class="fx-btn-status" data-status="'
        + it.action + '">' + (it.hint || '') + '</span></div>';
    }
    if (it.type === 'subhead'){
      return '<div class="fx-subhead">' + it.label + '</div>';
    }
    return '<div class="fx-text-row"><span class="ft-label">' + it.label + '</span>'
      + '<input type="text" data-path="' + it.path + '" value="'
      + setEsc(val == null ? '' : val) + '" placeholder="' + (it.ph || '')
      + '" autocomplete="off" spellcheck="false"></div>';
  }

  /* ---- 主渲染（老 UI L3990-4088，R67-2 改路由到 4 tab） ----
     R67-2：设置页 4 tab 切换（AI / TTS / 音源 / 通用），每个 gid 渲染到对应
     .settings-dynamic[data-gid=...]（取代旧的单 #settings-dynamic）。
     通用 tab 内还有静态「本地曲库」「不喜欢」section，由 HTML 写死，
     动态 general + match 拼到它们后面。 */
  function renderSettingsGroups(){
    const hosts = document.querySelectorAll('.settings-dynamic[data-gid]');
    if (!hosts.length) return;
    const buildGroup = function(grp, cfg){
      const prim = grp.items.filter(function(it){ return !it.adv; });
      const adv  = grp.items.filter(function(it){ return !!it.adv; });
      let rows = prim.map(function(it){ return setControl(it, cfg); }).join('');
      if (adv.length){
        /* 高级项收进折叠区（默认隐藏，点开记忆状态） */
        rows += '<div class="set-fold" data-fold="adv-' + grp.gid + '">'
          + '<div class="set-fold-head">高级选项（' + adv.length + ' 项）</div>'
          + '<div class="set-fold-body">'
          + adv.map(function(it){ return setControl(it, cfg); }).join('')
          + '</div></div>';
      }
      return '<section class="set-group' + (grp.collapsed ? ' collapsed' : '')
        + '" data-gid="' + grp.gid + '"><h4>' + grp.group + '</h4>' + rows + '</section>';
    };
    const wireHost = function(host){
      /* 绑定保存：统一 change 监听（checkbox/range/select/text/password 同一逻辑） */
      host.querySelectorAll('[data-path]').forEach(function(el){
        el.addEventListener('change', function(){
          const v = (el.type === 'checkbox') ? (el.checked ? '1' : '0') : el.value;
          if (el.type === 'password' && v === '') return;
          setApiSet(el.dataset.path, String(v)).then(function(d){
            if (d && d.ok) toast('设置已保存');
            else toast('保存失败：' + ((d && d.error) || '未知错误'));
          }).catch(function(){ toast('保存失败：后端未连接'); });
        });
        if (el.type === 'range'){
          el.addEventListener('input', function(){
            const b = el.parentElement && el.parentElement.querySelector('.ft-label b');
            if (b) b.textContent = el.value;
          });
        }
      });
      host.querySelectorAll('[data-clear]').forEach(function(btn){
        btn.addEventListener('click', function(e){
          e.stopPropagation();
          setApiSet(btn.dataset.clear, '').then(function(){
            toast('已清除');
            setTimeout(renderSettingsGroups, 400);
          });
        });
      });
      host.querySelectorAll('[data-action]').forEach(function(btn){
        btn.addEventListener('click', function(){
          const act = btn.dataset.action;
          if (act === 'qr-netease'){ startQrLogin('netease'); }
          else if (act === 'qr-qq'){ startQrLogin('qq'); }
          else if (act === 'test-ai'){
            setStatus(act, '测试中…');
            setApiTest('ai').then(function(d){
              setStatus(act, setMark(d && d.passed, d && d.msg));
            }).catch(function(){ setStatus(act, '⚠ 后端未连接'); });
          }
          else if (act === 'test-tts'){
            setStatus(act, '检测中…');
            setApiTest('tts').then(function(d){
              setStatus(act, setMark(d && d.ok, d && d.msg));
            }).catch(function(){ setStatus(act, '⚠ 后端未连接'); });
          }
          else if (act === 'test-voicevox'){
            setStatus(act, '连接/拉起中…');
            setApiTest('voicevox').then(function(d){
              setStatus(act, setMark(d && d.passed, d && d.msg));
            }).catch(function(){ setStatus(act, '⚠ 后端未连接'); });
          }
          else if (act === 'test-tts-sample'){
            setStatus(act, '合成中…');
            setApiTest('tts-sample').then(function(d){
              setStatus(act, setMark(d && d.ok, d && d.msg));
            }).catch(function(){ setStatus(act, '⚠ 后端未连接'); });
          }
        });
      });
      restoreFoldState(host);
    };
    setApiGet().then(function(d){
      if (!d || !d.ok){
        hosts.forEach(function(h){ h.innerHTML = '<div class="set-hint">连接后端后显示</div>'; });
        return;
      }
      const cfg = d.values || {};
      cfg._sensitive = d.sensitive || {};
      /* 按 gid 路由到对应 host；通用 tab 内有 2 个 host（general + match）。 */
      hosts.forEach(function(host){
        const gid = host.dataset.gid;
        const grp = SETTING_SCHEMA.find(function(g){ return g.gid === gid; });
        if (!grp){ host.innerHTML = ''; return; }
        host.innerHTML = buildGroup(grp, cfg);
        wireHost(host);
      });
    }).catch(function(){
      hosts.forEach(function(h){ h.innerHTML = '<div class="set-hint">需连接后端后显示</div>'; });
    });
  }

`;
    const __epi = `  for (const __n of ["setControl", "renderSettingsGroups"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
