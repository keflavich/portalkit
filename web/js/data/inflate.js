/*
 * Raw DEFLATE decoder plus gzip member parsing (ARCHITECTURE.md section 5.1 "Inflate").
 * Fallback for environments without DecompressionStream('gzip').
 *
 * The inflate core is a port of tiny-inflate (https://github.com/foliojs/tiny-inflate):
 *
 *   MIT License
 *
 *   Copyright (c) 2015-present Devon Govett
 *
 *   Permission is hereby granted, free of charge, to any person obtaining a copy
 *   of this software and associated documentation files (the "Software"), to deal
 *   in the Software without restriction, including without limitation the rights
 *   to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 *   copies of the Software, and to permit persons to whom the Software is
 *   furnished to do so, subject to the following conditions:
 *
 *   The above copyright notice and this permission notice shall be included in all
 *   copies or substantial portions of the Software.
 *
 *   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 *   IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 *   FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 *   AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 *   LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 *   OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 *   SOFTWARE.
 *
 * tiny-inflate is itself a port of tinf, (c) 2003-2019 Joergen Ibsen (zlib license).
 *
 * Changes from upstream: growable output buffer, bounds checks that throw on corrupt
 * input, byte-exact realignment for stored blocks, the number of consumed input bytes is
 * reported (needed to walk concatenated gzip members), gzip header/trailer handling with
 * CRC32 verification. Pure module (no DOM).
 */

export class InflateError extends Error {
  constructor(msg) {
    super(msg);
    this.name = 'InflateError';
  }
}

function Tree() {
  this.table = new Uint16Array(16); // code length counts
  this.trans = new Uint16Array(288); // code -> symbol translation table
}

function Data(source, dest) {
  this.source = source;
  this.sourceIndex = 0;
  this.tag = 0;
  this.bitcount = 0;
  this.dest = dest;
  this.destLen = 0;
  this.ltree = new Tree();
  this.dtree = new Tree();
}

// ------------------------------------------------------------------ static tables

const sltree = new Tree();
const sdtree = new Tree();

const lengthBits = new Uint8Array(30);
const lengthBase = new Uint16Array(30);
const distBits = new Uint8Array(30);
const distBase = new Uint16Array(30);

const clcidx = new Uint8Array([16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15]);

const codeTree = new Tree();
const lengths = new Uint8Array(288 + 32);
const offs = new Uint16Array(16);

function buildBitsBase(bits, base, delta, first) {
  let i, sum;
  for (i = 0; i < delta; ++i) bits[i] = 0;
  for (i = 0; i < 30 - delta; ++i) bits[i + delta] = (i / delta) | 0;
  for (sum = first, i = 0; i < 30; ++i) {
    base[i] = sum;
    sum += 1 << bits[i];
  }
}

function buildFixedTrees(lt, dt) {
  let i;
  for (i = 0; i < 7; ++i) lt.table[i] = 0;
  lt.table[7] = 24;
  lt.table[8] = 152;
  lt.table[9] = 112;
  for (i = 0; i < 24; ++i) lt.trans[i] = 256 + i;
  for (i = 0; i < 144; ++i) lt.trans[24 + i] = i;
  for (i = 0; i < 8; ++i) lt.trans[24 + 144 + i] = 280 + i;
  for (i = 0; i < 112; ++i) lt.trans[24 + 144 + 8 + i] = 144 + i;
  for (i = 0; i < 5; ++i) dt.table[i] = 0;
  dt.table[5] = 32;
  for (i = 0; i < 32; ++i) dt.trans[i] = i;
}

function buildTree(t, lens, off, num) {
  let i, sum;
  for (i = 0; i < 16; ++i) t.table[i] = 0;
  for (i = 0; i < num; ++i) t.table[lens[off + i]]++;
  t.table[0] = 0;
  for (sum = 0, i = 0; i < 16; ++i) {
    offs[i] = sum;
    sum += t.table[i];
  }
  for (i = 0; i < num; ++i) {
    if (lens[off + i]) t.trans[offs[lens[off + i]]++] = i;
  }
}

buildFixedTrees(sltree, sdtree);
buildBitsBase(lengthBits, lengthBase, 4, 3);
buildBitsBase(distBits, distBase, 2, 1);
// fix a special case of length 258
lengthBits[28] = 0;
lengthBase[28] = 258;

// ------------------------------------------------------------------ bit reading

function getbit(d) {
  if (!d.bitcount--) {
    d.tag = d.source[d.sourceIndex++] | 0;
    d.bitcount = 7;
  }
  const bit = d.tag & 1;
  d.tag >>>= 1;
  return bit;
}

function readBits(d, num, base) {
  if (!num) return base;
  while (d.bitcount < 24) {
    d.tag |= (d.source[d.sourceIndex++] | 0) << d.bitcount;
    d.bitcount += 8;
  }
  const val = d.tag & (0xffff >>> (16 - num));
  d.tag >>>= num;
  d.bitcount -= num;
  return val + base;
}

function decodeSymbol(d, t) {
  while (d.bitcount < 24) {
    d.tag |= (d.source[d.sourceIndex++] | 0) << d.bitcount;
    d.bitcount += 8;
  }
  let sum = 0, cur = 0, len = 0;
  let tag = d.tag;
  do {
    cur = 2 * cur + (tag & 1);
    tag >>>= 1;
    ++len;
    if (len > 15) throw new InflateError('invalid Huffman code');
    sum += t.table[len];
    cur -= t.table[len];
  } while (cur >= 0);
  d.tag = tag;
  d.bitcount -= len;
  return t.trans[sum + cur];
}

function decodeTrees(d, lt, dt) {
  let i, num, length;
  const hlit = readBits(d, 5, 257);
  const hdist = readBits(d, 5, 1);
  const hclen = readBits(d, 4, 4);
  if (hlit > 286 || hdist > 30) throw new InflateError('invalid dynamic tree sizes');
  for (i = 0; i < 19; ++i) lengths[i] = 0;
  for (i = 0; i < hclen; ++i) lengths[clcidx[i]] = readBits(d, 3, 0);
  buildTree(codeTree, lengths, 0, 19);
  const total = hlit + hdist;
  for (num = 0; num < total;) {
    const sym = decodeSymbol(d, codeTree);
    switch (sym) {
      case 16: {
        if (num === 0) throw new InflateError('repeat with no previous length');
        const prev = lengths[num - 1];
        for (length = readBits(d, 2, 3); length; --length) {
          if (num >= total) throw new InflateError('code length repeat overflows');
          lengths[num++] = prev;
        }
        break;
      }
      case 17:
        for (length = readBits(d, 3, 3); length; --length) {
          if (num >= total) throw new InflateError('code length repeat overflows');
          lengths[num++] = 0;
        }
        break;
      case 18:
        for (length = readBits(d, 7, 11); length; --length) {
          if (num >= total) throw new InflateError('code length repeat overflows');
          lengths[num++] = 0;
        }
        break;
      default:
        if (sym > 18) throw new InflateError('invalid code length symbol');
        lengths[num++] = sym;
    }
  }
  buildTree(lt, lengths, 0, hlit);
  buildTree(dt, lengths, hlit, hdist);
}

function ensure(d, extra) {
  const need = d.destLen + extra;
  if (need <= d.dest.length) return;
  let cap = Math.max(1024, d.dest.length * 2);
  while (cap < need) cap *= 2;
  const nd = new Uint8Array(cap);
  nd.set(d.dest.subarray(0, d.destLen));
  d.dest = nd;
}

function inflateBlockData(d, lt, dt) {
  for (;;) {
    let sym = decodeSymbol(d, lt);
    if (sym === 256) return;
    if (sym < 256) {
      if (d.destLen >= d.dest.length) ensure(d, 1);
      d.dest[d.destLen++] = sym;
    } else {
      sym -= 257;
      if (sym > 28) throw new InflateError('invalid length symbol');
      const length = readBits(d, lengthBits[sym], lengthBase[sym]);
      const dist = decodeSymbol(d, dt);
      if (dist > 29) throw new InflateError('invalid distance symbol');
      const start = d.destLen - readBits(d, distBits[dist], distBase[dist]);
      if (start < 0) throw new InflateError('distance too far back');
      ensure(d, length);
      const dest = d.dest;
      for (let i = start; i < start + length; ++i) dest[d.destLen++] = dest[i];
    }
  }
}

function inflateUncompressedBlock(d) {
  // Drop the partial byte, give back the whole bytes still held in the bit buffer.
  d.sourceIndex -= d.bitcount >> 3;
  d.bitcount = 0;
  d.tag = 0;
  const s = d.source;
  const i = d.sourceIndex;
  if (i + 4 > s.length) throw new InflateError('truncated stored block');
  const length = s[i] | (s[i + 1] << 8);
  const invlength = s[i + 2] | (s[i + 3] << 8);
  if (length !== (~invlength & 0xffff)) throw new InflateError('stored block length mismatch');
  d.sourceIndex += 4;
  if (d.sourceIndex + length > s.length) throw new InflateError('truncated stored block');
  ensure(d, length);
  d.dest.set(s.subarray(d.sourceIndex, d.sourceIndex + length), d.destLen);
  d.destLen += length;
  d.sourceIndex += length;
}

/**
 * Inflate a raw DEFLATE stream starting at `source[0]`.
 * Returns {out: Uint8Array, consumed: number of input bytes used}.
 */
export function inflateRawWithInfo(source, sizeHint = 0) {
  const src = source instanceof Uint8Array ? source : new Uint8Array(source);
  const d = new Data(src, new Uint8Array(Math.max(64, sizeHint | 0 || src.length * 4)));
  let bfinal;
  do {
    if (d.sourceIndex > src.length) throw new InflateError('unexpected end of deflate data');
    bfinal = getbit(d);
    const btype = readBits(d, 2, 0);
    switch (btype) {
      case 0:
        inflateUncompressedBlock(d);
        break;
      case 1:
        inflateBlockData(d, sltree, sdtree);
        break;
      case 2:
        decodeTrees(d, d.ltree, d.dtree);
        inflateBlockData(d, d.ltree, d.dtree);
        break;
      default:
        throw new InflateError('invalid block type');
    }
  } while (!bfinal);
  const consumed = d.sourceIndex - (d.bitcount >> 3);
  if (consumed > src.length) throw new InflateError('unexpected end of deflate data');
  const out = d.dest.length === d.destLen ? d.dest : d.dest.slice(0, d.destLen);
  return {out, consumed};
}

/** Inflate a raw DEFLATE stream. */
export function inflateRaw(source, sizeHint = 0) {
  return inflateRawWithInfo(source, sizeHint).out;
}

// ------------------------------------------------------------------ CRC32

const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();

/** CRC-32 (gzip polynomial) of `bytes`. */
export function crc32(bytes, crc = 0) {
  let c = (crc ^ 0xffffffff) >>> 0;
  for (let i = 0; i < bytes.length; i++) c = CRC_TABLE[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

// ------------------------------------------------------------------ gzip

const FHCRC = 2, FEXTRA = 4, FNAME = 8, FCOMMENT = 16;

/** Offset of the deflate data of the gzip member starting at `pos`. */
function gzipHeaderEnd(b, pos) {
  if (pos + 10 > b.length) throw new InflateError('truncated gzip header');
  if (b[pos] !== 0x1f || b[pos + 1] !== 0x8b) throw new InflateError('not a gzip member');
  if (b[pos + 2] !== 8) throw new InflateError('unsupported gzip compression method');
  const flg = b[pos + 3];
  let p = pos + 10;
  if (flg & FEXTRA) {
    if (p + 2 > b.length) throw new InflateError('truncated gzip header');
    p += 2 + (b[p] | (b[p + 1] << 8));
  }
  if (flg & FNAME) {
    while (p < b.length && b[p] !== 0) p++;
    p++;
  }
  if (flg & FCOMMENT) {
    while (p < b.length && b[p] !== 0) p++;
    p++;
  }
  if (flg & FHCRC) p += 2;
  if (p > b.length) throw new InflateError('truncated gzip header');
  return p;
}

function u32le(b, p) {
  return (b[p] | (b[p + 1] << 8) | (b[p + 2] << 16) | (b[p + 3] << 24)) >>> 0;
}

/**
 * Decode one gzip member at `pos`.
 * Returns {out, end} where `end` is the offset just past the member's 8-byte trailer.
 */
export function gunzipMember(bytes, pos = 0, {verify = true} = {}) {
  const b = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  const start = gzipHeaderEnd(b, pos);
  const {out, consumed} = inflateRawWithInfo(b.subarray(start));
  const tr = start + consumed;
  if (tr + 8 > b.length) throw new InflateError('truncated gzip trailer');
  if (verify) {
    const crc = u32le(b, tr);
    const isize = u32le(b, tr + 4);
    if (isize !== (out.length >>> 0)) throw new InflateError('gzip size mismatch');
    if (crc32(out) !== crc) throw new InflateError('gzip CRC mismatch');
  }
  return {out, end: tr + 8};
}

/** [start, end) byte ranges of the concatenated gzip members in `bytes`. */
export function gzipMembers(bytes) {
  const b = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  const ranges = [];
  let pos = 0;
  while (pos < b.length) {
    if (b.length - pos < 2 || b[pos] !== 0x1f || b[pos + 1] !== 0x8b) {
      // trailing zero padding is tolerated, anything else is an error
      for (let i = pos; i < b.length; i++) if (b[i] !== 0) throw new InflateError('garbage after gzip member');
      break;
    }
    const {end} = gunzipMember(b, pos, {verify: false});
    ranges.push([pos, end]);
    pos = end;
  }
  return ranges;
}

/**
 * Synchronous gunzip. Concatenated members are decoded and joined (as `gzip -d` does).
 * Input without the gzip magic is returned unchanged.
 */
export function gunzipSync(bytes, {verify = true} = {}) {
  const b = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  if (b.length < 2 || b[0] !== 0x1f || b[1] !== 0x8b) return b;
  const parts = [];
  let total = 0;
  let pos = 0;
  while (pos < b.length) {
    if (b.length - pos < 2 || b[pos] !== 0x1f || b[pos + 1] !== 0x8b) {
      for (let i = pos; i < b.length; i++) if (b[i] !== 0) throw new InflateError('garbage after gzip member');
      break;
    }
    const {out, end} = gunzipMember(b, pos, {verify});
    parts.push(out);
    total += out.length;
    pos = end;
  }
  if (parts.length === 1) return parts[0];
  const res = new Uint8Array(total);
  let o = 0;
  for (const p of parts) {
    res.set(p, o);
    o += p.length;
  }
  return res;
}
