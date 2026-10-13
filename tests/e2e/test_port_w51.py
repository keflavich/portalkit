"""Port acceptance for the w51 family (ARCHITECTURE.md section 15 WP29, port table 14.10).

Drives a staged w51 build (one secure portal, ten pages) through the DevServer with
``?pktest=1`` and the ``__pk``/``__aladinStub`` hooks. ``PK_PORT_BUILD`` names a staged build
made with ``--vendor-assets`` (the build directory or its ``w51/`` portal directory); without
it the module builds a fixture (``portalkit build w51 --limit 5 --allow-tmp-inputs
--vendor-assets``) into a temp stage root -- ``--allow-tmp-inputs`` is required because
``spectra_candidate``'s inputs are BLOCKED pending promotion out of a scratch location (open
question Q2); a separate, browser-free test checks that a build *without* that flag refuses
with exit 5.

* landing: nine in-family cards + one secure cross-family (jwst_archive) card, login-badged.
* map: clicking a GTC-overlay-adjacent sky source opens its detail pane (WP29's explicit
  regression check for the legacy ``map.html`` ``objectClicked`` handler's undefined ``r`` bug,
  W51_Portal_Map.py ~955-970 -- the generic markers.js click path this port uses never
  references any such variable, so the click must resolve a concrete, non-null id).
* map_ccd: two independent catalogues/selections on one sky+scatter page.
* wavelength: the 17-rung HiPS tour, rung0 as the default base layer.
* spectra: list + spectrum1d + spectrum2d panes, scoped to the ``spec_cat`` catalog.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

FAMILY = "w51"
LIMIT = 5
SECURE = "https://data.rc.ufl.edu/secure/"
LEGACY = Path("/orange/adamginsburg/w51/portal")


def _plugin():
    return sys.modules["pk_conftest_browser"]


def _json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def portal_dir(tmp_path_factory) -> Path:
    env = os.environ.get("PK_PORT_BUILD")
    if env:
        p = Path(env)
        p = p / "w51" if (p / "w51" / "portal.json").is_file() else p
        if not (p / "portal.json").is_file():
            pytest.fail(f"PK_PORT_BUILD={env} holds no w51 portal")
        if re.search(r'"assets":\s*"https?://', (p / "index.html").read_text(encoding="utf-8")):
            pytest.skip("PK_PORT_BUILD loads its assets from the production URL; build it with --vendor-assets")
        return p
    if not (LEGACY / "index.html").is_file():
        pytest.skip("W51 legacy portal tree not mounted")
    from portalkit import cli
    from portalkit.deploy import ExposureMap
    stage = tmp_path_factory.mktemp("stage")
    mp = pytest.MonkeyPatch()
    mp.setattr(cli, "_emap", lambda: ExposureMap(roots={}, url_prefixes={}))
    try:
        rc = cli.main(["build", FAMILY, "--limit", str(LIMIT), "--stage-root", str(stage),
                      "--vendor-assets", "--allow-tmp-inputs"])
    finally:
        mp.undo()
    assert rc == 0
    found = sorted(stage.glob(f"{FAMILY}/*/w51/portal.json"))
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
        r = _plugin().open_run(browser, server, f"w51/{page}", **kw)
        opened.append(r)
        return r
    yield open_
    for r in opened:
        r.close()


def _secure_requests(run) -> list[str]:
    return [u for u in run.external if u.startswith(SECURE)]


def _no_errors(run):
    errs = run.errors(allow=(_plugin().ABORTED_LOAD,))
    assert not errs, errs


def _panes(run) -> dict:
    return run.ev("""() => { const o = {}; for (const s of document.querySelectorAll('section.pk-pane')) {
        (o[s.dataset.paneType] = o[s.dataset.paneType] || []).push(s.dataset.paneId); } return o; }""")


# -- exit code 5 when the candidate inputs are not promoted (browser-free) -------------------
def test_blocked_candidate_build_exits_5(tmp_path):
    if not (LEGACY / "index.html").is_file():
        pytest.skip("W51 legacy portal tree not mounted")
    from portalkit import cli
    from portalkit.deploy import ExposureMap
    mp = pytest.MonkeyPatch()
    mp.setattr(cli, "_emap", lambda: ExposureMap(roots={}, url_prefixes={}))
    try:
        rc = cli.main(["build", FAMILY, "--limit", str(LIMIT), "--stage-root", str(tmp_path)])
    finally:
        mp.undo()
    assert rc == 5, rc
    # the legacy page must stay live: nothing in sites/w51.toml replaces it, and no partial
    # staged w51 portal is left behind by the refused build.
    assert not list(tmp_path.glob(f"{FAMILY}/*/w51/portal.json"))


# -- landing ----------------------------------------------------------------------------------
def test_landing_cards_and_secure_badge(runs, portal_dir):
    portal = _json(portal_dir / "portal.json")
    assert portal["visibility"] == "secure"
    run = runs("index.html")
    run.wait_panes_settled()
    run.page.wait_for_function(
        "() => Array.from(document.querySelectorAll('.pk-card-count, .pk-card-link')).length > 0",
        timeout=30000)
    cards = run.ev("""() => Array.from(document.querySelectorAll('.pk-card-link')).map((a) => ({
        href: a.getAttribute('href'), page: a.dataset.page || null,
        badge: !!a.querySelector('.pk-login-badge')}))""")
    in_family = {c["page"] for c in cards if c["page"]}
    assert in_family == {"map", "map_ccd", "colorcolor", "gallery", "spectra",
                         "spectra_candidate", "w51_manifold", "w51_catalog_manifold",
                         "wavelength"}
    ext = [c for c in cards if not c["page"]]
    assert len(ext) == 1 and ext[0]["href"].startswith(SECURE) and ext[0]["badge"]
    _no_errors(run)


# -- map: click-through regression (the legacy objectClicked undefined-r bug) -----------------
def test_map_click_opens_detail(runs, portal_dir):
    from portalkit.legacy import extract
    man = _json(portal_dir / "data" / "gal" / "manifest.json")
    assert man["n"] == LIMIT
    # hips_map's target fov (0.4-0.5 deg, centred on the field) frames the whole legacy survey;
    # the LIMIT=5 fixture is just the first 5 gallery rows, which need not fall inside that
    # default view, so point the viewer at their actual bounding box before looking for markers.
    rows = extract.script_json(LEGACY / "gallery.html", "srcdata")[:LIMIT]
    ras = [float(r["ra"]) for r in rows]
    decs = [float(r["dec"]) for r in rows]
    ra_c, dec_c = sum(ras) / len(ras), sum(decs) / len(decs)
    fov = max((max(ras) - min(ras)), (max(decs) - min(decs))) * 3 + 0.02
    run = runs("map.html")
    run.wait_panes_settled()
    p = _panes(run)
    assert {"sky", "list", "detail"} <= set(p)
    sky = p["sky"][0]
    det = p["detail"][0]
    run.page.wait_for_function(
        f"() => {{ const h = window.__pk.app.layout.pane('{sky}'); const s = h && h.stats();"
        " return !!(s && s.view && s.markers && s.layers); }", timeout=60000)
    run.ev("([ra, dec, fov]) => window.__pk.store.set({sky: {ra, dec, fov}}, {source: 'test'})",
          [ra_c, dec_c, fov])
    run.page.wait_for_function(
        f"() => {{ const h = window.__pk.app.layout.pane('{sky}'); const s = h && h.stats();"
        " return !!(s && s.markers && s.markers.catalogs > 0); }", timeout=30000)
    summ = run.ev("() => window.__aladinStub.summary(0)")
    cat = next((c for c in summ["catalogs"] if c["n"] > 0), None)
    assert cat is not None, summ["catalogs"]
    before = run.ev(f"() => {{ const h = window.__pk.app.layout.pane('{det}');"
                    " const s = h && h.stats(); return s ? s.id : null; }")
    ok = run.ev("([n, k]) => window.__aladinStub.clickSource(n, k)", [cat["name"], 0])
    assert ok
    run.page.wait_for_function(
        f"() => {{ const h = window.__pk.app.layout.pane('{det}');"
        " const s = h && h.stats(); return !!(s && s.id != null); }", timeout=30000)
    after = run.ev(f"() => {{ const h = window.__pk.app.layout.pane('{det}');"
                   " const s = h && h.stats(); return s ? s.id : null; }")
    assert after is not None and after != before
    # the click focuses {ds, i} first; the store's focus.id is filled once the id resolves
    run.page.wait_for_function(
        "() => { const f = window.__pk.store.get('focus'); return !!(f && f.id != null); }", timeout=30000)
    focus = run.store_get("focus")
    assert focus is not None and focus["ds"] == "gal" and focus.get("id") == after
    # the detail pane now wires gal_img (map.html's lightbox/static_sed_png fix this work
    # package): selecting a source legitimately loads that source's own sed/cutout thumbnail
    # (data/gal/*), so only secure requests for the newly-focused id's own PNGs are expected.
    reqs = _secure_requests(run)
    assert all(f"/{after}.png" in u for u in reqs), reqs
    _no_errors(run)


def test_map_sky_overlays_are_gtc_layer_ids(portal_dir):
    pk_page = json.loads(re.search(
        r'<script type="application/json" id="pk-page">(.*?)</script>',
        (portal_dir / "map.html").read_text(encoding="utf-8"), re.S).group(1))
    sky_leaf = next(leaf for row in pk_page["layout"]["row"] for leaf in row["col"]
                    if leaf.get("pane") == "sky")
    overlays = sky_leaf["options"]["overlays"]
    assert overlays
    # gtc_ovl is the only OverlaySpec dataset, so it claims the flat "data/overlays.json"
    # path (build.py: first overlay-kind dataset processed gets "data/{kind}.json").
    ovl = _json(portal_dir / "data" / "overlays.json")
    assert set(overlays) <= {lay["id"] for lay in ovl["layers"]}


# -- map_ccd: two independent catalogues on one page -------------------------------------------
def test_map_ccd_two_catalogs(runs, portal_dir):
    wide = _json(portal_dir / "data" / "ccd_wide" / "manifest.json")
    jwst = _json(portal_dir / "data" / "ccd_jwst" / "manifest.json")
    assert wide["n"] == LIMIT and jwst["n"] == LIMIT
    run = runs("map_ccd.html")
    run.wait_panes_settled()
    p = _panes(run)
    assert "sky" in p and "scatter" in p and len(p["scatter"]) >= 2
    _no_errors(run)


# -- wavelength: 17-rung HiPS tour --------------------------------------------------------------
def test_wavelength_rungs(runs, portal_dir):
    # hips_map claims the flat "data/hips.json" path first (build.py: first hips-kind dataset
    # processed gets "data/{kind}.json"); hips_wave, the second one, falls back to its own
    # per-key subfolder "data/hips_wave/hips.json".
    hips = _json(portal_dir / "data" / "hips_wave" / "hips.json")
    from portalkit.legacy import extract
    rungs_raw = len(extract.script_json(LEGACY / "wavelength.html", "rungdata"))
    assert hips["rungs"] is not None and len(hips["rungs"]) == rungs_raw
    assert len(hips["base"]) == 1 and hips["base"][0]["id"] == hips["rungs"][0]["id"]
    run = runs("wavelength.html")
    run.wait_panes_settled()
    run.page.wait_for_function(
        "() => !!(window.__aladinStub && window.__aladinStub.instances && window.__aladinStub.instances.length)",
        timeout=60000)
    summ = run.ev("() => window.__aladinStub.summary(0)")
    assert summ["base"] == hips["rungs"][0]["url"]
    _no_errors(run)


# -- spectra: list + spectrum1d + spectrum2d, scoped to spec_cat --------------------------------
def test_spectra_panes(runs, portal_dir):
    spec_man = _json(portal_dir / "data" / "spec_cat" / "manifest.json")
    assert spec_man["n"] == LIMIT
    run = runs("spectra.html")
    run.wait_panes_settled()
    p = _panes(run)
    for t in ("list", "detail", "spectrum1d", "spectrum2d"):
        assert t in p, (t, p)
    _no_errors(run)


def test_spectra_candidate_page_stays_blocked_placeholder(runs, portal_dir):
    """The legacy spectra_candidate.html keeps serving; the ported page shows a 0-row
    placeholder (its real per-object format is unscoped for this work package)."""
    cand_man = _json(portal_dir / "data" / "candidate" / "manifest.json")
    assert cand_man["n"] == 0
    run = runs("spectra_candidate.html")
    run.wait_panes_settled()
    _no_errors(run)
