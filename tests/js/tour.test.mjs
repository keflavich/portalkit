// Tests for web/js/math/tour.js (pure): geodesic legs, drift, per-axis play, manual moves.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {Tour, LEG_MS, HOP_MS, DWELL_MS} from '../../web/js/math/tour.js';
import {basisFrame, randomFrame, orthonormalityError} from '../../web/js/vendor/gc/math/frame.js';
import {mulberry32} from '../../web/js/vendor/gc/math/linalg.js';

const ortho = (F, tol = 1e-9) => orthonormalityError(F) < tol;

function maxDiff(a, b) {
  let m = 0;
  for (let i = 0; i < a.length; i++) m = Math.max(m, Math.abs(a[i] - b[i]));
  return m;
}

/** Column `c` of a frame equals ±e_k. */
function columnIs(F, D, c, k, tol = 1e-9) {
  for (let i = 0; i < D; i++) {
    const v = F[c * D + i];
    if (i === k ? Math.abs(Math.abs(v) - 1) > tol : Math.abs(v) > tol) return false;
  }
  return true;
}

test('constructor validates D and starts on the e0/e1 plane', () => {
  assert.throws(() => new Tour(1), RangeError);
  assert.throws(() => new Tour(0), RangeError);
  assert.throws(() => new Tour(2.5), RangeError);
  assert.throws(() => new Tour(NaN), RangeError);
  const t = new Tour(4);
  assert.deepEqual(Array.from(t.frame), Array.from(basisFrame(4, 0, 1)));
  assert.equal(t.mode, 'idle');
  assert.ok(!t.animating && !t.playing);
  // a non-orthonormal initial frame is repaired
  const u = new Tour(3, {F: Float64Array.from([2, 0, 0, 1, 1, 0])});
  assert.ok(ortho(u.frame));
});

test('to(F, ms) ends exactly on the target and stays orthonormal on the way', () => {
  const D = 6;
  const target = randomFrame(D, null, mulberry32(3));
  const settled = [];
  const t = new Tour(D, {onSettle: (F, kind) => settled.push(kind)});
  t.to(target, 1000);
  assert.ok(t.animating);
  for (let k = 0; k < 9; k++) {
    t.step(100);
    assert.ok(ortho(t.frame, 1e-8), `step ${k}`);
  }
  assert.ok(t.animating);
  t.step(100);
  assert.ok(!t.animating);
  assert.equal(t.mode, 'idle');
  assert.ok(maxDiff(t.frame, target) < 1e-9);
  assert.deepEqual(settled, ['to']);
});

test('to(F, 0) jumps and settles at once; bad frames throw', () => {
  const t = new Tour(3);
  const T = basisFrame(3, 2, 0);
  let n = 0;
  t.onSettle = () => { n++; };
  t.to(T, 0);
  assert.deepEqual(Array.from(t.frame), Array.from(T));
  assert.ok(t.justSettled);
  assert.equal(n, 1);
  assert.throws(() => t.to(new Float64Array(5), 100), RangeError);
  assert.throws(() => t.set(null), RangeError);
});

test('step ignores zero, negative and non-finite dt; a large dt finishes the leg', () => {
  const t = new Tour(4);
  const T = basisFrame(4, 2, 3);
  t.to(T, 500);
  const before = Float64Array.from(t.frame);
  for (const dt of [0, -10, NaN, Infinity, undefined]) t.step(dt);
  assert.deepEqual(Array.from(t.frame), Array.from(before));
  t.step(1e9);
  assert.ok(maxDiff(t.frame, T) < 1e-9);
});

test('drift stays orthonormal and inside the span of its group', () => {
  const D = 7;
  const group = [0, 1, 4];
  const t = new Tour(D, {seed: 11});
  t.drift(group);
  assert.ok(t.playing);
  let legs = 0;
  t.onSettle = () => { legs++; };
  for (let k = 0; k < 200; k++) {
    t.step(LEG_MS / 37);
    assert.ok(ortho(t.frame, 1e-8));
    for (const i of [2, 3, 5, 6]) {
      assert.ok(Math.abs(t.frame[i]) < 1e-9 && Math.abs(t.frame[D + i]) < 1e-9, `dim ${i} leaked at step ${k}`);
    }
  }
  assert.ok(legs >= 4, `legs ${legs}`);
  assert.ok(t.playing, 'drift is endless');
  t.stop();
  assert.ok(!t.playing && !t.animating);
});

test('drift with fewer than two valid group dims uses every dim', () => {
  const t = new Tour(4, {seed: 5});
  t.drift([9, -1, 2.5]);
  assert.equal(t._group, null);
  t.drift([3]);
  assert.equal(t._group, null);
  t.drift(Uint8Array.of(1, 1, 3));
  assert.deepEqual(t._group, [1, 3]);
});

test('the same seed gives the same drift', () => {
  const a = new Tour(5, {seed: 42});
  const b = new Tour(5, {seed: 42});
  const c = new Tour(5, {seed: 43});
  for (const t of [a, b, c]) t.drift();
  for (let k = 0; k < 50; k++) for (const t of [a, b, c]) t.step(123);
  assert.deepEqual(Array.from(a.frame), Array.from(b.frame));
  assert.ok(maxDiff(a.frame, c.frame) > 1e-3);
});

test('playAxis hops the chosen column through e_k and keeps the other column', () => {
  const D = 4;
  const t = new Tour(D);
  t.playAxis('x');
  assert.equal(t.mode, 'axis');
  t.step(HOP_MS);
  // started on e0, so the first hop goes to e1, which coincides with the y column: skipped
  assert.ok(columnIs(t.frame, D, 0, 2), `x column ${Array.from(t.frame.subarray(0, D))}`);
  assert.ok(columnIs(t.frame, D, 1, 1), 'y column is kept');
  assert.ok(ortho(t.frame));
  t.step(DWELL_MS + HOP_MS);
  assert.ok(columnIs(t.frame, D, 0, 3));
  t.step(DWELL_MS + HOP_MS);
  assert.ok(columnIs(t.frame, D, 0, 0), 'wraps around');
  // y axis with an explicit dim list
  const u = new Tour(D);
  u.playAxisLoop(1, {dims: [3, 2]});
  u.step(HOP_MS);
  assert.ok(columnIs(u.frame, D, 1, 3) || columnIs(u.frame, D, 1, 2));
  assert.ok(columnIs(u.frame, D, 0, 0), 'x column is kept');
  u.playAxis(null);
  assert.equal(u.mode, 'idle');
});

test('playAxis(dim) (section 6.9 form) animates one hop that puts dim k on the x axis', () => {
  const D = 5;
  const t = new Tour(D);
  const settled = [];
  t.onSettle = (F, kind) => settled.push(kind);
  t.playAxis(3);
  assert.ok(t.animating);
  assert.equal(t.mode, 'to');
  t.step(HOP_MS / 2);
  assert.ok(ortho(t.frame, 1e-8));
  t.step(HOP_MS);
  assert.ok(columnIs(t.frame, D, 0, 3), `x column ${Array.from(t.frame.subarray(0, D))}`);
  assert.ok(columnIs(t.frame, D, 1, 1), 'y column kept');
  assert.equal(t.mode, 'idle');
  assert.deepEqual(settled, ['to']);
  // dims 0 and 1 are dims, not axes
  t.playAxis(0);
  t.step(HOP_MS);
  assert.ok(columnIs(t.frame, D, 0, 0));
  // y axis through opts; out-of-range dims are ignored
  t.playAxis(4, {axis: 'y'});
  t.step(HOP_MS);
  assert.ok(columnIs(t.frame, D, 1, 4) && columnIs(t.frame, D, 0, 0));
  // the dim already on the other axis: the axes swap
  t.playAxis(4);
  t.step(HOP_MS);
  assert.ok(columnIs(t.frame, D, 0, 4) && columnIs(t.frame, D, 1, 0), `swap ${Array.from(t.frame)}`);
  const before = Float64Array.from(t.frame);
  t.playAxis(7);
  t.playAxis(-1);
  assert.ok(!t.animating && maxDiff(t.frame, before) === 0);
});

test('axisTarget is orthonormal and swaps columns for the y axis', () => {
  const t = new Tour(5, {F: randomFrame(5, null, mulberry32(9))});
  const Tx = t.axisTarget(0, 3);
  const Ty = t.axisTarget(1, 3);
  assert.ok(ortho(Tx) && ortho(Ty));
  assert.ok(columnIs(Tx, 5, 0, 3));
  assert.ok(columnIs(Ty, 5, 1, 3));
});

test('nextPair walks the basis-pair frames in both directions', () => {
  const D = 3;
  const t = new Tour(D);
  assert.deepEqual(t.nextPair(), [0, 1]);
  t.step(HOP_MS);
  assert.ok(maxDiff(t.frame, basisFrame(D, 0, 1)) < 1e-9);
  assert.deepEqual(t.nextPair(), [0, 2]);
  t.step(HOP_MS);
  assert.ok(maxDiff(t.frame, basisFrame(D, 0, 2)) < 1e-9);
  assert.deepEqual(t.nextPair(-1), [0, 1]);
  assert.deepEqual(t.nextPair(-1), [1, 2], 'wraps backwards');
  assert.equal(t.mode, 'pair');
});

test('manual moves the dim projection to the target and stops motion', () => {
  const D = 5;
  const t = new Tour(D);
  t.drift();
  t.step(500);
  t.manual(3, 0.3, -0.4);
  assert.equal(t.mode, 'idle');
  assert.ok(ortho(t.frame, 1e-8));
  assert.ok(Math.abs(t.frame[3] - 0.3) < 1e-6 && Math.abs(t.frame[D + 3] + 0.4) < 1e-6);
  const before = Float64Array.from(t.frame);
  t.manual(99, 1, 0);
  t.manual(1, NaN, 0);
  assert.ok(maxDiff(t.frame, before) < 1e-12, 'invalid index / NaN target leave the frame alone');
});

test('setSpeed clamps and scales time', () => {
  const t = new Tour(3);
  assert.equal(t.setSpeed(100), 8);
  assert.equal(t.setSpeed(0), 0.1);
  assert.equal(t.setSpeed('x'), 1);
  t.setSpeed(2);
  const T = basisFrame(3, 1, 2);
  t.to(T, 1000);
  t.step(500);
  assert.ok(maxDiff(t.frame, T) < 1e-9, 'speed 2 finishes a 1000 ms leg in 500 ms');
});

test('a degenerate (antipodal) target still animates to a valid frame', () => {
  const D = 3;
  const t = new Tour(D);
  const T = Float64Array.from([-1, 0, 0, 0, -1, 0]);
  t.to(T, 300);
  for (let k = 0; k < 3; k++) {
    t.step(100);
    assert.ok(t.frame.every(Number.isFinite));
  }
  assert.ok(maxDiff(t.frame, T) < 1e-9);
});
