// Tests for web/js/math/fold.js (pure): folding virtual dims into base-column weights.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {
  compileLinear, fold, directEval, projectRows, foldCamera, foldedFrame, foldFade, frameRowsOf,
  oneHotRows, scatterDims, SUPPORT_EPS,
} from '../../web/js/math/fold.js';
import {FADE_WIDTH} from '../../web/js/core/constants.js';
import {compile} from '../../web/js/data/expr.js';
import {readFileSync} from 'node:fs';

function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Σ_b W[r,b]·base_b + K_r for one record. */
function foldedEval(f, rec, r) {
  let v = f.c[r];
  f.terms[r].keys.forEach((k, i) => { v += f.terms[r].w[i] * rec[k]; });
  return v;
}

const plain = (w) => ({...w});

test('compileLinear parses the linear grammar', () => {
  const one = compileLinear('f182m');
  assert.deepEqual([plain(one.w), one.c, one.keys], [{f182m: 1}, 0, ['f182m']]);
  const a = compileLinear('f182m-f410m');
  assert.deepEqual(plain(a.w), {f182m: 1, f410m: -1});
  const b = compileLinear(' 0.5*f405n + 0.5 * f410m ');
  assert.deepEqual(plain(b.w), {f405n: 0.5, f410m: 0.5});
  const c = compileLinear('0 - a + 2.5e-1*b - 3');
  assert.deepEqual(plain(c.w), {a: -1, b: 0.25});
  assert.equal(c.c, -3);
  const d = compileLinear('x - x + y');
  assert.deepEqual(plain(d.w), {x: 0, y: 1}, 'cancelled keys stay with weight 0');
  assert.deepEqual(d.keys, ['x', 'y']);
  const o = compileLinear({w: {a: 2, b: 0, c: NaN}, c: '1.5'});
  assert.deepEqual([plain(o.w), o.c, o.keys], [{a: 2, b: 0}, 1.5, ['a', 'b']]);
  assert.deepEqual(compileLinear('c_f182m_f410m').keys, ['c_f182m_f410m']);
  assert.deepEqual(compileLinear('ks.mag').keys, ['ks.mag']);
  // keys named like Object.prototype members are ordinary keys
  const p = compileLinear('constructor-toString');
  assert.deepEqual(plain(p.w), {constructor: 1, toString: -1});
});

test('compileLinear rejects malformed and non-linear input', () => {
  for (const bad of ['', '   ', 'a*b', '2*', 'a +', '3 4', 'a-', 'f182m−f410m', 'λ1+λ2', 'a/b', '(a)',
    '-f410m', '+f410m', '1e999*x', '1e999', null, undefined, 3]) {
    assert.throws(() => compileLinear(bad), SyntaxError, `input ${String(bad)}`);
  }
});

test('compileLinear agrees with the shared grammar vectors (portalkit/data/expr_vectors.json)', () => {
  const url = new URL('../../portalkit/data/expr_vectors.json', import.meta.url);
  const doc = JSON.parse(readFileSync(url, 'utf8'));
  const vecs = doc.compile || doc.vectors || doc;
  assert.ok(Array.isArray(vecs) && vecs.length > 10, 'compile vectors present');
  let n = 0;
  for (const v of vecs) {
    if (typeof v.expr !== 'string') continue;
    n++;
    if (v.error) {
      assert.throws(() => compileLinear(v.expr), SyntaxError, `should reject ${JSON.stringify(v.expr)}`);
      continue;
    }
    const got = compileLinear(v.expr);
    const ref = compile(v.expr);
    assert.deepEqual(got.keys, ref.keys, v.expr);
    if (Array.isArray(v.keys)) assert.deepEqual(got.keys, v.keys, v.expr);
    assert.deepEqual(plain(got.w), plain(ref.w), v.expr);
    assert.equal(got.c, ref.c, v.expr);
  }
  assert.ok(n > 10);
});

test('fold(W·base + K) equals direct evaluation for random frames, virtual dims and dm', () => {
  const R = rng(7);
  const bases = ['f115w', 'f182m', 'f212n', 'f405n', 'f410m', 'f444w'];
  const dims = [
    {key: 'f115w', center: 18.2, scale: 1.7},
    {key: 'f182m', center: 16.5, scale: 1.1},
    {key: 'c1', expr: 'f182m-f410m', center: 0.8, scale: 0.6},
    {key: 'c2', expr: '0.5*f405n+0.5*f410m - 1', center: 14, scale: 2},
    {key: 'q', expr: 'f212n - 1.3*f115w + 0.3*f444w + 2', center: -3, scale: 0.4},
    {key: 'f444w', center: 0, scale: 0},     // zero scale is treated as 1
  ];
  const isMag = new Set(bases);
  for (let trial = 0; trial < 25; trial++) {
    const rows = [{}, {}];
    for (const d of dims) {
      rows[0][d.key] = R() * 2 - 1;
      rows[1][d.key] = R() * 2 - 1;
    }
    const dm = trial % 2 ? 0 : (R() * 10 - 5);
    const f = fold(rows, dims, {dm, isMag});
    for (let k = 0; k < 10; k++) {
      const rec = {};
      for (const b of bases) rec[b] = 10 + R() * 15;
      for (let r = 0; r < 2; r++) {
        const want = directEval(rows, dims, rec, r, {dm, isMag});
        const got = foldedEval(f, rec, r);
        assert.ok(Math.abs(want - got) <= 1e-9 * Math.max(1, Math.abs(want)), `trial ${trial} row ${r}: ${got} vs ${want}`);
      }
    }
  }
});

test('frame given as a vendor column-major Float64Array matches row objects', () => {
  const dims = [{key: 'a', center: 1, scale: 2}, {key: 'b', expr: 'x-y', center: 0, scale: 1}, {key: 'c'}];
  const F = Float64Array.from([0.6, 0.8, 0, 0, 0, 1]);
  const rows = frameRowsOf(F, dims);
  assert.deepEqual(rows, [{a: 0.6, b: 0.8}, {c: 1}]);
  const f1 = fold(F, dims);
  const f2 = fold(rows, dims);
  assert.deepEqual(f1.support, f2.support);
  assert.deepEqual(Array.from(f1.terms[0].w), Array.from(f2.terms[0].w));
  assert.deepEqual(f1.c, f2.c);
});

test('support drops cancelling bases and both terms share the key list', () => {
  const dims = [{key: 'c1', expr: 'a-b'}, {key: 'b'}];
  const f = fold([{c1: 1, b: 1}, {c1: 0.5, b: 0.5}], dims);
  assert.deepEqual(f.support, ['a']);
  assert.equal(f.terms[0].keys, f.terms[1].keys);
  assert.deepEqual(Array.from(f.terms[0].w), [1]);
  assert.deepEqual(Array.from(f.terms[1].w), [0.5]);
  // tiny weights below SUPPORT_EPS are not in the support
  const g = fold([{a: SUPPORT_EPS / 10, b: 1}, {b: 0.1}], [{key: 'a'}, {key: 'b'}]);
  assert.deepEqual(g.support, ['b']);
});

test('dm folds into K over magnitude bases only', () => {
  const dims = [{key: 'c', expr: 'f1-f2+3*k'}];
  const f0 = fold([{c: 1}, {}], dims);
  const f1 = fold([{c: 1}, {}], dims, {dm: 2, isMag: ['f1', 'f2']});
  // f1 - f2 is distance independent; 3*k is not a magnitude
  assert.equal(f1.c[0], f0.c[0]);
  const f2 = fold([{c: 1}, {}], dims, {dm: 2, isMag: (k) => k !== 'k'});
  assert.equal(f2.c[0], f0.c[0]);
  const f3 = fold([{f1: 1}, {}], [], {dm: 2.5, isMag: {f1: true}});
  assert.equal(f3.c[0], 2.5);
});

test('keys without a dims entry are base columns; empty frames fold to nothing', () => {
  const f = fold([{raw: 2}, {}], []);
  assert.deepEqual(f.support, ['raw']);
  assert.equal(f.terms[0].w[0], 2);
  assert.deepEqual(f.c, [0, 0]);
  const e = fold([{}, {}], []);
  assert.deepEqual(e.support, []);
  assert.equal(e.terms[0].w.length, 0);
  const n = fold(null, null);
  assert.deepEqual(n.support, []);
  const bad = fold([{a: NaN, b: Infinity, c: 1}, {}], []);
  assert.deepEqual(bad.support, ['c']);
});

test('manifest virtual dims (expression text as key, no expr field) fold into their bases', () => {
  const dims = [{key: 'f182m-f410m', center: 0.5, scale: 0.3}, {key: 'f410m', center: 14, scale: 2}];
  const f = fold([{'f182m-f410m': 1}, {'f410m': 1}], dims);
  assert.deepEqual(f.support, ['f182m', 'f410m']);
  assert.ok(Math.abs(f.terms[0].w[0] - 1 / 0.3) < 1e-12);
  assert.ok(Math.abs(f.terms[0].w[1] + 1 / 0.3) < 1e-12);
  assert.ok(Math.abs(f.terms[1].w[1] - 0.5) < 1e-12);
  assert.ok(Math.abs(f.c[0] + 0.5 / 0.3) < 1e-12 && Math.abs(f.c[1] + 7) < 1e-12);
  const rec = {f182m: 15, f410m: 13};
  assert.ok(Math.abs(foldedEval(f, rec, 0) - directEval([{'f182m-f410m': 1}, {}], dims, rec, 0)) < 1e-12);
  // a frame key with no dims entry is compiled the same way
  assert.deepEqual(fold([{'a - 2*b': 1}, {}], []).support, ['a', 'b']);
  assert.throws(() => fold([{'a*b': 1}, {}], []), SyntaxError);
});

test('scatter helpers: one-hot rows over expression dims reproduce the axis expressions', () => {
  const dims = scatterDims('f182m-f410m', 'f410m');
  const f = fold(oneHotRows('f182m-f410m', 'f410m'), dims);
  const rec = {f182m: 15, f410m: 12.5};
  assert.equal(foldedEval(f, rec, 0), 2.5);
  assert.equal(foldedEval(f, rec, 1), 12.5);
  assert.equal(scatterDims('a', 'a').length, 1);
});

test('foldCamera and foldedFrame describe the constant-free GPU projection', () => {
  const f = fold([{c: 1}, {d: 1}], [{key: 'c', expr: 'a+1', center: 0}, {key: 'd', expr: 'b-2'}]);
  assert.deepEqual(f.c, [1, -2]);
  const cam = foldCamera({cx: 5, cy: 5, scale: 10, aspect: 1, width: 100, height: 100}, f.c);
  assert.equal(cam.cx, 4);
  assert.equal(cam.cy, 7);
  assert.equal(cam.scale, 10);
  const F = foldedFrame(f);
  assert.deepEqual(Array.from(F), [1, 0, 0, 1]);
  // screen position of a record is the same through both routes
  const rec = {a: 3, b: 4};
  const X = foldedEval(f, rec, 0), Y = foldedEval(f, rec, 1);
  const Xp = F[0] * rec.a + F[1] * rec.b, Yp = F[2] * rec.a + F[3] * rec.b;
  const full = {cx: 5, cy: 5, scale: 10};
  assert.equal((X - full.cx) * full.scale, (Xp - cam.cx) * cam.scale);
  assert.equal((Y - full.cy) * full.scale, (Yp - cam.cy) * cam.scale);
});

test('projectRows: N = 0 and N = 1', () => {
  const f = fold([{a: 1}, {b: 1}], []);
  const z = projectRows(f, {a: new Float32Array(0), b: new Float32Array(0)}, 0);
  assert.equal(z.x.length, 0);
  assert.equal(z.vis.length, 0);
  const one = projectRows(f, {a: Float32Array.of(2), b: Float32Array.of(-3)}, 1);
  assert.deepEqual([one.x[0], one.y[0], one.vis[0]], [2, -3, 255]);
});

test('projectRows: missing values hide rows, or fade them with GC semantics', () => {
  const W = [{a: 1, b: FADE_WIDTH / 4}, {c: 1}];
  const f = fold(W, []);
  const cols = {a: Float32Array.of(1, 2, NaN), b: Float32Array.of(10, NaN, 1), c: Float32Array.of(0, 0, 0)};
  const hard = projectRows(f, cols, 3);
  assert.equal(hard.vis[0], 255);
  assert.equal(hard.vis[1], 0);
  assert.ok(Number.isNaN(hard.x[1]));
  assert.equal(hard.vis[2], 0);
  const soft = projectRows(f, cols, 3, {fade: true});
  const fade = foldFade(f);
  const ib = f.support.indexOf('b');
  assert.ok(Math.abs(fade[ib] - 0.75) < 1e-6);
  assert.equal(soft.vis[1], Math.round(255 * fade[ib]));
  assert.equal(soft.x[1], 2);           // missing b contributes 0
  assert.equal(soft.vis[2], 0);         // a has |W| = 1 > FADE_WIDTH: fade factor 0 hides it
  // a missing column object behaves like all-NaN
  const none = projectRows(f, {a: cols.a, c: cols.c}, 3);
  assert.ok(Array.from(none.vis).every((v) => v === 0));
});

test('projectRows honours input visibility (0/1 masks and 0..255 bytes)', () => {
  const f = fold([{a: 1}, {a: 1}], []);
  const cols = {a: Float32Array.of(1, 2, 3)};
  const m = projectRows(f, cols, 3, {vis: Uint8Array.of(1, 0, 1)});
  assert.deepEqual(Array.from(m.vis), [255, 0, 255]);
  const b = projectRows(f, cols, 3, {vis: Uint8Array.of(255, 128, 1)});
  assert.equal(b.vis[0], 255);
  assert.equal(b.vis[1], 128);
  assert.equal(b.vis[2], 0, 'visibility below 0.01 hides the row');
});

test('large magnitudes and extreme weights keep relative precision', () => {
  const dims = [{key: 'big', expr: '1e6*a - 1e6*b', center: 1e6, scale: 1e3}];
  const rows = [{big: 1}, {}];
  const f = fold(rows, dims);
  const rec = {a: 1e20, b: 1e20 - 1e14};
  const want = directEval(rows, dims, rec, 0);
  const got = foldedEval(f, rec, 0);
  assert.ok(Math.abs(want - got) <= 1e-6 * Math.abs(want), `${got} vs ${want}`);
  assert.ok(Number.isNaN(directEval(rows, dims, {a: 1}, 0)), 'missing base gives NaN');
});
