/* newui/js/glass.js — 液态玻璃透镜，逐位移植自 liquid_glass.js。
 * 真实现：feImage(位移图 dataURL) + feDisplacementMap(R/G)；无 feTurbulence/无 blur。
 * 位移图 = 圆角矩形 SDF 生成的边缘强/中心弱的 R/G 编码图。
 */
let supported = false;
let defs = null;
const registry = [];

export function init(){
  supported = !!(window.CSS && window.CSS.supports
                 && window.CSS.supports('backdrop-filter', 'url(#x)'));
  if (!supported) return false;
  const old = document.getElementById('lg-defs');
  if (old){ defs = old.querySelector('defs') || old; return true; }
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.id = 'lg-defs';
  svg.setAttribute('width', '0'); svg.setAttribute('height', '0');
  svg.style.position = 'absolute'; svg.style.width = svg.style.height = '0';
  svg.innerHTML = '<defs></defs>';
  document.body.appendChild(svg);
  defs = svg.querySelector('defs');
  return true;
}

function smoothstep(a, b, x){ x = Math.max(0, Math.min(1, (x-a)/(b-a))); return x*x*(3-2*x); }
function clamp128(v){ return Math.max(0, Math.min(255, v|0)); }
/* 圆角矩形 SDF（liquid_glass.js:50-54 照抄） */
function sdf(px, py, bx, by, r){
  const qx = Math.abs(px) - bx + r, qy = Math.abs(py) - by + r;
  const ax = Math.max(qx, 0), ay = Math.max(qy, 0);
  return Math.min(Math.max(qx, qy), 0) + Math.hypot(ax, ay) - r;
}

/* 位移图（liquid_glass.js:58-86 照抄） */
function buildMap(w, h, radius, thickness, base){
  w = Math.max(2, w|0); h = Math.max(2, h|0);
  const cv = document.createElement('canvas');
  cv.width = w; cv.height = h;
  const ctx = cv.getContext('2d');
  const img = ctx.createImageData(w, h), data = img.data;
  const bx = w/2, by = h/2, r = Math.min(radius, bx, by);
  base = Math.max(0, Math.min(0.9, base || 0));
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++){
    const lx = x - bx, ly = y - by;
    const dist = sdf(lx, ly, bx, by, r);
    const i = (y*w + x)*4;
    if (dist > 0){ data[i]=128; data[i+1]=128; data[i+2]=128; data[i+3]=0; continue; }
    const edgeDist = -dist;
    const t = Math.min(1, edgeDist / thickness);
    const rim = 1 - smoothstep(0, 1, t);              // :70 边缘=1 中心=0
    const len = Math.hypot(lx, ly) || 1;
    const dx = lx/len, dy = ly/len;
    const mag = base + (1 - base) * rim;              // :74
    data[i]   = clamp128(128 - dx*mag*127);           // :76 R
    data[i+1] = clamp128(128 - dy*mag*127);           // :77 G
    data[i+2] = 128;
    data[i+3] = 255;
  }
  ctx.putImageData(img, 0, 0);
  return cv.toDataURL();
}

/* 滤镜（liquid_glass.js:91-109 照抄：只有 feImage + feDisplacementMap） */
function makeFilter(fid){
  const f = document.createElementNS('http://www.w3.org/2000/svg', 'filter');
  f.setAttribute('id', fid);
  f.setAttribute('x','0%'); f.setAttribute('y','0%');
  f.setAttribute('width','100%'); f.setAttribute('height','100%');
  f.setAttribute('color-interpolation-filters','sRGB');
  const fi = document.createElementNS('http://www.w3.org/2000/svg', 'feImage');
  fi.setAttribute('result','map');                     // :98 不设 x/y/w/h
  const disp = document.createElementNS('http://www.w3.org/2000/svg', 'feDisplacementMap');
  disp.setAttribute('in','SourceGraphic');
  disp.setAttribute('in2','map');
  disp.setAttribute('scale','1');
  disp.setAttribute('xChannelSelector','R');
  disp.setAttribute('yChannelSelector','G');
  f.appendChild(fi); f.appendChild(disp);
  defs.appendChild(f);
  return { f, fi, disp };
}

function buildMapFor(o){
  const w = Math.max(2, o.el.clientWidth|0), h = Math.max(2, o.el.clientHeight|0);
  if (w <= 2 && h <= 2) return;                        // :116 display:none 跳过
  const url = buildMap(w, h, o.radius, o.thickness, o.base);
  const set = () => {
    o.fi.setAttribute('href', url);
    o.fi.setAttributeNS('http://www.w3.org/1999/xlink','href', url);
  };
  set(); setTimeout(set, 80); setTimeout(set, 300);    // :121-123 Chromium 兜底
}

/* 挂载（liquid_glass.js:126-150 照抄；blur 形参不存在） */
export function applyGlass(el, radius, thickness, scale, base = 0){
  if (!supported || !defs || !el) return false;
  const fid = 'lg-' + (el.id || (el.id = 'lg' + Math.random().toString(36).slice(2)));
  let f = document.getElementById(fid), fi, disp;
  if (!f){ const p = makeFilter(fid); fi = p.fi; disp = p.disp; }
  else { fi = f.querySelector('feImage'); disp = f.querySelector('feDisplacementMap'); }
  disp.setAttribute('scale', String(scale));           // :138 位移强度
  const vf = 'url(#' + fid + ')';
  el.style.backdropFilter = vf;
  el.style.webkitBackdropFilter = vf;
  const o = { el, fi, radius, thickness, scale, base: base||0 };
  registry.push(o);
  buildMapFor(o);
  if (typeof ResizeObserver !== 'undefined'){
    o.ro = new ResizeObserver(() => buildMapFor(o));
    o.ro.observe(el);                                  // :146-149
  }
  return true;
}

/* 固定配置表（liquid_glass.js:152-175 照抄，选择器映射到新 UI） */
const TARGETS = [
  ['#view-player', 44, 26, 55, 0],       // ≈ #dock
  ['.search-box',  23, 14, 30, 0],
  ['.aibar',       20, 14, 38, 0],
  ['.entry',       20, 18, 40, 0],
  ['.accent-dot',  27, 14, 80, 0.12],
];
export function run(){
  if (!init()) return 0;
  let n = 0;
  TARGETS.forEach(([sel, radius, thickness, scale, base]) => {
    document.querySelectorAll(sel).forEach(el => {
      if (applyGlass(el, radius, thickness, scale, base)) n++;
    });
  });
  return n;
}

export const _diag = { supported: () => supported, count: () => registry.length };
