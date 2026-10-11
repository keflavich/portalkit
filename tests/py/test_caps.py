"""portalkit.caps: flags, rules, providers and added panes (ARCHITECTURE.md section 8)."""

from __future__ import annotations

import copy

import pytest

from portalkit import caps
from portalkit.constants import T0_MAX
from portalkit.spec import (ColumnSpec, ExtinctionSpec, FilterSetSpec, ForeignRef, HipsSpec,
                            ImageItem, ImageSetSpec, IsochroneSpec, ManifoldSpec, NarrativeSpec,
                            PageSpec, Preset, RecordSetSpec, Track, catalog_from_rows)

import numpy as np


def _cat(n=10, key="src", mags=("f182m", "f405n", "f410m"), pos=True, cls=True, context=False, extra=None):
    rows = []
    for i in range(n):
        r = {"name": f"s{i}"}
        if pos:
            r["ra"] = 266.0 + 0.001 * i
            r["dec"] = -29.0
        for j, m in enumerate(mags):
            r[m] = 14.0 + 0.1 * i + j
        if cls:
            r["cls"] = "a" if i % 2 else "b"
        r.update(extra or {})
        rows.append(r)
    roles = {m: "mag" for m in mags}
    if cls:
        roles["cls"] = "category"
    cols = {m: {"filter": f"JWST/NIRCam.{m.upper()}"} for m in mags}
    return catalog_from_rows(rows, id="name", ra="ra" if pos else None, dec="dec" if pos else None,
                             roles=roles, columns=cols, key=key, context=context)


def _rec(key, kind, cat="src", **kw):
    return RecordSetSpec(key=key, kind=kind, catalog=cat, ids=["s0"], loader=lambda i: None, **kw)


def _page(arche, dss, **kw):
    return PageSpec(id=kw.pop("id", arche), archetype=arche, title=arche.title(), datasets=dss, **kw)


def _check_providers(res):
    assert caps.missing_providers(res.layout, res.enabled) == []
    ids = [p.get("id", p["pane"]) for p in caps.iter_panes(res.layout)]
    assert len(ids) == len(set(ids)), ids
    for p in res.added_panes:
        assert p["collapsed"] is True
    assert res.enabled == sorted(res.enabled)
    assert not set(res.enabled) & set(res.disabled)
    assert set(caps.ALWAYS) - set(res.disabled) <= set(res.enabled)


# ---------------------------------------------------------------------------
# tables
# ---------------------------------------------------------------------------

def test_tables_consistent():
    assert len(caps.CAPS) == 39 and len(set(caps.CAPS)) == 39
    assert set(caps.PROVIDERS) == set(caps.CAPS)
    assert set(caps.RULES) == set(caps.FLAGS)
    for fl, cs in caps.RULES.items():
        assert set(cs) <= set(caps.CAPS), fl
    assert set(caps.ALWAYS) <= set(caps.CAPS)
    for cap, panes in caps.ADD_PANES.items():
        assert cap in caps.CAPS
        assert set(panes) & set(caps.PROVIDERS[cap]) or not caps.PROVIDERS[cap]
    for arche in caps.CORE:
        assert arche in caps.TEMPLATES


def test_caps_from_flags():
    assert caps.caps_from_flags([]) == set(caps.ALWAYS)
    got = caps.caps_from_flags(["mags2", "dens"])
    assert {"cmd_ccd_scatter", "density_rendering"} <= got
    with pytest.raises(KeyError):
        caps.caps_from_flags(["bogus"])


# ---------------------------------------------------------------------------
# archetypes
# ---------------------------------------------------------------------------

def test_explorer_full():
    ds = {"src": _cat(), "spec": _rec("spec", "pks1"), "s2d": _rec("s2d", "pki1")}
    res = caps.resolve(_page("explorer", ["src", "spec", "s2d"]), ds, {})
    assert res.flags == ["multi", "cat", "cats", "num2", "mags2", "num3", "mags3f", "sky", "skycat",
                         "spec", "spec2d"]
    for c in ("detail_panel", "cmd_ccd_scatter", "brush_lasso", "selectable_axes", "sky_map_aladin",
              "catalog_markers_on_sky", "spectrum_viewer_1d", "spectrum_2d_viewer", "live_sed_viewer",
              "linked_selection", "resizable_panels", "class_coloring", "av_slider"):
        assert c in res.enabled, c
    for c in ("density_rendering", "manifold_embedding", "lightbox", "fits_overlay", "isochrones_or_tracks"):
        assert c not in res.enabled, c
    types = caps.pane_types(res.layout)
    assert {"list", "sky", "detail", "scatter", "spectrum1d", "spectrum2d", "sed", "readme"} <= types
    core = [p for p in caps.iter_panes(res.layout) if not p.get("collapsed")]
    assert {p["pane"] for p in core} == {"list", "sky", "detail"}
    # positions give `sky`, but layer stacking needs a HiPS document (note 89)
    assert set(res.disabled) == {"hips_layer_stacking"}
    assert "HiPS" in res.disabled["hips_layer_stacking"]
    _check_providers(res)
    # template slots: list | (sky, scatter) | (detail, spectra, sed, readme)
    row = res.layout["row"]
    assert row[0]["pane"] == "list" and row[0]["size"] == 250
    assert [p["pane"] for p in row[1]["col"]] == ["sky", "scatter"]
    assert row[2]["size"] == 430 and row[2]["col"][0]["pane"] == "detail"
    scatter = next(p for p in caps.iter_panes(res.layout) if p["pane"] == "scatter")
    # the shared extinction resource (default resources) gives the G23 vector (item 88)
    assert scatter["options"] == {"x": "f182m-f410m", "y": "f410m", "overlays": ["g23_vector"]}
    s1 = next(p for p in caps.iter_panes(res.layout) if p["pane"] == "spectrum1d")
    assert s1["dataset"] == "spec" and s1["options"]["records"] == "spec"


def test_explorer_without_positions_uses_scatter_core():
    ds = {"src": _cat(pos=False)}
    res = caps.resolve(_page("explorer", ["src"]), ds, {})
    assert "sky" not in res.flags and "skycat" not in res.flags
    core = [p["pane"] for p in caps.iter_panes(res.layout) if not p.get("collapsed")]
    assert core == ["list", "scatter", "detail"]
    assert "sky" not in caps.pane_types(res.layout)
    _check_providers(res)


def test_landing():
    ds = {"src": _cat()}
    res = caps.resolve(_page("landing", [], id="index"), ds, {})
    assert res.flags == ["search"]
    assert "source_list_search" in res.enabled
    assert res.layout == {"row": [{"pane": "landing"}, {"pane": "readme"}]}
    assert res.added_panes == []
    _check_providers(res)
    bare = caps.resolve(_page("landing", [], id="index"), {}, {})
    assert bare.flags == [] and sorted(bare.enabled) == sorted(caps.ALWAYS)
    # a context catalog alone is not searchable
    ctx = caps.resolve(_page("landing", []), {"ctx": _cat(context=True)}, {})
    assert "search" not in ctx.flags


def test_gallery_with_catalog_gets_collapsed_sky_and_list():
    img = ImageSetSpec(key="img", items=[ImageItem(id="s0", roles={"spec": "a.png", "sed": "b.png"})],
                       roles=[("spec", "Spectrum"), ("sed", "SED")], catalog="src")
    ds = {"img": img, "src": _cat()}
    res = caps.resolve(_page("gallery", ["img"]), ds, {})
    assert {"items", "img", "sedimg", "cat", "sky", "skycat", "multi"} <= set(res.flags)
    added = {p["pane"] for p in res.added_panes}
    assert "sky" in added and "detail" in added
    assert {"lightbox", "static_sed_png", "catalog_markers_on_sky"} <= set(res.enabled)
    gal = next(p for p in caps.iter_panes(res.layout) if p["pane"] == "gallery")
    assert not gal.get("collapsed") and gal["options"]["images"] == "img"
    _check_providers(res)


def test_gallery_without_catalog():
    img = ImageSetSpec(key="img", items=[], roles=[("png", "PNG")])
    res = caps.resolve(_page("gallery", ["img"]), {"img": img}, {})
    assert res.flags == ["img", "items"]
    assert res.layout == {"pane": "gallery", "dataset": "img", "options": {"images": "img"}} or \
        caps.pane_types(res.layout) == {"gallery", "readme"}
    assert "source_list_search" in res.enabled and "resizable_panels" not in res.enabled
    _check_providers(res)


def test_manifold_archetype():
    ds = {"src": _cat()}
    res = caps.resolve(_page("manifold", ["src"], manifold=True), ds, {})
    assert "mani" in res.flags
    assert {"manifold_embedding", "selectable_axes"} <= set(res.enabled)
    man = next(p for p in caps.iter_panes(res.layout) if p["pane"] == "manifold")
    assert not man.get("collapsed")
    assert man["options"]["dims"] == ["f182m", "f405n", "f410m"]
    _check_providers(res)


def test_manifold_flag_without_enough_dims_disables_with_reason():
    ds = {"src": _cat(mags=("f410m",), pos=False, cls=False)}
    res = caps.resolve(_page("explorer", ["src"], manifold=True), ds, {})
    assert "mani" in res.flags
    assert "manifold_embedding" in res.disabled
    assert "no pane" in res.disabled["manifold_embedding"]
    assert "manifold_embedding" not in res.enabled
    _check_providers(res)


def test_wavelength_single_pane():
    hips = HipsSpec(base=[{"id": "b", "name": "B", "url": "https://alasky.cds.unistra.fr/x"}])
    res = caps.resolve(_page("wavelength", ["hips"]), {"hips": hips}, {})
    assert res.flags == ["sky"]
    assert res.layout["row"][0] == {"pane": "wavelength", "options": {"hips": "hips"}}
    assert "resizable_panels" not in res.enabled
    assert {"sky_map_aladin", "hips_layer_stacking", "cdn_fallback"} <= set(res.enabled)
    _check_providers(res)


def test_wavelength_fits_needs_sky_pane():
    hips = HipsSpec(base=[], fits=[{"id": "f", "name": "F", "url": "x.fits", "auth": "public",
                                    "ra": 1.0, "dec": 2.0, "size_deg": 0.1}])
    res = caps.resolve(_page("wavelength", ["hips"]), {"hips": hips}, {})
    assert "fits" in res.flags
    assert "fits_overlay" in res.enabled
    assert "sky" in {p["pane"] for p in res.added_panes}
    _check_providers(res)


def test_narrative():
    nar = NarrativeSpec(beats=[{"id": "b1", "markdown": "x"}])
    hips = HipsSpec(base=[])
    res = caps.resolve(_page("narrative", ["nar", "hips"]), {"nar": nar, "hips": hips}, {})
    leaves = list(caps.iter_panes(res.layout))
    assert leaves[0] == {"pane": "narrative", "options": {"narrative": "nar", "sticky": "sky"}}
    assert leaves[1]["pane"] == "sky"
    _check_providers(res)


def test_spectra_archetype():
    ds = {"src": _cat(), "spec": _rec("spec", "pks1")}
    res = caps.resolve(_page("spectra", ["src", "spec"]), ds, {})
    core = [p["pane"] for p in caps.iter_panes(res.layout) if not p.get("collapsed")]
    assert core == ["list", "spectrum1d"]
    _check_providers(res)


def test_legacy_json_and_png_mode_flags():
    ds = {"src": _cat(cls=False, mags=()), "spec": _rec("spec", "json", legacy_json=("spec/", "{id}.json")),
          "s2d": _rec("s2d", "json", png=lambda i: None)}
    res = caps.resolve(_page("explorer", ["src", "spec", "s2d"]), ds, {})
    assert "spec" in res.flags and "spec2d" in res.flags
    # pki1 records are absent: the 2D viewer cannot be mounted
    assert "spectrum_2d_viewer" in res.disabled
    assert "spectrum1d" in caps.pane_types(res.layout)
    _check_providers(res)


# ---------------------------------------------------------------------------
# flags from manifests, density, iso, annot
# ---------------------------------------------------------------------------

def _manifest(n, mags=("f182m", "f410m", "f444w"), context=False):
    return {"schema": "pk-catalog/1", "n": n, "ids": {"file": "ids.bin"}, "pos": {"ra": "ra", "dec": "dec"},
            "cols": [{"key": m, "role": "mag", "enc": "u16q", "filter": f"JWST/NIRCam.{m.upper()}"} for m in mags],
            "virtual": [{"key": "f182m-f410m"}],
            "presets": [{"key": "ccd", "label": "CCD", "kind": "axes", "x": "f182m-f410m", "y": "f410m-f444w"}]}


def test_foreign_catalog_from_manifest_dens():
    from pathlib import Path
    gc = PageSpec(id="gc", archetype="explorer", title="GC", datasets=["gc"])
    from portalkit.spec import CatalogSpec
    spec = CatalogSpec(key="gc", table=None, id="id", columns=[],
                       foreign=ForeignRef(kind="gc-manifest-1", url_base="https://starformation.astro.ufl.edu/x/",
                                          manifest_path=Path("/nonexistent/manifest.json")))
    res = caps.resolve(gc, {"gc": spec}, {"gc": _manifest(T0_MAX + 1)})
    assert {"dens", "mags3f", "cat", "sky", "skycat", "num3"} <= set(res.flags)
    assert "density_rendering" in res.enabled
    scatter = next(p for p in caps.iter_panes(res.layout) if p["pane"] == "scatter")
    assert scatter["options"]["x"] == "f182m-f410m" and scatter["options"]["y"] == "f410m-f444w"
    _check_providers(res)
    small = caps.resolve(gc, {"gc": spec}, {"gc": _manifest(T0_MAX)})
    assert "dens" not in small.flags


def test_context_catalog_sets_dens_and_background():
    ds = {"src": _cat(), "ctx": _cat(key="ctx", context=True)}
    res = caps.resolve(_page("explorer", ["src", "ctx"]), ds, {})
    assert "dens" in res.flags
    scatter = next(p for p in caps.iter_panes(res.layout) if p["pane"] == "scatter")
    assert scatter["options"]["background"] == "ctx"
    _check_providers(res)


def test_iso_without_scatter_dims_is_disabled():
    iso = IsochroneSpec(tracks=[Track(label="1 Myr", age_myr=1, dist_pc=8000, feh=0,
                                      mass=np.array([0.1, 1.0]), mags={"f410m": np.array([20.0, 15.0])})])
    ds = {"src": _cat(mags=("f410m",), cls=False), "iso": iso}
    res = caps.resolve(_page("explorer", ["src", "iso"]), ds, {})
    assert "iso" in res.flags
    assert "isochrones_or_tracks" in res.disabled
    _check_providers(res)
    ds2 = {"src": _cat(), "iso": iso}
    res2 = caps.resolve(_page("explorer", ["src", "iso"]), ds2, {})
    assert "isochrones_or_tracks" in res2.enabled


def test_annot_from_explicit_layout():
    img = ImageSetSpec(key="img", items=[], roles=[("png", "PNG")])
    layout = {"pane": "gallery", "dataset": "img",
              "options": {"images": "img", "annotate": {"codes": ["y", "n"], "key": "checklist"}}}
    res = caps.resolve(_page("gallery", ["img"], layout=layout), {"img": img}, {})
    assert "annot" in res.flags and "selection_export" in res.enabled
    _check_providers(res)


def test_explicit_layout_preserved_and_extended():
    ds = {"src": _cat(), "spec": _rec("spec", "pks1")}
    layout = {"row": [{"pane": "list", "dataset": "src"},
                      {"col": [{"pane": "scatter", "id": "ccd", "dataset": "src",
                                "options": {"x": "f182m-f410m", "y": "f410m"}}]}]}
    page = _page("explorer", ["src", "spec"], layout=copy.deepcopy(layout))
    res = caps.resolve(page, ds, {})
    assert page.layout == layout   # input not mutated
    assert res.layout["row"][0] == layout["row"][0]
    last = res.layout["row"][1]["col"]
    assert last[0]["id"] == "ccd"
    added = [p["pane"] for p in last[1:]]
    assert {"detail", "sky", "spectrum1d"} <= set(added)
    _check_providers(res)


def test_explicit_single_leaf_layout_wrapped_in_row():
    ds = {"src": _cat()}
    res = caps.resolve(_page("explorer", ["src"], layout={"pane": "list", "dataset": "src"}), ds, {})
    assert res.layout["row"][0] == {"pane": "list", "dataset": "src"}
    assert "col" in res.layout["row"][1]
    _check_providers(res)


def test_caps_disable():
    ds = {"src": _cat()}
    page = _page("explorer", ["src"], caps_disable={"sky_map_aladin": "offline page",
                                                     "live_sed_viewer": "no filters"})
    res = caps.resolve(page, ds, {})
    assert res.disabled["sky_map_aladin"] == "offline page"
    assert "sky_map_aladin" not in res.enabled and "live_sed_viewer" not in res.enabled
    # the sed pane may still be added for the template/feature overlays it also provides
    _check_providers(res)
    with pytest.raises(ValueError, match="unknown capability"):
        caps.resolve(_page("explorer", ["src"], caps_disable={"warp_drive": "x"}), ds, {})
    with pytest.raises(ValueError, match="reason"):
        caps.resolve(_page("explorer", ["src"], caps_disable={"av_slider": " "}), ds, {})


def test_unknown_dataset_and_archetype():
    with pytest.raises(KeyError):
        caps.resolve(_page("explorer", ["zz"]), {}, {})
    with pytest.raises(ValueError):
        caps.resolve(PageSpec(id="x", archetype="nope", title="x", datasets=[]), {}, {})
    with pytest.raises(KeyError):
        caps.resolve(_page("explorer", ["spec"]), {"spec": _rec("spec", "pks1", cat="missing")}, {})


def test_empty_and_single_row_catalog():
    for n in (0, 1):
        res = caps.resolve(_page("explorer", ["src"]), {"src": _cat(n=n)}, {})
        _check_providers(res)
        assert "cat" in res.flags


def test_resolve_deterministic():
    ds = {"src": _cat(), "spec": _rec("spec", "pks1"), "ctx": _cat(key="ctx", context=True)}
    a = caps.resolve(_page("explorer", list(ds)), ds, {})
    b = caps.resolve(_page("explorer", list(ds)), ds, {})
    assert a == b


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def test_default_axes():
    info = caps.CatInfo(ds="x", cols={"a": {"role": "value"}, "b": {"role": "value"}})
    assert caps.default_axes(info) == ("a", "b")
    info.cols["m1"] = {"role": "mag"}
    info.cols["m2"] = {"role": "mag"}
    assert caps.default_axes(info) == ("m1-m2", "m2")
    info.presets = [{"kind": "axes", "x": "a", "y": "m1"}]
    assert caps.default_axes(info) == ("a", "m1")
    assert caps.default_axes(caps.CatInfo(ds="y", cols={"a": {"role": "value"}})) is None


def test_catinfo_resolves():
    cat = _cat()
    cat.columns.append(ColumnSpec(key="note", role="text"))
    cat.presets.append(Preset(key="p", label="P", kind="axes", data={"x": "f182m", "y": "f410m"}))
    info = caps.catalog_info("src", cat)
    assert info.resolves("f182m-f410m") is None
    assert info.resolves("0.5*f405n+0.5*f410m") is None
    assert "unknown column" in info.resolves("A_V")
    assert "non-numeric" in info.resolves("cls")
    assert info.resolves("cls", numeric=False) is None
    assert info.resolves("name", numeric=False) is None      # id field
    assert "non-numeric" in info.resolves("note+f410m")
    assert info.resolves("o${o}") is not None
    assert info.resolves("") is not None and info.resolves(None) is not None
    assert info.presets[0]["x"] == "f182m"


def test_insert_panes_and_default_layout():
    leaf = {"pane": "detail"}
    assert caps.insert_panes({"pane": "list"}, []) == {"pane": "list"}
    assert caps.insert_panes({"row": [{"pane": "list"}]}, [leaf]) == {"row": [{"pane": "list"}, leaf]}
    assert caps.insert_panes({"row": [{"col": [{"pane": "list"}]}]}, [leaf, leaf]) == \
        {"row": [{"col": [{"pane": "list"}, leaf, leaf]}]}
    assert caps.default_layout("explorer", []) == {"pane": "readme"}
    assert caps.default_layout("explorer", [{"pane": "list"}]) == {"pane": "list"}



def test_manifold_default_group_is_numeric():
    from portalkit.spec import Group, catalog_from_rows, standard_pages
    from portalkit import config
    rows = [{"id": f"s{i}", "a": 1.0 * i, "b": 2.0 + i, "c": 3.0 - i, "cls": "x" if i % 2 else "y"}
            for i in range(6)]
    cat = catalog_from_rows(rows, id="id", roles={"cls": "category"})
    cat.groups = [Group(key="meta", label="Meta", dims=["cls"]), Group(key="phot", label="Phot", dims=["a", "b", "c"])]
    ds = {"src": cat}
    pages = [p for p in standard_pages(ds, manifold=True) if p.archetype == "manifold"]
    assert pages
    res = caps.resolve(pages[0], ds, {})
    leaves = [l for l in caps.iter_panes(res.layout) if l.get("pane") == "manifold"]
    # the group's keys as an explicit list, as build.resolve_manifold_dims writes it
    assert leaves and leaves[0]["options"]["dims"] == ["a", "b", "c"]
    config.assemble(pages[0], ds, {}, res, portal={"id": "t", "title": "T", "visibility": "public"})
    cat.groups = [Group(key="meta", label="Meta", dims=["cls"])]
    res = caps.resolve(pages[0], ds, {})
    leaves = [l for l in caps.iter_panes(res.layout) if l.get("pane") == "manifold"]
    assert leaves[0]["options"]["dims"] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# IMPLEMENTATION_NOTES 89: capabilities that need a document of one kind
# ---------------------------------------------------------------------------

_HIPS = HipsSpec(base=[{"id": "b", "name": "B", "url": "https://alasky.cds.unistra.fr/x"}])


def test_needs_table():
    assert set(caps.NEEDS) == {"hips_layer_stacking", "live_sed_viewer", "labeled_features_overlay",
                               "extinction_vectors_on_ccd"}
    assert {k for k, _ in caps.NEEDS.values()} == {"hips", "filters", "features", "extinction"}
    for cap, (kind, reason) in caps.NEEDS.items():
        assert cap in caps.CAPS and reason
        # a need served by a shared resource is attached on demand by the same cap (item 110)
        if kind in caps.SHARED_RESOURCES:
            assert cap in caps.RESOURCE_CAPS[kind], (cap, kind)
    assert caps.unmet_needs(["live_sed_viewer", "av_slider"], ["catalog"]) == \
        {"live_sed_viewer": caps.NEEDS["live_sed_viewer"][1]}
    assert caps.unmet_needs([], []) == {}
    assert caps.unmet_needs(caps.NEEDS, ["hips", "filters", "features"]) == \
        {"extinction_vectors_on_ccd": "no extinction dataset is attached to this page"}
    assert caps.unmet_needs(caps.NEEDS, ["hips", "filters", "features", "extinction"]) == {}
    assert set(caps.RESOURCE_CAPS) == set(caps.SHARED_RESOURCES) == set(caps.RESOURCE_REF_IDS)
    for kind, cs in caps.RESOURCE_CAPS.items():
        assert set(cs) <= set(caps.CAPS), kind


def test_hips_layer_stacking_needs_hips_document():
    ds = {"src": _cat(), "hips": _HIPS}
    without = caps.resolve(_page("explorer", ["src"]), ds, {})
    assert "sky" in without.flags and "sky_map_aladin" in without.enabled
    assert "hips_layer_stacking" not in without.enabled
    assert without.disabled["hips_layer_stacking"] == caps.NEEDS["hips_layer_stacking"][1]
    with_h = caps.resolve(_page("explorer", ["src", "hips"]), ds, {})
    assert "hips_layer_stacking" in with_h.enabled and "hips_layer_stacking" not in with_h.disabled
    _check_providers(without)
    _check_providers(with_h)
    # an adapter opt-out keeps its own reason
    opt = caps.resolve(_page("explorer", ["src"], caps_disable={"hips_layer_stacking": "mine"}), ds, {})
    assert opt.disabled["hips_layer_stacking"] == "mine"


def test_filters_and_features_follow_resources():
    ds = {"src": _cat(), "flt": FilterSetSpec(keys=["JWST/NIRCam.F182M"]), "spec": _rec("spec", "pks1")}
    # default: the shared resources are attachable (build.py adds them on demand)
    dflt = caps.resolve(_page("explorer", ["src", "spec"]), ds, {})
    assert {"live_sed_viewer", "labeled_features_overlay"} <= set(dflt.enabled)
    assert dflt.layout == caps.resolve(_page("explorer", ["src", "spec"]), ds, {},
                                       resources=caps.SHARED_RESOURCES).layout
    # none attached: both off with the item-89 reasons; no SED pane without filters
    none = caps.resolve(_page("explorer", ["src", "spec"]), ds, {}, resources=())
    for cap in ("live_sed_viewer", "labeled_features_overlay"):
        assert cap not in none.enabled
        assert none.disabled[cap] == caps.NEEDS[cap][1]
    assert "sed" not in caps.pane_types(none.layout)
    assert "av_slider" in none.enabled           # moves to spectrum1d / scatter
    _check_providers(none)
    # a filters dataset on the page counts; features only through resources
    flt = caps.resolve(_page("explorer", ["src", "spec", "flt"]), ds, {}, resources=())
    assert "live_sed_viewer" in flt.enabled and "labeled_features_overlay" in flt.disabled
    assert "sed" in caps.pane_types(flt.layout)
    feat = caps.resolve(_page("explorer", ["src", "spec"]), ds, {}, resources=["features"])
    assert "labeled_features_overlay" in feat.enabled and "live_sed_viewer" in feat.disabled
    _check_providers(feat)
    with pytest.raises(ValueError, match="shared resource"):
        caps.resolve(_page("explorer", ["src"]), ds, {}, resources=["hips"])


def test_available_kinds_and_spec_kind():
    ds = {"src": _cat(), "hips": _HIPS, "ext": ExtinctionSpec()}
    pd = caps.page_data(_page("explorer", ["src", "hips", "ext"]), ds, {})
    assert caps.available_kinds(pd, ()) == {"catalog", "hips", "extinction"}
    assert caps.available_kinds(pd) == {"catalog", "hips", "extinction", *caps.SHARED_RESOURCES}
    assert caps.spec_kind(ExtinctionSpec()) == "extinction"
    from portalkit.spec import ReadmeSpec
    assert caps.spec_kind(ReadmeSpec(markdown="x")) is None


# ---------------------------------------------------------------------------
# IMPLEMENTATION_NOTES 88: default overlays and manifold dims
# ---------------------------------------------------------------------------

def _iso2():
    m = np.array([0.1, 1.0])
    return IsochroneSpec(tracks=[
        Track(label="MIST 1 Myr", age_myr=1, dist_pc=8000, feh=0, mass=m,
              mags={"f182m": np.array([20.0, 15.0]), "f410m": np.array([19.0, 14.0])}),
        Track(label="MIST 1 Myr", age_myr=1, dist_pc=8000, feh=0, mass=m,      # duplicate slug
              mags={"f182m": np.array([20.0, 15.0]), "f410m": np.array([19.0, 14.0])}),
        Track(label="10 Myr (Z=0.02)", age_myr=10, dist_pc=8000, feh=0, mass=m,
              mags={"f182m": np.array([21.0, 16.0]), "f410m": np.array([20.0, 15.0])})])


def _leaves(res, ptype):
    return [p for p in caps.iter_panes(res.layout) if p["pane"] == ptype]


def test_overlay_slug():
    assert caps.overlay_slug("MIST 1 Myr") == "mist_1_myr"
    assert caps.overlay_slug("mist_1myr") == "mist_1myr"
    assert caps.overlay_slug("  G23 ") == "g23"
    assert caps.overlay_slug("Ωμ") == ""
    from portalkit import config
    assert config.overlay_slug is caps.overlay_slug


def test_default_scatter_overlays_tracks_and_vector():
    ds = {"src": _cat(), "iso": _iso2(), "ext": ExtinctionSpec(law="G23")}
    res = caps.resolve(_page("explorer", ["src", "iso", "ext"]), ds, {})
    assert {"isochrones_or_tracks", "extinction_vectors_on_ccd"} <= set(res.enabled)
    (sc,) = _leaves(res, "scatter")
    assert sc["options"]["overlays"] == ["mist_1_myr", "10_myr_z_0_02", "g23_vector"]
    added = [p for p in res.added_panes if p["pane"] == "scatter"]
    assert added and added[0]["options"]["overlays"] == sc["options"]["overlays"]
    # tracks only / vector only (no shared resources attached)
    res = caps.resolve(_page("explorer", ["src", "iso"]), ds, {}, resources=())
    assert _leaves(res, "scatter")[0]["options"]["overlays"] == ["mist_1_myr", "10_myr_z_0_02"]
    res = caps.resolve(_page("explorer", ["src", "ext"]), ds, {}, resources=())
    assert _leaves(res, "scatter")[0]["options"]["overlays"] == ["g23_vector"]
    # neither dataset nor the shared extinction resource: no overlays key
    res = caps.resolve(_page("explorer", ["src"]), ds, {}, resources=())
    assert "overlays" not in _leaves(res, "scatter")[0]["options"]


def test_default_overlays_follow_caps():
    ds = {"src": _cat(), "iso": _iso2(), "ext": ExtinctionSpec(law="F99")}
    page = _page("explorer", ["src", "iso", "ext"],
                 caps_disable={"isochrones_or_tracks": "x", "extinction_vectors_on_ccd": "y"})
    res = caps.resolve(page, ds, {})
    assert "overlays" not in _leaves(res, "scatter")[0]["options"]
    page = _page("explorer", ["src", "iso", "ext"], caps_disable={"isochrones_or_tracks": "x"})
    assert _leaves(caps.resolve(page, ds, {}), "scatter")[0]["options"]["overlays"] == ["f99_vector"]
    # the isochrones document (manifest) wins over the spec labels
    man = {"iso": {"schema": "pk-isochrones/1", "tracks": [{"label": "PARSEC 3 Myr"}, {"nolabel": 1}]}}
    res = caps.resolve(_page("explorer", ["src", "iso"]), ds, man, resources=())
    assert _leaves(res, "scatter")[0]["options"]["overlays"] == ["parsec_3_myr"]


def test_default_vector_from_shared_extinction_resource():
    # a page without an ExtinctionSpec: build attaches the shared {res} extinction ("ext")
    ds = {"src": _cat()}
    page = _page("explorer", ["src"])
    res = caps.resolve(page, ds, {})                 # build.py: resources=None
    assert "extinction_vectors_on_ccd" in res.enabled
    assert _leaves(res, "scatter")[0]["options"]["overlays"] == [caps.SHARED_EXTINCTION_VECTOR]
    assert caps.SHARED_EXTINCTION_VECTOR == "g23_vector"
    res = caps.resolve(page, ds, {}, resources=["filters"])
    assert "overlays" not in _leaves(res, "scatter")[0]["options"]
    # build-style refs: the spec-less extinction ref validates as G23 and the vector stays
    from portalkit import config
    refs = {"ext": {"kind": "extinction", "url": "{res}ext.json"},
            "filters": {"kind": "filters", "url": "{res}f.json"},
            "tpl": {"kind": "templates", "url": "{res}t.json"},
            "feat": {"kind": "features", "url": "{res}feat.json"}}
    portal = {"id": "t", "title": "T", "visibility": "public"}
    for given in (res, None, caps.resolve(page, ds, {})):
        doc = config.assemble(page, ds, {}, given, portal=portal, refs=refs)
        sc = next(p for p in caps.iter_panes(doc["layout"]) if p["pane"] == "scatter")
        if given is res:
            assert "overlays" not in sc["options"]
        else:
            assert sc["options"]["overlays"] == ["g23_vector"]
        assert "ext" in doc["datasets"]
    # no extinction ref: assemble drops the resolver's shared vector instead of failing
    doc = config.assemble(page, ds, {}, caps.resolve(page, ds, {}), portal=portal,
                          refs={k: v for k, v in refs.items() if k != "ext"})
    sc = next(p for p in caps.iter_panes(doc["layout"]) if p["pane"] == "scatter")
    assert "overlays" not in sc["options"]


def test_default_overlays_use_first_dataset_of_each_kind():
    # the panes read one document per kind (docOf: the first in page order)
    ds = {"src": _cat(), "e1": ExtinctionSpec(law="G23"), "e2": ExtinctionSpec(law="CT06"),
          "i1": _iso2(), "i2": IsochroneSpec(tracks=[Track(
              label="PARSEC 3 Myr", age_myr=3, dist_pc=8000, feh=0, mass=np.array([0.1, 1.0]),
              mags={"f182m": np.array([20.0, 15.0]), "f410m": np.array([19.0, 14.0])})])}
    res = caps.resolve(_page("explorer", ["src", "e2", "e1", "i1", "i2"]), ds, {})
    assert _leaves(res, "scatter")[0]["options"]["overlays"] == \
        ["mist_1_myr", "10_myr_z_0_02", "ct06_vector"]


def test_layout_option_datasets_count_as_available():
    # a HiPS dataset named only by an adapter pane option (sky options.hips) is attached by
    # config.assemble, so hips_layer_stacking stays on (item 89)
    from portalkit import config
    ds = {"src": _cat(), "hips": _HIPS}
    layout = {"row": [{"pane": "list", "dataset": "src"},
                      {"pane": "sky", "dataset": "src", "options": {"hips": "hips"}}]}
    page = _page("explorer", ["src"], layout=layout)
    res = caps.resolve(page, ds, {})
    assert "hips_layer_stacking" in res.enabled
    assert "hips_layer_stacking" not in res.disabled
    doc = config.assemble(page, ds, {}, res, portal={"id": "t", "title": "T", "visibility": "public"},
                          refs={"filters": {"kind": "filters", "url": "{res}f.json"},
                                "feat": {"kind": "features", "url": "{res}feat.json"},
                                "ext": {"kind": "extinction", "url": "{res}e.json"}})
    assert doc["datasets"]["hips"]["kind"] == "hips"
    assert "hips_layer_stacking" in doc["caps"]["enabled"]
    assert caps.layout_datasets(layout) == ["src", "hips"]


def test_default_overlays_on_manifold_and_not_on_adapter_panes():
    ds = {"src": _cat(), "iso": _iso2(), "ext": ExtinctionSpec()}
    res = caps.resolve(_page("manifold", ["src", "iso", "ext"], manifold=True), ds, {})
    (man,) = _leaves(res, "manifold")
    assert man["options"]["overlays"] == ["mist_1_myr", "10_myr_z_0_02", "g23_vector"]
    layout = {"row": [{"pane": "list", "dataset": "src"},
                      {"pane": "scatter", "id": "ccd", "dataset": "src",
                       "options": {"x": "f182m-f410m", "y": "f410m"}}]}
    res = caps.resolve(_page("explorer", ["src", "iso", "ext"], layout=copy.deepcopy(layout)), ds, {})
    ccd = next(p for p in caps.iter_panes(res.layout) if p.get("id") == "ccd")
    assert "overlays" not in ccd["options"]


def test_overlays_pass_config_validation():
    from portalkit import config
    ds = {"src": _cat(), "iso": _iso2(), "ext": ExtinctionSpec(),
          "flt": FilterSetSpec(keys=["JWST/NIRCam.F182M"])}
    for arche in ("explorer", "manifold"):
        page = _page(arche, ["src", "iso", "ext", "flt"], manifold=arche == "manifold")
        res = caps.resolve(page, ds, {}, resources=["templates", "features"])
        doc = config.assemble(page, ds, {}, res, portal={"id": "t", "title": "T", "visibility": "public"},
                              res_refs={"filters": "{res}f.json", "extinction": "{res}e.json",
                                        "templates": "{res}t.json"},
                              refs={"feat": {"kind": "features", "url": "{res}feat.json"}})
        ov = [p["options"]["overlays"] for p in caps.iter_panes(doc["layout"])
              if p["pane"] in ("scatter", "manifold") and "overlays" in p.get("options", {})]
        assert ov and all("g23_vector" in o for o in ov)
        assert doc["caps"]["enabled"] == res.enabled


def test_manifold_dims_from_manifest():
    ds = {"src": _cat()}
    ds["src"].manifold = ManifoldSpec(dims=["f405n", "f410m", "f182m-f410m"])
    res = caps.resolve(_page("explorer", ["src"], manifold=True), ds, {})
    (man,) = _leaves(res, "manifold")
    assert man["options"]["dims"] == ["f405n", "f410m", "f182m-f410m"]
    # a written manifest wins over the spec
    manifest = _manifest(10)
    manifest["manifold"] = {"dims": ["f182m", "f410m", "f444w", "f182m-f410m"]}
    gc = PageSpec(id="gc", archetype="manifold", title="GC", datasets=["gc"], manifold=True)
    from portalkit.spec import CatalogSpec
    from pathlib import Path
    spec = CatalogSpec(key="gc", table=None, id="id", columns=[],
                       foreign=ForeignRef(kind="gc-manifest-1", url_base="https://starformation.astro.ufl.edu/x/",
                                          manifest_path=Path("/nonexistent/manifest.json")))
    res = caps.resolve(gc, {"gc": spec}, {"gc": manifest})
    (man,) = _leaves(res, "manifold")
    assert man["options"]["dims"] == ["f182m", "f410m", "f444w", "f182m-f410m"]
    # the build's resolve_manifold_dims leaves an explicit list as it is
    from portalkit.build import resolve_manifold_dims
    assert resolve_manifold_dims(res.layout, {"gc": manifest}, "gc") == res.layout


# ---------------------------------------------------------------------------
# IMPLEMENTATION_NOTES 110: extinction-vector need and the shared attachment rule
# ---------------------------------------------------------------------------

def _ovl(*layers):
    from portalkit.spec import OverlaySpec
    return OverlaySpec(layers=list(layers))


_RED = {"id": "reddening", "name": "Reddening", "space": "dim", "kind": "vector", "default": True,
        "items": [{"x": "f182m-f410m", "y": "f410m", "pts": [[0.5, 12.0], [1.5, 12.2]]}]}


def test_extinction_vector_needs_extinction_dataset():
    ds = {"src": _cat(), "ext": ExtinctionSpec(law="F99")}
    page = _page("explorer", ["src"])
    # nothing attachable: the cap is off with the item-110 reason, the scatter stays (CMD)
    none = caps.resolve(page, ds, {}, resources=())
    assert "extinction_vectors_on_ccd" not in none.enabled
    assert none.disabled["extinction_vectors_on_ccd"] == caps.NEEDS["extinction_vectors_on_ccd"][1]
    assert "scatter" in caps.pane_types(none.layout)
    _check_providers(none)
    # the page's own extinction dataset satisfies it, so does the shared resource
    own = caps.resolve(_page("explorer", ["src", "ext"]), ds, {}, resources=())
    assert "extinction_vectors_on_ccd" in own.enabled
    assert _leaves(own, "scatter")[0]["options"]["overlays"] == ["f99_vector"]
    shared = caps.resolve(page, ds, {}, resources=["extinction"])
    assert "extinction_vectors_on_ccd" in shared.enabled
    assert _leaves(shared, "scatter")[0]["options"]["overlays"] == ["g23_vector"]
    # an adapter opt-out keeps its own reason
    opt = caps.resolve(_page("explorer", ["src"], caps_disable={"extinction_vectors_on_ccd": "no"}), ds, {})
    assert opt.disabled["extinction_vectors_on_ccd"] == "no"


def test_attachable_resources_rule():
    ds = {"src": _cat(), "spec": _rec("spec", "pks1"), "ext": ExtinctionSpec(),
          "flt": FilterSetSpec(keys=["JWST/NIRCam.F182M"])}
    page = _page("explorer", ["src", "spec"])
    res = caps.resolve(page, ds, {})
    assert caps.attachable_resources(page, ds, res) == caps.SHARED_RESOURCES
    assert caps.attachable_resources(page, ds, res.enabled) == caps.SHARED_RESOURCES
    # kinds the page already carries are not attached again (also through implied ids)
    own = _page("explorer", ["spec", "ext", "flt"])
    assert caps.attachable_resources(own, ds, caps.resolve(own, ds, {})) == ("templates", "features")
    # only kinds some enabled cap uses
    assert caps.attachable_resources(page, ds, ["labeled_features_overlay"]) == ("features",)
    assert caps.attachable_resources(page, ds, ["extinction_vectors_on_ccd"]) == ("filters", "extinction")
    assert caps.attachable_resources(page, ds, []) == ()
    assert caps.attachable_resources(page, ds, ["keyboard_nav", "brush_rect"]) == ()
    # a page without magnitudes in 3+ filters and no spectra needs none
    bare = {"src": _cat(mags=("f182m",))}
    bp = _page("explorer", ["src"])
    assert caps.attachable_resources(bp, bare, caps.resolve(bp, bare, {})) == ()
    # an adapter layout that names a dataset counts as carrying its kind
    from portalkit.spec import TemplateSpec
    ds["mytpl"] = TemplateSpec()
    lay = {"row": [{"pane": "list", "dataset": "src"},
                   {"pane": "spectrum1d", "dataset": "spec", "options": {"records": "spec", "templates": "mytpl"}}]}
    lp = _page("spectra", ["src", "spec"], layout=lay)
    assert "templates" not in caps.attachable_resources(lp, ds, ["model_template_overlay"])


def test_resource_refs_names_and_collisions():
    urls = {k: "{res}" + k + ".json" for k in caps.SHARED_RESOURCES}
    refs = caps.resource_refs(caps.SHARED_RESOURCES, urls)
    assert refs == {"tpl": {"kind": "templates", "url": "{res}templates.json"},
                    "filters": {"kind": "filters", "url": "{res}filters.json"},
                    "ext": {"kind": "extinction", "url": "{res}extinction.json"},
                    "feat": {"kind": "features", "url": "{res}features.json"}}
    got = caps.resource_refs(["extinction", "templates"], urls, taken={"ext", "_ext", "tpl"})
    assert got == {"__ext": {"kind": "extinction", "url": "{res}extinction.json"},
                   "_tpl": {"kind": "templates", "url": "{res}templates.json"}}
    assert caps.resource_refs([], urls) == {}
    with pytest.raises(KeyError):
        caps.resource_refs(["features"], {})
    with pytest.raises(ValueError, match="shared resource"):
        caps.resource_refs(["hips"], urls)


def test_overlay_document_reddening_layers_in_default_overlays():
    locus = {"id": "locus", "space": "dim", "kind": "track", "items": []}
    hidden = {"id": "hidden_vec", "space": "dim", "kind": "vector", "default": False, "items": []}
    sky = {"id": "fields", "space": "sky", "kind": "polygons", "items": []}
    ds = {"src": _cat(), "ovl": _ovl(sky, locus, _RED, hidden, dict(_RED)), "hips": _HIPS,
          "nar": NarrativeSpec(beats=[{"id": "b1", "title": "B", "markdown": "x"}])}
    pd = caps.page_data(_page("wavelength", ["hips", "src", "ovl"]), ds, {})
    assert caps.dim_vector_layers(pd) == ["reddening"]
    # the written document wins over the spec
    pdm = caps.page_data(_page("wavelength", ["hips", "src", "ovl"]), ds,
                         {"ovl": {"layers": [{"id": "rv", "space": "dim", "kind": "vector"}]}})
    assert caps.dim_vector_layers(pdm) == ["rv"]
    assert caps.dim_vector_layers(caps.page_data(_page("explorer", ["src"]), ds, {})) == []
    for arche, dss in (("wavelength", ["hips", "src", "ovl"]), ("narrative", ["nar", "hips", "ovl", "src"])):
        res = caps.resolve(_page(arche, dss), ds, {})
        assert "extinction_vectors_on_ccd" in res.enabled, arche
        (sc,) = _leaves(res, "scatter")
        assert sc["options"]["overlays"] == ["g23_vector", "reddening"], arche
        _check_providers(res)
        # cap off (opt-out or nothing attachable): neither is listed
        off = caps.resolve(_page(arche, dss, caps_disable={"extinction_vectors_on_ccd": "x"}), ds, {})
        assert "overlays" not in (_leaves(off, "scatter")[0].get("options") or {})
        none = caps.resolve(_page(arche, dss), ds, {}, resources=())
        assert "extinction_vectors_on_ccd" in none.disabled
        sc = _leaves(none, "scatter")
        assert not sc or "overlays" not in (sc[0].get("options") or {})
    # other archetypes keep only the <law>_vector (note 110 scopes the layers to
    # wavelength/narrative documents), so they draw one reddening vector, not two
    for arche in ("explorer", "manifold"):
        res = caps.resolve(_page(arche, ["src", "ovl"]), ds, {})
        assert "extinction_vectors_on_ccd" in res.enabled, arche
        leaves = _leaves(res, "scatter") + _leaves(res, "manifold")
        assert leaves, arche
        for leaf in leaves:
            ov = (leaf.get("options") or {}).get("overlays") or []
            assert "reddening" not in ov and "g23_vector" in ov, (arche, leaf)


def test_reddening_layers_validate_and_drop_without_extinction():
    from portalkit import config
    ds = {"src": _cat(), "ovl": _ovl(_RED), "hips": _HIPS}
    page = _page("wavelength", ["hips", "src", "ovl"])
    res = caps.resolve(page, ds, {})
    portal = {"id": "t", "title": "T", "visibility": "public"}
    urls = {k: "{res}" + k + ".json" for k in caps.SHARED_RESOURCES}
    refs = caps.resource_refs(caps.attachable_resources(page, ds, res), urls, taken=ds)
    assert {r["kind"] for r in refs.values()} == set(caps.SHARED_RESOURCES)
    doc = config.assemble(page, ds, {}, res, portal=portal, refs=refs)
    assert doc["caps"]["enabled"] == res.enabled
    sc = next(p for p in caps.iter_panes(doc["layout"]) if p["pane"] == "scatter")
    assert sc["options"]["overlays"] == ["g23_vector", "reddening"]
    # the same caps without the extinction ref: the cap goes off with the reason and the
    # resolver-listed vectors go with it
    no_ext = {k: v for k, v in refs.items() if v["kind"] != "extinction"}
    doc = config.assemble(page, ds, {}, res, portal=portal, refs=no_ext)
    assert "extinction_vectors_on_ccd" not in doc["caps"]["enabled"]
    assert doc["caps"]["disabled"]["extinction_vectors_on_ccd"] == caps.NEEDS["extinction_vectors_on_ccd"][1]
    sc = next(p for p in caps.iter_panes(doc["layout"]) if p["pane"] == "scatter")
    assert "overlays" not in (sc.get("options") or {})
