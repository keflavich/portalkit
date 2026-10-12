// Canonical URL hash <-> store binding (ARCHITECTURE.md section 6.4, key table in section 6.2).
// Pure module: parseHash/formatHash/stateToHash/hashToPatch touch no DOM; bindHash uses
// `opts.win` (default globalThis.window) so it can run against a fake window under node.
//
// Canonical value formats (see docs/js.md, "Hash grammar"):
//   id=<id>                       focus.id (focus.ds = primary dataset; boot resolves focus.i)
//   sel=ids:..|bits:..|r:..       Selection.serialize() of the primary dataset (the form is
//                                 chosen by the Selection alone; see selection.js)
//   f=<b64url(JSON)>              filters:<primaryDs>  (raw JSON starting with '{' also accepted;
//                                 cats entries equal to [] are kept: "all levels on" for a
//                                 default_off column, IMPLEMENTATION_NOTES 104)
//   <pane>.x=<expr>, <pane>.y=<expr>   axes:<pane>.x / .y
//   <pane>.frame=<preset>|b64:<b64url(f32 LE F)>   frame:<pane>
//   <pane>.xr=<lo>,<hi>, <pane>.yr=<lo>,<hi>     axes:<pane>.xr / .yr (zoomed axis ranges)
//   <pane>.raster=0|1             layers:<pane>.raster (raster layers off / on)
//   <pane>.ovl=<id>,<id> | -      layers:<pane>.ovl (plot overlay layers shown; '-' none;
//                                 absent = every overlay of the pane options)
//   c=<by>[~<cmap>[~<lo>~<hi>]]   color
//   av=, rv=, dm=                 numbers
//   speed=<x>                     speed (tour / playback speed multiplier, default 1)
//   tpl=<key>[~<grid>[~<scale>[~<band,band>]]]   template
//   feat=<fam>,<fam>              features
//   spec=<b64url(JSON)>           spec without xr;   wl=<w0>,<w1>  spec.xr
//   sky=<ra>,<dec>,<fov>  base=<id>  ovl=<id>:<opacity>,<id>   sky
//                                 (ovl=- explicitly no overlays; ovl absent = layout default)
//   rung=, beat=, role=           strings
//   fac=<b64url(JSON)>            facets
//   lb=<set>,<index> | <index>    lightbox
//   view=<pane>:<half|full>,...   layout.popout
//
// Cleared values: when the page default has a key and the current state is empty (colour-by
// off, no template, no features, ...), the key is written with the CLEARED marker ('-'; 'none'
// for `c`) so a reload or Back keeps the cleared state instead of restoring the default.
//
// Beat-implied sky (narrative pages; port table msata "suppressView keeps user zoom; #beat=
// hash"): `sky.beatView` / `sky.beatOvl` name the beat whose view / overlay set the `sky` value
// holds ('' = the first beat of a page opened without `beat=`). A part implied by the current
// beat (marker === state.beat, or '' while no beat is set) is not written: `#beat=b3` stands for
// b3's view and overlays. A user pan/zoom (sky pane) drops `beatView` and a layer change drops
// `beatOvl`, so `sky=` / `ovl=` are written from then on. Reading a hash with `beat=` sets the
// markers for the parts the hash leaves out; `sky=` / `ovl=` in the hash clear them, so
// `#sky=..&beat=b3` keeps the hash's view over the beat's. On navigation the page defaults for
// absent keys are merged into that `sky` keeping the markers (the narrative re-applies the
// beat's parts). With a beat set, an explicit `sky=` / `ovl=` equal to the page default is still
// written (`beat=` alone would stand for the beat's own view / overlays).

import {HASH_DEBOUNCE_MS, HASH_MAX_CHARS} from './constants.js';
import {b64urlEncode, b64urlDecode, utf8Encode, utf8Decode, encodeHashValue} from './selection.js';

/** Stable key order for formatHash (section 6.2 table order). Pane-scoped keys sit after `f`. */
export const HASH_ORDER = Object.freeze([
  'id', 'sel', 'f', '@pane', 'c', 'av', 'rv', 'dm', 'speed', 'tpl', 'feat', 'spec', 'wl',
  'sky', 'base', 'ovl', 'rung', 'beat', 'role', 'fac', 'lb', 'view',
]);
const PANE_SUFFIX_ORDER = ['x', 'y', 'frame', 'xr', 'yr', 'raster', 'ovl'];
/** Pane-scoped hash keys `<pane>.<suffix>`. */
export const PANE_KEY_RE = /^(.+)\.(x|y|frame|xr|yr|raster|ovl)$/;

/** Store keys that never appear in the hash are ignored by bindHash. */
const SIMPLE_STORE_KEYS = new Set([
  'focus', 'color', 'av', 'rv', 'dm', 'speed', 'template', 'features', 'spec', 'sky', 'rung', 'beat',
  'role', 'facets', 'lightbox', 'layout',
]);

function safeDecode(s) {
  try {
    return decodeURIComponent(s);
  } catch (err) {
    if (err instanceof URIError) return s;
    throw err;
  }
}

/** "#a=1&b=x%20y&flag" -> {a:"1", b:"x y", flag:""}. Later duplicates win. */
export function parseHash(h) {
  const out = {};
  let s = String(h ?? '');
  if (s.startsWith('#')) s = s.slice(1);
  if (!s) return out;
  for (const part of s.split('&')) {
    if (!part) continue;
    const eq = part.indexOf('=');
    const k = safeDecode(eq < 0 ? part : part.slice(0, eq));
    const v = eq < 0 ? '' : safeDecode(part.slice(eq + 1));
    if (!k || k === '__proto__' || k === 'constructor' || k === 'prototype') continue;
    out[k] = v;
  }
  return out;
}

/**
 * encodeURIComponent, keeping ',' ':' '/' '@' '!' '*' '(' ')' '~' readable. Defined in
 * selection.js (Selection.serialize measures `sel=` forms with it) and re-exported here.
 */
export {encodeHashValue};

function orderOf(k) {
  const i = HASH_ORDER.indexOf(k);
  if (i >= 0) return [i, '', 0];
  const m = PANE_KEY_RE.exec(k);
  if (m) return [HASH_ORDER.indexOf('@pane'), m[1], PANE_SUFFIX_ORDER.indexOf(m[2])];
  return [HASH_ORDER.length, k, 0];
}

/** Record -> "a=1&b=2" (no leading '#'), stable order; null/undefined/'' values are omitted. */
export function formatHash(obj) {
  const keys = Object.keys(obj || {}).filter((k) => obj[k] !== null && obj[k] !== undefined && obj[k] !== '');
  keys.sort((a, b) => {
    const oa = orderOf(a);
    const ob = orderOf(b);
    if (oa[0] !== ob[0]) return oa[0] - ob[0];
    if (oa[1] !== ob[1]) return oa[1] < ob[1] ? -1 : 1;
    return oa[2] - ob[2];
  });
  return keys.map((k) => `${encodeHashValue(k)}=${encodeHashValue(obj[k])}`).join('&');
}

// ----------------------------------------------------------------------------------------
// value codecs
// ----------------------------------------------------------------------------------------

/** JSON -> base64url(UTF-8 JSON). */
export function encodeJSONParam(v) {
  return b64urlEncode(utf8Encode(JSON.stringify(v)));
}

/** base64url JSON, or raw JSON text starting with '{' / '['. Returns undefined when malformed. */
export function decodeJSONParam(s) {
  if (typeof s !== 'string' || !s) return undefined;
  const t = s.trim();
  try {
    if (t.startsWith('{') || t.startsWith('[')) return JSON.parse(t);
    return JSON.parse(utf8Decode(b64urlDecode(t)));
  } catch (err) {
    if (err instanceof SyntaxError) return undefined;
    throw err;
  }
}

/** Marker for "cleared relative to the page default" (see header). */
export const CLEARED = '-';

/**
 * Keys that always carry a number (never cleared) or are not cleared through the marker by
 * bindHash. `ovl` writes its own marker: stateToHash gives `ovl=-` for an explicitly empty
 * overlay list (IMPLEMENTATION_NOTES 72), and an absent overlay list means the layout default.
 */
const NO_CLEAR_KEYS = new Set(['sel', 'av', 'rv', 'dm', 'speed', 'ovl']);

/** Pane-key suffixes bindHash never writes the CLEARED marker for (see compute()). */
const NO_CLEAR_SUFFIXES = new Set(['frame', 'raster', 'ovl']);

/**
 * Number -> short decimal text. Magnitudes >= 1e-3 keep 1e-6 absolute precision (coordinates,
 * slider values); smaller magnitudes keep 7 significant digits (fluxes in Jy, template scales).
 */
function num(v) {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '';
  const r = Math.abs(v) >= 1e-3 ? Math.round(v * 1e6) / 1e6 : Number(v.toPrecision(7));
  return String(Object.is(r, -0) ? 0 : r);
}

function parseNum(s) {
  if (s === undefined || s === null || String(s).trim() === '') return undefined;
  const v = Number(s);
  return Number.isFinite(v) ? v : undefined;
}

function isEmptyValue(v) {
  if (v === null || v === undefined || v === '') return true;
  if (Array.isArray(v)) return v.length === 0;
  if (typeof v === 'object') return Object.keys(v).every((k) => isEmptyValue(v[k]));
  return false;
}

/**
 * True when a `filters:<ds>` value carries something for `f=`. Like isEmptyValue, except that a
 * `cats` entry whose value is an array counts even when the array is empty: `cats: {key: []}`
 * records "all levels on" for a column with manifest `default_off` (IMPLEMENTATION_NOTES 104).
 */
function filtersCarry(f) {
  if (!f || typeof f !== 'object' || Array.isArray(f)) return false;
  for (const k of Object.keys(f)) {
    const v = f[k];
    if (k === 'cats' && v && typeof v === 'object' && !Array.isArray(v)) {
      if (Object.keys(v).some((ck) => Array.isArray(v[ck]))) return true;
      continue;
    }
    if (!isEmptyValue(v)) return true;
  }
  return false;
}

function encodeF32(arr) {
  const f = Float32Array.from(arr);
  const b = new Uint8Array(f.length * 4);
  const dv = new DataView(b.buffer);
  for (let i = 0; i < f.length; i++) dv.setFloat32(i * 4, f[i], true);
  return b64urlEncode(b);
}

function decodeF32(s) {
  const b = b64urlDecode(s);
  if (b.length % 4) throw new SyntaxError('frame bytes not a multiple of 4');
  const dv = new DataView(b.buffer, b.byteOffset, b.byteLength);
  const out = [];
  for (let i = 0; i < b.length; i += 4) out.push(dv.getFloat32(i, true));
  return out;
}

function trimTail(parts) {
  const p = parts.slice();
  while (p.length && (p[p.length - 1] === '' || p[p.length - 1] === undefined || p[p.length - 1] === null)) p.pop();
  return p;
}

/** Store `sky` fields naming the beat that implies the view / the overlay set (see header). */
export const BEAT_VIEW = 'beatView';
export const BEAT_OVL = 'beatOvl';

/**
 * Which parts of `state.sky` are implied by the current beat and therefore stay out of the
 * hash: {view, ovl}. A marker implies its part when it equals `state.beat`, or when it is ''
 * and no beat is set.
 */
export function skyImplied(state) {
  const sky = state && state.sky;
  if (!sky || typeof sky !== 'object') return {view: false, ovl: false};
  const beat = typeof state.beat === 'string' ? state.beat : '';
  const is = (m) => typeof m === 'string' && m === beat;
  return {view: is(sky[BEAT_VIEW]), ovl: is(sky[BEAT_OVL])};
}

/** Is `key` a store key that bindHash mirrors into the hash for this primary dataset? */
export function isHashStoreKey(key, primaryDs) {
  if (SIMPLE_STORE_KEYS.has(key)) return true;
  if (key.startsWith('axes:') || key.startsWith('frame:') || key.startsWith('layers:')) return true;
  if (primaryDs && (key === `sel:${primaryDs}` || key === `filters:${primaryDs}`)) return true;
  return false;
}

/**
 * Store state -> canonical hash record (no defaults filtering, no length check).
 * @param {object} state  store snapshot (plain object)
 * @param {{primaryDs?: string, serializeSel?: () => string}} opts
 */
export function stateToHash(state, opts = {}) {
  const out = {};
  const ds = opts.primaryDs;
  const focus = state.focus;
  if (focus && focus.id !== undefined && focus.id !== null && focus.id !== '') out.id = String(focus.id);
  if (typeof opts.serializeSel === 'function') {
    const s = opts.serializeSel();
    if (s) out.sel = s;
  }
  if (ds) {
    const f = state[`filters:${ds}`];
    if (filtersCarry(f)) out.f = encodeJSONParam(f);
  }
  for (const key of Object.keys(state)) {
    if (key.startsWith('axes:')) {
      const pane = key.slice(5);
      const a = state[key];
      if (!a) continue;
      if (a.x !== undefined && a.x !== null && a.x !== '') out[`${pane}.x`] = String(a.x);
      if (a.y !== undefined && a.y !== null && a.y !== '') out[`${pane}.y`] = String(a.y);
      for (const r of ['xr', 'yr']) {
        const v = a[r];
        if (Array.isArray(v) && v.length === 2 && v.every(Number.isFinite)) out[`${pane}.${r}`] = `${num(v[0])},${num(v[1])}`;
      }
    } else if (key.startsWith('layers:')) {
      const pane = key.slice(7);
      const L = state[key];
      if (!L || typeof L !== 'object') continue;
      if (typeof L.raster === 'boolean') out[`${pane}.raster`] = L.raster ? '1' : '0';
      if (Array.isArray(L.ovl)) {
        const ids = L.ovl.filter((o) => o !== null && o !== undefined && o !== '').map(String);
        out[`${pane}.ovl`] = ids.length ? ids.join(',') : CLEARED;
      }
    } else if (key.startsWith('frame:')) {
      const pane = key.slice(6);
      const fr = state[key];
      if (!fr) continue;
      if (fr.preset) out[`${pane}.frame`] = String(fr.preset);
      else if (fr.F && fr.F.length) out[`${pane}.frame`] = 'b64:' + encodeF32(fr.F);
    }
  }
  const color = state.color;
  if (color && color.by !== null && color.by !== undefined && color.by !== '') {
    const parts = [String(color.by), color.cmap || ''];
    if (Number.isFinite(color.lo) || Number.isFinite(color.hi)) parts.push(num(color.lo), num(color.hi));
    out.c = trimTail(parts).join('~');
  }
  for (const k of ['av', 'rv', 'dm']) {
    if (typeof state[k] === 'number' && Number.isFinite(state[k])) out[k] = num(state[k]);
  }
  if (typeof state.speed === 'number' && Number.isFinite(state.speed) && state.speed > 0) out.speed = num(state.speed);
  const tpl = state.template;
  if (tpl && tpl.key) {
    const scale = tpl.scale === undefined || tpl.scale === null ? '' : (typeof tpl.scale === 'number' ? num(tpl.scale) : String(tpl.scale));
    const bands = Array.isArray(tpl.bands) ? tpl.bands.join(',') : '';
    out.tpl = trimTail([String(tpl.key), tpl.grid || '', scale, bands]).join('~');
  }
  if (Array.isArray(state.features) && state.features.length) out.feat = state.features.join(',');
  const spec = state.spec;
  if (spec && typeof spec === 'object') {
    const rest = {};
    for (const k of Object.keys(spec)) {
      if (k === 'xr') continue;
      if (!isEmptyValue(spec[k])) rest[k] = spec[k];
    }
    if (Object.keys(rest).length) out.spec = encodeJSONParam(rest);
    if (Array.isArray(spec.xr) && spec.xr.length === 2 && spec.xr.every(Number.isFinite)) {
      out.wl = `${num(spec.xr[0])},${num(spec.xr[1])}`;
    }
  }
  const sky = state.sky;
  if (sky && typeof sky === 'object') {
    const implied = skyImplied(state);
    if (!implied.view && [sky.ra, sky.dec, sky.fov].every(Number.isFinite)) {
      out.sky = `${num(sky.ra)},${num(sky.dec)},${num(sky.fov)}`;
    }
    if (sky.base) out.base = String(sky.base);
    // overlays: absent/undefined = the layout default (no `ovl`); [] = explicitly none (`ovl=-`).
    if (!implied.ovl && Array.isArray(sky.overlays)) {
      const toks = sky.overlays.filter((o) => o !== null && o !== undefined && (typeof o === 'string' ? o : o.id !== undefined && o.id !== null && o.id !== ''))
        .map((o) => {
          if (typeof o === 'string') return o;
          return Number.isFinite(o.opacity) ? `${o.id}:${num(o.opacity)}` : String(o.id);
        });
      out.ovl = toks.length ? toks.join(',') : CLEARED;
    }
  }
  for (const k of ['rung', 'beat', 'role']) {
    if (typeof state[k] === 'string' && state[k]) out[k] = state[k];
  }
  if (state.facets && !isEmptyValue(state.facets)) out.fac = encodeJSONParam(state.facets);
  const lb = state.lightbox;
  if (lb && Number.isFinite(lb.index)) out.lb = lb.set ? `${lb.set},${lb.index}` : String(lb.index);
  const lay = state.layout;
  if (lay && lay.popout) {
    const parts = Object.keys(lay.popout).filter((p) => lay.popout[p]).map((p) => `${p}:${lay.popout[p]}`);
    if (parts.length) out.view = parts.join(',');
  }
  return out;
}

/**
 * Canonical hash record -> store patch.
 * @param {object} h  parseHash output
 * @param {{primaryDs?: string, state?: object, markers?: boolean}} opts  state = current snapshot
 *   (for merges); markers false leaves the beat markers of `sky` as they are (page defaults
 *   merged under a hash, bindHash)
 * @returns {{patch: object, sel: string|undefined}}
 */
export function hashToPatch(h, opts = {}) {
  const ds = opts.primaryDs;
  const state = opts.state || {};
  const patch = {};
  const cur = (k) => (Object.prototype.hasOwnProperty.call(patch, k) ? patch[k] : state[k]);
  let sel;
  // beat-implied sky (header): which sky parts this hash gives, and the beat it names
  let viewGiven = false, ovlGiven = false, beatGiven = null;

  for (const key of Object.keys(h)) {
    const v = h[key];
    const pm = PANE_KEY_RE.exec(key);
    if (v === CLEARED) {
      if (key === 'sky') viewGiven = true;
      else if (key === 'ovl') ovlGiven = true;
      if (pm) {
        if (pm[2] === 'ovl') {
          // `<pane>.ovl=-`: explicitly no overlay layers
          patch[`layers:${pm[1]}`] = Object.assign({}, cur(`layers:${pm[1]}`) || {}, {ovl: []});
        } else if (pm[2] === 'raster') {
          const prev = Object.assign({}, cur(`layers:${pm[1]}`) || {});
          delete prev.raster;
          patch[`layers:${pm[1]}`] = prev;
        } else if (pm[2] !== 'frame') {
          const prev = Object.assign({}, cur(`axes:${pm[1]}`) || {});
          prev[pm[2]] = null;
          patch[`axes:${pm[1]}`] = prev;
        }
        continue;
      }
      const cleared = clearedValue(key, ds, cur);
      if (key === 'sel') sel = '';
      else if (cleared) Object.assign(patch, cleared);
      continue;
    }
    if (pm) {
      const pane = pm[1];
      if (pm[2] === 'frame') {
        const prev = Object.assign({}, cur(`frame:${pane}`) || {});
        delete prev.preset;
        delete prev.F;
        if (v.startsWith('b64:')) {
          try {
            prev.F = decodeF32(v.slice(4));
          } catch (err) {
            if (!(err instanceof SyntaxError)) throw err;
            continue;
          }
        } else if (v) {
          prev.preset = v;
        } else {
          continue;
        }
        if (!prev.dims) prev.dims = [];
        patch[`frame:${pane}`] = prev;
      } else if (pm[2] === 'xr' || pm[2] === 'yr') {
        const parts = v.split(',').map(parseNum);
        const prev = Object.assign({}, cur(`axes:${pane}`) || {});
        prev[pm[2]] = parts.length === 2 && parts.every((x) => x !== undefined) && parts[0] !== parts[1] ? parts : null;
        patch[`axes:${pane}`] = prev;
      } else if (pm[2] === 'raster') {
        const prev = Object.assign({}, cur(`layers:${pane}`) || {});
        if (v === '0' || v === 'off' || v === 'false') prev.raster = false;
        else if (v === '1' || v === 'on' || v === 'true') prev.raster = true;
        else continue;
        patch[`layers:${pane}`] = prev;
      } else if (pm[2] === 'ovl') {
        const prev = Object.assign({}, cur(`layers:${pane}`) || {});
        prev.ovl = v.split(',').map((t) => t.trim()).filter(Boolean);
        patch[`layers:${pane}`] = prev;
      } else {
        const prev = Object.assign({}, cur(`axes:${pane}`) || {});
        prev[pm[2]] = v;
        patch[`axes:${pane}`] = prev;
      }
      continue;
    }
    switch (key) {
      case 'id': {
        if (!v) { patch.focus = null; break; }
        const f = cur('focus');
        patch.focus = f && String(f.id) === v && (!ds || f.ds === ds) ? f : {ds: ds ?? null, i: null, id: v};
        break;
      }
      case 'sel':
        sel = v;
        break;
      case 'f': {
        if (!ds) break;
        const val = decodeJSONParam(v);
        if (val && typeof val === 'object' && !Array.isArray(val)) patch[`filters:${ds}`] = val;
        else if (v === '') patch[`filters:${ds}`] = {};
        break;
      }
      case 'c': {
        const parts = v.split('~');
        const prev = cur('color') || {};
        const by = parts[0] === '' || parts[0] === 'none' ? null : parts[0];
        const c = {by, cmap: parts[1] || prev.cmap || 'viridis'};
        const lo = parseNum(parts[2]);
        const hi = parseNum(parts[3]);
        if (lo !== undefined) c.lo = lo;
        if (hi !== undefined) c.hi = hi;
        patch.color = c;
        break;
      }
      case 'av':
      case 'rv':
      case 'dm': {
        const n = parseNum(v);
        if (n !== undefined) patch[key] = n;
        break;
      }
      case 'speed': {
        const n = parseNum(v);
        if (n !== undefined && n > 0) patch.speed = n;
        break;
      }
      case 'tpl': {
        if (!v || v === 'none') { patch.template = null; break; }
        const parts = v.split('~');
        const t = {key: parts[0], grid: parts[1] || (cur('template') || {}).grid || null, scale: 'auto'};
        if (parts[2]) t.scale = parseNum(parts[2]) ?? parts[2];
        if (parts[3]) t.bands = parts[3].split(',').filter(Boolean);
        patch.template = t;
        break;
      }
      case 'feat':
        patch.features = v ? v.split(',').filter(Boolean) : [];
        break;
      case 'spec': {
        const val = decodeJSONParam(v);
        if (val && typeof val === 'object' && !Array.isArray(val)) {
          const prev = cur('spec') || {};
          const next = Object.assign({pinned: [], variant: prev.variant ?? 'flux'}, val);
          if (prev.xr && !val.xr) next.xr = prev.xr;
          patch.spec = next;
        }
        break;
      }
      case 'wl': {
        const parts = v.split(',').map(parseNum);
        const prev = Object.assign({pinned: [], variant: 'flux'}, cur('spec') || {});
        if (parts.length === 2 && parts.every((x) => x !== undefined)) prev.xr = parts;
        else delete prev.xr;
        patch.spec = prev;
        break;
      }
      case 'sky': {
        const parts = v.split(',').map(parseNum);
        if (parts.length >= 2 && parts[0] !== undefined && parts[1] !== undefined) {
          const prev = Object.assign({}, cur('sky') || {});
          prev.ra = parts[0];
          prev.dec = parts[1];
          if (parts[2] !== undefined) prev.fov = parts[2];
          patch.sky = prev;
          viewGiven = true;
        }
        break;
      }
      case 'base': {
        const prev = Object.assign({}, cur('sky') || {});
        prev.base = v || null;
        patch.sky = prev;
        break;
      }
      case 'ovl': {
        const prev = Object.assign({}, cur('sky') || {});
        prev.overlays = v ? v.split(',').filter(Boolean).map((tok) => {
          const c = tok.lastIndexOf(':');
          if (c > 0) {
            const op = parseNum(tok.slice(c + 1));
            if (op !== undefined) return {id: tok.slice(0, c), opacity: op};
          }
          return {id: tok};
        }) : [];
        patch.sky = prev;
        ovlGiven = true;
        break;
      }
      case 'beat':
        beatGiven = v || null;
        patch[key] = v || null;
        break;
      case 'rung':
      case 'role':
        patch[key] = v || null;
        break;
      case 'fac': {
        const val = decodeJSONParam(v);
        if (val && typeof val === 'object' && !Array.isArray(val)) patch.facets = val;
        else if (v === '') patch.facets = {};
        break;
      }
      case 'lb': {
        if (!v) { patch.lightbox = null; break; }
        const c = v.lastIndexOf(',');
        const idx = parseNum(c >= 0 ? v.slice(c + 1) : v);
        if (idx === undefined) break;
        patch.lightbox = {set: c >= 0 ? v.slice(0, c) : ((cur('lightbox') || {}).set ?? null), index: idx};
        break;
      }
      case 'view': {
        const popout = {};
        for (const tok of v.split(',')) {
          const c = tok.lastIndexOf(':');
          if (c <= 0) continue;
          const mode = tok.slice(c + 1);
          if (mode === 'half' || mode === 'full') popout[tok.slice(0, c)] = mode;
        }
        patch.layout = Object.assign({}, cur('layout') || {}, {popout});
        break;
      }
      default:
        break;
    }
  }
  if (opts.markers !== false && (viewGiven || ovlGiven || beatGiven)) {
    // `sky=` / `ovl=` make their part explicit; with `beat=`, a part the hash leaves out is
    // implied by that beat (the narrative pane applies the beat's view / overlays)
    const s = Object.assign({}, cur('sky') || {});
    if (viewGiven) delete s[BEAT_VIEW];
    else if (beatGiven) s[BEAT_VIEW] = beatGiven;
    if (ovlGiven) delete s[BEAT_OVL];
    else if (beatGiven) s[BEAT_OVL] = beatGiven;
    patch.sky = s;
  }
  return {patch, sel};
}

/** Store patch for a hash key carrying the CLEARED marker (null when the key has none). */
function clearedValue(key, ds, cur) {
  switch (key) {
    case 'id': return {focus: null};
    case 'f': return ds ? {[`filters:${ds}`]: {}} : null;
    case 'c': return {color: {by: null, cmap: (cur('color') || {}).cmap || 'viridis'}};
    case 'tpl': return {template: null};
    case 'feat': return {features: []};
    case 'spec': {
      const prev = cur('spec') || {};
      const next = {pinned: [], variant: 'flux'};
      if (prev.xr) next.xr = prev.xr;
      return {spec: next};
    }
    case 'wl': {
      const prev = Object.assign({pinned: [], variant: 'flux'}, cur('spec') || {});
      delete prev.xr;
      return {spec: prev};
    }
    case 'sky': {
      const prev = Object.assign({}, cur('sky') || {});
      delete prev.ra;
      delete prev.dec;
      delete prev.fov;
      return {sky: prev};
    }
    case 'base': return {sky: Object.assign({}, cur('sky') || {}, {base: null})};
    case 'ovl': return {sky: Object.assign({}, cur('sky') || {}, {overlays: []})};
    case 'rung':
    case 'beat':
    case 'role': return {[key]: null};
    case 'fac': return {facets: {}};
    case 'lb': return {lightbox: null};
    case 'view': return {layout: Object.assign({}, cur('layout') || {}, {popout: {}})};
    default: return null;
  }
}

// ----------------------------------------------------------------------------------------
// binding
// ----------------------------------------------------------------------------------------

function makeKeyFilter(keys) {
  if (!keys || keys === '*') return () => true;
  const set = new Set(keys);
  if (set.has('*')) return () => true;
  return (k) => {
    if (set.has(k)) return true;
    const m = PANE_KEY_RE.exec(k);
    if (m && (set.has(`*.${m[2]}`) || set.has(`${m[1]}.*`))) return true;
    return false;
  };
}

function pick(obj, filter) {
  const out = {};
  for (const k of Object.keys(obj)) if (filter(k)) out[k] = obj[k];
  return out;
}

/** Structural equality of store values (object key order ignored; undefined fields = absent). */
function sameValue(a, b) {
  if (a === b) return true;
  if (a === null || b === null || typeof a !== 'object' || typeof b !== 'object') {
    return (a === null || a === undefined) && (b === null || b === undefined) ? true : Number.isNaN(a) && Number.isNaN(b);
  }
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  if (Array.isArray(a)) return a.length === b.length && a.every((v, i) => sameValue(v, b[i]));
  const ka = Object.keys(a).filter((k) => a[k] !== undefined);
  const kb = Object.keys(b).filter((k) => b[k] !== undefined);
  return ka.length === kb.length && ka.every((k) => Object.prototype.hasOwnProperty.call(b, k) && sameValue(a[k], b[k]));
}

/** Keys whose absence from the hash means "cleared" even without defaults. */
const CLEAR_ON_ABSENT = {id: 'focus', lb: 'lightbox', view: 'layout'};

/** history.state field: true when the entry's hash left out a non-empty selection. */
export const SEL_OMITTED_STATE = 'pkSelOmitted';

/** Store sources whose focus changes replace the current history entry instead of pushing. */
const QUIET_FOCUS_SOURCES = new Set(['broadcast']);

/**
 * Two-way binding between the store and location.hash.
 *   store -> hash: replaceState, debounced HASH_DEBOUNCE_MS; focus id changes (or a set with
 *                  {history:true}) use pushState immediately so Back returns to the prior source.
 *                  A focus with an index but no id yet ({ds, i, id:null}) is pending: nothing is
 *                  written until the id arrives (boot resolves it), so one step makes one entry.
 *                  Focus changes from cross-page broadcasts (source 'broadcast') replace.
 *   hash -> store: on 'hashchange' and 'popstate'; sets with source 'hash'.
 * opts:
 *   primaryDs   dataset whose sel/filters are in the hash
 *   keys        hash keys to bind (default all); '<pane>.*' and '*.x' patterns allowed
 *   selection   {serialize(): string, apply(str: string): void, count?(): number} for `sel=`
 *   defaults    store snapshot of page defaults; hash values equal to the defaults are omitted,
 *               keys absent from the hash restore the default on navigation, and values cleared
 *               relative to a default are written with the CLEARED marker
 *   applyInitial  apply the current location.hash on bind (default true). When false (the caller
 *               applied it), a store that no longer holds what the hash gives (a write made
 *               before binding) schedules a debounced write; otherwise the hash is left as is
 *   win         window-like object {location, history, addEventListener, removeEventListener,
 *               setTimeout, clearTimeout} (tests)
 *   debounceMs  default HASH_DEBOUNCE_MS
 *
 * Selections that cannot be put in the hash (too long for HASH_MAX_CHARS, or not serializable)
 * are marked in history.state[SEL_OMITTED_STATE]; navigating to such an entry keeps the live
 * selection instead of clearing it. Store event 'hash:sel-dropped' {ds, length} fires when a
 * written hash starts leaving `sel` out for exceeding HASH_MAX_CHARS (once per transition).
 * Returns an unbind function with helpers: flush(), touch(), replaceNow(), compute() -> string,
 * computeInfo() -> {str, dropped, omitted}, selDropped() -> boolean (last write),
 * setDefaults(defaults) (replace the page defaults compared against).
 */
export function bindHash(store, opts = {}) {
  const win = opts.win || (typeof window !== 'undefined' ? window : null);
  if (!win || !win.location || !win.history) return () => {};
  const primaryDs = opts.primaryDs;
  const filter = makeKeyFilter(opts.keys);
  const debounceMs = opts.debounceMs ?? HASH_DEBOUNCE_MS;
  const selection = opts.selection || null;
  const setT = (fn, ms) => (win.setTimeout ? win.setTimeout(fn, ms) : setTimeout(fn, ms));
  const clearT = (t) => (win.clearTimeout ? win.clearTimeout(t) : clearTimeout(t));
  let defaultsHash = opts.defaults ? pick(stateToHash(opts.defaults, {primaryDs}), filter) : null;
  let timer = null;
  let applying = false;
  let lastFocusId = focusId(store.get('focus'));
  let selDropped = false;

  function focusId(f) {
    return f && f.id !== undefined && f.id !== null && f.id !== '' ? String(f.id) : null;
  }

  function selCount() {
    if (!selection) return 0;
    if (typeof selection.count === 'function') return selection.count() || 0;
    return selection.serialize() ? 1 : 0;
  }

  /** Pure: the hash string for the current state, and whether `sel` had to be left out. */
  function compute() {
    let selStr = '';
    const snap = store.snapshot();
    const obj = pick(stateToHash(snap, {
      primaryDs,
      serializeSel: selection ? () => { selStr = selection.serialize() || ''; return selStr; } : undefined,
    }), filter);
    if (defaultsHash) {
      // a view implied by the beat is left out, not cleared relative to a default view
      const implied = skyImplied(snap);
      for (const k of Object.keys(defaultsHash)) {
        const pk = PANE_KEY_RE.exec(k);
        if (k in obj || NO_CLEAR_KEYS.has(k) || (pk && NO_CLEAR_SUFFIXES.has(pk[2])) || (k === 'sky' && implied.view)) continue;
        obj[k] = k === 'c' ? 'none' : CLEARED;
      }
      // with a beat set, an explicit view / overlay set equal to the page default is still
      // written: `beat=` alone would stand for the beat's own
      const beatSet = typeof snap.beat === 'string' && snap.beat !== '';
      const keepExplicit = (k) => beatSet && ((k === 'sky' && !implied.view) || (k === 'ovl' && !implied.ovl));
      for (const k of Object.keys(obj)) if (defaultsHash[k] === obj[k] && !keepExplicit(k)) delete obj[k];
    }
    let str = formatHash(obj);
    let dropped = false;
    let length = str.length;
    if (str.length > HASH_MAX_CHARS && obj.sel) {
      delete obj.sel;
      str = formatHash(obj);
      dropped = true;
    }
    const omitted = dropped || (!!selection && filter('sel') && !selStr && selCount() > 0);
    return {str, dropped, omitted, length};
  }

  function currentHash() {
    return String(win.location.hash || '').replace(/^#/, '');
  }

  function stateWith(omitted) {
    const st = win.history.state;
    const base = st && typeof st === 'object' && !Array.isArray(st) ? Object.assign({}, st) : {};
    base[SEL_OMITTED_STATE] = !!omitted;
    return base;
  }

  function markedOmitted() {
    const st = win.history.state;
    return st && typeof st === 'object' ? st[SEL_OMITTED_STATE] : undefined;
  }

  function hereUrl(str) {
    return `${win.location.pathname || ''}${win.location.search || ''}${str ? '#' + str : ''}`;
  }

  function write(push) {
    const r = compute();
    if (r.dropped && !selDropped) store.emit('hash:sel-dropped', {ds: primaryDs, length: r.length});
    selDropped = r.dropped;
    if (push && r.omitted && markedOmitted() !== true) {
      // The selection changed on the entry we are leaving; Back to it must keep the selection.
      win.history.replaceState(stateWith(true), '', hereUrl(currentHash()) || '#');
    }
    if (r.str === currentHash()) {
      if ((markedOmitted() === true) !== r.omitted) win.history.replaceState(stateWith(r.omitted), '', hereUrl(r.str) || '#');
      return;
    }
    const fn = push ? 'pushState' : 'replaceState';
    win.history[fn](stateWith(r.omitted), '', hereUrl(r.str) || '#');
    if (!r.str && win.location.hash && win.history.replaceState) {
      // Some environments keep a bare '#'; normalise.
      win.history.replaceState(stateWith(r.omitted), '', hereUrl(''));
    }
  }

  const off = store.on('*', (changed, _st, sources, info) => {
    if (applying) return;
    let relevant = false;
    for (const k of changed) if (isHashStoreKey(k, primaryDs)) { relevant = true; break; }
    if (!relevant) return;
    const f = store.get('focus');
    const fid = focusId(f);
    // Pending focus: an index without its id yet. The resolved set writes the hash.
    if (changed.has('focus') && fid === null && f && Number.isInteger(f.i) && f.i >= 0) return;
    const focusMoved = changed.has('focus') && fid !== lastFocusId;
    lastFocusId = fid;
    const quiet = sources && sources.size > 0 && Array.from(sources).every((s) => QUIET_FOCUS_SOURCES.has(s));
    if ((focusMoved && !quiet) || (info && info.history)) {
      if (timer !== null) { clearT(timer); timer = null; }
      write(true);
      return;
    }
    if (timer !== null) clearT(timer);
    timer = setT(() => { timer = null; write(false); }, debounceMs);
  }, {sync: true});

  function onNav() {
    if (timer !== null) { clearT(timer); timer = null; }
    const h = pick(parseHash(win.location.hash), filter);
    const snap = store.snapshot();
    const {patch, sel} = hashToPatch(h, {primaryDs, state: snap});
    // Absent keys: restore defaults, or clear the "clear on absent" keys.
    const absentDefaults = {};
    if (defaultsHash) {
      for (const k of Object.keys(defaultsHash)) if (!(k in h)) absentDefaults[k] = defaultsHash[k];
    }
    // the defaults are merged on top of the hash's patch (state below) without touching the beat
    // markers, so `sky` from the defaults adds the default view / base / overlays the hash leaves
    // out to the hash's own `sky` and keeps the parts the hash gives
    const fromDefaults = hashToPatch(absentDefaults, {primaryDs, state: Object.assign({}, snap, patch), markers: false}).patch;
    for (const k of Object.keys(fromDefaults)) if (!(k in patch) || k === 'sky') patch[k] = fromDefaults[k];
    // `ovl` absent (and no page default for it): the sky overlays return to the layout default,
    // i.e. the store holds no `overlays` list at all.
    if (filter('ovl') && !('ovl' in h) && !(defaultsHash && 'ovl' in defaultsHash)) {
      const sky = 'sky' in patch ? patch.sky : snap.sky;
      if (sky && typeof sky === 'object' && Object.prototype.hasOwnProperty.call(sky, 'overlays')) {
        const next = Object.assign({}, sky);
        delete next.overlays;
        patch.sky = next;
      }
    }
    for (const hk of Object.keys(CLEAR_ON_ABSENT)) {
      const sk = CLEAR_ON_ABSENT[hk];
      if (!filter(hk) || hk in h || sk in patch || (defaultsHash && hk in defaultsHash)) continue;
      if (sk === 'layout') {
        const lay = snap.layout;
        if (lay && lay.popout && Object.keys(lay.popout).length) patch.layout = Object.assign({}, lay, {popout: {}});
      } else if (snap[sk] !== null && snap[sk] !== undefined) {
        patch[sk] = null;
      }
    }
    // Pane zoom ranges and layer toggles absent from the hash (and from the defaults): back to
    // the pane's own default (no range, the option's layers).
    const absent = (hk) => filter(hk) && !(hk in h) && !(defaultsHash && hk in defaultsHash);
    for (const sk of Object.keys(snap)) {
      const isAxes = sk.startsWith('axes:');
      if (!isAxes && !sk.startsWith('layers:')) continue;
      const pane = sk.slice(isAxes ? 5 : 7);
      const base = sk in patch ? patch[sk] : snap[sk];
      if (!base || typeof base !== 'object') continue;
      let next = null;
      for (const f of isAxes ? ['xr', 'yr'] : ['raster', 'ovl']) {
        const has = isAxes ? Array.isArray(base[f]) : base[f] !== undefined;
        if (!has || !absent(`${pane}.${f}`)) continue;
        next = next || Object.assign({}, base);
        if (isAxes) next[f] = null;
        else delete next[f];
      }
      if (next) patch[sk] = next;
    }
    applying = true;
    try {
      store.set(patch, {source: 'hash'});
    } finally {
      applying = false;
    }
    lastFocusId = focusId(store.get('focus'));
    if (selection && filter('sel')) {
      let want = sel !== undefined ? sel : ((defaultsHash && defaultsHash.sel) || '');
      if (sel === undefined && !(defaultsHash && defaultsHash.sel)) {
        // `sel` absent: an empty selection, unless this entry was written while the selection
        // could not be put in the hash (then the live selection is the best available state).
        const marked = markedOmitted();
        const curOmitted = compute().omitted;
        if (marked === true || (marked === undefined && curOmitted)) want = null;
      }
      if (want !== null) {
        const cur = selection.serialize() || '';
        if (want === '' ? (cur !== '' || selCount() > 0) : want !== cur) selection.apply(want);
      }
    }
  }

  /**
   * Bound after the hash was applied (applyInitial false): true when the store no longer holds
   * what location.hash gives, i.e. a pane wrote a hash key before the binding existed (that
   * write never reached the hash; e.g. the narrative replacing an unknown `beat=` id).
   */
  function divergedFromHash() {
    const snap = store.snapshot();
    const {patch} = hashToPatch(pick(parseHash(win.location.hash), filter), {primaryDs, state: snap});
    return Object.keys(patch).some((k) => isHashStoreKey(k, primaryDs) && !sameValue(patch[k], snap[k]));
  }

  win.addEventListener('hashchange', onNav);
  win.addEventListener('popstate', onNav);
  if (opts.applyInitial !== false) onNav();
  else if (divergedFromHash()) timer = setT(() => { timer = null; write(false); }, debounceMs);

  const unbind = () => {
    off();
    if (timer !== null) { clearT(timer); timer = null; }
    win.removeEventListener('hashchange', onNav);
    win.removeEventListener('popstate', onNav);
  };
  unbind.flush = () => {
    if (timer !== null) { clearT(timer); timer = null; write(false); }
  };
  // Schedule a debounced rewrite (e.g. after selected ids arrived asynchronously).
  unbind.touch = () => {
    if (timer !== null) clearT(timer);
    timer = setT(() => { timer = null; write(false); }, debounceMs);
  };
  // Rewrite now with replaceState (e.g. to canonicalise a legacy hash after boot).
  unbind.replaceNow = () => {
    if (timer !== null) { clearT(timer); timer = null; }
    write(false);
  };
  unbind.selDropped = () => selDropped;
  // Replace the page defaults (e.g. once a manifest supplies a pane's default frame preset);
  // the next write compares against them. Nothing is written by the call itself.
  unbind.setDefaults = (defaults) => {
    defaultsHash = defaults ? pick(stateToHash(defaults, {primaryDs}), filter) : null;
  };
  unbind.compute = () => compute().str;
  unbind.computeInfo = () => {
    const r = compute();
    return {str: r.str, dropped: r.dropped, omitted: r.omitted};
  };
  return unbind;
}
