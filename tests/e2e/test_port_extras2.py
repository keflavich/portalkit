"""Port acceptance for the WP32 "extras": ``jwst_gc_aladin`` and ``cmd_explorer``
(ARCHITECTURE.md section 14 -- the two extras rows WP31 left unbuilt; see
``tests/e2e/test_port_extras.py``'s docstring and this repo's WP32 ``contract_issues``).

Both families are lean, static-plus-discovery Tier A adapters with no server-rendered
computation, so each gets one check per page rather than per-pane depth, matching the reduced
scope ``test_port_extras.py`` already uses for its own two families. The build limit is set
above ``density.T0_MAX`` (3000) so the pages actually resolve the "dens" capability
(``density_rendering``) the way a real build would, not the way a 20-row fixture would.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

REPO = Path(__file__).resolve().parents[2]
LIMIT = 4000


def _plugin():
    return sys.modules["pk_conftest_browser"]


def _json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def _build_portal(tmp_path_factory, fam: str, legacy_probe: Path):
    env = os.environ.get("PK_PORT_BUILD")
    if env:
        p = Path(env)
        p = p / fam if (p / fam / "portal.json").is_file() else p
        if not (p / "portal.json").is_file():
            pytest.fail(f"PK_PORT_BUILD={env} holds no {fam} portal")
        return p
    if not legacy_probe.exists():
        pytest.skip(f"legacy tree for {fam} not mounted")
    from portalkit import cli
    from portalkit.deploy import ExposureMap
    stage = tmp_path_factory.mktemp("stage")
    mp = pytest.MonkeyPatch()
    mp.setattr(cli, "_emap", lambda: ExposureMap(roots={}, url_prefixes={}))
    try:
        rc = cli.main(["build", fam, "--limit", str(LIMIT), "--stage-root", str(stage), "--vendor-assets"])
    finally:
        mp.undo()
    assert rc == 0
    found = sorted(stage.glob(f"{fam}/*/{fam}/portal.json"))
    assert found, "build staged no portal"
    return found[-1].parent


def _panes(run) -> dict:
    return run.ev("""() => { const o = {}; for (const s of document.querySelectorAll('section.pk-pane')) {
        (o[s.dataset.paneType] = o[s.dataset.paneType] || []).push(s.dataset.paneId); } return o; }""")


def _no_errors(run):
    errs = run.errors(allow=(_plugin().ABORTED_LOAD,))
    assert not errs, errs


def _wait(run, pane: str, cond: str, timeout=30000):
    run.page.wait_for_function(
        f"() => {{ const h = window.__pk.app.layout.pane('{pane}'); const s = h && h.stats(); return !!(s && ({cond})); }}",
        timeout=timeout)
    return run.pane_stats(pane)


# ============================================================ jwst_gc_aladin ================

ALADIN_PROBE = Path("/orange/adamginsburg/web/public/avm_images/jwst_gc_aladin.html")


@pytest.fixture(scope="module")
def aladin_portal_dir(tmp_path_factory):
    return _build_portal(tmp_path_factory, "jwst_gc_aladin", ALADIN_PROBE)


@pytest.fixture(scope="module")
def aladin_server(aladin_portal_dir):
    from portalkit.deploy.devserver import DevServer
    srv = DevServer(aladin_portal_dir.parent).start()
    yield srv
    srv.stop()


@pytest.fixture(scope="module")
def aladin_runs(browser, aladin_server):
    opened = []

    def open_(page: str, **kw):
        r = _plugin().open_run(browser, aladin_server, f"jwst_gc_aladin/{page}", **kw)
        opened.append(r)
        return r
    yield open_
    for r in opened:
        r.close()


def test_aladin_sky_page_has_only_a_sky_pane_and_a_readme(aladin_runs, aladin_portal_dir):
    """WP32's multi-avoidance design (ARCHITECTURE.md section 8.1 "multi"): a single sky pane
    bound to the catalog, no list/detail panel -- matching the legacy UI's own feature set
    (collapsible layer-toggle sections, no per-source list or detail table)."""
    portal = _json(aladin_portal_dir / "portal.json")
    assert portal["visibility"] == "public"
    page = next(p for p in portal["pages"] if p["id"] == "jwst_gc_aladin")
    assert page["archetype"] == "explorer"
    run = aladin_runs(page["file"])
    run.wait_panes_settled()
    p = _panes(run)
    assert "sky" in p, p
    assert "list" not in p and "detail" not in p, p
    _no_errors(run)


def test_aladin_sky_pane_loads_hips_and_catalog_markers(aladin_runs, aladin_portal_dir):
    hips = _json(aladin_portal_dir / "data" / "hips.json")
    assert hips["base"] and any(b.get("default") for b in hips["base"])
    assert hips["overlays"]
    page = next(p for p in _json(aladin_portal_dir / "portal.json")["pages"]
               if p["id"] == "jwst_gc_aladin")
    run = aladin_runs(page["file"])
    run.wait_panes_settled()
    p = _panes(run)
    st = _wait(run, p["sky"][0], "true")
    assert st is not None
    _no_errors(run)


# ============================================================== cmd_explorer ================

CMD_PROBE = Path("/orange/adamginsburg/jwst/releases/site/cmd_explorer_data.json")


@pytest.fixture(scope="module")
def cmd_portal_dir(tmp_path_factory):
    return _build_portal(tmp_path_factory, "cmd_explorer", CMD_PROBE)


@pytest.fixture(scope="module")
def cmd_server(cmd_portal_dir):
    from portalkit.deploy.devserver import DevServer
    srv = DevServer(cmd_portal_dir.parent).start()
    yield srv
    srv.stop()


@pytest.fixture(scope="module")
def cmd_runs(browser, cmd_server):
    opened = []

    def open_(page: str, **kw):
        r = _plugin().open_run(browser, cmd_server, f"cmd_explorer/{page}", **kw)
        opened.append(r)
        return r
    yield open_
    for r in opened:
        r.close()


def test_cmd_explorer_shows_list_scatter_and_detail_panes(cmd_runs, cmd_portal_dir):
    portal = _json(cmd_portal_dir / "portal.json")
    page = next(p for p in portal["pages"] if p["id"] == "cmd_explorer")
    assert page["archetype"] == "explorer"
    run = cmd_runs(page["file"])
    run.wait_panes_settled()
    p = _panes(run)
    for t in ("list", "scatter", "detail"):
        assert t in p, (t, p)
    st = _wait(run, p["list"][0], "s.rows > 0")
    assert st["rows"] > 0
    _no_errors(run)


def test_cmd_explorer_cells_catalog_has_no_position_columns(cmd_runs, cmd_portal_dir):
    man = _json(cmd_portal_dir / "data" / "cells" / "manifest.json")
    assert "pos" not in man or man["pos"] is None
    assert man["n"] > 0
    page = next(p for p in _json(cmd_portal_dir / "portal.json")["pages"]
               if p["id"] == "cmd_explorer")
    run = cmd_runs(page["file"])
    run.wait_panes_settled()
    _no_errors(run)
