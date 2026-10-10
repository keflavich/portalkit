// Tests for web/js/plot/axes.js (pure): scales, ticks, views, camera equivalence.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {
  scale, decadeTicks, linearTicks, fmtTick, decadeLabel, niceStep, makeView, cameraFromView,
  zoomDomain, panDomain, extent, MAX_TICKS,
} from '../../web/js/plot/axes.js';

const close = (a, b, tol = 1e-9) => Math.abs(a - b) <= tol * Math.max(1, Math.abs(a), Math.abs(b));

test('linear scale maps ends and round-trips', () => {
  const s = scale('lin', -2, 8, 50, 450);
  assert.equal(s.f(-2), 50);
  assert.equal(s.f(8), 450);
  assert.equal(s.f(3), 250);
  for (const v of [-2, 0, 1.2345, 7.9, 1e3, -1e3]) assert.ok(close(s.inv(s.f(v)), v));
  assert.ok(Number.isNaN(s.f(NaN)));
  assert.ok(Number.isNaN(s.f(null)));
  assert.ok(Number.isNaN(s.f(undefined)));
});

test('invert swaps the pixel ends (magnitude axes)', () => {
  const s = scale('lin', 10, 20, 0, 100, true);
  assert.equal(s.f(10), 100);
  assert.equal(s.f(20), 0);
  assert.ok(close(s.inv(25), 17.5));
});

test('degenerate and non-finite linear domains are widened to finite scales', () => {
  const s0 = scale('lin', 5, 5, 0, 100);
  assert.ok(s0.d1 > s0.d0);
  assert.ok(Number.isFinite(s0.f(5)));
  assert.ok(close(s0.f(5), 50));
  const z = scale('lin', 0, 0, 0, 100);
  assert.deepEqual([z.d0, z.d1], [-0.5, 0.5]);
  const n = scale('lin', NaN, Infinity, 0, 10);
  assert.deepEqual([n.d0, n.d1], [0, 1]);
  const h = scale('lin', NaN, 4, 0, 10);
  assert.deepEqual([h.d0, h.d1], [3, 4]);
  const bad = scale('lin', 0, 1, NaN, NaN);
  assert.ok(Number.isFinite(bad.f(0.5)));
});

test('log scale: decades are evenly spaced, v <= 0 is NaN, bounds are repaired', () => {
  const s = scale('log', 1, 1000, 0, 300);
  assert.ok(close(s.f(10), 100));
  assert.ok(close(s.f(100), 200));
  assert.ok(close(s.inv(150), Math.pow(10, 1.5)));
  assert.ok(Number.isNaN(s.f(0)));
  assert.ok(Number.isNaN(s.f(-5)));
  const r = scale('log', -1, 100, 0, 1);
  assert.ok(r.d0 > 0 && r.d0 < 100);
  const d = scale('log', 7, 7, 0, 1);
  assert.ok(close(d.d0, 0.7) && close(d.d1, 70));
  const huge = scale('log', 1e-200, 1e200, 0, 400);
  assert.ok(close(huge.f(1), 200));
});

test('linear ticks: inside the domain, 1-2-5 steps, typographic minus, no −0', () => {
  const t = linearTicks(-1.3, 2.7, 6);
  assert.ok(t.length >= 3 && t.length <= 12);
  for (const {v} of t) assert.ok(v >= -1.3 - 1e-12 && v <= 2.7 + 1e-12);
  const neg = t.find((x) => x.v < 0);
  assert.ok(neg.label.startsWith('−'), `label ${neg.label}`);
  const zero = t.find((x) => x.v === 0);
  assert.equal(zero.label, '0.0'.slice(0, zero.label.length));
  assert.ok(!t.some((x) => x.label.includes('-')));
  assert.deepEqual(linearTicks(NaN, 1), []);
  assert.equal(linearTicks(3, 3).length, 1);
  assert.ok(linearTicks(0, 1e9, 1e6).length <= MAX_TICKS);
  // either order
  assert.deepEqual(linearTicks(5, 0).map((x) => x.v), linearTicks(0, 5).map((x) => x.v));
});

test('fmtTick: exponent form for large and tiny values, unicode minus', () => {
  assert.equal(fmtTick(-0.0001, 1), '0.0');
  assert.equal(fmtTick(-2.5, 1), '−2.5');
  assert.equal(fmtTick(3e8, 0), '3e8');
  assert.equal(fmtTick(-3e-9, 0), '−3e−9');
  assert.equal(fmtTick(NaN, 2), '');
  assert.equal(fmtTick(Infinity, 2), '');
  // with the step, exponential labels keep enough mantissa digits
  assert.equal(fmtTick(1.2e7, 0, 2e6), '1.2e7');
  assert.equal(fmtTick(1.05e9, 0, 5e7), '1.05e9');
  assert.equal(fmtTick(1e7, 0, 2e6), '1e7');
});

test('linear tick labels stay distinct for large and tiny ranges', () => {
  for (const [a, b] of [[1e7, 2e7], [1e7, 1.1e7], [1e9, 1.1e9], [-2e8, -1e8], [1e-7, 2e-7], [3.1e12, 3.1000001e12]]) {
    const ticks = scale('lin', a, b, 0, 500).ticks(6);
    const labels = ticks.map((t) => t.label);
    assert.ok(ticks.length >= 2, `${a}..${b}`);
    assert.equal(new Set(labels).size, labels.length, `${a}..${b}: ${labels.join(' ')}`);
  }
  assert.equal(niceStep(0.37), 0.5);
  assert.equal(niceStep(-1), 1);
});

test('decadeTicks: labelled majors, unlabelled minors, invalid ranges', () => {
  const t = decadeTicks(1, 1000);
  const majors = t.filter((x) => x.major);
  assert.deepEqual(majors.map((x) => x.v), [1, 10, 100, 1000]);
  assert.deepEqual(majors.map((x) => x.label), ['1', '10', '100', '1000']);
  const minors = t.filter((x) => !x.major);
  assert.equal(minors.length, 3 * 8);
  assert.ok(minors.every((x) => x.label === ''));
  assert.deepEqual(decadeTicks(-5, -1), []);
  assert.deepEqual(decadeTicks(NaN, 10), []);
  // lo <= 0 is repaired to hi / 1e6
  assert.ok(decadeTicks(0, 100).some((x) => x.v === 1e-4));
  assert.equal(decadeLabel(-5), '10⁻⁵');
  assert.equal(decadeLabel(-2), '0.01');
  assert.equal(decadeLabel(7), '10⁷');
  // either order gives the same ticks
  assert.deepEqual(decadeTicks(1000, 1), t);
});

test('decadeTicks thins labels over many decades and spans huge ranges', () => {
  const t = decadeTicks(1e-10, 1e20);
  const labelled = t.filter((x) => x.label);
  assert.ok(labelled.length <= 13 && labelled.length >= 8, `${labelled.length}`);
  assert.ok(t.length <= MAX_TICKS);
  const huge = decadeTicks(1e-150, 1e150);
  assert.ok(huge.length > 5 && huge.length <= MAX_TICKS);
  assert.ok(huge.every((x) => x.major));
  assert.ok(huge[huge.length - 1].v >= 1e120, 'ticks reach the top of the range (within one label step)');
});

test('log scale ticks: short ranges get 1-2-5 labels, long ranges decades', () => {
  const short = scale('log', 2, 9, 0, 100).ticks(5);
  assert.ok(short.length >= 2 && short.every((x) => x.label && x.v > 0));
  const long = scale('log', 0.01, 1e4, 0, 100).ticks(5);
  assert.ok(long.some((x) => x.label === '0.01') && long.some((x) => x.label === '10⁴' || x.label === '10000'));
});

test('makeView: plot rectangle, y up, toPx/toData inverse, inPlot', () => {
  const v = makeView({width: 400, height: 300, x: {kind: 'lin', d0: 0, d1: 10}, y: {kind: 'lin', d0: -1, d1: 1}});
  assert.deepEqual([v.plot.left, v.plot.top, v.plot.right, v.plot.bottom], [52, 10, 388, 262]);
  assert.deepEqual(v.toPx(0, -1), [52, 262]);
  assert.deepEqual(v.toPx(10, 1), [388, 10]);
  const [x, y] = v.toData(...v.toPx(3.3, 0.25));
  assert.ok(close(x, 3.3) && close(y, 0.25));
  assert.ok(v.inPlot(100, 100));
  assert.ok(!v.inPlot(10, 100));
  const tiny = makeView({width: 0, height: NaN});
  assert.ok(tiny.plot.width >= 1 && tiny.plot.height >= 1);
});

test('cameraFromView reproduces view.toPx through the vendor camera formula', () => {
  for (const spec of [
    {x: {kind: 'lin', d0: -3, d1: 5}, y: {kind: 'lin', d0: 22, d1: 12}},
    {x: {kind: 'log', d0: 0.1, d1: 1e3}, y: {kind: 'lin', d0: 0, d1: 1, invert: true}},
    {x: {kind: 'lin', d0: 1e6, d1: 1e6 + 1}, y: {kind: 'log', d0: 1e-3, d1: 10}},
  ]) {
    const v = makeView({width: 640, height: 480, ...spec});
    // camera in true coordinates; XYRenderer subtracts a setXY origin from both buffer and
    // camera, which is what the shifted check below reproduces
    const origin = [spec.x.kind === 'lin' ? spec.x.d0 : 0, 0];
    const cam = cameraFromView(v);
    assert.equal(cameraFromView.length, 1, 'no origin parameter');
    for (const [dx, dy] of [[0.3, 0.7], [0.01, 0.99], [0.5, 0.5]]) {
      const X = v.sx.inv(v.plot.left + dx * v.plot.width);
      const Y = v.sy.inv(v.plot.top + dy * v.plot.height);
      const tx = v.sx.tf(X) - origin[0], ty = v.sy.tf(Y) - origin[1];
      const cx = cam.cx - origin[0], cy = cam.cy - origin[1];
      const sx = cam.width / 2 + (tx - cx) * cam.scale;
      const sy = cam.height / 2 - (ty - cy) * cam.scale * cam.aspect;
      const [px, py] = v.toPx(X, Y);
      assert.ok(Math.abs(sx - px) < 1e-6 && Math.abs(sy - py) < 1e-6, `${sx},${sy} vs ${px},${py}`);
    }
  }
});

test('zoomDomain keeps the anchor fixed; panDomain shifts by pixels', () => {
  const s = scale('lin', 0, 10, 0, 100);
  const [a, b] = zoomDomain(s, 30, 2);
  assert.ok(close(a, 1.5) && close(b, 6.5));
  const z = scale('lin', a, b, 0, 100);
  assert.ok(close(z.inv(30), s.inv(30)));
  assert.deepEqual(zoomDomain(s, 30, 0), [0, 10]);
  assert.deepEqual(zoomDomain(s, 30, NaN), [0, 10]);
  const [p0, p1] = panDomain(s, 10);
  assert.ok(close(p0, -1) && close(p1, 9));
  const L = scale('log', 1, 100, 0, 100);
  const [l0, l1] = zoomDomain(L, 50, 2);
  assert.ok(close(l0, Math.pow(10, 0.5)) && close(l1, Math.pow(10, 1.5)));
});

test('extent: empty, single value, NaN, log and percentiles', () => {
  assert.equal(extent([]), null);
  assert.equal(extent(null), null);
  assert.equal(extent([NaN, Infinity]), null);
  const one = extent([5]);
  assert.ok(one[0] < 5 && one[1] > 5);
  const e = extent(Float32Array.from([0, NaN, 10]), {pad: 0});
  assert.deepEqual(e, [0, 10]);
  const lg = extent([-1, 0, 1, 100], {kind: 'log', pad: 0});
  assert.ok(close(lg[0], 1) && close(lg[1], 100));
  const vals = Array.from({length: 101}, (_, i) => i);
  vals.push(1e9);
  const q = extent(vals, {q: [0, 0.98], pad: 0});
  assert.ok(q[1] < 1e9 && q[1] >= 99);
  const m = extent([1, 2, 300], {mask: [1, 1, 0], pad: 0});
  assert.deepEqual(m, [1, 2]);
});

// ---------------------------------------------------------------- plot/overlays.js and plot/canvas2d.js
// (WP07 owns no separate overlay test file; these exercise the view transform consumers.)

/** 2D-context stub that records path vertices (moveTo / lineTo) between strokes. */
function recCtx() {
  const calls = [];
  const target = {
    calls,
    globalAlpha: 1,
    moveTo(x, y) { calls.push(['M', x, y]); },
    lineTo(x, y) { calls.push(['L', x, y]); },
    measureText: (t) => ({width: String(t).length * 6}),
  };
  return new Proxy(target, {
    get(t, k) { return k in t ? t[k] : () => {}; },
    set(t, k, v) { t[k] = v; return true; },
  });
}

const OV = await import('../../web/js/plot/overlays.js');
const C2 = await import('../../web/js/plot/canvas2d.js');
const ISO = await import('../../web/js/data/isochrones.js');
const GOLD = JSON.parse((await import('node:fs')).readFileSync(new URL('../golden/physics/track_xy.json', import.meta.url), 'utf8'));

function vertices(ctx) {
  return ctx.calls.filter((c) => c[0] === 'M' || c[0] === 'L').map((c) => [c[1], c[2]]);
}

test('overlays: isochrone tracks match data/isochrones.js trackXY (golden track_xy.json)', () => {
  const view = makeView({width: 400, height: 300, x: {kind: 'lin', d0: -1, d1: 4}, y: {kind: 'lin', d0: 26, d1: 8, invert: false}});
  for (const cs of GOLD.cases) {
    const iso = {schema: 'pk-isochrones/1', tracks: [GOLD.track]};
    const {layers, missing} = OV.resolveOverlays(['golden_1_myr'], {isochrones: iso});
    assert.deepEqual(missing, []);
    const ctx = recCtx();
    const state = {av: cs.av, rv: cs.rv, dm: cs.dm, filters: GOLD.filters, colmap: cs.colmap || null, x: cs.x_expr, y: cs.y_expr};
    const res = OV.drawOverlays(ctx, view, layers, state);
    const want = ISO.trackXY(GOLD.track, cs.x_expr, cs.y_expr, cs.av, cs.rv, cs.dm, GOLD.filters, cs.colmap || null);
    const pts = [];
    for (let i = 0; i < want.x.length; i++) {
      // golden and trackXY agree
      if (cs.x[i] === null) { assert.ok(Number.isNaN(want.x[i])); continue; }
      assert.ok(Math.abs(want.x[i] - cs.x[i]) < 1e-4 && Math.abs(want.y[i] - cs.y[i]) < 1e-4);
      pts.push(view.toPx(cs.x[i], cs.y[i]));
    }
    const got = vertices(ctx);
    if (!pts.length) {
      assert.equal(res[0].drawn, 0, `${cs.x_expr}: unresolved keys draw nothing`);
      continue;
    }
    assert.ok(res[0].drawn > 0, `${cs.x_expr} / ${cs.y_expr}: drawn ${JSON.stringify(res)}`);
    assert.equal(got.length, pts.length);
    got.forEach((q, i) => assert.ok(Math.abs(q[0] - pts[i][0]) < 1e-3 && Math.abs(q[1] - pts[i][1]) < 1e-3, `${cs.x_expr} pt ${i}: ${q} vs ${pts[i]}`));
  }
});

test('overlays: tracks with column-style keys get the distance modulus and case-insensitive keys', () => {
  const view = makeView({width: 400, height: 300, x: {kind: 'lin', d0: -1, d1: 2}, y: {kind: 'lin', d0: 22, d1: 0}});
  const track = {label: 'b', dist_pc: 8000, mass: [1, 2, 3], mags: {F182M: [5, 4, 3], F410M: [4.5, 3.5, 2.5]}};
  const {layers} = OV.resolveOverlays(['b'], {isochrones: {tracks: [track]}});
  const ctx = recCtx();
  OV.drawOverlays(ctx, view, layers, {x: 'f182m-f410m', y: 'f410m'});
  const mu = 5 * Math.log10(800);
  const got = vertices(ctx);
  assert.equal(got.length, 3);
  [4.5, 3.5, 2.5].forEach((m, i) => {
    const [px, py] = view.toPx(0.5, m + mu);
    assert.ok(Math.abs(got[i][0] - px) < 1e-6 && Math.abs(got[i][1] - py) < 1e-6);
  });
});

test('overlays: the <law>_vector arrow resolves column keys to filter keys', () => {
  const view = makeView({width: 400, height: 300, x: {kind: 'lin', d0: -1, d1: 4}, y: {kind: 'lin', d0: 22, d1: 10}});
  const {layers} = OV.resolveOverlays(['g23_vector'], {extinction: 'G23', vectorAv: 10});
  const ctx = recCtx();
  const res = OV.drawOverlays(ctx, view, layers, {rv: 3.1, filters: GOLD.filters, x: 'f182m - f410m', y: 'f410m'});
  assert.ok(res[0].drawn === 1 && res[0].opacity > 0.9, JSON.stringify(res));
  const [a, b] = vertices(ctx);
  const [x0, y0] = view.toData(...a), [x1, y1] = view.toData(...b);
  assert.ok(Math.abs((x1 - x0) - 10 * (0.13931 - 0.0359533)) < 1e-6, `dx ${x1 - x0}`);
  assert.ok(Math.abs((y1 - y0) - 10 * 0.0359533) < 1e-6, `dy ${y1 - y0}`);
  // alavFromFilters resolves column, short and full keys alike, and uses a colmap
  const al = OV.alavFromFilters(GOLD.filters, 'G23', 3.1);
  for (const k of ['f410m', 'F410M', 'JWST/NIRCam.F410M']) assert.equal(al(k), 0.0359533);
  assert.equal(OV.alavFromFilters(GOLD.filters, 'G23', 3.1, {a: 'JWST/NIRCam.F182M'})('a'), 0.13931);
  assert.ok(Number.isNaN(al('nope')));
});

test('overlays: the frame:<pane> store value (string dims) projects with dimMeta', () => {
  const view = makeView({width: 300, height: 300, x: {kind: 'lin', d0: -5, d1: 5}, y: {kind: 'lin', d0: -5, d1: 5}});
  const frame = {F: [1, 0, 0, 0, 1, 0], dims: ['a', 'b', 'c']};
  const p = OV.makeProjector(view, {frame});
  assert.deepEqual(p.point({a: 1, b: 2, c: 3}), [1, 2]);
  assert.equal(p.coverage(new Set(['a', 'b', 'c'])), 1);
  const q = OV.makeProjector(view, {frame, dimMeta: {a: {center: 1, scale: 2}, b: {center: 0, scale: 4}}});
  assert.deepEqual(q.point({a: 3, b: 2}), [1, 0.5]);
  // a virtual dim key (expression text, no expr field) evaluates from its bases
  const v = OV.makeProjector(view, {frame: {F: [1, 0, 0, 1], dims: ['f182m-f410m', 'f410m']}});
  assert.deepEqual(v.point({f182m: 15, f410m: 13}), [2, 13]);
  assert.equal(v.coverage(new Set(['f182m', 'f410m'])), 1);
});

test('canvas2d: log-axis error bars that cross zero run toward smaller values', () => {
  const v = makeView({width: 400, height: 300, x: {kind: 'lin', d0: 0, d1: 10}, y: {kind: 'log', d0: 0.1, d1: 100}});
  const ctx = recCtx();
  C2.drawPoints(ctx, v, [{x: 5, y: 1, ey: [2, 1]}]);
  const segs = ctx.calls.filter((c) => c[0] === 'M' || c[0] === 'L');
  const [, px, py] = segs[0];
  const [, , ey1] = segs[1];
  const [, ptY] = v.toPx(5, 1);
  // first segment: lower end (edge, below the plot) to the upper end (y + 1 = 2, above the point)
  assert.ok(py > v.plot.bottom, `lower end ${py} should be below the plot bottom ${v.plot.bottom}`);
  assert.ok(ey1 < ptY, 'upper end is above the point');
  assert.equal(px, v.toPx(5, 1)[0]);
  // inverted log x: smaller values are on the right
  const s = scale('log', 0.1, 100, 52, 388, true);
  const [a, b, aEdge] = C2.errSpan(s, 1, 5, 1, 42, 398);
  assert.ok(aEdge && a === 398, `lower end ${a}`);
  assert.ok(b < s.f(1));
  const n = scale('log', 0.1, 100, 52, 388);
  assert.equal(C2.errSpan(n, 1, 5, 1, 42, 398)[0], 42);
});
