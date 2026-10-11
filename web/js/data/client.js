// DataClient (ARCHITECTURE.md section 6.6): main-thread facade over workers/data.worker.js, one
// module worker per catalog dataset. Falls back to running the same engine in-thread when module
// workers are unavailable (or opts.worker === false, as in the node tests).

import {RECORD_LRU} from '../core/constants.js';
import {Selection} from '../core/selection.js';
import {baseKeys as exprBaseKeys, isValid, isKey} from './expr.js';
import {createRpcClient, createLocalRpc} from '../workers/rpc.js';
import {LRU} from './records.js';
import {normDir} from '../workers/ops/order.js';
import {abortError} from './fetch.js';

const POS_META = {
  ra: {key: 'ra', label: 'RA', unit: 'deg', role: 'ra', enc: 'pos', group: 'position'},
  dec: {key: 'dec', label: 'Dec', unit: 'deg', role: 'dec', enc: 'pos', group: 'position'},
};

function baseHref() {
  if (typeof document !== 'undefined' && document.baseURI) return document.baseURI;
  if (typeof location !== 'undefined' && location.href) return location.href;
  return undefined;
}

function isWorkerLoadError(err) {
  return !!err && typeof err.message === 'string' && err.message.startsWith('worker error');
}

async function makeLocal(opts) {
  const {createEngine} = await import('../workers/data.worker.js');
  const {getStats} = await import('./fetch.js');
  const eng = createEngine({fetch: opts.fetch});
  return {rpc: createLocalRpc(eng.handlers, {getStats, onProgress: opts._onProgress}), local: true};
}

function makeWorker(opts) {
  if (opts.worker === false || typeof Worker !== 'function') return null;
  let w;
  try {
    w = new Worker(new URL('../workers/data.worker.js', import.meta.url), {type: 'module'});
  } catch (err) {
    // SecurityError (file://, CSP) or TypeError (no module workers): run in-thread instead.
    if (err instanceof TypeError || (typeof DOMException === 'function' && err instanceof DOMException)) return null;
    throw err;
  }
  const holder = {stats: null};
  const rpc = createRpcClient(w, {
    onStats: (s) => {
      holder.stats = s;
    },
    onProgress: opts._onProgress,
  });
  return {rpc, local: false, holder};
}

export class DataClient {
  /**
   * Open a catalog manifest. opts: {signal, worker (false = in-thread), fetch (in-thread only),
   * onProgress(p)}.
   */
  static async open(url, opts = {}) {
    const abs = new URL(url, baseHref()).toString();
    let progressFn = opts.onProgress || null;
    const relay = (p) => {
      if (progressFn) progressFn(p);
    };
    const o = {...opts, _onProgress: relay};
    let h = makeWorker(o);
    let info;
    if (h) {
      try {
        info = await h.rpc.call('open', {url: abs}, {signal: opts.signal});
      } catch (err) {
        if (!isWorkerLoadError(err)) {
          h.rpc.terminate();
          throw err;
        }
        h.rpc.terminate();
        h = null;
      }
    }
    if (!h) {
      h = await makeLocal(o);
      info = await h.rpc.call('open', {url: abs}, {signal: opts.signal});
    }
    const c = new DataClient(h, info);
    c._setProgress = (fn) => {
      progressFn = fn;
    };
    return c;
  }

  constructor(h, info) {
    this._rpc = h.rpc;
    this._h = h;
    this.local = !!h.local;
    this.url = info.url;
    this.manifest = info.manifest;
    this.n = info.n;
    this._metas = new Map((info.manifest.cols || []).map((c) => [c.key, c]));
    this._virtual = new Map((info.manifest.virtual || []).map((v) => [v.key, v]));
    this._sel = null;
    this._selPushed = -1;
    this._recLRU = new LRU(RECORD_LRU);
    this._recToken = 0;
    this._recChannels = new Map(); // channel -> {ac: AbortController, superseded}
    this._applySeq = 0;
    this._idCache = new LRU(4096);
    this._setProgress = () => {};
  }

  /** Progress callback for data:progress (set by the hub). */
  set onProgress(fn) {
    this._setProgress(fn);
  }

  /** Latest worker counters ({fetches, bytes, decodeMs}); null in-thread. */
  workerStats() {
    return this._h.holder ? this._h.holder.stats : null;
  }

  _call(op, args, opts) {
    return this._rpc.call(op, args, opts);
  }

  // ---------------------------------------------------------------- metadata

  /** Stored column meta, virtual pseudo-meta ({virtual: true}) or ra/dec pseudo-meta; else null. */
  col(key) {
    const m = this._metas.get(key);
    if (m) return m;
    if ((key === 'ra' || key === 'dec') && this.manifest.pos) return POS_META[key];
    const v = this._virtual.get(key);
    if (v) return {label: key, ...v, virtual: true, expr: key};
    return null;
  }

  /** True for manifest virtual keys and for valid expressions that are not stored columns. */
  isVirtual(key) {
    if (this._metas.has(key)) return false;
    if (key === 'ra' || key === 'dec') return false;
    if (this._virtual.has(key)) return true;
    return isValid(key) && !isKey(key);
  }

  /** Stored keys an expression depends on. */
  baseKeys(expr) {
    if (this._metas.has(expr)) return [expr];
    return exprBaseKeys(expr);
  }

  // ---------------------------------------------------------------- selection

  /** The page Selection for this dataset (created lazily). */
  selection() {
    if (!this._sel) this._sel = new Selection(this.n);
    return this._sel;
  }

  async _syncSel(mask) {
    if (mask !== 'sel') return;
    const s = this._sel;
    if (!s) {
      if (this._selPushed !== -2) {
        await this._call('setSel', {mask: null, version: -2});
        this._selPushed = -2;
      }
      return;
    }
    if (s.version === this._selPushed) return;
    const m = s.mask.slice();
    const version = s.version;
    await this._call('setSel', {mask: m, version}, {transfer: [m.buffer]});
    this._selPushed = version;
  }

  // ---------------------------------------------------------------- data ops

  ensure(keys, opts = {}) {
    return this._call('ensure', {keys: Array.from(keys), prefix: opts.prefix}, {signal: opts.signal});
  }

  gpuColumns(keys, opts = {}) {
    return this._call('gpuColumns', {keys: Array.from(keys), prefix: opts.prefix}, {signal: opts.signal});
  }

  values(expr, opts = {}) {
    return this._call('values', {expr, prefix: opts.prefix}, {signal: opts.signal});
  }

  /**
   * Project a frame. With opts.token the settled projection is kept for {kind:'proj'} ViewRefs;
   * pass opts.pane as well (the cache key is pane + token, since panes number tokens on their
   * own). A projection stored without a pane answers any pane's ViewRef with that token.
   */
  project(spec, opts = {}) {
    return this._call('project', {spec, prefix: opts.prefix, token: opts.token, pane: opts.pane ?? null}, {signal: opts.signal});
  }

  /**
   * Nearest drawable row within rPx pixels of (px, py) (data coordinates), or -1.
   * The ViewRef must carry `kx`, `ky`: the pane's pixels per data unit (per decade on log axes),
   * which convert the pixel radius to data units. Without them the call rejects with TypeError.
   */
  pick(view, px, py, rPx = 6) {
    const kx = +(view && view.kx), ky = +(view && view.ky);
    if (!(Number.isFinite(kx) && kx !== 0 && Number.isFinite(ky) && ky !== 0)) {
      return Promise.reject(new TypeError('pick: ViewRef needs kx, ky (pixels per data unit, per decade on log axes) to convert rPx to data units'));
    }
    return this._call('pick', {view, px, py, rPx});
  }

  brush(view, shape) {
    return this._call('brush', {view, shape});
  }

  /**
   * Evaluate filters. opts.apply = true makes the result the worker's page vis mask. Applying
   * queries are numbered in call order; the worker ignores the mask of one that finishes after a
   * newer applying query (`applied: false` in its result).
   */
  query(filters, opts = {}) {
    const apply = !!opts.apply;
    const seq = apply ? ++this._applySeq : null;
    return this._call('query', {filters, apply, seq}, {signal: opts.signal});
  }

  async hist(expr, bins, opts = {}) {
    await this._syncSel(opts.mask);
    return this._call('hist', {expr, bins, range: opts.range ?? null, mask: opts.mask ?? null, log: !!opts.log}, {signal: opts.signal});
  }

  async bin2d(view, nx, ny, opts = {}) {
    const mask = opts.mask === undefined ? 'vis' : opts.mask;
    await this._syncSel(mask);
    return this._call('bin2d', {view, nx, ny, mask, xr: opts.xr ?? null, yr: opts.yr ?? null}, {signal: opts.signal});
  }

  rows(i0, i1, opts = {}) {
    return this._call('rows', {i0, i1}, {signal: opts.signal});
  }

  /**
   * Argsort of every row (IMPLEMENTATION_NOTES 74), computed in the worker: an Int32Array of the
   * n stored row indices ordered by `key` (stored column, 'ra'/'dec', expression, or 'id' for the
   * catalog ids in natural order). `dir` is 1 / 'asc' (default) or -1 / 'desc'. Missing values
   * (NaN, missing codes) come last in both directions; ties keep ascending row order. Category
   * columns sort by level label. opts: {signal}.
   */
  order(key, dir = 1, opts = {}) {
    return this._call('order', {key: String(key), dir: normDir(dir)}, {signal: opts.signal});
  }

  /**
   * One row (exact chunk or quantized fallback) with `_limits`. LRU of RECORD_LRU rows.
   * Accepts record(i, opts) and record(ds, i, opts) (the ds is this client's dataset).
   * opts: {signal, token, channel}.
   * - With `channel` (a non-empty string), a new call aborts the previous unfinished call on the
   *   same channel, which then resolves to null; calls on other channels are unaffected
   *   (IMPLEMENTATION_NOTES 73).
   * - Without a channel (the default channel), a tokened call (opts.token) whose response is
   *   superseded by a later tokened default-channel call resolves to null; untokened calls always
   *   resolve.
   * An abort through opts.signal rejects with AbortError.
   */
  async record(a, b, c) {
    let i, opts;
    if (typeof a === 'string' && typeof b === 'number') {
      i = b;
      opts = c || {};
    } else {
      i = a;
      opts = b || {};
    }
    const ch = opts.channel;
    if (ch === undefined || ch === null || ch === '') return this._recordDefault(i, opts);
    return this._recordChannel(i, String(ch), opts);
  }

  async _recordDefault(i, opts) {
    const tok = opts.token !== undefined ? ++this._recToken : null;
    let r = this._recLRU.get(i);
    if (r === undefined) {
      r = await this._call('record', {i}, {signal: opts.signal});
      this._recLRU.set(i, r);
    }
    if (tok !== null && tok !== this._recToken) return null;
    return r;
  }

  async _recordChannel(i, ch, opts) {
    const prev = this._recChannels.get(ch);
    if (prev) {
      prev.superseded = true;
      prev.ac.abort();
    }
    const own = opts.signal || null;
    if (own && own.aborted) {
      if (this._recChannels.get(ch) === prev) this._recChannels.delete(ch);
      throw abortError();
    }
    const entry = {ac: new AbortController(), superseded: false};
    this._recChannels.set(ch, entry);
    const relay = () => entry.ac.abort();
    if (own) own.addEventListener('abort', relay, {once: true});
    try {
      let r = this._recLRU.get(i);
      if (r === undefined) {
        r = await this._call('record', {i}, {signal: entry.ac.signal});
        this._recLRU.set(i, r);
      }
      if (entry.superseded) return null;
      if (own && own.aborted) throw abortError();
      return r;
    } catch (err) {
      if (err && err.name === 'AbortError' && entry.superseded && !(own && own.aborted)) return null;
      throw err;
    } finally {
      if (own) own.removeEventListener('abort', relay);
      if (this._recChannels.get(ch) === entry) this._recChannels.delete(ch);
    }
  }

  async id(i) {
    const hit = this._idCache.get(i);
    if (hit !== undefined) return hit;
    const s = await this._call('id', {i});
    this._idCache.set(i, s);
    return s;
  }

  find(text, opts = {}) {
    return this._call('find', {text, limit: opts.limit ?? 50}, {signal: opts.signal});
  }

  crossmatch(ra, dec, rArcsec) {
    return this._call('crossmatch', {ra, dec, rArcsec});
  }

  async skyWindow(win) {
    await this._syncSel(win.mask);
    return this._call('skyWindow', {win});
  }

  async exportCSV(opts = {}) {
    await this._syncSel(opts.mask);
    const r = await this._call('exportCSV', {mask: opts.mask ?? 'all', cols: opts.cols ?? 'all', exact: !!opts.exact});
    return new Blob([r.text], {type: `${r.mime};charset=utf-8`});
  }

  /** Export as csv | json | ids | ds9 -> Blob. */
  async export(opts = {}) {
    await this._syncSel(opts.mask);
    const r = await this._call('export', {mask: opts.mask ?? 'all', cols: opts.cols ?? 'all', exact: !!opts.exact, format: opts.format || 'csv', r: opts.r ?? 1});
    return new Blob([r.text], {type: `${r.mime};charset=utf-8`});
  }

  /** Decoded pre-binned density grid by key (manifest `density`). */
  density(key, opts = {}) {
    return this._call('density', {key}, {signal: opts.signal});
  }

  /** Current page vis mask ({vis: Uint8Array | null, version}). */
  vis() {
    return this._call('vis', {});
  }

  terminate() {
    this._rpc.terminate();
  }
}
