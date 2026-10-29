// Aladin Lite v3 loader and instance registry (ARCHITECTURE.md section 8.2: sky_map_aladin,
// cdn_fallback, hips_layer_stacking).
//
// - loadAladin(page): the library is loaded once per page from page.aladin.cdn, then
//   page.aladin.mirror (core/loadscript.js, ALADIN_TIMEOUT_MS per source), then A.init is
//   awaited within what is left of that source's ALADIN_TIMEOUT_MS (initBudget). The promise
//   rejects with a LoadScriptError (or an init timeout) when every source fails; callers show
//   errorNote() and the rest of the page keeps working.
// - aladinFor(owner, opts): one Aladin instance per owning pane, created on first use and kept
//   for the life of the page (a remount, pop-out or tab switch re-attaches the same host div
//   instead of creating a new viewer). Commands issued before the viewer is ready are queued
//   with handle.run(fn) and replayed in order. IMPLEMENTATION_NOTES 82: the library loads once
//   per page and each sky pane owns one viewer, so a layout with one sky-type pane has one
//   viewer and a layout with two (e.g. sky + wavelength) has two, since one Aladin canvas shows
//   one view. A failed library load is also kept for the page: later panes get the same
//   rejection without another download attempt (loadAladin(page, {retry: true}) starts over).
// - IMPLEMENTATION_NOTES 109: a failed fire-and-forget prefetch does not poison later loads.
//   retryAladinOnce(page) is what a sky pane calls when it mounts: when the page's load has
//   failed and no retry was made yet, it starts the one retry (loadAladin(page, {retry: true}));
//   otherwise it returns the current load. aladinFor() calls it too, so a wavelength pane (or
//   any other viewer owner) that is the first to need Aladin after a failure also retries, and
//   it restarts a handle that failed on an earlier load once a retry is under way, so a
//   remounted pane gets a viewer from the retry.
// - Feature detection for the API drift between Aladin v3 builds: layer opacity
//   (setOpacity / setAlpha / setOptions / opacity property), HiPS construction (A.HiPS /
//   createImageSurvey / newImageSurvey), base layer (setBaseImageLayer / setImageSurvey),
//   removal (removeOverlay / removeLayer / removeCatalog / removeAll), setFoV / setFov and
//   getFov returning a number or [fx, fy].

import {loadScript} from '../core/loadscript.js';
import {ALADIN_TIMEOUT_MS} from '../core/constants.js';

export const DEFAULT_CDN = 'https://aladin.cds.unistra.fr/AladinLite/api/v3/latest/aladin.js';
export const DEFAULT_MIRROR = 'https://data.rc.ufl.edu/pub/adamginsburg/jwst-gc/aladin.js';

const D2R = Math.PI / 180;

let lib = null;                       // {promise, state, url, index, source, error}
let retried = false;                  // the page's one retry after a failed load was used
const handles = new Map();            // owner -> AladinHandle

/** [cdn, mirror] for a page.json (defaults when the page has no `aladin`). */
export function aladinSources(page) {
  const al = (page && page.aladin) || {};
  const out = [al.cdn || DEFAULT_CDN, al.mirror || DEFAULT_MIRROR];
  return out.filter((u, i) => u && out.indexOf(u) === i);
}

function libReady() {
  return typeof window !== 'undefined' && !!window.A && typeof window.A.aladin === 'function';
}

function withTimeout(p, ms, what) {
  let timer = null;
  const t = new Promise((resolve, reject) => {
    timer = setTimeout(() => reject(new Error(`${what} did not finish within ${ms} ms`)), ms);
  });
  return Promise.race([p, t]).finally(() => clearTimeout(timer));
}

function now() {
  return typeof performance !== 'undefined' && performance.now ? performance.now() : Date.now();
}

/**
 * Time left for A.init after the script from `url` loaded: one deadline of `timeout` ms covers
 * the script load and the initialisation of the source that succeeded, with a floor of a quarter
 * of `timeout` so a slow but successful download still gets a short init window. The download
 * time comes from Resource Timing, else from `elapsed` (which also counts any failed source
 * before it, so it can only shorten the budget down to the floor). Worst case per source:
 * 1.25 x timeout.
 */
export function initBudget(timeout, url, elapsed) {
  let loadMs = elapsed;
  if (url && typeof performance !== 'undefined' && typeof performance.getEntriesByName === 'function') {
    let abs = url;
    try {
      abs = new URL(url, typeof location !== 'undefined' ? location.href : undefined).href;
    } catch (err) {
      if (!(err instanceof TypeError)) throw err;
    }
    const ent = performance.getEntriesByName(abs);
    const last = ent.length ? ent[ent.length - 1] : null;
    if (last && Number.isFinite(last.duration) && last.duration > 0) loadMs = last.duration;
  }
  const left = timeout - (Number.isFinite(loadMs) ? loadMs : 0);
  return Math.max(Math.round(timeout / 4), Math.round(left));
}

/**
 * Load Aladin Lite once per page. Resolves {A, url, index, source: 'cdn'|'mirror'|'preloaded'}.
 * opts.timeout overrides ALADIN_TIMEOUT_MS (per source). The first call's promise is returned to
 * every later call, including a rejected one; opts.retry = true replaces a failed load.
 */
export function loadAladin(page, opts = {}) {
  if (lib && !(opts.retry && lib.state === 'error')) return lib.promise;
  const urls = aladinSources(page);
  const timeout = opts.timeout ?? ALADIN_TIMEOUT_MS;
  const st = {state: 'loading', url: null, index: -1, source: null, error: null, promise: null};
  st.promise = (async () => {
    const t0 = now();
    const r = await loadScript(urls, {timeout, test: libReady});
    const A = window.A;
    const init = typeof A.init === 'function' ? A.init() : A.init;
    if (init && typeof init.then === 'function') {
      await withTimeout(Promise.resolve(init), initBudget(timeout, r.url, now() - t0), 'Aladin Lite initialisation');
    }
    st.state = 'ready';
    st.url = r.url;
    st.index = r.index;
    st.source = r.index < 0 ? 'preloaded' : r.index === 0 ? 'cdn' : 'mirror';
    return {A, url: r.url, index: r.index, source: st.source};
  })();
  st.promise.catch((err) => {
    st.state = 'error';
    st.error = err;
  });
  lib = st;
  return st.promise;
}

/**
 * The load a mounting sky pane waits on (IMPLEMENTATION_NOTES 109). When the page's library load
 * has failed and the page has not retried yet, starts the one retry (loadAladin(page,
 * {retry: true}) with `opts`); otherwise loadAladin(page, opts). Later failures are kept for the
 * page like any other load, so a page with a blocked CDN and mirror makes at most two attempts.
 */
export function retryAladinOnce(page, opts = {}) {
  if (lib && lib.state === 'error' && !retried) {
    retried = true;
    return loadAladin(page, Object.assign({}, opts, {retry: true}));
  }
  return loadAladin(page, opts);
}

/** True once the page has used its one retry after a failed load. */
export function aladinRetried() {
  return retried;
}

/** Forget the page's library load, its retry and every handle (unit tests). */
export function resetAladin() {
  lib = null;
  retried = false;
  handles.clear();
}

/** {state: 'idle'|'loading'|'ready'|'error', source, url, error} of the page's library load. */
export function libState() {
  if (!lib) return {state: 'idle', source: null, url: null, error: null};
  return {state: lib.state, source: lib.source, url: lib.url, error: lib.error};
}

/** Human-readable reason for a failed load. */
export function errorText(err) {
  if (!err) return 'unknown error';
  if (Array.isArray(err.attempts) && err.attempts.length) {
    return err.attempts.map((a) => `${hostOf(a.url)}: ${a.reason}`).join('; ');
  }
  return err.message ? String(err.message) : String(err);
}

function hostOf(url) {
  try {
    return new URL(url, typeof location !== 'undefined' ? location.href : undefined).host || url;
  } catch (err) {
    if (err instanceof TypeError) return url;
    throw err;
  }
}

/** The error note shown in a sky pane when the viewer cannot load (page stays usable). */
export function errorNote(err, {what = 'Sky viewer'} = {}) {
  const note = document.createElement('div');
  note.className = 'pk-error-note pk-sky-error';
  note.setAttribute('role', 'alert');
  const b = document.createElement('strong');
  b.textContent = `${what} unavailable.`;
  note.append(b, ' Aladin Lite could not be loaded from the CDS CDN or the mirror, so the sky map is off. ' +
    'The rest of this page works as usual.');
  const d = document.createElement('div');
  d.className = 'pk-sky-error-detail';
  d.textContent = errorText(err);
  note.append(d);
  return note;
}

// ------------------------------------------------------------------ feature detection

/** True when `layer` exposes some per-layer opacity control. */
export function hasOpacity(layer) {
  if (!layer || typeof layer !== 'object') return false;
  return typeof layer.setOpacity === 'function' || typeof layer.setAlpha === 'function' ||
    typeof layer.setOptions === 'function' || 'opacity' in layer;
}

/** Set a layer's opacity with whichever API the build has; false when none exists. */
export function setLayerOpacity(layer, a) {
  if (!layer || typeof layer !== 'object') return false;
  const v = Math.max(0, Math.min(1, +a));
  if (typeof layer.setOpacity === 'function') { layer.setOpacity(v); return true; }
  if (typeof layer.setAlpha === 'function') { layer.setAlpha(v); return true; }
  if (typeof layer.setOptions === 'function') { layer.setOptions({opacity: v}); return true; }
  if ('opacity' in layer) { layer.opacity = v; return true; }
  return false;
}

/** Current opacity of a layer (null when unknown). */
export function layerOpacity(layer) {
  if (!layer || typeof layer !== 'object') return null;
  if (typeof layer.getOpacity === 'function') {
    const v = +layer.getOpacity();
    if (Number.isFinite(v)) return v;
  }
  if (typeof layer.getAlpha === 'function') {
    const v = +layer.getAlpha();
    if (Number.isFinite(v)) return v;
  }
  if ('opacity' in layer && Number.isFinite(+layer.opacity)) return +layer.opacity;
  if (layer.options && Number.isFinite(+layer.options.opacity)) return +layer.options.opacity;
  return null;
}

// A CDS HiPS identifier such as `CDS/P/DSS2/color`, `P/2MASS/color` or `ov-gso/P/GLIMPSE/irac1`
// (no scheme): Aladin resolves it through the MocServer, so it stays as written. Bundle paths never
// take this shape (they start with data/, _res/ or {assets}).
const SURVEY_ID = /^(?:[A-Za-z0-9][\w.+-]*\/)?P\/[^\s?#:]+$/;

/** Whether `url` is a CDS HiPS identifier. */
export function isSurveyId(url) {
  return typeof url === 'string' && SURVEY_ID.test(url) && !/^(?:data|_res)\//.test(url);
}

/** A pk-hips/1 layer url for Aladin: CDS ids pass through, everything else resolves with `resolve`. */
export function surveyUrl(url, resolve) {
  if (isSurveyId(url)) return url;
  return resolve ? String(resolve(url)) : url;
}

/** A HiPS image layer object for `url` (null when the build only takes URL strings). */
export function makeHiPS(al, A, url, name, opts = {}) {
  const o = Object.assign({name}, opts);
  if (A && typeof A.HiPS === 'function') return A.HiPS(url, o);
  if (al && typeof al.createImageSurvey === 'function') return al.createImageSurvey(url, name, url, 'equatorial', opts.maxOrder);
  if (al && typeof al.newImageSurvey === 'function') return al.newImageSurvey(url, o);
  return null;
}

/** Set the base (background) layer. Returns the layer object or null. */
export function setBaseLayer(al, A, url, name, opts = {}) {
  const layer = makeHiPS(al, A, url, name, opts);
  if (typeof al.setBaseImageLayer === 'function') {
    const r = al.setBaseImageLayer(layer || url);
    return r && typeof r === 'object' ? r : layer;
  }
  if (typeof al.setImageSurvey === 'function') {
    const r = al.setImageSurvey(layer || url);
    return r && typeof r === 'object' ? r : layer;
  }
  return null;
}

/** Add (or replace) a named overlay image layer from a URL or a layer object. */
export function addImageLayer(al, A, src, name, opts = {}) {
  if (typeof al.setOverlayImageLayer !== 'function') return null;
  const layer = typeof src === 'string' ? makeHiPS(al, A, src, name, opts) : src;
  const r = al.setOverlayImageLayer(layer || src, name);
  if (r && typeof r === 'object') return r;
  if (layer) return layer;
  return typeof al.getOverlayImageLayer === 'function' ? al.getOverlayImageLayer(name) : null;
}

/** Remove a named overlay image layer; false when the build cannot. */
export function removeImageLayer(al, name, layer) {
  if (typeof al.removeImageLayer === 'function') { al.removeImageLayer(name); return true; }
  if (typeof al.removeLayer === 'function') { al.removeLayer(layer || name); return true; }
  return false;
}

/** Remove a catalog or graphic overlay object (MSATA dropLayer order), emptying it as well. */
export function dropObject(al, obj) {
  if (!al || !obj) return;
  if (typeof obj.removeAll === 'function') obj.removeAll();
  if (typeof al.removeOverlay === 'function') al.removeOverlay(obj);
  else if (typeof al.removeCatalog === 'function') al.removeCatalog(obj);
  else if (typeof al.removeLayer === 'function') al.removeLayer(obj);
}

/** Show or hide a catalog / overlay object. */
export function showObject(obj, on) {
  if (!obj) return;
  if (on && typeof obj.show === 'function') obj.show();
  else if (!on && typeof obj.hide === 'function') obj.hide();
}

/** {ra, dec, fov, fovy} of the viewer (NaN fields when unavailable). */
export function viewOf(al) {
  let ra = NaN, dec = NaN, fov = NaN, fovy = NaN;
  if (al && typeof al.getRaDec === 'function') {
    const c = al.getRaDec();
    if (c && c.length >= 2) { ra = +c[0]; dec = +c[1]; }
  }
  if (al && typeof al.getFov === 'function') {
    const f = al.getFov();
    if (Array.isArray(f)) { fov = +f[0]; fovy = +(f[1] ?? f[0]); } else { fov = +f; fovy = +f; }
  }
  return {ra, dec, fov, fovy};
}

/** Move the viewer to {ra, dec, fov}; any field may be missing. */
export function gotoView(al, v) {
  if (!al || !v) return;
  if (Number.isFinite(v.ra) && Number.isFinite(v.dec) && typeof al.gotoRaDec === 'function') al.gotoRaDec(v.ra, v.dec);
  if (Number.isFinite(v.fov) && v.fov > 0) {
    if (typeof al.setFoV === 'function') al.setFoV(v.fov);
    else if (typeof al.setFov === 'function') al.setFov(v.fov);
  }
}

/** [ra, dec] at a pixel of the viewer, or null outside the projection. */
export function pixToWorld(al, x, y) {
  if (!al || typeof al.pix2world !== 'function') return null;
  const r = al.pix2world(x, y);
  if (!r || !Number.isFinite(+r[0]) || !Number.isFinite(+r[1])) return null;
  return [+r[0], +r[1]];
}

/** [x, y] pixel of a sky position, or null when it is not visible. */
export function worldToPix(al, ra, dec) {
  if (!al || typeof al.world2pix !== 'function') return null;
  const r = al.world2pix(ra, dec);
  if (!r || !Number.isFinite(+r[0]) || !Number.isFinite(+r[1])) return null;
  return [+r[0], +r[1]];
}

/** Great-circle separation in degrees. */
export function separation(ra1, dec1, ra2, dec2) {
  const p1 = dec1 * D2R, p2 = dec2 * D2R;
  const dp = p2 - p1, dl = (ra2 - ra1) * D2R;
  const s = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return (2 * Math.asin(Math.min(1, Math.sqrt(s)))) / D2R;
}

// ------------------------------------------------------------------ handle

class AladinHandle {
  constructor(owner, opts) {
    this.owner = owner;
    this.opts = opts;
    this.host = document.createElement('div');
    this.host.className = 'pk-aladin-host';
    this.host.dataset.owner = owner;
    this.A = null;
    this.al = null;
    this.state = 'loading';
    this.error = null;
    this.source = null;
    this.initialSurvey = opts.survey || null;
    this._queue = [];
    this._listeners = new Map();      // event -> Set(fn)
    this._bound = new Set();          // events registered on the Aladin instance
    this._load = null;                // the library load promise this handle waited on
    this.ready = this._start();
    this.ready.catch(() => {});
  }

  /**
   * Start over after a failed load (the viewer was never created): wait on the page's current
   * library load with `opts` (the remounting pane's view and survey). No-op unless in 'error'.
   */
  restart(opts) {
    if (this.state !== 'error') return this.ready;
    if (opts) this.opts = opts;
    this.initialSurvey = this.opts.survey || null;
    this.state = 'loading';
    this.error = null;
    this.ready = this._start();
    this.ready.catch(() => {});
    return this.ready;
  }

  async _start() {
    try {
      this._load = loadAladin(this.opts.page, {timeout: this.opts.timeout});
      const r = await this._load;
      this.A = r.A;
      this.source = r.source;
      const o = this.opts.view || {};
      const init = Object.assign({
        cooFrame: 'icrs',
        showReticle: false,
        showCooGrid: false,
        showLayersControl: false,
        showSettingsControl: false,
        showFullscreenControl: false,
        showGotoControl: false,
        showShareControl: false,
        showSimbadPointerControl: false,
        showProjectionControl: false,
        showFrame: false,
        showCooLocation: true,
        showZoomControl: true,
      }, this.opts.aladinOptions || {});
      if (Number.isFinite(o.ra) && Number.isFinite(o.dec)) init.target = `${o.ra} ${o.dec}`;
      if (Number.isFinite(o.fov) && o.fov > 0) init.fov = o.fov;
      if (this.opts.survey) init.survey = this.opts.survey;
      this.al = this.A.aladin(this.host, init);
      this.state = 'ready';
      for (const ev of this._listeners.keys()) this._bind(ev);
      const q = this._queue.splice(0);
      for (const fn of q) this._exec(fn);
      return this;
    } catch (err) {
      this.state = 'error';
      this.error = err;
      this._queue.length = 0;
      throw err;
    }
  }

  _exec(fn) {
    try {
      return fn(this.al, this.A, this);
    } catch (err) {
      // API drift between Aladin builds shows up as TypeErrors (missing methods); keep the
      // pane alive and report it. Any other error is a bug and propagates.
      if (err instanceof TypeError) {
        console.warn(`aladin(${this.owner}): ${err.message}`);
        return undefined;
      }
      throw err;
    }
  }

  /** Run fn(al, A, handle) now when ready, else queue it (dropped if loading fails). */
  run(fn) {
    if (this.state === 'ready') return this._exec(fn);
    if (this.state === 'loading') this._queue.push(fn);
    return undefined;
  }

  /** Put the viewer host inside `el` (moving it from a previous parent). */
  attach(el) {
    if (this.host.parentNode !== el) el.appendChild(this.host);
  }

  /** Take the host out of the page (it is kept for a later attach). */
  detach() {
    if (this.host.parentNode) this.host.parentNode.removeChild(this.host);
  }

  _bind(ev) {
    if (!this.al || this._bound.has(ev) || typeof this.al.on !== 'function') return;
    this._bound.add(ev);
    this.al.on(ev, (...args) => {
      const set = this._listeners.get(ev);
      if (!set) return;
      for (const fn of Array.from(set)) fn(...args);
    });
  }

  /** Subscribe to an Aladin event; returns an unsubscribe function. */
  on(ev, fn) {
    if (!this._listeners.has(ev)) this._listeners.set(ev, new Set());
    this._listeners.get(ev).add(fn);
    if (this.state === 'ready') this._bind(ev);
    return () => {
      const s = this._listeners.get(ev);
      if (s) s.delete(fn);
    };
  }

  view() {
    return this.al ? viewOf(this.al) : {ra: NaN, dec: NaN, fov: NaN, fovy: NaN};
  }

  goto(v) {
    this.run((al) => gotoView(al, v));
  }

  size() {
    const r = this.host.getBoundingClientRect();
    return [r.width, r.height];
  }

  pix2world(x, y) {
    return this.al ? pixToWorld(this.al, x, y) : null;
  }

  world2pix(ra, dec) {
    return this.al ? worldToPix(this.al, ra, dec) : null;
  }
}

/**
 * The Aladin handle owned by `owner` (a pane id), created on first call. Every pane that hosts a
 * viewer (sky, wavelength) comes through here, so this is where the page's one retry after a
 * failed load starts (retryAladinOnce, IMPLEMENTATION_NOTES 109): the first call after a failed
 * load, from whichever pane type, retries before creating or restarting its handle. A handle
 * that failed on an earlier library load is restarted with `opts` when the page has since
 * started a new load that is not in error (a retry is loading or has succeeded).
 * opts: {page, view: {ra, dec, fov}, survey, aladinOptions, timeout}.
 */
export function aladinFor(owner, opts = {}) {
  if (lib && lib.state === 'error' && !retried) {
    retryAladinOnce(opts.page, {timeout: opts.timeout}).catch(() => {});
  }
  let h = handles.get(owner);
  if (!h) {
    h = new AladinHandle(owner, opts);
    handles.set(owner, h);
  } else if (h.state === 'error' && lib && lib.state !== 'error' && lib.promise !== h._load) {
    h.restart(opts);
  }
  return h;
}

/** Handles created on this page (for tests and stats). */
export function aladinHandles() {
  return Array.from(handles.values());
}
