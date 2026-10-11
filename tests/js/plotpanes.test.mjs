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
import {createStore} from '../../web/js/core/store.js';
import {Budget} from '../../web/js/gl/budget.js';
import {makeView} from '../../web/js/plot/axes.js';

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
