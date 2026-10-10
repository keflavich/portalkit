// Record decoders (pks1 / pki1 goldens), RecordStore (shards, single files, png, json-legacy,
// shared grids, LRU, superseded tokens, abort) and TemplateSet loading over a fake fetch.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {gzipSync} from 'node:zlib';

import {
  splitRecord, gridValues, decodePKS1, decodePKI1, pki1Values, decodeRecord, ByteLRU, LRU,
  RecordStore, parseGridFile,
} from '../../web/js/data/records.js';
import {TemplateSet} from '../../web/js/data/templates.js';
import {getStats, resetStats} from '../../web/js/data/fetch.js';

const GC = new URL('../golden/codec/', import.meta.url);
const INDEX = JSON.parse(readFileSync(new URL('index.json', GC), 'utf8'));
const CASES = Object.fromEntries(INDEX.cases.map((c) => [c.name, c]));
const bin = (f) => new Uint8Array(readFileSync(new URL(f, GC)));

function closeRel(got, want, tol, what) {
  if (want === null) {
    assert.ok(Number.isNaN(got), `${what}: expected NaN, got ${got}`);
    return;
  }
  assert.ok(Math.abs(got - want) <= tol * Math.max(1, Math.abs(want)), `${what}: got ${got}, want ${want}`);
}

/** Build a pks1 / pki1 record: magic + u32 header_len (padded to 4) + header JSON + binary. */
function makeRecord(magic, header, binary = new Uint8Array(0), gzip = true) {
  let hj = new TextEncoder().encode(JSON.stringify(header));
  const pad = (4 - (hj.length % 4)) % 4;
  if (pad) {
    const h2 = new Uint8Array(hj.length + pad).fill(0x20);
    h2.set(hj);
    hj = h2;
  }
  const out = new Uint8Array(8 + hj.length + binary.length);
  out.set(new TextEncoder().encode(magic), 0);
  new DataView(out.buffer).setUint32(4, hj.length, true);
  out.set(hj, 8);
  out.set(binary, 8 + hj.length);
  return gzip ? new Uint8Array(gzipSync(out)) : out;
}

const f32bytes = (arr) => new Uint8Array(Float32Array.from(arr).buffer);

/** Fake fetch over a Map url -> bytes|string; counts calls; optional delay. */
function fakeFetch(files, {delay = 0} = {}) {
  const calls = [];
  const fn = async (url, init = {}) => {
    const u = String(url);
    calls.push(u);
    if (delay) {
      await new Promise((resolve, reject) => {
        const t = setTimeout(resolve, delay);
        if (init.signal) {
          init.signal.addEventListener('abort', () => {
            clearTimeout(t);
            reject(new DOMException('aborted', 'AbortError'));
          }, {once: true});
        }
      });
    }
    if (!files.has(u)) return new Response('missing', {status: 404});
    const v = files.get(u);
    return new Response(typeof v === 'string' ? v : v, {status: 200});
  };
  fn.calls = calls;
  return fn;
}

test('pks1 goldens (uniform / array wave, f32, u16q, u16log channels)', () => {
  for (const name of ['pks1_uniform', 'pks1_array']) {
    const c = CASES[name];
    const r = decodePKS1(bin(c.file));
    const e = c.expected;
    assert.equal(r.id, e.header.id);
    assert.equal(r.n, e.header.n);
    assert.deepEqual(r.header, e.header);
    e.wave.forEach((w, i) => closeRel(r.wave[i], w, c.tol_rel, `${name} wave[${i}]`));
    for (const [ch, vals] of Object.entries(e.channels)) {
      assert.ok(r.channels[ch] instanceof Float32Array || r.channels[ch] instanceof Float64Array);
      vals.forEach((v, i) => closeRel(r.channels[ch][i], v, c.tol_rel, `${name} ${ch}[${i}]`));
    }
    assert.equal(decodeRecord(bin(c.file), 'pks1').id, e.header.id);
  }
  const u = decodePKS1(bin(CASES.pks1_uniform.file));
  assert.deepEqual(u.segments, [[0, 3, 'G140M'], [3, 6, 'G235M']]);
  assert.equal(u.lines[0].name, 'Paα');
  assert.equal(u.meta.target, 'src-é');
  assert.equal(u.units.flux, 'Jy');
});

test('pki1 goldens (u16 asinh, u8 linear)', () => {
  for (const name of ['pki1_u16', 'pki1_u8']) {
    const c = CASES[name];
    const im = decodePKI1(bin(c.file));
    const e = c.expected;
    assert.equal(im.nx, e.header.nx);
    assert.equal(im.ny, e.header.ny);
    assert.deepEqual(Array.from(im.data), e.codes);
    assert.deepEqual(Array.from(im.wave).map((x) => +x.toFixed(9)), e.wave);
    assert.deepEqual(Array.from(im.y), e.y);
    const vals = pki1Values(im);
    e.values.forEach((v, i) => closeRel(vals[i], v, 1e-6, `${name} value[${i}]`));
  }
});

test('splitRecord / gridValues / parseGridFile errors and edge cases', () => {
  assert.throws(() => splitRecord(new Uint8Array(3)), /too short/);
  const rec = makeRecord('PKS1', {id: 'x', n: 0, wave: {kind: 'uniform', w0: 1, dw: 1}, channels: []}, undefined, false);
  assert.throws(() => splitRecord(rec, 'PKI1'), /not a pki1 record/);
  const bad = rec.slice();
  new DataView(bad.buffer).setUint32(4, 6, true);
  assert.throws(() => splitRecord(bad, 'PKS1'), /multiple of 4/);
  const trunc = rec.slice();
  new DataView(trunc.buffer).setUint32(4, 4096, true);
  assert.throws(() => splitRecord(trunc, 'PKS1'), /truncated/);
  // N = 0 record decodes to empty arrays
  const r0 = decodePKS1(rec);
  assert.equal(r0.n, 0);
  assert.equal(r0.wave.length, 0);
  assert.deepEqual(Array.from(gridValues({kind: 'uniform', w0: 2, dw: 0.5, n: 3})), [2, 2.5, 3]);
  assert.deepEqual(Array.from(gridValues({kind: 'array', values: [1, 4]})), [1, 4]);
  assert.throws(() => gridValues({kind: 'array', file: 'g.bin'}), /not resolved/);
  assert.deepEqual(Array.from(parseGridFile(new TextEncoder().encode(' [1, 2.5, 1e30]'))), [1, 2.5, 1e30]);
  assert.deepEqual(Array.from(parseGridFile(new TextEncoder().encode('{"values": [3]}'))), [3]);
  assert.deepEqual(Array.from(parseGridFile(new Uint8Array(Float64Array.from([0.5, -7]).buffer))), [0.5, -7]);
  assert.throws(() => parseGridFile(new Uint8Array(5)), /multiple of 8/);
  // shared grid length mismatch / missing grid
  const sh = makeRecord('PKS1', {id: 's', n: 2, wave: {kind: 'shared', grid: 'g'}, channels: [{name: 'flux', enc: 'f32', off: 0}]}, f32bytes([1, 2]));
  assert.throws(() => decodePKS1(sh, {}), /not supplied/);
  assert.throws(() => decodePKS1(sh, {g: [1, 2, 3]}), /3 samples/);
  assert.deepEqual(Array.from(decodePKS1(sh, {g: {kind: 'uniform', w0: 1, dw: 1, n: 2}}).wave), [1, 2]);
  assert.throws(() => decodeRecord(sh, 'nope'), /unknown record kind/);
});

test('LRU and ByteLRU eviction order', () => {
  const l = new LRU(2);
  l.set('a', 1);
  l.set('b', 2);
  assert.equal(l.get('a'), 1); // a is now newest
  l.set('c', 3);
  assert.equal(l.has('b'), false);
  assert.equal(l.get('missing'), undefined);
  assert.equal(l.size, 2);
  l.set('z', 0);
  assert.equal(l.get('z'), 0);
  l.delete('z');
  l.clear();
  assert.equal(l.size, 0);
  const b = new ByteLRU(10);
  b.set('x', new Uint8Array(6));
  b.set('y', new Uint8Array(6));
  assert.equal(b.has('x'), false);
  assert.equal(b.bytes, 6);
  b.set('big', new Uint8Array(100)); // newest entry is kept even when over budget
  assert.equal(b.has('big'), true);
  assert.equal(b.size, 1);
});

test('RecordStore: pks1 shard, single files, png, unknown id, LRU, decode stats', async () => {
  const a = bin(CASES.pks1_uniform.file);
  const b = bin(CASES.pks1_array.file);
  const shard = new Uint8Array(a.length + b.length);
  shard.set(a);
  shard.set(b, a.length);
  const base = 'https://x.test/rec/';
  const files = new Map([
    [base + 'index.json', JSON.stringify({
      schema: 'pk-records/1', kind: 'pks1', n: 4,
      shards: ['shard_000.bin'],
      entries: {
        src1: [0, 0, a.length], 'sé2': [0, a.length, b.length], single: 'one/src1.pks1',
        pic: {png: 'png/pic.png', extent: [0, 1, 0, 1]},
      },
    })],
    [base + 'shard_000.bin', shard],
    [base + 'one/src1.pks1', a],
  ]);
  const f = fakeFetch(files);
  resetStats();
  const rs = await RecordStore.open(base + 'index.json', {fetch: f, recordLRU: 2});
  assert.equal(rs.n, 4);
  assert.equal(rs.has('src1'), true);
  assert.equal(rs.has('nope'), false);
  assert.deepEqual(rs.ids().sort(), ['pic', 'single', 'src1', 'sé2']);
  const [r1, r2] = await Promise.all([rs.get('src1'), rs.get('sé2')]);
  assert.equal(r1.id, 'src1');
  assert.equal(r2.id, 'sé2');
  assert.equal(f.calls.filter((u) => u.endsWith('shard_000.bin')).length, 1, 'shard fetched once');
  assert.equal((await rs.get('single')).id, 'src1');
  const png = await rs.get('pic');
  assert.equal(png.kind, 'png');
  assert.equal(png.png, base + 'png/pic.png');
  assert.equal(await rs.get('nope'), null);
  assert.ok(getStats().fetches >= 3);
  // LRU of 2 records: src1 was evicted, the shard comes from the byte cache (no refetch)
  const n0 = f.calls.length;
  assert.equal((await rs.get('src1')).id, 'src1');
  assert.equal(f.calls.length, n0);
});

test('RecordStore: superseded focus tokens resolve to null, other channels do not interfere', async () => {
  const a = bin(CASES.pks1_uniform.file);
  const base = 'https://x.test/tok/';
  const files = new Map([
    [base + 'index.json', JSON.stringify({kind: 'pks1', entries: {a: 'a.pks1', b: 'b.pks1'}})],
    [base + 'a.pks1', a], [base + 'b.pks1', a],
  ]);
  const rs = await RecordStore.open(base + 'index.json', {fetch: fakeFetch(files, {delay: 5})});
  const p1 = rs.get('a', {token: 1});
  const p2 = rs.get('b', {token: 2});
  const p3 = rs.get('a', {token: 1, channel: 'hover'});
  assert.equal(await p1, null);
  assert.equal((await p2).id, 'src1');
  assert.equal((await p3).id, 'src1');
  // without a token, results always arrive
  assert.equal((await rs.get('a')).id, 'src1');
});

test('RecordStore: abort rejects with AbortError and leaves the store usable', async () => {
  const a = bin(CASES.pks1_uniform.file);
  const base = 'https://x.test/ab/';
  const files = new Map([[base + 'index.json', JSON.stringify({kind: 'pks1', entries: {a: 'a.pks1'}})], [base + 'a.pks1', a]]);
  const rs = await RecordStore.open(base + 'index.json', {fetch: fakeFetch(files, {delay: 20})});
  const ac = new AbortController();
  const p = rs.get('a', {signal: ac.signal});
  ac.abort();
  await assert.rejects(p, (e) => e.name === 'AbortError');
  const pre = new AbortController();
  pre.abort();
  await assert.rejects(rs.get('a', {signal: pre.signal}), (e) => e.name === 'AbortError');
  assert.equal((await rs.get('a')).id, 'src1');
});

test('RecordStore: json shard, json-legacy directory, HTTP errors', async () => {
  const c = CASES.shard_json;
  const base = 'https://x.test/js/';
  const files = new Map([
    [base + 'index.json', JSON.stringify({kind: 'json', shards: ['s0.bin'], entries: c.entries})],
    [base + 's0.bin', bin(c.file)],
    [base + 'legacy/index.json', JSON.stringify({kind: 'json-legacy', dir: 'spec', pattern: '{id}.json'})],
    [base + 'legacy/spec/a%20b.json', JSON.stringify({ok: 'é'})],
    [base + 'broken/index.json', JSON.stringify({kind: 'json', shards: ['gone.bin'], entries: {x: [0, 0, 10], y: [3, 0, 1]}})],
  ]);
  const f = fakeFetch(files);
  const rs = await RecordStore.open(base + 'index.json', {fetch: f});
  for (const [k, v] of Object.entries(c.expected)) assert.deepEqual(await rs.get(k), v);
  const lg = await RecordStore.open(base + 'legacy/index.json', {fetch: f});
  assert.equal(lg.has('anything'), true);
  assert.deepEqual(lg.ids(), []);
  assert.deepEqual(await lg.get('a b'), {ok: 'é'});
  await assert.rejects(lg.get('missing'), (e) => e.name === 'FetchError' && e.status === 404);
  const br = await RecordStore.open(base + 'broken/index.json', {fetch: f});
  await assert.rejects(br.get('x'), (e) => e.status === 404);
  await assert.rejects(br.get('y'), /shard 3 not listed/);
  await assert.rejects(RecordStore.open(base + 'nope/index.json', {fetch: f}), (e) => e.status === 404);
});

test('RecordStore: shared array grid loaded from a file once', async () => {
  const base = 'https://x.test/grid/';
  const rec = (id, vals) => makeRecord('PKS1', {id, n: 3, wave: {kind: 'shared', grid: 'm'}, channels: [{name: 'flux', unit: 'Jy', enc: 'f32', off: 0}]}, f32bytes(vals));
  const files = new Map([
    [base + 'index.json', JSON.stringify({kind: 'pks1', grids: {m: {kind: 'array', file: 'grid_m.bin', n: 3}}, entries: {p: 'p.pks1', q: 'q.pks1'}})],
    [base + 'grid_m.bin', new Uint8Array(gzipSync(Buffer.from(Float64Array.from([1, 2, 4]).buffer)))],
    [base + 'p.pks1', rec('p', [1, NaN, 3])],
    [base + 'q.pks1', rec('q', [1e30, -1e-30, 0])],
  ]);
  const f = fakeFetch(files);
  const rs = await RecordStore.open(base + 'index.json', {fetch: f});
  const [p, q] = await Promise.all([rs.get('p'), rs.get('q')]);
  assert.deepEqual(Array.from(p.wave), [1, 2, 4]);
  assert.ok(Number.isNaN(p.channels.flux[1]));
  assert.equal(q.channels.flux[0], Math.fround(1e30));
  assert.equal(f.calls.filter((u) => u.endsWith('grid_m.bin')).length, 1);
});

test('TemplateSet: grids, templates, laws (1/R_V interpolation) and reddening', async () => {
  const base = 'https://x.test/tpl/';
  const grid = {kind: 'uniform', w0: 1, dw: 1, n: 3};
  const tpl = makeRecord('PKS1', {
    id: 'A0V', n: 3, wave: {kind: 'shared', grid: 'prism'},
    channels: [{name: 'flux', unit: 'Jy', enc: 'f32', off: 0}], meta: {template: 'A0V', grid: 'prism', teff: 9600, logg: 4},
  }, f32bytes([1, 2, 4]));
  const law = (rv, k) => makeRecord('PKS1', {
    id: `law${rv}`, n: 3, wave: {kind: 'shared', grid: 'prism'},
    channels: [{name: 'k', enc: 'f32', off: 0}], meta: {law: 'G23', rv},
  }, f32bytes(k));
  const files = new Map([
    [base + 'index.json', JSON.stringify({
      schema: 'pk-templates/1', grids: {prism: {file: 'grid_prism.0123abcd.json', n: 3}},
      templates: [{key: 'A0V', teff: 9600, logg: 4, label: 'A0V', files: {prism: 'tpl/A0V_prism.pks1'}}],
      laws: {k31: {prism: 'law_k31_prism'}, k55: {prism: 'law_k55_prism'}},
    })],
    [base + 'grid_prism.0123abcd.json', JSON.stringify(grid)],
    [base + 'tpl/A0V_prism.pks1', tpl],
    [base + 'law_k31_prism', law(3.1, [0.5, 0.25, 0.125])],
    [base + 'law_k55_prism', law(5.5, [0.75, 0.375, 0.25])],
  ]);
  const ts = await TemplateSet.open(base + 'index.json', {fetch: fakeFetch(files)});
  assert.deepEqual(ts.gridNames(), ['prism']);
  const t = await ts.template('A0V', 'prism');
  assert.deepEqual(Array.from(t.wave), [1, 2, 3]);
  assert.deepEqual(Array.from(t.flux), [1, 2, 4]);
  await assert.rejects(ts.template('B0V', 'prism'), /unknown template/);
  await assert.rejects(ts.template('A0V', 'medium'), /no medium grid/);
  const l31 = await ts.law(3.1, 'prism');
  assert.deepEqual(Array.from(l31.k), [0.5, 0.25, 0.125]);
  const l4 = await ts.law(4.0, 'prism');
  const f = (1 / 4 - 1 / 3.1) / (1 / 5.5 - 1 / 3.1);
  assert.ok(Math.abs(l4.k[0] - (0.5 + (0.75 - 0.5) * f)) < 1e-6);
  const red = await ts.reddened('A0V', 'prism', 2, 3.1);
  assert.ok(Math.abs(red.flux[0] - 1 * 10 ** (-0.4 * 0.5 * 2)) < 1e-6);
  const same = await ts.reddened('A0V', 'prism', 0, 3.1);
  assert.deepEqual(Array.from(same.flux), [1, 2, 4]);
});
