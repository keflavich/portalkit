// Source lookup (ARCHITECTURE.md section 6.6 DataClient.find): exact id, alias, prefix and
// substring matches (case-insensitive) and "ra dec [r]" cones in sexagesimal or decimal form.
// Pure module. Ids are `str` payload chunks (codec.decodeStr) searched at byte level so that
// millions of ids are never materialised as JS strings.

import {cone, nearest} from './sky.js';

const enc = new TextEncoder();
const dec = new TextDecoder('utf-8');

/** Ids of a catalog: chunks [{start, str}] (str from decodeStr) plus an alias map. */
export class IdIndex {
  constructor(chunks, aliases = null) {
    this.chunks = chunks.slice().sort((a, b) => a.start - b.start);
    this.n = this.chunks.reduce((s, c) => Math.max(s, c.start + c.str.n), 0);
    this.aliases = aliases instanceof Map ? aliases : new Map(Object.entries(aliases || {}));
  }

  chunkOf(i) {
    for (const c of this.chunks) if (i >= c.start && i < c.start + c.str.n) return c;
    return null;
  }

  /** Id of stored row i ('' when out of range). */
  get(i) {
    const c = this.chunkOf(i);
    return c ? c.str.get(i - c.start) : '';
  }

  /** Rows whose id equals `text` exactly (byte comparison). */
  exact(text) {
    const q = enc.encode(String(text));
    const out = [];
    for (const c of this.chunks) {
      const {offsets, blob, n} = c.str;
      outer: for (let i = 0; i < n; i++) {
        const a = offsets[i];
        if (offsets[i + 1] - a !== q.length) continue;
        for (let k = 0; k < q.length; k++) if (blob[a + k] !== q[k]) continue outer;
        out.push(c.start + i);
      }
    }
    return out;
  }

  /** Row of an alias (exact), or -1. */
  alias(text) {
    const r = this.aliases.get(String(text));
    return Number.isInteger(r) ? r : -1;
  }

  /**
   * Case-insensitive matches of `text`: {exactCI, prefix, substr} row lists (ascending) for ids,
   * plus alias rows {aliasExactCI, aliasSubstr}. Stops collecting a list at `limit` entries.
   */
  search(text, limit = 50) {
    const qs = String(text).toLowerCase();
    const res = {exactCI: [], prefix: [], substr: []};
    const ascii = /^[\x00-\x7f]*$/.test(qs);
    if (!qs.length) return res;
    const full = () => res.exactCI.length >= limit && res.prefix.length >= limit && res.substr.length >= limit;
    if (ascii) {
      const q = enc.encode(qs);
      const m = q.length;
      for (const c of this.chunks) {
        const {offsets, blob, n} = c.str;
        for (let i = 0; i < n && !full(); i++) {
          const a = offsets[i], len = offsets[i + 1] - a;
          if (len < m) continue;
          let pos = -1;
          for (let s = 0; s + m <= len; s++) {
            let k = 0;
            for (; k < m; k++) {
              let b = blob[a + s + k];
              if (b >= 65 && b <= 90) b += 32;
              if (b !== q[k]) break;
            }
            if (k === m) {
              pos = s;
              break;
            }
          }
          if (pos < 0) continue;
          const row = c.start + i;
          if (pos === 0 && len === m) {
            if (res.exactCI.length < limit) res.exactCI.push(row);
          } else if (pos === 0) {
            if (res.prefix.length < limit) res.prefix.push(row);
          } else if (res.substr.length < limit) res.substr.push(row);
        }
      }
    } else {
      for (const c of this.chunks) {
        const {offsets, blob, n} = c.str;
        for (let i = 0; i < n && !full(); i++) {
          const s = dec.decode(blob.subarray(offsets[i], offsets[i + 1])).toLowerCase();
          const pos = s.indexOf(qs);
          if (pos < 0) continue;
          const row = c.start + i;
          if (pos === 0 && s.length === qs.length) {
            if (res.exactCI.length < limit) res.exactCI.push(row);
          } else if (pos === 0) {
            if (res.prefix.length < limit) res.prefix.push(row);
          } else if (res.substr.length < limit) res.substr.push(row);
        }
      }
    }
    return res;
  }

  /** Per-row 0/1 mask: id or any alias contains `text` (case-insensitive). */
  matchMask(text) {
    const n = this.n;
    const mask = new Uint8Array(n);
    const r = this.search(text, Infinity);
    for (const l of [r.exactCI, r.prefix, r.substr]) for (const i of l) mask[i] = 1;
    const qs = String(text).toLowerCase();
    for (const [a, i] of this.aliases) if (i >= 0 && i < n && a.toLowerCase().includes(qs)) mask[i] = 1;
    return mask;
  }

  /** Stored rows for a list of ids (exact id first, then alias); -1 when unknown. */
  rowsOf(ids) {
    const want = new Map();
    ids.forEach((id, k) => {
      const s = String(id);
      if (!want.has(s)) want.set(s, []);
      want.get(s).push(k);
    });
    const out = new Int32Array(ids.length).fill(-1);
    // one pass over the ids with a hash lookup per row (bounded by the request size)
    const qbytes = new Map();
    for (const s of want.keys()) qbytes.set(s, enc.encode(s));
    const byLen = new Map();
    for (const [s, b] of qbytes) {
      if (!byLen.has(b.length)) byLen.set(b.length, []);
      byLen.get(b.length).push(s);
    }
    for (const c of this.chunks) {
      const {offsets, blob, n} = c.str;
      for (let i = 0; i < n; i++) {
        const a = offsets[i], len = offsets[i + 1] - a;
        const cands = byLen.get(len);
        if (!cands) continue;
        for (const s of cands) {
          const b = qbytes.get(s);
          let k = 0;
          for (; k < len; k++) if (blob[a + k] !== b[k]) break;
          if (k === len) {
            for (const pos of want.get(s)) if (out[pos] < 0) out[pos] = c.start + i;
          }
        }
      }
    }
    ids.forEach((id, k) => {
      if (out[k] < 0) out[k] = this.alias(String(id));
    });
    return out;
  }
}

// ------------------------------------------------------------------ coordinates

const RADIUS_UNITS = {
  '': 1, '"': 1, '″': 1, "''": 1, as: 1, arcsec: 1, s: 1,
  "'": 60, '′': 60, am: 60, arcmin: 60, m: 60,
  '°': 3600, d: 3600, deg: 3600,
};

function radiusArcsec(tok) {
  const m = /^(\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\s*(arcsec|arcmin|deg|as|am|''|["″'′°dms])?$/.exec(tok.trim());
  if (!m) return null;
  const v = Number(tok.trim().replace(/[^\d.eE+-].*$/, ''));
  const u = RADIUS_UNITS[m[2] || ''];
  if (!Number.isFinite(v) || u === undefined) return null;
  return v * u;
}

function validRaDec(ra, dec) {
  return Number.isFinite(ra) && Number.isFinite(dec) && ra >= 0 && ra < 360 && dec >= -90 && dec <= 90;
}

/**
 * Parse "ra dec [r]" where ra/dec are decimal degrees or sexagesimal (hh:mm:ss.s ±dd:mm:ss.s,
 * "hh mm ss ±dd mm ss", or with h/m/s and d/m/s letters), separated by spaces and/or a comma.
 * The optional radius defaults to arcsec and accepts ", ', °, arcsec, arcmin, deg suffixes.
 * Returns {ra, dec, r: arcsec | null} or null.
 */
export function parseCoords(text) {
  if (typeof text !== 'string') return null;
  let s = text.trim().replace(/−/g, '-');
  if (!s) return null;
  // decimal: two numbers (+ optional radius)
  const num = '([+-]?(?:\\d+(?:\\.\\d*)?|\\.\\d+))';
  const decRe = new RegExp(`^${num}\\s*(?:,\\s*|\\s+)${num}(?:\\s*(?:,\\s*|\\s+)(\\S+(?:\\s*(?:arcsec|arcmin|deg))?))?$`);
  let m = decRe.exec(s);
  if (m && !/[:hdms]/i.test(m[1] + m[2])) {
    const ra = Number(m[1]), d = Number(m[2]);
    let r = null;
    if (m[3] !== undefined) {
      r = radiusArcsec(m[3]);
      if (r === null) m = null;
    }
    if (m && validRaDec(ra, d)) return {ra, dec: d, r};
    if (m) return null;
  }
  // sexagesimal: split off an optional radius first
  let radius = null;
  const rm = /(?:,\s*|\s+)((?:\d+(?:\.\d*)?|\.\d+)\s*(?:arcsec|arcmin|deg|as|am|''|["″'′°])?)$/.exec(s);
  const body = (str) => str.replace(/[hH]|[dD°]|[mM′']|[sS″"]|:/g, ' ').replace(/,/g, ' ').trim().split(/\s+/);
  let toks = body(s);
  if (toks.length === 7 && rm) {
    radius = radiusArcsec(rm[1]);
    if (radius === null) return null;
    toks = body(s.slice(0, rm.index));
  }
  if (toks.length !== 6) return null;
  if (!toks.every((t) => /^[+-]?\d+(?:\.\d*)?$/.test(t))) return null;
  const [h, mi, se] = toks.slice(0, 3).map(Number);
  const sign = toks[3].startsWith('-') ? -1 : 1;
  const [dd, dm, ds] = [Math.abs(Number(toks[3])), Number(toks[4]), Number(toks[5])];
  if (toks[0].startsWith('-') || toks[1].startsWith('-') || toks[2].startsWith('-')) return null;
  if (!(h < 24 && mi < 60 && se < 60 && dm < 60 && ds < 60)) return null;
  const ra = 15 * (h + mi / 60 + se / 3600);
  const d = sign * (dd + dm / 60 + ds / 3600);
  if (!validRaDec(ra, d)) return null;
  return {ra, dec: d, r: radius};
}

/**
 * find(): ordered unique stored rows for `text`.
 * Order: exact id, alias, case-insensitive exact id, id prefix, id substring, alias substring.
 * When nothing matches by id and `text` parses as coordinates, returns the rows inside the cone
 * (radius given) sorted by separation, or the `limit` nearest rows (no radius).
 * `pos` is {ra, dec, index?} (null when the catalog has no positions).
 */
export function findRows(text, ids, pos, {limit = 50} = {}) {
  const t = String(text ?? '').trim();
  const L = Math.max(0, limit | 0);
  if (!t || !L) return [];
  const out = [];
  const seen = new Set();
  const add = (i) => {
    if (out.length < L && i >= 0 && !seen.has(i)) {
      seen.add(i);
      out.push(i);
    }
  };
  if (ids) {
    for (const i of ids.exact(t)) add(i);
    add(ids.alias(t));
    const r = ids.search(t, L);
    for (const l of [r.exactCI, r.prefix, r.substr]) for (const i of l) add(i);
    if (out.length < L) {
      const tl = t.toLowerCase();
      const al = [];
      for (const [a, i] of ids.aliases) if (a.toLowerCase().includes(tl)) al.push(i);
      al.sort((a, b) => a - b);
      for (const i of al) add(i);
    }
  }
  if (out.length) return out;
  const c = pos ? parseCoords(t) : null;
  if (!c) return out;
  const hits = c.r !== null
    ? cone(pos.ra, pos.dec, c.ra, c.dec, c.r, {limit: L, index: pos.index || null})
    : nearest(pos.ra, pos.dec, c.ra, c.dec, L);
  for (const h of hits) add(h.i);
  return out;
}
