"""ImageSetSpec -> ``images.json`` (``pk-images/1``, ARCHITECTURE.md section 5.10).

Document shape::

    {"schema": "pk-images/1",
     "roles":   [{"key", "label"}],
     "items":   [{"id", "row"?, "roles": {role: {"url", "w"?, "h"?, "thumb"?}},
                  "caption"?, "tags": {facet: value}, "w"?, "h"?}],
     "facets":  [{"key", "label", "values": [sorted distinct values]}],
     "presets": [{"key", "label", "facets": {facet: [values]}}],
     "catalog"?: dsId}

When the gzip size of the document exceeds ``IMAGES_SOFT_MAX`` (4 MiB) the items move to
page files ``images.<k>.<h8>.json`` (``{"items": [...]}``, k = 0, 1, ...) and the index keeps
``"items": []`` plus ``"pages": [...]`` (IMPLEMENTATION_NOTES item 10).

``row`` is the catalog stored row of the item, looked up through ``rows`` (a mapping or a
callable such as ``CatalogResult.row_of``). The lookup key is ``item.id``, or the item's tag
named by ``spec.join`` when ``join`` names a facet. A thumbnail hook ``thumb(url, item_id,
role)`` may return a thumbnail URL (or None); its result goes into the role's ``thumb``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .codec import gz
from .writer import IMAGES_SOFT_MAX, BundleError, BundleWriter, Ledger, dumps_json, to_jsonable

__all__ = ["write", "ImagesResult", "build_document", "paginate"]


@dataclass
class ImagesResult:
    doc: dict                         # what was written to images.json
    pages: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)   # item ids without a catalog row
    warnings: list[str] = field(default_factory=list)


def _g(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _pos_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        iv = int(v)
    except (TypeError, ValueError):
        return None
    return iv if iv >= 1 else None


def build_document(spec: Any, *, rows: Mapping[str, int] | Callable[[str], int | None] | None = None,
                   thumb: Callable[[str, str, str], str | None] | None = None,
                   warn: Callable[[str], None] | None = None) -> tuple[dict, list[str]]:
    """Return (images.json document without pagination, unmatched item ids)."""
    def _warn(m: str) -> None:
        if warn is not None:
            warn(m)

    roles = [{"key": str(k), "label": str(lbl)} for k, lbl in (_g(spec, "roles") or [])]
    role_keys = {r["key"] for r in roles}
    if len(role_keys) != len(roles):
        raise BundleError(f"{_g(spec, 'key')}: duplicate image role keys")
    facets_in = [(str(k), str(lbl)) for k, lbl in (_g(spec, "facets") or [])]
    facet_keys = [k for k, _ in facets_in]
    lookup = None
    if rows is not None:
        lookup = rows if callable(rows) else rows.get
    join = _g(spec, "join")

    items = []
    unmatched: list[str] = []
    seen: set = set()
    values: dict[str, set] = {k: set() for k in facet_keys}
    for it in _g(spec, "items") or []:
        iid = str(_g(it, "id"))
        if not iid:
            raise BundleError("image item with empty id")
        if iid in seen:
            raise BundleError(f"duplicate image id {iid!r}")
        seen.add(iid)
        rmap = {}
        for rk, url in (_g(it, "roles") or {}).items():
            if url in (None, ""):
                continue
            if rk not in role_keys:
                _warn(f"image {iid!r}: role {rk!r} not declared; added")
                roles.append({"key": str(rk), "label": str(rk)})
                role_keys.add(rk)
            rv: dict = {"url": str(url)}
            if thumb is not None:
                t = thumb(str(url), iid, str(rk))
                if t:
                    rv["thumb"] = str(t)
            rmap[str(rk)] = rv
        if not rmap:
            _warn(f"image {iid!r} has no image URL; skipped")
            continue
        out: dict = {"id": iid}
        if lookup is not None:
            tags = _g(it, "tags") or {}
            key = str(tags[join]) if join and join in tags else iid
            r = lookup(key)
            if r is None and key != iid:
                r = lookup(iid)
            if r is None:
                unmatched.append(iid)
            else:
                out["row"] = int(r)
        out["roles"] = rmap
        cap = _g(it, "caption") or ""
        if cap:
            out["caption"] = str(cap)
        tags = {}
        for tk, tv in (_g(it, "tags") or {}).items():
            if tv is None:
                continue
            tags[str(tk)] = str(tv)
            if tk in values:
                values[tk].add(str(tv))
        out["tags"] = tags
        # section 5.10 places w/h per role: roles:{role:{url,w?,h?,thumb?}}
        w, h = _pos_int(_g(it, "w")), _pos_int(_g(it, "h"))
        for rv in rmap.values():
            if w is not None:
                rv["w"] = w
            if h is not None:
                rv["h"] = h
        items.append(out)

    facets = [{"key": k, "label": lbl, "values": sorted(values[k], key=_natural)} for k, lbl in facets_in]
    presets = []
    for p in _g(spec, "presets") or []:
        data = dict(_g(p, "data") or {})
        fsel = data.get("facets", data)
        fout = {}
        for fk, fv in fsel.items():
            if fk not in values:
                _warn(f"image preset {_g(p, 'key')!r}: unknown facet {fk!r}; dropped")
                continue
            fv = [fv] if isinstance(fv, str) else [str(v) for v in fv]
            keep = [v for v in fv if v in values[fk]]
            if len(keep) != len(fv):
                _warn(f"image preset {_g(p, 'key')!r}: values {sorted(set(fv) - set(keep))} "
                      f"of facet {fk!r} not present; dropped")
            fout[fk] = keep
        presets.append({"key": str(_g(p, "key")), "label": str(_g(p, "label")), "facets": fout})

    doc: dict = {"schema": "pk-images/1", "roles": roles, "items": items, "facets": facets}
    if presets:
        doc["presets"] = presets
    cat = _g(spec, "catalog")
    if cat:
        doc["catalog"] = str(cat)
    if unmatched:
        _warn(f"{len(unmatched)} image items have no catalog row, e.g. {unmatched[:3]}")
    return to_jsonable(doc), unmatched


def _natural(s: str) -> tuple:
    """Numeric strings sort numerically before other strings."""
    try:
        f = float(s)
    except ValueError:
        return (1, 0.0, s)
    if math.isnan(f):
        return (1, 0.0, s)
    return (0, f, s)


def paginate(items: list[dict], max_gz: int) -> list[list[dict]]:
    """Split ``items`` into the fewest equal-count pages whose gzip size is <= ``max_gz``."""
    if not items:
        return [[]]
    total = len(gz(dumps_json({"items": items})))
    k = max(1, math.ceil(total / (0.9 * max_gz)))
    while True:
        size = math.ceil(len(items) / k)
        pages = [items[i:i + size] for i in range(0, len(items), size)]
        if all(len(gz(dumps_json({"items": p}))) <= max_gz for p in pages) or size == 1:
            return pages
        k = k + max(1, k // 4)


def write(spec: Any, dest: str | Path | BundleWriter, *,
          rows: Mapping[str, int] | Callable[[str], int | None] | None = None,
          thumb: Callable[[str, str, str], str | None] | None = None,
          ledger: Ledger | None = None, log: Callable[[str], None] | None = None,
          page_max: int = IMAGES_SOFT_MAX) -> ImagesResult:
    """Write ``images.json`` (and page files) for an ``ImageSetSpec`` into ``dest``
    (the image-set directory ``data/<set>/``)."""
    if isinstance(dest, BundleWriter):
        w = dest
        if ledger is not None and w.ledger is not ledger:
            w = BundleWriter(w.root, w.base, ledger, w.dataset, w.fsync)
    else:
        w = BundleWriter(Path(dest), "", ledger)
    key = _g(spec, "key")
    if w.dataset is None:
        w.dataset = key
    warnings: list[str] = []

    def warn(msg: str) -> None:
        warnings.append(msg)
        w.ledger.warn(f"{key}: {msg}")
        if log is not None:
            log(f"[{key}] warning: {msg}")

    doc, unmatched = build_document(spec, rows=rows, thumb=thumb, warn=warn)
    pages: list[str] = []
    data = dumps_json(doc)
    if len(gz(data)) > page_max:
        parts = paginate(doc["items"], page_max)
        for k, part in enumerate(parts):
            pages.append(w.put("", f"images.{k}", dumps_json({"items": part}), "json", "images"))
        doc = dict(doc)
        doc["items"] = []
        doc["pages"] = pages
        data = dumps_json(doc)
    w.put_stable("images.json", data, "images", first_paint=True)
    return ImagesResult(doc, pages, unmatched, warnings)
