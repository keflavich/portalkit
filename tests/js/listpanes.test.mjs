// Unit tests for the list / gallery / detail / landing helpers changed in WPI4
// (IMPLEMENTATION_NOTES 74, 76, 81, 85, 93), the startup reads of the list, hover card and
// sky markers (note 99), and the panes' first-paint promises and the list's deferred column
// reads (note 112). Pure functions, plus list mount / hover install / SkyMarkers on a
// minimal fake DOM and fake DataClient (no jsdom).
import test from 'node:test';
import assert from 'node:assert/strict';

import {itemUrl, productUrl, substituteRefs} from '../../web/js/ui/lightbox.js';
import {
  asOrder, ID_SORT, keepMask, normalizeSortSpec, orderKey, parseSort, subsetInOrder,
  afterFirstPaint, cellText, levelIndex, LIST_TIMING, mount as mountList, ROW_BLOCK, ROW_CACHE_ROWS, RowCache, rowBlocks,
  payloadLevel, rowId, rowsCarry, rowsIdKey,
} from '../../web/js/panes/list.js';
import {HOVER_CHANNEL, install as installHover, recordColumns, recordExact, recordId, recordValueText} from '../../web/js/ui/hover.js';
import {afterFirstPaint as markersAfterFirstPaint, SkyMarkers} from '../../web/js/sky/markers.js';
import {createStore} from '../../web/js/core/store.js';
import {MISSING} from '../../web/js/core/fmt.js';
import {annotateOptions, catalogSortKey, mount as mountGallery, orderByRank, rankOf} from '../../web/js/panes/gallery.js';
import {mount as mountHist} from '../../web/js/panes/hist.js';
import {mount as mountScatter} from '../../web/js/panes/scatter.js';
import {densityCacheStats} from '../../web/js/plot/canvas2d.js';
import {mount as mountManifold} from '../../web/js/panes/manifold.js';
import {COUNT_NOUNS, countEntries, countLabel, mount as mountLanding, searchManifests} from '../../web/js/panes/landing.js';
import {setFetch} from '../../web/js/data/fetch.js';
import {importResultText} from '../../web/js/ui/export.js';
import * as legend from '../../web/js/ui/legend.js';
import {mount as mountTable} from '../../web/js/panes/table.js';
import {mount as mountDetail, propertyText, quantizedKey} from '../../web/js/panes/detail.js';
import {mount as mountSky} from '../../web/js/panes/sky.js';
import {mount as mountWavelength} from '../../web/js/panes/wavelength.js';
import {mount as mountSpectrum2d} from '../../web/js/panes/spectrum2d.js';
import * as aladin from '../../web/js/sky/aladin.js';

const PAGE = 'https://example.org/portal/brick/index.html';

// ------------------------------------------------------------------ note 81: URLs

test('substituteRefs: {assets} and {res}, missing page keys left alone', () => {
  const page = {assets: '_pk/v1.0.0', res: 'https://cdn.example.org/res'};
  assert.equal(substituteRefs('{assets}/img/a.png', page), '_pk/v1.0.0/img/a.png');
  assert.equal(substituteRefs('{res}/x/{res}.png', page), 'https://cdn.example.org/res/x/https://cdn.example.org/res.png');
  assert.equal(substituteRefs('{res}/a.png', {}), '{res}/a.png');
  assert.equal(substituteRefs('{res}/a.png', null), '{res}/a.png');
  assert.equal(substituteRefs('img/a.png', {res: 5}), 'img/a.png');
});

test('itemUrl: page-relative, no doubled prefix, absolute and data URLs kept', () => {
  assert.equal(itemUrl('img/sed/s1.png', null, PAGE), 'https://example.org/portal/brick/img/sed/s1.png');
  assert.equal(itemUrl('./img/x.png', null, PAGE), 'https://example.org/portal/brick/img/x.png');
  assert.equal(itemUrl('../shared/x.png', null, PAGE), 'https://example.org/portal/shared/x.png');
  assert.equal(itemUrl('/abs/x.png', null, PAGE), 'https://example.org/abs/x.png');
  assert.equal(itemUrl('https://other.org/a.png', null, PAGE), 'https://other.org/a.png');
  assert.equal(itemUrl('data:image/png;base64,AAAA', null, PAGE), 'data:image/png;base64,AAAA');
  // resolving an already-resolved URL is idempotent (no img/img/)
  const once = itemUrl('img/x.png', null, PAGE);
  assert.equal(itemUrl(once, null, PAGE), once);
  assert.ok(!once.includes('img/img/'));
  // {assets} prefixes resolve once against the page as well
  assert.equal(itemUrl('{assets}/img/x.png', {assets: '_pk/v1'}, PAGE), 'https://example.org/portal/brick/_pk/v1/img/x.png');
});

test('itemUrl: {res}, whitespace, unicode, empty and invalid input', () => {
  assert.equal(itemUrl('{res}/a.png', {res: 'https://cdn.example.org/r'}, PAGE), 'https://cdn.example.org/r/a.png');
  assert.equal(itemUrl('  img/a.png  ', null, PAGE), 'https://example.org/portal/brick/img/a.png');
  assert.equal(itemUrl('img/Sgr B2 β.png', null, PAGE), 'https://example.org/portal/brick/img/Sgr%20B2%20%CE%B2.png');
  for (const bad of ['', '   ', null, undefined, 5, {}]) assert.equal(itemUrl(bad, null, PAGE), null);
  // no base and a relative URL cannot resolve
  assert.equal(itemUrl('img/a.png', null, 'not a url'), null);
});

test('productUrl: relative to the records index URL, page-relative index, fallback to the page', () => {
  assert.equal(productUrl('s2d/a.fits', 'data/rec/index.json', null, PAGE),
    'https://example.org/portal/brick/data/rec/s2d/a.fits');
  assert.equal(productUrl('s2d/a.fits', 'https://data.example.org/rec/index.json', null, PAGE),
    'https://data.example.org/rec/s2d/a.fits');
  assert.equal(productUrl('{res}/a.fits', 'data/rec/index.json', {res: 'https://cdn.example.org'}, PAGE),
    'https://cdn.example.org/a.fits');
  assert.equal(productUrl('/top/a.fits', 'data/rec/index.json', null, PAGE), 'https://example.org/top/a.fits');
  assert.equal(productUrl('a.fits', null, null, PAGE), 'https://example.org/portal/brick/a.fits');
  assert.equal(productUrl('a.fits', '', null, PAGE), 'https://example.org/portal/brick/a.fits');
  // page-relative {res}/{assets} (vendored './res/') resolve against the page, like itemUrl
  const vend = {res: './res/', assets: '_pk/v1/'};
  assert.equal(productUrl('{res}filters.json', 'data/products/index.json', vend, PAGE),
    itemUrl('{res}filters.json', vend, PAGE));
  assert.equal(productUrl('{res}filters.json', 'data/products/index.json', vend, PAGE),
    'https://example.org/portal/brick/res/filters.json');
  assert.equal(productUrl('{assets}x.png', 'data/products/index.json', vend, PAGE),
    'https://example.org/portal/brick/_pk/v1/x.png');
  // an unsubstituted placeholder (page lacks res) stays a plain path under the index
  assert.equal(productUrl('{res}a.fits', 'data/rec/index.json', null, PAGE),
    'https://example.org/portal/brick/data/rec/%7Bres%7Da.fits');
  assert.equal(productUrl('', 'data/rec/index.json', null, PAGE), null);
  assert.equal(productUrl(null, 'data/rec/index.json', null, PAGE), null);
});

// ------------------------------------------------------------------ note 74: list order

test('normalizeSortSpec / orderKey: id maps to the id sort unless the catalog has an id column', () => {
  assert.deepEqual(normalizeSortSpec(parseSort('id')), {key: ID_SORT, dir: 1});
  assert.deepEqual(normalizeSortSpec(parseSort('-id')), {key: ID_SORT, dir: -1});
  assert.deepEqual(normalizeSortSpec(parseSort('id'), (k) => k === 'id'), {key: 'id', dir: 1});
  assert.deepEqual(normalizeSortSpec({key: '__id', dir: -1}, () => true), {key: ID_SORT, dir: -1});
  assert.deepEqual(normalizeSortSpec(parseSort('-f410m')), {key: 'f410m', dir: -1});
  assert.equal(normalizeSortSpec(null), null);
  assert.equal(normalizeSortSpec({key: ''}), null);
  assert.equal(orderKey(ID_SORT), '__id');
  assert.equal(orderKey('id'), 'id');   // a stored column called id keeps its key
  assert.equal(orderKey('f410m'), 'f410m');
});

// Local (worker:false) DataClient over an in-memory catalog with ids a..d and an f32 column
// called `id` holding [3, 1, 4, 2].
async function idColumnClient() {
  const {gzipSync} = await import('node:zlib');
  const {DataClient} = await import('../../web/js/data/client.js');
  const gz = (u8) => new Uint8Array(gzipSync(u8));
  const strPayload = (list) => {
    const te = new TextEncoder();
    const parts = list.map((x) => te.encode(x));
    const total = parts.reduce((a, q) => a + q.length, 0);
    const out = new Uint8Array(4 + 4 * (list.length + 1) + total);
    const dv = new DataView(out.buffer);
    dv.setUint32(0, list.length, true);
    let off = 0;
    parts.forEach((q, i) => { dv.setUint32(4 + 4 * i, off, true); off += q.length; });
    dv.setUint32(4 + 4 * list.length, off, true);
    let at = 4 + 4 * (list.length + 1);
    for (const q of parts) { out.set(q, at); at += q.length; }
    return out;
  };
  const B = 'https://x.test/d/';
  const man = {schema: 'pk-catalog/1', id: 'src', n: 4, order: {kind: 'input'}, chunk_rows: 4,
    ids: {file: 'ids.bin', enc: 'str'},
    cols: [{key: 'id', role: 'value', enc: 'f32', shuffle: false, file: 'cols/id.bin'}], virtual: []};
  const files = new Map([
    [B + 'manifest.json', JSON.stringify(man)],
    [B + 'ids.bin', gz(strPayload(['d', 'b', 'a', 'c']))],
    [B + 'cols/id.bin', gz(new Uint8Array(Float32Array.from([3, 1, 4, 2]).buffer))],
  ]);
  const fetch = async (u) => (files.has(String(u)) ? new Response(files.get(String(u))) : new Response('x', {status: 404}));
  return DataClient.open(B + 'manifest.json', {worker: false, fetch});
}

test('list id sort through a local DataClient: ID_SORT orders by catalog id', async () => {
  const c = await idColumnClient();
  try {
    const o = await c.order(orderKey(ID_SORT), 1);
    assert.deepEqual(Array.from(o), [2, 1, 3, 0]);   // ids a, b, c, d
    const d = await c.order(orderKey(ID_SORT), -1);
    assert.deepEqual(Array.from(d), [0, 3, 1, 2]);
  } finally {
    if (c.terminate) c.terminate();
  }
});

test('list sort on a stored column called id orders by its values', {
  todo: 'needs the worker (WPI1) to sort a stored column `id` before treating "id" as the id alias',
}, async () => {
  const c = await idColumnClient();
  try {
    const spec = normalizeSortSpec(parseSort('id'), (k) => !!c.col(k));
    assert.deepEqual(spec, {key: 'id', dir: 1});
    assert.deepEqual(Array.from(await c.order(orderKey(spec.key), 1)), [1, 3, 0, 2]);
  } finally {
    if (c.terminate) c.terminate();
  }
});

test('asOrder: Int32Array passthrough, arrays and wrappers converted, bad input throws', () => {
  const a = Int32Array.from([2, 0, 1]);
  assert.equal(asOrder(a), a);
  assert.deepEqual(Array.from(asOrder([2, 0, 1])), [2, 0, 1]);
  assert.deepEqual(Array.from(asOrder(Uint32Array.from([1, 0]))), [1, 0]);
  assert.deepEqual(Array.from(asOrder({order: [1, 0]})), [1, 0]);
  assert.deepEqual(Array.from(asOrder({idx: Int32Array.from([0])})), [0]);
  assert.deepEqual(Array.from(asOrder([])), []);
  for (const bad of [null, undefined, 5, {}, {order: null}]) assert.throws(() => asOrder(bad), TypeError);
});

test('keepMask: null without restrictions; search hits, vis and selection combine', () => {
  assert.equal(keepMask(5), null);
  assert.equal(keepMask(0, {}), null);
  const vis = Uint8Array.from([1, 1, 0, 1, 1]);
  const sel = Uint8Array.from([0, 1, 1, 1, 0]);
  assert.deepEqual(Array.from(keepMask(5, {vis})), [1, 1, 0, 1, 1]);
  assert.deepEqual(Array.from(keepMask(5, {vis, selMask: sel})), [0, 1, 0, 1, 0]);
  assert.deepEqual(Array.from(keepMask(5, {found: Int32Array.from([4, 2, 0, 99, -1])})), [1, 0, 1, 0, 1]);
  assert.deepEqual(Array.from(keepMask(5, {found: [4, 2, 0], vis})), [1, 0, 0, 0, 1]);
  assert.deepEqual(Array.from(keepMask(5, {found: []})), [0, 0, 0, 0, 0]);
});

test('subsetInOrder: keeps permutation order, drops out-of-range rows, N=0 and N=1', () => {
  const full = Int32Array.from([3, 0, 4, 1, 2]);
  assert.equal(subsetInOrder(full, null), full);
  assert.deepEqual(Array.from(subsetInOrder(full, Uint8Array.from([1, 0, 1, 1, 0]))), [3, 0, 2]);
  assert.deepEqual(Array.from(subsetInOrder(full, new Uint8Array(5))), []);
  assert.deepEqual(Array.from(subsetInOrder(Int32Array.from([7, -1, 1]), Uint8Array.from([1, 1]))), [1]);
  assert.deepEqual(Array.from(subsetInOrder(new Int32Array(0), new Uint8Array(0))), []);
  assert.deepEqual(Array.from(subsetInOrder(Int32Array.from([0]), Uint8Array.from([1]))), [0]);
});

test('subsetInOrder: 1M-row permutation with a sparse mask stays ordered and linear', () => {
  const n = 1000000;
  const full = new Int32Array(n);
  for (let p = 0; p < n; p++) full[p] = n - 1 - p;
  const keep = new Uint8Array(n);
  for (let i = 0; i < n; i += 1000) keep[i] = 1;
  const t0 = Date.now();
  const out = subsetInOrder(full, keep);
  assert.ok(Date.now() - t0 < 2000);
  assert.equal(out.length, 1000);
  for (let k = 1; k < out.length; k++) assert.ok(out[k] < out[k - 1]);
});

// ------------------------------------------------------------------ note 74: gallery order

test('rankOf: rank per row, -1 for unlisted, out-of-range and duplicate entries ignored', () => {
  assert.deepEqual(Array.from(rankOf(Int32Array.from([2, 0, 1]), 3)), [1, 2, 0]);
  assert.deepEqual(Array.from(rankOf([5, 1, -1, 1], 3)), [-1, 1, -1]);
  assert.deepEqual(Array.from(rankOf(null, 2)), [-1, -1]);
  assert.deepEqual(Array.from(rankOf([0], 0)), []);
  assert.deepEqual(Array.from(rankOf([0], NaN)), []);
});

test('orderByRank: ranked items in rank order, rowless/unranked items stable at the end', () => {
  const items = [{row: 2}, {row: null}, {row: 0}, {row: 1}, {}, {row: 9}];
  const rank = rankOf(Int32Array.from([1, 2, 0]), 3);   // row 1 first, then 2, then 0
  assert.deepEqual(orderByRank([0, 1, 2, 3, 4, 5], items, rank), [3, 0, 2, 1, 4, 5]);
  assert.deepEqual(orderByRank([4, 1], items, rank), [4, 1]);
  assert.deepEqual(orderByRank([], items, rank), []);
  assert.deepEqual(orderByRank([2, 0], items, null), [2, 0]);
  // two items sharing a catalog row keep their index order
  assert.deepEqual(orderByRank([1, 0], [{row: 0}, {row: 0}], rankOf([0], 1)), [0, 1]);
});

test('catalogSortKey: worker sort for id and catalog columns only when joined', () => {
  const col = (k) => (k === 'f410m' || k === 'Target' ? {key: k} : null);
  const tagKeys = new Set(['Target', 'grating']);
  const ctx = {joined: true, col, tagKeys};
  assert.equal(catalogSortKey('id', ctx), '__id');
  assert.equal(catalogSortKey('__id', ctx), '__id');
  assert.equal(catalogSortKey('f410m', ctx), 'f410m');
  assert.equal(catalogSortKey('Target', ctx), null);    // tag keys sort locally
  assert.equal(catalogSortKey('grating', ctx), null);
  assert.equal(catalogSortKey('caption', ctx), null);
  assert.equal(catalogSortKey('nope', ctx), null);
  assert.equal(catalogSortKey('id', {joined: false, col}), null);
  assert.equal(catalogSortKey('', ctx), null);
  assert.equal(catalogSortKey('f410m', {joined: true}), null);
});

test('annotateOptions: true means defaults, objects pass through, others disable', () => {
  assert.deepEqual(annotateOptions(true), {});
  const o = {codes: 'yn'};
  assert.equal(annotateOptions(o), o);
  for (const v of [false, null, undefined, 'yes', 1, ['y']]) assert.equal(annotateOptions(v), null);
});

// ------------------------------------------------------------------ note 76: shared legend helpers

test('legend.js provides the isCategory / plotLevelColors helpers list and gallery import', () => {
  assert.equal(typeof legend.isCategory, 'function');
  assert.equal(typeof legend.levelColors, 'function');
  assert.equal(typeof legend.plotLevelColors, 'function');
  const meta = {key: 'cls', role: 'category', enc: 'cat8', levels: ['star', 'YSO']};
  assert.equal(legend.isCategory(meta), true);
  assert.equal(legend.isCategory({key: 'x', enc: 'f32'}), false);
  const cols = legend.levelColors(meta, null);
  assert.equal(cols.length, 2);
  for (const c of cols) assert.equal(typeof c, 'string');
});

test('list / gallery colour with plotLevelColors, matching the scatter for 8+ levels', async () => {
  const fs = await import('node:fs');
  for (const f of ['list.js', 'gallery.js']) {
    const src = fs.readFileSync(new URL(`../../web/js/panes/${f}`, import.meta.url), 'utf8');
    assert.match(src, /colorPal = plotLevelColors\(/, f);
    assert.doesNotMatch(src, /colorPal = levelColors\(/, f);
  }
  const meta = {key: 'cls', role: 'category', enc: 'cat8', levels: 'abcdefghi'.split('')};
  const pc = legend.plotLevelColors(meta, null);
  assert.equal(pc.length, 9);
  assert.deepEqual(pc.slice(0, 7), legend.levelColors(meta, null).slice(0, 7));
  assert.equal(pc[7], pc[8]);   // neutral from GPU_LEVELS on, as the scatter draws them
});

// ------------------------------------------------------------------ note 85: landing search

test('searchManifests: one group per resolved manifest URL, entries without page skipped', () => {
  const base = 'https://example.org/portal/portal.json';
  const g = searchManifests([
    {page: 'a', ds: 'cat', manifest: 'data/cat/manifest.json'},
    {page: 'b', ds: 'cat', manifest: './data/cat/manifest.json'},
    {page: 'c', ds: 'other', manifest: 'data/other/manifest.json'},
    {ds: 'nopage', manifest: 'data/x/manifest.json'},
    {page: 'd', ds: 'nomanifest'},
    null, 5,
  ], base);
  assert.equal(g.length, 2);
  assert.equal(g[0].url, 'https://example.org/portal/data/cat/manifest.json');
  assert.deepEqual(g[0].targets.map((s) => s.page), ['a', 'b']);
  assert.equal(g[1].url, 'https://example.org/portal/data/other/manifest.json');
  assert.deepEqual(searchManifests(null, base), []);
  assert.deepEqual(searchManifests([], base), []);
  // unresolvable relative URL without a base is skipped
  assert.deepEqual(searchManifests([{page: 'a', manifest: 'm.json'}], undefined), []);
  assert.equal(searchManifests([{page: 'a', manifest: 'https://x.org/m.json'}], undefined)[0].url, 'https://x.org/m.json');
});

test('countEntries: valid portal.json counts[] grouped by page, nouns from the kind', () => {
  const g = countEntries([
    {page: 'cat', ds: 'src', kind: 'catalog', n: 1234},
    {page: 'cat', ds: 'src2', kind: 'catalog', n: 1},
    {page: 'img', ds: 'cut', kind: 'images', n: 1},
    {page: 'spec', ds: 'sp', kind: 'records', n: 20},
    {page: 'odd', ds: 'x', kind: 'mystery', n: 3},
    {page: 'u1', ds: 'a', kind: 'catalog', n: 2, unit: 'field'},
    {page: 'u2', ds: 'a', kind: 'catalog', n: 2, units: 'Sgr B2 cores'},
    {page: 'u3', ds: 'a', kind: 'records', n: 2, unit: ' ', units: ''},
    // invalid entries: dropped
    {page: 'bad', kind: 'catalog', n: -1}, {page: 'bad', kind: 'catalog', n: 1.5},
    {page: 'bad', kind: 'catalog', n: NaN}, {page: 'bad', kind: 'catalog', n: '7'},
    {page: '', kind: 'catalog', n: 3}, {kind: 'catalog', n: 3}, null, 4, 'x',
  ]);
  assert.deepEqual(Array.from(g.keys()), ['cat', 'img', 'spec', 'odd', 'u1', 'u2', 'u3']);
  assert.deepEqual(g.get('cat').map((e) => [e.ds, e.n, e.unit, e.units]), [['src', 1234, 'source', 'sources'], ['src2', 1, 'source', 'sources']]);
  assert.deepEqual([g.get('img')[0].unit, g.get('img')[0].units], COUNT_NOUNS.images);
  assert.deepEqual([g.get('spec')[0].unit, g.get('spec')[0].units], ['spectrum', 'spectra']);
  assert.deepEqual([g.get('odd')[0].unit, g.get('odd')[0].units], ['item', 'items']);
  assert.deepEqual([g.get('u1')[0].unit, g.get('u1')[0].units], ['field', 'fields']);
  assert.deepEqual([g.get('u2')[0].unit, g.get('u2')[0].units], ['Sgr B2 cores', 'Sgr B2 cores']);
  assert.deepEqual([g.get('u3')[0].unit, g.get('u3')[0].units], ['spectrum', 'spectra'], 'blank nouns: kind default');
  assert.equal(countEntries(null).size, 0);
  assert.equal(countEntries({page: 'a', n: 1}).size, 0);
});

test('countLabel: singular at n == 1, same nouns summed, different nouns listed', () => {
  const one = (n, unit = 'source', units = 'sources', ds = 'a') => ({ds, n, unit, units});
  assert.deepEqual(countLabel([one(0)]), {text: '0 sources', title: 'a: 0 sources'});
  assert.equal(countLabel([one(1)]).text, '1 source');
  assert.equal(countLabel([one(1), one(1, 'source', 'sources', 'b')]).text, '2 sources');
  assert.equal(countLabel([one(1234), one(20, 'spectrum', 'spectra', 's'), one(5, 'source', 'sources', 'c')]).text,
    '1,239 sources · 20 spectra');
  assert.equal(countLabel([one(1234), one(20, 'spectrum', 'spectra', 's')]).title, 'a: 1,234 sources\ns: 20 spectra');
  assert.equal(countLabel([{ds: 'm', title: 'Main catalog', n: 3, unit: 'source', units: 'sources'}]).title, 'Main catalog: 3 sources');
  assert.equal(countLabel([one(1, 'Hα knot', 'Hα knots')]).text, '1 Hα knot');
  assert.equal(countLabel([one(-1), one(NaN), null]).text, '');
  assert.deepEqual(countLabel([]), {text: '', title: ''});
  assert.deepEqual(countLabel(null), {text: '', title: ''});
});

// ------------------------------------------------------------------ note 93: import dialog

test('importResultText: app.importCsv result and resolveImport result', () => {
  assert.deepEqual(importResultText({summary: 'Matched 3 of 3 entries', missing: 0, skipped: 0}),
    {text: 'Matched 3 of 3 entries', level: 'info'});
  const w = importResultText({summary: 'Matched 1 of 2 entries; not found: X', missing: 1, skipped: 2});
  assert.equal(w.level, 'warn');
  assert.match(w.text, /2 unreadable lines$/);
  assert.match(importResultText({matched: 4, entries: 5, missing: 1}).text, /^Matched 4 of 5/);
  const res = {matched: [0, 1], missing: [{id: 'Sgr B2 β', line: 3}], indices: Int32Array.from([4, 7]), byId: 2, byPosition: 0};
  const r = importResultText({res, total: 3, skipped: 1});
  assert.equal(r.level, 'warn');
  assert.match(r.text, /Matched 2 of 3 entries/);
  assert.match(r.text, /Sgr B2 β/);
  assert.match(r.text, /1 unreadable line$/);
  assert.deepEqual(importResultText(null), {text: '', level: 'info'});
});

// ------------------------------------------------------------------ note 99: startup reads

test('rowId / recordId: _id, quantized id, else the row-chunk id key, as strings', () => {
  for (const f of [rowId, recordId]) {
    assert.equal(f({_id: 'a', id: 'b'}), 'a');
    assert.equal(f({id: 7, _quantized: true}), '7');
    assert.equal(f({_id: null, id: 'x', _quantized: true}), 'x');
    assert.equal(f({id: 'x'}, 'id'), 'x');
    // row chunk keyed by the catalog id key; a stored column `id` is never taken for the id
    assert.equal(f({source_id: 'J1745-2900', ra: 266.4, _i: 0}, 'source_id'), 'J1745-2900');
    assert.equal(f({name: 'N2', id: 1002, _i: 2}, 'name'), 'N2');
    assert.equal(f({id: 1002, _i: 2}), null, 'no id key: a bare id is a column value');
    assert.equal(f({}), null);
    assert.equal(f(null), null);
  }
  assert.equal(rowsIdKey({rows: {cols: ['source_id', 'f410m']}}), 'source_id');
  assert.equal(rowsIdKey({rows: null}), null);
  assert.equal(rowsIdKey(null), null);
});

test('cellText: payload labels and column values read the same', () => {
  const cat = {key: 'cls', role: 'category', levels: ['star', 'yso']};
  assert.equal(cellText(cat, 'yso', {payload: true}).text, 'yso');
  assert.equal(cellText(cat, 1).text, 'yso', 'column value: level index');
  assert.equal(cellText(cat, NaN).text, MISSING);
  assert.equal(cellText(cat, null, {payload: true}).text, MISSING);
  // integer-labelled category (class codes): the payload holds the label, the column the index
  const codes = {key: 'k', role: 'category', levels: ['1', '2', '3']};
  assert.equal(cellText(codes, 1, {payload: true}).text, '1');
  assert.equal(cellText(codes, 2, {payload: true}).text, '2');
  assert.equal(cellText(codes, 0).text, '1');
  assert.equal(cellText(codes, 1).text, '2');
  const flag = {key: 'good', role: 'flag'};
  assert.equal(cellText(flag, true).text, 'yes');
  assert.equal(cellText(flag, 0).text, 'no');
  assert.equal(cellText(flag, 1, {payload: true}).text, 'yes');
  assert.equal(cellText(flag, NaN).text, MISSING);
  const mag = {key: 'f115w', role: 'mag'};
  assert.equal(cellText(mag, undefined).text, MISSING);
  assert.match(cellText(mag, 12.5).text, /^12\.5/);
  assert.match(cellText(mag, 12.5, {payload: true}).text, /^12\.5/);
});

test('RowCache: put by _i, LRU cap, missing ranges', () => {
  const c = new RowCache(4);
  c.put([{_i: 10, id: 'a'}, {_i: 11, id: 'b'}]);
  assert.equal(c.get(10).id, 'a');
  assert.deepEqual(c.missing(10, 12), null);
  assert.deepEqual(c.missing(9, 13), [9, 13]);
  assert.deepEqual(c.missing(11, 14), [12, 14]);
  c.put([{id: 'x'}, {id: 'y'}, {id: 'z'}], 20);   // no _i: numbered from i0
  assert.equal(c.get(22).id, 'z');
  assert.equal(c.size, 4);
  assert.equal(c.has(10), false, 'oldest row evicted');
  c.put(null);
  c.clear();
  assert.equal(c.size, 0);
});

test('rowBlocks: aligned blocks clipped to [0, n)', () => {
  assert.deepEqual(rowBlocks(0, 10, 1000), [[0, ROW_BLOCK]]);
  assert.deepEqual(rowBlocks(120, 140, 1000, 128), [[0, 128], [128, 256]]);
  assert.deepEqual(rowBlocks(990, 1010, 1000, 128), [[896, 1000]]);
  assert.deepEqual(rowBlocks(5, 5, 1000), []);
  assert.deepEqual(rowBlocks(0, 3, 0), []);
});

test('afterFirstPaint: app signal, else resolved at once', async () => {
  for (const f of [afterFirstPaint, markersAfterFirstPaint]) {
    let done = false;
    let release;
    const app = {afterFirstPaint: () => new Promise((r) => { release = r; })};
    const p = f(app).then(() => { done = true; });
    await tick();
    assert.equal(done, false);
    release();
    await p;
    assert.equal(done, true);
    await f({});
    await f(null);
  }
});

test('hover record helpers: carried columns only, exact numbers, labels', () => {
  const cols = [{key: 'f115w', role: 'mag'}, {key: 'cls', role: 'category', levels: ['a', 'b']}, {key: 'z', role: 'value'}];
  assert.deepEqual(recordColumns(cols, {f115w: 1, cls: 'a', _id: 's'}).map((c) => c.key), ['f115w', 'cls']);
  assert.deepEqual(recordColumns(cols, null), []);
  assert.equal(recordValueText(null, cols[0]).text, MISSING);
  assert.equal(recordValueText(NaN, cols[0]).text, MISSING);
  assert.equal(recordValueText('b', cols[1]).text, 'b');
  // records carry labels, never level indices: an integer label prints as itself
  assert.equal(recordValueText(1, cols[1]).text, '1');
  assert.equal(recordValueText(2, {role: 'category', levels: ['1', '2', '3']}, true).text, '2');
  assert.equal(recordValueText(0, {role: 'flag'}).text, 'no');
  assert.equal(recordValueText(true, {role: 'flag'}).text, 'yes');
  assert.match(recordValueText(12.34567, cols[0], true).text, /^12\.3/);
  assert.equal(recordExact({f115w: 1}, 'f115w'), true);
  assert.equal(recordExact({f115w: 1, _quantized: true}, 'f115w'), false);
  assert.equal(recordExact({f115w: 1, f200w: 2, _qkeys: ['f200w']}, 'f200w'), false);
  assert.equal(recordExact({f115w: 1, f200w: 2, _qkeys: ['f200w']}, 'f115w'), true);
  assert.equal(recordExact({f200w: 2, _qkeys: {f200w: true}}, 'f200w'), false);
  assert.equal(recordExact(null, 'x'), false);
});

// ---- minimal DOM + fake catalog client

function tick(ms = 0) {
  return new Promise((r) => setTimeout(r, ms));
}

async function settle(n = 6) {
  for (let k = 0; k < n; k++) await tick();
}

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
    this.parent = null;
    this.title = '';
    this.id = '';
    this.scrollTop = 0;
    this.clientHeight = 240;
    this.style = {props: {}, setProperty(k, v) { this.props[k] = v; }};
    const self = this;
    this.classList = {
      add(c) { this.toggle(c, true); },
      remove(c) { this.toggle(c, false); },
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
  get firstChild() { return this.children[0] || null; }
  /** A <select>'s <option> children, for code (e.g. wavelength.js's speed <select>) that reads
   * `sel.options` directly rather than querying the DOM. */
  get options() { return this.children.filter((c) => c.tagName === 'OPTION'); }
  get selected() { return !!this._selected; }
  /** Setting an <option>'s `selected = true` (as the DOM does on build) mirrors its value onto
   * the parent <select>, matching a real <select>'s value-follows-selected-option behaviour. */
  set selected(v) {
    this._selected = v;
    if (v && this.parent && this.parent.tagName === 'SELECT') this.parent.value = this.value;
  }
  setAttribute(k, v) {
    this.attrs[k] = String(v);
    // the DOM mirrors the `value` attribute onto the `.value` property at creation time (e.g.
    // h(tag, cls, {value}) building an <option> before it is selected/appended).
    if (k === 'value') this.value = String(v);
  }
  getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k); }
  append(...cs) {
    for (const c of cs) {
      if (c && typeof c === 'object') {
        c.parent = this;
        this.children.push(c);
        // an <option selected> appended before (or after) its parent <select> mirrors its value,
        // matching a real <select>'s value-follows-selected-option behaviour.
        if (c.tagName === 'OPTION' && c._selected && this.tagName === 'SELECT') this.value = c.value;
      }
    }
  }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  removeEventListener(t, fn) { if (this.listeners[t]) this.listeners[t] = this.listeners[t].filter((f) => f !== fn); }
  fire(t, ev = {}) { for (const fn of this.listeners[t] || []) fn({type: t, target: this, ...ev}); }
  contains(e) { return e === this || this.all().includes(e); }
  closest(sel) {
    const m = /^\.([\w-]+)$/.exec(sel);
    for (let e = this; e; e = e.parent) if (m && e.classList.contains(m[1])) return e;
    return null;
  }
  getBoundingClientRect() { return {width: 100, height: 40, left: 0, top: 0}; }
  get parentNode() { return this.parent; }
  appendChild(c) { if (c.parent) c.remove(); this.append(c); return c; }
  removeChild(c) { c.remove(); return c; }
  prepend(...cs) { for (const c of cs.reverse()) { if (c && typeof c === 'object') { c.parent = this; this.children.unshift(c); } } }
  insertBefore(c, ref) { const k = this.children.indexOf(ref); c.parent = this; this.children.splice(k < 0 ? this.children.length : k, 0, c); return c; }
  replaceChildren(...cs) { this.children = []; this._text = ''; this.append(...cs); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  get childNodes() { return this.children; }
  get lastChild() { return this.children[this.children.length - 1] || null; }
  getContext() {
    const nop = () => {};
    const base = {save: nop, restore: nop, setTransform: nop, clearRect: nop, fillRect: nop, beginPath: nop, arc: nop,
      fill: nop, stroke: nop, moveTo: nop, lineTo: nop, closePath: nop, rect: nop, scale: nop, translate: nop,
      createImageData: (w, h) => ({data: new Uint8ClampedArray(w * h * 4), width: w, height: h}), putImageData: nop,
      drawImage: nop, fillText: nop, measureText: () => ({width: 0})};
    // any other 2-D context method is a no-op
    return new Proxy(base, {get: (t, k) => (k in t ? t[k] : (typeof k === 'string' ? nop : undefined))});
  }
  all() { return this.children.flatMap((c) => [c, ...c.all()]); }
  querySelectorAll(sel) {
    // `.cls` or `.cls[data-key]` (the landing's card selector)
    const m = /^\.([\w-]+)(?:\[data-([\w-]+)\])?$/.exec(sel);
    if (!m) return [];
    const dk = m[2] ? m[2].replace(/-([a-z])/g, (_, c) => c.toUpperCase()) : null;
    return this.all().filter((e) => e.classList.contains(m[1]) && (!dk || e.dataset[dk] !== undefined));
  }
}

async function withDom(fn) {
  const saved = {};
  for (const k of ['document', 'window', 'requestAnimationFrame', 'cancelAnimationFrame']) saved[k] = globalThis[k];
  const body = new FakeEl('body');
  const docEl = new FakeEl('html');
  globalThis.document = {createElement: (t) => new FakeEl(t), activeElement: null, body, documentElement: docEl,
    createTextNode: (t) => ({textContent: t}), createDocumentFragment: () => new FakeEl('#fragment'),
    baseURI: 'https://example.org/p/index.html'};
  globalThis.window = {addEventListener() {}, innerWidth: 1000, innerHeight: 800, devicePixelRatio: 1};
  globalThis.requestAnimationFrame = (f) => setTimeout(f, 0);
  globalThis.cancelAnimationFrame = (h) => clearTimeout(h);
  try {
    return await fn();
  } finally {
    for (const [k, v] of Object.entries(saved)) {
      if (v === undefined) delete globalThis[k];
      else globalThis[k] = v;
    }
  }
}

const N_ROWS = 10000;
const LIST_COLS = [
  {key: 'id', role: 'id', enc: 'str'},
  {key: 'ra', role: 'ra', enc: 'f64'},
  {key: 'dec', role: 'dec', enc: 'f64'},
  {key: 'cls', role: 'category', levels: ['star', 'yso'], enc: 'u8'},
  {key: 'f115w', role: 'mag', enc: 'f32'},
];

/**
 * Catalog DataClient fake that records every read. opts: {cols (manifest columns), idKey (row
 * chunk id key, manifest.rows.cols[0]), row(i) (row-chunk payload without _i), record(i)}.
 */
function fakeClient(n = N_ROWS, opts = {}) {
  const calls = {rows: [], values: [], id: [], record: [], order: []};
  const cols = opts.cols || LIST_COLS;
  const idKey = opts.idKey || 'id';
  const rowOf = opts.row || ((i) => ({[idKey]: `s${i}`, cls: i % 2 ? 'yso' : 'star', f115w: 10 + i / 100}));
  return {
    calls,
    n,
    manifest: {cols, rows: {chunk: 4096, cols: [idKey, ...cols.filter((c) => c.role !== 'id').map((c) => c.key)]}},
    col: (k) => cols.find((c) => c.key === k) || null,
    rows(a, b) {
      calls.rows.push([a, b]);
      const out = [];
      for (let i = a; i < Math.min(b, n); i++) out.push({...rowOf(i), _i: i});
      return Promise.resolve(out);
    },
    values(k) {
      calls.values.push(k);
      const v = new Float32Array(n);
      for (let i = 0; i < n; i++) v[i] = k === 'cls' ? i % 2 : 10 + i / 100;
      return Promise.resolve(v);
    },
    id(i) {
      calls.id.push(i);
      return Promise.resolve(`s${i}`);
    },
    record(i, o) {
      calls.record.push([i, o]);
      return Promise.resolve(opts.record ? opts.record(i) : {_i: i, _id: `s${i}`, cls: 'yso', f115w: 12.25});
    },
    order(key, dir) {
      calls.order.push([key, dir]);
      const o = new Int32Array(n);
      for (let i = 0; i < n; i++) o[i] = dir > 0 ? i : n - 1 - i;
      return Promise.resolve(o);
    },
    vis: () => Promise.resolve({vis: null}),
    find: () => Promise.resolve([]),
  };
}

function gate() {
  let release;
  const p = new Promise((r) => { release = r; });
  return {p, release};
}

function listHarness(config = {}, client = fakeClient()) {
  const store = createStore({});
  const fp = gate();
  const app = {
    page: {datasets: {cat: {kind: 'catalog'}}},
    catalog: async () => client,
    selectionFor: async () => ({mask: new Uint8Array(client.n), setIndices() {}}),
    afterFirstPaint: () => fp.p,
    idOf: async (ds, i) => `s${i}`,
  };
  const root = new FakeEl('div');
  const ctx = {id: 'list1', config, store, app, dataset: 'cat', setStatus() {}, theme: () => null, tools: null};
  const pane = mountList(root, ctx);
  return {client, store, app, root, pane, fp};
}

function listedIds(root) {
  return root.querySelectorAll('.pk-list-row').filter((r) => !r.hidden).map((r) => r.firstChild.textContent);
}

/** Run fn with LIST_TIMING.idleColumnsMs set to ms (restored afterwards). */
async function withIdle(ms, fn) {
  const saved = LIST_TIMING.idleColumnsMs;
  LIST_TIMING.idleColumnsMs = ms;
  try {
    return await fn();
  } finally {
    LIST_TIMING.idleColumnsMs = saved;
  }
}

test('levelIndex / rowsCarry: label map and carried row columns', () => {
  assert.equal(levelIndex({}), null);
  const m = levelIndex({levels: ['star', 'yso', 1]});
  assert.equal(m.get('yso'), 1);
  assert.equal(m.get('1'), 2);
  assert.equal(rowsCarry({rows: {cols: ['id', 'cls']}, cols: [{key: 'f115w'}]}, 'cls'), true);
  assert.equal(rowsCarry({rows: {cols: ['id', 'cls']}, cols: [{key: 'f115w'}]}, 'f115w'), false);
  assert.equal(rowsCarry({cols: [{key: 'f115w'}]}, 'f115w'), true, 'quantized rows carry every stored column');
  assert.equal(rowsCarry(null, 'x'), false);
});

test('list: first screen from rows(), no values() or id() until idle after afterFirstPaint', () => withIdle(30, () => withDom(async () => {
  const h = listHarness();
  let painted = false;
  h.pane.firstPaint.then(() => { painted = true; });
  await settle();
  assert.deepEqual(h.client.calls.rows, [[0, ROW_BLOCK]], 'one block read for the first screen');
  assert.deepEqual(h.client.calls.values, [], 'no whole column before first paint');
  assert.deepEqual(h.client.calls.id, [], 'ids come from the rows payload');
  assert.equal(painted, true, 'firstPaint resolves once the rows are drawn');
  const ids = listedIds(h.root);
  assert.equal(ids.length, 12);
  assert.deepEqual(ids.slice(0, 3), ['s0', 's1', 's2']);
  // cells filled from the payload (cls label, f115w number)
  const r1 = h.root.querySelectorAll('.pk-list-row')[1];
  assert.ok(r1.children.some((c) => c.textContent === 'yso'));
  assert.equal(h.pane.stats().colsLoaded, false);

  h.fp.release();
  await settle();
  assert.deepEqual(h.client.calls.values, [], 'no whole column right after first paint');
  await tick(80);
  await settle();
  assert.deepEqual([...h.client.calls.values].sort(), ['cls', 'f115w'], 'shown columns load at idle');
  assert.equal(h.pane.stats().colsLoaded, true);
  assert.deepEqual(h.client.calls.id, []);

  // scrolling far in stored order reads that block, still without id()
  const view = h.root.children.find((c) => c.classList.contains('pk-list-view'));
  view.scrollTop = 5000 * 24;
  view.fire('scroll');
  await settle();
  assert.ok(h.client.calls.rows.some(([a]) => a === 4992), JSON.stringify(h.client.calls.rows));
  assert.equal(listedIds(h.root)[0], 's5000');
  assert.deepEqual(h.client.calls.id, []);
  h.pane.destroy();
})));

test('list: the first-screen rows() read does not wait for an animation frame', () => withIdle(Infinity, () => withDom(async () => {
  // A busy compositor (software GL) can hold animation frames back: queue them by hand.
  const frames = [];
  globalThis.requestAnimationFrame = (f) => { frames.push(f); return frames.length; };
  globalThis.cancelAnimationFrame = (k) => { frames[k - 1] = null; };
  const h = listHarness();
  let painted = false;
  h.pane.firstPaint.then(() => { painted = true; });
  await settle();
  assert.ok(frames.length > 0, 'a paint frame is queued');
  assert.deepEqual(h.client.calls.rows, [[0, ROW_BLOCK]], 'rows requested before any frame ran');
  assert.deepEqual(h.client.calls.values, []);
  assert.equal(painted, false, 'the latch waits for the frame that draws the rows (note 112)');
  while (frames.length) {
    const f = frames.shift();
    if (f) f(0);
    await settle();
  }
  assert.equal(painted, true);
  assert.deepEqual(h.client.calls.rows, [[0, ROW_BLOCK]], 'the frame reuses the read in flight');
  assert.deepEqual(listedIds(h.root).slice(0, 2), ['s0', 's1']);
  h.pane.destroy();
})));

test('list: firstPaint waits for the rows; scrolling defers the idle column load', () => withIdle(60, () => withDom(async () => {
  const rowsGate = gate();
  const client = fakeClient();
  const rows0 = client.rows.bind(client);
  client.rows = (a, b) => rowsGate.p.then(() => rows0(a, b));
  const h = listHarness({}, client);
  let painted = false;
  h.pane.firstPaint.then(() => { painted = true; });
  await settle();
  assert.equal(painted, false, 'no first paint while rows() is pending');
  assert.equal(h.pane.stats().firstPaint, false);
  rowsGate.release();
  await settle();
  assert.equal(painted, true);
  h.fp.release();
  await settle();
  const view = h.root.children.find((c) => c.classList.contains('pk-list-view'));
  for (let k = 0; k < 4; k++) {
    await tick(30);
    view.fire('scroll');
  }
  assert.deepEqual(client.calls.values, [], 'activity restarts the quiet period');
  await tick(120);
  await settle();
  assert.deepEqual([...client.calls.values].sort(), ['cls', 'f115w']);
  h.pane.destroy();
})));

test('list: class colours come from the row records in stored order', () => withIdle(Infinity, () => withDom(async () => {
  const store0 = createStore({});
  store0.set({color: {by: 'cls'}});
  const client = fakeClient();
  const fp = gate();
  const app = {
    page: {datasets: {cat: {kind: 'catalog'}}},
    catalog: async () => client,
    selectionFor: async () => ({mask: new Uint8Array(client.n), setIndices() {}}),
    afterFirstPaint: () => fp.p,
  };
  const root = new FakeEl('div');
  const pane = mountList(root, {id: 'list1', config: {}, store: store0, app, dataset: 'cat', setStatus() {}, theme: () => null, tools: null});
  await settle();
  fp.release();
  await settle();
  assert.deepEqual(client.calls.values, [], 'no colour column read');
  const pal = legend.plotLevelColors(client.col('cls'), null);
  const rows = root.querySelectorAll('.pk-list-row');
  assert.equal(rows[0].style.props['--pk-swatch'], pal[0]);
  assert.equal(rows[1].style.props['--pk-swatch'], pal[1]);
  assert.ok(root.classList.contains('pk-list-colored'));
  pane.destroy();
})));

test('list: a colour column the rows do not carry loads after first paint', () => withIdle(Infinity, () => withDom(async () => {
  const store0 = createStore({});
  store0.set({color: {by: 'cls'}});
  const client = fakeClient();
  client.manifest.rows.cols = client.manifest.rows.cols.filter((k) => k !== 'cls');
  const fp = gate();
  const app = {
    page: {datasets: {cat: {kind: 'catalog'}}},
    catalog: async () => client,
    selectionFor: async () => ({mask: new Uint8Array(client.n), setIndices() {}}),
    afterFirstPaint: () => fp.p,
  };
  const root = new FakeEl('div');
  const pane = mountList(root, {id: 'list1', config: {}, store: store0, app, dataset: 'cat', setStatus() {}, theme: () => null, tools: null});
  await settle();
  assert.deepEqual(client.calls.values, []);
  fp.release();
  await settle();
  assert.deepEqual(client.calls.values, ['cls']);
  pane.destroy();
})));

test('list: firstPaint rejects when the catalog fails, resolves on the non-catalog note', () => withDom(async () => {
  const store0 = createStore({});
  const bad = {
    page: {datasets: {cat: {kind: 'catalog'}, img: {kind: 'images'}}},
    catalog: async () => { throw new Error('boom'); },
    selectionFor: async () => null,
    afterFirstPaint: () => Promise.resolve(),
  };
  const mk = (ds) => mountList(new FakeEl('div'), {id: 'l', config: {}, store: store0, app: bad, dataset: ds, setStatus() {}, theme: () => null, tools: null});
  const warn = console.warn;
  console.warn = () => {};
  try {
    await assert.rejects(mk('cat').firstPaint, /boom/);
  } finally {
    console.warn = warn;
  }
  await mk('img').firstPaint;
}));

test('list: a sort loads the whole columns before first paint', () => withDom(async () => {
  const h = listHarness({sort: '-f115w'});
  await settle();
  assert.deepEqual(h.client.calls.order, [['f115w', -1]]);
  assert.deepEqual([...h.client.calls.values].sort(), ['cls', 'f115w']);
  assert.equal(listedIds(h.root)[0], `s${N_ROWS - 1}`, 'sorted order shows ids (id() after the sort)');
  h.pane.destroy();
}));

test('list: header click sorts and loads columns; colour-by loads its column', () => withDom(async () => {
  const h = listHarness();
  await settle();
  assert.deepEqual(h.client.calls.values, []);
  const head = h.root.children.find((c) => c.classList.contains('pk-list-head'));
  const th = head.children.find((b) => b.dataset.key === 'f115w');
  head.fire('click', {target: th});
  await settle();
  assert.deepEqual(h.client.calls.order, [['f115w', 1]]);
  assert.deepEqual([...h.client.calls.values].sort(), ['cls', 'f115w']);
  h.store.set({color: {by: 'cls'}});
  await settle();
  assert.equal(h.client.calls.values.filter((k) => k === 'cls').length, 1, 'the loaded cls column also gives the swatches');
  const sw = h.root.querySelectorAll('.pk-list-row').map((r) => r.style.props['--pk-swatch']);
  assert.ok(sw.some((c) => c && c !== 'transparent'), 'swatches drawn in the sorted order');
  h.fp.release();
  await settle();
  h.pane.destroy();
}));

test('list: colour-by after startup loads columns before first paint', () => withDom(async () => {
  const h = listHarness();
  await settle();
  h.store.set({color: {by: 'cls'}});
  await settle();
  assert.ok(h.client.calls.values.includes('cls'));
  assert.ok(h.client.calls.values.includes('f115w'), 'colour-by also loads the shown columns');
  h.pane.destroy();
}));

test('payloadLevel: bool and float32 payload labels match codec level spellings', () => {
  const lv = (levels) => levelIndex({levels});
  assert.equal(payloadLevel(lv(['False', 'True']), true), 1);
  assert.equal(payloadLevel(lv(['False', 'True']), false), 0);
  assert.equal(payloadLevel(lv(['false', 'true']), true), 1, 'JS spelling still matches');
  const f32 = lv([String(Math.fround(0.1)), String(Math.fround(0.2))]);
  assert.equal(String(Math.fround(0.1)), '0.10000000149011612');
  assert.equal(payloadLevel(f32, 0.1), 0);
  assert.equal(payloadLevel(f32, 0.2), 1);
  assert.equal(payloadLevel(lv(['1', '2']), 2), 1, 'integer labels');
  assert.equal(payloadLevel(lv(['0.5']), 0.5), 0, 'float64 labels');
  assert.equal(payloadLevel(lv(['a']), 'a'), 0);
  assert.ok(Number.isNaN(payloadLevel(lv(['a']), 'b')));
  assert.ok(Number.isNaN(payloadLevel(lv(['a']), null)));
  assert.ok(Number.isNaN(payloadLevel(null, 'a')));
});

/** Category colour client: `grp` (3 levels, not shown) besides the default columns. */
function grpClient() {
  const cols = [...LIST_COLS, {key: 'grp', role: 'category', levels: ['a', 'b', 'c'], enc: 'u8'}];
  const client = fakeClient(N_ROWS, {cols, row: (i) => ({id: `s${i}`, cls: i % 2 ? 'yso' : 'star', f115w: 10 + i / 100, grp: ['a', 'b', 'c'][i % 3]})});
  const values0 = client.values.bind(client);
  client.values = (k) => {
    if (k !== 'grp') return values0(k);
    client.calls.values.push(k);
    return Promise.resolve(Float32Array.from({length: client.n}, (_, i) => i % 3));
  };
  return client;
}

function swatches(root) {
  return root.querySelectorAll('.pk-list-row').filter((r) => !r.hidden).map((r) => r.style.props['--pk-swatch']);
}

test('list: colour-by A -> none -> A in sorted order re-reads the colour column', () => withIdle(Infinity, () => withDom(async () => {
  const h = listHarness({sort: '-f115w', columns: ['f115w']}, grpClient());
  await settle();
  h.fp.release();
  h.store.set({color: {by: 'grp'}});
  await settle();
  const first = swatches(h.root);
  assert.ok(first.length > 0 && first.every((c) => c && c !== 'transparent'), JSON.stringify(first));
  h.store.set({color: {by: null}});
  await settle();
  assert.ok(swatches(h.root).every((c) => c === 'transparent'));
  h.store.set({color: {by: 'grp'}});
  await settle();
  assert.deepEqual(swatches(h.root), first, 'swatches back after A -> none -> A');
  assert.equal(h.client.calls.values.filter((k) => k === 'grp').length, 2);
  assert.ok(h.root.classList.contains('pk-list-colored'));
  h.pane.destroy();
})));

test('list: a shown colour column is read once when a sort loads both', () => withIdle(Infinity, () => withDom(async () => {
  const store0 = createStore({});
  store0.set({color: {by: 'cls'}});
  const client = fakeClient();
  const fp = gate();
  const app = {
    page: {datasets: {cat: {kind: 'catalog'}}},
    catalog: async () => client,
    selectionFor: async () => ({mask: new Uint8Array(client.n), setIndices() {}}),
    afterFirstPaint: () => fp.p,
  };
  const root = new FakeEl('div');
  const pane = mountList(root, {id: 'list1', config: {columns: ['cls', 'f115w']}, store: store0, app, dataset: 'cat', setStatus() {}, theme: () => null, tools: null});
  await settle();
  fp.release();
  await settle();
  assert.deepEqual(client.calls.values, []);
  const head = root.children.find((c) => c.classList.contains('pk-list-head'));
  head.fire('click', {target: head.children.find((b) => b.dataset.key === 'f115w')});
  await settle();
  assert.deepEqual([...client.calls.values].sort(), ['cls', 'f115w'], 'one read per column');
  const sw = swatches(root);
  assert.ok(sw.length && sw.every((c) => c && c !== 'transparent'));
  pane.destroy();
})));

test('list: a shown column the rows do not carry loads right after first paint', () => withIdle(Infinity, () => withDom(async () => {
  const client = fakeClient(N_ROWS, {row: (i) => ({id: `s${i}`, cls: i % 2 ? 'yso' : 'star'})});
  client.manifest.rows.cols = client.manifest.rows.cols.filter((k) => k !== 'f115w');
  const h = listHarness({columns: ['cls', 'f115w']}, client);
  let painted = false;
  h.pane.firstPaint.then(() => { painted = true; });
  await settle();
  assert.equal(painted, true);
  assert.deepEqual(client.calls.values, [], 'no whole column before first paint');
  h.fp.release();
  await settle();
  assert.deepEqual(client.calls.values, ['f115w'], 'only the uncarried column, without the idle wait');
  const r0 = h.root.querySelectorAll('.pk-list-row')[0];
  assert.deepEqual(r0.children.map((c) => c.textContent), ['s0', 'star', '10']);
  assert.equal(h.pane.stats().colsLoaded, false);
  h.pane.destroy();
})));

test('list: a bool category colours from its rows; an unmatched label loads the column', () => withIdle(Infinity, () => withDom(async () => {
  const mk = (levels, rowVal) => {
    const cols = [{key: 'id', role: 'id', enc: 'str'}, {key: 'ok', role: 'category', levels, enc: 'u8'}];
    const client = fakeClient(N_ROWS, {cols, row: (i) => ({id: `s${i}`, ok: rowVal(i)})});
    const values0 = client.values.bind(client);
    client.values = (k) => {
      if (k !== 'ok') return values0(k);
      client.calls.values.push(k);
      return Promise.resolve(Float32Array.from({length: client.n}, (_, i) => i % 2));
    };
    const store0 = createStore({});
    store0.set({color: {by: 'ok'}});
    const fp = gate();
    const app = {
      page: {datasets: {cat: {kind: 'catalog'}}},
      catalog: async () => client,
      selectionFor: async () => ({mask: new Uint8Array(client.n), setIndices() {}}),
      afterFirstPaint: () => fp.p,
    };
    const root = new FakeEl('div');
    const pane = mountList(root, {id: 'l', config: {columns: []}, store: store0, app, dataset: 'cat', setStatus() {}, theme: () => null, tools: null});
    return {client, root, pane, fp};
  };
  const a = mk(['False', 'True'], (i) => i % 2 === 1);
  await settle();
  a.fp.release();
  await settle();
  assert.deepEqual(a.client.calls.values, [], 'bool labels match True/False levels');
  assert.ok(swatches(a.root).every((c) => c && c !== 'transparent'));
  a.pane.destroy();

  const b = mk(['x', 'y'], (i) => (i % 2 ? 'Y' : 'X'));
  await settle();
  assert.deepEqual(b.client.calls.values, [], 'the fallback waits for first paint');
  b.fp.release();
  await settle();
  assert.deepEqual(b.client.calls.values, ['ok'], 'unmatched labels fall back to the colour column');
  assert.ok(swatches(b.root).every((c) => c && c !== 'transparent'));
  b.pane.destroy();
})));

test('list: ids come from the row-chunk id key, never from a stored column called id', () => withDom(async () => {
  const cols = [
    {key: 'name', role: 'id', enc: 'str'},
    {key: 'id', role: 'value', enc: 'f32'},
    {key: 'f115w', role: 'mag', enc: 'f32'},
  ];
  const client = fakeClient(1000, {cols, idKey: 'name', row: (i) => ({name: `NAME${i}`, id: 1000 + i, f115w: 10 + i / 100})});
  const h = listHarness({}, client);
  await settle();
  assert.deepEqual(listedIds(h.root).slice(0, 3), ['NAME0', 'NAME1', 'NAME2']);
  assert.deepEqual(client.calls.id, [], 'no client.id(i) per row');
  const view = h.root.children.find((c) => c.classList.contains('pk-list-view'));
  const rowEl = h.root.querySelectorAll('.pk-list-row')[2];
  view.fire('click', {target: rowEl});
  assert.deepEqual(h.store.get('focus'), {ds: 'cat', i: 2, id: 'NAME2'});
  h.fp.release();
  await settle();
  h.pane.destroy();
}));

test('list and hover: an integer-labelled category shows its labels, not shifted levels', () => withIdle(0, () => withDom(async () => {
  const cols = [
    {key: 'id', role: 'id', enc: 'str'},
    {key: 'kls', role: 'category', levels: ['1', '2', '3'], enc: 'cat8'},
  ];
  const client = fakeClient(100, {cols, row: (i) => ({id: `s${i}`, kls: (i % 3) + 1}),
    record: (i) => ({_i: i, _id: `s${i}`, kls: (i % 3) + 1})});
  const h = listHarness({columns: ['kls']}, client);
  await settle();
  const cell = (k) => h.root.querySelectorAll('.pk-list-row')[k]._cells[0].textContent;
  assert.deepEqual([cell(0), cell(1), cell(2)], ['1', '2', '3'], 'payload labels');
  h.fp.release();
  await settle();
  await tick(20);
  await settle();
  assert.equal(h.pane.stats().colsLoaded, true);
  assert.deepEqual([cell(0), cell(1), cell(2)], ['1', '2', '3'], 'after the columns load');
  h.pane.destroy();

  const store = createStore({});
  const app = {store, page: {datasets: {cat: {kind: 'catalog'}}}, catalog: async () => client,
    idOf: async () => 'x', hub: {json: async () => null, records: async () => ({has: () => false})}, theme: () => ({})};
  const {card} = installHover(app);
  store.set({hover: {ds: 'cat', i: 0}});
  await settle();
  assert.match(card.textContent, /kls1$/, card.textContent);
  store.set({hover: {ds: 'cat', i: 1}});
  await settle();
  assert.match(card.textContent, /kls2$/, card.textContent);
})));

test('list virtual:false with more rows than the row cache settles', () => withDom(async () => {
  const n = ROW_CACHE_ROWS + 400;
  const h = listHarness({virtual: false}, fakeClient(n));
  await settle(20);
  const after20 = h.client.calls.rows.length;
  await settle(40);
  assert.equal(h.client.calls.rows.length, after20, 'no further rows() reads once the window is cached');
  assert.equal(after20, Math.ceil(n / ROW_BLOCK), 'each block read once');
  assert.equal(listedIds(h.root).length, n);
  assert.equal(listedIds(h.root)[n - 1], `s${n - 1}`);
  h.pane.destroy();
}));

test('hover card reads one record on its channel and never values()', () => withDom(async () => {
  const client = fakeClient();
  const store = createStore({});
  let idOf = 0;
  const app = {
    store,
    page: {datasets: {cat: {kind: 'catalog'}}},
    catalog: async () => client,
    idOf: async () => { idOf++; return 'x'; },
    hub: {json: async () => null, records: async () => ({has: () => false})},
    theme: () => ({}),
  };
  const {card} = installHover(app);
  store.set({hover: {ds: 'cat', i: 42}});
  await settle();
  assert.equal(client.calls.record.length, 1);
  assert.equal(client.calls.record[0][0], 42);
  assert.equal(client.calls.record[0][1].channel, HOVER_CHANNEL);
  assert.deepEqual(client.calls.values, []);
  assert.equal(idOf, 0, 'id from the record');
  const text = card.textContent;
  assert.match(text, /s42/);
  assert.match(text, /yso/);
  assert.match(text, /12\.25/);
}));

// WPE8 item 1: the mini-spectrum lookup in addFigure() must use hasRecordOf (exact for a foreign
// id_column set via hasRecord(), not the optimistic has()) and must treat a null get() as 'no
// record', trying the next candidate 'pks1' record-set dataset rather than stopping.
test('hover mini-spectrum: optimistic has() trap and a null get() both fall through to the next record set', () => withDom(async () => {
  const client = fakeClient();
  const store = createStore({});
  const calls = [];
  const sets = {
    // a foreign id_column set: has() is optimistic (true) but hasRecord() is exact (false) -
    // must be skipped, not treated as present.
    spec1: {has: () => true, hasRecord: async (id) => { calls.push(['spec1.hasRecord', id]); return false; },
      get: async () => { calls.push(['spec1.get']); return {channels: {flux: new Float32Array([1])}, wave: new Float32Array([1])}; }},
    // hasRecord() reports present, but get() resolves null (no row for this key) - also skipped.
    spec2: {has: () => false, hasRecord: async (id) => { calls.push(['spec2.hasRecord', id]); return true; },
      get: async (id) => { calls.push(['spec2.get', id]); return null; }},
    // the real record: reached only after both prior candidates are correctly skipped.
    spec3: {has: () => false, hasRecord: async (id) => { calls.push(['spec3.hasRecord', id]); return true; },
      get: async (id) => { calls.push(['spec3.get', id]); return {channels: {flux: new Float32Array([1, 2])}, wave: new Float32Array([1, 2])}; }},
  };
  const app = {
    store,
    page: {datasets: {
      cat: {kind: 'catalog'},
      spec1: {kind: 'records', catalog: 'cat'},
      spec2: {kind: 'records', catalog: 'cat'},
      spec3: {kind: 'records', catalog: 'cat'},
    }},
    catalog: async () => client,
    idOf: async () => 'x',
    hub: {json: async () => null, records: async (ds) => sets[ds]},
    theme: () => ({}),
  };
  const {card} = installHover(app);
  store.set({hover: {ds: 'cat', i: 42}});
  await settle();
  assert.deepEqual(calls, [
    ['spec1.hasRecord', 's42'], ['spec2.hasRecord', 's42'], ['spec2.get', 's42'],
    ['spec3.hasRecord', 's42'], ['spec3.get', 's42'],
  ], 'spec1 skipped on hasRecord()=false (never reaches get); spec2 skipped on a null get()');
  assert.ok(card.querySelector('.pk-hover-fig'), 'the figure drew from spec3');
}));

function fakeHandle() {
  return {state: 'ready', on: () => () => {}, view: () => ({ra: 0, dec: 0, fov: 1, fovy: 1})};
}

test('SkyMarkers: ra/dec load only after afterFirstPaint', async () => {
  const client = fakeClient(100);
  const fp = gate();
  const m = new SkyMarkers({h: fakeHandle(), app: {afterFirstPaint: () => fp.p}, store: createStore({}),
    ds: 'cat', pane: 'sky1', client});
  const p = m._positions();
  await settle();
  assert.deepEqual(client.calls.values, [], 'no position read before first paint');
  assert.equal(m.position(3), null);
  fp.release();
  await p;
  assert.deepEqual([...client.calls.values].sort(), ['dec', 'ra']);
  assert.ok(m.position(3));
  await m._positions();
  assert.equal(client.calls.values.length, 2, 'positions load once');
});

test('SkyMarkers: destroyed before first paint reads nothing', async () => {
  const client = fakeClient(100);
  const fp = gate();
  const m = new SkyMarkers({h: fakeHandle(), app: {afterFirstPaint: () => fp.p}, store: createStore({}),
    ds: 'cat', pane: 'sky1', client});
  const p = m._positions();
  m.destroyed = true;
  fp.release();
  await p;
  assert.deepEqual(client.calls.values, []);
});

// ------------------------------------------------------------------ deferred positions (WPC1)

/**
 * Quantized-catalog client (no row chunks) with positions: rows()/record() leave ra/dec null with
 * `_posDeferred` unless called with {pos: true}, like the worker before first paint.
 */
function quantizedPosClient(n = 50) {
  const calls = {rows: [], record: [], values: []};
  const cols = [{key: 'f115w', role: 'mag', enc: 'u16q', step: 0.001, lo: 10, hi: 20}];
  const row = (i, pos) => Object.assign({id: `s${i}`, _id: `s${i}`, f115w: 10 + i / 100, _quantized: true, _i: i},
    pos ? {ra: 10 + i / 1000, dec: -1 + i / 1000} : {ra: null, dec: null, _posDeferred: true});
  return {
    calls, n,
    manifest: {cols, pos: {enc: 'u32lin'}, bbox: {ra: [10, 11], dec: [-1, 1]}},
    col: (k) => cols.find((c) => c.key === k) || null,
    rows(a, b, opts = {}) {
      calls.rows.push([a, b, !!opts.pos]);
      const out = [];
      for (let i = a; i < Math.min(b, n); i++) out.push(row(i, !!opts.pos));
      return Promise.resolve(out);
    },
    record(i, opts = {}) {
      calls.record.push([i, opts]);
      return Promise.resolve(i >= 0 && i < n ? row(i, !!opts.pos) : null);
    },
    values(k) {
      calls.values.push(k);
      return Promise.resolve(new Float64Array(n));
    },
  };
}

test('table: quantized blocks read before first paint are re-read with {pos: true} after it', () => withDom(async () => {
  const client = quantizedPosClient(50);
  const store = createStore({});
  const fp = gate();
  const app = {page: {datasets: {cat: {kind: 'catalog'}}}, catalog: async () => client, afterFirstPaint: () => fp.p};
  const root = new FakeEl('div');
  const pane = mountTable(root, {id: 't1', config: {}, store, app, dataset: 'cat', setStatus() {}, tools: null});
  await settle();
  assert.deepEqual(client.calls.rows, [[0, 50, false]], 'first screen without positions');
  const raCell = () => {
    const r = root.querySelectorAll('.pk-list-row')[1];
    return r.children[1].textContent;
  };
  assert.equal(raCell(), MISSING, 'ra deferred before first paint');
  assert.equal(pane.stats().posReady, false);
  fp.release();
  await settle();
  assert.deepEqual(client.calls.rows, [[0, 50, false], [0, 50, true]], 'the deferred block is re-read once with pos');
  assert.notEqual(raCell(), MISSING, 'ra filled after first paint');
  assert.equal(pane.stats().posReady, true);
  assert.deepEqual(client.calls.values, [], 'no whole column read');
  pane.destroy();
}));

test('table: a block in flight at first paint is dropped and re-read; row-chunk catalogs are not re-read', () => withDom(async () => {
  const client = quantizedPosClient(30);
  const slow = gate();
  const rows0 = client.rows.bind(client);
  const order = [];
  client.rows = (a, b, o = {}) => {
    order.push([a, b, !!o.pos]);
    return o.pos ? rows0(a, b, o) : slow.p.then(() => rows0(a, b, o));
  };
  const store = createStore({});
  const fp = gate();
  const app = {page: {datasets: {cat: {kind: 'catalog'}}}, catalog: async () => client, afterFirstPaint: () => fp.p};
  const root = new FakeEl('div');
  const pane = mountTable(root, {id: 't1', config: {}, store, app, dataset: 'cat', setStatus() {}, tools: null});
  await settle();
  fp.release();
  await settle();
  slow.release();                       // the stale pos-less answer arrives last and is ignored
  await settle();
  assert.deepEqual(order, [[0, 30, false], [0, 30, true]]);
  const r = root.querySelectorAll('.pk-list-row')[2];
  assert.notEqual(r.children[1].textContent, MISSING);
  pane.destroy();

  // exact row chunks carry their own ra/dec: nothing is re-read after first paint
  const exact = fakeClient(20);
  exact.manifest.pos = {enc: 'u32lin'};
  const fp2 = gate();
  const app2 = {page: {datasets: {cat: {kind: 'catalog'}}}, catalog: async () => exact, afterFirstPaint: () => fp2.p};
  const p2 = mountTable(new FakeEl('div'), {id: 't2', config: {}, store, app: app2, dataset: 'cat', setStatus() {}, tools: null});
  await settle();
  fp2.release();
  await settle();
  assert.equal(exact.calls.rows.length, 1);
  p2.destroy();
}));

test('detail: the focus record is read with {pos: true}; _qkeys values show as quantized', () => withDom(async () => {
  const client = quantizedPosClient(10);
  const store = createStore({focus: {ds: 'cat', i: 4, id: 's4'}});
  const app = {page: {datasets: {cat: {kind: 'catalog'}}}, catalog: async () => client, resolveIds: async () => [4],
    afterFirstPaint: () => new Promise(() => {})};
  const root = new FakeEl('div');
  const ctx = {id: 'd1', config: {}, store, app, dataset: 'cat', setStatus() {}, setTitle() {}, tools: null,
    hub: {json: async () => null, records: async () => null, resolve: (u) => new URL(u, PAGE)}};
  const pane = mountDetail(root, ctx);
  await settle();
  assert.equal(client.calls.record.length, 1);
  assert.equal(client.calls.record[0][0], 4);
  assert.equal(client.calls.record[0][1].pos, true, 'focus from the URL before first paint gets coordinates');
  assert.equal(client.calls.record[0][1].channel, 'detail:d1');
  assert.ok(root.querySelectorAll('.pk-detail-pos').length, 'position shown');
  pane.destroy();

  // _qkeys: an exact row-chunk record with merged quantized cells
  const meta = {key: 'f200w', role: 'mag', step: 0.001};
  const row = {f115w: 12.3456789, f200w: 13.123456789, _qkeys: ['f200w']};
  assert.equal(quantizedKey(row, 'f200w'), true);
  assert.equal(quantizedKey(row, 'f115w'), false);
  assert.equal(quantizedKey({_qkeys: {f200w: true}}, 'f200w'), true);
  assert.equal(quantizedKey({}, 'f200w'), false);
  assert.equal(quantizedKey(null, 'f200w'), false);
  const q = propertyText(row, 'f200w', meta, !quantizedKey(row, 'f200w'));
  const e = propertyText(row, 'f200w', meta, true);
  assert.equal(q.text, '13.123');
  assert.notEqual(e.text, q.text);
}));

function fakeAladinLib() {
  const shapes = [];
  const al = {
    on() {}, getRaDec: () => [10.02, -0.98], getFov: () => [1, 1], addOverlay() {}, addCatalog() {},
    gotoRaDec() {}, setFoV() {}, world2pix: () => [50, 20], pix2world: () => [10, -1], removeOverlay() {},
  };
  const A = {
    init: Promise.resolve(),
    aladin: () => al,
    graphicOverlay: () => ({add(sh) { shapes.push(sh); }, removeAll() { shapes.length = 0; }}),
    circle: (ra, dec, r) => ({ra, dec, r}),
    polygon: (pts) => ({pts}),
    catalog: () => ({addSources() {}, removeAll() {}}),
  };
  return {A, al, shapes};
}

const DEAD_ALADIN = {cdn: 'https://127.0.0.1:9/none.js', mirror: 'https://127.0.0.1:9/none2.js'};

function skyApp(client, fp) {
  return {
    page: {datasets: {cat: {kind: 'catalog'}}, aladin: DEAD_ALADIN},
    catalog: async () => client,
    afterFirstPaint: () => fp.p,
    resolveIds: async (ds, ids) => ids.map((s) => Number(String(s).slice(1))),
    runtime: {webgl2: true},
    notes: {},
    idOf: async (ds, i) => `s${i}`,
  };
}

test('sky: setup does not wait for the markers; listeners and narrative:sync come first; highlight reads {pos: true}', () => withDom(async () => {
  aladin.resetAladin();
  const {A, shapes} = fakeAladinLib();
  globalThis.window.A = A;
  const client = quantizedPosClient(20);
  const store = createStore({});
  const fp = gate();
  const syncs = [];
  store.onEvent('narrative:sync', (p) => syncs.push(p));
  const root = new FakeEl('div');
  const ctx = {id: 'sky1', config: {brush: false}, store, app: skyApp(client, fp), dataset: 'cat', setStatus() {},
    theme: () => ({}), tools: null, hub: {json: async () => null, resolve: (u) => new URL(u, PAGE)}};
  const pane = mountSky(root, ctx);
  let done = false;
  pane.ready.then(() => { done = true; });
  await settle(10);
  assert.equal(done, true, 'ready resolves while first paint is still pending');
  assert.deepEqual(syncs.map((p) => p.pane), ['sky1']);
  const h = aladin.aladinHandles().find((x) => x.owner === 'sky1');
  for (const ev of ['positionChanged', 'zoomChanged', 'resizeChanged']) {
    assert.ok(h._listeners.has(ev) && h._listeners.get(ev).size >= 1, ev);
  }
  assert.deepEqual(client.calls.values, [], 'no positions before first paint');
  // a narrative highlight (e.g. from the URL) before first paint still finds coordinates
  store.emit('narrative:highlight', {pane: 'sky1', ds: 'cat', ids: ['s3'], beat: 1});
  await settle(10);
  assert.deepEqual(client.calls.record.map(([i, o]) => [i, o.pos]), [[3, true]]);
  assert.equal(pane.stats().highlight, 1);
  assert.ok(shapes.some((sh) => Math.abs(sh.ra - 10.003) < 1e-9 && Math.abs(sh.dec + 0.997) < 1e-9), JSON.stringify(shapes));
  pane.destroy();
  fp.release();
  await settle();
  delete globalThis.window.A;
  aladin.resetAladin();
}));

test('wavelength: mount retries a failed Aladin prefetch; view listeners before the markers settle', () => withDom(async () => {
  aladin.resetAladin();
  const pre = aladin.loadAladin({aladin: DEAD_ALADIN}, {timeout: 50});   // fire-and-forget prefetch
  await assert.rejects(pre);
  await tick();
  assert.equal(aladin.libState().state, 'error');
  const {A} = fakeAladinLib();
  globalThis.window.A = A;
  const client = quantizedPosClient(20);
  const store = createStore({});
  const fp = gate();
  const hips = gate();
  const root = new FakeEl('div');
  const app = skyApp(client, fp);
  const ctx = {id: 'wl1', config: {hips: 'hips', markers: 'cat'}, store, app, dataset: null, setStatus() {},
    theme: () => ({}), tools: null, hub: {json: () => hips.p, resolve: (u) => new URL(u, PAGE)}};
  const pane = mountWavelength(root, ctx);
  // the retry starts at mount (retryAladinOnce), before setup reaches aladinFor
  assert.equal(aladin.aladinRetried(), true);
  assert.equal(aladin.aladinHandles().length, 0);
  hips.release(null);
  let done = false;
  pane.ready.then(() => { done = true; });
  await settle(10);
  assert.equal(done, true, 'ready resolves while first paint is still pending');
  assert.equal(pane.stats().aladin, 'preloaded');
  const h = aladin.aladinHandles().find((x) => x.owner === 'wl1');
  for (const ev of ['positionChanged', 'zoomChanged', 'resizeChanged']) {
    assert.ok(h._listeners.has(ev) && h._listeners.get(ev).size >= 1, ev);
  }
  assert.deepEqual(client.calls.values, [], 'no positions before first paint');
  assert.ok(pane.stats().markers, 'markers created');
  pane.destroy();
  fp.release();
  await settle();
  delete globalThis.window.A;
  aladin.resetAladin();
}));

// WPE8 item 3: wavelength.js's playback speed reads the store key `speed` (written by legacy
// `?speed=`/hash `speed=` via mapLegacy before boot strips consumed query keys), not
// `location.search` directly, and keeps the <select> and the store in sync both ways while
// ignoring its own echo.
test('wavelength: speed select follows the store key "speed" and writes it back, ignoring its own echo', () => withDom(async () => {
  aladin.resetAladin();
  const {A} = fakeAladinLib();
  globalThis.window.A = A;
  const client = quantizedPosClient(20);
  const store = createStore({});
  const fp = gate();
  const root = new FakeEl('div');
  const app = skyApp(client, fp);
  const ctx = {id: 'wl1', config: {hips: 'hips', markers: 'cat'}, store, app, dataset: null, setStatus() {},
    theme: () => ({}), tools: null, hub: {json: () => Promise.resolve(null), resolve: (u) => new URL(u, PAGE)}};
  const pane = mountWavelength(root, ctx);
  await settle(10);
  const speedSel = root.querySelector('.pk-wl-speed');
  assert.equal(speedSel.value, '1', 'no store speed and no options.speed: default 1');

  // another pane / legacy ?speed= writes the store directly: the select follows it
  store.set({speed: 2}, {source: 'legacy'});
  await settle();
  assert.equal(speedSel.value, '2');

  // the user picks a value from the control: it writes {speed} back to the store with its own id
  let lastSources = null;
  store.on(['speed'], (changed, st, sources) => { lastSources = sources; });
  speedSel.value = '0.5';
  speedSel.fire('change');
  await settle();
  assert.equal(store.get('speed'), 0.5);
  assert.ok(lastSources.has('wl1') && lastSources.size === 1, 'the write carries source: wl1');
  assert.equal(speedSel.value, '0.5', 'the select keeps its own value (no self-echo override)');

  pane.destroy();
  fp.release();
  await settle();
  delete globalThis.window.A;
  aladin.resetAladin();
}));

// ---- first paint of the catalog readers (IMPLEMENTATION_NOTES 112)

test('table: firstPaint resolves once the first window is drawn; a failed block rejects it', () => withDom(async () => {
  const client = quantizedPosClient(50);
  const rowsGate = gate();
  const rows0 = client.rows.bind(client);
  client.rows = (a, b, o) => rowsGate.p.then(() => rows0(a, b, o));
  const store = createStore({});
  const app = {page: {datasets: {cat: {kind: 'catalog'}}}, catalog: async () => client, afterFirstPaint: () => new Promise(() => {})};
  const pane = mountTable(new FakeEl('div'), {id: 't1', config: {}, store, app, dataset: 'cat', setStatus() {}, tools: null});
  let painted = false;
  pane.firstPaint.then(() => { painted = true; });
  await settle();
  assert.equal(painted, false, 'rows still pending');
  rowsGate.release();
  await settle();
  assert.equal(painted, true);
  assert.equal(pane.stats().firstPaint, true);
  pane.destroy();

  const bad = quantizedPosClient(50);
  bad.rows = () => Promise.reject(new Error('block failed'));
  const warn = console.warn;
  console.warn = () => {};
  try {
    const app2 = {page: {datasets: {cat: {kind: 'catalog'}}}, catalog: async () => bad, afterFirstPaint: () => new Promise(() => {})};
    const p2 = mountTable(new FakeEl('div'), {id: 't2', config: {}, store, app: app2, dataset: 'cat', setStatus() {}, tools: null});
    await assert.rejects(p2.firstPaint, /block failed/);
    p2.destroy();
    // a configuration note counts as painted
    const app3 = {page: {datasets: {img: {kind: 'images'}}}};
    await mountTable(new FakeEl('div'), {id: 't3', config: {}, store, app: app3, dataset: 'img', setStatus() {}, tools: null}).firstPaint;
  } finally {
    console.warn = warn;
  }
}));

test('gallery: firstPaint resolves at the first grid paint; a failed image set rejects it', () => withDom(async () => {
  const savedOption = globalThis.Option;
  globalThis.Option = function Option(text = '', value = '') {
    const o = new FakeEl('option');
    o.textContent = text;
    o.value = value;
    return o;
  };
  try {
    await galleryFirstPaint();
  } finally {
    if (savedOption === undefined) delete globalThis.Option;
    else globalThis.Option = savedOption;
  }
}));

async function galleryFirstPaint() {
  const store = createStore({});
  const docGate = gate();
  const doc = {items: [{id: 'a', url: 'a.png'}, {id: 'b', url: 'b.png'}]};
  const hub = {json: () => docGate.p.then(() => doc)};
  const app = {page: {datasets: {img: {kind: 'images'}}}, afterFirstPaint: () => Promise.resolve()};
  const pane = mountGallery(new FakeEl('div'), {id: 'g1', config: {}, store, app, hub, dataset: 'img', setStatus() {}, theme: () => null, tools: null});
  let painted = false;
  pane.firstPaint.then(() => { painted = true; });
  await settle();
  assert.equal(painted, false, 'images.json pending');
  docGate.release();
  await settle(10);
  assert.equal(painted, true);
  pane.destroy();

  const warn = console.warn;
  console.warn = () => {};
  try {
    const hub2 = {json: () => Promise.reject(new Error('no images'))};
    const p2 = mountGallery(new FakeEl('div'), {id: 'g2', config: {}, store, app, hub: hub2, dataset: 'img', setStatus() {}, theme: () => null, tools: null});
    await assert.rejects(p2.firstPaint, /no images/);
    p2.destroy();
  } finally {
    console.warn = warn;
  }
}

test('hist: firstPaint resolves once the histogram is drawn; a failed load rejects it', () => withDom(async () => {
  const store = createStore({});
  const cols = [{key: 'f115w', role: 'mag', enc: 'f32', label: 'F115W'}];
  const mk = (hist) => ({
    n: 100, manifest: {cols}, col: (k) => cols.find((c) => c.key === k) || null,
    hist, baseKeys: (e) => [e], vis: async () => ({vis: null}),
  });
  const okHist = async (expr, nb) => ({edges: Array.from({length: nb + 1}, (_, k) => 10 + k), counts: new Float64Array(nb).fill(1), total: 100});
  const appOf = (client) => ({
    page: {datasets: {cat: {kind: 'catalog'}}}, catalog: async () => client,
    selectionFor: async () => ({count: 0, mask: new Uint8Array(100), onChange: () => () => {}}),
  });
  const ctxOf = (client) => ({id: 'h1', config: {expr: 'f115w'}, store, app: appOf(client), dataset: 'cat', setStatus() {}, theme: () => ({}), tools: null});
  const pane = await mountHist(new FakeEl('div'), ctxOf(mk(okHist)));
  await settle();
  await pane.firstPaint;
  pane.destroy();

  const p2 = await mountHist(new FakeEl('div'), ctxOf(mk(async () => { throw new Error('hist failed'); })));
  await assert.rejects(p2.firstPaint, /hist failed/);
  p2.destroy();
}));

/** window.localStorage stand-in for panes whose widgets remember settings. */
function fakeStorage() {
  const m = new Map();
  return {getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k)};
}

test('scatter: firstPaint resolves at the first data draw; a failed x/y load rejects it', () => withDom(async () => {
  globalThis.window.localStorage = fakeStorage();
  globalThis.window.removeEventListener = () => {};
  const store = createStore({});
  const cols = [{key: 'id', role: 'id', enc: 'str'}, {key: 'f115w', role: 'mag', enc: 'f32', label: 'F115W'},
    {key: 'f200w', role: 'mag', enc: 'f32', label: 'F200W'}];
  const n = 50;
  const xyGate = gate();
  const mk = (values) => ({
    n, manifest: {cols}, col: (k) => cols.find((c) => c.key === k) || null,
    values, baseKeys: (e) => [e], vis: async () => ({vis: null}),
  });
  const okValues = (k) => xyGate.p.then(() => Float32Array.from({length: n}, (_, i) => (k === 'f115w' ? 10 : 11) + i / 10));
  const appOf = (client) => ({
    page: {datasets: {cat: {kind: 'catalog'}}}, catalog: async () => client, runtime: {webgl2: false},
    caps: new Set(), afterFirstPaint: () => Promise.resolve(),
    selectionFor: async () => ({count: 0, mask: new Uint8Array(n), onChange: () => () => {}}),
  });
  const ctxOf = (client, id) => ({id, type: 'scatter', config: {x: 'f115w', y: 'f200w'}, store, app: appOf(client), dataset: 'cat',
    setStatus() {}, theme: () => ({}), tools: null});
  const pane = await mountScatter(new FakeEl('div'), ctxOf(mk(okValues), 's1'));
  pane.resize(200, 150, 1);
  let painted = false;
  pane.firstPaint.then(() => { painted = true; });
  await settle();
  assert.equal(painted, false, 'x/y pending');
  xyGate.release();
  await settle(10);
  assert.equal(painted, true);
  pane.destroy();

  const failing = () => Promise.reject(new Error('xy failed'));
  const p2 = await mountScatter(new FakeEl('div'), ctxOf(mk(failing), 's2'));
  p2.resize(200, 150, 1);
  await assert.rejects(p2.firstPaint, /xy failed/);
  p2.destroy();
}));

/**
 * Scatter over catalog `cat` with a context catalog `bg` (context:true) whose manifest lists the
 * count grids `density` (manifest entries); every read of either client is recorded.
 */
async function scatterWithContext(density, config = {x: 'f115w', y: 'f200w'}, opts = {}) {
  globalThis.window.localStorage = fakeStorage();
  globalThis.window.removeEventListener = () => {};
  const store = createStore({});
  const cols = [{key: 'id', role: 'id', enc: 'str'}, {key: 'f115w', role: 'mag', enc: 'f32', label: 'F115W'},
    {key: 'f200w', role: 'mag', enc: 'f32', label: 'F200W'}, {key: 'f444w', role: 'mag', enc: 'f32', label: 'F444W'}];
  const n = 50;
  const mk = (name, man) => {
    const calls = {values: [], bin2d: [], density: []};
    return {
      calls, n, manifest: {cols, ...man}, col: (k) => cols.find((c) => c.key === k) || null, baseKeys: (e) => [e],
      vis: async () => ({vis: null}),
      values: async (k) => { calls.values.push(k); return Float32Array.from({length: n}, (_, i) => 10 + i / 10); },
      bin2d: async (ref, nx, ny) => { calls.bin2d.push(ref); const c = new Float64Array(nx * ny); c[0] = 3; return {counts: c, nx, ny, xr: [9, 16], yr: [9, 16]}; },
      density: async (k) => {
        calls.density.push(k);
        if (opts.densityFails) throw new Error('grid 404');
        const v = new Float32Array(16);
        v[5] = 40; v[6] = 7;
        // a new array per call, as client.density transfers one
        return {values: v, nx: 4, ny: 4, xr: [9, 16], yr: [10, 17], total: 47};
      },
    };
  };
  const fg = mk('cat', {});
  const bg = mk('bg', {density});
  const app = {
    page: {datasets: {cat: {kind: 'catalog'}, bg: {kind: 'catalog', context: true}}},
    catalog: async (k) => (k === 'bg' ? bg : fg), runtime: {webgl2: false},
    caps: new Set(), afterFirstPaint: () => Promise.resolve(),
    selectionFor: async () => ({count: 0, mask: new Uint8Array(n), onChange: () => () => {}}),
  };
  const pane = await mountScatter(new FakeEl('div'), {id: 'sc', type: 'scatter', config, store, app, dataset: 'cat',
    setStatus() {}, theme: () => ({}), tools: null});
  pane.resize(200, 150, 1);
  return {pane, fg, bg, store};
}

const CMD_GRID = {key: 'cmd', x: 'f115w', y: 'f200w', xr: [9, 16], yr: [10, 17], nx: 4, ny: 4, enc: 'f32', lo: 0, hi: 40, file: 'density/cmd.bin'};

test('scatter context: a matching pre-binned grid is drawn without reading context columns', () => withDom(async () => {
  const {pane, bg, fg} = await scatterWithContext([{...CMD_GRID, key: 'stat', stat: 'median:f444w'}, CMD_GRID]);
  let painted = false;
  pane.firstPaint.then(() => { painted = true; });
  await settle(12);
  assert.equal(painted, true);
  assert.deepEqual(bg.calls.density, ['cmd'], 'the count grid, never the stat grid');
  assert.deepEqual(bg.calls.values, [], 'no context column read');
  assert.deepEqual(bg.calls.bin2d, [], 'no worker binning of the context catalog');
  assert.deepEqual(pane.stats().context, {prebinned: 1, binned: 0});
  assert.ok(fg.calls.values.length >= 2, 'foreground x / y read');
  // redraws reuse the decoded counts array: the colourised backdrop comes from the cache
  const h0 = densityCacheStats().hits;
  pane.onVisible(true);   // two full redraws
  await settle(6);
  pane.onVisible(true);
  await settle(6);
  assert.ok(densityCacheStats().hits >= h0 + 2, 'canvas2d colourise cache hit on redraw');
  assert.deepEqual(bg.calls.density, ['cmd'], 'grid fetched once');
  pane.destroy();
}));

test('scatter context: no grid for the axes (or log axes) bins the context columns in the worker', () => withDom(async () => {
  const other = await scatterWithContext([{...CMD_GRID, x: 'f115w', y: 'f444w'}]);
  await settle(12);
  assert.deepEqual(other.bg.calls.density, []);
  assert.equal(other.bg.calls.bin2d.length, 1, 'worker bin2d of the context catalog');
  assert.deepEqual(other.pane.stats().context, {prebinned: 0, binned: 1});
  other.pane.destroy();

  const log = await scatterWithContext([CMD_GRID], {x: 'f115w', y: 'f200w', xlog: true});
  await settle(12);
  assert.deepEqual(log.bg.calls.density, [], 'grids are linear: a log axis bins instead');
  assert.equal(log.bg.calls.bin2d.length, 1);
  log.pane.destroy();
}));

test('scatter context: an unreadable grid falls back to binning its columns', () => withDom(async () => {
  const warn = console.warn;
  const warned = [];
  console.warn = (m) => { warned.push(String(m)); };
  try {
    const r = await scatterWithContext([CMD_GRID], {x: 'f115w', y: 'f200w'}, {densityFails: true});
    await settle(12);
    assert.deepEqual(r.bg.calls.density, ['cmd']);
    assert.equal(r.bg.calls.bin2d.length, 1, 'worker bin2d after the failed grid read');
    assert.deepEqual(r.pane.stats().context, {prebinned: 0, binned: 1});
    assert.ok(warned.some((m) => /context grid bg\/cmd unavailable/.test(m)));
    r.pane.destroy();
  } finally {
    console.warn = warn;
  }
}));

test('manifold: firstPaint resolves at the first projected draw; a failed base load rejects it', () => withDom(async () => {
  globalThis.window.localStorage = fakeStorage();
  globalThis.window.removeEventListener = () => {};
  const store = createStore({});
  const cols = [{key: 'id', role: 'id', enc: 'str'},
    {key: 'a', role: 'mag', enc: 'f32', center: 10, scale: 1}, {key: 'b', role: 'mag', enc: 'f32', center: 11, scale: 1},
    {key: 'c', role: 'mag', enc: 'f32', center: 12, scale: 1}];
  const n = 40;
  const baseGate = gate();
  const col = (k) => Float32Array.from({length: n}, (_, i) => cols.findIndex((c) => c.key === k) * 10 + i / 7);
  const mk = (values) => ({
    n, manifest: {cols}, col: (k) => cols.find((c) => c.key === k) || null,
    values, baseKeys: (e) => [e], vis: async () => ({vis: null}),
    project: async () => ({x: Float32Array.from({length: n}, (_, i) => i / n), y: Float32Array.from({length: n}, (_, i) => (i % 5) / 5), vis: null}),
    density: async () => null,
  });
  const appOf = (client) => ({
    page: {datasets: {cat: {kind: 'catalog'}}}, catalog: async () => client, runtime: {webgl2: false},
    caps: new Set(), afterFirstPaint: () => Promise.resolve(),
    selectionFor: async () => ({count: 0, mask: new Uint8Array(n), onChange: () => () => {}}),
  });
  const ctxOf = (client, id) => ({id, type: 'manifold', config: {dims: ['a', 'b', 'c'], frame: 'pair:0,1', tour: false},
    store, app: appOf(client), dataset: 'cat', setStatus() {}, theme: () => ({}), tools: null});
  const pane = await mountManifold(new FakeEl('div'), ctxOf(mk((k) => baseGate.p.then(() => col(k))), 'm1'));
  pane.resize(200, 150, 1);
  let painted = false;
  pane.firstPaint.then(() => { painted = true; });
  await settle();
  assert.equal(painted, false, 'base columns pending');
  baseGate.release();
  await tick(40);
  await settle(10);
  assert.equal(painted, true);
  pane.destroy();

  const warn = console.warn;
  console.warn = () => {};
  try {
    const p2 = await mountManifold(new FakeEl('div'), ctxOf(mk(() => Promise.reject(new Error('base failed'))), 'm2'));
    p2.resize(200, 150, 1);
    await assert.rejects(p2.firstPaint, /base failed/);
    p2.destroy();
  } finally {
    console.warn = warn;
  }
}));

// WPE8 item 1: spectrum2d.js's loadFocus must use hasRecordOf (exact hasRecord() for a foreign
// id_column set, not the optimistic has()) and must treat a null get() the same way, both
// reporting 'No 2D spectrum for <id>' (status 'none') rather than hanging in 'loading…'.
test('spectrum2d loadFocus: optimistic has() and a null get() both resolve to "no record"', () => withDom(async () => {
  const store = createStore({focus: null});
  const statusCalls = [];
  const rs = {
    has: () => true, // always optimistic, as a foreign id_column set's has() would be
    hasRecord: async (id) => id === 'C',
    get: async (id) => (id === 'C' ? {nx: 2, ny: 2, data: new Uint8Array([1, 2, 3, 4]), enc: 'u8'} : null),
  };
  const ctx = {
    id: 's2d', config: {records: 'spec2d'}, store,
    app: {page: {datasets: {spec2d: {kind: 'records'}}}},
    hub: {records: async () => rs},
    setStatus(...a) { statusCalls.push(a); },
    theme: () => ({}),
  };
  const pane = await mountSpectrum2d(new FakeEl('div'), ctx);
  await settle();
  assert.deepEqual(statusCalls.pop(), [''], 'no focus yet: blank status');

  // A: hasRecord() exact = false even though has() is optimistically true -> 'no record', no get()
  store.set({focus: {ds: 'cat', id: 'A'}});
  await settle();
  assert.deepEqual(statusCalls.pop(), ['none']);
  assert.equal(pane.stats().id, 'A');

  // B: hasRecord() = true but get() resolves null (no row for this key) -> also 'no record'
  store.set({focus: {ds: 'cat', id: 'B'}});
  await settle();
  assert.deepEqual(statusCalls.pop(), ['none']);
  assert.equal(pane.stats().id, 'B');

  // C: hasRecord() = true and get() returns a real 2D record -> loads normally
  store.set({focus: {ds: 'cat', id: 'C'}});
  await settle();
  const last = statusCalls[statusCalls.length - 1];
  assert.equal(last[0], '2×2');
  assert.equal(pane.stats().id, 'C');
  pane.destroy();
}));

// ------------------------------------------------------------------ landing counts (WPE2)

/** Fake fetch over a {url: object} map; records each URL. Unknown URLs answer 404. */
function fakeFetch(docs) {
  const urls = [];
  const enc = new TextEncoder();
  const f = async (u) => {
    urls.push(String(u));
    if (!Object.prototype.hasOwnProperty.call(docs, u)) return {ok: false, status: 404, arrayBuffer: async () => new ArrayBuffer(0)};
    const b = enc.encode(JSON.stringify(docs[u]));
    return {ok: true, status: 200, arrayBuffer: async () => b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength)};
  };
  return {f, urls};
}

async function landingCards(portal, docs = {}) {
  const ff = fakeFetch({'https://example.org/p/portal.json': portal, ...docs});
  setFetch(ff.f);
  try {
    return await withDom(async () => {
      const root = new FakeEl('div');
      const app = {page: {page: {id: 'home'}, datasets: {}}};
      const pane = mountLanding(root, {id: 'land', config: {search: false}, app, setStatus() {}});
      await settle(8);
      const cards = root.querySelectorAll('.pk-card-link');
      const out = {
        urls: ff.urls, stats: pane.stats(),
        counts: Object.fromEntries(cards.map((c) => [c.dataset.page || c.dataset.key, c.querySelector('.pk-card-count').textContent])),
        titles: Object.fromEntries(cards.map((c) => [c.dataset.page || c.dataset.key, c.querySelector('.pk-card-count').title])),
      };
      pane.destroy();
      return out;
    });
  } finally {
    setFetch(null);
  }
}

const LANDING_PAGES = [
  {id: 'home', file: 'index.html', archetype: 'landing'},
  {id: 'cat', file: 'cat.html', archetype: 'catalog'},
  {id: 'img', file: 'img.html', archetype: 'gallery'},
  {id: 'mix', file: 'mix.html', archetype: 'catalog'},
];

test('landing: portal.json counts[] rendered on the cards, no manifest fetched', async () => {
  const r = await landingCards({
    pages: LANDING_PAGES,
    search: [{page: 'cat', ds: 'src', manifest: 'data/src/manifest.json'}],
    counts: [
      {page: 'cat', ds: 'src', kind: 'catalog', n: 1},
      {page: 'img', ds: 'cut', kind: 'images', n: 12, unit: 'cutout'},
      {page: 'mix', ds: 'a', kind: 'catalog', n: 1000},
      {page: 'mix', ds: 'b', kind: 'catalog', n: 234},
      {page: 'mix', ds: 'sp', kind: 'records', n: 1},
    ],
  });
  assert.deepEqual(r.urls, ['https://example.org/p/portal.json'], 'counts present: only portal.json');
  assert.equal(r.stats.counts, 'counts');
  assert.deepEqual(r.counts, {cat: '1 source', img: '12 cutouts', mix: '1,234 sources · 1 spectrum'});
  assert.equal(r.titles.mix, 'a: 1,000 sources\nb: 234 sources\nsp: 1 spectrum');
});

test('landing: empty counts[] leaves the cards blank; no counts falls back to the search manifests', async () => {
  const empty = await landingCards({pages: LANDING_PAGES, counts: [], search: [{page: 'cat', ds: 'src', manifest: 'm.json'}]});
  assert.deepEqual(empty.urls, ['https://example.org/p/portal.json']);
  assert.deepEqual(empty.counts, {cat: '', img: '', mix: ''});

  const fb = await landingCards({
    pages: LANDING_PAGES,
    search: [{page: 'cat', ds: 'src', manifest: 'data/src/manifest.json'}, {page: 'mix', ds: 'gone', manifest: 'data/gone/manifest.json'}],
  }, {'https://example.org/p/data/src/manifest.json': {n: 5, title: 'Sources'}});
  assert.equal(fb.stats.counts, 'search');
  assert.deepEqual(fb.urls.slice().sort(), ['https://example.org/p/data/gone/manifest.json',
    'https://example.org/p/data/src/manifest.json', 'https://example.org/p/portal.json']);
  assert.deepEqual(fb.counts, {cat: '5 sources', img: '', mix: ''}, 'unreadable manifest: card left blank');
  assert.equal(fb.titles.cat, 'Sources: 5 sources');
});
