"""page.json assembly and validation (ARCHITECTURE.md sections 4.3, 6.10, 8).

:func:`assemble` turns a :class:`~portalkit.spec.PageSpec`, the build's dataset specs, the
written dataset documents (``manifests``: catalog manifests, records indexes, images.json,
hips.json, overlays.json, ...) and the resolver output (:class:`~portalkit.caps.CapsResult`)
into a page document. It fails with :class:`ConfigError` (listing every problem) when:

* a column key or expression in ``state``, pane options, catalog presets or dim overlays
  does not resolve against the catalog it applies to;
* a URL is neither relative inside the staged tree, nor ``{res}``/``{assets}``-prefixed,
  nor absolute under a known exposure prefix (:data:`KNOWN_PREFIXES` plus ``known_prefixes``);
* an enabled capability has no mounted provider pane;
* ``schema.validate_page`` reports an error (unknown pane options, 64 kB cap, ...).

Unset pane options are omitted, except the section 6.10 optional (``?``) options, which may
stay as ``null`` (IMPLEMENTATION_NOTES item 12).

Default DatasetRef URLs (overridable per dataset with ``refs``)::

    catalog     data/<ds>/manifest.json         (foreign: <url_base><manifest file name>)
    records     data/<ds>/index.json
    images      data/<ds>/images.json
    hips        data/hips.json                  (stable name, section 5.1)
    overlays    data/overlays.json              (stable name)
    narrative   data/narrative.json             (stable name)
    isochrones  data/iso.<h8>.json              (h8 of isochrones_file(spec) bytes)
    templates   res_refs["templates"]           (also filters, extinction, features)

Two datasets of one page that would share a default URL (for example two overlays
datasets) are an error; pass ``refs`` for one of them.

Scatter ``overlays`` ids name ``space:"dim"`` layers of the page's overlays documents, the
tracks of its isochrones datasets (the track ``label`` or its slug, e.g. ``mist_1myr``), or
the extinction vector of an attached extinction dataset (``<law>_vector`` in lower case,
e.g. ``g23_vector``).

A page of a ``public`` portal may not fetch data from the secure tree
(:data:`SECURE_PREFIX`, section 11.2); ``links`` may still point there.

The module also builds the small JSON documents written next to the page
(:func:`hips_document`, :func:`overlays_document`, :func:`narrative_document`,
:func:`isochrones_document`) and the readme fragment (:func:`readme_html`).
"""

from __future__ import annotations

import copy
import hashlib
import html
import json
import math
import re
import urllib.parse
from pathlib import Path
from typing import Any, Iterable, Mapping


from . import caps as capsmod
from .caps import CapsResult, CatInfo, PageData, iter_panes, page_data
from .render import markdown_lite
from .schema import OPTIONAL_PANE_OPTIONS, foreign_base_error, validate_hips, \
    validate_isochrones, validate_narrative, validate_overlays, validate_page
from .spec import (CatalogSpec, DatasetSpec, ExtinctionSpec, HipsSpec, ImageSetSpec,
                   IsochroneSpec, NarrativeSpec, OverlaySpec, PageSpec, ReadmeSpec, RecordSetSpec)
from .version import VERSION, asset_base, res_base

__all__ = [
    "ConfigError", "KNOWN_PREFIXES", "ALADIN_CDN", "ALADIN_MIRROR", "README_INLINE_MAX",
    "RES_KINDS", "assemble", "dataset_ref", "check_url", "check_foreign_url",
    "foreign_records_ref", "validate_keys", "readme_html",
    "readme_file_name", "hips_document", "overlays_document", "narrative_document",
    "isochrones_document", "isochrones_file", "SECURE_PREFIX", "PROVENANCE_MAX",
    "provenance_summary",
]

ALADIN_CDN = "https://aladin.cds.unistra.fr/AladinLite/api/v3/latest/aladin.js"
ALADIN_MIRROR = "https://data.rc.ufl.edu/pub/adamginsburg/jwst-gc/aladin.js"

#: Absolute URL prefixes with a known exposure (section 4.3 builder validation).
KNOWN_PREFIXES: tuple[str, ...] = (
    "https://data.rc.ufl.edu/pub/adamginsburg/",
    "https://data.rc.ufl.edu/secure/adamginsburg/",
    "https://aladin.cds.unistra.fr/",
    "https://alasky.cds.unistra.fr/",
    "https://alasky.u-strasbg.fr/",
    "https://starformation.astro.ufl.edu/",
    "https://mast.stsci.edu/",
)

#: URLs under this prefix need a login (section 11.2); a public page may not fetch them.
SECURE_PREFIX = "https://data.rc.ufl.edu/secure/"

#: Byte budget of one catalog's provenance summary inside page.json.
PROVENANCE_MAX = 4096

#: Inline readme limit (section 4.3); a larger readme goes to ``readme.<h8>.html``.
README_INLINE_MAX = 16384

#: Dataset kinds served from the shared resource tree.
RES_KINDS = ("templates", "filters", "extinction", "features")

_SPEC_KIND: tuple[tuple[type, str], ...] = capsmod.SPEC_KINDS

# pane options that name datasets (their targets are attached to the page automatically)
_DS_OPTIONS = capsmod.DS_OPTIONS


class ConfigError(ValueError):
    """page.json assembly failed; ``errors`` lists every problem found."""

    def __init__(self, errors: list[str], page: str = ""):
        self.errors = list(errors)
        head = f"page {page!r}: " if page else ""
        super().__init__(head + f"{len(self.errors)} configuration error(s):\n  "
                         + "\n  ".join(self.errors[:50]))


# ---------------------------------------------------------------------------
# dataset references and URLs
# ---------------------------------------------------------------------------

def _spec_kind(spec: DatasetSpec) -> str | None:
    return capsmod.spec_kind(spec)


def dataset_ref(ds: str, spec: DatasetSpec, refs: Mapping[str, Mapping] | None = None,
                res_refs: Mapping[str, str] | None = None) -> dict:
    """The page.json DatasetRef for ``spec`` (``refs[ds]`` replaces the default)."""
    kind = _spec_kind(spec)
    if kind is None:
        raise TypeError(f"dataset {ds!r}: {type(spec).__name__} has no DatasetRef kind")
    if refs and ds in refs:
        ref = dict(refs[ds])
        ref.setdefault("kind", kind)
        if ref["kind"] != kind:
            raise ValueError(f"refs[{ds!r}] has kind {ref['kind']!r}, the spec is {kind!r}")
        return ref
    if kind == "catalog":
        if spec.foreign is not None:
            base = spec.foreign.url_base if spec.foreign.url_base.endswith("/") else spec.foreign.url_base + "/"
            ref = {"kind": "catalog", "url": base + Path(spec.foreign.manifest_path).name}
        else:
            ref = {"kind": "catalog", "url": f"data/{ds}/manifest.json"}
        if spec.context:
            ref["context"] = True
        return ref
    if kind == "records":
        if getattr(spec, "foreign", None) is not None:
            return foreign_records_ref(ds, spec)
        return {"kind": "records", "url": f"data/{ds}/index.json", "catalog": spec.catalog,
                "record": spec.kind}
    if kind == "images":
        ref = {"kind": "images", "url": f"data/{ds}/images.json"}
        if spec.catalog:
            ref["catalog"] = spec.catalog
        return ref
    if kind in RES_KINDS:
        url = (res_refs or {}).get(kind)
        if not url:
            raise ValueError(f"dataset {ds!r} ({kind}) needs a published resource URL "
                             f"(refs[{ds!r}] or res_refs[{kind!r}])")
        return {"kind": kind, "url": url}
    if kind == "isochrones":
        try:
            name, _ = isochrones_file(spec)
        except ConfigError as err:
            raise ValueError(f"dataset {ds!r}: {err}") from err
        return {"kind": kind, "url": f"data/{name}"}
    return {"kind": kind, "url": f"data/{kind}.json"}


def _dot_segment(path: str) -> bool:
    """True when ``path`` (query and fragment removed) has a ``..`` segment once
    percent-decoded, with ``\\`` read as ``/`` (browsers treat ``%2e%2e`` as ``..``)."""
    p = path.split("#", 1)[0].split("?", 1)[0]
    p = urllib.parse.unquote(p).replace("\\", "/")
    return ".." in p.split("/")


def foreign_records_ref(ds: str, spec: RecordSetSpec) -> dict:
    """The page.json DatasetRef of a foreign record set (section 5.6, contract (A)).

    ``url`` is the foreign index (``<base><key>/index.json``), ``base`` the foreign portal's
    ``data/`` prefix (the JS resolves shard URLs against the index URL, which lies under
    ``base``) and ``id_column`` the catalog column holding the foreign record id. Raises
    ValueError for a filesystem path, a missing trailing '/' or an empty/unsafe key."""
    fr = spec.foreign
    err = foreign_base_error(fr.base)
    if err is not None:
        raise ValueError(f"dataset {ds!r}: foreign records base {err}")
    if not isinstance(fr.key, str) or not fr.key or "/" in fr.key or "\\" in fr.key \
            or fr.key in (".", ".."):
        raise ValueError(f"dataset {ds!r}: foreign records key {fr.key!r} must be one path segment")
    ref = {"kind": "records", "url": f"{fr.base}{fr.key}/index.json", "catalog": spec.catalog,
           "record": spec.kind, "base": fr.base}
    if fr.id_column:
        ref["id_column"] = fr.id_column
    return ref


def check_foreign_url(url: Any, known_prefixes: Iterable[str] = ()) -> str | None:
    """:func:`check_url` for a foreign records URL: a page-relative URL may climb out of
    the staged tree with leading ``..`` segments (to a sibling portal on the same host);
    everything else follows :func:`check_url` (no filesystem paths, absolute URLs under a
    known exposure prefix)."""
    if isinstance(url, str) and not re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", url) \
            and not url.startswith(("/", "\\")):
        rest = url
        while rest.startswith("../"):
            rest = rest[3:]
        if rest != url:
            return check_url(rest, known_prefixes) if rest else "foreign URL has no path"
    return check_url(url, known_prefixes)


def check_url(url: Any, known_prefixes: Iterable[str] = ()) -> str | None:
    """None when ``url`` is allowed in page.json, else the reason it is not.

    Allowed: a relative path inside the staged tree (no scheme, no leading ``/``, no ``..``
    segment), a ``{res}``/``{assets}``-prefixed path, or an http(s) URL under a known prefix.
    ``..`` segments are found after percent-decoding (``%2e%2e``), and ``\\`` counts as ``/``.
    """
    if not isinstance(url, str) or not url:
        return f"expected a non-empty URL string, got {url!r}"
    if any(ord(ch) < 0x20 or ch.isspace() for ch in url):
        return f"URL {url!r} contains whitespace or control characters"
    for tok in ("{res}", "{assets}"):
        if url.startswith(tok):
            rest = url[len(tok):]
            if rest.startswith(("/", "\\")) or _dot_segment(rest):
                return f"URL {url!r} leaves the {tok} tree"
            return None
    low = url.lower()
    if low.startswith(("http://", "https://")):
        if "\\" in url:
            return f"URL {url!r} contains a backslash"
        for p in tuple(KNOWN_PREFIXES) + tuple(known_prefixes):
            if url.startswith(p):
                if _dot_segment(url[len(p):]):
                    return f"URL {url!r} contains a '..' segment"
                return None
        return f"absolute URL {url!r} is not under a known exposure prefix"
    if re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", url):
        return f"URL {url!r} has a disallowed scheme"
    if url.startswith(("/", "\\")):
        return f"URL {url!r} is absolute on the server; use a path relative to the page"
    if _dot_segment(url):
        return f"URL {url!r} leaves the staged tree ('..')"
    return None


# ---------------------------------------------------------------------------
# readme
# ---------------------------------------------------------------------------

def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and not math.isfinite(v):
        return ""
    return markdown_lite.render_inline(str(v))


def _block_html(i: int, b: Mapping) -> str:
    if not isinstance(b, Mapping):
        raise ValueError(f"readme block {i}: expected an object, got {type(b).__name__}")
    t = b.get("type")
    if t == "markdown":
        return markdown_lite.render(str(b.get("text", "")))
    if t == "code":
        lang = re.sub(r"[^A-Za-z0-9_+#.-]", "", str(b.get("lang", "")))[:32]
        cls = f' class="language-{lang}"' if lang else ""
        return f"<pre><code{cls}>{html.escape(str(b.get('text', '')))}</code></pre>"
    if t == "list":
        items = b.get("items") or []
        tag = "ol" if b.get("ordered") else "ul"
        return f"<{tag}>" + "".join(f"<li>{_cell(x)}</li>" for x in items) + f"</{tag}>"
    if t == "table":
        cols = list(b.get("columns") or [])
        rows = b.get("rows") or []
        if not cols:
            raise ValueError(f"readme block {i}: table needs 'columns'")
        out = ["<table><thead><tr>", "".join(f"<th>{_cell(c)}</th>" for c in cols), "</tr></thead><tbody>"]
        for r in rows:
            cells = list(r.values()) if isinstance(r, Mapping) else list(r)
            if isinstance(r, Mapping):
                cells = [r.get(c) for c in cols]
            cells = (cells + [None] * len(cols))[:len(cols)]
            out.append("<tr>" + "".join(f"<td>{_cell(c)}</td>" for c in cells) + "</tr>")
        out.append("</tbody></table>")
        return "".join(out)
    raise ValueError(f"readme block {i}: unknown type {t!r} (markdown, code, list, table)")


def readme_html(spec: ReadmeSpec) -> str:
    """Render a :class:`ReadmeSpec` (Markdown text or file, then blocks) to safe HTML."""
    md = spec.markdown
    if isinstance(md, Path):
        md = md.read_text(encoding="utf-8")
    parts = [markdown_lite.render(md)] if md else []
    parts += [_block_html(i, b) for i, b in enumerate(spec.blocks)]
    return "\n".join(p for p in parts if p)


def readme_file_name(html_text: str) -> str:
    """Content-hashed file name for a readme too large to inline."""
    return f"readme.{hashlib.sha256(html_text.encode('utf-8')).hexdigest()[:8]}.html"


_PROV_SCALAR_MAX = 300      # characters kept of one provenance string
_PROV_LIST_MAX = 20         # entries kept of one provenance list
_PROV_ITEM_KEYS = {
    "inputs": ("path", "sha256", "mtime", "size"),
    "legacy_modules": ("name", "path", "sha256"),
    "catalogs": ("name", "title", "path", "n"),
}


_MISSING = object()


def _prov_scalar(v: Any) -> Any:
    if isinstance(v, str):
        return v if len(v) <= _PROV_SCALAR_MAX else v[:_PROV_SCALAR_MAX - 1] + "\u2026"
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    return _MISSING


def _prov_size(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":"),
                          default=str).encode("utf-8"))


def provenance_summary(prov: Mapping) -> dict:
    """Small copy of a manifest ``provenance`` object for page.json ``readme.provenance``.

    Scalar fields are kept (long strings shortened). ``inputs``, ``legacy_modules`` and
    ``catalogs`` keep up to 20 entries with their identifying fields only; a catalog's FITS
    ``header`` becomes ``header_cards`` (its card count). Other nested values are dropped.
    The result stays under :data:`PROVENANCE_MAX` bytes (trailing list entries are removed
    first, then lists). The complete provenance stays in the catalog manifest.
    """
    out: dict[str, Any] = {}
    for k, v in prov.items():
        if not isinstance(k, str):
            continue
        sv = _prov_scalar(v)
        if sv is not _MISSING:
            out[k] = sv
            continue
        if isinstance(v, (list, tuple)) and k in _PROV_ITEM_KEYS:
            items = []
            for it in list(v)[:_PROV_LIST_MAX]:
                if not isinstance(it, Mapping):
                    sv = _prov_scalar(it)
                    if sv is not _MISSING:
                        items.append(sv)
                    continue
                ent = {}
                for f in _PROV_ITEM_KEYS[k]:
                    if f in it:
                        fv = _prov_scalar(it[f])
                        if fv is not _MISSING:
                            ent[f] = fv
                if isinstance(it.get("header"), Mapping):
                    ent["header_cards"] = len(it["header"])
                items.append(ent)
            if len(v) > _PROV_LIST_MAX:
                items.append({"truncated": len(v) - _PROV_LIST_MAX})
            out[k] = items
    # enforce the byte budget: trim list tails, then drop lists, then long strings
    lists = [k for k, v in out.items() if isinstance(v, list)]
    while _prov_size(out) > PROVENANCE_MAX and lists:
        k = max(lists, key=lambda key: _prov_size(out[key]))
        items = out[k]
        tail = items[-1] if items else None
        prev = tail["truncated"] if isinstance(tail, dict) and set(tail) == {"truncated"} else 0
        body = items[:-1] if prev else items
        if len(body) > 1:
            keep = len(body) // 2
            out[k] = body[:keep] + [{"truncated": prev + len(body) - keep}]
        else:
            del out[k]
            lists.remove(k)
    while _prov_size(out) > PROVENANCE_MAX and out:
        out.pop(max(out, key=lambda key: _prov_size(out[key])))
    return out


def _provenance(pd: PageData, cats: Iterable[str]) -> dict:
    out: dict[str, Any] = {}
    for ds in cats:
        man = pd.manifests.get(ds)
        spec = pd.all_specs.get(ds)
        ent: dict[str, Any] = {}
        if isinstance(spec, CatalogSpec):
            if spec.title:
                ent["title"] = spec.title
            if spec.caveats:
                ent["caveats"] = list(spec.caveats)
        if isinstance(man, Mapping):
            if isinstance(man.get("n"), int):
                ent["n"] = man["n"]
            if isinstance(man.get("title"), str) and man["title"]:
                ent["title"] = man["title"]
            prov = man.get("provenance")
            if isinstance(prov, Mapping):
                small = provenance_summary(prov)
                if small:
                    ent["provenance"] = small
            if man.get("caveats"):
                ent["caveats"] = list(man["caveats"])
        elif isinstance(spec, CatalogSpec):
            if spec.table is not None:
                ent["n"] = len(spec.table)
            if spec.catalogs_provenance:
                ent["provenance"] = provenance_summary({"catalogs": list(spec.catalogs_provenance)})
        if ent:
            out[ds] = ent
    return {"catalogs": out} if out else {}


# ---------------------------------------------------------------------------
# key validation
# ---------------------------------------------------------------------------

def _dim_alias_errors(alias: Mapping, pd: PageData) -> list[str]:
    """``page.legacy.dim_alias`` (contract (B)): every value must be a dim of the page's
    dataset (a stored or virtual key of a foreground catalog on the page)."""
    errs: list[str] = []
    keys: set[str] = set()
    for k in pd.ids:
        info = pd.cats.get(k)
        if info is not None and not info.context:
            keys |= set(info.cols) | set(info.virtual) | set(info.manifold_dims or ())
    for lk, uk in alias.items():
        if not isinstance(lk, str) or not lk:
            errs.append(f"page.legacy.dim_alias: legacy key {lk!r} must be a non-empty string")
        elif not isinstance(uk, str) or uk not in keys:
            errs.append(f"page.legacy.dim_alias.{lk}: {uk!r} is not a dim of the page's dataset "
                        f"({', '.join(sorted(keys)[:12]) or 'no catalog'}"
                        f"{', ...' if len(keys) > 12 else ''})")
    return errs


def _cmd_pane_errors(cmd_pane: str, layout: Any) -> list[str]:
    """``page.legacy.cmd_pane`` (contract (E), ARCHITECTURE.md section 6.4): must name a
    scatter pane of this page -- gc-query's legacy CMD axes (a/b/m) map onto its
    ``.x``/``.y`` canonical keys instead of ``<manifold>.frame`` when it is set."""
    ids = {(p.get("id") or p.get("pane")): p.get("pane") for p in capsmod.iter_panes(layout)}
    if cmd_pane not in ids:
        return [f"page.legacy.cmd_pane: {cmd_pane!r} is not a pane of this page "
                f"({', '.join(sorted(k for k in ids if k)) or 'no panes'})"]
    if ids[cmd_pane] != "scatter":
        return [f"page.legacy.cmd_pane: {cmd_pane!r} is a {ids[cmd_pane]!r} pane, not scatter"]
    return []


def _pane_catalog(leaf: Mapping, pd: PageData, refs: Mapping[str, Mapping]) -> str | None:
    ds = leaf.get("dataset")
    if isinstance(ds, str):
        ref = refs.get(ds)
        if ref is not None:
            if ref.get("kind") == "catalog":
                return ds
            if ref.get("kind") in ("records", "images"):
                return ref.get("catalog")
    return pd.primary


def _sky_layers(pd: PageData, refs: Mapping[str, Mapping]) -> tuple[set[str], set[str]]:
    sky, dim = set(), set()
    for ds, ref in refs.items():
        if ref.get("kind") != "overlays":
            continue
        doc = pd.manifests.get(ds)
        if not isinstance(doc, Mapping) and isinstance(pd.all_specs.get(ds), OverlaySpec):
            doc = {"layers": pd.all_specs[ds].layers}
        for lay in (doc or {}).get("layers") or []:
            if isinstance(lay, Mapping) and isinstance(lay.get("id"), str):
                (dim if lay.get("space") == "dim" else sky).add(lay["id"])
    return sky, dim


#: Overlay id of an isochrone track label (``"MIST 1 Myr"`` -> ``mist_1_myr``); defined in
#: :mod:`portalkit.caps`, which uses it for the default overlays of scatter/manifold panes.
overlay_slug = capsmod.overlay_slug


def _computed_dim_layers(pd: PageData, refs: Mapping[str, Mapping]) -> set[str]:
    """Dim overlay ids drawn from isochrones and extinction datasets (section 7.4)."""
    out: set[str] = set()
    for ds, ref in refs.items():
        kind = ref.get("kind")
        spec = pd.all_specs.get(ds)
        doc = pd.manifests.get(ds)
        if kind == "isochrones":
            labels: list[Any] = []
            if isinstance(spec, IsochroneSpec):
                labels += [t.label for t in spec.tracks]
            if isinstance(doc, Mapping):
                labels += [t.get("label") for t in doc.get("tracks") or [] if isinstance(t, Mapping)]
            for lab in labels:
                if isinstance(lab, str) and lab:
                    out |= {lab, overlay_slug(lab)}
        elif kind == "extinction":
            law = spec.law if isinstance(spec, ExtinctionSpec) else (
                doc.get("law") if isinstance(doc, Mapping) and isinstance(doc.get("law"), str) else "G23")
            out.add(f"{overlay_slug(law)}_vector")
    return out


def _fits_ids(pd: PageData, refs: Mapping[str, Mapping]) -> set[str]:
    out = set()
    for ds, ref in refs.items():
        if ref.get("kind") != "hips":
            continue
        doc = pd.manifests.get(ds)
        fits = doc.get("fits") if isinstance(doc, Mapping) else getattr(pd.all_specs.get(ds), "fits", [])
        out |= {f["id"] for f in fits or [] if isinstance(f, Mapping) and isinstance(f.get("id"), str)}
    return out


def _image_roles(pd: PageData, ds: str | None) -> set[str] | None:
    if not ds:
        return None
    doc = pd.manifests.get(ds)
    if isinstance(doc, Mapping) and isinstance(doc.get("roles"), list):
        return {r.get("key") for r in doc["roles"] if isinstance(r, Mapping)}
    spec = pd.all_specs.get(ds)
    if isinstance(spec, ImageSetSpec):
        return {r[0] for r in spec.roles}
    return None


def _check_dim_overlays(errs: list[str], path: str, o: Mapping, dim_ids: set[str]) -> None:
    ovs = o.get("overlays")
    if not isinstance(ovs, (list, tuple)):
        return
    for lay in ovs:
        if lay not in dim_ids:
            errs.append(f"{path}.overlays: unknown dim overlay layer {lay!r} (not a dim "
                        f"layer of an overlays dataset, an isochrone track or an extinction vector)")


def validate_keys(doc: Mapping, pd: PageData) -> list[str]:
    """Check every column key and expression in ``doc`` (state, layout options) and in the
    catalogs' presets and dim overlays against the catalog it applies to."""
    errs: list[str] = []
    refs = doc.get("datasets") or {}
    cats: dict[str, CatInfo] = {k: v for k, v in pd.cats.items() if k in refs}

    def need(path: str, ds: str | None, expr: Any, numeric: bool = True) -> None:
        if ds is None or ds not in cats:
            errs.append(f"{path}: {expr!r} has no catalog to resolve against")
            return
        msg = cats[ds].resolves(expr, numeric=numeric)
        if msg is not None:
            errs.append(f"{path}: {msg}")

    leaves = list(iter_panes(doc.get("layout")))
    by_id = {leaf.get("id", leaf.get("pane")): leaf for leaf in leaves}
    sky_ids, dim_ids = _sky_layers(pd, refs)
    dim_ids |= _computed_dim_layers(pd, refs)
    fits_ids = _fits_ids(pd, refs)
    fg = [k for k in cats if not cats[k].context]

    for n, leaf in enumerate(leaves):
        ptype = leaf.get("pane")
        pid = leaf.get("id", ptype)
        o = leaf.get("options") or {}
        path = f"layout pane {pid!r} options"
        cat = _pane_catalog(leaf, pd, refs)
        if ptype == "scatter":
            for k in ("x", "y", "hollow_if"):
                if o.get(k) is not None:
                    need(f"{path}.{k}", cat, o[k])
            if isinstance(o.get("color"), str):
                need(f"{path}.color", cat, o["color"], numeric=False)
            if isinstance(o.get("labels"), str):
                need(f"{path}.labels", cat, o["labels"], numeric=False)
            pairs = o.get("pairs")
            if isinstance(pairs, Mapping):
                for k in ("x2", "y2"):
                    if k in pairs:
                        need(f"{path}.pairs.{k}", cat, pairs[k])
            for gi, g in enumerate(o.get("grid") or []):
                if isinstance(g, (list, tuple)) and len(g) == 2:
                    need(f"{path}.grid[{gi}].x", cat, g[0])
                    need(f"{path}.grid[{gi}].y", cat, g[1])
                else:
                    errs.append(f"{path}.grid[{gi}]: expected [x, y]")
            bg = o.get("background")
            if isinstance(bg, str) and bg in cats:
                for k in ("x", "y"):
                    if o.get(k) is not None:
                        need(f"{path}.{k} (background {bg!r})", bg, o[k])
            _check_dim_overlays(errs, path, o, dim_ids)
            for ri, r in enumerate(o.get("raster") or []):
                if isinstance(r, Mapping):
                    for k in ("x", "y"):
                        if isinstance(r.get(k), str):
                            need(f"{path}.raster[{ri}].{k}", cat, r[k])
        elif ptype == "list" or ptype == "table":
            for c in o.get("columns") or []:
                need(f"{path}.columns", cat, c, numeric=False)
            sort = o.get("sort")
            if isinstance(sort, str) and sort:
                need(f"{path}.sort", cat, sort.lstrip("-+"), numeric=False)
        elif ptype == "detail":
            for k in ("bars", "prose"):
                for c in o.get(k) or []:
                    need(f"{path}.{k}", cat, c, numeric=False)
        elif ptype == "hist":
            if o.get("expr") is not None:
                need(f"{path}.expr", cat, o["expr"])
        elif ptype == "manifold":
            dims = o.get("dims")
            info = cats.get(cat) if cat else None
            if isinstance(dims, str) and dims.startswith("group:"):
                if info is None or dims[6:] not in info.groups:
                    errs.append(f"{path}.dims: unknown group {dims[6:]!r}")
                else:
                    for d in info.groups[dims[6:]]:
                        need(f"{path}.dims ({dims})", cat, d)
            elif isinstance(dims, list):
                for d in dims:
                    need(f"{path}.dims", cat, d)
            _check_dim_overlays(errs, path, o, dim_ids)
            fr = o.get("frame")
            if isinstance(fr, str) and fr != "pca" and info is not None \
                    and fr not in info.frames and fr not in info.axes_presets:
                errs.append(f"{path}.frame: unknown frame preset {fr!r} (known: "
                            f"{', '.join(['pca'] + sorted(info.frames | info.axes_presets))})")
            bg = o.get("background")
            if isinstance(bg, str) and bg in cats and info is not None:
                if isinstance(dims, list):
                    bdims = dims
                elif isinstance(dims, str) and dims[6:] in info.groups:
                    bdims = info.groups[dims[6:]]
                else:
                    bdims = info.manifold_dims or []
                for d in bdims:
                    need(f"{path}.dims (background {bg!r})", bg, d)
        elif ptype == "sky":
            for lay in o.get("overlays") or []:
                if lay not in sky_ids:
                    errs.append(f"{path}.overlays: unknown sky overlay layer {lay!r}")
            for f in o.get("fits") or []:
                if f not in fits_ids:
                    errs.append(f"{path}.fits: unknown FITS layer {f!r}")
        elif ptype == "gallery":
            roles = _image_roles(pd, o.get("images") or leaf.get("dataset"))
            for r in o.get("roles") or []:
                if roles is not None and r not in roles:
                    errs.append(f"{path}.roles: unknown image role {r!r}")
            for c in o.get("sort") or []:
                ds = o.get("images") or leaf.get("dataset")
                spec = pd.all_specs.get(ds) if isinstance(ds, str) else None
                if isinstance(spec, ImageSetSpec) and spec.catalog in cats:
                    if cats[spec.catalog].resolves(c.lstrip("-+"), numeric=False) is None:
                        continue
                facets = {f[0] for f in spec.facets} if isinstance(spec, ImageSetSpec) else set()
                if c.lstrip("-+") not in facets | {"id", "caption"}:
                    errs.append(f"{path}.sort: {c!r} is neither a catalog column nor a facet")

    # state
    st = doc.get("state") or {}
    color = st.get("color")
    if isinstance(color, Mapping) and isinstance(color.get("by"), str):
        by = color["by"]
        if not any(cats[k].resolves(by, numeric=False) is None for k in fg):
            errs.append(f"state.color.by: {by!r} does not resolve in any catalog on the page "
                        f"({', '.join(fg) or 'none'})")
    for key, v in st.items():
        if key.startswith("filters:") and isinstance(v, Mapping):
            ds = key.split(":", 1)[1]
            if ds not in cats:
                continue
            for rk in (v.get("ranges") or {}):
                need(f"state.{key}.ranges", ds, rk)
            for ck in (v.get("cats") or {}):
                need(f"state.{key}.cats", ds, ck, numeric=False)
            for fk in (v.get("flags") or {}):
                need(f"state.{key}.flags", ds, fk, numeric=False)
            for sk in (v.get("snr") or {}) if isinstance(v.get("snr"), Mapping) else ():
                need(f"state.{key}.snr", ds, sk, numeric=False)
        elif key.startswith("frame:"):
            pane = key.split(":", 1)[1]
            leaf = by_id.get(pane)
            if leaf is None:
                continue
            cat = _pane_catalog(leaf, pd, refs)
            info = cats.get(cat) if cat else None
            preset = v.get("preset") if isinstance(v, Mapping) else v
            # a manifold's initial view may be a named frame or an axes preset (a 2-D
            # projection onto two dims; the JS applies both kinds through the preset chips)
            if isinstance(preset, str) and preset != "pca" and not preset.startswith("b64:") \
                    and info is not None and preset not in info.frames \
                    and preset not in info.axes_presets:
                errs.append(f"state.{key}.preset: unknown frame or axes preset {preset!r}")
            if isinstance(v, Mapping):
                for d in v.get("dims") or []:
                    need(f"state.{key}.dims", cat, d)
        elif key.startswith("axes:") and isinstance(v, Mapping):
            leaf = by_id.get(key.split(":", 1)[1])
            if leaf is None:
                continue
            cat = _pane_catalog(leaf, pd, refs)
            for a in ("x", "y"):
                if a in v:
                    need(f"state.{key}.{a}", cat, v[a])

    # catalog presets and dim overlays
    for ds, info in cats.items():
        for p in info.presets:
            kind = p.get("kind")
            pp = f"catalog {ds!r} preset {p.get('key')!r}"
            if kind == "axes":
                for a in ("x", "y"):
                    if a in p:
                        need(f"{pp}.{a}", ds, p[a])
            elif kind == "filter":
                for rk in (p.get("ranges") or {}):
                    need(f"{pp}.ranges", ds, rk)
                for ck in (p.get("cats") or {}):
                    need(f"{pp}.cats", ds, ck, numeric=False)
    for ds, ref in refs.items():
        if ref.get("kind") != "overlays":
            continue
        odoc = pd.manifests.get(ds)
        if not isinstance(odoc, Mapping) and isinstance(pd.all_specs.get(ds), OverlaySpec):
            odoc = {"layers": pd.all_specs[ds].layers}
        for lay in (odoc or {}).get("layers") or []:
            if not isinstance(lay, Mapping) or lay.get("space") != "dim":
                continue
            for j, it in enumerate(lay.get("items") or []):
                if not isinstance(it, Mapping):
                    continue
                exprs = [it[k] for k in ("x", "y") if k in it] + list((it.get("dims") or {}).keys())
                for ex in exprs:
                    if not any(cats[k].resolves(ex) is None for k in cats):
                        errs.append(f"overlay layer {lay.get('id')!r} item {j}: {ex!r} does not "
                                    f"resolve in any catalog on the page")
    return errs


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------

def _clean_layout(node: Any) -> Any:
    """Copy the layout dropping ``None`` options (unless optional) and ``None`` datasets."""
    if not isinstance(node, dict):
        return node
    out = {}
    for k, v in node.items():
        if k in ("row", "col"):
            out[k] = [_clean_layout(c) for c in v] if isinstance(v, list) else v
        elif k == "options" and isinstance(v, dict):
            opt = OPTIONAL_PANE_OPTIONS.get(node.get("pane"), frozenset())
            o = {ok: copy.deepcopy(ov) for ok, ov in v.items() if ov is not None or ok in opt}
            if o:
                out[k] = o
        elif k == "dataset" and v is None:
            continue
        else:
            out[k] = copy.deepcopy(v)
    return out


def _drop_pane(node: Any, pid: str) -> Any:
    """Copy of ``node`` without the pane leaf ``pid``; a split left with one child becomes
    that child (keeping the split's size), an empty one disappears (``None``)."""
    if not isinstance(node, dict):
        return node
    if "pane" in node:
        return None if node.get("id", node.get("pane")) == pid else node
    for k in ("row", "col"):
        if isinstance(node.get(k), list):
            kids = [c for c in (_drop_pane(c, pid) for c in node[k]) if c is not None]
            if not kids:
                return None
            if len(kids) == 1:
                only = dict(kids[0])
                if "size" in node and "size" not in only:
                    only["size"] = node["size"]
                return only
            return {**node, k: kids}
    return node


def _prune_added(layout: Any, added: list[dict], enabled: list[str], dropped: Mapping[str, str],
                 kinds: Iterable[str] | None = None) -> Any:
    """Remove the collapsed panes the resolver added that only served capabilities in
    ``dropped``: a pane goes when no enabled capability loses its last provider. An added
    SED pane also goes when ``kinds`` (the page's dataset kinds) lacks ``filters``: without a
    filters dataset it places no points (``caps._make_pane`` does not build it either)."""
    types = {t for c in dropped for t in capsmod.PROVIDERS.get(c, ())}
    no_filters = kinds is not None and "filters" not in set(kinds)
    for leaf in reversed(added):
        forced = no_filters and leaf.get("pane") == "sed"
        if leaf.get("pane") not in types and not forced:
            continue
        trial = _drop_pane(layout, leaf.get("id", leaf.get("pane")))
        if trial is None:
            continue
        if forced or set(capsmod.missing_providers(trial, enabled)) <= set(
                capsmod.missing_providers(layout, enabled)):
            layout = trial
    return layout


def _drop_overlay(node: Any, oid: str, ids: set[str] | None) -> Any:
    """Copy of ``node`` whose scatter / manifold panes (those with an id in ``ids``; every
    one when ``ids`` is None) no longer list the dim overlay ``oid``."""
    if not isinstance(node, dict):
        return node
    for k in ("row", "col"):
        if isinstance(node.get(k), list):
            return {**node, k: [_drop_overlay(c, oid, ids) for c in node[k]]}
    if node.get("pane") not in ("scatter", "manifold"):
        return node
    if ids is not None and node.get("id", node.get("pane")) not in ids:
        return node
    opts = node.get("options")
    if not isinstance(opts, dict) or not isinstance(opts.get("overlays"), list) \
            or oid not in opts["overlays"]:
        return node
    o = dict(opts)
    o["overlays"] = [x for x in opts["overlays"] if x != oid]
    if not o["overlays"]:
        del o["overlays"]
    out = {**node, "options": o}
    if not o:
        del out["options"]
    return out


def _used_datasets(layout: Any) -> list[str]:
    return capsmod.layout_datasets(layout)


def _json_number(v: float) -> float | None:
    return None if not math.isfinite(v) else float(v)


def assemble(page: PageSpec, datasets: Mapping[str, DatasetSpec], manifests: Mapping[str, Any],
             caps: CapsResult | None = None, *, portal: Mapping[str, Any],
             refs: Mapping[str, Mapping] | None = None, res_refs: Mapping[str, str] | None = None,
             pk: str = VERSION, vendor_assets: bool = False, aladin: Mapping[str, str] | None = None,
             budget: Mapping[str, int] | None = None, known_prefixes: Iterable[str] = (),
             files: dict[str, bytes] | None = None) -> dict:
    """Assemble and validate the page.json document for ``page``.

    ``portal`` holds ``id``, ``title`` and ``visibility`` of the deployed portal. ``caps``
    defaults to ``caps.resolve(page, datasets, manifests)``; its ``layout`` (when set) is the
    pane tree. ``refs`` overrides DatasetRefs by dataset id and may add datasets that have no
    spec (for example a ``features`` resource). ``res_refs`` maps resource kinds
    (:data:`RES_KINDS`) to their published URLs. ``budget`` adds ``first_paint_bytes``;
    ``render.shell`` fills ``html_bytes``. A readme above 16 kB is written to ``files``
    (``{name: bytes}``) and referenced by URL; without ``files`` that is an error.
    """
    refs = dict(refs or {})
    known_prefixes = tuple(known_prefixes)
    if caps is None:
        extra = {r.get("kind") for k, r in refs.items() if k not in datasets and isinstance(r, Mapping)}
        caps = capsmod.resolve(page, dict(datasets), dict(manifests),
                               resources=[k for k in capsmod.SHARED_RESOURCES if k in extra])
    errs: list[str] = []
    pd = page_data(page, datasets, manifests)

    layout = caps.layout if caps.layout is not None else page.layout
    if layout is None:
        raise ConfigError(["no layout: the CapsResult has none and PageSpec.layout is None"], page.id)
    layout = _clean_layout(layout)
    enabled = list(caps.enabled)
    disabled = dict(caps.disabled)

    # datasets: page datasets, implied catalogs, layout targets, extra refs
    ds_ids = [k for k in pd.ids if not isinstance(datasets[k], ReadmeSpec)]
    for k in _used_datasets(layout):
        if k in datasets and k not in ds_ids and not isinstance(datasets[k], ReadmeSpec):
            ds_ids.append(k)
    for k in list(ds_ids):
        s = datasets[k]
        if isinstance(s, (RecordSetSpec, ImageSetSpec)) and s.catalog and s.catalog not in ds_ids:
            ds_ids.append(s.catalog)
    if any(k not in pd.ids for k in ds_ids):
        pd = page_data(PageSpec(id=page.id, archetype=page.archetype, title=page.title,
                                datasets=ds_ids, manifold=page.manifold), datasets, manifests)
    ds_refs: dict[str, dict] = {}
    for k in ds_ids:
        try:
            ds_refs[k] = dataset_ref(k, datasets[k], refs, res_refs)
        except ValueError as err:
            errs.append(str(err))
    for k, ref in refs.items():
        if k not in ds_refs and k not in datasets:
            ds_refs[k] = dict(ref)
    if page.archetype == "landing" and not any(r.get("kind") == "portal" for r in ds_refs.values()):
        pid = "portal" if "portal" not in ds_refs else "portal_registry"
        ds_refs[pid] = {"kind": "portal", "url": "portal.json"}

    # capabilities that need a document the page does not carry (IMPLEMENTATION_NOTES item 89)
    kinds = {r.get("kind") for r in ds_refs.values()}
    unmet = capsmod.unmet_needs(enabled, kinds)
    if unmet:
        enabled = [c for c in enabled if c not in unmet]
        disabled.update(unmet)
    before = set(capsmod.missing_providers(layout, enabled))
    layout = _prune_added(layout, caps.added_panes, enabled, unmet, kinds)
    if "extinction" not in kinds:
        # the resolver's shared-resource vector (caps.default_overlays) has no document here,
        # and extinction_vectors_on_ccd is off (IMPLEMENTATION_NOTES 110), so the overlays
        # document's reddening layers it listed go too; only resolver-built panes lose them
        # (an adapter's overlay list is validated as given)
        built = None if page.layout is None else {
            p.get("id", p.get("pane")) for p in caps.added_panes}
        listed = (capsmod.dim_vector_layers(pd)
                  if page.archetype in capsmod.REDDENING_LAYER_ARCHETYPES else [])
        for oid in [capsmod.SHARED_EXTINCTION_VECTOR] + listed:
            layout = _drop_overlay(layout, oid, built)
    for cap in capsmod.missing_providers(layout, enabled):
        if cap in before:
            continue
        needs = capsmod.PROVIDERS.get(cap) or ()
        disabled[cap] = ("the page has a single pane" if cap == "resizable_panels" else
                         f"no pane on this page can provide it (needs one of: {', '.join(needs)})")
        enabled.remove(cap)

    doc: dict[str, Any] = {
        "schema": "pk-page/1",
        "pk": pk,
        "assets": asset_base(pk, vendor_assets),
        "res": res_base(vendor_assets),
        "portal": {"id": portal.get("id"), "title": portal.get("title"),
                   "visibility": portal.get("visibility"), "nav": portal.get("nav", "portal.json")},
        "page": {"id": page.id, "archetype": page.archetype, "title": page.title},
        "datasets": ds_refs,
        "layout": layout,
        "state": copy.deepcopy(page.state),
        "caps": {"enabled": enabled, "disabled": disabled},
    }
    if page.subtitle:
        doc["page"]["subtitle"] = page.subtitle
    if page.legacy is not None:
        lg: dict[str, Any] = {"files": list(page.legacy.files)}
        if page.legacy.grammar:
            lg["grammar"] = list(page.legacy.grammar)
        if page.legacy.preset:
            lg["preset"] = page.legacy.preset
        presets = {k: v for k, v in (getattr(page.legacy, "presets", None) or {}).items()
                   if k in lg["files"]}
        if presets:
            lg["presets"] = presets
        alias = dict(getattr(page.legacy, "dim_alias", None) or {})
        if alias:
            errs.extend(_dim_alias_errors(alias, pd))
            lg["dim_alias"] = alias
        cmd_pane = getattr(page.legacy, "cmd_pane", None)
        if cmd_pane:
            errs.extend(_cmd_pane_errors(cmd_pane, layout))
            lg["cmd_pane"] = cmd_pane
        doc["page"]["legacy"] = lg
    if page.links:
        doc["links"] = dict(page.links)

    # readme
    rspecs = [datasets[k] for k in dict.fromkeys(page.datasets) if isinstance(datasets[k], ReadmeSpec)]
    cat_ids = [k for k, r in ds_refs.items() if r.get("kind") == "catalog"]
    prov = _provenance(pd, cat_ids)
    try:
        rhtml = "\n".join(readme_html(r) for r in rspecs)
    except (ValueError, OSError) as err:
        errs.append(f"readme: {err}")
        rhtml = ""
    if rspecs or prov or any(p.get("pane") in ("readme", "landing") for p in iter_panes(layout)):
        readme: dict[str, Any] = {}
        if len(rhtml.encode("utf-8")) > README_INLINE_MAX:
            name = readme_file_name(rhtml)
            if files is None:
                errs.append(f"readme: {len(rhtml.encode('utf-8'))} bytes exceeds the 16 kB inline "
                            f"limit; pass files={{}} to receive {name}")
            else:
                files[name] = rhtml.encode("utf-8")
            readme["url"] = name
        else:
            readme["html"] = rhtml
        if prov:
            readme["provenance"] = prov
        doc["readme"] = readme

    al = dict(aladin or {})
    doc["aladin"] = {"cdn": al.get("cdn", ALADIN_CDN), "mirror": al.get("mirror", ALADIN_MIRROR)}
    if budget is not None:
        doc["budget"] = {"html_bytes": int(budget.get("html_bytes", 0)),
                         "first_paint_bytes": int(budget.get("first_paint_bytes", 0))}

    # URLs
    public = portal.get("visibility") == "public"

    def fetched(where: str, url: Any) -> None:
        """A URL the page fetches: allowed, and outside the secure tree on a public page."""
        msg = check_url(url, known_prefixes)
        if msg:
            errs.append(f"{where}: {msg}")
        elif public and isinstance(url, str) and url.startswith(SECURE_PREFIX):
            errs.append(f"{where}: public page fetches {url!r} from the secure tree "
                        f"(section 11.2); publish the data publicly or make the portal secure")

    seen_urls: dict[str, str] = {}
    for k, ref in ds_refs.items():
        if ref.get("kind") == "records" and "base" in ref:
            u = ref.get("url")
            msg = check_foreign_url(u, known_prefixes)
            if msg:
                errs.append(f"datasets.{k}.url: {msg}")
            elif public and isinstance(u, str) and u.startswith(SECURE_PREFIX):
                errs.append(f"datasets.{k}.url: public page fetches foreign records {u!r} from "
                            f"the secure tree (section 11.2)")
        else:
            fetched(f"datasets.{k}.url", ref.get("url"))
        u = ref.get("url")
        if isinstance(u, str) and ref.get("kind") not in RES_KINDS:
            if u in seen_urls and ref.get("kind") != "isochrones":
                errs.append(f"datasets.{k}.url: {u!r} is also the URL of dataset "
                            f"{seen_urls[u]!r}; pass refs={{{k!r}: {{...}}}} for one of them")
            seen_urls.setdefault(u, k)
    if "readme" in doc and "url" in doc["readme"]:
        fetched("readme.url", doc["readme"]["url"])
    for k, v in (doc.get("links") or {}).items():
        msg = check_url(v, known_prefixes)
        if msg:
            errs.append(f"links.{k}: {msg}")
    for k, v in doc["aladin"].items():
        msg = check_url(v, known_prefixes)
        if msg:
            errs.append(f"aladin.{k}: {msg}")
    for leaf in iter_panes(layout):
        o = leaf.get("options") or {}
        for li, r in enumerate(o.get("raster") or []):
            if isinstance(r, Mapping):
                fetched(f"layout pane {leaf.get('id', leaf.get('pane'))!r} raster[{li}]", r.get("url"))
        if leaf.get("pane") == "readme" and "url" in o:
            fetched(f"layout pane {leaf.get('id', 'readme')!r} url", o["url"])

    # keys and providers
    errs += validate_keys(doc, pd)
    missing = capsmod.missing_providers(layout, doc["caps"]["enabled"])
    for cap in missing:
        need = capsmod.PROVIDERS.get(cap) or ()
        errs.append(f"capability {cap!r} is enabled but no pane provides it "
                    f"(needs one of: {', '.join(need) or 'two or more panes'})")
    unknown = [c for c in doc["caps"]["enabled"] if c not in capsmod.CAPS]
    errs += [f"caps.enabled: unknown capability {c!r}" for c in unknown]
    errs += [f"caps.disabled: unknown capability {c!r}" for c in doc["caps"]["disabled"]
             if c not in capsmod.CAPS]

    errs += validate_page(doc)
    if errs:
        raise ConfigError(errs, page.id)
    return doc


# ---------------------------------------------------------------------------
# small JSON documents
# ---------------------------------------------------------------------------

def _raise(errs: list[str], what: str) -> None:
    if errs:
        raise ConfigError([f"{what}: {e}" for e in errs])


def hips_document(spec: HipsSpec, *, default_target: Mapping | None = None) -> dict:
    """``pk-hips/1`` document for ``spec`` (validated)."""
    target = spec.target or default_target or {"ra": 266.4168, "dec": -29.0078, "fov": 180.0}
    doc: dict[str, Any] = {"schema": "pk-hips/1", "base": copy.deepcopy(spec.base),
                           "overlays": copy.deepcopy(spec.overlays), "fits": copy.deepcopy(spec.fits),
                           "target": dict(target)}
    if spec.rungs is not None:
        doc["rungs"] = copy.deepcopy(spec.rungs)
    if spec.bookmarks is not None:
        doc["bookmarks"] = copy.deepcopy(spec.bookmarks)
    _raise(validate_hips(doc), "hips.json")
    return doc


def overlays_document(spec: OverlaySpec) -> dict:
    """``pk-overlays/1`` document for ``spec`` (validated)."""
    doc = {"schema": "pk-overlays/1", "layers": copy.deepcopy(spec.layers)}
    _raise(validate_overlays(doc), "overlays.json")
    return doc


def narrative_document(spec: NarrativeSpec) -> dict:
    """``pk-narrative/1`` document; each beat's ``markdown`` becomes ``html`` via markdown_lite.

    Beats carry ``id``, ``markdown`` (or ``text``), ``view`` and optional ``title``. A beat
    with ready-made ``html`` is rejected, so every narrative passes the sanitizer.
    """
    beats = []
    for i, b in enumerate(spec.beats):
        if "html" in b:
            raise ConfigError([f"narrative beat {i}: give 'markdown', not 'html' "
                               "(beat HTML is produced by markdown_lite)"])
        md = b.get("markdown", b.get("text", ""))
        out = {"id": b.get("id", f"b{i + 1}"), "html": markdown_lite.render(str(md)),
               "view": copy.deepcopy(b.get("view", {"pane": "sky", "layers_on": [], "layers_off": []}))}
        if b.get("title"):
            out["title"] = str(b["title"])
        extra = set(b) - {"id", "markdown", "text", "view", "title"}
        if extra:
            raise ConfigError([f"narrative beat {i}: unknown keys {sorted(extra)}"])
        beats.append(out)
    doc = {"schema": "pk-narrative/1", "beats": beats}
    _raise(validate_narrative(doc), "narrative.json")
    return doc


def isochrones_document(spec: IsochroneSpec) -> dict:
    """``pk-isochrones/1`` document for ``spec`` (NaN becomes null; validated, ConfigError
    when invalid). Built by :func:`portalkit.physics.isochrones.build_document`."""
    from .physics.isochrones import build_document
    doc = build_document(spec)
    _raise(validate_isochrones(doc), "isochrones.json")
    return doc


def isochrones_file(spec: IsochroneSpec) -> tuple[str, bytes]:
    """(``iso.<h8>.json``, bytes) for ``spec``: compact UTF-8 JSON of
    :func:`isochrones_document` (the bytes ``bundle.writer.dumps_json`` produces), named by
    the first 8 hex digits of its sha256 (section 5.1). The default isochrones DatasetRef is
    ``data/<name>``; the build writes these bytes there."""
    data = json.dumps(isochrones_document(spec), ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")
    return f"iso.{hashlib.sha256(data).hexdigest()[:8]}.json", data
