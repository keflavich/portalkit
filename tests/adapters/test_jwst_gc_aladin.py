"""Contract tests for the jwst_gc_aladin adapter (WP32 extra; ARCHITECTURE.md section 14 /
docs/port_table.json "jwst_gc_aladin.html (critic)").

Tier A only. The JS-object-literal/HTML parsing helpers are exercised directly against inline
fixtures (no legacy mount needed); dataset-level tests skip when the legacy AVM tree is not
mounted.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FAMILY = "jwst_gc_aladin"
TMP_ROOTS = tuple(os.path.realpath(p) for p in {tempfile.gettempdir(), "/tmp", "/var/tmp"})

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from adapters import jwst_gc_aladin as mod  # noqa: E402

from portalkit.build import build_family, check_build  # noqa: E402
from portalkit.caps import resolve  # noqa: E402
from portalkit.context import BuildContext, expand_inputs, load_site  # noqa: E402
from portalkit.deploy import ExposureMap  # noqa: E402
from portalkit.spec import CatalogSpec, HipsSpec, PageSpec, TierUnavailable  # noqa: E402

SITE = load_site(FAMILY)
pytestmark = pytest.mark.adapters
_MOUNTED = mod.HTML_FILE.is_file()
skip_unmounted = pytest.mark.skipif(not _MOUNTED, reason="legacy jwst_gc_aladin page not mounted")


def _ctx(tier: str, staging: Path, limit=None) -> BuildContext:
    return BuildContext(family=FAMILY, site=SITE, staging=staging, tier=tier, limit=limit,
                        log=lambda m: None)


class _InputSet:
    def __init__(self):
        self.seen: set = set()

    def add(self, p):
        self.seen.add(p)


class _Ctx:
    def __init__(self):
        self.inputs = _InputSet()


# -- pure helpers: no legacy mount required --------------------------------------------------

def test_import_has_no_side_effects(tmp_path):
    code = ("import sys; sys.path.insert(0, %r); import adapters.jwst_gc_aladin as m; "
            "assert m.ADAPTER.name == 'jwst_gc_aladin'" % str(REPO))
    subprocess.run([sys.executable, "-c", code], cwd=tmp_path, check=True, timeout=60)
    assert list(tmp_path.iterdir()) == []


def test_parse_js_objects_strings_bools_numbers():
    text = "{id: 'a', on: true, opacity: 0.5, n: 3}, {id: 'b', on: false}"
    out = mod._parse_js_objects(text)
    assert out == [{"id": "a", "on": True, "opacity": 0.5, "n": 3}, {"id": "b", "on": False}]


def test_parse_js_objects_empty_text_gives_empty_list():
    assert mod._parse_js_objects("") == []


def test_parse_js_objects_unicode_note_preserved():
    text = "{id: 'x', note: 'F212N−F480M color (± 1″)'}"
    out = mod._parse_js_objects(text)
    assert out[0]["note"] == "F212N−F480M color (± 1″)"


def test_parse_js_objects_object_with_no_recognised_pairs_skipped():
    assert mod._parse_js_objects("{/* just a comment, no pairs */}") == []


def test_js_array_missing_name_gives_empty_list():
    assert mod._js_array("const OTHER = [{id: 'a'}];", "LAYERS") == []


def test_js_array_finds_const_let_or_var():
    for kw in ("const", "let", "var"):
        out = mod._js_array(f"{kw} LAYERS = [{{id: 'a', dir: 'd', name: 'n'}}];", "LAYERS")
        assert out == [{"id": "a", "dir": "d", "name": "n"}]


def test_base_surveys_n0_no_select():
    assert mod.base_surveys("<p>no select here</p>") == []


def test_base_surveys_parses_options_and_default():
    html = ('<select id="base" autocomplete="off">'
            '<option value="CDS/P/A/B">A B</option>'
            '<option value="CDS/P/C/D" selected>C D</option>'
            '</select>')
    out = mod.base_surveys(html)
    assert [o["url"] for o in out] == ["CDS/P/A/B", "CDS/P/C/D"]
    assert [o["default"] for o in out] == [False, True]
    assert out[0]["id"] != out[1]["id"]


def test_view_buttons_parses_and_skips_malformed():
    html = ('<button data-go="1.0 2.0 0.5">All</button>'
            '<button data-go="bad">Bad</button>'
            '<button data-go="1.0 2.0">TooFew</button>')
    out = mod.view_buttons(html)
    assert out == [{"ra": 1.0, "dec": 2.0, "fov": 0.5, "name": "All"}]


def test_hips_overlays_builds_url_and_defaults():
    text = ("const LAYERS = [{id: 'nir', dir: 'jwst_nir_hips', name: 'NIRCam', on: true, "
            "opacity: 1.0}];\nconst ALMA_LAYERS = [];\nconst STAR_LAYERS = [];")
    out = mod.hips_overlays(text)
    assert len(out) == 1
    assert out[0]["url"] == mod.AVM + "jwst_nir_hips/"
    assert out[0]["default"] is True
    assert out[0]["opacity"] == 1.0


def test_hips_overlays_n0_when_no_arrays():
    assert mod.hips_overlays("no arrays here") == []


def test_hips_overlays_skips_entries_without_dir():
    text = "const LAYERS = [{id: 'x', name: 'no dir'}];"
    assert mod.hips_overlays(text) == []


def test_discover_catalogs_skips_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "AVM_DIR", tmp_path)
    monkeypatch.setattr(mod, "SIDECARS", {"kmos": "jwst_gc_cat_kmos_pointings.json"})
    ctx = _Ctx()
    assert mod.discover_catalogs(ctx) == {}


def test_discover_catalogs_skips_malformed_json(tmp_path, monkeypatch):
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(mod, "AVM_DIR", tmp_path)
    monkeypatch.setattr(mod, "SIDECARS", {"bad": "bad.json"})
    assert mod.discover_catalogs(_Ctx()) == {}


def test_discover_catalogs_n1_reads_sources_and_registers_input(tmp_path, monkeypatch):
    p = tmp_path / "one.json"
    p.write_text(json.dumps({"sources": [{"ra": 1.0, "dec": 2.0}]}), encoding="utf-8")
    monkeypatch.setattr(mod, "AVM_DIR", tmp_path)
    monkeypatch.setattr(mod, "SIDECARS", {"one": "one.json"})
    ctx = _Ctx()
    out = mod.discover_catalogs(ctx)
    assert out == {"one": [{"ra": 1.0, "dec": 2.0}]}
    assert p in ctx.inputs.seen


def test_discover_catalogs_drops_rows_missing_ra_or_dec(tmp_path, monkeypatch):
    p = tmp_path / "mixed.json"
    p.write_text(json.dumps({"sources": [{"ra": 1.0, "dec": 2.0}, {"ra": 3.0},
                                          {"dec": 4.0}, {}]}), encoding="utf-8")
    monkeypatch.setattr(mod, "AVM_DIR", tmp_path)
    monkeypatch.setattr(mod, "SIDECARS", {"mixed": "mixed.json"})
    out = mod.discover_catalogs(_Ctx())
    assert out == {"mixed": [{"ra": 1.0, "dec": 2.0}]}


def test_merged_catalog_rows_n0_empty():
    assert mod.merged_catalog_rows({}) == []


def test_merged_catalog_rows_unique_ids_across_sources():
    rows = mod.merged_catalog_rows({"a": [{"ra": 1.0, "dec": 2.0}], "b": [{"ra": 3.0, "dec": 4.0}]})
    ids = [r["id"] for r in rows]
    assert len(set(ids)) == len(ids) == 2
    assert {r["source"] for r in rows} == {"a", "b"}


def test_merged_catalog_rows_skips_non_numeric_ra_dec():
    rows = mod.merged_catalog_rows({"a": [{"ra": "x", "dec": 2.0}, {"ra": 1.0, "dec": 2.0}]})
    assert len(rows) == 1


# -- dataset-level: needs the legacy mount ---------------------------------------------------

@pytest.fixture(scope="module")
def run():
    with tempfile.TemporaryDirectory() as td:
        ctx = _ctx("A", Path(td), limit=4000)
        adapter = mod.ADAPTER
        ds = adapter.datasets(ctx)
        pages = adapter.pages(ctx, ds)
        yield {"ctx": ctx, "ds": ds, "pages": pages}


@skip_unmounted
def test_datasets_valid(run):
    ds = run["ds"]
    hips = ds["sky"]
    assert isinstance(hips, HipsSpec)
    assert hips.base and any(b["default"] for b in hips.base)
    assert hips.overlays
    assert hips.target and {"ra", "dec", "fov"} <= set(hips.target)
    assert hips.bookmarks and len(hips.bookmarks) == 4
    if "stars" in ds:
        cat = ds["stars"]
        assert isinstance(cat, CatalogSpec)
        assert cat.ra == "ra" and cat.dec == "dec"
        roles = {c.key: c.role for c in cat.columns}
        assert "category" not in roles.values()
        assert "value" not in roles.values() and "mag" not in roles.values()
    for p in run["pages"]:
        assert isinstance(p, PageSpec)
        for k in p.datasets:
            assert k in ds, (p.id, k)


@skip_unmounted
def test_tier_b_unavailable(tmp_path):
    with pytest.raises(TierUnavailable):
        mod.JwstGcAladinAdapter().datasets(_ctx("B", tmp_path))


@skip_unmounted
def test_single_page_sky_archetype(run):
    pages = run["pages"]
    assert len(pages) == 1
    assert pages[0].archetype == "explorer"


@skip_unmounted
def test_inputs_exist_and_not_tmp(run):
    paths, unmatched = expand_inputs(SITE.inputs)
    assert not unmatched, unmatched
    for p in paths:
        assert os.path.exists(p), p
        rp = os.path.realpath(p)
        assert not any(rp == t or rp.startswith(t + os.sep) for t in TMP_ROOTS), p


@skip_unmounted
def test_resolved_flags_match_port_table(run):
    """docs/port_table.json's jwst_gc_aladin row: flags {sky, cat, skycat, dens}.

    This adapter deliberately disables detail_panel/source_list_search (no list/detail
    panel exists in the legacy UI -- see contract_issues) to keep "multi" unset, which the
    port_table row's flags also do not list; the trade-off is 15 of the row's 17 listed
    capabilities_to_fill rather than all 17 (see contract_issues/deviations)."""
    res = resolve(run["pages"][0], run["ds"], {})
    assert set(res.flags) == {"sky", "cat", "skycat", "dens"}
    assert "multi" not in res.flags


@skip_unmounted
def test_fixture_build_passes_check():
    with tempfile.TemporaryDirectory() as td:
        stage = Path(td)
        mp = pytest.MonkeyPatch()
        mp.setenv("PK_STAGE_ROOT", str(stage))
        mp.setenv("PK_CACHE_DIR", str(stage / "cache"))
        try:
            res = build_family(FAMILY, limit=50, stage_root=stage, log=lambda m: None,
                               emap=ExposureMap(roots={}, url_prefixes={}))
        finally:
            mp.undo()
        assert res.ok, res.report.get("errors")
        rep = check_build(res.staging, gate="G2")
        assert rep.ok, rep.errors
