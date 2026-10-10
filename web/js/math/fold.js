// Folding virtual dims into base-column weights (ARCHITECTURE.md section 7.3).
// Pure module: no DOM access, loadable under `node --test`.
//
// A frame F (2 x D over displayed dims, which may be virtual expressions) with per-dim
// expression expr_d = Σ_b w_db·base_b + c_d and standardization (v - center_d)/scale_d gives
//
//   x_r = Σ_d F[r,d]·(expr_d - center_d)/scale_d = Σ_b W[r,b]·base_b + K_r
//   W[r,b] = Σ_d F[r,d]·w_db/scale_d,   K_r = Σ_d F[r,d]·(c_d - center_d)/scale_d
//
// The distance modulus dm adds dm to every magnitude base column; it folds into K as
// K_r += dm·Σ_{b mag} W[r,b]. Support = bases with |W| > 1e-9 in either row.
//
// The GPU path draws X' = Σ W·base (no constant) and shifts the camera instead
// (foldCamera); the worker path receives {terms, c} for DataClient.project.

import {FADE_WIDTH} from '../core/constants.js';
import {compile as compileExpr, isKey} from '../data/expr.js';

export const SUPPORT_EPS = 1e-9;

/**
 * Compile a linear expression (section 4.4 grammar) to {w: {key: coeff} (null prototype), c,
 * keys}. Text goes through data/expr.js compile (the pure grammar module shared with the
 * Python twin, portalkit/data/expr_vectors.json), so this accepts and rejects exactly what the
 * data layer does: no unary sign, finite numbers only, and keys whose weights cancel ('a-a')
 * stay in `keys` with weight 0 so a missing value still marks the row missing.
 * An already compiled {w, c} is normalized (non-finite weights dropped). Throws SyntaxError
 * (ExprError) on bad input.
 */
export function compileLinear(expr) {
  if (expr && typeof expr === 'object') {
    const w = Object.create(null);
    const keys = [];
    const src = expr.w || {};
    for (const k of Object.keys(src)) {
      const v = Number(src[k]);
      if (!Number.isFinite(v)) continue;
      if (!(k in w)) { w[k] = 0; keys.push(k); }
      w[k] += v;
    }
    const c = Number(expr.c);
    return {w, c: Number.isFinite(c) ? c : 0, keys};
  }
  if (typeof expr !== 'string') throw new SyntaxError(`expression must be a string, got ${expr === null ? 'null' : typeof expr}`);
  const r = compileExpr(expr);
  // a private copy: compileCached results are shared and callers may hold on to `w`
  const w = Object.create(null);
  for (const k of r.keys) w[k] = r.w[k];
  return {w, c: r.c, keys: r.keys.slice()};
}

/**
 * Expression of a dim: d.expr when present; else d.key itself, which is a stored base column
 * when it matches the stored-key pattern and otherwise a virtual-dim expression (manifest
 * virtual dims use the expression text as key, section 4.4 / 5.2).
 */
export function dimExpr(d) {
  if (d.expr !== undefined && d.expr !== null && d.expr !== '') return compileLinear(d.expr);
  if (isKey(d.key)) {
    const w = Object.create(null);
    w[d.key] = 1;
    return {w, c: 0, keys: [d.key]};
  }
  return compileLinear(String(d.key));
}

export function goodScale(s) {
  const v = Number(s);
  return Number.isFinite(v) && v !== 0 ? v : 1;
}

export function magPredicate(isMag) {
  if (!isMag) return () => false;
  if (typeof isMag === 'function') return isMag;
  if (isMag instanceof Set) return (k) => isMag.has(k);
  if (Array.isArray(isMag)) {
    const s = new Set(isMag);
    return (k) => s.has(k);
  }
  if (typeof isMag === 'object') return (k) => !!isMag[k];
  return () => false;
}

/**
 * Normalize frame rows. Accepts [{dimKey: w}, {dimKey: w}] or a column-major Float64Array
 * (vendor frame layout, 2·D entries aligned with dims). Non-finite weights are dropped.
 */
export function frameRowsOf(frameRows, dims) {
  if (frameRows && (ArrayBuffer.isView(frameRows) || (Array.isArray(frameRows) && typeof frameRows[0] === 'number'))) {
    const D = frameRows.length >> 1;
    const rows = [{}, {}];
    for (let d = 0; d < Math.min(D, dims.length); d++) {
      const a = frameRows[d], b = frameRows[D + d];
      if (Number.isFinite(a) && a !== 0) rows[0][dims[d].key] = a;
      if (Number.isFinite(b) && b !== 0) rows[1][dims[d].key] = b;
    }
    return rows;
  }
  const rows = [{}, {}];
  for (let r = 0; r < 2; r++) {
    const src = (frameRows && frameRows[r]) || {};
    for (const k of Object.keys(src)) {
      const v = Number(src[k]);
      if (Number.isFinite(v) && v !== 0) rows[r][k] = v;
    }
  }
  return rows;
}

/**
 * fold(frameRows, dims, opts?) -> {terms: [{w: Float64Array, keys}, {w, keys}], c: [K0, K1],
 *   support: string[], W: [Map, Map]}
 * dims: [{key, center = 0, scale = 1, expr?: string | {w, c}}]. A frame key without a dims
 * entry is treated as a stored base column (center 0, scale 1).
 * opts: {dm = 0, isMag: (key) => bool | Set | string[] | {key: true}, eps = 1e-9}.
 * Both terms share the same keys array (= support, in first-appearance order).
 */
export function fold(frameRows, dims, opts = {}) {
  const dimList = Array.isArray(dims) ? dims : [];
  const byKey = new Map();
  for (const d of dimList) byKey.set(d.key, d);
  const rows = frameRowsOf(frameRows, dimList);
  const eps = opts.eps ?? SUPPORT_EPS;
  const W = [new Map(), new Map()];
  const K = [0, 0];
  const order = [];
  const seen = new Set();
  for (let r = 0; r < 2; r++) {
    for (const key of Object.keys(rows[r])) {
      const f = rows[r][key];
      const d = byKey.get(key) || {key};
      const e = dimExpr(d);
      const sc = goodScale(d.scale ?? 1);
      const ctr = Number.isFinite(Number(d.center)) ? Number(d.center) : 0;
      for (const b of Object.keys(e.w)) {
        W[r].set(b, (W[r].get(b) || 0) + (f * e.w[b]) / sc);
        if (!seen.has(b)) { seen.add(b); order.push(b); }
      }
      K[r] += (f * ((Number(e.c) || 0) - ctr)) / sc;
    }
  }
  const support = order.filter((b) => Math.abs(W[0].get(b) || 0) > eps || Math.abs(W[1].get(b) || 0) > eps);
  const dm = Number(opts.dm) || 0;
  if (dm) {
    const mag = magPredicate(opts.isMag);
    for (const b of support) if (mag(b)) for (let r = 0; r < 2; r++) K[r] += dm * (W[r].get(b) || 0);
  }
  const keys = support.slice();
  const terms = [0, 1].map((r) => {
    const w = new Float64Array(keys.length);
    keys.forEach((b, i) => { w[i] = W[r].get(b) || 0; });
    return {w, keys};
  });
  return {terms, c: K, support, W};
}

/** One-hot frame rows for a scatter view (x expr on row 0, y expr on row 1). */
export function oneHotRows(xKey, yKey) {
  return [{[xKey]: 1}, {[yKey]: 1}];
}

/** Dims for a scatter view: each axis expression is its own dim (center 0, scale 1). */
export function scatterDims(xExpr, yExpr) {
  const out = [{key: xExpr, center: 0, scale: 1, expr: xExpr}];
  if (yExpr !== xExpr) out.push({key: yExpr, center: 0, scale: 1, expr: yExpr});
  return out;
}

/**
 * Camera for the GPU path, which draws X' = Σ W·base without the constant K:
 * a camera centred at (cx, cy) in true coordinates becomes (cx - K0, cy - K1).
 */
export function foldCamera(camera, c) {
  return {...camera, cx: camera.cx - (c[0] || 0), cy: camera.cy - (c[1] || 0)};
}

/** Folded weights as a vendor-layout frame Float64Array(2·S) over the support keys. */
export function foldedFrame(folded) {
  const S = folded.support.length;
  const F = new Float64Array(2 * S);
  F.set(folded.terms[0].w, 0);
  F.set(folded.terms[1].w, S);
  return F;
}

/** Missing-data fade factor per support key: clamp(1 - hypot(W0, W1)/FADE_WIDTH, 0, 1). */
export function foldFade(folded, width = FADE_WIDTH) {
  const S = folded.support.length;
  const out = new Float32Array(S);
  const w0 = folded.terms[0].w, w1 = folded.terms[1].w;
  for (let i = 0; i < S; i++) out[i] = Math.min(1, Math.max(0, 1 - Math.hypot(w0[i], w1[i]) / width));
  return out;
}

/**
 * Evaluate a folded projection on CPU (tier T0 / tests).
 * cols: {key: ArrayLike<number>} for every support key (NaN = missing); n rows.
 * opts.fade: true applies the GC missing-data semantics (a missing base contributes 0 and
 * multiplies visibility by its fade factor); false (default) makes any missing base hide
 * the row (x = y = NaN, vis = 0). opts.vis: optional input visibility (0/1 or 0..255).
 * Returns {x: Float32Array, y: Float32Array, vis: Uint8Array (0..255)}.
 */
export function projectRows(folded, cols, n, opts = {}) {
  const keys = folded.support;
  const S = keys.length;
  const w0 = folded.terms[0].w, w1 = folded.terms[1].w;
  const K0 = folded.c[0], K1 = folded.c[1];
  const x = new Float32Array(n), y = new Float32Array(n), vis = new Uint8Array(n);
  const fade = opts.fade ? foldFade(folded) : null;
  const src = keys.map((k) => cols[k]);
  const visIn = opts.vis || null;
  let visScale = 1;
  if (visIn) {
    let mx = 0;
    for (let i = 0; i < Math.min(n, visIn.length); i++) if (visIn[i] > mx) mx = visIn[i];
    visScale = mx > 1 ? 1 / 255 : 1;
  }
  for (let i = 0; i < n; i++) {
    let X = K0, Y = K1, v = visIn ? visIn[i] * visScale : 1;
    let bad = false;
    for (let s = 0; s < S; s++) {
      const col = src[s];
      const val = col ? col[i] : NaN;
      if (!Number.isFinite(val)) {
        if (fade) { v *= fade[s]; continue; }
        bad = true;
        break;
      }
      X += w0[s] * val;
      Y += w1[s] * val;
    }
    if (bad || !(v >= 0.01)) {
      x[i] = NaN; y[i] = NaN; vis[i] = 0;
      continue;
    }
    x[i] = X; y[i] = Y;
    vis[i] = Math.max(1, Math.round(255 * Math.min(1, v)));
  }
  return {x, y, vis};
}

/**
 * Direct (unfolded) evaluation of one frame row for a single record, for tests and
 * overlays: Σ_d F[r,d]·(expr_d(rec) - center_d)/scale_d (+ dm on magnitude bases).
 * rec: {baseKey: value}. Returns NaN when a needed base is missing.
 */
export function directEval(frameRows, dims, rec, r, opts = {}) {
  const rows = frameRowsOf(frameRows, dims);
  const byKey = new Map(dims.map((d) => [d.key, d]));
  const dm = Number(opts.dm) || 0;
  const mag = magPredicate(opts.isMag);
  let out = 0;
  for (const key of Object.keys(rows[r])) {
    const d = byKey.get(key) || {key};
    const e = dimExpr(d);
    let v = Number(e.c) || 0;
    for (const b of Object.keys(e.w)) {
      const bv = Number(rec[b]);
      if (!Number.isFinite(bv)) return NaN;
      v += e.w[b] * (bv + (mag(b) ? dm : 0));
    }
    out += (rows[r][key] * (v - (Number(d.center) || 0))) / goodScale(d.scale ?? 1);
  }
  return out;
}
