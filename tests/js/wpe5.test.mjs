// node --test tests/js/wpe5.test.mjs
// Wave E JS follow-ups: manifold default frame, canonical `speed`, legacy dim alias and the
// spherex-colours grammar, recipe presets with modifiers, per-segment spectrum readout,
// context binned-statistic grids and the raster / overlay layer switches, gallery preset roles,
// meta.fluxscale and foreign record sets (id_column).
import test from 'node:test';
import assert from 'node:assert/strict';
import {gzipSync} from 'node:zlib';

import {frameDefault, manifestFrameKnown, buildDefaults} from '../../web/js/core/boot.js';
import {stateToHash, hashToPatch, parseHash, bindHash, HASH_ORDER, decodeJSONParam} from '../../web/js/core/hash.js';
import {createStore} from '../../web/js/core/store.js';
import {mapLegacy, GRAMMARS, encodeRecipe, dimAlias, aliasDim} from '../../web/js/core/legacyhash.js';
import {Selection} from '../../web/js/core/selection.js';
import {applyPresetSelection, presetSelectionHit} from '../../web/js/ui/chips.js';
import {
  normalizeRecord, fluxScaleOf, readoutSamples, nearestInSegment, segmentRanges, hasRecordOf,
} from '../../web/js/panes/spectrum1d.js';
import {
  statGridOptions, parseStatValue, layerState, visibleOverlays, withRaster, CTX_STAT_SEP,
} from '../../web/js/panes/scatter.js';
import {presetRole, presetFacets, matchPreset, presetPatch, normalizeFacets} from '../../web/js/panes/gallery.js';
import {initialPreset, knownPreset, tourSpeed} from '../../web/js/panes/manifold.js';
import {Tour} from '../../web/js/math/tour.js';
import {RecordStore, recordIndexUrl, foreignKeyOf, isForeignRef, foreignPageBase} from '../../web/js/data/records.js';

// ---------------------------------------------------------------- 1. manifold default frame

test('frameDefault: options.frame, else a known manifest default_frame, else pca', () => {
  const man = {manifold: {default_frame: 'gmm', frames: {gmm: [1, 0, 0, 1]}, dims: ['a', 'b']}};
  assert.equal(frameDefault({frame: 'pair:0,1'}, man), 'pair:0,1');
  assert.equal(frameDefault({}, man), 'gmm');
  assert.equal(frameDefault({}, {manifold: {default_frame: 'pair:1,2'}}), 'pair:1,2');
  assert.equal(frameDefault({}, {manifold: {default_frame: 'ax1'}, presets: [{key: 'ax1', kind: 'axes', x: 'a', y: 'b'}]}), 'ax1');
  assert.equal(frameDefault({}, {manifold: {default_frame: 'nope'}}), 'pca', 'unknown preset falls back');
  assert.equal(frameDefault({}, {manifold: {}}), 'pca');
  assert.equal(frameDefault(null, null), 'pca');
  assert.equal(manifestFrameKnown({}, 'pca'), true);
  assert.equal(manifestFrameKnown({presets: [{key: 'k', kind: 'selection'}]}, 'k'), false);
});

test('initialPreset mirrors frameDefault for the pane and keeps a stored frame', () => {
  const man = {manifold: {default_frame: 'gmm'}};
  const presets = [{key: 'gmm'}];
  assert.equal(initialPreset({}, {}, man, presets), 'gmm');
  assert.equal(initialPreset({preset: null}, {}, man, presets), 'gmm', 'store preset null = unset');
  assert.equal(initialPreset({preset: 'pair:0,2'}, {}, man, presets), 'pair:0,2');
  assert.equal(initialPreset({}, {frame: 'pca'}, man, presets), 'pca');
  assert.equal(initialPreset({}, {}, man, []), 'pca', 'unknown default_frame');
  assert.equal(initialPreset({}, {}, {}, presets), 'pca');
  assert.equal(initialPreset({F: [1, 0, 0, 1]}, {}, man, presets), null);
  assert.equal(knownPreset('pair:3,4'), true);
  assert.equal(knownPreset('', []), false);
});

test('buildDefaults: manifold frame preset open until the manifest arrives; speed default', () => {
  const page = {datasets: {src: {kind: 'catalog', url: 'x'}}};
  const panes = new Map([
    ['mani', {id: 'mani', type: 'manifold', dataset: 'src', options: {dims: ['a', 'b']}}],
    ['m2', {id: 'm2', type: 'manifold', dataset: 'src', options: {frame: 'pair:0,1', speed: 2.5}}],
  ]);
  const d = buildDefaults(page, {panes});
  assert.equal(d['frame:mani'].preset, null);
  assert.equal(d['frame:m2'].preset, 'pair:0,1');
  assert.equal(d.speed, 2.5);
  const d2 = buildDefaults(page, {panes: new Map([['mani', panes.get('mani')]])});
  assert.equal(d2.speed, 1);
});

// ---------------------------------------------------------------- 2. speed

test('speed: canonical hash key, store round trip, tour honours it', () => {
  assert.ok(HASH_ORDER.includes('speed'));
  assert.equal(stateToHash({speed: 2}).speed, '2');
  assert.equal(stateToHash({speed: 0}).speed, undefined);
  assert.equal(hashToPatch(parseHash('speed=0.5')).patch.speed, 0.5);
  assert.equal(hashToPatch(parseHash('speed=-1')).patch.speed, undefined);
  assert.equal(tourSpeed(3, {speed: 2}), 3);
  assert.equal(tourSpeed(null, {speed: 2}), 2);
  assert.equal(tourSpeed(undefined, {}), 1);
  assert.equal(tourSpeed('abc', null), 1);
  const t = new Tour(3, {speed: tourSpeed(4, {})});
  assert.equal(t.speed, 4);
});

test('legacy speed: wavelength ?speed= and hash speed=, kv-view speed=', () => {
  const r = mapLegacy(['wavelength'], {hash: '', search: '?speed=3'});
  assert.equal(r.hash.speed, '3');
  assert.ok(r.consumedSearch.includes('speed'));
  assert.equal(r.fromHash, false);
  const r2 = mapLegacy(['wavelength'], {hash: '#ra=10&dec=20&speed=2', search: ''});
  assert.equal(r2.hash.speed, '2');
  assert.equal(r2.hash.sky, '10,20,0.25');
  // ?speed= merges also when the hash is canonical
  const r3 = mapLegacy(['wavelength'], {hash: '#id=S1', search: '?speed=0.25'});
  assert.deepEqual(r3.hash, {speed: '0.25'});
  const r4 = mapLegacy(['kv-view'], {hash: '#x=a&y=b&speed=1.5', search: '', page: {page: {}, layout: null}});
  assert.equal(r4.hash.speed, '1.5');
  assert.equal(r4.fromHash, true);
  // manifold pages (kv-view): legacy ?speed= maps to speed and is consumed, also under a
  // canonical hash or with no hash
  const r5 = mapLegacy(['kv-view', 'bare-name'], {hash: '#name=foo', search: '?speed=3', page: {page: {}, layout: null}});
  assert.deepEqual(r5.hash, {id: 'foo', speed: '3'});
  assert.deepEqual(r5.consumedSearch, ['speed']);
  assert.equal(r5.fromHash, true);
  const r6 = mapLegacy(['kv-view', 'bare-name'], {hash: '', search: '?speed=3'});
  assert.deepEqual(r6, {hash: {speed: '3'}, consumedSearch: ['speed'], fromHash: false});
  const r7 = mapLegacy(['kv-view', 'bare-name'], {hash: '#foo', search: '?speed=0.5'});
  assert.deepEqual(r7.hash, {id: 'foo', speed: '0.5'});
  const r8 = mapLegacy(['kv-view'], {hash: '#id=S1', search: '?speed=-1'});
  assert.deepEqual(r8, {hash: {}, consumedSearch: ['speed'], fromHash: false}, 'bad speed consumed and dropped');
  assert.equal(GRAMMARS['kv-view']({hash: '', search: '?speed=2'}).speed, '2');
});

// ---------------------------------------------------------------- 3. dim alias + spherex-colours

const ALIAS = {c_F162M_F210M: 'f162m-f210m', m_F162M: 'f162m', m_F210M: 'f210m', c_F150W_F200W: 'f150w-f200w'};
const SPX_PAGE = {
  page: {legacy: {dim_alias: ALIAS}},
  layout: {row: [
    {pane: 'scatter', id: 'ccd', options: {overlays: ['yoo_a', 'yoo_b', 'g23_vector'], color: 's'}},
    {pane: 'scatter', id: 'cmd', options: {}},
  ]},
};

test('dim alias: table lookup and kv-view x / y / c', () => {
  assert.deepEqual(dimAlias({page: SPX_PAGE}), ALIAS);
  assert.deepEqual(dimAlias({}), {});
  assert.equal(aliasDim(ALIAS, 'm_F162M'), 'f162m');
  assert.equal(aliasDim(ALIAS, 'other'), 'other');
  // x / y name dims of the manifold through the alias: frame row 0 on dim 1, row 1 on dim 0
  const r = mapLegacy(['kv-view'], {hash: '#x=c_F162M_F210M&y=m_F162M&c=c_F162M_F210M~magma~0~2', search: '',
    page: {page: SPX_PAGE.page, layout: null}, dims: ['f162m', 'f162m-f210m'], pane: 'mani', ds: 'src'});
  assert.equal(r.hash.c, 'f162m-f210m~magma~0~2');
  const F = new Float32Array(Uint8Array.from(Buffer.from(r.hash['mani.frame'].slice(4), 'base64url')).buffer);
  assert.deepEqual(Array.from(F), [0, 1, 1, 0]);
  // without the alias the legacy names match no dim and no frame is written
  const r0 = mapLegacy(['kv-view'], {hash: '#x=c_F162M_F210M&y=m_F162M', search: '', dimAlias: {},
    page: {page: {}, layout: null}, dims: ['f162m', 'f162m-f210m'], pane: 'mani', ds: 'src'});
  assert.equal(r0.hash['mani.frame'], undefined);
});

test('spherex-colours: axes, CMD, colour-by, zoom, yoo layers, hidden levels', () => {
  const loc = {hash: '', page: SPX_PAGE,
    search: '?ax=F162M&ay=F210M&bx=F150W&by=F200W&c=s&band=F162M&cmd=1&zoom=0,1,-0.5,2&yoo=1&hide=young|evolved'};
  const r = mapLegacy(['spherex-colours'], loc);
  const h = r.hash;
  assert.equal(h['ccd.x'], 'f162m-f210m');
  assert.equal(h['ccd.y'], 'f150w-f200w');
  assert.equal(h['cmd.y'], 'f162m');
  assert.equal(h['cmd.x'], 'f162m-f210m');
  assert.equal(h.view, 'cmd:half');
  assert.equal(h.c, 's');
  assert.equal(h['ccd.xr'], '0,1');
  assert.equal(h['ccd.yr'], '-0.5,2');
  assert.equal(h['ccd.ovl'], 'yoo_a,yoo_b,g23_vector');
  assert.deepEqual(decodeJSONParam(h.f), {cats: {s: ['young', 'evolved']}});
  assert.deepEqual(r.consumedSearch.sort(), ['ax', 'ay', 'band', 'bx', 'by', 'c', 'cmd', 'hide', 'yoo', 'zoom']);
  // without yoo the legacy page drew no Yoo layer
  const r2 = mapLegacy(['spherex-colours'], {hash: '', page: SPX_PAGE, search: '?ax=F162M&ay=F210M'});
  assert.equal(r2.hash['ccd.ovl'], 'g23_vector');
  // unaliased filters fall back to lower-case colours; a yoo list picks layers
  const r3 = GRAMMARS['spherex-colours']({hash: '', page: SPX_PAGE, search: '?ax=F444W&ay=F480M&yoo=b'});
  assert.equal(r3['ccd.x'], 'f444w-f480m');
  assert.equal(r3['ccd.ovl'], 'yoo_b,g23_vector');
  assert.equal(GRAMMARS['spherex-colours']({hash: '', search: '?foo=1'}), null);
  // the canonical keys parse back into store patches
  const p = hashToPatch(parseHash(Object.entries(h).map(([k, v]) => `${k}=${v}`).join('&')), {primaryDs: 'src', state: {
    'axes:ccd': {x: null, y: null}, 'axes:cmd': {x: null, y: null}}}).patch;
  assert.deepEqual(p['axes:ccd'].xr, [0, 1]);
  assert.deepEqual(p['axes:ccd'].yr, [-0.5, 2]);
  assert.deepEqual(p['layers:ccd'].ovl, ['yoo_a', 'yoo_b', 'g23_vector']);
});

test('spherex-colours: one scatter pane drops the CMD keys', () => {
  const page = {page: SPX_PAGE.page, layout: {pane: 'scatter', id: 'ccd', options: {}}};
  const r = mapLegacy(['spherex-colours'], {hash: '', page, search: '?ax=F162M&ay=F210M&band=F162M&cmd=1'});
  assert.deepEqual(r.hash, {'ccd.x': 'f162m-f210m'});
  assert.deepEqual(r.consumedSearch.sort(), ['ax', 'ay', 'band', 'cmd']);
  // a single scatter pane with another id is the CCD
  const page2 = {page: SPX_PAGE.page, layout: {pane: 'scatter', id: 'cmd', options: {}}};
  const r2 = mapLegacy(['spherex-colours'], {hash: '', page: page2, search: '?ax=F162M&ay=F210M&band=F162M&cmd=1'});
  assert.deepEqual(r2.hash, {'cmd.x': 'f162m-f210m'});
});

test('mapLegacy: fromHash only when a grammar mapped the hash', () => {
  assert.equal(mapLegacy(['spherex-colours'], {hash: '#id=X', search: '?ax=F162M&ay=F210M', page: SPX_PAGE}).fromHash, false);
  assert.equal(mapLegacy([], {hash: '#x', search: ''}).fromHash, false);
});

// ---------------------------------------------------------------- hash: pane layer keys + setDefaults

test('hash: <pane>.raster / <pane>.ovl / <pane>.xr round trip', () => {
  const h = stateToHash({'layers:ccd': {raster: false, ovl: ['a', 'b']}, 'layers:cmd': {ovl: []},
    'axes:ccd': {x: 'a', y: 'b', xr: [0, 1.5], yr: null}});
  assert.equal(h['ccd.raster'], '0');
  assert.equal(h['ccd.ovl'], 'a,b');
  assert.equal(h['cmd.ovl'], '-');
  assert.equal(h['ccd.xr'], '0,1.5');
  const p = hashToPatch(parseHash('ccd.raster=1&cmd.ovl=-&ccd.ovl=x')).patch;
  assert.equal(p['layers:ccd'].raster, true);
  assert.deepEqual(p['layers:ccd'].ovl, ['x']);
  assert.deepEqual(p['layers:cmd'].ovl, []);
});

function fakeWin(hash = '') {
  let url = `/p.html${hash ? '#' + hash : ''}`;
  const timers = [];
  return {
    location: {pathname: '/p.html', search: '', get hash() { const i = url.indexOf('#'); return i < 0 ? '' : url.slice(i); }},
    history: {state: null, pushState(s, t, u) { url = u; }, replaceState(s, t, u) { url = u; }},
    addEventListener() {}, removeEventListener() {},
    setTimeout(fn) { timers.push(fn); return timers.length; }, clearTimeout() {},
    flush() { while (timers.length) timers.shift()(); },
  };
}

test('bindHash.setDefaults: a manifest default frame stays out of the hash', () => {
  const init = {focus: null, color: {by: null, cmap: 'viridis'}, av: 0, rv: 3.1, dm: 0, speed: 1,
    'frame:mani': {preset: null, dims: []}};
  const store = createStore(structuredClone(init), {schedule: (fn) => fn()});
  const win = fakeWin('');
  const unbind = bindHash(store, {primaryDs: 'src', defaults: structuredClone(init), win});
  assert.equal(typeof unbind.setDefaults, 'function');
  store.set({'frame:mani': {preset: 'gmm', dims: []}}, {source: 'mani'});
  assert.match(unbind.compute(), /mani\.frame=gmm/);
  unbind.setDefaults({...init, 'frame:mani': {preset: 'gmm', dims: []}});
  assert.doesNotMatch(unbind.compute(), /mani\.frame/);
  store.set({speed: 2});
  assert.match(unbind.compute(), /speed=2/);
  unbind();
});

// ---------------------------------------------------------------- 4. chips: recipe presets with modifiers

function chipEnv(n, hitIdx) {
  const sel = new Selection(n);
  const events = [];
  const calls = {applySelection: 0, selectionHit: 0};
  const app = {
    selectionFor: async () => sel,
    resolveIds: async (ds, ids) => ids.map((id) => (id === 'a' ? 0 : id === 'b' ? 1 : -1)),
    selectionHit: async () => {
      calls.selectionHit++;
      const m = new Uint8Array(n);
      for (const i of hitIdx) m[i] = 1;
      return m;
    },
    applySelection: async () => { calls.applySelection++; return sel; },
  };
  const store = {emit: (t, v) => events.push([t, v])};
  return {sel, events, calls, env: {app, store, client: {n}, ds: 'src', pane: 'p'}};
}

test('recipe (r:) presets honour shift (add) and alt (subtract)', async () => {
  const p = {key: 'young', kind: 'selection', sel: encodeRecipe([{op: 'replace', kind: 'ids', ids: ['a']}])};
  const {sel, env, events, calls} = chipEnv(4, [2, 3]);
  sel.apply(Uint8Array.from([1, 0, 1, 0]), 'replace');
  await applyPresetSelection(env, p, 'add');
  assert.deepEqual(Array.from(sel.mask), [1, 0, 1, 1]);
  await applyPresetSelection(env, p, 'subtract');
  assert.deepEqual(Array.from(sel.mask), [1, 0, 0, 0]);
  await applyPresetSelection(env, p, 'replace');
  assert.deepEqual(Array.from(sel.mask), [0, 0, 1, 1]);
  assert.equal(calls.applySelection, 0);
  assert.equal(calls.selectionHit, 3);
  assert.deepEqual(events.filter((e) => e[0] === 'brush:commit').map((e) => e[1].op), ['add', 'subtract', 'replace']);
  // ids presets keep working
  const ids = await presetSelectionHit(env, {key: 'k', sel: 'ids:a,b,zz'});
  assert.deepEqual(Array.from(ids), [1, 1, 0, 0]);
  // without app.selectionHit a recipe preset falls back to the replace path with a notice
  const fb = chipEnv(4, []);
  delete fb.env.app.selectionHit;
  await applyPresetSelection(fb.env, p, 'add');
  assert.equal(fb.calls.applySelection, 1);
  assert.ok(fb.events.some((e) => e[0] === 'toast'));
});

// ---------------------------------------------------------------- 5. spectrum readout per segment

test('readoutSamples: sorted and unsorted segments, overlaps, hidden segments', () => {
  // segment A sorted 1..1.4, segment B unsorted 1.3..1.7 (overlaps A)
  const wave = Float64Array.from([1.0, 1.1, 1.2, 1.3, 1.4, 1.7, 1.3, 1.5, 1.6, 1.45]);
  const y = Float32Array.from([10, 11, 12, 13, 14, 27, 23, 25, 26, 24.5]);
  const rec = {n: 10, wave, segments: [[0, 5, 'A'], [5, 10, 'B']]};
  const s = {wave, y, err: null, ranges: segmentRanges(rec)};
  const at = readoutSamples(s, 1.31);
  assert.deepEqual(at.map((r) => [r.label, r.k]), [['A', 3], ['B', 6]]);
  const b = readoutSamples(s, 1.62);
  assert.deepEqual(b.map((r) => [r.label, r.y]), [['B', 26]]);
  const out = readoutSamples(s, 0.5);
  assert.equal(out.length, 1);
  assert.equal(out[0].k, 0, 'outside every segment: the nearest sample');
  const hidden = readoutSamples({...s, ranges: segmentRanges(rec, {A: false})}, 1.31);
  assert.deepEqual(hidden.map((r) => r.label), ['B']);
  assert.equal(nearestInSegment(wave, 5, 10, 1.44), 9, 'linear scan in the unsorted segment');
  assert.equal(nearestInSegment(wave, 0, 5, 1.16), 2, 'binary search in the sorted segment');
  // a record without segments is one range
  const one = readoutSamples({wave: Float64Array.from([3, 1, 2]), y: Float32Array.from([30, 10, 20])}, 1.9);
  assert.deepEqual(one.map((r) => r.y), [20]);
});

test('readoutSamples: a segment with non-finite wavelengths uses the linear scan', () => {
  const lead = Float64Array.from([NaN, 1.0, 1.1, 1.2, 1.3]);
  assert.equal(nearestInSegment(lead, 0, 5, 1.15), 2);
  assert.deepEqual(readoutSamples({wave: lead, y: Float32Array.from([0, 1, 2, 3, 4])}, 1.15).map((r) => r.k), [2]);
  const mid = Float64Array.from([1.0, 1.1, NaN, 1.3, 1.4]);
  assert.equal(nearestInSegment(mid, 0, 5, 1.32), 3);
  const tail = Float64Array.from([1.0, 1.1, 1.2, NaN]);
  assert.equal(nearestInSegment(tail, 0, 4, 1.5), 2);
  assert.equal(nearestInSegment(Float64Array.from([NaN, NaN]), 0, 2, 1), -1);
});

// ---------------------------------------------------------------- 6. scatter: stat grids, layers

test('statGridOptions: own and context grids for the axes', () => {
  const own = {density: [
    {key: 'g1', x: 'a', y: 'b', stat: 'mean', value: 'c'},
    {key: 'g2', x: 'a', y: 'b'},
    {key: 'g3', x: 'a', y: 'z', stat: 'mean'},
  ]};
  const ctxMan = {density: [{key: 'h1', x: 'a', y: 'b', stat: 'median', value: 'd'}, {key: 'h2', x: 'a', y: 'b', stat: 'count'}]};
  const opts = statGridOptions(own, [{ds: 'bg', manifest: ctxMan}], {x: 'a', y: 'b'});
  assert.deepEqual(opts.map((o) => [o.value, o.ds]), [['g1', null], [`bg${CTX_STAT_SEP}h1`, 'bg']]);
  assert.deepEqual(statGridOptions(own, [], {x: 'a', y: 'b', xlog: true}), []);
  assert.deepEqual(parseStatValue('bg::h1', ['bg']), {ds: 'bg', key: 'h1'});
  assert.deepEqual(parseStatValue('g1', ['bg']), {ds: null, key: 'g1'});
  assert.deepEqual(parseStatValue('zz::q', ['bg']), {ds: null, key: 'zz::q'});
});

test('layer state: raster switch and overlay filter', () => {
  assert.deepEqual(layerState(undefined), {raster: true, ovl: null});
  assert.deepEqual(layerState({raster: false, ovl: ['a']}), {raster: false, ovl: ['a']});
  assert.deepEqual(withRaster({ovl: ['a']}, false), {ovl: ['a'], raster: false});
  assert.deepEqual(withRaster({raster: false}, true), {});
  const layers = [{id: 'yoo_a'}, {id: 'g23_vector'}, {id: 'my_track', name: 'My Track'}];
  assert.equal(visibleOverlays(layers, null).length, 3);
  assert.deepEqual(visibleOverlays(layers, []).map((l) => l.id), []);
  assert.deepEqual(visibleOverlays(layers, ['g23_vector', 'My Track']).map((l) => l.id), ['g23_vector', 'my_track']);
});

// ---------------------------------------------------------------- 7. gallery preset roles

test('gallery presets: role from preset.role or preset.data.role', () => {
  const presets = [
    {key: 'all-rgb', label: 'RGB', data: {facets: {grating: ['g395m']}, role: 'rgb'}},
    {key: 'all', label: 'Any', facets: {grating: ['g395m']}},
    {key: 'spec', label: 'Spectra', facets: {grating: ['g395m']}, role: 'spec'},
  ];
  assert.equal(presetRole(presets[0]), 'rgb');
  assert.equal(presetRole(presets[1]), null);
  assert.deepEqual(presetFacets(presets[0]), {grating: ['g395m']});
  assert.equal(matchPreset(presets, {grating: ['g395m']}, 'spec'), 'spec');
  assert.equal(matchPreset(presets, {grating: ['g395m']}, 'rgb'), 'all-rgb');
  assert.equal(matchPreset(presets, {grating: ['g395m']}, 'other'), 'all');
  assert.equal(matchPreset(presets, {}, 'rgb'), '');
  assert.deepEqual(presetPatch(presets[0], ['rgb', 'spec']), {facets: {grating: ['g395m']}, role: 'rgb'});
  assert.deepEqual(presetPatch(presets[0], ['spec']), {facets: {grating: ['g395m']}}, 'role not offered');
  assert.deepEqual(presetPatch(null), {facets: {}});
  assert.deepEqual(normalizeFacets({b: ['2', '1'], a: []}), {b: ['1', '2']});
});

// ---------------------------------------------------------------- 8. fluxscale

test('normalizeRecord applies meta.fluxscale to flux and error channels once', () => {
  const rec = {id: 'S', wave: Float64Array.from([1, 2]), channels: {flux: Float32Array.from([1, 2]), err: Float32Array.from([0.1, 0.2])},
    units: {flux: 'Jy'}, meta: {fluxscale: 2}};
  const n = normalizeRecord(rec);
  assert.deepEqual(Array.from(n.channels.flux), [2, 4]);
  assert.ok(Math.abs(n.channels.err[1] - 0.4) < 1e-6);
  assert.equal(n.meta.fluxscaleApplied, 2);
  assert.deepEqual(Array.from(rec.channels.flux), [1, 2], 'input untouched');
  const again = normalizeRecord(n);
  assert.deepEqual(Array.from(again.channels.flux), [2, 4], 'not applied twice');
  const json = normalizeRecord({wave: [1, 2], flux: [3, null], meta: {fluxscale: 10}});
  assert.equal(json.channels.flux[0], 30);
  assert.ok(Number.isNaN(json.channels.flux[1]));
  assert.equal(fluxScaleOf({fluxscale: 1}), null);
  assert.equal(fluxScaleOf({fluxscale: -2}), null);
  assert.equal(fluxScaleOf({fluxscale: 'x'}), null);
  assert.equal(normalizeRecord({wave: [1], flux: [5]}).channels.flux[0], 5);
});

// ---------------------------------------------------------------- 9. foreign record sets

test('foreign refs: index URL, key of a row', () => {
  assert.equal(recordIndexUrl({url: 'data/x/index.json'}), 'data/x/index.json');
  assert.equal(recordIndexUrl({base: '../other/data/', key: 'spec'}), '../other/data/spec/index.json');
  assert.equal(recordIndexUrl({base: 'https://h/p/data', key: 'spec'}), 'https://h/p/data/spec/index.json');
  assert.equal(recordIndexUrl({}), null);
  assert.equal(isForeignRef({kind: 'records', url: 'u', base: '../o/'}), true);
  assert.equal(isForeignRef({kind: 'records', url: 'u'}), false);
  assert.equal(foreignKeyOf({src: 1234}, 'src'), '1234');
  assert.equal(foreignKeyOf({src: 1.5}, 'src'), '1.5');
  assert.equal(foreignKeyOf({src: ' J1 '}, 'src'), 'J1');
  assert.equal(foreignKeyOf({src: NaN}, 'src'), null);
  assert.equal(foreignKeyOf({src: ''}, 'src'), null);
  assert.equal(foreignKeyOf({}, 'src'), null);
  assert.equal(foreignKeyOf(null, 'src'), null);
});

test('RecordStore idOf: records looked up by the id_column value', async () => {
  const base = 'https://h/other/data/spec/';
  const enc = (o) => new Uint8Array(gzipSync(Buffer.from(JSON.stringify(o))));
  const files = new Map([
    [base + 'index.json', JSON.stringify({schema: 'pk-records/1', kind: 'json', n: 2,
      entries: {F1: 'r/F1.json', F2: 'r/F2.json'}})],
    [base + 'r/F1.json', enc({id: 'F1', wave: [1, 2], flux: [3, 4]})],
    [base + 'r/F2.json', enc({id: 'F2', wave: [1, 2], flux: [5, 6]})],
  ]);
  const calls = [];
  const fetch = async (url) => {
    const u = String(url);
    calls.push(u);
    if (!files.has(u)) return new Response('missing', {status: 404});
    return new Response(files.get(u), {status: 200});
  };
  const lookups = [];
  const rows = {rowA: 'F2', rowB: 'F1', rowC: null};
  const rs = await RecordStore.open(base + 'index.json', {fetch, base: 'https://h/me/page.html',
    idOf: async (id) => { lookups.push(id); return rows[id]; }});
  assert.equal(rs.has('rowA'), true, 'unknown key counts as present');
  const a = await rs.get('rowA');
  assert.equal(a.id, 'rowA');
  assert.equal(a.key, 'F2');
  assert.deepEqual(a.flux, [5, 6]);
  assert.equal(rs.has('rowA'), true);
  assert.equal(await rs.get('rowC'), null);
  assert.equal(rs.has('rowC'), false);
  assert.equal(await rs.get('nope'), null);
  await rs.get('rowA');
  assert.deepEqual(lookups, ['rowA', 'rowC', 'nope'], 'keys are cached');
  assert.ok(calls.every((u) => u.startsWith(base)), 'every fetch under the foreign base');
  // a superseded token resolves to null after the key lookup
  const p1 = rs.get('rowB', {token: 1});
  const p2 = rs.get('rowA', {token: 2});
  assert.equal(await p1, null);
  assert.equal((await p2).key, 'F2');
});

test('RecordStore.hasRecord: exact for id_column rows without a foreign record', async () => {
  const idx = {schema: 'pk-records/1', kind: 'json', n: 1, entries: {F1: 'r/F1.json'}};
  const rows = {rowA: null, rowB: 'NOPE', rowC: 'F1'};
  const rs = new RecordStore(idx, 'https://h/other/data/spec/index.json', {base: 'https://h/me/page.html',
    idOf: async (id) => rows[id]});
  assert.equal(rs.has('rowA'), true, 'sync has() stays optimistic before the lookup');
  assert.equal(await rs.hasRecord('rowA'), false, 'no id_column value');
  assert.equal(await rs.hasRecord('rowB'), false, 'value missing from the foreign index');
  assert.equal(await rs.hasRecord('rowC'), true);
  assert.equal(rs.has('rowB'), false, 'the lookup is cached for has()');
  // spectrum1d's check goes through hasRecord, so such a row reports 'No spectrum'
  assert.equal(await hasRecordOf(rs, 'rowA'), false);
  assert.equal(await hasRecordOf(rs, 'rowC'), true);
  const plain = new RecordStore({kind: 'json', n: 1, entries: {a: 'r/a.json'}}, 'https://h/me/data/s/index.json', {base: 'https://h/me/p.html'});
  assert.equal(await plain.hasRecord('a'), true);
  assert.equal(await plain.hasRecord('b'), false);
  assert.equal(await hasRecordOf({has: (id) => id === 'x'}, 'x'), true, 'stores without hasRecord use has()');
  assert.equal(await hasRecordOf(null, 'x'), false);
});

test('foreign PNG-mode sidecars resolve in the foreign portal', async () => {
  const idxUrl = 'https://h/w51/portal/unified/data/s2d/index.json';
  assert.equal(foreignPageBase(idxUrl), 'https://h/w51/portal/unified/');
  assert.equal(foreignPageBase(new URL('../../wd2/unified/data/spec/index.json', 'https://h/jwst/a/unified/p.html')),
    'https://h/jwst/wd2/unified/');
  const idx = {kind: 'pki1', n: 2, entries: {A: {png: '{assets}s2d/A.png'}, B: {png: 'pngs/B.png'}}};
  const rs = new RecordStore(idx, idxUrl, {base: foreignPageBase(idxUrl), page: {assets: './assets/'},
    idOf: async (id) => id});
  assert.equal((await rs.get('A')).png, 'https://h/w51/portal/unified/assets/s2d/A.png');
  assert.equal((await rs.get('B')).png, 'https://h/w51/portal/unified/pngs/B.png');
});
