// WP10 unit tests: panes/sed.js (SED series, units, limits, extents, nearest point, blackbody
// fit, sedcurve models, template scale), spec/features.js (Doppler, coverage, collision
// dropping, dashed requires_coverage features) and the pure helpers of panes/spectrum1d.js and
// panes/spectrum2d.js (autoY, pins, channels, decimation, masks, bands, pki1 stretch/resample).
// Run: node --test tests/js/sed.test.mjs
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

import {makeView} from '../../web/js/plot/axes.js';
import {sedPoints, nufnu, blackbodyFnu, C_UM_S} from '../../web/js/data/filters.js';
import {
  sedSeries, convertFlux, normUnits, sedExtent, nearestPoint, fitBlackbody, bbAmplitude, bbCurve,
  logGrid, kInterp, sedcurveSeries, resolveBands, templateScaleFor, waveFactor, gridForGrating,
  datasetOfKind, focusIndex, meta as sedMeta,
} from '../../web/js/panes/sed.js';
import {
  C_KMS, dopplerFactor, familiesOf, featureList, hasCoverage, coverageOf, layoutFeatures, featureAt,
  drawFeatures, isSorted, lowerIndex, LABEL_MAX,
} from '../../web/js/spec/features.js';
import {
  normalizeRecord, channelFor, errChannelName, variantsOf, visibleRange, segmentRanges, segmentLabels,
  autoY, xExtent, togglePin, maskSpans, bandWindows, templateScale, decimate, errBand, validRange, sig7,
  isErrChannel, PIN_MAX, effectiveVariant, linkedFullExtent, meta as s1Meta,
} from '../../web/js/panes/spectrum1d.js';
import {
  nearestIndex, stretchCodes, resample, imageExtent, pngSourceRect, MISSING, meta as s2Meta,
} from '../../web/js/panes/spectrum2d.js';

const G = new URL('../golden/physics/', import.meta.url);
const load = (f) => JSON.parse(readFileSync(new URL(f, G), 'utf8'));
const INDEX = load('index.json');
const RTOL = INDEX.rtol, ATOL = INDEX.atol;

function close(got, want, what, rtol = RTOL) {
  if (want === null) {
    assert.ok(got === null || Number.isNaN(got), `${what}: expected NaN/null, got ${got}`);
    return;
  }
  assert.equal(typeof got, 'number', `${what}: got ${got}`);
  assert.ok(Math.abs(got - want) <= ATOL + rtol * Math.abs(want), `${what}: got ${got}, want ${want}`);
}

const lin = (d0, d1, extra = {}) => makeView({width: 600, height: 300, margin: {l: 50, r: 10, t: 10, b: 30},
  x: {kind: 'lin', d0, d1}, y: {kind: 'lin', d0: 0, d1: 1}, ...extra});

// ------------------------------------------------------------------ module shape

test('pane modules export meta and mount', () => {
  assert.equal(sedMeta.type, 'sed');
  assert.equal(s1Meta.type, 'spectrum1d');
  assert.equal(s2Meta.type, 'spectrum2d');
  assert.ok(s1Meta.caps.includes('spectrum_viewer_1d'));
  assert.ok(sedMeta.caps.includes('live_sed_viewer'));
  assert.ok(s2Meta.caps.includes('spectrum_2d_viewer'));
});

// ------------------------------------------------------------------ SED series vs goldens

const SP = load('sed_points.json');

test('sedSeries in F_nu reproduces the golden SED points (values, errors, limits)', () => {
  for (const c of SP.cases) {
    const pts = sedPoints(c.row, SP.filters, SP.cols);
    const ser = sedSeries(pts, 'fnu', SP.filters);
    const want = c.points.filter((p) => p.fnu > 0);
    assert.equal(ser.length, want.length, `case ${c.row.id}`);
    ser.forEach((s, k) => {
      const w = want[k];
      close(s.x, w.wl, `${c.row.id}[${k}].x`);
      close(s.y, w.fnu, `${c.row.id}[${k}].y`);
      assert.equal(s.kind, w.kind);
      assert.equal(s.limit, w.kind === 'ul' ? 'u' : w.kind === 'll' ? 'l' : null);
      if (s.limit) assert.equal(s.ey, null, 'limits carry no error bar');
      else close(s.ey, w.err, `${c.row.id}[${k}].ey`);
      const f = SP.filters[w.filter];
      if (f && f.weff_um > 0) close(s.ex, f.weff_um / 2, 'ex = weff/2');
      assert.ok(typeof s.label === 'string' && s.label.length);
    });
  }
});

test('sedSeries in nuFnu scales value and error by c/lambda (W m^-2)', () => {
  const c = SP.cases.find((x) => x.row.id === 'all');
  const pts = sedPoints(c.row, SP.filters, SP.cols);
  const ser = sedSeries(pts, 'nufnu', SP.filters);
  ser.forEach((s, k) => {
    const p = pts[k];
    const want = (p.fnu * 1e-26 * C_UM_S) / p.wl;
    close(s.y, want, `nufnu[${k}]`);
    if (p.err !== null) close(s.ey, (p.err * 1e-26 * C_UM_S) / p.wl, `nufnu err[${k}]`);
  });
});

test('convertFlux matches the nufnu goldens and rejects invalid input', () => {
  const N = load('nufnu.json');
  for (const r of N.nufnu) {
    const got = convertFlux(r.wl_um, r.fnu_jy, 'nufnu');
    if (r.nufnu_w_m2 === null) assert.ok(Number.isNaN(got));
    else close(got, r.nufnu_w_m2, `nufnu(${r.wl_um}, ${r.fnu_jy})`);
    if (Number.isFinite(r.fnu_jy) && r.wl_um > 0) assert.equal(convertFlux(r.wl_um, r.fnu_jy, 'fnu'), r.fnu_jy);
  }
  assert.ok(Number.isNaN(convertFlux(NaN, 1, 'fnu')));
  assert.ok(Number.isNaN(convertFlux(1, Infinity, 'nufnu')));
  assert.ok(Number.isNaN(convertFlux(0, 1, 'nufnu')));
  assert.equal(normUnits('nuFnu'), 'nufnu');
  assert.equal(normUnits('nu_fnu'), 'nufnu');
  assert.equal(normUnits('bogus'), 'fnu');
  assert.equal(normUnits(undefined), 'fnu');
});

test('sedSeries skips non-positive / non-finite values and handles empty input', () => {
  assert.deepEqual(sedSeries([], 'fnu'), []);
  assert.deepEqual(sedSeries(null, 'fnu'), []);
  const ser = sedSeries([
    {wl: 1, fnu: 0, err: null, kind: 'det', key: 'a', filter: 'X'},
    {wl: 2, fnu: NaN, err: null, kind: 'det', key: 'b', filter: 'X'},
    {wl: 3, fnu: Infinity, err: null, kind: 'det', key: 'c', filter: 'X'},
    {wl: 4, fnu: 1e30, err: 1e29, kind: 'det', key: 'α-光', filter: null},
    {wl: 5, fnu: 2, err: -1, kind: 'ul', key: 'u', filter: 'NOPE'},
  ], 'fnu', null);
  assert.equal(ser.length, 2);
  assert.equal(ser[0].y, 1e30);
  assert.equal(ser[0].ey, 1e29);
  assert.equal(ser[0].label, 'α-光', 'unicode key used as the label when there is no filter');
  assert.equal(ser[0].ex, null);
  assert.equal(ser[1].limit, 'u');
  assert.equal(ser[1].ey, null);
});

test('sedExtent: N=0 defaults, N=1 padding, error bars, NaN skipped, curves only without points', () => {
  assert.deepEqual(sedExtent([], [], 'fnu'), {x: [0.5, 30], y: [1e-6, 1]});
  assert.deepEqual(sedExtent([], [], 'nufnu'), {x: [0.5, 30], y: [1e-20, 1e-12]});
  const one = sedExtent([{x: 2, y: 1e-3}], [], 'fnu');
  close(one.x[0], 2 / Math.pow(10, 0.5), 'x0');
  close(one.x[1], 2 * Math.pow(10, 0.5), 'x1');
  close(one.y[0], 1e-4, 'y0');
  close(one.y[1], 1e-2, 'y1');
  const many = sedExtent([{x: 1, y: 1, ey: 0.5}, {x: 10, y: 100}, {x: NaN, y: 5}, {x: 3, y: -1}], [], 'fnu');
  close(many.x[0], 1 / Math.pow(10, 0.08), 'x0 pad');
  close(many.x[1], 10 * Math.pow(10, 0.08), 'x1 pad');
  close(many.y[0], 0.5 / Math.pow(10, 0.25), 'y0 includes the error bar');
  close(many.y[1], 100 * Math.pow(10, 0.25), 'y1');
  const curvesOnly = sedExtent([], [{x: [1, 2, 4], y: [1, NaN, 4]}], 'fnu');
  assert.ok(curvesOnly.x[0] < 1 && curvesOnly.x[1] > 4);
  const ignored = sedExtent([{x: 2, y: 1}], [{x: [100], y: [1e9]}], 'fnu');
  assert.ok(ignored.y[1] < 1e9, 'models do not drive the extent when points exist');
});

test('nearestPoint finds the closest marker within the pixel radius', () => {
  const view = makeView({width: 400, height: 300, x: {kind: 'log', d0: 1, d1: 100}, y: {kind: 'log', d0: 1e-3, d1: 1}});
  const ser = [{x: 2, y: 0.01}, {x: 20, y: 0.1}, {x: NaN, y: 0.1}];
  const [px, py] = view.toPx(20, 0.1);
  const hit = nearestPoint(ser, view, px + 3, py - 2, 24);
  assert.equal(hit.index, 1);
  assert.ok(hit.distPx < 4);
  assert.equal(nearestPoint(ser, view, px + 200, py, 24), null);
  assert.equal(nearestPoint([], view, px, py), null);
});

// ------------------------------------------------------------------ blackbody

test('fitBlackbody recovers the temperature of a synthetic blackbody', () => {
  for (const T of [600, 3000, 12000]) {
    const wls = [0.9, 1.25, 1.65, 2.2, 3.6, 4.5, 5.8, 8.0];
    const amp = 1e-3 / blackbodyFnu(2.2, T);
    const pts = wls.map((wl) => ({wl, fnu: amp * blackbodyFnu(wl, T), err: null, kind: 'det'}));
    const fit = fitBlackbody(pts);
    assert.ok(fit, `fit at ${T} K`);
    assert.ok(Math.abs(fit.T / T - 1) < 0.01, `T=${T}: got ${fit.T}`);
    assert.ok(Math.abs(fit.amp / amp - 1) < 0.03, `amp at ${T}`);
    assert.equal(fit.n, wls.length);
  }
});

test('fitBlackbody with extinction recovers T of a reddened blackbody; limits are ignored', () => {
  const T = 4000, av = 20;
  const law = {w: [0.5, 1, 2, 5, 10], k: [1.5, 0.4, 0.12, 0.03, 0.04]};
  const kAt = kInterp(law);
  const wls = [1.1, 1.6, 2.1, 3.0, 4.4, 8];
  const pts = wls.map((wl) => ({wl, fnu: 1e10 * blackbodyFnu(wl, T) * Math.pow(10, -0.4 * kAt(wl) * av), err: null, kind: 'det'}));
  pts.push({wl: 0.8, fnu: 1e5, err: null, kind: 'ul'});
  const fit = fitBlackbody(pts, {kAt, av});
  assert.ok(Math.abs(fit.T / T - 1) < 0.01, `got ${fit.T}`);
  const naive = fitBlackbody(pts);
  assert.ok(naive.T < fit.T, 'ignoring extinction gives a cooler fit');
  const a = bbAmplitude(pts, T, {kAt, av});
  assert.ok(Math.abs(a.amp / 1e10 - 1) < 1e-6);
  const curve = bbCurve(T, a.amp, logGrid(1, 10, 5), {units: 'fnu', kAt, av});
  close(curve.y[0], 1e10 * blackbodyFnu(1, T) * Math.pow(10, -0.4 * kAt(1) * av), 'curve value', 1e-9);
  const cnu = bbCurve(T, a.amp, [2], {units: 'nufnu'});
  close(cnu.y[0], nufnu(2, 1e10 * blackbodyFnu(2, T)), 'nufnu curve', 1e-9);
});

test('fitBlackbody edge cases: N=0/1, NaN, limits only', () => {
  assert.equal(fitBlackbody([]), null);
  assert.equal(fitBlackbody([{wl: 2, fnu: 1, kind: 'det'}]), null);
  assert.equal(fitBlackbody([{wl: 2, fnu: NaN, kind: 'det'}, {wl: 3, fnu: Infinity, kind: 'det'}]), null);
  assert.equal(fitBlackbody([{wl: 2, fnu: 1, kind: 'ul'}, {wl: 3, fnu: 1, kind: 'll'}]), null);
  assert.equal(bbAmplitude([], 3000), null);
  assert.equal(bbAmplitude([{wl: 2, fnu: 1, kind: 'det'}], 0), null);
  const flatBlue = fitBlackbody([{wl: 1, fnu: 1, kind: 'det'}, {wl: 2, fnu: 0.25, kind: 'det'}]);
  assert.ok(flatBlue.T > 0 && Number.isFinite(flatBlue.T));
  assert.equal(typeof flatBlue.edge, 'boolean');
});

test('logGrid and kInterp', () => {
  const g = logGrid(1, 100, 3);
  close(g[0], 1, 'g0'); close(g[1], 10, 'g1'); close(g[2], 100, 'g2');
  assert.equal(logGrid(0, 1).length, 0);
  assert.equal(logGrid(2, 1).length, 0);
  assert.equal(logGrid(1, Infinity).length, 0);
  const k = kInterp({w: [1, 2], k: [1, 0]});
  assert.equal(k(1.5), 0.5);
  assert.equal(k(5), 0, 'outside the law -> no extinction');
  assert.equal(kInterp(null)(2), 0);
});

// ------------------------------------------------------------------ sedcurve models, templates

test('sedcurveSeries converts units, skips error and unknown-unit channels', () => {
  const rec = {
    wave: Float64Array.from([1000, 2000, 4000]), unitW: 'nm',
    channels: {ap1: Float32Array.from([1, 2, NaN]), ap2: Float32Array.from([5, 0, 3]), err: Float32Array.from([1, 1, 1]), weird: Float32Array.from([1, 1, 1])},
    units: {ap1: 'mJy', ap2: 'Jy', err: 'Jy', weird: 'furlongs'},
  };
  const cs = sedcurveSeries(rec, 'fnu');
  assert.deepEqual(cs.map((c) => c.name), ['ap1', 'ap2']);
  assert.deepEqual(cs.skipped, ['weird']);
  close(cs[0].x[1], 2, 'nm -> um');
  close(cs[0].y[1], 2e-3, 'mJy -> Jy');
  assert.ok(Number.isNaN(cs[0].y[2]));
  assert.ok(Number.isNaN(cs[1].y[1]), 'zero flux is not drawable on a log axis');
  const nu = sedcurveSeries(rec, 'nufnu');
  close(nu[1].y[0], nufnu(1, 5), 'nufnu');
  assert.deepEqual(Array.from(sedcurveSeries(null)), []);
  const badW = sedcurveSeries({...rec, unitW: 'parsec'});
  assert.equal(badW.length, 0);
  assert.equal(badW.skipped.length, 4);
});

test('waveFactor and gridForGrating', () => {
  assert.equal(waveFactor('um'), 1);
  assert.equal(waveFactor('µm'), 1);
  assert.equal(waveFactor('Angstrom'), 1e-4);
  assert.equal(waveFactor(null), 1);
  assert.throws(() => waveFactor('lightyear'), RangeError);
  const grids = ['prism', 'medium', 'high'];
  assert.equal(gridForGrating('G235H', grids), 'high');
  assert.equal(gridForGrating('G395M', grids), 'medium');
  assert.equal(gridForGrating('PRISM', grids), 'prism');
  assert.equal(gridForGrating('', grids), 'prism');
  assert.equal(gridForGrating('G140H', ['medium']), 'medium');
  assert.equal(gridForGrating('G140H', []), null);
});

test('templateScaleFor: numeric scale as is, auto = scaleTo goldens, band names resolved', () => {
  const S = load('scale_to.json');
  for (const c of S.cases) {
    const got = templateScaleFor(S.model, c.points, {key: 't', scale: 'auto', bands: c.bands || undefined}, null);
    close(got, c.scale, `scale_to ${JSON.stringify(c.bands || null)}`);
  }
  assert.equal(templateScaleFor(S.model, [], {key: 't', scale: 2.5}, null), 2.5);
  assert.ok(Number.isNaN(templateScaleFor(S.model, [], {key: 't', scale: 'auto'}, null)));
  // bands by label resolve to the filter keys of the points
  const keys = resolveBands(['F182M', 'ks', ''], SP.filters);
  assert.ok(keys.includes('JWST/NIRCam.F182M'));
  assert.ok(keys.includes('2MASS/2MASS.Ks'));
  assert.equal(resolveBands([], SP.filters), null);
  assert.equal(resolveBands(null, SP.filters), null);
  const c = SP.cases.find((x) => x.row.id === 'all');
  const pts = sedPoints(c.row, SP.filters, SP.cols);
  const model = {wave: [1, 30], flux: [1, 1]};
  const s1 = templateScaleFor(model, pts, {key: 't', scale: 'auto', bands: ['F182M']}, SP.filters);
  close(s1, pts.find((p) => p.key === 'f182m').fnu, 'normalised on F182M only');
});

test('datasetOfKind and focusIndex', async () => {
  const ctx = {app: {page: {datasets: {src: {kind: 'catalog'}, f: {kind: 'filters'}, f2: {kind: 'filters'}}},
    resolveIds: async (ds, ids) => ids.map((id) => (id === 'a' ? 7 : -1))}};
  assert.equal(datasetOfKind(ctx, 'filters'), 'f');
  assert.equal(datasetOfKind(ctx, 'filters', 'f2'), 'f2');
  assert.equal(datasetOfKind(ctx, 'templates'), null);
  assert.equal(await focusIndex(ctx, 'src', null), null);
  assert.equal(await focusIndex(ctx, 'src', {ds: 'src', i: 3, id: null}), 3);
  assert.equal(await focusIndex(ctx, 'src', {ds: 'src', i: null, id: 'a'}), 7);
  assert.equal(await focusIndex(ctx, 'src', {ds: 'other', i: 3, id: 'zz'}), null);
});

// ------------------------------------------------------------------ features

const FDOC = {
  schema: 'pk-features/1',
  meta: {families: {gas: {label: 'Gas', color: '#123456', source: 'test'}, hi: {label: 'H I', source: 'x'}}},
  families: {
    gas: [
      {name: 'CO', w_um: 4.67, label: 'CO', kind: 'band', lo_um: 4.5, hi_um: 4.8},
      {name: 'H2O', w_um: 3.0, label: 'H₂O ice', kind: 'band', lo_um: 3.2, hi_um: 2.8},
      {name: 'bad', w_um: null, label: 'bad', kind: 'line'},
    ],
    hi: [
      {name: 'Brα', w_um: 4.0523, label: 'Brα', kind: 'line', requires_coverage: true},
      {name: 'Paα', w_um: 1.8756, label: 'Paα', kind: 'line', color: '#ff0000'},
    ],
  },
};

test('familiesOf and featureList (sorting, colours, bad entries, unicode, Doppler)', () => {
  const fams = familiesOf(FDOC);
  assert.deepEqual(fams.map((f) => f.key), ['gas', 'hi']);
  assert.equal(fams[0].count, 3);
  assert.equal(fams[1].label, 'H I');
  assert.deepEqual(familiesOf(null), []);
  const fl = featureList(FDOC, ['hi', 'gas', 'nope']);
  assert.deepEqual(fl.map((f) => f.name), ['Paα', 'H2O', 'Brα', 'CO']);
  const h2o = fl.find((f) => f.name === 'H2O');
  assert.equal(h2o.lo, 2.8);
  assert.equal(h2o.hi, 3.2, 'lo/hi swapped into order');
  assert.equal(h2o.color, '#123456', 'family colour');
  assert.equal(fl.find((f) => f.name === 'Paα').color, '#ff0000', 'feature colour wins');
  assert.equal(fl.find((f) => f.name === 'Brα').requiresCoverage, true);
  assert.deepEqual(featureList(FDOC, []), []);
  assert.deepEqual(featureList(null, ['hi']), []);
  const v = 3000;
  const sh = featureList(FDOC, ['hi'], {vel: v});
  close(sh[0].w, 1.8756 * (1 + v / C_KMS), 'Doppler-shifted');
  assert.equal(sh[0].w0, 1.8756);
  assert.equal(dopplerFactor(NaN), 1);
  assert.equal(dopplerFactor(-C_KMS * 2), 1);
  assert.equal(dopplerFactor(0), 1);
});

test('hasCoverage: gaps, NaN flux, edges, N=0/1, unsorted', () => {
  const wave = Float64Array.from([1.0, 1.01, 1.02, 1.03, 2.0, 2.01, 2.02]);
  const flux = Float32Array.from([1, 1, NaN, 1, 1, 1, 1]);
  assert.equal(hasCoverage(wave, flux, 1.005), true);
  assert.equal(hasCoverage(wave, flux, 1.5), false, 'inside the gap between segments');
  assert.equal(hasCoverage(wave, flux, 1.02), true, 'NaN sample but finite neighbour within 1.5 spacing');
  assert.equal(hasCoverage(wave, Float32Array.from([1, 1, NaN, NaN, NaN, 1, 1]), 1.025), false);
  assert.equal(hasCoverage(wave, flux, 0.5), false);
  assert.equal(hasCoverage(wave, flux, 3), false);
  assert.equal(hasCoverage(wave, flux, NaN), false);
  assert.equal(hasCoverage([], [], 1), false);
  assert.equal(hasCoverage([2], [1], 2), true);
  assert.equal(hasCoverage([2], [1], 2.1), false);
  assert.equal(hasCoverage([2], [1], 2.1, 0.2), true, 'explicit tolerance');
  assert.equal(hasCoverage([3, 1, 2], [1, 1, 1], 2.0), true, 'unsorted wave falls back to a scan');
  assert.equal(isSorted([1, 2, 2, 3]), true);
  assert.equal(isSorted([1, 3, 2]), false);
  assert.equal(lowerIndex([1, 2, 3], 2.5), 1);
  assert.equal(lowerIndex([1, 2, 3], 0), -1);
  assert.equal(lowerIndex([1, 2, 3], 9), 2);
  const cov = coverageOf({wave, flux});
  assert.equal(cov(1.01), true);
  assert.equal(coverageOf(null), null);
});

test('layoutFeatures: collision dropping into rows, LABEL_MAX, dashed uncovered lines', () => {
  const view = lin(1, 2);
  // 20 features 0.002 um apart = 6 px apart: far closer than a label width
  const feats = Array.from({length: 20}, (_, k) => ({name: `L${k}`, label: `Line ${k}`, w: 1.4 + 0.002 * k, kind: 'line', lo: null, hi: null, color: '#000'}));
  const lay = layoutFeatures(feats, view, {rows: 3, minGap: 6, measure: (t) => t.length * 6});
  assert.equal(lay.shown, 20, 'every marker line is kept');
  assert.ok(lay.labelled >= 3 && lay.labelled < 20, `labelled ${lay.labelled}`);
  assert.equal(lay.dropped, 20 - lay.labelled);
  assert.ok(lay.items.filter((i) => i.labelled).every((i) => i.row >= 0 && i.row < 3));
  // labels in one row never overlap
  for (let r = 0; r < 3; r++) {
    const row = lay.items.filter((i) => i.labelled && i.row === r).sort((a, b) => a.labelX - b.labelX);
    for (let k = 1; k < row.length; k++) assert.ok(row[k].labelX >= row[k - 1].labelX + row[k - 1].labelW + 6 - 1e-9);
  }
  // spread-out features all get labels
  const sparse = feats.slice(0, 5).map((f, k) => ({...f, w: 1.1 + 0.2 * k}));
  assert.equal(layoutFeatures(sparse, view, {measure: (t) => t.length * 6}).labelled, 5);
  // more than LABEL_MAX in view: lines only
  const many = Array.from({length: LABEL_MAX + 1}, (_, k) => ({name: `m${k}`, label: `m${k}`, w: 1.01 + 0.025 * k, kind: 'line', lo: null, hi: null, color: '#000'}));
  const lm = layoutFeatures(many, view, {});
  assert.equal(lm.named, false);
  assert.equal(lm.labelled, 0);
  assert.equal(lm.shown, LABEL_MAX + 1);
  // out of range features are not shown; bands overlapping the edge are
  const edge = layoutFeatures([
    {name: 'out', label: 'out', w: 5, kind: 'line', lo: null, hi: null, color: '#000'},
    {name: 'band', label: 'band', w: 0.9, kind: 'band', lo: 0.8, hi: 1.1, color: '#000'},
  ], view, {});
  assert.deepEqual(edge.items.map((i) => i.f.name), ['band']);
  // requires_coverage -> dashed where the spectrum has no data
  const wave = Float64Array.from({length: 101}, (_, i) => 1 + i * 0.004);
  const flux = Float32Array.from(wave, (w) => (w > 1.5 && w < 1.7 ? NaN : 1));
  const rc = layoutFeatures([
    {name: 'He I a', label: 'He I', w: 1.6, kind: 'line', lo: null, hi: null, color: '#000', requiresCoverage: true},
    {name: 'He I b', label: 'He I', w: 1.2, kind: 'line', lo: null, hi: null, color: '#000', requiresCoverage: true},
    {name: 'Other', label: 'Other', w: 1.62, kind: 'line', lo: null, hi: null, color: '#000', requiresCoverage: false},
  ], view, {coverage: coverageOf({wave, flux})});
  const byName = Object.fromEntries(rc.items.map((i) => [i.f.name, i]));
  assert.equal(byName['He I a'].dashed, true);
  assert.equal(byName['He I b'].dashed, false);
  assert.equal(byName.Other.dashed, false);
  assert.equal(rc.dashed, 1);
  // N=0 / no rows
  assert.equal(layoutFeatures([], view).shown, 0);
  assert.equal(layoutFeatures(sparse, view, {rows: 0}).labelled, 0);
});

test('featureAt and drawFeatures (recording context)', () => {
  const view = lin(1, 2);
  const fl = featureList(FDOC, ['gas', 'hi']).map((f) => ({...f, w: f.w / 2.5, lo: f.lo === null ? null : f.lo / 2.5, hi: f.hi === null ? null : f.hi / 2.5}));
  const lay = layoutFeatures(fl, view, {coverage: () => false});
  assert.ok(lay.shown >= 3);
  const it = lay.items[0];
  assert.equal(featureAt(lay, it.px + 2, 4), it);
  assert.equal(featureAt(lay, -100, 4), null);
  assert.equal(featureAt(null, 10), null);
  const calls = [];
  const rec = new Proxy({}, {
    get(t, k) {
      if (k in t) return t[k];
      return (...a) => { calls.push([k, a]); };
    },
    set(t, k, v) { t[k] = v; calls.push(['set:' + String(k), v]); return true; },
  });
  const n = drawFeatures(rec, view, lay, {tokens: {plotBg: '#fff', font: 'sans-serif'}});
  assert.equal(n, lay.shown);
  assert.ok(calls.some(([k, a]) => k === 'setLineDash' && a[0].length === 2), 'dashed line drawn');
  assert.ok(calls.some(([k]) => k === 'fillText'), 'labels drawn');
  assert.ok(calls.some(([k]) => k === 'fillRect'), 'band shading drawn');
  assert.equal(drawFeatures(rec, view, {items: []}), 0);
});

// ------------------------------------------------------------------ spectrum1d helpers

test('autoY: min/max of the visible range with 8 % pad and y0 = min(0, lo - pad)', () => {
  const wave = Float64Array.from({length: 1001}, (_, i) => 1 + i * 0.004); // 1..5
  const y = Float32Array.from(wave, (w) => (w < 3 ? 10 + w : 100 + w));
  const s = [{wave, y}];
  const full = autoY(s, [1, 5]);
  close(full[0], 0, 'zero included');
  const hi = 105, lo = 11;
  close(full[1], hi + (hi - lo) * 0.08, 'top pad', 1e-5);
  const zoom = autoY(s, [1.5, 2.5]);
  close(zoom[1], 12.5 + (12.5 - 11.5) * 0.08, 'zoom follows the visible range', 1e-5);
  const noZero = autoY(s, [1.5, 2.5], {zero: false});
  close(noZero[0], 11.5 - 0.08, 'no zero', 1e-5);
  const neg = autoY([{wave: [1, 2, 3], y: [-5, -1, -3]}], [0, 4]);
  close(neg[0], -5 - 4 * 0.08, 'negative lo');
  close(neg[1], -1 + 4 * 0.08, 'negative hi');
  const lg = autoY([{wave: [1, 2, 3], y: [1e-3, -1, 10]}], [0, 4], {log: true});
  close(lg[0], Math.pow(10, -3 - 4 * 0.08), 'log lo');
  close(lg[1], Math.pow(10, 1 + 4 * 0.08), 'log hi');
  assert.equal(autoY([], [1, 2]), null);
  assert.equal(autoY([{wave: [1, 2], y: [NaN, Infinity]}], [0, 3]), null);
  assert.equal(autoY([{wave: [1, 2], y: [1, 2]}], [5, 6]), null, 'nothing visible');
  const flat = autoY([{wave: [1], y: [7]}], [0, 2], {zero: false});
  assert.ok(flat[0] < 7 && flat[1] > 7, 'N=1 is padded');
  const big = autoY([{wave: [1, 2], y: [1e30, 2e30]}], [0, 3]);
  assert.ok(Number.isFinite(big[1]) && big[1] > 2e30);
  // hidden segments are excluded
  const segd = autoY([{wave, y, ranges: [{i0: 0, i1: 500, on: true}, {i0: 500, i1: 1001, on: false}]}], [1, 5]);
  assert.ok(segd[1] < 20);
  // extra values (sed photometry) widen the range
  assert.ok(autoY(s, [1.5, 2.5], {extra: [50]})[1] > 50);
});

test('visibleRange, xExtent, validRange, sig7', () => {
  const w = Float64Array.from([1, 2, 3, 4, 5]);
  assert.deepEqual(visibleRange(w, 2.5, 3.5), [1, 4]);
  assert.deepEqual(visibleRange(w, 3.5, 2.5), [1, 4]);
  assert.deepEqual(visibleRange(w, 0, 10), [0, 5]);
  assert.deepEqual(visibleRange(w, 7, 9), [4, 5]);
  assert.deepEqual(visibleRange([], 0, 1), [0, 0]);
  assert.deepEqual(visibleRange([3, 1, 2], 0, 1, false), [0, 3]);
  assert.deepEqual(xExtent([{wave: w}]), [1, 5]);
  assert.deepEqual(xExtent([{wave: [NaN, 2, NaN]}]).map((v) => +v.toFixed(4)), [1.98, 2.02]);
  assert.equal(xExtent([]), null);
  assert.deepEqual(xExtent([{wave: [-1, 2, 3]}], true), [2, 3]);
  assert.deepEqual(validRange([3, 1]), [1, 3]);
  assert.equal(validRange([1, 1]), null);
  assert.equal(validRange([0, 1], true), null);
  assert.equal(validRange([NaN, 1]), null);
  assert.equal(validRange('1,2'), null);
  assert.equal(sig7(1.23456789), 1.234568);
  assert.ok(Number.isNaN(sig7(NaN)));
});

test('togglePin keeps at most 8 (or the option), toggles off, dedupes', () => {
  let p = [];
  for (let k = 0; k < 10; k++) p = togglePin(p, `s${k}`);
  assert.equal(p.length, PIN_MAX);
  assert.deepEqual(p, ['s2', 's3', 's4', 's5', 's6', 's7', 's8', 's9'], 'oldest dropped');
  p = togglePin(p, 's5');
  assert.ok(!p.includes('s5'));
  assert.equal(p.length, 7);
  assert.deepEqual(togglePin(['a'], 'b', 1), ['b']);
  assert.deepEqual(togglePin(['a'], 'b', 99), ['a', 'b'], 'option capped at 8');
  assert.deepEqual(togglePin(null, 7), ['7'], 'ids stored as strings');
  assert.deepEqual(togglePin(['x'], null), ['x']);
  assert.deepEqual(togglePin(['α'], 'α'), []);
});

test('normalizeRecord: pks1 shape, JSON spectra, wavelength units, PNG entries', () => {
  const pks = {id: 'a', n: 3, wave: Float64Array.from([1, 2, 3]), channels: {flux: Float32Array.from([1, 2, 3]), err: Float32Array.from([0.1, 0.1, 0.1]), norm: Float32Array.from([1, 1, 1])},
    units: {flux: 'Jy', err: 'Jy', norm: ''}, unitW: 'um', segments: [[0, 2, 'G235H'], [2, 3, 'G395H']], maskRuns: [[1, 2, 1]], maskBits: {1: 'quality'}, lines: [], meta: {grating: 'G235H'}};
  const r = normalizeRecord(pks);
  assert.equal(r.n, 3);
  assert.equal(r.sorted, true);
  assert.deepEqual(Array.from(r.wave), [1, 2, 3]);
  const j = normalizeRecord({id: 'j', wavelength: [10000, 20000, null], flux: [1, null, 3], unit_w: 'Angstrom', units: {flux: 'mJy'}});
  assert.deepEqual(Array.from(j.wave.slice(0, 2)), [1, 2]);
  assert.ok(Number.isNaN(j.wave[2]));
  assert.ok(Number.isNaN(j.channels.flux[1]));
  assert.equal(j.units.flux, 'mJy');
  assert.equal(normalizeRecord({id: 'p', kind: 'png', png: 'x.png'}), null);
  assert.equal(normalizeRecord({id: 'x', flux: [1]}), null);
  assert.equal(normalizeRecord(null), null);
  const weird = normalizeRecord({...pks, unitW: 'furlong'});
  assert.deepEqual(Array.from(weird.wave), [1, 2, 3], 'unknown wavelength unit kept as micron');
});

test('channelFor / variantsOf / error channels', () => {
  const rec = normalizeRecord({id: 'a', wave: [1, 2], channels: {flux: [1, 2], err: [0.1, 0.2], norm: [1, 1], norm_err: [0.01, 0.01], normcm: [2, 2]}, units: {flux: 'Jy'}});
  const f = channelFor(rec, 'flux');
  assert.equal(f.name, 'flux');
  assert.equal(f.unit, 'Jy');
  assert.equal(f.err, rec.channels.err);
  assert.equal(channelFor(rec, 'norm').err, rec.channels.norm_err);
  assert.equal(channelFor(rec, 'normcm').err, null);
  const fb = channelFor(rec, 'missing');
  assert.equal(fb.name, 'flux');
  assert.equal(fb.fallback, true);
  assert.equal(channelFor(null, 'flux'), null);
  const onlyOdd = normalizeRecord({wave: [1], channels: {err: [1], weird: [2]}});
  assert.equal(channelFor(onlyOdd, 'flux').name, 'weird');
  assert.deepEqual(variantsOf(rec, null, null), ['flux', 'norm', 'normcm']);
  assert.deepEqual(variantsOf(rec, {variants: ['flux', 'norm']}, null), ['flux', 'norm']);
  assert.deepEqual(variantsOf(rec, {variants: ['flux']}, ['normcm']), ['normcm']);
  assert.deepEqual(variantsOf(null, null, null), ['flux']);
  assert.equal(isErrChannel('e_flux'), true);
  assert.equal(isErrChannel('flux'), false);
  assert.equal(errChannelName(rec, 'normcm'), null);
});

test('effectiveVariant: select and plot agree when options.variants leaves out flux', () => {
  const vars = variantsOf(null, null, ['flux_nobg', 'norm']);
  assert.equal(effectiveVariant('flux', vars), 'flux_nobg');
  assert.equal(effectiveVariant('norm', vars), 'norm');
  assert.equal(effectiveVariant(undefined, vars), 'flux_nobg');
  assert.equal(effectiveVariant('flux', ['flux', 'norm']), 'flux');
  assert.equal(effectiveVariant('x', []), 'flux');
  assert.equal(effectiveVariant('x', null), 'flux');
  assert.equal(linkedFullExtent({}), null);
  assert.equal(linkedFullExtent(null), null);
});

test('segmentRanges / segmentLabels / maskSpans', () => {
  const rec = normalizeRecord({id: 'a', wave: [1, 1.1, 1.2, 2, 2.1], channels: {flux: [1, 1, 1, 1, 1]},
    segments: [[0, 3, 'G140H'], [3, 5, 'G235H'], [4, 99, 'X'], [5, 2, 'bad']], maskRuns: [[1, 2, 2], [4, 5, 9], [3, 3, 1]], maskBits: {2: 'CO extrapolation'}});
  const segs = segmentRanges(rec, {G235H: false});
  assert.deepEqual(segs.map((s) => [s.i0, s.i1, s.label, s.on]), [[0, 3, 'G140H', true], [3, 5, 'G235H', false], [4, 5, 'X', true]]);
  assert.deepEqual(segmentLabels(rec), ['G140H', 'G235H', 'X']);
  assert.deepEqual(segmentRanges(normalizeRecord({wave: [1, 2], channels: {flux: [1, 2]}})).map((s) => [s.i0, s.i1, s.label]), [[0, 2, null]]);
  assert.deepEqual(segmentRanges(null), []);
  const m = maskSpans(rec);
  assert.equal(m.length, 2, 'empty run dropped');
  close(m[0].w0, 1.05, 'half a sample left');
  close(m[0].w1, 1.15, 'half a sample right');
  assert.equal(m[0].label, 'CO extrapolation');
  assert.equal(m[1].label, 'mask 9');
  assert.ok(m[1].w1 > 2.1);
  assert.deepEqual(maskSpans(null), []);
});

test('bandWindows resolves option bands and filters; templateScale is a median ratio', () => {
  const bands = [{label: 'K', w0: 2.3, w1: 2.0}, {label: 'L', w0: 3.4, w1: 4.0}];
  assert.deepEqual(bandWindows(['K'], bands, null), [[2.0, 2.3]]);
  assert.deepEqual(bandWindows(['k'], bands, null), [[2.0, 2.3]], 'case-insensitive label');
  const fw = bandWindows(['F182M', 'nope', ''], bands, SP.filters);
  assert.equal(fw.length, 1);
  close(fw[0][0], 1.845167 - 0.225081 / 2, 'filter window lo');
  assert.deepEqual(bandWindows(null, bands, null), []);
  const wave = Float64Array.from({length: 401}, (_, i) => 1 + i * 0.01);
  const tplF = Float32Array.from(wave, (w) => 2 / w);
  const obs = {wave, y: Float32Array.from(wave, (w) => (3 * 2) / w), sorted: true};
  close(templateScale(obs, {wave, flux: tplF}, [[2, 3]]), 3, 'exact ratio', 1e-6);
  obs.y[150] = 1e9; // a spike inside the window barely moves the median
  close(templateScale(obs, {wave, flux: tplF}, [[2, 3]]), 3, 'median is robust', 1e-6);
  assert.ok(Number.isNaN(templateScale(obs, {wave, flux: tplF}, [[10, 11]])));
  assert.ok(Number.isNaN(templateScale(null, {wave, flux: tplF}, [[2, 3]])));
  const neg = {wave, y: Float32Array.from(wave, () => -1)};
  assert.ok(Number.isNaN(templateScale(neg, {wave, flux: tplF}, [[2, 3]])));
  // hidden segments are not used
  const hidden = templateScale({wave, y: obs.y, sorted: true}, {wave, flux: tplF}, [[1, 5]], [{i0: 0, i1: 401, on: false}]);
  assert.ok(Number.isNaN(hidden));
});

test('decimate: sparse passthrough, min/max per pixel when dense, NaN breaks', () => {
  const view = makeView({width: 110, height: 100, margin: {l: 5, r: 5, t: 5, b: 5}, x: {kind: 'lin', d0: 0, d1: 1}, y: {kind: 'lin', d0: -2, d1: 2}});
  const sparse = decimate([0.1, 0.2, NaN, 0.4], [0, 1, 1, Infinity], 0, 4, view.sx, view.sy);
  assert.equal(sparse.length, 4 + 2, 'two points then one break (trailing break not added twice)');
  assert.ok(Number.isNaN(sparse[4]));
  const N = 50000;
  const wave = Float64Array.from({length: N}, (_, i) => i / (N - 1));
  const y = Float32Array.from(wave, (w, i) => (i === 25000 ? 1.9 : Math.sin(40 * w)));
  const d = decimate(wave, y, 0, N, view.sx, view.sy);
  assert.ok(d.length / 2 <= 4 * 101 + 2, `decimated to ${d.length / 2} points`);
  const minPy = Math.min(...d.filter((_, k) => k % 2 === 1));
  close(minPy, view.sy.f(1.9), 'spike survives decimation', 1e-6);
  const gap = Float32Array.from(y);
  gap.fill(NaN, 20000, 30000);
  const dg = decimate(wave, gap, 0, N, view.sx, view.sy);
  assert.ok(dg.some((v) => Number.isNaN(v)), 'gap breaks the path');
  assert.deepEqual(decimate(wave, y, 10, 10, view.sx, view.sy), []);
  assert.deepEqual(decimate([], [], 0, 5, view.sx, view.sy), []);
  const one = decimate([0.5], [1], 0, 1, view.sx, view.sy);
  assert.equal(one.length, 2);
});

test('errBand strides and skips invalid samples', () => {
  const view = lin(0, 10);
  const w = Float64Array.from({length: 100}, (_, i) => i / 10);
  const y = Float32Array.from(w, () => 0.5);
  const e = Float32Array.from(w, (_, i) => (i === 3 ? NaN : 0.1));
  const b = errBand(w, y, e, 0, 100, view.sx, view.sy, 25);
  assert.equal(b.length, 25);
  assert.ok(b.every((p) => p === null || p.lo > p.hi), 'lo pixel below hi pixel');
  assert.deepEqual(errBand(w, y, null, 0, 100, view.sx, view.sy), []);
});

// ------------------------------------------------------------------ spectrum2d helpers

function img(nx, ny, enc, fill) {
  const data = enc === 'u16' ? new Uint16Array(nx * ny) : new Uint8Array(nx * ny);
  for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) data[j * nx + i] = fill(i, j);
  return {nx, ny, enc, data, stretch: {kind: 'linear', lo: 0, hi: 10}, wave: Float64Array.from({length: nx}, (_, i) => 2 + i * 0.1),
    y: Float64Array.from({length: ny}, (_, j) => -1 + j * 0.5), unitY: 'arcsec', segments: []};
}

test('nearestIndex: ascending, descending, outside, N=0/1', () => {
  const a = [1, 2, 3, 4];
  assert.equal(nearestIndex(a, 2.4), 1);
  assert.equal(nearestIndex(a, 2.6), 2);
  assert.equal(nearestIndex(a, 0.6), 0);
  assert.equal(nearestIndex(a, 0.4), -1);
  assert.equal(nearestIndex(a, 4.6), -1);
  assert.equal(nearestIndex([4, 3, 2, 1], 2.4), 2);
  assert.equal(nearestIndex([], 1), -1);
  assert.equal(nearestIndex([5], 5), 0);
  assert.equal(nearestIndex(a, NaN), -1);
});

test('stretchCodes: stored u8 / u16 (code 0 missing) and re-stretches of physical values', () => {
  const u8 = img(4, 2, 'u8', (i, j) => i * 80 + j);
  const s8 = stretchCodes(u8, 'stored');
  assert.deepEqual(Array.from(s8.codes), Array.from(u8.data));
  const u16 = img(3, 1, 'u16', (i) => [0, 1, 65535][i]);
  const s16 = stretchCodes(u16, 'stored');
  assert.deepEqual(Array.from(s16.codes), [MISSING, 0, 255]);
  const lin16 = stretchCodes(u16, 'linear', {qlo: 0, qhi: 1});
  assert.equal(lin16.codes[0], MISSING);
  assert.equal(lin16.codes[1], 0);
  assert.equal(lin16.codes[2], 255);
  close(lin16.lo, 0, 'lo');
  close(lin16.hi, 10, 'hi');
  const flat = stretchCodes(img(2, 2, 'u8', () => 7), 'asinh');
  assert.ok(Array.from(flat.codes).every((c) => c >= 0 && c <= 255), 'constant image stays in range');
  for (const m of ['sqrt', 'asinh', 'log']) {
    const s = stretchCodes(u8, m, {qlo: 0, qhi: 1});
    assert.equal(s.codes[0], 0, `${m} lo`);
    assert.equal(s.codes[7], 255, `${m} hi (max pixel i=3, j=1)`);
  }
});

test('resample maps wavelength columns and y rows through the view; outside is transparent', () => {
  const im = img(4, 2, 'u8', (i, j) => (j === 0 ? i : 100 + i));
  const codes = stretchCodes(im, 'stored').codes;
  const lut = new Uint8Array(768);
  for (let k = 0; k < 256; k++) { lut[3 * k] = k; lut[3 * k + 1] = 255 - k; lut[3 * k + 2] = 7; }
  // view x 1.95..2.45 (image covers 1.95..2.35), y -1.25..0.25 (rows at -1 and -0.5; 0 and above outside)
  const view = makeView({width: 100, height: 60, margin: {l: 0, r: 0, t: 0, b: 0}, x: {kind: 'lin', d0: 1.95, d1: 2.45}, y: {kind: 'lin', d0: -1.25, d1: 0.25}});
  const out = {width: 100, height: 60, data: new Uint8ClampedArray(100 * 60 * 4)};
  const hit = resample(im, codes, lut, view, out, 1);
  assert.equal(hit, 80, 'columns inside the image');
  const px = (x, y) => Array.from(out.data.slice((y * 100 + x) * 4, (y * 100 + x) * 4 + 4));
  assert.deepEqual(px(5, 55), [0, 255, 7, 255], 'bottom row, first column');
  assert.deepEqual(px(75, 55), [3, 252, 7, 255], 'bottom row, last column');
  assert.deepEqual(px(5, 25), [100, 155, 7, 255], 'second row');
  assert.equal(px(95, 55)[3], 0, 'beyond the last column');
  assert.equal(px(5, 2)[3], 0, 'above the last row');
});

test('imageExtent and pngSourceRect', () => {
  const im = img(4, 2, 'u8', () => 0);
  const e = imageExtent(im);
  close(e.w[0], 1.95, 'w0'); close(e.w[1], 2.35, 'w1');
  close(e.y[0], -1.25, 'y0'); close(e.y[1], -0.25, 'y1');
  assert.equal(imageExtent(null), null);
  const png = {kind: 'png', png: 'x.png', extent: {w: [2, 4], y: [-1, 1]}};
  assert.deepEqual(imageExtent(png), {w: [2, 4], y: [-1, 1]});
  assert.deepEqual(imageExtent({kind: 'png', png: 'x', extent: null}, {w: 640, h: 80}), {w: [0, 640], y: [0, 80]});
  const r = pngSourceRect({w: [2, 4], y: [-1, 1]}, 1000, 2.5, 3);
  close(r.sx, 250, 'sx'); close(r.sw, 250, 'sw');
  assert.equal(r.w0, 2.5); assert.equal(r.w1, 3);
  const clip = pngSourceRect({w: [2, 4], y: [0, 1]}, 1000, 1, 2.5);
  close(clip.sx, 0, 'clipped sx'); close(clip.sw, 250, 'clipped sw');
  assert.equal(pngSourceRect({w: [2, 4], y: [0, 1]}, 1000, 5, 6), null);
  assert.equal(pngSourceRect({w: [2, 4], y: [0, 1]}, 0, 2, 3), null);
});
