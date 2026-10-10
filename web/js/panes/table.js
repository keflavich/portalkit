// Table pane (ARCHITECTURE.md section 6.10): a virtualized table of a catalog's rows
// (source "rows", default: DataClient.rows, exact row chunks when published, otherwise the
// quantized fallback) or of a JSON document (source "json:<url>": an array of objects or
// {columns, rows}). Only the visible rows are in the DOM; row blocks are fetched on demand.
// JSON tables sort by clicking a header (NaN and missing last). Clicking a catalog row focuses
// the source; hover writes `hover`.

import {fmtCount, fmtValue, MISSING} from '../core/fmt.js';
import {fetchJSON, FetchError} from '../data/fetch.js';
import {mountSelectionTools} from '../ui/export.js';

export const meta = {type: 'table', caps: ['selection_export'], needs: []};

const BLOCK = 256;
const MAX_BLOCKS = 64;
const ROW_H = 24;
const MAX_SCROLL_PX = 4000000;

/**
 * Normalise a JSON table document to {columns:[string], rows:[object]}.
 * Accepts an array of objects, {columns, rows} with rows as arrays or objects, or {rows}.
 * Throws TypeError for anything else.
 */
export function normalizeJSONTable(doc, columns = null) {
  let rows;
  let cols = Array.isArray(columns) && columns.length ? columns.map(String) : null;
  if (Array.isArray(doc)) rows = doc;
  else if (doc && typeof doc === 'object' && Array.isArray(doc.rows)) {
    rows = doc.rows;
    if (!cols && Array.isArray(doc.columns)) cols = doc.columns.map((c) => (c && typeof c === 'object' ? String(c.key ?? c.name) : String(c)));
  } else {
    throw new TypeError('table JSON must be an array of objects or {columns, rows}');
  }
  const docCols = doc && Array.isArray(doc.columns)
    ? doc.columns.map((c) => (c && typeof c === 'object' ? String(c.key ?? c.name) : String(c))) : null;
  const objs = rows.map((r) => {
    if (Array.isArray(r)) {
      const names = docCols || r.map((_, j) => `col${j}`);
      const o = {};
      names.forEach((n, j) => { o[n] = r[j]; });
      return o;
    }
    return r && typeof r === 'object' ? r : {value: r};
  });
  if (!cols) {
    const seen = new Set();
    cols = [];
    for (const o of objs.slice(0, 1000)) {
      for (const k of Object.keys(o)) {
        if (!seen.has(k) && !k.startsWith('_')) {
          seen.add(k);
          cols.push(k);
        }
      }
    }
  }
  return {columns: cols, rows: objs};
}

/** Stable sort of row objects by `key` (numbers numerically, strings with numeric collation). */
export function sortRows(rows, key, dir = 1) {
  const coll = new Intl.Collator('en', {numeric: true, sensitivity: 'base'});
  const d = dir < 0 ? -1 : 1;
  const missing = (v) => v === undefined || v === null || v === '' || (typeof v === 'number' && Number.isNaN(v));
  return rows.map((r, k) => [r, k]).sort(([a, ka], [b, kb]) => {
    const va = a[key], vb = b[key];
    const ma = missing(va), mb = missing(vb);
    if (ma || mb) return ma === mb ? ka - kb : ma ? 1 : -1;
    const c = typeof va === 'number' && typeof vb === 'number' ? va - vb : coll.compare(String(va), String(vb));
    return c ? c * d : ka - kb;
  }).map(([r]) => r);
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
  const source = typeof cfg.source === 'string' && cfg.source ? cfg.source : 'rows';
  const jsonUrl = source.startsWith('json:') ? source.slice(5) : null;

  root.classList.add('pk-list', 'pk-table');
  root.style.setProperty('--pk-row-h', `${ROW_H}px`);
  const bar = el('div', 'pk-list-bar');
  const count = el('span', 'pk-list-count pk-muted');
  bar.append(count);
  const view = el('div', 'pk-list-view pk-table-view');
  view.tabIndex = 0;
  view.setAttribute('role', 'table');
  const head = el('div', 'pk-list-head pk-table-head');
  head.setAttribute('role', 'row');
  const spacer = el('div', 'pk-list-spacer');
  const rowsEl = el('div', 'pk-list-rows');
  view.append(spacer, rowsEl);
  const skel = el('div', 'pk-skeleton pk-list-skeleton');
  root.append(bar, head, view, skel);

  let columns = [];
  let metas = new Map();
  let total = 0;
  let client = null;
  let jsonRows = null;           // array of objects (json source)
  let sortSpec = null;
  const blocks = new Map();      // block index -> rows | Promise
  let visible = true;
  let destroyed = false;
  let raf = 0;
  let hoverI = -1;
  const pool = [];
  const offs = [];
  let offTools = null;

  function report(err) {
    if (!(err instanceof Error)) throw err;
    if (err.name === 'AbortError') return;
    console.warn(`table ${ctx.id}`, err);
    ctx.setStatus('error', err.message);
  }

  function fail(msg) {
    skel.remove();
    const n = el('div', 'pk-error-note', msg);
    n.setAttribute('role', 'alert');
    root.append(n);
  }

  function renderHead() {
    head.textContent = '';
    root.style.setProperty('--pk-list-cols', String(Math.max(0, columns.length - 1)));
    for (const c of columns) {
      const m = metas.get(c) || {};
      const b = el('button', 'pk-list-th', m.label || c);
      b.type = 'button';
      b.dataset.key = c;
      b.setAttribute('role', 'columnheader');
      const on = sortSpec && sortSpec.key === c;
      b.setAttribute('aria-sort', on ? (sortSpec.dir > 0 ? 'ascending' : 'descending') : 'none');
      if (on) b.append(el('span', 'pk-sort-ind', sortSpec.dir > 0 ? ' ▲' : ' ▼'));
      b.title = jsonRows ? `Sort by ${m.label || c}` : `${m.label || c}${m.unit ? ` [${m.unit}]` : ''}`;
      b.disabled = !jsonRows;
      head.append(b);
    }
  }

  head.addEventListener('click', (ev) => {
    const b = ev.target.closest('.pk-list-th');
    if (!b || !jsonRows) return;
    const key = b.dataset.key;
    if (!sortSpec || sortSpec.key !== key) sortSpec = {key, dir: 1};
    else if (sortSpec.dir > 0) sortSpec = {key, dir: -1};
    else sortSpec = null;
    jsonRows = sortSpec ? sortRows(jsonRowsOrig, sortSpec.key, sortSpec.dir) : jsonRowsOrig.slice();
    renderHead();
    paint(true);
  });
  let jsonRowsOrig = null;

  function geometry() {
    const full = total * ROW_H;
    const virt = Math.min(full, MAX_SCROLL_PX);
    const vh = view.clientHeight;
    const ratio = virt > vh && full > virt ? (full - vh) / (virt - vh) : 1;
    return {virt, ratio, vh};
  }

  function rowAt(i) {
    if (jsonRows) return jsonRows[i] || null;
    const b = Math.floor(i / BLOCK);
    const blk = blocks.get(b);
    if (Array.isArray(blk)) return blk[i - b * BLOCK] || null;
    if (!blk && client) {
      const p = client.rows(b * BLOCK, Math.min(total, (b + 1) * BLOCK)).then((rows) => {
        blocks.set(b, rows);
        while (blocks.size > MAX_BLOCKS) blocks.delete(blocks.keys().next().value);
        paint(true);
      }, (err) => {
        blocks.delete(b);
        report(err);
      });
      blocks.set(b, p);
    }
    return null;
  }

  function cellText(row, c) {
    if (!row) return {text: '', title: ''};
    const m = metas.get(c) || {};
    return fmtValue(row[c], m, {exact: !row._quantized});
  }

  function paint() {
    if (!visible || destroyed || raf) return;
    raf = requestAnimationFrame(() => {
      raf = 0;
      paintNow();
    });
  }

  function paintNow() {
    const {ratio, vh} = geometry();
    const pos = view.scrollTop * ratio;
    const first = Math.max(0, Math.floor(pos / ROW_H));
    const frac = pos - first * ROW_H;
    const k = Math.max(0, Math.min(total - first, Math.ceil(vh / ROW_H) + 2));
    while (pool.length < k) {
      const r = el('div', 'pk-list-row');
      r.setAttribute('role', 'row');
      rowsEl.append(r);
      pool.push(r);
    }
    pool.forEach((r, j) => { r.hidden = j >= k; });
    rowsEl.style.transform = `translateY(${Math.round(view.scrollTop - frac)}px)`;
    const f = store.get('focus');
    for (let j = 0; j < k; j++) {
      const r = pool[j];
      const i = first + j;
      const row = rowAt(i);
      while (r.childNodes.length < columns.length) r.append(el('span', 'pk-list-td'));
      while (r.childNodes.length > columns.length) r.lastChild.remove();
      r.dataset.i = String(i);
      r.classList.toggle('pk-odd', i % 2 === 1);
      r.classList.toggle('pk-loading', !row);
      const ci = row && Number.isInteger(row._i) ? row._i : i;
      r.classList.toggle('pk-focus', !jsonRows && !!f && f.ds === ds && f.i === ci);
      r.classList.toggle('pk-hover', !jsonRows && ci === hoverI);
      columns.forEach((c, n) => {
        const cell = r.childNodes[n];
        const {text, title} = cellText(row, c);
        cell.textContent = text;
        cell.title = title || (typeof row?.[c] === 'string' ? row[c] : '');
        cell.classList.toggle('pk-num', !!row && typeof row[c] === 'number');
        cell.classList.toggle('pk-missing', text === MISSING);
        cell.setAttribute('role', 'cell');
      });
    }
  }

  view.addEventListener('scroll', () => paint(), {passive: true});
  view.addEventListener('click', (ev) => {
    if (jsonRows || !client) return;
    const r = ev.target.closest('.pk-list-row');
    if (!r) return;
    const i = Number(r.dataset.i);
    if (Number.isInteger(i)) store.set({focus: {ds, i, id: null}}, {source: ctx.id, history: true});
  });
  view.addEventListener('pointermove', (ev) => {
    if (jsonRows || !client || ev.pointerType === 'touch') return;
    const r = ev.target.closest('.pk-list-row');
    const i = r ? Number(r.dataset.i) : -1;
    if (i === hoverI) return;
    hoverI = i;
    store.set({hover: i >= 0 ? {ds, i} : null}, {source: ctx.id});
    paint();
  });
  view.addEventListener('pointerleave', () => {
    if (hoverI < 0) return;
    hoverI = -1;
    store.set({hover: null}, {source: ctx.id});
    paint();
  });

  function layout() {
    spacer.style.height = `${geometry().virt}px`;
    count.textContent = `${fmtCount(total)} rows`;
    paint();
  }

  if (jsonUrl) {
    ctx.setStatus('loading…');
    const url = new URL(ctx.hub && ctx.hub.resolve ? ctx.hub.resolve(jsonUrl) : jsonUrl, document.baseURI).toString();
    fetchJSON(url).then((doc) => {
      if (destroyed) return;
      const t = normalizeJSONTable(doc, cfg.columns);
      columns = t.columns;
      jsonRowsOrig = t.rows;
      jsonRows = t.rows.slice();
      total = jsonRows.length;
      skel.remove();
      renderHead();
      layout();
      ctx.setStatus('');
    }, (err) => {
      if (!(err instanceof FetchError) && !(err instanceof TypeError)) throw err;
      fail(`The table could not be loaded: ${err.message}`);
    }).catch(report);
  } else {
    const ref = app.page && app.page.datasets ? app.page.datasets[ds] : null;
    if (!ref || ref.kind !== 'catalog') {
      fail(`The table needs a catalog dataset or source "json:<url>" (got ${ref ? ref.kind : 'none'}).`);
    } else {
      ctx.setStatus('loading…');
      (async () => {
        client = await app.catalog(ds);
        if (destroyed) return;
        const man = client.manifest;
        const rowCols = man.rows && Array.isArray(man.rows.cols) ? man.rows.cols : null;
        const all = ['id', ...(man.pos ? ['ra', 'dec'] : []), ...(man.cols || []).filter((c) => c.role !== 'id').map((c) => c.key)];
        columns = Array.isArray(cfg.columns) && cfg.columns.length ? cfg.columns.map(String) : rowCols || all;
        metas = new Map((man.cols || []).map((c) => [c.key, c]));
        total = client.n;
        if (ctx.tools) offTools = mountSelectionTools(app, ds, ctx.tools, {cols: () => columns.filter((c) => metas.has(c))});
        skel.remove();
        renderHead();
        layout();
        ctx.setStatus(man.rows ? '' : 'quantized', man.rows ? '' : 'Exact rows are not published; values are shown at the stored precision.');
      })().catch((err) => {
        report(err);
        fail(`The table could not be loaded: ${err.message}`);
      });
      offs.push(store.on(['focus'], () => paint()));
    }
  }

  return {
    resize() {
      layout();
    },
    onVisible(v) {
      visible = v;
      if (v) layout();
    },
    destroy() {
      destroyed = true;
      if (raf) cancelAnimationFrame(raf);
      for (const off of offs) off();
      if (offTools) offTools();
      blocks.clear();
    },
    stats() {
      return {rows: total, blocks: blocks.size};
    },
  };
}
