// Gallery pane (capabilities lightbox, hover_preview, filter_toggles, lazy_data_loading,
// static_sed_png; ARCHITECTURE.md sections 3, 5.6 and 6.10).
//
// A virtualized card grid over an image set (images.json, pk-images/1). Only the cards of the
// visible rows exist in the DOM and images load lazily. The toolbar has the role switcher
// (store `role`), facet presets, sort, text search and the annotate toggle; the facet sidebar
// shows each facet value with its count under the other facets (store `facets`). When the image
// set is joined to a catalog (`catalog` in images.json or the dataset ref), the grid also follows
// that catalog's filters (vis:<cat>), marks selected sources, and focus/hover use catalog rows,
// so every other pane on the catalog follows the gallery.
//
// Double-click or Enter opens the lightbox over the filtered, sorted set. Annotate mode
// (option `annotate`) stores per-viewer codes (default y/n/g/w/l/e) in localStorage through
// ui/annotate.js and exports them as CSV with the checklist key columns; nothing is written to
// the server (`annotate: true` uses the default codes and key). Collapsed companion panes (sky,
// scatter, detail) are opened from the mini-pane buttons through the layout API.
//
// Sorting (IMPLEMENTATION_NOTES 74): a sort key naming a column of the joined catalog (and
// `id` when the set is joined) is ordered by the worker through DataClient.order(key, dir);
// the items follow the rank of their catalog row, items without a row go last. Tag, caption
// and unjoined id sorts run locally over the items. Item image URLs resolve against the page
// URL (note 81); category colours come from ui/legend.js (note 76).

import {fmtCount, plural} from '../core/fmt.js';
import {MOBILE_BREAK_PX} from '../core/constants.js';
import {itemUrl, openLightbox, refreshSet, registerSet} from '../ui/lightbox.js';
import {
  annotationsFor, checklistFields, codeForKey, CODE_LABELS, downloadAnnotations,
} from '../ui/annotate.js';
import {mountSelectionTools} from '../ui/export.js';
import {isCategory, plotLevelColors} from '../ui/legend.js';

export const meta = {
  type: 'gallery',
  caps: ['lightbox', 'hover_preview', 'filter_toggles', 'lazy_data_loading', 'static_sed_png', 'class_coloring',
    'source_list_search', 'selection_export'],
  needs: ['images'],
};

const GAP = 8;
const CAPTION_H = 46;
const MIN_CARD = 96;
const MAX_CARD = 640;
const MINI_TYPES = ['sky', 'scatter', 'detail', 'sed', 'spectrum1d', 'list'];
const RANK_CACHE = 2;

// ------------------------------------------------------------------ pure helpers

/** Tag values of `item` for facet `key` as strings ([] when absent). */
export function tagValues(item, key) {
  const t = item && item.tags ? item.tags[key] : undefined;
  if (t === undefined || t === null || t === '') return [];
  return Array.isArray(t) ? t.filter((v) => v !== null && v !== undefined).map(String) : [String(t)];
}

/** True when `item` passes every facet in `facets` ({key: [values]}; empty lists pass). */
export function passesFacets(item, facets, skipKey = null) {
  for (const [k, vals] of Object.entries(facets || {})) {
    if (k === skipKey || !Array.isArray(vals) || !vals.length) continue;
    const have = tagValues(item, k);
    if (!have.some((v) => vals.includes(v))) return false;
  }
  return true;
}

/** Lower-cased search haystack of an item (id, caption, tag values). */
export function itemText(item) {
  if (!item) return '';
  const parts = [item.id, item.caption];
  for (const v of Object.values(item.tags || {})) parts.push(Array.isArray(v) ? v.join(' ') : v);
  return parts.filter((p) => p !== undefined && p !== null).join(' ').toLowerCase();
}

/**
 * Counts per facet value among the items passing the *other* facets (standard faceted search):
 * {key: Map(value -> count)}. `pass(k)` is an optional extra predicate on the item index.
 */
export function facetCounts(items, facetDefs, facets, pass = null) {
  const out = {};
  for (const f of facetDefs || []) {
    const m = new Map();
    for (const v of f.values || []) m.set(String(v), 0);
    out[f.key] = m;
  }
  items.forEach((it, k) => {
    if (!it || (pass && !pass(k))) return;
    for (const f of facetDefs || []) {
      if (!passesFacets(it, facets, f.key)) continue;
      const m = out[f.key];
      for (const v of tagValues(it, f.key)) m.set(v, (m.get(v) || 0) + 1);
    }
  });
  return out;
}

/** Parse a sort key: "key" | "-key" -> {key, dir}. */
export function parseSortKey(s) {
  const t = String(s || '').trim();
  if (!t) return null;
  return t.startsWith('-') ? {key: t.slice(1), dir: -1} : {key: t, dir: 1};
}

/** Sort comparator value for an item: id, caption, or a tag value. */
function sortValue(item, key) {
  if (key === 'id') return item.id;
  if (key === 'caption') return item.caption;
  const v = item.tags ? item.tags[key] : undefined;
  return Array.isArray(v) ? v[0] : v;
}

/**
 * Item indices in display order: items passing facets, text and `pass`, sorted by `sort`
 * ({key, dir} | null; numeric-aware collation, missing values last, stable).
 */
export function filterItems(items, {facets = {}, text = '', sort = null, pass = null} = {}) {
  const q = String(text || '').trim().toLowerCase();
  const terms = q ? q.split(/\s+/) : [];
  const out = [];
  for (let k = 0; k < items.length; k++) {
    const it = items[k];
    if (!it || !passesFacets(it, facets)) continue;
    if (pass && !pass(k)) continue;
    if (terms.length) {
      const hay = itemText(it);
      if (!terms.every((t) => hay.includes(t))) continue;
    }
    out.push(k);
  }
  if (sort && sort.key) {
    const coll = new Intl.Collator('en', {numeric: true, sensitivity: 'base'});
    const d = sort.dir < 0 ? -1 : 1;
    out.sort((a, b) => {
      const va = sortValue(items[a], sort.key), vb = sortValue(items[b], sort.key);
      const ma = va === undefined || va === null || va === '' || (typeof va === 'number' && !Number.isFinite(va));
      const mb = vb === undefined || vb === null || vb === '' || (typeof vb === 'number' && !Number.isFinite(vb));
      if (ma || mb) return ma === mb ? a - b : ma ? 1 : -1;
      const c = typeof va === 'number' && typeof vb === 'number' ? va - vb : coll.compare(String(va), String(vb));
      return c ? c * d : a - b;
    });
  }
  return out;
}

/**
 * Rank of every row in a DataClient.order permutation: rank[row] = position (Int32Array of
 * length n, -1 for rows the permutation does not list). Entries outside [0, n) are ignored.
 */
export function rankOf(order, n) {
  const N = Number.isInteger(n) && n > 0 ? n : 0;
  const rank = new Int32Array(N).fill(-1);
  const m = order ? order.length : 0;
  for (let p = 0; p < m; p++) {
    const r = order[p];
    if (r >= 0 && r < N && rank[r] < 0) rank[r] = p;
  }
  return rank;
}

/**
 * Item indices `idx` reordered by the rank of their catalog row (items[k].row): ranked items
 * first in rank order, then items without a row or rank in their current order (stable).
 */
export function orderByRank(idx, items, rank) {
  const ranked = [];
  const rest = [];
  for (const k of idx) {
    const it = items[k];
    const r = it && Number.isInteger(it.row) ? it.row : -1;
    const q = r >= 0 && rank && r < rank.length ? rank[r] : -1;
    if (q >= 0) ranked.push([q, k]);
    else rest.push(k);
  }
  ranked.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  return ranked.map((x) => x[1]).concat(rest);
}

/**
 * Key to pass to DataClient.order for a gallery sort key, or null for a local sort: tag keys
 * (facets or item tags) and `caption` sort locally; `id` and catalog columns use the joined
 * catalog when there is one (`col(key)` is the catalog's column lookup).
 */
export function catalogSortKey(key, {joined = false, col = null, tagKeys = null} = {}) {
  if (!joined || !key) return null;
  if (key === 'caption') return null;
  if (tagKeys && tagKeys.has(key)) return null;
  if (key === 'id' || key === '__id') return '__id';   // the item / catalog id ('__id' is id-only)
  return typeof col === 'function' && col(key) ? key : null;
}

/** Gallery `annotate` option -> {codes?, key?} | null (`true` means the defaults). */
export function annotateOptions(opt) {
  if (opt === true) return {};
  return opt && typeof opt === 'object' && !Array.isArray(opt) ? opt : null;
}

/** Grid geometry for a width: {cols, cardW, imgH, rowH}. */
export function gridGeometry(width, cardSize) {
  const size = Math.max(MIN_CARD, Math.min(MAX_CARD, Number.isFinite(+cardSize) && +cardSize > 0 ? +cardSize : 220));
  const w = Math.max(0, Number.isFinite(width) ? width : 0);
  const cols = Math.max(1, Math.floor((w + GAP) / (size + GAP)));
  const cardW = Math.max(MIN_CARD / 2, Math.floor((w - GAP * (cols - 1)) / cols));
  const imgH = Math.round(cardW * 0.75);
  return {cols, cardW, imgH, rowH: imgH + CAPTION_H + GAP};
}

/** Role key shown for an item: the requested role when the item has it, else its first role. */
export function roleFor(item, role) {
  if (!item || !item.roles) return null;
  if (role && item.roles[role] && item.roles[role].url) return role;
  for (const [k, r] of Object.entries(item.roles)) if (r && r.url) return k;
  return null;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

// ------------------------------------------------------------------ pane

export function mount(root, ctx) {
  const cfg = ctx.config || {};
  const store = ctx.store;
  const app = ctx.app;
  const hub = ctx.hub;
  const datasets = (app.page && app.page.datasets) || {};
  const imgDs = cfg.images || ctx.dataset;
  const ref = datasets[imgDs];
  const setId = ctx.id;
  const annotCfg = annotateOptions(cfg.annotate);
  const sortKeys = Array.isArray(cfg.sort) ? cfg.sort.map(parseSortKey).filter(Boolean) : [];

  root.classList.add('pk-gallery');
  root.style.setProperty('--pk-gal-gap', `${GAP}px`);
  const bar = el('div', 'pk-gal-bar');
  const main = el('div', 'pk-gal-main');
  const side = el('aside', 'pk-gal-facets');
  side.setAttribute('aria-label', 'Facets');
  const view = el('div', 'pk-gal-view');
  view.tabIndex = 0;
  view.setAttribute('role', 'grid');
  view.setAttribute('aria-label', 'Images');
  const spacer = el('div', 'pk-gal-spacer');
  const grid = el('div', 'pk-gal-grid');
  view.append(spacer, grid);
  const empty = el('div', 'pk-gal-empty pk-muted');
  empty.hidden = true;
  main.append(side, view, empty);
  const skel = el('div', 'pk-skeleton pk-gal-skeleton');
  root.append(bar, main, skel);

  let doc = null;
  let items = [];
  let catDs = null;
  let client = null;
  let sel = null;
  let vis = null;
  let rowToItems = null;          // Map(row -> [item index])
  let idToItems = null;           // Map(id -> [item index])
  let order = [];                 // item indices in display order
  let posOf = new Map();          // item index -> position in order
  let geo = gridGeometry(0, cfg.cardSize);
  let cardSize = Number.isFinite(+cfg.cardSize) && +cfg.cardSize > 0 ? +cfg.cardSize : 220;
  let text = '';
  let sort = sortKeys[0] || null;
  let visible = true;
  let destroyed = false;
  let focusPos = -1;              // position of the keyboard/focused card
  let hoverK = -1;
  let raf = 0;
  let annotOn = false;
  let ann = null;
  const pool = [];
  const offs = [];
  let unregisterSet = null;
  let setReady = false;           // items (and catalog filters, when joined) are loaded
  let catRows = null;             // cached exact/quantized rows for CSV export, by row index
  let colorVals = null;           // catalog level indices for color.by (category columns)
  let colorPal = null;
  let offTools = null;
  let tagKeys = new Set();        // facet and item tag keys (sorted locally)
  const ranks = new Map();        // `${key}\u0000${dir}` -> Int32Array | Promise (pending)
  let failedNow = null;           // sort key whose request just failed, during its one recompute

  // ------------------------------------------------------------------ helpers

  function role() {
    const r = store.get('role');
    return typeof r === 'string' && r ? r : (Array.isArray(cfg.roles) && cfg.roles[0]) || (doc && doc.roles && doc.roles[0] && doc.roles[0].key) || null;
  }

  function facets() {
    const f = store.get('facets');
    return f && typeof f === 'object' ? f : {};
  }

  function urlOf(u) {
    // Item URLs resolve against the page URL (section 4.3), with {res}/{assets} substituted.
    return itemUrl(u, app.page);
  }

  function imageOf(item, full = false) {
    const rk = roleFor(item, role());
    if (!rk) return null;
    const r = item.roles[rk];
    return {src: urlOf(full ? r.url : (r.thumb || r.url)), full: urlOf(r.url), role: rk, w: r.w, h: r.h};
  }

  function rowOfItem(k) {
    const it = items[k];
    return it && Number.isInteger(it.row) ? it.row : -1;
  }

  function passVis(k) {
    if (!vis) return true;
    const r = rowOfItem(k);
    return r < 0 || !!vis[r];
  }

  function roleLabel(key) {
    const r = ((doc && doc.roles) || []).find((x) => x.key === key);
    return r ? r.label || r.key : key;
  }

  // ------------------------------------------------------------------ toolbar

  const roleBox = el('span', 'pk-btn-group pk-gal-roles');
  roleBox.setAttribute('role', 'radiogroup');
  roleBox.setAttribute('aria-label', 'Image role');
  const presetSel = el('select', 'pk-gal-presets');
  presetSel.setAttribute('aria-label', 'Preset');
  const sortSel = el('select', 'pk-gal-sort');
  sortSel.setAttribute('aria-label', 'Sort');
  const search = el('input', 'pk-gal-search');
  search.type = 'search';
  search.placeholder = 'Search captions and tags';
  search.setAttribute('aria-label', 'Search captions and tags');
  const sizeIn = el('input', 'pk-gal-size');
  sizeIn.type = 'range';
  sizeIn.min = String(MIN_CARD);
  sizeIn.max = '480';
  sizeIn.step = '4';
  sizeIn.value = String(cardSize);
  sizeIn.setAttribute('aria-label', 'Card size');
  sizeIn.title = 'Card size';
  const facetBtn = el('button', 'pk-btn pk-gal-facet-toggle', 'Facets');
  facetBtn.type = 'button';
  facetBtn.setAttribute('aria-expanded', 'true');
  const count = el('span', 'pk-gal-count pk-muted');
  count.setAttribute('aria-live', 'polite');
  const annotBox = el('span', 'pk-gal-annot');
  const miniBox = el('span', 'pk-btn-group pk-gal-mini');

  function renderRoles() {
    roleBox.textContent = '';
    const roles = ((doc && doc.roles) || []).filter((r) => !Array.isArray(cfg.roles) || !cfg.roles.length || cfg.roles.includes(r.key));
    if (roles.length < 2) {
      roleBox.hidden = true;
      return;
    }
    roleBox.hidden = false;
    const cur = role();
    for (const r of roles) {
      const b = el('button', 'pk-btn', r.label || r.key);
      b.type = 'button';
      b.setAttribute('role', 'radio');
      b.setAttribute('aria-checked', String(r.key === cur));
      b.classList.toggle('pk-on', r.key === cur);
      b.addEventListener('click', () => store.set({role: r.key}, {source: ctx.id, history: true}));
      roleBox.append(b);
    }
  }

  function renderPresets() {
    const presets = (doc && doc.presets) || [];
    presetSel.textContent = '';
    if (!presets.length) {
      presetSel.hidden = true;
      return;
    }
    presetSel.hidden = false;
    presetSel.append(new Option('All images', ''));
    const cur = JSON.stringify(normFacets(facets()));
    let matched = '';
    for (const p of presets) {
      presetSel.append(new Option(p.label || p.key, p.key));
      if (JSON.stringify(normFacets(p.facets || {})) === cur) matched = p.key;
    }
    if (!matched && Object.keys(normFacets(facets())).length) presetSel.append(new Option('Custom', '__custom'));
    presetSel.value = matched || (Object.keys(normFacets(facets())).length ? '__custom' : '');
  }

  function normFacets(f) {
    const out = {};
    for (const k of Object.keys(f || {}).sort()) {
      const v = Array.isArray(f[k]) ? f[k].map(String).sort() : [];
      if (v.length) out[k] = v;
    }
    return out;
  }

  presetSel.addEventListener('change', () => {
    const key = presetSel.value;
    if (key === '__custom') return;
    const p = ((doc && doc.presets) || []).find((x) => x.key === key);
    store.set({facets: p ? normFacets(p.facets || {}) : {}}, {source: ctx.id, history: true});
  });

  function renderSort() {
    sortSel.textContent = '';
    const opts = [{v: '', label: 'Original order'}];
    const seen = new Set();
    for (const s of sortKeys) {
      seen.add(s.key);
      opts.push({v: `${s.dir < 0 ? '-' : ''}${s.key}`, label: `${s.key}${s.dir < 0 ? ' ↓' : ''}`});
    }
    for (const f of (doc && doc.facets) || []) {
      if (seen.has(f.key)) continue;
      opts.push({v: f.key, label: f.label || f.key});
    }
    if (!seen.has('id')) opts.push({v: 'id', label: 'id'});
    for (const o of opts) sortSel.append(new Option(`Sort: ${o.label}`, o.v));
    sortSel.value = sort ? `${sort.dir < 0 ? '-' : ''}${sort.key}` : '';
  }
  sortSel.addEventListener('change', () => {
    sort = parseSortKey(sortSel.value);
    recompute();
  });

  let searchTimer = 0;
  search.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      text = search.value;
      recompute();
    }, 150);
  });
  search.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && search.value) {
      ev.preventDefault();
      ev.stopPropagation();
      search.value = '';
      text = '';
      recompute();
    } else if (ev.key === 'Enter' || ev.key === 'ArrowDown') {
      ev.preventDefault();
      view.focus();
      if (order.length) setFocusPos(Math.max(0, focusPos));
    }
  });

  sizeIn.addEventListener('input', () => {
    cardSize = +sizeIn.value;
    layoutGrid();
  });

  if (typeof window !== 'undefined' && window.innerWidth < MOBILE_BREAK_PX) {
    // Phones: the facet sidebar overlays the grid, so it starts closed.
    facetBtn.setAttribute('aria-expanded', 'false');
    root.classList.add('pk-gal-facets-closed');
  }
  facetBtn.addEventListener('click', () => {
    const open = facetBtn.getAttribute('aria-expanded') !== 'true';
    facetBtn.setAttribute('aria-expanded', String(open));
    root.classList.toggle('pk-gal-facets-closed', !open);
    layoutGrid();
  });

  function renderMini() {
    miniBox.textContent = '';
    const layout = ctx.layout;
    if (!layout || typeof layout.panes !== 'function') return;
    const list = layout.panes().filter((p) => p && p.id !== ctx.id && MINI_TYPES.includes(p.type));
    for (const p of list) {
      const b = el('button', 'pk-btn pk-gal-mini-btn', p.type);
      b.type = 'button';
      b.title = p.collapsed ? `Open the ${p.type} panel` : `Show the ${p.type} panel`;
      b.classList.toggle('pk-on', !p.collapsed && p.visible);
      b.addEventListener('click', () => {
        layout.reveal(p.id).then(renderMini, report);
      });
      miniBox.append(b);
    }
    miniBox.hidden = !list.length;
  }

  // ------------------------------------------------------------------ annotate

  function renderAnnot() {
    annotBox.textContent = '';
    if (!annotCfg) return;
    const tog = el('button', 'pk-btn pk-gal-annot-toggle', 'Annotate');
    tog.type = 'button';
    tog.setAttribute('aria-pressed', String(annotOn));
    tog.classList.toggle('pk-on', annotOn);
    tog.title = 'Classify images with keyboard codes (stored in this browser only)';
    tog.addEventListener('click', () => {
      annotOn = !annotOn;
      if (annotOn && !ann) ann = annotationsFor(app, imgDs, {codes: annotCfg.codes});
      root.classList.toggle('pk-gal-annotating', annotOn);
      renderAnnot();
      paint(true);
      if (annotOn) view.focus();
    });
    annotBox.append(tog);
    if (!annotOn || !ann) return;
    const counts = ann.counts();
    const legend = el('span', 'pk-gal-annot-legend');
    for (const c of ann.codes) {
      const chip = el('span', `pk-chip pk-annot-chip pk-annot-${c}`, `${c} ${fmtCount(counts[c] || 0)}`);
      chip.title = `${c}: ${CODE_LABELS[c] || c} (press ${c})`;
      legend.append(chip);
    }
    const ex = el('button', 'pk-btn', 'Export CSV');
    ex.type = 'button';
    ex.disabled = !ann.size;
    ex.title = 'Download the annotations with the checklist key columns';
    ex.addEventListener('click', () => {
      downloadAnnotations(app, imgDs, ann, fieldsOf, {key: annotCfg.key || 'checklist'}).catch(report);
    });
    const clr = el('button', 'pk-btn', 'Clear');
    clr.type = 'button';
    clr.disabled = !ann.size;
    clr.addEventListener('click', () => {
      if (window.confirm(`Remove all ${plural(ann.size, 'annotation')} stored in this browser?`)) ann.clear();
    });
    const help = el('span', 'pk-muted pk-gal-annot-help', 'Keys set the code of the focused card; Backspace clears.');
    annotBox.append(legend, ex, clr, help);
  }

  async function fieldsOf(id) {
    const ks = idToItems ? idToItems.get(String(id)) : null;
    const it = ks && ks.length ? items[ks[0]] : null;
    const sources = [];
    if (it && it.tags) sources.push(it.tags);
    const r = it ? rowOfItem(ks[0]) : -1;
    if (client && r >= 0) {
      if (!catRows) catRows = new Map();
      // An untokened default-channel read: never superseded, never cancels another pane's read
      // (note 73). A null row is not cached.
      if (!catRows.has(r)) {
        const got = await client.record(r);
        if (got) catRows.set(r, got);
      }
      const row = catRows.get(r);
      if (row) sources.push(row);
    }
    if (it) sources.push(it);
    return checklistFields(sources);
  }

  function annotate(code) {
    if (!ann || focusPos < 0 || focusPos >= order.length) return false;
    const it = items[order[focusPos]];
    if (!it || it.id === undefined || it.id === null) return false;
    if (code === null) ann.set(String(it.id), null);
    else ann.set(String(it.id), code);
    if (code !== null && focusPos < order.length - 1) setFocusPos(focusPos + 1);
    return true;
  }

  // ------------------------------------------------------------------ facets sidebar

  function renderFacets() {
    side.textContent = '';
    const defs = (doc && doc.facets) || [];
    if (!defs.length) {
      root.classList.add('pk-gal-nofacets');
      facetBtn.hidden = true;
      return;
    }
    root.classList.remove('pk-gal-nofacets');
    facetBtn.hidden = false;
    const fac = facets();
    const counts = facetCounts(items, defs, fac, vis ? passVis : null);
    for (const f of defs) {
      const box = el('fieldset', 'pk-facet');
      const leg = el('legend', 'pk-facet-title', f.label || f.key);
      box.append(leg);
      const chosen = new Set(Array.isArray(fac[f.key]) ? fac[f.key].map(String) : []);
      const values = Array.from(counts[f.key].keys());
      for (const v of values) {
        const n = counts[f.key].get(v) || 0;
        const lab = el('label', 'pk-facet-value');
        const cb = el('input');
        cb.type = 'checkbox';
        cb.checked = chosen.has(v);
        cb.dataset.key = f.key;
        cb.dataset.value = v;
        lab.classList.toggle('pk-zero', n === 0 && !cb.checked);
        lab.append(cb, el('span', 'pk-facet-name', v), el('span', 'pk-facet-count pk-num', fmtCount(n)));
        box.append(lab);
      }
      if (chosen.size) {
        const clr = el('button', 'pk-btn pk-facet-clear', 'any');
        clr.type = 'button';
        clr.dataset.key = f.key;
        clr.title = `Show every ${f.label || f.key}`;
        box.append(clr);
      }
      side.append(box);
    }
  }

  side.addEventListener('change', (ev) => {
    const cb = ev.target;
    if (!cb || cb.type !== 'checkbox') return;
    const fac = Object.assign({}, facets());
    const cur = new Set(Array.isArray(fac[cb.dataset.key]) ? fac[cb.dataset.key].map(String) : []);
    if (cb.checked) cur.add(cb.dataset.value);
    else cur.delete(cb.dataset.value);
    if (cur.size) fac[cb.dataset.key] = Array.from(cur);
    else delete fac[cb.dataset.key];
    store.set({facets: normFacets(fac)}, {source: ctx.id, history: true});
  });
  side.addEventListener('click', (ev) => {
    const b = ev.target.closest ? ev.target.closest('.pk-facet-clear') : null;
    if (!b) return;
    const fac = Object.assign({}, facets());
    delete fac[b.dataset.key];
    store.set({facets: normFacets(fac)}, {source: ctx.id, history: true});
  });

  // ------------------------------------------------------------------ order + grid

  /**
   * Row ranks of the worker sort for (key, dir): the Int32Array when ready, 'failed' while the
   * recompute that follows a failed request runs, else null (the request is started and
   * recompute() runs again when it lands). A failed request is dropped from the cache, so the
   * next sort or recompute asks the worker again (as list.js does).
   */
  function rankFor(key, dir) {
    const ck = `${key}\u0000${dir}`;
    if (failedNow === ck) return 'failed';
    const have = ranks.get(ck);
    if (have instanceof Int32Array) {
      ranks.delete(ck);
      ranks.set(ck, have);
      return have;
    }
    if (have) return null;
    if (!client || typeof client.order !== 'function') return 'failed';
    let failed = false;
    const p = Promise.resolve(client.order(key, dir)).then((o) => {
      const arr = o instanceof Int32Array ? o : Int32Array.from(o && (o.order || o.idx) ? (o.order || o.idx) : (o || []));
      ranks.set(ck, rankOf(arr, client.n));
      while (ranks.size > RANK_CACHE) ranks.delete(ranks.keys().next().value);
    }, (err) => {
      if (!(err instanceof Error)) throw err;
      failed = true;
      if (ranks.get(ck) === p) ranks.delete(ck);
      if (err.name !== 'AbortError') {
        console.warn(`gallery ${ctx.id}: sort by ${key}`, err);
        store.emit('toast', {level: 'warn', text: `Sorting by ${key} failed: ${err.message}`});
      }
    }).then(() => {
      if (destroyed || !sort || !sort.key || `${catalogSortKey(sort.key, sortCtx())}\u0000${sort.dir < 0 ? -1 : 1}` !== ck) return;
      // After a failure this one recompute shows the local order without asking again.
      failedNow = failed ? ck : null;
      try {
        recompute();
      } finally {
        failedNow = null;
      }
    });
    ranks.set(ck, p);
    return null;
  }

  function sortCtx() {
    return {joined: !!(catDs && client), col: client ? (k) => client.col(k) : null, tagKeys};
  }

  function displayOrder() {
    const pass = vis ? passVis : null;
    // The local sort also orders the items the worker sort cannot rank (no catalog row).
    const list = filterItems(items, {facets: facets(), text, sort, pass});
    const ck = sort && sort.key ? catalogSortKey(sort.key, sortCtx()) : null;
    if (!ck) return {list, pending: false};
    const rank = rankFor(ck, sort.dir < 0 ? -1 : 1);
    if (rank === 'failed') return {list, pending: false};
    return rank ? {list: orderByRank(list, items, rank), pending: false} : {list, pending: true};
  }

  function recompute() {
    if (!doc) return;
    const keepId = focusPos >= 0 && focusPos < order.length ? order[focusPos] : -1;
    const shown = displayOrder();
    order = shown.list;
    posOf = new Map(order.map((k, p) => [k, p]));
    focusPos = keepId >= 0 && posOf.has(keepId) ? posOf.get(keepId) : -1;
    const parts = [order.length === items.length ? plural(items.length, 'image') : `${fmtCount(order.length)} of ${plural(items.length, 'image')}`];
    count.textContent = parts.join(' · ');
    empty.hidden = order.length > 0;
    if (!order.length) {
      empty.textContent = items.length ? 'No image matches the current facets, search or filters.' : 'This image set is empty.';
    }
    renderFacets();
    renderPresets();
    layoutGrid();
    refreshSet(setId);
    if (shown.pending) ctx.setStatus('sorting…');
    else ctx.setStatus(order.length === items.length ? '' : `${fmtCount(order.length)} shown`);
  }

  function layoutGrid() {
    const w = view.clientWidth;
    if (!w) return;
    geo = gridGeometry(w, cardSize);
    root.style.setProperty('--pk-gal-card-w', `${geo.cardW}px`);
    root.style.setProperty('--pk-gal-img-h', `${geo.imgH}px`);
    const rows = Math.ceil(order.length / geo.cols);
    spacer.style.height = `${Math.max(0, rows * geo.rowH - GAP)}px`;
    paint(true);
  }

  function paint(force = false) {
    if (!visible || destroyed || raf) return;
    raf = requestAnimationFrame(() => {
      raf = 0;
      paintNow(force);
    });
  }

  function ensurePool(k) {
    while (pool.length < k) {
      const c = el('div', 'pk-card');
      c.setAttribute('role', 'gridcell');
      const box = el('div', 'pk-card-img');
      const img = el('img');
      img.loading = 'lazy';
      img.decoding = 'async';
      img.draggable = false;
      img.addEventListener('error', () => {
        if (img.getAttribute('src')) c.classList.add('pk-card-broken');
      });
      img.addEventListener('load', () => c.classList.remove('pk-card-broken'));
      box.append(img);
      const cap = el('div', 'pk-card-cap');
      const badge = el('span', 'pk-card-badge');
      badge.hidden = true;
      c.append(box, cap, badge);
      c._img = img;
      c._cap = cap;
      c._badge = badge;
      c._k = -1;
      grid.append(c);
      pool.push(c);
    }
    for (let j = 0; j < pool.length; j++) pool[j].hidden = j >= k;
  }

  function paintNow() {
    if (!doc) return;
    const top = view.scrollTop;
    const h = view.clientHeight;
    const r0 = Math.max(0, Math.floor(top / geo.rowH) - 1);
    const r1 = Math.min(Math.ceil(order.length / geo.cols), Math.ceil((top + h) / geo.rowH) + 1);
    const p0 = r0 * geo.cols;
    const p1 = Math.min(order.length, r1 * geo.cols);
    const nCards = Math.max(0, p1 - p0);
    ensurePool(nCards);
    const f = store.get('focus');
    const selMask = sel ? sel.mask : null;
    const cur = role();
    for (let j = 0; j < nCards; j++) {
      const p = p0 + j;
      const k = order[p];
      const it = items[k];
      const c = pool[j];
      const row = Math.floor(p / geo.cols), col = p % geo.cols;
      c.style.transform = `translate(${col * (geo.cardW + GAP)}px, ${row * geo.rowH}px)`;
      c.style.width = `${geo.cardW}px`;
      c.dataset.k = String(k);
      c.dataset.p = String(p);
      c.id = `${ctx.id}-card-${p}`;
      if (c._k !== k || c._role !== cur) {
        c._k = k;
        c._role = cur;
        c.classList.remove('pk-card-broken');
        const im = imageOf(it);
        if (im && im.src) {
          if (c._img.getAttribute('src') !== im.src) c._img.src = im.src;
          c._img.alt = it.caption || String(it.id ?? '');
        } else {
          c._img.removeAttribute('src');
          c._img.alt = '';
          c.classList.add('pk-card-broken');
        }
        c._cap.textContent = '';
        c._cap.append(el('span', 'pk-card-id', String(it.id ?? k)));
        if (it.caption && it.caption !== String(it.id)) c._cap.append(el('span', 'pk-card-caption', it.caption));
        c.title = [String(it.id ?? ''), it.caption || '', im && im.role !== cur && cur ? `(${roleLabel(im.role)}; no ${roleLabel(cur)})` : ''].filter(Boolean).join(' · ');
      }
      const r = rowOfItem(k);
      const focused = p === focusPos || isFocused(f, k, r);
      c.classList.toggle('pk-focus', focused);
      c.classList.toggle('pk-hover', k === hoverK);
      c.classList.toggle('pk-sel', !!(selMask && r >= 0 && selMask[r]));
      const cv = colorVals && r >= 0 ? colorVals[r] : NaN;
      c.style.setProperty('--pk-swatch', cv === cv && colorPal && colorPal[cv | 0] ? colorPal[cv | 0] : 'transparent');
      c.setAttribute('aria-selected', String(focused));
      if (annotOn && ann && it.id !== undefined) {
        const code = ann.get(String(it.id));
        c._badge.hidden = !code;
        c._badge.textContent = code || '';
        c._badge.className = `pk-card-badge${code ? ` pk-annot-${code}` : ''}`;
        c._badge.title = code ? `${code}: ${CODE_LABELS[code] || code}` : '';
      } else {
        c._badge.hidden = true;
      }
    }
    if (focusPos >= 0 && view.contains(document.activeElement)) view.setAttribute('aria-activedescendant', `${ctx.id}-card-${focusPos}`);
  }

  function isFocused(f, k, r) {
    if (!f) return false;
    if (f.ds === imgDs) return f.i === k;
    if (catDs && f.ds === catDs) {
      if (r >= 0 && Number.isInteger(f.i)) return f.i === r;
      const it = items[k];
      return f.id !== null && f.id !== undefined && it && String(it.id) === String(f.id);
    }
    return false;
  }

  view.addEventListener('scroll', () => paint(), {passive: true});

  function scrollToPos(p) {
    if (p < 0) return;
    const row = Math.floor(p / geo.cols);
    const y = row * geo.rowH;
    const h = view.clientHeight;
    if (y < view.scrollTop) view.scrollTop = y;
    else if (y + geo.rowH > view.scrollTop + h) view.scrollTop = y + geo.rowH - h;
    paint();
  }

  // ------------------------------------------------------------------ interaction

  function writeFocus(k) {
    const it = items[k];
    if (!it) return;
    const r = rowOfItem(k);
    if (catDs && r >= 0) store.set({focus: {ds: catDs, i: r, id: null}}, {source: ctx.id, history: true});
    else if (catDs && it.id !== undefined && it.id !== null) {
      // boot resolves the row from the id (a toast reports ids missing from the catalog)
      store.set({focus: {ds: catDs, i: null, id: String(it.id)}}, {source: ctx.id, history: true});
    } else store.set({focus: {ds: imgDs, i: k, id: it.id !== undefined ? String(it.id) : null}}, {source: ctx.id, history: true});
  }

  function setFocusPos(p, write = true) {
    if (!order.length) return;
    const q = Math.max(0, Math.min(order.length - 1, p));
    focusPos = q;
    scrollToPos(q);
    if (write) writeFocus(order[q]);
    paint();
  }

  function cardFromEvent(ev) {
    const c = ev.target.closest ? ev.target.closest('.pk-card') : null;
    if (!c || !grid.contains(c)) return null;
    return {k: Number(c.dataset.k), p: Number(c.dataset.p)};
  }

  view.addEventListener('click', (ev) => {
    const hit = cardFromEvent(ev);
    if (!hit) return;
    const r = rowOfItem(hit.k);
    if (sel && r >= 0 && (ev.shiftKey || ev.altKey || ev.ctrlKey || ev.metaKey)) {
      const op = ev.ctrlKey || ev.metaKey ? (sel.mask[r] ? 'subtract' : 'add') : ev.altKey ? 'subtract' : 'add';
      sel.setIndices([r], op);
    }
    setFocusPos(hit.p);
  });
  view.addEventListener('dblclick', (ev) => {
    const hit = cardFromEvent(ev);
    if (!hit) return;
    openLightbox(store, setId, hit.k, ctx.id);
  });
  view.addEventListener('pointermove', (ev) => {
    if (ev.pointerType === 'touch') return;
    const hit = cardFromEvent(ev);
    const k = hit ? hit.k : -1;
    if (k === hoverK) return;
    hoverK = k;
    if (k < 0) store.set({hover: null}, {source: ctx.id});
    else {
      const r = rowOfItem(k);
      store.set({hover: catDs && r >= 0 ? {ds: catDs, i: r} : {ds: imgDs, i: k}}, {source: ctx.id});
    }
    paint();
  });
  view.addEventListener('pointerleave', () => {
    if (hoverK < 0) return;
    hoverK = -1;
    store.set({hover: null}, {source: ctx.id});
    paint();
  });

  view.addEventListener('keydown', (ev) => {
    if (ev.ctrlKey || ev.metaKey) return;
    if (annotOn && ann && !ev.altKey) {
      const code = codeForKey(ev.key, ann.codes);
      if (code !== undefined && annotate(code)) {
        ev.preventDefault();
        ev.stopPropagation();
        return;
      }
    }
    const cols = geo.cols;
    const rowsPerPage = Math.max(1, Math.floor(view.clientHeight / geo.rowH));
    let handled = true;
    const p = focusPos;
    switch (ev.key) {
      case 'ArrowRight': setFocusPos(p < 0 ? 0 : p + 1); break;
      case 'ArrowLeft': setFocusPos(p < 0 ? 0 : p - 1); break;
      case 'ArrowDown': setFocusPos(p < 0 ? 0 : p + cols); break;
      case 'ArrowUp': setFocusPos(p < 0 ? 0 : p - cols); break;
      case 'PageDown': setFocusPos(p < 0 ? 0 : p + cols * rowsPerPage); break;
      case 'PageUp': setFocusPos(p < 0 ? 0 : p - cols * rowsPerPage); break;
      case 'Home': setFocusPos(0); break;
      case 'End': setFocusPos(order.length - 1); break;
      case 'Enter':
        if (p >= 0) openLightbox(store, setId, order[p], ctx.id);
        else handled = false;
        break;
      default: handled = false;
    }
    if (handled) {
      ev.preventDefault();
      ev.stopPropagation();
    }
  });

  offs.push(store.onEvent('nav:step', (pl) => {
    if (!pl || pl.handled || !doc || !order.length) return;
    if (pl.ds !== imgDs && pl.ds !== catDs) return;
    // A visible source list on the same catalog walks its own order; the gallery steps only
    // when the focus is one of its cards or when it is the page's primary view.
    if (focusPos < 0 && pl.ds === catDs && hasVisibleList()) return;
    setFocusPos(focusPos < 0 ? 0 : focusPos + (pl.dir > 0 ? 1 : -1));
    pl.handled = true;
  }));

  function hasVisibleList() {
    const layout = ctx.layout;
    if (!layout || typeof layout.panes !== 'function') return false;
    return layout.panes().some((p) => p && p.type === 'list' && p.dataset === catDs && p.visible);
  }

  // ------------------------------------------------------------------ store wiring

  function followFocus(sources) {
    const f = store.get('focus');
    if (!f || !doc) {
      paint();
      return;
    }
    if (sources && sources.size === 1 && sources.has(ctx.id)) {
      paint();
      return;
    }
    let k = -1;
    if (f.ds === imgDs && Number.isInteger(f.i)) k = f.i;
    else if (catDs && f.ds === catDs) {
      const byRow = Number.isInteger(f.i) && rowToItems ? rowToItems.get(f.i) : null;
      const byId = f.id !== null && f.id !== undefined && idToItems ? idToItems.get(String(f.id)) : null;
      const cand = (byRow && byRow.length ? byRow : byId) || [];
      // Several items can share a catalog row: keep the focused card when it is one of them.
      const curK = focusPos >= 0 && focusPos < order.length ? order[focusPos] : -1;
      k = cand.includes(curK) ? curK : (cand.find((x) => posOf.has(x)) ?? -1);
    }
    if (k >= 0 && posOf.has(k)) {
      focusPos = posOf.get(k);
      scrollToPos(focusPos);
    }
    paint();
  }

  offs.push(store.on(['focus'], (changed, st, sources) => followFocus(sources)));
  offs.push(store.on(['facets'], () => recompute()));
  offs.push(store.on(['role'], () => {
    renderRoles();
    paint(true);
    refreshSet(setId);
  }));
  offs.push(store.on(['hover'], (changed, st, sources) => {
    if (sources.size === 1 && sources.has(ctx.id)) return;
    const hv = st.get('hover');
    let k = -1;
    if (hv && hv.ds === imgDs) k = hv.i;
    else if (hv && catDs && hv.ds === catDs && rowToItems) {
      const c = rowToItems.get(hv.i);
      k = c && c.length ? c[0] : -1;
    }
    if (k !== hoverK) {
      hoverK = k;
      paint();
    }
  }));
  offs.push(store.on([`annot:${imgDs}`], () => {
    if (annotOn) {
      renderAnnot();
      paint(true);
    }
  }));
  offs.push(store.onEvent('pane:resize', () => renderMini()));

  function report(err) {
    if (!(err instanceof Error)) throw err;
    if (err.name === 'AbortError') return;
    console.warn(`gallery ${ctx.id}`, err);
    ctx.setStatus('error', err.message);
  }

  // ------------------------------------------------------------------ load

  if (!ref || ref.kind !== 'images') {
    skel.remove();
    const n = el('div', 'pk-info-note', `The gallery needs an image set (got ${ref ? ref.kind : 'none'}).`);
    root.append(n);
  } else {
    ctx.setStatus('loading…');
    (async () => {
      doc = await hub.json(imgDs);
      if (destroyed) return;
      items = Array.isArray(doc && doc.items) ? doc.items : [];
      tagKeys = new Set(((doc && doc.facets) || []).map((f) => f && f.key).filter(Boolean));
      for (const it of items) if (it && it.tags) for (const k of Object.keys(it.tags)) tagKeys.add(k);
      rowToItems = new Map();
      idToItems = new Map();
      items.forEach((it, k) => {
        if (!it) return;
        if (Number.isInteger(it.row)) {
          if (!rowToItems.has(it.row)) rowToItems.set(it.row, []);
          rowToItems.get(it.row).push(k);
        }
        if (it.id !== undefined && it.id !== null) {
          const key = String(it.id);
          if (!idToItems.has(key)) idToItems.set(key, []);
          idToItems.get(key).push(k);
        }
      });
      const c = doc.catalog || ref.catalog || null;
      catDs = c && datasets[c] && datasets[c].kind === 'catalog' ? c : null;
      skel.remove();
      bar.append(facetBtn, roleBox, presetSel, sortSel, search, sizeIn, count, annotBox, miniBox);
      renderRoles();
      renderSort();
      renderAnnot();
      renderMini();
      // Populate `order` before registering, so a lightbox restored from the hash finds its items.
      recompute();
      setReady = !catDs;
      // The stored lightbox index is the item index in images.json (stable across the local sort
      // and search); the provider maps it to and from the display position.
      unregisterSet = registerSet(setId, {
        label: 'Gallery',
        ready: () => setReady,
        count: () => order.length,
        encode: (p) => (p >= 0 && p < order.length ? order[p] : -1),
        decode: (k) => (posOf.has(k) ? posOf.get(k) : -1),
        item: (p) => {
          const it = items[order[p]];
          if (!it) return null;
          const im = imageOf(it, true);
          if (!im) return null;
          return {src: im.full, title: String(it.id ?? ''), caption: [it.caption, im.role ? roleLabel(im.role) : ''].filter(Boolean).join(' · '), alt: it.caption || String(it.id ?? '')};
        },
        onIndex: (p) => {
          if (p !== focusPos) setFocusPos(p, false);
        },
      });
      ctx.setStatus('');
      followFocus(null);
      if (catDs) {
        client = await app.catalog(catDs);
        sel = await app.selectionFor(catDs);
        if (destroyed) return;
        const refreshVis = async () => {
          const v = store.get(`vis:${catDs}`);
          if (!v || v.count === null || v.count === undefined) vis = null;
          else {
            const r = await client.vis();
            vis = r && r.vis ? r.vis : null;
          }
          recompute();
        };
        offs.push(store.on([`vis:${catDs}`], () => { refreshVis().catch(report); }));
        offs.push(store.on([`sel:${catDs}`], () => paint()));
        const refreshColor = async () => {
          const cc = store.get('color');
          const m = cc && cc.by ? client.col(cc.by) : null;
          if (isCategory(m)) {
            colorVals = await client.values(cc.by);
            colorPal = plotLevelColors(m, ctx.theme ? ctx.theme() : null);
          } else {
            colorVals = null;
            colorPal = null;
          }
          root.classList.toggle('pk-gal-colored', !!colorVals);
          paint(true);
        };
        offs.push(store.on(['color'], () => { refreshColor().catch(report); }));
        offs.push(store.onEvent('theme:change', () => { refreshColor().catch(report); }));
        if (ctx.tools) offTools = mountSelectionTools(app, catDs, ctx.tools, {base: imgDs});
        await refreshVis();
        setReady = true;
        refreshSet(setId);
        await refreshColor();
      }
    })().catch((err) => {
      if (!setReady) {
        setReady = true;
        refreshSet(setId);
      }
      skel.remove();
      report(err);
      const n = el('div', 'pk-error-note', `The gallery could not be loaded: ${err.message}`);
      n.setAttribute('role', 'alert');
      root.append(n);
    });
  }

  return {
    resize() {
      layoutGrid();
    },
    onVisible(v) {
      visible = v;
      if (v) {
        layoutGrid();
        renderMini();
      }
    },
    destroy() {
      destroyed = true;
      clearTimeout(searchTimer);
      if (raf) cancelAnimationFrame(raf);
      for (const off of offs) off();
      if (unregisterSet) unregisterSet();
      if (offTools) offTools();
      if (hoverK >= 0) store.set({hover: null}, {source: ctx.id});
    },
    stats() {
      return {items: items.length, shown: order.length, cards: pool.filter((c) => !c.hidden).length, cols: geo.cols};
    },
  };
}
