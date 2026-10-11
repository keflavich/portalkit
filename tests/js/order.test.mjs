// Worker argsort (workers/ops/order.js) and DataClient.order (IMPLEMENTATION_NOTES 74): numeric,
// code, category and natural-string orders; NaN / missing last in both directions; stable ties;
// N = 0 / 1; unicode ids; the engine op over a small JS-built bundle. A 5M-row timing check runs
// only with PK_PERF_JS=1.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {gzipSync} from 'node:zlib';

import {
  normDir, argsortValues, argsortCodes, argsortCategory, argsortNatural,
} from '../../web/js/workers/ops/order.js';
import {DataClient} from '../../web/js/data/client.js';

// ------------------------------------------------------------------ helpers

/** Reference argsort: comparator sort with NaN last and row-index ties. */
function refOrder(n, valid, cmp, dir) {
  const ok = [], bad = [];
  for (let i = 0; i < n; i++) (valid(i) ? ok : bad).push(i);
  ok.sort((a, b) => (dir > 0 ? cmp(a, b) : cmp(b, a)) || a - b);
  return ok.concat(bad);
}

function rng(seed) {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 4294967296;
  };
}

function isPerm(o, n) {
  assert.ok(o instanceof Int32Array);
  assert.equal(o.length, n);
  const seen = new Uint8Array(n);
  for (const i of o) {
    assert.ok(i >= 0 && i < n && !seen[i], `bad permutation entry ${i}`);
    seen[i] = 1;
  }
}

// ------------------------------------------------------------------ pure ops

test('normDir accepts numbers and strings', () => {
  assert.equal(normDir(1), 1);
  assert.equal(normDir(undefined), 1);
  assert.equal(normDir('asc'), 1);
  assert.equal(normDir(-1), -1);
  assert.equal(normDir(-3), -1);
  assert.equal(normDir('desc'), -1);
  assert.equal(normDir('descending'), -1);
});

test('argsortValues: NaN last both ways, infinities at the ends, -0 == +0, stable ties', () => {
  const v = Float32Array.from([3, NaN, -1, Infinity, 0, -0, -Infinity, 3, NaN, 2.5, -1]);
  const asc = Array.from(argsortValues(v, 1));
  assert.deepEqual(asc, [6, 2, 10, 4, 5, 9, 0, 7, 3, 1, 8]);
  const desc = Array.from(argsortValues(v, -1));
  assert.deepEqual(desc, [3, 0, 7, 9, 4, 5, 2, 10, 6, 1, 8]);
  // float64 input and plain arrays take the 64-bit path
  const d = Float64Array.from(v);
  assert.deepEqual(Array.from(argsortValues(d, 1)), asc);
  assert.deepEqual(Array.from(argsortValues(Array.from(v), -1)), desc);
});

test('argsortValues: N = 0, N = 1, all NaN, all equal', () => {
  assert.deepEqual(Array.from(argsortValues(new Float32Array(0))), []);
  assert.deepEqual(Array.from(argsortValues(Float32Array.from([NaN]))), [0]);
  assert.deepEqual(Array.from(argsortValues(Float32Array.from([5]), -1)), [0]);
  assert.deepEqual(Array.from(argsortValues(Float32Array.from([NaN, NaN, NaN]), -1)), [0, 1, 2]);
  assert.deepEqual(Array.from(argsortValues(Float32Array.from([7, 7, 7, 7]), -1)), [0, 1, 2, 3]);
});

test('argsortValues matches a comparator sort on random data (f32 and f64, wide range)', () => {
  const r = rng(7);
  const n = 3000;
  const f64 = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    const u = r();
    f64[i] = u < 0.05 ? NaN : u < 0.1 ? Math.round(r() * 5) : (r() - 0.5) * 10 ** Math.floor(r() * 40 - 20);
  }
  const f32 = Float32Array.from(f64);
  for (const arr of [f32, f64]) {
    for (const dir of [1, -1]) {
      const got = argsortValues(arr, dir);
      isPerm(got, n);
      const want = refOrder(n, (i) => !Number.isNaN(arr[i]), (a, b) => arr[a] - arr[b], dir);
      assert.deepEqual(Array.from(got), want);
    }
  }
});

test('argsortCodes: u8 / u16 / u32 codes with a missing code', () => {
  const u16 = Uint16Array.from([5, 0, 65535, 5, 1, 0, 300]);
  assert.deepEqual(Array.from(argsortCodes(u16, 0, 1)), [4, 0, 3, 6, 2, 1, 5]);
  assert.deepEqual(Array.from(argsortCodes(u16, 0, -1)), [2, 6, 0, 3, 4, 1, 5]);
  const u32 = Uint32Array.from([4294967295, 70000, 3, 4294967294, 70000]);
  assert.deepEqual(Array.from(argsortCodes(u32, 4294967295, 1)), [2, 1, 4, 3, 0]);
  assert.deepEqual(Array.from(argsortCodes(u32, 4294967295, -1)), [3, 1, 4, 2, 0]);
  const u8 = Uint8Array.from([2, 1, 2]);
  assert.deepEqual(Array.from(argsortCodes(u8, null, 1)), [1, 0, 2]);
  assert.deepEqual(Array.from(argsortCodes(new Uint8Array(0), 0)), []);
  // random u16 against the reference
  const r = rng(3);
  const c = Uint16Array.from({length: 2000}, () => (r() < 0.1 ? 0 : 1 + Math.floor(r() * 65534)));
  for (const dir of [1, -1]) {
    assert.deepEqual(Array.from(argsortCodes(c, 0, dir)), refOrder(c.length, (i) => c[i] !== 0, (a, b) => c[a] - c[b], dir));
  }
});

test('argsortCategory: by level label (natural), unlabeled codes after, missing last', () => {
  const levels = ['yso', 'Star', 'agb', 'c10', 'c9'];
  // label order: agb(2) c9(4) c10(3) Star(1) yso(0)
  const codes = Uint8Array.from([0, 1, 255, 2, 3, 4, 7, 2]);
  assert.deepEqual(Array.from(argsortCategory(codes, levels, 255, 1)), [3, 7, 5, 4, 1, 0, 6, 2]);
  // descending reverses the labelled levels only; the unlabeled code (row 6) stays after them
  assert.deepEqual(Array.from(argsortCategory(codes, levels, 255, -1)), [0, 1, 4, 5, 3, 7, 6, 2]);
  const c2 = Uint8Array.from([0, 5, 1, 255, 2]);
  assert.deepEqual(Array.from(argsortCategory(c2, ['b', 'a', 'c'], 255, 1)), [2, 0, 4, 1, 3]);
  assert.deepEqual(Array.from(argsortCategory(c2, ['b', 'a', 'c'], 255, -1)), [4, 0, 2, 1, 3]);
  assert.deepEqual(Array.from(argsortCategory(new Uint16Array(0), [], 65535)), []);
});

test('argsortNatural: numbers inside ids, case, punctuation, missing, long digit runs', () => {
  const ids = ['a10', 'a2', 'A1', 'b', '', null, '10', '9', 'a_1', 'a1',
    'x99999999999999999999', 'x100000000000000000000', 'x8', 'src-é', 'src-e', 'Ünï'];
  const get = (i) => ids[i];
  const asc = Array.from(argsortNatural(ids.length, get, 1)).map((i) => ids[i]);
  assert.deepEqual(asc, ['', '9', '10', 'a_1', 'a1', 'A1', 'a2', 'a10', 'b', 'src-e', 'src-é', 'Ünï',
    'x8', 'x99999999999999999999', 'x100000000000000000000', null]);
  const desc = Array.from(argsortNatural(ids.length, get, -1)).map((i) => ids[i]);
  // descending reverses the order of distinct keys; null stays last
  const want = asc.slice(0, -1).reverse();
  // 'a1' and 'A1' differ only in case (lower case first, as the collator orders them); they are
  // distinct keys, so they swap too
  assert.deepEqual(desc, [...want, null]);
  assert.deepEqual(Array.from(argsortNatural(0, get)), []);
  assert.deepEqual(Array.from(argsortNatural(1, () => 'only')), [0]);
  assert.deepEqual(Array.from(argsortNatural(2, () => null, -1)), [0, 1]);
  // identical strings keep row order in both directions
  assert.deepEqual(Array.from(argsortNatural(3, () => 'same', -1)), [0, 1, 2]);
  // leading zeros compare by value; ties keep row order
  const z = ['x007', 'x7', 'x06'];
  assert.deepEqual(Array.from(argsortNatural(3, (i) => z[i], 1)), [2, 0, 1]);
});

test('argsortNatural agrees with Intl.Collator(numeric) on random lower-case ids', () => {
  const r = rng(11);
  const alpha = 'abc_-.';
  const n = 1500;
  const ids = [];
  for (let i = 0; i < n; i++) {
    let s = '';
    const parts = 1 + Math.floor(r() * 4);
    for (let p = 0; p < parts; p++) {
      if (r() < 0.5) s += alpha[Math.floor(r() * alpha.length)];
      else s += String(1 + Math.floor(r() * 3000));
    }
    ids.push(s);
  }
  const coll = new Intl.Collator('en', {numeric: true});
  for (const dir of [1, -1]) {
    const got = Array.from(argsortNatural(n, (i) => ids[i], dir)).map((i) => ids[i]);
    const want = refOrder(n, () => true, (a, b) => coll.compare(ids[a], ids[b]), dir).map((i) => ids[i]);
    assert.deepEqual(got, want);
  }
});

function asciiSources(list, chunk) {
  const te = new TextEncoder();
  const out = [];
  for (let s0 = 0; s0 < list.length; s0 += chunk) {
    const part = list.slice(s0, s0 + chunk).map((s) => te.encode(s));
    const offsets = new Uint32Array(part.length + 1);
    part.forEach((p, k) => {
      offsets[k + 1] = offsets[k] + p.length;
    });
    const blob = new Uint8Array(offsets[part.length]);
    part.forEach((p, k) => blob.set(p, offsets[k]));
    out.push({start: s0, n: part.length, offsets, blob});
  }
  return out;
}

test('argsortNatural: mixed punctuation / case ids agree with the collator at base strength', () => {
  const r = rng(5);
  const alpha = 'aBcXy+-._ :/';
  const n = 1200;
  const ids = [];
  for (let i = 0; i < n; i++) {
    let s = '';
    const parts = 1 + Math.floor(r() * 6);
    for (let p = 0; p < parts; p++) {
      if (r() < 0.5) s += alpha[Math.floor(r() * alpha.length)];
      else s += String(Math.floor(r() * 100000)).padStart(r() < 0.2 ? 6 : 0, '0');
    }
    ids.push(s);
  }
  const base = new Intl.Collator('en', {numeric: true, sensitivity: 'base'});
  for (const dir of [1, -1]) {
    const o = argsortNatural(n, (i) => ids[i], dir);
    isPerm(o, n);
    for (let k = 1; k < n; k++) {
      const c = base.compare(ids[o[k - 1]], ids[o[k]]);
      assert.ok(dir > 0 ? c <= 0 : c >= 0, `${ids[o[k - 1]]} / ${ids[o[k]]}`);
    }
    // the ASCII byte path gives the same permutation as the string path
    const ob = argsortNatural(n, (i) => ids[i], dir, {sources: asciiSources(ids, 500)});
    assert.deepEqual(Array.from(ob), Array.from(o));
  }
});

test('argsortNatural: positional ids, long shared prefixes, wide weight tables', () => {
  // sexagesimal ids (W51 identity, section 14); the per-row tail is unique
  const r = rng(9);
  const pos = [];
  for (let i = 0; i < 800; i++) {
    const ra = (r() * 86400).toFixed(2).padStart(9, '0');
    const de = (r() * 400000).toFixed(1).padStart(8, '0');
    pos.push(`W51-JW-${ra}${r() < 0.5 ? '+' : '-'}${de}`);
  }
  const coll = new Intl.Collator('en', {numeric: true});
  for (const dir of [1, -1]) {
    const got = Array.from(argsortNatural(pos.length, (i) => pos[i], dir, {sources: asciiSources(pos, 300)}));
    const want = refOrder(pos.length, () => true, (a, b) => coll.compare(pos[a], pos[b]), dir);
    assert.deepEqual(got.map((i) => pos[i]), want.map((i) => pos[i]));
  }
  // long keys with an 80-character shared prefix
  const pre = 'x'.repeat(80);
  const long = [`${pre}b2`, `${pre}a10`, `${pre}a9`, `${pre}a10`, `${pre}`];
  assert.deepEqual(Array.from(argsortNatural(5, (i) => long[i], 1)), [4, 2, 1, 3, 0]);
  assert.deepEqual(Array.from(argsortNatural(5, (i) => long[i], -1)), [0, 1, 3, 2, 4]);
  // more than 255 distinct non-ASCII characters switch to two-byte weights
  const cjk = Array.from({length: 400}, (_, i) => String.fromCodePoint(0x4e00 + ((i * 37) % 400)) + (i % 3));
  const got = Array.from(argsortNatural(cjk.length, (i) => cjk[i], 1)).map((i) => cjk[i]);
  const want = refOrder(cjk.length, () => true, (a, b) => coll.compare(cjk[a], cjk[b]), 1).map((i) => cjk[i]);
  assert.deepEqual(got, want);
});

test('argsortNatural: large groups (16-bit radix digits) and large duplicate / case-variant groups', () => {
  const r = rng(13);
  const n = 40000;   // above the 16-bit digit threshold (32768 rows in one group)
  const vals = ['star', 'Star', 'STAR', 'yso', 'agb', 'a10', 'a9', 'a09', 'x'];
  const ids = Array.from({length: n}, (_, i) => (i % 5 === 0
    ? vals[Math.floor(r() * vals.length)]
    : `src${Math.floor(r() * 1e6)}_${Math.floor(r() * 50)}`));
  const coll = new Intl.Collator('en', {numeric: true});
  const base = new Intl.Collator('en', {numeric: true, sensitivity: 'base'});
  for (const dir of [1, -1]) {
    const o = argsortNatural(n, (i) => ids[i], dir);
    isPerm(o, n);
    for (let k = 1; k < n; k++) {
      const a = o[k - 1], b = o[k];
      const c = base.compare(ids[a], ids[b]);
      assert.ok(dir > 0 ? c <= 0 : c >= 0, `${ids[a]} / ${ids[b]}`);
      // identical strings keep row order; case variants order lower case first (reversed when desc)
      if (ids[a] === ids[b]) assert.ok(a < b);
      else if (c === 0 && ids[a].toLowerCase() === ids[b].toLowerCase()) {
        const t = coll.compare(ids[a], ids[b]);
        assert.ok(dir > 0 ? t < 0 : t > 0, `${ids[a]} / ${ids[b]}`);
      }
    }
    assert.deepEqual(Array.from(argsortNatural(n, (i) => ids[i], dir, {sources: asciiSources(ids, 25000)})), Array.from(o));
  }
});

// ------------------------------------------------------------------ engine op

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

const B = 'https://order.test/cat/';
const IDS = ['s10', 's2', 'S1', 'Ünï', 's1'];
const RA = [10.1, 10.5, 10.9, NaN, 10.5];

function bundle() {
  const bbox = {ra: [10, 11], dec: [-1, 1]};
  const lin = (v, [lo, hi]) => (Number.isFinite(v) ? Math.round(((v - lo) / (hi - lo)) * 4294967294) : 0xffffffff);
  const man = {
    schema: 'pk-catalog/1', id: 'ord', n: 5, order: {kind: 'input'}, chunk_rows: 3,
    ids: {file: 'ids.bin', enc: 'str'},
    pos: {enc: 'u32lin', shuffle: false, files: {ra: 'pos/ra.bin', dec: 'pos/dec.bin'}},
    bbox,
    cols: [
      {key: 'mag', role: 'mag', enc: 'u16q', lo: 10, hi: 665.34, step: 0.01, shuffle: false,
        chunk_files: ['cols/mag.0.bin', 'cols/mag.1.bin']},
      {key: 'emag', role: 'error', enc: 'f32', shuffle: false, file: 'cols/emag.bin'},
      {key: 'cls', role: 'category', enc: 'cat8', levels: ['yso', 'agb', 'star'], file: 'cols/cls.bin'},
      {key: 'name', role: 'text', enc: 'str', file: 'cols/name.bin'},
    ],
    virtual: [],
  };
  const files = new Map([
    ['manifest.json', JSON.stringify(man)],
    ['ids.bin', gz(strPayload(IDS))],
    ['pos/ra.bin', gz(u32b(RA.map((v) => lin(v, bbox.ra))))],
    ['pos/dec.bin', gz(u32b([0, 0, 0, NaN, 0].map((v) => lin(v, bbox.dec))))],
    // mag codes: 12, missing, 11 | 11, 10
    ['cols/mag.0.bin', gz(u16b([201, 0, 101]))],
    ['cols/mag.1.bin', gz(u16b([101, 1]))],
    ['cols/emag.bin', gz(f32b([0.5, 0.1, NaN, 0.2, -0.3]))],
    ['cols/cls.bin', gz(new Uint8Array([0, 2, 1, 255, 1]))],
    ['cols/name.bin', gz(strPayload(['b', 'a10', 'a9', '', 'a']))],
  ]);
  return new Map(Array.from(files, ([k, v]) => [B + k, v]));
}

test('DataClient.order: columns, codes, categories, strings, positions, expressions, ids', async () => {
  const f = fakeFetch(bundle());
  const c = await DataClient.open(B + 'manifest.json', {worker: false, fetch: f});
  const arr = async (...a) => Array.from(await c.order(...a));
  assert.deepEqual(await arr('mag'), [4, 2, 3, 0, 1]);           // ties 2,3 in row order; missing last
  assert.deepEqual(await arr('mag', -1), [0, 2, 3, 4, 1]);
  assert.deepEqual(await arr('mag', 'desc'), [0, 2, 3, 4, 1]);
  assert.deepEqual(await arr('emag'), [4, 1, 3, 0, 2]);
  assert.deepEqual(await arr('emag', -1), [0, 3, 1, 4, 2]);
  assert.deepEqual(await arr('cls'), [2, 4, 1, 0, 3]);           // agb, agb, star, yso, missing
  assert.deepEqual(await arr('cls', -1), [0, 1, 2, 4, 3]);
  // '' is the str missing marker (section 5.3): last in both directions
  assert.deepEqual(await arr('name'), [4, 2, 1, 0, 3]);          // a a9 a10 b ''
  assert.deepEqual(await arr('name', -1), [0, 1, 2, 4, 3]);      // b a10 a9 a ''
  assert.deepEqual(await arr('ra'), [0, 1, 4, 2, 3]);
  assert.deepEqual(await arr('ra', -1), [2, 1, 4, 0, 3]);
  assert.deepEqual(await arr('mag - emag'), [4, 3, 0, 1, 2]);    // 10.3, 10.8, 11.5, NaN, NaN
  assert.deepEqual((await arr('id')).map((i) => IDS[i]), ['s1', 'S1', 's2', 's10', 'Ünï']);
  assert.deepEqual((await arr('id', -1)).map((i) => IDS[i]), ['Ünï', 's10', 's2', 'S1', 's1']);
  assert.deepEqual(await arr('__id'), await arr('id'));
  // the result is a fresh copy: mutating it does not change later answers (worker cache)
  const o = await c.order('mag');
  o.fill(-1);
  assert.deepEqual(await arr('mag'), [4, 2, 3, 0, 1]);
  await assert.rejects(c.order('nope'), /unknown column/);
  const ac = new AbortController();
  ac.abort();
  await assert.rejects(c.order('emag', 1, {signal: ac.signal}), (e) => e.name === 'AbortError');
});

test('order perf: 5M rows in the engine op (PK_PERF_JS=1 only)', {skip: process.env.PK_PERF_JS !== '1'}, () => {
  const n = 5_000_000;
  const r = rng(1);
  const v = new Float32Array(n);
  for (let i = 0; i < n; i++) v[i] = r() < 0.01 ? NaN : r() * 30;
  let t0 = performance.now();
  const o = argsortValues(v, -1);
  const tv = performance.now() - t0;
  assert.equal(o.length, n);
  for (let k = 1; k < 1000; k++) assert.ok(!(v[o[k]] > v[o[k - 1]]));
  const ids = Array.from({length: n}, (_, i) => `gc_${(i * 7919) % n}`);
  t0 = performance.now();
  const oi = argsortNatural(n, (i) => ids[i], 1);
  const ti = performance.now() - t0;
  assert.equal(ids[oi[0]], 'gc_0');
  console.log(`argsort 5M f32 ${tv.toFixed(0)} ms, 5M ids ${ti.toFixed(0)} ms`);
});

test('order perf: 5M positional / multi-run ids (PK_PERF_JS=1 only)', {skip: process.env.PK_PERF_JS !== '1'}, () => {
  const n = 5_000_000;
  const te = new TextEncoder(), td = new TextDecoder();
  // a decodeStr-like table built row by row (no 5M-string array held alongside it)
  function table(gen) {
    const offsets = new Uint32Array(n + 1);
    let blob = new Uint8Array(n * 32);
    for (let i = 0; i < n; i++) {
      const b = te.encode(gen(i));
      if (offsets[i] + b.length > blob.length) {
        const g = new Uint8Array(blob.length * 2);
        g.set(blob);
        blob = g;
      }
      blob.set(b, offsets[i]);
      offsets[i + 1] = offsets[i] + b.length;
    }
    return {start: 0, n, offsets, blob, get: (i) => td.decode(blob.subarray(offsets[i], offsets[i + 1]))};
  }
  const r = rng(2);
  const base = new Intl.Collator('en', {numeric: true, sensitivity: 'base'});
  const timings = {};
  const run = (name, t, dir, opts) => {
    const t0 = performance.now();
    const o = argsortNatural(n, t.get, dir, opts);
    timings[name] = performance.now() - t0;
    for (let k = 1; k < 5000; k++) {
      const c = base.compare(t.get(o[k - 1]), t.get(o[k]));
      assert.ok(dir > 0 ? c <= 0 : c >= 0);
    }
  };
  {
    const pos = table(() => `W51-JW-${(r() * 86400).toFixed(2).padStart(9, '0')}${r() < 0.5 ? '+' : '-'}` +
      `${(r() * 400000).toFixed(1).padStart(8, '0')}`);
    // the worker's id path (ASCII bytes) and its str-column path (decoded strings)
    run('positional bytes', pos, 1, {sources: [pos]});
    const flat = new Array(n);
    for (let i = 0; i < n; i++) flat[i] = pos.get(i);
    run('positional strings desc', {get: (i) => flat[i]}, -1);
  }
  {
    const jw = table((i) => `jw0${1000 + (i % 7000)}-o0${i % 97}_s${(i * 7919) % n}_nirspec`);
    run('jw bytes', jw, 1, {sources: [jw]});
  }
  console.log('argsort 5M ids (ms):', JSON.stringify(Object.fromEntries(Object.entries(timings).map(([k, v]) => [k, Math.round(v)]))));
  // budget per 5M-id sort (the worker runs the same code). Measured 3-6 s per case in a quiet
  // moment and 10-26 s on a heavily shared node, so the limit is configurable.
  const budget = Number(process.env.PK_PERF_ORDER_MS) || 15000;
  for (const [k, v] of Object.entries(timings)) assert.ok(v < budget, `${k}: ${v.toFixed(0)} ms (budget ${budget})`);
});
