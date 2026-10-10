// Hover card (capability hover_preview, ARCHITECTURE.md section 8.2).
//
// Follows store `hover` = {ds, i}. The card sits next to the pointer and flips to the other
// side of the pointer at the viewport edges. Its content is rebuilt only when the hovered
// index changes; pointer moves only reposition it. Values come from cached column values
// (quantized, shown with step-aware precision) and never from row chunks. One small figure is
// added when available: the item's thumbnail PNG, else a mini spectrum (pks1 records of the
// same catalog), else a mini SED (filters dataset, catalogs up to T1_MAX rows).
//
// placeCard() and pickHoverColumns() are pure and covered by tests/js/export.test.mjs.

import {T1_MAX} from '../core/constants.js';
import {fmtQuant, fmtNum, fmtValue, MISSING} from '../core/fmt.js';
import {sedPoints} from '../data/filters.js';

const SKIP_ROLES = new Set(['id', 'ra', 'dec', 'text', 'url', 'image', 'limit', 'error']);
const ROLE_RANK = {category: 0, flag: 1, mag: 2, color: 3, flux: 4, value: 5};
const MAX_VALUE_COLS = 4;
const VALUE_CACHE_COLS = 24;
const BIG_N = 2000000;

/**
 * Card position for pointer (x, y), card size (w, h) and viewport (vw, vh).
 * The card goes below-right of the pointer by `offset`; it flips to the left / above when it
 * would cross the right / bottom edge, and is clamped inside `margin` when it fits nowhere.
 * Non-finite pointer coordinates place the card at the top-left margin.
 * Returns {left, top, flipX, flipY}.
 */
export function placeCard({x, y, w, h, vw, vh, offset = 14, margin = 8} = {}) {
  const W = Number.isFinite(w) && w > 0 ? w : 0;
  const H = Number.isFinite(h) && h > 0 ? h : 0;
  const VW = Number.isFinite(vw) && vw > 0 ? vw : W + 2 * margin;
  const VH = Number.isFinite(vh) && vh > 0 ? vh : H + 2 * margin;
  if (!Number.isFinite(x) || !Number.isFinite(y)) return {left: margin, top: margin, flipX: false, flipY: false};
  const clamp = (v, lo, hi) => (hi < lo ? lo : Math.max(lo, Math.min(hi, v)));
  let left = x + offset;
  let flipX = false;
  if (left + W > VW - margin) {
    const alt = x - offset - W;
    if (alt >= margin) {
      left = alt;
      flipX = true;
    } else {
      // Neither side fits whole: use the side with more room, then clamp.
      flipX = x > VW / 2;
      left = flipX ? alt : left;
    }
  }
  let top = y + offset;
  let flipY = false;
  if (top + H > VH - margin) {
    const alt = y - offset - H;
    if (alt >= margin) {
      top = alt;
      flipY = true;
    } else {
      flipY = y > VH / 2;
      top = flipY ? alt : top;
    }
  }
  left = clamp(left, margin, VW - margin - W);
  top = clamp(top, margin, VH - margin - H);
  return {left: Math.round(left), top: Math.round(top), flipX, flipY};
}

/**
 * Columns worth showing on a hover card: category and flag columns first, then magnitudes,
 * colours, fluxes and values (manifest order inside each role); ids, positions, text, urls,
 * errors and limit tables are skipped. `prefer` keys (e.g. the colour-by key) come first.
 */
export function pickHoverColumns(cols, n = MAX_VALUE_COLS, prefer = []) {
  const list = Array.isArray(cols) ? cols : [];
  const out = [];
  const byKey = new Map(list.map((c) => [c && c.key, c]));
  for (const k of prefer || []) {
    const c = byKey.get(k);
    if (c && !SKIP_ROLES.has(c.role) && !out.includes(c) && out.length < n) out.push(c);
  }
  const ranked = list
    .map((c, k) => ({c, k}))
    .filter(({c}) => c && c.key && !String(c.key).startsWith('_') && !SKIP_ROLES.has(c.role) && c.enc !== 'str'
      && Object.prototype.hasOwnProperty.call(ROLE_RANK, c.role || 'value'))
    .sort((a, b) => (ROLE_RANK[a.c.role || 'value'] - ROLE_RANK[b.c.role || 'value']) || (a.k - b.k));
  for (const {c} of ranked) {
    if (out.length >= n) break;
    if (!out.includes(c)) out.push(c);
  }
  return out;
}

/** Text of one cached value for the card (category levels, quantized steps). */
export function hoverValueText(v, meta) {
  if (!meta) return {text: fmtNum(v), title: ''};
  if (meta.role === 'category' || meta.levels) {
    if (!Number.isFinite(v)) return {text: MISSING, title: 'missing'};
    const lv = Array.isArray(meta.levels) ? meta.levels[v | 0] : undefined;
    return {text: lv !== undefined ? String(lv) : String(v), title: ''};
  }
  if (meta.role === 'flag') {
    if (!Number.isFinite(v)) return {text: MISSING, title: 'missing'};
    return {text: v ? 'yes' : 'no', title: ''};
  }
  if (meta.step > 0 && /^u(8|16)q$/.test(meta.enc || '')) return fmtQuant(v, meta.step);
  return fmtValue(v, meta, {exact: false});
}

// ---------------------------------------------------------------------------- figures

/** Polyline sparkline of y(x) into a canvas (finite points only). */
function drawLine(canvas, xs, ys, t, opts = {}) {
  const dpr = (typeof window !== 'undefined' && window.devicePixelRatio) || 1;
  const W = opts.w || 180, H = opts.h || 56;
  canvas.width = Math.round(W * dpr);
  canvas.height = Math.round(H * dpr);
  canvas.style.width = `${W}px`;
  canvas.style.height = `${H}px`;
  const g = canvas.getContext('2d');
  if (!g) return;
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, W, H);
  g.fillStyle = t.plotBg || t.bg2 || 'transparent';
  g.fillRect(0, 0, W, H);
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  const n = Math.min(xs.length, ys.length);
  const tx = (v) => (opts.logx ? (v > 0 ? Math.log10(v) : NaN) : v);
  const ty = (v) => (opts.logy ? (v > 0 ? Math.log10(v) : NaN) : v);
  for (let k = 0; k < n; k++) {
    const a = tx(xs[k]), b = ty(ys[k]);
    if (!Number.isFinite(a) || !Number.isFinite(b)) continue;
    if (a < x0) x0 = a;
    if (a > x1) x1 = a;
    if (b < y0) y0 = b;
    if (b > y1) y1 = b;
  }
  if (!(x1 > x0)) { x0 -= 0.5; x1 += 0.5; }
  if (!(y1 > y0)) { y0 -= 0.5; y1 += 0.5; }
  if (!Number.isFinite(x0) || !Number.isFinite(y0)) return;
  const pad = 4;
  const px = (a) => pad + ((a - x0) / (x1 - x0)) * (W - 2 * pad);
  const py = (b) => H - pad - ((b - y0) / (y1 - y0)) * (H - 2 * pad);
  g.strokeStyle = t.accent || '#3b82f6';
  g.fillStyle = t.accent || '#3b82f6';
  g.lineWidth = 1;
  if (opts.points) {
    for (let k = 0; k < n; k++) {
      const a = tx(xs[k]), b = ty(ys[k]);
      if (!Number.isFinite(a) || !Number.isFinite(b)) continue;
      g.beginPath();
      if (opts.kinds && opts.kinds[k] !== 'det') {
        // limits: open triangle pointing down (upper) / up (lower)
        const dir = opts.kinds[k] === 'ul' ? 1 : -1;
        g.moveTo(px(a) - 3, py(b) - 2 * dir);
        g.lineTo(px(a) + 3, py(b) - 2 * dir);
        g.lineTo(px(a), py(b) + 3 * dir);
        g.closePath();
        g.stroke();
      } else {
        g.arc(px(a), py(b), 2.2, 0, 2 * Math.PI);
        g.fill();
      }
    }
    return;
  }
  g.beginPath();
  let pen = false;
  // Decimate to about one point per pixel column.
  const stride = Math.max(1, Math.floor(n / (W * 2)));
  for (let k = 0; k < n; k += stride) {
    const a = tx(xs[k]), b = ty(ys[k]);
    if (!Number.isFinite(a) || !Number.isFinite(b)) { pen = false; continue; }
    if (pen) g.lineTo(px(a), py(b));
    else g.moveTo(px(a), py(b));
    pen = true;
  }
  g.stroke();
}

// ---------------------------------------------------------------------------- install

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

export function install(app) {
  const store = app.store;
  const page = app.page || {};
  const datasets = page.datasets || {};
  const card = el('div', 'pk-hovercard');
  card.setAttribute('role', 'tooltip');
  card.id = 'pk-hovercard';
  card.hidden = true;
  document.body.append(card);

  let px = NaN, py = NaN, pointerType = 'mouse', inside = true;
  let cur = null;           // {ds, i}
  let token = 0;
  let redraw = null;        // figure redraw on theme change
  let raf = 0;

  const valueCache = new Map();   // `${ds}\0${key}` -> Promise<Float32Array>
  const imageIndex = new Map();   // imgDs -> Promise<{byRow, byId, base}>
  const filtersDoc = new Map();

  function values(ds, client, key) {
    const k = `${ds}\u0000${key}`;
    if (valueCache.has(k)) {
      const p = valueCache.get(k);
      valueCache.delete(k);
      valueCache.set(k, p);
      return p;
    }
    const p = client.values(key);
    p.catch(() => valueCache.delete(k));
    valueCache.set(k, p);
    while (valueCache.size > VALUE_CACHE_COLS) valueCache.delete(valueCache.keys().next().value);
    return p;
  }

  function position() {
    if (card.hidden) return;
    const r = card.getBoundingClientRect();
    const p = placeCard({x: px, y: py, w: r.width, h: r.height, vw: window.innerWidth, vh: window.innerHeight});
    card.style.left = `${p.left}px`;
    card.style.top = `${p.top}px`;
    card.classList.toggle('pk-flip-x', p.flipX);
    card.classList.toggle('pk-flip-y', p.flipY);
  }

  function schedule() {
    if (raf) return;
    raf = requestAnimationFrame(() => {
      raf = 0;
      position();
    });
  }

  window.addEventListener('pointermove', (ev) => {
    px = ev.clientX;
    py = ev.clientY;
    pointerType = ev.pointerType || 'mouse';
    inside = true;
    if (!card.hidden) schedule();
  }, {passive: true, capture: true});
  document.documentElement.addEventListener('pointerleave', () => {
    inside = false;
    hide();
  });
  window.addEventListener('blur', () => hide());

  function hide() {
    card.hidden = true;
    redraw = null;
  }

  function showIfReady() {
    if (pointerType === 'touch' || !inside || !Number.isFinite(px)) return;
    card.hidden = false;
    position();
  }

  function datasetsOf(kind, pred) {
    return Object.entries(datasets).filter(([, r]) => r && r.kind === kind && (!pred || pred(r))).map(([k]) => k);
  }

  function imagesIndexFor(imgDs) {
    if (imageIndex.has(imgDs)) return imageIndex.get(imgDs);
    const p = app.hub.json(imgDs).then((doc) => {
      const byRow = new Map();
      const byId = new Map();
      const items = (doc && doc.items) || [];
      items.forEach((it, k) => {
        if (!it) return;
        if (Number.isInteger(it.row)) byRow.set(it.row, k);
        if (it.id !== undefined) byId.set(String(it.id), k);
      });
      // Item URLs resolve against the page URL (section 4.3).
      const base = typeof document !== 'undefined' ? document.baseURI : undefined;
      return {doc, items, byRow, byId, base};
    });
    p.catch(() => imageIndex.delete(imgDs));
    imageIndex.set(imgDs, p);
    return p;
  }

  function thumbUrl(item, base, roleKey) {
    if (!item || !item.roles) return null;
    const keys = Object.keys(item.roles);
    const rk = roleKey && item.roles[roleKey] ? roleKey : keys[0];
    const r = rk ? item.roles[rk] : null;
    if (!r) return null;
    const u = r.thumb || r.url;
    if (!u) return null;
    return new URL(app.hub.resolve(u), base).toString();
  }

  function addThumb(body, url, alt) {
    const img = el('img', 'pk-hover-thumb');
    img.alt = alt || '';
    img.decoding = 'async';
    img.src = url;
    img.addEventListener('load', () => position(), {once: true});
    img.addEventListener('error', () => img.remove(), {once: true});
    body.append(img);
  }

  async function buildImageItem(ds, i, my) {
    const idx = await imagesIndexFor(ds);
    if (my !== token) return;
    const it = idx.items[i];
    card.textContent = '';
    if (!it) { hide(); return; }
    card.append(el('div', 'pk-hover-id', String(it.id ?? i)));
    if (it.caption) card.append(el('div', 'pk-hover-caption', String(it.caption)));
    const tags = it.tags && typeof it.tags === 'object' ? Object.entries(it.tags).slice(0, 4) : [];
    if (tags.length) {
      const dl = el('dl', 'pk-hover-vals');
      for (const [k, v] of tags) dl.append(el('dt', null, k), el('dd', null, String(v)));
      card.append(dl);
    }
    const u = thumbUrl(it, idx.base, store.get('role'));
    if (u) addThumb(card, u, String(it.id ?? ''));
    showIfReady();
  }

  async function buildCatalogRow(ds, i, my) {
    const client = await app.catalog(ds);
    if (my !== token) return;
    const id = await app.idOf(ds, i);
    if (my !== token) return;
    card.textContent = '';
    card.append(el('div', 'pk-hover-id', String(id)));
    showIfReady();
    const color = store.get('color');
    const prefer = color && color.by && client.col(color.by) ? [color.by] : [];
    const ncols = client.n > BIG_N ? 2 : MAX_VALUE_COLS;
    const cols = pickHoverColumns(client.manifest.cols, ncols, prefer);
    if (cols.length) {
      const arrs = await Promise.all(cols.map((c) => values(ds, client, c.key)));
      if (my !== token) return;
      const dl = el('dl', 'pk-hover-vals');
      cols.forEach((c, k) => {
        const v = arrs[k] ? arrs[k][i] : NaN;
        const {text, title} = hoverValueText(v, c);
        const dt = el('dt', null, c.label || c.key);
        const dd = el('dd', 'pk-num', text);
        if (title) dd.title = title;
        if (c.unit && c.role !== 'category' && text !== MISSING) dd.append(el('span', 'pk-unit', ` ${c.unit}`));
        dl.append(dt, dd);
      });
      card.append(dl);
      position();
    }
    await addFigure(ds, client, i, id, my);
  }

  async function addFigure(ds, client, i, id, my) {
    // 1. thumbnail from an images set joined to this catalog
    for (const imgDs of datasetsOf('images', (r) => r.catalog === ds)) {
      const idx = await imagesIndexFor(imgDs);
      if (my !== token) return;
      const k = idx.byRow.has(i) ? idx.byRow.get(i) : idx.byId.get(String(id));
      const u = k !== undefined ? thumbUrl(idx.items[k], idx.base, store.get('role')) : null;
      if (u) {
        addThumb(card, u, String(id));
        return;
      }
    }
    // 2. mini spectrum
    for (const rds of datasetsOf('records', (r) => r.catalog === ds && (r.record || 'pks1') === 'pks1')) {
      const rs = await app.hub.records(rds);
      if (my !== token) return;
      if (!rs.has(id)) continue;
      const spec = await rs.get(id, {token: my, channel: 'hover'});
      if (my !== token || !spec || !spec.channels) return;
      const names = Object.keys(spec.channels);
      const ch = spec.channels.flux || spec.channels[names[0]];
      if (!ch) continue;
      const cv = el('canvas', 'pk-hover-fig');
      cv.setAttribute('aria-label', `spectrum of ${id}`);
      card.append(cv);
      redraw = () => drawLine(cv, spec.wave, ch, app.theme(), {w: 180, h: 56});
      redraw();
      position();
      return;
    }
    // 3. mini SED
    if (client.n > T1_MAX) return;
    const fds = datasetsOf('filters')[0];
    if (!fds) return;
    if (!filtersDoc.has(fds)) {
      const p = app.hub.json(fds);
      p.catch(() => filtersDoc.delete(fds));
      filtersDoc.set(fds, p);
    }
    const fdoc = await filtersDoc.get(fds);
    if (my !== token) return;
    const sedCols = (client.manifest.cols || []).filter((c) => (c.role === 'mag' || c.role === 'flux') && c.filter);
    if (sedCols.length < 2) return;
    const need = [];
    for (const c of sedCols) {
      need.push(c.key);
      if (c.err && client.col(c.err)) need.push(c.err);
    }
    const arrs = await Promise.all(need.map((k) => values(ds, client, k)));
    if (my !== token) return;
    const row = {};
    need.forEach((k, j) => { row[k] = arrs[j][i]; });
    const pts = sedPoints(row, fdoc, sedCols);
    if (pts.length < 2) return;
    const cv = el('canvas', 'pk-hover-fig');
    cv.setAttribute('aria-label', `SED of ${id}`);
    card.append(cv);
    redraw = () => drawLine(cv, pts.map((p) => p.wl), pts.map((p) => p.fnu), app.theme(),
      {w: 180, h: 56, logx: true, logy: true, points: true, kinds: pts.map((p) => p.kind)});
    redraw();
    position();
  }

  function update() {
    const hv = store.get('hover');
    if (!hv || !hv.ds || !Number.isInteger(hv.i) || hv.i < 0) {
      cur = null;
      token++;
      hide();
      return;
    }
    if (cur && cur.ds === hv.ds && cur.i === hv.i) {
      showIfReady();
      return;
    }
    cur = {ds: hv.ds, i: hv.i};
    const my = ++token;
    redraw = null;
    const ref = datasets[hv.ds];
    const kind = ref && ref.kind;
    const job = kind === 'images' ? buildImageItem(hv.ds, hv.i, my)
      : kind === 'catalog' ? buildCatalogRow(hv.ds, hv.i, my) : null;
    if (!job) { hide(); return; }
    job.catch((err) => {
      if (!(err instanceof Error)) throw err;
      if (err.name === 'AbortError') return;
      // A failed preview is not worth a toast: the card keeps what it has.
      console.warn('hover card', err);
    });
  }

  store.on(['hover'], update);
  store.onEvent('theme:change', () => { if (redraw && !card.hidden) redraw(); });
  return {card, placeCard};
}
