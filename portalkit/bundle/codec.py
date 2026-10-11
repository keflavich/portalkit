"""Binary codecs for the portalkit data bundle (ARCHITECTURE.md sections 5.1 and 5.3).

Contents
--------
* :func:`gz` -- GC ``gz()`` verbatim from ``GCTreasury_Portal_Manifold.py`` (level 9, mtime 0,
  OS byte ``\\xff``; ``filtered=True`` uses ``Z_FILTERED``, wbits 31 and OS byte ``\\x03``).
* :func:`shuffle_u16` / :func:`shuffle_i16` (GC verbatim, 2 byte planes), :func:`shuffle4`
  (4 byte planes for f32/u32/u32lin) and :func:`unshuffle`.
* :func:`quantize` / :func:`log_quantize` / :func:`dim_stats` / :func:`log_dim_stats` (GC
  verbatim) plus :func:`col_stats`, the section 5.2 wrapper (empty columns, scale 0 -> 1.0).
* Encoders for every section 5.3 encoding: ``u16q, u16log, u8q, u8log, cat8, cat16, bits, f32,
  u32, u32lin, i16off, str, sparse``. Each numeric encoder returns an :class:`Encoded`
  holding the per-row codes, the manifest metadata and the clip-limit kinds.
* Python decoders (:func:`decode_file`, :func:`dequant`, :func:`decode_str`,
  :func:`decode_sparse`, :func:`decode_pos`) used by tests and parity checks; they mirror
  ``web/js/data/codec.js``.
* Catalog chunk geometry (:func:`catalog_chunk_starts`, :func:`chunk_ranges`; manifest
  ``chunk_starts``, IMPLEMENTATION_NOTES 98) and whole-column readers (:func:`read_column`,
  :func:`read_ids`), mirroring ``web/js/workers/data.worker.js``.

Every multi-byte array is little-endian. A column file is ``gz(payload)``; when the payload is
byte-shuffled the gzip stream uses ``filtered=True``.
"""

from __future__ import annotations

import gzip
import math
import struct
import zlib
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import numpy as np

from ..constants import CHUNK_ROWS

__all__ = [
    "QMAX", "QMAX_U8", "U32_MISSING", "U32LIN_MAX", "I16_MISSING", "CAT8_MISSING", "CAT16_MISSING",
    "LIMIT_UPPER", "LIMIT_LOWER", "LIMIT_CLIP_LOW", "LIMIT_CLIP_HIGH", "LIMIT_FLAGGED",
    "SHUFFLED_ENCS", "SHUFFLE_WIDTH",
    "gz", "gunzip", "gzip_members",
    "shuffle_u16", "shuffle_i16", "shuffle4", "unshuffle",
    "quantize", "log_quantize", "dim_stats", "log_dim_stats", "col_stats",
    "Encoded", "encode_u16q", "encode_u16log", "encode_u8q", "encode_u8log", "encode_cat",
    "encode_bits", "encode_f32", "encode_u32", "encode_u32lin", "encode_i16off",
    "encode_str", "decode_str", "encode_sparse", "decode_sparse",
    "pack_codes", "file_bytes", "decode_file", "dequant", "decode_pos", "morton16",
    "to_float_array", "is_missing_obj",
    "DEFAULT_CHUNK_ROWS", "catalog_chunk_starts", "chunk_ranges", "column_files", "read_column", "read_ids",
]

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

QMAX = 65534                 # galaxy-manifold's quantize() ceiling
QMAX_U8 = 254                # 1..255, 0 = missing
U32_MISSING = 0xFFFFFFFF
U32LIN_MAX = 4294967294
I16_MISSING = -32768
CAT8_MISSING = 255
CAT16_MISSING = 65535

# sparse limit kinds (section 5.3)
LIMIT_UPPER = 1
LIMIT_LOWER = 2
LIMIT_CLIP_LOW = 3
LIMIT_CLIP_HIGH = 4
LIMIT_FLAGGED = 5

#: byte-plane width of the shuffle used by each encoding when ``shuffle`` is true
SHUFFLE_WIDTH = {"u16q": 2, "u16log": 2, "cat16": 2, "i16": 2, "u16": 2,
                 "f32": 4, "u32": 4, "u32lin": 4}
#: encodings written shuffled by default
SHUFFLED_ENCS = frozenset(SHUFFLE_WIDTH)


# ---------------------------------------------------------------------------
# GC verbatim helpers (GCTreasury_Portal_Manifold.py)
# ---------------------------------------------------------------------------

def gz(data: bytes, filtered: bool = False) -> bytes:
    """Byte-identical to galaxy-manifold's pipeline/export_web.py:gz (same OS-byte pinning)."""
    if not filtered:
        out, os_byte = gzip.compress(data, compresslevel=9, mtime=0), b'\xff'
    else:
        c = zlib.compressobj(level=9, method=zlib.DEFLATED, wbits=31, memLevel=9,
                              strategy=zlib.Z_FILTERED)
        out, os_byte = c.compress(data) + c.flush(), b'\x03'
    return out[:9] + os_byte + out[10:]


def quantize(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """galaxy-manifold pipeline/export_web.py:quantize, verbatim. 0 = missing, else 1..65535
    linear across [lo, hi]."""
    x = np.asarray(x, dtype=np.float64)
    ok = np.isfinite(x) & (x >= lo) & (x <= hi)
    v = np.zeros(len(x), dtype=np.uint16)
    q = 1.0 + np.rint((x[ok] - lo) / (hi - lo) * QMAX)
    v[ok] = np.clip(q, 1, 65535).astype(np.uint16)
    return v


def shuffle_u16(v: np.ndarray) -> bytes:
    """galaxy-manifold pipeline/export_web.py:shuffle_u16, verbatim (byte-plane split helps
    gzip on weakly-correlated columns)."""
    v = v.astype('<u2', copy=False)
    return (v & 0xFF).astype(np.uint8).tobytes() + (v >> 8).astype(np.uint8).tobytes()


def shuffle_i16(v: np.ndarray) -> bytes:
    """Same byte-plane trick for signed int16 (ra/dec offsets)."""
    v = v.astype('<i2', copy=False)
    u = v.view('<u2')
    return (u & 0xFF).astype(np.uint8).tobytes() + (u >> 8).astype(np.uint8).tobytes()


def dim_stats(x: np.ndarray) -> dict:
    """galaxy-manifold pipeline/export_web.py:dim_stats, verbatim (median/MAD center+scale
    used by the renderer's shader-side A/B decode into standardized u-space)."""
    v = x[np.isfinite(x)]
    center = float(np.median(v))
    mad = float(np.median(np.abs(v - center)))
    scale = 1.4826 * mad
    fallback = False
    if not np.isfinite(scale) or scale < 1e-9 * max(1.0, abs(center)):
        scale, fallback = float(np.std(v)), True
    qs = np.quantile(v, np.linspace(0.0, 1.0, 101))
    return {'center': center, 'scale': scale, 'nvalid': int(v.size),
            'quantiles': [float(q) for q in qs], '_scale_fallback_std': fallback}


def log_quantize(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """1-byte analog of quantize(), for values spanning large dynamic range (e.g. emag_ab_*:
    ~1e-4 to ~1e3) where a linear u16 scale would waste nearly all codes on the faint end.
    0 = missing/non-positive, else 1..255 linear in log10-space across [log10(lo), log10(hi)].
    Only positive, finite x are quantizable (log10 undefined otherwise)."""
    x = np.asarray(x, dtype=np.float64)
    lo_l, hi_l = np.log10(lo), np.log10(hi)
    ok = np.isfinite(x) & (x > 0)
    lx = np.full(len(x), -np.inf)
    lx[ok] = np.log10(x[ok])
    ok &= (lx >= lo_l) & (lx <= hi_l)
    v = np.zeros(len(x), dtype=np.uint8)
    q = 1.0 + np.rint((lx[ok] - lo_l) / (hi_l - lo_l) * QMAX_U8)
    v[ok] = np.clip(q, 1, 255).astype(np.uint8)
    return v


def log_dim_stats(x: np.ndarray, lo_pct: float = 0.1, hi_pct: float = 99.9) -> dict:
    """Percentile-derived [lo, hi] (log-space quantize range) for a positive-valued column,
    plus the same quantile table shape as dim_stats() (in linear, not log, units) so the JS
    side can decode without needing its own percentile math."""
    v = x[np.isfinite(x) & (x > 0)]
    lo, hi = float(np.quantile(v, lo_pct / 100)), float(np.quantile(v, hi_pct / 100))
    qs = np.quantile(v, np.linspace(0.0, 1.0, 101))
    return {'lo': lo, 'hi': hi, 'nvalid': int(v.size), 'quantiles': [float(q) for q in qs]}


# ---------------------------------------------------------------------------
# gzip helpers
# ---------------------------------------------------------------------------

def gunzip(data: bytes) -> bytes:
    """Inflate one or more concatenated gzip members."""
    return gzip.decompress(data)


def gzip_members(data: bytes) -> list[tuple[int, int]]:
    """``[(offset, length), ...]`` of the independent gzip members concatenated in ``data``."""
    out = []
    off = 0
    while off < len(data):
        d = zlib.decompressobj(wbits=31)
        d.decompress(data[off:])
        if not d.eof:
            raise ValueError(f"truncated gzip member at offset {off}")
        ln = len(data) - off - len(d.unused_data)
        out.append((off, ln))
        off += ln
    return out


# ---------------------------------------------------------------------------
# shuffles
# ---------------------------------------------------------------------------

def shuffle4(v: np.ndarray) -> bytes:
    """4-plane byte shuffle of a 4-byte little-endian array (f32/u32/u32lin)."""
    a = np.ascontiguousarray(v)
    if a.dtype.itemsize != 4:
        raise ValueError("shuffle4 needs a 4-byte dtype")
    a = a.astype(a.dtype.newbyteorder("<"), copy=False)
    b = a.view(np.uint8).reshape(-1, 4)
    return b.T.copy().tobytes()


def unshuffle(b: bytes, width: int) -> bytes:
    """Inverse of the ``width``-plane byte shuffle (``width`` in {1, 2, 4})."""
    if width == 1:
        return bytes(b)
    a = np.frombuffer(b, dtype=np.uint8)
    if a.size % width:
        raise ValueError(f"shuffled length {a.size} not a multiple of {width}")
    return a.reshape(width, -1).T.copy().tobytes()


# ---------------------------------------------------------------------------
# input normalisation
# ---------------------------------------------------------------------------

def is_missing_obj(v: Any) -> bool:
    """True for None, numpy masked, NaN floats and empty strings."""
    if v is None or v is np.ma.masked:
        return True
    if isinstance(v, (float, np.floating)):
        return bool(np.isnan(v))
    if isinstance(v, (str, bytes)):
        return len(v) == 0
    return False


def to_float_array(x: Any) -> np.ndarray:
    """float64 array with masked / None / non-numeric entries as NaN."""
    if isinstance(x, np.ma.MaskedArray):
        if x.dtype.kind in "fiub":
            return np.ma.filled(x.astype(np.float64), np.nan)
        x = [None if m else v for v, m in zip(x.data, np.ma.getmaskarray(x))]
    a = np.asarray(x)
    if a.dtype.kind in "fiub":
        return a.astype(np.float64)
    out = np.full(len(a), np.nan)
    for i, v in enumerate(a):
        if v is None or v is np.ma.masked:
            continue
        try:
            out[i] = float(v)
        except (TypeError, ValueError):
            out[i] = np.nan
    return out


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------

def col_stats(x: np.ndarray, log: bool = False) -> dict:
    """Section 5.2 column statistics: ``center`` (median), ``scale`` (1.4826 MAD, std
    fallback, 1.0 if both 0), ``nvalid`` and 101 ``quantiles`` (0..100 %).

    With ``log=True`` the statistics are computed on log10 of the positive values.
    Columns with no valid value return ``{"nvalid": 0, "center": 0.0, "scale": 1.0}`` and no
    quantiles.
    """
    x = np.asarray(x, dtype=np.float64)
    if log:
        with np.errstate(divide="ignore", invalid="ignore"):
            x = np.where(np.isfinite(x) & (x > 0), np.log10(np.where(x > 0, x, 1.0)), np.nan)
    v = x[np.isfinite(x)]
    if v.size == 0:
        return {"center": 0.0, "scale": 1.0, "nvalid": 0}
    st = dim_stats(v)
    scale = st["scale"]
    if not math.isfinite(scale) or scale <= 0:
        scale = 1.0
    return {"center": st["center"], "scale": scale, "nvalid": st["nvalid"],
            "quantiles": st["quantiles"]}


# ---------------------------------------------------------------------------
# Encoded container
# ---------------------------------------------------------------------------

@dataclass
class Encoded:
    """Result of a column encoder.

    ``codes`` holds one entry per row (bits unpacked, 0/1 uint8). ``meta`` holds the manifest
    fields (``enc``, ``lo``, ``hi``, ``step``, ``clip``, ``shuffle``, ``levels``).
    ``limits`` maps row index -> sparse kind (3 clipped-low, 4 clipped-high).
    """
    enc: str
    codes: np.ndarray
    meta: dict
    limits: dict[int, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def file_bytes(self, i0: int = 0, i1: int | None = None) -> bytes:
        """gz file bytes for rows ``[i0, i1)``."""
        return file_bytes(self.enc, self.codes[i0:i1], self.meta.get("shuffle", False))


def _clean(v: float) -> float:
    """Round float noise from lo/hi computed as multiples of a step (12 significant digits)."""
    return float(f"{v:.12g}")


def _quant_bounds(xf: np.ndarray, finite: np.ndarray, lo: float | None, hi: float | None,
                  clip: str, warnings: list[str]) -> tuple[float, float, float, float]:
    """Return (data_lo, data_hi, clamp_lo, clamp_hi) for a quantized encoding."""
    if clip not in ("none", "saturate", "drop"):
        raise ValueError(f"unknown clip mode {clip!r}")
    v = xf[finite]
    if v.size:
        dmin, dmax = float(v.min()), float(v.max())
    else:
        dmin = dmax = float(lo if lo is not None else (hi if hi is not None else 0.0))
    if clip == "none":
        a = dmin if lo is None else min(lo, dmin)
        b = dmax if hi is None else max(hi, dmax)
        return a, b, -np.inf, np.inf
    if clip == "saturate":
        if v.size:
            pl, ph = np.percentile(v, [0.1, 99.9])
        else:
            pl = ph = dmin
        a = float(pl) if lo is None else float(lo)
        b = float(ph) if hi is None else float(hi)
    else:   # drop
        a = dmin if lo is None else float(lo)
        b = dmax if hi is None else float(hi)
    if a > b:
        raise ValueError(f"clip bounds lo={a} > hi={b}")
    return a, b, a, b


def _linear_codes(xf: np.ndarray, lo: float, hi: float, step: float | None, clip: str,
                  qmax: int, warnings: list[str], *, label: str = "",
                  keep_low: np.ndarray | None = None) -> tuple[np.ndarray, dict, dict[int, int]]:
    """Shared u16q/u8q quantizer. ``qmax`` is 65534 (u16) or 254 (u8).

    Returns (codes as int64, meta {lo, hi, step, clip}, limits {row: kind}).
    """
    finite = np.isfinite(xf)
    a, b, clo, chi = _quant_bounds(xf, finite, lo, hi, clip, warnings)
    x = xf.copy()
    limits: dict[int, int] = {}
    valid = finite.copy()
    if clip == "saturate":
        low = finite & (x < clo)
        high = finite & (x > chi)
        for i in np.flatnonzero(low):
            limits[int(i)] = LIMIT_CLIP_LOW
        for i in np.flatnonzero(high):
            limits[int(i)] = LIMIT_CLIP_HIGH
        x[low] = clo
        x[high] = chi
    elif clip == "drop":
        valid &= (x >= clo) & (x <= chi)
    if step is not None:
        step0 = float(step)
        if not (step0 > 0 and math.isfinite(step0)):
            raise ValueError(f"step must be positive, got {step}")
        mult = 1
        while True:
            st = _clean(step0 * mult)
            q_lo = _clean(math.floor(a / st + 1e-9) * st)
            if q_lo > a:
                q_lo = _clean(q_lo - st)
            if b - q_lo <= qmax * st * (1 + 1e-12):
                break
            mult = max(mult + 1, math.ceil((b - a) / (qmax * step0)))
        if st != step0:
            warnings.append(f"{label}: span {b - a:.6g} exceeds {qmax}*step={qmax * step0:.6g}; "
                            f"step widened to {st:.6g}")
        lo_q, step_q = q_lo, st
        hi_q = _clean(lo_q + qmax * step_q)
    else:
        lo_q, hi_q = float(a), float(b)
        if hi_q > lo_q:
            step_q = (hi_q - lo_q) / qmax
        else:
            step_q = 1e-6 * max(1.0, abs(lo_q))
    codes = np.zeros(len(x), dtype=np.int64)
    if valid.any():
        q = 1.0 + np.rint((x[valid] - lo_q) / step_q)
        codes[valid] = np.clip(q, 1, qmax + 1).astype(np.int64)
    if keep_low is not None:
        codes[keep_low] = 1
    meta = {"lo": lo_q, "hi": hi_q, "step": step_q, "clip": clip}
    return codes, meta, limits


# ---------------------------------------------------------------------------
# numeric encoders
# ---------------------------------------------------------------------------

def encode_u16q(x: Any, lo: float | None = None, hi: float | None = None, step: float | None = None,
                clip: str = "none", label: str = "") -> Encoded:
    """``u16q``: ``code = 1 + rint((x - lo) / step)``, 0 = missing.

    ``step = (hi - lo) / 65534`` unless a fixed ``step`` is given; then ``lo`` is rounded down to
    a multiple of ``step``, ``hi = lo + 65534 * step``, and the step is widened (with a warning)
    when the span does not fit.
    """
    xf = to_float_array(x)
    w: list[str] = []
    codes, meta, limits = _linear_codes(xf, lo, hi, step, clip, QMAX, w, label=label)
    meta = {"enc": "u16q", **meta, "shuffle": True}
    return Encoded("u16q", codes.astype(np.uint16), meta, limits, w)


def encode_u16log(x: Any, lo: float | None = None, hi: float | None = None, step: float | None = None,
                  clip: str = "none", label: str = "", keep: np.ndarray | None = None) -> Encoded:
    """``u16log``: ``u16q`` on ``log10(x)``; ``lo``/``hi``/``step`` are in log10 (dex).

    ``lo``/``hi`` arguments are given in linear units. ``x <= 0`` is missing, except for rows
    flagged in ``keep`` (rows with a limit kind), which get code 1 (the floor ``lo``).
    """
    xf = to_float_array(x)
    pos = np.isfinite(xf) & (xf > 0)
    lx = np.full(len(xf), np.nan)
    lx[pos] = np.log10(xf[pos])
    llo = None if lo is None else (math.log10(lo) if lo > 0 else None)
    lhi = None if hi is None else (math.log10(hi) if hi > 0 else None)
    keep_low = None
    if keep is not None:
        keep = np.asarray(keep, dtype=bool)
        keep_low = keep & np.isfinite(xf) & (xf <= 0)
    w: list[str] = []
    codes, meta, limits = _linear_codes(lx, llo, lhi, step, clip, QMAX, w, label=label,
                                        keep_low=keep_low)
    meta = {"enc": "u16log", **meta, "shuffle": True}
    return Encoded("u16log", codes.astype(np.uint16), meta, limits, w)


def encode_u8q(x: Any, lo: float | None = None, hi: float | None = None, step: float | None = None,
               clip: str = "none", label: str = "") -> Encoded:
    """``u8q``: ``step = (hi - lo) / 254``; ``code = 1 + rint((x - lo) / step)``, 0 = missing."""
    xf = to_float_array(x)
    w: list[str] = []
    codes, meta, limits = _linear_codes(xf, lo, hi, step, clip, QMAX_U8, w, label=label)
    meta = {"enc": "u8q", **meta, "shuffle": False}
    return Encoded("u8q", codes.astype(np.uint8), meta, limits, w)


def encode_u8log(x: Any, lo: float | None = None, hi: float | None = None, clip: str = "none",
                 label: str = "") -> Encoded:
    """``u8log``: GC ``log_quantize``; ``lo``/``hi`` linear and > 0.

    ``clip="none"`` uses the positive finite min/max, ``saturate`` clamps to the bounds
    (adapter values, else the GC ``log_dim_stats`` 0.1/99.9 % rule) and records kinds 3/4,
    ``drop`` is ``log_quantize`` verbatim (out-of-range values become missing).
    ``step`` is emitted in dex: ``(log10 hi - log10 lo) / 254``.
    """
    xf = to_float_array(x)
    pos = np.isfinite(xf) & (xf > 0)
    v = xf[pos]
    w: list[str] = []
    if clip not in ("none", "saturate", "drop"):
        raise ValueError(f"unknown clip mode {clip!r}")
    if lo is not None and lo <= 0:
        raise ValueError("u8log lo must be > 0")
    if hi is not None and hi <= 0:
        raise ValueError("u8log hi must be > 0")
    if v.size:
        dmin, dmax = float(v.min()), float(v.max())
    else:
        dmin = dmax = float(lo if lo is not None else (hi if hi is not None else 1.0))
    limits: dict[int, int] = {}
    x = xf.copy()
    if clip == "none":
        a = dmin if lo is None else min(lo, dmin)
        b = dmax if hi is None else max(hi, dmax)
    elif clip == "saturate":
        if v.size and (lo is None or hi is None):
            st = log_dim_stats(v)
            pl, ph = st["lo"], st["hi"]
        else:
            pl, ph = dmin, dmax
        a = float(pl if lo is None else lo)
        b = float(ph if hi is None else hi)
        low = pos & (x < a)
        high = pos & (x > b)
        for i in np.flatnonzero(low):
            limits[int(i)] = LIMIT_CLIP_LOW
        for i in np.flatnonzero(high):
            limits[int(i)] = LIMIT_CLIP_HIGH
        x[low] = a
        x[high] = b
    else:
        a = dmin if lo is None else float(lo)
        b = dmax if hi is None else float(hi)
    if a > b:
        raise ValueError(f"u8log lo={a} > hi={b}")
    if b > a:
        codes = log_quantize(x, a, b)
        step = (math.log10(b) - math.log10(a)) / QMAX_U8
    else:
        codes = np.zeros(len(x), dtype=np.uint8)
        codes[pos & (x == a)] = 1
        step = 1e-6
    meta = {"enc": "u8log", "lo": a, "hi": b, "step": step, "clip": clip, "shuffle": False}
    return Encoded("u8log", codes.astype(np.uint8), meta, limits, w)


def encode_f32(x: Any, shuffle: bool = True) -> Encoded:
    """``f32``: float32, NaN = missing; 4-plane byte shuffle when ``shuffle``."""
    xf = to_float_array(x)
    with np.errstate(over="ignore"):
        a = xf.astype(np.float32)
    w = []
    over = np.isfinite(xf) & ~np.isfinite(a)
    if over.any():
        w.append(f"{int(over.sum())} values overflow float32 and become +/-inf")
    return Encoded("f32", a, {"enc": "f32", "shuffle": bool(shuffle)}, {}, w)


def encode_u32(x: Any, shuffle: bool = True) -> Encoded:
    """``u32``: uint32, 0xFFFFFFFF = missing; out-of-range/negative/non-finite -> missing."""
    xf = to_float_array(x)
    ok = np.isfinite(xf) & (xf >= 0) & (xf <= U32LIN_MAX)
    w = []
    bad = np.isfinite(xf) & ~ok
    if bad.any():
        w.append(f"{int(bad.sum())} values outside [0, {U32LIN_MAX}] stored as missing")
    r = np.rint(xf[ok])
    if np.any(r != xf[ok]):
        w.append("non-integer values rounded")
    out = np.full(len(xf), U32_MISSING, dtype=np.uint32)
    out[ok] = r.astype(np.uint32)
    return Encoded("u32", out, {"enc": "u32", "shuffle": bool(shuffle)}, {}, w)


def encode_u32lin(x: Any, lo: float, hi: float, shuffle: bool = True) -> Encoded:
    """``u32lin``: ``code = rint((x - lo) / (hi - lo) * 4294967294)``, 0xFFFFFFFF = missing.

    Values outside ``[lo, hi]`` are clamped (``lo``/``hi`` come from the catalog bbox).
    """
    xf = to_float_array(x)
    ok = np.isfinite(xf)
    out = np.full(len(xf), U32_MISSING, dtype=np.uint32)
    if hi > lo:
        q = np.rint((np.clip(xf[ok], lo, hi) - lo) / (hi - lo) * U32LIN_MAX)
        out[ok] = np.clip(q, 0, U32LIN_MAX).astype(np.uint32)
    else:
        out[ok] = 0
    return Encoded("u32lin", out, {"enc": "u32lin", "lo": float(lo), "hi": float(hi),
                                   "shuffle": bool(shuffle)}, {}, [])


def encode_bits(x: Any) -> Encoded:
    """``bits``: 1 bit per row, LSB-first; missing is not representable (stored as 0)."""
    if isinstance(x, np.ma.MaskedArray):
        x = np.ma.filled(x.astype(np.float64) if x.dtype.kind in "fiub" else x, 0)
    a = np.asarray(x)
    if a.dtype.kind == "b":
        b = a.astype(np.uint8)
    elif a.dtype.kind in "fiu":
        af = a.astype(np.float64)
        b = (np.isfinite(af) & (af != 0)).astype(np.uint8)
    else:
        b = np.array([0 if is_missing_obj(v) else int(_truthy(v)) for v in a], dtype=np.uint8)
    return Encoded("bits", b, {"enc": "bits"}, {}, [])


def _truthy(v: Any) -> bool:
    if isinstance(v, (str, bytes)):
        s = v.decode() if isinstance(v, bytes) else v
        return s.strip().lower() in ("1", "true", "t", "yes", "y")
    return bool(v)


def _as_label(v: Any) -> str | None:
    if is_missing_obj(v):
        return None
    if isinstance(v, bytes):
        return v.decode("utf-8")
    if isinstance(v, (np.integer, int)) and not isinstance(v, bool):
        return str(int(v))
    if isinstance(v, (float, np.floating)):
        f = float(v)
        return str(int(f)) if f.is_integer() else repr(f)
    return str(v)


def encode_cat(x: Any, levels: Sequence[str] | None = None, enc: str | None = None,
               label: str = "") -> Encoded:
    """``cat8``/``cat16``: index into ``levels``; missing 255 / 65535.

    Without ``levels`` the sorted unique labels are used. Labels absent from a given
    ``levels`` list are appended (with a warning). ``enc`` defaults to ``cat8`` up to 255
    levels and ``cat16`` above.
    """
    if isinstance(x, np.ma.MaskedArray):
        mask = np.ma.getmaskarray(x)
        vals = [None if m else v for v, m in zip(x.data, mask)]
    else:
        vals = list(np.asarray(x, dtype=object))
    labs = [_as_label(v) for v in vals]
    w: list[str] = []
    if levels is None:
        lv = sorted({s for s in labs if s is not None})
    else:
        lv = [str(s) for s in levels]
        if len(set(lv)) != len(lv):
            raise ValueError(f"{label}: duplicate category levels")
        extra = sorted({s for s in labs if s is not None} - set(lv))
        if extra:
            w.append(f"{label}: {len(extra)} values not in levels appended: {extra[:5]}")
            lv = lv + extra
    if enc is None:
        enc = "cat8" if len(lv) <= 255 else "cat16"
    cap = 255 if enc == "cat8" else 65535
    if len(lv) > cap:
        raise ValueError(f"{label}: {len(lv)} levels exceed {enc} capacity {cap}")
    index = {s: i for i, s in enumerate(lv)}
    missing = CAT8_MISSING if enc == "cat8" else CAT16_MISSING
    codes = np.array([missing if s is None else index[s] for s in labs],
                     dtype=np.uint8 if enc == "cat8" else np.uint16)
    meta = {"enc": enc, "levels": lv, "shuffle": enc == "cat16"}
    return Encoded(enc, codes, meta, {}, w)


# ---------------------------------------------------------------------------
# positions: i16off
# ---------------------------------------------------------------------------

def _spread16(v: np.ndarray) -> np.ndarray:
    x = v.astype(np.uint32) & 0xFFFF
    x = (x | (x << 8)) & 0x00FF00FF
    x = (x | (x << 4)) & 0x0F0F0F0F
    x = (x | (x << 2)) & 0x33333333
    x = (x | (x << 1)) & 0x55555555
    return x


def morton16(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Interleave two uint16 arrays into a uint32 Morton (Z-order) key (x in the even bits)."""
    return (_spread16(np.asarray(x)) | (_spread16(np.asarray(y)) << 1)).astype(np.uint32)


I16OFF_MAX_TILES = 4096   # keeps manifest tile_refs near 100 kB (MANIFEST_MAX is 256 KiB)


def encode_i16off(ra: Any, dec: Any, step_mas: float = 8.0, tile_deg: float | None = None,
                  max_tiles: int = I16OFF_MAX_TILES) -> dict:
    """GC ``i16off`` positions.

    Rows are assigned to sky tiles (dec bands of ``tile_deg``, each split into
    ``max(1, ceil(360 cos(dec0) / tile_deg))`` RA cells). The tile reference ``[ra0, dec0]``
    is the cell centre rounded to 1e-6 deg; ``tile`` (u16) indexes ``tile_refs`` (only
    occupied tiles, sorted by (band, cell)).
    ``dra = rint(wrap(ra - ra0) cos(dec0) 3.6e6 / step_mas)``,
    ``ddec = rint((dec - dec0) 3.6e6 / step_mas)``; missing rows have ``dra = ddec = -32768``
    and ``tile = 0``.

    ``tile_deg`` defaults to ``0.125 * step_mas / 8`` (offsets then stay below 28200 counts).
    When more than ``max_tiles`` (default 4096, at most 65535) occupied tiles are needed,
    step and tile size are doubled until they fit; the inlined ``tile_refs`` then stay well
    inside the manifest budget. A wide-area catalog therefore gets a coarser ``step_mas``,
    and the returned ``warnings`` list says so.

    Returns ``{"tile", "dra", "ddec", "tile_refs", "step_mas", "tile_deg", "warnings"}``.
    """
    raf = to_float_array(ra)
    decf = to_float_array(dec)
    if raf.shape != decf.shape:
        raise ValueError("ra and dec lengths differ")
    if step_mas <= 0:
        raise ValueError("step_mas must be > 0")
    ok = np.isfinite(raf) & np.isfinite(decf) & (decf >= -90) & (decf <= 90)
    T = float(tile_deg) if tile_deg is not None else 0.125 * step_mas / 8.0
    if not T > 0:
        raise ValueError("tile_deg must be > 0")
    max_tiles = int(min(max(int(max_tiles), 1), 65535))
    step_req = float(step_mas)
    # offsets are bounded by T/2 per axis; keep them within +/-30000 counts
    step_mas = max(float(step_mas), T * 3.6e6 / 60000.0)
    while True:
        ra_w = np.mod(raf[ok], 360.0)
        de = decf[ok]
        nband = max(1, int(math.ceil(180.0 / T)))
        band = np.clip(np.floor((de + 90.0) / T).astype(np.int64), 0, nband - 1)
        dec0_band = -90.0 + (band + 0.5) * T
        dec0_band = np.clip(dec0_band, -90.0, 90.0)
        cosd = np.cos(np.radians(dec0_band))
        nra = np.maximum(1, np.ceil(360.0 * cosd / T)).astype(np.int64)
        width = 360.0 / nra
        cell = np.minimum(np.floor(ra_w / width).astype(np.int64), nra - 1)
        key = band * (1 << 32) + cell
        ukeys, inv = np.unique(key, return_inverse=True)
        if len(ukeys) <= max_tiles:
            break
        step_mas *= 2.0
        T *= 2.0
    tile_refs: list[list[float]] = []
    if len(ukeys):
        ub = ukeys >> 32
        uc = ukeys & 0xFFFFFFFF
        ub_dec0 = np.clip(-90.0 + (ub + 0.5) * T, -90.0, 90.0)
        ub_nra = np.maximum(1, np.ceil(360.0 * np.cos(np.radians(ub_dec0)) / T)).astype(np.int64)
        ub_ra0 = (uc + 0.5) * (360.0 / ub_nra)
        tile_refs = [[round(float(r), 6), round(float(d), 6)] for r, d in zip(ub_ra0, ub_dec0)]
    n = len(raf)
    tile = np.zeros(n, dtype=np.uint16)
    dra = np.full(n, I16_MISSING, dtype=np.int16)
    ddec = np.full(n, I16_MISSING, dtype=np.int16)
    if len(tile_refs):
        refs = np.array(tile_refs, dtype=np.float64)
        ra0 = refs[inv, 0]
        d0 = refs[inv, 1]
        dra_f = (np.mod(ra_w - ra0 + 180.0, 360.0) - 180.0) * np.cos(np.radians(d0)) * 3.6e6 / step_mas
        ddec_f = (de - d0) * 3.6e6 / step_mas
        a = np.rint(dra_f)
        b = np.rint(ddec_f)
        if np.any(np.abs(a) > 32767) or np.any(np.abs(b) > 32767):
            raise ValueError("i16off offset exceeds int16 range; reduce tile_deg or raise step_mas")
        tile[ok] = inv.astype(np.uint16)
        dra[ok] = a.astype(np.int16)
        ddec[ok] = b.astype(np.int16)
    warns: list[str] = []
    if step_mas > step_req:
        warns.append(f"i16off step_mas raised from {step_req:g} to {step_mas:g} mas "
                     f"(tile {T:g} deg) to keep {len(tile_refs)} tile_refs within "
                     f"{max_tiles} tiles")
    return {"tile": tile, "dra": dra, "ddec": ddec, "tile_refs": tile_refs,
            "step_mas": float(step_mas), "tile_deg": T, "warnings": warns}


def decode_pos(pos_meta: dict, files: dict[str, bytes], bbox: dict | None = None
               ) -> tuple[np.ndarray, np.ndarray]:
    """Python twin of ``codec.js decodePos``; ``files`` maps role -> gz file bytes."""
    enc = pos_meta["enc"]
    if enc == "i16off":
        tile = np.frombuffer(unshuffle(gunzip(files["tile"]), 2), dtype="<u2")
        dra = np.frombuffer(unshuffle(gunzip(files["dra"]), 2), dtype="<i2")
        ddec = np.frombuffer(unshuffle(gunzip(files["ddec"]), 2), dtype="<i2")
        refs = np.asarray(pos_meta["tile_refs"], dtype=np.float64).reshape(-1, 2)
        s = pos_meta["step_mas"] / 3.6e6
        miss = (dra == I16_MISSING) | (ddec == I16_MISSING)
        t = np.minimum(tile, max(len(refs) - 1, 0))
        if len(refs) == 0:
            return np.full(len(tile), np.nan), np.full(len(tile), np.nan)
        ra0 = refs[t, 0]
        d0 = refs[t, 1]
        ra = ra0 + dra * s / np.cos(np.radians(d0))
        dec = d0 + ddec * s
        ra[miss] = np.nan
        dec[miss] = np.nan
        return ra, dec
    if enc == "u32lin":
        if bbox is None:
            raise ValueError("u32lin positions need the catalog bbox")
        out = []
        for k in ("ra", "dec"):
            c = np.frombuffer(unshuffle(gunzip(files[k]), 4), dtype="<u4")
            lo, hi = bbox[k]
            v = lo + c.astype(np.float64) * (hi - lo) / U32LIN_MAX
            v[c == U32_MISSING] = np.nan
            out.append(v)
        return out[0], out[1]
    raise ValueError(f"unknown position encoding {enc!r}")


# ---------------------------------------------------------------------------
# str and sparse
# ---------------------------------------------------------------------------

def encode_str(strings: Iterable[Any]) -> bytes:
    """``str`` payload: ``u32 n``, ``u32 offsets[n+1]`` (byte offsets into the blob), UTF-8 blob.

    Missing values (None, masked, NaN) are written as the empty string.
    """
    parts = []
    for s in strings:
        if is_missing_obj(s):
            parts.append(b"")
        elif isinstance(s, bytes):
            parts.append(s)
        else:
            parts.append(str(s).encode("utf-8"))
    n = len(parts)
    lens = np.fromiter((len(p) for p in parts), dtype=np.int64, count=n)
    offs = np.zeros(n + 1, dtype=np.int64)
    np.cumsum(lens, out=offs[1:])
    if offs[-1] > 0xFFFFFFFF:
        raise ValueError("str blob exceeds 4 GiB")
    return struct.pack("<I", n) + offs.astype("<u4").tobytes() + b"".join(parts)


def decode_str(raw: bytes) -> list[str]:
    """Inverse of :func:`encode_str` (input is the inflated payload)."""
    n = struct.unpack_from("<I", raw, 0)[0]
    offs = np.frombuffer(raw, dtype="<u4", count=n + 1, offset=4)
    base = 4 + 4 * (n + 1)
    blob = raw[base:]
    return [blob[offs[i]:offs[i + 1]].decode("utf-8") for i in range(n)]


def encode_sparse(idx: Sequence[int] | np.ndarray, kind: Sequence[int] | np.ndarray) -> bytes:
    """``sparse`` payload: ``u32 m``, ``u32 idx[m]`` ascending, ``u8 kind[m]``.

    Entries are sorted by row index; duplicate rows keep the first kind given.
    """
    idx = np.asarray(idx, dtype=np.int64)
    kind = np.asarray(kind, dtype=np.int64)
    if idx.shape != kind.shape:
        raise ValueError("idx and kind lengths differ")
    if idx.size and (idx.min() < 0 or idx.max() > 0xFFFFFFFE):
        raise ValueError("sparse row index out of range")
    if kind.size and (kind.min() < 1 or kind.max() > 255):
        raise ValueError("sparse kind out of range 1..255")
    order = np.argsort(idx, kind="stable")
    idx, kind = idx[order], kind[order]
    if idx.size:
        first = np.ones(idx.size, dtype=bool)
        first[1:] = idx[1:] != idx[:-1]
        idx, kind = idx[first], kind[first]
    return struct.pack("<I", idx.size) + idx.astype("<u4").tobytes() + kind.astype(np.uint8).tobytes()


def decode_sparse(raw: bytes) -> tuple[np.ndarray, np.ndarray]:
    m = struct.unpack_from("<I", raw, 0)[0]
    idx = np.frombuffer(raw, dtype="<u4", count=m, offset=4)
    kind = np.frombuffer(raw, dtype=np.uint8, count=m, offset=4 + 4 * m)
    return idx.copy(), kind.copy()


# ---------------------------------------------------------------------------
# packing / file bytes / decoding
# ---------------------------------------------------------------------------

_DTYPES = {"u16q": "<u2", "u16log": "<u2", "cat16": "<u2", "u16": "<u2", "i16": "<i2",
           "u8q": "u1", "u8log": "u1", "cat8": "u1", "u8": "u1",
           "f32": "<f4", "u32": "<u4", "u32lin": "<u4"}


def pack_codes(enc: str, codes: np.ndarray, shuffle: bool = False) -> bytes:
    """Raw (uncompressed) payload of a column/chunk."""
    if enc == "bits":
        return np.packbits(np.asarray(codes, dtype=np.uint8) & 1, bitorder="little").tobytes()
    if enc not in _DTYPES:
        raise ValueError(f"cannot pack encoding {enc!r}")
    a = np.ascontiguousarray(np.asarray(codes).astype(_DTYPES[enc], copy=False))
    w = a.dtype.itemsize
    if shuffle and w == 2:
        return shuffle_i16(a) if enc == "i16" else shuffle_u16(a)
    if shuffle and w == 4:
        return shuffle4(a)
    return a.tobytes()


def file_bytes(enc: str, codes: np.ndarray, shuffle: bool = False) -> bytes:
    """gz file bytes of a column/chunk (``filtered=True`` when byte-shuffled)."""
    width = np.dtype(_DTYPES[enc]).itemsize if enc in _DTYPES else 1
    shuffled = bool(shuffle) and width > 1
    return gz(pack_codes(enc, codes, shuffled), filtered=shuffled)


def decode_file(meta: dict, data: bytes, n: int | None = None) -> np.ndarray:
    """Decode the gz bytes of a column/chunk to its code array (bits -> 0/1 uint8 of length n).

    For ``str`` columns returns an object array of strings.
    """
    raw = gunzip(data) if data[:2] == b"\x1f\x8b" else bytes(data)
    enc = meta["enc"]
    if enc == "str":
        return np.array(decode_str(raw), dtype=object)
    if enc == "sparse":
        return np.stack(decode_sparse(raw))
    if enc == "bits":
        b = np.unpackbits(np.frombuffer(raw, dtype=np.uint8), bitorder="little")
        return b[:n] if n is not None else b
    dt = np.dtype(_DTYPES[enc])
    if meta.get("shuffle") and dt.itemsize > 1:
        raw = unshuffle(raw, dt.itemsize)
    return np.frombuffer(raw, dtype=dt).copy()


def dequant(meta: dict, codes: np.ndarray) -> np.ndarray:
    """Decoded values (float64, NaN for missing) for numeric encodings; labels for cats."""
    enc = meta["enc"]
    c = np.asarray(codes)
    if enc in ("u16q", "u8q", "u16log"):
        lo, hi = float(meta["lo"]), float(meta["hi"])
        step = meta.get("step")
        if step is None:
            step = (hi - lo) / (QMAX if enc != "u8q" else QMAX_U8)
        v = lo + (c.astype(np.float64) - 1.0) * float(step)
        if enc == "u16log":
            v = 10.0 ** v
        v[c == 0] = np.nan
        return v
    if enc == "u8log":
        lo_l, hi_l = math.log10(meta["lo"]), math.log10(meta["hi"])
        v = 10.0 ** (lo_l + (c.astype(np.float64) - 1.0) / QMAX_U8 * (hi_l - lo_l))
        v[c == 0] = np.nan
        return v
    if enc == "f32":
        return c.astype(np.float64)
    if enc == "u32":
        v = c.astype(np.float64)
        v[c == U32_MISSING] = np.nan
        return v
    if enc == "u32lin":
        lo, hi = float(meta["lo"]), float(meta["hi"])
        v = lo + c.astype(np.float64) * (hi - lo) / U32LIN_MAX
        v[c == U32_MISSING] = np.nan
        return v
    if enc in ("cat8", "cat16"):
        miss = CAT8_MISSING if enc == "cat8" else CAT16_MISSING
        lv = meta["levels"]
        return np.array([None if int(k) == miss else lv[int(k)] for k in c], dtype=object)
    if enc == "bits":
        return c.astype(np.float64)
    raise ValueError(f"dequant: unsupported encoding {enc!r}")


# ---------------------------------------------------------------------------
# catalog chunk geometry and whole-column readers (IMPLEMENTATION_NOTES 98)
# ---------------------------------------------------------------------------

#: default ``chunk_rows`` of a manifest that omits it (section 5.4)
DEFAULT_CHUNK_ROWS = CHUNK_ROWS


def _is_int(v: Any) -> bool:
    return isinstance(v, (int, np.integer)) and not isinstance(v, (bool, np.bool_))


def catalog_chunk_starts(man: dict) -> list[int]:
    """First row of every chunk of a ``pk-catalog/1`` manifest.

    Mirrors ``catalogChunkStarts`` in ``web/js/workers/data.worker.js``: the manifest's
    ``chunk_starts`` when present (integers, starting at 0, strictly increasing, every value
    < n), else ``[0, chunk_rows, 2*chunk_rows, ...]`` with ``ceil(n / chunk_rows)`` entries (one
    chunk when ``n <= chunk_rows``, including ``n = 0``). Raises ValueError on a malformed
    ``chunk_starts`` or ``chunk_rows``.
    """
    n = man.get("n")
    if not _is_int(n) or n < 0:
        raise ValueError(f"manifest n = {n!r}: expected a non-negative integer")
    cs = man.get("chunk_starts")
    if cs is not None:
        if not isinstance(cs, (list, tuple)) or not cs:
            raise ValueError("manifest chunk_starts must be a non-empty array")
        for k, v in enumerate(cs):
            ok = _is_int(v) and (v == 0 if k == 0 else (cs[k - 1] < v < n))
            if not ok:
                raise ValueError(f"manifest chunk_starts[{k}] = {v!r}: expected strictly increasing "
                                 f"integers from 0, each < n ({n})")
        return [int(v) for v in cs]
    rows = man.get("chunk_rows")
    rows = DEFAULT_CHUNK_ROWS if rows is None else rows
    if not _is_int(rows) or rows <= 0:
        raise ValueError(f"manifest chunk_rows = {rows!r}: expected a positive integer")
    k = max(1, -(-n // rows))
    return [c * int(rows) for c in range(k)]


def chunk_ranges(man: dict, nfiles: int | None = None) -> list[tuple[int, int]]:
    """``[first, end)`` rows of each chunk file of a column or of the ids.

    ``nfiles`` is the number of files of that column (``len(chunk_files)``, 1 for ``file``);
    one file always covers ``[0, n)``. Several files must match the catalog chunk count
    (ValueError otherwise), as in the worker's ``fileStarts``.
    """
    n = int(man["n"])
    if nfiles is not None and nfiles <= 1:
        return [(0, n)]
    starts = catalog_chunk_starts(man)
    if nfiles is not None and nfiles != len(starts):
        raise ValueError(f"{nfiles} chunk files, expected {len(starts)} (one per chunk of {n} rows)")
    return [(a, starts[k + 1] if k + 1 < len(starts) else n) for k, a in enumerate(starts)]


def column_files(meta: dict) -> list[str]:
    """Files of a column (``chunk_files`` or ``[file]``) or of the ids (``chunks`` or ``[file]``)."""
    for k in ("chunk_files", "chunks"):
        v = meta.get(k)
        if v:
            return list(v)
    f = meta.get("file")
    return [f] if f else []


def read_column(man: dict, key_or_meta: str | dict, read: Any, *, chunks: Sequence[int] | None = None
                ) -> np.ndarray:
    """Codes of a stored column in stored order (``str`` -> object array of strings).

    ``read(rel_path) -> bytes`` fetches a file relative to the dataset directory. Chunks follow
    :func:`chunk_ranges` (``chunk_starts`` when present); ``bits`` chunks are cut to their own row
    counts. ``chunks`` limits the read to those chunk indices (concatenated in the order given),
    e.g. ``[0]`` for the head chunk. Each chunk's decoded length is checked.
    """
    meta = key_or_meta if isinstance(key_or_meta, dict) else \
        next((c for c in man.get("cols") or [] if c.get("key") == key_or_meta), None)
    if meta is None:
        raise KeyError(f"column {key_or_meta!r} not in manifest")
    files = column_files(meta)
    ranges = chunk_ranges(man, len(files))
    sel = range(len(files)) if chunks is None else list(chunks)
    parts = []
    for ci in sel:
        a, b = ranges[ci]
        v = decode_file(meta, read(files[ci]), b - a)
        if len(v) != b - a:
            raise ValueError(f"column {meta.get('key')!r} chunk {ci}: {len(v)} rows, expected {b - a}")
        parts.append(v)
    if not parts:
        return np.zeros(0, dtype=object if meta.get("enc") == "str" else np.uint8)
    return np.concatenate(parts)


def read_ids(man: dict, read: Any, *, chunks: Sequence[int] | None = None) -> list[str]:
    """Catalog ids in stored order (``ids.file`` or ``ids.chunks``), checked per chunk."""
    im = man.get("ids") or {}
    files = column_files(im)
    if not files:
        return []
    ranges = chunk_ranges(man, len(files))
    out: list[str] = []
    for ci in (range(len(files)) if chunks is None else chunks):
        a, b = ranges[ci]
        raw = read(files[ci])
        s = decode_str(gunzip(raw) if raw[:2] == b"\x1f\x8b" else bytes(raw))
        if len(s) != b - a:
            raise ValueError(f"ids chunk {ci}: {len(s)} ids, expected {b - a}")
        out += s
    return out
