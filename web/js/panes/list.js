// Source list pane (capabilities source_list_search, keyboard_nav; ARCHITECTURE.md 6.10).
//
// A virtualized list for any N: only the visible rows exist in the DOM, and very tall lists
// map the scroll position onto rows proportionally so the browser's element height limit is
// never reached. Rows follow the filtered (vis:<ds>) order, optionally sorted by a column
// (NaN last), restricted to the selection ("selection only") or to search results from
// DataClient.find (id, alias, case-insensitive, prefix, substring, coordinates).
//
// Store: writes focus {ds, i, id} and hover {ds, i}; reads sel:<ds>, vis:<ds>, filters:<ds>
// and focus. Handles the `nav:step` offer, so arrow keys walk the filtered, sorted order.
// Click focuses; Shift-click adds to the selection, Alt-click removes, Ctrl/Cmd-click toggles.

import {fmtCount, fmtQuant, fmtValue, MISSING} from '../core/fmt.js';
import {pickHoverColumns} from '../ui/hover.js';
import {mountSelectionTools} from '../ui/export.js';

// Category colouring helpers. These mirror ui/legend.js (WP08) and are kept local so this pane
// does not depend on exports outside the contract.
const FALLBACK_CAT = ['#3b82c4', '#e0812f', '#3f9b4a', '#c8463d', '#8a64b5', '#8c5a48', '#d36fb0', '#7b818a', '#b1b235', '#2fb3c4'];

/** True when a column meta describes a category column with levels. */
function isCategory(meta) {
  if (!meta) return false;
  if (!(meta.role === 'category' || meta.enc === 'cat8' || meta.enc === 'cat16')) return false;
  return Array.isArray(meta.levels) && meta.levels.length > 0;
}

/** CSS colour per level: manifest colors{level: hex}, else the theme category palette. */
function levelColors(meta, tokens) {
  const levels = (meta && meta.levels) || [];
  const pal = (tokens && Array.isArray(tokens.cat) && tokens.cat.some(Boolean)) ? tokens.cat : FALLBACK_CAT;
  const named = (meta && meta.colors) || {};
  return levels.map((lv, i) => named[lv] || pal[i % pal.length] || FALLBACK_CAT[i % FALLBACK_CAT.length]);
}

export const meta = {
  type: 'list',
  caps: ['source_list_search', 'keyboard_nav', 'hover_preview', 'class_coloring', 'filter_toggles', 'selection_export'],
  needs: ['catalog'],
};

const MAX_SCROLL_PX = 4000000;   // below every browser's element height limit
const SEARCH_LIMIT = 5000;
const SORT_MAX = 2000000;
const ID_SORT_MAX = 500000;
const DEFAULT_ROW_H = 24;

/** Parse the `sort` option: "key", "-key", {key, dir} or [key, dir] -> {key, dir: 1|-1} | null. */
export function parseSort(s) {
  if (!s) return null;
  if (typeof s === 'string') {
    const t = s.trim();
    if (!t) return null;
    if (t.startsWith('-')) return {key: t.slice(1), dir: -1};
    if (t.startsWith('+')) return {key: t.slice(1), dir: 1};
    return {key: t, dir: 1};
  }
  if (Array.isArray(s) && s.length) return parseSort(s[1] === 'desc' || s[1] === -1 ? `-${s[0]}` : String(s[0]));
  if (typeof s === 'object' && s.key) return {key: String(s.key), dir: s.dir === -1 || s.dir === 'desc' ? -1 : 1};
  return null;
}

const LITTLE_ENDIAN = new Uint8Array(new Uint32Array([1]).buffer)[0] === 1;

/**
 * Sort row indices `idx` (Int32Array, modified in place) by numeric `vals` (NaN last in both
 * directions); ties keep their order in `idx`. A stable LSD radix sort over the float64 bit
 * patterns (four 16-bit passes, O(n)) keeps 2M rows well under a second on the main thread.
 */
export function sortIndices(idx, vals, dir = 1) {
  const n = idx.length;
  const neg = dir < 0;
  let m = 0;
  for (let p = 0; p < n; p++) {
    const v = +vals[idx[p]];
    if (v === v) m++;
  }
  const keys = new Float64Array(m);
  let rows = new Int32Array(m);
  const nan = new Int32Array(n - m);
  let a = 0, b = 0;
  for (let p = 0; p < n; p++) {
    const r = idx[p];
    const v = +vals[r];
    if (v === v) {
      keys[a] = (neg ? -v : v) + 0;   // + 0 folds -0 into +0
      rows[a++] = r;
    } else nan[b++] = r;
  }
  if (m > 1) {
    const u = new Uint32Array(keys.buffer);
    let lo = new Uint32Array(m);
    let hi = new Uint32Array(m);
    const iLo = LITTLE_ENDIAN ? 0 : 1, iHi = 1 - iLo;
    for (let q = 0; q < m; q++) {
      let h = u[2 * q + iHi], l = u[2 * q + iLo];
      // Map the IEEE bits to an unsigned order: flip everything for negatives, else the sign.
      if (h & 0x80000000) { h = ~h >>> 0; l = ~l >>> 0; } else h = (h | 0x80000000) >>> 0;
      hi[q] = h;
      lo[q] = l;
    }
    let lo2 = new Uint32Array(m), hi2 = new Uint32Array(m), rows2 = new Int32Array(m);
    const cnt = new Uint32Array(65536);
    for (let pass = 0; pass < 4; pass++) {
      const src = pass < 2 ? lo : hi;
      const shift = pass % 2 ? 16 : 0;
      cnt.fill(0);
      for (let q = 0; q < m; q++) cnt[(src[q] >>> shift) & 0xffff]++;
      if (cnt[(src[0] >>> shift) & 0xffff] === m) continue;   // every key shares this digit
      let sum = 0;
      for (let d = 0; d < 65536; d++) { const c = cnt[d]; cnt[d] = sum; sum += c; }
      for (let q = 0; q < m; q++) {
        const t = cnt[(src[q] >>> shift) & 0xffff]++;
        lo2[t] = lo[q];
        hi2[t] = hi[q];
        rows2[t] = rows[q];
      }
      [lo, lo2] = [lo2, lo];
      [hi, hi2] = [hi2, hi];
      [rows, rows2] = [rows2, rows];
    }
  }
  idx.set(rows, 0);
  idx.set(nan, m);
  return idx;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

export function mount(root, ctx) {
  const cfg = ctx.config || {};
  const store = ctx.store;
  const app = ctx.app;
  const ds = ctx.dataset;
  const rowH = Number.isFinite(+cfg.rowHeight) && +cfg.rowHeight >= 12 ? +cfg.rowHeight : DEFAULT_ROW_H;
  const searchOn = cfg.search !== false;
  const virtualOn = cfg.virtual !== false;

  root.classList.add('pk-list');
  root.style.setProperty('--pk-row-h', `${rowH}px`);
  const bar = el('div', 'pk-list-bar');
  const search = el('input', 'pk-list-search');
  search.type = 'search';
  search.placeholder = 'Search id, alias or RA Dec';
  search.setAttribute('aria-label', 'Search sources by id, alias or position');
  search.spellcheck = false;
  search.autocomplete = 'off';
  const filterBtn = el('button', 'pk-btn pk-list-filter', 'Filter');
  filterBtn.type = 'button';
  filterBtn.title = 'Apply the search text as a filter to every panel';
  filterBtn.setAttribute('aria-pressed', 'false');
  const selOnlyBtn = el('button', 'pk-btn pk-list-selonly', 'Selected');
  selOnlyBtn.type = 'button';
  selOnlyBtn.title = 'Show only selected rows';
  selOnlyBtn.setAttribute('aria-pressed', 'false');
  const count = el('span', 'pk-list-count pk-muted');
  count.setAttribute('aria-live', 'polite');
  if (searchOn) bar.append(search, filterBtn);
  bar.append(selOnlyBtn, count);
  const head = el('div', 'pk-list-head');
  head.setAttribute('role', 'row');
  const view = el('div', 'pk-list-view');
  view.tabIndex = 0;
  view.setAttribute('role', 'listbox');
  view.setAttribute('aria-label', 'Sources');
  view.setAttribute('aria-multiselectable', 'true');
  const spacer = el('div', 'pk-list-spacer');
  const rowsEl = el('div', 'pk-list-rows');
  view.append(spacer, rowsEl);
  const empty = el('div', 'pk-list-empty pk-muted');
  empty.hidden = true;
  const skel = el('div', 'pk-skeleton pk-list-skeleton');
  root.append(bar, head, view, empty, skel);

  let client = null;
  let sel = null;
  let cols = [];                 // column metas shown after the id
  const colVals = new Map();     // key -> Float32Array
  let vis = null;                // Uint8Array | null
  let order = null;              // Int32Array | null (null = identity over n)
  let posOf = null;              // Int32Array stored row -> position in order (built lazily)
  let total = 0;                 // rows in the current order
  let sortSpec = parseSort(cfg.sort);
  let selOnly = !!cfg.showSelectionOnly;
  let query = '';
  let found = null;              // Int32Array of search hits (rank order) | null
  let orderToken = 0;
  let visible = true;
  let viewH = 0;
  let pool = [];
  let first = 0;
  let destroyed = false;
  let hoverI = -1;
  let rafPaint = 0;
  const offs = [];
  let offTools = null;
  let idSortCache = null;
  let colorVals = null;          // Float32Array of level indices for color.by (category)
  let colorPal = null;           // CSS colour per level

  selOnlyBtn.setAttribute('aria-pressed', String(selOnly));

  // ------------------------------------------------------------------ helpers

  function rowAt(pos) {
    return order ? order[pos] : pos;
  }

  function positionOf(i) {
    if (!order) return i >= 0 && i < total ? i : -1;
    if (!posOf) {
      posOf = new Int32Array(client.n).fill(-1);
      for (let k = 0; k < order.length; k++) posOf[order[k]] = k;
    }
    return i >= 0 && i < posOf.length ? posOf[i] : -1;
  }

  function scrollGeometry() {
    const full = total * rowH;
    const virt = Math.min(full, MAX_SCROLL_PX);
    const ratio = virt > viewH && full > virt ? (full - viewH) / (virt - viewH) : 1;
    return {full, virt, ratio};
  }

  function scrollToPos(pos, mode = 'nearest') {
    if (pos < 0) return;
    const {ratio} = scrollGeometry();
    const y = pos * rowH;
    const cur = view.scrollTop * ratio;
    let target = null;
    if (mode === 'center') target = y - viewH / 2 + rowH / 2;
    else if (y < cur) target = y;
    else if (y + rowH > cur + viewH) target = y + rowH - viewH;
    if (target !== null) view.scrollTop = Math.max(0, target / ratio);
    paint();
  }

  function cellText(meta, v) {
    if (meta.role === 'category' || Array.isArray(meta.levels)) {
      if (!Number.isFinite(v)) return {text: MISSING, title: 'missing'};
      const lv = meta.levels ? meta.levels[v | 0] : undefined;
      return {text: lv !== undefined ? String(lv) : String(v), title: ''};
    }
    if (meta.role === 'flag') return {text: Number.isFinite(v) ? (v ? 'yes' : 'no') : MISSING, title: ''};
    if (meta.step > 0 && /^u(8|16)q$/.test(meta.enc || '')) return fmtQuant(v, meta.step);
    return fmtValue(v, meta, {exact: false});
  }

  // ------------------------------------------------------------------ header

  function renderHead() {
    head.textContent = '';
    const mk = (key, label, title) => {
      const b = el('button', 'pk-list-th', label);
      b.type = 'button';
      b.dataset.key = key;
      b.title = title || `Sort by ${label}`;
      const on = sortSpec && sortSpec.key === key;
      b.setAttribute('aria-sort', on ? (sortSpec.dir > 0 ? 'ascending' : 'descending') : 'none');
      if (on) b.append(el('span', 'pk-sort-ind', sortSpec.dir > 0 ? ' ▲' : ' ▼'));
      head.append(b);
    };
    mk('__id', 'id', client && client.n > ID_SORT_MAX ? `Sorting by id is limited to ${fmtCount(ID_SORT_MAX)} rows` : 'Sort by id');
    for (const c of cols) mk(c.key, c.label || c.key, `${c.label || c.key}${c.unit ? ` [${c.unit}]` : ''}: sort`);
    root.style.setProperty('--pk-list-cols', String(cols.length));
  }

  head.addEventListener('click', (ev) => {
    const b = ev.target.closest('.pk-list-th');
    if (!b) return;
    const key = b.dataset.key;
    if (!sortSpec || sortSpec.key !== key) sortSpec = {key, dir: 1};
    else if (sortSpec.dir > 0) sortSpec = {key, dir: -1};
    else sortSpec = null;
    renderHead();
    recompute();
  });

  // ------------------------------------------------------------------ order

  async function idValues() {
    if (idSortCache) return idSortCache;
    const blob = await client.export({mask: 'all', format: 'ids'});
    const text = await blob.text();
    const ids = text.split('\n');
    if (ids.length && ids[ids.length - 1] === '') ids.pop();
    const ranks = new Float32Array(client.n);
    const k = Array.from(ids.keys());
    const coll = new Intl.Collator('en', {numeric: true});
    k.sort((a, b) => coll.compare(ids[a], ids[b]) || a - b);
    k.forEach((i, r) => { ranks[i] = r; });
    idSortCache = ranks;
    return ranks;
  }

  async function sortValues(key) {
    if (key === '__id') return idValues();
    const meta = client.col(key);
    const v = colVals.get(key) || await client.values(key);
    if (meta && Array.isArray(meta.levels)) {
      // categories sort by label
      const lv = meta.levels.map((l, j) => ({l: String(l), j}));
      lv.sort((a, b) => (a.l < b.l ? -1 : a.l > b.l ? 1 : 0));
      const rank = new Float32Array(meta.levels.length);
      lv.forEach((o, r) => { rank[o.j] = r; });
      const out = new Float32Array(v.length);
      for (let i = 0; i < v.length; i++) out[i] = Number.isFinite(v[i]) && v[i] < rank.length ? rank[v[i] | 0] : NaN;
      return out;
    }
    return v;
  }

  async function recompute() {
    if (!client) return;
    const my = ++orderToken;
    const n = client.n;
    const selMask = selOnly && sel ? sel.mask : null;
    let idx = null;
    if (found) {
      const keep = [];
      for (const i of found) if ((!vis || vis[i]) && (!selMask || selMask[i])) keep.push(i);
      idx = Int32Array.from(keep);
    } else if (vis || selMask) {
      let m = 0;
      for (let i = 0; i < n; i++) if ((!vis || vis[i]) && (!selMask || selMask[i])) m++;
      idx = new Int32Array(m);
      let k = 0;
      for (let i = 0; i < n; i++) if ((!vis || vis[i]) && (!selMask || selMask[i])) idx[k++] = i;
    }
    let note = '';
    if (sortSpec) {
      const len = idx ? idx.length : n;
      const limit = sortSpec.key === '__id' ? ID_SORT_MAX : SORT_MAX;
      if (len > limit || (sortSpec.key === '__id' && n > ID_SORT_MAX)) {
        note = `sort needs ≤ ${fmtCount(limit)} rows`;
      } else if (sortSpec.key !== '__id' && !client.col(sortSpec.key)) {
        note = `unknown sort column ${sortSpec.key}`;
      } else {
        ctx.setStatus('sorting…');
        const vals = await sortValues(sortSpec.key);
        if (my !== orderToken || destroyed) return;
        if (!idx) {
          idx = new Int32Array(n);
          for (let i = 0; i < n; i++) idx[i] = i;
        }
        sortIndices(idx, vals, sortSpec.dir);
      }
    }
    if (my !== orderToken || destroyed) return;
    order = idx;
    posOf = null;
    total = order ? order.length : n;
    updateCount(note);
    layoutSpacer();
    paint(true);
  }

  function updateCount(note = '') {
    const parts = [];
    const n = client ? client.n : 0;
    parts.push(total === n ? `${fmtCount(n)} rows` : `${fmtCount(total)} of ${fmtCount(n)}`);
    if (found) parts.push(`${fmtCount(found.length)}${found.length >= SEARCH_LIMIT ? '+' : ''} matches`);
    if (note) parts.push(note);
    count.textContent = parts.join(' · ');
    ctx.setStatus(note ? note : '', note || '');
    empty.hidden = total > 0;
    if (!total) {
      empty.textContent = found ? `No source matches “${query}”${vis ? ' among the filtered rows' : ''}.`
        : selOnly ? 'Nothing is selected.' : vis ? 'No rows pass the current filters.' : 'This catalog is empty.';
    }
  }

  // ------------------------------------------------------------------ painting

  function layoutSpacer() {
    const {virt} = scrollGeometry();
    spacer.style.height = `${virt}px`;
  }

  function ensurePool(k) {
    while (pool.length < k) {
      const r = el('div', 'pk-list-row');
      r.setAttribute('role', 'option');
      const idc = el('span', 'pk-list-td pk-list-id');
      r.append(idc);
      r._cells = [];
      r._i = -1;
      rowsEl.append(r);
      pool.push(r);
    }
    for (let k2 = 0; k2 < pool.length; k2++) pool[k2].hidden = k2 >= k;
  }

  function syncCells(r) {
    while (r._cells.length < cols.length) {
      const c = el('span', 'pk-list-td pk-num');
      r.append(c);
      r._cells.push(c);
    }
    while (r._cells.length > cols.length) r._cells.pop().remove();
  }

  function paint(force = false) {
    if (!client || !visible || destroyed) return;
    if (rafPaint) return;
    rafPaint = requestAnimationFrame(() => {
      rafPaint = 0;
      paintNow(force);
    });
  }

  function paintNow() {
    if (!client) return;
    viewH = view.clientHeight || viewH;
    const {ratio} = scrollGeometry();
    // virtual:false (section 6.10) renders every row in place for lists up to 5000 rows.
    const full = !virtualOn && total <= 5000;
    let k;
    if (full) {
      first = 0;
      k = total;
      rowsEl.style.transform = 'translateY(0px)';
    } else {
      const pos = view.scrollTop * ratio;
      first = Math.max(0, Math.min(Math.max(0, total - 1), Math.floor(pos / rowH)));
      const frac = pos - first * rowH;
      k = Math.min(total - first, Math.ceil(viewH / rowH) + 2);
      rowsEl.style.transform = `translateY(${Math.round(view.scrollTop - frac)}px)`;
    }
    const nRows = Math.max(0, k);
    ensurePool(nRows);
    const f = store.get('focus');
    const fi = f && f.ds === ds && Number.isInteger(f.i) ? f.i : -1;
    const selMask = sel ? sel.mask : null;
    for (let k2 = 0; k2 < nRows; k2++) {
      const r = pool[k2];
      const p = first + k2;
      const i = rowAt(p);
      syncCells(r);
      r.dataset.i = String(i);
      r.id = `${ctx.id}-row-${i}`;
      const isSel = !!(selMask && selMask[i]);
      r.classList.toggle('pk-sel', isSel);
      r.setAttribute('aria-selected', String(isSel));
      r.classList.toggle('pk-focus', i === fi);
      r.classList.toggle('pk-hover', i === hoverI);
      r.classList.toggle('pk-odd', p % 2 === 1);
      const cv = colorVals ? colorVals[i] : NaN;
      r.style.setProperty('--pk-swatch', cv === cv && colorPal && colorPal[cv | 0] ? colorPal[cv | 0] : 'transparent');
      if (r._i !== i || r._cols !== cols) {
        r._i = i;
        r._cols = cols;
        const idc = r.firstChild;
        idc.textContent = '';
        idc.title = '';
        client.id(i).then((id) => {
          if (r._i === i) {
            idc.textContent = String(id);
            idc.title = String(id);
          }
        }, (err) => {
          if (!(err instanceof Error)) throw err;
          if (r._i === i) idc.title = `id unavailable: ${err.message}`;
        });
        cols.forEach((c, j) => {
          const vals = colVals.get(c.key);
          const cell = r._cells[j];
          if (!vals) {
            cell.textContent = '';
            cell.title = '';
            return;
          }
          const {text, title} = cellText(c, vals[i]);
          cell.textContent = text;
          cell.title = title;
          cell.classList.toggle('pk-missing', text === MISSING);
        });
      }
    }
    if (fi >= 0 && view.contains(document.activeElement)) view.setAttribute('aria-activedescendant', `${ctx.id}-row-${fi}`);
  }

  view.addEventListener('scroll', () => paint(), {passive: true});

  // ------------------------------------------------------------------ interaction

  function rowFromEvent(ev) {
    const r = ev.target.closest ? ev.target.closest('.pk-list-row') : null;
    if (!r || !rowsEl.contains(r)) return -1;
    const i = Number(r.dataset.i);
    return Number.isInteger(i) ? i : -1;
  }

  function setFocus(i, scroll = true) {
    if (i < 0) return;
    const cached = client.id(i);
    // Write focus once with both halves when the id is at hand (LRU hit), else with i only;
    // boot fills in the id.
    let done = false;
    cached.then((id) => {
      if (done) return;
      done = true;
      store.set({focus: {ds, i, id}}, {source: ctx.id, history: true});
    }, (err) => {
      if (!(err instanceof Error)) throw err;
      if (done) return;
      done = true;
      store.set({focus: {ds, i, id: null}}, {source: ctx.id, history: true});
    });
    if (scroll) scrollToPos(positionOf(i));
  }

  view.addEventListener('click', (ev) => {
    const i = rowFromEvent(ev);
    if (i < 0 || !client) return;
    if (sel && (ev.shiftKey || ev.altKey || ev.ctrlKey || ev.metaKey)) {
      const op = ev.ctrlKey || ev.metaKey ? (sel.mask[i] ? 'subtract' : 'add') : ev.altKey ? 'subtract' : 'add';
      sel.setIndices([i], op);
    }
    setFocus(i, false);
  });

  view.addEventListener('pointermove', (ev) => {
    if (ev.pointerType === 'touch') return;
    const i = rowFromEvent(ev);
    if (i === hoverI) return;
    hoverI = i;
    store.set({hover: i >= 0 ? {ds, i} : null}, {source: ctx.id});
    paint();
  });
  view.addEventListener('pointerleave', () => {
    if (hoverI < 0) return;
    hoverI = -1;
    const hv = store.get('hover');
    if (hv && hv.ds === ds) store.set({hover: null}, {source: ctx.id});
    paint();
  });

  function stepFocus(dir) {
    if (!client || !total) return false;
    const f = store.get('focus');
    const cur = f && f.ds === ds && Number.isInteger(f.i) ? positionOf(f.i) : -1;
    let p;
    if (cur < 0) p = dir > 0 ? Math.max(0, first) : Math.min(total - 1, first + Math.max(0, Math.floor(viewH / rowH) - 1));
    else p = Math.max(0, Math.min(total - 1, cur + dir));
    if (p === cur) return true;
    setFocus(rowAt(p));
    return true;
  }

  view.addEventListener('keydown', (ev) => {
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
    const page = Math.max(1, Math.floor(viewH / rowH) - 1);
    let handled = true;
    switch (ev.key) {
      case 'ArrowDown': stepFocus(1); break;
      case 'ArrowUp': stepFocus(-1); break;
      case 'PageDown': stepFocus(page); break;
      case 'PageUp': stepFocus(-page); break;
      case 'Home': if (total) setFocus(rowAt(0)); break;
      case 'End': if (total) setFocus(rowAt(total - 1)); break;
      case 'Enter':
      case ' ': {
        const f = store.get('focus');
        if (sel && f && f.ds === ds && Number.isInteger(f.i)) sel.setIndices([f.i], sel.mask[f.i] ? 'subtract' : 'add');
        break;
      }
      default: handled = false;
    }
    if (handled) {
      ev.preventDefault();
      ev.stopPropagation();
    }
  });

  offs.push(store.onEvent('nav:step', (p) => {
    if (!p || p.handled || !client || p.ds !== ds) return;
    if (stepFocus(p.dir > 0 ? 1 : -1)) p.handled = true;
  }));

  // search
  let searchTimer = 0;
  let searchSeq = 0;
  function runSearch() {
    const text = search.value.trim();
    query = text;
    filterBtn.disabled = !text;
    if (!text) {
      found = null;
      recompute();
      return;
    }
    const my = ++searchSeq;
    count.textContent = 'searching…';
    client.find(text, {limit: SEARCH_LIMIT}).then((hits) => {
      if (my !== searchSeq || destroyed) return;
      found = Int32Array.from(hits || []);
      recompute();
    }, (err) => {
      if (!(err instanceof Error)) throw err;
      if (my !== searchSeq) return;
      found = new Int32Array(0);
      store.emit('toast', {level: 'warn', text: `Search failed: ${err.message}`});
      recompute();
    });
  }
  search.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(runSearch, 180);
  });
  search.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') {
      ev.preventDefault();
      clearTimeout(searchTimer);
      runSearch();
      const go = () => { if (total) setFocus(rowAt(0)); };
      setTimeout(go, 250);
    } else if (ev.key === 'Escape' && search.value) {
      ev.preventDefault();
      ev.stopPropagation();
      search.value = '';
      runSearch();
    } else if (ev.key === 'ArrowDown') {
      ev.preventDefault();
      view.focus();
      stepFocus(1);
    }
  });
  filterBtn.disabled = true;
  filterBtn.addEventListener('click', () => {
    const f = store.get(`filters:${ds}`) || {};
    const on = filterBtn.getAttribute('aria-pressed') === 'true';
    const next = Object.assign({}, f);
    if (on) delete next.text;
    else if (query) next.text = query;
    store.set({[`filters:${ds}`]: next}, {source: ctx.id, history: true});
  });
  function syncFilterBtn() {
    const f = store.get(`filters:${ds}`) || {};
    const on = typeof f.text === 'string' && f.text.trim() !== '';
    filterBtn.setAttribute('aria-pressed', String(on));
    filterBtn.classList.toggle('pk-on', on);
    filterBtn.title = on ? `Filtering every panel by “${f.text}” (click to remove)` : 'Apply the search text as a filter to every panel';
    if (on && !search.value) {
      search.value = f.text;
      query = f.text.trim();
      filterBtn.disabled = false;
    }
    if (on) filterBtn.disabled = false;
  }

  selOnlyBtn.addEventListener('click', () => {
    selOnly = !selOnly;
    selOnlyBtn.setAttribute('aria-pressed', String(selOnly));
    selOnlyBtn.classList.toggle('pk-on', selOnly);
    recompute();
  });
  selOnlyBtn.classList.toggle('pk-on', selOnly);

  // ------------------------------------------------------------------ store wiring

  async function refreshVis() {
    if (!client) return;
    const v = store.get(`vis:${ds}`);
    if (!v || v.count === null || v.count === undefined) {
      vis = null;
    } else {
      const r = await client.vis();
      vis = r && r.vis ? r.vis : null;
    }
    recompute();
  }

  offs.push(store.on([`vis:${ds}`], () => { refreshVis().catch(report); }));
  offs.push(store.on([`sel:${ds}`], () => {
    if (selOnly) recompute();
    else paint();
  }));
  offs.push(store.on([`filters:${ds}`], () => syncFilterBtn()));
  offs.push(store.on(['color'], () => { refreshColor().catch(report); }));
  offs.push(store.onEvent('theme:change', () => { refreshColor().catch(report); }));

  async function refreshColor() {
    if (!client) return;
    const c = store.get('color');
    const m = c && c.by ? client.col(c.by) : null;
    if (!isCategory(m)) {
      colorVals = null;
      colorPal = null;
    } else {
      colorVals = await client.values(c.by);
      colorPal = levelColors(m, ctx.theme ? ctx.theme() : null);
    }
    root.classList.toggle('pk-list-colored', !!colorVals);
    paint(true);
  }
  offs.push(store.on(['focus'], (changed, st, sources) => {
    const f = st.get('focus');
    if (f && f.ds === ds && Number.isInteger(f.i) && !(sources.size === 1 && sources.has(ctx.id))) {
      scrollToPos(positionOf(f.i), 'center');
    }
    paint();
  }));
  offs.push(store.on(['hover'], (changed, st, sources) => {
    if (sources.size === 1 && sources.has(ctx.id)) return;
    const hv = st.get('hover');
    hoverI = hv && hv.ds === ds && Number.isInteger(hv.i) ? hv.i : -1;
    paint();
  }));

  function report(err) {
    if (!(err instanceof Error)) throw err;
    if (err.name === 'AbortError') return;
    console.warn(`list ${ctx.id}`, err);
    ctx.setStatus('error', err.message);
  }

  // ------------------------------------------------------------------ load

  const ref = app.page && app.page.datasets ? app.page.datasets[ds] : null;
  if (!ref || ref.kind !== 'catalog') {
    skel.remove();
    const note = el('div', 'pk-info-note', `The source list needs a catalog dataset (got ${ref ? ref.kind : 'none'}).`);
    root.append(note);
  } else {
    ctx.setStatus('loading…');
    (async () => {
      client = await app.catalog(ds);
      sel = await app.selectionFor(ds);
      if (destroyed) return;
      const metaCols = client.manifest.cols || [];
      if (Array.isArray(cfg.columns) && cfg.columns.length) {
        const missingKeys = [];
        cols = [];
        for (const k of cfg.columns) {
          const m = client.col(k);
          if (!m || m.role === 'id') {
            if (!m) missingKeys.push(k);
            continue;
          }
          cols.push({...m, key: k});
        }
        if (missingKeys.length) console.warn(`list ${ctx.id}: unknown columns ${missingKeys.join(', ')}`);
      } else {
        cols = pickHoverColumns(metaCols, 3);
      }
      // str columns have no numeric values; keep them out of the numeric cells.
      cols = cols.filter((c) => c.enc !== 'str');
      skel.remove();
      renderHead();
      await Promise.all(cols.map(async (c) => {
        colVals.set(c.key, await client.values(c.key));
      }));
      if (destroyed) return;
      ctx.setStatus('');
      offTools = ctx.tools ? mountSelectionTools(app, ds, ctx.tools, {cols: () => cols.map((c) => c.key)}) : null;
      syncFilterBtn();
      total = client.n;
      await refreshVis();
      await refreshColor();
      if (query) runSearch();
      const f = store.get('focus');
      if (f && f.ds === ds && Number.isInteger(f.i)) scrollToPos(positionOf(f.i), 'center');
    })().catch((err) => {
      skel.remove();
      report(err);
      const note = el('div', 'pk-error-note', `The source list could not be loaded: ${err.message}`);
      note.setAttribute('role', 'alert');
      root.append(note);
    });
  }

  return {
    resize(w, h) {
      viewH = view.clientHeight || h;
      layoutSpacer();
      paint(true);
    },
    onVisible(v) {
      visible = v;
      if (v) paint(true);
    },
    destroy() {
      destroyed = true;
      clearTimeout(searchTimer);
      if (rafPaint) cancelAnimationFrame(rafPaint);
      for (const off of offs) off();
      if (offTools) offTools();
      const hv = store.get('hover');
      if (hv && hv.ds === ds && hoverI >= 0) store.set({hover: null}, {source: ctx.id});
    },
    stats() {
      return {rows: total, rendered: pool.filter((r) => !r.hidden).length};
    },
    order() {
      return order ? Array.from(order) : null;
    },
  };
}
