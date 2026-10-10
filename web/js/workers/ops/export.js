// Selection export (ARCHITECTURE.md section 6.6 exportCSV; WP05 export formats): CSV, JSON, a
// plain id list and DS9 region files. Pure module.
// Quantized values are written with the decimals their step supports (core/fmt.js stepDigits),
// so a CSV never implies more precision than the bundle stores; exact row chunks are written
// in full precision.

import {stepDigits, fmtNum} from '../../core/fmt.js';

const LINEAR_Q = new Set(['u16q', 'u8q']);
const LOG_Q = new Set(['u16log', 'u8log']);

/** RFC 4180 cell escaping: quote when the cell holds a comma, quote, CR or LF. */
export function csvEscape(v) {
  const s = v === null || v === undefined ? '' : String(v);
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

/** Shortest decimal that reads back as the same float32 value. */
export function f32String(v) {
  if (!Number.isFinite(v)) return '';
  const f = Math.fround(v);
  for (let p = 6; p <= 9; p++) {
    const s = f.toPrecision(p);
    if (Math.fround(Number(s)) === f) return String(Number(s));
  }
  return String(f);
}

/**
 * Text of one value for export. meta: column meta (enc, step, levels, ...).
 * Missing (null/NaN) -> ''. Infinite values -> 'inf' / '-inf'.
 */
export function formatValue(v, meta = {}, {exact = false} = {}) {
  if (v === null || v === undefined) return '';
  if (typeof v === 'string') return v;
  if (typeof v === 'boolean') return v ? '1' : '0';
  if (typeof v === 'bigint') return v.toString();
  if (typeof v !== 'number') return String(v);
  if (Number.isNaN(v)) return '';
  if (v === Infinity) return 'inf';
  if (v === -Infinity) return '-inf';
  const enc = meta && meta.enc;
  if (exact) return String(v);
  if (LINEAR_Q.has(enc) && +meta.step > 0) {
    const d = stepDigits(+meta.step);
    const s = fmtNum(v, d);
    return s === '-0' ? '0' : s;
  }
  if (LOG_Q.has(enc) && +meta.step > 0) {
    // log step s (dex) -> relative precision ~ s·ln10; digits for that relative precision
    const rel = +meta.step * Math.LN10;
    const sig = Math.max(2, Math.min(15, Math.ceil(-Math.log10(rel) - 1e-9) + 1));
    return String(Number(v.toPrecision(sig)));
  }
  if (enc === 'f32') return f32String(v);
  return String(v);
}

/**
 * CSV text. header: column names; rows: iterable of arrays of already formatted cells (or raw
 * values, written with String()). `comments`: lines written first, prefixed with "# ".
 */
export function toCSV(header, rows, {comments = []} = {}) {
  const out = [];
  for (const c of comments) out.push(`# ${String(c).replace(/[\r\n]+/g, ' ')}`);
  out.push(header.map(csvEscape).join(','));
  for (const r of rows) out.push(r.map(csvEscape).join(','));
  return out.join('\n') + '\n';
}

/** Parse RFC 4180 CSV (comment lines starting with '#' skipped) into arrays of strings. */
export function parseCSV(text) {
  const rows = [];
  let row = [], cell = '', q = false, i = 0, atStart = true;
  const s = String(text);
  while (i < s.length) {
    const ch = s[i];
    if (q) {
      if (ch === '"') {
        if (s[i + 1] === '"') {
          cell += '"';
          i += 2;
          continue;
        }
        q = false;
        i++;
        continue;
      }
      cell += ch;
      i++;
      continue;
    }
    if (atStart && ch === '#') {
      while (i < s.length && s[i] !== '\n') i++;
      i++;
      continue;
    }
    atStart = false;
    if (ch === '"') {
      q = true;
      i++;
    } else if (ch === ',') {
      row.push(cell);
      cell = '';
      i++;
    } else if (ch === '\r' || ch === '\n') {
      row.push(cell);
      rows.push(row);
      row = [];
      cell = '';
      i += ch === '\r' && s[i + 1] === '\n' ? 2 : 1;
      atStart = true;
    } else {
      cell += ch;
      i++;
    }
  }
  if (cell !== '' || row.length) {
    row.push(cell);
    rows.push(row);
  }
  return rows;
}

/**
 * JSON text: an array of objects keyed by header. Cells that look numeric in `numeric` columns
 * are written as numbers; '' becomes null.
 */
export function toJSON(header, rows, {numeric = null} = {}) {
  const num = numeric ? new Set(numeric) : null;
  const out = [];
  for (const r of rows) {
    const o = {};
    header.forEach((h, k) => {
      const c = r[k];
      if (c === '' || c === null || c === undefined) o[h] = null;
      else if (num && num.has(h) && typeof c === 'string' && Number.isFinite(Number(c))) o[h] = Number(c);
      else if (typeof c === 'number' && !Number.isFinite(c)) o[h] = null;
      else o[h] = c;
    });
    out.push(o);
  }
  return JSON.stringify(out);
}

/** One id per line. */
export function toIdList(ids) {
  const out = [];
  for (const id of ids) out.push(String(id).replace(/[\r\n]+/g, ' '));
  return out.length ? out.join('\n') + '\n' : '';
}

function ds9Text(s) {
  return String(s).replace(/[{}\r\n]/g, ' ');
}

/**
 * DS9 region file (fk5, circles of radius r arcsec). items: [{id, ra, dec}]; rows with a
 * non-finite position are skipped.
 */
export function toDS9(items, {r = 1, color = 'green'} = {}) {
  const rad = Number.isFinite(+r) && +r > 0 ? +r : 1;
  const out = ['# Region file format: DS9 version 4.1', `global color=${color}`, 'fk5'];
  for (const it of items) {
    const ra = +it.ra, dec = +it.dec;
    if (!Number.isFinite(ra) || !Number.isFinite(dec)) continue;
    const tag = it.id !== undefined && it.id !== null && it.id !== '' ? ` # text={${ds9Text(it.id)}}` : '';
    out.push(`circle(${ra.toFixed(7)},${dec.toFixed(7)},${rad}")${tag}`);
  }
  return out.join('\n') + '\n';
}

export const MIME = {
  csv: 'text/csv',
  json: 'application/json',
  ids: 'text/plain',
  ds9: 'text/plain',
};
