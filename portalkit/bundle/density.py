"""Pre-binned 2D density grids and binned statistics (ARCHITECTURE.md section 5.9).

Grid file layout: ``nx*ny`` values in row-major order with x fastest
(``cell = iy * nx + ix``, ``iy = 0`` at ``yr[0]``, ``ix = 0`` at ``xr[0]``). Bin edges are
``linspace(xr[0], xr[1], nx + 1)``; the last bin is closed on the right. Rows outside the
ranges or with a missing x/y are not counted.

Encodings (manifest ``density[].enc``):

* ``u16log`` (count grids, default): the section 5.3 ``u16log`` column encoding applied to the
  counts (``codec.encode_u16log``): ``lo``/``hi`` are log10 of the smallest non-zero and the
  largest count, ``step = (hi - lo) / 65534`` (always emitted, implementation note 9),
  ``code = 0`` for an empty cell, else ``1 + rint((log10 c - lo) / step)``.
  Decode: ``10 ** (lo + (code - 1) * step)``, the same rule as a ``u16log`` column.
* ``u16q`` (count grids): ``lo``/``hi``/``step`` linear, ``code = 1 + rint((c - lo) / step)``.
* ``f32`` (binned statistics, implementation note 4): float32 per cell, NaN for an empty
  cell, ``"stat": <fn>`` and ``"value": <expr>``.

u16 grids are 2-plane shuffled and written with ``gz(filtered=True)``; f32 grids are 4-plane
shuffled (``"shuffle": true``).
"""

from __future__ import annotations

import re
from typing import Callable, Mapping

import numpy as np

from .codec import QMAX, encode_u16log, file_bytes

__all__ = ["DensityError", "parse_linear", "eval_linear", "base_keys", "auto_range",
           "count_grid", "encode_counts", "decode_counts", "binned_stat", "parse_stat",
           "grid_bytes", "build_density", "STATS"]

STATS = ("count", "mean", "median", "min", "max", "std", "sum")


class DensityError(ValueError):
    pass


# ---------------------------------------------------------------------------
# linear expressions (section 4.4 grammar; leading '-' also accepted)
# ---------------------------------------------------------------------------

_NUM = r"[0-9]+(?:\.[0-9]*)?(?:[eE][+-]?[0-9]+)?"
_KEY = r"[A-Za-z_][A-Za-z0-9_.]*"
_TERM = re.compile(rf"\s*(?:({_NUM})\s*\*\s*({_KEY})|({_KEY})|({_NUM}))\s*")
_SIGN = re.compile(r"\s*([+-])")


def parse_linear(expr: str) -> tuple[dict[str, float], float]:
    """Compile a linear expression to ``({key: coeff}, const)``."""
    if not isinstance(expr, str) or not expr.strip():
        raise DensityError(f"empty expression {expr!r}")
    w: dict[str, float] = {}
    c = 0.0
    i = 0
    sign = 1.0
    m = _SIGN.match(expr, i)
    if m and m.group(1) == "-":
        sign = -1.0
        i = m.end()
    while True:
        m = _TERM.match(expr, i)
        if not m or m.end() == i:
            raise DensityError(f"bad expression {expr!r} at {i}")
        num, k1, k2, num2 = m.groups()
        if k1 is not None:
            w[k1] = w.get(k1, 0.0) + sign * float(num)
        elif k2 is not None:
            w[k2] = w.get(k2, 0.0) + sign
        else:
            c += sign * float(num2)
        i = m.end()
        if i >= len(expr):
            break
        m = _SIGN.match(expr, i)
        if not m:
            raise DensityError(f"bad expression {expr!r} at {i}")
        sign = 1.0 if m.group(1) == "+" else -1.0
        i = m.end()
    return w, c


def base_keys(expr: str) -> list[str]:
    return list(parse_linear(expr)[0])


def eval_linear(expr: str, cols: Mapping[str, np.ndarray] | Callable[[str], np.ndarray]
                ) -> np.ndarray:
    """Evaluate ``expr`` over float columns; missing (NaN) in any base column -> NaN."""
    w, c = parse_linear(expr)
    get = cols if callable(cols) else cols.__getitem__
    out = None
    for k, a in w.items():
        v = np.asarray(get(k), dtype=np.float64) * a
        out = v if out is None else out + v
    if out is None:
        raise DensityError(f"expression {expr!r} references no column")
    return out + c


# ---------------------------------------------------------------------------
# grids
# ---------------------------------------------------------------------------

def auto_range(v: np.ndarray, lo_pct: float = 0.5, hi_pct: float = 99.5, pad: float = 0.05
               ) -> list[float]:
    """Percentile range padded by ``pad`` of its width (``[0, 1]`` when no finite value)."""
    v = np.asarray(v, dtype=np.float64)
    v = v[np.isfinite(v)]
    if v.size == 0:
        return [0.0, 1.0]
    a, b = (float(q) for q in np.percentile(v, [lo_pct, hi_pct]))
    if b <= a:
        half = max(abs(a) * 0.05, 0.5)
        return [a - half, a + half]
    d = (b - a) * pad
    return [float(f"{a - d:.6g}"), float(f"{b + d:.6g}")]


def _cells(x: np.ndarray, y: np.ndarray, xr, yr, nx: int, ny: int) -> tuple[np.ndarray, np.ndarray]:
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    if nx < 1 or ny < 1:
        raise DensityError("nx and ny must be >= 1")
    x0, x1 = float(xr[0]), float(xr[1])
    y0, y1 = float(yr[0]), float(yr[1])
    if not (x1 > x0 and y1 > y0):
        raise DensityError(f"empty range xr={xr} yr={yr}")
    ok = np.isfinite(x) & np.isfinite(y) & (x >= x0) & (x <= x1) & (y >= y0) & (y <= y1)
    ix = np.minimum(((x[ok] - x0) / (x1 - x0) * nx).astype(np.int64), nx - 1)
    iy = np.minimum(((y[ok] - y0) / (y1 - y0) * ny).astype(np.int64), ny - 1)
    return ok, iy * nx + ix


def count_grid(x: np.ndarray, y: np.ndarray, xr, yr, nx: int = 256, ny: int = 256) -> np.ndarray:
    """Counts per cell, shape ``(ny, nx)``, int64."""
    _, cell = _cells(x, y, xr, yr, nx, ny)
    return np.bincount(cell, minlength=nx * ny).reshape(ny, nx)


def encode_counts(counts: np.ndarray, enc: str = "u16log") -> tuple[np.ndarray, dict]:
    """Quantize a count grid; returns (flat uint16 codes, meta {enc, lo, hi[, step]})."""
    c = np.asarray(counts, dtype=np.float64).ravel()
    nz = c > 0
    codes = np.zeros(c.size, dtype=np.uint16)
    if enc == "u16log":
        e = encode_u16log(c, None, None, None, "none", label="density")
        m = e.meta
        return e.codes.astype(np.uint16), {"enc": "u16log", "lo": float(m["lo"]),
                                           "hi": float(m["hi"]), "step": float(m["step"])}
    if enc == "u16q":
        if not nz.any():
            return codes, {"enc": "u16q", "lo": 0, "hi": 0, "step": 1.0}
        lo, hi = float(c[nz].min()), float(c[nz].max())
        step = (hi - lo) / QMAX if hi > lo else 1.0
        q = 1.0 + np.rint((c[nz] - lo) / step)
        codes[nz] = np.clip(q, 1, QMAX + 1).astype(np.uint16)
        return codes, {"enc": "u16q", "lo": _num(lo), "hi": _num(hi), "step": step}
    raise DensityError(f"count grids use u16log or u16q, not {enc!r}")


def _num(v: float) -> int | float:
    return int(v) if float(v).is_integer() else float(v)


def decode_counts(meta: dict, codes: np.ndarray) -> np.ndarray:
    """Approximate counts per cell (0 for empty cells)."""
    c = np.asarray(codes).astype(np.float64)
    out = np.zeros_like(c)
    nz = c > 0
    lo, hi = float(meta["lo"]), float(meta["hi"])
    if meta["enc"] == "u16log":
        step = float(meta["step"]) if meta.get("step") is not None else (hi - lo) / QMAX
        out[nz] = 10.0 ** (lo + (c[nz] - 1.0) * step)
    elif meta["enc"] == "u16q":
        out[nz] = lo + (c[nz] - 1.0) * float(meta["step"])
    else:
        raise DensityError(f"not a count encoding: {meta['enc']!r}")
    return out


def parse_stat(stat: str | None) -> tuple[str, str | None]:
    """``None``/``"count"`` -> ("count", None); ``"median:av"`` or ``"median(av)"`` -> ("median", "av")."""
    if stat is None or stat == "count":
        return "count", None
    m = re.fullmatch(r"\s*([a-z]+)\s*(?::\s*(.+?)|\(\s*(.+?)\s*\))\s*", stat)
    if not m:
        raise DensityError(f"stat {stat!r} must be 'count' or '<fn>:<expr>' with fn in {STATS}")
    fn = m.group(1)
    expr = m.group(2) or m.group(3)
    if fn not in STATS:
        raise DensityError(f"unknown statistic {fn!r} (have {STATS})")
    if fn == "count":
        return "count", None
    return fn, expr


def binned_stat(x: np.ndarray, y: np.ndarray, v: np.ndarray, xr, yr, nx: int = 256,
                ny: int = 256, stat: str = "median") -> np.ndarray:
    """Per-cell statistic of ``v`` (rows with non-finite ``v`` ignored); float32 ``(ny, nx)``,
    NaN for empty cells."""
    if stat not in STATS:
        raise DensityError(f"unknown statistic {stat!r}")
    ok, cell = _cells(x, y, xr, yr, nx, ny)
    vals = np.asarray(v, dtype=np.float64)[ok]
    good = np.isfinite(vals)
    cell, vals = cell[good], vals[good]
    ncell = nx * ny
    out = np.full(ncell, np.nan)
    if stat == "count":
        out = np.bincount(cell, minlength=ncell).astype(np.float64)
        out[out == 0] = np.nan
        return out.reshape(ny, nx).astype(np.float32)
    if cell.size == 0:
        return out.reshape(ny, nx).astype(np.float32)
    n = np.bincount(cell, minlength=ncell).astype(np.float64)
    has = n > 0
    if stat in ("mean", "sum", "std"):
        s = np.bincount(cell, weights=vals, minlength=ncell)
        if stat == "sum":
            out[has] = s[has]
        elif stat == "mean":
            out[has] = s[has] / n[has]
        else:
            mu = np.zeros(ncell)
            mu[has] = s[has] / n[has]
            s2 = np.bincount(cell, weights=(vals - mu[cell]) ** 2, minlength=ncell)
            out[has] = np.sqrt(s2[has] / n[has])
    else:
        order = np.lexsort((vals, cell))
        cs, vs = cell[order], vals[order]
        starts = np.flatnonzero(np.r_[True, cs[1:] != cs[:-1]])
        cnt = np.diff(np.r_[starts, cs.size])
        ucell = cs[starts]
        if stat == "min":
            out[ucell] = vs[starts]
        elif stat == "max":
            out[ucell] = vs[starts + cnt - 1]
        else:
            out[ucell] = 0.5 * (vs[starts + (cnt - 1) // 2] + vs[starts + cnt // 2])
    return out.reshape(ny, nx).astype(np.float32)


def grid_bytes(enc: str, grid: np.ndarray) -> bytes:
    """gz file bytes of a grid (u16 codes 2-plane shuffled; f32 4-plane shuffled)."""
    flat = np.asarray(grid).ravel()
    if enc in ("u16log", "u16q"):
        return file_bytes("u16q", flat.astype(np.uint16), shuffle=True)
    if enc == "f32":
        return file_bytes("f32", flat.astype(np.float32), shuffle=True)
    raise DensityError(f"unknown grid encoding {enc!r}")


def build_density(key: str, x: np.ndarray, y: np.ndarray, *, x_expr: str, y_expr: str,
                  xr=None, yr=None, nx: int = 256, ny: int = 256, stat: str | None = None,
                  values: np.ndarray | None = None, enc: str = "u16log"
                  ) -> tuple[dict, bytes]:
    """Return ``(manifest entry without 'file', file bytes)`` for one density preset.

    ``x``/``y`` are the evaluated axis values (any order: a grid does not depend on row
    order). ``stat`` follows :func:`parse_stat`; for a statistic other than count, ``values``
    holds the evaluated value expression.
    """
    xr = list(xr) if xr is not None else auto_range(x)
    yr = list(yr) if yr is not None else auto_range(y)
    fn, vexpr = parse_stat(stat)
    entry: dict = {"key": key, "x": x_expr, "y": y_expr, "xr": [float(xr[0]), float(xr[1])],
                   "yr": [float(yr[0]), float(yr[1])], "nx": int(nx), "ny": int(ny)}
    if fn == "count":
        counts = count_grid(x, y, xr, yr, nx, ny)
        codes, meta = encode_counts(counts, enc)
        entry.update(meta)
        entry["total"] = int(counts.sum())
        return entry, grid_bytes(meta["enc"], codes)
    if values is None:
        raise DensityError(f"density {key!r}: statistic {fn!r} needs values")
    g = binned_stat(x, y, values, xr, yr, nx, ny, fn)
    fin = g[np.isfinite(g)]
    entry.update({"enc": "f32", "stat": fn, "value": vexpr, "shuffle": True,
                  "lo": float(fin.min()) if fin.size else 0.0,
                  "hi": float(fin.max()) if fin.size else 0.0})
    return entry, grid_bytes("f32", g)
