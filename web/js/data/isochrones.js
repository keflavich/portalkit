// Isochrone tracks on a plot (ARCHITECTURE.md sections 6.6 and 7.4). Pure module; the Python
// twin is portalkit/physics/isochrones.py track_xy (tests/golden/physics/track_xy.json).
//
// Each expression is c + Σ w_k·(M_k + 5·log10(dist_pc/10) + dm + A_k), where M_k is the track's
// absolute magnitude for the filter key k resolves to and A_k = aLambda(k, av, rv) when av != 0.
// An unresolved key or a missing magnitude gives NaN.

import {compileCached} from './expr.js';
import {resolveKey} from './filters.js';
import {aLambda} from './extinction.js';

/** Distance modulus 5·log10(dist_pc/10) (NaN for dist <= 0). */
export function distanceModulus(distPc) {
  const d = Number(distPc);
  return d > 0 ? 5 * Math.log10(d / 10) : NaN;
}

function axis(track, expr, av, rv, dm, filters, colmap, ext) {
  const mags = track.mags || {};
  const keys = Object.keys(mags);
  const n = (track.mass || []).length;
  const mu = distanceModulus(track.dist_pc);
  const c = compileCached(expr);
  const v = new Float64Array(n).fill(c.c);
  for (const k of c.keys) {
    const w = c.w[k];
    const mk = resolveKey(k, keys, colmap);
    if (mk === null) {
      v.fill(NaN);
      continue;
    }
    const m = mags[mk];
    let shift = mu + Number(dm);
    if (Number(av) !== 0) shift += aLambda(mk, av, rv, ext, filters);
    for (let i = 0; i < n; i++) {
      const mi = m[i] === null || m[i] === undefined ? NaN : +m[i];
      v[i] = v[i] + w * (mi + shift);
    }
  }
  return Float32Array.from(v);
}

/**
 * {x, y} Float32Arrays of a track for two expressions at A_V = av, R_V = rv and an extra
 * distance-modulus shift dm. `colmap` maps expression keys to filter keys; `ext` is the
 * pk-extinction/1 document used when a filter has no a_over_av value.
 */
export function trackXY(track, xExpr, yExpr, av, rv, dm, filters, colmap = null, ext = null) {
  return {
    x: axis(track, xExpr, av, rv, dm, filters, colmap, ext),
    y: axis(track, yExpr, av, rv, dm, filters, colmap, ext),
  };
}

/** Tracks of a pk-isochrones/1 document. */
export function tracksOf(doc) {
  return (doc && Array.isArray(doc.tracks) ? doc.tracks : []);
}

/** True when every key of both expressions resolves to a magnitude of the track. */
export function trackCovers(track, xExpr, yExpr, colmap = null) {
  const keys = Object.keys(track.mags || {});
  for (const e of [xExpr, yExpr]) {
    for (const k of compileCached(e).keys) if (resolveKey(k, keys, colmap) === null) return false;
  }
  return true;
}
