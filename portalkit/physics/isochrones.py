"""MIST isochrones -> ``Track``/``IsochroneSpec`` -> ``iso.<h8>.json`` (``pk-isochrones/1``).

ARCHITECTURE.md sections 5.10, 6.6 (``trackXY``) and 9.4.

:func:`load_mist_table` is the WD2_Portal_Manifold loader (header names from the ``# EEP``
line). :func:`build_isochrone_track` follows WD2 ``build_isochrone_track`` (nearest log age,
phase 9 dropped, sorted by ``star_mass``) and optionally thins the track evenly in log mass
as W51_Portal_ColorColor does (``npt``).

Track magnitudes are **absolute** magnitudes (MIST, at the file's own A_V, normally 0),
keyed by filter registry key (``NIRCAM_F410M`` or a bare ``F410M`` column in a
``JWST NIRCAM`` file becomes ``JWST/NIRCam.F410M``). :func:`track_xy` places a track on a
plot::

    m_app = M + 5 log10(dist_pc / 10) + dm + A_V * k(filter, R_V)

The extinction term is only added when ``av != 0`` (a filter with unknown k does not hide an
unreddened track).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from . import PhysicsResult, _writer

try:
    from ..spec import IsochroneSpec, Track
except ImportError:  # spec.py (WP03) not present: same-shaped local dataclasses
    @dataclass
    class Track:  # type: ignore[no-redef]
        label: str
        age_myr: float
        dist_pc: float
        feh: float
        mass: np.ndarray
        mags: dict = field(default_factory=dict)

    @dataclass
    class IsochroneSpec:  # type: ignore[no-redef]
        tracks: list

__all__ = ["DEFAULT_MIST", "Track", "IsochroneSpec", "load_mist_table", "mist_header",
           "mist_filter_key", "mag_columns", "build_isochrone_track", "isochrone_spec",
           "isochrones_document", "isochrones_bytes", "write", "track_xy", "thin_log_mass"]

DEFAULT_MIST = Path("/orange/adamginsburg/jwst/isochrones/MIST_iso_69cd1ed13b15a.iso.JWST")

_IRAC = {"3.6": "I1", "4.5": "I2", "5.8": "I3", "8.0": "I4"}
_BAND = re.compile(r"F\d{3,4}[A-Z]\d?\Z")
#: MIST JWST column suffixes that do not match the real NIRCam filter names
_MIST_NIRCAM_FIX = {"F140W": "F140M", "F470W": "F470N", "F480W": "F480M"}


def load_mist_table(path: str | Path):
    """Read a MIST ASCII isochrone file using the column names in its own header line
    (verbatim from WD2_Portal_Manifold.load_mist_table)."""
    from astropy.table import Table
    with open(path) as fh:
        header_line = next(line for line in fh if line.startswith('# EEP'))
    names = header_line.lstrip('#').split()
    return Table.read(str(path), format='ascii.no_header', comment='#', names=names)


def mist_header(path: str | Path) -> dict:
    """``{version, system, feh, av, rv}`` from a MIST file's comment header (None if absent)."""
    out: dict[str, Any] = {"version": None, "system": None, "feh": None, "av": None, "rv": None}
    with open(path) as fh:
        lines = []
        for line in fh:
            if not line.startswith("#"):
                break
            lines.append(line.rstrip("\n"))
    for i, line in enumerate(lines):
        s = line.lstrip("#").strip()
        if s.startswith("MIST version number"):
            out["version"] = s.split("=", 1)[1].strip()
        elif s.startswith("photometric system"):
            out["system"] = s.split("=", 1)[1].strip()
        elif s.startswith("Yinit") and i + 1 < len(lines):
            hdr = s.split()
            vals = lines[i + 1].lstrip("#").split()
            if "[Fe/H]" in hdr and len(vals) == len(hdr):
                out["feh"] = float(vals[hdr.index("[Fe/H]")])
        elif "extinction" in s and "=" in s:
            lhs, rhs = s.split("=", 1)
            nums = [float(x) for x in rhs.split()]
            names = [n.strip() for n in lhs.split(":")[-1].split(",")]
            for n, v in zip(names, nums):
                if n.lower() == "av":
                    out["av"] = v
                elif n.lower() == "rv":
                    out["rv"] = v
    return out


def _jwst_key(inst: str, band: str) -> str:
    band = band.upper()
    if inst == "NIRCam":
        band = _MIST_NIRCAM_FIX.get(band, band)
    return f"JWST/{inst}.{band}"


def mist_filter_key(col: str, system: str | None = None) -> str | None:
    """Filter registry key of a MIST magnitude column, or None for non-photometric columns.

    The MIST JWST tables label three NIRCam bands with a wrong suffix (``F140W``, ``F470W``,
    ``F480W``); they map to the real filters ``F140M``, ``F470N`` and ``F480M``.
    """
    c = str(col)
    sysu = (system or "").upper()
    m = re.match(r"(NIRCAM|MIRI|NIRISS)_(F\w+)\Z", c, re.IGNORECASE)
    if m:
        inst = {"NIRCAM": "NIRCam", "MIRI": "MIRI", "NIRISS": "NIRISS"}[m.group(1).upper()]
        return _jwst_key(inst, m.group(2))
    if _BAND.match(c.upper()):
        for token, inst in (("NIRCAM", "NIRCam"), ("MIRI", "MIRI"), ("NIRISS", "NIRISS")):
            if token in sysu:
                return _jwst_key(inst, c)
        return None
    m = re.match(r"2MASS_(J|H|Ks)\Z", c)
    if m:
        return f"2MASS/2MASS.{m.group(1)}"
    m = re.match(r"IRAC_(3\.6|4\.5|5\.8|8\.0)\Z", c)
    if m:
        return f"Spitzer/IRAC.{_IRAC[m.group(1)]}"
    m = re.match(r"(?:Roman|WFI)_(F\d{3})\Z", c)
    if m:
        return f"Roman/WFI.{m.group(1)}"
    return None


def mag_columns(colnames: Iterable[str], system: str | None = None) -> dict[str, str]:
    """``{MIST column: filter key}`` for the photometric columns."""
    out = {}
    for c in colnames:
        k = mist_filter_key(c, system)
        if k is not None and k not in out.values():
            out[c] = k
    return out


def thin_log_mass(mass: np.ndarray, npt: int) -> np.ndarray:
    """Indices thinning a mass-sorted track to <= ``npt`` points evenly in log mass
    (W51_Portal_ColorColor)."""
    mass = np.asarray(mass, dtype=np.float64)
    if npt is None or mass.size <= npt:
        return np.arange(mass.size)
    lm = np.log10(mass)
    edges = np.linspace(lm.min(), lm.max(), int(npt))
    return np.unique(np.searchsorted(lm, edges).clip(0, mass.size - 1))


def _resolve_wanted(wanted: Sequence[str] | None, colmap: Mapping[str, str]) -> dict[str, str]:
    if not wanted:
        return dict(colmap)
    from .filters import resolve_key
    by_key = {v: c for c, v in colmap.items()}
    out = {}
    for w in wanted:
        if w in colmap:
            out[w] = colmap[w]
            continue
        k = resolve_key(w, list(by_key))
        if k is None:
            raise KeyError(f"isochrone has no magnitude column for filter {w!r}")
        out[by_key[k]] = k
    return out


def build_isochrone_track(path: str | Path = DEFAULT_MIST, age_log10_yr: float = 6.0,
                          dist_pc: float = 10.0, filters: Sequence[str] | None = None, *,
                          drop_phases: Sequence[int] = (9,), mass_col: str = "star_mass",
                          npt: int | None = None, label: str | None = None,
                          feh: float | None = None, table: Any = None,
                          mag_decimals: int = 4) -> Track:
    """A ``Track`` from a MIST isochrone file (nearest log age to ``age_log10_yr``).

    ``filters`` selects magnitude columns by registry key, MIST column name or short name
    (default: every photometric column). Masses are sorted ascending; magnitudes are
    absolute, rounded to ``mag_decimals``. ``dist_pc`` is stored on the track for
    :func:`track_xy`.
    """
    hdr = mist_header(path)
    t = table if table is not None else load_mist_table(path)
    ages = np.array(sorted(set(np.asarray(t['log10_isochrone_age_yr'], dtype=float))))
    if ages.size == 0:
        raise ValueError(f"{path}: no isochrones")
    age = ages[np.abs(ages - float(age_log10_yr)).argmin()]
    rows = t[np.asarray(t['log10_isochrone_age_yr'], dtype=float) == age]
    if "phase" in rows.colnames and drop_phases:
        ph = np.asarray(rows['phase'], dtype=float)
        rows = rows[~np.isin(ph, np.asarray(list(drop_phases), dtype=float))]
    rows = rows[np.argsort(np.asarray(rows[mass_col], dtype=float), kind="stable")]
    mass = np.asarray(rows[mass_col], dtype=float)
    keep = thin_log_mass(mass, npt) if npt else np.arange(mass.size)
    mass = mass[keep]
    cmap = _resolve_wanted(filters, mag_columns(rows.colnames, hdr.get("system")))
    if not cmap:
        raise ValueError(f"{path}: no photometric columns found")
    mags = {}
    for col, key in cmap.items():
        v = np.asarray(rows[col], dtype=float)[keep]
        mags[key] = np.round(v, mag_decimals)
    age_myr = float(10.0 ** age / 1e6)
    if feh is None:
        feh = hdr.get("feh")
    if feh is None:
        if "[Fe/H]_init" in rows.colnames and len(rows):
            feh = float(rows["[Fe/H]_init"][0])
        else:
            feh = 0.0
    if label is None:
        label = f"MIST {_fmt_age(age_myr)}"
    return Track(label=label, age_myr=float(f"{age_myr:.6g}"), dist_pc=float(dist_pc),
                 feh=float(feh), mass=np.asarray([float(f"{m:.6g}") for m in mass]), mags=mags)


def _fmt_age(age_myr: float) -> str:
    if age_myr >= 1000:
        return f"{age_myr / 1000:g} Gyr"
    return f"{float(f'{age_myr:.3g}'):g} Myr"


def isochrone_spec(path: str | Path = DEFAULT_MIST, ages_myr: Sequence[float] = (1.0,),
                   dist_pc: float = 10.0, filters: Sequence[str] | None = None, *,
                   npt: int | None = None, labels: Sequence[str] | None = None,
                   **kw: Any) -> IsochroneSpec:
    """An ``IsochroneSpec`` with one track per age (the file is read once)."""
    t = load_mist_table(path)
    tracks = []
    for i, a in enumerate(ages_myr):
        lab = labels[i] if labels else None
        tracks.append(build_isochrone_track(path, math.log10(float(a) * 1e6), dist_pc, filters,
                                            npt=npt, label=lab, table=t, **kw))
    return IsochroneSpec(tracks=tracks)


def _numlist(a: Any) -> list[float | None]:
    arr = np.asarray(a, dtype=np.float64).ravel()
    return [float(x) if math.isfinite(x) else None for x in arr]


def _tg(t: Any, k: str) -> Any:
    return t[k] if isinstance(t, Mapping) else getattr(t, k)


def isochrones_document(spec: Any) -> dict:
    """``pk-isochrones/1`` document (same structure and key order as
    ``portalkit.config.isochrones_document``)."""
    from ..schema import validate_isochrones
    tracks = []
    for t in _tg(spec, "tracks"):
        tracks.append({"label": _tg(t, "label"), "age_myr": float(_tg(t, "age_myr")),
                       "dist_pc": float(_tg(t, "dist_pc")), "feh": float(_tg(t, "feh")),
                       "mass": _numlist(_tg(t, "mass")),
                       "mags": {k: _numlist(v) for k, v in _tg(t, "mags").items()}})
    doc = {"schema": "pk-isochrones/1", "tracks": tracks}
    errs = validate_isochrones(doc)
    if errs:
        raise ValueError("isochrones document invalid: " + "; ".join(errs[:10]))
    return doc


def isochrones_bytes(spec: Any) -> bytes:
    from ..bundle.writer import dumps_json
    return dumps_json(isochrones_document(spec))


def write(spec: Any, dest: Any, *, ledger: Any = None) -> PhysicsResult:
    """Write ``iso.<h8>.json`` into ``dest`` (the page's ``data/`` directory)."""
    w = _writer(dest, ledger)
    doc = isochrones_document(spec)
    path = w.put_json("", "iso", doc, kind="isochrones")
    return PhysicsResult(path=path, doc=doc, files=[path])


def track_xy(track: Any, x_expr: str, y_expr: str, av: float, rv: float, dm: float,
             filters: Mapping, colmap: Mapping[str, str] | None = None,
             ext: Mapping | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Python twin of ``trackXY``: (x, y) float32 arrays of a track for two expressions.

    Expression keys resolve to the track's ``mags`` keys with
    :func:`filters.resolve_key` (``colmap`` first, then exact/short-name matching). An
    unresolved key gives an all-NaN axis. For ``av != 0`` the filter entry is the mags key
    resolved in ``filters`` (a full key also finds a short-keyed document through
    :func:`filters.short_name`), else the expression key resolved in ``filters`` with
    ``colmap``; no entry gives NaN.
    """
    from ..expr import compile as compile_expr
    from .extinction import _filters_map, a_lambda
    from .filters import resolve_key
    fl = _filters_map(filters)
    mags = _tg(track, "mags")
    n = len(_tg(track, "mass"))
    dist = float(_tg(track, "dist_pc"))
    mu = 5.0 * math.log10(dist / 10.0) if dist > 0 else float("nan")
    out = []
    for expr in (x_expr, y_expr):
        c = compile_expr(expr)
        v = np.full(n, float(c["c"]), dtype=np.float64)
        for k, wgt in c["w"].items():
            mk = resolve_key(k, list(mags), colmap)
            if mk is None:
                v[:] = np.nan
                continue
            m = np.asarray([np.nan if x is None else x for x in mags[mk]], dtype=np.float64)
            shift = mu + float(dm)
            if float(av) != 0.0:
                fk = resolve_key(mk, fl)
                if fk is None:
                    fk = resolve_key(k, fl, colmap)
                shift += a_lambda(fl[fk], av, rv, ext) if fk is not None else float("nan")
            v = v + wgt * (m + shift)
        out.append(v.astype(np.float32))
    return out[0], out[1]
