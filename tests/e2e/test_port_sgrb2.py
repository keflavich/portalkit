"""Port acceptance for the sgrb2 family (port table section 14.7, WP20).

Drives a staged sgrb2 build through the DevServer with ``?pktest=1`` and the ``__pk`` hooks.
``PK_PORT_BUILD`` names a staged build built with ``--vendor-assets`` (the build directory or
its ``sgrb2/`` portal directory); without it the module builds a fixture portal (``portalkit
build sgrb2 --limit 50 --vendor-assets``) into a temp stage root. Each special_handling entry
of the port table becomes one test:

* index: counts come from the manifests (no hard-coded numbers); the manifold card links the
  secure site with a login-required badge and nothing is fetched from it.
* sgrb2_map: a focus set while Aladin Lite initialises is applied once it is ready (the stub
  delays A.init); the BASE/OVERLAY split of SgrB2_Portal_Map.py with CDS ids as HiPS URLs.
* sgrb2_map_ccd: data encodings per tier, 0.2-99.8 percentile axis limits, CMD + CCD scatter.
* sgrb2_seds: PNG for a subset of the SED sources, live SED for all; flux <= 0 as upper limits.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

REPO = Path(__file__).resolve().parents[2]
LIMIT = 50
SECURE = "https://data.rc.ufl.edu/secure/"


def _plugin():
    return sys.modules["pk_conftest_browser"]


@pytest.fixture(scope="module")
def portal_dir(tmp_path_factory) -> Path:
    env = os.environ.get("PK_PORT_BUILD")
    if env:
        p = Path(env)
        p = p / "sgrb2" if (p / "sgrb2" / "portal.json").is_file() else p
        if not (p / "portal.json").is_file():
            pytest.fail(f"PK_PORT_BUILD={env} holds no sgrb2 portal")
        if re.search(r'"assets":\s*"https?://', (p / "index.html").read_text(encoding="utf-8")):
            pytest.skip("PK_PORT_BUILD loads its assets from the production URL; build it with --vendor-assets")
        return p
    if not Path("/orange/adamginsburg/jwst/sgrb2/portal/data/mapccd/manifest.json").is_file():
        pytest.skip("legacy sgrb2 portal tree not mounted")
    from portalkit import cli
    from portalkit.deploy import ExposureMap
    stage = tmp_path_factory.mktemp("stage")
    mp = pytest.MonkeyPatch()
    mp.setattr(cli, "_emap", lambda: ExposureMap(roots={}, url_prefixes={}))
    try:
        rc = cli.main(["build", "sgrb2", "--limit", str(LIMIT), "--stage-root", str(stage), "--vendor-assets"])
    finally:
        mp.undo()
    assert rc == 0
    found = sorted(stage.glob("sgrb2/*/sgrb2/portal.json"))
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
        r = _plugin().open_run(browser, server, f"sgrb2/{page}", **kw)
        opened.append(r)
        return r
    yield open_
    for r in opened:
        r.close()


def _json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def _secure_requests(run) -> list[str]:
    return [u for u in run.external if u.startswith(SECURE)]


def _no_errors(run):
    errs = run.errors(allow=(_plugin().ABORTED_LOAD,))
    assert not errs, errs


# -- index -----------------------------------------------------------------------------------
def test_landing_counts_and_login_badge(runs, portal_dir):
    run = runs("index.html")
    run.wait_panes_settled()
    portal = _json(portal_dir / "portal.json")
    assert portal["visibility"] == "public"
    run.page.wait_for_function(
        "() => Array.from(document.querySelectorAll('.pk-card-count')).some((c) => c.textContent.trim())",
        timeout=30000)
    cards = run.ev("""() => Array.from(document.querySelectorAll('.pk-card-link')).map((a) => ({
        href: a.getAttribute('href'), page: a.dataset.page || null,
        badge: !!a.querySelector('.pk-login-badge'),
        count: (a.querySelector('.pk-card-count') || {}).textContent || '',
        text: (a.querySelector('.pk-card-text') || {}).textContent || ''}))""")
    sec = [c for c in cards if c["href"] and c["href"].startswith(SECURE)]
    assert len(sec) == 1 and sec[0]["badge"], cards
    assert all(not c["badge"] for c in cards if c not in sec)
    # counts come from the search manifests
    for s in portal["search"]:
        n = _json(portal_dir / s["manifest"])["n"]
        card = next(c for c in cards if c["page"] == s["page"])
        assert re.sub(r"[,\s ]", "", card["count"]).startswith(str(n)), card
    # SED card text carries the build-time counts of the seds and img datasets
    nsed = _json(portal_dir / "data" / "seds" / "manifest.json")["n"]
    nimg = len(_json(portal_dir / "data" / "img" / "images.json")["items"])
    seds = next(c for c in cards if c["page"] == "sgrb2_seds")
    assert f"{nsed:,} red sources" in seds["text"] and f"{nimg:,} with legacy PNGs" in seds["text"]
    assert not _secure_requests(run)
    _no_errors(run)


# -- sgrb2_map -------------------------------------------------------------------------------
def test_map_hips_split_and_queue(runs, portal_dir):
    hips = _json(portal_dir / "data" / "hips.json")
    assert len(hips["base"]) >= 1 and len(hips["overlays"]) >= 1
    assert sum(bool(b.get("default")) for b in hips["base"]) == 1
    run = runs("sgrb2_map.html", init_scripts=("window.__aladinStubConfig = {initDelayMs: 2500};",))
    sky = run.ev("() => document.querySelector(\"section.pk-pane[data-pane-type='sky']\")?.dataset.paneId || null")
    assert sky
    # a focus set while Aladin Lite is still initialising is applied once the viewer is ready
    before = run.pane_stats(sky)
    run.ev("() => window.__pk.store.set({focus: {ds: 'src', i: 3, id: null}}, {source: 'test'})")
    run.page.wait_for_function(
        f"() => {{ const h = window.__pk.app.layout.pane('{sky}'); const s = h && h.stats();"
        " return !!(s && s.view && s.markers && s.layers); }", timeout=60000)
    st = run.pane_stats(sky)
    assert st["aladin"] not in ("error", None)
    assert before is None or before["aladin"] != "error"
    summ = run.ev("() => window.__aladinStub.summary(0)")
    default = next(b for b in hips["base"] if b.get("default"))
    assert summ["base"] == default["url"]
    assert all(b["url"].startswith("https://") for b in hips["base"] + hips["overlays"])
    # the focus ring of row 3 is drawn once the viewer is up
    run.page.wait_for_function(
        "() => (window.__aladinCalls || []).some((c) => c[0] === 'overlay.add' && c[1][0].endsWith(':focus'))",
        timeout=30000)
    rings = [c[1] for c in run.aladin_calls() if c[0] == "circle"]
    assert rings
    for ra, dec in (r[:2] for r in rings):
        assert 266.0 < float(ra) < 268.0 and -29.0 < float(dec) < -28.0, (ra, dec)
    assert abs(hips["target"]["ra"] - 266.83) < 0.1 and abs(hips["target"]["dec"] + 28.40) < 0.1
    assert run.store_get("focus")["i"] == 3
    assert not _secure_requests(run)
    _no_errors(run)


# -- sgrb2_map_ccd ---------------------------------------------------------------------------
def test_map_ccd_axes_and_encoding(runs, portal_dir):
    man = _json(portal_dir / "data" / "src" / "manifest.json")
    rep = _json(portal_dir / "build_report.json")
    mags = [c for c in man["cols"] if c.get("role") == "mag"]
    assert len(mags) == 11
    want = "f32" if rep["tier"] == "A" else "u16q"
    assert {c["enc"] for c in mags} == {want}
    if rep["tier"] == "B" and man["n"] > 131072:
        assert man["order"]["kind"] == "lod"
    page = next(p for p in rep["pages"] if p["id"] == "sgrb2_map_ccd")
    assert {"isochrones_or_tracks", "extinction_vectors_on_ccd", "hips_layer_stacking"} <= set(page["caps"]["enabled"])
    run = runs("sgrb2_map_ccd.html")
    run.wait_panes_settled()
    for pane, (x, y) in (("cmd", ("f182m-f480m", "f480m")), ("ccd", ("f182m-f212n", "f405n-f480m"))):
        # isochrone tracks + the shared reddening vector attach after the iso document loads
        run.page.wait_for_function(
            f"() => {{ const h = window.__pk.app.layout.pane('{pane}'); return !!h && h.stats().overlays >= 2; }}",
            timeout=60000)
        st = run.pane_stats(pane)
        assert st is not None, pane
        assert st["n"] == man["n"]
        ax = run.store_get(f"axes:{pane}")
        assert (ax["x"], ax["y"]) == (x, y)
        assert ax["xr"][0] < ax["xr"][1] and ax["yr"][0] < ax["yr"][1]
        # the 0.2-99.8 percentile limits stored at build time are the initial view
        assert st["view"]["xr"] == pytest.approx(ax["xr"], abs=1e-3)
        assert sorted(st["view"]["yr"]) == pytest.approx(sorted(ax["yr"]), abs=1e-3)
    assert not _secure_requests(run)
    _no_errors(run)


# -- sgrb2_seds ------------------------------------------------------------------------------
def test_seds_png_subset_and_upper_limits(runs, portal_dir):
    man = _json(portal_dir / "data" / "seds" / "manifest.json")
    items = _json(portal_dir / "data" / "img" / "images.json")["items"]
    run = runs("sgrb2_seds.html")
    run.wait_panes_settled()
    panes = run.ev("""() => Object.fromEntries(Array.from(document.querySelectorAll('section.pk-pane'))
        .map((s) => [s.dataset.paneType, s.dataset.paneId]))""")
    run.page.wait_for_function(
        f"() => window.__pk.app.layout.pane('{panes['gallery']}').stats().items > 0", timeout=60000)
    gal = run.pane_stats(panes["gallery"])
    assert gal["items"] == len(items)
    lst = run.pane_stats(panes["list"])
    assert lst["rows"] == man["n"] >= len(items)
    limited = [c for c in man["cols"] if c.get("role") == "flux" and c.get("limits")]
    if not limited:
        pytest.skip("no upper limits in this build")
    sed = panes["sed"]
    found = None
    for i in range(man["n"]):
        run.ev(f"() => window.__pk.store.set({{focus: {{ds: 'seds', i: {i}, id: null}}}}, {{source: 'test'}})")
        run.page.wait_for_function(
            f"() => {{ const s = window.__pk.app.layout.pane('{sed}').stats(); return s && s.i === {i}; }}",
            timeout=30000)
        st = run.pane_stats(sed)
        ul = [p for p in st["points"] if p["kind"] == "ul"]
        if ul:
            found = (i, st, ul)
            break
    assert found, "no focused SED showed an upper limit"
    i, st, ul = found
    assert all(p["fnu"] > 0 for p in ul)
    assert any(s["limit"] == "u" for s in st["series"])
    assert not _secure_requests(run)
    _no_errors(run)


def test_seds_link_resolves_on_map_ccd(runs, portal_dir):
    """Gallery -> map_ccd: every SED id is a src row (also in --limit fixture builds)."""
    seds = runs("sgrb2_seds.html")
    seds.wait_panes_settled()
    last = _json(portal_dir / "data" / "seds" / "manifest.json")["n"] - 1
    ids = []
    for i in (0, last):
        seds.ev(f"() => window.__pk.store.set({{focus: {{ds: 'seds', i: {i}, id: null}}}}, {{source: 'test'}})")
        seds.page.wait_for_function(
            f"() => {{ const f = window.__pk.store.get('focus'); return !!f && f.i === {i} && f.id != null; }}",
            timeout=30000)
        ids.append(str(seds.store_get("focus")["id"]))
    for sid in ids:
        run = runs("sgrb2_map_ccd.html", hash=f"id={sid}")
        run.page.wait_for_function(
            "() => { const f = window.__pk.store.get('focus'); return !!f && Number.isInteger(f.i) && f.i >= 0; }",
            timeout=60000)
        f = run.store_get("focus")
        assert str(f["id"]) == sid and f["ds"] == "src", f
        _no_errors(run)
