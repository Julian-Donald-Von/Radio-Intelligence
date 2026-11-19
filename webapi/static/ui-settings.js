/* ══════════════════════════════════════════════════════════
   ui-settings.js — Radio Intelligence 前端模块 · 个性化设置（LAB / 语音 TTS / 音源 / 通用 / 匹配）
   逐行移植自老 web UI `ui/ai-radio-player.html` L3873-4150（SETTING_SCHEMA +
   renderSettingsGroups + 扫码登录 startQrLogin/pollQrLoop），
   数据通道由 QWebChannel（mi.getSettings/setSetting）改为 fetch 新架构
   `/api/settings` 系列端点（webapi/server.py R64）。
   与所有 ui-*.js 共享顶层作用域 → 加载顺序即执行顺序，勿打乱。
   ══════════════════════════════════════════════════════════ */

  /* ★ 2026-11-18：TTS 统一到 Qwen3-TTS。9 个独立可选音色，每个都覆盖中/英/日
     全部语种（不按语言拆分）；选中后全程同一副嗓子。
     后 7 个为 11-18 试听后用户保留：2 个真人录音 + 5 个 VOICEVOX normal。 */
  const QWEN3_TTS_VOICES = [
    ['alba-mackenna','Alba Mackenna'],
    ['ono-anna','Ono Anna'],
    ['serena','Serena'],
    ['uncle-fu','Uncle Fu'],
    ['四国めたん','四国めたん'],
    ['青山龍星','青山龍星'],
    ['もち子さん','もち子さん'],
    ['春日部つむぎ','春日部つむぎ'],
    ['中部つるぎ','中部つるぎ'],
  ];
  const FAMILIARITY_LABELS = ['更熟悉','略熟悉','略陌生','更陌生'];

  /* ---- 设置大纲（老 UI L3888-3968） ----
     adv:1 的项收进组内「高级选项」折叠区（默认隐藏，点开才显示）；
     collapsed:1 的组默认整组折叠。折叠状态记 localStorage，下次打开保持。 */
  const SETTING_SCHEMA = [
    { group: 'LAB', gid: 'ai', items: [
      { path: 'ai.backend', type: 'select', label: 'AI 后端', options: [['llamacpp','本机 llama.cpp（默认·localhost:8080）'],['api','远程 API（OpenAI 兼容）']] },
      { path: 'ai.api_base', type: 'password', label: 'API 地址', ph: 'https://…（多数需以 /v1 结尾）' },
      { path: 'ai.api_key', type: 'password', label: 'API Key' },
      { path: 'ai.model', type: 'text', label: '模型名', ph: 'gpt-4o-mini / deepseek-chat / moonshot-v1-8k' },
      { path: 'ai.ollama_host', type: 'text', label: 'Ollama 地址', ph: 'http://127.0.0.1:11434' },
      { path: 'ai.ollama_deep', type: 'text', label: '口播模型（深度）', ph: 'qwen3.5:4b（默认，实测最稳）/ granite4.2:3b（可思考，但见下）' },
      /* ★ R119-3k 实测备注（2026-09-19，同一首"椎名林檎"用例各 5 次）：
         · qwen3.5:4b ：均值 51 汉字（合约内）、**多语言 <ja> 标签 3/3 次都用对**、0.8~2.6s。
         · granite4.2:3b：均值 69 汉字（超合约）、**<ja> 标签 0/3 次**（日文歌名会被 MeloTTS
           用中文模型硬读成乱码）、中文偏生硬 ⇒ **暂不推荐做口播主力**。
         · granite 的思考**可用**（Ollama 不拆字段，推理内联在 content 里、以 </think> 收尾，
           已在后端自动切分），开思考后单条约 20~30s。 */
      { path: 'ai.thinking', type: 'toggle', label: '思考模式（慢但更高质量）' },
      { path: 'ai.web_search', type: 'toggle', label: '联网搜索（DJ 口播引用实时信息）' },
      /* R66 精简提示：只保留「肉眼看不见的信息」（登录态是否持久化、是否会自动拉起引擎）；
         单纯复述按钮作用的 hint 全删（测试 AI 连接 / 测试朗读 / 测试 TTS 连接 都自明）。
         ⚠ .fx-btn-status 同时是动作状态位（data-status），hint 为空只是初始留白，不影响回显。 */
      { type: 'button', action: 'test-ai', label: '测试 AI 连接' },
      { path: 'ai.ollama_fast', type: 'text', label: '快速模型（转场）', ph: 'qwen3.5:4b', adv: 1 },
      { path: 'ai.ollama_fast_enabled', type: 'toggle', label: '启用快速模型（降低延迟）', adv: 1 },
      { path: 'ai.temperature', type: 'range', label: '温度（创造性）', min: 0, max: 2, step: 0.1, adv: 1 },
      { path: 'ai.skip_temperature', type: 'toggle', label: '不传温度（兼容 o1/o3）', adv: 1 },
      { path: 'ai.web_search_interval', type: 'select', label: '联网搜索频率', options: [[0,'0 · 仅开场第一首'],[1,'1 · 每首'],[3,'3 · 每3首'],[5,'5 · 每5首']], adv: 1 },
      { path: 'ai.context_window', type: 'select', label: '上下文窗口', options: [[0,'0 · 纯实时'],[3,'3 · 轻量连贯'],[6,'6 · 更强连贯']], adv: 1 },
      { path: 'prompts.broadcast_output', type: 'textarea', label: '口播风格提示词', rows: 3, ph: '补充风格要求；留空用内置人设（无需粘贴示例）', adv: 1 },
      { path: 'prompts.taste_output', type: 'textarea', label: '口味分析提示词', rows: 2, ph: '额外强调的分析侧重', adv: 1 },
      { path: 'prompts.transition_output', type: 'textarea', label: '转场提示词', rows: 2, ph: '创作原则与风格参考', adv: 1 },
      { path: 'prompts.reco_explain', type: 'textarea', label: '推荐理由提示词', rows: 2, ph: '推荐解说系统指令', adv: 1 },
    ]},
    { group: '语音 TTS', gid: 'tts', items: [
      { path: 'tts.qwen3tts_voice', type: 'select', label: '音色', options: QWEN3_TTS_VOICES,
        hint: '选一个音色，中文/英文/日文全程同一个嗓子；切换即时生效。' },
      { type: 'button', action: 'test-tts-sample', label: '测试朗读（合成并试听）' },
      { path: 'tts.qwen3tts_postprocess', type: 'toggle', label: '播报后处理（ffmpeg 均衡/压缩/响度）' },
      { type: 'subhead', label: '后处理参数（开启后处理时生效）', adv: 1 },
      { path: 'tts.melotts_pp_hs1', type: 'range', label: '低频增益 120Hz（dB）', min: -12, max: 12, step: 0.5, adv: 1 },
      { path: 'tts.melotts_pp_hs2', type: 'range', label: '中频增益 250Hz（dB）', min: -12, max: 12, step: 0.5, adv: 1 },
      { path: 'tts.melotts_pp_hs3', type: 'range', label: '高频微亮 3000Hz（dB）', min: -12, max: 12, step: 0.5, adv: 1 },
      { path: 'tts.melotts_pp_comp_th', type: 'range', label: '压缩器阈值（dB）', min: -40, max: 0, step: 1, adv: 1 },
      { path: 'tts.melotts_pp_comp_ratio', type: 'range', label: '压缩比', min: 1, max: 10, step: 0.5, adv: 1 },
      { path: 'tts.melotts_pp_echo', type: 'range', label: '回声湿声比例', min: 0, max: 1, step: 0.05, adv: 1 },
      { path: 'tts.melotts_pp_loud_i', type: 'range', label: '目标响度 I（LUFS）', min: -24, max: -10, step: 1, adv: 1 },
    ]},
    { group: '音源', gid: 'sources', items: [
    /* 音源插件设置项由 plugin-settings.js 按已安装插件动态注入 */
      { type: 'subhead', label: 'Spotify', adv: 1 },
      { path: 'sources.spotify.enabled', type: 'toggle', label: '启用 Spotify', adv: 1 },
      { path: 'sources.spotify.api_base', type: 'text', label: '服务地址（可选）', ph: '兼容代理地址', adv: 1 },
      { path: 'sources.spotify.client_id', type: 'text', label: 'Client ID（可选）', adv: 1 },
      { path: 'sources.spotify.client_secret', type: 'password', label: 'Client Secret（可选）', adv: 1 },
      { path: 'sources.spotify.cookie', type: 'password', label: 'Cookie', adv: 1 },
      { path: 'sources.spotify.default_playlist_id', type: 'text', label: '默认歌单ID', adv: 1 },
      { type: 'subhead', label: '本地' },
      { path: 'sources.local.enabled', type: 'toggle', label: '扫描本地目录' },
      { path: 'sources.local.roots', type: 'textarea', label: '本地目录', rows: 3, ph: '每行一个目录，如：\nD:\\CloudMusic\nD:\\Music' },
    ]},
    { group: '通用', gid: 'general', items: [
      /* R67-2：rec_pref 从 SETTING_SCHEMA 删除 —— 用户原话"主界面有相对应的滑块了，
         通用设置里面的推荐偏好功能可以去了"。user_config.rec_pref 字段保留，
         由主界面 ui-column.js 推荐风格滑块统一读写。 */
      { path: 'geo.city', type: 'text', label: '所在城市（手动覆盖）', ph: '长春（留空则自动定位）' },
      { path: 'roam_strategy', type: 'select', label: '漫游策略', options: [['continuous','连续相似'],['leap','风格跳跃']] },
      { path: 'auto_play', type: 'toggle', label: '启动自动播放' },
      { path: 'search.mode', type: 'select', label: '检索方式', options: [['browser','浏览器内核（本地服务）'],['online','联网 DuckDuckGo'],['offline','本地向量库']] },
      { path: 'search.browser_url', type: 'text', label: '搜索服务地址', ph: 'http://127.0.0.1:8787', adv: 1 },
      { path: 'search.browser_engine', type: 'select', label: '搜索服务引擎', options: [['bing','必应国内版'],['360','360 搜索']], adv: 1 },
      { path: 'search.max_results', type: 'range', label: '搜索结果条数', min: 1, max: 10, step: 1, adv: 1 },
    ]},
    { group: '曲库与匹配', gid: 'match', collapsed: 1, items: [
      { path: 'match.fuzzy_enabled', type: 'toggle', label: '推荐模糊匹配（rapidfuzz）' },
      { path: 'match.fuzzy_threshold', type: 'range', label: '匹配阈值', min: 50, max: 100, step: 1, adv: 1 },
    ]},
  ];

  /* ---- 小工具 ---- */
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
    // 敏感值走 JSON body，不进 URL/access log
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
    if (/^[\u2705\u274C\u26A0\uD83D\uDD0A]/.test(m)) return m;
    return (ok ? '✅ ' : '⚠ ') + (m || '无返回');
  }

  /* ---- 渲染：单个控件 → HTML（老 UI L3994-4020） ---- */
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
          if (act === 'test-ai'){
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

  /* ---- 4 tab 切换（点 .stab 切 .stab-panel 显隐） ----
     ⚠ tab 状态不写 localStorage：设置卡每次打开默认落「AI」tab，符合"先做设置"心智。 */
  function bindSettingsTabs(){
    const tabs = document.querySelectorAll('#settings-tabs .stab');
    const panels = document.querySelectorAll('#settings-panel .stab-panel');
    if (!tabs.length || !panels.length) return;
    tabs.forEach(function(btn){
      btn.addEventListener('click', function(){
        const k = btn.dataset.tab;
        tabs.forEach(function(b){ b.classList.toggle('active', b === btn); });
        panels.forEach(function(p){ p.classList.toggle('active', p.dataset.tab === k); });
      });
    });
  }

  /* ═══ 扫码登录（音源设置 · 老 UI L4098-4150） ═══ */
  function startQrLogin(source){
    const name = '在线音源';
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

  /* ═══ 折叠委托（高级选项 .set-fold-head + 动态分组 h4）═══
     ⚠ 只接管 **动态区**（#settings-dynamic 内、非 data-foldable）的分组，
     避免与 ui-skinset.js 里 data-foldable 静态分组的委托重复触发。 */
  function initSettingsFolds(){
    if (initSettingsFolds._done) return;
    initSettingsFolds._done = true;
    try { buildFxWater(); } catch (_) {}   // 2026-09-19：高级·视觉效果（水面调参）
    document.addEventListener('click', function(e){
      const head = e.target && e.target.closest ? e.target.closest('.set-fold-head') : null;
      if (head){
        const fold = head.parentElement;
        if (!fold) return;
        fold.classList.toggle('open');
        if (fold.dataset.fold){
          try {
            localStorage.setItem('setfold:' + fold.dataset.fold,
                                 fold.classList.contains('open') ? '1' : '0');
          } catch (_) {}
        }
        return;
      }
      const h4 = e.target && e.target.closest
        ? e.target.closest('.set-group[data-gid]:not([data-foldable]) > h4') : null;
      if (h4){
        const g = h4.parentElement;
        if (!g) return;
        g.classList.toggle('collapsed');
        if (g.dataset.gid){
          try {
            localStorage.setItem('setgroup:' + g.dataset.gid,
                                 g.classList.contains('collapsed') ? '1' : '0');
          } catch (_) {}
        }
      }
    });
    const qc = $('qr-close');
    if (qc) qc.addEventListener('click', closeQrModal);
    const qm = $('qr-modal');
    if (qm) qm.addEventListener('click', function(e){ if (e.target === qm) closeQrModal(); });
  }

  /* ═══ 2026-09-19：高级设置 · 视觉效果（水面调参，绑定 __water.tune）═══
     设计：schema 驱动 —— **接入新的效果层只需往数组里加一项**，不必再动 HTML/事件。
     每一项的语义（与 ui-water.js 内 TUNE 注释一致）：
       bright  整体亮度倍率（乘在最终水色上，不改透明度）
       interf  干涉等值线强度（去掉直流后的波幅等高线；0=关）
       interfN 等值线密度（每单位 ampN 的线数，越大越密）
       foam    坡度亮线增益（旧「波峰窄亮线」，保留作一般波纹轮廓）
       gSat    高波数额外阻尼（越小短波活得越久 ⇒ 波纹越细 ⇒ 干涉越可辨）
       rain    低密度欢快雨（真实涟漪；0=关）
       drawDpr DRAW 分辨率上限（性能；越小越省 GPU，给音频留余量） */
  const FX_WATER_SCHEMA = [
    { k: 'bright',  label: '整体亮度',        min: 0.30, max: 1.00, step: 0.01, dp: 2 },
    { k: 'refractMul', label: '横向扰动（折射）★', min: 0.5, max: 10, step: 0.1, dp: 1 },
    { k: 'foam',    label: '坡度亮线增益',    min: 0,    max: 2,    step: 0.05, dp: 2 },
    { k: 'softAmp', label: '源衰减（0=关·默认）', min: 0, max: 5, step: 0.1, dp: 1 },
    { k: 'advect',  label: '微涟漪平流（0=关·会平移）', min: 0, max: 1.0, step: 0.02, dp: 2 },
    { k: 'fineDens', label: '细波纹随激动变密',    min: 0, max: 2, step: 0.05, dp: 2 },
    { k: 'freezeDamp', label: '凝结阻尼（0=关）',  min: 0, max: 3, step: 0.05, dp: 2 },
    { k: 'subGrid', label: '亚网格耗散（消像素块）', min: 0, max: 4, step: 0.05, dp: 2 },
    { k: 'purgeCalm', label: '换曲平静（0=不压）', min: 0, max: 1, step: 0.05, dp: 2 },
    { k: 'freeze',  label: '凝结强度（安静时冻住）', min: 0, max: 1, step: 0.05, dp: 2 },
    { k: 'autoTier', label: '自动调档（0=关·固定Q3）', min: 0, max: 1, step: 1, dp: 0 },
    { k: 'debugAmp', label: '〔调试〕幅度 X 光', min: 0, max: 1, step: 1, dp: 0 },
    { k: 'ug',      label: '水平动量系数',        min: 0, max: 3, step: 0.05, dp: 2 },
    { k: 'interf',  label: '〔实验〕等高线·强度', min: 0, max: 0.25, step: 0.005, dp: 3 },
    { k: 'interfN', label: '〔实验〕等高线·密度', min: 0.5, max: 6,  step: 0.1,  dp: 1 },
    { k: 'gSat',    label: '短波耗散（越小越细）', min: 0, max: 1.5, step: 0.05, dp: 2 },
    { k: 'rain',    label: '欢快雨（0=关）',  min: 0,    max: 2,    step: 0.05, dp: 2 },
    { k: 'drawDpr', label: '性能·分辨率上限', min: 0.6,  max: 1.5,  step: 0.05, dp: 2 }
  ];
  /* ★ 2026-09-19 09:3x：目标容器搬到**皮肤卡**（换肤 → 叠层效果 → 水面参数 → 高级）。
     版式**复用皮肤卡既有的 `.wo-row / .wo-range / .wo-val`**（不再自造 .fxw-* 类），
     档位也不再自建下拉 —— 皮肤卡本来就有「画质档位」#wo-tier 分段。 */
  function buildFxWater(){
    const host = document.getElementById('wo-fx-body');
    if (!host) return;
    const w = window.__water;
    const st = (w && w.tuneState) ? (w.tuneState() || {}) : {};
    host.innerHTML = FX_WATER_SCHEMA.map(function(it){
      const raw = st[it.k];
      const v = (typeof raw === 'number' && isFinite(raw)) ? raw : it.min;
      return '<div class="wo-row">'
        + '<label class="set-label">' + it.label + '</label>'
        + '<input type="range" class="wo-range" data-fxw="' + it.k + '" min="' + it.min
        + '" max="' + it.max + '" step="' + it.step + '" value="' + v + '">'
        + '<span class="wo-val" data-k="' + it.k + '">' + v.toFixed(it.dp) + '</span>'
        + '</div>';
    }).join('')
      + '<div class="wo-btns"><button class="set-mini-btn" id="wo-fx-reset">恢复默认</button></div>';
    if (host._fxBound) return;
    host._fxBound = true;
    host.addEventListener('input', function(e){
      const el = e.target;
      const k = el && el.getAttribute ? el.getAttribute('data-fxw') : null;
      if (!k || !window.__water || !window.__water.tune) return;
      const v = parseFloat(el.value);
      if (!isFinite(v)) return;
      const o = {}; o[k] = v;
      try { window.__water.tune(o); } catch (_) {}
      const it = FX_WATER_SCHEMA.filter(function(x){ return x.k === k; })[0];
      const out = host.querySelector('.wo-val[data-k="' + k + '"]');
      if (out) out.textContent = v.toFixed(it ? it.dp : 3);
    });
    const rb = document.getElementById('wo-fx-reset');
    if (rb) rb.addEventListener('click', function(){
      try { if (window.__water && window.__water.resetOpts) window.__water.resetOpts(); } catch (_) {}
      host._fxBound = false;
      buildFxWater();
    });
  }

  /* 黑盒自检面（offscreen 验证用）：暴露渲染/刷新/大纲，供外部断言 */
  window.__r64 = {
    schema: function(){ return SETTING_SCHEMA; },
    groups: function(){
      /* R67-2：动态分组渲染进 .settings-dynamic[data-gid]（4 tab 结构） */
      return Array.prototype.map.call(
        document.querySelectorAll('.settings-dynamic .set-group'),
        function(g){ return g.dataset.gid; });
    },
    /* R67-2：切 tab（黑盒验证用；等于点 .stab） */
    tab: function(k){
      const b = document.querySelector('#settings-tabs .stab[data-tab="' + k + '"]');
      if (!b) return false;
      b.click();
      return true;
    },
    activeTab: function(){
      const p = document.querySelector('#settings-panel .stab-panel.active');
      return p ? p.dataset.tab : '';
    },
    render: function(){ renderSettingsGroups(); },
    qrModalOpen: function(){
      const m = $('qr-modal'); return !!(m && m.classList.contains('open'));
    },
  };
