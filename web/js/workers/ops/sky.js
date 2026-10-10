// Sky queries (ARCHITECTURE.md sections 5.4 and 6.6): viewport windows with an LOD-uniform cap,
// dec-sorted crossmatch, cones and nearest neighbours. Pure module. Angles in degrees.

import {SKY_MARKER_CAP} from '../../core/constants.js';

const D2R = Math.PI / 180;

/** Great-circle separation in degrees (haversine). */
export function separation(ra1, dec1, ra2, dec2) {
  const p1 = dec1 * D2R, p2 = dec2 * D2R;
  const dp = p2 - p1, dl = (ra2 - ra1) * D2R;
  const s = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return (2 * Math.asin(Math.min(1, Math.sqrt(s)))) / D2R;
}

/** RA difference wrapped to (-180, 180]. */
export function wrapDRA(d) {
  let x = ((d + 180) % 360 + 360) % 360 - 180;
  if (x === -180) x = 180;
  return x;
}

/** Window test function for {ra, dec, fov, fovy?}: box of half-widths fov/2 (RA scaled by cos dec). */
export function windowTest(win) {
  const ra0 = +win.ra, dec0 = +win.dec;
  const hx = Math.abs(+win.fov) / 2;
  const hy = Math.abs(+(win.fovy ?? win.fov)) / 2;
  const allRA = dec0 + hy >= 90 || dec0 - hy <= -90 || hx >= 180;
  return (ra, dec) => {
    if (!(Math.abs(dec - dec0) <= hy)) return false;
    if (allRA) return true;
    const c = Math.cos(dec * D2R);
    return Math.abs(wrapDRA(ra - ra0)) * c <= hx;
  };
}

/**
 * Rows inside a sky window, capped at `cap` (default SKY_MARKER_CAP).
 * order: manifest `order` ({kind:"lod", levels:[cumulative]} or {kind:"input"}).
 * When more than `cap` rows are inside, the result keeps every in-window row of the largest LOD
 * prefix that fits and a stride subsample of the next level (each prefix is a uniform random
 * subsample, levels are sky-sorted so a stride is spatially even); input order uses a stride.
 * Returns {idx: Int32Array (ascending stored order), total, sampled}.
 */
export function skyWindow(ra, dec, win, {mask = null, order = null} = {}) {
  const cap = Math.max(0, (win.cap ?? SKY_MARKER_CAP) | 0);
  const inWin = windowTest(win);
  const n = ra.length;
  const rows = [];
  for (let i = 0; i < n; i++) {
    if (mask && !mask[i]) continue;
    const a = ra[i], d = dec[i];
    if (!Number.isFinite(a) || !Number.isFinite(d)) continue;
    if (inWin(a, d)) rows.push(i);
  }
  const total = rows.length;
  if (total <= cap) return {idx: Int32Array.from(rows), total, sampled: false};
  if (cap === 0) return {idx: new Int32Array(0), total, sampled: true};
  const levels = order && order.kind === 'lod' && Array.isArray(order.levels) ? order.levels : null;
  let out;
  if (levels && levels.length) {
    // rows is ascending; count in-window rows per level prefix
    let k = 0;
    let take = 0; // number of leading entries of `rows` fully taken
    for (; k < levels.length; k++) {
      const end = levels[k];
      let j = take;
      while (j < rows.length && rows[j] < end) j++;
      if (j > cap) break;
      take = j;
    }
    out = rows.slice(0, take);
    const nextEnd = k < levels.length ? levels[k] : n;
    const nextRows = [];
    for (let j = take; j < rows.length && rows[j] < nextEnd; j++) nextRows.push(rows[j]);
    const room = cap - out.length;
    if (room > 0 && nextRows.length) {
      const stride = nextRows.length / room;
      for (let t = 0; t < room; t++) out.push(nextRows[Math.min(nextRows.length - 1, Math.floor(t * stride))]);
    }
  } else {
    out = [];
    const stride = rows.length / cap;
    for (let t = 0; t < cap; t++) out.push(rows[Math.min(rows.length - 1, Math.floor(t * stride))]);
  }
  return {idx: Int32Array.from(out), total, sampled: true};
}

/** Dec-sorted index over rows with finite positions: {rows: Int32Array, dec: Float64Array}. */
export function buildDecIndex(ra, dec) {
  const rows = [];
  for (let i = 0; i < ra.length; i++) if (Number.isFinite(ra[i]) && Number.isFinite(dec[i])) rows.push(i);
  const r = Int32Array.from(rows);
  r.sort((a, b) => dec[a] - dec[b] || a - b);
  const d = new Float64Array(r.length);
  for (let k = 0; k < r.length; k++) d[k] = dec[r[k]];
  return {rows: r, dec: d};
}

function lowerBound(arr, v) {
  let lo = 0, hi = arr.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (arr[mid] < v) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/**
 * For every query position, the nearest catalog row within rArcsec (-1 when none).
 * `index` from buildDecIndex (built when omitted).
 */
export function crossmatch(ra, dec, qra, qdec, rArcsec, index = null) {
  const ix = index || buildDecIndex(ra, dec);
  const r = Math.abs(+rArcsec) / 3600;
  const out = new Int32Array(qra.length).fill(-1);
  if (!(r > 0) && r !== 0) return out;
  for (let q = 0; q < qra.length; q++) {
    const a = +qra[q], d = +qdec[q];
    if (!Number.isFinite(a) || !Number.isFinite(d)) continue;
    let best = -1, bestS = r;
    for (let k = lowerBound(ix.dec, d - r); k < ix.dec.length && ix.dec[k] <= d + r; k++) {
      const i = ix.rows[k];
      const s = separation(a, d, ra[i], dec[i]);
      if (s < bestS || (s === bestS && (best < 0 || i < best))) {
        best = i;
        bestS = s;
      }
    }
    out[q] = best;
  }
  return out;
}

/** Rows within rArcsec of (ra0, dec0), sorted by separation: [{i, sep (arcsec)}]. */
export function cone(ra, dec, ra0, dec0, rArcsec, {limit = Infinity, index = null, mask = null} = {}) {
  const ix = index || buildDecIndex(ra, dec);
  const r = Math.abs(+rArcsec) / 3600;
  const hits = [];
  for (let k = lowerBound(ix.dec, dec0 - r); k < ix.dec.length && ix.dec[k] <= dec0 + r; k++) {
    const i = ix.rows[k];
    if (mask && !mask[i]) continue;
    const s = separation(ra0, dec0, ra[i], dec[i]);
    if (s <= r) hits.push({i, sep: s * 3600});
  }
  hits.sort((p, q) => p.sep - q.sep || p.i - q.i);
  return hits.length > limit ? hits.slice(0, limit) : hits;
}

/** The k nearest rows to (ra0, dec0): [{i, sep (arcsec)}] ascending. */
export function nearest(ra, dec, ra0, dec0, k = 1, {mask = null} = {}) {
  const K = Math.max(0, k | 0);
  const best = [];
  if (!K) return best;
  let worst = Infinity;
  for (let i = 0; i < ra.length; i++) {
    if (mask && !mask[i]) continue;
    const a = ra[i], d = dec[i];
    if (!Number.isFinite(a) || !Number.isFinite(d)) continue;
    // cheap reject: dec difference alone already exceeds the current k-th distance
    if (best.length === K && Math.abs(d - dec0) > worst) continue;
    const s = separation(ra0, dec0, a, d);
    if (best.length < K || s < worst) {
      let p = best.length;
      if (p === K) {
        best.pop();
        p = K - 1;
      }
      while (p > 0 && best[p - 1].s > s) p--;
      best.splice(p, 0, {i, s});
      worst = best.length === K ? best[K - 1].s : Infinity;
    }
  }
  return best.map((b) => ({i: b.i, sep: b.s * 3600}));
}
