// Shared expression vectors (portalkit/data/expr_vectors.json) against web/js/data/expr.js.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {compile, evaluate, isValid, isKey, baseKeys, ExprError} from '../../web/js/data/expr.js';

const REPO = fileURLToPath(new URL('../../', import.meta.url));
const V = JSON.parse(readFileSync(REPO + 'portalkit/data/expr_vectors.json', 'utf8'));
const RTOL = V.rtol ?? 1e-6;
const ATOL = V.atol ?? 1e-9;

function close(a, b) {
  return Math.abs(a - b) <= ATOL + RTOL * Math.abs(b);
}

function cell(v) {
  if (v === null) return NaN;
  if (v === 'inf') return Infinity;
  if (v === '-inf') return -Infinity;
  return v;
}

test('vector file schema', () => {
  assert.equal(V.schema, 'pk-expr-vectors/1');
  assert.ok(V.compile.length > 20);
  assert.ok(V.evaluate.length > 3);
});

test('compile vectors', () => {
  for (const vec of V.compile) {
    if (vec.error) {
      assert.throws(() => compile(vec.expr), ExprError, `expected error for ${JSON.stringify(vec.expr)}`);
      assert.equal(isValid(vec.expr), false);
      continue;
    }
    const r = compile(vec.expr);
    assert.deepEqual(r.keys, vec.keys, `keys of ${JSON.stringify(vec.expr)}`);
    assert.ok(close(r.c, vec.c), `c of ${vec.expr}: ${r.c} vs ${vec.c}`);
    assert.deepEqual(Object.keys(r.w), Object.keys(vec.w), `w keys of ${vec.expr}`);
    for (const k of Object.keys(vec.w)) assert.ok(close(r.w[k], vec.w[k]), `w[${k}] of ${vec.expr}`);
    assert.equal(isValid(vec.expr), true);
  }
});

test('evaluate vectors', () => {
  for (const vec of V.evaluate) {
    const cols = {};
    for (const [k, arr] of Object.entries(vec.cols)) cols[k] = Float32Array.from(arr.map(cell));
    const out = evaluate(compile(vec.expr), cols, vec.n);
    assert.ok(out instanceof Float32Array);
    assert.equal(out.length, vec.n);
    vec.out.forEach((e, i) => {
      if (e === null) assert.ok(Number.isNaN(out[i]), `${vec.expr}[${i}] should be NaN`);
      else assert.ok(close(out[i], e), `${vec.expr}[${i}]: ${out[i]} vs ${e}`);
    });
  }
});

test('prototype-named keys and plain arrays', () => {
  const r = compile('constructor - toString + 2*__proto__');
  assert.deepEqual(r.keys, ['constructor', 'toString', '__proto__']);
  assert.equal(r.w.constructor, 1);
  assert.equal(r.w.toString, -1);
  assert.equal(r.w.__proto__, 2);
  const out = evaluate(r, {constructor: [1, null], toString: [0.5, 1], __proto__: [1, 1]}, 2);
  assert.ok(close(out[0], 2.5));
  assert.ok(Number.isNaN(out[1]));
});

test('helpers and edge cases', () => {
  assert.equal(isKey('f182m'), true);
  assert.equal(isKey('a.b_c9'), true);
  assert.equal(isKey('9a'), false);
  assert.equal(isKey(''), false);
  assert.equal(isValid(42), false);
  assert.throws(() => compile(null), TypeError);
  assert.deepEqual(baseKeys('a - b + a'), ['a', 'b']);
  // string form and inferred n
  const out = evaluate('a + 1', {a: Float32Array.from([1, 2])});
  assert.deepEqual(Array.from(out), [2, 3]);
  assert.throws(() => evaluate('a', {}, 2), /not supplied/);
  assert.throws(() => evaluate('7', {}), /n is required/);
  assert.throws(() => evaluate('a', {a: [1]}, 2), /length/);
  // unicode key characters are rejected, ASCII whitespace is accepted
  assert.throws(() => compile('é'), ExprError);
  assert.deepEqual(compile('\ta\r\n-\nb ').keys, ['a', 'b']);
});
