// Photometry helpers (ARCHITECTURE.md section 6.6): mag <-> Jy, flux units, nuFnu, blackbody,
// filter-key resolution and SED points of one catalog row. Pure module; the Python twins are
// portalkit/physics/sed.py and portalkit/physics/filters.py (tests/golden/physics, 1e-6 rel).

export const C_UM_S = 2.99792458e14; // speed of light, micron / s
export const LN10_OVER_2P5 = Math.LN10 / 2.5;

const H = 6.62607015e-34;
const C = 2.99792458e8;
const KB = 1.380649e-23;

/** Flux unit -> factor to Jy. */
export const FLUX_UNITS = Object.freeze({
  Jy: 1.0, jy: 1.0,
  mJy: 1e-3, mjy: 1e-3,
  uJy: 1e-6, 'µJy': 1e-6, 'μJy': 1e-6, ujy: 1e-6,
  nJy: 1e-9, njy: 1e-9,
  MJy: 1e6,
  'erg/s/cm2/Hz': 1e23, 'erg/s/cm^2/Hz': 1e23, 'erg s-1 cm-2 Hz-1': 1e23,
  'W/m2/Hz': 1e26, 'W/m^2/Hz': 1e26, 'W m-2 Hz-1': 1e26,
});

const fin = (v) => typeof v === 'number' && Number.isFinite(v);

/** A finite number or null (booleans, strings, null and NaN are missing). */
export function num(v) {
  return fin(v) ? v : null;
}

/** zp·10^(-0.4·mag); NaN when an input is missing / non-finite or zp <= 0. */
export function magToJy(mag, zpJy) {
  const m = mag === null || mag === undefined ? NaN : +mag;
  const z = zpJy === null || zpJy === undefined ? NaN : +zpJy;
  if (!Number.isFinite(m) || !Number.isFinite(z) || !(z > 0)) return NaN;
  return z * Math.pow(10, -0.4 * m);
}

/** -2.5·log10(jy/zp); NaN for jy <= 0, zp <= 0 or non-finite inputs. */
export function jyToMag(jy, zpJy) {
  const f = jy === null || jy === undefined ? NaN : +jy;
  const z = zpJy === null || zpJy === undefined ? NaN : +zpJy;
  if (!Number.isFinite(f) || !Number.isFinite(z) || !(f > 0) || !(z > 0)) return NaN;
  return -2.5 * Math.log10(f / z);
}

/** First-order flux error of a magnitude error: |fnu|·ln10/2.5·emag (NaN for emag < 0). */
export function magErrToJy(fnuJy, emag) {
  const f = fnuJy === null || fnuJy === undefined ? NaN : +fnuJy;
  const e = emag === null || emag === undefined ? NaN : +emag;
  const out = Math.abs(f) * LN10_OVER_2P5 * e;
  return Number.isFinite(out) && e >= 0 ? out : NaN;
}

/** Factor converting a flux in `unit` to Jy (null/'' = Jy). Unknown units throw. */
export function fluxUnitFactor(unit) {
  if (unit === null || unit === undefined || unit === '') return 1.0;
  const u = String(unit).trim();
  if (Object.prototype.hasOwnProperty.call(FLUX_UNITS, u)) return FLUX_UNITS[u];
  throw new RangeError(`unknown flux unit ${JSON.stringify(unit)}`);
}

/** nu·F_nu in W m^-2 (unit 'W/m2') or erg s^-1 cm^-2 ('erg/s/cm2'); NaN for wl <= 0. */
export function nufnu(wlUm, fnuJy, unit = 'W/m2') {
  const w = wlUm === null || wlUm === undefined ? NaN : +wlUm;
  const f = fnuJy === null || fnuJy === undefined ? NaN : +fnuJy;
  let out = (f * 1e-26 * C_UM_S) / w;
  if (unit === 'W/m2' || unit === 'W/m^2' || unit === 'W m-2') {
    // as is
  } else if (unit === 'erg/s/cm2' || unit === 'erg/s/cm^2' || unit === 'erg s-1 cm-2') {
    out *= 1e3;
  } else {
    throw new RangeError(`unknown nuFnu unit ${JSON.stringify(unit)} (use 'W/m2' or 'erg/s/cm2')`);
  }
  return Number.isFinite(w) && w > 0 ? out : NaN;
}

/** Planck B_nu in W m^-2 Hz^-1 sr^-1 at wlUm (micron) for tempK (K); NaN for wl or T <= 0. */
export function blackbodyFnu(wlUm, tempK) {
  const w = +wlUm, t = +tempK;
  if (!(Number.isFinite(w) && w > 0 && t > 0)) return NaN;
  const nu = C / (w * 1e-6);
  const x = (H * nu) / (KB * t);
  return (2 * H * nu ** 3) / C ** 2 / Math.expm1(x);
}

/**
 * Linear interpolation of (xp, fp) at x (xp increasing), NaN outside [xp[0], xp[-1]];
 * null entries of fp are NaN. Follows numpy.interp.
 */
export function interpLinear(x, xp, fp) {
  const n = xp ? xp.length : 0;
  const F = (i) => (fp[i] === null || fp[i] === undefined ? NaN : +fp[i]);
  if (!n || !(x === x)) return NaN;
  if (x < xp[0] || x > xp[n - 1]) return NaN;
  if (x === xp[n - 1]) return F(n - 1);
  let lo = 0, hi = n - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (xp[mid] <= x) lo = mid;
    else hi = mid;
  }
  const j = lo;
  if (xp[j] === x) return F(j);
  const f0 = F(j), f1 = F(j + 1);
  const slope = (f1 - f0) / (xp[j + 1] - xp[j]);
  let r = slope * (x - xp[j]) + f0;
  if (!(r === r)) {
    r = slope * (x - xp[j + 1]) + f1;
    if (!(r === r) && f0 === f1) r = f0;
  }
  return r;
}

// ------------------------------------------------------------------ filter keys

/** 'JWST/NIRCam.F410M' -> 'F410M'; '2MASS/2MASS.Ks' -> 'Ks'. */
export function shortName(key) {
  const s = String(key);
  const a = s.slice(s.lastIndexOf('/') + 1);
  return a.slice(a.lastIndexOf('.') + 1);
}

/**
 * Map a name (column key, expression key, short filter name) to one of `candidates`
 * (array or object of filter entries): colmap[name]; exact; unique case-insensitive; unique
 * alias; then short-name probes lower(name), the part after its last '_' and
 * shortName(lower(name)). For each probe the unique candidate whose shortName matches
 * case-insensitively wins; when several match and `candidates` is an object of entries, the
 * unique one whose `label` equals the probe wins. Else null.
 * (Python twin: physics.filters.resolve_key; goldens in resolve_key.json.)
 */
export function resolveKey(name, candidates, colmap = null, aliases = null) {
  const isMap = !Array.isArray(candidates);
  const cands = isMap ? Object.keys(candidates || {}) : candidates;
  const labels = new Map();
  if (isMap) {
    for (const c of cands) {
      const e = candidates[c];
      if (e && typeof e === 'object' && typeof e.label === 'string') labels.set(c, e.label.toLowerCase());
    }
  }
  const cset = new Set(cands);
  if (colmap && Object.prototype.hasOwnProperty.call(colmap, name) && cset.has(colmap[name])) return colmap[name];
  if (cset.has(name)) return name;
  const low = String(name).toLowerCase();
  const ci = cands.filter((c) => c.toLowerCase() === low);
  if (ci.length === 1) return ci[0];
  if (aliases) {
    const al = cands.filter((c) => (aliases[c] || []).some((a) => String(a).toLowerCase() === low));
    if (al.length === 1) return al[0];
  }
  const probes = [];
  for (const p of [low, low.slice(low.lastIndexOf('_') + 1), shortName(low)]) {
    if (p && !probes.includes(p)) probes.push(p);
  }
  for (const probe of probes) {
    const sn = cands.filter((c) => shortName(c).toLowerCase() === probe);
    if (sn.length === 1) return sn[0];
    if (sn.length > 1 && labels.size) {
      const lab = sn.filter((c) => labels.get(c) === probe);
      if (lab.length === 1) return lab[0];
    }
  }
  return null;
}

/** The `filters` object of a pk-filters/1 document (or the object itself). */
export function filtersMap(filters) {
  if (filters && typeof filters === 'object' && filters.filters && typeof filters.filters === 'object') return filters.filters;
  return filters || {};
}

// ------------------------------------------------------------------ SED points

const LIM_STR = {u: 'ul', l: 'll', U: 'ul', L: 'll', 1: 'ul', 2: 'll'};

function limCode(v) {
  if (typeof v === 'number') return v === 1 ? 'ul' : v === 2 ? 'll' : 'det';
  const k = String(v).trim();
  return Object.prototype.hasOwnProperty.call(LIM_STR, k) ? LIM_STR[k] : 'det';
}

function limitKind(row, col) {
  const key = col.key;
  for (const name of ['_lim', '_limits']) {
    const lim = row[name];
    if (lim && typeof lim === 'object' && Object.prototype.hasOwnProperty.call(lim, key)) {
      const v = lim[key];
      if (typeof v === 'number' && Number.isInteger(v)) return limCode(v);
      if (typeof v === 'string') return limCode(v);
    }
  }
  const sib = col.limit;
  if (sib) {
    const v = row[sib];
    if (typeof v === 'string') return limCode(v);
    if (typeof v === 'number' && Number.isInteger(v)) return limCode(v);
  }
  return 'det';
}

/**
 * SED points of one row: one point per mag/flux column with a `filter` known to `filters` and a
 * finite value. mag -> zp·10^(-0.4 m), err = fnu·ln10/2.5·e (null if e missing or < 0);
 * flux -> value·unitFactor, err = |e|·factor; non-positive flux skipped. kind from
 * row._lim / row._limits [key] (1 'ul', 2 'll') or the column's `limit` sibling ('u'/'l');
 * limits have err null. Sorted by wl (stable). Returns [{wl, fnu, err, kind, key, filter}].
 */
export function sedPoints(row, filters, cols) {
  const fl = filtersMap(filters);
  const pts = [];
  if (!row) return pts;
  for (const c of cols || []) {
    const role = c.role;
    if (role !== 'mag' && role !== 'flux') continue;
    const fk = c.filter;
    if (!fk || !Object.prototype.hasOwnProperty.call(fl, fk)) continue;
    const f = fl[fk];
    const wl = num(f.wl_um);
    if (wl === null || wl <= 0) continue;
    const v = num(row[c.key]);
    if (v === null) continue;
    const kind = limitKind(row, c);
    const ev = c.err ? num(row[c.err]) : null;
    let fnu, err;
    if (role === 'mag') {
      const zp = num(f.zp_jy);
      if (zp === null || zp <= 0) continue;
      fnu = zp * Math.pow(10, -0.4 * v);
      if (!Number.isFinite(fnu) || fnu <= 0) continue;
      err = ev !== null && ev >= 0 ? fnu * LN10_OVER_2P5 * ev : null;
    } else {
      const fac = fluxUnitFactor(c.unit);
      fnu = v * fac;
      if (!(fnu > 0) || !Number.isFinite(fnu)) continue;
      err = ev !== null ? Math.abs(ev) * fac : null;
    }
    if (kind !== 'det') err = null;
    pts.push({wl, fnu, err, kind, key: c.key, filter: fk});
  }
  pts.sort((a, b) => a.wl - b.wl);
  return pts;
}
