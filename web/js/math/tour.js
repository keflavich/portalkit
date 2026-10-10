// Tour engine for manifold frames (ARCHITECTURE.md section 6.9).
// Pure module: no DOM, no timers. The caller drives it with step(dtMs) from its own
// requestAnimationFrame loop, which keeps it testable under node and lets the pane stop
// stepping while hidden. Wraps the vendored frame math (Buja et al. 2005 geodesics,
// Cook & Buja 1997 manual rotation).
//
//   const t = new Tour(D, {F, seed, legMs, hopMs, dwellMs, onSettle});
//   t.to(F, ms)          geodesic animation to a frame (presets, deep links)
//   t.drift(group)       endless random-plane legs restricted to the dims in `group`
//   t.playAxis(k)        section 6.9 form: one animated hop that puts dim k on the x axis
//                        (opts.axis 'y' for the y axis), keeping the other column
//   t.playAxis('x')      per-axis play loop (= playAxisLoop('x')): the x (or y) column steps
//                        through the dims, one hop at a time, as the legacy manifold axis
//                        "play" buttons did
//   t.manual(j, a, b)    compass drag: move dim j's projection toward (a, b)
//   t.nextPair(±1)       tour-set hop through all basis-pair frames
//   F = t.step(dtMs)     advance; returns the live frame (reused buffer, copy to keep it)
//
// Frames use the vendor layout: Float64Array(2D), [px_0..px_{D-1}, py_0..py_{D-1}].

import {
  basisFrame, copyFrame, geodesic, blendFrames, orthonormalize, randomFrame, manualRotate,
  isOrthonormal,
} from '../vendor/gc/math/frame.js';
import {mulberry32} from '../vendor/gc/math/linalg.js';

export const LEG_MS = 3800;      // drift leg at speed 1 (legacy grand-tour leg)
export const HOP_MS = 1200;      // preset / tour-set / axis hop
export const DWELL_MS = 200;     // pause between axis-play hops (legacy interval 1400 = hop + dwell)
export const TO_MS = 1100;       // default to() duration (vendor MOTION.viewMs)

function smootherstep(t) {
  return t * t * t * (t * (t * 6 - 15) + 10);
}

function axisIndex(axis) {
  if (axis === 'x' || axis === 0) return 0;
  if (axis === 'y' || axis === 1) return 1;
  return -1;
}

export class Tour {
  /**
   * D: number of displayed dims (>= 2).
   * opts: {F?: initial frame (default e0/e1), seed?: number, rng?: () => [0,1),
   *        legMs, hopMs, dwellMs, speed = 1, ease = true, onSettle?(F, kind)}
   */
  constructor(D, opts = {}) {
    if (!Number.isInteger(D) || D < 2) throw new RangeError(`Tour needs D >= 2 (got ${D})`);
    this.D = D;
    this.legMs = opts.legMs ?? LEG_MS;
    this.hopMs = opts.hopMs ?? HOP_MS;
    this.dwellMs = opts.dwellMs ?? DWELL_MS;
    this.speed = 1;
    this.setSpeed(opts.speed ?? 1);
    this.ease = opts.ease !== false;
    this.rng = opts.rng || mulberry32(opts.seed ?? 0x9e3779b9);
    this.onSettle = opts.onSettle || null;
    this.F = new Float64Array(2 * D);
    if (opts.F && opts.F.length === 2 * D) {
      this.F.set(opts.F);
      if (!isOrthonormal(this.F, 1e-6)) orthonormalize(this.F);
    } else {
      this.F.set(basisFrame(D, 0, 1));
    }
    this.mode = 'idle';        // idle | to | drift | axis | pair
    this._leg = null;          // {geo, t, dur, ease}
    this._dwell = 0;
    this._group = null;
    this._axis = -1;
    this._axisDims = null;
    this._axisPos = -1;
    this._pairs = null;
    this._pairIdx = -1;
    this.justSettled = false;
  }

  /** Live frame (reused buffer). */
  get frame() { return this.F; }

  /** True while a leg or hop is in progress. */
  get animating() { return !!this._leg; }

  /** True while an endless motion (drift or axis play) is active. */
  get playing() { return this.mode === 'drift' || this.mode === 'axis'; }

  setSpeed(s) {
    const v = Number(s);
    this.speed = Number.isFinite(v) ? Math.min(8, Math.max(0.1, v)) : 1;
    return this.speed;
  }

  /** Jump to F immediately (orthonormalized copy); stops any motion. */
  set(F) {
    this.stop();
    if (!F || F.length !== 2 * this.D) throw new RangeError('frame length must be 2·D');
    this.F.set(F);
    if (!isOrthonormal(this.F, 1e-9)) orthonormalize(this.F);
    return this.F;
  }

  /** Stop all motion; the frame stays where it is. */
  stop() {
    this.mode = 'idle';
    this._leg = null;
    this._dwell = 0;
    this._group = null;
    this._axis = -1;
    return this.F;
  }

  _startLeg(target, ms, {noSpin = false} = {}) {
    let T = target;
    if (!isOrthonormal(T, 1e-9)) T = orthonormalize(copyFrame(T));
    let geo = geodesic(this.F, T, {noSpin});
    if (!Number.isFinite(geo.dist)) {
      // numerically degenerate input: fall back to blend + polar re-orthonormalization
      const F0 = copyFrame(this.F), T1 = copyFrame(T);
      geo = {
        at: (t, out) => {
          if (t >= 1) { const o = out || new Float64Array(T1.length); o.set(T1); return o; }
          return blendFrames(F0, T1, t, out);
        },
        target: T1,
      };
    }
    this._leg = {geo, t: 0, dur: Math.max(1, ms), ease: this.ease};
  }

  /** Animate to frame F over `ms` (<= 0: jump). Stops drift / axis play. */
  to(F, ms = TO_MS, opts = {}) {
    if (!F || F.length !== 2 * this.D) throw new RangeError('frame length must be 2·D');
    this.stop();
    if (!(ms > 0)) {
      this.set(F);
      this._settle('to');
      return this.F;
    }
    this.mode = 'to';
    this._startLeg(F, ms, opts);
    return this.F;
  }

  /**
   * Endless drift through random planes spanned by the dims in `group` (indices). Fewer
   * than 2 valid indices use all dims. Legs use noSpin geodesics (grand-tour style).
   */
  drift(group) {
    this.stop();
    let idx = Array.isArray(group) || ArrayBuffer.isView(group)
      ? [...new Set(Array.from(group).filter((i) => Number.isInteger(i) && i >= 0 && i < this.D))]
      : [];
    if (idx.length < 2) idx = null;
    this._group = idx;
    this.mode = 'drift';
    this._nextDrift();
    return this.F;
  }

  _nextDrift() {
    const T = randomFrame(this.D, this._group, this.rng);
    this._startLeg(T, this.legMs, {noSpin: true});
  }

  /**
   * playAxis(dim) (section 6.9): an integer `dim` animates frame column opts.axis ('x' default,
   * or 'y') to e_dim over hopMs, keeping the other column (orthogonalized); settles with kind
   * 'to'. The strings 'x' / 'y' start the per-axis play loop (playAxisLoop). null, 'stop' or
   * anything else stops a running loop.
   */
  playAxis(dim, opts = {}) {
    if (Number.isInteger(dim)) {
      if (dim < 0 || dim >= this.D) return this.F;
      const a = axisIndex(opts.axis ?? 'x');
      if (a < 0) return this.F;
      const T = this.axisTarget(a, dim);
      this.stop();
      this.mode = 'to';
      this._startLeg(T, Number(opts.ms) > 0 ? Number(opts.ms) : this.hopMs, {noSpin: false});
      return this.F;
    }
    if (dim === 'x' || dim === 'y') return this.playAxisLoop(dim, opts);
    if (this.mode === 'axis') this.stop();
    return this.F;
  }

  /**
   * Per-axis play loop: step frame column `axis` ('x'|'y'|0|1) through `opts.dims` (default
   * all dims) one hop at a time, keeping the other column (orthogonalized). null / 'stop'
   * stops.
   */
  playAxisLoop(axis, opts = {}) {
    const a = axisIndex(axis);
    if (a < 0) {
      if (this.mode === 'axis') this.stop();
      return this.F;
    }
    this.stop();
    const dims = Array.isArray(opts.dims) && opts.dims.length
      ? opts.dims.filter((i) => Number.isInteger(i) && i >= 0 && i < this.D)
      : [...Array(this.D).keys()];
    if (!dims.length) return this.F;
    this._axis = a;
    this._axisDims = dims;
    // continue from the dim currently dominating this column
    let best = -1, bv = -1;
    for (let k = 0; k < dims.length; k++) {
      const v = Math.abs(this.F[a * this.D + dims[k]]);
      if (v > bv) { bv = v; best = k; }
    }
    this._axisPos = best;
    this.mode = 'axis';
    this._nextAxisHop();
    return this.F;
  }

  /** Target frame with column `a` = e_k and the other column kept (Gram-Schmidt). */
  axisTarget(a, k) {
    const D = this.D;
    const T = new Float64Array(2 * D);
    // keep the other column, unless it already shows dim k: then it takes this column (swap)
    const other = Math.abs(this.F[(1 - a) * D + k]) > 0.999 ? a : 1 - a;
    T[k] = 1;
    for (let i = 0; i < D; i++) T[D + i] = this.F[other * D + i];
    orthonormalize(T);           // column 0 = e_k exactly, column 1 = other ⟂ e_k
    if (a === 0) return T;
    const S = new Float64Array(2 * D);
    S.set(T.subarray(D, 2 * D), 0);
    S.set(T.subarray(0, D), D);
    return S;
  }

  _nextAxisHop() {
    const dims = this._axisDims;
    const D = this.D;
    const otherCol = this.F.subarray((1 - this._axis) * D, (2 - this._axis) * D);
    // skip a dim that would coincide with the other column
    for (let tries = 0; tries < dims.length; tries++) {
      this._axisPos = (this._axisPos + 1) % dims.length;
      const k = dims[this._axisPos];
      if (Math.abs(otherCol[k]) < 0.999 || dims.length === 1) break;
    }
    const T = this.axisTarget(this._axis, dims[this._axisPos]);
    this._startLeg(T, this.hopMs, {noSpin: false});
  }

  /** Tour-set hop through all C(D,2) basis-pair frames (delta = ±1). */
  nextPair(delta = 1) {
    if (!this._pairs) {
      this._pairs = [];
      for (let i = 0; i < this.D; i++) for (let j = i + 1; j < this.D; j++) this._pairs.push([i, j]);
    }
    this.stop();
    const n = this._pairs.length;
    this._pairIdx = (((this._pairIdx + delta) % n) + n) % n;
    const [i, j] = this._pairs[this._pairIdx];
    this.mode = 'pair';
    this._startLeg(basisFrame(this.D, i, j), this.hopMs, {noSpin: false});
    return this._pairs[this._pairIdx];
  }

  /** Manual rotation (compass drag) of dim j toward (a, b); stops motion. */
  manual(j, a, b, opts = {}) {
    if (!Number.isInteger(j) || j < 0 || j >= this.D) return this.F;
    this.stop();
    const F = manualRotate(this.F, j, [a, b], opts);
    this.F.set(F);
    return this.F;
  }

  _settle(kind) {
    this.justSettled = true;
    if (this.onSettle) this.onSettle(this.F, kind);
  }

  /** Advance by dtMs (scaled by speed). Returns the live frame. */
  step(dtMs) {
    this.justSettled = false;
    let dt = Number(dtMs);
    if (!(dt > 0) || !Number.isFinite(dt)) return this.F;
    dt *= this.speed;
    // loop so a large dt can finish a leg and continue into dwell / the next leg
    for (let guard = 0; guard < 8 && dt > 0; guard++) {
      if (this._dwell > 0) {
        const use = Math.min(this._dwell, dt);
        this._dwell -= use;
        dt -= use;
        if (this._dwell <= 0 && this.mode === 'axis') this._nextAxisHop();
        continue;
      }
      const leg = this._leg;
      if (!leg) break;
      const remain = (1 - leg.t) * leg.dur;
      // within a microsecond of the end counts as the end (dt sums accumulate rounding)
      const finish = remain <= dt + 1e-3;
      const use = finish ? remain : dt;
      leg.t = finish ? 1 : Math.min(1, leg.t + use / leg.dur);
      dt = finish ? Math.max(0, dt - remain) : 0;
      if (leg.t >= 1) {
        this.F.set(leg.geo.at(1));
        this._leg = null;
        const kind = this.mode;
        this._settle(kind);
        if (this.mode === 'drift') this._nextDrift();
        else if (this.mode === 'axis') { this._dwell = this.dwellMs; if (!(this._dwell > 0)) this._nextAxisHop(); }
        else this.mode = 'idle';
        continue;
      }
      const e = leg.ease && this.mode !== 'drift' ? smootherstep(leg.t) : leg.t;
      leg.geo.at(e, this.F);
    }
    return this.F;
  }
}
