// Manifold pane (ARCHITECTURE.md sections 6.9, 7.1, 7.3, 7.4; capability manifold_embedding).
//
// A 2-D linear projection of D standardized dims ((expr - center) / scale), driven by a
// Tour (math/tour.js): presets, drift (space toggles), basis-pair stepping, per-axis play and
// compass drags (math/compass.js). The frame is folded onto stored base columns
// (math/fold.js) so virtual dims (colours) cost nothing extra and the distance modulus enters
// the constant K on magnitude bases. Rendering paths, by tier and folded support:
//   T2, support <= MAXD_GPU   vendor GC Renderer: every base column on the GPU; tours run
//                             entirely on the GPU, the frame is the folded weights (padded
//                             with zero-weight colour dims) and the camera is foldCamera(cam, K)
//   T2, support > MAXD_GPU    XYRenderer density over worker projections (DataClient.project)
//                             on settle; a main-thread LOD prefix while the frame moves.
//                             Semantic zoom: markers when < SEMANTIC_ZOOM_MARKERS rows are in
//                             view (worker bin2d count of the settled projection)
//   T1                        XYRenderer markers on the worker projection; while moving, a
//                             main-thread projection of at most MOTION_CPU_ROWS prefix rows
//   T0                        canvas2d points with per-point colour / selection styling
//   F                         worker bin2d of the settled projection; prefix dots while moving
// Pre-binned first paint: when the frame is a dim pair matching a manifest density grid, the
// grid is drawn (affinely mapped into projected units) until the first projection arrives.
// Missing data follows the GC semantics: a missing base contributes 0 and multiplies the
// point's visibility by clamp(1 - |w| / FADE_WIDTH), so it fades out as its weight grows.
// After every settle the worker projection is stored under a token so picks and brushes use
// {kind:'proj'} ViewRefs. Selections from this pane carry an ids RecipeOp when the catalog
// is larger than BITS_IN_HASH_MAX_N and at most IDS_IN_HASH_MAX rows are hit
// (IMPLEMENTATION_NOTES 35); otherwise the hash stores the selection bits.
//
// Store: frame:<pane> {preset?, F (2·D numbers), dims} written when motion settles (not
// during drift), color, dm, speed, vis:<ds>, sel:<ds>, focus, hover, tier:<pane>. A `preset`
// names 'pca', `pair:i,j`, a manifest frame preset or an `axes` preset (unit weights on its x / y
// dims; IMPLEMENTATION_NOTES 91). Without a stored frame the pane opens on options.frame, else
// the manifest's manifold.default_frame (when it names a preset this pane knows), else 'pca'
// (initialPreset). `speed` (hash speed=, legacy ?speed=) scales the tour (Tour.setSpeed; default
// options.speed, else 1).
// Events (IMPLEMENTATION_NOTES 71): listens for `narrative:highlight` {pane, ds, ids, beat,
// goto?} addressed to this pane id, to 'manifold' or to no pane, and rings those rows in the
// current projection (`goto` re-centres the view when some are out of view); emits
// `narrative:sync` {pane} after mount. GPU memory is accounted in the page's app.gpuBudget
// when boot provides one (note 68).

import {
  MAXD_GPU, SEMANTIC_ZOOM_MARKERS, BITS_IN_HASH_MAX_N, MOTION_PREFIX, T0_MAX,
} from '../core/constants.js';
import {makeView, cameraFromView, zoomDomain, panDomain} from '../plot/axes.js';
import {prepareCanvas, tokensOf, drawAxes, drawPoints, drawFocusRing, drawDensityGrid, toSVG} from '../plot/canvas2d.js';
import {attachBrush, drawBrushPreview, eventToViewPx} from '../plot/brushui.js';
import {resolveOverlays, drawOverlays} from '../plot/overlays.js';
import {XYRenderer} from '../gl/xyrenderer.js';
import {gcVertexBytes, XY_VERTEX_BYTES} from '../gl/budget.js';
import {Renderer as GCRenderer} from '../vendor/gc/gl/renderer.js';
import {basisFrame, orthonormalize, axisInfo, axisLabel} from '../vendor/gc/math/frame.js';
import {fold, dimExpr, foldCamera, foldFade, projectRows} from '../math/fold.js';
import {Tour} from '../math/tour.js';
import {drawCompass, attachCompass} from '../math/compass.js';
import {fmtCount} from '../core/fmt.js';
import {exprLabel} from '../ui/axis_picker.js';
import {createLegend} from '../ui/legend.js';
import {createChips} from '../ui/chips.js';
import {firstPaintLatch} from '../layout/layout.js';
import {createControls} from '../ui/controls.js';
import {
  el, iconButton, paneDataset, docOf, gpuBudget, planGpu, requestGpu, releaseGpu, paneTier, setPaneTier, makeStage,
  brushToolbar, snapshotCanvases, clipToPlot, magKeys, loadColor, rowColor, pickRadius, applyBrush, idsRecipe, focusRow,
  storeRow, contextDatasets, resolvesIn, sampledExtent, WHEEL_STEP, HOVER_MS,
  narrativeHighlight, highlightCentre, recentreDomain,
} from './scatter.js';

export const meta = {
  type: 'manifold',
  caps: ['manifold_embedding', 'class_coloring', 'density_rendering', 'brush_rect', 'brush_circle', 'brush_lasso',
    'brush_set_ops', 'linked_selection', 'filter_toggles', 'selectable_axes', 'av_slider', 'extinction_vectors_on_ccd',
    'isochrones_or_tracks'],
  needs: ['catalog'],
};

export const PCA_SAMPLE = 20000;
export const MOTION_CPU_ROWS = SEMANTIC_ZOOM_MARKERS;
export const F_BINS = 384;
export const TRAIL_FRAMES = 6;
export const MAX_AUTO_DIMS = 16;
export const CONTEXT_BINS = 256;

const now = () => performance.now();

// =============================================================== pure helpers

/** Dims of a manifold pane: option list, "group:<g>", manifest manifold.dims, else mags. */
export function resolveDims(client, spec) {
  const man = client.manifest || {};
  let keys = null;
  if (typeof spec === 'string' && spec.startsWith('group:')) {
    const g = (man.groups || []).find((q) => q.key === spec.slice(6));
    keys = g && Array.isArray(g.dims) ? g.dims.slice() : null;
  } else if (Array.isArray(spec) && spec.length) {
    keys = spec.slice();
  }
  if (!keys && man.manifold && Array.isArray(man.manifold.dims)) keys = man.manifold.dims.slice();
  if (!keys) keys = (man.cols || []).filter((c) => c.role === 'mag' || c.role === 'color').map((c) => c.key).slice(0, MAX_AUTO_DIMS);
  return [...new Set(keys)].filter((k) => resolvesIn(client, [k]));
}

/** Median of a sorted array. */
function sortedMedian(a) {
  const m = a.length >> 1;
  return a.length % 2 ? a[m] : 0.5 * (a[m - 1] + a[m]);
}

/**
 * Robust centre / scale of a sample, the GC dim_stats rule of ColMeta (ARCHITECTURE.md 5.2):
 * centre = median, scale = 1.4826·MAD, std as fallback, 1 when both are 0.
 */
export function robustStats(v) {
  const a = [];
  const step = Math.max(1, Math.floor(v.length / 50000));
  for (let i = 0; i < v.length; i += step) if (Number.isFinite(v[i])) a.push(v[i]);
  if (!a.length) return {center: 0, scale: 1};
  a.sort((p, q) => p - q);
  const center = sortedMedian(a);
  const dev = a.map((x) => Math.abs(x - center)).sort((p, q) => p - q);
  let scale = 1.4826 * sortedMedian(dev);
  if (!(scale > 0)) {
    let s = 0;
    for (const x of a) s += (x - center) ** 2;
    scale = Math.sqrt(s / a.length);
  }
  return {center, scale: scale > 0 ? scale : 1};
}

/**
 * Top-2 principal components of standardized dims (power iteration on the covariance of a
 * row sample with every dim finite). cols: Float32Array per dim (raw values). Returns a
 * vendor-layout Float64Array(2D) or null when fewer than D + 1 complete rows exist.
 */
export function pcaFrame(cols, dims) {
  const D = dims.length;
  const n = cols[0] ? cols[0].length : 0;
  const rows = [];
  const step = Math.max(1, Math.floor(n / PCA_SAMPLE));
  for (let i = 0; i < n; i += step) {
    const r = new Float64Array(D);
    let ok = true;
    for (let d = 0; d < D; d++) {
      const v = cols[d][i];
      if (!Number.isFinite(v)) { ok = false; break; }
      r[d] = (v - dims[d].center) / dims[d].scale;
    }
    if (ok) rows.push(r);
  }
  if (rows.length <= D) return null;
  const mean = new Float64Array(D);
  for (const r of rows) for (let d = 0; d < D; d++) mean[d] += r[d] / rows.length;
  const C = new Float64Array(D * D);
  for (const r of rows) {
    for (let a = 0; a < D; a++) {
      const da = r[a] - mean[a];
      for (let b = a; b < D; b++) C[a * D + b] += da * (r[b] - mean[b]);
    }
  }
  for (let a = 0; a < D; a++) for (let b = a; b < D; b++) C[b * D + a] = C[a * D + b];
  const power = (deflate) => {
    let v = new Float64Array(D).map((_, i) => 1 / Math.sqrt(D) + 1e-3 * i);
    for (let it = 0; it < 200; it++) {
      const w = new Float64Array(D);
      for (let a = 0; a < D; a++) {
        let s = 0;
        for (let b = 0; b < D; b++) s += C[a * D + b] * v[b];
        w[a] = s;
      }
      if (deflate) {
        let p = 0;
        for (let a = 0; a < D; a++) p += w[a] * deflate[a];
        for (let a = 0; a < D; a++) w[a] -= p * deflate[a];
      }
      let m = 0;
      for (let a = 0; a < D; a++) m += w[a] * w[a];
      m = Math.sqrt(m);
      if (!(m > 0)) return null;
      for (let a = 0; a < D; a++) w[a] /= m;
      v = w;
    }
    return v;
  };
  const v1 = power(null);
  const v2 = v1 && power(v1);
  if (!v1 || !v2) return null;
  const F = new Float64Array(2 * D);
  F.set(v1, 0);
  F.set(v2, D);
  orthonormalize(F);
  return F;
}

/** Frame over `toDims` from a frame over `fromDims` (matching keys; zero elsewhere). */
export function remapFrame(F, fromDims, toDims) {
  const D0 = fromDims.length, D = toDims.length;
  const at = new Map(fromDims.map((k, i) => [k, i]));
  const out = new Float64Array(2 * D);
  let any = false;
  toDims.forEach((k, i) => {
    const j = at.get(k);
    if (j === undefined) return;
    out[i] = Number(F[j]) || 0;
    out[D + i] = Number(F[D0 + j]) || 0;
    if (out[i] || out[D + i]) any = true;
  });
  if (!any) return null;
  let n0 = 0, n1 = 0;
  for (let i = 0; i < D; i++) { n0 += out[i] ** 2; n1 += out[D + i] ** 2; }
  if (!(n0 > 1e-12) || !(n1 > 1e-12)) return null;
  orthonormalize(out);
  return out;
}

/**
 * Frame presets of a catalog: [{key, label, dims, F, kind}] from manifold.frames, manifest
 * presets of kind 'frame', and (IMPLEMENTATION_NOTES 91) presets of kind 'axes', which become
 * the frame with unit weight on dim x (row 0) and dim y (row 1). When `dimKeys` is given, an
 * axes preset is kept only if both its x and y are among those dims. Earlier entries win on a
 * repeated key.
 */
export function framePresets(client, dimKeys = null) {
  const man = client.manifest || {};
  const out = [];
  const seen = new Set();
  const add = (q) => { if (q.key && !seen.has(q.key)) { seen.add(q.key); out.push(q); } };
  const mdims = (man.manifold && man.manifold.dims) || [];
  for (const [k, F] of Object.entries((man.manifold && man.manifold.frames) || {})) {
    if (Array.isArray(F) && F.length === 2 * mdims.length) add({key: k, label: k, dims: mdims, F, kind: 'frame'});
  }
  const have = Array.isArray(dimKeys) ? new Set(dimKeys) : null;
  for (const p of man.presets || []) {
    if (!p) continue;
    if (p.kind === 'frame' && Array.isArray(p.dims) && Array.isArray(p.F)) {
      add({key: p.key, label: p.label || p.key, dims: p.dims, F: p.F, kind: 'frame'});
    } else if (p.kind === 'axes' && typeof p.x === 'string' && typeof p.y === 'string' && p.x && p.y && p.x !== p.y) {
      if (have && !(have.has(p.x) && have.has(p.y))) continue;
      add({key: p.key, label: p.label || p.key, dims: [p.x, p.y], F: [1, 0, 0, 1], kind: 'axes'});
    }
  }
  return out;
}

/**
 * Dim pair of a basis frame: {i, si, j, sj} when row 0 is ±1 on dim i and row 1 is ±1 on
 * dim j (all other weights ~0), else null. Vendor layout F = [row0 (D), row1 (D)].
 */
export function pairOfFrame(F, D, eps = 1e-6) {
  if (!F || F.length !== 2 * D) return null;
  const one = (o) => {
    let k = -1;
    for (let d = 0; d < D; d++) {
      const v = Number(F[o + d]);
      if (Math.abs(Math.abs(v) - 1) < eps) { if (k >= 0) return null; k = d; } else if (Math.abs(v) > eps) return null;
    }
    return k < 0 ? null : {k, s: Math.sign(Number(F[o + k]))};
  };
  const a = one(0), b = one(D);
  return a && b && a.k !== b.k ? {i: a.k, si: a.s, j: b.k, sj: b.s} : null;
}

/** Union of the stored base keys of the dims, in first-appearance order. */
export function baseKeysOf(dims) {
  const out = [];
  const seen = new Set();
  for (const d of dims) {
    let e;
    try {
      e = dimExpr(d);
    } catch (err) {
      if (err instanceof SyntaxError) continue;
      throw err;
    }
    for (const k of e.keys) if (!seen.has(k)) { seen.add(k); out.push(k); }
  }
  return out;
}

/** Vendor frame over `keys` from folded weights (keys missing from the support get 0). */
export function gpuFrame(folded, keys) {
  const G = keys.length;
  const F = new Float64Array(2 * G);
  const w0 = folded.W[0], w1 = folded.W[1];
  keys.forEach((k, g) => {
    F[g] = w0.get(k) || 0;
    F[G + g] = w1.get(k) || 0;
  });
  return F;
}

// =============================================================== pane

/** True for a frame preset name the pane can resolve: 'pca', `pair:i,j` or a key of `presets`. */
export function knownPreset(name, presets = []) {
  if (typeof name !== 'string' || !name) return false;
  if (name === 'pca' || /^pair:\d+,\d+$/.test(name)) return true;
  return (presets || []).some((p) => p && p.key === name);
}

/**
 * Frame preset a manifold pane opens on when the store holds no frame matrix: the stored
 * preset (fr0.preset), else options.frame, else the manifest's manifold.default_frame when it
 * names a known preset (knownPreset over `presets`), else 'pca'. Null when fr0 carries F.
 */
export function initialPreset(fr0, cfg, manifest, presets = []) {
  const f = fr0 || {};
  if (f.F && f.F.length) return null;
  if (typeof f.preset === 'string' && f.preset) return f.preset;
  const c = cfg || {};
  if (typeof c.frame === 'string' && c.frame) return c.frame;
  const df = manifest && manifest.manifold ? manifest.manifold.default_frame : null;
  if (typeof df === 'string' && df && knownPreset(df, presets)) return df;
  return 'pca';
}

/** Tour speed factor: store `speed`, else options.speed, else 1 (finite and > 0 only). */
export function tourSpeed(storeSpeed, cfg) {
  const a = Number(storeSpeed);
  if (storeSpeed !== null && storeSpeed !== undefined && Number.isFinite(a) && a > 0) return a;
  const b = Number(cfg && cfg.speed);
  if (cfg && cfg.speed !== null && cfg.speed !== undefined && Number.isFinite(b) && b > 0) return b;
  return 1;
}

export async function mount(host, ctx) {
  const {store, app, id} = ctx;
  const cfg = ctx.config || {};
  const ds = paneDataset(ctx);
  if (!ds) throw new Error('manifold pane needs a catalog dataset');
  const client = await app.catalog(ds);
  const n = client.n;
  const rt = app.runtime || {};
  const isMag = magKeys(client);

  const root = el('div', 'pk-manifold pk-plot');
  const bar = el('div', 'pk-plot-bar');
  const body = el('div', 'pk-plot-body');
  const foot = el('div', 'pk-plot-foot');
  root.append(bar, body, foot);
  host.append(root);
  const {stage, under, glc, over, note} = makeStage(body, 'pk-manifold-stage');
  let glCanvas = glc;
  over.setAttribute('aria-label', 'Manifold projection');

  // ------------------------------------------------------------ dims and frame
  const fr0 = store.get(`frame:${id}`) || {};
  const dimKeys = resolveDims(client, Array.isArray(fr0.dims) && fr0.dims.length ? fr0.dims : cfg.dims);
  if (dimKeys.length < 2) throw new Error('manifold needs at least two numeric dims');
  const D = dimKeys.length;
  ctx.setStatus('loading dims…');
  const dims = await Promise.all(dimKeys.map(async (k) => {
    const m = client.col(k) || {};
    const label = m.virtual ? (m.label && m.label !== k ? m.label : k) : (m.label || k);
    if (!m.virtual && Number.isFinite(m.center) && Number(m.scale) > 0) return {key: k, label, center: m.center, scale: m.scale};
    const v = await client.values(k, {prefix: Math.min(n, PCA_SAMPLE * 5)});
    return {key: k, label, ...robustStats(v)};
  }));
  const dimMeta = Object.fromEntries(dims.map((d) => [d.key, {center: d.center, scale: d.scale}]));
  const baseKeys = baseKeysOf(dims);
  const presets = framePresets(client, dimKeys);

  async function presetFrame(name) {
    if (!name || name === 'pca') {
      const mf = client.manifest.manifold;
      if (mf && mf.frames && Array.isArray(mf.frames.pca)) {
        const F = remapFrame(mf.frames.pca, mf.dims, dimKeys);
        if (F) return F;
      }
      const cols = await Promise.all(dimKeys.map((k) => client.values(k, {prefix: Math.min(n, PCA_SAMPLE * 5)})));
      return pcaFrame(cols, dims) || basisFrame(D, 0, 1);
    }
    const m = /^pair:(\d+),(\d+)$/.exec(name);
    if (m) {
      const i = Math.min(D - 1, Number(m[1])), j = Math.min(D - 1, Number(m[2]));
      return basisFrame(D, i, j === i ? (i + 1) % D : j);
    }
    const p = presets.find((q) => q.key === name);
    if (p) {
      const F = remapFrame(p.F, p.dims, dimKeys);
      if (F) return F;
      store.emit('toast', {level: 'warn', text: `Preset ${p.label} shares no dims with this view`});
    } else {
      store.emit('toast', {level: 'warn', text: `Unknown frame preset ${name}`});
    }
    return basisFrame(D, 0, 1);
  }

  let F0 = null;
  if (Array.isArray(fr0.F) && fr0.F.length === 2 * D) F0 = Float64Array.from(fr0.F);
  else if (fr0.F && fr0.F.length === 2 * D) F0 = Float64Array.from(fr0.F);
  const preset0 = initialPreset(fr0, cfg, client.manifest, presets);
  if (!F0) F0 = await presetFrame(preset0 || 'pca');
  ctx.setStatus('');

  // ------------------------------------------------------------ state
  const S = {
    tier: paneTier(ctx, n),
    path: null,              // 'gc' | 'xy' | 'canvas' | 'binned'
    preset: fr0.F ? null : preset0,
    folded: null, foldKey: '',
    base: new Map(), baseN: 0, baseLoading: null,
    cpu: null,               // {x, y, vis, m} main-thread projection of rows [0, m)
    proj: null,              // {token, x, y, vis, F} settled worker projection
    projPromise: null, token: 0,
    vis: null, visVersion: -1,
    color: null,
    w: 0, h: 0, dpr: 1, glDpr: 1,
    view: null, dom: null, auto: null,
    moving: false, preview: null, renderMs: 0,
    grid: null, gridToken: -1, ctxGrids: [], ctxKey: '',
    trails: [], hoverDim: -1, overlays: [], overlayDocs: null,
    gcBytes: 0,
    inView: null, inViewKey: '',   // rows in view after the last settle (XY path semantic zoom)
    prebinned: null,               // {g, i, j} manifest density grid of a dim-pair frame
  };
  let sel = null;
  let hl = null;             // narrative highlight (scatter.js narrativeHighlight)
  let xy = null;
  let gc = null;
  let gcData = null;
  let destroyed = false;
  let visible = true;
  const offs = [];
  // first paint (IMPLEMENTATION_NOTES 112): the first frame showing the prebinned density grid
  // or projected data; a failed base-column load or projection rejects
  const fp = firstPaintLatch();

  const tour = new Tour(D, {F: F0, seed: 0x5eed, speed: tourSpeed(store.get('speed'), cfg),
    onSettle: () => { S.moving = false; settle(); }});

  // ------------------------------------------------------------ toolbar
  const tools = ctx.tools || bar;
  const brushModes = brushToolbar((m) => {
    if (m !== 'pan') brush.setMode(m);
    over.style.cursor = m === 'pan' ? 'grab' : 'crosshair';
  });
  const tourGrp = el('span', 'pk-btn-group pk-tour-controls');
  tourGrp.setAttribute('role', 'group');
  tourGrp.setAttribute('aria-label', 'Tour');
  const playBtn = iconButton('▶', 'Play / pause the tour (space)', 'pk-tour-play');
  const prevBtn = iconButton('‹', 'Previous dim pair', 'pk-tour-prev');
  const nextBtn = iconButton('›', 'Next dim pair', 'pk-tour-next');
  const axX = iconButton('x▶', 'Step the x axis through the dims', 'pk-tour-axis-x');
  const axY = iconButton('y▶', 'Step the y axis through the dims', 'pk-tour-axis-y');
  tourGrp.append(prevBtn, playBtn, nextBtn, axX, axY);
  const resetBtn = iconButton('⟲', 'Reset zoom (double-click the plot)', 'pk-reset-view');
  if (cfg.tour !== false) tools.append(tourGrp);
  tools.append(brushModes.el, resetBtn);

  const presetSel = el('select', 'pk-preset-select pk-frame-presets');
  presetSel.setAttribute('aria-label', 'Frame preset');
  const addOpt = (v, t, parent = presetSel) => {
    const o = el('option', null, t);
    o.value = v;
    parent.append(o);
    return o;
  };
  addOpt('', 'Frame…');
  addOpt('pca', 'Principal components');
  for (const p of presets) if (p.key !== 'pca') addOpt(p.key, p.label);
  const pairs = el('optgroup');
  pairs.label = `Dim pairs (${Math.min(45, (D * (D - 1)) / 2)})`;
  let np = 0;
  for (let i = 0; i < D && np < 45; i++) for (let j = i + 1; j < D && np < 45; j++, np++) addOpt(`pair:${i},${j}`, `${dims[i].label} · ${dims[j].label}`, pairs);
  presetSel.append(pairs);
  presetSel.addEventListener('change', async () => {
    const v = presetSel.value;
    presetSel.value = '';
    if (!v) return;
    const F = await presetFrame(v);
    S.preset = v;
    goTo(F);
  });
  bar.append(presetSel);
  const dimInfo = el('span', 'pk-manifold-dims pk-muted', `${D} dims · ${baseKeys.length} columns`);
  dimInfo.title = dims.map((d) => d.label).join(', ');
  bar.append(dimInfo);

  const controls = createControls(bar, {app, store, client, pane: id, config: cfg,
    show: {template: false, features: false, av: Array.isArray(cfg.overlays) && cfg.overlays.length > 0,
      rv: Array.isArray(cfg.overlays) && cfg.overlays.length > 0}});
  const legend = createLegend(foot, {app, store, client, ds, pane: id, config: cfg, theme: ctx.theme,
    range: () => (S.color && S.color.kind === 'cont' ? [S.color.lo, S.color.hi] : null),
    note: (c) => colorNote(c)});

  /**
   * Why the plotted colour differs from the legend's: the GC renderer colours by a GPU dim, so
   * a continuous colour needs a stored (non-virtual) column with a free dim slot.
   */
  function colorNote(c) {
    if (!c || c.kind !== 'cont' || S.path !== 'gc' || !gcData) return null;
    if (gcData.keys.includes(c.by)) return null;
    const m = client.col(c.by);
    const why = !m || m.virtual ? 'an expression' : `all ${MAXD_GPU} GPU dims in use`;
    return `colour not drawn at this tier (${why}); points use the neutral colour`;
  }
  const chips = createChips(foot, {app, store, client, ds, pane: id, exclude: () => (S.color ? S.color.by : null)});

  function syncTourButtons() {
    const playing = tour.mode === 'drift';
    playBtn.textContent = playing ? '❚❚' : '▶';
    playBtn.setAttribute('aria-pressed', playing ? 'true' : 'false');
    axX.setAttribute('aria-pressed', tour.mode === 'axis' && tour._axis === 0 ? 'true' : 'false');
    axY.setAttribute('aria-pressed', tour.mode === 'axis' && tour._axis === 1 ? 'true' : 'false');
    stage.dataset.tour = tour.mode || 'idle';
  }
  function toggleDrift() {
    if (tour.mode === 'drift') { tour.stop(); S.moving = false; settle(); }
    else { S.preset = null; tour.drift(null); startMotion(); }
    syncTourButtons();
  }
  playBtn.addEventListener('click', toggleDrift);
  prevBtn.addEventListener('click', () => { S.preset = null; tour.nextPair(-1); startMotion(); syncTourButtons(); });
  nextBtn.addEventListener('click', () => { S.preset = null; tour.nextPair(1); startMotion(); syncTourButtons(); });
  const axisLoop = (a) => {
    if (tour.mode === 'axis' && tour._axis === (a === 'x' ? 0 : 1)) { tour.stop(); S.moving = false; settle(); }
    else { S.preset = null; tour.playAxisLoop(a); startMotion(); }
    syncTourButtons();
  };
  axX.addEventListener('click', () => axisLoop('x'));
  axY.addEventListener('click', () => axisLoop('y'));
  resetBtn.addEventListener('click', resetZoom);

  function goTo(F) {
    tour.to(F);
    startMotion();
    syncTourButtons();
  }

  // ------------------------------------------------------------ folding
  function currentFold() {
    const F = tour.frame;
    const dm = Number(store.get('dm')) || 0;
    let key = `${dm}|`;
    for (let i = 0; i < F.length; i++) key += `${F[i].toFixed(9)},`;
    if (key === S.foldKey && S.folded) return S.folded;
    S.folded = fold(F, dims, {dm, isMag});
    S.foldKey = key;
    return S.folded;
  }

  function projSpec(folded) {
    return {terms: folded.terms, c: folded.c, fade: Array.from(foldFade(folded))};
  }

  // ------------------------------------------------------------ paths
  function choosePath() {
    if (S.tier === 'T0') return 'canvas';
    if (S.tier === 'F' || rt.webgl2 === false) return 'binned';
    if (S.tier === 'T2' && rt.floatRT !== false && baseKeys.length <= MAXD_GPU) return 'gc';
    return 'xy';
  }

  function lowerTier(reason) {
    const t = n <= T0_MAX ? 'T0' : 'F';
    if (reason) console.info(`manifold ${id}: ${reason}; tier ${t}`);
    S.tier = t;
    setPaneTier(ctx, t);
    setPath();
  }

  function setPath() {
    const p = choosePath();
    if (p !== 'gc' && gc) { gc.destroy(); gc = null; gcData = null; releaseGpu(app, `${id}:gc`); }
    if (p !== 'xy' && xy) { xy.destroy(); xy = null; }
    if (p !== 'gc' && p !== 'xy') releaseGpu(app, id);
    if (p !== S.path && p === 'gc') {
      // a fresh canvas: the GC renderer and XYRenderer cannot share a WebGL context
      replaceGLCanvas();
    } else if (p !== S.path && p === 'xy' && S.path === 'gc') {
      replaceGLCanvas();
    }
    S.path = p;
    glCanvas.style.visibility = p === 'gc' || p === 'xy' ? '' : 'hidden';
    stage.dataset.path = p;
    stage.dataset.tier = S.tier;
    replan();
    if (p === 'gc') initGC().catch(gpuFail);
    else if (p === 'xy') initXY();
    loadBase().catch((err) => {
      warnErr('columns')(err);
      if (!(err && err.name === 'AbortError')) fp.reject(err);
    });
    legend.refresh();
    settle();
  }

  function replaceGLCanvas() {
    const c = el('canvas', 'pk-plot-gl');
    c.setAttribute('aria-hidden', 'true');
    glCanvas.replaceWith(c);
    glCanvas = c;
  }

  function gpuFail(err) {
    if (err && err.name === 'AbortError') return;
    console.warn(`manifold ${id}: GPU path failed`, err);
    lowerTier(`GPU path failed (${err.message})`);
  }

  function replan() {
    if ((S.path !== 'gc' && S.path !== 'xy') || !S.w) return;
    const dev = window.devicePixelRatio || 1;
    const vb = S.path === 'gc' ? n * gcVertexBytes(baseKeys.length) : n * XY_VERTEX_BYTES;
    const p = planGpu(app, id, {cssW: S.w, cssH: S.h, density: S.path === 'gc' || S.tier === 'T2', vertexBytes: vb}, dev);
    if (p.refused) {
      store.emit('toast', {level: 'info', text: `${id}: density layer limit reached; showing a binned image`});
      lowerTier('density panes over budget');
      return;
    }
    S.glDpr = Math.min(dev, p.dpr);
    if (xy) xy.resize(S.w, S.h, S.glDpr);
    if (gc) {
      gc.resize(S.w, S.h, S.glDpr);
      const bytes = vb + Math.round(S.w * S.glDpr) * Math.round(S.h * S.glDpr) * 48;
      if (!requestGpu(app, `${id}:gc`, bytes)) lowerTier('GPU memory budget');
    }
  }

  function initXY() {
    if (xy) return xy;
    xy = new XYRenderer(glCanvas, {budget: gpuBudget(app), owner: id,
      onLost: () => ctx.setStatus('WebGL context lost'),
      onRestored: () => { ctx.setStatus(''); uploadXY(); schedule(); }});
    if (!xy.ok) {
      xy.destroy();
      xy = null;
      lowerTier('WebGL2 unavailable');
      return null;
    }
    if (S.w) xy.resize(S.w, S.h, S.glDpr);
    uploadXY();
    return xy;
  }

  async function initGC() {
    const r = new GCRenderer(glCanvas, {onLost: () => ctx.setStatus('WebGL context lost'),
      onRestored: () => { ctx.setStatus(''); uploadGC(); schedule(); }});
    if (!r.init()) {
      r.destroy();
      lowerTier('WebGL2 unavailable');
      return;
    }
    gc = r;
    if (S.w) gc.resize(S.w, S.h, S.glDpr);
    ctx.setStatus('uploading columns…');
    const keys = gcKeys();
    const cols = await client.gpuColumns(keys);
    if (destroyed || gc !== r) return;
    const buf = await GCRenderer.interleave({n: cols.n, D: keys.length, raw: cols.raw});
    if (destroyed || gc !== r) return;
    gcData = {keys, cols, buf};
    uploadGC();
    legend.refresh();
    ctx.setStatus('');
    replan();
    schedule();
  }

  /** GPU dims: the base keys, plus the colour column when it is a stored key and fits. */
  function gcKeys() {
    const keys = baseKeys.slice();
    const by = S.color && S.color.kind === 'cont' ? S.color.by : null;
    if (by && client.col(by) && !client.col(by).virtual && !keys.includes(by) && keys.length < MAXD_GPU) keys.push(by);
    return keys;
  }

  /**
   * Upload the GC dataset. The uint16 dims are interleaved once per column set (gcData.buf,
   * Renderer.interleave in time slices), so filter / colour / theme changes only rebuild the
   * per-row vis and category codes.
   */
  function uploadGC() {
    if (!gc || !gcData) return;
    const {keys, cols, buf} = gcData;
    const need = gcKeys();
    if (need.length !== keys.length || need.some((k, i) => k !== keys[i])) {
      initGCColumns(need);
      return;
    }
    const visCodes = new Uint8Array(cols.n);
    for (let i = 0; i < cols.n; i++) visCodes[i] = !S.vis || S.vis[i] ? 1 : 0;
    const catCodes = S.color && S.color.kind === 'cat' ? S.color.codes : new Uint8Array(cols.n);
    gc.setData({n: cols.n, D: keys.length, raw: cols.raw, dims: keys.map((k, d) => ({A: cols.A[d], B: cols.B[d]})),
      categories: [{key: 'color', slot: 0}, {key: 'vis', slot: 3}], cats: {color: catCodes, vis: visCodes}}, {dims: buf});
    gc.setSelection(sel && sel.count ? sel.mask : null);
  }

  let gcColSeq = 0;
  function initGCColumns(keys) {
    const my = ++gcColSeq;
    client.gpuColumns(keys).then(async (cols) => {
      if (destroyed || my !== gcColSeq || !gc) return;
      const buf = await GCRenderer.interleave({n: cols.n, D: keys.length, raw: cols.raw});
      if (destroyed || my !== gcColSeq || !gc) return;
      gcData = {keys, cols, buf};
      uploadGC();
      legend.refresh();
      schedule();
    }).catch(gpuFail);
  }

  // ------------------------------------------------------------ data
  async function loadVis() {
    const v = store.get(`vis:${ds}`) || {};
    if (v.version === S.visVersion) return;
    S.visVersion = v.version;
    const r = await client.vis();
    if (destroyed) return;
    S.vis = r.vis || null;
    uploadGC();
    S.cpu = null;
    settle();
  }

  async function loadColorState() {
    try {
      S.color = await loadColor(client, store, cfg, ctx.theme, S.color);
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      S.color = null;
      store.emit('toast', {level: 'warn', text: `Colour: ${err.message}`});
    }
    if (destroyed) return;
    uploadGC();
    uploadXYColor();
    legend.refresh();
    chips.refresh();
    schedule();
  }

  /**
   * Main-thread base values: every row at T0, else the LOD prefix of MOTION_CPU_ROWS rows
   * (section 7.1: main-thread projection only for <= 50k rows; the worker projects the rest).
   */
  async function loadBase() {
    const m = S.tier === 'T0' ? n : Math.min(n, MOTION_CPU_ROWS);
    if (S.baseN === m && S.base.size === baseKeys.length) return;
    if (S.baseLoading) return S.baseLoading;
    S.baseLoading = (async () => {
      const vals = await Promise.all(baseKeys.map((k) => client.values(k, m < n ? {prefix: m} : {})));
      if (destroyed) return;
      S.base = new Map(baseKeys.map((k, i) => [k, vals[i]]));
      S.baseN = m;
      S.cpu = null;
      schedule();
    })();
    try {
      await S.baseLoading;
    } finally {
      S.baseLoading = null;
    }
  }

  /** Main-thread projection of the first m rows. */
  function cpuProject(folded, m) {
    if (S.base.size !== baseKeys.length) return null;
    const cols = {};
    for (const k of folded.support) cols[k] = S.base.get(k);
    const mm = Math.min(m, S.baseN);
    const vis = S.vis ? S.vis.subarray(0, mm) : null;
    const p = projectRows(folded, cols, mm, {fade: true, vis});
    p.m = mm;
    return p;
  }

  // ------------------------------------------------------------ settle: worker projection
  let settleTimer = null;
  function settle() {
    if (settleTimer) clearTimeout(settleTimer);
    settleTimer = setTimeout(() => {
      settleTimer = null;
      settleNow().catch((err) => {
        if (err && err.name === 'AbortError') return;
        console.warn(`manifold ${id}: projection failed`, err);
        ctx.setStatus('error', err.message);
        fp.reject(err);
      });
    }, 0);
    schedule();
  }

  async function settleNow() {
    if (destroyed) return;
    const folded = currentFold();
    const F = Float64Array.from(tour.frame);
    const token = ++S.token;
    const p = client.project(projSpec(folded), {token, pane: id}).then((r) => {
      if (destroyed || token !== S.token) return S.proj;
      S.proj = {token, x: r.x, y: r.y, vis: r.vis, F};
      return S.proj;
    });
    S.projPromise = p;
    const proj = await p;
    if (destroyed || !proj || proj.token !== token) return;
    if (!S.dom || !S.auto) S.auto = autoDomain(proj.x, proj.y, proj.vis);
    if (S.path === 'xy') uploadXY();
    if (!tour.playing) writeFrame();
    const jobs = [];
    if (S.path === 'binned') jobs.push(binGrid(token));
    if (S.path === 'xy') jobs.push(countInView(token));
    jobs.push(contextGrids());
    await Promise.all(jobs);
    schedule();
  }

  function viewRanges(view) {
    return [[Math.min(view.sx.d0, view.sx.d1), Math.max(view.sx.d0, view.sx.d1)],
      [Math.min(view.sy.d0, view.sy.d1), Math.max(view.sy.d0, view.sy.d1)]];
  }

  /** Semantic zoom (section 7.1, T2 on XYRenderer): rows of the settled projection in view. */
  async function countInView(token) {
    if (S.tier !== 'T2') return;
    const view = buildView();   // the domain may have changed since the last draw
    const [xr, yr] = viewRanges(view);
    const key = `${token}|${xr}|${yr}`;
    if (S.inViewKey === key) return;
    const g = await client.bin2d({pane: id, kind: 'proj', token}, 1, 1, {mask: null, xr, yr});
    if (token !== S.token) return;
    S.inView = g.counts[0] || 0;
    S.inViewKey = key;
  }

  /** XY path draws density at T2 unless fewer than SEMANTIC_ZOOM_MARKERS rows are in view. */
  function xyDensity(view, moving) {
    if (S.tier !== 'T2' || rt.floatRT === false) return false;
    if (moving || S.inView === null) return true;
    const [xr, yr] = viewRanges(view);
    if (S.inViewKey !== `${S.token}|${xr}|${yr}`) return true;
    return !(S.inView < SEMANTIC_ZOOM_MARKERS);
  }

  function autoDomain(x, y, vis) {
    // vis (0..255 fade) doubles as the mask; quantiles come from a strided sample
    const q = x.length > T0_MAX ? [0.003, 0.997] : null;
    const ex = sampledExtent(x, {q, mask: vis, pad: 0.06}) || [-4, 4];
    const ey = sampledExtent(y, {q, mask: vis, pad: 0.06}) || [-4, 4];
    // equal data units per pixel on both axes would distort non-square panes; keep each axis fitted
    return {x: ex, y: ey};
  }

  async function binGrid(token) {
    const view = buildView();
    const xr = [Math.min(view.sx.d0, view.sx.d1), Math.max(view.sx.d0, view.sx.d1)];
    const yr = [Math.min(view.sy.d0, view.sy.d1), Math.max(view.sy.d0, view.sy.d1)];
    const g = await client.bin2d({pane: id, kind: 'proj', token}, F_BINS, F_BINS, {mask: null, xr, yr});
    if (token !== S.token) return;
    S.grid = g;
    S.gridToken = token;
  }

  async function contextGrids() {
    const ids = contextDatasets(ctx, ds);
    if (!ids.length) { S.ctxGrids = []; return; }
    const folded = currentFold();
    const view = S.view || buildView();
    const key = `${S.foldKey}|${view.sx.d0},${view.sx.d1},${view.sy.d0},${view.sy.d1}`;
    if (key === S.ctxKey) return;
    const xr = [Math.min(view.sx.d0, view.sx.d1), Math.max(view.sx.d0, view.sx.d1)];
    const yr = [Math.min(view.sy.d0, view.sy.d1), Math.max(view.sy.d0, view.sy.d1)];
    const grids = await Promise.all(ids.map(async (cds, k) => {
      const bg = await app.catalog(cds);
      if (!resolvesIn(bg, folded.support)) return null;
      const tok = S.token * 16 + k;
      await bg.project(projSpec(folded), {token: tok, pane: `${id}:ctx`});
      const g = await bg.bin2d({pane: `${id}:ctx`, kind: 'proj', token: tok}, CONTEXT_BINS, CONTEXT_BINS, {mask: 'vis', xr, yr});
      return {counts: g.counts, nx: g.nx, ny: g.ny, xr: g.xr, yr: g.yr};
    }));
    S.ctxGrids = grids.filter(Boolean);
    S.ctxKey = key;
  }

  function writeFrame() {
    const val = {dims: dimKeys.slice(), F: Array.from(tour.frame, (v) => Math.round(v * 1e6) / 1e6)};
    if (S.preset) val.preset = S.preset;
    const cur = store.get(`frame:${id}`) || {};
    const same = cur.preset === val.preset && Array.isArray(cur.F) && cur.F.length === val.F.length && cur.F.every((v, i) => Math.abs(v - val.F[i]) < 1e-6);
    if (!same) store.set({[`frame:${id}`]: val}, {source: id});
  }

  // ------------------------------------------------------------ uploads (XYRenderer)
  function uploadXY(cpu = null) {
    if (!xy) return;
    const src = cpu || S.proj;
    if (!src) return;
    xy.setXY(src.x, src.y, src.vis, {origin: [0, 0]});
    uploadXYColor(src.x.length);
    xy.setSelection(sel && sel.count ? sliceTo(sel.mask, src.x.length) : null);
  }

  function sliceTo(a, m) {
    return a && a.length !== m ? a.subarray(0, m) : a;
  }

  function uploadXYColor(m = null) {
    if (!xy) return;
    const cs = S.color;
    const len = m ?? (S.proj ? S.proj.x.length : n);
    if (cs && cs.kind === 'cat') { xy.setCats([sliceTo(cs.codes, len)]); xy.setColorValues(null); }
    else if (cs && cs.kind === 'cont') { xy.setCats([]); xy.setColorValues(sliceTo(cs.values, len)); }
    else { xy.setCats([]); xy.setColorValues(null); }
  }

  // ------------------------------------------------------------ view
  function domain() {
    if (S.dom) return S.dom;
    if (S.auto) return S.auto;
    const g = prebinnedGrid();
    if (g) return {x: [Math.min(...g.xr), Math.max(...g.xr)], y: [Math.min(...g.yr), Math.max(...g.yr)]};
    return {x: [-4, 4], y: [-4, 4]};
  }

  // ------------------------------------------------------------ pre-binned first paint
  // A manifest density grid (section 5.9, built for axes presets) applies when the frame is a
  // dim pair whose dims are that grid's x / y expressions on linear axes: the projection is then
  // x' = s·(x - c_e)/scale + K, an affine map of the grid's extents. It is drawn until the first
  // projection (or the GC columns) arrives.
  function loadPrebinned() {
    S.prebinned = null;
    if (n <= T0_MAX) return;
    const pr = pairOfFrame(tour.frame, D);
    if (!pr) return;
    const kx = dims[pr.i].key, ky = dims[pr.j].key;
    const isLog = (k) => !!(client.col(k) && client.col(k).log);
    if (isLog(kx) || isLog(ky)) return;
    const man = client.manifest || {};
    const logPreset = (man.presets || []).some((q) => q && q.kind === 'axes' && q.x === kx && q.y === ky && (q.xlog || q.ylog));
    const g = (man.density || []).find((q) => q && !q.stat && q.x === kx && q.y === ky);
    if (!g || logPreset) return;
    client.density(g.key).then((d) => {
      if (destroyed || !d) return;
      S.prebinned = {g: d, i: pr.i, j: pr.j};
      schedule();
    }, (err) => {
      if (err && err.name === 'AbortError') return;
      console.warn(`density grid ${g.key} unavailable`, err);
    });
  }

  /** The pre-binned grid in projected units for the current frame, or null. */
  function prebinnedGrid() {
    const P = S.prebinned;
    if (!P) return null;
    const pr = pairOfFrame(tour.frame, D);
    if (!pr || pr.i !== P.i || pr.j !== P.j) return null;
    const folded = currentFold();
    const map = (d, s, c) => {
      const e = dimExpr(dims[d]);
      const a = s / (Number(dims[d].scale) || 1);
      return (v) => a * (v - (Number(e.c) || 0)) + c;
    };
    const fx = map(P.i, pr.si, folded.c[0]), fy = map(P.j, pr.sj, folded.c[1]);
    const g = P.g;
    return {counts: g.values, nx: g.nx, ny: g.ny, xr: [fx(g.xr[0]), fx(g.xr[1])], yr: [fy(g.yr[0]), fy(g.yr[1])]};
  }

  function firstPaintPending() {
    if (!S.path) return S.tier !== 'T0';
    if (S.path === 'gc') return !gcData;
    if (S.path === 'xy' || S.path === 'binned') return !S.proj;
    return false;
  }

  function axisText(c) {
    const info = axisInfo(tour.frame, c, dims);
    const s = info ? axisLabel(info, dims) : '';
    return s || (c === 0 ? 'x' : 'y');
  }

  function buildView() {
    const d = domain();
    return makeView({width: S.w, height: S.h, dpr: S.dpr,
      x: {kind: 'lin', d0: d.x[0], d1: d.x[1], label: axisText(0)},
      y: {kind: 'lin', d0: d.y[0], d1: d.y[1], label: axisText(1)}});
  }

  function projRef(view = S.view) {
    return {pane: id, kind: 'proj', token: S.proj ? S.proj.token : S.token,
      kx: view ? Math.abs(view.sx.b) : 1, ky: view ? Math.abs(view.sy.b) : 1};
  }

  // ------------------------------------------------------------ drawing
  let frameReq = 0;
  let lastT = 0;
  function schedule() {
    if (frameReq || destroyed) return;
    frameReq = requestAnimationFrame(tick);
  }

  function startMotion() {
    S.moving = true;
    lastT = 0;
    schedule();
  }

  function tick(t) {
    frameReq = 0;
    if (destroyed) return;
    const animating = tour.animating || tour.playing || tour._dwell > 0;
    if (animating && visible) {
      const dt = lastT ? Math.min(100, t - lastT) : 16;
      lastT = t;
      tour.step(dt);
      S.moving = !tour.justSettled || tour.playing;
    } else {
      lastT = 0;
    }
    draw();
    if ((tour.animating || tour.playing || tour._dwell > 0) && visible) schedule();
  }

  function draw() {
    if (!visible || !(S.w > 1 && S.h > 1)) return;
    const t0 = now();
    const theme = ctx.theme();
    const tk = tokensOf(theme);
    const folded = currentFold();
    const view = S.view = buildView();
    brush.setView(view);
    const u = prepareCanvas(under, S.w, S.h, S.dpr);
    u.clearRect(0, 0, S.w, S.h);
    drawAxes(u, view, {tokens: tk, xLabel: view.x.label, yLabel: view.y.label});
    for (const g of S.ctxGrids) drawDensityGrid(u, view, g, {tokens: tk, cmap: 'gray_r', alpha: 0.45});
    let drewGrid = false;
    if (S.prebinned && firstPaintPending()) {
      const pg = prebinnedGrid();
      if (pg) {
        drawDensityGrid(u, view, pg, {tokens: tk, cmap: theme.densityCmap || 'magma'});
        drewGrid = true;
      }
    }
    const moving = S.moving || tour.animating || tour.playing;
    let shown = 0;
    let pts = null;
    if (S.path === 'canvas') {
      const p = cpuProject(folded, n);
      if (p) {
        S.cpu = p;
        if (cfg.trails && moving) {
          S.trails.push({x: p.x, y: p.y, vis: p.vis});
          if (S.trails.length > TRAIL_FRAMES) S.trails.shift();
          drawTrails(u, view, tk);
        } else S.trails = [];
        pts = pointsFrom(p, tk);
        drawPoints(u, view, pts, {tokens: tk});
        shown = pts.length;
      }
    } else if (S.path === 'binned') {
      if (moving || S.gridToken !== S.token || !S.grid) {
        const p = cpuProject(folded, Math.min(n, MOTION_CPU_ROWS));
        if (p) { S.cpu = p; shown = drawDots(u, view, p, tk); }
      } else {
        drawDensityGrid(u, view, S.grid, {tokens: tk, cmap: theme.densityCmap || 'magma'});
        shown = S.grid.total;
      }
    } else if (S.path === 'xy' && xy) {
      let count = n;
      if (moving) {
        const p = cpuProject(folded, Math.min(n, MOTION_CPU_ROWS));
        if (p) { S.cpu = p; uploadXY(p); count = p.m; }
        S.uploadedCpu = true;
      } else if (S.uploadedCpu && S.proj && S.proj.token === S.token) {
        uploadXY();
        S.uploadedCpu = false;
      }
      const dens = xyDensity(view, moving);
      const ok = xy.render({camera: cameraFromView(view), mode: dens ? 'density' : 'markers',
        color: S.color ? S.color.gpu : {mode: 0}, pointSize: dens ? 2.5 : n <= 20000 ? 5 : 3.5,
        selection: {show: true}, neutral: tk.neutral, selColor: tk.sel, count, alpha: n <= 20000 ? 0.85 : 0.6,
        densityCmap: theme.densityCmap});
      if (!ok && xy.stats().refused === 'targets') { lowerTier('density targets over budget'); return; }
      const st = xy.stats();
      shown = st.count;
      if (cfg.trails && moving) trailGL(tk);
    } else if (S.path === 'gc' && gc && gcData) {
      const keys = gcData.keys;
      const frame = gpuFrame(folded, keys);
      const cs = S.color;
      let color = {mode: 'none'};
      if (cs && cs.kind === 'cat') color = {mode: 'categorical', slot: 0, colors: cs.gpu.colors};
      else if (cs && cs.kind === 'cont') {
        const d = keys.indexOf(cs.by);
        let lo = cs.lo, hi = cs.hi;
        if (String(cs.cmap).endsWith('_r')) [lo, hi] = [hi, lo];
        if (d >= 0) color = {mode: 'continuous', dim: d, lo, hi, cmap: String(cs.cmap).replace(/_r$/, '')};
      }
      const count = moving ? Math.min(n, MOTION_PREFIX) : n;
      gc.render({frame, camera: foldCamera(cameraFromView(view), folded.c), color,
        filters: {masks: [null, null, null, 0b10], z: null, need: -1}, pointSize: 2.5, gain: 1,
        selection: !!(sel && sel.count), bloom: 0, count, neutral: theme.neutralRGB, selColor: theme.selRGB});
      shown = count;
      if (cfg.trails && moving) trailGL(tk);
    }
    if (S.path === 'xy' || S.path === 'gc') clipToPlot(glCanvas, view);
    if (!moving && !cfg.trails) clearTrail();
    if (!moving && cfg.trails && S.path !== 'canvas') clearTrail();
    S.shown = shown;
    drawOver(view, tk);
    stage.dataset.mode = S.path;
    stage.dataset.moving = moving ? '1' : '0';
    note.hidden = true;
    S.renderMs = now() - t0;
    if (n === 0 || drewGrid || (S.path && !firstPaintPending() && (S.path !== 'canvas' || pts))) fp.resolve();
  }

  // trails for GL paths: a 2-D canvas between `under` and the GL canvas keeps fading copies
  let trailCv = null;
  function trailGL() {
    if (!trailCv) {
      trailCv = el('canvas', 'pk-plot-trail');
      trailCv.setAttribute('aria-hidden', 'true');
      glCanvas.before(trailCv);
    }
    const c = prepareCanvas(trailCv, S.w, S.h, S.dpr);
    clipToPlot(trailCv, S.view);
    c.save();
    c.globalCompositeOperation = 'destination-out';
    c.fillStyle = 'rgba(0,0,0,0.22)';
    c.fillRect(0, 0, S.w, S.h);
    c.globalCompositeOperation = 'source-over';
    c.globalAlpha = 0.45;
    c.drawImage(glCanvas, 0, 0, S.w, S.h);
    c.restore();
  }
  function clearTrail() {
    if (trailCv) { trailCv.remove(); trailCv = null; }
  }

  function drawTrails(c, view, tk) {
    const k = S.trails.length;
    S.trails.forEach((t, j) => {
      if (j === k - 1) return;
      c.globalAlpha = 0.08 + (0.25 * j) / k;
      c.fillStyle = tk.neutral;
      for (let i = 0; i < t.x.length; i++) {
        if (!t.vis[i]) continue;
        const [px, py] = view.toPx(t.x[i], t.y[i]);
        c.fillRect(px - 1, py - 1, 2, 2);
      }
    });
    c.globalAlpha = 1;
  }

  function pointsFrom(p, tk) {
    const cs = S.color;
    const selOn = sel && sel.count > 0;
    const base = [], top = [];
    for (let i = 0; i < p.m; i++) {
      if (!p.vis[i]) continue;
      const q = {x: p.x[i], y: p.y[i], color: rowColor(cs, i) || tk.neutral, size: n <= 500 ? 7 : 5};
      const a = p.vis[i] / 255;
      if (a < 0.999) q.alpha = a;
      if (selOn && sel.mask[i]) { q.stroke = tk.sel; q.lineWidth = 1.75; top.push(q); }
      else { if (selOn) q.alpha = (q.alpha ?? 1) * 0.3; base.push(q); }
    }
    return base.concat(top);
  }

  function drawDots(c, view, p, tk) {
    const selOn = sel && sel.count > 0;
    let k = 0;
    c.save();
    const P = view.plot;
    c.beginPath();
    c.rect(P.left, P.top, P.width, P.height);
    c.clip();
    for (let i = 0; i < p.m; i++) {
      if (!p.vis[i]) continue;
      const [px, py] = view.toPx(p.x[i], p.y[i]);
      c.globalAlpha = Math.max(0.15, p.vis[i] / 255) * 0.6;
      c.fillStyle = selOn && sel.mask[i] ? tk.sel : (rowColor(S.color, i) || tk.neutral);
      c.fillRect(px - 1, py - 1, 2, 2);
      k++;
    }
    c.restore();
    return k;
  }

  function focusXY(i) {
    if (i < 0) return null;
    if (S.cpu && i < S.cpu.m && S.cpu.vis[i]) return [S.cpu.x[i], S.cpu.y[i]];
    if (S.proj && S.proj.token === S.token && S.proj.vis[i]) return [S.proj.x[i], S.proj.y[i]];
    return null;
  }

  function overlayState() {
    const docs = S.overlayDocs || {};
    const colmap = {};
    for (const c of client.manifest.cols || []) if (c.filter) colmap[c.key] = c.filter;
    return {av: Number(store.get('av')) || 0, rv: Number(store.get('rv')) || 3.1, dm: 0,
      frame: {F: tour.frame, dims: dimKeys}, dimMeta, filters: docs.filt || null, colmap, ext: docs.ext || null,
      law: (docs.ext && docs.ext.law) || undefined, isMag};
  }

  function compassOpts() {
    const size = Math.max(90, Math.min(170, 0.36 * Math.min(S.w, S.h)));
    const P = S.view ? S.view.plot : {right: S.w, top: 0};
    return {size, cx: P.right - size / 2 - 4, cy: P.top + size / 2 + 4, width: S.w, height: S.h};
  }

  function drawOver(view, tk) {
    const o = prepareCanvas(over, S.w, S.h, S.dpr);
    o.clearRect(0, 0, S.w, S.h);
    if (S.overlays.length) drawOverlays(o, view, S.overlays, overlayState());
    if (hl) {
      const hr = hl.rows();
      if (S.hlGoto) gotoHighlight(hr);
      for (let k = 0; k < hr.length; k++) {
        const q = focusXY(hr[k]);
        if (q) drawFocusRing(o, view, q[0], q[1], {hover: true, tokens: tk, color: tk.focus, width: 1.75, r: 7});
      }
    }
    const fi = storeRow(store, 'focus', ds), hi = storeRow(store, 'hover', ds);
    const hp = hi !== fi ? focusXY(hi) : null;
    if (hp) drawFocusRing(o, view, hp[0], hp[1], {hover: true, tokens: tk, r: 6});
    const fp = focusXY(fi);
    if (fp) drawFocusRing(o, view, fp[0], fp[1], {tokens: tk});
    if (cfg.compass !== false) {
      drawCompass(o, tour.frame, dims, {...compassOpts(), tokens: tk, hover: S.hoverDim, background: true});
    }
    if (S.preview) drawBrushPreview(o, view, S.preview, tk);
  }

  /** Narrative `goto`: re-centre on the highlighted rows when some are outside the view. */
  function gotoHighlight(rows) {
    if (!S.cpu && !(S.proj && S.proj.token === S.token)) { S.hlGoto = true; return; }
    S.hlGoto = false;
    if (!rows || !rows.length) return;
    const xs = [], ys = [];
    for (const i of rows) {
      const q = focusXY(i);
      if (q) { xs.push(q[0]); ys.push(q[1]); }
    }
    const d = domain();
    const c = highlightCentre(xs.map((_, k) => k), xs, ys, {dom: d});
    if (!c || !c.outside) return;
    S.dom = {x: recentreDomain(d.x, c.x), y: recentreDomain(d.y, c.y)};
    settleZoom();
    schedule();
  }

  // ------------------------------------------------------------ interaction
  const brush = attachBrush(over, () => S.view, {
    mode: 'rect', store, pane: id,
    enabled: () => brushModes.mode() !== 'pan',
    onPreview: (p) => {
      if (p && (tour.animating || tour.playing)) { tour.stop(); S.moving = false; syncTourButtons(); settle(); }
      S.preview = p;
      if (S.view) drawOver(S.view, tokensOf(ctx.theme()));
    },
    onCommit: (shape, op) => { commitBrush(shape, op).catch(brushError); },
    onClick: (px, py, e) => { pickAt(px, py, e, true); },
  });

  function brushError(err) {
    if (err && err.name === 'AbortError') return;
    store.emit('toast', {level: 'error', text: `Selection failed: ${err.message}`});
  }

  async function commitBrush(shape, op) {
    if (S.moving || !S.proj || S.proj.token !== S.token) await (S.projPromise || settleNow());
    if (S.projPromise) await S.projPromise;
    const res = await client.brush(projRef(), shape);
    const recipe = n > BITS_IN_HASH_MAX_N ? await idsRecipe(ctx, ds, res.hit, op) : null;
    return applyBrush(ctx, ds, res, shape, op, recipe);
  }

  async function pickAt(px, py, e, click) {
    const view = S.view;
    if (!view || !S.proj || S.proj.token !== S.token || S.moving || !view.inPlot(px, py)) {
      if (!click && store.get('hover')) store.set({hover: null}, {source: id});
      return;
    }
    const [x, y] = view.toData(px, py);
    const i = await client.pick(projRef(view), x, y, pickRadius(e));
    if (destroyed) return;
    if (click) { if (i >= 0) focusRow(ctx, ds, i); return; }
    const cur = store.get('hover');
    if (i >= 0 && !(cur && cur.ds === ds && cur.i === i)) store.set({hover: {ds, i}}, {source: id});
    else if (i < 0 && cur && cur.ds === ds) store.set({hover: null}, {source: id});
  }

  // hover picks are throttled to one in flight; the latest pointer event that arrived while a
  // pick was busy is replayed when it finishes, so hover follows the pointer's final position
  let hoverBusy = false, hoverNext = null, hoverT = 0;
  function onHoverMove(e) {
    if (brush.active() || panDrag || !S.view || S.hoverDim >= 0) return;
    hoverNext = e;
    if (hoverBusy) return;
    hoverBusy = true;
    const wait = Math.max(0, HOVER_MS - (now() - hoverT));
    setTimeout(() => {
      const ev = hoverNext;
      hoverNext = null;
      hoverT = now();
      if (!ev || !S.view || destroyed) { hoverBusy = false; return; }
      const [px, py] = eventToViewPx(over, S.view, ev);
      pickAt(px, py, ev, false).catch((err) => {
        if (!(err && err.name === 'AbortError')) console.warn('pick failed', err);
      }).finally(() => { hoverBusy = false; if (hoverNext) onHoverMove(hoverNext); });
    }, wait);
  }
  over.addEventListener('pointermove', onHoverMove);
  over.addEventListener('pointerleave', () => {
    hoverNext = null;
    const cur = store.get('hover');
    if (cur && cur.ds === ds) store.set({hover: null}, {source: id});
  });

  let detachCompass = null;
  if (cfg.compass !== false) {
    detachCompass = attachCompass(over, {
      getFrame: () => tour.frame,
      dims,
      getOpts: compassOpts,
      onMove: (j, a, b) => {
        S.preset = null;
        tour.manual(j, a, b);
        S.moving = true;
        syncTourButtons();
        schedule();
      },
      onEnd: () => { S.moving = false; settle(); },
      onReset: (j) => { tour.manual(j, 0, 0); S.moving = false; settle(); },
      onHover: (j) => { S.hoverDim = j; if (S.view) drawOver(S.view, tokensOf(ctx.theme())); },
    });
  }

  let panDrag = null;
  over.addEventListener('pointerdown', (e) => {
    if (e.pkHandled || brushModes.mode() !== 'pan' || !S.view || e.button !== 0) return;
    const [px, py] = eventToViewPx(over, S.view, e);
    panDrag = {px, py, view: S.view};
    over.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  over.addEventListener('pointermove', (e) => {
    if (!panDrag) return;
    const [px, py] = eventToViewPx(over, panDrag.view, e);
    const v = panDrag.view;
    S.dom = {x: panDomain(v.sx, px - panDrag.px), y: panDomain(v.sy, py - panDrag.py)};
    schedule();
  });
  const endPan = () => { if (panDrag) { panDrag = null; settleZoom(); } };
  over.addEventListener('pointerup', endPan);
  over.addEventListener('pointercancel', endPan);
  over.addEventListener('wheel', (e) => {
    if (!S.view) return;
    const [px, py] = eventToViewPx(over, S.view, e);
    if (!S.view.inPlot(px, py)) return;
    e.preventDefault();
    const f = Math.pow(WHEEL_STEP, -e.deltaY * (e.deltaMode === 1 ? 16 : 1));
    S.dom = {x: zoomDomain(S.view.sx, px, f), y: zoomDomain(S.view.sy, py, f)};
    schedule();
    settleZoom();
  }, {passive: false});
  over.addEventListener('dblclick', (e) => {
    if (e.defaultPrevented) return;
    resetZoom();
  });
  let zoomTimer = null;
  function settleZoom() {
    if (zoomTimer) clearTimeout(zoomTimer);
    zoomTimer = setTimeout(() => {
      zoomTimer = null;
      if (S.path === 'binned' && S.proj) binGrid(S.proj.token).then(schedule, warnErr('density'));
      if (S.path === 'xy' && S.proj && S.proj.token === S.token) countInView(S.proj.token).then(schedule, warnErr('rows in view'));
      contextGrids().then(schedule, warnErr('context density'));
    }, 150);
  }
  function resetZoom() {
    S.dom = null;
    if (S.proj) S.auto = autoDomain(S.proj.x, S.proj.y, S.proj.vis);
    settleZoom();
    schedule();
  }

  function warnErr(what) {
    return (err) => {
      if (err && err.name === 'AbortError') return;
      store.emit('toast', {level: 'warn', text: `${what}: ${err.message}`});
    };
  }

  // ------------------------------------------------------------ overlays (dim space)
  async function loadOverlays() {
    const ids = Array.isArray(cfg.overlays) ? cfg.overlays : [];
    if (!ids.length) return;
    const [ov, iso, ext, filt] = await Promise.all(['overlays', 'isochrones', 'extinction', 'filters'].map((k) => docOf(ctx, k).catch((err) => {
      console.warn(`${k} document unavailable`, err);
      return null;
    })));
    if (destroyed) return;
    S.overlayDocs = {ov, iso, ext, filt};
    S.overlays = resolveOverlays(ids, {overlays: ov, isochrones: iso, extinction: ext}).layers;
    schedule();
  }

  // ------------------------------------------------------------ store wiring
  offs.push(store.on([`frame:${id}`], (changed, s, sources) => {
    if (sources.size === 1 && sources.has(id)) return;
    const fr = s.get(`frame:${id}`) || {};
    if (Array.isArray(fr.dims) && fr.dims.length && fr.dims.join('\u0000') !== dimKeys.join('\u0000')) {
      const F = fr.F && fr.F.length === 2 * fr.dims.length ? remapFrame(fr.F, fr.dims, dimKeys) : null;
      if (F) { S.preset = null; goTo(F); }
      return;
    }
    if (fr.F && fr.F.length === 2 * D) { S.preset = fr.preset || null; goTo(Float64Array.from(fr.F)); }
    else if (fr.preset) presetFrame(fr.preset).then((F) => { S.preset = fr.preset; goTo(F); }, warnErr('frame preset'));
  }));
  offs.push(store.on([`vis:${ds}`], () => { loadVis().catch(warnErr('filter mask')); }));
  offs.push(store.on(['color'], () => { loadColorState(); }));
  offs.push(store.on(['dm'], () => { S.foldKey = ''; settle(); }));
  offs.push(store.on(['speed'], (changed, s) => { tour.setSpeed(tourSpeed(s.get('speed'), cfg)); }));
  offs.push(store.on(['av', 'rv'], () => schedule()));
  offs.push(store.on(['focus', 'hover'], () => { if (S.view) drawOver(S.view, tokensOf(ctx.theme())); }));
  offs.push(store.on(['theme'], () => loadColorState()));
  offs.push(store.onEvent('theme:change', () => loadColorState()));
  offs.push(store.on([`tier:${id}`], (changed, s, sources) => {
    if (sources.size === 1 && sources.has(id)) return;
    const t = paneTier(ctx, n);
    if (t === S.tier) return;
    S.tier = t;
    setPath();
  }));
  offs.push(store.onEvent('tour:toggle', (p) => {
    if (!p || p.handled || cfg.tour === false || !visible) return;
    const lay = ctx.layout;
    if (lay && typeof lay.panes === 'function') {
      const first = lay.panes().find((q) => q && q.type === 'manifold');
      if (first && first.id !== id) return;
    }
    p.handled = true;
    toggleDrift();
  }));
  offs.push(store.onEvent('selection:invert', (p) => {
    if (!p || p.handled || (p.ds && p.ds !== ds) || !sel || !S.proj) return;
    p.handled = true;
    const d = new Uint8Array(n);
    for (let i = 0; i < n; i++) d[i] = S.proj.vis[i] > 0 && Number.isFinite(S.proj.x[i]) ? 1 : 0;
    sel.invert(d);
  }));
  // narrative beats (IMPLEMENTATION_NOTES 71)
  hl = narrativeHighlight(ctx, ds, meta.type, (rows, p) => {
    if (p && p.goto && rows.length) gotoHighlight(rows);
    if (S.view) drawOver(S.view, tokensOf(ctx.theme()));
  });
  offs.push(() => hl.destroy());
  sel = await app.selectionFor(ds);
  offs.push(sel.onChange(() => {
    if (gc) gc.setSelection(sel.count ? sel.mask : null);
    if (xy) xy.setSelection(sel.count ? sliceTo(sel.mask, S.proj ? S.proj.x.length : n) : null);
    schedule();
  }));

  // ------------------------------------------------------------ sizing
  function measure() {
    const r = stage.getBoundingClientRect();
    const w = Math.floor(r.width), h = Math.floor(r.height);
    const dpr = window.devicePixelRatio || 1;
    if (w === S.w && h === S.h && dpr === S.dpr) return;
    S.w = w; S.h = h; S.dpr = dpr;
    replan();
    settleZoom();
    schedule();
  }
  const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(measure) : null;
  if (ro) ro.observe(stage);

  // ------------------------------------------------------------ start
  if (!store.get(`tier:${id}`)) setPaneTier(ctx, S.tier);
  measure();
  loadPrebinned();
  await loadVis().catch(warnErr('filter mask'));
  await loadColorState();
  setPath();
  loadOverlays().catch(warnErr('overlays'));
  syncTourButtons();
  if (cfg.tour === true && fr0.F == null && fr0.preset == null && cfg.autoplay) toggleDrift();
  hl.sync();

  return {
    firstPaint: fp.promise,
    resize() { measure(); },
    onVisible(v) {
      visible = !!v;
      if (visible) { measure(); schedule(); }
    },
    destroy() {
      destroyed = true;
      fp.resolve();
      tour.stop();
      if (frameReq) cancelAnimationFrame(frameReq);
      if (settleTimer) clearTimeout(settleTimer);
      if (zoomTimer) clearTimeout(zoomTimer);
      for (const off of offs) if (typeof off === 'function') off();
      brush();
      if (detachCompass) detachCompass();
      if (ro) ro.disconnect();
      if (xy) xy.destroy();
      if (gc) gc.destroy();
      releaseGpu(app, `${id}:gc`);
      releaseGpu(app, id);
      controls.destroy();
      legend.destroy();
      chips.destroy();
      tourGrp.remove();
      brushModes.el.remove();
      resetBtn.remove();
      root.remove();
    },
    async snapshot() {
      const view = S.view || buildView();
      const tk = tokensOf(ctx.theme());
      if (S.path === 'canvas' && S.cpu) {
        const svg = toSVG(view, [
          {kind: 'axes', tokens: tk, xLabel: view.x.label, yLabel: view.y.label},
          {kind: 'points', pts: pointsFrom(S.cpu, tk), opts: {tokens: tk}},
          {kind: 'draw', fn: (c) => { if (cfg.compass !== false) drawCompass(c, tour.frame, dims, {...compassOpts(), tokens: tk, background: true}); }},
        ]);
        return new Blob([svg], {type: 'image/svg+xml'});
      }
      draw();
      return snapshotCanvases([under, trailCv, glCanvas, over], S.w, S.h, tk.bg);
    },
    stats() {
      return {renderMs: S.renderMs, tier: S.tier, path: S.path, n, D, support: baseKeys.length,
        shown: S.shown || 0, selected: sel ? sel.count : 0, tour: tour.mode, token: S.token,
        projected: !!(S.proj && S.proj.token === S.token), gl: xy ? xy.stats() : gc && gcData ? {renderer: 'gc', n: gcData.cols.n, dims: gcData.keys.length} : null,
        frame: Array.from(tour.frame), dims: dimKeys.slice(), status: fmtCount(n),
        inView: S.inView, prebinned: !!S.prebinned, colorNote: colorNote(S.color),
        preset: S.preset || null, highlight: hl ? hl.rows().length : 0};
    },
    async brush(shape, op = 'replace') {
      if (!S.proj || S.proj.token !== S.token) await (S.projPromise || settleNow());
      return commitBrush(shape, op);
    },
  };
}
