"""Generate the physics golden files consumed by tests/js/physics.test.mjs (WP05).

Usage::

    python tests/golden/physics/generate.py           # (re)write the JSON files
    python tests/golden/physics/generate.py --check   # exit 1 if any file differs

Every value comes from the Python reference implementation in ``portalkit.physics``.
JSON numbers are float64 (``null`` = NaN / missing). JS results must match to ``rtol``
(1e-6 relative; values whose expected magnitude is below ``atol`` compare absolutely).
See ``index.json`` for the conventions each file encodes.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))

from portalkit.physics import extinction as ext_mod  # noqa: E402
from portalkit.physics import filters as flt_mod  # noqa: E402
from portalkit.physics import isochrones as iso_mod  # noqa: E402
from portalkit.physics import sed as sed_mod  # noqa: E402

RTOL = 1e-6
ATOL = 1e-12

NAN = float("nan")


def _j(v):
    """JSON-safe: NaN/inf -> None, numpy -> python."""
    if isinstance(v, dict):
        return {str(k): _j(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_j(x) for x in v]
    if isinstance(v, np.ndarray):
        return [_j(x) for x in v.tolist()]
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return f if math.isfinite(f) else None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, np.bool_):
        return bool(v)
    return v


def _n(v):
    """JSON input value -> float (None -> NaN)."""
    return NAN if v is None else float(v)


# ---------------------------------------------------------------------------
# shared inputs
# ---------------------------------------------------------------------------

GOLDEN_FILTER_KEYS = ["JWST/NIRCam.F182M", "JWST/NIRCam.F212N", "JWST/NIRCam.F410M",
                      "JWST/NIRCam.F444W", "2MASS/2MASS.Ks", "JWST/MIRI.F1500W",
                      "Spitzer/MIPS.70mu"]


def golden_filters() -> dict:
    """A small ``filters`` object: registry entries plus three synthetic ones that exercise
    the aLambda fallbacks (curve lookup, 1/R_V interpolation from a_over_av, no data)."""
    doc, _ = flt_mod.filters_document(type("S", (), {"keys": list(GOLDEN_FILTER_KEYS),
                                                     "extra": {}})())
    fl = doc["filters"]
    fl["TEST/Curve.C300"] = {"label": "C300", "instrument": "TEST", "wl_um": 3.0, "weff_um": None,
                             "zp_jy": 350.0, "system": "Vega",
                             "a_over_av": {"G23_3.1": None, "G23_5.5": None}}
    fl["TEST/Pair.P250"] = {"label": "P250", "instrument": "TEST", "wl_um": 2.5, "weff_um": 0.2,
                            "zp_jy": 500.0, "system": "AB",
                            "a_over_av": {"G23_3.1": 0.082, "G23_5.5": 0.1366}}
    fl["TEST/None.N900"] = {"label": "N900", "instrument": "TEST", "wl_um": 900.0, "weff_um": None,
                            "zp_jy": 0.01, "system": "Vega", "a_over_av": {}}
    return fl


def golden_ext() -> dict:
    return ext_mod.extinction_document(None)


# ---------------------------------------------------------------------------
# mag <-> Jy, nuFnu, blackbody
# ---------------------------------------------------------------------------

def gen_mag_jy() -> dict:
    mag_cases = [(0.0, 3631.0), (10.0, 208.75045), (18.21, 844.94329), (-1.5, 17.11778),
                 (25.0, 2768.4046), (40.0, 1.0), (None, 208.75), (12.0, None), (12.0, 0.0),
                 (300.0, 100.0), (-300.0, 100.0), (1e-9, 1.0)]
    m2j = []
    for m, z in mag_cases:
        m2j.append({"mag": m, "zp_jy": z, "jy": sed_mod.mag_to_jy(_n(m), _n(z))})
    jy_cases = [(1.0, 3631.0), (2.0875045e-2, 208.75045), (0.0, 100.0), (-1.0, 100.0),
                (None, 100.0), (1e-30, 1.0), (1e30, 1.0), (5.0, None), (1e-3, 844.94329)]
    j2m = []
    for j, z in jy_cases:
        j2m.append({"jy": j, "zp_jy": z, "mag": sed_mod.jy_to_mag(_n(j), _n(z))})
    err = []
    for f, e in [(1.0, 0.1), (2.5e-3, 0.02), (1.0, None), (None, 0.1), (3.0, -0.1)]:
        err.append({"fnu_jy": f, "emag": e, "err_jy": sed_mod.mag_err_to_jy(_n(f), _n(e))})
    return _j({"description": "magToJy(mag, zpJy) = zpJy*10**(-0.4*mag); jyToMag(jy, zpJy) = "
                              "-2.5*log10(jy/zpJy), null for jy <= 0, zp <= 0 or missing input; "
                              "magErrToJy(fnu, emag) = |fnu|*ln(10)/2.5*emag (null for emag < 0)",
               "mag_to_jy": m2j, "jy_to_mag": j2m, "mag_err_to_jy": err})


def gen_nufnu() -> dict:
    cases = []
    for w, f in [(1.0, 1.0), (4.082238, 0.0123), (15.06351, 2.5e-3), (0.6, 3631.0), (2.0, None),
                 (0.0, 1.0), (-1.0, 1.0), (1e3, 1e3)]:
        cases.append({"wl_um": w, "fnu_jy": f, "nufnu_w_m2": sed_mod.nufnu(_n(w), _n(f)),
                      "nufnu_erg_s_cm2": sed_mod.nufnu(_n(w), _n(f), "erg/s/cm2")})
    bb = []
    for w, t in [(1.0, 5750.0), (2.2, 3000.0), (10.0, 300.0), (0.5, 30000.0), (100.0, 20.0),
                 (0.1, 100.0), (5.0, 0.0)]:
        bb.append({"wl_um": w, "temp_k": t, "bnu_si": sed_mod.blackbody_fnu(w, t)})
    units = {u: sed_mod.flux_unit_factor(u) for u in ("Jy", "mJy", "uJy", "µJy", "nJy", "MJy",
                                                       "erg/s/cm2/Hz", "W/m2/Hz")}
    return _j({"description": "nuFnu = fnu*1e-26*c/lambda with c = 2.99792458e14 um/s (W m^-2); "
                              "erg s^-1 cm^-2 = W m^-2 * 1e3; null for wl <= 0. blackbody B_nu in "
                              "W m^-2 Hz^-1 sr^-1 (h=6.62607015e-34, c=2.99792458e8, "
                              "k=1.380649e-23); null for T <= 0. flux_units: factor to Jy.",
               "c_um_s": sed_mod.C_UM_S, "nufnu": cases, "blackbody": bb, "flux_units": units})


# ---------------------------------------------------------------------------
# SED points
# ---------------------------------------------------------------------------

def gen_sed_points(filters: dict) -> dict:
    cols = [
        {"key": "f182m", "role": "mag", "filter": "JWST/NIRCam.F182M", "err": "e_f182m"},
        {"key": "e_f182m", "role": "error"},
        {"key": "f410m", "role": "mag", "filter": "JWST/NIRCam.F410M", "err": "e_f410m"},
        {"key": "e_f410m", "role": "error"},
        {"key": "f212n", "role": "mag", "filter": "JWST/NIRCam.F212N", "err": "e_f212n",
         "limit": "k_f212n"},
        {"key": "e_f212n", "role": "error"},
        {"key": "k_f212n", "role": "limit"},
        {"key": "ks", "role": "mag", "filter": "2MASS/2MASS.Ks"},
        {"key": "f1500w_mjy", "role": "flux", "filter": "JWST/MIRI.F1500W", "unit": "mJy",
         "err": "e_f1500w_mjy"},
        {"key": "e_f1500w_mjy", "role": "error"},
        {"key": "f444w_flux", "role": "flux", "filter": "JWST/NIRCam.F444W", "unit": "uJy"},
        {"key": "nofilter", "role": "mag"},
        {"key": "unknownfilter", "role": "mag", "filter": "NOPE/NOPE.X"},
        {"key": "notamag", "role": "value", "filter": "JWST/NIRCam.F410M"},
    ]
    rows = [
        {"id": "all", "f182m": 15.0, "e_f182m": 0.05, "f410m": 12.3, "e_f410m": 0.02,
         "f212n": 14.1, "e_f212n": 0.1, "k_f212n": "", "ks": 13.9, "f1500w_mjy": 12.5,
         "e_f1500w_mjy": 0.5, "f444w_flux": 850.0, "nofilter": 1.0, "unknownfilter": 2.0,
         "notamag": 3.0},
        {"id": "limits", "f182m": 21.0, "e_f182m": 0.3, "f410m": 16.0, "e_f410m": None,
         "f212n": 19.5, "e_f212n": 0.2, "k_f212n": "u", "ks": None, "f1500w_mjy": 0.8,
         "e_f1500w_mjy": 0.1, "f444w_flux": -5.0, "_lim": {"f182m": 2, "f1500w_mjy": 1}},
        {"id": "missing", "f182m": None, "f410m": None, "f212n": None, "ks": None,
         "f1500w_mjy": 0.0, "f444w_flux": None},
        {"id": "big", "f182m": -3.0, "e_f182m": 0.0, "f410m": 35.0, "e_f410m": 5.0,
         "f212n": 14.0, "e_f212n": -1.0, "k_f212n": "l", "ks": 9.0, "f1500w_mjy": 1e6,
         "f444w_flux": 1e-3},
    ]
    rows += [
        {"id": "sentinels", "f182m": -999.0, "e_f182m": 0.1, "f410m": 999.0, "e_f410m": 0.1,
         "f212n": 14.0, "e_f212n": 0.1, "ks": 12.0, "f1500w_mjy": 1.0,
         "_limits": {"ks": 1, "f212n": 2}},
    ]
    cases = []
    for r in rows:
        cases.append({"row": r, "points": sed_mod.sed_points(r, filters, cols)})
    str_rows = [
        {"id": "string_digits", "f182m": 15.0, "e_f182m": 0.05, "f410m": 12.3, "e_f410m": 0.02,
         "f212n": 14.1, "e_f212n": 0.1, "k_f212n": "2", "ks": 13.9, "f1500w_mjy": 1.0,
         "_lim": {"f182m": "1", "f410m": " 2 ", "f1500w_mjy": "0"}},
        {"id": "string_digit_sibling", "f212n": 14.1, "e_f212n": 0.1, "k_f212n": "1",
         "f182m": 15.0, "_lim": {"f182m": "x"}},
    ]
    str_cases = []
    for r in str_rows:
        str_cases.append({"row": r, "points": sed_mod.sed_points(r, filters, cols)})
    return _j({"description": "sedPoints(row, filters, cols): one point per mag/flux column with "
                              "a filter present in filters and a finite value; mag -> "
                              "zp*10**(-0.4 m) (a non-finite result, e.g. from a -999 sentinel, "
                              "is skipped), err = fnu*ln10/2.5*e (null if e missing or < 0); "
                              "flux -> value*unitFactor (Jy, mJy, uJy ...), err = |e|*factor; "
                              "non-positive flux skipped. kind: row._lim[colKey], then "
                              "row._limits[colKey]: integer 1 or string '1' -> 'ul', 2 or '2' "
                              "-> 'll' (strings trimmed), any other value -> 'det'; else the "
                              "column's limit sibling 'u'/'U'/'1'/1 -> 'ul', 'l'/'L'/'2'/2 -> "
                              "'ll', else 'det'; limits have err null. Sorted by wl (stable). "
                              "Extra fields key/filter name the source column and filter. "
                              "cases_string_codes exercise the string digit codes '1'/'2'.",
               "filters": filters, "cols": cols, "cases": cases,
               "cases_string_codes": str_cases})


# ---------------------------------------------------------------------------
# reddening
# ---------------------------------------------------------------------------

def gen_reddening(filters: dict, ext: dict) -> dict:
    al = []
    for fk in list(filters) + ["NOPE/NOPE.X"]:
        for av, rv in [(1.0, 3.1), (10.0, 5.5), (30.0, 4.0), (0.0, 3.1), (5.0, 2.5)]:
            al.append({"filter": fk, "av": av, "rv": rv,
                       "a_lambda": ext_mod.a_lambda(fk, av, rv, ext, filters)})
    vec = []
    for x, y, av, rv, colmap in [
        ("f182m - f410m", "f410m", 10.0, 3.1, None),
        ("f182m-f212n", "f212n-f410m", 1.0, 5.5, None),
        ("0.5*f182m + 0.5*f410m - f444w", "f444w", 25.0, 4.0, None),
        ("a - b", "b", 3.0, 3.1, {"a": "JWST/NIRCam.F182M", "b": "JWST/NIRCam.F410M"}),
        ("f182m - nope", "2.5", 1.0, 3.1, None),
        ("C300 - P250", "N900", 2.0, 3.1, None),
        ("f182m - f410m + 7", "f410m - f410m", 1.0, 3.1, None),
        ("nircam_f182m - F410M", "mag_f410m", 2.0, 3.1, None),
    ]:
        v = ext_mod.reddening_vector(x, y, av, rv, filters, colmap, ext)
        case = {"x": x, "y": y, "av": av, "rv": rv, "dx": v["dx"], "dy": v["dy"]}
        if colmap:
            case["colmap"] = colmap
        vec.append(case)
    law_w = [1.0, 2.0, 3.0, 4.0, 5.0]
    law_k = [0.3, 0.12, 0.06, 0.04, 0.03]
    red = []
    for wave, flux, av in [([1.0, 1.5, 2.0, 4.5, 5.0], [1.0, 2.0, 3.0, 4.0, 5.0], 10.0),
                           ([0.5, 1.0, 5.0, 6.0], [1.0, 1.0, 1.0, 1.0], 1.0),
                           ([2.5, 3.5], [1e-20, 1e20], 50.0),
                           ([1.0, 2.0, 3.0], [1.0, None, 3.0], 0.0)]:
        out = ext_mod.redden(wave, [_n(f) for f in flux], law_w, law_k, av)
        red.append({"wave": wave, "flux": flux, "av": av,
                    "out": [float(x) for x in out.astype(np.float64)]})
    rvi = []
    for rv in (2.3, 3.1, 4.0, 5.5, 5.6):
        rvi.append({"rv": rv, "k31": 0.1, "k55": 0.2,
                    "k": ext_mod.interp_rv(rv, {3.1: 0.1, 5.5: 0.2})})
    curve = []
    for wl in (0.0912, 0.55, 1.0, 2.2, 4.082238, 9.7, 31.9, 32.0, 40.0, 0.05):
        for rv in (3.1, 5.5, 4.0):
            curve.append({"wl_um": wl, "rv": rv,
                          "k": ext_mod.entry_k({"wl_um": wl, "a_over_av": {}}, rv, ext)})
    return _j({"description": "aLambda(filter, av, rv, ext) = av*k with k from: filters[f]."
                              "a_over_av['G23_'+rv] if non-null; else linear in 1/rv between the "
                              "entry's two G23_* values; else ext.curve at wl_um (curve for "
                              "String(rv) if present, else 1/rv interpolation of the two curves, "
                              "each linear in wavelength, null outside); else null. Unknown "
                              "filter -> null. vector(x, y, av, rv, filters): sum of expression "
                              "weight * aLambda; expression keys resolve with resolveKey(key, filters, colmap) (full "
                              "order in resolve_key.json: colmap, exact, unique case-insensitive, "
                              "unique alias, then short-name probes with a label tie-break); "
                              "constants do not contribute; unresolved key -> null. "
                              "redden(flux, law, av) = flux*10**(-0.4*k*av), k linear in "
                              "wavelength, null outside the law (float32). rv keys use "
                              "Number.toString-like %g formatting ('3.1', '5.5').",
               "filters": filters, "ext": ext, "a_lambda": al, "vector": vec,
               "redden": {"law": {"w": law_w, "k": law_k}, "cases": red},
               "interp_rv": rvi, "curve_k": curve})


# ---------------------------------------------------------------------------
# scaleTo
# ---------------------------------------------------------------------------

def gen_scale_to() -> dict:
    model = {"wave": [1.0, 2.0, 3.0, 4.0, 5.0], "flux": [2.0, 1.0, 0.5, 0.25, 0.2]}
    pts = [
        {"wl": 1.5, "fnu": 3.0, "err": 0.1, "kind": "det", "key": "a", "filter": "F1"},
        {"wl": 2.0, "fnu": 2.0, "err": None, "kind": "det", "key": "b", "filter": "F2"},
        {"wl": 3.5, "fnu": 0.9, "err": 0.1, "kind": "det", "key": "c", "filter": "F3"},
        {"wl": 4.5, "fnu": 10.0, "err": None, "kind": "ul", "key": "d", "filter": "F4"},
        {"wl": 6.0, "fnu": 1.0, "err": 0.1, "kind": "det", "key": "e", "filter": "F5"},
        {"wl": 2.5, "fnu": 0.0, "err": 0.1, "kind": "det", "key": "f", "filter": "F6"},
    ]
    cases = []
    for bands in (None, ["F1", "F3"], ["b"], ["F4"], ["F5"], ["F1", "c", "F2"]):
        c = {"points": pts, "scale": sed_mod.scale_to(model["wave"], model["flux"], pts, bands)}
        if bands is not None:
            c["bands"] = bands
        cases.append(c)
    cases.append({"points": [], "scale": sed_mod.scale_to(model["wave"], model["flux"], [])})
    return _j({"description": "scaleTo(model{wave,flux}, points, bands?): median over points with "
                              "kind 'det', fnu > 0 and (bands absent or filter/key in bands) of "
                              "fnu / model(wl), model linear in wavelength and finite > 0 "
                              "(null outside); median of an even count is the mean of the middle "
                              "two; null when nothing is usable.",
               "model": model, "cases": cases})


# ---------------------------------------------------------------------------
# trackXY
# ---------------------------------------------------------------------------

def gen_track_xy(filters: dict, ext: dict) -> dict:
    track = {"label": "golden 1 Myr", "age_myr": 1.0, "dist_pc": 5400.0, "feh": 0.0,
             "mass": [0.1, 0.5, 1.0, 2.0, 8.0],
             "mags": {"JWST/NIRCam.F182M": [8.1, 5.2, 3.9, 1.8, -1.2],
                      "JWST/NIRCam.F410M": [7.6, 4.9, 3.7, 1.75, -1.25],
                      "JWST/NIRCam.F212N": [7.9, 5.0, None, 1.7, -1.3],
                      "TEST/None.N900": [1.0, 1.0, 1.0, 1.0, 1.0]}}
    cases = []
    for x, y, av, rv, dm, colmap in [
        ("f182m - f410m", "f410m", 0.0, 3.1, 0.0, None),
        ("f182m - f410m", "f410m", 20.0, 3.1, 0.0, None),
        ("f182m-f212n", "f212n", 5.0, 5.5, 1.5, None),
        ("a - b", "b + 1", 10.0, 4.0, -0.5, {"a": "JWST/NIRCam.F182M", "b": "JWST/NIRCam.F410M"}),
        ("f182m - nope", "f410m", 1.0, 3.1, 0.0, None),
        ("N900", "f410m", 0.0, 3.1, 0.0, None),
        ("N900", "f410m", 1.0, 3.1, 0.0, None),
    ]:
        xs, ys = iso_mod.track_xy(track, x, y, av, rv, dm, filters, colmap, ext)
        c = {"x_expr": x, "y_expr": y, "av": av, "rv": rv, "dm": dm,
             "x": [float(v) for v in xs.astype(np.float64)],
             "y": [float(v) for v in ys.astype(np.float64)]}
        if colmap:
            c["colmap"] = colmap
        cases.append(c)
    short_filters = {"F182M": dict(filters["JWST/NIRCam.F182M"]),
                     "F410M": dict(filters["JWST/NIRCam.F410M"])}
    short_cases = []
    for x, y, av, rv, dm, colmap in [
        ("f182m - f410m", "f410m", 0.0, 3.1, 0.0, None),
        ("f182m - f410m", "f410m", 20.0, 3.1, 0.0, None),
        ("f182m-f212n", "f212n", 5.0, 5.5, 0.0, None),
        ("a - b", "b", 3.0, 3.1, 0.0, {"a": "JWST/NIRCam.F182M", "b": "JWST/NIRCam.F410M"}),
    ]:
        xs, ys = iso_mod.track_xy(track, x, y, av, rv, dm, short_filters, colmap, ext)
        c = {"x_expr": x, "y_expr": y, "av": av, "rv": rv, "dm": dm,
             "x": [float(v) for v in xs.astype(np.float64)],
             "y": [float(v) for v in ys.astype(np.float64)]}
        if colmap:
            c["colmap"] = colmap
        short_cases.append(c)
    return _j({"description": "trackXY(track, xExpr, yExpr, av, rv, dm, filters): each "
                              "expression is c + sum w_k*(M_k + 5*log10(dist_pc/10) + dm + "
                              "(av != 0 ? aLambda(k, av, rv, ext) : 0)); expression keys resolve "
                              "against track.mags keys with resolveKey(key, Object.keys(track.mags), "
                              "colmap) (no label tie-break: plain key list); unresolved key or "
                              "null magnitude -> null. For av != 0 the filter entry is "
                              "resolveKey(magsKey, filters) (a full key also finds a short-keyed "
                              "filters object through shortName), else resolveKey(exprKey, "
                              "filters, colmap); no entry -> null. Float32. cases_short_filters "
                              "use short_filters (keys 'F182M', 'F410M') in place of filters.",
               "filters": filters, "ext_note": "uses reddening.json ext", "track": track,
               "cases": cases, "short_filters": short_filters,
               "cases_short_filters": short_cases})


# ---------------------------------------------------------------------------
# resolveKey + vector against the shared (full registry) filters document
# ---------------------------------------------------------------------------

def shared_filters() -> dict:
    """The ``filters`` object of the default shared ``filters.json`` (every registry filter,
    without ``trans`` URLs)."""
    doc, _ = flt_mod.filters_document(None)
    return doc["filters"]


def gen_resolve_key(shared: dict, ext: dict) -> dict:
    short = {"F200W": {"label": "F200W"}, "F444W": {"label": "F444W"}}
    pair = ["JWST/NIRCam.F410M", "JWST/NIRCam2025.F410M"]
    two_ks = {"A/X.Ks": {"label": "A Ks"}, "B/Y.Ks": {"label": "B Ks"}}
    al = {"JWST/NIRCam.F410M": ["F410M", "NIRCam_F410M"], "JWST/NIRCam2025.F410M": ["NIRCam2025_F410M"]}
    raw = []
    for name in ("f410m", "F410M", "nircam_f410m", "mag_f182m", "JWST/NIRCam.F410M",
                 "jwst/nircam.f410m", "JWST/NIRCam2025.F410M", "f182m", "f444w", "f200w",
                 "f1500w", "f150w", "ks", "k", "j", "i1", "nope", "F410M (2025)"):
        raw.append({"name": name, "candidates": "shared"})
    raw += [
        {"name": "f410m", "candidates": pair},
        {"name": "f410m", "candidates": pair, "aliases": al},
        {"name": "nircam2025_f410m", "candidates": pair, "aliases": al},
        {"name": "JWST/NIRCam.F200W", "candidates": short},
        {"name": "jwst/nircam.f444w", "candidates": list(short)},
        {"name": "ks", "candidates": two_ks},
        {"name": "ks", "candidates": two_ks, "colmap": {"ks": "B/Y.Ks"}},
        {"name": "ks", "candidates": two_ks, "colmap": {"ks": "C/Z.Ks"}},
        {"name": "x", "candidates": "shared", "colmap": {"x": "JWST/MIRI.F1500W"}},
    ]
    cases = []
    for c in raw:
        cands = shared if c["candidates"] == "shared" else c["candidates"]
        c = dict(c)
        c["expected"] = flt_mod.resolve_key(c["name"], cands, c.get("colmap"), c.get("aliases"))
        cases.append(c)
    vec = []
    for x, y, av, rv, colmap in [
        ("f182m - f410m", "f410m", 10.0, 3.1, None),
        ("f200w - f444w", "f444w", 10.0, 3.1, None),
        ("F115W - F200W", "nircam_f200w", 5.0, 5.5, None),
        ("f1500w - f444w", "f444w", 30.0, 4.0, None),
        ("ks - f410m", "f410m", 10.0, 3.1, None),
        ("ks - f410m", "f410m", 10.0, 3.1, {"ks": "2MASS/2MASS.Ks"}),
    ]:
        v = ext_mod.reddening_vector(x, y, av, rv, shared, colmap, ext)
        case = {"x": x, "y": y, "av": av, "rv": rv, "dx": v["dx"], "dy": v["dy"]}
        if colmap:
            case["colmap"] = colmap
        vec.append(case)
    return _j({"description": "resolveKey(name, candidates, colmap?, aliases?) -> key or null. "
                              "candidates: an array of keys or an object of filter entries "
                              "('shared' = shared_filters, the default filters.json object with "
                              "every registry filter). Order: (1) colmap[name] when it is a "
                              "candidate; (2) exact key; (3) the unique key equal to name "
                              "case-insensitively; (4) the unique key listing name among "
                              "aliases[key] (case-insensitive); (5) short-name probes, in turn "
                              "lower(name), the text after its last '_', and shortName(lower("
                              "name)) (text after the last '/' then the last '.'), skipping "
                              "repeats: the unique candidate with lower(shortName(key)) == probe "
                              "wins; when several match and candidates is an object of entries, "
                              "the unique one among them with lower(entry.label) == probe wins; "
                              "(6) null. vector cases: vector(x, y, av, rv, shared_filters, "
                              "colmap, reddening.json ext); 'ks' stays ambiguous without a "
                              "colmap (null).",
               "shared_filters": shared, "cases": cases, "vector": vec})


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------

def build() -> dict[str, bytes]:
    filters = golden_filters()
    ext = golden_ext()
    docs = {
        "mag_jy.json": gen_mag_jy(),
        "nufnu.json": gen_nufnu(),
        "sed_points.json": gen_sed_points(filters),
        "reddening.json": gen_reddening(filters, ext),
        "scale_to.json": gen_scale_to(),
        "track_xy.json": gen_track_xy(filters, ext),
        "resolve_key.json": gen_resolve_key(shared_filters(), ext),
    }
    index = {"schema": "pk-golden-physics/1", "rtol": RTOL, "atol": ATOL,
             "files": sorted(docs),
             "notes": {
                 "null": "null in expected values means NaN / missing",
                 "float32": "redden and trackXY outputs are float32 in JS; expected values are "
                            "the float32 results widened to float64",
                 "generator": "python tests/golden/physics/generate.py (portalkit.physics)",
             }}
    out = {name: (json.dumps(d, ensure_ascii=False, allow_nan=False, indent=1) + "\n").encode()
           for name, d in docs.items()}
    out["index.json"] = (json.dumps(index, indent=1) + "\n").encode()
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="compare instead of writing")
    args = ap.parse_args(argv)
    files = build()
    bad = []
    for name, data in files.items():
        p = HERE / name
        if args.check:
            if not p.is_file() or p.read_bytes() != data:
                bad.append(name)
        else:
            p.write_bytes(data)
    if bad:
        print("out of date: " + ", ".join(bad))
        return 1
    if not args.check:
        print(f"wrote {len(files)} files to {HERE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
