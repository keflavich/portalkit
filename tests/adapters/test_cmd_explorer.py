"""Contract tests for the cmd_explorer adapter (WP32 extra; ARCHITECTURE.md section 14 /
docs/port_table.json "cmd_explorer (critic)").

Tier A only. The hex-cell reconstruction math is exercised directly (no legacy mount needed);
dataset-level tests skip when the legacy data file is not mounted.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FAMILY = "cmd_explorer"
TMP_ROOTS = tuple(os.path.realpath(p) for p in {tempfile.gettempdir(), "/tmp", "/var/tmp"})

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from adapters import cmd_explorer as mod  # noqa: E402

from portalkit.build import build_family, check_build  # noqa: E402
from portalkit.caps import resolve  # noqa: E402
from portalkit.context import BuildContext, expand_inputs, load_site  # noqa: E402
from portalkit.deploy import ExposureMap  # noqa: E402
from portalkit.spec import CatalogSpec, PageSpec, ReadmeSpec, TierUnavailable  # noqa: E402

SITE = load_site(FAMILY)
pytestmark = pytest.mark.adapters
_MOUNTED = mod.DATA_FILE.is_file()
skip_unmounted = pytest.mark.skipif(not _MOUNTED, reason="legacy cmd_explorer data not mounted")


def _ctx(tier: str, staging: Path, limit=None, allow_tmp_inputs=False) -> BuildContext:
    return BuildContext(family=FAMILY, site=SITE, staging=staging, tier=tier, limit=limit,
                        log=lambda m: None, allow_tmp_inputs=allow_tmp_inputs)


# -- pure helpers: no legacy mount required --------------------------------------------------

def test_import_has_no_side_effects(tmp_path):
    code = ("import sys; sys.path.insert(0, %r); import adapters.cmd_explorer as m; "
            "assert m.ADAPTER.name == 'cmd_explorer'" % str(REPO))
    subprocess.run([sys.executable, "-c", code], cwd=tmp_path, check=True, timeout=60)
    assert list(tmp_path.iterdir()) == []


def test_cell_rows_n0_empty_cells():
    assert mod._cell_rows([], [0.0, 1.0, 10.0, 20.0], 0.01) == []


def test_cell_rows_n1_reconstructs_q0_r0_at_extent_min():
    # q=r=0 -> u=v=0 -> both axes land exactly on the extent minimum.
    rows = mod._cell_rows([[0, 0, 5]], [-1.0, 4.0, 8.0, 22.0], 0.01)
    assert len(rows) == 1
    r = rows[0]
    assert r["id"] == "cell0"
    assert math.isclose(r["f212n"], 8.0)
    assert math.isclose(r["f480m"], 8.0 - (-1.0))
    assert r["count"] == 5.0


def test_cell_rows_known_offset_cell():
    # q=1, r=0: u = R*sqrt(3), v = 0 -> color moves by R*sqrt(3) of the x-span, y unchanged.
    extent = [0.0, 10.0, 0.0, 10.0]
    radius = 0.1
    rows = mod._cell_rows([[1, 0, 3]], extent, radius)
    u = radius * math.sqrt(3) * 1.0
    assert math.isclose(rows[0]["f480m"], 0.0 - (u * 10.0), rel_tol=1e-9)
    assert math.isclose(rows[0]["f212n"], 0.0, abs_tol=1e-9)


@pytest.mark.parametrize("cell", [
    [1, 2],                       # wrong length
    [1, 2, 3, 4],                 # wrong length
    ["a", 0, 1],                  # non-numeric
    [0, 0, float("nan")],         # NaN count
    [0, 0, float("inf")],         # inf count
    [0, 0, 0],                    # zero count
    [0, 0, -5],                   # negative count
    "not-a-list",                 # not a sequence at all
])
def test_cell_rows_skips_malformed_or_non_positive(cell):
    assert mod._cell_rows([cell], [0.0, 1.0, 0.0, 1.0], 0.1) == []


def test_cell_rows_large_n_all_rows_finite():
    cells = [[q, (q * 7) % 11, 1 + q % 5] for q in range(2000)]
    rows = mod._cell_rows(cells, [-2.0, 6.0, 5.0, 25.0], 0.02)
    assert len(rows) == 2000
    assert all(math.isfinite(r["f212n"]) and math.isfinite(r["f480m"]) for r in rows)
    assert len({r["id"] for r in rows}) == 2000


def _synthetic_doc(nx):
    grid = {"extent": [0.0, 1.0, 0.0, 1.0], "radius": 0.1}
    if nx is not None:
        grid["nx"] = nx
    return {"grid": grid, "all": {"cells": [[0, 0, 5], [1, 0, 3]], "n": 8}}


@pytest.mark.parametrize("nx, expected", [(0, 0), (None, 60), (12, 12)])
def test_density_nx_honors_zero_and_missing(tmp_path, nx, expected):
    # grid.nx == 0 is a legitimate (if degenerate) value and must be honored, distinct
    # from nx missing entirely (which falls back to 60); mirrors the radius-is-None check
    # just above it in adapters.cmd_explorer.CmdExplorerAdapter.datasets.
    data_file = tmp_path / "cmd_explorer_data.json"
    data_file.write_text(json.dumps(_synthetic_doc(nx)), encoding="utf-8")
    mp = pytest.MonkeyPatch()
    mp.setattr(mod, "DATA_FILE", data_file)
    mp.setattr(mod, "HTML_FILE", tmp_path / "cmd_explorer.html")
    try:
        ctx = _ctx("A", tmp_path / "staging", allow_tmp_inputs=True)
        ds = mod.CmdExplorerAdapter().datasets(ctx)
    finally:
        mp.undo()
    assert ds["cells"].density[0].nx == expected
    assert ds["cells"].density[0].ny == expected


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
    assert isinstance(ds["readme"], ReadmeSpec)
    cat = ds["cells"]
    assert isinstance(cat, CatalogSpec)
    assert cat.ra is None and cat.dec is None  # no sky/skycat flag by design
    assert {"id", "f212n", "f480m", "count"} <= set(cat.table.colnames)
    assert [v.key for v in cat.virtual] == ["f212n-f480m"]
    assert cat.density and cat.density[0].x == "f212n-f480m" and cat.density[0].y == "f212n"
    roles = {c.key: c.role for c in cat.columns}
    assert "category" not in roles.values()
    # Rows are occupied hex cells (each an aggregate of many stars), not one row per star:
    # the landing-card count must say "cells", not fall back to the catalog-kind default.
    assert cat.noun == ("cell", "cells")
    for p in run["pages"]:
        assert isinstance(p, PageSpec)
        for k in p.datasets:
            assert k in ds, (p.id, k)


@skip_unmounted
def test_tier_b_unavailable(tmp_path):
    with pytest.raises(TierUnavailable):
        mod.CmdExplorerAdapter().datasets(_ctx("B", tmp_path))


@skip_unmounted
def test_limit_caps_row_count(tmp_path):
    ctx = _ctx("A", tmp_path, limit=3)
    ds = mod.CmdExplorerAdapter().datasets(ctx)
    assert len(ds["cells"].table) == 3


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
    """docs/port_table.json's cmd_explorer row: flags {multi, cat, num2, mags2, num3, dens}."""
    res = resolve(run["pages"][0], run["ds"], {})
    assert set(res.flags) == {"multi", "cat", "num2", "mags2", "num3", "dens"}
    assert len(res.enabled) == 21


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
