// Overlay drawing for scatter and manifold panes (ARCHITECTURE.md 6.9, 7.4, 5.10 pk-overlays/1).
//
//   drawOverlays(ctx2d, view, layers, state) -> [{id, opacity, drawn}]
//
// view: plot/axes.js makeView(). Overlay coordinates resolve in one of two ways:
//   * scatter (no state.frame): the view's axis expressions (view.x.expr / view.y.expr, or
//     state.x / state.y) are linear expressions over base keys; an item value vector
//     {key: v} maps to (x_expr(v), y_expr(v)). An item missing a needed key is not drawn.
//   * frame (state.frame = {F | rows, dims}): values project through the current frame with
//     the per-dim standardization, x_r = Σ_d F[r,d]·(expr_d(v) − center_d)/scale_d, so the
//     overlay moves with tours. Dims the item does not cover contribute 0 and the layer's
//     opacity is 1 − Σ(uncovered weight)/Σ(total weight), weight_d = F[0,d]² + F[1,d]².
//     frame is the `frame:<pane>` store value (section 6.2: {preset?, F: 2·D numbers,
//     dims: string[]}); string dims take {center, scale, expr?} from state.dimMeta[key]
//     (manifest ColMeta / virtual-dim standardization; default center 0, scale 1). Object
//     dims {key, center, scale, expr?} are accepted as well. A dim without expr whose key is
//     not a stored-key name is a virtual dim and its key is compiled as the expression.
//
// Layer shapes (pk-overlays/1 `space:"dim"`):
//   {id, name, space, kind: polygons|circles|markers|polyline|rings|boxes|vector|track,
//    style: {color, width, dash, shape, opacity, fill, size, label}, items}
//   items: {x: expr, y: expr, pts: [[x, y], ...]} (2D) or {dims: {key: [v...]}}, plus
//   optional label. Track items may be isochrone tracks {label, mags: {key: [..]}}.
// Runtime kinds built by panes: pairs {x, y, x2, y2, mask?}, refline {slope, intercept},
//   cut {axis, value} | {slope, intercept}, vector {at?, av, dims?} (reddening arrow).
// `space:"sky"` layers are left to the sky pane and skipped here.
//
// Isochrone tracks (pk-isochrones/1 {label, dist_pc, mass, mags: {filterKey: [..]}}) follow
// data/isochrones.js trackXY (section 7.4): every expression base key resolves to a track
// magnitude key through data/filters.js resolveKey (state.colmap: column key -> ColMeta.filter,
// exact, case-insensitive, short name), and each magnitude is shifted by
// 5·log10(dist_pc/10) + dm + A_V·A_λ/A_V. A track without dist_pc is taken as apparent (no
// distance modulus). Scatter mode reproduces trackXY exactly (tests/golden/physics/track_xy.json).
//
// A_λ/A_V: with state.filters (pk-filters/1, keyed by filter key) it is data/extinction.js
// entryK of the filter that the key resolves to (state.colmap, state.ext, state.law); with A_V
// != 0 an unknown filter makes the track point NaN, as in trackXY. Without state.filters,
// state.alav (function (key, rv) | {key: value}) is used and unknown values count as 0.
// The reddening vector resolves the axis base (column) keys the same way.

import {compileLinear, dimExpr, goodScale, magPredicate} from '../math/fold.js';
import {shapePath, arrowPath} from './canvas2d.js';
import {distanceModulus} from '../data/isochrones.js';
import {resolveKey, filtersMap} from '../data/filters.js';
import {entryK} from '../data/extinction.js';

const DEFAULT_COLOR = '#2b6cb0';
const MIN_OPACITY = 0.02;
const REFLINE_SAMPLES = 96;

// ------------------------------------------------------------------ id resolution

/** Overlay id of an isochrone label (twin of portalkit.config.overlay_slug). */
export function overlaySlug(label) {
  return String(label ?? '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
}

/**
 * Resolve scatter overlay ids (IMPLEMENTATION_NOTES 22): (a) `space:"dim"` layers of
 * overlays.json, (b) isochrone track labels or their slugs, (c) `<law lower>_vector` for an
 * attached extinction dataset. Unknown ids are returned in `missing`.
 * sources: {overlays?: pk-overlays doc, isochrones?: pk-isochrones doc, extinction?: doc | law,
 *           trackStyle?, vectorStyle?, vectorAv = 10}
 */
export function resolveOverlays(ids, sources = {}) {
  const want = Array.isArray(ids) ? ids : (ids == null ? [] : [ids]);
  const dimLayers = ((sources.overlays && sources.overlays.layers) || []).filter((l) => l && l.space === 'dim');
  const tracks = (sources.isochrones && sources.isochrones.tracks) || [];
  const ext = sources.extinction;
  const law = ext ? (typeof ext === 'string' ? ext : (ext.law || 'G23')) : null;
  const layers = [];
  const missing = [];
  for (const id of want) {
    const L = dimLayers.find((l) => l.id === id);
    if (L) { layers.push(L); continue; }
    const t = tracks.find((tr) => tr && (tr.label === id || overlaySlug(tr.label) === id));
    if (t) {
      layers.push({
        id: overlaySlug(t.label), name: t.label, space: 'dim', kind: 'track',
        style: {color: '#c05621', width: 1.5, ...(sources.trackStyle || {})},
        items: [{label: t.label, track: t, mags: t.mags, mass: t.mass, dist_pc: t.dist_pc, age_myr: t.age_myr}],
      });
      continue;
    }
    if (law && id === `${overlaySlug(law)}_vector`) {
      layers.push({
        id, name: `${law} reddening`, space: 'dim', kind: 'vector',
        style: {color: '#9b2c2c', width: 1.5, ...(sources.vectorStyle || {})},
        items: [{av: sources.vectorAv ?? 10, label: `A_V = ${sources.vectorAv ?? 10}`}],
      });
      continue;
    }
    missing.push(id);
  }
  return {layers, missing};
}

/**
 * A_λ/A_V lookup from a pk-filters/1 document. Returns (key, rv?) -> number. `key` may be a
 * filter key ('JWST/NIRCam.F410M') or a column / expression key ('f410m'): it is resolved
 * with data/filters.js resolveKey (colmap: column key -> ColMeta.filter, then exact,
 * case-insensitive, alias and short-name matches). The value is data/extinction.js entryK:
 * a_over_av['<law>_<rv>'], else linear in 1/R_V between tabulated values, else the
 * pk-extinction/1 curve `ext` at the filter wavelength; NaN when unknown.
 */
export function alavFromFilters(filtersDoc, law = 'G23', rv = 3.1, colmap = null, ext = null) {
  const F = filtersMap(filtersDoc);
  const keys = Object.keys(F);
  const L = law || (ext && ext.law) || 'G23';
  return (key, rvArg) => {
    const k = resolveKey(key, keys, colmap);
    if (k === null) return NaN;
    const r = Number(rvArg) > 0 ? Number(rvArg) : (Number(rv) > 0 ? Number(rv) : 3.1);
    return entryK(F[k], r, ext, L);
  };
}

function alavFn(state) {
  const a = state.alav;
  if (typeof a === 'function') return (k) => Number(a(k, state.rv));
  if (a && typeof a === 'object') return (k) => Number(a[k]);
  return () => NaN;
}

/** The A_λ/A_V function of a draw state (see the header). */
export function alavOf(state = {}) {
  if (state.filters) {
    const law = state.law || (state.ext && state.ext.law) || 'G23';
    return alavFromFilters(state.filters, law, state.rv, state.colmap || null, state.ext || null);
  }
  return alavFn(state);
}

// ------------------------------------------------------------------ projectors

function safeCompile(expr) {
  if (expr === null || expr === undefined || expr === '') return null;
  try {
    return compileLinear(expr);
  } catch (err) {
    if (err instanceof SyntaxError) return null;
    throw err;
  }
}

function sameExpr(a, b) {
  const A = safeCompile(a), B = safeCompile(b);
  if (!A || !B) return false;
  if (Math.abs(A.c - B.c) > 1e-12) return false;
  const keys = new Set([...A.keys, ...B.keys]);
  for (const k of keys) if (Math.abs((A.w[k] || 0) - (B.w[k] || 0)) > 1e-12) return false;
  return true;
}

/** Value of a linear expression given a record {key: v}; NaN when a key is missing. */
function evalLinear(e, rec, exprKey) {
  if (exprKey != null && Number.isFinite(rec[exprKey])) return rec[exprKey];
  let v = e.c;
  for (const k of e.keys) {
    const b = rec[k];
    if (!Number.isFinite(b)) return NaN;
    v += e.w[k] * b;
  }
  return v;
}

/** Shift of a linear expression under per-base shifts (delta mode, constants dropped). */
function evalDelta(e, delta) {
  let v = 0;
  for (const k of e.keys) {
    const d = delta(k);
    if (Number.isFinite(d)) v += e.w[k] * d;
  }
  return v;
}

/**
 * Frame dims as objects {key, center, scale, expr?}. String entries (the `frame:<pane>` store
 * value) take their standardization from dimMeta[key] = {center, scale, expr?}.
 */
export function normalizeFrameDims(dims, dimMeta = null) {
  return (Array.isArray(dims) ? dims : []).map((d) => {
    if (d && typeof d === 'object') return d;
    const key = String(d);
    const m = (dimMeta && (dimMeta instanceof Map ? dimMeta.get(key) : dimMeta[key])) || {};
    const out = {key, center: m.center ?? 0, scale: m.scale ?? 1};
    if (m.expr !== undefined && m.expr !== null) out.expr = m.expr;
    return out;
  });
}

function frameMatrix(frame, dims) {
  const D = dims.length;
  const F = new Float64Array(2 * D);
  if (frame.F && frame.F.length >= 2 * D) {
    F.set(frame.F.subarray ? frame.F.subarray(0, 2 * D) : frame.F.slice(0, 2 * D));
  } else if (Array.isArray(frame.rows)) {
    dims.forEach((d, i) => {
      F[i] = Number((frame.rows[0] || {})[d.key]) || 0;
      F[D + i] = Number((frame.rows[1] || {})[d.key]) || 0;
    });
  }
  return F;
}

/**
 * Build a projector for the view/state. Returns
 *   {mode: 'frame'|'scatter', point(rec) -> [x, y] | null, delta(fn) -> [dx, dy],
 *    coverage(keys: Set) -> opacity in [0, 1]}
 * or null when the view has no usable coordinates.
 */
export function makeProjector(view, state = {}) {
  const frame = state.frame || (view && view.frame) || null;
  if (frame && Array.isArray(frame.dims) && frame.dims.length) {
    const dims = normalizeFrameDims(frame.dims, state.dimMeta);
    const D = dims.length;
    const F = frameMatrix(frame, dims);
    const exprs = dims.map((d) => {
      try {
        return dimExpr(d);
      } catch (err) {
        if (err instanceof SyntaxError) return null;
        throw err;
      }
    });
    const ctr = dims.map((d) => (Number.isFinite(Number(d.center)) ? Number(d.center) : 0));
    const sc = dims.map((d) => goodScale(d.scale ?? 1));
    const wt = dims.map((_, i) => F[i] * F[i] + F[D + i] * F[D + i]);
    const totalW = wt.reduce((s, v) => s + v, 0);
    const baseKeys = [];
    exprs.forEach((e, i) => {
      if (!e || !(wt[i] > 0)) return;
      for (const k of e.keys) if (!baseKeys.includes(k)) baseKeys.push(k);
    });
    const dimValue = (i, rec) => {
      const d = dims[i];
      if (Number.isFinite(rec[d.key])) return rec[d.key];
      const e = exprs[i];
      return e ? evalLinear(e, rec, null) : NaN;
    };
    return {
      mode: 'frame',
      baseKeys,
      point(rec) {
        let x = 0, y = 0, any = false;
        for (let i = 0; i < D; i++) {
          if (!(wt[i] > 0)) continue;
          const v = dimValue(i, rec);
          if (!Number.isFinite(v)) continue;
          const u = (v - ctr[i]) / sc[i];
          x += F[i] * u;
          y += F[D + i] * u;
          any = true;
        }
        return any ? [x, y] : null;
      },
      delta(fn) {
        let x = 0, y = 0;
        for (let i = 0; i < D; i++) {
          const e = exprs[i];
          if (!e || !(wt[i] > 0)) continue;
          const dv = evalDelta(e, fn);
          x += (F[i] * dv) / sc[i];
          y += (F[D + i] * dv) / sc[i];
        }
        return [x, y];
      },
      coverage(keys) {
        if (!(totalW > 0)) return 0;
        let unc = 0;
        for (let i = 0; i < D; i++) {
          if (!(wt[i] > 0)) continue;
          const d = dims[i];
          if (keys.has(d.key)) continue;
          const e = exprs[i];
          if (e && e.keys.length && e.keys.every((k) => keys.has(k))) continue;
          unc += wt[i];
        }
        return Math.min(1, Math.max(0, 1 - unc / totalW));
      },
    };
  }
  const xe = state.x ?? (view && view.x && view.x.expr);
  const ye = state.y ?? (view && view.y && view.y.expr);
  const X = safeCompile(xe), Y = safeCompile(ye);
  if (!X || !Y) return null;
  return {
    mode: 'scatter',
    xExpr: xe,
    yExpr: ye,
    baseKeys: [...new Set([...X.keys, ...Y.keys])],
    point(rec) {
      const x = evalLinear(X, rec, xe), y = evalLinear(Y, rec, ye);
      return Number.isFinite(x) && Number.isFinite(y) ? [x, y] : null;
    },
    delta(fn) { return [evalDelta(X, fn), evalDelta(Y, fn)]; },
    coverage(keys) {
      const okX = keys.has(xe) || X.keys.every((k) => keys.has(k));
      const okY = keys.has(ye) || Y.keys.every((k) => keys.has(k));
      return okX && okY ? 1 : 0;
    },
  };
}

// ------------------------------------------------------------------ item -> polylines in data space

function recordsFromDims(dims, shift) {
  const keys = Object.keys(dims || {});
  let n = 0;
  for (const k of keys) {
    const a = dims[k];
    const len = Array.isArray(a) || ArrayBuffer.isView(a) ? a.length : 1;
    n = Math.max(n, len);
  }
  const recs = [];
  for (let i = 0; i < n; i++) {
    const rec = {};
    for (const k of keys) {
      const a = dims[k];
      const v = Array.isArray(a) || ArrayBuffer.isView(a) ? Number(a[i]) : Number(a);
      if (Number.isFinite(v)) rec[k] = v + (shift ? shift(k) : 0);
    }
    recs.push(rec);
  }
  return {recs, keys: new Set(keys)};
}

/**
 * Data-space point lists of one item: {lines: [[[x, y] | null, ...]], coverage}.
 * null entries break a polyline (missing values).
 */
function itemPoints(item, proj, shift, trk) {
  if (!item || !proj) return {pts: [], coverage: 0};
  if (trk && item.mags && typeof item.mags === 'object') {
    const {recs, keys} = trackRecords(item, proj.baseKeys || [], trk);
    return {pts: recs.map((r) => proj.point(r)), coverage: proj.coverage(keys)};
  }
  if (Array.isArray(item.pts) && (item.x !== undefined || item.y !== undefined)) {
    if (proj.mode === 'scatter') {
      const ok = (item.x === undefined || sameExpr(item.x, proj.xExpr)) && (item.y === undefined || sameExpr(item.y, proj.yExpr));
      if (!ok) {
        // swapped axes are still the same 2D plane
        const sw = item.x !== undefined && item.y !== undefined && sameExpr(item.x, proj.yExpr) && sameExpr(item.y, proj.xExpr);
        if (!sw) return {pts: [], coverage: 0};
        return {pts: item.pts.map((p) => (p && Number.isFinite(+p[0]) && Number.isFinite(+p[1]) ? [+p[1], +p[0]] : null)), coverage: 1};
      }
      return {pts: item.pts.map((p) => (p && Number.isFinite(+p[0]) && Number.isFinite(+p[1]) ? [+p[0], +p[1]] : null)), coverage: 1};
    }
    // frame: x/y expressions are dims (or base-key expressions) of the projection
    const xk = String(item.x), yk = String(item.y);
    const XE = safeCompile(item.x), YE = safeCompile(item.y);
    const keys = new Set([xk, yk]);
    const pts = item.pts.map((p) => {
      if (!p || !Number.isFinite(+p[0]) || !Number.isFinite(+p[1])) return null;
      const rec = {[xk]: +p[0], [yk]: +p[1]};
      // single-key expressions also provide the base value
      if (XE && XE.keys.length === 1 && XE.c === 0 && XE.w[XE.keys[0]] === 1) rec[XE.keys[0]] = +p[0];
      if (YE && YE.keys.length === 1 && YE.c === 0 && YE.w[YE.keys[0]] === 1) rec[YE.keys[0]] = +p[1];
      return proj.point(rec);
    });
    if (XE && XE.keys.length === 1) keys.add(XE.keys[0]);
    if (YE && YE.keys.length === 1) keys.add(YE.keys[0]);
    return {pts, coverage: proj.coverage(keys)};
  }
  const dims = item.dims || item.mags || null;
  if (dims) {
    const {recs, keys} = recordsFromDims(dims, shift);
    const pts = recs.map((r) => proj.point(r));
    return {pts, coverage: proj.coverage(keys)};
  }
  if (Number.isFinite(+item.x) && Number.isFinite(+item.y) && proj.mode === 'scatter') {
    return {pts: [[+item.x, +item.y]], coverage: 1};
  }
  return {pts: [], coverage: 0};
}

/**
 * Records {baseKey: apparent magnitude} of an isochrone track for the projector's base keys
 * (data/isochrones.js trackXY semantics). trk: {colmap, dm, av, alav(filterKey), strictA}.
 * Base keys that resolve to no track magnitude are left out (not covered).
 */
export function trackRecords(item, baseKeys, trk) {
  const track = item.track || item;
  const mags = track.mags || item.mags || {};
  const mkeys = Object.keys(mags);
  const dist = track.dist_pc ?? item.dist_pc;
  const mu = dist === undefined || dist === null ? 0 : distanceModulus(dist);
  const n = Array.isArray(track.mass) || ArrayBuffer.isView(track.mass)
    ? track.mass.length
    : mkeys.reduce((m, k) => Math.max(m, (mags[k] || []).length), 0);
  const cols = [];
  const keys = new Set();
  for (const b of baseKeys) {
    const mk = resolveKey(b, mkeys, trk.colmap || null);
    if (mk === null) continue;
    let shift = mu + trk.dm;
    if (trk.av !== 0) {
      const r = trk.alav(mk);
      if (Number.isFinite(r)) shift += trk.av * r;
      else if (trk.strictA) shift = NaN;
    }
    cols.push([b, mags[mk], shift]);
    keys.add(b);
  }
  const recs = [];
  for (let i = 0; i < n; i++) {
    const rec = {};
    for (const [b, m, shift] of cols) {
      const v = m && m[i] !== null && m[i] !== undefined ? +m[i] + shift : NaN;
      if (Number.isFinite(v)) rec[b] = v;
    }
    recs.push(rec);
  }
  return {recs, keys};
}

// ------------------------------------------------------------------ drawing helpers

function styleOf(layer, item) {
  const s = {...(layer.style || {}), ...((item && item.style) || {})};
  return {
    color: s.color || layer.color || DEFAULT_COLOR,
    width: Number(s.width) > 0 ? Number(s.width) : 1.25,
    dash: Array.isArray(s.dash) ? s.dash : (s.dash === true ? [5, 4] : (typeof s.dash === 'string' && s.dash ? s.dash.split(/[ ,]+/).map(Number).filter((v) => v >= 0) : [])),
    shape: s.shape || 'circle',
    opacity: Number.isFinite(Number(s.opacity)) ? Math.min(1, Math.max(0, Number(s.opacity))) : 1,
    fill: s.fill === true ? (s.color || DEFAULT_COLOR) : (s.fill || null),
    size: Number(s.size) > 0 ? Number(s.size) : 7,
    fontSize: Number(s.fontSize) > 0 ? Number(s.fontSize) : 11,
  };
}

function px(view, p) {
  if (!p) return null;
  const x = view.sx.f(p[0]), y = view.sy.f(p[1]);
  return Number.isFinite(x) && Number.isFinite(y) ? [x, y] : null;
}

/** Trace a data-space polyline; null / unprojectable points break it. Returns segments drawn. */
function tracePolyline(ctx, view, pts, close = false) {
  let pen = false, segs = 0, first = null, broken = false;
  for (const p of pts) {
    const q = px(view, p);
    if (!q) { pen = false; broken = true; continue; }
    if (!pen) { ctx.moveTo(q[0], q[1]); pen = true; if (!first) first = q; }
    else { ctx.lineTo(q[0], q[1]); segs++; }
  }
  if (close && first && !broken && segs >= 2) ctx.closePath();
  return segs;
}

function label(ctx, view, p, text, st, dx = 4, dy = -4) {
  const q = px(view, p);
  if (!q || text == null || text === '') return;
  ctx.save();
  ctx.font = `${st.fontSize}px system-ui,-apple-system,"Segoe UI",Roboto,sans-serif`;
  ctx.fillStyle = st.color;
  ctx.textAlign = 'left';
  ctx.textBaseline = 'bottom';
  ctx.fillText(String(text), q[0] + dx, q[1] + dy);
  ctx.restore();
}

function lastFinite(pts) {
  for (let i = pts.length - 1; i >= 0; i--) if (pts[i]) return pts[i];
  return null;
}

/** Sample the data-space line y = slope·x + intercept across the view's x domain. */
function lineSamples(view, slope, intercept) {
  const s = view.sx;
  const t0 = s.tf(s.d0), t1 = s.tf(s.d1);
  const nonlin = view.sx.kind === 'log' || view.sy.kind === 'log';
  const n = nonlin ? REFLINE_SAMPLES : 2;
  const pts = [];
  for (let k = 0; k < n; k++) {
    const x = s.itf(t0 + ((t1 - t0) * k) / (n - 1));
    const y = slope * x + intercept;
    pts.push(Number.isFinite(x) && Number.isFinite(y) ? [x, y] : null);
  }
  return pts;
}

function arrowBetween(ctx, a, b, head) {
  const dx = b[0] - a[0], dy = b[1] - a[1];
  const len = Math.hypot(dx, dy);
  if (!(len > 0.5)) return false;
  arrowPath(ctx, a[0], a[1], dx / len, dy / len, len, Math.min(head, len * 0.5));
  return true;
}

// ------------------------------------------------------------------ main entry

/**
 * Draw overlay layers. state: {av = 0, rv = 3.1, dm = 0, frame?, dimMeta?, filters?, colmap?,
 * ext?, law?, alav?, isMag?, x?, y?, hidden?: Set|string[] of layer ids} (see the header). opts: {clip = true}. Returns per-layer
 * [{id, opacity, drawn}] (drawn = items or segments drawn) for legends and tests.
 */
export function drawOverlays(ctx, view, layers, state = {}, opts = {}) {
  const out = [];
  if (!Array.isArray(layers) || !layers.length || !view) return out;
  const proj = makeProjector(view, state);
  const hidden = state.hidden ? new Set(state.hidden) : null;
  const av = Number(state.av) || 0;
  const dm = Number(state.dm) || 0;
  const alav = alavOf(state);
  const trk = {colmap: state.colmap || null, dm, av, alav, strictA: !!state.filters};
  const isMag = state.isMag ? magPredicate(state.isMag) : () => true;
  const P = view.plot;
  ctx.save();
  if (opts.clip !== false) {
    ctx.beginPath();
    ctx.rect(P.left, P.top, P.width, P.height);
    ctx.clip();
  }
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  for (const layer of layers) {
    if (!layer || layer.space === 'sky' || layer.visible === false) continue;
    if (hidden && hidden.has(layer.id)) continue;
    const res = {id: layer.id ?? null, opacity: 0, drawn: 0};
    out.push(res);
    const kind = layer.kind || 'polyline';
    const items = Array.isArray(layer.items) ? layer.items : [];
    if (kind === 'pairs') { drawPairs(ctx, view, layer, res); continue; }
    if (kind === 'refline' || kind === 'cut') { drawLineLayer(ctx, view, layer, res); continue; }
    if (!proj) continue;
    const shift = kind === 'track'
      ? (k) => (isMag(k) ? dm + (av ? av * (Number.isFinite(alav(k)) ? alav(k) : 0) : 0) : 0)
      : null;
    let layerOpacity = 0;
    for (const item of items) {
      const st = styleOf(layer, item);
      if (kind === 'vector') {
        const op = drawVector(ctx, view, proj, item, st, alav, isMag, state);
        if (op > 0) { res.drawn++; layerOpacity = Math.max(layerOpacity, op); }
        continue;
      }
      if (kind === 'boxes' && proj.mode === 'scatter' && ['x0', 'x1', 'y0', 'y1'].every((k) => Number.isFinite(+item[k]))) {
        ctx.globalAlpha = st.opacity;
        ctx.beginPath();
        tracePolyline(ctx, view, [[+item.x0, +item.y0], [+item.x1, +item.y0], [+item.x1, +item.y1], [+item.x0, +item.y1]], true);
        strokeFill(ctx, st, true);
        res.drawn++;
        layerOpacity = Math.max(layerOpacity, 1);
        continue;
      }
      const {pts, coverage} = itemPoints(item, proj, shift, kind === 'track' ? trk : null);
      const op = st.opacity * coverage;
      if (!(op > MIN_OPACITY) || !pts.length) continue;
      layerOpacity = Math.max(layerOpacity, coverage);
      ctx.globalAlpha = op;
      ctx.setLineDash(st.dash);
      if (kind === 'markers' || kind === 'circles' || kind === 'rings') {
        const r = kind === 'markers' ? st.size / 2 : (Number(item.r_px ?? item.r) > 0 ? Number(item.r_px ?? item.r) : st.size);
        for (const p of pts) {
          const q = px(view, p);
          if (!q) continue;
          ctx.beginPath();
          if (kind === 'markers') shapePath(ctx, item.shape || st.shape, q[0], q[1], r);
          else ctx.arc(q[0], q[1], r, 0, 2 * Math.PI);
          strokeFill(ctx, st, kind !== 'markers' || !!item.hollow || !!(layer.style && layer.style.hollow));
          if (kind === 'rings' && Array.isArray(item.radii)) {
            for (const rr of item.radii) if (rr > 0) { ctx.beginPath(); ctx.arc(q[0], q[1], rr, 0, 2 * Math.PI); ctx.stroke(); }
          }
          res.drawn++;
        }
        if (item.label != null && pts[0]) label(ctx, view, pts.find(Boolean), item.label, st, r + 2, -r);
      } else {
        const close = kind === 'polygons' || kind === 'boxes';
        ctx.beginPath();
        const segs = tracePolyline(ctx, view, pts, close);
        if (segs > 0) {
          strokeFill(ctx, st, !close);
          res.drawn += segs;
        }
        if (item.label != null && (layer.style?.label !== false)) label(ctx, view, lastFinite(pts), item.label, st);
      }
      ctx.setLineDash([]);
    }
    res.opacity = layerOpacity;
  }
  ctx.restore();
  return out;
}

function strokeFill(ctx, st, strokeOnly) {
  if (!strokeOnly && st.fill) {
    const a = ctx.globalAlpha;
    ctx.fillStyle = st.fill;
    ctx.globalAlpha = a * (st.fillOpacity ?? 0.18);
    ctx.fill();
    ctx.globalAlpha = a;
  }
  ctx.strokeStyle = st.color;
  ctx.lineWidth = st.width;
  ctx.stroke();
}

function drawPairs(ctx, view, layer, res) {
  const {x, y, x2, y2} = layer;
  if (!x || !y || !x2 || !y2) return;
  const st = styleOf(layer, null);
  const n = Math.min(x.length, y.length, x2.length, y2.length, layer.n ?? Infinity);
  const mask = layer.mask || null;
  ctx.globalAlpha = st.opacity;
  ctx.strokeStyle = st.color;
  ctx.lineWidth = st.width;
  ctx.setLineDash(st.dash);
  ctx.beginPath();
  for (let i = 0; i < n; i++) {
    if (mask && !mask[i]) continue;
    const a = px(view, [x[i], y[i]]), b = px(view, [x2[i], y2[i]]);
    if (!a || !b) continue;
    ctx.moveTo(a[0], a[1]);
    ctx.lineTo(b[0], b[1]);
    res.drawn++;
  }
  ctx.stroke();
  ctx.setLineDash([]);
  res.opacity = res.drawn ? st.opacity : 0;
}

function drawLineLayer(ctx, view, layer, res) {
  const st = styleOf(layer, null);
  if (layer.kind === 'cut' && !layer.style?.dash && !layer.dash) st.dash = [5, 4];
  ctx.globalAlpha = st.opacity;
  ctx.strokeStyle = st.color;
  ctx.lineWidth = st.width;
  ctx.setLineDash(st.dash);
  let pts = null;
  if (layer.kind === 'cut' && (layer.axis === 'x' || layer.axis === 'y')) {
    const v = Number(layer.value);
    if (!Number.isFinite(v)) { ctx.setLineDash([]); return; }
    pts = layer.axis === 'x' ? [[v, view.y.d0], [v, view.y.d1]] : [[view.x.d0, v], [view.x.d1, v]];
  } else {
    const slope = Number.isFinite(Number(layer.slope)) ? Number(layer.slope) : 1;
    const icpt = Number.isFinite(Number(layer.intercept)) ? Number(layer.intercept) : 0;
    pts = lineSamples(view, slope, icpt);
  }
  ctx.beginPath();
  const segs = tracePolyline(ctx, view, pts, false);
  if (segs) ctx.stroke();
  ctx.setLineDash([]);
  res.drawn = segs;
  res.opacity = segs ? st.opacity : 0;
  if (segs && layer.label) {
    // label near the end that lies inside the plot
    const P = view.plot;
    let at = null;
    for (let i = pts.length - 1; i >= 0; i--) {
      const q = px(view, pts[i]);
      if (q && q[0] >= P.left && q[0] <= P.right - 8 && q[1] >= P.top + 12 && q[1] <= P.bottom) { at = pts[i]; break; }
    }
    if (at) label(ctx, view, at, layer.label, st, -4 - 6 * String(layer.label).length, -3);
  }
}

/**
 * Reddening vector. Item forms:
 *   {from: [x, y], to: [x, y]}            explicit data-space arrow
 *   {x, y, dx, dy}                        data-space arrow
 *   {av, dims?: {key: [v]}, at?: [fx, fy]} A_V arrow; starts at the item's projected point, or
 *                                         at plot fraction `at` (default [0.12, 0.18] from the
 *                                         top-left), length from A_λ/A_V of each base key
 * Returns the drawn opacity (0 when nothing was drawn).
 */
function drawVector(ctx, view, proj, item, st, alav, isMag, state) {
  let a = null, b = null, cov = 1;
  if (Array.isArray(item.from) && Array.isArray(item.to)) {
    a = [+item.from[0], +item.from[1]];
    b = [+item.to[0], +item.to[1]];
  } else if (Number.isFinite(+item.dx) && Number.isFinite(+item.dy) && Number.isFinite(+item.x) && Number.isFinite(+item.y)) {
    a = [+item.x, +item.y];
    b = [+item.x + +item.dx, +item.y + +item.dy];
  } else {
    const av = Number(item.av ?? state.vectorAv ?? 10);
    if (!(Number.isFinite(av) && av !== 0)) return 0;
    let missing = false;
    const fn = (k) => {
      if (!isMag(k)) return 0;
      const r = alav(k);
      if (!Number.isFinite(r)) { missing = true; return 0; }
      return av * r;
    };
    const d = proj.delta(fn);
    if (missing) cov = 0.5;            // some bases have no A_λ/A_V: draw faint
    if (item.dims) {
      const {recs, keys} = recordsFromDims(item.dims, null);
      a = recs.length ? proj.point(recs[0]) : null;
      cov *= proj.coverage(keys);
    }
    const P = view.plot;
    const at = Array.isArray(item.at) ? item.at : [0.12, 0.18];
    if (a) {
      b = [a[0] + d[0], a[1] + d[1]];
    } else {
      // anchor on screen; arrow length from the data-space delta through the local scale
      const q0 = [P.left + at[0] * P.width, P.top + at[1] * P.height];
      const t0 = [view.sx.tf(view.sx.inv(q0[0])), view.sy.tf(view.sy.inv(q0[1]))];
      a = [view.sx.itf(t0[0]), view.sy.itf(t0[1])];
      b = [a[0] + d[0], a[1] + d[1]];
    }
  }
  const pa = px(view, a), pb = px(view, b);
  if (!pa || !pb) return 0;
  const op = st.opacity * cov;
  if (!(op > MIN_OPACITY)) return 0;
  ctx.globalAlpha = op;
  ctx.strokeStyle = st.color;
  ctx.lineWidth = st.width;
  ctx.setLineDash([]);
  ctx.beginPath();
  if (!arrowBetween(ctx, pa, pb, 6)) return 0;
  ctx.stroke();
  if (item.label != null) label(ctx, view, b, item.label, st);
  return op;
}
