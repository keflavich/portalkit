// Axis scales, ticks and the plot "view" transform (ARCHITECTURE.md section 6.9).
// Pure module: no DOM access, loadable under `node --test`.
//
// The view built by makeView() is THE transform for a plot surface. Canvas drawing
// (plot/canvas2d.js), overlays (plot/overlays.js), brush hit-testing (plot/brushui.js) and
// the WebGL camera (cameraFromView -> gl/xyrenderer.js) all derive from it, so what is drawn
// and what a pointer hits can never disagree (the legacy Brick letterboxing bug).
//
//   scale(kind, d0, d1, r0, r1, invert?) -> {f(v), inv(px), ticks(n)} (+ kind, d0, d1, r0, r1,
//        tf(v), itf(t), a, b: px = a + b * tf(v))
//   decadeTicks(d0, d1) -> [{v, label, major}]
//   makeView({width, height, margin, x: AxisSpec, y: AxisSpec, dpr}) -> View
//   cameraFromView(view) -> {cx, cy, scale, aspect, width, height} (vendor renderer camera in
//        transformed coordinates: log10 for log axes)
//
// AxisSpec = {kind: 'lin'|'log', d0, d1, invert?, expr?, label?}. For y, d0 maps to the
// bottom of the plot rectangle unless invert is set (magnitude axes).

const MINUS = '−';
const SUPER = {'-': '⁻', 0: '⁰', 1: '¹', 2: '²', 3: '³', 4: '⁴',
  5: '⁵', 6: '⁶', 7: '⁷', 8: '⁸', 9: '⁹'};
export const LOG_FLOOR = 1e-300;
export const MAX_TICKS = 200;

function finite(v) {
  return typeof v === 'number' && Number.isFinite(v);
}

/** 1-2-5 step nearest (in log space) to a raw spacing; 1 for invalid input. */
export function niceStep(raw) {
  if (!(raw > 0) || !Number.isFinite(raw)) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  const m = raw / p;
  return (m < Math.SQRT2 ? 1 : m < Math.sqrt(10) ? 2 : m < Math.sqrt(50) ? 5 : 10) * p;
}

/** Decimals needed to label multiples of `step` (0..12). */
export function stepDecimals(step) {
  if (!(step > 0) || !Number.isFinite(step)) return 0;
  return Math.max(0, Math.min(12, Math.ceil(-Math.log10(step) - 1e-9)));
}

/** Mantissa digits that tell multiples of `step` apart near |v| in exponential notation. */
export function expDigits(v, step) {
  const a = Math.abs(v);
  if (!(a > 0) || !(step > 0) || !Number.isFinite(a) || !Number.isFinite(step)) return 0;
  const ps = Math.floor(Math.log10(step) + 1e-9);
  const mant = step / Math.pow(10, ps);
  const d = Math.floor(Math.log10(a) + 1e-9) - ps + (Math.abs(mant - 1) > 1e-6 ? 1 : 0);
  return Math.max(0, Math.min(15, d));
}

/**
 * Tick label with a typographic minus; '−0' never appears. Values with |v| >= 1e7 or
 * < 1e-6 use exponential notation; pass the tick `step` so the mantissa keeps enough digits
 * to tell adjacent ticks apart (without it, `decimals` caps the mantissa digits at 6).
 */
export function fmtTick(v, decimals, step = null) {
  if (!finite(v)) return '';
  const a = Math.abs(v);
  if (a !== 0 && (a >= 1e7 || a < 1e-6)) {
    const digits = step > 0 ? expDigits(v, step) : Math.max(0, Math.min(6, decimals));
    const s = v.toExponential(digits).replace(/\.?0+e/, 'e').replace('e+', 'e');
    return s.replace(/^-/, MINUS).replace('e-', 'e' + MINUS);
  }
  const s = a.toFixed(decimals);
  const neg = v < 0 && Number(s) !== 0;
  return (neg ? MINUS : '') + s;
}

function superscript(n) {
  return String(n).split('').map((c) => SUPER[c] ?? c).join('');
}

/** Label of the decade 10^k: plain numbers for -3 <= k <= 4, else "10⁻⁵". */
export function decadeLabel(k) {
  if (k >= 0 && k <= 4) return String(Math.pow(10, k));
  if (k < 0 && k >= -3) return Math.pow(10, k).toFixed(-k);
  return '10' + superscript(k);
}

/** Linear ticks: about n ticks at 1-2-5 multiples inside [lo, hi] (either order). */
export function linearTicks(d0, d1, n = 6) {
  if (!finite(d0) || !finite(d1)) return [];
  const lo = Math.min(d0, d1), hi = Math.max(d0, d1);
  if (!(hi > lo)) return [{v: lo, label: fmtTick(lo, stepDecimals(Math.abs(lo) / 10 || 1))}];
  const step = niceStep((hi - lo) / Math.max(1, n));
  const dec = stepDecimals(step);
  const i0 = Math.ceil(lo / step - 1e-9), i1 = Math.floor(hi / step + 1e-9);
  const out = [];
  for (let i = i0; i <= i1 && out.length < MAX_TICKS; i++) {
    const v = i * step;
    out.push({v: Math.abs(v) < step * 1e-9 ? 0 : v, label: fmtTick(v, dec, step)});
  }
  return out;
}

/**
 * Decade ticks over a positive range (either order): majors at 10^k (labelled), minors at
 * 2..9 × 10^k (unlabelled, only while the range spans <= 22 decades). Non-positive or non-finite bounds give []. Capped at MAX_TICKS.
 */
export function decadeTicks(d0, d1) {
  if (!finite(d0) || !finite(d1)) return [];
  let lo = Math.min(d0, d1), hi = Math.max(d0, d1);
  if (!(hi > 0)) return [];
  if (!(lo > 0)) lo = hi / 1e6;
  const k0 = Math.floor(Math.log10(lo) + 1e-9), k1 = Math.ceil(Math.log10(hi) - 1e-9);
  const out = [];
  const decades = k1 - k0 + 1;
  const withMinor = decades * 9 <= MAX_TICKS;   // minors only while all of them fit
  const every = decades > 12 ? Math.ceil(decades / 12) : 1;
  // without minors (> 22 decades) only the labelled decades are emitted, so the ticks span the
  // whole range instead of stopping at MAX_TICKS
  const kStep = withMinor ? 1 : every;
  for (let k = k0; k <= k1 && out.length < MAX_TICKS; k += kStep) {
    const base = Math.pow(10, k);
    if (base >= lo * (1 - 1e-12) && base <= hi * (1 + 1e-12)) {
      out.push({v: base, label: (k - k0) % every === 0 ? decadeLabel(k) : '', major: true});
    }
    if (!withMinor) continue;
    for (let m = 2; m <= 9 && out.length < MAX_TICKS; m++) {
      const v = m * base;
      if (v >= lo * (1 - 1e-12) && v <= hi * (1 + 1e-12)) out.push({v, label: '', major: false});
    }
  }
  return out;
}

/** Ticks for a log axis spanning less than ~1.5 decades: labelled 1-2-5 linear ticks. */
function shortLogTicks(lo, hi, n) {
  const out = linearTicks(lo, hi, n).filter((t) => t.v > 0).map((t) => ({...t, major: true}));
  return out;
}

/**
 * A scale mapping data [d0, d1] onto pixels [r0, r1] (linear in log10 for 'log').
 * invert swaps the pixel ends. Degenerate or invalid domains are widened so f/inv stay
 * finite: lin [v-0.5, v+0.5] (or ±10 %), log [v/10, v*10]; log bounds <= 0 are floored.
 */
export function scale(kind, d0, d1, r0, r1, invert = false) {
  const log = kind === 'log';
  let a0 = Number(d0), a1 = Number(d1);
  if (log) {
    if (!(a0 > 0) || !Number.isFinite(a0)) a0 = a1 > 0 && Number.isFinite(a1) ? a1 / 1000 : 1;
    if (!(a1 > 0) || !Number.isFinite(a1)) a1 = a0 * 1000;
    if (a0 === a1) { a0 /= 10; a1 *= 10; }
  } else {
    if (!Number.isFinite(a0) && !Number.isFinite(a1)) { a0 = 0; a1 = 1; }
    else if (!Number.isFinite(a0)) a0 = a1 - 1;
    else if (!Number.isFinite(a1)) a1 = a0 + 1;
    if (a0 === a1) {
      const w = a0 === 0 ? 0.5 : Math.abs(a0) * 0.1;
      a0 -= w; a1 += w;
    }
  }
  let p0 = Number(r0), p1 = Number(r1);
  if (!Number.isFinite(p0)) p0 = 0;
  if (!Number.isFinite(p1)) p1 = p0 + 1;
  if (invert) [p0, p1] = [p1, p0];
  const tf = log ? (v) => (v > 0 ? Math.log10(v) : NaN) : (v) => +v;
  const itf = log ? (t) => Math.pow(10, t) : (t) => t;
  const t0 = tf(a0), t1 = tf(a1);
  const b = (p1 - p0) / (t1 - t0);
  const a = p0 - b * t0;
  const f = (v) => {
    if (v === null || v === undefined) return NaN;
    const t = tf(v);
    return Number.isFinite(t) ? a + b * t : NaN;
  };
  const inv = (px) => itf((px - a) / b);
  const ticks = (n = 6) => {
    const lo = Math.min(a0, a1), hi = Math.max(a0, a1);
    if (!log) return linearTicks(lo, hi, n);
    if (Math.log10(hi / lo) < 1.5) return shortLogTicks(lo, hi, n);
    return decadeTicks(lo, hi);
  };
  return {kind: log ? 'log' : 'lin', d0: a0, d1: a1, r0: p0, r1: p1, invert: !!invert, a, b, tf, itf, f, inv, ticks};
}

const DEFAULT_MARGIN = {l: 52, r: 12, t: 10, b: 38};

/**
 * Build a view. width/height are CSS px of the drawing surface; margin {l, r, t, b} insets
 * the plot rectangle (labels live in the margin). Returns
 *   {width, height, dpr, margin, plot: {left, top, right, bottom, width, height},
 *    x: {...AxisSpec, s}, y: {...AxisSpec, s}, sx, sy,
 *    toPx(x, y) -> [px, py], toData(px, py) -> [x, y], inPlot(px, py) -> bool}
 */
export function makeView(opts = {}) {
  const width = Math.max(1, Number(opts.width) || 1);
  const height = Math.max(1, Number(opts.height) || 1);
  const m = {...DEFAULT_MARGIN, ...(opts.margin || {})};
  const left = Math.min(width - 1, Math.max(0, m.l));
  const right = Math.max(left + 1, width - Math.max(0, m.r));
  const top = Math.min(height - 1, Math.max(0, m.t));
  const bottom = Math.max(top + 1, height - Math.max(0, m.b));
  const xs = {kind: 'lin', d0: 0, d1: 1, ...(opts.x || {})};
  const ys = {kind: 'lin', d0: 0, d1: 1, ...(opts.y || {})};
  const sx = scale(xs.kind, xs.d0, xs.d1, left, right, !!xs.invert);
  const sy = scale(ys.kind, ys.d0, ys.d1, bottom, top, !!ys.invert);
  const view = {
    width, height, dpr: Number(opts.dpr) > 0 ? Number(opts.dpr) : 1, margin: m,
    plot: {left, top, right, bottom, width: right - left, height: bottom - top},
    x: {...xs, d0: sx.d0, d1: sx.d1, s: sx}, y: {...ys, d0: sy.d0, d1: sy.d1, s: sy},
    sx, sy,
    frame: opts.frame || null,
    toPx(x, y) { return [sx.f(x), sy.f(y)]; },
    toData(px, py) { return [sx.inv(px), sy.inv(py)]; },
    inPlot(px, py) { return px >= left && px <= right && py >= top && py <= bottom; },
  };
  return view;
}

/**
 * Vendor-renderer camera equivalent to the view, in transformed coordinates (log10 of the
 * value on log axes): screen x = width/2 + (X - cx)·scale, screen y = height/2 - (Y - cy)·scale·aspect.
 * The camera is always in true (unshifted) coordinates. gl/xyrenderer.js applies the
 * setXY({origin}) shift itself inside render(), so callers never subtract the origin here.
 */
export function cameraFromView(view) {
  const sx = view.sx, sy = view.sy;
  const W = view.width, H = view.height;
  // px = a + b X  ->  X at W/2
  const cx = (W / 2 - sx.a) / sx.b;
  const cy = (H / 2 - sy.a) / sy.b;
  const scaleX = sx.b;
  const aspect = -sy.b / sx.b;
  return {cx, cy, scale: scaleX, aspect, width: W, height: H};
}

/** Domain after zooming by `factor` (> 1 zooms in) about pixel `px` of scale s. */
export function zoomDomain(s, px, factor) {
  if (!(factor > 0) || !Number.isFinite(factor)) return [s.d0, s.d1];
  const t = (px - s.a) / s.b;
  const t0 = s.tf(s.d0), t1 = s.tf(s.d1);
  return [s.itf(t + (t0 - t) / factor), s.itf(t + (t1 - t) / factor)];
}

/** Domain after dragging the content by dpx pixels along scale s. */
export function panDomain(s, dpx) {
  const dt = -dpx / s.b;
  return [s.itf(s.tf(s.d0) + dt), s.itf(s.tf(s.d1) + dt)];
}

/**
 * Padded extent of finite values ([lo, hi]); log extents ignore values <= 0.
 * Optional robust percentiles q = [qlo, qhi] in 0..1. Returns null when nothing is finite.
 */
export function extent(values, {kind = 'lin', pad = 0.04, q = null, mask = null} = {}) {
  const log = kind === 'log';
  const vals = [];
  let lo = Infinity, hi = -Infinity;
  const n = values ? values.length : 0;
  for (let i = 0; i < n; i++) {
    if (mask && !mask[i]) continue;
    const v = values[i];
    if (!Number.isFinite(v) || (log && !(v > 0))) continue;
    const t = log ? Math.log10(v) : v;
    if (q) vals.push(t);
    if (t < lo) lo = t;
    if (t > hi) hi = t;
  }
  if (!(hi >= lo)) return null;
  if (q && vals.length > 2) {
    vals.sort((a, b) => a - b);
    const at = (p) => vals[Math.max(0, Math.min(vals.length - 1, Math.round(p * (vals.length - 1))))];
    lo = at(q[0]);
    hi = at(q[1]);
  }
  let w = hi - lo;
  if (!(w > 0)) w = log ? 1 : (Math.abs(lo) * 0.1 || 1);
  lo -= w * pad;
  hi += w * pad;
  return log ? [Math.pow(10, lo), Math.pow(10, hi)] : [lo, hi];
}
