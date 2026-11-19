/* newui/js/fine/eggs.js — 「彩蛋」自定义空间模块（源自 ui-eggs.js，自动生成）。
 * 二级拆分：一个文件内的一个 UI 自定义空间 = 一个可注册模块。 */
export default {
  name: 'eggs',
  source: 'ui-eggs.js',
  space: '彩蛋',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `/* ═════════════════════════════════════════════════════════════════════════
 * R105 / E-α · ui-eggs.js
 * 「Radio Intelligence」彩蛋层（实际 UI 部分）。
 * 设计铁则（彩蛋集原则 #8）：可算才显示 / 不可算就标艺术或不做 / 数字追常数。
 * 本文件零水体改动；只读 window.__water.physics() 真值。
 * 反伪科学守卫：verify_eggs.py 的 F 组守着「禁止硬编码未建模量」。
 *
 * 实际 UI 保留：
 *   · Apple 1977 六色彩虹发丝线（换肤卡最顶）
 *   · 声明彩蛋（Homo sapiens / Silicon-based life / ¡Viva la vida y la cooperación entre especies!）
 *   · 隐藏档案页 archive()（Version Archaeology / Museum / Agent Fingerprint / lore）
 *   · 只读诊断（Froude / Phillips / Stokes Correction / sr 44100·48000）
 * 诗卡 / 星露谷像素鸡（真实动画 gif）/ 1984「Hello.」问候 = demo-only（见 _eggs_preview.html）。
 * ⚠ Python / Linux / Acknowledgements / Open Source 致敬 = 仅计划文档设计，未落任何 UI（见 easter_eggs_plan.md）。
 * ═════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';
  /* var $ 去重 */
  // 总开关：tune({eggs:false}) 逐位回退（ui-core.js:255 读此标志）
  window.__eggsOn = true;

  function pick(arr) { return arr[Math.floor(Math.random() * arr.length)]; }

  /* ───────────────────────── 状态机 BOOT→LISTENING→ARRIVING→PLAYING ─────────────────────────
   * 复用真实运行时状态；诗卡已移除，tick 仅维护 _mode 供 __eggs.mode() 使用。 */
  var _mode = 'boot';
  function deriveMode() {
    var playing = (typeof st !== 'undefined' && st && st.playing);
    var ready = (typeof st !== 'undefined' && st && st.ready);
    var title = (typeof st !== 'undefined' && st && st.title);
    if (playing) return 'playing';
    if (title) return 'arriving';
    if (ready) return 'listening';
    return 'boot';
  }
  function tick() { _mode = deriveMode(); }

  /* ───────────────────────── 物理：诚实读取（不伪造） ───────────────────────── */
  function readPhysics() {
    // 优先读运行期内省（ui-water.js 同 closure 常量，零改动）
    if (window.__water && typeof window.__water.physics === 'function') {
      return window.__water.physics();
    }
    // 仅当 __water 不可达时回退到文档常量（仍列 sources 标注来源）
    return { PHY_G: 9.81, PHY_DOM_M: 8.0, PHY_L_MID: 6.0,
             PHY_SPEC_EXP_DEF: -2.0, PHY_STOKES: 0.08,
             BREATH_HZ: 0.07, PHY_DRAIN_TAU: 25.0, PHY_GAM_MIN: 0.04 };
  }
  function diagnostics() {
    var p = readPhysics();
    var g = p.PHY_G, L = p.PHY_L_MID;
    var c = Math.sqrt(g * L / (2 * Math.PI));   // 深水重力波相速度 U=√(gL/2π)
    var Fr = c / Math.sqrt(g * L);              // = 1/√(2π) ≈ 0.399
    return {
      fr: {
        value: +Fr.toFixed(3),
        note: 'Fr = U/√(gL)；U=深水重力波相速度 √(gL/2π) ≈ ' + c.toFixed(2) + ' m/s',
        live: !!(window.__water && window.__water.physics)
      },
      phillipsExp: p.PHY_SPEC_EXP_DEF,          // -2.0
      stokes: { label: 'Stokes correction (NOT drift)', gain: p.PHY_STOKES },
      sr: { engine: 44100, clap: 48000 },        // core/audio_engine.py:39 / core/audio_features.py:367
      breathHz: p.BREATH_HZ,
      drainTau: p.PHY_DRAIN_TAU,
      gamMin: p.PHY_GAM_MIN,
      sources: {
        PHY_G: 'ui-water.js:95', PHY_L_MID: 'ui-water.js:96',
        PHY_SPEC_EXP_DEF: 'ui-water.js:83', PHY_STOKES: 'ui-water.js:64',
        sr_engine: 'core/audio_engine.py:39 STREAM_SR=44100',
        sr_clap: 'core/audio_features.py:367 librosa.load(sr=48000)'
      }
    };
  }

  /* ───────────────────────── E-β 占位：四类自我认知事件（训练子项目接入后填充） ───────────────────────── */
  var _eventCounts = { model_disagree: 0, emotion_uncertain: 0, no_stable_state: 0, revised: 0 };
  function events() {
    return {
      model_disagree: null, emotion_uncertain: null, no_stable_state: null, revised: null,
      note: 'E-β 未实现：四类自我认知事件需训练子项目接入后由 radio_bridge 推送',
      counts: _eventCounts
    };
  }

  /* ───────────────────────── 频率闸（彩蛋集原则 #1「不打扰」） ───────────────────────── */
  var FG = { max: 3, key: 'ri_eggs_fg_' };
  function _todayKey() { var d = new Date(); return d.getFullYear() + '-' + (d.getMonth() + 1) + '-' + d.getDate(); }
  function _fgUsed() { var k = FG.key + _todayKey(), u = 0; try { u = parseInt(localStorage.getItem(k) || '0', 10) || 0; } catch (_) {} return u; }
  function _fgBump() { var k = FG.key + _todayKey(); var u = _fgUsed(); try { localStorage.setItem(k, String(u + 1)); } catch (_) {} return u + 1; }
  function freqGate() {
    var used = _fgUsed();
    return {
      used: used, max: FG.max, remaining: Math.max(0, FG.max - used),
      firstBootBlocked: true,   // 首启 / Empty Player 期一律不出自我怀疑话术
      note: '每日≤3次；首次启动禁用；no_stable_state 与 emotion_uncertain 同轮只出一句'
    };
  }

  /* ───────────────────────── 发现层级矩阵（E14，彩蛋集 §11 L0–L6） ───────────────────────── */
  function discoveryMatrix() {
    return [
      { level: 'L0', name: '世界已在', desc: '开屏即见，无需触发', eggs: ['The Empty Player'] },
      { level: 'L1', name: '命名', desc: '行为已在，仅加文案', eggs: ['Silence', 'Music Not Yet Arrived', 'R82 water remembers'] },
      { level: 'L2', name: '只读诊断', desc: '读真实常量/公式，不伪造', eggs: ['Phillips', 'Tessendorf', 'Schlick', 'Nyquist', 'The Click', 'Audio Callback', 'Froude', 'Stokes Correction', 'BPM', 'Beat This!', 'NaN'] },
      { level: 'L3', name: '隐藏页', desc: '秘密入口可达，普通路径不可达', eggs: ['Version Archaeology', 'Museum of Things That Went Wrong', 'Agent Fingerprint', 'Hohfeld / Code≠Rule / The Bundle'] },
      { level: 'L4', name: 'LLM 自我认知', desc: 'E-β：需训练子项目接入，频率闸约束', eggs: ['The Models Disagree', 'Emotion Uncertain', 'I May Have Misunderstood'] },
      { level: 'L5', name: '反物理显形', desc: 'E-γ：须 R104 解冻', eggs: ['Anti-Physics', 'Wave Breaking'] },
      { level: 'L6', name: '语料级深度', desc: 'E-δ：须 M9 之后', eggs: ['Music Geography', 'GEMS-45 L3', 'agree w/ MERT teacher'] }
    ];
  }

  /* ───────────────────────── 隐藏档案页（秘密入口：__eggs.archive()） ───────────────────────── */
  var ARCHIVE_HTML = [
    '<h2>Radio Intelligence · 隐藏档案</h2>',
    '<p class="muted">本页只经 <code>window.__eggs.archive()</code> 打开；普通界面路径不可达（不打扰原则）。</p>',

    '<h3>Version Archaeology（实测里程碑，R17 不成立已删）</h3>',
    '<table><tr><th>轮次</th><th>事件</th><th>出处</th></tr>',
    '<tr><td>R1</td><td>v1 架构起点（PySide6 + QWebChannel 桌面时代）</td><td>technical_README_interview.md:267</td></tr>',
    '<tr><td>R46</td><td>第二架构：前后端分离（FastAPI + QWebEngineView）</td><td>:271</td></tr>',
    '<tr><td>R63</td><td>音乐理解起点（频谱律动→壁纸→EQ→水面）</td><td>:274</td></tr>',
    '<tr><td>R79</td><td>首个语义层：CLAP 14 轴→分位数标定+频带 onset</td><td>:275</td></tr>',
    '<tr><td>R82</td><td>water remembers：分位数标定 + uCalm 暂停平静化</td><td>interview_qa_ip_role.md:57,79</td></tr>',
    '<tr><td>R86–R95</td><td>水面物理（呼吸/Stokes/饱和谱/排水/反物理）</td><td>ui-water.js</td></tr>',
    '<tr><td>R100–R104</td><td>语义升级（GEMS-45 三层）→ 情绪识别训练子项目</td><td>memory</td></tr>',
    '</table>',

    '<h3>Museum of Things That Went Wrong（记真失败 + 它教了什么）</h3>',
    '<table><tr><th>事故</th><th>本仓真实条目</th><th>出处</th></tr>',
    '<tr><td>THE CLICK</td><td>重采样边界：引擎 44100 ↔ CLAP 48000；biquad 流式状态</td><td>_research_biquad_state.py / _research_streaming.py</td></tr>',
    '<tr><td>WHITE WATER</td><td>波前"只走一半"：30fps 下 61.66 vs 44.91（比值 1.373），Q5 修</td><td>verify_q5_prop</td></tr>',
    '<tr><td>MISSING WEBSOCKET</td><td>uvicorn 缺 [standard] ⇒ WS 静默不支持；TestClient 给假绿灯</td><td>跨轮铁则</td></tr>',
    '<tr><td>CLAP OOM</td><td>R46–R53 真机崩溃修复（CLAP OOM）</td><td>technical_README_interview.md:271</td></tr>',
    '<tr><td>B15</td><td>静默丢维：KeyError ⇒ 整集中止、report.json 不落盘、被 grep 吞</td><td>R104 §9k</td></tr>',
    '<tr><td>B16</td><td>幂等判据 UnboundLocalError ⇒ 旧缓存永刷不新</td><td>R104 §9l</td></tr>',
    '<tr><td>爆音</td><td>特征帧 30Hz 抖动的真实爆音</td><td>README R63–R81</td></tr>',
    '<tr><td>val R²=−1.305</td><td>年份迁移崩塌 = 真发现，不是 bug</td><td>R104 §9c</td></tr>',
    '</table>',

    '<h3>Agent Fingerprint（人定问题 / Agent 扩展 / 人终判）</h3>',
    '<p>本项目的真实开发方式：人提出可断言的问题 → Agent 在约束内扩展实现 → 人做最终判断与验收。',
    '口播 LLM 只引用运行时算出的事实，不生成置信度或分歧（反伪科学硬约束）。</p>',

    '<h3>Private Lore（Hohfeld / Code ≠ Rule / The Bundle）</h3>',
    '<p><b>Hohfeld：</b>权利·特权·权力·豁免 与 无权利·义务·无能力·责任 的关联矩阵——权限不是单点开关。</p>',
    '<p><b>Code ≠ Rule：</b>实现（implementation）≠ 语义（semantics）；水面是"被组织感知的状态"，不是固定对象集合。</p>',
    '<p><b>The Bundle：</b>emotion = bundle of relations（情绪是一束关系，而非一个独立实体）。</p>',

    '<h3>Credits（声明 · 订正版）</h3>',
    '<p>Originally designed by Homo sapiens<br>assembled and implemented by Silicon-based life<br><b>¡Viva la vida y la cooperación entre especies!</b></p>',
    '<h3>不做清单（诚实交底）</h3>',
    '<p class="muted">韦伯数（无 σ，未建模）/ 雷诺数（无 ν，除非显式标 ν_eff）/ 拍号（全仓无检测能力）/ Z-Fighting（无真实触发场景）/ Stokes 原义需速度场，已改名 Stokes Correction。</p>'
  ].join('\\n');

  function archive() {
    var card = $('eggs-archive-card'); if (!card) return;
    card.innerHTML = ARCHIVE_HTML;
    var a = $('eggs-archive'); if (a) a.classList.add('show');
  }
  function _closeArchive() { var a = $('eggs-archive'); if (a) a.classList.remove('show'); }
  // 关闭按钮 + Esc
  if ($('eggs-archive-close')) $('eggs-archive-close').addEventListener('click', _closeArchive);
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') _closeArchive(); });

  /* ───────────────────────── 回退：tune({eggs:false}) 逐位回旧文案 ───────────────────────── */
  function tune(map) {
    if (map && map.eggs === false) {
      window.__eggsOn = false;
      var t = $('dc-title');
      if (t) {
        var s = (typeof st !== 'undefined' && st) ? st : null;
        t.textContent = (s && s.title) ? s.title
                       : ((s && s.ready) ? '未在播放' : '电台准备中');
      }
      return { eggs: false };
    }
    if (map && map.eggs === true) {
      window.__eggsOn = true;
      return { eggs: true };
    }
    return { eggs: window.__eggsOn !== false };
  }

  /* ───────────────────────── 公开 API ───────────────────────── */
  window.__eggs = {
    mode: function () { return _mode; },
    diagnostics: diagnostics,
    events: events,
    freqGate: freqGate,
    discoveryMatrix: discoveryMatrix,
    archive: archive,
    tune: tune
  };

  /* ───────────────────────── 启动 ───────────────────────── */
  tick();
  setInterval(tick, 700);
  if (typeof console !== 'undefined') console.info('[eggs] R105 / E-α loaded · the world is already here.');
})();
`;
    const __epi = `  for (const __n of ["pick", "_mode", "deriveMode", "tick", "readPhysics", "diagnostics", "_eventCounts", "events", "FG", "_todayKey", "_fgUsed", "_fgBump", "freqGate", "discoveryMatrix", "ARCHIVE_HTML", "archive", "_closeArchive", "tune"]) { try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {} }`;
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + __epi + '\n}');
    return __run(ctx);
  },
};
