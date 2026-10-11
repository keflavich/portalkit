// DataClient record channels (IMPLEMENTATION_NOTES 73), hub category default_off at catalog open
// (note 70) and page-relative PNG sidecar URLs for pki1 record sets (note 81), over JS-built
// bundles and a fake fetch (in-thread engine).
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {gzipSync} from 'node:zlib';

import {DataClient} from '../../web/js/data/client.js';
import {createHub, defaultOffCats, isEmptyFilters, withDefaultOff} from '../../web/js/data/hub.js';
import {RecordStore, resolvePageUrl} from '../../web/js/data/records.js';
import {createStore} from '../../web/js/core/store.js';

// ------------------------------------------------------------------ helpers

const gz = (u8) => new Uint8Array(gzipSync(u8));
const u16b = (a) => new Uint8Array(Uint16Array.from(a).buffer);
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

/** Fake fetch over a Map url -> bytes|string; optional per-call delay (ms). */
function fakeFetch(files, {delay = 0} = {}) {
  const calls = [];
  const fn = async (url) => {
    const u = String(url);
    calls.push(u);
    if (delay) await new Promise((r) => setTimeout(r, delay));
    if (!files.has(u)) return new Response('missing', {status: 404});
    return new Response(files.get(u), {status: 200});
  };
  fn.calls = calls;
  return fn;
}

const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));

/** Store whose listeners run on a microtask (no rAF in node). */
function makeStore(initial = {}) {
  return createStore(initial, {schedule: (fn) => queueMicrotask(fn)});
}

async function until(cond, ms = 2000) {
  const t0 = Date.now();
  while (!cond()) {
    if (Date.now() - t0 > ms) throw new Error('timed out waiting for condition');
    await tick(5);
  }
}

// ------------------------------------------------------------------ catalog bundle

const B = 'https://cli.test/p/data/src/';
const IDS = ['r0', 'r1', 'r2', 'r3', 'r4', 'r5'];

function bundle({defaultOff = ['agb', 'not-a-level']} = {}) {
  const man = {
    schema: 'pk-catalog/1', id: 'src', n: 6, order: {kind: 'input'}, chunk_rows: 4,
    ids: {file: 'ids.bin', enc: 'str'},
    cols: [
      {key: 'mag', role: 'mag', enc: 'u16q', lo: 10, hi: 665.34, step: 0.01, shuffle: false,
        chunk_files: ['cols/mag.0.bin', 'cols/mag.1.bin']},
      {key: 'cls', role: 'category', enc: 'cat8', levels: ['star', 'yso', 'agb'], default_off: defaultOff,
        file: 'cols/cls.bin'},
      {key: 'kind', role: 'category', enc: 'cat8', levels: ['x', 'y'], default_off: [], file: 'cols/kind.bin'},
      {key: 'emag', role: 'error', enc: 'f32', shuffle: false, file: 'cols/emag.bin'},
    ],
    virtual: [],
  };
  const files = new Map([
    ['manifest.json', JSON.stringify(man)],
    ['ids.bin', gz(strPayload(IDS))],
    ['cols/mag.0.bin', gz(u16b([1, 101, 201, 301]))],
    ['cols/mag.1.bin', gz(u16b([401, 0]))],
    ['cols/cls.bin', gz(new Uint8Array([0, 2, 1, 2, 255, 0]))],
    ['cols/kind.bin', gz(new Uint8Array([0, 1, 0, 1, 0, 1]))],
    ['cols/emag.bin', gz(f32b([0.1, 0.2, 0.3, 0.4, 0.5, NaN]))],
  ]);
  return new Map(Array.from(files, ([k, v]) => [B + k, v]));
}

// ------------------------------------------------------------------ record channels (note 73)

test('record: a new call on a channel supersedes only that channel (previous resolves null)', async () => {
  const f = fakeFetch(bundle(), {delay: 15});
  const c = await DataClient.open(B + 'manifest.json', {worker: false, fetch: f});
  const p1 = c.record(0, {channel: 'detail'});
  const p2 = c.record(5, {channel: 'detail'});
  const p3 = c.record(1, {channel: 'hover'});
  const p4 = c.record(2);                         // default channel, untokened: always resolves
  assert.equal(await p1, null);
  const r2 = await p2;
  assert.equal(r2._i, 5);
  assert.equal(r2.id, 'r5');
  assert.equal(r2.mag, null);                     // missing u16q code
  assert.equal((await p3)._i, 1);
  assert.equal((await p4)._i, 2);
  // a finished call is not affected by a later one on the same channel
  const a = await c.record(3, {channel: 'detail'});
  const b = await c.record(4, {channel: 'detail'});
  assert.equal(a._i, 3);
  assert.equal(b._i, 4);
  assert.equal(b.cls, null);                       // missing category code
  assert.equal(c._recChannels.size, 0, 'finished channel entries are released');
});

test('record(ds, i, {channel}) form and LRU hits on a channel', async () => {
  const f = fakeFetch(bundle(), {delay: 5});
  const c = await DataClient.open(B + 'manifest.json', {worker: false, fetch: f});
  const r = await c.record('src', 2, {channel: 'sed'});
  assert.equal(r._i, 2);
  assert.equal(r.cls, 'yso');
  const n0 = f.calls.length;
  // a cached row is answered at once (no fetch), before a later call on the channel is issued,
  // so both answers arrive, in call order
  const got = [];
  const q1 = c.record('src', 2, {channel: 'sed'}).then((x) => got.push(x._i));
  const q2 = c.record('src', 0, {channel: 'sed'}).then((x) => got.push(x._i));
  await Promise.all([q1, q2]);
  assert.deepEqual(got, [2, 0]);
  const n1 = f.calls.length;
  assert.equal((await c.record('src', 2, {channel: 'sed'}))._i, 2);
  assert.equal(f.calls.length, n1, 'LRU hit fetches nothing');
  assert.ok(n1 >= n0);
  assert.equal((await c.record(2, {channel: 'sed'}))._i, 2);
});

test('record: default channel keeps token semantics and ignores named channels', async () => {
  const f = fakeFetch(bundle(), {delay: 10});
  const c = await DataClient.open(B + 'manifest.json', {worker: false, fetch: f});
  const t1 = c.record(0, {token: 1});
  const t2 = c.record(1, {token: 2});
  const ch = c.record(2, {channel: 'focus2'});
  assert.equal(await t1, null);
  assert.equal((await t2)._i, 1);
  assert.equal((await ch)._i, 2);
  // a named-channel call does not supersede a tokened default-channel call
  const d1 = c.record(3, {token: 3});
  const d2 = c.record(4, {channel: 'other'});
  assert.equal((await d1)._i, 3);
  assert.equal((await d2)._i, 4);
});

test('record: caller abort rejects with AbortError and leaves the channel usable', async () => {
  const f = fakeFetch(bundle(), {delay: 20});
  const c = await DataClient.open(B + 'manifest.json', {worker: false, fetch: f});
  const ac = new AbortController();
  const p = c.record(1, {channel: 'x', signal: ac.signal});
  ac.abort();
  await assert.rejects(p, (e) => e.name === 'AbortError');
  const pre = new AbortController();
  pre.abort();
  await assert.rejects(c.record(1, {channel: 'x', signal: pre.signal}), (e) => e.name === 'AbortError');
  assert.equal((await c.record(1, {channel: 'x'}))._i, 1);
  // out-of-range rows resolve to null on a channel as on the default channel
  assert.equal(await c.record(99, {channel: 'x'}), null);
  assert.equal(await c.record(-1), null);
});

// ------------------------------------------------------------------ default_off (note 70)

test('defaultOffCats / isEmptyFilters', () => {
  assert.equal(isEmptyFilters(undefined), true);
  assert.equal(isEmptyFilters(null), true);
  assert.equal(isEmptyFilters({}), true);
  assert.equal(isEmptyFilters({cats: {}}), false);
  assert.deepEqual(defaultOffCats({cols: [
    {key: 'a', levels: ['p', 'q', 'é'], default_off: ['q', 'zz', 'q', 'é']},
    {key: 'b', levels: ['p'], default_off: []},
    {key: 'c', default_off: ['p']},
    {key: 'd', levels: [1, 2], default_off: [2]},
  ]}), {a: ['q', 'é'], d: ['2']});
  assert.equal(defaultOffCats({cols: [{key: 'a', levels: ['p'], default_off: ['zz']}]}), null);
  assert.equal(defaultOffCats(null), null);
});

function pageFor(extra = {}) {
  return {
    schema: 'pk-page/1', assets: 'https://cdn.test/pk/v1/', res: 'https://cdn.test/res/',
    datasets: {src: {kind: 'catalog', url: 'data/src/manifest.json'}, ...extra},
    layout: {pane: 'list', dataset: 'src'},
  };
}

test('hub: category default_off applies at catalog open when the page has no filters', async () => {
  const store = makeStore();
  const hub = createHub(pageFor(), store, {base: 'https://cli.test/p/index.html', worker: false, fetch: fakeFetch(bundle())});
  await hub.catalog('src');
  assert.deepEqual(store.get('filters:src'), {cats: {cls: ['agb']}});
  await until(() => store.get('vis:src') && store.get('vis:src').count !== undefined);
  // rows 1 and 3 are agb; row 4 (missing category) stays visible
  assert.equal(store.get('vis:src').count, 4);
  // re-opening (cached client) does not re-apply after the viewer clears the filter
  store.set({'filters:src': {}}, {source: 'test'});
  await hub.catalog('src');
  assert.deepEqual(store.get('filters:src'), {});
  await until(() => store.get('vis:src').count === null);
  hub.dispose();
});

test('withDefaultOff fills only the default_off columns the filters leave undefined', () => {
  const man = {cols: [
    {key: 'cls', levels: ['star', 'yso', 'agb'], default_off: ['agb']},
    {key: 'kind', levels: ['x', 'y'], default_off: ['y']},
  ]};
  assert.deepEqual(withDefaultOff(undefined, man), {cats: {cls: ['agb'], kind: ['y']}});
  assert.deepEqual(withDefaultOff({ranges: {mag: [1, 2]}}, man),
    {ranges: {mag: [1, 2]}, cats: {cls: ['agb'], kind: ['y']}});
  // an explicit array (including [] = "all levels on") wins
  assert.deepEqual(withDefaultOff({cats: {cls: [], kind: ['x']}}, man), {cats: {cls: [], kind: ['x']}});
  assert.deepEqual(withDefaultOff({cats: {cls: []}}, man), {cats: {cls: [], kind: ['y']}});
  const same = {cats: {cls: ['star'], kind: []}};
  assert.equal(withDefaultOff(same, man), same);
  assert.equal(withDefaultOff(same, {cols: []}), same);
});

test('hub: default_off fills undefined columns of hash / carry filters, keeps explicit ones, can be disabled', async () => {
  const s1 = makeStore({'filters:src': {flags: {}, ranges: {mag: [11, null]}}});
  const h1 = createHub(pageFor(), s1, {base: 'https://cli.test/p/index.html', worker: false, fetch: fakeFetch(bundle())});
  await h1.catalog('src');
  // a hash that carries only ranges does not switch default_off off
  assert.deepEqual(s1.get('filters:src'), {flags: {}, ranges: {mag: [11, null]}, cats: {cls: ['agb']}});
  await until(() => s1.get('vis:src'));
  assert.equal(s1.get('vis:src').count, 2);   // mag >= 11: rows 1..4, minus agb rows 1 and 3
  h1.dispose();

  // a viewer who re-enabled the default_off level ({cls: []}) keeps it on after a reload / carry
  const s4 = makeStore({'filters:src': {cats: {cls: []}}});
  const h4 = createHub(pageFor(), s4, {base: 'https://cli.test/p/index.html', worker: false, fetch: fakeFetch(bundle())});
  await h4.catalog('src');
  assert.deepEqual(s4.get('filters:src'), {cats: {cls: []}});
  h4.dispose();

  const s2 = makeStore();
  const h2 = createHub(pageFor(), s2, {base: 'https://cli.test/p/index.html', worker: false, fetch: fakeFetch(bundle()), defaultOff: false});
  await h2.catalog('src');
  assert.equal(s2.get('filters:src'), undefined);
  h2.dispose();

  // a manifest without default_off levels sets nothing
  const s3 = makeStore();
  const h3 = createHub(pageFor(), s3, {base: 'https://cli.test/p/index.html', worker: false, fetch: fakeFetch(bundle({defaultOff: []}))});
  await h3.catalog('src');
  assert.equal(s3.get('filters:src'), undefined);
  h3.dispose();
});

// ------------------------------------------------------------------ png sidecars (note 81)

test('resolvePageUrl substitutes {assets}/{res} and resolves against the page', () => {
  const page = {assets: 'https://cdn.test/pk/v1/', res: 'https://cdn.test/res/'};
  const base = 'https://site.test/portal/spectra_png.html#id=x';
  assert.equal(resolvePageUrl('img/s2d/a b.png', {base, page}), 'https://site.test/portal/img/s2d/a%20b.png');
  assert.equal(resolvePageUrl('{res}s2d/y.png', {base, page}), 'https://cdn.test/res/s2d/y.png');
  assert.equal(resolvePageUrl('{assets}img/z.png', {base, page}), 'https://cdn.test/pk/v1/img/z.png');
  assert.equal(resolvePageUrl('/abs/w.png', {base, page}), 'https://site.test/abs/w.png');
  assert.equal(resolvePageUrl('https://other.test/w.png', {base, page}), 'https://other.test/w.png');
  assert.equal(resolvePageUrl('img/q.png', {fallback: 'https://site.test/portal/data/s2d/index.json'}),
    'https://site.test/portal/data/s2d/img/q.png');
});

function pngIndex() {
  return JSON.stringify({
    schema: 'pk-records/1', kind: 'pki1', layout: 'shard', n: 4, shards: [],
    entries: {
      'src-1': {png: 'img/s2d/src-1.png', extent: {w: [1.66, 3.07], y: [-15, 15]}},
      'src-2': {png: '{res}s2d/src-2.png', extent: {w: [1, 2], y: [0, 1]}},
      'src-3': {png: '{assets}img/src-3.png'},
      'src-é': {png: 'img/s2d/src-%C3%A9.png'},
    },
  });
}

test('hub.records: pki1 PNG sidecars resolve against the page URL (spectra_png fix)', async () => {
  const pageUrl = 'https://cli.test/p/spectra_png.html';
  const files = new Map([['https://cli.test/p/data/s2d/index.json', pngIndex()]]);
  const hub = createHub(pageFor({s2d: {kind: 'records', url: 'data/s2d/index.json', catalog: 'src', record: 'pki1'}}),
    makeStore(), {base: pageUrl, worker: false, fetch: fakeFetch(files)});
  const rs = await hub.records('s2d');
  const r1 = await rs.get('src-1');
  assert.equal(r1.kind, 'png');
  assert.equal(r1.png, 'https://cli.test/p/img/s2d/src-1.png');   // not .../data/s2d/img/s2d/...
  assert.deepEqual(r1.extent, {w: [1.66, 3.07], y: [-15, 15]});
  assert.equal((await rs.get('src-2')).png, 'https://cdn.test/res/s2d/src-2.png');
  const r3 = await rs.get('src-3');
  assert.equal(r3.png, 'https://cdn.test/pk/v1/img/src-3.png');
  assert.equal(r3.extent, null);
  assert.equal((await rs.get('src-é')).png, 'https://cli.test/p/img/s2d/src-%C3%A9.png');
  assert.equal(await rs.get('missing'), null);
  hub.dispose();
});

test('RecordStore: explicit base/page options; without them PNGs resolve against the index', async () => {
  const idx = 'https://cli.test/q/data/s2d/index.json';
  const files = new Map([[idx, pngIndex()]]);
  const rs = await RecordStore.open(idx, {fetch: fakeFetch(files), base: 'https://cli.test/q/page.html', page: {res: 'https://r.test/'}});
  assert.equal((await rs.get('src-1')).png, 'https://cli.test/q/img/s2d/src-1.png');
  assert.equal((await rs.get('src-2')).png, 'https://r.test/s2d/src-2.png');
  const plain = await RecordStore.open(idx, {fetch: fakeFetch(files)});
  assert.equal((await plain.get('src-1')).png, 'https://cli.test/q/data/s2d/img/s2d/src-1.png');
});
