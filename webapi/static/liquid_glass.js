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
