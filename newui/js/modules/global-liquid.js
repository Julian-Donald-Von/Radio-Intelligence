/* newui/js/modules/global-liquid.js — 由老 liquid.js 拆出的独立模块（自动生成，勿手改）。
 * factory(ctx) 用 new Function 承载老代码，保持 classic script 的顶层声明语义；
 * 本模块可独立 register/unregister，替换时只需提供等价 factory。
 */
export default {
  name: 'global-liquid',
  deps: ['ctx'],
  factory: async function (ctx) {
    const __src = `/* ══════════════════════════════════════════════════════════════
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
  ].join("\\n");

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
     历史遗留：setSource 的 onload 里顺手 \`img.src = url; has-bg\` —— 这在老
     架构（bgimg 即壁纸位）成立；R57-1 后 #bgimg 由封面/皮肤逻辑（ui-core
     render / applyBrandSkin / ensureDefaultSkin）统一接管，这里再写就**越权**：
       · ui-view.initLiquidBg 启动时喂程序化渐变 dataURL → 抢先占住 bgimg.src，
         使 ensureDefaultSkin 的 \`if (!img.getAttribute('src'))\` 永远不触发
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
`;
    /* preamble：ctx 既是共享对象，也让老代码里的裸标识符可解析。
       new Function 非严格模式，with 可用 —— 这是"老代码零改动"的关键。 */
    const __run = new Function('ctx', 'with (ctx) {\n' + __src + '\n' + "  /* \u628a\u672c\u6a21\u5757\u9876\u5c42\u58f0\u660e\u53d1\u5e03\u5230 ctx\uff08\u4f9b\u540e\u7eed\u6a21\u5757 use\uff09 */\n  const __defs = [\"canvas\", \"_ready\", \"_imgW\", \"VERT\", \"FRAG\", \"initGL\", \"resize\", \"draw\", \"tick\", \"apply_\"];\n  for (const __n of __defs) {\n    try { if (typeof eval(__n) !== 'undefined') ctx.__publish(__n, eval(__n)); } catch (_e) {}\n  }" + '\n}');
    return __run(ctx);
  },
};
