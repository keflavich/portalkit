// Visibility filters (store key `filters:<ds>`, ARCHITECTURE.md section 6.2):
//   {cats:{key:[offLevels]}, ranges:{expr:[lo,hi]}, flags:{key:0|1},
//    snr:{min, scope:"projection"|"all", keys?}, text?:string, ids?:string[]}
// Semantics:
//   cats    a row whose level is listed as off is hidden; rows with a missing level stay visible
//   ranges  lo <= v <= hi (null / missing bound = open); rows with a missing value are hidden
//   flags   the flag bit must equal the given 0|1
//   snr     S/N = 1.0857/err for mag bands, value/err for flux bands; a row is hidden when any
//           band in scope has S/N < min (bands with a missing value or error are ignored).
//           scope "projection" uses snr.keys (the bands of the active projection) when given,
//           otherwise every band with an error column
//   text    case-insensitive substring of the id or an alias
//   ids     keep only these ids (exact id or alias)
// Pure module: the caller provides decoded columns through `src`.

const MAG_SNR = 2.5 / Math.LN10; // 1.0857...

function hasEntries(o) {
  return !!o && typeof o === 'object' && Object.keys(o).length > 0;
}

/** True when `filters` restricts anything. */
export function isActive(f) {
  if (!f || typeof f !== 'object') return false;
  if (f.cats && Object.values(f.cats).some((v) => Array.isArray(v) && v.length)) return true;
  if (hasEntries(f.ranges) && Object.values(f.ranges).some((r) => Array.isArray(r) && (isBound(r[0]) || isBound(r[1])))) return true;
  if (hasEntries(f.flags)) return true;
  if (f.snr && Number.isFinite(+f.snr.min) && +f.snr.min > 0) return true;
  if (typeof f.text === 'string' && f.text.trim()) return true;
  if (Array.isArray(f.ids)) return true;
  return false;
}

function isBound(v) {
  return v !== null && v !== undefined && Number.isFinite(+v);
}

/** Bands used by the S/N filter: [{key, err, kind:'mag'|'flux'}]. */
export function snrBands(f, cols) {
  if (!f || !f.snr) return [];
  const byKey = new Map(cols.map((c) => [c.key, c]));
  let keys;
  if (f.snr.scope === 'projection' && Array.isArray(f.snr.keys) && f.snr.keys.length) keys = f.snr.keys;
  else keys = cols.filter((c) => c.err).map((c) => c.key);
  const out = [];
  for (const k of keys) {
    const c = byKey.get(k);
    if (!c || !c.err || !byKey.has(c.err)) continue;
    const kind = c.role === 'mag' || c.unit === 'mag' ? 'mag' : 'flux';
    out.push({key: k, err: c.err, kind});
  }
  return out;
}

/**
 * Stored columns and expressions the filters need: {cols: string[], exprs: string[], ids: bool}.
 * `cols` is the manifest cols[] list.
 */
export function filterNeeds(f, cols) {
  const need = new Set();
  const exprs = [];
  if (!f) return {cols: [], exprs, ids: false};
  for (const k of Object.keys(f.cats || {})) need.add(k);
  for (const k of Object.keys(f.flags || {})) need.add(k);
  for (const [e, r] of Object.entries(f.ranges || {})) if (Array.isArray(r) && (isBound(r[0]) || isBound(r[1]))) exprs.push(e);
  if (f.snr && +f.snr.min > 0) for (const b of snrBands(f, cols)) {
    need.add(b.key);
    need.add(b.err);
  }
  const ids = (typeof f.text === 'string' && !!f.text.trim()) || Array.isArray(f.ids);
  return {cols: Array.from(need), exprs, ids};
}

/**
 * Apply filters. `src`:
 *   n, cols (manifest cols[]),
 *   col(key) -> {codes?, values, levels?, missing?, bits?} (decoded, stored order),
 *   expr(text) -> Float32Array (NaN missing),
 *   textMatch(text) -> Uint8Array | null, idRows(ids) -> Iterable<number>
 * Returns {vis: Uint8Array (1 visible), count, active}.
 */
export function applyFilters(f, src) {
  const n = src.n;
  const vis = new Uint8Array(n).fill(1);
  if (!isActive(f)) return {vis, count: n, active: false};

  for (const [key, off] of Object.entries(f.cats || {})) {
    if (!Array.isArray(off) || !off.length) continue;
    const c = src.col(key);
    if (!c) throw new Error(`filter on unknown category column ${key}`);
    const levels = c.levels || [];
    const offIdx = new Set();
    for (const lv of off) {
      const k = typeof lv === 'number' && !levels.includes(lv) ? lv : levels.indexOf(String(lv));
      if (k >= 0) offIdx.add(k);
    }
    if (!offIdx.size) continue;
    const lut = new Uint8Array(65536);
    for (const k of offIdx) if (k < 65536) lut[k] = 1;
    const codes = c.codes;
    for (let i = 0; i < n; i++) if (lut[codes[i]] && codes[i] !== c.missing) vis[i] = 0;
  }

  for (const [e, r] of Object.entries(f.ranges || {})) {
    if (!Array.isArray(r)) continue;
    const lo = isBound(r[0]) ? +r[0] : -Infinity;
    const hi = isBound(r[1]) ? +r[1] : Infinity;
    if (lo === -Infinity && hi === Infinity) continue;
    const v = src.expr(e);
    for (let i = 0; i < n; i++) {
      const a = v[i];
      if (!(a >= lo && a <= hi)) vis[i] = 0;
    }
  }

  for (const [key, want] of Object.entries(f.flags || {})) {
    const c = src.col(key);
    if (!c) throw new Error(`filter on unknown flag column ${key}`);
    const w = want ? 1 : 0;
    const b = c.bits || c.values;
    for (let i = 0; i < n; i++) if ((b[i] ? 1 : 0) !== w) vis[i] = 0;
  }

  if (f.snr && +f.snr.min > 0) {
    const min = +f.snr.min;
    for (const band of snrBands(f, src.cols)) {
      const v = src.col(band.key).values;
      const e = src.col(band.err).values;
      for (let i = 0; i < n; i++) {
        const a = v[i], s = e[i];
        if (!Number.isFinite(a) || !Number.isFinite(s) || !(s > 0)) continue;
        const snr = band.kind === 'mag' ? MAG_SNR / s : a / s;
        if (snr < min) vis[i] = 0;
      }
    }
  }

  if (typeof f.text === 'string' && f.text.trim()) {
    const m = src.textMatch(f.text.trim());
    for (let i = 0; i < n; i++) if (!m[i]) vis[i] = 0;
  }

  if (Array.isArray(f.ids)) {
    const keep = new Uint8Array(n);
    for (const i of src.idRows(f.ids)) if (i >= 0 && i < n) keep[i] = 1;
    for (let i = 0; i < n; i++) if (!keep[i]) vis[i] = 0;
  }

  let count = 0;
  for (let i = 0; i < n; i++) count += vis[i];
  return {vis, count, active: true};
}
