// Data hub (ARCHITECTURE.md section 6.6): resolves dataset refs of a page, opens one DataClient
// (worker) per catalog dataset, record stores and JSON documents, and keeps `vis:<ds>` in the
// store in step with `filters:<ds>` through the worker query op.
// Failures emit `data:error {ds, url, status, message}` plus an error toast, then reject.

import {DataClient} from './client.js';
import {RecordStore, isForeignRef, foreignPageBase, recordIndexUrl} from './records.js';
import {fetchJSON, getStats} from './fetch.js';
import {isActive} from '../workers/ops/query.js';
import {baseKeys} from './expr.js';

function defaultBase() {
  if (typeof document !== 'undefined' && document.baseURI) return document.baseURI;
  if (typeof location !== 'undefined' && location.href) return location.href;
  return undefined;
}

/** Pane leaves of a layout tree: [{id, pane, dataset, options}]. */
export function paneLeaves(node, out = []) {
  if (!node || typeof node !== 'object') return out;
  if (Array.isArray(node)) {
    for (const c of node) paneLeaves(c, out);
    return out;
  }
  if (node.pane) out.push(node);
  if (Array.isArray(node.row)) paneLeaves(node.row, out);
  if (Array.isArray(node.col)) paneLeaves(node.col, out);
  if (Array.isArray(node.children)) paneLeaves(node.children, out);
  return out;
}

/** Substitute {assets} / {res} in a dataset URL (no resolution). */
export function substitute(url, page) {
  let s = String(url);
  if (page && page.assets) s = s.split('{assets}').join(page.assets);
  if (page && page.res) s = s.split('{res}').join(page.res);
  return s;
}

/** Stored keys the active projections of `ds` panes use (snr scope "projection"). */
export function projectionKeys(page, store, ds) {
  const keys = new Set();
  const add = (expr) => {
    if (typeof expr !== 'string' || !expr) return;
    let ks;
    try {
      ks = baseKeys(expr);
    } catch (err) {
      if (err instanceof SyntaxError) return;
      throw err;
    }
    for (const k of ks) keys.add(k);
  };
  const panes = paneLeaves(page && page.layout).filter((p) => !p.dataset || p.dataset === ds);
  for (const p of panes) {
    const id = p.id || p.pane;
    const ax = store.get(`axes:${id}`);
    if (ax) {
      add(ax.x);
      add(ax.y);
    }
    const fr = store.get(`frame:${id}`);
    if (fr && Array.isArray(fr.dims)) {
      const D = fr.dims.length;
      const F = Array.isArray(fr.F) || ArrayBuffer.isView(fr.F) ? fr.F : null;
      fr.dims.forEach((d, j) => {
        if (F && F.length === 2 * D && !(Math.abs(F[j]) > 1e-9 || Math.abs(F[D + j]) > 1e-9)) return;
        add(d);
      });
    }
  }
  return Array.from(keys);
}

/** True when a `filters:<ds>` value carries nothing (absent, null or an empty object). */
export function isEmptyFilters(f) {
  if (f === null || f === undefined) return true;
  return typeof f === 'object' && !Array.isArray(f) && Object.keys(f).length === 0;
}

/**
 * `cats` filter value for a manifest's category `default_off` levels ({key: [level...]}), or
 * null when no column has any. Only levels present in the column's `levels` are kept.
 */
export function defaultOffCats(manifest) {
  const cats = {};
  for (const c of (manifest && manifest.cols) || []) {
    if (!c || !Array.isArray(c.levels) || !Array.isArray(c.default_off) || !c.default_off.length) continue;
    const lv = new Set(c.levels.map(String));
    const off = [];
    for (const v of c.default_off) {
      const s = String(v);
      if (lv.has(s) && !off.includes(s)) off.push(s);
    }
    if (off.length) cats[c.key] = off;
  }
  return Object.keys(cats).length ? cats : null;
}

/**
 * `filters` with the manifest's category default_off levels filled in for every column whose
 * `cats[key]` is undefined (an explicit array, including [], is kept). Returns `filters` itself
 * when nothing changes.
 */
export function withDefaultOff(filters, manifest) {
  const off = defaultOffCats(manifest);
  if (!off) return filters;
  const have = (filters && filters.cats) || {};
  const add = {};
  for (const [key, lv] of Object.entries(off)) if (have[key] === undefined) add[key] = lv;
  if (!Object.keys(add).length) return filters;
  return {...(filters || {}), cats: {...have, ...add}};
}

/**
 * createHub(page, store, opts?) -> Hub.
 * opts: {base (URL the page refs resolve against), worker (false = in-thread engines), fetch,
 * defaultOff (false skips applying manifest category default_off at catalog open)}.
 */
export function createHub(page, store, opts = {}) {
  const base = opts.base || defaultBase();
  const catalogs = new Map();
  const records = new Map();
  const jsons = new Map();
  const clients = new Map(); // resolved DataClient by ds
  const unsubs = [];
  const defaultOffDone = new Set();

  function ref(ds) {
    const r = page && page.datasets ? page.datasets[ds] : null;
    if (!r) throw new Error(`unknown dataset ${ds}`);
    return r;
  }

  function resolve(url) {
    return new URL(substitute(url, page), base);
  }

  function fail(ds, url, err) {
    if (err && err.name === 'AbortError') throw err;
    const status = err && Number.isInteger(err.status) ? err.status : 0;
    const message = (err && err.message) || String(err);
    if (store) {
      store.emit('data:error', {ds, url: String(url), status, message});
      store.emit('toast', {level: 'error', text: `Could not load ${ds}: ${message}`});
    }
    throw err;
  }

  // ------------------------------------------------------------ filters -> vis

  function installFilters(ds, client) {
    if (!store) return;
    // Category default_off (note 70): the filter owner applies it once, when the catalog opens, to
    // every default_off column whose `cats[key]` the page state (hash, carry, page) leaves
    // undefined. An explicit array wins, so `cats: {key: []}` records "all levels on".
    if (opts.defaultOff !== false && !defaultOffDone.has(ds)) {
      defaultOffDone.add(ds);
      const cur = store.get(`filters:${ds}`);
      const next = withDefaultOff(cur, client.manifest);
      if (next !== cur) store.set({[`filters:${ds}`]: next}, {source: 'hub'});
    }
    let seq = 0;
    let version = (store.get(`vis:${ds}`) || {}).version || 0;
    const run = () => {
      let f = store.get(`filters:${ds}`) || null;
      if (f && f.snr && f.snr.scope === 'projection' && !(Array.isArray(f.snr.keys) && f.snr.keys.length)) {
        f = {...f, snr: {...f.snr, keys: projectionKeys(page, store, ds)}};
      }
      const my = ++seq;
      return client.query(f || {}, {apply: true}).then(
        (res) => {
          if (my !== seq) return;
          version += 1;
          store.set({[`vis:${ds}`]: {version, count: res.active ? res.count : null}}, {source: 'hub'});
        },
        (err) => {
          if (my !== seq) return;
          fail(ds, client.url, err);
        },
      );
    };
    const safeRun = () => {
      run().catch(() => {});
    };
    unsubs.push(store.on([`filters:${ds}`], safeRun));
    unsubs.push(store.on(['axes:*', 'frame:*'], () => {
      const f = store.get(`filters:${ds}`);
      if (f && f.snr && f.snr.scope === 'projection' && +f.snr.min > 0) safeRun();
    }));
    if (isActive(store.get(`filters:${ds}`))) safeRun();
  }

  // ------------------------------------------------------------ hub API

  function catalog(ds) {
    if (catalogs.has(ds)) return catalogs.get(ds);
    let url;
    const p = (async () => {
      const r = ref(ds);
      url = resolve(r.url);
      const client = await DataClient.open(url.toString(), {worker: opts.worker, fetch: opts.fetch});
      client.onProgress = (pr) => {
        if (store) store.emit('data:progress', {...pr, ds});
      };
      clients.set(ds, client);
      installFilters(ds, client);
      return client;
    })().catch((err) => {
      catalogs.delete(ds);
      return fail(ds, url || ds, err);
    });
    catalogs.set(ds, p);
    return p;
  }

  function recordsFor(ds) {
    if (records.has(ds)) return records.get(ds);
    let url;
    const p = (async () => {
      const r = ref(ds);
      // recordIndexUrl falls back to a foreign ref's `<base><key>/index.json` when it carries no
      // `url` of its own (contract (A), base+key form) — `r.url` alone is undefined there.
      url = resolve(recordIndexUrl(r));
      // PNG-mode sidecars resolve against the page URL with {assets}/{res} substituted (note 81).
      // A foreign record set (contract (A)) is a different portal: its PNG-mode sidecars resolve
      // against ITS page directory (foreignPageBase of the foreign index URL), not this page's,
      // matching boot.js's hub.records override (core/boot.js ~709) so a non-boot caller of
      // createHub (e.g. a direct test) sees the same sidecar URLs as a booted page. id_column
      // lookups (idOf) stay boot.js's job: they need catalog row access this hub does not have.
      const pngBase = isForeignRef(r) ? foreignPageBase(url.toString()) : base;
      return RecordStore.open(url.toString(), {fetch: opts.fetch, base: pngBase, page: {assets: page && page.assets, res: page && page.res}});
    })().catch((err) => {
      records.delete(ds);
      return fail(ds, url || ds, err);
    });
    records.set(ds, p);
    return p;
  }

  async function loadJSON(url, kind) {
    const doc = await fetchJSON(url.toString(), {fetch: opts.fetch});
    if (kind === 'images' && doc && Array.isArray(doc.pages) && doc.pages.length) {
      const items = Array.isArray(doc.items) ? doc.items.slice() : [];
      for (const pg of doc.pages) {
        const rel = typeof pg === 'string' ? pg : pg && (pg.url || pg.file);
        if (!rel) continue;
        const part = await fetchJSON(new URL(rel, url).toString(), {fetch: opts.fetch});
        const its = Array.isArray(part) ? part : part && Array.isArray(part.items) ? part.items : [];
        for (const it of its) items.push(it);
      }
      return {...doc, items, pages: undefined};
    }
    return doc;
  }

  function json(ds) {
    if (jsons.has(ds)) return jsons.get(ds);
    let url;
    const p = (async () => {
      const r = ref(ds);
      url = resolve(r.url);
      return loadJSON(url, r.kind);
    })().catch((err) => {
      jsons.delete(ds);
      return fail(ds, url || ds, err);
    });
    jsons.set(ds, p);
    return p;
  }

  function stats() {
    const s = getStats();
    const out = {fetches: s.fetches, bytes: s.bytes, decodeMs: s.decodeMs};
    for (const c of clients.values()) {
      const w = c.workerStats();
      if (!w) continue;
      out.fetches += w.fetches || 0;
      out.bytes += w.bytes || 0;
      out.decodeMs += w.decodeMs || 0;
    }
    return out;
  }

  function dispose() {
    for (const u of unsubs.splice(0)) u();
    for (const c of clients.values()) c.terminate();
    clients.clear();
    catalogs.clear();
  }

  return {catalog, records: recordsFor, json, resolve, stats, dispose};
}
