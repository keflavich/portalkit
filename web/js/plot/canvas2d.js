// Canvas 2D plotting for tier T0 and the overlay layer of T1/T2 panes (ARCHITECTURE.md 6.9, 7.1).
//
//   prepareCanvas(canvas, cssW, cssH, dpr) -> ctx (transform set so drawing is in CSS px)
//   drawAxes(ctx, view, opts)               background, grid, frame, ticks, titles
//   drawPoints(ctx, view, pts, opts?)       per-point shape / hollow / size / limit arrows /
//                                           error bars / labels
//   drawFocusRing(ctx, view, x, y, opts?)   focus / hover ring
//   drawDensityGrid(ctx, view, grid, opts?) tier F / pre-binned density: densityImage(grid,
//                                           opts) colourises once per (counts, cmap, lo, hi,
//                                           log) into a CPU-backed scratch canvas, then each
//                                           redraw is one drawImage (IMPLEMENTATION_NOTES 103)
//   toSVG(view, layers) -> string           SVG export; replays the same drawing code into an
//                                           SVG-emitting context, so export == screen
//   colormaps: sampleCmap(name, t), cmapTable(name), cmapLUT(names), CMAP_NAMES, cmapCSS()
//
// All coordinates handed to these functions are data coordinates of the view (plot/axes.js
// makeView); the view's toPx is the only data->pixel transform used.

import {sampleColormap, rgbToOklab, oklabToRgb} from '../vendor/gc/gl/colormaps.js';
import {COLORMAPS as VENDOR_CMAPS} from '../vendor/gc/config/style.js';
import {drawOverlays} from './overlays.js';

// ------------------------------------------------------------------ colormaps

const CMAP_POINTS = {
  viridis: ['#440154', '#482878', '#3e4989', '#31688e', '#26828e', '#1f9e89', '#35b779', '#6ece58', '#b5de2b', '#fde725'],
  magma: ['#000004', '#180f3d', '#440f76', '#721f81', '#9e2f7f', '#cd4071', '#f1605d', '#fd9668', '#feca8d', '#fcfdbf'],
  inferno: ['#000004', '#1b0c41', '#4a0c6b', '#781c6d', '#a52c60', '#cf4446', '#ed6925', '#fb9b06', '#f7d13d', '#fcffa4'],
  plasma: ['#0d0887', '#46039f', '#7201a8', '#9c179e', '#bd3786', '#d8576b', '#ed7953', '#fb9f3a', '#fdca26', '#f0f921'],
  cividis: ['#00224e', '#123570', '#3b496c', '#575d6d', '#707173', '#8a8678', '#a59c74', '#c3b369', '#e1cc55', '#fee838'],
  turbo: ['#30123b', '#4145ab', '#4675ed', '#39a2fc', '#1bcfd4', '#24eca6', '#61fc6c', '#a4fc3b', '#d1e834', '#f3c63a', '#fe9b2d', '#f36315', '#d93806', '#b11901', '#7a0402'],
  coolwarm: ['#3b4cc0', '#688aef', '#99baff', '#c9d8ef', '#edd1c2', '#f7a789', '#e26952', '#b40426'],
  rdbu: ['#053061', '#2166ac', '#4393c3', '#92c5de', '#d1e5f0', '#f7f7f7', '#fddbc7', '#f4a582', '#d6604d', '#b2182b', '#67001f'],
  gray: ['#000000', '#ffffff'],
  grey: ['#000000', '#ffffff'],
  greys: ['#ffffff', '#000000'],
};
for (const k of Object.keys(VENDOR_CMAPS)) if (!CMAP_POINTS[k]) CMAP_POINTS[k] = VENDOR_CMAPS[k];

/** Colormap names known to sampleCmap / the LUT (a '_r' suffix reverses any of them). */
export const CMAP_NAMES = Object.freeze(Object.keys(CMAP_POINTS));

function hex01(h) {
  const s = h.replace('#', '');
  const v = parseInt(s.length === 3 ? s.split('').map((c) => c + c).join('') : s, 16);
  return [((v >> 16) & 255) / 255, ((v >> 8) & 255) / 255, (v & 255) / 255];
}

const labCache = new Map();
const tableCache = new Map();

function baseName(name) {
  let n = String(name || 'viridis').toLowerCase();
  let rev = false;
  if (n.endsWith('_r')) { rev = true; n = n.slice(0, -2); }
  if (!CMAP_POINTS[n]) n = 'viridis';
  return [n, rev];
}

/** Colour of map `name` at t in [0, 1] as [r, g, b] in 0..1 (OKLab interpolation). */
export function sampleCmap(name, t) {
  const [n, rev] = baseName(name);
  if (VENDOR_CMAPS[n] && CMAP_POINTS[n] === VENDOR_CMAPS[n]) return sampleColormap(n, Number.isFinite(t) ? t : 0, rev);
  let pts = labCache.get(n);
  if (!pts) {
    pts = CMAP_POINTS[n].map((h) => rgbToOklab(hex01(h)));
    labCache.set(n, pts);
  }
  let u = Math.min(1, Math.max(0, Number.isFinite(t) ? t : 0));
  if (rev) u = 1 - u;
  u *= pts.length - 1;
  const i = Math.min(pts.length - 2, Math.floor(u));
  u -= i;
  const a = pts[i], b = pts[i + 1];
  return oklabToRgb([a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u, a[2] + (b[2] - a[2]) * u]);
}

/** 256-entry RGB table (Uint8Array(768)) for fast per-point colouring. */
export function cmapTable(name) {
  const key = String(name || 'viridis').toLowerCase();
  let t = tableCache.get(key);
  if (!t) {
    t = new Uint8Array(256 * 3);
    for (let k = 0; k < 256; k++) {
      const c = sampleCmap(key, k / 255);
      t[3 * k] = Math.round(c[0] * 255);
      t[3 * k + 1] = Math.round(c[1] * 255);
      t[3 * k + 2] = Math.round(c[2] * 255);
    }
    tableCache.set(key, t);
  }
  return t;
}

/** CSS colour string of map `name` at t. */
export function cmapColor(name, t) {
  const tab = cmapTable(name);
  const k = Math.max(0, Math.min(255, Math.round((Number.isFinite(t) ? t : 0) * 255)));
  return `rgb(${tab[3 * k]},${tab[3 * k + 1]},${tab[3 * k + 2]})`;
}

/** CSS linear-gradient for colour bars. */
export function cmapCSS(name, {direction = '90deg', steps = 11} = {}) {
  const stops = [];
  for (let k = 0; k < steps; k++) stops.push(`${cmapColor(name, k / (steps - 1))} ${((100 * k) / (steps - 1)).toFixed(1)}%`);
  return `linear-gradient(${direction}, ${stops.join(', ')})`;
}

/** RGBA8 LUT texture data: one 256-texel row per name. Returns {data, width, rows, names, row(name)}. */
export function cmapLUT(names = CMAP_NAMES) {
  const list = Array.from(names);
  const data = new Uint8Array(256 * list.length * 4);
  list.forEach((n, r) => {
    const t = cmapTable(n);
    for (let k = 0; k < 256; k++) {
      const o = (r * 256 + k) * 4;
      data[o] = t[3 * k]; data[o + 1] = t[3 * k + 1]; data[o + 2] = t[3 * k + 2]; data[o + 3] = 255;
    }
  });
  const row = (name) => {
    const i = list.indexOf(String(name || '').toLowerCase());
    return i >= 0 ? i : Math.max(0, list.indexOf('viridis'));
  };
  return {data, width: 256, rows: list.length, names: list, row};
}

// ------------------------------------------------------------------ canvas setup

/** Size a canvas for CSS size × dpr and return a 2D context drawing in CSS px. */
export function prepareCanvas(canvas, cssW, cssH, dpr = 1) {
  const r = Math.max(1, Math.min(3, Number(dpr) || 1));
  const w = Math.max(1, Math.round(cssW * r)), h = Math.max(1, Math.round(cssH * r));
  if (canvas.width !== w) canvas.width = w;
  if (canvas.height !== h) canvas.height = h;
  if (canvas.style) {
    canvas.style.width = `${cssW}px`;
    canvas.style.height = `${cssH}px`;
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(r, 0, 0, r, 0, 0);
  return ctx;
}

const DEFAULT_TOKENS = {
  bg: '#ffffff', plotBg: '#ffffff', plotGrid: '#e6e9ee', plotAxis: '#4a5360', fg: '#1b1f24', fg2: '#4a5360',
  fg3: '#7a8494', sel: '#d9822b', hover: '#1f9bb6', neutral: '#4b5563', focus: '#e08a2c',
  font: 'system-ui,-apple-system,"Segoe UI",Roboto,sans-serif',
};

/** Theme tokens with defaults for anything missing (theme.tokens() output or partial object). */
export function tokensOf(t) {
  const out = {...DEFAULT_TOKENS};
  if (t) for (const k of Object.keys(DEFAULT_TOKENS)) if (t[k]) out[k] = t[k];
  if (t && t.cat) out.cat = t.cat;
  return out;
}

function clipPlot(ctx, view) {
  const p = view.plot;
  ctx.beginPath();
  ctx.rect(p.left, p.top, p.width, p.height);
  ctx.clip();
}

// ------------------------------------------------------------------ markers

export const SHAPES = Object.freeze(['circle', 'square', 'diamond', 'triangle', 'triangle-down', 'star', 'plus', 'cross', 'hexagon']);

/** Add the outline of a marker shape of radius r at (x, y) to the current path. */
export function shapePath(ctx, shape, x, y, r) {
  switch (shape) {
    case 'square':
      ctx.rect(x - r * 0.85, y - r * 0.85, r * 1.7, r * 1.7);
      break;
    case 'diamond':
      ctx.moveTo(x, y - r * 1.15); ctx.lineTo(x + r, y); ctx.lineTo(x, y + r * 1.15); ctx.lineTo(x - r, y); ctx.closePath();
      break;
    case 'triangle':
    case 'triangle-up':
      ctx.moveTo(x, y - r * 1.15); ctx.lineTo(x + r, y + r * 0.75); ctx.lineTo(x - r, y + r * 0.75); ctx.closePath();
      break;
    case 'triangle-down':
      ctx.moveTo(x, y + r * 1.15); ctx.lineTo(x + r, y - r * 0.75); ctx.lineTo(x - r, y - r * 0.75); ctx.closePath();
      break;
    case 'star': {
      for (let k = 0; k < 10; k++) {
        const a = -Math.PI / 2 + (k * Math.PI) / 5;
        const rr = k % 2 ? r * 0.5 : r * 1.2;
        const px = x + rr * Math.cos(a), py = y + rr * Math.sin(a);
        if (k === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
      }
      ctx.closePath();
      break;
    }
    case 'hexagon':
      for (let k = 0; k < 6; k++) {
        const a = (k * Math.PI) / 3;
        const px = x + r * Math.cos(a), py = y + r * Math.sin(a);
        if (k === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
      }
      ctx.closePath();
      break;
    case 'plus':
      ctx.moveTo(x - r, y); ctx.lineTo(x + r, y); ctx.moveTo(x, y - r); ctx.lineTo(x, y + r);
      break;
    case 'cross':
    case 'x':
      ctx.moveTo(x - r * 0.8, y - r * 0.8); ctx.lineTo(x + r * 0.8, y + r * 0.8);
      ctx.moveTo(x + r * 0.8, y - r * 0.8); ctx.lineTo(x - r * 0.8, y + r * 0.8);
      break;
    default:
      ctx.moveTo(x + r, y);
      ctx.arc(x, y, r, 0, 2 * Math.PI);
      ctx.closePath();
  }
}

const OPEN_SHAPES = new Set(['plus', 'cross', 'x']);

/** Arrow from (x, y) of length len in pixel direction (dx, dy) (unit), with a head. */
export function arrowPath(ctx, x, y, dx, dy, len, head = 4) {
  const ex = x + dx * len, ey = y + dy * len;
  ctx.moveTo(x, y);
  ctx.lineTo(ex, ey);
  const nx = -dy, ny = dx;
  ctx.moveTo(ex - dx * head + nx * head * 0.7, ey - dy * head + ny * head * 0.7);
  ctx.lineTo(ex, ey);
  ctx.lineTo(ex - dx * head - nx * head * 0.7, ey - dy * head - ny * head * 0.7);
}

/** Pixel direction (+1/-1) of increasing data along a scale. */
function pxSign(s) {
  return s.b >= 0 ? 1 : -1;
}

function errPair(e) {
  if (e === null || e === undefined) return null;
  if (Array.isArray(e) || ArrayBuffer.isView(e)) {
    const lo = Number(e[0]), hi = Number(e[1]);
    if (!(lo >= 0) && !(hi >= 0)) return null;
    return [lo >= 0 ? lo : 0, hi >= 0 ? hi : 0];
  }
  const v = Number(e);
  return v > 0 && Number.isFinite(v) ? [v, v] : null;
}

/**
 * Pixel ends [a, b, aEdge, bEdge] of the error bar from v - lo to v + hi along scale s. An end
 * that cannot be mapped (v - lo <= 0 on a log axis) runs to the plot edge on the side of
 * smaller data values, which depends on the axis direction (s.b > 0: smaller pixels).
 * edgeA/edgeB are the pixel limits (either order).
 */
export function errSpan(s, v, lo, hi, edgeA, edgeB) {
  const pLo = Math.min(edgeA, edgeB), pHi = Math.max(edgeA, edgeB);
  const lowSide = s.b > 0 ? pLo : pHi;      // pixel edge toward smaller data values
  const highSide = s.b > 0 ? pHi : pLo;
  let a = s.f(v - lo), b = s.f(v + hi);
  const aEdge = !Number.isFinite(a), bEdge = !Number.isFinite(b);
  if (aEdge) a = lowSide;
  if (bEdge) b = highSide;
  return [a, b, aEdge, bEdge];
}

/**
 * Draw styled points. pts: [{x, y, color?, shape?, hollow?, size?, limit?: 'u'|'l'|null,
 *   limitAxis?: 'x'|'y', ex?, ey?, label?, alpha?, stroke?, lineWidth?}]
 * size is the marker diameter in CSS px (default 6). ex/ey: number (symmetric) or [minus, plus].
 * limit 'u' = the true value is at most the plotted one (arrow toward smaller data values),
 * 'l' = at least (arrow toward larger values); limit markers default to hollow.
 * opts: {color, size, clip = true, font, labelColor, errorColor, tokens}.
 * Points with non-finite pixel positions are skipped. Returns the number drawn.
 */
export function drawPoints(ctx, view, pts, opts = {}) {
  if (!pts || !pts.length) return 0;
  const tk = tokensOf(opts.tokens);
  ctx.save();
  if (opts.clip !== false) clipPlot(ctx, view);
  const sx = view.sx, sy = view.sy, P = view.plot;
  const defColor = opts.color || tk.neutral;
  const defSize = opts.size || 6;
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  let drawn = 0;
  // error bars and limit arrows under all markers
  for (const p of pts) {
    const px = sx.f(p.x), py = sy.f(p.y);
    if (!Number.isFinite(px) || !Number.isFinite(py)) continue;
    const color = p.color || defColor;
    const ex = errPair(p.ex), ey = errPair(p.ey);
    const size = p.size > 0 ? p.size : defSize;
    if (ex || ey) {
      ctx.globalAlpha = (p.alpha ?? 1) * 0.8;
      ctx.strokeStyle = opts.errorColor || color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      const cap = Math.min(3, size / 2);
      if (ex && !(p.limit && (p.limitAxis || 'y') === 'x')) {
        const [a, b, aEdge, bEdge] = errSpan(sx, p.x, ex[0], ex[1], P.left - 10, P.right + 10);
        ctx.moveTo(a, py); ctx.lineTo(b, py);
        if (!aEdge) { ctx.moveTo(a, py - cap); ctx.lineTo(a, py + cap); }
        if (!bEdge) { ctx.moveTo(b, py - cap); ctx.lineTo(b, py + cap); }
      }
      if (ey && !(p.limit && (p.limitAxis || 'y') === 'y')) {
        const [a, b, aEdge, bEdge] = errSpan(sy, p.y, ey[0], ey[1], P.top - 10, P.bottom + 10);
        ctx.moveTo(px, a); ctx.lineTo(px, b);
        if (!aEdge) { ctx.moveTo(px - cap, a); ctx.lineTo(px + cap, a); }
        if (!bEdge) { ctx.moveTo(px - cap, b); ctx.lineTo(px + cap, b); }
      }
      ctx.stroke();
    }
    if (p.limit === 'u' || p.limit === 'l') {
      const axis = p.limitAxis === 'x' ? 'x' : 'y';
      const s = axis === 'x' ? sx : sy;
      const dir = (p.limit === 'u' ? -1 : 1) * pxSign(s);
      const len = Math.max(10, size * 2);
      ctx.globalAlpha = p.alpha ?? 1;
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.25;
      ctx.beginPath();
      if (axis === 'x') arrowPath(ctx, px, py, dir, 0, len, 4);
      else arrowPath(ctx, px, py, 0, dir, len, 4);
      ctx.stroke();
    }
  }
  for (const p of pts) {
    const px = sx.f(p.x), py = sy.f(p.y);
    if (!Number.isFinite(px) || !Number.isFinite(py)) continue;
    const color = p.color || defColor;
    const size = p.size > 0 ? p.size : defSize;
    const r = size / 2;
    const shape = p.shape || 'circle';
    const hollow = p.hollow ?? !!p.limit;
    ctx.globalAlpha = p.alpha ?? 1;
    ctx.beginPath();
    shapePath(ctx, shape, px, py, r);
    if (hollow || OPEN_SHAPES.has(shape)) {
      ctx.strokeStyle = color;
      ctx.lineWidth = p.lineWidth || 1.25;
      ctx.stroke();
    } else {
      ctx.fillStyle = color;
      ctx.fill();
      if (p.stroke) {
        ctx.strokeStyle = p.stroke;
        ctx.lineWidth = p.lineWidth || 1;
        ctx.stroke();
      }
    }
    drawn++;
  }
  // labels on top
  const font = opts.font || `11px ${tk.font}`;
  let hasLabel = false;
  for (const p of pts) if (p.label != null && p.label !== '') { hasLabel = true; break; }
  if (hasLabel) {
    ctx.font = font;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'bottom';
    for (const p of pts) {
      if (p.label == null || p.label === '') continue;
      const px = sx.f(p.x), py = sy.f(p.y);
      if (!Number.isFinite(px) || !Number.isFinite(py)) continue;
      const r = (p.size > 0 ? p.size : defSize) / 2;
      ctx.globalAlpha = p.alpha ?? 1;
      ctx.fillStyle = opts.labelColor || tk.fg2;
      ctx.fillText(String(p.label), px + r + 2, py - r);
    }
  }
  ctx.restore();
  return drawn;
}

/** Ring around a data point (focus: solid, hover: thinner). */
export function drawFocusRing(ctx, view, x, y, opts = {}) {
  const px = view.sx.f(x), py = view.sy.f(y);
  if (!Number.isFinite(px) || !Number.isFinite(py)) return false;
  const tk = tokensOf(opts.tokens);
  ctx.save();
  ctx.globalAlpha = opts.alpha ?? 1;
  ctx.strokeStyle = opts.color || (opts.hover ? tk.hover : tk.focus);
  ctx.lineWidth = opts.width || (opts.hover ? 1.5 : 2.25);
  ctx.beginPath();
  ctx.arc(px, py, opts.r || 8, 0, 2 * Math.PI);
  ctx.stroke();
  if (!opts.hover) {
    ctx.globalAlpha = (opts.alpha ?? 1) * 0.35;
    ctx.beginPath();
    ctx.arc(px, py, (opts.r || 8) + 4, 0, 2 * Math.PI);
    ctx.stroke();
  }
  ctx.restore();
  return true;
}

// ------------------------------------------------------------------ axes

function measure(ctx, s) {
  const m = ctx.measureText ? ctx.measureText(s) : null;
  return m && Number.isFinite(m.width) ? m.width : s.length * 6;
}

/**
 * Background, grid, frame, ticks, tick labels and axis titles.
 * opts: {tokens, xLabel, yLabel, grid = true, ticks = 6 (x) / yTicks, fontSize = 11, font,
 *        background = true, frame = true, title}
 */
export function drawAxes(ctx, view, opts = {}) {
  const tk = tokensOf(opts.tokens);
  const P = view.plot;
  const fs = opts.fontSize || 11;
  const font = opts.font || `${fs}px ${tk.font}`;
  ctx.save();
  if (opts.background !== false) {
    ctx.fillStyle = tk.plotBg;
    ctx.fillRect(0, 0, view.width, view.height);
  }
  const nx = opts.ticks || Math.max(2, Math.round(P.width / 90));
  const ny = opts.yTicks || Math.max(2, Math.round(P.height / 60));
  const xt = view.sx.ticks(nx);
  const yt = view.sy.ticks(ny);
  if (opts.grid !== false) {
    ctx.lineWidth = 1;
    for (const [ticks, isX] of [[xt, true], [yt, false]]) {
      for (const t of ticks) {
        const p = isX ? view.sx.f(t.v) : view.sy.f(t.v);
        if (!Number.isFinite(p)) continue;
        ctx.strokeStyle = tk.plotGrid;
        ctx.globalAlpha = t.major === false ? 0.5 : 1;
        ctx.beginPath();
        if (isX) { const q = Math.round(p) + 0.5; ctx.moveTo(q, P.top); ctx.lineTo(q, P.bottom); }
        else { const q = Math.round(p) + 0.5; ctx.moveTo(P.left, q); ctx.lineTo(P.right, q); }
        ctx.stroke();
      }
    }
    ctx.globalAlpha = 1;
  }
  if (opts.frame !== false) {
    ctx.strokeStyle = tk.plotAxis;
    ctx.globalAlpha = 0.6;
    ctx.lineWidth = 1;
    ctx.strokeRect(Math.round(P.left) + 0.5, Math.round(P.top) + 0.5, Math.round(P.width) - 1, Math.round(P.height) - 1);
    ctx.globalAlpha = 1;
  }
  ctx.font = font;
  ctx.fillStyle = tk.plotAxis;
  ctx.strokeStyle = tk.plotAxis;
  ctx.lineWidth = 1;
  // x ticks
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  let lastRight = -Infinity;
  const xs = xt.map((t) => [view.sx.f(t.v), t]).filter(([p]) => Number.isFinite(p) && p >= P.left - 0.5 && p <= P.right + 0.5)
    .sort((a, b) => a[0] - b[0]);
  for (const [p, t] of xs) {
    const len = t.major === false ? 3 : 5;
    ctx.beginPath();
    ctx.moveTo(Math.round(p) + 0.5, P.bottom);
    ctx.lineTo(Math.round(p) + 0.5, P.bottom + len);
    ctx.stroke();
    if (!t.label) continue;
    const w = measure(ctx, t.label);
    if (p - w / 2 < lastRight + 4) continue;
    ctx.fillText(t.label, p, P.bottom + 6);
    lastRight = p + w / 2;
  }
  // y ticks
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  let lastY = Infinity;
  const ys = yt.map((t) => [view.sy.f(t.v), t]).filter(([p]) => Number.isFinite(p) && p >= P.top - 0.5 && p <= P.bottom + 0.5)
    .sort((a, b) => b[0] - a[0]);
  for (const [p, t] of ys) {
    const len = t.major === false ? 3 : 5;
    ctx.beginPath();
    ctx.moveTo(P.left - len, Math.round(p) + 0.5);
    ctx.lineTo(P.left, Math.round(p) + 0.5);
    ctx.stroke();
    if (!t.label) continue;
    if (p > lastY - fs - 2) continue;
    ctx.fillText(t.label, P.left - 7, p);
    lastY = p;
  }
  // titles
  ctx.fillStyle = tk.fg2;
  if (opts.xLabel) {
    ctx.textAlign = 'center';
    ctx.textBaseline = 'bottom';
    ctx.fillText(String(opts.xLabel), (P.left + P.right) / 2, view.height - 3);
  }
  if (opts.yLabel) {
    ctx.save();
    ctx.translate(fs + 1, (P.top + P.bottom) / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(String(opts.yLabel), 0, 0);
    ctx.restore();
  }
  if (opts.title) {
    ctx.textAlign = 'left';
    ctx.textBaseline = 'top';
    ctx.fillText(String(opts.title), P.left + 4, P.top + 3);
  }
  ctx.restore();
}

// ------------------------------------------------------------------ density grid (tier F)

/**
 * Options of every 2D context this module and the raster panes create for pixel uploads
 * (IMPLEMENTATION_NOTES 103): a CPU-backed canvas, so putImageData never becomes a GPU
 * readback/finish (seconds on SwiftShader).
 */
export const SCRATCH_CTX_OPTS = Object.freeze({willReadFrequently: true});

/** Off-screen canvas of w × h (OffscreenCanvas, else a detached <canvas>), or null. */
export function makeScratchCanvas(w, h) {
  if (typeof OffscreenCanvas === 'function') return new OffscreenCanvas(w, h);
  if (typeof document !== 'undefined' && document && typeof document.createElement === 'function') {
    const c = document.createElement('canvas');
    c.width = w; c.height = h;
    return c;
  }
  return null;
}

/** CPU-backed 2D context of a scratch canvas (SCRATCH_CTX_OPTS), or null. */
export function scratchContext(canvas) {
  if (!canvas || typeof canvas.getContext !== 'function') return null;
  return canvas.getContext('2d', SCRATCH_CTX_OPTS);
}

// Colourised density images: counts array -> Map(key -> {canvas, fp}). Weakly keyed, so an
// image lives as long as its grid. A few (cmap, lo, hi, log) variants are kept per grid.
const densityCache = new WeakMap();
const DENSITY_VARIANTS = 4;
const densityStats = {builds: 0, hits: 0};

/** Counters of the density image cache ({builds, hits}); for tests and pane stats. */
export function densityCacheStats() {
  return {...densityStats};
}

// Bit views of one float64, for hashing count values of any numeric array type.
const fpF64 = new Float64Array(1);
const fpU32 = new Uint32Array(fpF64.buffer);

/**
 * Max of the finite counts and a fingerprint of the array contents: two independent 32-bit
 * hashes (FNV-1a and a multiply-xorshift mix) over the bits of every value in order, so
 * a grid whose counts array is refilled in place gets a new image. Any change of value or
 * position changes the hashes except for a 2^-64-scale chance collision.
 */
function scanCounts(counts, len) {
  let mx = 0;
  let h1 = 0x811c9dc5 | 0, h2 = 0x9e3779b9 | 0;
  const w32 = counts instanceof Float32Array || counts instanceof Int32Array || counts instanceof Uint32Array;
  if (w32) {
    // 32-bit elements: hash their bits through a Uint32Array view (one load per value)
    const bits = new Uint32Array(counts.buffer, counts.byteOffset, len);
    for (let i = 0; i < len; i++) {
      const c = counts[i];
      if (c > mx && Number.isFinite(c)) mx = c;
      const b = bits[i];
      h1 = Math.imul(h1 ^ b, 0x01000193);
      h2 = Math.imul(h2 ^ b ^ (h2 >>> 15), 0x85ebca6b);
    }
  } else {
    for (let i = 0; i < len; i++) {
      const c = counts[i];
      if (c > mx && Number.isFinite(c)) mx = c;
      fpF64[0] = c;
      const lo = fpU32[0], hi = fpU32[1];
      h1 = Math.imul(h1 ^ lo, 0x01000193);
      h1 = Math.imul(h1 ^ hi, 0x01000193);
      h2 = Math.imul(h2 ^ lo ^ (h2 >>> 15), 0x85ebca6b);
      h2 = Math.imul(h2 ^ hi ^ (h2 >>> 13), 0xc2b2ae35);
    }
  }
  return {mx, fp: `${len}|${h1 >>> 0}|${h2 >>> 0}`};
}

/**
 * Colourised image (a scratch canvas, nx × ny, row 0 at the top) of a count grid for the
 * given cmap / lo / hi / log. The image is cached per (grid.counts, cmap, lo, hi, log), so
 * a redraw of an unchanged grid costs one drawImage and no pixel upload
 * (IMPLEMENTATION_NOTES 103). Returns null when the grid is empty or no canvas exists.
 * opts: {cmap = tokens.densityCmap | 'magma', log = true, tokens, lo = 0, hi = max count}.
 */
export function densityImage(grid, opts = {}) {
  if (!grid || !grid.counts || !(grid.nx > 0) || !(grid.ny > 0)) return null;
  const {counts} = grid;
  const nx = grid.nx | 0, ny = grid.ny | 0;
  const len = Math.min(nx * ny, counts.length >>> 0);
  const {mx, fp} = scanCounts(counts, len);
  if (!(mx > 0)) return null;
  const lo = Number.isFinite(opts.lo) ? opts.lo : 0;
  const hi = Number.isFinite(opts.hi) ? opts.hi : mx;
  const log = opts.log !== false;
  const cname = String(opts.cmap || (opts.tokens && opts.tokens.densityCmap) || 'magma').toLowerCase();
  const key = `${cname}\u0000${lo}\u0000${hi}\u0000${log ? 1 : 0}\u0000${nx}x${ny}`;
  const cacheable = typeof counts === 'object' && counts !== null;
  let byKey = cacheable ? densityCache.get(counts) : null;
  const hit = byKey ? byKey.get(key) : null;
  if (hit && hit.fp === fp) {
    densityStats.hits++;
    byKey.delete(key);       // keep LRU order
    byKey.set(key, hit);
    return hit.canvas;
  }
  const sc = hit && hit.canvas.width === nx && hit.canvas.height === ny ? hit.canvas : makeScratchCanvas(nx, ny);
  const sctx = scratchContext(sc);
  if (!sctx) return null;
  const tab = cmapTable(cname);
  const img = sctx.createImageData(nx, ny);
  const d = img.data;
  const den = log ? Math.log1p(Math.max(0, hi - lo)) : Math.max(1e-30, hi - lo);
  for (let iy = 0; iy < ny; iy++) {
    const row = ny - 1 - iy;     // image rows run top-down
    for (let ix = 0; ix < nx; ix++) {
      const k0 = iy * nx + ix;
      const c = k0 < len ? counts[k0] : NaN;
      const o = (row * nx + ix) * 4;
      if (!(c > lo) || !Number.isFinite(c)) { d[o + 3] = 0; continue; }
      let t = log ? Math.log1p(c - lo) / den : (c - lo) / den;
      t = Number.isFinite(t) ? Math.max(0, Math.min(1, t)) : 1;
      const k = Math.round(t * 255);
      d[o] = tab[3 * k]; d[o + 1] = tab[3 * k + 1]; d[o + 2] = tab[3 * k + 2];
      d[o + 3] = Math.round(255 * Math.min(1, 0.35 + 0.65 * t));
    }
  }
  sctx.putImageData(img, 0, 0);
  densityStats.builds++;
  if (cacheable) {
    if (!byKey) {
      byKey = new Map();
      densityCache.set(counts, byKey);
    }
    byKey.delete(key);
    byKey.set(key, {canvas: sc, fp});
    while (byKey.size > DENSITY_VARIANTS) byKey.delete(byKey.keys().next().value);
  }
  return sc;
}

/**
 * Draw a binned count grid. grid: {counts, nx, ny, xr: [x0, x1], yr: [y0, y1]} with
 * counts[iy·nx + ix], iy = 0 at yr[0]; bins are uniform in the view's transformed space.
 * opts: {cmap = tokens.densityCmap | 'magma', log = true, alpha = 1, tokens, lo, hi}.
 * The colourised image comes from densityImage (cached per counts / cmap / lo / hi / log).
 * Returns false when nothing can be drawn (no canvas, empty grid).
 */
export function drawDensityGrid(ctx, view, grid, opts = {}) {
  if (!grid || !grid.counts || !(grid.nx > 0) || !(grid.ny > 0)) return false;
  if (!Array.isArray(grid.xr) || !Array.isArray(grid.yr)) return false;
  const nx = grid.nx | 0, ny = grid.ny | 0;
  const [x0, x1] = grid.xr, [y0, y1] = grid.yr;
  const pxA0 = view.sx.f(x0), pxB0 = view.sx.f(x1), pyA0 = view.sy.f(y1), pyB0 = view.sy.f(y0);
  if (![pxA0, pxB0, pyA0, pyB0].every(Number.isFinite)) return false;
  const sc = densityImage(grid, opts);
  if (!sc) return false;
  const pxA = pxA0, pxB = pxB0, pyA = pyA0, pyB = pyB0;
  ctx.save();
  clipPlot(ctx, view);
  ctx.globalAlpha = opts.alpha ?? 1;
  ctx.imageSmoothingEnabled = false;
  // drawImage with a transform so inverted axes (negative extents) mirror correctly
  ctx.translate(pxA, pyA);
  ctx.scale((pxB - pxA) / nx, (pyB - pyA) / ny);
  ctx.drawImage(sc, 0, 0);
  ctx.restore();
  return true;
}

// ------------------------------------------------------------------ SVG export

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function num(v) {
  if (!Number.isFinite(v)) return '0';
  const r = Math.round(v * 100) / 100;
  return String(Object.is(r, -0) ? 0 : r);
}

function mul(m, n) {
  return [
    m[0] * n[0] + m[2] * n[1], m[1] * n[0] + m[3] * n[1],
    m[0] * n[2] + m[2] * n[3], m[1] * n[2] + m[3] * n[3],
    m[0] * n[4] + m[2] * n[5] + m[4], m[1] * n[4] + m[3] * n[5] + m[5],
  ];
}

/**
 * Minimal CanvasRenderingContext2D look-alike that records SVG. Supports the subset used by
 * this module, overlays.js, compass.js and brushui previews: paths (moveTo, lineTo, arc,
 * rect, closePath, quadraticCurveTo, bezierCurveTo), fill/stroke, fillRect/strokeRect,
 * fillText, save/restore, translate/scale/rotate/setTransform, clip, setLineDash.
 */
export class SVGContext {
  constructor(width, height) {
    this.width = width;
    this.height = height;
    this.parts = [];
    this.defs = [];
    this._clipN = 0;
    this._stack = [];
    this.fillStyle = '#000';
    this.strokeStyle = '#000';
    this.lineWidth = 1;
    this.globalAlpha = 1;
    this.font = '10px sans-serif';
    this.textAlign = 'start';
    this.textBaseline = 'alphabetic';
    this.lineCap = 'butt';
    this.lineJoin = 'miter';
    this.imageSmoothingEnabled = true;
    this._dash = [];
    this._m = [1, 0, 0, 1, 0, 0];
    this._clip = null;
    this._d = '';
    this._cur = null;
    this._start = null;
  }

  _pt(x, y) {
    const m = this._m;
    return [m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]];
  }

  _state() {
    return {
      fillStyle: this.fillStyle, strokeStyle: this.strokeStyle, lineWidth: this.lineWidth,
      globalAlpha: this.globalAlpha, font: this.font, textAlign: this.textAlign,
      textBaseline: this.textBaseline, lineCap: this.lineCap, lineJoin: this.lineJoin,
      _dash: this._dash.slice(), _m: this._m.slice(), _clip: this._clip,
    };
  }

  save() { this._stack.push(this._state()); }

  restore() {
    const s = this._stack.pop();
    if (s) Object.assign(this, s);
  }

  setTransform(a, b, c, d, e, f) {
    this._m = typeof a === 'object' && a ? [a.a, a.b, a.c, a.d, a.e, a.f] : [a, b, c, d, e, f];
  }

  resetTransform() { this._m = [1, 0, 0, 1, 0, 0]; }
  transform(a, b, c, d, e, f) { this._m = mul(this._m, [a, b, c, d, e, f]); }
  translate(x, y) { this._m = mul(this._m, [1, 0, 0, 1, x, y]); }
  scale(x, y) { this._m = mul(this._m, [x, 0, 0, y, 0, 0]); }
  rotate(a) { const c = Math.cos(a), s = Math.sin(a); this._m = mul(this._m, [c, s, -s, c, 0, 0]); }
  setLineDash(d) { this._dash = Array.isArray(d) ? d.slice() : []; }
  getLineDash() { return this._dash.slice(); }

  measureText(s) {
    const m = /([\d.]+)px/.exec(this.font);
    const fs = m ? Number(m[1]) : 10;
    return {width: String(s).length * fs * 0.56};
  }

  beginPath() { this._d = ''; this._cur = null; this._start = null; }

  moveTo(x, y) {
    const [X, Y] = this._pt(x, y);
    this._d += `M${num(X)} ${num(Y)}`;
    this._cur = [X, Y];
    this._start = [X, Y];
  }

  lineTo(x, y) {
    const [X, Y] = this._pt(x, y);
    this._d += this._cur ? `L${num(X)} ${num(Y)}` : `M${num(X)} ${num(Y)}`;
    if (!this._cur) this._start = [X, Y];
    this._cur = [X, Y];
  }

  closePath() {
    if (this._cur) {
      this._d += 'Z';
      this._cur = this._start;
    }
  }

  rect(x, y, w, h) {
    this.moveTo(x, y); this.lineTo(x + w, y); this.lineTo(x + w, y + h); this.lineTo(x, y + h); this.closePath();
  }

  quadraticCurveTo(cx, cy, x, y) {
    const [CX, CY] = this._pt(cx, cy), [X, Y] = this._pt(x, y);
    this._d += `Q${num(CX)} ${num(CY)} ${num(X)} ${num(Y)}`;
    this._cur = [X, Y];
  }

  bezierCurveTo(c1x, c1y, c2x, c2y, x, y) {
    const a = this._pt(c1x, c1y), b = this._pt(c2x, c2y), e = this._pt(x, y);
    this._d += `C${num(a[0])} ${num(a[1])} ${num(b[0])} ${num(b[1])} ${num(e[0])} ${num(e[1])}`;
    this._cur = e;
  }

  arc(x, y, r, a0, a1, ccw = false) {
    let span = a1 - a0;
    if (ccw) span = -(((a0 - a1) % (2 * Math.PI)) + (a0 - a1 < 0 ? 2 * Math.PI : 0)) || (a0 === a1 ? 0 : -2 * Math.PI);
    if (!ccw && span > 2 * Math.PI) span = 2 * Math.PI;
    const n = Math.max(2, Math.ceil(Math.abs(span) / (Math.PI / 24)));
    for (let k = 0; k <= n; k++) {
      const a = a0 + (span * k) / n;
      const px = x + r * Math.cos(a), py = y + r * Math.sin(a);
      if (k === 0 && this._cur) this.lineTo(px, py);
      else if (k === 0) this.moveTo(px, py);
      else this.lineTo(px, py);
    }
  }

  ellipse(x, y, rx, ry, rot, a0, a1) {
    const n = Math.max(8, Math.ceil(Math.abs(a1 - a0) / (Math.PI / 24)));
    const c = Math.cos(rot), s = Math.sin(rot);
    for (let k = 0; k <= n; k++) {
      const a = a0 + ((a1 - a0) * k) / n;
      const ex = rx * Math.cos(a), ey = ry * Math.sin(a);
      const px = x + ex * c - ey * s, py = y + ex * s + ey * c;
      if (k === 0 && !this._cur) this.moveTo(px, py); else this.lineTo(px, py);
    }
  }

  _common(extra) {
    let s = '';
    if (this.globalAlpha < 1) s += ` opacity="${num(this.globalAlpha)}"`;
    if (this._clip) s += ` clip-path="url(#${this._clip})"`;
    return s + extra;
  }

  _scaleW() {
    const m = this._m;
    return Math.sqrt(Math.abs(m[0] * m[3] - m[1] * m[2])) || 1;
  }

  fill() {
    if (!this._d) return;
    this.parts.push(`<path d="${this._d}" fill="${esc(this.fillStyle)}" stroke="none"${this._common('')}/>`);
  }

  stroke() {
    if (!this._d) return;
    const dash = this._dash.length ? ` stroke-dasharray="${this._dash.map(num).join(' ')}"` : '';
    this.parts.push(`<path d="${this._d}" fill="none" stroke="${esc(this.strokeStyle)}" stroke-width="${num(this.lineWidth * this._scaleW())}" stroke-linecap="${this.lineCap}" stroke-linejoin="${this.lineJoin}"${dash}${this._common('')}/>`);
  }

  fillRect(x, y, w, h) {
    const d = this._d, cur = this._cur, st = this._start;
    this.beginPath(); this.rect(x, y, w, h); this.fill();
    this._d = d; this._cur = cur; this._start = st;
  }

  strokeRect(x, y, w, h) {
    const d = this._d, cur = this._cur, st = this._start;
    this.beginPath(); this.rect(x, y, w, h); this.stroke();
    this._d = d; this._cur = cur; this._start = st;
  }

  clearRect() { /* SVG export starts from an explicit background */ }

  clip() {
    if (!this._d) return;
    const id = `pkclip${++this._clipN}`;
    const parent = this._clip ? ` clip-path="url(#${this._clip})"` : '';
    this.defs.push(`<clipPath id="${id}"><path d="${this._d}"${parent}/></clipPath>`);
    this._clip = id;
  }

  fillText(text, x, y) {
    const m = /([\d.]+)px\s+(.*)$/.exec(this.font);
    const fs = m ? Number(m[1]) : 10;
    const fam = m ? m[2] : 'sans-serif';
    const anchor = {center: 'middle', right: 'end', end: 'end'}[this.textAlign] || 'start';
    const base = {middle: 'central', top: 'hanging', hanging: 'hanging', bottom: 'text-after-edge', ideographic: 'ideographic'}[this.textBaseline] || 'alphabetic';
    const M = this._m;
    const tr = (M[1] !== 0 || M[2] !== 0 || M[0] !== M[3])
      ? ` transform="matrix(${M.map(num).join(' ')})" x="${num(x)}" y="${num(y)}"`
      : ` x="${num(M[0] * x + M[4])}" y="${num(M[3] * y + M[5])}"`;
    const size = M[1] !== 0 || M[2] !== 0 || M[0] !== M[3] ? fs : fs * Math.abs(M[0]);
    this.parts.push(`<text${tr} font-size="${num(size)}" font-family="${esc(fam)}" text-anchor="${anchor}" dominant-baseline="${base}" fill="${esc(this.fillStyle)}"${this._common('')}>${esc(text)}</text>`);
  }

  strokeText() { /* not used in exports */ }
  drawImage() { /* raster layers are not embedded in vector exports */ }
  createImageData(w, h) { return {width: w, height: h, data: new Uint8ClampedArray(w * h * 4)}; }
  putImageData() {}

  toString() {
    const defs = this.defs.length ? `<defs>${this.defs.join('')}</defs>` : '';
    return `<svg xmlns="http://www.w3.org/2000/svg" width="${num(this.width)}" height="${num(this.height)}" viewBox="0 0 ${num(this.width)} ${num(this.height)}">${defs}${this.parts.join('')}</svg>`;
  }
}

/**
 * Vector export of a plot. layers (drawn in order):
 *   {kind: 'axes', ...drawAxes opts}
 *   {kind: 'points', pts, opts?}
 *   {kind: 'overlays', layers, state, opts?}     plot/overlays.js drawOverlays
 *   {kind: 'focus', x, y, opts?}
 *   {kind: 'draw', fn(ctx, view)}                 any extra canvas-API drawing
 * Unknown kinds are ignored. Returns a standalone SVG document string.
 */
export function toSVG(view, layers) {
  const ctx = new SVGContext(view.width, view.height);
  const list = Array.isArray(layers) ? layers : [];
  const hasAxes = list.some((l) => l && l.kind === 'axes');
  if (!hasAxes) {
    const tk = tokensOf(list.find((l) => l && l.tokens)?.tokens);
    ctx.fillStyle = tk.plotBg;
    ctx.fillRect(0, 0, view.width, view.height);
  }
  for (const l of list) {
    if (!l) continue;
    if (l.kind === 'axes') drawAxes(ctx, view, l);
    else if (l.kind === 'points') drawPoints(ctx, view, l.pts || [], l.opts || {});
    else if (l.kind === 'overlays') drawOverlays(ctx, view, l.layers || [], l.state || {}, l.opts || {});
    else if (l.kind === 'focus') drawFocusRing(ctx, view, l.x, l.y, l.opts || {});
    else if (l.kind === 'draw' && typeof l.fn === 'function') l.fn(ctx, view);
  }
  return ctx.toString();
}
