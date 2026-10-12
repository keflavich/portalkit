"""Filter registry -> ``filters.<h8>.json`` (``pk-filters/1``).

ARCHITECTURE.md sections 5.10 and 9.4; WP02.

The registry (``portalkit/data/filters_registry.ecsv``) is built by
:func:`regenerate_registry` from the astroquery SVO Filter Profile Service cache
(``~/.astropy/cache/astroquery/SvoFps/*.pickle``) and the JWST Vega zero points in
``jwst_vega_zeropoints.ecsv``. It holds one row per filter:

* ``key``        SVO filter id, e.g. ``JWST/NIRCam.F410M`` (the catalog ``filter`` value)
* ``label``      short display label (``F410M``, ``NIRISS F150W``, ``2MASS Ks``, ``IRAC I1``)
* ``instrument`` instrument name (``NIRCam``, ``MIRI``, ``NIRISS``, ``2MASS``, ``IRAC`` ...)
* ``facility``   SVO facility (``JWST``, ``2MASS``, ``Spitzer``, ``Roman``, ...)
* ``wl_um``      pivot wavelength (micron)
* ``weff_um``    effective width (SVO ``WidthEff``, micron)
* ``lam_eff_um`` effective wavelength (SVO ``WavelengthEff``, micron)
* ``zp_jy``      zero point in Jy (jwst_vega_zeropoints.ecsv for JWST, SVO otherwise)
* ``system``     magnitude system (``Vega``, ``AB``)
* ``aliases``    space-separated alternative names accepted by :func:`lookup`
* ``trans_w``/``trans_t``  downsampled transmission (64 samples, peak 1) as compact JSON
  lists, or ``""`` when the cache holds no curve for the filter

NIRSpec dispersers and the Roman WFI prism/grism are excluded (spectroscopic modes).
Reading the cache (pickled ``requests.Response`` objects) only happens in
:func:`regenerate_registry`; normal builds read the vendored ECSV.

The cache directory may also hold raw SVO VOTables (``*.xml``/``*.vot``, the body of
``fps.php?ID=<filterID>``): the HST rows (WFC3/IR, WFC3/UVIS2, ACS/WFC; added for wd2) came
from such files, since the astroquery cache had no HST data. Rows of the current registry
whose key the cache does not provide are carried over unchanged (``carry_over``), so a
regeneration from a cache without them keeps them.

:func:`write` emits ``filters.<h8>.json`` plus one ``filters/<slug>.<h8>.json`` per
transmission curve (``{"w":[..],"t":[..]}``); ``trans`` URLs are relative to the
directory of ``filters.<h8>.json``.
"""

from __future__ import annotations

import io
import json
import math
import os
import pickle
import re
import warnings
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np

from . import PhysicsResult, _writer
from . import extinction as _ext

__all__ = ["REGISTRY_PATH", "SVO_CACHE_DIR", "JWST_ZP_ECSV", "TRANS_NPT", "load_registry",
           "registry_keys", "lookup", "resolve_key", "short_name", "slug", "filter_entry",
           "filters_document", "write", "regenerate_registry", "build_registry_table",
           "downsample_transmission", "read_svo_cache"]

REGISTRY_PATH = Path(__file__).resolve().parent.parent / "data" / "filters_registry.ecsv"
SVO_CACHE_DIR = Path(os.path.expanduser("~/.astropy/cache/astroquery/SvoFps"))
JWST_ZP_ECSV = Path("/orange/adamginsburg/robitaille_models/portal/jwst_vega_zeropoints.ecsv")
TRANS_NPT = 64
TRANS_THRESHOLD = 0.005
EXCLUDE_INSTRUMENTS = ("NIRSpec",)
EXCLUDE_BANDS = ("Prism", "Grism")

_DOC_FIELDS = ("label", "instrument", "wl_um", "weff_um", "zp_jy", "system")


# ---------------------------------------------------------------------------
# registry access
# ---------------------------------------------------------------------------

def _jsonlist(s: Any) -> list[float] | None:
    if s is None or s is np.ma.masked or np.ma.is_masked(s):
        return None
    s = str(s).strip()
    if not s:
        return None
    v = json.loads(s)
    return [float(x) for x in v] if v else None


def _fnum(v: Any) -> float | None:
    if v is None or v is np.ma.masked:
        return None
    f = float(v)
    return f if math.isfinite(f) else None


@lru_cache(maxsize=4)
def _load(path: str) -> dict[str, dict]:
    from astropy.table import Table
    t = Table.read(path, format="ascii.ecsv")
    out: dict[str, dict] = {}
    for r in t:
        key = str(r["key"])
        out[key] = {
            "key": key,
            "label": str(r["label"]),
            "instrument": str(r["instrument"]),
            "facility": str(r["facility"]),
            "wl_um": _fnum(r["wl_um"]),
            "weff_um": _fnum(r["weff_um"]),
            "lam_eff_um": _fnum(r["lam_eff_um"]),
            "zp_jy": _fnum(r["zp_jy"]),
            "system": str(r["system"]),
            "aliases": [] if np.ma.is_masked(r["aliases"]) else
                       [a for a in str(r["aliases"]).split() if a],
            "trans_w": _jsonlist(r["trans_w"]),
            "trans_t": _jsonlist(r["trans_t"]),
        }
    return out


def load_registry(path: str | Path | None = None) -> dict[str, dict]:
    """The filter registry as ``{key: row-dict}`` (cached; do not mutate the result)."""
    return _load(str(Path(path) if path is not None else REGISTRY_PATH))


def registry_keys(path: str | Path | None = None) -> list[str]:
    return list(load_registry(path))


def short_name(key: str) -> str:
    """``JWST/NIRCam.F410M`` -> ``F410M``; ``2MASS/2MASS.Ks`` -> ``Ks``."""
    return str(key).rsplit("/", 1)[-1].rsplit(".", 1)[-1]


def slug(key: str) -> str:
    """File-name-safe lower-case form of a filter key (``jwst_nircam_f410m``)."""
    return re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_") or "filter"


def resolve_key(name: str, candidates: Iterable[str] | Mapping[str, Any],
                colmap: Mapping[str, str] | None = None,
                aliases: Mapping[str, Iterable[str]] | None = None) -> str | None:
    """Map a name (catalog column key, expression key, short filter name) to a filter key.

    Order:

    1. ``colmap[name]`` (when it is a candidate);
    2. exact candidate;
    3. the unique case-insensitive candidate;
    4. the unique candidate listing ``name`` among its ``aliases`` (case-insensitive);
    5. short-name probes, in turn: ``name``, the part of ``name`` after its last ``_``
       (``nircam_f410m`` -> ``f410m``) and :func:`short_name` of ``name``
       (``JWST/NIRCam.F410M`` -> ``f410m``, so a full key finds a short-keyed document), all
       lower-cased. For each probe, the unique candidate whose :func:`short_name` equals it
       case-insensitively wins. When several candidates match and ``candidates`` is a
       mapping of filter entries (a ``pk-filters/1`` ``filters`` object or the registry),
       the unique one among them whose ``label`` equals the probe case-insensitively wins
       (``JWST/NIRCam.F410M``, label ``F410M``, over ``JWST/NIRCam2025.F410M``, label
       ``F410M (2025)``).

    Returns None when nothing matches or every step is ambiguous.
    """
    labels: dict[str, str] = {}
    if isinstance(candidates, Mapping):
        for c, v in candidates.items():
            lab = v.get("label") if isinstance(v, Mapping) else None
            if isinstance(lab, str):
                labels[c] = lab.lower()
    cands = list(candidates)
    cset = set(cands)
    if colmap and name in colmap and colmap[name] in cset:
        return colmap[name]
    if name in cset:
        return name
    low = str(name).lower()
    ci = [c for c in cands if c.lower() == low]
    if len(ci) == 1:
        return ci[0]
    if aliases:
        al = [c for c in cands if any(low == str(a).lower() for a in aliases.get(c, ()))]
        if len(al) == 1:
            return al[0]
    probes: list[str] = []
    for p in (low, low.rsplit("_", 1)[-1], short_name(low)):
        if p and p not in probes:
            probes.append(p)
    for probe in probes:
        sn = [c for c in cands if short_name(c).lower() == probe]
        if len(sn) == 1:
            return sn[0]
        if len(sn) > 1 and labels:
            lab = [c for c in sn if labels.get(c) == probe]
            if len(lab) == 1:
                return lab[0]
    return None


def lookup(name: str, path: str | Path | None = None) -> dict | None:
    """Registry row for a key, alias or unique short name (see :func:`resolve_key`)."""
    reg = load_registry(path)
    k = resolve_key(name, reg, aliases={k: v["aliases"] for k, v in reg.items()})
    return reg[k] if k is not None else None


# ---------------------------------------------------------------------------
# document
# ---------------------------------------------------------------------------

def _g(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _a_over_av(key: str, wl: float | None, given: Mapping | None) -> dict:
    out: dict[str, float | None] = {}
    tab = _ext.load_alav()
    for rv in _ext.VENDORED_RV:
        lk = _ext.law_key(rv)
        v = None
        if given and lk in given:
            v = _fnum(given[lk])
        elif key in tab and (wl is None or tab[key].get("wl_um") is None
                             or math.isclose(wl, tab[key]["wl_um"], rel_tol=1e-6)):
            # vendored value at the registry pivot; an overridden wl_um is re-sampled below
            v = tab[key].get(lk)
        elif wl is not None:
            v = _fnum(_ext.a_over_av(wl, rv))
        out[lk] = v
    if given:
        for k, v in given.items():
            if k not in out:
                out[k] = _fnum(v)
    return out


def filter_entry(name: str, extra: Mapping[str, Any] | None = None) -> tuple[dict, dict | None]:
    """(``pk-filters/1`` entry without ``trans``, transmission ``{w,t}`` or None) for ``name``.

    ``name`` is resolved with :func:`lookup`; ``extra`` overrides registry fields or defines
    a filter missing from the registry (then ``wl_um`` and ``zp_jy`` are required). ``extra``
    may carry ``a_over_av`` (dict) and ``trans`` as ``{"w": [...], "t": [...]}``.
    """
    extra = dict(extra or {})
    row = lookup(name)
    if row is None and not extra:
        raise KeyError(f"filter {name!r} is not in the registry and has no 'extra' definition")
    base: dict[str, Any] = {}
    trans = None
    if row is not None:
        base = {k: row[k] for k in _DOC_FIELDS}
        if row["trans_w"] and row["trans_t"]:
            trans = {"w": row["trans_w"], "t": row["trans_t"]}
    unknown = set(extra) - set(_DOC_FIELDS) - {"a_over_av", "trans"}
    if unknown:
        raise ValueError(f"filter {name!r}: unknown extra fields {sorted(unknown)}")
    for k in _DOC_FIELDS:
        if k in extra:
            base[k] = extra[k]
    if row is None:
        missing = [k for k in ("wl_um", "zp_jy") if base.get(k) is None]
        if missing:
            raise ValueError(f"filter {name!r} is not in the registry; extra must give {missing}")
        base.setdefault("label", short_name(name))
        base.setdefault("instrument", "")
        base.setdefault("weff_um", None)
        base.setdefault("system", "Vega")
    if "trans" in extra:
        tr = extra["trans"]
        trans = None if tr is None else {"w": [float(x) for x in tr["w"]],
                                         "t": [float(x) for x in tr["t"]]}
    wl = _fnum(base.get("wl_um"))
    if wl is None or wl <= 0:
        raise ValueError(f"filter {name!r}: wl_um must be a positive number")
    zp = _fnum(base.get("zp_jy"))
    if zp is None or zp <= 0:
        raise ValueError(f"filter {name!r}: zp_jy must be a positive number")
    entry = {"label": str(base["label"]), "instrument": str(base.get("instrument") or ""),
             "wl_um": wl, "weff_um": _fnum(base.get("weff_um")), "zp_jy": zp,
             "system": str(base.get("system") or "Vega"),
             "a_over_av": _a_over_av(row["key"] if row else name, wl, extra.get("a_over_av"))}
    return entry, trans


def filters_document(spec: Any = None, *, trans_urls: Mapping[str, str] | None = None
                     ) -> tuple[dict, dict[str, dict]]:
    """(``pk-filters/1`` document, ``{key: {w,t}}`` transmission curves) for a FilterSetSpec.

    ``spec.keys`` empty (or ``spec`` None) means every registry filter. Keys are emitted as
    given (a short or alias key stays as written, so it matches catalog ``filter`` values);
    ``spec.extra`` keys not in ``spec.keys`` are added. ``trans_urls`` fills ``trans``.
    """
    keys = list(_g(spec, "keys", None) or []) if spec is not None else []
    extra = dict(_g(spec, "extra", None) or {}) if spec is not None else {}
    if not keys:
        keys = registry_keys()
    for k in extra:
        if k not in keys:
            keys.append(k)
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate filter keys")
    filters: dict[str, dict] = {}
    curves: dict[str, dict] = {}
    for k in keys:
        if not isinstance(k, str) or not k:
            raise ValueError(f"bad filter key {k!r}")
        entry, tr = filter_entry(k, extra.get(k))
        if tr is not None:
            curves[k] = tr
            if trans_urls and k in trans_urls:
                entry["trans"] = trans_urls[k]
        filters[k] = entry
    doc = {"schema": "pk-filters/1", "filters": filters}
    return doc, curves


def write(spec: Any, dest: Any, *, ledger: Any = None, trans: bool = True) -> PhysicsResult:
    """Write ``filters.<h8>.json`` (+ ``filters/<slug>.<h8>.json`` curves) into ``dest``."""
    from ..schema import validate_filters
    w = _writer(dest, ledger)
    doc, curves = filters_document(spec)
    files: list[str] = []
    urls: dict[str, str] = {}
    if trans:
        for k, c in curves.items():
            rel = w.put_json("filters", slug(k), {"w": c["w"], "t": c["t"]}, kind="filters")
            urls[k] = rel
            files.append(rel)
        doc, _ = filters_document(spec, trans_urls=urls)
    errs = validate_filters(doc)
    if errs:
        raise ValueError("filters document invalid: " + "; ".join(errs[:10]))
    path = w.put_json("", "filters", doc, kind="filters")
    files.append(path)
    return PhysicsResult(path=path, doc=doc, files=files)


# ---------------------------------------------------------------------------
# registry regeneration (SVO cache)
# ---------------------------------------------------------------------------

def downsample_transmission(w_um: Any, t: Any, npt: int = TRANS_NPT,
                            threshold: float = TRANS_THRESHOLD) -> tuple[list[float], list[float]]:
    """Trim to ``T >= threshold * max`` (first to last such sample), resample to ``npt``
    points uniform in wavelength, normalise the peak to 1 and round (w: 6 significant
    digits, t: 4 decimals)."""
    w = np.asarray(w_um, dtype=np.float64)
    t = np.asarray(t, dtype=np.float64)
    ok = np.isfinite(w) & np.isfinite(t)
    w, t = w[ok], t[ok]
    order = np.argsort(w, kind="stable")
    w, t = w[order], t[order]
    if w.size < 2 or not np.nanmax(t) > 0:
        raise ValueError("transmission curve needs >= 2 samples and a positive maximum")
    tmax = float(np.max(t))
    idx = np.nonzero(t >= threshold * tmax)[0]
    i0 = max(int(idx[0]) - 1, 0)
    i1 = min(int(idx[-1]) + 1, w.size - 1)
    w, t = w[i0:i1 + 1], t[i0:i1 + 1]
    wg = np.linspace(w[0], w[-1], npt)
    tg = np.interp(wg, w, t) / tmax
    tg = np.clip(tg, 0.0, 1.0)
    wl = [float(f"{x:.6g}") for x in wg]
    for i in range(1, len(wl)):
        if wl[i] <= wl[i - 1]:
            wl[i] = float(np.nextafter(wl[i - 1], np.inf))
    return wl, [round(float(x), 4) for x in tg]


def read_svo_cache(cache_dir: str | Path = SVO_CACHE_DIR) -> tuple[dict[str, dict], dict[str, tuple]]:
    """Parse the astroquery SvoFps cache.

    Returns (metadata ``{filterID: {...}}`` from facility listings and single-filter params,
    transmission ``{filterID: (w_um, t)}``). Error responses ("Filter not found") are skipped.
    Only call this on a trusted cache: the files are pickles.
    """
    from astropy.io.votable import parse_single_table

    meta: dict[str, dict] = {}
    curves: dict[str, tuple] = {}
    cdir = Path(cache_dir)
    files = sorted(list(cdir.glob("*.pickle")) + list(cdir.glob("*.xml")) + list(cdir.glob("*.vot")))
    for fn in files:
        if fn.suffix == ".pickle":
            with open(fn, "rb") as fh:
                try:
                    resp = pickle.load(fh)  # noqa: S301 - local astroquery cache
                except (pickle.UnpicklingError, EOFError, AttributeError, ModuleNotFoundError,
                        ImportError) as err:
                    warnings.warn(f"skipping unreadable SVO cache file {fn.name}: {err}")
                    continue
            content = getattr(resp, "content", None)
        else:
            content = fn.read_bytes()
        if not isinstance(content, (bytes, bytearray)) or b"<TABLE" not in content.upper():
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            vt = parse_single_table(io.BytesIO(bytes(content)))
            tab = vt.to_table()
        params = {p.name: p.value for p in vt.params}
        if "filterID" in tab.colnames:
            for r in tab:
                fid = str(r["filterID"])
                meta[fid] = {k: r[k] for k in tab.colnames}
        elif "Wavelength" in tab.colnames and "Transmission" in tab.colnames and "filterID" in params:
            fid = params["filterID"]
            fid = fid.decode() if isinstance(fid, bytes) else str(fid)
            wl = np.asarray(tab["Wavelength"], dtype=np.float64) * 1e-4
            curves[fid] = (wl, np.asarray(tab["Transmission"], dtype=np.float64))
            pm = {}
            for k, v in params.items():
                pm[k] = v.decode() if isinstance(v, bytes) else v
            meta.setdefault(fid, {}).update({k: v for k, v in pm.items() if k not in meta.get(fid, {})})
    return meta, curves


_LABEL_PREFIX = {"NIRCam": "", "NIRCam2025": "", "MIRI": "", "NIRISS": "NIRISS ",
                 "2MASS": "2MASS ", "IRAC": "IRAC ", "MIPS": "MIPS ", "WFI": "WFI ",
                 "VISTA": "VISTA ", "WFCAM": "WFCAM ", "UKIDSS": "UKIDSS ",
                 "WFC3_IR": "WFC3 ", "WFC3_UVIS1": "WFC3 ", "WFC3_UVIS2": "WFC3 ",
                 "ACS_WFC": "ACS "}

#: extra short aliases of the HST cameras (``WFC3_F160W``, ``ACS_F814W``)
_HST_ALIAS = {"WFC3_IR": "WFC3", "WFC3_UVIS2": "WFC3", "ACS_WFC": "ACS"}


def _label_instrument(fid: str) -> tuple[str, str, str]:
    """(label, instrument, facility) of an SVO filter id."""
    facility, rest = fid.split("/", 1)
    inst, band = rest.rsplit(".", 1)
    if inst == "MIPS" and band.endswith("mu"):
        band = band[:-2]
    label = _LABEL_PREFIX.get(inst, f"{inst} ") + band
    if inst == "NIRCam2025":
        label += " (2025)"
        instrument = "NIRCam"
    else:
        instrument = inst
    return label, instrument, facility


def _aliases(fid: str, instrument: str) -> list[str]:
    facility, rest = fid.split("/", 1)
    inst, band = rest.rsplit(".", 1)
    if inst == "NIRCam2025":
        return [f"NIRCam2025_{band}"]
    out = [rest, f"{inst}_{band}", f"{inst.upper()}_{band.upper()}"]
    if inst == "NIRCam":
        out.append(band)
    if inst == "MIRI":
        out.append(band)
    if inst == "MIPS" and band.endswith("mu"):
        out.append(f"MIPS{band[:-2]}")
    if inst == "IRAC":
        out += [f"IRAC{band[1:]}", f"I{band[1:]}"]
    if inst in _HST_ALIAS:
        out.append(f"{_HST_ALIAS[inst]}_{band}")
    seen: list[str] = []
    for a in out:
        if a not in seen and a != fid:
            seen.append(a)
    return seen


def _num_meta(m: Mapping, k: str) -> float | None:
    v = m.get(k)
    if v is None or v is np.ma.masked:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _str_meta(m: Mapping, k: str, default: str = "") -> str:
    v = m.get(k)
    if v is None or v is np.ma.masked:
        return default
    return v.decode() if isinstance(v, bytes) else str(v)


def build_registry_table(cache_dir: str | Path = SVO_CACHE_DIR, zp_ecsv: str | Path = JWST_ZP_ECSV,
                         carry_over: str | Path | None = None):
    """Registry :class:`astropy.table.Table` from the SVO cache and the JWST zero points.

    ``carry_over`` (a registry ECSV) contributes its rows whose key the cache does not
    provide, unchanged."""
    from astropy.table import Table

    meta, curves = read_svo_cache(cache_dir)
    zps: dict[str, float] = {}
    if zp_ecsv is not None and Path(zp_ecsv).exists():
        zt = Table.read(str(zp_ecsv), format="ascii.ecsv")
        for r in zt:
            zps["JWST/" + str(r["filter"])] = float(r["zeropoint_Jy"])
    rows = []
    for fid in sorted(meta):
        if "/" not in fid or "." not in fid.split("/", 1)[1]:
            continue
        m = meta[fid]
        inst_part = fid.split("/", 1)[1].rsplit(".", 1)[0]
        if inst_part in EXCLUDE_INSTRUMENTS or fid.rsplit(".", 1)[1] in EXCLUDE_BANDS:
            continue
        piv = _num_meta(m, "WavelengthPivot")
        zp = zps.get(fid, _num_meta(m, "ZeroPoint"))
        if piv is None or zp is None:
            continue
        label, instrument, facility = _label_instrument(fid)
        tw, tt = "", ""
        if fid in curves:
            ww, ttv = downsample_transmission(*curves[fid])
            tw = json.dumps(ww, separators=(",", ":"))
            tt = json.dumps(ttv, separators=(",", ":"))
        weff = _num_meta(m, "WidthEff")
        leff = _num_meta(m, "WavelengthEff")
        rows.append({
            "key": fid, "label": label, "instrument": instrument, "facility": facility,
            "wl_um": float(f"{piv * 1e-4:.7g}"),
            "weff_um": float(f"{weff * 1e-4:.6g}") if weff is not None else np.nan,
            "lam_eff_um": float(f"{leff * 1e-4:.7g}") if leff is not None else np.nan,
            "zp_jy": float(f"{zp:.8g}"),
            "zp_source": "jwst_vega_zeropoints" if fid in zps else "svo",
            "system": _str_meta(m, "MagSys", "Vega") or "Vega",
            "aliases": " ".join(_aliases(fid, instrument)),
            "trans_w": tw, "trans_t": tt,
        })
    names = ["key", "label", "instrument", "facility", "wl_um", "weff_um", "lam_eff_um", "zp_jy",
             "zp_source", "system", "aliases", "trans_w", "trans_t"]
    if carry_over is not None and Path(carry_over).is_file():
        have = {r["key"] for r in rows}
        old = Table.read(str(carry_over), format="ascii.ecsv")
        for r in old:
            if str(r["key"]) not in have:
                rows.append({n: (r[n].item() if hasattr(r[n], "item") else r[n]) for n in names})
    rows.sort(key=lambda r: (r["wl_um"], r["key"]))
    t = Table(rows=[[r[n] for n in names] for r in rows], names=names)
    for c, u in (("wl_um", "um"), ("weff_um", "um"), ("lam_eff_um", "um"), ("zp_jy", "Jy")):
        t[c].unit = u
    t.meta["source"] = ("SVO Filter Profile Service via the astroquery SvoFps cache; JWST zero "
                        f"points from {Path(zp_ecsv).name if zp_ecsv else 'SVO'}")
    t.meta["columns"] = {
        "wl_um": "pivot wavelength", "weff_um": "effective width (SVO WidthEff)",
        "lam_eff_um": "effective wavelength (SVO WavelengthEff)",
        "trans_w/trans_t": f"transmission resampled to {TRANS_NPT} points uniform in wavelength "
                           f"over T >= {TRANS_THRESHOLD} max, peak-normalised; JSON lists",
    }
    t.meta["generator"] = "portalkit.physics.filters.regenerate_registry"
    return t


def regenerate_registry(path: str | Path = REGISTRY_PATH, cache_dir: str | Path = SVO_CACHE_DIR,
                        zp_ecsv: str | Path = JWST_ZP_ECSV,
                        carry_over: str | Path | None = REGISTRY_PATH) -> Path:
    """Rebuild ``filters_registry.ecsv`` from the SVO cache (see module docstring); rows of
    ``carry_over`` (default: the vendored registry) missing from the cache are kept."""
    t = build_registry_table(cache_dir, zp_ecsv, carry_over=carry_over)
    if len(t) == 0:
        raise RuntimeError(f"no filters found in {cache_dir}")
    path = Path(path)
    tmp = path.with_suffix(".ecsv.tmp")
    t.write(str(tmp), format="ascii.ecsv", overwrite=True)
    os.replace(tmp, path)
    _load.cache_clear()
    return path
