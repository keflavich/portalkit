// Histograms and 2D binning (ARCHITECTURE.md sections 6.6 and 7.1 tier F). Pure module.

/** Finite [min, max] of `v` over rows where mask is non-zero (all rows when mask is null). */
export function finiteRange(v, mask = null, {positive = false} = {}) {
  let lo = Infinity, hi = -Infinity;
  for (let i = 0; i < v.length; i++) {
    if (mask && !mask[i]) continue;
    const a = v[i];
    if (!Number.isFinite(a) || (positive && !(a > 0))) continue;
    if (a < lo) lo = a;
    if (a > hi) hi = a;
  }
  return lo <= hi ? [lo, hi] : null;
}

function widen(r) {
  if (!r) return [0, 1];
  let [lo, hi] = r;
  if (!(hi > lo)) {
    const d = lo === 0 ? 0.5 : Math.abs(lo) * 1e-3;
    lo -= d;
    hi += d;
  }
  return [lo, hi];
}

/**
 * Histogram of `values` (NaN ignored) into `bins` equal bins over `range` (default: finite
 * min/max of the masked values). The upper edge is inclusive. With `log`, bins are equal in
 * log10 and non-positive values are ignored (range is given in data units).
 * Returns {edges: Float64Array(bins+1), counts: Uint32Array(bins), total, under, over}.
 */
export function histogram(values, bins, {range = null, mask = null, log = false} = {}) {
  const nb = Math.max(1, bins | 0);
  let r = range && Number.isFinite(range[0]) && Number.isFinite(range[1]) ? [+range[0], +range[1]] : finiteRange(values, mask, {positive: log});
  if (r && r[0] > r[1]) r = [r[1], r[0]];
  if (log && r) r = [Math.log10(Math.max(r[0], Number.MIN_VALUE)), Math.log10(Math.max(r[1], Number.MIN_VALUE))];
  const [lo, hi] = widen(r);
  const edges = new Float64Array(nb + 1);
  for (let k = 0; k <= nb; k++) edges[k] = lo + ((hi - lo) * k) / nb;
  if (log) for (let k = 0; k <= nb; k++) edges[k] = Math.pow(10, edges[k]);
  const counts = new Uint32Array(nb);
  const scale = nb / (hi - lo);
  let total = 0, under = 0, over = 0;
  for (let i = 0; i < values.length; i++) {
    if (mask && !mask[i]) continue;
    let a = values[i];
    if (!Number.isFinite(a)) continue;
    if (log) {
      if (!(a > 0)) continue;
      a = Math.log10(a);
    }
    if (a < lo) {
      under++;
      continue;
    }
    if (a > hi) {
      over++;
      continue;
    }
    let k = Math.floor((a - lo) * scale);
    if (k >= nb) k = nb - 1;
    counts[k]++;
    total++;
  }
  return {edges, counts, total, under, over};
}

/**
 * 2D counts on an nx x ny grid (cell iy*nx+ix, iy = 0 at yr[0]); ranges default to the finite
 * extent of the masked rows. Points outside the ranges are dropped; upper edges are inclusive.
 * Returns {counts: Uint32Array, nx, ny, xr, yr, total}.
 */
export function bin2d(x, y, nx, ny, {xr = null, yr = null, mask = null} = {}) {
  const NX = Math.max(1, nx | 0), NY = Math.max(1, ny | 0);
  const both = new Uint8Array(x.length);
  for (let i = 0; i < x.length; i++) both[i] = (!mask || mask[i]) && Number.isFinite(x[i]) && Number.isFinite(y[i]) ? 1 : 0;
  const XR = widen(xr && Number.isFinite(xr[0]) && Number.isFinite(xr[1]) ? [Math.min(xr[0], xr[1]), Math.max(xr[0], xr[1])] : finiteRange(x, both));
  const YR = widen(yr && Number.isFinite(yr[0]) && Number.isFinite(yr[1]) ? [Math.min(yr[0], yr[1]), Math.max(yr[0], yr[1])] : finiteRange(y, both));
  const counts = new Uint32Array(NX * NY);
  const sx = NX / (XR[1] - XR[0]), sy = NY / (YR[1] - YR[0]);
  let total = 0;
  for (let i = 0; i < x.length; i++) {
    if (!both[i]) continue;
    const a = x[i], b = y[i];
    if (a < XR[0] || a > XR[1] || b < YR[0] || b > YR[1]) continue;
    let ix = Math.floor((a - XR[0]) * sx), iy = Math.floor((b - YR[0]) * sy);
    if (ix >= NX) ix = NX - 1;
    if (iy >= NY) iy = NY - 1;
    counts[iy * NX + ix]++;
    total++;
  }
  return {counts, nx: NX, ny: NY, xr: XR, yr: YR, total};
}

/** Number of rows with mask set and finite x/y inside the window [x0,x1] x [y0,y1]. */
export function countInWindow(x, y, mask, x0, x1, y0, y1) {
  let c = 0;
  for (let i = 0; i < x.length; i++) {
    if (mask && !mask[i]) continue;
    const a = x[i], b = y[i];
    if (a >= x0 && a <= x1 && b >= y0 && b <= y1) c++;
  }
  return c;
}
