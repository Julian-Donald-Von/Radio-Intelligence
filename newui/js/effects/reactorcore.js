/* newui/js/effects/reactorcore.js — 叠层效果插件：堆芯（声场反应堆 / CORE 01）。
 *
 * 用户 2026-11-19：「是那个冷却堆的那个芯……正常网上有那个音频响应的特效，
 * 但是接线成情绪相应就好」。对应的就是 wzk112/reactor-audio-visualizer：
 * 切尔诺贝利式堆芯、709 根控制棒的环形阵列，原本由 Web Audio 频谱驱动。
 *
 * 本插件保留那套视觉（709 根方形控制棒、等距投影、橄榄绿堆芯、同心环底座），
 * 把输入整体换掉：
 *   · 不再创建 AudioContext、不再读 FFT / 麦克风 / 音频文件；
 *   · 输入 = window.__mood.axes（后端 mood 帧的 14 维语义轴）；
 *   · 整体激烈度（arousal 为主）→ 整座堆芯的平均高度 / 亮度；
 *   · 14 维轴按「中心 → 边缘」排成一条情绪谱 → 每一圈控制棒的高度；
 *   · mood 段变化 → 一道从中心向外扩散的抬升波（不是音频 onset）。
 *
 * 视觉核心改编自（MIT License）：
 *   wzk112/reactor-audio-visualizer
 *   https://github.com/wzk112/reactor-audio-visualizer
 *   Copyright (c) 2026 Andy Wang
 *   完整 MIT 许可见同目录 reactorcore.LICENSE.txt。
 *
 * 插件契约见 effects.js：{ id, type:'ontop', name, onEnable, onDisable }
 * 与 water / sonic 等所有 ontop 插件互斥（同一个叠层槽）。
 *
 * 诊断：window.__reactorcore.state() / tune({...}) / debugHeat(0..1|null) /
 *       debugAxes(axes|null) / pulse()
 */
import { registerEffect } from '../effects.js';

/* ════════════════════════════════════════════════════════════════════
   可调参数
   ════════════════════════════════════════════════════════════════════ */
const TUNE = {
  zoom: 1.0,           // 堆芯大小倍率（满屏基准下微调）
  height: 6.2,         // 单根控制棒最大基准高度（世界单位，原 demo 约 4.8）
  sensitivity: 1.0,    // 情绪 → 控制棒高度总增益
  smoothing: 0.78,     // 控制棒跟随平滑度（越大越稳，映射为弹簧阻尼）
  pulse: 1,            // 情绪段变化时向外扩散的抬升波
  details: 0,          // 棒顶中心小圆点 / 高光（默认关，709 根时性价比低）
  strokes: 2,          // 0=不描边；非 0=描每根棒的竖直棱线（默认 2）
  renderScale: 0.62,   // 核心层渲染缩放（静态地板/设备仍是全分辨率）
  label: 0,            // 底座左下角小铭牌「CORE 01 · 声场反应堆」
  dprCap: 1.5,         // 渲染 DPR 上限

  /* ═══ 跳动引擎：情绪 → 频率 / 幅度 / 面积 / 根数 ═══
     每一拍给选中的控制棒一个向上的速度冲量，由弹簧收回。
     低情绪：约 4.5s 一拍、中心 28% 半径、24 根、0.35 单位高；
     高情绪：约 0.5s 一拍、全半径、709 根、3.2 单位高。 */
  jumpOn: 1,
  jumpFreqMin: 0.22,   // 拍频下限（Hz）
  jumpFreqMax: 2.0,    // 拍频上限（Hz）
  jumpAmpMin: 0.35,    // 单拍抬升下限（世界单位）
  jumpAmpMax: 3.6,     // 单拍抬升上限（世界单位）
  jumpAreaMin: 0.28,   // 参与跳动的半径占比下限
  jumpAreaMax: 1.0,    // 参与跳动的半径占比上限
  jumpCountMin: 24,    // 单拍参与根数下限
  jumpCountMax: 709,   // 单拍参与根数上限
  stiffness: 22,       // 弹簧刚度（越大回弹越快）
  damping: 7,          // 弹簧阻尼（越大越不弹）
};

const TAU = Math.PI * 2;
const clamp01 = (v) => Math.max(0, Math.min(1, Number(v) || 0));
const clamp = (v, a, b) => Math.max(a, Math.min(b, Number(v) || 0));
const lerp = (a, b, t) => a + (b - a) * t;
const rand = (a, b) => a + Math.random() * (b - a);
const mixRGB = (a, b, t) => [
  Math.round(lerp(a[0], b[0], t)),
  Math.round(lerp(a[1], b[1], t)),
  Math.round(lerp(a[2], b[2], t)),
];

/* 14 维语义轴里，**高度只由“激活/强度”类轴决定**；valence 不参与高度，
   而是转成颜色（暖金 ↔ 冷锈），否则“开心但安静”会把某一圈无端抬高。
   由中心到边缘的顺序 = 从“总激活”到“弱激活/氛围”，权重逐级递减：
   这也正好对应原 demo “低频在内、高频在外”的能量分布（外圈通常更矮）。
   最后一项是固定结构起伏，不随情绪变化。 */
const AXES_PROFILE = [
  { k: 'arousal', w: 1.00 },
  { k: 'a1_energetic', w: 0.95 },
  { k: 'u_aggressive', w: 0.90 },
  { k: 'a8_sharp', w: 0.85 },
  { k: 'a2_tense', w: 0.80 },
  { k: 'a5_dense', w: 0.76 },
  { k: 'a6_electronic', w: 0.72 },
  { k: 'a3_bright', w: 0.68 },
  { k: 'a7_expansive', w: 0.62 },
  { k: 'a4_warm', w: 0.58 },
  { k: 'u_dreamy', w: 0.50 },
  { k: 'u_ambient', w: 0.45 },
  { k: 'u_melancholic', w: 0.45 },
];
const AXES_ORDER = AXES_PROFILE.map((x) => x.k);

const AXIS_W = {
  arousal: 0.55,
  a1_energetic: 0.20,
  a8_sharp: 0.12,
  a2_tense: 0.07,
};

/* 整体激烈度（0..1）。和水面 P4 语义层的强度口径一致：arousal 主导，
   energetic/sharp/tense 补足；u_aggressive 只在激活侧额外加权。 */
function axesIntensity(axes) {
  if (!axes || typeof axes !== 'object') return null;
  let sum = 0, wsum = 0;
  for (const k in AXIS_W) {
    const v = Number(axes[k]);
    if (!Number.isFinite(v)) continue;
    sum += clamp01(v) * AXIS_W[k];
    wsum += AXIS_W[k];
  }
  if (wsum <= 0) return null;
  let m = sum / wsum;
  const agg = Number(axes.u_aggressive);
  if (Number.isFinite(agg) && agg > 0.5) m = lerp(m, 1, (agg - 0.5) * 0.28);
  return clamp01((m - 0.12) / 0.76);
}

function axisValue(axes, key) {
  const v = Number(axes && axes[key]);
  return Number.isFinite(v) ? clamp01(v) : 0.5;
}

/* 半径 u∈[0,1] 处的情绪谱值：在 13 个激活轴之间线性插值。
   每个端点先乘该轴的“能量权重”，所以外圈天然比内圈矮；
   某个外圈轴特别强时仍能抬起，不会永远被压死。 */
function profileAt(axes, u) {
  if (!axes) return 0.5;
  const p = clamp01(u) * (AXES_PROFILE.length - 1);
  const i = Math.floor(p);
  const f = p - i;
  const a = axisValue(axes, AXES_PROFILE[i].k) * AXES_PROFILE[i].w;
  const b = axisValue(axes, AXES_PROFILE[Math.min(AXES_PROFILE.length - 1, i + 1)].k)
          * AXES_PROFILE[Math.min(AXES_PROFILE.length - 1, i + 1)].w;
  return a + (b - a) * f;
}

function readAxesFallback() {
  /* 与上一版相同的 1Hz 兜底：mood 帧先写 __mood，再转发给 __water；
     只有 __mood 缺席时才去读水体快照（semantics() 会深拷贝，不能每帧调）。 */
  try {
    const s = window.__water && window.__water.semantics && window.__water.semantics();
    if (s && s.axes && Object.keys(s.axes).length) return s.axes;
  } catch (_e) {}
  return null;
}

/* 原 demo 的棒顶配色：大部分橄榄/沙色，少数棕褐、灰绿。
   为了合批，归成 4 类 × 4 档亮度 = 最多 16 种颜色，视觉差异保留。 */
const TOP_BASE = [
  [143, 109, 81],    // 棕褐
  [94, 122, 102],    // 灰绿
  [146, 156, 120],   // 沙橄榄
  [160, 166, 132],   // 浅沙
];
/* ════════════════════════════════════════════════════════════════════
   场景
   ════════════════════════════════════════════════════════════════════ */
class ReactorCoreScene {
  constructor() {
    this.canvas = null;
    this.ctx = null;
    this.w = 0; this.h = 0; this.dpr = 1;
    this.running = false;
    this.raf = 0;
    this.last = 0;
    this.t = 0;

    this.s = 20;          // 世界单位 → 屏幕像素
    this.cx = 0;
    this.cy = 0;
    this.angle = -0.32;   // 原 demo 的默认视角
    this.tilt = 0.58;
    this.rods = [];

    this.heat = 0.05;
    this.target = 0.05;
    this.valence = 0.5;
    this._axes = null;
    this._axesSum = null;
    this._fallbackAt = 0;
    this._fallbackAxes = null;
    this._mood = null;
    this._moodSeg = null;
    this.pulse = 0;
    this.waveR = 0;
    this.flash = 0;
    this.forcedHeat = null;
    this.forcedAxes = null;

    /* 跳动引擎状态：拍频 / 幅度 / 半径 / 根数由情绪实时推导 */
    this.jumpTimer = 0.6;
    this.jumpFreq = TUNE.jumpFreqMin;
    this.jumpAmp = TUNE.jumpAmpMin;
    this.jumpArea = TUNE.jumpAreaMin;
    this.jumpCount = TUNE.jumpCountMin;
    this.pulses = 0;
    this.lastJumpN = 0;
    this.motionOn = true;
    this._topFills = null;
    this._topValence = -1;

    this.fps = 60;
    this.frameMs = 16.7;
    this.lowFx = false;
    this.ticks = 0;

    this._onFrame = this._frame.bind(this);
    this._onResize = this.resize.bind(this);
    this._onVisibility = this._visibility.bind(this);
    this.reducedMq = null;
    this.reduced = false;
    this._onReduced = (e) => { this.reduced = !!(e && e.matches); };
  }

  /* ---------- 生命周期 ---------- */

  ensure() {
    if (this.canvas && this.canvas.isConnected) return;
    let host = document.getElementById('bg');
    if (!host) {
      host = document.createElement('div');
      host.id = 'bg';
      host.style.cssText = 'position:fixed;inset:0;z-index:0;overflow:hidden;pointer-events:none;';
      document.body.appendChild(host);
    }
    const make = (id, z) => {
      const c = document.createElement('canvas');
      c.id = id;
      c.setAttribute('aria-hidden', 'true');
      /* z=2 与 #water/#sonic-canvas 同槽：ontop 效果互斥。
         本插件用三层：静态背景（地板/设备）→ 动态控制棒 → 静态前景（栏杆/设备）。 */
      c.style.cssText =
        'position:absolute;inset:0;width:100%;height:100%;z-index:' + z + ';' +
        'display:none;pointer-events:none;';
      host.appendChild(c);
      return c;
    };
    this.bgCanvas = make('reactorcore-bg', '2');
    this.canvas = make('reactorcore', '2');
    this.canvas.id = 'reactorcore';
    this.fgCanvas = make('reactorcore-fg', '2');
    this.bgCtx = this.bgCanvas.getContext('2d', { alpha: true });
    this.ctx = this.canvas.getContext('2d', { alpha: true, desynchronized: true });
    this.fgCtx = this.fgCanvas.getContext('2d', { alpha: true });
    if (window.matchMedia) {
      try {
        this.reducedMq = window.matchMedia('(prefers-reduced-motion: reduce)');
        this.reduced = !!this.reducedMq.matches;
        if (this.reducedMq.addEventListener) this.reducedMq.addEventListener('change', this._onReduced);
        else if (this.reducedMq.addListener) this.reducedMq.addListener(this._onReduced);
      } catch (_e) {}
    }
  }

  enable() {
    if (this.running) return true;
    this.ensure();
    if (!this.ctx) return false;
    /* 与水面互斥：切换过来先把水体关掉（persist=false，不动偏好）。 */
    try {
      const w = window.__water;
      if (w && typeof w.setEnabled === 'function') w.setEnabled(false, false);
    } catch (_e) {}
    this.resize();
    for (const c of [this.bgCanvas, this.canvas, this.fgCanvas]) {
      if (c) c.style.display = 'block';
    }
    this.running = true;
    this.t = 0;
    this.last = performance.now();
    this.pulse = 0.8;
    this.waveR = 0;
    this.flash = 0.25;
    this.jumpTimer = 0.45;
    window.addEventListener('resize', this._onResize, { passive: true });
    document.addEventListener('visibilitychange', this._onVisibility);
    this.raf = requestAnimationFrame(this._onFrame);
    return true;
  }

  disable() {
    this.running = false;
    if (this.raf) { cancelAnimationFrame(this.raf); this.raf = 0; }
    window.removeEventListener('resize', this._onResize);
    document.removeEventListener('visibilitychange', this._onVisibility);
    for (const c of [this.bgCanvas, this.canvas, this.fgCanvas]) {
      if (c) c.style.display = 'none';
    }
  }

  _visibility() {
    if (!this.running) return;
    this.last = performance.now();
    if (!document.hidden && !this.raf) this.raf = requestAnimationFrame(this._onFrame);
  }

  /* ---------- 尺寸 / 几何 ---------- */

  resize() {
    if (!this.canvas) return;
    const host = this.canvas.parentElement || document.body;
    const rect = host.getBoundingClientRect();
    const W = Math.max(320, Math.round(rect.width || window.innerWidth || 1280));
    const H = Math.max(240, Math.round(rect.height || window.innerHeight || 720));
    const dpr = clamp(window.devicePixelRatio || 1, 1, TUNE.dprCap);
    const rs = clamp(TUNE.renderScale, 0.4, 1);
    this.w = W; this.h = H; this.dpr = dpr;

    /* 三层画布：背景/前景全分辨率，只有控制棒层降采样。 */
    for (const c of [this.bgCanvas, this.fgCanvas]) {
      if (!c) continue;
      c.width = Math.round(W * dpr);
      c.height = Math.round(H * dpr);
    }
    this.canvas.width = Math.round(W * dpr * rs);
    this.canvas.height = Math.round(H * dpr * rs);
    if (this.bgCtx) this.bgCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (this.fgCtx) this.fgCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.ctx.setTransform(dpr * rs, 0, 0, dpr * rs, 0, 0);

    /* 满屏构图：堆芯直径约 82% 屏宽；更平的俯角（tilt .42）露出周围地板/设备；
       中心下移到 .68H，让远侧控制棒顶端不压到曲名。 */
    this.angle = -0.32;
    this.tilt = 0.45;
    const baseS = Math.min(
      (W * 0.82) / 33,
      (H * 0.74) / (33 * this.tilt + TUNE.height)
    );
    this.s = baseS * TUNE.zoom;
    this.cx = W * 0.46;
    this.cy = H * 0.68;
    this.lowFx = false;
    this._buildRods();
    this._drawBg();
    this._drawFg();
  }

  /* ══════════════════════════════════════════════════════════════════
     静态场景（背景层）：工业地板、环形走道、堆芯底座、外围设备、厂房。
     这些只在 resize 时画一次，之后每帧只 drawImage 到合成器，成本为零。
     ══════════════════════════════════════════════════════════════════ */

  _readAccent() {
    try {
      const cs = getComputedStyle(document.body);
      let v = cs.getPropertyValue('--accent').trim()
           || cs.getPropertyValue('--primary').trim()
           || '#a99bff';
      if (!/^#[0-9a-f]{3,8}$/i.test(v) && !/^rgba?\(/i.test(v) && !/^hsla?\(/i.test(v)) {
        v = '#a99bff';
      }
      return v;
    } catch (_e) { return '#a99bff'; }
  }

  _drawBg() {
    const ctx = this.bgCtx;
    if (!ctx) return;
    const W = this.w, H = this.h;
    ctx.clearRect(0, 0, W, H);
    this._accent = this._readAccent();

    const g = ctx.createLinearGradient(0, 0, 0, H);
    g.addColorStop(0, '#070a0c');
    g.addColorStop(0.36, '#101719');
    g.addColorStop(0.70, '#0c1214');
    g.addColorStop(1, '#050808');
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, W, H);

    this._drawHall(ctx);
    this._drawFloor(ctx);
    this._drawWalkway(ctx);
    this._drawBgEquipment(ctx);
    this._drawCoreSpill(ctx);
    this._drawBase(ctx, 0.5);
    this._rail(ctx, 20.35, Math.PI * 1.01, Math.PI * 1.99, 1.35, '#4f5847');
    this._drawLamps(ctx);
  }

  /* 厂房顶部的暗部、立柱、桁架；只在最上面 1/3，压住画面、托住曲名。 */
  _drawHall(ctx) {
    const W = this.w, H = this.h;
    const fog = ctx.createLinearGradient(0, 0, 0, H * 0.38);
    fog.addColorStop(0, 'rgba(2,4,5,.78)');
    fog.addColorStop(0.55, 'rgba(5,8,9,.45)');
    fog.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = fog;
    ctx.fillRect(0, 0, W, H * 0.38);

    ctx.save();
    const cols = [0.07, 0.26, 0.50, 0.74, 0.93];
    for (const f of cols) {
      const x = W * f;
      ctx.fillStyle = 'rgba(9,14,15,.78)';
      ctx.fillRect(x - 7, 0, 14, H * 0.25);
      ctx.strokeStyle = 'rgba(22,32,32,.5)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x - 7, H * 0.23);
      ctx.lineTo(x + 7, 0);
      ctx.stroke();
    }
    ctx.strokeStyle = 'rgba(14,21,21,.75)';
    ctx.lineWidth = 8;
    ctx.beginPath();
    ctx.moveTo(0, H * 0.055);
    ctx.lineTo(W, H * 0.055);
    ctx.stroke();
    ctx.lineWidth = 3;
    for (let i = 0; i <= 6; i++) {
      const x = W * (i / 6);
      ctx.beginPath();
      ctx.moveTo(x, H * 0.055);
      ctx.lineTo(x + W * 0.07, H * 0.18);
      ctx.stroke();
    }
    ctx.restore();
  }

  /* 大地面：投影成一个覆盖整屏的四边形，再做板缝/刻度/水渍。 */
  _drawFloor(ctx) {
    const s = this.s, cx = this.cx, cy = this.cy;
    const quad = [
      this._project(-70, -70), this._project(70, -70),
      this._project(70, 70), this._project(-70, 70),
    ];
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(quad[0][0], quad[0][1]);
    for (let i = 1; i < 4; i++) ctx.lineTo(quad[i][0], quad[i][1]);
    ctx.closePath();

    const g = ctx.createRadialGradient(cx, cy, s * 2, cx, cy, s * 36);
    g.addColorStop(0, 'rgba(32,41,45,.62)');
    g.addColorStop(0.40, 'rgba(18,26,30,.88)');
    g.addColorStop(0.80, 'rgba(10,15,18,.96)');
    g.addColorStop(1, 'rgba(5,9,12,.99)');
    ctx.fillStyle = g;
    ctx.fill();
    ctx.clip();

    /* 板缝网格 */
    for (let k = -48; k <= 48; k += 4) {
      const major = k % 8 === 0;
      ctx.strokeStyle = major ? 'rgba(150,170,176,.12)' : 'rgba(116,138,144,.060)';
      ctx.lineWidth = major ? 1 : 0.6;
      let p = this._project(k, -60);
      ctx.beginPath();
      ctx.moveTo(p[0], p[1]);
      p = this._project(k, 60);
      ctx.lineTo(p[0], p[1]);
      ctx.stroke();
      p = this._project(-60, k);
      ctx.beginPath();
      ctx.moveTo(p[0], p[1]);
      p = this._project(60, k);
      ctx.lineTo(p[0], p[1]);
      ctx.stroke();
    }

    /* 外圈放射缝（每 15°） */
    for (let i = 0; i < 24; i++) {
      const a = i / 24 * TAU;
      ctx.strokeStyle = 'rgba(128,150,146,.045)';
      ctx.lineWidth = 0.7;
      let p = this._project(Math.cos(a) * 24, Math.sin(a) * 24);
      ctx.beginPath();
      ctx.moveTo(p[0], p[1]);
      p = this._project(Math.cos(a) * 70, Math.sin(a) * 70);
      ctx.lineTo(p[0], p[1]);
      ctx.stroke();
    }

    /* 几块油渍/水渍：非常低，只增加地面的“不干净”层次 */
    let seed = 17;
    const rnd = () => { seed = (seed * 16807) % 2147483647; return seed / 2147483647; };
    for (let i = 0; i < 14; i++) {
      const x = rnd() * 68 - 34;
      const z = rnd() * 68 - 34;
      const w = 3 + rnd() * 7;
      const d = 2 + rnd() * 5;
      const p = [
        this._project(x, z), this._project(x + w, z),
        this._project(x + w, z + d), this._project(x, z + d),
      ];
      ctx.beginPath();
      ctx.moveTo(p[0][0], p[0][1]);
      for (let j = 1; j < 4; j++) ctx.lineTo(p[j][0], p[j][1]);
      ctx.closePath();
      ctx.fillStyle = i % 3 ? 'rgba(128,146,136,.018)' : 'rgba(180,195,170,.014)';
      ctx.fill();
    }
    this._puddle(ctx, 13, 7, 3.2, 1.5, 'rgba(130,160,120,.075)');
    this._puddle(ctx, -10, 11, 2.4, 1.2, 'rgba(100,140,140,.055)');
    this._puddle(ctx, -18, -6, 2.0, 1.0, 'rgba(140,150,110,.045)');

    /* 安全线：外圈虚线，给地板一个尺度感 */
    ctx.save();
    ctx.setLineDash([10, 14]);
    this._ring(ctx, 23.5, 'rgba(202,182,92,.22)', 2);
    ctx.restore();
    ctx.restore();
  }

  _puddle(ctx, x, z, rx, rz, color) {
    const p = this._project(x, z, 0);
    const R = rx * this.s, Ry = rz * this.s * this.tilt;
    ctx.save();
    ctx.beginPath();
    ctx.ellipse(p[0], p[1], R, Ry, 0, 0, TAU);
    ctx.clip();
    const g = ctx.createRadialGradient(p[0] - R * 0.2, p[1] - Ry * 0.2, 0, p[0], p[1], R);
    g.addColorStop(0, color);
    g.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = g;
    ctx.fillRect(p[0] - R, p[1] - Ry, R * 2, Ry * 2);
    ctx.restore();
  }

  /* 堆芯外侧的环形走道：格栅、危险色环、安全线、内圈台阶 */
  _drawWalkway(ctx) {
    const s = this.s, cx = this.cx, cy = this.cy;
    const ro = 20.2 * s, ri = 16.5 * s;
    const ryO = ro * this.tilt, ryI = ri * this.tilt;
    const path = () => {
      ctx.beginPath();
      ctx.ellipse(cx, cy, ro, ryO, 0, 0, TAU);
      ctx.ellipse(cx, cy, ri, ryI, 0, 0, TAU, true);
    };

    path();
    ctx.fillStyle = 'rgba(19,27,23,.94)';
    ctx.fill('evenodd');

    ctx.save();
    path();
    ctx.clip('evenodd');
    for (let r = 16.9; r <= 20.0; r += 0.45) {
      this._ring(ctx, r, 'rgba(136,158,122,.17)', 0.55);
    }
    for (let i = 0; i < 96; i++) {
      const a = i / 96 * TAU;
      const p1 = this._project(Math.cos(a) * 16.6, Math.sin(a) * 16.6);
      const p2 = this._project(Math.cos(a) * 20.1, Math.sin(a) * 20.1);
      ctx.strokeStyle = 'rgba(126,148,118,.10)';
      ctx.lineWidth = 0.5;
      ctx.beginPath();
      ctx.moveTo(p1[0], p1[1]);
      ctx.lineTo(p2[0], p2[1]);
      ctx.stroke();
    }
    for (const r of [17.5, 18.6, 19.6]) {
      this._ring(ctx, r, 'rgba(205,193,122,.10)', 1);
    }
    ctx.restore();

    this._hazardRing(ctx, 19.55, 20.15, 96);
    this._ring(ctx, 20.18, '#23261a', 1);
    ctx.save();
    ctx.setLineDash([7, 9]);
    this._ring(ctx, 20.55, 'rgba(204,184,95,.30)', 2);
    ctx.restore();

    /* 近侧走道上的堆芯反光：让“光从堆芯里出来”这件事落到地面上 */
    const ry = 16.4 * s * this.tilt;
    const rf = ctx.createRadialGradient(cx, cy + ry * 0.72, 0, cx, cy + ry * 0.72, s * 6.5);
    rf.addColorStop(0, 'rgba(158,180,112,.075)');
    rf.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.save();
    ctx.globalCompositeOperation = 'lighter';
    ctx.fillStyle = rf;
    ctx.beginPath();
    ctx.ellipse(cx, cy + ry * 0.72, s * 7.5, s * 2.2, 0, 0, TAU);
    ctx.fill();
    ctx.restore();

    /* 内圈台阶/唇边 */
    ctx.beginPath();
    ctx.ellipse(cx, cy, ri, ryI, 0, 0, TAU);
    const ri2 = 16.15 * s, ryI2 = ri2 * this.tilt;
    ctx.ellipse(cx, cy, ri2, ryI2, 0, 0, TAU, true);
    ctx.fillStyle = 'rgba(54,66,54,.92)';
    ctx.fill('evenodd');
    this._ring(ctx, 16.5, '#0a0f0a', 3);
    this._ring(ctx, 16.15, 'rgba(180,190,130,.35)', 1);
  }

  _hazardRing(ctx, r0, r1, n) {
    for (let i = 0; i < n; i++) {
      const a = i / n * TAU, a1 = (i + 1) / n * TAU;
      const p1 = this._project(Math.cos(a) * r0, Math.sin(a) * r0);
      const p2 = this._project(Math.cos(a1) * r0, Math.sin(a1) * r0);
      const p3 = this._project(Math.cos(a1) * r1, Math.sin(a1) * r1);
      const p4 = this._project(Math.cos(a) * r1, Math.sin(a) * r1);
      ctx.beginPath();
      ctx.moveTo(p1[0], p1[1]);
      ctx.lineTo(p2[0], p2[1]);
      ctx.lineTo(p3[0], p3[1]);
      ctx.lineTo(p4[0], p4[1]);
      ctx.closePath();
      ctx.fillStyle = i % 2 ? 'rgba(185,154,62,.72)' : 'rgba(22,24,17,.92)';
      ctx.fill();
    }
  }

  /* 栏杆：bg 画远半圈，fg 画近半圈，方向由角度范围决定 */
  _rail(ctx, r, a0, a1, h, color) {
    const steps = Math.max(8, Math.round((a1 - a0) / 0.16));
    ctx.save();
    ctx.lineCap = 'round';
    /* 地面投影阴影 */
    ctx.strokeStyle = 'rgba(0,0,0,.32)';
    ctx.lineWidth = 3;
    ctx.beginPath();
    for (let i = 0; i <= steps; i++) {
      const a = a0 + (a1 - a0) * i / steps;
      const p = this._project(Math.cos(a) * r, Math.sin(a) * r, 0);
      if (i) ctx.lineTo(p[0], p[1] + 5);
      else ctx.moveTo(p[0], p[1] + 5);
    }
    ctx.stroke();

    /* 立柱 */
    ctx.strokeStyle = color;
    ctx.lineWidth = 2.2;
    for (let i = 0; i <= steps; i++) {
      const a = a0 + (a1 - a0) * i / steps;
      const b = this._project(Math.cos(a) * r, Math.sin(a) * r, 0);
      const t = this._project(Math.cos(a) * r, Math.sin(a) * r, h);
      ctx.beginPath();
      ctx.moveTo(t[0], t[1]);
      ctx.lineTo(b[0], b[1]);
      ctx.stroke();
    }
    /* 两道横杆 */
    for (const lev of [h * 0.52, h]) {
      ctx.strokeStyle = lev === h ? '#6a735c' : color;
      ctx.lineWidth = lev === h ? 1.8 : 1.2;
      ctx.beginPath();
      for (let i = 0; i <= steps; i++) {
        const a = a0 + (a1 - a0) * i / steps;
        const p = this._project(Math.cos(a) * r, Math.sin(a) * r, lev);
        if (i) ctx.lineTo(p[0], p[1]);
        else ctx.moveTo(p[0], p[1]);
      }
      ctx.stroke();
    }
    ctx.restore();
  }

  /* 等距长方体（设备外壳/机柜） */
  _isoBox(ctx, x, z, w, d, h, top, left, right, stroke) {
    const hw = w / 2, hd = d / 2;
    const a0 = this._project(x - hw, z - hd);
    const b0 = this._project(x + hw, z - hd);
    const c0 = this._project(x + hw, z + hd);
    const d0 = this._project(x - hw, z + hd);
    const a = this._project(x - hw, z - hd, h);
    const b = this._project(x + hw, z - hd, h);
    const c = this._project(x + hw, z + hd, h);
    const dd = this._project(x - hw, z + hd, h);
    const sc = this._project(x, z, 0);

    ctx.save();
    ctx.fillStyle = 'rgba(0,0,0,.35)';
    ctx.beginPath();
    ctx.ellipse(sc[0], sc[1] + 4, w * 0.65 * this.s, d * 0.65 * this.s * this.tilt, 0, 0, TAU);
    ctx.fill();
    ctx.restore();

    const poly = (pts, fill) => {
      ctx.beginPath();
      ctx.moveTo(pts[0][0], pts[0][1]);
      for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
      ctx.closePath();
      ctx.fillStyle = fill;
      ctx.fill();
    };
    poly([b, b0, c0, c], right);
    poly([c, c0, d0, dd], left);
    poly([a, b, c, dd], top);
    ctx.strokeStyle = stroke || '#0b1210';
    ctx.lineWidth = 0.8;
    for (const pts of [[a, b, c, dd], [b, b0, c0, c], [c, c0, d0, dd]]) {
      ctx.beginPath();
      ctx.moveTo(pts[0][0], pts[0][1]);
      for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
      ctx.closePath();
      ctx.stroke();
    }
    /* 正面板缝 */
    const f1 = this._project(x + hw, z - hd, h * 0.42);
    const f2 = this._project(x + hw, z + hd, h * 0.42);
    ctx.strokeStyle = 'rgba(0,0,0,.30)';
    ctx.lineWidth = 0.7;
    ctx.beginPath();
    ctx.moveTo(f1[0], f1[1]);
    ctx.lineTo(f2[0], f2[1]);
    ctx.stroke();

    /* 顶面受光边：让设备在暗厂房里有一圈可读的轮廓 */
    ctx.strokeStyle = 'rgba(196,208,164,.28)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(a[0], a[1]);
    ctx.lineTo(b[0], b[1]);
    ctx.lineTo(c[0], c[1]);
    ctx.stroke();
  }

  /* 等距圆柱（泵壳/桶） */
  _isoCylinder(ctx, x, z, r, h, top, side, band) {
    const b = this._project(x, z, 0);
    const t = this._project(x, z, h);
    const rx = r * this.s, ry = r * this.s * this.tilt;
    ctx.save();
    ctx.fillStyle = 'rgba(0,0,0,.35)';
    ctx.beginPath();
    ctx.ellipse(b[0], b[1] + 4, rx * 1.25, ry * 1.25, 0, 0, TAU);
    ctx.fill();
    ctx.restore();

    ctx.fillStyle = side;
    ctx.fillRect(b[0] - rx, t[1], rx * 2, Math.max(1, b[1] - t[1]));
    ctx.beginPath();
    ctx.ellipse(b[0], b[1], rx, ry, 0, 0, Math.PI);
    ctx.fill();
    if (band) {
      ctx.fillStyle = 'rgba(196,168,70,.34)';
      const bh = Math.max(2, (b[1] - t[1]) * 0.13);
      const by = t[1] + (b[1] - t[1]) * 0.52;
      ctx.fillRect(b[0] - rx, by, rx * 2, bh);
    }
    ctx.beginPath();
    ctx.ellipse(t[0], t[1], rx, ry, 0, 0, TAU);
    ctx.fillStyle = top;
    ctx.fill();
    ctx.strokeStyle = 'rgba(0,0,0,.35)';
    ctx.lineWidth = 0.8;
    ctx.stroke();
    ctx.strokeStyle = 'rgba(180,190,150,.18)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.ellipse(t[0], t[1], rx * 0.66, ry * 0.66, 0, 0, TAU);
    ctx.stroke();
  }

  /* 管道：世界点 → 粗线 + 高光 + 法兰 */
  _pipe(ctx, pts, radius, color, hi) {
    const P = pts.map((p) => this._project(p.x, p.z, p.h || 0));
    const w = Math.max(2, radius * 2 * this.s);
    ctx.save();
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.strokeStyle = 'rgba(0,0,0,.36)';
    ctx.lineWidth = w + 4;
    ctx.beginPath();
    for (let i = 0; i < P.length; i++) {
      if (i) ctx.lineTo(P[i][0] + 2, P[i][1] + 6);
      else ctx.moveTo(P[0][0] + 2, P[0][1] + 6);
    }
    ctx.stroke();
    ctx.strokeStyle = color;
    ctx.lineWidth = w;
    ctx.beginPath();
    for (let i = 0; i < P.length; i++) {
      if (i) ctx.lineTo(P[i][0], P[i][1]);
      else ctx.moveTo(P[0][0], P[0][1]);
    }
    ctx.stroke();
    ctx.strokeStyle = hi;
    ctx.lineWidth = Math.max(1, w * 0.22);
    ctx.beginPath();
    for (let i = 0; i < P.length; i++) {
      if (i) ctx.lineTo(P[i][0], P[i][1] - w * 0.22);
      else ctx.moveTo(P[0][0], P[0][1] - w * 0.22);
    }
    ctx.stroke();
    for (const p of P) {
      ctx.beginPath();
      ctx.ellipse(p[0], p[1], radius * 1.35 * this.s, radius * 1.35 * this.s * this.tilt, 0, 0, TAU);
      ctx.fillStyle = '#4a5448';
      ctx.fill();
      ctx.strokeStyle = 'rgba(0,0,0,.3)';
      ctx.lineWidth = 0.6;
      ctx.stroke();
    }
    ctx.restore();
  }

  /* 外围设备：机柜、泵、管、桶、立柱 */
  _drawBgEquipment(ctx) {
    const pol = (r, deg) => {
      const a = deg * Math.PI / 180;
      return { x: Math.cos(a) * r, z: Math.sin(a) * r };
    };
    const acc = this._accent;

    /* 远侧机柜 */
    for (const deg of [206, 248, 294]) {
      const p = pol(18.6, deg);
      this._isoBox(ctx, p.x, p.z, 4.8, 2.6, 2.4,
        '#2b3631', '#1b2420', '#232d27', '#0b1210');
      const lp = this._project(p.x, p.z, 2.15);
      ctx.fillStyle = acc;
      ctx.globalAlpha = 0.85;
      ctx.fillRect(lp[0] - 2, lp[1] - 2, 4, 3);
      ctx.fillRect(lp[0] - 2, lp[1] - 10, 2, 7);
      ctx.fillStyle = '#d6a24a';
      ctx.fillRect(lp[0] + 3, lp[1] - 1, 3, 2);
      ctx.globalAlpha = 1;
      /* 主题高亮色的 LED 微光，把界面配色带进场景 */
      ctx.save();
      ctx.globalCompositeOperation = 'lighter';
      const lg = ctx.createRadialGradient(lp[0], lp[1] - 5, 0, lp[0], lp[1] - 5, 16);
      lg.addColorStop(0, acc);
      lg.addColorStop(1, 'rgba(0,0,0,0)');
      ctx.globalAlpha = 0.24;
      ctx.fillStyle = lg;
      ctx.beginPath();
      ctx.arc(lp[0], lp[1] - 5, 16, 0, TAU);
      ctx.fill();
      ctx.restore();
    }

    /* 左右两台主泵 */
    const pumpL = pol(19.2, 180);
    const pumpR = pol(19.2, 0);
    this._isoCylinder(ctx, pumpL.x, pumpL.z, 2.0, 2.5, '#39453c', '#26302a', true);
    this._isoCylinder(ctx, pumpR.x, pumpR.z, 2.0, 2.5, '#39453c', '#26302a', true);
    this._isoBox(ctx, pumpL.x, pumpL.z, 5.2, 5.2, 0.55, '#313b33', '#1d2621', '#26312a');
    this._isoBox(ctx, pumpR.x, pumpR.z, 5.2, 5.2, 0.55, '#313b33', '#1d2621', '#26312a');

    /* 泵到堆芯的管道 */
    this._pipe(ctx, [
      { x: pumpL.x, z: pumpL.z, h: 1.0 },
      { x: -16.2, z: -2.6, h: 1.0 },
      { x: -15.0, z: -1.0, h: 0.65 },
    ], 0.42, '#3d4840', '#66735f');
    this._pipe(ctx, [
      { x: pumpR.x, z: pumpR.z, h: 1.0 },
      { x: 16.2, z: 2.6, h: 1.0 },
      { x: 15.0, z: 1.0, h: 0.65 },
    ], 0.42, '#3d4840', '#66735f');

    /* 远侧横向管廊 + 电缆桥架 */
    this._pipe(ctx, [
      { x: -22, z: -17.5, h: 0.45 },
      { x: 22, z: -17.5, h: 0.45 },
    ], 0.5, '#343f38', '#5a675a');
    ctx.save();
    ctx.strokeStyle = 'rgba(70,84,72,.55)';
    ctx.lineWidth = 2;
    for (const off of [-0.5, 0.5]) {
      const a = this._project(-20, -15.5 + off, 0.18);
      const b = this._project(20, -15.5 + off, 0.18);
      ctx.beginPath();
      ctx.moveTo(a[0], a[1]);
      ctx.lineTo(b[0], b[1]);
      ctx.stroke();
    }
    ctx.lineWidth = 1;
    for (let x = -20; x <= 20; x += 2) {
      const a = this._project(x, -16, 0.18);
      const b = this._project(x, -15, 0.18);
      ctx.beginPath();
      ctx.moveTo(a[0], a[1]);
      ctx.lineTo(b[0], b[1]);
      ctx.stroke();
    }
    ctx.restore();

    /* 远侧立柱（高瘦，压住画面纵深） */
    for (const deg of [224, 316]) {
      const p = pol(24, deg);
      this._isoBox(ctx, p.x, p.z, 1.4, 1.4, 12, '#141d1d', '#0d1516', '#111a1a', '#080d0e');
    }

    /* 桶/罐 */
    const b1 = pol(18.9, 330);
    const b2 = pol(18.9, 150);
    this._isoCylinder(ctx, b1.x, b1.z, 0.8, 1.6, '#3f4a3d', '#2a332a');
    this._isoCylinder(ctx, b2.x, b2.z, 0.8, 1.6, '#3f4a3d', '#2a332a');
  }

  /* 顶灯：暗厂房里的暖色光锥（纯屏幕空间，最后画） */
  _drawLamps(ctx) {
    const W = this.w, H = this.h;
    ctx.save();
    ctx.globalCompositeOperation = 'lighter';
    for (const f of [0.18, 0.50, 0.82]) {
      const x = W * f, y = H * 0.065;

      /* 灯下的地面光池（柔和的椭圆，不用硬三角） */
      ctx.save();
      ctx.translate(x, H * 0.34);
      ctx.scale(1, 0.30);
      const pool = ctx.createRadialGradient(0, 0, 0, 0, 0, 150);
      pool.addColorStop(0, 'rgba(255,218,158,.085)');
      pool.addColorStop(0.55, 'rgba(255,205,140,.034)');
      pool.addColorStop(1, 'rgba(0,0,0,0)');
      ctx.fillStyle = pool;
      ctx.beginPath();
      ctx.arc(0, 0, 150, 0, TAU);
      ctx.fill();
      ctx.restore();

      /* 很淡的光柱，只作为空气感 */
      const beam = ctx.createLinearGradient(x, y, x, H * 0.34);
      beam.addColorStop(0, 'rgba(255,218,158,.028)');
      beam.addColorStop(1, 'rgba(255,218,158,0)');
      ctx.fillStyle = beam;
      ctx.fillRect(x - 14, y, 28, H * 0.34 - y);

      /* 灯泡/吊线 */
      const bulb = ctx.createRadialGradient(x, y, 0, x, y, 14);
      bulb.addColorStop(0, 'rgba(255,232,190,.85)');
      bulb.addColorStop(1, 'rgba(255,220,170,0)');
      ctx.fillStyle = bulb;
      ctx.beginPath();
      ctx.arc(x, y, 14, 0, TAU);
      ctx.fill();
      ctx.strokeStyle = 'rgba(20,25,24,.65)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, y);
      ctx.stroke();
    }
    ctx.restore();
  }

  /* 前景层：近半圈栏杆、近侧设备、暗角 */
  _drawFg() {
    const ctx = this.fgCtx;
    if (!ctx) return;
    const W = this.w, H = this.h;
    ctx.clearRect(0, 0, W, H);

    this._rail(ctx, 20.35, 0.02, Math.PI * 0.98, 1.35, '#5b6450');

    const pol = (r, deg) => {
      const a = deg * Math.PI / 180;
      return { x: Math.cos(a) * r, z: Math.sin(a) * r };
    };
    for (const deg of [28, 152]) {
      const p = pol(21.0, deg);
      this._isoCylinder(ctx, p.x, p.z, 0.55, 1.15, '#46503f', '#2b342a');
      const top = this._project(p.x, p.z, 1.0);
      ctx.fillStyle = 'rgba(200,178,86,.55)';
      ctx.fillRect(top[0] - 3, top[1] - 1, 6, 2);
      /* 主题高亮色的小警示灯，和界面呼应 */
      const acc = this._accent || '#a99bff';
      ctx.save();
      ctx.globalCompositeOperation = 'lighter';
      const bg = ctx.createRadialGradient(top[0], top[1] - 6, 0, top[0], top[1] - 6, 12);
      bg.addColorStop(0, acc);
      bg.addColorStop(1, 'rgba(0,0,0,0)');
      ctx.globalAlpha = 0.28;
      ctx.fillStyle = bg;
      ctx.beginPath();
      ctx.arc(top[0], top[1] - 6, 12, 0, TAU);
      ctx.fill();
      ctx.restore();
    }

    /* 前景管线：从画面底部两角压进来，增加“设备在身边”的感觉 */
    ctx.save();
    ctx.lineCap = 'round';
    ctx.strokeStyle = 'rgba(8,12,11,.85)';
    ctx.lineWidth = 26;
    ctx.beginPath();
    ctx.moveTo(-20, H * 0.82);
    ctx.quadraticCurveTo(W * 0.10, H * 0.88, W * 0.22, H * 1.02);
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(W + 20, H * 0.80);
    ctx.quadraticCurveTo(W * 0.90, H * 0.88, W * 0.80, H * 1.02);
    ctx.stroke();
    ctx.strokeStyle = 'rgba(90,105,88,.22)';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(-20, H * 0.815);
    ctx.quadraticCurveTo(W * 0.10, H * 0.875, W * 0.22, H * 1.01);
    ctx.stroke();
    ctx.restore();

    /* 暗角 + 底部压暗（让 dock 和曲名更清楚） */
    const v = ctx.createRadialGradient(this.cx, this.cy, Math.min(W, H) * 0.28,
      this.cx, this.cy, Math.max(W, H) * 0.78);
    v.addColorStop(0, 'rgba(0,0,0,0)');
    v.addColorStop(0.55, 'rgba(0,0,0,.10)');
    v.addColorStop(1, 'rgba(0,0,0,.60)');
    ctx.fillStyle = v;
    ctx.fillRect(0, 0, W, H);
    const b = ctx.createLinearGradient(0, H * 0.86, 0, H);
    b.addColorStop(0, 'rgba(0,0,0,0)');
    b.addColorStop(1, 'rgba(0,0,0,.55)');
    ctx.fillStyle = b;
    ctx.fillRect(0, H * 0.86, W, H * 0.14);
  }


  _project(x, z, h = 0) {
    const rx = x * Math.cos(this.angle) - z * Math.sin(this.angle);
    const rz = x * Math.sin(this.angle) + z * Math.cos(this.angle);
    return [this.cx + rx * this.s, this.cy + rz * this.s * this.tilt - h * this.s];
  }

  _buildRods() {
    /* 原 demo 的阵列：x,z ∈ [-15,15]，半径 ≤15 的整数格点 → 709 根。 */
    const rods = [];
    for (let x = -15; x <= 15; x++) {
      for (let z = -15; z <= 15; z++) {
        const r = Math.hypot(x, z);
        if (r > 15) continue;
        const seed = Math.abs(Math.sin(x * 127.1 + z * 311.7));
        rods.push({
          x, z, r, seed,
          h: 0.07,
          v: 0,
          amp: 0,
          depth: x * Math.sin(this.angle) + z * Math.cos(this.angle),
          topKind: seed > 0.88 ? 0 : seed > 0.76 ? 1 : seed > 0.42 ? 2 : 3,
          a0: this._project(x - 0.38, z - 0.38),
          b0: this._project(x + 0.38, z - 0.38),
          c0: this._project(x + 0.38, z + 0.38),
          d0: this._project(x - 0.38, z + 0.38),
          pc: this._project(x, z),
        });
      }
    }
    rods.sort((a, b) => a.depth - b.depth);   // 远 → 近，保证遮挡正确
    this.rods = rods;
  }

  /* ---------- 情绪输入 ---------- */

  _tick(dt) {
    const now = performance.now();
    let axes = this.forcedAxes;
    const mood = window.__mood;
    if (!axes) {
      if (mood && typeof mood === 'object' && mood.src !== 'none'
          && mood.axes && Object.keys(mood.axes).length) {
        axes = mood.axes;
      } else {
        if (now - this._fallbackAt > 1000) {
          this._fallbackAt = now;
          this._fallbackAxes = readAxesFallback();
        }
        axes = this._fallbackAxes;
      }
    }
    this._mood = mood;
    this._axes = axes;

    const st = window.st;
    this.motionOn = !(st && st.playing === false);

    let target = axesIntensity(axes);
    if (target == null) target = 0.05;
    if (this.forcedHeat != null) target = this.forcedHeat;
    target = clamp01(target);
    this.target = target;

    /* 起势快、回落慢：情绪段之间过渡自然，不闪。 */
    const tau = target > this.heat ? 0.85 : 2.4;
    this.heat += (target - this.heat) * (1 - Math.exp(-dt / tau));

    /* valence 不参与高度，用于颜色：高=暖金，低=冷锈 */
    const v = axes ? Number(axes.valence) : NaN;
    this.valence = Number.isFinite(v) ? clamp01(v) : 0.5;

    /* 情绪段变化 → 一道向外的抬升波（替代原 demo 的音频 onset）。
       优先用后端 seg；没有 seg 时退回轴和变化（阈值放大，避免小抖动误触发）。 */
    const seg = (mood && mood.seg != null && Number.isFinite(Number(mood.seg)))
      ? Number(mood.seg) : null;
    if (seg != null) {
      if (this._moodSeg != null && seg !== this._moodSeg && this.heat > 0.12) {
        this.pulse = 1;
        this.waveR = 0;
      }
      this._moodSeg = seg;
    } else {
      let sum = 0;
      for (let i = 0; i < AXES_PROFILE.length; i++) {
        sum += axes ? axisValue(axes, AXES_PROFILE[i].k) : 0.5;
      }
      if (this._axesSum != null && Math.abs(sum - this._axesSum) > 0.08 && this.heat > 0.12) {
        this.pulse = 1;
        this.waveR = 0;
      }
      this._axesSum = sum;
    }
    this.pulse *= Math.exp(-dt * 1.45);
    this.waveR = Math.min(15, this.waveR + dt * 7.4);
    this.flash *= Math.exp(-dt * 3.2);

    /* ═══ 情绪 → 跳动四参数 ═══
       freq  拍频（Hz）：0.22 → 2.0
       amp   单拍抬升（世界单位）：0.35 → 3.2
       area  参与半径占比：0.28 → 1.0
       count 单拍根数：24 → 709
       四条都用 heat 的幂曲线；减少动态时整体压低。 */
    const heat = this.heat;
    const motion = this.reduced ? 0.45 : 1;
    this.jumpFreq = lerp(TUNE.jumpFreqMin, TUNE.jumpFreqMax, Math.pow(heat, 1.15))
                  * (this.reduced ? 0.55 : 1);
    this.jumpAmp = lerp(TUNE.jumpAmpMin, TUNE.jumpAmpMax, Math.pow(heat, 1.1)) * motion;
    this.jumpArea = lerp(TUNE.jumpAreaMin, TUNE.jumpAreaMax, Math.pow(heat, 0.8));
    this.jumpCount = Math.round(lerp(TUNE.jumpCountMin, TUNE.jumpCountMax, Math.pow(heat, 1.1))
                    * (this.reduced ? 0.55 : 1));

    if (TUNE.jumpOn && this.motionOn) {
      this.jumpTimer -= dt;
      if (this.jumpTimer <= 0) {
        this.jumpTimer = (1 / Math.max(0.05, this.jumpFreq)) * rand(0.72, 1.28);
        this._jumpPulse();
      }
    } else if (!this.motionOn) {
      this.jumpTimer = Math.max(this.jumpTimer, 0.4);
    }
  }

  /* 一次跳动：按面积选半径，按根数随机挑棒，给一个向上的速度冲量。 */
  _jumpPulse() {
    const n = this.rods.length;
    if (!n || this.jumpAmp <= 0) return;
    const areaR = clamp(this.jumpArea, 0.05, 1) * 16;
    const eligible = [];
    for (const rod of this.rods) {
      /* 软边界：半径外仍按高斯概率参与，避免高情绪时边缘出现一圈“死平”的环 */
      const edge = rod.r <= areaR ? 1 : Math.exp(-Math.pow((rod.r - areaR) / 1.4, 2));
      if (edge >= 1 || Math.random() < edge) eligible.push(rod);
    }
    if (!eligible.length) return;
    const count = Math.min(eligible.length, Math.max(1, Math.round(this.jumpCount)));
    const v0 = this.jumpAmp * Math.sqrt(TUNE.stiffness);

    if (count >= eligible.length) {
      for (const rod of eligible) this._kick(rod, v0);
    } else {
      /* 部分 Fisher-Yates：不复制整个池，随机挑 count 根 */
      const pool = eligible;
      for (let i = 0; i < count; i++) {
        const j = i + ((Math.random() * (pool.length - i)) | 0);
        const tmp = pool[i]; pool[i] = pool[j]; pool[j] = tmp;
        this._kick(pool[i], v0);
      }
    }
    this.lastJumpN = count;
    this.pulses++;
    this.flash = Math.max(this.flash, 0.16 + this.heat * 0.34);
  }

  _kick(rod, v0) {
    const falloff = 1 - 0.30 * (rod.r / 15);
    rod.v += v0 * falloff * rand(0.70, 1.30);
  }

  _update(dt) {
    const axes = this._axes;
    const t = this.t;
    const s = this.s;
    const smooth = clamp(TUNE.smoothing, 0, 0.95);
    const motion = this.reduced ? 0.45 : 1;
    const stiff = TUNE.stiffness;
    /* smoothing 越大，阻尼越大（回弹更慢、更稳） */
    const damp = TUNE.damping * (0.75 + smooth * 0.5);
    const fills = this._topPalette();

    for (const rod of this.rods) {
      const u = rod.r / 15;
      const prof = profileAt(axes, u);
      const act = clamp01((prof - 0.22) / 0.62);

      /* 整体激烈度决定「整座堆芯」的基准高度；情绪谱在径向做明暗起伏。
         这里给情绪谱更大的权重（0.16 + 0.84×act）：激烈时不是整块一起抬，
         而是有的圈高、有的圈低，才像「堆芯」而不是一根均匀的圆柱。 */
      let amp = this.heat * (0.35 + 0.65 * act);

      /* 固定结构起伏：即使 13 个激活轴同时被顶到 1，也不至于变成一块均匀的圆柱。
         这是「堆芯本身的高低差」，不随情绪变化；情绪仍然决定整体高度。 */
      const relief = 0.74 + 0.26 * (0.5 + 0.5 * Math.sin(
        rod.r * 1.9 + rod.x * 0.7 + rod.z * 0.55
      ));
      amp *= relief;

      if (TUNE.pulse && this.pulse > 0.01) {
        const w = (u - this.waveR / 15) / 0.16;
        amp += this.pulse * 0.32 * Math.exp(-w * w) * motion;
      }
      /* 待机呼吸 / 高情绪时的持续微颤：纯时间函数，不读音频。 */
      amp += (1 - this.heat) * 0.025 * Math.sin(t * 0.7 + rod.seed * 9) * motion;
      amp += this.heat * 0.012 * Math.sin(t * 2.4 + rod.seed * 20) * motion;
      amp = clamp01(amp * TUNE.sensitivity);
      rod.sideBucket = Math.min(2, Math.floor(amp * 3));

      /* 基准高度 + 两条慢/快呼吸波；跳动冲量由 _jumpPulse 注入 rod.v。
         弹簧会把冲量收回来，所以高情绪时是「一下一下地跳」而不是静态抬高。 */
      let targetH = 0.07 + amp * TUNE.height * motion;
      targetH += (0.015 + 0.035 * this.heat)
               * Math.sin(t * 0.85 + rod.seed * 7 + u * 2.2) * motion;
      targetH += this.heat * 0.045 * act
               * Math.sin(t * 2.6 + rod.seed * 13 + u * 5.2) * motion;

      rod.v += ((targetH - rod.h) * stiff - rod.v * damp) * dt;
      rod.h += rod.v * dt;
      if (rod.h < 0.04) { rod.h = 0.04; rod.v = 0; }
      rod.amp = amp;

      /* 顶面四角 = 底面四角整体上移；批量绘制时每根只算一次。 */
      const dy = -rod.h * s;
      rod.ta = [rod.a0[0], rod.a0[1] + dy];
      rod.tb = [rod.b0[0], rod.b0[1] + dy];
      rod.tc = [rod.c0[0], rod.c0[1] + dy];
      rod.td = [rod.d0[0], rod.d0[1] + dy];

      const bucket = Math.min(3, Math.floor(clamp01(amp) * 4));
      rod.topFill = fills[rod.topKind][bucket];
    }
  }

  /* 顶面色板：valence 高→暖金，低→冷锈；只在 valence 明显变化时重建。 */
  _topPalette() {
    if (this._topFills && Math.abs(this.valence - this._topValence) < 0.01) {
      return this._topFills;
    }
    const val = this.valence;
    const warmK = val > 0.5 ? (val - 0.5) * 2 * 0.30 : 0;
    const coldK = val < 0.5 ? (0.5 - val) * 2 * 0.30 : 0;
    const warm = [214, 198, 146];
    const cold = [176, 112, 84];
    this._topFills = TOP_BASE.map((base) => [0, 1, 2, 3].map((b) => {
      let r = base[0] + b * 9;
      let g = base[1] + b * 8;
      let bl = base[2] + b * 5;
      r = lerp(r, warm[0], warmK); g = lerp(g, warm[1], warmK); bl = lerp(bl, warm[2], warmK);
      r = lerp(r, cold[0], coldK); g = lerp(g, cold[1], coldK); bl = lerp(bl, cold[2], coldK);
      return `rgb(${r | 0},${g | 0},${bl | 0})`;
    }));
    this._topValence = val;
    return this._topFills;
  }

  /* ---------- 绘制 ---------- */

  _frame(now) {
    if (!this.running) return;
    this.raf = requestAnimationFrame(this._onFrame);
    const dtRaw = (now - this.last) / 1000;
    this.last = now;
    if (document.hidden) return;
    const dt = clamp(dtRaw || 0.016, 0.001, 0.05);
    this.frameMs = this.frameMs * 0.94 + dt * 1000 * 0.06;
    this.fps = 1000 / Math.max(1, this.frameMs);
    /* 软降级：棒顶细节在低帧率时自动关掉；帧率恢复后再打开（带迟滞）。 */
    if (this.frameMs > 24) this.lowFx = true;
    else if (this.frameMs < 19) this.lowFx = false;
    this.t += dt;
    this.ticks++;
    this._tick(dt);
    this._update(dt);
    this._draw();
  }

  _draw() {
    const ctx = this.ctx, W = this.w, H = this.h;
    if (!ctx) return;
    ctx.clearRect(0, 0, W, H);
    const heat = this.heat;

    /* 堆芯底部的环境光：颜色随 valence 走（暖金 ↔ 冷青），跳动时加一点闪光。 */
    let glowC = [168, 188, 116];
    if (this.valence > 0.5) {
      glowC = mixRGB(glowC, [214, 190, 124], (this.valence - 0.5) * 2 * 0.75);
    } else {
      glowC = mixRGB(glowC, [118, 158, 150], (0.5 - this.valence) * 2 * 0.75);
    }
    const glowA = 0.06 + 0.10 * heat + 0.20 * this.flash;
    const glowR = 21.5 * this.s;
    const glow = ctx.createRadialGradient(this.cx, this.cy, 0, this.cx, this.cy, glowR);
    glow.addColorStop(0, `rgba(${glowC[0]},${glowC[1]},${glowC[2]},${glowA})`);
    glow.addColorStop(0.55, `rgba(${glowC[0]},${glowC[1]},${glowC[2]},${glowA * 0.42})`);
    glow.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.save();
    ctx.globalCompositeOperation = 'lighter';
    ctx.fillStyle = glow;
    ctx.fillRect(this.cx - glowR, this.cy - glowR, glowR * 2, glowR * 2);
    ctx.restore();

    /* 按远→近分带、带内按颜色合批画 709 根控制棒。
       原 demo 是每根 5 次 fill + 5 次 stroke；709 根在 Canvas2D 上只有 ~21fps。
       分带合批后每带约 16 次 fill/stroke，视觉上保留前后遮挡，帧率回到可用区间。 */
    this._drawRods();

    /* 统一光照：暖色顶光 + 右下冷阴影 + 底部 AO，只作用在棒体像素上。 */
    this._lightRods(ctx, heat);
    /* 堆芯自身的光：沿底座外沿的 Cherenkov 环，随情绪/valence 变色变亮。 */
    this._rimGlow(ctx, heat, glowC);
    /* 很淡的整场 bloom：让堆芯像“在发光”，而不是一盏被照亮的模型。 */
    this._bloom(ctx, heat, glowC);

    if (TUNE.label) this._drawLabel(heat);
  }

  _bloom(ctx, heat, glowC) {
    const cx = this.cx, cy = this.cy, s = this.s;
    const g = ctx.createRadialGradient(cx, cy - 4 * s, 0, cx, cy - 4 * s, 20 * s);
    g.addColorStop(0, `rgba(${glowC[0]},${glowC[1]},${glowC[2]},${0.035 + 0.055 * heat})`);
    g.addColorStop(0.60, `rgba(${glowC[0]},${glowC[1]},${glowC[2]},${0.012 + 0.022 * heat})`);
    g.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.save();
    ctx.globalCompositeOperation = 'lighter';
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, this.w, this.h);
    ctx.restore();
  }

  /* 棒体光照 pass：source-atop 保证只给已经画出的棒/辉光上色，不影响透明区。
     光源设计：左上方暖色工业顶光（约 2700K）+ 右下冷色环境遮蔽 + 底部 AO。 */
  _lightRods(ctx, heat) {
    const W = this.w, H = this.h, cx = this.cx, cy = this.cy, s = this.s;
    ctx.save();
    ctx.globalCompositeOperation = 'source-atop';

    const kx = cx - 8 * s, ky = cy - 16 * s;
    const key = ctx.createRadialGradient(kx, ky, 0, kx, ky, 24 * s);
    key.addColorStop(0, `rgba(255,232,196,${0.10 + 0.08 * heat})`);
    key.addColorStop(0.55, `rgba(255,220,178,${0.045 + 0.035 * heat})`);
    key.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = key;
    ctx.fillRect(0, 0, W, H);

    const shade = ctx.createLinearGradient(cx - 14 * s, cy - 10 * s, cx + 14 * s, cy + 16 * s);
    shade.addColorStop(0, 'rgba(0,0,0,0)');
    shade.addColorStop(1, `rgba(2,8,12,${0.30 + 0.14 * heat})`);
    ctx.fillStyle = shade;
    ctx.fillRect(0, 0, W, H);

    const ao = ctx.createLinearGradient(0, cy + 2 * s, 0, cy + 22 * s);
    ao.addColorStop(0, 'rgba(0,0,0,0)');
    ao.addColorStop(1, 'rgba(0,0,0,.30)');
    ctx.fillStyle = ao;
    ctx.fillRect(0, 0, W, H);

    ctx.restore();
  }

  _rimGlow(ctx, heat, glowC) {
    ctx.save();
    ctx.globalCompositeOperation = 'lighter';
    this._ring(ctx, 16.45, `rgba(${glowC[0]},${glowC[1]},${glowC[2]},${0.060 + 0.100 * heat})`, 6 + 8 * heat);
    this._ring(ctx, 16.55, `rgba(${glowC[0]},${glowC[1]},${glowC[2]},${0.18 + 0.24 * heat})`, 2.0);
    ctx.restore();
  }

  /* 堆芯自身的静态光溢出：给走道/设备边缘一层橄榄色的底光；
     动态部分（随情绪变亮/变色的光）在核心层 _draw 的 glow 里叠加。 */
  _drawCoreSpill(ctx) {
    const cx = this.cx, cy = this.cy, s = this.s;
    const g = ctx.createRadialGradient(cx, cy, s * 8, cx, cy, s * 26);
    g.addColorStop(0, 'rgba(158,178,110,.070)');
    g.addColorStop(0.55, 'rgba(126,148,96,.030)');
    g.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.save();
    ctx.globalCompositeOperation = 'lighter';
    ctx.fillStyle = g;
    ctx.fillRect(cx - s * 26, cy - s * 26, s * 52, s * 52);
    ctx.restore();
  }

  _drawBase(ctx, heat) {
    const s = this.s;
    const rx = 16.4 * s, ry = 16.4 * s * this.tilt;

    /* 深色底盘：半透明，让壁纸还能透一点；边缘由外圈压住。 */
    ctx.save();
    ctx.beginPath();
    ctx.ellipse(this.cx, this.cy, rx, ry, 0, 0, TAU);
    ctx.clip();
    const bg = ctx.createRadialGradient(this.cx, this.cy, 0, this.cx, this.cy, rx);
    bg.addColorStop(0, 'rgba(9,15,10,0.90)');
    bg.addColorStop(0.72, 'rgba(5,10,7,0.82)');
    bg.addColorStop(1, 'rgba(4,8,6,0.48)');
    ctx.fillStyle = bg;
    ctx.fillRect(this.cx - rx, this.cy - ry, rx * 2, ry * 2);
    ctx.restore();

    /* 原 demo 的四道同心环 + 80 段外圈刻度（刻度按两种颜色合批）。 */
    this._ring(ctx, 16.4, '#060b07', 20);
    this._ring(ctx, 16.5, '#60684f', 2.4);
    this._ring(ctx, 16.0, '#9a9a72', 1.2);
    this._ring(ctx, 17.3, '#4a5238', 1.4);

    /* 内圈参考线（原 demo 没有；很淡，增加堆芯底板的结构感）。 */
    for (const r of [4, 8, 12]) {
      this._ring(ctx, r, `rgba(150,160,105,${0.05 + 0.05 * heat})`, 0.7);
    }

    for (let c = 0; c < 2; c++) {
      ctx.beginPath();
      for (let i = c; i < 80; i += 2) {
        const a = i / 80 * TAU;
        const a1 = a + 0.034;
        const p1 = this._project(Math.cos(a) * 15.7, Math.sin(a) * 15.7);
        const p2 = this._project(Math.cos(a1) * 15.7, Math.sin(a1) * 15.7);
        const p3 = this._project(Math.cos(a1) * 16.15, Math.sin(a1) * 16.15);
        const p4 = this._project(Math.cos(a) * 16.15, Math.sin(a) * 16.15);
        ctx.moveTo(p1[0], p1[1]);
        ctx.lineTo(p2[0], p2[1]);
        ctx.lineTo(p3[0], p3[1]);
        ctx.lineTo(p4[0], p4[1]);
        ctx.closePath();
      }
      ctx.fillStyle = c ? '#adac64' : '#484d31';
      ctx.fill();
    }
  }

  _ring(ctx, r, color, width) {
    ctx.beginPath();
    for (let i = 0; i <= 100; i++) {
      const a = i / 100 * TAU;
      const p = this._project(Math.cos(a) * r, Math.sin(a) * r);
      if (i) ctx.lineTo(p[0], p[1]);
      else ctx.moveTo(p[0], p[1]);
    }
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.stroke();
  }

  _drawRods() {
    const ctx = this.ctx, rods = this.rods, n = rods.length, s = this.s;
    const BANDS = 32;
    const per = Math.ceil(n / BANDS);
    const heat = this.heat;
    /* 只画朝向镜头的两个侧面（+x / +z）；另外两个是背面，原 demo 画了也会被盖住。
       每个面再按 amp 分 3 档亮度，避免所有侧面糊成一堵绿墙。 */
    const sideGroups = [1, 2];
    const sideBase = [
      [0, 0, 0],
      [70 + heat * 30, 82 + heat * 30, 56 + heat * 20],
      [42 + heat * 15, 52 + heat * 15, 36 + heat * 10],
      [0, 0, 0],
    ];
    const sideFills = [[], [], []];
    for (const g of sideGroups) {
      sideFills[g] = [];
      for (let bucket = 0; bucket < 3; bucket++) {
        const add = bucket * (g === 1 ? 11 : 7);
        sideFills[g].push(`rgb(${sideBase[g][0] + add | 0},${sideBase[g][1] + add | 0},${sideBase[g][2] + add | 0})`);
      }
    }

    for (let b = 0; b < BANDS; b++) {
      const start = b * per;
      const end = Math.min(n, start + per);
      if (start >= end) break;

      /* 1) 可见侧面按颜色合批（2 个面 × 3 档亮度） */
      for (const g of sideGroups) {
        for (let bucket = 0; bucket < 3; bucket++) {
          ctx.beginPath();
          let any = false;
          for (let i = start; i < end; i++) {
            const rod = rods[i];
            if (rod.sideBucket !== bucket) continue;
            this._addSide(ctx, rod, g);
            any = true;
          }
          if (!any) continue;
          ctx.fillStyle = sideFills[g][bucket];
          ctx.fill();
        }
      }

      /* 2) 竖直棱线：只描每根棒的三条可见竖边（b/c/d），
         不用完整面描边——后者在 709 根长柱时会把帧率打到 30fps。 */
      if (TUNE.strokes) {
        ctx.beginPath();
        for (let i = start; i < end; i++) {
          const rod = rods[i];
          const edges = [
            [rod.tb, rod.b0], [rod.tc, rod.c0], [rod.td, rod.d0],
          ];
          for (const [top, base] of edges) {
            ctx.moveTo(top[0], top[1]);
            ctx.lineTo(base[0], base[1]);
          }
        }
        ctx.strokeStyle = '#0a120a';
        ctx.lineWidth = 1.2;
        ctx.stroke();
      }

      /* 2.5) 受光棱线：左缘一条很淡的暖高光，让每根棒在暗面里仍有体积 */
      if (TUNE.strokes) {
        ctx.beginPath();
        for (let i = start; i < end; i++) {
          const rod = rods[i];
          const t0 = rod.td, b0 = rod.d0;
          ctx.moveTo(t0[0], t0[1]);
          ctx.lineTo(b0[0], b0[1]);
        }
        ctx.strokeStyle = `rgba(232,238,196,${0.05 + 0.09 * heat})`;
        ctx.lineWidth = 0.8;
        ctx.stroke();
      }

      /* 3) 顶面：按颜色合批填充；描边单独一条路径，每带只 stroke 一次。 */
      const groups = new Map();
      const topStroke = new Path2D();
      for (let i = start; i < end; i++) {
        const rod = rods[i];
        let path = groups.get(rod.topFill);
        if (!path) { path = new Path2D(); groups.set(rod.topFill, path); }
        this._addTop(path, rod);
        this._addTop(topStroke, rod);
      }
      for (const [fill, path] of groups) {
        ctx.fillStyle = fill;
        ctx.fill(path);
      }
      if (TUNE.strokes) {
        ctx.strokeStyle = '#232d20';
        ctx.lineWidth = 0.55;
        ctx.stroke(topStroke);
      }

      /* 4) 棒顶中心小圆（可选细节；低帧率时自动关） */
      if (TUNE.details && !this.lowFx && s >= 14) {
        ctx.beginPath();
        for (let i = start; i < end; i++) {
          const rod = rods[i];
          const p = rod.pc;
          const dy = -rod.h * s;
          ctx.moveTo(p[0] + s * 0.12, p[1] + dy);
          ctx.ellipse(p[0], p[1] + dy, s * 0.12, s * 0.12 * this.tilt, 0, 0, TAU);
        }
        ctx.fillStyle = '#404735';
        ctx.fill();
      }
    }
  }

  _addSide(target, rod, g) {
    const a = rod.ta, b = rod.tb, c = rod.tc, d = rod.td;
    let pts;
    if (g === 0) pts = [a, rod.a0, rod.b0, b];
    else if (g === 1) pts = [b, rod.b0, rod.c0, c];
    else if (g === 2) pts = [c, rod.c0, rod.d0, d];
    else pts = [d, rod.d0, rod.a0, a];
    target.moveTo(pts[0][0], pts[0][1]);
    target.lineTo(pts[1][0], pts[1][1]);
    target.lineTo(pts[2][0], pts[2][1]);
    target.lineTo(pts[3][0], pts[3][1]);
    target.closePath();
  }

  _addTop(target, rod) {
    const a = rod.ta, b = rod.tb, c = rod.tc, d = rod.td;
    target.moveTo(a[0], a[1]);
    target.lineTo(b[0], b[1]);
    target.lineTo(c[0], c[1]);
    target.lineTo(d[0], d[1]);
    target.closePath();
  }

  _drawLabel(heat) {
    const ctx = this.ctx;
    const x = this.cx - 16.4 * this.s * 0.92;
    const y = this.cy + 16.4 * this.s * this.tilt * 0.92;
    ctx.save();
    ctx.font = `${Math.max(8, Math.round(9.5 * Math.min(1.2, this.s / 20)))}px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`;
    ctx.textBaseline = 'middle';
    ctx.fillStyle = `rgba(168,180,128,${0.10 + 0.18 * heat})`;
    ctx.fillText('CORE 01 · 声场反应堆 · 709 CHANNELS', x, y);
    ctx.restore();
  }

  /* ---------- 诊断面 ---------- */

  state() {
    return {
      enabled: this.running,
      heat: +this.heat.toFixed(3),
      target: +this.target.toFixed(3),
      rods: this.rods.length,
      airborne: this.rods.filter((r) => r.h > 0.6).length,
      maxH: +this.rods.reduce((m, r) => Math.max(m, r.h), 0).toFixed(2),
      fps: Math.round(this.fps),
      t: +this.t.toFixed(2),
      ticks: this.ticks,
      w: this.w, h: this.h, s: +this.s.toFixed(1),
      cx: Math.round(this.cx), cy: Math.round(this.cy),
      lowFx: this.lowFx,
      reduced: this.reduced,
      valence: +this.valence.toFixed(3),
      jumpFreq: +this.jumpFreq.toFixed(3),
      jumpAmp: +this.jumpAmp.toFixed(3),
      jumpArea: +this.jumpArea.toFixed(3),
      jumpCount: this.jumpCount,
      lastJumpN: this.lastJumpN,
      pulses: this.pulses,
      motionOn: this.motionOn,
    };
  }

  tune(o) {
    if (o && typeof o === 'object') {
      for (const k in o) if (k in TUNE) TUNE[k] = o[k];
      if ('zoom' in o || 'dprCap' in o || 'renderScale' in o) this.resize();
      if ('details' in o) this.lowFx = false;
    }
    return { ...TUNE };
  }
}

/* ════════════════════════════════════════════════════════════════════
   插件注册
   ════════════════════════════════════════════════════════════════════ */
let scene = null;
function getScene() {
  if (!scene) scene = new ReactorCoreScene();
  return scene;
}

export const reactorCoreEffect = {
  id: 'reactorcore',
  type: 'ontop',
  name: '堆芯',
  onEnable() {
    const ok = getScene().enable();
    window.__reactorcore = {
      enabled: () => !!scene && scene.running,
      state: () => (scene ? scene.state() : null),
      tune: (o) => (scene ? scene.tune(o) : null),
      /* 调试钩子：强制整体激烈度 / 强制一帧情绪轴。
         debugHeat(0..1) 看「平静 ↔ 暴走」；debugAxes(axes) 看径向谱形；
         传 null 恢复真实 window.__mood。 */
      debugHeat: (v) => {
        if (!scene) return null;
        scene.forcedHeat = (v == null) ? null : clamp01(v);
        return scene.forcedHeat;
      },
      debugAxes: (axes) => {
        if (!scene) return null;
        scene.forcedAxes = (axes && typeof axes === 'object') ? axes : null;
        scene._axesSum = null;
        return scene.forcedAxes;
      },
      pulse: () => {
        if (!scene) return 0;
        scene.pulse = 1;
        scene.waveR = 0;
        return 1;
      },
      jump: () => {
        if (!scene) return 0;
        scene._jumpPulse();
        return scene.lastJumpN;
      },
      sample: () => (scene ? scene.rods.slice(0, 6).map((r) => ({
        x: r.x, z: r.z, r: r.r, h: +r.h.toFixed(3), v: +r.v.toFixed(2),
        amp: +r.amp.toFixed(3),
      })) : null),
      /* 按半径分 5 圈的平均高度：验证「情绪谱 → 径向起伏」用 */
      rings: () => {
        if (!scene) return null;
        const b = [0, 1, 2, 3, 4].map(() => ({ n: 0, h: 0 }));
        for (const r of scene.rods) {
          const i = Math.min(4, Math.floor(r.r / 3));
          b[i].n++;
          b[i].h += r.h;
        }
        return b.map((x) => ({ n: x.n, h: +(x.h / Math.max(1, x.n)).toFixed(2) }));
      },
    };
    return ok;
  },
  onDisable() {
    if (scene) scene.disable();
  },
};

export function install() {
  return registerEffect(reactorCoreEffect);
}

export const _diag = {
  TUNE,
  get scene() { return scene; },
  axesIntensity,
  profileAt,
  AXES_ORDER,
};


