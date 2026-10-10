// Test hooks (ARCHITECTURE.md section 13), active only when the URL has ?pktest=1.
//
// window.__pk = {ready: Promise<void>, caps(): string[], tiers(): Record<pane, tier>, store, hub,
//                select(ds, ids, op?): Promise<number>, brush(pane, shape, op?): Promise<number>,
//                focus(ds, id): Promise<void>, stats(): {fetches, bytes, renderMs, decodeMs},
//                errors: string[], app}
//
// createTestHooks() runs first thing in boot() so `__pk.ready` and `__pk.errors` exist even when
// boot fails early (ready then rejects with the boot error). attach(app) fills in the rest.

export function testMode(loc) {
  const l = loc || (typeof location !== 'undefined' ? location : null);
  if (!l) return false;
  try {
    return new URLSearchParams(l.search || '').get('pktest') === '1';
  } catch (err) {
    if (err instanceof TypeError) return false;
    throw err;
  }
}

function errText(x) {
  if (!x) return String(x);
  if (x instanceof Error) return `${x.name}: ${x.message}`;
  if (typeof x === 'object' && x.message) return String(x.message);
  return String(x);
}

/** Install the early `window.__pk` (ready + errors). Returns null when not in test mode. */
export function createTestHooks() {
  if (typeof window === 'undefined' || !testMode()) return null;
  let resolveReady;
  let rejectReady;
  const ready = new Promise((res, rej) => { resolveReady = res; rejectReady = rej; });
  ready.catch(() => {});   // awaiting callers still observe the rejection
  const errors = [];
  window.addEventListener('error', (e) => errors.push(errText(e.error || e.message)));
  window.addEventListener('unhandledrejection', (e) => errors.push(`unhandledrejection: ${errText(e.reason)}`));
  const origError = console.error.bind(console);
  console.error = (...args) => {
    errors.push(args.map(errText).join(' '));
    origError(...args);
  };
  const pk = {
    ready, errors, store: null, hub: null, app: null,
    caps: () => [], tiers: () => ({}),
    select: () => Promise.reject(new Error('not booted')),
    brush: () => Promise.reject(new Error('not booted')),
    focus: () => Promise.reject(new Error('not booted')),
    stats: () => ({fetches: 0, bytes: 0, renderMs: 0, decodeMs: 0}),
  };
  window.__pk = pk;
  return {
    pk,
    resolve: () => resolveReady(),
    reject: (err) => rejectReady(err),
    attach: (app) => attach(pk, app),
  };
}

function catalogDs(app, ds) {
  const ref = app.page.datasets && app.page.datasets[ds];
  if (ref && ref.kind !== 'catalog' && ref.catalog) return ref.catalog;
  return ds;
}

function attach(pk, app) {
  const store = app.store;
  pk.app = app;
  pk.store = store;
  pk.hub = app.hub;
  pk.caps = () => Array.from(app.caps).sort();
  pk.tiers = () => {
    const out = {};
    for (const key of store.keys ? store.keys() : Object.keys(store.snapshot())) {
      if (key.startsWith('tier:') && store.get(key)) out[key.slice(5)] = store.get(key);
    }
    return out;
  };

  pk.select = async (ds, ids, op = 'replace') => {
    const sel = await app.selectionFor(ds);
    const idx = (await app.resolveIds(ds, ids)).filter((i) => i >= 0);
    sel.setIndices(idx, op, {op, kind: 'ids', view: {ds}, ids: ids.map(String)});
    return sel.count;
  };

  pk.brush = async (pane, shape, op = 'replace') => {
    const info = app.layout.info(pane);
    if (!info) throw new Error(`unknown pane: ${pane}`);
    const handle = await app.layout.whenMounted(pane);
    if (handle && typeof handle.brush === 'function') return handle.brush(shape, op);
    const ds = catalogDs(app, info.dataset);
    const axes = store.get(`axes:${pane}`) || info.options || {};
    if (!axes.x || !axes.y) throw new Error(`pane ${pane} has no x/y axes and no brush() hook`);
    const client = await app.hub.catalog(ds);
    const view = {pane, kind: 'xy', x: axes.x, y: axes.y, xlog: !!axes.xlog, ylog: !!axes.ylog};
    const res = await client.brush(view, shape);
    const sel = await app.selectionFor(ds);
    const {kind, ...geom} = shape;
    sel.apply(res.hit, op, res.drawable, {op, kind, view: {ds, x: axes.x, y: axes.y}, shape: geom});
    store.emit('brush:commit', {pane, ds, shape, op, count: sel.count});
    return sel.count;
  };

  pk.focus = async (ds, id) => {
    const [i] = await app.resolveIds(ds, [id]);
    store.set({focus: {ds, i: i >= 0 ? i : null, id: String(id)}}, {source: 'test'});
  };

  pk.stats = () => {
    const hs = app.hub && typeof app.hub.stats === 'function' ? app.hub.stats() : {};
    let renderMs = 0;
    for (const p of app.layout.panes()) {
      const h = p.handle;
      if (h && typeof h.stats === 'function') {
        const s = h.stats() || {};
        if (Number.isFinite(s.renderMs)) renderMs += s.renderMs;
      }
    }
    return {fetches: hs.fetches || 0, bytes: hs.bytes || 0, renderMs, decodeMs: hs.decodeMs || 0};
  };
}
