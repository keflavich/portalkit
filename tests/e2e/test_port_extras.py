"""Port acceptance for the WP31 "extras": ``hips_tours`` and ``spitzer_spectra``
(ARCHITECTURE.md section 14, wave 6).

Coverage note: WP31's prose also mentions ``jwst_gc_aladin`` and ``cmd_explorer`` pages, but
neither has an entry in ``docs/wps.json``'s WP31 ``files`` list (no adapter module, no site
TOML) -- there is nothing to port under this work package for them, and this file does not
invent one. This leaves a documented gap, reported separately in ``contract_issues``.

Both families are lean, static-plus-discovery Tier A adapters with no server-rendered
computation, so each gets one check per page rather than per-pane depth, matching the reduced
scope already used for the ``nirspec6927_narrative`` sibling.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

REPO = Path(__file__).resolve().parents[2]
LIMIT = 20


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


# ================================================================== hips_tours ==============

RGB_PROBE = Path("/orange/adamginsburg/web/public/avm_images/brick_rgb_wavelength_tour.html")


@pytest.fixture(scope="module")
def hips_portal_dir(tmp_path_factory):
    return _build_portal(tmp_path_factory, "hips_tours", RGB_PROBE)


@pytest.fixture(scope="module")
def hips_server(hips_portal_dir):
    from portalkit.deploy.devserver import DevServer
    srv = DevServer(hips_portal_dir.parent).start()
    yield srv
    srv.stop()


@pytest.fixture(scope="module")
def hips_runs(browser, hips_server):
    opened = []

    def open_(page: str, **kw):
        r = _plugin().open_run(browser, hips_server, f"hips_tours/{page}", **kw)
        opened.append(r)
        return r
    yield open_
    for r in opened:
        r.close()


def test_hips_tours_landing_lists_discovered_tours(hips_runs, hips_portal_dir):
    portal = _json(hips_portal_dir / "portal.json")
    assert portal["visibility"] == "public"
    non_index = [p for p in portal["pages"] if p["id"] != "index"]
    assert non_index and all(p["archetype"] == "wavelength" for p in non_index)
    run = hips_runs("index.html")
    run.wait_panes_settled()
    run.page.wait_for_function(
        "() => document.querySelectorAll('.pk-card-link').length > 0", timeout=30000)
    cards = run.ev("""() => Array.from(document.querySelectorAll('.pk-card-link')).map((a) =>
        a.dataset.page || null)""")
    assert {p["id"] for p in non_index} <= {c for c in cards if c}
    _no_errors(run)


def test_hips_tours_one_rgb_and_one_aces_page_report_rungs(hips_runs, hips_portal_dir):
    portal = _json(hips_portal_dir / "portal.json")
    rgb = next((p for p in portal["pages"] if p["id"].startswith("rgb_")), None)
    aces = next((p for p in portal["pages"] if p["id"].startswith("aces_")), None)
    assert rgb and aces, portal["pages"]
    for page in (rgb, aces):
        run = hips_runs(page["file"])
        run.wait_panes_settled()
        p = _panes(run)
        assert "wavelength" in p, p
        st = _wait(run, p["wavelength"][0], "s.rungs && s.rungs.length > 0")
        assert len(st["rungs"]) > 0
        _no_errors(run)


# ============================================================== spitzer_spectra =============

SPZ_PROBE = Path("/orange/adamginsburg/cmz/spitzer_spectra/spitzer_spectra_sources.json")


@pytest.fixture(scope="module")
def spz_portal_dir(tmp_path_factory):
    return _build_portal(tmp_path_factory, "spitzer_spectra", SPZ_PROBE)


@pytest.fixture(scope="module")
def spz_server(spz_portal_dir):
    from portalkit.deploy.devserver import DevServer
    srv = DevServer(spz_portal_dir.parent).start()
    yield srv
    srv.stop()


@pytest.fixture(scope="module")
def spz_runs(browser, spz_server):
    opened = []

    def open_(page: str, **kw):
        r = _plugin().open_run(browser, spz_server, f"spitzer_spectra/{page}", **kw)
        opened.append(r)
        return r
    yield open_
    for r in opened:
        r.close()


def test_spitzer_map_lists_sources_and_slit_overlay(spz_runs, spz_portal_dir):
    n = _json(spz_portal_dir / "data" / "src" / "manifest.json")["n"]
    ovl = _json(spz_portal_dir / "data" / "overlays.json")
    assert {"slits"} <= {layer["id"] for layer in ovl["layers"]}
    run = spz_runs("spitzer_map.html")
    run.wait_panes_settled()
    p = _panes(run)
    for t in ("list", "sky", "detail"):
        assert t in p, (t, p)
    st = _wait(run, p["list"][0], "s.rows > 0")
    assert st["rows"] == n
    _no_errors(run)


def test_spitzer_specviewer_loads_a_spectrum_record(spz_runs, spz_portal_dir):
    idx = _json(spz_portal_dir / "data" / "spec" / "index.json")
    assert idx["n"] > 0 and idx["entries"]
    run = spz_runs("spitzer_specviewer.html")
    run.wait_panes_settled()
    p = _panes(run)
    assert "spectrum1d" in p and "list" in p, p
    _no_errors(run)
