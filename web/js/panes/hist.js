// Histogram pane (ARCHITECTURE.md sections 6.2, 6.9): counts of one expression with a range
// brush that writes `filters:<ds>.ranges[expr] = [lo, hi]` (the hub applies it through the
// worker and every pane re-reads `vis:<ds>`).
//
// Three layers share fixed bin edges (from the unfiltered rows, so the bars do not jump while
// the range is dragged): all rows (faint), visible rows and selected rows (selection colour).
// Dragging horizontally sets the range, clicking inside the plot without dragging clears it,
// and the range edges can be typed in the foot. The axis picker changes the expression; the
// expression, x-log and count-log choices live in `axes:<pane>` ({x, xlog, ylog}).
// Snapshot is SVG.

import {SETTLE_MS} from '../core/constants.js';
import {makeView} from '../plot/axes.js';
import {prepareCanvas, tokensOf, drawAxes, toSVG} from '../plot/canvas2d.js';
import {fmtCount, fmtNum} from '../core/fmt.js';
import {createAxisPicker, exprLabel} from '../ui/axis_picker.js';
import {el, iconButton, paneDataset} from './scatter.js';
import {firstPaintLatch} from '../layout/layout.js';

export const meta = {
  type: 'hist',
  caps: ['filter_toggles', 'linked_selection'],
  needs: ['catalog'],
};

export const DEFAULT_BINS = 64;
export const DRAG_PX = 3;

/** A range bound as stored: a finite number, or null for an open side. */
const bound = (v) => (v === null || v === undefined || v === '' || !Number.isFinite(Number(v)) ? null : Number(v));

/**
 * Updated filters doc with ranges[expr] set to `range` (null removes it). A null bound stays
 * null (open side, as the worker's query.js reads it); the two bounds swap only when both
 * are finite and out of order. A range with both sides open removes the entry.
 */
export function withRange(filters, expr, range) {
  const f = {...(filters || {})};
  const ranges = {...(f.ranges || {})};
  let a = range ? bound(range[0]) : null, b = range ? bound(range[1]) : null;
  if (a !== null && b !== null && a > b) [a, b] = [b, a];
  if (range && (a !== null || b !== null)) ranges[expr] = [a, b];
  else delete ranges[expr];
  if (Object.keys(ranges).length) f.ranges = ranges;
  else delete f.ranges;
  return f;
}

/** Bars [{x0, x1, c}] of a hist result. */
export function bars(h) {
  const out = [];
  if (!h || !h.counts) return out;
  for (let k = 0; k < h.counts.length; k++) out.push({x0: h.edges[k], x1: h.edges[k + 1], c: h.counts[k]});
  return out;
}

export async function mount(host, ctx) {
  const {store, app, id} = ctx;
  const cfg = ctx.config || {};
  const ds = paneDataset(ctx);
  if (!ds) throw new Error('hist pane needs a catalog dataset');
  const client = await app.catalog(ds);
  const nbins = Math.max(4, Math.min(1024, Number(cfg.bins) || DEFAULT_BINS));
  const brushOn = cfg.brush !== false;

  const root = el('div', 'pk-hist pk-plot');
  const bar = el('div', 'pk-plot-bar');
  const stage = el('div', 'pk-hist-stage');
  const cv = el('canvas', 'pk-hist-canvas');
  cv.tabIndex = 0;
  cv.setAttribute('role', 'img');
  stage.append(cv);
  const foot = el('div', 'pk-plot-foot pk-hist-foot');
  root.append(bar, stage, foot);
  host.append(root);

  const firstNumeric = () => {
    const c = (client.manifest.cols || []).find((m) => m.role === 'mag' || m.role === 'color' || m.role === 'flux' || m.role === 'value');
    return c ? c.key : null;
  };
  const a0 = store.get(`axes:${id}`) || {};
  const S = {
    expr: a0.x || cfg.expr || firstNumeric(),
    xlog: !!(a0.xlog ?? cfg.xlog),
    ylog: !!(a0.ylog ?? cfg.ylog),
    all: null, vis: null, sel: null,
    view: null, w: 0, h: 0, dpr: 1,
    drag: null, renderMs: 0, seq: 0,
  };
  let destroyed = false;
  let visible = true;
  const offs = [];
  // first paint (IMPLEMENTATION_NOTES 112): the first drawn histogram, or the 'choose a column'
  // state; a failed first load rejects
  const fp = firstPaintLatch();

  // ------------------------------------------------------------ widgets
  const picker = createAxisPicker({client, axis: 'x', label: 'value', value: {expr: S.expr, log: S.xlog}, options: true,
    onChange: (p) => {
      if ('expr' in p) S.expr = p.expr;
      if ('log' in p) S.xlog = !!p.log;
      writeAxes();
      reload();
    }});
  bar.append(picker.el);
  const ylogBtn = iconButton('log N', 'Logarithmic counts', 'pk-hist-ylog');
  ylogBtn.classList.remove('pk-icon-btn');
  ylogBtn.setAttribute('aria-pressed', S.ylog ? 'true' : 'false');
  ylogBtn.addEventListener('click', () => {
    S.ylog = !S.ylog;
    ylogBtn.setAttribute('aria-pressed', S.ylog ? 'true' : 'false');
    writeAxes();
    draw();
  });
  (ctx.tools || bar).append(ylogBtn);

  const rangeBox = el('div', 'pk-hist-range');
  const lab = el('span', 'pk-hist-range-label pk-muted', 'range');
  const lo = el('input', 'pk-num pk-hist-lo');
  const hi = el('input', 'pk-num pk-hist-hi');
  for (const [inp, ph] of [[lo, 'min'], [hi, 'max']]) {
    inp.type = 'number';
    inp.step = 'any';
    inp.placeholder = ph;
    inp.setAttribute('aria-label', `range ${ph}`);
  }
  const clr = iconButton('×', 'Clear the range filter', 'pk-hist-clear');
  const count = el('span', 'pk-hist-count pk-muted pk-num');
  count.setAttribute('aria-live', 'polite');
  rangeBox.append(lab, lo, el('span', null, '–'), hi, clr);
  foot.append(rangeBox, count);
  rangeBox.hidden = !brushOn;
  const typed = () => {
    const a = lo.value === '' ? null : Number(lo.value);
    const b = hi.value === '' ? null : Number(hi.value);
    if (a === null && b === null) setRange(null);
    else if ((a === null || Number.isFinite(a)) && (b === null || Number.isFinite(b))) {
      const r = [a, b];
      if (a !== null && b !== null && a > b) r.reverse();
      setRange(r);
    }
  };
  lo.addEventListener('change', typed);
  hi.addEventListener('change', typed);
  clr.addEventListener('click', () => setRange(null));

  function writeAxes() {
    const cur = store.get(`axes:${id}`) || {};
    store.set({[`axes:${id}`]: {...cur, x: S.expr, xlog: S.xlog, ylog: S.ylog}}, {source: id});
  }

  // ------------------------------------------------------------ filter range
  function curRange() {
    const f = store.get(`filters:${ds}`) || {};
    const r = f.ranges && f.ranges[S.expr];
    return Array.isArray(r) ? r : null;
  }

  function setRange(r) {
    if (!S.expr) return;
    const f = withRange(store.get(`filters:${ds}`), S.expr, r);
    store.set({[`filters:${ds}`]: f}, {source: id});
    syncRange();
    draw();
  }

  function syncRange() {
    const r = curRange();
    if (document.activeElement !== lo) lo.value = r && r[0] !== null && r[0] !== undefined ? fmtNum(r[0], 4) : '';
    if (document.activeElement !== hi) hi.value = r && r[1] !== null && r[1] !== undefined ? fmtNum(r[1], 4) : '';
    clr.disabled = !r;
    root.classList.toggle('pk-has-range', !!r);
  }

  // ------------------------------------------------------------ data
  async function reload() {
    const my = ++S.seq;
    if (!S.expr) { ctx.setStatus('choose a column'); fp.resolve(); return; }
    ctx.setStatus('loading…');
    try {
      const all = await client.hist(S.expr, nbins, {mask: null, log: S.xlog});
      if (my !== S.seq || destroyed) return;
      S.all = all;
      await reloadLayers(my);
      ctx.setStatus('');
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      ctx.setStatus('error', err.message);
      store.emit('toast', {level: 'error', text: `Histogram of ${S.expr}: ${err.message}`});
      fp.reject(err);
    }
    syncRange();
  }

  async function reloadLayers(my = S.seq) {
    if (!S.all) return;
    const e = S.all.edges;
    const range = [e[0], e[e.length - 1]];
    const sel = await app.selectionFor(ds);
    const [vis, sh] = await Promise.all([
      client.hist(S.expr, nbins, {range, mask: 'vis', log: S.xlog}),
      sel.count ? client.hist(S.expr, nbins, {range, mask: 'sel', log: S.xlog}) : Promise.resolve(null),
    ]);
    if (my !== S.seq || destroyed) return;
    S.vis = vis;
    S.sel = sh;
    count.textContent = `${fmtCount(vis.total)} of ${fmtCount(S.all.total)} shown${sh ? ` · ${fmtCount(sh.total)} selected` : ''}`;
    draw();
  }

  let layerTimer = null;
  function reloadLayersSoon() {
    if (layerTimer) clearTimeout(layerTimer);
    layerTimer = setTimeout(() => {
      layerTimer = null;
      reloadLayers().catch((err) => {
        if (err && err.name === 'AbortError') return;
        console.warn(`hist ${id}: layer reload failed`, err);
      });
    }, SETTLE_MS / 3);
  }

  // ------------------------------------------------------------ drawing
  function buildView() {
    const h = S.all;
    const e = h ? h.edges : [0, 1];
    let max = 1;
    if (h) for (let k = 0; k < h.counts.length; k++) if (h.counts[k] > max) max = h.counts[k];
    return makeView({
      width: S.w, height: S.h, dpr: S.dpr, margin: {l: 52, r: 12, t: 10, b: 38},
      x: {kind: S.xlog ? 'log' : 'lin', d0: e[0], d1: e[e.length - 1], label: exprLabel(client, S.expr), expr: S.expr},
      y: S.ylog ? {kind: 'log', d0: 0.7, d1: max * 1.6, label: 'N'} : {kind: 'lin', d0: 0, d1: max * 1.05, label: 'N'},
    });
  }

  function paint(c, view, tk) {
    drawAxes(c, view, {tokens: tk, xLabel: view.x.label, yLabel: 'N'});
    const P = view.plot;
    c.save();
    c.beginPath();
    c.rect(P.left, P.top, P.width, P.height);
    c.clip();
    const base = S.ylog ? 0.7 : 0;
    const layer = (h, fill, alpha, stroke) => {
      if (!h) return;
      c.globalAlpha = alpha;
      c.fillStyle = fill;
      for (const b of bars(h)) {
        if (!b.c) continue;
        const [x0, y0] = view.toPx(b.x0, b.c);
        const [x1, yb] = view.toPx(b.x1, base);
        const left = Math.min(x0, x1), w = Math.max(1, Math.abs(x1 - x0) - (Math.abs(x1 - x0) > 3 ? 1 : 0));
        c.fillRect(left, y0, w, yb - y0);
      }
      if (stroke) {
        c.globalAlpha = 1;
        c.strokeStyle = stroke;
        c.lineWidth = 1.25;
        c.beginPath();
        let started = false;
        for (const b of bars(h)) {
          const [x0, y] = view.toPx(b.x0, Math.max(base, b.c));
          const [x1] = view.toPx(b.x1, base);
          if (!started) { c.moveTo(x0, view.toPx(b.x0, base)[1]); started = true; }
          c.lineTo(x0, y);
          c.lineTo(x1, y);
        }
        c.stroke();
      }
    };
    layer(S.all, tk.neutral, 0.25);
    layer(S.vis, tk.neutral, 0.75);
    layer(S.sel, tk.sel, 0.85, tk.sel);
    c.globalAlpha = 1;
    const r = S.drag && S.drag.moved ? [S.drag.a, S.drag.b] : curRange();
    if (r) {
      const xa = r[0] === null || r[0] === undefined ? P.left : view.toPx(r[0], 1)[0];
      const xb = r[1] === null || r[1] === undefined ? P.right : view.toPx(r[1], 1)[0];
      const l = Math.max(P.left, Math.min(xa, xb)), rr = Math.min(P.right, Math.max(xa, xb));
      c.fillStyle = tk.fg;
      c.globalAlpha = 0.06;
      c.fillRect(P.left, P.top, Math.max(0, l - P.left), P.height);
      c.fillRect(rr, P.top, Math.max(0, P.right - rr), P.height);
      c.globalAlpha = 1;
      c.strokeStyle = tk.sel;
      c.lineWidth = 1.5;
      c.setLineDash([4, 3]);
      for (const x of [l, rr]) {
        if (x <= P.left || x >= P.right) continue;
        c.beginPath();
        c.moveTo(x, P.top);
        c.lineTo(x, P.bottom);
        c.stroke();
      }
      c.setLineDash([]);
    }
    c.restore();
  }

  let frame = 0;
  function draw() {
    if (frame || destroyed) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      if (!visible || !(S.w > 1 && S.h > 1)) return;
      const t0 = performance.now();
      const tk = tokensOf(ctx.theme());
      const view = S.view = buildView();
      const c = prepareCanvas(cv, S.w, S.h, S.dpr);
      c.clearRect(0, 0, S.w, S.h);
      paint(c, view, tk);
      cv.setAttribute('aria-label', `Histogram of ${exprLabel(client, S.expr)}`);
      S.renderMs = performance.now() - t0;
      if (S.all) fp.resolve();
    });
  }

  // ------------------------------------------------------------ range brush
  const px = (e) => {
    const r = cv.getBoundingClientRect();
    return [e.clientX - r.left, e.clientY - r.top];
  };
  cv.addEventListener('pointerdown', (e) => {
    if (!brushOn || !S.view || e.button !== 0 || e.pkHandled) return;
    const [x, y] = px(e);
    if (!S.view.inPlot(x, y)) return;
    const v = S.view.toData(x, y)[0];
    S.drag = {x0: x, a: v, b: v, moved: false, id: e.pointerId};
    cv.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  cv.addEventListener('pointermove', (e) => {
    if (!S.drag || !S.view) return;
    const [x] = px(e);
    const P = S.view.plot;
    const xc = Math.max(P.left, Math.min(P.right, x));
    S.drag.b = S.view.toData(xc, P.top)[0];
    if (Math.abs(x - S.drag.x0) >= DRAG_PX) S.drag.moved = true;
    draw();
  });
  const end = (e, cancel) => {
    if (!S.drag) return;
    const d = S.drag;
    S.drag = null;
    if (cv.hasPointerCapture && cv.hasPointerCapture(e.pointerId)) cv.releasePointerCapture(e.pointerId);
    if (cancel) { draw(); return; }
    if (d.moved) setRange([Math.min(d.a, d.b), Math.max(d.a, d.b)]);
    else if (curRange()) setRange(null);
    else draw();
  };
  cv.addEventListener('pointerup', (e) => end(e, false));
  cv.addEventListener('pointercancel', (e) => end(e, true));
  cv.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && S.drag) { S.drag = null; draw(); }
    else if ((e.key === 'Delete' || e.key === 'Backspace') && curRange()) setRange(null);
  });

  // ------------------------------------------------------------ store wiring
  offs.push(store.on([`filters:${ds}`], () => { syncRange(); draw(); }));
  offs.push(store.on([`vis:${ds}`], () => reloadLayersSoon()));
  offs.push(store.on(['theme'], () => draw()));
  offs.push(store.onEvent('theme:change', () => draw()));
  offs.push(store.on([`axes:${id}`], (changed, s, sources) => {
    if (sources.size === 1 && sources.has(id)) return;
    const a = s.get(`axes:${id}`) || {};
    const ex = a.x || S.expr;
    const xl = !!a.xlog;
    S.ylog = !!a.ylog;
    ylogBtn.setAttribute('aria-pressed', S.ylog ? 'true' : 'false');
    if (ex !== S.expr || xl !== S.xlog) {
      S.expr = ex;
      S.xlog = xl;
      picker.set({expr: ex, log: xl});
      reload();
    } else draw();
  }));
  const sel = await app.selectionFor(ds);
  offs.push(sel.onChange(() => reloadLayersSoon()));

  // ------------------------------------------------------------ sizing
  function measure() {
    const r = stage.getBoundingClientRect();
    const w = Math.floor(r.width), h = Math.floor(r.height);
    const dpr = window.devicePixelRatio || 1;
    if (w === S.w && h === S.h && dpr === S.dpr) return;
    S.w = w; S.h = h; S.dpr = dpr;
    draw();
  }
  const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(measure) : null;
  if (ro) ro.observe(stage);
  measure();
  await reload();

  draw();

  return {
    firstPaint: fp.promise,
    resize() { measure(); },
    onVisible(v) { visible = !!v; if (visible) { measure(); draw(); } },
    destroy() {
      destroyed = true;
      fp.resolve();
      if (frame) cancelAnimationFrame(frame);
      if (layerTimer) clearTimeout(layerTimer);
      for (const off of offs) if (typeof off === 'function') off();
      if (ro) ro.disconnect();
      picker.destroy();
      ylogBtn.remove();
      root.remove();
    },
    async snapshot() {
      const view = S.view || buildView();
      const tk = tokensOf(ctx.theme());
      const svg = toSVG(view, [{kind: 'draw', tokens: tk, fn: (c, v) => paint(c, v, tk)}]);
      return new Blob([svg], {type: 'image/svg+xml'});
    },
    stats() {
      return {renderMs: S.renderMs, expr: S.expr, bins: nbins, total: S.all ? S.all.total : 0,
        shown: S.vis ? S.vis.total : 0, selected: S.sel ? S.sel.total : 0, range: curRange()};
    },
    /** Range brush from tests / scripts: shape {kind:'range', lo, hi} or {x0, x1}; null clears. */
    async brush(shape) {
      if (!shape) { setRange(null); return 0; }
      setRange([bound(shape.lo ?? shape.x0), bound(shape.hi ?? shape.x1)]);
      return new Promise((resolve) => {
        let done = false;
        const finish = () => {
          if (done) return;
          done = true;
          off();
          clearTimeout(t);
          resolve((store.get(`vis:${ds}`) || {}).count ?? 0);
        };
        const off = store.on([`vis:${ds}`], finish);
        const t = setTimeout(finish, 5000);
      });
    },
  };
}
