// Unit tests for the list / gallery / detail / landing helpers changed in WPI4
// (IMPLEMENTATION_NOTES 74, 76, 81, 85, 93). Pure functions only; no DOM.
import test from 'node:test';
import assert from 'node:assert/strict';

import {itemUrl, productUrl, substituteRefs} from '../../web/js/ui/lightbox.js';
import {
  asOrder, ID_SORT, keepMask, normalizeSortSpec, orderKey, parseSort, subsetInOrder,
} from '../../web/js/panes/list.js';
import {annotateOptions, catalogSortKey, orderByRank, rankOf} from '../../web/js/panes/gallery.js';
import {searchManifests} from '../../web/js/panes/landing.js';
import {importResultText} from '../../web/js/ui/export.js';
import * as legend from '../../web/js/ui/legend.js';

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
