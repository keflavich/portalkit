// Golden codec cases (tests/golden/codec/index.json) decoded by web/js/data/codec.js + inflate.js.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import zlib from 'node:zlib';
import {
  isGzip, gunzip, gunzipSlice, unshuffle, decodeColumn, dequant, decodeSparse, sparseKind, decodeStr,
  decodePos, decodeDensity, densityCount, setUseDecompressionStream, stepOf,
} from '../../web/js/data/codec.js';
import {gunzipSync, gzipMembers, inflateRaw, crc32, InflateError} from '../../web/js/data/inflate.js';
import {cacheModeFor} from '../../web/js/data/fetch.js';

const REPO = fileURLToPath(new URL('../../', import.meta.url));
const GOLD = REPO + 'tests/golden/codec/';
const INDEX = JSON.parse(readFileSync(GOLD + 'index.json', 'utf8'));
const CASES = Object.fromEntries(INDEX.cases.map((c) => [c.name, c]));
const bin = (f) => new Uint8Array(readFileSync(GOLD + f));

function near(a, e, tol, rel, msg) {
  const lim = (tol || 0) + (rel || 0) * Math.abs(e);
  assert.ok(Math.abs(a - e) <= lim || a === e, `${msg}: ${a} vs ${e} (tol ${lim})`);
}

test('golden index schema', () => {
  assert.equal(INDEX.schema, 'pk-golden-codec/1');
  assert.ok(INDEX.cases.length >= 20);
});

test('gzip magic and both inflate paths agree on every golden file', async () => {
  for (const c of INDEX.cases) {
    const files = c.file ? [c.file] : Object.values(c.files);
    for (const f of files) {
      const b = bin(f);
      assert.ok(isGzip(b), `${f} gzip magic`);
      const ref = new Uint8Array(zlib.gunzipSync(b));
      const fallback = gunzipSync(b);
      assert.deepEqual(fallback, ref, `${f} inflate.js`);
      setUseDecompressionStream(true);
      if (c.kind !== 'shard') assert.deepEqual(await gunzip(b), ref, `${f} DecompressionStream`);
      setUseDecompressionStream(false);
      assert.deepEqual(await gunzip(b), ref, `${f} gunzip fallback`);
      setUseDecompressionStream(true);
    }
  }
  const plain = new Uint8Array([1, 2, 3]);
  assert.equal(isGzip(plain), false);
  assert.equal(isGzip(new Uint8Array(0)), false);
  assert.equal(await gunzip(plain), plain, 'non-gzip passes through');
});

test('column goldens: codes, dequant (double) and Float32 values', () => {
  for (const c of INDEX.cases.filter((x) => x.kind === 'column')) {
    const out = decodeColumn(c.meta, bin(c.file), c.n);
    const enc = c.meta.enc;
    if (enc === 'str') {
      assert.deepEqual(out.strings, c.expected, c.name);
      assert.equal(out.str.n, c.n);
      for (let i = 0; i < c.n; i++) assert.equal(out.str.get(i), c.expected[i]);
      assert.equal(out.str.get(-1), '');
      assert.equal(out.str.get(c.n), '');
      continue;
    }
    if (enc === 'bits') {
      assert.deepEqual(Array.from(out.bits), c.expected, c.name);
      assert.equal(out.bits.length, c.n);
      continue;
    }
    if (enc === 'cat8' || enc === 'cat16') {
      assert.deepEqual(Array.from(out.codes), c.codes, c.name);
      const lv = c.meta.levels;
      const labels = Array.from(out.codes, (k) => (k === out.missing ? null : lv[k]));
      assert.deepEqual(labels, c.expected, c.name);
      out.codes.forEach((k, i) => assert.equal(Number.isNaN(out.values[i]), k === out.missing));
      continue;
    }
    assert.equal(out.values.length, c.n, c.name);
    if (c.codes) assert.deepEqual(Array.from(out.codes), c.codes, `${c.name} codes`);
    c.expected.forEach((e, i) => {
      const v = out.values[i];
      if (e === null) {
        assert.ok(Number.isNaN(v), `${c.name}[${i}] missing`);
        if (c.codes) assert.ok(Number.isNaN(dequant(c.meta, c.codes[i])));
        return;
      }
      if (c.codes) near(dequant(c.meta, c.codes[i]), e, c.tol, c.tol_rel, `${c.name}[${i}] dequant`);
      near(v, e, enc === 'f32' ? 0 : 0, 1.2e-7, `${c.name}[${i}] f32`);
    });
  }
});

test('u16q saturate limits round trip through sparse', () => {
  const c = CASES.u16q_saturate;
  const lim = Object.entries(c.limits).map(([i, k]) => [+i, k]);
  const raw = new Uint8Array(4 + 5 * lim.length);
  const dv = new DataView(raw.buffer);
  dv.setUint32(0, lim.length, true);
  lim.forEach(([i, k], j) => {
    dv.setUint32(4 + 4 * j, i, true);
    raw[4 + 4 * lim.length + j] = k;
  });
  const sp = decodeSparse(new Uint8Array(zlib.gzipSync(raw)));
  assert.equal(sparseKind(sp, 0), 3);
  assert.equal(sparseKind(sp, 4), 4);
  assert.equal(sparseKind(sp, 2), 0);
});

test('sparse golden', () => {
  const c = CASES.sparse_limits;
  const sp = decodeSparse(bin(c.file));
  assert.deepEqual(Array.from(sp.idx), c.expected.idx);
  assert.deepEqual(Array.from(sp.kind), c.expected.kind);
  c.expected.idx.forEach((i, j) => assert.equal(sparseKind(sp, i), c.expected.kind[j]));
  assert.equal(sparseKind(sp, 3), 0);
  assert.equal(sparseKind(sp, 1e9), 0);
  const empty = decodeSparse(new Uint8Array(4));
  assert.equal(empty.idx.length, 0);
  assert.equal(sparseKind(empty, 0), 0);
  assert.throws(() => decodeSparse(new Uint8Array([5, 0, 0, 0])), RangeError);
});

test('position goldens (u32lin with bbox, i16off with RA wrap)', () => {
  const a = CASES.pos_u32lin;
  const files = Object.fromEntries(Object.entries(a.files).map(([k, f]) => [k, bin(f)]));
  assert.throws(() => decodePos(a.meta, files), /bbox/);
  const p = decodePos(a.meta, files, a.bbox);
  const p2 = decodePos({...a.meta, bbox: a.bbox}, files);
  assert.deepEqual(p.ra, p2.ra);
  for (const k of ['ra', 'dec']) {
    a.expected[k].forEach((e, i) => {
      if (e === null) assert.ok(Number.isNaN(p[k][i]));
      else near(p[k][i], e, a.tol, 0, `u32lin ${k}[${i}]`);
      const inp = a.input[k][i];
      if (inp !== null && e !== null) near(p[k][i], inp, a.tol_deg, 0, `u32lin ${k}[${i}] vs input`);
    });
  }

  const b = CASES.pos_i16off;
  const fb = Object.fromEntries(Object.entries(b.files).map(([k, f]) => [k, bin(f)]));
  const q = decodePos(b.meta, fb);
  for (const k of ['ra', 'dec']) {
    b.expected[k].forEach((e, i) => {
      if (e === null) return assert.ok(Number.isNaN(q[k][i]));
      near(q[k][i], e, b.tol, 0, `i16off ${k}[${i}]`);
    });
  }
  b.input.ra.forEach((inp, i) => {
    if (inp === null || b.expected.ra[i] === null) return;
    let d = Math.abs(((q.ra[i] - inp + 540) % 360) - 180);
    d *= Math.cos((b.input.dec[i] * Math.PI) / 180);
    assert.ok(d <= b.tol_deg + 1e-12, `i16off ra[${i}] wrap distance ${d}`);
  });
  assert.throws(() => decodePos({enc: 'healpix'}, {}), /unknown/);
});

test('density goldens (counts and binned statistic)', () => {
  const c = CASES.density_cmd;
  const d = decodeDensity(c.meta, bin(c.file));
  assert.equal(d.nx * d.ny, c.n);
  assert.deepEqual(Array.from(d.codes), c.codes);
  c.expected.forEach((e, i) => {
    near(densityCount(c.meta, c.codes[i]), e, 0, c.tol_rel, `density[${i}]`);
    near(d.values[i], e, 0, 1.2e-7, `density f32[${i}]`);
  });
  const m = CASES.density_med;
  const dm = decodeDensity(m.meta, bin(m.file));
  m.expected.forEach((e, i) => {
    if (e === null) assert.ok(Number.isNaN(dm.values[i]));
    else assert.equal(dm.values[i], Math.fround(e));
  });
  assert.equal(dm.stat, 'median');
  assert.throws(() => decodeDensity({...c.meta, nx: 3}, bin(c.file)), /cells/);
});

test('unshuffle round trip and errors', () => {
  const n = 1000;
  const plain = new Uint8Array(4 * n);
  for (let i = 0; i < plain.length; i++) plain[i] = (i * 7919) & 255;
  for (const w of [2, 4]) {
    const sh = new Uint8Array(plain.length);
    const m = plain.length / w;
    for (let i = 0; i < m; i++) for (let p = 0; p < w; p++) sh[p * m + i] = plain[i * w + p];
    assert.deepEqual(unshuffle(sh, w), plain);
  }
  assert.deepEqual(unshuffle(new Uint8Array(0), 2), new Uint8Array(0));
  assert.throws(() => unshuffle(new Uint8Array(3), 2), RangeError);
  assert.throws(() => unshuffle(new Uint8Array(4), 3), RangeError);
});

test('dequant edge cases and derived step', () => {
  assert.equal(stepOf({enc: 'u16q', lo: 0, hi: 65534}), 1);
  assert.equal(stepOf({enc: 'u8q', lo: 0, hi: 254}), 1);
  assert.equal(dequant({enc: 'u16q', lo: 0, hi: 65534}, 11), 10);
  assert.equal(dequant({enc: 'u32', lo: 0}, 0xffffffff).toString(), 'NaN');
  assert.equal(dequant({enc: 'u32lin', lo: 0, hi: 4294967294}, 7), 7);
  assert.ok(Number.isNaN(dequant({enc: 'cat8'}, 255)));
  assert.equal(dequant({enc: 'cat16'}, 300), 300);
  assert.throws(() => dequant({enc: 'zzz'}, 1));
  // N = 0 columns
  const empty16 = decodeColumn({enc: 'u16q', lo: 0, hi: 1, shuffle: true}, new Uint8Array(0));
  assert.equal(empty16.values.length, 0);
  const emptyBits = decodeColumn({enc: 'bits'}, new Uint8Array(0), 0);
  assert.equal(emptyBits.bits.length, 0);
  assert.throws(() => decodeColumn({enc: 'bits'}, new Uint8Array(1), 9), RangeError);
  assert.throws(() => decodeColumn({enc: 'nope'}, new Uint8Array(0)), /unsupported/);
  // large u16log values stay finite in Float32 up to the f32 range
  const big = decodeColumn({enc: 'u16log', lo: 30, hi: 38, step: 8 / 65534, shuffle: false},
    new Uint8Array([0xff, 0xff]));
  assert.ok(Math.abs(big.values[0] / 1e38 - 1) < 1e-6);
});

test('str with unicode built locally', () => {
  const strs = ['', 'J1745−2900', '漢字', 'a'.repeat(300)];
  const enc = new TextEncoder();
  const parts = strs.map((s) => enc.encode(s));
  const total = parts.reduce((a, p) => a + p.length, 0);
  const raw = new Uint8Array(4 + 4 * (strs.length + 1) + total);
  const dv = new DataView(raw.buffer);
  dv.setUint32(0, strs.length, true);
  let o = 0;
  parts.forEach((p, i) => {
    dv.setUint32(4 + 4 * i, o, true);
    raw.set(p, 4 + 4 * (strs.length + 1) + o);
    o += p.length;
  });
  dv.setUint32(4 + 4 * strs.length, o, true);
  const s = decodeStr(new Uint8Array(zlib.gzipSync(raw)));
  assert.deepEqual(s.all(), strs);
  assert.throws(() => decodeStr(raw.subarray(0, 6)), RangeError);
});

test('inflate.js: members, stored blocks, corruption', () => {
  const sh = bin('shard_json.bin');
  const members = gzipMembers(sh);
  assert.equal(members.length, 3);
  const ent = CASES.shard_json.entries;
  const want = Object.values(ent).map(([, off, len]) => [off, off + len]);
  assert.deepEqual(members, want);
  for (const [k, [, off, len]] of Object.entries(ent)) {
    const txt = new TextDecoder().decode(gunzipSlice(sh, off, len));
    assert.deepEqual(JSON.parse(txt), CASES.shard_json.expected[k]);
  }
  // stored (level 0) and large dynamic blocks
  const data = new Uint8Array(200000);
  for (let i = 0; i < data.length; i++) data[i] = (i % 251) ^ (i >> 9);
  for (const level of [0, 9]) {
    assert.deepEqual(gunzipSync(new Uint8Array(zlib.gzipSync(data, {level}))), data);
    assert.deepEqual(inflateRaw(new Uint8Array(zlib.deflateRawSync(data, {level}))), data);
  }
  assert.deepEqual(gunzipSync(new Uint8Array(zlib.gzipSync(new Uint8Array(0)))), new Uint8Array(0));
  // header flags: FNAME + FCOMMENT + FEXTRA
  const g = new Uint8Array(zlib.gzipSync(new TextEncoder().encode('héllo')));
  const hdr = [0x1f, 0x8b, 8, 4 | 8 | 16, 0, 0, 0, 0, 0, 255, 2, 0, 0xaa, 0xbb, 0x6e, 0x6d, 0, 0x63, 0];
  const g2 = new Uint8Array(hdr.length + g.length - 10);
  g2.set(hdr);
  g2.set(g.subarray(10), hdr.length);
  assert.equal(new TextDecoder().decode(gunzipSync(g2)), 'héllo');
  // corrupt CRC and truncation are errors
  const bad = g.slice();
  bad[bad.length - 8] ^= 1;
  assert.throws(() => gunzipSync(bad), InflateError);
  assert.throws(() => gunzipSync(g.subarray(0, g.length - 4)), InflateError);
  assert.equal(crc32(new TextEncoder().encode('123456789')), 0xcbf43926);
});

test('gunzip: corrupt or truncated input raises without an unhandled rejection', async () => {
  const seen = [];
  const hook = (err) => seen.push(err);
  process.on('unhandledRejection', hook);
  try {
    setUseDecompressionStream(true);
    const good = new Uint8Array(zlib.gzipSync(new TextEncoder().encode('hello world '.repeat(200))));
    const corrupt = good.slice();
    corrupt[20] ^= 0xff;
    corrupt[21] ^= 0xff;
    const trunc = good.slice(0, good.length - 6);
    for (const [name, b] of [['corrupt', corrupt], ['truncated', trunc]]) {
      await assert.rejects(gunzip(b), (e) => e instanceof Error, name);
    }
    // let any stray rejection surface
    await new Promise((r) => setTimeout(r, 20));
    assert.deepEqual(seen.map(String), [], 'no unhandled rejections');
    assert.deepEqual(await gunzip(good), new Uint8Array(zlib.gunzipSync(good)));
  } finally {
    process.off('unhandledRejection', hook);
  }
});

test('fetch: cacheModeFor treats content-hashed names (incl. record files r/<h8>.bin) as cacheable', () => {
  assert.equal(cacheModeFor('https://x/data/spec/r/1a2b3c4d.bin'), 'default');
  assert.equal(cacheModeFor('https://x/data/spec/s000.1a2b3c4d.bin'), 'default');
  assert.equal(cacheModeFor('r/1a2b3c4d.bin?v=1'), 'default');
  assert.equal(cacheModeFor('https://x/data/spec/manifest.json'), 'no-cache');
  assert.equal(cacheModeFor('https://x/cat/cols/mag.0.bin'), 'no-cache');
  assert.equal(cacheModeFor('https://x/cat/x1a2b3c4d.bin'), 'no-cache');
});
