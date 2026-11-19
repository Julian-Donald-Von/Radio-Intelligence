/* newui/js/runtime.js — 老 UI 视觉与交互的 ESM runtime（自动生成，勿手改）。
 * 生成脚本：.workbuddy/newui_20261118/build_runtime.py
 * DOM+CSS 与老 UI 逐字一致（newui/index.html）；本文件按原加载顺序拼接 classic scripts，
 * 保留共享顶层作用域（st/toast/render 跨模块互见——这是老 UI 的既有契约）。
 * 水体 ui-water.js 不在此列（冻结核心、用户已结案）；#water canvas 保留但不渲染。
 * ESM 边界在文件末尾：export default { ... }。
 */
export const __BUILD = '2026-11-18';

/* ══════════ 全局兜底：老 UI 直接引用 window.__water 等，缺水体时给空实现 ══════════ */
if (typeof window.__water === 'undefined') {
  window.__water = {
    setMusicEvents(){}, setSemantics(){}, setEnabled(){},
    tune(){}, tuneState(){ return { water: false }; },
  };
}

/* ═══════ liquid.js (全局液态背景) ═══════ */
/* ══════════════════════════════════════════════════════════════
   liquid.js —— WebGL 液态折射（移植自 ui/ai-radio-player.html L1635-1800）

   为什么不用 CSS/SVG 滤镜（原文件 L1627 注释）：
     旧方案 #bgimg{filter:url(#liquid)} 把 SVG reference filter 挂在**全屏元素**上，
     窗口失焦/获焦/最大化都会触发 Chromium 整屏重栅格化 → 肉眼可见闪烁。
   新方案：#bgliquid 画布用**片元着色器逐像素**做同等折射，每帧只画一个全屏 quad。

   Shader 三段与 SVG 参数严格对齐：
     feTurbulence(fractalNoise 0.006/0.008, 2 octaves) → vnoise 值噪声 + fractal() 分形
     feDisplacementMap(scale 9)                        → disp = (vec2(n1,n2)-0.5)*uScale
     feOffset ±1px + feBlend screen ×2（RGB 色差）      → 3 次采样 screen 混合
   ══════════════════════════════════════════════════════════════ */
window.LiquidBg = (function () {
  var canvas = null, gl = null, prog = null, tex = null;
  /* ⚠ 全局背景折射**默认关闭**：用户明确要求弯曲只发生在 dock 内部，
     全局弯曲会扭曲整屏。dock 的局部透镜折射由 liquid_glass.js 负责
     （backdrop-filter: url(#lg-dock)，位移只作用于元素背后）。
     需要背景整体流动感时调用 LiquidBg.setEnabled(true) 开启。 */
  var _ready = false, _enabled = false, _loopOn = false, _lastDraw = 0;
  var _imgW = 2, _imgH = 2, _u = {};

  var VERT = "attribute vec2 aPos; void main(){ gl_Position = vec4(aPos, 0.0, 1.0); }";

  var FRAG = [
    "precision mediump float;",
    "uniform sampler2D uTex;",
    "uniform vec2 uRes;      /* 画布 px */",
    "uniform vec2 uImgRes;   /* 壁纸 px */",
    "uniform vec3 uCover;    /* x=cover缩放 y=偏移x z=偏移y (画布px, 左上原点) */",
    "uniform float uScale;   /* 位移幅度 px (SVG scale=9) */",
    "uniform float uChroma;  /* 色差偏移 px */",
    "uniform float uTime;",
    "uniform float uAdapt;   /* bg-adapt: 0/1 */",
    "float hash(vec2 p){ p = fract(p * vec2(123.34, 456.21));",
    "  p += dot(p, p + 45.32); return fract(p.x * p.y); }",
    "float vnoise(vec2 p){ vec2 i = floor(p), f = fract(p);",
    "  vec2 w = f * f * (3.0 - 2.0 * f);",
    "  float a = hash(i), b = hash(i + vec2(1.0, 0.0));",
    "  float c = hash(i + vec2(0.0, 1.0)), d = hash(i + vec2(1.0, 1.0));",
    "  return mix(mix(a, b, w.x), mix(c, d, w.x), w.y); }",
    "float fractal(vec2 p){ /* 2 octaves ≈ feTurbulence numOctaves=2 */",
    "  return vnoise(p) * 0.65 + vnoise(p * 2.03 + 17.7) * 0.35; }",
    "vec3 scr(vec3 a, vec3 b){ return 1.0 - (1.0 - a) * (1.0 - b); }",
    "void main(){",
    "  vec2 px = vec2(gl_FragCoord.x, uRes.y - gl_FragCoord.y); /* 转左上原点 */",
    "  vec2 f = vec2(0.006, 0.008);          /* 与 SVG baseFrequency 一致 */",
    "  float t = uTime;",
    "  float n1 = fractal(px * f + vec2(t * 0.035, t * 0.02));",
    "  float n2 = fractal(px * f.yx * 1.13 + vec2(-t * 0.025, t * 0.03) + 31.4);",
    "  vec2 disp = (vec2(n1, n2) - 0.5) * uScale;",
    "  vec2 pI = (px + disp - uCover.yz) / uCover.x;   /* 画布px → 壁纸px */",
    "  vec2 uv = pI / uImgRes;",
    "  vec3 base  = texture2D(uTex, uv).rgb;",
    "  vec3 cPlus = texture2D(uTex, (pI + vec2(uChroma, 0.0)) / uImgRes).rgb;",
    "  vec3 cMinus= texture2D(uTex, (pI - vec2(uChroma, 0.0)) / uImgRes).rgb;",
    "  vec3 col = scr(scr(base, cPlus), cMinus);",
    "  if (uAdapt > 0.5){ float l = dot(col, vec3(0.2126, 0.7152, 0.0722));",
    "    col = mix(vec3(l), col, 0.92) * 0.68; }   /* saturate(.92) brightness(.68) */",
    "  gl_FragColor = vec4(col, 1.0);",
    "}"
  ].join("\n");

  function initGL() {
    if (gl !== null) return gl;            /* 已初始化（含失败标记 false） */
    canvas = document.getElementById('bgliquid');
    if (!canvas) { gl = false; return false; }
    try {
      gl = canvas.getContext('webgl', { preserveDrawingBuffer: true, antialias: false })
        || canvas.getContext('experimental-webgl', { preserveDrawingBuffer: true });
    } catch (e) { gl = false; }
    if (!gl) return false;

    function sh(type, src) {
      var s = gl.createShader(type);
      gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) { return null; }
      return s;
    }
    var vs = sh(gl.VERTEX_SHADER, VERT), fs = sh(gl.FRAGMENT_SHADER, FRAG);
    if (!vs || !fs) { gl = false; return false; }
    prog = gl.createProgram();
    gl.attachShader(prog, vs); gl.attachShader(prog, fs); gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) { gl = false; return false; }
    gl.useProgram(prog);

    var buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    var loc = gl.getAttribLocation(prog, 'aPos');
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);

    ['uTex', 'uRes', 'uImgRes', 'uCover', 'uScale', 'uChroma', 'uTime', 'uAdapt']
      .forEach(function (n) { _u[n] = gl.getUniformLocation(prog, n); });

    tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.uniform1i(_u.uTex, 0);

    window.addEventListener('resize', function () { try { resize(); } catch (e) { } });
    return true;
  }

  function resize() {
    if (!initGL() || !canvas) return;
    var dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    var w = Math.max(2, Math.floor(canvas.clientWidth * dpr));
    var h = Math.max(2, Math.floor(canvas.clientHeight * dpr));
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    draw(performance.now() / 1000);
  }

  function draw(t) {
    if (!gl || !_ready || !canvas) return;
    var cw = canvas.width, ch = canvas.height;
    gl.viewport(0, 0, cw, ch);
    /* cover 布局（左上原点）：s = max(cw/iw, ch/ih)，居中偏移 */
    var s = Math.max(cw / _imgW, ch / _imgH);
    var ox = (cw - _imgW * s) / 2, oy = (ch - _imgH * s) / 2;
    gl.useProgram(prog);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.uniform2f(_u.uRes, cw, ch);
    gl.uniform2f(_u.uImgRes, _imgW, _imgH);
    gl.uniform3f(_u.uCover, s, ox, oy);
    gl.uniform1f(_u.uScale, 9.0);      /* 对齐 SVG feDisplacementMap scale=9 */
    gl.uniform1f(_u.uChroma, 1.0);     /* 对齐 SVG feOffset ±1px */
    gl.uniform1f(_u.uTime, t);
    gl.uniform1f(_u.uAdapt,
      document.body.classList.contains('bg-adapt-active') ? 1 : 0);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  }

  function tick(ts) {
    if (!_loopOn) return;
    requestAnimationFrame(tick);
    if (ts - _lastDraw < 33) return;   /* ~30fps 足够，省电省 GPU */
    _lastDraw = ts;
    draw(ts / 1000);
  }

  function apply_() {
    var active = !!(gl && _ready && _enabled);
    if (canvas) canvas.classList.toggle('on', active);
    var img = document.getElementById('bgimg');
    if (img) img.classList.toggle('liquid-canvas', active);  /* active 时 SVG 滤镜下岗 */
    if (active && !_loopOn) { _loopOn = true; requestAnimationFrame(tick); }
    if (!active) _loopOn = false;
    /* ⚠ 画布 .on 之前是 display:none（clientWidth=0），激活后须重算尺寸再画 */
    if (active) requestAnimationFrame(function () { resize(); });
  }

  /* ═══ R78：LiquidBg.setSource 只喂自己的 WebGL 纹理，不再写 #bgimg ═══
     历史遗留：setSource 的 onload 里顺手 `img.src = url; has-bg` —— 这在老
     架构（bgimg 即壁纸位）成立；R57-1 后 #bgimg 由封面/皮肤逻辑（ui-core
     render / applyBrandSkin / ensureDefaultSkin）统一接管，这里再写就**越权**：
       · ui-view.initLiquidBg 启动时喂程序化渐变 dataURL → 抢先占住 bgimg.src，
         使 ensureDefaultSkin 的 `if (!img.getAttribute('src'))` 永远不触发
         → 默认 logo 壁纸首帧不显示（用户报"开屏纯色画布"）。
       · 切歌 render() 内 LiquidBg.setSource(st.cover) → 无视 wp-active，
         把真实封面直接写进 #bgimg → 用户选的「默认」壁纸切歌即被覆盖。
     #bgliquid 的折射纹理与 #bgimg 皮肤位解耦：这里只上传纹理。 */
  return {
    setSource: function (url) {
      if (!url) { _ready = false; apply_(); return; }
      if (!initGL()) { apply_(); return; }   /* WebGL 不可用 → CSS 滤镜兜底 */
      var im = new Image();
      im.crossOrigin = 'anonymous';
      im.onload = function () {
        _imgW = im.naturalWidth || 2; _imgH = im.naturalHeight || 2;
        try {
          gl.bindTexture(gl.TEXTURE_2D, tex);
          gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA,
            gl.UNSIGNED_BYTE, im);   /* file:// 在此抛 SecurityError */
          _ready = true;
        } catch (e) { _ready = false; }      /* 污染纹理 → 回退 CSS 滤镜 */
        apply_();
      };
      im.onerror = function () { _ready = false; apply_(); };
      im.src = url;
    },
    setEnabled: function (on) { _enabled = !!on; apply_(); },
    isActive: function () { return !!(gl && _ready && _enabled); }
  };
})();

/* ═══════ liquid_glass.js (液态玻璃透镜) ═══════ */
/* ══════════════════════════════════════════════════════════════
   liquid_glass.js —— 真·液态玻璃：元素**局部**位移图折射
   （完整移植自 ui/ai-radio-player.html L5925-6089）

   ⚠ 与全局背景折射（liquid.js）的区别，别混用：
     · liquid.js        = 全局背景的 WebGL 水纹折射（背景流动感）
     · liquid_glass.js  = **元素背后**的透镜折射（dock/按钮的玻璃弯曲感）
   本模块用的是 backdrop-filter: url(#svgFilter) —— 只对元素**背后**的内容
   做位移，所以弯曲被严格限制在元素轮廓内（用户要的"只在 dock 里面"）。

   算法三步：
     1) buildMap() 用 SDF 生成圆角矩形「透镜」位移图
        · sdf() 圆角矩形有符号距离场
        · 边缘 rim 位移最强、中心为 base（放大透镜）
        · R = x 分量，G = y 分量，128 = 中性；A=0 表示玻璃外不位移
     2) makeFilter() 拼 feImage(位移图) + feDisplacementMap
     3) applyGlass() 挂到元素 backdrop-filter，ResizeObserver 自动重建

   仅 Chromium 支持 backdrop-filter: url(#x)；其余浏览器自动跳过（回退 CSS blur）。
   ══════════════════════════════════════════════════════════════ */
window.LiquidGlass = (function () {
  var supportsLG = !!(window.CSS && CSS.supports && CSS.supports('backdrop-filter', 'url(#x)'));
  if (!supportsLG) {
    return { supported: false, apply: function () { }, applyAll: function () { } };
  }

  var SVGNS = 'http://www.w3.org/2000/svg';
  var XLINK = 'http://www.w3.org/1999/xlink';

  // 共享 defs 容器（隐藏）
  var defs = document.getElementById('lg-defs');
  if (!defs) {
    var svg = document.createElementNS(SVGNS, 'svg');
    svg.setAttribute('id', 'lg-defs');
    svg.setAttribute('width', '0');
    svg.setAttribute('height', '0');
    svg.style.position = 'absolute';
    svg.style.width = '0';
    svg.style.height = '0';
    defs = document.createElementNS(SVGNS, 'defs');
    svg.appendChild(defs);
    document.body.appendChild(svg);
  }
  var defsEl = defs.querySelector('defs') || defs;

  function smoothstep(a, b, x) {
    x = Math.max(0, Math.min(1, (x - a) / (b - a)));
    return x * x * (3 - 2 * x);
  }
  function clamp128(v) { return Math.max(0, Math.min(255, v | 0)); }
  function sdf(px, py, bx, by, r) {
    var qx = Math.abs(px) - bx + r, qy = Math.abs(py) - by + r;
    var ax = Math.max(qx, 0), ay = Math.max(qy, 0);
    return Math.min(Math.max(qx, qy), 0) + Math.hypot(ax, ay) - r;
  }

  // 生成圆角矩形「透镜」位移图：边缘 rim 处位移最强、中心为 base（放大透镜）
  function buildMap(w, h, radius, thickness, scale, base) {
    w = Math.max(2, w | 0); h = Math.max(2, h | 0);
    var cv = document.createElement('canvas'); cv.width = w; cv.height = h;
    var ctx = cv.getContext('2d');
    var img = ctx.createImageData(w, h);
    var data = img.data;
    var bx = w / 2, by = h / 2, r = Math.min(radius, bx, by);
    base = Math.max(0, Math.min(0.9, base || 0));
    for (var y = 0; y < h; y++) {
      for (var x = 0; x < w; x++) {
        var lx = x - bx, ly = y - by;
        var d = sdf(lx, ly, bx, by, r);
        var i = (y * w + x) * 4;
        if (d > 0) { data[i] = 128; data[i + 1] = 128; data[i + 2] = 128; data[i + 3] = 0; continue; }
        var edgeDist = -d;
        var t = Math.min(1, edgeDist / thickness);
        var rim = 1 - smoothstep(0, 1, t);          // 1 在边缘 -> 0 中心
        var len = Math.hypot(lx, ly) || 1;
        var dx = lx / len, dy = ly / len;
        var mag = base + (1 - base) * rim;
        data[i] = clamp128(128 - dx * mag * 127);   // R 固定全幅编码
        data[i + 1] = clamp128(128 - dy * mag * 127); // G
        data[i + 2] = 128;
        data[i + 3] = 255;
      }
    }
    ctx.putImageData(img, 0, 0);
    return cv.toDataURL();
  }

  var registry = [];

  // feImage(位移图) + feDisplacementMap
  function makeFilter(fid) {
    var f = document.createElementNS(SVGNS, 'filter');
    f.setAttribute('id', fid);
    f.setAttribute('x', '0%'); f.setAttribute('y', '0%');
    f.setAttribute('width', '100%'); f.setAttribute('height', '100%');
    f.setAttribute('color-interpolation-filters', 'sRGB');
    var fi = document.createElementNS(SVGNS, 'feImage');
    fi.setAttribute('result', 'map');
    // 参考算法：feImage 不设 x/y/width/height，避免 Chromium 对带尺寸的 dataURL 加载失败
    var disp = document.createElementNS(SVGNS, 'feDisplacementMap');
    disp.setAttribute('in', 'SourceGraphic');
    disp.setAttribute('in2', 'map');
    disp.setAttribute('scale', '1');
    disp.setAttribute('xChannelSelector', 'R');
    disp.setAttribute('yChannelSelector', 'G');
    f.appendChild(fi); f.appendChild(disp);
    defsEl.appendChild(f);
    return { fi: fi, disp: disp };
  }

  // 按当前真实尺寸重绘位移图；未布局元素（display:none）尺寸为 0，跳过待其显形后重建
  function buildMapFor(o) {
    var w = Math.max(2, o.el.clientWidth | 0), h = Math.max(2, o.el.clientHeight | 0);
    if (w <= 2 && h <= 2) return;
    var url = buildMap(w, h, o.radius, o.thickness, o.scale, o.base);
    function setHref(u) {
      o.fi.setAttribute('href', u);
      o.fi.setAttributeNS(XLINK, 'href', u);
    }
    setHref(url);
    // Chromium 对 dataURL 的 feImage 偶发加载失败 → 延迟重设两次兜底
    setTimeout(function () { setHref(url); }, 80);
    setTimeout(function () { setHref(url); }, 300);
  }

  function applyGlass(el, radius, thickness, scale, blur, base) {
    if (!el) return;
    var fid = 'lg-' + (el.id || (el.id = 'lg' + Math.random().toString(36).slice(2)));
    var f = document.getElementById(fid);
    var fi, disp;
    if (!f) {
      var parts = makeFilter(fid);
      fi = parts.fi; disp = parts.disp;
    } else {
      fi = f.querySelector('feImage');
      disp = f.querySelector('feDisplacementMap');
    }
    disp.setAttribute('scale', String(scale));
    var vf = 'url(#' + fid + ')';
    el.style.backdropFilter = vf;
    el.style.webkitBackdropFilter = vf;
    var o = { el: el, fi: fi, radius: radius, thickness: thickness, scale: scale, base: base || 0 };
    registry.push(o);
    buildMapFor(o);
    // 尺寸变化（display:none→显示、窗口缩放）时自动重建位移图
    if (typeof ResizeObserver !== 'undefined') {
      o.ro = new ResizeObserver(function () { buildMapFor(o); });
      o.ro.observe(el);
    }
  }

  // [选择器, 圆角半径, bevel厚度, 位移强度, 叠加模糊, 中心基础位移(0~0.9)]
  // 圆形按钮半径取 ≥ 半边长 -> 完整圆形透镜
  var TARGETS = [
    ['#dock', 44, 26, 55, 0, 0],           // 控制栏（药丸）液态玻璃
    ['#search-box', 23, 14, 30, 0, 0],     // 搜索栏
    ['#rec-style', 20, 12, 26, 0, 0],      // 推荐风格框
    // R44：把 orbit 按钮 + 侧边面板也接入透镜
    // · 圆形按钮半径=54（108/2），圆角同半径 → 完整圆形透镜
    // · bevel=28 / scale=160（中等偏强，原 UI 「玻璃球」观感）
    ['.orbit', 54, 28, 160, 0, 0.15],      // Orbit 玻璃球
    ['#side-panel', 20, 18, 65, 0, 0],     // 侧边面板
    // ⚠ R53：AI 口播栏从"纯 blur 暗卡"升级为**暗色液态玻璃**（用户原话"类似 dock
    //   栏的算法加上调色"）——接入与 dock 同源透镜，圆角 20、温和位移 38。
    //   它只在有口播文案时 .show（pointer-events 由 .show 控制），平时 display 常驻
    //   但 opacity 0；透镜随内容尺寸由 ResizeObserver 自动重建，不会闪烁。
    ['#aibar', 20, 14, 38, 0, 0],          // AI 口播栏（R53 玻璃化）
    // R54：左下设置齿轮 + 换肤衣架（54px 圆钮 → 完整圆形透镜，同 .orbit 参数）
    ['#settings-gear', 27, 14, 80, 0, 0.12],   // 设置齿轮
    ['#theme-hanger', 27, 14, 80, 0, 0.12]     // 换肤衣架
    /* ⚠ 栏目面板（左侧"漫游/曲库/歌单"提示）**刻意不接透镜**（R46-5 用户要求）：
       它是悬在背景海报上的纯文字信息层，加 backdrop-filter 会把背后的歌词海报
       折射得发花、可读性变差；又因为它随鼠标靠近淡入，折射面反复重建也容易闪。
       它在 index.html 里也没有任何 background / border，本就是无底板的排版。 */
  ];

  function run() {
    TARGETS.forEach(function (cfg) {
      var sel = cfg[0], r = cfg[1], th = cfg[2], s = cfg[3], b = cfg[4], base = cfg[5] || 0;
      if (sel.charAt(0) === '.') {
        var els = [].slice.call(document.querySelectorAll(sel));
        els.forEach(function (el) { applyGlass(el, r, th, s, b, base); });
      } else {
        applyGlass(document.querySelector(sel), r, th, s, b, base);
      }
    });
  }

  function start() { run(); }

  if (document.readyState === 'complete') setTimeout(start, 60);
  else window.addEventListener('load', function () { setTimeout(start, 60); });

  return {
    supported: true,
    apply: applyGlass,
    applyAll: run,
    buildMap: buildMap
  };
})();

/* ═══════ poster.js (歌词海报) ═══════ */
/* ══════════════════════════════════════════════════════════════
   poster.js —— 背景「歌词海报」+ 皮肤层
   （完整移植自 ui/ai-radio-player.html L1494-1558 drawPoster / drawSkinBase）

   原 UI 的设计（L690-692 / L898-901）：
     · **没有独立的歌词卡片/歌词栏**（#lyric-panel 已被移除）
     · 歌词统一画在**背景海报**上：以当前曲目封面为底，叠加
       曲名/歌手 + 当前行 ±1 邻行渐变
     · 海报取色同时驱动粒子配色与节拍响应
     · 显隐由 #d-lyric「词」按钮切 state.lyricOn
     · 壁纸模式：poster(歌词海报，默认) | flow(流动水样)

   构图比例（严格对齐原实现 + R62 修正）：
     曲名   y = H*0.185             字号 H*0.034  700 白 .97（微软雅黑）字距 0
     歌手   y = H*0.185 + H*0.052   字号 H*0.019  300 白 .55（雅黑 Light）字距 H*0.001
     ⚠ R62：① 曲名与歌手垂直间距加大（歌手 y 增量 H*0.034 → H*0.052，避免
     R61 曲名加大后两行贴近/视觉粘连）；
     ② **曲名 letterSpacing 显式复位 '0px'** —— canvas painter state 跨帧泄漏：
     R61 画歌手设了宽字距后不还原，曲名（先画）被上一帧残留字距污染 → 用户
     "歌名字距过宽"的观感即来自此（曲名从未主动设字距却显示加宽）；
     ③ 歌手字距 R61 的 H*0.004 → **H*0.001**（再减半，用户反馈字距略宽）。
     歌词   y = H*0.52 + k*size*2   当前行 H*0.0408（R62：原 H*0.048 × 0.85 缩小）
                                    邻  行 H*0.022（原 .026 再调小）白 .20（R62 更淡）
     ⚠ R60：全部文字零阴影（曲名/歌手/歌词都不要字阴影）
     ⚠ R61：曲名再大一点（0.03→0.034）；歌手加字间距（美观），
             canvas letterSpacing 仅在受支持浏览器生效，否则自动忽略。
     ⚠ R62：① drawPoster 开头 **ctx.clearRect 整幅清屏** —— 此前画布从不清屏，
       每帧半透明白字叠画 = 歌词/曲名/歌手互相"堆叠 + 毛边残影"的直接根源；
       ② 歌词改**平滑滚动**（不做 R59 的"一帧硬切行"），行随进度上下滚动。 
   ══════════════════════════════════════════════════════════════ */
window.Poster = (function () {
  var cv = null, ctx = null, W = 0, H = 0;
  var lyricLines = [], lyricCur = -1;
  /* ═══ 歌词视图（R49-22 重做）═══
     · **恒显 3 句**（上一句 / 当前句 / 下一句）—— 绘制窗口严格 center±1
       （滑动过渡瞬间最多 4 行重叠：新行淡入 + 旧行淡出，保证不突兀跳变）
     · 浏览**只由滚轮驱动**（wheelLyric ±1）—— 用户原话"通过滚轮调节，
       而不是鼠标放上去就能调节"：悬停**不**换行、**不**放大、不重置计时
     · 滚轮停在非播放行 **2 秒未操作 → 自动回正**到正在播放的行
     · 行切换 = viewPos 浮点缓动 → **滑动过渡**，不是突兀一跳
     · 无任何背板 / 行高亮块 / 提示文字 */
  var viewPos = 0;            /* 视图中心行（浮点，平滑滚动向目标行缓动） */
  var viewPosInit = false;
  var hoverIdx = -1;          /* 滚轮浏览所指的行，-1 = 无（跟随播放行） */
  var hoverAt = 0;            /* 滚轮换行时刻（2 秒回正计时） */
  var HOVER_RETURN_MS = 2000;
  /* ═══ R62：歌词平滑滚动速度 ═══
     R59 曾改成 viewPos=target「一帧硬切行」来消除拖影 —— 但用户要求的是
     **滚动式过渡**（播过的上滚出、正在播的下滚入），硬切没有滚动感。
     新实现：viewPos 向 target 做**指数缓动**（每帧逼近，速度与距离成正比）。
       距离大（整行切换）→ 起始快、收尾慢 → 平滑滑入无突兀；
       正在播放时逐帧推进 → 行缓慢随歌词"滚"过中心，而非跳变。
     LYRIC_EASE 越大滚得越快；0.16 约 200~300ms 完成一行切换。
     ⚠ 帧率感知：缓动在 rAF 主循环每帧推进（ui-init.js mainLoop 驱动
     Poster.loop），本地插值 st.position 也每帧驱动 seekLyric —— 行推进
     与实际播放进度解耦（seekLyric 只改 target，不动 viewPos 本身），
     因此不会出现"卡顿一下跳一行"（R59 硬切的旧观感）。 */
  /* ═══ R62：行槽距随 0.85× 小字号同步收窄（原 2× H*0.048 → 2× H*0.0408），
     否则滚动时行距过大、歌词显得稀。下限 30px 防过密。 */
  function LH() { return Math.max(30, H * 0.082); }   /* ≈ 2 × 焦点字号(0.0408) */
  function viewTarget() {
    if (hoverIdx >= 0) return hoverIdx;
    return lyricCur < 0 ? 0 : lyricCur;
  }
  var state = { lyricOn: false, bgMode: 'poster' };
  var FONT = '"Microsoft YaHei","PingFang SC",system-ui,sans-serif';
  var lyricUrl = '';

  function init() {
    cv = document.getElementById('poster-canvas');
    if (!cv) return false;
    ctx = cv.getContext('2d');
    resize();
    window.addEventListener('resize', resize);
    requestAnimationFrame(loop);
    return true;
  }

  function resize() {
    if (!cv) return;
    var dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    W = Math.max(2, Math.floor(cv.clientWidth * dpr));
    H = Math.max(2, Math.floor(cv.clientHeight * dpr));
    if (cv.width !== W || cv.height !== H) { cv.width = W; cv.height = H; }
  }

  /* ---- 皮肤层：画布**全透明**（R59） ----
     ⚠ R57-1 已把壁纸交给 #bgimg（封面/自定义壁纸，锐利或亚克力 blur 由 CSS 控制）；
     R58-4 曾保留「底部渐隐 + 顶部压暗」保证文字可读 —— 但用户实测这段
     rgba(6,5,16,…) 渐隐/压暗在亮壁纸上叠出**一整块灰**（"播放音乐时一大块灰"）。
     R59：彻底去掉画布上的一切蒙层 —— 画布只画文字，背景 100% 由 #bgimg 承担。
     R61：连「海报底氛围光」posterBuf 一并移除（否则锐利/亚克力都被叠一层
     雾状模糊 → "点无效果和点亚克力看起来一样"）。文字可读性由 #bgimg 的
     灰调（R61 暗化）+ 文字本身透明度层级保证。 */
  function drawSkinBase() { /* no-op：不再画任何蒙层 */ }

  function drawPoster(now) {
    drawSkinBase();
    /* ═══ R62（堆叠/毛边真凶修复）：画布**每帧整幅清屏** ═══
       ⚠⚠ 此前 drawPoster 从不 clearRect —— 画布是透明层，fillText 叠画
       半透明白字，上一帧的字永远擦不掉：切行后旧行残影仍在 → 观感
       "下一句歌词堆到上一句上面" + 所有文字毛边/发虚（半透明叠半透明）。
       R59「干脆切行」掩盖了它（硬切整行互换，残影不明显），一旦改回平滑
       滚动就必须先清屏，否则滚动轨迹糊成一条带。 */
    ctx.clearRect(0, 0, W, H);
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';

    /* 曲名 / 歌手 */
    var song = window.PosterState && window.PosterState.song;
    if (song) {
      /* 曲名 / 歌手
         ⚠ R60（用户原话回退）："歌曲和歌手的那个格式怎么还给改了，要变回去，
         不要字的阴影；歌曲名称的字号略微大一点，还是之前那种纯白色的即可；
         歌手名字号小一点、略微细一点 —— 比如标题用微软雅黑，歌手名用雅黑 Light。"
         · 曲名：微软雅黑 700、H*0.034（R61 在 0.03 基础上再大一点）、纯白 .97、**零阴影**；
         · 歌手：'Microsoft YaHei Light' 300、H*0.019（小于曲名、更细）、白 .55、
           字距 H*0.001（R62 再缩：R61 的 H*0.004 用户觉得"过宽"）、零阴影。
         ⚠ R62：曲名与歌手的垂直间距拉大 —— 曲名 y=H*0.185，歌手
         y=H*0.185+H*0.052（原 H*0.034 是 R60 曲名小字号时代的增量，曲名
         加大后两行几乎贴在一起 → "挨得太近"）。 */
      ctx.fillStyle = 'rgba(255,255,255,.97)';
      ctx.font = '700 ' + Math.round(H * 0.034) + 'px ' + FONT;
      /* ⚠ R62（painter state 泄漏修复）：歌手段会设 ctx.letterSpacing，
         若曲名段不显式复位，上一帧的歌手宽字距会残留到曲名 —— 这正是
         "歌名字距看起来宽"但代码里曲名从没设字距的原因。每次画曲名前
         强制 '0px'；再切回歌手段自己的字距。 */
      try { ctx.letterSpacing = '0px'; } catch (_) {}
      ctx.fillText(String(song.name || ''), W / 2, H * 0.185);
      ctx.fillStyle = 'rgba(255,255,255,.55)';
      /* ⚠ R61→R62：歌手字距 —— 原 H*0.004px 偏宽（用户反馈"过宽"）；
         R62 缩为 H*0.001px（曲名则 0）。canvas ctx.letterSpacing 仅
         Chromium 99+ 支持，不支持引擎 try/catch 静默忽略（无副作用）。 */
      ctx.font = '300 ' + Math.round(H * 0.019) + 'px "Microsoft YaHei Light","Microsoft YaHei",sans-serif';
      try { ctx.letterSpacing = Math.max(0, Math.round(H * 0.001)) + 'px'; } catch (_) {}
      ctx.fillText(String(song.artist || ''), W / 2, H * 0.185 + H * 0.052);
    }

    /* 当前歌词 + 上下邻行：恒 3 句 + 滑动过渡（R49） */
    if (state.lyricOn && lyricLines.length && lyricCur >= 0) {
      drawLyricView(now);
    }
  }

  /* ═══ 歌词视图：滚动式渲染（R62 重做，替代 R59 的"一帧硬切行"）═══
     用户原话：「歌词的问题是不要做切换那种，要做滚动，播过的滚到上面去，
     正在播的从下面滚上来，过渡要平滑，而且帧率要上去，不然还是有拖尾，
     然后所有的歌词字号整体小一些（85% 左右吧），没有在播放的歌词还要再淡一点。」
     设计：
       · **清屏先行**（drawPoster 开头 clearRect）→ 无叠画残影；
       · viewPos 向 target **指数缓动**（非硬切）：整行切换时旧行平滑上滚、
         新行从下方滚入；播放中逐帧微调 → 行随歌词"滚过"中心。
         每帧都执行（rAF 驱动 + seekLyric 每帧同步 target）→ 高帧率无拖尾；
       · 字号整体 ×0.85：焦点行 H*0.048→H*0.0408，浏览放大行 ×1.32 → 正常
         邻行 H*0.026→H*0.022；
       · 邻行（非播放）透明度 .32 → **.20**（再淡一点，主次更分明）；
       · 焦点行恒 1.0 纯白、零阴影（同 R59 契约）；
       · 滚轮浏览仍可用：浏览行放大 + 2s 无操作自动回正（语义保留）。 */
  var LYRIC_EASE = 0.16;      /* 每帧向目标逼近比例：越大滚动越快 */
  /* ═══ R67-6：歌词识别区缩到三行宽高+5% ═══
     每帧 drawLyricView 测 3 可见行的最大文本宽度（measureText）→ 写入模块级
     _lastBoxW/_lastBoxH。外部通过 lyricBoxW()/lyricBoxH()/lyricBoxCenter()
     拿到「刚好包住 3 行歌词」的矩形 → lyricZone 改为命中这个矩形 + 5% 边距，
     而不再是固定 68%×46% 视口百分比（太大，按钮以外的暗区也算歌词区）。 */
  var _lastBoxW = 0, _lastBoxH = 0, _lastBoxCx = 0, _lastBoxCy = 0;
  function drawLyricView(now) {
    var cy = H * 0.52, lh = LH();
    var target = viewTarget();
    if (!viewPosInit){ viewPos = target; viewPosInit = true; }
    else {
      /* 指数缓动：距离大滚动快、接近时慢收 —— 平滑无跳变。
         已到目标（|Δ|<1e-4）直接咬死，避免浮点余振。 */
      var dv = target - viewPos;
      if (Math.abs(dv) > 1e-4) viewPos += dv * LYRIC_EASE;
      else viewPos = target;
    }
    var center = Math.round(viewPos);
    var focus = hoverIdx;                 /* 放大/置顶的行：滚轮浏览行优先 */
    var maxW = 0;
    for (var i = center - 1; i <= center + 1; i++) {
      if (i < 0 || i >= lyricLines.length) continue;
      var dist = Math.abs(i - viewPos);
      var y = cy + (i - viewPos) * lh;
      var isFocus = (i === focus);
      /* ⚠ R62：整体字号 ×0.85 —— 焦点行（播放/浏览）H*0.048×0.85≈H*0.0408；
         浏览行再 ×1.32（R59 语义：只有滚轮浏览行放大）；邻行 H*0.026×0.85≈0.022。 */
      var size = Math.round(isFocus ? H * 0.0408 * 1.32
                            : (dist < 0.5 ? H * 0.0408 : H * 0.022));
      /* ⚠ R62：透明度再降 —— 焦点行 1.0 纯白；邻行 .32 → **.20**（更淡）。 */
      var alpha = (dist < 0.5 || isFocus) ? 1 : 0.20;
      ctx.font = '700 ' + size + 'px ' + FONT;
      ctx.fillStyle = 'rgba(255,255,255,' + alpha.toFixed(3) + ')';
      /* ⚠ R59/R62：零阴影 —— 白字直接写（清屏后无残影，不需要阴影压边） */
      var txt = String(lyricLines[i].text || '');
      ctx.fillText(txt, W / 2, y);
      /* R67-6：测本行实际像素宽度 → 3 行取最大；首字符比 %s 显示的还宽时 margin 也算进 */
      var w = ctx.measureText(txt).width;
      if (w > maxW) maxW = w;
    }
    /* 高 = 3 行 × lh（焦点行 1.32× 行高但字本身接近 lh）—— 保守取 3*lh 包含浏览放大 */
    _lastBoxW = maxW;
    _lastBoxH = 3 * lh;
    _lastBoxCx = W / 2;
    _lastBoxCy = cy;
  }

  function loop() {
    requestAnimationFrame(loop);
    if (!ctx || !W || !H) return;
    var now = performance.now();
    /* 2 秒回正：滚轮浏览停在非播放行超过 HOVER_RETURN_MS 且未操作 → 自动跳回播放行 */
    if (hoverIdx >= 0 && hoverIdx !== lyricCur && hoverAt
        && now - hoverAt > HOVER_RETURN_MS){
      hoverIdx = -1; hoverAt = 0;
    }
    drawPoster(now);
  }

  /* ---- LRC 解析（多时间标签 + 行时长推断，默认 4.8s，clamp 0.45-12s）---- */
  function parseLRC(text) {
    var out = [];
    var re = /\[(\d{1,2}):(\d{1,2})(?:\.(\d{1,3}))?\]/g;
    String(text || '').split(/\r?\n/).forEach(function (raw) {
      re.lastIndex = 0;
      var tags = [], m;
      while ((m = re.exec(raw))) tags.push(m);
      if (!tags.length) return;
      var txt = raw.replace(re, '').trim();
      if (!txt) return;
      tags.forEach(function (tg) {
        var mm = parseInt(tg[1], 10) * 60 + parseInt(tg[2], 10);
        var frac = tg[3] ? parseFloat('0.' + tg[3]) : 0;
        out.push({ time: mm + frac, text: txt });
      });
    });
    out.sort(function (a, b) { return a.time - b.time; });
    for (var i = 0; i < out.length; i++) {
      var dur = (i + 1 < out.length) ? (out[i + 1].time - out[i].time) : 4.8;
      out[i].dur = Math.max(0.45, Math.min(12, dur));
    }
    return out;
  }

  return {
    init: init,
    /* 供主脚本调用 */
    /* ⚠⚠ R61：setCover 变为 no-op（保留 API 契约，主脚本多处仍调用）。
       旧实现载入封面画进 posterBuf 铺氛围光 —— R61 已移除该绘制（它是
       "锐利/亚克力看起来一样"的伪亚克力来源）；#bgimg 才是皮肤唯一内容位，
       歌词海报画布只画文字，不再需要任何封面图像引用。 */
    setCover: function () { return; },
    setLyric: function (text) {
      lyricLines = parseLRC(text);
      lyricCur = lyricLines.length ? 0 : -1;
      viewPos = 0; viewPosInit = false;   /* 切歌 → 视图直接对齐（不播滑动） */
      hoverIdx = -1; hoverAt = 0;
    },
    /* 后端 /api/lyrics 返回**已解析**的 [{time(秒), text}] —— 直接吃，不要二次 LRC 解析 */
    setLyricLines: function (lines) {
      var arr = Array.isArray(lines) ? lines : [];
      lyricLines = arr
        .filter(function (l) { return l && l.text != null; })
        .map(function (l) { return { time: +l.time || 0, text: String(l.text), dur: +l.dur || 4.8 }; })
        .sort(function (a, b) { return a.time - b.time; });
      lyricCur = lyricLines.length ? 0 : -1;
      viewPos = 0; viewPosInit = false;   /* 切歌 → 视图直接对齐（不播滑动） */
      hoverIdx = -1; hoverAt = 0;
      return lyricLines.length;
    },
    /* 按播放进度推进当前行（供每帧/每次轮询调用） */
    seekLyric: function (ms) {
      if (!lyricLines.length) { lyricCur = -1; return; }
      var t = ms / 1000, idx = -1;
      for (var i = 0; i < lyricLines.length; i++) {
        if (lyricLines[i].time <= t) idx = i; else break;
      }
      lyricCur = idx;
    },
    getLyric: function () {
      return { lines: lyricLines, cur: lyricCur };
    },
    setLyricOn: function (on) {
      state.lyricOn = !!on;
      if (!state.lyricOn) { hoverIdx = -1; hoverAt = 0; }
    },
    isLyricOn: function () { return state.lyricOn; },
    /* ═══ 滚轮浏览（R49-22 重做，替代 R48 的 pickOn 浏览态）═══
       · 浏览**只由滚轮驱动**：index.html wheel → wheelLyric(±1)
       · 浏览行**放大**（不改色、无背板）；播放行跟随时不放大当前行
       · 停在非播放行 2 秒未操作 → loop() 自动回正（HOVER_RETURN_MS=2000）
       · 点击 → lyricHitTest(clientY) 行号 → seekToMs
       · ⚠ 鼠标悬停不换行（用户原话"通过滚轮调节，而不是鼠标放上去就能调节"） */
    lyricCount: function () { return lyricLines.length; },
    /* R67-6：暴露「3 行歌词刚好包住」的矩形中心点 + 宽高 —— 给 ui-orbit.js
       的 lyricZone 用（缩到三行宽+5%，不再用 0.68×0.46 视口百分比）。
       返回 cx/cy 是画布坐标，外部自行按 #poster-canvas 的 getBoundingClientRect
       转屏幕坐标。 */
    lyricBoxW:   function () { return _lastBoxW; },
    lyricBoxH:   function () { return _lastBoxH; },
    lyricBoxCx:  function () { return _lastBoxCx; },
    lyricBoxCy:  function () { return _lastBoxCy; },
    setLyricHover: function (i) {
      var n = (i == null || i < 0 || i >= lyricLines.length) ? -1 : i;
      if (n !== hoverIdx){
        hoverIdx = n;
        hoverAt = n >= 0 ? performance.now() : 0;   /* 每次换行都重置 2 秒计时 */
      } else if (n >= 0){
        /* 停在同一行（滚轮被边界 clamp）：不刷新计时 —— 停留超时照样回正 */
      }
      return hoverIdx;
    },
    lyricHover: function () { return hoverIdx; },
    /* 滚轮换行：以当前浏览行（无则播放行）为基准 ±1；2 秒无操作自动回正 */
    wheelLyric: function (d) {
      if (!lyricLines.length) return -1;
      var base = hoverIdx >= 0 ? hoverIdx : (lyricCur < 0 ? 0 : lyricCur);
      return this.setLyricHover(Math.max(0, Math.min(lyricLines.length - 1, base + (d | 0))));
    },
    /* clientY → 行号：以**视图中心行**（浮点 viewPos）为基准换算，滑动态下也准。
       ⚠ R49-22 只用于点击 seek 命中测试，不再驱动换行。 */
    lyricHitTest: function (clientY) {
      if (!cv || !lyricLines.length) return -1;
      var r = cv.getBoundingClientRect();
      if (!r.height) return -1;
      var y = (clientY - r.top) / r.height * H;
      var i = Math.round(viewPos + (y - H * 0.52) / LH());
      return (i >= 0 && i < lyricLines.length) ? i : -1;
    },
    /* 该行的起始时间（毫秒）；越界返回 null */
    lyricTimeMs: function (i) {
      var ln = lyricLines[i];
      if (!ln) return null;
      return Math.max(0, Math.round((+ln.time || 0) * 1000));
    },
    setMode: function (m) { state.bgMode = m; },
    parseLRC: parseLRC
  };
})();

/* ═══════ ui-core.js (状态/进度环/取色/律动/render) ═══════ */
/* ══════════════════════════════════════════════════════════
   ui-core.js — Radio Intelligence 前端模块 · 全局 $/API/st/MODES/post/fmt · 进度环 framePath/EQ 取色/封面调色/render
   自 webapi/static/index.html 内嵌 <script> L1415-L1631 拆出（R58-7 #46）
   与原 index.html 内所有 ui-*.js 共享顶层作用域 → 加载顺序即执行顺序，勿打乱
   ══════════════════════════════════════════════════════════ */
  const $ = id => document.getElementById(id);
  const API = '/api/playback';

  const st = { ready:false, playing:false, position:0, duration:0,
               /* ★ 2026-09-18：后端是否已上报过 >0 的真实播放位置(=音频真的开始了)。
                  主循环只在 posLive 时本地插值推进 → 装载期不再乐观推进/跳回。 */
               posLive:false,
               /* ★ 2026-09-19：播放模式门控（后端为权威）。漫游/推荐下 modeLocked=true，
                  按钮置灰；高亮用 modeEff（"实际生效"的模式），避免显示随机却按顺序放。 */
               modeLocked:false, modeEff:'order',
               title:'', artist:'', cover:'', volume:0.8, muted:false,
               mode:'order', fav:false, disliked:false, current:null,
               /* R58-1 (#41)：开关镜像（后端为权威）。默认与后端 radio 一致
                  （dj_on/roam_on 初值 True）；loadBroadcast 的口播栏门控读它。 */
               djOn:true, roamOn:true };

  /* 播放模式：顺序 → 单曲循环 → 随机（与原 UI 三态一致） */
  const MODES = [
    { key:'order',    name:'顺序',     ic:'<path d="M4 6h16M4 12h16M4 18h16"/>' },
    { key:'loop_one', name:'单曲循环', ic:'<path d="M4 6h13M4 12h13M4 18h13"/><path d="M19 4l3 3-3 3"/>' },
    { key:'random',   name:'随机',     ic:'<path d="M16 3h5v5"/><path d="M4 20L21 3"/><path d="M21 16v5h-5"/><path d="M15 15l6 6"/><path d="M4 4l5 5"/>' },
  ];
  let modeIdx = 0;

  function post(path, qs){
    return fetch(API + path + (qs ? ('?' + qs) : ''), {method:'POST'})
      .then(r => r.json()).catch(e => ({ok:false, error:String(e)}));
  }
  function fmt(ms){
    if (!ms || ms < 0) ms = 0;
    const s = Math.floor(ms/1000);
    return Math.floor(s/60) + ':' + String(s%60).padStart(2,'0');
  }

  /* ---------- 外围环绕进度（移植原 computeFramePath） ---------- */
  const framePath=$('frame-path'), frameProg=$('frame-progress'), frameGlow=$('frame-glow'),
        frameBloom=$('frame-bloom'), frameBloom2=$('frame-bloom2'), frameHit=$('frame-hit'),
        playhead=$('frame-playhead'), playheadCore=$('frame-playhead-core');
  let pathD='', frameLen=0;

  function computeFramePath(){
    const r = $('dock').getBoundingClientRect();
    const x=0, y=0, w=r.width, h=r.height, rad=h*.45;
    /* 起点：底部边框正中，逆时针 下→左→上→右→回底部中点 */
    pathD = `M ${x + w/2} ${y + h}
      L ${x + rad} ${y + h}
      A ${rad} ${rad} 0 0 1 ${x} ${y + h - rad}
      L ${x} ${y + rad}
      A ${rad} ${rad} 0 0 1 ${x + rad} ${y}
      L ${x + w - rad} ${y}
      A ${rad} ${rad} 0 0 1 ${x + w} ${y + rad}
      L ${x + w} ${y + h - rad}
      A ${rad} ${rad} 0 0 1 ${x + w - rad} ${y + h}
      L ${x + w/2} ${y + h}`;
    [framePath, frameProg, frameGlow, frameBloom, frameBloom2, frameHit]
      .forEach(el => el && el.setAttribute('d', pathD));
    try { frameLen = framePath.getTotalLength(); } catch(_) { frameLen = 0; }
    _frameProgLen = -1;   /* 路径变了 → 强制 updateFrameProgress 重新写入一次（原 UI L2732） */
  }
  /* 上次写入的进度长度；<0 表示需要强制刷新（路径重算 / 首次）。 */
  let _frameProgLen = -1;
  /* ★ 2026-09-19：换曲时"把进度环补满并多留 260ms"的定时器（见 ui-view.js track_changed） */
  let ringHoldTimer = 0;

  /* ⚠ 播放头「靠近渐显 + 播放态呼吸」—— 原 UI L2757-2763 + L2798-2806。
     进度条"动画不对"的主因就是缺了这套：原 UI 的播放头不是常显，
     而是**鼠标靠近才出现**，播放时做正弦呼吸（.55 ± .13）。
     ⚠ 监听器**不在这里绑定**：dock 是本文件后面用 const 声明的（L1030），
     在此处立即执行会撞 TDZ。绑定统一放到下方「交互」区的 bindFrameHeadProximity()。 */
  let frameHeadNear = false;
  const HEAD_NEAR = 46;
  function bindFrameHeadProximity(){
    dock.addEventListener('mousemove', e => {
      const r = dock.getBoundingClientRect();
      const mx = e.clientX - r.left, my = e.clientY - r.top;
      const cx = parseFloat(playhead.getAttribute('cx')) || 0;
      const cy = parseFloat(playhead.getAttribute('cy')) || 0;
      frameHeadNear = Math.hypot(mx - cx, my - cy) < HEAD_NEAR;
    });
    const dh = $('dock-hit');
    if (dh) dh.addEventListener('mouseleave', () => { frameHeadNear = false; });
  }

  function updateFrameProgress(){
    if (!frameLen) return;
    const ratio = (st.duration > 0) ? Math.min(1, st.position / st.duration) : 0;
    const len = frameLen * ratio;
    /* ⚠ 节流：进度没实质变化（<0.5px）就不改写 stroke-dasharray。
       本函数每帧调用，无脑改写等于每帧重绘 4 条 SVG 路径
       （其中 bloom 还带 blur 滤镜）→ 疯狂闪烁（原 UI L2740-2743 同款坑）。 */
    if (_frameProgLen < 0 || Math.abs(len - _frameProgLen) >= 0.5){
      _frameProgLen = len;
      /* ⚠ dasharray 必须是 `${len} ${frameLen}`（dash=已播实线, gap=足够长保证后面全空）
         + dashoffset=0。早期单值 `frameLen` + offset 换算是错的（原 UI L2746-2751）。 */
      const da = `${len} ${frameLen}`;
      [frameProg, frameGlow, frameBloom, frameBloom2].forEach(el => {
        if (!el) return;
        el.style.strokeDasharray = da;
        el.style.strokeDashoffset = 0;
      });
      try {
        const p = framePath.getPointAtLength(Math.max(1, len));
        playhead.setAttribute('cx', p.x); playhead.setAttribute('cy', p.y);
        playheadCore.setAttribute('cx', p.x); playheadCore.setAttribute('cy', p.y);
      } catch(_) {}
    }
    /* 靠近时渐显：播放态呼吸，离开渐变消失（与几何解耦，每帧都要跑） */
    try {
      if (frameHeadNear || seeking){
        if (st.playing){
          const breathe = .55 + Math.sin(performance.now() * .0021) * .13;
          playhead.style.opacity = breathe;
        } else {
          playhead.style.opacity = 1;
        }
      } else {
        playhead.style.opacity = 0;
      }
    } catch(_) {}
  }

  /* ---------- 均衡器（播放时跳动） ----------
     ═══ 音频响应：有真实频谱（__spectrum，后端 10Hz 推 16 段）优先驱动；
     无频谱（后端没开/未播放）退回原来的随机模拟。EQ 是 dock 装饰，常跳。 */
  let eqTimer = null;
  /* EQ 条刷新体（interval 与 offscreen 手动 tick 共用）。
     抽出来供 __r54.tickEq() 黑盒手动排帧 —— offscreen 下 setInterval 被
     节流到 ~1s，probe 靠 pump(400ms) 赌 tick 会 flaky。 */
  function eqTick(){
    const bars = Array.from(document.querySelectorAll('#eq i'));
    if (bars.length){
      const sp = window.__spectrum;
      if (sp && sp.length >= 5 && st.playing){
        for (let i = 0; i < bars.length; i++){
          const idx = Math.min(sp.length - 1, Math.floor((i + 0.5) * sp.length / bars.length));
          const v = Math.max(0, Math.min(1, Number(sp[idx]) || 0));
          bars[i].style.height = (4 + v * 16) + 'px';
        }
      } else {
        bars.forEach(b => { b.style.height = (4 + Math.random()*14) + 'px'; });
      }
    }
    /* ⚠ 封面律动保底心跳：rAF mainLoop 在 offscreen/后台页被节流到零帧，
       beatTick 只挂 rAF 会永远不驱动封面 → 这里随 EQ tick 同步驱动一次
       beatPulse（内部幂等 memo，与 rAF 双写不冲突）。 */
    try { beatPulse(); } catch (_) {}
  }
  function startEq(){
    if (eqTimer) return;
    eqTimer = setInterval(eqTick, 100);
  }
  function stopEq(){
    if (eqTimer){ clearInterval(eqTimer); eqTimer = null; }
    document.querySelectorAll('#eq i').forEach(b => { b.style.height = '4px'; });
    /* 停止播放 → 封面律动归零（残留 scale 清掉） */
    try { const ci = $('dc-cover-img'); if (ci && ci.style.transform) ci.style.transform = ''; } catch (_) {}
  }

  /* ---------- 封面音频律动（body.beat-on 时启用） ----------
     bass = 频谱前 3 段均值 → 驱动 dock 封面（dc-cover-img）scale 微胀
     （老 UI L2977 dcCoverImg beatPulse 同款语义，但用**真实频谱**）。
     ⚠ 只动 dc-cover-img 的 transform —— 它是 dock 内静态封面，无其它
       写入方（老 UI 已这么干）。绝不碰：
         · #bgimg/#bgvideo filter/transform（acrylic/translateY 三层模型）
         · .scard transform（ui-cards layout 每帧写，覆盖即失效/闪跳）
     归零：停止播放 / 无频谱 2s / 关闭开关 → 恢复原状。 */
  function beatLevel(){
    const sp = window.__spectrum;
    if (!sp || !st.playing) return 0;
    if (performance.now() - (window.__specAt || 0) > 2000) return 0;  // 频谱过期
    let s = 0, n = 0;
    for (let i = 0; i < Math.min(4, sp.length); i++){ s += Number(sp[i]) || 0; n++; }
    if (!n) return 0;
    return Math.max(0, Math.min(1, s / n));
  }
  /* 封面律动实际执行体（幂等 memo：transform 不变就不写，避免每帧扰动）。
     rAF mainLoop 与 EQ interval 心跳双入口调用 —— offscreen/后台页 rAF
     零帧时靠 interval 保底驱动。返回当前电平 v（供断言/测试）。 */
  function beatPulse(){
    if (!document.body.classList.contains('beat-on')) return 0;
    const v = beatLevel();
    try {
      const ci = $('dc-cover-img');
      if (ci){
        if (v > 0.01){
          const s = (1 + v * 0.025).toFixed(4);        // ≤2.5% 微胀
          if (ci.style.transform !== 'scale(' + s + ')') ci.style.transform = 'scale(' + s + ')';
        } else if (ci.style.transform){
          ci.style.transform = '';
        }
      }
    } catch (_) {}
    return v;
  }
  function beatTick(){
    return beatPulse();
  }

  /* ---------- 封面取色：更新 --primary / --accent（音量条/进度随之变色） ---------- */
  function applyCoverPalette(url){
    if (!url) return;
    const img = new Image();
    img.crossOrigin = 'anonymous';
    img.onload = () => {
      try {
        const cv = document.createElement('canvas');
        cv.width = cv.height = 32;
        const g = cv.getContext('2d');
        g.drawImage(img, 0, 0, 32, 32);
        const d = g.getImageData(0, 0, 32, 32).data;
        let r=0, gg=0, b=0, n=0;
        for (let i=0; i<d.length; i+=4){
          if (d[i+3] < 128) continue;
          r+=d[i]; gg+=d[i+1]; b+=d[i+2]; n++;
        }
        if (!n) return;
        r=Math.round(r/n); gg=Math.round(gg/n); b=Math.round(b/n);
        const hex = (x,y,z) => '#' + [x,y,z].map(v =>
          Math.max(0, Math.min(255, v)).toString(16).padStart(2,'0')).join('');
        // primary = 取样色（提亮），accent = 取样色（再提亮）
        document.documentElement.style.setProperty('--primary', hex(r+30, gg+30, b+30));
        document.documentElement.style.setProperty('--accent',  hex(r+70, gg+70, b+70));
      } catch(_) { /* 跨域取色失败则保留默认色 */ }
    };
    img.onerror = () => {};
    img.src = url;
  }

  /* ---------- 渲染 ---------- */
  /* ═══ R59：默认皮肤（程序化主题底图）═══
     用户报"一开屏一大块黑只有一点背景色" + "播放音乐时一大块灰"。
     背景三层模型里 #bgimg 是唯一壁纸位，但**没有封面/壁纸时它没 src、
     opacity:0** → 底下只剩 #bg/body 的深色底（且 R58-4 那层画布蒙层曾
     叠出灰块）。给 #bgimg 喂一张主题色深空渐变 + 星点的程序化底图：
       · 无 src 期即开屏期就可见（类 has-bg），body 露黑面积大幅缩小；
       · 无封面/无壁纸期 Poster / acrylic 切换都有真实"皮肤"可作用；
       · 之后真实封面/自定义壁纸 setAttribute('src') 自然盖过（img 覆盖）。
     ⚠ 深色为主：这张图是"皮肤底"，亚克力模糊后不刺眼，白字也始终可读。 */
  let _defSkin = null;
  function ensureDefaultSkin(){
    const img = $('bgimg');
    if (!img) return;
    if (!img.getAttribute('src')){
      /* R67-5：启动默认壁纸 = Radio Intelligence 玻璃 logo（用户原话）。
         替代原先的程序化 canvas 渐变星点。logo 在 webapi/static/brand/logo.png。 */
      img.setAttribute('src', '/brand/logo.png');
      img.classList.add('has-bg');
    }
  }
  function render(){
    try { if (window.PosterState){ window.PosterState.song = {
      name: st.title || '', artist: st.artist || '' }; } } catch (_) {}
    /* R59：无 src 时补程序化默认皮肤（开屏/无封面期可见性），有封面后不再触发 */
    try {
      const bgi = $('bgimg');
      if (bgi && !bgi.getAttribute('src') && !st.cover) ensureDefaultSkin();
    } catch (_) {}
    /* 还原：dock 标题不再显示诗句；无曲时显示中性占位（加载类文案已移除） */
    $('dc-title').textContent  = st.title  || (st.ready ? '未在播放' : '电台准备中');
    $('dc-artist').textContent = st.artist || '—';
    // 播放/暂停图标 + 呼吸圈
    $('ico-play').innerHTML = st.playing
      ? '<path d="M7 5h3.5v14H7zm6.5 0H17v14h-3.5z"/>'
      : '<path d="M8 5v14l11-7z"/>';
    $('d-play').classList.toggle('playing', st.playing);
    if (st.playing) startEq(); else stopEq();
    // 封面
    const ci = $('dc-cover-img');
    if (st.cover && ci.getAttribute('src') !== st.cover){
      ci.setAttribute('src', st.cover);
      applyCoverPalette(st.cover);
      /* ═══ R57-1：背景三层模型重构 ═══
         新语义（用户原话）：
           · 歌词海报 / 自定义壁纸 = **背景内容层**（#bgimg 统一承载）
           · 亚克力 = **可选背景效果**：body.bgmode-acrylic 时对 #bgimg
             施加重模糊（CSS blur 72px）；去掉后壁纸/封面锐利显示
           · 歌词文字画在透明海报画布上，无论何种背景效果都叠在上层
         实现：
           · #bgimg 恒为背景图位：无自定义壁纸(wp-active) → 当前封面；
             wp-active → 自定义图（render 不再覆盖，见 applyWallpaperImage）
           · acrylic 只是给 #bgimg 加 blur 的 CSS 效果开关，与内容解耦
           · Poster.setCover 保留但仅作氛围色参考（画布已不铺不透明封面） */
      const bgimg = $('bgimg');
      /* R78：背景内容保持判定 —— body.wp-active（自定义壁纸/内置 logo 已挂）
         或偏好 uiWallpaper==='default'/'custom' 时，**背景不被歌曲封面覆盖**；
         仅「跟随封面」（cover）时 render 才用 st.cover 喂 bgimg。
         （applyBrandSkin/applyWallpaperImage 都挂 body.wp-active；这里双保险，
         防止 wp-active 被其它路径误摘后默认 logo 壁纸仍被切歌覆盖。） */
      const bgKept = document.body.classList.contains('wp-active')
                  || (typeof uiWallpaper === 'string' && uiWallpaper !== 'cover');
      if (bgimg && !bgKept && bgimg.getAttribute('src') !== st.cover){
        bgimg.setAttribute('src', st.cover);
        bgimg.classList.add('has-bg');
        /* 移除可能残留的壁纸覆盖态（切歌回到封面背景） */
        document.body.classList.remove('wp-active');
        bgimg.classList.remove('wp-active');
      }
      /* 歌词海报氛围底色（画布透明，仅局部光晕参考） */
      try { if (window.Poster) window.Poster.setCover(st.cover); } catch (_) {}
      try { if (window.LiquidBg) window.LiquidBg.setSource(st.cover); } catch (_) {}
    }
    // 模式按钮
    /* ★ 2026-09-19：高亮按**实际生效**模式（漫游/推荐下后端强制 order）；
       锁定态置灰 + tooltip，避免"按钮显示随机、实际按顺序"的错位。 */
    const _mEff = MODES.findIndex(x => x.key === (st.modeEff || 'order'));
    const m = MODES[_mEff >= 0 ? _mEff : modeIdx];
    $('d-mode-ic').innerHTML = m.ic;
    $('d-mode').classList.toggle('on', true); $('d-mode').dataset.mode = m.key;
    /* T2：三态点亮色相内联写入（绕开 .dbtn transition/特异性疑云） */
    $('d-mode').style.color = m.key === 'loop_one' ? '#7ec8ff'
                            : m.key === 'random' ? '#d3a4ff' : '';
    $('d-mode').style.filter = m.key === 'order' ? ''
                             : 'drop-shadow(0 0 8px ' + (m.key === 'loop_one' ? 'rgba(126,200,255,.65)' : 'rgba(211,164,255,.65)') + ')';
    $('d-mode').style.opacity = st.modeLocked ? '0.4' : '';
    $('d-mode').style.cursor = st.modeLocked ? 'not-allowed' : '';
    $('d-mode').title = st.modeLocked
      ? '漫游 / 推荐下不可切换播放模式（只按顺序播放）'
      : ('播放模式：' + m.name);
    // 音量
    const vp = Math.round((st.muted ? 0 : st.volume) * 100);
    $('vol-fill').style.width = vp + '%';
    $('vol-thumb').style.left = vp + '%';
    $('vol-icon').classList.toggle('muted', st.muted || st.volume === 0);
    // 按钮启用
    ['d-mode','d-prev','d-play','d-next','d-lyric','d-queue','d-dislike',
     'd-history','d-fav']               // R49：dock 喜欢 ♥ 恢复（老 UI #d-fav）
      .forEach(id => { const el = $(id); if (el) el.disabled = !st.ready; });
    // 喜欢 ♥ 点亮态以后端为准（applyState 已把 d.favorited 写进 st.fav）
    $('d-fav').classList.toggle('on', !!st.fav);
    // 不喜欢点亮态也以后端为准（R49-27：applyState 写 st.disliked，
    // 切歌自动重置 —— 新曲不是 disliked 就熄灭，不带进下一首）
    $('d-dislike').classList.toggle('on', !!st.disliked);
    updateFrameProgress();
  }


/* ═══════ ui-state.js (applyState/口播/toast/歌词) ═══════ */
/* ══════════════════════════════════════════════════════════
   ui-state.js — Radio Intelligence 前端模块 · applyState · AI 口播 aibar/thinking · toast · 歌词 loadLyrics
   自 webapi/static/index.html 内嵌 <script> L1632-L1808 拆出（R58-7 #46）
   与原 index.html 内所有 ui-*.js 共享顶层作用域 → 加载顺序即执行顺序，勿打乱
   ══════════════════════════════════════════════════════════ */

  /* ═══ R95d-Q1：换曲宽限（轮询误报 `playing=false` 的兜底） ═══
     根因（`docs/water_physics_research.md` §10.1）：切歌装载窗内后端引擎瞬时 STOPPED，
     2s 轮询 `/api/state` 会灌进 `playing=false` ⇒ `calm` 0.28s 冲顶、h 场 0.5s 掉 79%
     （"水波像被集体清掉"）。而**切歌路径从不发 WS `playing=false`**（只有用户真暂停
     才发）⇒ 宽限期内"轮询说停"一律不信；"WS 说停"才是权威、立即解除宽限。
     Q0（后端报意图）已掐掉源头，这里是**纯前端保险**（后端未升级/降级路径也安全）。 */
  const PLAY_GRACE_MS = 6000;
  let _playGraceUntil = 0;
  function armPlayGrace(ms){
    _playGraceUntil = performance.now() + (typeof ms === 'number' ? ms : PLAY_GRACE_MS);
    return _playGraceUntil;
  }
  function clearPlayGrace(){ _playGraceUntil = 0; }
  function inPlayGrace(){ return performance.now() < _playGraceUntil; }
  /* 黑盒验证钩（offscreen 断言用，只读 + 手动 arm/clear） */
  window.__playGrace = {
    ms:  function(){ return PLAY_GRACE_MS; },
    arm: function(ms){ return armPlayGrace(ms); },
    clear: function(){ clearPlayGrace(); return 0; },
    in:  function(){ return inPlayGrace(); },
    left: function(){ return Math.max(0, _playGraceUntil - performance.now()); }
  };

  function applyState(d){
    if (!d) return;
    /* ⚠⚠ prevTitle 必须在**任何** st.title 写入之前抓取（R47「切歌了歌词还是上一首」的根因）：
       下面既有 `d.title` 直写、又有 `d.current.title` 兜底写入，
       若在这两处之后才取 prevTitle，取到的已经是新标题 → 判定永远"没切歌" → 永不重新取词。 */
    const prevTitle = st.title;
    const prevPlaying = !!st.playing;
    if (typeof d.ready    === 'boolean') st.ready   = d.ready;
    /* ⚠ R95d-Q1：换曲宽限 —— 只**吞掉**轮询里的 false，从不吞 true。
       宽限内 `st.playing` 保持原值（真值），WS 'playing' 事件一到立即解除宽限。 */
    if (typeof d.playing  === 'boolean'){
      if (d.playing){ clearPlayGrace(); st.playing = true; }
      else if (!inPlayGrace()) st.playing = false;
    }
    adoptRemotePosition(d.position);
    /* 歌词随播放进度推进当前行 */
    try { if (window.Poster) window.Poster.seekLyric(st.position); } catch (_) {}
    if (typeof d.duration === 'number')  st.duration = d.duration;
    if (d.title  != null) st.title  = d.title;
    if (d.artist != null) st.artist = d.artist;
    if (typeof d.mode === 'string' && d.mode){
      const i = MODES.findIndex(m => m.key === d.mode);
      if (i >= 0) modeIdx = i;
    }
    const cur = d.current || {};
    /* ⚠ R49：必须把整包 current 存下 —— dock 的不喜欢/喜欢按钮靠它算
       fav_key（st.current 此前从未被赋值，点红心永远"当前无曲目"）。 */
    if (d.current) st.current = d.current;
    if (typeof d.favorited === 'boolean') st.fav = d.favorited;
    /* ⚠ R49-27：不喜欢状态以**后端为权威**，切歌后必须重置 —— 旧实现只在
       用户点击时 toggle，上一首点亮的不喜欢会带到下一首（下一首明明没标
       不喜欢，dock 斜杠红心却亮着）。get_state 现在带 disliked，轮询/初始化
       自动把 #d-dislike 同步成当前曲真实状态（新曲不是 disliked → 熄灭）。 */
    if (typeof d.disliked === 'boolean'){
      st.disliked = d.disliked;
      $('d-dislike').classList.toggle('on', d.disliked);
    }
    st.title  = cur.title  || st.title;
    st.artist = cur.artist || st.artist;
    if (cur.cover) st.cover = cur.cover;
    /* 切歌 → 重新取词（老 UI L4844：切歌即加载，不被 lyricOn 门控） */
    if (cur.title && cur.title !== prevTitle){
      try { loadLyrics(true); } catch (_) {}
      /* ★ 2026-11-16：自动切歌排水兜底。排水原本只靠 WS 'track_changed'
         （ui-view.js:132 → setMusicEvents(null)）；自动路径的并发守卫（_advance
         非阻塞锁 / 世代 seq）偶尔会吞掉那次 emit ⇒ 前端收不到 ⇒ 旧歌水波不排、
         机械堆叠。这里复用已存在的标题变化判定，在 2s 轮询里补一次排水：
         · prevTitle 非空守卫 → 初始化不误触发；
         · WS 正常到达时 title 已被更新 → 此处 prevTitle===cur.title，天然去重。 */
      if (prevTitle){
        try {
          if (window.__water && window.__water.setMusicEvents){
            window.__water.setMusicEvents(null);
          }
        } catch (_e) {}
      }
    }
    /* #d-radio / #d-ai 点亮态以**后端为权威**（初始化与轮询都同步） */
    if (typeof d.roam_on === 'boolean'){
      st.roamOn = d.roam_on;
      $('d-radio').classList.toggle('on', d.roam_on);
    }
    if (typeof d.dj_on   === 'boolean'){
      st.djOn = d.dj_on;
      $('d-ai').classList.toggle('on', d.dj_on);
      /* ★ R119-7：伴听关闭 ⇒ 圈圈恒熄（否则会留一个永远转的圈） */
      if (!d.dj_on) { try { setAIWaiting(false); } catch (_) {} }
    }
    /* R58-1 (#41)：AI 伴听关闭 → 口播栏立即淡出（原 UI setAI(false)）。
       ⚠ applyState 从 2s 轮询来，本身就带后端权威 dj_on —— 这里**无条件**
       收口口播栏（开：常驻显示，不依赖鼠标靠近/新文案；关：hide）。 */
    if (d.dj_on === true){
      /* R59：打开 = **立即常驻** —— 摘下 hide + 加 show（不再等鼠标靠近 /
         WS 文案才显形）；文字保留上一句，broadcast 轮询 3s 内会更新。 */
      showAIBar();
    } else if (d.dj_on === false){
      hideAIBar();
    }
    /* ⚠ R49-30：开始播放 → 空闲态"恒显"的栏目提示淡出（之后只随 orbit
       切栏目闪示 3s）；若当前显示是 orbit 切换触发的 3s 闪示则保留由计时器收走。 */
    if (d.playing === true && !prevPlaying){ try { hideColumnIfIdle(); } catch (_) {} }
    render();  }

  /* AI 口播栏：统一写入口（带 swap 换字动画 + 自动展开）。
     与 WS dj_speech / 轮询 loadBroadcast 共用，避免两处各写一份文案逻辑。
     原 UI L5485-5490：dj_speech 到达时 textContent 更新 + swap 重放 + 自动展开。 */
  /* ⚠⚠ R53 outro 展示修复：
     口播有两条到达路径 —— ① 轮询 /api/dj/broadcast（当前曲 intro）；
     ② WS dj_speech（**intro 与 outro 都走这条**：intro = 新歌开场导听，
     outro = 旧歌收尾的转场告别，见 ai/transition.py _begin_transition emit）。
     bug：outro 经 WS 显示后，≤3s 就被 loadBroadcast 轮询拉回的 current_broadcast
     （仍是当前曲 intro）覆盖 → 用户永远看不到 outro。
     修复：WS 刚推送过 dj_speech 的 3s 内，轮询**不得覆盖**文案 ——
     outro 得以停留到切歌；切歌后 track_changed/poll 自然换新歌 intro。 */
  let lastDjAt = 0;
  /* ★ R119-7 · A：**WS 推送锁定到下一次换歌**（取代"3 秒时间窗"）。
     为什么 3 秒窗不够（用户 2026-09-21 报的真 bug）：
       转场(outro)在**上一首结尾**播，那一刻 `current_broadcast` 仍是**这首歌的 intro**；
       3 秒窗一过轮询就把 outro 覆盖回同曲 intro
       ⇒ 用户看到"显示一会 outro 就一下子跳回该首的 intro"。
     新规则：WS 推送过文案 ⇒ 钉住，**只由换歌（track_changed）解锁**。
     轮询仍保留它原本的作用（页面刷新 / WS 掉线后的兜底回填，那时 hold 为假）。 */
  let djHold = false;
  function clearDjHold(){ djHold = false; }

  /* ★ R119-7 · B：口播栏「加载中」圈圈 = **本曲口播还没到位**（不是"正在朗读"）。
     旧行为为什么看不见：圈圈只由后端 `thinking` 事件驱动，而 (a) `_speak_intro`
     是**先发 thinking=False、再去播音频**，(b) 口播现在**预生成**（歌一开播文案
     就已就绪）⇒ True→False 只隔几毫秒。
     新行为：换曲即亮「等本曲口播」，文案一到达就熄；后端 thinking 仍可覆盖。
     ⚠ 兜底：45s 自动熄（口播彻底失败时不留一个永远转的圈）。 */
  let aiWaitTimer = 0;
  function setAIWaiting(on){
    const tb = $('ai-bar-think');
    if (!tb) return;
    /* ★ 2026-11-17（用户要求）：口播栏**不再显示"加载中"转圈**。
       理由：兜底音频/文案现在都是即时的（预生成 + 落盘），这个圈没有任何
       信息量，亮着反而丑。事件仍然收，只是不再点亮。 */
    clearTimeout(aiWaitTimer);
    aiWaitTimer = 0;
    tb.classList.remove('on');
    return;
    /* 以下为旧实现，保留作参考（永不执行） */
    if (on && st.djOn){
      tb.classList.add('on');
      clearTimeout(aiWaitTimer);
      aiWaitTimer = setTimeout(function(){ tb.classList.remove('on'); }, 45000);
    } else {
      clearTimeout(aiWaitTimer);
      aiWaitTimer = 0;
      tb.classList.remove('on');
    }
  }
  /* ═══ R55：口播栏锚到左侧栏目提示（column-panel）正上方 ═══
     口播栏与栏目提示同处左中，两元素都是 fixed；若各写死 top 值，
     一旦 column-panel 高度/位置变化就会互相叠压或离太远。
     这里读 column-panel 真实包围盒，把口播栏底边贴到它顶边上方 14px、
     左缘对齐 —— 无论栏目提示多高都精确"在其正上方"。
     口播栏本身 .show 前是不可见（opacity:0），锚定不产生跳变。

     ═══ R57-7：水平**固定**，不再实时追 cr.left ═══
     用户原话：「口播栏有时左右跳动，固定住，让它最左侧与 column-panel
     展开后的最左侧平齐」。
     根因：column-panel 收起/展开动画走 translateX(-26px↔0)（见 #column-panel
     CSS transform），旧实现 bar.style.left = cr.left 会把 transform 位移量
     （收起时 -26px → cr.left≈22px；展开后 →48px）实时写进口播栏 = 左右乱跳。
     修复：取 column-panel 的 **computed 静态 left（不含 transform）** =
     它展开后（translateX(0)）的最左侧 48px，据此固定。column-panel 的
     translateY(-50%) 是恒定垂直居中、动画不改高度 → cr.top 稳定，
     垂直方向保留实时锚（仅内容高度变化时微调，不会跳）。 */
  function positionAIBar(){
    const bar = $('aibar'), cp = $('column-panel');
    if (!bar || !cp) return;
    const cr = cp.getBoundingClientRect();
    if (!cr.height && !cr.width) return;   // 尚未布局，等下次调用
    const vh = window.innerHeight;
    /* 展开态最左侧 = computed left（静态定位值，transform 动画不改它） */
    const L = (window.__layout && window.__layout.get()) || null;   // P1
    const cssLeft = parseFloat(getComputedStyle(cp).left);
    bar.style.left = (Number.isFinite(cssLeft) ? cssLeft : (L?L.aibar.offsetX:48)) + 'px';
    /* fixed 的 bottom = 视口高 − 目标上缘；目标上缘 = 面板顶 − 间距 */
    const gap = L?L.aibar.gap:14;
    bar.style.top = 'auto';
    bar.style.bottom = Math.max(10, Math.round(vh - (cr.top - gap))) + 'px';
    bar.style.transform = '';
    bar.style.maxWidth = (L?L.aibar.maxWidth:'min(430px, 62vw)');
  }
  /* ═══ R59：口播栏「开即常驻」统一入口 ═══
     摘下 hide（若有）—— 打开 AI 伴听后面板**常驻显示**，不再需要鼠标靠近
     才出现（用户点名）。
     ⚠ 显形条件：ai-text 已有内容才直接 add show；空栏（启动初期尚无任何
     口播文案）保持隐藏态，等首条文案（setAIBarText 保底 add show）自然亮起，
     避免启动时左上角飘一张空玻璃卡。 */
  function showAIBar(){
    const bar = $('aibar');
    if (!bar) return;
    hideToken = 0;                 // 清除在途关闭收尾，防止旧 hide 尾巴把 show 摘掉
    bar.classList.remove('hide');
    const tx = $('ai-text');
    if (tx && String(tx.textContent || '').trim()){
      bar.classList.add('show');
      try { positionAIBar(); } catch (_) {}
    }
  }
  /* 文案变化 / 窗口缩放 / 面板显隐都重锚（R55：column-panel 靠近弹出时也要跟随） */
  window.addEventListener('resize', function(){
    try { positionAIBar(); } catch(_){}
    /* R62 #7：toast 固定锚随窗口高度变化（dock 底部 24px 是视口相对值） */
    const tv = $('toast');
    if (tv && tv.classList.contains('show')) { try { positionToast(); } catch(_){} }
  });
  function setAIBarText(text, viaWs){
    const bar = $('aibar'), tx = $('ai-text');
    if (!bar || !tx) return;
    if (viaWs){
      lastDjAt = Date.now();
      djHold = true;      // ★ R119-7：WS 文案钉到下一次换歌（轮询不得回填覆盖）
    }
    tx.classList.remove('swap'); void tx.offsetWidth; tx.classList.add('swap');
    tx.textContent = text;
    try { setAIWaiting(false); } catch (_) {}   // ★ R119-7：文案到了 ⇒ 熄「加载中」圈
    /* R59：有文案即常驻显形（移除时不再重复弹 show，setAIBarText 不再自动 add show
       以外的隐藏态管理 —— 显隐只由 showAIBar/hideAIBar 负责；此处保底 show 一次
       防止"开伴听后首条文案到达但栏仍是初始隐藏态"。 */
    if (!bar.classList.contains('show')) bar.classList.add('show');
    bar.classList.remove('hide');
    try { positionAIBar(); } catch (_) {}
  }
  /* 关闭 AI 伴听 → 口播栏淡出（原 UI setAI(false)：add hide 后 400ms 摘 show）。
     ⚠ R58-1 (#41)：hide 是异步收尾 —— 用 hideToken 快照，400ms 内若重新打开
     （摘 hide + 加 show），到点的旧定时器**不得**把 show 摘掉（否则开完又被
     上一轮关闭的尾巴藏起来，表现为"打开不显示"）。 */
  let hideToken = 0;
  function hideAIBar(){
    const bar = $('aibar'); if (!bar) return;
    const myTok = ++hideToken;
    bar.classList.add('hide');
    setTimeout(() => {
      if (myTok !== hideToken) return;   // 期间被重新打开过 → 放弃收尾
      bar.classList.remove('show');
    }, 400);
  }

  /* AI 口播文案：轮询后端 current_broadcast，有则淡入显示（WS dj_speech 兜底）。
     ⚠ R53：轮询到但刚被 WS dj_speech 推送过（3s 内）→ 跳过，
     否则 outro（WS 推送）会被 current_broadcast（intro）立即覆盖。
     ⚠ R58-1 (#41)：AI 伴听关闭（st.djOn=false）时**不拉也不显示**——
     后端 dj_on 只门控音频输出、文案仍"始终生成"，current_broadcast 残留
     旧文案；不 gate 的话用户关掉伴听后口播栏还会被 3s 轮询再次弹出。 */
  function loadBroadcast(){
    if (!st.djOn) return;
    /* ★ R119-7：从"3 秒时间窗"改为"WS 钉住直到换歌"—— 时间窗过短会被
       同曲 intro 覆盖（见 setAIWaiting/djHold 处注释）。 */
    if (djHold) return;
    fetch('/api/dj/broadcast').then(r => r.json()).then(d => {
      if (d && d.ready && d.text) setAIBarText(d.text);
    }).catch(() => {});
  }

  /* Toast（原 UI L4199-4220）：锚定 rec-style/dock 动态水平居中 + 纵向贴紧 */
  function toast(msg){
    const t = $('toast'); if (!t) return;
    t.innerHTML = msg;
    positionToast();
    t.classList.add('show');
    clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove('show'), 2200);
  }
  /* ═══ R62 #7：Toast 统一固定位 —— 恒在「风格推荐滑块(#rec-style)上方」═══
     用户原话：提示气泡的位置要统一到固定位置、避开风格推荐滑块（像
     "已切换到曲库"那样放滑块上方）。
     此前（R57-4）toast 动态锚「滑块 or dock」：滑块隐藏时落 dock 中央上方、
     滑块闪现时又跳到滑块上方 —— 位置随滑块显隐来回跳，且偶尔与滑块重叠。
     新实现：**恒定锚滑块预设位** —— toast 底边 = dock 顶上方 66px
     （14px 滑块空隙 + 40px 滑块占位高 + 12px 间距）。滑块不管是否 .visible，
     toast 都停在滑块将要出现的高度之上 → 永不重叠、永不跳动。水平恒居中。 */
  function positionToast(){
    const t = $('toast'); if (!t) return;
    const dock = $('dock');
    const L = (window.__layout && window.__layout.get()) || null;   // P1
    const dockTop = dock ? dock.getBoundingClientRect().top
                         : (window.innerHeight - (L?L.dock.bottom:24) - (L?L.dock.height:88));
    const w = t.offsetWidth;
    t.style.bottom = 'auto';
    t.style.left = Math.round(window.innerWidth / 2 - w / 2) + 'px';
    /* dockTop - 空隙 - 滑块高 - 间距（值来自 layout，默认同原 14/40/12） */
    const gr=(L?L.toast.gapRec:14), gs=(L?L.toast.gapSlider:40), gg=(L?L.toast.gap:12);
    t.style.top = Math.max(8, Math.round(dockTop - gr - gs - gg - t.offsetHeight)) + 'px';
  }

  /* ---------- 歌词（背景「歌词海报」，没有独立歌词卡片） ----------
     ⚠ 事故（2026-09-06）：新前端**从来没有**调用过 Poster.setLyric，
       后端也没有 /api/lyrics —— 点「词」按钮永远什么都不显示。
     老 UI（L4844-4865）的语义：
       · 切歌即加载，**不被 lyricOn 门控**（否则第一次开「词」要先等一次取词）
       · 取不到就留空，背景海报照常显示曲名/歌手
       · 逐行推进由主循环的 Poster.seekLyric(position) 完成 */
  let lyricTrackKey = '';
  let lyricLoading = false;
  let lyricReqSeq = 0;
  function loadLyrics(force){
    const key = (st.title || '') + '|' + (st.artist || '');
    if (!force && key === lyricTrackKey) return;
    /* ⚠ R58-5 (#44)：切歌偶现歌词不显示 —— 旧代码 `if (lyricLoading) return;`
       会在「上一次取词还在飞、而歌已切走」时**丢弃新歌的取词**（key 已被更新、
       lyricLoading 又挡掉重入 → 之后 poll 也因 key 相等不再触发 → 永远空白）。
       修法：在飞请求不挡路 —— 递增 seq，旧请求返回时若 seq/key 已过期则丢弃。 */
    lyricTrackKey = key;
    const mySeq = ++lyricReqSeq;
    lyricLoading = true;
    /* 先清空，避免停在上一首的歌词上 */
    try { if (window.Poster) window.Poster.setLyricLines([]); } catch (_) {}
    fetch('/api/lyrics').then(r => r.json()).then(d => {
      if (mySeq !== lyricReqSeq) return;   /* 已有更新的取词请求，丢弃过期结果 */
      const lines = (d && d.ok && Array.isArray(d.lines)) ? d.lines : [];
      try {
        if (window.Poster){
          window.Poster.setLyricLines(lines);
          if (lines.length) window.Poster.seekLyric(st.position);
        }
      } catch (_) {}
    }).catch(() => {}).then(() => {
      if (mySeq === lyricReqSeq) lyricLoading = false;
    });
  }

  /* ---------- 交互 ---------- */

/* ═══════ ui-dock.js (dock 交互/seek/礼花) ═══════ */
/* ══════════════════════════════════════════════════════════
   ui-dock.js — Radio Intelligence 前端模块 · dock 控制 ctl/播放三键/收藏 dislike · 音量 · seek 统一入口
   自 webapi/static/index.html 内嵌 <script> L1809-L2075 拆出（R58-7 #46）
   与原 index.html 内所有 ui-*.js 共享顶层作用域 → 加载顺序即执行顺序，勿打乱
   ══════════════════════════════════════════════════════════ */
  const dock = $('dock');
  dock.addEventListener('mouseenter', () => dock.classList.add('hovering'));
  dock.addEventListener('mouseleave', () => dock.classList.remove('hovering'));
  /* 播放头靠近渐显（原 UI L2798-2806）—— dock 已声明，此处绑定才安全 */
  bindFrameHeadProximity();

  /* ⚠ 播放控制：乐观更新 + 防连点
     原问题：点击后要等后端 + 2s 轮询才有反馈，用户以为没点上 → 连点 →
     "一下子跳两首"。这里点击后**立即**在本地翻转状态给反馈，
     并加 350ms 防连点窗口。 */
  let ctlBusy = 0;
  function ctl(action, optimistic){
    const now = Date.now();
    if (now - ctlBusy < 350) return;    // 防连点
    ctlBusy = now;
    if (optimistic) optimistic();       // 立即给视觉反馈
    action().then(() => setTimeout(poll, 150)).catch(() => setTimeout(poll, 400));
  }

  /* ⚠⚠ force_on 表达的是**意图**（"想进入播放态吗"），绝不是常量 true
     （R47「有的时候播放暂停不了」的根因）：
     后端 `if force_on == playing: return` —— 恒传 true 时，正在播放 →
     True == True → **一次 toggle 都不发** → 暂停永远点不动；
     只有后端把 playing 误判成 False 时才偶然生效，所以是"时灵时不灵"。
       · 想暂停 → force_on=false → 后端 False != True → 真正 toggle
       · 想播放 → force_on=true  → 无当前曲时冷启动拉起电台 */
  $('d-play').onclick = () => {
    const want = !st.playing;
    ctl(() => post('/toggle', 'force_on=' + (want ? 'true' : 'false')),
        () => { st.playing = want; render(); });        // 乐观翻转播放态
  };
  $('d-prev').onclick = () => ctl(() => post('/prev'));
  $('d-next').onclick = () => ctl(() => post('/next'));

  /* ---------- 礼花纸动效（原 ui/ai-radio-player.html L2961 spawnBurst） ----------
     收藏点亮红心时，从按钮中心迸发 16 颗彩色粒子（略微上抛 + 发光）。
     ⚠ 只在"从未收藏 → 点亮"时放；取消收藏不放（沿用原 UI 的 if (!wasFav) 语义）。 */
  function spawnBurst(el){
    if (!el) return;
    const r = el.getBoundingClientRect();
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    const colors = ['#ff5c7a', '#ffd166', '#7bf1a8', '#6ec7ff', '#c9a8ff', '#ff9f6e', '#ff8fa3'];
    const N = 16;
    for (let i = 0; i < N; i++){
      const p = document.createElement('span');
      const ang  = (i / N) * Math.PI * 2 + Math.random() * 0.4;
      const dist = 26 + Math.random() * 34;
      const size = 3 + Math.random() * 4;
      const col  = colors[i % colors.length];
      p.style.cssText = 'position:fixed;left:' + cx + 'px;top:' + cy + 'px;'
        + 'width:' + size + 'px;height:' + size + 'px;border-radius:50%;'
        + 'background:' + col + ';pointer-events:none;z-index:400;'
        + 'box-shadow:0 0 8px ' + col;
      document.body.appendChild(p);
      const dx = Math.cos(ang) * dist, dy = Math.sin(ang) * dist - 16;   // 略微上抛
      p.animate([
        { transform: 'translate(-50%,-50%) scale(1)', opacity: 1 },
        { transform: 'translate(' + (dx - size/2) + 'px,' + (dy - size/2) + 'px) scale(.15)', opacity: 0 }
      ], { duration: 650 + Math.random() * 250, easing: 'cubic-bezier(.16,1,.3,1)' })
       .onfinish = function(){ p.remove(); };
    }
  }

  /* 喜欢 ♥（R49 恢复，老 UI L2985 同语义）：收藏到曲库（我喜欢的音乐）。
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
    /* T22：乐观点亮——点击立即回显（后端仅 20ms，但原来等响应才亮）；
       竞态守卫仍在：响应回来若已切歌则回滚，绝不把 dislike 带到下一首。 */
    const _prevOn = btn.classList.contains('on');
    btn.classList.toggle('on', on);
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
            st.disliked = on;   // R49-27：与后端状态同步，供切歌后正确重置
          } else { btn.classList.toggle('on', _prevOn); }   // T22：切歌了→回滚乐观点亮
        }
        else toast((d && d.error) ? ('失败：' + d.error) : '操作失败');
      })
      .catch(e => { toast('请求失败：' + e.message); });
  };

  /* AI 伴听（口播）/ 电台模式：
     ⚠ 点亮态以**后端为权威** —— 点击后等后端返回再改 UI，
     失败则回滚并提示（避免"点了没反应"或"前端亮了后端没开"）。
     ⚠ R58-1 (#41)：AI 伴听（#d-ai）点击响应后**立即**开合口播栏，
     不等 2s 轮询 —— 关闭时后端仍"始终生成"文案、current_broadcast 残留，
     若不主动 hide，口播栏会赖着不走（用户要"随开关常开常闭"）。 */
  function bindToggle(id, url){
    const btn = $(id);
    btn.addEventListener('click', () => {
      const want = !btn.classList.contains('on');
      /* T23：乐观回显——点击立即 toggle（后端 5ms，但不等它才亮）；
         响应回来用后端权威值校准，失败回滚。 */
      btn.classList.toggle('on', want);
      fetch(url + '?on=' + (want ? 1 : 0), { method:'POST' })
        .then(r => r.json())
        .then(d => {
          if (d && d.ok){
            btn.classList.toggle('on', !!d.on);
            /* R58-1 (#41)：本地开关镜像立即生效（口播栏随动）。
               #d-ai ↔ st.djOn（口播）；#d-radio ↔ st.roamOn（电台续推）。
               后端返回的权威值（非本地 want），失败路径不镜像。 */
            if (id === 'd-ai'){
              st.djOn = !!d.on;
              /* ⚠ R58-1 (#41)：本地开关镜像立即生效（口播栏随动）。
                 #d-ai ↔ st.djOn（口播）；#d-radio ↔ st.roamOn（电台续推）。
                 后端返回的权威值（非本地 want），失败路径不镜像。
                 ⚠ R59：开 = 口播栏**立即常驻**（showAIBar 统一入口，
                 不再只摘 hide 等鼠标靠近/新文案才出现）；关 = 立即淡出。 */
              if (st.djOn){
                showAIBar();
                /* 立即拉一次当前口播文案填充（不等 3s 轮询；WS gate 内则跳过） */
                try { loadBroadcast(); } catch (_) {}
              }
              else hideAIBar();
            } else {
              st.roamOn = !!d.on;
            }
          } else {
            btn.classList.toggle('on', !want);   // T23：失败回滚
            toast((d && d.error) ? ('失败：' + d.error) : '操作失败');
          }
        })
        .catch(e => { btn.classList.toggle('on', !want); toast('请求失败：' + e.message); });
    });
  }
  bindToggle('d-ai',    '/api/dj/toggle');
  bindToggle('d-radio', '/api/radio/toggle');

  $('d-mode').onclick = () => {
    /* ★ 2026-09-19：漫游/推荐下**不许切模式**（后端也会拒），且**不静默**——给一次明确提示。 */
    if (st.modeLocked){
      try { toast('漫游 / 推荐下不可切换播放模式（只按顺序播放）'); } catch (_) {}
      return;
    }
    modeIdx = (modeIdx + 1) % MODES.length;
    /* T21：乐观回显——后端仅 2ms，但高亮原来只读 st.modeEff（poll 2s 才更新），
       点了要等最多 2s 才亮 =「点好几次没反应」。先本地回显，响应回来再校准。 */
    const _target = MODES[modeIdx].key;
    const _prevEff = st.modeEff;
    st.modeEff = _target; render();
    /* ★★ 2026-09-19 修：**路径被拼重复了**。—— `post(path)` 内部就是 `fetch(API + path)`，
       而 `API = '/api/playback'`；这里传过完整路径 '/playback/mode' ⇒ 实际请求变成
       `/api/playback/playback/mode` ⇒ **405 Method Not Allowed** ⇒ set_play_mode 从未被调用
       （所以"模式按钮一直不生效"）。
       对照：同文件其它调用都传相对路径（'/next' '/volume' '/seek'）⇒ 那只传 '/mode' 即可。
       路由确在 `webapi/server.py` L467 `@app.post("/api/playback/mode")`。 */
    post('/mode', 'mode=' + _target).then(d => {
      /* T21：用后端权威值校准，失败回滚 */
      if (d && d.ok){
        const eff = d.mode_effective || d.mode;
        if (eff) st.modeEff = eff;
        if (typeof d.mode_locked === 'boolean') st.modeLocked = d.mode_locked;
        const i = MODES.findIndex(x => x.key === eff);
        if (i >= 0) modeIdx = i;
      } else { st.modeEff = _prevEff; modeIdx = Math.max(0, MODES.findIndex(x => x.key === _prevEff)); }
      render();
    });
  };

  // 音量条点击/拖动
  const volEl = $('vol');
  function setVolFromEvent(e){
    const r = volEl.getBoundingClientRect();
    st.volume = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
    st.muted = false;
    render();
    post('/volume', 'vol=' + st.volume);
  }
  volEl.addEventListener('mousedown', e => {
    const move = ev => setVolFromEvent(ev);
    const up = () => {
      window.removeEventListener('mousemove', move);
      window.removeEventListener('mouseup', up);
    };
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', up);
    setVolFromEvent(e);
  });

  $('vol-icon').onclick = () => {
    st.muted = !st.muted;
    render();
    post('/volume', 'vol=' + (st.muted ? 0 : st.volume));
  };

  // ⚠ 外围进度条精确 seek（原 ui/ai-radio-player.html L2807-2854）
  // 关键：不按 ratio 换算，而是把路径离散采样 120 段，逐点算到鼠标的欧氏距离平方，
  // 取最小者对应的弧长 best → frac2 = best / len。
  // 这是用户反馈"进度条点不准"的根因修复。
  let seeking = false;
  /* ⚠⚠ 后端 position 一律**不直接覆盖** st.position（R46-5「进度条没和播放百分百链接」的真凶）
     主循环（rAF）已在本地逐帧插值推进，而后端值天然滞后：
       2s 轮询的 /api/playback/state 返回的是**上一拍**的位置、WS progress 也有往返延迟。
     原来 applyState() 无条件 `st.position = d.position` → 每 2 秒把进度条拽回旧值，
     表现为"拖动完又弹回去""进度条一跳一跳地回退""进度和声音对不上"。
     两条规则：
       ① 刚 seek 过（seekGuardUntil 守卫期内）或正在拖动 → 完全忽略后端；
       ② 其余时候只有偏差 > REMOTE_TOL_MS 才采信 —— 真·切歌/跳转必然远超阈值，
          正常播放的 1~2s 抖动则被平滑掉。 */
  const REMOTE_TOL_MS = 1200;
  let seekGuardUntil = 0;            // performance.now() 时间轴
  let _seekPostAt = 0;
  function adoptRemotePosition(ms, tol){
    if (typeof ms !== 'number') return;
    if (seeking || performance.now() < seekGuardUntil) return;
    /* ★ 2026-09-19：临近结尾（≤1.5s）**无条件采信**后端位置。
       正常播放走 1200ms 容差（用来平滑抖动），但收尾时这个容差会让进度条落后一小截，
       切歌瞬间表现为"歌播完了、条还剩一小条就跳走"。结尾窗口直接对齐后端即可。 */
    /* ★ 2026-09-19：结尾窗口 1500 → **4000ms**（收尾相位滞后可达 1~2s，
       1.5s 窗会漏采 ⇒ 环差最后一点。窗口放宽后更早采信后端真值。） */
    if (st.duration > 0 && ms >= st.duration - 4000){
      st.position = Math.max(0, Math.min(st.duration, ms));
      if (ms > 0) st.posLive = true;
      return;
    }
    if (Math.abs(ms - st.position) > (tol || REMOTE_TOL_MS)) st.position = ms;
    /* ★ 2026-09-18：后端报过 >0 位置 = 音频真的开始了 → 放开本地插值推进闸门。
       装载期后端恒报 0 → 闸门保持关 → 进度条停在 0 不动（不再"先走再跳回"）。 */
    if (ms > 0) st.posLive = true;
  }
  /* 统一的"跳到某个毫秒"入口：进度条点击/拖动 + 歌词点击走同一条路
     · doPost=false → 只更新视觉（拖动过程），不发请求
     · force=true   → 跳过 140ms 节流（按下 / 松手这一次必须发） */
  function seekToMs(ms, doPost, force){
    if (!(st.duration > 0)) return;
    st.position = Math.max(0, Math.min(st.duration, ms));
    seekGuardUntil = performance.now() + 900;   // 守卫：这段时间内后端旧值一律不采信
    _frameProgLen = -1;            // 强制进度条重绘一次（绕开 0.5px 节流）
    updateFrameProgress();
    if (doPost === false) return;
    const now = performance.now();
    if (!force && now - _seekPostAt < 140) return;   // 节流：mousemove 每帧都会来
    _seekPostAt = now;
    post('/seek', 'ms=' + Math.round(st.position));
  }
  function dockSeek(e, immediate){
    if (!frameLen || !st.duration) return;
    const r = dock.getBoundingClientRect();
    const pt = { x: e.clientX - r.left, y: e.clientY - r.top };
    const len = frameLen;
    let best = 0, bestD = 1e9;
    const steps = 120;
    for (let i = 0; i <= steps; i++){
      const L = len * i / steps;
      const p = framePath.getPointAtLength(L);
      const dx = p.x - pt.x, dy = p.y - pt.y;
      const d = dx * dx + dy * dy;
      if (d < bestD){ bestD = d; best = L; }
    }
    const frac = best / len;
    seekToMs(frac * st.duration, true, !!immediate);
  }

  function beginSeek(e){
    /* 视觉态先挂上：用户看到"正在操作"反馈。
       真正 seek 只在已加载时长时执行，否则撤回动作但视觉态保留 120ms 后撤。 */
    dock.classList.add('seeking');
    if (!st.duration){
      setTimeout(function(){ dock.classList.remove('seeking'); }, 120);
      return;
    }
    seeking = true;
    dockSeek(e, true);              // 按下即跳（force，跳过节流）
    e.preventDefault();
  }
  if (frameHit){
    frameHit.addEventListener('mousedown', beginSeek);
  } else {
    framePath.addEventListener('mousedown', beginSeek);
  }
  /* 拖动中：只做视觉 + 140ms 节流的真实 seek；松手再 force 提交一次最终位置。
     ⚠ 松手必须 force —— 否则最后一次落点可能正落在节流窗口里被丢掉，
        表现为"拖到 3:20 松手，实际跳到 3:12"（进度条与播放对不上）。 */
  window.addEventListener('mousemove', e => { if (seeking) dockSeek(e, false); });
  window.addEventListener('mouseup', () => {
    if (seeking){
      seeking = false;
      dock.classList.remove('seeking');
      seekToMs(st.position, true, true);
    }
  });

  /* ═══════════════════════════════════════════════════════════════
     Orbit 导航（原 ui/ai-radio-player.html L748-755 / L1838-1990）
     三按钮在 R1 圆弧上轮转；隐藏靠 #orbits 原位透明度（**不位移**），
     触发区：以 O 为圆心、R2 = R1+99 为半径的圆弧区。
     ═══════════════════════════════════════════════════════════════ */

/* ═══════ ui-orbit.js (圆盘导航/滚轮路由) ═══════ */
/* ══════════════════════════════════════════════════════════
   ui-orbit.js — Radio Intelligence 前端模块 · orbit 三键几何/轮转/自动隐藏 · column-panel 提示 · 滚轮/点击路由
   自 webapi/static/index.html 内嵌 <script> L2076-L2456 拆出（R58-7 #46）
   与原 index.html 内所有 ui-*.js 共享顶层作用域 → 加载顺序即执行顺序，勿打乱
   ══════════════════════════════════════════════════════════ */
  const ORBIT_MODE_ORDER = ['wander', 'fav', 'plist'];
  const ORBIT_IDS = { wander:'o-wander', fav:'o-fav', plist:'o-playlist' };
  const orbitEls = {};
  ORBIT_MODE_ORDER.forEach(m => { orbitEls[m] = $(ORBIT_IDS[m]); });

  // 几何参数：baseDy/baseDx/baseR1 是 1080p 视口下的基准值，按 innerHeight/2 缩放
  const ORBIT_R = 54;          // 按钮半径
  const ORBIT_EDGE = 36;       // 上下按钮的露边距（越小露出越少）
  const baseDy = 464.8, baseDx = 265, baseR1 = 535;
  const ORBIT_O_SHIFT = 27;
  function orbitGeo(){
    const k = Math.min(1, (innerHeight / 2 - ORBIT_EDGE) / baseDy);
    return { k, R1: baseR1 * k, dx: baseDx * k, dy: baseDy * k, R2: baseR1 * k + 99 };
  }
  function orbitOC(){
    const g = orbitGeo();
    return { x: innerWidth + g.dx - ORBIT_R + ORBIT_O_SHIFT, y: innerHeight / 2 };
  }
  /* ⚠ POS_LIST 是**位置数组**（不是 mode→坐标 的映射），每个 mode 用
     curPos[m]（0/1/2）索引进去 —— 这是老 UI 轮转算法的核心数据结构。
     [0] 中间位（屏幕右缘、视觉中心）  [1] 上  [2] 下
     轮转 = 全体 curPos += delta 再 %3，谁落到 0 谁就是当前栏目。 */
  const ORBIT_POS_LIST = [
    { x: () => innerWidth + orbitGeo().dx - ORBIT_R - orbitGeo().R1 + ORBIT_O_SHIFT, y: () => innerHeight / 2 },
    { x: () => innerWidth - ORBIT_R + ORBIT_O_SHIFT, y: () => innerHeight / 2 - orbitGeo().dy },
    { x: () => innerWidth - ORBIT_R + ORBIT_O_SHIFT, y: () => innerHeight / 2 + orbitGeo().dy },
  ];

  let orbitActive = 'wander';
  let orbitShown = false;
  let orbitAnim = { active:false, from:{}, to:{}, t0:0, dur:600 };
  /* 行进中的装饰性自转（原 UI L1880 rot[] + L1930 rotate(rot[m]*15deg)）。
     ⚠⚠ 2026-09-06 用户实测："图标转过来没有回正，还是歪的"。
     根因有两层：
       ① 我移植时**漏了老 UI L1966 的 `rot[newMode] = 0`** —— 那一行就是"回正"。
       ② 就算补上，老 UI 的 rot 是 `(rot ± 1 + 6) % 6` 的**累加器**，
          停在上下位的按钮会一路歪到 5×15° = **75°**，本身就很难看。
     所以本实现改为**瞬时摆动**：行进途中随进度摆出一个倾角，
     到中点最大、**落位必定回到 0°** —— 静止时三个图标永远正着。 */
  const ORBIT_SPIN_DEG = 20;   // 行进中途的最大倾角
  let orbitSpinDir = 1;        // +1 顺时针 / -1 逆时针，跟随轮转方向
  function orbitSpinDeg(m){
    if (!orbitAnim.active) return 0;                 // 静止 → 恒为 0（正）
    /* 原 UI L1966 `rot[newMode] = 0`：正在转进来的那一个**全程保持正着**
       （它是焦点，倾斜会影响可读性），只有让位的另外两个滚。 */
    if (m === orbitActive) return 0;
    const t = Math.min(1, (performance.now() - orbitAnim.t0) / orbitAnim.dur);
    /* sin(πt)：t=0 → 0，t=0.5 → 峰值，t=1 → 0。两头都是 0 = 必定回正 */
    return Math.sin(Math.PI * t) * ORBIT_SPIN_DEG * orbitSpinDir;
  }
  /* curPos[mode] = 该按钮当前在 POS_LIST 中的槽位（0=中间/当前栏目） */
  const orbitCurPos = { wander:0, fav:1, plist:2 };
  function orbitAngOf(px, py){
    const oc = orbitOC();
    return Math.atan2(py - oc.y, px - oc.x);
  }
  function orbitArcPoint(a){
    const oc = orbitOC();
    return { x: oc.x + orbitGeo().R1 * Math.cos(a), y: oc.y + orbitGeo().R1 * Math.sin(a) };
  }
  function orbitPlace(m, cx, cy){
    const el = orbitEls[m];
    if (!el) return;
    const active = orbitActive === m;
    el.style.zIndex = active ? 60 : 30;
    el.style.opacity = active ? 1 : .72;
    /* 视觉零阈值：sin(π·1) ≈ 1.2e-16，浮点残值会让 transform 里永远挂着
       `rotate(0.00deg)`（无害但没必要），低于 0.05° 一律当正。 */
    const spin = orbitSpinDeg(m);
    const useSpin = Math.abs(spin) > 0.05 ? spin : 0;
    el.style.transform = `translate(${cx - ORBIT_R}px, ${cy - ORBIT_R}px)`
      + (useSpin ? ` rotate(${useSpin.toFixed(2)}deg)` : '');
    el.classList.toggle('active', active);
  }
  /* 当前角度：动画中取插值，静止时取 POS_LIST[curPos] 的角度（老 UI L1911-1919） */
  function orbitCurrentAng(m){
    if (orbitAnim.active){
      const t = Math.min(1, (performance.now() - orbitAnim.t0) / orbitAnim.dur);
      const e = 1 - Math.pow(1 - t, 3);
      return orbitAnim.from[m] + (orbitAnim.to[m] - orbitAnim.from[m]) * e;
    }
    const p = ORBIT_POS_LIST[orbitCurPos[m]];
    return orbitAngOf(p.x(), p.y());
  }
  /* ⚠⚠ 致命坑（2026-09-06 修复，表现为「orbit 按钮完全不动」）：
     旧写法 `if (!orbitLayoutRAF) orbitLayoutRAF = requestAnimationFrame(orbitLayout)`
     —— **rAF 返回的是正整数 id，永远 truthy**，所以 `!orbitLayoutRAF` 恒为 false，
     第一帧之后再也不会排下一帧：既没有轮转动画，resize 后也不重排。
     必须在回调**入口把 id 清零**，否则整条 rAF 链只跑一次。 */
  let orbitLayoutRAF = 0;
  function orbitRequestLayout(){
    if (!orbitLayoutRAF) orbitLayoutRAF = requestAnimationFrame(orbitLayout);
  }
  function orbitLayout(){
    orbitLayoutRAF = 0;                 // ⚠ 先清零，否则下一帧永远排不上
    if (orbitAnim.active){
      const t = Math.min(1, (performance.now() - orbitAnim.t0) / orbitAnim.dur);
      if (t >= 1){
        /* ⚠ 收尾必须**先**置 false 再走下面的静态分支：
           若 rAF 在半路被掐断（切标签页/掉帧），按钮会永远停在倾角上。
           落到静态分支 = 精确槽位 + spin 恒 0 = 必定回正。 */
        orbitAnim.active = false;
      } else {
        const e = 1 - Math.pow(1 - t, 3);
        ORBIT_MODE_ORDER.forEach(m => {
          const a = orbitAnim.from[m] + (orbitAnim.to[m] - orbitAnim.from[m]) * e;
          const p = orbitArcPoint(a);
          orbitPlace(m, p.x, p.y);
        });
      }
    }
    if (!orbitAnim.active){
      ORBIT_MODE_ORDER.forEach(m => {
        const p = ORBIT_POS_LIST[orbitCurPos[m]];
        orbitPlace(m, p.x(), p.y());
      });
    }
    /* 动画期间持续排帧；静止时按需（resize / rotate）触发，不做空转 rAF */
    if (orbitAnim.active) orbitRequestLayout();
  }
  window.addEventListener('resize', orbitRequestLayout);
  /* ⚠ 必须**同步**跑一次首帧：rAF 在页面未被合成时（后台标签/离屏/首帧前）
     可能根本不触发，只靠 rAF 会出现"按钮停在 transform 未设置的原位"。
     同步一次保证加载完立刻就位，rAF 只负责后续动画。 */
  orbitLayout();
  orbitRequestLayout();
  /* 调试/自检钩子（对齐老 UI L1877 的 window.__orbitZone）：
     __orbitZone 供卡片架取同心弧心；__orbit 供验证套件读状态与手动排帧。 */
  window.__orbitZone = function(){
    const oc = orbitOC();
    return { x: oc.x, y: oc.y, r: orbitGeo().R2 };
  };
  window.__orbit = {
    state: function(){
      return {
        curPos: { wander:orbitCurPos.wander, fav:orbitCurPos.fav, plist:orbitCurPos.plist },
        active: orbitActive,
        animating: !!orbitAnim.active,
        /* 每个按钮**实际被应用**的倾角（度）。old UI 的 newMode 恒为 0。 */
        spin: {
          wander: +orbitSpinDeg('wander').toFixed(2),
          fav:    +orbitSpinDeg('fav').toFixed(2),
          plist:  +orbitSpinDeg('plist').toFixed(2)
        }
      };
    },
    layout: function(){ orbitLayout(); },
    /* 卡片架中心 x（老 UI shelfCenterX）—— 验证套件据此断言"卡片贴住 orbit 左缘 22px" */
    shelfCenterX: function(){ return shelfCenterX(); }
  };

  /* ═══════════ 轮转 rotate(delta) —— 老 UI L1959-1992 原样移植 ═══════════
     delta=1：下面那个转到中间（内容上滑）→ 角度逆时针增
     delta=2：上面那个转到中间（内容下滑）→ 角度顺时针减
     做法：全体 curPos += delta 后 %3，谁落到槽位 0 谁成为当前栏目。 */
  function orbitRotate(delta){
    delta = ((delta % 3) + 3) % 3;
    if (delta === 0) return;
    const from = {};
    ORBIT_MODE_ORDER.forEach(m => { from[m] = orbitCurrentAng(m); });
    ORBIT_MODE_ORDER.forEach(m => { orbitCurPos[m] = (orbitCurPos[m] + delta) % 3; });
    const newMode = ORBIT_MODE_ORDER.find(m => orbitCurPos[m] === 0);
    if (newMode) orbitActive = newMode;
    /* 方向一致性：下滑(delta=2)统一顺时针(角减小)，上滑(delta=1)统一逆时针(角增大) */
    const cw = (delta === 2);
    /* 装饰自转的摆动方向跟随轮转方向（clockwise → 正向摆） */
    orbitSpinDir = cw ? 1 : -1;
    const to = {};
    ORBIT_MODE_ORDER.forEach(m => {
      const p = ORBIT_POS_LIST[orbitCurPos[m]];
      let d = orbitAngOf(p.x(), p.y()) - from[m];
      if (cw){ while (d > 0) d -= 2 * Math.PI; }
      else   { while (d < 0) d += 2 * Math.PI; }
      to[m] = from[m] + d;
    });
    orbitAnim = { active:true, from, to, t0:performance.now(), dur:600 };
    orbitRequestLayout();
    /* 切栏目 → 刷新内容（与老 UI refreshColumn() 同语义） */
    if (newMode) onOrbitModeChange(newMode);
  }
  /* 点击：点上面的转下来、点下面的转上去、点中间无动作（老 UI L2004-2010） */
  ORBIT_MODE_ORDER.forEach(m => {
    orbitEls[m].addEventListener('click', () => {
      const delta = ((0 - orbitCurPos[m]) % 3 + 3) % 3;
      if (delta !== 0) orbitRotate(delta);
    });
  });
  /* orbitInZone(x,y): 以 O 为圆心、R2 为半径的**完整圆盘**（含圆弧内部，老 UI L1999-2003） */
  function orbitInZone(x, y){
    const oc = orbitOC();
    const dx = x - oc.x, dy = y - oc.y;
    const r = orbitGeo().R2;
    return dx * dx + dy * dy <= r * r;
  }
  /* cardsInZone(x,y): 以 O 为圆心、R_CARD 为半径、宽 CARD_W 的**同心圆环带**（R47）
     "鼠标进入卡片宽度所在的那个同心圆弧" —— 与 shelfCenterX()/R_CARD 完全同源。
     ⚠ 必须用 |dist - R_CARD| ≤ CARD_W/2 的**环形**而非整圆：
       否则半径 R_CARD 上**屏幕外**的一段弧也会误触发（用户从屏幕左边缘划过也算"在卡片上"）。
     ⚠ 还要限制 dy 落在可见弧带内（sin 角度分量 ≤ 0.85 ≈ 5.6 张卡 ±58°），
       否则屏幕角落的点（dy 大、dx 大）也能凑出 |dist - R_CARD| ≤ CARD_W/2。 */
  function cardsInZone(x, y){
    const oc = orbitOC();
    const dx = x - oc.x, dy = y - oc.y;
    const dist = Math.sqrt(dx * dx + dy * dy);
    const R_CARD = Math.max(160, oc.x - shelfCenterX());
    if (Math.abs(dist - R_CARD) > CARD_W / 2) return false;
    return Math.abs(dy) <= R_CARD * 0.85;
  }
  /* ═══ 3 秒自动隐藏（R47：5s → R57-8：3s → R65：**区内静止也算**）═══
     触发区域 = orbit 圆盘 ∪ 卡片同心圆环。

     ⚠ R65 语义变更（用户原话"对于 orbit 和歌曲卡片，鼠标在识别区三秒钟
       无动作也会自动隐藏"）：
       旧：只有**离开**触发区才起 3s 计时；鼠标一直停在区内 → 永不隐藏。
       新：**区内任何动作**（移动 / 滚轮 / 点击）都算"有动作"→ 立即显示并重开
           3s 空闲计时；一旦静止满 3s **即便鼠标仍停在区内**也隐藏。
           离开触发区仍是 3s 后隐藏，且**区外移动不刷新计时**
           （否则"在外面乱动就永远不隐藏"）。 */
  const AUTO_HIDE_MS = 3000;
  let autoHideTimer = 0;
  function armAutoHide(){
    if (autoHideTimer) clearTimeout(autoHideTimer);
    autoHideTimer = setTimeout(() => {
      autoHideTimer = 0;
      $('orbits').classList.remove('show');
      $('cards').classList.add('autohide');
    }, AUTO_HIDE_MS);
  }
  function pokeShow(){
    $('orbits').classList.add('show');
    $('cards').classList.remove('autohide');
    armAutoHide();           // ⚠ 区内也起计时：3s 无动作即隐藏
  }
  function pokeHide(){
    if (autoHideTimer) return;   // 区外：离开那刻起一次，不随区外移动刷新
    armAutoHide();
  }
  /* ═══ 左侧栏目提示（R49-30 重做）═══
     ⚠ 用户原话："当 orbit 滚到『我的曲库』对应栏目时，提示应出现约 3 秒后消失
       （勿保持永久隐藏/永久可见）。"
     旧实现（R46）是"鼠标靠近 column-panel 就淡入"—— 那是 hover 面板交互，
     不是"orbit 落到某栏目"的提示，而且播放中会在你扫过屏幕左边时冒出来挡路。
     现在：**只在 orbit 轮转/点击落到新栏目时**闪示约 3 秒，然后自动淡出。
     未播放的空闲态保持显示（信息层），一旦开播立即淡出、只随栏目切换闪示。 */
  let colTimer = 0;
  function pokeColumn(ms){
    const el = $('column-panel');
    if (!el) return;
    el.classList.add('show');
    if (colTimer){ clearTimeout(colTimer); colTimer = 0; }
    colTimer = setTimeout(() => {
      colTimer = 0;
      /* 未播放时空闲态恒显（信息层不打扰）；播放中栏目提示到期即淡出 */
      if (!st.playing) return;
      el.classList.remove('show');
    }, ms || 3000);
  }
  function syncColumnPanel(){
    const el = $('column-panel');
    if (!el) return;
    /* 空闲（未播放）恒显作为信息层；播放中由 pokeColumn（orbit 切栏目）控制 */
    if (!st.playing){ el.classList.add('show'); }
  }
  /* 鼠标移动 → ① 负责 orbits + cards 的 5 秒触发区自动隐藏；
     ② **R53：鼠标靠近左侧栏目提示（column-panel）本身 → 立即复活**。
        R49-30 把提示改成"只在 orbit 落到栏目时闪 3s"，结果用户鼠标离开再
        靠近（想再瞄一眼"我的曲库/歌单中心"标题）永远不再出现 —— 用户点名。
        修复：column-panel 进入其包围盒热区（含周边 12px 手感）就 pokeColumn，
        但**不打断**正在播放的 3s 闪示节奏：接近即延长/重现，离开由各自逻辑收走。 */
  function columnProbeZone(x, y){
    const el = $('column-panel');
    if (!el) return false;
    try {
      const r = el.getBoundingClientRect();
      if (!r.width && !r.height) return false;
      return x >= r.left - 16 && x <= r.right + 16
          && y >= r.top - 12 && y <= r.bottom + 12;
    } catch (_) { return false; }
  }
  window.addEventListener('mousemove', e => {
    const inZ = orbitInZone(e.clientX, e.clientY) || cardsInZone(e.clientX, e.clientY);
    if (inZ) pokeShow(); else pokeHide();
    /* R53：鼠标重新靠近左侧栏目提示 → 立即复活。
       · 播放中：pokeColumn(3000) 闪示 3s 后自动淡出（不打断 R49-30 节奏）
       · 空闲中：pokeColumn 只加 show 不移除（空闲态本就恒显，无副作用）
       ⚠ 不能加 st.playing 门控：离开又回来时若无 show 类，靠几何区域判定复活。 */
    try {
      if (columnProbeZone(e.clientX, e.clientY)) pokeColumn(3000);
    } catch (_) {}
  });
  /* R65：点击也算「有动作」—— 区内点一下立即复活并重开 3s 空闲计时。
     （捕获阶段 + 只读几何判定，不拦截任何事件） */
  window.addEventListener('pointerdown', e => {
    if (orbitInZone(e.clientX, e.clientY) || cardsInZone(e.clientX, e.clientY)) pokeShow();
  }, true);
  /* orbit 落到某栏目（点击 orbit 按钮 / 滚轮轮转）→ 左侧栏目提示闪示约 3s */
  function columnFlashOnMode(){
    if (st.playing) pokeColumn(3000);
    else syncColumnPanel();        // 空闲态：恒显信息层
  }
  /* 开始播放 → 空闲态信息层淡出；播放中若没在闪示周期内则隐藏 */
  function hideColumnIfIdle(){
    const el = $('column-panel');
    if (!el) return;
    if (colTimer) return;          // 正在 3s 闪示：交给计时器收走
    el.classList.remove('show');
  }
  /* 播放状态变化（WS playing / 轮询）→ 同步 column-panel：
     · 停止播放 → 空闲信息层恒显
     · 开始播放 → 隐藏（除非正在 3s 闪示周期内） */
  function syncColumnByPlaying(playing){
    if (!playing){ syncColumnPanel(); }
    else { hideColumnIfIdle(); }
  }

  /* ═══════════ 滚轮路由（老 UI L4157-4177 原样移植） ═══════════
     优先级（R46-5 在最前面加了 ⓪ 歌词选择）：
       ⓪ 歌词选择态（鼠标停在歌词带内）→ 上下换行
       ① 鼠标真正悬停在卡片架某张卡(.scard)上 → 浏览卡片
       ② orbit 大圆盘（**包含按钮围成的圆弧内部**）→ 轮转三按钮
       ③ 其余区域 → **不拦截**，允许设置/歌词/队列/详情等面板内部原生滚动
     ⚠ 绝不能用 #shelf 容器包围盒判定：它铺满右缘全高，会吞掉圆弧内部的滚轮。
       必须用 document.elementFromPoint 精确到卡片元素。 */
  /* ═══ R65：独立交互区（面板 / 弹层）═══
     指针落在这些面板内部时，滚轮与点击**只归面板自己**：
     绝不翻歌词、绝不步进卡片、绝不轮转 orbit。
     ⚠ 这是「设置页一滚，后面的 orbit 和卡片也跟着转」的根因防护：
       orbitInZone / cardsInZone 是**纯几何**判定，右侧 380px 的设置抽屉在几何上
       正好压在 orbit 圆盘上 → 光靠"指针在抽屉内就放行"仍会漏到 orbitRotate。
       故统一用一个前置闸门：命中独立交互区（或抽屉开着）→ 直接 return。 */
  const PANEL_SEL = '#side-panel, #settings-panel, #theme-panel, #qr-modal, #aibar, #eq-panel';
  function isolatedZone(x, y){
    /* 抽屉开着 → 抽屉内外都不许动主界面（用户点名"设置页滚动别带背景"） */
    try { if (anyDrawerOpen()) return true; } catch (_) {}
    try {
      const el = document.elementFromPoint(x, y);
      return !!(el && el.closest(PANEL_SEL));
    } catch (_) { return false; }
  }
  function shelfCardZone(x, y){
    const el = document.elementFromPoint(x, y);
    return !!el && !!el.closest('.scard');
  }
  /* 歌词带：海报上歌词所在的横带（poster.js 画在 H*0.52 附近）。
     ⚠ #bg / canvas 是 pointer-events:none，只能按几何判定，不能靠 elementFromPoint。
     ⚠ 横向要**避开卡片架**，否则鼠标在卡片上滚会同时翻歌词。
     ⚠ R67-6：识别区缩到「3 行歌词刚好包住」的矩形 + 5% 上下边距（旧实现是
       0.68×0.46 视口百分比 → 暗区也算歌词带，按钮外一圈能跳歌）。 */
  function lyricZone(x, y){
    /* ⚠⚠ R67-6 修复：const P 曾只声明在下方第一段 try{} 里 —— const 是
       块级作用域，第二段 try{} 读 P 直接 ReferenceError → catch → 恒 false
       → 歌词识别区**完全失效**（wheel/click 全不响应，且不报错，藏得深）。
       提到函数顶层，两段共用。 */
    let P = null;
    try { P = window.Poster; } catch (_) {}
    try {
      if (!P || !P.isLyricOn || !P.isLyricOn() || !P.lyricCount || P.lyricCount() < 1) return false;
    } catch (_) { return false; }
    /* R67-6：几何用 Poster 暴露的「3 行宽高 + 中心」+ 5% 边距。
       _lastBoxW/H 是画布坐标系 → 经 #poster-canvas 的 getBoundingClientRect 转到屏幕坐标。 */
    let boxW, boxH, boxCx, boxCy;
    try {
      boxW = P.lyricBoxW(); boxH = P.lyricBoxH();
      boxCx = P.lyricBoxCx(); boxCy = P.lyricBoxCy();
    } catch (_) { return false; }
    if (!boxW || !boxH) return false;   /* 还没画过第一帧 */
    const cv = document.getElementById('poster-canvas');
    if (!cv) return false;
    const r = cv.getBoundingClientRect();
    /* 画布 → 屏幕：scaleX = r.width/W, scaleY = r.height/H；offset = r.left/top */
    const sx = (r.width || 0) > 0 ? r.width : 1;
    const sy = (r.height || 0) > 0 ? r.height : 1;
    const W = (cv.width  || sx);
    const H = (cv.height || sy);
    const scCx = r.left + (boxCx / W) * sx;
    const scCy = r.top  + (boxCy / H) * sy;
    const scW  = (boxW / W) * sx * 1.05;   /* +5% 横向（左右各 2.5%）*/
    const scH  = (boxH / H) * sy * 1.05;   /* +5% 上下 */
    if (x < scCx - scW / 2 || x > scCx + scW / 2) return false;
    if (y < scCy - scH / 2 || y > scCy + scH / 2) return false;
    /* ⚠⚠ R53 独立识别区：side-panel 打开时整块面板是**独立交互区**，
       其关闭叉/滚动条/行按钮绝不能被当成"歌词带"——
       否则点 sp-close 会命中 lyricHitTest → seek 到某句歌词（用户点名）。
       先在元素层排除（面板是实 DOM），再退几何。 */
    try {
      const el = document.elementFromPoint(x, y);
      /* ⚠ R56：右侧两张玻璃抽屉（设置卡/换肤卡）也是独立识别区，与歌词带互斥。
         ⚠ R65：统一用 isolatedZone 的面板清单（含 #qr-modal），
           歌单详情面板放大居中后必然压住歌词带 —— 元素层排除是唯一可靠手段
           （几何层排除不了：它就是盖在歌词上面）。 */
      if (el && el.closest(PANEL_SEL)) return false;
    } catch (_) {}
    try { if (x > shelfCenterX() - 170) return false; } catch (_) {}
    return true;
  }
  window.addEventListener('wheel', e => {
    /* ⓪ R65：独立交互区（设置/换肤抽屉、侧边面板、扫码弹层、口播条）→
       只交浏览器原生滚动，绝不触发翻歌词 / 卡片步进 / 圆盘轮转。
       ⚠ 旧实现写成「抽屉开着且指针不在抽屉内才 return」，指针**在抽屉内**时
         会继续往下走 —— 而 orbit 识别区是纯几何判定，抽屉在几何上压着 orbit，
         于是在设置页滚动时背景 orbit 照样转（用户点名）。现在一律闸门拦截。 */
    if (isolatedZone(e.clientX, e.clientY)) return;
    /* ⓪' 歌词浏览（R49-22）：滚轮换行（浏览行放大），**2 秒无操作自动回正**。
       ⚠ 浏览只由滚轮驱动 —— 鼠标悬停不换行不放大（用户原话"通过滚轮调节，
       而不是鼠标放上去就能调节"）。 */
    if (lyricZone(e.clientX, e.clientY)){
      const P = window.Poster;
      P.wheelLyric(e.deltaY > 0 ? 1 : -1);
      return;
    }
    if (shelfCardZone(e.clientX, e.clientY)){
      pokeShow();                         // R65：区内活动 → 重开 3s 空闲计时
      step(e.deltaY > 0 ? 1 : -1);        // 卡片架浏览：下滑下一张
      return;
    }
    if (orbitInZone(e.clientX, e.clientY)){
      pokeShow();                         // R65：同上（滚轮也是"动作"）
      orbitRotate(e.deltaY > 0 ? 2 : 1);  // 下滑：上面转到中间；上滑：下面转到中间
      return;
    }
    /* 其余区域：不切歌，交给浏览器原生滚动 */
  }, { passive: true });

  /* 点击歌词带 → 跳到那一行并立即回正（"点击歌词跳到对应位置"）。force=true 立即生效。
     ⚠ R49-22：**没有** mousemove 悬停联动 —— 换行只认滚轮（wheelLyric），
     鼠标靠近歌词只放大视觉不变行；点击是唯一鼠标驱动 seek。 */
  window.addEventListener('click', e => {
    const P = window.Poster;
    if (!P || !P.lyricHitTest) return;
    /* R65：与滚轮同一道闸门 —— 面板/弹层上、或抽屉开着时，点击不落到歌词 seek */
    if (isolatedZone(e.clientX, e.clientY)) return;
    if (!lyricZone(e.clientX, e.clientY)) return;
    const i = P.lyricHitTest(e.clientY);
    if (i < 0) return;
    const ms = P.lyricTimeMs(i);
    if (ms == null) return;
    seekToMs(ms, true, true);
    P.setLyricHover(-1);
    toast('跳到歌词：' + (P.getLyric().lines[i] || {}).text);
  });

  /* 栏目元数据（R45 修正语义：fav=曲库，plist=歌单）
     ⚠ 老 UI 是 fav=收藏/plist=歌单，但新架构 favorites 已废弃，
        用户指定 fav 这个 orbit 位接**曲库**，plist 接**在线歌单**。 */

/* ═══════ ui-column.js (栏目头/漫游滑块) ═══════ */
/* ══════════════════════════════════════════════════════════
   ui-column.js — Radio Intelligence 前端模块 · 栏目头 COL_META/syncColumnHead · ORBIT_API · rs 熟悉度滑块 · 播放全部/音源切换
   自 webapi/static/index.html 内嵌 <script> L2457-L2612 拆出（R58-7 #46）
   与原 index.html 内所有 ui-*.js 共享顶层作用域 → 加载顺序即执行顺序，勿打乱
   ══════════════════════════════════════════════════════════ */
  const COL_META = {
    wander: { kicker: 'RADIO · 漫游',    title: '星际漫游' },
    fav:    { kicker: 'LIBRARY · 曲库',  title: '我的曲库' },
    plist:  { kicker: 'PLAYLISTS · 歌单', title: '歌单中心' },
  };
  const modeName = m => m === 'wander' ? '漫游' : m === 'fav' ? '曲库' : '歌单';
  function syncColumnHead(){
    const meta = COL_META[orbitActive];
    if (!meta) return;
    const head = $('column-head');
    head.classList.remove('swap'); void head.offsetWidth; head.classList.add('swap');
    $('ch-kicker').textContent = meta.kicker;
    $('ch-title').textContent  = meta.title;
    /* 副标题显示**当前栏目的卡片数**（不是待播队列长度）。
       ⚠ 2026-11-17 修文案错接：R44 时代定的单位规则「歌单 N 个歌单，其余待播 N 首」
       在 R45 纠正数据源映射（fav 从 queue→library、plist→library→playlists）后没同步改，
       于是「曲库」显示「待播 N 首」（其实是曲库总数）、「漫游」显示「待播 N 首」
       （其实是推荐货架数）——把栏目卡片数谎称成待播队列。数据源本身是对的，只错文案。
       改为各栏目真实单位，不再出现「待播」二字（待播队列是独立 /api/queue 入口）。
       ⚠ cardItems 由后文 let 声明：若在声明前调用本函数会撞 TDZ，
       且 **typeof 对 TDZ 中的 let/const 变量同样抛 ReferenceError**（不是返回 undefined）。
       必须用 try/catch 兜住。 */
    let n = 0;
    try { n = Array.isArray(cardItems) ? cardItems.length : 0; } catch(_) { n = 0; }
    const SUB_UNIT = {
      wander: '首推荐',   // 推荐货架数（/api/recommendations）
      fav:    '首歌曲',   // 曲库曲目数（/api/library），不是收藏、不是待播
      plist:  '个歌单',   // 在线歌单数（/api/playlists）
    };
    $('ch-sub').textContent = n + ' ' + (SUB_UNIT[orbitActive] || '首');
    /* 音源切换 tabs：仅 plist（歌单）栏目显示 —— 歌单才有音源概念 */
    const ps = $('plist-src');
    if (ps) ps.style.display = (orbitActive === 'plist') ? 'flex' : 'none';
    /* 播放全部按钮：wander/fav/plist 都有意义 ——
       ⚠ R72：歌单栏目也显示。语义 = 播放**当前居中那张歌单卡**对应的歌单内容
       （用户拍板：「当前卡片对应歌单内容入队」，等价于进详情点播放全部，但不展开面板）。
       只有歌单卡片是"容器"（一卡一歌单），漫游/曲库才是整列数据源。
       按钮文案/title 随栏目切换：plist 用「播放歌单」避免误读成"全部歌单都播"。 */
    const pa = $('col-playall');
    if (pa){
      const isPlist = (orbitActive === 'plist');
      pa.style.display = (orbitActive === 'wander' || orbitActive === 'fav' || isPlist) ? '' : 'none';
      const tx = pa.querySelector('.pmc-tx');
      if (tx) tx.textContent = isPlist ? '播放歌单' : '播放全部';
      pa.title = isPlist ? '播放当前歌单全部曲目' : '播放全部';
    }
  }

  /* ═══════════ 切栏目 → 刷新内容（老 UI refreshColumn 同语义） ═══════════
     R45 正确语义（用户指定）：
       wander = 漫游/推荐  → /api/recommendations
       fav    = 曲库       → /api/library      （不是收藏！favorites 已废弃）
       plist  = 歌单       → /api/playlists    （在线歌单） */
  const ORBIT_API = { wander:'reco', fav:'library', plist:'plist' };
  function onOrbitModeChange(mode){
    curView = ORBIT_API[mode] || 'reco';
    syncColumnHead();
    loadView({ reset: true });
    toast('已切换到「' + modeName(mode) + '」');
    /* R49-30：orbit 落到栏目（轮转/点击）→ 左侧提示闪示约 3s 后消失 */
    try { columnFlashOnMode(); } catch (_) {}
    /* R49-31：漫游滑块随栏目显隐 —— 仅 wander(漫游) 栏目显示 */
    try { syncRecMode(); } catch (_) {}
  }

  /* ═══ R49-31 漫游滑块（移植老 UI L3023-3070）═══
     仅「漫游」栏目显示；切到漫游闪现 ~5s，鼠标靠近滑块常显、离开淡出。
     档位 0..3 → /api/roam/familiarity POST（radio.set_familiarity）。 */
  const RS_LABELS = ['更熟悉', '略熟悉', '略陌生', '更陌生'];
  const rsSlider = $('rs-slider'), rsVal = $('rs-val'), rsBox = $('rec-style');
  let rsTimer = 0, rsHovering = false, rsAutoPending = false;
  function rsShow(){ if (rsBox) rsBox.classList.add('visible'); }
  function rsHide(){ if (rsBox) rsBox.classList.remove('visible'); }
  function rsAutoShow(){
    if (!rsBox) return;
    rsShow();
    clearTimeout(rsTimer); rsTimer = 0;
    rsAutoPending = true;
    rsTimer = setTimeout(() => {
      rsTimer = 0; rsAutoPending = false;
      if (!rsHovering) rsHide();
    }, 5000);
  }
  function syncRecMode(){
    if (!rsBox) return;
    const on = orbitActive === 'wander';
    rsBox.classList.toggle('on', on);
    if (on) rsAutoShow();
    else { rsHide(); clearTimeout(rsTimer); rsTimer = 0; rsHovering = false; rsAutoPending = false; }
  }
  /* 初始化：本地缓存立即回填（零闪烁，R57-4「刷新后先默认后跳回」根因：GET 是
     异步的，fetch 回来前滑块显示 HTML 默认 value=1 → 先默认后跳记忆值）。
     三通道：localStorage 立即首帧 → /api/roam/familiarity 后端权威 → /api/settings
     user_config.rec_pref 校正（跨设备/重启的最终持久化层）。 */
  (function initRs(){
    /* ① localStorage 同步回填 —— 不等网络，首帧即上次档位 */
    try {
      const f = JSON.parse(localStorage.getItem('r54.ui') || '{}');
      if (f && typeof f.roamFam === 'number' && rsSlider){
        const v = Math.max(0, Math.min(3, f.roamFam | 0));
        rsSlider.value = v; rsSlider.style.setProperty('--fill', (v / 3 * 100) + '%');
        if (rsVal) rsVal.textContent = RS_LABELS[v];
      }
    } catch (_) {}
    /* ② /api/roam/familiarity 内存态校正（同时反映给后端算法用） */
    fetch('/api/roam/familiarity').then(r => r.json()).then(d => {
      if (d && d.ok && rsSlider){
        const v = Math.max(0, Math.min(3, +(d.value || 1)));
        rsSlider.value = v; rsSlider.style.setProperty('--fill', (v / 3 * 100) + '%');
        if (rsVal) rsVal.textContent = (d.labels && d.labels[v]) || RS_LABELS[v];
        try {
          const f = JSON.parse(localStorage.getItem('r54.ui') || '{}');
          f.roamFam = v; localStorage.setItem('r54.ui', JSON.stringify(f));
        } catch (_) {}
      }
    }).catch(() => {});
    /* ③ R67-3：user_config.rec_pref 跨重启持久化层校正（设置卡里 rec_pref
       控件已删，这里就是它的唯一读写入口）。后端启动时把 rec_pref 同步到
       radio.roam_familiarity，启动 = 上次滑块位置。 */
    fetch('/api/settings').then(r => r.json()).then(d => {
      if (!d || !d.ok) return;
      const v = d.values && typeof d.values.rec_pref === 'number' ? d.values.rec_pref : null;
      if (v == null || !rsSlider) return;
      const clamped = Math.max(0, Math.min(3, v | 0));
      rsSlider.value = clamped; rsSlider.style.setProperty('--fill', (clamped / 3 * 100) + '%');
      if (rsVal) rsVal.textContent = RS_LABELS[clamped];
      try {
        const f = JSON.parse(localStorage.getItem('r54.ui') || '{}');
        f.roamFam = clamped; localStorage.setItem('r54.ui', JSON.stringify(f));
      } catch (_) {}
    }).catch(() => {});
  })();
  if (rsSlider){
    rsSlider.addEventListener('input', () => {
      const v = +rsSlider.value;
      if (rsVal) rsVal.textContent = RS_LABELS[v];
      /* T8：已滑/未滑两色轨道（非整体变暗）；fill = 当前档位百分比 */
      if (rsSlider) rsSlider.style.setProperty('--fill', (v / 3 * 100) + '%');
      try {
        const f = JSON.parse(localStorage.getItem('r54.ui') || '{}');
        f.roamFam = v; localStorage.setItem('r54.ui', JSON.stringify(f));
      } catch (_) {}
      fetch('/api/roam/familiarity?level=' + v, { method:'POST' }).catch(() => {});
      /* R67-3：同步写 user_config.rec_pref —— 跨重启持久化（设置卡里 rec_pref 控件已删） */
      fetch('/api/settings/set?path=rec_pref&value=' + encodeURIComponent(String(v)),
            { method: 'POST' }).catch(() => {});
    });
  }
  window.addEventListener('mousemove', e => {
    if (!rsBox || !rsBox.classList.contains('on')) return;
    const r = rsBox.getBoundingClientRect();
    const inZone = e.clientX >= r.left - 8 && e.clientX <= r.right + 8
                && e.clientY >= r.top - 8 && e.clientY <= r.bottom;
    rsHovering = inZone;
    if (inZone){ clearTimeout(rsTimer); rsTimer = 0; rsAutoPending = false; rsShow(); }
    else if (!rsAutoPending) rsHide();
  });
  /* ═══ 播放全部：推荐与曲库是**两个不同入口**（R46-5 用户点名别弄混）═══
       wander(漫游/推荐) → /api/reco/play-all     数据源 = 推荐货架
       fav(曲库)         → /api/library/play-all  数据源 = 本地曲库
     两者在后端都走「替换播放队列 + 从第一首开播」语义（upcoming 整体重写，
     不是追加）。⚠ 旧实现**恒定**打 reco → 在曲库栏目点播放全部播的是推荐。

     ⚠ R72：歌单栏目也接入 —— 用户拍板语义 = 「当前卡片对应歌单内容入队」。
     歌单卡片是"一卡一歌单"的容器，卡片架当前居中位（spring.target 取整）
     对应的就是用户正在看的那张歌单 → 走 /api/playlists/play 整单入队
     （后端 play_playlist 内部 play_tracks + 从第 index 首开播，index=0 从第一首）。 */
  const PLAYALL_API = { wander: '/api/reco/play-all', fav: '/api/library/play-all' };
  $('col-playall').addEventListener('click', () => {
    if (orbitActive === 'plist'){
      /* 取卡片架当前居中卡 = 用户当前聚焦的歌单卡（spring.target 取整） */
      let pl = null;
      try {
        const i = Math.max(0, Math.min(cardItems.length - 1, Math.round(spring.target)));
        const e = cardItems[i];
        if (e && e.platform && e.ext_id != null) pl = e;
      } catch (_) { pl = null; }
      if (!pl){
        toast('请先选中一个歌单');
        return;
      }
      toast('正在播放歌单…');
      fetch('/api/playlists/play?platform=' + encodeURIComponent(pl.platform)
            + '&ext_id=' + encodeURIComponent(pl.ext_id) + '&index=0', { method: 'POST' })
        .then(r => r.json()).then(d => {
          if (d && d.ok){ setTimeout(poll, 260); }
          else toast('播放失败：' + ((d && d.error) || ''));
        }).catch(() => toast('播放失败'));
      return;
    }
    const url = PLAYALL_API[orbitActive];
    if (!url) return;                       // 其它无意义栏目隐藏
    fetch(url, { method: 'POST' })
      .then(r => r.json())
      .then(d => {
        if (!d || !d.ok) return;
        /* 队列已被整体替换 → 立刻拉一次播放状态，让进度条/卡片对上新曲 */
        setTimeout(poll, 260);
        setTimeout(() => loadView({ reset: true }), 420);
      })
      .catch(() => {});
  });
  /* 音源切换 tabs（仅 plist 栏目展示）：切换后按音源重新拉歌单 */
  $('plist-src').querySelectorAll('button').forEach(btn => {
    btn.addEventListener('click', () => {
      $('plist-src').querySelectorAll('button').forEach(b => b.classList.remove('on'));
      btn.classList.add('on');
      plistSource = btn.dataset.s || '';
      /* ⚠ 必须带 reset：否则签名判定/位置保留会让老卡片原封不动 */
      if (curView === 'plist') loadView({ reset: true });
      var plistLabel = btn.getAttribute('data-label') || plistSource || '';
      toast(plistSource ? ('歌单音源：' + plistLabel) : '歌单音源：全部');
    });
  });

  /* ═══════════════════════════════════════════════════════════════
     侧边面板（#side-panel）—— 队列/历史/曲库 三 tab + 行操作
     原 ui/ai-radio-player.html L533-577 / L3130-3333
     ═══════════════════════════════════════════════════════════════ */

/* ═══════ ui-sidepanel.js (侧栏四 tab) ═══════ */
/* ══════════════════════════════════════════════════════════
   ui-sidepanel.js — Radio Intelligence 前端模块 · 侧边面板 side-panel 队列/历史/曲库 行操作
   自 webapi/static/index.html 内嵌 <script> L2613-L2861 拆出（R58-7 #46）
   与原 index.html 内所有 ui-*.js 共享顶层作用域 → 加载顺序即执行顺序，勿打乱
   ══════════════════════════════════════════════════════════ */
  let sideOpen = false;
  let sideTab = 'queue';
  const HEART_SVG  = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z"/></svg>';
  const BROKEN_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z"/><path d="M4 4l16 16"/></svg>';
  const PLAY_SVG   = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>';
  const NEXT_SVG   = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h13M12 6l6 6-6 6"/></svg>';
  const RECO_SVG   = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>';
  const DEL_SVG    = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 6l12 12M18 6l-12 12"/></svg>';

  /* actions 集合（原 UI rowSide L3130-1333 的 actions 三态） */
  function rowSideActions(){
    if (sideTab === 'history') return ['rm'];
    if (sideTab === 'queue')   return ['play', 'fav', 'dislike', 'rm'];
    if (sideTab === 'library') return ['play', 'next', 'reco', 'del'];
    /* 歌单详情：在线曲目无本地 db id，只能整单起播（/api/playlists/play） */
    if (sideTab === 'plist')   return ['play'];
    return [];
  }
  /* i = 行下标（歌单详情模式用作 play_playlist 的 index） */
  function drowHtml(s, i){
    const idx = (i == null) ? '' : (' data-pl="' + i + '"');
    const acts = rowSideActions();
    const favOn = !!s._fav, disOn = !!s._dis;
    const playing = !!s.current;
    const inl = '<img src="' + esc(s.cover || '') + '" alt="" onerror="this.style.display=\'none\'">'
      + '<div class="dm"><div class="dn">' + esc(s.title || '未知曲目') + '</div>'
      + '<div class="da">' + esc(s.artist || '') + '</div></div>';
    let btns = '';
    if (acts.length){
      const b = (act, title, inner, on) =>
        '<button class="dact" data-act="' + act + '" title="' + title + '"'
        + (on ? ' data-on="1"' : '') + '>' + inner + '</button>';
      btns = '<div class="dacts">';
      if (acts.includes('play'))    btns += b('play',    '播放', PLAY_SVG, false);
      if (acts.includes('next'))    btns += b('next',    '下一首播放', NEXT_SVG, false);
      if (acts.includes('reco'))    btns += b('reco',    '加入推荐', RECO_SVG, false);
      if (acts.includes('fav'))     btns += b('fav',     '收藏', HEART_SVG, favOn);
      if (acts.includes('dislike')) btns += b('dislike', '不喜欢', BROKEN_SVG, disOn);
      if (acts.includes('del'))     btns += b('del',     '从曲库删除', DEL_SVG, false);
      if (acts.includes('rm'))      btns += b('rm',      '不播放（仅移除）', DEL_SVG, false);
      btns += '</div>';
    }
    return '<div class="drow' + (playing ? ' playing' : '') + '" data-id="' + esc(s.id) + '"'
      + idx + '>'
      + inl + btns + '</div>';
  }
  function _disKey(s){
    if (s.source && String(s.source) !== 'local'){
      return 'e:' + s.source + ':' + (s.ext_id != null ? s.ext_id : '');
    }
    return s.id != null ? ('i:' + s.id) : '';
  }
  /* 行内按钮点击事件代理 */
  $('sp-body').addEventListener('click', e => {
    const btn = e.target.closest('.dact');
    const row = e.target.closest('.drow');
    if (!row) return;
    const id = row.dataset.id;
    /* 歌单详情行：在线曲目没有本地 db id，/api/play/track/{id} 要求 int 会直接 422。
       → 整单入队 + 后端按 index 跳（内部修正 jump_to(index-1)）。 */
    const plIdx = (sideTab === 'plist' && row.dataset.pl) ? parseInt(row.dataset.pl, 10) : -1;
    const playRow = () => {
      if (plIdx >= 0 && sidePlist){
        toast('正在播放歌单…');
        fetch('/api/playlists/play?platform=' + encodeURIComponent(sidePlist.platform)
              + '&ext_id=' + encodeURIComponent(sidePlist.ext_id)
              + '&index=' + encodeURIComponent(plIdx), { method: 'POST' })
          .then(r => r.json()).then(d => {
            if (d && !d.ok) toast('播放失败：' + ((d && d.error) || ''));
          }).catch(() => { toast('播放失败'); });
        return;
      }
      fetch('/api/play/track/' + encodeURIComponent(id), { method: 'POST' })
        .then(r => r.json()).then(d => {
          if (d && d.ok) { /* 播放成功，无需提示 */ }
          else toast('播放失败');
        }).catch(() => {});
    };
    if (btn){
      e.stopPropagation();
      const act = btn.dataset.act;
      if (act === 'play'){
        playRow();
      } else if (act === 'next'){
        /* ⚠ R44 曾接到 /api/playback/next —— 那是**切下一首**，语义相反。
           插入待播首位走 /api/queue/play-next。 */
        fetch('/api/queue/play-next?track_id=' + encodeURIComponent(id), { method: 'POST' })
          .then(r => r.json()).then(() => loadSidePanel()).catch(() => {});
      } else if (act === 'reco'){
        fetch('/api/reco/play?track_id=' + encodeURIComponent(id), { method: 'POST' })
          .then(r => r.json()).then(() => loadSidePanel()).catch(() => {});
      } else if (act === 'del'){
        fetch('/api/library/delete?id=' + encodeURIComponent(id), { method: 'POST' })
          .then(r => r.json()).then(() => loadSidePanel()).catch(() => {});
      } else if (act === 'rm'){
        if (sideTab === 'history'){
          fetch('/api/history/' + encodeURIComponent(id), { method: 'DELETE' })
            .then(r => r.json()).then(() => loadSidePanel()).catch(() => {});
        } else {
          fetch('/api/queue/remove?track_id=' + encodeURIComponent(id), { method: 'POST' })
            .then(r => r.json()).then(() => loadSidePanel()).catch(() => {});
        }
      } else if (act === 'dislike'){
        /* 「不喜欢」= AI 避雷 + 从待播/推荐货架剔除。
           ⚠ 只有 dislike 有后端接口（/api/dislike/add|remove）。
           ⚠ R62 #23：entry 带完整条目 → 在线歌落库带 title/artist。 */
        const entry = sideItems.find(it => String(it.id) === String(id));
        if (!entry) return;
        const key = _disKey(entry);
        const on = (btn.dataset.on === '1');
        const tq = 'key=' + encodeURIComponent(key)
                 + '&title=' + encodeURIComponent(entry.title || '')
                 + '&artist=' + encodeURIComponent(entry.artist || '');
        fetch('/api/dislike/' + (on ? 'remove' : 'add') + '?' + tq,
              { method: 'POST' })
          .then(r => r.json()).then(d => {
            if (d && d.ok){
              btn.dataset.on = on ? '' : '1';
              btn.classList.toggle('on', !on);
              toast(on ? '已取消不喜欢' : '已加入不喜欢');
            }
          }).catch(() => {});
      } else if (act === 'fav'){
        /* ⚠ favorites 整套已废弃（后端无 /api/favorite 接口）。
           原 UI 的 ♥ 语义 = 收藏到曲库；曲库(library) 是**独立**入口，
           列表项收藏无后端支持 → 不请求，只给提示（避免 404 噪音）。
           保留按钮仅为视觉对齐原 UI。 */
        toast('收藏已并入曲库页管理');
      }
    } else {
      /* 行点击 = 播放 */
      playRow();
    }
  });

  let sideItems = [];
  /* 歌单详情模式：sideTab === 'plist' 时按 (platform, ext_id) 拉曲目 */
  let sidePlist = null;      // { platform, ext_id, name }
  function loadSidePanel(){
    const body = $('sp-body');
    if (sideTab === 'plist'){
      if (!sidePlist){ body.innerHTML = '<div class="sp-empty">未选择歌单</div>'; return; }
      body.innerHTML = '<div class="sp-empty">加载歌单曲目…</div>';
      fetch('/api/playlists/tracks?platform=' + encodeURIComponent(sidePlist.platform)
            + '&ext_id=' + encodeURIComponent(sidePlist.ext_id))
        .then(r => r.json()).then(d => {
          const entries = (d && d.ok) ? (d.entries || []) : [];
          sideItems = entries;
          if (!entries.length){
            body.innerHTML = '<div class="sp-empty">该歌单暂无曲目</div>';
            return;
          }
          body.innerHTML = entries.map((s, i) => drowHtml(s, i)).join('');
        }).catch(() => {
          body.innerHTML = '<div class="sp-empty">歌单曲目加载失败</div>';
        });
      return;
    }
    const url = sideTab === 'history' ? '/api/history'
              : sideTab === 'library' ? '/api/library'
              : '/api/queue';
    fetch(url).then(r => r.json()).then(d => {
      if (!d || !d.ok){ body.innerHTML = '<div class="sp-empty">加载失败</div>'; return; }
      const entries = d.entries || d.tracks || d.history || [];
      sideItems = entries;
      if (!entries.length){
        body.innerHTML = '<div class="sp-empty">' + (
          sideTab === 'history' ? '暂无播放历史'
          : sideTab === 'library' ? '曲库为空'
          : '队列为空' ) + '</div>';
        return;
      }
      body.innerHTML = entries.map(drowHtml).join('');
    }).catch(() => {
      $('sp-body').innerHTML = '<div class="sp-empty">加载失败</div>';
    });
  }
  /* 点歌单卡 → 侧边面板展示该歌单曲目。
     ⚠ R65：歌单详情是**居中大面板**（.plist-mode：屏幕正中 + 1.5× 尺寸），
       与队列/历史/曲库那种"贴着触发图标上方"的小面板是两套定位：
       · 进 plist → 清掉上一次可能残留的 inline left/bottom/top/right
         （positionSidePanel 写过内联样式，会盖掉 CSS 的居中），再挂 .plist-mode；
       · 切回其它 tab / 关面板 → 摘掉 .plist-mode，交还 positionSidePanel 定位。 */
  function openPlaylist(platform, ext_id, name){
    if (!platform || !ext_id) return;
    sidePlist = { platform: platform, ext_id: String(ext_id), name: name || '歌单' };
    sideTab = 'plist';
    sideOpen = true;
    const p = $('side-panel');
    if (p){
      p.style.left = ''; p.style.right = ''; p.style.top = ''; p.style.bottom = '';
      p.classList.add('plist-mode');
    }
    $('sp-title').textContent = sidePlist.name;
    /* R72：歌单详情头部「播放全部」仅 plist-mode 显示（其它 tab 无此语义） */
    const spa = $('sp-playall');
    if (spa) spa.style.display = '';
    if (p) p.classList.add('open');
    loadSidePanel();
  }
  /* 模式名（面板标题）—— 与老 UI openSidePanel 里的三元式同义 */
  const SIDE_TITLE = { queue:'播放列表', history:'播放历史', library:'曲库', plist:'歌单' };
  /* ⚠ 一个入口 = 一个模式，面板里**没有 tab**（R46-4）。
     播放列表就只装播放列表，绝不混入历史/曲库（老 UI 原样：#d-queue / #d-history 分开）。
     R49：面板**在触发图标正上方弹出**（positionSidePanel 按图标实时定位），
     且每个入口有自己的触发空间（图标 + 面板本体），互不重叠。 */
  function positionSidePanel(){
    const p = $('side-panel');
    const btn = $(sideTab === 'history' ? 'd-history'
               : sideTab === 'queue'   ? 'd-queue' : null);
    if (!p || !btn) return;
    const L = (window.__layout && window.__layout.get()) || null;   // P1
    const r = btn.getBoundingClientRect();
    /* 面板水平中心 = 图标中心；底边 = 图标顶边上方 gap px */
    const pw = L?L.side.width:348, gp = L?L.side.gap:14;
    p.style.left = Math.max(8, Math.min(innerWidth - pw,
                     r.left + r.width / 2 - pw/2)) + 'px';
    p.style.right = 'auto';
    p.style.bottom = (innerHeight - r.top + gp) + 'px';
  }
  function openSidePanel(tab){
    if (tab) sideTab = tab;
    if (sideTab !== 'plist') sidePlist = null;
    sideOpen = true;
    /* R65：非歌单详情 → 摘掉居中大面板模式（回到"图标上方"定位） */
    const sp = $('side-panel');
    if (sp) sp.classList.remove('plist-mode');
    positionSidePanel();
    $('sp-title').textContent = SIDE_TITLE[sideTab] || '播放列表';
    /* R72：非歌单模式隐藏「播放全部」（只有歌单详情才有整单播放按钮） */
    const spa = $('sp-playall');
    if (spa) spa.style.display = 'none';
    if (sp) sp.classList.add('open');
    loadSidePanel();
  }
  function closeSidePanel(){
    sideOpen = false;
    const sp = $('side-panel');
    if (sp) sp.classList.remove('open');
    /* ⚠ 延迟摘 .plist-mode：立刻摘会让面板在淡出动画途中瞬移回图标上方。
       等 .35s 过渡走完再摘（与基座 transition 同一时长）。 */
    if (sp && sp.classList.contains('plist-mode')){
      setTimeout(() => {
        if (!sideOpen) sp.classList.remove('plist-mode');
      }, 380);
    }
  }
  $('sp-close').addEventListener('click', closeSidePanel);
  /* ═══ R72：歌单详情「播放全部」—— 整单入队从第一首开播（不走逐行播放）。
     与卡片行 play（带 index 跳转）同后端 /api/playlists/play；index=0 = 第一首起播。
     ⚠ stopPropagation：防止冒泡到 window click（歌词 seek）或点外部关闭。 */
  const spPlayAll = $('sp-playall');
  if (spPlayAll){
    spPlayAll.addEventListener('click', e => {
      e.stopPropagation();
      if (!sidePlist) return;
      toast('正在播放歌单…');
      fetch('/api/playlists/play?platform=' + encodeURIComponent(sidePlist.platform)
            + '&ext_id=' + encodeURIComponent(sidePlist.ext_id) + '&index=0',
            { method: 'POST' })
        .then(r => r.json()).then(d => {
          if (d && d.ok){ setTimeout(poll, 260); }
          else toast('播放失败：' + ((d && d.error) || ''));
        }).catch(() => { toast('播放失败'); });
    });
  }
  /* ⚠⚠ R69 修复：点 sp-close 关面板的**同一记 click** 会继续冒泡到
     ui-orbit.js 的 window click（歌词 seek）。那个 handler 用 elementFromPoint
     复查命中 —— 但 closeSidePanel 先把 .open 摘了（面板 pointer-events 立即
     失效），elementFromPoint 已穿透到背景歌词带 → 关个面板把歌 seek 了（用户点名）。
     设置卡没这问题：抽屉按钮/关闭钮与歌词带不重叠，且 anyDrawerOpen 全程兜底。
     这里照设置卡的处理思路：在关闭钮上**截断**整条点击链路 —— stopPropagation
     掐掉 window 冒泡段；stopImmediatePropagation 连 sp-close 同元素其它监听也
     不放过（防未来加逻辑被穿透）；同时再补一个 document 捕获层闸门 —— 面板
     **刚关（open 摘下 / plist-mode 未摘）的那一瞬**，后续元素/全局监听照样
     可能用 elementFromPoint 误判，凡"正在淡出面板内按下的 click"一律归零。 */
  const spClose = $('sp-close');
  if (spClose && !spClose._r69Bound){
    spClose._r69Bound = true;
    spClose.addEventListener('click', e => {
      e.stopPropagation();
      e.stopImmediatePropagation();
    });
  }
  window.addEventListener('resize', () => { if (sideOpen) positionSidePanel(); });
  /* R53 触发空间改造（用户原话）：
     "dock 栏中，播放列表和播放历史要做成**单击出现**的而不是鼠标靠近出现。
      然后你要给这个窗口做**单独的识别区**，不然点击它们关闭的叉，
      会触发到歌词的识别区导致歌词被点击。"
     → ① 移除 mouseenter 即开 + 450ms 自动收起（hover 弹窗彻底下线）；
       ② 单击按钮开合（toggle）；点面板**外部任意处**关闭；
       ③ 面板区域 = 独立识别区：歌词点击/滚轮识别（lyricZone）必须把它排除，
          否则 sp-close / 面板内部滚动会误触发歌词 seek 或翻行。 */
  function bindSideBtn(id, tab){
    const btn = $(id);
    if (!btn) return;
    btn.addEventListener('click', ev => {
      ev.stopPropagation();
      if (sideOpen && sideTab === tab){ closeSidePanel(); return; }
      openSidePanel(tab);
    });
  }
  bindSideBtn('d-queue',   'queue');
  bindSideBtn('d-history', 'history');
  /* 点面板外部任意处关闭（单击语义；捕获阶段判断，避免面板内点击误关） */
  document.addEventListener('click', ev => {
    if (!sideOpen) return;
    const t = ev.target;
    if (!t || !t.closest) return;
    if (t.closest('#side-panel') || t.closest('#dock')) return;
    closeSidePanel();
  }, true);
  /* ⚠ 初始化栏目头部**不要在这里调用**！
     syncColumnHead() 内部读 cardItems（本文件后面用 let 声明），
     在此处同步调用会撞上 TDZ（Cannot access 'cardItems' before initialization）。
     统一挪到文件末尾初始化区（与 loadView() 一起）。 */

  /* ---------- Phase 3：推荐货架（3D 弧排布 + 弹簧物理，沿用原 #shelf 动画） ---------- */

/* ═══════ ui-cards.js (卡片墙弹簧布局) ═══════ */
/* ══════════════════════════════════════════════════════════
   ui-cards.js — Radio Intelligence 前端模块 · 卡片架 shelf/Spring/__cards · 推荐/曲库/歌单卡片渲染
   自 webapi/static/index.html 内嵌 <script> L2862-L3310 拆出（R58-7 #46）
   与原 index.html 内所有 ui-*.js 共享顶层作用域 → 加载顺序即执行顺序，勿打乱
   ══════════════════════════════════════════════════════════ */
  const CARD_W = 372, CARD_H = 120;
  const SHELF_CARD_HALF = CARD_W * 1.06 / 2;   // 中心卡半宽（含居中 1.06 放大）
  /* 相邻卡的圆心角（老 UI SHELF_ARC_STEP = 0.135，原样值）。
     相邻卡纵向间距 ≈ R_CARD × sin(ARC_STEP)，R_CARD≈808 → 约 109px，
     卡高 120 → 邻卡**边缘叠压约 11px**：斜向收进中心卡下方，配 z-index 递减
     + 景深模糊形成分层堆叠。调小=更密叠压更多，调大=弧更弯更松。
     ⚠ 旧值 0.30 是"居中弧心"那套的配套参数，同心弧下会稀疏到 239px（几乎不叠压）。 */
  /* P1：卡片弧度读 layout（默认原值 0.135） */
  const ARC_STEP = (window.__layout && window.__layout.get().cards.arcStep) || 0.135;
  /* 景深 Z：卡片沿 pz 后退会经 perspective(1500px) 投影把卡片**拉离真弧**，
     老 UI 实测半径从 677 漂到 624 → 压到 40，保留立体感又贴合弧线。 */
  const STEP_Z   = 40;
  const VISIBLE  = 5.6;    // 中心 ±5.6 张可见，其余隐藏（省渲染）
  /* ═══ shelfCenterX()（老 UI L4267-4280 原样移植）═══
     卡片右缘与「中间位 orbit 按钮左缘」留 **22px** 间隙 —— 这是老 UI 解决
     "卡片被 orbit 按钮压住 → 悬停命中按钮而非卡片" 的几何约束。
     ⚠ 绝不能读 #o-wander 的实时位置：切栏目时三按钮沿弧轮转，谁在中间会变，
       卡片架的水平基准跟着变 → 各栏目卡片不在同一条竖线上。
       必须走几何常量：中间位按钮左缘 = O.x - R1 - ORBIT_R。 */
  function shelfCenterX(){
    const z = orbitOC(), g = orbitGeo();
    const obLeft = z.x - g.R1 - ORBIT_R;
    const box = $('cards');
    const shelfW = (box && box.clientWidth) || innerWidth;
    const cx = Math.min(shelfW - 280, obLeft - 22 - SHELF_CARD_HALF);
    /* ⚠ R49-28：用户要求"卡片整体左移 10px" —— 中心 x 再减 10，
       卡片架整体（含弧心 R_CARD / 触发带 cardsInZone）都随 shelfCenterX 左移，
       保持同心几何一致（不破坏触发区与布局联动）。
       ⚠⚠ R57-8：用户要求"所有卡片架整体**再**左移 10px" —— 在 R49-28 的
       -10 基础上累计到 **-20**（任务 #38 原话）。中心卡更靠左，留出右侧呼吸。 */
    return Math.max(8 + SHELF_CARD_HALF, cx - 20);
  }

  /* 弹簧（阻尼振子）：原 UI 的 shelf.spring，用于卡片轮转的跟手回弹 */
  function Spring(p){ this.p = p||0; this.v = 0; this.target = this.p; }
  Spring.prototype.setTarget = function(t){ this.target = t; };
  Spring.prototype.update = function(dt){
    const k = 180, d = 26;                       // 刚度 / 阻尼
    const a = (this.target - this.p) * k - this.v * d;
    this.v += a * dt; this.p += this.v * dt;
    if (Math.abs(this.target - this.p) < 0.001 && Math.abs(this.v) < 0.01){
      this.p = this.target; this.v = 0;
    }
  };

  const spring = new Spring(0);
  let cards = [], cardItems = [], hoverIdx = -1, openAt = 0, lastT = 0, raf = 0;
  let bootFastReveal = true;    // T20：首屏用快速淡入(错峰20ms/时长150ms)，切栏目用原速
  /* ⚠ 数据签名：轮询每 5s 会回调 buildCards，若无条件重建 DOM 会
       ① spring 被重置到 0 → "自动跳到第一个"
       ② 重放进场动画 → "反复刷新闪烁"
     只有条目 id / 当前曲**真正变化**时才重建。 */
  let lastSig = '';
  let lastCurId = null;
  /* ⚠⚠ 歌单条目**根本没有 id 字段**（后端 _pl_meta_to_dict 只给
     platform / ext_id / name / cover / creator / track_count）。
     旧签名一律取 e.id → 每个歌单都签成空串，只要**数量相同**签名就相同
     → buildCards 直接 return，切音源时**老卡片原封不动**
     （用户报的"点切换音源不会自动刷新，得把功能换掉才出现不同音源的歌单"）。 */
  function keyOf(e){
    if (!e) return '';
    if (curView === 'plist') return (e.platform || '') + ':' + (e.ext_id || '');
    return String(e.id == null ? '' : e.id);
  }
  function sigOf(list){
    return (list || []).map(e => keyOf(e) + (e && e.current ? ':c' : '')).join('|');
  }
  /* 切栏目 / 切音源 → 下一批数据到达时必须**复位到第一张**
     （老 UI 的 shelf 进出场语义）。只靠签名相同就 return 会停在旧位置。 */
  let pendingReset = false;

  /* 把当前播放的卡滚到中心（原 UI：切歌后卡片架跟随当前曲） */
  function scrollToCurrent(force){
    const i = cardItems.findIndex(e => e && e.current);
    if (i < 0) return;
    const id = cardItems[i] && cardItems[i].id;
    if (force || id !== lastCurId){ lastCurId = id; spring.setTarget(i); }
  }

  function esc(s){
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
      {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  const PAL = ['#7c6cff','#3ddc97','#ff9f6e','#6ec7ff','#c9a8ff','#ffd166'];

  function buildCards(entries){
    /* 签名相同**且**没要求复位 → 数据没变，直接返回
       （不动 DOM、不动 spring、不重放动画） */
    const sig = sigOf(entries);
    if (sig === lastSig && cards.length && !pendingReset) return;
    const isFirst = !cards.length;
    const reset = pendingReset || isFirst;   // 切栏目/切音源 = 复位，不是"保留位置"
    pendingReset = false;
    lastSig = sig;

    const box = $('cards');
    box.innerHTML = '';
    /* ⚠ 保留浏览位置（原 UI 的 keepPos 语义），否则续拉/刷新后弹回开头；
       但**切栏目/切音源必须归零**，否则新栏目的卡片停在旧栏目滚到的位置。 */
    const keepTarget = reset ? 0 : Math.max(0, Math.round(spring.target));
    if (reset){
      spring.p = 0; spring.v = 0; spring.target = 0;
      /* ⚠ 复位后必须把"当前曲"基准同步到**新栏目**的当前曲，否则下一次 5s 轮询
         会因为 lastCurId 变化又把卡片拽到当前曲 —— 等于复位白做。 */
      const cur0 = (entries || []).find(e => e && e.current);
      if (cur0) lastCurId = (cur0.id == null ? null : cur0.id);
    }
    cards = []; cardItems = entries || [];
    hoverIdx = -1;
    /* 进场错峰动画：首次 + 切栏目/切音源都播；**轮询刷新不重放**（避免反复闪烁） */
    /* T20：启动首屏跳过错峰（用户要推荐页加载即出）；切栏目照常播 */
    /* T20：首屏也播动画但用快速参数（用户选「快但留一点淡入感」）；
       切栏目用原速。bootFastReveal 只标记本次 build 是首屏。 */
    openAt = reset ? performance.now() : 0;
    bootFastReveal = !!isFirst;  // 只有启动首屏用快速档；切栏目(isFirst=false)用原速
    (entries || []).forEach((e, i) => {
      const el = document.createElement('div');
      const isPlist = (curView === 'plist');
      el.className = isPlist ? 'scard is-plist' : 'scard';
      /* ⚠ R49-29：current 卡**立即**落 .playing，不等 layout 帧。
         进场揭示期首帧 rev≤0 会 `continue` 跳过中心卡；offscreen/低帧率下
         rAF 可能迟迟不来 → 高亮延迟甚至永不落（用户要"最醒目"）。 */
      if (e && e.current) el.classList.add('playing');
      el.dataset.i = i;
      /* ⚠ 默认调色板先上（避免封面异步到达前 .sc-prog / ::before 都是黑灰），
         封面 img.onload 后会被 sampleColor 覆盖（cross-origin 失败时仍用 PAL）。 */
      const toneA = PAL[i % PAL.length];
      const toneB = PAL[(i + 2) % PAL.length];
      el.style.setProperty('--tone-a', toneA);
      el.style.setProperty('--tone-b', toneB);
      const name  = isPlist ? (e.name  || '未命名歌单') : (e.title || '未知曲目');
      const sub   = isPlist
        ? ((e.creator ? e.creator + ' · ' : '') + (e.track_count || 0) + ' 首')
        : (e.artist || '未知艺术家');
      const badge = isPlist
        ? (e.platform && e.platform !== 'local' ? (e.platform_label || '在线') : '本地')
        : (e.reason || '');
      /* ⚠ 渐变底板**恒定**铺上，封面 img 叠在其上：
         否则一旦 URL 加载失败（坏图/防盗链 403/69 字节的残缺缓存），
         onerror 把 img 藏掉后就是一块**纯黑空白**（没有兜底色）。
         底板恒在 → img 失败自然露出程序化封面（与原 UI coverOfSong 的
         PALETTES 兜底同语义）。 */
      const bg = 'background:linear-gradient(140deg,'
        + PAL[i % PAL.length] + ',rgba(20,18,34,.9))';
      /* R47：行操作 + 渐变（取样）进度条 —— 与漫游卡片一致；
         plist 卡片是容器（点开看曲目），不挂行操作。 */
      /* R49：行操作补回「喜欢 ♥」（老 UI shelfActsHTML L4359：推荐卡=播放+喜欢+不喜欢+删除），
         点亮态来自后端 favorited（bridge._mark_fav 按 fav_key 填充）；plist 卡不挂行操作。 */
      const favOn = !!e.favorited;
      const acts = isPlist ? '' :
        '<div class="sc-acts">'
        + '<button data-act="play" title="播放"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg></button>'
        + '<button data-act="fav" title="喜欢（收藏到曲库）"' + (favOn ? ' class="on"' : '') + '><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20s-7-4.5-9.5-9A5.4 5.4 0 0 1 12 6.4 5.4 5.4 0 0 1 21.5 11C19 15.5 12 20 12 20z"/></svg></button>'
        + '<button data-act="dislike" title="不喜欢（AI 将避开）"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20s-7-4.5-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.5-7 10-7 10z"/><line x1="5" y1="5" x2="19" y2="19"/></svg></button>'
        + '<button data-act="delete" title="删除"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="6" y1="6" x2="18" y2="18"/><line x1="6" y1="18" x2="18" y2="6"/></svg></button>'
        + '</div>';
      const prog = isPlist ? '' :
        '<div class="sc-prog"><span class="fill"></span></div>';
      el.innerHTML =
        '<div class="sc-cover" style="' + bg + '">'
          + (e.cover ? '<img src="' + esc(e.cover) + '" alt="" '
                       + 'onerror="this.style.display=\'none\'">' : '') + '</div>'
        + '<div class="sc-info">'
        +   '<div class="sc-name">' + esc(name) + '</div>'
        +   '<div class="sc-sub">' + esc(sub) + '</div>'
        + '</div>'
        + (badge ? '<span class="sc-chip">' + esc(badge) + '</span>' : '')
        + '<div class="sc-eq"><i></i><i></i><i></i><i></i><i></i></div>'
        + acts + prog;
      /* 悬停浮起（原 UI 的 floatMix） */
      el.addEventListener('mouseenter', () => { hoverIdx = i; });
      el.addEventListener('mouseleave', () => { if (hoverIdx === i) hoverIdx = -1; });
      el.addEventListener('click', ev => {
        /* ⚠ R50：行操作按钮（play/fav/dislike/delete）优先 —— 事件冒泡**由内向外**，
           .scard 的监听**先于** #cards 容器委托执行；不在这里排除按钮，
           点喜欢/不喜欢会先触发"滚到中心/播放当前卡"（用户实测"点不喜欢变成播放"）。
           容器委托里的 stopPropagation 太晚 —— 内层监听那时已经跑完了。
           ⚠ 参数名必须避开外层 forEach 的条目 e（否则 e.id 变成 event.id）。 */
        if (ev.target && ev.target.closest && ev.target.closest('button[data-act]')) return;
        const d = Math.abs(i - spring.target);
        if (d >= 0.5){ spring.setTarget(i); return; }   // 非居中卡先滚到中心
        if (isPlist){
          /* 歌单卡 → 拉歌单详情曲目（在线歌单） */
          openPlaylist(e.platform, e.ext_id, e.name);
          return;
        }
        /* ⚠ 点播接口按栏目选：
           推荐/队列 在推荐货架里 → play_from_reco（快）
           历史/曲库 **不在货架里** → 必须回库查 Track 再 start_radio，
           用 play_from_reco 会找不到 → "曲库点播放没反应"（踩过）。 */
        const url = (curView === 'reco' || curView === 'queue')
          ? ('/api/reco/play?track_id=' + encodeURIComponent(e.id))
          : ('/api/play/track/' + encodeURIComponent(e.id));
        fetch(url, {method:'POST'})
          /* T24：后端 play 同步完成（<5ms），400ms 等待改为立即 poll + 60ms 刷新卡片 */
          .then(() => { try { poll(); } catch (_e) {} setTimeout(loadCards, 60); }).catch(() => {});
      });
      box.appendChild(el);
      cards.push({ el: el, lift: 0, vis: false, _z:'', _op:'', _tf:'', _filt:'' });
      /* ⚠ 取样封面颜色（drawImage 2×1 取两端主色）→ 覆盖默认调色板 →
         .sc-prog 的 --tone-a/--tone-b 与 .scard::before 的色洗随之变成封面色。
         跨域图会 SecurityError → 保留默认 PAL[i]。 */
      if (e.cover && !isPlist){
        const imgEl = el.querySelector('.sc-cover img');
        if (imgEl) sampleCoverColor(imgEl, (c1, c2) => {
          if (c1) el.style.setProperty('--tone-a', c1);
          if (c2) el.style.setProperty('--tone-b', c2);
        });
      }
    });
    if (!cards.length){
      box.innerHTML = '<div style="position:absolute;left:50%;top:50%;'
        + 'transform:translate(-50%,-50%);color:rgba(232,230,242,.45);font-size:13px">'
        + 'music has not arrived.</div>';
    }
    /* 恢复浏览位置（上限为末张）；首次构建则滚到**当前播放的卡** */
    const maxI = Math.max(0, cards.length - 1);
    spring.setTarget(Math.min(keepTarget, maxI));
    spring.p = spring.target;
    /* 定位到当前播放卡：
       ⚠ R49-29 曲库当前播放卡要"最醒目 + 自动定位" —— 用户切到曲库/推荐栏目
       时希望直接看到正在播的卡在中间，而不是停在第一张。
       · 首次构建（isFirst）：必滚当前曲；
       · 切栏目/切音源（reset）且**数据里确实有当前曲**：也滚到当前曲
         （此前刻意不滚是怕"切栏目弹走"，但用户要看的是正在播的那首，
         数据里没有 current 才保留第一张）;
       · 普通轮询刷新（!reset）：仅当 current id 变化才拽过去（scrollToCurrent(false)）。 */
    if (isFirst || (reset && (entries || []).some(e => e && e.current))){
      scrollToCurrent(true);
    } else if (!reset) scrollToCurrent(false);
    if (!raf) raf = requestAnimationFrame(layout);
    /* 进度条立即对齐到当前 position（卡片刚渲染完就显示对的位置） */
    updateCardProgress();
    /* ★★ 2026-11-17 修「副标题数字串栏目」：**数据到达后必须刷新栏目头**。
       此前 syncColumnHead() 只在 onOrbitModeChange()/ui-init 里调用，而 loadView()
       内部是 `fetch().then()` 异步 ⇒ 调用那一刻 cardItems 还是**上一个栏目**的数据：
         · 切到曲库 → 副标题显示上一栏（漫游）的数字；
         · 切到漫游 → 显示上一栏（可能歌单 limit=30）的数字。
       用户实测症状：曲库实际 84 首却显示「3 首歌曲」、漫游实际 5 首却显示「30 首推荐」。
       cardItems 已在本函数 :112 赋值为 entries ⇒ 此处才是刷新副标题的正确时机。
       ⚠ syncColumnHead 定义在 ui-column.js（更早加载的全局函数声明），可安全调用；
         仍用 typeof 兜底，避免加载顺序变化时报错。 */
    try { if (typeof syncColumnHead === 'function') syncColumnHead(); } catch (_) {}
  }
  /* 封面取色（drawImage 2×1 像素）—— 跨域图会被 SecurityError 吞掉，自动保留 PAL 兜底 */
  function sampleCoverColor(img, cb){
    function _try(){
      try {
        const cv = document.createElement('canvas');
        cv.width = 2; cv.height = 1;
        const ctx = cv.getContext('2d');
        ctx.drawImage(img, 0, 0, 2, 1);
        const d = ctx.getImageData(0, 0, 2, 1).data;
        const c1 = 'rgb(' + d[0] + ',' + d[1] + ',' + d[2] + ')';
        const c2 = 'rgb(' + d[4] + ',' + d[5] + ',' + d[6] + ')';
        cb(c1, c2);
      } catch (_) { cb(null, null); }
    }
    if (img.complete && img.naturalWidth) _try();
    else img.addEventListener('load', _try, { once: true });
  }
  /* 同步当前曲卡片的渐变进度条宽度（主循环每帧调） */
  function updateCardProgress(){
    if (!cards.length || !(st.duration > 0)) return;
    const pct = Math.max(0, Math.min(1, st.position / st.duration));
    const pctW = (pct * 100).toFixed(2) + '%';
    for (let i = 0; i < cards.length; i++){
      const c = cards[i];
      const e = cardItems[i];
      if (!c || !e) continue;
      const fill = c.el.querySelector('.sc-prog .fill');
      if (!fill) continue;
      if (e.current){
        if (c._pw !== pctW){ fill.style.width = pctW; c._pw = pctW; }
      } else if (c._pw !== '0%'){ fill.style.width = '0%'; c._pw = '0%'; }
    }
  }
  /* ═══ 卡片行操作事件委托（R47：play / dislike / delete）═══
     全部挂在 #cards 容器上 —— 避免每张卡都挂 listener，且 buildCards 重建也不丢事件。
     ⚠ 必须 e.stopPropagation()：否则点击按钮会冒泡到 .scard 的 click 处理 →
     又触发"播放当前卡"（用户只想要删除，点完就播了 → "怎么删了还播出来"）。
     ⚠ dislike key 用 (source || '') + ':' + ext_id，与 #d-dislike / 侧边面板保持一致
     （e.id 是本地 db id，可能与 dislike 表里的 ext_id 重复 → 重复删/加）。 */
  $('cards').addEventListener('click', e => {
    const btn = e.target.closest && e.target.closest('button[data-act]');
    if (!btn) return;
    e.stopPropagation();
    const cardEl = btn.closest('.scard');
    if (!cardEl) return;
    const i = +cardEl.dataset.i;
    const entry = cardItems[i];
    if (!entry) return;
    const act = btn.dataset.act;
    if (act === 'play'){
      const url = (curView === 'reco' || curView === 'queue')
        ? ('/api/reco/play?track_id=' + encodeURIComponent(entry.id))
        : ('/api/play/track/' + encodeURIComponent(entry.id));
      fetch(url, {method: 'POST'})
        /* T24：同上，卡片整体点击点播提速 */
        .then(() => { try { poll(); } catch (_e) {} setTimeout(loadCards, 60); }).catch(() => {});
    } else if (act === 'fav'){
      /* R49：喜欢/取消喜欢指定曲目 → /api/favorite/track（稳定键 source+ext_id / db id）。
         点亮瞬间放礼花（老 UI L4408 卡片 ♥ 同款）；失败回滚点亮态。
         ⚠ R72：卡片 ♥ 的本质是「把这首歌收进曲库」。条目上就有完整元数据
         （后端 _norm_entry 已带 title/artist/album/cover），必须**一并 POST** ——
         没播放过的在线曲在库里没有行，后端要靠这些字段 + source/ext_id 拼出
         占位 path 才能真入库；只传 source/ext_id/track_id 时后端落库读不到
         path → 红心点亮但曲库永远不出现（用户实测 bug）。R62 dislike 同款解法。 */
      const was = btn.classList.contains('on');
      const want = !was;
      btn.classList.toggle('on', want);
      if (want){ try { spawnBurst(btn); } catch (_) {} }
      const qs = 'source=' + encodeURIComponent(entry.source || '')
               + '&ext_id=' + encodeURIComponent(entry.ext_id != null ? entry.ext_id : '')
               + (entry.id != null ? '&track_id=' + encodeURIComponent(entry.id) : '')
               + '&title=' + encodeURIComponent(entry.title || '')
               + '&artist=' + encodeURIComponent(entry.artist || '')
               + '&album=' + encodeURIComponent(entry.album || '')
               + '&cover=' + encodeURIComponent(entry.cover || '');
      fetch('/api/favorite/track?' + qs, {method: 'POST'})
        .then(r => r.json())
        .then(d => {
          if (d && d.ok){ btn.classList.toggle('on', !!d.favorited); }
          else { btn.classList.toggle('on', was); toast((d && d.error) || '操作失败'); }
        })
        .catch(() => { btn.classList.toggle('on', was); });
    } else if (act === 'dislike'){
      /* ⚠ R50：key 规则必须与 dock/#d-dislike、radio.fav_key 一致 ——
         'e:<source>:<ext_id>'（在线）/ 'i:<db id>'（本地）。
         旧写法拼 '<平台>:xxx' / '123'（无前缀）→ 与后端判定键（fav_key）
         永不相等 → 卡片 dislike 永不生效（AI 不避雷、点亮态不同步）。
         ⚠ R62 #23：卡片上就有完整条目 —— 把 title/artist 一并 POST，
         在线歌落 dislike 库才有真实歌名（否则后端按 key 解析不到 Track →
         title/artist 为 NULL → 不喜欢列表显示「未知歌手/未知歌曲」）。 */
      let dkey = '';
      if (entry.source && String(entry.source) !== 'local'
          && entry.ext_id != null && entry.ext_id !== ''){
        dkey = 'e:' + entry.source + ':' + entry.ext_id;
      } else if (entry.id != null){
        dkey = 'i:' + entry.id;
      }
      if (!dkey) return;
      const on = !btn.classList.contains('on');
      btn.classList.toggle('on', on);
      const qs = 'key=' + encodeURIComponent(dkey)
               + '&title=' + encodeURIComponent(entry.title || '')
               + '&artist=' + encodeURIComponent(entry.artist || '');
      fetch('/api/dislike/' + (on ? 'add' : 'remove') + '?' + qs,
            {method: 'POST'})
        .catch(() => { btn.classList.toggle('on', !on); });
    } else if (act === 'delete'){
      const id = entry.id;
      if (id == null) return;
      let url = null;
      if      (curView === 'library')  url = '/api/library/delete?id=' + encodeURIComponent(id);
      else if (curView === 'reco')     url = '/api/reco/remove?track_id=' + encodeURIComponent(id);
      else if (curView === 'queue')    url = '/api/queue/remove?track_id=' + encodeURIComponent(id);
      else if (curView === 'history')  url = '/api/history/' + encodeURIComponent(id);
      else return;
      /* 乐观移除：滑出 + 淡出 → 失败时回滚；不在此 loadCards（要等动画走完才看着不刺眼） */
      const oldTf = cardEl.style.transform;
      const oldOp = cardEl.style.opacity;
      cardEl.style.transition = 'transform .25s ease, opacity .25s ease';
      cardEl.style.transform = (oldTf || '') + ' translateX(40px)';
      cardEl.style.opacity = '0';
      setTimeout(() => fetch(url, {method: 'POST'}).then(r => {
        if (!r.ok) throw new Error(r.status);
        setTimeout(loadCards, 220);
      }).catch(() => {
        cardEl.style.transition = '';
        cardEl.style.transform = oldTf;
        cardEl.style.opacity = oldOp;
      }), 60);
    }
  });

  /* 逐帧布局：极坐标 → 直角坐标，脏值比较后写 DOM（原 UI shelfTick） */
  function layout(now){
    raf = requestAnimationFrame(layout);
    if (!cards.length) return;
    const dt = Math.min(0.05, (now - lastT) / 1000 || 0.016);
    lastT = now;
    spring.update(dt);
    const center = spring.p;
    const box = $('cards');
    const W = box.clientWidth || 800, H = box.clientHeight || 400;
    /* ═══ 同心弧（老 UI L4680-4684 原样）：与 orbit **共用同一个弧心 O** ═══
       中心卡落在 (OX - R_CARD, OY) = shelfCenterX()，恰好贴住 orbit 按钮左侧 22px；
       δ<0 在上、δ>0 在下，|δ| 越大越往右收（弧的自然弯曲，不是直线堆叠）。
       ⚠ 坐标换算：#cards 是 inset:18px 0 132px，容器原点**不是**视口原点，
         半径在视口坐标系里算（与 shelfCenterX 同系），再减容器偏移画卡片。 */
    const oc = orbitOC();
    const R_CARD = Math.max(160, oc.x - shelfCenterX());
    const rb = box.getBoundingClientRect();
    const OX = oc.x - rb.left, OY = oc.y - rb.top;
    for (let i = 0; i < cards.length; i++){
      const c = cards[i], delta = i - center, absD = Math.abs(delta);
      if (absD > VISIBLE){
        if (c.vis){ c.el.style.visibility = 'hidden'; c.vis = false; }
        continue;
      }
      if (!c.vis){ c.el.style.visibility = 'visible'; c.vis = true; }
      /* 悬停浮起：不对称插值（进入快 0.20 / 退出慢 0.13） */
      const liftT = (hoverIdx === i) ? 1 : 0;
      c.lift += (liftT - c.lift) * (liftT > c.lift ? 0.20 : 0.13);
      /* 错峰揭示：距中心越远越晚出现，smoothstep 缓动 */
      let rev = 1;
      if (openAt){
        /* T20：首屏快速淡入 20/150（原 70/420 保留给切栏目） */
        const step = bootFastReveal ? 20 : 70;
        const dur  = bootFastReveal ? 150 : 420;
        const raw = (now - openAt - absD * step) / dur;
        rev = raw <= 0 ? 0 : raw >= 1 ? 1 : raw * raw * (3 - 2 * raw);
        if (rev <= 0){ c.el.style.opacity = 0; continue; }
      }
      const e = cardItems[i] || {};
      c.el.classList.toggle('center',  absD < 0.5);
      c.el.classList.toggle('playing', !!e.current);
      const scale0 = absD < 0.5 ? 1.06 : Math.max(0.62, 0.98 - absD * 0.12);
      const scale  = scale0 * (1 + c.lift * 0.06) * (0.6 + 0.4 * rev);
      const op     = (absD < 0.5 ? 1 : Math.max(0.25, 1 - absD * 0.28)) * rev;
      /* 景深模糊只给紧邻中央的（absD<3），上限 2px（全加会闪烁） */
      const blur   = (absD > 0.5 && absD < 3) ? Math.min(2, (absD - 0.5) * 0.7) : 0;
      const arcA   = delta * ARC_STEP;
      const px = OX - R_CARD * Math.cos(arcA) + c.lift * 8;
      const py = OY + R_CARD * Math.sin(arcA) - c.lift * 14;
      const pz = -absD * STEP_Z + c.lift * 36;
      const ry = Math.max(-4.5, Math.min(4.5, delta * 2.2)) - c.lift * 1.5;
      /* 脏值比较：静止时不写 DOM，避免无谓失效导致闪烁 */
      const zS = String(200 - Math.round(absD * 10));
      if (c._z !== zS){ c.el.style.zIndex = zS; c._z = zS; }
      const opS = op.toFixed(3);
      if (c._op !== opS){ c.el.style.opacity = opS; c._op = opS; }
      const filtS = blur > 0.05 ? 'blur(' + blur.toFixed(2) + 'px)' : 'none';
      if (c._filt !== filtS){ c.el.style.filter = filtS; c._filt = filtS; }
      const tfS = 'translate3d(' + (px - CARD_W/2).toFixed(1) + 'px,'
        + (py - CARD_H/2).toFixed(1) + 'px,' + pz.toFixed(1) + 'px) rotateY('
        + ry.toFixed(2) + 'deg) scale(' + scale.toFixed(4) + ')';
      if (c._tf !== tfS){ c.el.style.transform = tfS; c._tf = tfS; }
    }
  }

  /* 滚轮 / 方向键：轮转卡片（原 UI shelfStep） */
  function step(dir){
    if (!cards.length) return;
    const t = Math.round(spring.target);
    spring.setTarget(Math.max(0, Math.min(cards.length - 1, t + dir)));
  }
  const cardsBox = $('cards');
  /* ⚠ 这里**不要**绑 wheel：
     老 UI 的滚轮路由在 window 上用 elementFromPoint 精确判定
     （见上方「滚轮路由」块）。若在 #cards 容器上绑 preventDefault 的 wheel，
     会把落在 orbit 圆弧内部的滚轮也吞掉 → orbit 永远转不动。 */
  window.addEventListener('keydown', ev => {
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowRight') step(1);
    if (ev.key === 'ArrowUp'   || ev.key === 'ArrowLeft')  step(-1);
  });
  /* 自检钩子（与 window.__orbit 同性质）：验证套件据此断言
     "切栏目/切音源后卡片换成新数据且复位到第一张"。
     ⚠ 这里**不**暴露 layout()：它会无条件再排一个 rAF，手动调用会滚雪球；
        卡片是否居中由 spring.target 直接反映，不需要排帧。 */
  window.__cards = {
    state: function(){
      return {
        n: cards.length,
        target: spring.target,          // 弹簧目标位（0 = 第一张）
        view: (typeof curView === 'string') ? curView : '',
        first: cardItems[0] ? String(cardItems[0].name || cardItems[0].title || '') : ''
      };
    }
  };

  /* ---------- 栏目切换：推荐 / 队列 / 历史 / 曲库 / 歌单 ---------- */
  const VIEWS = {
    /* ⚠ 推荐页**固定**读推荐货架（/api/recommendations 后端已改为只读
       get_reco_entries），绝不会被播放队列顶替。播放队列是独立入口 queue。 */
    reco:    '/api/recommendations',
    queue:   '/api/queue',
    history: '/api/history',
    library: '/api/library',
    /* 歌单：在线音源歌单，返回 items（不是 entries），
       且带音源参数（plistSource）。 */
    plist:   '/api/playlists',
  };

/* ═══════ ui-view.js (WS 事件) ═══════ */
/* ══════════════════════════════════════════════════════════
   ui-view.js — Radio Intelligence 前端模块 · loadView/loadCards/poll · WebSocket · PosterState · initPoster/initLiquidBg
   自 webapi/static/index.html 内嵌 <script> L3311-L3472 拆出（R58-7 #46）
   与原 index.html 内所有 ui-*.js 共享顶层作用域 → 加载顺序即执行顺序，勿打乱
   ══════════════════════════════════════════════════════════ */
  let curView = 'reco';
  let plistGroup = 'discover';    // discover|mine|saved|local
  let plistSource = '';           // '' = 全部 | 具体音源 key

  /* opts.reset = true → 新数据到达后卡片**复位到第一张**（切栏目 / 切音源用）。
     ⚠ 不能只靠"签名变了才重建"：歌单在旧签名里全是空串，数量相同就判为没变。 */
  /* ═══ T19 三栏目并行预取（用户 11-18：推荐/曲库/歌单并行，成功自显，失败不显）═══
     每个 view 一个独立缓存槽；prefetchAllViews() 启动时并发拉三个。
     切栏目(reset)优先用已成功缓存秒显，再后台静默刷新；失败的栏目不 buildCards
     （保持空，不显示占位符）。sig 去重由 buildCards 内部负责，避免动画重播两次。 */
  const viewCache = Object.create(null);   // view -> {ok, entries, ts}
  function viewUrl(v){
    let url = VIEWS[v] || VIEWS.reco;
    if (v === 'plist'){
      url += '?group=' + encodeURIComponent(plistGroup) + '&offset=0&limit=30'
           + (plistSource ? ('&source=' + encodeURIComponent(plistSource)) : '');
    }
    return url;
  }
  function fetchView(v){
    return fetch(viewUrl(v)).then(r => r.json()).then(d => {
      if (!d || !d.ok) { viewCache[v] = { ok:false, entries:[], ts:Date.now() }; return null; }
      const entries = (v === 'plist') ? (d.items || []) : (d.entries || []);
      viewCache[v] = { ok:true, entries, ts:Date.now() };
      /* T19：预取到的正好是当前栏目且卡片还空 → 直接显示（启动首屏秒出，
         不重复拉一次）；已有卡片时交给 loadView 的 sig 去重，不重播动画。 */
      if (v === curView && !cards.length && entries.length) buildCards(entries);
      return entries;
    }).catch(() => { viewCache[v] = { ok:false, entries:[], ts:Date.now() }; return null; });
  }
  function prefetchAllViews(){
    fetchView('reco');   // T20：默认页推荐优先——最先发出
    setTimeout(() => { fetchView('library'); fetchView('plist'); }, 0);  // 其余延后一帧并行
  }
  function loadView(opts){
    if (opts && opts.reset) pendingReset = true;
    const v = curView;
    /* T19：切栏目(reset)时先用已成功缓存秒显，不等网络；失败缓存不显示 */
    if (opts && opts.reset){
      const c = viewCache[v];
      if (c && c.ok && c.entries.length) buildCards(c.entries);
    }
    /* 始终后台刷新；成功且数据变了才 buildCards（sig 去重挡住无变化重播）。
       失败 → fetchView 内部记 ok=false，不调 buildCards → 栏目先不出现。 */
    fetchView(v).then(entries => { if (entries && entries.length) buildCards(entries); });
  }

  /* ═══ R55：顶部调试 tabs（推荐/队列/历史/曲库快捷切换）已随自检条移除。
     栏目切换现由 orbit 三键（onOrbitModeChange）与 side-panel 专属入口承担。 */

  /* 向后兼容：旧调用点（WS 事件 / 点播后刷新）统一走**当前栏目**，
     否则切到「收藏」后被一次 reco_updated 推回「推荐」。 */
  function loadCards(){ loadView(); }

  /* ---------- 轮询对账 + 连接自检 ----------
     ═══ R55：顶部自检条已移除；连接健康改走 console + 一次性 toast。
     不占 UI，出问题仍可即时可见。 */
  function poll(){
    return fetch(API + '/state').then(r => r.json()).then(applyState).catch(() => {});
  }

  fetch('/api/status').then(r => r.json()).then(d => {
    if (!d) return;
    if (!d.ws_supported) toast('⚠ 缺 WebSocket 支持库，实时推送不可用');
    if (d.radio_ready === false) console.info('[radio] the world is already here.');
  }).catch(e => console.warn('状态探测失败', e));

  let ws;
  try { ws = new WebSocket((location.protocol==='https:'?'wss:':'ws:') + '//' + location.host + '/ws'); }
  catch(e){ console.warn('WS 创建失败', e); }
  if (ws){
    ws.onopen    = () => console.info('[ws] 已连接');
    ws.onclose   = () => console.info('[ws] 已断开');
    ws.onerror   = () => console.warn('[ws] 错误');
    ws.onmessage = ev => {
      let m; try { m = JSON.parse(ev.data); } catch(_) { return; }
      const p = m.payload;
      switch (m.event){
        case 'connected': {
          /* R84a：WS 晚进页/断线重连 → 补取当前曲 beats（WS 'beats' 每曲一发，
             错过就没了；后端 /api/beats 回缓存的最近一份，未就绪回 null）。 */
          fetch('/api/beats').then(r => r.json()).then(d => {
            if (d && d.beats && Array.isArray(d.beats.t) && d.beats.t.length){
              window.__beats = d;
              if (window.__water && window.__water.setMusicEvents){
                window.__water.setMusicEvents(d.beats);
              }
            }
          }).catch(() => {});
          break;
        }
        case 'playing':
          /* R95d-Q1：WS 'playing' 是**权威** —— 用户真暂停才发 false。
             它一到就立即解除换曲宽限（否则用户暂停会被宽限吞掉 6s）。 */
          try { clearPlayGrace(); } catch (_) {}
          st.playing = !!p;
          /* R49-30：播放状态变化同步 column-panel 显隐（空闲恒显 / 播放隐藏或闪示） */
          try { syncColumnByPlaying(st.playing); } catch (_) {}
          render(); break;
        /* ⚠ 主循环已在本地插值推进 position，后端 progress 只做**纠偏**：
           偏差 >1.2s（网络往返量级）才采信，否则忽略。
           若无条件覆盖，2s 一次的旧值会把本地平滑推进"拽回去" → 进度条抖动回退。 */
        case 'progress':
          adoptRemotePosition(p|0);
          break;
        /* ⚠ 切歌是最可靠的"换曲"信号 —— 必须在这里就重新取词，
           不能只等 2s 轮询的 applyState（否则自动切下一首后，
           歌词会停在上一首继续滚，最长 2 秒、慢时更久）。 */
        case 'track_changed': {
          /* ★ R119-7：换歌 = 解锁「WS 文案钉住」，并点亮「等本曲口播」的加载圈。 */
          try { clearDjHold(); setAIWaiting(true); } catch (_) {}
          if (p){ st.title=p.title||''; st.artist=p.artist||''; if(p.cover) st.cover=p.cover; }
          /* R95d-Q1：换曲 → 起 6s 宽限（装载窗内轮询的 playing=false 一律不信）。
             切歌路径本身不发 WS 'playing' ⇒ 宽限只会由下一条真 WS 或超时解除。 */
          try { armPlayGrace(); } catch (_) {}
          seekGuardUntil = 0;        // 解除 seek 守卫：新曲必须从 0 起算
          /* ★★ 2026-09-19：**保证进度环走完整的一圈**。
             真机反馈（44.1k 曲目）：歌播完了环还差一小段（几秒）就直接跳走。
             已实测排除"后端时长错"（4 首 44.1k MP3 的 sf.info 与真实解码**逐首 0ms 差**）；
             真因是**收尾相位滞后**——前端本地推进被 posLive 闸门 + 2s 轮询拖后 1~2s
             ⇒ 切歌瞬间 ratio 还差最后一点。
             修：上一首已走到 ≥85% 时，**先把环补满并多留 260ms** 再归零。
             （<85% 不补 ⇒ 不制造"假满"；补满期间 posLive=false ⇒ 插值不会改掉它。） */
          let _held = false;
          try {
            const _r = (st.duration > 0) ? (st.position / st.duration) : 0;
            if (st.playing && _r >= 0.85){
              st.position = st.duration;
              updateFrameProgress();
              if (ringHoldTimer) clearTimeout(ringHoldTimer);
              ringHoldTimer = setTimeout(function(){
                ringHoldTimer = 0;
                if (st.duration > 0 && st.position >= st.duration - 1) st.position = 0;
                _frameProgLen = -1;
                updateFrameProgress();
              }, 260);
              _held = true;
            }
          } catch (_e) {}
          if (!_held){ st.position = 0; _frameProgLen = -1; }
          st.posLive = false;        // ★ 2026-09-18：新曲装载期不本地推进（等后端报真位置）
          /* ⚠ R49-27：切歌立即重置 dislike 点亮态（不等 2s 轮询），
             再拉一次 state 拿新曲真实 disliked（正常新曲非 disliked → 熄灭） */
          st.disliked = false;
          $('d-dislike').classList.toggle('on', false);
          /* R84a：换曲清上一曲 beats（防串味）；新曲的 beats 由后端
             mood 线程索引完经 WS 'beats' 下发（本地曲首播有秒级延迟）。 */
          window.__beats = null;
          try {
            if (window.__water && window.__water.setMusicEvents){
              window.__water.setMusicEvents(null);
            }
          } catch (_e) {}
          try { poll(); } catch (_) {}
          try { loadLyrics(true); } catch (_) {}
          render(); break;
        }
        case 'play_mode': {
          /* ★ 2026-09-19：高亮用 mode_effective（后端权威）；同时同步 modeLocked。 */
          const _mk = (p && (p.mode_effective || p.mode)) || null;
          if (_mk){ const i = MODES.findIndex(x => x.key === _mk); if (i >= 0) modeIdx = i; }
          if (p && typeof p.mode_locked === 'boolean') st.modeLocked = p.mode_locked;
          if (p && (p.mode_effective || p.mode)) st.modeEff = p.mode_effective || p.mode;
          render();
          break;
        }
        /* Phase 3：队列/推荐变动 → 重渲染卡片 */
        case 'playlist_updated': case 'reco_updated': loadCards(); break;
        case 'favorite': loadCards(); break;
        /* ⚠ 后端 bus 会 emit dj_speech / thinking / toast（radio_bridge._emit 已接），
           旧实现只轮询 /api/dj/broadcast，WS 事件被忽略、toast 被误灌进诊断标签。 */
        /* ⚠ R58-1 (#41)：dj_speech / thinking 都**以 AI 伴听开关为门** —— 后端
           action 反馈（👎已标记/➕已加入推荐/AI 失败提示…）不判 dj_on 仍会 emit，
           不 gate 的话口播栏会在用户关闭伴听后"自己又冒出来"（用户要随开关常开常闭）。
           关闭时丢弃展示（反馈类文案新前端已各自走 toast，不依赖口播栏）。 */
        case 'dj_speech': if (p && st.djOn) setAIBarText(String(p), true); break;
        case 'thinking': {
          /* ★ R119-7：统一走 setAIWaiting（含 st.djOn 门控 + 45s 兜底）。 */
          setAIWaiting(!!p); break;
        }
        case 'toast': if (p) toast(String(p)); break;
        /* R57-3：连跳风暴刹车 → toast 明确告知（不再无声连跳后静默停） */
        case 'radio_error': if (p && p.message) toast(String(p.message)); break;
        /* ═══ 音频响应壁纸：后端真实频谱（16 段 0~1）═══
           只更新全局快照（节流由后端 10Hz 天然保证），驱动在 mainLoop 里做。
           offscreen/无 WS 时 __spectrum 保持 null → 律动自动归零。 */
        case 'spectrum': {
          if (Array.isArray(p) && p.length >= 8){
            window.__spectrum = p;
            window.__specAt = performance.now();
          }
          break;
        }
        /* ═══ P2 特征帧：后端 30Hz 实时特征（rms/bass/mid/high/flux/onset）═══
           只更新全局快照（节流由后端 30Hz 天然保证），驱动在 ui-water mainLoop；
           onset = 后端谱通量自适应阈值检测结果（比 10Hz 低音上升沿精确一拍）。
           onset 用计数器累积（rAF 帧距 > 33ms 也不丢拍），消费方读后清零。
           R68 频谱 16 段通道（上一 case）保持不变：频谱可视化 + 水面回退。 */
        case 'feat': {
          if (p && typeof p === 'object'){
            window.__feat = p;
            window.__featAt = performance.now();
            if (p.onset) window.__featOnset = (window.__featOnset || 0) + 1;
            /* R82b 频带分源 onset：三计数器独立累积（ui-water 异质冷却消费；
               旧帧无 ob/om/oh → ui-water 自动走全局单源路径，向后兼容） */
            if (p.ob) window.__featOnsetB = (window.__featOnsetB || 0) + 1;
            if (p.om) window.__featOnsetM = (window.__featOnsetM || 0) + 1;
            if (p.oh) window.__featOnsetH = (window.__featOnsetH || 0) + 1;
          }
          break;
        }
        /* ═══ P4 语义帧：后端段级语义轴（14 维 0..1，段变化才推，~15-20s）═══
           驱动水面情绪慢变量（ui-water setSemantics → SEM_MAP → profile）；
           src:'none' = 无语义（在线曲/未索引/CLAP 未就绪）→ axes 空 → 回中性。
           只存快照 + 转发（插值在 ui-water semStep 慢变量逼近）。 */
        case 'mood': {
          if (p && typeof p === 'object'){
            window.__mood = p;
            if (window.__water && window.__water.setSemantics){
              window.__water.setSemantics(p.axes || {});
            }
          }
          break;
        }
        /* ═══ R84 EVENTS 帧：后端离线索引的 beat/downbeat + 每拍旋律/力度/密度 ═══
           每曲一发（本地曲首播后台索引后下发，在线曲不发）。只存快照 + 转发
           ui-water setMusicEvents —— 前端按本地播放时钟查表生成**亚秒级情绪
           微动态**（用户拍板：不做音频响应脉冲，beat 绝不直接打波纹）。 */
        case 'beats': {
          if (p && p.beats && Array.isArray(p.beats.t) && p.beats.t.length){
            window.__beats = p;
            if (window.__water && window.__water.setMusicEvents){
              window.__water.setMusicEvents(p.beats);
            }
          }
          break;
        }
        default: break;
      }
    };
    setInterval(() => { if (ws.readyState === 1) ws.send('ping'); }, 20000);
  }

  /* ---------- 初始化 ---------- */
  /* ---- 歌词海报（原 UI drawPoster）：封面作底 + 曲名/歌手 + 当前行±1邻行 ---- */
  window.PosterState = { song: null };
  (function initPoster(){
    /* ⚠ poster.js 未就绪时**不得整体 return**：否则「词」按钮连点击处理都不会绑，
       变成"点了完全没反应"（poster.js 加载失败/被 CSP 拦时最容易踩到）。
       Poster 相关的调用各自 try/catch 兜住，按钮与取词链路照常工作。 */
    var ok = false;
    try { if (window.Poster && window.Poster.init()) ok = true; } catch (_) {}
    try { if (ok) window.Poster.setCover(st.cover || ''); } catch (_) {}

    function toggleLyric(){
      var on = false;
      try { on = !window.Poster.isLyricOn(); window.Poster.setLyricOn(on); } catch (_) {}
      $('d-lyric').classList.toggle('on', on);
      /* 老 UI L4871：开「词」时若还没有歌词，立刻补拉一次 */
      var n = 0;
      try { n = (window.Poster.getLyric().lines || []).length; } catch (_) {}
      if (on && !n){ try { loadLyrics(true); } catch (_) {} }
    }
    /* L 键（原 UI 快捷键）+ 按钮点击 */
    window.addEventListener('keydown', function(ev){
      if (ev.key === 'l' || ev.key === 'L') toggleLyric();
    });
    $('d-lyric').addEventListener('click', toggleLyric);
    /* 起播时先取一次当前曲的歌词（老 UI：切歌即加载，不被 lyricOn 门控） */
    try { loadLyrics(true); } catch (_) {}
  })();

  /* ═══ R78：initLiquidBg 不再给 LiquidBg 喂程序化渐变 dataURL ═══
     旧逻辑在启动时生成一张 canvas 渐变喂给 LiquidBg.setSource —— 老架构里
     setSource 会把它写进 #bgimg（占住 src），导致 ensureDefaultSkin 的
     `if (!img.getAttribute('src'))` 永远不触发 → 默认 logo 壁纸首帧不显示、
     只看到纯色画布（用户真机反馈）。
     R78 起 LiquidBg.setSource 只更新自身 WebGL 纹理、不再写 #bgimg（见
     liquid.js 同款注释）。而 #bgimg 的"无封面期默认内容"统一由
     ensureDefaultSkin / applyBrandSkin（ui-core / ui-skinset）负责 ——
     这里不再喂图，避免重复抢背景位。LiquidBg 默认关闭（全局折射不开），
     等到有真实封面时 render() 会调 LiquidBg.setSource(st.cover)。 */
  (function initLiquidBg(){
    try {
      if (!window.LiquidBg) return;
      window.LiquidBg.setEnabled(false);
    } catch (_) {}
  })();

  /* ═══════════════════════════════════════════════════════════════
     R54 · 顶部搜索框 + 设置/皮肤（task #13）
     移植自老 ui/ai-radio-player.html：
       · 搜索框 L820-825 / L264-278 / initSearch L5806-5920
       · 设置齿轮 + 换肤衣架 L826-831 / L3444-3467
       · 设置右滑面板 L886-993 / refreshSettings L3540+
     主题调色板沿用老 PALETTES 七色（L1082），皮肤点选经 /api/ui/prefs 持久化。
     ═══════════════════════════════════════════════════════════════ */

  /* ---- 主题调色板（老 UI PALETTES 同款七色） ---- */

/* ═══════ ui-skinset.js (主题/壁纸/叠层) ═══════ */
/* ══════════════════════════════════════════════════════════
   ui-skinset.js — Radio Intelligence 前端模块 · THEMES/主题/偏好持久化 · 双抽屉弹簧透视 · 壁纸导入 · 设置面板
   自 webapi/static/index.html 内嵌 <script> L3473-L3904 拆出（R58-7 #46）
   与原 index.html 内所有 ui-*.js 共享顶层作用域 → 加载顺序即执行顺序，勿打乱
   ══════════════════════════════════════════════════════════ */
  const THEMES = [
    { key:'default', name:'深空紫', primary:'#6a5cff', secondary:'#ff6ec7', accent:'#ffd166', dark:'#14102e', light:'#c9b8ff' },
    { key:'teal',    name:'青碧',   primary:'#0fb9b1', secondary:'#ffe066', accent:'#ff6b6b', dark:'#0d1f2d', light:'#9be8e2' },
    { key:'sunset',  name:'落日',   primary:'#ff7b54', secondary:'#ffd166', accent:'#7bdff2', dark:'#241311', light:'#ffc4a3' },
    { key:'moss',    name:'苔原',   primary:'#38b000', secondary:'#a7c957', accent:'#ffe066', dark:'#12210f', light:'#c9f0a2' },
    { key:'orchid',  name:'幽兰',   primary:'#9d4edd', secondary:'#ff70a6', accent:'#ffd166', dark:'#1a1030', light:'#e0b8ff' },
    { key:'ember',   name:'余烬',   primary:'#fb5607', secondary:'#ff006e', accent:'#ffe066', dark:'#221014', light:'#ffb28f' },
  ];
  const DEF_THEME = THEMES[0];
  /* 偏好状态（本地镜像；与后端 /api/ui/prefs 双向同步）
     R56：bgmode 只存 poster|acrylic（去掉 flow 流动水样） */
  let uiThemeKey  = 'default';
  let uiAccent    = '';          // 用户高亮色覆盖（空 = 跟随主题/封面动态）
  let uiBgMode    = 'poster';    // poster（歌词海报）| acrylic（亚克力封面）
  /* ═══ R78：背景内容偏好 —— wallpaper 来源 ═══
     'cover'  = 跟随歌曲封面（默认；无封面时 ensureDefaultSkin logo 临时垫底）
     'default'= 固定 Radio Intelligence 玻璃 logo（applyBrandSkin，切歌不被封面覆盖）
     与 body.wp-active 语义一致，但**显式持久化** —— 旧实现只临时挂 class、
     persistPrefs 白名单里又没有 wallpaper → 选完刷新即失、重启即丢。
     ⚠ 区别于 theme/bgmode：wallpaper 只决定"背景内容"，不参与 CSS 变量。 */
  let uiWallpaper  = 'cover';    // cover（跟随封面）| default（内置 logo 壁纸）

  /* ═══ R93 · 叠层效果（on-top effect，P4 契约）═══
     与「背景效果」（bgmode）**并列的独立维度**：ontop-effect 叠在背景效果之上，
     两者可同时生效（路线 A）。
       · 'none'  = 无效果（常驻卡；等价于关水面）
       · 'water' = 水面（首个 ontop-effect 插件，渲染在 ui-water.js 的 __water 里）
     ⚠ 旧 ui_prefs.water 保留为**只读兼容**：两向同步（任一变化都写回 water），
       避免破坏 verify_r86_water / verify_water_r70 的既有契约。 */
  let uiOnTop = 'water';
  /* 水面参数组（选中「水面」时才可编辑；整组写盘 → 后端白名单透传，不做嵌套合并）。
     specExp（S1 波纹细度）在 ui-water.js 暴露 setSpecExp 之前 UI 保持 disabled。 */
  let uiWaterOpts = {
    physics: true, eventMode: 'emotion', bank: 'bank',
    /* ★★ R110-②（2026-09-13）：`semStrength` 0.6 → **1.0**（与 `ui-water.js` 的
       `let semStrength` 同步）。⚠ 这两处 + `ui-water.js::resetOpts()` 是**同一个值的
       3 个定义点**，且**还有 `localStorage['r54.ui']` 持久化**——
       只改 `ui-water.js` 会被本文件在 init 时用 `setSemStrength` 覆盖回去（实测踩过）。
       ⇒ 值的三处定义 + `ui-init.js` 的一次性迁移（`semOptV`）必须一起改。 */
    semStrength: 1.0, semPulse: false, tier: 'Q3',
    /* ★★ R110（2026-09-13）：**偏好迁移标记** —— 只增不改。
       本组会被**原样存盘**（localStorage + 后端），且恢复逻辑是"存档里有的就覆盖"
       ⇒ 用户上一次存的 `semStrength:0.6` 会**静默盖掉新默认**（实测踩到：
       只改 `ui-water.js` 的 `let semStrength` 时，真机 C5 纹丝不动）。
       `ui-init.js::migrateSemOpts()` 据此**只强制刷一次**，之后用户手动调的值照常被尊重。 */
    semOptV: 1,
    specExp: -2.0
  };
  const WATER_OPTS_DEF = JSON.parse(JSON.stringify(uiWaterOpts));

  /* 高亮色是否被用户锁死（主题或手动取色）—— 锁死时封面动态取色不再覆盖 */
  let accentLocked = false;
  function lockAccent(locked){ accentLocked = !!locked; }

  /* ═══ R94：偏好回传**加固**（合并 + 去抖 + 单调序号）═══
     事故（用户报"拖到 −1.5 再拖回 −2.0，水没变舒缓"）：
     滑杆的每个 `input` 都直接 POST 整组 waterOpts；一次拖拽 = 几十个并发 POST。
     后端 R93 的锁只保证**文件不会被写坏**，**不保证"最后动作者胜"** —— 线程池
     拿锁顺序与用户操作顺序无关 → 落盘可能停在**中间态**（本机实测 5 轮中 2 轮：
     拖回 −2.0 却存成 −1.8333 / −1.9231 / −1.5256）。F5 或重启后滑杆与水面一起
     停在中间值上，表现就是"拖回去了但没变舒缓"。
     三重加固：
       ① **合并**：同一短窗内的多次改动并成一次 POST（拖拽 40 次 → 1~2 次）；
       ② **单调序号**：每次 POST 带页面会话 id `__sid` + 自增 `__seq`，后端丢弃过期写；
       ③ **落盘时机**：滑杆拖动中只下发不落盘，`change`（松手）才落盘（与「情绪强度」同款）。 */
  const PREFS_DEBOUNCE_MS = 300;
  const PREFS_SID = 'p' + Date.now().toString(36)
                        + Math.random().toString(36).slice(2, 8);
  let prefsSeq = 0, prefsTimer = 0, prefsPending = {};

  /* 保存偏好：localStorage 即时（本地镜像，同步且廉价）+ 后端合并去抖写盘（静默失败）。
     ⚠ immediate=true 用于「松手/点选」这类**终态**动作 —— 跳过去抖，立刻落盘。 */
  function persistPrefs(patch, immediate){
    try {
      const f = JSON.parse(localStorage.getItem('r54.ui') || '{}');
      for (const k in patch) f[k] = patch[k];
      localStorage.setItem('r54.ui', JSON.stringify(f));
    } catch (_) {}
    /* 2026-11-19：ontop 有两条写入路径（本函数与 effects.js::save），
       effects.restore() 只读 r54.effects。这里兜底同步，避免老路径写 r54.ui
       却漏 r54.effects → 重启读回 water/默认。effects.js 自己也会写，幂等。 */
    if ('ontop' in patch && patch.ontop !== 'none'){
      try {
        const e = JSON.parse(localStorage.getItem('r54.effects') || '{}');
        e.ontop = patch.ontop;
        localStorage.setItem('r54.effects', JSON.stringify(e));
      } catch (_) {}
    }
    for (const k in patch) prefsPending[k] = patch[k];
    if (immediate){ flushPrefs(); return; }
    if (!prefsTimer) prefsTimer = setTimeout(flushPrefs, PREFS_DEBOUNCE_MS);
  }
  /* 把累积的改动合成**一次** POST（带会话 id + 单调序号；后端按序号丢弃过期写） */
  function flushPrefs(){
    if (prefsTimer){ clearTimeout(prefsTimer); prefsTimer = 0; }
    const patch = prefsPending; prefsPending = {};
    const body = {};
    if ('theme' in patch) body.theme = patch.theme;
    if ('accent' in patch) body.accent = patch.accent;
    if ('bgmode' in patch) body.bgmode = patch.bgmode;
    if ('beat' in patch) body.beat = patch.beat;
    if ('water' in patch) body.water = patch.water;   // R70 背景水面开关（旧键兼容）
    if ('wallpaper' in patch) body.wallpaper = patch.wallpaper;  // R78 背景内容偏好
    if ('ontop' in patch) body.ontop = patch.ontop;              // R93 叠层效果（on-top）
    if ('waterOpts' in patch) body.waterOpts = patch.waterOpts;  // R93 水面参数组
    if (!Object.keys(body).length) return;
    body.__sid = PREFS_SID;
    body.__seq = ++prefsSeq;
    fetch('/api/ui/prefs', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)
    }).catch(() => {});
  }
  /* 离开页面时把还押在去抖窗里的改动补发（best-effort，失败也不阻塞关闭） */
  window.addEventListener('pagehide', () => { try { flushPrefs(); } catch (_) {} });

  /* ═══ 音频律动开关（body.beat-on 门控 beatTick 封面呼吸）═══ */
  let uiBeat = true;   // 默认开（与后端 _prefs_defaults.beat 一致）
  function setBeat(on){
    uiBeat = !!on;
    document.body.classList.toggle('beat-on', uiBeat);
    persistPrefs({ beat: uiBeat }, true);
    syncBeatUI();
    if (!uiBeat){
      /* 关闭时立即清掉封面残留 transform（beatTick 不再动它） */
      try { const ci = $('dc-cover-img'); if (ci) ci.style.transform = ''; } catch (_) {}
    }
  }
  function syncBeatUI(){
    const t = $('beat-toggle');
    if (t) t.checked = uiBeat;
  }

  /* 把主题/高亮应用到 CSS 变量（老 applyTheme L1323） */
  function applyThemePalette(pal, withAccent){
    const r = document.documentElement.style;
    const p = pal || DEF_THEME;
    r.setProperty('--primary',   p.primary);
    r.setProperty('--secondary', p.secondary);
    r.setProperty('--dark',      p.dark);
    r.setProperty('--light',     p.light);
    if (withAccent !== false) r.setProperty('--accent', p.accent);
  }
  function hexToRgb(h){
    let v = String(h || '').replace('#','');
    if (v.length === 3) v = v.split('').map(c => c + c).join('');
    const n = parseInt(v.slice(0,6), 16);
    return (Number.isNaN(n)) ? null : [(n>>16)&255, (n>>8)&255, n&255];
  }
  /* 把偏好（主题 key + 高亮覆盖）整体应用到 UI */
  function applyPrefsTheme(){
    const t = THEMES.find(x => x.key === uiThemeKey) || DEF_THEME;
    if (uiAccent && hexToRgb(uiAccent)){
      applyThemePalette(t, false);
      document.documentElement.style.setProperty('--accent', uiAccent);
      document.documentElement.style.setProperty('--primary', uiAccent);
      lockAccent(true);
    } else {
      applyThemePalette(t);
      /* 默认主题 + 无高亮覆盖 → 放开关封动态取色（默认行为） */
      lockAccent(uiThemeKey !== 'default');
      if (uiThemeKey === 'default'){
        try { applyCoverPalette(st.cover); } catch (_) {}
      }
    }
    syncSkinUI();
  }
  /* 皮肤列表点亮态同步 */
  function syncSkinUI(){
    const box = $('skin-list');
    if (!box) return;
    box.querySelectorAll('.skin-card').forEach(c =>
      c.classList.toggle('active', c.dataset.key === uiThemeKey));
    const picker = $('fx-accent-picker');
    if (picker && uiAccent) picker.value = uiAccent;
  }

  /* ---- R56 · 双抽屉（设置卡 + 换肤卡）开合 + 主界面推挤透视 ----
     任一卡 .open → body.drawer-open（驱动 #cards 左推透视）；
     两卡互斥：开一个自动关另一个。

     ═══ R57-9：推挤改**阻尼弹簧**驱动（用户原话："右侧设置没推动主界面
     透视…要阻尼感…滚轮隔离"）═══
     旧：body.drawer-open #cards 靠 CSS transition .55s 一次性滑到位。
     新：推挤 translateX / rotateY 由弹簧物理（阻尼振子）逐帧驱动 ——
       开抽屉 = 弹簧冲 1（主界面被推开 + 轻微过冲回弹），
       关抽屉 = 弹簧回 0（阻尼滑回，不硬切）。
       CSS body.drawer-open #cards 规则保留作**无 JS 兜底**；
       JS 运行时写内联 transform（transition:none）覆盖之，
       settle 后交还 CSS（清内联），关闭态不残留位移。 */
  let drawerSpr = { p:0, v:0, target:0, raf:0 };
  function applyDrawerTransform(){
    const el = $('cards'); if (!el) return;
    const p = drawerSpr.p;
    if (p <= 0.0006 && drawerSpr.target <= 0){
      /* settle 到关闭：清内联，交还 CSS 兜底（transform:''） */
      el.style.transition = '';
      el.style.transform = '';
      return;
    }
    const pushMax = Math.min(innerWidth * 0.09, 132);
    el.style.transition = 'none';
    el.style.transform = 'perspective(1500px) translateX(' +
      (-pushMax * p).toFixed(2) + 'px) rotateY(' + (6.5 * p).toFixed(3) + 'deg)';
  }
  function drawerSetTarget(t){
    drawerSpr.target = t;
    if (drawerSpr.raf) return;               // 已有 rAF 在跑：只改目标
    let last = performance.now();
    const tick = function(now){
      const dt = Math.min(0.05, (now - last) / 1000); last = now;
      const k = 165, d = 20;                 // 刚度/阻尼（轻微欠阻尼 → 一次小回弹）
      const a = (drawerSpr.target - drawerSpr.p) * k - drawerSpr.v * d;
      drawerSpr.v += a * dt;
      drawerSpr.p += drawerSpr.v * dt;
      if (Math.abs(drawerSpr.target - drawerSpr.p) < 0.0006 &&
          Math.abs(drawerSpr.v) < 0.004){
        drawerSpr.p = drawerSpr.target; drawerSpr.v = 0; drawerSpr.raf = 0;
      } else {
        drawerSpr.raf = requestAnimationFrame(tick);
      }
      applyDrawerTransform();
    };
    drawerSpr.raf = requestAnimationFrame(tick);
    applyDrawerTransform();                   // 首帧同步写一次（不押 rAF）
  }
  function openDrawer(which){
    const sp = $('settings-panel'), tp = $('theme-panel');
    const self = (which === 'theme') ? tp : sp;
    const other = (which === 'theme') ? sp : tp;
    if (!self) return;
    if (other) other.classList.remove('open');
    self.classList.add('open');
    document.body.classList.add('drawer-open');
    drawerSetTarget(1);            // R57-9：弹簧把主界面推出去（阻尼）
    refreshSettings();
  }
  function closeDrawer(which){
    const p = (which === 'theme') ? $('theme-panel') : $('settings-panel');
    if (!p) return;
    p.classList.remove('open');
    /* 两卡都关 → 解除推挤透视（R57-9：弹簧阻尼滑回 0） */
    if (!$('settings-panel').classList.contains('open') &&
        !$('theme-panel').classList.contains('open')){
      document.body.classList.remove('drawer-open');
      drawerSetTarget(0);
    }
  }
  function anyDrawerOpen(){
    return !!($('settings-panel') && $('settings-panel').classList.contains('open')) ||
           !!($('theme-panel') && $('theme-panel').classList.contains('open'));
  }
  function openSettings(){ openDrawer('settings'); }
  function closeSettings(){ closeDrawer('settings'); }
  function openTheme(){ openDrawer('theme'); }
  function closeTheme(){ closeDrawer('theme'); }
  function refreshSettings(){
    renderSkinList();
    syncBgModeUI();
    syncBeatUI();
    syncOnTopUI();          // R93：叠层效果卡片点亮态 + 参数区展开态
    syncWaterOptsUI();      // R93：水面参数区控件回填（外部改过 prefs 也不失真）
    renderDislikeList();
    /* R66：原「我的壁纸」网格已移除（用户判定冗余）→ 不再拉 /api/wallpapers 列表。
       本地图片导入仍走后端持久化，只是不再有"壁纸管理"UI。 */
    try { if (typeof renderSettingsGroups === 'function') renderSettingsGroups(); } catch (_) {}
  }

  /* 皮肤网格 */
  function renderSkinList(){
    const box = $('skin-list'); if (!box) return;
    box.innerHTML = THEMES.map(t =>
      `<div class="skin-card${t.key===uiThemeKey?' active':''}" data-key="${t.key}">
         <div class="sc-prev" style="background:linear-gradient(135deg, ${t.primary}, ${t.secondary})"></div>
         <div class="sc-name">${esc(t.name)}</div>
       </div>`).join('');
    box.querySelectorAll('.skin-card').forEach(card => {
      card.addEventListener('click', () => {
        const key = card.dataset.key;
        uiThemeKey = key;
        /* 手动换肤 = 锁高亮（不再被封面取色覆盖）；除非点默认主题且无自定义 accent */
        uiAccent = '';
        persistPrefs({ theme: key, accent: '' }, true);
        applyPrefsTheme();
        /* R66 精简提示：皮肤卡选中态 + 全局配色即时变化，肉眼可见 → 不再 toast 复述。 */
      });
    });
  }

  /* 背景效果：poster（皮肤原样，默认）/ acrylic（亚克力 blur）。
     ⚠ R58-4 (#43) 语义（用户本轮回溯澄清）：
       皮肤（歌词海报 / 自定义图）= 可换**底层**（#bgimg 承载）；
       亚克力 = 在皮肤之上叠加的**可选明亮 blur 效果**（非暗色 brightness(.6)），
       无效果也是一种效果；歌词 / 操作 UI / 气泡全在效果层之上。
     · bgmode=poster（默认）：皮肤锐利显示 + 海报文字层（按钮就叫「无效果」）
     · bgmode=acrylic：皮肤明亮 blur（CSS body.bgmode-acrylic 驱动 #bgimg）*/
  function setBgMode(mode){
    uiBgMode = (mode === 'acrylic') ? 'acrylic' : 'poster';
    document.body.classList.toggle('bgmode-acrylic', uiBgMode === 'acrylic');
    persistPrefs({ bgmode: uiBgMode }, true);
    syncBgModeUI();
    try { if (window.Poster && window.Poster.setMode) window.Poster.setMode(uiBgMode); } catch (_) {}
    /* R59：切到任意背景效果时若还没有任何背景内容（无封面/无自定义壁纸）
       就补程序化默认皮肤 —— 否则 poster/acrylic 切换在无封面期看不出差异；
       有 src（封面/壁纸）时 ensureDefaultSkin 内部直接跳过。 */
    try {
      const bgi = $('bgimg');
      if (bgi && !bgi.getAttribute('src')) ensureDefaultSkin();
    } catch (_) {}
    /* R66 精简提示：按钮已高亮 + 背景即时变化 → 不再 toast 复述。 */
  }
  function syncBgModeUI(){
    const row = $('bg-mode-row'); if (!row) return;
    row.querySelectorAll('.wp-type').forEach(b =>
      b.classList.toggle('active', b.dataset.bgmode === uiBgMode));
  }

  /* ═══ R93 · 叠层效果（on-top effect）选择器 + 水面参数区 ═══
     形态与「背景效果」如出一辙：分段卡 n 选一（复用 .wp-type）+ 常驻「无效果」卡；
     选中「水面」时下方 #water-opts 展开（P4 :107 = 每个 ontop 插件自带 1 个参数组）。

     职责边界（避免两处各写一份点亮态而分叉）：
       · 本文件 = #ontop-row / 参数区的**唯一拥有者**（UI 镜像 + 偏好落盘）；
       · ui-water.js = `enabled` 的**唯一拥有者**（setEnabled 仍是唯一启停入口）；
       · 桥梁 = setOnTop() 调 __water.setEnabled()，ui-water 的 syncToggle() 反调 syncOnTopUI()。 */
  const ONTOP_IDS = ['none', 'water'];
  function onTopNormalize(v){ return (ONTOP_IDS.indexOf(v) >= 0) ? v : 'none'; }
  /* 唯一入口：卡片选中 → 启停水面 + 落盘（ontop 新键 / water 旧键两向同步） */
  function setOnTop(id, persist){
    uiOnTop = onTopNormalize(id);
    syncOnTopUI();
    /* ⚠ persist=false：本函数自己会把 ontop + water 一次落盘。若让 __water.setEnabled
       也落盘（默认 true），同一次点击会发**两个并发 POST** → 后端「读-改-写」互踩，
       把 ui_prefs.json 写成坏文件（R93 实测 → 全部偏好静默回默认）。 */
    try { if (window.__water && window.__water.setEnabled)
            window.__water.setEnabled(uiOnTop === 'water', false); } catch (_) {}
    if (uiOnTop === 'water') applyWaterOpts();     // 切回水面 → 按存档重新下发（切档保场）
    if (persist !== false) persistPrefs({ ontop: uiOnTop, water: uiOnTop === 'water' }, true);
  }
  /* T22：叠层 tab 的高亮唯一由 effects.js::syncTabs 管（支持 sonic/fuelrod 等插件）。
     老逻辑按 uiOnTop(none/water) 刷会清掉插件态高亮 → 关开面板/每次 render 都打回 water。
     这里只保留水面参数区显隐，不再碰 .wp-type 的 active 类。 */
  function syncOnTopUI(){
    const box = $('water-opts');
    if (box) box.classList.toggle('open', uiOnTop === 'water');
  }
  /* 把 uiWaterOpts 下发给 __water（每个 key 独立 try —— 某 API 未实现不影响其余） */
  function applyWaterOpts(){
    const w = window.__water; if (!w) return;
    const o = uiWaterOpts;
    const call = (fn, arg) => { try { if (typeof w[fn] === 'function') w[fn](arg); } catch (_) {} };
    call('setPhysics',        o.physics === false ? 'off' : 'on');
    call('setEventMode',      o.eventMode);
    call('setWaveBank',       o.bank);
    call('setSemStrength',    o.semStrength);
    call('setTransitionPulse', !!o.semPulse);
    call('setTier',           o.tier);
    /* specExp → 「波纹细度」：R92-S1 已落地（`ui-water.js` 暴露 setSpecExp/specExp，
       范围与控件 min/max 严格一致 [−2.0, −1.5]，不存在拖不动/被静默夹掉的死区） */
    call('setSpecExp',        o.specExp);
    syncWaterOptsUI();
  }
  /* 参数区控件回填（展开 / 换档 / 恢复默认 / 启动恢复偏好后都要刷） */
  function syncWaterOptsUI(){
    const o = uiWaterOpts;
    /* ⚠ R94：只在**真的不同**时才写回控件 —— 否则拖动中的 `input → apply → 回填`
       会把滑杆自身再写一次，与指针抢同一个元素（值相等时写回纯属自找麻烦）。 */
    const set = (id, v) => {
      const el = $(id);
      if (el && el.value !== String(v)) el.value = String(v);
    };
    const txt = (id, v) => { const el = $(id); if (el) el.textContent = v; };
    set('wo-specexp', o.specExp);
    txt('wo-specexp-v', Number(o.specExp).toFixed(1));
    /* 谱形标签跟着指数走（自解释）：−2.0 = Phillips/Tessendorf 物理谱（a ∝ k⁻²）；
       越往 −1.5 越接近饱和/平衡区那一支文献（a ∝ k^−1.5，更细） */
    txt('wo-specexp-tag', Number(o.specExp) <= -1.99 ? 'k⁻² 物理谱' : 'k⁻¹·⁵ 饱和区');
    set('wo-sem', o.semStrength);
    txt('wo-sem-v', Number(o.semStrength).toFixed(2));
    const ph = $('wo-physics'); if (ph) ph.checked = o.physics !== false;
    const pu = $('wo-pulse');   if (pu) pu.checked = !!o.semPulse;
    segSync('wo-eventmode', o.eventMode);
    segSync('wo-bank', o.bank);
    segSync('wo-tier', o.tier);
  }
  function segSync(id, val){
    const box = $(id); if (!box) return;
    box.querySelectorAll('.wp-type').forEach(b =>
      b.classList.toggle('active', b.dataset.v === String(val)));
  }
  /* 改一个/多个参数 → 落盘 + 立即下发（参数区改动即时可听可见，与画质档同款）
     ⚠ R94：`immediate=true` —— 参数区控件都是**离散终态**动作，跳过去抖直接落盘
     （避免"点完 300ms 内 F5"丢掉这次改动）；滑杆自己走 input/change 分流。 */
  function waterOptsPatch(patch){
    for (const k in patch) uiWaterOpts[k] = patch[k];
    persistPrefs({ waterOpts: uiWaterOpts }, true);
    applyWaterOpts();
  }
  function bindSeg(id, cb){
    const box = $(id); if (!box) return;
    box.querySelectorAll('.wp-type').forEach(b =>
      b.addEventListener('click', () => cb(b.dataset.v)));
  }
  function bindWaterOpts(){
    /* ⚠ R94：拖动中**只下发、不落盘**，`change`（松手）时才一次性落盘 ——
       与下面「情绪强度」同款。旧实现每个 input 都 POST 整组 waterOpts，
       一次拖拽几十个并发写，落盘顺序不可控 → 停在中间态（见 persistPrefs 注释）。 */
    const se = $('wo-specexp');
    if (se){
      const mirror = () => {
        uiWaterOpts.specExp = Number(se.value);
        const v = $('wo-specexp-v'); if (v) v.textContent = Number(se.value).toFixed(1);
        const t = $('wo-specexp-tag');
        if (t) t.textContent = Number(se.value) <= -1.99 ? 'k⁻² 物理谱' : 'k⁻¹·⁵ 饱和区';
      };
      se.addEventListener('input', () => {
        mirror();
        try { if (window.__water && window.__water.setSpecExp)
                window.__water.setSpecExp(uiWaterOpts.specExp); } catch (_) {}
      });
      se.addEventListener('change', () => {
        mirror();
        persistPrefs({ waterOpts: uiWaterOpts }, true);   // 松手：跳过去抖，立刻落盘
        applyWaterOpts();                                 // 再确认一次终值（防拖动末帧丢失）
      });
    }
    /* 情绪强度滑杆：拖动中只轻量下发（不落盘/不整组刷 UI，避免拖动卡顿），
       release（change）时一次性持久化。 */
    const sm = $('wo-sem');
    if (sm){
      sm.addEventListener('input', () => {
        uiWaterOpts.semStrength = Number(sm.value);
        const v = $('wo-sem-v'); if (v) v.textContent = Number(sm.value).toFixed(2);
        try { if (window.__water && window.__water.setSemStrength)
                window.__water.setSemStrength(uiWaterOpts.semStrength); } catch (_) {}
      });
      sm.addEventListener('change', () => persistPrefs({ waterOpts: uiWaterOpts }, true));
    }
    const ph = $('wo-physics');
    if (ph) ph.addEventListener('change', () => waterOptsPatch({ physics: ph.checked }));
    const pu = $('wo-pulse');
    if (pu) pu.addEventListener('change', () => waterOptsPatch({ semPulse: pu.checked }));
    bindSeg('wo-eventmode', v => waterOptsPatch({ eventMode: v }));
    bindSeg('wo-bank',      v => waterOptsPatch({ bank: v }));
    bindSeg('wo-tier',      v => waterOptsPatch({ tier: v }));
    /* 换一批波形：确定性重掷随机银行（rndSeed 同种子 → 逐位可复现） */
    const rer = $('wo-reroll');
    if (rer) rer.addEventListener('click', () => {
      let s = 1;
      try { s = (Date.now() % 100000) | 0; } catch (_) {}
      try { if (window.__water && window.__water.rndSeed) window.__water.rndSeed(s); } catch (_) {}
      toast('波形已重掷');
    });
    /* 恢复默认：本地参数组回出厂 + __water 回观测基线（SEM_BASE / 默认档位） */
    const rst = $('wo-reset');
    if (rst) rst.addEventListener('click', () => {
      uiWaterOpts = JSON.parse(JSON.stringify(WATER_OPTS_DEF));
      persistPrefs({ waterOpts: uiWaterOpts }, true);
      try { if (window.__water && window.__water.resetOpts) window.__water.resetOpts(); } catch (_) {}
      applyWaterOpts();
      toast('水面参数已恢复默认');
    });
    const adv = $('wo-adv'), advBtn = $('wo-adv-btn');
    if (adv && advBtn) advBtn.addEventListener('click', () => adv.classList.toggle('open'));
  }

  /* ---- 自定义壁纸导入（老 UI wp-type-row / wp-input / importWallpaper 移植）
       支持图片/视频/链接/壁纸引擎；前端即时把源喂给背景（bgimg 亚克力或
       Poster 层），并经后端 WallpaperImporter 持久化。 */
  /* ═══ T10 自定义壁纸选项记忆 ═══
     localStorage r54.wpchoice = {type, url}，记住上次选的 tab 与导入的图片 URL。
     不走 prefs 白名单（无需后端改动）；壁纸引擎类型照存但不改其自身逻辑。 */
  const WP_CHOICE_KEY = 'r54.wpchoice';
  function wpChoiceGet(){
    try { return JSON.parse(localStorage.getItem(WP_CHOICE_KEY) || '{}') || {}; }
    catch (_e) { return {}; }
  }
  function wpChoiceSet(patch){
    try { const o = wpChoiceGet(); Object.assign(o, patch || {});
          localStorage.setItem(WP_CHOICE_KEY, JSON.stringify(o)); } catch (_e) {}
  }
  let wpType = 'image';
  /* cover=歌曲封面 | image=图片 | link=链接 | video=视频壁纸 | wallpaper=壁纸引擎工程
     ⚠ R62 #10：恢复 video / wallpaper 两型（R61 曾移除）。用户原话：作为按钮
     放皮肤自定义设置里，**共用一个导入框**。video 前端直接可用（#bgvideo +
     FileReader/dataURL/直链）。
     ⚠ R65：wallpaper 真导入 —— 后端 WallpaperImporter 本就能解析工程
     project.json / Steam 创意工坊链接 / zip：scene 3D 壁纸退化为 preview 图、
     video/web 场景正常渲染（core/skin.py WallpaperEngineSkin.render_descriptor），
     并非「必须桌面端」。前端接 REST：Steam 链接/ID → import-engine；本地工程
     zip → import-zip 上传。 */
  /* ═══ R61：切回「歌曲封面」═══
     自定义壁纸一旦导入（wp-active），render() 就不再喂封面 —— 用户切不回封面背景。
     摘除 wp-active / 清 #bgimg 的壁纸 src 后，让 render() 重新以 st.cover 喂 bgimg。
     这里提供 useCoverSkin()：body+img 摘 wp-active，且 bgimg src 先置空、摘 has-bg
     （避免旧壁纸残留一帧），再按当前效果模式决定是否立即补封面。 */
  function useCoverSkin(){
    try { hideWebWallpaper(); } catch (_) {}   /* T18 */
    /* R62 #10：切回封面 → 停掉视频壁纸 */
    try { hideVideoWallpaper(); } catch (_) {}
    const bd = document.body, bgimg = $('bgimg');
    bd.classList.remove('wp-active');
    uiWallpaper = 'cover';               /* R78：背景内容偏好回「跟随封面」 */
    if (bgimg){
      bgimg.classList.remove('wp-active');
      bgimg.removeAttribute('src');        /* 逼 render() 用新封面重新喂 */
      bgimg.classList.remove('has-bg');
    }
    /* 当前曲封面（st.cover）即时补上 —— 走统一路径（同时喂 Poster 氛围参考）。
       无封面 → ensureDefaultSkin 程序化底（render 内部逻辑一致）。 */
    if (st && st.cover){
      try {
        if (bgimg){
          bgimg.setAttribute('src', st.cover);
          bgimg.classList.add('has-bg');
        }
        try { if (window.Poster) window.Poster.setCover(st.cover); } catch (_) {}
        try { if (window.LiquidBg) window.LiquidBg.setSource(st.cover); } catch (_) {}
      } catch (_) {}
    } else {
      try { ensureDefaultSkin(); } catch (_) {}
    }
    /* ★ 2026-11-19 修：切回封面必须持久化 wallpaper='cover'。
       旧实现只改内存 uiWallpaper + 清 src，没写盘 → 刷新后恢复成上次存的
       'default'（logo），用户得手动再点一次封面才回来。与 applyBrandSkin 对称。 */
    persistPrefs({ wallpaper: 'cover' });
  }
  /* ═══ R67-5：Radio Intelligence 玻璃 logo 作为内置默认背景 ═══
     与 useCoverSkin 一样：摘 wp-active、停视频壁纸、设 bgimg src。
     区别：src 是 /brand/logo.png（项目自带，不入用户壁纸库，不走后端）。
     有歌曲时也会被切到默认 logo（用户原话：默认选项就是这个图片作为壁纸），
     所以这是「皮肤」而非「封面」—— 永远展示 logo，无视 st.cover。 */
  function applyBrandSkin(){
    try { hideVideoWallpaper(); } catch (_) {}
    const bd = document.body, bgimg = $('bgimg');
    bd.classList.add('wp-active');
    uiWallpaper = 'default';             /* R78：持久化镜像 + 切歌不被覆盖判据 */
    if (bgimg){
      bgimg.classList.add('wp-active');
      bgimg.classList.add('has-bg');
      bgimg.setAttribute('src', '/brand/logo.png');
    }
    try { if (window.Poster) window.Poster.setCover(''); } catch (_) {}
    try { if (window.LiquidBg) window.LiquidBg.setSource(''); } catch (_) {}
    persistPrefs({ wallpaper: 'default' });
  }
  /* ═══ R78：按持久化偏好恢复背景内容（启动 / 偏好回填时调用）═══
     uiWallpaper 三态：
       · default  → applyBrandSkin()：固定 logo 壁纸，wp-active，切歌不被封面盖
       · custom   → 用户导入的自定义壁纸：保持当前 bgimg src（刷新后背景内容由
                    /api/ui/prefs 之外的自定义壁纸机制另行恢复，这里不覆盖）；
                    若 bgimg 无 src（视频壁纸等未持久化场景）→ 兜底 ensureDefaultSkin
       · cover    → 跟随封面：交给 render() / ensureDefaultSkin 处理，无需干预
     仅 default 是**纯偏好驱动**（logo 项目内置、无需后端），因此启动恢复只对它
     负责；custom 壁纸是图片文件，其"刷新后仍在"由后端 wallpaper 导入保证。 */
  function applyWallpaperPref(){
    try {
      if (uiWallpaper === 'default'){ applyBrandSkin(); return; }
      if (uiWallpaper === 'custom'){
        const bgi = $('bgimg');
        if (bgi && !bgi.getAttribute('src')) ensureDefaultSkin();
        return;
      }
      /* cover：当前曲封面（若有）由 render() 喂；无封面 ensureDefaultSkin 垫底 */
    } catch (_) {}
  }
  function bindWallpaper(){
    /* T10：启动恢复上次选中的 tab 与导入的图片 */
    (function restoreWpChoice(){
      try {
        const c = wpChoiceGet();
        const row = $('wp-type-row');
        if (c.type && row){
          const btn = row.querySelector('.wp-type[data-wp="' + c.type + '"]');
          if (btn){ row.querySelectorAll('.wp-type').forEach(x => x.classList.remove('active'));
                    btn.classList.add('active'); wpType = c.type; }
        } else if (row) {
          /* 2026-11-19 Qt 记忆修复：Qt 退出丢 localStorage → 从后端 wallpaper 恢复 tab 高亮。
             先摆正到 cover 默认值，后端返回后异步纠正。 */
          const setWpHighlight = (t) => {
            row.querySelectorAll('.wp-type').forEach(x => x.classList.remove('active'));
            const b = row.querySelector('.wp-type[data-wp="' + t + '"]');
            if (b) b.classList.add('active');
            wpType = t;
          };
          setWpHighlight('cover');
          fetch('/api/ui/prefs', { cache: 'no-store' }).then(r => r.json()).then(d => {
            const w = d && d.prefs && d.prefs.wallpaper;
            if (w && row.querySelector('.wp-type[data-wp="' + w + '"]')) setWpHighlight(w);
          }).catch(() => {});
        }
        /* T17：记住的选项要真正应用——之前只恢复高亮不套图，导致选了「歌曲封面」
           却仍保持默认/旧图。cover→摘掉自定义图走回封面；default→内置 logo。 */
        if (c.type === 'cover'){ try { useCoverSkin(); } catch (_e) {} }
        else if (c.type === 'default'){ try { applyBrandSkin(); } catch (_e) {} }
        const inp = $('wp-input');
        if (inp && c.url){ inp.value = c.url; inp.classList.add('has-text');
                           if (c.type === 'image' || c.type === 'link' || c.type === 'wallpaper')
                             { try { applyWallpaperImage(c.url); } catch (_e) {} } }
      } catch (_e) {}
    })();
    const typeRow = $('wp-type-row');
    if (typeRow) typeRow.querySelectorAll('.wp-type').forEach(b => {
      b.addEventListener('click', () => {
        typeRow.querySelectorAll('.wp-type').forEach(x => x.classList.remove('active'));
        b.classList.add('active'); wpType = b.dataset.wp;
        wpChoiceSet({ type: wpType });   /* T10：记住选中的 tab */
        const inp = $('wp-input');
        if (inp) inp.placeholder = {
          cover:'当前播放歌曲封面（无需导入）',
          image:'粘贴图片 URL，或点「选择文件」',
          link:'粘贴壁纸直链 URL',
          video:'粘贴视频直链 URL，或点「选择文件」',
          wallpaper:'粘贴 Steam 创意工坊链接 / 壁纸 ID，或点「选择文件」上传 .zip 工程包',
          /* R67-5：默认 = Radio Intelligence 玻璃 logo（项目自带，不入用户壁纸库） */
          default:'Radio Intelligence 玻璃 logo（项目内置默认）'
        }[wpType] || inp.placeholder;
        if (wpType === 'cover') useCoverSkin();   /* R66：切封面即时可见，不再 toast */
        if (wpType === 'default') applyBrandSkin();   /* R67-5：内置默认背景 */
      });
    });
    const imp = $('wp-import'), inp = $('wp-input'), pick = $('wp-pick'), file = $('wp-file');
    /* ═══ R57-2：本地图片百分百可用 ═══
       http 页面不能读 file:// 路径 → 旧「粘贴本地路径」在浏览器里必失败。
       新增「选择文件」：FileReader 把图片读成 dataURL → 直接喂背景层，
       无需后端 WallpaperImporter。拖放同样优先走 FileReader。 */
    function readFileAsWallpaper(f){
      if (!f) return;
      /* R65：wallpaper 类型 + .zip → 上传后端解包导入（壁纸引擎工程包 /
         Steam 导出 zip）。zip 不是图片/视频，须在图片校验前拦截。 */
      if (wpType === 'wallpaper' && /\.zip$/i.test(f.name || '')){
        uploadWallpaperZip(f);
        return;
      }
      /* R62 #10：video 类型接受视频文件（其它一律按图片校验） */
      const isVideo = wpType === 'video'
        && (/^video\//.test(f.type || '')
            || /\.(mp4|webm|ogg|ogv|mov)$/i.test(f.name || ''));
      if (!isVideo && f.type && f.type.indexOf('image/') !== 0){
        toast('仅支持图片 / 视频文件'); return;
      }
      /* ═══ R63：图片文件导入 = 真实后端持久化 ═══
         本地图片（image/link/wallpaper 类型选的文件）POST 原字节到
         /api/wallpapers/import → SkinManager 落 plugins/skins + assets，
         重启仍在。仅 video 保留 FileReader dataURL 即时预览（视频壁纸
         网页端不持久化，属诚实降级）。
         R66：导入成功后不再刷「我的壁纸」网格（该网格已随冗余功能移除），
         直接用响应里的 /skin-assets URL 应用为背景。 */
      if (!isVideo){
        uploadWallpaperFile(f);
        return;
      }
      const rd = new FileReader();
      rd.onload = () => {
        applyWallpaperVideo(String(rd.result)); toast('视频壁纸已应用');
      };
      rd.onerror = () => toast('读取文件失败');
      rd.readAsDataURL(f);
    }
    if (pick && file){
      pick.addEventListener('click', () => { file.value = ''; file.click(); });
      file.addEventListener('change', () => {
        const f = file.files && file.files[0];
        if (f){ readFileAsWallpaper(f); }
      });
    }
    if (imp && inp) imp.addEventListener('click', () => {
      const src = inp.value.trim();
      if (!src){ toast('请先粘贴 URL / 路径，或点「选择文件」'); return; }
      try { importWallpaperSrc(wpType, src); } catch (_) {}
    });
    /* 拖放：R57-2 优先 FileReader 读真实文件（次选填路径文本） */
    const dz = $('wp-input') || document.body;
    dz.addEventListener('dragover', e => e.preventDefault());
    dz.addEventListener('drop', e => {
      e.preventDefault();
      const f = e.dataTransfer.files && e.dataTransfer.files[0];
      if (f){
        /* R62 #10：video 类型也放行 —— readFileAsWallpaper 内部按 wpType 分流 */
        readFileAsWallpaper(f);
      }
    });
  }

  /* ═══ 本地图片 / 壁纸引擎上传（后端持久化导入）═══
     R66：原「我的壁纸」网格（loadMyWallpapers / renderWallpaperGrid）已移除 ——
     与上方「自定义壁纸」重复。上传仍走后端 SkinManager 落盘（重启仍在），
     收尾只做一件事：把返回的 /skin-assets URL 直接应用为背景。 */
  function uploadWallpaperFile(f){
    if (!f) return;
    const okType = /^image\//.test(f.type || '')
      || /\.(png|jpe?g|gif|webp|bmp)$/i.test(f.name || '');
    if (!okType){ toast('仅支持图片文件（持久化导入）'); return; }
    toast('正在导入壁纸…');
    fetch('/api/wallpapers/import?filename=' + encodeURIComponent(f.name), {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: f
    }).then(r => r.json()).then(d => {
      if (!d || !d.ok){ toast('导入失败：' + ((d && d.error) || '未知错误')); return; }
      /* 应用为背景（/skin-assets URL 喂 #bgimg → 自定义壁纸 wp-active） */
      if (d.url) applyWallpaperImage(d.url);
      toast('壁纸已导入并应用');
    }).catch(() => toast('导入失败（后端未连接？）'));
  }
  /* ═══ R65：壁纸引擎 zip 上传导入（场景/网页壁纸工程包）═══
     之前 wallpaper 类型只有「需桌面端解析」toast；现在 zip 直传后端解包。
     后端 _import_zip 找 project.json（scene→preview 图 / video / web）或首个
     媒体文件导入。前端收尾同图片：应用背景 + 刷网格。 */
  function uploadWallpaperZip(f){
    if (!f) return;
    toast('正在导入壁纸引擎工程…');
    fetch('/api/wallpapers/import-zip?filename=' + encodeURIComponent(f.name), {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: f
    }).then(r => r.json()).then(d => {
      if (!d || !d.ok){ toast('导入失败：' + ((d && d.error) || '未知错误')); return; }
      if (d && d.ok) applyWallpaperByDescriptor(d);   /* T18：按类型分派 */
      toast('壁纸引擎已导入');
    }).catch(() => toast('导入失败（后端未连接？）'));
  }
  function importWallpaperSrc(type, src){
    /* 图片/链接 → 背景图；歌曲封面 → useCoverSkin；视频 → #bgvideo；
       壁纸引擎 → 真导入（R65：此前只 toast 引导「需桌面端解析」，用户反馈
       「场景和网页都没办法导入」。后端 WallpaperImporter 本就能解析工程 /
       Steam 链接 / zip —— scene 退化为 preview 图、video/web 正常渲染，
       无需桌面端。现在接 REST 导入并把返回的 /skin-assets URL 应用为背景）。 */
    if (type === 'cover'){ useCoverSkin(); return; }   /* R66：不再 toast 复述 */
    if (type === 'video'){
      applyWallpaperVideo(src);
      return;
    }
    if (type === 'wallpaper'){
      /* Steam 创意工坊链接 / 数字 ID → 后端定位本地订阅并导入 */
      const isSteam = /steamcommunity\.com\/(sharedfiles|workshop)/i.test(src)
        || /^https?:\/\//i.test(src) || /^\d{4,}$/.test(src.trim());
      if (isSteam){
        importWallpaperEngine(src);
        return;
      }
      /* 本地盘符 / file:// 路径 → 浏览器读不到 → 引导上传 zip */
      toast('网页无法直接读取本地工程文件夹，请用「选择文件」上传壁纸引擎导出的 .zip');
      const file = $('wp-file'); if (file) file.click();
      return;
    }
    const looksLocal = /^[a-zA-Z]:[\\/]|^file:\/\//.test(src);
    if (looksLocal){
      toast('浏览器无法直接读本地路径，请点「选择文件」上传');
      const file = $('wp-file'); if (file) file.click();
      return;
    }
    if (type === 'image' || type === 'link'){
      applyWallpaperImage(src);   /* R66：背景即时变化，不再 toast */
    } else {
      toast('暂不支持该类型');
    }
  }
  function importWallpaperEngine(src){
    /* ⚠ R65：真正走后端 WallpaperImporter（不再是「需桌面端」降级）。
       scene 3D 壁纸退化为 preview 图 / video / web 场景由渲染层解析。 */
    fetch('/api/wallpapers/import-engine?source=' + encodeURIComponent(src)
          + '&name=wallpaper', { method: 'POST' })
      .then(r => r.json()).then(d => {
        if (!d || !d.ok){ toast('导入失败：' + ((d && d.error) || '后端未连接')); return; }
        /* R66：原 activateWallpaper(key) 靠"重拉网格找 url"，网格已移除
           → 直接用导入响应里的 url 应用背景。 */
        if (d && d.ok && (d.media || d.url)) applyWallpaperByDescriptor(d);   /* T18 */
        else if (!(d && d.ok)) toast('导入失败：' + ((d && d.error) || '后端未连接'));
        else useCoverSkin();
        toast('壁纸引擎已导入');
      }).catch(() => toast('导入失败（后端未连接？）'));
  }
  /* ═══ R62 #10：视频壁纸（#bgvideo）═══
     与 #bgimg 同为皮肤内容位、互斥显示：
       · 设 src → #bgvideo.has-bg 显示、#bgimg 隐藏（has-bg 摘除）；
       · body.wp-active = 自定义壁纸优先（切歌不被封面覆盖）。
     src 支持：远程直链 URL / FileReader dataURL（本地文件）。
     ⚠ 视频自动播放策略：muted+loop+playsinline+autoplay（HTML 属性已设），
     play() 失败（浏览器策略）时静默 —— 壁纸是装饰，不能因播放策略报错。 */
  function applyWallpaperVideo(src){
    const v = $('bgvideo'); if (!v) return;
    if (!src){ return; }
    v.src = src;
    v.classList.add('has-bg', 'wp-active');
    document.body.classList.add('wp-active');
    uiWallpaper = 'custom';              /* R78：自定义视频壁纸（非 cover/default） */
    /* 隐藏图片壁纸（若有）—— 皮肤内容位同一时刻只有一个可见 */
    const bgimg = $('bgimg');
    if (bgimg){
      bgimg.classList.remove('wp-active', 'has-bg');
      bgimg.removeAttribute('src');
    }
    try { v.play().catch && v.play().catch(() => {}); } catch (_) {}
    /* 氛围参考同步（Poster/LiquidBg 吃不到视频帧，仅清掉旧图引用） */
    try { if (window.Poster) window.Poster.setCover(''); } catch (_) {}
    try { if (window.LiquidBg) window.LiquidBg.setSource(''); } catch (_) {}
  }
  /* T18：切回其它壁纸时隐藏 web iframe */
  function hideWebWallpaper(){
    const f = $('bgweb'); if (f){ f.classList.remove('has-bg'); f.removeAttribute('src'); }
  }
  /* T18：web 壁纸（壁纸引擎 web 类型 / 动态网页）—— iframe 铺满背景层，
     与 #bgimg/#bgvideo 互斥。sandbox 只给脚本+同源，壁纸是装饰不拦截交互。 */
  function applyWallpaperWeb(src){
    const f = $('bgweb'); if (!f || !src) return;
    f.src = src; f.classList.add('has-bg');
    document.body.classList.add('wp-active');
    uiWallpaper = 'custom'; persistPrefs({ wallpaper: 'custom' }); /* 2026-11-19 记忆修复 */
    try { hideVideoWallpaper(); } catch (_e) {}
    const im = $('bgimg'); if (im){ im.classList.remove('wp-active','has-bg'); im.removeAttribute('src'); }
  }
  /* T18：按后端导入响应 descriptor 分派——image/video/web/scene_preview。
     scene_preview 后端已给 preview 静态图，当图片显示（明确非真 3D）。 */
  function applyWallpaperByDescriptor(d){
    if (!d) return;
    const type = d.type || ((d.descriptor||{}).type) || 'image';
    const url = d.media || d.url || '';
    if (!url) return;
    if (type === 'video') applyWallpaperVideo(url);
    else if (type === 'web') applyWallpaperWeb(url);
    else applyWallpaperImage(url);   // image / scene_preview / 其它 → 静态图
  }
  function hideVideoWallpaper(){
    const v = $('bgvideo');
    if (v){
      v.pause && v.pause();
      v.removeAttribute('src');
      v.classList.remove('has-bg', 'wp-active');
    }
  }
  function applyWallpaperImage(src){
    try { hideWebWallpaper(); } catch (_) {}   /* T18 */
    wpChoiceSet({ url: src });   /* T10：记住导入的图片 URL（下次恢复） */
    /* R62 #10：图壁纸激活 → 停掉视频壁纸（皮肤内容位互斥，同一时刻仅一个可见） */
    try { hideVideoWallpaper(); } catch (_) {}
    /* ═══ R57-2：导入壁纸"百分百可见"重构 ═══
       旧实现只设 #bgimg.src，而 #bgimg 默认 opacity:0、仅 acrylic 模式
       （body.bgmode-acrylic #bgimg）才可见 → poster 模式导入"没反应"。
       新模型：#bgimg 是唯一背景壁纸位（见 R57-1 CSS），有 src 即显示；
       这里设 src + body.wp-active（自定义壁纸优先：切歌不被封面覆盖、
       锐利显示不做亚克力模糊）。歌词海报画布透明叠上，壁纸即时可见。
       ⚠ R59：wp-active **不再**强制 filter:none（CSS 覆盖已删）——
       明亮亚克力 blur 对自定义壁纸同样生效（用户原话：亚克力是背景效果，
       自定义壁纸/封面只是背景内容，两者正交）。 */
    const bgimg = $('bgimg');
    const bd = document.body;
    if (bgimg){
      bgimg.src = src;
      bgimg.classList.add('has-bg', 'wp-active');
      bd.classList.add('wp-active');
      uiWallpaper = 'custom'; persistPrefs({ wallpaper: 'custom' }); /* R78 记忆修复 2026-11-19 */
    }
    /* 画布氛围色参考（透明海报下局部光晕跟随壁纸） */
    try { if (window.Poster) window.Poster.setCover(src); } catch (_) {}
    try { if (window.LiquidBg) window.LiquidBg.setSource(src); } catch (_) {}
  }
  /* 切歌时若存在自定义壁纸则不被封面覆盖 —— render() 只在 body 无 wp-active 时喂封面 */
  function isCustomWallpaper(){ return document.body.classList.contains('wp-active'); }

  /* 不喜欢的音乐列表（设置面板只读 + 移除） */
  function renderDislikeList(){
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
        return `<div class="dl-row" data-key="${esc(it.key || '')}">
           <span class="dl-name" title="${esc(t)}">${esc(t)}</span>
           <span class="dl-artist">${esc(a)}</span>
           <button class="dl-rm" title="移除" data-key="${esc(it.key || '')}">✕</button>
         </div>`;
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
  function bindSettingsEvents(){
    /* ═══ R62 #8：设置分组折叠（移植老 UI .set-fold 折叠机制）═══
       点 data-foldable 分组的 h4 → 整组 collapsed 折叠/展开。
       委派在 document 上：动态渲染的分组（如不喜欢列表刷新）也生效。 */
    document.addEventListener('click', ev => {
      const h = ev.target && ev.target.closest
        ? ev.target.closest('.set-group[data-foldable] > h4') : null;
      if (!h) return;
      const grp = h.parentElement;
      if (grp) grp.classList.toggle('collapsed');
    });
    const gear   = $('settings-gear');
    /* ═══ R62 #9：本地曲库导入 / 重新扫描 ═══
       导入文件夹：POST /api/library/import/folder?folder=…（后端扫描入库）。
       重新扫描：POST /api/library/scan（扫配置根目录）。成功后刷新曲库列表
       （若侧边面板正开在曲库 tab）并 toast 结果。 */
    const libImp = $('lib-import-folder'), libInp = $('lib-folder-input');
    if (libImp && libInp) libImp.addEventListener('click', () => {
      const folder = libInp.value.trim();
      if (!folder){ toast('请先输入文件夹路径'); return; }
      fetch('/api/library/import/folder?folder=' + encodeURIComponent(folder),
            { method:'POST' })
        .then(r => r.json()).then(d => {
          if (d && d.ok) toast('已导入 ' + (d.added || 0) + ' 首到曲库');
          else toast((d && d.error) || '导入失败');
        }).catch(() => toast('导入失败：后端未连接'));
    });
    const libScan = $('lib-scan');
    if (libScan) libScan.addEventListener('click', () => {
      fetch('/api/library/scan', { method:'POST' })
        .then(r => r.json()).then(d => {
          if (d && d.ok) toast('扫描完成，新增 ' + (d.added || 0) + ' 首');
          else toast((d && d.error) || '扫描失败');
        }).catch(() => toast('扫描失败：后端未连接'));
    });
    const hanger = $('theme-hanger');
    const setClose = $('set-close');
    const themeClose = $('theme-close');
    if (gear) gear.addEventListener('click', () => {
      if ($('settings-panel') && $('settings-panel').classList.contains('open')) closeSettings();
      else openSettings();
    });
    if (hanger) hanger.addEventListener('click', () => {
      if ($('theme-panel') && $('theme-panel').classList.contains('open')) closeTheme();
      else openTheme();
    });
    /* R67-2：原「打开换肤面板」入口（设置卡内 appeal section 的按钮）随整个
       外观 section 一并删除 —— 用户原话"皮肤界面在换肤按钮里，设置页保留=冗余"。
       换肤卡仍由 #theme-hanger 衣架钮 / openTheme() 走原路径。 */
    if (setClose) setClose.addEventListener('click', closeSettings);
    if (themeClose) themeClose.addEventListener('click', closeTheme);
    /* 外点关闭：点任一打开的卡 + 两按钮之外 → 全关 */
    document.addEventListener('click', ev => {
      if (!anyDrawerOpen()) return;
      const t = ev.target;
      if (!t || !t.closest) return;
      if (t.closest('#settings-panel') || t.closest('#theme-panel') ||
          t.closest('#settings-gear') || t.closest('#theme-hanger')) return;
      if ($('settings-panel') && $('settings-panel').classList.contains('open')) closeSettings();
      if ($('theme-panel') && $('theme-panel').classList.contains('open')) closeTheme();
    });
    /* Esc 优先关抽屉（先关设置卡，再关换肤卡） */
    document.addEventListener('keydown', ev => {
      if (ev.key !== 'Escape') return;
      if ($('settings-panel') && $('settings-panel').classList.contains('open')){
        closeSettings(); ev.preventDefault();
      } else if ($('theme-panel') && $('theme-panel').classList.contains('open')){
        closeTheme(); ev.preventDefault();
      }
    });
    /* 背景模式按钮 */
    const bgRow = $('bg-mode-row');
    if (bgRow) bgRow.querySelectorAll('.wp-type').forEach(b => {
      b.addEventListener('click', () => setBgMode(b.dataset.bgmode));
    });
    /* 叠层效果卡片（R93）：点击即切换（segmented card switcher；沿用 dock 铁则
       「简单点亮、禁 :active scale」，无滑块）+ 参数区控件绑定与首次回填。 */
    const onTopRow = $('ontop-row');
    if (onTopRow) onTopRow.querySelectorAll('.wp-type').forEach(b => {
      b.addEventListener('click', () => setOnTop(b.dataset.ontop));
    });
    bindWaterOpts();
    syncOnTopUI();
    applyWaterOpts();
    /* 音频律动开关 */
    const beatToggle = $('beat-toggle');
    if (beatToggle){
      beatToggle.addEventListener('change', () => setBeat(beatToggle.checked));
      beatToggle.checked = uiBeat;
    }
    /* 高亮色 picker / reset
       ⚠ R94：`input`（拖动中连发）保去抖；`change`（松手/关闭取色器）立即落盘收尾。 */
    const picker = $('fx-accent-picker');
    if (picker){
      picker.addEventListener('input', () => {
        uiAccent = picker.value || '';
        persistPrefs({ accent: uiAccent, theme: uiThemeKey });
        applyPrefsTheme();
      });
      picker.addEventListener('change', () => {
        uiAccent = picker.value || '';
        persistPrefs({ accent: uiAccent, theme: uiThemeKey }, true);
        applyPrefsTheme();
      });
    }
    const reset = $('fx-accent-reset');
    if (reset) reset.addEventListener('click', () => {
      uiAccent = '';
      persistPrefs({ accent: '', theme: uiThemeKey }, true);
      applyPrefsTheme();
      /* R66：取色器即时回到主题色，肉眼可见 → 不再 toast。 */
    });
    bindWallpaper();
  }

  /* ---- R57-6 · 左下按钮 hover 显现：驻留 15% 透明，靠近本体/热区即复原 ----
     齿轮(#settings-gear)与衣架(#theme-hanger)在左下角 dock 左外侧纵向排列。
     指针距任一按钮中心 < 72px（大于按钮半径 26 → 感应区比按钮大一圈）
     或正悬停其上 → 摘 .dim（opacity 1）；离开 → 加 .dim（15%）。
     避免按钮在低透明下点不中：pointer-events 恒 auto。 */
  function bindCornerButtons(){
    const btns = [$('settings-gear'), $('theme-hanger')].filter(Boolean);
    if (!btns.length) return;
    /* 初始即 dim（除非已 hover —— 见下方 mousemove 首帧） */
    btns.forEach(b => b.classList.add('dim'));
    document.addEventListener('mousemove', ev => {
      const mx = ev.clientX, my = ev.clientY;
      let near = false;
      for (const b of btns){
        const r = b.getBoundingClientRect();
        const cx = r.left + r.width/2, cy = r.top + r.height/2;
        const dist = Math.hypot(mx - cx, my - cy);
        const hitR = Math.max(36, r.width/2 + 22);   /* 比按钮大一圈 */
        if (dist <= hitR){ near = true; break; }
      }
      btns.forEach(b => b.classList.toggle('dim', !near));
    });
    document.addEventListener('mouseleave', () => btns.forEach(b => b.classList.add('dim')));
  }

  /* ---- 顶部搜索框（老 initSearch L5806-5920 移植） ---- */

/* ═══════ ui-settings.js (设置面板) ═══════ */
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

/* ═══════ ui-eq.js (EQ 面板) ═══════ */
/* ══════════════════════════════════════════════════════════
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
    box.innerHTML = EQ_FREQS.map((f, i) => `
      <div class="eq-band" data-i="${i}">
        <input type="range" min="-12" max="12" step="1" value="${eqState.bands[i] || 0}"
               aria-label="EQ ${fmtFreq(f)}Hz">
        <span class="b-db">${fmtDb(eqState.bands[i] || 0)}</span>
        <span class="b-freq">${fmtFreq(f)}</span>
      </div>`).join('');
    box.querySelectorAll('.eq-band input').forEach((inp, i) => {
      inp.addEventListener('input', () => onBandInput(i, inp));
      /* dblclick 归零 */
      inp.addEventListener('dblclick', () => { inp.value = 0; onBandInput(i, inp); });
      /* T8：初始化就上色（只写视觉变量，不触发 POST） */
      (function(){ const pct = ((eqState.bands[i] || 0) + 12) / 24 * 100;
        inp.style.setProperty('--fill-pct', Math.abs(pct - 50) + '%');
        inp.style.setProperty('--fill-from', pct >= 50 ? '50%' : pct + '%');
        inp.style.setProperty('--fill-to', pct >= 50 ? pct + '%' : '50%'); })();
    });
  }
  function fmtFreq(f){ return f >= 1000 ? (f/1000) + 'k' : String(f); }
  function fmtDb(v){ return (v > 0 ? '+' : '') + v; }
  function onBandInput(i, inp){
    const v = parseInt(inp.value, 10) || 0;
    /* T8：已滑/未滑两色 —— 从中点(0dB)到 thumb 方向着色 */
    if (inp) {
      const pct = (v + 12) / 24 * 100;
      inp.style.setProperty('--fill-pct', Math.abs(pct - 50) + '%');
      inp.style.setProperty('--fill-from', pct >= 50 ? '50%' : pct + '%');
      inp.style.setProperty('--fill-to',   pct >= 50 ? pct + '%' : '50%');
    }
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
    box.innerHTML = Object.keys(EQ_PRESETS).map(k => `
      <button class="eqp-chip${eqState.preset === k ? ' on' : ''}" data-preset="${k}">${EQ_PRESETS[k]}</button>`).join('');
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

/* ═══════ ui-eggs.js (彩蛋) ═══════ */
/* ═════════════════════════════════════════════════════════════════════════
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
  /* var $ 去重：复用 ui-core.js 的 const $（等价定义） */
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
  ].join('\n');

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

/* ═══════ ui-init.js (初始化/轮询/主循环) ═══════ */
/* ══════════════════════════════════════════════════════════
   ui-init.js — Radio Intelligence 前端模块 · initSearchR54/initR54/__r54 · 启动 bootstrap/__r53/mainLoop
   自 webapi/static/index.html 内嵌 <script> L3905-L4158 拆出（R58-7 #46）
   与原 index.html 内所有 ui-*.js 共享顶层作用域 → 加载顺序即执行顺序，勿打乱
   ══════════════════════════════════════════════════════════ */
  function initSearchR54(){
    const box = $('search-box'), input = $('search-input'),
          clear = $('search-clear'), suggest = $('search-suggest');
    if (!box || !input || !suggest) return;
    let cur = -1, items = [], searchTimer = 0;

    /* ═══ R86 · 切换搜索源开关（搜索框内右侧）═══
       交互 = 点击循环切换可用音源（2 个时即等价于 on/off 开关），选择持久化到
       ui_prefs.searchSource；样式见 index.html 的 .ss-src（透明轨道 + 渐变发光滑块）。 */
    const srcWrap = $('ss-src'), srcLb = $('ss-src-lb'), srcCb = $('ss-src-cb');
    let srcList = [], srcIdx = 0, srcKey = '';

    function renderSrc(){
      if (!srcWrap) return;
      if (!srcList.length){ srcWrap.style.display = 'none'; return; }
      srcWrap.style.display = '';
      const curSrc = srcList[srcIdx] || srcList[0];
      srcKey = curSrc.key || '';
      /* 「可切换」= 除「自动」外还有 ≥2 个真实音源；否则压暗禁用（避免点了没反应） */
      const realCount = srcList.filter(s => !!s.key).length;
      const switchable = realCount >= 2;
      if (srcLb) srcLb.textContent = curSrc.short || curSrc.key || '自动';
      if (srcCb) srcCb.checked = srcIdx > 0;
      srcWrap.classList.toggle('solo', !switchable);
      srcWrap.classList.toggle('on', srcIdx > 0);
      srcWrap.title = '切换搜索源：' + srcList.map(s => s.label || s.key || '自动').join(' / ')
                    + (switchable ? '' : '（仅一个可用音源）');
    }
    function loadSearchSources(){
      fetch('/api/search/sources').then(r => r.json()).then(d => {
        const all = (d && Array.isArray(d.sources)) ? d.sources : [];
        /* ⚠ R86-fix：只保留「已启用」音源 —— 后端旧进程可能仍把模板插件
           （key=example，短名兜底显示成「EXAM」）也列进来；此处兜底过滤，
           改完刷新页面即生效，无需重启后端。 */
        srcList = all.filter(s => s && s.enabled !== false && s.searchable !== false);
        const want = (d && d.current) || '';
        const i = srcList.findIndex(s => s.key === want);
        srcIdx = i >= 0 ? i : 0;
        renderSrc();
      }).catch(() => { if (srcWrap) srcWrap.style.display = 'none'; });
    }
    if (srcCb) srcCb.addEventListener('change', () => {
      if (srcList.filter(s => !!s.key).length < 2){ renderSrc(); return; }
      srcIdx = (srcIdx + 1) % srcList.length;
      renderSrc();
      fetch('/api/ui/prefs', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ searchSource: srcKey }),
      }).catch(() => {});
      toast('搜索源：' + (srcList[srcIdx].label || srcKey || '自动'));
      /* 切源即重搜当前关键词（无关键词则等下次输入） */
      const kw = input.value.trim().toLowerCase();
      if (kw){ clearTimeout(searchTimer); backendSearch(kw); }
    });
    loadSearchSources();

    function escS(v){ return esc(v); }

    function render(){
      const kw = input.value.trim().toLowerCase();
      box.classList.toggle('has-text', input.value.length > 0);
      if (!kw){
        suggest.innerHTML = ''; box.classList.remove('show-suggest');
        items = []; cur = -1; return;
      }
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => backendSearch(kw), 300);
    }
    function backendSearch(kw){
      /* ⚠ R86：带上当前搜索源（srcKey 为空 = 后端自动竞速，谁先返回用谁）。
         切换音源后必须重搜，否则用户切了源却仍看到别家的结果。 */
      const u = '/api/search?q=' + encodeURIComponent(kw)
              + (srcKey ? '&source=' + encodeURIComponent(srcKey) : '');
      fetch(u).then(r => r.json()).then(d => {
        /* 过期结果丢弃 */
        if (input.value.trim().toLowerCase() !== kw) return;
        items = (d && Array.isArray(d.entries)) ? d.entries : [];
        if (!items.length){
          suggest.innerHTML = '<div class="ss-empty">未找到相关歌曲（可换关键词）</div>';
          box.classList.add('show-suggest'); cur = -1; return;
        }
        suggest.innerHTML = items.map((s, i) =>
          `<div class="ss-item${i===cur?' cur':''}" data-i="${i}">
             <span class="ss-name">${escS(s.title || '')}</span>
             <span class="ss-artist">${escS((s.artist||'') + (s.source ? ' · ' + s.source : ''))}</span>
           </div>`).join('');
        box.classList.add('show-suggest');
      }).catch(() => {
        suggest.innerHTML = '<div class="ss-empty">搜索失败（后端未连接？）</div>';
        box.classList.add('show-suggest');
      });
    }
    function playItem(s){
      if (!s) return;
      const src = s.source || '', ext = s.ext_id || '';
      const id = Number(s.id);
      /* ⚠ 在线曲无本地 db id → 走 /api/search/play（source+ext_id）；
         本地曲 → /api/play/track/{id}。绝不用队列下标。 */
      let url = null;
      if (src && ext){
        const q = new URLSearchParams();
        q.set('source', src); q.set('ext_id', ext);
        if (s.title) q.set('title', s.title);
        if (s.artist) q.set('artist', s.artist);
        if (s.album) q.set('album', s.album);
        if (s.cover) q.set('cover', s.cover);
        /* ⚠ R86：duration 必须透传 —— 缺它时 seed 曲目时长=0，
           后端 state().duration=0 → 进度条永远不动（且转场口播被跳过）。 */
        if (s.duration > 0) q.set('duration', String(s.duration));
        url = '/api/search/play?' + q.toString();
      } else if (Number.isFinite(id) && id > 0){
        url = '/api/play/track/' + id;
      }
      if (url) fetch(url, { method:'POST' }).then(r => r.json()).catch(() => {});
      toast('♪ ' + (s.title || '') + (s.artist ? ' — ' + s.artist : ''));
      box.classList.remove('show-suggest','has-text');
      input.value = ''; items = []; cur = -1; input.blur();
    }
    function pick(i){
      if (i >= 0 && i < items.length) playItem(items[i]);
    }
    input.addEventListener('input', () => { cur = -1; render(); });
    input.addEventListener('keydown', ev => {
      ev.stopPropagation();          // 阻止冒泡到全局卡片方向键
      if (ev.key === 'ArrowDown'){ ev.preventDefault(); cur = Math.min(cur + 1, items.length - 1); if (box.classList.contains('show-suggest')) highlight(); }
      else if (ev.key === 'ArrowUp'){ ev.preventDefault(); cur = Math.max(cur - 1, -1); if (box.classList.contains('show-suggest')) highlight(); }
      else if (ev.key === 'Enter'){ ev.preventDefault(); if (cur >= 0) pick(cur); }
      else if (ev.key === 'Escape'){ suggest.innerHTML=''; box.classList.remove('show-suggest'); input.blur(); }
    });
    function highlight(){
      suggest.querySelectorAll('.ss-item').forEach((el, i) =>
        el.classList.toggle('cur', i === cur));
      const el = suggest.querySelector('.ss-item.cur');
      if (el && el.scrollIntoView) el.scrollIntoView({ block:'nearest' });
    }
    if (clear) clear.addEventListener('click', () => {
      input.value = ''; render(); input.focus();
    });
    /* 建议点选（mousedown 避免 input blur 抢先） */
    suggest.addEventListener('mousedown', ev => {
      const item = ev.target.closest('.ss-item'); if (!item) return;
      ev.preventDefault();
      const idx = +item.getAttribute('data-i');
      if (Number.isFinite(idx)) pick(idx);
    });
    /* ═══ R56：鼠标靠近显现 / 远离消失 —— 触发范围比搜索栏本体大一圈 ═══
       老逻辑只在顶部 130px 竖向热区 + 中心窄带内才显示；
       R56 改为**包围盒外扩**：以搜索栏为中心、横向/纵向都放大约 1.6 倍矩形
       （含其正上方到顶部的空档），指针一靠近就显现，离开才消失。
       移入搜索栏本体 / 输入聚焦时**绝不消失**（保证可搜索）。 */
    /* ═══ R62：输入中恒显 —— 鼠标远离/移出窗口也不能藏 ═══
       用户原话：「搜索栏在打字的时候不能隐藏，要一直显现。哪怕鼠标不在触发
       范围，在打字搜索的时候也要显现。」
       keepVisible = 聚焦 或 有非空输入内容。满足任一条件时 mousemove /
       mouseleave / blur 一律不得摘 .visible（blur 只在失焦且内容为空时允许藏）。 */
    function searchKeepVisible(){
      return document.activeElement === input
          || !!(input.value && input.value.trim().length > 0)
          || box.classList.contains('show-suggest');
    }
    document.addEventListener('mousemove', ev => {
      if (box.classList.contains('ui-hidden')) return;
      const r = box.getBoundingClientRect();
      /* 触发区 = 包围盒外扩（比栏大一圈）：宽 ×1.5、高含顶部空档 60px */
      const padX = Math.max(70, r.width * 0.3);
      const padY = Math.max(46, 40 + r.height * 0.4);
      const nearX = ev.clientX >= r.left - padX && ev.clientX <= r.right + padX;
      const nearY = ev.clientY >= Math.max(0, r.top - padY) && ev.clientY <= r.bottom + 26;
      const near = (nearX && nearY) || box.contains(ev.target);
      const vis = box.classList.contains('visible');
      if (near && !vis) box.classList.add('visible');
      else if (!near && vis && !box.contains(ev.target)
               && !searchKeepVisible()) box.classList.remove('visible');
    });
    /* ⚠ R62：鼠标整体移出窗口时，若仍在输入（聚焦/有内容/建议开着）不得隐藏 */
    document.addEventListener('mouseleave', () => {
      if (!searchKeepVisible()) box.classList.remove('visible');
    });
    input.addEventListener('focus', () => box.classList.add('visible'));
    input.addEventListener('input', () => box.classList.add('visible'));
    input.addEventListener('blur', () => {
      requestAnimationFrame(() => {
        /* 焦点在输入框/建议列表内 → 保持可见 */
        if (box.contains(document.activeElement) || box.contains(document.querySelector(':focus'))) return;
        /* ⚠ R62：内容非空（用户打完字点外面看建议/刚要复制）也保持可见；
           只有「失焦 + 内容已清空」才收起。真正清空动作由 clear 按钮/
           playItem 完成（它们摘 .visible 前会先清 input.value）。 */
        if (searchKeepVisible()) return;
        box.classList.remove('visible');
      });
    });
    /* 点外部收起建议 */
    document.addEventListener('click', ev => {
      if (!box.contains(ev.target)) box.classList.remove('show-suggest');
    });
  }

  /* ★★ R110（2026-09-13）一次性偏好迁移：语义强度档默认 **0.6 → 1.0**（配 ① 加权聚合）。
     为什么必须做：本文件的恢复逻辑是「**存档里有的就覆盖**」
     ⇒ 用户上一次存的 `semStrength:0.6` 会**静默盖掉新默认**。
     实测踩到：只改 `ui-water.js` 的 `let semStrength = 1.0` 时，真机端到端
     （`verify_r95b` C5）**纹丝不动（仍是 2.06×）** —— 因为 `ui-skinset.js` 在 init 时
     用 `setSemStrength(0.6)` 又推了回去；`resetOpts()` 里还有第三个 0.6。
     ⇒ 用 `semOptV` 标记**只强制刷一次**；之后用户手动调的值照常被尊重。
     ⚠ **两条恢复路径（localStorage / 后端 prefs）都要调** —— 只改一条会漏另一半。 */
  const SEM_OPT_V = 1;
  function migrateSemOpts(src){
    if (Number((src && src.semOptV) || 0) >= SEM_OPT_V) return;
    uiWaterOpts.semStrength = 1.0;          // 新默认（三处定义须同步）
    uiWaterOpts.semOptV = SEM_OPT_V;
    try { persistPrefs({ waterOpts: uiWaterOpts }, true); } catch (_) {}
  }

  /* ---- 初始化：加载偏好 → 应用 + 绑事件 ---- */
  (function initR54(){
    /* 先绑事件（本地兜底可用），再异步拉后端偏好 */
    bindSettingsEvents();
    bindCornerButtons();
    initSearchR54();
    /* 本地镜像先应用（offline/演示也有主题可用） */
    try {
      const f = JSON.parse(localStorage.getItem('r54.ui') || '{}');
      if (f.theme && THEMES.find(t => t.key === f.theme)) uiThemeKey = f.theme;
      if (typeof f.accent === 'string') uiAccent = f.accent;
      /* 旧值 flow → 归并到 poster（R56 去掉流动水样） */
      if (f.bgmode === 'acrylic') uiBgMode = 'acrylic';
      if (typeof f.beat === 'boolean') uiBeat = f.beat;
      if (typeof f.wallpaper === 'string') uiWallpaper = f.wallpaper;  // R78
      /* R93：叠层效果（ontop 主键 / water 旧键）+ 水面参数组 */
      if (f.ontop === 'none' || f.ontop === 'water') uiOnTop = f.ontop;
      else if (typeof f.water === 'boolean') uiOnTop = f.water ? 'water' : 'none';
      if (f.waterOpts && typeof f.waterOpts === 'object'){
        for (const k in f.waterOpts){ if (k in uiWaterOpts) uiWaterOpts[k] = f.waterOpts[k]; }
      }
      migrateSemOpts(f.waterOpts);
    } catch (_) {}
    /* ═══ R78：首帧即按本地 wallpaper 偏好定背景内容 ═══
       必须在第一次 render() 之前执行 —— 用户上次选「默认」logo 壁纸时,
       首帧就挂 wp-active + logo;否则 render() 按默认 cover 语义处理,
       随后 2s 轮询带回 st.cover 会先把封面铺上,再等 fetch 回填才纠正 →
       闪一下封面(用户报"皮肤默认,切歌后被封面取代"的另一半根因)。 */
    try { if (uiWallpaper === 'default') applyWallpaperPref(); } catch (_) {}
    /* 音频律动 body 初值（beatTick 据此门控封面呼吸） */
    document.body.classList.toggle('beat-on', uiBeat);
    /* 后端偏好为准（皮肤/高亮/背景） */
    fetch('/api/ui/prefs').then(r => r.json()).then(d => {
      const p = (d && d.ok && d.prefs) ? d.prefs : null;
      if (!p) return;
      if (p.theme && THEMES.find(t => t.key === p.theme)) uiThemeKey = p.theme;
      if (typeof p.accent === 'string') uiAccent = p.accent;
      if (p.bgmode === 'acrylic') uiBgMode = 'acrylic';
      if (typeof p.beat === 'boolean') uiBeat = p.beat;
      if (typeof p.wallpaper === 'string') uiWallpaper = p.wallpaper;   // R78
      /* ═══ R93：叠层效果 + 水面参数组 ═══
         ontop 为主键（'none'|'water'），water 为旧键回退 —— 与 ui-water.js 的
         _prefOn 同一优先级，保证卡片显示与实际启停同源。 */
      if (p.ontop === 'none' || p.ontop === 'water') uiOnTop = p.ontop;
      else if (typeof p.water === 'boolean') uiOnTop = p.water ? 'water' : 'none';
      if (p.waterOpts && typeof p.waterOpts === 'object'){
        for (const k in p.waterOpts){ if (k in uiWaterOpts) uiWaterOpts[k] = p.waterOpts[k]; }
      }
      migrateSemOpts(p.waterOpts);
      applyPrefsTheme();
      syncBgModeUI();
      syncBeatUI();
      syncOnTopUI();
      applyWaterOpts();     // ui-init 在 ui-water 之后加载 → window.__water 已就绪
      /* ⚠⚠ R57-1：成功分支也必须挂 body 类 —— 否则后端 prefs 是 acrylic 时
         亚克力层 #bgimg 永远 opacity:0（此前只在 .catch/初值挂，静默失效，
         实测 uiBgMode=acrylic 而 body.className 为空）。 */
      document.body.classList.toggle('bgmode-acrylic', uiBgMode === 'acrylic');
      document.body.classList.toggle('beat-on', uiBeat);
      /* ═══ R78：恢复背景内容偏好（默认 logo 壁纸 / 跟随封面）═══
         必须在 render() 之前做 —— 若用户上次选了「默认」壁纸，首帧就挂
         wp-active + logo，避免被随后到达的 st.cover / 轮询 render 覆盖成封面。 */
      applyWallpaperPref();
      try { if (window.Poster && window.Poster.setMode) window.Poster.setMode(uiBgMode); } catch (_) {}
      try { localStorage.setItem('r54.ui', JSON.stringify({ theme:uiThemeKey, accent:uiAccent, bgmode:uiBgMode, beat:uiBeat, wallpaper:uiWallpaper, ontop:uiOnTop, waterOpts:uiWaterOpts })); } catch (_) {}
    }).catch(() => {
      /* 后端不可用：本地镜像直接生效 */
      applyPrefsTheme();
      document.body.classList.toggle('bgmode-acrylic', uiBgMode === 'acrylic');
      document.body.classList.toggle('beat-on', uiBeat);
      applyWallpaperPref();
      syncOnTopUI();       // R93：本地镜像下的卡片点亮态 + 参数区展开态
      applyWaterOpts();
    });
    /* 背景模式 body class 初值（CSS 驱动 #bgimg 显隐） */
    document.body.classList.toggle('bgmode-acrylic', uiBgMode === 'acrylic');
    try { if (window.Poster && window.Poster.setMode) window.Poster.setMode(uiBgMode); } catch (_) {}
  })();

  /* R54/R56 验证钩子（offscreen 黑盒断言用）：
     theme()/accent()/bgmode()/setTheme(key)/setAccent(c)/setBgMode(m)
     openSettings()/closeSettings()/openTheme()/closeTheme()/isSettingsOpen()/isThemeOpen()
     searchInput(v)/suggestCount()/playFirst()/lastPlayReq()/skinCount()
     importWallpaper(type, src) / drawerOpen() */
  window.__r54 = {
    theme:    function(){ return uiThemeKey; },
    accent:   function(){ return uiAccent; },
    bgmode:   function(){ return uiBgMode; },
    locked:   function(){ return accentLocked; },
    /* ⚠ R94：换肤/换高亮是**离散终态**动作 → immediate=true 立刻落盘
       （跳过去抖，避免"点完 300ms 内 F5"丢掉这次改动；也保证黑盒面能看到 POST）。
       唯一保留去抖的是颜色取色器的 `input`（拖动中连发），它另有 change 收尾。 */
    setTheme: function(k){ uiThemeKey = k; uiAccent=''; persistPrefs({ theme:k, accent:'' }, true); applyPrefsTheme(); return true; },
    setAccent:function(c){ uiAccent = c || ''; persistPrefs({ accent:uiAccent, theme:uiThemeKey }, true); applyPrefsTheme(); return true; },
    setBgMode:function(m){ setBgMode(m); return true; },
    beatOn:   function(){ return uiBeat; },
    setBeat:  function(on){ setBeat(!!on); return uiBeat; },
    /* ═══ R93 黑盒面：叠层效果（on-top effect）═══ */
    ontop:       function(){ return uiOnTop; },
    setOnTop:    function(v){ setOnTop(v); return uiOnTop; },
    waterOpts:   function(){ return JSON.parse(JSON.stringify(uiWaterOpts)); },
    setWaterOpts:function(p){ waterOptsPatch(p || {}); return JSON.parse(JSON.stringify(uiWaterOpts)); },
    waterEnabled:function(){
      try { return !!(window.__water && window.__water.enabled && window.__water.enabled()); }
      catch (_) { return null; }
    },
    spectrum: function(a){ if (Array.isArray(a) && a.length >= 8){ window.__spectrum = a; window.__specAt = performance.now(); } return true; },
    /* ⚠ offscreen rAF 零帧 + setInterval 节流到 ~1s：beatTick/eqTick 可能
       长时间不触发 → 手动排一帧。eqTick 内部会带 beatPulse，测封面也用它。
       rAF/interval 正常时此调用幂等（beatPulse/EQ 写均 memo 化不抖动）。 */
    tickBeat: function(){ try { return window.beatPulse ? window.beatPulse() : 0; } catch (_) { return 0; } },
    tickEq:   function(){ try { if (window.eqTick){ window.eqTick(); return true; } return false; } catch (_) { return false; } },
    openSettings:  function(){ openSettings(); return $('settings-panel').classList.contains('open'); },
    closeSettings: function(){ closeSettings(); return !$('settings-panel').classList.contains('open'); },
    isSettingsOpen:function(){ return !!( $('settings-panel') && $('settings-panel').classList.contains('open') ); },
    openTheme:  function(){ openTheme(); return $('theme-panel').classList.contains('open'); },
    closeTheme: function(){ closeTheme(); return !$('theme-panel').classList.contains('open'); },
    isThemeOpen:function(){ return !!( $('theme-panel') && $('theme-panel').classList.contains('open') ); },
    drawerOpen: function(){ return document.body.classList.contains('drawer-open'); },
    importWallpaper: function(type, src){ importWallpaperSrc(type || 'image', src || ''); return true; },
    wpActive: function(){ return isCustomWallpaper(); },
    /* ═══ R78 黑盒面：默认 logo 壁纸 / 封面背景偏好（供 offscreen 断言）═══ */
    setDefaultSkin: function(){ try { applyBrandSkin(); return true; } catch (e) { return String(e); } },
    setCoverSkin:   function(){ try { useCoverSkin(); return true; } catch (e) { return String(e); } },
    wallpaperPref:  function(){ return typeof uiWallpaper === 'string' ? uiWallpaper : null; },
    bgWallpaperState: function(){
      const b = $('bgimg'); if (!b) return null;
      return { src: b.getAttribute('src'), hasBg: b.classList.contains('has-bg'),
               wp: b.classList.contains('wp-active'), bodyWp: document.body.classList.contains('wp-active') };
    },
    /* 模拟切歌：喂 st.cover 并跑 render() 的封面更新段（不依赖后端） */
    simulateCoverChange: function(url){
      st.cover = url || '/covers/fake.jpg';
      try { render(); } catch (e) { return String(e); }
      const b = $('bgimg');
      return b ? (b.getAttribute('src') || '') : '';
    },
    /* R66：原「我的壁纸」网格黑盒面（wallpapers / reloadWallpapers）随功能
       一并移除 —— 网格 DOM 与 loadMyWallpapers 都已不存在，留着会 ReferenceError。 */
    /* R61：切回「歌曲封面」背景（摘 wp-active，重新喂 st.cover） */
    useCoverSkin: function(){ useCoverSkin(); return true; },
    bgSrc: function(){ const b = $('bgimg'); return b ? (b.getAttribute('src') || '') : ''; },
    /* R57-1/2 黑盒面：壁纸层 src/class、acrylic body 态、画布透明度 */
    bgWallpaper: function(){ const b = $('bgimg'); return b ? { src: b.getAttribute('src'), hasBg: b.classList.contains('has-bg'), wp: b.classList.contains('wp-active') } : null; },
    bgWallpaperVisible: function(){
      const b = $('bgimg'); if (!b) return false;
      const cs = getComputedStyle(b);
      if ((parseFloat(cs.opacity) || 0) > 0.05) return true;
      /* offscreen/合成器暂停时 opacity .8s transition 停在起点 0 —— 按规则目标判定：
         has-bg（无 liquid-canvas 覆盖）时 CSS 目标 opacity=.9，视为可见 */
      return b.classList.contains('has-bg') && !b.classList.contains('liquid-canvas');
    },
    acrylicOn: function(){ return document.body.classList.contains('bgmode-acrylic'); },
    posterCanvasOpacity: function(){ const p = $('poster-canvas'); return p ? getComputedStyle(p).opacity : null; },
    cornerDim: function(id){ const el = $(id); return el ? el.classList.contains('dim') : null; },
    /* R59 黑盒面：默认皮肤 / acrylic 滤镜 / 口播栏常驻
       ⚠ R67-5：默认皮肤从程序化 dataURL 改为 /brand/logo.png（玻璃 logo）。
       R78：识别 logo 数据源（兼容旧 data:image/png 判定已无意义）。 */
    bgHasDefaultSkin: function(){
      const b = $('bgimg'); if (!b) return false;
      const s = b.getAttribute('src') || '';
      return !!s && (s.indexOf('data:image/png') === 0
                     || s === '/brand/logo.png');
    },
    bgFilter: function(){
      const b = $('bgimg'); if (!b) return '';
      try { return (getComputedStyle(b).filter || ''); } catch (_) { return ''; }
    },
    aibarShow: function(){
      const b = $('aibar'); if (!b) return false;
      return b.classList.contains('show') && !b.classList.contains('hide');
    },
    aiText: function(){
      const t = $('ai-text'); return t ? String(t.textContent || '') : '';
    },
    skinCount:function(){ return THEMES.length; },
    themeOf:  function(k){ const t = THEMES.find(x=>x.key===k); return t ? t.name : ''; },
    /* 搜索 */
    searchFocus: function(){ const i = $('search-input'); if (i){ i.focus(); return true; } return false; },
    searchType:  function(v){ const i = $('search-input'); if (!i) return false; i.value = v; i.dispatchEvent(new Event('input', { bubbles:true })); return true; },
    suggestItems:function(){ return Array.prototype.map.call(
      document.querySelectorAll('#search-suggest .ss-item'),
      el => el.textContent || ''); },
    searchVisible:function(){ return $('search-box').classList.contains('visible'); },
    clickSug: function(i){
      const el = document.querySelectorAll('#search-suggest .ss-item')[i];
      if (!el) return false;
      el.dispatchEvent(new MouseEvent('mousedown', { bubbles:true, cancelable:true }));
      return true;
    },
  };

  computeFramePath();
  window.addEventListener('resize', () => { computeFramePath(); updateFrameProgress(); });
  poll();
  prefetchAllViews();                // T19：启动即并行预取三栏目（成功自显，失败不显）
  /* ⚠ 2026-11-17 纠正：这里是**唯一**的显式调用已不可靠 —— loadView() 是异步 fetch，
     此刻 cardItems 仍为空/上一栏目，副标题会显示错数字（用户实测"曲库 84 首显示 3 首"）。
     真实数字由 buildCards() 末尾的 syncColumnHead() 在数据落地后刷新；
     这里保留一次只为让 kicker/title 立即可见（不等网络）。 */
  syncColumnHead();
  /* R49-30：初始显隐 —— 未播放时空闲信息层恒显；播放中（恢复会话）隐藏 */
  try { syncColumnByPlaying(!!st.playing); } catch (_) {}
  /* R49-31：漫游滑块初始显隐（默认栏目 wander 时显示） */
  try { syncRecMode(); } catch (_) {}
  loadBroadcast();                   // AI 口播
  setInterval(poll, 2000);
  setInterval(loadCards, 5000);      // 推荐变动兜底刷新（信号丢失自愈）
  setInterval(loadBroadcast, 3000);  // 口播文案轮询
  render();
  /* R59：首帧即补默认皮肤 —— 不等 2s 轮询的 render() 触发（render 内部有
     st.cover 门控，但此处显式调一次保证无封面开屏不是纯黑底）。 */
  try { ensureDefaultSkin(); } catch (_) {}
  /* ═══ R59：AI 伴听开启 → 口播栏启动即常驻 ═══
     st.djOn 初值 true（与后端默认一致）。等待首条 broadcast 文案
     （首帧 loadBroadcast + 3s 轮询）到达自动显示；若后端已关伴听，
     下一次轮询 applyState 会 hideAIBar 收走。 */

  /* R64：个性化设置折叠委托（高级选项 .set-fold-head + 动态分组 h4）+ 扫码弹层关闭。
     委托挂在 document 上，动态重渲染无需重绑。 */
  try { if (typeof initSettingsFolds === 'function') initSettingsFolds(); } catch (_) {}
  /* R67-2：4 tab 切换（AI / TTS / 音源 / 通用），点切一次只看一组。tab 状态不持久化。 */
  try { if (typeof bindSettingsTabs === 'function') bindSettingsTabs(); } catch (_) {}

  /* R53 验证钩子（offscreen 黑盒断言用，最小只读面）：
     wsText()        = 模拟 WS dj_speech 推送（viaWs=true 记 lastDjAt）
     pollBroadcast() = 手动触发一次轮询回填（验证 outro 门控）
     lastDjAt()      = 读上次 WS 推送时间戳
     ⚠ R58-1 (#41) 增补：
     wsText 应用与 WS case 同款 st.djOn 门控（关闭时不得弹口播栏）；
     djSet(on) = 从后端权威 applyState 喂 dj_on（黑盒驱动开/关路径）；
     djGet()   = 读开关镜像（断言随动）。 */
  window.__r53 = {
    lastDjAt: function(){ return lastDjAt; },
    wsText:   function(t){ if (!st.djOn) return false; return setAIBarText(String(t), true); },
    pollBroadcast: function(){ return loadBroadcast(); },
    djSet:    function(on){ applyState({ dj_on: !!on }); return st.djOn; },
    djGet:    function(){ return st.djOn; },
    /* ★ R119-7 增补（offscreen 黑盒用）：
       newTrack() = 等价一次 track_changed（解锁 WS 钉住 + 亮加载圈）
       holdOn()   = 读「WS 文案钉住」状态（验证轮询是否被正确门控）
       thinkOn()  = 读圈圈是否点亮（验证"加载中"可见性） */
    newTrack: function(){ clearDjHold(); setAIWaiting(true); return true; },
    holdOn:   function(){ return djHold; },
    thinkOn:  function(){ var e = document.getElementById('ai-bar-think');
                          return !!(e && e.classList.contains('on')); }
  };

  /* ═══════════════════════════════════════════════════════════════
     主循环（rAF）—— 原 UI 把 updateFrameProgress() 放在 mainLoop 里**每帧**跑。
     ⚠ 之前缺失这一环，进度条只在 2s 轮询的 progress 事件里更新，
     表现为：进度条两秒跳一格、播放头呼吸动画完全不动
     （sin(performance.now()) 需要逐帧重写 opacity 才看得出呼吸）。
     这里补上：播放中本地插值推进 position，后端 progress 事件负责校正。
     ═══════════════════════════════════════════════════════════════ */
  let _lastTick = performance.now();
  (function mainLoop(now){
    const dt = Math.min(250, now - _lastTick);   // 卡顿/切后台时钳位，避免进度暴冲
    _lastTick = now;
    /* ★ 2026-09-18：加 `st.posLive` 闸门 —— 只有后端报过真实播放位置(>0)才本地插值。
       否则"起播装载期"（is_playing_intent 已置 playing=true，但音频尚未开始）会乐观推进，
       随后后端回 0 → 进度条跳回（用户点名的"走过一块再回去才能正常播放"）。 */
    if (st.playing && !seeking && st.duration > 0 && st.posLive){
      st.position = Math.min(st.duration, st.position + dt);
    }
    updateFrameProgress();
    updateCardProgress();                          // R47：曲目卡底部渐变进度条每帧对齐
    try { beatTick(); } catch (_) {}               // 音频律动（真实频谱驱动封面，beat-on 才动）
    /* ⚠ R59：歌词当前行**每帧**推进 —— 旧实现只在 2s 轮询的 applyState 里
       seekLyric，行高亮会滞后整 2 秒（歌词"不跟唱"）。主循环已本地插值
       st.position，每帧驱动一次即可（内部二分/线性扫到 break，开销极小）。 */
    try { if (window.Poster) window.Poster.seekLyric(st.position); } catch (_) {}
    requestAnimationFrame(mainLoop);
  })(performance.now());


/* ══════════ ESM 边界（新架构接入点） ══════════
 * 未来每个模块可逐个替换为独立 ESM，只要在此导出等价接口。
 * 现阶段导出全局句柄，供调试/验证/插件宿主读取。 */
export const legacy = {
  get st(){ return typeof st !== 'undefined' ? st : null; },
  get MODES(){ return typeof MODES !== 'undefined' ? MODES : null; },
  render: (...a) => (typeof render === 'function' ? render(...a) : undefined),
  applyState: (...a) => (typeof applyState === 'function' ? applyState(...a) : undefined),
  poll: (...a) => (typeof poll === 'function' ? poll(...a) : undefined),
  loadCards: (...a) => (typeof loadCards === 'function' ? loadCards(...a) : undefined),
  toast: (...a) => (typeof toast === 'function' ? toast(...a) : undefined),
  setPlayMode: mode => {
    if (typeof post === 'function' && typeof MODES !== 'undefined') {
      return post('/mode', 'mode=' + mode);
    }
  },
};
/* ══════════ T14 暴露共享状态给冻结核心 water ══════════
 * 水体 classic script 先于本 ESM 加载，靠全局 st.playing 门控情绪数据
 * （ui-water.js sampleAudio:3208）。本 runtime 的 st 是模块作用域，水体读不到
 * → playing 恒 false → __feat/__spectrum 全被挡（后端数据到不了水）。
 * 这里在模块末尾把 st 挂到 window，水体自由变量即可解析。 */
if (typeof st !== 'undefined' && typeof window !== 'undefined') {
  window.st = st;
}
export default legacy;
