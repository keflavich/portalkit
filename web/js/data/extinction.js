// Extinction helpers (ARCHITECTURE.md section 6.6): A_lambda, CCD/CMD reddening vectors and
// spectrum reddening. Pure module; the Python twin is portalkit/physics/extinction.py
// (tests/golden/physics/reddening.json, 1e-6 relative).
//
// G23 A(lambda)/A(V) is a(x) + b(x)(1/R_V - 1/3.1), so it is linear in 1/R_V: any R_V is
// interpolated from two tabulated values. Wavelength interpolation is linear, NaN outside.

import {compileCached} from './expr.js';
import {interpLinear, resolveKey, filtersMap} from './filters.js';

/** Curve key of an R_V value (Python f"{rv:g}": 3.1 -> '3.1', 4.0 -> '4'). */
export function rvKey(rv) {
  const r = Number(rv);
  if (!Number.isFinite(r)) return String(r);
  const e = r === 0 ? 0 : Math.floor(Math.log10(Math.abs(r)));
  if (e < -4 || e >= 6) {
    const [m, x] = r.toExponential(5).split('e');
    const mm = m.indexOf('.') >= 0 ? m.replace(/0+$/, '').replace(/\.$/, '') : m;
    const xi = parseInt(x, 10);
    return `${mm}e${xi < 0 ? '-' : '+'}${String(Math.abs(xi)).padStart(2, '0')}`;
  }
  return String(Number(r.toPrecision(6)));
}

/** a_over_av key: 'G23_3.1'. */
export function lawKey(rv, law = 'G23') {
  return `${law}_${rvKey(rv)}`;
}

function parseLawKey(k) {
  const i = k.lastIndexOf('_');
  if (i < 0) return null;
  const law = k.slice(0, i);
  const s = k.slice(i + 1).trim();
  if (!/^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$|^[+-]?(inf|infinity|nan)$/i.test(s)) return null;
  const r = Number(s);
  return Number.isFinite(r) && r > 0 ? [law, r] : null;
}

const finOrNull = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);

/**
 * Value at `rv` from {rv: value} pairs (Map, object or [[rv, v]...]), linear in 1/R_V between the
 * bracketing pair (the two nearest when outside). Exact R_V matches are returned as is.
 * Values may be numbers (null -> null result) or arrays (element-wise).
 */
export function interpRv(rv, values) {
  let items = values instanceof Map ? Array.from(values) : Array.isArray(values) ? values.slice() : Object.entries(values);
  items = items.map(([r, v]) => [Number(r), v]).sort((a, b) => 1 / a[0] - 1 / b[0]);
  const key = rvKey(rv);
  for (const [r, v] of items) if (rvKey(r) === key) return v;
  if (items.length < 2) throw new RangeError(`need two R_V values to interpolate to R_V=${rv}`);
  const inv = 1 / Number(rv);
  const xs = items.map(([r]) => 1 / r);
  let j = 0;
  while (j < xs.length && xs[j] < inv) j++;
  j = Math.min(Math.max(j, 1), items.length - 1);
  const [r1, v1] = items[j - 1], [r2, v2] = items[j];
  const x1 = 1 / r1, x2 = 1 / r2;
  const f = (inv - x1) / (x2 - x1);
  const isArr = (v) => Array.isArray(v) || ArrayBuffer.isView(v);
  if (isArr(v1) || isArr(v2)) {
    const n = Math.max(v1.length, v2.length);
    const out = new Float64Array(n);
    const g = (a, i) => (a[i] === null || a[i] === undefined ? NaN : +a[i]);
    for (let i = 0; i < n; i++) out[i] = g(v1, i) + (g(v2, i) - g(v1, i)) * f;
    return out;
  }
  if (v1 === null || v1 === undefined || v2 === null || v2 === undefined) return null;
  return +v1 + (+v2 - +v1) * f;
}

/**
 * A(lambda)/A(V) of a pk-filters/1 entry at `rv`:
 *   1. a_over_av['<law>_<rv>'] when present and not null;
 *   2. linear in 1/R_V from the entry's other '<law>_*' values (two or more non-null);
 *   3. the pk-extinction/1 curve at the entry's wl_um (curve for rv if present, else 1/R_V
 *      interpolation of the curves);
 *   4. NaN.
 * `law` defaults to ext.law or 'G23'.
 */
export function entryK(entry, rv, ext = null, law = null) {
  const L = law || (ext && ext.law) || 'G23';
  const aa = (entry && entry.a_over_av) || {};
  const v = finOrNull(aa[lawKey(rv, L)]);
  if (v !== null) return v;
  const same = new Map();
  for (const [k, val] of Object.entries(aa)) {
    const p = parseLawKey(k);
    const fv = finOrNull(val);
    if (p && p[0] === L && fv !== null) same.set(p[1], fv);
  }
  if (same.size >= 2) return +interpRv(rv, same);
  if (ext && ext.curve && Object.keys(ext.curve).length) {
    const wl = finOrNull(entry && entry.wl_um);
    if (wl === null) return NaN;
    const curves = ext.curve;
    const rk = rvKey(rv);
    if (Object.prototype.hasOwnProperty.call(curves, rk)) {
      const c = curves[rk];
      return interpLinear(wl, c.w, c.k);
    }
    const vals = new Map();
    for (const [ck, c] of Object.entries(curves)) vals.set(Number(ck), interpLinear(wl, c.w, c.k));
    if (vals.size >= 2) {
      const r = interpRv(rv, vals);
      return r === null ? NaN : +r;
    }
  }
  return NaN;
}

/**
 * A_lambda = A_V·k for a filter key (resolved in `filters` by resolveKey, with `colmap`) or a
 * filter entry object. Unknown filter -> NaN. (Python twin: extinction.a_lambda.)
 */
export function aLambda(filterKey, av, rv, ext = null, filters = null, colmap = null) {
  let entry;
  if (filterKey && typeof filterKey === 'object') entry = filterKey;
  else {
    if (filters === null || filters === undefined) {
      // A filter key cannot be resolved without the pk-filters/1 document; report the missing
      // argument instead of returning NaN reddening for every key.
      throw new TypeError(`aLambda(${JSON.stringify(filterKey)}): a filter key needs the filters document (5th argument), or pass a filter entry object`);
    }
    const fl = filtersMap(filters);
    const k = resolveKey(filterKey, fl, colmap);
    if (k === null) return NaN;
    entry = fl[k];
  }
  return Number(av) * entryK(entry, rv, ext);
}

/**
 * {dx, dy}: change of two linear expressions for A_V = av. Each expression key resolves to a
 * filter; the vector is Σ w_k·A_lambda(k) over keys with non-zero weight. Constants do not
 * contribute; an unresolved key makes that component NaN.
 */
export function vector(xExpr, yExpr, av, rv, filters, colmap = null, ext = null) {
  const out = {};
  for (const [name, expr] of [['dx', xExpr], ['dy', yExpr]]) {
    const c = compileCached(expr);
    let tot = 0;
    for (const k of c.keys) {
      const w = c.w[k];
      if (w === 0) continue;
      tot += w * aLambda(k, av, rv, ext, filters, colmap);
    }
    out[name] = tot;
  }
  return out;
}

function fluxOf(spec) {
  if (spec.flux) return spec.flux;
  const ch = spec.channels || {};
  if (ch.flux) return ch.flux;
  const names = Object.keys(ch);
  if (!names.length) throw new Error('spectrum has no flux channel');
  return ch[names[0]];
}

/**
 * Reddened flux: flux·10^(-0.4·k(wave)·av) as Float32Array, k linear in wavelength from
 * law {w, k}, NaN outside the law. `spec` = {wave, flux} or a decoded pks1 Spectrum (uses the
 * 'flux' channel, else the first channel).
 */
export function redden(spec, law, av) {
  const wave = spec.wave;
  const flux = fluxOf(spec);
  const n = wave.length;
  const out = new Float32Array(n);
  const A = Number(av);
  for (let i = 0; i < n; i++) {
    const k = interpLinear(+wave[i], law.w, law.k);
    const f = flux[i] === null || flux[i] === undefined ? NaN : +flux[i];
    out[i] = f * Math.pow(10, -0.4 * k * A);
  }
  return out;
}

/** A(lambda)/A(V) curve at R_V from a pk-extinction/1 document: {w: Float64Array, k: Float64Array}. */
export function kCurve(ext, rv) {
  const curves = ext.curve || {};
  const rk = rvKey(rv);
  if (Object.prototype.hasOwnProperty.call(curves, rk)) {
    const c = curves[rk];
    return {w: Float64Array.from(c.w), k: Float64Array.from(c.k, (x) => (x === null ? NaN : x))};
  }
  const entries = Object.entries(curves);
  if (entries.length < 2) throw new RangeError(`extinction document has no curve for R_V=${rv}`);
  // interpolate in 1/R_V on the first curve's wavelength grid
  const w = Float64Array.from(entries[0][1].w);
  const vals = new Map();
  for (const [ck, c] of entries) vals.set(Number(ck), Array.from(w, (x) => interpLinear(x, c.w, c.k)));
  return {w, k: interpRv(rv, vals)};
}
