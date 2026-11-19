/* water-config.js — 两个 UI 共用的**唯一**水体调参面（2026-11-18）。
 *
 * 背景（docs/WATER_TWO_ENTRY_AUDIT_20261118.md）：
 *   /      老 UI（start_radio.bat）
 *   /new   新 UI（newui/）
 * 两者加载的是**同一个** /ui-water.js。之前"两个入口观感不同"全部来自驱动层差异：
 *   · 新 UI 强制 tune({kEstNorm:0.25})，老 UI 从不发送 ⇒ 耗散模型不同；
 *   · 新 UI 的 ambient 定时器在播放中也每 1.8s drop(0.55) ⇒ 场能量 +51%。
 * 本文件把"调好的一套"集中在这里，**两个页面都引它** ⇒ 老 UI 复用新 UI 的调校，
 * 以后只改这一处，两个入口永远一致。
 *
 * 加载顺序：必须在 /ui-water.js 之后（页面里紧跟其后），DOM 就绪后 apply。
 * 全部值可在控制台用 __water.tune({...}) 覆盖做 A/B。
 */
(function () {
  'use strict';

  /* ═══ 共用调参（2026-11-18 首版）═══
     kEstNorm : 波数估计的幅度归一化（0=旧式；0.25=新 UI 实验值，引擎已加"幅度底"防跳变）
     capSoft  : 软膝饱和（1=开）—— 治"放大太大 ⇒ 削顶"
     freezeMax: 凝结上限 —— 治"冻结太狠"
     ampGain  : 注入幅度总倍率（1.15 → 1.0，压掉一点"放大太大"）
     kMul     : 波速总倍率（1.0 → 0.88）—— 治"波动速度太快"
     gSat     : 高波数阻尼系数（越大 ⇒ 越有"阻尼感"）
     coolJitter / eventK / echoTailK : 前几轮已验证的节奏/尾响参数
  */
  var WATER_PROFILE = {
    drawDpr: 0.90,   // 十七轮：DRAW 缓冲 0.9×（省 GPU；上采样更柔和，压摩尔纹/闪烁）
    kEstNorm: 0.25,
    capSoft: 1,
    /* ★ 2026-11-18 二轮（用户：冻结还是太狠 / 化冻太慢 / 面积太大 / 参数太高）：
       freezeMax  0.55 → 0.30（凝结量上限降 45%）
       freezeDamp 0.90 → 0.40（冻结的额外阻尼砍半以上 ⇒ 冻住时仍有流动感）
       freezeRel  化冻 τ 2.2s → 0.7s（音乐回来立刻化开）
       freezeArea 0.6（冻结优先作用于平滑区，有涟漪处少冻 ⇒ 冻结面积显小） */
    /* ★★★ 2026-11-18 七轮（用户："老 UI 参数好一点，只有整个冻结特别坑"）：
       **冻结整体默认关闭**（引擎总开关 TUNE.freeze=0 ⇒ uFreeze 恒 0）。
       反复调小上限/阻尼/面积/化冻速度都仍被感知为"坑"，说明"凝结"这个效果本身
       在真实观感里收益为负 ⇒ 先彻底关掉，把观感交给"排水/尾响/去节拍器"。
       想试回来：__water.tune({freeze:0.25})（0~1），下面四个的慢参数已就位。 */
    freeze: 1.0,    // 2026-11-18 十轮：冻结恢复（用户："冻结是否恢复一些了"→ 之前 0.35×0.12 等于没开）
    freezeMax: 0.35,
    freezeDamp: 0.30,
    freezeRel: 0.30,
    freezeArea: 1.00,
    /* dynFall→freeze 的耦合权重。旧硬编码 0.35 是"全局敏感度一高就结冰"的根因
       （dynGain 越大 dynFall 越大 ⇒ 冻结目标被抬高）⇒ 降到 0.08。 */
    freezeDyn: 0.0,
    ampGain: 0.92,
    kMul: 0.66,
    gSat: 1.40,
    /* 亚网格耗散（s⁻¹）：只吃 λ≤5 texel 的"细密残留"（1.5 → 2.2）。
       微涟漪层是程序化图案、永不消散；SIM 侧的细残留靠这一项吃掉。 */
    subGrid: 2.20,
    /* 小振幅额外耗散（s⁻¹）：让"细残留"快速排掉（0 → 0.18）。原理见引擎 softAmp 注释。 */
    softAmp: 0.18,
    /* ★★★ 2026-11-18 八轮（用户："几乎是固定的波纹沾满了整个屏幕只有简单蠕动"）：
       定位到两块**屏幕空间固定干涉图案**（凝结已确认关闭、freeze.u≡0）：
         ① 焦散 caustic：`sin(vUv.x*9 + …) + sin(vUv.y*7 + …)` = 写死的 9×7 网格，
            只有相位缓慢漂移 ⇒ 满屏固定格纹 + 简单蠕动；
         ② 微涟漪 micro：6 个**固定方向**平面波干涉，空间频率写死 6.0、方向整场不变。
       ⇒ 默认大幅压掉：causticK=0（完全关）、microK=0.25（只留一点细纹暗示）。
       全关对照：__water.tune({microK:0})；若关掉水面仍在 ⇒ 是新 UI 自己的液态背景层。
       以后若要"活的"焦散/微涟漪，正确做法是让图案由**波场**驱动（而非屏幕空间固定频率）。 */
    causticK: 0.0,    // 2026-11-18 十七轮：**关闭**（9×7 sin 网格 + 画布放大 = 又一个摩尔纹源）
    /* 2026-11-18 十二轮（用户："除了细密波纹就满意了"）：**程序化微涟漪默认关**。
       它是 6 个固定方向平面波的干涉、**永不消散** ⇒ 满屏细密纹样、最不自然的那层。
       要质感时：__water.tune({microK:0.2})（0.65 频率）；想更细：{microFreqK:1.0}。 */
    /* 2026-11-18 十三轮：微涟漪**改回开启**，但由真实波场驱动门控制——
       波面平了它自动消失、波在它才在（跟着波一起"排掉"，不再恒定贴纸）。 */
    microK: 0.0,    // 2026-11-18 十七轮：**关闭**（用户：水体变摩尔纹）——6 方向波银行干涉 = 人工摩尔纹
    microFieldK: 1.0,     // 1=完全由场驱动（0=旧行为：恒定纹理）
    microFieldLo: 0.0028, // |∇h| 低于此 → 彻底不出现（十六轮：0.0012→0.0028，触发更难）
    microFieldHi: 0.0160, // |∇h| 高于此 → 完整强度（十六轮：0.0090→0.0160，面积更小）
    microFreqK: 0.65, // 微涟漪空间频率 6.0 → 3.9 周期/屏：纹样变粗变少，不再"细密"
    microPatch: 0.06, // 细纹成片噪声（0.35→0.20→0.10→0.06）
    microDriveHi: 1.00,   // 驱动满量程（0.85→1.00，更难拉满）
    microDriveFloor: 0.18,// 驱动起征点（低于此完全不出现）
    eventK: 1.15,   // 十七轮：微涟漪/焦散关掉后，用事件脉冲把物理波场撑起来（不是屏幕图案，无摩尔纹）
    coolJitter: 0.6,
    echoTailK: 1.0,
    /* ★★★ 2026-11-18 三轮（用户："全局情绪敏感度再调高一点，有点冷淡了"）：
       semContrast 1.00 → 1.45：各轴以 0.5 为中性点扩张（平静更静、热烈更烈），
                               也是"绝对值敏感度"的主要旋钮；
       semSpeed    1.00 → 1.50：情绪目标的跟随速率 ×1.5（τ 1.19/4.17 → 0.79/2.78s），
                               治"切换不够顺滑/反应慢"；
       dynGain     0.50 → 0.90：一阶差分通道（变化敏感度）加 80%；
       ampGain     1.00 → 1.10：把上一轮为治"放大太大"降掉的幅度补一点回来
                               （配合软膝，不会再削顶）。
       运行时 A/B：__water.tune({semContrast:1.7}) / {semSpeed:1.2} / {dynGain:0.5} */
    /* 2026-11-18 四轮：全局敏感度取前两轮的**中间值**
       （用户：冷 1.00/1.00/0.50 ↔ 过了 1.45/1.50/0.90 ⇒ 取中间 1.22/1.25/0.70）。
       四轮："中间偏向低侧 30%" = 区间 35% 处 ⇒ 1.16 / 1.18 / 0.64（用户觉得中间值仍偏高）。
       六轮（用户："再小、波速太快、冻结面积/解冻还要修"）：1.00 / 1.00 / 0.30 / 0.98，kMul 0.88→0.78，
       运行时 A/B：__water.tune({semContrast:1.0}) / {semSpeed:1.0} / {dynGain:0.3} */
    semContrast: 1.00,
    semSpeed: 1.00,
    dynGain: 0.30
  };

  var applied = false;
  function apply() {
    var w = window.__water;
    if (!w || typeof w.tune !== 'function') return false;
    try { w.tune(WATER_PROFILE); } catch (e) { return false; }
    applied = true;
    try {
      var st = (typeof w.tuneState === 'function') ? w.tuneState() : null;
      console.info('[water-config] applied', JSON.stringify({
        kEstNorm: st && st.kEstNorm, capSoft: st && st.capSoft,
        freezeMax: st && st.freezeMax, ampGain: st && st.ampGain
      }));
    } catch (e) {}
    return true;
  }

  /* 水体是 classic script，可能尚未执行完；轮询几次直到 __water 可用 */
  var tries = 0;
  function boot() {
    if (apply()) return;
    if (tries++ > 40) return;                 // ~8s 后放弃（不影响页面）
    setTimeout(boot, 200);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }

  /* 供控制台/验证读取 */
  window.__WATER_PROFILE = WATER_PROFILE;
  window.__waterConfigApplied = function () { return applied; };
})();
