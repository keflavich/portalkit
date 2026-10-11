// Catalog markers on the sky (ARCHITECTURE.md sections 7.1 sky column and 8.2
// catalog_markers_on_sky).
//
// - One A.catalog per colour group: category levels of `color.by` (or of the first category
//   column when nothing is chosen), 8 colour-map bins for a continuous `color.by`, or a single
//   neutral catalog. Missing values get a neutral catalog. Selected rows are drawn on top in a
//   separate catalog in the selection colour.
// - Rows come from DataClient.skyWindow with mask 'vis' (filters applied):
//     T0     every visible row (whole-sky window, cap = N);
//     T1, F  viewport window capped at SKY_MARKER_CAP; a uniform LOD subsample is flagged
//            through onNotice;
//     T2     viewport markers when at most SKY_MARKER_CAP rows are in view, else a density
//            image of the visible rows (worker bin2d) drawn on the pane's canvas, with the
//            selected rows still drawn as markers.
//   Viewport windows are re-queried SETTLE_MS after the view stops moving.
// - Positions (whole ra/dec columns) and colour values load only after app.afterFirstPaint()
//   (IMPLEMENTATION_NOTES 99): the first refresh waits for it.
// - objectClicked sets `focus`, objectHovered / objectHoveredStop set `hover`.

import {SKY_MARKER_CAP, SETTLE_MS} from '../core/constants.js';
import {cmapColor, drawDensityGrid, prepareCanvas} from '../plot/canvas2d.js';
import {fmtCount} from '../core/fmt.js';
import {dropObject} from './aladin.js';

const NBINS = 8;
const DENSITY_BINS = 256;
const MARGIN = 1.15;            // query window = view × MARGIN, so small pans need no re-query
const CAT_MAX_LEVELS = 64;

/**
 * Promise of the page's first paint (app.afterFirstPaint(), IMPLEMENTATION_NOTES 100). Apps
 * without the signal (unit tests, embedders) resolve at once, which is the pre-note-99 order.
 */
export function afterFirstPaint(app) {
  if (app && typeof app.afterFirstPaint === 'function') return Promise.resolve(app.afterFirstPaint());
  return Promise.resolve();
}

/** Colour scheme for markers: {kind:'cat'|'cont'|'none', key, ...}. Pure. */
export function colorScheme(client, color) {
  const c = color || {};
  const by = c.by;
  if (by) {
    const meta = client.col(by);
    if (meta && meta.role === 'category' && Array.isArray(meta.levels) && meta.levels.length <= CAT_MAX_LEVELS) {
      return {kind: 'cat', key: by, levels: meta.levels, colors: meta.colors || {}};
    }
    if (meta || (typeof client.isVirtual === 'function' && client.isVirtual(by))) {
      return {kind: 'cont', key: by, cmap: c.cmap || 'viridis', lo: c.lo, hi: c.hi, meta};
    }
  }
  const cols = (client.manifest && client.manifest.cols) || [];
  const cat = cols.find((m) => m.role === 'category' && Array.isArray(m.levels) && m.levels.length > 1 &&
    m.levels.length <= CAT_MAX_LEVELS);
  if (cat) return {kind: 'cat', key: cat.key, levels: cat.levels, colors: cat.colors || {}};
  return {kind: 'none'};
}

/** Range for a continuous scheme: explicit lo/hi, else 2-98 % quantiles, else data min/max. */
export function contRange(scheme, values, idx) {
  let lo = Number.isFinite(+scheme.lo) && scheme.lo !== null ? +scheme.lo : NaN;
  let hi = Number.isFinite(+scheme.hi) && scheme.hi !== null ? +scheme.hi : NaN;
  const q = scheme.meta && Array.isArray(scheme.meta.quantiles) ? scheme.meta.quantiles : null;
  if (!Number.isFinite(lo) && q && q.length === 101 && !scheme.meta.log) lo = +q[2];
  if (!Number.isFinite(hi) && q && q.length === 101 && !scheme.meta.log) hi = +q[98];
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) {
    let a = Infinity, b = -Infinity;
    for (let k = 0; k < idx.length; k++) {
      const v = values[idx[k]];
      if (Number.isFinite(v)) { if (v < a) a = v; if (v > b) b = v; }
    }
    if (!Number.isFinite(lo)) lo = a;
    if (!Number.isFinite(hi)) hi = b;
  }
  if (!(hi > lo)) hi = lo + 1;
  return [lo, hi];
}

/**
 * Split rows `idx` into colour groups. Returns Map(groupKey -> {label, color, rows: number[]}).
 * Pure apart from the colour lookup.
 */
export function groupRows(idx, scheme, values, tokens) {
  const groups = new Map();
  const neutral = (tokens && tokens.neutral) || '#4b5563';
  const add = (key, label, color, i) => {
    let g = groups.get(key);
    if (!g) {
      g = {label, color, rows: []};
      groups.set(key, g);
    }
    g.rows.push(i);
  };
  if (scheme.kind === 'cat' && values) {
    const cat = (tokens && tokens.cat) || [];
    for (let k = 0; k < idx.length; k++) {
      const i = idx[k];
      const v = values[i];
      if (!Number.isFinite(v)) { add('missing', 'missing', neutral, i); continue; }
      const lev = scheme.levels[v] ?? String(v);
      add(`c${v}`, String(lev), scheme.colors[lev] || cat[v % 10] || neutral, i);
    }
  } else if (scheme.kind === 'cont' && values) {
    const [lo, hi] = contRange(scheme, values, idx);
    for (let k = 0; k < idx.length; k++) {
      const i = idx[k];
      const v = values[i];
      if (!Number.isFinite(v)) { add('missing', 'missing', neutral, i); continue; }
      const b = Math.max(0, Math.min(NBINS - 1, Math.floor(((v - lo) / (hi - lo)) * NBINS)));
      add(`b${b}`, `bin ${b + 1}/${NBINS}`, cmapColor(scheme.cmap, (b + 0.5) / NBINS), i);
    }
  } else {
    for (let k = 0; k < idx.length; k++) add('all', 'sources', (tokens && tokens.accent) || '#1f9bb6', idx[k]);
  }
  return groups;
}

/** Window for a view: {ra, dec, fov, fovy} widened by MARGIN. */
export function queryWindow(v, margin = MARGIN) {
  return {ra: v.ra, dec: v.dec, fov: Math.min(360, v.fov * margin), fovy: Math.min(180, (v.fovy || v.fov) * margin)};
}

function covers(outer, v) {
  if (!outer) return false;
  if (outer.fov >= 360) return true;
  const c = Math.cos((v.dec * Math.PI) / 180);
  let dra = Math.abs(v.ra - outer.ra);
  if (dra > 180) dra = 360 - dra;
  return dra * c + v.fov / 2 <= outer.fov / 2 + 1e-9 &&
    Math.abs(v.dec - outer.dec) + (v.fovy || v.fov) / 2 <= outer.fovy / 2 + 1e-9;
}

export class SkyMarkers {
  /**
   * opts: {h (AladinHandle), app, store, ds, pane, client, tokens: () => Tokens,
   *        onNotice(text|null), canvas (density canvas), mode: 'auto'|'off'}
   */
  constructor(opts) {
    Object.assign(this, {h: opts.h, app: opts.app, store: opts.store, ds: opts.ds, pane: opts.pane,
      client: opts.client, tokens: opts.tokens || (() => ({})), onNotice: opts.onNotice || (() => {}),
      canvas: opts.canvas || null});
    this.mode = opts.mode === 'off' ? 'off' : 'auto';
    this.cats = new Map();          // groupKey -> {catalog, color, label}
    this.selCat = null;
    this.idx = new Int32Array(0);
    this.selIdx = new Int32Array(0);
    this.total = 0;
    this.sampled = false;
    this.density = null;            // last density grid
    this.lastWin = null;
    this.ra = null;
    this.dec = null;
    this.values = new Map();        // key -> Float32Array
    this.schemeKey = '';
    this.seq = 0;
    this.selSeq = 0;
    this.timer = null;
    this.visible = true;
    this.destroyed = false;
    this.offs = [];
    this.lastStats = {mode: 'none', shown: 0, total: 0, sampled: false, catalogs: 0, selected: 0};
    this.offs.push(this.h.on('objectClicked', (obj) => this._click(obj)));
    this.offs.push(this.h.on('objectHovered', (obj) => this._hover(obj)));
    this.offs.push(this.h.on('objectHoveredStop', () => this._hover(null)));
  }

  _ours(obj) {
    if (!obj || !obj.data || !Number.isInteger(obj.data.idx)) return false;
    const c = obj.catalog;
    return !c || typeof c.name !== 'string' || c.name.startsWith(`${this.pane}:m:`);
  }

  async _click(obj) {
    if (!this._ours(obj)) return;
    const i = obj.data.idx;
    this.store.set({focus: {ds: this.ds, i, id: null}}, {source: this.pane});
    const id = await this.app.idOf(this.ds, i);
    const f = this.store.get('focus');
    if (f && f.ds === this.ds && f.i === i && f.id == null && id != null) {
      this.store.set({focus: {ds: this.ds, i, id}}, {source: this.pane});
    }
  }

  _hover(obj) {
    if (obj && !this._ours(obj)) return;
    const next = obj ? {ds: this.ds, i: obj.data.idx} : null;
    const cur = this.store.get('hover');
    if (!next && (!cur || cur.ds !== this.ds)) return;
    this.store.set({hover: next}, {source: this.pane});
  }

  /**
   * Load ra/dec once. The whole-column read waits for app.afterFirstPaint()
   * (IMPLEMENTATION_NOTES 99, 100), so the sky pane never competes with the first paint of the
   * other panes; until then the viewer shows the survey without markers.
   */
  async _positions() {
    if (this.ra) return;
    if (!this._posP) {
      const p = (async () => {
        await afterFirstPaint(this.app);
        if (this.destroyed) return;
        const [ra, dec] = await Promise.all([this.client.values('ra'), this.client.values('dec')]);
        this.ra = ra;
        this.dec = dec;
      })();
      this._posP = p;
      // a failed load is retried by the next refresh
      p.catch(() => { if (this._posP === p) this._posP = null; });
    }
    await this._posP;
  }

  async _values(key) {
    if (!key) return null;
    if (this.values.has(key)) return this.values.get(key);
    const v = await this.client.values(key);
    this.values.set(key, v);
    return v;
  }

  /** Position of row i ([ra, dec]) from the cached arrays, or null. */
  position(i) {
    if (!this.ra || !(i >= 0) || i >= this.ra.length) return null;
    const a = this.ra[i], d = this.dec[i];
    return Number.isFinite(a) && Number.isFinite(d) ? [a, d] : null;
  }

  tier() {
    return this.store.get(`tier:${this.pane}`) || 'T1';
  }

  setMode(mode) {
    this.mode = mode === 'off' ? 'off' : 'auto';
    this.refresh({force: true});
  }

  setVisible(v) {
    this.visible = !!v;
    if (v && this._pending) this.refresh({force: true});
  }

  /** View moved: clear the density image now, re-query after SETTLE_MS when needed. */
  viewChanged() {
    if (this.density && this.canvas) this._clearCanvas();
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.refresh(), SETTLE_MS);
  }

  /** Recompute rows and catalogs. opts.force skips the "window still covers the view" test. */
  async refresh(opts = {}) {
    clearTimeout(this.timer);
    if (this.destroyed) return;
    if (!this.visible) { this._pending = true; return; }
    this._pending = false;
    if (this.h.state !== 'ready') return;
    const seq = ++this.seq;
    if (this.mode === 'off') {
      this._clearAll();
      this.onNotice(null);
      this._setStats({mode: 'off', shown: 0, total: 0, sampled: false});
      return;
    }
    const tier = this.tier();
    const v = this.h.view();
    if (![v.ra, v.dec, v.fov].every(Number.isFinite)) return;
    const whole = tier === 'T0';
    if (!opts.force && !whole && !this.density && covers(this.lastWin, v) && !this.sampled) return;
    await this._positions();
    if (seq !== this.seq || this.destroyed || !this.ra) return;
    const win = whole ? {ra: 0, dec: 0, fov: 360, fovy: 180, cap: this.client.n, mask: 'vis'}
      : Object.assign(queryWindow(v), {cap: SKY_MARKER_CAP, mask: 'vis'});
    const r = await this.client.skyWindow(win);
    if (seq !== this.seq || this.destroyed) return;
    this.lastWin = whole ? {ra: 0, dec: 0, fov: 360, fovy: 180} : win;
    this.total = r.total;
    if (tier === 'T2' && r.sampled) {
      this.idx = new Int32Array(0);
      this.sampled = true;
      await this._drawDensity(v, seq);
      if (seq !== this.seq) return;
      this._fillCatalogs(new Map());
      await this._refreshSelected(seq);
      this.onNotice(`Density of ${fmtCount(r.total)} sources in view; zoom in below ${fmtCount(SKY_MARKER_CAP)} to see markers.`);
      this._setStats({mode: 'density', shown: 0, total: r.total, sampled: true});
      return;
    }
    if (this.density) this._clearCanvas();
    this.density = null;
    this.idx = r.idx;
    this.sampled = !!r.sampled;
    const scheme = colorScheme(this.client, this.store.get('color'));
    const values = scheme.kind === 'none' ? null : await this._values(scheme.key);
    if (seq !== this.seq || this.destroyed) return;
    const groups = groupRows(this.idx, scheme, values, this.tokens());
    const key = `${scheme.kind}:${scheme.key || ''}:${scheme.cmap || ''}:${this.tokens().theme || ''}`;
    if (key !== this.schemeKey) {
      this._dropCats();
      this.schemeKey = key;
    }
    this._fillCatalogs(groups);
    await this._refreshSelected(seq);
    if (this.sampled) {
      this.onNotice(`Showing ${fmtCount(this.idx.length)} of ${fmtCount(r.total)} sources in view (uniform subsample); zoom in to see all.`);
    } else {
      this.onNotice(null);
    }
    this._setStats({mode: 'markers', shown: this.idx.length, total: r.total, sampled: this.sampled});
  }

  _setStats(s) {
    this.lastStats = Object.assign({}, s, {catalogs: this.cats.size, selected: this.selIdx.length});
  }

  _catName(key) {
    return `${this.pane}:m:${this.ds}:${key}`;
  }

  _fillCatalogs(groups) {
    const shown = new Set(groups.keys());
    this.h.run((al, A) => {
      for (const [key, rec] of this.cats) {
        if (!shown.has(key) && rec.catalog) rec.catalog.removeAll();
      }
      for (const [key, g] of groups) {
        let rec = this.cats.get(key);
        if (!rec || rec.color !== g.color) {
          if (rec) dropObject(al, rec.catalog);
          const catalog = A.catalog({name: this._catName(key), color: g.color, sourceSize: 9, shape: 'circle'});
          al.addCatalog(catalog);
          rec = {catalog, color: g.color, label: g.label};
          this.cats.set(key, rec);
        } else {
          rec.catalog.removeAll();
        }
        const ra = this.ra, dec = this.dec;
        const srcs = new Array(g.rows.length);
        for (let k = 0; k < g.rows.length; k++) {
          const i = g.rows[k];
          srcs[k] = A.source(ra[i], dec[i], {idx: i, group: g.label});
        }
        rec.catalog.addSources(srcs);
      }
    });
  }

  /** Selected rows (mask 'sel') in view, drawn on top. */
  async refreshSelected() {
    if (this.h.state !== 'ready' || this.mode === 'off') return;
    await this._positions();
    if (this.destroyed || !this.ra) return;
    await this._refreshSelected(this.seq);
    this.lastStats.selected = this.selIdx.length;
  }

  async _refreshSelected(seq) {
    const mySeq = ++this.selSeq;
    const sel = this.app.selections && this.app.selections.get(this.ds);
    let idx = new Int32Array(0);
    if (sel && sel.count > 0) {
      const v = this.h.view();
      const whole = this.tier() === 'T0' || !Number.isFinite(v.fov);
      const win = whole ? {ra: 0, dec: 0, fov: 360, fovy: 180, cap: SKY_MARKER_CAP, mask: 'sel'}
        : Object.assign(queryWindow(v), {cap: SKY_MARKER_CAP, mask: 'sel'});
      const r = await this.client.skyWindow(win);
      if (mySeq !== this.selSeq || seq !== this.seq || this.destroyed) return;
      idx = r.idx;
    }
    this.selIdx = idx;
    if (this.density) this._paintDensity();
    const color = this.tokens().sel || '#d9822b';
    this.h.run((al, A) => {
      if (this.selCat && this.selCat.color !== color) {
        dropObject(al, this.selCat.catalog);
        this.selCat = null;
      }
      if (!idx.length) {
        if (this.selCat) this.selCat.catalog.removeAll();
        return;
      }
      if (!this.selCat) {
        const catalog = A.catalog({name: this._catName('selected'), color, sourceSize: 13, shape: 'circle'});
        al.addCatalog(catalog);
        this.selCat = {catalog, color};
      } else {
        this.selCat.catalog.removeAll();
      }
      const srcs = new Array(idx.length);
      for (let k = 0; k < idx.length; k++) {
        const i = idx[k];
        srcs[k] = A.source(this.ra[i], this.dec[i], {idx: i, group: 'selected'});
      }
      this.selCat.catalog.addSources(srcs);
    });
  }

  async _drawDensity(v, seq) {
    if (!this.canvas) return;
    const c = Math.max(1e-6, Math.cos((v.dec * Math.PI) / 180));
    const hx = v.fov / 2 / c, hy = (v.fovy || v.fov) / 2;
    const xr = [v.ra - hx, v.ra + hx], yr = [v.dec - hy, v.dec + hy];
    const [w, hgt] = this.h.size();
    const nx = Math.max(16, Math.min(DENSITY_BINS, Math.round(w / 2)));
    const ny = Math.max(16, Math.min(DENSITY_BINS, Math.round(hgt / 2)));
    const grid = await this.client.bin2d({pane: this.pane, kind: 'sky', x: 'ra', y: 'dec'}, nx, ny, {mask: 'vis', xr, yr});
    if (seq !== this.seq || this.destroyed) return;
    this.density = grid;
    this._paintDensity();
  }

  _clearCanvas() {
    if (!this.canvas) return;
    const ctx = this.canvas.getContext('2d');
    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    ctx.restore();
  }

  _paintDensity() {
    const g = this.density;
    if (!g || !this.canvas) return;
    const [w, hgt] = this.h.size();
    const ctx = prepareCanvas(this.canvas, w, hgt, window.devicePixelRatio || 1);
    ctx.clearRect(0, 0, w, hgt);
    const p00 = this.h.world2pix(g.xr[0], g.yr[0]);
    const p10 = this.h.world2pix(g.xr[1], g.yr[0]);
    const p01 = this.h.world2pix(g.xr[0], g.yr[1]);
    if (!p00 || !p10 || !p01) return;
    const lin = (a0, a1, b0, b1) => ({f: (v) => b0 + ((v - a0) / (a1 - a0)) * (b1 - b0)});
    const view = {
      plot: {left: 0, top: 0, width: w, height: hgt},
      sx: lin(g.xr[0], g.xr[1], p00[0], p10[0]),
      sy: lin(g.yr[0], g.yr[1], p00[1], p01[1]),
    };
    const t = this.tokens();
    drawDensityGrid(ctx, view, g, {tokens: t, cmap: t.densityCmap || 'magma', alpha: 0.85});
    // The density canvas sits above the Aladin canvas (which draws the HiPS and the markers),
    // so the selected rows are redrawn here to stay visible on top of the raster.
    const sel = this.selIdx;
    if (sel && sel.length && this.ra) {
      ctx.save();
      ctx.strokeStyle = t.sel || '#d9822b';
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      const r = 5;
      for (let k = 0; k < sel.length; k++) {
        const i = sel[k];
        const x = view.sx.f(this.ra[i]), y = view.sy.f(this.dec[i]);
        if (!Number.isFinite(x) || !Number.isFinite(y) || x < -r || y < -r || x > w + r || y > hgt + r) continue;
        ctx.moveTo(x + r, y);
        ctx.arc(x, y, r, 0, 2 * Math.PI);
      }
      ctx.stroke();
      ctx.restore();
    }
  }

  /** Theme or colour change: rebuild catalogs with new colours. */
  restyle() {
    this.schemeKey = '';
    this.refresh({force: true});
  }

  _dropCats() {
    const cats = Array.from(this.cats.values());
    this.cats.clear();
    if (cats.length) this.h.run((al) => { for (const r of cats) dropObject(al, r.catalog); });
  }

  _clearAll() {
    this._dropCats();
    const s = this.selCat;
    this.selCat = null;
    if (s) this.h.run((al) => dropObject(al, s.catalog));
    this.idx = new Int32Array(0);
    this.selIdx = new Int32Array(0);
    this.density = null;
    this._clearCanvas();
    this.schemeKey = '';
  }

  stats() {
    return Object.assign({}, this.lastStats, {catalogs: this.cats.size, selected: this.selIdx.length});
  }

  destroy() {
    this.destroyed = true;
    clearTimeout(this.timer);
    for (const off of this.offs) off();
    this._clearAll();
  }
}
