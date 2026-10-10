"""Capability resolver (ARCHITECTURE.md section 8.1).

``resolve(page, datasets, manifests)`` derives the data flags of a page from the datasets
attached to it, applies the union rules of section 8.1, removes the adapter's opt-outs
(``PageSpec.caps_disable``), and inserts collapsed panes so that every enabled capability
has a mounted provider (section 8.2). The returned :class:`CapsResult` carries the final
pane tree in ``layout`` (an addition to the section 8.1 fields, used by ``config.assemble``).

The pane tree starts from ``PageSpec.layout`` when the adapter gives one, else from the
archetype's default tree (section 3), which holds only the archetype's core panes. Added
panes go to their archetype slot in a default tree, or to the last column of an adapter
tree.

``manifests`` maps dataset ids to written documents (catalog manifests, records indexes,
hips.json, ...). They take precedence over the specs, so a foreign (GC) catalog without a
table resolves from its manifest.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping

from .constants import T0_MAX
from .expr import ExprError, compile as compile_expr
from .spec import (NUMERIC_ROLES, CatalogSpec, DatasetSpec, HipsSpec, ImageSetSpec,
                   IsochroneSpec, NarrativeSpec, OverlaySpec, PageSpec, RecordSetSpec, TemplateSpec)

__all__ = [
    "CAPS", "FLAGS", "ALWAYS", "RULES", "PROVIDERS", "ADD_PANES", "TEMPLATES", "CORE",
    "CapsResult", "CatInfo", "PageData", "resolve", "catalog_info", "page_data", "derive_flags",
    "caps_from_flags", "iter_panes", "pane_types", "missing_providers", "default_axes",
    "insert_panes", "default_layout", "manifold_group",
]

#: The 39 capabilities of section 8.2, in table order.
CAPS: tuple[str, ...] = (
    "keyboard_nav", "cross_page_nav", "responsive_mobile", "lightbox", "detail_panel",
    "filter_toggles", "deep_link_hash", "hover_preview", "static_sed_png", "cmd_ccd_scatter",
    "class_coloring", "source_list_search", "lazy_data_loading", "brush_rect", "brush_circle",
    "brush_lasso", "brush_set_ops", "selectable_axes", "readme_provenance", "sky_map_aladin",
    "catalog_markers_on_sky", "cdn_fallback", "hips_layer_stacking", "fits_overlay",
    "density_rendering", "linked_selection", "manifold_embedding", "av_slider",
    "extinction_vectors_on_ccd", "isochrones_or_tracks", "model_template_overlay",
    "labeled_features_overlay", "spectrum_viewer_1d", "spectrum_2d_viewer", "live_sed_viewer",
    "collapsible_panels", "resizable_panels", "dark_light_theme", "selection_export",
)

#: Data flags of section 8.1, in table order.
FLAGS: tuple[str, ...] = ("multi", "cat", "cats", "flags", "num2", "mags2", "num3", "mani",
                          "mags3f", "sky", "skycat", "img", "sedimg", "spec", "spec2d", "iso",
                          "fits", "dens", "items", "annot", "search")

ALWAYS: tuple[str, ...] = ("keyboard_nav", "cross_page_nav", "responsive_mobile", "deep_link_hash",
                           "readme_provenance", "dark_light_theme", "collapsible_panels",
                           "lazy_data_loading")

RULES: dict[str, tuple[str, ...]] = {
    "multi": ("resizable_panels", "linked_selection"),
    "cat": ("detail_panel", "source_list_search", "hover_preview", "selection_export"),
    "search": ("source_list_search",),
    "cats": ("class_coloring", "filter_toggles"),
    "flags": ("filter_toggles",),
    "num2": ("brush_rect", "brush_circle", "brush_lasso", "brush_set_ops", "selection_export"),
    "mags2": ("cmd_ccd_scatter",),
    "num3": ("selectable_axes",),
    "mani": ("manifold_embedding", "selectable_axes"),
    "mags3f": ("live_sed_viewer", "extinction_vectors_on_ccd", "av_slider", "model_template_overlay",
               "labeled_features_overlay"),
    "sky": ("sky_map_aladin", "cdn_fallback", "hips_layer_stacking"),
    "skycat": ("catalog_markers_on_sky",),
    "img": ("lightbox",),
    "sedimg": ("static_sed_png",),
    "spec": ("spectrum_viewer_1d", "labeled_features_overlay", "model_template_overlay", "av_slider"),
    "spec2d": ("spectrum_2d_viewer",),
    "iso": ("isochrones_or_tracks",),
    "fits": ("fits_overlay",),
    "dens": ("density_rendering",),
    "items": ("source_list_search", "filter_toggles", "hover_preview"),
    "annot": ("selection_export",),
}

_PLOTS = ("scatter", "manifold", "sky")

#: Pane types that provide each capability (section 8.2). An empty tuple means a page-level
#: module (keys, nav, hash, theme, layout, hub) that every page mounts.
PROVIDERS: dict[str, tuple[str, ...]] = {
    "keyboard_nav": (), "cross_page_nav": (), "responsive_mobile": (), "deep_link_hash": (),
    "dark_light_theme": (), "collapsible_panels": (), "lazy_data_loading": (),
    "resizable_panels": (), "linked_selection": (),
    "readme_provenance": ("readme", "landing"),
    "lightbox": ("gallery", "detail"),
    "detail_panel": ("detail",),
    "filter_toggles": ("list", "scatter", "manifold", "sky", "gallery", "hist"),
    "hover_preview": ("list", "scatter", "manifold", "sky", "gallery"),
    "static_sed_png": ("detail", "gallery"),
    "cmd_ccd_scatter": ("scatter",),
    "class_coloring": ("list", "scatter", "manifold", "sky", "gallery"),
    "source_list_search": ("list", "landing", "gallery"),
    "brush_rect": _PLOTS, "brush_circle": _PLOTS, "brush_lasso": _PLOTS,
    "brush_set_ops": _PLOTS,
    "selectable_axes": ("scatter", "manifold"),
    "sky_map_aladin": ("sky", "wavelength"),
    "catalog_markers_on_sky": ("sky", "wavelength"),
    "cdn_fallback": ("sky", "wavelength"),
    "hips_layer_stacking": ("sky", "wavelength"),
    "fits_overlay": ("sky",),
    "density_rendering": _PLOTS,
    "manifold_embedding": ("manifold",),
    "av_slider": ("scatter", "manifold", "sed", "spectrum1d"),
    "extinction_vectors_on_ccd": ("scatter", "manifold"),
    "isochrones_or_tracks": ("scatter", "manifold"),
    "model_template_overlay": ("spectrum1d", "sed"),
    "labeled_features_overlay": ("spectrum1d", "sed"),
    "spectrum_viewer_1d": ("spectrum1d",),
    "spectrum_2d_viewer": ("spectrum2d",),
    "live_sed_viewer": ("sed",),
    "selection_export": ("list", "scatter", "manifold", "sky", "gallery", "table"),
}

#: Pane types the resolver tries (in order) when an enabled capability has no provider.
ADD_PANES: dict[str, tuple[str, ...]] = {
    "readme_provenance": ("readme",),
    "lightbox": ("detail", "gallery"),
    "detail_panel": ("detail",),
    "filter_toggles": ("list", "gallery", "scatter", "sky"),
    "hover_preview": ("list", "gallery", "scatter", "sky"),
    "static_sed_png": ("detail", "gallery"),
    "cmd_ccd_scatter": ("scatter",),
    "class_coloring": ("list", "scatter", "sky"),
    "source_list_search": ("list", "gallery"),
    "brush_rect": ("scatter", "sky"), "brush_circle": ("scatter", "sky"),
    "brush_lasso": ("scatter", "sky"), "brush_set_ops": ("scatter", "sky"),
    "selectable_axes": ("scatter", "manifold"),
    "sky_map_aladin": ("sky",), "catalog_markers_on_sky": ("sky",), "cdn_fallback": ("sky",),
    "hips_layer_stacking": ("sky",), "fits_overlay": ("sky",),
    "density_rendering": ("scatter", "sky"),
    "manifold_embedding": ("manifold",),
    "av_slider": ("sed", "spectrum1d", "scatter"),
    "extinction_vectors_on_ccd": ("scatter",),
    "isochrones_or_tracks": ("scatter",),
    "model_template_overlay": ("spectrum1d", "sed"),
    "labeled_features_overlay": ("spectrum1d", "sed"),
    "spectrum_viewer_1d": ("spectrum1d",),
    "spectrum_2d_viewer": ("spectrum2d",),
    "live_sed_viewer": ("sed",),
    "selection_export": ("list", "scatter", "sky", "gallery"),
}

#: Default trees (section 3) as columns of pane slots; a pane type not listed goes to the
#: last column. Empty columns are dropped; a one-pane column collapses to the leaf.
TEMPLATES: dict[str, list[list[str]]] = {
    "landing": [["landing"], ["readme"]],
    "explorer": [["list"], ["sky", "scatter", "manifold", "hist"],
                 ["detail", "spectrum1d", "spectrum2d", "sed", "gallery", "table", "readme"]],
    "manifold": [["list"], ["manifold", "scatter", "hist"],
                 ["detail", "sed", "spectrum1d", "spectrum2d", "sky", "gallery", "table", "readme"]],
    "gallery": [["list"], ["gallery"],
                ["detail", "scatter", "sky", "spectrum1d", "spectrum2d", "sed", "hist", "table", "readme"]],
    "spectra": [["list"], ["spectrum1d", "spectrum2d"],
                ["detail", "sed", "sky", "scatter", "manifold", "gallery", "table", "hist", "readme"]],
    "wavelength": [["wavelength"], ["sky", "list", "detail", "scatter", "readme"]],
    "narrative": [["narrative"], ["sky", "scatter"], ["list", "detail", "readme"]],
}

#: Core panes per archetype (section 3). "scatter|sky" means sky when the primary catalog
#: has positions, else scatter. Panes that cannot be built (no suitable dataset) are skipped.
CORE: dict[str, tuple[str, ...]] = {
    "landing": ("landing", "readme"),
    "explorer": ("list", "sky|scatter", "detail"),
    "manifold": ("list", "manifold", "detail"),
    "gallery": ("gallery",),
    "spectra": ("list", "spectrum1d"),
    "wavelength": ("wavelength",),
    "narrative": ("narrative", "sky|scatter"),
}

_SLOT_SIZE = {0: 250, 2: 430}   # px sizes of the first and third template columns


@dataclass
class CapsResult:
    enabled: list[str]            # sorted
    disabled: dict[str, str]      # cap -> reason (opt-outs and unmet runtime needs)
    flags: list[str]              # data flags derived below (for the build report)
    added_panes: list[dict]       # collapsed panes the resolver inserted into the layout
    layout: dict | None = None    # final pane tree (adapter or archetype tree + added panes)


# ---------------------------------------------------------------------------
# dataset summaries
# ---------------------------------------------------------------------------

#: column roles whose values may appear in linear expressions
EXPR_ROLES = ("mag", "flux", "error", "color", "value")


@dataclass
class CatInfo:
    """Column summary of one catalog, from its manifest (preferred) or its spec."""
    ds: str
    n: int | None = None
    context: bool = False
    has_id: bool = False
    has_pos: bool = False
    cols: dict[str, dict] = field(default_factory=dict)    # key -> {role, filter, enc, levels}
    virtual: list[str] = field(default_factory=list)
    groups: dict[str, list[str]] = field(default_factory=dict)
    presets: list[dict] = field(default_factory=list)      # manifest-shaped presets
    manifold_dims: list[str] | None = None
    frames: set[str] = field(default_factory=set)          # named frames (presets, manifold.frames)
    extra_keys: set[str] = field(default_factory=set)      # id/ra/dec/row-chunk fields

    def role_keys(self, *roles: str) -> list[str]:
        return [k for k, c in self.cols.items() if c.get("role") in roles]

    @property
    def numeric(self) -> list[str]:
        """Numeric dims: stored mag/flux/color/value columns, then virtual dims."""
        return self.role_keys(*NUMERIC_ROLES) + [v for v in self.virtual if v not in self.cols]

    @property
    def expr_keys(self) -> set[str]:
        """Stored keys usable inside linear expressions."""
        return set(self.role_keys(*EXPR_ROLES))

    @property
    def all_keys(self) -> set[str]:
        return set(self.cols) | set(self.virtual) | self.extra_keys

    def resolves(self, expr: str, numeric: bool = True) -> str | None:
        """None when ``expr`` is a known key or a linear expression over numeric stored
        columns of this catalog; otherwise an error message."""
        if not isinstance(expr, str) or not expr:
            return f"expected a column key or expression, got {expr!r}"
        if expr in self.virtual or expr in self.expr_keys:
            return None
        if not numeric and expr in self.all_keys:
            return None
        try:
            comp = compile_expr(expr)
        except ExprError as err:
            if expr in self.all_keys:
                return f"column {expr!r} of catalog {self.ds!r} is not numeric"
            return f"{expr!r} is neither a column of catalog {self.ds!r} nor a valid expression ({err})"
        bad = [k for k in comp["keys"] if k not in self.expr_keys]
        if bad:
            what = ", ".join(repr(k) for k in bad)
            if any(k in self.all_keys for k in bad):
                return f"expression {expr!r} uses non-numeric column(s) {what} of catalog {self.ds!r}"
            return f"expression {expr!r} uses unknown column(s) {what} (catalog {self.ds!r})"
        return None


def catalog_info(ds: str, spec: CatalogSpec | None, manifest: Mapping | None = None) -> CatInfo:
    """Summarise a catalog for flag derivation and key validation."""
    info = CatInfo(ds=ds)
    if spec is not None:
        info.context = bool(spec.context)
    if manifest:
        info.n = manifest.get("n") if isinstance(manifest.get("n"), int) else None
        for c in manifest.get("cols") or []:
            if isinstance(c, dict) and isinstance(c.get("key"), str):
                info.cols[c["key"]] = {"role": c.get("role", "value"), "filter": c.get("filter"),
                                       "enc": c.get("enc"), "levels": c.get("levels")}
        info.virtual = [v["key"] for v in manifest.get("virtual") or []
                        if isinstance(v, dict) and isinstance(v.get("key"), str)]
        info.groups = {g["key"]: list(g.get("dims") or []) for g in manifest.get("groups") or []
                       if isinstance(g, dict) and isinstance(g.get("key"), str)}
        info.presets = [p for p in manifest.get("presets") or [] if isinstance(p, dict)]
        man = manifest.get("manifold")
        if isinstance(man, dict) and isinstance(man.get("dims"), list):
            info.manifold_dims = list(man["dims"])
        if isinstance(man, dict) and isinstance(man.get("frames"), dict):
            info.frames |= {k for k in man["frames"] if isinstance(k, str)}
        info.frames |= {p["key"] for p in info.presets
                        if p.get("kind") == "frame" and isinstance(p.get("key"), str)}
        info.has_id = manifest.get("ids") is not None or bool(info.role_keys("id"))
        info.has_pos = manifest.get("pos") is not None or bool(info.role_keys("ra") and info.role_keys("dec"))
        rows = manifest.get("rows")
        if isinstance(rows, dict):
            info.extra_keys |= {k for k in rows.get("cols") or [] if isinstance(k, str)}
        if info.has_id:
            info.extra_keys.add("id")
        if info.has_pos:
            info.extra_keys |= {"ra", "dec"}
        if spec is not None:
            info.extra_keys |= {k for k in (spec.id, spec.ra, spec.dec) if k}
        return info
    if spec is None:
        return info
    info.n = len(spec.table) if spec.table is not None else None
    for c in spec.columns:
        info.cols[c.key] = {"role": c.role, "filter": c.filter, "enc": c.enc, "levels": c.levels}
    info.virtual = [v.key for v in spec.virtual]
    info.groups = {g.key: list(g.dims) for g in spec.groups}
    info.presets = [{"key": p.key, "label": p.label, "kind": p.kind, **(p.data or {})}
                    for p in spec.presets]
    if spec.manifold is not None:
        info.manifold_dims = list(spec.manifold.dims)
        info.frames |= set(spec.manifold.frames)
    info.frames |= {p["key"] for p in info.presets if p.get("kind") == "frame"}
    info.has_id = bool(spec.id)
    info.has_pos = bool(spec.ra and spec.dec)
    info.extra_keys |= {k for k in (spec.id, spec.ra, spec.dec) if k}
    if info.has_id:
        info.extra_keys.add("id")
    if info.has_pos:
        info.extra_keys |= {"ra", "dec"}
    if spec.table is not None:
        info.extra_keys |= set(spec.table.colnames)
    return info


@dataclass
class PageData:
    """Datasets attached to one page, grouped by kind."""
    page: PageSpec
    ids: list[str]                                  # page datasets incl. implied catalogs
    specs: dict[str, DatasetSpec]
    manifests: dict[str, Any]
    cats: dict[str, CatInfo]                        # every catalog on the page
    all_specs: dict[str, DatasetSpec]

    def of(self, *types: type) -> list[str]:
        return [k for k in self.ids if isinstance(self.specs[k], types)]

    @property
    def primary(self) -> str | None:
        """First non-context catalog on the page (records/images catalogs count)."""
        for k in self.ids:
            if k in self.cats and not self.cats[k].context:
                return k
        return None

    @property
    def context_cats(self) -> list[str]:
        return [k for k in self.ids if k in self.cats and self.cats[k].context]

    def records(self, *kinds: str) -> list[str]:
        out = []
        for k in self.of(RecordSetSpec):
            s = self.specs[k]
            m = self.manifests.get(k) if isinstance(self.manifests.get(k), dict) else {}
            kind = m.get("kind", s.kind)
            if kind in kinds or ("json-legacy" in kinds and s.legacy_json is not None):
                out.append(k)
        return out

    def first(self, *types: type) -> str | None:
        got = self.of(*types)
        return got[0] if got else None


def page_data(page: PageSpec, datasets: Mapping[str, DatasetSpec],
              manifests: Mapping[str, Any] | None = None) -> PageData:
    """Collect the page's datasets; catalogs referenced by its record and image sets are
    attached implicitly (after the explicit ones)."""
    manifests = dict(manifests or {})
    ids: list[str] = []
    for k in page.datasets:
        if k not in datasets:
            raise KeyError(f"page {page.id!r} uses unknown dataset {k!r}")
        if k not in ids:
            ids.append(k)
    for k in list(ids):
        s = datasets[k]
        cat = s.catalog if isinstance(s, (RecordSetSpec, ImageSetSpec)) else None
        if cat:
            if cat not in datasets:
                raise KeyError(f"dataset {k!r} references unknown catalog {cat!r}")
            if not isinstance(datasets[cat], CatalogSpec):
                raise TypeError(f"dataset {k!r} references {cat!r}, which is not a catalog")
            if cat not in ids:
                ids.append(cat)
    specs = {k: datasets[k] for k in ids}
    cats = {k: catalog_info(k, s, manifests.get(k)) for k, s in specs.items()
            if isinstance(s, CatalogSpec)}
    return PageData(page=page, ids=ids, specs=specs, manifests=manifests, cats=cats,
                    all_specs=dict(datasets))


# ---------------------------------------------------------------------------
# layout helpers
# ---------------------------------------------------------------------------

def iter_panes(node: Any) -> Iterator[dict]:
    """Yield every pane leaf of a layout tree in tree order."""
    if not isinstance(node, dict):
        return
    if "pane" in node:
        yield node
        return
    for k in ("row", "col"):
        for child in node.get(k) or []:
            yield from iter_panes(child)


def pane_types(node: Any) -> set[str]:
    return {p.get("pane") for p in iter_panes(node)}


def _pane_ids(node: Any) -> set[str]:
    return {p.get("id", p.get("pane")) for p in iter_panes(node)}


def _unique_id(ptype: str, taken: set[str]) -> str:
    if ptype not in taken:
        taken.add(ptype)
        return ptype
    k = 2
    while f"{ptype}{k}" in taken:
        k += 1
    taken.add(f"{ptype}{k}")
    return f"{ptype}{k}"


def default_axes(info: CatInfo) -> tuple[str, str] | None:
    """Default scatter axes: first ``axes`` preset; else a CMD over the first and last
    ``mag`` columns (``m0-mN`` vs ``mN``, short-minus-long in column order); else the first
    two numeric dims."""
    for p in info.presets:
        if p.get("kind") == "axes" and isinstance(p.get("x"), str) and isinstance(p.get("y"), str):
            return p["x"], p["y"]
    mags = info.role_keys("mag")
    if len(mags) >= 2:
        return f"{mags[0]}-{mags[-1]}", mags[-1]
    num = info.numeric
    if len(num) >= 2:
        return num[0], num[1]
    return None


def _sky_layer_ids(pd: PageData) -> list[str]:
    out: list[str] = []
    for k in pd.of(OverlaySpec):
        m = pd.manifests.get(k)
        layers = m.get("layers") if isinstance(m, dict) else pd.specs[k].layers
        for lay in layers or []:
            if isinstance(lay, dict) and lay.get("space", "sky") == "sky" and isinstance(lay.get("id"), str):
                out.append(lay["id"])
    return out


def manifold_group(info: CatInfo) -> str | None:
    """First catalog group with 3 or more dims, all numeric, or None."""
    for g, dims in info.groups.items():
        if len(dims) >= 3 and all(info.resolves(d) is None for d in dims):
            return g
    return None


def _make_pane(ptype: str, pd: PageData) -> dict | None:
    """Build a pane leaf of ``ptype`` for the page, or None when the data cannot feed it."""
    prim = pd.primary
    info = pd.cats.get(prim) if prim else None
    leaf: dict[str, Any] = {"pane": ptype}
    opts: dict[str, Any] = {}
    if ptype == "list":
        if prim is None:
            return None
        leaf["dataset"] = prim
    elif ptype == "detail":
        if prim is None:
            return None
        leaf["dataset"] = prim
        prods = [k for k in pd.records("json") if pd.specs[k].catalog == prim]
        if prods:
            opts["products"] = prods[0]
        imgs = [k for k in pd.of(ImageSetSpec) if pd.specs[k].catalog == prim]
        if imgs:
            opts["images"] = imgs[0]
    elif ptype in ("scatter", "hist"):
        if info is None or len(info.numeric) < (2 if ptype == "scatter" else 1):
            return None
        leaf["dataset"] = prim
        if ptype == "hist":
            opts["expr"] = info.numeric[0]
        else:
            ax = default_axes(info)
            if ax is not None:
                opts["x"], opts["y"] = ax
                for c in pd.context_cats:
                    ci = pd.cats[c]
                    if ci.resolves(ax[0]) is None and ci.resolves(ax[1]) is None:
                        opts["background"] = c
                        break
    elif ptype == "manifold":
        if info is None or (len(info.numeric) < 3 and not info.manifold_dims):
            return None
        leaf["dataset"] = prim
        dims = info.manifold_dims
        if not dims:
            grp = manifold_group(info)
            if grp is not None:
                opts["dims"] = f"group:{grp}"
                dims = info.groups[grp]
            else:
                opts["dims"] = dims = list(info.numeric)
        for c in pd.context_cats:
            if all(pd.cats[c].resolves(d) is None for d in dims):
                opts["background"] = c
                break
    elif ptype == "sky":
        hips = pd.first(HipsSpec)
        if info is not None and info.has_pos:
            leaf["dataset"] = prim
        elif hips is None:
            return None
        if hips is not None:
            opts["hips"] = hips
        lays = _sky_layer_ids(pd)
        if lays:
            opts["overlays"] = lays
    elif ptype == "wavelength":
        hips = pd.first(HipsSpec)
        if hips is None:
            return None
        opts["hips"] = hips
        if info is not None and info.has_pos:
            opts["markers"] = prim
    elif ptype == "narrative":
        nar = pd.first(NarrativeSpec)
        if nar is None:
            return None
        opts["narrative"] = nar
        opts["sticky"] = "sky" if (pd.first(HipsSpec) or (info is not None and info.has_pos)) else "scatter"
    elif ptype == "spectrum1d":
        recs = pd.records("pks1", "json-legacy")
        if not recs:
            return None
        leaf["dataset"] = recs[0]
        opts["records"] = recs[0]
        tpl = pd.first(TemplateSpec)
        if tpl is not None:
            opts["templates"] = tpl
    elif ptype == "spectrum2d":
        recs = pd.records("pki1")
        if not recs:
            return None
        leaf["dataset"] = recs[0]
        opts["records"] = recs[0]
    elif ptype == "sed":
        if info is None or not info.role_keys("mag", "flux"):
            return None
        leaf["dataset"] = prim
        curves = pd.records("sedcurve")
        if curves:
            opts["models"] = ["template", "blackbody"] + [f"sedcurve:{c}" for c in curves]
    elif ptype == "gallery":
        img = pd.first(ImageSetSpec)
        if img is None:
            return None
        leaf["dataset"] = img
        opts["images"] = img
    elif ptype in ("landing", "readme"):
        pass
    elif ptype == "table":
        if prim is None:
            return None
        leaf["dataset"] = prim
    else:
        raise ValueError(f"unknown pane type {ptype!r}")
    if opts:
        leaf["options"] = opts
    return leaf


def _core_panes(pd: PageData) -> list[dict]:
    out: list[dict] = []
    for slot in CORE[pd.page.archetype]:
        if slot == "sky|scatter":
            info = pd.cats.get(pd.primary) if pd.primary else None
            order = ("sky", "scatter") if (info is not None and info.has_pos) or pd.first(HipsSpec) \
                else ("scatter", "sky")
            for t in order:
                leaf = _make_pane(t, pd)
                if leaf is not None:
                    out.append(leaf)
                    break
            continue
        leaf = _make_pane(slot, pd)
        if leaf is not None:
            out.append(leaf)
    return out


def _slot_of(archetype: str, ptype: str) -> int:
    cols = TEMPLATES[archetype]
    for i, col in enumerate(cols):
        if ptype in col:
            return i
    return len(cols) - 1


def default_layout(archetype: str, leaves: list[dict]) -> dict:
    """Place pane leaves into the archetype's column template (section 3)."""
    cols = TEMPLATES[archetype]
    buckets: list[list[dict]] = [[] for _ in cols]
    for leaf in leaves:
        buckets[_slot_of(archetype, leaf["pane"])].append(leaf)
    for i, b in enumerate(buckets):
        order = cols[i]
        b.sort(key=lambda lf: order.index(lf["pane"]) if lf["pane"] in order else len(order))
    nodes: list[dict] = []
    multi_col = sum(1 for b in buckets if b) > 1
    for i, b in enumerate(buckets):
        if not b:
            continue
        node = b[0] if len(b) == 1 else {"col": b}
        if multi_col and i in _SLOT_SIZE and len(cols) == 3 and cols[0] == ["list"]:
            node["size"] = _SLOT_SIZE[i]
        nodes.append(node)
    if not nodes:
        return {"pane": "readme"}
    if len(nodes) == 1:
        return nodes[0]
    return {"row": nodes}


def insert_panes(tree: dict, leaves: list[dict]) -> dict:
    """Append pane leaves to an adapter-supplied tree: into the root row's last column, or a
    new trailing column (wrapping a non-row root in a row)."""
    if not leaves:
        return tree
    tree = copy.deepcopy(tree)
    if "row" in tree:
        last = tree["row"][-1] if tree["row"] else None
        if isinstance(last, dict) and "col" in last:
            last["col"].extend(leaves)
        else:
            tree["row"].append(leaves[0] if len(leaves) == 1 else {"col": list(leaves)})
        return tree
    return {"row": [tree, leaves[0] if len(leaves) == 1 else {"col": list(leaves)}]}


# ---------------------------------------------------------------------------
# flags and rules
# ---------------------------------------------------------------------------

def _pane_effective_ds(leaf: dict, pd: PageData) -> set[str]:
    out: set[str] = set()
    names = [leaf.get("dataset")]
    opts = leaf.get("options") or {}
    for k in ("records", "images", "products", "markers"):
        names.append(opts.get(k))
    for n in names:
        if not isinstance(n, str) or n not in pd.all_specs:
            continue
        s = pd.all_specs[n]
        if isinstance(s, CatalogSpec):
            if not s.context:
                out.add(n)
        elif isinstance(s, (RecordSetSpec, ImageSetSpec)):
            out.add(s.catalog if s.catalog else n)
    return out


def derive_flags(pd: PageData, tree: dict | None) -> list[str]:
    """Data flags of section 8.1 for the page and pane tree, in :data:`FLAGS` order."""
    page = pd.page
    f: set[str] = set()
    fg = [pd.cats[k] for k in pd.ids if k in pd.cats and not pd.cats[k].context]
    leaves = list(iter_panes(tree)) if tree else []
    counts: dict[str, int] = {}
    for leaf in leaves:
        for d in _pane_effective_ds(leaf, pd):
            counts[d] = counts.get(d, 0) + 1
    if any(v >= 2 for v in counts.values()):
        f.add("multi")
    if any(c.has_id for c in fg):
        f.add("cat")
    if any(c.role_keys("category") for c in fg):
        f.add("cats")
    if any(c.role_keys("flag", "error") for c in fg):
        f.add("flags")
    nnum = max((len(c.numeric) for c in fg), default=0)
    nmag = max((len(c.role_keys("mag", "flux")) for c in fg), default=0)
    nmagf = max((sum(1 for k in c.role_keys("mag", "flux") if c.cols[k].get("filter")) for c in fg),
                default=0)
    if nnum >= 2:
        f.add("num2")
    if nmag >= 2:
        f.add("mags2")
    if nnum >= 3:
        f.add("num3")
    if page.archetype == "manifold" or page.manifold:
        f.add("mani")
    if nmagf >= 3:
        f.add("mags3f")
    hips_ids = pd.of(HipsSpec)
    if any(c.has_pos for c in fg) or hips_ids:
        f.add("sky")
    types = {leaf.get("pane") for leaf in leaves}
    has_sky_pane = "sky" in types or any(
        leaf.get("pane") == "wavelength" and (leaf.get("options") or {}).get("markers") for leaf in leaves)
    if has_sky_pane and any(c.has_pos for c in fg):
        f.add("skycat")
    img_sets = pd.of(ImageSetSpec)
    if img_sets or any(c.role_keys("image") for c in fg):
        f.add("img")
    sed_role = False
    for k in img_sets:
        m = pd.manifests.get(k)
        roles = [r.get("key") for r in m.get("roles", []) if isinstance(r, dict)] \
            if isinstance(m, dict) else [r[0] for r in pd.specs[k].roles]
        if "sed" in roles:
            sed_role = True
    if any("sed" in c.role_keys("image") for c in fg):
        sed_role = True
    if sed_role:
        f.add("sedimg")
    if pd.records("pks1", "json-legacy"):
        f.add("spec")
    spec2d = bool(pd.records("pki1")) or any(
        pd.specs[k].png is not None for k in pd.of(RecordSetSpec))
    if spec2d:
        f.add("spec2d")
    if pd.of(IsochroneSpec):
        f.add("iso")
    for k in hips_ids:
        m = pd.manifests.get(k)
        fits = m.get("fits") if isinstance(m, dict) else pd.specs[k].fits
        if fits:
            f.add("fits")
    if any((c.n or 0) > T0_MAX for c in fg) or pd.context_cats:
        f.add("dens")
    if page.archetype == "gallery":
        f.add("items")
    if any(leaf.get("pane") == "gallery" and (leaf.get("options") or {}).get("annotate")
           for leaf in leaves):
        f.add("annot")
    if page.archetype == "landing" and any(
            isinstance(s, CatalogSpec) and not s.context for s in pd.all_specs.values()):
        f.add("search")
    return [x for x in FLAGS if x in f]


def caps_from_flags(flags: list[str] | set[str]) -> set[str]:
    """Union of :data:`ALWAYS` and the rules of every flag."""
    out = set(ALWAYS)
    for fl in flags:
        if fl not in RULES:
            raise KeyError(f"unknown flag {fl!r}")
        out.update(RULES[fl])
    return out


def missing_providers(tree: dict | None, enabled: list[str] | set[str]) -> list[str]:
    """Enabled caps without a provider pane in ``tree`` (page-level caps always pass, except
    ``resizable_panels``, which needs at least two panes)."""
    types = pane_types(tree) if tree else set()
    n = sum(1 for _ in iter_panes(tree)) if tree else 0
    out = []
    for cap in enabled:
        prov = PROVIDERS.get(cap)
        if prov is None:
            out.append(cap)
        elif cap == "resizable_panels":
            if n < 2:
                out.append(cap)
        elif prov and not types.intersection(prov):
            out.append(cap)
    return out


def resolve(page: PageSpec, datasets: dict[str, DatasetSpec], manifests: dict[str, dict]) -> CapsResult:
    """Resolve the capabilities of ``page`` (section 8.1) and the pane tree that hosts them."""
    if page.archetype not in TEMPLATES:
        raise ValueError(f"page {page.id!r}: unknown archetype {page.archetype!r}")
    for cap, reason in page.caps_disable.items():
        if cap not in CAPS:
            raise ValueError(f"page {page.id!r}: caps_disable names unknown capability {cap!r}")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"page {page.id!r}: caps_disable[{cap!r}] needs a reason")
    pd = page_data(page, datasets, manifests)
    explicit = page.layout is not None
    base = [] if explicit else _core_panes(pd)
    added: list[dict] = []
    taken: set[str] = _pane_ids(page.layout) if explicit else set()
    for leaf in base:
        leaf["id"] = _unique_id(leaf["pane"], taken)
        if leaf["id"] == leaf["pane"]:
            del leaf["id"]

    def build() -> dict:
        if explicit:
            return insert_panes(page.layout, added)
        return default_layout(page.archetype, [copy.deepcopy(x) for x in base + added])

    tree = build()
    flags: list[str] = []
    enabled: set[str] = set()
    for _ in range(8):
        flags = derive_flags(pd, tree)
        enabled = caps_from_flags(flags) - set(page.caps_disable)
        new: list[dict] = []
        have = pane_types(tree)
        for cap in sorted(enabled, key=CAPS.index):
            if cap not in missing_providers(tree, [cap]):
                continue
            if have.intersection(PROVIDERS[cap]) or any(n["pane"] in PROVIDERS[cap] for n in new):
                continue
            for ptype in ADD_PANES.get(cap, ()):
                leaf = _make_pane(ptype, pd)
                if leaf is None:
                    continue
                leaf["collapsed"] = True
                leaf["id"] = _unique_id(ptype, taken)
                new.append(leaf)
                break
        if not new:
            break
        added.extend(new)
        tree = build()
    disabled = dict(page.caps_disable)
    for cap in missing_providers(tree, enabled):
        needs = PROVIDERS.get(cap) or ()
        if cap == "resizable_panels":
            reason = "the page has a single pane"
        else:
            reason = f"no pane on this page can provide it (needs one of: {', '.join(needs)})"
        disabled[cap] = reason
        enabled.discard(cap)
    return CapsResult(enabled=sorted(enabled), disabled=disabled, flags=flags,
                      added_panes=copy.deepcopy(added), layout=tree)
