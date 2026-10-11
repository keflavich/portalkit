// Filter and selection chips (filter_toggles, ARCHITECTURE.md 8.2 / 8.3).
//
//   const chips = createChips(host, {app, store, client, ds, pane, exclude, show});
//   chips.refresh(); chips.destroy();
//
// Rows (each shown only when the catalog has something for it):
//   category  one chip per level of every category column with <= MAX_LEVELS levels (except
//             `exclude`, normally the legend's colour-by key) with counts; a click toggles the
//             level in filters:<ds>.cats[key]; alt-click shows only that level. A column's
//             `default_off` levels are applied by the filter owner (data/hub.js, when the
//             catalog opens; IMPLEMENTATION_NOTES 70); the chips only render the filter state and
//             toggle with legend.toggleLevel(..., {defaultOff: hasDefaultOff(meta)}) (note 104).
//   flag      tri-state chip per flag column: any -> 1 -> 0 -> any (filters:<ds>.flags[key])
//   S/N       "S/N ≥ k" chip cycling off -> 3 -> 5 -> 10 -> off, scoped to the active projection
//             (filters:<ds>.snr = {min, scope: 'projection'}; hub.js derives the bands)
//   presets   manifest presets of kind 'selection' (preselect buttons, op from the modifier
//             keys, RecipeOp kind 'preset') and 'filter' (toggle the preset's filters)
//   saved     named selections kept per viewer in localStorage; "Save" stores the current
//             selection (ids / bits / recipe), a chip restores it, × forgets it. Emits
//             'selection:saved' {ds, name}.

import {opFromEvent, Selection, unpackBits} from '../core/selection.js';
import {fmtCount, plural} from '../core/fmt.js';
import {IDS_IN_HASH_MAX} from '../core/constants.js';
import {hasDefaultOff, isCategory, toggleLevel} from './legend.js';

export const MAX_LEVELS = 16;
export const MAX_CAT_COLUMNS = 4;
export const SNR_STEPS = Object.freeze([0, 3, 5, 10]);

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function chip(label, {title, on = false, cls = ''} = {}) {
  const b = el('button', `pk-chip ${cls}`.trim(), label);
  b.type = 'button';
  if (title) b.title = title;
  b.classList.toggle('pk-on', !!on);
  b.setAttribute('aria-pressed', on ? 'true' : 'false');
  return b;
}

function storageKey(app, ds) {
  const portal = (app && app.portalId) || (app && app.page && app.page.portal && app.page.portal.id) || 'pk';
  const page = (app && app.page && app.page.page && app.page.page.id) || 'page';
  return `pk:saved:${portal}:${page}:${ds}`;
}

function readSaved(key) {
  let raw = null;
  try {
    raw = window.localStorage.getItem(key);
  } catch (err) {
    if (err instanceof DOMException) return [];   // storage blocked (private mode, sandbox)
    throw err;
  }
  if (!raw) return [];
  let list;
  try {
    list = JSON.parse(raw);
  } catch (err) {
    if (err instanceof SyntaxError) return [];
    throw err;
  }
  return Array.isArray(list) ? list.filter((e) => e && typeof e.name === 'string' && typeof e.sel === 'string') : [];
}

function writeSaved(key, list) {
  try {
    window.localStorage.setItem(key, JSON.stringify(list));
    return true;
  } catch (err) {
    if (err instanceof DOMException) return false;   // quota or storage blocked
    throw err;
  }
}

/** Category columns shown as chips. */
export function chipColumns(client, exclude = null) {
  const cols = (client.manifest.cols || []).filter((c) => isCategory(c) && c.levels.length <= MAX_LEVELS && c.key !== exclude);
  return cols.slice(0, MAX_CAT_COLUMNS);
}

/** Flag columns shown as tri-state chips. */
export function flagColumns(client) {
  return (client.manifest.cols || []).filter((c) => c.role === 'flag').slice(0, 8);
}

/** Next S/N threshold after `cur` in SNR_STEPS (wrapping to 0). */
export function nextSnr(cur) {
  const i = SNR_STEPS.indexOf(Number(cur) || 0);
  return SNR_STEPS[(i + 1) % SNR_STEPS.length];
}

/**
 * Mount the chip rows into `host`. opts: {app, store, client, ds, pane, exclude?: () => key,
 * show?: {cats, flags, snr, presets, saved}}. Returns {el, refresh(), destroy()}.
 */
export function createChips(host, opts) {
  const {app, store, client, ds, pane} = opts;
  const show = {cats: true, flags: true, snr: true, presets: true, saved: true, ...(opts.show || {})};
  const root = el('div', 'pk-chips');
  host.append(root);
  let destroyed = false;
  const catCounts = new Map();   // key -> Uint32Array
  const skey = storageKey(app, ds);
  let saving = false;

  const filters = () => store.get(`filters:${ds}`) || {};
  const setFilters = (f) => store.set({[`filters:${ds}`]: f}, {source: pane});

  async function loadCounts(c) {
    const L = c.levels.length;
    const h = await client.hist(c.key, L, {range: [-0.5, L - 0.5], mask: null});
    if (destroyed) return;
    catCounts.set(c.key, h.counts);
    render();
  }

  function catRow(c) {
    const row = el('div', 'pk-chip-row');
    row.append(el('span', 'pk-chip-label', c.label || c.key));
    const off = new Set((filters().cats && filters().cats[c.key]) || []);
    const counts = catCounts.get(c.key);
    c.levels.forEach((lv, i) => {
      const on = !off.has(lv);
      const b = chip('', {on, title: `${on ? 'Hide' : 'Show'} ${c.label || c.key} = ${lv} (alt-click: only this)`});
      b.append(el('span', 'pk-chip-text', String(lv)));
      if (counts) b.append(el('span', 'pk-chip-n pk-num', fmtCount(counts[i] || 0)));
      b.classList.toggle('pk-muted', !on);
      b.addEventListener('click', (e) => {
        // defaultOff: re-showing every level of a default_off column keeps cats[key] = []
        // (explicit "all levels on", IMPLEMENTATION_NOTES 104) so a reload does not hide them again
        setFilters(toggleLevel(filters(), c.key, lv, {only: e.altKey, levels: c.levels, defaultOff: hasDefaultOff(c)}));
        render();
      });
      row.append(b);
    });
    if (!counts) {
      loadCounts(c).catch((err) => {
        if (err && err.name === 'AbortError') return;
        store.emit('toast', {level: 'warn', text: `Counts for ${c.key} unavailable: ${err.message}`});
      });
    }
    return row;
  }

  function flagRow(cols) {
    const row = el('div', 'pk-chip-row');
    row.append(el('span', 'pk-chip-label', 'Flags'));
    const fl = filters().flags || {};
    for (const c of cols) {
      const v = Object.prototype.hasOwnProperty.call(fl, c.key) ? fl[c.key] : null;
      const text = v === null ? (c.label || c.key) : `${c.label || c.key}${v ? '' : ' ✗'}`;
      const b = chip(text, {on: v !== null, title: `${c.label || c.key}: ${v === null ? 'any' : v ? 'only flagged' : 'only unflagged'} (click to cycle)`});
      b.dataset.state = v === null ? 'any' : String(v);
      b.addEventListener('click', () => {
        const f = {...filters()};
        const flags = {...(f.flags || {})};
        if (v === null) flags[c.key] = 1;
        else if (v === 1) flags[c.key] = 0;
        else delete flags[c.key];
        if (Object.keys(flags).length) f.flags = flags;
        else delete f.flags;
        setFilters(f);
        render();
      });
      row.append(b);
    }
    return row;
  }

  function snrChip() {
    const cur = (filters().snr && Number(filters().snr.min)) || 0;
    const b = chip(cur ? `S/N ≥ ${cur}` : 'S/N', {on: cur > 0, title: 'Tighten the S/N cut on the bands of the active projection (click to cycle)', cls: 'pk-snr-chip'});
    b.addEventListener('click', () => {
      const nx = nextSnr(cur);
      const f = {...filters()};
      if (nx > 0) f.snr = {min: nx, scope: 'projection'};
      else delete f.snr;
      setFilters(f);
      render();
    });
    return b;
  }

  async function presetHit(p) {
    const n = client.n;
    const parsed = Selection.parse(p.sel || '');
    if (!parsed) throw new Error(`preset ${p.key} has no readable selection`);
    if (parsed.kind === 'ids') {
      const idx = await app.resolveIds(ds, parsed.ids);
      const hit = new Uint8Array(n);
      let missing = 0;
      for (const i of idx) {
        if (i >= 0) hit[i] = 1;
        else missing++;
      }
      if (missing) store.emit('toast', {level: 'warn', text: `${missing} ${plural(missing, 'source')} of “${p.label || p.key}” not found.`});
      return hit;
    }
    if (parsed.kind === 'bits') return unpackBits(parsed.bytes, n);
    return null;
  }

  async function applyPreset(p, e) {
    const sel = await app.selectionFor(ds);
    const op = opFromEvent(e);
    const hit = await presetHit(p);
    const recipe = {op, kind: 'preset', view: {ds}, key: p.key};
    if (hit) {
      sel.apply(hit, op, undefined, recipe);
    } else {
      // recipe-encoded preset: boot replays it (replace only)
      await app.applySelection(ds, p.sel);
    }
    store.emit('brush:commit', {pane, ds, shape: {kind: 'preset', key: p.key}, op, count: sel.count});
  }

  function filterPresetActive(p) {
    const want = p.filters || {};
    const f = filters();
    return Object.keys(want).length > 0 && Object.keys(want).every((k) => JSON.stringify(f[k]) === JSON.stringify(want[k]));
  }

  function presetsRow(list) {
    const row = el('div', 'pk-chip-row');
    row.append(el('span', 'pk-chip-label', 'Preselect'));
    for (const p of list) {
      if (p.kind === 'selection') {
        const b = chip(p.label || p.key, {title: `Select ${p.label || p.key} (shift: add, alt: subtract)`, cls: 'pk-preset-chip'});
        b.addEventListener('click', (e) => {
          applyPreset(p, e).catch((err) => store.emit('toast', {level: 'error', text: `Preset failed: ${err.message}`}));
        });
        row.append(b);
      } else if (p.kind === 'filter') {
        const on = filterPresetActive(p);
        const b = chip(p.label || p.key, {on, title: `${on ? 'Remove' : 'Apply'} filter “${p.label || p.key}”`, cls: 'pk-preset-chip'});
        b.addEventListener('click', () => {
          const f = {...filters()};
          if (on) for (const k of Object.keys(p.filters || {})) delete f[k];
          else Object.assign(f, p.filters || {});
          setFilters(f);
          render();
        });
        row.append(b);
      }
    }
    return row;
  }

  async function serializeCurrent() {
    const sel = await app.selectionFor(ds);
    if (!sel.count) return '';
    if (sel.count <= IDS_IN_HASH_MAX) {
      const idx = Array.from(sel.indices());
      const ids = await Promise.all(idx.map((i) => app.idOf(ds, i)));
      const map = new Map(idx.map((i, k) => [i, ids[k]]));
      return sel.serialize((i) => map.get(i));
    }
    return sel.serialize(undefined);
  }

  function savedRow() {
    const row = el('div', 'pk-chip-row pk-saved-row');
    row.append(el('span', 'pk-chip-label', 'Saved'));
    const list = readSaved(skey);
    for (const s of list) {
      const wrap = el('span', 'pk-chip-group');
      const b = chip(s.name, {title: `Restore selection “${s.name}” (${fmtCount(s.count || 0)})`, cls: 'pk-saved-chip'});
      b.addEventListener('click', () => {
        app.applySelection(ds, s.sel).catch((err) => store.emit('toast', {level: 'error', text: `Could not restore “${s.name}”: ${err.message}`}));
      });
      const x = el('button', 'pk-chip pk-chip-x', '×');
      x.type = 'button';
      x.title = `Forget “${s.name}”`;
      x.setAttribute('aria-label', `Forget ${s.name}`);
      x.addEventListener('click', () => {
        writeSaved(skey, readSaved(skey).filter((e) => e.name !== s.name));
        render();
      });
      wrap.append(b, x);
      row.append(wrap);
    }
    const selState = store.get(`sel:${ds}`) || {};
    if (saving) {
      const input = el('input', 'pk-chip-input');
      input.type = 'text';
      input.placeholder = 'name';
      input.setAttribute('aria-label', 'Name for the saved selection');
      const commit = async () => {
        const name = input.value.trim();
        saving = false;
        if (!name) { render(); return; }
        const str = await serializeCurrent();
        if (!str) {
          store.emit('toast', {level: 'warn', text: 'This selection cannot be saved (too large to encode).'});
          render();
          return;
        }
        const next = readSaved(skey).filter((e) => e.name !== name);
        next.push({name, sel: str, count: selState.count || 0, t: Date.now()});
        if (!writeSaved(skey, next)) store.emit('toast', {level: 'warn', text: 'Browser storage is unavailable; the selection was not saved.'});
        else store.emit('selection:saved', {ds, name});
        render();
      };
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); commit().catch((err) => store.emit('toast', {level: 'error', text: err.message})); }
        if (e.key === 'Escape') { e.stopPropagation(); saving = false; render(); }
      });
      row.append(input);
      queueMicrotask(() => input.focus());
    } else {
      const save = chip('+ Save', {title: 'Save the current selection under a name (kept in this browser)', cls: 'pk-save-chip'});
      save.disabled = !selState.count;
      save.addEventListener('click', () => { saving = true; render(); });
      row.append(save);
    }
    return row;
  }

  function render() {
    if (destroyed) return;
    root.textContent = '';
    const ex = opts.exclude ? opts.exclude() : null;
    if (show.cats) for (const c of chipColumns(client, ex)) root.append(catRow(c));
    const flags = show.flags ? flagColumns(client) : [];
    const hasErr = (client.manifest.cols || []).some((c) => c.err);
    if (flags.length || (show.snr && hasErr)) {
      const row = flags.length ? flagRow(flags) : el('div', 'pk-chip-row');
      if (!flags.length) row.append(el('span', 'pk-chip-label', 'Quality'));
      if (show.snr && hasErr) row.append(snrChip());
      root.append(row);
    }
    const presets = show.presets ? (client.manifest.presets || []).filter((p) => p.kind === 'selection' || p.kind === 'filter') : [];
    if (presets.length) root.append(presetsRow(presets));
    if (show.saved && app && app.page && !(app.page.datasets[ds] || {}).context) root.append(savedRow());
    root.hidden = !root.childElementCount;
  }

  const offStore = store.on([`filters:${ds}`, `sel:${ds}`, 'color'], (changed, s, sources) => {
    if (saving) return;
    if (sources.size === 1 && sources.has(pane) && !changed.has(`sel:${ds}`)) return;
    render();
  });
  render();

  return {
    el: root,
    refresh: render,
    destroy() {
      destroyed = true;
      offStore();
      root.remove();
    },
  };
}
