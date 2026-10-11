// Density image cache and CPU-backed scratch contexts (IMPLEMENTATION_NOTES 103).
//
// Node has no canvas, so a minimal OffscreenCanvas / 2D context fake records getContext
// options, putImageData and drawImage calls.

import {test, beforeEach} from 'node:test';
import assert from 'node:assert/strict';

const log = {getContext: [], put: 0, created: 0};

class FakeImageData {
  constructor(w, h) {
    this.width = w;
    this.height = h;
    this.data = new Uint8ClampedArray(w * h * 4);
  }
}

class FakeScratchCtx {
  constructor(canvas) { this.canvas = canvas; }
  createImageData(w, h) { return new FakeImageData(w, h); }
  putImageData(img) {
    log.put++;
    this.canvas.pixels = img.data.slice();
  }
}

class FakeOffscreenCanvas {
  constructor(w, h) {
    this.width = w;
    this.height = h;
    this.pixels = null;
    log.created++;
  }
  getContext(kind, opts) {
    log.getContext.push({kind, opts});
    if (!this._ctx) this._ctx = new FakeScratchCtx(this);
    return this._ctx;
  }
}

globalThis.OffscreenCanvas = FakeOffscreenCanvas;

const {densityImage, drawDensityGrid, densityCacheStats, makeScratchCanvas, scratchContext,
  SCRATCH_CTX_OPTS} = await import('../../web/js/plot/canvas2d.js');
const {resampleKey, sameResampleKey, resample, uniformGrid, nativeGrid, nativeImage, nativeRects, MISSING} =
  await import('../../web/js/panes/spectrum2d.js');

/** Target 2D context fake: records drawImage calls and the transform ops around them. */
function targetCtx() {
  const calls = [];
  const rec = (name) => (...a) => calls.push([name, ...a]);
  return {
    calls,
    save: rec('save'), restore: rec('restore'), beginPath: rec('beginPath'), rect: rec('rect'),
    clip: rec('clip'), translate: rec('translate'), scale: rec('scale'), drawImage: rec('drawImage'),
    set globalAlpha(v) { calls.push(['globalAlpha', v]); },
    set imageSmoothingEnabled(v) { calls.push(['imageSmoothingEnabled', v]); },
  };
}

function view() {
  // linear 0..10 on both axes onto a 100 × 100 px plot
  const lin = (r0, r1) => ({f: (v) => r0 + (v / 10) * (r1 - r0)});
  return {sx: lin(0, 100), sy: lin(100, 0), plot: {left: 0, top: 0, width: 100, height: 100}};
}

function grid(nx = 4, ny = 3, fill = (i) => i + 1) {
  const counts = new Float32Array(nx * ny);
  for (let i = 0; i < counts.length; i++) counts[i] = fill(i);
  return {counts, nx, ny, xr: [0, 10], yr: [0, 10]};
}

beforeEach(() => {
  log.getContext.length = 0;
  log.put = 0;
  log.created = 0;
});

test('scratch contexts are CPU-backed (willReadFrequently)', () => {
  assert.deepEqual({...SCRATCH_CTX_OPTS}, {willReadFrequently: true});
  const c = makeScratchCanvas(5, 7);
  assert.ok(c instanceof FakeOffscreenCanvas);
  assert.equal(c.width, 5);
  assert.equal(c.height, 7);
  assert.ok(scratchContext(c));
  assert.equal(log.getContext.length, 1);
  assert.equal(log.getContext[0].kind, '2d');
  assert.equal(log.getContext[0].opts.willReadFrequently, true);
  assert.equal(scratchContext(null), null);
});

test('densityImage builds with a willReadFrequently context', () => {
  const g = grid();
  const sc = densityImage(g, {cmap: 'viridis'});
  assert.ok(sc);
  assert.equal(sc.width, 4);
  assert.equal(sc.height, 3);
  assert.equal(log.put, 1);
  assert.ok(log.getContext.length >= 1);
  for (const c of log.getContext) assert.equal(c.opts && c.opts.willReadFrequently, true);
});

test('a redraw of the same grid is one drawImage and no new upload', () => {
  const g = grid();
  const v = view();
  const s0 = densityCacheStats();
  const a = targetCtx();
  assert.equal(drawDensityGrid(a, v, g, {cmap: 'magma'}), true);
  assert.equal(log.put, 1);
  const b = targetCtx();
  assert.equal(drawDensityGrid(b, v, g, {cmap: 'magma'}), true);
  assert.equal(log.put, 1, 'second draw reuses the colourised canvas');
  assert.equal(log.created, 1);
  assert.equal(b.calls.filter((c) => c[0] === 'drawImage').length, 1);
  const s1 = densityCacheStats();
  assert.equal(s1.builds - s0.builds, 1);
  assert.equal(s1.hits - s0.hits, 1);
  // both draws hand the same cached canvas to drawImage
  const imgA = a.calls.find((c) => c[0] === 'drawImage')[1];
  const imgB = b.calls.find((c) => c[0] === 'drawImage')[1];
  assert.equal(imgA, imgB);
});

test('cmap, lo, hi and log changes rebuild; returning to a variant is a hit', () => {
  const g = grid();
  densityImage(g, {cmap: 'magma'});
  assert.equal(log.put, 1);
  densityImage(g, {cmap: 'viridis'});
  assert.equal(log.put, 2);
  densityImage(g, {cmap: 'viridis', lo: 2});
  assert.equal(log.put, 3);
  densityImage(g, {cmap: 'viridis', lo: 2, hi: 5});
  assert.equal(log.put, 4);
  densityImage(g, {cmap: 'viridis', lo: 2, hi: 5, log: false});
  assert.equal(log.put, 5);
  // the last few variants stay cached
  densityImage(g, {cmap: 'viridis', lo: 2, hi: 5});
  densityImage(g, {cmap: 'viridis', lo: 2, hi: 5, log: false});
  assert.equal(log.put, 5);
});

test('different grids with equal contents keep separate images', () => {
  const g1 = grid();
  const g2 = grid();
  densityImage(g1, {});
  densityImage(g2, {});
  assert.equal(log.put, 2);
  densityImage(g1, {});
  densityImage(g2, {});
  assert.equal(log.put, 2);
});

test('counts refilled in place rebuild the image', () => {
  const g = grid();
  const sc1 = densityImage(g, {});
  const px1 = sc1.pixels.slice();
  g.counts.reverse();
  const sc2 = densityImage(g, {});
  assert.equal(log.put, 2);
  assert.equal(sc2, sc1, 'same-size rebuild reuses the scratch canvas');
  assert.notDeepEqual(Array.from(sc2.pixels), Array.from(px1));
});

test('image rows run top-down and empty bins are transparent', () => {
  // 2 × 2: bottom row (iy = 0) = [0, 1], top row (iy = 1) = [2, 3]
  const g = {counts: new Float32Array([0, 1, 2, 3]), nx: 2, ny: 2, xr: [0, 1], yr: [0, 1]};
  const sc = densityImage(g, {cmap: 'gray', log: false});
  const a = (x, y) => sc.pixels[(y * 2 + x) * 4 + 3];
  const r = (x, y) => sc.pixels[(y * 2 + x) * 4];
  assert.equal(a(0, 1), 0, 'count 0 (bottom-left) is transparent');
  assert.ok(a(1, 1) > 0);
  assert.equal(a(1, 0), 255, 'max count is opaque');
  assert.ok(r(1, 0) > r(0, 0), 'gray: larger counts are brighter');
});

test('empty, NaN, infinite and degenerate grids', () => {
  const v = view();
  const ctx = targetCtx();
  assert.equal(drawDensityGrid(ctx, v, grid(3, 3, () => 0)), false, 'all-zero grid draws nothing');
  assert.equal(drawDensityGrid(ctx, v, grid(3, 3, () => NaN)), false, 'all-NaN grid draws nothing');
  assert.equal(drawDensityGrid(ctx, v, {counts: new Float32Array(0), nx: 0, ny: 0, xr: [0, 1], yr: [0, 1]}), false);
  assert.equal(drawDensityGrid(ctx, v, null), false);
  assert.equal(drawDensityGrid(ctx, v, {counts: new Float32Array(4), nx: 2, ny: 2}), false, 'no extents');
  assert.equal(ctx.calls.filter((c) => c[0] === 'drawImage').length, 0);
  assert.equal(log.put, 0);

  // NaN / inf bins are transparent; the finite ones set the scale
  const g = grid(3, 1, (i) => [NaN, 4, Infinity][i]);
  const sc = densityImage(g, {});
  assert.ok(sc);
  assert.equal(sc.pixels[3], 0);
  assert.ok(sc.pixels[7] > 0);
  assert.equal(sc.pixels[11], 0);

  // nx = 1
  const g1 = grid(1, 4);
  assert.equal(drawDensityGrid(ctx, v, g1), true);
  const sc1 = densityImage(g1, {});
  assert.equal(sc1.width, 1);
  assert.equal(sc1.height, 4);

  // non-finite pixel extents
  const bad = {...grid(), xr: [0, NaN]};
  assert.equal(drawDensityGrid(targetCtx(), v, bad), false);
});

test('spectrum2d resample key: same image only for identical codes, cmap, size and view', () => {
  const mkView = (d0 = 1, d1 = 2, kind = 'lin') => ({
    sx: {kind, d0, d1, r0: 52, r1: 300},
    sy: {kind: 'lin', d0: 0, d1: 10, r0: 200, r1: 6},
    plot: {left: 52, top: 6, width: 248, height: 194},
  });
  const codes = new Uint16Array(4);
  const k = resampleKey(codes, 'magma', mkView(), 248, 194, 1);
  assert.equal(sameResampleKey(k, resampleKey(codes, 'magma', mkView(), 248, 194, 1)), true);
  assert.equal(sameResampleKey(k, resampleKey(new Uint16Array(4), 'magma', mkView(), 248, 194, 1)), false, 'new codes');
  assert.equal(sameResampleKey(k, resampleKey(codes, 'viridis', mkView(), 248, 194, 1)), false, 'cmap');
  assert.equal(sameResampleKey(k, resampleKey(codes, 'magma', mkView(1, 2.5), 248, 194, 1)), false, 'zoom');
  assert.equal(sameResampleKey(k, resampleKey(codes, 'magma', mkView(1, 2, 'log'), 248, 194, 1)), false, 'scale kind');
  assert.equal(sameResampleKey(k, resampleKey(codes, 'magma', mkView(), 496, 388, 2)), false, 'dpr');
  assert.equal(sameResampleKey(null, k), false);
  assert.equal(sameResampleKey(k, null), false);
});

test('counts moved in place between bins 1024 apart rebuild the image', () => {
  const g = grid(256, 256, () => 0);
  g.counts[0] = 5;
  g.counts[1024] = 1;
  const before = densityCacheStats();
  densityImage(g, {});
  g.counts[0] = 1;
  g.counts[1024] = 5;
  densityImage(g, {});
  const after = densityCacheStats();
  assert.equal(after.builds - before.builds, 2, 'second call rebuilt');
  assert.equal(after.hits - before.hits, 0, 'no stale hit');
  // a value swap between neighbours and a permutation that keeps the sum also rebuild
  g.counts[0] = 5; g.counts[1] = 1; g.counts[1024] = 0;
  densityImage(g, {});
  g.counts[0] = 1; g.counts[1] = 5;
  densityImage(g, {});
  assert.equal(densityCacheStats().builds - after.builds, 2);
  // unchanged contents still hit
  densityImage(g, {});
  assert.equal(densityCacheStats().hits - after.hits, 1);
});

// ------------------------------------------------------------------ spectrum2d native image

function linView(d0, d1, e0, e1, plot = {left: 10, top: 5, width: 200, height: 100}) {
  const sc = (a, b, r0, r1) => ({kind: 'lin', d0: a, d1: b, r0, r1,
    f: (v) => r0 + ((v - a) / (b - a)) * (r1 - r0), inv: (p) => a + ((p - r0) / (r1 - r0)) * (b - a)});
  return {sx: sc(d0, d1, plot.left, plot.left + plot.width), sy: sc(e0, e1, plot.top + plot.height, plot.top), plot};
}

test('uniformGrid / nativeGrid: uniform wavelengths only, size caps', () => {
  assert.deepEqual(uniformGrid([1, 2, 3, 4]), {v0: 1, step: 1});
  assert.deepEqual(uniformGrid(Float32Array.from([4, 3, 2])), {v0: 4, step: -1});
  assert.equal(uniformGrid([1, 2, 3, 10]), null, 'gap between segments');
  assert.equal(uniformGrid([1, NaN, 3]), null);
  assert.equal(uniformGrid([1]), null);
  assert.equal(uniformGrid([2, 2, 2]), null);
  const img = {nx: 4, ny: 2, wave: Float64Array.from([1, 1.1, 1.2, 1.3]), y: [0, 0.5]};
  const g = nativeGrid(img);
  assert.ok(Math.abs(g.gx.step - 0.1) < 1e-12);
  assert.deepEqual(g.gy, {v0: 0, step: 0.5});
  assert.equal(nativeGrid({...img, wave: [1, 1.1, 1.2, 2]}), null);
  assert.equal(nativeGrid({...img, nx: 20000, wave: new Float64Array(20000).map((_, k) => k)}), null);
  assert.deepEqual(nativeGrid({nx: 2, ny: 3, wave: [5, 6]}).gy, {v0: 0, step: 1});
});

test('nativeImage + nativeRects reproduce resample at the pixel centres', () => {
  const nx = 6, ny = 4;
  const img = {nx, ny, wave: [1, 2, 3, 4, 5, 6], y: [0, 1, 2, 3]};
  const codes = new Uint16Array(nx * ny);
  for (let k = 0; k < codes.length; k++) codes[k] = k % 7 === 3 ? MISSING : k;
  const lut = new Uint8Array(256 * 3);
  for (let k = 0; k < 256; k++) { lut[3 * k] = k; lut[3 * k + 1] = 255 - k; lut[3 * k + 2] = 7; }
  const g = nativeGrid(img);
  const out = new FakeImageData(nx, ny);
  nativeImage(img, codes, lut, g, out);
  // zoomed view: x 1.7..4.9, y -0.5..3.5 on a 200 x 100 plot
  const v = linView(1.7, 4.9, -0.5, 3.5);
  const R = nativeRects(g, nx, ny, v);
  const W = 200, H = 100;
  const ref = new FakeImageData(W, H);
  resample(img, codes, lut, v, ref, 1);
  // sample the drawn image (nearest neighbour through the rects) at device pixel centres
  let checked = 0;
  for (let yy = 2; yy < H; yy += 7) {
    for (let x = 1; x < W; x += 9) {
      const px = v.plot.left + x + 0.5, py = v.plot.top + yy + 0.5;
      const u = R.sx + ((px - R.dx) / R.dw) * R.sw, w = R.sy + ((py - R.dy) / R.dh) * R.sh;
      const cx = Math.floor(u), cy = Math.floor(w);
      const o = (yy * W + x) * 4;
      if (cx < 0 || cx >= nx || cy < 0 || cy >= ny) { assert.equal(ref.data[o + 3], 0); continue; }
      const q = (cy * nx + cx) * 4;
      assert.deepEqual([...out.data.slice(q, q + 4)], [...ref.data.slice(o, o + 4)], `pixel ${x},${yy}`);
      checked++;
    }
  }
  assert.ok(checked > 100);
  // fully outside the image, and unsupported (log) views
  assert.deepEqual(nativeRects(g, nx, ny, linView(10, 12, 0, 3)), {empty: true});
  const lv = linView(1, 6, 0, 3);
  lv.sx.kind = 'log';
  assert.equal(nativeRects(g, nx, ny, lv), null);
});

test('nativeImage flips descending wavelengths and y so the drawn image matches resample', () => {
  const nx = 3, ny = 2;
  const img = {nx, ny, wave: [3, 2, 1], y: [1, 0]};
  const codes = Uint16Array.from([0, 1, 2, 3, 4, 5]);
  const lut = new Uint8Array(256 * 3).map((_, k) => k / 3);
  const g = nativeGrid(img);
  const out = new FakeImageData(nx, ny);
  nativeImage(img, codes, lut, g, out);
  // canvas (0, 0) = shortest wavelength (wave 1 -> ix 2), largest y (y 1 -> row 0)
  assert.equal(out.data[0], lut[3 * codes[0 * nx + 2]]);
  // canvas (2, 1) = longest wavelength (ix 0), smallest y (row 1)
  assert.equal(out.data[(1 * nx + 2) * 4], lut[3 * codes[1 * nx + 0]]);
});
