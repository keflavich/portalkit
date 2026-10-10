// Spatial-index nearest-point picking, for click-to-inspect / hover.
//
// Builds a uniform grid over the current (X,Y) projection plane (the same two axes the
// renderer draws) via counting sort, so a pick is O(1)-ish instead of an O(n) scan over
// every row. Rebuilt whenever the projected axes change (band pickers), not on every pan/zoom
// — the grid lives in data space (u-space X,Y), not screen space, so panning/zooming the
// camera doesn't invalidate it.

const GRID_RES = 1024;   // cells per side; ~balances build time and candidates-per-cell at 1-4M rows

/**
 * Build a pick index for the dataset projected through `frame` (same Float64Array(2D) shape
 * used by the renderer/draw loop: frame[0..D) = X weights, frame[D..2D) = Y weights, applied
 * to each row's standardized value data.ustd(i,k)).
 */
export function buildPickIndex(data, frame) {
  const n = data.n, D = data.D, FD = frame.length >> 1;
  const X = new Float32Array(n), Y = new Float32Array(n);
  const valid = new Uint8Array(n);
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (let i = 0; i < n; i++) {
    let x = 0, y = 0, ok = true;
    for (let d = 0; d < FD; d++) {
      const fx = frame[d], fy = frame[FD + d];
      if (fx === 0 && fy === 0) continue;
      const u = data.ustd(i, d);
      if (!Number.isFinite(u)) { ok = false; break; }
      x += fx * u; y += fy * u;
    }
    if (!ok) continue;
    X[i] = x; Y[i] = y; valid[i] = 1;
    if (x < x0) x0 = x; if (x > x1) x1 = x;
    if (y < y0) y0 = y; if (y > y1) y1 = y;
  }
  if (!(x1 > x0)) { x0 -= 1; x1 += 1; }
  if (!(y1 > y0)) { y0 -= 1; y1 += 1; }
  const gw = GRID_RES, gh = GRID_RES;
  const sx = gw / (x1 - x0), sy = gh / (y1 - y0);
  const cellOf = (x, y) => {
    let gx = Math.floor((x - x0) * sx), gy = Math.floor((y - y0) * sy);
    if (gx < 0) gx = 0; else if (gx >= gw) gx = gw - 1;
    if (gy < 0) gy = 0; else if (gy >= gh) gy = gh - 1;
    return gy * gw + gx;
  };
  const counts = new Uint32Array(gw * gh + 1);
  for (let i = 0; i < n; i++) if (valid[i]) counts[cellOf(X[i], Y[i]) + 1]++;
  for (let c = 0; c < gw * gh; c++) counts[c + 1] += counts[c];
  const order = new Uint32Array(counts[gw * gh]);
  const cursor = counts.slice(0, gw * gh);
  for (let i = 0; i < n; i++) {
    if (!valid[i]) continue;
    const c = cellOf(X[i], Y[i]);
    order[cursor[c]++] = i;
  }
  return { n, X, Y, valid, x0, y0, sx, sy, gw, gh, starts: counts, order };
}

/**
 * Find the row nearest the screen point (sx,sy), searching only cells within `rPx` CSS px
 * (camera-projected), expanding outward one ring at a time until a candidate is found or the
 * search exceeds a small cell radius. Returns the row index, or -1 if nothing is within rPx.
 */
export function pick(index, camera, sx, sy, rPx = 10) {
  if (!index) return -1;
  const [dx, dy] = camera.toData(sx, sy);
  const gx0 = Math.floor((dx - index.x0) * index.sx), gy0 = Math.floor((dy - index.y0) * index.sy);
  let best = -1, bestD2 = Infinity, foundRing = -1;
  const maxRing = 4;
  for (let ring = 0; ring <= maxRing; ring++) {
    if (foundRing >= 0 && ring > foundRing + 1) break;   // one extra ring past first hit, to catch closer points just over a cell edge
    for (let gy = gy0 - ring; gy <= gy0 + ring; gy++) {
      if (gy < 0 || gy >= index.gh) continue;
      for (let gx = gx0 - ring; gx <= gx0 + ring; gx++) {
        if (gx < 0 || gx >= index.gw) continue;
        if (ring > 0 && gx > gx0 - ring && gx < gx0 + ring && gy > gy0 - ring && gy < gy0 + ring) continue; // interior already scanned
        const c = gy * index.gw + gx;
        const s0 = index.starts[c], s1 = index.starts[c + 1];
        for (let k = s0; k < s1; k++) {
          const i = index.order[k];
          const [psx, psy] = camera.toScreen(index.X[i], index.Y[i]);
          const d2 = (psx - sx) * (psx - sx) + (psy - sy) * (psy - sy);
          if (d2 < bestD2) { bestD2 = d2; best = i; }
        }
      }
    }
    if (best >= 0 && foundRing < 0) foundRing = ring;
  }
  return bestD2 <= rPx * rPx ? best : -1;
}
