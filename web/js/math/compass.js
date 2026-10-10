// Manual-tour compass on a Canvas 2D context (ARCHITECTURE.md 6.9). Port of the legacy GC
// SVG compass (gc_fields_manifold_assets/js/app/compass.js): one handle per dim on a unit
// disk; a dim fully in the projection plane sits on the ring. Dims whose projection length is
// <= SHOW_W are parked as small dots on an arc above the disk.
//
//   const layout = drawCompass(ctx2d, F, dims, opts)      draws and returns the layout
//   compassLayout(F, dims, opts)                          same geometry without drawing
//   compassHit(layout, px, py)                            -> dim index | -1
//   compassTarget(layout, px, py)                         -> [a, b] for Tour.manual(j, a, b)
//   attachCompass(el, {getFrame, dims, opts, onMove, onEnd, onReset, onHover})
//
// Drawing and hit testing use the same layout object, so the hit areas are where the handles
// are drawn. Frames use the vendor layout Float64Array(2·D) = [px_0..px_{D-1}, py_0..py_{D-1}].

export const R_FRAC = 0.68;        // ring radius / half-size (legacy R = 68 of VIEW = 100)
export const PARK_FRAC = 0.92;     // park arc radius / half-size (legacy PARK_R = 92)
export const SHOW_W = 0.04;        // projection length below which a dim is parked
export const LABEL_W = 0.08;       // vendor LABEL_MIN_WEIGHT: labels for dims at least this long
const HIT_FRAC = 0.13;             // legacy hit radius 13 of 100

const DEFAULT_COLORS = ['#3182bd', '#e6550d', '#31a354', '#756bb1', '#de2d26', '#17becf', '#bcbd22', '#8c564b',
  '#e377c2', '#7f7f7f'];

/**
 * Geometry of the compass. dims: [{key, label?, short?, color?}] or key strings (the
 * `frame:<pane>` store value). opts: {size = 180, cx, cy (centre, CSS px; default size/2),
 * colors?: string[] | (i) => string, labels?: string[]}.
 * Returns {cx, cy, half, R, parkR, hitR, items: [{i, key, label, color, px, py, w, x, y, ux,
 * uy, parked, showLabel}]} with (x, y) the handle centre in CSS px.
 */
export function compassLayout(F, dims, opts = {}) {
  const list = Array.isArray(dims) ? dims : [];
  const D = list.length;
  const size = Number(opts.size) > 0 ? Number(opts.size) : 180;
  const half = size / 2;
  const cx = Number.isFinite(opts.cx) ? opts.cx : half;
  const cy = Number.isFinite(opts.cy) ? opts.cy : half;
  const R = R_FRAC * half, parkR = PARK_FRAC * half, hitR = Math.max(8, HIT_FRAC * half);
  const colorOf = typeof opts.colors === 'function' ? opts.colors
    : (i) => (Array.isArray(opts.colors) && opts.colors[i]) || (list[i] && list[i].color) || DEFAULT_COLORS[i % DEFAULT_COLORS.length];
  const items = [];
  const parked = [];
  const n2 = F && F.length >= 2 * D ? D : 0;
  for (let i = 0; i < D; i++) {
    const d = typeof list[i] === 'string' ? {key: list[i]} : (list[i] || {});   // frame:<pane> dims are strings
    const px = n2 ? Number(F[i]) || 0 : 0;
    const py = n2 ? Number(F[D + i]) || 0 : 0;
    const w = Math.hypot(px, py);
    const label = (opts.labels && opts.labels[i]) || d.short || d.label || d.key || String(i);
    const it = {i, key: d.key, label: String(label), color: colorOf(i), px, py, w, x: cx, y: cy, ux: 1, uy: 0, parked: false, showLabel: false};
    if (!(w > SHOW_W)) {
      it.parked = true;
      parked.push(it);
    } else {
      const wc = Math.min(w, 1);
      it.ux = px / w;
      it.uy = -py / w;
      it.x = cx + it.ux * wc * R;
      it.y = cy + it.uy * wc * R;
      it.showLabel = w >= LABEL_W;
    }
    items.push(it);
  }
  const n = parked.length;
  parked.forEach((it, k) => {
    const deg = 20 + (n > 1 ? (140 * k) / (n - 1) : 70);
    const a = (Math.PI / 180) * deg;
    it.x = cx + parkR * Math.cos(a);
    it.y = cy - parkR * Math.sin(a);
    it.ux = Math.cos(a);
    it.uy = -Math.sin(a);
  });
  return {cx, cy, half, R, parkR, hitR, items};
}

/** Index of the handle nearest (px, py) within the hit radius (in-plane handles win ties). */
export function compassHit(layout, px, py) {
  if (!layout || !Number.isFinite(px) || !Number.isFinite(py)) return -1;
  let best = -1, bd = Infinity;
  for (const it of layout.items) {
    // parked dots get a smaller hit radius so they don't shadow nearby in-plane handles
    const r = it.parked ? layout.hitR * 0.7 : layout.hitR;
    const d = Math.hypot(px - it.x, py - it.y);
    if (d <= r && d + (it.parked ? 2 : 0) < bd) { bd = d + (it.parked ? 2 : 0); best = it.i; }
  }
  return best;
}

/** Projection target [a, b] for a pointer at (px, py): ((x - cx)/R, -(y - cy)/R), |.| <= 1. */
export function compassTarget(layout, px, py) {
  let a = (px - layout.cx) / layout.R;
  let b = -(py - layout.cy) / layout.R;
  if (!Number.isFinite(a) || !Number.isFinite(b)) return [0, 0];
  const m = Math.hypot(a, b);
  if (m > 1) { a /= m; b /= m; }
  return [a, b];
}

/**
 * Draw the compass. opts: compassLayout opts plus {tokens: {fg2, fg3, plotGrid, bg}, active?: i,
 * hover?: i, font?, background?: bool}. Returns the layout used.
 */
export function drawCompass(ctx, F, dims, opts = {}) {
  const L = compassLayout(F, dims, opts);
  const tk = opts.tokens || {};
  const ring = tk.plotGrid || 'rgba(128,128,128,0.45)';
  const fg = tk.fg2 || '#4a5360';
  const fontPx = Math.max(9, Math.min(12, L.half * 0.12));
  const font = opts.font || `${fontPx}px system-ui,-apple-system,"Segoe UI",Roboto,sans-serif`;
  ctx.save();
  if (opts.background) {
    ctx.fillStyle = tk.bg || tk.plotBg || '#ffffff';
    ctx.globalAlpha = 0.85;
    ctx.beginPath();
    ctx.arc(L.cx, L.cy, L.half, 0, 2 * Math.PI);
    ctx.fill();
    ctx.globalAlpha = 1;
  }
  ctx.strokeStyle = ring;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.arc(L.cx, L.cy, L.R, 0, 2 * Math.PI);
  ctx.stroke();
  ctx.setLineDash([2, 3]);
  ctx.beginPath();
  ctx.moveTo(L.cx - L.R, L.cy); ctx.lineTo(L.cx + L.R, L.cy);
  ctx.moveTo(L.cx, L.cy - L.R); ctx.lineTo(L.cx, L.cy + L.R);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = fg;
  ctx.beginPath();
  ctx.arc(L.cx, L.cy, 2.5, 0, 2 * Math.PI);
  ctx.fill();
  ctx.font = font;
  // in-plane handles: shortest first so long ones draw on top
  const live = L.items.filter((it) => !it.parked).sort((p, q) => p.w - q.w);
  for (const it of live) {
    const emph = it.i === opts.active || it.i === opts.hover;
    ctx.strokeStyle = it.color;
    ctx.globalAlpha = emph ? 1 : 0.85;
    ctx.lineWidth = emph ? 2.25 : 1.5;
    ctx.beginPath();
    ctx.moveTo(L.cx, L.cy);
    ctx.lineTo(it.x, it.y);
    ctx.stroke();
    ctx.fillStyle = it.color;
    ctx.beginPath();
    ctx.arc(it.x, it.y, emph ? 6.5 : 5, 0, 2 * Math.PI);
    ctx.fill();
    if (it.showLabel || emph) {
      ctx.globalAlpha = 1;
      ctx.fillStyle = fg;
      ctx.textAlign = it.ux > 0.3 ? 'left' : it.ux < -0.3 ? 'right' : 'center';
      ctx.textBaseline = it.uy > 0.3 ? 'top' : it.uy < -0.3 ? 'bottom' : 'middle';
      ctx.fillText(it.label, it.x + it.ux * 9, it.y + it.uy * 9);
    }
  }
  for (const it of L.items) {
    if (!it.parked) continue;
    const emph = it.i === opts.active || it.i === opts.hover;
    ctx.globalAlpha = emph ? 1 : 0.6;
    ctx.fillStyle = it.color;
    ctx.beginPath();
    ctx.arc(it.x, it.y, emph ? 4.5 : 3, 0, 2 * Math.PI);
    ctx.fill();
    if (emph) {
      ctx.globalAlpha = 1;
      ctx.fillStyle = fg;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'bottom';
      ctx.fillText(it.label, it.x, it.y - 6);
    }
  }
  ctx.restore();
  return L;
}

/**
 * Pointer wiring for a compass drawn on `el` (a canvas or an element overlaying it).
 * cfg: {getFrame() -> F, dims, opts | getOpts() (same as drawCompass; size / cx / cy in the
 *       element's CSS px), onMove(j, a, b), onEnd(j), onReset(j), onHover(j | -1)}
 * Pointer -> compass px scales the bounding rect to opts.width/height when given (the CSS
 * size the compass was drawn at), so stretched canvases hit-test where they draw.
 * Returns detach().
 */
export function attachCompass(el, cfg) {
  let drag = null;   // {j, id}
  let hover = -1;
  const optsNow = () => (typeof cfg.getOpts === 'function' ? cfg.getOpts() : cfg.opts || {});
  const layoutNow = () => compassLayout(cfg.getFrame(), typeof cfg.dims === 'function' ? cfg.dims() : cfg.dims, optsNow());
  const toLocal = (e) => {
    const r = el.getBoundingClientRect();
    const o = optsNow();
    const W = Number(o.width) > 0 ? Number(o.width) : r.width;
    const H = Number(o.height) > 0 ? Number(o.height) : r.height;
    return [(e.clientX - r.left) * (r.width > 0 ? W / r.width : 1), (e.clientY - r.top) * (r.height > 0 ? H / r.height : 1)];
  };
  const onDown = (e) => {
    if (e.button !== undefined && e.button !== 0) return;
    const L = layoutNow();
    const [x, y] = toLocal(e);
    const j = compassHit(L, x, y);
    if (j < 0) return;
    drag = {j, id: e.pointerId};
    if (el.setPointerCapture && e.pointerId != null) {
      try {
        el.setPointerCapture(e.pointerId);
      } catch (err) {
        if (!(err instanceof DOMException)) throw err;
      }
    }
    // the compass owns this press: a brush on the same element must not start a drag.
    // The listener runs in the capture phase (before target-phase bubble listeners in current
    // browsers) and stops the other listeners; `pkHandled` covers listeners that still run.
    e.pkHandled = 'compass';
    if (e.preventDefault) e.preventDefault();
    if (e.stopImmediatePropagation) e.stopImmediatePropagation();
    else if (e.stopPropagation) e.stopPropagation();
  };
  const onMove = (e) => {
    const L = layoutNow();
    const [x, y] = toLocal(e);
    if (drag) {
      const [a, b] = compassTarget(L, x, y);
      if (cfg.onMove) cfg.onMove(drag.j, a, b);
      return;
    }
    const h = compassHit(L, x, y);
    if (h !== hover) {
      hover = h;
      if (el.style) el.style.cursor = h >= 0 ? 'grab' : '';
      if (cfg.onHover) cfg.onHover(h);
    }
  };
  const end = () => {
    if (!drag) return;
    const j = drag.j;
    drag = null;
    if (cfg.onEnd) cfg.onEnd(j);
  };
  const onDbl = (e) => {
    const j = compassHit(layoutNow(), ...toLocal(e));
    if (j < 0) return;
    if (e.preventDefault) e.preventDefault();
    if (cfg.onReset) cfg.onReset(j);
    else if (cfg.onMove) { cfg.onMove(j, 0, 0); if (cfg.onEnd) cfg.onEnd(j); }
  };
  const onLeave = () => {
    if (hover !== -1 && !drag) {
      hover = -1;
      if (el.style) el.style.cursor = '';
      if (cfg.onHover) cfg.onHover(-1);
    }
  };
  el.addEventListener('pointerdown', onDown, true);
  el.addEventListener('pointermove', onMove);
  el.addEventListener('pointerup', end);
  el.addEventListener('pointercancel', end);
  el.addEventListener('pointerleave', onLeave);
  el.addEventListener('dblclick', onDbl);
  return () => {
    el.removeEventListener('pointerdown', onDown, true);
    el.removeEventListener('pointermove', onMove);
    el.removeEventListener('pointerup', end);
    el.removeEventListener('pointercancel', end);
    el.removeEventListener('pointerleave', onLeave);
    el.removeEventListener('dblclick', onDbl);
  };
}
