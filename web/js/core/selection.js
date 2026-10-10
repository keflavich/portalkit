// Selection masks with frozen set semantics, undo/redo, recipes and hash serialization
// (ARCHITECTURE.md section 6.3). Pure module: no DOM access, loadable under `node --test`.
//
// Semantics [FROZEN]: `hit` covers only rows drawable in the acting view.
//   replace:   sel = hit
//   add:       sel |= hit
//   subtract:  sel &= ~hit
//   intersect: sel = (sel & hit) | (sel & ~drawable)   (rows the view cannot show keep their state)
//
// Hash serialization (`sel=`), checked in order:
//   count <= IDS_IN_HASH_MAX (300)      -> "ids:<id1>,<id2>,..."   (each id encodeURIComponent'ed)
//   n <= BITS_IN_HASH_MAX_N (200000)   -> "bits:<base64url(deflate-raw(mask packed LSB-first))>"
//   otherwise                           -> "r:<base64url(deflate-raw(JSON(recipe)))>"
// deflate-raw / inflate-raw are implemented here synchronously (fixed-Huffman LZ77 encoder;
// full stored/fixed/dynamic decoder) because serialize()/parse() are synchronous and the
// browser CompressionStream API is asynchronous. Both are exported for legacyhash.js and tests.

import {UNDO_MAX, UNDO_BYTES, IDS_IN_HASH_MAX, BITS_IN_HASH_MAX_N} from './constants.js';

export const OPS = Object.freeze(['replace', 'add', 'subtract', 'intersect']);

// ----------------------------------------------------------------------------------------
// base64url and UTF-8
// ----------------------------------------------------------------------------------------

const B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';
const B64_INV = (() => {
  const t = new Int16Array(128).fill(-1);
  for (let i = 0; i < 64; i++) t[B64.charCodeAt(i)] = i;
  // Accept the standard alphabet too.
  t['+'.charCodeAt(0)] = 62;
  t['/'.charCodeAt(0)] = 63;
  return t;
})();

/** Bytes -> base64url without padding. */
export function b64urlEncode(bytes) {
  const b = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  let out = '';
  let i = 0;
  for (; i + 2 < b.length; i += 3) {
    const v = (b[i] << 16) | (b[i + 1] << 8) | b[i + 2];
    out += B64[(v >> 18) & 63] + B64[(v >> 12) & 63] + B64[(v >> 6) & 63] + B64[v & 63];
  }
  const rest = b.length - i;
  if (rest === 1) {
    const v = b[i] << 16;
    out += B64[(v >> 18) & 63] + B64[(v >> 12) & 63];
  } else if (rest === 2) {
    const v = (b[i] << 16) | (b[i + 1] << 8);
    out += B64[(v >> 18) & 63] + B64[(v >> 12) & 63] + B64[(v >> 6) & 63];
  }
  return out;
}

/** base64url (or standard base64, padded or not) -> bytes. Throws SyntaxError on bad input. */
export function b64urlDecode(str) {
  const s = String(str).replace(/=+$/, '');
  if (s.length % 4 === 1) throw new SyntaxError('invalid base64 length');
  const out = new Uint8Array(Math.floor((s.length * 3) / 4));
  let o = 0;
  let acc = 0;
  let nb = 0;
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    const v = c < 128 ? B64_INV[c] : -1;
    if (v < 0) throw new SyntaxError(`invalid base64 character at ${i}`);
    acc = (acc << 6) | v;
    nb += 6;
    if (nb >= 8) {
      nb -= 8;
      out[o++] = (acc >> nb) & 255;
    }
  }
  return out.subarray(0, o);
}

const _te = new TextEncoder();
const _td = new TextDecoder('utf-8', {fatal: false});
export function utf8Encode(s) { return _te.encode(String(s)); }
export function utf8Decode(b) { return _td.decode(b); }

// ----------------------------------------------------------------------------------------
// deflate-raw (RFC 1951) encoder: LZ77 with hash chains + fixed Huffman codes
// ----------------------------------------------------------------------------------------

const LBASE = [3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31, 35, 43, 51, 59, 67, 83, 99, 115, 131, 163, 195, 227, 258];
const LEXT = [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 0];
const DBASE = [1, 2, 3, 4, 5, 7, 9, 13, 17, 25, 33, 49, 65, 97, 129, 193, 257, 385, 513, 769, 1025, 1537, 2049, 3073, 4097, 6145, 8193, 12289, 16385, 24577];
const DEXT = [0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13, 13];
const CLORDER = [16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15];

// length (3..258) -> symbol index into LBASE; distance (1..32768) -> index into DBASE
const LEN_SYM = (() => {
  const t = new Uint8Array(259);
  for (let s = 0; s < 29; s++) {
    const hi = s === 28 ? 258 : LBASE[s + 1] - 1;
    for (let l = LBASE[s]; l <= hi && l <= 258; l++) t[l] = s;
  }
  t[258] = 28;
  return t;
})();

function distSym(d) {
  let lo = 0;
  let hi = 29;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (DBASE[mid] <= d) lo = mid; else hi = mid - 1;
  }
  return lo;
}

function reverseBits(code, len) {
  let r = 0;
  for (let i = 0; i < len; i++) { r = (r << 1) | (code & 1); code >>= 1; }
  return r;
}

// Fixed literal/length codes, pre-reversed for an LSB-first bit writer.
const FIXED_LIT = (() => {
  const code = new Uint16Array(288);
  const len = new Uint8Array(288);
  for (let s = 0; s < 288; s++) {
    let c;
    let l;
    if (s < 144) { c = 0x30 + s; l = 8; } else if (s < 256) { c = 0x190 + (s - 144); l = 9; } else if (s < 280) { c = s - 256; l = 7; } else { c = 0xc0 + (s - 280); l = 8; }
    code[s] = reverseBits(c, l);
    len[s] = l;
  }
  return {code, len};
})();
const FIXED_DIST = (() => {
  const code = new Uint16Array(30);
  for (let s = 0; s < 30; s++) code[s] = reverseBits(s, 5);
  return code;
})();

class BitWriter {
  constructor(cap) {
    this.buf = new Uint8Array(Math.max(64, cap));
    this.pos = 0;
    this.acc = 0;
    this.nbits = 0;
  }
  ensure(extra) {
    if (this.pos + extra <= this.buf.length) return;
    const nb = new Uint8Array(Math.max(this.buf.length * 2, this.pos + extra + 64));
    nb.set(this.buf.subarray(0, this.pos));
    this.buf = nb;
  }
  write(value, n) {
    // n <= 16
    this.acc |= value << this.nbits;
    this.nbits += n;
    this.ensure(4);
    while (this.nbits >= 8) {
      this.buf[this.pos++] = this.acc & 255;
      this.acc >>>= 8;
      this.nbits -= 8;
    }
  }
  finish() {
    this.ensure(2);
    if (this.nbits > 0) {
      this.buf[this.pos++] = this.acc & 255;
      this.acc = 0;
      this.nbits = 0;
    }
    return this.buf.slice(0, this.pos);
  }
}

/** Synchronous raw DEFLATE (no zlib/gzip header). Output inflates with any RFC 1951 decoder. */
export function deflateRaw(input) {
  const data = input instanceof Uint8Array ? input : new Uint8Array(input);
  const n = data.length;
  const w = new BitWriter((n >> 1) + 16);
  w.write(1, 1); // BFINAL
  w.write(1, 2); // BTYPE = 01 (fixed Huffman)
  const WSIZE = 32768;
  const HBITS = 15;
  const HSIZE = 1 << HBITS;
  const head = new Int32Array(HSIZE).fill(-1);
  const prev = new Int32Array(WSIZE);
  const MAXCHAIN = 64;
  const lit = (b) => w.write(FIXED_LIT.code[b], FIXED_LIT.len[b]);
  const hash = (i) => (((data[i] << 10) ^ (data[i + 1] << 5) ^ data[i + 2]) * 0x9e3779b1 >>> (32 - HBITS));
  const insert = (i) => {
    if (i + 2 >= n) return;
    const h = hash(i);
    prev[i & (WSIZE - 1)] = head[h];
    head[h] = i;
  };
  let i = 0;
  while (i < n) {
    let bestLen = 0;
    let bestDist = 0;
    if (i + 2 < n) {
      const h = hash(i);
      let cand = head[h];
      let chain = MAXCHAIN;
      const maxLen = Math.min(258, n - i);
      while (cand >= 0 && i - cand <= WSIZE && chain-- > 0) {
        if (data[cand + bestLen] === data[i + bestLen] && data[cand] === data[i]) {
          let l = 0;
          while (l < maxLen && data[cand + l] === data[i + l]) l++;
          if (l > bestLen) {
            bestLen = l;
            bestDist = i - cand;
            if (l === maxLen) break;
          }
        }
        const p = prev[cand & (WSIZE - 1)];
        if (p >= cand) break;
        cand = p;
      }
    }
    if (bestLen >= 3) {
      const ls = LEN_SYM[bestLen];
      const sym = 257 + ls;
      w.write(FIXED_LIT.code[sym], FIXED_LIT.len[sym]);
      if (LEXT[ls]) w.write(bestLen - LBASE[ls], LEXT[ls]);
      const ds = distSym(bestDist);
      w.write(FIXED_DIST[ds], 5);
      if (DEXT[ds]) w.write(bestDist - DBASE[ds], DEXT[ds]);
      for (let k = 0; k < bestLen; k++) insert(i + k);
      i += bestLen;
    } else {
      lit(data[i]);
      insert(i);
      i += 1;
    }
  }
  w.write(FIXED_LIT.code[256], FIXED_LIT.len[256]); // end of block
  return w.finish();
}

// ----------------------------------------------------------------------------------------
// inflate-raw (RFC 1951) decoder: stored, fixed and dynamic blocks
// ----------------------------------------------------------------------------------------

class Huff {
  constructor(lengths, n) {
    this.count = new Uint16Array(16);
    this.symbol = new Uint16Array(n);
    for (let s = 0; s < n; s++) this.count[lengths[s]]++;
    this.count[0] = 0;
    const offs = new Uint16Array(16);
    for (let l = 1; l < 16; l++) offs[l] = offs[l - 1] + this.count[l - 1];
    for (let s = 0; s < n; s++) if (lengths[s]) this.symbol[offs[lengths[s]]++] = s;
  }
}

const FIXED_LTREE = (() => {
  const l = new Uint8Array(288);
  for (let i = 0; i < 144; i++) l[i] = 8;
  for (let i = 144; i < 256; i++) l[i] = 9;
  for (let i = 256; i < 280; i++) l[i] = 7;
  for (let i = 280; i < 288; i++) l[i] = 8;
  return new Huff(l, 288);
})();
const FIXED_DTREE = new Huff(new Uint8Array(30).fill(5), 30);

/** Synchronous raw INFLATE. Throws Error on corrupt input. maxOut guards against bombs. */
export function inflateRaw(input, maxOut = 1 << 28) {
  const src = input instanceof Uint8Array ? input : new Uint8Array(input);
  let pos = 0;
  let bitbuf = 0;
  let bitcnt = 0;
  let out = new Uint8Array(Math.max(1024, src.length * 4));
  let olen = 0;
  const grow = (extra) => {
    if (olen + extra <= out.length) return;
    if (olen + extra > maxOut) throw new Error('inflate: output exceeds limit');
    const nb = new Uint8Array(Math.min(maxOut, Math.max(out.length * 2, olen + extra)));
    nb.set(out.subarray(0, olen));
    out = nb;
  };
  const bits = (need) => {
    let val = bitbuf;
    while (bitcnt < need) {
      if (pos >= src.length) throw new Error('inflate: unexpected end of data');
      val |= src[pos++] << bitcnt;
      bitcnt += 8;
    }
    bitbuf = val >>> need;
    bitcnt -= need;
    return val & ((1 << need) - 1);
  };
  const decode = (h) => {
    let code = 0;
    let first = 0;
    let index = 0;
    for (let len = 1; len < 16; len++) {
      code |= bits(1);
      const count = h.count[len];
      if (code - count < first) return h.symbol[index + (code - first)];
      index += count;
      first += count;
      first <<= 1;
      code <<= 1;
    }
    throw new Error('inflate: invalid Huffman code');
  };
  const codes = (lt, dt) => {
    for (;;) {
      const sym = decode(lt);
      if (sym < 256) {
        grow(1);
        out[olen++] = sym;
      } else if (sym === 256) {
        return;
      } else {
        const ls = sym - 257;
        if (ls >= 29) throw new Error('inflate: invalid length symbol');
        const len = LBASE[ls] + bits(LEXT[ls]);
        const ds = decode(dt);
        if (ds >= 30) throw new Error('inflate: invalid distance symbol');
        const dist = DBASE[ds] + bits(DEXT[ds]);
        if (dist > olen) throw new Error('inflate: distance too far back');
        grow(len);
        for (let k = 0; k < len; k++, olen++) out[olen] = out[olen - dist];
      }
    }
  };
  let last = 0;
  do {
    last = bits(1);
    const type = bits(2);
    if (type === 0) {
      bitbuf = 0;
      bitcnt = 0;
      if (pos + 4 > src.length) throw new Error('inflate: truncated stored block');
      const len = src[pos] | (src[pos + 1] << 8);
      const nlen = src[pos + 2] | (src[pos + 3] << 8);
      pos += 4;
      if ((len ^ 0xffff) !== nlen) throw new Error('inflate: stored block length mismatch');
      if (pos + len > src.length) throw new Error('inflate: truncated stored block');
      grow(len);
      out.set(src.subarray(pos, pos + len), olen);
      olen += len;
      pos += len;
    } else if (type === 1) {
      codes(FIXED_LTREE, FIXED_DTREE);
    } else if (type === 2) {
      const hlit = bits(5) + 257;
      const hdist = bits(5) + 1;
      const hclen = bits(4) + 4;
      if (hlit > 286 || hdist > 30) throw new Error('inflate: bad dynamic header');
      const cl = new Uint8Array(19);
      for (let k = 0; k < hclen; k++) cl[CLORDER[k]] = bits(3);
      const clh = new Huff(cl, 19);
      const lens = new Uint8Array(hlit + hdist);
      let k = 0;
      while (k < hlit + hdist) {
        const sym = decode(clh);
        if (sym < 16) {
          lens[k++] = sym;
        } else {
          let rep = 0;
          let val = 0;
          if (sym === 16) {
            if (k === 0) throw new Error('inflate: repeat with no previous length');
            val = lens[k - 1];
            rep = 3 + bits(2);
          } else if (sym === 17) {
            rep = 3 + bits(3);
          } else {
            rep = 11 + bits(7);
          }
          if (k + rep > hlit + hdist) throw new Error('inflate: too many code lengths');
          while (rep--) lens[k++] = val;
        }
      }
      if (lens[256] === 0) throw new Error('inflate: missing end-of-block code');
      codes(new Huff(lens.subarray(0, hlit), hlit), new Huff(lens.subarray(hlit), hdist));
    } else {
      throw new Error('inflate: invalid block type 3');
    }
  } while (!last);
  return out.slice(0, olen);
}

// ----------------------------------------------------------------------------------------
// bit packing
// ----------------------------------------------------------------------------------------

/** 0/1 mask -> ceil(n/8) bytes, LSB-first (same layout as the `bits` column encoding). */
export function packBits(mask) {
  const n = mask.length;
  const out = new Uint8Array((n + 7) >> 3);
  for (let i = 0; i < n; i++) if (mask[i]) out[i >> 3] |= 1 << (i & 7);
  return out;
}

/** LSB-first packed bytes -> 0/1 Uint8Array of length n (missing bytes read as 0). */
export function unpackBits(bytes, n) {
  const out = new Uint8Array(n);
  const m = Math.min(n, bytes.length * 8);
  for (let i = 0; i < m; i++) out[i] = (bytes[i >> 3] >> (i & 7)) & 1;
  return out;
}

// ----------------------------------------------------------------------------------------
// Selection
// ----------------------------------------------------------------------------------------

function checkOp(op) {
  if (!OPS.includes(op)) throw new TypeError(`unknown selection op: ${op}`);
}

function safeDecode(s) {
  try {
    return decodeURIComponent(s);
  } catch (err) {
    if (err instanceof URIError) return s;
    throw err;
  }
}

/** Undo depth rule: min(maxUndo, floor(undoBytes / n)), at least 1. */
export function undoDepth(n, maxUndo = UNDO_MAX, undoBytes = UNDO_BYTES) {
  const byBytes = n > 0 ? Math.floor(undoBytes / n) : maxUndo;
  return Math.max(1, Math.min(maxUndo, byBytes));
}

export class Selection {
  /**
   * @param {number} n  rows (stored order)
   * @param {{maxUndo?:number, undoBytes?:number}} [opts]
   */
  constructor(n, opts = {}) {
    const nn = Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
    this.n = nn;
    this._mask = new Uint8Array(nn);
    this._version = 0;
    this._count = 0;
    this.undoDepth = undoDepth(nn, opts.maxUndo ?? UNDO_MAX, opts.undoBytes ?? UNDO_BYTES);
    this._undo = [];
    this._redo = [];
    this._recipe = [];
    this._recipeOk = true;
    this._listeners = new Set();
  }

  get mask() { return this._mask; }
  get version() { return this._version; }
  get count() { return this._count; }
  get canUndo() { return this._undo.length > 0; }
  get canRedo() { return this._redo.length > 0; }

  /** Subscribe to changes (apply, setIndices, clear, invert, undo, redo). Returns unsubscribe. */
  onChange(fn) {
    this._listeners.add(fn);
    return () => { this._listeners.delete(fn); };
  }

  _state() {
    return {mask: this._mask.slice(), count: this._count, recipe: this._recipe.slice(), recipeOk: this._recipeOk};
  }

  _restore(st) {
    this._mask.set(st.mask);
    this._count = st.count;
    this._recipe = st.recipe.slice();
    this._recipeOk = st.recipeOk;
  }

  _pushUndo() {
    this._undo.push(this._state());
    while (this._undo.length > this.undoDepth) this._undo.shift();
    this._redo.length = 0;
  }

  _changed(kind) {
    this._version += 1;
    for (const fn of Array.from(this._listeners)) fn(this, kind);
  }

  _record(op, recipeOp) {
    if (op === 'replace') {
      this._recipe = recipeOp ? [recipeOp] : [];
      this._recipeOk = !!recipeOp;
    } else if (recipeOp) {
      this._recipe.push(recipeOp);
    } else {
      this._recipeOk = false;
    }
  }

  /**
   * Apply a brush / set result.
   * @param {Uint8Array} hit       0/1 per stored row (rows past hit.length count as 0)
   * @param {'replace'|'add'|'subtract'|'intersect'} op
   * @param {Uint8Array} [drawable] 0/1 per row; only used by intersect (absent = all drawable)
   * @param {object} [recipeOp]    RecipeOp describing the action (keeps the r: serialization valid)
   */
  apply(hit, op, drawable, recipeOp) {
    checkOp(op);
    this._pushUndo();
    const m = this._mask;
    const n = this.n;
    const hn = hit ? Math.min(hit.length, n) : 0;
    let count = 0;
    if (op === 'replace') {
      for (let i = 0; i < n; i++) {
        const v = i < hn && hit[i] ? 1 : 0;
        m[i] = v;
        count += v;
      }
    } else if (op === 'add') {
      for (let i = 0; i < n; i++) {
        if (i < hn && hit[i]) m[i] = 1;
        count += m[i];
      }
    } else if (op === 'subtract') {
      for (let i = 0; i < n; i++) {
        if (i < hn && hit[i]) m[i] = 0;
        count += m[i];
      }
    } else {
      const dn = drawable ? Math.min(drawable.length, n) : n;
      for (let i = 0; i < n; i++) {
        if (m[i] && !(i < hn && hit[i])) {
          const isDrawable = drawable ? (i < dn && drawable[i] ? 1 : 0) : 1;
          if (isDrawable) m[i] = 0;
        }
        count += m[i];
      }
    }
    this._count = count;
    this._record(op, recipeOp);
    this._changed(op);
  }

  /** Apply a set of row indices (out-of-range and non-integer entries are ignored). */
  setIndices(idx, op, recipeOp) {
    checkOp(op);
    const hit = new Uint8Array(this.n);
    const valid = [];
    const len = idx ? idx.length : 0;
    for (let k = 0; k < len; k++) {
      const i = idx[k];
      if (Number.isInteger(i) && i >= 0 && i < this.n && !hit[i]) {
        hit[i] = 1;
        valid.push(i);
      }
    }
    let rop = recipeOp;
    if (!rop) {
      rop = {op, kind: 'ids', view: {ds: null}};
      // Indices are turned into ids at serialize() time; keep them out of JSON.
      Object.defineProperty(rop, '_idx', {value: Int32Array.from(valid), enumerable: false});
    }
    this.apply(hit, op, undefined, rop);
  }

  /**
   * Replace the mask and the recipe in one undoable step (kind 'replace'), e.g. after a linked
   * r: selection was replayed on a scratch Selection. `recipe` is a RecipeOp[] (copied).
   */
  replaceWith(hit, recipe, recipeOk = true) {
    this._pushUndo();
    const m = this._mask;
    const n = this.n;
    const hn = hit ? Math.min(hit.length, n) : 0;
    let count = 0;
    for (let i = 0; i < n; i++) {
      const v = i < hn && hit[i] ? 1 : 0;
      m[i] = v;
      count += v;
    }
    this._count = count;
    this._recipe = Array.isArray(recipe) ? recipe.slice() : [];
    this._recipeOk = !!recipeOk && (this._recipe.length > 0 || count === 0);
    this._changed('replace');
  }

  clear() {
    if (this._count === 0 && this._recipe.length === 0 && this._recipeOk) return;
    this._pushUndo();
    this._mask.fill(0);
    this._count = 0;
    this._recipe = [];
    this._recipeOk = true;
    this._changed('clear');
  }

  /** Invert within drawable (all rows when drawable is absent). */
  invert(drawable) {
    this._pushUndo();
    const m = this._mask;
    const n = this.n;
    const dn = drawable ? Math.min(drawable.length, n) : n;
    let count = 0;
    for (let i = 0; i < n; i++) {
      if (!drawable || (i < dn && drawable[i])) m[i] = m[i] ? 0 : 1;
      count += m[i];
    }
    this._count = count;
    this._recipeOk = false;   // inversion is not expressible as a RecipeOp
    this._changed('invert');
  }

  undo() {
    if (!this._undo.length) return false;
    this._redo.push(this._state());
    this._restore(this._undo.pop());
    this._changed('undo');
    return true;
  }

  redo() {
    if (!this._redo.length) return false;
    this._undo.push(this._state());
    while (this._undo.length > this.undoDepth) this._undo.shift();
    this._restore(this._redo.pop());
    this._changed('redo');
    return true;
  }

  indices() {
    const out = new Int32Array(this._count);
    let k = 0;
    const m = this._mask;
    for (let i = 0; i < m.length && k < out.length; i++) if (m[i]) out[k++] = i;
    return out;
  }

  /** Ops since the last replace/clear (copies). */
  recipe() {
    return this._recipe.map((op) => {
      const c = Object.assign({}, op);
      if (op._idx) Object.defineProperty(c, '_idx', {value: op._idx, enumerable: false});
      return c;
    });
  }

  /** True when recipe() reproduces the current mask (no invert or op without RecipeOp since the last replace/clear). */
  get recipeComplete() {
    return this._recipeOk;
  }

  /**
   * Hash form of the selection: 'ids:..' | 'bits:..' | 'r:..' | ''.
   * '' means empty, or not serializable (n > BITS_IN_HASH_MAX_N with an incomplete recipe).
   */
  serialize(idOf) {
    if (this._count === 0) return '';
    if (this._count <= IDS_IN_HASH_MAX && typeof idOf === 'function') {
      const ids = [];
      const m = this._mask;
      for (let i = 0; i < m.length; i++) if (m[i]) ids.push(encodeURIComponent(String(idOf(i))));
      return 'ids:' + ids.join(',');
    }
    if (this.n <= BITS_IN_HASH_MAX_N) {
      return 'bits:' + b64urlEncode(deflateRaw(packBits(this._mask)));
    }
    if (!this._recipeOk || !this._recipe.length) return '';
    const ops = this._recipe.map((op) => {
      if (op._idx && !op.ids) {
        if (typeof idOf !== 'function') return null;
        return Object.assign({}, op, {ids: Array.from(op._idx, (i) => String(idOf(i)))});
      }
      return op;
    });
    if (ops.includes(null)) return '';
    return 'r:' + b64urlEncode(deflateRaw(utf8Encode(JSON.stringify(ops))));
  }

  /**
   * Parse a serialized selection. Returns null for '' or malformed input.
   * Accepts "r:" followed by raw JSON ("[...]") as well as the compressed form.
   */
  static parse(s) {
    if (typeof s !== 'string') return null;
    const str = s.trim();
    if (!str) return null;
    const colon = str.indexOf(':');
    if (colon < 0) return null;
    const kind = str.slice(0, colon);
    const rest = str.slice(colon + 1);
    if (kind === 'ids') {
      const ids = rest === '' ? [] : rest.split(',').map(safeDecode);
      return {kind: 'ids', ids};
    }
    if (kind === 'bits') {
      try {
        return {kind: 'bits', bytes: inflateRaw(b64urlDecode(rest))};
      } catch (err) {
        if (err instanceof Error) return null;   // SyntaxError (base64, JSON) or inflate errors
        throw err;
      }
    }
    if (kind === 'r') {
      let text;
      try {
        const raw = safeDecode(rest);
        text = raw.startsWith('[') ? raw : utf8Decode(inflateRaw(b64urlDecode(rest)));
        const ops = JSON.parse(text);
        if (!Array.isArray(ops)) return null;
        return {kind: 'r', ops};
      } catch (err) {
        if (err instanceof Error) return null;   // SyntaxError (base64, JSON) or inflate errors
        throw err;
      }
    }
    return null;
  }
}

/** Modifier keys -> op: none replace, shift add, alt subtract, shift+alt intersect. */
export function opFromEvent(e) {
  const shift = !!(e && e.shiftKey);
  const alt = !!(e && e.altKey);
  if (shift && alt) return 'intersect';
  if (shift) return 'add';
  if (alt) return 'subtract';
  return 'replace';
}
