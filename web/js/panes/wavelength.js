// Wavelength pane (ARCHITECTURE.md sections 6.8, 6.10 `rung`, 6.13 options `hips`, `speed`,
// `markers`; legacy W51_Portal_Wavelength.py:239-264).
//
// A sky viewer with a rail of HiPS "rungs" (hips.rungs, else the base surveys). The slider
// position `pos` is continuous in [0, n-1]. With a per-layer opacity API the pane cross-fades
// asymmetrically: the lower rung floor(pos) is opaque and the upper rung ceil(pos) sits above it
// with opacity pos - floor(pos); every other mounted rung is transparent. Builds without an
// opacity API fall back to a step mode that shows only the nearest rung (the others are removed)
// and say so in a warning line.
//
// Controls: notches (click), slider, play/pause (rAF, stops at the last rung), speed select
// (1.2 s per rung divided by the speed factor), reset view. Keys inside the pane: ArrowUp/Right
// next rung, ArrowDown/Left previous rung, Space play/pause.
// Store: writes `rung` (nearest rung id) and `sky` {ra, dec, fov} after pans (SETTLE_MS);
// reads both (hash or other panes). Optional catalog markers via sky/markers.js.

import {SETTLE_MS} from '../core/constants.js';
import {fmtCount, fmtRA, fmtDec, fmtAngle, escapeHtml} from '../core/fmt.js';
import {aladinFor, addImageLayer, errorNote, errorText, hasOpacity, libState, loadAladin, removeImageLayer,
  setLayerOpacity} from '../sky/aladin.js';
import {defaultBase} from '../sky/layers.js';
import {SkyMarkers} from '../sky/markers.js';
import {initialView} from './sky.js';

export const meta = {
  type: 'wavelength',
  caps: ['sky_map_aladin', 'catalog_markers_on_sky', 'cdn_fallback', 'hips_layer_stacking'],
  needs: [],
};

export const BASE_SECONDS_PER_RUNG = 1.2;
export const SPEEDS = Object.freeze([{label: 'Slow', factor: 0.5}, {label: 'Medium', factor: 1}, {label: 'Fast', factor: 2}]);

function h(tag, cls, attrs) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (attrs) for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
}

/** Rungs of a pk-hips/1 document: `rungs`, else the base surveys, as [{id, label, url, desc}]. */
export function rungsOf(hips) {
  const src = hips && Array.isArray(hips.rungs) && hips.rungs.length ? hips.rungs : ((hips && hips.base) || []);
  return src.filter((r) => r && r.url).map((r, k) => ({
    id: String(r.id ?? `r${k}`), label: r.label || r.name || String(r.id ?? k), url: r.url, desc: r.desc || '',
    maxOrder: r.maxOrder, format: r.format, r_um: r.r_um, g_um: r.g_um, b_um: r.b_um,
  }));
}

/**
 * Opacities for position `pos` (asymmetric cross-fade): {lo, hi, frac, alpha: Map index -> a}
 * over the `mounted` indices. lo is opaque, hi gets frac, every other mounted rung 0.
 */
export function fadePlan(pos, n, mounted = []) {
  const p = Math.max(0, Math.min(n - 1, pos));
  const lo = Math.floor(p), hi = Math.min(n - 1, Math.ceil(p));
  const frac = hi === lo ? 0 : p - lo;
  const alpha = new Map();
  for (const i of mounted) alpha.set(i, 0);
  alpha.set(lo, 1);
  if (hi !== lo) alpha.set(hi, frac);
  return {lo, hi, frac, alpha};
}

/** Step fallback: the one rung kept for `pos`. */
export function stepRung(pos, n) {
  const p = Math.max(0, Math.min(n - 1, pos));
  const lo = Math.floor(p), hi = Math.min(n - 1, Math.ceil(p));
  return p - lo > 0.5 ? hi : lo;
}

function speedFactor(cfg) {
  let q = null;
  if (typeof location !== 'undefined') q = new URLSearchParams(location.search).get('speed');
  const v = +(q ?? cfg.speed ?? 1);
  return Number.isFinite(v) && v > 0 ? v : 1;
}

export function mount(el, ctx) {
  const {store, app, hub} = ctx;
  const cfg = ctx.config || {};
  const id = ctx.id;
  const tokens = () => (typeof ctx.theme === 'function' ? ctx.theme() : {}) || {};
  const offs = [];
  let destroyed = false;

  const root = h('div', 'pk-wl-pane', {tabindex: '0'});
  const stage = h('div', 'pk-sky-stage pk-wl-stage');
  const density = h('canvas', 'pk-sky-density', {'aria-hidden': 'true'});
  const skel = h('div', 'pk-skeleton pk-sky-skel');
  skel.textContent = 'Loading the sky viewer…';
  stage.append(density, skel);
  const notice = h('div', 'pk-sky-notice', {role: 'status', 'aria-live': 'polite'});
  notice.hidden = true;

  const rail = h('div', 'pk-wl-rail');
  const labels = h('div', 'pk-wl-labels');
  const now = h('span', 'pk-wl-now');
  const fade = h('span', 'pk-wl-fade');
  labels.append(now, fade);
  const track = h('div', 'pk-wl-track');
  const notches = h('div', 'pk-wl-notches', {role: 'group', 'aria-label': 'Wavelength rungs'});
  const slider = h('input', 'pk-wl-slider', {type: 'range', min: '0', max: '0', step: '0.001', value: '0',
    'aria-label': 'Wavelength position'});
  track.append(slider, notches);
  const ctrls = h('div', 'pk-wl-ctrls');
  const play = h('button', 'pk-btn pk-wl-play', {type: 'button', 'aria-pressed': 'false', title: 'Play (Space)'});
  play.textContent = 'Play';
  const speedSel = h('select', 'pk-wl-speed', {'aria-label': 'Playback speed'});
  const reset = h('button', 'pk-btn pk-wl-reset', {type: 'button', title: 'Reset the view'});
  reset.textContent = 'Reset view';
  ctrls.append(play, speedSel, reset);
  const note = h('div', 'pk-wl-note');
  const warn = h('div', 'pk-wl-warn pk-warn-note');
  warn.hidden = true;
  rail.append(labels, track, ctrls, note, warn);
  root.append(stage, notice, rail);
  el.append(root);

  loadAladin(app.page).catch(() => {});

  const sf = speedFactor(cfg);
  const speeds = SPEEDS.slice();
  if (!speeds.some((s) => Math.abs(s.factor - sf) < 1e-9)) speeds.push({label: `${sf}×`, factor: sf});
  speeds.sort((a, b) => a.factor - b.factor);
  for (const s of speeds) {
    const o = h('option', null, {value: String(s.factor)});
    o.textContent = s.label;
    if (Math.abs(s.factor - sf) < 1e-9) o.selected = true;
    speedSel.append(o);
  }

  const S = {h: null, rungs: [], pos: 0, mounted: new Map(), seq: 0, canFade: null, playing: false, raf: 0,
    applyQueued: false, markers: null, hips: null, home: null, writeTimer: null, rungTimer: null, error: null};

  const setNotice = (text) => {
    notice.hidden = !text;
    notice.textContent = text || '';
  };
  const nearest = () => Math.max(0, Math.min(S.rungs.length - 1, Math.round(S.pos)));

  // ------------------------------------------------------------- rung layers

  const layerName = (i) => `${id}:rung:${i}`;

  const mountRung = (al, A, i) => {
    let r = S.mounted.get(i);
    if (r) return r;
    const g = S.rungs[i];
    const url = hub.resolve(g.url).toString();
    const layer = addImageLayer(al, A, url, layerName(i), {maxOrder: g.maxOrder, imgFormat: g.format});
    r = {layer, alpha: null, seq: ++S.seq};
    S.mounted.set(i, r);
    if (S.canFade === null) {
      S.canFade = !!layer && hasOpacity(layer);
      if (!S.canFade) {
        warn.hidden = false;
        warn.textContent = 'This Aladin build has no layer opacity control, so rungs switch in steps instead of fading.';
      }
    }
    if (S.canFade && !setLayerOpacity(layer, 0)) S.canFade = false;
    else if (S.canFade) r.alpha = 0;
    return r;
  };

  const unmountRung = (al, i) => {
    const r = S.mounted.get(i);
    if (!r) return;
    S.mounted.delete(i);
    removeImageLayer(al, layerName(i), r.layer);
  };

  const setAlpha = (r, a) => {
    if (r.alpha !== null && Math.abs(r.alpha - a) < 1e-4) return;
    if (setLayerOpacity(r.layer, a)) r.alpha = a;
  };

  const applyNow = (al, A) => {
    const n = S.rungs.length;
    if (!n) return;
    if (S.canFade !== false) {
      const plan0 = fadePlan(S.pos, n);
      const lo = mountRung(al, A, plan0.lo);
      let hi = plan0.hi !== plan0.lo ? mountRung(al, A, plan0.hi) : null;
      // the upper rung has to sit above the lower one in the layer stack
      if (hi && S.canFade !== false && hi.seq < lo.seq) {
        unmountRung(al, plan0.hi);
        hi = mountRung(al, A, plan0.hi);
      }
    }
    if (S.canFade === false) {
      const keep = stepRung(S.pos, n);
      for (const i of Array.from(S.mounted.keys())) if (i !== keep) unmountRung(al, i);
      mountRung(al, A, keep);
      return;
    }
    const plan = fadePlan(S.pos, n, Array.from(S.mounted.keys()));
    for (const [i, a] of plan.alpha) {
      const r = S.mounted.get(i);
      if (r) setAlpha(r, a);
    }
  };

  const scheduleApply = () => {
    if (S.applyQueued || !S.h) return;
    S.applyQueued = true;
    S.h.run((al, A) => {
      S.applyQueued = false;
      if (!destroyed) applyNow(al, A);
    });
  };

  // ------------------------------------------------------------- rail UI

  const renderLabels = () => {
    const n = S.rungs.length;
    if (!n) return;
    const k = nearest();
    const g = S.rungs[k];
    now.textContent = g.label;
    const lo = Math.floor(S.pos), hi = Math.min(n - 1, Math.ceil(S.pos)), frac = S.pos - lo;
    if (hi !== lo && frac > 0.001 && frac < 0.999) {
      const into = frac >= 0.5 ? S.rungs[lo] : S.rungs[hi];
      const f = frac >= 0.5 ? 1 - frac : frac;
      fade.textContent = S.canFade === false ? '' : `fading into ${into.label} (${Math.round(f * 100)}%)`;
    } else {
      fade.textContent = '';
    }
    note.innerHTML = g.desc ? escapeHtml(g.desc) : '';
    for (const b of notches.children) {
      b.setAttribute('aria-pressed', String(+b.dataset.i === k));
    }
    if (Math.abs(+slider.value - S.pos) > 1e-4) slider.value = String(S.pos);
  };

  const writeRung = () => {
    clearTimeout(S.rungTimer);
    S.rungTimer = setTimeout(() => {
      if (!S.rungs.length || destroyed) return;
      const rid = S.rungs[nearest()].id;
      if (store.get('rung') !== rid) store.set({rung: rid}, {source: id});
    }, S.playing ? 4 * SETTLE_MS : SETTLE_MS);
  };

  const setPos = (p, {write = true} = {}) => {
    const n = S.rungs.length;
    if (!n) return;
    const v = Math.max(0, Math.min(n - 1, +p));
    if (!Number.isFinite(v)) return;
    S.pos = v;
    renderLabels();
    scheduleApply();
    if (write) writeRung();
  };

  const stop = () => {
    if (!S.playing) return;
    S.playing = false;
    cancelAnimationFrame(S.raf);
    play.textContent = 'Play';
    play.setAttribute('aria-pressed', 'false');
    writeRung();
  };

  const start = () => {
    const n = S.rungs.length;
    if (n < 2 || S.playing) return;
    if (S.pos >= n - 1 - 1e-6) setPos(0);
    S.playing = true;
    play.textContent = 'Pause';
    play.setAttribute('aria-pressed', 'true');
    const p0 = S.pos;
    let t0 = null;
    const per = BASE_SECONDS_PER_RUNG / (+speedSel.value || 1);
    const step = (ts) => {
      if (!S.playing || destroyed) return;
      if (t0 === null) t0 = ts;
      const p = p0 + (ts - t0) / 1000 / per;
      if (p >= n - 1) {
        setPos(n - 1);
        stop();
        return;
      }
      setPos(p);
      S.raf = requestAnimationFrame(step);
    };
    S.raf = requestAnimationFrame(step);
  };

  const togglePlay = () => (S.playing ? stop() : start());

  const buildRail = () => {
    const n = S.rungs.length;
    slider.max = String(Math.max(0, n - 1));
    slider.disabled = n < 2;
    play.disabled = n < 2;
    speedSel.disabled = n < 2;
    notches.textContent = '';
    S.rungs.forEach((g, i) => {
      const b = h('button', 'pk-wl-notch', {type: 'button', 'data-i': String(i), 'aria-pressed': 'false',
        title: g.desc || g.label});
      b.style.setProperty('--pk-wl-at', n > 1 ? `${(100 * i) / (n - 1)}%` : '50%');
      b.textContent = g.label;
      notches.append(b);
    });
  };

  notches.addEventListener('click', (e) => {
    const b = e.target.closest('.pk-wl-notch');
    if (!b) return;
    stop();
    setPos(+b.dataset.i);
  });
  slider.addEventListener('input', () => {
    stop();
    setPos(+slider.value);
  });
  play.addEventListener('click', togglePlay);
  speedSel.addEventListener('change', () => {
    if (S.playing) { stop(); start(); }
  });
  reset.addEventListener('click', () => {
    if (S.h && S.home) S.h.goto(S.home);
  });
  root.addEventListener('keydown', (e) => {
    if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    const tag = t && t.tagName;
    if (tag === 'SELECT' || tag === 'TEXTAREA' || (tag === 'INPUT' && t.type !== 'range')) return;
    const n = S.rungs.length;
    if (!n) return;
    if (e.key === 'ArrowUp' || e.key === 'ArrowRight') {
      stop();
      setPos(Math.min(n - 1, Math.floor(S.pos + 1e-9) + 1));
    } else if (e.key === 'ArrowDown' || e.key === 'ArrowLeft') {
      stop();
      setPos(Math.max(0, Math.ceil(S.pos - 1e-9) - 1));
    } else if (e.key === ' ' || e.key === 'Spacebar') {
      if (tag === 'BUTTON') return;
      togglePlay();
    } else {
      return;
    }
    e.preventDefault();
    e.stopPropagation();
  });

  // ------------------------------------------------------------- view sync

  const writeView = () => {
    if (!S.h || S.h.state !== 'ready') return;
    const v = S.h.view();
    if (![v.ra, v.dec, v.fov].every(Number.isFinite)) return;
    ctx.setStatus(`${fmtRA(v.ra)} ${fmtDec(v.dec)} · ${fmtAngle(v.fov)}`);
    const cur = store.get('sky') || {};
    const r6 = (x) => Math.round(x * 1e6) / 1e6;
    const next = {ra: r6(v.ra), dec: r6(v.dec), fov: r6(v.fov)};
    if (Math.abs((+cur.ra || 0) - next.ra) < 1e-6 && Math.abs((+cur.dec || 0) - next.dec) < 1e-6 &&
      Math.abs((+cur.fov || 0) - next.fov) < 1e-6) return;
    store.set({sky: Object.assign({}, cur, next)}, {source: id});
  };

  const setup = async () => {
    const hips = cfg.hips ? await hub.json(cfg.hips) : null;
    if (destroyed) return;
    S.hips = hips;
    S.rungs = rungsOf(hips);
    buildRail();
    const sky = store.get('sky') || {};
    const view = initialView(sky, hips, null);
    S.home = initialView(null, hips, null);
    const usingRungs = !!(hips && Array.isArray(hips.rungs) && hips.rungs.length);
    const backdrop = usingRungs ? defaultBase(hips) : S.rungs[0];
    const survey = backdrop ? hub.resolve(backdrop.url).toString() : null;
    const hd = aladinFor(id, {page: app.page, view, survey});
    S.h = hd;
    hd.attach(stage);
    stage.append(density);
    if (!S.rungs.length) {
      const n = h('div', 'pk-info-note');
      n.textContent = 'This page lists no wavelength rungs.';
      rail.prepend(n);
    }
    const r0 = store.get('rung');
    const k0 = S.rungs.findIndex((g) => g.id === r0);
    setPos(k0 >= 0 ? k0 : 0, {write: false});

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
      return;
    }
    if (destroyed) return;
    skel.remove();
    stage.dataset.aladin = hd.source || 'ready';
    // re-read `sky`: changes made while Aladin was loading could not reach the viewer
    const cur = store.get('sky') || {};
    if (Number.isFinite(+cur.ra) && Number.isFinite(+cur.dec)) hd.goto(initialView(cur, hips, null));
    scheduleApply();

    if (cfg.markers) {
      const client = await app.catalog(cfg.markers).catch(() => null);
      if (client && client.manifest.pos && !destroyed) {
        S.client = client;
        S.markers = new SkyMarkers({h: hd, app, store, ds: cfg.markers, pane: id, client, tokens,
          onNotice: setNotice, canvas: density, mode: 'auto'});
        await S.markers.refresh({force: true});
        offs.push(store.on([`vis:${cfg.markers}`, `tier:${id}`], () => S.markers && S.markers.refresh({force: true})));
        offs.push(store.on(['color', 'theme'], () => S.markers && S.markers.restyle()));
        offs.push(store.on([`sel:${cfg.markers}`], () => S.markers && S.markers.refreshSelected()));
      }
    }
    const moved = () => {
      if (S.markers) S.markers.viewChanged();
      clearTimeout(S.writeTimer);
      S.writeTimer = setTimeout(writeView, SETTLE_MS);
    };
    offs.push(hd.on('positionChanged', moved));
    offs.push(hd.on('zoomChanged', moved));
    const v = hd.view();
    if ([v.ra, v.dec, v.fov].every(Number.isFinite)) {
      ctx.setStatus(`${S.rungs.length} rungs${S.client ? ` · ${fmtCount(S.client.n)} sources` : ''}`);
    }
  };

  offs.push(store.on(['rung'], (changed, st, sources) => {
    if (sources.size === 1 && sources.has(id)) return;
    const k = S.rungs.findIndex((g) => g.id === st.get('rung'));
    if (k >= 0 && k !== nearest()) {
      stop();
      setPos(k, {write: false});
    }
  }));
  offs.push(store.on(['sky'], (changed, st, sources) => {
    if (sources.size === 1 && sources.has(id)) return;
    if (!S.h || S.h.state !== 'ready') return;
    const sky = st.get('sky') || {};
    if (!Number.isFinite(+sky.ra) || !Number.isFinite(+sky.dec)) return;
    const v = S.h.view();
    const tol = 1e-4 * (+v.fov || 1);
    if (Math.abs(v.ra - sky.ra) > tol || Math.abs(v.dec - sky.dec) > tol ||
      (+sky.fov > 0 && Math.abs(v.fov - sky.fov) > 1e-3 * sky.fov)) {
      S.h.goto({ra: +sky.ra, dec: +sky.dec, fov: +sky.fov});
    }
  }));

  const ready = setup().catch((err) => {
    if (destroyed) return;
    console.error(`wavelength pane ${id} failed`, err);
    skel.remove();
    const n = h('div', 'pk-error-note', {role: 'alert'});
    n.textContent = `The wavelength viewer could not start: ${err && err.message ? err.message : err}`;
    stage.append(n);
  });

  return {
    ready,
    resize() {
      if (S.markers && S.markers.density) S.markers._paintDensity();
    },
    onVisible(v) {
      if (!v) stop();
      if (S.markers) S.markers.setVisible(v);
    },
    /** Test and scripting hooks. */
    setPos(p) { stop(); setPos(p); },
    play: start,
    pause: stop,
    stats() {
      const opacities = {};
      for (const [i, r] of S.mounted) opacities[S.rungs[i] ? S.rungs[i].id : i] = r.alpha;
      const lib = libState();
      return {
        aladin: S.error ? 'error' : (S.h && S.h.state === 'ready' ? (S.h.source || lib.source) : lib.state),
        pos: S.pos,
        rung: S.rungs.length ? S.rungs[nearest()].id : null,
        rungs: S.rungs.map((g) => g.id),
        canFade: S.canFade,
        mounted: Array.from(S.mounted.keys()).sort((a, b) => a - b).map((i) => S.rungs[i].id),
        opacities,
        playing: S.playing,
        speed: +speedSel.value,
        markers: S.markers ? S.markers.stats() : null,
      };
    },
    destroy() {
      destroyed = true;
      stop();
      clearTimeout(S.writeTimer);
      clearTimeout(S.rungTimer);
      for (const off of offs.splice(0)) off();
      if (S.markers) S.markers.destroy();
      if (S.h) {
        const ids = Array.from(S.mounted.keys());
        S.mounted.forEach((r, i) => S.h.run((al) => removeImageLayer(al, layerName(i), r.layer)));
        if (ids.length) S.mounted.clear();
        S.h.detach();
      }
      root.remove();
    },
  };
}
