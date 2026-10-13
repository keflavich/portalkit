"""``pk-catalog/1`` manifests over the GC manifold binaries, referenced in place.

The GC Treasury prototype (``gc-treasury/manifold_prototype``) and the per-field GC
manifolds (``gc-treasury/manifold_fields/<field>``) store a schema-1 manifest next to
gzip binaries:

* ``dims/<key>.u16.gz`` -- ``u16-shuffle-gzip``, code ``1 + rint((x-min)/(max-min)*65534)``,
  0 missing (exactly portalkit's ``u16q`` with ``lo=min``, ``hi=max``,
  ``step=(max-min)/65534``);
* ``errs/<key>.u8.gz`` -- log-quantized u8 over linear ``[lo, hi]`` (portalkit ``u8log`` with
  ``step`` in dex, IMPLEMENTATION_NOTES item 47);
* ``meta/dra.i16.gz``, ``meta/ddec.i16.gz`` -- shuffled int16 offsets of ``offset_step_mas``
  from each row's tile reference (portalkit ``i16off``);
* ``meta/tile.u8.gz`` -- the row's tile index (u8).

:func:`write` emits a portalkit manifest whose column and position ``file`` entries are URLs
of the GC files (``url_base`` + the GC relative path), so nothing is copied or re-encoded
except the tile index: portalkit's ``i16off`` reads a u16 tile file, so the u8 tile array is
widened to u16 and written (2-plane shuffled, gzip) into the dataset directory. The u8 file
itself is also exposed in place as the ``tile`` ``cat8`` column (levels = the tile reference
file names), so the per-pointing provenance stays filterable.
"""

from __future__ import annotations

import gzip
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

import numpy as np

from ..bundle import codec
from ..bundle.catalog import CatalogResult
from ..bundle.writer import BundleError, BundleWriter, Ledger
from ..constants import FADE_WIDTH

__all__ = ["GcManifestError", "read_manifest", "convert", "write", "tile_u16_bytes",
           "SCHEMA1_ENCODING"]

#: the encodings this converter understands (schema-1 ``encoding`` block)
SCHEMA1_ENCODING = {"dims": "u16-shuffle-gzip", "missing": 0, "meta_offsets": "i16-shuffle-gzip"}


class GcManifestError(BundleError):
    """The GC manifest is not a schema-1 manifold manifest this converter can map."""


def read_manifest(path: os.PathLike | str) -> dict:
    """Load and sanity-check a GC schema-1 ``manifest.json``."""
    try:
        with open(path, encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, ValueError) as exc:
        raise GcManifestError(f"cannot read GC manifest {path}: {exc}") from exc
    _check(obj, str(path))
    return obj


def _check(m: Any, where: str) -> None:
    if not isinstance(m, Mapping):
        raise GcManifestError(f"{where}: not a JSON object")
    for k in ("n", "encoding", "dims", "meta", "tile_refs"):
        if k not in m:
            raise GcManifestError(f"{where}: missing {k!r}")
    if m.get("schema") not in (1, "1", None):
        raise GcManifestError(f"{where}: unsupported schema {m.get('schema')!r}")
    enc = m["encoding"]
    if enc.get("dims") != SCHEMA1_ENCODING["dims"] or enc.get("missing", 0) != 0:
        raise GcManifestError(f"{where}: unsupported dims encoding {enc.get('dims')!r}")
    if enc.get("meta_offsets", SCHEMA1_ENCODING["meta_offsets"]) != SCHEMA1_ENCODING["meta_offsets"]:
        raise GcManifestError(f"{where}: unsupported offset encoding {enc.get('meta_offsets')!r}")
    meta = m["meta"]
    for k in ("dra", "ddec", "tile"):
        if k not in meta or "file" not in meta[k]:
            raise GcManifestError(f"{where}: meta.{k}.file missing")
    if meta["tile"].get("dtype", "u8") not in ("u8", "u16"):
        raise GcManifestError(f"{where}: unsupported tile dtype {meta['tile'].get('dtype')!r}")


def _url(url_base: str, rel: str) -> str:
    """Join ``url_base`` and a GC relative path. The result must be an http(s) URL or a
    relative path without ``..`` (the manifest schema forbids parent references)."""
    if urlsplit(rel).scheme in ("http", "https"):
        return rel
    base = url_base if url_base.endswith("/") or not url_base else url_base + "/"
    out = base + rel.lstrip("/")
    if urlsplit(out).scheme not in ("http", "https"):
        parts = out.split("/")
        if ".." in parts or out.startswith("/"):
            raise GcManifestError(f"GC file URL {out!r} must be absolute http(s) or a relative "
                                  f"path without '..'; pass an absolute url_base")
    return out


def _finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _quantiles(q: Any) -> list[float] | None:
    if isinstance(q, list) and len(q) == 101 and all(_finite(v) for v in q):
        vals = [float(v) for v in q]
        if all(b >= a for a, b in zip(vals, vals[1:])):
            return vals
    return None


def _tile_levels(refs: list[Mapping]) -> list[str]:
    out: list[str] = []
    for i, r in enumerate(refs):
        name = str(r.get("file") or f"tile{i}")
        name = os.path.basename(name) or f"tile{i}"
        if name in out:
            name = f"{name}#{i}"
        out.append(name)
    return out


def convert(m: Mapping, *, ds: str, url_base: str, tile_file: str | None,
            build: str | None = None, title: str | None = None,
            links: Mapping | None = None, provenance: Mapping | None = None,
            source_manifest: str | None = None, spec: Any = None,
            labeler: Any = None) -> dict:
    """Map a GC schema-1 manifest onto a ``pk-catalog/1`` manifest.

    ``tile_file`` is the dataset-relative path of the u16 tile file (see
    :func:`tile_u16_bytes`); ``None`` keeps the GC tile file, which requires a u16 tile
    dtype.

    ``spec``, when given, is the originating :class:`~portalkit.spec.CatalogSpec`: its
    ``presets``, ``virtual``, ``groups`` (merged with the groups the GC dims' own
    ``group`` metadata would otherwise auto-derive; an explicit group wins its key),
    ``manifold.frames``/``manifold.default_frame``, per-column ``filter`` (matched by
    :class:`~portalkit.spec.ColumnSpec.key` against the built columns) and ``id`` (marks
    the matching column's role ``"id"``, for the id/search metadata caps.py reads off a
    served manifest) are merged into the output -- otherwise only what the GC manifest
    itself carries is emitted, as before.

    ``labeler``, when given, provides ``scrub``/``scrub_text`` (the shape of
    :class:`~portalkit.build.PathLabeler`; not imported here to avoid a cycle with
    ``build.py``) and is applied to ``source_manifest`` and the raw ``gc_*`` provenance
    fields pulled from the GC manifest, so absolute local paths embedded there (e.g. in
    ``source`` or ``build_notes`` free text) do not leak into the served manifest
    (``portalkit check --gate G2``).
    """
    _check(m, source_manifest or ds)
    n = int(m["n"])
    cols: list[dict] = []
    errs_by_dim: dict[str, dict] = {}
    for e in m.get("errs") or []:
        dk = e.get("dim_key") or e.get("key")
        if dk is not None and "file" in e and _finite(e.get("lo")) and _finite(e.get("hi")):
            errs_by_dim[str(dk)] = e
    keys: set[str] = set()
    for d in m["dims"]:
        key = str(d["key"])
        lo, hi = d.get("min"), d.get("max")
        if not (_finite(lo) and _finite(hi)) or hi < lo:
            raise GcManifestError(f"dim {key!r}: min/max missing or inverted")
        unit = str(d.get("unit") or "")
        c: dict[str, Any] = {"key": key, "role": "mag" if unit == "mag" else "value",
                             "enc": "u16q", "file": _url(url_base, d["file"])}
        if d.get("label"):
            c["label"] = str(d["label"])
        if unit:
            c["unit"] = unit
        if d.get("group"):
            c["group"] = str(d["group"])
        if d.get("desc"):
            c["note"] = str(d["desc"])
        if unit == "mag":
            c["invert"] = True
        span = float(hi) - float(lo)
        c.update(lo=float(lo), hi=float(hi), step=(span / codec.QMAX) if span > 0 else 1.0,
                 clip="drop", shuffle=True)
        if _finite(d.get("center")):
            c["center"] = float(d["center"])
        if _finite(d.get("scale")) and d["scale"] > 0:
            c["scale"] = float(d["scale"])
        if isinstance(d.get("nvalid"), int):
            c["nvalid"] = min(int(d["nvalid"]), n)
        q = _quantiles(d.get("quantiles"))
        if q is not None:
            c["quantiles"] = q
        err = errs_by_dim.get(key)
        if err is not None:
            c["err"] = f"e_{key}"
        cols.append(c)
        keys.add(key)
    for dk, e in errs_by_dim.items():
        if dk not in keys:
            continue
        ek = f"e_{dk}"
        elo, ehi = float(e["lo"]), float(e["hi"])
        if elo <= 0 or ehi <= elo:
            raise GcManifestError(f"err {ek!r}: lo/hi must satisfy 0 < lo < hi")
        c = {"key": ek, "role": "error", "enc": "u8log", "file": _url(url_base, e["file"]),
             "label": f"σ {dk}", "lo": elo, "hi": ehi,
             "step": (math.log10(ehi) - math.log10(elo)) / codec.QMAX_U8, "clip": "drop"}
        unit = next((x.get("unit") for x in m["dims"] if x["key"] == dk), None)
        if unit:
            c["unit"] = str(unit)
        if e.get("desc"):
            c["note"] = str(e["desc"])
        if isinstance(e.get("nvalid"), int):
            c["nvalid"] = min(int(e["nvalid"]), n)
        q = _quantiles(e.get("quantiles"))
        if q is not None:
            c["quantiles"] = q
        cols.append(c)
        keys.add(ek)
    refs = list(m["tile_refs"])
    levels = _tile_levels(refs)
    tmeta = m["meta"]["tile"]
    if tmeta.get("dtype", "u8") == "u8" and "tile" not in keys and 0 < len(levels) <= 255:
        cols.append({"key": "tile", "role": "category", "enc": "cat8",
                     "file": _url(url_base, tmeta["file"]), "label": "Source table",
                     "levels": levels, "note": "catalogue table (pointing) each row came from"})
    for c in m.get("categories") or []:
        if not isinstance(c, Mapping) or "file" not in c or "key" not in c:
            continue
        lv = [str(x) for x in (c.get("levels") or [])]
        if not lv or c["key"] in keys:
            continue
        enc = "cat8" if len(lv) <= 255 else "cat16"
        cc = {"key": str(c["key"]), "role": "category", "enc": enc,
              "file": _url(url_base, c["file"]), "levels": lv}
        if c.get("label"):
            cc["label"] = str(c["label"])
        if isinstance(c.get("colors"), Mapping):
            cc["colors"] = {str(k): str(v) for k, v in c["colors"].items() if str(k) in lv}
        cols.append(cc)
        keys.add(cc["key"])

    col_specs_by_key = {c.key: c for c in (getattr(spec, "columns", None) or [])}
    id_key = str(getattr(spec, "id", "") or "")
    for c in cols:
        cs = col_specs_by_key.get(c["key"])
        if cs is not None and getattr(cs, "filter", None):
            c["filter"] = cs.filter
        if id_key and c["key"] == id_key:
            c["role"] = "id"

    if tile_file is None and tmeta.get("dtype", "u8") != "u16":
        raise GcManifestError("the u8 GC tile index needs a widened u16 tile file")
    pos = {"enc": "i16off",
           "step_mas": float(m["encoding"].get("offset_step_mas", 8.0)),
           "tile_refs": [[float(r["ref_ra"]), float(r["ref_dec"])] for r in refs],
           "files": {"tile": tile_file if tile_file is not None else _url(url_base, tmeta["file"]),
                     "dra": _url(url_base, m["meta"]["dra"]["file"]),
                     "ddec": _url(url_base, m["meta"]["ddec"]["file"])}}
    man: dict[str, Any] = {"schema": "pk-catalog/1", "id": ds}
    t = title if title is not None else m.get("title")
    if t:
        man["title"] = str(t)
    man["n"] = n
    if build:
        man["build"] = build
    man["order"] = {"kind": "input", "seed": int(m["seed"])} if isinstance(m.get("seed"), int) \
        else {"kind": "input"}
    man["pos"] = pos
    man["cols"] = cols

    presets_out: list[dict] = []
    for p in getattr(spec, "presets", None) or []:
        pd: dict[str, Any] = {"key": p.key, "label": p.label, "kind": p.kind}
        pdata = dict(getattr(p, "data", None) or {})
        for k in ("key", "label", "kind"):
            pdata.pop(k, None)
        pd.update(pdata)
        presets_out.append(pd)
    if presets_out:
        man["presets"] = presets_out

    virtual_out: list[dict] = []
    for v in getattr(spec, "virtual", None) or []:
        ve: dict[str, Any] = {"key": v.key}
        if getattr(v, "label", None) is not None:
            ve["label"] = v.label
        if getattr(v, "unit", None):
            ve["unit"] = v.unit
        if getattr(v, "group", None) is not None:
            ve["group"] = v.group
        virtual_out.append(ve)
    if virtual_out:
        man["virtual"] = virtual_out

    dims = [str(d["key"]) for d in m["dims"]]
    groups: dict[str, list[str]] = {}
    for d in m["dims"]:
        if d.get("group"):
            groups.setdefault(str(d["group"]), []).append(str(d["key"]))
    auto_groups = [{"key": g, "label": g, "dims": v} for g, v in groups.items()] if len(groups) > 1 else []
    explicit_groups = [{"key": g.key, "label": g.label, "dims": list(g.dims)}
                       for g in (getattr(spec, "groups", None) or [])]
    explicit_keys = {g["key"] for g in explicit_groups}
    merged_groups = explicit_groups + [g for g in auto_groups if g["key"] not in explicit_keys]
    if merged_groups:
        man["groups"] = merged_groups

    mf = getattr(spec, "manifold", None)
    man["manifold"] = {"dims": dims,
                       "default_frame": getattr(mf, "default_frame", None) or "pca",
                       "fade_width": FADE_WIDTH}
    mf_frames = dict(getattr(mf, "frames", None) or {}) if mf is not None else {}
    if mf_frames:
        man["manifold"]["frames"] = {k: [float(x) for x in v] for k, v in mf_frames.items()}

    if links:
        man["links"] = dict(links)
    prov: dict[str, Any] = dict(provenance or {})
    sm = source_manifest
    if labeler is not None and sm is not None:
        sm = labeler.scrub_text(str(sm))
    prov.setdefault("foreign", {"kind": "gc-manifest-1", "url_base": url_base,
                                "manifest": sm, "created": m.get("created")})
    for k in ("source", "sample", "cutout_layers", "build_notes"):
        if m.get(k) is not None:
            val = m[k] if labeler is None else labeler.scrub(m[k])
            prov.setdefault(f"gc_{k}", val)
    man["provenance"] = prov
    if m.get("caveats"):
        man["caveats"] = [str(c) for c in m["caveats"]]
    return man


def tile_u16_bytes(u8_gz: bytes, n: int) -> bytes:
    """Widen the GC u8 tile index (gzip) to portalkit's u16 2-plane shuffled gzip file."""
    raw = gzip.decompress(u8_gz) if u8_gz[:2] == b"\x1f\x8b" else bytes(u8_gz)
    tile = np.frombuffer(raw, dtype=np.uint8)
    if tile.size != n:
        raise GcManifestError(f"tile index has {tile.size} rows, manifest n={n}")
    return codec.file_bytes("u16", tile.astype(np.uint16), shuffle=True)


@dataclass
class GcResult:
    manifest: dict
    files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def write(source: Any, dest: str | Path | BundleWriter, *, ds: str | None = None,
          url_base: str | None = None, build: str | None = None, title: str | None = None,
          links: Mapping | None = None, provenance: Mapping | None = None,
          ledger: Ledger | None = None, log: Callable[[str], None] | None = None,
          labeler: Any = None) -> CatalogResult:
    """Write ``manifest.json`` (and the widened tile file) for a GC manifold into ``dest``.

    ``source`` is a :class:`~portalkit.spec.CatalogSpec` with ``foreign`` set (its
    ``url_base`` and ``manifest_path`` are used -- its ``presets``/``virtual``/``groups``/
    ``manifold``/``columns[].filter``/``id`` are merged into the manifest, see
    :func:`convert`), a :class:`~portalkit.spec.ForeignRef`, or the path of a GC
    ``manifest.json`` (then ``ds`` and ``url_base`` are required, and no CatalogSpec
    metadata is available to merge).
    Returns a :class:`~portalkit.bundle.catalog.CatalogResult` with empty id maps (GC
    manifolds carry no per-row ids).

    ``labeler``, when given, scrubs absolute local paths out of the ``provenance`` block
    (see :func:`convert`); pass ``build.PathLabeler`` for a real build.
    """
    foreign = getattr(source, "foreign", None)
    cat_spec: Any = None
    if foreign is not None:
        ds = ds or source.key
        title = title if title is not None else (getattr(source, "title", "") or None)
        caveats = list(getattr(source, "caveats", []) or [])
        cat_spec = source
    else:
        caveats = []
        foreign = source if hasattr(source, "manifest_path") else None
    if foreign is not None:
        mpath = Path(foreign.manifest_path)
        url_base = url_base if url_base is not None else foreign.url_base
    else:
        mpath = Path(source)
    if not ds:
        raise GcManifestError("dataset key (ds) is required")
    if url_base is None:
        raise GcManifestError("url_base is required")
    m = read_manifest(mpath)
    if isinstance(dest, BundleWriter):
        w = dest
        if ledger is not None and w.ledger is not ledger:
            w = BundleWriter(w.root, w.base, ledger, w.dataset, w.fsync)
    else:
        w = BundleWriter(Path(dest), "", ledger)
    if w.dataset is None:
        w.dataset = ds
    files: list[str] = []
    tmeta = m["meta"]["tile"]
    tile_rel: str | None = None
    if tmeta.get("dtype", "u8") == "u8":
        tpath = mpath.parent / tmeta["file"]
        try:
            data = tpath.read_bytes()
        except OSError as exc:
            raise GcManifestError(f"cannot read the GC tile index {tpath}: {exc}") from exc
        tile_rel = w.put("pos", "tile", tile_u16_bytes(data, int(m["n"])), kind="pos")
        files.append(tile_rel)
    man = convert(m, ds=ds, url_base=url_base, tile_file=tile_rel, build=build, title=title,
                  links=links, provenance=provenance, source_manifest=str(mpath),
                  spec=cat_spec, labeler=labeler)
    for c in caveats:
        man.setdefault("caveats", [])
        if c not in man["caveats"]:
            man["caveats"].append(c)
    w.put_stable_json("manifest.json", man, kind="manifest", first_paint=True)
    files.append("manifest.json")
    if log is not None:
        log(f"{ds}: GC manifest {mpath} -> {len(man['cols'])} columns referenced at {url_base}")
    return CatalogResult(manifest=man, order=None, ids=[], id_to_row={}, alias_to_row={},
                         files=files, warnings=[], writer=w)
