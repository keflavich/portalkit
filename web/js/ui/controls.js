// Page controls (av_slider and friends, ARCHITECTURE.md 6.2 / 8.2): A_V, R_V and distance
// modulus sliders, colour-by picker, template picker and feature toggles.
//
//   const c = createControls(host, {app, store, client, pane, config, show});
//   c.destroy();
// or the single widgets: colorControl(), extinctionControls(), templatePicker(),
// featureToggles() -> {el, destroy()}.
//
// Store keys written (source = pane id): color {by, cmap, lo?, hi?}, av, rv, dm (numbers; slider
// drags are coalesced to one store.set per animation frame), template {key, grid, scale,
// bands?} | null, features string[]. Every widget follows outside changes of its key.
//
// Default visibility (createControls): colour when a catalog is given; A_V / R_V with the
// `av_slider` capability; distance modulus when the page state sets `dm`; templates with
// `model_template_overlay` and a `templates` dataset; features with `labeled_features_overlay`
// and a `features` dataset. opts.show = {color, av, rv, dm, template, features} overrides.

import {CMAP_NAMES, cmapCSS} from '../plot/canvas2d.js';
import {fmtNum} from '../core/fmt.js';
import {axisGroups, checkExpr} from './axis_picker.js';
import {isCategory} from './legend.js';

export const AV_MAX = 60;
export const DM_MAX = 20;
export const RV_CHOICES = Object.freeze([3.1, 5.5]);
const MAIN_CMAPS = ['viridis', 'magma', 'inferno', 'plasma', 'cividis', 'turbo', 'coolwarm', 'rdbu', 'gray'];

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

const raf = typeof requestAnimationFrame === 'function' ? requestAnimationFrame : (fn) => setTimeout(fn, 16);

/** A setter that coalesces patches and writes them to the store once per animation frame. */
export function rafSetter(store, source) {
  let pending = null;
  return (patch) => {
    const first = pending === null;
    pending = {...(pending || {}), ...patch};
    if (!first) return;
    raf(() => {
      const p = pending;
      pending = null;
      store.set(p, {source});
    });
  };
}

/** Datasets of `kind` in the page (ids). */
export function datasetsOfKind(page, kind) {
  const ds = (page && page.datasets) || {};
  return Object.keys(ds).filter((k) => ds[k] && ds[k].kind === kind);
}

function sliderRow({key, label, title, min, max, step, value, digits, onInput}) {
  const row = el('label', `pk-ctl pk-ctl-${key}`);
  row.title = title;
  const name = el('span', 'pk-ctl-name', label);
  const range = el('input', 'pk-ctl-range');
  range.type = 'range';
  range.min = String(min);
  range.max = String(max);
  range.step = String(step);
  range.value = String(value);
  range.setAttribute('aria-label', title);
  const num = el('input', 'pk-ctl-num pk-num');
  num.type = 'number';
  num.min = String(min);
  num.step = String(step);
  num.value = fmtNum(value, digits);
  num.setAttribute('aria-label', `${title} value`);
  range.addEventListener('input', () => {
    const v = Number(range.value);
    num.value = fmtNum(v, digits);
    onInput(v);
  });
  num.addEventListener('change', () => {
    const v = Number(num.value);
    if (!Number.isFinite(v)) return;
    if (v > Number(range.max)) range.max = String(Math.ceil(v * 1.5));
    range.value = String(v);
    onInput(v);
  });
  row.append(name, range, num);
  return {
    el: row,
    set(v) {
      if (!Number.isFinite(Number(v))) return;
      if (Number(v) > Number(range.max)) range.max = String(Math.ceil(Number(v) * 1.5));
      if (document.activeElement !== range) range.value = String(v);
      if (document.activeElement !== num) num.value = fmtNum(Number(v), digits);
    },
  };
}

/**
 * A_V / R_V / distance-modulus controls. opts: {store, pane, av = true, rv = true, dm = false,
 * avMax = AV_MAX, dmMax = DM_MAX}.
 */
export function extinctionControls(opts) {
  const {store, pane} = opts;
  const root = el('div', 'pk-ctl-group pk-ctl-ext');
  const set = rafSetter(store, pane);
  const rows = {};
  if (opts.av !== false) {
    rows.av = sliderRow({key: 'av', label: 'A_V', title: 'Visual extinction A_V (mag)', min: 0, max: Math.max(opts.avMax || AV_MAX, Number(store.get('av')) || 0), step: 0.1,
      value: Number(store.get('av')) || 0, digits: 1, onInput: (v) => set({av: Math.max(0, v)})});
    root.append(rows.av.el);
  }
  let rvSel = null;
  if (opts.rv !== false) {
    const lab = el('label', 'pk-ctl pk-ctl-rv');
    lab.title = 'Total-to-selective extinction R_V';
    rvSel = el('select', 'pk-ctl-select');
    rvSel.setAttribute('aria-label', 'R_V');
    const fill = () => {
      const cur = Number(store.get('rv')) || 3.1;
      rvSel.textContent = '';
      const vals = RV_CHOICES.includes(cur) ? RV_CHOICES : [...RV_CHOICES, cur].sort((a, b) => a - b);
      for (const v of vals) {
        const o = el('option', null, String(v));
        o.value = String(v);
        rvSel.append(o);
      }
      rvSel.value = String(cur);
    };
    fill();
    rvSel.addEventListener('change', () => store.set({rv: Number(rvSel.value)}, {source: pane}));
    lab.append(el('span', 'pk-ctl-name', 'R_V'), rvSel);
    root.append(lab);
    rows.rv = {set: fill};
  }
  if (opts.dm) {
    rows.dm = sliderRow({key: 'dm', label: 'DM', title: 'Distance modulus added to magnitudes (mag)', min: 0, max: Math.max(opts.dmMax || DM_MAX, Number(store.get('dm')) || 0), step: 0.05,
      value: Number(store.get('dm')) || 0, digits: 2, onInput: (v) => set({dm: v})});
    root.append(rows.dm.el);
  }
  const off = store.on(['av', 'rv', 'dm'], (changed, s, sources) => {
    if (sources.size === 1 && sources.has(pane)) return;
    for (const k of changed) if (rows[k]) rows[k].set(s.get(k));
  });
  return {el: root, destroy() { off(); root.remove(); }};
}

/**
 * Colour-by picker: none, numeric columns / virtual dims (grouped), category columns, a typed
 * expression and the colormap. opts: {store, client, pane, config?}.
 */
export function colorControl(opts) {
  const {store, client, pane} = opts;
  const root = el('div', 'pk-ctl-group pk-ctl-color');
  const lab = el('label', 'pk-ctl');
  lab.title = 'Colour points by a column or expression';
  const sel = el('select', 'pk-ctl-select pk-color-by');
  sel.setAttribute('aria-label', 'Colour by');
  const none = el('option', null, 'none');
  none.value = '';
  sel.append(none);
  const custom = el('option');
  custom.hidden = true;
  sel.append(custom);
  const cats = (client.manifest.cols || []).filter(isCategory);
  if (cats.length) {
    const og = el('optgroup');
    og.label = `Categories (${cats.length})`;
    for (const c of cats) {
      const o = el('option', null, c.label || c.key);
      o.value = c.key;
      og.append(o);
    }
    sel.append(og);
  }
  const known = new Set(['', ...cats.map((c) => c.key)]);
  for (const g of axisGroups(client, {position: false})) {
    const og = el('optgroup');
    og.label = `${g.label} (${g.items.length})`;
    for (const it of g.items) {
      const o = el('option', null, it.label);
      o.value = it.key;
      og.append(o);
      known.add(it.key);
    }
    sel.append(og);
  }
  const expr = el('input', 'pk-ctl-expr pk-mono');
  expr.type = 'text';
  expr.placeholder = 'colour expression';
  expr.spellcheck = false;
  expr.setAttribute('aria-label', 'Colour expression');
  const cm = el('select', 'pk-ctl-select pk-cmap');
  cm.setAttribute('aria-label', 'Colormap');
  const names = MAIN_CMAPS.filter((n) => CMAP_NAMES.includes(n)).concat(CMAP_NAMES.filter((n) => !MAIN_CMAPS.includes(n) && n !== 'grey' && n !== 'greys'));
  for (const n of names) {
    for (const nm of [n, `${n}_r`]) {
      const o = el('option', null, nm);
      o.value = nm;
      cm.append(o);
    }
  }
  const sw = el('span', 'pk-cmap-swatch');
  lab.append(el('span', 'pk-ctl-name', 'Colour'), sel);
  root.append(lab, expr, cm, sw);

  const optBy = () => {
    const oc = opts.config && opts.config.color;
    return typeof oc === 'string' ? oc : (oc && oc.by) || null;
  };
  function sync() {
    const c = store.get('color') || {};
    const by = c.by === '' ? null : (c.by != null ? c.by : optBy());
    if (by && !known.has(by)) {
      custom.hidden = false;
      custom.value = by;
      custom.textContent = by;
    } else {
      custom.hidden = true;
    }
    sel.value = by || '';
    cm.value = c.cmap || 'viridis';
    if (!cm.value) cm.value = 'viridis';
    sw.style.background = cmapCSS(cm.value);
    const isCat = by && cats.some((x) => x.key === by);
    cm.disabled = !by || isCat;
  }
  const write = (patch) => {
    const c = {...(store.get('color') || {cmap: 'viridis'}), ...patch};
    if ('by' in patch) { delete c.lo; delete c.hi; }
    store.set({color: c}, {source: pane});
  };
  // 'none' writes by: '' (explicit none) when a pane option colour would otherwise apply.
  const noneBy = () => (optBy() ? '' : null);
  sel.addEventListener('change', () => { write({by: sel.value || noneBy()}); sync(); });
  cm.addEventListener('change', () => { write({cmap: cm.value}); sync(); });
  expr.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    const s = expr.value.trim();
    const err = s ? checkExpr(client, s) : null;
    expr.classList.toggle('pk-invalid', !!err);
    expr.title = err || '';
    if (err) return;
    write({by: s || noneBy()});
    expr.value = '';
    sync();
  });
  const off = store.on(['color'], (changed, s, sources) => {
    if (sources.size === 1 && sources.has(pane)) return;
    sync();
  });
  sync();
  return {el: root, destroy() { off(); root.remove(); }};
}

/**
 * Template picker for a pk-templates/1 dataset: template, grid and normalization.
 * opts: {store, hub, pane, ds (templates dataset id)}.
 */
export function templatePicker(opts) {
  const {store, hub, pane, ds} = opts;
  const root = el('div', 'pk-ctl-group pk-ctl-template');
  const lab = el('label', 'pk-ctl');
  lab.title = 'Model template drawn over spectra and SEDs';
  const sel = el('select', 'pk-ctl-select pk-template');
  sel.setAttribute('aria-label', 'Template');
  const grid = el('select', 'pk-ctl-select pk-template-grid');
  grid.setAttribute('aria-label', 'Template grid');
  const scale = el('select', 'pk-ctl-select pk-template-scale');
  scale.setAttribute('aria-label', 'Template scaling');
  for (const [v, t] of [['auto', 'scale: auto'], ['1', 'scale: 1']]) {
    const o = el('option', null, t);
    o.value = v;
    scale.append(o);
  }
  lab.append(el('span', 'pk-ctl-name', 'Template'), sel);
  root.append(lab, grid, scale);
  sel.disabled = true;
  const loading = el('option', null, 'loading…');
  loading.value = '';
  sel.append(loading);
  let doc = null;
  let destroyed = false;

  function sync() {
    const t = store.get('template');
    sel.value = t && t.key ? t.key : '';
    grid.value = (t && t.grid) || grid.options[0]?.value || '';
    scale.value = t && t.scale !== undefined && t.scale !== 'auto' ? String(t.scale) : 'auto';
    if (t && t.scale !== undefined && t.scale !== 'auto' && scale.value !== String(t.scale)) {
      const o = el('option', null, `scale: ${t.scale}`);
      o.value = String(t.scale);
      scale.append(o);
      scale.value = String(t.scale);
    }
    grid.disabled = !sel.value;
    scale.disabled = !sel.value;
  }
  function write() {
    if (!sel.value) {
      store.set({template: null}, {source: pane});
    } else {
      const prev = store.get('template') || {};
      const sc = scale.value === 'auto' ? 'auto' : Number(scale.value);
      const t = {key: sel.value, grid: grid.value || null, scale: sc};
      if (Array.isArray(prev.bands)) t.bands = prev.bands;
      store.set({template: t}, {source: pane});
    }
    sync();
  }
  sel.addEventListener('change', write);
  grid.addEventListener('change', write);
  scale.addEventListener('change', write);

  Promise.resolve(hub.json(ds)).then((d) => {
    if (destroyed) return;
    doc = d || {};
    sel.textContent = '';
    const none = el('option', null, 'none');
    none.value = '';
    sel.append(none);
    for (const t of doc.templates || []) {
      const o = el('option', null, t.label || t.key);
      o.value = t.key;
      sel.append(o);
    }
    grid.textContent = '';
    for (const g of Object.keys(doc.grids || {})) {
      const o = el('option', null, g);
      o.value = g;
      grid.append(o);
    }
    sel.disabled = false;
    sync();
  }, (err) => {
    if (destroyed) return;
    loading.textContent = 'unavailable';
    root.title = err.message;
  });
  const off = store.on(['template'], (changed, s, sources) => {
    if (sources.size === 1 && sources.has(pane)) return;
    if (doc) sync();
  });
  return {el: root, destroy() { destroyed = true; off(); root.remove(); }};
}

/**
 * Spectral-feature family toggles for a pk-features/1 dataset. opts: {store, hub, pane, ds}.
 */
export function featureToggles(opts) {
  const {store, hub, pane, ds} = opts;
  const root = el('div', 'pk-ctl-group pk-ctl-features');
  root.setAttribute('role', 'group');
  root.setAttribute('aria-label', 'Spectral features');
  root.append(el('span', 'pk-ctl-name', 'Features'));
  const box = el('span', 'pk-ctl-chips');
  root.append(box);
  let fams = [];
  let destroyed = false;
  function render() {
    box.textContent = '';
    const on = new Set(store.get('features') || []);
    for (const f of fams) {
      const b = el('button', 'pk-chip', f);
      b.type = 'button';
      b.classList.toggle('pk-on', on.has(f));
      b.setAttribute('aria-pressed', on.has(f) ? 'true' : 'false');
      b.addEventListener('click', () => {
        const cur = new Set(store.get('features') || []);
        if (cur.has(f)) cur.delete(f);
        else cur.add(f);
        store.set({features: fams.filter((x) => cur.has(x)).concat(Array.from(cur).filter((x) => !fams.includes(x)))}, {source: pane});
        render();
      });
      box.append(b);
    }
  }
  Promise.resolve(hub.json(ds)).then((doc) => {
    if (destroyed) return;
    fams = Object.keys((doc && doc.families) || {});
    render();
  }, (err) => {
    if (destroyed) return;
    root.title = err.message;
    root.hidden = true;
  });
  const off = store.on(['features'], (changed, s, sources) => {
    if (sources.size === 1 && sources.has(pane)) return;
    render();
  });
  return {el: root, destroy() { destroyed = true; off(); root.remove(); }};
}

/** Which widgets createControls shows by default (see the header). */
export function defaultShow(app, client) {
  const caps = (app && app.caps) || new Set();
  const page = (app && app.page) || {};
  const state = page.state || {};
  return {
    color: !!client,
    av: caps.has('av_slider'),
    rv: caps.has('av_slider'),
    dm: Object.prototype.hasOwnProperty.call(state, 'dm'),
    template: caps.has('model_template_overlay') && datasetsOfKind(page, 'templates').length > 0,
    features: caps.has('labeled_features_overlay') && datasetsOfKind(page, 'features').length > 0,
  };
}

/**
 * Mount the control bar. opts: {app, store, client?, pane, config?, show?}. Returns
 * {el, destroy()}; el is hidden when nothing applies.
 */
export function createControls(host, opts) {
  const {app, store, client, pane} = opts;
  const show = {...defaultShow(app, client), ...(opts.show || {})};
  const root = el('div', 'pk-controls');
  host.append(root);
  const parts = [];
  if (show.color && client) parts.push(colorControl({store, client, pane, config: opts.config}));
  if (show.av || show.rv || show.dm) parts.push(extinctionControls({store, pane, av: !!show.av, rv: !!show.rv, dm: !!show.dm}));
  const page = (app && app.page) || {};
  const hub = (app && app.hub) || opts.hub;
  if (show.template && hub) {
    const ds = datasetsOfKind(page, 'templates')[0];
    if (ds) parts.push(templatePicker({store, hub, pane, ds}));
  }
  if (show.features && hub) {
    const ds = datasetsOfKind(page, 'features')[0];
    if (ds) parts.push(featureToggles({store, hub, pane, ds}));
  }
  for (const p of parts) root.append(p.el);
  root.hidden = !parts.length;
  return {
    el: root,
    destroy() {
      for (const p of parts) p.destroy();
      root.remove();
    },
  };
}
