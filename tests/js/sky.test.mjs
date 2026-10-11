// CDS HiPS identifiers in pk-hips/1 layer urls pass through to Aladin unresolved.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {isSurveyId, surveyUrl} from '../../web/js/sky/aladin.js';
import {SkyLayers} from '../../web/js/sky/layers.js';

const resolve = (u) => new URL(u, 'https://example.org/portal/index.html');

test('isSurveyId accepts CDS ids and rejects bundle paths and URLs', () => {
  for (const id of ['CDS/P/DSS2/color', 'P/2MASS/color', 'P/DSS2/color', 'ov-gso/P/GLIMPSE/irac1',
    'JAXA/P/AKARI/FIS/Color', 'CDS/P/PanSTARRS/DR1/color-z-zg-g']) {
    assert.equal(isSurveyId(id), true, id);
  }
  for (const u of ['data/hips/f444w/', 'data/P/x', '_res/P/y', 'https://alasky.cds.unistra.fr/DSS/DSSColor/',
    '{assets}/hips/', 'hips/f200w', './P/x', '../P/x', '', null, undefined, 'ivo://CDS/P/DSS2/color']) {
    assert.equal(isSurveyId(u), false, String(u));
  }
});

test('surveyUrl passes ids through and resolves the rest', () => {
  assert.equal(surveyUrl('P/2MASS/color', resolve), 'P/2MASS/color');
  assert.equal(surveyUrl('CDS/P/DSS2/color', resolve), 'CDS/P/DSS2/color');
  assert.equal(surveyUrl('data/hips/f444w/', resolve), 'https://example.org/portal/data/hips/f444w/');
  assert.equal(surveyUrl('https://alasky.cds.unistra.fr/2MASS/Color/', resolve),
    'https://alasky.cds.unistra.fr/2MASS/Color/');
  assert.equal(surveyUrl('data/x/', null), 'data/x/');
});

test('SkyLayers keeps CDS ids out of the page resolver', () => {
  const seen = [];
  const L = new SkyLayers(null, {
    hips: {base: [{id: 'dss', name: 'DSS', url: 'CDS/P/DSS2/color', default: true}],
      overlays: [{id: 'm', name: 'Mosaic', url: 'data/hips/m/', opacity: 1}], fits: []},
    resolve: (u) => { seen.push(u); return resolve(u).toString(); },
  });
  assert.equal(L.resolve('CDS/P/DSS2/color'), 'CDS/P/DSS2/color');
  assert.equal(L.resolve('data/hips/m/'), 'https://example.org/portal/data/hips/m/');
  assert.deepEqual(seen, ['data/hips/m/']);
});
