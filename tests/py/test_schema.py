"""Tests for portalkit.schema validators (ARCHITECTURE.md sections 4, 5 and 11.3)."""

from __future__ import annotations

import json
import time
import tomllib
from pathlib import Path

import pytest

from portalkit import schema
from portalkit.constants import PAGE_JSON_MAX
from portalkit.schema import SchemaError

REPO = Path(__file__).resolve().parents[2]
DOC = (REPO / "docs/ARCHITECTURE.md").read_text(encoding="utf-8")


def _doc_block(heading: str, lang: str = "json") -> str:
    i = DOC.index(heading)
    return DOC[i:].split(f"```{lang}", 1)[1].split("```", 1)[0]


def _has(errs: list[str], *needles: str) -> bool:
    return any(all(n in e for n in needles) for e in errs)


# ---------------------------------------------------------------------------
# fixtures: one minimal valid document per type
# ---------------------------------------------------------------------------

BUILD = "20261010T120000Z-1a2b3c4d"
H64 = "a" * 64


def page_doc():
    return json.loads(_doc_block("#### 4.3.1 Example"))


def portal_doc():
    return json.loads(_doc_block("### 4.2 `portal.json`"))


def catalog_doc(n=3):
    q = [10.0 + 0.1 * i for i in range(101)]
    return {
        "schema": "pk-catalog/1", "id": "src", "title": "Synthetic", "n": n, "build": BUILD,
        "order": {"kind": "input"}, "chunk_rows": 1048576,
        "ids": {"file": "ids.2b7e11aa.bin", "enc": "str", "chunks": None, "alias_file": None},
        "pos": {"enc": "u32lin", "files": {"ra": "pos/ra.aa11bb22.bin", "dec": "pos/dec.cc33dd44.bin"}},
        "bbox": {"ra": [266.76, 266.90], "dec": [-28.45, -28.31]},
        "cols": [
            {"key": "f410m", "label": "F410M", "unit": "mag", "role": "mag", "filter": "JWST/NIRCam.F410M",
             "err": "e_f410m", "enc": "u16q", "lo": 10.0, "hi": 75.534, "step": 0.001, "clip": "none",
             "shuffle": True, "file": "cols/f410m.7c3a91d0.bin", "center": 18.21, "scale": 1.31,
             "nvalid": n, "quantiles": q, "limits": None, "group": "nircam", "invert": True},
            {"key": "f182m", "role": "mag", "enc": "u16q", "lo": 10.0, "hi": 75.534, "step": 0.001,
             "file": "cols/f182m.7c3a91d1.bin", "group": "nircam"},
            {"key": "e_f410m", "role": "error", "enc": "u8log", "lo": 0.001, "hi": 1.0,
             "file": "cols/e_f410m.0b1c2d3e.bin"},
            {"key": "cls", "role": "category", "enc": "cat8", "levels": ["star", "YSO", "red"],
             "colors": {"YSO": "#e05a47"}, "default_off": [], "file": "cols/cls.44aa0912.bin"},
            {"key": "has_spec", "role": "flag", "enc": "bits", "file": "cols/has_spec.19fe22c0.bin"}],
        "virtual": [{"key": "f182m-f410m", "label": "F182M − F410M", "unit": "mag", "group": "colors"}],
        "groups": [{"key": "nircam", "label": "NIRCam mags", "dims": ["f182m", "f410m"]}],
        "presets": [{"key": "ccd1", "label": "CCD", "kind": "axes", "x": "f182m-f410m", "y": "f410m"},
                    {"key": "pca", "label": "PCA", "kind": "frame", "dims": ["f182m", "f410m"],
                     "F": [1, 0, 0, 1]},
                    {"key": "yso", "label": "YSO", "kind": "selection", "sel": "r:eyJvcHMiOls"}],
        "density": [{"key": "ccd1", "x": "f182m-f410m", "y": "f410m", "xr": [-1, 6], "yr": [-1, 5],
                     "nx": 256, "ny": 256, "enc": "u16log", "lo": 1, "hi": 40211,
                     "file": "density/ccd1.5ae0c1b2.bin"}],
        "rows": {"chunk": 4096, "files": ["rows/r0000.9a0b1c2d.bin"] if n else [], "cols": ["id", "f410m"]},
        "manifold": {"dims": ["f182m", "f410m"], "default_frame": "pca", "fade_width": 0.12},
        "links": {"records": {"spec": "../spec/index.json"}, "images": "../img/images.json"},
        "provenance": {"inputs": [], "tier": "B"},
        "caveats": ["F2550W fluxes are beam-crowded in W51 IRS2."],
        "budget": {"first_paint_bytes": 1834112, "total_bytes": 38822001},
    }


def records_doc():
    return {"schema": "pk-records/1", "kind": "pks1", "layout": "shard", "catalog": "src", "key": "id",
            "n": 3, "shards": ["s000.1a2b3c4d.bin", "s001.5e6f7a8b.bin"],
            "entries": {"6927_10038": [0, 0, 18234], "6927_10041": [0, 18234, 20110],
                        "6927_big": "r/0a1b2c3d.bin"},
            "grids": {"g235m": {"kind": "uniform", "w0": 1.66, "dw": 0.000356, "n": 3700}},
            "variants": ["flux", "norm", "normcm"], "unit_w": "um", "unit_f": "Jy"}


def pks1_header():
    return {"id": "6927_10038", "n": 7400, "wave": {"kind": "uniform", "w0": 1.66, "dw": 0.000356},
            "unit_w": "um",
            "channels": [{"name": "flux", "unit": "Jy", "enc": "f32", "off": 29600},
                         {"name": "err", "unit": "Jy", "enc": "u16log", "lo": -6.0, "hi": -1.0, "off": 59200},
                         {"name": "norm", "unit": "", "enc": "u16q", "lo": 0.0, "hi": 3.2, "off": 74000}],
            "segments": [[0, 3700, "G235H"], [3700, 7400, "G395H"]],
            "mask_runs": [[120, 180, 1], [5000, 5100, 2]],
            "mask_bits": {"1": "quality", "2": "CO extrapolation"},
            "lines": [{"name": "H2 S(1)", "w": 2.1218, "kind": "emission", "detected": True}],
            "meta": {"grating": "G235H", "snr": 42.1}}


def pki1_header():
    return {"id": "x", "nx": 100, "ny": 20, "enc": "u8", "off": 0,
            "stretch": {"kind": "asinh", "lo": -1e-3, "hi": 2e-2, "a": 0.1},
            "wave": {"kind": "shared", "grid": "g235m"},
            "y": {"kind": "uniform", "y0": -0.5, "dy": 0.1, "unit": "arcsec"},
            "segments": [[0, 100, "G235H"]]}


def images_doc():
    return {"schema": "pk-images/1",
            "roles": [{"key": "sed", "label": "SED"}, {"key": "cut", "label": "Cutout"}],
            "items": [{"id": "a", "row": 0, "roles": {"sed": {"url": "png/a.png", "w": 480, "h": 360,
                                                            "thumb": "thumbs/a.jpg"}},
                       "caption": "Source a — ü", "tags": {"field": "brick"}},
                      {"id": "b", "roles": {"cut": {"url": "png/b.png"}}, "tags": {}}],
            "facets": [{"key": "field", "label": "Field", "values": ["brick", "cloudc"]}],
            "presets": [{"key": "brick", "label": "Brick only", "facets": {"field": ["brick"]}}],
            "catalog": "src"}


def hips_doc():
    return {"base": [{"id": "dss", "name": "DSS", "url": "https://alasky.cds.unistra.fr/DSS/", "default": True},
                     {"id": "2mass", "name": "2MASS", "url": "CDS/P/2MASS/color", "maxOrder": 9}],
            "overlays": [{"id": "f410", "name": "F410M", "url": "hips/f410m", "opacity": 0.6}],
            "fits": [{"id": "m1", "name": "mosaic", "url": "fits/m.fits", "auth": "secure", "ra": 266.8,
                      "dec": -28.4, "size_deg": 0.1}],
            "rungs": [{"id": "r1", "label": "Near-IR", "url": "hips/nir", "r_um": 4.1, "desc": "..."}],
            "bookmarks": [{"name": "IRS2", "ra": 290.9, "dec": 14.5, "fov": 0.05}],
            "target": {"ra": 266.8, "dec": -28.4, "fov": 0.2}}


def overlays_doc():
    return {"layers": [
        {"id": "msa", "name": "MSA shutters", "space": "sky", "kind": "polygons",
         "style": {"color": "#f00", "width": 1, "opacity": 0.8}, "default": True,
         "items": [{"poly": [[266.8, -28.4], [266.81, -28.4], [266.81, -28.39]]}]},
        {"id": "stars", "name": "Ref", "space": "sky", "kind": "markers", "style": {}, "default": False,
         "items": [{"ra": 266.8, "dec": -28.4, "shape": "square", "label": "R1"},
                   {"ra": 266.8, "dec": -28.4, "r_arcsec": 2.0}]},
        {"id": "iso", "name": "MIST 1 Myr", "space": "dim", "kind": "track", "style": {}, "default": True,
         "items": [{"dims": {"f182m": [10, 11, None], "f410m": [9, 10, 11]}},
                   {"x": "f182m-f410m", "y": "f410m", "pts": [[0, 10], [1, 12]]}]}]}


def narrative_doc():
    return {"beats": [{"id": "b1", "html": "<p>Intro</p>",
                       "view": {"pane": "sky", "sky": {"ra": 266.8, "dec": -28.4, "fov": 0.1},
                                "layers_on": ["msa"], "layers_off": [],
                                "highlight": {"ds": "src", "ids": ["a"]}, "suppress_view": False}},
                      {"id": "b2", "html": "", "view": {"pane": "scatter", "layers_on": [], "layers_off": ["msa"],
                                                       "axes": {"x": "f182m-f410m", "y": "f410m"}}}]}


def templates_doc():
    return {"meta": {"source": "jwst_template_spectra.json"},
            "grids": {"prism": {"file": "grid_prism.1a2b3c4d.bin"}, "medium": {"file": "grid_medium.1a2b3c4e.bin"},
                      "high": {"file": "grid_high.1a2b3c4f.bin"}},
            "templates": [{"key": "k5iii", "teff": 4100, "logg": 2.0, "label": "K5 III",
                           "files": {"prism": "t/k5iii_prism.0a0b0c0d.bin"}}],
            "laws": {"k31": {"prism": "t/k31_prism.0a0b0c0e.bin"}, "k55": {"prism": "t/k55_prism.0a0b0c0f.bin"}}}


def filters_doc():
    return {"filters": {"JWST/NIRCam.F410M": {
        "label": "F410M", "instrument": "NIRCam", "wl_um": 4.08, "weff_um": 0.43, "zp_jy": 3631.0,
        "system": "AB", "trans": "trans/f410m.1a2b3c4d.json",
        "a_over_av": {"G23_3.1": 0.031, "G23_5.5": 0.045}}}}


def extinction_doc():
    return {"law": "G23", "rv": [3.1, 5.5],
            "curve": {"3.1": {"w": [0.5, 1.0, 2.0], "k": [1.2, 0.4, 0.1]},
                      "5.5": {"w": [0.5, 1.0, 2.0], "k": [1.1, 0.45, 0.12]}}}


def isochrones_doc():
    return {"tracks": [{"label": "MIST 1 Myr", "age_myr": 1.0, "dist_pc": 8100.0, "feh": 0.0,
                        "mass": [0.1, 0.5, 1.0], "mags": {"JWST/NIRCam.F410M": [20.0, 18.0, None]}}]}


def features_doc():
    return {"families": {"ice": [{"name": "CO", "w_um": 4.67, "label": "CO ice", "kind": "absorption",
                                  "requires_coverage": True}],
                         "h2": []}}


def products_doc():
    return {"items": [{"label": "x1d G235H", "url": "https://mast.stsci.edu/x.fits", "kind": "fits",
                       "size": 123456, "auth": "public"},
                      {"label": "MAST", "url": "https://mast.stsci.edu/", "kind": "mast", "auth": "public"}]}


def deploy_doc():
    return {"schema": "pk-deploy/1", "family": "sgrb2", "portal": "sgrb2",
            "target": "/orange/adamginsburg/jwst/sgrb2/unified",
            "builds": [{"build_id": BUILD, "deployed": "2026-10-10T12:00:00Z", "portalkit": "1.0.0+gabc",
                        "adapter_sha": "abc", "legacy_modules": []}],
            "files": {"index.html": {"sha256": H64, "size": 1234, "build_id": BUILD},
                      "data/src/manifest.json": {"sha256": "b" * 64, "size": 99, "build_id": BUILD}},
            "replaced": [{"legacy": "../sgrb2_map_ccd.html",
                          "backup": ".portalkit_backup/20261010T120000Z-1a2b3c4d/sgrb2_map_ccd.html",
                          "md5": "c" * 32, "mode": "stub"}]}


def site_doc():
    return tomllib.loads(_doc_block("### 4.1 Site registry", "toml"))


VALID = {
    "page": page_doc, "portal": portal_doc, "catalog": catalog_doc, "records": records_doc,
    "pks1_header": pks1_header, "pki1_header": pki1_header, "images": images_doc, "hips": hips_doc,
    "overlays": overlays_doc, "narrative": narrative_doc, "templates": templates_doc,
    "filters": filters_doc, "extinction": extinction_doc, "isochrones": isochrones_doc,
    "features": features_doc, "products": products_doc, "deploy": deploy_doc, "site": site_doc,
}


# ---------------------------------------------------------------------------
# generic behaviour
# ---------------------------------------------------------------------------

def test_every_schema_id_has_validator():
    want = {"pk-page/1", "pk-portal/1", "pk-catalog/1", "pk-records/1", "pk-images/1", "pk-hips/1",
            "pk-overlays/1", "pk-narrative/1", "pk-templates/1", "pk-filters/1", "pk-extinction/1",
            "pk-isochrones/1", "pk-features/1", "products/1", "pk-deploy/1"}
    assert set(schema.SCHEMA_IDS.values()) == want
    for doc in list(schema.SCHEMA_IDS) + ["site", "pks1_header", "pki1_header"]:
        assert callable(getattr(schema, f"validate_{doc}"))
        assert callable(getattr(schema, f"assert_valid_{doc}"))
        assert f"validate_{doc}" in schema.__all__


@pytest.mark.parametrize("doc", sorted(VALID))
def test_valid_documents(doc):
    obj = VALID[doc]()
    assert getattr(schema, f"validate_{doc}")(obj) == []
    getattr(schema, f"assert_valid_{doc}")(obj)


@pytest.mark.parametrize("doc", sorted(VALID))
def test_non_object_rejected(doc):
    for bad in (None, [], "x", 3):
        errs = getattr(schema, f"validate_{doc}")(bad)
        assert errs and "expected object" in errs[0]


@pytest.mark.parametrize("doc", sorted(VALID))
def test_unknown_top_level_key(doc):
    obj = VALID[doc]()
    obj["bogus_key"] = 1
    obj["_comment"] = "annotations with a leading underscore are tolerated"
    errs = getattr(schema, f"validate_{doc}")(obj)
    assert _has(errs, "unknown key 'bogus_key'")
    assert not _has(errs, "_comment")


def test_assert_valid_raises_schema_error():
    obj = portal_doc()
    del obj["title"]
    with pytest.raises(SchemaError) as ei:
        schema.assert_valid_portal(obj)
    assert isinstance(ei.value, ValueError)
    assert ei.value.doc == "portal"
    assert _has(ei.value.errors, "missing required key 'title'")
    assert "1 schema error" in str(ei.value)


def test_schema_error_message_truncates():
    err = SchemaError("x", [f"e{i}" for i in range(120)])
    assert "70 more" in str(err) and len(err.errors) == 120


def test_dispatch_by_schema_id_and_name():
    assert schema.validate(portal_doc()) == []
    assert schema.validate(images_doc()) == []
    assert schema.validate(hips_doc(), "hips") == []
    assert schema.validate(hips_doc(), "pk-hips/1") == []
    with pytest.raises(KeyError):
        schema.validate({"x": 1})
    with pytest.raises(KeyError):
        schema.validate({}, "nope")
    bad = portal_doc()
    bad["visibility"] = "open"
    with pytest.raises(SchemaError):
        schema.assert_valid(bad)


def test_wrong_and_missing_schema_id():
    p = portal_doc()
    p["schema"] = "pk-portal/2"
    assert _has(schema.validate_portal(p), "expected 'pk-portal/1'")
    del p["schema"]
    errs = schema.validate_portal(p)
    assert _has(errs, "missing required key 'schema'")
    # reported once, at 'schema' (not again at '<root>')
    assert [x for x in errs if "'schema'" in x] == [
        "schema: missing required key 'schema' (expected 'pk-portal/1')"]
    for build, name in ((page_doc, "page"), (catalog_doc, "catalog"), (records_doc, "records"),
                        (deploy_doc, "deploy")):
        d = build()
        del d["schema"]
        errs = schema.validate(d, name)
        assert len([x for x in errs if "'schema'" in x]) == 1, (name, errs)
    # section 5.10 documents may omit schema, but a wrong one is an error
    h = hips_doc()
    assert schema.validate_hips(h) == []
    h["schema"] = "pk-images/1"
    assert _has(schema.validate_hips(h), "expected 'pk-hips/1'")


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_non_finite_numbers_rejected(bad):
    for doc in ("page", "catalog", "isochrones", "extinction", "hips"):
        obj = VALID[doc]()
        obj["_x"] = {"deep": [1, {"v": bad}]}
        errs = getattr(schema, f"validate_{doc}")(obj)
        assert _has(errs, "_x.deep[1].v", "non-finite"), (doc, errs)


def test_non_json_types_rejected():
    obj = filters_doc()
    obj["_set"] = {1, 2}
    obj["_k"] = {3: "int key"}
    errs = schema.validate_filters(obj)
    assert _has(errs, "_set", "not JSON-serialisable")
    assert _has(errs, "non-string key 3")
    assert schema.json_errors({"a": (1, 2.5, None, True, "é")}) == []


def test_bool_is_not_a_number():
    f = filters_doc()
    f["filters"]["JWST/NIRCam.F410M"]["wl_um"] = True
    assert _has(schema.validate_filters(f), "wl_um", "expected number")


# ---------------------------------------------------------------------------
# page.json
# ---------------------------------------------------------------------------

def test_page_contract_example_verbatim():
    assert schema.validate_page(page_doc()) == []


def test_page_layout_errors():
    p = page_doc()
    leaf = p["layout"]["row"][1]["col"][1]
    leaf["options"]["bogus"] = 1
    leaf["options"]["density"] = "maybe"
    p["layout"]["row"].append({"pane": "nonsense"})
    p["layout"]["row"].append({"pane": "list", "dataset": "missing"})   # duplicate id 'list'
    p["layout"]["row"].append({"row": [], "col": []})
    p["layout"]["row"].append({"col": []})
    p["layout"]["row"][0]["size"] = -3
    errs = schema.validate_page(p)
    assert _has(errs, "options.bogus", "unknown option for pane 'scatter'")
    assert _has(errs, "options.density", "'maybe' not one of")
    assert _has(errs, "pane", "'nonsense' not one of")
    assert _has(errs, "duplicate pane id 'list'")
    assert _has(errs, "unknown dataset 'missing'")
    assert _has(errs, "exactly one of 'row', 'col', 'pane'")
    assert _has(errs, "empty array")
    assert _has(errs, "size", "below minimum")


def test_page_option_dataset_kinds():
    p = page_doc()
    sky = p["layout"]["row"][1]["col"][0]
    sky["options"]["hips"] = "src"           # a catalog, not hips
    det = p["layout"]["row"][2]["col"][0]
    det["options"]["products"] = "nope"
    sed = p["layout"]["row"][2]["col"][3]
    sed["options"] = {"models": ["template", "sedcurve:nope", "fancy"], "units": "fnu"}
    spec = p["layout"]["row"][2]["col"][1]
    spec["options"]["pin"] = 9
    errs = schema.validate_page(p)
    assert _has(errs, "options.hips", "has kind 'catalog', expected 'hips'")
    assert _has(errs, "options.products", "unknown dataset 'nope'")
    assert _has(errs, "models[1]", "unknown dataset 'nope'")
    assert _has(errs, "models[2]", "unknown model 'fancy'")
    assert _has(errs, "options.pin", "above maximum 8")


def test_page_optional_options_accept_null_others_do_not():
    p = page_doc()
    scat = p["layout"]["row"][1]["col"][1]
    assert scat["pane"] == "scatter"
    scat["options"].update({"labels": None, "hollow_if": None, "pairs": None, "background": None,
                            "grid": None})
    det = p["layout"]["row"][2]["col"][0]
    det["options"].update({"products": None, "images": None})
    assert schema.validate_page(p) == []
    # options without '?' in section 6.10 reject null, whatever their checker
    sky = p["layout"]["row"][1]["col"][0]
    sky["options"]["hips"] = None
    det["options"]["template"] = None
    errs = schema.validate_page(p)
    assert _has(errs, "options.hips", "expected string, got NoneType")
    assert _has(errs, "options.template", "expected string, got NoneType")
    assert set(schema.OPTIONAL_PANE_OPTIONS) <= set(schema.PANE_OPTIONS)
    for pane, names in schema.OPTIONAL_PANE_OPTIONS.items():
        assert names <= set(schema.PANE_OPTIONS[pane]), pane


def test_page_datasets():
    p = page_doc()
    p["datasets"]["spec"]["record"] = "mp3"
    p["datasets"]["img"]["catalog"] = "tpl"
    p["datasets"]["x"] = {"kind": "weird", "url": "a"}
    p["datasets"]["y"] = {"kind": "catalog", "url": "a", "context": "yes", "extra": 1}
    del p["datasets"]["s2d"]["catalog"]
    errs = schema.validate_page(p)
    assert _has(errs, "datasets.spec.record", "'mp3' not one of")
    assert _has(errs, "datasets.img.catalog", "is not a catalog")
    assert _has(errs, "datasets.x.kind")
    assert _has(errs, "datasets.y.context", "expected boolean")
    assert _has(errs, "datasets.y", "unknown key 'extra'")
    assert _has(errs, "datasets.s2d", "missing required key 'catalog'")


def test_page_state_keys():
    p = page_doc()
    p["state"].update({"bogus": 1, "filters:nope": {}, "axes:ccd": {"x": "f182m", "y": "f410m", "xr": [0, 1]},
                       "axes:ghost": {}, "rv": 0, "av": -1, "theme": "neon", "tier:ccd": "T9",
                       "spec": {"pinned": [str(i) for i in range(9)]},
                       "sky": {"ra": 10, "dec": 95, "fov": 1},
                       "filters:src": {"cats": {"cls": ["red"]}, "ranges": {"f410m": [5, 2]}, "zzz": 1},
                       "focus": {"ds": "src", "id": "6927_10038"}})
    errs = schema.validate_page(p)
    assert _has(errs, "state.bogus", "unknown store key")
    assert _has(errs, "state.filters:nope", "unknown dataset")
    assert _has(errs, "state.axes:ghost", "unknown pane id")
    assert not _has(errs, "state.axes:ccd")
    assert not _has(errs, "state.focus")
    assert _has(errs, "state.rv", "below minimum")
    assert _has(errs, "state.av", "below minimum")
    assert _has(errs, "state.theme")
    assert _has(errs, "state.tier:ccd")
    assert _has(errs, "state.spec.pinned", "at most 8")
    assert _has(errs, "state.sky.dec", "above maximum")
    assert _has(errs, "state.filters:src.ranges.f410m", "lo > hi")
    assert _has(errs, "state.filters:src", "unknown key 'zzz'")


def test_page_caps_readme_aladin_budget():
    p = page_doc()
    p["caps"]["disabled"]["keyboard_nav"] = "no"
    p["caps"]["enabled"].append("keyboard_nav")
    p["readme"] = {"html": "x" * 20000, "url": "readme.1a2b3c4d.html"}
    p["aladin"]["cdn"] = "aladin.js"
    p["budget"] = {"html_bytes": -1}
    p["assets"] = "https://example.org/no-slash"
    p["pk"] = "1.0"
    errs = schema.validate_page(p)
    assert _has(errs, "caps.disabled.keyboard_nav", "both enabled and disabled")
    assert _has(errs, "caps.enabled", "duplicate 'keyboard_nav'")
    assert _has(errs, "readme", "exactly one of 'html' or 'url'")
    assert _has(errs, "readme.html", "16 kB")
    assert _has(errs, "aladin.cdn", "http(s) URL")
    assert _has(errs, "budget.html_bytes")
    assert _has(errs, "assets", "must end in '/'")
    assert _has(errs, "pk", "not semver")


def test_page_size_budget_and_unicode():
    p = page_doc()
    p["page"]["title"] = "NIRSpec · Σ λ — ü 日本"
    assert schema.validate_page(p) == []
    p["readme"] = {"url": "readme.1a2b3c4d.html", "provenance": {"blob": "é" * PAGE_JSON_MAX}}
    assert _has(schema.validate_page(p), "PAGE_JSON_MAX")


def test_page_minimal_and_legacy_grammar():
    p = {"schema": "pk-page/1", "pk": "1.0.0", "assets": "./_pk/v1.0.0/", "res": "./res/",
         "portal": {"id": "p", "title": "P", "visibility": "public", "nav": "portal.json"},
         "page": {"id": "index", "archetype": "landing", "title": "Home",
                  "legacy": {"files": ["index.html"], "grammar": ["bare-name", "klingon"]}},
         "datasets": {}, "layout": {"pane": "landing"}, "state": {},
         "caps": {"enabled": [], "disabled": {}},
         "aladin": {"cdn": "https://a/aladin.js", "mirror": "https://b/aladin.js"}}
    errs = schema.validate_page(p)
    assert errs == ["page.legacy.grammar[1]: unknown grammar 'klingon'"]
    p["page"]["legacy"]["grammar"] = ["bare-name"]
    p["layout"] = {"pane": "sky", "options": {"hips": "h"}}
    assert schema.validate_page(p) == ["layout.options.hips: unknown dataset 'h'"]


def test_page_deep_layout_guard():
    node = {"pane": "list"}
    for _ in range(40):
        node = {"row": [node]}
    p = page_doc()
    p["layout"] = node
    assert _has(schema.validate_page(p), "deeper than 32")


# ---------------------------------------------------------------------------
# portal.json and site TOML
# ---------------------------------------------------------------------------

def test_portal_errors():
    p = portal_doc()
    p["build"] = "yesterday"
    p["pages"].append({"id": "map", "title": "dup", "file": "../x.html", "archetype": "explorer"})
    p["pages"].append({"id": "p3", "title": "t", "file": "x.htm", "archetype": "explorer"})
    p["search"][0]["page"] = "ghost"
    p["related"][0]["auth"] = "maybe"
    errs = schema.validate_portal(p)
    assert _has(errs, "build", "not a build id")
    assert _has(errs, "duplicate id 'map'")
    assert _has(errs, "pages[2].file", "'..'")
    assert _has(errs, "pages[3].file", ".html")
    assert _has(errs, "search[0].page", "not listed")
    assert _has(errs, "related[0].auth")


def test_portal_empty_pages_ok():
    p = portal_doc()
    p["pages"] = []
    p["search"] = []
    assert schema.validate_portal(p) == []


def test_site_contract_example_and_errors():
    s = site_doc()
    assert schema.validate_site(s, stem="nirspec6927") == []
    assert _has(schema.validate_site(s, stem="other"), "differs from file stem")
    s["family"] = "Bad-Name"
    s["tier_default"] = "D"
    s["portal"][0]["visibility"] = "private"
    s["portal"][0]["url"] = "https://x/no-slash"
    s["portal"][0]["page"][0]["grammar"] = ["nope"]
    s["portal"][0]["legacy_dir"] = "relative/dir"
    errs = schema.validate_site(s)
    assert _has(errs, "family", "[a-z0-9_]+")
    assert _has(errs, "tier_default")
    assert _has(errs, "portal[0].visibility")
    assert _has(errs, "portal[0].url", "ending in '/'")
    assert _has(errs, "grammar[0]", "unknown grammar")
    assert _has(errs, "legacy_dir", "absolute")


def test_site_excluded_without_adapter():
    s = {"family": "gc_monitor", "status": "excluded", "portal": []}
    assert schema.validate_site(s) == []
    s["status"] = "active"
    errs = schema.validate_site(s)
    assert _has(errs, "adapter") and _has(errs, "portal", "empty array")


# ---------------------------------------------------------------------------
# catalog manifest
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n", [0, 1, 3, 4096, 4097])
def test_catalog_small_n_and_row_chunks(n):
    c = catalog_doc(n)
    c["rows"]["files"] = [f"rows/r{i:04d}.9a0b1c2d.bin" for i in range(-(-n // 4096))]
    for col in c["cols"]:
        col.pop("nvalid", None)
    assert schema.validate_catalog(c) == []
    c["rows"]["files"].append("rows/extra.bin")
    assert _has(schema.validate_catalog(c), "rows.files", "row chunks")


def test_catalog_large_n_lod_and_chunk_files():
    n = 5_000_000
    c = catalog_doc(n)
    c["order"] = {"kind": "lod", "seed": 20261010,
                  "levels": [32768, 65536, 131072, 262144, 524288, 1048576, 2097152, 4194304, n],
                  "sort": "tile-morton"}
    c["rows"] = {"chunk": 4096, "files": [f"rows/r{i}.bin" for i in range(-(-n // 4096))]}
    col = c["cols"][1]
    del col["file"]
    col["chunk_files"] = [f"cols/f182m.c{i:03d}.aaaaaaaa.bin" for i in range(5)]
    c["pos"] = {"enc": "i16off", "step_mas": 8.0, "tile_refs": [[266.83, -28.39], [266.84, -28.38]],
                "files": {"tile": "pos/tile.aa11bb22.bin", "dra": "pos/dra.cc33dd44.bin",
                          "ddec": "pos/ddec.ee55ff66.bin"}}
    assert schema.validate_catalog(c) == []
    col["chunk_files"] = col["chunk_files"][:4]
    c["order"]["levels"][-1] = n - 1
    errs = schema.validate_catalog(c)
    assert _has(errs, "chunk_files", "expected 5")
    assert _has(errs, "order.levels", "!= n")


def _chunked_doc(n, starts, nfiles=None, ids_chunks=None):
    """catalog_doc(n) with ``chunk_starts`` and every column/ids split into ``nfiles`` files."""
    c = catalog_doc(n)
    c["order"] = {"kind": "lod", "seed": 1, "levels": [32768, n], "sort": "tile-morton"}
    c["rows"] = {"chunk": 4096, "files": [f"rows/r{i:04d}.bin" for i in range(-(-n // 4096))]}
    if starts is not None:
        c["chunk_starts"] = starts
    k = len(starts) if nfiles is None else nfiles
    for col in c["cols"]:
        stem = col.pop("file").rsplit(".", 2)[0]
        col["chunk_files"] = [f"{stem}.c{i:03d}.aaaaaaaa.bin" for i in range(k)]
    c["ids"] = {"file": None, "enc": "str", "alias_file": None,
                "chunks": [f"ids.c{i:03d}.bbbbbbbb.bin" for i in range(k if ids_chunks is None else ids_chunks)]}
    return c


def test_catalog_chunk_starts_head_chunk_valid():
    # IMPLEMENTATION_NOTES 98: [0, LOD_BASE, LOD_BASE + CHUNK_ROWS, ...], every value < n
    for n, starts in ((131073, [0, 32768]), (2_000_000, [0, 32768, 1081344]),
                      (5_000_000, [0, 32768, 1081344, 2129920, 3178496, 4227072])):
        c = _chunked_doc(n, starts)
        assert schema.validate_catalog(c) == [], n
    # chunk_starts wins over chunk_rows: n = 2M has 2 chunks by the ceil rule, 3 by chunk_starts
    c = _chunked_doc(2_000_000, [0, 32768, 1081344], nfiles=2, ids_chunks=2)
    errs = schema.validate_catalog(c)
    assert _has(errs, "cols[0].chunk_files", "2 chunks, expected 3", "len(chunk_starts)=3")
    assert _has(errs, "ids.chunks", "2 chunks, expected 3")
    # a single-file column next to chunked ones is accepted (the reader maps one file to [0, n))
    c = _chunked_doc(131073, [0, 32768])
    c["cols"][4]["file"] = "cols/has_spec.19fe22c0.bin"
    del c["cols"][4]["chunk_files"]
    assert schema.validate_catalog(c) == []
    # n = 0 and n = 1 with a one-chunk chunk_starts [0]
    for n in (0, 1):
        c = _chunked_doc(n, [0])
        c["order"] = {"kind": "input"}
        for col in c["cols"]:
            col.pop("nvalid", None)
        assert schema.validate_catalog(c) == [], n
    # null chunk_starts means absent: the ceil(n / chunk_rows) rule applies
    c = _chunked_doc(131073, None, nfiles=1, ids_chunks=1)
    c["chunk_starts"] = None
    assert schema.validate_catalog(c) == []
    c = _chunked_doc(131073, None, nfiles=2, ids_chunks=2)
    errs = schema.validate_catalog(c)
    assert _has(errs, "chunk_files", "expected 1", "chunk_rows=1048576")
    assert _has(errs, "ids.chunks", "expected 1")


@pytest.mark.parametrize("starts, needle", [
    ([], "empty array"),
    ("0,32768", "expected array"),
    ([1, 32768], "first chunk start must be 0"),
    ([0, 32768, 32768], "strictly increasing"),
    ([0, 40000, 32768], "strictly increasing"),
    ([0, 32768, 131073], ">= n"),
    ([0, 32768, 200000], ">= n"),
    ([0, 32768.0], "must be an integer"),
    ([0, True], "must be an integer"),
    ([0, None], "must be an integer"),
    ([0, -5], "strictly increasing"),
])
def test_catalog_chunk_starts_errors(starts, needle):
    c = _chunked_doc(131073, [0, 32768])
    c["chunk_starts"] = starts
    errs = schema.validate_catalog(c)
    assert _has(errs, "chunk_starts", needle), errs
    # an invalid chunk_starts skips the chunk-count checks (no fallback to the chunk_rows rule)
    assert not any("chunks, expected" in m for m in errs), errs


def test_catalog_ids_chunks_follow_chunk_rule():
    # without chunk_starts, ids.chunks follow ceil(n / chunk_rows) like the columns
    n = 3_000_000
    c = catalog_doc(n)
    c["rows"] = {"chunk": 4096, "files": [f"rows/r{i}.bin" for i in range(-(-n // 4096))]}
    c["ids"] = {"file": None, "enc": "str", "chunks": [f"ids.c{i:03d}.bin" for i in range(3)]}
    for col in c["cols"]:
        col.pop("nvalid", None)
        stem = col.pop("file").rsplit(".", 2)[0]
        col["chunk_files"] = [f"{stem}.c{i:03d}.bin" for i in range(3)]
    assert schema.validate_catalog(c) == []
    c["ids"]["chunks"] = c["ids"]["chunks"][:2]
    assert _has(schema.validate_catalog(c), "ids.chunks", "2 chunks, expected 3", "chunk_rows=1048576")
    c["ids"]["chunks"] = []
    assert _has(schema.validate_catalog(c), "ids.chunks", "empty array")
    c["ids"] = {"file": "ids.bin", "enc": "str", "chunks": ["ids.c000.bin"]}
    assert _has(schema.validate_catalog(c), "ids", "at most one of 'file' or 'chunks'")


def test_catalog_column_errors():
    c = catalog_doc()
    cols = c["cols"]
    cols[0]["enc"] = "u12"
    cols[1]["lo"], cols[1]["hi"] = 20.0, 10.0
    cols[2]["lo"] = 0.0
    cols[3]["levels"] = ["a", "a"]
    cols[3]["colors"] = {"zzz": "#fff"}
    cols[4]["file"] = "/abs/path.bin"
    cols.append({"key": "f182m", "role": "mag", "enc": "u16q", "lo": 1, "hi": 2, "step": 0.001})
    cols.append({"key": "q", "role": "value", "enc": "u16q", "lo": 0, "hi": 1, "step": 0.1, "file": "q.bin",
                 "quantiles": [3, 2] + [5] * 99, "exact": True, "err": "nonexistent"})
    errs = schema.validate_catalog(c)
    assert _has(errs, "cols[0].enc")
    assert _has(errs, "cols[1]", "lo 20.0 > hi 10.0")
    assert _has(errs, "cols[2].lo", "u8log")
    assert _has(errs, "cols[3].levels[1]", "duplicate")
    assert _has(errs, "cols[3].colors.zzz", "not in 'levels'")
    assert _has(errs, "cols[4].file", "relative")
    assert _has(errs, "duplicate key 'f182m'")
    assert _has(errs, "cols[5]", "exactly one of 'file' or 'chunk_files'")
    assert _has(errs, "cols[6].quantiles", "non-decreasing")
    assert _has(errs, "cols[6]", "unknown key 'exact'")
    assert _has(errs, "cols[6].err", "unknown column 'nonexistent'")


def test_catalog_quantized_step_is_optional():
    """Section 5.2 lists 'step' as optional; readers derive (hi-lo)/65534 or /254."""
    c = catalog_doc()
    quant = [col for col in c["cols"] if col["enc"] in ("u16q", "u16log", "u8q")]
    assert quant
    for col in quant:
        col.pop("step", None)
    assert schema.validate_catalog(c) == []
    for enc in ("u16q", "u16log", "u8q"):
        d = catalog_doc()
        d["cols"][0].update(enc=enc, lo=0.0, hi=1.0)
        d["cols"][0].pop("step", None)
        assert schema.validate_catalog(d) == [], enc
        d["cols"][0]["step"] = 0.0
        assert _has(schema.validate_catalog(d), "cols[0].step", "below minimum"), enc


@pytest.mark.parametrize("key", ["2mass j", "f182m-f410m", "1abc", "a b", "x+y"])
def test_catalog_column_key_grammar(key):
    c = catalog_doc()
    c["cols"][0]["key"] = key
    assert _has(schema.validate_catalog(c), "cols[0].key", "must match")


@pytest.mark.parametrize("key", ["f182m", "_x", "A.b_2", "ra"])
def test_catalog_column_key_grammar_ok(key):
    c = catalog_doc()
    c["cols"][0]["key"] = key
    assert not _has(schema.validate_catalog(c), "cols[0].key", "must match")


def test_huge_integers_and_non_string_keys_are_errors_not_exceptions():
    text = '{"schema":"pk-catalog/1","id":"x","n":1' + "0" * 400 + ',"order":"none","cols":[]}'
    errs = schema.validate_catalog(json.loads(text))
    assert _has(errs, "n", "float64 range")
    p = page_doc()
    p["state"]["av"] = 10 ** 400
    p["state"][3] = 1
    errs = schema.validate_page(p)
    assert _has(errs, "state.av", "float64 range")
    assert _has(errs, "state", "non-string key 3")
    h = pks1_header()
    h["mask_bits"][7] = "x"
    assert _has(schema.validate_pks1_header(h, grids={"g235m": {}}), "non-string key 7")
    assert schema.validate_catalog(catalog_doc()) == []


def test_catalog_cat8_capacity_and_quantile_nulls():
    c = catalog_doc()
    c["cols"][3]["levels"] = [f"l{i}" for i in range(256)]
    c["cols"][3]["colors"] = {}
    c["cols"][0]["quantiles"] = [None] * 101
    errs = schema.validate_catalog(c)
    assert errs == ["cols[3].levels: 256 levels exceed cat8 capacity 255"]
    c["cols"][3]["enc"] = "cat16"
    assert schema.validate_catalog(c) == []


def test_catalog_cross_refs_and_presets():
    c = catalog_doc()
    c["virtual"].append({"key": "f410m"})
    c["groups"][0]["dims"].append("ghost")
    c["presets"][1]["F"] = [1, 0, 0]
    c["presets"].append({"key": "flt", "label": "f", "kind": "filter"})
    c["presets"].append({"key": "z", "label": "z", "kind": "magic"})
    c["manifold"]["dims"] = ["f410m", "f410m"]
    c["pos"] = {"enc": "i16off", "files": {"ra": "a", "dec": "b"}}
    c["bbox"]["ra"] = [10, 5]
    errs = schema.validate_catalog(c)
    assert _has(errs, "virtual[1].key", "clashes")
    assert _has(errs, "groups[0].dims[2]", "unknown dim")
    assert _has(errs, "presets[1].F", "2*len(dims)")
    assert _has(errs, "presets[3]", "requires 'filters'")
    assert _has(errs, "presets[4].kind")
    assert _has(errs, "manifold.dims[1]", "duplicate")
    assert _has(errs, "pos.files", "missing required key 'tile'")
    assert _has(errs, "pos", "requires 'step_mas'")
    assert _has(errs, "bbox.ra", "lo > hi")


def test_catalog_contract_example_after_unabridging():
    c = json.loads(_doc_block("### 5.2 Catalog bundle layout"))
    # the contract example is abridged with "..." placeholders; fill them in
    c["cols"][0]["quantiles"] = [10.0 + 0.6 * i for i in range(101)]
    c["cols"].extend({"key": k, "role": "mag", "enc": "u16q", "lo": 10.0, "hi": 75.534, "step": 0.001,
                      "file": f"cols/{k}.00000000.bin"} for k in ("f115w", "f150w", "f182m"))
    c["presets"][1]["dims"] = ["f115w", "f150w"]
    c["presets"][1]["F"] = [0.1, 0.2, 0.3, 0.4]
    c["manifold"]["dims"] = ["f115w", "f150w", "f182m", "f410m"]
    c["rows"]["files"] = [f"rows/r{i:04d}.00000000.bin" for i in range(-(-c["n"] // 4096))]
    assert schema.validate_catalog(c) == []


# ---------------------------------------------------------------------------
# records and binary headers
# ---------------------------------------------------------------------------

def test_records_errors():
    r = records_doc()
    r["entries"]["bad1"] = [5, 0, 10]
    r["entries"]["bad2"] = [0, -1, 0]
    r["entries"]["bad3"] = [0, 1]
    r["entries"]["bad4"] = {"png": "x.png", "extent": {"w": [1, 2], "y": [0, 1]}}
    r["entries"]["bad5"] = 7
    r["entries"][""] = "r/aa.bin"
    r["n"] = len(r["entries"])
    r["layout"] = "zip"
    r["grids"]["g2"] = {"kind": "uniform", "w0": 1.0, "dw": 0.0}
    errs = schema.validate_records(r)
    assert _has(errs, "entries.bad1", "shard index 5 out of range")
    assert _has(errs, "entries.bad2", "bad offset/length")
    assert _has(errs, "entries.bad3", "[shard, offset, length]")
    assert _has(errs, "entries.bad4", "only valid for pki1")
    assert _has(errs, "entries.bad5")
    assert _has(errs, "empty record id")
    assert _has(errs, "layout")
    assert _has(errs, "grids.g2.dw", "below minimum")
    assert _has(errs, "grids.g2", "requires 'n'")


def test_records_count_mismatch_and_empty():
    r = records_doc()
    r["n"] = 99
    assert _has(schema.validate_records(r), "n=99 but 3 entries")
    empty = {"schema": "pk-records/1", "kind": "json", "layout": "shard", "n": 0, "shards": [], "entries": {}}
    assert schema.validate_records(empty) == []


def test_records_pki1_png_mode_and_json_legacy():
    r = {"schema": "pk-records/1", "kind": "pki1", "layout": "file", "catalog": "src", "n": 1,
         "entries": {"w51-1": {"png": "spectra2d/w51-1.png", "extent": {"w": [1.0, 5.0], "y": [-1, 1]}}}}
    assert schema.validate_records(r) == []
    r["entries"]["w51-1"]["extent"]["w"] = [1.0]
    assert _has(schema.validate_records(r), "extent.w", "expected 2 values")
    leg = {"schema": "pk-records/1", "kind": "json-legacy", "dir": "specdata/", "pattern": "{id}.json"}
    assert schema.validate_records(leg) == []
    # the exact section 5.6 shape has no 'schema' key
    bare = {"kind": "json-legacy", "dir": "specdata/", "pattern": "{id}.json"}
    assert schema.validate(bare, "records") == []
    schema.assert_valid_records(bare)
    assert _has(schema.validate_records(dict(bare, schema="pk-records/2")), "expected 'pk-records/1'")
    # other kinds still require it
    nos = records_doc()
    del nos["schema"]
    assert _has(schema.validate_records(nos), "missing required key 'schema'")
    leg["pattern"] = "x.json"
    assert _has(schema.validate_records(leg), "'{id}'")


def test_pks1_header_errors():
    h = pks1_header()
    h["channels"][1]["off"] = 59201
    del h["channels"][2]["lo"]
    h["segments"].append([7000, 8000, "X"])
    h["mask_runs"].append([10, 5, 1])
    h["mask_bits"]["q"] = "x"
    h["wave"] = {"kind": "shared", "grid": "nope"}
    errs = schema.validate_pks1_header(h, grids={"g235m": {}})
    assert _has(errs, "channels[1].off", "aligned")
    assert _has(errs, "channels[2]", "requires 'lo'")
    assert _has(errs, "segments[2]", "beyond n=7400")
    assert _has(errs, "mask_runs[2]", "bounds")
    assert _has(errs, "mask_bits.q", "decimal")
    assert _has(errs, "wave.grid", "unknown shared grid")


def test_pks1_header_n1_and_array_wave():
    h = {"id": "s", "n": 1, "wave": {"kind": "array", "off": 0, "dtype": "f32"},
         "channels": [{"name": "flux", "enc": "f32", "off": 4}], "segments": [[0, 1, "PRISM"]]}
    assert schema.validate_pks1_header(h) == []


def test_pki1_header_errors():
    h = pki1_header()
    h["enc"] = "u32"
    h["stretch"]["lo"] = 1.0
    h["stretch"]["hi"] = 0.0
    h["nx"] = 0
    errs = schema.validate_pki1_header(h)
    assert _has(errs, "enc")
    assert _has(errs, "stretch", "lo must be < hi")
    assert _has(errs, "nx", "below minimum")


# ---------------------------------------------------------------------------
# section 5.10 documents
# ---------------------------------------------------------------------------

def test_images_errors():
    d = images_doc()
    d["items"].append({"id": "a", "roles": {}})
    d["items"].append({"id": "c", "roles": {"ghost": {"url": "x"}}, "row": -1})
    d["presets"].append({"key": "p2", "label": "x", "facets": {"field": ["mars"], "nope": ["x"]}})
    errs = schema.validate_images(d)
    assert _has(errs, "duplicate id 'a'")
    assert _has(errs, "items[2].roles", "no images")
    assert _has(errs, "items[3].roles.ghost", "not declared")
    assert _has(errs, "items[3].row", "below minimum")
    assert _has(errs, "presets[1].facets.field", "'mars'")
    assert _has(errs, "presets[1].facets.nope", "unknown facet")


def test_images_paginated_and_empty():
    d = {"roles": [], "items": [], "pages": ["images.0.1a2b3c4d.json", "images.1.5e6f7a8b.json"]}
    assert schema.validate_images(d) == []
    # a paginated index may drop 'items' (section 5.10)
    paged = {"roles": [{"key": "rgb", "label": "RGB"}], "pages": ["images.0.1a2b3c4d.json"],
             "facets": []}
    assert schema.validate_images(paged) == []
    # without pages, items is required
    assert _has(schema.validate_images({"roles": []}), "missing required key 'items'")
    assert _has(schema.validate_images({"roles": [], "pages": []}), "pages", "empty array")


def test_hips_errors():
    h = hips_doc()
    h["base"][1]["default"] = True
    h["overlays"][0]["opacity"] = 1.5
    h["fits"][0]["auth"] = "world"
    h["fits"][0]["dec"] = -91
    h["target"]["fov"] = 0
    h["bookmarks"].append({"name": "x", "ra": 1, "dec": 2})
    errs = schema.validate_hips(h)
    assert _has(errs, "more than one base layer marked default")
    assert _has(errs, "overlays[0].opacity", "above maximum")
    assert _has(errs, "fits[0].auth")
    assert _has(errs, "fits[0].dec", "below minimum")
    assert _has(errs, "target.fov")
    assert _has(errs, "bookmarks[1]", "missing required key 'fov'")


def test_overlays_errors():
    o = overlays_doc()
    o["layers"][0]["items"].append({"poly": [[400, 0], [1, 2]]})
    o["layers"][0]["items"].append({"foo": 1})
    o["layers"][1]["kind"] = "blob"
    o["layers"][1]["style"]["glow"] = 1
    o["layers"][2]["items"].append({"dims": {"a": [1, 2], "b": [1]}})
    o["layers"][2]["items"].append({"x": "a", "pts": [[1, 2, 3]]})
    errs = schema.validate_overlays(o)
    assert _has(errs, "items[1].poly[0]", "ra 400")
    assert _has(errs, "items[2]", "needs 'poly' or 'ra'/'dec'")
    assert _has(errs, "layers[1].kind")
    assert _has(errs, "layers[1].style", "unknown key 'glow'")
    assert _has(errs, "items[2].dims", "different lengths")
    assert _has(errs, "items[3].y", "expected string")
    assert _has(errs, "items[3].pts[0]", "expected 2 values")


def test_narrative_errors():
    n = narrative_doc()
    n["beats"][1]["id"] = "b1"
    del n["beats"][0]["view"]["layers_off"]
    n["beats"][0]["view"]["sky"]["fov"] = -1
    errs = schema.validate_narrative(n)
    assert _has(errs, "duplicate id 'b1'")
    assert _has(errs, "missing required key 'layers_off'")
    assert _has(errs, "sky.fov")


def test_templates_errors():
    t = templates_doc()
    t["templates"][0]["files"]["ultra"] = "t/x.bin"
    t["laws"]["k31"]["mega"] = "t/y.bin"
    t["templates"].append(dict(t["templates"][0]))
    errs = schema.validate_templates(t)
    assert _has(errs, "files.ultra", "unknown grid")
    assert _has(errs, "laws.k31.mega", "unknown grid")
    assert _has(errs, "duplicate key 'k5iii'")


def test_filters_errors():
    f = filters_doc()
    k = "JWST/NIRCam.F410M"
    f["filters"][k]["zp_jy"] = 0
    f["filters"][k]["a_over_av"]["G23_3.1"] = -0.1
    f["filters"]["X"] = {"label": "X"}
    errs = schema.validate_filters(f)
    assert _has(errs, "zp_jy", "below minimum")
    assert _has(errs, "a_over_av.G23_3.1", "below minimum")
    assert _has(errs, "filters.X", "missing required key 'wl_um'")


def test_extinction_errors():
    x = extinction_doc()
    x["rv"].append(4.0)
    x["curve"]["3.1"]["w"] = [0.5, 0.5, 2.0]
    x["curve"]["5.5"]["k"] = [1.0]
    errs = schema.validate_extinction(x)
    assert _has(errs, "no curve for R_V=4.0")
    assert _has(errs, "curve.3.1.w", "strictly increasing")
    assert _has(errs, "curve.5.5", "k has 1")


def test_isochrones_errors_and_large_values():
    i = isochrones_doc()
    i["tracks"][0]["dist_pc"] = 1e300
    assert schema.validate_isochrones(i) == []
    i["tracks"][0]["mags"]["F"] = [1, 2]
    i["tracks"][0]["mass"] = [1.0, 0.5, 2.0]
    i["tracks"][0]["age_myr"] = 0
    errs = schema.validate_isochrones(i)
    assert _has(errs, "mags.F", "mass has 3")
    assert _has(errs, "mass", "non-decreasing")
    assert _has(errs, "age_myr")


def test_features_and_products_errors():
    f = features_doc()
    f["families"]["ice"].append({"name": "H2O", "w_um": -3, "label": "x", "kind": "absorption"})
    f["families"]["ice"].append({"name": "x", "w_um": 3})
    errs = schema.validate_features(f)
    assert _has(errs, "ice[1].w_um")
    assert _has(errs, "ice[2]", "missing required key 'label'")
    p = products_doc()
    p["items"][0]["kind"] = "zip"
    p["items"][1]["auth"] = "nobody"
    errs = schema.validate_products(p)
    assert _has(errs, "items[0].kind") and _has(errs, "items[1].auth")


# ---------------------------------------------------------------------------
# pk-deploy/1
# ---------------------------------------------------------------------------

def test_deploy_errors():
    d = deploy_doc()
    d["target"] = "relative/unified"
    d["files"]["../escape.html"] = {"sha256": "XYZ", "size": -1, "build_id": "ghost"}
    d["replaced"][0]["mode"] = "delete"
    d["replaced"][0]["md5"] = "123"
    errs = schema.validate_deploy(d)
    assert _has(errs, "target", "absolute")
    assert _has(errs, "files.../escape.html", "'..'")
    assert _has(errs, "sha256", "hex sha256")
    assert _has(errs, "size", "below minimum")
    assert _has(errs, "build 'ghost' not listed")
    assert _has(errs, "replaced[0].mode")
    assert _has(errs, "replaced[0].md5")


def test_deploy_contract_example_shape():
    d = json.loads(_doc_block("### 11.3 Ownership manifest").replace('"...":"..."', '"sha256":"x"')
                   .replace("[...]", "[]"))
    errs = schema.validate_deploy(d)
    # the abridged example has '...' placeholders for hashes; only those should fail
    assert errs and all(("sha256" in e) or ("md5" in e) or ("build_id" in e) or ("size" in e) for e in errs), errs


def test_validators_are_fast_on_large_documents():
    # 50k image items stays well under a second on one core
    d = {"roles": [{"key": "r", "label": "R"}],
         "items": [{"id": f"i{k}", "roles": {"r": {"url": f"p/{k}.png"}}} for k in range(50_000)]}
    t0 = time.perf_counter()
    assert schema.validate_images(d) == []
    assert time.perf_counter() - t0 < 5.0


# ---------------------------------------------------------------------------
# IMPLEMENTATION_NOTES 78 and 88: raster layers, detail templates, manifold overlays
# ---------------------------------------------------------------------------

def _scatter(p):
    scat = p["layout"]["row"][1]["col"][1]
    assert scat["pane"] == "scatter"
    return scat


def test_page_raster_layers():
    p = page_doc()
    scat = _scatter(p)
    scat["options"]["grid"] = [["f182m", "f410m"], ["f182m-f410m", "f410m"]]
    scat["options"]["raster"] = [
        {"url": "img/a.png", "extent": [0, 1, 2, 3]},
        {"url": "img/b.png", "extent": [0.0, 1e30, -1e30, 3], "x": "f182m-f410m", "y": "f410m",
         "opacity": 0, "label": "Hexbin ✓"},
        {"url": "img/c.png", "extent": [0, 1, 2, 3], "cell": 1, "opacity": 1}]
    assert schema.validate_page(p) == []
    scat["options"]["raster"] = [
        {"url": "", "extent": [0, 1, 2, 3]},
        {"url": "a.png", "extent": [0, 1, 2, 3, 4]},
        {"url": "a.png", "extent": [0, 1, 2, None]},
        {"url": "a.png", "extent": [5, 5, 2, 3]},
        {"url": "a.png", "extent": [0, 1, 2, 3], "y": "f410m"},
        {"url": "a.png", "extent": [0, 1, 2, 3], "cell": 2},
        {"url": "a.png", "extent": [0, 1, 2, 3], "cell": True},
        {"url": "a.png", "extent": [0, 1, 2, 3], "opacity": -0.1},
        {"url": "a.png", "extent": [0, 1, 2, 3], "x": 3, "y": "f410m"},
        {"url": "a.png", "extent": [0, 1, 2, 3], "bogus": 1},
        7]
    errs = schema.validate_page(p)
    assert _has(errs, "raster[0].url", "empty string")
    assert _has(errs, "raster[1].extent", "expected 4 values")
    assert _has(errs, "raster[2].extent[3]", "finite")
    assert _has(errs, "raster[3].extent", "degenerate")
    assert _has(errs, "raster[4]", "go together")
    assert _has(errs, "raster[5]", "outside the grid (2 cell(s))")
    assert _has(errs, "raster[6].cell", "expected integer")
    assert _has(errs, "raster[7].opacity", "below minimum")
    assert _has(errs, "raster[8].x", "expected string")
    assert _has(errs, "raster[9]", "unknown key 'bogus'")
    assert _has(errs, "raster[10]", "expected object")
    scat["options"]["raster"] = {"url": "a.png"}
    assert _has(schema.validate_page(p), "options.raster", "expected array")


def test_page_detail_templates():
    assert schema.DETAIL_TEMPLATES == ("auto", "nirspec_source", "photometry", "compact")
    p = page_doc()
    det = p["layout"]["row"][2]["col"][0]
    assert det["pane"] == "detail"
    for tpl in schema.DETAIL_TEMPLATES:
        det["options"]["template"] = tpl
        assert schema.validate_page(p) == [], tpl
    det["options"]["template"] = "Photometry"
    assert _has(schema.validate_page(p), "options.template", "not one of")


def test_page_manifold_overlays_option():
    p = page_doc()
    p["layout"]["row"][1]["col"].append({"pane": "manifold", "dataset": "src",
                                         "options": {"dims": ["f182m", "f410m"],
                                                     "overlays": ["mist_1myr", "g23_vector"]}})
    assert schema.validate_page(p) == []
    p["layout"]["row"][1]["col"][-1]["options"]["overlays"] = "g23_vector"
    assert _has(schema.validate_page(p), "options.overlays", "expected array")
