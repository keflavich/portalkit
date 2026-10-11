// Data worker (ARCHITECTURE.md section 6.6): one module worker per catalog dataset. It fetches
// and decodes column files (chunk-aware), positions, ids and row chunks, and serves the
// DataClient ops over workers/rpc.js:
//   open, ensure, values, project, pick, brush, query, hist, bin2d, rows, find, crossmatch,
//   skyWindow, exportCSV, gpuColumns, cancel
// plus id, ids, record, export (csv|json|ids|ds9), setSel, density, info, order (argsort over
// all rows, IMPLEMENTATION_NOTES 74) and resolveIds (one pass over the ids, note 102).
// createEngine() holds the state and is also used in-thread (createLocalRpc) when Workers are
// unavailable, and by the node tests.
//
// Chunks (IMPLEMENTATION_NOTES 98): column chunk_files and ids.chunks follow the manifest's
// `chunk_starts` when present (chunk 0 = the LOD base sample), else ceil(n / chunk_rows) slices.
// Startup reads (note 99): id(i), ids(idx) and record(i) load only the ids chunks that hold the
// rows asked for (record(i) none at all when the row chunk carries the id); find, query (text or
// id filters), export, order('id') and resolveIds load every ids chunk. rows() and record() on the
// quantized fallback (no row chunks) read pos only with {pos: true} or when it is already decoded.
// record(i) on a catalog with row chunks merges the quantized cells of the stored columns the row
// chunk omits (exact: false) and lists their keys in `_qkeys`.
// Manifest prefetch (note 101): started as data.worker.js?m=<manifest url>, the worker fetches the
// manifest from top-level code and `open` reuses that in-flight request.

import {CHUNK_ROWS, SKY_MARKER_CAP} from '../core/constants.js';
import {stepDigits} from '../core/fmt.js';
import {decodeColumn, decodePos, decodeStr, decodeSparse, sparseKind, decodeDensity, stepOf, dequant, CAT8_MISSING, CAT16_MISSING} from '../data/codec.js';
import {fetchBytes, fetchJSON, getStats, addDecodeMs, now, abortError, throwIfAborted, FetchError} from '../data/fetch.js';
import {compileCached, evaluate} from '../data/expr.js';
import {serve, withTransfer} from './rpc.js';
import {drawableXY, drawableProj, brushArrays, buildPickGrid, pickNearest} from './ops/select.js';
import {histogram, bin2d} from './ops/raster.js';
import {applyFilters, filterNeeds} from './ops/query.js';
import {skyWindow, buildDecIndex, crossmatch} from './ops/sky.js';
import {IdIndex, findRows} from './ops/find.js';
import {project, ProjCache, normSpec} from './ops/project.js';
import {formatValue, f32String, toCSV, toJSON, toIdList, toDS9, MIME} from './ops/export.js';
import {argsortValues, argsortCodes, argsortCategory, argsortNatural, normDir} from './ops/order.js';

const U32_MISSING = 0xffffffff;

/**
 * Pixel scale of a pick ViewRef. `rPx` is a pixel radius (§6.6), so the ViewRef must carry
 * `kx`, `ky`: pixels per data unit (per decade on log axes) of the pane's current view.
 */
export function pickScale(view) {
  const kx = +(view && view.kx), ky = +(view && view.ky);
  if (!(Number.isFinite(kx) && kx !== 0 && Number.isFinite(ky) && ky !== 0)) {
    throw new TypeError('pick: ViewRef needs kx, ky (pixels per data unit, per decade on log axes) to convert rPx to data units');
  }
  return {kx: Math.abs(kx), ky: Math.abs(ky)};
}
const LINEAR_Q = new Set(['u16q', 'u8q']);
const LOG_Q = new Set(['u16log', 'u8log']);
const ROW_CACHE = 16;
const QCHUNK_CACHE = 64;
// Encodings whose float64 value comes from the code (the Float32 values cache rounds large
// offsets such as MJD or wavelengths in Angstrom).
const CODE_DEQUANT = new Set(['u16q', 'u8q', 'u16log', 'u8log', 'u32lin']);
const EXPR_CACHE = 16;
const ORDER_CACHE = 2;
// Encodings whose codes order rows exactly like their values (code 0 / 0xffffffff = missing).
const CODE_ORDER = new Set(['u16q', 'u8q', 'u16log', 'u8log', 'u32', 'u32lin']);
/** Keys DataClient.order treats as "sort by catalog id". */
export const ID_ORDER_KEYS = Object.freeze(['id', '__id']);

function raceAbort(p, signal) {
  if (!signal) return p;
  if (signal.aborted) return Promise.reject(abortError());
  return new Promise((resolve, reject) => {
    const on = () => reject(abortError());
    signal.addEventListener('abort', on, {once: true});
    p.then(
      (v) => {
        signal.removeEventListener('abort', on);
        resolve(v);
      },
      (e) => {
        signal.removeEventListener('abort', on);
        reject(e);
      },
    );
  });
}

/**
 * First row of every chunk of a catalog (IMPLEMENTATION_NOTES 98): the manifest's `chunk_starts`
 * (validated: integers, starting at 0, strictly increasing, every value < n), else
 * [0, chunk_rows, 2*chunk_rows, ...] with ceil(n / chunk_rows) entries (one chunk when
 * n <= chunk_rows, including n = 0). Throws Error on a malformed `chunk_starts`.
 */
export function catalogChunkStarts(man) {
  const n = man.n;
  const cs = man.chunk_starts;
  if (cs !== undefined && cs !== null) {
    if (!Array.isArray(cs) || !cs.length) throw new Error('manifest chunk_starts must be a non-empty array');
    for (let k = 0; k < cs.length; k++) {
      const v = cs[k];
      const ok = Number.isInteger(v) && (k === 0 ? v === 0 : v > cs[k - 1] && v < n);
      if (!ok) throw new Error(`manifest chunk_starts[${k}] = ${JSON.stringify(v)}: expected strictly increasing integers from 0, each < n (${n})`);
    }
    return cs.slice();
  }
  const rows = man.chunk_rows === undefined || man.chunk_rows === null ? CHUNK_ROWS : man.chunk_rows;
  if (!(Number.isInteger(rows) && rows > 0)) throw new Error(`manifest chunk_rows = ${JSON.stringify(man.chunk_rows)}: expected a positive integer`);
  const k = Math.max(1, Math.ceil(n / rows));
  const out = new Array(k);
  for (let c = 0; c < k; c++) out[c] = c * rows;
  return out;
}

/** Index of the chunk that holds row i (the last start <= i; 0 for i below the first start). */
export function chunkIndexOf(starts, i) {
  let lo = 0, hi = starts.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (starts[mid] <= i) lo = mid;
    else hi = mid - 1;
  }
  return lo;
}

/**
 * Manifest prefetch of a worker started as `data.worker.js?m=<url>` (IMPLEMENTATION_NOTES 101):
 * {url, promise} with the fetch already running, or null when `href` carries no `m` parameter.
 * The promise never causes an unhandled rejection; `open` reports its error (or refetches).
 */
export function startPrefetch(href, fopts = {}) {
  let m = null;
  try {
    m = new URL(href).searchParams.get('m');
  } catch (err) {
    if (err instanceof TypeError) return null;   // not an absolute URL
    throw err;
  }
  if (!m) return null;
  const url = new URL(m, href).toString();
  const promise = fetchJSON(url, fopts);
  promise.catch(() => {});
  return {url, promise};
}

function missingCode(enc) {
  if (enc === 'cat8') return CAT8_MISSING;
  if (enc === 'cat16') return CAT16_MISSING;
  if (enc === 'u32' || enc === 'u32lin') return U32_MISSING;
  return 0;
}

function codeArray(enc, n) {
  if (enc === 'u8q' || enc === 'u8log' || enc === 'cat8') return new Uint8Array(n);
  if (enc === 'u32' || enc === 'u32lin') return new Uint32Array(n);
  if (enc === 'u16q' || enc === 'u16log' || enc === 'cat16') return new Uint16Array(n);
  return null;
}

/**
 * Meta with the quantization step formatValue needs: the linear step for u16q/u8q, the dex
 * step for u16log (lo/hi in log10) and u8log (lo/hi linear, 254 code steps).
 */
export function fmtMeta(meta) {
  const enc = meta.enc;
  if (enc === 'u8log') {
    const st = (Math.log10(+meta.hi) - Math.log10(+meta.lo)) / 254;
    return {...meta, step: st};
  }
  if (LINEAR_Q.has(enc) || enc === 'u16log') return {...meta, step: stepOf(meta)};
  return meta;
}

/** Rounded number for quantized row output (null when missing). */
export function quantizedNumber(v, meta) {
  if (!Number.isFinite(v)) return null;
  const enc = meta.enc;
  if (LINEAR_Q.has(enc)) return Number(v.toFixed(stepDigits(stepOf(meta))));
  if (LOG_Q.has(enc)) return Number(formatValue(v, fmtMeta(meta)));
  if (enc === 'f32') return Number(f32String(v));
  return v;
}

/**
 * Engine over one catalog. opts: {fetch} (injected fetch for tests).
 * Returns {handlers, state} where handlers are the RPC ops.
 */
export function createEngine(opts = {}) {
  const fopts = opts.fetch ? {fetch: opts.fetch} : {};
  const S = {
    url: null,
    man: null,
    n: 0,
    starts: [0], // first row of every catalog chunk (catalogChunkStarts)
    idChunks: new Map(), // ids chunk index -> Promise<{start, str}>
    metas: new Map(),
    cols: new Map(), // key -> entry
    pos: null,
    posP: null,
    decIndex: null,
    ids: null,
    idsP: null,
    limits: new Map(),
    rowChunks: new Map(),
    qchunks: new Map(), // `${key}\u0000${ci}` -> Promise<chunk-local column source>
    exprCache: new Map(),
    vis: null,
    visVersion: 0,
    visSeq: -Infinity,
    sel: null,
    selVersion: -1,
    proj: new ProjCache(8),
    views: new Map(),
    orders: new Map(), // `${key}\u0000${dir}` -> Int32Array (LRU of ORDER_CACHE)
  };

  const ds = () => (S.man && S.man.id) || '';
  const resolve = (rel) => new URL(rel, S.url).toString();

  function requireOpen() {
    if (!S.man) throw new Error('catalog not open');
  }

  /** Chunk starts of a file list: [0] for a single file, else the catalog chunk starts. */
  function fileStarts(nFiles, what) {
    if (nFiles <= 1) return [0];
    if (nFiles === S.starts.length) return S.starts;
    throw new Error(`${what}: ${nFiles} chunk files, expected ${S.starts.length} (one per chunk of ${S.n} rows)`);
  }

  /** [first, end) rows of chunk ci for chunk starts `starts`. */
  function rangeOf(starts, ci) {
    return [starts[ci], ci + 1 < starts.length ? starts[ci + 1] : S.n];
  }

  async function getBytes(rel, signal) {
    return fetchBytes(resolve(rel), {...fopts, signal});
  }

  // ---------------------------------------------------------------- columns

  function entryFor(key) {
    let e = S.cols.get(key);
    if (e) return e;
    const meta = S.metas.get(key);
    if (!meta) return null;
    const n = S.n;
    const files = meta.chunk_files || (meta.file ? [meta.file] : []);
    const nChunks = files.length;
    e = {
      meta,
      files,
      nChunks,
      starts: fileStarts(nChunks, `column ${key}`),
      loaded: new Uint8Array(nChunks),
      pending: new Map(),
      values: new Float32Array(n).fill(NaN),
      codes: null,
      bits: meta.enc === 'bits' ? new Uint8Array(n) : null,
      strings: meta.enc === 'str' ? new Array(n).fill(null) : null,
      levels: meta.levels || null,
      missing: missingCode(meta.enc),
    };
    const ca = codeArray(meta.enc, n);
    if (ca) {
      if (e.missing) ca.fill(e.missing);
      e.codes = ca;
    }
    S.cols.set(key, e);
    return e;
  }

  function chunkRange(e, ci) {
    return rangeOf(e.starts, ci);
  }

  /** Load the chunks of column entry `e` that overlap rows [a, b). */
  function loadRange(e, a, b) {
    const ps = [];
    const k0 = chunkIndexOf(e.starts, a);
    for (let k = k0; k < e.nChunks && (k === k0 || e.starts[k] < b); k++) ps.push(loadChunk(e, k));
    return Promise.all(ps);
  }

  async function loadChunk(e, ci) {
    if (e.loaded[ci]) return;
    let p = e.pending.get(ci);
    if (!p) {
      p = (async () => {
        const [a, b] = chunkRange(e, ci);
        let vbits = null;
        if (e.meta.validity) {
          const ve = entryFor(e.meta.validity);
          if (!ve) throw new Error(`column ${e.meta.key}: unknown validity column ${e.meta.validity}`);
          await loadRange(ve, a, b);
          vbits = ve.bits || ve.values;
        }
        const bytes = await getBytes(e.files[ci], null);
        const t0 = now();
        const d = decodeColumn(e.meta, bytes, b - a);
        const len = b - a;
        if (d.values && d.values.length < len) throw new Error(`column ${e.meta.key} chunk ${ci}: ${d.values.length} rows, expected ${len}`);
        if (d.values) e.values.set(d.values.subarray(0, len), a);
        if (e.codes && d.codes) e.codes.set(d.codes.subarray(0, len), a);
        if (e.bits && d.bits) e.bits.set(d.bits.subarray(0, len), a);
        if (e.strings && d.strings) {
          if (d.strings.length < len) throw new Error(`column ${e.meta.key} chunk ${ci}: ${d.strings.length} strings, expected ${len}`);
          for (let i = 0; i < len; i++) e.strings[a + i] = d.strings[i];
        }
        if (vbits) {
          for (let i = a; i < b; i++) {
            if (vbits[i]) continue;
            e.values[i] = NaN;
            if (e.codes) e.codes[i] = e.missing;
            if (e.strings) e.strings[i] = null;
          }
        }
        addDecodeMs(now() - t0);
        e.loaded[ci] = 1;
      })();
      e.pending.set(ci, p);
      p.then(
        () => e.pending.delete(ci),
        () => e.pending.delete(ci),
      );
    }
    return p;
  }

  function chunksFor(e, prefix) {
    const m = prefix === undefined || prefix === null ? S.n : Math.max(0, Math.min(S.n, prefix | 0));
    const out = [];
    for (let ci = 0; ci < e.nChunks && e.starts[ci] < m; ci++) out.push(ci);
    return out;
  }

  function isFull(e) {
    for (let ci = 0; ci < e.nChunks; ci++) if (!e.loaded[ci]) return false;
    return true;
  }

  /** Stored keys behind a list of keys / expressions; 'ra'/'dec' map to positions. */
  function expand(keys) {
    const stored = new Set();
    let pos = false;
    for (const k of keys || []) {
      if (S.metas.has(k)) stored.add(k);
      else if (k === 'ra' || k === 'dec') {
        if (!S.man.pos) throw new Error(`catalog ${ds()} has no positions`);
        pos = true;
      } else {
        const c = compileCached(k);
        for (const b of c.keys) {
          if (S.metas.has(b)) stored.add(b);
          else if ((b === 'ra' || b === 'dec') && S.man.pos) pos = true;
          else throw new Error(`unknown column ${b} in ${JSON.stringify(k)}`);
        }
      }
    }
    for (const k of Array.from(stored)) {
      const v = S.metas.get(k).validity;
      if (v && S.metas.has(v)) stored.add(v);
    }
    return {stored: Array.from(stored), pos};
  }

  async function ensure(keys, {prefix, signal, progress} = {}) {
    requireOpen();
    const {stored, pos} = expand(keys);
    const jobs = [];
    for (const k of stored) {
      const e = entryFor(k);
      for (const ci of chunksFor(e, prefix)) if (!e.loaded[ci]) jobs.push([e, ci]);
    }
    const total = jobs.length + (pos && !S.pos ? 1 : 0);
    let loaded = 0;
    const tick = (phase) => {
      loaded++;
      if (progress) progress({ds: ds(), loaded, total, phase});
    };
    if (progress && total) progress({ds: ds(), loaded: 0, total, phase: 'columns'});
    const ps = jobs.map(([e, ci]) => loadChunk(e, ci).then(() => tick('columns')));
    if (pos && !S.pos) ps.push(ensurePos().then(() => tick('pos')));
    await raceAbort(Promise.all(ps), signal);
    return {loaded: total};
  }

  async function ensurePos(signal) {
    if (S.pos) return S.pos;
    if (!S.man.pos) throw new Error(`catalog ${ds()} has no positions`);
    if (!S.posP) {
      S.posP = (async () => {
        const pm = S.man.pos;
        const files = {};
        await Promise.all(Object.entries(pm.files).map(async ([k, rel]) => {
          files[k] = await getBytes(rel, null);
        }));
        const t0 = now();
        const p = decodePos(pm, files, S.man.bbox);
        addDecodeMs(now() - t0);
        if (p.ra.length !== S.n) throw new Error(`positions hold ${p.ra.length} rows, expected ${S.n}`);
        S.pos = p;
        return p;
      })();
      S.posP.catch(() => {
        S.posP = null;
      });
    }
    return raceAbort(S.posP, signal);
  }

  /** Ids files and their chunk starts ({files: [], starts: []} without ids). */
  function idGeom() {
    const im = S.man.ids || {};
    const files = im.file ? [im.file] : im.chunks || [];
    return {files, starts: files.length ? fileStarts(files.length, 'ids') : []};
  }

  /** Ids chunk ci as {start, str} (fetched and decoded once). */
  function idChunk(ci, signal) {
    let p = S.idChunks.get(ci);
    if (!p) {
      const {files, starts} = idGeom();
      const [a, b] = rangeOf(starts, ci);
      p = (async () => {
        const bytes = await getBytes(files[ci], null);
        const t0 = now();
        const str = decodeStr(bytes);
        addDecodeMs(now() - t0);
        if (str.n !== b - a) throw new Error(`ids chunk ${ci}: ${str.n} ids, expected ${b - a}`);
        return {start: a, str};
      })();
      S.idChunks.set(ci, p);
      p.catch(() => {
        if (S.idChunks.get(ci) === p) S.idChunks.delete(ci);
      });
    }
    return raceAbort(p, signal);
  }

  /** Id of row i, loading only the ids chunk that holds it ('' when out of range). */
  async function idAt(i, signal) {
    requireOpen();
    if (S.ids) return S.ids.get(i);
    if (!(Number.isInteger(i) && i >= 0 && i < S.n)) return '';
    const {files, starts} = idGeom();
    if (!files.length) return '';
    const c = await idChunk(chunkIndexOf(starts, i), signal);
    return c.str.get(i - c.start);
  }

  /** Ids of a list of rows, loading only the ids chunks that hold them. */
  async function idsAt(idx, signal) {
    requireOpen();
    const list = Array.from(idx || []);
    if (S.ids) return list.map((i) => S.ids.get(i));
    const {files, starts} = idGeom();
    const ok = (i) => Number.isInteger(i) && i >= 0 && i < S.n && files.length > 0;
    const need = new Set();
    for (const i of list) if (ok(i)) need.add(chunkIndexOf(starts, i));
    const got = new Map();
    await Promise.all(Array.from(need, async (ci) => got.set(ci, await idChunk(ci, signal))));
    return list.map((i) => {
      if (!ok(i)) return '';
      const c = got.get(chunkIndexOf(starts, i));
      return c.str.get(i - c.start);
    });
  }

  /** Every ids chunk plus the alias file as an IdIndex (find, query, export, order, resolveIds). */
  async function ensureIds(signal) {
    if (S.ids) return S.ids;
    if (!S.idsP) {
      S.idsP = (async () => {
        const im = S.man.ids || {};
        const {files} = idGeom();
        const [chunks, aliases] = await Promise.all([
          Promise.all(files.map((_, ci) => idChunk(ci, null))),
          im.alias_file ? fetchJSON(resolve(im.alias_file), fopts) : null,
        ]);
        S.ids = new IdIndex(chunks, aliases);
        return S.ids;
      })();
      S.idsP.catch(() => {
        S.idsP = null;
      });
    }
    return raceAbort(S.idsP, signal);
  }

  async function ensureLimits(key) {
    const meta = S.metas.get(key);
    if (!meta || !meta.limits || !meta.limits.file) return null;
    if (!S.limits.has(key)) {
      const p = getBytes(meta.limits.file, null).then((b) => decodeSparse(b));
      S.limits.set(key, p);
      p.catch(() => S.limits.delete(key));
    }
    return S.limits.get(key);
  }

  function prefixLen(prefix) {
    return prefix === undefined || prefix === null ? S.n : Math.max(0, Math.min(S.n, prefix | 0));
  }

  /** Float32 values of a key or expression (columns must be ensured). Shared cache arrays. */
  function valuesOf(expr) {
    if (S.metas.has(expr)) return S.cols.get(expr).values;
    if (expr === 'ra' || expr === 'dec') {
      const src = S.pos[expr];
      const out = new Float32Array(S.n);
      for (let i = 0; i < S.n; i++) out[i] = src[i];
      return out;
    }
    const hit = S.exprCache.get(expr);
    if (hit) {
      S.exprCache.delete(expr);
      S.exprCache.set(expr, hit);
      return hit;
    }
    const c = compileCached(expr);
    const cols = Object.create(null);
    let full = true;
    for (const k of c.keys) {
      if (k === 'ra' || k === 'dec') {
        cols[k] = S.pos[k];
      } else {
        const e = S.cols.get(k);
        cols[k] = e.values;
        if (!isFull(e)) full = false;
      }
    }
    const v = evaluate(c, cols, S.n);
    if (full) {
      S.exprCache.set(expr, v);
      while (S.exprCache.size > EXPR_CACHE) S.exprCache.delete(S.exprCache.keys().next().value);
    }
    return v;
  }

  // ---------------------------------------------------------------- masks / views

  function maskOf(m) {
    if (m === 'vis') return S.vis;
    if (m === 'sel') return S.sel || new Uint8Array(S.n);
    if (m && ArrayBuffer.isView(m)) return m;
    return null;
  }

  async function xyView(view, signal) {
    const key = `${view.x}\u0000${view.y}\u0000${!!view.xlog}\u0000${!!view.ylog}\u0000${S.visVersion}`;
    let v = S.views.get(key);
    if (v) return v;
    await ensure([view.x, view.y], {signal});
    const x = valuesOf(view.x), y = valuesOf(view.y);
    const drawable = drawableXY(x, y, S.vis, {xlog: !!view.xlog, ylog: !!view.ylog});
    v = {x, y, drawable, grid: null, lx: null, ly: null};
    S.views.set(key, v);
    while (S.views.size > 6) S.views.delete(S.views.keys().next().value);
    return v;
  }

  /** Projection cache key: tokens are numbered per pane, so the pane is part of the key. */
  function projKey(pane, token) {
    return `${pane === undefined || pane === null ? '' : String(pane)}\u0000${token}`;
  }

  function projView(view) {
    // A projection registered without a pane is shared by every pane (fallback lookup).
    const p = S.proj.get(projKey(view.pane, view.token)) || S.proj.get(projKey(null, view.token));
    if (!p) throw new Error(`unknown projection token ${view.token}${view.pane ? ` for pane ${view.pane}` : ''}`);
    if (!p.drawable) p.drawable = drawableProj(p.vis, p.x, p.y);
    return p;
  }

  // ---------------------------------------------------------------- rows

  async function rowChunk(ci, signal) {
    let p = S.rowChunks.get(ci);
    if (!p) {
      p = fetchJSON(resolve(S.man.rows.files[ci]), {...fopts});
      S.rowChunks.set(ci, p);
      p.catch(() => S.rowChunks.delete(ci));
      while (S.rowChunks.size > ROW_CACHE) S.rowChunks.delete(S.rowChunks.keys().next().value);
    } else {
      S.rowChunks.delete(ci);
      S.rowChunks.set(ci, p);
    }
    return raceAbort(p, signal);
  }

  function storedKeys() {
    return S.man.cols.map((c) => c.key);
  }

  /** Float64 value of row i of a column source (codes dequantized for the quantized encodings). */
  function numberAt(e, i) {
    const j = i - (e.base || 0);
    if (e.codes && CODE_DEQUANT.has(e.meta.enc)) return dequant(e.meta, e.codes[j]);
    return e.values ? e.values[j] : NaN;
  }

  function cellValue(e, i) {
    const enc = e.meta.enc;
    const j = i - (e.base || 0);
    if (enc === 'cat8' || enc === 'cat16') {
      const code = e.codes[j];
      return code === e.missing ? null : (e.levels && e.levels[code] !== undefined ? e.levels[code] : code);
    }
    if (enc === 'bits') return e.bits[j];
    if (enc === 'str') return e.strings[j];
    if (enc === 'u32') return e.codes[j] === U32_MISSING ? null : e.codes[j];
    return quantizedNumber(numberAt(e, i), e.meta);
  }

  /** Decode chunk ci of a column into chunk-local arrays (validity applied), without a full-n entry. */
  async function decodeChunkLocal(key, ci, signal) {
    const meta = S.metas.get(key);
    const files = meta.chunk_files || (meta.file ? [meta.file] : []);
    const [a, b] = rangeOf(fileStarts(files.length, `column ${key}`), ci);
    const len = b - a;
    const bytes = await getBytes(files[ci], signal);
    const t0 = now();
    const d = decodeColumn(meta, bytes, len);
    addDecodeMs(now() - t0);
    const src = {
      meta,
      base: a,
      codes: d.codes ? d.codes.subarray(0, len) : null,
      values: d.values ? d.values.subarray(0, len) : null,
      bits: d.bits ? d.bits.subarray(0, len) : null,
      strings: d.strings ? d.strings.slice(0, len) : null,
      levels: meta.levels || null,
      missing: missingCode(meta.enc),
    };
    if (meta.validity && S.metas.has(meta.validity)) {
      const vs = await cellSource(meta.validity, a, signal);
      for (let i = a; i < b; i++) {
        const j = i - (vs.base || 0);
        const ok = vs.bits ? vs.bits[j] : vs.values[j];
        if (ok) continue;
        const k = i - a;
        if (src.values) src.values[k] = NaN;
        if (src.codes) src.codes[k] = src.missing;
        if (src.strings) src.strings[k] = null;
      }
    }
    return src;
  }

  /**
   * Column source holding row i: the full entry when that chunk is already decoded (or the column
   * has one file), else a cached chunk-local decode of only the chunk that contains row i.
   */
  async function cellSource(key, i, signal) {
    const e = S.cols.get(key);
    const meta = S.metas.get(key);
    const nChunks = (meta.chunk_files || (meta.file ? [meta.file] : [])).length;
    if (nChunks <= 1) {
      await ensure([key], {signal});
      return S.cols.get(key);
    }
    const ci = chunkIndexOf(fileStarts(nChunks, `column ${key}`), i);
    if (e && e.loaded[ci]) return e;
    const ck = `${key}\u0000${ci}`;
    let p = S.qchunks.get(ck);
    if (p) {
      S.qchunks.delete(ck);
      S.qchunks.set(ck, p);
    } else {
      p = decodeChunkLocal(key, ci, null);
      S.qchunks.set(ck, p);
      p.catch(() => S.qchunks.delete(ck));
      while (S.qchunks.size > QCHUNK_CACHE) S.qchunks.delete(S.qchunks.keys().next().value);
    }
    return raceAbort(p, signal);
  }

  /**
   * Rows [a, b) from the column chunks that hold them (no row chunks). Each row carries its
   * catalog id as `_id` (a stored column called 'id' cannot overwrite it; `id` holds the stored
   * column when there is one, else the catalog id). ra/dec come from pos only when pos is
   * already decoded or `wantPos` is set: pos is one whole-catalog file, so a first-screen or hover
   * read never pulls it (IMPLEMENTATION_NOTES 99). Rows whose ra/dec were withheld carry
   * ra = dec = null and `_posDeferred: true`.
   */
  async function quantizedRows(a, b, signal, wantPos = false) {
    const idx = [];
    for (let i = a; i < b; i++) idx.push(i);
    const [idList] = await Promise.all([idsAt(idx, signal), S.man.pos && wantPos ? ensurePos(signal) : null]);
    const out = [];
    const starts = S.starts;
    for (let c0 = a; c0 < b;) {
      const ci = chunkIndexOf(starts, c0);
      const c1 = Math.min(b, rangeOf(starts, ci)[1]);
      const srcs = await Promise.all(S.man.cols.map((c) => cellSource(c.key, c0, signal)));
      for (let i = c0; i < c1; i++) {
        const cid = idList[i - a];
        const o = {id: cid};
        if (S.man.pos) {
          if (S.pos) {
            o.ra = Number.isFinite(S.pos.ra[i]) ? S.pos.ra[i] : null;
            o.dec = Number.isFinite(S.pos.dec[i]) ? S.pos.dec[i] : null;
          } else {
            o.ra = null;
            o.dec = null;
            o._posDeferred = true;
          }
        }
        S.man.cols.forEach((c, k) => {
          o[c.key] = cellValue(srcs[k], i);
        });
        o._id = cid === null || cid === undefined ? null : String(cid);
        o._quantized = true;
        o._i = i;
        out.push(o);
      }
      c0 = c1;
    }
    return out;
  }

  /**
   * Rows [i0, i1): exact row chunks when the manifest has them, else the quantized fallback.
   * opts.pos: on the quantized fallback, load pos (one whole file) so ra/dec are filled; without
   * it ra/dec are filled only when pos is already decoded.
   */
  async function rows(i0, i1, signal, {pos = false} = {}) {
    requireOpen();
    const a = Math.max(0, Math.min(S.n, Math.floor(+i0 || 0)));
    const b = Math.max(a, Math.min(S.n, Math.floor(+i1 || 0)));
    if (b <= a) return [];
    if (S.man.rows && S.man.rows.files && S.man.rows.files.length) {
      const ch = S.man.rows.chunk || 4096;
      const out = [];
      for (let ci = Math.floor(a / ch); ci * ch < b; ci++) {
        const chunk = await rowChunk(ci, signal);
        const s = Math.max(a, ci * ch), t = Math.min(b, (ci + 1) * ch);
        for (let i = s; i < t; i++) {
          const r = chunk[i - ci * ch];
          out.push({...(r || {}), _i: i});
        }
      }
      return out;
    }
    // Quantized fallback: only the column and ids chunks that contain [a, b) are fetched.
    return quantizedRows(a, b, signal, !!pos);
  }

  /**
   * Id carried by a rows() row: `_id` of a quantized row (from the ids chunk), or the first
   * row-chunk column (the id key) of an exact row.
   */
  function carriedId(r) {
    if (r._quantized) return r._id === null || r._id === undefined ? null : String(r._id);
    const key = S.man.rows && Array.isArray(S.man.rows.cols) ? S.man.rows.cols[0] : null;
    if (!key || !Object.prototype.hasOwnProperty.call(r, key)) return null;
    const v = r[key];
    return v === null || v === undefined ? null : String(v);
  }

  /** Keys a row-chunk row carries: `rows.cols` when the manifest lists it, else the row's own keys. */
  function rowChunkKeys(r) {
    if (S.man.rows && Array.isArray(S.man.rows.cols)) return S.man.rows.cols;
    return Object.keys(r).filter((k) => k !== '_i');
  }

  /**
   * User stored columns an exact row-chunk row omits (columns with `exact: false` are left out of
   * row chunks). Internal columns (keys starting with '_', e.g. the single-file `_src_index` of a
   * reordered catalog) are never merged: they are not displayed and a single-file column would be
   * loaded whole (IMPLEMENTATION_NOTES 99).
   */
  function omittedKeys(r) {
    const has = new Set(rowChunkKeys(r));
    return S.man.cols.filter((c) => !c.key.startsWith('_') && !has.has(c.key)).map((c) => c.key);
  }

  /**
   * True when the row chunk carries no positions although the catalog has pos. The builder puts
   * the configured ra/dec input columns (any name, e.g. RAJ2000) right after the id key and never
   * stores them as columns when pos exists; the only row keys that are not stored columns are the
   * id, ra/dec and rows-only text/url/image columns. So a row chunk whose keys, apart from the id
   * (first key), are all stored columns lacks ra/dec (they were declared `exact: false`). Any other
   * extra key counts as a position, so an ambiguous row chunk never pulls pos.
   */
  function rowChunkLacksPos(r) {
    if (!S.man.pos) return false;
    const stored = new Set(S.man.cols.map((c) => c.key));
    return rowChunkKeys(r).slice(1).every((k) => stored.has(k));
  }

  /**
   * Complete an exact row-chunk row with the quantized cell values of the user stored columns the
   * row chunk omits (read through cellSource, i.e. only the column chunk that holds row i) and list
   * those keys in `_qkeys`. When the row chunk carries no positions (rowChunkLacksPos) and the
   * catalog has pos, ra/dec come from pos as for quantized rows (`_posDeferred` until pos is
   * decoded or `wantPos` asks for it); they are quantized too and listed in `_qkeys`.
   */
  async function mergeQuantized(r, i, signal, wantPos) {
    const keys = omittedKeys(r);
    const posGap = rowChunkLacksPos(r);
    if (!keys.length && !posGap) return;
    const [srcs] = await Promise.all([
      Promise.all(keys.map((k) => cellSource(k, i, signal))),
      posGap && wantPos ? ensurePos(signal) : null,
    ]);
    const q = [];
    keys.forEach((k, j) => {
      r[k] = cellValue(srcs[j], i);
      q.push(k);
    });
    if (posGap) {
      if (S.pos) {
        r.ra = Number.isFinite(S.pos.ra[i]) ? S.pos.ra[i] : null;
        r.dec = Number.isFinite(S.pos.dec[i]) ? S.pos.dec[i] : null;
        q.push('ra', 'dec');
      } else {
        r.ra = null;
        r.dec = null;
        r._posDeferred = true;
      }
    }
    if (q.length) r._qkeys = q;
  }

  /**
   * One row with `_id` and `_limits`. opts.pos as for rows(). An exact row-chunk row also carries
   * the quantized values of the stored columns the row chunk omits, listed in `_qkeys`
   * (mergeQuantized).
   */
  async function record(i, signal, {pos = false} = {}) {
    const [r] = await rows(i, i + 1, signal, {pos});
    if (!r) return null;
    if (!r._quantized) await mergeQuantized(r, r._i, signal, !!pos);
    // the row chunk carries the id: no ids file is read (IMPLEMENTATION_NOTES 99)
    const cid = carriedId(r);
    r._id = cid !== null ? cid : await idAt(r._i, signal);
    const lim = {};
    for (const c of S.man.cols) {
      if (!c.limits) continue;
      const sp = await raceAbort(ensureLimits(c.key), signal);
      const k = sp ? sparseKind(sp, i) : 0;
      if (k) lim[c.key] = k;
    }
    if (Object.keys(lim).length) r._limits = lim;
    return r;
  }

  // ---------------------------------------------------------------- export

  async function exportTable({mask = 'all', cols = 'all', exact = false, format = 'csv', r = 1} = {}, signal) {
    requireOpen();
    const mk = maskOf(mask);
    const idx = [];
    for (let i = 0; i < S.n; i++) if (!mk || mk[i]) idx.push(i);
    await ensureIds(signal);
    if (format === 'ids') return {text: toIdList(idx.map((i) => S.ids.get(i))), mime: MIME.ids, count: idx.length};
    if (format === 'ds9') {
      if (!S.man.pos) throw new Error(`catalog ${ds()} has no positions`);
      await ensurePos(signal);
      const items = idx.map((i) => ({id: S.ids.get(i), ra: S.pos.ra[i], dec: S.pos.dec[i]}));
      return {text: toDS9(items, {r}), mime: MIME.ds9, count: idx.length};
    }
    const want = cols === 'all' ? storedKeys().filter((k) => !k.startsWith('_')) : Array.from(cols || []);
    const header = [];
    const getters = [];
    const numeric = [];
    const rowsCfg = S.man.rows && S.man.rows.files && S.man.rows.files.length ? S.man.rows : null;
    let rowKeys = new Set();
    let chunkCache = null;
    if (exact && rowsCfg) {
      rowKeys = new Set(rowsCfg.cols || []);
      const ch = rowsCfg.chunk || 4096;
      chunkCache = new Map();
      const need = new Set(idx.map((i) => Math.floor(i / ch)));
      for (const ci of need) chunkCache.set(ci, await rowChunk(ci, signal));
      const idKey = (rowsCfg.cols || [])[0];
      const rowOf = (i) => chunkCache.get(Math.floor(i / ch))[i - Math.floor(i / ch) * ch] || {};
      if (idKey) {
        header.push(idKey);
        getters.push((i) => formatValue(rowOf(i)[idKey], {}, {exact: true}));
      }
      for (const k of rowsCfg.cols.slice(1)) {
        if (cols !== 'all' && !want.includes(k)) continue;
        header.push(k);
        getters.push((i) => formatValue(rowOf(i)[k], {}, {exact: true}));
        numeric.push(k);
      }
    } else {
      header.push('id');
      getters.push((i) => S.ids.get(i));
      if (S.man.pos) {
        await ensurePos(signal);
        header.push('ra', 'dec');
        getters.push((i) => (Number.isFinite(S.pos.ra[i]) ? String(Number(S.pos.ra[i].toFixed(7))) : ''));
        getters.push((i) => (Number.isFinite(S.pos.dec[i]) ? String(Number(S.pos.dec[i].toFixed(7))) : ''));
        numeric.push('ra', 'dec');
      }
    }
    const rest = want.filter((k) => !rowKeys.has(k) && !header.includes(k));
    if (rest.length) await ensure(rest, {signal});
    for (const k of rest) {
      header.push(k);
      if (S.metas.has(k)) {
        const e = S.cols.get(k);
        const enc = e.meta.enc;
        if (enc === 'cat8' || enc === 'cat16' || enc === 'str') getters.push((i) => {
          const v = cellValue(e, i);
          return v === null ? '' : String(v);
        });
        else {
          numeric.push(k);
          if (enc === 'bits') getters.push((i) => String(e.bits[i]));
          else {
            const fm = fmtMeta(e.meta);
            getters.push((i) => formatValue(numberAt(e, i), fm));
          }
        }
      } else {
        const v = valuesOf(k);
        numeric.push(k);
        getters.push((i) => formatValue(v[i], {enc: 'f32'}));
      }
    }
    const rowsOut = idx.map((i) => getters.map((g) => g(i)));
    if (format === 'json') return {text: toJSON(header, rowsOut, {numeric}), mime: MIME.json, count: idx.length};
    // No comment lines: astropy ascii.csv and pandas read_csv take the first line as the header.
    // Whether values are exact or quantized is reported in `exact`.
    return {text: toCSV(header, rowsOut), mime: MIME.csv, count: idx.length, exact: !!(exact && rowsCfg)};
  }

  // ---------------------------------------------------------------- gpu columns

  async function gpuColumns(keys, prefix, signal) {
    await ensure(keys, {prefix, signal});
    const m = prefixLen(prefix);
    const raw = [], A = new Float64Array(keys.length), B = new Float64Array(keys.length);
    keys.forEach((k, d) => {
      const meta = S.metas.get(k);
      if (meta && meta.enc === 'u16q') {
        const st = stepOf(meta);
        raw.push(S.cols.get(k).codes.slice(0, m));
        A[d] = 65535 * st;
        B[d] = +meta.lo - st;
      } else if (meta && meta.enc === 'u8q') {
        const st = stepOf(meta);
        raw.push(Uint16Array.from(S.cols.get(k).codes.subarray(0, m)));
        A[d] = 65535 * st;
        B[d] = +meta.lo - st;
      } else {
        // requantize decoded values to u16 over their finite range (code 0 = missing)
        const v = valuesOf(k);
        let lo = Infinity, hi = -Infinity;
        for (let i = 0; i < m; i++) {
          const a = v[i];
          if (a === a && a !== Infinity && a !== -Infinity) {
            if (a < lo) lo = a;
            if (a > hi) hi = a;
          }
        }
        const out = new Uint16Array(m);
        if (lo <= hi) {
          const st = hi > lo ? (hi - lo) / 65534 : 1;
          for (let i = 0; i < m; i++) {
            const a = v[i];
            if (a === a && a !== Infinity && a !== -Infinity) out[i] = 1 + Math.min(65534, Math.max(0, Math.round((a - lo) / st)));
          }
          A[d] = 65535 * st;
          B[d] = lo - st;
        } else {
          A[d] = 65535;
          B[d] = -1;
        }
        raw.push(out);
      }
    });
    return withTransfer({n: m, raw, A, B, missing0: true, keys: keys.slice()}, raw);
  }

  // ---------------------------------------------------------------- order

  /** Sequential-friendly id getter over the IdIndex chunks (O(1) amortized for ascending i). */
  function idGetter(ix) {
    const chunks = ix.chunks;
    let c = chunks[0] || null;
    let ci = 0;
    return (i) => {
      if (!c || i < c.start || i >= c.start + c.str.n) {
        c = null;
        for (ci = 0; ci < chunks.length; ci++) {
          const k = chunks[ci];
          if (i >= k.start && i < k.start + k.str.n) {
            c = k;
            break;
          }
        }
        if (!c) return null;
      }
      return c.str.get(i - c.start);
    };
  }

  /**
   * Argsort of all rows by `key` (stored column, 'ra'/'dec', expression, or 'id' / '__id' for
   * catalog ids; a stored column called 'id' wins over the alias, '__id' is always the ids) in
   * direction `dir`; missing values last in both directions, ties in row order.
   */
  async function order(key, dir, signal, progress) {
    requireOpen();
    const d = normDir(dir);
    const k = String(key);
    const ck = `${k}\u0000${d}`;
    const hit = S.orders.get(ck);
    if (hit) {
      S.orders.delete(ck);
      S.orders.set(ck, hit);
      return hit;
    }
    let out;
    if (ID_ORDER_KEYS.includes(k) && !(k === 'id' && S.metas.has('id'))) {
      const ix = await ensureIds(signal);
      // ASCII id tables are keyed straight from the UTF-8 bytes (no per-row decode)
      const sources = ix.chunks.map((c) => ({start: c.start, n: c.str.n, offsets: c.str.offsets, blob: c.str.blob}));
      out = argsortNatural(S.n, idGetter(ix), d, {sources});
    } else {
      await ensure([k], {signal, progress});
      throwIfAborted(signal);
      const meta = S.metas.get(k);
      if (meta) {
        const e = S.cols.get(k);
        const enc = meta.enc;
        if (enc === 'cat8' || enc === 'cat16') out = argsortCategory(e.codes, meta.levels || [], e.missing, d);
        // section 5.3: the empty string is the missing marker of a `str` column (sorts last)
        else if (enc === 'str') {
          out = argsortNatural(S.n, (i) => {
            const s = e.strings[i];
            return s === '' ? null : s;
          }, d);
        }
        else if (CODE_ORDER.has(enc) && e.codes) out = argsortCodes(e.codes, e.missing, d);
        else out = argsortValues(e.values, d);
      } else if (k === 'ra' || k === 'dec') {
        out = argsortValues(S.pos[k], d);
      } else {
        out = argsortValues(valuesOf(k), d);
      }
    }
    S.orders.set(ck, out);
    while (S.orders.size > ORDER_CACHE) S.orders.delete(S.orders.keys().next().value);
    return out;
  }

  // ---------------------------------------------------------------- handlers

  const handlers = {
    async open({url}, ctx) {
      const abs = new URL(url, (typeof location !== 'undefined' && location.href) || undefined).toString();
      const signal = ctx && ctx.signal;
      let man = null;
      const pf = opts.prefetch;
      if (pf && pf.url === abs) {
        // the manifest request started from top-level worker code (IMPLEMENTATION_NOTES 101)
        try {
          man = await raceAbort(pf.promise, signal);
        } catch (err) {
          // a failed prefetch (HTTP or network) is retried once by the normal fetch below
          if (!(err instanceof FetchError)) throw err;
          man = null;
        }
      }
      if (!man) man = await fetchJSON(abs, {...fopts, signal});
      if (!man || !Array.isArray(man.cols) || !Number.isInteger(man.n)) throw new Error(`${url}: not a pk-catalog manifest`);
      const starts = catalogChunkStarts(man);
      S.url = abs;
      S.man = man;
      S.n = man.n;
      S.starts = starts;
      S.metas = new Map(man.cols.map((c) => [c.key, c]));
      S.cols.clear();
      S.qchunks.clear();
      S.rowChunks.clear();
      S.idChunks.clear();
      S.ids = null;
      S.idsP = null;
      S.pos = null;
      S.posP = null;
      S.decIndex = null;
      return {n: man.n, manifest: man, url: S.url};
    },

    async info() {
      requireOpen();
      return {n: S.n, visVersion: S.visVersion, loaded: Array.from(S.cols.keys()).filter((k) => isFull(S.cols.get(k)))};
    },

    async ensure({keys, prefix}, ctx) {
      return ensure(keys, {prefix, signal: ctx.signal, progress: ctx.progress});
    },

    async values({expr, prefix}, ctx) {
      await ensure([expr], {prefix, signal: ctx.signal, progress: ctx.progress});
      const m = prefixLen(prefix);
      const v = valuesOf(expr).slice(0, m);
      return withTransfer(v, [v]);
    },

    async project({spec, prefix, token, pane}, ctx) {
      const ns = normSpec(spec);
      await ensure(ns.keys, {prefix, signal: ctx.signal, progress: ctx.progress});
      const m = prefixLen(prefix);
      const src = {
        n: m,
        meta: (k) => S.metas.get(k) || null,
        codes: (k) => S.cols.get(k).codes.subarray(0, m),
        values: (k) => valuesOf(k).subarray(0, m),
      };
      const mask = S.vis ? S.vis.subarray(0, m) : null;
      const p = project(spec, src, mask);
      if (token !== undefined && token !== null) S.proj.set(projKey(pane, token), {x: p.x, y: p.y, vis: p.vis, drawable: null, grid: null});
      const out = {x: p.x.slice(), y: p.y.slice(), vis: p.vis.slice(), path: p.path, keys: p.keys};
      return withTransfer(out, [out.x, out.y, out.vis]);
    },

    async pick({view, px, py, rPx = 6}, ctx) {
      const {kx, ky} = pickScale(view);
      let x, y, drawable, holder;
      let qx = +px, qy = +py;
      if (view.kind === 'proj') {
        holder = projView(view);
        ({x, y, drawable} = holder);
      } else {
        holder = await xyView(view, ctx.signal);
        drawable = holder.drawable;
        if (view.xlog) {
          if (!holder.lx) holder.lx = holder.x.map((a) => (a > 0 ? Math.log10(a) : NaN));
          qx = qx > 0 ? Math.log10(qx) : NaN;
        }
        if (view.ylog) {
          if (!holder.ly) holder.ly = holder.y.map((a) => (a > 0 ? Math.log10(a) : NaN));
          qy = qy > 0 ? Math.log10(qy) : NaN;
        }
        x = view.xlog ? holder.lx : holder.x;
        y = view.ylog ? holder.ly : holder.y;
      }
      if (!holder.grid) holder.grid = buildPickGrid(x, y, drawable);
      return pickNearest(x, y, drawable, qx, qy, +rPx, {kx, ky, grid: holder.grid});
    },

    async brush({view, shape}, ctx) {
      let res, drawable;
      if (view.kind === 'proj') {
        const p = projView(view);
        drawable = p.drawable;
        res = brushArrays(p.x, p.y, drawable, shape);
      } else {
        const v = await xyView(view, ctx.signal);
        drawable = v.drawable;
        res = brushArrays(v.x, v.y, drawable, shape, {xlog: !!view.xlog, ylog: !!view.ylog});
      }
      const out = {hit: res.hit, drawable: drawable.slice(), count: res.count};
      return withTransfer(out, [out.hit, out.drawable]);
    },

    async query({filters, apply = false, seq = null}, ctx) {
      requireOpen();
      const need = filterNeeds(filters, S.man.cols);
      await ensure([...need.cols, ...need.exprs], {signal: ctx.signal, progress: ctx.progress});
      if (need.ids) await ensureIds(ctx.signal);
      const res = applyFilters(filters, {
        n: S.n,
        cols: S.man.cols,
        col: (k) => S.cols.get(k) || null,
        expr: (e) => valuesOf(e),
        textMatch: (t) => S.ids.matchMask(t),
        idRows: (ids) => S.ids.rowsOf(ids),
      });
      // Handlers run concurrently: an older applying query whose column fetch finishes late must
      // not overwrite the mask of a newer one. Applying queries carry a monotonically increasing
      // `seq` (DataClient numbers them); only a seq newer than the last applied one sets S.vis.
      let applied = false;
      if (apply) {
        const sq = seq === null || seq === undefined ? null : +seq;
        if (sq === null || !(sq <= S.visSeq)) {
          if (sq !== null) S.visSeq = sq;
          S.vis = res.active ? res.vis : null;
          S.visVersion++;
          S.views.clear();
          applied = true;
        }
      }
      const out = {vis: res.vis.slice(), count: res.count, active: res.active, version: S.visVersion, applied};
      return withTransfer(out, [out.vis]);
    },

    async setSel({mask, version}) {
      requireOpen();
      if (mask === null || mask === undefined) S.sel = null;
      else {
        if (mask.length !== S.n) throw new Error(`selection mask has ${mask.length} rows, expected ${S.n}`);
        S.sel = mask;
      }
      S.selVersion = version ?? S.selVersion + 1;
      return {version: S.selVersion};
    },

    async vis() {
      requireOpen();
      const out = {vis: S.vis ? S.vis.slice() : null, version: S.visVersion};
      return out.vis ? withTransfer(out, [out.vis]) : out;
    },

    async hist({expr, bins = 64, range = null, mask = null, log = false}, ctx) {
      await ensure([expr], {signal: ctx.signal, progress: ctx.progress});
      const h = histogram(valuesOf(expr), bins, {range, mask: maskOf(mask), log});
      return withTransfer(h, [h.edges, h.counts]);
    },

    async bin2d({view, nx = 256, ny = 256, mask = 'vis', xr = null, yr = null}, ctx) {
      let x, y, m = maskOf(mask);
      if (view.kind === 'proj') {
        const p = projView(view);
        x = p.x;
        y = p.y;
        const mm = new Uint8Array(p.vis.length);
        for (let i = 0; i < mm.length; i++) mm[i] = p.vis[i] && (!m || m[i]) ? 1 : 0;
        m = mm;
      } else {
        const v = await xyView(view, ctx.signal);
        x = view.xlog ? v.x.map((a) => (a > 0 ? Math.log10(a) : NaN)) : v.x;
        y = view.ylog ? v.y.map((a) => (a > 0 ? Math.log10(a) : NaN)) : v.y;
      }
      const r = bin2d(x, y, nx, ny, {xr, yr, mask: m});
      return withTransfer(r, [r.counts]);
    },

    async order({key, dir = 1}, ctx) {
      const o = await order(key, dir, ctx.signal, ctx.progress);
      const copy = o.slice();
      return withTransfer(copy, [copy]);
    },

    async rows({i0, i1, pos = false}, ctx) {
      return rows(i0, i1, ctx.signal, {pos});
    },

    async record({i, pos = false}, ctx) {
      return record(i, ctx.signal, {pos});
    },

    async id({i}, ctx) {
      return idAt(i, ctx.signal);
    },

    async ids({idx}, ctx) {
      return idsAt(idx, ctx.signal);
    },

    /**
     * Rows of a list of ids in one pass over every ids chunk (IMPLEMENTATION_NOTES 102): exact id
     * first (first stored row wins), then, with `aliases` (default true), the alias file.
     * Returns an Int32Array (-1 when missing).
     */
    async resolveIds({ids, aliases = true}, ctx) {
      requireOpen();
      const list = Array.from(ids || [], (s) => String(s));
      if (!list.length) return new Int32Array(0);
      const ix = await ensureIds(ctx.signal);
      const out = ix.rowsOf(list, {aliases: aliases !== false});
      return withTransfer(out, [out]);
    },

    async find({text, limit = 50}, ctx) {
      requireOpen();
      await ensureIds(ctx.signal);
      let pos = null;
      if (S.man.pos) {
        await ensurePos(ctx.signal);
        if (!S.decIndex) S.decIndex = buildDecIndex(S.pos.ra, S.pos.dec);
        pos = {ra: S.pos.ra, dec: S.pos.dec, index: S.decIndex};
      }
      return findRows(text, S.ids, pos, {limit});
    },

    async crossmatch({ra, dec, rArcsec}, ctx) {
      requireOpen();
      await ensurePos(ctx.signal);
      if (!S.decIndex) S.decIndex = buildDecIndex(S.pos.ra, S.pos.dec);
      const out = crossmatch(S.pos.ra, S.pos.dec, ra, dec, rArcsec, S.decIndex);
      return withTransfer(out, [out]);
    },

    async skyWindow({win}, ctx) {
      requireOpen();
      await ensurePos(ctx.signal);
      const w = {...win, cap: win.cap ?? SKY_MARKER_CAP};
      const r = skyWindow(S.pos.ra, S.pos.dec, w, {mask: maskOf(win.mask ?? null), order: S.man.order});
      return withTransfer(r, [r.idx]);
    },

    async exportCSV(args, ctx) {
      return exportTable({...args, format: 'csv'}, ctx.signal);
    },

    async export(args, ctx) {
      return exportTable(args, ctx.signal);
    },

    async gpuColumns({keys, prefix}, ctx) {
      return gpuColumns(keys, prefix, ctx.signal);
    },

    async density({key}, ctx) {
      requireOpen();
      const d = (S.man.density || []).find((e) => e.key === key);
      if (!d) throw new Error(`catalog ${ds()} has no density grid ${key}`);
      const b = await getBytes(d.file, ctx.signal);
      throwIfAborted(ctx.signal);
      const g = decodeDensity(d, b);
      g.total = d.total;
      return withTransfer(g, [g.values]);
    },
  };

  return {handlers, state: S};
}

// Running as a module worker: serve the engine on the worker scope.
if (typeof WorkerGlobalScope !== 'undefined' && typeof self !== 'undefined' && self instanceof WorkerGlobalScope) {
  const prefetch = startPrefetch(self.location.href);
  serve(self, createEngine({prefetch}).handlers, {getStats});
}

