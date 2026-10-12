// Page boot (ARCHITECTURE.md section 6.1). Invoked by shell.html: `boot()`.
//
// Order:
//  1. parse #pk-page (theme already set by the shell's inline pre-script); start importing the
//     module graphs of the panes shown at load (warmPaneModules; WPD3) so their fetch chains
//     overlap the CSS and manifest waits. registry.load() still owns pane CSS and errors (step 5)
//  2. store: defaults <- page.state <- carry <- legacy hash mapping (legacyhash.js, only when
//     page.page.legacy.grammar is set; imported dynamically) <- canonical hash
//  3. hub; start fetching manifests of every catalog a non-collapsed pane uses
//  4. runtime capabilities (WebGL2, EXT_color_buffer_float, DecompressionStream, deviceMemory);
//     per-pane tiers once N is known
//  5. mount the layout once the base sheets and css/panes/ui.css have loaded; each shown pane
//     loads through core/registry.js (pane CSS first, then the module warmed at step 1)
//  6. <body data-caps> (enabled caps, space separated) and data-tier ("pane:tier ...")
//  7. bind hash, keys and theme; install window.__pk when ?pktest=1
//  8. emit 'ready' {ms}; app.afterFirstPaint() resolves once no visible pane is loading, at the
//     next idle callback (IMPLEMENTATION_NOTES 100)
//
// App = {page, store, hub, layout, selections: Map<ds, Selection>, caps: Set<string>, emit, on,
//        gpuBudget (one gl/budget.js Budget per page; panes never create one),
//        ...helpers documented in docs/js.md (selectionFor, resolveIds, applySelection,
//        importCsv, ...)}

import {T0_MAX, T1_MAX, BITS_IN_HASH_MAX_N} from './constants.js';
import {createStore} from './store.js';
import {Selection, unpackBits, OPS} from './selection.js';
import {parseHash, hashToPatch, bindHash, formatHash, stateToHash} from './hash.js';
import {takeCarry, broadcastFocus, onBroadcastFocus} from './carry.js';
import {bindKeys, KEYMAP} from './keys.js';
import * as theme from './theme.js';
import {registry} from './registry.js';
import {createTestHooks} from './testhooks.js';
import {loadCSS} from './loadscript.js';
import {fmtCount, escapeHtml} from './fmt.js';
import {mountLayout, normalizeTree, shownAtLoad} from '../layout/layout.js';
import {createHub} from '../data/hub.js';
import {RecordStore, recordIndexUrl, foreignKeyOf, isForeignRef, foreignPageBase} from '../data/records.js';
import {Budget} from '../gl/budget.js';

/** Pane types that get a rendering tier from their catalog's N (section 7.1). */
export const TIERED_PANES = Object.freeze(['scatter', 'manifold', 'hist', 'sky', 'list', 'table']);

/** UI widgets mounted by boot: each may export install(app) -> (destroy fn | void). */
export const UI_MODULES = Object.freeze(['toast', 'nav', 'help', 'hover', 'lightbox']);

/**
 * Most ids boot prefetches so that index-only selection ops (Selection.recipeIndices) can be
 * written to the hash as ids; larger index sets fall back to the `bits:` form or are left out.
 */
export const PREFETCH_IDS_MAX = 1000;

/**
 * app.resolveIds looks up at least this many unknown ids in one pass over the catalog's id list
 * (DataClient.export format 'ids') instead of one DataClient.find call per id.
 */
export const RESOLVE_BATCH_MIN = 16;

/** requestIdleCallback timeout of app.afterFirstPaint (IMPLEMENTATION_NOTES 100). */
export const FIRST_PAINT_IDLE_TIMEOUT_MS = 1500;

/**
 * Longest wait after `ready` for the visible panes to leave the loading state before
 * app.afterFirstPaint resolves anyway (with a console warning), so a pane whose mount waits on
 * deferred data cannot hold every deferred load back for good.
 */
export const FIRST_PAINT_MAX_WAIT_MS = 30000;

/** True while a visible `section.pk-pane` under `root` has data-state="loading". */
export function visiblePanesLoading(root) {
  if (!root || typeof root.querySelectorAll !== 'function') return false;
  for (const s of root.querySelectorAll('section.pk-pane[data-state="loading"]')) {
    const shown = s.offsetParent !== null || (typeof s.getClientRects === 'function' && s.getClientRects().length > 0);
    if (shown) return true;
  }
  return false;
}

/**
 * Promise that resolves once (to undefined) when no visible pane under `root` is loading, at the
 * next idle callback (`requestIdleCallback` with a FIRST_PAINT_IDLE_TIMEOUT_MS timeout; a short
 * timer where it is missing). The pane states are re-checked on DOM attribute changes and every
 * `poll` ms. opts: {loading(root) -> bool, idle(cb, {timeout}), poll (ms, default 100),
 * maxWaitMs (default FIRST_PAINT_MAX_WAIT_MS), MutationObserver (null disables observing)}.
 */
export function whenFirstPaint(root, opts = {}) {
  const loading = opts.loading || visiblePanesLoading;
  const idle = opts.idle || ((cb, o) => {
    if (typeof requestIdleCallback === 'function') requestIdleCallback(cb, o);
    else setTimeout(cb, 16);
  });
  const poll = opts.poll ?? 100;
  const maxWaitMs = opts.maxWaitMs ?? FIRST_PAINT_MAX_WAIT_MS;
  const MO = opts.MutationObserver !== undefined ? opts.MutationObserver
    : (typeof MutationObserver === 'function' ? MutationObserver : null);
  return new Promise((resolve) => {
    let done = false;
    let timer = null;
    let guard = null;
    let mo = null;
    const finish = () => {
      if (done) return;
      done = true;
      if (timer !== null) clearInterval(timer);
      if (guard !== null) clearTimeout(guard);
      if (mo) mo.disconnect();
      idle(() => resolve(), {timeout: FIRST_PAINT_IDLE_TIMEOUT_MS});
    };
    const check = () => {
      if (!done && !loading(root)) finish();
    };
    check();
    if (done) return;
    if (MO && root) {
      mo = new MO(check);
      mo.observe(root, {subtree: true, attributes: true, attributeFilter: ['data-state', 'class', 'hidden', 'style']});
    }
    timer = setInterval(check, poll);
    if (Number.isFinite(maxWaitMs) && maxWaitMs >= 0) {
      guard = setTimeout(() => {
        if (done) return;
        console.warn(`afterFirstPaint: panes still loading ${maxWaitMs} ms after ready; releasing deferred loads`);
        finish();
      }, maxWaitMs);
    }
  });
}

/**
 * First stored row of each wanted id in `text`, the newline-separated id list in stored order
 * (DataClient.export({mask: 'all', format: 'ids'})). Exact string match. Returns Map(id -> row).
 */
export function matchIdList(text, wanted) {
  const found = new Map();
  if (!wanted || !wanted.size) return found;
  const s = String(text ?? '');
  let row = 0;
  let a = 0;
  while (a < s.length && found.size < wanted.size) {
    let b = s.indexOf('\n', a);
    if (b < 0) b = s.length;
    const id = s.slice(a, b);
    if (wanted.has(id) && !found.has(id)) found.set(id, row);
    row += 1;
    a = b + 1;
  }
  return found;
}

/**
 * Core of app.importCsv (IMPLEMENTATION_NOTES 93), exported for tests. Parses `text` with
 * io.parseImportText, matches entries with io.resolveImport (ids and aliases through
 * deps.resolveIds, then positions through client.crossmatch when the manifest has `pos`), and
 * applies the matched rows to `sel` with `mode` in one setIndices call.
 * deps = {mode, sel, client: {n, manifest, crossmatch?}, io: ui/export.js, resolveIds, radiusArcsec?}
 * Returns {mode, format, entries, skipped, matched, byId, byPosition, missing, missingLabels,
 *          rows, count, summary}; the selection is left untouched when no entry was recognised.
 */
export async function importSelection(text, deps) {
  const {sel, client, io} = deps;
  const mode = deps.mode ?? 'replace';
  if (!OPS.includes(mode)) throw new TypeError(`unknown import mode: ${mode}`);
  const parsed = io.parseImportText(String(text ?? ''));
  const out = {
    mode, format: parsed.format, entries: parsed.entries.length, skipped: parsed.skipped || 0,
    matched: 0, byId: 0, byPosition: 0, missing: 0, missingLabels: [], rows: 0, count: sel.count, summary: '',
  };
  if (!parsed.entries.length) {
    out.summary = parsed.format === 'empty' ? 'Nothing to import.' : 'No ids or positions were recognised.';
    return out;
  }
  const pos = !!(client.manifest && client.manifest.pos) && typeof client.crossmatch === 'function';
  const r = Number(deps.radiusArcsec);
  const res = await io.resolveImport(parsed.entries, {
    n: client.n,
    radiusArcsec: Number.isFinite(r) && r > 0 ? r : undefined,
    resolveIds: deps.resolveIds,
    crossmatch: pos ? (ra, dec, rad) => client.crossmatch(ra, dec, rad) : null,
  });
  sel.setIndices(Array.from(res.indices), mode);
  const label = typeof io.entryLabel === 'function' ? io.entryLabel : (e) => String(e && (e.id ?? `line ${e.line}`));
  Object.assign(out, {
    matched: res.matched.length, byId: res.byId, byPosition: res.byPosition, missing: res.missing.length,
    missingLabels: res.missing.slice(0, 50).map(label), rows: res.indices.length, count: sel.count,
  });
  out.summary = typeof io.importSummary === 'function' ? io.importSummary(res, parsed.entries.length)
    : `Matched ${fmtCount(out.matched)} of ${fmtCount(out.entries)}`;
  return out;
}

/**
 * Tier for a pane from N and runtime support, with +-10 % hysteresis around the boundaries
 * when `prev` is given (section 7.1). T0 never needs WebGL.
 */
export function tierFor(n, rt = {}, prev = null) {
  const webgl = rt.webgl2 !== false;
  const floatRT = rt.floatRT !== false;
  const b0 = prev === 'T0' ? T0_MAX * 1.1 : (prev && prev !== 'T0' ? T0_MAX * 0.9 : T0_MAX);
  const b1 = prev === 'T2' ? T1_MAX * 0.9 : (prev === 'T0' || prev === 'T1' || prev === 'F' ? T1_MAX * 1.1 : T1_MAX);
  if (n <= b0) return 'T0';
  if (!webgl) return 'F';
  if (n <= b1) return 'T1';
  return floatRT ? 'T2' : 'F';
}

/** Runtime capability probe (step 4). */
export function detectRuntime() {
  const rt = {webgl2: false, floatRT: false, decompressionStream: typeof DecompressionStream === 'function',
    deviceMemory: (typeof navigator !== 'undefined' && navigator.deviceMemory) || null, maxTexture: 0};
  if (typeof document === 'undefined') return rt;
  const c = document.createElement('canvas');
  let gl = null;
  try {
    gl = c.getContext('webgl2', {antialias: false, preserveDrawingBuffer: false});
  } catch (err) {
    // Some drivers throw instead of returning null when WebGL is blocked.
    if (!(err instanceof Error)) throw err;
    gl = null;
  }
  if (gl) {
    rt.webgl2 = true;
    rt.floatRT = !!gl.getExtension('EXT_color_buffer_float');
    rt.maxTexture = gl.getParameter(gl.MAX_TEXTURE_SIZE) || 0;
    const lose = gl.getExtension('WEBGL_lose_context');
    if (lose) lose.loseContext();
  }
  return rt;
}

function isPlainObject(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v) && Object.getPrototypeOf(v) === Object.prototype;
}

/**
 * Frame preset a manifold pane opens on: the pane option `frame`, else the catalog manifest's
 * `manifold.default_frame` when it names a known preset (manifestFrameKnown), else 'pca'.
 * Mirrors panes/manifold.js initialPreset for a store without a frame.
 */
export function frameDefault(options, manifest) {
  const o = options || {};
  if (typeof o.frame === 'string' && o.frame) return o.frame;
  const df = manifest && manifest.manifold && manifest.manifold.default_frame;
  return typeof df === 'string' && df && manifestFrameKnown(manifest, df) ? df : 'pca';
}

/**
 * True when `name` is a frame preset a manifold pane on this manifest can resolve: 'pca',
 * `pair:i,j`, a manifold.frames key or a manifest preset of kind 'frame' / 'axes' (the same
 * sources as panes/manifold.js framePresets).
 */
export function manifestFrameKnown(manifest, name) {
  if (typeof name !== 'string' || !name) return false;
  if (name === 'pca' || /^pair:\d+,\d+$/.test(name)) return true;
  const m = manifest || {};
  if (m.manifold && m.manifold.frames && Object.prototype.hasOwnProperty.call(m.manifold.frames, name)) return true;
  return (m.presets || []).some((p) => p && p.key === name && (p.kind === 'frame' || p.kind === 'axes'));
}

/**
 * Store defaults for a page (before page.state). Exported for tests and docs. A manifold pane
 * without an options.frame gets preset null: boot fills it from the manifest's default_frame
 * once the catalog opens (frameDefault). `speed` is the first manifold / wavelength pane
 * options.speed, else 1.
 */
export function buildDefaults(page, norm) {
  const s = {
    focus: null, hover: null,
    color: {by: null, cmap: 'viridis'},
    av: 0, rv: 3.1, dm: 0, speed: 1,
    template: null, features: [],
    spec: {pinned: [], variant: 'flux'},
    sky: null, rung: null, beat: null, role: null, facets: {}, lightbox: null,
    theme: theme.get(),
    layout: {popout: {}},
  };
  for (const [ds, ref] of Object.entries(page.datasets || {})) {
    if (ref.kind !== 'catalog') continue;
    s[`filters:${ds}`] = {};
    s[`vis:${ds}`] = {version: 0, count: null};   // count null = no filter applied yet
    if (!ref.context) {
      s[`sel:${ds}`] = {version: 0, count: 0};
      s[`annot:${ds}`] = {version: 0};
    }
  }
  let speedSet = false;
  for (const leaf of norm.panes.values()) {
    const o = leaf.options || {};
    if (!speedSet && (leaf.type === 'manifold' || leaf.type === 'wavelength') && Number(o.speed) > 0 && Number.isFinite(Number(o.speed))) {
      s.speed = Number(o.speed);
      speedSet = true;
    }
    if (leaf.type === 'scatter') {
      s[`axes:${leaf.id}`] = {x: o.x ?? null, y: o.y ?? null, xlog: !!o.xlog, ylog: !!o.ylog, xinv: false, yinv: false};
    } else if (leaf.type === 'manifold') {
      s[`frame:${leaf.id}`] = {preset: typeof o.frame === 'string' && o.frame ? o.frame : null, dims: Array.isArray(o.dims) ? o.dims.slice() : []};
    } else if (leaf.type === 'spectrum1d' && Array.isArray(o.features) && !s.features.length) {
      s.features = o.features.slice();
    }
  }
  return s;
}

function mergeState(base, patch) {
  for (const [k, v] of Object.entries(patch || {})) {
    base[k] = isPlainObject(base[k]) && isPlainObject(v) ? Object.assign({}, base[k], v) : v;
  }
  return base;
}

/** The dataset whose selection and filters live in the hash. */
export function primaryDataset(page, norm) {
  const ds = page.datasets || {};
  const catOf = (id) => {
    const r = ds[id];
    if (!r) return null;
    if (r.kind === 'catalog' && !r.context) return id;
    if (r.catalog && ds[r.catalog] && ds[r.catalog].kind === 'catalog') return r.catalog;
    return null;
  };
  for (const id of norm.order) {
    const leaf = norm.panes.get(id);
    const c = leaf.dataset && catOf(leaf.dataset);
    if (c) return c;
  }
  for (const [id, r] of Object.entries(ds)) if (r.kind === 'catalog' && !r.context) return id;
  return null;
}

/** Dynamic-import failure because the module file does not exist (browser-specific messages). */
function isMissingModule(err) {
  const msg = String(err && err.message ? err.message : err);
  return err instanceof TypeError
    && /Failed to fetch dynamically imported module|error loading dynamically imported module|Importing a module script failed|Cannot find module/i.test(msg);
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function showFatal(err) {
  const note = el('div', 'pk-error-note pk-fatal');
  note.setAttribute('role', 'alert');
  note.innerHTML = `<strong>This page could not start.</strong> ${escapeHtml(err && err.message ? err.message : err)}`;
  (document.querySelector('.pk-main') || document.body).prepend(note);
}

/**
 * Start importing the module graphs of the panes the layout shows at load (layout.js
 * shownAtLoad: saved fold state, tab mode) while boot still waits on the base CSS and the
 * manifests. A pane module imports several levels of helpers (list -> legend -> canvas2d ->
 * overlays -> ...), and the browser discovers each level only after the previous one arrives;
 * starting early takes that chain off the first-paint path. import() fetches and evaluates the
 * modules; the pane modules' top level defines constants and functions only (no DOM work), so
 * nothing visible happens before the pane CSS loads. registry.load() still attaches the pane
 * CSS before mount() and reports any load error when the layout mounts the pane. Types
 * registered in memory (registry.register: tests, embedding) are skipped when the registry
 * exposes isOverridden(), as are types modulePath() cannot resolve. `where` = {portalId,
 * pageId, tabMode} as for shownAtLoad. Returns the types whose import started.
 */
export function warmPaneModules(norm, reg = registry, where = {}) {
  const types = new Set();
  const overridden = typeof reg.isOverridden === 'function' ? (t) => reg.isOverridden(t) : () => false;
  for (const id of shownAtLoad(norm, where)) {
    const leaf = norm.panes.get(id);
    if (!leaf || !reg.has(leaf.type) || overridden(leaf.type)) continue;
    types.add(leaf.type);
  }
  const warmed = [];
  for (const type of types) {
    let url;
    try {
      url = reg.modulePath(type);
    } catch (err) {
      if (!(err instanceof Error)) throw err;
      continue;  // registered in memory only: nothing to fetch
    }
    // registry.load() imports the same URL later and surfaces a failure there.
    import(url).catch((err) => { if (!(err instanceof Error)) throw err; });
    warmed.push(type);
  }
  return warmed;
}

function readPage() {
  const node = document.getElementById('pk-page');
  if (!node) throw new Error('missing #pk-page configuration');
  return JSON.parse(node.textContent);
}

export async function boot() {
  const t0 = typeof performance !== 'undefined' ? performance.now() : Date.now();
  const hooks = createTestHooks();
  try {
    const app = await bootInner(t0, hooks);
    if (hooks) hooks.resolve();
    return app;
  } catch (err) {
    console.error('portalkit boot failed', err);
    if (typeof document !== 'undefined') showFatal(err);
    if (hooks) hooks.reject(err);
    throw err;
  }
}

async function bootInner(t0, hooks) {
  // 1. page config -----------------------------------------------------------------------
  const page = readPage();
  const norm = normalizeTree(page.layout);
  const primaryDs = primaryDataset(page, norm);
  const portalId = (page.portal && page.portal.id) || 'portal';
  const assetBase = new URL('../../', import.meta.url).href;
  warmPaneModules(norm, registry, {portalId, pageId: (page.page && page.page.id) || 'page'});
  // ui.css (shared widget rules, IMPLEMENTATION_NOTES 108) loads with the base sheets, before
  // mountLayout: its <link> precedes every pane sheet the registry adds, and panes that mount
  // widgets (controls, chips, legends) never paint unstyled.
  const cssReady = Promise.all([
    loadCSS(`${assetBase}css/tokens.css`), loadCSS(`${assetBase}css/base.css`),
    loadCSS(`${assetBase}css/layout.css`), loadCSS(`${assetBase}css/panes/ui.css`),
    loadCSS(`${assetBase}css/print.css`, {media: 'print'}),
  ]);

  // 2. store -----------------------------------------------------------------------------
  const pageDefaults = mergeState(buildDefaults(page, norm), page.state || {});
  const initial = Object.assign({}, pageDefaults);
  const pendingSel = new Map();
  const carry = takeCarry(portalId);
  if (carry && page.datasets && page.datasets[carry.ds]) {
    if (carry.focusId) initial.focus = {ds: carry.ds, i: null, id: carry.focusId};
    if (carry.sel) pendingSel.set(carry.ds, carry.sel);
  }
  let legacyHash = {};
  let legacyUsed = false;
  let legacyFromHash = false;   // the legacy mapping consumed location.hash itself
  const grammar = page.page && page.page.legacy && page.page.legacy.grammar;
  if (Array.isArray(grammar) && grammar.length) {
    try {
      const mod = await import('./legacyhash.js');
      const res = mod.mapLegacy(grammar, {hash: location.hash, search: location.search, page});
      if (res && res.hash && Object.keys(res.hash).length) {
        legacyHash = res.hash;
        legacyUsed = true;
        legacyFromHash = typeof res.fromHash === 'boolean' ? res.fromHash
          : !(Array.isArray(res.consumedSearch) && res.consumedSearch.length);
      }
      if (res && Array.isArray(res.consumedSearch) && res.consumedSearch.length) {
        const params = new URLSearchParams(location.search);
        for (const k of res.consumedSearch) params.delete(k);
        const q = params.toString();
        history.replaceState(history.state, '', `${location.pathname}${q ? '?' + q : ''}${location.hash}`);
        legacyUsed = true;
      }
    } catch (err) {
      // A missing or broken legacyhash.js must not stop the page; canonical hashes still work.
      // It is expected whenever a grammar is configured, so the failure is reported as an error.
      console.error('legacy hash mapping unavailable', err);
    }
  }
  // A legacy mapping that consumed the hash replaces it (re-parsing the legacy text as canonical
  // would overwrite mapped keys, e.g. kv-view's raw 'view=sky'); a search-only mapping is merged
  // under the canonical hash, with the mapped keys winning.
  const canonical = legacyFromHash ? Object.assign({}, legacyHash)
    : Object.assign({}, parseHash(location.hash), legacyHash);
  const {patch: hashPatch, sel: hashSel} = hashToPatch(canonical, {primaryDs, state: initial});
  Object.assign(initial, hashPatch);
  if (hashSel !== undefined && primaryDs) pendingSel.set(primaryDs, hashSel);
  const store = createStore(initial);

  const app = {
    page, store, hub: null, layout: null, primaryDs, portalId,
    selections: new Map(),
    caps: new Set((page.caps && page.caps.enabled) || []),
    disabled: Object.assign({}, (page.caps && page.caps.disabled) || {}),
    notes: {},
    runtime: null,
    catalogs: new Map(),
    keymap: KEYMAP,
    emit: (topic, payload) => store.emit(topic, payload),
    on: (topic, fn) => store.onEvent(topic, fn),
    theme: () => theme.tokens(),
    // One GPU memory budget per page (IMPLEMENTATION_NOTES 68), shared by every plot pane
    // through ctx.app.gpuBudget (also ctx.gpuBudget).
    gpuBudget: new Budget(),
  };

  // First paint signal (IMPLEMENTATION_NOTES 100): one promise per page, resolved after the
  // 'ready' event once no visible pane is loading, at the next idle callback. Deferred loads
  // (whole columns, pos, ids) wait on it; panes may call it during mount.
  let firstPaintResolve;
  let firstPaintAtMs = null;
  const firstPaint = new Promise((r) => { firstPaintResolve = r; });
  app.afterFirstPaint = () => firstPaint;
  /** Promise of the epoch-ms time afterFirstPaint resolved (IMPLEMENTATION_NOTES 112). */
  app.firstPaintAt = () => firstPaint.then(() => firstPaintAtMs);

  // 3. hub + manifest prefetch -----------------------------------------------------------
  const hub = createHub(page, store);
  app.hub = hub;
  const idCache = new Map();      // ds -> Map(i -> id)
  const idIndex = new Map();      // ds -> Map(id -> i)
  const cacheId = (ds, i, id) => {
    if (!idCache.has(ds)) { idCache.set(ds, new Map()); idIndex.set(ds, new Map()); }
    idCache.get(ds).set(i, id);
    idIndex.get(ds).set(id, i);
  };

  app.catalog = (ds) => {
    if (!app.catalogs.has(ds)) {
      const p = Promise.resolve(hub.catalog(ds));
      app.catalogs.set(ds, p);
      p.then((client) => ensureSelection(ds, client), () => {});
      p.then((client) => applyFrameDefaults(ds, client), () => {});
      // rows()/record() read the whole pos file only after the first paint (note 99)
      Promise.all([p, firstPaint]).then(([client]) => {
        if (client && typeof client.allowPositions === 'function') client.allowPositions();
      }, () => {});
    }
    return app.catalogs.get(ds);
  };

  /**
   * Manifold panes on catalog `ds` whose page defaults leave the frame preset open (no
   * options.frame, no page.state preset) open on the manifest's default_frame (frameDefault).
   * The page defaults the hash compares against follow, so the default preset stays out of the
   * hash. The store is not touched: the pane resolves the same preset itself at mount.
   */
  function applyFrameDefaults(ds, client) {
    let changed = false;
    for (const leaf of norm.panes.values()) {
      if (leaf.type !== 'manifold' || leaf.dataset !== ds) continue;
      const k = `frame:${leaf.id}`;
      const d = pageDefaults[k];
      if (!d || d.preset || (d.F && d.F.length)) continue;
      pageDefaults[k] = Object.assign({}, d, {preset: frameDefault(leaf.options, client && client.manifest)});
      changed = true;
    }
    if (changed && unbindHash && typeof unbindHash.setDefaults === 'function') unbindHash.setDefaults(pageDefaults);
  }

  function ensureSelection(ds, client) {
    if (app.selections.has(ds)) return app.selections.get(ds);
    const ref = page.datasets[ds] || {};
    let sel = typeof client.selection === 'function' ? client.selection() : null;
    if (!sel || typeof sel.apply !== 'function' || typeof sel.onChange !== 'function' || !sel.mask) {
      sel = new Selection(client.n);
    }
    app.selections.set(ds, sel);
    if (!ref.context) {
      sel.onChange((s) => {
        store.set({[`sel:${ds}`]: {version: s.version, count: s.count}}, {source: 'selection'});
        if (ds === primaryDs) prefetchRecipeIds(ds, s);
      });
      store.set({[`sel:${ds}`]: {version: sel.version, count: sel.count}}, {source: 'selection'});
      if (pendingSel.has(ds)) {
        const str = pendingSel.get(ds);
        pendingSel.delete(ds);
        app.applySelection(ds, str).catch((err) => {
          store.emit('toast', {level: 'warn', text: `Could not restore the selection from the link: ${err.message}`});
        });
      }
    }
    return sel;
  }

  app.selectionFor = async (ds) => ensureSelection(ds, await app.catalog(ds));

  // Exact alias lookup: manifest ids.alias_file ({"alias": stored row}, section 5.2), loaded
  // once per dataset on the first id that is not an exact id match.
  // The newline-separated id list of a catalog (stored order), fetched from the worker once per
  // dataset on the first batch lookup and kept for later imports (about 12 bytes per row).
  const idLists = new Map();      // ds -> Promise<string>
  function idListText(ds, client) {
    if (!idLists.has(ds)) {
      const p = client.export({mask: 'all', format: 'ids'}).then((blob) => blob.text());
      p.catch(() => { if (idLists.get(ds) === p) idLists.delete(ds); });
      idLists.set(ds, p);
    }
    return idLists.get(ds);
  }

  const aliasMaps = new Map();    // ds -> Promise<Map(alias -> i) | null>
  function aliasMap(ds, client) {
    if (aliasMaps.has(ds)) return aliasMaps.get(ds);
    const load = async () => {
      const ids = client.manifest && client.manifest.ids;
      const file = ids && ids.alias_file;
      const ref = page.datasets[ds];
      if (!file || !ref || !ref.url) return null;
      const base = typeof hub.resolve === 'function' ? hub.resolve(ref.url) : new URL(ref.url, document.baseURI);
      const resp = await fetch(new URL(file, base));
      if (!resp.ok) {
        console.warn(`alias lookup unavailable for ${ds}: HTTP ${resp.status}`);
        return null;
      }
      let bytes = new Uint8Array(await resp.arrayBuffer());
      if (bytes.length > 1 && bytes[0] === 0x1f && bytes[1] === 0x8b && typeof DecompressionStream === 'function') {
        const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'));
        bytes = new Uint8Array(await new Response(stream).arrayBuffer());
      }
      const obj = JSON.parse(new TextDecoder().decode(bytes));
      const m = new Map();
      for (const [alias, i] of Object.entries(obj || {})) if (Number.isInteger(i) && i >= 0 && i < client.n) m.set(alias, i);
      return m;
    };
    const p = load().catch((err) => {
      // fetch network errors and gzip stream errors are TypeErrors; bad JSON is a SyntaxError.
      if (!(err instanceof TypeError || err instanceof SyntaxError)) throw err;
      console.warn(`alias lookup unavailable for ${ds}`, err);
      return null;
    });
    aliasMaps.set(ds, p);
    return p;
  }

  /**
   * ids -> stored row indices (-1 when not found). A row matches when its id equals the text
   * exactly (first stored row wins) or when the text is one of its aliases (alias file); substring
   * hits never count. Ids resolved before answer from a per-dataset cache; the rest go to the
   * worker in one DataClient.resolveIds pass (IMPLEMENTATION_NOTES 102). Clients without
   * resolveIds (test doubles) use the id-list / find path below.
   */
  const resolved = new Map();     // ds -> Map(text -> row), exact ids and aliases
  app.resolveIds = async (ds, ids) => {
    const client = await app.catalog(ds);
    const list = Array.from(ids || [], (raw) => String(raw));
    const out = new Array(list.length).fill(-1);
    const known = idIndex.get(ds);
    if (!resolved.has(ds)) resolved.set(ds, new Map());
    const memo = resolved.get(ds);
    let todo = [];
    list.forEach((id, k) => {
      let i = known && known.get(id);
      if (i === undefined) i = memo.get(id);
      if (i !== undefined) out[k] = i; else todo.push(k);
    });
    if (todo.length && typeof client.resolveIds === 'function') {
      const uniq = Array.from(new Set(todo.map((k) => list[k])));
      const rows = await client.resolveIds(uniq, {aliases: true});
      const at = new Map(uniq.map((id, j) => [id, rows[j]]));
      for (const k of todo) {
        const i = at.get(list[k]);
        out[k] = Number.isInteger(i) ? i : -1;
      }
      for (const [id, i] of at) if (Number.isInteger(i) && i >= 0) memo.set(id, i);
      return out;
    }
    if (todo.length && (todo.length >= RESOLVE_BATCH_MIN || idLists.has(ds)) && typeof client.export === 'function') {
      const lineSafe = (id) => !/[\r\n]/.test(id);
      const found = matchIdList(await idListText(ds, client), new Set(todo.map((k) => list[k]).filter(lineSafe)));
      const rest = [];
      for (const k of todo) {
        const i = found.get(list[k]);
        if (i !== undefined) { out[k] = i; cacheId(ds, i, list[k]); } else rest.push(k);
      }
      const am = rest.length ? await aliasMap(ds, client) : null;
      todo = [];
      for (const k of rest) {
        if (am && am.has(list[k])) out[k] = am.get(list[k]);
        else if (!lineSafe(list[k])) todo.push(k);
      }
    }
    for (const k of todo) {
      const id = list[k];
      const cands = await client.find(id, {limit: 10});
      let hit = -1;
      for (const i of cands || []) {
        const cid = String(await client.id(i));
        cacheId(ds, i, cid);
        if (cid === id) { hit = i; break; }
      }
      if (hit < 0) {
        const am = await aliasMap(ds, client);
        if (am && am.has(id)) hit = am.get(id);
      }
      out[k] = hit;
    }
    return out;
  };

  app.idOf = async (ds, i) => {
    const c = idCache.get(ds);
    if (c && c.has(i)) return c.get(i);
    const client = await app.catalog(ds);
    const id = await client.id(i);
    cacheId(ds, i, id);
    return id;
  };

  /**
   * Foreign record sets (shared contract (A)): hub.records(ds) opens the foreign index
   * (recordIndexUrl). PNG-mode sidecars resolve against the foreign portal's page directory
   * (foreignPageBase), with this page's {assets}/{res} values substituted (absolute shared bases,
   * or relative ones that then point into the foreign portal). With `id_column` an idOf maps a
   * catalog row id to the row's value in id_column (row lookup through app.resolveIds +
   * client.record on ref.catalog). Other refs use the hub as before.
   */
  const hubRecords = hub.records;
  const foreignStores = new Map(); // ds -> Promise<RecordStore>
  app.foreignRecordKey = async (ds, id, opts = {}) => {
    const ref = page.datasets[ds] || {};
    if (!ref.id_column || !ref.catalog) return id === null || id === undefined ? null : String(id);
    const [i] = await app.resolveIds(ref.catalog, [String(id)]);
    if (!(i >= 0)) return null;
    const client = await app.catalog(ref.catalog);
    const row = await client.record(i, {signal: opts.signal});
    return foreignKeyOf(row, ref.id_column);
  };
  hub.records = (ds) => {
    const ref = page.datasets[ds];
    if (!isForeignRef(ref)) return hubRecords(ds);
    if (foreignStores.has(ds)) return foreignStores.get(ds);
    let url = ds;
    const p = (async () => {
      const rel = recordIndexUrl(ref);
      if (!rel) throw new Error('foreign record set has no url or base');
      url = typeof hub.resolve === 'function' ? hub.resolve(rel) : new URL(rel, document.baseURI);
      return RecordStore.open(String(url), {
        base: foreignPageBase(url), page: {assets: page.assets, res: page.res},
        idOf: ref.id_column ? (id, o) => app.foreignRecordKey(ds, id, o) : undefined,
      });
    })().catch((err) => {
      foreignStores.delete(ds);
      if (err && err.name === 'AbortError') throw err;
      store.emit('data:error', {ds, url: String(url), status: Number.isInteger(err && err.status) ? err.status : 0, message: (err && err.message) || String(err)});
      store.emit('toast', {level: 'error', text: `Could not load ${ds}: ${(err && err.message) || err}`});
      throw err;
    });
    foreignStores.set(ds, p);
    return p;
  };

  let unbindHash = null;

  /** Rows whose ids Selection.serialize may ask for (index-only ids ops of the recipe). */
  function idRowsOf(sel) {
    if (typeof sel.recipeIndices === 'function') return sel.recipeIndices();
    return sel.count <= PREFETCH_IDS_MAX ? sel.indices() : new Int32Array(0);
  }

  // Fetch the ids that index-only recipe ops need, then rewrite the hash once they are known.
  // The sel= form itself is chosen by the Selection (IMPLEMENTATION_NOTES 92).
  async function prefetchRecipeIds(ds, sel) {
    if (sel.count === 0) return;
    // A recipe with brush ops is written as bits: while a mask form exists (Selection.serialize).
    if (sel.recipeExact === false && typeof sel.bitsForm === 'function' && sel.n <= BITS_IN_HASH_MAX_N) return;
    const rows = idRowsOf(sel);
    if (!rows.length || rows.length > PREFETCH_IDS_MAX) return;
    const c = idCache.get(ds);
    const missing = Array.from(rows).filter((i) => !(c && c.has(i)));
    if (!missing.length) return;
    const version = sel.version;
    try {
      await Promise.all(missing.map((i) => app.idOf(ds, i)));
    } catch (err) {
      // Worker/network failures surface as Error subclasses; the hash keeps the bits:/'' form.
      if (!(err instanceof Error)) throw err;
      console.warn(`could not fetch selected ids of ${ds}`, err);
      return;
    }
    if (sel.version === version && unbindHash) unbindHash.touch();
  }

  /** Synchronous idOf over the id cache, or undefined while a needed id is still unknown. */
  function syncIdOf(ds, sel) {
    const c = idCache.get(ds) || new Map();
    for (const i of idRowsOf(sel)) if (!c.has(i)) return undefined;
    return (i) => c.get(i);
  }

  /** Apply a serialized selection ('ids:' | 'bits:' | 'r:' | '') to dataset ds. */
  app.applySelection = async (ds, str) => {
    const sel = await app.selectionFor(ds);
    const parsed = Selection.parse(str || '');
    if (!parsed) {
      if (!str) sel.clear();
      else store.emit('toast', {level: 'warn', text: 'The selection in this link could not be read.'});
      return sel;
    }
    if (parsed.kind === 'ids') {
      const idx = await app.resolveIds(ds, parsed.ids);
      const missing = idx.filter((i) => i < 0).length;
      sel.setIndices(idx.filter((i) => i >= 0), 'replace', {op: 'replace', kind: 'ids', view: {ds}, ids: parsed.ids});
      if (missing) store.emit('toast', {level: 'warn', text: `${missing} linked source(s) were not found.`});
    } else if (parsed.kind === 'bits') {
      sel.apply(unpackBits(parsed.bytes, sel.n), 'replace');
    } else {
      // Recipes hold the ops since the last replace/clear and may start with add/subtract, so
      // they are replayed on an empty scratch Selection and loaded as one replace.
      const tmp = new Selection(sel.n, {maxUndo: 1});
      await replayRecipe(ds, tmp, parsed.ops);
      if (typeof sel.replaceWith === 'function') sel.replaceWith(tmp.mask, tmp.recipe(), tmp.recipeComplete);
      else sel.apply(tmp.mask, 'replace');
    }
    return sel;
  };

  /**
   * Rows a serialized selection ('ids:' | 'bits:' | 'r:') selects on dataset ds, as a Uint8Array
   * mask over the catalog, without touching the dataset's Selection (recipes replay on a scratch
   * Selection). Null when the text cannot be read. Used by ui/chips.js so that recipe presets
   * combine with shift (add) / alt (subtract) like the other presets.
   */
  app.selectionHit = async (ds, str) => {
    const parsed = str ? Selection.parse(str) : null;
    if (!parsed) return null;
    const client = await app.catalog(ds);
    const n = client.n;
    if (parsed.kind === 'bits') return unpackBits(parsed.bytes, n);
    const tmp = new Selection(n, {maxUndo: 1});
    if (parsed.kind === 'ids') tmp.setIndices((await app.resolveIds(ds, parsed.ids)).filter((i) => i >= 0), 'replace');
    else await replayRecipe(ds, tmp, parsed.ops);
    return tmp.mask.slice();
  };

  async function recipeHit(ds, client, op, n) {
    if (op.kind === 'ids') {
      const idx = await app.resolveIds(ds, op.ids || []);
      const hit = new Uint8Array(n);
      for (const i of idx) if (i >= 0) hit[i] = 1;
      return {hit};
    }
    if (op.kind === 'rect' || op.kind === 'circle' || op.kind === 'lasso') {
      const v = op.view || {};
      if (v.frame || !v.x || !v.y) return null;
      const view = {pane: v.pane || 'recipe', kind: 'xy', x: v.x, y: v.y, xlog: !!v.xlog, ylog: !!v.ylog};
      const res = await client.brush(view, Object.assign({kind: op.kind}, op.shape || {}));
      return {hit: res.hit, drawable: res.drawable};
    }
    if (op.kind === 'filter' && op.filters) {
      const res = await client.query(op.filters);
      return {hit: res.vis};
    }
    if (op.kind === 'preset') {
      const key = op.key || op.preset;
      const preset = (client.manifest.presets || []).find((p) => p.key === key && p.kind === 'selection');
      const s = op.sel || (preset && preset.sel);
      const parsed = s ? Selection.parse(s) : null;
      if (!parsed) return null;
      const tmp = new Selection(n, {maxUndo: 1});
      if (parsed.kind === 'ids') tmp.setIndices((await app.resolveIds(ds, parsed.ids)).filter((i) => i >= 0), 'replace');
      else if (parsed.kind === 'bits') tmp.apply(unpackBits(parsed.bytes, n), 'replace');
      else await replayRecipe(ds, tmp, parsed.ops);
      return {hit: tmp.mask.slice()};
    }
    return null;
  }

  async function replayRecipe(ds, sel, ops) {
    const client = await app.catalog(ds);
    let skipped = 0;
    for (const op of ops) {
      if (!op || !['replace', 'add', 'subtract', 'intersect'].includes(op.op)) { skipped++; continue; }
      const r = await recipeHit(ds, client, op, sel.n);
      if (!r) { skipped++; continue; }
      sel.apply(r.hit, op.op, r.drawable, op);
    }
    if (skipped) store.emit('toast', {level: 'warn', text: `${skipped} step(s) of the linked selection could not be replayed.`});
  }

  /**
   * Select rows from import text (CSV/TSV with an id or ra/dec header, ids or positions one per
   * line, DS9 regions, JSON), IMPLEMENTATION_NOTES 93. Parsing and matching use ui/export.js
   * parseImportText + resolveImport (ids and aliases first, then a positional crossmatch when
   * the catalog has positions). opts: {mode: 'replace'|'add'|'subtract'|'intersect' (default
   * 'replace'), ds (default the primary dataset), radiusArcsec}. Resolves to counts:
   * {ds, mode, format, entries, skipped, matched, byId, byPosition, missing, missingLabels,
   *  rows, count, summary}. Nothing is applied when the text holds no entries.
   */
  app.importCsv = async (text, opts = {}) => {
    const mode = opts.mode ?? 'replace';
    if (!OPS.includes(mode)) throw new TypeError(`unknown import mode: ${mode}`);
    const ds = opts.ds || primaryDs;
    if (!ds || !page.datasets[ds] || page.datasets[ds].kind !== 'catalog') {
      throw new Error(ds ? `not a catalog dataset: ${ds}` : 'this page has no catalog to import into');
    }
    const [io, client] = await Promise.all([import('../ui/export.js'), app.catalog(ds)]);
    const sel = await app.selectionFor(ds);
    const out = await importSelection(text, {
      mode, sel, client, io, radiusArcsec: opts.radiusArcsec, resolveIds: (ids) => app.resolveIds(ds, ids),
    });
    return Object.assign({ds}, out);
  };

  /** Payload for carry.js stashCarry (used by ui/nav.js on clicks to sibling pages). */
  app.carryPayload = () => {
    const f = store.get('focus');
    const ds = (f && f.ds) || primaryDs;
    const sel = ds && app.selections.get(ds);
    return {ds, focusId: f && f.id != null ? String(f.id) : undefined,
      sel: sel && sel.count ? sel.serialize(syncIdOf(ds, sel)) : undefined};
  };

  app.disableCap = (cap, reason) => {
    app.caps.delete(cap);
    app.disabled[cap] = reason;
    if (document.body) document.body.dataset.caps = Array.from(app.caps).sort().join(' ');
    store.emit('caps:change', {cap, enabled: false, reason});
  };

  const prefetch = new Set();
  const catOf = (ds) => {
    const r = page.datasets[ds];
    if (!r) return null;
    if (r.kind === 'catalog') return ds;
    if (r.catalog && page.datasets[r.catalog] && page.datasets[r.catalog].kind === 'catalog') return r.catalog;
    return null;
  };
  for (const id of norm.order) {
    const leaf = norm.panes.get(id);
    if (leaf.collapsed) continue;
    const c = leaf.dataset && catOf(leaf.dataset);
    if (c) prefetch.add(c);
    const bg = leaf.options && leaf.options.background;
    if (bg && catOf(bg)) prefetch.add(bg);
  }
  if (pendingSel.size) for (const ds of pendingSel.keys()) if (catOf(ds)) prefetch.add(ds);
  for (const ds of prefetch) app.catalog(ds);

  // 4. runtime capabilities and tiers ----------------------------------------------------
  const rt = detectRuntime();
  app.runtime = rt;
  if (!rt.webgl2) app.notes.density_rendering = 'WebGL2 unavailable; pre-binned fallback (tier F)';
  else if (!rt.floatRT) app.notes.density_rendering = 'EXT_color_buffer_float unavailable; density panes use tier F';
  if (!rt.decompressionStream) app.notes.lazy_data_loading = 'DecompressionStream unavailable; pure-JS inflate fallback';
  for (const id of norm.order) {
    const leaf = norm.panes.get(id);
    if (!TIERED_PANES.includes(leaf.type)) continue;
    const c = leaf.dataset && catOf(leaf.dataset);
    if (!c) continue;
    const assign = (client) => {
      if (!store.get(`tier:${id}`)) store.set({[`tier:${id}`]: tierFor(client.n, rt)}, {source: 'boot'});
    };
    if (prefetch.has(c)) app.catalog(c).then(assign, () => {});
  }

  // 5. chrome + layout -------------------------------------------------------------------
  await cssReady;
  const root = document.getElementById('pk-app') || document.getElementById('pk-root')
    || document.querySelector('[data-pk-root]') || document.body.appendChild(el('div'));
  root.classList.add('pk-app');
  root.textContent = '';
  const header = el('header', 'pk-header');
  const brand = el('div', 'pk-brand');
  const portalTitle = el('span', 'pk-portal-title', (page.portal && page.portal.title) || '');
  const h1 = el('h1', 'pk-page-title', (page.page && page.page.title) || '');
  brand.append(portalTitle, h1);
  if (page.page && page.page.subtitle) brand.append(el('span', 'pk-subtitle', page.page.subtitle));
  const navSlot = el('nav', 'pk-nav-slot');
  navSlot.setAttribute('aria-label', 'Portal pages');
  const tools = el('div', 'pk-header-tools');
  const selChip = el('button', 'pk-chip pk-sel-chip');
  selChip.type = 'button';
  selChip.hidden = true;
  selChip.title = 'Clear selection (x)';
  const helpBtn = el('button', 'pk-btn pk-icon-btn pk-help-btn', '?');
  helpBtn.type = 'button';
  helpBtn.title = 'Help and keyboard shortcuts (?)';
  helpBtn.setAttribute('aria-label', 'Help');
  tools.append(selChip, helpBtn, theme.makeToggleButton());
  header.append(brand, navSlot, tools);
  const main = el('main', 'pk-main');
  const selbar = el('footer', 'pk-selbar');
  selbar.setAttribute('aria-live', 'polite');
  root.append(header, main, selbar);
  app.root = root;
  app.slots = {header, nav: navSlot, tools, main, selbar};
  document.title = [page.page && page.page.title, page.portal && page.portal.title].filter(Boolean).join(' · ') || document.title;

  const layout = mountLayout(main, page.layout, {app, registry});
  app.layout = layout;

  // Selection summary (header chip on desktop, pinned bar on mobile).
  const renderSel = () => {
    const ds = primaryDs;
    const s = ds && store.get(`sel:${ds}`);
    const sel = ds && app.selections.get(ds);
    const count = s ? s.count : 0;
    const f = store.get('focus');
    selChip.hidden = !count;
    selChip.textContent = count ? `${fmtCount(count)} selected ×` : '';
    const parts = [];
    parts.push(count ? `${fmtCount(count)} of ${fmtCount(sel ? sel.n : 0)} selected` : 'No selection');
    if (f && f.id != null) parts.push(`focus: ${f.id}`);
    selbar.textContent = parts.join(' · ');
  };
  selChip.addEventListener('click', () => {
    const sel = primaryDs && app.selections.get(primaryDs);
    if (sel) sel.clear();
  });
  store.on(['sel:*', 'focus'], renderSel);
  renderSel();
  helpBtn.addEventListener('click', () => openHelp());

  // Focus resolution (id <-> i) and cross-page broadcast. Also run once for the initial focus
  // (hash, carry, legacy mapping), which the listener never sees as a change.
  const resolveFocus = (st, sources) => {
    const f = st.get('focus');
    if (!f || !f.ds) return;
    const missingI = !Number.isInteger(f.i) || f.i < 0;
    const missingId = f.id === null || f.id === undefined || f.id === '';
    if (missingI && !missingId) {
      app.resolveIds(f.ds, [f.id]).then(([i]) => {
        const cur = st.get('focus');
        if (!cur || cur.id !== f.id || cur.ds !== f.ds) return;
        if (i >= 0) st.set({focus: {ds: f.ds, i, id: f.id}}, {source: 'boot'});
        else st.emit('toast', {level: 'warn', text: `Source "${f.id}" was not found.`});
      }, () => {});
    } else if (!missingI && missingId) {
      app.idOf(f.ds, f.i).then((id) => {
        const cur = st.get('focus');
        if (cur && cur.i === f.i && cur.ds === f.ds) st.set({focus: {ds: f.ds, i: f.i, id}}, {source: 'boot'});
      }, () => {});
    } else if (!missingId && !sources.has('broadcast') && !sources.has('boot')) {
      broadcastFocus(portalId, f.id, f.ds);
    }
  };
  store.on(['focus'], (changed, st, sources) => resolveFocus(st, sources), {sync: true});
  resolveFocus(store, new Set(['boot']));
  if (primaryDs) {
    // Only ids of this page's primary dataset are adopted (section 6.5 carries same-ds only);
    // the hash binding replaces the history entry for source 'broadcast'.
    onBroadcastFocus(portalId, (id, ds) => {
      if (ds !== primaryDs) return;
      const f = store.get('focus');
      if (f && f.ds === ds && String(f.id) === id) return;
      store.set({focus: {ds, i: null, id}}, {source: 'broadcast'});
    });
  }

  // UI widgets (WP11): optional modules exporting install(app). Their CSS (ui.css) loaded with
  // cssReady, before mountLayout.
  await Promise.all(UI_MODULES.map(async (name) => {
    let mod;
    try {
      mod = await import(`../ui/${name}.js`);
    } catch (err) {
      // A module that is absent from this build is expected (warn); any other load failure
      // (syntax error, failing top-level code) is a bug and goes to console.error.
      if (isMissingModule(err)) console.warn(`ui/${name}.js not available`, err);
      else console.error(`ui/${name}.js failed to load`, err);
      return;
    }
    if (typeof mod.install !== 'function') return;
    try {
      await mod.install(app);
    } catch (err) {
      console.error(`ui/${name}.js install failed`, err);
    }
  }));
  // Built-in help fallback when ui/help.js did not claim the event.
  store.onEvent('help:toggle', (p) => { if (p && !p.handled) { p.handled = true; openHelp(); } });

  // 6. caps / tiers on <body> ------------------------------------------------------------
  document.body.dataset.caps = Array.from(app.caps).sort().join(' ');
  const renderTiers = () => {
    const parts = [];
    for (const id of norm.order) {
      const t = store.get(`tier:${id}`);
      if (t) parts.push(`${id}:${t}`);
    }
    document.body.dataset.tier = parts.join(' ');
  };
  store.on(['tier:*'], renderTiers, {sync: true});
  renderTiers();

  // 7. hash, keys, theme, test hooks ------------------------------------------------------
  unbindHash = bindHash(store, {
    primaryDs,
    defaults: pageDefaults,
    applyInitial: false,
    selection: primaryDs ? {
      serialize: () => {
        const sel = app.selections.get(primaryDs);
        if (!sel) return pendingSel.get(primaryDs) || '';
        return sel.serialize(syncIdOf(primaryDs, sel));
      },
      count: () => {
        const sel = app.selections.get(primaryDs);
        return sel ? sel.count : 0;
      },
      apply: (str) => {
        app.applySelection(primaryDs, str).catch((err) => store.emit('toast', {level: 'warn', text: err.message}));
      },
    } : null,
  });
  if (legacyUsed) unbindHash.replaceNow();
  // bindHash emits this once when the hash starts leaving the selection out.
  store.onEvent('hash:sel-dropped', () => {
    store.emit('toast', {level: 'warn', text: 'The selection is too large for the link; copied links will not include it.'});
  });
  app.hashLink = () => unbindHash.compute();
  /** {str, dropped, omitted}: for "copy link" actions that warn when the selection is left out. */
  app.hashLinkInfo = () => unbindHash.computeInfo();
  bindKeys(app);
  theme.bindTheme(store);
  if (hooks) hooks.attach(app);

  // 8. ready -----------------------------------------------------------------------------
  const ms = (typeof performance !== 'undefined' ? performance.now() : Date.now()) - t0;
  app.readyMs = ms;
  store.emit('ready', {ms});
  whenFirstPaint(root).then(() => {
    firstPaintAtMs = Date.now();
    firstPaintResolve();
  });
  return app;

  function openHelp() {
    const handled = {handled: false};
    store.emit('help:open', handled);
    if (handled.handled) return;
    let dlg = document.querySelector('dialog.pk-help-fallback');
    if (!dlg) {
      dlg = document.createElement('dialog');
      dlg.className = 'pk-help-fallback pk-dialog';
      document.body.append(dlg);
    }
    const caps = Array.from(app.caps).sort();
    const dis = Object.entries(app.disabled);
    dlg.innerHTML = `<form method="dialog"><h2>Keyboard shortcuts</h2><table class="pk-keymap">${
      KEYMAP.map((k) => `<tr><th>${escapeHtml(k.keys)}</th><td>${escapeHtml(k.action)}</td></tr>`).join('')
    }</table><h2>Capabilities</h2><p class="pk-muted">${caps.map(escapeHtml).join(', ')}</p>${
      dis.length ? `<h3>Not available here</h3><ul>${dis.map(([c, r]) => `<li><code>${escapeHtml(c)}</code>: ${escapeHtml(r)}</li>`).join('')}</ul>` : ''
    }<p><button class="pk-btn" value="close">Close</button></p></form>`;
    if (typeof dlg.showModal === 'function') dlg.showModal();
  }
}

// Unused-import guard for tools that tree-shake: formatHash/stateToHash are re-exported for
// pane authors who build "copy link" actions without importing hash.js directly.
export {formatHash, stateToHash};
