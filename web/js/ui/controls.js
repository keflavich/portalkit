// Page controls (av_slider and friends, ARCHITECTURE.md 6.2 / 8.2): A_V, R_V and distance
// modulus sliders, colour-by picker, template picker and feature toggles.
//
//   const c = createControls(host, {app, store, client, pane, config, show});
//   c.destroy();
// or the single widgets: colorControl(), extinctionControls(), templateControls() /
// templatePicker(), featureToggles(), modelControls() -> {el, update?(), destroy()}.
//
// IMPLEMENTATION_NOTES 75: this module holds the only implementation of the A_V / R_V sliders,
// the template picker and the feature-family toggles. panes/sed.js and panes/spectrum1d.js mount
// modelControls(); spec/features.js featureToggles(store, getDoc, opts) delegates to
// featureToggles() here. Accessible names are shared by every pane: the A_V range is
// 'A_V (mag)', the template select 'Model template', and feature chips carry `.pk-feat-chip`.
//
// Store keys written (source = pane id): color {by, cmap, lo?, hi?}, av, rv, dm (numbers; slider
// drags are coalesced to one store.set per animation frame), template {key, grid?, scale,
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
// circular with spec/features.js (it re-exports featureToggles from here); only function
// declarations cross the cycle, so evaluation order does not matter
import {familiesOf, familyColor} from '../spec/features.js';

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

/** Number-box text: at most `digits` decimals, trailing zeros dropped (2.500 -> 2.5). */
export function numText(v, digits) {
  const s = fmtNum(v, digits);
  return s.includes('.') && !/e/i.test(s) ? s.replace(/\.?0+$/, '') : s;
}

/** A `<span class="pk-ctl-name">` holding plain text or a fixed sub/superscript label. */
function nameSpan(label, html) {
  const s = el('span', 'pk-ctl-name');
  if (html) s.innerHTML = html;   // constant markup from this module only
  else s.textContent = label;
  return s;
}

function sliderRow({key, label, html, title, aria, min, max, step, value, digits, onInput}) {
  const row = el('label', `pk-ctl pk-ctl-${key} pk-models-${key}`);
  row.title = title;
  const name = nameSpan(label, html);
  const range = el('input', 'pk-ctl-range');
  range.type = 'range';
  range.min = String(min);
  range.max = String(max);
  range.step = String(step);
  range.value = String(value);
  range.setAttribute('aria-label', aria || title);
  const num = el('input', 'pk-ctl-num pk-num pk-models-num');
  num.type = 'number';
  num.min = String(min);
  num.step = String(step);
  num.inputMode = 'decimal';
  num.value = numText(value, digits);
  num.setAttribute('aria-label', `${label} value`);
  range.addEventListener('input', () => {
    const v = Number(range.value);
    num.value = numText(v, digits);
    onInput(v);
  });
  num.addEventListener('change', () => {
    const raw = num.value === '' ? NaN : Number(num.value);
    if (!Number.isFinite(raw)) {
      num.value = numText(Number(range.value), digits);   // back to the slider's value
      return;
    }
    // the box obeys the slider's minimum: a value below it is clamped and shown clamped
    const v = Math.max(Number(min), raw);
    if (v !== raw) num.value = numText(v, digits);
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
      if (document.activeElement !== num) num.value = numText(Number(v), digits);
    },
  };
}

/** R_V choices for a select: RV_CHOICES plus the current value when it is not one of them. */
export function rvChoices(cur) {
  const v = Number(cur);
  const r = Number.isFinite(v) && v > 0 ? v : 3.1;
  return RV_CHOICES.includes(r) ? RV_CHOICES.slice() : [...RV_CHOICES, r].sort((a, b) => a - b);
}

/** A_V value written by the slider: finite, >= 0, rounded to 1e-3 mag (null when invalid). */
export function cleanAv(v) {
  const x = Number(v);
  if (!Number.isFinite(x) || x < 0) return null;
  return Math.round(x * 1000) / 1000;
}

function rvRow(store, source) {
  const lab = el('label', 'pk-ctl pk-ctl-rv pk-models-rv');
  lab.title = 'Total-to-selective extinction R_V';
  const sel = el('select', 'pk-ctl-select');
  sel.setAttribute('aria-label', 'R_V');
  const fill = () => {
    const cur = Number(store.get('rv')) || 3.1;
    sel.textContent = '';
    for (const v of rvChoices(cur)) {
      const o = el('option', null, String(v));
      o.value = String(v);
      sel.append(o);
    }
    sel.value = String(cur);
  };
  fill();
  sel.addEventListener('change', () => store.set({rv: Number(sel.value)}, {source}));
  lab.append(nameSpan('R_V', 'R<sub>V</sub>'), sel);
  return {el: lab, set: fill};
}

/**
 * A_V / R_V / distance-modulus controls. opts: {store, pane, av = true, rv = true, dm = false,
 * avMax = AV_MAX, dmMax = DM_MAX}. Returns {el, update(), destroy()}.
 */
export function extinctionControls(opts) {
  const {store, pane} = opts;
  const root = el('div', 'pk-ctl-group pk-ctl-ext');
  const set = rafSetter(store, pane);
  const rows = {};
  if (opts.av !== false) {
    rows.av = sliderRow({key: 'av', label: 'A_V', html: 'A<sub>V</sub>', title: 'Visual extinction A_V (mag)', aria: 'A_V (mag)',
      min: 0, max: Math.max(opts.avMax || AV_MAX, Number(store.get('av')) || 0), step: 0.1,
      value: Number(store.get('av')) || 0, digits: 3, onInput: (v) => { const a = cleanAv(v); if (a !== null) set({av: a}); }});
    root.append(rows.av.el);
  }
  if (opts.rv !== false) {
    rows.rv = rvRow(store, pane);
    root.append(rows.rv.el);
  }
  if (opts.dm) {
    rows.dm = sliderRow({key: 'dm', label: 'DM', title: 'Distance modulus added to magnitudes (mag)', min: 0, max: Math.max(opts.dmMax || DM_MAX, Number(store.get('dm')) || 0), step: 0.05,
      value: Number(store.get('dm')) || 0, digits: 2, onInput: (v) => set({dm: v})});
    root.append(rows.dm.el);
  }
  const update = () => {
    for (const k of Object.keys(rows)) rows[k].set(k === 'av' || k === 'dm' ? Number(store.get(k)) || 0 : store.get(k));
  };
  const off = store.on(['av', 'rv', 'dm'], (changed, s, sources) => {
    if (sources && sources.size === 1 && sources.has(pane)) return;
    for (const k of changed) if (rows[k]) rows[k].set(k === 'rv' ? s.get(k) : Number(s.get(k)) || 0);
  });
  return {el: root, update, destroy() { off(); root.remove(); }};
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
 * Template list source for templateControls: {list(): [{key, label}], gridNames(): string[]}.
 * `templates` is a TemplateSet (or a Promise of one; data/templates.js); otherwise the
 * pk-templates/1 document `hub.json(ds)` is adapted. Resolves null when neither is given.
 */
export function templateSource({templates = null, hub = null, ds = null} = {}) {
  if (templates) return Promise.resolve(templates);
  if (!hub || !ds) return Promise.resolve(null);
  return Promise.resolve(hub.json(ds)).then((doc) => templateDocSource(doc));
}

/** {list(), gridNames()} over a pk-templates/1 document (null-safe). */
export function templateDocSource(doc) {
  const d = doc && typeof doc === 'object' ? doc : {};
  const list = (Array.isArray(d.templates) ? d.templates : []).filter((t) => t && t.key)
    .map((t) => ({key: String(t.key), label: t.label || String(t.key)}));
  const grids = Object.keys(d.grids && typeof d.grids === 'object' ? d.grids : {});
  return {list: () => list, gridNames: () => grids};
}

/**
 * Next store value of `template` after a widget edit (pure). `cur` is the current value,
 * `patch` holds the edited fields: key ('' or null clears the template), grid ('' = automatic),
 * bands (string[]; empty = automatic) and scale ('auto' or a positive number). Without a
 * current key, only a patch that sets a key creates a template.
 */
export function nextTemplate(cur, patch) {
  const t = cur && cur.key ? cur : null;
  if ('key' in patch) {
    if (!patch.key) return null;
  } else if (!t) {
    return null;
  }
  const merged = {...(t || {}), ...patch};
  const next = {key: String(merged.key)};
  if (merged.grid) next.grid = merged.grid;
  const sc = merged.scale;
  next.scale = sc === undefined || sc === null || sc === 'auto' || !(Number(sc) > 0) ? 'auto' : Number(sc);
  if (Array.isArray(merged.bands) && merged.bands.length) next.bands = merged.bands.slice();
  return next;
}

/** Parse the normalization-bands box: comma / space separated names (empty -> undefined). */
export function parseBands(text) {
  const list = String(text || '').split(/[,\s]+/).map((s) => s.trim()).filter(Boolean);
  return list.length ? list : undefined;
}

function labeled(cls, text, input) {
  const lab = el('label', `pk-ctl ${cls}`);
  lab.append(nameSpan(text), input);
  return lab;
}

/**
 * Model-template picker: template select ('Model template'), wavelength grid ('auto' plus the
 * set's grids), normalization bands and scale, bound to store key `template`.
 * opts: {store, pane|source, templates? (TemplateSet or Promise), hub?, ds? (templates dataset),
 * bandsPlaceholder?}. Returns {el, update(), destroy()}.
 */
export function templateControls(opts) {
  const {store} = opts;
  const source = opts.source || opts.pane;
  const root = el('div', 'pk-ctl-group pk-ctl-template');
  root.title = 'Model template drawn over spectra and SEDs';
  const tplSel = el('select', 'pk-ctl-select pk-template');
  tplSel.setAttribute('aria-label', 'Model template');
  const gridSel = el('select', 'pk-ctl-select pk-template-grid');
  gridSel.setAttribute('aria-label', 'Template wavelength grid');
  const bandsIn = el('input', 'pk-template-bands');
  bandsIn.type = 'text';
  bandsIn.size = 10;
  bandsIn.placeholder = opts.bandsPlaceholder || 'auto';
  bandsIn.setAttribute('aria-label', 'Normalization bands (comma separated filters or band labels; empty = automatic)');
  const scaleIn = el('input', 'pk-template-scale');
  scaleIn.type = 'text';
  scaleIn.size = 6;
  scaleIn.placeholder = 'auto';
  scaleIn.setAttribute('aria-label', 'Template scale (auto or a positive number)');
  root.append(labeled('pk-models-tpl', 'Template', tplSel), labeled('pk-models-grid', 'Grid', gridSel),
    labeled('pk-models-bands', 'Norm', bandsIn), labeled('pk-models-scale', 'Scale', scaleIn));

  let tset = null;
  let state = 'loading';
  let destroyed = false;
  const cur = () => store.get('template') || null;
  const write = (patch) => {
    const next = nextTemplate(cur(), patch);
    if (next === null && !('key' in patch)) return;
    store.set({template: next}, {source});
  };
  tplSel.addEventListener('change', () => write({key: tplSel.value, grid: gridSel.value || undefined}));
  gridSel.addEventListener('change', () => write({grid: gridSel.value || undefined}));
  bandsIn.addEventListener('change', () => write({bands: parseBands(bandsIn.value)}));
  scaleIn.addEventListener('change', () => {
    const s = scaleIn.value.trim();
    const v = Number(s);
    if (!s || s.toLowerCase() === 'auto') write({scale: 'auto'});
    else if (Number.isFinite(v) && v > 0) write({scale: v});
    else update();
  });

  function option(value, text) {
    const o = el('option', null, text);
    o.value = value;
    return o;
  }
  function update() {
    const t = cur();
    const list = tset ? tset.list() : [];
    const grids = tset ? tset.gridNames() : [];
    tplSel.textContent = '';
    tplSel.append(option('', state === 'ready' ? 'none' : state === 'error' ? 'unavailable' : 'loading…'));
    for (const e of list) tplSel.append(option(e.key, e.label || e.key));
    if (t && t.key && !list.some((e) => e.key === t.key)) tplSel.append(option(t.key, t.key));
    tplSel.value = t && t.key ? t.key : '';
    tplSel.disabled = state !== 'ready';
    gridSel.textContent = '';
    gridSel.append(option('', 'auto'));
    for (const g of grids) gridSel.append(option(g, g));
    gridSel.value = t && t.grid && grids.includes(t.grid) ? t.grid : '';
    gridSel.disabled = !t;
    bandsIn.disabled = !t;
    scaleIn.disabled = !t;
    if (document.activeElement !== bandsIn) bandsIn.value = t && Array.isArray(t.bands) ? t.bands.join(',') : '';
    if (document.activeElement !== scaleIn) scaleIn.value = t && typeof t.scale === 'number' ? String(t.scale) : '';
  }
  templateSource(opts).then((ts) => {
    if (destroyed) return;
    tset = ts;
    state = ts ? 'ready' : 'error';
    root.hidden = !ts;
    update();
  }, (err) => {
    if (destroyed) return;
    state = 'error';
    root.hidden = true;
    root.title = `Templates unavailable: ${err && err.message}`;
    console.warn('templates unavailable', err);
  });
  const off = store.on(['template'], () => { if (!destroyed) update(); });
  update();
  return {el: root, update, destroy() { destroyed = true; off(); root.remove(); }};
}

/** Template picker for the page's templates dataset (createControls). opts: {store, hub, pane, ds}. */
export function templatePicker(opts) {
  return templateControls({store: opts.store, pane: opts.pane, hub: opts.hub, ds: opts.ds});
}

/** `features` value for the set `on`: document order first, then unknown keys in set order. */
export function toggledFeatures(order, on) {
  const next = order.filter((k) => on.has(k));
  for (const k of on) if (!next.includes(k)) next.push(k);
  return next;
}

/**
 * Spectral-feature family toggles (pk-features/1) writing store key `features`, in the
 * document's family order so hashes are stable. The document comes from `getDoc()` (current
 * document or null while loading; call update() when it changes) or from `hub.json(ds)`.
 * opts: {store, pane|source, getDoc? | hub+ds, label?, heading?, className?}.
 * Returns {el, update(), destroy()}; el is hidden while there are no families.
 */
export function featureToggles(opts) {
  const {store} = opts;
  const source = opts.source || opts.pane || 'features';
  const root = el('div', `pk-feat-toggles${opts.className ? ` ${opts.className}` : ''}`);
  root.setAttribute('role', 'group');
  root.setAttribute('aria-label', opts.label || 'Spectral features');
  let box = root;
  if (opts.heading) {
    root.append(nameSpan(opts.heading));
    box = el('span', 'pk-ctl-chips');
    root.append(box);
  }
  let doc = null;
  let destroyed = false;
  const getDoc = typeof opts.getDoc === 'function' ? opts.getDoc : () => doc;
  let builtKey = null;
  // chips are rebuilt only when the family list changes; a `features` change flips
  // aria-pressed in place so a keyboard-toggled chip keeps focus
  function update() {
    const fams = familiesOf(getDoc());
    const on = new Set(store.get('features') || []);
    const key = JSON.stringify(fams.map((f) => [f.key, f.label, f.count, f.color || '', f.source || '']));
    if (key === builtKey) {
      for (const b of box.querySelectorAll('.pk-feat-chip')) {
        const p = on.has(b.dataset.family);
        b.setAttribute('aria-pressed', p ? 'true' : 'false');
        b.classList.toggle('pk-on', p);
      }
      return;
    }
    builtKey = key;
    box.textContent = '';
    root.hidden = !fams.length;
    fams.forEach((fam, i) => {
      const b = el('button', 'pk-btn pk-feat-chip', fam.label);
      b.type = 'button';
      b.title = `${fam.label}: ${fam.count} features${fam.source ? ` (${fam.source})` : ''}`;
      b.dataset.family = fam.key;
      b.setAttribute('aria-pressed', on.has(fam.key) ? 'true' : 'false');
      b.classList.toggle('pk-on', on.has(fam.key));
      b.style.setProperty('--pk-feat-color', familyColor(fam, i));
      b.addEventListener('click', () => {
        const cur = new Set(store.get('features') || []);
        if (cur.has(fam.key)) cur.delete(fam.key);
        else cur.add(fam.key);
        store.set({features: toggledFeatures(familiesOf(getDoc()).map((f) => f.key), cur)}, {source});
      });
      box.append(b);
    });
  }
  if (typeof opts.getDoc !== 'function' && opts.hub && opts.ds) {
    Promise.resolve(opts.hub.json(opts.ds)).then((d) => {
      if (destroyed) return;
      doc = d || null;
      update();
    }, (err) => {
      if (destroyed) return;
      root.title = err.message;
      root.hidden = true;
    });
  }
  const off = store.on(['features'], () => { if (!destroyed) update(); });
  update();
  return {el: root, update, destroy() { destroyed = true; if (typeof off === 'function') off(); root.remove(); }};
}

/**
 * Model controls (sed, spectrum1d): A_V slider and number box, R_V select and, when a template
 * source is given (opts.templates: TemplateSet or Promise, or opts.hub + opts.ds), the template
 * picker. ctx: {store, id}. opts: {source?, av?, rv?, dm?, avMax?, dmMax?, templates?, hub?, ds?,
 * bandsPlaceholder?}. Returns {el, update(), destroy()}.
 */
export function modelControls(ctx, opts = {}) {
  const store = ctx.store;
  const source = opts.source || ctx.id;
  const root = el('div', 'pk-models');
  root.setAttribute('role', 'group');
  root.setAttribute('aria-label', 'Extinction and model template');
  const parts = [extinctionControls({store, pane: source, av: opts.av !== false, rv: opts.rv !== false, dm: !!opts.dm,
    avMax: opts.avMax, dmMax: opts.dmMax})];
  if (opts.templates || (opts.hub && opts.ds)) {
    parts.push(templateControls({store, source, templates: opts.templates || null, hub: opts.hub, ds: opts.ds,
      bandsPlaceholder: opts.bandsPlaceholder}));
  }
  for (const p of parts) root.append(p.el);
  return {
    el: root,
    update() { for (const p of parts) p.update(); },
    destroy() { for (const p of parts) p.destroy(); root.remove(); },
  };
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
    if (ds) parts.push(featureToggles({store, hub, pane, ds, heading: 'Features', className: 'pk-ctl-group pk-ctl-features'}));
  }
  for (const p of parts) root.append(p.el);
  root.hidden = !parts.length;
  return {
    el: root,
    update() { for (const p of parts) if (typeof p.update === 'function') p.update(); },
    destroy() {
      for (const p of parts) p.destroy();
      root.remove();
    },
  };
}
