// Axis picker (selectable_axes, ARCHITECTURE.md 8.2): grouped <optgroup>s with counts,
// previous / next / play stepping, typed expressions and an axis options popover
// (log, flip, range).
//
//   const p = createAxisPicker({client, axis: 'x', label: 'x', value: {expr, log, inv, range},
//                               onChange(patch), play: true, typed: true, options: true});
//   host.append(p.el); p.set(value); p.stop(); p.destroy();
//
// onChange receives a partial patch: {expr} | {log} | {inv} | {range: [lo, hi] | null}.
// Typed expressions follow the section 4.4 grammar (data/expr.js); every base key must be a
// catalog column, otherwise the input is marked invalid and nothing is emitted.

import {isValid} from '../data/expr.js';

const NUMERIC_ROLES = new Set(['mag', 'flux', 'color', 'value', 'error', 'ra', 'dec', undefined, null, '']);
const NON_NUMERIC_ENC = new Set(['str', 'cat8', 'cat16', 'bits', 'id']);
const ROLE_GROUPS = {mag: 'Magnitudes', flux: 'Fluxes', color: 'Colours', error: 'Errors', ra: 'Position', dec: 'Position', value: 'Values'};
export const PLAY_MS = 1500;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

/** True for stored columns that can be an axis. */
export function isNumericColumn(c) {
  if (!c || !NUMERIC_ROLES.has(c.role)) return false;
  return !NON_NUMERIC_ENC.has(c.enc);
}

/** Display label of an expression ("label [unit]" for columns, the expression otherwise). */
export function exprLabel(client, expr) {
  if (!expr) return '';
  const m = client && client.col(expr);
  if (!m) return String(expr);
  const lab = m.label || m.key || expr;
  return m.unit && m.unit !== 'mag' && !m.virtual ? `${lab} [${m.unit}]` : lab;
}

/**
 * Grouped axis choices: [{key, label, items: [{key, label}]}]. Manifest `groups` (by key,
 * through each column's `group`) come first in manifest order, then role groups, then virtual
 * dims by their group.
 */
export function axisGroups(client, opts = {}) {
  const man = client.manifest || {};
  const exclude = new Set(opts.exclude || []);
  const groupLabel = new Map((man.groups || []).map((g) => [g.key, g.label || g.key]));
  const out = new Map();
  const add = (gkey, glabel, item) => {
    if (!out.has(gkey)) out.set(gkey, {key: gkey, label: glabel, items: []});
    const g = out.get(gkey);
    if (!g.items.some((it) => it.key === item.key)) g.items.push(item);
  };
  for (const g of man.groups || []) out.set(`g:${g.key}`, {key: `g:${g.key}`, label: g.label || g.key, items: []});
  for (const c of man.cols || []) {
    if (!isNumericColumn(c) || exclude.has(c.key)) continue;
    const item = {key: c.key, label: exprLabel(client, c.key)};
    if (c.group && groupLabel.has(c.group)) add(`g:${c.group}`, groupLabel.get(c.group), item);
    else if (c.group) add(`g:${c.group}`, c.group, item);
    else add(`r:${c.role || 'value'}`, ROLE_GROUPS[c.role] || 'Values', item);
  }
  if (man.pos && opts.position !== false) {
    add('r:ra', 'Position', {key: 'ra', label: 'RA'});
    add('r:ra', 'Position', {key: 'dec', label: 'Dec'});
  }
  for (const v of man.virtual || []) {
    if (exclude.has(v.key)) continue;
    const item = {key: v.key, label: v.label || v.key};
    if (v.group && groupLabel.has(v.group)) add(`g:${v.group}`, groupLabel.get(v.group), item);
    else add(`v:${v.group || 'derived'}`, v.group || 'Derived', item);
  }
  // groups listing dims explicitly (manifold groups) contribute their virtual members too
  for (const g of man.groups || []) {
    for (const d of g.dims || []) {
      if (exclude.has(d) || !client.col(d)) continue;
      const m = client.col(d);
      if (m && !m.virtual && !isNumericColumn(m)) continue;
      add(`g:${g.key}`, g.label || g.key, {key: d, label: exprLabel(client, d)});
    }
  }
  return Array.from(out.values()).filter((g) => g.items.length);
}

/** Validate a typed expression against the catalog: null when usable, else a message. */
export function checkExpr(client, expr) {
  const s = String(expr || '').trim();
  if (!s) return 'empty expression';
  if (client.col(s)) return null;
  if (!isValid(s)) return 'not a valid expression (keys, numbers, + − × ÷, parentheses)';
  const missing = client.baseKeys(s).filter((k) => !client.col(k));
  return missing.length ? `unknown column${missing.length > 1 ? 's' : ''}: ${missing.join(', ')}` : null;
}

/**
 * Build an axis picker. opts: {client, axis: 'x'|'y'|string, label?, value: {expr, log?, inv?,
 * range?}, onChange(patch), play = true, typed = true, options = true, exclude?: [keys],
 * groups?: from axisGroups}.
 */
export function createAxisPicker(opts) {
  const {client} = opts;
  const axis = opts.axis || 'x';
  let value = {expr: null, log: false, inv: false, range: null, ...(opts.value || {})};
  const groups = opts.groups || axisGroups(client, {exclude: opts.exclude});
  const flat = groups.flatMap((g) => g.items.map((it) => it.key));
  const root = el('div', 'pk-axis-picker');
  root.dataset.axis = axis;
  const lab = el('label', 'pk-axis-label', opts.label ?? axis);
  const sel = el('select', 'pk-axis-select');
  const selId = `pk-axis-${axis}-${Math.random().toString(36).slice(2, 8)}`;
  sel.id = selId;
  lab.htmlFor = selId;
  sel.setAttribute('aria-label', `${axis} axis`);
  const custom = el('option', 'pk-axis-custom');
  custom.hidden = true;
  sel.append(custom);
  for (const g of groups) {
    const og = el('optgroup');
    og.label = `${g.label} (${g.items.length})`;
    for (const it of g.items) {
      const o = el('option', null, it.label);
      o.value = it.key;
      if (it.label !== it.key) o.title = it.key;
      og.append(o);
    }
    sel.append(og);
  }
  root.append(lab, sel);

  const btn = (text, title, cls) => {
    const b = el('button', `pk-btn pk-icon-btn ${cls || ''}`.trim(), text);
    b.type = 'button';
    b.title = title;
    b.setAttribute('aria-label', title);
    return b;
  };
  let timer = null;
  const prev = btn('‹', `Previous ${axis} column`, 'pk-axis-prev');
  const next = btn('›', `Next ${axis} column`, 'pk-axis-next');
  const play = btn('▶', `Play through ${axis} columns`, 'pk-axis-play');
  if (opts.play !== false && flat.length > 1) {
    const grp = el('span', 'pk-btn-group pk-axis-step');
    grp.append(prev, next, play);
    root.append(grp);
  }

  let input = null;
  if (opts.typed !== false) {
    input = el('input', 'pk-axis-expr pk-mono');
    input.type = 'text';
    input.placeholder = 'expression';
    input.spellcheck = false;
    input.setAttribute('aria-label', `${axis} axis expression`);
    input.title = 'Type an expression such as f182m-f410m and press Enter';
    root.append(input);
  }

  let more = null, logBox = null, invBox = null, loIn = null, hiIn = null;
  if (opts.options !== false) {
    more = el('details', 'pk-axis-more');
    const sum = el('summary', 'pk-btn pk-icon-btn', '⋯');
    sum.title = `${axis} axis options`;
    const panel = el('div', 'pk-axis-more-panel');
    const mk = (text) => {
      const l = el('label', 'pk-axis-opt');
      const b = el('input');
      b.type = 'checkbox';
      l.append(b, document.createTextNode(` ${text}`));
      panel.append(l);
      return b;
    };
    logBox = mk('log');
    invBox = mk('flip');
    const rng = el('div', 'pk-axis-range');
    loIn = el('input', 'pk-num');
    hiIn = el('input', 'pk-num');
    for (const [inp, ph] of [[loIn, 'min'], [hiIn, 'max']]) {
      inp.type = 'number';
      inp.step = 'any';
      inp.placeholder = ph;
      inp.setAttribute('aria-label', `${axis} ${ph}`);
    }
    rng.append(loIn, el('span', null, '–'), hiIn);
    const reset = btn('auto', 'Automatic range', 'pk-axis-auto');
    reset.classList.remove('pk-icon-btn');
    rng.append(reset);
    panel.append(rng);
    more.append(sum, panel);
    root.append(more);
    logBox.addEventListener('change', () => emit({log: logBox.checked}));
    invBox.addEventListener('change', () => emit({inv: invBox.checked}));
    const rangeChange = () => {
      const lo = loIn.value === '' ? null : Number(loIn.value);
      const hi = hiIn.value === '' ? null : Number(hiIn.value);
      if (lo === null && hi === null) emit({range: null});
      else if (Number.isFinite(lo) && Number.isFinite(hi) && lo !== hi) emit({range: [lo, hi]});
    };
    loIn.addEventListener('change', rangeChange);
    hiIn.addEventListener('change', rangeChange);
    reset.addEventListener('click', () => { loIn.value = ''; hiIn.value = ''; emit({range: null}); });
  }

  function emit(patch) {
    value = {...value, ...patch};
    if (opts.onChange) opts.onChange(patch);
  }

  function sync() {
    const e = value.expr;
    const known = e != null && flat.includes(e);
    custom.hidden = known || e == null;
    if (!known && e != null) {
      custom.value = e;
      custom.textContent = exprLabel(client, e);
    }
    sel.value = e == null ? '' : e;
    if (input && document.activeElement !== input) {
      input.value = known ? '' : (e || '');
      input.classList.remove('pk-invalid');
    }
    if (logBox) logBox.checked = !!value.log;
    if (invBox) invBox.checked = !!value.inv;
    if (loIn && document.activeElement !== loIn) loIn.value = value.range ? String(value.range[0]) : '';
    if (hiIn && document.activeElement !== hiIn) hiIn.value = value.range ? String(value.range[1]) : '';
    const i = flat.indexOf(e);
    prev.disabled = flat.length < 2;
    next.disabled = flat.length < 2;
    root.dataset.index = String(i);
  }

  function step(d) {
    if (!flat.length) return;
    const i = flat.indexOf(value.expr);
    const j = i < 0 ? (d > 0 ? 0 : flat.length - 1) : (i + d + flat.length) % flat.length;
    emit({expr: flat[j], range: null});
    sync();
  }

  function stop() {
    if (timer) clearInterval(timer);
    timer = null;
    play.textContent = '▶';
    play.classList.remove('pk-active');
    play.setAttribute('aria-pressed', 'false');
  }

  sel.addEventListener('change', () => {
    stop();
    if (sel.value && sel.value !== value.expr) emit({expr: sel.value, range: null});
    sync();
  });
  prev.addEventListener('click', () => { stop(); step(-1); });
  next.addEventListener('click', () => { stop(); step(1); });
  play.addEventListener('click', () => {
    if (timer) { stop(); return; }
    play.textContent = '❚❚';
    play.classList.add('pk-active');
    play.setAttribute('aria-pressed', 'true');
    step(1);
    timer = setInterval(() => step(1), PLAY_MS);
  });
  if (input) {
    input.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      const s = input.value.trim();
      const err = checkExpr(client, s);
      input.classList.toggle('pk-invalid', !!err);
      input.title = err || 'Type an expression such as f182m-f410m and press Enter';
      if (err) return;
      stop();
      emit({expr: s, range: null});
      input.blur();
      sync();
    });
    input.addEventListener('input', () => input.classList.remove('pk-invalid'));
  }
  sync();

  return {
    el: root,
    get value() { return {...value}; },
    set(v) { value = {...value, ...(v || {})}; sync(); },
    stop,
    destroy() { stop(); root.remove(); },
  };
}
