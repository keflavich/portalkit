// Legacy URL grammars -> canonical hash keys (ARCHITECTURE.md section 6.4).
//
// Pages migrated from the legacy generators keep their old deep links working. The page JSON
// lists the grammars the legacy page used (page.page.legacy.grammar); boot.js calls
//   mapLegacy(grammars, {hash: location.hash, search: location.search})
// and merges the returned canonical keys into the store. `consumedSearch` lists the query
// parameters the mapping used, so boot can strip them from the address bar.
//
// Output formats follow IMPLEMENTATION_NOTES item 33: c=by~cmap~lo~hi, <pane>.frame=preset|b64:<f32 LE>,
// f=base64url JSON, sky=ra,dec,fov, lb=set,index, view=pane:mode, and gc-query selections as
// sel=r:<base64url(deflate-raw(JSON RecipeOp[]))>.
//
// Grammars that index dims (gc-query a/b/m, x1/x2/y1/y2, cb, cd, y; kv-view x/y) need the dim
// list. It comes from loc.dims ([key] or [{key, scale}]) when the caller passes it, else from
// the manifold pane's `options.dims` in the page JSON (<script id="pk-page">). The build
// (portalkit/build.py) writes that option as an explicit key list on pages whose legacy
// grammar indexes dims, so "group:<g>" and manifest-default dims reach this module resolved.
// Without a dim list the dim-indexed query parameters are left unconsumed, so the original
// URL stays in the address bar. Dim scales (for frames that reproduce raw-unit colours) come
// from loc.dims objects only; without them the frame uses unit weights, which keeps the axis
// orientation and dim choice.
//
// Decoding: query strings and kv-view hashes (written with URLSearchParams) decode '+' as a
// space; the other hash grammars use decodeURIComponent only, because legacy pages set
// `location.hash = name` raw and source names contain '+' (IRAS_04381+2540_683).
//
// tests/fixtures/legacy_snippets/hash_vectors.json holds the shared vectors that the Python
// mirror (portalkit/legacy/hashmap.py) is tested against.
//
// Legacy dim names (shared contract (B)): page.page.legacy.dim_alias maps legacy dim keys onto
// the unified ones ({"c_F162M_F210M": "f162m-f210m", "m_F162M": "f162m"}). kv-view applies it
// to its x / y / z / c values and spherex-colours to its filter and colour names before the dims
// are resolved (dimAlias / aliasDim).
//
// Tour speed (shared contract (D)): the canonical key is `speed` (a float multiplier, store key
// `speed`). The wavelength and kv-view (manifold) grammars map the legacy `?speed=` query
// parameter (consumed, also under a canonical hash) and a legacy `speed=` hash value onto it.

import {b64urlEncode, deflateRaw, utf8Encode} from './selection.js';

/** Canonical top-level hash keys (core/hash.js HASH_ORDER without '@pane'). */
export const CANONICAL_KEYS = Object.freeze([
  'id', 'sel', 'f', 'c', 'av', 'rv', 'dm', 'speed', 'tpl', 'feat', 'spec', 'wl',
  'sky', 'base', 'ovl', 'rung', 'beat', 'role', 'fac', 'lb', 'view',
]);
const CANON = new Set(CANONICAL_KEYS);
/** Pane-scoped canonical keys (core/hash.js PANE_KEY_RE). */
export const PANE_KEY_RE = /^(.+)\.(x|y|frame|xr|yr|raster|ovl)$/;

/** Radius of the gc-query `sel=ra,dec` circle, arcsec. */
export const SEL_RADIUS_ARCSEC = 0.5;
/** Field of view used when a wavelength link gives ra/dec without fov, degrees. */
export const DEFAULT_FOV_DEG = 0.25;
/** Bounds standing in for an open side of a gc-query criterion range. */
const OPEN_LO = -1e300;
const OPEN_HI = 1e300;

// ---------------------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------------------

function safeDecode(s, plus = false) {
  const t = plus ? String(s).replace(/\+/g, ' ') : String(s);
  try {
    return decodeURIComponent(t);
  } catch (err) {
    if (err instanceof URIError) return t;
    throw err;
  }
}

function hashText(loc) {
  let h = String((loc && loc.hash) || '');
  if (h.startsWith('#')) h = h.slice(1);
  return h;
}

/** `a=1&b=2` -> {a, b}; `plus` decodes '+' as a space (URLSearchParams / form encoding). */
function parseKV(text, plus = false) {
  const out = {};
  for (const part of String(text).split('&')) {
    if (!part) continue;
    const i = part.indexOf('=');
    if (i < 0) out[safeDecode(part, plus)] = '';
    else out[safeDecode(part.slice(0, i), plus)] = safeDecode(part.slice(i + 1), plus);
  }
  return out;
}

function parseSearch(loc) {
  let s = String((loc && loc.search) || '');
  if (s.startsWith('?')) s = s.slice(1);
  return parseKV(s, true);
}

function num(s) {
  if (s === undefined || s === null || String(s).trim() === '') return undefined;
  const v = Number(s);
  return Number.isFinite(v) ? v : undefined;
}

function int(s) {
  const v = num(s);
  return v !== undefined && Number.isInteger(v) ? v : undefined;
}

/** Short decimal text (as core/hash.js num()). */
function fmt(v) {
  const r = Math.abs(v) >= 1e-3 ? Math.round(v * 1e6) / 1e6 : Number(v.toPrecision(7));
  return String(Object.is(r, -0) ? 0 : r);
}

/** JSON -> base64url(UTF-8 JSON), as core/hash.js encodeJSONParam. */
export function encodeJSON(v) {
  return b64urlEncode(utf8Encode(JSON.stringify(v)));
}

/** number[] -> 'b64:' + base64url(f32 little-endian). */
export function encodeFrame(F) {
  const b = new Uint8Array(F.length * 4);
  const dv = new DataView(b.buffer);
  for (let i = 0; i < F.length; i++) dv.setFloat32(i * 4, F[i], true);
  return `b64:${b64urlEncode(b)}`;
}

/** RecipeOp[] -> 'r:' + base64url(deflate-raw(JSON)). */
export function encodeRecipe(ops) {
  return `r:${b64urlEncode(deflateRaw(utf8Encode(JSON.stringify(ops))))}`;
}

/** Is the hash already in canonical form (every key canonical, `view` values pane:mode)? */
export function isCanonicalHash(text) {
  if (!text || text.indexOf('=') < 0) return false;
  const kv = parseKV(text);
  const keys = Object.keys(kv);
  if (!keys.length) return false;
  for (const k of keys) {
    if (!CANON.has(k) && !PANE_KEY_RE.test(k)) return false;
  }
  if ('view' in kv && kv.view && kv.view.indexOf(':') < 0) return false;
  return true;
}

function canonicalPassThrough(kv) {
  const out = {};
  for (const [k, v] of Object.entries(kv)) {
    if (k === 'view' && v && v.indexOf(':') < 0) continue;
    if (CANON.has(k) || PANE_KEY_RE.test(k)) out[k] = v;
  }
  return out;
}

/** Page JSON from <script id="pk-page"> when running in a document; null otherwise. */
function pageJSON() {
  const doc = globalThis.document;
  if (!doc || typeof doc.getElementById !== 'function') return null;
  const el = doc.getElementById('pk-page');
  if (!el || !el.textContent) return null;
  try {
    return JSON.parse(el.textContent);
  } catch (err) {
    if (err instanceof SyntaxError) return null;
    throw err;
  }
}

function* panesOf(node) {
  if (!node || typeof node !== 'object') return;
  if (Array.isArray(node.row)) for (const c of node.row) yield* panesOf(c);
  else if (Array.isArray(node.col)) for (const c of node.col) yield* panesOf(c);
  else if (typeof node.pane === 'string') yield node;
}

/** Pane ids of the page layout and the first manifold pane id; null without a page JSON. */
export function layoutInfo(loc = {}) {
  const page = loc.page || pageJSON();
  if (!page || !page.layout) return null;
  const ids = new Set();
  let manifold = null;
  for (const p of panesOf(page.layout)) {
    const id = p.id || p.pane;
    ids.add(id);
    if (p.pane === 'manifold' && manifold === null) manifold = id;
  }
  return {ids, manifold};
}

/**
 * Legacy dim alias table of the page (page.page.legacy.dim_alias, contract (B)): loc.dimAlias
 * wins, then loc.page, then the page JSON in the document. {} when there is none.
 */
export function dimAlias(loc = {}) {
  if (loc.dimAlias && typeof loc.dimAlias === 'object') return loc.dimAlias;
  const page = loc.page || pageJSON();
  const a = page && page.page && page.page.legacy && page.page.legacy.dim_alias;
  return a && typeof a === 'object' && !Array.isArray(a) ? a : {};
}

/** A legacy dim key through the alias table (unchanged when the table has no entry). */
export function aliasDim(alias, key) {
  if (key === undefined || key === null) return key;
  const k = String(key);
  return alias && Object.prototype.hasOwnProperty.call(alias, k) && typeof alias[k] === 'string' ? alias[k] : k;
}

/** Scatter panes of the page layout in layout order: [{id, options}]. */
export function scatterPanes(loc = {}) {
  const page = loc.page || pageJSON();
  const out = [];
  if (!page || !page.layout) return out;
  for (const p of panesOf(page.layout)) {
    if (p.pane === 'scatter') out.push({id: p.id || p.pane, options: p.options || {}});
  }
  return out;
}

/**
 * Context for the dim-indexed grammars: {dims: [{key, scale?}], pane, ds}.
 * loc.dims / loc.pane / loc.ds win; the page JSON fills the gaps.
 */
export function legacyContext(loc = {}) {
  const ctx = {dims: null, pane: loc.pane || null, ds: loc.ds || null};
  if (Array.isArray(loc.dims)) {
    ctx.dims = loc.dims.map((d) => (typeof d === 'string' ? {key: d} : {key: String(d.key), scale: num(d.scale)}));
  }
  if (!ctx.dims || !ctx.pane || !ctx.ds) {
    const page = loc.page || pageJSON();
    if (page) {
      let mani = null;
      for (const p of panesOf(page.layout)) {
        if (p.pane === 'manifold') { mani = p; break; }
      }
      if (mani) {
        if (!ctx.pane) ctx.pane = mani.id || 'manifold';
        if (!ctx.ds && mani.dataset) ctx.ds = mani.dataset;
        const od = mani.options && mani.options.dims;
        if (!ctx.dims && Array.isArray(od) && od.every((x) => typeof x === 'string')) {
          ctx.dims = od.map((k) => ({key: k}));
        }
      }
      if (!ctx.ds && page.datasets && typeof page.datasets === 'object') {
        const first = Object.keys(page.datasets)[0];
        if (first) ctx.ds = first;
      }
    }
  }
  if (!ctx.pane) ctx.pane = 'manifold';
  return ctx;
}

function dimAt(ctx, i) {
  if (!ctx.dims || i === undefined || i < 0 || i >= ctx.dims.length) return null;
  return ctx.dims[i];
}

function weight(d) {
  return d && Number.isFinite(d.scale) && d.scale > 0 ? d.scale : 1;
}

// ---------------------------------------------------------------------------------------
// grammars. Each rule returns {hash, consumed} or null.
// ---------------------------------------------------------------------------------------

function bareName(loc) {
  const h = hashText(loc);
  if (!h || h.indexOf('=') >= 0 || h.indexOf('&') >= 0) return null;
  const id = safeDecode(h).trim();
  return id ? {hash: {id}, consumed: []} : null;
}

const KV_ID_KEYS = ['name', 'src', 'source', 'id', 'star', 'obj', 'target'];

/**
 * `#name=..&view=..&x=..&y=..&c=..` (URLSearchParams-written; archive and 6927 manifolds).
 * `view=<pane id>` opens that pane full size; on a page with a manifold pane any other `view`
 * value is a frame preset (`<manifold>.frame=<preset>`). Without a page JSON the value is
 * taken as a pane id. Legacy axes `x=<dim>&y=<dim>` become a unit frame on those two dims.
 */
function kvView(loc) {
  // the query-string part (legacy `?speed=`, contract (D)) applies also under a canonical hash
  const sq = searchSpeed(loc);
  const res = kvViewHash(loc);
  if (!res && !sq.consumed.length) return null;
  return {hash: res ? res.hash : {}, searchHash: sq.searchHash, consumed: sq.consumed};
}

/** Legacy `?speed=` -> canonical `speed` (contract (D)); an unreadable value is consumed and dropped. */
function searchSpeed(loc) {
  const q = parseSearch(loc);
  const searchHash = {};
  const consumed = [];
  if ('speed' in q) {
    consumed.push('speed');
    const sp = speedText(q.speed);
    if (sp !== undefined) searchHash.speed = sp;
  }
  return {searchHash, consumed};
}

function kvViewHash(loc) {
  const h = hashText(loc);
  if (!h || h.indexOf('=') < 0 || isCanonicalHash(h)) return null;
  const kv = parseKV(h, true);
  const out = {};
  for (const k of KV_ID_KEYS) {
    if (kv[k]) { out.id = kv[k]; break; }
  }
  const info = layoutInfo(loc);
  const alias = dimAlias(loc);
  for (const k of ['x', 'y', 'z']) if (kv[k]) kv[k] = aliasDim(alias, kv[k]);
  if (kv.c) {
    // colour-by: `<by>[~cmap~lo~hi]`; only the column part is a dim name
    const t = kv.c.indexOf('~');
    const by = aliasDim(alias, t < 0 ? kv.c : kv.c.slice(0, t));
    const c = t < 0 ? by : by + kv.c.slice(t);
    if (c !== kv.c) out.c = c;
  }
  if (kv.speed !== undefined) {
    const sp = speedText(kv.speed);
    if (sp !== undefined) out.speed = sp;
    delete kv.speed;
  }
  if (kv.view) {
    const v = kv.view;
    if (v.indexOf(':') >= 0) out.view = v;
    else if (info && info.manifold !== null && !info.ids.has(v)) out[`${info.manifold}.frame`] = v;
    else out.view = `${v}:full`;
  } else if (kv.x && kv.y) {
    const ctx = legacyContext(loc);
    if (ctx.dims) {
      const keys = ctx.dims.map((d) => d.key);
      const ix = keys.indexOf(kv.x), iy = keys.indexOf(kv.y);
      if (ix >= 0 && iy >= 0) {
        const D = keys.length;
        const F = new Array(2 * D).fill(0);
        F[ix] = weight(ctx.dims[ix]);
        F[D + iy] = weight(ctx.dims[iy]);
        out[`${ctx.pane}.frame`] = encodeFrame(F);
      }
    }
  }
  if (!Object.keys(out).length) return null;
  return {hash: Object.assign(canonicalPassThrough(kv), out), consumed: []};
}

/** A positive speed multiplier as hash text, or undefined. */
function speedText(v) {
  const sp = num(v);
  return sp !== undefined && sp > 0 ? fmt(sp) : undefined;
}

function wavelength(loc) {
  const h = hashText(loc);
  const out = {};
  let kv = {};
  if (h && h.indexOf('=') >= 0) {
    if (isCanonicalHash(h)) kv = {};
    else kv = parseKV(h);
  } else if (h && /^[^,]+(,[-+0-9.eE]+){2,3}$/.test(h)) {
    // compact form "#rung,ra,dec[,fov]"
    const p = h.split(',');
    kv = {rung: safeDecode(p[0]), ra: p[1], dec: p[2], fov: p[3]};
  }
  if (kv.rung) out.rung = kv.rung;
  if (kv.speed !== undefined) {
    // legacy `speed=` in the hash: the tour / playback speed (contract (D))
    const sp = speedText(kv.speed);
    if (sp !== undefined) out.speed = sp;
    delete kv.speed;
  }
  const ra = num(kv.ra);
  const dec = num(kv.dec);
  if (ra !== undefined && dec !== undefined) {
    const fov = num(kv.fov);
    out.sky = `${fmt(ra)},${fmt(dec)},${fmt(fov !== undefined && fov > 0 ? fov : DEFAULT_FOV_DEG)}`;
  }
  const {searchHash: fromSearch, consumed} = searchSpeed(loc);
  if (!Object.keys(out).length && !consumed.length) return null;
  return {hash: Object.keys(out).length ? Object.assign(canonicalPassThrough(kv), out) : {}, searchHash: fromSearch, consumed};
}

function beat(loc) {
  const h = hashText(loc);
  if (!h || h.indexOf('=') < 0) return null;
  const kv = parseKV(h);
  if (!kv.beat || isCanonicalHash(h)) return null;
  return {hash: Object.assign(canonicalPassThrough(kv), {beat: kv.beat}), consumed: []};
}

function galleryIndex(loc) {
  const h = hashText(loc);
  if (!h) return null;
  if (/^\d+$/.test(h)) return {hash: {lb: String(parseInt(h, 10))}, consumed: []};
  if (h.indexOf('=') < 0) return null;
  const kv = parseKV(h);
  if (!('img' in kv) || kv.img === '') return null;
  const v = kv.img;
  const out = canonicalPassThrough(kv);
  if (/^\d+$/.test(v)) out.lb = String(parseInt(v, 10));
  else if (/^[^,]+,\d+$/.test(v)) out.lb = v;
  else out.id = v;
  return {hash: out, consumed: []};
}

const W51_RE = /^W51-\d+$/i;

function w51RowId(loc) {
  const h = safeDecode(hashText(loc)).trim();
  if (!W51_RE.test(h)) return null;
  return {hash: {id: h.toUpperCase()}, consumed: []};
}

function specviewer(loc) {
  const h = hashText(loc);
  if (!h || h.indexOf('=') >= 0 || h.indexOf('&') >= 0) return null;
  const id = safeDecode(h).trim();
  return id ? {hash: {id}, consumed: []} : null;
}

const GC_KEYS = ['a', 'b', 'm', 'p', 'x1', 'x2', 'y1', 'y2', 'cb', 'cd', 'cm', 'cl', 'ch', 'g', 'rv', 'fa', 'sel', 'y'];

function gcQuery(loc) {
  const q = parseSearch(loc);
  if (!GC_KEYS.some((k) => k in q)) return null;
  const ctx = legacyContext(loc);
  const D = ctx.dims ? ctx.dims.length : 0;
  const out = {};
  const consumed = [];
  const use = (...ks) => { for (const k of ks) if (k in q && !consumed.includes(k)) consumed.push(k); };
  const pane = ctx.pane;

  // projection: CCD (x1-x2 vs y1-y2) or CMD (a-b vs m)
  if (q.p === 'ccd') {
    const ix = [int(q.x1), int(q.x2), int(q.y1), int(q.y2)];
    const ds = ix.map((i) => dimAt(ctx, i));
    if (ds.every(Boolean)) {
      const F = new Array(2 * D).fill(0);
      F[ix[0]] += weight(ds[0]);
      F[ix[1]] -= weight(ds[1]);
      F[D + ix[2]] += weight(ds[2]);
      F[D + ix[3]] -= weight(ds[3]);
      out[`${pane}.frame`] = encodeFrame(F);
      use('p', 'x1', 'x2', 'y1', 'y2');
    }
  } else if ('a' in q || 'b' in q || 'm' in q) {
    const ia = int(q.a), ib = int(q.b), im = int(q.m);
    const da = dimAt(ctx, ia), db = dimAt(ctx, ib), dm = dimAt(ctx, im);
    if (da && db && dm) {
      const F = new Array(2 * D).fill(0);
      F[ia] += weight(da);
      F[ib] -= weight(db);
      F[D + im] = -1;          // magnitude axis, bright up
      out[`${pane}.frame`] = encodeFrame(F);
      use('a', 'b', 'm', 'p');
    }
  }
  // Treasury band toggle: CMD of (first - second) dim against the chosen band. An unknown
  // band is dropped; without a dim list `y` stays in the URL.
  if ('y' in q && q.y && ctx.dims) {
    const keys = ctx.dims.map((d) => d.key);
    const iy = keys.indexOf(q.y);
    if (iy >= 0 && D >= 2) {
      const F = new Array(2 * D).fill(0);
      F[0] += weight(ctx.dims[0]);
      F[1] -= weight(ctx.dims[1]);
      F[D + iy] = -1;
      out[`${pane}.frame`] = encodeFrame(F);
    }
    use('y');
  }
  // colour-by
  if ('cb' in q) {
    const icb = int(q.cb);
    if (icb === -1) {
      use('cb');                // density colouring is the default
    } else {
      const d = dimAt(ctx, icb);
      if (d) { out.c = d.key; use('cb'); }
    }
  }
  // criterion: filter (f=) or highlight (sel=r: filter op)
  if ('cd' in q) {
    const d = dimAt(ctx, int(q.cd));
    if (int(q.cd) === -1) {
      use('cd', 'cm', 'cl', 'ch');
    } else if (d) {
      const lo = num(q.cl), hi = num(q.ch);
      const filters = {ranges: {[d.key]: [lo !== undefined ? lo : OPEN_LO, hi !== undefined ? hi : OPEN_HI]}};
      if (q.cm === 'h') {
        out.sel = encodeRecipe([{op: 'replace', kind: 'filter', view: {ds: ctx.ds || ''}, filters}]);
      } else {
        out.f = encodeJSON(filters);
      }
      use('cd', 'cm', 'cl', 'ch');
    }
  }
  // Display toggles with no canonical state, consumed and dropped (ARCHITECTURE section 6.4
  // lists rv as the target, which holds a numeric R_V): rv=1 shows the GC reddening vector
  // (the unified manifold draws extinction vectors from the page's overlays option), g is
  // the density gain slider, fa=1 is the fit-all view toggle. A numeric rv (> 1.5) maps to rv=.
  if ('rv' in q) {
    const rv = num(q.rv);
    if (rv !== undefined && rv > 1.5) out.rv = fmt(rv);
    use('rv');
  }
  if ('g' in q) use('g');
  if ('fa' in q) use('fa');
  // selected star
  if ('sel' in q && q.sel) {
    if (q.sel.startsWith('r:') || q.sel.startsWith('ids:') || q.sel.startsWith('bits:')) {
      if (!out.sel) out.sel = q.sel;
      use('sel');
    } else {
      const p = q.sel.split(',');
      const ra = num(p[0]), dec = num(p[1]);
      if (p.length === 2 && ra !== undefined && dec !== undefined) {
        const r = SEL_RADIUS_ARCSEC / 3600;
        const cosd = Math.max(1e-6, Math.cos((dec * Math.PI) / 180));
        const op = {op: out.sel ? 'add' : 'replace', kind: 'circle', view: {ds: ctx.ds || '', x: 'ra', y: 'dec'},
          shape: {cx: ra, cy: dec, r: r / cosd, rx: r / cosd, ry: r}};
        out.sel = encodeRecipe([op]);
        use('sel');
      }
    }
  }
  if (!Object.keys(out).length && !consumed.length) return null;
  return {hash: out, consumed};
}

// ---------------------------------------------------------------------------------------
// spherex-colours (contract (C)): the query string of the legacy SPHEREx colours.html
//   ax, ay   CCD x axis = colour ax - ay       bx, by   CCD y axis = colour bx - by
//   c        colour-by column                  band     CMD magnitude band (CMD y axis)
//   cmd=1    CMD panel shown                   zoom     x0,x1,y0,y1 of the CCD
//   yoo      Yoo overlay: '1' = every yoo_* layer of the CCD, else a comma list of layer ids
//   hide     k1|k2 hidden levels of the colour-by category
// The legacy page plots colours of NIRCam filter names (F162M); dim_alias maps the colour
// `c_<A>_<B>` and the magnitude `m_<A>` onto unified dims, else the colour is `<a>-<b>` of the
// lower-cased names. The CMD shares the CCD x colour. On a page with a single scatter pane
// `band` and `cmd` are consumed and dropped.
// ---------------------------------------------------------------------------------------

const SPHX_KEYS = ['ax', 'ay', 'bx', 'by', 'c', 'band', 'cmd', 'zoom', 'yoo', 'hide'];

/** Unified magnitude dim of a legacy filter name. */
function sphxMag(alias, f) {
  const k = `m_${f}`;
  if (Object.prototype.hasOwnProperty.call(alias, k)) return String(alias[k]);
  if (Object.prototype.hasOwnProperty.call(alias, f)) return String(alias[f]);
  return String(f).toLowerCase();
}

/** Unified colour expression of legacy filters a - b. */
function sphxColour(alias, a, b) {
  const k = `c_${a}_${b}`;
  if (Object.prototype.hasOwnProperty.call(alias, k)) return String(alias[k]);
  return `${sphxMag(alias, a)}-${sphxMag(alias, b)}`;
}

function colourByOf(opts) {
  const c = opts && opts.color;
  if (typeof c === 'string' && c) return c;
  if (c && typeof c === 'object' && typeof c.by === 'string' && c.by) return c.by;
  return null;
}

function spherexColours(loc) {
  const q = parseSearch(loc);
  if (!SPHX_KEYS.some((k) => k in q)) return null;
  const alias = dimAlias(loc);
  const sc = scatterPanes(loc);
  const ccdP = sc.find((p) => p.id === 'ccd') || sc[0] || {id: 'ccd', options: {}};
  // The CMD keys (band, cmd) need a second scatter pane; without one they are consumed and
  // dropped. A page without layout info (no page JSON) keeps the legacy pane ids ccd / cmd.
  const cmdP = sc.length ? (sc.find((p) => p.id === 'cmd' && p !== ccdP) || sc.find((p) => p !== ccdP) || null)
    : {id: 'cmd', options: {}};
  const ccd = ccdP.id, cmd = cmdP ? cmdP.id : null;
  const out = {};
  const consumed = [];
  const use = (...ks) => { for (const k of ks) if (k in q && !consumed.includes(k)) consumed.push(k); };
  let xcol = null;
  if (q.ax && q.ay) { xcol = sphxColour(alias, q.ax, q.ay); out[`${ccd}.x`] = xcol; }
  if (q.bx && q.by) out[`${ccd}.y`] = sphxColour(alias, q.bx, q.by);
  use('ax', 'ay', 'bx', 'by');
  if (q.band && cmd) {
    out[`${cmd}.y`] = sphxMag(alias, q.band);
    if (xcol) out[`${cmd}.x`] = xcol;
  }
  use('band');
  if (cmd && (q.cmd === '1' || q.cmd === 'true')) out.view = `${cmd}:half`;
  use('cmd');
  let by = null;
  if (q.c) { by = aliasDim(alias, q.c); out.c = by; }
  use('c');
  if (q.zoom) {
    const p = q.zoom.split(',').map(num);
    if (p.length === 4 && p.every((v) => v !== undefined)) {
      out[`${ccd}.xr`] = `${fmt(p[0])},${fmt(p[1])}`;
      out[`${ccd}.yr`] = `${fmt(p[2])},${fmt(p[3])}`;
    }
  }
  use('zoom');
  // Yoo layers: the CCD's overlays option lists them; the legacy page drew none without `yoo`,
  // so a CCD with yoo_* layers always gets an explicit layer list (`<ccd>.ovl`).
  const ovls = Array.isArray(ccdP.options.overlays) ? ccdP.options.overlays.map(String) : [];
  const yooAll = ovls.filter((o) => o.startsWith('yoo_'));
  if (yooAll.length) {
    let on = [];
    const y = q.yoo;
    if (y === '1' || y === 'true') on = yooAll.slice();
    else if (y) {
      const want = y.split(',').map((s) => s.trim()).filter(Boolean).map((s) => (s.startsWith('yoo_') ? s : `yoo_${s}`));
      on = yooAll.filter((o) => want.includes(o));
    }
    const list = ovls.filter((o) => !o.startsWith('yoo_') || on.includes(o));
    out[`${ccd}.ovl`] = list.length ? list.join(',') : '-';
  }
  use('yoo');
  if (q.hide) {
    const key = by || colourByOf(ccdP.options);
    const levels = q.hide.split('|').map((s) => s.trim()).filter(Boolean);
    if (key && levels.length) out.f = encodeJSON({cats: {[key]: levels}});
  }
  use('hide');
  if (!Object.keys(out).length && !consumed.length) return null;
  return {hash: out, consumed};
}

/** name -> rule; `source` tells whether the rule reads the hash or the query string. */
const RULES = {
  'bare-name': {source: 'hash', fn: bareName},
  'kv-view': {source: 'both', fn: kvView},
  'gc-query': {source: 'search', fn: gcQuery},
  wavelength: {source: 'both', fn: wavelength},
  beat: {source: 'hash', fn: beat},
  'gallery-index': {source: 'hash', fn: galleryIndex},
  'w51-rowid': {source: 'hash', fn: w51RowId},
  specviewer: {source: 'hash', fn: specviewer},
  'spherex-colours': {source: 'search', fn: spherexColours},
};

/** Grammar name -> (loc) => canonical hash record, or null when the grammar does not apply. */
export const GRAMMARS = Object.freeze(Object.fromEntries(Object.entries(RULES).map(([k, r]) => [k, (loc) => {
  const res = r.fn(loc || {});
  return res ? Object.assign({}, res.hash, res.searchHash || {}) : null;
}])));

export const GRAMMAR_NAMES = Object.freeze(Object.keys(RULES));

/**
 * Map a legacy location onto canonical hash keys.
 *
 * Hash grammars are tried in the given order and the first that applies wins; a hash that
 * is already canonical is left alone. Search grammars (gc-query, spherex-colours, the ?speed=
 * of wavelength and kv-view) all apply and merge, also when the hash was canonical or another
 * grammar mapped it.
 * Unknown grammar names are ignored. `fromHash` is true when a grammar mapped the hash itself
 * (boot then replaces the hash instead of merging the mapping under it).
 *
 * @param {string[]} grammars
 * @param {{hash?: string, search?: string, dims?: Array, pane?: string, ds?: string,
 *          page?: object, dimAlias?: object}} loc
 * @returns {{hash: Record<string,string>, consumedSearch: string[], fromHash: boolean}}
 */
export function mapLegacy(grammars, loc = {}) {
  const names = Array.isArray(grammars) ? grammars : (grammars ? [grammars] : []);
  const h = hashText(loc);
  const hashIsCanonical = !h || isCanonicalHash(h);
  let hash = {};
  const consumed = [];
  let hashDone = hashIsCanonical;
  let fromHash = false;
  for (const name of names) {
    const rule = RULES[name];
    if (!rule) continue;
    if (rule.source === 'hash' && hashDone) continue;
    let res;
    if (rule.source === 'both' && hashDone) {
      res = rule.fn(Object.assign({}, loc, {hash: ''}));
    } else {
      res = rule.fn(loc);
    }
    if (!res) continue;
    if (rule.source === 'search') {
      hash = Object.assign({}, hash, res.hash);
    } else if (Object.keys(res.hash).length && !hashDone) {
      hash = Object.assign({}, res.hash, hash);
      hashDone = true;
      fromHash = true;
    }
    // the query-string part of a 'both' rule merges whether or not the hash was mapped
    if (res.searchHash) hash = Object.assign({}, hash, res.searchHash);
    for (const k of res.consumed) if (!consumed.includes(k)) consumed.push(k);
  }
  return {hash, consumedSearch: consumed, fromHash};
}
