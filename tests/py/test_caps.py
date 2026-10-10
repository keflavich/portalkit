"""portalkit.caps: flags, rules, providers and added panes (ARCHITECTURE.md section 8)."""

from __future__ import annotations

import copy

import pytest

from portalkit import caps
from portalkit.constants import T0_MAX
from portalkit.spec import (ColumnSpec, ForeignRef, HipsSpec, ImageItem, ImageSetSpec,
                            IsochroneSpec, NarrativeSpec, PageSpec, Preset, RecordSetSpec,
                            Track, catalog_from_rows)

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
    assert res.disabled == {}
    _check_providers(res)
    # template slots: list | (sky, scatter) | (detail, spectra, sed, readme)
    row = res.layout["row"]
    assert row[0]["pane"] == "list" and row[0]["size"] == 250
    assert [p["pane"] for p in row[1]["col"]] == ["sky", "scatter"]
    assert row[2]["size"] == 430 and row[2]["col"][0]["pane"] == "detail"
    scatter = next(p for p in caps.iter_panes(res.layout) if p["pane"] == "scatter")
    assert scatter["options"] == {"x": "f182m-f410m", "y": "f410m"}
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
    assert leaves and leaves[0]["options"]["dims"] == "group:phot"
    config.assemble(pages[0], ds, {}, res, portal={"id": "t", "title": "T", "visibility": "public"})
    cat.groups = [Group(key="meta", label="Meta", dims=["cls"])]
    res = caps.resolve(pages[0], ds, {})
    leaves = [l for l in caps.iter_panes(res.layout) if l.get("pane") == "manifold"]
    assert leaves[0]["options"]["dims"] == ["a", "b", "c"]
