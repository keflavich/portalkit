// Sky-space vector overlays on Aladin (ARCHITECTURE.md section 5.10 `pk-overlays/1`, space "sky").
//
// Layer kinds drawn on the sky: polygons, boxes, circles, rings, polyline and markers
// (dim-space kinds `vector`/`track` belong to plot/overlays.js and are skipped here).
// Items: {poly:[[ra,dec],...]} | {ra, dec, r_arcsec?, label?, shape?} (boxes also take
// w_arcsec/h_arcsec; rings take r_arcsec as a number or a list of radii).
// Every layer becomes one A.graphicOverlay (shapes) and/or one A.catalog (markers), built when
// shown and removed from the viewer when hidden. An opacity change rebuilds the layer, because
// Aladin shapes take their opacity at creation.
//
// ShapeSet is a small named graphic overlay for transient shapes owned by a pane: focus and
// hover rings, `rings` option circles, narrative highlights, the brush footprint.

import {dropObject, showObject} from './aladin.js';

const D2R = Math.PI / 180;
export const SKY_KINDS = Object.freeze(['polygons', 'boxes', 'circles', 'rings', 'polyline', 'markers']);

const SHAPE_MAP = {
  circle: 'circle', square: 'square', diamond: 'rhomb', rhomb: 'rhomb', triangle: 'triangle',
  'triangle-down': 'triangle', plus: 'plus', cross: 'cross', star: 'plus', hexagon: 'circle',
};

/** Aladin marker shape for a portalkit shape name. */
export function aladinShape(name) {
  return SHAPE_MAP[String(name || 'square').toLowerCase()] || 'square';
}

/** Corners of a box centred on (ra, dec) with half-sizes in degrees (RA stretched by 1/cos dec). */
export function boxCorners(ra, dec, hw, hh = hw) {
  const c = Math.max(1e-6, Math.cos(dec * D2R));
  const dx = hw / c;
  return [[ra - dx, dec - hh], [ra + dx, dec - hh], [ra + dx, dec + hh], [ra - dx, dec + hh]];
}

function finitePt(p) {
  return Array.isArray(p) && p.length >= 2 && Number.isFinite(+p[0]) && Number.isFinite(+p[1]);
}

function polyOf(item) {
  if (!item || !Array.isArray(item.poly)) return null;
  const pts = item.poly.filter(finitePt).map((p) => [+p[0], +p[1]]);
  return pts.length >= 2 ? pts : null;
}

function styleOpts(style, tokens, opacityOverride) {
  const s = style || {};
  const color = s.color || (tokens && tokens.accent) || '#1f9bb6';
  const opacity = opacityOverride ?? (Number.isFinite(+s.opacity) ? +s.opacity : 1);
  const o = {color, lineWidth: Number.isFinite(+s.width) ? +s.width : 2, opacity};
  if (s.fill) {
    o.fill = true;
    o.fillColor = typeof s.fill === 'string' ? s.fill : color;
  }
  if (Array.isArray(s.dash) && s.dash.length) o.lineDash = s.dash.map(Number);
  else if (s.dash === true) o.lineDash = [4, 4];
  return o;
}

/**
 * Aladin objects for one sky layer: {overlay, catalog, count}. Pure apart from A.* calls.
 * opts: {tokens, opacity, name}.
 */
export function buildSkyLayer(A, layer, opts = {}) {
  const kind = layer.kind;
  const style = layer.style || {};
  const so = styleOpts(style, opts.tokens, opts.opacity);
  const name = opts.name || `ovl:${layer.id}`;
  let overlay = null, catalog = null, count = 0;
  const ov = () => {
    if (!overlay) {
      const o = {name, color: so.color, lineWidth: so.lineWidth};
      if (so.lineDash) o.lineDash = so.lineDash;
      overlay = A.graphicOverlay(o);
    }
    return overlay;
  };
  const shapes = [];
  for (const item of layer.items || []) {
    if (!item || typeof item !== 'object') continue;
    const pts = polyOf(item);
    if (kind === 'markers') {
      if (!Number.isFinite(+item.ra) || !Number.isFinite(+item.dec)) continue;
      if (!catalog) {
        catalog = A.catalog({
          name, color: so.color, sourceSize: Number.isFinite(+style.size) ? +style.size : 10,
          shape: aladinShape(style.shape), displayLabel: (layer.items || []).some((it) => it && it.label),
          labelColumn: 'label', labelColor: so.color, labelFont: '12px sans-serif',
        });
      }
      const src = A.source(+item.ra, +item.dec, {label: item.label ? String(item.label) : '', layer: layer.id});
      shapes.push(['src', src]);
      continue;
    }
    if (pts) {
      if (kind === 'polyline') shapes.push(['shape', typeof A.polyline === 'function' ? A.polyline(pts, so) : A.polygon(pts, so)]);
      else shapes.push(['shape', A.polygon(pts, so)]);
      continue;
    }
    const ra = +item.ra, dec = +item.dec;
    if (!Number.isFinite(ra) || !Number.isFinite(dec)) continue;
    if (kind === 'boxes') {
      const w = Number.isFinite(+item.w_arcsec) ? +item.w_arcsec : +item.r_arcsec * 2;
      const hgt = Number.isFinite(+item.h_arcsec) ? +item.h_arcsec : w;
      if (!(w > 0) || !(hgt > 0)) continue;
      shapes.push(['shape', A.polygon(boxCorners(ra, dec, w / 7200, hgt / 7200), so)]);
    } else if (kind === 'circles' || kind === 'rings' || kind === 'polygons') {
      const radii = Array.isArray(item.r_arcsec) ? item.r_arcsec : [item.r_arcsec ?? style.r_arcsec ?? 1];
      for (const r of radii) {
        if (Number.isFinite(+r) && +r > 0) shapes.push(['shape', A.circle(ra, dec, +r / 3600, so)]);
      }
    }
  }
  for (const [t, obj] of shapes) {
    if (t === 'src') {
      count++;
      continue;
    }
    ov().add(obj);
    count++;
  }
  if (catalog) catalog.addSources(shapes.filter((s) => s[0] === 'src').map((s) => s[1]));
  return {overlay, catalog, count};
}

/** Vector overlay layers of one sky pane. */
export class VectorOverlays {
  /**
   * @param h       AladinHandle
   * @param opts    {prefix (unique per pane), tokens: () => Tokens}
   */
  constructor(h, opts = {}) {
    this.h = h;
    this.prefix = opts.prefix || 'pk';
    this.tokens = opts.tokens || (() => ({}));
    this.layers = new Map();       // id -> layer doc
    this.built = new Map();        // id -> {overlay, catalog, count, opacity}
    this.on = new Map();           // id -> opacity (shown layers)
  }

  /** Register the sky-space layers this pane may draw. */
  setLayers(layers) {
    for (const lay of layers || []) {
      if (!lay || typeof lay.id !== 'string') continue;
      if ((lay.space || 'sky') !== 'sky' || !SKY_KINDS.includes(lay.kind)) continue;
      this.layers.set(lay.id, lay);
    }
  }

  has(id) {
    return this.layers.has(id);
  }

  ids() {
    return Array.from(this.layers.keys());
  }

  /** Ids shown by default (layer.default). */
  defaults() {
    return this.ids().filter((id) => this.layers.get(id).default);
  }

  layer(id) {
    return this.layers.get(id) || null;
  }

  shown() {
    return Array.from(this.on.keys());
  }

  _build(id, opacity) {
    const lay = this.layers.get(id);
    const rec = {overlay: null, catalog: null, count: 0, opacity, dropped: false};
    this.built.set(id, rec);
    this.h.run((al, A) => {
      if (rec.dropped) return;
      const b = buildSkyLayer(A, lay, {tokens: this.tokens(), opacity, name: `${this.prefix}:ovl:${id}`});
      Object.assign(rec, b);
      if (b.overlay) al.addOverlay(b.overlay);
      if (b.catalog) al.addCatalog(b.catalog);
      if (!this.on.has(id)) { showObject(b.overlay, false); showObject(b.catalog, false); }
    });
    return rec;
  }

  _drop(id) {
    const b = this.built.get(id);
    if (!b) return;
    this.built.delete(id);
    b.dropped = true;
    this.h.run((al) => {
      if (b.overlay) dropObject(al, b.overlay);
      if (b.catalog) dropObject(al, b.catalog);
    });
  }

  /** Show or hide layer `id` at `opacity` (default: the layer style opacity). */
  set(id, show, opacity) {
    const lay = this.layers.get(id);
    if (!lay) return false;
    const op = Number.isFinite(+opacity) ? Math.max(0, Math.min(1, +opacity))
      : (Number.isFinite(+(lay.style && lay.style.opacity)) ? +lay.style.opacity : 1);
    if (!show) {
      // Hidden layers are removed from the viewer (not just hidden), so a narrative scene that
      // turns a layer off leaves nothing behind; showing it again rebuilds it.
      this.on.delete(id);
      this._drop(id);
      return true;
    }
    this.on.set(id, op);
    const b = this.built.get(id);
    if (b && Math.abs((b.opacity ?? 1) - op) > 1e-6) this._drop(id);
    if (!this.built.has(id)) this._build(id, op);
    else this.h.run(() => { showObject(b.overlay, true); showObject(b.catalog, true); });
    return true;
  }

  /** Make exactly `wanted` ({id: opacity|undefined}) visible. */
  apply(wanted) {
    for (const id of this.shown()) if (!(id in wanted)) this.set(id, false);
    for (const [id, op] of Object.entries(wanted)) if (this.layers.has(id)) this.set(id, true, op);
  }

  /** Theme change: rebuild shown layers that take token colours. */
  retheme() {
    for (const [id, op] of this.on) {
      const lay = this.layers.get(id);
      if (lay && !(lay.style && lay.style.color)) {
        this._drop(id);
        this._build(id, op);
      }
    }
  }

  stats() {
    const out = {};
    for (const [id, b] of this.built) out[id] = {shown: this.on.has(id), count: b ? b.count : 0};
    return out;
  }

  destroy() {
    for (const id of Array.from(this.built.keys())) this._drop(id);
    this.on.clear();
  }
}

/** A named graphic overlay for transient pane-owned shapes. */
export class ShapeSet {
  constructor(h, name) {
    this.h = h;
    this.name = name;
    this.overlay = null;
    this.n = 0;
  }

  _ensure(al, A, color, width) {
    if (!this.overlay) {
      this.overlay = A.graphicOverlay({name: this.name, color, lineWidth: width});
      al.addOverlay(this.overlay);
    }
    return this.overlay;
  }

  /** Replace the contents: circles [{ra, dec, r (deg)}] and polygons [[[ra,dec]...]]. */
  set({circles = [], polygons = [], color = '#ffe14d', width = 2, opacity = 1, fill = false} = {}) {
    this.h.run((al, A) => {
      if (this.overlay && typeof this.overlay.removeAll === 'function') this.overlay.removeAll();
      this.n = 0;
      if (!circles.length && !polygons.length) return;
      const ov = this._ensure(al, A, color, width);
      const so = {color, lineWidth: width, opacity};
      if (fill) { so.fill = true; so.fillColor = color; }
      for (const c of circles) {
        if (![c.ra, c.dec, c.r].every(Number.isFinite) || !(c.r > 0)) continue;
        ov.add(A.circle(c.ra, c.dec, c.r, so));
        this.n++;
      }
      for (const p of polygons) {
        const pts = (p || []).filter(finitePt);
        if (pts.length < 2) continue;
        ov.add(A.polygon(pts, so));
        this.n++;
      }
    });
  }

  clear() {
    this.set({});
  }

  destroy() {
    const ov = this.overlay;
    this.overlay = null;
    this.n = 0;
    if (ov) this.h.run((al) => dropObject(al, ov));
  }
}
