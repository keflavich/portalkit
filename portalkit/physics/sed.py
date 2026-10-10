"""SED reference implementations: mag <-> Jy, flux units, nuFnu, blackbody, SED points.

ARCHITECTURE.md section 6.6. These functions are the Python reference for
``web/js/data/filters.js`` (``magToJy``, ``jyToMag``, ``sedPoints``) and
``web/js/data/templates.js`` (``scaleTo``); ``tests/golden/physics`` holds their values for
the node tests (1e-6 relative).

Conventions
-----------
* Magnitudes are Vega (or whatever ``system`` the filter declares) with
  ``F = zp_jy * 10**(-0.4 * mag)``.
* A non-positive or non-finite flux has no magnitude (NaN).
* ``nufnu`` returns ``nu * F_nu`` in W m^-2 (``unit="W/m2"``, the spherex portal convention:
  ``F_nu[Jy] * 1e-26 * c / lambda``) or erg s^-1 cm^-2 (``unit="erg/s/cm2"``).
* :func:`sed_points` turns one catalog row into SED points. Each ``mag``/``flux`` column with
  a ``filter`` present in ``filters`` contributes one point. Points are sorted by
  wavelength (stable for ties). Limits come from the row's ``_lim`` mapping (column key ->
  sparse limit kind: 1 upper, 2 lower, as an integer or the strings ``"1"``/``"2"``; a
  ``_limits`` mapping is read the same way) or from the column's ``limit`` sibling column
  holding the legacy codes ``u``/``l`` (or ``1``/``2``); a limit point has ``err = None``.
* Magnitude sentinels (``-99``, ``99``, ``-999`` ...) must be NaN'd upstream (the catalog
  build's null handling): :func:`sed_points` only drops values whose flux is not finite
  (``-999`` overflows and is dropped; ``-99`` gives a very large finite flux and is kept).
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

__all__ = ["C_UM_S", "LN10_OVER_2P5", "FLUX_UNITS", "mag_to_jy", "jy_to_mag", "mag_err_to_jy",
           "flux_unit_factor", "nufnu", "blackbody_fnu", "sed_points", "scale_to", "interp_linear"]

#: speed of light in micron / s
C_UM_S = 2.99792458e14
LN10_OVER_2P5 = math.log(10.0) / 2.5

_H = 6.62607015e-34
_C = 2.99792458e8
_KB = 1.380649e-23

#: flux unit -> factor to Jy (case-sensitive aliases listed explicitly)
FLUX_UNITS: dict[str, float] = {
    "Jy": 1.0, "jy": 1.0,
    "mJy": 1e-3, "mjy": 1e-3,
    "uJy": 1e-6, "µJy": 1e-6, "μJy": 1e-6, "ujy": 1e-6,
    "nJy": 1e-9, "njy": 1e-9,
    "MJy": 1e6,
    "erg/s/cm2/Hz": 1e23, "erg/s/cm^2/Hz": 1e23, "erg s-1 cm-2 Hz-1": 1e23,
    "W/m2/Hz": 1e26, "W/m^2/Hz": 1e26, "W m-2 Hz-1": 1e26,
}


def _out(x: np.ndarray, *inputs: Any) -> Any:
    """A Python float when every input is a scalar, else the float64 array."""
    x = np.asarray(x, dtype=np.float64)
    if x.ndim == 0 and all(np.ndim(a) == 0 for a in inputs):
        return float(x)
    return x


def mag_to_jy(mag: Any, zp_jy: Any) -> Any:
    """``zp_jy * 10**(-0.4 * mag)``; NaN where either input is missing or non-finite."""
    m = np.asarray(mag, dtype=np.float64)
    z = np.asarray(zp_jy, dtype=np.float64)
    with np.errstate(over="ignore", invalid="ignore"):
        out = z * np.power(10.0, -0.4 * m)
    out = np.where(np.isfinite(m) & np.isfinite(z) & (z > 0), out, np.nan)
    return _out(out, mag, zp_jy)


def jy_to_mag(jy: Any, zp_jy: Any) -> Any:
    """``-2.5 log10(jy / zp_jy)``; NaN for ``jy <= 0`` or non-finite inputs."""
    f = np.asarray(jy, dtype=np.float64)
    z = np.asarray(zp_jy, dtype=np.float64)
    ok = np.isfinite(f) & np.isfinite(z) & (f > 0) & (z > 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = -2.5 * np.log10(np.where(ok, f, 1.0) / np.where(ok, z, 1.0))
    out = np.where(ok, out, np.nan)
    return _out(out, jy, zp_jy)


def mag_err_to_jy(fnu_jy: Any, emag: Any) -> Any:
    """First-order flux error of a magnitude error: ``fnu * ln(10)/2.5 * emag``."""
    f = np.asarray(fnu_jy, dtype=np.float64)
    e = np.asarray(emag, dtype=np.float64)
    out = np.abs(f) * LN10_OVER_2P5 * e
    out = np.where(np.isfinite(out) & (e >= 0), out, np.nan)
    return _out(out, fnu_jy, emag)


def flux_unit_factor(unit: str | None) -> float:
    """Factor converting a flux column in ``unit`` to Jy (``None``/empty means Jy)."""
    if unit is None or unit == "":
        return 1.0
    u = str(unit).strip()
    if u in FLUX_UNITS:
        return FLUX_UNITS[u]
    raise ValueError(f"unknown flux unit {unit!r}; known: {sorted(set(FLUX_UNITS))}")


def nufnu(wl_um: Any, fnu_jy: Any, unit: str = "W/m2") -> Any:
    """``nu * F_nu`` with ``nu = c / lambda``: W m^-2 (default) or erg s^-1 cm^-2."""
    w = np.asarray(wl_um, dtype=np.float64)
    f = np.asarray(fnu_jy, dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = f * 1e-26 * C_UM_S / w
    if unit in ("W/m2", "W/m^2", "W m-2"):
        pass
    elif unit in ("erg/s/cm2", "erg/s/cm^2", "erg s-1 cm-2"):
        out = out * 1e3
    else:
        raise ValueError(f"unknown nuFnu unit {unit!r} (use 'W/m2' or 'erg/s/cm2')")
    out = np.where(np.isfinite(w) & (w > 0), out, np.nan)
    return _out(out, wl_um, fnu_jy)


def blackbody_fnu(wl_um: Any, temp_k: float) -> Any:
    """Planck ``B_nu`` in W m^-2 Hz^-1 sr^-1 at ``wl_um`` (micron) for ``temp_k`` (K)."""
    w = np.asarray(wl_um, dtype=np.float64)
    nu = _C / (w * 1e-6)
    with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
        x = _H * nu / (_KB * float(temp_k))
        out = 2.0 * _H * nu ** 3 / _C ** 2 / np.expm1(x)
    out = np.where(np.isfinite(w) & (w > 0) & (float(temp_k) > 0), out, np.nan)
    return _out(out, wl_um)


# ---------------------------------------------------------------------------
# SED points
# ---------------------------------------------------------------------------

def _g(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _num(v: Any) -> float | None:
    """A finite float or None (bool, strings, None and NaN are missing)."""
    if v is None or isinstance(v, (bool, np.bool_, str, bytes)):
        return None
    if v is np.ma.masked:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


_LIM_CODES = {1: "ul", 2: "ll", "1": "ul", "2": "ll", "u": "ul", "l": "ll", "U": "ul", "L": "ll"}


def _limit_kind(row: Mapping, col: Any) -> str:
    key = _g(col, "key")
    for name in ("_lim", "_limits"):
        lim = row.get(name) if isinstance(row, Mapping) else None
        if isinstance(lim, Mapping) and key in lim:
            v = lim[key]
            if isinstance(v, (int, np.integer)) and not isinstance(v, bool):
                return _LIM_CODES.get(int(v), "det")
            if isinstance(v, str):
                return _LIM_CODES.get(v.strip(), "det")
    sib = _g(col, "limit")
    if sib and isinstance(row, Mapping):
        v = row.get(sib)
        if isinstance(v, str):
            return _LIM_CODES.get(v.strip(), "det")
        if isinstance(v, (int, np.integer)) and not isinstance(v, bool):
            return _LIM_CODES.get(int(v), "det")
    return "det"


def sed_points(row: Mapping[str, Any], filters: Mapping[str, Any],
               cols: Sequence[Any]) -> list[dict]:
    """SED points of one catalog row (Python twin of ``sedPoints``).

    ``filters`` is the ``filters`` object of a ``pk-filters/1`` document (or the document
    itself); ``cols`` are column metadata (manifest column dicts or ``ColumnSpec``) with
    ``key``, ``role``, ``filter`` and optionally ``err``, ``unit`` and ``limit``.

    Returns ``[{wl, fnu, err, kind, key, filter}]`` sorted by ``wl``; ``fnu``/``err`` in Jy.
    """
    fl = filters.get("filters", filters) if isinstance(filters, Mapping) else filters
    pts: list[dict] = []
    for c in cols:
        role = _g(c, "role")
        if role not in ("mag", "flux"):
            continue
        fk = _g(c, "filter")
        if not fk or fk not in fl:
            continue
        f = fl[fk]
        wl = _num(_g(f, "wl_um"))
        if wl is None or wl <= 0:
            continue
        key = _g(c, "key")
        v = _num(row.get(key)) if isinstance(row, Mapping) else None
        if v is None:
            continue
        kind = _limit_kind(row, c)
        ek = _g(c, "err")
        ev = _num(row.get(ek)) if ek else None
        if role == "mag":
            zp = _num(_g(f, "zp_jy"))
            if zp is None or zp <= 0:
                continue
            try:
                fnu = zp * 10.0 ** (-0.4 * v)
            except OverflowError:  # e.g. a -999 sentinel: no finite flux
                continue
            if not math.isfinite(fnu) or fnu <= 0:
                continue
            err = fnu * LN10_OVER_2P5 * ev if (ev is not None and ev >= 0) else None
        else:
            fac = flux_unit_factor(_g(c, "unit"))
            fnu = v * fac
            if fnu <= 0 or not math.isfinite(fnu):
                continue
            err = abs(ev) * fac if ev is not None else None
        if kind != "det":
            err = None
        pts.append({"wl": wl, "fnu": fnu, "err": err, "kind": kind, "key": key, "filter": fk})
    pts.sort(key=lambda p: p["wl"])
    return pts


def interp_linear(x: Any, xp: Any, fp: Any) -> np.ndarray:
    """Linear interpolation, NaN outside ``[xp[0], xp[-1]]`` (``xp`` increasing)."""
    xp = np.asarray(xp, dtype=np.float64)
    fp = np.asarray(fp, dtype=np.float64)
    x = np.asarray(x, dtype=np.float64)
    if xp.size == 0:
        return np.full(x.shape, np.nan)
    return np.interp(x, xp, fp, left=np.nan, right=np.nan)


def scale_to(model_wave: Any, model_flux: Any, points: Iterable[Mapping],
             bands: Sequence[str] | None = None) -> float:
    """Median of ``fnu / model(wl)`` over detected points (Python twin of ``scaleTo``).

    ``model(wl)`` is the model linearly interpolated at the point wavelength. Points are
    used when ``kind == "det"``, ``fnu > 0`` and the model value is finite and positive.
    With ``bands``, only points whose ``filter`` or ``key`` is listed are used. Returns NaN
    when no point is usable.
    """
    sel = set(bands) if bands else None
    wl, ob = [], []
    for p in points:
        if p.get("kind", "det") != "det":
            continue
        if sel is not None and p.get("filter") not in sel and p.get("key") not in sel:
            continue
        f = _num(p.get("fnu"))
        w = _num(p.get("wl"))
        if f is None or w is None or f <= 0:
            continue
        wl.append(w)
        ob.append(f)
    if not wl:
        return float("nan")
    mv = interp_linear(np.asarray(wl), model_wave, model_flux)
    ratios = np.asarray(ob) / mv
    ok = np.isfinite(mv) & (mv > 0)
    if not np.any(ok):
        return float("nan")
    return float(np.median(ratios[ok]))
