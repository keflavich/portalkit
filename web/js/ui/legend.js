// Colour legend with counts and category toggles (class_coloring, ARCHITECTURE.md 8.2).
//
//   const lg = createLegend(host, {app, store, client, ds, pane, config});
//   lg.refresh(); lg.destroy();
//
// The legend follows the effective colour of a plot pane: the store key `color`
// ({by, cmap, lo?, hi?}), or the pane option `color` when the store `by` is null; the store
// value by: '' is an explicit "none" that overrides the option.
//   category column -> one toggle chip per level (swatch, label, count); clicking a chip hides
//                      or shows the level through filters:<ds>.cats[key] (off-level list);
//                      alt-click shows only that level, a second alt-click shows all again
//   numeric column  -> colormap gradient with the lo / hi values
//   none            -> nothing but the counts line
// The counts line reads "<shown> of <N> shown · <k> selected" from vis:<ds> and sel:<ds>.
//
// Helpers shared with the plot panes (same colours everywhere):
//   resolveColor(store, config, client) -> {by, cmap, lo, hi, kind: 'none'|'cat'|'cont', meta}
//   levelColors(meta, tokens) -> CSS colour per level
//   plotLevelColors(meta, tokens) -> CSS colour per level as plotted (neutral from level 7 on)
//   gpuCatCodes(values, nLevels) -> Uint8Array GPU codes (level L -> L + 1 for L < 7, else 0)
//   gpuCatColors(meta, tokens) -> 8 [r, g, b] (code 0 = neutral)

import {cmapCSS, CMAP_NAMES} from '../plot/canvas2d.js';
import {parseColor} from '../core/theme.js';
import {fmtCount, fmtNum} from '../core/fmt.js';
import {isValid} from '../data/expr.js';

/** GPU colour codes available to category levels (code 0 is the neutral "other / missing"). */
export const GPU_LEVELS = 7;

const FALLBACK_CAT = ['#3b82c4', '#e0812f', '#3f9b4a', '#c8463d', '#8a64b5', '#8c5a48', '#d36fb0', '#7b818a', '#b1b235', '#2fb3c4'];

/** True when a column meta describes a category column with levels. */
export function isCategory(meta) {
  if (!meta) return false;
  if (!(meta.role === 'category' || meta.enc === 'cat8' || meta.enc === 'cat16')) return false;
  return Array.isArray(meta.levels) && meta.levels.length > 0;
}

/** CSS colour per level: manifest colors{level: hex}, else the theme category palette. */
export function levelColors(meta, tokens) {
  const levels = (meta && meta.levels) || [];
  const pal = (tokens && Array.isArray(tokens.cat) && tokens.cat.some(Boolean)) ? tokens.cat : FALLBACK_CAT;
  const named = (meta && meta.colors) || {};
  return levels.map((lv, i) => named[lv] || pal[i % pal.length] || FALLBACK_CAT[i % FALLBACK_CAT.length]);
}

/** GPU category codes from level indices (NaN = missing): level L -> L + 1 when L < 7, else 0. */
export function gpuCatCodes(values, nLevels = GPU_LEVELS) {
  const n = values ? values.length : 0;
  const out = new Uint8Array(n);
  const lim = Math.min(GPU_LEVELS, Math.max(0, nLevels | 0) || GPU_LEVELS);
  for (let i = 0; i < n; i++) {
    const v = values[i];
    if (v === v && v >= 0 && v < lim) out[i] = (v | 0) + 1;
  }
  return out;
}

/**
 * Plot colour per level, the same on every tier: levels below GPU_LEVELS take levelColors,
 * levels from GPU_LEVELS on take the neutral token (the GPU code 0 colour), so a category keeps
 * its colour when the pane changes between canvas2d (T0) and XYRenderer (T1/T2).
 */
export function plotLevelColors(meta, tokens) {
  const neutral = (tokens && tokens.neutral) || '#4b5563';
  return levelColors(meta, tokens).map((c, i) => (i < GPU_LEVELS ? c : neutral));
}

function rgb01(css, fallback) {
  const c = typeof css === 'string' ? parseColor(css) : null;
  return c || fallback;
}

/** 8 [r, g, b] (0..1) for GPU codes 0..7: code 0 neutral, code L + 1 the colour of level L. */
export function gpuCatColors(meta, tokens) {
  const neutral = (tokens && tokens.neutralRGB) || [0.29, 0.33, 0.39];
  const cols = levelColors(meta, tokens);
  const out = [neutral];
  for (let c = 0; c < GPU_LEVELS; c++) out.push(c < cols.length ? rgb01(cols[c], neutral) : neutral);
  return out;
}

function exprUsable(client, by) {
  if (!by || typeof by !== 'string') return false;
  if (client.col(by)) return true;
  if (!isValid(by)) return false;
  return client.baseKeys(by).every((k) => !!client.col(k));
}

/**
 * Effective colour of a plot pane. The store `color.by` wins; otherwise the pane option
 * `color` (a key / expression string or {by, cmap, lo, hi}). Keys the catalog cannot evaluate
 * give kind 'none'.
 */
export function resolveColor(store, config, client) {
  const sc = (store && store.get('color')) || {};
  const oc = config && config.color;
  const opt = typeof oc === 'string' ? {by: oc} : (oc && typeof oc === 'object' ? oc : {});
  // by: '' in the store is an explicit "none" that overrides the pane option; null / missing
  // means "not chosen" and falls back to the option.
  const explicitNone = sc.by === '';
  const fromStore = sc.by != null && sc.by !== '';
  const by = fromStore ? sc.by : (explicitNone ? null : (opt.by ?? null));
  let cmap = sc.cmap || opt.cmap || 'viridis';
  const base = String(cmap).toLowerCase().replace(/_r$/, '');
  if (!CMAP_NAMES.includes(base)) cmap = 'viridis';
  const lo = fromStore ? sc.lo : (sc.lo ?? opt.lo);
  const hi = fromStore ? sc.hi : (sc.hi ?? opt.hi);
  if (!client || !exprUsable(client, by)) return {by: null, cmap, lo: null, hi: null, kind: 'none', meta: null};
  const meta = client.col(by);
  if (isCategory(meta)) return {by, cmap, lo: null, hi: null, kind: 'cat', meta};
  return {
    by, cmap, kind: 'cont', meta: meta || {key: by, label: by},
    lo: Number.isFinite(Number(lo)) && lo !== null ? Number(lo) : null,
    hi: Number.isFinite(Number(hi)) && hi !== null ? Number(hi) : null,
  };
}

/** Robust colour range (2nd..98th percentile of a strided sample) of a value array. */
export function autoRange(values, mask = null) {
  const n = values ? values.length : 0;
  const step = Math.max(1, Math.floor(n / 20000));
  const s = [];
  for (let i = 0; i < n; i += step) {
    if (mask && !mask[i]) continue;
    const v = values[i];
    if (v === v && v !== Infinity && v !== -Infinity) s.push(v);
  }
  if (!s.length) return [0, 1];
  s.sort((a, b) => a - b);
  const q = (p) => s[Math.min(s.length - 1, Math.max(0, Math.round(p * (s.length - 1))))];
  let lo = q(0.02), hi = q(0.98);
  if (!(hi > lo)) { lo = s[0]; hi = s[s.length - 1]; }
  if (!(hi > lo)) hi = lo + 1;
  return [lo, hi];
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

/** Toggle `level` in a filters object's cats[key] off-list (returns a new filters object). */
export function toggleLevel(filters, key, level, {only = false, levels = []} = {}) {
  const f = {...(filters || {})};
  const cats = {...(f.cats || {})};
  const off = new Set(cats[key] || []);
  if (only) {
    const others = levels.filter((l) => l !== level);
    const isOnly = !off.has(level) && others.every((l) => off.has(l));
    cats[key] = isOnly ? [] : others;
  } else if (off.has(level)) {
    off.delete(level);
    cats[key] = Array.from(off);
  } else {
    off.add(level);
    cats[key] = Array.from(off);
  }
  if (!cats[key].length) delete cats[key];
  if (Object.keys(cats).length) f.cats = cats;
  else delete f.cats;
  return f;
}

/**
 * Mount a legend into `host`. opts: {app, store, client, ds, pane, config, theme?: () => tokens,
 * range?: () => [lo, hi] (continuous colour range in use), note?: (colour) => string | null
 * (a pane that cannot draw a continuous colour on its current path returns the reason)}.
 * Returns {el, refresh(), destroy()}.
 */
export function createLegend(host, opts) {
  const {store, client, ds, pane} = opts;
  const config = opts.config || {};
  const theme = opts.theme || (opts.app && opts.app.theme) || (() => ({}));
  const root = el('div', 'pk-legend');
  root.setAttribute('aria-label', 'Colour legend');
  const counts = el('div', 'pk-legend-counts pk-num');
  const body = el('div', 'pk-legend-body');
  root.append(counts, body);
  host.append(root);
  let destroyed = false;
  let seq = 0;
  let levelCounts = null;    // {by, counts: Float64Array}

  function renderCounts() {
    const vis = store.get(`vis:${ds}`) || {};
    const sel = store.get(`sel:${ds}`) || {};
    const n = client.n;
    const shown = vis.count == null ? n : vis.count;
    const parts = [shown === n ? `${fmtCount(n)} sources` : `${fmtCount(shown)} of ${fmtCount(n)} shown`];
    if (sel.count) parts.push(`${fmtCount(sel.count)} selected`);
    counts.textContent = parts.join(' · ');
  }

  async function loadLevelCounts(c) {
    const my = ++seq;
    const L = c.meta.levels.length;
    const h = await client.hist(c.by, L, {range: [-0.5, L - 0.5], mask: null});
    if (destroyed || my !== seq) return;
    levelCounts = {by: c.by, counts: h.counts};
    renderBody();
  }

  function renderBody() {
    const c = resolveColor(store, config, client);
    body.textContent = '';
    root.dataset.kind = c.kind;
    if (c.kind === 'cat') {
      const meta = c.meta;
      const cols = plotLevelColors(meta, theme());
      const filters = store.get(`filters:${ds}`) || {};
      const off = new Set((filters.cats && filters.cats[c.by]) || []);
      const title = el('span', 'pk-legend-title', meta.label || c.by);
      body.append(title);
      const list = el('div', 'pk-legend-levels');
      list.setAttribute('role', 'group');
      meta.levels.forEach((lv, i) => {
        const b = el('button', 'pk-chip pk-legend-level');
        b.type = 'button';
        const on = !off.has(lv);
        b.classList.toggle('pk-on', on);
        b.classList.toggle('pk-muted', !on);
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
        b.title = `${on ? 'Hide' : 'Show'} ${lv} (alt-click: show only this level)`;
        const sw = el('span', 'pk-swatch');
        sw.style.background = cols[i];
        if (i >= GPU_LEVELS) sw.classList.add('pk-swatch-shared');
        const lab = el('span', 'pk-legend-label', String(lv));
        b.append(sw, lab);
        if (levelCounts && levelCounts.by === c.by) {
          b.append(el('span', 'pk-legend-n pk-num', fmtCount(levelCounts.counts[i] || 0)));
        }
        b.addEventListener('click', (e) => {
          const f = toggleLevel(store.get(`filters:${ds}`) || {}, c.by, lv, {only: e.altKey, levels: meta.levels});
          store.set({[`filters:${ds}`]: f}, {source: pane});
          renderBody();
        });
        list.append(b);
      });
      body.append(list);
      if (!levelCounts || levelCounts.by !== c.by) {
        loadLevelCounts(c).catch((err) => {
          if (err && err.name === 'AbortError') return;
          store.emit('toast', {level: 'warn', text: `Legend counts unavailable: ${err.message}`});
        });
      }
    } else if (c.kind === 'cont') {
      const title = el('span', 'pk-legend-title', (c.meta && c.meta.label) || c.by);
      const note = opts.note ? opts.note(c) : null;
      if (note) {
        // the pane cannot draw this colour on its current path: say so instead of a gradient
        root.dataset.kind = 'none';
        body.append(title, el('span', 'pk-legend-note pk-muted', note));
        return;
      }
      const bar = el('span', 'pk-legend-bar');
      bar.style.background = cmapCSS(c.cmap);
      const r = opts.range ? opts.range() : null;
      const lo = c.lo ?? (r ? r[0] : null);
      const hi = c.hi ?? (r ? r[1] : null);
      const loEl = el('span', 'pk-legend-lo pk-num', lo == null ? '' : fmtNum(lo, 3));
      const hiEl = el('span', 'pk-legend-hi pk-num', hi == null ? '' : fmtNum(hi, 3));
      body.append(title, loEl, bar, hiEl);
    }
  }

  function refresh() {
    if (destroyed) return;
    renderCounts();
    renderBody();
  }

  const offStore = store.on(['color', `filters:${ds}`, `vis:${ds}`, `sel:${ds}`, 'theme'], (changed) => {
    if (changed.has('color') || changed.has(`filters:${ds}`) || changed.has('theme')) renderBody();
    renderCounts();
  });
  const offTheme = store.onEvent('theme:change', () => renderBody());
  refresh();

  return {
    el: root,
    refresh,
    destroy() {
      destroyed = true;
      offStore();
      offTheme();
      root.remove();
    },
  };
}
