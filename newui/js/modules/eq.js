/* newui/js/modules/eq.js — 由老 ui-eq.js 拆出的独立模块（自动生成，勿手改）。
 * factory(ctx) 用 new Function 承载老代码，保持 classic script 的顶层声明语义；
 * 本模块可独立 register/unregister，替换时只需提供等价 factory。
 */
export default {
  name: 'eq',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `/* ══════════════════════════════════════════════════════════
   ui-eq.js — Radio Intelligence 前端模块 · EQ 面板（R68）
   dock #eq（律动条）→ 点击唤出 #eq-panel；手动 10 段 / 预设 chips / 动态强度。
   后端：GET/POST /api/eq（注意：EQ 路由独立于 /api/playback，直接 fetch）。
   与原 index.html 内所有 ui-*.js 共享顶层作用域 → 加载顺序即执行顺序。
   依赖：ui-core（$/st/toast）、ui-orbit（isolatedZone 的 PANEL_SEL 需含 #eq-panel）。
   ══════════════════════════════════════════════════════════ */

  /* ---------- 常量 ---------- */
  const EQ_FREQS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000];
  const EQ_PRESETS = {
    flat:      'Flat', bass: '低音增强', treble: '高音增强', vocal: '人声',
    podcast:   '播客', pop: '流行', rock: '摇滚', hiphop: '嘻哈',
    classical: '古典', loudness: '响度 V'
  };
  const EQ_PRESET_HINT = {
    flat: '复位：全频段 0 dB，EQ 旁路零开销。',
    bass: '提升 31–125 Hz，为薄扬声器 / 耳机补充低频分量。',
    treble: '提升 4k–16k，增加空气感与亮度（老录音 / 闷耳机适用）。',
    vocal: '削减 250 Hz 浑浊 + 提升 1k–4k 人声临场，让人声突出。',
    podcast: '削减 31–62 Hz（等效高通）+ 提升 4k 清晰度，说话更清楚。',
    pop: '微笑曲线：轻抬低频与高频、中频不动，广播向听感。',
    rock: '低频冲击 + 4k/8k 镲片亮度，适合电吉他素材。',
    hiphop: '重 sub-bass（31/62），500 Hz 清泥、8k 提镲。',
    classical: '温和抬升，尊重原混音，久听不累。',
    loudness: 'V 型等响曲线：小音量聆听时补足人耳不敏感频段（静态版）。'
  };
  const EQ_LABELS = { manual: '手动', preset: '预设', dynamic: '动态' };

  let eqState = { enabled:false, source:'manual', preset:'flat',
                  bands:[0,0,0,0,0,0,0,0,0,0], dyn:0.6 };
  let eqPanelOpen = false;
  let _dynTimer = 0;

  /* ---------- 请求 ---------- */
  function eqFetch(qs, method){
    return fetch('/api/eq' + (qs ? ('?' + qs) : ''), { method: method || 'GET' })
      .then(r => r.json()).catch(() => null);
  }
  /* POST /api/eq 走 query 参数（与后端 eq_set 的 b0..b9 签名一致）。
     ⚠ bands 数组要展开成 b0=..&b1=..，server.py 只认 b<i>；传 'bands=1,2'
     会被 FastAPI 当未知 query 丢弃 → 手动滑杆/归零全失效。 */
  function eqPost(obj){
    const parts = [];
    Object.keys(obj).forEach(k => {
      if (k === 'bands'){
        const arr = Array.isArray(obj[k]) ? obj[k] : String(obj[k]).split(',').map(x => Number(x) || 0);
        arr.slice(0, 10).forEach((v, i) => parts.push('b' + i + '=' + encodeURIComponent(v)));
      } else {
        parts.push(k + '=' + encodeURIComponent(obj[k]));
      }
    });
    return eqFetch(parts.join('&'), 'POST');
  }

  /* ---------- 状态同步到 UI ---------- */
  /* ⚠⚠ R69：EQ 总开关从底部大按钮改成**顶栏小滑块**（省高、紧凑）。
     #eqp-power 原是 <button>，现是包住 range 的 <label>（verify_eq_r68 用
     powerOn()/GET enabled，不走 textContent，语义兼容）。
     视觉：value=1 → .on（滑块右滑 accent 点亮）；value=0 → 灰。 */
  function syncEqPower(){
    const pw = $('eqp-power');
    if (pw){
      pw.classList.toggle('on', !!eqState.enabled);
      const rg = $('eqp-power-range');
      if (rg) rg.value = eqState.enabled ? '1' : '0';
      /* label 内文字不需要变：滑块位置本身就是状态（省 width/height 计算） */
      const lb = $('eqp-power-lb');
      if (lb) lb.textContent = eqState.enabled ? 'EQ 开' : 'EQ 关';
    }
    const eqEl = $('eq');
    if (eqEl) eqEl.classList.toggle('on', !!eqState.enabled);
  }
  /* R71：模式提示 eqp-note（手动/预设/动态）已随元素一起移除 ——
     当前模式由 seg 高亮（syncSeg 的 .on）自己表达，无需冗余文字提示。 */
  function syncSeg(){
    document.querySelectorAll('#eqp-seg .eqp-tab').forEach(b =>
      b.classList.toggle('on', b.getAttribute('data-mode') === eqState.source));
    const show = m => {
      const el = $('eqp-' + m);
      if (el) el.hidden = (m !== eqState.source);
    };
    show('manual'); show('preset'); show('dynamic');
    if (eqState.source === 'dynamic'){
      /* 动态 tab 内强度滑杆只读显示（总开关在底部） */
      const dyn = $('eqp-dyn');
      if (dyn){ dyn.value = Math.round((eqState.dyn || 0) * 100); }
      const v = $('eqp-dyn-val');
      if (v) v.textContent = Math.round((eqState.dyn || 0) * 100) + '%';
    }
  }
  /* 手动段滑杆渲染 + 当前值（R68 老版：横排 10 段垂直原生滑杆，
     用户拍板频谱回老版本 —— 恢复 writing-mode vertical range 布局） */
  function renderBands(){
    const box = $('eqp-bands');
    if (!box) return;
    box.innerHTML = EQ_FREQS.map((f, i) => \`
      <div class="eq-band" data-i="\${i}">
        <input type="range" min="-12" max="12" step="1" value="\${eqState.bands[i] || 0}"
               aria-label="EQ \${fmtFreq(f)}Hz">
        <span class="b-db">\${fmtDb(eqState.bands[i] || 0)}</span>
        <span class="b-freq">\${fmtFreq(f)}</span>
      </div>\`).join('');
    box.querySelectorAll('.eq-band input').forEach((inp, i) => {
      inp.addEventListener('input', () => onBandInput(i, inp));
      /* dblclick 归零 */
      inp.addEventListener('dblclick', () => { inp.value = 0; onBandInput(i, inp); });
    });
  }
  function fmtFreq(f){ return f >= 1000 ? (f/1000) + 'k' : String(f); }
  function fmtDb(v){ return (v > 0 ? '+' : '') + v; }
  function onBandInput(i, inp){
    const v = parseInt(inp.value, 10) || 0;
    eqState.bands[i] = v;
    const band = inp.closest('.eq-band');
    const db = band ? band.querySelector('.b-db') : null;
    if (db) db.textContent = fmtDb(v);
    /* 手动模式：拖滑杆实时打后端（节流 120ms，但每次落点都发最终值） */
    if (eqState.source !== 'manual') return;
    clearTimeout(_dynTimer);
    _dynTimer = setTimeout(() => {
      eqPost({ source:'manual', bands: eqState.bands });
    }, 120);
  }
  function renderChips(){
    const box = $('eqp-chips');
    if (!box) return;
    box.innerHTML = Object.keys(EQ_PRESETS).map(k => \`
      <button class="eqp-chip\${eqState.preset === k ? ' on' : ''}" data-preset="\${k}">\${EQ_PRESETS[k]}</button>\`).join('');
    box.querySelectorAll('.eqp-chip').forEach(ch => {
      ch.addEventListener('click', () => pickPreset(ch.getAttribute('data-preset')));
    });
    const hint = $('eqp-preset-hint');
    if (hint) hint.textContent = EQ_PRESET_HINT[eqState.preset] || '';
  }
  function pickPreset(k){
    if (!EQ_PRESETS[k]) return;
    eqState.preset = k;
    eqState.source = 'preset';
    eqPost({ source:'preset', preset:k, enabled: eqState.enabled ? 1 : 0 });
    syncSeg(); renderChips();
  }
  function setSource(mode){
    if (!EQ_LABELS[mode]) return;
    eqState.source = mode;
    eqPost({ source: mode });
    syncSeg();
    if (mode === 'manual') renderBands();
    if (mode === 'preset') renderChips();
  }
  function setDynamic(v01){
    eqState.dyn = Math.max(0, Math.min(1, v01));
    eqPost({ dyn: eqState.dyn });
    syncSeg();
  }
  /* EQ 总开关 */
  function setPower(on){
    eqState.enabled = !!on;
    eqPost({ enabled: eqState.enabled ? 1 : 0 });
    syncEqPower();
  }
  function resetBands(){
    eqState.bands = [0,0,0,0,0,0,0,0,0,0];
    eqPost({ source:'manual', bands: eqState.bands });
    renderBands();
  }

  /* ---------- 面板开关 ---------- */
  function openEqPanel(){
    if (eqPanelOpen) return;
    eqPanelOpen = true;
    const p = $('eq-panel');
    if (!p) return;
    /* 与漫游滑块（#rec-style）同轴同层互斥：面板开时隐藏滑块 */
    const rs = $('rec-style');
    if (rs){ rs.style.display = 'none'; }
    p.classList.add('open');
    p.setAttribute('aria-hidden', 'false');
    eqFetch(null, 'GET').then(d => {
      if (!d || !d.ok) return;
      eqState = {
        enabled: !!d.enabled, source: d.source || 'manual',
        preset: d.preset || 'flat',
        bands: Array.isArray(d.bands) && d.bands.length === 10
                ? d.bands.map(x => parseInt(x, 10) || 0) : [0,0,0,0,0,0,0,0,0,0],
        dyn: typeof d.dyn === 'number' ? d.dyn : 0.6
      };
      syncEqPower(); syncSeg(); renderBands(); renderChips();
    });
  }
  function closeEqPanel(){
    if (!eqPanelOpen) return;
    eqPanelOpen = false;
    const p = $('eq-panel');
    if (p){ p.classList.remove('open'); p.setAttribute('aria-hidden', 'true'); }
    const rs = $('rec-style');
    if (rs){ rs.style.display = ''; try { syncRecMode(); } catch (_) {} }
  }
  function toggleEqPanel(){ eqPanelOpen ? closeEqPanel() : openEqPanel(); }
  function isEqOpen(){ return eqPanelOpen && !!($('eq-panel') && $('eq-panel').classList.contains('open')); }

  /* ---------- 绑定 ---------- */
  function initEqUI(){
    const eqEl = $('eq');
    if (!eqEl) return;
    if (eqEl._eqBound) return;
    eqEl._eqBound = true;
    eqEl.addEventListener('click', toggleEqPanel);
    eqEl.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' '){ e.preventDefault(); toggleEqPanel(); }
    });
    /* 面板关闭钮 + 点外部（非面板、非 #eq）收起 —— 注意 dock 本身不能误关 */
    const closeBtn = $('eqp-close');
    if (closeBtn) closeBtn.addEventListener('click', e => { e.stopPropagation(); closeEqPanel(); });
    document.addEventListener('mousedown', e => {
      if (!eqPanelOpen) return;
      const p = $('eq-panel');
      if (!p) return;
      if (!p.contains(e.target) && e.target !== eqEl && !eqEl.contains(e.target)){
        closeEqPanel();
      }
    });
    /* seg 切换 */
    document.querySelectorAll('#eqp-seg .eqp-tab').forEach(b =>
      b.addEventListener('click', () => setSource(b.getAttribute('data-mode'))));
    /* R69：EQ 总开关 = 顶栏小滑块（label#eqp-power 包隐形 range#eqp-power-range）。
       ⚠⚠ 真机双跳 bug（实证，trusted CDP 点击）：**不可只挂 range change** ——
       range 是 width:0/pointer-events:none，label activation 无法把点击映射成
       range 值变化 → change 永不触发（删 label click 后实证零请求）。
       双跳真相：真实点 label ①先跑 label 的 click 监听 setPower(!enabled)
       （POST enabled=1）→ ②click 默认行为做 label activation，给 range 派发
       合成 click（trusted）→ ③合成 click 冒泡回 label 又跑一次 click 监听
       setPower(!true)=false（POST enabled=0）→ 一开一关互相抵消，用户看到
       "点了没反应"。offscreen verify 用 JS 合成 click（untrusted，不触发
       activation）→ 只跑① → 假绿。
       正确姿势：label click 正常翻转，但**短路从 range 冒泡上来的合成 click**
       （e.target === range），保证一次真实点击只翻转一次。 */
    const pw = $('eqp-power');
    const rg = $('eqp-power-range');
    if (pw){
      pw.addEventListener('click', e => {
        if (e.target === rg) return;   /* label activation 给 range 的合成 click → 忽略 */
        setPower(!eqState.enabled);
      });
    }
    if (rg){
      rg.addEventListener('change', () => setPower(rg.value === '1')); /* 键盘/直拖兜底 */
    }
    /* 动态强度滑杆 */
    const dyn = $('eqp-dyn');
    if (dyn) dyn.addEventListener('input', () => {
      const v = parseInt(dyn.value, 10) / 100;
      const disp = $('eqp-dyn-val');
      if (disp) disp.textContent = Math.round(v * 100) + '%';
      clearTimeout(_dynTimer);
      _dynTimer = setTimeout(() => setDynamic(v), 150);
    });
    const resetBtn = $('eqp-reset');
    if (resetBtn) resetBtn.addEventListener('click', resetBands);
    /* Esc 关闭 */
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && eqPanelOpen) closeEqPanel();
    });
    /* 启动拉一次后端状态（点亮 dock #eq if 已开） */
    eqFetch(null, 'GET').then(d => {
      if (d && d.ok){
        eqState.enabled = !!d.enabled;
        eqState.source = d.source || 'manual';
        eqState.preset = d.preset || 'flat';
        if (Array.isArray(d.bands) && d.bands.length === 10)
          eqState.bands = d.bands.map(x => parseInt(x, 10) || 0);
        if (typeof d.dyn === 'number') eqState.dyn = d.dyn;
        syncEqPower();
      }
    });
    /* 切歌 / 轮询后 EQ 状态不随动 —— 无 per-track 语义，保持全局 */
  }

  /* ---------- 启动 ---------- */
  initEqUI();

  /* R68 验证钩子（offscreen 黑盒） */
  window.__r68 = {
    open:   function(){ openEqPanel(); return isEqOpen(); },
    close:  function(){ closeEqPanel(); return !isEqOpen(); },
    isOpen: function(){ return isEqOpen(); },
    state:  function(){ return JSON.parse(JSON.stringify(eqState)); },
    segOn:  function(){ const b = document.querySelector('#eqp-seg .eqp-tab.on'); return b ? b.getAttribute('data-mode') : ''; },
    chipOn: function(){ const c = document.querySelector('#eqp-chips .eqp-chip.on'); return c ? c.getAttribute('data-preset') : ''; },
    setPreset: function(k){ pickPreset(k); return true; },
    setManualBand: function(i, v){
      /* 走真实链路：先切到手动 tab，再对真实滑杆派发 input（onBandInput 需
         DOM 元素，传假对象会因 parentElement 为 null 抛错）。 */
      if (eqState.source !== 'manual'){
        const tab = document.querySelector('#eqp-seg .eqp-tab[data-mode="manual"]');
        if (tab) tab.click();
      }
      const inp = document.querySelectorAll('#eqp-bands .eq-band input')[i];
      if (!inp) return false;
      inp.value = String(v);
      inp.dispatchEvent(new Event('input', { bubbles: true }));
      return true;
    },
    setDyn: function(v){ setDynamic(v); return true; },
    setPower: function(on){ setPower(!!on); return true; },
    powerOn: function(){ return !!eqState.enabled; },
    bandDb:  function(i){ return eqState.bands[i] || 0; },
    panelVisible: function(){ const p = $('eq-panel'); return p ? p.classList.contains('open') : false; },
    recStyleHidden: function(){ const r = $('rec-style'); return r ? r.style.display === 'none' : false; }
  };
`;
    /* preamble：ctx 既是共享对象，也让老代码里的裸标识符可解析。
       new Function 非严格模式，with 可用 —— 这是"老代码零改动"的关键。 */
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + "  /* \u628a\u672c\u6a21\u5757\u9876\u5c42\u58f0\u660e\u53d1\u5e03\u5230 ctx\uff08\u4f9b\u540e\u7eed\u6a21\u5757 use\uff09 */\n  const __defs = [\"EQ_FREQS\", \"EQ_PRESETS\", \"EQ_PRESET_HINT\", \"EQ_LABELS\", \"eqState\", \"eqPanelOpen\", \"_dynTimer\", \"eqFetch\", \"eqPost\", \"syncEqPower\", \"syncSeg\", \"renderBands\", \"fmtFreq\", \"fmtDb\", \"onBandInput\", \"renderChips\", \"pickPreset\", \"setSource\", \"setDynamic\", \"setPower\", \"resetBands\", \"openEqPanel\", \"closeEqPanel\", \"toggleEqPanel\", \"isEqOpen\", \"initEqUI\"];\n  for (const __n of __defs) {\n    try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {}\n  }" + '\n}');
    return __run(ctx);
  },
};
