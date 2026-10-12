"""Port acceptance for the nirspec6927_narrative family (WP31; ARCHITECTURE.md sections 14.1,
14.7, wave 6).

Reduced scope relative to the WP22 sibling (``test_port_nirspec6927.py``): one check per page
rather than its full per-pane/per-link depth (documented in the work-package deviations).
Drives a staged ``nirspec6927_narrative`` build through the DevServer with the same
``PK_PORT_BUILD``/fixture-build fallback pattern.
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
FAM = "nirspec6927_narrative"
SECURE = "https://data.rc.ufl.edu/secure/"
LEGACY = Path("/orange/adamginsburg/jwst/spectra/nirspec_6927/nirspec_6927_sources.json")


def _plugin():
    return sys.modules["pk_conftest_browser"]


@pytest.fixture(scope="module")
def portal_dir(tmp_path_factory) -> Path:
    env = os.environ.get("PK_PORT_BUILD")
    if env:
        p = Path(env)
        p = p / FAM if (p / FAM / "portal.json").is_file() else p
        if not (p / "portal.json").is_file():
            pytest.fail(f"PK_PORT_BUILD={env} holds no {FAM} portal")
        return p
    if not LEGACY.is_file():
        pytest.skip("legacy nirspec_6927 tree not mounted")
    from portalkit import cli
    from portalkit.deploy import ExposureMap
    stage = tmp_path_factory.mktemp("stage")
    mp = pytest.MonkeyPatch()
    mp.setattr(cli, "_emap", lambda: ExposureMap(roots={}, url_prefixes={}))
    try:
        rc = cli.main(["build", FAM, "--limit", str(LIMIT), "--stage-root", str(stage), "--vendor-assets"])
    finally:
        mp.undo()
    assert rc == 0
    found = sorted(stage.glob(f"{FAM}/*/{FAM}/portal.json"))
    assert found, "build staged no portal"
    return found[-1].parent


@pytest.fixture(scope="module")
def server(portal_dir):
    from portalkit.deploy.devserver import DevServer
    srv = DevServer(portal_dir.parent).start()
    yield srv
    srv.stop()


@pytest.fixture(scope="module")
def runs(browser, server):
    opened = []

    def open_(page: str, **kw):
        r = _plugin().open_run(browser, server, f"{FAM}/{page}", **kw)
        opened.append(r)
        return r
    yield open_
    for r in opened:
        r.close()


def _json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def _no_errors(run):
    errs = run.errors(allow=(_plugin().ABORTED_LOAD,))
    assert not errs, errs


def _panes(run) -> dict:
    return run.ev("""() => { const o = {}; for (const s of document.querySelectorAll('section.pk-pane')) {
        (o[s.dataset.paneType] = o[s.dataset.paneType] || []).push(s.dataset.paneId); } return o; }""")


def _wait(run, pane: str, cond: str, timeout=60000):
    run.page.wait_for_function(
        f"() => {{ const h = window.__pk.app.layout.pane('{pane}'); const s = h && h.stats(); return !!(s && ({cond})); }}",
        timeout=timeout)
    return run.pane_stats(pane)


# -- index: cards for the four pages, same secure target as WP22 -----------------------------
def test_landing_cards(runs, portal_dir):
    portal = _json(portal_dir / "portal.json")
    assert portal["visibility"] == "secure"
    run = runs("n_index.html")
    run.wait_panes_settled()
    # renderCards() runs after the async loadPortal() fetch resolves (web/js/panes/landing.js);
    # wait_panes_settled() does not track that promise, so wait for a populated card count too
    # (same pattern as the WP22 sibling's test_landing_secure_cards).
    run.page.wait_for_function(
        "() => Array.from(document.querySelectorAll('.pk-card-link')).length > 0", timeout=30000)
    cards = run.ev("""() => Array.from(document.querySelectorAll('.pk-card-link')).map((a) => ({
        href: a.getAttribute('href'), page: a.dataset.page || null}))""")
    assert {"n_msata", "n_h2", "n_refstar_map", "n_refstar_check"} <= {c["page"] for c in cards if c["page"]}
    _no_errors(run)


# -- n_msata: narrative pane loads all beats and go() switches the highlighted beat ----------
def test_msata_narrative_beats_and_go(runs, portal_dir):
    doc = _json(portal_dir / "data" / "narrative.json")
    run = runs("n_msata.html")
    run.wait_panes_settled()
    p = _panes(run)
    assert "narrative" in p and "sky" in p
    st = _wait(run, p["narrative"][0], "s.beats > 0")
    assert st["beats"] == len(doc["beats"])
    pane = p["narrative"][0]
    run.ev(f"() => window.__pk.app.layout.pane('{pane}').go('n_beat05')")
    st2 = _wait(run, pane, f"s.beat === 'n_beat05'")
    assert st2["index"] == 4
    _no_errors(run)


# -- n_h2: list + scatter (rotation-diagram proxy) + detail -----------------------------------
def test_h2_report_panes(runs, portal_dir):
    lines = _json(portal_dir / "data" / "h2lines" / "manifest.json")
    run = runs("n_h2.html")
    run.wait_panes_settled()
    p = _panes(run)
    for t in ("list", "scatter", "detail"):
        assert t in p, (t, p)
    if lines["n"] > 0:
        st = _wait(run, p["scatter"][0], "s.n > 0")
        assert st["n"] > 0
    _no_errors(run)


# -- n_refstar_map: sky pane with the quadrant overlay and click-rings enabled ----------------
def test_refstar_map_sky_and_rings(runs, portal_dir):
    # m_ovl (msata) claims the stable data/overlays.json first; rm_ovl's layers land at
    # data/rm_ovl/overlays.json instead (portalkit/build.py's stable-name collision rule)
    ovl = _json(portal_dir / "data" / "rm_ovl" / "overlays.json")
    assert {"msa_quads"} <= {layer["id"] for layer in ovl["layers"]}
    # rings is a static sky-pane layout option (section 6.10), baked into the page's own
    # embedded #pk-page JSON rather than exposed by the pane's live stats(). Values must match
    # the legacy page's own showRing() vetting-criteria circles (2"/3"), not placeholder radii.
    from portalkit.legacy.extract import script_json
    page = script_json(portal_dir / "n_refstar_map.html", "pk-page")
    sky_opts = next(r["options"] for r in page["layout"]["row"] if r.get("pane") == "sky")
    assert sky_opts["rings"] == [2, 3]
    run = runs("n_refstar_map.html")
    run.wait_panes_settled()
    p = _panes(run)
    _wait(run, p["sky"][0], "s.layers")
    _no_errors(run)


# -- n_refstar_check: contact-sheet gallery ----------------------------------------------------
def test_refstar_check_gallery(runs, portal_dir):
    doc = _json(portal_dir / "data" / "refcheck" / "images.json")
    run = runs("n_refstar_check.html")
    run.wait_panes_settled()
    p = _panes(run)
    st = _wait(run, p["gallery"][0], "s.items > 0")
    assert st["items"] == len(doc["items"])
    _no_errors(run)
