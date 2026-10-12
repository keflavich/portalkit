"""Contract tests for the spitzer_spectra adapter (WP31 extra; ARCHITECTURE.md section 14,
wave 6).

Tier A only. Pure helpers (``_num``, ``_order_segments``, ``load_sources``) are exercised
directly against temp fixtures (no legacy mount needed); dataset-level tests skip when the
legacy ``cmz/spitzer_spectra`` tree is not mounted.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[2]
FAMILY = "spitzer_spectra"
TMP_ROOTS = tuple(os.path.realpath(p) for p in {tempfile.gettempdir(), "/tmp", "/var/tmp"})

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from adapters import spitzer_spectra as mod  # noqa: E402

from portalkit.build import build_family, check_build  # noqa: E402
from portalkit.context import BuildContext, expand_inputs, load_site  # noqa: E402
from portalkit.deploy import ExposureMap  # noqa: E402
from portalkit.spec import (CatalogSpec, ImageSetSpec, OverlaySpec, PageSpec,  # noqa: E402
                            RecordSetSpec, ReadmeSpec, TierUnavailable)

SITE = load_site(FAMILY)
pytestmark = pytest.mark.adapters
_MOUNTED = (mod.LEGACY_DIR / "spitzer_spectra_sources.json").is_file()
skip_unmounted = pytest.mark.skipif(not _MOUNTED, reason="legacy cmz/spitzer_spectra tree not mounted")


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
    code = ("import sys; sys.path.insert(0, %r); import adapters.spitzer_spectra as m; "
            "assert m.ADAPTER.name == 'spitzer_spectra'" % str(REPO))
    subprocess.run([sys.executable, "-c", code], cwd=tmp_path, check=True, timeout=60)
    assert list(tmp_path.iterdir()) == []


def test_num_handles_nan_inf_none_and_strings():
    assert mod._num(None) is None
    assert mod._num(float("nan")) is None
    assert mod._num(float("inf")) is None
    assert mod._num("3.5") == 3.5
    assert mod._num("not a number") is None


def test_rel_url_none_passthrough_and_join():
    assert mod._rel_url(None) is None
    assert mod._rel_url("pngs/x.png") == mod.PUBLIC + "pngs/x.png"


def test_order_segments_empty_input_gives_no_segments():
    assert mod._order_segments(np.array([])) == []


def test_order_segments_n1_single_value():
    segs = mod._order_segments(np.array([2.0]))
    assert segs == [(0, 1, "order2")]


def test_order_segments_groups_contiguous_equal_values():
    order = np.array([1.0, 1.0, 2.0, 2.0, 2.0, 1.0])
    segs = mod._order_segments(order)
    assert segs == [(0, 2, "order1"), (2, 5, "order2"), (5, 6, "order1")]


def test_order_segments_handles_nan_runs():
    order = np.array([np.nan, np.nan, 1.0])
    segs = mod._order_segments(order)
    assert segs == [(0, 2, "order?"), (2, 3, "order1")]


def test_load_sources_missing_file_returns_empty_not_raise(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "LEGACY_DIR", tmp_path)
    assert mod.load_sources(_Ctx()) == []


def test_load_sources_malformed_json_returns_empty_not_raise(monkeypatch, tmp_path):
    (tmp_path / "spitzer_spectra_sources.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(mod, "LEGACY_DIR", tmp_path)
    assert mod.load_sources(_Ctx()) == []


def test_load_sources_non_list_json_returns_empty_not_raise(monkeypatch, tmp_path):
    (tmp_path / "spitzer_spectra_sources.json").write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    monkeypatch.setattr(mod, "LEGACY_DIR", tmp_path)
    assert mod.load_sources(_Ctx()) == []


def test_load_sources_reads_list_and_registers_input(monkeypatch, tmp_path):
    rows = [{"Name": "a"}, {"Name": "b"}]
    (tmp_path / "spitzer_spectra_sources.json").write_text(json.dumps(rows), encoding="utf-8")
    monkeypatch.setattr(mod, "LEGACY_DIR", tmp_path)
    ctx = _Ctx()
    assert mod.load_sources(ctx) == rows
    assert (tmp_path / "spitzer_spectra_sources.json") in ctx.inputs.seen


def test_spectrum_none_row_returns_none():
    adapter = mod.SpitzerSpectraAdapter()
    assert adapter._spectrum(_Ctx(), None) is None


def test_spectrum_missing_spec_file_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "LEGACY_DIR", tmp_path)
    adapter = mod.SpitzerSpectraAdapter()
    assert adapter._spectrum(_Ctx(), {"Name": "nope"}) is None


def test_spectrum_mismatched_array_lengths_returns_none(monkeypatch, tmp_path):
    (tmp_path / "specdata").mkdir()
    (tmp_path / "specdata" / "x.json").write_text(
        json.dumps({"w": [1.0, 2.0], "f": [1.0], "e": [1.0, 2.0], "order": [1, 1]}), encoding="utf-8")
    monkeypatch.setattr(mod, "LEGACY_DIR", tmp_path)
    adapter = mod.SpitzerSpectraAdapter()
    assert adapter._spectrum(_Ctx(), {"Name": "x", "SpecFile": "specdata/x.json"}) is None


def test_spectrum_sorts_by_wave_and_segments_by_order(monkeypatch, tmp_path):
    (tmp_path / "specdata").mkdir()
    (tmp_path / "specdata" / "x.json").write_text(json.dumps({
        "name": "x", "w": [3.0, 1.0, 2.0], "f": [30.0, 10.0, 20.0], "e": [0.1, 0.1, 0.1],
        "order": [2, 1, 1]}), encoding="utf-8")
    monkeypatch.setattr(mod, "LEGACY_DIR", tmp_path)
    adapter = mod.SpitzerSpectraAdapter()
    spec = adapter._spectrum(_Ctx(), {"Name": "x", "SpecFile": "specdata/x.json", "Object": "O"})
    assert list(spec.wave) == [1.0, 2.0, 3.0]
    assert list(spec.channels["flux"]) == [10.0, 20.0, 30.0]
    assert spec.segments == [(0, 2, "order1"), (2, 3, "order2")]


def test_spectrum_negative_or_nonpositive_err_becomes_nan(monkeypatch, tmp_path):
    (tmp_path / "specdata").mkdir()
    (tmp_path / "specdata" / "x.json").write_text(json.dumps({
        "w": [1.0, 2.0], "f": [1.0, 2.0], "e": [-0.1, 0.1], "order": [1, 1]}), encoding="utf-8")
    monkeypatch.setattr(mod, "LEGACY_DIR", tmp_path)
    adapter = mod.SpitzerSpectraAdapter()
    spec = adapter._spectrum(_Ctx(), {"Name": "x", "SpecFile": "specdata/x.json"})
    assert np.isnan(spec.channels["err"][0])
    assert spec.channels["err"][1] == 0.1


# -- dataset-level: needs the legacy mount ---------------------------------------------------

@pytest.fixture(scope="module")
def run():
    with tempfile.TemporaryDirectory() as td:
        ctx = _ctx("A", Path(td), limit=20)
        adapter = mod.ADAPTER
        ds = adapter.datasets(ctx)
        pages = adapter.pages(ctx, ds)
        yield {"ctx": ctx, "ds": ds, "pages": pages}


@skip_unmounted
def test_datasets_valid(run):
    ds = run["ds"]
    assert isinstance(ds["src"], CatalogSpec)
    assert isinstance(ds["spec"], RecordSetSpec)
    assert isinstance(ds["specimg"], ImageSetSpec)
    assert isinstance(ds["slits"], OverlaySpec)
    assert isinstance(ds["readme"], ReadmeSpec)
    assert ds["src"].n == 20
    for p in run["pages"]:
        assert isinstance(p, PageSpec)
        for k in p.datasets:
            assert k in ds, (p.id, k)
    assert {p.id for p in run["pages"]} == {"index", "spitzer_map", "spitzer_specviewer"}


@skip_unmounted
def test_tier_b_unavailable(tmp_path):
    with pytest.raises(TierUnavailable):
        mod.SpitzerSpectraAdapter().datasets(_ctx("B", tmp_path))


@skip_unmounted
def test_records_loader_is_single_argument_and_resolves_real_rows(run):
    ds = run["ds"]
    rec = ds["spec"]
    assert rec.ids
    spec = rec.loader(rec.ids[0])
    assert spec is not None and spec.wave.size > 0
    assert rec.loader("__does_not_exist__") is None


@skip_unmounted
def test_slit_polygons_are_closed_with_at_least_4_vertices(run):
    ovl = ds = run["ds"]["slits"]
    assert ds.layers and ds.layers[0]["kind"] == "polygons"
    for it in ds.layers[0]["items"]:
        assert len(it["poly"]) >= 4
        assert it["poly"][0] == it["poly"][-1]


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
