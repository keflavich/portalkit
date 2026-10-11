// 1D spectrum pane (ARCHITECTURE.md sections 5.7, 6.2, 6.8; capability spectrum_viewer_1d).
//
// Interactions (legacy NIRSpec_6927_SpecViewer.py:288-327,533-615 and
// JWST_Archive_ColorPortal.py drawBig):
//   drag            pan in x (and in y when the y range is fixed)
//   Shift+drag      box zoom (x and y; a flat box zooms x only and keeps autoY)
//   drag on x axis  rubber-band x range
//   wheel           zoom x by 1.15 per notch about the cursor (over the y axis: zoom y)
//   two fingers     pinch zoom in x
//   double-click    reset (full x range, autoY)
//   keys            + / - zoom, arrows pan, 0 reset, p pin
// The y range follows the visible x range (autoY: min/max of the visible samples with 8 %
// padding, y0 = min(0, lo - pad) on a linear axis) until a box zoom or y-axis wheel fixes it.
// View state lives in store.spec ({xr, yr}); spectrum2d links its x axis to spec.xr.
//
// Overlays: pinned spectra (up to options.pin, default 8), segment toggles, variants
// (record channels), band jumps (options.bands), mask runs shaded, filter transmission at
// the bottom 22 % of the plot, atlas features (spec/features.js) with a velocity offset,
// record lines, a reddened template scaled over configurable bands, and an SED mode
// (log-log, catalog photometry on top).

import {makeView, zoomDomain, panDomain} from '../plot/axes.js';
import {prepareCanvas, tokensOf, drawAxes, drawPoints} from '../plot/canvas2d.js';
import {eventToViewPx} from '../plot/brushui.js';
import {fetchJSON} from '../data/fetch.js';
import {fluxUnitFactor, filtersMap, interpLinear, resolveKey, sedPoints, shortName} from '../data/filters.js';
import {
  featureList, layoutFeatures, drawFeatures, featureToggles, featureAt, dopplerFactor, isSorted, lowerIndex,
  hasCoverage,
} from '../spec/features.js';
import {
  modelControls, templateSetFor, loadDoc, datasetOfKind, datasetURL, pageDatasets, gridForGrating, raf,
  waveFactor, sedSeries, focusIndex,
} from './sed.js';

export const meta = {
  type: 'spectrum1d',
  caps: ['spectrum_viewer_1d', 'av_slider', 'model_template_overlay', 'labeled_features_overlay'],
  needs: ['records'],
};

export const PIN_MAX = 8;
export const Y_PAD = 0.08;
export const FILTER_FRAC = 0.22;
export const WHEEL_STEP = 1.15;

// ------------------------------------------------------------------ pure helpers

const fin = (v) => typeof v === 'number' && Number.isFinite(v);

function toF32(a) {
  if (a instanceof Float32Array) return a;
  const out = new Float32Array(a.length);
  for (let i = 0; i < a.length; i++) {
    const v = a[i];
    out[i] = v === null || v === undefined ? NaN : +v;
  }
  return out;
}

/**
 * A decoded record (pks1 / sedcurve Spectrum, or a JSON spectrum {wave|wavelength|w|lam,
 * flux, err?, ...}) as {id, n, wave (micron, Float64Array), channels, units, unitW: 'um',
 * segments, maskRuns, maskBits, lines, meta, sorted}. Null for PNG entries and records
 * without a wavelength array.
 */
export function normalizeRecord(rec) {
  if (!rec || typeof rec !== 'object' || rec.kind === 'png') return null;
  let wave, channels, units, unitW;
  if (rec.wave && rec.channels) {
    wave = rec.wave;
    channels = rec.channels;
    units = rec.units || {};
    unitW = rec.unitW || 'um';
  } else {
    const wk = ['wave', 'wavelength', 'w', 'lam', 'lambda'].find((k) => Array.isArray(rec[k]) || ArrayBuffer.isView(rec[k]));
    if (!wk) return null;
    wave = rec[wk];
    channels = {};
    for (const [k, v] of Object.entries(rec)) {
      if (k === wk || !(Array.isArray(v) || ArrayBuffer.isView(v)) || v.length !== wave.length) continue;
      channels[k] = toF32(v);
    }
    units = rec.units && typeof rec.units === 'object' ? rec.units : {};
    unitW = rec.unit_w || rec.unitW || 'um';
  }
  let wf = 1;
  try {
    wf = waveFactor(unitW);
  } catch (err) {
    if (!(err instanceof RangeError)) throw err;
    wf = 1;
  }
  const n = wave.length;
  const w = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    const v = wave[i];
    w[i] = v === null || v === undefined ? NaN : +v * wf;
  }
  return {
    id: rec.id ?? null, n, wave: w, channels, units, unitW: 'um',
    segments: Array.isArray(rec.segments) ? rec.segments : [],
    maskRuns: Array.isArray(rec.maskRuns) ? rec.maskRuns : Array.isArray(rec.mask_runs) ? rec.mask_runs : [],
    maskBits: rec.maskBits || rec.mask_bits || {},
    lines: Array.isArray(rec.lines) ? rec.lines : [],
    meta: rec.meta || {},
    sorted: isSorted(w),
  };
}

const ERR_RE = /^(err|error|unc|sigma|e)$|(_err|_error|_unc|_sigma)$|^(err_|e_|unc_|sigma_)/i;

/** True for channel names that hold uncertainties. */
export function isErrChannel(name) {
  return ERR_RE.test(String(name));
}

/** Name of the uncertainty channel of channel `name` in rec (null when none). */
export function errChannelName(rec, name) {
  const ch = (rec && rec.channels) || {};
  const cands = name === 'flux' ? ['err', 'flux_err', 'error', 'unc', 'sigma', 'e_flux'] : [`${name}_err`, `err_${name}`, `e_${name}`, `${name}_unc`];
  for (const c of cands) if (Object.prototype.hasOwnProperty.call(ch, c)) return c;
  return null;
}

/** Variant names: options.variants, else index.variants, else the record's non-error channels. */
export function variantsOf(rec, index, opt) {
  if (Array.isArray(opt) && opt.length) return opt.map(String);
  if (index && Array.isArray(index.variants) && index.variants.length) return index.variants.map(String);
  if (rec && rec.channels) return Object.keys(rec.channels).filter((k) => !isErrChannel(k));
  return ['flux'];
}

/**
 * The variant actually shown: `wanted` (store spec.variant) when it is one of `vars`, else the
 * first entry of `vars` (else 'flux'). The variant select and the plotted channel both use it.
 */
export function effectiveVariant(wanted, vars) {
  if (wanted && Array.isArray(vars) && vars.includes(String(wanted))) return String(wanted);
  return Array.isArray(vars) && vars.length ? String(vars[0]) : 'flux';
}

const linkedFull = new WeakMap(); // store -> [w0, w1] | null

/**
 * Full (unzoomed) wavelength extent of the 1D spectrum pane sharing `store`, or null. spectrum2d
 * uses it as its default x range while spec.xr is unset, so both panes start on one range.
 * Changes are announced with the transient store event 'spec:full'.
 */
export function linkedFullExtent(store) {
  return (store && linkedFull.get(store)) || null;
}

/**
 * Channel for a variant: {name, y, err, unit, fallback}. A variant missing from the record
 * falls back to 'flux', then to the first non-error channel (fallback = true).
 */
export function channelFor(rec, variant) {
  if (!rec || !rec.channels) return null;
  const ch = rec.channels;
  let name = variant && Object.prototype.hasOwnProperty.call(ch, variant) ? variant : null;
  let fallback = false;
  if (!name) {
    fallback = true;
    if (ch.flux) name = 'flux';
    else name = Object.keys(ch).find((k) => !isErrChannel(k)) || Object.keys(ch)[0] || null;
  }
  if (!name) return null;
  const en = errChannelName(rec, name);
  return {name, y: ch[name], err: en ? ch[en] : null, unit: (rec.units || {})[name] ?? '', fallback};
}

/** [i0, i1) sample range covering [x0, x1] plus one sample on each side (whole range if unsorted). */
export function visibleRange(wave, x0, x1, sorted = true) {
  const n = wave ? wave.length : 0;
  if (!n) return [0, 0];
  if (!sorted) return [0, n];
  const lo = Math.min(x0, x1), hi = Math.max(x0, x1);
  const a = lowerIndex(wave, lo);
  const b = lowerIndex(wave, hi);
  return [Math.max(0, a), Math.min(n, b + 2)];
}

/**
 * Segment index ranges [{i0, i1, label, on}] of a record; `state` maps label -> false to hide.
 * A record without segments is one range. Ranges are clamped to [0, n).
 */
export function segmentRanges(rec, state = null) {
  const n = rec ? rec.n ?? (rec.wave ? rec.wave.length : 0) : 0;
  const segs = rec && Array.isArray(rec.segments) ? rec.segments : [];
  if (!segs.length) return n ? [{i0: 0, i1: n, label: null, on: true}] : [];
  const out = [];
  for (const s of segs) {
    if (!Array.isArray(s)) continue;
    const i0 = Math.max(0, Math.min(n, s[0] | 0)), i1 = Math.max(0, Math.min(n, s[1] | 0));
    if (i1 <= i0) continue;
    const label = s[2] === undefined || s[2] === null ? null : String(s[2]);
    const on = !(state && label !== null && state[label] === false);
    out.push({i0, i1, label, on});
  }
  return out;
}

/** Unique segment labels of a record in order. */
export function segmentLabels(rec) {
  const out = [];
  for (const s of segmentRanges(rec)) if (s.label !== null && !out.includes(s.label)) out.push(s.label);
  return out;
}

/**
 * Padded y range of the visible samples of several series [{wave, y, ranges?: [{i0, i1, on}],
 * sorted?}] over x range xr. Linear: pad = 8 % of the span and y0 = min(0, lo - pad) when
 * `zero`; log: positive values only, padded by 8 % of the span in dex. Null when no finite
 * sample is visible.
 */
export function autoY(series, xr, opts = {}) {
  const log = !!opts.log;
  const pad = opts.pad ?? Y_PAD;
  const zero = opts.zero !== false;
  const x0 = Math.min(xr[0], xr[1]), x1 = Math.max(xr[0], xr[1]);
  let lo = Infinity, hi = -Infinity;
  for (const s of series || []) {
    if (!s || !s.wave || !s.y) continue;
    const ranges = s.ranges || [{i0: 0, i1: s.wave.length, on: true}];
    const [v0, v1] = visibleRange(s.wave, x0, x1, s.sorted !== false);
    for (const r of ranges) {
      if (r.on === false) continue;
      const a = Math.max(r.i0, v0), b = Math.min(r.i1, v1);
      for (let i = a; i < b; i++) {
        const w = s.wave[i];
        if (!(w >= x0 && w <= x1)) continue;
        const v = s.y[i];
        if (!Number.isFinite(v) || (log && !(v > 0))) continue;
        if (v < lo) lo = v;
        if (v > hi) hi = v;
      }
    }
  }
  for (const v of opts.extra || []) {
    if (!Number.isFinite(v) || (log && !(v > 0))) continue;
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  if (!(hi >= lo)) return null;
  if (log) {
    let a = Math.log10(lo), b = Math.log10(hi);
    let w = b - a;
    if (!(w > 0)) w = 1;
    a -= w * pad;
    b += w * pad;
    return [Math.pow(10, a), Math.pow(10, b)];
  }
  let w = hi - lo;
  if (!(w > 0)) w = Math.abs(hi) * 0.1 || 1;
  const p = w * pad;
  const y0 = zero ? Math.min(0, lo - p) : lo - p;
  return [y0, hi + p];
}

/** Full x extent [lo, hi] of the visible segments of several series; null when empty. */
export function xExtent(series, log = false) {
  let lo = Infinity, hi = -Infinity;
  for (const s of series || []) {
    if (!s || !s.wave) continue;
    const ranges = s.ranges || [{i0: 0, i1: s.wave.length, on: true}];
    for (const r of ranges) {
      if (r.on === false) continue;
      for (let i = r.i0; i < r.i1; i++) {
        const w = s.wave[i];
        if (!Number.isFinite(w) || (log && !(w > 0))) continue;
        if (w < lo) lo = w;
        if (w > hi) hi = w;
      }
    }
  }
  if (!(hi >= lo)) return null;
  if (hi === lo) return log ? [lo / 1.05, hi * 1.05] : [lo - 0.01 * (Math.abs(lo) || 1), hi + 0.01 * (Math.abs(hi) || 1)];
  return [lo, hi];
}

/** Pinned ids after toggling `id` (appended; at most `max`, oldest dropped). */
export function togglePin(pinned, id, max = PIN_MAX) {
  const cur = Array.isArray(pinned) ? pinned.map(String) : [];
  if (id === null || id === undefined) return cur;
  const s = String(id);
  const i = cur.indexOf(s);
  if (i >= 0) return cur.slice(0, i).concat(cur.slice(i + 1));
  const m = Math.max(1, Math.min(PIN_MAX, max | 0 || PIN_MAX));
  const next = cur.concat([s]);
  return next.length > m ? next.slice(next.length - m) : next;
}

/** Masked wavelength spans [{w0, w1, bit, label}] from mask runs [i0, i1, bit] (i1 exclusive). */
export function maskSpans(rec) {
  const out = [];
  if (!rec || !rec.wave) return out;
  const n = rec.wave.length;
  for (const r of rec.maskRuns || []) {
    if (!Array.isArray(r)) continue;
    const i0 = Math.max(0, r[0] | 0), i1 = Math.min(n, r[1] | 0);
    if (i1 <= i0) continue;
    // half a sample on each side so single-sample runs have width
    const a = rec.wave[i0], b = rec.wave[i1 - 1];
    const da = i0 > 0 ? (a - rec.wave[i0 - 1]) / 2 : i0 + 1 < n ? (rec.wave[i0 + 1] - a) / 2 : 0;
    const db = i1 < n ? (rec.wave[i1] - b) / 2 : i1 - 2 >= 0 ? (b - rec.wave[i1 - 2]) / 2 : 0;
    const bit = r[2] ?? 1;
    const label = (rec.maskBits || {})[String(bit)] || `mask ${bit}`;
    out.push({w0: Math.min(a - da, b + db), w1: Math.max(a - da, b + db), bit, label});
  }
  return out;
}

/**
 * Wavelength windows for template normalization: each name matches an option band label
 * ({label, w0, w1}), else a filter (resolveKey in the filters document) giving wl_um +- weff/2.
 * Unknown names are skipped. Returns [[w0, w1], ...] (possibly empty).
 */
export function bandWindows(names, bands, filters) {
  const out = [];
  if (!Array.isArray(names)) return out;
  const fl = filters ? filtersMap(filters) : null;
  for (const raw of names) {
    const name = String(raw).trim();
    if (!name) continue;
    const b = (bands || []).find((x) => x && String(x.label) === name)
      || (bands || []).find((x) => x && String(x.label).toLowerCase() === name.toLowerCase());
    if (b && fin(+b.w0) && fin(+b.w1)) {
      out.push([Math.min(+b.w0, +b.w1), Math.max(+b.w0, +b.w1)]);
      continue;
    }
    if (fl) {
      const k = resolveKey(name, fl);
      const f = k ? fl[k] : null;
      if (f && fin(f.wl_um)) {
        const hw = fin(f.weff_um) && f.weff_um > 0 ? f.weff_um / 2 : f.wl_um * 0.01;
        out.push([f.wl_um - hw, f.wl_um + hw]);
      }
    }
  }
  return out;
}

function median(vals) {
  if (!vals.length) return NaN;
  const s = Float64Array.from(vals).sort();
  const h = s.length >> 1;
  return s.length % 2 ? s[h] : (s[h - 1] + s[h]) / 2;
}

/**
 * Median of obs / template over observed samples inside any window (template linear in
 * wavelength; both finite and > 0). `ranges` limits the observed samples (segments shown).
 * NaN when nothing is usable.
 */
export function templateScale(obs, tpl, windows, ranges = null) {
  if (!obs || !obs.wave || !obs.y || !tpl || !tpl.wave || !tpl.flux) return NaN;
  const ratios = [];
  const rs = ranges || [{i0: 0, i1: obs.wave.length, on: true}];
  for (const [w0, w1] of windows || []) {
    const [v0, v1] = visibleRange(obs.wave, w0, w1, obs.sorted !== false);
    for (const r of rs) {
      if (r.on === false) continue;
      for (let i = Math.max(v0, r.i0); i < Math.min(v1, r.i1); i++) {
        const w = obs.wave[i];
        if (!(w >= w0 && w <= w1)) continue;
        const o = obs.y[i];
        if (!(o > 0) || !Number.isFinite(o)) continue;
        const m = interpLinear(w, tpl.wave, tpl.flux);
        if (m > 0 && Number.isFinite(m)) ratios.push(o / m);
      }
    }
  }
  return median(ratios);
}

/**
 * Pixel path of y(wave) over [i0, i1): [x, y, x, y, ...] with NaN pairs as pen-up breaks.
 * When there are more than two samples per pixel, each pixel column keeps its first, min, max
 * and last samples (min/max decimation keeps spikes and absorption lines visible).
 */
export function decimate(wave, y, i0, i1, sx, sy) {
  const out = [];
  const a = Math.max(0, i0 | 0), b = Math.min(wave ? wave.length : 0, i1 | 0);
  if (b <= a) return out;
  const p0 = sx.f(wave[a]), p1 = sx.f(wave[b - 1]);
  const spanPx = Math.abs(p1 - p0);
  const dense = Number.isFinite(spanPx) && b - a > 2 * Math.max(1, spanPx);
  let broken = true;
  const brk = () => { if (!broken) { out.push(NaN, NaN); broken = true; } };
  if (!dense) {
    for (let i = a; i < b; i++) {
      const px = sx.f(wave[i]), py = sy.f(y[i]);
      if (!Number.isFinite(px) || !Number.isFinite(py)) { brk(); continue; }
      out.push(px, py);
      broken = false;
    }
    return out;
  }
  let col = null, first = 0, last = 0, mn = 0, mx = 0, imn = 0, imx = 0, fx = 0, lx = 0;
  const flush = () => {
    if (col === null) return;
    const pts = [[fx, first, 0], [col + 0.5, mn, imn], [col + 0.5, mx, imx], [lx, last, Infinity]];
    pts.sort((p, q) => p[2] - q[2]);
    for (const [x, v] of pts) out.push(x, v);
    broken = false;
    col = null;
  };
  for (let i = a; i < b; i++) {
    const px = sx.f(wave[i]), py = sy.f(y[i]);
    if (!Number.isFinite(px) || !Number.isFinite(py)) { flush(); brk(); continue; }
    const c = Math.floor(px);
    if (c !== col) {
      flush();
      col = c; first = last = mn = mx = py; imn = imx = i; fx = lx = px;
    } else {
      last = py; lx = px;
      if (py < mn) { mn = py; imn = i; }
      if (py > mx) { mx = py; imx = i; }
    }
  }
  flush();
  return out;
}

/** Error band polygon sample points [{px, lo, hi}] (stride-sampled to at most maxPts). */
export function errBand(wave, y, err, i0, i1, sx, sy, maxPts = 2000) {
  const out = [];
  if (!err) return out;
  const a = Math.max(0, i0), b = Math.min(wave.length, i1);
  const step = Math.max(1, Math.ceil((b - a) / maxPts));
  for (let i = a; i < b; i += step) {
    let e = 0;
    let v = y[i];
    // within a stride use the widest error so the band stays conservative
    for (let k = i; k < Math.min(b, i + step); k++) if (Number.isFinite(err[k]) && err[k] > e) e = err[k];
    const px = sx.f(wave[i]);
    if (!Number.isFinite(v) || !Number.isFinite(px) || !(e > 0)) { out.push(null); continue; }
    out.push({px, lo: sy.f(v - e), hi: sy.f(v + e)});
  }
  return out;
}

/** Valid [a, b] range (a < b, finite; both > 0 on a log axis) or null. */
export function validRange(r, log = false) {
  if (!Array.isArray(r) || r.length !== 2) return null;
  const a = +r[0], b = +r[1];
  if (!Number.isFinite(a) || !Number.isFinite(b) || a === b) return null;
  if (log && (a <= 0 || b <= 0)) return null;
  return a < b ? [a, b] : [b, a];
}

/** Round to 7 significant digits (compact hash values). */
export function sig7(v) {
  return Number.isFinite(v) ? Number(v.toPrecision(7)) : v;
}

function fmtNum(v, digits = 4) {
  if (!Number.isFinite(v)) return '—';
  const a = Math.abs(v);
  if (a !== 0 && (a < 1e-3 || a >= 1e5)) return v.toExponential(digits - 1);
  return Number(v.toPrecision(digits)).toString();
}

function el(tag, cls, attrs) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (attrs) for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
}

// ------------------------------------------------------------------ pane

function recordsDataset(ctx) {
  const cfg = ctx.config || {};
  const ds = pageDatasets(ctx);
  if (cfg.records && ds[cfg.records]) return cfg.records;
  if (ctx.dataset && ds[ctx.dataset] && ds[ctx.dataset].kind === 'records') return ctx.dataset;
  for (const [id, r] of Object.entries(ds)) if (r && r.kind === 'records' && r.record !== 'pki1' && r.record !== 'sedcurve') return id;
  return datasetOfKind(ctx, 'records');
}

export function mount(host, ctx) {
  const cfg = ctx.config || {};
  const store = ctx.store;
  const page = pageDatasets(ctx);
  const recDs = recordsDataset(ctx);
  const catDs = (recDs && page[recDs] && page[recDs].catalog) || (ctx.app && ctx.app.primaryDs) || null;
  const sedMode = cfg.mode === 'sed';
  const logx = sedMode || !!cfg.logx;
  let logy = sedMode || !!cfg.logy;
  const pinMax = Math.max(1, Math.min(PIN_MAX, Number.isInteger(cfg.pin) ? cfg.pin : PIN_MAX));
  const bands = (Array.isArray(cfg.bands) ? cfg.bands : []).filter((b) => b && fin(+b.w0) && fin(+b.w1) && +b.w0 !== +b.w1);
  let showFilters = cfg.filters !== false;
  let showErr = true;

  let rs = null, rsErr = null;
  let focusId = null, focusRec = null, focusI = null, message = 'Select a source';
  const pinned = new Map(); // id -> normalized record | null (missing) | 'loading'
  let tpl = null; // {key, grid, wave, flux, av, rv, label}
  let tplScaleCache = {key: '', scale: NaN};
  let filtersDoc = null, featDoc = null, filtersURL = null, catClient = null;
  const trans = new Map(); // filter key -> {w, t} | null | 'loading'
  let filterKeys = null; // filters used by the catalog
  let sedPts = []; // sed mode photometry (Jy)
  let view = null;
  let featLayout = null;
  let visible = true, destroyed = false;
  let lastW = 0, lastH = 0, dpr = 1;
  let renderMs = 0;
  let lastStats = {};
  let focusSeq = 0, tplSeq = 0;
  let loadedOnce = false;
  let gesture = null;
  const pointers = new Map();
  let hover = null; // {px, py}
  const tplSetP = templateSetFor(ctx, cfg.templates || null).catch((err) => { console.warn('templates unavailable', err); return null; });

  const specGet = () => store.get('spec') || {pinned: [], variant: 'flux'};
  function writeSpec(patch) {
    const next = {...specGet()};
    for (const [k, v] of Object.entries(patch)) {
      if (v === undefined) delete next[k];
      else next[k] = v;
    }
    if (!Array.isArray(next.pinned)) next.pinned = [];
    store.set({spec: next}, {source: ctx.id});
    scheduleRender();
  }

  // ---------------------------------------------------------------- DOM
  const root = el('div', 'pk-spec1d');
  const bar = el('div', 'pk-spec-bar');
  const varSel = el('select', 'pk-spec-variant', {'aria-label': 'Spectrum variant'});
  varSel.addEventListener('change', () => writeSpec({variant: varSel.value}));
  const segBox = el('div', 'pk-spec-segs', {role: 'group', 'aria-label': 'Segments'});
  const bandBox = el('div', 'pk-spec-bands', {role: 'group', 'aria-label': 'Wavelength ranges'});
  if (bands.length) {
    const all = el('button', 'pk-btn pk-spec-band', {type: 'button', title: 'Full range'});
    all.textContent = 'All';
    all.addEventListener('click', () => writeSpec({xr: undefined, yr: undefined}));
    bandBox.append(all);
    for (const b of bands) {
      const btn = el('button', 'pk-btn pk-spec-band', {type: 'button', title: `${fmtNum(+b.w0)}–${fmtNum(+b.w1)} µm`});
      btn.textContent = String(b.label ?? `${b.w0}-${b.w1}`);
      btn.addEventListener('click', () => writeSpec({xr: [sig7(Math.min(+b.w0, +b.w1)), sig7(Math.max(+b.w0, +b.w1))], yr: undefined}));
      bandBox.append(btn);
    }
  }
  const pinBtn = el('button', 'pk-btn pk-spec-pin', {type: 'button', title: `Pin this spectrum (up to ${pinMax}; key p)`});
  pinBtn.textContent = 'Pin';
  pinBtn.addEventListener('click', () => doPin());
  const pinBox = el('div', 'pk-spec-pins', {role: 'list', 'aria-label': 'Pinned spectra'});
  bar.append(varSel, segBox, bandBox, pinBtn, pinBox);

  const more = el('details', 'pk-spec-more');
  const sum = el('summary');
  sum.textContent = 'Overlays';
  more.append(sum);
  const moreBody = el('div', 'pk-spec-more-body');
  const velLab = el('label', 'pk-spec-vel');
  velLab.innerHTML = '<span>v</span>';
  const velIn = el('input', null, {type: 'number', step: '1', size: '6', inputmode: 'decimal', 'aria-label': 'Velocity offset (km/s)', placeholder: '0'});
  const velUnit = el('span', 'pk-muted');
  velUnit.textContent = 'km/s';
  velLab.append(velIn, velUnit);
  velIn.addEventListener('change', () => {
    const v = Number(velIn.value);
    writeSpec({vel: velIn.value.trim() && Number.isFinite(v) && v !== 0 ? v : undefined});
  });
  const togg = (label, title, get, set) => {
    const b = el('button', 'pk-btn pk-spec-toggle', {type: 'button', title});
    b.textContent = label;
    const sync = () => b.setAttribute('aria-pressed', get() ? 'true' : 'false');
    b.addEventListener('click', () => { set(!get()); sync(); scheduleRender(); });
    sync();
    return b;
  };
  const errBtn = togg('±err', 'Show the uncertainty band', () => showErr, (v) => { showErr = v; });
  const filtBtn = togg('Filters', 'Shade filter transmission curves', () => showFilters, (v) => { showFilters = v; if (v) ensureTrans(); });
  const logBtn = togg('log y', 'Logarithmic flux axis', () => logy, (v) => { logy = v; writeSpec({yr: undefined}); });
  const toggles = el('div', 'pk-spec-toggles');
  toggles.append(errBtn, filtBtn, logBtn);
  const feats = featureToggles(store, () => featDoc, {source: ctx.id});
  const models = modelControls(ctx, {templates: tplSetP, bandsPlaceholder: bands.length ? bands.map((b) => b.label).slice(0, 2).join(',') : 'visible'});
  moreBody.append(velLab, toggles, feats.el, models.el);
  more.append(moreBody);
  bar.append(more);

  const wrap = el('div', 'pk-spec-wrap');
  const canvas = el('canvas', 'pk-spec-canvas', {tabindex: '0', role: 'img',
    'aria-label': 'Spectrum plot. Drag to pan, wheel to zoom, Shift+drag to box zoom, double-click to reset.'});
  const readout = el('div', 'pk-spec-readout', {'aria-live': 'polite'});
  wrap.append(canvas, readout);
  root.append(bar, wrap);
  host.append(root);

  const resetBtn = el('button', 'pk-icon-btn pk-spec-reset', {type: 'button', title: 'Reset zoom (double-click / 0)', 'aria-label': 'Reset zoom'});
  resetBtn.textContent = '⟲';
  resetBtn.addEventListener('click', () => writeSpec({xr: undefined, yr: undefined}));
  if (ctx.tools) ctx.tools.append(resetBtn);
  else bar.append(resetBtn);

  // ---------------------------------------------------------------- data loading
  const rsP = (async () => {
    if (!recDs) throw new Error('no records dataset for this spectrum pane');
    rs = await ctx.hub.records(recDs);
    return rs;
  })();
  rsP.catch((err) => { rsErr = err; message = 'Spectra unavailable'; ctx.setStatus('error', String(err && err.message ? err.message : err)); scheduleRender(); });

  (async () => {
    const [fd, fe] = await Promise.all([
      loadDoc(ctx, 'filters').catch((err) => { console.warn('filters document unavailable', err); return null; }),
      loadDoc(ctx, 'features').catch((err) => { console.warn('features document unavailable', err); return null; }),
    ]);
    filtersDoc = fd;
    featDoc = fe;
    const fds = datasetOfKind(ctx, 'filters');
    filtersURL = fds ? datasetURL(ctx, fds) : null;
    feats.update();
    if (catDs && ctx.app && typeof ctx.app.catalog === 'function' && (showFilters || sedMode)) {
      try {
        catClient = await ctx.app.catalog(catDs);
        const keys = [];
        for (const c of (catClient.manifest && catClient.manifest.cols) || []) {
          if ((c.role === 'mag' || c.role === 'flux') && c.filter && !keys.includes(c.filter)) keys.push(c.filter);
        }
        filterKeys = keys.length ? keys : null;
      } catch (err) {
        if (err && err.name === 'AbortError') return;
        console.warn('catalog unavailable for filter shading', err);
      }
    }
    if (showFilters) ensureTrans();
    if (sedMode) loadPhotometry().catch(reportErr);
    scheduleRender();
  })().catch(reportErr);

  function reportErr(err) {
    if (err && err.name === 'AbortError') return;
    console.error(err);
    ctx.setStatus('error', String(err && err.message ? err.message : err));
  }

  function shadedFilters() {
    if (!filtersDoc) return [];
    const fl = filtersMap(filtersDoc);
    const keys = filterKeys || Object.keys(fl);
    return keys.filter((k) => fl[k] && fin(fl[k].wl_um)).slice(0, 40).map((k) => [k, fl[k]]);
  }

  function ensureTrans() {
    for (const [k, f] of shadedFilters()) {
      if (trans.has(k)) continue;
      if (!f.trans) { trans.set(k, null); continue; }
      trans.set(k, 'loading');
      let url;
      try {
        url = new URL(f.trans, filtersURL || (typeof document !== 'undefined' ? document.baseURI : undefined)).toString();
      } catch (err) {
        if (!(err instanceof TypeError)) throw err;
        trans.set(k, null);
        continue;
      }
      fetchJSON(url).then((d) => {
        const w = d && (d.w || d.wave || d.wl_um), t = d && (d.t || d.trans || d.throughput);
        trans.set(k, w && t && w.length === t.length ? {w: Float64Array.from(w), t: Float64Array.from(t, (x) => (x === null ? NaN : x))} : null);
        scheduleRender();
      }, (err) => {
        if (err && err.name === 'AbortError') return;
        trans.set(k, null);
        console.warn(`transmission ${k} unavailable`, err);
      });
    }
  }

  async function loadPhotometry() {
    sedPts = [];
    if (!sedMode || !catClient || !filtersDoc) return;
    const f = store.get('focus');
    if (!f) return;
    const my = focusSeq;
    const i = await focusIndex(ctx, catDs, f);
    if (i === null || my !== focusSeq) return;
    // per-pane record channel (IMPLEMENTATION_NOTES 73): a newer focus supersedes this fetch
    const row = await catClient.record(i, {channel: `spectrum1d:${ctx.id}`});
    if (my !== focusSeq) return;
    try {
      // a row the catalog lacks (null) drops the previous source's points
      sedPts = row ? sedPoints(row, filtersDoc, catClient.manifest.cols || []) : [];
    } catch (err) {
      if (!(err instanceof RangeError)) throw err;
      sedPts = [];
    }
    focusI = i;
    scheduleRender();
  }

  async function loadFocus() {
    const my = ++focusSeq;
    const f = store.get('focus') || null;
    // the previous source's photometry never survives a focus change
    if (sedPts.length) { sedPts = []; scheduleRender(); }
    focusI = null;
    if (!f) {
      focusId = null;
      focusRec = null;
      message = 'Select a source';
      ctx.setStatus('');
      refreshToolbar();
      scheduleRender();
      return;
    }
    let id = f.id;
    if ((id === null || id === undefined) && Number.isInteger(f.i) && ctx.app && typeof ctx.app.idOf === 'function') {
      id = await ctx.app.idOf(f.ds, f.i);
    }
    if (my !== focusSeq) return;
    if (id === null || id === undefined) return;
    id = String(id);
    // a failed record-set load is already reported by the rsP handler above
    const store_ = await rsP.then((s) => s, () => null);
    if (!store_ || my !== focusSeq) return;
    if (!store_.has(id)) {
      focusId = id;
      focusRec = null;
      message = `No spectrum for ${id}`;
      ctx.setStatus('no spectrum');
      refreshToolbar();
      if (sedMode) loadPhotometry().catch(reportErr); // photometry-only sources still get an SED
      scheduleRender();
      return;
    }
    ctx.setStatus('loading…');
    let raw;
    try {
      raw = await store_.get(id, {token: my, channel: ctx.id});
    } catch (err) {
      if (my !== focusSeq || destroyed) return; // a superseded focus never reports
      if (err && err.name === 'AbortError') throw err;
      console.warn(`spectrum ${id} unavailable`, err);
      focusId = id;
      focusRec = null;
      message = `Spectrum for ${id} unavailable`;
      ctx.setStatus('error', String(err && err.message ? err.message : err));
      refreshToolbar();
      if (sedMode) loadPhotometry().catch(reportErr);
      scheduleRender();
      return;
    }
    if (raw === null || my !== focusSeq || destroyed) return;
    const rec = normalizeRecord(raw);
    focusId = id;
    focusRec = rec;
    message = rec ? null : `Record ${id} is not a 1D spectrum`;
    const sp = specGet();
    if (loadedOnce && sp.yr) writeSpec({yr: undefined});
    loadedOnce = true;
    ctx.setStatus(rec ? `${rec.n} px` : '', rec ? `${id} · ${rec.n} samples${rec.meta && rec.meta.grating ? ` · ${rec.meta.grating}` : ''}` : '');
    refreshToolbar();
    if (sedMode) loadPhotometry().catch(reportErr);
    loadTemplate().catch(reportErr);
    scheduleRender();
  }

  function syncPinned() {
    const ids = (specGet().pinned || []).map(String);
    for (const k of Array.from(pinned.keys())) if (!ids.includes(k)) pinned.delete(k);
    for (const id of ids) {
      if (pinned.has(id)) continue;
      pinned.set(id, 'loading');
      rsP.then(async (s) => {
        if (!s.has(id)) { pinned.set(id, null); return; }
        const raw = await s.get(id, {channel: `${ctx.id}:pin`});
        if (pinned.get(id) === 'loading') pinned.set(id, normalizeRecord(raw));
      }).then(() => { refreshPins(); scheduleRender(); }, (err) => {
        if (err && err.name === 'AbortError') return;
        pinned.set(id, null);
        console.warn(`pinned spectrum ${id} unavailable`, err);
      });
    }
    refreshPins();
  }

  async function loadTemplate() {
    const my = ++tplSeq;
    const t = store.get('template');
    if (!t || !t.key) { tpl = null; scheduleRender(); return; }
    const ts = await tplSetP;
    if (!ts || my !== tplSeq) { if (!ts) tpl = null; return; }
    const grids = ts.gridNames();
    const grid = t.grid && grids.includes(t.grid) ? t.grid : gridForGrating(focusRec && focusRec.meta ? focusRec.meta.grating : '', grids);
    if (!grid) { tpl = null; return; }
    const av = Number(store.get('av')) || 0;
    const rv = Number(store.get('rv')) || 3.1;
    let m;
    try {
      m = await ts.reddened(t.key, grid, av, rv);
    } catch (err) {
      if (my !== tplSeq) return;
      tpl = null;
      ctx.setStatus('template error', String(err && err.message ? err.message : err));
      scheduleRender();
      return;
    }
    if (my !== tplSeq) return;
    const entry = ts.list().find((e) => e.key === t.key);
    tpl = {key: t.key, grid, wave: m.wave, flux: m.flux, av, rv, label: entry ? entry.label || entry.key : t.key};
    tplScaleCache = {key: '', scale: NaN};
    scheduleRender();
  }

  function doPin() {
    if (focusId === null) return;
    writeSpec({pinned: togglePin(specGet().pinned, focusId, pinMax)});
    syncPinned();
  }

  // ---------------------------------------------------------------- toolbar refresh
  function refreshToolbar() {
    const sp = specGet();
    const vars = variantsOf(focusRec, rs && rs.index, cfg.variants);
    varSel.textContent = '';
    for (const v of vars) {
      const o = el('option');
      o.value = v;
      o.textContent = v;
      varSel.append(o);
    }
    varSel.value = effectiveVariant(sp.variant, vars);
    varSel.hidden = vars.length < 2;
    segBox.textContent = '';
    const labels = segmentLabels(focusRec);
    if (labels.length > 1) {
      const segState = sp.segments || {};
      for (const lab of labels) {
        const b = el('button', 'pk-btn pk-spec-seg', {type: 'button', title: `Show or hide ${lab}`});
        b.textContent = lab;
        b.setAttribute('aria-pressed', segState[lab] === false ? 'false' : 'true');
        b.addEventListener('click', () => {
          const s = {...(specGet().segments || {})};
          if (s[lab] === false) delete s[lab];
          else s[lab] = false;
          writeSpec({segments: Object.keys(s).length ? s : undefined, yr: undefined});
          refreshToolbar();
        });
        segBox.append(b);
      }
    }
    segBox.hidden = labels.length < 2;
    const ids = (sp.pinned || []).map(String);
    pinBtn.disabled = focusId === null || !focusRec;
    pinBtn.setAttribute('aria-pressed', focusId !== null && ids.includes(String(focusId)) ? 'true' : 'false');
    pinBtn.textContent = focusId !== null && ids.includes(String(focusId)) ? 'Unpin' : 'Pin';
    if (document.activeElement !== velIn) velIn.value = fin(sp.vel) && sp.vel !== 0 ? String(sp.vel) : '';
    refreshPins();
  }

  function refreshPins() {
    const tk = ctx.theme() || {};
    const cat = tk.cat || [];
    const ids = (specGet().pinned || []).map(String);
    pinBox.textContent = '';
    ids.forEach((id, k) => {
      const chip = el('span', 'pk-chip pk-spec-pinchip', {role: 'listitem'});
      chip.style.setProperty('--pk-pin-color', cat[(k + 1) % 10] || '#888');
      const st = pinned.get(id);
      const name = el('span');
      name.textContent = id;
      if (st === null) { chip.classList.add('pk-missing'); chip.title = `No spectrum for ${id}`; }
      const x = el('button', 'pk-spec-unpin', {type: 'button', 'aria-label': `Unpin ${id}`, title: `Unpin ${id}`});
      x.textContent = '×';
      x.addEventListener('click', () => {
        writeSpec({pinned: togglePin(specGet().pinned, id, pinMax)});
        syncPinned();
        refreshToolbar();
      });
      chip.append(name, x);
      pinBox.append(chip);
    });
    pinBox.hidden = !ids.length;
  }

  // ---------------------------------------------------------------- series & view
  function seriesOf(rec, variant, segState) {
    if (!rec) return null;
    const ch = channelFor(rec, variant);
    if (!ch) return null;
    let y = ch.y, err = ch.err;
    let unit = ch.unit;
    if (sedMode) {
      // SED mode plots F_nu in Jy
      let fac = 1;
      try {
        fac = fluxUnitFactor(unit || (rs && rs.index && rs.index.unit_f) || 'Jy');
      } catch (e) {
        if (!(e instanceof RangeError)) throw e;
      }
      if (fac !== 1) {
        y = Float32Array.from(y, (v) => v * fac);
        if (err) err = Float32Array.from(err, (v) => v * fac);
      }
      unit = 'Jy';
    }
    return {rec, wave: rec.wave, y, err, unit, name: ch.name, fallback: ch.fallback, sorted: rec.sorted,
      ranges: segmentRanges(rec, segState)};
  }

  const seriesCache = new WeakMap(); // rec -> {key, s}
  function cachedSeries(rec, variant, segState) {
    if (!rec) return null;
    const key = `${variant}|${JSON.stringify(segState || {})}|${sedMode}`;
    const hit = seriesCache.get(rec);
    if (hit && hit.key === key) return hit.s;
    const s = seriesOf(rec, variant, segState);
    seriesCache.set(rec, {key, s});
    return s;
  }

  function computeView(w, h) {
    const sp = specGet();
    const variant = effectiveVariant(sp.variant, variantsOf(focusRec, rs && rs.index, cfg.variants));
    const segState = sp.segments || null;
    const main = cachedSeries(focusRec, variant, segState);
    const pins = [];
    for (const id of (sp.pinned || []).map(String)) {
      const r = pinned.get(id);
      if (!r || r === 'loading' || id === String(focusId)) continue;
      const s = cachedSeries(r, variant, segState);
      if (s) pins.push({id, s});
    }
    const all = (main ? [main] : []).concat(pins.map((p) => p.s));
    const specFull = xExtent(all, logx);
    if (!sedMode) publishFull(specFull);
    let full = specFull;
    if (sedMode && sedPts.length) {
      // SED mode: the photometry widens the range (0.08 dex padding) so no band falls off the plot
      let lo = Infinity, hi = -Infinity;
      for (const p of sedPts) if (p.wl > 0) { lo = Math.min(lo, p.wl); hi = Math.max(hi, p.wl); }
      if (hi >= lo) {
        const pad = Math.pow(10, 0.08);
        full = full ? [Math.min(full[0], lo / pad), Math.max(full[1], hi * pad)] : [lo / pad, hi * pad];
      }
    }
    if (!full) full = logx ? [0.5, 30] : [1, 5];
    let xr = validRange(sp.xr, logx) || full;
    const fixedY = validRange(sp.yr, logy);
    let yr = fixedY;
    if (!yr) {
      const extra = [];
      if (sedMode) for (const p of sedPts) if (p.wl >= xr[0] && p.wl <= xr[1]) extra.push(p.fnu);
      yr = autoY(all, xr, {log: logy, zero: !logy, extra}) || (logy ? [1e-6, 1] : [0, 1]);
    }
    const yLabel = main ? (sedMode ? 'Fν (Jy)' : `${main.name}${main.unit ? ` (${main.unit})` : ''}`) : 'flux';
    const v = makeView({
      width: w, height: h, dpr,
      margin: {l: 58, r: 10, t: 8, b: 36},
      x: {kind: logx ? 'log' : 'lin', d0: xr[0], d1: xr[1]},
      y: {kind: logy ? 'log' : 'lin', d0: yr[0], d1: yr[1]},
    });
    return {view: v, main, pins, all, xr, yr, autoY: !fixedY, full, yLabel, variant, sp};
  }

  function tplScaleFor(main, xr) {
    const t = store.get('template');
    if (!tpl || !main || !t) return NaN;
    if (typeof t.scale === 'number' && t.scale > 0) return t.scale;
    let windows = bandWindows(t.bands, bands, filtersDoc);
    if (!windows.length) windows = [xr];
    const key = `${focusId}|${main.name}|${tpl.key}|${tpl.grid}|${tpl.av}|${tpl.rv}|${JSON.stringify(windows)}|${JSON.stringify(specGet().segments || {})}`;
    if (tplScaleCache.key === key) return tplScaleCache.scale;
    const s = templateScale(main, tpl, windows, main.ranges);
    tplScaleCache = {key, scale: s};
    return s;
  }

  let publishedFull = null;
  function publishFull(full) {
    const same = full && publishedFull ? full[0] === publishedFull[0] && full[1] === publishedFull[1] : full === publishedFull;
    if (same) return;
    publishedFull = full ? [full[0], full[1]] : null;
    linkedFull.set(store, publishedFull);
    if (typeof store.emit === 'function') store.emit('spec:full', {source: ctx.id, full: publishedFull});
  }

  // ---------------------------------------------------------------- drawing
  let rafPending = false;
  function scheduleRender() {
    if (rafPending || destroyed) return;
    rafPending = true;
    raf(() => {
      rafPending = false;
      render();
    });
  }

  function strokePath(c, pts, color, width, alpha, dash) {
    c.globalAlpha = alpha;
    c.strokeStyle = color;
    c.lineWidth = width;
    c.setLineDash(dash || []);
    c.beginPath();
    let pen = false;
    for (let k = 0; k < pts.length; k += 2) {
      const x = pts[k], y = pts[k + 1];
      if (!Number.isFinite(x) || !Number.isFinite(y)) { pen = false; continue; }
      if (pen) c.lineTo(x, y);
      else { c.moveTo(x, y); pen = true; }
    }
    c.stroke();
    c.setLineDash([]);
  }

  function drawSeries(c, v, s, color, width, alpha, withErr) {
    if (!s) return 0;
    const [v0, v1] = visibleRange(s.wave, v.sx.d0, v.sx.d1, s.sorted !== false);
    let n = 0;
    for (const r of s.ranges) {
      if (!r.on) continue;
      const a = Math.max(r.i0, v0), b = Math.min(r.i1, v1);
      if (b <= a) continue;
      n += b - a;
      if (withErr && s.err) {
        const band = errBand(s.wave, s.y, s.err, a, b, v.sx, v.sy, Math.max(200, v.plot.width * 2));
        c.globalAlpha = 0.18;
        c.fillStyle = color;
        let run = [];
        const flush = () => {
          if (run.length > 1) {
            c.beginPath();
            c.moveTo(run[0].px, run[0].hi);
            for (const p of run) c.lineTo(p.px, Number.isFinite(p.hi) ? p.hi : v.plot.top);
            for (let k = run.length - 1; k >= 0; k--) c.lineTo(run[k].px, Number.isFinite(run[k].lo) ? run[k].lo : v.plot.bottom);
            c.closePath();
            c.fill();
          }
          run = [];
        };
        for (const p of band) { if (p === null) flush(); else run.push(p); }
        flush();
      }
      strokePath(c, decimate(s.wave, s.y, a, b, v.sx, v.sy), color, width, alpha);
    }
    return n;
  }

  function render() {
    if (!visible || destroyed) return;
    const w = wrap.clientWidth || lastW, h = wrap.clientHeight || lastH;
    if (!(w > 20 && h > 20)) return;
    const t0 = typeof performance !== 'undefined' ? performance.now() : 0;
    const tk = tokensOf(ctx.theme());
    const cat = tk.cat || ['#4c78a8', '#f58518', '#54a24b', '#e45756', '#72b7b2', '#eeca3b', '#b279a2', '#ff9da6', '#9d755d', '#bab0ac'];
    const c = prepareCanvas(canvas, w, h, dpr);
    const V = computeView(w, h);
    view = V.view;
    const v = view;
    const P = v.plot;
    drawAxes(c, v, {tokens: tk, xLabel: 'λ (µm)', yLabel: V.yLabel});
    c.save();
    c.beginPath();
    c.rect(P.left, P.top, P.width, P.height);
    c.clip();
    // zero line
    if (!logy && V.yr[0] < 0 && V.yr[1] > 0) {
      const y0 = Math.round(v.sy.f(0)) + 0.5;
      c.globalAlpha = 0.5;
      c.strokeStyle = tk.plotAxis;
      c.lineWidth = 1;
      c.beginPath();
      c.moveTo(P.left, y0);
      c.lineTo(P.right, y0);
      c.stroke();
      c.globalAlpha = 1;
    }
    // filter transmission (bottom 22 %)
    const shaded = [];
    if (showFilters && filtersDoc) {
      const H = P.height * FILTER_FRAC;
      let k = 0;
      for (const [key, f] of shadedFilters()) {
        const tr = trans.get(key);
        const hw = fin(f.weff_um) ? f.weff_um / 2 : 0;
        const lo = tr && tr !== 'loading' ? tr.w[0] : f.wl_um - hw;
        const hi = tr && tr !== 'loading' ? tr.w[tr.w.length - 1] : f.wl_um + hw;
        if (hi < v.sx.d0 || lo > v.sx.d1) { k++; continue; }
        const color = cat[(k + 2) % cat.length];
        k++;
        shaded.push(key);
        c.fillStyle = color;
        c.strokeStyle = color;
        c.lineWidth = 1;
        if (tr && tr !== 'loading') {
          c.beginPath();
          c.moveTo(v.sx.f(tr.w[0]), P.bottom);
          for (let i = 0; i < tr.w.length; i++) {
            const t = Number.isFinite(tr.t[i]) ? Math.max(0, Math.min(1, tr.t[i])) : 0;
            c.lineTo(v.sx.f(tr.w[i]), P.bottom - t * H);
          }
          c.lineTo(v.sx.f(tr.w[tr.w.length - 1]), P.bottom);
          c.closePath();
          c.globalAlpha = 0.12;
          c.fill();
          c.globalAlpha = 0.45;
          c.stroke();
        } else if (hw > 0) {
          const a = v.sx.f(lo), b = v.sx.f(hi);
          c.globalAlpha = 0.08;
          c.fillRect(Math.min(a, b), P.bottom - H, Math.abs(b - a), H);
        }
        c.globalAlpha = 0.85;
        c.font = `9px ${tk.font}`;
        c.textAlign = 'center';
        c.textBaseline = 'bottom';
        const lx = v.sx.f(f.wl_um);
        if (Number.isFinite(lx)) c.fillText(f.label || shortName(key), lx, P.bottom - H - 1);
      }
      c.globalAlpha = 1;
    }
    // mask shading
    const masks = focusRec ? maskSpans(focusRec) : [];
    if (masks.length) {
      c.fillStyle = tk.fg3;
      c.globalAlpha = 0.13;
      for (const m of masks) {
        const a = v.sx.f(m.w0), b = v.sx.f(m.w1);
        if (!Number.isFinite(a) || !Number.isFinite(b)) continue;
        c.fillRect(Math.min(a, b), P.top, Math.max(1, Math.abs(b - a)), P.height);
      }
      c.globalAlpha = 1;
    }
    c.restore();
    // features + record lines
    const sp = V.sp;
    const vel = fin(sp.vel) ? sp.vel : 0;
    const fams = store.get('features') || [];
    let flist = featDoc ? featureList(featDoc, fams, {vel}) : [];
    if (focusRec && focusRec.lines && focusRec.lines.length) {
      const d = dopplerFactor(vel);
      for (const ln of focusRec.lines) {
        if (!ln || !fin(+ln.w)) continue;
        flist.push({name: String(ln.name ?? ''), label: String(ln.name ?? ''), w: +ln.w * d, w0: +ln.w, lo: null, hi: null,
          kind: 'line', color: ln.detected ? tk.sel : tk.fg3, family: 'record', group: ln.kind ?? null, requiresCoverage: false});
      }
      flist = flist.sort((a, b) => a.w - b.w);
    }
    const covSeries = V.main;
    const coverage = covSeries ? (wv) => {
      // covered: a finite sample near wv inside a segment that is shown
      let inShown = false;
      for (const r of covSeries.ranges) {
        if (r.on && wv >= covSeries.wave[r.i0] && wv <= covSeries.wave[r.i1 - 1]) { inShown = true; break; }
      }
      return inShown && hasCoverage(covSeries.wave, covSeries.y, wv);
    } : (() => false);
    featLayout = flist.length ? layoutFeatures(flist, v, {
      rows: 4, maxLabels: 34, fontSize: 10, minGap: 6, coverage,
      measure: (t) => { c.font = `10px ${tk.font}`; return c.measureText(t).width; },
    }) : null;
    if (featLayout) drawFeatures(c, v, featLayout, {tokens: tk});
    // spectra
    c.save();
    c.beginPath();
    c.rect(P.left, P.top, P.width, P.height);
    c.clip();
    V.pins.forEach((p) => {
      const k = (sp.pinned || []).map(String).indexOf(p.id);
      drawSeries(c, v, p.s, cat[(k + 1) % cat.length], 1, 0.85, false);
    });
    const mainColor = tk.fg;
    const visibleN = V.main ? drawSeries(c, v, V.main, mainColor, 1.2, 1, showErr) : 0;
    // template
    let tplInfo = null;
    if (tpl && V.main) {
      const scale = tplScaleFor(V.main, V.xr);
      if (Number.isFinite(scale) && scale > 0) {
        const tw = tpl.wave, tf = tpl.flux;
        const ys = new Float64Array(tf.length);
        for (let i = 0; i < tf.length; i++) ys[i] = tf[i] * scale;
        const [a, b] = visibleRange(tw, v.sx.d0, v.sx.d1, true);
        const path = decimate(tw, ys, a, b, v.sx, v.sy);
        strokePath(c, path, cat[1] || '#f58518', 1.4, 0.95, [6, 4]);
        const pys = [];
        for (let k = 1; k < path.length; k += 2) if (Number.isFinite(path[k])) pys.push(path[k]);
        pys.sort((x, y) => x - y);
        const ymed = [];
        for (let i = a; i < b; i++) if (Number.isFinite(ys[i])) ymed.push(ys[i]);
        ymed.sort((x, y) => x - y);
        tplInfo = {key: tpl.key, grid: tpl.grid, scale, av: tpl.av, rv: tpl.rv, n: b - a, label: tpl.label,
          medianPx: pys.length ? pys[pys.length >> 1] : null, yMedian: ymed.length ? ymed[ymed.length >> 1] : null};
      } else {
        tplInfo = {key: tpl.key, grid: tpl.grid, scale: NaN, av: tpl.av, rv: tpl.rv, n: 0, label: tpl.label, medianPx: null, yMedian: null};
      }
    }
    c.restore();
    // sed-mode photometry
    let ptsDrawn = [];
    if (sedMode && sedPts.length) {
      ptsDrawn = sedSeries(sedPts, 'fnu', filtersDoc);
      drawPoints(c, v, ptsDrawn.map((p) => ({x: p.x, y: p.y, ex: p.ex, ey: p.ey, limit: p.limit, color: cat[3] || '#e45756', size: 7})), {tokens: tk});
    }
    // gesture previews
    if (gesture && (gesture.kind === 'box' || gesture.kind === 'band')) {
      c.save();
      c.strokeStyle = tk.sel;
      c.fillStyle = tk.sel;
      c.lineWidth = 1;
      const x0 = Math.min(gesture.x0, gesture.x1), x1 = Math.max(gesture.x0, gesture.x1);
      if (gesture.kind === 'band') {
        c.globalAlpha = 0.15;
        c.fillRect(x0, P.top, x1 - x0, P.height);
        c.globalAlpha = 0.9;
        c.strokeRect(x0 + 0.5, P.top + 0.5, x1 - x0, P.height - 1);
      } else {
        const flat = Math.abs(gesture.y1 - gesture.y0) <= 8;
        const y0 = flat ? P.top : Math.min(gesture.y0, gesture.y1), y1 = flat ? P.bottom : Math.max(gesture.y0, gesture.y1);
        c.globalAlpha = 0.12;
        c.fillRect(x0, y0, x1 - x0, y1 - y0);
        c.globalAlpha = 0.9;
        c.setLineDash([4, 3]);
        c.strokeRect(x0 + 0.5, y0 + 0.5, x1 - x0, y1 - y0);
      }
      c.restore();
    }
    // legend
    const leg = [];
    if (V.main) leg.push([mainColor, String(focusId) + (V.main.fallback ? ` (${V.main.name})` : ''), null]);
    V.pins.forEach((p) => {
      const k = (sp.pinned || []).map(String).indexOf(p.id);
      leg.push([cat[(k + 1) % cat.length], p.id, null]);
    });
    if (tplInfo && Number.isFinite(tplInfo.scale)) leg.push([cat[1] || '#f58518', `${tplInfo.label} A_V=${fmtNum(tplInfo.av, 3)}`, [6, 4]]);
    if (leg.length > 1 || tplInfo) {
      c.save();
      c.font = `10px ${tk.font}`;
      c.textBaseline = 'middle';
      const lw = Math.min(220, Math.max(...leg.map((l) => c.measureText(l[1]).width)) + 28);
      const x = P.right - lw - 6;
      let y = P.top + 10 + (featLayout && featLayout.labelled ? 4 * 13 + 2 : 0);
      c.globalAlpha = 0.85;
      c.fillStyle = tk.plotBg;
      c.fillRect(x - 4, y - 8, lw + 8, leg.length * 14 + 2);
      c.globalAlpha = 1;
      for (const [color, text, dash] of leg) {
        c.strokeStyle = color;
        c.lineWidth = 1.5;
        c.setLineDash(dash || []);
        c.beginPath();
        c.moveTo(x, y);
        c.lineTo(x + 18, y);
        c.stroke();
        c.setLineDash([]);
        c.fillStyle = tk.fg2;
        c.fillText(text.length > 32 ? `${text.slice(0, 31)}…` : text, x + 24, y);
        y += 14;
      }
      c.restore();
    }
    if (!V.main) {
      c.save();
      c.fillStyle = tk.fg3;
      c.font = `12px ${tk.font}`;
      c.textAlign = 'center';
      c.textBaseline = 'middle';
      c.fillText(rsErr ? 'Spectra could not be loaded' : message || 'Select a source', (P.left + P.right) / 2, (P.top + P.bottom) / 2);
      c.restore();
    }
    // hover crosshair + readout
    updateReadout(V, masks);
    renderMs = (typeof performance !== 'undefined' ? performance.now() : 0) - t0;
    lastStats = {
      xr: [v.sx.d0, v.sx.d1], yr: [v.sy.d0, v.sy.d1], autoY: V.autoY, full: V.full,
      n: V.main ? V.main.wave.length : 0, visibleN, variant: V.main ? V.main.name : V.variant,
      template: tplInfo, shaded, masks: masks.length,
      features: featLayout ? {shown: featLayout.shown, labelled: featLayout.labelled, dropped: featLayout.dropped, dashed: featLayout.dashed, named: featLayout.named} : {shown: 0, labelled: 0, dropped: 0, dashed: 0, named: true},
      points: ptsDrawn.map((p) => ({wl: p.wl, fnu: p.fnu, kind: p.kind, key: p.key})),
      pinnedShown: V.pins.map((p) => p.id),
    };
  }

  function updateReadout(V, masks) {
    if (!hover || !view || !view.inPlot(hover.px, hover.py)) { readout.textContent = ''; readout.hidden = true; return; }
    const [wx] = view.toData(hover.px, hover.py);
    const parts = [`λ ${fmtNum(wx, 6)} µm`];
    const s = V.main;
    if (s && s.sorted !== false) {
      const j = lowerIndex(s.wave, wx);
      let k = j;
      if (j + 1 < s.wave.length && (j < 0 || Math.abs(s.wave[j + 1] - wx) < Math.abs(s.wave[j] - wx))) k = j + 1;
      if (k >= 0 && k < s.wave.length) {
        const yv = s.y[k];
        parts.push(`${s.name} ${fmtNum(yv, 4)}${s.err && Number.isFinite(s.err[k]) ? ` ± ${fmtNum(s.err[k], 2)}` : ''}${s.unit ? ` ${s.unit}` : ''}`);
      }
    }
    const fh = featLayout ? featureAt(featLayout, hover.px, 4) : null;
    if (fh) parts.push(`${fh.f.label || fh.f.name} ${fmtNum(fh.f.w0, 6)}${fh.dashed ? ' (no data)' : ''}`);
    for (const m of masks) if (wx >= m.w0 && wx <= m.w1) { parts.push(m.label); break; }
    readout.textContent = parts.join('  ·  ');
    readout.hidden = false;
  }

  // ---------------------------------------------------------------- interaction
  const pos = (e) => (view ? eventToViewPx(canvas, view, e) : [0, 0]);

  canvas.addEventListener('pointerdown', (e) => {
    if (!view) return;
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    const [px, py] = pos(e);
    pointers.set(e.pointerId, [px, py]);
    if (canvas.setPointerCapture) {
      try {
        canvas.setPointerCapture(e.pointerId);
      } catch (err) {
        if (!(err && err.name === 'NotFoundError')) throw err;
      }
    }
    const P = view.plot;
    if (pointers.size === 2) {
      const [a, b] = Array.from(pointers.values());
      gesture = {kind: 'pinch', d0: Math.max(10, Math.abs(a[0] - b[0])), mid: (a[0] + b[0]) / 2, sx: view.sx};
      return;
    }
    if (py > P.bottom && px >= P.left && px <= P.right) gesture = {kind: 'band', x0: px, x1: px};
    else if (e.shiftKey && view.inPlot(px, py)) gesture = {kind: 'box', x0: px, y0: py, x1: px, y1: py};
    else if (view.inPlot(px, py)) gesture = {kind: 'pan', px0: px, py0: py, sx: view.sx, sy: view.sy, yFixed: !!validRange(specGet().yr, logy), moved: false};
    else gesture = null;
    if (gesture) {
      e.preventDefault();
      if (typeof canvas.focus === 'function') canvas.focus({preventScroll: true});
    }
  });

  canvas.addEventListener('pointermove', (e) => {
    if (!view) return;
    const [px, py] = pos(e);
    hover = {px, py};
    if (pointers.has(e.pointerId)) pointers.set(e.pointerId, [px, py]);
    if (!gesture) { scheduleRender(); return; }
    if (gesture.kind === 'pinch' && pointers.size >= 2) {
      const [a, b] = Array.from(pointers.values());
      const d = Math.max(10, Math.abs(a[0] - b[0]));
      const xr = zoomDomain(gesture.sx, gesture.mid, d / gesture.d0);
      writeSpec({xr: [sig7(xr[0]), sig7(xr[1])]});
    } else if (gesture.kind === 'pan') {
      const dx = px - gesture.px0, dy = py - gesture.py0;
      if (!gesture.moved && Math.hypot(dx, dy) < 3) return;
      gesture.moved = true;
      const xr = panDomain(gesture.sx, dx);
      const patch = {xr: [sig7(xr[0]), sig7(xr[1])]};
      if (gesture.yFixed) {
        const yr = panDomain(gesture.sy, dy);
        patch.yr = [sig7(Math.min(yr[0], yr[1])), sig7(Math.max(yr[0], yr[1]))];
      }
      writeSpec(patch);
    } else if (gesture.kind === 'box' || gesture.kind === 'band') {
      gesture.x1 = Math.max(view.plot.left, Math.min(view.plot.right, px));
      gesture.y1 = Math.max(view.plot.top, Math.min(view.plot.bottom, py));
      scheduleRender();
    }
  });

  const endGesture = (e) => {
    pointers.delete(e.pointerId);
    const g = gesture;
    if (!g) return;
    if (g.kind === 'pinch') {
      if (pointers.size < 2) gesture = null;
      return;
    }
    gesture = null;
    if (!view) return;
    if (g.kind === 'band' && Math.abs(g.x1 - g.x0) > 3) {
      const a = view.sx.inv(Math.min(g.x0, g.x1)), b = view.sx.inv(Math.max(g.x0, g.x1));
      writeSpec({xr: [sig7(Math.min(a, b)), sig7(Math.max(a, b))], yr: undefined});
    } else if (g.kind === 'box' && Math.abs(g.x1 - g.x0) > 4) {
      const a = view.sx.inv(Math.min(g.x0, g.x1)), b = view.sx.inv(Math.max(g.x0, g.x1));
      const patch = {xr: [sig7(Math.min(a, b)), sig7(Math.max(a, b))], yr: undefined};
      if (Math.abs(g.y1 - g.y0) > 8) {
        const y0 = view.sy.inv(g.y0), y1 = view.sy.inv(g.y1);
        patch.yr = [sig7(Math.min(y0, y1)), sig7(Math.max(y0, y1))];
      }
      writeSpec(patch);
    }
    scheduleRender();
  };
  canvas.addEventListener('pointerup', endGesture);
  canvas.addEventListener('pointercancel', endGesture);
  canvas.addEventListener('pointerleave', () => { if (!gesture) { hover = null; scheduleRender(); } });

  canvas.addEventListener('wheel', (e) => {
    if (!view) return;
    e.preventDefault();
    const [px, py] = pos(e);
    const unit = e.deltaMode === 1 ? 3 : e.deltaMode === 2 ? 1 / 3 : 100;
    const notches = Math.max(-4, Math.min(4, -e.deltaY / unit));
    if (!notches) return;
    const factor = Math.pow(WHEEL_STEP, notches);
    if (px < view.plot.left) {
      const yr = zoomDomain(view.sy, py, factor);
      writeSpec({yr: [sig7(Math.min(yr[0], yr[1])), sig7(Math.max(yr[0], yr[1]))]});
    } else {
      const xr = zoomDomain(view.sx, Math.max(view.plot.left, Math.min(view.plot.right, px)), factor);
      writeSpec({xr: [sig7(xr[0]), sig7(xr[1])]});
    }
  }, {passive: false});

  canvas.addEventListener('dblclick', (e) => {
    e.preventDefault();
    writeSpec({xr: undefined, yr: undefined});
  });

  canvas.addEventListener('keydown', (e) => {
    if (!view || e.altKey || e.ctrlKey || e.metaKey) return;
    const cx = (view.plot.left + view.plot.right) / 2;
    let handled = true;
    if (e.key === '+' || e.key === '=') {
      const xr = zoomDomain(view.sx, cx, 1.5);
      writeSpec({xr: [sig7(xr[0]), sig7(xr[1])]});
    } else if (e.key === '-' || e.key === '_') {
      const xr = zoomDomain(view.sx, cx, 1 / 1.5);
      writeSpec({xr: [sig7(xr[0]), sig7(xr[1])]});
    } else if (e.key === '0') {
      writeSpec({xr: undefined, yr: undefined});
    } else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      const xr = panDomain(view.sx, (e.key === 'ArrowLeft' ? 1 : -1) * view.plot.width * 0.1);
      writeSpec({xr: [sig7(xr[0]), sig7(xr[1])]});
    } else if (e.key === 'p' || e.key === 'P') {
      doPin();
      refreshToolbar();
    } else {
      handled = false;
    }
    if (handled) {
      e.preventDefault();
      e.stopPropagation();
    }
  });

  // ---------------------------------------------------------------- store wiring
  const offs = [];
  offs.push(store.on(['focus'], () => { loadFocus().catch(reportErr); }));
  offs.push(store.on(['spec'], (changed, s, sources) => {
    if (sources.size === 1 && sources.has(ctx.id)) return;
    syncPinned();
    refreshToolbar();
    scheduleRender();
  }));
  offs.push(store.on(['template', 'av', 'rv'], () => { loadTemplate().catch(reportErr); }));
  offs.push(store.on(['features', 'theme'], () => { refreshPins(); scheduleRender(); }));
  offs.push(store.onEvent('theme:change', () => { refreshPins(); scheduleRender(); }));

  rsP.then(() => { refreshToolbar(); scheduleRender(); }, () => {});
  syncPinned();
  refreshToolbar();
  loadFocus().catch(reportErr);
  loadTemplate().catch(reportErr);

  return {
    resize(w, h, d) {
      lastW = w;
      lastH = h;
      dpr = d || 1;
      scheduleRender();
    },
    onVisible(v) {
      visible = !!v;
      if (visible) scheduleRender();
    },
    destroy() {
      destroyed = true;
      for (const off of offs) off();
      if (!sedMode && linkedFull.get(store) === publishedFull) publishFull(null);
      feats.destroy();
      models.destroy();
      resetBtn.remove();
      root.remove();
    },
    snapshot() {
      return new Promise((resolve) => canvas.toBlob(resolve, 'image/png'));
    },
    stats() {
      const sp = specGet();
      return {
        renderMs,
        ds: recDs, mode: sedMode ? 'sed' : 'spectrum', logx, logy,
        id: focusId, i: focusI, ids: (focusId !== null ? [String(focusId)] : []).concat(lastStats.pinnedShown || []),
        pinned: (sp.pinned || []).slice(), vel: fin(sp.vel) ? sp.vel : 0,
        segments: Object.fromEntries(segmentRanges(focusRec, sp.segments || null).filter((r) => r.label !== null).map((r) => [r.label, r.on])),
        ...lastStats,
      };
    },
  };
}
