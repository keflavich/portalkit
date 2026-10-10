// Data hub (ARCHITECTURE.md section 6.6): resolves dataset refs of a page, opens one DataClient
// (worker) per catalog dataset, record stores and JSON documents, and keeps `vis:<ds>` in the
// store in step with `filters:<ds>` through the worker query op.
// Failures emit `data:error {ds, url, status, message}` plus an error toast, then reject.

import {DataClient} from './client.js';
import {RecordStore} from './records.js';
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

/**
 * createHub(page, store, opts?) -> Hub.
 * opts: {base (URL the page refs resolve against), worker (false = in-thread engines), fetch}.
 */
export function createHub(page, store, opts = {}) {
  const base = opts.base || defaultBase();
  const catalogs = new Map();
  const records = new Map();
  const jsons = new Map();
  const clients = new Map(); // resolved DataClient by ds
  const unsubs = [];

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
      url = resolve(r.url);
      return RecordStore.open(url.toString(), {fetch: opts.fetch});
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
