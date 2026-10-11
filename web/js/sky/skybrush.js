// Rectangle / circle / lasso brushes on the sky (ARCHITECTURE.md section 8.2 brush_* on sky;
// IMPLEMENTATION_NOTES 40 and 67).
//
// A transparent canvas over the Aladin view captures the pointer while a brush mode is active
// (pan mode leaves the pointer to Aladin). Screen points are converted with pix2world and the
// shape is sent to DataClient.brush with the sky ViewRef {pane, kind:'sky', x:'ra', y:'dec'},
// which the worker evaluates in degrees with no RA wrap. Shapes in data coordinates:
//   rect    {x0, x1, y0, y1}: RA/Dec bounds of the dragged screen rectangle's corners;
//   circle  {cx, cy, r, ry}: a great-circle radius rdeg becomes the RA/Dec ellipse
//           r = rdeg / cos(dec0) (RA semi-axis), ry = rdeg;
//   lasso   {pts: [[ra, dec], ...]}.
// The result goes through Selection.apply with the modifier op (shift add, alt subtract,
// shift+alt intersect) and a RecipeOp, then `brush:commit` is emitted. While a drag is active
// `keys:escape` cancels it; in a brush mode with no drag, Escape returns to pan.

import {opFromEvent} from '../core/selection.js';
import {separation} from './aladin.js';

const D2R = Math.PI / 180;
export const SKY_BRUSH_MODES = Object.freeze(['rect', 'circle', 'lasso']);
const MIN_DRAG_PX = 3;
const LABELS = {pan: 'Pan', rect: 'Rectangle', circle: 'Circle', lasso: 'Lasso'};
const ICONS = {pan: '✥', rect: '▭', circle: '◯', lasso: '➰'};

/** Sky ViewRef for a pane (note 67). */
export function skyView(pane) {
  return {pane, kind: 'sky', x: 'ra', y: 'dec'};
}

/**
 * Data-space shape from screen geometry. `toWorld(x, y)` -> [ra, dec] | null.
 * screen: {kind:'rect', x0, y0, x1, y1} | {kind:'circle', cx, cy, r} | {kind:'lasso', pts}.
 * Returns null when a point is off the sky.
 */
export function screenToSkyShape(screen, toWorld) {
  if (screen.kind === 'rect') {
    const cs = [[screen.x0, screen.y0], [screen.x1, screen.y0], [screen.x1, screen.y1], [screen.x0, screen.y1]].map((p) => toWorld(p[0], p[1]));
    if (cs.some((c) => !c)) return null;
    const ras = unwrap(cs.map((c) => c[0]));
    const decs = cs.map((c) => c[1]);
    return {kind: 'rect', x0: Math.min(...ras), x1: Math.max(...ras), y0: Math.min(...decs), y1: Math.max(...decs)};
  }
  if (screen.kind === 'circle') {
    const c = toWorld(screen.cx, screen.cy);
    const e = toWorld(screen.cx + screen.r, screen.cy);
    if (!c || !e) return null;
    const rdeg = separation(c[0], c[1], e[0], e[1]);
    const cosd = Math.max(1e-6, Math.cos(c[1] * D2R));
    return {kind: 'circle', cx: c[0], cy: c[1], r: rdeg / cosd, ry: rdeg};
  }
  if (screen.kind === 'lasso') {
    const pts = screen.pts.map((p) => toWorld(p[0], p[1]));
    if (pts.some((p) => !p)) return null;
    const ras = unwrap(pts.map((p) => p[0]));
    return {kind: 'lasso', pts: pts.map((p, k) => [ras[k], p[1]])};
  }
  return null;
}

/** RA values made continuous around the first one (no jump across 0/360). */
function unwrap(ras) {
  const r0 = ras[0];
  return ras.map((r) => {
    let d = r - r0;
    if (d > 180) d -= 360;
    if (d < -180) d += 360;
    return r0 + d;
  });
}

export class SkyBrush {
  /**
   * opts: {h (AladinHandle), stage (element holding the viewer), app, store, ds, pane,
   *        client: () => Promise<DataClient>, modes (subset of SKY_BRUSH_MODES), tokens}
   */
  constructor(opts) {
    Object.assign(this, {h: opts.h, stage: opts.stage, app: opts.app, store: opts.store, ds: opts.ds,
      pane: opts.pane, getClient: opts.client, tokens: opts.tokens || (() => ({}))});
    this.modes = (opts.modes || SKY_BRUSH_MODES).filter((m) => SKY_BRUSH_MODES.includes(m));
    this.mode = 'pan';
    this.drag = null;
    this.canvas = document.createElement('canvas');
    this.canvas.className = 'pk-sky-brush';
    this.canvas.setAttribute('aria-hidden', 'true');
    this.stage.appendChild(this.canvas);
    this.bar = null;
    this.offs = [];
    const c = this.canvas;
    const down = (e) => this._down(e);
    const move = (e) => this._move(e);
    const up = (e) => this._up(e);
    const cancel = () => this._cancel();
    c.addEventListener('pointerdown', down);
    c.addEventListener('pointermove', move);
    c.addEventListener('pointerup', up);
    c.addEventListener('pointercancel', cancel);
    this.offs.push(() => {
      c.removeEventListener('pointerdown', down);
      c.removeEventListener('pointermove', move);
      c.removeEventListener('pointerup', up);
      c.removeEventListener('pointercancel', cancel);
    });
    this.offs.push(this.store.onEvent('keys:escape', (p) => {
      if (p && p.handled) return;
      if (this.drag) {
        this._cancel();
        if (p) p.handled = true;
      } else if (this.mode !== 'pan' && this._focusedHere()) {
        this.setMode('pan');
        if (p) p.handled = true;
      }
    }));
    this.offs.push(this.store.onEvent('selection:invert', (p) => this._invert(p)));
    this._sync();
  }

  _focusedHere() {
    const a = document.activeElement;
    return !!(a && (this.stage.contains(a) || (this.bar && this.bar.contains(a)) || this.stage.closest('.pk-pane')?.contains(a)));
  }

  /** Mode toolbar (pan + brush modes), built once. */
  toolbar() {
    if (this.bar) return this.bar;
    const g = document.createElement('div');
    g.className = 'pk-btn-group pk-sky-brushbar';
    g.setAttribute('role', 'group');
    g.setAttribute('aria-label', 'Sky brush mode');
    for (const m of ['pan', ...this.modes]) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'pk-btn pk-icon-btn';
      b.dataset.mode = m;
      b.textContent = ICONS[m];
      const hint = m === 'pan' ? 'Pan and zoom the map' : `${LABELS[m]} select (shift adds, alt subtracts, shift+alt intersects)`;
      b.title = hint;
      b.setAttribute('aria-label', hint);
      b.addEventListener('click', () => this.setMode(m));
      g.appendChild(b);
    }
    this.bar = g;
    this._sync();
    return g;
  }

  setMode(m) {
    this.mode = m === 'pan' || this.modes.includes(m) ? m : 'pan';
    this._cancel();
    this._sync();
  }

  _sync() {
    this.canvas.classList.toggle('pk-active', this.mode !== 'pan');
    this.stage.dataset.brush = this.mode;
    if (this.bar) {
      for (const b of this.bar.querySelectorAll('button[data-mode]')) {
        b.setAttribute('aria-pressed', String(b.dataset.mode === this.mode));
      }
    }
  }

  _pt(e) {
    const r = this.canvas.getBoundingClientRect();
    return [e.clientX - r.left, e.clientY - r.top];
  }

  _down(e) {
    if (this.mode === 'pan' || e.button !== 0) return;
    e.preventDefault();
    if (this.canvas.setPointerCapture) this.canvas.setPointerCapture(e.pointerId);
    const p = this._pt(e);
    this.drag = {kind: this.mode, start: p, cur: p, pts: [p], op: opFromEvent(e)};
    this._draw();
  }

  _move(e) {
    if (!this.drag) return;
    const p = this._pt(e);
    this.drag.cur = p;
    if (this.drag.kind === 'lasso') {
      const last = this.drag.pts[this.drag.pts.length - 1];
      if (Math.hypot(p[0] - last[0], p[1] - last[1]) >= 2) this.drag.pts.push(p);
    }
    this._draw();
    const sc = this._screen();
    if (sc) this.store.emit('brush:preview', {pane: this.pane, shape: sc});
  }

  async _up(e) {
    const d = this.drag;
    if (!d) return;
    d.cur = this._pt(e);
    const op = opFromEvent(e) !== 'replace' ? opFromEvent(e) : d.op;
    const screen = this._screen();
    this.drag = null;
    this._clear();
    if (!screen) return;
    const shape = screenToSkyShape(screen, (x, y) => this.h.pix2world(x, y));
    if (!shape) {
      this.store.emit('toast', {level: 'warn', text: 'The brush reaches off the sky; nothing was selected.'});
      return;
    }
    await this.commit(shape, op);
  }

  _screen() {
    const d = this.drag;
    if (!d) return null;
    const [x0, y0] = d.start, [x1, y1] = d.cur;
    if (d.kind === 'rect') {
      if (Math.abs(x1 - x0) < MIN_DRAG_PX && Math.abs(y1 - y0) < MIN_DRAG_PX) return null;
      return {kind: 'rect', x0: Math.min(x0, x1), y0: Math.min(y0, y1), x1: Math.max(x0, x1), y1: Math.max(y0, y1)};
    }
    if (d.kind === 'circle') {
      const r = Math.hypot(x1 - x0, y1 - y0);
      return r < MIN_DRAG_PX ? null : {kind: 'circle', cx: x0, cy: y0, r};
    }
    if (d.kind === 'lasso') return d.pts.length >= 3 ? {kind: 'lasso', pts: d.pts.slice()} : null;
    return null;
  }

  _cancel() {
    this.drag = null;
    this._clear();
  }

  _clear() {
    const c = this.canvas;
    const ctx = c.getContext('2d');
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, c.width, c.height);
  }

  _draw() {
    const c = this.canvas;
    const r = c.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.round(r.width * dpr)), hgt = Math.max(1, Math.round(r.height * dpr));
    if (c.width !== w) c.width = w;
    if (c.height !== hgt) c.height = hgt;
    const ctx = c.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, r.width, r.height);
    const s = this._screen();
    if (!s) return;
    const t = this.tokens();
    const col = t.sel || '#d9822b';
    ctx.strokeStyle = col;
    ctx.lineWidth = 1.5;
    ctx.setLineDash([5, 4]);
    ctx.fillStyle = col;
    ctx.beginPath();
    if (s.kind === 'rect') ctx.rect(s.x0, s.y0, s.x1 - s.x0, s.y1 - s.y0);
    else if (s.kind === 'circle') ctx.arc(s.cx, s.cy, s.r, 0, 2 * Math.PI);
    else {
      ctx.moveTo(s.pts[0][0], s.pts[0][1]);
      for (const p of s.pts.slice(1)) ctx.lineTo(p[0], p[1]);
      ctx.closePath();
    }
    ctx.globalAlpha = 0.15;
    ctx.fill();
    ctx.globalAlpha = 1;
    ctx.stroke();
  }

  /** Apply a data-space shape with `op`; resolves the new selection count. */
  async commit(shape, op = 'replace') {
    const client = await this.getClient();
    const sel = await this.app.selectionFor(this.ds);
    const res = await client.brush(skyView(this.pane), shape);
    const {kind, ...geom} = shape;
    sel.apply(res.hit, op, res.drawable, {op, kind, view: {ds: this.ds, x: 'ra', y: 'dec'}, shape: geom});
    this.store.emit('brush:commit', {pane: this.pane, ds: this.ds, shape, op, count: sel.count});
    return sel.count;
  }

  async _invert(p) {
    if (!p || p.handled || (p.ds && p.ds !== this.ds)) return;
    p.handled = true;
    const client = await this.getClient();
    const sel = await this.app.selectionFor(this.ds);
    const res = await client.brush(skyView(this.pane), {kind: 'rect', x0: -1e9, x1: 1e9, y0: -1e9, y1: 1e9});
    sel.invert(res.drawable);
  }

  destroy() {
    for (const off of this.offs.splice(0)) off();
    this.canvas.remove();
    if (this.bar) this.bar.remove();
  }
}
