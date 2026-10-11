// Worker-side argsort for DataClient.order (IMPLEMENTATION_NOTES 74). Every function returns an
// Int32Array permutation of all n rows: valid rows sorted by key in the requested direction, ties
// in ascending row order (stable), then the missing rows (NaN, missing codes, null strings) in
// ascending row order, in both directions.
//
// The sorts are stable LSD radix sorts over unsigned keys (16-bit digits, a pass is skipped when
// every row shares the digit), so 5M rows sort in O(n) with a few typed arrays and no comparator.
// Natural (id / string) order encodes each string as a byte sort key: every character becomes its
// Intl.Collator (numeric, base sensitivity) rank among single characters, and every ASCII digit run
// becomes [digit marker, significant-digit count, digits]; keys sort by a stable MSD radix sort
// that only descends into groups still tied. Rows whose keys are identical but whose strings differ
// (case / accent variants, leading zeros) are ordered afterwards per tie group.

const LITTLE_ENDIAN = new Uint8Array(new Uint32Array([1]).buffer)[0] === 1;
const SIGN = 0x80000000;

/** +1 ascending, -1 descending ('desc', '-', -1, 'descending'); anything else is ascending. */
export function normDir(dir) {
  if (dir === -1 || dir === 'desc' || dir === 'descending' || dir === '-') return -1;
  if (typeof dir === 'number' && dir < 0) return -1;
  return 1;
}

function identityRows(valid, n) {
  let m = 0;
  if (valid) {
    for (let i = 0; i < n; i++) if (valid[i]) m++;
  } else m = n;
  const rows = new Int32Array(m);
  const miss = new Int32Array(n - m);
  let a = 0, b = 0;
  for (let i = 0; i < n; i++) {
    if (!valid || valid[i]) rows[a++] = i;
    else miss[b++] = i;
  }
  return {rows, miss};
}

/**
 * Stable LSD sort of `rows` (Int32Array of row indices) by the 32-bit unsigned per-row key
 * `key[row]`. `tmp` has the length of `rows`; returns whichever of the two holds the result.
 */
function radix32(rows, tmp, key, cnt) {
  const m = rows.length;
  if (m < 2) return rows;
  let src = rows, dst = tmp;
  for (let shift = 0; shift < 32; shift += 16) {
    cnt.fill(0);
    for (let q = 0; q < m; q++) cnt[(key[src[q]] >>> shift) & 0xffff]++;
    if (cnt[(key[src[0]] >>> shift) & 0xffff] === m) continue;
    let sum = 0;
    for (let d = 0; d < 65536; d++) {
      const c = cnt[d];
      cnt[d] = sum;
      sum += c;
    }
    for (let q = 0; q < m; q++) {
      const r = src[q];
      dst[cnt[(key[r] >>> shift) & 0xffff]++] = r;
    }
    const t = src;
    src = dst;
    dst = t;
  }
  return src;
}

/**
 * Sort `rows` stably by a sequence of unsigned 32-bit per-row keys, most significant first:
 * keys = [k0, k1, ...] orders by k0, then k1, ... (LSD: the last key is applied first).
 */
function sortByKeys(rows, keys) {
  if (rows.length < 2 || !keys.length) return rows;
  const tmp = new Int32Array(rows.length);
  const cnt = new Uint32Array(65536);
  let cur = rows;
  for (let k = keys.length - 1; k >= 0; k--) {
    const out = radix32(cur, cur === rows ? tmp : rows, keys[k], cnt);
    cur = out;
  }
  if (cur !== rows) rows.set(cur);
  return rows;
}

function finish(rows, miss, n) {
  const out = new Int32Array(n);
  out.set(rows, 0);
  out.set(miss, rows.length);
  return out;
}

/**
 * Argsort of numeric values (Float32Array, Float64Array or plain array). NaN is missing;
 * -Infinity / +Infinity sort at the ends of the valid block; -0 equals +0.
 */
export function argsortValues(values, dir = 1) {
  const n = values.length;
  const neg = normDir(dir) < 0;
  const valid = new Uint8Array(n);
  for (let i = 0; i < n; i++) {
    const v = +values[i];
    valid[i] = v === v ? 1 : 0;
  }
  const {rows, miss} = identityRows(valid, n);
  if (rows.length < 2) return finish(rows, miss, n);
  if (values instanceof Float32Array) {
    const f = new Float32Array(n);
    const u = new Uint32Array(f.buffer);
    for (let i = 0; i < n; i++) f[i] = values[i] + 0;   // + 0 folds -0 into +0
    for (let i = 0; i < n; i++) {
      if (!valid[i]) {
        u[i] = 0;
        continue;
      }
      let h = u[i];
      h = h & SIGN ? ~h >>> 0 : (h | SIGN) >>> 0;
      u[i] = neg ? ~h >>> 0 : h;
    }
    sortByKeys(rows, [u]);
    return finish(rows, miss, n);
  }
  const f = new Float64Array(n);
  for (let i = 0; i < n; i++) f[i] = valid[i] ? +values[i] + 0 : 0;
  const u = new Uint32Array(f.buffer);
  const hi = new Uint32Array(n), lo = new Uint32Array(n);
  const iLo = LITTLE_ENDIAN ? 0 : 1, iHi = 1 - iLo;
  for (let i = 0; i < n; i++) {
    let h = u[2 * i + iHi], l = u[2 * i + iLo];
    if (h & SIGN) {
      h = ~h >>> 0;
      l = ~l >>> 0;
    } else h = (h | SIGN) >>> 0;
    if (neg) {
      h = ~h >>> 0;
      l = ~l >>> 0;
    }
    hi[i] = h;
    lo[i] = l;
  }
  sortByKeys(rows, [hi, lo]);
  return finish(rows, miss, n);
}

/**
 * Argsort of unsigned integer codes (Uint8Array / Uint16Array / Uint32Array) where `missing`
 * (a code value, or null for none) marks missing rows. Codes are compared numerically.
 */
export function argsortCodes(codes, missing = null, dir = 1) {
  const n = codes.length;
  const neg = normDir(dir) < 0;
  let valid = null;
  if (missing !== null && missing !== undefined) {
    valid = new Uint8Array(n);
    for (let i = 0; i < n; i++) valid[i] = codes[i] === missing ? 0 : 1;
  }
  const {rows, miss} = identityRows(valid, n);
  if (rows.length < 2) return finish(rows, miss, n);
  let key;
  if (!neg && codes instanceof Uint32Array) key = codes;
  else {
    key = new Uint32Array(n);
    for (let i = 0; i < n; i++) key[i] = neg ? ~codes[i] >>> 0 : codes[i] >>> 0;
  }
  sortByKeys(rows, [key]);
  return finish(rows, miss, n);
}

/** Collator used for text runs and category labels (numeric, so "a2" < "a10"). */
export function naturalCollator() {
  return new Intl.Collator('en', {numeric: true});
}

/** Comparator: collator order, then code-unit order so distinct strings never tie. */
function textCompare(coll) {
  return (a, b) => coll.compare(a, b) || (a < b ? -1 : a > b ? 1 : 0);
}

/**
 * Argsort of category codes by level label (natural order). `levels[code]` is the label; codes
 * without a label sort after the labelled ones by code in both directions; `missing` codes last.
 */
export function argsortCategory(codes, levels, missing, dir = 1) {
  const L = Array.isArray(levels) ? levels.length : 0;
  const coll = naturalCollator();
  const order = Array.from({length: L}, (_, j) => j);
  const cmp = textCompare(coll);
  order.sort((a, b) => cmp(String(levels[a]), String(levels[b])) || a - b);
  const rank = new Uint32Array(L);
  order.forEach((j, r) => {
    rank[j] = r;
  });
  const n = codes.length;
  const neg = normDir(dir) < 0;
  // labelled codes take ranks 0..L-1 (reversed for descending); unlabeled codes follow in code
  // order in both directions, then the missing code
  const key = new Uint32Array(n);
  for (let i = 0; i < n; i++) {
    const c = codes[i];
    key[i] = c < L ? (neg ? L - 1 - rank[c] : rank[c]) : L + c;
  }
  const valid = new Uint8Array(n);
  for (let i = 0; i < n; i++) valid[i] = codes[i] === missing ? 0 : 1;
  const {rows, miss} = identityRows(valid, n);
  if (rows.length < 2) return finish(rows, miss, n);
  sortByKeys(rows, [key]);
  return finish(rows, miss, n);
}

// ------------------------------------------------------------------ natural (string) order

let ASCII_TABLE = null;

/**
 * Character weight table: `ascii` (Uint16Array(128)) and `other` (Map code point -> weight) give
 * each character its rank among single characters under Intl.Collator('en', numeric, base
 * sensitivity), starting at 1 (characters equal at base strength share a weight; 0 = ignorable,
 * left out of the key). `digit` is the weight of a digit run; `wide` is true when weights need
 * two bytes.
 */
function weightTable(extra) {
  const base = new Intl.Collator('en', {numeric: true, sensitivity: 'base'});
  const chars = [];
  for (let c = 0; c < 128; c++) if (c < 49 || c > 57) chars.push(String.fromCharCode(c));
  for (const cp of extra || []) chars.push(String.fromCodePoint(cp));
  const live = chars.filter((ch) => base.compare(ch, '') !== 0);
  live.sort((a, b) => base.compare(a, b));
  const ascii = new Uint16Array(128);
  const other = new Map();
  let w = 0;
  live.forEach((ch, r) => {
    if (r === 0 || base.compare(live[r - 1], ch) !== 0) w++;
    const cp = ch.codePointAt(0);
    if (cp < 128) ascii[cp] = w;
    else other.set(cp, w);
  });
  const digit = ascii[48];
  for (let c = 49; c <= 57; c++) ascii[c] = digit;
  return {ascii, other, digit, wide: w > 255};
}

function asciiTable() {
  if (!ASCII_TABLE) ASCII_TABLE = weightTable(null);
  return ASCII_TABLE;
}

/** Growable byte buffer of per-row sort keys: key of row i is buf[off[i] .. off[i + 1]). */
class KeyBuf {
  constructor(n) {
    this.buf = new Uint8Array(Math.max(64, n * 16));
    this.len = 0;
    this.off = new Uint32Array(n + 1);
  }
  ensure(extra) {
    if (this.len + extra <= this.buf.length) return;
    let cap = this.buf.length * 2;
    while (cap < this.len + extra) cap *= 2;
    const b = new Uint8Array(cap);
    b.set(this.buf.subarray(0, this.len));
    this.buf = b;
  }
}

// Every key byte is in 1..255, so a key that has ended (padding 0 ascending, 255 descending after
// the bytes are flipped to 255 - b) sorts before (after) any continuation. Wide weights are two
// bytes 1 + w / 255, 1 + w % 255. A digit run is [digit weight, significant-digit count (max
// 255), digit pairs 1 + 10 a + b, a last odd digit 1 + a].

function putWeight(buf, p, w, wide) {
  if (wide) {
    buf[p] = 1 + ((w / 255) | 0);
    buf[p + 1] = 1 + (w % 255);
    return p + 2;
  }
  buf[p] = w;
  return p + 1;
}

/** Append the sort key of string `s` (weights from `tbl`). */
function encodeString(kb, s, tbl) {
  const L = s.length;
  // worst case per code unit: a one-digit run (weight 2 + count 1 + digit 1)
  kb.ensure(4 * L + 4);
  const buf = kb.buf, ascii = tbl.ascii, wide = tbl.wide;
  let p = kb.len;
  for (let k = 0; k < L;) {
    const c = s.charCodeAt(k);
    if (c >= 48 && c <= 57) {
      let a = k;
      let e = k + 1;
      while (e < L) {
        const d = s.charCodeAt(e);
        if (d < 48 || d > 57) break;
        e++;
      }
      while (a < e - 1 && s.charCodeAt(a) === 48) a++;
      p = putWeight(buf, p, tbl.digit, wide);
      buf[p++] = e - a > 255 ? 255 : e - a;
      for (; a + 1 < e; a += 2) buf[p++] = 1 + (s.charCodeAt(a) - 48) * 10 + (s.charCodeAt(a + 1) - 48);
      if (a < e) buf[p++] = 1 + (s.charCodeAt(a) - 48);
      k = e;
      continue;
    }
    let w;
    if (c < 128) {
      w = ascii[c];
      k++;
    } else {
      const cp = s.codePointAt(k);
      w = tbl.other.get(cp) || 0;
      k += cp > 0xffff ? 2 : 1;
    }
    if (w) p = putWeight(buf, p, w, wide);
  }
  kb.len = p;
}

/** Append the sort key of the ASCII bytes blob[a..b) (narrow ASCII table). */
function encodeAscii(kb, blob, a, b, tbl) {
  kb.ensure(4 * (b - a) + 4);
  const buf = kb.buf, ascii = tbl.ascii, digit = tbl.digit;
  let p = kb.len;
  for (let k = a; k < b;) {
    const c = blob[k];
    if (c >= 48 && c <= 57) {
      let s = k;
      let e = k + 1;
      while (e < b && blob[e] >= 48 && blob[e] <= 57) e++;
      while (s < e - 1 && blob[s] === 48) s++;
      buf[p++] = digit;
      buf[p++] = e - s > 255 ? 255 : e - s;
      for (; s + 1 < e; s += 2) buf[p++] = 1 + (blob[s] - 48) * 10 + (blob[s + 1] - 48);
      if (s < e) buf[p++] = 1 + (blob[s] - 48);
      k = e;
      continue;
    }
    const w = ascii[c];
    if (w) buf[p++] = w;
    k++;
  }
  kb.len = p;
}

function isAscii(u8) {
  for (let k = 0; k < u8.length; k++) if (u8[k] > 127) return false;
  return true;
}

/** Tie order for strings with equal sort keys: code units with ASCII case swapped (lower first). */
function tieCompare(a, b) {
  const L = Math.min(a.length, b.length);
  for (let k = 0; k < L; k++) {
    let x = a.charCodeAt(k), y = b.charCodeAt(k);
    if (x === y) continue;
    if (x >= 65 && x <= 90) x += 32;
    else if (x >= 97 && x <= 122) x -= 32;
    if (y >= 65 && y <= 90) y += 32;
    else if (y >= 97 && y <= 122) y -= 32;
    return x - y;
  }
  return a.length - b.length;
}

/**
 * Natural argsort of strings. `get(i)` returns the string of row i (null / undefined = missing).
 * Order: characters by Intl.Collator ('en', numeric) rank at base strength (punctuation, digits,
 * then letters; case and accents fold), ASCII digit runs by value (exact for any length up to 255
 * significant digits), a string that ends earlier first; strings equal at that strength order by
 * code units with lower case first; identical strings keep row order.
 *
 * opts.sources: optional [{start, n, offsets, blob}] UTF-8 string tables (codec decodeStr) that
 * hold rows start..start+n-1; when every blob is ASCII the keys are built from the bytes directly
 * (no string decoding; `get` is then called only for rows whose keys tie).
 */
export function argsortNatural(n, get, dir = 1, {sources = null} = {}) {
  const neg = normDir(dir) < 0;
  const valid = new Uint8Array(n);
  let tbl = asciiTable();
  let kb = new KeyBuf(n);
  const off = kb.off;
  const bytePath = Array.isArray(sources) && sources.length > 0 && sources.every((s) => s && s.blob && isAscii(s.blob));
  if (bytePath) {
    let i = 0;
    for (const src of sources) {
      const end = Math.min(n, src.start + src.n);
      for (; i < src.start && i < n; i++) off[i + 1] = kb.len;
      for (; i < end; i++) {
        const j = i - src.start;
        valid[i] = 1;
        encodeAscii(kb, src.blob, src.offsets[j], src.offsets[j + 1], tbl);
        off[i + 1] = kb.len;
      }
    }
    for (; i < n; i++) off[i + 1] = kb.len;
  } else {
    let extra = null;
    for (let i = 0; i < n; i++) {
      const v = get(i);
      if (v !== null && v !== undefined) {
        valid[i] = 1;
        const s = String(v);
        encodeString(kb, s, tbl);
        if (!extra) {
          for (let k = 0; k < s.length; k++) {
            if (s.charCodeAt(k) > 127) {
              extra = new Set();
              break;
            }
          }
        }
        if (extra) {
          for (let k = 0; k < s.length; k++) {
            if (s.charCodeAt(k) > 127) {
              const cp = s.codePointAt(k);
              extra.add(cp);
              if (cp > 0xffff) k++;
            }
          }
        }
      }
      off[i + 1] = kb.len;
    }
    if (extra) {
      // non-ASCII characters change the ranks: rebuild every key with the merged table
      tbl = weightTable(extra);
      kb = new KeyBuf(n);
      for (let i = 0; i < n; i++) {
        if (valid[i]) encodeString(kb, String(get(i)), tbl);
        kb.off[i + 1] = kb.len;
      }
    }
  }
  const {rows, miss} = identityRows(valid, n);
  if (rows.length < 2) return finish(rows, miss, n);
  const ties = msdSort(rows, kb.buf, kb.off, neg);
  fixTies(rows, ties, get, neg);
  return finish(rows, miss, n);
}

const SMALL_GROUP = 24;       // insertion sort at or below this many rows
const BYTE_GROUP = 32768;     // 8-bit digits below this many rows, 16-bit digits above

/**
 * Stable MSD radix sort of `rows` by their key bytes (ko/buf), descending when `neg` (bytes
 * flipped to 255 - b; an ended key reads as 0 ascending, 255 descending, so it sorts first / last).
 * Groups whose keys are fully equal keep their incoming (ascending row) order; their position
 * ranges are returned as a flat [lo, hi, lo, hi, ...] array.
 */
function msdSort(rows, buf, ko, neg) {
  const m = rows.length;
  const flip = neg ? 255 : 0;
  const pad = neg ? 255 : 0;
  const end16 = neg ? 0xffff : 0;
  const tmp = new Int32Array(m);
  const dg = new Uint16Array(m);
  const cnt = new Uint32Array(65537);
  const stack = [0, m, 0];
  const ties = [];
  while (stack.length) {
    const d = stack.pop(), hi = stack.pop(), lo = stack.pop();
    const size = hi - lo;
    if (size <= SMALL_GROUP) {
      insertionFrom(rows, lo, hi, buf, ko, d, flip, pad);
      for (let q = lo; q < hi;) {
        let e = q + 1;
        while (e < hi && cmpFrom(buf, ko, rows[q], rows[e], d, flip, pad) === 0) e++;
        if (e - q > 1) ties.push(q, e);
        q = e;
      }
      continue;
    }
    const wide = size >= BYTE_GROUP;
    const B = wide ? 65536 : 256;
    const endMark = wide ? end16 : pad;
    for (let q = lo; q < hi; q++) {
      const r = rows[q];
      const a = ko[r];
      const l = ko[r + 1] - a;
      const b0 = d < l ? buf[a + d] ^ flip : pad;
      dg[q] = wide ? (b0 << 8) | (d + 1 < l ? buf[a + d + 1] ^ flip : pad) : b0;
    }
    const step = wide ? 2 : 1;
    cnt.fill(0, 0, B + 1);
    for (let q = lo; q < hi; q++) cnt[dg[q] + 1]++;
    const first = dg[lo];
    if (cnt[first + 1] === size) {
      // every row shares this digit: go one level deeper without moving rows
      if (first !== endMark) stack.push(lo, hi, d + step);
      else ties.push(lo, hi);
      continue;
    }
    for (let b = 0; b < B; b++) cnt[b + 1] += cnt[b];
    // cnt[b] is now the start of bucket b; scatter (stable) into tmp, then copy back
    for (let b = 0; b < B; b++) {
      const s = cnt[b], e = cnt[b + 1];
      if (e - s > 1) {
        if (b !== endMark) stack.push(lo + s, lo + e, d + step);
        else ties.push(lo + s, lo + e);
      }
    }
    for (let q = lo; q < hi; q++) tmp[lo + cnt[dg[q]]++] = rows[q];
    rows.set(tmp.subarray(lo, hi), lo);
  }
  return ties;
}

/** Compare the keys of rows x and y from byte d on (flipped / padded as in msdSort). */
function cmpFrom(buf, ko, x, y, d, flip, pad) {
  const ax = ko[x], lx = ko[x + 1] - ax, ay = ko[y], ly = ko[y + 1] - ay;
  const L = lx > ly ? lx : ly;
  for (let k = d; k < L; k++) {
    const u = k < lx ? buf[ax + k] ^ flip : pad;
    const v = k < ly ? buf[ay + k] ^ flip : pad;
    if (u !== v) return u - v;
  }
  return 0;
}

function insertionFrom(rows, lo, hi, buf, ko, d, flip, pad) {
  for (let q = lo + 1; q < hi; q++) {
    const r = rows[q];
    let p = q - 1;
    while (p >= lo && cmpFrom(buf, ko, rows[p], r, d, flip, pad) > 0) {
      rows[p + 1] = rows[p];
      p--;
    }
    rows[p + 1] = r;
  }
}

/**
 * Order every range of rows with identical keys (`ties` = [lo, hi, ...] from msdSort) by
 * tieCompare in the requested direction; identical strings keep row order (the sort was stable).
 */
function fixTies(rows, ties, get, neg) {
  const sgn = neg ? -1 : 1;
  for (let t = 0; t < ties.length; t += 2) {
    const g0 = ties[t], g1 = ties[t + 1];
    if (g1 - g0 > 1) {
      const strs = new Array(g1 - g0);
      let same = true;
      for (let q = g0; q < g1; q++) {
        const v = get(rows[q]);
        strs[q - g0] = String(v);
        if (same && strs[q - g0] !== strs[0]) same = false;
      }
      if (!same) {
        const uid = new Map();
        const reps = [];
        const ids = new Int32Array(g1 - g0);
        for (let k = 0; k < strs.length; k++) {
          let u = uid.get(strs[k]);
          if (u === undefined) {
            u = reps.length;
            uid.set(strs[k], u);
            reps.push(k);
          }
          ids[k] = u;
        }
        const ord = reps.map((_, u) => u).sort((x, y) => sgn * tieCompare(strs[reps[x]], strs[reps[y]]));
        const rank = new Int32Array(reps.length);
        ord.forEach((u, r) => {
          rank[u] = r;
        });
        const start = new Int32Array(reps.length + 1);
        for (let k = 0; k < ids.length; k++) start[rank[ids[k]] + 1]++;
        for (let r = 0; r < reps.length; r++) start[r + 1] += start[r];
        const out = new Int32Array(g1 - g0);
        for (let k = 0; k < ids.length; k++) out[start[rank[ids[k]]]++] = rows[g0 + k];
        rows.set(out, g0);
      }
    }
  }
}
