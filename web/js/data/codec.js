// Column / position / string / sparse / density decoders (ARCHITECTURE.md sections 5.3 and 5.9).
// Pure module: no DOM. Python twin: portalkit/bundle/codec.py.

import {gunzipSync, gunzipMember} from './inflate.js';

const QMAX = 65534;
const QMAX_U8 = 254;
const U32_MISSING = 0xffffffff;
const U32LIN_MAX = 4294967294;
const I16_MISSING = -32768;
export const CAT8_MISSING = 255;
export const CAT16_MISSING = 65535;

export const LIMIT_UPPER = 1;
export const LIMIT_LOWER = 2;
export const LIMIT_CLIP_LOW = 3;
export const LIMIT_CLIP_HIGH = 4;
export const LIMIT_FLAGGED = 5;

const LITTLE = new Uint8Array(new Uint16Array([1]).buffer)[0] === 1;
const utf8 = new TextDecoder('utf-8');

let useDecompressionStream = true;

/** Tests: force the inflate.js fallback (false) or allow DecompressionStream (true). */
export function setUseDecompressionStream(on) {
  useDecompressionStream = !!on;
}

export function asU8(b) {
  if (b instanceof Uint8Array) return b;
  if (b instanceof ArrayBuffer) return new Uint8Array(b);
  if (ArrayBuffer.isView(b)) return new Uint8Array(b.buffer, b.byteOffset, b.byteLength);
  throw new TypeError('expected bytes (Uint8Array / ArrayBuffer)');
}

/** True when `b` starts with the gzip magic 1f 8b (GC loader_lite.isGzip). */
export function isGzip(b) {
  return !!b && b.length >= 2 && b[0] === 0x1f && b[1] === 0x8b;
}

/** Synchronous inflate when gzip, identity otherwise (uses inflate.js). */
export function maybeGunzipSync(b) {
  const u = asU8(b);
  return isGzip(u) ? gunzipSync(u) : u;
}

async function streamGunzip(u) {
  const ds = new DecompressionStream('gzip');
  const writer = ds.writable.getWriter();
  // The write/close chain rejects together with the reader when the stream errors (corrupt or
  // truncated input). Settle it into a value right away so that rejection is always consumed;
  // the reader's error is the one that propagates.
  const done = writer.write(u).then(() => writer.close()).then(
    () => null,
    (err) => ({err}),
  );
  const reader = ds.readable.getReader();
  const parts = [];
  let total = 0;
  for (;;) {
    const {value, done: end} = await reader.read();
    if (end) break;
    parts.push(value);
    total += value.length;
  }
  const w = await done;
  if (w) throw w.err;
  if (parts.length === 1) return parts[0];
  const out = new Uint8Array(total);
  let o = 0;
  for (const p of parts) {
    out.set(p, o);
    o += p.length;
  }
  return out;
}

/**
 * Inflate one gzip stream (DecompressionStream('gzip') when present, inflate.js otherwise).
 * Bytes without the gzip magic are returned as they are (the server may already have decoded them).
 * Concatenated members (record shards) are handled by the fallback; callers decode shard slices
 * member by member.
 */
export async function gunzip(b) {
  const u = asU8(b);
  if (!isGzip(u)) return u;
  if (useDecompressionStream && typeof DecompressionStream === 'function') {
    try {
      return await streamGunzip(u);
    } catch (err) {
      // Engines differ on trailing data after the first member; inflate.js handles it.
      if (!(err instanceof TypeError)) throw err;
    }
  }
  return gunzipSync(u);
}

/** Inflate the single gzip member at bytes [off, off+len) synchronously. */
export function gunzipSlice(b, off, len) {
  const u = asU8(b).subarray(off, off + len);
  return gunzipMember(u, 0).out;
}

/** Inverse of the `width`-plane byte shuffle: out[i*w+p] = in[p*N+i]. */
export function unshuffle(b, width) {
  const u = asU8(b);
  if (width === 1) return u.slice();
  if (width !== 2 && width !== 4) throw new RangeError(`unshuffle width must be 2 or 4, got ${width}`);
  if (u.length % width) throw new RangeError(`shuffled length ${u.length} not a multiple of ${width}`);
  const n = u.length / width;
  const out = new Uint8Array(u.length);
  for (let p = 0; p < width; p++) {
    const base = p * n;
    for (let i = 0; i < n; i++) out[i * width + p] = u[base + i];
  }
  return out;
}

// ------------------------------------------------------------------ typed readers

function readU16(u, shuffled) {
  if (u.length % 2) throw new RangeError('u16 payload has odd length');
  const n = u.length >> 1;
  const out = new Uint16Array(n);
  if (shuffled) {
    for (let i = 0; i < n; i++) out[i] = u[i] | (u[n + i] << 8);
  } else {
    for (let i = 0; i < n; i++) out[i] = u[2 * i] | (u[2 * i + 1] << 8);
  }
  return out;
}

function readI16(u, shuffled) {
  const c = readU16(u, shuffled);
  return new Int16Array(c.buffer);
}

function read4(u, shuffled, Ctor) {
  if (u.length % 4) throw new RangeError('4-byte payload length not a multiple of 4');
  const plain = shuffled ? unshuffle(u, 4) : u;
  const n = plain.length >> 2;
  if (LITTLE) {
    const buf = new ArrayBuffer(n * 4);
    new Uint8Array(buf).set(plain.subarray(0, n * 4));
    return new Ctor(buf);
  }
  const dv = new DataView(plain.buffer, plain.byteOffset, plain.byteLength);
  const out = new Ctor(n);
  const f = Ctor === Float32Array;
  for (let i = 0; i < n; i++) out[i] = f ? dv.getFloat32(4 * i, true) : dv.getUint32(4 * i, true);
  return out;
}

export function readF32LE(u, off, n) {
  const dv = new DataView(u.buffer, u.byteOffset + off, n * 4);
  const out = new Float32Array(n);
  for (let i = 0; i < n; i++) out[i] = dv.getFloat32(4 * i, true);
  return out;
}

export function readF64LE(u, off, n) {
  const dv = new DataView(u.buffer, u.byteOffset + off, n * 8);
  const out = new Float64Array(n);
  for (let i = 0; i < n; i++) out[i] = dv.getFloat64(8 * i, true);
  return out;
}

export function readU16LE(u, off, n) {
  const out = new Uint16Array(n);
  for (let i = 0; i < n; i++) out[i] = u[off + 2 * i] | (u[off + 2 * i + 1] << 8);
  return out;
}

function u32at(u, p) {
  return (u[p] | (u[p + 1] << 8) | (u[p + 2] << 16) | (u[p + 3] << 24)) >>> 0;
}

// ------------------------------------------------------------------ quantization

/** Quantization step, derived when absent (IMPLEMENTATION_NOTES item 9). */
export function stepOf(meta) {
  const s = meta.step;
  if (typeof s === 'number' && Number.isFinite(s)) return s;
  const lo = +meta.lo, hi = +meta.hi;
  return (hi - lo) / (meta.enc === 'u8q' ? QMAX_U8 : QMAX);
}

/** Decoded value of one code (NaN for missing). Category codes return the level index. */
export function dequant(meta, code) {
  const enc = meta.enc;
  switch (enc) {
    case 'u16q':
    case 'u8q':
      return code === 0 ? NaN : +meta.lo + (code - 1) * stepOf(meta);
    case 'u16log':
      return code === 0 ? NaN : Math.pow(10, +meta.lo + (code - 1) * stepOf(meta));
    case 'u8log': {
      if (code === 0) return NaN;
      const a = Math.log10(+meta.lo), b = Math.log10(+meta.hi);
      return Math.pow(10, a + ((code - 1) / QMAX_U8) * (b - a));
    }
    case 'u32':
      return code === U32_MISSING ? NaN : code;
    case 'u32lin':
      return code === U32_MISSING ? NaN : +meta.lo + (code * (+meta.hi - +meta.lo)) / U32LIN_MAX;
    case 'cat8':
      return code === CAT8_MISSING ? NaN : code;
    case 'cat16':
      return code === CAT16_MISSING ? NaN : code;
    case 'f32':
    case 'bits':
      return code;
    default:
      throw new Error(`dequant: unsupported encoding ${enc}`);
  }
}

/** Float32 values for a code array (fast paths for the quantized encodings). */
export function dequantArray(meta, codes) {
  const n = codes.length;
  const out = new Float32Array(n);
  const enc = meta.enc;
  if (enc === 'u16q' || enc === 'u8q' || enc === 'u16log') {
    const lo = +meta.lo, step = stepOf(meta);
    const log = enc === 'u16log';
    for (let i = 0; i < n; i++) {
      const c = codes[i];
      if (c === 0) out[i] = NaN;
      else {
        const v = lo + (c - 1) * step;
        out[i] = log ? Math.pow(10, v) : v;
      }
    }
    return out;
  }
  if (enc === 'u8log') {
    const a = Math.log10(+meta.lo), b = Math.log10(+meta.hi);
    const lut = new Float32Array(256);
    lut[0] = NaN;
    for (let c = 1; c < 256; c++) lut[c] = Math.pow(10, a + ((c - 1) / QMAX_U8) * (b - a));
    for (let i = 0; i < n; i++) out[i] = lut[codes[i]];
    return out;
  }
  for (let i = 0; i < n; i++) out[i] = dequant(meta, codes[i]);
  return out;
}

// ------------------------------------------------------------------ str / sparse

/** `str` payload: u32 n, u32 offsets[n+1], UTF-8 blob. */
export function decodeStr(bytes) {
  const u = maybeGunzipSync(bytes);
  if (u.length < 4) throw new RangeError('str payload too short');
  const n = u32at(u, 0);
  const base = 4 + 4 * (n + 1);
  if (base > u.length) throw new RangeError('str payload truncated');
  const offsets = new Uint32Array(n + 1);
  for (let i = 0; i <= n; i++) offsets[i] = u32at(u, 4 + 4 * i);
  const blob = u.subarray(base);
  if (offsets[n] > blob.length) throw new RangeError('str blob truncated');
  return {
    n,
    offsets,
    blob,
    get(i) {
      if (!(i >= 0 && i < n)) return '';
      return utf8.decode(blob.subarray(offsets[i], offsets[i + 1]));
    },
    all() {
      const out = new Array(n);
      for (let i = 0; i < n; i++) out[i] = utf8.decode(blob.subarray(offsets[i], offsets[i + 1]));
      return out;
    },
  };
}

/** `sparse` payload: u32 m, u32 idx[m] ascending, u8 kind[m]. */
export function decodeSparse(bytes) {
  const u = maybeGunzipSync(bytes);
  if (u.length < 4) throw new RangeError('sparse payload too short');
  const m = u32at(u, 0);
  if (4 + 5 * m > u.length) throw new RangeError('sparse payload truncated');
  const idx = new Uint32Array(m);
  for (let i = 0; i < m; i++) idx[i] = u32at(u, 4 + 4 * i);
  const kind = u.slice(4 + 4 * m, 4 + 5 * m);
  return {idx, kind};
}

/** Kind of row `i` in a decoded sparse table (0 when absent). Binary search. */
export function sparseKind(sp, i) {
  const idx = sp.idx;
  let lo = 0, hi = idx.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const v = idx[mid];
    if (v === i) return sp.kind[mid];
    if (v < i) lo = mid + 1;
    else hi = mid - 1;
  }
  return 0;
}

// ------------------------------------------------------------------ columns

/**
 * Decode one column (or chunk) file. `bytes` may be gzip (inflated synchronously with inflate.js;
 * workers normally pass bytes already inflated by `gunzip`). `n` is needed for `bits` (defaults to
 * meta.n, then 8 * bytes).
 *
 * Returns, by encoding:
 *   u16q/u16log/u8q/u8log: {codes, values}
 *   cat8/cat16:            {codes, values (level index, NaN missing), levels, missing}
 *   bits:                  {bits (0/1 per row), values}
 *   f32:                   {values}
 *   u32/u32lin:            {codes, values}
 *   str:                   {strings, str}
 *   sparse:                {idx, kind}
 */
export function decodeColumn(meta, bytes, n) {
  const u = maybeGunzipSync(bytes);
  const enc = meta.enc;
  const shuffled = !!meta.shuffle;
  switch (enc) {
    case 'u16q':
    case 'u16log': {
      const codes = readU16(u, shuffled);
      return {codes, values: dequantArray(meta, codes)};
    }
    case 'u8q':
    case 'u8log': {
      const codes = u.slice();
      return {codes, values: dequantArray(meta, codes)};
    }
    case 'cat8':
    case 'cat16': {
      const codes = enc === 'cat8' ? u.slice() : readU16(u, shuffled);
      const missing = enc === 'cat8' ? CAT8_MISSING : CAT16_MISSING;
      const values = new Float32Array(codes.length);
      for (let i = 0; i < codes.length; i++) values[i] = codes[i] === missing ? NaN : codes[i];
      return {codes, values, levels: meta.levels || [], missing};
    }
    case 'bits': {
      const nn = n ?? meta.n ?? u.length * 8;
      if (nn > u.length * 8) throw new RangeError(`bits payload holds ${u.length * 8} rows, need ${nn}`);
      const bits = new Uint8Array(nn);
      const values = new Float32Array(nn);
      for (let i = 0; i < nn; i++) {
        const b = (u[i >> 3] >> (i & 7)) & 1;
        bits[i] = b;
        values[i] = b;
      }
      return {bits, values};
    }
    case 'f32':
      return {values: read4(u, shuffled, Float32Array)};
    case 'u32':
    case 'u32lin': {
      const codes = read4(u, shuffled, Uint32Array);
      return {codes, values: dequantArray(meta, codes)};
    }
    case 'str': {
      const str = decodeStr(u);
      return {strings: str.all(), str};
    }
    case 'sparse':
      return decodeSparse(u);
    default:
      throw new Error(`decodeColumn: unsupported encoding ${enc}`);
  }
}

// ------------------------------------------------------------------ positions

/**
 * Decode catalog positions. `files` maps roles to (gz or inflated) bytes:
 * u32lin {ra, dec} (needs bbox: third argument or posMeta.bbox, IMPLEMENTATION_NOTES item 5),
 * i16off {tile, dra, ddec}. Missing rows are NaN.
 */
export function decodePos(posMeta, files, bbox) {
  const enc = posMeta.enc;
  if (enc === 'u32lin') {
    const bb = bbox || posMeta.bbox;
    if (!bb) throw new Error('u32lin positions need the catalog bbox');
    const out = {};
    for (const k of ['ra', 'dec']) {
      const u = maybeGunzipSync(files[k]);
      const codes = read4(u, posMeta.shuffle !== false, Uint32Array);
      const lo = +bb[k][0], hi = +bb[k][1];
      const sc = (hi - lo) / U32LIN_MAX;
      const v = new Float64Array(codes.length);
      for (let i = 0; i < codes.length; i++) v[i] = codes[i] === U32_MISSING ? NaN : lo + codes[i] * sc;
      out[k] = v;
    }
    if (out.ra.length !== out.dec.length) throw new Error('ra/dec lengths differ');
    // Each file decodes on its own (as the Python twin does); the writer already blanks both
    // coordinates of a row that has only one finite value.
    return out;
  }
  if (enc === 'i16off') {
    const tile = readU16(maybeGunzipSync(files.tile), true);
    const dra = readI16(maybeGunzipSync(files.dra), true);
    const ddec = readI16(maybeGunzipSync(files.ddec), true);
    const n = tile.length;
    if (dra.length !== n || ddec.length !== n) throw new Error('i16off file lengths differ');
    const refs = posMeta.tile_refs || [];
    const s = +posMeta.step_mas / 3.6e6;
    const ra = new Float64Array(n);
    const dec = new Float64Array(n);
    const nr = refs.length;
    const ra0s = new Float64Array(nr), d0s = new Float64Array(nr), sec = new Float64Array(nr);
    for (let t = 0; t < nr; t++) {
      ra0s[t] = +refs[t][0];
      d0s[t] = +refs[t][1];
      sec[t] = s / Math.cos((d0s[t] * Math.PI) / 180);
    }
    for (let i = 0; i < n; i++) {
      if (nr === 0 || dra[i] === I16_MISSING || ddec[i] === I16_MISSING) {
        ra[i] = NaN;
        dec[i] = NaN;
        continue;
      }
      const t = tile[i] < nr ? tile[i] : nr - 1;
      ra[i] = ra0s[t] + dra[i] * sec[t];
      dec[i] = d0s[t] + ddec[i] * s;
    }
    return {ra, dec};
  }
  throw new Error(`decodePos: unknown position encoding ${enc}`);
}

// ------------------------------------------------------------------ density (section 5.9)

/**
 * Decode a pre-binned density grid. Count grids (u16log / u16q, 2-plane shuffled) give counts
 * (0 for empty cells); binned-statistic grids (f32) give values with NaN for empty cells.
 * Returns {nx, ny, xr, yr, values: Float32Array (cell iy*nx+ix, iy = 0 at yr[0]), codes?, stat?}.
 */
export function decodeDensity(meta, bytes) {
  const u = maybeGunzipSync(bytes);
  const nx = meta.nx | 0, ny = meta.ny | 0;
  const out = {nx, ny, xr: meta.xr, yr: meta.yr, key: meta.key, x: meta.x, y: meta.y};
  if (meta.enc === 'f32') {
    out.values = read4(u, meta.shuffle !== false, Float32Array);
    out.stat = meta.stat;
  } else if (meta.enc === 'u16log' || meta.enc === 'u16q') {
    const codes = readU16(u, meta.shuffle !== false);
    const v = dequantArray(meta, codes);
    for (let i = 0; i < v.length; i++) if (codes[i] === 0) v[i] = 0;
    out.codes = codes;
    out.values = v;
  } else {
    throw new Error(`decodeDensity: unsupported encoding ${meta.enc}`);
  }
  if (out.values.length !== nx * ny) throw new Error(`density grid has ${out.values.length} cells, expected ${nx * ny}`);
  return out;
}

/** Exact double count of one density code (u16log semantics; 0 = empty). */
export function densityCount(meta, code) {
  if (code === 0) return 0;
  return dequant(meta, code);
}
