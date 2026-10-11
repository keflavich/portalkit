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
// Startup reads (IMPLEMENTATION_NOTES 99, 112): the first screen paints from client.rows(first,
// last) (row chunks, or the head chunks without them); id cells always come from a rows
// payload (the row-chunk id key manifest.rows.cols[0], or `id` on quantized rows), never from
// client.id(i) per row, while the list is in stored order. Payload category values are labels
// (possibly numbers); column values are level indices. In stored order the cells and the
// class-colour swatches read the row records the list holds; the shown columns (and the
// colour-by column) load whole only on sort, filter, search or a colour-by change, or at idle
// once the list has been quiet for LIST_TIMING.idleColumnsMs after app.afterFirstPaint(). A
// shown column or colour column the row records do not carry (rowsCarry false) loads right
// after first paint, as does the colour column when a row label matches no level
// (payloadLevel). One client.values(key) read per key is in flight at a time. Once loaded,
// cells read the column values; rows read earlier keep their payload values.
//
// The handle's `firstPaint` promise (IMPLEMENTATION_NOTES 112) resolves once the first screen
// is drawn from data: the rows of the visible window are cached (stored order), the shown
// columns are loaded (sorted / filtered order), or the list is empty.
//
// Store: writes focus {ds, i, id} and hover {ds, i}; reads sel:<ds>, vis:<ds>, filters:<ds>
// and focus. Handles the `nav:step` offer, so arrow keys walk the filtered, sorted order.
// Click focuses; Shift-click adds to the selection, Alt-click removes, Ctrl/Cmd-click toggles.

import {fmtCount, fmtQuant, fmtValue, MISSING} from '../core/fmt.js';
import {pickHoverColumns} from '../ui/hover.js';
import {mountSelectionTools} from '../ui/export.js';
import {isCategory, plotLevelColors} from '../ui/legend.js';
import {firstPaintLatch} from '../layout/layout.js';

export const meta = {
  type: 'list',
  caps: ['source_list_search', 'keyboard_nav', 'hover_preview', 'class_coloring', 'filter_toggles', 'selection_export'],
  needs: ['catalog'],
};

const MAX_SCROLL_PX = 4000000;   // below every browser's element height limit
const SEARCH_LIMIT = 5000;
const DEFAULT_ROW_H = 24;
const ORDER_CACHE = 2;           // worker argsorts kept on the main thread (key + dir)
/** Rows payloads (client.rows objects) kept on the main thread. */
export const ROW_CACHE_ROWS = 4096;
/** rows() reads are whole blocks of this many rows, so scrolling repeats the same requests. */
export const ROW_BLOCK = 128;
/** Header key of the id column (a catalog column may itself be called "id"). */
export const ID_SORT = '__id';
/**
 * Idle whole-column load (IMPLEMENTATION_NOTES 112): after first paint the list waits for
 * `idleColumnsMs` without scrolling, pointer or key activity and without rows() reads in
 * flight, then loads the shown columns (and the colour column) at the next idle callback
 * (timeout `idleTimeoutMs`). Mutable for tests; Infinity disables the idle load.
 */
export const LIST_TIMING = {idleColumnsMs: 4000, idleTimeoutMs: 2000};

/** Map of category label (as a string) -> level index for a column meta, or null. */
export function levelIndex(meta) {
  const lv = meta && Array.isArray(meta.levels) ? meta.levels : null;
  if (!lv) return null;
  const m = new Map();
  lv.forEach((v, k) => { if (!m.has(String(v))) m.set(String(v), k); });
  return m;
}

/**
 * Level index of a rows-payload category value `v` in `levels` (levelIndex output), or NaN.
 * Payload values and level labels are spelled differently for some dtypes (codec._as_label):
 * a bool payload `true` has the level 'True', and a float32 payload is the shortest float32
 * spelling (0.1) while its level is the float64 repr ('0.10000000149011612'). Both spellings are
 * tried; NaN when neither matches (the caller then loads the whole colour column).
 */
export function payloadLevel(levels, v) {
  if (!levels || v === null || v === undefined) return NaN;
  let k;
  if (typeof v === 'boolean') {
    k = levels.get(v ? 'True' : 'False');
    if (k === undefined) k = levels.get(String(v));
    if (k === undefined) k = levels.get(v ? '1' : '0');
  } else {
    k = levels.get(String(v));
    if (k === undefined && typeof v === 'number' && Number.isFinite(v)) k = levels.get(String(Math.fround(v)));
  }
  return k === undefined ? NaN : k;
}

/**
 * True when client.rows() records carry column `key`: the row-chunk columns
 * (manifest.rows.cols) list it, or, without row chunks, it is a stored catalog column (the
 * quantized fallback rows carry every stored column).
 */
export function rowsCarry(manifest, key) {
  if (!manifest || !key) return false;
  const r = manifest.rows;
  if (r && Array.isArray(r.cols)) return r.cols.includes(key);
  return Array.isArray(manifest.cols) && manifest.cols.some((c) => c && c.key === key);
}

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

/**
 * Promise of the page's first paint (app.afterFirstPaint(), IMPLEMENTATION_NOTES 100). Apps
 * without the signal resolve at once.
 */
export function afterFirstPaint(app) {
  if (app && typeof app.afterFirstPaint === 'function') return Promise.resolve(app.afterFirstPaint());
  return Promise.resolve();
}

/**
 * Id key of a catalog's row chunks: the first entry of manifest.rows.cols (the catalog id key,
 * ARCHITECTURE.md section 5.2), or null when the catalog has no row chunks.
 */
export function rowsIdKey(manifest) {
  const r = manifest && manifest.rows;
  const k = r && Array.isArray(r.cols) && r.cols.length ? r.cols[0] : null;
  return typeof k === 'string' && k ? k : null;
}

/**
 * Catalog id of a client.rows / client.record row as a string, or null. Read in this order:
 * `_id` (worker-supplied), then `id` for a quantized-fallback row (`_quantized`), then the row
 * chunk's id key `idKey` (rowsIdKey of the manifest). A row-chunk row never reads a bare `id`
 * property unless the id key is `id`, so a stored column called `id` is never taken for the id.
 */
export function rowId(row, idKey = null) {
  if (!row || typeof row !== 'object') return null;
  let v = row._id;
  if (v === undefined || v === null) {
    const k = row._quantized ? 'id' : idKey;
    v = k && Object.prototype.hasOwnProperty.call(row, k) ? row[k] : null;
  }
  return v === undefined || v === null ? null : String(v);
}

/**
 * Text of one list cell. `v` is a column value (Float32Array entry: level index for
 * categories, 0/1 flags, dequantized numbers) or, with `opts.payload`, a rows-payload value
 * (category label, which may be a number such as a class code; boolean or 0/1 flag; null for
 * missing; exact number). Numbers keep the column's step-aware precision either way, so a cell
 * reads the same from both sources.
 */
export function cellText(meta, v, opts = {}) {
  const m = meta || {};
  if (v === null || v === undefined) return {text: MISSING, title: 'missing'};
  if (typeof v === 'string') return {text: v, title: ''};
  if (typeof v === 'boolean') return {text: v ? 'yes' : 'no', title: ''};
  if (m.role === 'category' || Array.isArray(m.levels)) {
    if (!Number.isFinite(v)) return {text: MISSING, title: 'missing'};
    // a payload carries the label itself; a column value is a level index
    if (opts.payload) return {text: String(v), title: ''};
    const lv = m.levels ? m.levels[v | 0] : undefined;
    return {text: lv !== undefined ? String(lv) : String(v), title: ''};
  }
  if (m.role === 'flag') return {text: Number.isFinite(v) ? (v ? 'yes' : 'no') : MISSING, title: ''};
  if (m.step > 0 && /^u(8|16)q$/.test(m.enc || '')) return fmtQuant(v, m.step);
  return fmtValue(v, m, {exact: false});
}

/**
 * Small LRU of rows payloads keyed by stored row index. put(rows) stores client.rows output
 * (each row carries `_i`; rows without one are numbered from `i0`).
 */
export class RowCache {
  constructor(cap = ROW_CACHE_ROWS) {
    this.cap = Math.max(1, cap | 0);
    this.map = new Map();
  }

  get size() { return this.map.size; }

  /** Raise the cap to at least k rows (a rendered window must fit, or it refetches forever). */
  reserve(k) {
    if (Number.isFinite(k) && k > this.cap) this.cap = Math.ceil(k);
  }

  has(i) { return this.map.has(i); }

  get(i) { return this.map.get(i); }

  put(rows, i0 = 0) {
    if (!rows || typeof rows.length !== 'number') return 0;
    let k = 0;
    for (const r of rows) {
      const i = r && Number.isInteger(r._i) ? r._i : i0 + k;
      k++;
      if (!r || typeof r !== 'object') continue;
      this.map.delete(i);
      this.map.set(i, r);
    }
    while (this.map.size > this.cap) this.map.delete(this.map.keys().next().value);
    return k;
  }

  /** First [a, b) sub-range of [i0, i1) whose rows are not cached, or null. */
  missing(i0, i1) {
    let a = -1, b = -1;
    for (let i = i0; i < i1; i++) {
      if (this.map.has(i)) continue;
      if (a < 0) a = i;
      b = i + 1;
    }
    return a < 0 ? null : [a, b];
  }

  clear() { this.map.clear(); }
}

/** Block-aligned [a, b) ranges of ROW_BLOCK rows covering [i0, i1), clipped to [0, n). */
export function rowBlocks(i0, i1, n, block = ROW_BLOCK) {
  const out = [];
  const lo = Math.max(0, Math.floor(i0)), hi = Math.min(n, Math.ceil(i1));
  if (!(hi > lo) || !(block > 0)) return out;
  for (let a = Math.floor(lo / block) * block; a < hi; a += block) out.push([a, Math.min(n, a + block)]);
  return out;
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
  let colorKey = null;           // category colour-by column, or null
  let colorLevels = null;        // label -> level index for payload swatches
  let colorFallbackKey = null;   // colour column queued to load because a row label missed
  let colorMissed = false;       // the last paint had a row label with no level
  const valuesInflight = new Map(); // key -> Promise of client.values(key) in flight (shared)
  let idleTimer = 0;             // quiet-period timer of the idle column load
  let idleArmed = false;         // first paint released: the idle load may run
  const fp = firstPaintLatch();
  const rowCache = new RowCache();
  const rowsInflight = new Map(); // `${a}:${b}` -> Promise
  let rowsFailed = false;        // rows() failed: ids fall back to client.id(i)
  let idKey = null;              // id key of the row chunks (manifest.rows.cols[0])
  let dataVer = 0;               // bumps when cached rows or column values change
  let colsP = null;              // Promise of the full column load (loadColumns)
  let colsLoaded = false;
  let startupDone = false;       // first screen requested; later colour changes load at once
  const reads = {rows: 0, rowsRows: 0, values: 0, ids: 0};

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

  // ------------------------------------------------------------------ reads

  /**
   * client.values(key), one read per key at a time: the shown-column, uncarried-column and
   * colour-column loaders share the read in flight, so a column is transferred once.
   */
  function columnValues(key) {
    if (colVals.has(key)) return Promise.resolve(colVals.get(key));
    const hit = valuesInflight.get(key);
    if (hit) return hit;
    reads.values++;
    const p = Promise.resolve(client.values(key));
    valuesInflight.set(key, p);
    const clear = () => { if (valuesInflight.get(key) === p) valuesInflight.delete(key); };
    p.then(clear, clear);
    return p;
  }

  /** Whole shown columns (sort, filter, colour-by, or at idle). Idempotent. */
  function loadColumns() {
    if (colsP) return colsP;
    if (!client) return Promise.resolve();
    disarmIdle();
    const p = Promise.all(cols.map(async (c) => {
      if (colVals.has(c.key)) return;
      const v = await columnValues(c.key);
      if (!destroyed) colVals.set(c.key, v);
    })).then(() => {
      if (destroyed) return;
      colsLoaded = true;
      dataVer++;
      paint(true);
    });
    // A failed load is reported here (the list keeps its rows-payload cells) and retried on
    // the next sort, filter or colour-by.
    const done = p.then(null, (err) => {
      if (colsP === done) colsP = null;
      report(err);
      // the first screen keeps its ids; count it as painted
      if (order) fp.resolve();
    });
    colsP = done;
    return done;
  }

  /**
   * Shown columns the row records do not carry (manifest.rows.cols lacks them, e.g. `exact:
   * false` columns): their cells have no other source, so they load right after first paint
   * instead of waiting for the idle quiet period.
   */
  function loadUncarriedColumns() {
    if (!client) return Promise.resolve();
    const keys = cols.map((c) => c.key).filter((k) => !colVals.has(k) && !rowsCarry(client.manifest, k));
    if (!keys.length) return Promise.resolve();
    return Promise.all(keys.map(async (k) => {
      const v = await columnValues(k);
      if (!destroyed) colVals.set(k, v);
    })).then(() => {
      if (destroyed) return;
      dataVer++;
      paint(true);
    }, report);
  }

  /**
   * Whole colour-by column (level indices) for the swatches; reuses a loaded shown column and a
   * read in flight (columnValues). Every call with colorVals unset reads or reuses the column,
   * so colour-by A -> none -> A recovers its swatches.
   */
  function loadColorColumn() {
    const key = colorKey;
    if (!key || !client || colorVals) return Promise.resolve();
    if (colVals.has(key)) {
      colorVals = colVals.get(key);
      paint(true);
      return Promise.resolve();
    }
    return columnValues(key).then((vals) => {
      if (destroyed || colorKey !== key || colorVals) return;
      colorVals = vals;
      paint(true);
    }, report);
  }

  /** A row label missed the levels: load the whole colour column once, after first paint. */
  function colorFallback() {
    const key = colorKey;
    if (!key || colorVals || colorFallbackKey === key) return;
    colorFallbackKey = key;
    afterFirstPaint(app).then(() => {
      if (destroyed || colorKey !== key) return;
      return loadColorColumn();
    }).catch(report);
  }

  // Idle load: a quiet period after first paint, then an idle callback.
  function disarmIdle() {
    if (idleTimer) clearTimeout(idleTimer);
    idleTimer = 0;
  }

  function armIdle() {
    disarmIdle();
    if (!idleArmed || destroyed || (colsP && (!colorKey || colorVals))) return;
    const ms = LIST_TIMING.idleColumnsMs;
    if (!Number.isFinite(ms) || ms < 0) return;
    idleTimer = setTimeout(() => {
      idleTimer = 0;
      if (destroyed) return;
      if (rowsInflight.size) { armIdle(); return; }
      const run = () => {
        if (destroyed || !idleArmed) return;
        loadColumns();
        loadColorColumn();
      };
      if (typeof requestIdleCallback === 'function') requestIdleCallback(run, {timeout: LIST_TIMING.idleTimeoutMs});
      else setTimeout(run, 0);
    }, ms);
  }

  /** Activity restarts the quiet period of the idle column load. */
  function poke() {
    if (idleTimer) armIdle();
  }

  /** Fetch rows [a, b) once (client.rows) into the row cache, then repaint. */
  function requestRows(a, b) {
    const key = `${a}:${b}`;
    if (rowsInflight.has(key) || b <= a) return;
    reads.rows++;
    reads.rowsRows += b - a;
    const p = Promise.resolve(client.rows(a, b)).then((rows) => {
      if (destroyed) return;
      rowCache.put(rows, a);
      dataVer++;
      paint(true);
    }, (err) => {
      if (!(err instanceof Error)) throw err;
      if (err.name === 'AbortError') return;
      console.warn(`list ${ctx.id}: rows ${a}-${b}`, err);
      rowsFailed = true;
      dataVer++;
      paint(true);
    }).finally(() => rowsInflight.delete(key));
    rowsInflight.set(key, p);
  }

  /** Id of row i for a cell: the rows payload, or (sorted / filtered order) client.id(i). */
  function fillId(idc, r, i) {
    const row = rowCache.get(i);
    const id = rowId(row, idKey);
    if (id !== null) {
      idc.textContent = id;
      idc.title = id;
      return;
    }
    // In stored order the visible window comes from rows(): wait for its payload. Other orders
    // (whose sort, filter or search made the worker load the ids), payloads without an id and
    // failed rows() reads use id(i).
    if (!row && !order && !rowsFailed) return;
    if (r._idReq === i) return;
    r._idReq = i;
    reads.ids++;
    client.id(i).then((v) => {
      if (r._i === i) {
        idc.textContent = String(v);
        idc.title = String(v);
      }
    }, (err) => {
      if (!(err instanceof Error)) throw err;
      if (r._i === i) idc.title = `id unavailable: ${err.message}`;
    });
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
    // sorted / filtered / searched orders scatter the shown rows: read whole columns
    if (order) {
      loadColumns();
      loadColorColumn();
    }
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
    // Every row of the window must fit in the cache at once (virtual:false shows up to 5000).
    rowCache.reserve(nRows + 2 * ROW_BLOCK);
    ensurePool(nRows);
    // In stored order the window [first, first + nRows) is one rows() read (row chunks or the
    // head chunks); its payload gives the ids and, until the columns load, the cells.
    if (!order && nRows > 0 && !rowsFailed) {
      const miss = rowCache.missing(first, first + nRows);
      if (miss) for (const [a, b] of rowBlocks(miss[0], miss[1], client.n)) if (rowCache.missing(a, b)) requestRows(a, b);
    }
    const f = store.get('focus');
    const fi = f && f.ds === ds && Number.isInteger(f.i) ? f.i : -1;
    const selMask = sel ? sel.mask : null;
    colorMissed = false;
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
      const cv = swatchLevel(i);
      r.style.setProperty('--pk-swatch', cv === cv && colorPal && colorPal[cv | 0] ? colorPal[cv | 0] : 'transparent');
      if (r._i !== i || r._cols !== cols || r._ver !== dataVer) {
        const fresh = r._i !== i;
        r._i = i;
        r._cols = cols;
        r._ver = dataVer;
        const idc = r.firstChild;
        if (fresh) {
          idc.textContent = '';
          idc.title = '';
          r._idReq = -1;
        }
        fillId(idc, r, i);
        const row = rowCache.get(i);
        cols.forEach((c, j) => {
          const vals = colVals.get(c.key);
          const cell = r._cells[j];
          const fromRow = row && Object.prototype.hasOwnProperty.call(row, c.key);
          if (!fromRow && !vals) {
            cell.textContent = '';
            cell.title = '';
            cell.classList.toggle('pk-missing', false);
            return;
          }
          const {text, title} = fromRow ? cellText(c, row[c.key], {payload: true}) : cellText(c, vals[i]);
          cell.textContent = text;
          cell.title = title;
          cell.classList.toggle('pk-missing', text === MISSING);
        });
      }
    }
    if (fi >= 0 && view.contains(document.activeElement)) view.setAttribute('aria-activedescendant', `${ctx.id}-row-${fi}`);
    if (colorMissed) colorFallback();
    if (!fp.done && firstScreenDrawn(nRows)) fp.resolve();
  }

  /** Level index of row i for its swatch: the colour column, else the row record's label. */
  function swatchLevel(i) {
    if (colorVals) return colorVals[i];
    if (!colorLevels) return NaN;
    const row = rowCache.get(i);
    if (!row || !Object.prototype.hasOwnProperty.call(row, colorKey)) return NaN;
    const v = row[colorKey];
    if (v === null || v === undefined) return NaN;
    const k = payloadLevel(colorLevels, v);
    if (k !== k) colorMissed = true;
    return k;
  }

  /** True once the window of nRows rows shows data (IMPLEMENTATION_NOTES 112). */
  function firstScreenDrawn(nRows) {
    if (!startupDone) return false;
    if (total === 0 || nRows === 0) return true;
    if (!order) return rowsFailed || rowCache.missing(first, first + nRows) === null;
    return colsLoaded || !cols.length;
  }

  view.addEventListener('scroll', () => { poke(); paint(); }, {passive: true});
  view.addEventListener('keydown', poke);

  // ------------------------------------------------------------------ interaction

  function rowFromEvent(ev) {
    const r = ev.target.closest ? ev.target.closest('.pk-list-row') : null;
    if (!r || !rowsEl.contains(r)) return -1;
    const i = Number(r.dataset.i);
    return Number.isInteger(i) ? i : -1;
  }

  function setFocus(i, scroll = true) {
    if (i < 0) return;
    const known = rowId(rowCache.get(i), idKey);
    if (known !== null) {
      store.set({focus: {ds, i, id: known}}, {source: ctx.id, history: true});
      if (scroll) scrollToPos(positionOf(i));
      return;
    }
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
    poke();
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
  offs.push(store.on(['color'], () => { if (startupDone) refreshColor({load: true}).catch(report); }));
  offs.push(store.onEvent('theme:change', () => { if (startupDone) refreshColor().catch(report); }));

  /**
   * Category swatches for the `color` store key. The palette and the label -> level map come
   * from the column meta, so rows in stored order colour from their records; the whole colour
   * column loads for a colour-by change (`load`), a sorted / filtered order, or a column the
   * row records do not carry (after first paint).
   */
  async function refreshColor({load = false} = {}) {
    if (!client) return;
    const c = store.get('color');
    const m = c && c.by ? client.col(c.by) : null;
    if (!isCategory(m)) {
      colorKey = null;
      colorLevels = null;
      colorVals = null;
      colorPal = null;
      colorFallbackKey = null;
    } else {
      if (colorKey !== c.by) {
        colorVals = null;
        colorFallbackKey = null;
      }
      colorKey = c.by;
      colorLevels = levelIndex(m);
      colorPal = plotLevelColors(m, ctx.theme ? ctx.theme() : null);
      if (load) {
        // a colour-by change reads whole columns (the colour column and the shown ones)
        loadColumns();
        await loadColorColumn();
      } else if (order) {
        await loadColorColumn();
      } else if (!rowsCarry(client.manifest, c.by)) {
        await afterFirstPaint(app);
        if (destroyed || colorKey !== c.by) return;
        await loadColorColumn();
      }
      if (destroyed) return;
      const now = store.get('color');
      if (!now || now.by !== c.by) return;     // a newer colour change owns the swatches
    }
    root.classList.toggle('pk-list-colored', !!colorKey);
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
    fp.resolve();
  } else {
    ctx.setStatus('loading…');
    (async () => {
      client = await app.catalog(ds);
      sel = await app.selectionFor(ds);
      if (destroyed) return;
      sortSpec = normalizeSortSpec(sortSpec, (k) => !!client.col(k));
      idKey = rowsIdKey(client.manifest);
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
      if (destroyed) return;
      ctx.setStatus('');
      offTools = ctx.tools ? mountSelectionTools(app, ds, ctx.tools, {cols: () => cols.map((c) => c.key)}) : null;
      syncFilterBtn();
      total = client.n;
      // First screen: rows(first, last) through paint (no whole column, no ids).
      await refreshVis();
      if (destroyed) return;
      if (query) runSearch();
      const f = store.get('focus');
      if (f && f.ds === ds && Number.isInteger(f.i)) scrollToPos(positionOf(f.i), 'center');
      // swatches from the row records (a colour column they lack loads after first paint)
      refreshColor().catch(report);
      paint(true);
      startupDone = true;
      // Whole shown columns wait for the page's first paint and then a quiet period.
      await afterFirstPaint(app);
      if (destroyed) return;
      idleArmed = true;
      loadUncarriedColumns();
      armIdle();
    })().catch((err) => {
      skel.remove();
      report(err);
      const note = el('div', 'pk-error-note', `The source list could not be loaded: ${err.message}`);
      note.setAttribute('role', 'alert');
      root.append(note);
      fp.reject(err);
    });
  }

  return {
    firstPaint: fp.promise,
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
      fp.resolve();
      disarmIdle();
      orderCache.clear();
      clearTimeout(searchTimer);
      if (rafPaint) cancelAnimationFrame(rafPaint);
      for (const off of offs) off();
      if (offTools) offTools();
      const hv = store.get('hover');
      if (hv && hv.ds === ds && hoverI >= 0) store.set({hover: null}, {source: ctx.id});
    },
    stats() {
      return {rows: total, rendered: pool.filter((r) => !r.hidden).length, colsLoaded, firstPaint: fp.done,
        cachedRows: rowCache.size, reads: {...reads}};
    },
    order() {
      return order ? Array.from(order) : null;
    },
  };
}
