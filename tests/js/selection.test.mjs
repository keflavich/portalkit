// node --test tests/js/selection.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import {inflateRawSync, deflateRawSync, constants as zc} from 'node:zlib';
import {
  Selection, OPS, opFromEvent, undoDepth, packBits, unpackBits,
  deflateRaw, inflateRaw, b64urlEncode, b64urlDecode, utf8Encode, utf8Decode,
  encodeHashValue, hashLength,
} from '../../web/js/core/selection.js';
import {UNDO_MAX, UNDO_BYTES, IDS_IN_HASH_MAX, BITS_IN_HASH_MAX_N} from '../../web/js/core/constants.js';

const u8 = (arr) => Uint8Array.from(arr);
const bits = (s) => Uint8Array.from(s.split('').map(Number));
const str = (sel) => Array.from(sel.mask).join('');

/** Deterministic PRNG (mulberry32). */
function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6D2B79F5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// ---------------------------------------------------------------- set ops (frozen semantics)

test('replace / add / subtract follow sel = hit, sel |= hit, sel &= ~hit', () => {
  const s = new Selection(6);
  s.apply(bits('110000'), 'replace');
  assert.equal(str(s), '110000');
  assert.equal(s.count, 2);
  s.apply(bits('011100'), 'add');
  assert.equal(str(s), '111100');
  assert.equal(s.count, 4);
  s.apply(bits('100001'), 'subtract');
  assert.equal(str(s), '011100');
  assert.equal(s.count, 3);
  s.apply(bits('011100'), 'subtract');
  assert.equal(s.count, 0, 'subtracting to empty leaves count 0');
  assert.deepEqual(Array.from(s.indices()), []);
});

test('intersect keeps rows the view cannot draw: sel = (sel & hit) | (sel & ~drawable)', () => {
  const s = new Selection(8);
  s.apply(bits('11110000'), 'replace');
  //            sel        11110000
  //            hit        01000000
  //            drawable   11001111  -> rows 2,3 not drawable keep their state
  s.apply(bits('01000000'), 'intersect', bits('11001111'));
  assert.equal(str(s), '01110000');
  assert.equal(s.count, 3);
  // brute-force check of the formula on random masks
  const r = rng(7);
  for (let trial = 0; trial < 50; trial++) {
    const n = 1 + Math.floor(r() * 40);
    const sel0 = Uint8Array.from({length: n}, () => (r() < 0.5 ? 1 : 0));
    const hit = Uint8Array.from({length: n}, () => (r() < 0.5 ? 1 : 0));
    const drw = Uint8Array.from({length: n}, () => (r() < 0.7 ? 1 : 0));
    for (let i = 0; i < n; i++) hit[i] &= drw[i];        // hit covers drawable rows only
    const t = new Selection(n);
    t.apply(sel0, 'replace');
    t.apply(hit, 'intersect', drw);
    const want = sel0.map((v, i) => (v & hit[i]) | (v & (1 - drw[i])));
    assert.deepEqual(Array.from(t.mask), Array.from(want));
    assert.equal(t.count, want.reduce((a, b) => a + b, 0));
  }
});

test('intersect without drawable treats every row as drawable', () => {
  const s = new Selection(4);
  s.apply(bits('1111'), 'replace');
  s.apply(bits('0101'), 'intersect');
  assert.equal(str(s), '0101');
});

test('short hit / drawable arrays: missing rows count as 0', () => {
  const s = new Selection(5);
  s.apply(bits('11'), 'replace');
  assert.equal(str(s), '11000');
  s.apply(bits('11111'), 'add');
  s.apply(bits('0'), 'intersect', bits('1'));
  assert.equal(str(s), '01111', 'rows beyond drawable.length are not drawable');
  s.apply(null, 'replace');
  assert.equal(s.count, 0);
});

test('unknown op throws TypeError and leaves state unchanged', () => {
  const s = new Selection(3);
  s.apply(bits('100'), 'replace');
  const v = s.version;
  assert.throws(() => s.apply(bits('111'), 'xor'), TypeError);
  assert.throws(() => s.setIndices([1], 'union'), TypeError);
  assert.equal(s.version, v);
  assert.equal(str(s), '100');
  assert.deepEqual(OPS, ['replace', 'add', 'subtract', 'intersect']);
});

test('setIndices ignores duplicates, out-of-range, NaN and non-integers', () => {
  const s = new Selection(5);
  s.setIndices([0, 0, 4, 5, -1, 2.5, NaN, Infinity, 2], 'replace');
  assert.deepEqual(Array.from(s.indices()), [0, 2, 4]);
  s.setIndices(new Int32Array([1]), 'add');
  assert.deepEqual(Array.from(s.indices()), [0, 1, 2, 4]);
  s.setIndices([], 'replace');
  assert.equal(s.count, 0);
  s.setIndices(null, 'add');
  assert.equal(s.count, 0);
});

test('N = 0 and N = 1', () => {
  const z = new Selection(0);
  assert.equal(z.n, 0);
  z.apply(new Uint8Array(0), 'replace');
  z.invert();
  assert.equal(z.count, 0);
  assert.equal(z.serialize(String), '');
  assert.deepEqual(Array.from(z.indices()), []);
  for (const bad of [-5, NaN, undefined, Infinity]) assert.equal(new Selection(bad).n, 0);

  const one = new Selection(1);
  one.setIndices([0], 'replace');
  assert.equal(one.count, 1);
  assert.equal(one.recipeForm((i) => `src-${i}`), 'ids:src-0');
  const withIds = one.serialize((i) => `src-${i}`);
  assert.equal(withIds, [one.recipeForm((i) => `src-${i}`), one.bitsForm()].sort((a, b) => a.length - b.length)[0]);
  assert.match(one.serialize(), /^bits:/, 'index-only recipe without idOf: mask form');
  const p = Selection.parse(one.serialize());
  assert.deepEqual(Array.from(unpackBits(p.bytes, 1)), [1]);
  one.invert();
  assert.equal(one.count, 0);
});

test('version increments on every change; clear on empty is a no-op', () => {
  const s = new Selection(4);
  const kinds = [];
  const off = s.onChange((sel, kind) => kinds.push([kind, sel.version]));
  s.clear();
  assert.equal(s.version, 0);
  s.apply(bits('1000'), 'replace');
  s.apply(bits('1000'), 'add');            // same mask: still a change event
  s.invert();
  s.undo();
  s.redo();
  s.clear();
  assert.deepEqual(kinds, [['replace', 1], ['add', 2], ['invert', 3], ['undo', 4], ['redo', 5], ['clear', 6]]);
  off();
  s.apply(bits('1111'), 'replace');
  assert.equal(kinds.length, 6);
});

test('invert within drawable', () => {
  const s = new Selection(6);
  s.apply(bits('110000'), 'replace');
  s.invert(bits('101010'));
  assert.equal(str(s), '011010');
  s.invert();
  assert.equal(str(s), '100101');
});

// ---------------------------------------------------------------- undo / redo

test('undoDepth = min(20, floor(64 MiB / n)), at least 1', () => {
  assert.equal(UNDO_MAX, 20);
  assert.equal(UNDO_BYTES, 64 * 1024 * 1024);
  assert.equal(undoDepth(0), 20);
  assert.equal(undoDepth(1), 20);
  assert.equal(undoDepth(1_000_000), 20);
  assert.equal(undoDepth(3_355_443), 20);        // floor(67108864/3355443) = 20
  assert.equal(undoDepth(3_355_444), 19);
  assert.equal(undoDepth(3_740_000), 17);        // treasury-scale catalog
  assert.equal(undoDepth(64 * 1024 * 1024), 1);
  assert.equal(undoDepth(1e9), 1, 'never below 1');
  assert.equal(undoDepth(100, 5, 1000), 5);
  assert.equal(undoDepth(400, 5, 1000), 2);
  assert.equal(new Selection(10, {maxUndo: 3}).undoDepth, 3);
  assert.equal(new Selection(1000, {undoBytes: 2500}).undoDepth, 2);
});

test('undo / redo restore mask, count and recipe; history is bounded by depth', () => {
  const s = new Selection(4, {maxUndo: 3});
  assert.equal(s.canUndo, false);
  assert.equal(s.undo(), false);
  assert.equal(s.redo(), false);
  const states = [];
  for (const m of ['1000', '1100', '1110', '1111', '0001']) {
    s.apply(bits(m), 'replace', undefined, {op: 'replace', kind: 'ids', ids: [m]});
    states.push(m);
  }
  assert.equal(str(s), '0001');
  assert.ok(s.undo());
  assert.equal(str(s), '1111');
  assert.equal(s.recipe()[0].ids[0], '1111');
  assert.ok(s.undo());
  assert.ok(s.undo());
  assert.equal(str(s), '1100');
  assert.equal(s.count, 2);
  assert.equal(s.undo(), false, 'depth 3 reached');
  assert.ok(s.canRedo);
  assert.ok(s.redo());
  assert.equal(str(s), '1110');
  s.apply(bits('0110'), 'add');
  assert.equal(s.canRedo, false, 'a new action clears redo');
  assert.ok(s.undo());
  assert.equal(str(s), '1110');
});

test('undo of clear and invert', () => {
  const s = new Selection(3);
  s.apply(bits('101'), 'replace', undefined, {op: 'replace', kind: 'ids', ids: ['a', 'c']});
  assert.equal(s.recipeComplete, true);
  s.clear();
  assert.equal(s.count, 0);
  s.undo();
  assert.equal(str(s), '101');
  s.invert();
  assert.equal(s.recipeComplete, false);
  s.undo();
  assert.equal(str(s), '101');
  assert.equal(s.recipeComplete, true, 'recipe validity is restored by undo');
});

// ---------------------------------------------------------------- recipes

test('recipe holds the ops since the last replace / clear', () => {
  const s = new Selection(10);
  const A = {op: 'replace', kind: 'rect', view: {ds: 'src', x: 'a', y: 'b'}, shape: {x0: 0, y0: 0, x1: 1, y1: 1}};
  const B = {op: 'add', kind: 'circle', view: {ds: 'src', x: 'a', y: 'b'}, shape: {cx: 1, cy: 1, r: 2}};
  const C = {op: 'subtract', kind: 'lasso', view: {ds: 'src', x: 'a', y: 'b'}, shape: {pts: [[0, 0], [1, 0], [1, 1]]}};
  s.apply(bits('1100000000'), 'replace', undefined, A);
  s.apply(bits('0011000000'), 'add', undefined, B);
  s.apply(bits('0100000000'), 'subtract', undefined, C);
  assert.deepEqual(s.recipe(), [A, B, C]);
  assert.ok(s.recipeComplete);
  s.recipe()[0].op = 'mutated';
  assert.equal(s.recipe()[0].op, 'replace', 'recipe() returns copies');
  s.apply(bits('0000000001'), 'replace', undefined, B);
  assert.deepEqual(s.recipe(), [B]);
  s.clear();
  assert.deepEqual(s.recipe(), []);
  s.apply(bits('1'), 'replace');           // replace without a RecipeOp
  assert.equal(s.recipeComplete, false);
  s.apply(bits('1'), 'add', undefined, B);
  assert.equal(s.recipeComplete, false, 'stays incomplete until the next replace/clear');
});

test('setIndices without recipeOp records an ids op with hidden indices', () => {
  const s = new Selection(5);
  s.setIndices([3, 1], 'replace');
  const [op] = s.recipe();
  assert.equal(op.kind, 'ids');
  assert.deepEqual(Array.from(op._idx), [3, 1]);
  assert.equal(JSON.stringify(op).includes('_idx'), false);
});

// ---------------------------------------------------------------- serialization

test('serialize: ids form for a single replace-ids recipe (unicode, commas, percent)', () => {
  const ids = ['α Cen', 'a,b', '100%', 'x:y', 'G0.1+0.2', '日本', '😀'];
  // above BITS_IN_HASH_MAX_N there is no mask form, so the recipe form is used
  const s = new Selection(BITS_IN_HASH_MAX_N + 1);
  s.setIndices([0, 1, 2, 3, 4, 5, 6], 'replace');
  const out = s.serialize((i) => ids[i]);
  assert.match(out, /^ids:/);
  assert.equal(out.split(',').length, ids.length, 'commas inside ids are escaped');
  const p = Selection.parse(out);
  assert.equal(p.kind, 'ids');
  assert.deepEqual(p.ids, ids);
});

test('recipeForm: ids up to IDS_IN_HASH_MAX in one replace op, r: otherwise', () => {
  const n = BITS_IN_HASH_MAX_N + 1000;
  const s = new Selection(n);
  const idx = Array.from({length: IDS_IN_HASH_MAX}, (_, k) => k * 3);
  s.setIndices(idx, 'replace');
  const out = s.serialize((i) => `s${i}`);
  assert.match(out, /^ids:/);
  assert.equal(Selection.parse(out).ids.length, IDS_IN_HASH_MAX);
  s.setIndices([n - 1], 'add');
  const r = s.serialize((i) => `s${i}`);
  assert.match(r, /^r:/, 'two ops: recipe JSON');
  assert.deepEqual(Selection.parse(r).ops.map((o) => [o.op, o.ids.length]), [['replace', IDS_IN_HASH_MAX], ['add', 1]]);
  s.setIndices(Array.from({length: IDS_IN_HASH_MAX + 1}, (_, k) => k), 'replace');
  assert.match(s.serialize((i) => `s${i}`), /^r:/, 'more than IDS_IN_HASH_MAX ids: r:');
  // at n <= BITS_IN_HASH_MAX_N the shorter of the two forms wins
  const m = new Selection(1000);
  m.setIndices(idx, 'replace');
  const rec = m.recipeForm((i) => `s${i}`);
  const bf = m.bitsForm();
  assert.match(rec, /^ids:/);
  assert.match(bf, /^bits:/);
  assert.equal(m.serialize((i) => `s${i}`), rec.length <= bf.length ? rec : bf);
  assert.deepEqual(unpackBits(Selection.parse(bf).bytes, 1000), m.mask);
});

test('serialize picks the shorter of the recipe and mask forms; a tie goes to the recipe', () => {
  const r = rng(5);
  let sawBits = 0;
  let sawRec = 0;
  for (const n of [8, 64, 1000, 20_000]) {
    for (const k of [1, 3, 40]) {
      const s = new Selection(n);
      const idx = Array.from({length: Math.min(k, n)}, () => Math.floor(r() * n));
      s.setIndices(idx, 'replace', {op: 'replace', kind: 'ids', view: {ds: 'src'}, ids: idx.map((i) => `source-${i}`)});
      const rec = s.recipeForm();
      const bf = s.bitsForm();
      const out = s.serialize();
      assert.equal(out, rec.length <= bf.length ? rec : bf, `n=${n} k=${k}`);
      if (out === bf) sawBits++; else sawRec++;
    }
  }
  assert.ok(sawBits > 0 && sawRec > 0, 'both forms occur');
  // a forced tie
  const t = new Selection(16);
  t.apply(bits('1000000000000000'), 'replace', undefined, {op: 'replace', kind: 'ids', ids: ['x']});
  const len = t.bitsForm().length;
  t.apply(bits('1000000000000000'), 'replace', undefined, {op: 'replace', kind: 'ids', ids: ['x'.repeat(len - 4)]});
  assert.equal(t.recipeForm().length, len);
  assert.match(t.serialize(), /^ids:/);
});

test('recipeIndices lists the rows index-only ops need, including subtracted ones', () => {
  const s = new Selection(10);
  assert.deepEqual(Array.from(s.recipeIndices()), []);
  s.setIndices([4, 2, 4], 'replace');
  s.setIndices([2, 7], 'subtract');
  s.apply(bits('0000000001'), 'add', undefined, {op: 'add', kind: 'ids', ids: ['S9']});
  assert.deepEqual(Array.from(s.recipeIndices()), [4, 2, 7]);
  assert.deepEqual(Array.from(s.indices()), [4, 9]);
  // idOf that knows only the selected rows cannot spell the subtract op
  const partial = (i) => (s.mask[i] ? `S${i}` : undefined);
  assert.equal(s.recipeForm(partial), '');
  // ... so the ids of the selected rows (the equivalent replace-ids recipe) compete with bits:
  const sp = s.serialize(partial);
  assert.equal(sp, [`ids:S4,S9`, s.bitsForm()].sort((a, b) => a.length - b.length)[0]);
  assert.match(s.serialize(), /^bits:/, 'no idOf: mask form');
  const full = Selection.parse(s.recipeForm((i) => `S${i}`));
  assert.deepEqual(full.ops.map((o) => [o.op, o.ids]), [['replace', ['S4', 'S2']], ['subtract', ['S2', 'S7']], ['add', ['S9']]]);
  s.invert();
  assert.deepEqual(Array.from(s.recipeIndices()), [], 'incomplete recipe: nothing to look up');
});

test('restored selections write the same form again (reload stability)', () => {
  const idOf = (i) => `S${i}`;
  const rowOf = (id) => Number(id.slice(1));
  const restore = (n, str) => {
    const sel = new Selection(n);
    const p = Selection.parse(str);
    if (p.kind === 'ids') sel.setIndices(p.ids.map(rowOf), 'replace', {op: 'replace', kind: 'ids', view: {ds: 'src'}, ids: p.ids});
    else if (p.kind === 'bits') sel.apply(unpackBits(p.bytes, n), 'replace');
    else {
      const tmp = new Selection(n);
      for (const op of p.ops) {
        const hit = new Uint8Array(n);
        if (op.kind === 'rect') hit.fill(1, op.shape.x0, op.shape.x1);
        else for (const id of op.ids) hit[rowOf(id)] = 1;
        tmp.apply(hit, op.op, undefined, op);
      }
      sel.replaceWith(tmp.mask, tmp.recipe(), tmp.recipeComplete);
    }
    return sel;
  };
  const rect = (x0, x1) => ({op: 'replace', kind: 'rect', view: {ds: 'src', x: 'a', y: 'b'}, shape: {x0, x1}});
  for (const n of [50, 5000, BITS_IN_HASH_MAX_N + 7]) {
    const cases = [
      (s) => s.setIndices([0, n - 1], 'replace'),
      (s) => s.setIndices([1, 2], 'replace', {op: 'replace', kind: 'ids', view: {ds: 'src'}, ids: ['S1', 'S2']}),
      (s) => { const h = new Uint8Array(n); h.fill(1, 3, Math.min(n, 40)); s.apply(h, 'replace', undefined, rect(3, Math.min(n, 40))); },
      (s) => { s.setIndices([5, 6, 7], 'replace'); s.setIndices([6], 'subtract'); },
    ];
    for (const make of cases) {
      const s = new Selection(n);
      make(s);
      const a = s.serialize(idOf);
      assert.ok(a, `n=${n}`);
      const b = restore(n, a).serialize(idOf);
      assert.equal(b, a, `n=${n}: ${a.slice(0, 16)}`);
      assert.equal(restore(n, b).serialize(), b, 'no idOf needed once restored');
    }
  }
});

test('brush recipes compete with bits: only when there is no mask form (replay depends on filters)', () => {
  const rect = (op, x0, x1) => ({op, kind: 'rect', view: {ds: 'src', x: 'a', y: 'b'}, shape: {x0, y0: 0, x1, y1: 1}});
  const r = rng(11);
  for (const n of [5000, 50_000, BITS_IN_HASH_MAX_N]) {
    const s = new Selection(n);
    const h = new Uint8Array(n);
    for (let i = 0; i < n; i++) if (r() < 0.1) h[i] = 1;   // scattered hits: bits: is long
    s.apply(h, 'replace', undefined, rect('replace', 0, 1));
    assert.ok(s.recipeForm().length < s.bitsForm().length, 'the r: form would be shorter');
    assert.equal(s.recipeExact, false);
    assert.match(s.serialize(), /^bits:/, `n=${n}: brush selection keeps the exact mask form`);
    // an ids op after the brush does not make the recipe view-independent
    s.setIndices([0], 'add', {op: 'add', kind: 'ids', view: {ds: 'src'}, ids: ['S0']});
    assert.match(s.serialize(), /^bits:/);
  }
  const big = new Selection(BITS_IN_HASH_MAX_N + 1);
  big.apply(bits('0111'), 'replace', undefined, rect('replace', 1, 4));
  assert.match(big.serialize(), /^r:/, 'n > BITS_IN_HASH_MAX_N: the recipe is the only form');
  // filter ops that carry their filters and preset ops are exact
  const f = new Selection(100);
  f.apply(bits('1'.repeat(50)), 'replace', undefined, {op: 'replace', kind: 'filter', view: {ds: 'src'}, filters: [{key: 'a', min: 0}]});
  assert.equal(f.recipeExact, true);
  f.apply(bits('1'), 'add', undefined, {op: 'add', kind: 'preset', view: {ds: 'src'}, key: 'p'});
  assert.equal(f.recipeExact, true);
  f.apply(bits('1'), 'add', undefined, {op: 'add', kind: 'filter', view: {ds: 'src'}});
  assert.equal(f.recipeExact, false, 'filter op without its filters');
});

test('serialize: idOf over the selected rows only still writes ids: above BITS_IN_HASH_MAX_N (chips)', () => {
  const s = new Selection(BITS_IN_HASH_MAX_N + 1);
  s.setIndices([1, 2, 3], 'replace');
  s.setIndices([2], 'subtract');
  const sel = Array.from(s.indices());
  const map = new Map(sel.map((i) => [i, `S${i}`]));
  assert.equal(s.recipeForm((i) => map.get(i)), '', 'the subtract op names an unselected row');
  assert.equal(s.serialize((i) => map.get(i)), 'ids:S1,S3');
  assert.equal(s.serialize(), '', 'no idOf and no mask form: not serializable');
  // with every recipe row known, the recipe form is used
  assert.match(s.serialize((i) => `S${i}`), /^r:/);
  // incomplete recipe (invert) above the bits limit: selected-row ids when few enough
  const t = new Selection(BITS_IN_HASH_MAX_N + 1);
  t.setIndices([5], 'replace');
  t.invert(bits('0000011'));
  assert.equal(t.recipeComplete, false);
  assert.equal(t.serialize((i) => `S${i}`), 'ids:S6');
});

test('serialize compares the forms by their length in the URL (hashLength)', () => {
  assert.equal(hashLength('ids:a%20b,c'), encodeHashValue('ids:a%20b,c').length);
  assert.equal(hashLength('ids:a%20b'), 'ids:a%2520b'.length);
  assert.equal(hashLength('bits:AB-_'), 9);
  const idOf = (i) => `J ${i} \u00dc`;
  let sawRawWin = 0;
  for (const [n, k] of [[20_000, 2], [100_000, 6]]) {
    const s = new Selection(n);
    const idx = Array.from({length: k}, (_, j) => Math.floor((j + 0.5) * n / k));
    s.setIndices(idx, 'replace', {op: 'replace', kind: 'ids', view: {ds: 'src'}, ids: idx.map(idOf)});
    const rec = s.recipeForm();
    const bf = s.bitsForm();
    if (rec.length <= bf.length && hashLength(rec) > hashLength(bf)) sawRawWin++;
    const out = s.serialize();
    assert.equal(out, hashLength(rec) <= hashLength(bf) ? rec : bf, `n=${n} k=${k}`);
  }
  assert.ok(sawRawWin > 0, 'a case where the raw lengths would pick the longer URL');
});

test('serialize: bits round-trip at n = BITS_IN_HASH_MAX_N and is compact for clustered masks', () => {
  const n = BITS_IN_HASH_MAX_N;
  const s = new Selection(n);
  const hit = new Uint8Array(n);
  for (let i = 50_000; i < 60_000; i++) hit[i] = 1;
  hit[n - 1] = 1;
  s.apply(hit, 'replace');
  const out = s.serialize();
  assert.match(out, /^bits:[A-Za-z0-9_-]+$/);
  assert.ok(out.length < 400, `compressed length ${out.length}`);
  assert.deepEqual(unpackBits(Selection.parse(out).bytes, n), hit);
});

test('serialize: r: recipe above BITS_IN_HASH_MAX_N; "" when the recipe is incomplete', () => {
  const n = BITS_IN_HASH_MAX_N + 1;
  const s = new Selection(n);
  const hit = new Uint8Array(n);
  hit.fill(1, 0, 400);
  const op = {op: 'replace', kind: 'rect', view: {ds: 'src', x: 'f115w-f200w', y: 'f200w'}, shape: {x0: 0.5, y0: 18, x1: 2.5, y1: 14}};
  s.apply(hit, 'replace', undefined, op);
  const out = s.serialize((i) => `s${i}`);
  assert.match(out, /^r:/);
  assert.deepEqual(Selection.parse(out), {kind: 'r', ops: [op]});
  s.invert();
  assert.equal(s.serialize(), '');
  s.undo();
  // ids ops recorded from indices need idOf to become ids
  s.setIndices([5, 7], 'add');
  assert.equal(s.serialize(), '', 'index-only ops cannot be serialized without idOf');
  const r = Selection.parse(s.serialize((i) => `id-${i}`));
  assert.equal(r.kind, 'r');
  assert.deepEqual(r.ops[1].ids, ['id-5', 'id-7']);
  assert.equal(r.ops[1].op, 'add');
});

test('parse: empty, malformed and lenient inputs', () => {
  for (const bad of ['', '   ', null, undefined, 42, 'nocolon', 'xyz:abc', 'bits:!!!!', 'r:%%%', 'r:{"a":1}', 'bits:AAAA']) {
    assert.equal(Selection.parse(bad), null, JSON.stringify(bad));
  }
  assert.deepEqual(Selection.parse('ids:'), {kind: 'ids', ids: []});
  assert.deepEqual(Selection.parse('ids:a%2Cb,c%E0%A4'), {kind: 'ids', ids: ['a,b', 'c%E0%A4']}, 'bad escapes kept raw');
  assert.deepEqual(Selection.parse('r:[{"op":"add","kind":"ids","ids":["x"]}]'),
    {kind: 'r', ops: [{op: 'add', kind: 'ids', ids: ['x']}]});
  assert.deepEqual(Selection.parse('r:%5B%5D'), {kind: 'r', ops: []});
});

test('opFromEvent modifier mapping', () => {
  assert.equal(opFromEvent({}), 'replace');
  assert.equal(opFromEvent(null), 'replace');
  assert.equal(opFromEvent({shiftKey: true}), 'add');
  assert.equal(opFromEvent({altKey: true}), 'subtract');
  assert.equal(opFromEvent({shiftKey: true, altKey: true}), 'intersect');
});

// ---------------------------------------------------------------- codecs

test('packBits / unpackBits are LSB-first and tolerate short input', () => {
  const m = bits('10000000' + '01' + '1');
  const p = packBits(m);
  assert.deepEqual(Array.from(p), [1, 2 | 4]);
  assert.deepEqual(unpackBits(p, 11), m);
  assert.deepEqual(Array.from(unpackBits(u8([255]), 10)), [1, 1, 1, 1, 1, 1, 1, 1, 0, 0]);
  assert.deepEqual(Array.from(unpackBits(u8([]), 3)), [0, 0, 0]);
  assert.equal(packBits(new Uint8Array(0)).length, 0);
});

test('b64url round-trip, accepts standard base64, rejects junk', () => {
  const r = rng(3);
  for (const len of [0, 1, 2, 3, 4, 5, 63, 64, 65, 1000]) {
    const b = Uint8Array.from({length: len}, () => Math.floor(r() * 256));
    const e = b64urlEncode(b);
    assert.match(e, /^[A-Za-z0-9_-]*$/);
    assert.equal(e, Buffer.from(b).toString('base64url'));
    assert.deepEqual(b64urlDecode(e), b);
    assert.deepEqual(b64urlDecode(Buffer.from(b).toString('base64')), b, 'standard alphabet with padding');
  }
  assert.throws(() => b64urlDecode('ab$c'), SyntaxError);
});

test('utf8 helpers round-trip astral characters', () => {
  const s = 'Ωmega 😀 日本 \u0000 end';
  assert.equal(utf8Decode(utf8Encode(s)), s);
});

test('deflateRaw output is valid raw DEFLATE (checked with node:zlib)', () => {
  const r = rng(11);
  const inputs = [
    new Uint8Array(0),
    u8([0]),
    u8([1, 2, 3]),
    new Uint8Array(70_000),                                   // long runs, > 32 KiB window
    Uint8Array.from({length: 50_000}, () => Math.floor(r() * 256)),   // incompressible
    Uint8Array.from({length: 30_000}, (_, i) => (i % 17 === 0 ? 255 : i % 3)),
    utf8Encode(JSON.stringify(Array.from({length: 300}, (_, i) => ({op: 'add', kind: 'ids', ids: [`source-${i}`]})))),
  ];
  for (const inp of inputs) {
    const d = deflateRaw(inp);
    assert.deepEqual(new Uint8Array(inflateRawSync(d)), inp, `len ${inp.length}`);
  }
  assert.ok(deflateRaw(new Uint8Array(70_000)).length < 1000);
});

test('inflateRaw decodes zlib output: stored, fixed and dynamic Huffman blocks', () => {
  const r = rng(19);
  const text = utf8Encode('The quick brown fox jumps over the lazy dog. '.repeat(400) + '日本語テキスト'.repeat(50));
  const noise = Uint8Array.from({length: 40_000}, () => Math.floor(r() * 256));
  const skewed = Uint8Array.from({length: 40_000}, () => (r() < 0.9 ? 0 : Math.floor(r() * 8)));
  const cases = [
    [text, {}],                                                       // dynamic
    [text, {strategy: zc.Z_FIXED}],                                   // fixed
    [noise, {level: 0}],                                              // stored
    [noise, {}],                                                      // mostly stored
    [skewed, {strategy: zc.Z_HUFFMAN_ONLY}],                          // dynamic, literals only
    [skewed, {strategy: zc.Z_RLE}],
    [new Uint8Array(0), {}],
    [u8([42]), {level: 9}],
  ];
  for (const [inp, opts] of cases) {
    const z = new Uint8Array(deflateRawSync(inp, opts));
    assert.deepEqual(inflateRaw(z), inp, JSON.stringify(opts));
  }
});

test('inflateRaw rejects corrupt and oversized streams', () => {
  const z = new Uint8Array(deflateRawSync(new Uint8Array(10_000)));
  assert.throws(() => inflateRaw(z, 100), Error, 'maxOut enforced');
  assert.throws(() => inflateRaw(u8([0xff, 0xff, 0xff])), Error);
  assert.throws(() => inflateRaw(z.slice(0, 2)), Error, 'truncated');
});

test('random selections round-trip through serialize/parse at several sizes', () => {
  const r = rng(29);
  for (const n of [1, 7, 8, 9, 301, 4096, 100_003]) {
    const s = new Selection(n);
    const density = r();
    const hit = Uint8Array.from({length: n}, () => (r() < density ? 1 : 0));
    s.apply(hit, 'replace');
    const out = s.serialize();
    if (s.count === 0) { assert.equal(out, ''); continue; }
    const p = Selection.parse(out);
    assert.equal(p.kind, 'bits');
    assert.deepEqual(unpackBits(p.bytes, n), hit, `n=${n}`);
  }
});

test('replaceWith: one undoable replace that carries the recipe', () => {
  const s = new Selection(10);
  s.setIndices([1, 2], 'replace');
  const v = s.version;
  const rec = [{op: 'add', kind: 'ids', view: {ds: 'src'}, ids: ['S5']}];
  s.replaceWith(bits('0000010000'), rec, true);
  assert.equal(s.version, v + 1);
  assert.deepEqual(Array.from(s.indices()), [5]);
  assert.equal(s.recipeComplete, true);
  assert.deepEqual(s.recipe(), rec);
  assert.ok(s.undo());
  assert.deepEqual(Array.from(s.indices()), [1, 2]);
  s.replaceWith(new Uint8Array(10), [], true);
  assert.equal(s.count, 0);
  assert.equal(s.recipeComplete, true);
  s.replaceWith(bits('1000000000'), [], true);
  assert.equal(s.recipeComplete, false, 'non-empty mask without ops is not reproducible');
});
