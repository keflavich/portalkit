// Record sets (ARCHITECTURE.md sections 5.6-5.8): pks1 / pki1 / json / sedcurve records in
// gzip-member shards, single-member files, PNG mode and json-legacy directories.
// decodePKS1 / decodePKI1 are pure; RecordStore needs only fetch (works in workers and node).
// Python twin of the decoders: portalkit/bundle/records.py.
//
// Foreign record sets (shared contract (A)): a page DatasetRef {kind: 'records', url, base,
// id_column?} names another portal's record set. `url` is the foreign index
// (<base><key>/index.json; recordIndexUrl builds it when only base and key are given), and every
// shard / file URL resolves against that index URL, i.e. under `base`. With `id_column` the
// record of a catalog row is looked up by the row's value in that column (RecordStore opts.idOf,
// wired by core/boot.js) instead of by the row id. PNG-mode sidecars of a foreign set resolve
// against the foreign portal's page directory (foreignPageBase), the directory above its data/.

import {RECORD_LRU, SHARD_LRU_BYTES} from '../core/constants.js';
import {asU8, maybeGunzipSync, gunzip, dequantArray, readF32LE, readF64LE, readU16LE} from './codec.js';
import {fetchRaw, fetchBytes, fetchJSON, abortError, throwIfAborted, now, addDecodeMs} from './fetch.js';

const utf8 = new TextDecoder('utf-8');

function u32at(u, p) {
  return (u[p] | (u[p + 1] << 8) | (u[p + 2] << 16) | (u[p + 3] << 24)) >>> 0;
}

/** Split "MAGC" + u32 header_len + header JSON + binary. */
export function splitRecord(bytes, magic) {
  const u = maybeGunzipSync(bytes);
  if (u.length < 8) throw new Error('record too short');
  const m = String.fromCharCode(u[0], u[1], u[2], u[3]);
  if (magic && m !== magic) throw new Error(`not a ${magic.toLowerCase()} record (magic ${JSON.stringify(m)})`);
  const hl = u32at(u, 4);
  if (hl % 4) throw new Error(`header_len ${hl} not a multiple of 4`);
  if (8 + hl > u.length) throw new Error('record header truncated');
  const header = JSON.parse(utf8.decode(u.subarray(8, 8 + hl)));
  return {magic: m, header, binary: u.subarray(8 + hl)};
}

/** Wavelength grid from an index `grids` entry (uniform / array values) or an array. */
export function gridValues(g) {
  if (g instanceof Float64Array) return g;
  if (Array.isArray(g) || ArrayBuffer.isView(g)) return Float64Array.from(g);
  if (g && g.kind === 'uniform') {
    const n = g.n | 0;
    const out = new Float64Array(n);
    for (let i = 0; i < n; i++) out[i] = g.w0 + g.dw * i;
    return out;
  }
  if (g && g.kind === 'array' && g.values) return Float64Array.from(g.values);
  throw new Error('grid descriptor not resolved (array grids stored in a file must be loaded first)');
}

function waveValues(desc, n, binary, grids) {
  const k = desc.kind;
  if (k === 'uniform') {
    const out = new Float64Array(n);
    for (let i = 0; i < n; i++) out[i] = desc.w0 + desc.dw * i;
    return out;
  }
  if (k === 'array') {
    const dt = desc.dtype || 'f32';
    if (dt === 'f32') return Float64Array.from(readF32LE(binary, desc.off, n));
    if (dt === 'f64') return readF64LE(binary, desc.off, n);
    throw new Error(`unknown wave dtype ${dt}`);
  }
  if (k === 'shared') {
    if (!grids || !(desc.grid in grids)) throw new Error(`shared grid ${JSON.stringify(desc.grid)} not supplied`);
    const g = gridValues(grids[desc.grid]);
    if (g.length !== n) throw new Error(`shared grid ${desc.grid} has ${g.length} samples, record has ${n}`);
    return g;
  }
  throw new Error(`unknown wave kind ${k}`);
}

/**
 * Decode a pks1 record (raw or gzip bytes). `grids` maps shared grid names to Float64Array /
 * arrays / index descriptors. Returns
 * {id, n, wave: Float64Array, channels: {name: Float32Array}, units: {name: unit}, unitW,
 *  segments, maskRuns, maskBits, lines, meta, header}.
 */
export function decodePKS1(bytes, grids) {
  const {header: h, binary} = splitRecord(bytes, 'PKS1');
  const n = h.n | 0;
  const wave = waveValues(h.wave, n, binary, grids);
  const channels = {};
  const units = {};
  for (const c of h.channels || []) {
    let v;
    if (c.enc === 'f32') v = readF32LE(binary, c.off, n);
    else if (c.enc === 'u16q' || c.enc === 'u16log') v = dequantArray(c, readU16LE(binary, c.off, n));
    else throw new Error(`unsupported channel encoding ${c.enc}`);
    channels[c.name] = v;
    units[c.name] = c.unit ?? '';
  }
  return {
    id: h.id, n, wave, channels, units, unitW: h.unit_w || 'um',
    segments: h.segments || [], maskRuns: h.mask_runs || [], maskBits: h.mask_bits || {},
    lines: h.lines || [], meta: h.meta || {}, header: h,
  };
}

/** Inverse stretch of a normalised value s in [0, 1] (NaN passes through). */
export function unstretch(st, s) {
  let lin = s;
  if (st.kind === 'asinh') {
    const a = st.a ?? 0.1;
    lin = a * Math.sinh(s * Math.asinh(1 / a));
  }
  return st.lo + (st.hi - st.lo) * lin;
}

/**
 * Decode a pki1 record. Returns {id, nx, ny, enc, data: Uint8Array|Uint16Array (row-major, y
 * ascending), stretch, wave: Float64Array, y: Float64Array, unitY, segments, header}.
 */
export function decodePKI1(bytes, grids) {
  const {header: h, binary} = splitRecord(bytes, 'PKI1');
  const nx = h.nx | 0, ny = h.ny | 0;
  let data;
  if (h.enc === 'u8') data = binary.slice(h.off, h.off + nx * ny);
  else if (h.enc === 'u16') data = readU16LE(binary, h.off, nx * ny);
  else throw new Error(`unsupported pki1 enc ${h.enc}`);
  if (data.length !== nx * ny) throw new Error('pki1 data truncated');
  const wave = waveValues(h.wave, nx, binary, grids);
  const y = new Float64Array(ny);
  const yd = h.y || {y0: 0, dy: 1};
  for (let j = 0; j < ny; j++) y[j] = yd.y0 + yd.dy * j;
  return {
    id: h.id, nx, ny, enc: h.enc, data, stretch: h.stretch, wave, y, unitY: yd.unit || '',
    segments: h.segments || [], header: h,
  };
}

/** Physical values of a decoded pki1 image (NaN for u16 code 0). */
export function pki1Values(img) {
  const out = new Float32Array(img.data.length);
  const u16 = img.enc === 'u16';
  for (let i = 0; i < out.length; i++) {
    const c = img.data[i];
    const s = u16 ? (c === 0 ? NaN : (c - 1) / 65534) : c / 255;
    out[i] = unstretch(img.stretch, s);
  }
  return out;
}

/** Decode one inflated record member by record-set kind. */
export function decodeRecord(raw, kind, grids) {
  if (kind === 'pks1' || kind === 'sedcurve') return decodePKS1(raw, grids);
  if (kind === 'pki1') return decodePKI1(raw, grids);
  if (kind === 'json') return JSON.parse(utf8.decode(maybeGunzipSync(raw)));
  throw new Error(`unknown record kind ${kind}`);
}

// ------------------------------------------------------------------ LRUs

/** Byte-bounded LRU (Map insertion order). The newest entry is always kept. */
export class ByteLRU {
  constructor(maxBytes) {
    this.max = maxBytes;
    this.map = new Map();
    this.bytes = 0;
  }
  get(k) {
    const v = this.map.get(k);
    if (v === undefined) return undefined;
    this.map.delete(k);
    this.map.set(k, v);
    return v;
  }
  set(k, v) {
    const old = this.map.get(k);
    if (old !== undefined) {
      this.bytes -= old.byteLength;
      this.map.delete(k);
    }
    this.map.set(k, v);
    this.bytes += v.byteLength;
    for (const [kk, vv] of this.map) {
      if (this.bytes <= this.max || this.map.size <= 1) break;
      this.map.delete(kk);
      this.bytes -= vv.byteLength;
    }
  }
  has(k) {
    return this.map.has(k);
  }
  get size() {
    return this.map.size;
  }
}

/** Count-bounded LRU. */
export class LRU {
  constructor(max) {
    this.max = Math.max(1, max | 0);
    this.map = new Map();
  }
  get(k) {
    if (!this.map.has(k)) return undefined;
    const v = this.map.get(k);
    this.map.delete(k);
    this.map.set(k, v);
    return v;
  }
  set(k, v) {
    this.map.delete(k);
    this.map.set(k, v);
    while (this.map.size > this.max) this.map.delete(this.map.keys().next().value);
  }
  has(k) {
    return this.map.has(k);
  }
  delete(k) {
    this.map.delete(k);
  }
  clear() {
    this.map.clear();
  }
  get size() {
    return this.map.size;
  }
}

// ------------------------------------------------------------------ RecordStore

const DEFAULT_CHANNEL = 'focus';

function documentBase() {
  if (typeof document !== 'undefined' && document && document.baseURI) return document.baseURI;
  return null;
}

/**
 * URL of a pki1 PNG-mode sidecar (IMPLEMENTATION_NOTES 81): `{assets}` / `{res}` are substituted
 * from `page`, then the URL resolves against the page URL `base` (section 4.3), not against the
 * records index. Without a base it falls back to `fallback` (the index URL).
 */
export function resolvePageUrl(rel, {base = null, page = null, fallback = undefined} = {}) {
  let s = String(rel);
  if (page && page.assets) s = s.split('{assets}').join(page.assets);
  if (page && page.res) s = s.split('{res}').join(page.res);
  return new URL(s, base || fallback).toString();
}

/**
 * Index URL of a records DatasetRef (page-relative or absolute, unresolved): `ref.url`, or for a
 * foreign ref (contract (A)) without one `<base><key>/index.json`, where key is `ref.key`. Null
 * when neither is available.
 */
export function recordIndexUrl(ref) {
  if (!ref || typeof ref !== 'object') return null;
  if (typeof ref.url === 'string' && ref.url) return ref.url;
  if (typeof ref.base === 'string' && ref.base && typeof ref.key === 'string' && ref.key) {
    return `${ref.base.endsWith('/') ? ref.base : ref.base + '/'}${ref.key}/index.json`;
  }
  return null;
}

/** True for a foreign records DatasetRef (one that carries `base` or `id_column`). */
export function isForeignRef(ref) {
  return !!ref && typeof ref === 'object' && ref.kind === 'records'
    && ((typeof ref.base === 'string' && !!ref.base) || (typeof ref.id_column === 'string' && !!ref.id_column));
}

/**
 * Record key of a catalog row for a foreign set with `id_column` (contract (A)): the row's value
 * in that column as text (integers without a decimal point), null when missing or NaN.
 */
export function foreignKeyOf(row, column) {
  if (!row || typeof row !== 'object' || !column) return null;
  const v = row[column];
  if (v === null || v === undefined || v === '') return null;
  if (typeof v === 'number') {
    if (!Number.isFinite(v)) return null;
    return String(v);
  }
  if (typeof v === 'bigint') return v.toString();
  const t = String(v).trim();
  return t ? t : null;
}

/**
 * Page directory of the portal that owns a foreign record set (contract (A)): the directory above
 * its `data/`, i.e. `<index url>/../../` for an index at `<base><key>/index.json`. PNG-mode
 * sidecar URLs of a foreign set resolve against it, so `{res}` / relative paths land in the
 * foreign portal.
 */
export function foreignPageBase(indexUrl) {
  return new URL('../../', String(indexUrl)).toString();
}

const KEY_LRU = 4096;

export class RecordStore {
  /**
   * Open a record set from its index.json URL. opts: {fetch, signal, shardBytes, recordLRU,
   * base, page, idOf}. `base` is the page URL that PNG-mode sidecar URLs resolve against (default
   * document.baseURI, else the index URL); `page` supplies `assets` / `res` for `{assets}` /
   * `{res}` substitution in them. `idOf(id, {signal})` (foreign sets with id_column) resolves a
   * catalog row id to the record key (null when the row has none); without it the row id is
   * the record key.
   */
  static async open(url, opts = {}) {
    const u = String(url);
    const index = await fetchJSON(u, {signal: opts.signal, fetch: opts.fetch});
    return new RecordStore(index, u, opts);
  }

  constructor(index, url, opts = {}) {
    this.index = index;
    this.url = String(url);
    this.kind = index.kind;
    this.fetchOpts = {fetch: opts.fetch};
    this.entries = index.entries || {};
    this.shardCache = new ByteLRU(opts.shardBytes ?? SHARD_LRU_BYTES);
    this.recordCache = new LRU(opts.recordLRU ?? RECORD_LRU);
    this.inflight = new Map(); // shard/file url -> Promise<Uint8Array>
    this.latest = new Map(); // channel -> newest token
    this.grids = null; // resolved {name: Float64Array}
    this.gridsPromise = null;
    this.legacy = index.kind === 'json-legacy';
    this.pageBase = opts.base ? String(opts.base) : documentBase();
    this.page = opts.page || null;
    this.idOf = typeof opts.idOf === 'function' ? opts.idOf : null;
    this.keyCache = new LRU(KEY_LRU); // row id -> record key | null (idOf results)
  }

  /** Record key of a row id: the id itself, or the idOf lookup (cached). */
  async keyFor(id, signal) {
    const s = String(id);
    if (!this.idOf) return s;
    if (this.keyCache.has(s)) return this.keyCache.get(s);
    const k = await this.idOf(s, {signal});
    const key = k === null || k === undefined || k === '' ? null : String(k);
    this.keyCache.set(s, key);
    return key;
  }

  /** Resolve a PNG sidecar URL against the page (with {assets}/{res} substituted). */
  resolvePng(rel) {
    return resolvePageUrl(rel, {base: this.pageBase, page: this.page, fallback: this.url});
  }

  /** Resolve a path relative to the index URL. */
  resolve(rel) {
    return new URL(rel, this.url).toString();
  }

  /**
   * True when the set has a record for `id` (json-legacy sets cannot tell and return true). With
   * idOf, an id whose key is not looked up yet counts as present; get() then resolves null when
   * the lookup finds none. Callers that must tell "no record" from "superseded" use hasRecord().
   */
  has(id) {
    if (this.legacy) return true;
    let key = String(id);
    if (this.idOf) {
      if (!this.keyCache.has(key)) return this.n > 0;
      key = this.keyCache.get(key);
      if (key === null) return false;
    }
    return Object.prototype.hasOwnProperty.call(this.entries, key);
  }

  /**
   * Async has(): with idOf the row's record key is looked up first (cached), so the answer is
   * exact for foreign sets with id_column. Without idOf it equals has(id).
   */
  async hasRecord(id, {signal} = {}) {
    if (this.legacy || !this.idOf) return this.has(id);
    const key = await this.keyFor(id, signal);
    throwIfAborted(signal);
    return key !== null && Object.prototype.hasOwnProperty.call(this.entries, key);
  }

  /** Ids with records (empty for json-legacy). */
  ids() {
    return this.legacy ? [] : Object.keys(this.entries);
  }

  get n() {
    return this.index.n ?? this.ids().length;
  }

  async loadGrids(signal) {
    if (this.grids) return this.grids;
    if (!this.gridsPromise) {
      this.gridsPromise = (async () => {
        const out = {};
        for (const [name, g] of Object.entries(this.index.grids || {})) {
          if (g.kind === 'array' && g.file && !g.values) {
            const b = await fetchBytes(this.resolve(g.file), {...this.fetchOpts, signal});
            out[name] = parseGridFile(b);
          } else {
            out[name] = gridValues(g);
          }
        }
        this.grids = out;
        return out;
      })();
      this.gridsPromise.catch(() => {
        this.gridsPromise = null;
      });
    }
    return this.gridsPromise;
  }

  async fetchCached(url, signal) {
    const hit = this.shardCache.get(url);
    if (hit) return hit;
    let p = this.inflight.get(url);
    if (!p) {
      p = fetchRaw(url, {...this.fetchOpts}).then(
        (b) => {
          this.inflight.delete(url);
          this.shardCache.set(url, b);
          return b;
        },
        (err) => {
          this.inflight.delete(url);
          throw err;
        },
      );
      this.inflight.set(url, p);
    }
    if (!signal) return p;
    return new Promise((resolve, reject) => {
      if (signal.aborted) return reject(abortError());
      const onAbort = () => reject(abortError());
      signal.addEventListener('abort', onAbort, {once: true});
      p.then(
        (v) => {
          signal.removeEventListener('abort', onAbort);
          resolve(v);
        },
        (e) => {
          signal.removeEventListener('abort', onAbort);
          reject(e);
        },
      );
    });
  }

  async load(id, signal) {
    const key = String(id);
    if (this.legacy) {
      const pat = this.index.pattern || '{id}.json';
      const dir = this.index.dir || '';
      const rel = (dir && !dir.endsWith('/') ? dir + '/' : dir) + pat.split('{id}').join(encodeURIComponent(key));
      return fetchJSON(this.resolve(rel), {...this.fetchOpts, signal});
    }
    const ent = this.entries[key];
    if (ent === undefined) return null;
    if (ent && typeof ent === 'object' && !Array.isArray(ent)) {
      if (ent.png) return {id: key, kind: 'png', png: this.resolvePng(ent.png), extent: ent.extent || null};
      throw new Error(`record ${key}: unsupported entry`);
    }
    const needsGrids = this.kind !== 'json' && this.index.grids && Object.keys(this.index.grids).length;
    const grids = needsGrids ? await this.loadGrids(signal) : undefined;
    let member;
    if (Array.isArray(ent)) {
      const [si, off, len] = ent;
      const shard = (this.index.shards || [])[si];
      if (shard === undefined) throw new Error(`record ${key}: shard ${si} not listed`);
      const bytes = await this.fetchCached(this.resolve(shard), signal);
      if (off + len > bytes.length) throw new Error(`record ${key}: slice beyond shard end`);
      member = bytes.subarray(off, off + len);
    } else {
      member = await this.fetchCached(this.resolve(ent), signal);
    }
    throwIfAborted(signal);
    const t0 = now();
    const raw = await gunzip(member);
    const rec = decodeRecord(raw, this.kind, grids);
    addDecodeMs(now() - t0);
    return rec;
  }

  /**
   * Get one record. opts: {signal, token, channel}. When `token` is given, a response whose token
   * is older than the newest token issued on the same channel (default 'focus') resolves to null
   * (superseded focus). Unknown ids resolve to null; aborted requests reject with AbortError.
   * With idOf the row id is mapped to the record key first; the result is a shallow copy with
   * `id` = the row id and `key` = the record key.
   */
  async get(id, opts = {}) {
    let key = String(id);
    const channel = opts.channel ?? DEFAULT_CHANNEL;
    const token = opts.token;
    if (token !== undefined) {
      const cur = this.latest.get(channel);
      if (cur === undefined || token >= cur) this.latest.set(channel, token);
    }
    const stale = () => token !== undefined && this.latest.get(channel) !== token;
    throwIfAborted(opts.signal);
    if (this.idOf) {
      const k = await this.keyFor(key, opts.signal);
      throwIfAborted(opts.signal);
      if (k === null || stale()) return null;
      key = k;
    }
    let rec = this.recordCache.get(key);
    if (rec === undefined) {
      rec = await this.load(key, opts.signal);
      if (rec !== null) this.recordCache.set(key, rec);
    }
    throwIfAborted(opts.signal);
    if (stale()) return null;
    // Foreign set with id_column: the record answers for the catalog row, so it carries the row
    // id and keeps the foreign record key in `key` (the cached record itself is shared).
    if (this.idOf && rec && typeof rec === 'object') return {...rec, id: String(id), key};
    return rec;
  }
}

/** Array grid stored in a file: JSON array / {values}, or little-endian f64 values. */
export function parseGridFile(bytes) {
  const u = asU8(bytes);
  let i = 0;
  while (i < u.length && (u[i] === 0x20 || u[i] === 0x0a || u[i] === 0x0d || u[i] === 0x09)) i++;
  if (u[i] === 0x5b || u[i] === 0x7b) {
    const j = JSON.parse(utf8.decode(u));
    return Float64Array.from(Array.isArray(j) ? j : j.values);
  }
  if (u.length % 8) throw new Error('binary grid file length is not a multiple of 8');
  return readF64LE(u, 0, u.length / 8);
}
