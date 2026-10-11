// Sky pane (ARCHITECTURE.md sections 6.8, 6.10 `sky`, 7.1 sky column, 8.2 sky_map_aladin,
// catalog_markers_on_sky, cdn_fallback, hips_layer_stacking, fits_overlay, brush_* on sky).
//
// Options: hips (dsId of a pk-hips/1 document), overlays ([sky layer ids]; default all sky
// layers of the page's overlays documents), markers ("auto" | "off"), brush (true),
// rings ([arcsec] circles around the focused source), fits ([FITS ids]; default all).
//
// Store: reads and writes `sky` {ra, dec, fov, base, overlays, fits} (view writes are debounced
// by SETTLE_MS; external writes such as the hash or a narrative beat move the view and set the
// layers), reads `focus`/`hover` (rings, pan to a focused source that is out of view), writes
// `focus`/`hover` from marker clicks, reacts to `vis:<ds>`, `sel:<ds>`, `color`, `tier:<pane>`
// and `theme`.
// Events (IMPLEMENTATION_NOTES 71): emits `brush:commit` (through sky/skybrush.js) and
// `narrative:sync` {pane} once the viewer is ready; listens to `narrative:highlight`
// {pane, ds, ids, beat, goto?} addressed to this pane id, to 'sky' or to no pane, and draws
// circles around those rows (centring on them with `goto`). A highlight received while Aladin is
// still loading is kept and drawn when the viewer is ready. Highlight rows are read with
// {pos: true}, so a highlight from the URL gets coordinates before first paint.
// Setup does not wait for the markers (their positions load after app.afterFirstPaint()): the
// view listeners and `narrative:sync` are registered as soon as the viewer is ready.
// The Aladin library loads once per page and each sky pane owns one viewer (sky/aladin.js,
// IMPLEMENTATION_NOTES 82); the viewer survives remounts. A failed load is retried once per page
// by the first sky pane that mounts after it (IMPLEMENTATION_NOTES 109). When
// Aladin cannot be loaded from the CDN or the mirror, the pane shows an error note and the rest
// of the page keeps working.

import {T0_MAX, T1_MAX, SETTLE_MS} from '../core/constants.js';
import {fmtCount, fmtRA, fmtDec, fmtAngle} from '../core/fmt.js';
import {aladinFor, errorNote, errorText, libState, retryAladinOnce} from '../sky/aladin.js';
import {SkyLayers, defaultBase} from '../sky/layers.js';
import {SkyMarkers} from '../sky/markers.js';
import {VectorOverlays, ShapeSet} from '../sky/overlays.js';
import {SkyBrush, skyView} from '../sky/skybrush.js';

export const meta = {
  type: 'sky',
  caps: ['sky_map_aladin', 'catalog_markers_on_sky', 'cdn_fallback', 'hips_layer_stacking', 'fits_overlay',
    'brush_rect', 'brush_circle', 'brush_lasso', 'brush_set_ops', 'hover_preview', 'class_coloring',
    'density_rendering'],
  needs: [],
};

const DEFAULT_VIEW = {ra: 266.4168, dec: -29.0078, fov: 1};
const VIEW_EPS = 1e-7;

function h(tag, cls, attrs) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (attrs) for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
}

function finiteView(v) {
  return v && Number.isFinite(+v.ra) && Number.isFinite(+v.dec);
}

/** Initial view: store `sky`, then hips target, then the catalog bbox, then a default. */
export function initialView(sky, hips, manifest) {
  const out = Object.assign({}, DEFAULT_VIEW);
  const bb = manifest && manifest.bbox;
  if (bb && Array.isArray(bb.ra) && Array.isArray(bb.dec)) {
    const ra = (bb.ra[0] + bb.ra[1]) / 2, dec = (bb.dec[0] + bb.dec[1]) / 2;
    const span = Math.max((bb.ra[1] - bb.ra[0]) * Math.cos((dec * Math.PI) / 180), bb.dec[1] - bb.dec[0]);
    if ([ra, dec, span].every(Number.isFinite)) Object.assign(out, {ra, dec, fov: Math.max(0.004, span * 1.2)});
  }
  const t = hips && hips.target;
  if (finiteView(t)) Object.assign(out, {ra: +t.ra, dec: +t.dec}, Number.isFinite(+t.fov) && +t.fov > 0 ? {fov: +t.fov} : {});
  if (finiteView(sky)) Object.assign(out, {ra: +sky.ra, dec: +sky.dec}, Number.isFinite(+sky.fov) && +sky.fov > 0 ? {fov: +sky.fov} : {});
  return out;
}

function sameView(a, b) {
  if (!a || !b) return false;
  let dra = Math.abs(+a.ra - +b.ra);
  if (dra > 180) dra = 360 - dra;
  const tol = Math.max(VIEW_EPS, 1e-4 * Math.min(+a.fov || 1, +b.fov || 1));
  return dra <= tol && Math.abs(+a.dec - +b.dec) <= tol &&
    (!(+a.fov > 0) || !(+b.fov > 0) || Math.abs(+a.fov - +b.fov) <= 1e-3 * Math.max(+a.fov, +b.fov));
}

function round(v, digits) {
  const k = 10 ** digits;
  return Math.round(v * k) / k;
}

function localTier(n, rt) {
  const webgl = !rt || rt.webgl2 !== false;
  if (n <= T0_MAX) return 'T0';
  if (!webgl) return 'F';
  return n <= T1_MAX ? 'T1' : 'T2';
}

/** Sky-space overlay layers of every `overlays` dataset on the page. */
async function loadVectorLayers(app, hub) {
  const out = [];
  const ds = Object.entries((app.page && app.page.datasets) || {}).filter(([, r]) => r && r.kind === 'overlays').map(([k]) => k);
  const docs = await Promise.all(ds.map((k) => hub.json(k).catch((err) => {
    console.warn(`sky: overlays ${k} unavailable: ${err && err.message}`);
    return null;
  })));
  for (const d of docs) for (const lay of (d && d.layers) || []) if ((lay.space || 'sky') === 'sky') out.push(lay);
  return out;
}

export function mount(el, ctx) {
  const {store, app, hub} = ctx;
  const cfg = ctx.config || {};
  const id = ctx.id;
  const tokens = () => (typeof ctx.theme === 'function' ? ctx.theme() : {}) || {};
  const offs = [];
  let destroyed = false;

  const root = h('div', 'pk-sky-pane');
  const stage = h('div', 'pk-sky-stage');
  const density = h('canvas', 'pk-sky-density', {'aria-hidden': 'true'});
  const notice = h('div', 'pk-sky-notice', {role: 'status', 'aria-live': 'polite'});
  notice.hidden = true;
  const coord = h('div', 'pk-sky-coord');
  const skel = h('div', 'pk-skeleton pk-sky-skel');
  skel.textContent = 'Loading the sky viewer…';
  const panelWrap = h('div', 'pk-sky-panel');
  panelWrap.hidden = true;
  stage.append(density, skel);
  root.append(stage, panelWrap, notice, coord);
  el.append(root);

  // Start the library download right away (shared by every sky pane of the page). After a
  // failed load (e.g. an earlier pane's prefetch), the first pane that mounts retries once
  // (IMPLEMENTATION_NOTES 109); aladinFor() then restarts this pane's viewer on that retry.
  retryAladinOnce(app.page).catch(() => {});

  const S = {h: null, layers: null, vector: null, markers: null, brush: null, client: null,
    rings: null, highlight: null, hl: null, hlCount: 0, written: null, writeTimer: null, error: null,
    hipsDoc: null};

  const setNotice = (text) => {
    notice.hidden = !text;
    notice.textContent = text || '';
  };

  // ------------------------------------------------------------- store writes

  const writeView = () => {
    if (!S.h || S.h.state !== 'ready') return;
    const v = S.h.view();
    if (![v.ra, v.dec, v.fov].every(Number.isFinite)) return;
    const cur = store.get('sky') || {};
    const next = {ra: round(v.ra, 6), dec: round(v.dec, 6), fov: round(v.fov, 6)};
    coord.textContent = `${fmtRA(v.ra)} ${fmtDec(v.dec)} · ${fmtAngle(v.fov)}`;
    if (sameView(cur, next)) return;
    S.written = next;
    store.set({sky: Object.assign({}, cur, next)}, {source: id});
  };

  const scheduleWrite = () => {
    clearTimeout(S.writeTimer);
    S.writeTimer = setTimeout(writeView, SETTLE_MS);
  };

  const writeLayers = (st) => {
    const cur = store.get('sky') || {};
    store.set({sky: Object.assign({}, cur, {base: st.base, overlays: st.overlays.map((o) => (Number.isFinite(o.opacity) ? {id: o.id, opacity: o.opacity} : {id: o.id})), fits: st.fits})}, {source: id});
  };

  // ------------------------------------------------------------- focus / hover / highlight

  const ringRadius = () => {
    const v = S.h ? S.h.view() : null;
    return v && Number.isFinite(v.fov) ? Math.max(0.5 / 3600, v.fov / 70) : 1 / 3600;
  };

  const drawRings = () => {
    if (!S.rings || !S.markers) return;
    const t = tokens();
    const circles = [];
    const f = store.get('focus');
    if (f && f.ds === ctx.dataset && Number.isInteger(f.i)) {
      const p = S.markers.position(f.i);
      if (p) {
        circles.push({ra: p[0], dec: p[1], r: ringRadius()});
        for (const a of Array.isArray(cfg.rings) ? cfg.rings : []) if (+a > 0) circles.push({ra: p[0], dec: p[1], r: +a / 3600});
      }
    }
    S.rings.set({circles, color: t.focus || '#e08a2c', width: 2});
    const hv = store.get('hover');
    const hc = [];
    if (hv && hv.ds === ctx.dataset && Number.isInteger(hv.i) && !(f && f.i === hv.i)) {
      const p = S.markers.position(hv.i);
      if (p) hc.push({ra: p[0], dec: p[1], r: ringRadius() * 0.8});
    }
    S.hoverRing.set({circles: hc, color: t.hover || '#1f9bb6', width: 2});
  };

  const panToFocus = (sources) => {
    if (sources.has(id) || !S.markers || !S.h || S.h.state !== 'ready') return;
    const f = store.get('focus');
    if (!f || f.ds !== ctx.dataset || !Number.isInteger(f.i)) return;
    const p = S.markers.position(f.i);
    if (!p) return;
    const px = S.h.world2pix(p[0], p[1]);
    const [w, hgt] = S.h.size();
    if (!px || px[0] < 0 || px[1] < 0 || px[0] > w || px[1] > hgt) S.h.goto({ra: p[0], dec: p[1]});
  };

  const drawHighlight = async (p) => {
    const tok = ++S.hlTok;
    S.hl = p && Array.isArray(p.ids) && p.ids.length ? p : null;
    // a highlight that arrives while the viewer is still loading is kept in S.hl and drawn at
    // the end of setup()
    if (!S.highlight || !S.h || S.h.state !== 'ready') return;
    if (!S.hl) {
      S.highlight.clear();
      S.hlCount = 0;
      return;
    }
    const ds = p.ds || ctx.dataset;
    const idx = (await app.resolveIds(ds, p.ids.map(String))).filter((i) => i >= 0);
    const client = await app.catalog(ds);
    // {pos: true}: a highlight set from the URL before first paint still gets coordinates on a
    // quantized catalog (positions are otherwise deferred until after first paint)
    const recs = await Promise.all(idx.map((i) => client.record(i, {pos: true})));
    if (tok !== S.hlTok || destroyed) return;
    const pts = recs.filter((r) => r && Number.isFinite(r.ra) && Number.isFinite(r.dec));
    const r = Math.max(0.55 / 3600, ringRadius() * 0.7);
    S.highlight.set({circles: pts.map((q) => ({ra: q.ra, dec: q.dec, r})), color: tokens().focus || '#e08a2c', width: 2});
    S.hlCount = pts.length;
    if (p.goto && pts.length && S.h) {
      const ra = pts.reduce((a, q) => a + q.ra, 0) / pts.length;
      const dec = pts.reduce((a, q) => a + q.dec, 0) / pts.length;
      S.h.goto({ra, dec});
      // goto applies once: later redraws (zoom settle, theme, ready) keep the user's view
      if (S.hl === p) S.hl = Object.assign({}, p, {goto: false});
    }
  };
  S.hlTok = 0;
  S.hlHeld = null;           // last narrative payload accepted by this pane
  S.hlGotoBeat = undefined;  // beat whose goto this pane has received

  // ------------------------------------------------------------- build

  const setup = async () => {
    let hips = null;
    if (cfg.hips) {
      hips = await hub.json(cfg.hips).catch((err) => {
        const n = h('div', 'pk-warn-note');
        n.textContent = `Sky layers unavailable (${err && err.message ? err.message : err}); showing the default survey.`;
        root.prepend(n);
        return null;
      });
    }
    S.hipsDoc = hips;
    let client = null;
    if (ctx.dataset) {
      client = await app.catalog(ctx.dataset).catch(() => null);
      if (client && !client.manifest.pos) client = null;
    }
    S.client = client;
    if (destroyed) return;
    const sky = store.get('sky') || {};
    const view = initialView(sky, hips, client && client.manifest);
    const baseEntry = (hips && ((sky.base && hips.base.find((b) => b.id === sky.base)) || defaultBase(hips))) || null;
    const survey = baseEntry ? hub.resolve(baseEntry.url).toString() : null;
    const hd = aladinFor(id, {page: app.page, view, survey});
    S.h = hd;
    hd.attach(stage);
    stage.append(density);

    const allowed = Array.isArray(cfg.overlays) ? new Set(cfg.overlays) : null;
    const vecLayers = (await loadVectorLayers(app, hub)).filter((l) => !allowed || allowed.has(l.id));
    if (destroyed) return;
    S.vector = new VectorOverlays(hd, {prefix: id, tokens});
    S.vector.setLayers(vecLayers);
    S.layers = new SkyLayers(hd, {
      hips, vector: S.vector, prefix: id,
      fitsIds: Array.isArray(cfg.fits) ? cfg.fits : null,
      resolve: (u) => hub.resolve(u).toString(),
      onChange: writeLayers,
      onGoto: (v) => hd.goto(v),
      toast: (t) => store.emit('toast', t),
    });
    S.rings = new ShapeSet(hd, `${id}:focus`);
    S.hoverRing = new ShapeSet(hd, `${id}:hover`);
    S.highlight = new ShapeSet(hd, `${id}:highlight`);

    // tools: brush modes + layers toggle
    if (client && cfg.brush !== false) {
      S.brush = new SkyBrush({h: hd, stage, app, store, ds: ctx.dataset, pane: id, client: () => Promise.resolve(client), tokens});
      if (ctx.tools) ctx.tools.append(S.brush.toolbar());
    }
    const hasLayers = S.layers.overlayIds().length || S.layers.fitsList.length || S.layers.baseList.length > 1 ||
      ((hips && hips.bookmarks) || []).length;
    if (hasLayers && ctx.tools) {
      const b = h('button', 'pk-btn pk-sky-layers-btn', {type: 'button', 'aria-expanded': 'false', title: 'Sky layers'});
      b.textContent = 'Layers';
      b.addEventListener('click', () => {
        panelWrap.hidden = !panelWrap.hidden;
        b.setAttribute('aria-expanded', String(!panelWrap.hidden));
      });
      ctx.tools.append(b);
      panelWrap.append(S.layers.panel());
      S.layersBtn = b;
    }

    try {
      await hd.ready;
    } catch (err) {
      if (destroyed) return;
      S.error = err;
      skel.remove();
      stage.classList.add('pk-sky-failed');
      stage.append(errorNote(err));
      if (app.notes) app.notes.sky_map_aladin = `Aladin Lite unavailable: ${errorText(err)}`;
      ctx.setStatus('viewer unavailable', errorText(err));
      for (const b of (ctx.tools ? ctx.tools.querySelectorAll('button') : [])) b.disabled = true;
      return;
    }
    if (destroyed) return;
    skel.remove();
    stage.dataset.aladin = hd.source || 'ready';
    // Re-read `sky`: it may have changed while Aladin was loading (deep link, narrative beat,
    // another pane), when the store listener could not reach the viewer. A remounted pane also
    // re-applies the stored view here (the viewer kept the old one).
    const cur = store.get('sky') || {};
    if (finiteView(cur)) {
      const want = initialView(cur, hips, client && client.manifest);
      if (!sameView(hd.view(), want)) hd.goto(want);
    }
    S.layers.apply(cur);

    if (client) {
      if (!store.get(`tier:${id}`)) store.set({[`tier:${id}`]: localTier(client.n, app.runtime)}, {source: id});
      S.markers = new SkyMarkers({h: hd, app, store, ds: ctx.dataset, pane: id, client, tokens,
        onNotice: setNotice, canvas: density, mode: cfg.markers === 'off' ? 'off' : 'auto'});
      // Not awaited: the markers load positions after app.afterFirstPaint(), which itself waits
      // for the visible panes (this one included) to finish loading. The view listeners and the
      // narrative:sync emit below are registered right away.
      S.markers.refresh({force: true}).then(() => {
        if (!destroyed) drawRings();
      }, markersErr);
      ctx.setStatus(`${fmtCount(client.n)} sources`);
    }

    offs.push(hd.on('positionChanged', () => {
      if (S.markers) S.markers.viewChanged();
      scheduleWrite();
    }));
    offs.push(hd.on('zoomChanged', () => {
      if (S.markers) S.markers.viewChanged();
      scheduleWrite();
      drawRingsSoon();
    }));
    offs.push(hd.on('resizeChanged', () => { if (S.markers) S.markers.viewChanged(); }));
    const v = hd.view();
    if ([v.ra, v.dec, v.fov].every(Number.isFinite)) coord.textContent = `${fmtRA(v.ra)} ${fmtDec(v.dec)} · ${fmtAngle(v.fov)}`;
    if (S.hl) drawHighlight(S.hl).catch(highlightErr);
    store.emit('narrative:sync', {pane: id, type: 'sky'});
  };

  function markersErr(err) {
    if (destroyed || (err && err.name === 'AbortError')) return;
    console.warn(`sky ${id}: catalog markers failed`, err);
    setNotice(`Catalog markers unavailable: ${err && err.message ? err.message : err}`);
  }

  function highlightErr(err) {
    if (err && err.name === 'AbortError') return;
    console.warn(`sky ${id}: narrative highlight failed`, err);
  }

  let ringTimer = null;
  const drawRingsSoon = () => {
    clearTimeout(ringTimer);
    ringTimer = setTimeout(() => {
      drawRings();
      if (S.hl) drawHighlight(S.hl).catch(highlightErr);
    }, SETTLE_MS);
  };

  // ------------------------------------------------------------- store listeners

  offs.push(store.on(['sky'], (changed, st, sources) => {
    if (sources.size === 1 && sources.has(id)) return;
    if (!S.h || S.h.state !== 'ready') return;
    const sky = st.get('sky') || {};
    if (finiteView(sky)) {
      const v = S.h.view();
      if (!sameView(v, sky)) S.h.goto({ra: +sky.ra, dec: +sky.dec, fov: +sky.fov});
    }
    if (S.layers) S.layers.apply(sky);
  }));
  if (ctx.dataset) {
    offs.push(store.on([`vis:${ctx.dataset}`, `tier:${id}`], () => {
      if (S.markers) S.markers.refresh({force: true});
    }));
    offs.push(store.on(['color'], () => {
      if (S.markers) S.markers.restyle();
    }));
    offs.push(store.on([`sel:${ctx.dataset}`], () => {
      if (S.markers) S.markers.refreshSelected();
    }));
    offs.push(store.on(['focus', 'hover'], (changed, st, sources) => {
      if (changed.has('focus')) panToFocus(sources);
      drawRings();
    }));
  }
  offs.push(store.on(['theme'], () => {
    if (S.markers) S.markers.restyle();
    if (S.vector) S.vector.retheme();
    drawRings();
    if (S.hl) drawHighlight(S.hl).catch(highlightErr);
  }));
  offs.push(store.onEvent('narrative:highlight', (p) => {
    if (!p || typeof p !== 'object') return;
    if (p.beat !== S.hlGotoBeat) S.hlGotoBeat = undefined;
    const elsewhere = (p.pane && p.pane !== id && p.pane !== 'sky') ||
      (p.ds && ctx.dataset && p.ds !== ctx.dataset && !(app.page.datasets || {})[p.ds]);
    if (elsewhere) {
      // the next beat targets another pane: its rings replace this pane's
      if (S.hlHeld && p.beat !== S.hlHeld.beat) {
        S.hlHeld = null;
        if (S.hl) drawHighlight(null).catch(highlightErr);
      }
      return;
    }
    S.hlHeld = p;
    if (p.goto) {
      // a replay of the same beat (narrative:sync answer) does not move the view again
      if (p.beat != null && p.beat === S.hlGotoBeat) p = Object.assign({}, p, {goto: false});
      else S.hlGotoBeat = p.beat;
    }
    drawHighlight(p).catch(highlightErr);
  }));

  const ready = setup().catch((err) => {
    if (destroyed) return;
    console.error(`sky pane ${id} failed`, err);
    skel.remove();
    const n = h('div', 'pk-error-note', {role: 'alert'});
    n.textContent = `The sky panel could not start: ${err && err.message ? err.message : err}`;
    stage.append(n);
  });

  return {
    ready,
    resize() {
      if (S.markers && S.markers.density) S.markers._paintDensity();
    },
    onVisible(v) {
      if (S.markers) S.markers.setVisible(v);
    },
    async brush(shape, op = 'replace') {
      await ready;
      if (!S.client) throw new Error(`sky pane ${id} has no catalog with positions`);
      if (S.brush) return S.brush.commit(shape, op);
      const sel = await app.selectionFor(ctx.dataset);
      const res = await S.client.brush(skyView(id), shape);
      const {kind, ...geom} = shape;
      sel.apply(res.hit, op, res.drawable, {op, kind, view: {ds: ctx.dataset, x: 'ra', y: 'dec'}, shape: geom});
      store.emit('brush:commit', {pane: id, ds: ctx.dataset, shape, op, count: sel.count});
      return sel.count;
    },
    stats() {
      const lib = libState();
      return {
        aladin: S.error ? 'error' : (S.h && S.h.state === 'ready' ? (S.h.source || lib.source) : lib.state),
        view: S.h && S.h.state === 'ready' ? S.h.view() : null,
        markers: S.markers ? S.markers.stats() : null,
        layers: S.layers ? S.layers.stats() : null,
        overlays: S.vector ? S.vector.stats() : null,
        highlight: S.hlCount,
        brushMode: S.brush ? S.brush.mode : null,
      };
    },
    /** Ids of the overlays currently shown (HiPS image overlays and vector layers). */
    layers() {
      return S.layers ? S.layers.state().overlays.map((o) => o.id) : [];
    },
    destroy() {
      destroyed = true;
      clearTimeout(S.writeTimer);
      clearTimeout(ringTimer);
      for (const off of offs.splice(0)) off();
      if (S.markers) S.markers.destroy();
      if (S.brush) S.brush.destroy();
      if (S.layers) S.layers.destroy();
      for (const s of [S.rings, S.hoverRing, S.highlight]) if (s) s.destroy();
      if (S.layersBtn) S.layersBtn.remove();
      if (S.h) S.h.detach();
      root.remove();
    },
  };
}
