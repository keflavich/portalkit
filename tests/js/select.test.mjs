// Worker ops (select, raster, query, sky, find, project, export) and the data engine end to end:
// DataClient (in-thread engine) over a small bundle built here in JS, plus bundles written by the
// Python writer (portalkit.bundle.catalog) when python is available.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {gzipSync} from 'node:zlib';
import {spawnSync} from 'node:child_process';
import {mkdtempSync, readFileSync, existsSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';

import {drawableXY, drawableProj, pointInPolygon, normShape, brushArrays, buildPickGrid, pickNearest} from '../../web/js/workers/ops/select.js';
import {finiteRange, histogram, bin2d, countInWindow} from '../../web/js/workers/ops/raster.js';
import {isActive, snrBands, filterNeeds, applyFilters} from '../../web/js/workers/ops/query.js';
import {separation, wrapDRA, windowTest, skyWindow, buildDecIndex, crossmatch, cone, nearest} from '../../web/js/workers/ops/sky.js';
import {IdIndex, parseCoords, findRows} from '../../web/js/workers/ops/find.js';
import {normSpec, projectKernel, projectSparse, project, kernelEligible, ProjCache} from '../../web/js/workers/ops/project.js';
import {csvEscape, f32String, formatValue, toCSV, parseCSV, toJSON, toIdList, toDS9} from '../../web/js/workers/ops/export.js';
import {decodeStr} from '../../web/js/data/codec.js';
import {DataClient} from '../../web/js/data/client.js';
import {createEngine, quantizedNumber} from '../../web/js/workers/data.worker.js';
import {createLocalRpc} from '../../web/js/workers/rpc.js';

// ------------------------------------------------------------------ byte helpers

const gz = (u8) => new Uint8Array(gzipSync(u8));
const u16b = (a) => new Uint8Array(Uint16Array.from(a).buffer);
const u32b = (a) => new Uint8Array(Uint32Array.from(a).buffer);
const f32b = (a) => new Uint8Array(Float32Array.from(a).buffer);

function strPayload(list) {
  const te = new TextEncoder();
  const parts = list.map((s) => te.encode(s));
  const total = parts.reduce((s, p) => s + p.length, 0);
  const out = new Uint8Array(4 + 4 * (list.length + 1) + total);
  const dv = new DataView(out.buffer);
  dv.setUint32(0, list.length, true);
  let off = 0;
  parts.forEach((p, i) => {
    dv.setUint32(4 + 4 * i, off, true);
    off += p.length;
  });
  dv.setUint32(4 + 4 * list.length, off, true);
  let at = 4 + 4 * (list.length + 1);
  for (const p of parts) {
    out.set(p, at);
    at += p.length;
  }
  return out;
}

function idIndexOf(list, aliases = null) {
  return new IdIndex([{start: 0, str: decodeStr(strPayload(list))}], aliases);
}

function fakeFetch(files) {
  const calls = [];
  const fn = async (url) => {
    const u = String(url);
    calls.push(u);
    if (!files.has(u)) return new Response('missing', {status: 404});
    return new Response(files.get(u), {status: 200});
  };
  fn.calls = calls;
  return fn;
}

// ------------------------------------------------------------------ mini bundle (JS-built)

const MINI = 'https://mini.test/cat/';
const U32LIN_MAX = 4294967294;
const MINI_IDS = ['a1', 'B2', 'c3', 'Ünï', 'a10'];
const MINI_RA = [10.1, 10.5, 10.9, NaN, 10.5];
const MINI_DEC = [-0.5, 0, 0.5, NaN, 0.001];

function miniBundle() {
  const bbox = {ra: [10, 11], dec: [-1, 1]};
  const lin = (v, [lo, hi]) => (Number.isFinite(v) ? Math.round(((v - lo) / (hi - lo)) * U32LIN_MAX) : 0xffffffff);
  const man = {
    schema: 'pk-catalog/1', id: 'mini', n: 5, order: {kind: 'input'}, chunk_rows: 3,
    ids: {file: 'ids.bin', enc: 'str', chunks: null, alias_file: 'aliases.json'},
    pos: {enc: 'u32lin', shuffle: false, files: {ra: 'pos/ra.bin', dec: 'pos/dec.bin'}},
    bbox,
    cols: [
      {key: 'mag', role: 'mag', unit: 'mag', err: 'emag', enc: 'u16q', lo: 10, hi: 665.34, step: 0.01, shuffle: false,
        chunk_files: ['cols/mag.0.bin', 'cols/mag.1.bin']},
      {key: 'emag', role: 'error', unit: 'mag', enc: 'f32', shuffle: false, file: 'cols/emag.bin'},
      {key: 'cls', role: 'category', enc: 'cat8', levels: ['star', 'yso'], file: 'cols/cls.bin'},
      {key: 'good', role: 'flag', enc: 'bits', chunk_files: ['cols/good.0.bin', 'cols/good.1.bin']},
      {key: 'flux', role: 'flux', unit: 'Jy', enc: 'u16q', lo: 0, hi: 1, step: 0.001, shuffle: false, file: 'cols/flux.bin',
        validity: 'good', limits: {file: 'limits/flux.bin', count: 1}},
    ],
    virtual: [{key: 'mag - emag', label: 'mag minus err'}],
    density: [],
  };
  const lim = new Uint8Array(4 + 5 * 1);
  new DataView(lim.buffer).setUint32(0, 1, true);
  new DataView(lim.buffer).setUint32(4, 2, true);
  lim[8] = 1;
  const files = new Map([
    ['manifest.json', JSON.stringify(man)],
    ['ids.bin', gz(strPayload(MINI_IDS))],
    ['aliases.json', JSON.stringify({'2MASS J1': 2, 'Other-é': 1})],
    ['pos/ra.bin', gz(u32b(MINI_RA.map((v) => lin(v, bbox.ra))))],
    ['pos/dec.bin', gz(u32b(MINI_DEC.map((v) => lin(v, bbox.dec))))],
    // mag codes: 10, 11, missing | 12, 665.34
    ['cols/mag.0.bin', gz(u16b([1, 101, 0]))],
    ['cols/mag.1.bin', gz(u16b([201, 65535]))],
    ['cols/emag.bin', gz(f32b([0.01, 0.5, NaN, 0.2, 0.1]))],
    ['cols/cls.bin', gz(new Uint8Array([0, 1, 255, 1, 0]))],
    ['cols/good.0.bin', gz(new Uint8Array([0b101]))], // rows 0,2 good; 1 bad
    ['cols/good.1.bin', gz(new Uint8Array([0b10]))], // row 3 bad, row 4 good
    ['cols/flux.bin', gz(u16b([101, 201, 301, 401, 501]))],
    ['limits/flux.bin', gz(lim)],
  ]);
  return new Map(Array.from(files, ([k, v]) => [MINI + k, v]));
}

async function openMini() {
  const f = fakeFetch(miniBundle());
  const c = await DataClient.open(MINI + 'manifest.json', {worker: false, fetch: f});
  return {c, f};
}

// ------------------------------------------------------------------ ops: select

test('select: drawable, shapes, brush (rect / circle / ellipse / lasso, log axes)', () => {
  const x = Float32Array.from([1, 2, 3, NaN, 5, 10, -1]);
  const y = Float32Array.from([1, 2, 3, 4, Infinity, 10, 1]);
  const vis = Uint8Array.from([1, 1, 0, 1, 1, 1, 1]);
  assert.deepEqual(Array.from(drawableXY(x, y, vis)), [1, 1, 0, 0, 0, 1, 1]);
  assert.deepEqual(Array.from(drawableXY(x, y, null, {xlog: true})), [1, 1, 1, 0, 0, 1, 0]);
  assert.deepEqual(Array.from(drawableProj(Uint8Array.from([3, 0, 255]), Float32Array.from([0, 0, NaN]), null)), [1, 0, 0]);
  const d = drawableXY(x, y, vis);
  const r = brushArrays(x, y, d, {kind: 'rect', x0: 2.5, x1: 0, y0: 0, y1: 2.5});
  assert.deepEqual(Array.from(r.hit), [1, 1, 0, 0, 0, 0, 0]);
  assert.equal(r.count, 2);
  // row 2 (3,3) is inside but hidden: brushes only touch drawable rows
  const c = brushArrays(x, y, d, {kind: 'circle', cx: 2, cy: 2, r: 1.5});
  assert.deepEqual(Array.from(c.hit), [1, 1, 0, 0, 0, 0, 0]);
  const e = brushArrays(x, y, d, {kind: 'circle', cx: 0, cy: 1, r: 1.01, ry: 0.1});
  assert.deepEqual(Array.from(e.hit), [1, 0, 0, 0, 0, 0, 1]);
  assert.equal(brushArrays(x, y, d, {kind: 'circle', cx: 0, cy: 0, r: 0}).count, 0);
  const tri = {kind: 'lasso', pts: [[0, 0], [12, 0], [12, 12]]};
  assert.deepEqual(Array.from(brushArrays(x, y, d, tri).hit), [1, 1, 0, 0, 0, 1, 0]);
  assert.equal(brushArrays(x, y, d, {kind: 'lasso', pts: [[0, 0], [1, 1]]}).count, 0);
  // log axes: decade-space rect
  const lg = brushArrays(x, y, drawableXY(x, y, null, {xlog: true, ylog: true}), {kind: 'rect', x0: 1.5, x1: 20, y0: 1.5, y1: 20}, {xlog: true, ylog: true});
  assert.deepEqual(Array.from(lg.hit), [0, 1, 1, 0, 0, 1, 0]);
  assert.deepEqual(normShape({kind: 'rect', x0: 10, x1: 100, y0: 1, y1: 2}, {xlog: true}).x1, 2);
  assert.throws(() => normShape({kind: 'hexagon'}), /unknown brush shape/);
  assert.equal(pointInPolygon(0.5, 0.5, [[0, 0], [1, 0], [1, 1], [0, 1]]), true);
  assert.equal(pointInPolygon(1.5, 0.5, [[0, 0], [1, 0], [1, 1], [0, 1]]), false);
});

test('select: pickNearest grid and linear scan agree; radius, ties and N=0', () => {
  const N = 2000;
  let s = 7;
  const rnd = () => ((s = (s * 1103515245 + 12345) % 2147483648) / 2147483648);
  const x = new Float32Array(N), y = new Float32Array(N);
  for (let i = 0; i < N; i++) {
    x[i] = rnd() * 100;
    y[i] = rnd() * 50;
  }
  x[5] = NaN;
  const d = drawableXY(x, y, null);
  const grid = buildPickGrid(x, y, d);
  for (let q = 0; q < 50; q++) {
    const px = rnd() * 100, py = rnd() * 50;
    const a = pickNearest(x, y, d, px, py, 3, {grid});
    const b = pickNearest(x, y, d, px, py, 3);
    assert.equal(a, b, `query ${q}`);
    // anisotropic pixel scale
    assert.equal(pickNearest(x, y, d, px, py, 6, {kx: 2, ky: 4, grid}), pickNearest(x, y, d, px, py, 6, {kx: 2, ky: 4}));
  }
  assert.equal(pickNearest(x, y, d, 1000, 1000, 1, {grid}), -1);
  assert.equal(pickNearest(x, y, d, 1, 1, 0), -1);
  assert.equal(pickNearest(x, y, d, NaN, 1, 5), -1);
  // ties resolve to the lowest row
  const tx = Float32Array.from([1, 1]), ty = Float32Array.from([1, 1]);
  assert.equal(pickNearest(tx, ty, Uint8Array.from([1, 1]), 1, 1, 1), 0);
  const e = new Float32Array(0);
  assert.equal(pickNearest(e, e, new Uint8Array(0), 0, 0, 1, {grid: buildPickGrid(e, e, new Uint8Array(0))}), -1);
});

test('select: pick grid over sparse drawable rows off the vendored subsample stride', () => {
  // n = 40000 -> vendored buildGrid samples rows with i % 2 === 0; drawable rows are all odd.
  const N = 40000;
  const x = new Float32Array(N), y = new Float32Array(N);
  for (let i = 0; i < N; i++) {
    x[i] = i;
    y[i] = i;
  }
  const d = new Uint8Array(N);
  d[12345] = 1;
  d[20001] = 1;
  const grid = buildPickGrid(x, y, d);
  assert.ok(Number.isFinite(grid.x0) && grid.cw > 0, 'grid bounds are finite');
  assert.equal(grid.nvis, 2);
  assert.equal(pickNearest(x, y, d, 12345.5, 12345, 2, {grid}), 12345);
  assert.equal(pickNearest(x, y, d, 20000, 20001, 2, {grid}), 20001);
  // every other row drawable (all odd indices)
  const odd = new Uint8Array(N);
  for (let i = 1; i < N; i += 2) odd[i] = 1;
  const g2 = buildPickGrid(x, y, odd);
  assert.ok(Number.isFinite(g2.x0));
  assert.equal(pickNearest(x, y, odd, 777.2, 777, 1, {grid: g2}), 777);
  assert.equal(pickNearest(x, y, odd, 777.2, 777, 1, {grid: g2}), pickNearest(x, y, odd, 777.2, 777, 1));
  // a grid with NaN bounds falls back to the linear scan
  assert.equal(pickNearest(x, y, d, 12345, 12345, 2, {grid: {...grid, x0: NaN}}), 12345);
});

// ------------------------------------------------------------------ ops: raster

test('raster: histogram (linear / log / mask / degenerate) and bin2d', () => {
  const v = Float32Array.from([0, 1, 2, 3, 4, NaN, Infinity, -1]);
  const h = histogram(v, 4, {range: [0, 4]});
  assert.deepEqual(Array.from(h.counts), [1, 1, 1, 2]); // upper edge inclusive
  assert.equal(h.total, 5);
  assert.equal(h.under, 1);
  assert.deepEqual(Array.from(h.edges), [0, 1, 2, 3, 4]);
  const hm = histogram(v, 2, {mask: Uint8Array.from([1, 1, 0, 0, 0, 0, 0, 0])});
  assert.deepEqual(Array.from(hm.counts), [1, 1]);
  const hl = histogram(Float32Array.from([1, 10, 100, 1000, -5, 0]), 3, {log: true});
  assert.deepEqual(Array.from(hl.counts), [1, 1, 2]);
  assert.ok(Math.abs(hl.edges[3] - 1000) < 1e-9);
  const one = histogram(Float32Array.from([5]), 3);
  assert.equal(one.total, 1);
  assert.ok(one.edges[0] < 5 && one.edges[3] > 5);
  const none = histogram(new Float32Array(0), 3);
  assert.equal(none.total, 0);
  assert.deepEqual(finiteRange(Float32Array.from([NaN, 2, -3])), [-3, 2]);
  assert.equal(finiteRange(Float32Array.from([NaN])), null);

  const x = Float32Array.from([0, 1, 1, 2, NaN, 5]);
  const y = Float32Array.from([0, 0, 1, 2, 1, 5]);
  const b = bin2d(x, y, 2, 2, {xr: [0, 2], yr: [0, 2]});
  assert.deepEqual(Array.from(b.counts), [1, 1, 0, 2]);
  assert.equal(b.total, 4);
  const bm = bin2d(x, y, 1, 1, {mask: Uint8Array.from([1, 1, 0, 0, 0, 0])});
  assert.equal(bm.total, 2);
  assert.equal(countInWindow(x, y, null, 0, 1, 0, 1), 3);
});

// ------------------------------------------------------------------ ops: query

test('query: cats / ranges / flags / snr / text / ids semantics', () => {
  const cols = [
    {key: 'm', role: 'mag', unit: 'mag', err: 'em'},
    {key: 'em', role: 'error'},
    {key: 'f', role: 'flux', unit: 'Jy', err: 'ef'},
    {key: 'ef', role: 'error'},
    {key: 'c', role: 'category', levels: ['a', 'b']},
    {key: 'g', role: 'flag'},
  ];
  const data = {
    m: {values: Float32Array.from([10, 11, NaN, 12])},
    em: {values: Float32Array.from([0.01, 0.5, 0.1, NaN])},
    f: {values: Float32Array.from([1, 1, 1, 1])},
    ef: {values: Float32Array.from([0.1, 0.1, 0.5, 0.1])},
    c: {codes: Uint8Array.from([0, 1, 255, 1]), levels: ['a', 'b'], missing: 255},
    g: {bits: Uint8Array.from([1, 0, 1, 1])},
  };
  const src = {
    n: 4, cols, col: (k) => data[k] || null,
    expr: (e) => (e === 'm' ? data.m.values : Float32Array.from([1, 2, 3, 4])),
    textMatch: () => Uint8Array.from([1, 1, 0, 0]),
    idRows: () => [3, -1, 99],
  };
  assert.equal(isActive(null), false);
  assert.equal(isActive({cats: {c: []}, ranges: {m: [null, null]}}), false);
  assert.equal(isActive({ids: []}), true);
  const none = applyFilters({}, src);
  assert.equal(none.active, false);
  assert.equal(none.count, 4);
  assert.deepEqual(Array.from(applyFilters({cats: {c: ['b']}}, src).vis), [1, 0, 1, 0]);
  assert.deepEqual(Array.from(applyFilters({cats: {c: [0]}}, src).vis), [0, 1, 1, 1]);
  assert.deepEqual(Array.from(applyFilters({ranges: {m: [10.5, null]}}, src).vis), [0, 1, 0, 1]);
  assert.deepEqual(Array.from(applyFilters({flags: {g: 0}}, src).vis), [0, 1, 0, 0]);
  // S/N: mag 1.0857/em (108.6, 2.17, 10.9, -), flux f/ef (10, 10, 2, 10)
  const snr = applyFilters({snr: {min: 5, scope: 'all'}}, src);
  assert.deepEqual(Array.from(snr.vis), [1, 0, 0, 1]);
  assert.equal(snr.count, 2);
  assert.deepEqual(Array.from(applyFilters({snr: {min: 5, scope: 'projection', keys: ['m']}}, src).vis), [1, 0, 1, 1]);
  assert.deepEqual(snrBands({snr: {min: 1, scope: 'projection', keys: ['f', 'zzz']}}, cols), [{key: 'f', err: 'ef', kind: 'flux'}]);
  assert.deepEqual(Array.from(applyFilters({text: ' x '}, src).vis), [1, 1, 0, 0]);
  assert.deepEqual(Array.from(applyFilters({ids: ['q']}, src).vis), [0, 0, 0, 1]);
  const nd = filterNeeds({cats: {c: ['a']}, flags: {g: 1}, ranges: {'m - f': [0, 1], m: [null, null]}, snr: {min: 3}, text: 'x'}, cols);
  assert.deepEqual(nd.cols.sort(), ['c', 'ef', 'em', 'f', 'g', 'm']);
  assert.deepEqual(nd.exprs, ['m - f']);
  assert.equal(nd.ids, true);
  assert.throws(() => applyFilters({cats: {nope: ['a']}}, src), /unknown category/);
});

// ------------------------------------------------------------------ ops: sky

test('sky: separation, wrap, windows (RA wrap, pole), LOD cap, crossmatch, cone, nearest', () => {
  assert.ok(Math.abs(separation(0, 0, 1, 0) - 1) < 1e-12);
  assert.ok(Math.abs(separation(359.5, 0, 0.5, 0) - 1) < 1e-12);
  assert.equal(separation(10, 89.9, 190, 89.9).toFixed(6), (0.2).toFixed(6));
  assert.equal(wrapDRA(-180), 180);
  assert.equal(wrapDRA(350), -10);
  const inWin = windowTest({ra: 0, dec: 0, fov: 2});
  assert.equal(inWin(359.5, 0.5), true);
  assert.equal(inWin(2, 0), false);
  assert.equal(windowTest({ra: 0, dec: 89.5, fov: 2})(180, 89.8), true);

  const N = 1000;
  const ra = new Float64Array(N), dec = new Float64Array(N);
  for (let i = 0; i < N; i++) {
    ra[i] = (359 + (i % 40) * 0.05) % 360;
    dec[i] = -1 + Math.floor(i / 40) * 0.08;
  }
  ra[7] = NaN;
  const all = skyWindow(ra, dec, {ra: 0, dec: 0, fov: 10, cap: 5000});
  assert.equal(all.total, N - 1);
  assert.equal(all.sampled, false);
  const capped = skyWindow(ra, dec, {ra: 0, dec: 0, fov: 10, cap: 100}, {order: {kind: 'lod', levels: [50, 200, N]}});
  assert.equal(capped.idx.length, 100);
  assert.equal(capped.sampled, true);
  // the first LOD level (49 in-window rows below 50) is kept in full
  for (let i = 0; i < 50; i++) if (i !== 7) assert.ok(capped.idx.includes(i));
  const stride = skyWindow(ra, dec, {ra: 0, dec: 0, fov: 10, cap: 10});
  assert.equal(stride.idx.length, 10);
  assert.equal(skyWindow(ra, dec, {ra: 0, dec: 0, fov: 10, cap: 0}).idx.length, 0);
  const masked = skyWindow(ra, dec, {ra: 0, dec: 0, fov: 10, cap: 5000}, {mask: new Uint8Array(N)});
  assert.equal(masked.total, 0);

  const ix = buildDecIndex(ra, dec);
  assert.equal(ix.rows.length, N - 1);
  const qra = [ra[3] + 0.1 / 3600, 123, NaN, ra[7]];
  const qdec = [dec[3], 0, 0, dec[7]];
  assert.deepEqual(Array.from(crossmatch(ra, dec, qra, qdec, 1, ix)), [3, -1, -1, -1]);
  const cn = cone(ra, dec, ra[100], dec[100], 0.06 * 3600, {index: ix});
  assert.equal(cn[0].i, 100);
  assert.equal(cn[0].sep, 0);
  assert.ok(cn.every((h, k) => k === 0 || h.sep >= cn[k - 1].sep));
  assert.equal(cone(ra, dec, ra[100], dec[100], 1e4, {limit: 3}).length, 3);
  const nn = nearest(ra, dec, ra[100], dec[100], 3);
  assert.equal(nn.length, 3);
  assert.equal(nn[0].i, 100);
  assert.deepEqual(nn.map((h) => h.i), cone(ra, dec, ra[100], dec[100], 1e5).slice(0, 3).map((h) => h.i));
  assert.deepEqual(nearest(ra, dec, 0, 0, 0), []);
});

// ------------------------------------------------------------------ ops: find

test('find: parseCoords (decimal, sexagesimal, radius units, invalid)', () => {
  assert.deepEqual(parseCoords('266.4 -28.9'), {ra: 266.4, dec: -28.9, r: null});
  assert.deepEqual(parseCoords('266.4, −28.9, 5'), {ra: 266.4, dec: -28.9, r: 5});
  assert.deepEqual(parseCoords('266.4 -28.9 2arcmin'), {ra: 266.4, dec: -28.9, r: 120});
  assert.deepEqual(parseCoords("10 20 1'"), {ra: 10, dec: 20, r: 60});
  assert.deepEqual(parseCoords('10 20 0.5deg'), {ra: 10, dec: 20, r: 1800});
  const sx = parseCoords('17:45:40.04 -29:00:28.2');
  assert.ok(Math.abs(sx.ra - 266.41683333) < 1e-6 && Math.abs(sx.dec + 29.00783333) < 1e-6);
  const sl = parseCoords('17h45m40.04s -29d00m28.2s 3"');
  assert.ok(Math.abs(sl.ra - sx.ra) < 1e-9 && sl.r === 3);
  const sp = parseCoords('17 45 40.04 -29 00 28.2');
  assert.ok(Math.abs(sp.dec - sx.dec) < 1e-9);
  const neg0 = parseCoords('00:00:00 -00:30:00');
  assert.equal(neg0.dec, -0.5);
  for (const bad of ['', 'abc', '400 0', '10 95', '24:00:00 +00:00:00', '1 2 3 4', '10 20 5furlong', null]) {
    assert.equal(parseCoords(bad), null, String(bad));
  }
});

test('find: IdIndex search order, unicode, aliases, coordinates fallback', () => {
  const ids = idIndexOf(['J1', 'j1', 'J10', 'XJ1', 'Ünï', 'abc'], {'2MASS J9': 5, Alpha: 0});
  assert.equal(ids.get(4), 'Ünï');
  assert.equal(ids.get(99), '');
  assert.deepEqual(ids.exact('J1'), [0]);
  assert.equal(ids.alias('Alpha'), 0);
  assert.equal(ids.alias('nope'), -1);
  assert.deepEqual(findRows('J1', ids, null), [0, 1, 2, 3]);
  assert.deepEqual(findRows('j1', ids, null, {limit: 2}), [1, 0]);
  assert.deepEqual(findRows('ünï', ids, null), [4]);
  assert.deepEqual(findRows('2MASS J9', ids, null), [5]);
  assert.deepEqual(findRows('mass', ids, null), [5]);
  assert.deepEqual(findRows('', ids, null), []);
  assert.deepEqual(Array.from(ids.matchMask('J1')), [1, 1, 1, 1, 0, 0]);
  assert.deepEqual(Array.from(ids.matchMask('alp')), [1, 0, 0, 0, 0, 0]);
  assert.deepEqual(Array.from(ids.rowsOf(['abc', 'Alpha', 'zzz'])), [5, 0, -1]);
  const pos = {ra: Float64Array.from([10, 10.001, 20, 30, NaN, 10.5]), dec: Float64Array.from([0, 0, 0, 0, 0, 0])};
  pos.index = buildDecIndex(pos.ra, pos.dec);
  assert.deepEqual(findRows('10 0 10', ids, pos), [0, 1]);
  assert.deepEqual(findRows('10.0006 0', ids, pos, {limit: 2}), [1, 0]);
  assert.deepEqual(findRows('10 0', ids, null), []);
});

// ------------------------------------------------------------------ ops: project

test('project: kernel and sparse paths agree (weights, constants, fade, mask)', () => {
  const N = 300;
  const metaA = {enc: 'u16q', lo: 10, step: 0.001};
  const metaB = {enc: 'u16q', lo: -5, step: 0.0005};
  const ca = new Uint16Array(N), cb = new Uint16Array(N);
  for (let i = 0; i < N; i++) {
    ca[i] = i % 17 === 0 ? 0 : 1 + ((i * 7919) % 60000);
    cb[i] = i % 23 === 0 ? 0 : 1 + ((i * 104729) % 60000);
  }
  const deq = (m, c) => Float32Array.from(c, (k) => (k === 0 ? NaN : m.lo + (k - 1) * m.step));
  const va = deq(metaA, ca), vb = deq(metaB, cb);
  const mask = new Uint8Array(N).fill(1);
  mask[1] = 0;
  for (const fade of [{a: 0, b: 0}, {a: 1, b: 0.5}, [0.3, 1]]) {
    const spec = {terms: [{w: [1, -1], keys: ['a', 'b']}, {w: [0.5], keys: ['b']}], c: [2, -3], fade};
    const ns = normSpec(spec);
    assert.deepEqual(ns.keys, ['a', 'b']);
    const k = projectKernel(ns, [{codes: ca, meta: metaA}, {codes: cb, meta: metaB}], N, mask);
    const s = projectSparse(ns, [va, vb], N, mask);
    for (let i = 0; i < N; i++) {
      assert.equal(k.vis[i], s.vis[i], `vis[${i}] fade ${JSON.stringify(fade)}`);
      if (k.vis[i] && Number.isFinite(s.x[i])) {
        assert.ok(Math.abs(k.x[i] - s.x[i]) < 2e-3, `x[${i}] ${k.x[i]} vs ${s.x[i]}`);
        assert.ok(Math.abs(k.y[i] - s.y[i]) < 2e-3, `y[${i}]`);
      }
    }
    assert.equal(k.vis[1], 0);
  }
  const src = {
    n: N,
    meta: (key) => ({a: metaA, b: metaB, f: {enc: 'f32'}}[key] || null),
    codes: (key) => ({a: ca, b: cb}[key]),
    values: (key) => ({a: va, b: vb, f: vb}[key]),
  };
  assert.equal(project({terms: [{w: [1], keys: ['a']}, {w: [1], keys: ['b']}]}, src).path, 'kernel');
  assert.equal(project({terms: [{w: [1], keys: ['a']}, {w: [1], keys: ['f']}]}, src).path, 'sparse');
  assert.equal(kernelEligible(normSpec({terms: [{w: [1], keys: ['a']}, {w: [], keys: []}]}), [metaA]), true);
  // duplicate keys sum; non-finite weights throw; N = 0 works
  const dup = normSpec({terms: [{w: [1, 2], keys: ['a', 'a']}, {w: [1], keys: ['a']}]});
  assert.deepEqual(Array.from(dup.wx), [3]);
  assert.throws(() => normSpec({terms: [{w: [NaN], keys: ['a']}]}), /non-finite/);
  const z = projectSparse(normSpec({terms: [{w: [1], keys: ['a']}, {w: [1], keys: ['a']}]}), [new Float32Array(0)], 0);
  assert.equal(z.x.length, 0);
  const cache = new ProjCache(2);
  cache.set(1, 'a');
  cache.set(2, 'b');
  cache.set(3, 'c');
  assert.equal(cache.get(1), null);
  assert.equal(cache.get(3), 'c');
});

// ------------------------------------------------------------------ ops: export

test('export: CSV escaping and round trip, formatting, JSON, ids, DS9', () => {
  assert.equal(csvEscape('a,b'), '"a,b"');
  assert.equal(csvEscape('say "hi"'), '"say ""hi"""');
  assert.equal(csvEscape('plain'), 'plain');
  assert.equal(f32String(Math.fround(0.1)), '0.1');
  assert.equal(f32String(NaN), '');
  assert.equal(formatValue(NaN, {enc: 'u16q', step: 0.001}), '');
  assert.equal(formatValue(Infinity), 'inf');
  assert.equal(formatValue(-Infinity), '-inf');
  assert.equal(formatValue(12.3456789, {enc: 'u16q', step: 0.001}), '12.346');
  assert.equal(formatValue(1e300, {}, {exact: true}), '1e+300');
  assert.equal(formatValue(null), '');
  assert.equal(formatValue(true), '1');
  const header = ['id', 'note', 'v'];
  const rows = [['é,1', 'line\nbreak', '1.5'], ['"q"', '', '-inf'], ['☃', '# not a comment', '']];
  const text = toCSV(header, rows, {comments: ['made by test', 'second']});
  assert.ok(text.startsWith('# made by test\n# second\n'));
  assert.ok(text.endsWith('\n'));
  assert.deepEqual(parseCSV(text), [header, ...rows]);
  assert.deepEqual(parseCSV(toCSV(['a'], [])), [['a']]);
  const js = JSON.parse(toJSON(['id', 'v', 'w'], [['x', '1.5', ''], ['y', 'inf', '2']], {numeric: ['v', 'w']}));
  assert.equal(js[0].v, 1.5);
  assert.equal(js[0].w, null);
  assert.equal(js[1].id, 'y');
  assert.equal(toIdList(['a', 'b']), 'a\nb\n');
  assert.equal(toIdList([]), '');
  const ds9 = toDS9([{id: 'src {1}', ra: 266.4, dec: -28.9}, {id: 'nan', ra: NaN, dec: 0}], {r: 2, color: 'red'});
  const lines = ds9.trim().split('\n');
  assert.equal(lines[0], '# Region file format: DS9 version 4.1');
  assert.ok(lines.includes('global color=red'));
  assert.ok(lines.includes('fk5'));
  assert.ok(lines.some((l) => l.startsWith('circle(266.4000000,-28.9000000,2") # text={src  1 }')));
  assert.ok(!lines.some((l) => l.includes('text={nan}')));
});

// ------------------------------------------------------------------ engine: mini bundle

test('engine (mini): metadata, values, chunks, validity, gpuColumns, progress', async () => {
  const {c, f} = await openMini();
  assert.equal(c.n, 5);
  assert.equal(c.local, true);
  assert.equal(c.col('ra').role, 'ra');
  assert.equal(c.col('mag').enc, 'u16q');
  assert.equal(c.col('mag - emag').virtual, true);
  assert.equal(c.col('nope'), null);
  assert.equal(c.isVirtual('mag'), false);
  assert.equal(c.isVirtual('mag - emag'), true);
  assert.equal(c.isVirtual('ra'), false);
  assert.deepEqual(c.baseKeys('mag - 2*emag').sort(), ['emag', 'mag']);
  const prog = [];
  c.onProgress = (p) => prog.push(p);
  await c.ensure(['mag', 'emag']);
  assert.ok(prog.length && prog.at(-1).loaded === prog.at(-1).total && prog.at(-1).ds === 'mini');
  const mag = await c.values('mag');
  assert.ok(mag instanceof Float32Array);
  assert.deepEqual(Array.from(mag).map((v) => (Number.isNaN(v) ? null : +v.toFixed(2))), [10, 11, null, 12, 665.34]);
  const diff = await c.values('mag - emag', {prefix: 2});
  assert.equal(diff.length, 2);
  assert.ok(Math.abs(diff[1] - 10.5) < 1e-5);
  // validity: flux is NaN where good == 0 (rows 1 and 3)
  const flux = await c.values('flux');
  assert.deepEqual(Array.from(flux, (v) => (Number.isNaN(v) ? null : +v.toFixed(3))), [0.1, null, 0.3, null, 0.5]);
  const ra = await c.values('ra');
  assert.ok(Math.abs(ra[0] - 10.1) < 1e-5 && Number.isNaN(ra[3]));
  // gpuColumns: u16q codes pass through; u = A·code/65535 + B
  const g = await c.gpuColumns(['mag', 'emag']);
  assert.deepEqual(g.keys, ['mag', 'emag']);
  assert.deepEqual(Array.from(g.raw[0]), [1, 101, 0, 201, 65535]);
  const u = (d, i) => (g.A[d] * g.raw[d][i]) / 65535 + g.B[d];
  assert.ok(Math.abs(u(0, 3) - 12) < 1e-9);
  assert.equal(g.raw[1][2], 0);
  assert.ok(Math.abs(u(1, 1) - 0.5) < 1e-4 && Math.abs(u(1, 0) - 0.01) < 1e-4);
  const gp = await c.gpuColumns(['mag'], {prefix: 2});
  assert.equal(gp.raw[0].length, 2);
  await assert.rejects(c.values('nosuch + 1'), /unknown column nosuch/);
  // each file fetched once
  const counts = new Map();
  for (const u2 of f.calls) counts.set(u2, (counts.get(u2) || 0) + 1);
  for (const [k, v] of counts) assert.equal(v, 1, k);
  c.terminate();
});

test('engine (mini): query / vis / hist / bin2d / pick / brush / selection', async () => {
  const {c} = await openMini();
  const q = await c.query({cats: {cls: ['star']}});
  assert.deepEqual(Array.from(q.vis), [0, 1, 1, 1, 0]);
  assert.equal(q.count, 3);
  assert.equal((await c.vis()).vis, null, 'query without apply leaves the page vis alone');
  assert.deepEqual(Array.from((await c.query({flags: {good: 1}})).vis), [1, 0, 1, 0, 1]);
  assert.deepEqual(Array.from((await c.query({ranges: {mag: [10.5, 100]}})).vis), [0, 1, 0, 1, 0]);
  assert.deepEqual(Array.from((await c.query({snr: {min: 5, scope: 'all'}})).vis), [1, 0, 1, 1, 1]);
  assert.deepEqual(Array.from((await c.query({text: 'A1'})).vis), [1, 0, 0, 0, 1]);
  assert.deepEqual(Array.from((await c.query({text: 'mass'})).vis), [0, 0, 1, 0, 0]);
  assert.deepEqual(Array.from((await c.query({ids: ['c3', 'Ünï', 'Other-é', 'zz']})).vis), [0, 1, 1, 1, 0]);
  const applied = await c.query({cats: {cls: ['yso']}}, {apply: true});
  assert.equal(applied.count, 3);
  assert.deepEqual(Array.from((await c.vis()).vis), [1, 0, 1, 0, 1]);

  const h = await c.hist('mag', 2, {range: [10, 12]});
  assert.deepEqual(Array.from(h.counts), [1, 2]);
  assert.equal(h.over, 1);
  const hv = await c.hist('mag', 2, {range: [10, 12], mask: 'vis'});
  assert.deepEqual(Array.from(hv.counts), [1, 0]);

  const view = {pane: 'p', kind: 'xy', x: 'mag', y: 'emag'};
  const b2 = await c.bin2d(view, 2, 1, {xr: [10, 12], yr: [0, 1]});
  assert.equal(b2.total, 1, 'vis mask by default (rows 0, 4; row 4 out of range)');
  const b2all = await c.bin2d(view, 2, 1, {xr: [10, 12], yr: [0, 1], mask: null});
  assert.equal(b2all.total, 3);

  // brush acts on drawable rows only (vis hides rows 1 and 3)
  const br = await c.brush(view, {kind: 'rect', x0: 9, x1: 13, y0: 0, y1: 1});
  assert.deepEqual(Array.from(br.hit), [1, 0, 0, 0, 0]);
  assert.deepEqual(Array.from(br.drawable), [1, 0, 0, 0, 1]);
  assert.equal(br.count, 1);
  const dv = {...view, kx: 1, ky: 1}; // 1 px per data unit: rPx in data units
  assert.equal(await c.pick(dv, 10.02, 0.01, 0.1), 0);
  assert.equal(await c.pick(dv, 11, 0.5, 0.1), -1, 'hidden rows are not pickable');
  assert.equal(await c.pick(dv, 600, 0.1, 100), 4);
  assert.equal(await c.pick({...view, kx: 100, ky: 100}, 10.2, 0.01, 6), -1);
  assert.equal(await c.pick({...view, kx: 100, ky: 100}, 10.03, 0.01, 6), 0);
  // rPx is a pixel radius: a ViewRef without a pixel scale is refused (no silent data-unit radius)
  await assert.rejects(c.pick(view, 10.02, 0.01), TypeError);
  const eng = createEngine({fetch: fakeFetch(miniBundle())});
  await eng.handlers.open({url: MINI + 'manifest.json'}, {});
  await assert.rejects(eng.handlers.pick({view, px: 10, py: 0}, {}), /kx, ky/);

  await c.query({}, {apply: true});
  assert.equal((await c.vis()).vis, null);
  const p = await c.project({terms: [{w: [1], keys: ['mag']}, {w: [2], keys: ['mag']}], c: [0, 1]}, {token: 7});
  assert.equal(p.path, 'kernel');
  assert.deepEqual(Array.from(p.vis).map((v) => (v ? 1 : 0)), [1, 1, 0, 1, 1]);
  assert.ok(Math.abs(p.y[1] - 23) < 1e-4);
  const ps = await c.project({terms: [{w: [1], keys: ['mag']}, {w: [1], keys: ['emag']}], fade: {emag: 1}});
  assert.equal(ps.path, 'sparse');
  const pv = {pane: 'p', kind: 'proj', token: 7};
  const pb = await c.brush(pv, {kind: 'rect', x0: 10.5, x1: 12.5, y0: 0, y1: 100});
  assert.deepEqual(Array.from(pb.hit), [0, 1, 0, 1, 0]);
  assert.equal(await c.pick({...pv, kx: 1, ky: 1}, 12, 25, 0.5), 3);
  await assert.rejects(c.pick({pane: 'p', kind: 'proj', token: 99, kx: 1, ky: 1}, 0, 0, 1), /unknown projection token/);

  // selection mask feeds 'sel'
  const sel = c.selection();
  assert.equal(c.selection(), sel);
  assert.equal((await c.hist('mag', 1, {mask: 'sel'})).total, 0);
  sel.apply(Uint8Array.from([1, 1, 0, 0, 1]), 'replace');
  assert.equal((await c.hist('mag', 1, {mask: 'sel'})).total, 3);
  const ids = await (await c.export({mask: 'sel', format: 'ids'})).text();
  assert.equal(ids, 'a1\nB2\na10\n');
  c.terminate();
});

test('engine (mini): rows (quantized), record, id, find, crossmatch, skyWindow, exports', async () => {
  const {c} = await openMini();
  const rows = await c.rows(0, 99);
  assert.equal(rows.length, 5);
  assert.equal(rows[0].id, 'a1');
  assert.equal(rows[0]._quantized, true);
  assert.equal(rows[0].mag, 10);
  assert.equal(rows[2].mag, null);
  assert.equal(rows[2].cls, null);
  assert.equal(rows[1].cls, 'yso');
  assert.equal(rows[3].good, 0);
  assert.equal(rows[4].mag, 665.34);
  assert.equal(rows[3].ra, null);
  assert.deepEqual(await c.rows(3, 3), []);
  assert.deepEqual(await c.rows(-5, 1).then((r) => r.map((x) => x._i)), [0]);
  const rec = await c.record(2);
  assert.equal(rec._id, 'c3');
  assert.deepEqual(rec._limits, {flux: 1});
  assert.equal((await c.record(0))._limits, undefined);
  const [r1, r2] = await Promise.all([c.record(3, {token: true}), c.record(4, {token: true})]);
  assert.equal(r1, null, 'superseded record request resolves to null');
  assert.equal(r2._id, 'a10');
  assert.equal(await c.id(3), 'Ünï');
  assert.equal(await c.id(3), 'Ünï');

  assert.deepEqual(await c.find('a1'), [0, 4]);
  assert.deepEqual(await c.find('ÜNÏ'), [3]);
  assert.deepEqual(await c.find('2MASS J1'), [2]);
  assert.deepEqual(await c.find('10.5 0 2'), [1]);
  assert.deepEqual(await c.find('10.5 0 10'), [1, 4]);
  assert.deepEqual(await c.find('10.5 0', {limit: 2}), [1, 4]);
  assert.deepEqual(await c.find('zzz'), []);

  const xm = await c.crossmatch([10.1, 10.9, 50], [-0.5, 0.5, 0], 1);
  assert.deepEqual(Array.from(xm), [0, 2, -1]);
  const sw = await c.skyWindow({ra: 10.5, dec: 0, fov: 0.2});
  assert.deepEqual(Array.from(sw.idx), [1, 4]);
  assert.equal(sw.sampled, false);
  const capped = await c.skyWindow({ra: 10.5, dec: 0, fov: 4, cap: 2});
  assert.equal(capped.idx.length, 2);
  assert.equal(capped.total, 4);

  const csv = await (await c.exportCSV()).text();
  const table = parseCSV(csv);
  assert.deepEqual(table[0], ['id', 'ra', 'dec', 'mag', 'emag', 'cls', 'good', 'flux']);
  assert.equal(table.length, 6);
  assert.equal(table[1][0], 'a1');
  assert.equal(table[1][3], '10.00', 'quantized values keep the stored step precision');
  assert.equal(table[3][3], '');
  assert.equal(table[4][1], '');
  assert.equal(table[5][3], '665.34');
  assert.equal(table[2][5], 'yso');
  assert.ok(csv.startsWith('id,ra,dec,'), 'no comment line before the header (astropy / pandas readers)');
  const sub = parseCSV(await (await c.exportCSV({cols: ['mag - emag']})).text());
  assert.deepEqual(sub[0], ['id', 'ra', 'dec', 'mag - emag']);
  assert.equal(sub[2][3], '10.5');
  const js = JSON.parse(await (await c.export({format: 'json', cols: ['mag']})).text());
  assert.equal(js.length, 5);
  assert.equal(js[0].mag, 10);
  assert.equal(js[2].mag, null);
  const ds9 = await (await c.export({format: 'ds9', r: 0.5})).text();
  assert.equal(ds9.split('\n').filter((l) => l.startsWith('circle(')).length, 4);
  const blob = await c.export({format: 'ids'});
  assert.ok(blob.type.startsWith('text/'));
  c.terminate();
});

test('engine: errors (404 manifest, non-catalog, abort, rpc progress / cancel)', async () => {
  const files = miniBundle();
  files.set('https://mini.test/bad/manifest.json', JSON.stringify({hello: 1}));
  const f = fakeFetch(files);
  await assert.rejects(DataClient.open('https://mini.test/none/manifest.json', {worker: false, fetch: f}), (e) => e.status === 404);
  await assert.rejects(DataClient.open('https://mini.test/bad/manifest.json', {worker: false, fetch: f}), /not a pk-catalog manifest/);
  // engine directly over the local rpc
  const eng = createEngine({fetch: f});
  const prog = [];
  const rpc = createLocalRpc(eng.handlers, {onProgress: (p) => prog.push(p)});
  await assert.rejects(rpc.call('values', {expr: 'mag'}), /catalog not open/);
  const info = await rpc.call('open', {url: MINI + 'manifest.json'});
  assert.equal(info.n, 5);
  const ac = new AbortController();
  ac.abort();
  await assert.rejects(rpc.call('ensure', {keys: ['mag']}, {signal: ac.signal}), (e) => e.name === 'AbortError');
  await rpc.call('ensure', {keys: ['mag', 'cls']});
  assert.ok(prog.length > 0);
  const inf = await rpc.call('info', {});
  assert.deepEqual(inf.loaded.sort(), ['cls', 'mag']);
  await assert.rejects(rpc.call('density', {key: 'nope'}), /no density grid/);
  await assert.rejects(rpc.call('setSel', {mask: new Uint8Array(2)}), /expected 5/);
  assert.equal(quantizedNumber(12.34567, {enc: 'u16q', step: 0.01}), 12.35);
  assert.equal(quantizedNumber(NaN, {enc: 'u16q', step: 0.01}), null);
  assert.equal(quantizedNumber(Math.fround(0.1), {enc: 'f32'}), 0.1);
});

// ------------------------------------------------------------------ engine: Python-written bundles

const REPO = fileURLToPath(new URL('../../', import.meta.url));
const PY = process.env.PYTHON || 'python';

const PY_SCRIPT = String.raw`
import json, math, sys
from pathlib import Path
sys.path[:0] = [sys.argv[1], str(Path(sys.argv[1]) / 'tests' / 'py')]
from test_catalog_bundle import make_table, make_spec
from portalkit.bundle import catalog
out = Path(sys.argv[2])
for n in (0, 1, 37):
    t = make_table(n)
    catalog.write(make_spec(t), out / f'n{n}')
    def clean(v):
        v = v.item() if hasattr(v, 'item') else v
        return None if isinstance(v, float) and not math.isfinite(v) else v
    exp = {k: [clean(v) for v in t[k]] for k in ('id', 'ra', 'dec', 'F444W', 'teff', 'cls', 'good', 'lim')}
    (out / f'n{n}' / 'expected.json').write_text(json.dumps(exp))
print('ok')
`;

let pyDir = null;
let pyReason = null;
function pyBundles() {
  if (pyDir || pyReason) return pyDir;
  const base = process.env.PK_JS_TMP || tmpdir();
  const dir = mkdtempSync(join(base, 'pk-select-'));
  const r = spawnSync(PY, ['-c', PY_SCRIPT, REPO, dir], {encoding: 'utf8', timeout: 120000, cwd: REPO});
  if (r.status !== 0 || !existsSync(join(dir, 'n37', 'manifest.json'))) {
    pyReason = `python bundle writer unavailable: ${(r.stderr || r.error || '').toString().trim().split('\n').pop()}`;
    rmSync(dir, {recursive: true, force: true});
    return null;
  }
  pyDir = dir;
  process.on('exit', () => rmSync(dir, {recursive: true, force: true}));
  return pyDir;
}

function diskFetch(root, prefix) {
  return async (url) => {
    const u = String(url);
    if (!u.startsWith(prefix)) return new Response('', {status: 404});
    const p = join(root, decodeURIComponent(u.slice(prefix.length)));
    if (!existsSync(p)) return new Response('', {status: 404});
    return new Response(readFileSync(p), {status: 200});
  };
}

async function openPy(n) {
  const dir = pyBundles();
  if (!dir) return null;
  const prefix = 'https://py.test/';
  const c = await DataClient.open(`${prefix}n${n}/manifest.json`, {worker: false, fetch: diskFetch(dir, prefix)});
  const exp = JSON.parse(readFileSync(join(dir, `n${n}`, 'expected.json'), 'utf8'));
  return {c, exp};
}

test('engine (python bundle, N=37): values, exact rows, limits, filters, find, crossmatch, export', async (t) => {
  const o = await openPy(37);
  if (!o) return t.skip(pyReason);
  const {c, exp} = o;
  const man = c.manifest;
  assert.equal(c.n, 37);
  assert.equal(man.order.kind, 'input');
  const step = c.col('F444W').step;
  const f444 = await c.values('F444W');
  exp.F444W.forEach((v, i) => {
    if (v === null) assert.ok(Number.isNaN(f444[i]), `F444W[${i}]`);
    else assert.ok(Math.abs(f444[i] - v) <= step / 2 + 1e-5 * Math.abs(v), `F444W[${i}] ${f444[i]} vs ${v}`);
  });
  const teff = await c.values('teff');
  exp.teff.forEach((v, i) => assert.ok(Math.abs(teff[i] / v - 1) < 1e-3, `teff[${i}]`));
  const ra = await c.values('ra');
  exp.ra.forEach((v, i) => assert.ok(Math.abs(ra[i] - v) < 3e-5, `ra[${i}] (Float32)`));
  const rows = await c.rows(0, 37);
  assert.equal(rows.length, 37);
  rows.forEach((r, i) => {
    assert.equal(r._i, i);
    assert.equal(r.id, exp.id[i]);
    assert.equal(r.F444W ?? null, exp.F444W[i]);
  });
  const rec1 = await c.record(1);
  assert.equal(rec1._id, exp.id[1]);
  assert.equal(rec1._limits && rec1._limits.flux, 1);
  assert.equal((await c.record(2))._limits.flux, 2);
  const rec0 = await c.record(0);
  assert.ok(!rec0._limits || rec0._limits.flux === undefined);

  const nStar = exp.cls.filter((v) => v === 'star').length;
  const q = await c.query({cats: {cls: ['star']}});
  assert.equal(q.count, 37 - nStar);
  const qg = await c.query({flags: {good: 1}});
  assert.equal(qg.count, exp.good.filter(Boolean).length);
  const qr = await c.query({ranges: {F444W: [15, 18]}});
  assert.equal(qr.count, exp.F444W.filter((v) => v !== null && v >= 15 - step && v <= 18 + step).length);

  assert.deepEqual(await c.find(exp.id[5]), [5]);
  assert.deepEqual((await c.find(exp.id[5].toLowerCase())).slice(0, 1), [5]);
  assert.deepEqual(await c.find(`${exp.ra[9]} ${exp.dec[9]} 0.5`), [9]);
  const xm = await c.crossmatch(exp.ra, exp.dec, 0.1);
  assert.deepEqual(Array.from(xm), exp.id.map((_, i) => i));

  const csv = parseCSV(await (await c.exportCSV({exact: true})).text());
  assert.equal(csv.length, 38);
  const hid = csv[0].indexOf(man.rows.cols[0]);
  assert.ok(hid >= 0);
  assert.deepEqual(csv.slice(1).map((r) => r[hid]), exp.id);
  const fcol = csv[0].indexOf('F444W');
  csv.slice(1).forEach((r, i) => {
    if (exp.F444W[i] === null) assert.equal(r[fcol], '');
    else assert.equal(Number(r[fcol]), exp.F444W[i]);
  });
  const quant = parseCSV(await (await c.exportCSV()).text());
  assert.equal(quant.length, 38);
  assert.deepEqual(quant.slice(1).map((r) => r[0]), exp.id);
  c.terminate();
});

test('engine (python bundles, N=0 and N=1): empty and single-row catalogs', async (t) => {
  const o0 = await openPy(0);
  if (!o0) return t.skip(pyReason);
  const c0 = o0.c;
  assert.equal(c0.n, 0);
  assert.equal((await c0.values('F444W')).length, 0);
  assert.deepEqual(await c0.rows(0, 10), []);
  assert.equal((await c0.query({cats: {cls: ['star']}})).count, 0);
  assert.deepEqual(await c0.find('J000000'), []);
  assert.equal((await c0.hist('F444W', 4)).total, 0);
  const csv0 = parseCSV(await (await c0.exportCSV()).text());
  assert.equal(csv0.length, 1);
  c0.terminate();

  const {c: c1, exp} = await openPy(1);
  assert.equal(c1.n, 1);
  const v = await c1.values('F444W');
  assert.equal(v.length, 1);
  assert.equal(exp.F444W[0], null);
  assert.ok(Number.isNaN(v[0]));
  const tv = await c1.values('teff');
  assert.ok(Math.abs(tv[0] / exp.teff[0] - 1) < 1e-3);
  const rows = await c1.rows(0, 1);
  assert.equal(rows[0].id, exp.id[0]);
  assert.deepEqual(await c1.find(exp.id[0]), [0]);
  const p = await c1.project({terms: [{w: [1], keys: ['F444W']}, {w: [1], keys: ['teff']}]});
  assert.equal(p.x.length, 1);
  assert.equal(p.vis[0], 0, 'missing F444W with fade 0 hides the row');
  const pf = await c1.project({terms: [{w: [1], keys: ['F444W']}, {w: [1], keys: ['teff']}], fade: {F444W: 1}});
  assert.ok(pf.vis[0] > 0);
  assert.equal(await c1.pick({pane: 'p', kind: 'xy', x: 'teff', y: 'teff', kx: 1, ky: 1}, tv[0], tv[0], 1), 0);
  assert.equal(await c1.pick({pane: 'p', kind: 'xy', x: 'F444W', y: 'teff', kx: 1, ky: 1}, 0, tv[0], 1e9), -1);
  c1.terminate();
});

// ------------------------------------------------------------------ hub

test('hub: catalog / filters -> vis / json pages / records / errors / stats', async () => {
  const {createHub, substitute, paneLeaves, projectionKeys} = await import('../../web/js/data/hub.js');
  const {createStore} = await import('../../web/js/core/store.js');
  const files = miniBundle();
  files.set('https://mini.test/img/images.json', JSON.stringify({schema: 'pk-images/1', items: [{id: 'a'}], pages: ['p1.json', {url: 'p2.json'}]}));
  files.set('https://mini.test/img/p1.json', JSON.stringify([{id: 'b'}]));
  files.set('https://mini.test/img/p2.json', JSON.stringify({items: [{id: 'c'}]}));
  files.set('https://mini.test/rec/index.json', JSON.stringify({kind: 'json', entries: {}}));
  const f = fakeFetch(files);
  const page = {
    assets: 'https://mini.test',
    layout: {row: [{pane: 'cmd', id: 'cmd', dataset: 'mini'}, {col: [{pane: 'sky', id: 'sky'}]}]},
    datasets: {
      mini: {kind: 'catalog', url: '{assets}/cat/manifest.json'},
      imgs: {kind: 'images', url: '{assets}/img/images.json'},
      recs: {kind: 'records', url: '{assets}/rec/index.json'},
      gone: {kind: 'catalog', url: '{assets}/gone/manifest.json'},
    },
  };
  assert.equal(substitute('{assets}/x', page), 'https://mini.test/x');
  assert.deepEqual(paneLeaves(page.layout).map((p) => p.pane), ['cmd', 'sky']);
  const store = createStore({'vis:mini': {version: 0, count: null}});
  const errors = [];
  const toasts = [];
  store.onEvent('data:error', (e) => errors.push(e));
  store.onEvent('toast', (e) => toasts.push(e));
  const hub = createHub(page, store, {base: 'https://mini.test/page/', worker: false, fetch: f});
  assert.equal(hub.resolve('{assets}/a.json').toString(), 'https://mini.test/a.json');
  const c = await hub.catalog('mini');
  assert.equal(await hub.catalog('mini'), c);
  assert.equal(c.n, 5);

  // filters:<ds> drives vis:<ds> through the worker query op
  const seen = new Promise((resolve) => {
    const off = store.on(['vis:mini'], () => {
      off();
      resolve(store.get('vis:mini'));
    });
  });
  store.set({'filters:mini': {cats: {cls: ['star']}}});
  store.flush();
  const vis = await seen;
  assert.equal(vis.count, 3);
  assert.ok(vis.version >= 1);
  assert.deepEqual(Array.from((await c.vis()).vis), [0, 1, 1, 1, 0]);

  store.set({'axes:cmd': {x: 'mag - emag', y: 'flux'}, 'frame:sky': {dims: ['mag', 'cls'], F: [1, 0, 0, 0]}});
  assert.deepEqual(projectionKeys(page, store, 'mini').sort(), ['emag', 'flux', 'mag']);

  const imgs = await hub.json('imgs');
  assert.deepEqual(imgs.items.map((i) => i.id), ['a', 'b', 'c']);
  const rs = await hub.records('recs');
  assert.equal(rs.n, 0);
  await assert.rejects(hub.catalog('gone'), (e) => e.status === 404);
  store.flush();
  assert.equal(errors.length, 1);
  assert.equal(errors[0].ds, 'gone');
  assert.equal(errors[0].status, 404);
  assert.equal(toasts[0].level, 'error');
  await assert.rejects(hub.catalog('unknown-ds'), /unknown dataset/);
  store.flush();
  assert.equal(errors.at(-1).ds, 'unknown-ds');
  const st = hub.stats();
  assert.ok(st.fetches > 0 && st.bytes > 0);
  hub.dispose();
});

// ------------------------------------------------------------------ rpc transport

test('rpc: serve / createRpcClient over a MessageChannel (results, transfer, progress, errors, cancel)', async () => {
  const {serve, createRpcClient, withTransfer, serializeError, deserializeError} = await import('../../web/js/workers/rpc.js');
  const {port1, port2} = new MessageChannel();
  serve(port1, {
    async echo(args) {
      return args;
    },
    async arr({n}) {
      const a = new Float32Array(n).fill(1.5);
      return withTransfer({a}, [a]);
    },
    async prog(args, ctx) {
      ctx.progress({loaded: 1, total: 2});
      ctx.progress({loaded: 2, total: 2});
      return 'done';
    },
    async fail() {
      const e = new Error('nope é');
      e.status = 418;
      e.url = 'u';
      throw e;
    },
    async slow(args, ctx) {
      await new Promise((resolve, reject) => {
        const t = setTimeout(resolve, 2000);
        ctx.signal.addEventListener('abort', () => {
          clearTimeout(t);
          reject(new Error('aborted inside'));
        });
      });
      return 'late';
    },
  }, {getStats: () => ({fetches: 3, bytes: 4, decodeMs: 0})});
  let stats = null;
  const seenProg = [];
  const cli = createRpcClient(port2, {onStats: (s) => (stats = s), onProgress: (p, op) => seenProg.push([op, p.loaded])});
  try {
    assert.deepEqual(await cli.call('echo', {s: 'ü', v: [1, null]}), {s: 'ü', v: [1, null]});
    assert.deepEqual(stats, {fetches: 3, bytes: 4, decodeMs: 0});
    const r = await cli.call('arr', {n: 4});
    assert.ok(r.a instanceof Float32Array && r.a[3] === 1.5);
    const local = [];
    assert.equal(await cli.call('prog', {}, {onProgress: (p) => local.push(p.loaded)}), 'done');
    assert.deepEqual(local, [1, 2]);
    assert.deepEqual(seenProg, [['prog', 1], ['prog', 2]]);
    await assert.rejects(cli.call('fail'), (e) => e.message === 'nope é' && e.status === 418 && e.url === 'u');
    await assert.rejects(cli.call('missingOp'), /unknown op missingOp/);
    const ac = new AbortController();
    const p = cli.call('slow', {}, {signal: ac.signal});
    setTimeout(() => ac.abort(), 20);
    await assert.rejects(p, (e) => e.name === 'AbortError');
    assert.equal(cli.pending, 0);
    const p2 = cli.call('slow', {});
    cli.terminate();
    await assert.rejects(p2, /worker terminated/);
  } finally {
    port1.close();
    port2.close();
  }
  const se = serializeError('plain');
  assert.equal(deserializeError(se).message, 'plain');
});

test('client: a worker that fails to load falls back to the in-thread engine', async () => {
  const prev = globalThis.Worker;
  let made = 0;
  globalThis.Worker = class FakeWorker {
    constructor() {
      made++;
    }
    postMessage() {
      setTimeout(() => this.onerror && this.onerror({message: 'module script failed'}), 1);
    }
    terminate() {}
  };
  try {
    const c = await DataClient.open(MINI + 'manifest.json', {fetch: fakeFetch(miniBundle())});
    assert.equal(made, 1);
    assert.equal(c.local, true);
    assert.equal(c.n, 5);
    assert.equal(c.workerStats(), null);
    assert.equal((await c.values('mag')).length, 5);
  } finally {
    if (prev === undefined) delete globalThis.Worker;
    else globalThis.Worker = prev;
  }
});

// ------------------------------------------------------------------ review fixes

test('engine: an older applying query that finishes last does not overwrite the newer vis', async () => {
  const files = miniBundle();
  const base = fakeFetch(files);
  const slow = async (url) => {
    if (String(url).endsWith('cols/emag.bin')) await new Promise((r) => setTimeout(r, 50));
    return base(url);
  };
  const c = await DataClient.open(MINI + 'manifest.json', {worker: false, fetch: slow});
  const pa = c.query({ranges: {emag: [0, 0.05]}}, {apply: true}); // waits on emag
  const pb = c.query({}, {apply: true}); // newer, no fetch
  const [ra, rb] = await Promise.all([pa, pb]);
  assert.equal(rb.applied, true);
  assert.equal(ra.applied, false, 'stale applying query is not applied');
  assert.equal((await c.vis()).vis, null, 'worker keeps the newest (empty) filter state');
  // a later applying query still applies
  const rc = await c.query({ranges: {emag: [0, 0.05]}}, {apply: true});
  assert.equal(rc.applied, true);
  assert.deepEqual(Array.from((await c.vis()).vis), [1, 0, 0, 0, 0]);
  c.terminate();
});

test('engine: projection tokens are kept per pane', async () => {
  const {c} = await openMini();
  const pos = {terms: [{w: [1], keys: ['mag']}, {w: [1], keys: ['mag']}]};
  const neg = {terms: [{w: [-1], keys: ['mag']}, {w: [-1], keys: ['mag']}]};
  await c.project(pos, {token: 1, pane: 'A'});
  await c.project(neg, {token: 1, pane: 'B'});
  const ba = await c.brush({pane: 'A', kind: 'proj', token: 1}, {kind: 'rect', x0: 9.5, x1: 12.5, y0: 9.5, y1: 12.5});
  assert.equal(ba.count, 3, 'pane A brushes its own (positive) projection');
  const bb = await c.brush({pane: 'B', kind: 'proj', token: 1}, {kind: 'rect', x0: -12.5, x1: -9.5, y0: -12.5, y1: -9.5});
  assert.equal(bb.count, 3);
  await assert.rejects(c.brush({pane: 'C', kind: 'proj', token: 1}, {kind: 'rect', x0: 0, x1: 1, y0: 0, y1: 1}), /unknown projection token/);
  // a projection stored without a pane answers any pane
  await c.project(pos, {token: 5});
  assert.equal((await c.brush({pane: 'Z', kind: 'proj', token: 5}, {kind: 'rect', x0: 9.5, x1: 12.5, y0: 9.5, y1: 12.5})).count, 3);
  c.terminate();
});

const QB = 'https://q.test/cat/';
function precisionBundle() {
  const man = {
    schema: 'pk-catalog/1', id: 'q', n: 5, order: {kind: 'input'}, chunk_rows: 3,
    ids: {file: 'ids.bin', enc: 'str'},
    cols: [
      {key: 'mjd', role: 'time', unit: 'd', enc: 'u16q', lo: 59000, hi: 59065.534, step: 0.001, shuffle: false,
        chunk_files: ['cols/mjd.0.bin', 'cols/mjd.1.bin'], validity: 'ok'},
      {key: 'ok', role: 'flag', enc: 'bits', chunk_files: ['cols/ok.0.bin', 'cols/ok.1.bin']},
    ],
  };
  const files = new Map([
    ['manifest.json', JSON.stringify(man)],
    ['ids.bin', gz(strPayload(['r0', 'r1', 'r2', 'r3', 'r4']))],
    ['cols/mjd.0.bin', gz(u16b([4, 8, 1235]))],
    ['cols/mjd.1.bin', gz(u16b([2, 3]))],
    ['cols/ok.0.bin', gz(new Uint8Array([0b111]))],
    ['cols/ok.1.bin', gz(new Uint8Array([0b10]))], // row 3 invalid, row 4 valid
  ]);
  return new Map(Array.from(files, ([k, v]) => [QB + k, v]));
}

test('engine: quantized rows load only the containing chunks; values dequantized in float64', async () => {
  const f = fakeFetch(precisionBundle());
  const c = await DataClient.open(QB + 'manifest.json', {worker: false, fetch: f});
  const r = await c.rows(3, 5);
  assert.deepEqual(r.map((o) => o.mjd), [null, 59000.002]);
  assert.deepEqual(r.map((o) => o.ok), [0, 1]);
  assert.ok(!f.calls.some((u) => u.endsWith('mjd.0.bin') || u.endsWith('ok.0.bin')), `only chunk 1 fetched: ${f.calls}`);
  const r0 = await c.rows(0, 3);
  assert.deepEqual(r0.map((o) => o.mjd), [59000.003, 59000.007, 59001.234]);
  const rec = await c.record(1);
  assert.equal(rec.mjd, 59000.007);
  const csv = await (await c.exportCSV()).text();
  const t = parseCSV(csv);
  assert.deepEqual(t[0], ['id', 'mjd', 'ok']);
  assert.deepEqual(t.slice(1).map((row) => row[1]), ['59000.003', '59000.007', '59001.234', '', '59000.002']);
  const js = JSON.parse(await (await c.export({format: 'json'})).text());
  assert.equal(js[2].mjd, 59001.234);
  c.terminate();
});
