"""Line and feature atlases -> ``features.<h8>.json`` (``pk-features/1``).

ARCHITECTURE.md sections 5.10 and 9.4. The atlases are vendored as
``portalkit/data/features/<family>.json``; :func:`regenerate_atlases` rewrites them from the
constants below, which are lifted from

* ``JWST_Archive_ColorPortal.py`` lines 123-242 (ICE_BANDS, GAS_BANDS, H2_LINES, ION_LINES,
  STELLAR_LINES, ``hydrogen_lines``),
* ``Brick2221_F1500W_SEDPortal.py`` FEATURE_FAMILIES (lines 126-135: PAH, silicate, ice),
* ``W51_Portal_Spectra.py`` LINES / LCOL (lines 138-155).

Atlas file layout::

    {"family": "ice", "label": "Ices", "color": "#58a6ff", "source": "...",
     "features": [{"name", "w_um", "label", "kind": "line"|"band",
                   "lo_um"?, "hi_um"?, "group"?, "color"?, "requires_coverage"?}]}

Feature names are unique within a family. ``requires_coverage`` marks features the spectrum
pane draws dashed where the spectrum has no data (the W51 He I convention).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import PhysicsResult, _writer

__all__ = ["FEATURES_DIR", "FAMILIES", "ICE_BANDS", "GAS_BANDS", "H2_LINES", "ION_LINES",
           "STELLAR_LINES", "BRICK_FEATURE_FAMILIES", "W51_LINES", "W51_LCOL", "hydrogen_lines",
           "build_atlases", "regenerate_atlases", "load_atlas", "features_document", "write"]

FEATURES_DIR = Path(__file__).resolve().parent.parent / "data" / "features"

FAMILIES = ("ice", "gas", "h2", "hi", "ionized", "stellar", "w51_nebular", "w51_photospheric")

_SRC_ARCHIVE = "JWST_Archive_ColorPortal.py:123-242"
_SRC_BRICK = "Brick2221_F1500W_SEDPortal.py:126-135"
_SRC_W51 = "W51_Portal_Spectra.py:138-155"

# --- JWST_Archive_ColorPortal.py ------------------------------------------------
# Ice and solid-state absorption bands, as (label, centre, blue edge, red edge) in micron.
ICE_BANDS = [
    ('H2O ice 1.5 um', 1.50, 1.45, 1.60),
    ('H2O ice 2.0 um', 2.02, 1.95, 2.10),
    ('CO2 ice comb. 2.70', 2.70, 2.68, 2.72),
    ('CO2 ice comb. 2.78', 2.78, 2.76, 2.80),
    ('H2O ice O-H stretch', 3.05, 2.72, 3.60),
    ('NH3 ice N-H', 2.96, 2.90, 3.02),
    ('CH4 ice', 3.32, 3.29, 3.35),
    ('3.47 um band', 3.47, 3.42, 3.52),
    ('CH3OH ice C-H', 3.53, 3.47, 3.60),
    ('CO2 ice nu3', 4.27, 4.21, 4.33),
    ('13CO2 ice', 4.39, 4.36, 4.42),
    ('OCN- (XCN)', 4.62, 4.58, 4.65),
    ('CO ice', 4.67, 4.63, 4.70),
    ('OCS ice', 4.90, 4.87, 4.93),
]

# Broad gas-phase / dust bands, same format.
GAS_BANDS = [
    ('PAH 3.29 um', 3.29, 3.25, 3.33),
    ('aliphatic C-H 3.4', 3.40, 3.36, 3.44),
    ('CO gas fundamental v=1-0', 4.70, 4.40, 5.10),
    ('CO gas overtone v=2-0', 2.35, 2.29, 2.45),
]

# H2 ro-vibrational and pure-rotational lines, micron, vacuum.
H2_LINES = [
    ('1-0 S(0)', 2.2233), ('1-0 S(1)', 2.1218), ('1-0 S(2)', 2.0338),
    ('1-0 S(3)', 1.9576), ('1-0 S(4)', 1.8920), ('1-0 S(5)', 1.8358),
    ('1-0 S(6)', 1.7880), ('1-0 S(7)', 1.7480), ('1-0 S(8)', 1.7147),
    ('1-0 S(9)', 1.6877),
    ('1-0 Q(1)', 2.4066), ('1-0 Q(2)', 2.4134), ('1-0 Q(3)', 2.4237),
    ('1-0 Q(4)', 2.4375), ('1-0 Q(5)', 2.4548), ('1-0 Q(6)', 2.4756),
    ('1-0 Q(7)', 2.5001),
    ('1-0 O(2)', 2.6269), ('1-0 O(3)', 2.8025), ('1-0 O(4)', 3.0039),
    ('1-0 O(5)', 3.2350), ('1-0 O(6)', 3.5007), ('1-0 O(7)', 3.8075),
    ('2-1 S(0)', 2.3556), ('2-1 S(1)', 2.2477), ('2-1 S(2)', 2.1542),
    ('2-1 S(3)', 2.0735), ('2-1 S(4)', 2.0041), ('2-1 S(5)', 1.9449),
    ('2-1 Q(1)', 2.5510), ('2-1 Q(3)', 2.5698),
    ('3-2 S(1)', 2.3864), ('3-2 S(2)', 2.2870), ('3-2 S(3)', 2.2014),
    ('3-2 S(5)', 2.0656),
    ('0-0 S(9)', 4.6947), ('0-0 S(10)', 4.4096), ('0-0 S(11)', 4.1810),
    ('0-0 S(12)', 3.9968), ('0-0 S(13)', 3.8464), ('0-0 S(14)', 3.7237),
    ('0-0 S(15)', 3.6255), ('0-0 S(16)', 3.5470), ('0-0 S(17)', 3.4855),
    ('0-0 S(18)', 3.4384), ('0-0 S(19)', 3.4038), ('0-0 S(20)', 3.3798),
    ('0-0 S(21)', 3.3648),
]

# He I and the forbidden ionic lines that show up in ionized-gas sightlines.
ION_LINES = [
    ('He I 1.083', 1.08331), ('He I 1.279', 1.27884), ('He I 1.701', 1.70073),
    ('He I 2.059', 2.05869), ('He I 2.113', 2.11274), ('He I 4.049', 4.04900),
    ('[Fe II] 1.257', 1.25702), ('[Fe II] 1.295', 1.29462),
    ('[Fe II] 1.321', 1.32092), ('[Fe II] 1.534', 1.53390),
    ('[Fe II] 1.644', 1.64400), ('[Fe II] 1.677', 1.67733),
    ('[Fe II] 1.745', 1.74490), ('[Fe II] 1.810', 1.80990),
    ('[S III] 0.907', 0.90690), ('[S III] 0.953', 0.95310),
    ('[C I] 0.985', 0.98530),
]

# Photospheric features, for telling a reddened star from an embedded source.
STELLAR_LINES = [
    ('Na I 2.206', 2.20620), ('Na I 2.209', 2.20900),
    ('Ca I 2.261', 2.26140), ('Ca I 2.263', 2.26310), ('Ca I 2.266', 2.26570),
    ('Mg I 1.711', 1.71090), ('Mg I 2.107', 2.10660),
    ('Al I 2.110', 2.10990), ('Al I 2.117', 2.11700),
    ('CO 2-0 bandhead', 2.29350), ('CO 3-1 bandhead', 2.32270),
    ('CO 4-2 bandhead', 2.35350), ('CO 5-3 bandhead', 2.38290),
    ('CO 6-4 bandhead', 2.41420),
]


def hydrogen_lines(lo: float = 0.6, hi: float = 5.6) -> list[tuple[str, float]]:
    """H I recombination wavelengths (vacuum, micron) from the Rydberg formula.

    Verbatim from JWST_Archive_ColorPortal.py: Paschen, Brackett, Pfund and Humphreys series,
    members within ``(lo, hi)``, a series stops once adjacent members are closer than
    0.006 um.
    """
    R = 1.0967758e7  # 1/m
    series = {3: 'Pa', 4: 'Br', 5: 'Pf', 6: 'Hu'}
    greek = ['a', 'b', 'g', 'd', 'e', 'z', 'eta', 'th', 'i', 'k', 'l', 'm']
    out = []
    for n1, pre in sorted(series.items()):
        prev = None
        for k, n2 in enumerate(range(n1 + 1, n1 + 16)):
            lam = 1.0 / (R * (1.0 / n1 ** 2 - 1.0 / n2 ** 2)) * 1e6
            if not (lo < lam < hi):
                continue
            if prev is not None and abs(lam - prev) < 0.006:
                break
            prev = lam
            label = f'{pre}-{greek[k]}' if k < len(greek) else f'{pre}({n2})'
            out.append((label, round(lam, 5)))
    return out


# --- Brick2221_F1500W_SEDPortal.py ---------------------------------------------------
BRICK_FEATURE_FAMILIES = {
    'PAH': {'color': '#ffcc33', 'lines': [
        (6.2, 'PAH 6.2'), (7.7, 'PAH 7.7'), (8.6, 'PAH 8.6'),
        (11.3, 'PAH 11.3'), (12.7, 'PAH 12.7')]},
    'silicate': {'color': '#ff7b72', 'lines': [
        (9.7, 'silicate 9.7'), (18.0, 'silicate 18')]},
    'ice': {'color': '#58a6ff', 'lines': [
        (3.0, 'H2O ice 3.0'), (4.27, 'CO2 ice 4.27'),
        (4.67, 'CO ice 4.67'), (15.2, 'CO2 ice 15.2')]},
}

# --- W51_Portal_Spectra.py -----------------------------------------------------------
# (vacuum um, name, colour class, toggle groups: 'neb' nebular, 'ph' photospheric)
W51_LINES = [
    (2.0587, 'He I', 'hot', ['neb', 'ph']),
    (2.1126, 'He I', 'hot', ['neb', 'ph']),
    (2.1218, 'H2 1-0 S(1)', 'h2', ['neb']),
    (2.1661, 'Br-gamma', 'hot', ['neb', 'ph']),
    (2.1885, 'He II', 'hot', ['ph']),
    (2.2233, 'H2 1-0 S(0)', 'h2', ['neb']),
    (2.2062, 'Na I', 'cool', ['ph']),
    (2.2090, 'Na I', 'cool', ['ph']),
    (2.2635, 'Ca I', 'cool', ['ph']),
    (2.2814, 'Mg I', 'cool', ['ph']),
    (2.2935, 'CO 2-0', 'cool', ['ph']),
    (2.3227, 'CO 3-1', 'cool', ['ph']),
    (2.3535, 'CO 4-2', 'cool', ['ph']),
    (2.3830, 'CO 5-3', 'cool', ['ph']),
    (1.7367, 'Br-10', 'hot', ['neb', 'ph']),
    (1.6811, 'Br-11', 'hot', ['neb', 'ph']),
]
W51_LCOL = {'hot': '#8ab4ff', 'cool': '#ffb347', 'h2': '#67d5b5'}

#: H I lines reach the MIRI range (Hu/Pf members to 28 um) for the SED and MIRI spectra
HI_RANGE_UM = (0.6, 28.0)


def _band(name: str, c: float, lo: float, hi: float, **kw: Any) -> dict:
    d = {"name": name, "w_um": float(c), "label": name, "kind": "band",
         "lo_um": float(lo), "hi_um": float(hi)}
    d.update(kw)
    return d


def _line(name: str, w: float, label: str | None = None, **kw: Any) -> dict:
    d = {"name": name, "w_um": float(w), "label": label if label is not None else name,
         "kind": "line"}
    d.update(kw)
    return d


def _unique_names(feats: list[dict]) -> list[dict]:
    """Suffix every repeated name with its wavelength (``He I`` -> ``He I 2.0587``)."""
    counts: dict[str, int] = {}
    for f in feats:
        counts[f["name"]] = counts.get(f["name"], 0) + 1
    for f in feats:
        if counts[f["name"]] > 1:
            f["name"] = f"{f['name']} {f['w_um']:.4f}"
    return feats


def _sorted(feats: list[dict]) -> list[dict]:
    return sorted(feats, key=lambda f: (f["w_um"], f["name"]))


def build_atlases() -> dict[str, dict]:
    """All atlases as ``{family: atlas}`` built from the module constants."""
    out: dict[str, dict] = {}

    ice = [_band(n, c, lo, hi) for n, c, lo, hi in ICE_BANDS]
    names = {f["name"] for f in ice}
    for w, lab in BRICK_FEATURE_FAMILIES["ice"]["lines"]:
        # 3.0, 4.27 and 4.67 duplicate archive bands; only CO2 15.2 is new
        if any(abs(f["w_um"] - w) < 0.06 for f in ice) or lab in names:
            continue
        ice.append(_line(lab, w, source=_SRC_BRICK))
    out["ice"] = {"family": "ice", "label": "Ices", "color": BRICK_FEATURE_FAMILIES["ice"]["color"],
                  "source": f"{_SRC_ARCHIVE} ICE_BANDS; {_SRC_BRICK} ice", "features": _sorted(ice)}

    gas = [_band(n, c, lo, hi, group="gas") for n, c, lo, hi in GAS_BANDS]
    for fam in ("PAH", "silicate"):
        col = BRICK_FEATURE_FAMILIES[fam]["color"]
        for w, lab in BRICK_FEATURE_FAMILIES[fam]["lines"]:
            gas.append(_line(lab, w, group=fam, color=col))
    out["gas"] = {"family": "gas", "label": "Gas-phase and dust bands",
                  "source": f"{_SRC_ARCHIVE} GAS_BANDS; {_SRC_BRICK} PAH, silicate",
                  "features": _sorted(gas)}

    out["h2"] = {"family": "h2", "label": "H2", "source": f"{_SRC_ARCHIVE} H2_LINES",
                 "features": _sorted([_line(f"H2 {n}", w, n) for n, w in H2_LINES])}

    out["hi"] = {"family": "hi", "label": "H I recombination",
                 "source": f"{_SRC_ARCHIVE} hydrogen_lines(lo={HI_RANGE_UM[0]}, hi={HI_RANGE_UM[1]})",
                 "features": _sorted([_line(n, w) for n, w in hydrogen_lines(*HI_RANGE_UM)])}

    out["ionized"] = {"family": "ionized", "label": "He I and forbidden lines",
                      "source": f"{_SRC_ARCHIVE} ION_LINES",
                      "features": _sorted([_line(n, round(w, 5)) for n, w in ION_LINES])}

    out["stellar"] = {"family": "stellar", "label": "Photospheric features",
                      "source": f"{_SRC_ARCHIVE} STELLAR_LINES",
                      "features": _sorted([_line(n, round(w, 5)) for n, w in STELLAR_LINES])}

    for fam, grp, label in (("w51_nebular", "neb", "W51 nebular lines"),
                            ("w51_photospheric", "ph", "W51 photospheric lines")):
        feats = [_line(n, w, group=cls, color=W51_LCOL[cls], requires_coverage=True)
                 for w, n, cls, groups in W51_LINES if grp in groups]
        out[fam] = {"family": fam, "label": label, "source": f"{_SRC_W51} LINES ({grp!r} group)",
                    "features": _unique_names(_sorted(feats))}
    for a in out.values():
        _unique_names(a["features"])
    return out


def _atlas_bytes(atlas: dict) -> bytes:
    return (json.dumps(atlas, ensure_ascii=False, indent=1, allow_nan=False) + "\n").encode("utf-8")


def regenerate_atlases(directory: str | Path = FEATURES_DIR) -> list[Path]:
    """Rewrite ``data/features/<family>.json`` from the module constants."""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for fam, atlas in build_atlases().items():
        p = d / f"{fam}.json"
        tmp = p.with_suffix(".json.tmp")
        tmp.write_bytes(_atlas_bytes(atlas))
        os.replace(tmp, p)
        out.append(p)
    _load_atlas.cache_clear()
    return out


@lru_cache(maxsize=32)
def _load_atlas(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


_FAMILY_RE = re.compile(r"[a-z0-9_]+\Z")


def _family_list(families: Any) -> list[str] | None:
    """Normalise a families argument: None/empty -> None, a string -> [string], any other
    iterable -> list. Each name must match ``[a-z0-9_]+``."""
    if families is None:
        return None
    if isinstance(families, str):
        fams = [families]
    elif isinstance(families, _Iterable) and not isinstance(families, (bytes, _Mapping)):
        fams = list(families)
    else:
        raise TypeError(f"families must be a family name or an iterable of names, "
                        f"got {type(families).__name__}")
    for f in fams:
        _check_family(f)
    return fams or None


def _check_family(family: Any) -> str:
    if not isinstance(family, str) or not _FAMILY_RE.match(family):
        raise ValueError(f"bad feature family name {family!r} (expected [a-z0-9_]+)")
    return family


def load_atlas(family: str, directory: str | Path | None = None) -> dict:
    """The vendored atlas of ``family`` (a fresh copy); ``family`` must match ``[a-z0-9_]+``."""
    _check_family(family)
    d = Path(directory) if directory is not None else FEATURES_DIR
    p = d / f"{family}.json"
    if not p.is_file():
        raise KeyError(f"no feature atlas {family!r} in {d}")
    return json.loads(json.dumps(_load_atlas(str(p))))


def features_document(families: Iterable[str] | None = None,
                      directory: str | Path | None = None) -> dict:
    """``pk-features/1`` document for ``families`` (default: all of :data:`FAMILIES`).

    ``meta`` carries each family's label, colour and source.
    """
    fams: Sequence[str] = _family_list(families) or list(FAMILIES)
    if len(set(fams)) != len(fams):
        raise ValueError("duplicate feature families")
    doc_f: dict[str, list] = {}
    meta: dict[str, dict] = {}
    for fam in fams:
        a = load_atlas(fam, directory)
        doc_f[fam] = a["features"]
        m = {"label": a.get("label", fam), "source": a.get("source", "")}
        if a.get("color"):
            m["color"] = a["color"]
        meta[fam] = m
    return {"schema": "pk-features/1", "meta": {"families": meta}, "families": doc_f}


def write(families: Any, dest: Any, *, ledger: Any = None) -> PhysicsResult:
    """Write ``features.<h8>.json`` into ``dest``. ``families`` is a list of family names
    (a single name, any iterable of names, an object with a ``families`` attribute, or None
    for all)."""
    from ..schema import validate_features
    if (families is not None and not isinstance(families, (str, _Iterable))
            and hasattr(families, "families")):
        families = getattr(families, "families")
    w = _writer(dest, ledger)
    doc = features_document(families)
    errs = validate_features(doc)
    if errs:
        raise ValueError("features document invalid: " + "; ".join(errs[:10]))
    path = w.put_json("", "features", doc, kind="features")
    return PhysicsResult(path=path, doc=doc, files=[path])
