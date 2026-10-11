// Unit tests for the list / gallery / detail / landing helpers changed in WPI4
// (IMPLEMENTATION_NOTES 74, 76, 81, 85, 93), and the startup reads of the list, hover card and
// sky markers (note 99). Pure functions, plus list mount / hover install / SkyMarkers on a
// minimal fake DOM and fake DataClient (no jsdom).
import test from 'node:test';
import assert from 'node:assert/strict';

import {itemUrl, productUrl, substituteRefs} from '../../web/js/ui/lightbox.js';
import {
  asOrder, ID_SORT, keepMask, normalizeSortSpec, orderKey, parseSort, subsetInOrder,
  afterFirstPaint, cellText, mount as mountList, ROW_BLOCK, ROW_CACHE_ROWS, RowCache, rowBlocks, rowId, rowsIdKey,
} from '../../web/js/panes/list.js';
import {HOVER_CHANNEL, install as installHover, recordColumns, recordExact, recordId, recordValueText} from '../../web/js/ui/hover.js';
import {afterFirstPaint as markersAfterFirstPaint, SkyMarkers} from '../../web/js/sky/markers.js';
import {createStore} from '../../web/js/core/store.js';
import {MISSING} from '../../web/js/core/fmt.js';
import {annotateOptions, catalogSortKey, orderByRank, rankOf} from '../../web/js/panes/gallery.js';
import {searchManifests} from '../../web/js/panes/landing.js';
import {importResultText} from '../../web/js/ui/export.js';
import * as legend from '../../web/js/ui/legend.js';
import {mount as mountTable} from '../../web/js/panes/table.js';
import {mount as mountDetail, propertyText, quantizedKey} from '../../web/js/panes/detail.js';
import {mount as mountSky} from '../../web/js/panes/sky.js';
import {mount as mountWavelength} from '../../web/js/panes/wavelength.js';
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
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; }
  append(...cs) { for (const c of cs) { if (c && typeof c === 'object') { c.parent = this; this.children.push(c); } } }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
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
    return {save: nop, restore: nop, setTransform: nop, clearRect: nop, fillRect: nop, beginPath: nop, arc: nop,
      fill: nop, stroke: nop, moveTo: nop, lineTo: nop, closePath: nop, rect: nop, scale: nop, translate: nop,
      createImageData: (w, h) => ({data: new Uint8ClampedArray(w * h * 4), width: w, height: h}), putImageData: nop,
      drawImage: nop, fillText: nop, measureText: () => ({width: 0})};
  }
  all() { return this.children.flatMap((c) => [c, ...c.all()]); }
  querySelectorAll(sel) {
    const m = /^\.([\w-]+)$/.exec(sel);
    return m ? this.all().filter((e) => e.classList.contains(m[1])) : [];
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

test('list: first screen from rows(), no values() or id() until afterFirstPaint', () => withDom(async () => {
  const h = listHarness();
  await settle();
  assert.deepEqual(h.client.calls.rows, [[0, ROW_BLOCK]], 'one block read for the first screen');
  assert.deepEqual(h.client.calls.values, [], 'no whole column before first paint');
  assert.deepEqual(h.client.calls.id, [], 'ids come from the rows payload');
  const ids = listedIds(h.root);
  assert.equal(ids.length, 12);
  assert.deepEqual(ids.slice(0, 3), ['s0', 's1', 's2']);
  // cells filled from the payload (cls label, f115w number)
  const r1 = h.root.querySelectorAll('.pk-list-row')[1];
  assert.ok(r1.children.some((c) => c.textContent === 'yso'));
  assert.equal(h.pane.stats().colsLoaded, false);

  h.fp.release();
  await settle();
  assert.deepEqual([...h.client.calls.values].sort(), ['cls', 'f115w'], 'shown columns load after first paint');
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
  assert.equal(h.client.calls.values.filter((k) => k === 'cls').length, 2, 'colour column read for the swatches');
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

test('list and hover: an integer-labelled category shows its labels, not shifted levels', () => withDom(async () => {
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
}));

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
