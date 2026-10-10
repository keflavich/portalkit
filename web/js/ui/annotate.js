// Checklist annotation mode (archive gallery, ARCHITECTURE.md section 8.3).
//
// Codes (default y/n/g/w/l/e, the key of jwst_archive_spectra_checklist.csv) are stored per
// viewer in localStorage['pk:<portal>:annot:<ds>']. Nothing here talks to a server: the only
// outputs are the browser storage and a CSV download. The CSV carries the checklist key
// columns so `portalkit archive-checklist-diff EXPORT.csv` can print a diff against the
// hand-edited checklist for manual application.
//
// CSV format (UTF-8, RFC 4180, no comment lines, rows sorted by id):
//   id,Target,Program,Object,SlitID,Observation,Visit,VisitGroup,Filename,<key>
// where <key> is the gallery option annotate.key (default "checklist") and holds one code.
//
// Every storage access is wrapped in try/catch: private windows and blocked storage throw.
// The helpers above the DOM section are pure and covered by tests/js/export.test.mjs.

import {toCSV} from '../workers/ops/export.js';
import {downloadBlob} from './export.js';

export const DEFAULT_CODES = Object.freeze(['y', 'n', 'g', 'w', 'l', 'e']);

/** Meaning of the archive checklist codes (jwst_archive_spectra_checklist.csv key). */
export const CODE_LABELS = Object.freeze({
  y: 'all filters covered',
  n: 'some filters not covered',
  g: 'overlap between filter and gap',
  w: 'weird',
  l: 'low S/N at long wavelength',
  e: 'emission',
});

/** Join key columns of the hand-edited checklist. */
export const CHECKLIST_KEYS = Object.freeze(['Target', 'Program', 'Object', 'SlitID', 'Observation', 'Visit',
  'VisitGroup', 'Filename']);

export const STORAGE_VERSION = 1;

/** localStorage key of one dataset's annotations. */
export function storageKey(portal, ds) {
  return `pk:${portal}:annot:${ds}`;
}

/** Valid code list: unique, non-empty, at most 8 characters each; DEFAULT_CODES when none. */
export function normalizeCodes(codes) {
  if (!Array.isArray(codes)) return DEFAULT_CODES.slice();
  const out = [];
  for (const c of codes) {
    const s = String(c ?? '').trim();
    if (!s || s.length > 8 || /[\s,"]/.test(s) || out.includes(s)) continue;
    out.push(s);
  }
  return out.length ? out : DEFAULT_CODES.slice();
}

/**
 * Code for a key press: the code itself (case-insensitive for single letters), null for
 * Backspace / Delete / '0' (clear), undefined for keys that do not annotate.
 */
export function codeForKey(key, codes = DEFAULT_CODES) {
  if (typeof key !== 'string' || !key) return undefined;
  if (key === 'Backspace' || key === 'Delete' || key === '0') return null;
  if (codes.includes(key)) return key;
  if (key.length === 1) {
    const lo = key.toLowerCase();
    const hit = codes.find((c) => c.toLowerCase() === lo);
    if (hit !== undefined) return hit;
  }
  return undefined;
}

/** {id: {c, t}} map -> storage text. */
export function serializeAnnotations(map) {
  const codes = {};
  for (const [id, v] of map) codes[id] = {c: v.c, t: v.t};
  return JSON.stringify({v: STORAGE_VERSION, codes});
}

/** Storage text -> Map(id -> {c, t}); codes outside `codes` and malformed records are dropped. */
export function parseAnnotations(text, codes = DEFAULT_CODES) {
  const out = new Map();
  if (typeof text !== 'string' || !text) return out;
  let doc;
  try {
    doc = JSON.parse(text);
  } catch (err) {
    if (err instanceof SyntaxError) return out;
    throw err;
  }
  const src = doc && typeof doc === 'object' && doc.codes && typeof doc.codes === 'object' ? doc.codes : null;
  if (!src) return out;
  for (const [id, v] of Object.entries(src)) {
    const c = v && typeof v === 'object' ? v.c : v;
    if (typeof c !== 'string' || !codes.includes(c)) continue;
    const t = v && typeof v === 'object' && Number.isFinite(v.t) ? v.t : 0;
    out.set(id, {c, t});
  }
  return out;
}

/** In-memory Storage stand-in (used when localStorage is unavailable, and by tests). */
export function memoryStorage() {
  const m = new Map();
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => { m.set(k, String(v)); },
    removeItem: (k) => { m.delete(k); },
    get length() { return m.size; },
  };
}

/** window.localStorage, or null when absent or blocked. */
export function browserStorage() {
  try {
    return typeof localStorage !== 'undefined' && localStorage ? localStorage : null;
  } catch (err) {
    if (err && err.name === 'SecurityError') return null;
    throw err;
  }
}

function isStorageError(err) {
  return !!err && (err.name === 'QuotaExceededError' || err.name === 'SecurityError'
    || err.name === 'NS_ERROR_DOM_QUOTA_REACHED' || err.code === 22);
}

/**
 * Per-viewer annotations of one dataset.
 * opts: {portal, ds, codes?, storage? (Storage-like; default localStorage, else memory),
 *        onChange?(version), onError?(err), now?() -> ms}
 */
export class Annotations {
  constructor(opts = {}) {
    this.portal = String(opts.portal ?? 'portal');
    this.ds = String(opts.ds ?? 'ds');
    this.codes = normalizeCodes(opts.codes);
    this.key = storageKey(this.portal, this.ds);
    this.storage = opts.storage === undefined ? browserStorage() : opts.storage;
    this.persistent = !!this.storage;
    if (!this.storage) this.storage = memoryStorage();
    this.onChange = typeof opts.onChange === 'function' ? opts.onChange : null;
    this.onError = typeof opts.onError === 'function' ? opts.onError : null;
    this.now = typeof opts.now === 'function' ? opts.now : () => Date.now();
    this.version = 0;
    this.map = new Map();
    this.reload();
  }

  /** Re-read storage (after a 'storage' event from another tab). Returns true when it changed. */
  reload() {
    let text = null;
    try {
      text = this.storage.getItem(this.key);
    } catch (err) {
      if (!isStorageError(err)) throw err;
      if (this.onError) this.onError(err);
    }
    const next = parseAnnotations(text, this.codes);
    const same = next.size === this.map.size && Array.from(next).every(([k, v]) => {
      const o = this.map.get(k);
      return o && o.c === v.c;
    });
    this.map = next;
    if (!same) this._bump();
    return !same;
  }

  _bump() {
    this.version += 1;
    if (this.onChange) this.onChange(this.version);
  }

  _save() {
    try {
      if (this.map.size) this.storage.setItem(this.key, serializeAnnotations(this.map));
      else this.storage.removeItem(this.key);
      return true;
    } catch (err) {
      if (!isStorageError(err)) throw err;
      if (this.onError) this.onError(err);
      return false;
    }
  }

  get size() {
    return this.map.size;
  }

  /** Code of `id`, or null. */
  get(id) {
    const v = this.map.get(String(id));
    return v ? v.c : null;
  }

  /** Set (or with null, clear) the code of `id`. Throws RangeError for unknown codes. */
  set(id, code) {
    const k = String(id);
    if (code === null || code === undefined || code === '') {
      if (!this.map.has(k)) return false;
      this.map.delete(k);
    } else {
      if (!this.codes.includes(code)) throw new RangeError(`unknown annotation code ${JSON.stringify(code)}`);
      const cur = this.map.get(k);
      if (cur && cur.c === code) return false;
      this.map.set(k, {c: code, t: this.now()});
    }
    this._save();
    this._bump();
    return true;
  }

  /** Set `code`, or clear it when `id` already has that code. Returns the new code or null. */
  toggle(id, code) {
    const cur = this.get(id);
    if (cur === code) {
      this.set(id, null);
      return null;
    }
    this.set(id, code);
    return code;
  }

  /** Remove every annotation of this dataset. */
  clear() {
    if (!this.map.size) return false;
    this.map.clear();
    this._save();
    this._bump();
    return true;
  }

  /** [[id, code], ...] in insertion order. */
  entries() {
    return Array.from(this.map, ([id, v]) => [id, v.c]);
  }

  /** {code: count} over the current annotations (every code present, zero when unused). */
  counts() {
    const out = Object.fromEntries(this.codes.map((c) => [c, 0]));
    for (const v of this.map.values()) out[v.c] = (out[v.c] || 0) + 1;
    return out;
  }
}

function cellText(v) {
  if (v === null || v === undefined) return '';
  if (typeof v === 'number') {
    if (Number.isNaN(v)) return '';
    if (v === Infinity) return 'inf';
    if (v === -Infinity) return '-inf';
    return String(v);
  }
  if (typeof v === 'boolean') return v ? 'True' : 'False';
  return String(v);
}

/**
 * Checklist key values of one annotated item. Looks the key up (case-insensitive) in each
 * source in order: item tags, then the catalog row, then the item itself.
 */
export function checklistFields(sources, keys = CHECKLIST_KEYS) {
  const list = (Array.isArray(sources) ? sources : [sources]).filter((s) => s && typeof s === 'object');
  const lowered = list.map((s) => {
    const m = new Map();
    for (const k of Object.keys(s)) if (!m.has(k.toLowerCase())) m.set(k.toLowerCase(), k);
    return m;
  });
  const out = {};
  for (const key of keys) {
    let val;
    for (let j = 0; j < list.length && val === undefined; j++) {
      const s = list[j];
      if (Object.prototype.hasOwnProperty.call(s, key) && s[key] !== null && s[key] !== undefined && s[key] !== '') val = s[key];
      else {
        const alt = lowered[j].get(key.toLowerCase());
        if (alt !== undefined && s[alt] !== null && s[alt] !== undefined && s[alt] !== '') val = s[alt];
      }
    }
    out[key] = cellText(val);
  }
  return out;
}

/**
 * CSV text of annotations. entries: [{id, code, fields: {Target: .., ...}}].
 * Header: id, ...keys, <key>. Rows are sorted by id (numeric-aware); rows without a code are
 * skipped. opts: {keys = CHECKLIST_KEYS, key = 'checklist'}.
 */
export function annotationsCSV(entries, opts = {}) {
  const keys = Array.isArray(opts.keys) ? opts.keys : CHECKLIST_KEYS;
  const codeKey = opts.key ? String(opts.key) : 'checklist';
  const header = ['id', ...keys, codeKey];
  const coll = new Intl.Collator('en', {numeric: true, sensitivity: 'variant'});
  const rows = (entries || [])
    .filter((e) => e && e.code !== null && e.code !== undefined && e.code !== '')
    .slice()
    .sort((a, b) => coll.compare(String(a.id), String(b.id)) || (String(a.id) < String(b.id) ? -1 : String(a.id) > String(b.id) ? 1 : 0))
    .map((e) => [String(e.id), ...keys.map((k) => cellText(e.fields ? e.fields[k] : '')), String(e.code)]);
  return toCSV(header, rows);
}

// ---------------------------------------------------------------------------- app binding

const instances = new Map();   // `${portal}\0${ds}` -> Annotations

/**
 * Shared Annotations of dataset `ds` for this page. Bumps store `annot:<ds>` = {version} on
 * every change (also when another tab edits the same storage key).
 */
export function annotationsFor(app, ds, opts = {}) {
  const id = `${app.portalId}\u0000${ds}`;
  if (instances.has(id)) return instances.get(id);
  let warned = false;
  const ann = new Annotations({
    portal: app.portalId,
    ds,
    codes: opts.codes,
    storage: opts.storage,
    onChange: (version) => app.store.set({[`annot:${ds}`]: {version}}, {source: 'annotate'}),
    onError: (err) => {
      if (warned) return;
      warned = true;
      app.store.emit('toast', {level: 'warn', text: `Annotations cannot be saved in this browser (${err.name}); they last until the page closes. Export them as CSV.`});
    },
  });
  if (!ann.persistent) {
    app.store.emit('toast', {level: 'warn', text: 'Browser storage is unavailable: annotations last until the page closes. Export them as CSV.'});
  }
  if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
    window.addEventListener('storage', (ev) => {
      if (ev.key === ann.key || ev.key === null) ann.reload();
    });
  }
  app.store.set({[`annot:${ds}`]: {version: ann.version}}, {source: 'annotate'});
  instances.set(id, ann);
  return ann;
}

/**
 * Download the annotations as CSV. `fieldsOf(id)` -> Promise<object> | object returns the
 * checklist fields of one id (see checklistFields). opts: {key, keys, filename}.
 */
export async function downloadAnnotations(app, ds, ann, fieldsOf, opts = {}) {
  const keys = Array.isArray(opts.keys) ? opts.keys : CHECKLIST_KEYS;
  const entries = [];
  for (const [id, code] of ann.entries()) {
    const fields = fieldsOf ? await fieldsOf(id) : {};
    entries.push({id, code, fields: fields || {}});
  }
  const text = annotationsCSV(entries, {keys, key: opts.key});
  const name = opts.filename || `${app.portalId}_${ds}_${opts.key || 'checklist'}_annotations.csv`.replace(/[^\p{L}\p{N}._-]+/gu, '_');
  downloadBlob(new Blob([text], {type: 'text/csv;charset=utf-8'}), name);
  app.store.emit('selection:saved', {ds, name});
  app.store.emit('toast', {level: 'info', text: `Saved ${name} (${entries.length} annotated). Apply it with: portalkit archive-checklist-diff ${name}`});
  return name;
}
