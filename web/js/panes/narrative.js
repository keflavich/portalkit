// Narrative pane (ARCHITECTURE.md sections 5.10 pk-narrative/1, 6.10 `beat`, 6.13 options
// `narrative`, `sticky`; legacy NIRSpec_6927_MSATA.py scroll linkage).
//
// Beats scroll in the pane; an IntersectionObserver over a band in the middle of the pane
// (rootMargin -42% top and bottom) signals beats crossing the band; the beat containing the band's
// centre line (else the one nearest to it), measured over every beat, is active, so a beat leaving
// the band never re-activates itself while the next one covers the centre. After a programmatic
// activation (go(k), `beat` from the hash or another pane) the choice is paused until beat k
// reaches the centre or the user scrolls by hand, so a smooth scroll does not pass the
// activation through the beats it crosses.
// Clicking a beat activates it. Activating a beat:
//   * writes `beat` (hash `beat=`);
//   * sets the sky overlays to (defaults + layers_on) - layers_off, computed from scratch for
//     each beat, so overlays that one scene turned on are removed when the next scene starts;
//   * moves the sticky view unless the beat sets `suppress_view`: `sky` {ra, dec, fov} for a sky
//     target, `axes:<pane>` {x, y} for a scatter target;
//   * emits `narrative:highlight` {pane, ds, ids, beat} (an empty `ids` list clears it). The sky
//     pane draws circles around those rows; a pane that mounts later asks for the current state
//     with `narrative:sync`.
// Elements inside beat html that carry `data-ids` ("id1,id2", optional `data-ds`) highlight
// those rows while hovered or after a click, keeping the view unchanged.

import {fmtCount} from '../core/fmt.js';

export const meta = {type: 'narrative', caps: [], needs: []};

const ROOT_MARGIN = '-42% 0px -42% 0px';
const THRESHOLDS = [0, 0.25, 0.5, 0.75, 1];
const PIN_MAX_MS = 2500;          // upper bound on the observer pause after a programmatic scroll

function h(tag, cls, attrs) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (attrs) for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
}

/**
 * Overlay ids shown for a beat: (defaults + layers_on) - layers_off, in a stable order
 * (defaults first, then layers_on).
 */
export function beatOverlays(defaults, view) {
  const v = view || {};
  const off = new Set(Array.isArray(v.layers_off) ? v.layers_off : []);
  const out = [];
  for (const id of defaults || []) if (!off.has(id) && !out.includes(id)) out.push(id);
  for (const id of Array.isArray(v.layers_on) ? v.layers_on : []) if (!off.has(id) && !out.includes(id)) out.push(id);
  return out;
}

/** Default overlay ids from the page's hips and overlays documents (sky space only). */
export function overlayDefaults(hipsDocs, overlayDocs) {
  const ids = [];
  for (const d of hipsDocs) for (const o of (d && d.overlays) || []) if (o && o.default && o.id) ids.push(o.id);
  for (const d of overlayDocs) {
    for (const l of (d && d.layers) || []) if (l && l.default && l.id && (l.space || 'sky') === 'sky') ids.push(l.id);
  }
  return ids;
}

function idsOf(attr) {
  return String(attr || '').split(',').map((s) => s.trim()).filter(Boolean);
}

export function mount(el, ctx) {
  const {store, app, hub} = ctx;
  const cfg = ctx.config || {};
  const id = ctx.id;
  const offs = [];
  let destroyed = false;

  const root = h('div', 'pk-nar-pane');
  const scroller = h('div', 'pk-nar-scroll', {tabindex: '0', role: 'region', 'aria-label': 'Story'});
  const skel = h('div', 'pk-skeleton pk-nar-skel');
  skel.textContent = 'Loading the story…';
  scroller.append(skel);
  root.append(scroller);
  el.append(root);

  const S = {beats: [], sections: [], current: -1, defaults: [], io: null, hl: null, hover: null,
    stickyType: cfg.sticky === 'scatter' ? 'scatter' : 'sky', ovl: null,
    pinK: -1, pinMax: null};

  // ---- observer pause around programmatic scrolls: activate(k, {scroll: true}) pins beat k
  // until the band centre lies inside it (a smooth scroll toward its middle then keeps it there),
  // the user scrolls by hand, or PIN_MAX_MS passes.
  const unpin = () => {
    if (S.pinK < 0) return;
    S.pinK = -1;
    clearTimeout(S.pinMax);
    pickActive();
  };
  const arrived = () => {
    if (S.pinK >= 0 && atCentre(S.pinK)) unpin();
  };
  const pin = (k) => {
    S.pinK = k;
    clearTimeout(S.pinMax);
    S.pinMax = setTimeout(unpin, PIN_MAX_MS);
  };
  const userScroll = () => {
    if (S.pinK >= 0) unpin();
  };
  scroller.addEventListener('scroll', arrived, {passive: true});
  for (const ev of ['wheel', 'touchstart', 'pointerdown']) scroller.addEventListener(ev, userScroll, {passive: true});
  scroller.addEventListener('keydown', (e) => {
    if (['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(e.key)) userScroll();
  });

  const bandMid = () => scroller.getBoundingClientRect().top + scroller.clientHeight / 2;
  /** True when the band centre line lies inside beat k. */
  const atCentre = (k) => {
    const r = S.sections[k].getBoundingClientRect();
    const mid = bandMid();
    return r.top <= mid && mid <= r.bottom;
  };
  /**
   * Index of the beat at the band centre among `cands` (indices), from live geometry: the one
   * containing the centre line, else the nearest; ties keep the current beat. -1 when empty.
   */
  const centreBeat = (cands) => {
    const mid = bandMid();
    let best = -1, bestD = Infinity;
    for (const k of cands) {
      const r = S.sections[k].getBoundingClientRect();
      const d = mid < r.top ? r.top - mid : mid > r.bottom ? mid - r.bottom : 0;
      if (d < bestD || (d === bestD && k === S.current)) { bestD = d; best = k; }
    }
    return best;
  };
  function pickActive() {
    if (S.pinK >= 0 || destroyed || !S.sections.length) return;
    // every beat is measured (not only the observer's entries, which hold just the beats that
    // crossed a threshold), so a beat leaving the band never re-activates itself
    const k = centreBeat(S.sections.map((_, i) => i));
    if (k >= 0 && k !== S.current) activate(k);
  }

  const leaves = () => (ctx.layout && typeof ctx.layout.panes === 'function' ? ctx.layout.panes() : []);

  /** The pane a beat's view applies to: view.pane as an id, else as a type, else the sticky pane. */
  const targetPane = (view) => {
    const ps = leaves().filter((p) => p.id !== id);
    const want = (view && view.pane) || S.stickyType;
    return ps.find((p) => p.id === want) || ps.find((p) => p.type === want) ||
      ps.find((p) => p.type === S.stickyType) || null;
  };

  const emitHighlight = (beatId, hl, extra = {}) => {
    const tgt = targetPane(S.beats[S.current] && S.beats[S.current].view);
    const p = Object.assign({pane: tgt ? tgt.id : S.stickyType, ds: (hl && hl.ds) || null,
      ids: (hl && Array.isArray(hl.ids) ? hl.ids.map(String) : []), beat: beatId}, extra);
    const prev = S.hl;
    // the highlight moves to another pane: clear the rings left on the previous target
    if (prev && prev.ids.length && prev.pane !== p.pane) {
      store.emit('narrative:highlight', Object.assign({}, prev, {ids: [], goto: false}));
    }
    S.hl = p;
    store.emit('narrative:highlight', p);
  };

  const activate = (k, {write = true, scroll = false} = {}) => {
    if (k < 0 || k >= S.beats.length) return;
    if (scroll) {
      pin(k);
      S.sections[k].scrollIntoView({block: 'center'});
      arrived();               // already centred, or an instant scroll
    }
    if (k === S.current) return;
    if (S.current >= 0) {
      S.sections[S.current].classList.remove('pk-live');
      S.sections[S.current].removeAttribute('aria-current');
    }
    for (const n of scroller.querySelectorAll('[data-ids].pk-on')) n.classList.remove('pk-on');
    S.hover = null;
    S.current = k;
    const sec = S.sections[k];
    sec.classList.add('pk-live');
    sec.setAttribute('aria-current', 'step');
    const beat = S.beats[k];
    const view = beat.view || {};
    const patch = {};
    if (write && store.get('beat') !== beat.id) patch.beat = beat.id;
    const tgt = targetPane(view);
    const tgtType = tgt ? tgt.type : (view.pane || S.stickyType);
    // overlays: computed from scratch for every beat (scene exit removes overlays)
    const ovl = beatOverlays(S.defaults, view);
    S.ovl = ovl;
    const cur = store.get('sky') || {};
    const sky = Object.assign({}, cur, {overlays: ovl.map((o) => ({id: o}))});
    if (!view.suppress_view && view.sky && Number.isFinite(+view.sky.ra) && Number.isFinite(+view.sky.dec)) {
      sky.ra = +view.sky.ra;
      sky.dec = +view.sky.dec;
      if (+view.sky.fov > 0) sky.fov = +view.sky.fov;
    }
    patch.sky = sky;
    if (!view.suppress_view && view.axes && tgtType === 'scatter' && tgt) {
      const key = `axes:${tgt.id}`;
      patch[key] = Object.assign({}, store.get(key) || {}, view.axes);
    }
    store.set(patch, {source: id});
    const hl = view.highlight && Array.isArray(view.highlight.ids) && view.highlight.ids.length ? view.highlight : null;
    emitHighlight(beat.id, hl, hl && !view.suppress_view && !view.sky ? {goto: true} : {});
    ctx.setStatus(`${k + 1} / ${S.beats.length}`);
  };

  const render = (doc) => {
    skel.remove();
    S.beats = (doc && Array.isArray(doc.beats) ? doc.beats : []).filter((b) => b && typeof b === 'object')
      .map((b, k) => Object.assign({}, b, {id: String(b.id ?? `b${k + 1}`)}));
    if (!S.beats.length) {
      const n = h('div', 'pk-info-note');
      n.textContent = 'This story has no beats.';
      scroller.append(n);
      return;
    }
    scroller.append(h('div', 'pk-nar-spacer pk-nar-spacer-top', {'aria-hidden': 'true'}));
    S.beats.forEach((b, k) => {
      const sec = h('section', 'pk-nar-beat', {'data-beat': b.id, 'data-k': String(k), id: `pk-beat-${id}-${b.id}`});
      if (b.title && !/^\s*<h[1-6][\s>]/i.test(b.html || '')) {
        const t = h('h2', 'pk-nar-title');
        t.textContent = b.title;
        sec.append(t);
      }
      const body = h('div', 'pk-nar-body');
      // html comes from markdown_lite at build time (raw html is rejected by the writer)
      body.innerHTML = b.html || '';
      sec.append(body);
      scroller.append(sec);
      S.sections.push(sec);
    });
    scroller.append(h('div', 'pk-nar-spacer pk-nar-spacer-bottom', {'aria-hidden': 'true'}));
  };

  const observe = () => {
    if (typeof IntersectionObserver !== 'function') {
      scroller.addEventListener('scroll', pickActive, {passive: true});
      offs.push(() => scroller.removeEventListener('scroll', pickActive));
      return;
    }
    S.io = new IntersectionObserver(() => {
      if (S.pinK >= 0) arrived();
      else pickActive();
    }, {root: scroller, rootMargin: ROOT_MARGIN, threshold: THRESHOLDS});
    for (const s of S.sections) S.io.observe(s);
  };

  // clicks: a highlight phrase toggles its highlight, anywhere else in a beat activates it
  scroller.addEventListener('click', (e) => {
    const sec = e.target.closest('.pk-nar-beat');
    if (!sec) return;
    const k = +sec.dataset.k;
    const ph = e.target.closest('[data-ids]');
    if (ph && sec.contains(ph)) {
      e.stopPropagation();
      if (k !== S.current) activate(k);
      const on = !ph.classList.contains('pk-on');
      for (const n of scroller.querySelectorAll('[data-ids].pk-on')) n.classList.remove('pk-on');
      if (on) {
        ph.classList.add('pk-on');
        S.hover = ph;
        emitHighlight(S.beats[k].id, {ds: ph.dataset.ds || null, ids: idsOf(ph.dataset.ids)});
      } else {
        S.hover = null;
        restoreBeatHighlight();
      }
      return;
    }
    if (e.target.closest('a[href]')) return;
    activate(k);
  });
  const restoreBeatHighlight = () => {
    const b = S.beats[S.current];
    if (!b) return;
    const v = b.view || {};
    emitHighlight(b.id, v.highlight && Array.isArray(v.highlight.ids) && v.highlight.ids.length ? v.highlight : null);
  };
  scroller.addEventListener('mouseover', (e) => {
    const ph = e.target.closest('[data-ids]');
    if (!ph || ph === S.hover || scroller.querySelector('[data-ids].pk-on')) return;
    S.hover = ph;
    const b = S.beats[S.current];
    emitHighlight(b ? b.id : null, {ds: ph.dataset.ds || null, ids: idsOf(ph.dataset.ids)});
  });
  scroller.addEventListener('mouseout', (e) => {
    const ph = e.target.closest('[data-ids]');
    if (!ph || ph !== S.hover || ph.contains(e.relatedTarget) || ph.classList.contains('pk-on')) return;
    S.hover = null;
    restoreBeatHighlight();
  });

  offs.push(store.on(['beat'], (changed, st, sources) => {
    if (sources.size === 1 && sources.has(id)) return;
    const k = S.beats.findIndex((b) => b.id === st.get('beat'));
    if (k >= 0 && k !== S.current) activate(k, {write: false, scroll: true});
  }));
  offs.push(store.onEvent('narrative:sync', () => {
    if (S.hl) store.emit('narrative:highlight', S.hl);
  }));

  const setup = async () => {
    if (!cfg.narrative) throw new Error('narrative pane needs the "narrative" option (a narrative dataset id)');
    const ds = (app.page && app.page.datasets) || {};
    const hipsIds = Object.keys(ds).filter((k) => ds[k] && ds[k].kind === 'hips');
    const ovlIds = Object.keys(ds).filter((k) => ds[k] && ds[k].kind === 'overlays');
    const soft = (k) => hub.json(k).catch((err) => {
      console.warn(`narrative: ${k} unavailable: ${err && err.message}`);
      return null;
    });
    const [doc, hipsDocs, ovlDocs] = await Promise.all([
      hub.json(cfg.narrative), Promise.all(hipsIds.map(soft)), Promise.all(ovlIds.map(soft))]);
    if (destroyed) return;
    S.defaults = overlayDefaults(hipsDocs, ovlDocs);
    render(doc);
    if (!S.beats.length) return;
    ctx.setStatus(`${fmtCount(S.beats.length)} beats`);
    const want = store.get('beat');
    const k0 = Math.max(0, S.beats.findIndex((b) => b.id === want));
    activate(k0, {write: !!want && k0 > 0, scroll: k0 > 0});
    // let the scroll position settle before the observer starts choosing beats
    await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
    if (!destroyed) observe();
  };

  const ready = setup().catch((err) => {
    if (destroyed) return;
    if (err instanceof TypeError || err instanceof ReferenceError || err instanceof RangeError) {
      console.error(`narrative pane ${id} failed`, err);
    }
    skel.remove();
    const n = h('div', 'pk-error-note', {role: 'alert'});
    n.textContent = `The story could not be loaded: ${err && err.message ? err.message : err}`;
    scroller.append(n);
  });

  return {
    ready,
    resize() {},
    onVisible() {},
    /** Activate beat k (index or id) and scroll it into view. */
    go(k) {
      const i = typeof k === 'number' ? k : S.beats.findIndex((b) => b.id === String(k));
      activate(i, {scroll: true});
    },
    stats() {
      return {
        beat: S.current >= 0 ? S.beats[S.current].id : null,
        index: S.current,
        beats: S.beats.length,
        overlays: S.ovl ? S.ovl.slice() : null,
        highlight: S.hl ? S.hl.ids.length : 0,
        defaults: S.defaults.slice(),
      };
    },
    destroy() {
      destroyed = true;
      clearTimeout(S.pinMax);
      if (S.io) S.io.disconnect();
      for (const off of offs.splice(0)) off();
      if (S.hl && S.hl.ids.length) store.emit('narrative:highlight', Object.assign({}, S.hl, {ids: []}));
      root.remove();
    },
  };
}
