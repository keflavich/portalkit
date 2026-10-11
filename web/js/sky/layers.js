// HiPS layer stacking and FITS overlays for a sky pane (ARCHITECTURE.md section 8.2:
// hips_layer_stacking, fits_overlay).
//
// SkyLayers keeps the base layer, the HiPS overlay layers (each with its own opacity), the
// sky-space vector layers (through sky/overlays.js VectorOverlays) and lazily loaded FITS
// overlays (createImageFITS on first use, with a loading state and an error note). It applies
// the `sky` store value {base, overlays:[{id, opacity}], fits:[id]} and reports user changes
// through onChange(state). `overlays` holds HiPS overlay ids and vector layer ids together;
// `overlays` undefined/null means "defaults" (layers flagged `default`), [] means all off.
// The panel offers a base picker, per-layer toggles and opacity sliders, FITS toggles,
// bookmarks, and the "Overlays off" / "Defaults" buttons.

import {addImageLayer, hasOpacity, removeImageLayer, setBaseLayer, setLayerOpacity} from './aladin.js';
import {escapeHtml} from '../core/fmt.js';

const FITS_ON_OPACITY = 0.9;

function clamp01(v, dflt = 1) {
  const x = +v;
  return Number.isFinite(x) ? Math.max(0, Math.min(1, x)) : dflt;
}

/** The default base entry of a pk-hips/1 document (flagged `default`, else the first). */
export function defaultBase(hips) {
  const base = (hips && hips.base) || [];
  return base.find((b) => b && b.default) || base[0] || null;
}

/**
 * Overlay set to show: `sky.overlays` when it is an array, else the defaults.
 * Returns {id: opacity|undefined}.
 */
export function wantedOverlays(skyOverlays, defaults) {
  const out = {};
  if (Array.isArray(skyOverlays)) {
    for (const o of skyOverlays) {
      if (typeof o === 'string') out[o] = undefined;
      else if (o && typeof o.id === 'string') out[o.id] = Number.isFinite(+o.opacity) && o.opacity !== null ? +o.opacity : undefined;
    }
    return out;
  }
  for (const d of defaults) out[d.id] = d.opacity;
  return out;
}

export class SkyLayers {
  /**
   * @param h     AladinHandle
   * @param opts  {hips: pk-hips/1 doc | null, vector: VectorOverlays | null, prefix,
   *               overlayIds: allowed vector ids (null = all), fitsIds: allowed fits ids
   *               (null = all), resolve(url) -> url, onChange(state), onGoto({ra, dec, fov}),
   *               toast({level, text})}
   */
  constructor(h, opts = {}) {
    this.h = h;
    this.hips = opts.hips || {base: [], overlays: [], fits: []};
    this.vector = opts.vector || null;
    this.prefix = opts.prefix || 'pk';
    this.resolve = opts.resolve || ((u) => u);
    this.onChange = opts.onChange || (() => {});
    this.onGoto = opts.onGoto || null;
    this.toast = opts.toast || (() => {});
    const allowF = opts.fitsIds ? new Set(opts.fitsIds) : null;
    this.baseList = (this.hips.base || []).filter((b) => b && b.id && b.url);
    this.imgList = (this.hips.overlays || []).filter((o) => o && o.id && o.url);
    this.fitsList = (this.hips.fits || []).filter((f) => f && f.id && f.url && (!allowF || allowF.has(f.id)));
    this.base = null;              // applied base id
    this.img = new Map();          // id -> {layer, opacity, name}
    this.fits = new Map();         // id -> {status, layer, on, name, error}
    this.panelEl = null;
    this.noOpacity = false;
  }

  /** Initial base URL for the viewer (passed as `survey` at creation). */
  initialBaseUrl(id) {
    const b = this.baseList.find((x) => x.id === id) || defaultBase({base: this.baseList});
    return b ? this.resolve(b.url) : null;
  }

  /** [{id, opacity}] shown by default. */
  defaults() {
    const out = [];
    for (const o of this.imgList) if (o.default) out.push({id: o.id, opacity: clamp01(o.opacity, 1)});
    if (this.vector) for (const id of this.vector.defaults()) out.push({id, opacity: undefined});
    return out;
  }

  /** Every overlay id this pane knows (HiPS overlays then vector layers). */
  overlayIds() {
    return this.imgList.map((o) => o.id).concat(this.vector ? this.vector.ids() : []);
  }

  /** Current state as stored in `sky`. */
  state() {
    const overlays = [];
    for (const [id, r] of this.img) overlays.push({id, opacity: r.opacity});
    if (this.vector) for (const [id, op] of this.vector.on) overlays.push({id, opacity: op});
    const fits = [];
    for (const [id, r] of this.fits) if (r.on) fits.push(id);
    return {base: this.base, overlays, fits};
  }

  /** Apply a `sky` store value (base, overlays, fits); missing fields mean defaults. */
  apply(sky) {
    const s = sky || {};
    const baseId = s.base && this.baseList.some((b) => b.id === s.base) ? s.base : (defaultBase({base: this.baseList}) || {}).id;
    if (baseId && baseId !== this.base) this.setBase(baseId, {quiet: true});
    const wanted = wantedOverlays(s.overlays, this.defaults());
    const imgWanted = {}, vecWanted = {};
    for (const [id, op] of Object.entries(wanted)) {
      if (this.imgList.some((o) => o.id === id)) imgWanted[id] = op;
      else if (this.vector && this.vector.has(id)) vecWanted[id] = op;
    }
    for (const id of Array.from(this.img.keys())) if (!(id in imgWanted)) this._imgOff(id);
    for (const [id, op] of Object.entries(imgWanted)) this._imgOn(id, op);
    if (this.vector) this.vector.apply(vecWanted);
    const fitsOn = new Set(Array.isArray(s.fits) ? s.fits : []);
    for (const f of this.fitsList) this._fitsSet(f.id, fitsOn.has(f.id));
    this.refreshPanel();
  }

  setBase(id, {quiet = false} = {}) {
    const b = this.baseList.find((x) => x.id === id);
    if (!b) return;
    this.base = id;
    const url = this.resolve(b.url);
    const skip = !this._baseTouched && this.h.initialSurvey === url;
    this._baseTouched = true;
    if (!skip) this.h.run((al, A) => setBaseLayer(al, A, url, b.name || b.id, {maxOrder: b.maxOrder, imgFormat: b.format}));
    if (!quiet) this._changed();
  }

  _imgName(id) {
    return `${this.prefix}:img:${id}`;
  }

  _imgOn(id, opacity) {
    const o = this.imgList.find((x) => x.id === id);
    if (!o) return;
    const op = clamp01(opacity ?? o.opacity, 1);
    let r = this.img.get(id);
    if (!r) {
      r = {layer: null, opacity: op, name: this._imgName(id)};
      this.img.set(id, r);
      const url = this.resolve(o.url);
      this.h.run((al, A) => {
        if (!this.img.has(id) || this.img.get(id) !== r) return;
        r.layer = addImageLayer(al, A, url, r.name, {maxOrder: o.maxOrder, imgFormat: o.format});
        if (!setLayerOpacity(r.layer, r.opacity)) this.noOpacity = true;
      });
    } else if (Math.abs(r.opacity - op) > 1e-6) {
      r.opacity = op;
      this.h.run(() => { if (r.layer && !setLayerOpacity(r.layer, op)) this.noOpacity = true; });
    }
  }

  _imgOff(id) {
    const r = this.img.get(id);
    if (!r) return;
    this.img.delete(id);
    this.h.run((al) => {
      if (!removeImageLayer(al, r.name, r.layer)) setLayerOpacity(r.layer, 0);
    });
  }

  /** User toggle / opacity of an overlay id (HiPS or vector). */
  setOverlay(id, on, opacity) {
    if (this.imgList.some((o) => o.id === id)) {
      if (on) this._imgOn(id, opacity);
      else this._imgOff(id);
    } else if (this.vector && this.vector.has(id)) {
      this.vector.set(id, on, opacity);
    }
    this._changed();
  }

  overlaysOff() {
    for (const id of Array.from(this.img.keys())) this._imgOff(id);
    if (this.vector) this.vector.apply({});
    for (const f of this.fitsList) this._fitsSet(f.id, false);
    this._changed();
  }

  overlaysDefault() {
    const wanted = wantedOverlays(null, this.defaults());
    for (const id of Array.from(this.img.keys())) if (!(id in wanted)) this._imgOff(id);
    const vec = {};
    for (const [id, op] of Object.entries(wanted)) {
      if (this.imgList.some((o) => o.id === id)) this._imgOn(id, op);
      else vec[id] = op;
    }
    if (this.vector) this.vector.apply(vec);
    for (const f of this.fitsList) this._fitsSet(f.id, false);
    this._changed();
  }

  // ---------------------------------------------------------------- FITS

  /** Turn FITS overlay `id` on/off (loads it on first use). */
  setFits(id, on) {
    this._fitsSet(id, on);
    this._changed();
  }

  _fitsSet(id, on) {
    const f = this.fitsList.find((x) => x.id === id);
    if (!f) return;
    let r = this.fits.get(id);
    if (!r) {
      if (!on) return;
      r = {status: 'idle', layer: null, on: false, name: `${this.prefix}:fits:${id}`, error: null};
      this.fits.set(id, r);
    }
    r.on = !!on;
    if (on && r.status === 'idle') this._fitsLoad(f, r);
    else if (r.status === 'ready') this._fitsShow(r);
    this.refreshPanel();
  }

  _fitsLoad(f, r) {
    r.status = 'loading';
    const url = this.resolve(f.url);
    this.h.run((al, A) => {
      if (typeof al.createImageFITS !== 'function') {
        r.status = 'error';
        r.error = 'this Aladin build cannot open FITS images';
        this.refreshPanel();
        return;
      }
      // Aladin Lite v3 runs the success/error callbacks of createImageFITS only once the image
      // has been added to the view (setOverlayImageLayer -> Image._addToView, as the legacy
      // Sickle portal does), so the layer is added as soon as it is created and the callbacks
      // only report the outcome.
      const mount = (img) => {
        if (r.layer || !img || typeof img !== 'object') return;
        r.layer = img;                  // set first: a build may call back synchronously
        r.layer = addImageLayer(al, A, img, r.name) || img;
      };
      const ok = (ra, dec, fov, img) => {
        if (r.status !== 'loading') return;
        mount(img);                     // builds that fire before createImageFITS returns
        r.status = 'ready';
        this._fitsShow(r);
        this.refreshPanel();
      };
      const fail = (err) => {
        if (r.status !== 'loading') return;
        r.status = 'error';
        r.error = err && err.message ? err.message : 'the image could not be loaded';
        if (f.auth === 'secure') r.error += ' (this image needs a login)';
        if (r.layer) removeImageLayer(al, r.name, r.layer);
        r.layer = null;
        this.toast({level: 'warn', text: `FITS overlay "${f.name || f.id}" failed: ${r.error}`});
        this.refreshPanel();
      };
      const returned = al.createImageFITS(url, {name: r.name, colormap: f.colormap || 'magma'}, ok, fail);
      if (r.status === 'loading') mount(returned);
    });
    this.refreshPanel();
  }

  _fitsShow(r) {
    this.h.run((al, A) => {
      if (!r.layer) return;
      if (hasOpacity(r.layer)) {
        setLayerOpacity(r.layer, r.on ? FITS_ON_OPACITY : 0);
      } else if (r.on) {
        addImageLayer(al, A, r.layer, r.name);
      } else {
        removeImageLayer(al, r.name, r.layer);
      }
    });
  }

  _changed() {
    this.refreshPanel();
    this.onChange(this.state());
  }

  // ---------------------------------------------------------------- panel

  /** Build (once) and return the layer panel element. */
  panel() {
    if (this.panelEl) return this.panelEl;
    const el = document.createElement('div');
    el.className = 'pk-sky-layers';
    el.setAttribute('role', 'group');
    el.setAttribute('aria-label', 'Sky layers');
    el.addEventListener('change', (e) => this._onInput(e, true));
    el.addEventListener('input', (e) => this._onInput(e, false));
    el.addEventListener('click', (e) => {
      const b = e.target.closest('button[data-act]');
      if (!b) return;
      if (b.dataset.act === 'off') this.overlaysOff();
      else if (b.dataset.act === 'defaults') this.overlaysDefault();
    });
    this.panelEl = el;
    this._render();
    return el;
  }

  _onInput(e, commit) {
    const t = e.target;
    if (!t || !t.dataset) return;
    if (t.dataset.role === 'base' && commit) this.setBase(t.value);
    else if (t.dataset.role === 'ovl' && commit) {
      const op = this.panelEl.querySelector(`input[data-role="op"][data-id="${CSS.escape(t.dataset.id)}"]`);
      this.setOverlay(t.dataset.id, t.checked, op ? +op.value : undefined);
    } else if (t.dataset.role === 'op') {
      const id = t.dataset.id;
      const box = this.panelEl.querySelector(`input[data-role="ovl"][data-id="${CSS.escape(id)}"]`);
      if (box && box.checked) {
        if (commit) this.setOverlay(id, true, +t.value);
        else if (this.img.has(id)) this._imgOn(id, +t.value);
      }
    } else if (t.dataset.role === 'fits' && commit) this.setFits(t.dataset.id, t.checked);
    else if (t.dataset.role === 'bookmark' && commit) {
      const bm = ((this.hips && this.hips.bookmarks) || [])[+t.value];
      if (bm && this.onGoto) this.onGoto({ra: +bm.ra, dec: +bm.dec, fov: +bm.fov});
      t.value = '';
    }
  }

  _render() {
    const el = this.panelEl;
    if (!el) return;
    const parts = [];
    if (this.baseList.length > 1) {
      parts.push('<label class="pk-sky-row"><span class="pk-sky-lab">Base</span><select data-role="base">' +
        this.baseList.map((b) => `<option value="${escapeHtml(b.id)}">${escapeHtml(b.name || b.id)}</option>`).join('') +
        '</select></label>');
    }
    const ovl = [];
    for (const o of this.imgList) ovl.push({id: o.id, name: o.name || o.id, group: o.group, op: clamp01(o.opacity, 1), img: true});
    if (this.vector) {
      for (const id of this.vector.ids()) {
        const lay = this.vector.layer(id);
        const op = lay.style && Number.isFinite(+lay.style.opacity) ? +lay.style.opacity : 1;
        ovl.push({id, name: lay.name || id, group: lay.group, op, img: false, note: lay.note});
      }
    }
    if (ovl.length) {
      parts.push('<div class="pk-sky-sec">Overlays</div>');
      for (const o of ovl) {
        const title = o.note ? ` title="${escapeHtml(o.note)}"` : '';
        parts.push(`<div class="pk-sky-row"${title}><label class="pk-sky-chk"><input type="checkbox" data-role="ovl" data-id="${escapeHtml(o.id)}"> ` +
          `<span>${escapeHtml(o.name)}</span></label>` +
          `<input type="range" min="0" max="1" step="0.05" value="${o.op}" data-role="op" data-id="${escapeHtml(o.id)}" aria-label="${escapeHtml(o.name)} opacity"></div>`);
      }
    }
    if (this.fitsList.length) {
      parts.push('<div class="pk-sky-sec">FITS images</div>');
      for (const f of this.fitsList) {
        const lock = f.auth === 'secure' ? ' <span class="pk-chip" title="needs a login">login</span>' : '';
        parts.push(`<div class="pk-sky-row"><label class="pk-sky-chk"><input type="checkbox" data-role="fits" data-id="${escapeHtml(f.id)}"> ` +
          `<span>${escapeHtml(f.name || f.id)}</span>${lock}</label><span class="pk-sky-fits-status" data-id="${escapeHtml(f.id)}"></span></div>`);
      }
    }
    const bms = (this.hips && this.hips.bookmarks) || [];
    if (bms.length) {
      parts.push('<label class="pk-sky-row"><span class="pk-sky-lab">Go to</span><select data-role="bookmark"><option value="">…</option>' +
        bms.map((b, i) => `<option value="${i}">${escapeHtml(b.name || `${b.ra}, ${b.dec}`)}</option>`).join('') + '</select></label>');
    }
    if (ovl.length || this.fitsList.length) {
      parts.push('<div class="pk-sky-row pk-sky-btns"><button type="button" class="pk-btn" data-act="off">Overlays off</button>' +
        '<button type="button" class="pk-btn" data-act="defaults">Defaults</button></div>');
    }
    parts.push('<div class="pk-sky-warn" hidden></div>');
    el.innerHTML = parts.join('');
    this.refreshPanel();
  }

  /** Sync the panel controls with the current state. */
  refreshPanel() {
    const el = this.panelEl;
    if (!el) return;
    const sel = el.querySelector('select[data-role="base"]');
    if (sel && this.base) sel.value = this.base;
    const st = this.state();
    const on = new Map(st.overlays.map((o) => [o.id, o.opacity]));
    for (const box of el.querySelectorAll('input[data-role="ovl"]')) {
      box.checked = on.has(box.dataset.id);
      const op = on.get(box.dataset.id);
      const r = el.querySelector(`input[data-role="op"][data-id="${CSS.escape(box.dataset.id)}"]`);
      if (r && Number.isFinite(op) && document.activeElement !== r) r.value = String(op);
    }
    for (const box of el.querySelectorAll('input[data-role="fits"]')) {
      const r = this.fits.get(box.dataset.id);
      box.checked = !!(r && r.on);
      const s = el.querySelector(`.pk-sky-fits-status[data-id="${CSS.escape(box.dataset.id)}"]`);
      if (s) {
        s.textContent = !r ? '' : r.status === 'loading' ? 'loading…' : r.status === 'error' ? 'failed' : '';
        s.title = r && r.error ? r.error : '';
        s.dataset.status = r ? r.status : 'idle';
      }
    }
    const warn = el.querySelector('.pk-sky-warn');
    if (warn) {
      warn.hidden = !this.noOpacity;
      warn.textContent = this.noOpacity ? 'This viewer build has no per-layer opacity; overlays are shown at full strength.' : '';
    }
  }

  stats() {
    const fits = {};
    for (const [id, r] of this.fits) fits[id] = {status: r.status, on: r.on};
    return {base: this.base, overlays: this.state().overlays, fits, noOpacity: this.noOpacity};
  }

  destroy() {
    for (const id of Array.from(this.img.keys())) this._imgOff(id);
    for (const [, r] of this.fits) {
      if (r.layer) this.h.run((al) => removeImageLayer(al, r.name, r.layer));
    }
    this.fits.clear();
    if (this.vector) this.vector.destroy();
    if (this.panelEl) this.panelEl.remove();
  }
}
