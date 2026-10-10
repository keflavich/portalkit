// Stellar model templates (ARCHITECTURE.md sections 5.10 and 6.6): scaleTo() plus a loader for
// pk-templates/1 sets (shared wavelength grids, pks1 template and G23 law records).
// scaleTo is pure; its Python twin is portalkit/physics/sed.py scale_to
// (tests/golden/physics/scale_to.json, 1e-6 relative).

import {fetchJSON, fetchBytes} from './fetch.js';
import {decodePKS1, gridValues} from './records.js';
import {interpLinear, num} from './filters.js';
import {interpRv, redden} from './extinction.js';

function modelArrays(model) {
  const wave = model.wave;
  let flux = model.flux;
  if (!flux && model.channels) flux = model.channels.flux || model.channels[Object.keys(model.channels)[0]];
  if (!wave || !flux) throw new Error('model needs wave and flux');
  return {wave, flux};
}

function median(a) {
  const s = Float64Array.from(a).sort();
  const n = s.length;
  if (!n) return NaN;
  const h = n >> 1;
  return n % 2 ? s[h] : (s[h - 1] + s[h]) / 2;
}

/**
 * Median of fnu / model(wl) over points with kind 'det' (default when absent), fnu > 0 and (when
 * `bands` is a non-empty list) filter or key in `bands`; model(wl) is linear in wavelength and
 * must be finite and > 0. NaN when nothing is usable. `model` = {wave, flux} or a Spectrum.
 */
export function scaleTo(model, obsPoints, bands = null) {
  const {wave, flux} = modelArrays(model);
  const sel = Array.isArray(bands) && bands.length ? new Set(bands) : null;
  const ratios = [];
  for (const p of obsPoints || []) {
    if ((p.kind ?? 'det') !== 'det') continue;
    if (sel && !sel.has(p.filter) && !sel.has(p.key)) continue;
    const f = num(p.fnu), w = num(p.wl);
    if (f === null || w === null || f <= 0) continue;
    const mv = interpLinear(w, wave, flux);
    if (Number.isFinite(mv) && mv > 0) ratios.push(f / mv);
  }
  return ratios.length ? median(ratios) : NaN;
}

/** Law key for an R_V tabulated in the set ('k31' for 3.1, 'k55' for 5.5). */
export function lawKeyFor(rv) {
  return 'k' + String(rv).replace('.', '');
}

/** A pk-templates/1 set. URLs inside the index resolve against the index URL. */
export class TemplateSet {
  static async open(url, opts = {}) {
    const u = String(url);
    const index = await fetchJSON(u, {fetch: opts.fetch, signal: opts.signal});
    return new TemplateSet(index, u, opts);
  }

  constructor(index, url, opts = {}) {
    this.index = index;
    this.url = url;
    this.fopts = {fetch: opts.fetch};
    this.gridCache = new Map();
    this.recCache = new Map();
  }

  resolve(rel) {
    return new URL(rel, this.url).toString();
  }

  /** Template entries [{key, teff, logg, label, files}]. */
  list() {
    return this.index.templates || [];
  }

  /** Grid names ('prism', 'medium', 'high', ...). */
  gridNames() {
    return Object.keys(this.index.grids || {});
  }

  async grid(name, signal) {
    if (!this.gridCache.has(name)) {
      const g = (this.index.grids || {})[name];
      if (!g) throw new Error(`template set has no grid ${name}`);
      const p = fetchJSON(this.resolve(g.file), {...this.fopts, signal}).then((d) => Float64Array.from(gridValues(d)));
      this.gridCache.set(name, p);
      p.catch(() => this.gridCache.delete(name));
    }
    return this.gridCache.get(name);
  }

  async record(rel, gridName, signal) {
    const url = this.resolve(rel);
    if (!this.recCache.has(url)) {
      const p = (async () => {
        const w = await this.grid(gridName, signal);
        const b = await fetchBytes(url, {...this.fopts, signal});
        return decodePKS1(b, {[gridName]: w});
      })();
      this.recCache.set(url, p);
      p.catch(() => this.recCache.delete(url));
    }
    return this.recCache.get(url);
  }

  /** Template spectrum {wave, flux, ...pks1 fields} on grid `gridName`. */
  async template(key, gridName, signal) {
    const t = this.list().find((e) => e.key === key);
    if (!t) throw new Error(`unknown template ${key}`);
    const rel = (t.files || {})[gridName];
    if (!rel) throw new Error(`template ${key} has no ${gridName} grid`);
    const s = await this.record(rel, gridName, signal);
    return {...s, flux: s.channels.flux};
  }

  /** G23 A(lambda)/A(V) on a grid at any R_V (1/R_V interpolation of the tabulated laws). */
  async law(rv, gridName, signal) {
    const laws = this.index.laws || {};
    const tab = new Map();
    for (const [lk, files] of Object.entries(laws)) {
      const rel = files[gridName];
      if (!rel) continue;
      const rec = await this.record(rel, gridName, signal);
      const r = rec.meta && Number.isFinite(+rec.meta.rv) ? +rec.meta.rv : Number(lk.slice(1, 2) + '.' + lk.slice(2));
      tab.set(r, rec.channels.k);
    }
    if (!tab.size) throw new Error(`template set has no extinction law on grid ${gridName}`);
    const w = await this.grid(gridName, signal);
    const k = tab.size === 1 ? Array.from(tab.values())[0] : interpRv(rv, tab);
    return {w, k: Float64Array.from(k)};
  }

  /** Template reddened by A_V at R_V: {wave, flux: Float32Array}. */
  async reddened(key, gridName, av, rv, signal) {
    const t = await this.template(key, gridName, signal);
    if (!av) return {wave: t.wave, flux: Float32Array.from(t.flux)};
    const law = await this.law(rv, gridName, signal);
    return {wave: t.wave, flux: redden({wave: t.wave, flux: t.flux}, law, av)};
  }
}
