// Scatter pane (ARCHITECTURE.md sections 6.9, 7.1, 7.2, 7.5, 7.6; capability cmd_ccd_scatter).
//
// Tier table (section 7.1), chosen from N by boot (`tier:<pane>`) and lowered to F here when
// WebGL2 is missing, the GPU budget refuses the pane or XYRenderer reports refused targets:
//   T0  canvas2d with per-point styling: category / continuous colour, hollow_if, error bars
//       (ColMeta.err, combined in quadrature through the axis expression), limit arrows (sparse
//       limits via record()._limits), labels, pair connectors; vector snapshot (SVG)
//   T1  XYRenderer markers; canvas2d layers for axes, overlays, brush UI and focus ring
//   T2  XYRenderer density on the full N after SETTLE_MS and on the MOTION_PREFIX LOD prefix
//       while the view moves. Semantic zoom: when bin2d counts < SEMANTIC_ZOOM_MARKERS rows in
//       view the pane switches to markers
//   F   pre-binned manifest grid (preset axes, linear) or worker bin2d (512²) drawn with
//       putImageData; markers when <= T0_MAX rows are in view
// Pre-binned first paint: the manifest density grid for the current axes is drawn as soon as
// the manifest is open, before any column arrives. Context datasets (`background` option, or
// any page catalog with context:true whose columns resolve the axes) are drawn as a faint
// worker-binned density under the foreground and never take part in selection. `raster`
// layers ([{url, extent: [x0, x1, y_bottom, y_top], x?, y?, cell?, opacity?}], matplotlib
// extent order) are drawn under the points; `x` / `y` bind a layer to an axis pair and `cell`
// to a grid cell (an unbound layer belongs to the single plot). Binned-statistic grids of the
// manifest (`density[].stat`) for the current axes can be chosen as a background in the bar.
// `grid: [[x, y], ...]` shows small multiples with the same layers per cell (rasters, context
// density, points or density, hollow_if, overlays); each cell can be expanded (focused and
// popped out); closing the pop-out returns to the grid. An empty `grid` gives a single plot.
//
// Layer stack inside .pk-plot-stage: under canvas (axes, raster, context density, T0 / F
// drawing), GL canvas (XYRenderer), over canvas (overlays, focus / hover rings, brush preview;
// receives the pointer). Every layer derives from the same plot/axes.js view.
//
// Store: axes:<pane> {x, y, xlog, ylog, xinv, yinv, xr?, yr?} (xr / yr written when a zoom or
// pan settles), color, av, rv, dm, filters:<ds> (via chips / legend), vis:<ds>, sel:<ds>,
// focus, hover, tier:<pane>, layout. Distance modulus: dm folds into the axis constant
// (math/fold.js, section 7.3) on linear axes; the GL camera is shifted with foldCamera and
// worker shapes / picks / bins are un-shifted, so recipes stay in catalog units. Overlay tracks
// get dm = 0 because the data already carry it (IMPLEMENTATION_NOTES 37).
//
// This module also exports the plot machinery shared by panes/manifold.js and panes/hist.js.

import {
  T0_MAX, MOTION_PREFIX, SETTLE_MS, SEMANTIC_ZOOM_MARKERS, IDS_IN_HASH_MAX,
} from '../core/constants.js';
import {tierFor} from '../core/boot.js';
import {makeView, cameraFromView, zoomDomain, panDomain, extent} from '../plot/axes.js';
import {
  prepareCanvas, tokensOf, drawAxes, drawPoints, drawFocusRing, drawDensityGrid, toSVG, cmapColor,
} from '../plot/canvas2d.js';
import {attachBrush, drawBrushPreview, eventToViewPx} from '../plot/brushui.js';
import {resolveOverlays, drawOverlays} from '../plot/overlays.js';
import {XYRenderer} from '../gl/xyrenderer.js';
import {Budget, XY_VERTEX_BYTES} from '../gl/budget.js';
import {fold, oneHotRows, scatterDims, compileLinear, foldCamera} from '../math/fold.js';
import {fmtCount} from '../core/fmt.js';
import {createAxisPicker, exprLabel} from '../ui/axis_picker.js';
import {createLegend, resolveColor, autoRange, plotLevelColors, gpuCatCodes, gpuCatColors} from '../ui/legend.js';
import {createChips} from '../ui/chips.js';
import {createControls} from '../ui/controls.js';

export const meta = {
  type: 'scatter',
  caps: ['cmd_ccd_scatter', 'selectable_axes', 'class_coloring', 'density_rendering', 'brush_rect',
    'brush_circle', 'brush_lasso', 'brush_set_ops', 'linked_selection', 'filter_toggles', 'av_slider',
    'isochrones_or_tracks', 'extinction_vectors_on_ccd'],
  needs: ['catalog'],
};

export const F_BINS = 512;
export const CONTEXT_BINS = 256;
export const GRID_BINS = 128;
export const HOVER_MS = 40;
export const PICK_PX = 6;
export const PICK_PX_TOUCH = 14;
export const LIMIT_BATCH = 200;
export const WHEEL_STEP = 1.0015;

// =============================================================== shared helpers

/** document.createElement with a class and text. */
export function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

/** A small button (pk-btn pk-icon-btn) with title / aria-label. */
export function iconButton(text, title, cls = '') {
  const b = el('button', `pk-btn pk-icon-btn ${cls}`.trim(), text);
  b.type = 'button';
  b.title = title;
  b.setAttribute('aria-label', title);
  return b;
}

const now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now());
const raf = typeof requestAnimationFrame === 'function' ? requestAnimationFrame : (fn) => setTimeout(() => fn(now()), 16);
const caf = typeof cancelAnimationFrame === 'function' ? cancelAnimationFrame : clearTimeout;

/** Page datasets ({id: DatasetRef}). */
export function pageDatasets(ctx) {
  return (ctx && ctx.app && ctx.app.page && ctx.app.page.datasets) || {};
}

/** Catalog dataset of a pane: its own dataset, the catalog behind it, or the app's primary. */
export function paneDataset(ctx) {
  const all = pageDatasets(ctx);
  let ds = ctx.dataset;
  if (ds && all[ds] && all[ds].kind !== 'catalog' && all[ds].catalog) ds = all[ds].catalog;
  if (ds && all[ds] && all[ds].kind === 'catalog') return ds;
  if (ctx.app && ctx.app.primaryDs) return ctx.app.primaryDs;
  for (const [id, r] of Object.entries(all)) if (r && r.kind === 'catalog' && !r.context) return id;
  return null;
}

const docCaches = new WeakMap();

/** JSON document of the first page dataset of `kind` (hub.json, cached per hub); null if none. */
export function docOf(ctx, kind) {
  const all = pageDatasets(ctx);
  const ds = Object.keys(all).find((k) => all[k] && all[k].kind === kind);
  const hub = ctx.hub || (ctx.app && ctx.app.hub);
  if (!ds || !hub || typeof hub.json !== 'function') return Promise.resolve(null);
  let m = docCaches.get(hub);
  if (!m) { m = new Map(); docCaches.set(hub, m); }
  if (!m.has(ds)) {
    const p = Promise.resolve(hub.json(ds));
    m.set(ds, p);
    p.catch(() => m.delete(ds));
  }
  return m.get(ds);
}

/** The page-wide GPU budget (created by the first plot pane; boot does not create one). */
export function gpuBudget(app) {
  if (!app) return new Budget();
  if (!app.gpuBudget) app.gpuBudget = new Budget();
  if (!app.gpuPanes) app.gpuPanes = new Map();
  return app.gpuBudget;
}

/**
 * Register a pane's GPU needs and run Budget.plan over all registered panes (section 7.5).
 * info: {cssW, cssH, density, vertexBytes}. Returns {dpr, refused: bool, plan}.
 */
export function planGpu(app, id, info, deviceDpr = 1) {
  const B = gpuBudget(app);
  app.gpuPanes.set(id, {id, cssW: info.cssW, cssH: info.cssH, density: !!info.density, vertexBytes: info.vertexBytes || 0});
  const panes = Array.from(app.gpuPanes.values());
  const vb = panes.reduce((s, p) => s + (p.vertexBytes || 0), 0);
  const plan = B.plan(panes, vb, {dpr: Math.min(2, Math.max(1, deviceDpr || 1))});
  return {dpr: plan.dpr, refused: plan.refused.includes(id), plan};
}

export function releaseGpu(app, id) {
  if (app && app.gpuPanes) app.gpuPanes.delete(id);
  if (app && app.gpuBudget) app.gpuBudget.release(id);
}

/** Current tier of a pane (store, else tierFor(n)). */
export function paneTier(ctx, n) {
  const t = ctx.store.get(`tier:${ctx.id}`);
  if (t === 'T0' || t === 'T1' || t === 'T2' || t === 'F') return t;
  const rt = (ctx.app && ctx.app.runtime) || {};
  return tierFor(n, rt);
}

export function setPaneTier(ctx, t) {
  if (ctx.store.get(`tier:${ctx.id}`) !== t) ctx.store.set({[`tier:${ctx.id}`]: t}, {source: ctx.id});
}

/** Stage: under (2D), gl (WebGL) and over (2D, pointer) canvases stacked in `host`. */
export function makeStage(host, cls = '') {
  const stage = el('div', `pk-plot-stage ${cls}`.trim());
  const under = el('canvas', 'pk-plot-under');
  const glc = el('canvas', 'pk-plot-gl');
  const over = el('canvas', 'pk-plot-over');
  over.tabIndex = 0;
  over.setAttribute('role', 'img');
  under.setAttribute('aria-hidden', 'true');
  glc.setAttribute('aria-hidden', 'true');
  const note = el('div', 'pk-plot-note');
  note.hidden = true;
  note.setAttribute('role', 'status');
  stage.append(under, glc, over, note);
  host.append(stage);
  return {stage, under, glc, over, note};
}

/** Brush-mode toolbar (rect / circle / lasso / pan) for ctx.tools. Returns {el, mode(), set(m)}. */
export function brushToolbar(onMode, {modes = ['rect', 'circle', 'lasso'], pan = true, initial = 'rect'} = {}) {
  const grp = el('span', 'pk-btn-group pk-brush-modes');
  grp.setAttribute('role', 'group');
  grp.setAttribute('aria-label', 'Pointer mode');
  const labels = {rect: ['▭', 'Rectangle brush'], circle: ['◯', 'Circle brush'], lasso: ['➰', 'Lasso brush'], pan: ['✥', 'Pan (drag); wheel zooms']};
  const all = pan ? [...modes, 'pan'] : modes.slice();
  let cur = all.includes(initial) ? initial : all[0];
  const btns = {};
  for (const m of all) {
    const b = iconButton(labels[m][0], `${labels[m][1]} (shift add, alt subtract, shift+alt intersect)`, `pk-mode-${m}`);
    if (m === 'pan') b.title = labels[m][1];
    b.addEventListener('click', () => set(m));
    btns[m] = b;
    grp.append(b);
  }
  function set(m) {
    if (!btns[m]) return;
    cur = m;
    for (const [k, b] of Object.entries(btns)) b.setAttribute('aria-pressed', k === m ? 'true' : 'false');
    onMode(m);
  }
  for (const [k, b] of Object.entries(btns)) b.setAttribute('aria-pressed', k === cur ? 'true' : 'false');
  return {el: grp, mode: () => cur, set};
}

/** Composite canvases (same CSS size) into one PNG Blob over the theme background. */
/**
 * Clip a full-stage WebGL canvas to the plot rectangle of `view` (the GPU renderers draw over
 * the whole surface, margins included). The rectangle is kept in data-clip for snapshots.
 */
export function clipToPlot(canvas, view) {
  if (!canvas || !view || !view.plot) return;
  const {left, top, right, bottom} = view.plot;
  const W = view.width, H = view.height;
  const v = `inset(${top}px ${Math.max(0, W - right)}px ${Math.max(0, H - bottom)}px ${left}px)`;
  if (canvas.style.clipPath !== v) canvas.style.clipPath = v;
  canvas.dataset.clip = `${left},${top},${right},${bottom}`;
}

export function snapshotCanvases(canvases, w, h, bg = '#ffffff', scale = 2) {
  const out = document.createElement('canvas');
  out.width = Math.max(1, Math.round(w * scale));
  out.height = Math.max(1, Math.round(h * scale));
  const c = out.getContext('2d');
  c.fillStyle = bg;
  c.fillRect(0, 0, out.width, out.height);
  for (const cv of canvases) {
    if (!cv || !cv.width || cv.hidden || (cv.style && cv.style.visibility === 'hidden')) continue;
    const clip = cv.dataset && cv.dataset.clip ? cv.dataset.clip.split(',').map(Number) : null;
    c.save();
    if (clip && clip.length === 4 && clip.every(Number.isFinite)) {
      c.beginPath();
      c.rect(clip[0] * scale, clip[1] * scale, (clip[2] - clip[0]) * scale, (clip[3] - clip[1]) * scale);
      c.clip();
    }
    c.drawImage(cv, 0, 0, out.width, out.height);
    c.restore();
  }
  return new Promise((resolve, reject) => {
    out.toBlob((b) => (b ? resolve(b) : reject(new Error('canvas snapshot failed'))), 'image/png');
  });
}

/** Rows sampled for quantile extents (the sort runs on at most this many values). */
export const EXTENT_SAMPLE = 50000;

/**
 * plot/axes.js extent() with quantile limits computed on a strided sample of at most
 * EXTENT_SAMPLE values (the full sort over millions of rows blocks the main thread for
 * seconds). Without `q` the exact min / max of every row is used. Rows are LOD-ordered, so a
 * stride is a fair sample.
 */
export function sampledExtent(values, opts = {}) {
  const n = values ? values.length : 0;
  if (!opts.q || n <= EXTENT_SAMPLE) return extent(values, opts);
  const step = Math.ceil(n / EXTENT_SAMPLE);
  const mask = opts.mask || null;
  const sub = new Float64Array(Math.ceil(n / step));
  let k = 0;
  for (let i = 0; i < n; i += step) {
    if (mask && !mask[i]) continue;
    sub[k++] = values[i];
  }
  if (k < 3) return extent(values, opts);
  return extent(sub.subarray(0, k), {...opts, mask: null});
}

/** Rows that can be drawn: finite x / y and visible. */
export function drawableMask(x, y, vis, n) {
  const m = new Uint8Array(n);
  for (let i = 0; i < n; i++) {
    if (vis && !vis[i]) continue;
    const a = x[i], b = y[i];
    if (a === a && b === b && a !== Infinity && a !== -Infinity && b !== Infinity && b !== -Infinity) m[i] = 1;
  }
  return m;
}

/** log10 copy (NaN for <= 0) or the array itself. */
export function transformed(v, log) {
  if (!log) return v;
  const out = new Float32Array(v.length);
  for (let i = 0; i < v.length; i++) out[i] = v[i] > 0 ? Math.log10(v[i]) : NaN;
  return out;
}

/** Mid point of the finite range of a strided sample (float32 origin for XYRenderer). */
export function midOrigin(v) {
  let lo = Infinity, hi = -Infinity;
  const step = Math.max(1, Math.floor(v.length / 50000));
  for (let i = 0; i < v.length; i += step) {
    const a = v[i];
    if (a === a && a !== Infinity && a !== -Infinity) { if (a < lo) lo = a; if (a > hi) hi = a; }
  }
  return hi >= lo ? (lo + hi) / 2 : 0;
}

/** Magnitude base keys of a catalog (Set). */
export function magKeys(client) {
  return new Set((client.manifest.cols || []).filter((c) => c.role === 'mag').map((c) => c.key));
}

/** Translate a brush shape by (dx, dy) in data units (linear axes only). */
export function shiftShape(s, dx, dy) {
  if (!s || (!dx && !dy)) return s;
  if (s.kind === 'rect') return {...s, x0: s.x0 + dx, x1: s.x1 + dx, y0: s.y0 + dy, y1: s.y1 + dy};
  if (s.kind === 'circle') return {...s, cx: s.cx + dx, cy: s.cy + dy};
  if (s.kind === 'lasso') return {...s, pts: s.pts.map((p) => [p[0] + dx, p[1] + dy])};
  return s;
}

/** Axis shift from the distance modulus folded into linear axis expressions (section 7.3). */
export function dmShift(client, x, y, xlog, ylog, dm) {
  if (!dm || !x || !y) return [0, 0];
  const ex = (k) => {
    const m = client.col(k);
    return m && m.virtual && m.expr ? m.expr : k;
  };
  const dims = scatterDims(ex(x), ex(y));
  const rows = oneHotRows(ex(x), ex(y));
  let f0, f1;
  try {
    f0 = fold(rows, dims, {dm: 0});
    f1 = fold(rows, dims, {dm, isMag: magKeys(client)});
  } catch (err) {
    if (err instanceof SyntaxError) return [0, 0];
    throw err;
  }
  return [xlog ? 0 : f1.c[0] - f0.c[0], ylog ? 0 : f1.c[1] - f0.c[1]];
}

/** Default inversion of an axis expression: ColMeta.invert, else true for magnitudes. */
export function autoInvert(client, expr) {
  const m = expr ? client.col(expr) : null;
  if (!m) return false;
  if (m.invert !== undefined && m.invert !== null) return !!m.invert;
  return m.role === 'mag';
}

/** Data-space grid {counts, nx, ny, xr, yr} from a worker bin2d result in transformed units. */
export function gridToData(g, xlog, ylog, shift = [0, 0]) {
  if (!g || !g.counts) return null;
  const xr = g.xr || [0, 1], yr = g.yr || [0, 1];
  const tx = (a) => (xlog ? Math.pow(10, a) : a + shift[0]);
  const ty = (a) => (ylog ? Math.pow(10, a) : a + shift[1]);
  return {counts: g.counts, nx: g.nx, ny: g.ny, xr: [tx(xr[0]), tx(xr[1])], yr: [ty(yr[0]), ty(yr[1])], total: g.total};
}

/**
 * Binned-statistic grid (manifest density entry with `stat`, f32 values, NaN = empty) as a
 * drawDensityGrid input: values stretched linearly over their 2..98 % range to (0, 1], NaN
 * kept, so drawDensityGrid({log: false, lo: 0, hi: 1}) colour-maps the statistic.
 */
export function statGridImage(g) {
  if (!g || !g.values) return null;
  const v = g.values;
  const fin = [];
  const step = Math.max(1, Math.floor(v.length / 20000));
  for (let i = 0; i < v.length; i += step) if (Number.isFinite(v[i])) fin.push(v[i]);
  if (!fin.length) return null;
  fin.sort((a, b) => a - b);
  const q = (p) => fin[Math.min(fin.length - 1, Math.max(0, Math.round(p * (fin.length - 1))))];
  let lo = q(0.02), hi = q(0.98);
  if (!(hi > lo)) { lo = fin[0]; hi = fin[fin.length - 1]; }
  if (!(hi > lo)) hi = lo + 1;
  const out = new Float32Array(v.length);
  for (let i = 0; i < v.length; i++) {
    const a = v[i];
    out[i] = Number.isFinite(a) ? 1e-6 + Math.max(0, Math.min(1, (a - lo) / (hi - lo))) : NaN;
  }
  return {counts: out, nx: g.nx, ny: g.ny, xr: g.xr, yr: g.yr, lo, hi};
}

/** Raw transformed range [lo, hi] of a view axis (un-shifted, log10 on log axes). */
export function rawRange(s, shift = 0) {
  const a = Math.min(s.d0, s.d1), b = Math.max(s.d0, s.d1);
  if (s.kind === 'log') return [Math.log10(a), Math.log10(b)];
  return [a - shift, b - shift];
}

/**
 * Colour state for a catalog: values / codes loaded for the effective colour.
 * Returns {kind, by, cmap, lo, hi, meta, values, codes, levelCss, gpu: XYRenderer colour}.
 */
export async function loadColor(client, store, config, theme, cache) {
  const c = resolveColor(store, config, client);
  const tk = theme ? theme() : {};
  const out = {kind: c.kind, by: c.by, cmap: c.cmap, lo: c.lo, hi: c.hi, meta: c.meta, values: null, codes: null, levelCss: null,
    gpu: {mode: 0}};
  if (c.kind === 'none') return out;
  let values = cache && cache.by === c.by ? cache.values : null;
  if (!values) values = await client.values(c.by);
  out.values = values;
  if (c.kind === 'cat') {
    const L = c.meta.levels.length;
    out.codes = gpuCatCodes(values, L);
    out.levelCss = plotLevelColors(c.meta, tk);
    out.gpu = {mode: 2, slot: 0, colors: gpuCatColors(c.meta, tk)};
  } else {
    if (out.lo === null || out.hi === null) {
      const [lo, hi] = autoRange(values);
      if (out.lo === null) out.lo = lo;
      if (out.hi === null) out.hi = hi;
    }
    out.gpu = {mode: 1, lo: out.lo, hi: out.hi, cmap: out.cmap};
  }
  return out;
}

/** CSS colour of row i under a colour state (null = default). */
export function rowColor(cs, i) {
  if (!cs || cs.kind === 'none' || !cs.values) return null;
  const v = cs.values[i];
  if (!(v === v)) return null;
  if (cs.kind === 'cat') return cs.levelCss[v | 0] || null;
  let lo = cs.lo, hi = cs.hi;
  let name = String(cs.cmap || 'viridis');
  if (name.endsWith('_r')) { name = name.slice(0, -2); [lo, hi] = [hi, lo]; }
  const t = hi !== lo ? (v - lo) / (hi - lo) : 0.5;
  return cmapColor(name, Math.max(0, Math.min(1, t)));
}

/** Pick radius in CSS px for a pointer event. */
export function pickRadius(e) {
  return e && e.pointerType === 'touch' ? PICK_PX_TOUCH : PICK_PX;
}

/**
 * Apply a worker brush result to the page selection and announce it. recipeView is the
 * RecipeOp view ({ds, x, y, xlog, ylog} for xy brushes) or null to record no recipe.
 */
export async function applyBrush(ctx, ds, res, shape, op, recipe) {
  const sel = await ctx.app.selectionFor(ds);
  sel.apply(res.hit, op, res.drawable, recipe || undefined);
  ctx.store.emit('brush:commit', {pane: ctx.id, ds, shape, op, count: sel.count});
  return sel.count;
}

/** Selection ids RecipeOp for brushes that cannot be replayed (IMPLEMENTATION_NOTES 35). */
export async function idsRecipe(ctx, ds, hit, op) {
  const idx = [];
  for (let i = 0; i < hit.length && idx.length <= IDS_IN_HASH_MAX; i++) if (hit[i]) idx.push(i);
  if (idx.length > IDS_IN_HASH_MAX) return null;
  const ids = await Promise.all(idx.map((i) => ctx.app.idOf(ds, i)));
  return {op, kind: 'ids', view: {ds}, ids: ids.map(String)};
}

/** Focus a row of `ds` from a pick (sets store `focus`). */
export function focusRow(ctx, ds, i) {
  if (!(i >= 0)) return;
  const f = ctx.store.get('focus');
  if (f && f.ds === ds && f.i === i) return;
  ctx.store.set({focus: {ds, i, id: null}}, {source: ctx.id});
}

/** Focus / hover row index in `ds` from the store (-1 when none). */
export function storeRow(store, key, ds) {
  const f = store.get(key);
  return f && f.ds === ds && Number.isInteger(f.i) && f.i >= 0 ? f.i : -1;
}

/** Text of the axis title for an expression. */
export function axisTitle(client, expr, log) {
  const t = exprLabel(client, expr);
  return log ? `${t} (log)` : t;
}

/** Context catalogs to draw under a pane (`background` option, else page context:true catalogs). */
export function contextDatasets(ctx, ds) {
  const all = pageDatasets(ctx);
  const bg = ctx.config && ctx.config.background;
  if (bg) return all[bg] && all[bg].kind === 'catalog' && bg !== ds ? [bg] : [];
  return Object.keys(all).filter((k) => k !== ds && all[k] && all[k].kind === 'catalog' && all[k].context);
}

/** True when every base key of the expressions is a column of the client. */
export function resolvesIn(client, exprs) {
  for (const e of exprs) {
    if (!e) return false;
    if (client.col(e)) continue;
    let keys;
    try {
      keys = client.baseKeys(e);
    } catch (err) {
      if (err instanceof SyntaxError) return false;
      throw err;
    }
    if (!keys.length || !keys.every((k) => client.col(k))) return false;
  }
  return true;
}

/** Whether another pane of the page already provides the A_V controls (sed / spectrum1d). */
export function pageHasModelPane(ctx) {
  const lay = ctx.layout;
  if (!lay || typeof lay.panes !== 'function') return false;
  return lay.panes().some((p) => p && p.id !== ctx.id && (p.type === 'sed' || p.type === 'spectrum1d'));
}

function wavelengthOf(meta) {
  const f = String((meta && meta.filter) || meta.key || '');
  const m = /F(\d{3,4})(W2|W|M|N|LP|C)?\b/i.exec(f);
  if (m) return Number(m[1]) / (m[1].length === 4 ? 1000 : 100);
  const um = /([\d.]+)\s*um/i.exec(f);
  return um ? Number(um[1]) : NaN;
}

/**
 * Axis presets: manifest presets of kind 'axes', else automatic colour-magnitude and
 * colour-colour presets from the magnitude columns in wavelength order (short minus long).
 * Returns [{key, label, x, y, xlog?, ylog?}].
 */
export function axisPresets(client) {
  const man = client.manifest || {};
  const listed = (man.presets || []).filter((p) => p && p.kind === 'axes' && p.x && p.y)
    .map((p) => ({key: p.key, label: p.label || p.key, x: p.x, y: p.y, xlog: !!p.xlog, ylog: !!p.ylog}));
  if (listed.length) return listed;
  const mags = (man.cols || []).filter((c) => c.role === 'mag');
  const order = mags.map((c, i) => ({c, i, w: wavelengthOf(c)}));
  order.sort((a, b) => (Number.isFinite(a.w) && Number.isFinite(b.w) ? a.w - b.w : a.i - b.i));
  const m = order.map((o) => o.c);
  const out = [];
  const lab = (c) => c.label || c.key;
  for (let k = 0; k + 1 < m.length && out.length < 4; k++) {
    const a = m[k], b = m[k + 1];
    out.push({key: `cmd_${a.key}_${b.key}`, label: `CMD ${lab(a)}−${lab(b)} vs ${lab(b)}`, x: `${a.key}-${b.key}`, y: b.key});
  }
  for (let k = 0; k + 2 < m.length && out.length < 7; k++) {
    const a = m[k], b = m[k + 1], c = m[k + 2];
    out.push({key: `ccd_${a.key}_${b.key}_${c.key}`, label: `CCD ${lab(b)}−${lab(c)} vs ${lab(a)}−${lab(b)}`, x: `${b.key}-${c.key}`, y: `${a.key}-${b.key}`});
  }
  return out;
}

// =============================================================== error bars and limits

/** Quadrature error of an axis expression from ColMeta.err columns; null when any base lacks one. */
export async function axisErrors(client, expr) {
  let lin;
  try {
    lin = client.col(expr) && !client.col(expr).virtual ? {w: {[expr]: 1}, keys: [expr]} : compileLinear(expr);
  } catch (err) {
    if (err instanceof SyntaxError) return null;
    throw err;
  }
  const parts = [];
  for (const k of lin.keys) {
    const m = client.col(k);
    if (!m || !m.err || !client.col(m.err)) return null;
    parts.push([lin.w[k], m.err]);
  }
  if (!parts.length) return null;
  const vals = await Promise.all(parts.map(([, e]) => client.values(e)));
  const n = vals[0].length;
  const out = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    let s = 0;
    for (let p = 0; p < parts.length; p++) {
      const v = vals[p][i] * parts[p][0];
      s += v * v;
    }
    out[i] = Math.sqrt(s);
  }
  return out;
}

/** Limit direction of an axis value from a record's _limits ('u' | 'l' | null). */
export function limitDir(client, expr, lim) {
  if (!lim) return null;
  let lin;
  try {
    lin = compileLinear(expr);
  } catch (err) {
    if (err instanceof SyntaxError) return null;
    throw err;
  }
  let dir = null;
  for (const k of lin.keys) {
    const code = lim[k];
    if (!code) continue;
    const m = client.col(k) || {};
    // ul on a flux = upper limit on the value; ul on a magnitude = the source is fainter
    let d = code === 1 ? 'u' : code === 2 ? 'l' : null;
    if (!d) continue;
    if (m.role === 'mag') d = d === 'u' ? 'l' : 'u';
    if (lin.w[k] < 0) d = d === 'u' ? 'l' : 'u';
    if (dir && dir !== d) return null;
    dir = d;
  }
  return dir;
}

// =============================================================== pane

export async function mount(host, ctx) {
  const {store, app, id} = ctx;
  const cfg = ctx.config || {};
  const ds = paneDataset(ctx);
  if (!ds) throw new Error('scatter pane needs a catalog dataset');
  const root = el('div', 'pk-scatter pk-plot');
  const bar = el('div', 'pk-plot-bar');
  const body = el('div', 'pk-plot-body');
  const foot = el('div', 'pk-plot-foot');
  root.append(bar, body, foot);
  host.append(root);
  const {stage, under, glc, over, note} = makeStage(body);
  over.setAttribute('aria-label', 'Scatter plot');
  const gridEl = el('div', 'pk-scatter-grid');
  gridEl.hidden = true;
  body.append(gridEl);

  const client = await app.catalog(ds);
  const n = client.n;
  const rt = app.runtime || {};
  let destroyed = false;
  let visible = true;
  const offs = [];
  // small multiples: an empty or invalid `grid` list means a single plot
  const gridSpecs = (Array.isArray(cfg.grid) ? cfg.grid : [])
    .map((c) => (Array.isArray(c) ? {x: c[0], y: c[1]} : {x: c && c.x, y: c && c.y}))
    .filter((c) => c.x && c.y);
  const gridOn = gridSpecs.length > 0;

  // ------------------------------------------------------------ state
  const S = {
    axes: normAxes(store.get(`axes:${id}`) || {}),
    xy: null,              // {key, x, y, gx, gy, origin, auto: {x, y}}
    xyLoading: null,
    vis: null, visVersion: -1,
    color: null,
    style: null,           // T0 / T1 per-point data {key, hollow, ex, ey, labels, limits, x2, y2}
    dom: null,             // {x: [d0, d1], y: [d0, d1]} in displayed units while zoomed locally
    shift: [0, 0],
    w: 0, h: 0, dpr: 1, glDpr: 1,
    tier: paneTier(ctx, n),
    view: null,
    interacting: false,
    settleTimer: null,
    preview: null,
    renderMs: 0,
    fGrid: null, fKey: '',
    ctxGrids: [], ctxKey: '',
    inView: null, inViewKey: '',
    prebinned: null,
    statKey: '', stat: null,  // chosen binned-statistic background {key, img}
    overlays: [], overlayDocs: null,
    focusCell: null,
    lastCount: 0,
  };
  let sel = null;
  let renderer = null;
  let frame = 0;

  function normAxes(a) {
    const o = {x: a.x ?? cfg.x ?? null, y: a.y ?? cfg.y ?? null, xlog: !!(a.xlog ?? cfg.xlog), ylog: !!(a.ylog ?? cfg.ylog),
      xinv: !!a.xinv, yinv: !!a.yinv, xr: Array.isArray(a.xr) ? a.xr.slice(0, 2) : null, yr: Array.isArray(a.yr) ? a.yr.slice(0, 2) : null};
    if (!o.x || !o.y) {
      const pre = axisPresets(client);
      if (pre.length) { o.x = o.x || pre[0].x; o.y = o.y || pre[0].y; }
    }
    return o;
  }

  const xInv = () => autoInvert(client, S.axes.x) !== !!S.axes.xinv;
  const yInv = () => autoInvert(client, S.axes.y) !== !!S.axes.yinv;
  const xyKey = () => `${S.axes.x}|${S.axes.y}|${S.axes.xlog ? 1 : 0}|${S.axes.ylog ? 1 : 0}`;
  const usesGL = () => S.tier === 'T1' || S.tier === 'T2';

  // ------------------------------------------------------------ toolbar and widgets
  const brushModes = brushToolbar((m) => {
    if (m !== 'pan') brush.setMode(m);
    for (const c of cellBrushes) c.setMode(m === 'pan' ? 'rect' : m);
    over.style.cursor = m === 'pan' ? 'grab' : 'crosshair';
  });
  const resetBtn = iconButton('⟲', 'Reset the view (double-click the plot)', 'pk-reset-view');
  resetBtn.addEventListener('click', resetView);
  const gridBtn = iconButton('▦', 'Back to the small multiples', 'pk-grid-back');
  gridBtn.hidden = true;
  gridBtn.addEventListener('click', () => leaveCell(true));
  const tools = ctx.tools || bar;
  tools.append(brushModes.el, resetBtn, gridBtn);

  const pickX = createAxisPicker({client, axis: 'x', label: 'x', value: pickerValue('x'), onChange: (p) => axisPatch('x', p)});
  const pickY = createAxisPicker({client, axis: 'y', label: 'y', value: pickerValue('y'), onChange: (p) => axisPatch('y', p)});
  bar.append(pickX.el, pickY.el);
  let presetSel = null;
  if (cfg.presets !== false) {
    const pre = axisPresets(client);
    if (pre.length) {
      presetSel = el('select', 'pk-preset-select');
      presetSel.setAttribute('aria-label', 'Axis presets');
      const o0 = el('option', null, 'Presets…');
      o0.value = '';
      presetSel.append(o0);
      for (const p of pre) {
        const o = el('option', null, p.label);
        o.value = p.key;
        presetSel.append(o);
      }
      presetSel.addEventListener('change', () => {
        const p = pre.find((q) => q.key === presetSel.value);
        presetSel.value = '';
        if (!p) return;
        setAxes({x: p.x, y: p.y, xlog: !!p.xlog, ylog: !!p.ylog, xinv: false, yinv: false, xr: null, yr: null});
      });
      bar.append(presetSel);
    }
  }
  const statSel = el('select', 'pk-preset-select pk-stat-select');
  statSel.setAttribute('aria-label', 'Binned-statistic background');
  statSel.title = 'Background: binned statistic of the manifest for these axes';
  statSel.hidden = true;
  statSel.addEventListener('change', () => { S.statKey = statSel.value; loadStat(); schedule(); });
  bar.append(statSel);
  const showAv = app.caps && app.caps.has('av_slider') && ((Array.isArray(cfg.overlays) && cfg.overlays.length) || !pageHasModelPane(ctx));
  const controls = createControls(bar, {app, store, client, pane: id, config: cfg,
    show: {template: false, features: false, av: !!showAv, rv: !!showAv}});
  const legend = createLegend(foot, {app, store, client, ds, pane: id, config: cfg, theme: ctx.theme,
    range: () => (S.color && S.color.kind === 'cont' ? [S.color.lo, S.color.hi] : null)});
  const chips = createChips(foot, {app, store, client, ds, pane: id, exclude: () => (S.color ? S.color.by : null)});

  function pickerValue(a) {
    const ax = S.axes;
    return a === 'x' ? {expr: ax.x, log: ax.xlog, inv: ax.xinv, range: ax.xr} : {expr: ax.y, log: ax.ylog, inv: ax.yinv, range: ax.yr};
  }

  function axisPatch(a, p) {
    const nx = {...S.axes};
    if ('expr' in p) { nx[a] = p.expr; nx[`${a}r`] = null; }
    if ('log' in p) { nx[`${a}log`] = !!p.log; nx[`${a}r`] = null; }
    if ('inv' in p) nx[`${a}inv`] = !!p.inv;
    if ('range' in p) nx[`${a}r`] = p.range;
    setAxes(nx);
  }

  function setAxes(nx) {
    const changedData = nx.x !== S.axes.x || nx.y !== S.axes.y || !!nx.xlog !== S.axes.xlog || !!nx.ylog !== S.axes.ylog;
    S.axes = normAxes(nx);
    S.dom = null;
    store.set({[`axes:${id}`]: {...S.axes}}, {source: id});
    pickX.set(pickerValue('x'));
    pickY.set(pickerValue('y'));
    if (changedData) reloadData();
    else schedule();
  }

  // ------------------------------------------------------------ GL
  function ensureRenderer() {
    if (renderer || !usesGL()) return renderer;
    if (rt.webgl2 === false) { lowerTier(); return null; }
    renderer = new XYRenderer(glc, {
      budget: gpuBudget(app), owner: id,
      onLost: () => { ctx.setStatus('WebGL context lost'); },
      onRestored: () => { ctx.setStatus(''); uploadGL(); schedule(); },
    });
    if (!renderer.ok) {
      renderer.destroy();
      renderer = null;
      lowerTier();
      return null;
    }
    if (S.w) renderer.resize(S.w, S.h, S.glDpr);
    uploadGL();
    return renderer;
  }

  function lowerTier() {
    const t = n <= T0_MAX ? 'T0' : 'F';
    if (S.tier !== t) {
      S.tier = t;
      setPaneTier(ctx, t);
    }
    if (renderer) { renderer.destroy(); renderer = null; }
    releaseGpu(app, id);
    glc.style.visibility = 'hidden';
    loadStyle();
    schedule();
  }

  function wantDensity() {
    if (!usesGL()) return false;
    if (rt.floatRT === false) return false;
    if (cfg.density === 'off') return false;
    if (cfg.density === 'on') return true;
    if (S.tier !== 'T2') return false;
    return !(S.inView !== null && S.inView < SEMANTIC_ZOOM_MARKERS);
  }

  function replan() {
    if (!usesGL() || !S.w) return;
    const dev = typeof window !== 'undefined' ? window.devicePixelRatio || 1 : 1;
    const p = planGpu(app, id, {cssW: S.w, cssH: S.h, density: S.tier === 'T2' && cfg.density !== 'off', vertexBytes: n * XY_VERTEX_BYTES}, dev);
    if (p.refused) {
      store.emit('toast', {level: 'info', text: `${ctx.config && ctx.config.title ? ctx.config.title : id}: density layer limit reached; showing a binned image`});
      lowerTier();
      return;
    }
    S.glDpr = Math.min(dev, p.dpr);
    if (renderer) renderer.resize(S.w, S.h, S.glDpr);
  }

  function uploadGL() {
    if (!renderer || !S.xy) return;
    const {gx, gy, origin} = S.xy;
    renderer.setXY(gx, gy, S.vis, {origin});
    uploadColor();
    if (sel) renderer.setSelection(sel.count ? sel.mask : null);
  }

  function uploadColor() {
    if (!renderer || !S.xy) return;
    const cs = S.color;
    if (cs && cs.kind === 'cat') { renderer.setCats([cs.codes]); renderer.setColorValues(null); }
    else if (cs && cs.kind === 'cont') { renderer.setCats([]); renderer.setColorValues(cs.values); }
    else { renderer.setCats([]); renderer.setColorValues(null); }
  }

  // ------------------------------------------------------------ data
  async function reloadData() {
    const key = xyKey();
    const ax = S.axes;
    S.shift = dmShift(client, ax.x, ax.y, ax.xlog, ax.ylog, Number(store.get('dm')) || 0);
    S.fGrid = null; S.fKey = ''; S.ctxKey = ''; S.inView = null; S.inViewKey = ''; S.style = null;
    loadPrebinned();
    schedule();
    if (!ax.x || !ax.y) { ctx.setStatus('choose x and y'); return; }
    ctx.setStatus('loading…');
    const p = (async () => {
      const [x, y] = await Promise.all([client.values(ax.x), client.values(ax.y)]);
      if (key !== xyKey() || destroyed) return;
      const gx = transformed(x, ax.xlog), gy = transformed(y, ax.ylog);
      const dr = drawableMask(gx, gy, S.vis, n);
      const kind = (log) => (log ? 'log' : 'lin');
      const q = n > T0_MAX ? [0.002, 0.998] : null;
      const ex = sampledExtent(x, {kind: kind(ax.xlog), q, mask: dr}) || (ax.xlog ? [1, 10] : [0, 1]);
      const ey = sampledExtent(y, {kind: kind(ax.ylog), q, mask: dr}) || (ax.ylog ? [1, 10] : [0, 1]);
      S.xy = {key, x, y, gx, gy, origin: [midOrigin(gx), midOrigin(gy)], auto: {x: ex, y: ey}};
      uploadGL();
      ctx.setStatus('');
      loadStyle();
      settle();
    })();
    S.xyLoading = p;
    try {
      await p;
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      ctx.setStatus('error', err.message);
      store.emit('toast', {level: 'error', text: `Scatter ${ax.x} / ${ax.y}: ${err.message}`});
    }
  }

  async function loadVis() {
    const v = store.get(`vis:${ds}`) || {};
    if (v.version === S.visVersion) return;
    S.visVersion = v.version;
    const r = await client.vis();
    if (destroyed) return;
    S.vis = r.vis || null;
    S.fKey = '';
    S.inViewKey = '';
    uploadGL();
    loadStyle();
    schedule();
    settle();
  }

  async function loadColorState() {
    const prev = S.color;
    try {
      S.color = await loadColor(client, store, cfg, ctx.theme, prev);
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      S.color = null;
      store.emit('toast', {level: 'warn', text: `Colour ${resolveColor(store, cfg, client).by}: ${err.message}`});
    }
    if (destroyed) return;
    uploadColor();
    legend.refresh();
    chips.refresh();
    drawGrid();
    schedule();
  }

  /** Binned-statistic grids of the manifest for the current axes (linear axes only). */
  function statGrids() {
    const ax = S.axes;
    if (ax.xlog || ax.ylog) return [];
    return (client.manifest.density || []).filter((g) => g && g.stat && g.stat !== 'count' && g.x === ax.x && g.y === ax.y);
  }

  function syncStatSelect() {
    const list = statGrids();
    statSel.textContent = '';
    const o0 = el('option', null, 'no background');
    o0.value = '';
    statSel.append(o0);
    for (const g of list) {
      const o = el('option', null, `${g.stat} ${g.value ? exprLabel(client, g.value) : ''}`.trim());
      o.value = g.key;
      statSel.append(o);
    }
    if (!list.some((g) => g.key === S.statKey)) S.statKey = '';
    statSel.value = S.statKey;
    statSel.hidden = !list.length;
    loadStat();
  }

  function loadStat() {
    const key = S.statKey;
    if (!key) { if (S.stat) { S.stat = null; schedule(); } return; }
    if (S.stat && S.stat.key === key) return;
    client.density(key).then((g) => {
      if (destroyed || S.statKey !== key || !g) return;
      S.stat = {key, img: statGridImage(g)};
      schedule();
    }, (err) => {
      if (err && err.name === 'AbortError') return;
      store.emit('toast', {level: 'warn', text: `Background grid ${key}: ${err.message}`});
    });
  }

  function drawStat(c, view) {
    if (!S.stat || !S.stat.img || S.stat.key !== S.statKey) return;
    const g = S.stat.img;
    const [dx, dy] = S.shift;
    drawDensityGrid(c, view, {...g, xr: [g.xr[0] + dx, g.xr[1] + dx], yr: [g.yr[0] + dy, g.yr[1] + dy]},
      {log: false, lo: 0, hi: 1, cmap: 'viridis', alpha: 0.75});
  }

  function loadPrebinned() {
    S.prebinned = null;
    syncStatSelect();
    const ax = S.axes;
    if (ax.xlog || ax.ylog) return;
    const d = (client.manifest.density || []).find((g) => g && g.x === ax.x && g.y === ax.y && !g.stat);
    if (!d) return;
    const key = xyKey();
    client.density(d.key).then((g) => {
      if (destroyed || key !== xyKey() || !g) return;
      S.prebinned = {counts: g.values, nx: g.nx, ny: g.ny, xr: [g.xr[0] + S.shift[0], g.xr[1] + S.shift[0]], yr: [g.yr[0] + S.shift[1], g.yr[1] + S.shift[1]]};
      schedule();
    }, (err) => {
      if (err && err.name === 'AbortError') return;
      console.warn(`density grid ${d.key} unavailable`, err);
    });
  }

  /** Per-point styling for T0 (and pair connectors for T0 / T1 up to SEMANTIC_ZOOM_MARKERS). */
  async function loadStyle() {
    const ax = S.axes;
    if (!S.xy) return;
    const key = `${xyKey()}|${S.tier}`;
    if (S.style && S.style.key === key) return;
    const st = {key, hollow: null, ex: null, ey: null, labels: null, limits: null, x2: null, y2: null};
    S.style = st;
    const jobs = [];
    const t0 = S.tier === 'T0';
    if (t0 && cfg.hollow_if) jobs.push(client.values(cfg.hollow_if).then((v) => { st.hollow = v; }));
    if (t0 && cfg.errorbars) {
      jobs.push(axisErrors(client, ax.x).then((v) => { st.ex = v; }));
      jobs.push(axisErrors(client, ax.y).then((v) => { st.ey = v; }));
    }
    if (t0 && cfg.labels) {
      jobs.push(client.rows(0, n).then((rows) => {
        st.labels = rows.map((r) => (r[cfg.labels] === undefined || r[cfg.labels] === null ? '' : String(r[cfg.labels])));
      }));
    }
    if (cfg.pairs && cfg.pairs.x2 && cfg.pairs.y2 && n <= SEMANTIC_ZOOM_MARKERS && (t0 || S.tier === 'T1')) {
      jobs.push(Promise.all([client.values(cfg.pairs.x2), client.values(cfg.pairs.y2)]).then(([a, b]) => { st.x2 = a; st.y2 = b; }));
    }
    const limKeys = new Set();
    for (const e of [ax.x, ax.y]) {
      let ks = [];
      try {
        ks = client.baseKeys(e);
      } catch (err) {
        if (!(err instanceof SyntaxError)) throw err;
      }
      for (const k of ks) if (client.col(k) && client.col(k).limits) limKeys.add(k);
    }
    if (t0 && limKeys.size) jobs.push(loadLimits(st));
    try {
      await Promise.all(jobs);
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      store.emit('toast', {level: 'warn', text: `Point styling unavailable: ${err.message}`});
    }
    if (!destroyed && S.style === st) schedule();
  }

  async function loadLimits(st) {
    const lims = new Array(n).fill(null);
    for (let a = 0; a < n; a += LIMIT_BATCH) {
      const b = Math.min(n, a + LIMIT_BATCH);
      const recs = await Promise.all(Array.from({length: b - a}, (_, k) => client.record(a + k)));
      if (destroyed || S.style !== st) return;
      recs.forEach((r, k) => { if (r && r._limits) lims[a + k] = r._limits; });
    }
    st.limits = lims;
  }

  async function loadOverlays() {
    const ids = Array.isArray(cfg.overlays) ? cfg.overlays : [];
    if (!ids.length) return;
    const [ov, iso, ext, filt] = await Promise.all(['overlays', 'isochrones', 'extinction', 'filters'].map((k) => docOf(ctx, k).catch((err) => {
      console.warn(`${k} document unavailable`, err);
      return null;
    })));
    if (destroyed) return;
    S.overlayDocs = {ov, iso, ext, filt};
    const r = resolveOverlays(ids, {overlays: ov, isochrones: iso, extinction: ext});
    S.overlays = r.layers;
    if (r.missing.length) console.warn(`scatter ${id}: unknown overlays ${r.missing.join(', ')}`);
    schedule();
  }

  // ------------------------------------------------------------ view
  function domain() {
    const ax = S.axes;
    const auto = S.xy ? S.xy.auto : null;
    const sx = ax.xlog ? 0 : S.shift[0], sy = ax.ylog ? 0 : S.shift[1];
    let x = S.dom ? S.dom.x : ax.xr;
    let y = S.dom ? S.dom.y : ax.yr;
    if (!x) x = auto ? [auto.x[0] + sx, auto.x[1] + sx] : (S.prebinned ? S.prebinned.xr : (ax.xlog ? [1, 10] : [0, 1]));
    if (!y) y = auto ? [auto.y[0] + sy, auto.y[1] + sy] : (S.prebinned ? S.prebinned.yr : (ax.ylog ? [1, 10] : [0, 1]));
    return {x, y};
  }

  function buildView(w = S.w, h = S.h) {
    const ax = S.axes;
    const d = domain();
    return makeView({
      width: w, height: h, dpr: S.dpr,
      x: {kind: ax.xlog ? 'log' : 'lin', d0: d.x[0], d1: d.x[1], invert: xInv(), expr: ax.x, label: axisTitle(client, ax.x, false)},
      y: {kind: ax.ylog ? 'log' : 'lin', d0: d.y[0], d1: d.y[1], invert: yInv(), expr: ax.y, label: axisTitle(client, ax.y, false)},
    });
  }

  function viewRef(view = S.view) {
    const ax = S.axes;
    return {pane: id, kind: 'xy', x: ax.x, y: ax.y, xlog: ax.xlog, ylog: ax.ylog,
      kx: view ? Math.abs(view.sx.b) : 1, ky: view ? Math.abs(view.sy.b) : 1};
  }

  // ------------------------------------------------------------ drawing
  function schedule() {
    if (frame || destroyed) return;
    frame = raf(() => { frame = 0; draw(); });
  }

  function draw() {
    if (destroyed || !visible || !(S.w > 1 && S.h > 1)) return;
    if (gridOn && S.focusCell === null) { drawGrid(); return; }
    const t0 = now();
    const theme = ctx.theme();
    const tk = tokensOf(theme);
    const view = S.view = buildView();
    brush.setView(view);
    const u = prepareCanvas(under, S.w, S.h, S.dpr);
    u.clearRect(0, 0, S.w, S.h);
    drawAxes(u, view, {tokens: tk, xLabel: view.x.label, yLabel: view.y.label});
    drawRasters(u, view);
    drawContext(u, view, tk);
    drawStat(u, view);
    let shown = 0;
    if (!S.xy) {
      if (S.prebinned) drawDensityGrid(u, view, S.prebinned, {tokens: tk, cmap: theme.densityCmap || 'magma'});
    } else if (S.tier === 'T0') {
      shown = drawT0(u, view, tk);
    } else if (S.tier === 'F') {
      shown = drawF(u, view, tk, theme);
    }
    if (usesGL() && S.xy && ensureRenderer()) {
      glc.style.visibility = '';
      clipToPlot(glc, view);
      const cam = foldCamera(cameraFromView(view), S.shift);
      const dens = wantDensity();
      const count = S.interacting && S.tier === 'T2' ? Math.min(n, MOTION_PREFIX) : n;
      const ok = renderer.render({
        camera: cam, mode: dens ? 'density' : 'markers', color: S.color ? S.color.gpu : {mode: 0},
        pointSize: dens ? 2.5 : n <= 20000 ? 5 : 3.5, selection: {show: true}, neutral: tk.neutral, selColor: tk.sel,
        count, alpha: n <= 20000 ? 0.85 : 0.6, densityCmap: theme.densityCmap,
      });
      if (!ok && renderer.stats().refused === 'targets') {
        lowerTier();
        return;
      }
      const st = renderer.stats();
      shown = st.count;
      ctx.setStatus(st.capped ? `showing ${fmtCount(st.count)} of ${fmtCount(n)}` : '');
    } else {
      glc.style.visibility = 'hidden';
    }
    S.lastCount = shown;
    drawOver(view, tk);
    stage.dataset.tier = S.tier;
    stage.dataset.mode = usesGL() ? (wantDensity() ? 'density' : 'markers') : S.tier === 'F' ? 'binned' : 'canvas';
    S.renderMs = now() - t0;
  }

  function drawOver(view, tk) {
    const o = prepareCanvas(over, S.w, S.h, S.dpr);
    o.clearRect(0, 0, S.w, S.h);
    drawOverlayLayers(o, view, tk);
    const fi = storeRow(store, 'focus', ds), hi = storeRow(store, 'hover', ds);
    if (S.xy) {
      const [dx, dy] = S.shift;
      if (hi >= 0 && hi !== fi) drawFocusRing(o, view, S.xy.x[hi] + dx, S.xy.y[hi] + dy, {hover: true, tokens: tk, r: 6});
      if (fi >= 0) drawFocusRing(o, view, S.xy.x[fi] + dx, S.xy.y[fi] + dy, {tokens: tk});
    }
    if (S.preview) drawBrushPreview(o, view, S.preview, tk);
  }

  function overlayState(x = S.axes.x, y = S.axes.y) {
    const docs = S.overlayDocs || {};
    const colmap = {};
    for (const c of client.manifest.cols || []) if (c.filter) colmap[c.key] = c.filter;
    return {av: Number(store.get('av')) || 0, rv: Number(store.get('rv')) || 3.1, dm: 0, filters: docs.filt || null, colmap,
      ext: docs.ext || null, law: (docs.ext && docs.ext.law) || undefined, x, y, isMag: magKeys(client)};
  }

  function runtimeLayers(tk) {
    const out = [];
    const st = S.style;
    if (st && st.x2 && st.y2 && S.xy) {
      const [dx, dy] = S.shift;
      const sh = (v, d) => (d ? Float32Array.from(v, (a) => a + d) : v);
      out.push({id: 'pairs', kind: 'pairs', x: sh(S.xy.x, dx), y: sh(S.xy.y, dy), x2: sh(st.x2, dx), y2: sh(st.y2, dy),
        mask: drawableMask(S.xy.gx, S.xy.gy, S.vis, n), style: {color: tk.fg3 || tk.neutral, width: 0.8, opacity: 0.6}});
    }
    return out;
  }

  function drawOverlayLayers(c, view, tk) {
    const layers = [...runtimeLayers(tk), ...S.overlays];
    if (!layers.length) return [];
    return drawOverlays(c, view, layers, overlayState());
  }

  /**
   * Raster layers for an axis pair. A raster with `x` / `y` is drawn only where the plot shows
   * that pair on linear axes; one with `cell: k` only in grid cell k (and its expanded view).
   * An unbound raster belongs to the single plot (it has no axis pair to match a grid cell).
   */
  function rasterApplies(r, spec, k) {
    if (r.x || r.y) return !!spec && r.x === spec.x && r.y === spec.y && !spec.xlog && !spec.ylog;
    if (Number.isInteger(r.cell)) return k === r.cell;
    return k === null;
  }

  function drawRasters(c, view, spec = S.axes, k = S.focusCell) {
    for (const r of rasters) {
      if (!rasterApplies(r, spec, k)) continue;
      if (!r.img || !r.img.complete || !r.img.naturalWidth) continue;
      const [x0, x1, y0, y1] = r.extent;
      const a = view.toPx(x0, y1), b = view.toPx(x1, y0);
      if (![...a, ...b].every(Number.isFinite)) continue;
      c.save();
      const P = view.plot;
      c.beginPath();
      c.rect(P.left, P.top, P.width, P.height);
      c.clip();
      c.globalAlpha = r.opacity;
      c.translate(a[0], a[1]);
      c.scale((b[0] - a[0]) / r.img.naturalWidth, (b[1] - a[1]) / r.img.naturalHeight);
      c.drawImage(r.img, 0, 0);
      c.restore();
    }
  }

  function drawContext(c, view, tk) {
    for (const g of S.ctxGrids) drawDensityGrid(c, view, g, {tokens: tk, cmap: 'gray_r', alpha: 0.45});
  }

  function pointsFor(view, tk, maxRows = Infinity) {
    const {x, y, gx, gy} = S.xy;
    const [dx, dy] = S.shift;
    const st = S.style || {};
    const cs = S.color;
    const selOn = sel && sel.count > 0;
    const xs = view.sx, ys = view.sy;
    const [a0, a1] = [Math.min(xs.d0, xs.d1), Math.max(xs.d0, xs.d1)];
    const [b0, b1] = [Math.min(ys.d0, ys.d1), Math.max(ys.d0, ys.d1)];
    const base = [], top = [];
    for (let i = 0; i < n; i++) {
      if (S.vis && !S.vis[i]) continue;
      if (!(gx[i] === gx[i]) || !(gy[i] === gy[i])) continue;
      const px = x[i] + dx, py = y[i] + dy;
      if (px < a0 || px > a1 || py < b0 || py > b1) continue;
      if (base.length + top.length >= maxRows) return null;
      const p = {x: px, y: py, color: rowColor(cs, i) || tk.neutral, size: n <= 500 ? 7 : 5};
      if (st.hollow) { const h = st.hollow[i]; p.hollow = h === h && h !== 0; }
      if (st.ex) p.ex = st.ex[i];
      if (st.ey) p.ey = st.ey[i];
      if (st.labels && st.labels[i]) p.label = st.labels[i];
      if (st.limits && st.limits[i]) {
        const ly = limitDir(client, S.axes.y, st.limits[i]);
        const lx = ly ? null : limitDir(client, S.axes.x, st.limits[i]);
        if (ly) { p.limit = ly; p.limitAxis = 'y'; }
        else if (lx) { p.limit = lx; p.limitAxis = 'x'; }
      }
      if (selOn && sel.mask[i]) { p.stroke = tk.sel; p.lineWidth = 1.75; p.size += 1; top.push(p); }
      else { if (selOn) p.alpha = 0.3; base.push(p); }
    }
    return base.concat(top);
  }

  function drawT0(c, view, tk) {
    const pts = pointsFor(view, tk);
    drawPoints(c, view, pts, {tokens: tk});
    return pts.length;
  }

  function drawF(c, view, tk, theme) {
    if (S.fGrid && S.fGrid.key === S.fKey) {
      drawDensityGrid(c, view, S.fGrid.grid, {tokens: tk, cmap: theme.densityCmap || 'magma'});
      if (S.fGrid.total <= T0_MAX) {
        const pts = pointsFor(view, tk, T0_MAX + 1);
        if (pts) { drawPoints(c, view, pts, {tokens: tk}); return pts.length; }
      }
      return S.fGrid.total;
    }
    if (S.prebinned) drawDensityGrid(c, view, S.prebinned, {tokens: tk, cmap: theme.densityCmap || 'magma'});
    return 0;
  }

  // ------------------------------------------------------------ settle work (worker bins)
  function settle() {
    if (S.settleTimer) clearTimeout(S.settleTimer);
    S.settleTimer = setTimeout(() => {
      S.settleTimer = null;
      S.interacting = false;
      settleNow().catch((err) => {
        if (err && err.name === 'AbortError') return;
        console.warn(`scatter ${id}: settle failed`, err);
      });
      schedule();
    }, S.interacting ? SETTLE_MS : 0);
  }

  async function settleNow() {
    if (!S.xy || !S.w) return;
    const view = S.view || buildView();
    const ax = S.axes;
    const xr = rawRange(view.sx, S.shift[0]), yr = rawRange(view.sy, S.shift[1]);
    const key = `${xyKey()}|${xr}|${yr}|${S.visVersion}`;
    const jobs = [];
    if (S.tier === 'F' && S.fKey !== key) {
      jobs.push(client.bin2d(viewRef(view), F_BINS, F_BINS, {mask: 'vis', xr, yr}).then((g) => {
        let total = 0;
        for (let i = 0; i < g.counts.length; i++) total += g.counts[i];
        S.fGrid = {key, grid: gridToData(g, ax.xlog, ax.ylog, S.shift), total};
        S.fKey = key;
      }));
    }
    if (S.tier === 'T2' && S.inViewKey !== key && cfg.density !== 'on') {
      jobs.push(client.bin2d(viewRef(view), 1, 1, {mask: 'vis', xr, yr}).then((g) => {
        S.inView = g.counts[0] || 0;
        S.inViewKey = key;
      }));
    }
    const ctxIds = contextDatasets(ctx, ds);
    const ckey = `${key}|${ctxIds.join(',')}`;
    if (ctxIds.length && S.ctxKey !== ckey) {
      jobs.push(Promise.all(ctxIds.map(async (cds) => {
        const bg = await app.catalog(cds);
        if (!resolvesIn(bg, [ax.x, ax.y])) return null;
        const g = await bg.bin2d({pane: `${id}:ctx`, kind: 'xy', x: ax.x, y: ax.y, xlog: ax.xlog, ylog: ax.ylog}, CONTEXT_BINS, CONTEXT_BINS, {mask: 'vis', xr, yr});
        return gridToData(g, ax.xlog, ax.ylog, S.shift);
      })).then((grids) => {
        S.ctxGrids = grids.filter(Boolean);
        S.ctxKey = ckey;
      }));
    }
    if (!jobs.length) return;
    await Promise.all(jobs);
    if (!destroyed) schedule();
  }

  // ------------------------------------------------------------ interaction
  const brush = attachBrush(over, () => S.view, {
    mode: 'rect', store, pane: id,
    enabled: () => brushModes.mode() !== 'pan' && !!S.xy,
    onPreview: (p) => { S.preview = p; if (S.view) drawOver(S.view, tokensOf(ctx.theme())); },
    onCommit: (shape, op) => { commitBrush(shiftShape(shape, -S.shift[0], -S.shift[1]), op).catch(brushError); },
    onClick: (px, py, e) => { pickAt(px, py, e, true); },
  });

  function brushError(err) {
    if (err && err.name === 'AbortError') return;
    store.emit('toast', {level: 'error', text: `Selection failed: ${err.message}`});
  }

  async function commitBrush(shape, op) {
    const ax = S.axes;
    const res = await client.brush(viewRef(), shape);
    const {kind, ...geom} = shape;
    return applyBrush(ctx, ds, res, shape, op, {op, kind, view: {ds, x: ax.x, y: ax.y, xlog: ax.xlog, ylog: ax.ylog}, shape: geom});
  }

  async function pickAt(px, py, e, click) {
    const view = S.view;
    if (!view || !S.xy || !view.inPlot(px, py)) {
      if (click) return;
      if (store.get('hover')) store.set({hover: null}, {source: id});
      return;
    }
    const [dx0, dy0] = view.toData(px, py);
    const i = await client.pick(viewRef(view), dx0 - S.shift[0], dy0 - S.shift[1], pickRadius(e));
    if (destroyed) return;
    if (click) {
      if (i >= 0) focusRow(ctx, ds, i);
      return;
    }
    const cur = store.get('hover');
    if (i >= 0 && !(cur && cur.ds === ds && cur.i === i)) store.set({hover: {ds, i}}, {source: id});
    else if (i < 0 && cur && cur.ds === ds) store.set({hover: null}, {source: id});
  }

  let hoverBusy = false, hoverNext = null, hoverT = 0;
  function onHoverMove(e) {
    if (brush.active() || panDrag || !S.view) return;
    hoverNext = e;
    if (hoverBusy) return;
    const wait = Math.max(0, HOVER_MS - (now() - hoverT));
    hoverBusy = true;
    setTimeout(() => {
      const ev = hoverNext;
      hoverNext = null;
      hoverT = now();
      const [px, py] = eventToViewPx(over, S.view, ev);
      pickAt(px, py, ev, false).catch((err) => {
        if (!(err && err.name === 'AbortError')) console.warn('pick failed', err);
      }).finally(() => { hoverBusy = false; if (hoverNext) onHoverMove(hoverNext); });
    }, wait);
  }
  over.addEventListener('pointermove', onHoverMove);
  over.addEventListener('pointerleave', () => {
    const cur = store.get('hover');
    if (cur && cur.ds === ds) store.set({hover: null}, {source: id});
  });

  // pan (pan mode) and wheel zoom
  let panDrag = null;
  over.addEventListener('pointerdown', (e) => {
    if (brushModes.mode() !== 'pan' || !S.view || e.button !== 0) return;
    const [px, py] = eventToViewPx(over, S.view, e);
    panDrag = {px, py, view: S.view, id: e.pointerId};
    over.setPointerCapture(e.pointerId);
    over.style.cursor = 'grabbing';
    e.preventDefault();
  });
  over.addEventListener('pointermove', (e) => {
    if (!panDrag) return;
    const [px, py] = eventToViewPx(over, panDrag.view, e);
    const v = panDrag.view;
    S.dom = {x: panDomain(v.sx, px - panDrag.px), y: panDomain(v.sy, py - panDrag.py)};
    S.interacting = true;
    schedule();
  });
  const endPan = () => {
    if (!panDrag) return;
    panDrag = null;
    over.style.cursor = 'grab';
    commitDomain();
  };
  over.addEventListener('pointerup', endPan);
  over.addEventListener('pointercancel', endPan);
  over.addEventListener('wheel', (e) => {
    if (!S.view) return;
    const [px, py] = eventToViewPx(over, S.view, e);
    if (!S.view.inPlot(px, py)) return;
    e.preventDefault();
    const f = Math.pow(WHEEL_STEP, -e.deltaY * (e.deltaMode === 1 ? 16 : 1));
    const v = S.view;
    S.dom = {x: e.altKey ? domain().x : zoomDomain(v.sx, px, f), y: e.shiftKey ? domain().y : zoomDomain(v.sy, py, f)};
    S.interacting = true;
    schedule();
    commitDomain();
  }, {passive: false});
  over.addEventListener('dblclick', (e) => {
    if (brush.active()) return;
    e.preventDefault();
    resetView();
  });

  let domTimer = null;
  function commitDomain() {
    settle();
    if (domTimer) clearTimeout(domTimer);
    domTimer = setTimeout(() => {
      domTimer = null;
      if (!S.dom) return;
      S.axes = {...S.axes, xr: S.dom.x.slice(), yr: S.dom.y.slice()};
      store.set({[`axes:${id}`]: {...S.axes}}, {source: id});
      pickX.set(pickerValue('x'));
      pickY.set(pickerValue('y'));
    }, SETTLE_MS * 2);
  }

  function resetView() {
    S.dom = null;
    S.axes = {...S.axes, xr: null, yr: null};
    store.set({[`axes:${id}`]: {...S.axes}}, {source: id});
    pickX.set(pickerValue('x'));
    pickY.set(pickerValue('y'));
    settle();
    schedule();
  }

  // ------------------------------------------------------------ raster layers
  const rasters = [];
  for (const r of Array.isArray(cfg.raster) ? cfg.raster : []) {
    if (!r || !r.url || !Array.isArray(r.extent) || r.extent.length !== 4) continue;
    const img = new Image();
    img.decoding = 'async';
    const entry = {img, extent: r.extent.map(Number), opacity: Number.isFinite(Number(r.opacity)) ? Number(r.opacity) : 1,
      x: typeof r.x === 'string' ? r.x : null, y: typeof r.y === 'string' ? r.y : null,
      cell: Number.isInteger(r.cell) ? r.cell : null};
    img.addEventListener('load', () => { schedule(); if (gridOn && S.focusCell === null) drawGrid(); });
    img.addEventListener('error', () => console.warn(`raster layer ${r.url} failed to load`));
    img.src = ctx.hub && typeof ctx.hub.resolve === 'function' ? ctx.hub.resolve(r.url).toString() : r.url;
    rasters.push(entry);
  }

  // ------------------------------------------------------------ small multiples
  // Each cell has a base canvas (axes, raster layers bound to the cell's axis pair, context
  // density, points or worker density, overlays) and a ring canvas on top (focus / hover rings
  // and the brush preview), so hover traffic redraws only the rings. Worker bin2d results are
  // cached per cell and keyed by the vis version and the cell ranges.
  const cells = [];
  const cellBrushes = [];
  const cellData = new Map();   // expr pair key -> Promise<{x, y, auto}>
  let gridDrawn = false;
  let gridHollow = null;        // Promise<values> of cfg.hollow_if for T0 grid cells

  function buildGrid() {
    gridSpecs.forEach((sp, k) => {
      const cell = el('div', 'pk-grid-cell');
      const cv = el('canvas', 'pk-grid-canvas');
      cv.setAttribute('role', 'img');
      cv.setAttribute('aria-label', `${exprLabel(client, sp.y)} against ${exprLabel(client, sp.x)}`);
      const rc = el('canvas', 'pk-grid-rings');
      rc.setAttribute('aria-hidden', 'true');
      const exp = iconButton('⤢', 'Expand this panel', 'pk-grid-expand');
      exp.addEventListener('click', () => enterCell(k));
      cell.append(cv, rc, exp);
      gridEl.append(cell);
      const c = {k, spec: sp, cell, cv, rc, view: null, w: 0, h: 0, data: null, dens: null, ctx: null, preview: null};
      cells.push(c);
      const b = attachBrush(cv, () => c.view, {
        mode: brushModes.mode() === 'pan' ? 'rect' : brushModes.mode(), store, pane: `${id}:g${k}`,
        onPreview: (pv) => { c.preview = pv; drawCellRings(c, tokensOf(ctx.theme())); },
        onCommit: (shape, op) => {
          const vr = {pane: `${id}:g${k}`, kind: 'xy', x: sp.x, y: sp.y, xlog: false, ylog: false};
          client.brush(vr, shape).then((res) => {
            const {kind, ...geom} = shape;
            return applyBrush(ctx, ds, res, shape, op, {op, kind, view: {ds, x: sp.x, y: sp.y, xlog: false, ylog: false}, shape: geom});
          }).catch(brushError);
        },
        onClick: (px, py, e) => {
          if (!c.view) return;
          const [qx, qy] = c.view.toData(px, py);
          client.pick({pane: `${id}:g${k}`, kind: 'xy', x: sp.x, y: sp.y, xlog: false, ylog: false, kx: Math.abs(c.view.sx.b), ky: Math.abs(c.view.sy.b)}, qx, qy, pickRadius(e))
            .then((i) => focusRow(ctx, ds, i), brushError);
        },
      });
      cellBrushes.push(b);
    });
  }

  async function cellValues(sp) {
    const key = `${sp.x}|${sp.y}`;
    if (!cellData.has(key)) {
      const p = Promise.all([client.values(sp.x), client.values(sp.y)]).then(([x, y]) => {
        const dr = drawableMask(x, y, null, n);
        const q = n > T0_MAX ? [0.002, 0.998] : null;
        return {x, y, auto: {x: sampledExtent(x, {q, mask: dr}) || [0, 1], y: sampledExtent(y, {q, mask: dr}) || [0, 1]}};
      });
      cellData.set(key, p);
      p.catch(() => cellData.delete(key));
    }
    return cellData.get(key);
  }

  const cellRange = (view) => [[Math.min(view.sx.d0, view.sx.d1), Math.max(view.sx.d0, view.sx.d1)],
    [Math.min(view.sy.d0, view.sy.d1), Math.max(view.sy.d0, view.sy.d1)]];

  /** Worker density of the cell (cached per vis version and range). */
  async function cellDensity(c, view) {
    const [xr, yr] = cellRange(view);
    const key = `${S.visVersion}|${xr}|${yr}`;
    if (c.dens && c.dens.key === key) return c.dens.grid;
    const vr = {pane: `${id}:g${c.k}`, kind: 'xy', x: c.spec.x, y: c.spec.y, xlog: false, ylog: false};
    const g = await client.bin2d(vr, GRID_BINS, GRID_BINS, {mask: 'vis', xr, yr});
    c.dens = {key, grid: gridToData(g, false, false)};
    return c.dens.grid;
  }

  /** Context density of the cell (page context catalogs / `background`; cached per range). */
  async function cellContext(c, view) {
    const ids = contextDatasets(ctx, ds);
    if (!ids.length) return [];
    const [xr, yr] = cellRange(view);
    const key = `${ids.join(',')}|${xr}|${yr}`;
    if (c.ctx && c.ctx.key === key) return c.ctx.grids;
    const grids = await Promise.all(ids.map(async (cds) => {
      const bg = await app.catalog(cds);
      if (!resolvesIn(bg, [c.spec.x, c.spec.y])) return null;
      const g = await bg.bin2d({pane: `${id}:g${c.k}:ctx`, kind: 'xy', x: c.spec.x, y: c.spec.y, xlog: false, ylog: false},
        CONTEXT_BINS, CONTEXT_BINS, {mask: 'vis', xr, yr});
      return gridToData(g, false, false);
    }));
    c.ctx = {key, grids: grids.filter(Boolean)};
    return c.ctx.grids;
  }

  function drawCellRings(c, tk) {
    if (!c.view || !c.data) return;
    const g = prepareCanvas(c.rc, c.w, c.h, S.dpr);
    g.clearRect(0, 0, c.w, c.h);
    const fi = storeRow(store, 'focus', ds), hi = storeRow(store, 'hover', ds);
    const {x, y} = c.data;
    if (hi >= 0 && hi !== fi) drawFocusRing(g, c.view, x[hi], y[hi], {hover: true, tokens: tk, r: 5});
    if (fi >= 0) drawFocusRing(g, c.view, x[fi], y[fi], {tokens: tk, r: 6});
    if (c.preview) drawBrushPreview(g, c.view, c.preview, tk);
  }

  function drawGridRings() {
    if (!gridOn || S.focusCell !== null || destroyed || !visible) return;
    const tk = tokensOf(ctx.theme());
    for (const c of cells) drawCellRings(c, tk);
  }

  let gridSeq = 0;
  async function drawGrid() {
    if (!gridOn || S.focusCell !== null || destroyed || !visible) return;
    const my = ++gridSeq;
    const theme = ctx.theme();
    const tk = tokensOf(theme);
    let hollow = null;
    if (cfg.hollow_if && n <= T0_MAX) {
      if (!gridHollow) {
        gridHollow = client.values(cfg.hollow_if);
        gridHollow.catch(() => { gridHollow = null; });
      }
      try {
        hollow = await gridHollow;
      } catch (err) {
        if (err && err.name === 'AbortError') return;
        store.emit('toast', {level: 'warn', text: `Hollow-marker rule ${cfg.hollow_if}: ${err.message}`});
      }
    }
    for (const c of cells) {
      const r = c.cell.getBoundingClientRect();
      const w = Math.max(40, Math.floor(r.width)), h = Math.max(40, Math.floor(r.height));
      let d;
      try {
        d = await cellValues(c.spec);
      } catch (err) {
        if (err && err.name === 'AbortError') return;
        c.cell.dataset.error = err.message;
        continue;
      }
      if (my !== gridSeq || destroyed) return;
      delete c.cell.dataset.error;
      const view = makeView({width: w, height: h, dpr: S.dpr, margin: {l: 40, r: 8, t: 8, b: 30},
        x: {kind: 'lin', d0: d.auto.x[0], d1: d.auto.x[1], invert: autoInvert(client, c.spec.x), label: exprLabel(client, c.spec.x)},
        y: {kind: 'lin', d0: d.auto.y[0], d1: d.auto.y[1], invert: autoInvert(client, c.spec.y), label: exprLabel(client, c.spec.y)}});
      // worker results first, so the canvas is painted in one go
      let ctxGrids = [], dens = null;
      try {
        [ctxGrids, dens] = await Promise.all([
          cellContext(c, view).catch((err) => {
            if (err && err.name === 'AbortError') throw err;
            console.warn(`scatter ${id}: context density of cell ${c.k} failed`, err);
            return [];
          }),
          n > T0_MAX ? cellDensity(c, view) : Promise.resolve(null),
        ]);
      } catch (err) {
        if (err && err.name === 'AbortError') return;
        c.cell.dataset.error = err.message;
        continue;
      }
      if (my !== gridSeq || destroyed) return;
      c.view = view; c.w = w; c.h = h; c.data = d;
      const g = prepareCanvas(c.cv, w, h, S.dpr);
      g.clearRect(0, 0, w, h);
      drawAxes(g, view, {tokens: tk, xLabel: view.x.label, yLabel: view.y.label, fontSize: 10});
      drawRasters(g, view, c.spec, c.k);
      for (const cg of ctxGrids) drawDensityGrid(g, view, cg, {tokens: tk, cmap: 'gray_r', alpha: 0.45});
      const selOn = sel && sel.count > 0;
      if (n <= T0_MAX) {
        const pts = [];
        const top = [];
        for (let i = 0; i < n; i++) {
          if (S.vis && !S.vis[i]) continue;
          const p = {x: d.x[i], y: d.y[i], color: rowColor(S.color, i) || tk.neutral, size: 4};
          if (hollow) { const hv = hollow[i]; p.hollow = hv === hv && hv !== 0; }
          if (selOn && sel.mask[i]) { p.stroke = tk.sel; p.lineWidth = 1.5; top.push(p); } else { if (selOn) p.alpha = 0.3; pts.push(p); }
        }
        drawPoints(g, view, pts.concat(top), {tokens: tk});
      } else {
        drawDensityGrid(g, view, dens, {tokens: tk, cmap: theme.densityCmap || 'magma'});
        if (selOn && sel.count <= SEMANTIC_ZOOM_MARKERS) {
          const pts = [];
          for (let i = 0; i < n; i++) if (sel.mask[i]) pts.push({x: d.x[i], y: d.y[i], color: tk.sel, size: 3});
          drawPoints(g, view, pts, {tokens: tk});
        }
      }
      if (S.overlays.length) drawOverlays(g, view, S.overlays, overlayState(c.spec.x, c.spec.y));
      drawCellRings(c, tk);
    }
    gridDrawn = true;
  }

  function enterCell(k) {
    const sp = gridSpecs[k];
    if (!sp) return;
    S.focusCell = k;
    gridEl.hidden = true;
    stage.hidden = false;
    gridBtn.hidden = false;
    setAxes({...S.axes, x: sp.x, y: sp.y, xlog: false, ylog: false, xr: null, yr: null});
    if (ctx.layout && typeof ctx.layout.popout === 'function') ctx.layout.popout(id, 'half');
    measure();
  }

  function leaveCell(closePop) {
    if (S.focusCell === null) return;
    S.focusCell = null;
    gridEl.hidden = false;
    stage.hidden = true;
    gridBtn.hidden = true;
    const lay = store.get('layout') || {};
    if (closePop && lay.popout && lay.popout[id] && ctx.layout) ctx.layout.popout(id, null);
    drawGrid();
  }

  if (gridOn) {
    buildGrid();
    gridEl.hidden = false;
    stage.hidden = true;
    bar.classList.add('pk-grid-mode');
  }

  // ------------------------------------------------------------ store wiring
  offs.push(store.on([`axes:${id}`], (changed, s, sources) => {
    if (sources.size === 1 && sources.has(id)) return;
    const a = s.get(`axes:${id}`) || {};
    const prevKey = xyKey();
    S.axes = normAxes(a);
    S.dom = null;
    pickX.set(pickerValue('x'));
    pickY.set(pickerValue('y'));
    if (xyKey() !== prevKey) reloadData();
    else { settle(); schedule(); }
  }));
  offs.push(store.on([`vis:${ds}`], () => { loadVis().catch(warnErr('filter mask')); }));
  offs.push(store.on(['color'], () => { loadColorState(); }));
  offs.push(store.on(['focus', 'hover'], () => { if (S.view) drawOver(S.view, tokensOf(ctx.theme())); if (gridOn && S.focusCell === null) drawGridRings(); }));
  offs.push(store.on(['av', 'rv'], () => schedule()));
  offs.push(store.on(['dm'], () => {
    const ax = S.axes;
    const old = S.shift;
    S.shift = dmShift(client, ax.x, ax.y, ax.xlog, ax.ylog, Number(store.get('dm')) || 0);
    if (S.dom) S.dom = {x: S.dom.x.map((v) => v + S.shift[0] - old[0]), y: S.dom.y.map((v) => v + S.shift[1] - old[1])};
    S.fKey = ''; S.ctxKey = ''; S.inViewKey = '';
    loadPrebinned();
    settle();
    schedule();
  }));
  offs.push(store.on(['theme'], () => { loadColorState(); }));
  offs.push(store.onEvent('theme:change', () => { loadColorState(); }));
  offs.push(store.on([`tier:${id}`], (changed, s, sources) => {
    if (sources.size === 1 && sources.has(id)) return;
    const t = paneTier(ctx, n);
    if (t === S.tier) return;
    S.tier = t;
    if (!usesGL() && renderer) { renderer.destroy(); renderer = null; releaseGpu(app, id); }
    replan();
    uploadGL();
    loadStyle();
    settle();
    schedule();
  }));
  offs.push(store.on(['layout'], () => {
    const lay = store.get('layout') || {};
    if (S.focusCell !== null && !(lay.popout && lay.popout[id])) leaveCell(false);
  }));
  offs.push(store.onEvent('selection:invert', (p) => {
    if (!p || p.handled || (p.ds && p.ds !== ds) || !S.xy || !sel) return;
    p.handled = true;
    sel.invert(drawableMask(S.xy.gx, S.xy.gy, S.vis, n));
  }));

  function warnErr(what) {
    return (err) => {
      if (err && err.name === 'AbortError') return;
      store.emit('toast', {level: 'warn', text: `${what}: ${err.message}`});
    };
  }

  // selection
  sel = await app.selectionFor(ds);
  offs.push(sel.onChange(() => {
    if (renderer) renderer.setSelection(sel.count ? sel.mask : null);
    if (gridOn && S.focusCell === null) drawGrid();
    schedule();
  }));

  // ------------------------------------------------------------ sizing
  function measure() {
    const target = gridOn && S.focusCell === null ? gridEl : stage;
    const r = target.getBoundingClientRect();
    const w = Math.floor(r.width), h = Math.floor(r.height);
    const dpr = typeof window !== 'undefined' ? window.devicePixelRatio || 1 : 1;
    if (w === S.w && h === S.h && dpr === S.dpr) return;
    S.w = w; S.h = h; S.dpr = dpr;
    replan();
    settle();
    if (gridOn && S.focusCell === null) drawGrid();
    schedule();
  }
  const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(() => measure()) : null;
  if (ro) { ro.observe(stage); ro.observe(gridEl); }

  // ------------------------------------------------------------ start
  if (S.tier === 'T0' || S.tier === 'F') glc.style.visibility = 'hidden';
  if (!store.get(`tier:${id}`)) setPaneTier(ctx, S.tier);
  S.shift = dmShift(client, S.axes.x, S.axes.y, S.axes.xlog, S.axes.ylog, Number(store.get('dm')) || 0);
  loadPrebinned();
  measure();
  await loadVis().catch(warnErr('filter mask'));
  loadColorState();
  loadOverlays().catch(warnErr('overlays'));
  reloadData();

  // ------------------------------------------------------------ handle
  return {
    resize() { measure(); },
    onVisible(v) {
      visible = !!v;
      if (visible) { measure(); schedule(); if (gridOn && S.focusCell === null) drawGrid(); }
    },
    destroy() {
      destroyed = true;
      if (frame) caf(frame);
      if (S.settleTimer) clearTimeout(S.settleTimer);
      if (domTimer) clearTimeout(domTimer);
      for (const off of offs) if (typeof off === 'function') off();
      brush();
      for (const b of cellBrushes) b();
      if (ro) ro.disconnect();
      if (renderer) renderer.destroy();
      releaseGpu(app, id);
      pickX.destroy();
      pickY.destroy();
      controls.destroy();
      legend.destroy();
      chips.destroy();
      brushModes.el.remove();
      resetBtn.remove();
      gridBtn.remove();
      root.remove();
    },
    async snapshot() {
      if (gridOn && S.focusCell === null) {
        const c0 = cells.find((c) => c.view);
        if (!c0) return null;
        return snapshotCanvases([c0.cv, c0.rc], c0.view.width, c0.view.height, tokensOf(ctx.theme()).bg);
      }
      const view = S.view || buildView();
      const tk = tokensOf(ctx.theme());
      if (S.tier === 'T0' && S.xy) {
        const svg = toSVG(view, [
          {kind: 'axes', tokens: tk, xLabel: view.x.label, yLabel: view.y.label},
          {kind: 'points', pts: pointsFor(view, tk), opts: {tokens: tk}},
          {kind: 'draw', fn: (c, v) => drawOverlayLayers(c, v, tk)},
        ]);
        return new Blob([svg], {type: 'image/svg+xml'});
      }
      draw();
      return snapshotCanvases([under, glc, over], S.w, S.h, tk.bg);
    },
    stats() {
      const gl = renderer ? renderer.stats() : null;
      return {renderMs: S.renderMs, tier: S.tier, mode: stage.dataset.mode || null, n, shown: S.lastCount,
        selected: sel ? sel.count : 0, inView: S.inView, gl, grid: !!(gridOn && S.focusCell === null), gridDrawn,
        overlays: S.overlays.length, stat: S.stat ? S.stat.key : null,
        cells: cells.map((c) => ({k: c.k, drawn: !!c.view, density: !!c.dens, context: c.ctx ? c.ctx.grids.length : 0}))};
    },
    async brush(shape, op = 'replace') {
      if (S.xyLoading) await S.xyLoading;
      return commitBrush(shape, op);
    },
  };
}
