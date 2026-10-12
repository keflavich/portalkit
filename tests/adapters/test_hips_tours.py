"""Contract tests for the hips_tours adapter (WP31 extra; ARCHITECTURE.md section 14, wave 6).

Tier A only. Pure discovery/parsing helpers are exercised directly against temp fixtures (no
legacy mount needed); dataset-level tests skip when the legacy AVM tree is not mounted.
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
FAMILY = "hips_tours"
TMP_ROOTS = tuple(os.path.realpath(p) for p in {tempfile.gettempdir(), "/tmp", "/var/tmp"})

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from adapters import hips_tours as mod  # noqa: E402

from portalkit.build import build_family, check_build  # noqa: E402
from portalkit.context import BuildContext, expand_inputs, load_site  # noqa: E402
from portalkit.deploy import ExposureMap  # noqa: E402
from portalkit.spec import HipsSpec, PageSpec, ReadmeSpec, TierUnavailable  # noqa: E402

SITE = load_site(FAMILY)
pytestmark = pytest.mark.adapters
_MOUNTED = mod.RGB_DIR.is_dir() or mod.ACES_DIR.is_dir()
skip_unmounted = pytest.mark.skipif(not _MOUNTED, reason="legacy AVM tour tree not mounted")


def _ctx(tier: str, staging: Path, limit=None) -> BuildContext:
    return BuildContext(family=FAMILY, site=SITE, staging=staging, tier=tier, limit=limit,
                        log=lambda m: None)


class _InputSet:
    """Minimal stand-in for ``ctx.inputs`` (a set) used by the pure-helper tests below."""

    def __init__(self):
        self.seen: set = set()

    def add(self, p):
        self.seen.add(p)


class _Ctx:
    def __init__(self):
        self.inputs = _InputSet()


# -- pure helpers: no legacy mount required --------------------------------------------------

def test_import_has_no_side_effects(tmp_path):
    code = ("import sys; sys.path.insert(0, %r); import adapters.hips_tours as m; "
            "assert m.ADAPTER.name == 'hips_tours'" % str(REPO))
    subprocess.run([sys.executable, "-c", code], cwd=tmp_path, check=True, timeout=60)
    assert list(tmp_path.iterdir()) == []


def test_sky_target_parses_ra_dec_and_fov():
    text = "A.aladin({target: '266.5417 -28.7069', fov: 0.25});"
    tgt = mod._sky_target(text)
    assert tgt == {"ra": 266.5417, "dec": -28.7069, "fov": 0.25}


def test_sky_target_without_fov_omits_it():
    text = "A.aladin({target: '10.0 20.0'});"
    tgt = mod._sky_target(text)
    assert tgt == {"ra": 10.0, "dec": 20.0}
    assert "fov" not in tgt


def test_sky_target_none_when_no_target():
    assert mod._sky_target("no target here") is None


def test_sky_target_converts_galactic_frame_to_icrs():
    # crowded_l3_nrca_rgb_wavelength_tour.html: cooFrame 'galactic', target l=3.0 b=-0.001
    text = "cooFrame: 'galactic', target: '3.0000 -0.0010', fov: 0.045"
    tgt = mod._sky_target(text)
    # l~3 deg, b~0 deg sits close to the Galactic Center (ra ~266.4, dec ~-28.9)
    assert tgt["fov"] == 0.045
    assert 260 < tgt["ra"] < 272
    assert -32 < tgt["dec"] < -26


def test_rgb_rungs_reads_filters_variable_name_too():
    text = ('const FILTERS = [{"tag": 115, "name": "115-90-70", "um": 0.91, '
            '"hips": "Crowded_RGB_115-90-70_hips"}];')
    rungs = mod.rgb_rungs(text)
    assert len(rungs) == 1 and rungs[0]["label"] == "115-90-70"
    assert rungs[0]["url"] == mod.AVM + "Crowded_RGB_115-90-70_hips/"


def test_um_triplet_parses_three_dash_numbers():
    assert mod._um_triplet("187-182-115") == {"r_um": 1.87, "g_um": 1.82, "b_um": 1.15}


@pytest.mark.parametrize("name", ["187-182", "187-182-115-410", "abc-def-ghi", ""])
def test_um_triplet_empty_dict_for_non_triplets(name):
    assert mod._um_triplet(name) == {}


def test_rgb_rungs_builds_rung_dicts_from_steps():
    text = ('const STEPS = [{"name": "187-182-115", "um": 1.87, "hips": "Brick_RGB_187-182-115_hips"}, '
            '{"name": "405-410-466", "um": 4.05, "hips": "Brick_RGB_405-410-466_hips/"}];')
    rungs = mod.rgb_rungs(text)
    assert len(rungs) == 2
    assert rungs[0]["id"] == "rung0" and rungs[0]["label"] == "187-182-115"
    assert rungs[0]["url"] == mod.AVM + "Brick_RGB_187-182-115_hips/"
    assert rungs[0]["desc"] == "1.87 μm"
    assert rungs[0]["r_um"] == 1.87 and rungs[0]["b_um"] == 1.15
    # trailing slash on the source hips name is not doubled
    assert rungs[1]["url"] == mod.AVM + "Brick_RGB_405-410-466_hips/"


def test_rgb_rungs_empty_steps_gives_empty_list():
    assert mod.rgb_rungs("const STEPS = [];") == []


def test_discover_rgb_tours_n0_when_dir_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "RGB_DIR", tmp_path / "does_not_exist")
    assert mod.discover_rgb_tours(_Ctx()) == {}


def test_discover_rgb_tours_n1_one_field(monkeypatch, tmp_path):
    (tmp_path / "foo_rgb_wavelength_tour.html").write_text(
        "A.aladin({target: '1.0 2.0', fov: 0.1}); const STEPS = [];", encoding="utf-8")
    monkeypatch.setattr(mod, "RGB_DIR", tmp_path)
    ctx = _Ctx()
    out = mod.discover_rgb_tours(ctx)
    assert list(out) == ["foo"]
    assert out["foo"]["target"] == {"ra": 1.0, "dec": 2.0, "fov": 0.1}
    assert (tmp_path / "foo_rgb_wavelength_tour.html") in ctx.inputs.seen


def test_aces_rungs_missing_file_returns_empty_not_raise(tmp_path):
    rungs, target = mod.aces_rungs(tmp_path / "nope.json")
    assert rungs == [] and target is None


def test_aces_rungs_malformed_json_returns_empty_not_raise(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    rungs, target = mod.aces_rungs(p)
    assert rungs == [] and target is None


def test_aces_rungs_skips_waypoints_without_slider_until_one_has_it():
    doc = {"waypoints": [
        {"ra": 0.0, "dec": 0.0},
        {"ra": 10.0, "dec": 20.0, "fov": 0.5, "wavelength_slider": {"wavelengths": [
            {"label": "RGB: 1.87μm / 1.82μm / 1.15μm", "url": "Field_hips/",
             "description": "near-IR"},
        ]}},
    ]}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(doc, fh)
        p = Path(fh.name)
    try:
        rungs, target = mod.aces_rungs(p)
        assert len(rungs) == 1
        assert rungs[0]["url"] == mod.AVM + "Field_hips/"
        assert rungs[0]["desc"] == "near-IR"
        assert rungs[0].get("r_um") == 1.87 and rungs[0].get("b_um") == 1.15
        assert target == {"ra": 10.0, "dec": 20.0, "fov": 0.5}
    finally:
        p.unlink()


def test_aces_rungs_n0_when_no_waypoint_has_slider():
    doc = {"waypoints": [{"ra": 0.0, "dec": 0.0}]}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(doc, fh)
        p = Path(fh.name)
    try:
        assert mod.aces_rungs(p) == ([], None)
    finally:
        p.unlink()


def test_discover_aces_tours_skips_html_without_matching_waypoints_file(monkeypatch, tmp_path):
    (tmp_path / "field_wavelength_tour.html").write_text("x", encoding="utf-8")
    monkeypatch.setattr(mod, "ACES_DIR", tmp_path)
    assert mod.discover_aces_tours(_Ctx()) == {}


def test_discover_aces_tours_matches_field_and_variant_suffix(monkeypatch, tmp_path):
    (tmp_path / "field_wavelength_tour_linear.html").write_text("x", encoding="utf-8")
    (tmp_path / "waypoints_field_wavelength_linear.json").write_text(
        json.dumps({"waypoints": []}), encoding="utf-8")
    monkeypatch.setattr(mod, "ACES_DIR", tmp_path)
    out = mod.discover_aces_tours(_Ctx())
    assert list(out) == ["field_linear"]


# -- dataset-level: needs the legacy mount ---------------------------------------------------

@pytest.fixture(scope="module")
def run():
    with tempfile.TemporaryDirectory() as td:
        ctx = _ctx("A", Path(td))
        adapter = mod.ADAPTER
        ds = adapter.datasets(ctx)
        pages = adapter.pages(ctx, ds)
        yield {"ctx": ctx, "ds": ds, "pages": pages}


@skip_unmounted
def test_datasets_valid(run):
    ds = run["ds"]
    assert isinstance(ds["readme"], ReadmeSpec)
    hips_keys = [k for k in ds if k.startswith("hips_")]
    assert hips_keys, "expected at least one discovered tour"
    for k in hips_keys:
        spec = ds[k]
        assert isinstance(spec, HipsSpec)
        assert spec.base
        assert spec.rungs and all({"id", "label", "url"} <= set(r) for r in spec.rungs)
    for p in run["pages"]:
        assert isinstance(p, PageSpec)
        for k in p.datasets:
            assert k in ds, (p.id, k)
    assert {p.id for p in run["pages"]} == {"index"} | {pid for pid, _, _ in mod.ADAPTER._tours}


@skip_unmounted
def test_tier_b_unavailable(tmp_path):
    with pytest.raises(TierUnavailable):
        mod.HipsToursAdapter().datasets(_ctx("B", tmp_path))


@skip_unmounted
def test_page_ids_unique_and_wavelength_archetype(run):
    pages = run["pages"]
    ids = [p.id for p in pages]
    assert len(set(ids)) == len(ids)
    non_index = [p for p in pages if p.id != "index"]
    assert non_index and all(p.archetype == "wavelength" for p in non_index)
    assert all(p.id.startswith("rgb_") or p.id.startswith("aces_") for p in non_index)


@skip_unmounted
def test_inputs_exist_and_not_tmp(run):
    paths, unmatched = expand_inputs(SITE.inputs)
    assert not unmatched, unmatched
    for p in paths:
        assert os.path.exists(p), p
        rp = os.path.realpath(p)
        assert not any(rp == t or rp.startswith(t + os.sep) for t in TMP_ROOTS), p


@skip_unmounted
def test_fixture_build_passes_check():
    with tempfile.TemporaryDirectory() as td:
        stage = Path(td)
        mp = pytest.MonkeyPatch()
        mp.setenv("PK_STAGE_ROOT", str(stage))
        mp.setenv("PK_CACHE_DIR", str(stage / "cache"))
        try:
            res = build_family(FAMILY, limit=20, stage_root=stage, log=lambda m: None,
                               emap=ExposureMap(roots={}, url_prefixes={}))
        finally:
            mp.undo()
        assert res.ok, res.report.get("errors")
        rep = check_build(res.staging, gate="G2")
        assert rep.ok, rep.errors
