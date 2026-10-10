// Brush interaction for scatter / manifold / hist panes (ARCHITECTURE.md 6.9).
//
//   const detach = attachBrush(el, view, {modes, mode, onPreview, onCommit(shape, op), onClick,
//                                         store, pane, enabled});
//   detach.setMode('lasso'); detach.setView(view); detach.cancel(); detach();
//
// Shapes are in data coordinates of the view:
//   {kind: 'rect', x0, y0, x1, y1}            (x0 <= x1, y0 <= y1)
//   {kind: 'circle', cx, cy, r, rx, ry}       circle drawn in screen space; rx / ry are the
//                                             semi-axes in transformed units (log10 on log
//                                             axes), r = rx
//   {kind: 'lasso', pts: [[x, y], ...]}       closed implicitly, even-odd rule
// Every shape carries xlog / ylog flags; circle and lasso tests run in the transformed
// (screen-linear) space so that the selected region is the region drawn on screen.
// The op comes from core/selection.js opFromEvent (none replace, shift add, alt subtract,
// shift+alt intersect), OR-ed over the modifiers held at pointerdown and pointerup.
//
// Pointer -> data mapping uses the element's bounding rect scaled to the view's CSS size and
// then view.toData, the inverse of the transform used to draw, so the hit area matches the
// drawing even when the canvas is stretched.

import {opFromEvent} from '../core/selection.js';

export const BRUSH_MODES = Object.freeze(['rect', 'circle', 'lasso']);
const CLICK_PX = 3;
const LASSO_STEP_PX = 2;

function getView(v) {
  return typeof v === 'function' ? v() : v;
}

/** Pointer event -> CSS px in the view's coordinate system (same transform as drawing). */
export function eventToViewPx(el, view, e) {
  const r = el.getBoundingClientRect();
  const sx = r.width > 0 ? view.width / r.width : 1;
  const sy = r.height > 0 ? view.height / r.height : 1;
  return [(e.clientX - r.left) * sx, (e.clientY - r.top) * sy];
}

function axisLog(view, a) {
  const s = a === 'x' ? view.sx : view.sy;
  return s.kind === 'log';
}

/**
 * Build a data-space shape from screen points (CSS px of the view).
 * rect: [p0, p1]; circle: [center, edge]; lasso: [p0, p1, ...]. Returns null when degenerate.
 */
export function shapeFromScreen(view, kind, pts) {
  const xlog = axisLog(view, 'x'), ylog = axisLog(view, 'y');
  const toD = (p) => view.toData(p[0], p[1]);
  if (kind === 'rect') {
    if (pts.length < 2) return null;
    const a = toD(pts[0]), b = toD(pts[pts.length - 1]);
    if (![a[0], a[1], b[0], b[1]].every(Number.isFinite)) return null;
    return {kind: 'rect', x0: Math.min(a[0], b[0]), y0: Math.min(a[1], b[1]), x1: Math.max(a[0], b[0]), y1: Math.max(a[1], b[1]), xlog, ylog};
  }
  if (kind === 'circle') {
    if (pts.length < 2) return null;
    const c = pts[0], e = pts[pts.length - 1];
    const rpx = Math.hypot(e[0] - c[0], e[1] - c[1]);
    if (!(rpx > 0)) return null;
    const cd = toD(c);
    if (!cd.every(Number.isFinite)) return null;
    const rx = Math.abs(rpx / view.sx.b);
    const ry = Math.abs(rpx / view.sy.b);
    return {kind: 'circle', cx: cd[0], cy: cd[1], r: rx, rx, ry, xlog, ylog};
  }
  if (kind === 'lasso') {
    const out = [];
    for (const p of pts) {
      const d = toD(p);
      if (d.every(Number.isFinite)) out.push([d[0], d[1]]);
    }
    if (out.length < 3) return null;
    return {kind: 'lasso', pts: out, xlog, ylog};
  }
  return null;
}

const tfx = (log) => (log ? (v) => (v > 0 ? Math.log10(v) : NaN) : (v) => v);

/**
 * Reference point test for a shape: returns (x, y) -> boolean in data coordinates.
 * workers/ops/select.js implements the same semantics for the bulk path.
 */
export function shapeTest(shape) {
  if (!shape) return () => false;
  const fx = tfx(!!shape.xlog), fy = tfx(!!shape.ylog);
  if (shape.kind === 'rect') {
    const {x0, x1, y0, y1} = shape;
    return (x, y) => x >= x0 && x <= x1 && y >= y0 && y <= y1;
  }
  if (shape.kind === 'circle') {
    const cx = fx(shape.cx), cy = fy(shape.cy);
    const rx = Number(shape.rx ?? shape.r), ry = Number(shape.ry ?? shape.r);
    if (!(rx > 0) || !(ry > 0) || !Number.isFinite(cx) || !Number.isFinite(cy)) return () => false;
    return (x, y) => {
      const u = (fx(x) - cx) / rx, v = (fy(y) - cy) / ry;
      return u * u + v * v <= 1;
    };
  }
  if (shape.kind === 'lasso') {
    const P = (shape.pts || []).map((p) => [fx(p[0]), fy(p[1])]).filter((p) => Number.isFinite(p[0]) && Number.isFinite(p[1]));
    if (P.length < 3) return () => false;
    let xmin = Infinity, xmax = -Infinity, ymin = Infinity, ymax = -Infinity;
    for (const [x, y] of P) {
      if (x < xmin) xmin = x; if (x > xmax) xmax = x;
      if (y < ymin) ymin = y; if (y > ymax) ymax = y;
    }
    const n = P.length;
    return (x0, y0) => {
      const x = fx(x0), y = fy(y0);
      if (!(x >= xmin && x <= xmax && y >= ymin && y <= ymax)) return false;
      let inside = false;
      for (let i = 0, j = n - 1; i < n; j = i++) {
        const xi = P[i][0], yi = P[i][1], xj = P[j][0], yj = P[j][1];
        if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
      }
      return inside;
    };
  }
  return () => false;
}

/**
 * Hit mask over arrays: out[i] = 1 when (xs[i], ys[i]) is inside and drawable[i] (if given)
 * is non-zero. Returns Uint8Array(n).
 */
export function hitTest(shape, xs, ys, drawable = null) {
  const n = Math.min(xs ? xs.length : 0, ys ? ys.length : 0);
  const out = new Uint8Array(n);
  const t = shapeTest(shape);
  for (let i = 0; i < n; i++) {
    if (drawable && !drawable[i]) continue;
    const x = xs[i], y = ys[i];
    if (Number.isFinite(x) && Number.isFinite(y) && t(x, y)) out[i] = 1;
  }
  return out;
}

/**
 * Draw an in-progress brush. preview: {kind, screen: [[px, py], ...]} (screen points as kept by
 * attachBrush) or a data-space shape. tokens: theme tokens ({sel}).
 */
export function drawBrushPreview(ctx, view, preview, tokens = {}) {
  if (!preview) return;
  const color = tokens.sel || '#d9822b';
  ctx.save();
  ctx.strokeStyle = color;
  ctx.fillStyle = color;
  ctx.lineWidth = 1.25;
  ctx.setLineDash([4, 3]);
  ctx.beginPath();
  const kind = preview.kind;
  if (preview.screen) {
    const S = preview.screen;
    if (kind === 'rect' && S.length >= 2) {
      const a = S[0], b = S[S.length - 1];
      ctx.rect(Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.abs(b[0] - a[0]), Math.abs(b[1] - a[1]));
    } else if (kind === 'circle' && S.length >= 2) {
      const r = Math.hypot(S[S.length - 1][0] - S[0][0], S[S.length - 1][1] - S[0][1]);
      ctx.moveTo(S[0][0] + r, S[0][1]);
      ctx.arc(S[0][0], S[0][1], r, 0, 2 * Math.PI);
    } else if (kind === 'lasso' && S.length >= 2) {
      ctx.moveTo(S[0][0], S[0][1]);
      for (let i = 1; i < S.length; i++) ctx.lineTo(S[i][0], S[i][1]);
      ctx.closePath();
    }
  } else if (kind === 'rect') {
    const a = view.toPx(preview.x0, preview.y0), b = view.toPx(preview.x1, preview.y1);
    if ([...a, ...b].every(Number.isFinite)) ctx.rect(Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.abs(b[0] - a[0]), Math.abs(b[1] - a[1]));
  } else if (kind === 'circle') {
    const c = view.toPx(preview.cx, preview.cy);
    const rx = Math.abs((preview.rx ?? preview.r) * view.sx.b);
    const ry = Math.abs((preview.ry ?? preview.r) * view.sy.b);
    if (c.every(Number.isFinite) && rx > 0 && ry > 0) {
      for (let k = 0; k <= 64; k++) {
        const a = (2 * Math.PI * k) / 64;
        const x = c[0] + rx * Math.cos(a), y = c[1] + ry * Math.sin(a);
        if (k === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
    }
  } else if (kind === 'lasso' && Array.isArray(preview.pts)) {
    let first = true;
    for (const p of preview.pts) {
      const q = view.toPx(p[0], p[1]);
      if (!q.every(Number.isFinite)) continue;
      if (first) { ctx.moveTo(q[0], q[1]); first = false; } else ctx.lineTo(q[0], q[1]);
    }
    ctx.closePath();
  }
  ctx.globalAlpha = 0.12;
  ctx.fill();
  ctx.globalAlpha = 1;
  ctx.stroke();
  ctx.restore();
}

/**
 * Attach brushing to `el`. view: a view object or a function returning the current view.
 * opts:
 *   modes        allowed modes (default all three); mode: initial mode (default modes[0])
 *   onPreview(p) p = {kind, screen, shape, op} while dragging, null when the brush ends
 *   onCommit(shape, op)
 *   onClick(px, py, event)   click without drag (< 3 px), e.g. picking
 *   store, pane  optional: emits 'brush:preview' {pane, shape} and handles 'keys:escape'
 *   enabled()    optional predicate; when false, pointerdown is ignored (e.g. pan mode)
 *   button       mouse button that brushes (default 0)
 * Returns detach(); detach.setMode(m), detach.mode(), detach.setView(v), detach.cancel(),
 * detach.active().
 */
export function attachBrush(el, view, opts = {}) {
  const modes = (Array.isArray(opts.modes) && opts.modes.length ? opts.modes : BRUSH_MODES).filter((m) => BRUSH_MODES.includes(m));
  let mode = modes.includes(opts.mode) ? opts.mode : modes[0] || 'rect';
  let viewRef = view;
  let drag = null;   // {id, kind, screen: [[x, y]], down: event modifiers, moved}
  const prevTouch = el.style ? el.style.touchAction : '';
  if (el.style) el.style.touchAction = 'none';
  const store = opts.store || null;
  const button = opts.button ?? 0;

  const emitPreview = (p) => {
    if (opts.onPreview) opts.onPreview(p);
    if (store && opts.pane != null) store.emit('brush:preview', {pane: opts.pane, shape: p ? p.shape : null});
  };

  const cancel = () => {
    if (!drag) return false;
    const id = drag.id;
    drag = null;
    if (el.releasePointerCapture && id != null) {
      try {
        el.releasePointerCapture(id);
      } catch (err) {
        if (!(err instanceof DOMException)) throw err;   // pointer already released
      }
    }
    emitPreview(null);
    return true;
  };

  const modsOf = (e) => ({shiftKey: !!e.shiftKey, altKey: !!e.altKey});

  const onDown = (e) => {
    if (e.button !== undefined && e.button !== button) return;
    if (e.pkHandled) return;                     // claimed by the compass (math/compass.js)
    if (opts.enabled && !opts.enabled(e)) return;
    const v = getView(viewRef);
    if (!v) return;
    const p = eventToViewPx(el, v, e);
    if (v.inPlot && !v.inPlot(p[0], p[1])) return;
    drag = {id: e.pointerId, kind: mode, screen: [p], down: modsOf(e), moved: false};
    if (el.setPointerCapture && e.pointerId != null) {
      try {
        el.setPointerCapture(e.pointerId);
      } catch (err) {
        if (!(err instanceof DOMException)) throw err;
      }
    }
    if (e.preventDefault) e.preventDefault();
  };

  const current = (e) => {
    const v = getView(viewRef);
    const S = drag.screen;
    const shape = shapeFromScreen(v, drag.kind, drag.kind === 'lasso' ? S : [S[0], S[S.length - 1]]);
    const op = opFromEvent({shiftKey: drag.down.shiftKey || !!(e && e.shiftKey), altKey: drag.down.altKey || !!(e && e.altKey)});
    return {shape, op};
  };

  const onMove = (e) => {
    if (!drag || (e.pointerId != null && drag.id != null && e.pointerId !== drag.id)) return;
    const v = getView(viewRef);
    let p = eventToViewPx(el, v, e);
    if (v.plot) {
      // keep the brush inside the plot rectangle
      p = [Math.min(v.plot.right, Math.max(v.plot.left, p[0])), Math.min(v.plot.bottom, Math.max(v.plot.top, p[1]))];
    }
    const S = drag.screen;
    const p0 = S[0];
    if (!drag.moved && Math.hypot(p[0] - p0[0], p[1] - p0[1]) < CLICK_PX) return;
    drag.moved = true;
    if (drag.kind === 'lasso') {
      const last = S[S.length - 1];
      if (Math.hypot(p[0] - last[0], p[1] - last[1]) >= LASSO_STEP_PX) S.push(p);
    } else if (S.length === 1) {
      S.push(p);
    } else {
      S[S.length - 1] = p;
    }
    const {shape, op} = current(e);
    emitPreview({kind: drag.kind, screen: S.slice(), shape, op});
  };

  const onUp = (e) => {
    if (!drag || (e.pointerId != null && drag.id != null && e.pointerId !== drag.id)) return;
    const d = drag;
    if (!d.moved) {
      cancel();
      if (opts.onClick) {
        const v = getView(viewRef);
        const p = eventToViewPx(el, v, e);
        opts.onClick(p[0], p[1], e);
      }
      return;
    }
    const {shape, op} = current(e);
    cancel();
    if (shape && opts.onCommit) opts.onCommit(shape, op);
  };

  const onKey = (e) => {
    if (e.key === 'Escape' && drag) {
      cancel();
      if (e.preventDefault) e.preventDefault();
    }
  };

  const onEscape = (payload) => {
    if (payload && payload.handled) return;
    if (cancel() && payload) payload.handled = true;
  };

  el.addEventListener('pointerdown', onDown);
  el.addEventListener('pointermove', onMove);
  el.addEventListener('pointerup', onUp);
  el.addEventListener('pointercancel', cancel);
  const win = typeof window !== 'undefined' ? window : null;
  if (win) win.addEventListener('keydown', onKey);
  const offEsc = store && typeof store.onEvent === 'function' ? store.onEvent('keys:escape', onEscape) : null;

  const detach = () => {
    cancel();
    el.removeEventListener('pointerdown', onDown);
    el.removeEventListener('pointermove', onMove);
    el.removeEventListener('pointerup', onUp);
    el.removeEventListener('pointercancel', cancel);
    if (win) win.removeEventListener('keydown', onKey);
    if (typeof offEsc === 'function') offEsc();
    if (el.style) el.style.touchAction = prevTouch;
  };
  detach.setMode = (m) => {
    if (modes.includes(m)) { mode = m; cancel(); }
    return mode;
  };
  detach.mode = () => mode;
  detach.setView = (v) => { viewRef = v; };
  detach.cancel = cancel;
  detach.active = () => !!drag;
  return detach;
}
