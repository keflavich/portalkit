// node --test tests/js/aladin_sources.test.mjs
//
// loadAladin walks the list aladinSources returns, so this list is what keeps a
// page's sky view alive when CDS is down: it must hold CDS first and the
// UF-hosted fallback second.  tests/py/test_aladin_defaults.py checks the
// constants; these check that the fallback actually reaches the list.
import test from 'node:test';
import assert from 'node:assert/strict';
import {aladinSources, DEFAULT_CDN, DEFAULT_MIRROR} from '../../web/js/sky/aladin.js';

test('a page with no aladin settings tries CDS, then the UF fallback', () => {
  assert.deepEqual(aladinSources({}), [DEFAULT_CDN, DEFAULT_MIRROR]);
});

test('a page that names only its cdn still gets the UF fallback', () => {
  const cdn = 'https://aladin.cds.unistra.fr/AladinLite/api/v3/3.8.2/aladin.js';
  assert.deepEqual(aladinSources({aladin: {cdn}}), [cdn, DEFAULT_MIRROR]);
});
