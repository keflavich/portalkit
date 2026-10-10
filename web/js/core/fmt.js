// Number and text formatting helpers (ARCHITECTURE.md sections 5.5 and 6).
// Pure module: no DOM access, loadable under `node --test`.
//
// The key function is stepDigits(step): quantized columns (u16q, u8q, ...) carry a
// `step`, and every place that shows a quantized value uses exactly stepDigits(step)
// decimals together with a "±step/2" tooltip (fmtQuant). Exact values from row chunks
// use fmtNum / fmtValue.

export const MISSING = '–';            // en dash shown for null / NaN
const MINUS = '−';                     // typographic minus for sexagesimal output

/**
 * Number of decimals needed to show a value quantized with `step` without implying
 * more precision than the step provides.
 *   0.001 -> 3, 0.0015 -> 3, 0.01 -> 2, 0.0153 -> 2, 0.5 -> 1, 1 -> 0, 25 -> 0.
 * Non-finite or non-positive steps return `opts.fallback` (default 6).
 * The result is clamped to [0, opts.max] (default 12).
 */
export function stepDigits(step, opts = {}) {
  const max = opts.max ?? 12;
  const fallback = opts.fallback ?? 6;
  const s = typeof step === 'string' ? Number(step) : step;
  if (typeof s !== 'number' || !Number.isFinite(s) || s <= 0) return fallback;
  // 1e-9 guards against log10(0.001) = -2.9999999999999996 style round-off.
  const d = Math.ceil(-Math.log10(s) - 1e-9);
  return Math.max(0, Math.min(max, d));
}

function isNum(v) {
  return typeof v === 'number' || typeof v === 'bigint';
}

/** Missing / non-finite handling shared by the number formatters. Returns a string or null. */
function special(v) {
  if (v === null || v === undefined) return MISSING;
  if (typeof v === 'bigint') return null;
  if (typeof v !== 'number') return null;
  if (Number.isNaN(v)) return MISSING;
  if (v === Infinity) return '∞';
  if (v === -Infinity) return '-∞';
  return null;
}

function stripZeros(s) {
  if (s.indexOf('e') >= 0) {
    const [m, e] = s.split('e');
    return stripZeros(m) + 'e' + e;
  }
  if (s.indexOf('.') < 0) return s;
  return s.replace(/0+$/, '').replace(/\.$/, '');
}

/**
 * General number formatting.
 *   fmtNum(v)            auto: 6 significant digits, exponent outside [1e-4, 1e6)
 *   fmtNum(v, 3)         fixed 3 decimals
 *   fmtNum(v, {sig: 4})  4 significant digits
 * null / undefined / NaN give MISSING; +-Infinity give the infinity sign.
 */
export function fmtNum(v, digits) {
  const sp = special(v);
  if (sp !== null) return sp;
  if (typeof v === 'bigint') return v.toString();
  if (typeof v !== 'number') {
    const n = Number(v);
    if (v === '' || !Number.isFinite(n)) return String(v);
    return fmtNum(n, digits);
  }
  if (typeof digits === 'number') {
    const d = Math.max(0, Math.min(20, Math.floor(digits)));
    const out = v.toFixed(d);
    return out === '-0' || /^-0\.0*$/.test(out) ? out.slice(1) : out;
  }
  const sig = (digits && typeof digits === 'object' && digits.sig) || 6;
  if (v === 0) return '0';
  const a = Math.abs(v);
  if (a >= 1e6 || a < 1e-4) return stripZeros(v.toExponential(Math.max(0, sig - 1)));
  // toPrecision switches to exponent form when the exponent >= sig (12345.6 at 3 sig ->
  // "1.23e+4"); inside [1e-4, 1e6) plain notation reads better ("12300").
  const p = v.toPrecision(sig);
  return stripZeros(p.indexOf('e') >= 0 ? String(Number(p)) : p);
}

/**
 * Value quantized with `step`, shown with stepDigits(step) decimals. Small values with a small
 * step (|v| < 1e-4 and more than 6 decimals, e.g. line fluxes ~1e-14 with step 1e-16) are shown
 * in exponent form with the significant digits the step supports, so they do not read as zeros.
 */
export function fmtStep(v, step) {
  const s = typeof step === 'string' ? Number(step) : step;
  if (typeof v === 'number' && Number.isFinite(v) && v !== 0 && Math.abs(v) < 1e-4
      && typeof s === 'number' && Number.isFinite(s) && s > 0) {
    const raw = Math.ceil(-Math.log10(s) - 1e-9);
    if (raw > 6) {
      const sig = Math.floor(Math.log10(Math.abs(v)) + 1e-9) - Math.floor(Math.log10(s) + 1e-9) + 1;
      return v.toExponential(Math.max(0, Math.min(14, sig - 1)));
    }
  }
  return fmtNum(v, stepDigits(step));
}

/**
 * Quantized value plus the tooltip text that discloses the quantization:
 * {text: "18.210", title: "±0.0005 (quantized, step 0.001)"}.
 */
export function fmtQuant(v, step) {
  const text = fmtStep(v, step);
  const s = typeof step === 'string' ? Number(step) : step;
  if (typeof s !== 'number' || !Number.isFinite(s) || s <= 0) return {text, title: ''};
  if (text === MISSING) return {text, title: 'missing'};
  return {text, title: `±${fmtNum(s / 2, {sig: 3})} (quantized, step ${fmtNum(s, {sig: 4})})`};
}

/**
 * printf-style formats used by manifest `fmt` fields: "%.3f", ".3f", "%.2e", "%.4g", "%d", "%s".
 * Unknown formats fall back to fmtNum.
 */
export function fmtPrintf(v, fmt) {
  const sp = special(v);
  if (sp !== null && (typeof v === 'number' || v == null)) return sp;
  const m = /^%?(\d*)(?:\.(\d+))?([fegdis])$/.exec(String(fmt || '').trim());
  if (!m) return fmtNum(v);
  const prec = m[2] === undefined ? undefined : Number(m[2]);
  const kind = m[3];
  if (kind === 's') return String(v);
  const n = typeof v === 'number' ? v : Number(v);
  if (!Number.isFinite(n)) return fmtNum(n);
  let out;
  switch (kind) {
    case 'f': out = n.toFixed(prec ?? 6); break;
    case 'e': out = n.toExponential(prec ?? 6); break;
    case 'g': out = stripZeros(n.toPrecision(Math.max(1, prec ?? 6))); break;
    case 'd':
    case 'i': out = String(Math.round(n)); break;
    default: out = fmtNum(n);
  }
  const width = m[1] ? Number(m[1]) : 0;
  return width > out.length ? out.padStart(width, ' ') : out;
}

/**
 * Format a column value using its manifest metadata.
 * meta: {fmt?, step?, unit?, role?}; opts.exact=true means the value came from a row chunk
 * (full precision); otherwise a quantized column uses step-aware digits.
 * Returns {text, title}.
 */
export function fmtValue(v, meta = {}, opts = {}) {
  if (v === null || v === undefined || (typeof v === 'number' && Number.isNaN(v))) {
    return {text: MISSING, title: 'missing'};
  }
  if (typeof v === 'string') return {text: v, title: ''};
  if (typeof v === 'boolean') return {text: v ? 'yes' : 'no', title: ''};
  if (meta.fmt) return {text: fmtPrintf(v, meta.fmt), title: ''};
  if (!opts.exact && meta.step > 0 && /^u(8|16)q$/.test(meta.enc || 'u16q')) return fmtQuant(v, meta.step);
  return {text: fmtNum(v), title: ''};
}

/** Integer counts with thousands separators: 464489 -> "464,489". */
export function fmtCount(n) {
  if (n === null || n === undefined || !Number.isFinite(Number(n))) return MISSING;
  const r = Math.round(Number(n));
  const s = String(Math.abs(r)).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  return r < 0 ? '-' + s : s;
}

/** Byte counts: 1536 -> "1.5 kB" (decimal kB/MB/GB as in the size budgets' reports). */
export function fmtBytes(b) {
  if (b === null || b === undefined || !Number.isFinite(Number(b))) return MISSING;
  const n = Number(b);
  const a = Math.abs(n);
  if (a < 1000) return `${Math.round(n)} B`;
  const units = ['kB', 'MB', 'GB', 'TB'];
  let v = n / 1000;
  let u = 0;
  while (Math.abs(v) >= 1000 && u < units.length - 1) { v /= 1000; u += 1; }
  return `${stripZeros(v.toFixed(Math.abs(v) >= 100 ? 0 : 1))} ${units[u]}`;
}

/** Milliseconds: 12.3 -> "12 ms", 2345 -> "2.35 s". */
export function fmtMs(ms) {
  if (!Number.isFinite(ms)) return MISSING;
  if (Math.abs(ms) < 1000) return `${Math.round(ms)} ms`;
  return `${stripZeros((ms / 1000).toFixed(2))} s`;
}

function sexa(value, digits, sign) {
  // value >= 0 in hours or degrees; returns [a, b, c] strings with carry handled.
  const scale = Math.pow(10, digits);
  let total = Math.round(value * 3600 * scale);   // in units of 10^-digits seconds
  const perMin = 60 * scale;
  const perUnit = 3600 * scale;
  const a = Math.floor(total / perUnit);
  total -= a * perUnit;
  const b = Math.floor(total / perMin);
  total -= b * perMin;
  const c = total / scale;
  const cs = digits > 0 ? c.toFixed(digits).padStart(3 + digits, '0') : String(c).padStart(2, '0');
  return `${sign}${String(a).padStart(2, '0')}:${String(b).padStart(2, '0')}:${cs}`;
}

/** Right ascension in degrees -> "hh:mm:ss.ss" (wraps into [0, 360)). */
export function fmtRA(deg, digits = 2) {
  if (!Number.isFinite(deg)) return MISSING;
  let d = deg % 360;
  if (d < 0) d += 360;
  let out = sexa(d / 15, digits, '');
  if (out.startsWith('24:')) out = '00:' + out.slice(3);
  return out;
}

/** Declination in degrees -> "+dd:mm:ss.s" / "−dd:mm:ss.s". */
export function fmtDec(deg, digits = 1) {
  if (!Number.isFinite(deg)) return MISSING;
  const sign = deg < 0 ? MINUS : '+';
  return sexa(Math.abs(deg), digits, sign);
}

/** Angles in degrees as a human scale: 0.5 -> "30′", 0.001 -> "3.6″". */
export function fmtAngle(deg) {
  if (!Number.isFinite(deg)) return MISSING;
  const a = Math.abs(deg);
  if (a >= 1) return `${fmtNum(deg, {sig: 4})}°`;
  if (a >= 1 / 60) return `${fmtNum(deg * 60, {sig: 4})}′`;
  return `${fmtNum(deg * 3600, {sig: 4})}″`;
}

/** HTML escaping for text inserted with innerHTML. */
export function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[c]);
}

/** Pluralize: plural(1, "source") -> "1 source", plural(3, "source") -> "3 sources". */
export function plural(n, word, pluralWord) {
  const w = Number(n) === 1 ? word : (pluralWord || word + 's');
  return `${fmtCount(n)} ${w}`;
}

export const _internal = {stripZeros, isNum};
