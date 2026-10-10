// Brushes and picking over projected / decoded coordinates (ARCHITECTURE.md section 6.6).
// Brushes act only on drawable rows (the Brick2221 hidden-point fix). Lasso uses the even-odd
// rule with a bounding-box prefilter. Pure module.

import {buildGrid} from '../../vendor/gc/workers/kernel.js';

const LOG = (v) => (v > 0 ? Math.log10(v) : NaN);

/**
 * Drawable rows of an x/y view: visible (vis > 0, or no vis mask), finite coordinates, and
 * positive values on log axes.
 */
export function drawableXY(x, y, vis, {xlog = false, ylog = false} = {}) {
  const n = x.length;
  const d = new Uint8Array(n);
  for (let i = 0; i < n; i++) {
    if (vis && !vis[i]) continue;
    const a = x[i], b = y[i];
    if (!(a === a && b === b) || a === Infinity || a === -Infinity || b === Infinity || b === -Infinity) continue;
    if (xlog && !(a > 0)) continue;
    if (ylog && !(b > 0)) continue;
    d[i] = 1;
  }
  return d;
}

/** Drawable rows of a projection: vis > 0 (kernel semantics) and finite coordinates. */
export function drawableProj(vis, x, y) {
  const n = vis.length;
  const d = new Uint8Array(n);
  for (let i = 0; i < n; i++) {
    if (!vis[i]) continue;
    if (x && !Number.isFinite(x[i])) continue;
    if (y && !Number.isFinite(y[i])) continue;
    d[i] = 1;
  }
  return d;
}

/** Even-odd point-in-polygon test (pts: [[x, y], ...], implicitly closed). */
export function pointInPolygon(px, py, pts) {
  let inside = false;
  const m = pts.length;
  for (let a = 0, b = m - 1; a < m; b = a++) {
    const xa = pts[a][0], ya = pts[a][1], xb = pts[b][0], yb = pts[b][1];
    if ((ya > py) !== (yb > py)) {
      const xc = ((xb - xa) * (py - ya)) / (yb - ya) + xa;
      if (px < xc) inside = !inside;
    }
  }
  return inside;
}

/**
 * Normalise a data-coordinate shape into the (possibly log10) space the test runs in.
 * Circles take `r` (and an optional `ry` for an ellipse) in the axis units of that space
 * (decades on log axes).
 */
export function normShape(shape, {xlog = false, ylog = false} = {}) {
  const tx = xlog ? LOG : (v) => +v;
  const ty = ylog ? LOG : (v) => +v;
  switch (shape && shape.kind) {
    case 'rect': {
      const x0 = tx(shape.x0), x1 = tx(shape.x1), y0 = ty(shape.y0), y1 = ty(shape.y1);
      return {kind: 'rect', x0: Math.min(x0, x1), x1: Math.max(x0, x1), y0: Math.min(y0, y1), y1: Math.max(y0, y1)};
    }
    case 'circle': {
      const r = Math.abs(+shape.r);
      const ry = shape.ry !== undefined ? Math.abs(+shape.ry) : r;
      return {kind: 'circle', cx: tx(shape.cx), cy: ty(shape.cy), rx: r, ry};
    }
    case 'lasso': {
      const pts = (shape.pts || []).map((p) => [tx(p[0]), ty(p[1])]).filter((p) => Number.isFinite(p[0]) && Number.isFinite(p[1]));
      let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
      for (const [a, b] of pts) {
        if (a < x0) x0 = a;
        if (a > x1) x1 = a;
        if (b < y0) y0 = b;
        if (b > y1) y1 = b;
      }
      return {kind: 'lasso', pts, x0, x1, y0, y1};
    }
    default:
      throw new Error(`unknown brush shape ${shape && shape.kind}`);
  }
}

/**
 * Rows of (x, y) inside `shape` among `drawable` rows.
 * Returns {hit: Uint8Array, count}.
 */
export function brushArrays(x, y, drawable, shape, opts = {}) {
  const n = x.length;
  const hit = new Uint8Array(n);
  const s = normShape(shape, opts);
  const xlog = !!opts.xlog, ylog = !!opts.ylog;
  let count = 0;
  const X = (i) => (xlog ? LOG(x[i]) : x[i]);
  const Y = (i) => (ylog ? LOG(y[i]) : y[i]);
  if (s.kind === 'rect') {
    if (!(s.x1 >= s.x0) || !(s.y1 >= s.y0)) return {hit, count};
    for (let i = 0; i < n; i++) {
      if (!drawable[i]) continue;
      const a = X(i), b = Y(i);
      if (a >= s.x0 && a <= s.x1 && b >= s.y0 && b <= s.y1) {
        hit[i] = 1;
        count++;
      }
    }
  } else if (s.kind === 'circle') {
    if (!(s.rx > 0) || !(s.ry > 0) || !Number.isFinite(s.cx) || !Number.isFinite(s.cy)) return {hit, count};
    const irx = 1 / s.rx, iry = 1 / s.ry;
    for (let i = 0; i < n; i++) {
      if (!drawable[i]) continue;
      const dx = (X(i) - s.cx) * irx, dy = (Y(i) - s.cy) * iry;
      if (dx * dx + dy * dy <= 1) {
        hit[i] = 1;
        count++;
      }
    }
  } else {
    if (s.pts.length < 3) return {hit, count};
    for (let i = 0; i < n; i++) {
      if (!drawable[i]) continue;
      const a = X(i), b = Y(i);
      if (a < s.x0 || a > s.x1 || b < s.y0 || b > s.y1) continue;
      if (pointInPolygon(a, b, s.pts)) {
        hit[i] = 1;
        count++;
      }
    }
  }
  return {hit, count};
}

/**
 * Pick grid over drawable rows (vendored GC kernel.buildGrid). The vendored builder takes its
 * bounds from rows with `i % floor(n/20000) === 0`; when no drawable row falls on that stride
 * (sparse id filters, every-other-row masks) its bounds are NaN. Those grids are rebuilt here with
 * bounds sampled from the drawable rows themselves.
 */
export function buildPickGrid(x, y, drawable, G = 256) {
  const g = buildGrid(x, y, drawable, x.length, G);
  if (!g.nvis || (Number.isFinite(g.x0) && Number.isFinite(g.y0) && g.cw > 0 && g.ch > 0)) return g;
  return buildGridDrawable(x, y, drawable, G);
}

/** Same layout as kernel.buildGrid, with the quantile subsample taken over drawable rows. */
export function buildGridDrawable(x, y, drawable, G = 256) {
  const n = x.length;
  let nvis = 0;
  for (let i = 0; i < n; i++) if (drawable[i]) nvis++;
  const start = new Uint32Array(G * G + 1);
  if (!nvis) return {x0: 0, y0: 0, cw: 1, ch: 1, G, start, items: new Uint32Array(0), nvis: 0};
  const step = Math.max(1, Math.floor(nvis / 20000));
  const xs = [], ys = [];
  for (let i = 0, k = 0; i < n; i++) {
    if (!drawable[i]) continue;
    if (k++ % step === 0) {
      xs.push(x[i]);
      ys.push(y[i]);
    }
  }
  xs.sort((a, b) => a - b);
  ys.sort((a, b) => a - b);
  const q = (a, p) => a[Math.min(a.length - 1, Math.max(0, Math.round(p * (a.length - 1))))];
  let x0 = q(xs, 0.001), x1 = q(xs, 0.999), y0 = q(ys, 0.001), y1 = q(ys, 0.999);
  if (!(x1 > x0)) {
    x0 -= 0.5;
    x1 += 0.5;
  }
  if (!(y1 > y0)) {
    y0 -= 0.5;
    y1 += 0.5;
  }
  const cw = (x1 - x0) / G, ch = (y1 - y0) / G;
  const cell = (i) => {
    let cx = Math.floor((x[i] - x0) / cw), cy = Math.floor((y[i] - y0) / ch);
    cx = cx < 0 ? 0 : cx >= G ? G - 1 : cx;
    cy = cy < 0 ? 0 : cy >= G ? G - 1 : cy;
    return cy * G + cx;
  };
  for (let i = 0; i < n; i++) if (drawable[i]) start[cell(i) + 1]++;
  for (let c = 0; c < G * G; c++) start[c + 1] += start[c];
  const fill = start.slice(0, G * G);
  const items = new Uint32Array(nvis);
  for (let i = 0; i < n; i++) if (drawable[i]) items[fill[cell(i)]++] = i;
  return {x0, y0, cw, ch, G, start, items, nvis};
}

/**
 * Nearest drawable row to (px, py) within radius r, measured as
 * sqrt(((x-px)*kx)^2 + ((y-py)*ky)^2) (kx, ky: pixels per data unit; 1 = data units).
 * Uses `grid` from buildPickGrid when given, else a linear scan. Returns -1 when none.
 */
export function pickNearest(x, y, drawable, px, py, r, {kx = 1, ky = 1, grid = null} = {}) {
  if (!(r > 0) || !Number.isFinite(px) || !Number.isFinite(py)) return -1;
  let best = -1, bestD = r * r;
  const test = (i) => {
    const dx = (x[i] - px) * kx, dy = (y[i] - py) * ky;
    const d = dx * dx + dy * dy;
    if (d <= bestD) {
      if (d < bestD || best < 0 || i < best) best = i;
      bestD = d;
    }
  };
  if (grid && grid.nvis !== undefined && Number.isFinite(grid.x0) && Number.isFinite(grid.y0) && grid.cw > 0 && grid.ch > 0) {
    if (!grid.nvis) return -1;
    const G = grid.G;
    const rx = r / Math.abs(kx), ry = r / Math.abs(ky);
    const clamp = (v) => (v < 0 ? 0 : v >= G ? G - 1 : v);
    const cx0 = clamp(Math.floor((px - rx - grid.x0) / grid.cw)), cx1 = clamp(Math.floor((px + rx - grid.x0) / grid.cw));
    const cy0 = clamp(Math.floor((py - ry - grid.y0) / grid.ch)), cy1 = clamp(Math.floor((py + ry - grid.y0) / grid.ch));
    for (let cy = cy0; cy <= cy1; cy++) {
      for (let cx = cx0; cx <= cx1; cx++) {
        const c = cy * G + cx;
        for (let k = grid.start[c]; k < grid.start[c + 1]; k++) test(grid.items[k]);
      }
    }
    return best;
  }
  for (let i = 0; i < x.length; i++) if (drawable[i]) test(i);
  return best;
}
