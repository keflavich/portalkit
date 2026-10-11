// Selection export and import (capability selection_export, ARCHITECTURE.md section 8.2).
//
// Export: CSV, JSON, ID list and DS9 regions of the selection, the filtered set or all rows,
// through DataClient.export (quantized values carry the decimals their step supports; exact
// row chunks are written in full precision when requested).
// Import: a pasted or loaded list resolves each entry by id, then alias (app.resolveIds), then
// by position within 0.3 arcsec (DataClient.crossmatch); matched rows are applied to the
// selection with a set operation.
//
// The parsing and resolution helpers at the top are pure (no DOM) and covered by
// tests/js/export.test.mjs. DOM work happens only inside the dialog functions.

import {parseCSV} from '../workers/ops/export.js';
import {parseCoords} from '../workers/ops/find.js';
import {fmtCount, plural} from '../core/fmt.js';

/** Default positional match radius for imports (arcsec). */
export const IMPORT_RADIUS_ARCSEC = 0.3;

/** Largest import accepted in one go (entries). */
export const IMPORT_MAX_ENTRIES = 200000;

export const EXPORT_FORMATS = Object.freeze([
  {key: 'csv', label: 'CSV', ext: 'csv'},
  {key: 'json', label: 'JSON', ext: 'json'},
  {key: 'ids', label: 'ID list', ext: 'txt'},
  {key: 'ds9', label: 'DS9 regions', ext: 'reg'},
]);

export const SCOPES = Object.freeze(['sel', 'vis', 'all']);

const ID_HEADERS = ['id', '_id', 'name', 'source', 'source_id', 'sourceid', 'srcid', 'src', 'object',
  'target', 'designation', 'identifier', 'alias'];
const RA_HEADERS = ['ra', 'raj2000', '_raj2000', 'ra_deg', 'radeg', 'ra_icrs', 'ra_j2000', 'alpha', 'ra(deg)'];
const DEC_HEADERS = ['dec', 'de', 'dej2000', '_dej2000', 'decj2000', 'dec_deg', 'decdeg', 'dec_icrs', 'de_icrs',
  'dec_j2000', 'delta', 'dec(deg)'];

function headerIndex(cells, names) {
  const low = cells.map((c) => String(c).trim().toLowerCase());
  for (const n of names) {
    const k = low.indexOf(n);
    if (k >= 0) return k;
  }
  return -1;
}

function isNumText(s) {
  return /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(String(s).trim());
}

/** Two cells (RA, Dec) -> {ra, dec} in degrees, or null. Accepts decimal and sexagesimal. */
export function parsePosition(raText, decText) {
  const a = String(raText ?? '').trim().replace(/−/g, '-');
  const d = String(decText ?? '').trim().replace(/−/g, '-');
  if (!a || !d) return null;
  if (isNumText(a) && isNumText(d)) {
    const ra = Number(a), dec = Number(d);
    if (!Number.isFinite(ra) || !Number.isFinite(dec)) return null;
    if (ra < 0 || ra >= 360 || dec < -90 || dec > 90) return null;
    return {ra, dec};
  }
  const c = parseCoords(`${a} ${d}`);
  return c ? {ra: c.ra, dec: c.dec} : null;
}

function cleanId(s) {
  return String(s ?? '').replace(/^﻿/, '').trim();
}

function splitTSV(text) {
  return String(text).split(/\r\n|\r|\n/).map((l) => l.split('\t'));
}

function parseRegionLine(line) {
  const m = /^\s*(?:fk5;\s*|icrs;\s*|j2000;\s*)?(?:circle|point|box|ellipse)\s*\(\s*([^,()]+?)\s*,\s*([^,()]+?)\s*(?:,|\))/i.exec(line);
  if (!m) return null;
  const pos = parsePosition(m[1], m[2]);
  if (!pos) return null;
  const t = /text\s*=\s*\{([^}]*)\}/i.exec(line) || /text\s*=\s*"([^"]*)"/i.exec(line);
  const out = {ra: pos.ra, dec: pos.dec};
  if (t && cleanId(t[1])) out.id = cleanId(t[1]);
  return out;
}

function fromObjects(arr) {
  const entries = [];
  let skipped = 0;
  arr.forEach((o, k) => {
    if (typeof o === 'string' || typeof o === 'number') {
      const id = cleanId(o);
      if (id) entries.push({id, line: k + 1});
      else skipped++;
      return;
    }
    if (!o || typeof o !== 'object') { skipped++; return; }
    const keys = Object.keys(o);
    const ik = headerIndex(keys, ID_HEADERS);
    const rk = headerIndex(keys, RA_HEADERS);
    const dk = headerIndex(keys, DEC_HEADERS);
    const e = {line: k + 1};
    if (ik >= 0 && o[keys[ik]] !== null && o[keys[ik]] !== undefined && cleanId(o[keys[ik]])) e.id = cleanId(o[keys[ik]]);
    if (rk >= 0 && dk >= 0) {
      const p = parsePosition(o[keys[rk]], o[keys[dk]]);
      if (p) Object.assign(e, p);
    }
    if (e.id !== undefined || e.ra !== undefined) entries.push(e);
    else skipped++;
  });
  return {entries, skipped};
}

/**
 * Parse import text into entries [{id?, ra?, dec?, line}].
 * Recognised inputs, checked in order:
 *   - JSON: an array of ids or of objects with id / ra / dec style keys
 *   - DS9 regions: circle(ra,dec,...) lines with optional `# text={id}`
 *   - CSV / TSV with a header naming an id column and/or ra + dec columns
 *   - plain lines: a coordinate pair ("ra dec", decimal or sexagesimal) or an id; a single
 *     line with commas is read as a comma-separated id list
 * Comment lines starting with '#' and blank lines are ignored.
 * Returns {entries, format: 'json'|'ds9'|'csv'|'lines'|'empty', skipped}.
 */
export function parseImportText(text) {
  const s = String(text ?? '').replace(/^﻿/, '');
  if (!s.trim()) return {entries: [], format: 'empty', skipped: 0};
  const trimmed = s.trim();
  if (trimmed.startsWith('[') || trimmed.startsWith('{')) {
    let doc;
    try {
      doc = JSON.parse(trimmed);
    } catch (err) {
      if (!(err instanceof SyntaxError)) throw err;
      doc = undefined;
    }
    if (doc !== undefined) {
      const arr = Array.isArray(doc) ? doc : Array.isArray(doc.ids) ? doc.ids : Array.isArray(doc.items) ? doc.items : null;
      if (arr) {
        const r = fromObjects(arr);
        return {entries: dedupe(r.entries), format: 'json', skipped: r.skipped};
      }
    }
  }
  const lines = s.split(/\r\n|\r|\n/);
  const content = lines.map((l, k) => ({l, k})).filter(({l}) => l.trim() && !l.trim().startsWith('#'));
  if (!content.length) return {entries: [], format: 'empty', skipped: 0};
  // DS9 regions
  const regionish = content.filter(({l}) => /^\s*(?:fk5;\s*|icrs;\s*|j2000;\s*)?(?:circle|point|box|ellipse)\s*\(/i.test(l));
  if (regionish.length && regionish.length >= content.filter(({l}) => !/^\s*(?:global|fk5|icrs|j2000|image|physical)\b/i.test(l)).length) {
    const entries = [];
    let skipped = 0;
    for (const {l, k} of regionish) {
      const e = parseRegionLine(l);
      if (e) entries.push(Object.assign(e, {line: k + 1}));
      else skipped++;
    }
    return {entries: dedupe(entries), format: 'ds9', skipped};
  }
  // CSV / TSV with a header
  const first = content[0].l;
  const tsv = first.includes('\t') && !first.includes(',');
  const rows = tsv ? splitTSV(content.map(({l}) => l).join('\n')) : parseCSV(content.map(({l}) => l).join('\n'));
  const head = rows[0] || [];
  const ik = headerIndex(head, ID_HEADERS);
  const rk = headerIndex(head, RA_HEADERS);
  const dk = headerIndex(head, DEC_HEADERS);
  // A header row: several columns naming an id or RA/Dec column, or a one-column list whose
  // first line is an id header ("id", "name", ...).
  const oneColHeader = head.length === 1 && ik === 0 && rows.length > 1 && rows.every((r) => r.length <= 1);
  if ((head.length > 1 && (ik >= 0 || (rk >= 0 && dk >= 0))) || oneColHeader) {
    const entries = [];
    let skipped = 0;
    for (let r = 1; r < rows.length; r++) {
      const cells = rows[r];
      if (!cells.length || (cells.length === 1 && !cells[0].trim())) continue;
      const e = {line: content[r] ? content[r].k + 1 : r + 1};
      if (ik >= 0 && cleanId(cells[ik])) e.id = cleanId(cells[ik]);
      if (rk >= 0 && dk >= 0) {
        const p = parsePosition(cells[rk], cells[dk]);
        if (p) Object.assign(e, p);
      }
      if (e.id !== undefined || e.ra !== undefined) entries.push(e);
      else skipped++;
    }
    return {entries: dedupe(entries), format: 'csv', skipped};
  }
  // Plain lines
  const entries = [];
  let skipped = 0;
  if (content.length === 1 && /,/.test(first) && !parseCoords(first.trim())) {
    for (const c of (parseCSV(first)[0] || [])) {
      const id = cleanId(c);
      if (id) entries.push({id, line: content[0].k + 1});
    }
    return {entries: dedupe(entries), format: 'lines', skipped};
  }
  for (const {l, k} of content) {
    const t = l.trim();
    const c = parseCoords(t);
    if (c) {
      entries.push({ra: c.ra, dec: c.dec, line: k + 1});
      continue;
    }
    let id = t;
    if (/[,\t]/.test(t)) {
      const cells = /\t/.test(t) && !/,/.test(t) ? t.split('\t') : (parseCSV(t)[0] || []);
      id = cleanId(cells[0]);
      if (cells.length >= 3) {
        const p = parsePosition(cells[1], cells[2]);
        if (p) {
          entries.push(id ? {id, ra: p.ra, dec: p.dec, line: k + 1} : {ra: p.ra, dec: p.dec, line: k + 1});
          continue;
        }
      }
      // A headerless multi-column line without a position: every cell is an id.
      let any = false;
      for (const c of cells) {
        const ci = cleanId(c);
        if (ci) {
          entries.push({id: ci, line: k + 1});
          any = true;
        }
      }
      if (!any) skipped++;
      continue;
    }
    if (id) entries.push({id, line: k + 1});
    else skipped++;
  }
  return {entries: dedupe(entries), format: 'lines', skipped};
}

function dedupe(entries) {
  const seen = new Set();
  const out = [];
  for (const e of entries) {
    const key = e.id !== undefined ? `i:${e.id}` : `p:${e.ra.toFixed(8)},${e.dec.toFixed(8)}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(e);
  }
  return out.length > IMPORT_MAX_ENTRIES ? out.slice(0, IMPORT_MAX_ENTRIES) : out;
}

/**
 * Resolve import entries to stored row indices.
 *   deps.resolveIds(ids: string[]) -> Promise<number[]>    (id exact, then alias; -1 = not found)
 *   deps.crossmatch(ra: Float64Array, dec: Float64Array, rArcsec) -> Promise<Int32Array> | null
 *   deps.n: number of rows (indices outside [0, n) are dropped)
 * Order: id/alias first; entries whose id is missing or unknown fall back to their position.
 * Returns {indices: Int32Array (unique, input order), matched: [{entry, i, how}],
 *          missing: [entry], byId, byPosition}.
 */
export async function resolveImport(entries, deps = {}) {
  const list = Array.isArray(entries) ? entries : [];
  const n = Number.isFinite(deps.n) ? deps.n : Infinity;
  const r = Number.isFinite(+deps.radiusArcsec) && +deps.radiusArcsec > 0 ? +deps.radiusArcsec : IMPORT_RADIUS_ARCSEC;
  const ok = (i) => Number.isInteger(i) && i >= 0 && i < n;
  const hit = new Array(list.length).fill(-1);
  const how = new Array(list.length).fill(null);
  const withId = [];
  list.forEach((e, k) => { if (e && e.id !== undefined && e.id !== null && String(e.id) !== '') withId.push(k); });
  if (withId.length && typeof deps.resolveIds === 'function') {
    const res = await deps.resolveIds(withId.map((k) => String(list[k].id)));
    withId.forEach((k, j) => {
      const i = res ? res[j] : -1;
      if (ok(i)) { hit[k] = i; how[k] = 'id'; }
    });
  }
  const posK = [];
  list.forEach((e, k) => {
    if (hit[k] < 0 && e && Number.isFinite(e.ra) && Number.isFinite(e.dec)) posK.push(k);
  });
  if (posK.length && typeof deps.crossmatch === 'function') {
    const ra = Float64Array.from(posK, (k) => list[k].ra);
    const dec = Float64Array.from(posK, (k) => list[k].dec);
    const res = await deps.crossmatch(ra, dec, r);
    if (res) {
      posK.forEach((k, j) => {
        const i = res[j];
        if (ok(i)) { hit[k] = i; how[k] = 'position'; }
      });
    }
  }
  const seen = new Set();
  const idx = [];
  const matched = [];
  const missing = [];
  let byId = 0, byPosition = 0;
  list.forEach((e, k) => {
    const i = hit[k];
    if (i < 0) { missing.push(e); return; }
    matched.push({entry: e, i, how: how[k]});
    if (how[k] === 'id') byId++; else byPosition++;
    if (!seen.has(i)) { seen.add(i); idx.push(i); }
  });
  return {indices: Int32Array.from(idx), matched, missing, byId, byPosition};
}

/** Label of an import entry for messages ("J1234", "266.41000, -29.00800"). */
export function entryLabel(e) {
  if (!e) return '';
  if (e.id !== undefined) return String(e.id);
  if (Number.isFinite(e.ra) && Number.isFinite(e.dec)) return `${e.ra.toFixed(5)}, ${e.dec.toFixed(5)}`;
  return `line ${e.line ?? '?'}`;
}

/** Human summary of a resolveImport result. */
export function importSummary(res, total) {
  const m = res.matched.length;
  const parts = [`Matched ${fmtCount(m)} of ${plural(total, 'entry', 'entries')}`];
  const how = [];
  if (res.byId) how.push(`${fmtCount(res.byId)} by id or alias`);
  if (res.byPosition) how.push(`${fmtCount(res.byPosition)} by position`);
  if (how.length) parts[0] += ` (${how.join(', ')})`;
  if (res.indices.length !== m) parts.push(plural(res.indices.length, 'distinct row', 'distinct rows'));
  if (res.missing.length) {
    const shown = res.missing.slice(0, 8).map(entryLabel).join(', ');
    parts.push(`not found: ${shown}${res.missing.length > 8 ? `, … (${fmtCount(res.missing.length - 8)} more)` : ''}`);
  }
  return parts.join('; ');
}

/**
 * Dialog status line and toast level for an import outcome: an app.importCsv result
 * ({summary, missing, skipped, ...}, IMPLEMENTATION_NOTES 93) or {res, total, skipped} from
 * resolveImport. Returns {text, level: 'info'|'warn'}.
 */
export function importResultText(out) {
  if (!out || typeof out !== 'object') return {text: '', level: 'info'};
  let text, missing;
  if (out.res && Array.isArray(out.res.matched)) {
    text = importSummary(out.res, out.total ?? out.res.matched.length + out.res.missing.length);
    missing = out.res.missing.length;
  } else {
    missing = Number.isFinite(+out.missing) ? +out.missing : 0;
    text = typeof out.summary === 'string' && out.summary ? out.summary
      : `Matched ${fmtCount(+out.matched || 0)} of ${plural(+out.entries || 0, 'entry', 'entries')}`;
  }
  const skipped = Number.isFinite(+out.skipped) ? +out.skipped : 0;
  if (skipped > 0) text += `; ${plural(skipped, 'unreadable line', 'unreadable lines')}`;
  return {text, level: missing ? 'warn' : 'info'};
}

/** File name for an export: <base>_<scope>.<ext>, keeping letters and digits of any script. */
export function exportFileName(base, format, scope) {
  const f = EXPORT_FORMATS.find((x) => x.key === format) || EXPORT_FORMATS[0];
  const clean = (s) => String(s ?? '').normalize('NFC').replace(/[^\p{L}\p{N}._-]+/gu, '_').replace(/^_+|_+$/g, '');
  const scopeName = {sel: 'selection', vis: 'filtered', all: 'all'}[scope] || clean(scope) || 'rows';
  const b = clean(base) || 'export';
  return `${b}_${scopeName}.${f.ext}`;
}

/** Store-derived counts for the scope options: {sel, vis, all}; vis is null without filters. */
export function scopeCounts(store, ds, n) {
  const s = store.get(`sel:${ds}`);
  const v = store.get(`vis:${ds}`);
  return {
    sel: s && Number.isFinite(s.count) ? s.count : 0,
    vis: v && Number.isFinite(v.count) ? v.count : null,
    all: Number.isFinite(n) ? n : 0,
  };
}

/** Default scope: the selection when one exists, else the filtered set, else everything. */
export function defaultScope(counts) {
  if (counts.sel > 0) return 'sel';
  if (counts.vis !== null) return 'vis';
  return 'all';
}

// ---------------------------------------------------------------------------- DOM helpers

function h(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

/** Save a Blob through a temporary download link. */
export function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.rel = 'noopener';
  a.style.display = 'none';
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

function toast(app, level, text) {
  app.store.emit('toast', {level, text});
}

function reportError(app, what, err) {
  if (!(err instanceof Error)) throw err;
  if (err.name === 'AbortError') return;
  console.warn(`${what} failed`, err);
  toast(app, 'error', `${what} failed: ${err.message}`);
}

function dialogShell(cls, title) {
  const dlg = h('dialog', `pk-dialog pk-io-dialog ${cls}`);
  const form = h('form', 'pk-io-form');
  form.method = 'dialog';
  const hd = h('h2', 'pk-io-title', title);
  form.append(hd);
  dlg.append(form);
  dlg.addEventListener('close', () => dlg.remove());
  dlg.addEventListener('cancel', () => dlg.remove());
  return {dlg, form};
}

function show(dlg) {
  document.body.append(dlg);
  if (typeof dlg.showModal === 'function') dlg.showModal();
  else dlg.setAttribute('open', '');
}

function radioGroup(name, options, value) {
  const fs = h('fieldset', 'pk-io-group');
  for (const o of options) {
    const lab = h('label', 'pk-io-radio');
    const inp = h('input');
    inp.type = 'radio';
    inp.name = name;
    inp.value = o.key;
    inp.checked = o.key === value;
    inp.disabled = !!o.disabled;
    lab.append(inp, document.createTextNode(` ${o.label}`));
    if (o.title) lab.title = o.title;
    fs.append(lab);
  }
  return fs;
}

function checkedValue(form, name) {
  const el = form.querySelector(`input[name="${name}"]:checked`);
  return el ? el.value : null;
}

/**
 * Export dialog for dataset `ds`.
 * opts: {scope?: 'sel'|'vis'|'all', cols?: string[] (shown columns), base?: string (file name)}
 */
export async function openExportDialog(app, ds, opts = {}) {
  let client;
  try {
    client = await app.catalog(ds);
  } catch (err) {
    reportError(app, 'Export', err);
    return null;
  }
  const counts = scopeCounts(app.store, ds, client.n);
  const scope0 = opts.scope && SCOPES.includes(opts.scope) && !(opts.scope === 'sel' && !counts.sel) ? opts.scope : defaultScope(counts);
  const {dlg, form} = dialogShell('pk-export-dialog', 'Export');
  const hasRows = !!(client.manifest.rows && client.manifest.rows.files && client.manifest.rows.files.length);
  const hasPos = !!client.manifest.pos;
  const fmts = EXPORT_FORMATS.map((f) => ({...f, disabled: f.key === 'ds9' && !hasPos,
    title: f.key === 'ds9' && !hasPos ? 'This catalog has no positions' : ''}));
  const scopeOpts = [
    {key: 'sel', label: `Selection (${fmtCount(counts.sel)})`, disabled: !counts.sel},
    {key: 'vis', label: counts.vis === null ? 'Filtered (no filter active)' : `Filtered (${fmtCount(counts.vis)})`},
    {key: 'all', label: `All rows (${fmtCount(counts.all)})`},
  ];
  form.append(h('h3', 'pk-io-sub', 'Rows'), radioGroup('scope', scopeOpts, scope0));
  form.append(h('h3', 'pk-io-sub', 'Format'), radioGroup('format', fmts, 'csv'));
  const extra = h('div', 'pk-io-extra');
  const colsLab = h('label', 'pk-io-check');
  const colsBox = h('input');
  colsBox.type = 'checkbox';
  colsBox.name = 'shown';
  colsBox.checked = false;
  colsLab.append(colsBox, document.createTextNode(' Only the columns shown in the list'));
  if (Array.isArray(opts.cols) && opts.cols.length) extra.append(colsLab);
  const exactLab = h('label', 'pk-io-check');
  const exactBox = h('input');
  exactBox.type = 'checkbox';
  exactBox.name = 'exact';
  exactBox.checked = hasRows;
  exactBox.disabled = !hasRows;
  exactLab.append(exactBox, document.createTextNode(hasRows ? ' Full-precision values (row chunks)' : ' Full-precision values (not available: quantized columns only)'));
  extra.append(exactLab);
  const radLab = h('label', 'pk-io-field');
  const radIn = h('input');
  radIn.type = 'number';
  radIn.min = '0.01';
  radIn.step = 'any';
  radIn.value = '1';
  radIn.name = 'radius';
  radLab.append(document.createTextNode('Region radius (arcsec) '), radIn);
  extra.append(radLab);
  form.append(extra);
  const status = h('p', 'pk-io-status pk-muted');
  status.setAttribute('aria-live', 'polite');
  form.append(status);
  const btns = h('div', 'pk-io-buttons');
  const cancel = h('button', 'pk-btn', 'Cancel');
  cancel.value = 'cancel';
  const copy = h('button', 'pk-btn', 'Copy IDs');
  copy.type = 'button';
  const go = h('button', 'pk-btn pk-primary', 'Download');
  go.type = 'button';
  btns.append(cancel, copy, go);
  form.append(btns);

  const sync = () => {
    const f = checkedValue(form, 'format');
    radLab.hidden = f !== 'ds9';
    exactLab.hidden = !(f === 'csv' || f === 'json');
    colsLab.hidden = !(f === 'csv' || f === 'json');
  };
  form.addEventListener('change', sync);
  sync();

  const run = async (format) => {
    const scope = checkedValue(form, 'scope') || 'all';
    const cols = colsBox.checked && Array.isArray(opts.cols) ? opts.cols : 'all';
    const exact = exactBox.checked && hasRows;
    const r = Number(radIn.value);
    status.textContent = 'Preparing…';
    go.disabled = copy.disabled = true;
    try {
      return await client.export({mask: scope, cols, exact, format, r: Number.isFinite(r) && r > 0 ? r : 1});
    } finally {
      go.disabled = copy.disabled = false;
    }
  };

  go.addEventListener('click', () => {
    const format = checkedValue(form, 'format') || 'csv';
    const scope = checkedValue(form, 'scope') || 'all';
    run(format).then((blob) => {
      const name = exportFileName(opts.base || `${app.portalId}_${ds}`, format, scope);
      downloadBlob(blob, name);
      app.store.emit('selection:saved', {ds, name});
      toast(app, 'info', `Saved ${name}`);
      dlg.close();
    }, (err) => {
      status.textContent = '';
      reportError(app, 'Export', err);
    });
  });
  copy.addEventListener('click', () => {
    run('ids').then(async (blob) => {
      const text = await blob.text();
      if (!navigator.clipboard || typeof navigator.clipboard.writeText !== 'function') {
        throw new Error('the clipboard is not available here');
      }
      await navigator.clipboard.writeText(text);
      const n = text ? text.split('\n').filter(Boolean).length : 0;
      status.textContent = `Copied ${plural(n, 'id', 'ids')}`;
      toast(app, 'info', `Copied ${plural(n, 'id', 'ids')} to the clipboard`);
    }).catch((err) => {
      status.textContent = '';
      reportError(app, 'Copy', err);
    });
  });
  show(dlg);
  return dlg;
}

/** Import dialog: paste or load ids / positions and apply them to the selection of `ds`. */
export async function openImportDialog(app, ds) {
  let client, sel;
  try {
    client = await app.catalog(ds);
    sel = await app.selectionFor(ds);
  } catch (err) {
    reportError(app, 'Import', err);
    return null;
  }
  const {dlg, form} = dialogShell('pk-import-dialog', 'Import a list');
  const help = h('p', 'pk-muted', 'One id, alias or position per line, a CSV/TSV with id or ra/dec columns, a JSON list, or a DS9 region file. Ids are matched first, then aliases, then positions.');
  const ta = h('textarea', 'pk-io-text');
  ta.rows = 8;
  ta.spellcheck = false;
  ta.placeholder = 'J174545.6-290026\n266.4167 -29.0072\n17:45:40.04 -29:00:28.1';
  ta.setAttribute('aria-label', 'Ids or positions');
  const file = h('input', 'pk-io-file');
  file.type = 'file';
  file.accept = '.csv,.tsv,.txt,.reg,.json,text/plain,text/csv,application/json';
  file.setAttribute('aria-label', 'Load a file');
  file.addEventListener('change', () => {
    const f = file.files && file.files[0];
    if (!f) return;
    f.text().then((t) => { ta.value = t; }, (err) => reportError(app, 'Reading the file', err));
  });
  const opSel = h('select', 'pk-io-op');
  for (const [k, label] of [['replace', 'Replace the selection'], ['add', 'Add to the selection'],
    ['subtract', 'Remove from the selection'], ['intersect', 'Intersect with the selection']]) {
    const o = h('option', null, label);
    o.value = k;
    opSel.append(o);
  }
  const radLab = h('label', 'pk-io-field');
  const radIn = h('input');
  radIn.type = 'number';
  radIn.min = '0.01';
  radIn.step = 'any';
  radIn.value = String(IMPORT_RADIUS_ARCSEC);
  radLab.append(document.createTextNode('Position match radius (arcsec) '), radIn);
  radLab.hidden = !client.manifest.pos;
  const row = h('div', 'pk-io-row');
  row.append(opSel, radLab);
  const status = h('p', 'pk-io-status');
  status.setAttribute('aria-live', 'polite');
  const btns = h('div', 'pk-io-buttons');
  const cancel = h('button', 'pk-btn', 'Close');
  cancel.value = 'cancel';
  const go = h('button', 'pk-btn pk-primary', 'Apply');
  go.type = 'button';
  btns.append(cancel, go);
  form.append(help, ta, file, row, status, btns);

  go.addEventListener('click', () => {
    const parsed = parseImportText(ta.value);
    if (!parsed.entries.length) {
      status.textContent = parsed.format === 'empty' ? 'Nothing to import.' : 'No ids or positions were recognised.';
      return;
    }
    go.disabled = true;
    status.textContent = `Matching ${plural(parsed.entries.length, 'entry', 'entries')}…`;
    const r = Number(radIn.value);
    const radiusArcsec = Number.isFinite(r) && r > 0 ? r : IMPORT_RADIUS_ARCSEC;
    const op = opSel.value;
    // app.importCsv is the runtime entry (IMPLEMENTATION_NOTES 93); the local path serves hosts
    // without it.
    const job = typeof app.importCsv === 'function'
      ? app.importCsv(ta.value, {mode: op, ds, radiusArcsec})
      : resolveImport(parsed.entries, {
        n: client.n,
        radiusArcsec,
        resolveIds: (ids) => app.resolveIds(ds, ids),
        crossmatch: client.manifest.pos ? (ra, dec, rad) => client.crossmatch(ra, dec, rad) : null,
      }).then((res) => {
        sel.setIndices(Array.from(res.indices), op);
        return {res, total: parsed.entries.length, skipped: parsed.skipped};
      });
    job.then((out) => {
      const {text, level} = importResultText(out);
      status.textContent = text;
      toast(app, level, text.length > 160 ? text.slice(0, 157) + '…' : text);
    }, (err) => {
      status.textContent = '';
      reportError(app, 'Import', err);
    }).finally(() => { go.disabled = false; });
  });
  show(dlg);
  ta.focus();
  return dlg;
}

/**
 * Export / Import buttons for a pane header. opts: {cols?: () => string[], base?: string}.
 * Returns a destroy function.
 */
export function mountSelectionTools(app, ds, container, opts = {}) {
  const wrap = h('span', 'pk-btn-group pk-io-tools');
  const ex = h('button', 'pk-btn pk-io-export', 'Export');
  ex.type = 'button';
  ex.title = 'Export the selection, the filtered rows or all rows (CSV, JSON, ids, DS9)';
  const im = h('button', 'pk-btn pk-io-import', 'Import');
  im.type = 'button';
  im.title = 'Select rows from a list of ids, aliases or positions';
  wrap.append(ex, im);
  container.append(wrap);
  const onEx = () => openExportDialog(app, ds, {cols: opts.cols ? opts.cols() : undefined, base: opts.base});
  const onIm = () => openImportDialog(app, ds);
  ex.addEventListener('click', onEx);
  im.addEventListener('click', onIm);
  return () => {
    ex.removeEventListener('click', onEx);
    im.removeEventListener('click', onIm);
    wrap.remove();
  };
}
