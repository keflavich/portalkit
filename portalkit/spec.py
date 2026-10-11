"""Adapter-facing dataclasses and helper constructors (ARCHITECTURE.md section 9.1, FROZEN).

Adapters return :class:`DatasetSpec` objects from ``datasets()`` and :class:`PageSpec`
objects from ``pages()``. The builder turns them into bundles (``portalkit.bundle``),
capabilities (``portalkit.caps``), page configs (``portalkit.config``) and HTML shells
(``portalkit.render.shell``).

Helpers:

* :func:`catalog_from_rows` builds a :class:`CatalogSpec` from a list of dicts;
* :func:`join` left-joins the columns of one catalog onto another;
* :meth:`ImageSetSpec.from_rows` builds an image set from a list of dicts;
* :func:`standard_pages` derives a conventional page list from a dataset dict;
* :func:`safe_key` turns a source column name into a stored key (``[A-Za-z_][A-Za-z0-9_.]*``).
"""

from __future__ import annotations

import math
import numbers
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal, Protocol, Union

import numpy as np
from astropy.table import MaskedColumn, Table

if TYPE_CHECKING:  # pragma: no cover
    from .context import BuildContext

__all__ = [
    "Role", "ROLES", "NUMERIC_ROLES", "TierUnavailable",
    "ColumnSpec", "VirtualDim", "Group", "Preset", "DensitySpec", "ManifoldSpec", "ForeignRef",
    "CatalogSpec", "Spectrum", "Image2D", "RecordSetSpec", "ImageItem", "ImageSetSpec",
    "TemplateSpec", "FilterSetSpec", "ExtinctionSpec", "Track", "IsochroneSpec", "HipsSpec",
    "OverlaySpec", "NarrativeSpec", "ReadmeSpec", "DatasetSpec", "LegacyFile", "LegacyRef",
    "PageSpec",
    "Adapter", "ARCHETYPES", "safe_key", "catalog_from_rows", "join", "standard_pages",
]

Role = Literal["id", "ra", "dec", "mag", "flux", "error", "color", "value", "category", "flag",
               "text", "url", "image", "limit"]

ROLES: tuple[str, ...] = ("id", "ra", "dec", "mag", "flux", "error", "color", "value", "category",
                          "flag", "text", "url", "image", "limit")

#: roles whose columns are numeric axes (scatter/manifold dims, flags num2/num3 in section 8.1)
NUMERIC_ROLES: tuple[str, ...] = ("mag", "flux", "color", "value")

ARCHETYPES: tuple[str, ...] = ("landing", "explorer", "manifold", "gallery", "spectra",
                               "wavelength", "narrative")


class TierUnavailable(RuntimeError):
    """Raised by an adapter for a tier it does not implement (section 9.2)."""


# ---------------------------------------------------------------------------
# section 9.1 dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ColumnSpec:
    key: str
    role: Role = "value"
    label: str | None = None
    unit: str = ""
    filter: str | None = None          # filters registry key, e.g. "JWST/NIRCam.F410M"
    err: str | None = None
    limit: str | None = None           # sibling column with legacy codes 'v'/'u'/'l'/''
    validity: str | None = None        # bits column key
    enc: str = "auto"
    step: float | None = None
    clip: Literal["none", "saturate", "drop"] = "none"
    lo: float | None = None
    hi: float | None = None
    group: str | None = None
    note: str = ""
    log: bool = False
    invert: bool | None = None
    fmt: str | None = None             # python format spec for display, e.g. ".3f"
    levels: list[str] | None = None
    colors: dict[str, str] | None = None
    default_off: list[str] = field(default_factory=list)
    exact: bool = True                 # include in row chunks
    exact_gpu: bool | None = None      # None -> CatalogSpec.exact_gpu; False lets 'value' use f32


@dataclass
class VirtualDim:
    key: str
    label: str | None = None
    unit: str = ""
    group: str | None = None


@dataclass
class Group:
    key: str
    label: str
    dims: list[str]


@dataclass
class Preset:
    key: str
    label: str
    kind: Literal["axes", "frame", "selection", "filter"]
    data: dict


@dataclass
class DensitySpec:
    key: str
    x: str
    y: str
    nx: int = 256
    ny: int = 256
    xr: tuple | None = None
    yr: tuple | None = None
    stat: str | None = None


@dataclass
class ManifoldSpec:
    dims: list[str]
    frames: dict[str, list[float]] = field(default_factory=dict)
    default_frame: str = "pca"


@dataclass
class ForeignRef:
    kind: Literal["gc-manifest-1"]
    url_base: str
    manifest_path: Path


@dataclass
class CatalogSpec:
    key: str
    table: Table | None                # None only with foreign
    id: str
    columns: list[ColumnSpec]
    ra: str | None = None
    dec: str | None = None
    aliases: dict[str, list[str]] | str | None = None   # id -> aliases, or column name holding '|'-joined aliases
    virtual: list[VirtualDim] = field(default_factory=list)
    groups: list[Group] = field(default_factory=list)
    presets: list[Preset] = field(default_factory=list)
    density: list[DensitySpec] = field(default_factory=list)
    manifold: ManifoldSpec | None = None
    exact_rows: bool | None = None     # None -> n <= EXACT_ROWS_MAX_N
    exact_gpu: bool = True             # False: 'value' columns spanning > 1e4 are stored f32 (section 5.3)
    context: bool = False
    order: Literal["auto", "input", "lod"] = "auto"
    title: str = ""
    caveats: list[str] = field(default_factory=list)
    catalogs_provenance: list[dict] = field(default_factory=list)
    foreign: ForeignRef | None = None
    # landing-card nouns (singular, plural) for portal.json counts; None -> the kind default
    noun: tuple[str, str] | None = None

    def column(self, key: str) -> ColumnSpec | None:
        """The :class:`ColumnSpec` with ``key``, or None."""
        for c in self.columns:
            if c.key == key:
                return c
        return None

    @property
    def n(self) -> int | None:
        """Row count (None for a foreign catalog without a table)."""
        return None if self.table is None else len(self.table)


@dataclass
class Spectrum:
    wave: np.ndarray                   # um, ascending within each segment
    channels: dict[str, np.ndarray]    # "flux" required; "err", variants optional
    units: dict[str, str] = field(default_factory=lambda: {"flux": "Jy"})
    segments: list[tuple[int, int, str]] = field(default_factory=list)
    mask_runs: list[tuple[int, int, int]] = field(default_factory=list)
    mask_bits: dict[int, str] = field(default_factory=dict)
    lines: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


@dataclass
class Image2D:
    data: np.ndarray
    wave: np.ndarray
    y: np.ndarray
    stretch: str = "asinh"
    segments: list = field(default_factory=list)


@dataclass
class RecordSetSpec:
    key: str
    kind: Literal["pks1", "pki1", "json", "sedcurve"]
    catalog: str
    ids: list[str]
    loader: Callable[[str], "Spectrum | Image2D | dict | None"]
    enc: dict[str, str] = field(default_factory=dict)  # channel -> "f32"|"u16q"|"u16log"
    grids: dict[str, np.ndarray] = field(default_factory=dict)
    png: Callable[[str], tuple[str, dict] | None] | None = None   # pki1 PNG mode: id -> (url, extent)
    layout: Literal["shard", "file"] = "shard"
    legacy_json: tuple[str, str] | None = None         # (dir_url, pattern) Tier A/B only
    # landing-card nouns (singular, plural) for portal.json counts; None -> the kind default
    noun: tuple[str, str] | None = None


@dataclass
class ImageItem:
    id: str
    roles: dict[str, str]
    caption: str = ""
    tags: dict[str, str] = field(default_factory=dict)
    w: int | None = None
    h: int | None = None


@dataclass
class ImageSetSpec:
    key: str
    items: list[ImageItem]
    roles: list[tuple[str, str]]
    facets: list[tuple[str, str]] = field(default_factory=list)
    presets: list[Preset] = field(default_factory=list)
    catalog: str | None = None
    join: str | None = None   # join item.id to catalog id or alias
    # landing-card nouns (singular, plural) for portal.json counts; None -> the kind default
    noun: tuple[str, str] | None = None

    @classmethod
    def from_rows(cls, rows: list[dict], roles: dict[str, Any], key: str = "img", *,
                  id: str = "id", caption: str | None = "caption", tags: list[str] | None = None,
                  catalog: str | None = None, join: str | None = None,
                  presets: list[Preset] | None = None) -> "ImageSetSpec":
        """Build an image set from row dicts.

        ``roles`` maps a role key to the row field holding that role's URL, or to a
        ``(field, label)`` tuple. Without a label the role key is title-cased
        (``"sed_fit"`` -> ``"Sed fit"``). ``id`` names the item-id field; ``caption`` the
        caption field (absent fields give ``""``). ``tags`` lists the facet fields; when
        None, every remaining scalar field (not id, caption, a role field, ``w`` or ``h``)
        becomes a tag. Items without any role URL are skipped. Duplicate ids raise
        ``ValueError``.
        """
        role_fields: list[tuple[str, str, str]] = []
        for rk, spec in roles.items():
            if isinstance(spec, (tuple, list)):
                fld, label = spec[0], spec[1]
            else:
                fld, label = spec, _title(rk)
            role_fields.append((rk, str(fld), str(label)))
        used = {id, "w", "h"} | {f for _, f, _ in role_fields}
        if caption:
            used.add(caption)
        if tags is None:
            tag_fields: list[str] = []
            for r in rows:
                for k, v in r.items():
                    if k in used or k in tag_fields:
                        continue
                    if v is None or isinstance(v, (str, bool, numbers.Number)):
                        tag_fields.append(k)
        else:
            tag_fields = list(tags)
        # a field that holds non-scalar values in any row is not a facet
        tag_fields = [t for t in tag_fields
                      if all(r.get(t) is None or isinstance(r.get(t), (str, bool, numbers.Number))
                             for r in rows)]
        items: list[ImageItem] = []
        seen: set[str] = set()
        for i, r in enumerate(rows):
            if id not in r or r[id] is None or str(r[id]) == "":
                raise ValueError(f"row {i} has no {id!r} value")
            iid = str(r[id])
            if iid in seen:
                raise ValueError(f"duplicate image id {iid!r}")
            item_roles = {rk: str(r[f]) for rk, f, _ in role_fields
                          if r.get(f) not in (None, "")}
            if not item_roles:
                continue
            seen.add(iid)
            item_tags = {t: _tag_str(r[t]) for t in tag_fields if r.get(t) not in (None, "")}
            w = r.get("w")
            h = r.get("h")
            items.append(ImageItem(id=iid, roles=item_roles,
                                   caption=str(r.get(caption) or "") if caption else "",
                                   tags=item_tags,
                                   w=int(w) if w is not None else None,
                                   h=int(h) if h is not None else None))
        used_roles = {rk for it in items for rk in it.roles}
        role_list = [(rk, label) for rk, _, label in role_fields if rk in used_roles]
        facet_list = [(t, _title(t)) for t in tag_fields if any(t in it.tags for it in items)]
        return cls(key=key, items=items, roles=role_list, facets=facet_list,
                   presets=list(presets or []), catalog=catalog, join=join)


@dataclass
class TemplateSpec:
    source: Path | None = None
    extend_to_um: float | None = None   # default jwst_template_spectra.json


@dataclass
class FilterSetSpec:
    keys: list[str]
    extra: dict[str, dict] = field(default_factory=dict)


@dataclass
class ExtinctionSpec:
    law: str = "G23"
    rv: tuple[float, ...] = (3.1, 5.5)


@dataclass
class Track:
    label: str
    age_myr: float
    dist_pc: float
    feh: float
    mass: np.ndarray
    mags: dict[str, np.ndarray]


@dataclass
class IsochroneSpec:
    tracks: list[Track]


@dataclass
class HipsSpec:
    base: list[dict]
    overlays: list[dict] = field(default_factory=list)
    fits: list[dict] = field(default_factory=list)
    rungs: list[dict] | None = None
    bookmarks: list[dict] | None = None
    target: dict | None = None


@dataclass
class OverlaySpec:
    layers: list[dict]


@dataclass
class NarrativeSpec:
    beats: list[dict]                   # html via markdown_lite at build


@dataclass
class ReadmeSpec:
    markdown: str | Path
    blocks: list[dict] = field(default_factory=list)


DatasetSpec = Union[CatalogSpec, RecordSetSpec, ImageSetSpec, TemplateSpec, FilterSetSpec,
                    ExtinctionSpec, IsochroneSpec, HipsSpec, OverlaySpec, NarrativeSpec, ReadmeSpec]


@dataclass
class LegacyFile:
    """One ``legacy`` item of a ``[[portal.page]]`` entry (a string or a table in the TOML).

    ``preset`` is the hash fragment the ``--replace`` redirect stub of this file appends
    (``None``: the page preset). ``replace=False`` keeps the file out of ``--replace``
    stubbing (for example a file reached through a served symlink)."""
    file: str
    preset: str | None = None
    replace: bool = True

    @classmethod
    def coerce(cls, item: "str | dict | LegacyFile") -> "LegacyFile":
        """A :class:`LegacyFile` from a TOML item (string or ``{file, preset?, replace?}``)."""
        if isinstance(item, LegacyFile):
            return item
        if isinstance(item, str):
            return cls(file=item)
        pre = item.get("preset")
        return cls(file=str(item["file"]), preset=None if pre is None else str(pre),
                   replace=bool(item.get("replace", True)))


@dataclass
class LegacyRef:
    files: list[str]
    grammar: list[str] = field(default_factory=list)
    preset: str = ""
    presets: dict[str, str] = field(default_factory=dict)   # file -> per-file stub preset


@dataclass
class PageSpec:
    id: str
    archetype: Literal["landing", "explorer", "manifold", "gallery", "spectra", "wavelength", "narrative"]
    title: str
    datasets: list[str]
    layout: dict | None = None
    state: dict = field(default_factory=dict)
    caps_disable: dict[str, str] = field(default_factory=dict)
    legacy: LegacyRef | None = None
    links: dict[str, str] = field(default_factory=dict)
    manifold: bool = False
    subtitle: str = ""
    portal: str | None = None           # [[portal]] id when the family has several


class Adapter(Protocol):
    name: str
    tiers: tuple[str, ...]              # tiers implemented, e.g. ("A", "B")

    def datasets(self, ctx: "BuildContext") -> dict[str, DatasetSpec]: ...

    def pages(self, ctx: "BuildContext", ds: dict[str, DatasetSpec]) -> list[PageSpec]: ...


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_KEY_OK = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*\Z")
_KEY_BAD = re.compile(r"[^A-Za-z0-9_.]+")


def _title(k: str) -> str:
    s = k.replace("_", " ").strip()
    return s[:1].upper() + s[1:] if s else k


def _tag_str(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def safe_key(name: str, taken: set[str] | None = None) -> str:
    """Stored column key for a source column name (IMPLEMENTATION_NOTES item 13).

    A valid key is returned unchanged. Otherwise runs of characters outside
    ``[A-Za-z0-9_.]`` become ``_``; a name containing ``-`` (a colour such as
    ``f182m-f410m``) gets the prefix ``c_`` (``c_f182m_f410m``); a key that would start with a
    digit or ``.`` gets the prefix ``_``. With ``taken``, a numeric suffix keeps the key unique
    and the result is added to ``taken``.
    """
    if not isinstance(name, str) or name == "":
        raise ValueError(f"column name must be a non-empty string, got {name!r}")
    if _KEY_OK.match(name):
        key = name
    else:
        key = _KEY_BAD.sub("_", name).strip("_") or "col"
        if "-" in name:
            key = "c_" + key
        if not _KEY_OK.match(key):
            key = "_" + key
    if taken is not None:
        base, k = key, 2
        while key in taken:
            key = f"{base}_{k}"
            k += 1
        taken.add(key)
    return key


def _is_missing(v: Any) -> bool:
    if v is None or v is np.ma.masked:
        return True
    if isinstance(v, float) and math.isnan(v):
        return True
    if isinstance(v, np.floating) and np.isnan(v):
        return True
    return False


#: A string field with at most this many distinct values (and at least one repeat) is
#: inferred as ``category`` by :func:`catalog_from_rows`.
CATEGORY_MAX_LEVELS = 32


def _column_from_values(name: str, values: list) -> tuple[Any, str]:
    """Build an astropy column and an inferred role from python values."""
    present = [v for v in values if not _is_missing(v)]
    mask = np.array([_is_missing(v) for v in values], dtype=bool)
    n = len(values)
    if present and all(isinstance(v, (bool, np.bool_)) for v in present):
        data = np.array([bool(v) if not m else False for v, m in zip(values, mask)], dtype=bool)
        return MaskedColumn(data, name=name, mask=mask), "flag"
    if present and all(isinstance(v, numbers.Number) and not isinstance(v, (bool, np.bool_))
                       for v in present):
        if all(isinstance(v, numbers.Integral) for v in present) and not mask.any():
            arr = np.array(values, dtype=np.int64) if n else np.zeros(0, dtype=np.int64)
            return MaskedColumn(arr, name=name, mask=mask), "value"
        data = np.array([float(v) if not m else np.nan for v, m in zip(values, mask)],
                        dtype=np.float64)
        return MaskedColumn(data, name=name, mask=mask), "value"
    strs = ["" if m else str(v) for v, m in zip(values, mask)]
    data = np.array(strs, dtype=str) if n else np.zeros(0, dtype="U1")
    nonempty = [s for s in strs if s]
    role = "text"
    if nonempty and all(s.startswith(("http://", "https://")) for s in nonempty):
        role = "url"
    elif nonempty and len(set(nonempty)) <= CATEGORY_MAX_LEVELS and len(set(nonempty)) < len(nonempty):
        role = "category"
    return MaskedColumn(data, name=name, mask=mask), role


def catalog_from_rows(rows: list[dict], id: str, ra: str | None = None, dec: str | None = None,
                      roles: dict[str, Role] | None = None, *, key: str = "src", title: str = "",
                      columns: dict[str, dict] | None = None, aliases: dict | str | None = None,
                      context: bool = False) -> CatalogSpec:
    """Build a :class:`CatalogSpec` from a list of row dicts.

    Every field that appears in any row becomes a table column (missing values are masked).
    ``id``, ``ra`` and ``dec`` name the identifier and position fields; they are kept in the
    table and are not listed in ``columns`` (the bundle writer handles them). ``roles`` sets
    the role of other fields; unlisted fields are inferred (bool -> ``flag``, number ->
    ``value``, all-http strings -> ``url``, strings with at most :data:`CATEGORY_MAX_LEVELS`
    distinct values and at least one repeat -> ``category``, other strings -> ``text``). ``columns`` gives
    extra :class:`ColumnSpec` keyword arguments per field (``filter``, ``err``, ``unit``...).
    Field names that are not valid stored keys are renamed with :func:`safe_key`; the
    original name becomes the column label. ``err``/``limit``/``validity`` references and the
    ``id``/``ra``/``dec`` arguments may use either the original or the renamed key.
    """
    roles = dict(roles or {})
    columns = dict(columns or {})
    names: list[str] = []
    for r in rows:
        if not isinstance(r, dict):
            raise TypeError(f"rows must be dicts, got {type(r).__name__}")
        for k in r:
            if k not in names:
                names.append(k)
    for req in (id, ra, dec):
        if req is not None and rows and req not in names:
            raise KeyError(f"field {req!r} not present in rows")
    if not rows:
        names = [x for x in (id, ra, dec) if x is not None] + [k for k in roles if k not in (id, ra, dec)]
    # valid field names keep their key; invalid ones are renamed around them
    taken: set[str] = {nm for nm in names if _KEY_OK.match(nm)}
    rename = {nm: (nm if nm in taken else safe_key(nm, taken)) for nm in names}
    name_set = set(names)

    def _lookup(d: dict, nm: str, default: Any) -> Any:
        """Override for field ``nm``: by original name, else by renamed key when no other
        field has that original name."""
        if nm in d:
            return d[nm]
        if rename[nm] != nm and rename[nm] not in name_set and rename[nm] in d:
            return d[rename[nm]]
        return default
    cols_out = []
    specs: list[ColumnSpec] = []
    for nm in names:
        vals = [r.get(nm) for r in rows]
        col, inferred = _column_from_values(rename[nm], vals)
        if nm == id:
            # ids are strings in the bundle
            col = MaskedColumn(np.array(["" if _is_missing(v) else str(v) for v in vals], dtype=str)
                               if rows else np.zeros(0, dtype="U1"), name=rename[nm])
            missing = [i for i, v in enumerate(vals) if _is_missing(v) or str(v) == ""]
            if missing:
                raise ValueError(f"{len(missing)} row(s) have no id (first: row {missing[0]})")
            if len(set(col.tolist())) != len(col):
                raise ValueError(f"id field {id!r} has duplicate values")
        if nm in (ra, dec):
            if inferred != "value" and rows:
                raise ValueError(f"position field {nm!r} is not numeric")
            pmask = np.array([_is_missing(v) for v in vals], dtype=bool)
            pdata = np.array([np.nan if m else float(v) for v, m in zip(vals, pmask)], dtype=np.float64)
            col = MaskedColumn(pdata, name=rename[nm], mask=pmask)
        cols_out.append(col)
        if nm in (id, ra, dec):
            continue
        role = _lookup(roles, nm, inferred)
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r} for field {nm!r}")
        kw = dict(_lookup(columns, nm, {}))
        for ref in ("err", "limit", "validity"):
            if kw.get(ref) in rename:
                kw[ref] = rename[kw[ref]]
        if rename[nm] != nm:
            kw.setdefault("label", nm)
        if role == "category" and "levels" not in kw:
            seen: list[str] = []
            for v in vals:
                if not _is_missing(v) and str(v) not in seen:
                    seen.append(str(v))
            kw["levels"] = seen
        specs.append(ColumnSpec(key=rename[nm], role=role, **kw))
    table = Table(cols_out) if cols_out else Table()
    return CatalogSpec(key=key, table=table, id=rename.get(id, id),
                       columns=specs, ra=rename.get(ra, ra) if ra else None,
                       dec=rename.get(dec, dec) if dec else None,
                       aliases=aliases, title=title, context=context)


def _match_keys(col: Any) -> list[str | None]:
    out: list[str | None] = []
    mask = np.asarray(getattr(col, "mask", np.zeros(len(col), dtype=bool)))
    if mask.shape != (len(col),):
        mask = np.zeros(len(col), dtype=bool)
    for v, m in zip(np.asarray(col), mask):
        if m:
            out.append(None)
            continue
        if isinstance(v, (bytes, np.bytes_)):
            v = v.decode("utf-8")
        if isinstance(v, (float, np.floating)) and float(v).is_integer():
            v = int(v)
        out.append(str(v))
    return out


def join(a: CatalogSpec, b: CatalogSpec, on: str) -> CatalogSpec:
    """Left-join the columns of ``b`` onto ``a`` by field ``on`` (kept in ``a``'s row order).

    ``on`` must be a table column of both catalogs (typically ``a.id``; ``b`` may use its own
    id column if it is named ``on``). Values are compared as strings (``12`` matches
    ``"12"``). Duplicate keys in ``b`` raise ``ValueError``. Rows of ``a`` without a match get
    masked values. Columns of ``b`` named like ``on``, ``b.id``, ``b.ra`` or ``b.dec`` are not
    copied; any other column whose key clashes with ``a`` is renamed ``<key>_<b.key>``.
    Virtual dims, groups, presets, density specs, caveats and provenance are concatenated;
    ``a`` wins for everything else.
    """
    if a.table is None or b.table is None:
        raise ValueError("join needs tables on both catalogs (foreign catalogs cannot be joined)")
    if on not in a.table.colnames:
        raise KeyError(f"{on!r} is not a column of catalog {a.key!r}")
    b_on = on if on in b.table.colnames else (b.id if b.id in b.table.colnames else None)
    if b_on is None:
        raise KeyError(f"{on!r} is not a column of catalog {b.key!r}")
    bkeys = _match_keys(b.table[b_on])
    index: dict[str, int] = {}
    for i, k in enumerate(bkeys):
        if k is None:
            continue
        if k in index:
            raise ValueError(f"duplicate join key {k!r} in catalog {b.key!r}")
        index[k] = i
    akeys = _match_keys(a.table[on])
    rows = np.array([index.get(k, -1) if k is not None else -1 for k in akeys], dtype=np.int64)
    hit = rows >= 0
    take = np.where(hit, rows, 0)
    out = a.table.copy(copy_data=True)
    skip = {b_on, b.id, b.ra, b.dec}
    taken = set(out.colnames)
    rename: dict[str, str] = {}
    for name in b.table.colnames:
        if name in skip:
            continue
        new = name
        if new in taken:
            new = safe_key(f"{name}_{b.key}", taken)
        else:
            taken.add(new)
        rename[name] = new
        src = b.table[name]
        data = np.asarray(src)
        mask = np.asarray(getattr(src, "mask", np.zeros(len(src), dtype=bool)))
        if mask.shape != (len(src),):
            mask = np.zeros(len(src), dtype=bool)
        if len(src):
            vals = data[take]
            m = ~hit | mask[take]
        else:
            vals = np.zeros(len(a.table), dtype=data.dtype)
            m = np.ones(len(a.table), dtype=bool)
        out[new] = MaskedColumn(vals, name=new, mask=m)
    cols = list(a.columns)
    for c in b.columns:
        if c.key not in rename:
            continue
        nc = ColumnSpec(**{**c.__dict__})
        nc.key = rename[c.key]
        if nc.key != c.key and nc.label is None:
            nc.label = c.key
        for ref in ("err", "limit", "validity"):
            r = getattr(nc, ref)
            if r is not None and r in rename:
                setattr(nc, ref, rename[r])
        cols.append(nc)
    return CatalogSpec(
        key=a.key, table=out, id=a.id, columns=cols, ra=a.ra, dec=a.dec, aliases=a.aliases,
        virtual=list(a.virtual) + [v for v in b.virtual if v.key not in {x.key for x in a.virtual}],
        groups=list(a.groups) + [g for g in b.groups if g.key not in {x.key for x in a.groups}],
        presets=list(a.presets) + [p for p in b.presets if p.key not in {x.key for x in a.presets}],
        density=list(a.density) + [d for d in b.density if d.key not in {x.key for x in a.density}],
        manifold=a.manifold if a.manifold is not None else b.manifold,
        exact_rows=a.exact_rows, context=a.context, order=a.order, title=a.title,
        caveats=list(a.caveats) + [c for c in b.caveats if c not in a.caveats],
        catalogs_provenance=list(a.catalogs_provenance) + list(b.catalogs_provenance),
        foreign=None)


def _numeric_count(c: CatalogSpec) -> int:
    return sum(1 for col in c.columns if col.role in NUMERIC_ROLES) + len(c.virtual)


def _page_id(base: str, key: str, multi: bool) -> str:
    return f"{base}_{key}" if multi else base


def standard_pages(ds: dict[str, DatasetSpec], landing: bool = True,
                   explorer: list[str] | None = None, manifold: bool | list[str] = False,
                   spectra: bool | list[str] = False, galleries: list[str] | None = None,
                   *, portal: str | None = None) -> list[PageSpec]:
    """Derive a conventional page list from the adapter's datasets.

    * ``landing`` -> page ``index`` (archetype landing) with every :class:`ReadmeSpec`.
    * ``explorer`` -> one explorer per listed catalog key (None: every non-context catalog).
    * ``manifold`` -> True: every non-context catalog with >= 3 numeric dims (or a
      :class:`ManifoldSpec`); a list names the catalogs.
    * ``spectra`` -> True: every ``pks1`` record set; a list names the record sets.
    * ``galleries`` -> one gallery per listed image set (None: every image set).

    Page ids are ``explorer``, ``manifold``, ``spectra``, ``gallery`` when there is one page
    of that kind and ``<kind>_<key>`` otherwise. Each page gets its primary dataset, the
    record and image sets linked to its catalog, context catalogs, and the shared
    resources (templates, filters, extinction, isochrones, hips, overlays). Pages that share
    a catalog get ``links`` to each other (``"<page>.html#id={id}"``).
    """
    cats = [k for k, v in ds.items() if isinstance(v, CatalogSpec) and not v.context]
    ctx_cats = [k for k, v in ds.items() if isinstance(v, CatalogSpec) and v.context]
    recs = {k: v for k, v in ds.items() if isinstance(v, RecordSetSpec)}
    imgs = {k: v for k, v in ds.items() if isinstance(v, ImageSetSpec)}
    readmes = [k for k, v in ds.items() if isinstance(v, ReadmeSpec)]
    shared = [k for k, v in ds.items()
              if isinstance(v, (TemplateSpec, FilterSetSpec, ExtinctionSpec, IsochroneSpec,
                                HipsSpec, OverlaySpec))]

    def check(keys: list[str], kind: type, what: str) -> None:
        for k in keys:
            if k not in ds:
                raise KeyError(f"{what} {k!r} is not a dataset")
            if not isinstance(ds[k], kind):
                raise TypeError(f"{what} {k!r} is a {type(ds[k]).__name__}, expected {kind.__name__}")

    exp_keys = cats if explorer is None else list(explorer)
    check(exp_keys, CatalogSpec, "explorer catalog")
    if manifold is True:
        man_keys = [k for k in cats if ds[k].manifold is not None or _numeric_count(ds[k]) >= 3]
    elif manifold is False or manifold is None:
        man_keys = []
    else:
        man_keys = list(manifold)
    check(man_keys, CatalogSpec, "manifold catalog")
    if spectra is True:
        spec_keys = [k for k, v in recs.items() if v.kind == "pks1" or v.legacy_json is not None]
    elif spectra is False or spectra is None:
        spec_keys = []
    else:
        spec_keys = list(spectra)
    check(spec_keys, RecordSetSpec, "spectra record set")
    gal_keys = list(imgs) if galleries is None else list(galleries)
    check(gal_keys, ImageSetSpec, "gallery image set")

    def attached(cat: str | None) -> list[str]:
        out: list[str] = []
        if cat is not None:
            out += [k for k, v in recs.items() if v.catalog == cat]
            out += [k for k, v in imgs.items() if v.catalog == cat]
        return out

    pages: list[PageSpec] = []
    by_cat: dict[str, list[str]] = {}

    def add(pid: str, arche: str, title: str, dss: list[str], cat: str | None, **kw: Any) -> None:
        seen: list[str] = []
        for d in dss:
            if d not in seen:
                seen.append(d)
        pages.append(PageSpec(id=pid, archetype=arche, title=title, datasets=seen, portal=portal, **kw))
        if cat is not None:
            by_cat.setdefault(cat, []).append(pid)

    if landing:
        add("index", "landing", "Overview", readmes, None)
    for k in exp_keys:
        title = ds[k].title or k
        add(_page_id("explorer", k, len(exp_keys) > 1), "explorer", title,
            [k] + attached(k) + ctx_cats + shared, k)
    for k in man_keys:
        title = (ds[k].title or k) + " manifold"
        add(_page_id("manifold", k, len(man_keys) > 1), "manifold", title,
            [k] + attached(k) + ctx_cats + shared, k, manifold=True)
    for k in spec_keys:
        cat = recs[k].catalog
        others = [r for r in attached(cat) if r != k]
        title = f"{ds[cat].title or cat} spectra" if isinstance(ds.get(cat), CatalogSpec) else f"{k} spectra"
        add(_page_id("spectra", k, len(spec_keys) > 1), "spectra", title,
            [cat, k] + others + shared, cat)
    for k in gal_keys:
        cat = imgs[k].catalog
        dss = [k] + ([cat] if cat else []) + shared
        add(_page_id("gallery", k, len(gal_keys) > 1), "gallery", f"{k} gallery", dss, cat)
    # cross links between pages that share a catalog
    for cat, pids in by_cat.items():
        if len(pids) < 2:
            continue
        for p in pages:
            if p.id not in pids:
                continue
            for other in pids:
                if other != p.id:
                    p.links.setdefault(other, f"{other}.html#id={{id}}")
    return pages
