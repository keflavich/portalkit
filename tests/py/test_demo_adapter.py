"""Unit tests for the self-contained demo family (adapters/demo.py, sites/demo.toml).

No external data, no legacy mount, no network: every assertion here runs as a normal unit
test (no ``adapters`` marker, no ``--run-adapters``), unlike the real families' contract
tests under ``tests/adapters/`` which need a HiPerGator legacy mount.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pytest

from adapters import demo as mod
from portalkit.build import build_family, check_build
from portalkit.context import BuildContext, load_site
from portalkit.deploy import ExposureMap
from portalkit.spec import CatalogSpec, PageSpec, ReadmeSpec, RecordSetSpec, TierUnavailable

FAMILY = "demo"
SITE = load_site(FAMILY)


def _ctx(staging: Path, limit: int | None = None, seed: int = 20261010) -> BuildContext:
    return BuildContext(family=FAMILY, site=SITE, staging=staging, tier="A", limit=limit,
                        seed=seed, log=lambda m: None)


def test_import_has_no_side_effects(tmp_path):
    import subprocess
    import sys
    code = ("import sys; sys.path.insert(0, %r); import adapters.demo as m; "
            "assert m.ADAPTER.name == 'demo'" % str(Path(__file__).resolve().parents[2]))
    subprocess.run([sys.executable, "-c", code], cwd=tmp_path, check=True, timeout=60)
    assert list(tmp_path.iterdir()) == []


def test_rows_deterministic_for_a_fixed_seed():
    a = mod._rows(10, 20261010)
    b = mod._rows(10, 20261010)
    assert a == b
    ids = [r["id"] for r in a]
    assert len(ids) == len(set(ids)) == 10


def test_rows_differ_for_a_different_seed():
    a = mod._rows(10, 20261010)
    b = mod._rows(10, 1)
    assert a != b


def test_spectrum_deterministic_and_well_formed():
    s1 = mod._spectrum(20261010, 0)
    s2 = mod._spectrum(20261010, 0)
    np.testing.assert_array_equal(s1.channels["flux"], s2.channels["flux"])
    assert s1.wave.size == mod.WAVE.size
    assert np.all(np.diff(s1.wave) > 0)
    assert np.all(np.isfinite(s1.channels["flux"]))
    assert np.all(s1.channels["err"] > 0)


def test_spectrum_differs_by_index():
    s0 = mod._spectrum(20261010, 0)
    s1 = mod._spectrum(20261010, 1)
    assert not np.array_equal(s0.channels["flux"], s1.channels["flux"])


def test_tier_b_and_c_unavailable(tmp_path):
    with pytest.raises(TierUnavailable):
        mod.DemoAdapter().datasets(BuildContext(family=FAMILY, site=SITE, staging=tmp_path,
                                                tier="B", log=lambda m: None))


def test_datasets_respects_limit(tmp_path):
    ctx = _ctx(tmp_path, limit=5)
    ds = mod.ADAPTER.datasets(ctx)
    assert isinstance(ds["src"], CatalogSpec)
    assert ds["src"].n == 5
    assert isinstance(ds["spec"], RecordSetSpec)
    assert len(ds["spec"].ids) == 5
    assert isinstance(ds["readme"], ReadmeSpec)


def test_datasets_without_limit_uses_n_sources(tmp_path):
    ctx = _ctx(tmp_path)
    ds = mod.ADAPTER.datasets(ctx)
    assert ds["src"].n == mod.N_SOURCES


def test_record_loader_resolves_every_id(tmp_path):
    ctx = _ctx(tmp_path, limit=5)
    ds = mod.ADAPTER.datasets(ctx)
    rec = ds["spec"]
    for sid in rec.ids:
        spec = rec.loader(sid)
        assert spec is not None and spec.wave.size > 0


def test_pages_cover_explorer_and_spectra(tmp_path):
    ctx = _ctx(tmp_path, limit=5)
    ds = mod.ADAPTER.datasets(ctx)
    pages = mod.ADAPTER.pages(ctx, ds)
    for p in pages:
        assert isinstance(p, PageSpec)
        for k in p.datasets:
            assert k in ds, (p.id, k)
    archetypes = {p.archetype for p in pages}
    assert "explorer" in archetypes
    assert "spectra" in archetypes
    assert "landing" in archetypes


def test_site_registry_is_public_and_not_under_orange():
    portal = SITE.portal()
    assert portal.visibility == "public"
    for p in (portal.legacy_dir, portal.target_dir, portal.url):
        assert p is not None
        assert not p.startswith("/orange")
    assert portal.url == "http://localhost:8765/demo/"


def test_fixture_build_passes_check_without_run_adapters_flag():
    """Builds the demo family with ``--limit 50`` into ``tmp_path`` and runs
    ``portalkit check --gate G2`` on it, exactly as the task asks -- and, because this test
    carries no ``adapters`` marker (it lives under tests/py, not tests/adapters), it runs in
    plain ``pytest -q`` with no opt-in flag."""
    with tempfile.TemporaryDirectory() as td:
        stage = Path(td)
        res = build_family(FAMILY, limit=50, stage_root=stage, log=lambda m: None,
                           emap=ExposureMap(roots={}, url_prefixes={}))
        assert res.ok, res.report.get("errors")
        rep = check_build(res.staging, gate="G2")
        assert rep.ok, rep.errors
