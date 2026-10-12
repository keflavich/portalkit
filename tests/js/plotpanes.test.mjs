// Plot-pane helpers: hist ranges, manifold frames / presets, scatter extents and view stats,
// legend colours, the shared GPU budget, narrative highlights, the unified model controls
// (ui/controls.js) and the Aladin loader. The first group comes from the WP08 unit checks.
import {test} from 'node:test';
import assert from 'node:assert/strict';

import * as hist from '../../web/js/panes/hist.js';
import * as man from '../../web/js/panes/manifold.js';
import * as sc from '../../web/js/panes/scatter.js';
import * as leg from '../../web/js/ui/legend.js';
import * as ctl from '../../web/js/ui/controls.js';
import * as feat from '../../web/js/spec/features.js';
import * as sed from '../../web/js/panes/sed.js';
import * as chips from '../../web/js/ui/chips.js';
import * as aladin from '../../web/js/sky/aladin.js';
import * as reg from '../../web/js/core/registry.js';
import {createStore} from '../../web/js/core/store.js';
import {Budget} from '../../web/js/gl/budget.js';
import {makeView} from '../../web/js/plot/axes.js';
import * as skyp from '../../web/js/panes/sky.js';
import * as narr from '../../web/js/panes/narrative.js';
import {stateToHash, BEAT_VIEW, BEAT_OVL} from '../../web/js/core/hash.js';

const tick = () => new Promise((r) => setTimeout(r, 5));

// ------------------------------------------------------------------ WP08 checks

test('hist.withRange sets, clears and does not mutate', () => {
  const f = hist.withRange({cats: {a: ['x']}}, 'f410m', [1, 2]);
  assert.deepEqual(f.ranges, {f410m: [1, 2]});
  assert.deepEqual(f.cats, {a: ['x']});
  const g = hist.withRange({ranges: {f410m: [1, 2], b: [0, 1]}}, 'f410m', null);
  assert.deepEqual(g.ranges, {b: [0, 1]});
  const a = {ranges: {x: [0, 1]}};
  hist.withRange(a, 'y', [1, 2]);
  assert.deepEqual(a, {ranges: {x: [0, 1]}});
});

test('hist.withRange open bounds, swapped bounds, NaN and full clear', () => {
  assert.deepEqual(hist.withRange({}, 'k', [18, null]).ranges, {k: [18, null]});
  assert.deepEqual(hist.withRange({}, 'k', [null, 18]).ranges, {k: [null, 18]});
  assert.deepEqual(hist.withRange({}, 'c', [-3, null]).ranges, {c: [-3, null]});
  assert.deepEqual(hist.withRange({}, 'c', [5, -2]).ranges, {c: [-2, 5]});
  assert.equal(hist.withRange({ranges: {c: [0, 1]}}, 'c', [null, null]).ranges, undefined);
  assert.deepEqual(hist.withRange({}, 'c', [NaN, 4]).ranges, {c: [null, 4]});
});

test('manifold.robustStats: centre and MAD scale', () => {
  const v = new Float32Array(1001).map((_, i) => i);
  const s = man.robustStats(v);
  assert.ok(Math.abs(s.center - 500) < 2, JSON.stringify(s));
  assert.ok(s.scale > 0);
  const m = man.robustStats(new Float64Array([1, 2, 3, 4, 100]));   // |dev| = [2,1,0,1,97] -> MAD 1
  assert.equal(m.center, 3);
  assert.ok(Math.abs(m.scale - 1.4826) < 1e-9, String(m.scale));
});

test('manifold.pcaFrame is orthonormal', () => {
  const n = 500, D = 4;
  const cols = [];
  for (let d = 0; d < D; d++) cols.push(new Float32Array(n));
  for (let i = 0; i < n; i++) {
    const a = Math.sin(i * 1.3), b = Math.cos(i * 0.7);
    cols[0][i] = a; cols[1][i] = a * 2 + b * 0.1; cols[2][i] = b; cols[3][i] = 0.01 * Math.sin(i);
  }
  const dims = cols.map((_, k) => ({key: 'd' + k, center: 0, scale: 1}));
  const F = man.pcaFrame(cols, dims);
  assert.equal(F.length, 2 * D);
  let xx = 0, yy = 0, xy = 0;
  for (let d = 0; d < D; d++) { xx += F[d] ** 2; yy += F[D + d] ** 2; xy += F[d] * F[D + d]; }
  assert.ok(Math.abs(xx - 1) < 1e-6 && Math.abs(yy - 1) < 1e-6 && Math.abs(xy) < 1e-6, [xx, yy, xy].join());
});

test('manifold.remapFrame moves weights to the target dims', () => {
  const F = man.remapFrame([1, 0, 0, 1], ['a', 'b'], ['b', 'c', 'a']);
  assert.equal(F.length, 6);
  assert.equal(F[2], 1);
  assert.equal(F[3], 1);
});

test('manifold.pairOfFrame', () => {
  assert.deepEqual(man.pairOfFrame([1, 0, 0, 0, 0, -1], 3), {i: 0, si: 1, j: 2, sj: -1});
  assert.equal(man.pairOfFrame([0.7, 0.7, 0, 0, 0, 1], 3), null);
  assert.equal(man.pairOfFrame([1, 0, 0, 1, 0, 0], 3), null);
});

test('manifold caps match PROVIDERS', () => {
  for (const c of ['selectable_axes', 'av_slider', 'extinction_vectors_on_ccd', 'isochrones_or_tracks']) {
    assert.ok(man.meta.caps.includes(c), c);
  }
});

test('scatter.drawableMask', () => {
  const m = sc.drawableMask(new Float32Array([1, NaN, 2, 3]), new Float32Array([1, 1, Infinity, 3]), null, 4);
  assert.deepEqual(Array.from(m), [1, 0, 0, 1]);
});

test('scatter.shiftShape moves a rect by (dx, dy)', () => {
  const s = sc.shiftShape({kind: 'rect', x0: 0, x1: 1, y0: 2, y1: 3}, 1, 2);
  assert.equal(s.kind, 'rect');
  assert.deepEqual([s.x0, s.x1, s.y0, s.y1], [1, 2, 4, 5]);
});

test('scatter.sampledExtent: close to the full quantiles, mask honoured, small n exact', () => {
  const n = 400000;
  const v = new Float32Array(n);
  for (let i = 0; i < n; i++) v[i] = ((i * 7919) % n) / n;
  const t0 = Date.now();
  const e = sc.sampledExtent(v, {q: [0.01, 0.99], pad: 0});
  const dt = Date.now() - t0;
  assert.ok(Math.abs(e[0] - 0.01) < 0.005 && Math.abs(e[1] - 0.99) < 0.005, e.join());
  assert.ok(dt < 1500, `${dt} ms (loose bound: shared 2-CPU test machines)`);
  const m2 = 200000;
  const w = new Float32Array(m2).map((_, i) => (i % 2 ? 100 : i / m2));
  const mask = new Uint8Array(m2).map((_, i) => (i % 2 ? 0 : 1));
  assert.ok(sc.sampledExtent(w, {q: [0, 1], mask, pad: 0})[1] < 1.01);
  assert.deepEqual(sc.sampledExtent(new Float32Array([1, 2, 3]), {pad: 0}), [1, 3]);
});

test('scatter.statGridImage keeps NaN and stretches values', () => {
  const g = sc.statGridImage({values: new Float32Array([NaN, -5, 0, 5]), nx: 2, ny: 2, xr: [0, 1], yr: [0, 1]});
  assert.ok(Number.isNaN(g.counts[0]));
  assert.ok(g.counts[1] > 0 && g.counts[1] < 0.01);
  assert.ok(g.counts[3] > 0.99);
});

test('scatter.contextGridEntry: count grid for the linear axes, never a stat grid', () => {
  const cnt = {key: 'cmd', x: 'f115w-f200w', y: 'f200w', nx: 4, ny: 4};
  const stat = {key: 'med', x: 'f115w-f200w', y: 'f200w', stat: 'median:f444w'};
  const man = {density: [stat, {x: 'f115w-f200w', y: 'f200w'}, cnt]};
  assert.equal(sc.contextGridEntry(man, {x: 'f115w-f200w', y: 'f200w'}), cnt);
  assert.equal(sc.contextGridEntry(man, {x: 'f200w', y: 'f115w-f200w'}), null, 'swapped axes do not match');
  assert.equal(sc.contextGridEntry(man, {x: 'f115w-f200w', y: 'f200w', ylog: true}), null, 'log axis');
  assert.equal(sc.contextGridEntry(man, {x: 'f115w-f200w', y: 'f200w', xlog: true}), null);
  assert.equal(sc.contextGridEntry({density: [stat]}, {x: 'f115w-f200w', y: 'f200w'}), null);
  assert.equal(sc.contextGridEntry({}, {x: 'a', y: 'b'}), null);
  assert.equal(sc.contextGridEntry({density: 'x'}, {x: 'a', y: 'b'}), null);
  assert.equal(sc.contextGridEntry(null, {x: 'a', y: 'b'}), null);
  assert.equal(sc.contextGridEntry(man, {x: '', y: 'f200w'}), null);
  assert.equal(sc.contextGridEntry(man, null), null);
});

test('scatter.contextGridData: same counts array, fold shift on the ranges, malformed grids null', () => {
  const values = new Float32Array(6);
  const g = {values, nx: 3, ny: 2, xr: [0, 3], yr: [10, 12], total: 9};
  const d = sc.contextGridData(g);
  assert.equal(d.counts, values, 'the decoded array itself (canvas2d cache key)');
  assert.deepEqual([d.nx, d.ny, d.xr, d.yr, d.total], [3, 2, [0, 3], [10, 12], 9]);
  const s = sc.contextGridData(g, [0.5, -2]);
  assert.equal(s.counts, values);
  assert.deepEqual([s.xr, s.yr], [[0.5, 3.5], [8, 10]]);
  assert.deepEqual(g.xr, [0, 3], 'input not mutated');
  assert.deepEqual(sc.contextGridData(g, [NaN, Infinity]).xr, [0, 3], 'non-finite shift ignored');
  assert.equal(sc.contextGridData(null), null);
  assert.equal(sc.contextGridData({...g, values: null}), null);
  assert.equal(sc.contextGridData({...g, nx: 0}), null);
  assert.equal(sc.contextGridData({...g, yr: 'x'}), null);
});

test('legend colours: GPU slots, plot levels and codes', () => {
  const c = leg.gpuCatColors({levels: ['a', 'b']}, {neutral: '#808080', categorical: ['#ff0000', '#00ff00']});
  assert.equal(c.length, 8);
  const lv = Array.from({length: 9}, (_, i) => 'l' + i);
  const p = leg.plotLevelColors({levels: lv}, {neutral: '#4b5563'});
  assert.equal(p[7], '#4b5563');
  assert.equal(p[8], '#4b5563');
  assert.notEqual(p[6], '#4b5563');
  assert.deepEqual(Array.from(leg.gpuCatCodes([0, 6, 7, 8], 9)), [1, 7, 0, 0]);
  for (const k of ['isCategory', 'levelColors', 'gpuCatColors', 'gpuCatCodes']) assert.equal(typeof leg[k], 'function', k);
});

test('legend.resolveColor: option, explicit none, store wins', () => {
  const fakeClient = {
    col: (k) => ({cls: {key: 'cls', role: 'category', enc: 'cat8', levels: ['a', 'b']}, v: {key: 'v', role: 'value'}})[k] || null,
    baseKeys: (e) => [e],
  };
  const st = (v) => ({get: (k) => (k === 'color' ? v : null)});
  assert.equal(leg.resolveColor(st({by: null, cmap: 'viridis'}), {color: 'cls'}, fakeClient).by, 'cls');
  const none = leg.resolveColor(st({by: '', cmap: 'viridis'}), {color: 'cls'}, fakeClient);
  assert.equal(none.kind, 'none');
  assert.equal(none.by, null);
  assert.equal(leg.resolveColor(st({by: 'v'}), {color: 'cls'}, fakeClient).kind, 'cont');
});

// ------------------------------------------------------------------ GPU budget (note 68)

test('gpuBudget reads app.gpuBudget and never creates one', () => {
  assert.equal(sc.gpuBudget(null), null);
  const app = {};
  assert.equal(sc.gpuBudget(app), null);
  assert.equal('gpuBudget' in app, false);
  const B = new Budget();
  assert.equal(sc.gpuBudget({gpuBudget: B}), B);
});

test('planGpu / requestGpu / releaseGpu without a page budget', () => {
  const app = {};
  const p = sc.planGpu(app, 'sc1', {cssW: 400, cssH: 300, density: true, vertexBytes: 1e6}, 3);
  assert.deepEqual(p, {dpr: 2, refused: false, plan: null});
  assert.equal(sc.planGpu(null, 'x', {cssW: 1, cssH: 1}, 0.5).dpr, 1);
  assert.equal(sc.requestGpu(app, 'sc1:gc', 1e9), true);
  sc.releaseGpu(app, 'sc1');
  sc.releaseGpu(null, 'sc1');
  assert.equal('gpuBudget' in app, false);
});

test('planGpu with the page budget registers panes and plans', () => {
  const app = {gpuBudget: new Budget()};
  const p = sc.planGpu(app, 'sc1', {cssW: 400, cssH: 300, density: false, vertexBytes: 1000}, 1);
  assert.ok(p.plan);
  assert.equal(p.refused, false);
  assert.ok(p.dpr >= 1 && p.dpr <= 2);
  assert.ok(app.gpuPanes.has('sc1'));
  assert.equal(sc.requestGpu(app, 'm1:gc', 1024), true);
  sc.releaseGpu(app, 'sc1');
  sc.releaseGpu(app, 'm1:gc');
  assert.equal(app.gpuPanes.has('sc1'), false);
});

// ------------------------------------------------------------------ manifold axes presets (note 91)

test('framePresets turns axes presets into unit-weight dim-pair frames', () => {
  const client = {manifest: {
    manifold: {dims: ['a', 'b', 'c'], frames: {pca: [1, 0, 0, 0, 1, 0]}},
    presets: [
      {kind: 'axes', key: 'cmd', label: 'CMD', x: 'c', y: 'a'},
      {kind: 'axes', key: 'other', x: 'a', y: 'zzz'},
      {kind: 'frame', key: 'fr', dims: ['a', 'b'], F: [1, 0, 0, 1]},
      {kind: 'axes', key: 'fr', x: 'a', y: 'b'},
      {kind: 'selection', key: 'sel'},
    ],
  }};
  const all = man.framePresets(client);
  assert.deepEqual(all.map((p) => p.key), ['pca', 'cmd', 'other', 'fr']);
  const cmd = all.find((p) => p.key === 'cmd');
  assert.deepEqual(cmd, {key: 'cmd', label: 'CMD', dims: ['c', 'a'], F: [1, 0, 0, 1], kind: 'axes'});
  const dimKeys = ['b', 'c', 'a'];
  const some = man.framePresets(client, dimKeys);
  assert.deepEqual(some.map((p) => p.key), ['pca', 'cmd', 'fr']);
  // remapped onto the pane's dims: row 0 = +1 on c (index 1), row 1 = +1 on a (index 2)
  const F = man.remapFrame(cmd.F, cmd.dims, dimKeys);
  assert.deepEqual(man.pairOfFrame(F, 3), {i: 1, si: 1, j: 2, sj: 1});
});

// ------------------------------------------------------------------ narrative highlight (note 71)

test('highlightApplies: pane id / type / none and dataset rules', () => {
  const pane = {id: 'sc1', type: 'scatter', ds: 'cat'};
  assert.equal(sc.highlightApplies(null, pane), false);
  assert.equal(sc.highlightApplies({ids: ['1']}, pane), true);
  assert.equal(sc.highlightApplies({pane: 'sc1', ids: ['1']}, pane), true);
  assert.equal(sc.highlightApplies({pane: 'scatter', ids: ['1']}, pane), true);
  assert.equal(sc.highlightApplies({pane: 'sky', ids: ['1']}, pane), false);
  assert.equal(sc.highlightApplies({pane: 'scatter', ds: 'other', ids: ['1']}, pane), false);
  assert.equal(sc.highlightApplies({pane: 'sc1', ds: 'other', ids: ['1']}, pane), true);
  assert.equal(sc.highlightApplies({ds: 'cat', ids: []}, pane), true);
});

test('recentreDomain keeps span and orientation (linear and log)', () => {
  assert.deepEqual(sc.recentreDomain([0, 10], 20), [15, 25]);
  assert.deepEqual(sc.recentreDomain([10, 0], 20), [25, 15]);
  const L = sc.recentreDomain([1, 100], 1000, true);
  assert.ok(Math.abs(L[0] - 100) < 1e-9 && Math.abs(L[1] - 10000) < 1e-6, L.join());
  assert.deepEqual(sc.recentreDomain([1, 100], -1, true), [1, 100]);
  assert.deepEqual(sc.recentreDomain([0, 1], NaN), [0, 1]);
});

test('highlightCentre: mean of finite rows and count outside the domain', () => {
  const x = new Float32Array([0, 2, 4, NaN, 100]);
  const y = new Float32Array([0, 2, 4, 1, 100]);
  const c = sc.highlightCentre([0, 1, 2, 3], x, y, {dom: {x: [0, 3], y: [3, 0]}});
  assert.deepEqual(c, {x: 2, y: 2, n: 3, outside: 1});
  const l = sc.highlightCentre([0, 1, 2], new Float32Array([1, 10, 100]), new Float32Array([1, 1, 1]), {xlog: true});
  assert.ok(Math.abs(l.x - 10) < 1e-6);
  assert.equal(sc.highlightCentre([3], x, y), null);
});

test('narrativeHighlight resolves ids, filters panes and emits sync', async () => {
  const store = createStore({});
  const synced = [];
  store.onEvent('narrative:sync', (p) => synced.push(p));
  const app = {resolveIds: async (ds, ids) => ids.map((s) => (s === 'missing' ? -1 : Number(s)))};
  const seen = [];
  const h = sc.narrativeHighlight({store, app, id: 'm1'}, 'cat', 'manifold', (rows, p) => seen.push([Array.from(rows), p && p.beat]));
  h.sync();
  assert.deepEqual(synced, [{pane: 'm1', type: 'manifold'}]);
  store.emit('narrative:highlight', {pane: 'sky1', ds: 'cat', ids: ['1'], beat: 'b0'});
  store.emit('narrative:highlight', {pane: 'manifold', ds: 'cat', ids: ['3', 'missing', '5'], beat: 'b1'});
  await tick();
  assert.deepEqual(seen, [[[3, 5], 'b1']]);
  assert.deepEqual(Array.from(h.rows()), [3, 5]);
  store.emit('narrative:highlight', {pane: 'm1', ds: 'cat', ids: [], beat: 'b1'});
  assert.deepEqual(Array.from(h.rows()), []);
  h.destroy();
  store.emit('narrative:highlight', {ids: ['7']});
  await tick();
  assert.equal(seen.length, 2);
});

test('narrativeHighlight clears when the next beat targets another pane', async () => {
  const store = createStore({});
  const app = {resolveIds: async (ds, ids) => ids.map(Number)};
  const seen = [];
  const h = sc.narrativeHighlight({store, app, id: 'sc1'}, 'src', 'scatter', (rows, p) => seen.push([Array.from(rows), p && p.beat]));
  store.emit('narrative:highlight', {pane: 'sc1', ds: 'src', ids: ['3', '4'], beat: 'b1', goto: true});
  await tick();
  assert.deepEqual(Array.from(h.rows()), [3, 4]);
  // another pane's payload of the same beat leaves the rings
  store.emit('narrative:highlight', {pane: 'sky1', ds: 'src', ids: ['5'], beat: 'b1'});
  assert.deepEqual(Array.from(h.rows()), [3, 4]);
  // beat b2 targets sky1 (no highlight): the scatter rings go
  store.emit('narrative:highlight', {pane: 'sky1', ds: null, ids: [], beat: 'b2'});
  assert.deepEqual(Array.from(h.rows()), []);
  assert.equal(h.payload(), null);
  assert.deepEqual(seen, [[[3, 4], 'b1'], [[], null]]);
  h.destroy();
});

test('narrativeHighlight delivers goto once per beat', async () => {
  const store = createStore({});
  const app = {resolveIds: async (ds, ids) => ids.map(Number)};
  const gotos = [];
  sc.narrativeHighlight({store, app, id: 'sc1'}, 'src', 'scatter', (rows, p) => { if (p && p.goto) gotos.push(p.beat); });
  const b1 = {pane: 'sc1', ds: 'src', ids: ['3'], beat: 'b1', goto: true};
  store.emit('narrative:highlight', b1);
  await tick();
  store.emit('narrative:highlight', b1);     // narrative:sync replay
  await tick();
  assert.deepEqual(gotos, ['b1']);
  store.emit('narrative:highlight', {pane: 'sky1', ids: [], beat: 'b2'});
  store.emit('narrative:highlight', b1);     // back to beat b1: goto again
  await tick();
  assert.deepEqual(gotos, ['b1', 'b1']);
});

test('narrativeHighlight drops a stale resolution', async () => {
  const store = createStore({});
  let release = null;
  const app = {resolveIds: (ds, ids) => (ids[0] === 'slow' ? new Promise((r) => { release = () => r([9]); }) : Promise.resolve([1]))};
  const seen = [];
  sc.narrativeHighlight({store, app, id: 's'}, 'cat', 'scatter', (rows) => seen.push(Array.from(rows)));
  store.emit('narrative:highlight', {ids: ['slow']});
  store.emit('narrative:highlight', {ids: ['fast']});
  await tick();
  release();
  await tick();
  assert.deepEqual(seen, [[1]]);
});

// ------------------------------------------------------------------ scatter view stats

test('viewStats reports domains, plot rectangle, margins and px per unit', () => {
  const view = makeView({width: 400, height: 300, dpr: 1,
    x: {kind: 'lin', d0: 0, d1: 10}, y: {kind: 'lin', d0: 5, d1: -5, invert: false}});
  const v = sc.viewStats(view, {x: 'a-b', y: 'c', xlog: false, ylog: false}, {shift: [0, 1.5], yinv: true});
  assert.equal(v.x, 'a-b');
  assert.deepEqual(v.xr, [0, 10]);
  assert.equal(v.width, 400);
  assert.equal(v.plot.left + v.plot.width, v.plot.right);
  assert.equal(v.margin.left, v.plot.left);
  assert.equal(v.margin.right, 400 - v.plot.right);
  assert.ok(Math.abs(v.kx - v.plot.width / 10) < 1e-9);
  assert.deepEqual(v.shift, [0, 1.5]);
  assert.equal(v.yinv, true);
  // data -> px with the reported geometry matches the view
  const px = v.plot.left + (4 - v.xr[0]) * v.kx;
  assert.ok(Math.abs(px - view.toPx(4, 0)[0]) < 1e-9);
  assert.equal(sc.viewStats(null), null);
});

// ------------------------------------------------------------------ model controls (note 75)

test('one modelControls / featureToggles implementation', () => {
  assert.equal(sed.modelControls, ctl.modelControls);
  assert.equal(typeof feat.featureToggles, 'function');
  assert.equal(typeof ctl.featureToggles, 'function');
  assert.equal('applyDefaultOff' in chips, false, 'chips render default_off state only (note 70)');
});

test('controls.rafSetter: frame looked up at call time, one coalesced write per frame', async () => {
  // the module was imported above, before this stand-in exists: a binding taken at import would
  // ignore it and run on the 16 ms timer (IMPLEMENTATION_NOTES 123)
  const prev = globalThis.requestAnimationFrame;
  const frames = [];
  globalThis.requestAnimationFrame = (fn) => { frames.push(fn); return frames.length; };
  try {
    const writes = [];
    const store = {set: (p, o) => writes.push([p, o])};
    const set = ctl.rafSetter(store, 'ctl1');
    set({av: 1});
    set({rv: 3.1});
    set({av: 2});
    assert.equal(frames.length, 1, 'one frame requested for the burst');
    await new Promise((r) => setTimeout(r, 40));
    assert.equal(writes.length, 0, 'nothing written before the frame (no timer fallback)');
    frames.shift()(0);
    assert.deepEqual(writes, [[{av: 2, rv: 3.1}, {source: 'ctl1'}]]);
    set({av: 5});
    assert.equal(frames.length, 1, 'a new burst requests a new frame');
    frames.shift()(16);
    assert.deepEqual(writes.at(-1), [{av: 5}, {source: 'ctl1'}]);
    assert.equal(writes.length, 2);
    // without requestAnimationFrame the setter falls back to a timer
    delete globalThis.requestAnimationFrame;
    set({dm: 1});
    set({dm: 2});
    assert.equal(writes.length, 2);
    await new Promise((r) => setTimeout(r, 40));
    assert.deepEqual(writes.at(-1), [{dm: 2}, {source: 'ctl1'}]);
    assert.equal(writes.length, 3);
  } finally {
    if (prev === undefined) delete globalThis.requestAnimationFrame;
    else globalThis.requestAnimationFrame = prev;
  }
});

test('controls pure helpers: R_V choices, A_V cleaning, number text', () => {
  assert.deepEqual(ctl.rvChoices(3.1), [3.1, 5.5]);
  assert.deepEqual(ctl.rvChoices(4), [3.1, 4, 5.5]);
  assert.deepEqual(ctl.rvChoices('x'), [3.1, 5.5]);
  assert.equal(ctl.cleanAv(-1), null);
  assert.equal(ctl.cleanAv('abc'), null);
  assert.equal(ctl.cleanAv(2.34567), 2.346);
  assert.equal(ctl.numText(2.5, 3), '2.5');
  assert.equal(ctl.numText(3, 2), '3');
  assert.equal(ctl.numText(0.05, 2), '0.05');
});

test('nextTemplate / parseBands / toggledFeatures', () => {
  assert.equal(ctl.nextTemplate(null, {grid: 'g'}), null);
  assert.equal(ctl.nextTemplate({key: 'a'}, {key: ''}), null);
  assert.deepEqual(ctl.nextTemplate(null, {key: 'k1'}), {key: 'k1', scale: 'auto'});
  assert.deepEqual(ctl.nextTemplate({key: 'k1', scale: 2, bands: ['F200W']}, {grid: 'prism'}),
    {key: 'k1', grid: 'prism', scale: 2, bands: ['F200W']});
  assert.deepEqual(ctl.nextTemplate({key: 'k1', grid: 'prism', scale: 2}, {grid: undefined, scale: 'auto'}), {key: 'k1', scale: 'auto'});
  assert.deepEqual(ctl.nextTemplate({key: 'k1'}, {scale: -3}), {key: 'k1', scale: 'auto'});
  assert.deepEqual(ctl.nextTemplate({key: 'k1', bands: ['a']}, {bands: undefined}), {key: 'k1', scale: 'auto'});
  assert.deepEqual(ctl.parseBands(' F200W, F444W  F150W'), ['F200W', 'F444W', 'F150W']);
  assert.equal(ctl.parseBands('  '), undefined);
  assert.deepEqual(ctl.toggledFeatures(['h2', 'co', 'ice'], new Set(['ice', 'xx', 'h2'])), ['h2', 'ice', 'xx']);
});

test('templateSource adapts a pk-templates/1 document or passes a TemplateSet', async () => {
  const hub = {json: async (ds) => (ds === 'tpl' ? {templates: [{key: 'k0', label: 'K0 III'}, {key: 'k1'}, {}], grids: {prism: {}, g395h: {}}} : null)};
  const s = await ctl.templateSource({hub, ds: 'tpl'});
  assert.deepEqual(s.list(), [{key: 'k0', label: 'K0 III'}, {key: 'k1', label: 'k1'}]);
  assert.deepEqual(s.gridNames(), ['prism', 'g395h']);
  const ts = {list: () => [], gridNames: () => []};
  assert.equal(await ctl.templateSource({templates: Promise.resolve(ts)}), ts);
  assert.equal(await ctl.templateSource({}), null);
  assert.deepEqual(ctl.templateDocSource(null).list(), []);
});

test('familyColor: own colour, else the fallback cycle', () => {
  assert.equal(feat.familyColor({color: '#123456'}, 3), '#123456');
  assert.equal(feat.familyColor({color: null}, 0), feat.familyColor({}, 10));
  assert.match(feat.familyColor(null, -1), /^#[0-9a-f]{6}$/);
});

// ---- minimal DOM for the widgets (no jsdom dependency)

class FakeEl {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.attrs = {};
    this.dataset = {};
    this.listeners = {};
    this.className = '';
    this.hidden = false;
    this.disabled = false;
    this.value = '';
    this._text = '';
    this.innerHTML = '';
    this.parent = null;
    this.style = {props: {}, setProperty(k, v) { this.props[k] = v; }};
    const self = this;
    this.classList = {
      toggle(c, on) {
        const s = new Set(self.className.split(/\s+/).filter(Boolean));
        if (on === undefined ? !s.has(c) : on) s.add(c); else s.delete(c);
        self.className = Array.from(s).join(' ');
      },
      contains(c) { return self.className.split(/\s+/).includes(c); },
    };
  }
  set textContent(t) { this.children = []; this._text = String(t); }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; }
  append(...cs) { for (const c of cs) { if (c && typeof c === 'object') { c.parent = this; this.children.push(c); } } }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  fire(t) { for (const fn of this.listeners[t] || []) fn({type: t}); }
  all() { return this.children.flatMap((c) => [c, ...c.all()]); }
  querySelectorAll(sel) {
    const m = /^\.([\w-]+)$/.exec(sel);
    return m ? this.all().filter((e) => e.classList.contains(m[1])) : [];
  }
  find(fn) { return this.all().find(fn) || null; }
}

function withDom(fn) {
  const prev = globalThis.document;
  globalThis.document = {createElement: (t) => new FakeEl(t), activeElement: null, createTextNode: (t) => ({textContent: t})};
  return Promise.resolve().then(fn).finally(() => { globalThis.document = prev; });
}

const FEAT_DOC = {families: {h2: [{w_um: 2.12}], co: [{w_um: 4.67}, {w_um: 4.7}]}, meta: {families: {co: {label: 'CO', color: '#ff0000'}}}};

test('featureToggles: .pk-feat-chip per family, document order, aria-pressed in place', () => withDom(async () => {
  const store = createStore({features: []});
  let doc = null;
  const ft = feat.featureToggles(store, () => doc, {source: 'sp1'});
  assert.equal(ft.el.hidden, true);
  doc = FEAT_DOC;
  ft.update();
  const chipsEls = ft.el.querySelectorAll('.pk-feat-chip');
  assert.deepEqual(chipsEls.map((b) => b.dataset.family), ['h2', 'co']);
  assert.equal(chipsEls[1].textContent, 'CO');
  assert.equal(chipsEls[1].style.props['--pk-feat-color'], '#ff0000');
  assert.equal(ft.el.hidden, false);
  chipsEls[1].fire('click');
  chipsEls[0].fire('click');
  await tick();
  assert.deepEqual(store.get('features'), ['h2', 'co']);
  const again = ft.el.querySelectorAll('.pk-feat-chip');
  assert.equal(again[0], chipsEls[0], 'chips are reused when the families are unchanged');
  assert.equal(again[0].getAttribute('aria-pressed'), 'true');
  ft.destroy();
}));

test('featureToggles from hub + ds (createControls form)', () => withDom(async () => {
  const store = createStore({features: ['co']});
  const ft = ctl.featureToggles({store, pane: 'sc1', hub: {json: async () => FEAT_DOC}, ds: 'feat', heading: 'Features'});
  await tick();
  const b = ft.el.querySelectorAll('.pk-feat-chip');
  assert.equal(b.length, 2);
  assert.equal(b[1].getAttribute('aria-pressed'), 'true');
  ft.destroy();
}));

test('modelControls: shared accessible names and store writes', () => withDom(async () => {
  const store = createStore({av: 2, rv: 3.1, template: null});
  const ts = {list: () => [{key: 'k0', label: 'K0 III'}], gridNames: () => ['prism']};
  const mc = ctl.modelControls({store, id: 'sed1'}, {templates: Promise.resolve(ts)});
  await tick();
  const byAria = (name) => mc.el.find((e) => e.getAttribute('aria-label') === name);
  const av = byAria('A_V (mag)');
  assert.ok(av, 'A_V range');
  assert.equal(av.attrs.type === undefined ? av.type : av.attrs.type, 'range');
  const tpl = byAria('Model template');
  assert.ok(tpl, 'template select');
  assert.equal(tpl.disabled, false);
  assert.deepEqual(tpl.children.map((o) => o.value), ['', 'k0']);
  assert.ok(byAria('R_V'));
  tpl.value = 'k0';
  tpl.fire('change');
  await tick();
  assert.deepEqual(store.get('template'), {key: 'k0', scale: 'auto'});
  const grid = byAria('Template wavelength grid');
  grid.value = 'prism';
  grid.fire('change');
  await tick();
  assert.deepEqual(store.get('template'), {key: 'k0', grid: 'prism', scale: 'auto'});
  av.value = '5.5';
  av.fire('input');
  await new Promise((r) => setTimeout(r, 40));
  assert.equal(store.get('av'), 5.5);
  mc.destroy();
}));

test('extinction number boxes clamp below the slider minimum and show the stored value', () => withDom(async () => {
  const store = createStore({av: 2, dm: 1});
  const x = ctl.extinctionControls({store, pane: 'sc1', av: true, rv: true, dm: true});
  const byAria = (name) => x.el.find((e) => e.getAttribute('aria-label') === name);
  const av = byAria('A_V value'), dm = byAria('DM value');
  av.value = '-3';
  av.fire('change');
  dm.value = '-4';
  dm.fire('change');
  await new Promise((r) => setTimeout(r, 40));
  assert.equal(av.value, '0');
  assert.equal(dm.value, '0');
  assert.equal(store.get('av'), 0);
  assert.equal(store.get('dm'), 0);
  av.value = '';
  av.fire('change');
  assert.equal(av.value, '0', 'an empty box returns to the slider value');
  if (typeof x.destroy === 'function') x.destroy();
}));

test('modelControls hides the template picker when no template set exists', () => withDom(async () => {
  const store = createStore({});
  const mc = ctl.modelControls({store, id: 'sp1'}, {templates: Promise.resolve(null)});
  await tick();
  const grp = mc.el.querySelectorAll('.pk-ctl-template')[0];
  assert.equal(grp.hidden, true);
  mc.destroy();
}));

// ------------------------------------------------------------------ Aladin (note 82)

test('loadAladin loads once per page, a failure included; retry starts over', async () => {
  const page = {aladin: {cdn: 'https://127.0.0.1:9/none.js', mirror: 'https://127.0.0.1:9/none.js'}};
  const p1 = aladin.loadAladin(page, {timeout: 50});
  const p2 = aladin.loadAladin(page, {timeout: 50});
  assert.equal(p1, p2);
  await assert.rejects(p1);
  await tick();
  assert.equal(aladin.libState().state, 'error');
  assert.equal(aladin.loadAladin(page, {timeout: 50}), p1);
  const p3 = aladin.loadAladin(page, {timeout: 50, retry: true});
  assert.notEqual(p3, p1);
  await assert.rejects(p3);
});

// ------------------------------------------------------------------ legend default_off (note 104)

test('toggleLevel keeps cats[key] = [] for a default_off column, drops it otherwise', () => {
  const levels = ['star', 'yso', 'agb'];
  // plain toggles
  assert.deepEqual(leg.toggleLevel({}, 'cls', 'yso', {levels}), {cats: {cls: ['yso']}});
  assert.deepEqual(leg.toggleLevel({cats: {cls: ['yso']}}, 'cls', 'yso', {levels, defaultOff: false}), {});
  // defaultOff left out (a caller that does not know the column, e.g. ui/chips.js): keep the safe []
  assert.deepEqual(leg.toggleLevel({cats: {cls: ['yso']}}, 'cls', 'yso', {levels}), {cats: {cls: []}});
  assert.deepEqual(leg.toggleLevel({cats: {cls: ['yso']}}, 'cls', 'yso'), {cats: {cls: []}});
  assert.deepEqual(leg.toggleLevel({cats: {cls: ['yso']}}, 'cls', 'yso', {levels, defaultOff: ['agb']}), {cats: {cls: []}});
  assert.deepEqual(leg.toggleLevel({cats: {cls: ['agb']}}, 'cls', 'agb', {levels, defaultOff: true}), {cats: {cls: []}});
  // an empty default_off list or false behaves like a plain column
  assert.deepEqual(leg.toggleLevel({cats: {cls: ['agb']}}, 'cls', 'agb', {levels, defaultOff: []}), {});
  assert.deepEqual(leg.toggleLevel({cats: {cls: ['agb']}}, 'cls', 'agb', {levels, defaultOff: false}), {});
  // other filter parts and other columns are kept, input not mutated
  const f0 = {cats: {cls: ['agb'], kind: ['x']}, ranges: {f410m: [1, 2]}};
  const f1 = leg.toggleLevel(f0, 'cls', 'agb', {levels, defaultOff: ['agb']});
  assert.deepEqual(f1, {cats: {cls: [], kind: ['x']}, ranges: {f410m: [1, 2]}});
  assert.deepEqual(f0, {cats: {cls: ['agb'], kind: ['x']}, ranges: {f410m: [1, 2]}});
  // hiding a level from the explicit all-on list
  assert.deepEqual(leg.toggleLevel(f1, 'cls', 'star', {levels, defaultOff: ['agb']}).cats.cls, ['star']);
  // alt-click: only this level, then all again (kept as [] for default_off)
  const only = leg.toggleLevel({cats: {cls: ['agb']}}, 'cls', 'yso', {only: true, levels, defaultOff: ['agb']});
  assert.deepEqual(only.cats.cls, ['star', 'agb']);
  assert.deepEqual(leg.toggleLevel(only, 'cls', 'yso', {only: true, levels, defaultOff: ['agb']}), {cats: {cls: []}});
  assert.deepEqual(leg.toggleLevel(only, 'cls', 'yso', {only: true, levels, defaultOff: false}), {});
  assert.deepEqual(leg.toggleLevel(only, 'cls', 'yso', {only: true, levels}), {cats: {cls: []}});
  // unicode levels; a one-level column
  assert.deepEqual(leg.toggleLevel({cats: {'ψ': ['é']}}, 'ψ', 'é', {levels: ['é'], defaultOff: ['é']}), {cats: {'ψ': []}});
  assert.deepEqual(leg.toggleLevel({}, 'ψ', 'é', {only: true, levels: ['é'], defaultOff: ['é']}), {cats: {'ψ': []}});
  assert.equal(leg.hasDefaultOff({default_off: ['a']}), true);
  for (const m of [null, {}, {default_off: []}, {default_off: 'a'}]) assert.equal(leg.hasDefaultOff(m), false);
});

test('legend chip click on a default_off column writes cats[key] = [] (not a deleted key)', () => withDom(async () => {
  const meta = {key: 'cls', role: 'category', enc: 'cat8', levels: ['star', 'yso', 'agb'], default_off: ['agb']};
  const client = {
    n: 3, col: (k) => (k === 'cls' ? meta : null), baseKeys: (e) => [e],
    hist: async () => ({counts: new Float64Array([1, 1, 1])}),
  };
  const store = createStore({color: {by: 'cls', cmap: 'viridis'}, 'filters:src': {cats: {cls: ['agb']}}, 'vis:src': {}, 'sel:src': {}});
  const host = new FakeEl('div');
  const lg = leg.createLegend(host, {store, client, ds: 'src', pane: 'sc1', config: {}});
  await tick();
  const chipFor = (lv) => lg.el.find((e) => e.classList.contains('pk-legend-level') && e.textContent.startsWith(lv));
  assert.equal(chipFor('agb').getAttribute('aria-pressed'), 'false');
  chipFor('agb').fire('click');
  await tick();
  assert.deepEqual(store.get('filters:src'), {cats: {cls: []}});
  assert.equal(chipFor('agb').getAttribute('aria-pressed'), 'true');
  chipFor('yso').fire('click');
  await tick();
  assert.deepEqual(store.get('filters:src'), {cats: {cls: ['yso']}});
  lg.destroy();
}));

test('filter chip click (ui/chips.js) on a default_off column also writes cats[key] = []', () => withDom(async () => {
  const meta = {key: 'cls', role: 'category', enc: 'cat8', levels: ['star', 'yso', 'agb'], default_off: ['agb']};
  const client = {n: 3, manifest: {cols: [meta]}, hist: async () => ({counts: new Float64Array([1, 1, 1])})};
  const store = createStore({'filters:src': {cats: {cls: ['agb']}}, 'sel:src': {}});
  const host = new FakeEl('div');
  const ch = chips.createChips(host, {store, client, ds: 'src', pane: 'chips1',
    show: {cats: true, flags: false, snr: false, presets: false, saved: false}});
  await tick();
  const chipFor = (lv) => ch.el.find((e) => e.classList.contains('pk-chip') && e.textContent.startsWith(lv));
  chipFor('agb').fire('click');
  await tick();
  assert.deepEqual(store.get('filters:src'), {cats: {cls: []}});
  ch.destroy();
}));

test('filter chip click on a plain column: re-showing every level deletes cats[key] (defaultOff false)', () => withDom(async () => {
  const meta = {key: 'cls', role: 'category', enc: 'cat8', levels: ['star', 'yso', 'agb']};
  const client = {n: 3, manifest: {cols: [meta]}, hist: async () => ({counts: new Float64Array([1, 1, 1])})};
  const store = createStore({'filters:src': {cats: {cls: ['agb']}}, 'sel:src': {}});
  const host = new FakeEl('div');
  const ch = chips.createChips(host, {store, client, ds: 'src', pane: 'chips1',
    show: {cats: true, flags: false, snr: false, presets: false, saved: false}});
  await tick();
  const chipFor = (lv) => ch.el.find((e) => e.classList.contains('pk-chip') && e.textContent.startsWith(lv));
  chipFor('agb').fire('click');
  await tick();
  assert.deepEqual(store.get('filters:src'), {});
  ch.destroy();
}));

// ------------------------------------------------------------------ Aladin prefetch retry (note 109)

const DEAD_PAGE = {aladin: {cdn: 'https://127.0.0.1:9/none.js', mirror: 'https://127.0.0.1:9/none2.js'}};

function fakeAladinLib() {
  const made = [];
  const A = {
    init: Promise.resolve(),
    aladin(host, init) {
      const al = {host, init, on() {}, getRaDec: () => [1, 2], getFov: () => [0.5, 0.5]};
      made.push(al);
      return al;
    },
  };
  return {A, made};
}

async function withWindow(win, fn) {
  const had = Object.prototype.hasOwnProperty.call(globalThis, 'window');
  const prev = globalThis.window;
  globalThis.window = win;
  try {
    return await fn();
  } finally {
    if (had) globalThis.window = prev;
    else delete globalThis.window;
  }
}

test('retryAladinOnce: a failed prefetch is retried once per page, then the failure is kept', async () => {
  aladin.resetAladin();
  assert.equal(aladin.aladinRetried(), false);
  const pre = aladin.loadAladin(DEAD_PAGE, {timeout: 50});       // the fire-and-forget prefetch
  pre.catch(() => {});
  // while the prefetch is still loading, a mounting pane shares it (no retry)
  assert.equal(aladin.retryAladinOnce(DEAD_PAGE, {timeout: 50}), pre);
  await assert.rejects(pre);
  await tick();
  assert.equal(aladin.libState().state, 'error');
  const r1 = aladin.retryAladinOnce(DEAD_PAGE, {timeout: 50});   // first pane after the failure
  assert.notEqual(r1, pre);
  assert.equal(aladin.aladinRetried(), true);
  assert.equal(aladin.libState().state, 'loading');
  await assert.rejects(r1);
  await tick();
  assert.equal(aladin.libState().state, 'error');
  assert.equal(aladin.retryAladinOnce(DEAD_PAGE, {timeout: 50}), r1, 'second pane: no further attempt');
  assert.equal(aladin.loadAladin(DEAD_PAGE, {timeout: 50}), r1);
  aladin.resetAladin();
  assert.equal(aladin.libState().state, 'idle');
  assert.equal(aladin.aladinRetried(), false);
});

test('retryAladinOnce: the retry can succeed; a ready library is never reloaded', async () => {
  aladin.resetAladin();
  const pre = aladin.loadAladin(DEAD_PAGE, {timeout: 50});
  await assert.rejects(pre);
  await tick();
  const {A} = fakeAladinLib();
  await withWindow({A}, async () => {
    const r = await aladin.retryAladinOnce(DEAD_PAGE, {timeout: 50});
    assert.equal(r.source, 'preloaded');
    assert.equal(r.A, A);
    assert.equal(aladin.libState().state, 'ready');
    const again = aladin.retryAladinOnce(DEAD_PAGE, {timeout: 50});
    assert.equal(again, aladin.loadAladin(DEAD_PAGE), 'ready load is shared');
  });
  aladin.resetAladin();
});

test('aladinFor: a remount after a failed load retries once, then keeps the failure', () => withDom(async () => {
  aladin.resetAladin();
  const h1 = aladin.aladinFor('skyA', {page: DEAD_PAGE, timeout: 50, view: {ra: 10, dec: 20, fov: 1}});
  await assert.rejects(h1.ready);
  await tick();
  assert.equal(h1.state, 'error');
  assert.equal(aladin.aladinRetried(), false, 'the first load is not a retry');
  // the next mount starts the page's one retry itself and restarts the failed handle on it
  const ready1 = h1.ready;
  assert.equal(aladin.aladinFor('skyA', {page: DEAD_PAGE, timeout: 50}), h1);
  assert.equal(aladin.aladinRetried(), true);
  assert.equal(h1.state, 'loading');
  assert.notEqual(h1.ready, ready1);
  await assert.rejects(h1.ready);
  await tick();
  assert.equal(h1.state, 'error');
  // retry used: a further remount keeps the failure without another attempt
  const ready2 = h1.ready;
  assert.equal(aladin.aladinFor('skyA', {page: DEAD_PAGE, timeout: 50}), h1);
  assert.equal(h1.ready, ready2);
  assert.equal(h1.state, 'error');
  assert.equal(aladin.libState().state, 'error');
  aladin.resetAladin();
}));

test('aladinFor restarts a viewer that failed on the prefetch once the retry is under way', () => withDom(async () => {
  aladin.resetAladin();
  const h1 = aladin.aladinFor('skyA', {page: DEAD_PAGE, timeout: 50, view: {ra: 10, dec: 20, fov: 1}});
  await assert.rejects(h1.ready);
  await tick();
  assert.equal(h1.state, 'error');
  const {A, made} = fakeAladinLib();
  await withWindow({A}, async () => {
    const queued = [];
    const h2 = aladin.aladinFor('skyA', {page: DEAD_PAGE, timeout: 50, view: {ra: 30, dec: -5, fov: 2}});
    assert.equal(h2, h1, 'the pane keeps its handle');
    assert.equal(h2.state, 'loading');
    h2.run((al) => queued.push(al));
    await h2.ready;
    assert.equal(h2.state, 'ready');
    assert.equal(made.length, 1);
    assert.equal(made[0].init.target, '30 -5', 'restart uses the remounting pane view');
    assert.deepEqual(queued, [made[0]], 'commands queued during the retry run once ready');
    // a ready handle is never restarted
    assert.equal(aladin.aladinFor('skyA', {page: DEAD_PAGE}).ready, h2.ready);
    // a pane mounting for the first time after the retry gets its own viewer from it
    const h3 = aladin.aladinFor('skyB', {page: DEAD_PAGE, timeout: 50});
    await h3.ready;
    assert.equal(made.length, 2);
  });
  aladin.resetAladin();
}));

// panes/wavelength.js mounts through retryAladinOnce(app.page) like panes/sky.js (note 109); this
// covers a pane that reaches aladinFor without that call (the handle still starts the one retry).
test('aladinFor: a pane that skips retryAladinOnce at mount also retries a failed prefetch', () => withDom(async () => {
  aladin.resetAladin();
  const pre = aladin.loadAladin(DEAD_PAGE, {timeout: 50});      // the fire-and-forget prefetch
  await assert.rejects(pre);
  await tick();
  const {A, made} = fakeAladinLib();
  await withWindow({A}, async () => {
    // loadAladin(page) returns the cached rejection, no retry
    assert.equal(aladin.loadAladin(DEAD_PAGE), pre);
    const hw = aladin.aladinFor('wl1', {page: DEAD_PAGE, timeout: 50, view: {ra: 1, dec: 2, fov: 3}});
    assert.equal(aladin.aladinRetried(), true);
    await hw.ready;
    assert.equal(hw.state, 'ready');
    assert.equal(made.length, 1);
    // a sky pane mounting next shares the successful retry
    assert.notEqual(aladin.retryAladinOnce(DEAD_PAGE), pre);
    assert.equal(aladin.libState().state, 'ready');
  });
  aladin.resetAladin();
}));

// ------------------------------------------------------------------ widget CSS before mount (note 108)

test('registry.load attaches ui.css before the pane sheets of a widget pane, once', async () => {
  const {existsSync} = await import('node:fs');
  const {fileURLToPath} = await import('node:url');
  const regUrl = new URL('../../web/js/core/registry.js', import.meta.url);
  assert.ok(existsSync(fileURLToPath(new URL(reg.WIDGET_CSS, regUrl))));
  for (const t of reg.WIDGET_PANES) assert.ok(reg.PANE_TYPES.includes(t), t);
  for (const t of ['hist', 'spectrum2d', 'list', 'sky']) assert.equal(reg.WIDGET_PANES.includes(t), false, t);
  const links = [];
  const prev = globalThis.document;
  globalThis.document = {
    baseURI: regUrl.href,
    querySelectorAll: () => links.slice(),
    createElement: () => ({}),
    head: {appendChild(l) { links.push(l); queueMicrotask(() => l.onload && l.onload()); }},
  };
  const name = (l) => l.href.split('/').slice(-2).join('/');
  try {
    // modules already imported by this file, so load() only adds the sheets and resolves
    const s = await reg.load('sed');
    assert.equal(typeof s.mount, 'function');
    assert.deepEqual(links.map(name), ['panes/ui.css', 'panes/sed.css'], 'ui.css first, before the pane mounts');
    await reg.load('scatter');
    await reg.load('spectrum1d');
    assert.deepEqual(links.map(name),
      ['panes/ui.css', 'panes/sed.css', 'panes/scatter.css', 'panes/controls.css', 'panes/spectrum.css'],
      'one ui.css link for every widget pane');
    // boot's own later loadCSS of the same href reuses the link
    const {loadCSS} = await import('../../web/js/core/loadscript.js');
    assert.equal(await loadCSS(new URL('../../css/panes/ui.css', regUrl).href), true);
    assert.equal(links.length, 5);
  } finally {
    globalThis.document = prev;
  }
});

// ------------------------------------------------------------------ narrative beat hash rule (WPD2)
// A beat-applied view and overlay set is implied by beat= and stays out of the hash; a user
// pan/zoom or layer change writes sky= / ovl=; Aladin's adjustment of a programmatic goto is not
// a user change.

const B3 = {ra: 266.4368, dec: -29.0078, fov: 0.14};

test('settledSky: store origin writes nothing (Aladin adjustment of a stored-view goto)', () => {
  const cur = {...B3, [BEAT_VIEW]: 'b3'};
  assert.equal(skyp.settledSky(cur, {ra: 266.43681, dec: -29.0078, fov: 0.1406}, {kind: 'store'}, null), null);
  assert.equal(skyp.settledSky(cur, {ra: 10, dec: 10, fov: 3}, {kind: 'store'}, null), null);
});

test('settledSky: beat origin writes the view marked with the beat', () => {
  const cur = {base: 'dss', overlays: [{id: 'a'}], ...B3, [BEAT_VIEW]: 'b3'};
  // same beat and same view (within tolerance): nothing
  assert.equal(skyp.settledSky(cur, {...B3, ra: B3.ra + 1e-7}, {kind: 'beat', beat: 'b3'}, null), null);
  // the highlight centred somewhere else: written, implied by b3
  const out = skyp.settledSky(cur, {ra: 266.5, dec: -29.1, fov: 0.2}, {kind: 'beat', beat: 'b3'}, null);
  assert.deepEqual(out, {base: 'dss', overlays: [{id: 'a'}], ra: 266.5, dec: -29.1, fov: 0.2, [BEAT_VIEW]: 'b3'});
  // a beat-less page marks with ''
  const o2 = skyp.settledSky({...B3, [BEAT_VIEW]: ''}, {ra: 266.5, dec: -29.1, fov: 0.2}, {kind: 'beat', beat: ''}, null);
  assert.equal(o2[BEAT_VIEW], '');
  assert.equal(cur[BEAT_VIEW], 'b3', 'cur not modified');
});

test('settledSky: a beat goto is implied only when the narrative marked the view for that beat', () => {
  // b2 applied fov 0.17; b3 = {highlight} without a sky view: the narrative left the view
  // unmarked (its fov is b2's, which '#beat=b3' would not reproduce), so the centring is explicit
  const carried = {ra: 266.41, dec: -29.0, fov: 0.17, [BEAT_OVL]: 'b3'};
  const out = skyp.settledSky(carried, {ra: 266.43, dec: -29.01, fov: 0.17}, {kind: 'beat', beat: 'b3'}, null);
  assert.equal(BEAT_VIEW in out, false);
  assert.deepEqual(stateToHash({beat: 'b3', sky: out}), {sky: '266.43,-29.01,0.17', beat: 'b3'});
  // a stale marker of another beat is not re-marked either
  const stale = skyp.settledSky({...carried, [BEAT_VIEW]: 'b2'}, {ra: 266.43, dec: -29.01, fov: 0.17}, {kind: 'beat', beat: 'b3'}, null);
  assert.equal(BEAT_VIEW in stale, false);
  // narrative marked it (fov equal to the page default): implied
  const ok = skyp.settledSky({...carried, [BEAT_VIEW]: 'b3'}, {ra: 266.43, dec: -29.01, fov: 0.17}, {kind: 'beat', beat: 'b3'}, null);
  assert.equal(ok[BEAT_VIEW], 'b3');
});

test('settledSky: input during a programmatic goto keeps the goto as the origin when it lands', () => {
  const cur = {...B3, [BEAT_VIEW]: 'b3', [BEAT_OVL]: 'b3'};
  const before = {ra: 266.3, dec: -29.2, fov: 0.5};
  // a click while the goto to B3 settles; Aladin adjusts the fov 0.4 %
  const origin = {kind: 'user', prev: {kind: 'store'}, target: {...B3}};
  const adjusted = {ra: 266.4368, dec: -29.0078, fov: 0.14056};
  assert.equal(skyp.settledSky(cur, adjusted, origin, before), null);
  // a beat goto pending: written as the beat's
  const borigin = {kind: 'user', prev: {kind: 'beat', beat: 'b3'}, target: {ra: 266.45, dec: -29.0, fov: 0.14}};
  assert.equal(skyp.settledSky(cur, {ra: 266.4501, dec: -29.0, fov: 0.14}, borigin, before)[BEAT_VIEW], 'b3');
  // a real drag during the goto: the user's view
  const dragged = skyp.settledSky(cur, {ra: 266.6, dec: -29.0, fov: 0.14}, origin, before);
  assert.equal(BEAT_VIEW in dragged, false);
  assert.equal(dragged.ra, 266.6);
  assert.ok(skyp.nearView({ra: 359.9999, dec: 0, fov: 1}, {ra: 0.0001, dec: 0, fov: 1.01}));
  assert.ok(!skyp.nearView({ra: 10, dec: 0, fov: 1}, {ra: 10, dec: 0, fov: 1.2}));
});

test('settledSky: user and explicit origins drop beatView; unchanged views write nothing', () => {
  const cur = {...B3, [BEAT_VIEW]: 'b3', [BEAT_OVL]: 'b3', overlays: [{id: 'a'}]};
  const moved = {ra: 266.49, dec: -29.0078, fov: 0.21};
  for (const kind of ['user', 'explicit']) {
    const out = skyp.settledSky(cur, moved, {kind}, B3);
    assert.equal(out.ra, 266.49);
    assert.equal(out.fov, 0.21);
    assert.equal(BEAT_VIEW in out, false, kind);
    assert.equal(out[BEAT_OVL], 'b3', 'overlay marker untouched by a view change');
  }
  // user input that left the viewer where it was (a click): nothing
  assert.equal(skyp.settledSky(cur, moved, {kind: 'user'}, moved), null);
  // explicit goto to the stored view: nothing
  assert.equal(skyp.settledSky(cur, {...B3}, {kind: 'explicit'}, null), null);
  // missing origin counts as user
  assert.equal(BEAT_VIEW in skyp.settledSky(cur, moved, null, null), false);
  // RA wrap: 359.99999 vs 0 is the same view
  assert.equal(skyp.settledSky({ra: 0, dec: 0, fov: 1}, {ra: 359.999999, dec: 0, fov: 1}, {kind: 'user'}, null), null);
  assert.ok(skyp.USER_VIEW_EVENTS.includes('pointerdown') && skyp.USER_VIEW_EVENTS.includes('wheel'));
});

test('layersSky writes explicit overlays and drops beatOvl only', () => {
  const cur = {...B3, [BEAT_VIEW]: 'b3', [BEAT_OVL]: 'b3', overlays: [{id: 'a'}]};
  const out = skyp.layersSky(cur, {base: 'dss', fits: null,
    overlays: [{id: 'a', opacity: 0.5}, {id: 'rings', opacity: NaN}, {id: 'c'}]});
  assert.deepEqual(out.overlays, [{id: 'a', opacity: 0.5}, {id: 'rings'}, {id: 'c'}]);
  assert.equal(out.base, 'dss');
  assert.equal(BEAT_OVL in out, false);
  assert.equal(out[BEAT_VIEW], 'b3');
  assert.equal(cur[BEAT_OVL], 'b3', 'cur not modified');
  // a base-only change (overlays and opacities as before) keeps the overlay marker
  const prev = [{id: 'a', opacity: 1}, {id: 'rings', opacity: undefined}];
  const base = skyp.layersSky(cur, {base: '2mass', fits: [], overlays: [{id: 'rings', opacity: undefined}, {id: 'a', opacity: 1}]}, prev);
  assert.equal(base[BEAT_OVL], 'b3');
  assert.equal(base.base, '2mass');
  assert.deepEqual(stateToHash({beat: 'b3', sky: base}), {base: '2mass', beat: 'b3'});
  // an opacity change is an overlay change
  const op = skyp.layersSky(cur, {base: '2mass', fits: [], overlays: [{id: 'rings'}, {id: 'a', opacity: 0.5}]}, prev);
  assert.equal(BEAT_OVL in op, false);
});

test('narrative.explicitSky: marked parts and the page default are not explicit', () => {
  const page = {ra: 266.4168, dec: -29.0078, fov: 0.5, overlays: [{id: 'fields'}]};
  assert.deepEqual(narr.explicitSky(null, page), {view: false, ovl: false});
  assert.deepEqual(narr.explicitSky({...page}, page), {view: false, ovl: false});
  assert.deepEqual(narr.explicitSky({ra: 266.45, dec: -29.01, fov: 0.3}, page), {view: true, ovl: false});
  assert.deepEqual(narr.explicitSky({ra: 266.45, dec: -29.01, fov: 0.3, [BEAT_VIEW]: 'b3'}, page), {view: false, ovl: false});
  assert.deepEqual(narr.explicitSky({...page, overlays: [{id: 'rings'}]}, page), {view: false, ovl: true});
  assert.deepEqual(narr.explicitSky({...page, overlays: [{id: 'rings'}], [BEAT_OVL]: ''}, page), {view: false, ovl: false});
  // no page default: any finite view / overlay list is explicit
  assert.deepEqual(narr.explicitSky({ra: 1, dec: 2, overlays: []}, undefined), {view: true, ovl: true});
  assert.deepEqual(narr.explicitSky({ra: NaN, dec: 2}, undefined), {view: false, ovl: false});
});

test('narrative.beatSky applies and marks the beat parts, honouring keep and suppress_view', () => {
  const cur = {base: 'dss', ra: 1, dec: 2, fov: 3, overlays: [{id: 'x'}]};
  const view = {sky: B3};
  const s = narr.beatSky(cur, view, ['fields', 'marks'], {mark: 'b3'});
  assert.deepEqual(s, {base: 'dss', ...B3, overlays: [{id: 'fields'}, {id: 'marks'}], [BEAT_VIEW]: 'b3', [BEAT_OVL]: 'b3'});
  assert.deepEqual(cur, {base: 'dss', ra: 1, dec: 2, fov: 3, overlays: [{id: 'x'}]}, 'cur not modified');
  const kv = narr.beatSky(cur, view, ['fields'], {mark: 'b3', keepView: true});
  assert.equal(kv.ra, 1);
  assert.equal(BEAT_VIEW in kv, false);
  assert.equal(kv[BEAT_OVL], 'b3');
  const ko = narr.beatSky(cur, view, ['fields'], {mark: '', keepOvl: true});
  assert.deepEqual(ko.overlays, [{id: 'x'}]);
  assert.equal(BEAT_OVL in ko, false);
  assert.equal(ko[BEAT_VIEW], '');
  const sup = narr.beatSky(cur, {sky: B3, suppress_view: true}, [], {mark: 'b3'});
  assert.equal(sup.ra, 1);
  assert.equal(BEAT_VIEW in sup, false);
  const nofov = narr.beatSky(cur, {sky: {ra: 5, dec: 6}}, [], {mark: 'b3'});
  assert.deepEqual([nofov.ra, nofov.dec, nofov.fov], [5, 6, 3]);
  // the carried fov 3 is not what '#beat=b3' gives (no page default): explicit
  assert.equal(BEAT_VIEW in nofov, false);
  // ... and implied when it equals the page default fov
  assert.equal(narr.beatSky(cur, {sky: {ra: 5, dec: 6}}, [], {mark: 'b3', home: {ra: 0, dec: 0, fov: 3}})[BEAT_VIEW], 'b3');
});

test('narrative.beatSky marks a view the beat does not set only when it is the page default', () => {
  const home = {ra: 10, dec: 20, fov: 1};
  const prevBeat = {ra: 266.41, dec: -29.0, fov: 0.17, [BEAT_VIEW]: 'b2'};
  // viewless beat after b2: the view stays (keeps the user's zoom) and becomes explicit
  const v = narr.beatSky(prevBeat, {}, [], {mark: 'b4', home});
  assert.deepEqual([v.ra, v.dec, v.fov, BEAT_VIEW in v], [266.41, -29.0, 0.17, false]);
  assert.equal(stateToHash({beat: 'b4', sky: v}).sky, '266.41,-29,0.17');
  // suppress_view behaves the same
  assert.equal(BEAT_VIEW in narr.beatSky(prevBeat, {sky: {ra: 1, dec: 2, fov: 3}, suppress_view: true}, [], {mark: 'b4', home}), false);
  // viewless beat on the page default view (fresh load of '#beat=b4'): implied
  assert.equal(narr.beatSky({...home, [BEAT_VIEW]: 'b4'}, {}, [], {mark: 'b4', home})[BEAT_VIEW], 'b4');
  // no page default and no view yet (fresh load, the sky pane's initial view): implied
  assert.equal(narr.beatSky({}, {}, [], {mark: 'b4', home: null})[BEAT_VIEW], 'b4');
  // highlight goto beat: the centre is the beat's, the fov must be the default one
  assert.equal(BEAT_VIEW in narr.beatSky(prevBeat, {}, [], {mark: 'b3', home, goto: true}), false);
  assert.equal(narr.beatSky({ra: 266.41, dec: -29.0, fov: 1}, {}, [], {mark: 'b3', home, goto: true})[BEAT_VIEW], 'b3');
});

test('narrative.explicitSky: with a beat set only the markers decide', () => {
  const page = {ra: 10, dec: 20, fov: 1, overlays: [{id: 'fields'}]};
  // '#sky=<default>&ovl=<default>&beat=b3': explicit, kept over b3's view
  assert.deepEqual(narr.explicitSky({...page}, page, {beatSet: true}), {view: true, ovl: true});
  assert.deepEqual(narr.explicitSky({...page, [BEAT_VIEW]: 'b3', [BEAT_OVL]: 'b3'}, page, {beatSet: true}), {view: false, ovl: false});
});

test('narrative.sameSky compares view, markers and overlay sets', () => {
  const a = {...B3, overlays: [{id: 'b'}, {id: 'a'}], [BEAT_VIEW]: 'b3', [BEAT_OVL]: 'b3', base: 'x'};
  assert.ok(narr.sameSky(a, {...a, overlays: [{id: 'a'}, {id: 'b'}], base: 'y'}));
  assert.ok(!narr.sameSky(a, {...a, [BEAT_VIEW]: 'b4'}));
  assert.ok(!narr.sameSky(a, {...a, [BEAT_OVL]: undefined}));
  assert.ok(!narr.sameSky(a, {...a, fov: 0.2}));
  assert.ok(!narr.sameSky(a, {...a, overlays: [{id: 'a'}]}));
  assert.ok(!narr.sameSky({...a, overlays: undefined}, {...a, overlays: []}));
});

test('beat rule end to end: beat writes stay out of the hash, user writes go in', () => {
  const h = (beat, sky) => stateToHash({beat, sky});
  // the narrative applies b3, the sky pane's highlight goto settles: no sky= / ovl=
  let sky = narr.beatSky({}, {sky: B3}, ['fields'], {mark: 'b3'});
  const hl = skyp.settledSky(sky, {ra: 266.44, dec: -29.0, fov: 0.14}, {kind: 'beat', beat: 'b3'}, null);
  assert.deepEqual(h('b3', hl), {beat: 'b3'});
  // Aladin adjusts the goto: nothing written
  assert.equal(skyp.settledSky(hl, {ra: 266.440001, dec: -29.0, fov: 0.1401}, {kind: 'store'}, null), null);
  // the user drags: sky= written
  sky = skyp.settledSky(hl, {ra: 266.49, dec: -29.0, fov: 0.14}, {kind: 'user'}, hl);
  assert.deepEqual(h('b3', sky), {sky: '266.49,-29,0.14', beat: 'b3'});
  // the user toggles a layer: ovl= written as well
  sky = skyp.layersSky(sky, {base: undefined, fits: undefined, overlays: [{id: 'fields'}, {id: 'rings'}]});
  const both = h('b3', sky);
  assert.equal(both.sky, '266.49,-29,0.14');
  assert.ok(typeof both.ovl === 'string' && both.ovl.includes('rings'), JSON.stringify(both));
  // the next beat re-marks both parts: beat only again
  sky = narr.beatSky(sky, {sky: {ra: 266.5, dec: -29.1, fov: 0.2}}, ['marks'], {mark: 'b4'});
  assert.deepEqual(h('b4', sky), {beat: 'b4'});
});

// ---- narrative pane mount: the narrative:highlight goto condition (docs/js.md event table)

class NarEl extends FakeEl {
  constructor(tag) {
    super(tag);
    const cl = this.classList;
    cl.add = (c) => cl.toggle(c, true);
    cl.remove = (c) => cl.toggle(c, false);
    this.clientHeight = 100;
  }
  removeAttribute(k) { delete this.attrs[k]; }
  removeEventListener(t, fn) { if (this.listeners[t]) this.listeners[t] = this.listeners[t].filter((f) => f !== fn); }
  closest() { return null; }
  querySelector() { return null; }
  // beats stacked 100 px apart under a 100 px band (centre line at 50). scrollIntoView centres
  // the beat at once (NarEl.instant: prefers-reduced-motion, scroll-behavior auto) or on a
  // later task (the pane's default scroll-behavior smooth)
  getBoundingClientRect() {
    const k = this.attrs['data-k'];
    const top = k === undefined ? 0 : Number(k) * 100 - NarEl.scrollTop;
    return {top, bottom: top + 100, left: 0, right: 100, width: 100, height: 100};
  }
  scrollIntoView() {
    const to = Number(this.attrs['data-k'] || 0) * 100;
    if (NarEl.instant) NarEl.scrollTop = to;
    else setTimeout(() => { NarEl.scrollTop = to; }, 0);
  }
}
NarEl.scrollTop = 0;
NarEl.instant = false;

const NAR_HOME = {ra: 266.42, dec: -29.0, fov: 0.5};
const NAR_HL = {ds: 'src', ids: ['s1', 's2', 's3']};
const NAR_DOC = {beats: [
  {id: 'b1', view: {pane: 'sky', sky: {ra: 266.40, dec: -29.01, fov: 0.2}}},
  {id: 'b2', view: {pane: 'sky', highlight: NAR_HL}},                                   // highlight only
  {id: 'b3', view: {pane: 'sky', sky: {ra: 266.45, dec: -29.02, fov: 0.1}, highlight: NAR_HL}},
  {id: 'b4', view: {pane: 'sky', highlight: NAR_HL, suppress_view: true}},
  {id: 'b5', view: {pane: 'sky', highlight: {ds: 'src', ids: []}}},                     // empty ids: no highlight
]};

/** Mount a narrative pane over NAR_DOC with a sky pane beside it; returns {pane, store, events}. */
async function narrativeHarness(initial = {}, {instant = false} = {}) {
  const prev = {document: globalThis.document, raf: globalThis.requestAnimationFrame};
  globalThis.document = {createElement: (t) => new NarEl(t), activeElement: null, createTextNode: (t) => ({textContent: t})};
  globalThis.requestAnimationFrame = (f) => setTimeout(f, 0);
  NarEl.scrollTop = 0;
  NarEl.instant = instant;
  const store = createStore(initial);
  const events = [];
  store.onEvent('narrative:highlight', (p) => events.push(p));
  const ctx = {id: 'nar', config: {narrative: 'story'}, store, setStatus() {},
    app: {page: {datasets: {}, state: {sky: NAR_HOME}}},
    hub: {json: async () => NAR_DOC},
    layout: {panes: () => [{id: 'nar', type: 'narrative'}, {id: 'sky1', type: 'sky'}]}};
  const pane = narr.mount(new NarEl('div'), ctx);
  await pane.ready;
  const restore = () => {
    pane.destroy();
    globalThis.document = prev.document;
    if (prev.raf === undefined) delete globalThis.requestAnimationFrame;
    else globalThis.requestAnimationFrame = prev.raf;
  };
  return {pane, store, events, restore};
}

const lastHl = (events) => events.filter((e) => e.ids.length || e.goto !== false).at(-1);

test('narrative:highlight goto: a highlight-only beat asks the sky pane to bring the rows into view', async () => {
  const h = await narrativeHarness({beat: 'b2'});
  try {
    assert.equal(h.pane.stats().beat, 'b2');
    const e = lastHl(h.events);
    assert.deepEqual([e.pane, e.ds, e.ids, e.beat, e.goto], ['sky1', 'src', ['s1', 's2', 's3'], 'b2', true]);
    assert.ok(Number.isInteger(e.seq) && e.seq > 0, 'each goto request carries a seq');
    // the centring is implied by beat=b2: the hash carries the beat only
    assert.deepEqual(stateToHash({beat: h.store.get('beat'), sky: h.store.get('sky')}), {beat: 'b2'});
  } finally {
    h.restore();
  }
});

test('narrative:highlight goto: not with a beat sky view, suppress_view, or empty ids', async () => {
  const h = await narrativeHarness({});
  try {
    const n0 = h.events.length;
    h.pane.go('b3');
    let e = h.events.at(-1);
    assert.deepEqual([e.beat, e.ids.length, 'goto' in e], ['b3', 3, false], 'beat sky view: rings only');
    h.pane.go('b4');
    e = h.events.at(-1);
    assert.deepEqual([e.beat, e.ids.length, 'goto' in e], ['b4', 3, false], 'suppress_view: rings only');
    h.pane.go('b5');
    e = h.events.at(-1);
    assert.deepEqual([e.beat, e.ids.length, 'goto' in e], ['b5', 0, false]);
    // go() to the highlight-only beat: goto, with a new seq each time
    h.pane.go('b2');
    const g1 = h.events.at(-1);
    assert.equal(g1.goto, true);
    h.pane.go('b1');
    h.pane.go('b2');
    const g2 = h.events.at(-1);
    assert.equal(g2.goto, true);
    assert.ok(g2.seq > g1.seq);
    assert.ok(h.events.length > n0);
  } finally {
    h.restore();
  }
});

test('narrative:highlight goto: an explicit deep-link view kept over the beat is not moved', async () => {
  // '#sky=..&beat=b2': the hash view is explicit (no beatView marker) and stays
  const deep = {ra: 266.3, dec: -28.9, fov: 0.3};
  const h = await narrativeHarness({beat: 'b2', sky: {...deep}});
  try {
    assert.equal(h.pane.stats().beat, 'b2');
    const e = lastHl(h.events);
    assert.deepEqual([e.beat, e.ids.length, 'goto' in e], ['b2', 3, false]);
    const sky = h.store.get('sky');
    assert.deepEqual([sky.ra, sky.dec, sky.fov], [deep.ra, deep.dec, deep.fov]);
    assert.equal(BEAT_VIEW in sky, false);
    assert.equal(stateToHash({beat: 'b2', sky}).sky, '266.3,-28.9,0.3');
  } finally {
    h.restore();
  }
});

// WPE4 (was a WPE2 contract issue): with an instant scroll (prefers-reduced-motion) the beat
// reaches the band centre inside scrollIntoView; activate() sets S.current before scrolling, so
// arrived() -> unpin() -> pickActive() finds the beat already active and does not activate it a
// second time without `keep` (which asked for the goto and left the deep-link view).
test('narrative:highlight goto: deep-link view kept over the beat with an instant scroll', async () => {
  const deep = {ra: 266.3, dec: -28.9, fov: 0.3};
  const h = await narrativeHarness({beat: 'b2', sky: {...deep}}, {instant: true});
  try {
    assert.equal(h.pane.stats().beat, 'b2');
    assert.equal(h.events.filter((e) => e.goto === true).length, 0, 'no goto at any point');
    const e = lastHl(h.events);
    assert.deepEqual([e.beat, e.ids.length, 'goto' in e], ['b2', 3, false]);
    const sky = h.store.get('sky');
    assert.deepEqual([sky.ra, sky.dec, sky.fov], [deep.ra, deep.dec, deep.fov]);
    assert.equal(BEAT_VIEW in sky, false);
    assert.equal(stateToHash({beat: 'b2', sky}).sky, '266.3,-28.9,0.3');
  } finally {
    h.restore();
  }
});

test('narrative: an instant-scroll activation runs once (go() and a hash beat change)', async () => {
  const h = await narrativeHarness({}, {instant: true});
  try {
    assert.equal(h.pane.stats().beat, 'b1');
    let n0 = h.events.length;
    h.pane.go('b3');
    assert.equal(h.pane.stats().beat, 'b3');
    assert.equal(h.events.length - n0, 1, 'one highlight per activation');
    // go() to the highlight-only beat: exactly one goto request
    n0 = h.events.length;
    h.pane.go('b2');
    const fresh = h.events.slice(n0);
    assert.deepEqual(fresh.map((e) => [e.beat, e.goto === true]), [['b2', true]]);
    // go() to the current beat scrolls but does not re-activate
    n0 = h.events.length;
    h.pane.go('b2');
    assert.equal(h.events.length, n0);
    assert.equal(NarEl.scrollTop, 100, 'beat b2 centred');
    // hash navigation to a highlight-only beat with an explicit view keeps that view
    const deep = {ra: 266.31, dec: -28.91, fov: 0.25};
    h.pane.go('b1');
    n0 = h.events.length;
    const tick = () => new Promise((r) => setTimeout(r, 0));
    h.store.set({beat: 'b4', sky: {...deep}}, {source: 'hash'});
    await tick();
    assert.equal(h.pane.stats().beat, 'b4');
    h.store.set({beat: 'b2'}, {source: 'hash'});
    await tick();
    assert.equal(h.pane.stats().beat, 'b2');
    assert.equal(h.events.slice(n0).filter((e) => e.goto === true).length, 0);
    const sky = h.store.get('sky');
    assert.deepEqual([sky.ra, sky.dec, sky.fov], [deep.ra, deep.dec, deep.fov]);
  } finally {
    h.restore();
  }
});

test('sky store listener follows only a changed stored view (storedView / sameStoredView)', () => {
  // a beat that keeps the view (highlight-only) writes overlays and markers with the same view;
  // that write is delivered after the highlight goto and must not count as a new view
  const b2 = {ra: 266.4268, dec: -29.0078, fov: 0.17};
  const prev = skyp.storedView({...b2, [BEAT_VIEW]: 'b2'});
  assert.deepEqual(prev, b2);
  assert.ok(skyp.sameStoredView(prev, {...b2, overlays: [{id: 'fields'}], [BEAT_OVL]: 'b4'}));
  assert.ok(!skyp.sameStoredView(prev, {...b2, ra: 266.5}), 'a moved centre is a change');
  assert.ok(!skyp.sameStoredView(prev, {...b2, fov: 0.3}), 'a new fov is a change');
  assert.ok(!skyp.sameStoredView({ra: 1, dec: 2, fov: NaN}, {ra: 1, dec: 2, fov: 3}), 'a fov appearing is a change');
  assert.ok(!skyp.sameStoredView(null, b2), 'no previous view: follow');
  assert.equal(skyp.storedView({beatView: 'b3'}), null);
});
