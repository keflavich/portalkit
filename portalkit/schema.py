"""Hand-written validators for every portalkit document (ARCHITECTURE.md sections 4 and 5).

No jsonschema dependency. For every document there is

* ``validate_<doc>(obj) -> list[str]``: returns error strings (empty list means valid);
* ``assert_valid_<doc>(obj)``: raises :class:`SchemaError` (a ``ValueError``) listing the errors.

Documents and schema ids:

=================  ==================  ======================================
validator          schema id           where
=================  ==================  ======================================
``site``           (TOML, no id)       ``sites/<family>.toml`` (section 4.1)
``portal``         ``pk-portal/1``     ``portal.json`` (4.2)
``page``           ``pk-page/1``       embedded ``#pk-page`` (4.3)
``catalog``        ``pk-catalog/1``    ``data/<ds>/manifest.json`` (5.2)
``records``        ``pk-records/1``    ``data/<set>/index.json`` (5.6)
``pks1_header``    (binary header)     pks1 record header JSON (5.7)
``pki1_header``    (binary header)     pki1 record header JSON (5.8)
``images``         ``pk-images/1``     ``images.json`` (5.10)
``hips``           ``pk-hips/1``       ``hips.json``
``overlays``       ``pk-overlays/1``   ``overlays.json``
``narrative``      ``pk-narrative/1``  ``narrative.json``
``templates``      ``pk-templates/1``  ``templates/index.<h8>.json`` (res)
``filters``        ``pk-filters/1``    ``filters.<h8>.json`` (res)
``extinction``     ``pk-extinction/1`` ``extinction.<h8>.json`` (res)
``isochrones``     ``pk-isochrones/1`` ``iso.<h8>.json``
``features``       ``pk-features/1``   ``features.<h8>.json`` (res)
``products``       ``products/1``      json record in a products record set
``deploy``         ``pk-deploy/1``     ``<target>/.portalkit-deploy.json`` (11.3)
=================  ==================  ======================================

Key policy. Unknown top-level keys are errors in every document (keys beginning with ``_``
are tolerated as annotations). Inside nested objects, unknown keys are errors only where
the contract enumerates the full key set: page layout nodes, pane options (section 6.10),
DatasetRefs, page.state keys and catalog ``cols[]`` entries. Every document must also be
serialisable as strict JSON (``allow_nan=False``): NaN/inf floats and non-JSON value types
are reported.

``schema`` key. ``pk-page``, ``pk-portal``, ``pk-catalog``, ``pk-records`` and
``pk-deploy`` documents must carry their ``schema`` id. The section 5.10 documents may omit
it (their shapes in the contract do not list it); when present it must match.
"""

from __future__ import annotations

import json
import math
import re
import sys
from typing import Any, Callable

from .constants import CHUNK_ROWS, PAGE_JSON_MAX

__all__ = [
    "SchemaError",
    "SCHEMA_IDS",
    "VALIDATORS",
    "ROLES",
    "ENCODINGS",
    "ARCHETYPES",
    "PANE_TYPES",
    "PANE_OPTIONS",
    "OPTIONAL_PANE_OPTIONS",
    "DATASET_KINDS",
    "RECORD_KINDS",
    "GRAMMARS",
    "validate",
    "assert_valid",
    "json_errors",
]

# ---------------------------------------------------------------------------
# vocabularies
# ---------------------------------------------------------------------------

ROLES = ("id", "ra", "dec", "mag", "flux", "error", "color", "value", "category",
         "flag", "text", "url", "image", "limit")

ENCODINGS = ("u16q", "u16log", "u8q", "u8log", "cat8", "cat16", "bits", "f32", "u32",
             "u32lin", "i16off", "str", "sparse")

#: Encodings that carry ``lo``/``hi`` quantization bounds.
_QUANT_ENCS = ("u16q", "u16log", "u8q", "u8log")

ARCHETYPES = ("landing", "explorer", "manifold", "gallery", "spectra", "wavelength", "narrative")

PANE_TYPES = ("list", "detail", "scatter", "manifold", "hist", "sky", "wavelength", "narrative",
              "spectrum1d", "spectrum2d", "sed", "gallery", "table", "readme", "landing")

DATASET_KINDS = ("catalog", "records", "images", "templates", "filters", "extinction",
                 "isochrones", "features", "hips", "overlays", "narrative", "portal")

#: ``record`` values of a records DatasetRef / ``kind`` of a records index.
RECORD_KINDS = ("pks1", "pki1", "json", "sedcurve")

GRAMMARS = ("bare-name", "kv-view", "gc-query", "wavelength", "beat", "gallery-index",
            "w51-rowid", "specviewer")

CLIP_MODES = ("none", "saturate", "drop")
VISIBILITIES = ("public", "secure")
AUTHS = ("public", "secure")
TIERS = ("A", "B", "C")
PANE_TIERS = ("T0", "T1", "T2", "F")
OVERLAY_KINDS = ("polygons", "circles", "markers", "polyline", "rings", "boxes", "vector", "track")
PRESET_KINDS = ("axes", "frame", "selection", "filter")
PRODUCT_KINDS = ("fits", "png", "mast", "link")

#: Pane options from section 6.10. Value: a checker name (see ``_OPTION_CHECKS``) or None
#: (any JSON value accepted).
PANE_OPTIONS: dict[str, dict[str, str | None]] = {
    "list": {"columns": "strlist", "sort": None, "search": "bool", "virtual": "bool",
             "rowHeight": "posnum", "showSelectionOnly": "bool"},
    "detail": {"template": "str", "products": "ds:records", "images": "ds:images", "bars": "strlist",
               "prose": "strlist", "links": "bool", "clickToAxis": "bool"},
    "scatter": {"x": "str", "y": "str", "xlog": "bool", "ylog": "bool", "color": None,
                "density": "enum:auto|on|off", "background": "ds:catalog", "raster": "list",
                "overlays": "strlist", "pairs": "obj", "hollow_if": "str", "labels": "str",
                "errorbars": "bool", "presets": "bool", "grid": "list"},
    "manifold": {"dims": "dims", "frame": "str", "trails": "bool", "compass": "bool", "tour": "bool",
                 "background": "ds:catalog"},
    "hist": {"expr": "str", "bins": "posint", "brush": "bool"},
    "sky": {"hips": "ds:hips", "overlays": "strlist", "markers": "enum:auto|off", "brush": "bool",
            "rings": "numlist", "fits": "strlist"},
    "wavelength": {"hips": "ds:hips", "speed": "posnum", "markers": "ds:catalog"},
    "narrative": {"narrative": "ds:narrative", "sticky": "enum:sky|scatter"},
    "spectrum1d": {"records": "ds:records", "variants": "strlist", "features": "strlist",
                   "templates": "ds:templates", "bands": "list", "filters": "bool", "pin": "pin",
                   "logx": "bool", "logy": "bool", "mode": "enum:spectrum|sed"},
    "spectrum2d": {"records": "ds:records", "cmap": "str", "link1d": "bool"},
    "sed": {"units": "enum:fnu|nufnu", "models": "models", "dialog": "bool"},
    "gallery": {"images": "ds:images", "roles": "strlist", "cardSize": "posnum", "annotate": "obj",
                "sort": "strlist"},
    "table": {"columns": "strlist", "source": "str"},
    "readme": {"html": "str", "url": "str"},
    "landing": {"cards": None, "counts": "bool", "search": "bool"},
}

#: Options marked ``?`` in section 6.10. A null value means "unset" for these (config.py may
#: emit null for an unset optional); every other option rejects null unless its checker is None.
OPTIONAL_PANE_OPTIONS: dict[str, frozenset[str]] = {
    "detail": frozenset({"products", "images"}),
    "scatter": frozenset({"background", "pairs", "hollow_if", "labels", "grid"}),
    "manifold": frozenset({"background"}),
    "sky": frozenset({"rings", "fits"}),
    "wavelength": frozenset({"markers"}),
    "spectrum1d": frozenset({"templates"}),
    "gallery": frozenset({"annotate"}),
}

SCHEMA_IDS = {
    "portal": "pk-portal/1",
    "page": "pk-page/1",
    "catalog": "pk-catalog/1",
    "records": "pk-records/1",
    "images": "pk-images/1",
    "hips": "pk-hips/1",
    "overlays": "pk-overlays/1",
    "narrative": "pk-narrative/1",
    "templates": "pk-templates/1",
    "filters": "pk-filters/1",
    "extinction": "pk-extinction/1",
    "isochrones": "pk-isochrones/1",
    "features": "pk-features/1",
    "products": "products/1",
    "deploy": "pk-deploy/1",
}

_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.+-]+)?$")
_BUILD_ID = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_FAMILY = re.compile(r"^[a-z0-9_]+$")
#: stored column key (section 4.4 grammar: no '-', no spaces, not starting with a digit)
_COL_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_ADAPTER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")


class SchemaError(ValueError):
    """Raised by ``assert_valid_*``; ``.errors`` holds the individual messages."""

    def __init__(self, doc: str, errors: list[str]):
        self.doc = doc
        self.errors = list(errors)
        head = f"{doc}: {len(self.errors)} schema error(s)"
        shown = "\n  ".join(self.errors[:50])
        more = f"\n  ... {len(self.errors) - 50} more" if len(self.errors) > 50 else ""
        super().__init__(f"{head}\n  {shown}{more}")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class _E:
    """Error collector."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.missing: set[str] = set()   # paths already reported as missing required keys

    def __call__(self, path: str, msg: str) -> None:
        if path in self.missing and msg.endswith("got NoneType"):
            return   # a type error for a key already reported missing adds nothing
        self.errors.append(f"{path or '<root>'}: {msg}")


def _p(path: str, key: Any) -> str:
    if isinstance(key, int):
        return f"{path}[{key}]"
    return f"{path}.{key}" if path else str(key)


def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _is_list(x: Any) -> bool:
    return isinstance(x, (list, tuple))


#: largest integer that converts to a finite float64 (JSON readers parse numbers as doubles)
_FLOAT_MAX_INT = int(sys.float_info.max)


def _finite(x: int | float) -> bool:
    """``math.isfinite`` that does not raise OverflowError for huge Python ints."""
    if isinstance(x, int):
        return -_FLOAT_MAX_INT <= x <= _FLOAT_MAX_INT
    return math.isfinite(x)


def _is_finite(x: Any) -> bool:
    return _is_num(x) and _finite(x)


def _is_url(s: Any) -> bool:
    return isinstance(s, str) and (s.startswith("https://") or s.startswith("http://"))


def json_errors(obj: Any, path: str = "", e: _E | None = None) -> list[str]:
    """Report values that strict JSON (``allow_nan=False``) cannot encode."""
    e = e if e is not None else _E()
    stack: list[tuple[Any, str]] = [(obj, path)]
    while stack:
        v, p = stack.pop()
        if v is None or isinstance(v, (str, bool)):
            continue
        if isinstance(v, int):
            if not _finite(v):
                e(p, "integer exceeds the float64 range")
            continue
        if isinstance(v, float):
            if not math.isfinite(v):
                e(p, f"non-finite number {v!r} (write null for missing)")
            continue
        if isinstance(v, dict):
            for k, sub in v.items():
                if not isinstance(k, str):
                    e(p, f"non-string key {k!r}")
                    continue
                stack.append((sub, _p(p, k)))
            continue
        if isinstance(v, (list, tuple)):
            for i, sub in enumerate(v):
                stack.append((sub, _p(p, i)))
            continue
        e(p, f"value of type {type(v).__name__} is not JSON-serialisable")
    return e.errors


def _obj(e: _E, path: str, v: Any) -> bool:
    if not isinstance(v, dict):
        e(path, f"expected object, got {type(v).__name__}")
        return False
    return True


def _keys(e: _E, path: str, d: dict, required: tuple = (), optional: tuple = (),
          strict: bool = True) -> None:
    for k in required:
        if k not in d:
            e(path, f"missing required key '{k}'")
            e.missing.add(_p(path, k))
    if strict:
        allowed = set(required) | set(optional)
        for k in d:
            if isinstance(k, str) and k not in allowed and not k.startswith("_"):
                e(path, f"unknown key '{k}'")


def _str(e: _E, path: str, v: Any, nonempty: bool = True, choices: tuple | None = None) -> bool:
    if not isinstance(v, str):
        e(path, f"expected string, got {type(v).__name__}")
        return False
    if nonempty and not v:
        e(path, "empty string")
        return False
    if choices is not None and v not in choices:
        e(path, f"{v!r} not one of {list(choices)}")
        return False
    return True


def _opt_str(e: _E, path: str, d: dict, key: str, **kw) -> None:
    if key in d and d[key] is not None:
        _str(e, _p(path, key), d[key], **kw)


def _num(e: _E, path: str, v: Any, lo: float | None = None, hi: float | None = None,
         lo_open: bool = False, integer: bool = False) -> bool:
    ok = _is_int(v) if integer else _is_num(v)
    if not ok:
        e(path, f"expected {'integer' if integer else 'number'}, got {type(v).__name__}")
        return False
    if not _finite(v):
        e(path, "non-finite number" if isinstance(v, float) else "integer exceeds the float64 range")
        return False
    if lo is not None and (v <= lo if lo_open else v < lo):
        e(path, f"{v!r} below minimum {lo}{' (exclusive)' if lo_open else ''}")
        return False
    if hi is not None and v > hi:
        e(path, f"{v!r} above maximum {hi}")
        return False
    return True


def _bool(e: _E, path: str, v: Any) -> bool:
    if not isinstance(v, bool):
        e(path, f"expected boolean, got {type(v).__name__}")
        return False
    return True


def _list(e: _E, path: str, v: Any, nonempty: bool = False) -> bool:
    if not _is_list(v):
        e(path, f"expected array, got {type(v).__name__}")
        return False
    if nonempty and len(v) == 0:
        e(path, "empty array")
        return False
    return True


def _strlist(e: _E, path: str, v: Any, unique: bool = False, nonempty: bool = False) -> bool:
    if not _list(e, path, v, nonempty=nonempty):
        return False
    ok = True
    for i, s in enumerate(v):
        ok &= _str(e, _p(path, i), s)
    if unique:
        seen = set()
        for i, s in enumerate(v):
            if isinstance(s, str):
                if s in seen:
                    e(_p(path, i), f"duplicate {s!r}")
                    ok = False
                seen.add(s)
    return ok


def _numlist(e: _E, path: str, v: Any, allow_null: bool = False, length: int | None = None) -> bool:
    if not _list(e, path, v):
        return False
    ok = True
    if length is not None and len(v) != length:
        e(path, f"expected {length} values, got {len(v)}")
        ok = False
    for i, x in enumerate(v):
        if x is None and allow_null:
            continue
        if not _is_finite(x):
            e(_p(path, i), f"expected finite number{' or null' if allow_null else ''}, got {x!r}")
            ok = False
    return ok


def _range2(e: _E, path: str, v: Any) -> bool:
    if not _numlist(e, path, v, length=2):
        return False
    if v[0] > v[1]:
        e(path, f"range [{v[0]}, {v[1]}] has lo > hi")
        return False
    return True


def _relpath(e: _E, path: str, v: Any, allow_url: bool = True, allow_parent: bool = False) -> bool:
    """A file reference inside the staged tree (relative, no '..', no leading '/')."""
    if not _str(e, path, v):
        return False
    if allow_url and _is_url(v):
        return True
    if v.startswith("/") or "\\" in v:
        e(path, f"file reference {v!r} must be relative with '/' separators")
        return False
    if not allow_parent and ".." in v.split("/"):
        e(path, f"file reference {v!r} must not contain '..'")
        return False
    return True


def _unique_ids(e: _E, path: str, items: Any, key: str = "id") -> set:
    seen: set = set()
    if not _is_list(items):
        return seen
    for i, it in enumerate(items):
        if isinstance(it, dict) and isinstance(it.get(key), str):
            if it[key] in seen:
                e(_p(_p(path, i), key), f"duplicate {key} {it[key]!r}")
            seen.add(it[key])
    return seen


def _schema_key(e: _E, d: dict, doc: str, required: bool) -> None:
    sid = SCHEMA_IDS[doc]
    if "schema" not in d:
        if required:
            e("schema", f"missing required key 'schema' (expected {sid!r})")
        return
    if d["schema"] != sid:
        e("schema", f"expected {sid!r}, got {d['schema']!r}")


def _radec(e: _E, path: str, d: dict, rkey: str = "ra", dkey: str = "dec") -> None:
    if rkey in d:
        _num(e, _p(path, rkey), d[rkey], -360.0, 360.0)
    if dkey in d:
        _num(e, _p(path, dkey), d[dkey], -90.0, 90.0)


def _sky_point(e: _E, path: str, v: Any) -> bool:
    if not _numlist(e, path, v, length=2):
        return False
    if not (-360.0 <= v[0] <= 360.0):
        e(path, f"ra {v[0]} out of range")
        return False
    if not (-90.0 <= v[1] <= 90.0):
        e(path, f"dec {v[1]} out of range")
        return False
    return True


def _wave(e: _E, path: str, w: Any, grids: dict | None = None) -> None:
    if not _obj(e, path, w):
        return
    kind = w.get("kind")
    if not _str(e, _p(path, "kind"), kind, choices=("uniform", "array", "shared")):
        return
    if kind == "uniform":
        _keys(e, path, w, ("kind", "w0", "dw"), ("n", "unit"), strict=False)
        if "w0" in w:
            _num(e, _p(path, "w0"), w["w0"])
        if "dw" in w:
            _num(e, _p(path, "dw"), w["dw"], 0.0, lo_open=True)
        if "n" in w:
            _num(e, _p(path, "n"), w["n"], 0, integer=True)
    elif kind == "array":
        if "off" in w:
            if _num(e, _p(path, "off"), w["off"], 0, integer=True) and w["off"] % 4:
                e(_p(path, "off"), "offset not 4-byte aligned")
            _str(e, _p(path, "dtype"), w.get("dtype"), choices=("f32", "f64"))
        elif "file" in w:
            _relpath(e, _p(path, "file"), w["file"])
        elif "values" in w:
            _numlist(e, _p(path, "values"), w["values"])
        else:
            e(path, "array wave needs 'off' (record) or 'file'/'values' (shared grid)")
    else:
        if _str(e, _p(path, "grid"), w.get("grid")) and grids is not None and w["grid"] not in grids:
            e(_p(path, "grid"), f"unknown shared grid {w['grid']!r}")


def _segments(e: _E, path: str, segs: Any, n: int | None) -> None:
    if not _list(e, path, segs):
        return
    for i, s in enumerate(segs):
        sp = _p(path, i)
        if not _is_list(s) or len(s) != 3:
            e(sp, "segment must be [i0, i1, label]")
            continue
        i0, i1, lab = s
        if not (_is_int(i0) and _is_int(i1)) or i0 < 0 or i1 <= i0:
            e(sp, f"bad segment bounds {i0!r}, {i1!r}")
        elif n is not None and i1 > n:
            e(sp, f"segment end {i1} beyond n={n}")
        if not isinstance(lab, str):
            e(sp, "segment label must be a string")


# ---------------------------------------------------------------------------
# section 4.1 site registry (TOML)
# ---------------------------------------------------------------------------

def validate_site(obj: Any, stem: str | None = None) -> list[str]:
    """Validate a parsed ``sites/<family>.toml`` (dict from ``tomllib``)."""
    e = _E()
    if not _obj(e, "", obj):
        return e.errors
    _keys(e, "", obj, ("family", "portal"),
          ("adapter", "tier_default", "legacy_paths", "status"))
    status = obj.get("status", "active")
    _str(e, "status", status, choices=("active", "excluded"))
    fam = obj.get("family")
    if _str(e, "family", fam):
        if not _FAMILY.match(fam):
            e("family", f"{fam!r} must match [a-z0-9_]+")
        if stem is not None and fam != stem:
            e("family", f"{fam!r} differs from file stem {stem!r}")
    if "adapter" in obj:
        if _str(e, "adapter", obj["adapter"]) and not _ADAPTER.match(obj["adapter"]):
            e("adapter", f"{obj['adapter']!r} must look like 'adapters.module:ATTR'")
    elif status == "active":
        e("", "missing required key 'adapter' for an active family")
    if "tier_default" in obj:
        _str(e, "tier_default", obj["tier_default"], choices=TIERS)
    if "legacy_paths" in obj:
        if _strlist(e, "legacy_paths", obj["legacy_paths"]):
            for i, p in enumerate(obj["legacy_paths"]):
                if not p.startswith("/"):
                    e(_p("legacy_paths", i), "must be an absolute path")
    portals = obj.get("portal")
    if portals is None:
        return e.errors
    if not _list(e, "portal", portals, nonempty=(status == "active")):
        return e.errors
    _unique_ids(e, "portal", portals)
    for i, por in enumerate(portals):
        pp = _p("portal", i)
        if not _obj(e, pp, por):
            continue
        _keys(e, pp, por, ("id", "title", "visibility", "legacy_dir", "url"),
              ("target_dir", "served_by", "inputs", "aladin_url", "page"))
        _str(e, _p(pp, "id"), por.get("id"))
        _str(e, _p(pp, "title"), por.get("title"))
        _str(e, _p(pp, "visibility"), por.get("visibility"), choices=VISIBILITIES)
        for k in ("legacy_dir", "target_dir"):
            if k in por and _str(e, _p(pp, k), por[k]) and not por[k].startswith("/"):
                e(_p(pp, k), "must be an absolute path")
        if "url" in por and _str(e, _p(pp, "url"), por["url"]):
            if not _is_url(por["url"]) or not por["url"].endswith("/"):
                e(_p(pp, "url"), "must be an http(s) URL ending in '/'")
        if "served_by" in por:
            _str(e, _p(pp, "served_by"), por["served_by"])
        if "inputs" in por:
            _strlist(e, _p(pp, "inputs"), por["inputs"])
        if "aladin_url" in por and not _is_url(por["aladin_url"]):
            e(_p(pp, "aladin_url"), "must be an http(s) URL")
        pages = por.get("page", [])
        if not _list(e, _p(pp, "page"), pages):
            continue
        _unique_ids(e, _p(pp, "page"), pages)
        for j, pg in enumerate(pages):
            gp = _p(_p(pp, "page"), j)
            if not _obj(e, gp, pg):
                continue
            _keys(e, gp, pg, ("id",), ("legacy", "preset", "grammar"))
            _str(e, _p(gp, "id"), pg.get("id"))
            if "legacy" in pg:
                _strlist(e, _p(gp, "legacy"), pg["legacy"])
            if "preset" in pg:
                _str(e, _p(gp, "preset"), pg["preset"], nonempty=False)
            if "grammar" in pg and _strlist(e, _p(gp, "grammar"), pg["grammar"]):
                for k, g in enumerate(pg["grammar"]):
                    if g not in GRAMMARS:
                        e(_p(_p(gp, "grammar"), k), f"unknown grammar {g!r}")
    return e.errors


# ---------------------------------------------------------------------------
# section 4.2 portal.json
# ---------------------------------------------------------------------------

def validate_portal(obj: Any) -> list[str]:
    e = _E()
    if not _obj(e, "", obj):
        return e.errors
    json_errors(obj, "", e)
    _schema_key(e, obj, "portal", True)
    _keys(e, "", obj, ("id", "title", "visibility", "build", "pk", "pages"),
          ("schema", "related", "search"))
    _str(e, "id", obj.get("id"))
    _str(e, "title", obj.get("title"))
    _str(e, "visibility", obj.get("visibility"), choices=VISIBILITIES)
    if "build" in obj and _str(e, "build", obj["build"]) and not _BUILD_ID.match(obj["build"]):
        e("build", f"{obj['build']!r} is not a build id (YYYYMMDDTHHMMSSZ-<8 hex>)")
    if "pk" in obj and _str(e, "pk", obj["pk"]) and not _SEMVER.match(obj["pk"]):
        e("pk", f"{obj['pk']!r} is not semver")
    pages = obj.get("pages")
    page_ids: set = set()
    if pages is not None and _list(e, "pages", pages):
        page_ids = _unique_ids(e, "pages", pages)
        files = set()
        for i, pg in enumerate(pages):
            pp = _p("pages", i)
            if not _obj(e, pp, pg):
                continue
            _keys(e, pp, pg, ("id", "title", "file", "archetype"), ("subtitle",))
            _str(e, _p(pp, "id"), pg.get("id"))
            _str(e, _p(pp, "title"), pg.get("title"))
            if _relpath(e, _p(pp, "file"), pg.get("file"), allow_url=False):
                if not pg["file"].endswith(".html"):
                    e(_p(pp, "file"), "must be an .html file")
                if pg["file"] in files:
                    e(_p(pp, "file"), f"duplicate file {pg['file']!r}")
                files.add(pg["file"])
            _str(e, _p(pp, "archetype"), pg.get("archetype"), choices=ARCHETYPES)
    if "related" in obj and _list(e, "related", obj["related"]):
        for i, r in enumerate(obj["related"]):
            rp = _p("related", i)
            if not _obj(e, rp, r):
                continue
            _keys(e, rp, r, ("title", "url", "auth"))
            _str(e, _p(rp, "title"), r.get("title"))
            if "url" in r and _str(e, _p(rp, "url"), r["url"]):
                if not _is_url(r["url"]) and r["url"].startswith("/"):
                    e(_p(rp, "url"), "must be an absolute http(s) URL or a relative path")
            _str(e, _p(rp, "auth"), r.get("auth"), choices=AUTHS)
    if "search" in obj and _list(e, "search", obj["search"]):
        for i, s in enumerate(obj["search"]):
            sp = _p("search", i)
            if not _obj(e, sp, s):
                continue
            _keys(e, sp, s, ("ds", "page", "manifest"))
            _str(e, _p(sp, "ds"), s.get("ds"))
            if _str(e, _p(sp, "page"), s.get("page")) and pages is not None and s["page"] not in page_ids:
                e(_p(sp, "page"), f"page {s['page']!r} is not listed in pages")
            _relpath(e, _p(sp, "manifest"), s.get("manifest"))
    return e.errors


# ---------------------------------------------------------------------------
# section 4.3 page.json
# ---------------------------------------------------------------------------

_DSREF_KEYS = {
    "catalog": (("kind", "url"), ("context",)),
    "records": (("kind", "url", "catalog", "record"), ()),
    "images": (("kind", "url"), ("catalog",)),
    "templates": (("kind", "url"), ()),
    "filters": (("kind", "url"), ()),
    "extinction": (("kind", "url"), ()),
    "isochrones": (("kind", "url"), ()),
    "features": (("kind", "url"), ()),
    "hips": (("kind", "url"), ()),
    "overlays": (("kind", "url"), ()),
    "narrative": (("kind", "url"), ()),
    "portal": (("kind", "url"), ()),
}

_STATE_FIXED = ("focus", "hover", "color", "av", "rv", "dm", "template", "features", "spec",
                "sky", "rung", "beat", "role", "facets", "lightbox", "theme", "layout")
_STATE_DS_PREFIX = ("sel:", "vis:", "filters:", "annot:")
_STATE_PANE_PREFIX = ("axes:", "frame:", "tier:")
_FILTER_KEYS = ("cats", "ranges", "flags", "snr", "text", "ids")


def _check_option(e: _E, path: str, kind: str | None, v: Any, datasets: dict) -> None:
    if kind is None:
        return
    if kind == "str":
        _str(e, path, v)
    elif kind == "bool":
        _bool(e, path, v)
    elif kind == "strlist":
        _strlist(e, path, v)
    elif kind == "numlist":
        _numlist(e, path, v)
    elif kind == "list":
        _list(e, path, v)
    elif kind == "obj":
        _obj(e, path, v)
    elif kind == "posnum":
        _num(e, path, v, 0.0, lo_open=True)
    elif kind == "posint":
        _num(e, path, v, 1, integer=True)
    elif kind == "pin":
        _num(e, path, v, 1, 8, integer=True)
    elif kind == "dims":
        if isinstance(v, str):
            if not v.startswith("group:") or len(v) <= 6:
                e(path, f"{v!r}: expected a key list or 'group:<g>'")
        else:
            _strlist(e, path, v, nonempty=True)
    elif kind == "models":
        if _strlist(e, path, v):
            for i, m in enumerate(v):
                if m in ("template", "blackbody"):
                    continue
                if m.startswith("sedcurve:"):
                    ds = m[len("sedcurve:"):]
                    if ds not in datasets:
                        e(_p(path, i), f"unknown dataset {ds!r}")
                    continue
                e(_p(path, i), f"unknown model {m!r}")
    elif kind.startswith("enum:"):
        _str(e, path, v, choices=tuple(kind[5:].split("|")))
    elif kind.startswith("ds:"):
        want = kind[3:]
        if _str(e, path, v):
            if v not in datasets:
                e(path, f"unknown dataset {v!r}")
            elif isinstance(datasets[v], dict) and datasets[v].get("kind") != want:
                e(path, f"dataset {v!r} has kind {datasets[v].get('kind')!r}, expected {want!r}")
    else:  # pragma: no cover - table typo guard
        raise AssertionError(f"unknown option checker {kind!r}")


def _layout(e: _E, path: str, node: Any, datasets: dict, pane_ids: dict, depth: int = 0) -> None:
    if depth > 32:
        e(path, "layout nested deeper than 32 levels")
        return
    if not _obj(e, path, node):
        return
    kinds = [k for k in ("row", "col", "pane") if k in node]
    if len(kinds) != 1:
        e(path, "layout node needs exactly one of 'row', 'col', 'pane'")
        return
    if "size" in node:
        _num(e, _p(path, "size"), node["size"], 0.0, lo_open=True)
    if "collapsed" in node:
        _bool(e, _p(path, "collapsed"), node["collapsed"])
    k = kinds[0]
    if k in ("row", "col"):
        _keys(e, path, node, (k,), ("size", "id", "collapsed"))
        if "id" in node:
            _str(e, _p(path, "id"), node["id"])
        if _list(e, _p(path, k), node[k], nonempty=True):
            for i, child in enumerate(node[k]):
                _layout(e, _p(_p(path, k), i), child, datasets, pane_ids, depth + 1)
        return
    _keys(e, path, node, ("pane",), ("id", "dataset", "collapsed", "size", "options"))
    ptype = node["pane"]
    if not _str(e, _p(path, "pane"), ptype, choices=PANE_TYPES):
        return
    pid = node.get("id", ptype)
    if _str(e, _p(path, "id"), pid):
        if pid in pane_ids:
            e(_p(path, "id"), f"duplicate pane id {pid!r} (also at {pane_ids[pid]})")
        else:
            pane_ids[pid] = path
    if "dataset" in node and node["dataset"] is not None:
        if _str(e, _p(path, "dataset"), node["dataset"]) and node["dataset"] not in datasets:
            e(_p(path, "dataset"), f"unknown dataset {node['dataset']!r}")
    if "options" in node:
        opts = node["options"]
        if _obj(e, _p(path, "options"), opts):
            table = PANE_OPTIONS[ptype]
            for ok, ov in opts.items():
                op = _p(_p(path, "options"), ok)
                if ok not in table:
                    e(op, f"unknown option for pane '{ptype}'")
                    continue
                if ov is None and ok in OPTIONAL_PANE_OPTIONS.get(ptype, ()):
                    continue
                _check_option(e, op, table[ok], ov, datasets)


def _state(e: _E, st: dict, datasets: dict, pane_ids: dict) -> None:
    for key, v in st.items():
        if not isinstance(key, str):
            continue   # already reported by json_errors
        sp = _p("state", key)
        if key in _STATE_FIXED:
            pass
        elif key.startswith(_STATE_DS_PREFIX):
            ds = key.split(":", 1)[1]
            if ds not in datasets:
                e(sp, f"unknown dataset {ds!r}")
        elif key.startswith(_STATE_PANE_PREFIX):
            pid = key.split(":", 1)[1]
            if pid not in pane_ids:
                e(sp, f"unknown pane id {pid!r}")
        else:
            e(sp, "unknown store key")
            continue
        if key in ("av", "dm"):
            _num(e, sp, v, 0.0 if key == "av" else None)
        elif key == "rv":
            _num(e, sp, v, 0.0, lo_open=True)
        elif key in ("rung", "beat", "role"):
            if v is not None:
                _str(e, sp, v)
        elif key == "theme":
            _str(e, sp, v, choices=("auto", "light", "dark"))
        elif key == "features":
            _strlist(e, sp, v)
        elif key == "focus":
            if v is not None and _obj(e, sp, v):
                _keys(e, sp, v, ("ds",), ("i", "id"))
                if _str(e, _p(sp, "ds"), v.get("ds")) and v["ds"] not in datasets:
                    e(_p(sp, "ds"), f"unknown dataset {v['ds']!r}")
                if "i" in v and v["i"] is not None:
                    _num(e, _p(sp, "i"), v["i"], 0, integer=True)
                if "id" in v and v["id"] is not None:
                    _str(e, _p(sp, "id"), v["id"])
        elif key == "color":
            if v is not None and _obj(e, sp, v):
                _keys(e, sp, v, ("by",), ("cmap", "lo", "hi"))
                if v.get("by") is not None:
                    _str(e, _p(sp, "by"), v["by"])
                for b in ("lo", "hi"):
                    if b in v and v[b] is not None:
                        _num(e, _p(sp, b), v[b])
        elif key == "template":
            if v is not None and _obj(e, sp, v):
                _keys(e, sp, v, ("key",), ("grid", "scale", "bands"))
                if "scale" in v and v["scale"] != "auto":
                    _num(e, _p(sp, "scale"), v["scale"], 0.0, lo_open=True)
        elif key == "spec":
            if _obj(e, sp, v) and "pinned" in v:
                if _strlist(e, _p(sp, "pinned"), v["pinned"]) and len(v["pinned"]) > 8:
                    e(_p(sp, "pinned"), "at most 8 pinned spectra")
        elif key == "sky":
            if _obj(e, sp, v):
                _radec(e, sp, v)
                if "fov" in v:
                    _num(e, _p(sp, "fov"), v["fov"], 0.0, 360.0, lo_open=True)
        elif key == "facets":
            if _obj(e, sp, v):
                for fk, fv in v.items():
                    _strlist(e, _p(sp, fk), fv)
        elif key == "lightbox":
            if v is not None and _obj(e, sp, v):
                _keys(e, sp, v, ("set", "index"))
                if "index" in v:
                    _num(e, _p(sp, "index"), v["index"], 0, integer=True)
        elif key.startswith("filters:"):
            if _obj(e, sp, v):
                _keys(e, sp, v, (), _FILTER_KEYS)
                if "ranges" in v and _obj(e, _p(sp, "ranges"), v["ranges"]):
                    for rk, rv in v["ranges"].items():
                        _range2(e, _p(_p(sp, "ranges"), rk), rv)
                if "cats" in v and _obj(e, _p(sp, "cats"), v["cats"]):
                    for ck, cv in v["cats"].items():
                        _strlist(e, _p(_p(sp, "cats"), ck), cv)
        elif key.startswith("axes:"):
            if _obj(e, sp, v):
                _keys(e, sp, v, (), ("x", "y", "xlog", "ylog", "xinv", "yinv", "xr", "yr"))
                for a in ("x", "y"):
                    if a in v:
                        _str(e, _p(sp, a), v[a])
                for a in ("xr", "yr"):
                    if a in v and v[a] is not None:
                        _numlist(e, _p(sp, a), v[a], length=2)
        elif key.startswith("tier:"):
            _str(e, sp, v, choices=PANE_TIERS)
        elif key in ("hover", "layout") or key.startswith(("sel:", "vis:", "annot:", "frame:")):
            if v is not None and not isinstance(v, (dict, str)):
                e(sp, f"expected object, got {type(v).__name__}")


def validate_page(obj: Any) -> list[str]:
    e = _E()
    if not _obj(e, "", obj):
        return e.errors
    json_errors(obj, "", e)
    _schema_key(e, obj, "page", True)
    _keys(e, "", obj, ("pk", "assets", "res", "portal", "page", "datasets", "layout",
                       "state", "caps", "aladin"),
          ("schema", "links", "readme", "budget"))
    if "pk" in obj and _str(e, "pk", obj["pk"]) and not _SEMVER.match(obj["pk"]):
        e("pk", f"{obj['pk']!r} is not semver")
    for k in ("assets", "res"):
        if k in obj and _str(e, k, obj[k]) and not obj[k].endswith("/"):
            e(k, "must end in '/'")
    por = obj.get("portal")
    if por is not None and _obj(e, "portal", por):
        _keys(e, "portal", por, ("id", "title", "visibility", "nav"))
        _str(e, "portal.id", por.get("id"))
        _str(e, "portal.title", por.get("title"))
        _str(e, "portal.visibility", por.get("visibility"), choices=VISIBILITIES)
        _str(e, "portal.nav", por.get("nav"))
    pg = obj.get("page")
    if pg is not None and _obj(e, "page", pg):
        _keys(e, "page", pg, ("id", "archetype", "title"), ("subtitle", "legacy"))
        _str(e, "page.id", pg.get("id"))
        _str(e, "page.archetype", pg.get("archetype"), choices=ARCHETYPES)
        _str(e, "page.title", pg.get("title"))
        _opt_str(e, "page", pg, "subtitle", nonempty=False)
        if "legacy" in pg and pg["legacy"] is not None and _obj(e, "page.legacy", pg["legacy"]):
            lg = pg["legacy"]
            _keys(e, "page.legacy", lg, ("files",), ("grammar", "preset"))
            if "files" in lg:
                _strlist(e, "page.legacy.files", lg["files"])
            if "grammar" in lg and _strlist(e, "page.legacy.grammar", lg["grammar"]):
                for i, g in enumerate(lg["grammar"]):
                    if g not in GRAMMARS:
                        e(_p("page.legacy.grammar", i), f"unknown grammar {g!r}")
    datasets = obj.get("datasets")
    if datasets is None or not _obj(e, "datasets", datasets):
        datasets = {}
    for ds, ref in datasets.items():
        dp = _p("datasets", ds)
        if not _obj(e, dp, ref):
            continue
        kind = ref.get("kind")
        if not _str(e, _p(dp, "kind"), kind, choices=DATASET_KINDS):
            continue
        req, opt = _DSREF_KEYS[kind]
        _keys(e, dp, ref, req, opt)
        _str(e, _p(dp, "url"), ref.get("url"))
        if "context" in ref:
            _bool(e, _p(dp, "context"), ref["context"])
        if kind == "records":
            _str(e, _p(dp, "record"), ref.get("record"), choices=RECORD_KINDS)
        if "catalog" in ref and ref["catalog"] is not None:
            c = ref["catalog"]
            if _str(e, _p(dp, "catalog"), c):
                if c not in datasets:
                    e(_p(dp, "catalog"), f"unknown dataset {c!r}")
                elif isinstance(datasets[c], dict) and datasets[c].get("kind") != "catalog":
                    e(_p(dp, "catalog"), f"dataset {c!r} is not a catalog")
    pane_ids: dict = {}
    if "layout" in obj:
        _layout(e, "layout", obj["layout"], datasets, pane_ids)
    if "state" in obj and _obj(e, "state", obj["state"]):
        _state(e, obj["state"], datasets, pane_ids)
    caps = obj.get("caps")
    if caps is not None and _obj(e, "caps", caps):
        _keys(e, "caps", caps, ("enabled", "disabled"))
        en = caps.get("enabled", [])
        _strlist(e, "caps.enabled", en, unique=True)
        dis = caps.get("disabled", {})
        if _obj(e, "caps.disabled", dis):
            for ck, cv in dis.items():
                _str(e, _p("caps.disabled", ck), cv)
                if _is_list(en) and ck in en:
                    e(_p("caps.disabled", ck), "capability is both enabled and disabled")
    if "links" in obj and _obj(e, "links", obj["links"]):
        for lk, lv in obj["links"].items():
            _str(e, _p("links", lk), lv)
    if "readme" in obj and obj["readme"] is not None:
        rd = obj["readme"]
        if _obj(e, "readme", rd):
            _keys(e, "readme", rd, (), ("html", "url", "provenance"))
            if ("html" in rd) == ("url" in rd):
                e("readme", "needs exactly one of 'html' or 'url'")
            if "html" in rd and _str(e, "readme.html", rd["html"], nonempty=False):
                if len(rd["html"].encode("utf-8")) > 16384:
                    e("readme.html", "inline readme exceeds 16 kB; write readme.<h8>.html and use 'url'")
            if "url" in rd:
                _relpath(e, "readme.url", rd["url"])
            if "provenance" in rd and rd["provenance"] is not None:
                _obj(e, "readme.provenance", rd["provenance"])
    al = obj.get("aladin")
    if al is not None and _obj(e, "aladin", al):
        _keys(e, "aladin", al, ("cdn", "mirror"))
        for k in ("cdn", "mirror"):
            if k in al and not _is_url(al[k]):
                e(_p("aladin", k), "must be an http(s) URL")
    if "budget" in obj and _obj(e, "budget", obj["budget"]):
        _keys(e, "budget", obj["budget"], (), ("html_bytes", "first_paint_bytes"))
        for k, v in obj["budget"].items():
            _num(e, _p("budget", k), v, 0, integer=True)
    if not json_errors(obj):
        size = len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if size > PAGE_JSON_MAX:
            e("", f"page.json is {size} bytes, above PAGE_JSON_MAX={PAGE_JSON_MAX}")
    return e.errors


# ---------------------------------------------------------------------------
# section 5.2 catalog manifest
# ---------------------------------------------------------------------------

_COL_KEYS_REQ = ("key", "role", "enc")
_COL_KEYS_OPT = ("file", "chunk_files", "label", "unit", "filter", "err", "group", "note", "log",
                 "invert", "fmt", "lo", "hi", "step", "clip", "shuffle", "center", "scale",
                 "quantiles", "nvalid", "levels", "colors", "default_off", "limits", "validity")


def _col(e: _E, cp: str, c: dict, n: Any, chunk_rows: Any) -> None:
    _keys(e, cp, c, _COL_KEYS_REQ, _COL_KEYS_OPT)
    if _str(e, _p(cp, "key"), c.get("key")) and not _COL_KEY.match(c["key"]):
        e(_p(cp, "key"), f"{c['key']!r} must match [A-Za-z_][A-Za-z0-9_.]* (section 4.4)")
    _str(e, _p(cp, "role"), c.get("role"), choices=ROLES)
    enc = c.get("enc")
    _str(e, _p(cp, "enc"), enc, choices=ENCODINGS)
    has_file = "file" in c and c["file"] is not None
    has_chunks = "chunk_files" in c and c["chunk_files"] is not None
    if has_file == has_chunks:
        e(cp, "needs exactly one of 'file' or 'chunk_files'")
    if has_file:
        _relpath(e, _p(cp, "file"), c["file"])
    if has_chunks and _list(e, _p(cp, "chunk_files"), c["chunk_files"], nonempty=True):
        for i, f in enumerate(c["chunk_files"]):
            _relpath(e, _p(_p(cp, "chunk_files"), i), f)
        if _is_int(n) and _is_int(chunk_rows) and chunk_rows > 0:
            want = max(1, -(-n // chunk_rows))
            if len(c["chunk_files"]) != want:
                e(_p(cp, "chunk_files"), f"{len(c['chunk_files'])} chunks, expected {want} "
                                        f"(n={n}, chunk_rows={chunk_rows})")
    for k in ("label", "unit", "filter", "err", "group", "note", "fmt", "validity"):
        _opt_str(e, cp, c, k, nonempty=(k not in ("unit", "note", "label")))
    for k in ("log", "invert", "shuffle"):
        if k in c and c[k] is not None:
            _bool(e, _p(cp, k), c[k])
    if "clip" in c:
        _str(e, _p(cp, "clip"), c["clip"], choices=CLIP_MODES)
    for k in ("lo", "hi", "center"):
        if k in c and c[k] is not None:
            _num(e, _p(cp, k), c[k])
    for k in ("step", "scale"):
        if k in c and c[k] is not None:
            _num(e, _p(cp, k), c[k], 0.0, lo_open=True)
    if "nvalid" in c and c["nvalid"] is not None:
        if _num(e, _p(cp, "nvalid"), c["nvalid"], 0, integer=True) and _is_int(n) and c["nvalid"] > n:
            e(_p(cp, "nvalid"), f"nvalid {c['nvalid']} > n {n}")
    if enc in _QUANT_ENCS:
        for k in ("lo", "hi"):
            if c.get(k) is None:
                e(cp, f"enc {enc} requires '{k}'")
        if _is_finite(c.get("lo")) and _is_finite(c.get("hi")) and c["lo"] > c["hi"]:
            e(cp, f"lo {c['lo']} > hi {c['hi']}")
        # 'step' is optional for every quantized encoding (section 5.2); readers derive
        # (hi-lo)/65534 for u16q/u16log and (hi-lo)/254 for u8q when it is absent.
        if enc == "u8log" and _is_finite(c.get("lo")) and c["lo"] <= 0:
            e(_p(cp, "lo"), "u8log lo must be > 0 (linear units)")
    if "quantiles" in c and c["quantiles"] is not None:
        q = c["quantiles"]
        if _numlist(e, _p(cp, "quantiles"), q, allow_null=True, length=101):
            vals = [x for x in q if x is not None]
            if any(b < a for a, b in zip(vals, vals[1:])):
                e(_p(cp, "quantiles"), "quantiles must be non-decreasing")
    if enc in ("cat8", "cat16"):
        lv = c.get("levels")
        if lv is None:
            e(cp, f"enc {enc} requires 'levels'")
        elif _strlist(e, _p(cp, "levels"), lv, unique=True):
            cap = 255 if enc == "cat8" else 65535
            if len(lv) > cap:
                e(_p(cp, "levels"), f"{len(lv)} levels exceed {enc} capacity {cap}")
    if "levels" in c and c["levels"] is not None and enc not in ("cat8", "cat16"):
        _strlist(e, _p(cp, "levels"), c["levels"], unique=True)
    if "colors" in c and c["colors"] is not None and _obj(e, _p(cp, "colors"), c["colors"]):
        lv = c.get("levels") if _is_list(c.get("levels")) else None
        for k, v in c["colors"].items():
            _str(e, _p(_p(cp, "colors"), k), v)
            if lv is not None and k not in lv:
                e(_p(_p(cp, "colors"), k), "colour for a level not in 'levels'")
    if "default_off" in c and c["default_off"] is not None:
        if _strlist(e, _p(cp, "default_off"), c["default_off"]):
            lv = c.get("levels") if _is_list(c.get("levels")) else None
            for i, v in enumerate(c["default_off"]):
                if lv is not None and v not in lv:
                    e(_p(_p(cp, "default_off"), i), f"{v!r} not in levels")
    if "limits" in c and c["limits"] is not None:
        lp = _p(cp, "limits")
        if _obj(e, lp, c["limits"]):
            _keys(e, lp, c["limits"], ("file", "count"))
            _relpath(e, _p(lp, "file"), c["limits"].get("file"))
            if "count" in c["limits"]:
                _num(e, _p(lp, "count"), c["limits"]["count"], 0, integer=True)


def validate_catalog(obj: Any) -> list[str]:
    e = _E()
    if not _obj(e, "", obj):
        return e.errors
    json_errors(obj, "", e)
    _schema_key(e, obj, "catalog", True)
    _keys(e, "", obj, ("id", "n", "order", "cols"),
          ("schema", "title", "build", "chunk_rows", "ids", "pos", "bbox", "virtual", "groups", "presets",
           "density", "rows", "manifold", "links", "provenance", "caveats", "budget"))
    _str(e, "id", obj.get("id"))
    _opt_str(e, "", obj, "title", nonempty=False)
    if "build" in obj and _str(e, "build", obj["build"]) and not _BUILD_ID.match(obj["build"]):
        e("build", f"{obj['build']!r} is not a build id")
    n = obj.get("n")
    if "n" in obj:
        if not _num(e, "n", n, 0, integer=True):
            n = None
    chunk_rows = obj.get("chunk_rows")
    if "chunk_rows" in obj and not _num(e, "chunk_rows", chunk_rows, 1, integer=True):
        chunk_rows = None
    order = obj.get("order")
    if order is not None and _obj(e, "order", order):
        _keys(e, "order", order, ("kind",), ("seed", "levels", "sort"))
        kind = order.get("kind")
        _str(e, "order.kind", kind, choices=("input", "lod"))
        if "seed" in order:
            _num(e, "order.seed", order["seed"], 0, integer=True)
        if "sort" in order and order["sort"] is not None:
            _str(e, "order.sort", order["sort"])
        lv = order.get("levels")
        if kind == "lod" and lv is None:
            e("order", "lod order requires 'levels'")
        if lv is not None and _list(e, "order.levels", lv, nonempty=True):
            if all(_is_int(x) for x in lv):
                if any(b <= a for a, b in zip(lv, lv[1:])) or lv[0] <= 0:
                    e("order.levels", "levels must be positive and strictly increasing")
                if _is_int(n) and lv[-1] != n:
                    e("order.levels", f"last level {lv[-1]} != n {n}")
            else:
                e("order.levels", "levels must be integers")
    ids = obj.get("ids")
    if ids is not None and _obj(e, "ids", ids):
        _keys(e, "ids", ids, ("file", "enc"), ("chunks", "alias_file"))
        _str(e, "ids.enc", ids.get("enc"), choices=("str",))
        if ids.get("file") is not None:
            _relpath(e, "ids.file", ids["file"])
        if ids.get("chunks") is not None and _list(e, "ids.chunks", ids["chunks"]):
            for i, f in enumerate(ids["chunks"]):
                _relpath(e, _p("ids.chunks", i), f)
        if ids.get("alias_file") is not None:
            _relpath(e, "ids.alias_file", ids["alias_file"])
    pos = obj.get("pos")
    if pos is not None and _obj(e, "pos", pos):
        _keys(e, "pos", pos, ("enc", "files"), ("step_mas", "tile_refs", "lo", "hi"))
        penc = pos.get("enc")
        _str(e, "pos.enc", penc, choices=("i16off", "u32lin"))
        files = pos.get("files")
        if files is not None and _obj(e, "pos.files", files):
            need = ("tile", "dra", "ddec") if penc == "i16off" else ("ra", "dec")
            _keys(e, "pos.files", files, need)
            for k, f in files.items():
                _relpath(e, _p("pos.files", k), f)
        if penc == "i16off":
            if "step_mas" not in pos:
                e("pos", "i16off requires 'step_mas'")
            else:
                _num(e, "pos.step_mas", pos["step_mas"], 0.0, lo_open=True)
            tr = pos.get("tile_refs")
            if tr is None:
                e("pos", "i16off requires 'tile_refs'")
            elif _list(e, "pos.tile_refs", tr, nonempty=True):
                for i, t in enumerate(tr):
                    _sky_point(e, _p("pos.tile_refs", i), t)
        if penc == "u32lin" and "bbox" not in obj:
            e("pos", "u32lin positions require 'bbox'")
    bbox = obj.get("bbox")
    if bbox is not None and _obj(e, "bbox", bbox):
        _keys(e, "bbox", bbox, ("ra", "dec"))
        for k in ("ra", "dec"):
            if k in bbox:
                _range2(e, _p("bbox", k), bbox[k])
    cols = obj.get("cols")
    col_keys: set = set()
    if cols is not None and _list(e, "cols", cols):
        col_keys = _unique_ids(e, "cols", cols, key="key")
        for i, c in enumerate(cols):
            cp = _p("cols", i)
            if _obj(e, cp, c):
                _col(e, cp, c, n, chunk_rows if chunk_rows is not None else CHUNK_ROWS)
        for i, c in enumerate(cols):
            if not isinstance(c, dict):
                continue
            for ref in ("err", "validity"):
                r = c.get(ref)
                if isinstance(r, str) and r not in col_keys:
                    e(_p(_p("cols", i), ref), f"references unknown column {r!r}")
    vkeys: set = set()
    if "virtual" in obj and _list(e, "virtual", obj["virtual"]):
        vkeys = _unique_ids(e, "virtual", obj["virtual"], key="key")
        for i, v in enumerate(obj["virtual"]):
            vp = _p("virtual", i)
            if not _obj(e, vp, v):
                continue
            _keys(e, vp, v, ("key",), ("label", "unit", "group", "note", "fmt"), strict=False)
            if _str(e, _p(vp, "key"), v.get("key")) and v["key"] in col_keys:
                e(_p(vp, "key"), f"virtual key {v['key']!r} clashes with a stored column")
    all_keys = col_keys | vkeys
    if "groups" in obj and _list(e, "groups", obj["groups"]):
        _unique_ids(e, "groups", obj["groups"], key="key")
        for i, g in enumerate(obj["groups"]):
            gp = _p("groups", i)
            if not _obj(e, gp, g):
                continue
            _keys(e, gp, g, ("key", "label", "dims"), strict=False)
            if "dims" in g and _strlist(e, _p(gp, "dims"), g["dims"]):
                for j, d in enumerate(g["dims"]):
                    if d not in all_keys:
                        e(_p(_p(gp, "dims"), j), f"unknown dim {d!r}")
    if "presets" in obj and _list(e, "presets", obj["presets"]):
        _unique_ids(e, "presets", obj["presets"], key="key")
        for i, pr in enumerate(obj["presets"]):
            pp = _p("presets", i)
            if not _obj(e, pp, pr):
                continue
            _keys(e, pp, pr, ("key", "label", "kind"), strict=False)
            kind = pr.get("kind")
            if not _str(e, _p(pp, "kind"), kind, choices=PRESET_KINDS):
                continue
            if kind == "axes":
                for a in ("x", "y"):
                    _str(e, _p(pp, a), pr.get(a))
            elif kind == "frame":
                dims, F = pr.get("dims"), pr.get("F")
                ok = _strlist(e, _p(pp, "dims"), dims, nonempty=True)
                if _numlist(e, _p(pp, "F"), F) and ok and len(F) != 2 * len(dims):
                    e(_p(pp, "F"), f"F has {len(F)} values, expected 2*len(dims)={2 * len(dims)}")
            elif kind == "selection":
                _str(e, _p(pp, "sel"), pr.get("sel"))
            elif kind == "filter":
                if "filters" in pr:
                    _obj(e, _p(pp, "filters"), pr["filters"])
                else:
                    e(pp, "filter preset requires 'filters'")
    if "density" in obj and _list(e, "density", obj["density"]):
        _unique_ids(e, "density", obj["density"], key="key")
        for i, d in enumerate(obj["density"]):
            dp = _p("density", i)
            if not _obj(e, dp, d):
                continue
            _keys(e, dp, d, ("key", "x", "y", "xr", "yr", "nx", "ny", "enc", "lo", "hi", "file"),
                  strict=False)
            for a in ("x", "y"):
                _str(e, _p(dp, a), d.get(a))
            for a in ("xr", "yr"):
                if a in d:
                    _range2(e, _p(dp, a), d[a])
            for a in ("nx", "ny"):
                if a in d:
                    _num(e, _p(dp, a), d[a], 1, integer=True)
            _str(e, _p(dp, "enc"), d.get("enc"), choices=("u16log", "u16q", "f32"))
            for a in ("lo", "hi"):
                if a in d:
                    _num(e, _p(dp, a), d[a])
            if "file" in d:
                _relpath(e, _p(dp, "file"), d["file"])
    rows = obj.get("rows")
    if rows is not None and _obj(e, "rows", rows):
        _keys(e, "rows", rows, ("chunk", "files"), ("cols",))
        if "chunk" in rows:
            _num(e, "rows.chunk", rows["chunk"], 1, integer=True)
        if "files" in rows and _list(e, "rows.files", rows["files"]):
            for i, f in enumerate(rows["files"]):
                _relpath(e, _p("rows.files", i), f)
            if _is_int(n) and _is_int(rows.get("chunk")) and rows["chunk"] > 0:
                want = -(-n // rows["chunk"])
                if len(rows["files"]) != want:
                    e("rows.files", f"{len(rows['files'])} row chunks, expected {want}")
        if "cols" in rows:
            _strlist(e, "rows.cols", rows["cols"])
    man = obj.get("manifold")
    if man is not None and _obj(e, "manifold", man):
        _keys(e, "manifold", man, ("dims",), ("default_frame", "fade_width", "frames"))
        if "dims" in man and _strlist(e, "manifold.dims", man["dims"], unique=True, nonempty=True):
            for j, d in enumerate(man["dims"]):
                if all_keys and d not in all_keys:
                    e(_p("manifold.dims", j), f"unknown dim {d!r}")
        if "fade_width" in man:
            _num(e, "manifold.fade_width", man["fade_width"], 0.0)
        _opt_str(e, "manifold", man, "default_frame")
    if "links" in obj and _obj(e, "links", obj["links"]):
        lk = obj["links"]
        if "records" in lk and _obj(e, "links.records", lk["records"]):
            for k, v in lk["records"].items():
                _relpath(e, _p("links.records", k), v, allow_parent=True)
        if "images" in lk and lk["images"] is not None:
            _relpath(e, "links.images", lk["images"], allow_parent=True)
    if "provenance" in obj and obj["provenance"] is not None:
        _obj(e, "provenance", obj["provenance"])
    if "caveats" in obj:
        _strlist(e, "caveats", obj["caveats"])
    if "budget" in obj and _obj(e, "budget", obj["budget"]):
        for k, v in obj["budget"].items():
            _num(e, _p("budget", k), v, 0, integer=True)
    return e.errors


# ---------------------------------------------------------------------------
# sections 5.6-5.8 records
# ---------------------------------------------------------------------------

def validate_records(obj: Any) -> list[str]:
    e = _E()
    if not _obj(e, "", obj):
        return e.errors
    json_errors(obj, "", e)
    kind = obj.get("kind")
    # section 5.6 writes the json-legacy index without a 'schema' key; every other kind needs it
    _schema_key(e, obj, "records", kind != "json-legacy")
    if not _str(e, "kind", kind, choices=RECORD_KINDS + ("json-legacy",)):
        return e.errors
    if kind == "json-legacy":
        _keys(e, "", obj, ("kind", "dir", "pattern"), ("schema", "catalog", "key", "n"))
        _str(e, "dir", obj.get("dir"))
        if _str(e, "pattern", obj.get("pattern")) and "{id}" not in obj["pattern"]:
            e("pattern", "pattern must contain '{id}'")
        return e.errors
    _keys(e, "", obj, ("kind", "layout", "n", "entries"),
          ("schema", "catalog", "key", "shards", "grids", "variants", "unit_w", "unit_f"))
    _str(e, "layout", obj.get("layout"), choices=("shard", "file"))
    _opt_str(e, "", obj, "catalog")
    _opt_str(e, "", obj, "key")
    _opt_str(e, "", obj, "unit_w", nonempty=False)
    _opt_str(e, "", obj, "unit_f", nonempty=False)
    shards = obj.get("shards", [])
    nsh = 0
    if _list(e, "shards", shards):
        nsh = len(shards)
        for i, s in enumerate(shards):
            _relpath(e, _p("shards", i), s, allow_url=False)
    entries = obj.get("entries")
    if entries is not None and _obj(e, "entries", entries):
        if "n" in obj and _num(e, "n", obj["n"], 0, integer=True) and obj["n"] != len(entries):
            e("n", f"n={obj['n']} but {len(entries)} entries")
        for rid, ent in entries.items():
            ep = _p("entries", rid)
            if not rid:
                e(ep, "empty record id")
            if isinstance(ent, str):
                _relpath(e, ep, ent, allow_url=False)
            elif _is_list(ent):
                if len(ent) != 3 or not all(_is_int(x) for x in ent):
                    e(ep, "shard entry must be [shard, offset, length] integers")
                    continue
                sh, off, ln = ent
                if not (0 <= sh < nsh):
                    e(ep, f"shard index {sh} out of range (have {nsh})")
                if off < 0 or ln <= 0:
                    e(ep, f"bad offset/length {off}, {ln}")
            elif isinstance(ent, dict):
                if kind != "pki1":
                    e(ep, "PNG-mode entries are only valid for pki1 record sets")
                    continue
                _keys(e, ep, ent, ("png", "extent"))
                _str(e, _p(ep, "png"), ent.get("png"))
                ext = ent.get("extent")
                if ext is not None and _obj(e, _p(ep, "extent"), ext):
                    _keys(e, _p(ep, "extent"), ext, ("w", "y"))
                    for k in ("w", "y"):
                        if k in ext:
                            _numlist(e, _p(_p(ep, "extent"), k), ext[k], length=2)
            else:
                e(ep, "entry must be [shard, off, len], a file path or a PNG-mode object")
    elif "n" in obj:
        _num(e, "n", obj["n"], 0, integer=True)
    if "grids" in obj and _obj(e, "grids", obj["grids"]):
        for g, gv in obj["grids"].items():
            gp = _p("grids", g)
            _wave(e, gp, gv)
            if isinstance(gv, dict) and gv.get("kind") == "shared":
                e(gp, "a grid cannot itself be kind 'shared'")
            if isinstance(gv, dict) and gv.get("kind") == "uniform" and "n" not in gv:
                e(gp, "uniform grid requires 'n'")
    if "variants" in obj:
        _strlist(e, "variants", obj["variants"], unique=True)
    return e.errors


def validate_pks1_header(obj: Any, grids: dict | None = None) -> list[str]:
    """Validate the JSON header of a pks1 (or sedcurve) record."""
    e = _E()
    if not _obj(e, "", obj):
        return e.errors
    json_errors(obj, "", e)
    _keys(e, "", obj, ("id", "n", "wave", "channels"),
          ("unit_w", "segments", "mask_runs", "mask_bits", "lines", "meta"))
    _str(e, "id", obj.get("id"))
    n = obj.get("n")
    if not _num(e, "n", n, 0, integer=True):
        n = None
    if "wave" in obj:
        _wave(e, "wave", obj["wave"], grids)
    _opt_str(e, "", obj, "unit_w", nonempty=False)
    ch = obj.get("channels")
    if ch is not None and _list(e, "channels", ch, nonempty=True):
        _unique_ids(e, "channels", ch, key="name")
        for i, c in enumerate(ch):
            cp = _p("channels", i)
            if not _obj(e, cp, c):
                continue
            _keys(e, cp, c, ("name", "enc", "off"), ("unit", "lo", "hi", "step"))
            _str(e, _p(cp, "name"), c.get("name"))
            enc = c.get("enc")
            _str(e, _p(cp, "enc"), enc, choices=("f32", "u16q", "u16log"))
            if "off" in c and _num(e, _p(cp, "off"), c["off"], 0, integer=True) and c["off"] % 4:
                e(_p(cp, "off"), "offset not 4-byte aligned")
            if enc in ("u16q", "u16log"):
                for k in ("lo", "hi"):
                    if k not in c:
                        e(cp, f"enc {enc} requires '{k}'")
                    else:
                        _num(e, _p(cp, k), c[k])
                if _is_finite(c.get("lo")) and _is_finite(c.get("hi")) and c["lo"] > c["hi"]:
                    e(cp, "lo > hi")
    if "segments" in obj:
        _segments(e, "segments", obj["segments"], n)
    if "mask_runs" in obj and _list(e, "mask_runs", obj["mask_runs"]):
        for i, r in enumerate(obj["mask_runs"]):
            rp = _p("mask_runs", i)
            if not _is_list(r) or len(r) != 3 or not all(_is_int(x) for x in r):
                e(rp, "mask run must be [i0, i1, bit] integers")
                continue
            if r[0] < 0 or r[1] <= r[0] or (n is not None and r[1] > n):
                e(rp, f"bad mask run bounds {r[0]}, {r[1]}")
    if "mask_bits" in obj and _obj(e, "mask_bits", obj["mask_bits"]):
        for k, v in obj["mask_bits"].items():
            if not isinstance(k, str):
                continue   # already reported by json_errors
            if not k.isdigit():
                e(_p("mask_bits", k), "mask bit keys must be decimal integers")
            _str(e, _p("mask_bits", k), v)
    if "lines" in obj and _list(e, "lines", obj["lines"]):
        for i, ln in enumerate(obj["lines"]):
            lp = _p("lines", i)
            if _obj(e, lp, ln):
                _keys(e, lp, ln, ("name", "w"), strict=False)
                if "w" in ln:
                    _num(e, _p(lp, "w"), ln["w"], 0.0, lo_open=True)
    if "meta" in obj:
        _obj(e, "meta", obj["meta"])
    return e.errors


def validate_pki1_header(obj: Any, grids: dict | None = None) -> list[str]:
    """Validate the JSON header of a pki1 record."""
    e = _E()
    if not _obj(e, "", obj):
        return e.errors
    json_errors(obj, "", e)
    _keys(e, "", obj, ("id", "nx", "ny", "enc", "off", "stretch", "wave", "y"), ("segments", "meta"))
    _str(e, "id", obj.get("id"))
    nx = obj.get("nx")
    if not _num(e, "nx", nx, 1, integer=True):
        nx = None
    _num(e, "ny", obj.get("ny"), 1, integer=True)
    _str(e, "enc", obj.get("enc"), choices=("u8", "u16"))
    if "off" in obj and _num(e, "off", obj["off"], 0, integer=True) and obj["off"] % 4:
        e("off", "offset not 4-byte aligned")
    st = obj.get("stretch")
    if st is not None and _obj(e, "stretch", st):
        _keys(e, "stretch", st, ("kind", "lo", "hi"), ("a",))
        _str(e, "stretch.kind", st.get("kind"), choices=("asinh", "linear"))
        for k in ("lo", "hi", "a"):
            if k in st:
                _num(e, _p("stretch", k), st[k])
        if _is_finite(st.get("lo")) and _is_finite(st.get("hi")) and st["lo"] >= st["hi"]:
            e("stretch", "lo must be < hi")
    if "wave" in obj:
        _wave(e, "wave", obj["wave"], grids)
    y = obj.get("y")
    if y is not None and _obj(e, "y", y):
        _keys(e, "y", y, ("kind", "y0", "dy"), ("unit",))
        _str(e, "y.kind", y.get("kind"), choices=("uniform",))
        if "y0" in y:
            _num(e, "y.y0", y["y0"])
        if "dy" in y:
            _num(e, "y.dy", y["dy"], 0.0, lo_open=True)
    if "segments" in obj:
        _segments(e, "segments", obj["segments"], nx)
    return e.errors


# ---------------------------------------------------------------------------
# section 5.10 other documents
# ---------------------------------------------------------------------------

def validate_images(obj: Any) -> list[str]:
    e = _E()
    if not _obj(e, "", obj):
        return e.errors
    json_errors(obj, "", e)
    _schema_key(e, obj, "images", False)
    # section 5.10: above 4 MB the items move to pages listed under 'pages', so 'items' is
    # required only for an unpaginated index (a paginated one may keep an inline first page).
    paged = obj.get("pages") is not None
    _keys(e, "", obj, ("roles",) if paged else ("roles", "items"),
          ("schema", "facets", "presets", "catalog", "pages", "items"))
    roles = obj.get("roles")
    role_keys: set = set()
    if roles is not None and _list(e, "roles", roles):
        role_keys = _unique_ids(e, "roles", roles, key="key")
        for i, r in enumerate(roles):
            rp = _p("roles", i)
            if _obj(e, rp, r):
                _keys(e, rp, r, ("key", "label"))
                _str(e, _p(rp, "key"), r.get("key"))
                _str(e, _p(rp, "label"), r.get("label"), nonempty=False)
    facets = obj.get("facets", [])
    facet_vals: dict = {}
    if _list(e, "facets", facets):
        _unique_ids(e, "facets", facets, key="key")
        for i, f in enumerate(facets):
            fp = _p("facets", i)
            if not _obj(e, fp, f):
                continue
            _keys(e, fp, f, ("key", "label", "values"))
            _str(e, _p(fp, "key"), f.get("key"))
            if "values" in f and _list(e, _p(fp, "values"), f["values"]):
                if isinstance(f.get("key"), str):
                    facet_vals[f["key"]] = set(v for v in f["values"] if isinstance(v, str))
    items = obj.get("items")
    if items is not None and _list(e, "items", items):
        _unique_ids(e, "items", items)
        for i, it in enumerate(items):
            ip = _p("items", i)
            if not _obj(e, ip, it):
                continue
            _keys(e, ip, it, ("id", "roles"), ("row", "caption", "tags", "w", "h"))
            _str(e, _p(ip, "id"), it.get("id"))
            if "row" in it and it["row"] is not None:
                _num(e, _p(ip, "row"), it["row"], 0, integer=True)
            _opt_str(e, ip, it, "caption", nonempty=False)
            ro = it.get("roles")
            if ro is not None and _obj(e, _p(ip, "roles"), ro):
                if not ro:
                    e(_p(ip, "roles"), "item has no images")
                for rk, rv in ro.items():
                    rp = _p(_p(ip, "roles"), rk)
                    if role_keys and rk not in role_keys:
                        e(rp, f"role {rk!r} not declared in roles")
                    if _obj(e, rp, rv):
                        _keys(e, rp, rv, ("url",), ("w", "h", "thumb"))
                        _str(e, _p(rp, "url"), rv.get("url"))
                        for k in ("w", "h"):
                            if k in rv and rv[k] is not None:
                                _num(e, _p(rp, k), rv[k], 1, integer=True)
                        _opt_str(e, rp, rv, "thumb")
            if "tags" in it and _obj(e, _p(ip, "tags"), it["tags"]):
                for tk, tv in it["tags"].items():
                    _str(e, _p(_p(ip, "tags"), tk), tv, nonempty=False)
    if "presets" in obj and _list(e, "presets", obj["presets"]):
        _unique_ids(e, "presets", obj["presets"], key="key")
        for i, pr in enumerate(obj["presets"]):
            pp = _p("presets", i)
            if not _obj(e, pp, pr):
                continue
            _keys(e, pp, pr, ("key", "label", "facets"))
            if "facets" in pr and _obj(e, _p(pp, "facets"), pr["facets"]):
                for fk, fv in pr["facets"].items():
                    fp = _p(_p(pp, "facets"), fk)
                    if fk not in facet_vals:
                        e(fp, f"unknown facet {fk!r}")
                    elif _strlist(e, fp, fv):
                        for v in fv:
                            if v not in facet_vals[fk]:
                                e(fp, f"value {v!r} not among facet values")
    _opt_str(e, "", obj, "catalog")
    if paged and _list(e, "pages", obj["pages"], nonempty=True):
        for i, pgf in enumerate(obj["pages"]):
            _relpath(e, _p("pages", i), pgf, allow_url=False)
    return e.errors


def validate_hips(obj: Any) -> list[str]:
    e = _E()
    if not _obj(e, "", obj):
        return e.errors
    json_errors(obj, "", e)
    _schema_key(e, obj, "hips", False)
    _keys(e, "", obj, ("base", "overlays", "fits", "target"), ("schema", "rungs", "bookmarks"))
    defaults = 0
    for sect, req, opt in (("base", ("id", "name", "url"), ("maxOrder", "format", "default")),
                           ("overlays", ("id", "name", "url", "opacity"), ("group", "default", "maxOrder", "format")),
                           ("fits", ("id", "name", "url", "auth", "ra", "dec", "size_deg"), ())):
        lst = obj.get(sect)
        if lst is None or not _list(e, sect, lst):
            continue
        _unique_ids(e, sect, lst)
        for i, it in enumerate(lst):
            ip = _p(sect, i)
            if not _obj(e, ip, it):
                continue
            _keys(e, ip, it, req, opt)
            _str(e, _p(ip, "id"), it.get("id"))
            _str(e, _p(ip, "name"), it.get("name"))
            _str(e, _p(ip, "url"), it.get("url"))
            if "maxOrder" in it and it["maxOrder"] is not None:
                _num(e, _p(ip, "maxOrder"), it["maxOrder"], 0, 29, integer=True)
            if "default" in it:
                if _bool(e, _p(ip, "default"), it["default"]) and it["default"] and sect == "base":
                    defaults += 1
            if "opacity" in it:
                _num(e, _p(ip, "opacity"), it["opacity"], 0.0, 1.0)
            if sect == "fits":
                _str(e, _p(ip, "auth"), it.get("auth"), choices=AUTHS)
                _radec(e, ip, it)
                if "size_deg" in it:
                    _num(e, _p(ip, "size_deg"), it["size_deg"], 0.0, 180.0, lo_open=True)
    if defaults > 1:
        e("base", "more than one base layer marked default")
    if "rungs" in obj and obj["rungs"] is not None and _list(e, "rungs", obj["rungs"]):
        _unique_ids(e, "rungs", obj["rungs"])
        for i, r in enumerate(obj["rungs"]):
            rp = _p("rungs", i)
            if not _obj(e, rp, r):
                continue
            _keys(e, rp, r, ("id", "label", "url", "desc"), ("r_um", "g_um", "b_um"))
            for k in ("r_um", "g_um", "b_um"):
                if k in r and r[k] is not None:
                    _num(e, _p(rp, k), r[k], 0.0, lo_open=True)
    if "bookmarks" in obj and obj["bookmarks"] is not None and _list(e, "bookmarks", obj["bookmarks"]):
        for i, b in enumerate(obj["bookmarks"]):
            bp = _p("bookmarks", i)
            if _obj(e, bp, b):
                _keys(e, bp, b, ("name", "ra", "dec", "fov"))
                _radec(e, bp, b)
                if "fov" in b:
                    _num(e, _p(bp, "fov"), b["fov"], 0.0, 360.0, lo_open=True)
    tg = obj.get("target")
    if tg is not None and _obj(e, "target", tg):
        _keys(e, "target", tg, ("ra", "dec", "fov"))
        _radec(e, "target", tg)
        if "fov" in tg:
            _num(e, "target.fov", tg["fov"], 0.0, 360.0, lo_open=True)
    return e.errors


def validate_overlays(obj: Any) -> list[str]:
    e = _E()
    if not _obj(e, "", obj):
        return e.errors
    json_errors(obj, "", e)
    _schema_key(e, obj, "overlays", False)
    _keys(e, "", obj, ("layers",), ("schema",))
    layers = obj.get("layers")
    if layers is None or not _list(e, "layers", layers):
        return e.errors
    _unique_ids(e, "layers", layers)
    for i, L in enumerate(layers):
        lp = _p("layers", i)
        if not _obj(e, lp, L):
            continue
        _keys(e, lp, L, ("id", "name", "space", "kind", "style", "default", "items"), ("group", "note"))
        _str(e, _p(lp, "id"), L.get("id"))
        _str(e, _p(lp, "name"), L.get("name"))
        space = L.get("space")
        _str(e, _p(lp, "space"), space, choices=("sky", "dim"))
        _str(e, _p(lp, "kind"), L.get("kind"), choices=OVERLAY_KINDS)
        if "default" in L:
            _bool(e, _p(lp, "default"), L["default"])
        st = L.get("style")
        if st is not None and _obj(e, _p(lp, "style"), st):
            _keys(e, _p(lp, "style"), st, (), ("color", "width", "dash", "shape", "opacity", "fill"))
            if "opacity" in st:
                _num(e, _p(_p(lp, "style"), "opacity"), st["opacity"], 0.0, 1.0)
            if "width" in st:
                _num(e, _p(_p(lp, "style"), "width"), st["width"], 0.0)
        items = L.get("items")
        if items is None or not _list(e, _p(lp, "items"), items):
            continue
        for j, it in enumerate(items):
            ip = _p(_p(lp, "items"), j)
            if not _obj(e, ip, it):
                continue
            if space == "sky":
                if "poly" in it:
                    if _list(e, _p(ip, "poly"), it["poly"]):
                        if len(it["poly"]) < 2:
                            e(_p(ip, "poly"), "needs at least 2 vertices")
                        for k, v in enumerate(it["poly"]):
                            _sky_point(e, _p(_p(ip, "poly"), k), v)
                elif "ra" in it and "dec" in it:
                    _radec(e, ip, it)
                    if "r_arcsec" in it:
                        _num(e, _p(ip, "r_arcsec"), it["r_arcsec"], 0.0, lo_open=True)
                    _opt_str(e, ip, it, "label", nonempty=False)
                    _opt_str(e, ip, it, "shape")
                else:
                    e(ip, "sky item needs 'poly' or 'ra'/'dec'")
            elif space == "dim":
                if "dims" in it:
                    d = it["dims"]
                    if _obj(e, _p(ip, "dims"), d):
                        if not d:
                            e(_p(ip, "dims"), "no dims")
                        lens = set()
                        for dk, dv in d.items():
                            if _numlist(e, _p(_p(ip, "dims"), dk), dv, allow_null=True):
                                lens.add(len(dv))
                        if len(lens) > 1:
                            e(_p(ip, "dims"), "dim arrays have different lengths")
                elif "pts" in it:
                    _str(e, _p(ip, "x"), it.get("x"))
                    _str(e, _p(ip, "y"), it.get("y"))
                    if _list(e, _p(ip, "pts"), it["pts"]):
                        for k, v in enumerate(it["pts"]):
                            _numlist(e, _p(_p(ip, "pts"), k), v, length=2)
                else:
                    e(ip, "dim item needs 'dims' or 'x'/'y'/'pts'")
    return e.errors


def validate_narrative(obj: Any) -> list[str]:
    e = _E()
    if not _obj(e, "", obj):
        return e.errors
    json_errors(obj, "", e)
    _schema_key(e, obj, "narrative", False)
    _keys(e, "", obj, ("beats",), ("schema",))
    beats = obj.get("beats")
    if beats is None or not _list(e, "beats", beats):
        return e.errors
    _unique_ids(e, "beats", beats)
    for i, b in enumerate(beats):
        bp = _p("beats", i)
        if not _obj(e, bp, b):
            continue
        _keys(e, bp, b, ("id", "html", "view"), ("title",))
        _str(e, _p(bp, "id"), b.get("id"))
        _str(e, _p(bp, "html"), b.get("html"), nonempty=False)
        v = b.get("view")
        if v is None or not _obj(e, _p(bp, "view"), v):
            continue
        vp = _p(bp, "view")
        _keys(e, vp, v, ("pane", "layers_on", "layers_off"), ("sky", "highlight", "axes", "suppress_view"))
        _str(e, _p(vp, "pane"), v.get("pane"))
        for k in ("layers_on", "layers_off"):
            if k in v:
                _strlist(e, _p(vp, k), v[k])
        if "sky" in v and v["sky"] is not None and _obj(e, _p(vp, "sky"), v["sky"]):
            _keys(e, _p(vp, "sky"), v["sky"], ("ra", "dec", "fov"))
            _radec(e, _p(vp, "sky"), v["sky"])
            if "fov" in v["sky"]:
                _num(e, _p(_p(vp, "sky"), "fov"), v["sky"]["fov"], 0.0, 360.0, lo_open=True)
        if "highlight" in v and v["highlight"] is not None and _obj(e, _p(vp, "highlight"), v["highlight"]):
            _keys(e, _p(vp, "highlight"), v["highlight"], ("ds", "ids"))
            _str(e, _p(_p(vp, "highlight"), "ds"), v["highlight"].get("ds"))
            if "ids" in v["highlight"]:
                _strlist(e, _p(_p(vp, "highlight"), "ids"), v["highlight"]["ids"])
        if "axes" in v and v["axes"] is not None and _obj(e, _p(vp, "axes"), v["axes"]):
            _keys(e, _p(vp, "axes"), v["axes"], ("x", "y"))
        if "suppress_view" in v:
            _bool(e, _p(vp, "suppress_view"), v["suppress_view"])
    return e.errors


def validate_templates(obj: Any) -> list[str]:
    e = _E()
    if not _obj(e, "", obj):
        return e.errors
    json_errors(obj, "", e)
    _schema_key(e, obj, "templates", False)
    _keys(e, "", obj, ("meta", "grids", "templates", "laws"), ("schema",))
    if "meta" in obj:
        _obj(e, "meta", obj["meta"])
    grids = obj.get("grids")
    gnames: set = set()
    if grids is not None and _obj(e, "grids", grids):
        if not grids:
            e("grids", "no grids")
        gnames = set(grids)
        for g, gv in grids.items():
            gp = _p("grids", g)
            if _obj(e, gp, gv):
                _keys(e, gp, gv, ("file",), strict=False)
                _relpath(e, _p(gp, "file"), gv.get("file"))
    tpls = obj.get("templates")
    if tpls is not None and _list(e, "templates", tpls):
        _unique_ids(e, "templates", tpls, key="key")
        for i, t in enumerate(tpls):
            tp = _p("templates", i)
            if not _obj(e, tp, t):
                continue
            _keys(e, tp, t, ("key", "teff", "logg", "label", "files"), ("kind", "note"))
            _str(e, _p(tp, "key"), t.get("key"))
            _str(e, _p(tp, "label"), t.get("label"), nonempty=False)
            for k in ("teff", "logg"):
                if k in t and t[k] is not None:
                    _num(e, _p(tp, k), t[k])
            fl = t.get("files")
            if fl is not None and _obj(e, _p(tp, "files"), fl):
                for g, u in fl.items():
                    fp = _p(_p(tp, "files"), g)
                    if gnames and g not in gnames:
                        e(fp, f"unknown grid {g!r}")
                    _relpath(e, fp, u)
    laws = obj.get("laws")
    if laws is not None and _obj(e, "laws", laws):
        for lk, lv in laws.items():
            lp = _p("laws", lk)
            if _obj(e, lp, lv):
                for g, u in lv.items():
                    if gnames and g not in gnames:
                        e(_p(lp, g), f"unknown grid {g!r}")
                    _relpath(e, _p(lp, g), u)
    return e.errors


def validate_filters(obj: Any) -> list[str]:
    e = _E()
    if not _obj(e, "", obj):
        return e.errors
    json_errors(obj, "", e)
    _schema_key(e, obj, "filters", False)
    _keys(e, "", obj, ("filters",), ("schema",))
    fl = obj.get("filters")
    if fl is None or not _obj(e, "filters", fl):
        return e.errors
    for k, f in fl.items():
        fp = _p("filters", k)
        if not k:
            e(fp, "empty filter key")
        if not _obj(e, fp, f):
            continue
        _keys(e, fp, f, ("label", "instrument", "wl_um", "weff_um", "zp_jy", "system", "a_over_av"),
              ("trans",))
        _str(e, _p(fp, "label"), f.get("label"))
        _str(e, _p(fp, "instrument"), f.get("instrument"), nonempty=False)
        _str(e, _p(fp, "system"), f.get("system"))
        if "wl_um" in f:
            _num(e, _p(fp, "wl_um"), f["wl_um"], 0.0, lo_open=True)
        if "weff_um" in f and f["weff_um"] is not None:
            _num(e, _p(fp, "weff_um"), f["weff_um"], 0.0)
        if "zp_jy" in f:
            _num(e, _p(fp, "zp_jy"), f["zp_jy"], 0.0, lo_open=True)
        if "trans" in f and f["trans"] is not None:
            _str(e, _p(fp, "trans"), f["trans"])
        aa = f.get("a_over_av")
        if aa is not None and _obj(e, _p(fp, "a_over_av"), aa):
            for ak, av in aa.items():
                if av is not None:
                    _num(e, _p(_p(fp, "a_over_av"), ak), av, 0.0)
    return e.errors


def _rv_key(rv: float) -> str:
    return f"{rv:g}"


def validate_extinction(obj: Any) -> list[str]:
    e = _E()
    if not _obj(e, "", obj):
        return e.errors
    json_errors(obj, "", e)
    _schema_key(e, obj, "extinction", False)
    _keys(e, "", obj, ("law", "rv", "curve"), ("schema",))
    _str(e, "law", obj.get("law"))
    rvs = obj.get("rv")
    if rvs is not None and _numlist(e, "rv", rvs):
        if not rvs:
            e("rv", "empty array")
        for i, r in enumerate(rvs):
            if r <= 0:
                e(_p("rv", i), "R_V must be > 0")
    cv = obj.get("curve")
    if cv is None or not _obj(e, "curve", cv):
        return e.errors
    if _is_list(rvs):
        for r in rvs:
            if _is_finite(r) and _rv_key(r) not in cv and str(r) not in cv:
                e("curve", f"no curve for R_V={r}")
    for ck, c in cv.items():
        cp = _p("curve", ck)
        if not _obj(e, cp, c):
            continue
        _keys(e, cp, c, ("w", "k"))
        w, k = c.get("w"), c.get("k")
        okw = _numlist(e, _p(cp, "w"), w) if w is not None else False
        okk = _numlist(e, _p(cp, "k"), k) if k is not None else False
        if okw and okk:
            if len(w) != len(k):
                e(cp, f"w has {len(w)} values, k has {len(k)}")
            if len(w) < 2:
                e(_p(cp, "w"), "need at least 2 samples")
            if any(b <= a for a, b in zip(w, w[1:])):
                e(_p(cp, "w"), "wavelengths must be strictly increasing")
    return e.errors


def validate_isochrones(obj: Any) -> list[str]:
    e = _E()
    if not _obj(e, "", obj):
        return e.errors
    json_errors(obj, "", e)
    _schema_key(e, obj, "isochrones", False)
    _keys(e, "", obj, ("tracks",), ("schema",))
    tracks = obj.get("tracks")
    if tracks is None or not _list(e, "tracks", tracks):
        return e.errors
    for i, t in enumerate(tracks):
        tp = _p("tracks", i)
        if not _obj(e, tp, t):
            continue
        _keys(e, tp, t, ("label", "age_myr", "dist_pc", "feh", "mass", "mags"), ("note",))
        _str(e, _p(tp, "label"), t.get("label"))
        if "age_myr" in t:
            _num(e, _p(tp, "age_myr"), t["age_myr"], 0.0, lo_open=True)
        if "dist_pc" in t:
            _num(e, _p(tp, "dist_pc"), t["dist_pc"], 0.0, lo_open=True)
        if "feh" in t:
            _num(e, _p(tp, "feh"), t["feh"])
        m = t.get("mass")
        nm = None
        if m is not None and _numlist(e, _p(tp, "mass"), m):
            nm = len(m)
            if any(b < a for a, b in zip(m, m[1:])):
                e(_p(tp, "mass"), "mass must be non-decreasing")
        mags = t.get("mags")
        if mags is not None and _obj(e, _p(tp, "mags"), mags):
            if not mags:
                e(_p(tp, "mags"), "no filters")
            for fk, fv in mags.items():
                fp = _p(_p(tp, "mags"), fk)
                if _numlist(e, fp, fv, allow_null=True) and nm is not None and len(fv) != nm:
                    e(fp, f"{len(fv)} values, mass has {nm}")
    return e.errors


def validate_features(obj: Any) -> list[str]:
    e = _E()
    if not _obj(e, "", obj):
        return e.errors
    json_errors(obj, "", e)
    _schema_key(e, obj, "features", False)
    _keys(e, "", obj, ("families",), ("schema", "meta"))
    fams = obj.get("families")
    if fams is None or not _obj(e, "families", fams):
        return e.errors
    for fk, fl in fams.items():
        fp = _p("families", fk)
        if not _list(e, fp, fl):
            continue
        for i, f in enumerate(fl):
            ip = _p(fp, i)
            if not _obj(e, ip, f):
                continue
            _keys(e, ip, f, ("name", "w_um", "label", "kind"), strict=False)
            _str(e, _p(ip, "name"), f.get("name"))
            if "w_um" in f:
                _num(e, _p(ip, "w_um"), f["w_um"], 0.0, lo_open=True)
            _str(e, _p(ip, "label"), f.get("label"), nonempty=False)
            _str(e, _p(ip, "kind"), f.get("kind"))
    return e.errors


def validate_products(obj: Any) -> list[str]:
    e = _E()
    if not _obj(e, "", obj):
        return e.errors
    json_errors(obj, "", e)
    _schema_key(e, obj, "products", False)
    _keys(e, "", obj, ("items",), ("schema",))
    items = obj.get("items")
    if items is None or not _list(e, "items", items):
        return e.errors
    for i, it in enumerate(items):
        ip = _p("items", i)
        if not _obj(e, ip, it):
            continue
        _keys(e, ip, it, ("label", "url", "kind", "auth"), ("size", "group", "note"))
        _str(e, _p(ip, "label"), it.get("label"))
        _str(e, _p(ip, "url"), it.get("url"))
        _str(e, _p(ip, "kind"), it.get("kind"), choices=PRODUCT_KINDS)
        _str(e, _p(ip, "auth"), it.get("auth"), choices=AUTHS)
        if "size" in it and it["size"] is not None:
            _num(e, _p(ip, "size"), it["size"], 0, integer=True)
    return e.errors


# ---------------------------------------------------------------------------
# section 11.3 ownership manifest
# ---------------------------------------------------------------------------

def validate_deploy(obj: Any) -> list[str]:
    e = _E()
    if not _obj(e, "", obj):
        return e.errors
    json_errors(obj, "", e)
    _schema_key(e, obj, "deploy", True)
    _keys(e, "", obj, ("family", "portal", "target", "builds", "files"), ("schema", "replaced"))
    if _str(e, "family", obj.get("family")) and not _FAMILY.match(obj["family"]):
        e("family", "must match [a-z0-9_]+")
    _str(e, "portal", obj.get("portal"))
    if _str(e, "target", obj.get("target")) and not obj["target"].startswith("/"):
        e("target", "must be an absolute path")
    builds = obj.get("builds")
    build_ids: set = set()
    if builds is not None and _list(e, "builds", builds):
        for i, b in enumerate(builds):
            bp = _p("builds", i)
            if not _obj(e, bp, b):
                continue
            _keys(e, bp, b, ("build_id", "deployed", "portalkit"),
                  ("adapter_sha", "legacy_modules", "replace", "note"))
            if _str(e, _p(bp, "build_id"), b.get("build_id")):
                build_ids.add(b["build_id"])
            _str(e, _p(bp, "deployed"), b.get("deployed"))
            _str(e, _p(bp, "portalkit"), b.get("portalkit"))
            if "legacy_modules" in b and b["legacy_modules"] is not None:
                _list(e, _p(bp, "legacy_modules"), b["legacy_modules"])
    files = obj.get("files")
    if files is not None and _obj(e, "files", files):
        for fk, fv in files.items():
            fp = _p("files", fk)
            _relpath(e, fp, fk, allow_url=False)
            if not _obj(e, fp, fv):
                continue
            _keys(e, fp, fv, ("sha256", "size", "build_id"))
            if _str(e, _p(fp, "sha256"), fv.get("sha256")) and not _HEX64.match(fv["sha256"]):
                e(_p(fp, "sha256"), "not a lowercase hex sha256")
            if "size" in fv:
                _num(e, _p(fp, "size"), fv["size"], 0, integer=True)
            if _str(e, _p(fp, "build_id"), fv.get("build_id")) and builds is not None \
                    and fv["build_id"] not in build_ids:
                e(_p(fp, "build_id"), f"build {fv['build_id']!r} not listed in builds")
    if "replaced" in obj and _list(e, "replaced", obj["replaced"]):
        for i, r in enumerate(obj["replaced"]):
            rp = _p("replaced", i)
            if not _obj(e, rp, r):
                continue
            _keys(e, rp, r, ("legacy", "backup", "md5", "mode"), ("size", "build_id"))
            _relpath(e, _p(rp, "legacy"), r.get("legacy"), allow_url=False, allow_parent=True)
            _relpath(e, _p(rp, "backup"), r.get("backup"), allow_url=False, allow_parent=True)
            if _str(e, _p(rp, "md5"), r.get("md5")) and not _HEX32.match(r["md5"]):
                e(_p(rp, "md5"), "not a lowercase hex md5")
            _str(e, _p(rp, "mode"), r.get("mode"), choices=("stub", "inplace"))
    return e.errors


# ---------------------------------------------------------------------------
# dispatch and assert_valid_*
# ---------------------------------------------------------------------------

VALIDATORS: dict[str, Callable[[Any], list[str]]] = {
    "site": validate_site,
    "portal": validate_portal,
    "page": validate_page,
    "catalog": validate_catalog,
    "records": validate_records,
    "pks1_header": validate_pks1_header,
    "pki1_header": validate_pki1_header,
    "images": validate_images,
    "hips": validate_hips,
    "overlays": validate_overlays,
    "narrative": validate_narrative,
    "templates": validate_templates,
    "filters": validate_filters,
    "extinction": validate_extinction,
    "isochrones": validate_isochrones,
    "features": validate_features,
    "products": validate_products,
    "deploy": validate_deploy,
}

_BY_SCHEMA_ID = {sid: doc for doc, sid in SCHEMA_IDS.items()}


def _doc_name(obj: Any, doc: str | None) -> str:
    if doc is not None:
        if doc in VALIDATORS:
            return doc
        if doc in _BY_SCHEMA_ID:
            return _BY_SCHEMA_ID[doc]
        raise KeyError(f"unknown document type {doc!r}")
    if isinstance(obj, dict) and obj.get("schema") in _BY_SCHEMA_ID:
        return _BY_SCHEMA_ID[obj["schema"]]
    raise KeyError("cannot infer document type: pass doc= or include a known 'schema' key")


def validate(obj: Any, doc: str | None = None) -> list[str]:
    """Validate ``obj`` as document ``doc`` (a validator name or schema id), or infer it
    from ``obj['schema']``."""
    return VALIDATORS[_doc_name(obj, doc)](obj)


def assert_valid(obj: Any, doc: str | None = None) -> None:
    name = _doc_name(obj, doc)
    errs = VALIDATORS[name](obj)
    if errs:
        raise SchemaError(name, errs)


def _make_assert(name: str) -> Callable[..., None]:
    fn = VALIDATORS[name]

    def assert_valid_doc(obj: Any, *args: Any, **kw: Any) -> None:
        errs = fn(obj, *args, **kw)
        if errs:
            raise SchemaError(name, errs)

    assert_valid_doc.__name__ = f"assert_valid_{name}"
    assert_valid_doc.__qualname__ = assert_valid_doc.__name__
    assert_valid_doc.__doc__ = f"Raise SchemaError if ``validate_{name}(obj)`` reports errors."
    return assert_valid_doc


assert_valid_site = _make_assert("site")
assert_valid_portal = _make_assert("portal")
assert_valid_page = _make_assert("page")
assert_valid_catalog = _make_assert("catalog")
assert_valid_records = _make_assert("records")
assert_valid_pks1_header = _make_assert("pks1_header")
assert_valid_pki1_header = _make_assert("pki1_header")
assert_valid_images = _make_assert("images")
assert_valid_hips = _make_assert("hips")
assert_valid_overlays = _make_assert("overlays")
assert_valid_narrative = _make_assert("narrative")
assert_valid_templates = _make_assert("templates")
assert_valid_filters = _make_assert("filters")
assert_valid_extinction = _make_assert("extinction")
assert_valid_isochrones = _make_assert("isochrones")
assert_valid_features = _make_assert("features")
assert_valid_products = _make_assert("products")
assert_valid_deploy = _make_assert("deploy")

for _n in VALIDATORS:
    __all__ += [f"validate_{_n}", f"assert_valid_{_n}"]
del _n
