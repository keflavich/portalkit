// Worker projection (ARCHITECTURE.md sections 6.6 and 7.3): x = Σ w0_b·base_b + c0,
// y = Σ w1_b·base_b + c1 over the folded support, with the GC kernel visibility semantics:
//   a missing base value multiplies visf by that base's fade factor (0 = hide, 1 = ignore),
//   vis = 0 when visf < VIS_MIN (0.01), else max(1, round(255·visf)); vis is then multiplied by
//   the filter mask (filtered rows get vis = 0).
// When the support is ≤ MAXD_GPU columns and every support column is stored u16q, the vendored
// kernel.projectRange runs on the raw codes (u = code·step + lo − step). Otherwise the sparse
// loop runs over decoded Float32 values (NaN = missing). Pure module.

import {projectRange, VIS_MIN} from '../../vendor/gc/workers/kernel.js';
import {MAXD_GPU} from '../../core/constants.js';
import {stepOf} from '../../data/codec.js';

/**
 * Normalise a projection spec into {keys, wx: Float64Array, wy: Float64Array, cx, cy, fade}.
 * Support = union of the term keys in first-appearance order (x terms first). Duplicate keys
 * within a term are summed. `fade` is a Float32Array aligned to the support (or a {key: f}
 * object); missing entries default to 0 (missing value hides the point).
 */
export function normSpec(spec) {
  const terms = (spec && spec.terms) || [];
  const keys = [];
  const at = new Map();
  for (let r = 0; r < 2; r++) {
    const t = terms[r];
    if (!t) continue;
    for (const k of t.keys || []) {
      if (!at.has(k)) {
        at.set(k, keys.length);
        keys.push(k);
      }
    }
  }
  const D = keys.length;
  const wx = new Float64Array(D), wy = new Float64Array(D);
  for (let r = 0; r < 2; r++) {
    const t = terms[r];
    if (!t) continue;
    const ks = t.keys || [];
    for (let j = 0; j < ks.length; j++) {
      const w = +((t.w && t.w[j]) ?? 0);
      if (!Number.isFinite(w)) throw new Error(`non-finite projection weight for ${ks[j]}`);
      (r === 0 ? wx : wy)[at.get(ks[j])] += w;
    }
  }
  const fade = new Float32Array(D);
  const f = spec && spec.fade;
  if (f && typeof f === 'object' && !ArrayBuffer.isView(f) && !Array.isArray(f)) {
    for (let d = 0; d < D; d++) {
      const v = f[keys[d]];
      fade[d] = Number.isFinite(+v) ? Math.min(1, Math.max(0, +v)) : 0;
    }
  } else if (f) {
    for (let d = 0; d < D; d++) {
      const v = f[d];
      fade[d] = v !== undefined && Number.isFinite(+v) ? Math.min(1, Math.max(0, +v)) : 0;
    }
  }
  const c = (spec && spec.c) || [0, 0];
  const cx = Number.isFinite(+c[0]) ? +c[0] : 0;
  const cy = Number.isFinite(+c[1]) ? +c[1] : 0;
  return {keys, wx, wy, cx, cy, fade};
}

function applyVisMask(vis, mask, n) {
  if (!mask) return;
  for (let i = 0; i < n; i++) if (!mask[i]) vis[i] = 0;
}

/**
 * Kernel path. cols[d] = {codes: Uint16Array, meta} (stored u16q). Returns {x, y, vis}.
 */
export function projectKernel(ns, cols, n, mask = null) {
  const D = ns.keys.length;
  const raw = new Array(D), ua = new Float64Array(D), ub = new Float64Array(D);
  for (let d = 0; d < D; d++) {
    const st = stepOf(cols[d].meta);
    raw[d] = cols[d].codes;
    ua[d] = st;
    ub[d] = +cols[d].meta.lo - st;
  }
  const F = new Float64Array(2 * D);
  F.set(ns.wx, 0);
  F.set(ns.wy, D);
  const out = {X: new Float32Array(n), Y: new Float32Array(n), vis: new Uint8Array(n), visf: new Float32Array(n)};
  projectRange({n, D, raw, ua, ub, cats: null}, {F, fade: ns.fade, zDim: -1, needDim: -1, masks: null}, out, 0, n);
  if (ns.cx !== 0 || ns.cy !== 0) {
    for (let i = 0; i < n; i++) {
      out.X[i] += ns.cx;
      out.Y[i] += ns.cy;
    }
  }
  applyVisMask(out.vis, mask, n);
  return {x: out.X, y: out.Y, vis: out.vis};
}

/**
 * Sparse path over decoded values. vals[d] = Float32Array (NaN missing). Returns {x, y, vis}.
 * Bases with zero weight in both rows still apply their fade (kernel skips them; here too).
 */
export function projectSparse(ns, vals, n, mask = null) {
  const D = ns.keys.length;
  const x = new Float32Array(n), y = new Float32Array(n), vis = new Uint8Array(n);
  const acx = new Float64Array(n), acy = new Float64Array(n);
  const visf = new Float32Array(n).fill(1);
  for (let d = 0; d < D; d++) {
    const px = ns.wx[d], py = ns.wy[d];
    if (px === 0 && py === 0) continue;
    const v = vals[d], f = ns.fade[d];
    for (let i = 0; i < n; i++) {
      const a = v[i];
      if (a === a && a !== Infinity && a !== -Infinity) {
        acx[i] += px * a;
        acy[i] += py * a;
      } else if (f < 1) {
        visf[i] *= f;
      }
    }
  }
  for (let i = 0; i < n; i++) {
    x[i] = acx[i] + ns.cx;
    y[i] = acy[i] + ns.cy;
    const f = visf[i];
    vis[i] = f < VIS_MIN ? 0 : Math.max(1, Math.round(255 * f));
  }
  applyVisMask(vis, mask, n);
  return {x, y, vis};
}

/** True when the kernel path applies: support ≤ MAXD_GPU and every column stored u16q. */
export function kernelEligible(ns, metas) {
  if (ns.keys.length > MAXD_GPU) return false;
  return ns.keys.every((k, d) => metas[d] && metas[d].enc === 'u16q');
}

/**
 * Project. `src`: {n, meta(key) -> ColMeta | null, codes(key) -> Uint16Array (u16q stored, with
 * rows invalidated by a `validity` column already set to code 0),
 * values(key) -> Float32Array}; `mask` = filter vis (Uint8Array, 1 visible) or null.
 * Returns {x, y, vis, path: 'kernel'|'sparse', keys}.
 */
export function project(spec, src, mask = null) {
  const ns = normSpec(spec);
  const n = src.n;
  const metas = ns.keys.map((k) => src.meta(k));
  if (kernelEligible(ns, metas)) {
    const cols = ns.keys.map((k, d) => ({codes: src.codes(k), meta: metas[d]}));
    return {...projectKernel(ns, cols, n, mask), path: 'kernel', keys: ns.keys};
  }
  const vals = ns.keys.map((k) => src.values(k));
  return {...projectSparse(ns, vals, n, mask), path: 'sparse', keys: ns.keys};
}

/** Small token-keyed cache of settled projections (for pick/brush/bin2d on proj views). */
export class ProjCache {
  constructor(max = 8) {
    this.max = max;
    this.map = new Map();
  }
  set(token, p) {
    if (token === undefined || token === null) return;
    this.map.delete(token);
    this.map.set(token, p);
    while (this.map.size > this.max) this.map.delete(this.map.keys().next().value);
  }
  get(token) {
    return this.map.get(token) || null;
  }
  clear() {
    this.map.clear();
  }
}
