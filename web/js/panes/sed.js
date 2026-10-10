// SED pane (ARCHITECTURE.md sections 6.6, 6.8; capability live_sed_viewer): the focused
// source's photometry on log-log axes in F_nu or nu F_nu, upper/lower limits drawn as arrows,
// error bars, models (reddened stellar template scaled to the photometry, a blackbody fit or
// guide curve, `sedcurve:<ds>` model curves) and a fullscreen dialog with a nearest-point
// tooltip. Also exports the model controls (A_V slider, R_V, template, grid, normalization
// bands) that panes/spectrum1d.js reuses, and the pure helpers tested in tests/js/sed.test.mjs.
//
// Legacy references: Brick2221_F1500W_SEDPortal.py lines 881-1079 (log-log, error bars,
// template median ratio at the data points); best_impl.json live_sed_viewer.

import {makeView} from '../plot/axes.js';
import {prepareCanvas, tokensOf, drawAxes, drawPoints, drawFocusRing} from '../plot/canvas2d.js';
import {eventToViewPx} from '../plot/brushui.js';
import {
  nufnu, blackbodyFnu, fluxUnitFactor, sedPoints, filtersMap, interpLinear, resolveKey, shortName,
} from '../data/filters.js';
import {kCurve} from '../data/extinction.js';
import {TemplateSet, scaleTo} from '../data/templates.js';
import {featureList, layoutFeatures, drawFeatures, featureToggles, featureAt} from '../spec/features.js';

export const meta = {
  type: 'sed',
  caps: ['live_sed_viewer', 'av_slider', 'model_template_overlay', 'labeled_features_overlay'],
  needs: ['catalog'],
};

export const UNITS = Object.freeze({
  fnu: {key: 'fnu', label: 'Fν (Jy)', short: 'Fν'},
  nufnu: {key: 'nufnu', label: 'νFν (W m⁻²)', short: 'νFν'},
});

export const BB_TMIN = 300;
export const BB_TMAX = 30000;

// ------------------------------------------------------------------ pure helpers

const fin = (v) => typeof v === 'number' && Number.isFinite(v);

/** Normalise a units option: 'nufnu' (also 'nuFnu', 'nu_fnu') or 'fnu'. */
export function normUnits(u) {
  const s = String(u || '').toLowerCase().replace(/[^a-z]/g, '');
  return s === 'nufnu' ? 'nufnu' : 'fnu';
}

/** F_nu (Jy) at wl (micron) in display units: Jy, or nu F_nu in W m^-2. NaN when invalid. */
export function convertFlux(wlUm, fnuJy, units) {
  if (!fin(wlUm) || !fin(fnuJy)) return NaN;
  return normUnits(units) === 'nufnu' ? nufnu(wlUm, fnuJy, 'W/m2') : fnuJy;
}

/** Factor converting a wavelength unit to micron (null/'' = micron). Unknown units throw. */
export function waveFactor(unit) {
  if (unit === null || unit === undefined || unit === '') return 1;
  const u = String(unit).trim();
  const table = {
    um: 1, micron: 1, microns: 1, 'µm': 1, 'μm': 1, um_: 1,
    nm: 1e-3, A: 1e-4, AA: 1e-4, Angstrom: 1e-4, angstrom: 1e-4, 'Å': 1e-4,
    mm: 1e3, cm: 1e4, m: 1e6,
  };
  if (Object.prototype.hasOwnProperty.call(table, u)) return table[u];
  throw new RangeError(`unknown wavelength unit ${JSON.stringify(unit)}`);
}

/**
 * Plot series of SED points (data/filters.js sedPoints output) in display units:
 * [{x, y, ex, ey, limit, kind, key, filter, label, fnu, err, wl}]. ex is half the filter's
 * effective width (weff_um / 2) when the filters document gives it; ey scales the F_nu error
 * by the same factor as the value. Limits: 'ul' -> 'u' (arrow down), 'll' -> 'l' (arrow up),
 * without error bars. Points whose value is not finite and > 0 are skipped (log axis).
 */
export function sedSeries(points, units = 'fnu', filters = null) {
  const fl = filters ? filtersMap(filters) : {};
  const out = [];
  for (const p of points || []) {
    if (!p) continue;
    const y = convertFlux(p.wl, p.fnu, units);
    if (!(y > 0) || !Number.isFinite(y)) continue;
    const f = Object.prototype.hasOwnProperty.call(fl, p.filter) ? fl[p.filter] : null;
    const weff = f && fin(f.weff_um) && f.weff_um > 0 ? f.weff_um : null;
    const limit = p.kind === 'ul' ? 'u' : p.kind === 'll' ? 'l' : null;
    let ey = null;
    if (!limit && fin(p.err) && p.err >= 0) ey = p.err * (y / p.fnu);
    out.push({
      x: p.wl, y, ex: weff !== null ? weff / 2 : null, ey, limit, kind: p.kind || 'det',
      key: p.key, filter: p.filter ?? null,
      label: f && f.label ? f.label : p.filter ? shortName(p.filter) : String(p.key ?? ''),
      fnu: p.fnu, err: fin(p.err) ? p.err : null, wl: p.wl,
    });
  }
  return out;
}

function padLog(lo, hi, dex) {
  return [lo / Math.pow(10, dex), hi * Math.pow(10, dex)];
}

/**
 * Log-log domains {x: [x0, x1], y: [y0, y1]} for series (with error bars) and optional model
 * curves [{x, y}] used only when there are no points. Padding: 0.08 dex in x and 0.25 dex in
 * y beyond the data; a single point gets +-0.5 dex in x and +-1 dex in y. With nothing finite
 * the x domain is [0.5, 30] micron and y [1e-6, 1] (fnu) or [1e-20, 1e-12] (nufnu).
 */
export function sedExtent(series, curves = [], units = 'fnu') {
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  const addX = (v) => { if (v > 0 && Number.isFinite(v)) { if (v < x0) x0 = v; if (v > x1) x1 = v; } };
  const addY = (v) => { if (v > 0 && Number.isFinite(v)) { if (v < y0) y0 = v; if (v > y1) y1 = v; } };
  let n = 0;
  for (const p of series || []) {
    if (!(p.x > 0) || !(p.y > 0) || !Number.isFinite(p.x) || !Number.isFinite(p.y)) continue;
    n++;
    addX(p.x);
    addY(p.y);
    if (fin(p.ex)) { addX(p.x - p.ex); addX(p.x + p.ex); }
    if (fin(p.ey)) { addY(p.y + p.ey); if (p.y - p.ey > p.y * 0.05) addY(p.y - p.ey); }
  }
  if (!n) {
    for (const c of curves || []) {
      const m = c && c.x ? c.x.length : 0;
      for (let i = 0; i < m; i++) {
        if (c.x[i] > 0 && c.y[i] > 0 && Number.isFinite(c.y[i])) { addX(c.x[i]); addY(c.y[i]); }
      }
    }
  }
  if (!(x1 >= x0)) {
    return {x: [0.5, 30], y: normUnits(units) === 'nufnu' ? [1e-20, 1e-12] : [1e-6, 1]};
  }
  let X, Y;
  if (x1 / x0 < 1.0001) X = padLog(x0, x1, 0.5);
  else X = padLog(x0, x1, 0.08);
  if (!(y1 >= y0)) Y = normUnits(units) === 'nufnu' ? [1e-20, 1e-12] : [1e-6, 1];
  else if (y1 / y0 < 1.0001) Y = padLog(y0, y1, 1);
  else Y = padLog(y0, y1, 0.25);
  return {x: X, y: Y};
}

/** Index of the series point nearest to pixel (px, py) within maxPx; null when none. */
export function nearestPoint(series, view, px, py, maxPx = 24) {
  let best = null, bd = maxPx * maxPx;
  for (let i = 0; i < (series ? series.length : 0); i++) {
    const p = series[i];
    const qx = view.sx.f(p.x), qy = view.sy.f(p.y);
    if (!Number.isFinite(qx) || !Number.isFinite(qy)) continue;
    const d = (qx - px) ** 2 + (qy - py) ** 2;
    if (d <= bd) { bd = d; best = i; }
  }
  return best === null ? null : {index: best, point: series[best], distPx: Math.sqrt(bd)};
}

/** n log-spaced samples from lo to hi (Float64Array; empty for invalid input). */
export function logGrid(lo, hi, n = 200) {
  if (!(lo > 0) || !(hi > lo) || !Number.isFinite(hi) || !(n >= 2)) return new Float64Array(0);
  const out = new Float64Array(n);
  const a = Math.log10(lo), b = Math.log10(hi);
  for (let i = 0; i < n; i++) out[i] = Math.pow(10, a + ((b - a) * i) / (n - 1));
  return out;
}

/** A(lambda)/A(V) interpolator from a {w, k} curve; NaN and out-of-range give 0 (no extinction). */
export function kInterp(law) {
  if (!law || !law.w || !law.w.length) return () => 0;
  return (wl) => {
    const k = interpLinear(wl, law.w, law.k);
    return Number.isFinite(k) ? k : 0;
  };
}

function bbFitPoints(points) {
  const out = [];
  for (const p of points || []) {
    if ((p.kind ?? 'det') !== 'det') continue;
    if (!(p.fnu > 0) || !(p.wl > 0) || !Number.isFinite(p.fnu) || !Number.isFinite(p.wl)) continue;
    // error in dex, floored at 0.02 dex so one tiny error bar does not dominate
    let s = 0.05;
    if (fin(p.err) && p.err > 0) s = p.err / (p.fnu * Math.LN10);
    out.push({wl: p.wl, lf: Math.log10(p.fnu), w: 1 / Math.max(0.02, s) ** 2});
  }
  return out;
}

function bbChi(pts, T, red) {
  let sw = 0, swr = 0;
  const r = new Array(pts.length);
  for (let i = 0; i < pts.length; i++) {
    const b = blackbodyFnu(pts[i].wl, T) * red[i];
    r[i] = pts[i].lf - Math.log10(b);
    if (!Number.isFinite(r[i])) return {chi2: Infinity, logA: NaN};
    sw += pts[i].w;
    swr += pts[i].w * r[i];
  }
  const logA = swr / sw;
  let chi2 = 0;
  for (let i = 0; i < pts.length; i++) chi2 += pts[i].w * (r[i] - logA) ** 2;
  return {chi2, logA};
}

/**
 * Amplitude of a blackbody at temperature T through extinction (A_V `av`, k(wl) from `kAt`)
 * matched to the detections: weighted mean of log10(fnu / model). Null with no detection.
 */
export function bbAmplitude(points, T, opts = {}) {
  const pts = bbFitPoints(points);
  if (!pts.length || !(T > 0)) return null;
  const kAt = opts.kAt || (() => 0);
  const av = fin(opts.av) ? opts.av : 0;
  const red = pts.map((p) => Math.pow(10, -0.4 * kAt(p.wl) * av));
  const {chi2, logA} = bbChi(pts, T, red);
  if (!Number.isFinite(logA)) return null;
  return {T, amp: Math.pow(10, logA), chi2, n: pts.length};
}

/**
 * Fit a reddened blackbody to the detections: grid search in log T over [tmin, tmax] then a
 * golden-section refinement; amplitude analytic in log space. Returns {T, amp, chi2, n, edge}
 * (edge: the best T sits at a grid end) or null with fewer than 2 detections.
 */
export function fitBlackbody(points, opts = {}) {
  const pts = bbFitPoints(points);
  if (pts.length < 2) return null;
  const tmin = opts.tmin ?? BB_TMIN, tmax = opts.tmax ?? BB_TMAX;
  const kAt = opts.kAt || (() => 0);
  const av = fin(opts.av) ? opts.av : 0;
  const red = pts.map((p) => Math.pow(10, -0.4 * kAt(p.wl) * av));
  const L0 = Math.log10(tmin), L1 = Math.log10(tmax);
  const steps = 160;
  let bi = -1, bc = Infinity;
  const chiAt = (lt) => bbChi(pts, Math.pow(10, lt), red).chi2;
  for (let i = 0; i <= steps; i++) {
    const c = chiAt(L0 + ((L1 - L0) * i) / steps);
    if (c < bc) { bc = c; bi = i; }
  }
  if (bi < 0) return null;
  const h = (L1 - L0) / steps;
  let a = Math.max(L0, L0 + (bi - 1) * h), b = Math.min(L1, L0 + (bi + 1) * h);
  const g = (Math.sqrt(5) - 1) / 2;
  let c = b - g * (b - a), d = a + g * (b - a);
  let fc = chiAt(c), fd = chiAt(d);
  for (let it = 0; it < 60 && b - a > 1e-7; it++) {
    if (fc < fd) { b = d; d = c; fd = fc; c = b - g * (b - a); fc = chiAt(c); }
    else { a = c; c = d; fc = fd; d = a + g * (b - a); fd = chiAt(d); }
  }
  const lt = (a + b) / 2;
  const T = Math.pow(10, lt);
  const r = bbChi(pts, T, red);
  return {T, amp: Math.pow(10, r.logA), chi2: r.chi2, n: pts.length, edge: bi === 0 || bi === steps};
}

/** Blackbody model curve {x, y} in display units on wavelength grid `wave` (micron). */
export function bbCurve(T, amp, wave, opts = {}) {
  const units = opts.units || 'fnu';
  const kAt = opts.kAt || (() => 0);
  const av = fin(opts.av) ? opts.av : 0;
  const n = wave ? wave.length : 0;
  const y = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    const f = amp * blackbodyFnu(wave[i], T) * Math.pow(10, -0.4 * kAt(wave[i]) * av);
    y[i] = convertFlux(wave[i], f, units);
  }
  return {x: Float64Array.from(wave || []), y};
}

/**
 * Model curves of a sedcurve record (pks1 with one or more flux channels) in display units:
 * [{name, x, y}]. Channels with an unknown flux unit are skipped and listed in `skipped`.
 */
export function sedcurveSeries(rec, units = 'fnu') {
  const out = [];
  const skipped = [];
  if (!rec || !rec.wave || !rec.channels) return Object.assign(out, {skipped});
  let wf;
  try {
    wf = waveFactor(rec.unitW);
  } catch (err) {
    if (!(err instanceof RangeError)) throw err;
    return Object.assign(out, {skipped: Object.keys(rec.channels)});
  }
  const n = rec.wave.length;
  for (const [name, ch] of Object.entries(rec.channels)) {
    if (/(^err|_err$|^e_|unc|sigma)/i.test(name)) continue;
    let fac;
    try {
      fac = fluxUnitFactor((rec.units || {})[name]);
    } catch (err) {
      if (!(err instanceof RangeError)) throw err;
      skipped.push(name);
      continue;
    }
    const x = new Float64Array(n), y = new Float64Array(n);
    for (let i = 0; i < n; i++) {
      x[i] = rec.wave[i] * wf;
      const v = ch[i] * fac;
      y[i] = Number.isFinite(v) && v > 0 ? convertFlux(x[i], v, units) : NaN;
    }
    out.push({name, x, y});
  }
  return Object.assign(out, {skipped});
}

/**
 * Filter keys for normalization band names (filter keys, labels or short names) resolved in
 * the filters document; names that do not resolve are kept as given (they may be column keys).
 */
export function resolveBands(bands, filters) {
  if (!Array.isArray(bands) || !bands.length) return null;
  const fl = filters ? filtersMap(filters) : null;
  const out = [];
  for (const b of bands) {
    const s = String(b).trim();
    if (!s) continue;
    const k = fl ? resolveKey(s, fl) : null;
    out.push(k || s);
    if (k && k !== s) out.push(s);
  }
  return out.length ? out : null;
}

/**
 * Template scale factor: a positive number `scale` is used as is; 'auto' (or anything else)
 * gives scaleTo(model, points, bands). NaN when nothing is usable.
 */
export function templateScaleFor(model, points, tpl, filters) {
  if (tpl && typeof tpl.scale === 'number' && tpl.scale > 0 && Number.isFinite(tpl.scale)) return tpl.scale;
  return scaleTo(model, points, resolveBands(tpl && tpl.bands, filters));
}

// ------------------------------------------------------------------ shared pane plumbing

/** page.datasets of a pane context. */
export function pageDatasets(ctx) {
  return (ctx && ctx.app && ctx.app.page && ctx.app.page.datasets) || {};
}

/** First dataset id of `kind` (or `preferred` when it exists with that kind). */
export function datasetOfKind(ctx, kind, preferred = null) {
  const ds = pageDatasets(ctx);
  if (preferred && ds[preferred] && (!kind || ds[preferred].kind === kind)) return preferred;
  for (const [id, r] of Object.entries(ds)) if (r && r.kind === kind) return id;
  return null;
}

const docCache = new WeakMap(); // hub -> Map(ds -> Promise<doc|null>)

/** The JSON document of the first dataset of `kind` through the hub; null when absent. */
export function loadDoc(ctx, kind, preferred = null) {
  const ds = datasetOfKind(ctx, kind, preferred);
  if (!ds || !ctx.hub || typeof ctx.hub.json !== 'function') return Promise.resolve(null);
  let m = docCache.get(ctx.hub);
  if (!m) { m = new Map(); docCache.set(ctx.hub, m); }
  if (!m.has(ds)) {
    const p = Promise.resolve(ctx.hub.json(ds));
    m.set(ds, p);
    p.catch(() => m.delete(ds));
  }
  return m.get(ds);
}

/** URL of a dataset resolved through the hub (for documents with relative inner URLs). */
export function datasetURL(ctx, ds) {
  const r = pageDatasets(ctx)[ds];
  if (!r || !r.url) return null;
  if (ctx.hub && typeof ctx.hub.resolve === 'function') return ctx.hub.resolve(r.url).toString();
  return new URL(r.url, typeof document !== 'undefined' ? document.baseURI : 'http://localhost/').toString();
}

const tplCache = new WeakMap(); // hub -> Map(url -> Promise<TemplateSet>)

/** TemplateSet of the templates dataset (`preferred` first); null when there is none. */
export function templateSetFor(ctx, preferred = null) {
  const ds = datasetOfKind(ctx, 'templates', preferred);
  if (!ds) return Promise.resolve(null);
  const url = datasetURL(ctx, ds);
  if (!url) return Promise.resolve(null);
  const key = ctx.hub || ctx;
  let m = tplCache.get(key);
  if (!m) { m = new Map(); tplCache.set(key, m); }
  if (!m.has(url)) {
    const p = TemplateSet.open(url);
    m.set(url, p);
    p.catch(() => m.delete(url));
  }
  return m.get(url);
}

/** Template grid for a record meta (NIRSpec grating) and the grids available. */
export function gridForGrating(grating, grids) {
  const names = grids || [];
  const g = String(grating || '').toUpperCase();
  let want = null;
  if (g.includes('PRISM') || g.includes('CLEAR')) want = 'prism';
  else if (/\d{3}H\b|H$/.test(g)) want = 'high';
  else if (/\d{3}M\b|M$/.test(g)) want = 'medium';
  if (want && names.includes(want)) return want;
  if (names.includes('prism')) return 'prism';
  return names[0] || null;
}

/** requestAnimationFrame with a timer fallback (node, hidden tabs). */
export function raf(fn) {
  if (typeof requestAnimationFrame === 'function') return requestAnimationFrame(fn);
  return setTimeout(fn, 16);
}

/** Focus row index in dataset `ds` ({i} or null), resolving ids through the app. */
export async function focusIndex(ctx, ds, focus) {
  if (!focus) return null;
  if (focus.ds === ds && Number.isInteger(focus.i) && focus.i >= 0) return focus.i;
  if (focus.id === null || focus.id === undefined) return null;
  if (!ctx.app || typeof ctx.app.resolveIds !== 'function') return null;
  const [i] = await ctx.app.resolveIds(ds, [String(focus.id)]);
  return Number.isInteger(i) && i >= 0 ? i : null;
}

function el(tag, cls, attrs) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (attrs) for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
}

function fmtNum(v, digits = 3) {
  if (!Number.isFinite(v)) return '—';
  const a = Math.abs(v);
  if (a !== 0 && (a < 1e-3 || a >= 1e5)) return v.toExponential(digits - 1);
  return Number(v.toPrecision(digits)).toString();
}

/**
 * Model controls bound to the store keys av, rv and template: an A_V slider (rAF-throttled)
 * with a number box, R_V select (3.1 / 5.5), and when `templates` (Promise<TemplateSet|null>)
 * is given a template select, grid select, normalization bands and scale. Returns
 * {el, update(), destroy()}.
 */
export function modelControls(ctx, opts = {}) {
  const store = ctx.store;
  const source = opts.source || ctx.id;
  const root = el('div', 'pk-models', {role: 'group', 'aria-label': 'Extinction and model template'});
  const avLab = el('label', 'pk-models-av');
  avLab.innerHTML = '<span>A<sub>V</sub></span>';
  const avRange = el('input', null, {type: 'range', min: '0', max: String(opts.avMax ?? 60), step: '0.1', 'aria-label': 'A_V (mag)'});
  const avNum = el('input', 'pk-models-num', {type: 'number', min: '0', step: '0.1', inputmode: 'decimal', 'aria-label': 'A_V value'});
  avLab.append(avRange, avNum);
  const rvLab = el('label', 'pk-models-rv');
  rvLab.innerHTML = '<span>R<sub>V</sub></span>';
  const rvSel = el('select', null, {'aria-label': 'R_V'});
  rvLab.append(rvSel);
  root.append(avLab, rvLab);

  let pendingAv = null;
  const flushAv = () => {
    if (pendingAv === null) return;
    const v = pendingAv;
    pendingAv = null;
    store.set({av: v}, {source});
  };
  const setAv = (v) => {
    if (!Number.isFinite(v) || v < 0) return;
    const r = Math.round(v * 1000) / 1000;
    const schedule = pendingAv === null;
    pendingAv = r;
    if (schedule) raf(flushAv);
  };
  avRange.addEventListener('input', () => { avNum.value = avRange.value; setAv(Number(avRange.value)); });
  avNum.addEventListener('change', () => setAv(Number(avNum.value)));
  rvSel.addEventListener('change', () => store.set({rv: Number(rvSel.value)}, {source}));

  let tplSet = null;
  let tplSel = null, gridSel = null, bandsIn = null, scaleIn = null;
  if (opts.templates) {
    const tLab = el('label', 'pk-models-tpl');
    tLab.innerHTML = '<span>Template</span>';
    tplSel = el('select', null, {'aria-label': 'Model template'});
    tLab.append(tplSel);
    const gLab = el('label', 'pk-models-grid');
    gLab.innerHTML = '<span>Grid</span>';
    gridSel = el('select', null, {'aria-label': 'Template wavelength grid'});
    gLab.append(gridSel);
    const bLab = el('label', 'pk-models-bands');
    bLab.innerHTML = '<span>Norm</span>';
    bandsIn = el('input', null, {type: 'text', placeholder: opts.bandsPlaceholder || 'auto', size: '10',
      'aria-label': 'Normalization bands (comma separated filters or band labels; empty = automatic)'});
    bLab.append(bandsIn);
    const sLab = el('label', 'pk-models-scale');
    sLab.innerHTML = '<span>Scale</span>';
    scaleIn = el('input', null, {type: 'text', placeholder: 'auto', size: '6', 'aria-label': 'Template scale (auto or a positive number)'});
    sLab.append(scaleIn);
    root.append(tLab, gLab, bLab, sLab);
    const cur = () => store.get('template') || null;
    const write = (patch) => {
      const t = cur();
      if (!t || !t.key) return;
      const next = {key: t.key};
      const merged = {...t, ...patch};
      if (merged.grid) next.grid = merged.grid;
      next.scale = merged.scale === undefined ? 'auto' : merged.scale;
      if (Array.isArray(merged.bands) && merged.bands.length) next.bands = merged.bands;
      store.set({template: next}, {source});
    };
    tplSel.addEventListener('change', () => {
      const key = tplSel.value;
      if (!key) { store.set({template: null}, {source}); return; }
      const t = cur() || {};
      const next = {key, scale: t.scale ?? 'auto'};
      if (gridSel.value) next.grid = gridSel.value;
      if (Array.isArray(t.bands) && t.bands.length) next.bands = t.bands;
      store.set({template: next}, {source});
    });
    gridSel.addEventListener('change', () => write({grid: gridSel.value || undefined}));
    bandsIn.addEventListener('change', () => {
      const list = bandsIn.value.split(/[,\s]+/).map((s) => s.trim()).filter(Boolean);
      write({bands: list.length ? list : undefined});
    });
    scaleIn.addEventListener('change', () => {
      const s = scaleIn.value.trim();
      const v = Number(s);
      if (!s || s.toLowerCase() === 'auto') write({scale: 'auto'});
      else if (Number.isFinite(v) && v > 0) write({scale: v});
      else scaleIn.value = typeof (cur() || {}).scale === 'number' ? String(cur().scale) : '';
    });
    Promise.resolve(opts.templates).then((ts) => {
      tplSet = ts;
      update();
    }, (err) => {
      root.querySelectorAll('.pk-models-tpl, .pk-models-grid, .pk-models-bands, .pk-models-scale').forEach((n) => { n.hidden = true; });
      console.warn('templates unavailable', err);
    });
  }

  function update() {
    const av = Number(store.get('av')) || 0;
    if (pendingAv === null) {
      if (av > Number(avRange.max)) avRange.max = String(Math.ceil(av * 1.5));
      avRange.value = String(av);
      if (document.activeElement !== avNum) avNum.value = String(av);
    }
    const rv = Number(store.get('rv')) || 3.1;
    const rvs = [3.1, 5.5];
    if (!rvs.includes(rv)) rvs.push(rv);
    rvSel.textContent = '';
    for (const r of rvs.sort((a, b) => a - b)) {
      const o = el('option');
      o.value = String(r);
      o.textContent = String(r);
      rvSel.append(o);
    }
    rvSel.value = String(rv);
    if (!tplSel) return;
    const t = store.get('template') || null;
    const list = tplSet ? tplSet.list() : [];
    const grids = tplSet ? tplSet.gridNames() : [];
    tplSel.textContent = '';
    const none = el('option');
    none.value = '';
    none.textContent = tplSet ? 'none' : 'loading…';
    tplSel.append(none);
    for (const e of list) {
      const o = el('option');
      o.value = e.key;
      o.textContent = e.label || e.key;
      tplSel.append(o);
    }
    if (t && t.key && !list.some((e) => e.key === t.key)) {
      const o = el('option');
      o.value = t.key;
      o.textContent = t.key;
      tplSel.append(o);
    }
    tplSel.value = t && t.key ? t.key : '';
    gridSel.textContent = '';
    const auto = el('option');
    auto.value = '';
    auto.textContent = 'auto';
    gridSel.append(auto);
    for (const g of grids) {
      const o = el('option');
      o.value = g;
      o.textContent = g;
      gridSel.append(o);
    }
    gridSel.value = t && t.grid && grids.includes(t.grid) ? t.grid : '';
    gridSel.disabled = !t;
    bandsIn.disabled = !t;
    scaleIn.disabled = !t;
    if (document.activeElement !== bandsIn) bandsIn.value = t && Array.isArray(t.bands) ? t.bands.join(',') : '';
    if (document.activeElement !== scaleIn) scaleIn.value = t && typeof t.scale === 'number' ? String(t.scale) : '';
  }
  const off = store.on(['av', 'rv', 'template'], (changed, s, sources) => {
    if (sources && sources.size === 1 && sources.has(source) && changed && changed.has && changed.has('av') && changed.size === 1) return;
    update();
  });
  update();
  return {
    el: root,
    update,
    destroy() {
      off();
      root.remove();
    },
  };
}

// ------------------------------------------------------------------ drawing

const LIMIT_COLOR_ALPHA = 0.9;

/** Draw one SED frame (shared by the pane canvas and the dialog canvas). Returns the view. */
function drawSED(canvas, cssW, cssH, dpr, st, tokens, opts = {}) {
  const ctx = prepareCanvas(canvas, cssW, cssH, dpr);
  const tk = tokensOf(tokens);
  const units = st.units;
  const big = !!opts.big;
  const view = makeView({
    width: cssW, height: cssH, dpr,
    margin: {l: big ? 70 : 60, r: 12, t: 10, b: 40},
    x: {kind: 'log', d0: st.xr[0], d1: st.xr[1]},
    y: {kind: 'log', d0: st.yr[0], d1: st.yr[1]},
  });
  drawAxes(ctx, view, {
    tokens: tk, xLabel: 'λ (µm)', yLabel: UNITS[units].label,
    fontSize: big ? 13 : 11,
  });
  const P = view.plot;
  // features (lines under the data)
  if (st.features && st.features.length) {
    st.featLayout = layoutFeatures(st.features, view, {
      rows: 2, maxLabels: big ? 40 : 24, fontSize: big ? 11 : 10,
      measure: (t) => { ctx.font = `${big ? 11 : 10}px ${tk.font}`; return ctx.measureText(t).width; },
    });
    drawFeatures(ctx, view, st.featLayout, {tokens: tk, alpha: 0.35});
  } else {
    st.featLayout = null;
  }
  ctx.save();
  ctx.beginPath();
  ctx.rect(P.left, P.top, P.width, P.height);
  ctx.clip();
  const curve = (c, color, width, dash, alpha) => {
    ctx.globalAlpha = alpha;
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.setLineDash(dash || []);
    ctx.beginPath();
    let pen = false;
    for (let i = 0; i < c.x.length; i++) {
      const px = view.sx.f(c.x[i]), py = view.sy.f(c.y[i]);
      if (!Number.isFinite(px) || !Number.isFinite(py)) { pen = false; continue; }
      if (pen) ctx.lineTo(px, py);
      else { ctx.moveTo(px, py); pen = true; }
    }
    ctx.stroke();
    ctx.setLineDash([]);
  };
  const cat = tk.cat || [];
  for (const c of st.sedcurves || []) curve(c, tk.fg3, 1, null, 0.5);
  if (st.bb && st.bb.curve) curve(st.bb.curve, cat[3] || '#c69026', 1.5, [6, 3], 0.9);
  if (st.tpl && st.tpl.curve) curve(st.tpl.curve, cat[1] || '#e5534b', 1.5, [5, 3], 0.9);
  ctx.restore();
  const pts = st.series.map((p, i) => ({
    x: p.x, y: p.y, ex: p.ex, ey: p.ey, limit: p.limit,
    color: p.limit ? tk.fg2 : (cat[0] || tk.neutral), size: big ? 8 : 7,
    alpha: p.limit ? LIMIT_COLOR_ALPHA : 1,
    label: opts.labels ? p.label : null,
    shape: i === st.hoverIndex ? 'diamond' : 'circle',
  }));
  drawPoints(ctx, view, pts, {tokens: tk, font: `${big ? 12 : 10}px ${tk.font}`});
  if (st.hoverIndex !== null && st.hoverIndex !== undefined && st.series[st.hoverIndex]) {
    const p = st.series[st.hoverIndex];
    drawFocusRing(ctx, view, p.x, p.y, {tokens: tk, hover: true});
  }
  // legend
  const leg = [];
  if (st.series.length) leg.push([cat[0] || tk.neutral, `${st.series.length} points`, null]);
  if (st.tpl && st.tpl.curve) leg.push([cat[1] || '#e5534b', `${st.tpl.label} ×${fmtNum(st.tpl.scale)}`, [5, 3]]);
  if (st.bb && st.bb.curve) leg.push([cat[3] || '#c69026', `BB ${Math.round(st.bb.T)} K${st.bb.fit ? ' (fit)' : ''}`, [6, 3]]);
  if (st.sedcurves && st.sedcurves.length) leg.push([tk.fg3, `${st.sedcurves.length} model curve${st.sedcurves.length === 1 ? '' : 's'}`, null]);
  if (leg.length) {
    ctx.save();
    const fs = big ? 12 : 10;
    ctx.font = `${fs}px ${tk.font}`;
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'left';
    let y = P.bottom - 8 - (leg.length - 1) * (fs + 4);
    const w = Math.max(...leg.map((l) => ctx.measureText(l[1]).width)) + 30;
    const x = P.right - w - 6;
    ctx.globalAlpha = 0.85;
    ctx.fillStyle = tk.plotBg;
    ctx.fillRect(x - 4, y - fs / 2 - 4, w + 8, leg.length * (fs + 4) + 4);
    ctx.globalAlpha = 1;
    for (const [color, text, dash] of leg) {
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5;
      ctx.setLineDash(dash || []);
      ctx.beginPath();
      ctx.moveTo(x, y);
      ctx.lineTo(x + 20, y);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = tk.fg2;
      ctx.fillText(text, x + 26, y);
      y += fs + 4;
    }
    ctx.restore();
  }
  if (opts.message) {
    ctx.save();
    ctx.fillStyle = tk.fg3;
    ctx.font = `${big ? 14 : 12}px ${tk.font}`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(opts.message, (P.left + P.right) / 2, (P.top + P.bottom) / 2);
    ctx.restore();
  }
  return view;
}

function tooltipHTML(p, units) {
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const kind = p.kind === 'ul' ? ' (upper limit)' : p.kind === 'll' ? ' (lower limit)' : '';
  let s = `<strong>${esc(p.label)}</strong>${kind}<br>λ = ${fmtNum(p.wl, 4)} µm<br>`;
  s += `Fν = ${fmtNum(p.fnu, 4)} Jy`;
  if (p.err !== null && p.err !== undefined && !p.limit) s += ` ± ${fmtNum(p.err, 2)}`;
  if (units === 'nufnu') s += `<br>νFν = ${fmtNum(p.y, 4)} W m⁻²`;
  if (p.key) s += `<br><span class="pk-muted">${esc(p.key)}</span>`;
  return s;
}

// ------------------------------------------------------------------ pane

export function mount(host, ctx) {
  const cfg = ctx.config || {};
  const store = ctx.store;
  const page = pageDatasets(ctx);
  let ds = ctx.dataset;
  if (ds && page[ds] && page[ds].kind !== 'catalog' && page[ds].catalog) ds = page[ds].catalog;
  if (!ds || !page[ds]) ds = (ctx.app && ctx.app.primaryDs) || datasetOfKind(ctx, 'catalog');
  const models = Array.isArray(cfg.models) ? cfg.models.map(String) : ['template', 'blackbody'];
  const wantTpl = models.includes('template');
  const wantBB = models.includes('blackbody');
  const curveSets = models.filter((m) => m.startsWith('sedcurve:')).map((m) => m.slice(9)).filter(Boolean);

  const st = {
    units: normUnits(cfg.units || 'fnu'),
    i: null, id: null, row: null, points: [], series: [],
    xr: [0.5, 30], yr: [1e-6, 1], tpl: null, bb: null, bbUser: null, sedcurves: [], features: [],
    hoverIndex: null, featLayout: null, message: 'Select a source', renderMs: 0,
    sedcurveRaw: [], skipped: [],
  };
  let visible = true;
  let destroyed = false;
  let seq = 0;
  let filtersDoc = null, extDoc = null, featDoc = null, client = null;
  let tplSet = null;
  const curveStores = new Map();

  // ---- DOM
  const root = el('div', 'pk-sed');
  const bar = el('div', 'pk-sed-bar');
  const unitsGroup = el('div', 'pk-btn-group pk-sed-units', {role: 'group', 'aria-label': 'Flux units'});
  const unitBtns = {};
  for (const u of ['fnu', 'nufnu']) {
    const b = el('button', 'pk-btn', {type: 'button'});
    b.textContent = UNITS[u].short;
    b.title = UNITS[u].label;
    b.addEventListener('click', () => { st.units = u; refreshUnits(); recompute(); });
    unitBtns[u] = b;
    unitsGroup.append(b);
  }
  bar.append(unitsGroup);
  let bbIn = null;
  if (wantBB) {
    const lab = el('label', 'pk-sed-bb');
    lab.innerHTML = '<span>BB T</span>';
    bbIn = el('input', null, {type: 'text', inputmode: 'numeric', placeholder: 'fit', size: '6', 'aria-label': 'Blackbody temperature in K (empty = fit)'});
    bbIn.addEventListener('change', () => {
      const v = Number(bbIn.value.trim());
      st.bbUser = bbIn.value.trim() && Number.isFinite(v) && v > 0 ? v : null;
      if (!st.bbUser) bbIn.value = '';
      recompute();
    });
    lab.append(bbIn);
    bar.append(lab);
  }
  const controls = modelControls(ctx, {templates: wantTpl ? (tplSetP()) : null});
  bar.append(controls.el);
  const feats = featureToggles(store, () => featDoc, {source: ctx.id});
  bar.append(feats.el);
  const wrap = el('div', 'pk-sed-wrap');
  const canvas = el('canvas', 'pk-sed-canvas', {tabindex: '0', role: 'img', 'aria-label': 'Spectral energy distribution'});
  const tip = el('div', 'pk-sed-tip', {role: 'status'});
  tip.hidden = true;
  wrap.append(canvas, tip);
  root.append(bar, wrap);
  host.append(root);

  let openBtn = null, dialog = null, dCanvas = null, dTip = null, dView = null, dRo = null;
  if (cfg.dialog !== false) {
    openBtn = el('button', 'pk-icon-btn pk-sed-open', {type: 'button', title: 'Open the SED full screen', 'aria-label': 'Open the SED full screen'});
    openBtn.textContent = '⤢';
    openBtn.addEventListener('click', openDialog);
    if (ctx.tools) ctx.tools.append(openBtn);
    else bar.append(openBtn);
  }

  function tplSetP() {
    // the templates dataset is discovered on the page (sed declares no templates option, §6.10)
    return templateSetFor(ctx, null).then((ts) => { tplSet = ts; return ts; });
  }

  function refreshUnits() {
    for (const [u, b] of Object.entries(unitBtns)) b.setAttribute('aria-pressed', u === st.units ? 'true' : 'false');
  }
  refreshUnits();

  // ---- dialog
  function openDialog() {
    if (!dialog) {
      dialog = el('dialog', 'pk-dialog pk-sed-dialog', {'aria-label': 'SED'});
      const head = el('div', 'pk-sed-dialog-head');
      const title = el('strong');
      title.className = 'pk-sed-dialog-title';
      const close = el('button', 'pk-icon-btn', {type: 'button', 'aria-label': 'Close'});
      close.textContent = '✕';
      close.addEventListener('click', () => dialog.close());
      head.append(title, close);
      const dw = el('div', 'pk-sed-dialog-wrap');
      dCanvas = el('canvas', 'pk-sed-canvas', {tabindex: '0', role: 'img', 'aria-label': 'Spectral energy distribution (full screen)'});
      dTip = el('div', 'pk-sed-tip', {role: 'status'});
      dTip.hidden = true;
      dw.append(dCanvas, dTip);
      dialog.append(head, dw);
      root.append(dialog);
      bindHover(dCanvas, dTip, () => dView);
      dialog.addEventListener('close', () => { st.hoverIndex = null; render(); });
      if (typeof ResizeObserver === 'function') {
        dRo = new ResizeObserver(() => renderDialog());
        dRo.observe(dw);
      }
    }
    if (typeof dialog.showModal === 'function') {
      if (!dialog.open) dialog.showModal();
    } else {
      dialog.setAttribute('open', '');
    }
    renderDialog();
  }

  function renderDialog() {
    if (!dialog || !dialog.open) return;
    const w = dCanvas.parentElement.clientWidth, h = dCanvas.parentElement.clientHeight;
    if (!(w > 10 && h > 10)) return;
    dialog.querySelector('.pk-sed-dialog-title').textContent = st.id !== null ? `SED — ${st.id}` : 'SED';
    dView = drawSED(dCanvas, w, h, window.devicePixelRatio || 1, st, ctx.theme(), {big: true, labels: true, message: st.series.length ? null : st.message});
  }

  // ---- hover tooltip
  function bindHover(cv, tipEl, getView) {
    const move = (e) => {
      const view = getView();
      if (!view) return;
      const [px, py] = eventToViewPx(cv, view, e);
      const hit = nearestPoint(st.series, view, px, py, e.pointerType === 'touch' ? 36 : 24);
      const idx = hit ? hit.index : null;
      let fHit = null;
      if (!hit && st.featLayout && view.inPlot(px, py)) fHit = featureAt(st.featLayout, px, 4);
      if (hit) {
        tipEl.innerHTML = tooltipHTML(hit.point, st.units);
      } else if (fHit) {
        tipEl.textContent = `${fHit.f.label || fHit.f.name} (${fmtNum(fHit.f.w0, 5)} µm)`;
      }
      tipEl.hidden = !hit && !fHit;
      if (!tipEl.hidden) {
        const W = cv.clientWidth;
        tipEl.style.left = `${Math.min(Math.max(4, px + 12), Math.max(4, W - tipEl.offsetWidth - 4))}px`;
        tipEl.style.top = `${Math.max(4, py - tipEl.offsetHeight - 10)}px`;
      }
      if (idx !== st.hoverIndex) {
        st.hoverIndex = idx;
        render();
        renderDialog();
      }
    };
    const leave = () => {
      tipEl.hidden = true;
      if (st.hoverIndex !== null) { st.hoverIndex = null; render(); renderDialog(); }
    };
    cv.addEventListener('pointermove', move);
    cv.addEventListener('pointerdown', move);
    cv.addEventListener('pointerleave', leave);
  }
  let mainView = null;
  bindHover(canvas, tip, () => mainView);

  // ---- data
  const ready = (async () => {
    const [fd, ed, fe] = await Promise.all([
      loadDoc(ctx, 'filters').catch((err) => { console.warn('filters document unavailable', err); return null; }),
      loadDoc(ctx, 'extinction').catch((err) => { console.warn('extinction document unavailable', err); return null; }),
      loadDoc(ctx, 'features').catch((err) => { console.warn('features document unavailable', err); return null; }),
    ]);
    filtersDoc = fd;
    extDoc = ed;
    featDoc = fe;
    feats.update();
    if (ds && ctx.app && typeof ctx.app.catalog === 'function') client = await ctx.app.catalog(ds);
  })();

  async function loadFocus() {
    const my = ++seq;
    const focus = store.get('focus') || null;
    if (!focus) {
      Object.assign(st, {i: null, id: null, row: null, points: [], sedcurveRaw: [], message: 'Select a source'});
      ctx.setStatus('');
      recompute();
      return;
    }
    try {
      await ready;
    } catch (err) {
      if (my !== seq) return;
      st.message = 'Catalog unavailable';
      ctx.setStatus('error', String(err && err.message ? err.message : err));
      recompute();
      return;
    }
    if (!client) { st.message = 'No catalog'; recompute(); return; }
    ctx.setStatus('loading…');
    const i = await focusIndex(ctx, ds, focus);
    if (my !== seq) return;
    if (i === null) {
      Object.assign(st, {i: null, id: focus.id ?? null, row: null, points: [], sedcurveRaw: [], message: `No row for ${focus.id ?? focus.i} in ${ds}`});
      ctx.setStatus('');
      recompute();
      return;
    }
    // Staleness uses this pane's own seq: DataClient's record token counter is shared by every
    // pane on the same catalog client, so a tokened call here could null another pane's fetch.
    let row;
    try {
      row = await client.record(i);
    } catch (err) {
      if (my !== seq || destroyed) return; // a superseded focus never reports
      if (err && err.name === 'AbortError') throw err;
      console.warn(`catalog row ${i} unavailable`, err);
      Object.assign(st, {i, id: focus.id ?? null, row: null, points: [], sedcurveRaw: [],
        message: `Photometry for ${focus.id ?? i} unavailable`});
      ctx.setStatus('error', String(err && err.message ? err.message : err));
      recompute();
      return;
    }
    if (my !== seq || destroyed) return;
    const id = row && row._id !== undefined ? row._id : focus.id;
    let pts = [];
    try {
      pts = row ? sedPoints(row, filtersDoc || {}, client.manifest.cols || []) : [];
    } catch (err) {
      if (!(err instanceof RangeError)) throw err;
      ctx.setStatus('unit error', err.message);
    }
    const curves = [];
    for (const cds of curveSets) {
      try {
        if (!curveStores.has(cds)) curveStores.set(cds, Promise.resolve(ctx.hub.records(cds)));
        const rs = await curveStores.get(cds);
        if (id !== null && id !== undefined && rs.has(String(id))) {
          const rec = await rs.get(String(id), {channel: `${ctx.id}:sedcurve`});
          if (rec) curves.push(rec);
        }
      } catch (err) {
        if (err && err.name === 'AbortError') return;
        console.warn(`sedcurve ${cds} unavailable`, err);
      }
      if (my !== seq) return;
    }
    Object.assign(st, {i, id, row, points: pts, sedcurveRaw: curves,
      message: pts.length ? null : 'No photometry for this source'});
    ctx.setStatus(`${pts.length} pt${pts.length === 1 ? '' : 's'}`, `${id ?? ''} · ${pts.filter((p) => p.kind !== 'det').length} limits`);
    recompute();
  }

  let tplSeq = 0;
  async function recompute() {
    const units = st.units;
    st.series = sedSeries(st.points, units, filtersDoc);
    const curves = [];
    st.skipped = [];
    for (const rec of st.sedcurveRaw) {
      const cs = sedcurveSeries(rec, units);
      st.skipped.push(...cs.skipped);
      curves.push(...cs);
    }
    st.sedcurves = curves;
    const av = Number(store.get('av')) || 0;
    const rv = Number(store.get('rv')) || 3.1;
    let kAt = () => 0;
    if (extDoc) {
      try {
        kAt = kInterp(kCurve(extDoc, rv));
      } catch (err) {
        if (!(err instanceof RangeError)) throw err;
      }
    }
    const ext = sedExtent(st.series, curves, units);
    st.xr = ext.x;
    st.yr = ext.y;
    // blackbody
    st.bb = null;
    if (wantBB && st.points.length) {
      const fit = st.bbUser ? bbAmplitude(st.points, st.bbUser, {kAt, av}) : fitBlackbody(st.points, {kAt, av});
      if (fit) {
        const grid = logGrid(st.xr[0], st.xr[1], 240);
        st.bb = {T: fit.T, amp: fit.amp, fit: !st.bbUser, edge: !!fit.edge, chi2: fit.chi2,
          curve: bbCurve(fit.T, fit.amp, grid, {units, kAt, av})};
        if (bbIn && !st.bbUser) bbIn.placeholder = `fit ${Math.round(fit.T)}`;
      }
    }
    // features
    const fams = store.get('features') || [];
    st.features = featDoc ? featureList(featDoc, fams, {vel: 0}) : [];
    render();
    renderDialog();
    // template (async)
    const t = store.get('template');
    const my = ++tplSeq;
    if (!wantTpl || !t || !t.key) { st.tpl = null; render(); renderDialog(); return; }
    let ts = tplSet;
    try {
      ts = ts || await tplSetP();
    } catch (err) {
      if (my !== tplSeq) return;
      st.tpl = null;
      ctx.setStatus('templates unavailable', String(err && err.message ? err.message : err));
      return;
    }
    if (!ts || my !== tplSeq) return;
    const grids = ts.gridNames();
    const grid = t.grid && grids.includes(t.grid) ? t.grid : gridForGrating('PRISM', grids);
    let model;
    try {
      model = await ts.reddened(t.key, grid, av, rv);
    } catch (err) {
      if (my !== tplSeq) return;
      st.tpl = null;
      ctx.setStatus('template error', String(err && err.message ? err.message : err));
      render();
      renderDialog();
      return;
    }
    if (my !== tplSeq) return;
    const scale = templateScaleFor(model, st.points, t, filtersDoc);
    const entry = ts.list().find((e) => e.key === t.key);
    const n = model.wave.length;
    const x = new Float64Array(n), y = new Float64Array(n);
    for (let k = 0; k < n; k++) {
      x[k] = model.wave[k];
      const f = model.flux[k] * scale;
      y[k] = f > 0 ? convertFlux(x[k], f, units) : NaN;
    }
    st.tpl = {key: t.key, grid, scale, av, rv, label: entry ? entry.label || entry.key : t.key, curve: {x, y}, n};
    render();
    renderDialog();
  }

  let rafPending = false;
  let lastW = 0, lastH = 0, lastDpr = 1;
  function render() {
    if (!visible || destroyed) return;
    if (rafPending) return;
    rafPending = true;
    raf(() => {
      rafPending = false;
      if (!visible || destroyed) return;
      const w = wrap.clientWidth || lastW, h = wrap.clientHeight || lastH;
      if (!(w > 10 && h > 10)) return;
      const t0 = performance.now();
      mainView = drawSED(canvas, w, h, lastDpr, st, ctx.theme(), {message: st.series.length ? null : st.message});
      st.renderMs = performance.now() - t0;
    });
  }

  const offs = [];
  offs.push(store.on(['focus'], () => { loadFocus().catch(reportErr); }));
  offs.push(store.on(['av', 'rv', 'template', 'features'], () => { recompute().catch(reportErr); }));
  offs.push(store.on(['theme'], () => { render(); renderDialog(); }));
  offs.push(store.onEvent('theme:change', () => { render(); renderDialog(); }));
  function reportErr(err) {
    if (err && err.name === 'AbortError') return;
    ctx.setStatus('error', String(err && err.message ? err.message : err));
    console.error(err);
  }
  ready.then(() => recompute(), reportErr);
  loadFocus().catch(reportErr);

  return {
    resize(w, h, dpr) {
      lastW = w;
      lastH = h;
      lastDpr = dpr || 1;
      render();
    },
    onVisible(v) {
      visible = !!v;
      if (visible) render();
    },
    destroy() {
      destroyed = true;
      for (const off of offs) off();
      controls.destroy();
      feats.destroy();
      if (openBtn) openBtn.remove();
      if (dRo) dRo.disconnect();
      if (dialog && dialog.open) dialog.close();
      root.remove();
    },
    snapshot() {
      return new Promise((resolve) => canvas.toBlob(resolve, 'image/png'));
    },
    stats() {
      return {
        renderMs: st.renderMs,
        ds, i: st.i, id: st.id, units: st.units,
        n: st.series.length,
        points: st.points.map((p) => ({wl: p.wl, fnu: p.fnu, err: p.err, kind: p.kind, key: p.key, filter: p.filter})),
        series: st.series.map((p) => ({x: p.x, y: p.y, limit: p.limit, key: p.key})),
        xr: st.xr.slice(), yr: st.yr.slice(),
        template: st.tpl ? {key: st.tpl.key, grid: st.tpl.grid, scale: st.tpl.scale, av: st.tpl.av, rv: st.tpl.rv, n: st.tpl.n,
          yMedian: medianFinite(st.tpl.curve.y)} : null,
        blackbody: st.bb ? {T: st.bb.T, amp: st.bb.amp, fit: st.bb.fit, edge: st.bb.edge} : null,
        sedcurves: st.sedcurves.map((c) => c.name),
        skippedChannels: st.skipped.slice(),
        features: st.featLayout ? {shown: st.featLayout.shown, labelled: st.featLayout.labelled} : {shown: 0, labelled: 0},
        dialog: !!(dialog && dialog.open),
      };
    },
  };
}

function medianFinite(a) {
  const v = [];
  for (let i = 0; i < (a ? a.length : 0); i++) if (Number.isFinite(a[i])) v.push(a[i]);
  if (!v.length) return null;
  v.sort((x, y) => x - y);
  const h = v.length >> 1;
  return v.length % 2 ? v[h] : (v[h - 1] + v[h]) / 2;
}
