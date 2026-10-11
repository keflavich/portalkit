"""portalkit.config: page.json assembly and validation (ARCHITECTURE.md section 4.3)."""

from __future__ import annotations

import copy
import json

import numpy as np
import pytest

from portalkit import caps, config
from portalkit.schema import validate_page
from portalkit.spec import (ColumnSpec, ExtinctionSpec, HipsSpec, ImageItem, ImageSetSpec,
                            IsochroneSpec, LegacyRef, NarrativeSpec, OverlaySpec, PageSpec, Preset,
                            ReadmeSpec, RecordSetSpec, TemplateSpec, Track, catalog_from_rows)

PORTAL = {"id": "test", "title": "Test portal", "visibility": "public"}


def _cat(n=8, key="src", context=False):
    rows = [{"name": f"s{i}", "ra": 266.0 + 0.001 * i, "dec": -29.0, "f182m": 15.0 + 0.1 * i,
             "f405n": 13.5, "f410m": 13.0 + 0.05 * i, "cls": "a" if i % 2 else "b",
             "note": f"n{i}"} for i in range(n)]
    roles = {"f182m": "mag", "f405n": "mag", "f410m": "mag", "cls": "category", "note": "text"}
    cols = {m: {"filter": f"JWST/NIRCam.{m.upper()}"} for m in ("f182m", "f405n", "f410m")}
    return catalog_from_rows(rows, id="name", ra="ra", dec="dec", roles=roles, columns=cols, key=key,
                             context=context, title="Sources")


def _rec(key, kind, cat="src"):
    return RecordSetSpec(key=key, kind=kind, catalog=cat, ids=["s0"], loader=lambda i: None)


def _ds():
    ovl = OverlaySpec(layers=[
        {"id": "shutters", "name": "Shutters", "space": "sky", "kind": "polygons", "style": {},
         "default": True, "items": [{"poly": [[266.0, -29.0], [266.001, -29.0]]}]},
        {"id": "g23", "name": "G23 vector", "space": "dim", "kind": "vector", "style": {}, "default": True,
         "items": [{"x": "f182m-f410m", "y": "f410m", "pts": [[0, 10], [1, 11]]}]}])
    return {"src": _cat(), "spec": _rec("spec", "pks1"), "s2d": _rec("s2d", "pki1"),
            "hips": HipsSpec(base=[{"id": "b", "name": "B", "url": "https://alasky.cds.unistra.fr/DSS"}]),
            "ovl": ovl, "readme": ReadmeSpec(markdown="# About\n\nSome text.")}


def _assemble(page, ds, manifests=None, **kw):
    manifests = manifests or {}
    res = caps.resolve(page, ds, manifests)
    kw.setdefault("portal", PORTAL)
    return config.assemble(page, ds, manifests, res, **kw)


def _explorer(**kw):
    return PageSpec(id="map", archetype="explorer", title="Map",
                    datasets=["src", "spec", "s2d", "hips", "ovl", "readme"], **kw)


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------

def test_explorer_document():
    doc = _assemble(_explorer(subtitle="MSA sources"), _ds())
    assert validate_page(doc) == []
    assert doc["schema"] == "pk-page/1" and doc["pk"] == "1.0.0"
    assert doc["assets"] == "https://data.rc.ufl.edu/pub/adamginsburg/portalkit/v1.0.0/"
    assert doc["res"] == "https://data.rc.ufl.edu/pub/adamginsburg/portalkit-res/"
    assert doc["portal"] == {**PORTAL, "nav": "portal.json"}
    assert doc["page"] == {"id": "map", "archetype": "explorer", "title": "Map", "subtitle": "MSA sources"}
    assert doc["datasets"] == {
        "src": {"kind": "catalog", "url": "data/src/manifest.json"},
        "spec": {"kind": "records", "url": "data/spec/index.json", "catalog": "src", "record": "pks1"},
        "s2d": {"kind": "records", "url": "data/s2d/index.json", "catalog": "src", "record": "pki1"},
        "hips": {"kind": "hips", "url": "data/hips.json"},
        "ovl": {"kind": "overlays", "url": "data/overlays.json"}}
    assert doc["aladin"] == {"cdn": config.ALADIN_CDN, "mirror": config.ALADIN_MIRROR}
    assert doc["readme"]["html"].startswith('<h1 id="about">About</h1>')
    assert doc["readme"]["provenance"]["catalogs"]["src"]["n"] == 8
    assert "readme" not in doc["datasets"]
    sky = next(p for p in caps.iter_panes(doc["layout"]) if p["pane"] == "sky")
    assert sky["options"] == {"hips": "hips", "overlays": ["shutters"]}
    assert "budget" not in doc and "links" not in doc
    json.dumps(doc, allow_nan=False)


def test_vendor_assets_and_budget_and_legacy():
    page = _explorer(legacy=LegacyRef(files=["old.html"], grammar=["bare-name"], preset="role=nb"),
                     links={"spectra": "spectra.html#id={id}"})
    doc = _assemble(page, _ds(), vendor_assets=True, budget={"first_paint_bytes": 1234}, pk="1.0.0")
    assert doc["assets"] == "./_pk/v1.0.0/" and doc["res"] == "./res/"
    assert doc["budget"] == {"html_bytes": 0, "first_paint_bytes": 1234}
    assert doc["page"]["legacy"] == {"files": ["old.html"], "grammar": ["bare-name"], "preset": "role=nb"}
    assert doc["links"] == {"spectra": "spectra.html#id={id}"}


def test_assemble_resolves_caps_when_none():
    page = _explorer()
    doc = config.assemble(page, _ds(), {}, None, portal=PORTAL)
    assert "spectrum_viewer_1d" in doc["caps"]["enabled"]


def test_landing_gets_portal_ref():
    ds = {"src": _cat(), "readme": ReadmeSpec(markdown="hello")}
    doc = _assemble(PageSpec(id="index", archetype="landing", title="Overview", datasets=["readme"]), ds)
    assert doc["datasets"] == {"portal": {"kind": "portal", "url": "portal.json"}}
    assert doc["readme"]["html"] == "<p>hello</p>"
    assert "source_list_search" in doc["caps"]["enabled"]


def test_context_catalog_ref_and_background():
    ds = _ds()
    ds["ctx"] = _cat(key="ctx", context=True)
    page = PageSpec(id="map", archetype="explorer", title="Map", datasets=["src", "ctx"])
    doc = _assemble(page, ds)
    assert doc["datasets"]["ctx"] == {"kind": "catalog", "url": "data/ctx/manifest.json", "context": True}


def test_layout_targets_are_attached():
    ds = _ds()
    ds["tpl"] = TemplateSpec()
    layout = {"row": [{"pane": "list", "dataset": "src"},
                      {"pane": "spectrum1d", "dataset": "spec", "options": {"records": "spec", "templates": "tpl"}}]}
    page = PageSpec(id="s", archetype="spectra", title="S", datasets=["spec", "readme"], layout=layout)
    doc = _assemble(page, ds, res_refs={"templates": "{res}templates/index.3f9a2b1c.json"})
    assert doc["readme"]["html"].startswith('<h1 id="about">')
    assert doc["datasets"]["tpl"] == {"kind": "templates", "url": "{res}templates/index.3f9a2b1c.json"}
    assert doc["datasets"]["src"]["kind"] == "catalog"


def test_resource_without_url_is_an_error():
    ds = _ds()
    ds["ext"] = ExtinctionSpec()
    page = PageSpec(id="map", archetype="explorer", title="Map", datasets=["src", "ext"])
    with pytest.raises(config.ConfigError) as ei:
        _assemble(page, ds)
    assert any("res_refs" in e for e in ei.value.errors)
    doc = _assemble(page, ds, res_refs={"extinction": "{res}extinction.9a8b7c6d.json"})
    assert doc["datasets"]["ext"]["url"] == "{res}extinction.9a8b7c6d.json"


def test_refs_override_and_add():
    page = _explorer()
    doc = _assemble(page, _ds(), refs={"hips": {"kind": "hips", "url": "data/hips.json"},
                                       "feat": {"kind": "features", "url": "{res}features.0a1b2c3d.json"}})
    assert doc["datasets"]["feat"]["kind"] == "features"
    with pytest.raises(ValueError):
        _assemble(page, _ds(), refs={"hips": {"kind": "overlays", "url": "data/x.json"}})


def test_optional_none_options_kept_others_dropped():
    layout = {"row": [{"pane": "list", "dataset": "src", "options": {"sort": None, "search": None}},
                      {"pane": "scatter", "dataset": "src",
                       "options": {"x": "f182m-f410m", "y": "f410m", "background": None, "xlog": None}},
                      {"pane": "detail", "dataset": None}]}
    page = PageSpec(id="m", archetype="explorer", title="M", datasets=["src"], layout=layout)
    doc = _assemble(page, {"src": _cat()})
    leaves = list(caps.iter_panes(doc["layout"]))
    assert "options" not in leaves[0]
    assert leaves[1]["options"] == {"x": "f182m-f410m", "y": "f410m", "background": None}
    assert "dataset" not in leaves[2]


# ---------------------------------------------------------------------------
# key validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state,needle", [
    ({"color": {"by": "A_V"}}, "A_V"),
    ({"filters:src": {"ranges": {"o${o}": [0, 1]}}}, "o${o}"),
    ({"filters:src": {"cats": {"nope": ["a"]}}}, "nope"),
    ({"filters:src": {"flags": {"zz": True}}}, "zz"),
    ({"axes:scatter": {"x": "f182m-zz", "y": "f410m"}}, "zz"),
])
def test_bad_state_keys(state, needle):
    layout = {"row": [{"pane": "list", "dataset": "src"},
                      {"pane": "scatter", "dataset": "src", "options": {"x": "f182m-f410m", "y": "f410m"}}]}
    page = PageSpec(id="m", archetype="explorer", title="M", datasets=["src"], layout=layout, state=state)
    with pytest.raises(config.ConfigError) as ei:
        _assemble(page, {"src": _cat()})
    assert any(needle in e for e in ei.value.errors), ei.value.errors


def test_good_state_keys():
    layout = {"row": [{"pane": "list", "dataset": "src"},
                      {"pane": "scatter", "dataset": "src", "options": {"x": "f182m-f410m", "y": "f410m"}}]}
    state = {"color": {"by": "cls"}, "filters:src": {"ranges": {"f182m-f410m": [0, 3]}, "cats": {"cls": ["a"]}},
             "axes:scatter": {"x": "0.5*f405n+0.5*f410m", "y": "f182m"}, "av": 0, "rv": 3.1}
    page = PageSpec(id="m", archetype="explorer", title="M", datasets=["src"], layout=layout, state=state)
    doc = _assemble(page, {"src": _cat()})
    assert doc["state"] == state


@pytest.mark.parametrize("leaf,needle", [
    ({"pane": "scatter", "dataset": "src", "options": {"x": "f182m-zz", "y": "f410m"}}, "zz"),
    ({"pane": "scatter", "dataset": "src", "options": {"x": "f182m", "y": "f410m", "hollow_if": "cls"}}, "cls"),
    ({"pane": "scatter", "dataset": "src", "options": {"x": "f182m", "y": "f410m", "grid": [["a", "f182m"]]}}, "'a'"),
    ({"pane": "scatter", "dataset": "src", "options": {"x": "f182m", "y": "f410m", "overlays": ["nope"]}}, "nope"),
    ({"pane": "list", "dataset": "src", "options": {"columns": ["name", "missing"]}}, "missing"),
    ({"pane": "list", "dataset": "src", "options": {"sort": "-zzz"}}, "zzz"),
    ({"pane": "hist", "dataset": "src", "options": {"expr": "note"}}, "note"),
    ({"pane": "detail", "dataset": "src", "options": {"bars": ["p_yso"]}}, "p_yso"),
    ({"pane": "manifold", "dataset": "src", "options": {"dims": "group:none"}}, "none"),
    ({"pane": "manifold", "dataset": "src", "options": {"dims": ["f182m", "bogus"]}}, "bogus"),
    ({"pane": "sky", "dataset": "src", "options": {"overlays": ["g23"]}}, "g23"),
    ({"pane": "sky", "dataset": "src", "options": {"fits": ["f1"]}}, "f1"),
])
def test_bad_layout_options(leaf, needle):
    layout = {"row": [{"pane": "list", "dataset": "src"}, leaf]}
    ds = {"src": _cat(), "ovl": _ds()["ovl"]}
    page = PageSpec(id="m", archetype="explorer", title="M", datasets=["src", "ovl"], layout=layout)
    with pytest.raises(config.ConfigError) as ei:
        _assemble(page, ds)
    assert any(needle in e for e in ei.value.errors), ei.value.errors


def test_good_layout_options():
    layout = {"row": [
        {"pane": "list", "dataset": "src", "options": {"columns": ["name", "cls", "f182m"], "sort": "-f410m"}},
        {"col": [{"pane": "scatter", "id": "ccd", "dataset": "src",
                  "options": {"x": "f182m-f410m", "y": "f405n-f410m", "color": "cls", "labels": "name",
                              "overlays": ["g23"], "pairs": {"x2": "f182m", "y2": "f410m"},
                              "grid": [["f182m", "f410m"]]}},
                 {"pane": "hist", "dataset": "src", "options": {"expr": "f182m-f410m"}},
                 {"pane": "manifold", "dataset": "src", "options": {"dims": ["f182m", "f405n", "f410m"]}},
                 {"pane": "sky", "dataset": "src", "options": {"hips": "hips", "overlays": ["shutters"]}}]},
        {"pane": "detail", "dataset": "src", "options": {"prose": ["note"]}}]}
    page = PageSpec(id="m", archetype="explorer", title="M", datasets=["src", "hips", "ovl"], layout=layout)
    doc = _assemble(page, _ds())
    assert validate_page(doc) == []


def test_gallery_roles_and_sort():
    img = ImageSetSpec(key="img", items=[ImageItem(id="s0", roles={"png": "a.png"}, tags={"program": "1"})],
                       roles=[("png", "PNG")], facets=[("program", "Program")], catalog="src")
    ds = {"src": _cat(), "img": img}
    ok = {"pane": "gallery", "dataset": "img", "options": {"images": "img", "roles": ["png"], "sort": ["program", "-f410m"]}}
    page = PageSpec(id="g", archetype="gallery", title="G", datasets=["img"], layout=ok)
    assert validate_page(_assemble(page, ds)) == []
    bad = copy.deepcopy(ok)
    bad["options"]["roles"] = ["sed"]
    bad["options"]["sort"] = ["nothing"]
    with pytest.raises(config.ConfigError) as ei:
        _assemble(PageSpec(id="g", archetype="gallery", title="G", datasets=["img"], layout=bad), ds)
    assert any("'sed'" in e for e in ei.value.errors)
    assert any("'nothing'" in e for e in ei.value.errors)


def test_manifest_presets_and_dim_overlays_checked():
    cat = _cat()
    cat.presets.append(Preset(key="bad", label="Bad", kind="axes", data={"x": "A_V", "y": "f410m"}))
    with pytest.raises(config.ConfigError) as ei:
        _assemble(PageSpec(id="m", archetype="explorer", title="M", datasets=["src"]), {"src": cat})
    assert any("preset 'bad'" in e and "A_V" in e for e in ei.value.errors)
    ds = _ds()
    ds["ovl"].layers[1]["items"][0]["x"] = "f999w-f410m"
    with pytest.raises(config.ConfigError) as ei:
        _assemble(_explorer(), ds)
    assert any("f999w" in e for e in ei.value.errors)


def test_manifest_takes_precedence_over_spec():
    manifest = {"n": 8, "ids": {}, "pos": {}, "cols": [{"key": "f182m", "role": "mag", "enc": "u16q"},
                                                        {"key": "f410m", "role": "mag", "enc": "u16q"}],
                "virtual": [{"key": "f182m-f410m"}], "provenance": {"inputs": [{"path": f"/x/{i}"} for i in range(30)]}}
    page = PageSpec(id="m", archetype="explorer", title="M", datasets=["src"], state={"color": {"by": "cls"}})
    with pytest.raises(config.ConfigError) as ei:
        _assemble(page, {"src": _cat()}, {"src": manifest})
    assert any("cls" in e for e in ei.value.errors)
    page.state = {}
    doc = _assemble(page, {"src": _cat()}, {"src": manifest})
    inputs = doc["readme"]["provenance"]["catalogs"]["src"]["provenance"]["inputs"]
    assert len(inputs) == 21 and inputs[-1] == {"truncated": 10}


# ---------------------------------------------------------------------------
# URLs, providers, sizes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,ok", [
    ("data/src/manifest.json", True), ("portal.json", True), ("page.html#id={id}", True),
    ("{res}filters.1c2d3e4f.json", True), ("{assets}js/x.js", True),
    ("https://data.rc.ufl.edu/pub/adamginsburg/x.json", True),
    ("https://data.rc.ufl.edu/secure/adamginsburg/x.json", True),
    (config.ALADIN_CDN, True), (config.ALADIN_MIRROR, True),
    ("https://starformation.astro.ufl.edu/gc/manifest.json", True),
    ("../up.json", False), ("data/../../x", False), ("/abs/path.json", False),
    ("https://evil.example/x.json", False), ("http://data.rc.ufl.edu/other/x", False),
    ("https://data.rc.ufl.edu/pub/adamginsburg/../../etc", False),
    ("javascript:alert(1)", False), ("file:///etc/passwd", False), ("", False), (None, False),
    ("{res}../x", False), ("{res}/x", False), ("data/a b.json", False), ("data/\x00.json", False),
    ("data/%2e%2e/%2e%2e/secret.json", False), ("data/.%2E/x.json", False), ("{res}%2e%2e/x", False),
    ("https://data.rc.ufl.edu/pub/adamginsburg/%2e%2e/%2e%2e/secure/x", False),
    ("https://data.rc.ufl.edu/pub/adamginsburg/a\\..\\x", False), ("data\\..\\..\\x", False),
    ("data/a%20b.json", True),
])
def test_check_url(url, ok):
    assert (config.check_url(url) is None) == ok


def test_known_prefixes_extend():
    assert config.check_url("https://example.org/x") is not None
    assert config.check_url("https://example.org/x", ["https://example.org/"]) is None


def test_bad_ref_url_is_an_error():
    with pytest.raises(config.ConfigError) as ei:
        _assemble(_explorer(), _ds(), refs={"hips": {"kind": "hips", "url": "https://evil.example/h.json"}})
    assert any("datasets.hips.url" in e for e in ei.value.errors)
    with pytest.raises(config.ConfigError):
        _assemble(_explorer(links={"x": "../other.html"}), _ds())
    with pytest.raises(config.ConfigError):
        _assemble(_explorer(), _ds(), aladin={"cdn": "https://cdn.example/aladin.js"})


def test_missing_provider_is_an_error():
    page = PageSpec(id="m", archetype="explorer", title="M", datasets=["src"])
    res = caps.resolve(page, {"src": _cat()}, {})
    res.enabled = sorted(res.enabled + ["spectrum_viewer_1d"])
    with pytest.raises(config.ConfigError) as ei:
        config.assemble(page, {"src": _cat()}, {}, res, portal=PORTAL)
    assert any("spectrum_viewer_1d" in e for e in ei.value.errors)
    res2 = caps.resolve(page, {"src": _cat()}, {})
    res2.enabled = sorted(res2.enabled + ["teleport"])
    with pytest.raises(config.ConfigError) as ei:
        config.assemble(page, {"src": _cat()}, {}, res2, portal=PORTAL)
    assert any("teleport" in e for e in ei.value.errors)


def test_no_layout_is_an_error():
    page = PageSpec(id="m", archetype="explorer", title="M", datasets=["src"])
    res = caps.CapsResult(enabled=[], disabled={}, flags=[], added_panes=[])
    with pytest.raises(config.ConfigError):
        config.assemble(page, {"src": _cat()}, {}, res, portal=PORTAL)


def test_page_json_size_cap():
    page = _explorer(links={f"l{i}": f"p{i}.html#id={{id}}" + "x" * 1000 for i in range(70)})
    with pytest.raises(config.ConfigError) as ei:
        _assemble(page, _ds())
    assert any("PAGE_JSON_MAX" in e for e in ei.value.errors)


def test_schema_errors_surface():
    layout = {"pane": "scatter", "dataset": "src", "options": {"x": "f182m", "y": "f410m", "wobble": True}}
    page = PageSpec(id="m", archetype="explorer", title="M", datasets=["src"], layout=layout)
    with pytest.raises(config.ConfigError) as ei:
        _assemble(page, {"src": _cat()})
    assert any("unknown option" in e for e in ei.value.errors)
    assert isinstance(ei.value, ValueError)


def test_portal_visibility_checked():
    with pytest.raises(config.ConfigError):
        _assemble(_explorer(), _ds(), portal={"id": "x", "title": "X", "visibility": "private"})


# ---------------------------------------------------------------------------
# readme
# ---------------------------------------------------------------------------

def test_readme_blocks():
    r = ReadmeSpec(markdown="Intro", blocks=[
        {"type": "table", "columns": ["a", "b"], "rows": [[1, "<x>"], {"a": float("nan"), "b": "`c`"}, [2]]},
        {"type": "list", "items": ["one", "[two](two.html)"], "ordered": True},
        {"type": "code", "text": "</script>", "lang": "py\"><x"},
        {"type": "markdown", "text": "## Notes"}])
    html = config.readme_html(r)
    assert html.startswith("<p>Intro</p>")
    assert "<td>&lt;x&gt;</td>" in html and "<td><code>c</code></td>" in html
    assert html.count("<tr>") == 4
    assert '<ol><li>one</li><li><a href="two.html">two</a></li></ol>' in html
    assert '<pre><code class="language-pyx">&lt;/script&gt;</code></pre>' in html
    assert '<h2 id="notes">Notes</h2>' in html
    with pytest.raises(ValueError):
        config.readme_html(ReadmeSpec(markdown="", blocks=[{"type": "video"}]))
    with pytest.raises(ValueError):
        config.readme_html(ReadmeSpec(markdown="", blocks=[{"type": "table", "rows": []}]))


def test_readme_from_file(tmp_path):
    p = tmp_path / "README.md"
    p.write_text("# Ünïcode ☃\n", encoding="utf-8")
    assert config.readme_html(ReadmeSpec(markdown=p)) == '<h1 id="unicode">Ünïcode ☃</h1>'


def test_large_readme_goes_to_file():
    ds = _ds()
    ds["readme"] = ReadmeSpec(markdown="\n\n".join(f"paragraph {i} " + "x" * 200 for i in range(100)))
    with pytest.raises(config.ConfigError) as ei:
        _assemble(_explorer(), ds)
    assert any("16 kB" in e for e in ei.value.errors)
    files: dict[str, bytes] = {}
    doc = _assemble(_explorer(), ds, files=files)
    (name, body), = files.items()
    assert doc["readme"]["url"] == name and "html" not in doc["readme"]
    assert name == config.readme_file_name(body.decode("utf-8"))
    assert len(body) > config.README_INLINE_MAX


# ---------------------------------------------------------------------------
# small documents
# ---------------------------------------------------------------------------

def test_hips_document():
    h = HipsSpec(base=[{"id": "dss", "name": "DSS", "url": "https://alasky.cds.unistra.fr/DSS", "default": True}],
                 bookmarks=[{"name": "Brick", "ra": 266.54, "dec": -28.7, "fov": 0.2}])
    doc = config.hips_document(h)
    assert doc["schema"] == "pk-hips/1" and doc["overlays"] == [] and doc["fits"] == []
    assert doc["target"]["fov"] == 180.0 and "rungs" not in doc
    bad = HipsSpec(base=[{"id": "a", "name": "A", "url": "u"}, {"id": "a", "name": "B", "url": "v"}])
    with pytest.raises(config.ConfigError):
        config.hips_document(bad)
    with pytest.raises(config.ConfigError):
        config.hips_document(HipsSpec(base=[], target={"ra": 400.0, "dec": 0.0, "fov": 1.0}))


def test_overlays_document():
    doc = config.overlays_document(_ds()["ovl"])
    assert doc["schema"] == "pk-overlays/1" and len(doc["layers"]) == 2
    with pytest.raises(config.ConfigError):
        config.overlays_document(OverlaySpec(layers=[{"id": "x"}]))


def test_narrative_document():
    nar = NarrativeSpec(beats=[
        {"id": "b1", "markdown": "# Look\n\n<script>x</script>", "title": "One",
         "view": {"pane": "sky", "layers_on": ["a"], "layers_off": [], "sky": {"ra": 1.0, "dec": 2.0, "fov": 0.5}}},
        {"text": "plain"}])
    doc = config.narrative_document(nar)
    assert doc["beats"][0]["html"].startswith('<h1 id="look">Look</h1>')
    assert "<script" not in doc["beats"][0]["html"]
    assert doc["beats"][1]["id"] == "b2" and doc["beats"][1]["html"] == "<p>plain</p>"
    with pytest.raises(config.ConfigError):
        config.narrative_document(NarrativeSpec(beats=[{"id": "x", "html": "<b>raw</b>"}]))
    with pytest.raises(config.ConfigError):
        config.narrative_document(NarrativeSpec(beats=[{"id": "x", "markdown": "a", "bogus": 1}]))
    assert config.narrative_document(NarrativeSpec(beats=[]))["beats"] == []


def test_isochrones_document():
    iso = IsochroneSpec(tracks=[Track(label="1 Myr", age_myr=1.0, dist_pc=8100.0, feh=0.0,
                                      mass=np.array([0.1, 0.5, 1.0]),
                                      mags={"JWST/NIRCam.F410M": np.array([20.0, np.nan, 15.0])})])
    doc = config.isochrones_document(iso)
    assert doc["tracks"][0]["mags"]["JWST/NIRCam.F410M"] == [20.0, None, 15.0]
    json.dumps(doc, allow_nan=False)
    bad = IsochroneSpec(tracks=[Track(label="x", age_myr=-1.0, dist_pc=1.0, feh=0.0, mass=np.array([1.0]),
                                      mags={"f": np.array([1.0])})])
    with pytest.raises(config.ConfigError):
        config.isochrones_document(bad)


def test_dataset_ref_errors():
    with pytest.raises(TypeError):
        config.dataset_ref("r", ReadmeSpec(markdown=""))
    cat = _cat()
    assert config.dataset_ref("src", cat) == {"kind": "catalog", "url": "data/src/manifest.json"}
    assert config.dataset_ref("img", ImageSetSpec(key="img", items=[], roles=[])) == \
        {"kind": "images", "url": "data/img/images.json"}
    col = ColumnSpec(key="x")
    assert col.key == "x"


# ---------------------------------------------------------------------------
# review fixes: frames, computed overlays, provenance size, default URLs, secure data
# ---------------------------------------------------------------------------

def _manifold_page(options, state=None, cat=None):
    cat = cat or _cat()
    layout = {"row": [{"pane": "list", "dataset": "src"},
                      {"pane": "manifold", "id": "m", "dataset": "src", "options": options}]}
    page = PageSpec(id="mf", archetype="manifold", title="M", datasets=["src"], layout=layout,
                    state=state or {})
    return page, {"src": cat}


def test_frame_state_and_manifold_frame_option_checked():
    dims = ["f182m", "f405n", "f410m"]
    page, ds = _manifold_page({"dims": dims, "frame": "no_such_preset"},
                              {"frame:m": {"dims": ["A_V", "o${o}"]}})
    with pytest.raises(config.ConfigError) as ei:
        _assemble(page, ds)
    errs = ei.value.errors
    assert any("no_such_preset" in e for e in errs), errs
    assert any("state.frame:m.dims" in e and "A_V" in e for e in errs), errs
    assert any("o${o}" in e for e in errs), errs
    page, ds = _manifold_page({"dims": dims}, {"frame:m": {"preset": "nope", "dims": dims}})
    with pytest.raises(config.ConfigError) as ei:
        _assemble(page, ds)
    assert any("'nope'" in e for e in ei.value.errors)
    cat = _cat()
    cat.presets.append(Preset(key="f1", label="F1", kind="frame", data={"dims": dims, "F": [0.0] * 6}))
    for fr in ("pca", "f1"):
        page, ds = _manifold_page({"dims": dims, "frame": fr}, {"frame:m": {"preset": fr, "dims": dims}},
                                  cat=cat)
        assert validate_page(_assemble(page, ds)) == []


def test_manifold_group_dims_must_be_numeric():
    from portalkit.spec import Group
    cat = _cat()
    cat.groups = [Group(key="meta", label="Meta", dims=["cls", "note", "f182m"])]
    page, ds = _manifold_page({"dims": "group:meta"}, cat=cat)
    with pytest.raises(config.ConfigError) as ei:
        _assemble(page, ds)
    assert any("group:meta" in e and "cls" in e for e in ei.value.errors), ei.value.errors


def _iso():
    return IsochroneSpec(tracks=[Track(label="mist_1myr", age_myr=1.0, dist_pc=8100.0, feh=0.0,
                                       mass=np.array([0.1, 1.0]),
                                       mags={"JWST/NIRCam.F410M": np.array([20.0, 15.0])})])


def test_scatter_overlays_accept_isochrone_tracks_and_extinction_vector():
    ds = {"src": _cat(), "iso": _iso(), "ext": ExtinctionSpec()}
    layout = {"row": [{"pane": "list", "dataset": "src"},
                      {"pane": "scatter", "id": "ccd", "dataset": "src",
                       "options": {"x": "f182m-f410m", "y": "f405n-f410m",
                                   "overlays": ["g23_vector", "mist_1myr"]}}]}
    page = PageSpec(id="e", archetype="explorer", title="E", datasets=["src", "iso", "ext"], layout=layout)
    doc = _assemble(page, ds, res_refs={"extinction": "{res}extinction.9a8b7c6d.json"})
    assert validate_page(doc) == []
    layout["row"][1]["options"]["overlays"] = ["g23_vector", "parsec_1myr"]
    with pytest.raises(config.ConfigError) as ei:
        _assemble(page, ds, res_refs={"extinction": "{res}extinction.9a8b7c6d.json"})
    assert any("parsec_1myr" in e for e in ei.value.errors)
    assert not any("g23_vector" in e for e in ei.value.errors)


def test_large_fits_header_provenance_stays_small():
    header = {f"KEY{i:04d}": "x" * 60 for i in range(1600)}
    prov = {"adapter": "a@1", "tier": "B", "built": "2026-10-10T12:00:00Z",
            "inputs": [{"path": f"/orange/x/{i}.fits", "sha256": "0" * 64, "extra": {"big": header}}
                       for i in range(40)],
            "catalogs": [{"name": f"c{i}", "header": header} for i in range(3)],
            "blob": {"nested": header}}
    manifest = {"n": 8, "ids": {}, "pos": {},
                "cols": [{"key": k, "role": "mag", "enc": "u16q", "filter": f"JWST/NIRCam.{k.upper()}"}
                         for k in ("f182m", "f405n", "f410m")] + [{"key": "cls", "role": "category", "enc": "cat8"}],
                "provenance": prov}
    page = PageSpec(id="m", archetype="explorer", title="M", datasets=["src"])
    doc = _assemble(page, {"src": _cat()}, {"src": manifest})
    small = doc["readme"]["provenance"]["catalogs"]["src"]["provenance"]
    assert len(json.dumps(small).encode()) <= config.PROVENANCE_MAX
    assert small["adapter"] == "a@1" and small["tier"] == "B"
    assert "blob" not in small
    assert all("header" not in c for c in small.get("catalogs", []))
    assert small["catalogs"][0] == {"name": "c0", "header_cards": 1600}
    assert small["inputs"][-1] == {"truncated": 20}
    assert len(json.dumps(doc).encode()) < 65536


def test_provenance_summary_budget():
    prov = {"inputs": [{"path": "/p/" + "y" * 250, "sha256": "f" * 64} for i in range(20)]}
    small = config.provenance_summary(prov)
    assert len(json.dumps(small, ensure_ascii=False).encode()) <= config.PROVENANCE_MAX
    assert small["inputs"][-1]["truncated"] > 0
    kept = len(small["inputs"]) - 1
    assert kept + small["inputs"][-1]["truncated"] == 20


def test_default_urls_follow_naming_rules():
    assert config.dataset_ref("ovl", OverlaySpec(layers=[]))["url"] == "data/overlays.json"
    assert config.dataset_ref("nar", NarrativeSpec(beats=[]))["url"] == "data/narrative.json"
    assert config.dataset_ref("sky", HipsSpec(base=[]))["url"] == "data/hips.json"
    name, data = config.isochrones_file(_iso())
    import hashlib
    assert name == f"iso.{hashlib.sha256(data).hexdigest()[:8]}.json"
    assert json.loads(data) == config.isochrones_document(_iso())
    assert config.dataset_ref("iso", _iso())["url"] == f"data/{name}"


def test_two_overlay_datasets_need_refs():
    ds = _ds()
    ds["ovl2"] = OverlaySpec(layers=[])
    page = PageSpec(id="m", archetype="explorer", title="M", datasets=["src", "ovl", "ovl2"])
    with pytest.raises(config.ConfigError) as ei:
        _assemble(page, ds)
    assert any("data/overlays.json" in e and "ovl" in e for e in ei.value.errors)
    doc = _assemble(page, ds, refs={"ovl2": {"kind": "overlays", "url": "data/overlays2.json"}})
    assert doc["datasets"]["ovl2"]["url"] == "data/overlays2.json"


def test_public_page_may_not_fetch_secure_data():
    secure = "https://data.rc.ufl.edu/secure/adamginsburg/jwst/x/manifest.json"
    page = PageSpec(id="m", archetype="explorer", title="M", datasets=["src"],
                    links={"archive": "https://data.rc.ufl.edu/secure/adamginsburg/jwst/x.html"})
    refs = {"src": {"kind": "catalog", "url": secure}}
    with pytest.raises(config.ConfigError) as ei:
        _assemble(page, {"src": _cat()}, refs=refs)
    assert any("secure" in e and "datasets.src.url" in e for e in ei.value.errors)
    doc = _assemble(page, {"src": _cat()}, refs=refs,
                    portal={"id": "t", "title": "T", "visibility": "secure"})
    assert doc["datasets"]["src"]["url"] == secure
    # a link (not fetched) to the secure tree is allowed on a public page
    assert _assemble(page, {"src": _cat()})["links"]["archive"].startswith("https://data.rc.ufl.edu/secure/")


# ---------------------------------------------------------------------------
# IMPLEMENTATION_NOTES 78: raster layers and detail templates
# ---------------------------------------------------------------------------

def _raster_page(raster, grid=None, x="f182m-f410m", y="f410m"):
    opts = {"x": x, "y": y, "raster": raster}
    if grid is not None:
        opts["grid"] = grid
    layout = {"row": [{"pane": "list", "dataset": "src"},
                      {"pane": "scatter", "id": "ccd", "dataset": "src", "options": opts}]}
    return PageSpec(id="r", archetype="explorer", title="R", datasets=["src"], layout=layout)


def test_raster_layers_accept_x_y_cell():
    raster = [{"url": "img/hex_all.png", "extent": [-1.0, 3.0, 18.0, 10.0]},
              {"url": "img/hex_ccd.png", "extent": [0, 2, 16, 11], "x": "f182m-f410m", "y": "f410m",
               "opacity": 0.5, "label": "Hexbin"},
              {"url": "img/hex_c1.png", "extent": [0, 2, 16, 11], "cell": 1},
              {"url": "{assets}img/x.png", "extent": [0, 1, 1, 0], "cell": 0, "_note": "kept"}]
    doc = _assemble(_raster_page(raster, grid=[["f182m", "f410m"], ["f182m-f410m", "f405n"]]),
                    {"src": _cat()})
    assert validate_page(doc) == []
    got = next(p for p in caps.iter_panes(doc["layout"]) if p.get("id") == "ccd")["options"]["raster"]
    assert got == raster


@pytest.mark.parametrize("entry,needle", [
    ({"extent": [0, 1, 0, 1]}, "missing required key 'url'"),
    ({"url": "a.png"}, "missing required key 'extent'"),
    ({"url": "a.png", "extent": [0, 1, 0]}, "expected 4 values"),
    ({"url": "a.png", "extent": [0, 1, 0, float("nan")]}, "finite"),
    ({"url": "a.png", "extent": [0, 0, 0, 1]}, "degenerate"),
    ({"url": "a.png", "extent": [0, 1, 0, 1], "x": "f182m"}, "go together"),
    ({"url": "a.png", "extent": [0, 1, 0, 1], "x": "f182m", "y": "zz9"}, "zz9"),
    ({"url": "a.png", "extent": [0, 1, 0, 1], "x": "f182m", "y": "note"}, "note"),
    ({"url": "a.png", "extent": [0, 1, 0, 1], "cell": -1}, "below minimum"),
    ({"url": "a.png", "extent": [0, 1, 0, 1], "cell": 1.5}, "expected integer"),
    ({"url": "a.png", "extent": [0, 1, 0, 1], "cell": 2}, "outside the grid"),
    ({"url": "a.png", "extent": [0, 1, 0, 1], "opacity": 1.5}, "above maximum"),
    ({"url": "a.png", "extent": [0, 1, 0, 1], "zoom": 2}, "unknown key 'zoom'"),
    ({"url": "../a.png", "extent": [0, 1, 0, 1]}, "leaves the staged tree"),
    ("a.png", "expected object"),
])
def test_bad_raster_layers(entry, needle):
    page = _raster_page([entry], grid=[["f182m", "f410m"], ["f405n", "f410m"]])
    with pytest.raises(config.ConfigError) as ei:
        _assemble(page, {"src": _cat()})
    assert any(needle in e for e in ei.value.errors), ei.value.errors


def test_raster_cell_without_grid_is_an_error():
    page = _raster_page([{"url": "a.png", "extent": [0, 1, 0, 1], "cell": 0}])
    with pytest.raises(config.ConfigError) as ei:
        _assemble(page, {"src": _cat()})
    assert any("outside the grid (0 cell(s))" in e for e in ei.value.errors), ei.value.errors


@pytest.mark.parametrize("tpl", ["auto", "nirspec_source", "photometry", "compact"])
def test_detail_templates_accepted(tpl):
    layout = {"row": [{"pane": "list", "dataset": "src"},
                      {"pane": "detail", "dataset": "src", "options": {"template": tpl}}]}
    page = PageSpec(id="d", archetype="explorer", title="D", datasets=["src"], layout=layout)
    doc = _assemble(page, {"src": _cat()})
    assert validate_page(doc) == []


def test_unknown_detail_template_is_an_error():
    layout = {"row": [{"pane": "list", "dataset": "src"},
                      {"pane": "detail", "dataset": "src", "options": {"template": "fancy"}}]}
    page = PageSpec(id="d", archetype="explorer", title="D", datasets=["src"], layout=layout)
    with pytest.raises(config.ConfigError) as ei:
        _assemble(page, {"src": _cat()})
    assert any("'fancy' not one of" in e and "photometry" in e for e in ei.value.errors)


# ---------------------------------------------------------------------------
# IMPLEMENTATION_NOTES 88: manifold overlays are dim layers
# ---------------------------------------------------------------------------

def test_manifold_overlays_checked():
    ds = {"src": _cat(), "iso": _iso(), "ext": ExtinctionSpec()}
    layout = {"row": [{"pane": "list", "dataset": "src"},
                      {"pane": "manifold", "id": "m", "dataset": "src",
                       "options": {"dims": ["f182m", "f405n", "f410m"], "overlays": ["mist_1myr", "g23_vector"]}}]}
    page = PageSpec(id="m", archetype="manifold", title="M", datasets=["src", "iso", "ext"], layout=layout,
                    manifold=True)
    rr = {"extinction": "{res}extinction.9a8b7c6d.json"}
    doc = _assemble(page, ds, res_refs=rr)
    assert validate_page(doc) == []
    layout["row"][1]["options"]["overlays"] = ["mist_1myr", "bogus_vector"]
    with pytest.raises(config.ConfigError) as ei:
        _assemble(page, ds, res_refs=rr)
    assert any("bogus_vector" in e and "unknown dim overlay" in e for e in ei.value.errors)


# ---------------------------------------------------------------------------
# IMPLEMENTATION_NOTES 89: needs are checked against the assembled datasets
# ---------------------------------------------------------------------------

def _sed_page():
    return PageSpec(id="w", archetype="explorer", title="W", datasets=["src"])


def test_unattached_resources_move_caps_to_disabled_and_prune_panes():
    ds = {"src": _cat()}
    res = caps.resolve(_sed_page(), ds, {})          # default: shared resources attachable
    assert {"live_sed_viewer", "labeled_features_overlay"} <= set(res.enabled)
    assert "sed" in caps.pane_types(res.layout)
    rr = {"templates": "{res}t.json"}
    # no filters and no features dataset: both move to disabled with the item-89 reasons
    doc = config.assemble(_sed_page(), ds, {}, res, portal=PORTAL, res_refs=rr,
                          refs={"tpl": {"kind": "templates", "url": "{res}t.json"}})
    for cap in ("live_sed_viewer", "labeled_features_overlay"):
        assert cap not in doc["caps"]["enabled"]
        assert doc["caps"]["disabled"][cap] == caps.NEEDS[cap][1]
    assert caps.missing_providers(doc["layout"], doc["caps"]["enabled"]) == []
    # without a filters dataset the added SED pane places no points, so it goes like in
    # caps.resolve(resources=...) and the template overlay loses its provider
    assert "sed" not in caps.pane_types(doc["layout"])
    assert "model_template_overlay" in doc["caps"]["disabled"]
    assert "no pane on this page" in doc["caps"]["disabled"]["model_template_overlay"]
    plain = config.assemble(_sed_page(), ds, {}, None, portal=PORTAL, res_refs=rr,
                            refs={"tpl": {"kind": "templates", "url": "{res}t.json"}})
    assert caps.pane_types(plain["layout"]) == caps.pane_types(doc["layout"])
    assert set(plain["caps"]["disabled"]) == set(doc["caps"]["disabled"])
    assert validate_page(doc) == []
    # extinction_vectors_on_ccd needs an extinction dataset too (IMPLEMENTATION_NOTES 110)
    assert doc["caps"]["disabled"]["extinction_vectors_on_ccd"] == caps.NEEDS["extinction_vectors_on_ccd"][1]
    # with filters and features (and extinction) attached nothing moves
    doc = config.assemble(_sed_page(), ds, {}, res, portal=PORTAL,
                          refs={"flt": {"kind": "filters", "url": "{res}f.json"},
                                "feat": {"kind": "features", "url": "{res}feat.json"},
                                "ext": {"kind": "extinction", "url": "{res}e.json"}})
    assert doc["caps"]["enabled"] == res.enabled
    assert doc["caps"]["disabled"] == res.disabled
    # the shared attachment rule (caps.attachable_resources) gives exactly what is needed
    urls = {k: "{res}" + k + ".json" for k in caps.SHARED_RESOURCES}
    refs = caps.resource_refs(caps.attachable_resources(_sed_page(), ds, res), urls, taken=ds)
    doc = config.assemble(_sed_page(), ds, {}, res, portal=PORTAL, refs=refs)
    assert doc["caps"]["enabled"] == res.enabled
    assert doc["caps"]["disabled"] == res.disabled
    assert {r["kind"] for r in refs.values()} <= {r["kind"] for r in doc["datasets"].values()}


def test_prune_drops_panes_that_only_served_dropped_caps():
    ds = {"src": _cat()}
    page = PageSpec(id="w", archetype="explorer", title="W", datasets=["src"],
                    caps_disable={"model_template_overlay": "no templates here",
                                  "av_slider": "fixed extinction"})
    res = caps.resolve(page, ds, {})
    assert "sed" in {p["pane"] for p in res.added_panes}
    doc = config.assemble(page, ds, {}, res, portal=PORTAL)
    assert "live_sed_viewer" in doc["caps"]["disabled"]
    assert "sed" not in caps.pane_types(doc["layout"])
    assert caps.missing_providers(doc["layout"], doc["caps"]["enabled"]) == []
    assert validate_page(doc) == []


def test_drop_pane_collapses_splits():
    tree = {"row": [{"pane": "list"}, {"col": [{"pane": "detail"}, {"pane": "sed", "id": "sed"}], "size": 430}]}
    assert config._drop_pane(tree, "sed") == {"row": [{"pane": "list"}, {"pane": "detail", "size": 430}]}
    assert config._drop_pane({"pane": "sed"}, "sed") is None
    assert config._drop_pane({"col": [{"pane": "sed"}]}, "sed") is None
    assert config._drop_pane(tree, "nope") == tree


def test_assemble_without_caps_takes_resources_from_refs():
    ds = {"src": _cat()}
    doc = config.assemble(_sed_page(), ds, {}, portal=PORTAL)
    assert "live_sed_viewer" in doc["caps"]["disabled"]
    assert "sed" not in caps.pane_types(doc["layout"])
    doc = config.assemble(_sed_page(), ds, {}, portal=PORTAL,
                          refs={"flt": {"kind": "filters", "url": "{res}f.json"}})
    assert "live_sed_viewer" in doc["caps"]["enabled"]
    assert "sed" in caps.pane_types(doc["layout"])
    assert doc["datasets"]["flt"] == {"kind": "filters", "url": "{res}f.json"}


def test_hips_needed_for_layer_stacking_in_page_json():
    ds = _ds()
    doc = _assemble(PageSpec(id="m", archetype="explorer", title="M", datasets=["src"]), ds)
    assert "hips_layer_stacking" in doc["caps"]["disabled"]
    doc = _assemble(PageSpec(id="m", archetype="explorer", title="M", datasets=["src", "hips"]), ds)
    assert "hips_layer_stacking" in doc["caps"]["enabled"]


def test_deploy_reexports_raise_for_plan():
    import portalkit.deploy as dep
    from portalkit.deploy.deploy import raise_for_plan
    assert dep.raise_for_plan is raise_for_plan
    assert "raise_for_plan" in dep.__all__
