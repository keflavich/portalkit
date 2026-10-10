"""Extinction: G23 A_lambda/A_V tables, ``extinction.<h8>.json`` and reddening helpers.

ARCHITECTURE.md sections 5.10 and 6.6 (``aLambda``, ``vector``, ``redden``).

The default source is the vendored ``portalkit/data/g23_alav.ecsv``:

* one row per registry filter: ``key``, ``wl_um`` (pivot) and ``G23_3.1``/``G23_5.5``,
  A(lambda)/A(V) of Gordon et al. (2023) at the pivot wavelength, computed with
  ``dust_extinction``; NaN where the pivot lies outside the G23 range (0.0912-32 um);
* ``meta["curve"]``: the G23 curve sampled at ``CURVE_NPT`` log-spaced wavelengths over the
  full G23 range, ``{"w": [...], "3.1": [...], "5.5": [...]}``.

G23 is ``a(x) + b(x) (1/R_V - 1/3.1)``, so A(lambda)/A(V) is linear in ``1/R_V``; any R_V is
interpolated exactly from the two vendored curves (:func:`interp_rv`). ``dust_extinction``
is optional: it is only needed for :func:`regenerate_alav_table` and for laws other than G23.

Interpolation in wavelength is linear and NaN outside the sampled range (the JS twin does
the same).
"""

from __future__ import annotations

import math
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from . import PhysicsResult, _writer
from .sed import interp_linear

__all__ = ["ALAV_PATH", "VENDORED_RV", "G23_WL_RANGE_UM", "G23_RV_RANGE", "CURVE_NPT",
           "rv_key", "law_key", "load_alav", "vendored_curve", "interp_rv", "k_curve",
           "a_over_av", "entry_k", "a_lambda", "reddening_vector", "redden",
           "dust_extinction_available", "g23_dust_extinction", "extinction_document", "write",
           "regenerate_alav_table"]

ALAV_PATH = Path(__file__).resolve().parent.parent / "data" / "g23_alav.ecsv"
VENDORED_RV = (3.1, 5.5)
#: G23 x_range [0.03125, 10.9649] 1/um
G23_WL_RANGE_UM = (1.0 / 10.9649, 32.0)
G23_RV_RANGE = (2.3, 5.6)
CURVE_NPT = 512


def rv_key(rv: float) -> str:
    """Curve key of an R_V value (``f"{rv:g}"``, as schema.py)."""
    return f"{float(rv):g}"


def law_key(rv: float, law: str = "G23") -> str:
    """``a_over_av`` key: ``G23_3.1``."""
    return f"{law}_{rv_key(rv)}"


def _parse_law_key(k: str) -> tuple[str, float] | None:
    if "_" not in k:
        return None
    law, rv = k.rsplit("_", 1)
    try:
        r = float(rv)
    except ValueError:
        return None
    return (law, r) if math.isfinite(r) and r > 0 else None


# ---------------------------------------------------------------------------
# vendored table
# ---------------------------------------------------------------------------

@lru_cache(maxsize=4)
def _load(path: str) -> tuple[dict[str, dict], dict]:
    from astropy.table import Table
    t = Table.read(path, format="ascii.ecsv")
    rows: dict[str, dict] = {}
    cols = [c for c in t.colnames if c.startswith("G23_")]
    for r in t:
        d: dict[str, Any] = {"wl_um": float(r["wl_um"])}
        for c in cols:
            v = float(r[c])
            d[c] = v if math.isfinite(v) else None
        rows[str(r["key"])] = d
    curve = t.meta.get("curve") or {}
    cv = {k: np.asarray(v, dtype=np.float64) for k, v in curve.items()}
    return rows, cv


def load_alav(path: str | Path | None = None) -> dict[str, dict]:
    """``{filter key: {"wl_um": .., "G23_3.1": .., "G23_5.5": ..}}`` (None outside G23)."""
    return _load(str(Path(path) if path is not None else ALAV_PATH))[0]


def vendored_curve(path: str | Path | None = None) -> dict[str, np.ndarray]:
    """``{"w": wl_um, "3.1": k, "5.5": k}`` from the vendored ECSV (do not mutate)."""
    return _load(str(Path(path) if path is not None else ALAV_PATH))[1]


def interp_rv(rv: float, values: Mapping[float, Any]) -> Any:
    """Value at ``rv`` from values at other R_V, linear in ``1/R_V`` (exact for G23).

    With an exact match that value is returned. Otherwise the two R_V values bracketing
    ``rv`` in ``1/R_V`` are used (the two nearest when ``rv`` is outside, i.e. linear
    extrapolation). Needs at least two entries unless ``rv`` matches one.
    """
    items = sorted(((float(r), v) for r, v in values.items()), key=lambda p: 1.0 / p[0])
    rv = float(rv)
    for r, v in items:
        if rv_key(r) == rv_key(rv):
            return v
    if len(items) < 2:
        raise ValueError(f"need two R_V values to interpolate to R_V={rv}")
    inv = 1.0 / rv
    xs = [1.0 / r for r, _ in items]
    j = int(np.searchsorted(xs, inv))
    j = min(max(j, 1), len(items) - 1)
    (r1, v1), (r2, v2) = items[j - 1], items[j]
    x1, x2 = 1.0 / r1, 1.0 / r2
    f = (inv - x1) / (x2 - x1)
    if isinstance(v1, np.ndarray) or isinstance(v2, np.ndarray):
        return np.asarray(v1, dtype=np.float64) + (np.asarray(v2, dtype=np.float64)
                                                   - np.asarray(v1, dtype=np.float64)) * f
    if v1 is None or v2 is None:
        return None
    return float(v1) + (float(v2) - float(v1)) * f


def dust_extinction_available() -> bool:
    try:
        import dust_extinction  # noqa: F401
    except ImportError:
        return False
    return True


def _law_class(law: str):
    try:
        from dust_extinction import averages, parameter_averages
    except ImportError as err:
        raise RuntimeError(f"extinction law {law!r} needs the optional 'dust_extinction' "
                           "package (pip install portalkit[extinction])") from err
    cls = getattr(parameter_averages, law, None) or getattr(averages, law, None)
    if cls is None:
        raise ValueError(f"unknown extinction law {law!r}")
    return cls


def _law_eval(law: str, wl_um: Any, rv: float) -> np.ndarray:
    import astropy.units as u
    cls = _law_class(law)
    try:
        model = cls(Rv=float(rv))
    except TypeError:
        model = cls()
    w = np.atleast_1d(np.asarray(wl_um, dtype=np.float64))
    x = np.full(w.shape, np.nan)
    good = np.isfinite(w) & (w > 0)
    x[good] = 1.0 / w[good]
    lo, hi = (float(v) for v in model.x_range)
    inr = good & (x >= lo * (1 - 1e-12)) & (x <= hi * (1 + 1e-12))
    out = np.full(w.shape, np.nan)
    if np.any(inr):
        xv = np.clip(x[inr], lo, hi)
        out[inr] = np.asarray(model(xv / u.micron), dtype=np.float64)
    return out


def g23_dust_extinction(wl_um: Any, rv: float) -> np.ndarray:
    """A(lambda)/A(V) from ``dust_extinction`` G23 (NaN outside 0.0912-32 um).

    Raises RuntimeError when dust_extinction is not installed.
    """
    return _law_eval("G23", wl_um, rv)


def _law_wl_range(law: str, rv: float) -> tuple[float, float]:
    cls = _law_class(law)
    try:
        model = cls(Rv=float(rv))
    except TypeError:
        model = cls()
    lo, hi = (float(v) for v in model.x_range)
    return 1.0 / hi, 1.0 / lo


def _curve_grid(wmin: float, wmax: float, npt: int = CURVE_NPT) -> np.ndarray:
    w = np.geomspace(wmin, wmax, npt)
    w = np.array([float(f"{x:.6g}") for x in w])
    w[0] = max(w[0], wmin)
    w[-1] = min(w[-1], wmax)
    return np.unique(w)


def k_curve(rv: float, law: str = "G23") -> tuple[np.ndarray, np.ndarray]:
    """(wl_um, A(lambda)/A(V)) for ``law`` at ``rv``.

    G23 comes from the vendored curve (any R_V by 1/R_V interpolation). Other laws are
    sampled from ``dust_extinction`` over their own range.
    """
    if law == "G23":
        rv = float(rv)
        if not (G23_RV_RANGE[0] <= rv <= G23_RV_RANGE[1]):
            raise ValueError(f"G23 is defined for R_V in {G23_RV_RANGE}, got {rv}")
        cv = vendored_curve()
        vals = {float(k): v for k, v in cv.items() if k != "w"}
        return cv["w"].copy(), np.asarray(interp_rv(rv, vals), dtype=np.float64)
    wmin, wmax = _law_wl_range(law, rv)
    w = _curve_grid(wmin, wmax)
    return w, _law_eval(law, w, rv)


def a_over_av(wl_um: Any, rv: float = 3.1, law: str = "G23") -> Any:
    """A(lambda)/A(V) at ``wl_um`` from :func:`k_curve` (linear in wavelength; NaN outside)."""
    w, k = k_curve(rv, law)
    out = interp_linear(wl_um, w, k)
    return float(out) if np.ndim(wl_um) == 0 else out


# ---------------------------------------------------------------------------
# reddening (Python twins of aLambda / vector / redden)
# ---------------------------------------------------------------------------

def _fin(v: Any) -> float | None:
    if v is None or isinstance(v, (bool, str)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def entry_k(entry: Mapping, rv: float, ext: Mapping | None = None, law: str | None = None) -> float:
    """A(lambda)/A(V) of a ``pk-filters/1`` entry at ``rv``.

    1. ``a_over_av["<law>_<rv>"]`` when present and not null;
    2. linear in 1/R_V from the entry's other ``<law>_*`` values (at least two non-null);
    3. the ``ext`` document (``pk-extinction/1``) curve at the entry's ``wl_um``: the curve
       for ``rv`` if present, else 1/R_V interpolation of the two curves;
    4. NaN.

    ``law`` defaults to ``ext["law"]`` or ``"G23"``.
    """
    law = law or (ext.get("law") if isinstance(ext, Mapping) and ext.get("law") else "G23")
    aa = entry.get("a_over_av") or {}
    lk = law_key(rv, law)
    v = _fin(aa.get(lk))
    if v is not None:
        return v
    same = {}
    for k, val in aa.items():
        p = _parse_law_key(k)
        fv = _fin(val)
        if p and p[0] == law and fv is not None:
            same[p[1]] = fv
    if len(same) >= 2:
        return float(interp_rv(rv, same))
    if isinstance(ext, Mapping) and ext.get("curve"):
        wl = _fin(entry.get("wl_um"))
        if wl is None:
            return float("nan")
        curves = ext["curve"]
        rvk = rv_key(rv)
        if rvk in curves:
            c = curves[rvk]
            return float(interp_linear(wl, c["w"], [np.nan if x is None else x for x in c["k"]]))
        vals = {}
        for ck, c in curves.items():
            kk = float(interp_linear(wl, c["w"], [np.nan if x is None else x for x in c["k"]]))
            vals[float(ck)] = kk
        if len(vals) >= 2:
            return float(interp_rv(rv, vals))
    return float("nan")


def _filters_map(filters: Any) -> Mapping:
    if isinstance(filters, Mapping) and "filters" in filters and isinstance(filters["filters"], Mapping):
        return filters["filters"]
    return filters or {}


def a_lambda(filt: str | Mapping, av: float, rv: float, ext: Mapping | None = None,
             filters: Mapping | None = None, colmap: Mapping[str, str] | None = None) -> float:
    """``A_lambda = A_V * k`` for a filter key (resolved in ``filters``) or a filter entry.

    Python twin of ``aLambda(filterKey, av, rv, ext)``; ``filters`` is the ``pk-filters/1``
    document or its ``filters`` object. Unknown filter -> NaN.
    """
    from .filters import resolve_key
    if isinstance(filt, Mapping):
        entry = filt
    else:
        fl = _filters_map(filters)
        k = resolve_key(filt, fl, colmap)
        if k is None:
            return float("nan")
        entry = fl[k]
    return float(av) * entry_k(entry, rv, ext)


def reddening_vector(x_expr: str, y_expr: str, av: float, rv: float, filters: Mapping,
                     colmap: Mapping[str, str] | None = None, ext: Mapping | None = None
                     ) -> dict[str, float]:
    """``{dx, dy}``: change of two linear expressions for ``A_V = av`` (twin of ``vector``).

    Each expression key is resolved to a filter (:func:`filters.resolve_key`); the vector is
    ``sum_k w_k * A_lambda(k)``. The constant term does not contribute. An unresolved key
    makes that component NaN.
    """
    from ..expr import compile as compile_expr
    out = {}
    for name, expr in (("dx", x_expr), ("dy", y_expr)):
        c = compile_expr(expr)
        tot = 0.0
        for k, wgt in c["w"].items():
            if wgt == 0:
                continue
            a = a_lambda(k, av, rv, ext, filters, colmap)
            tot += wgt * a
        out[name] = float(tot)
    return out


def redden(wave: Any, flux: Any, law_w: Any, law_k: Any, av: float) -> np.ndarray:
    """``flux * 10**(-0.4 * k(wave) * av)`` as float32; ``k`` linear in wavelength, NaN outside
    the law (twin of ``redden(spec, law, av)``)."""
    k = interp_linear(wave, law_w, law_k)
    f = np.asarray(flux, dtype=np.float64)
    return (f * np.power(10.0, -0.4 * k * float(av))).astype(np.float32)


# ---------------------------------------------------------------------------
# document
# ---------------------------------------------------------------------------

def _g(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _round6(a: np.ndarray) -> list[float | None]:
    return [float(f"{x:.6g}") if math.isfinite(x) else None for x in np.asarray(a, dtype=np.float64)]


def extinction_document(spec: Any = None) -> dict:
    """``pk-extinction/1`` document for an ExtinctionSpec (default G23, R_V 3.1 and 5.5)."""
    law = _g(spec, "law", "G23") if spec is not None else "G23"
    rvs = list(_g(spec, "rv", VENDORED_RV) if spec is not None else VENDORED_RV)
    if not rvs:
        raise ValueError("ExtinctionSpec.rv is empty")
    curve = {}
    rv_out = []
    for rv in rvs:
        rv = float(rv)
        if not (math.isfinite(rv) and rv > 0):
            raise ValueError(f"bad R_V {rv}")
        w, k = k_curve(rv, law)
        ok = np.isfinite(k)
        if ok.sum() < 2:
            raise ValueError(f"{law} R_V={rv}: fewer than 2 finite samples")
        key = rv_key(rv)
        if key in curve:
            raise ValueError(f"duplicate R_V {rv}")
        curve[key] = {"w": _round6(w[ok]), "k": _round6(k[ok])}
        rv_out.append(rv)
    return {"schema": "pk-extinction/1", "law": str(law), "rv": rv_out, "curve": curve}


def write(spec: Any, dest: Any, *, ledger: Any = None) -> PhysicsResult:
    """Write ``extinction.<h8>.json`` into ``dest``."""
    from ..schema import validate_extinction
    w = _writer(dest, ledger)
    doc = extinction_document(spec)
    errs = validate_extinction(doc)
    if errs:
        raise ValueError("extinction document invalid: " + "; ".join(errs[:10]))
    path = w.put_json("", "extinction", doc, kind="extinction")
    return PhysicsResult(path=path, doc=doc, files=[path])


# ---------------------------------------------------------------------------
# regeneration (needs dust_extinction)
# ---------------------------------------------------------------------------

def regenerate_alav_table(path: str | Path = ALAV_PATH, registry: Mapping[str, Mapping] | None = None,
                          rvs: Sequence[float] = VENDORED_RV, npt: int = CURVE_NPT) -> Path:
    """Recompute ``g23_alav.ecsv`` with ``dust_extinction`` for every registry filter."""
    from astropy.table import Table
    from .filters import load_registry
    reg = registry if registry is not None else load_registry()
    keys = list(reg)
    wl = np.array([float(reg[k]["wl_um"]) for k in keys])
    t = Table()
    t["key"] = keys
    t["wl_um"] = wl
    t["wl_um"].unit = "um"
    for rv in rvs:
        vals = g23_dust_extinction(wl, rv)
        t[law_key(rv)] = [float(f"{v:.6g}") if math.isfinite(v) else np.nan for v in vals]
    w = _curve_grid(*G23_WL_RANGE_UM, npt=npt)
    curve: dict[str, list] = {"w": [float(x) for x in w]}
    for rv in rvs:
        curve[rv_key(rv)] = [float(f"{x:.6g}") for x in g23_dust_extinction(w, rv)]
    import dust_extinction
    t.meta["law"] = "G23 (Gordon et al. 2023), A(lambda)/A(V) at the filter pivot wavelength"
    t.meta["generator"] = (f"portalkit.physics.extinction.regenerate_alav_table with dust_extinction "
                           f"{getattr(dust_extinction, '__version__', '?')}")
    t.meta["range_um"] = [float(G23_WL_RANGE_UM[0]), float(G23_WL_RANGE_UM[1])]
    t.meta["note"] = "NaN where the pivot wavelength is outside the G23 range"
    t.meta["curve"] = curve
    path = Path(path)
    tmp = path.with_suffix(".ecsv.tmp")
    t.write(str(tmp), format="ascii.ecsv", overwrite=True)
    os.replace(tmp, path)
    _load.cache_clear()
    return path
