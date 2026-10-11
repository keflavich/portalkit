// Chunked catalogs and startup reads (IMPLEMENTATION_NOTES 98, 99, 101, 102), over hand-built
// gzip bundles and a recording fake fetch (in-thread engine):
// - chunk ranges from the manifest's `chunk_starts` (head chunk = LOD base sample), with the
//   ceil(n / chunk_rows) fallback; prefix loads read only the head chunk
// - id(i) / ids(idx) / record(i) read only the ids chunks that hold the rows; record(i) reads no
//   ids file when the row chunk carries the id; find / order('id') / resolveIds read them all
// - worker manifest prefetch (`data.worker.js?m=<url>`) reused by `open`
// - resolveIds: one pass, exact id first (first row wins), aliases, unicode, duplicates
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {gzipSync} from 'node:zlib';

import {createEngine, catalogChunkStarts, chunkIndexOf, startPrefetch} from '../../web/js/workers/data.worker.js';
import {IdIndex, fnv1a} from '../../web/js/workers/ops/find.js';
import {DataClient, workerUrl} from '../../web/js/data/client.js';
import {whenFirstPaint, visiblePanesLoading, FIRST_PAINT_IDLE_TIMEOUT_MS} from '../../web/js/core/boot.js';

// ------------------------------------------------------------------ helpers

const gz = (u8) => new Uint8Array(gzipSync(u8));
const u16b = (a) => new Uint8Array(Uint16Array.from(a).buffer);
const f32b = (a) => new Uint8Array(Float32Array.from(a).buffer);
const te = new TextEncoder();

function strPayload(list) {
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

function bitsPayload(bits) {
  const out = new Uint8Array(Math.ceil(bits.length / 8));
  bits.forEach((b, i) => {
    if (b) out[i >> 3] |= 1 << (i & 7);
  });
  return out;
}

/** Fake fetch over a Map url -> bytes|string, recording every url asked for. */
function fakeFetch(files) {
  const calls = [];
  const fn = async (url) => {
    const u = String(url);
    calls.push(u);
    if (!files.has(u)) return new Response('missing', {status: 404});
    return new Response(files.get(u), {status: 200});
  };
  fn.calls = calls;
  fn.names = () => calls.map((u) => u.slice(u.lastIndexOf('/') + 1));
  fn.reset = () => {
    calls.length = 0;
  };
  return fn;
}

const ctx = () => ({signal: new AbortController().signal, progress() {}});

// ------------------------------------------------------------------ fixture

const B = 'https://chunks.test/p/data/cat/';
const N = 10;
const STARTS = [0, 3, 7];                        // head chunk = rows [0, 3)
const IDS = ['a0', 'a1', 'Ü2', 'a3', 'dup', 'a5', 'dup', 'a7', 'a8', 'z9'];
const MAG = [1, 2, 3, 0, 5, 6, 7, 8, 9, 10];     // u16q codes (0 = missing)
const ERR = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0];
const CLS = [0, 1, 0, 1, 255, 0, 1, 0, 1, 0];
const OK = [1, 1, 1, 1, 1, 0, 1, 1, 1, 1];       // validity of `flux`
const FLUX = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19];
const IDCOL = [100, 101, 102, 103, 104, 105, 106, 107, 108, 109];
const BBOX = {ra: [10, 11], dec: [-1, 1]};
const RA = [10.0, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9];
const DEC = [-0.9, -0.7, -0.5, -0.3, -0.1, 0.1, 0.3, 0.5, 0.7, 0.9];
const u32b = (a) => new Uint8Array(Uint32Array.from(a).buffer);
const lin = (v, [lo, hi]) => Math.round(((v - lo) / (hi - lo)) * 4294967294);
const NAME = ['n0', 'n1', 'n2', 'n3', 'n4', 'n5', 'n6', 'n7', 'n8', 'n9'];

const pieces = (arr, starts = STARTS, n = N) => starts.map((a, k) => arr.slice(a, k + 1 < starts.length ? starts[k + 1] : n));

/**
 * Bundle with chunk_starts [0, 3, 7]: chunked u16q, cat8, str, bits validity and validity-masked
 * f32 columns, one single-file f32 column, three ids chunks and an alias file.
 * opts: {rows (exact row chunks with the id), chunkStarts (false = drop it), chunkRows, idsFile,
 * pos (single-file u32lin ra/dec), idCol (a chunked f32 stored column keyed 'id')}.
 */
function bundle(opts = {}) {
  const starts = opts.starts || STARTS;
  const cf = (key) => starts.map((_, k) => `cols/${key}.c${k}.bin`);
  const man = {
    schema: 'pk-catalog/1', id: 'cat', n: N, order: {kind: 'lod'}, chunk_rows: opts.chunkRows ?? 1048576,
    ids: opts.idsFile ? {file: 'ids.bin', enc: 'str', alias_file: 'aliases.json'}
      : {file: null, enc: 'str', chunks: starts.map((_, k) => `ids.c${k}.bin`), alias_file: 'aliases.json'},
    cols: [
      {key: 'mag', role: 'mag', enc: 'u16q', lo: 10, hi: 665.34, step: 0.01, shuffle: false, chunk_files: cf('mag')},
      {key: 'err', role: 'error', enc: 'f32', shuffle: false, file: 'cols/err.bin'},
      {key: 'cls', role: 'category', enc: 'cat8', levels: ['star', 'yso'], chunk_files: cf('cls')},
      {key: 'ok', role: 'flag', enc: 'bits', chunk_files: cf('ok')},
      {key: 'flux', role: 'value', enc: 'f32', shuffle: false, validity: 'ok', chunk_files: cf('flux')},
      {key: 'name', role: 'text', enc: 'str', chunk_files: cf('name')},
    ],
    virtual: [],
  };
  if (opts.chunkStarts !== false) man.chunk_starts = starts.slice();
  if (opts.pos) {
    man.pos = {enc: 'u32lin', shuffle: false, files: {ra: 'pos/ra.bin', dec: 'pos/dec.bin'}};
    man.bbox = BBOX;
  }
  if (opts.idCol) man.cols.push({key: 'id', role: 'value', enc: 'f32', shuffle: false, chunk_files: cf('id')});
  if (opts.rows) man.rows = {chunk: 4, files: ['rows/r0.bin', 'rows/r1.bin', 'rows/r2.bin'], cols: ['source_id', 'mag']};
  const files = new Map([['manifest.json', JSON.stringify(man)], ['aliases.json', JSON.stringify({ALIAS7: 7, 'Älias': 2, far: 99})]]);
  pieces(MAG, starts).forEach((p, k) => files.set(`cols/mag.c${k}.bin`, gz(u16b(p))));
  pieces(CLS, starts).forEach((p, k) => files.set(`cols/cls.c${k}.bin`, gz(new Uint8Array(p))));
  pieces(OK, starts).forEach((p, k) => files.set(`cols/ok.c${k}.bin`, gz(bitsPayload(p))));
  pieces(FLUX, starts).forEach((p, k) => files.set(`cols/flux.c${k}.bin`, gz(f32b(p))));
  pieces(NAME, starts).forEach((p, k) => files.set(`cols/name.c${k}.bin`, gz(strPayload(p))));
  files.set('cols/err.bin', gz(f32b(ERR)));
  if (opts.pos) {
    files.set('pos/ra.bin', gz(u32b(RA.map((v) => lin(v, BBOX.ra)))));
    files.set('pos/dec.bin', gz(u32b(DEC.map((v) => lin(v, BBOX.dec)))));
  }
  if (opts.idCol) pieces(IDCOL, starts).forEach((p, k) => files.set(`cols/id.c${k}.bin`, gz(f32b(p))));
  if (opts.idsFile) files.set('ids.bin', gz(strPayload(IDS)));
  else pieces(IDS, starts).forEach((p, k) => files.set(`ids.c${k}.bin`, gz(strPayload(p))));
  if (opts.rows) {
    for (let c = 0; c < 3; c++) {
      const rows = [];
      for (let i = 4 * c; i < Math.min(N, 4 * c + 4); i++) rows.push({source_id: IDS[i], mag: 10 + 0.01 * (MAG[i] - 1) + 0.001});
      files.set(`rows/r${c}.bin`, gz(te.encode(JSON.stringify(rows))));
    }
  }
  return new Map(Array.from(files, ([k, v]) => [B + k, v]));
}

async function openEngine(opts = {}) {
  const f = fakeFetch(opts.files || bundle(opts));
  const eng = createEngine({fetch: f, prefetch: opts.prefetch});
  const info = await eng.handlers.open({url: B + 'manifest.json'}, ctx());
  f.reset();
  return {f, h: eng.handlers, S: eng.state, info};
}

// ------------------------------------------------------------------ chunk geometry

test('catalogChunkStarts: manifest chunk_starts, else ceil(n / chunk_rows) slices', () => {
  assert.deepEqual(catalogChunkStarts({n: 10, chunk_starts: [0, 3, 7]}), [0, 3, 7]);
  assert.deepEqual(catalogChunkStarts({n: 10, chunk_rows: 4}), [0, 4, 8]);
  assert.deepEqual(catalogChunkStarts({n: 8, chunk_rows: 4}), [0, 4]);
  assert.deepEqual(catalogChunkStarts({n: 3, chunk_rows: 4}), [0]);
  assert.deepEqual(catalogChunkStarts({n: 0, chunk_rows: 4}), [0]);
  assert.deepEqual(catalogChunkStarts({n: 1}), [0]);
  assert.deepEqual(catalogChunkStarts({n: 2097153}), [0, 1048576, 2097152]);   // default CHUNK_ROWS
  // LOD layout (note 98): [0, LOD_BASE, LOD_BASE + CHUNK_ROWS, ...]
  assert.deepEqual(catalogChunkStarts({n: 3000000, chunk_starts: [0, 32768, 32768 + 1048576, 32768 + 2 * 1048576]}),
    [0, 32768, 1081344, 2129920]);
  assert.deepEqual(catalogChunkStarts({n: 1, chunk_starts: [0]}), [0]);
  for (const bad of [[], [1, 3], [0, 3, 3], [0, 5, 4], [0, 10], [0, 2.5], [0, -1], ['0'], 'x']) {
    assert.throws(() => catalogChunkStarts({n: 10, chunk_starts: bad}), /chunk_starts/, JSON.stringify(bad));
  }
  assert.throws(() => catalogChunkStarts({n: 10, chunk_rows: 0}), /chunk_rows/);
  assert.throws(() => catalogChunkStarts({n: 10, chunk_rows: 2.5}), /chunk_rows/);
});

test('chunkIndexOf: last start <= i (binary search)', () => {
  const s = [0, 3, 7];
  assert.deepEqual([0, 1, 2, 3, 6, 7, 9, 1e9].map((i) => chunkIndexOf(s, i)), [0, 0, 0, 1, 1, 2, 2, 2]);
  assert.equal(chunkIndexOf([0], 5), 0);
  assert.equal(chunkIndexOf(s, -1), 0);
  const big = Array.from({length: 1000}, (_, k) => k * 7);
  for (const i of [0, 6, 7, 699, 6993, 6999]) assert.equal(chunkIndexOf(big, i), Math.min(999, Math.floor(i / 7)));
});

// ------------------------------------------------------------------ prefix and full loads

test('prefix loads read only the head chunk; full loads every chunk; values match', async () => {
  const {f, h} = await openEngine();
  const v = await h.values({expr: 'mag', prefix: 3}, ctx());
  const head = v.value || v;
  assert.equal(head.length, 3);
  assert.deepEqual(Array.from(head, (x) => Math.round(x * 100) / 100), [10, 10.01, 10.02]);
  assert.deepEqual(f.names(), ['mag.c0.bin']);
  f.reset();
  // prefix 4 reaches into chunk 1 ([3, 7)) only
  await h.ensure({keys: ['cls'], prefix: 4}, ctx());
  assert.deepEqual(f.names().sort(), ['cls.c0.bin', 'cls.c1.bin']);
  f.reset();
  const all = (await h.values({expr: 'mag'}, ctx())).value;
  assert.equal(all.length, N);
  assert.ok(Number.isNaN(all[3]));
  assert.equal(Math.round(all[9] * 100) / 100, 10.09);
  assert.deepEqual(f.names().sort(), ['mag.c1.bin', 'mag.c2.bin']);   // chunk 0 is cached
  f.reset();
  // validity column follows the same chunks; flux row 5 is invalid
  const flux = (await h.values({expr: 'flux'}, ctx())).value;
  assert.deepEqual(Array.from(flux, (x) => (Number.isNaN(x) ? null : x)), [10, 11, 12, 13, 14, null, 16, 17, 18, 19]);
  // prefix 0 loads nothing
  f.reset();
  await h.ensure({keys: ['name'], prefix: 0}, ctx());
  assert.deepEqual(f.calls, []);
  // gpuColumns over the head chunk
  const g = (await h.gpuColumns({keys: ['mag', 'err'], prefix: 3}, ctx())).value;
  assert.equal(g.n, 3);
  assert.deepEqual(Array.from(g.raw[0]), [1, 2, 3]);
  assert.equal(g.raw[1].length, 3);
});

test('without chunk_starts the chunks are ceil(n / chunk_rows) slices (unchanged)', async () => {
  const starts = [0, 4, 8];
  const {f, h} = await openEngine({chunkStarts: false, chunkRows: 4, starts});
  await h.ensure({keys: ['mag'], prefix: 4}, ctx());
  assert.deepEqual(f.names(), ['mag.c0.bin']);
  const all = (await h.values({expr: 'mag'}, ctx())).value;
  assert.equal(Math.round(all[8] * 100) / 100, 10.08);
  f.reset();
  assert.equal(await h.id({i: 9}, ctx()), 'z9');
  assert.deepEqual(f.names(), ['ids.c2.bin']);
});

test('a chunk file count that does not match the chunks is an error', async () => {
  const files = bundle();
  const man = JSON.parse(files.get(B + 'manifest.json'));
  man.cols[0].chunk_files = man.cols[0].chunk_files.slice(0, 2);
  files.set(B + 'manifest.json', JSON.stringify(man));
  const {h} = await openEngine({files});
  await assert.rejects(h.ensure({keys: ['mag']}, ctx()), /mag: 2 chunk files, expected 3/);
  // other columns still work
  const e = (await h.values({expr: 'err'}, ctx())).value;
  assert.equal(e.length, N);
});

test('a malformed chunk_starts fails open', async () => {
  const files = bundle();
  const man = JSON.parse(files.get(B + 'manifest.json'));
  man.chunk_starts = [0, 7, 3];
  files.set(B + 'manifest.json', JSON.stringify(man));
  const eng = createEngine({fetch: fakeFetch(files)});
  await assert.rejects(eng.handlers.open({url: B + 'manifest.json'}, ctx()), /chunk_starts\[2\]/);
});

// ------------------------------------------------------------------ ids per chunk

test('id(i) and ids(idx) read only the ids chunks holding the rows', async () => {
  const {f, h, S} = await openEngine();
  assert.equal(await h.id({i: 2}, ctx()), 'Ü2');
  assert.deepEqual(f.names(), ['ids.c0.bin']);
  assert.equal(await h.id({i: 1}, ctx()), 'a1');               // cached chunk
  assert.deepEqual(f.names(), ['ids.c0.bin']);
  f.reset();
  assert.deepEqual(await h.ids({idx: [0, 8, 9, -1, 10, 1.5]}, ctx()), ['a0', 'a8', 'z9', '', '', '']);
  assert.deepEqual(f.names(), ['ids.c2.bin']);
  assert.equal(S.ids, null, 'no full IdIndex was built');
  f.reset();
  assert.equal(await h.id({i: N}, ctx()), '');
  assert.equal(await h.id({i: -3}, ctx()), '');
  assert.deepEqual(f.calls, []);
});

test('ids chunk with the wrong number of ids is an error; a failed chunk is retried', async () => {
  const files = bundle();
  files.set(B + 'ids.c1.bin', gz(strPayload(['x', 'y'])));
  const {h} = await openEngine({files});
  await assert.rejects(h.id({i: 4}, ctx()), /ids chunk 1: 2 ids, expected 4/);
  files.set(B + 'ids.c1.bin', gz(strPayload(IDS.slice(3, 7))));
  assert.equal(await h.id({i: 4}, ctx()), 'dup');
});

test('single-file ids still work (one chunk for every row)', async () => {
  const {f, h} = await openEngine({idsFile: true});
  assert.equal(await h.id({i: 9}, ctx()), 'z9');
  assert.deepEqual(f.names(), ['ids.bin']);
  const r = await h.resolveIds({ids: ['a8', 'ALIAS7']}, ctx());
  assert.deepEqual(Array.from(r.value), [8, 7]);
});

// ------------------------------------------------------------------ record

test('record(i) on the quantized path reads only the chunks holding row i', async () => {
  const {f, h} = await openEngine();
  const r = await h.record({i: 8}, ctx());
  assert.equal(r._i, 8);
  assert.equal(r._id, 'a8');
  assert.equal(r.id, 'a8');
  assert.equal(r.mag, 10.08);
  assert.equal(r.cls, 'yso');
  assert.equal(r.flux, 18);
  assert.equal(r.name, 'n8');
  assert.equal(r.ok, 1);
  assert.ok(Math.abs(r.err - 0.9) < 1e-6);
  const got = f.names().sort();
  assert.deepEqual(got, ['cls.c2.bin', 'err.bin', 'flux.c2.bin', 'ids.c2.bin', 'mag.c2.bin', 'name.c2.bin', 'ok.c2.bin']);
  // missing values come back null; an invalid row nulls the masked column
  f.reset();
  const r5 = await h.record({i: 5}, ctx());
  assert.equal(r5.flux, null);
  assert.equal(r5.ok, 0);
  const r3 = await h.record({i: 3}, ctx());
  assert.equal(r3.mag, null);
  assert.ok(f.names().every((n) => !/c[02]\.bin$/.test(n)), f.names().join(' '));
  // rows() over a span crossing chunk boundaries
  const rows = await h.rows({i0: 1, i1: 9}, ctx());
  assert.deepEqual(rows.map((o) => o.id), IDS.slice(1, 9));
  assert.deepEqual(rows.map((o) => o.name), NAME.slice(1, 9));
  assert.equal(await h.record({i: N}, ctx()), null);
});

test('record(i) reads no ids file when the row chunk carries the id', async () => {
  const {f, h, S} = await openEngine({rows: true});
  const r = await h.record({i: 6}, ctx());
  assert.equal(r._id, 'dup');
  assert.equal(r.source_id, 'dup');
  assert.deepEqual(f.names(), ['r1.bin']);
  assert.equal(S.idChunks.size, 0);
  assert.equal(S.ids, null);
  // a row chunk whose id is null falls back to the ids chunk of that row
  const files = bundle({rows: true});
  files.set(B + 'rows/r0.bin', gz(te.encode(JSON.stringify([{source_id: null, mag: 1}, {mag: 2}, {source_id: 'a2'}, {source_id: 'a3'}]))));
  const o = await openEngine({files, rows: true});
  assert.equal((await o.h.record({i: 0}, ctx()))._id, 'a0');
  assert.deepEqual(o.f.names(), ['r0.bin', 'ids.c0.bin']);
});

test('quantized rows()/record() read no pos file unless asked; ra/dec fill once pos is decoded', async () => {
  const {f, h, S} = await openEngine({pos: true});
  // first screen: head chunks only, no whole-catalog pos
  const first = await h.rows({i0: 0, i1: 3}, ctx());
  assert.deepEqual(f.names().sort(), ['cls.c0.bin', 'err.bin', 'flux.c0.bin', 'ids.c0.bin', 'mag.c0.bin', 'name.c0.bin', 'ok.c0.bin']);
  assert.ok(first.every((o) => o.ra === null && o.dec === null && o._posDeferred === true));
  assert.deepEqual(first.map((o) => o._id), IDS.slice(0, 3));
  // a hover record before pos is requested
  f.reset();
  const r5 = await h.record({i: 5}, ctx());
  assert.ok(f.names().every((n) => !n.startsWith('ra.') && !n.startsWith('dec.')), f.names().join(' '));
  assert.equal(r5.ra, null);
  assert.equal(r5._posDeferred, true);
  assert.equal(S.pos, null);
  // {pos: true} loads pos once and fills ra/dec
  f.reset();
  const r8 = await h.record({i: 8, pos: true}, ctx());
  assert.deepEqual(f.names().filter((n) => n === 'ra.bin' || n === 'dec.bin').sort(), ['dec.bin', 'ra.bin']);
  assert.ok(Math.abs(r8.ra - 10.8) < 1e-6 && Math.abs(r8.dec - 0.7) < 1e-6);
  assert.equal(r8._posDeferred, undefined);
  // once decoded, every read fills ra/dec without another fetch
  f.reset();
  const again = await h.rows({i0: 0, i1: 2}, ctx());
  assert.ok(Math.abs(again[1].ra - 10.1) < 1e-6 && Math.abs(again[1].dec + 0.7) < 1e-6);
  assert.equal(again[1]._posDeferred, undefined);
  assert.deepEqual(f.names(), []);
});

test('quantized record(i)._id is the catalog id even with a stored column keyed id', async () => {
  const {h} = await openEngine({idCol: true});
  const r = await h.record({i: 2}, ctx());
  assert.equal(r._id, 'Ü2');
  assert.equal(r.id, 102);
  assert.equal(await h.id({i: 2}, ctx()), 'Ü2');
  const rows = await h.rows({i0: 6, i1: 8}, ctx());
  assert.deepEqual(rows.map((o) => o._id), ['dup', 'a7']);
  assert.deepEqual(rows.map((o) => o.id), [106, 107]);
});

test('DataClient: positions by default only after allowPositions(); a deferred cached record is re-read', async () => {
  const f = fakeFetch(bundle({pos: true}));
  const c = await DataClient.open(B + 'manifest.json', {worker: false, fetch: f});
  f.reset();
  const posFetched = () => f.names().filter((n) => n === 'ra.bin' || n === 'dec.bin').length;
  const r0 = await c.record(4, {channel: 'hover:h1'});
  assert.equal(r0.ra, null);
  assert.equal((await c.rows(0, 3))[0].ra, null);
  assert.equal(posFetched(), 0);
  c.allowPositions();
  const r1 = await c.record(4, {channel: 'hover:h1'});
  assert.ok(Math.abs(r1.ra - 10.4) < 1e-6);
  assert.equal(posFetched(), 2);
  assert.ok(Math.abs((await c.rows(0, 3))[2].dec + 0.5) < 1e-6);
  // explicit pos:false still reads ra/dec when pos is already decoded
  assert.ok(Math.abs((await c.record(9, {pos: false})).ra - 10.9) < 1e-6);
  assert.equal(posFetched(), 2);
  c.terminate();
});

// ------------------------------------------------------------------ whole-id ops

test('find, order(id) and resolveIds load every ids chunk once', async () => {
  const {f, h, S} = await openEngine();
  assert.deepEqual(await h.find({text: 'z9'}, ctx()), [9]);
  assert.deepEqual(f.names().sort(), ['aliases.json', 'ids.c0.bin', 'ids.c1.bin', 'ids.c2.bin']);
  assert.ok(S.ids instanceof IdIndex);
  f.reset();
  const o = (await h.order({key: 'id', dir: 1}, ctx())).value;
  assert.equal(o.length, N);
  assert.equal(f.calls.length, 0);
  // ids chunk loaded earlier by id(i) is reused by the full load
  const e2 = await openEngine();
  await e2.h.id({i: 0}, ctx());
  e2.f.reset();
  await e2.h.find({text: 'a'}, ctx());
  assert.deepEqual(e2.f.names().sort(), ['aliases.json', 'ids.c1.bin', 'ids.c2.bin']);
});

test('resolveIds: exact first (first row wins), aliases, unicode, duplicates, missing', async () => {
  const {f, h} = await openEngine();
  const q = ['a8', 'dup', 'Ü2', 'ALIAS7', 'Älias', 'nope', 'dup', '', 'far', 'a8 ', 'A8'];
  const r = (await h.resolveIds({ids: q}, ctx())).value;
  assert.ok(r instanceof Int32Array);
  // 'far' maps to row 99 >= n: rejected; '' and whitespace / case variants never match
  assert.deepEqual(Array.from(r), [8, 4, 2, 7, 2, -1, 4, -1, -1, -1, -1]);
  assert.deepEqual(f.names().sort(), ['aliases.json', 'ids.c0.bin', 'ids.c1.bin', 'ids.c2.bin']);
  const noAlias = (await h.resolveIds({ids: ['ALIAS7', 'a7'], aliases: false}, ctx())).value;
  assert.deepEqual(Array.from(noAlias), [-1, 7]);
  const none = await h.resolveIds({ids: []}, ctx());
  assert.equal(none.length, 0);
  // numbers are looked up as text
  const n = await openEngine({files: (() => {
    const fl = bundle();
    fl.set(B + 'ids.c2.bin', gz(strPayload(['7', '8', '9'])));
    return fl;
  })()});
  assert.deepEqual(Array.from((await n.h.resolveIds({ids: [9, '8', 7.0]}, ctx())).value), [9, 8, 7]);
});

test('resolveIds before open rejects; an exact id wins over an alias with the same text', async () => {
  const eng = createEngine({fetch: fakeFetch(bundle())});
  await assert.rejects(eng.handlers.resolveIds({ids: ['a1']}, ctx()), /not open/);
  const files = bundle();
  files.set(B + 'aliases.json', JSON.stringify({a1: 9}));
  const {h} = await openEngine({files});
  assert.deepEqual(Array.from((await h.resolveIds({ids: ['a1']}, ctx())).value), [1]);
});

test('IdIndex.rowsOf matches a Map reference on a larger random set (hash collisions safe)', () => {
  let s = 12345;
  const rnd = () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 4294967296;
  };
  const n = 20000;
  const ids = Array.from({length: n}, (_, i) => `J${Math.floor(rnd() * 1e6).toString(36)}${i % 7 === 0 ? 'é' : ''}`);
  const half = n / 2;
  const chunks = [
    {start: 0, str: decodeStrList(ids.slice(0, half))},
    {start: half, str: decodeStrList(ids.slice(half))},
  ];
  const ix = new IdIndex(chunks, {aliasX: 5});
  const ref = new Map();
  ids.forEach((id, i) => {
    if (!ref.has(id)) ref.set(id, i);
  });
  const q = [];
  for (let k = 0; k < 3000; k++) q.push(rnd() < 0.8 ? ids[Math.floor(rnd() * n)] : `miss${k}`);
  q.push('aliasX');
  const t0 = Date.now();
  const got = ix.rowsOf(q);
  const ms = Date.now() - t0;
  q.forEach((id, k) => assert.equal(got[k], id === 'aliasX' ? 5 : (ref.has(id) ? ref.get(id) : -1), id));
  assert.ok(ms < 2000, `rowsOf took ${ms} ms`);
  // fnv1a is a stable 32-bit hash
  assert.equal(fnv1a(te.encode('a'), 0, 1), 0xe40c292c);
  assert.equal(fnv1a(new Uint8Array(0), 0, 0), 0x811c9dc5);
});

function decodeStrList(list) {
  const p = strPayload(list);
  const n = list.length;
  const dv = new DataView(p.buffer);
  const offsets = new Uint32Array(n + 1);
  for (let i = 0; i <= n; i++) offsets[i] = dv.getUint32(4 + 4 * i, true);
  const blob = p.subarray(4 + 4 * (n + 1));
  const dec = new TextDecoder();
  return {n, offsets, blob, get: (i) => (i >= 0 && i < n ? dec.decode(blob.subarray(offsets[i], offsets[i + 1])) : '')};
}

// ------------------------------------------------------------------ manifest prefetch

test('startPrefetch: reads ?m=, fetches at once, and open reuses the in-flight request', async () => {
  const f = fakeFetch(bundle());
  const href = workerUrl(B + 'manifest.json', 'https://chunks.test/_pk/v1/js/data/client.js');
  assert.equal(href, `https://chunks.test/_pk/v1/js/workers/data.worker.js?m=${encodeURIComponent(B + 'manifest.json')}`);
  const pf = startPrefetch(href, {fetch: f});
  assert.equal(pf.url, B + 'manifest.json');
  assert.equal(f.calls.length, 1, 'fetch starts before open');
  const eng = createEngine({fetch: f, prefetch: pf});
  const info = await eng.handlers.open({url: B + 'manifest.json'}, ctx());
  assert.equal(info.n, N);
  assert.equal(f.calls.filter((u) => u.endsWith('manifest.json')).length, 1);
  // a different url fetches normally
  const other = createEngine({fetch: f, prefetch: pf});
  await assert.rejects(other.handlers.open({url: B + 'other.json'}, ctx()), /404/);
  // no ?m= (or a relative href): no prefetch
  assert.equal(startPrefetch('https://chunks.test/js/workers/data.worker.js', {fetch: f}), null);
  assert.equal(startPrefetch('data.worker.js?m=x', {fetch: f}), null);
  // a relative m resolves against the worker URL
  const rel = startPrefetch('https://chunks.test/p/data/cat/w.js?m=manifest.json', {fetch: f});
  assert.equal(rel.url, B + 'manifest.json');
  await rel.promise;
});

test('a failed prefetch is retried once by open; a missing manifest still fails', async () => {
  const files = bundle();
  let fail = true;
  const base = fakeFetch(files);
  const f = async (url, o) => {
    if (fail && String(url).endsWith('manifest.json')) {
      fail = false;
      return new Response('busy', {status: 503});
    }
    return base(url, o);
  };
  const pf = startPrefetch(`https://chunks.test/w.js?m=${encodeURIComponent(B + 'manifest.json')}`, {fetch: f});
  const eng = createEngine({fetch: f, prefetch: pf});
  assert.equal((await eng.handlers.open({url: B + 'manifest.json'}, ctx())).n, N);
  const gone = fakeFetch(new Map());
  const pf2 = startPrefetch(`https://chunks.test/w.js?m=${encodeURIComponent(B + 'manifest.json')}`, {fetch: gone});
  const e2 = createEngine({fetch: gone, prefetch: pf2});
  await assert.rejects(e2.handlers.open({url: B + 'manifest.json'}, ctx()), /404/);
  assert.equal(gone.calls.length, 2);
});

// ------------------------------------------------------------------ DataClient

test('DataClient.resolveIds and id over a chunked catalog (in-thread)', async () => {
  const f = fakeFetch(bundle());
  const c = await DataClient.open(B + 'manifest.json', {worker: false, fetch: f});
  f.reset();
  assert.equal(await c.id(3), 'a3');
  assert.deepEqual(f.names(), ['ids.c1.bin']);
  const r = await c.resolveIds(['z9', 'missing', 'ALIAS7', 'dup']);
  assert.ok(r instanceof Int32Array);
  assert.deepEqual(Array.from(r), [9, -1, 7, 4]);
  assert.deepEqual(Array.from(await c.resolveIds(['ALIAS7'], {aliases: false})), [-1]);
  assert.deepEqual(Array.from(await c.resolveIds([])), []);
  const rec = await c.record(0, {channel: 'hover:h1'});
  assert.equal(rec._id, 'a0');
  c.terminate();
});

// ------------------------------------------------------------------ afterFirstPaint (note 100)

function fakeRoot(panes) {
  return {querySelectorAll: (sel) => {
    assert.equal(sel, 'section.pk-pane[data-state="loading"]');
    return panes.filter((p) => p.state === 'loading');
  }};
}
const pane = (state, shown = true) => ({state, offsetParent: shown ? {} : null, getClientRects: () => (shown ? [{}] : [])});

test('visiblePanesLoading: only visible loading panes count', () => {
  assert.equal(visiblePanesLoading(fakeRoot([pane('ready'), pane('loading', false)])), false);
  assert.equal(visiblePanesLoading(fakeRoot([pane('ready'), pane('loading')])), true);
  assert.equal(visiblePanesLoading(null), false);
});

test('whenFirstPaint: waits for loading panes, then one idle callback with timeout 1500', async () => {
  const p = pane('loading');
  const root = fakeRoot([pane('ready'), p, pane('loading', false)]);
  const idles = [];
  let resolved = false;
  const w = whenFirstPaint(root, {idle: (cb, o) => idles.push([cb, o]), poll: 5, MutationObserver: null}).then(() => {
    resolved = true;
  });
  await new Promise((r) => setTimeout(r, 30));
  assert.equal(idles.length, 0);
  p.state = 'ready';
  await new Promise((r) => setTimeout(r, 30));
  assert.equal(idles.length, 1);
  assert.deepEqual(idles[0][1], {timeout: FIRST_PAINT_IDLE_TIMEOUT_MS});
  assert.equal(FIRST_PAINT_IDLE_TIMEOUT_MS, 1500);
  assert.equal(resolved, false, 'resolves at the idle callback');
  idles[0][0]();
  await w;
  assert.equal(resolved, true);
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(idles.length, 1, 'polling stopped');
});

test('whenFirstPaint: mutation observer re-check; max-wait guard releases a stuck pane', async () => {
  let obs = null;
  class MO {
    constructor(cb) {
      obs = this;
      this.cb = cb;
      this.off = false;
    }
    observe(root, o) {
      this.opts = o;
    }
    disconnect() {
      this.off = true;
    }
  }
  const p = pane('loading');
  const w = whenFirstPaint(fakeRoot([p]), {idle: (cb) => cb(), poll: 1e6, MutationObserver: MO});
  assert.ok(obs && obs.opts.attributeFilter.includes('data-state'));
  p.state = 'error';
  obs.cb();
  await w;
  assert.equal(obs.off, true);
  const warn = console.warn;
  const msgs = [];
  console.warn = (m) => msgs.push(m);
  try {
    const t0 = Date.now();
    await whenFirstPaint(fakeRoot([pane('loading')]), {idle: (cb) => cb(), poll: 5, maxWaitMs: 40, MutationObserver: null});
    assert.ok(Date.now() - t0 >= 35);
  } finally {
    console.warn = warn;
  }
  assert.equal(msgs.length, 1);
  assert.match(msgs[0], /still loading/);
});
