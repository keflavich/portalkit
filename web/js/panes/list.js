// Source list pane (capabilities source_list_search, keyboard_nav; ARCHITECTURE.md 6.10).
//
// A virtualized list for any N: only the visible rows exist in the DOM, and very tall lists
// map the scroll position onto rows proportionally so the browser's element height limit is
// never reached. Rows follow the filtered (vis:<ds>) order, optionally sorted by a column or
// the id (missing values last), restricted to the selection ("selection only") or to search
// results from DataClient.find (id, alias, case-insensitive, prefix, substring, coordinates).
//
// Sorting (IMPLEMENTATION_NOTES 74) runs in the data worker: DataClient.order(key, dir) returns
// an argsort of every row (key '__id' sorts the catalog ids), and the list keeps the rows of that
// permutation that pass the filters, the selection-only switch and the search. The main thread
// never sorts. Category colours come from ui/legend.js plotLevelColors (note 76), so rows match
// the plotted points for every level.
//
// Store: writes focus {ds, i, id} and hover {ds, i}; reads sel:<ds>, vis:<ds>, filters:<ds>
// and focus. Handles the `nav:step` offer, so arrow keys walk the filtered, sorted order.
// Click focuses; Shift-click adds to the selection, Alt-click removes, Ctrl/Cmd-click toggles.

import {fmtCount, fmtQuant, fmtValue, MISSING} from '../core/fmt.js';
import {pickHoverColumns} from '../ui/hover.js';
import {mountSelectionTools} from '../ui/export.js';
import {isCategory, plotLevelColors} from '../ui/legend.js';

export const meta = {
  type: 'list',
  caps: ['source_list_search', 'keyboard_nav', 'hover_preview', 'class_coloring', 'filter_toggles', 'selection_export'],
  needs: ['catalog'],
};

const MAX_SCROLL_PX = 4000000;   // below every browser's element height limit
const SEARCH_LIMIT = 5000;
const DEFAULT_ROW_H = 24;
const ORDER_CACHE = 2;           // worker argsorts kept on the main thread (key + dir)
/** Header key of the id column (a catalog column may itself be called "id"). */
export const ID_SORT = '__id';

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

/**
 * Sort spec with the id sort spelled ID_SORT: a key `id` (or `__id`) means the catalog id unless
 * the catalog has a column of that name (`hasCol(key)` true).
 */
export function normalizeSortSpec(spec, hasCol = () => false) {
  if (!spec || !spec.key) return null;
  const dir = spec.dir < 0 ? -1 : 1;
  if (spec.key === ID_SORT || (spec.key === 'id' && !hasCol('id'))) return {key: ID_SORT, dir};
  return {key: String(spec.key), dir};
}

/**
 * Key passed to DataClient.order for a sort spec key: ID_SORT ('__id') for the catalog id, so a
 * stored column called `id` keeps its own key (the worker's id-only alias is '__id').
 */
export function orderKey(key) {
  return key === ID_SORT ? ID_SORT : key;
}

/** DataClient.order result -> Int32Array ({order}/{idx} wrappers and plain arrays accepted). */
export function asOrder(result) {
  if (result instanceof Int32Array) return result;
  const wrapped = result && typeof result === 'object' && !Array.isArray(result) && !ArrayBuffer.isView(result);
  const src = wrapped ? (result.order || result.idx) : result;
  if (!src || typeof src.length !== 'number') throw new TypeError('DataClient.order returned no row permutation');
  return Int32Array.from(src);
}

/**
 * Rows of the permutation `full` (row indices in sorted order) that pass `keep`, in that order.
 * keep: Uint8Array mask over rows (truthy = keep), or null to keep every row (returns `full`).
 * Entries outside the mask's range are dropped.
 */
export function subsetInOrder(full, keep) {
  if (!keep) return full;
  const n = full.length;
  const len = keep.length;
  let m = 0;
  for (let p = 0; p < n; p++) {
    const r = full[p];
    if (r >= 0 && r < len && keep[r]) m++;
  }
  const out = new Int32Array(m);
  let k = 0;
  for (let p = 0; p < n; p++) {
    const r = full[p];
    if (r >= 0 && r < len && keep[r]) out[k++] = r;
  }
  return out;
}

/**
 * Membership mask of the rows the list shows over n rows, or null for every row: the search
 * hits `found` (row indices) and/or the rows passing both `vis` and `selMask` (Uint8Arrays,
 * null = no restriction).
 */
export function keepMask(n, {found = null, vis = null, selMask = null} = {}) {
  if (!found && !vis && !selMask) return null;
  const keep = new Uint8Array(n);
  const ok = (i) => (!vis || vis[i]) && (!selMask || selMask[i]);
  if (found) {
    for (const i of found) if (i >= 0 && i < n && ok(i)) keep[i] = 1;
  } else {
    for (let i = 0; i < n; i++) if (ok(i)) keep[i] = 1;
  }
  return keep;
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
  const orderCache = new Map();  // `${key}\u0000${dir}` -> Promise<Int32Array> (worker argsort)
  let identityRows = null;       // Int32Array 0..n-1, reused for unsorted subsets
  let visible = true;
  let viewH = 0;
  let pool = [];
  let first = 0;
  let destroyed = false;
  let hoverI = -1;
  let rafPaint = 0;
  const offs = [];
  let offTools = null;
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
    mk(ID_SORT, 'id', client && typeof client.order !== 'function' ? 'Sorting is unavailable for this catalog' : 'Sort by id');
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

  /** Worker argsort of every row for (key, dir), cached here for the last ORDER_CACHE specs. */
  function fullOrder(key, dir) {
    const ck = `${key}\u0000${dir}`;
    const hit = orderCache.get(ck);
    if (hit) {
      orderCache.delete(ck);
      orderCache.set(ck, hit);
      return hit;
    }
    const p = Promise.resolve(client.order(orderKey(key), dir)).then(asOrder);
    p.then(null, () => { if (orderCache.get(ck) === p) orderCache.delete(ck); });
    orderCache.set(ck, p);
    while (orderCache.size > ORDER_CACHE) orderCache.delete(orderCache.keys().next().value);
    return p;
  }

  function identity(n) {
    if (!identityRows || identityRows.length !== n) {
      identityRows = new Int32Array(n);
      for (let i = 0; i < n; i++) identityRows[i] = i;
    }
    return identityRows;
  }

  async function recompute() {
    if (!client) return;
    const my = ++orderToken;
    const n = client.n;
    const selMask = selOnly && sel ? sel.mask : null;
    let note = '';
    let idx = null;
    if (sortSpec) {
      if (typeof client.order !== 'function') note = 'sorting is unavailable for this catalog';
      else if (sortSpec.key !== ID_SORT && !client.col(sortSpec.key)) note = `unknown sort column ${sortSpec.key}`;
      else {
        ctx.setStatus('sorting…');
        let full = null;
        try {
          full = await fullOrder(sortSpec.key, sortSpec.dir);
        } catch (err) {
          if (!(err instanceof Error)) throw err;
          if (err.name === 'AbortError') return;
          console.warn(`list ${ctx.id}: sort by ${sortSpec.key}`, err);
          note = `sort failed: ${err.message}`;
        }
        if (my !== orderToken || destroyed) return;
        if (full) idx = subsetInOrder(full, keepMask(n, {found, vis, selMask}));
      }
    }
    if (!idx && found) {
      // Unsorted search results keep their rank order.
      const keep = [];
      for (const i of found) if (i >= 0 && i < n && (!vis || vis[i]) && (!selMask || selMask[i])) keep.push(i);
      idx = Int32Array.from(keep);
    } else if (!idx && (vis || selMask)) {
      idx = subsetInOrder(identity(n), keepMask(n, {vis, selMask}));
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
      colorPal = plotLevelColors(m, ctx.theme ? ctx.theme() : null);
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
      sortSpec = normalizeSortSpec(sortSpec, (k) => !!client.col(k));
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
      orderCache.clear();
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
