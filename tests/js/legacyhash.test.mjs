// node --test tests/js/legacyhash.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {
  CANONICAL_KEYS, GRAMMAR_NAMES, GRAMMARS, isCanonicalHash, layoutInfo, legacyContext, mapLegacy,
  encodeFrame, encodeJSON, encodeRecipe,
} from '../../web/js/core/legacyhash.js';
import {b64urlDecode, inflateRaw, utf8Decode} from '../../web/js/core/selection.js';

const VECTORS = JSON.parse(readFileSync(
  fileURLToPath(new URL('../fixtures/legacy_snippets/hash_vectors.json', import.meta.url)), 'utf8'));

function decodeRecipe(s) {
  return JSON.parse(utf8Decode(inflateRaw(b64urlDecode(s.slice(2)))));
}

function decodeFrame(s) {
  const b = b64urlDecode(s.slice(4));
  const dv = new DataView(b.buffer, b.byteOffset, b.byteLength);
  const out = [];
  for (let i = 0; i < b.byteLength; i += 4) out.push(dv.getFloat32(i, true));
  return out;
}

test('vectors cover every grammar', () => {
  const used = new Set(VECTORS.cases.flatMap((c) => c.grammars));
  for (const g of GRAMMAR_NAMES) assert.ok(used.has(g), `no vector for ${g}`);
  assert.deepEqual([...GRAMMAR_NAMES].sort(), Object.keys(GRAMMARS).sort());
});

for (const c of VECTORS.cases) {
  test(`vector: ${c.name}`, () => {
    const r = mapLegacy(c.grammars, c.loc);
    assert.deepEqual(r.consumedSearch, c.consumedSearch);
    assert.deepEqual(Object.keys(r.hash).sort(), Object.keys(c.hash).sort());
    for (const [k, v] of Object.entries(c.hash)) {
      if (k === 'sel' && v.startsWith('r:')) {
        // deflate output differs between zlib and selection.js; compare the recipe
        assert.ok(r.hash.sel.startsWith('r:'));
        assert.deepEqual(decodeRecipe(r.hash.sel), c.decoded.sel);
      } else {
        assert.equal(r.hash[k], v, k);
      }
      if (k.endsWith('.frame') && v.startsWith('b64:')) {
        assert.deepEqual(decodeFrame(r.hash[k]), c.decoded[k]);
      }
    }
  });
}

test('mapped hashes are canonical', () => {
  for (const c of VECTORS.cases) {
    const r = mapLegacy(c.grammars, c.loc);
    for (const k of Object.keys(r.hash)) {
      assert.ok(CANONICAL_KEYS.includes(k) || /^.+\.(x|y|frame)$/.test(k), `${c.name}: ${k}`);
    }
  }
});

test('isCanonicalHash', () => {
  assert.equal(isCanonicalHash('id=a&c=f200w'), true);
  assert.equal(isCanonicalHash('man.frame=pca'), true);
  assert.equal(isCanonicalHash('name=a'), false);
  assert.equal(isCanonicalHash('view=sed'), false);
  assert.equal(isCanonicalHash('view=sed:full'), true);
  assert.equal(isCanonicalHash('abc'), false);
  assert.equal(isCanonicalHash(''), false);
});

test('legacyContext reads the manifold pane of the page JSON', () => {
  const page = {layout: {row: [{pane: 'manifold', id: 'm2', dataset: 'cat', options: {dims: ['x', 'y']}}]}};
  assert.deepEqual(legacyContext({page}), {dims: [{key: 'x'}, {key: 'y'}], pane: 'm2', ds: 'cat'});
  const ctx = legacyContext({dims: [{key: 'a', scale: 3}, 'b']});
  assert.deepEqual(ctx.dims, [{key: 'a', scale: 3}, {key: 'b'}]);
  assert.equal(ctx.pane, 'manifold');
});

test('codecs', () => {
  assert.deepEqual(decodeFrame(encodeFrame([1, -0.5, 2])), [1, -0.5, 2]);
  assert.deepEqual(JSON.parse(utf8Decode(b64urlDecode(encodeJSON({a: [1, 2.5]})))), {a: [1, 2.5]});
  assert.deepEqual(decodeRecipe(encodeRecipe([{op: 'replace', kind: 'ids', ids: ['a']}])),
    [{op: 'replace', kind: 'ids', ids: ['a']}]);
});

test('no grammars, empty location', () => {
  assert.deepEqual(mapLegacy([], {hash: '#abc'}), {hash: {}, consumedSearch: []});
  assert.deepEqual(mapLegacy(['bare-name'], {}), {hash: {}, consumedSearch: []});
  assert.deepEqual(mapLegacy('bare-name', {hash: '#x'}).hash, {id: 'x'});
});

test('layoutInfo lists pane ids and the manifold pane', () => {
  const page = {layout: {row: [{pane: 'sky'}, {col: [{pane: 'manifold', id: 'm'}, {pane: 'manifold', id: 'm2'}]}]}};
  const info = layoutInfo({page});
  assert.deepEqual([...info.ids].sort(), ['m', 'm2', 'sky']);
  assert.equal(info.manifold, 'm');
  assert.equal(layoutInfo({}), null);
});

test('gc-query without a dim list leaves dim-indexed parameters in the URL', () => {
  const page = {layout: {row: [{pane: 'manifold', id: 'm', dataset: 'gc', options: {dims: 'group:mags'}}]}};
  const r = mapLegacy(['gc-query'], {search: '?a=0&b=1&m=1&y=f480m&cb=0&sel=266.1,-29', page});
  assert.deepEqual(r.consumedSearch, ['sel']);
  assert.deepEqual(Object.keys(r.hash), ['sel']);
});
