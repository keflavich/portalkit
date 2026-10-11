// Data worker (ARCHITECTURE.md section 6.6): one module worker per catalog dataset. It fetches
// and decodes column files (chunk-aware), positions, ids and row chunks, and serves the
// DataClient ops over workers/rpc.js:
//   open, ensure, values, project, pick, brush, query, hist, bin2d, rows, find, crossmatch,
//   skyWindow, exportCSV, gpuColumns, cancel
// plus id, record, export (csv|json|ids|ds9), setSel, density, info and order (argsort over all
// rows, IMPLEMENTATION_NOTES 74).
// createEngine() holds the state and is also used in-thread (createLocalRpc) when Workers are
// unavailable, and by the node tests.

import {CHUNK_ROWS, SKY_MARKER_CAP} from '../core/constants.js';
import {stepDigits} from '../core/fmt.js';
import {decodeColumn, decodePos, decodeStr, decodeSparse, sparseKind, decodeDensity, stepOf, dequant, CAT8_MISSING, CAT16_MISSING} from '../data/codec.js';
import {fetchBytes, fetchJSON, getStats, addDecodeMs, now, abortError, throwIfAborted} from '../data/fetch.js';
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
    chunkRows: CHUNK_ROWS,
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
      chunkRows: nChunks > 1 ? S.chunkRows : n,
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
    const a = ci * e.chunkRows;
    return [a, Math.min(S.n, a + e.chunkRows)];
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
          await loadChunk(ve, ci);
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
    for (let ci = 0; ci < e.nChunks; ci++) {
      const [a] = chunkRange(e, ci);
      if (a < m) out.push(ci);
    }
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

  async function ensureIds(signal) {
    if (S.ids) return S.ids;
    if (!S.idsP) {
      S.idsP = (async () => {
        const im = S.man.ids || {};
        const files = im.file ? [im.file] : im.chunks || [];
        const chunks = [];
        let start = 0;
        for (const rel of files) {
          const str = decodeStr(await getBytes(rel, null));
          chunks.push({start, str});
          start += str.n;
        }
        let aliases = null;
        if (im.alias_file) aliases = await fetchJSON(resolve(im.alias_file), fopts);
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
    const a = ci * S.chunkRows, b = Math.min(S.n, a + S.chunkRows);
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
    const ci = Math.min(nChunks - 1, Math.floor(i / S.chunkRows));
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

  async function quantizedRows(a, b, signal) {
    await Promise.all([ensureIds(signal), S.man.pos ? ensurePos(signal) : null]);
    const out = [];
    const ch = S.chunkRows;
    for (let c0 = a; c0 < b; c0 = (Math.floor(c0 / ch) + 1) * ch) {
      const c1 = Math.min(b, (Math.floor(c0 / ch) + 1) * ch);
      const srcs = await Promise.all(S.man.cols.map((c) => cellSource(c.key, c0, signal)));
      for (let i = c0; i < c1; i++) {
        const o = {id: S.ids.get(i)};
        if (S.pos) {
          o.ra = Number.isFinite(S.pos.ra[i]) ? S.pos.ra[i] : null;
          o.dec = Number.isFinite(S.pos.dec[i]) ? S.pos.dec[i] : null;
        }
        S.man.cols.forEach((c, k) => {
          o[c.key] = cellValue(srcs[k], i);
        });
        o._quantized = true;
        o._i = i;
        out.push(o);
      }
    }
    return out;
  }

  async function rows(i0, i1, signal) {
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
    // Quantized fallback: only the column chunks that contain [a, b) are fetched.
    return quantizedRows(a, b, signal);
  }

  async function record(i, signal) {
    const [r] = await rows(i, i + 1, signal);
    if (!r) return null;
    await ensureIds(signal);
    r._id = S.ids.get(i);
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
      S.url = new URL(url, (typeof location !== 'undefined' && location.href) || undefined).toString();
      const man = await fetchJSON(S.url, {...fopts, signal: ctx && ctx.signal});
      if (!man || !Array.isArray(man.cols) || !Number.isInteger(man.n)) throw new Error(`${url}: not a pk-catalog manifest`);
      S.man = man;
      S.n = man.n;
      S.chunkRows = man.chunk_rows || CHUNK_ROWS;
      S.metas = new Map(man.cols.map((c) => [c.key, c]));
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

    async rows({i0, i1}, ctx) {
      return rows(i0, i1, ctx.signal);
    },

    async record({i}, ctx) {
      return record(i, ctx.signal);
    },

    async id({i}, ctx) {
      await ensureIds(ctx.signal);
      return S.ids.get(i);
    },

    async ids({idx}, ctx) {
      await ensureIds(ctx.signal);
      return Array.from(idx || [], (i) => S.ids.get(i));
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
  serve(self, createEngine().handlers, {getStats});
}

