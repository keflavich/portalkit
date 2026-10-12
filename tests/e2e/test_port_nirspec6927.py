"""Port acceptance for the nirspec6927 family (port table section 14.7, WP22 wave 1).

Drives a staged nirspec6927 build through the DevServer with ``?pktest=1`` and the ``__pk``
hooks. ``PK_PORT_BUILD`` names a staged build built with ``--vendor-assets`` (the build
directory or its ``nirspec6927/`` portal directory); without it the module builds a fixture
portal (``portalkit build nirspec6927 --limit 50 --vendor-assets``) into a temp stage root.
External requests (HiPS tiles, secure figures) are aborted by the harness and recorded.

* index: secure portal; page cards with manifest counts; the four legacy-only pages are
  login-badged secure links.
* map: HiPS + MSA shutter / dither polygons, focus drives the 1D spectrum (grating segments),
  the 2D spectrum (pki1) and the products list (secure FITS with login badges, public MAST).
* legacy ``#<name>`` deep links focus the source (bare-name / specviewer grammars).
* manifold, specviewer, colorcolor (Brick density + isochrones + reddening vector),
  icecolumns (log axes + 1:1 line), cogas (initial focus on the highest comb S/N, prose).
* pvgallery / sedgallery / gallery_6927 (role presets c2221 / c1182 via the hash).
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
FAM = "nirspec6927"
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
        if re.search(r'"assets":\s*"https?://', (p / "index.html").read_text(encoding="utf-8")):
            pytest.skip("PK_PORT_BUILD loads its assets from the production URL; build it with --vendor-assets")
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
    """paneType -> [paneId, ...] in document order."""
    return run.ev("""() => { const o = {}; for (const s of document.querySelectorAll('section.pk-pane')) {
        (o[s.dataset.paneType] = o[s.dataset.paneType] || []).push(s.dataset.paneId); } return o; }""")


def _wait(run, pane: str, cond: str, timeout=60000):
    run.page.wait_for_function(
        f"() => {{ const h = window.__pk.app.layout.pane('{pane}'); const s = h && h.stats(); return !!(s && ({cond})); }}",
        timeout=timeout)
    return run.pane_stats(pane)


def _focus(run, i: int):
    run.ev(f"() => window.__pk.store.set({{focus: {{ds: 'src', i: {i}, id: null}}}}, {{source: 'test'}})")


def _no_public_data(run):
    """Secure portal: nothing is requested from the public tree of this data set."""
    assert not [u for u in run.external if "/pub/adamginsburg/jwst/spectra" in u]


# -- index -----------------------------------------------------------------------------------
def test_landing_secure_cards(runs, portal_dir):
    portal = _json(portal_dir / "portal.json")
    assert portal["visibility"] == "secure"
    run = runs("index.html")
    run.wait_panes_settled()
    run.page.wait_for_function(
        "() => Array.from(document.querySelectorAll('.pk-card-count')).some((c) => c.textContent.trim())",
        timeout=30000)
    cards = run.ev("""() => Array.from(document.querySelectorAll('.pk-card-link')).map((a) => ({
        href: a.getAttribute('href'), page: a.dataset.page || null,
        badge: !!a.querySelector('.pk-login-badge'),
        count: (a.querySelector('.pk-card-count') || {}).textContent || ''}))""")
    sec = [c for c in cards if c["href"] and c["href"].startswith(SECURE)]
    assert len(sec) == 4 and all(c["badge"] for c in sec), cards
    assert len([c for c in cards if c["page"]]) == 9
    n = _json(portal_dir / "data" / "src" / "manifest.json")["n"]
    counted = [c for c in cards if c["page"] and c["count"].strip()]
    assert counted, cards
    for c in counted:
        # catalog cards count the n sources (spectra); gallery cards count their images, at
        # most one per source (the SED set lacks a figure for some pointings)
        num = int(re.match(r"\d+", re.sub(r"[,\s ]", "", c["count"])).group(0))
        assert (num <= n) if "image" in c["count"] else (num == n), c
    _no_public_data(run)
    _no_errors(run)


# -- map -------------------------------------------------------------------------------------
def test_map_sky_spectra_products(runs, portal_dir):
    ovl = _json(portal_dir / "data" / "overlays.json")
    ids = {layer["id"] for layer in ovl["layers"]}
    assert {"msa_shutters", "dither", "one_to_one"} <= ids
    run = runs("nirspec_6927_map.html")
    run.wait_panes_settled()
    p = _panes(run)
    for t in ("list", "sky", "scatter", "detail", "spectrum1d", "spectrum2d"):
        assert t in p, (t, p)
    _wait(run, p["sky"][0], "s.layers && s.aladin !== 'error'")
    _focus(run, 0)
    s1 = _wait(run, p["spectrum1d"][0], "s.n > 0")
    assert set(s1["segments"]) >= {"G235M"}
    assert s1["features"]["shown"] > 0
    s2 = _wait(run, p["spectrum2d"][0], "s.nx > 0")
    assert s2["mode"] == "pki1" and s2["ny"] > 1
    run.page.wait_for_function("() => document.querySelectorAll('.pk-product').length > 0", timeout=30000)
    prods = run.ev("""() => Array.from(document.querySelectorAll('.pk-product')).map((li) => ({
        href: li.querySelector('a').href, badge: !!li.querySelector('.pk-login-badge')}))""")
    fits = [x for x in prods if x["href"].endswith(".fits")]
    assert fits and all(x["href"].startswith(SECURE) and x["badge"] for x in fits), prods
    mast = [x for x in prods if "mast.stsci.edu" in x["href"]]
    assert len(mast) == 1 and not mast[0]["badge"]
    _no_public_data(run)
    _no_errors(run)


# -- legacy deep links ------------------------------------------------------------------------
@pytest.mark.parametrize("page", ["nirspec_6927_map", "nirspec_6927_specviewer"])
def test_legacy_bare_name_hash(runs, portal_dir, page):
    """A legacy #<source name> link (redirect stub or bookmark) focuses that source."""
    run = runs(f"{page}.html")
    run.wait_panes_settled()
    run_ids = run.ev("""async () => { const c = await window.__pk.hub.catalog('src');
        const r = await c.rows(1, 2); return r.map((x) => x.id ?? x.name); }""")
    ids = [str(x) for x in run_ids if x is not None]
    assert ids, run_ids
    run2 = runs(f"{page}.html", hash=ids[0])
    run2.wait_panes_settled()
    run2.page.wait_for_function("() => { const f = window.__pk.store.get('focus'); return !!(f && f.id); }",
                                timeout=30000)
    assert str(run2.store_get("focus")["id"]) == ids[0]
    _no_errors(run2)


# -- manifold --------------------------------------------------------------------------------
def test_manifold(runs, portal_dir):
    man = _json(portal_dir / "data" / "src" / "manifest.json")
    run = runs("nirspec_6927_manifold.html")
    run.wait_panes_settled()
    p = _panes(run)
    st = _wait(run, p["manifold"][0], "s.n > 0 && s.D > 0")
    assert st["n"] == man["n"] and st["D"] >= 10
    _focus(run, 2)
    _wait(run, p["spectrum1d"][0], "s.n > 0")
    _no_errors(run)


# -- specviewer ------------------------------------------------------------------------------
def test_specviewer_bands_and_2d(runs):
    run = runs("nirspec_6927_specviewer.html")
    run.wait_panes_settled()
    p = _panes(run)
    _focus(run, 1)
    s1 = _wait(run, p["spectrum1d"][0], "s.n > 0")
    assert s1["xr"][0] < 2.0 < s1["xr"][1]
    assert all(v is True or v is False for v in s1["segments"].values())
    s2 = _wait(run, p["spectrum2d"][0], "s.nx > 0")
    assert s2["mode"] == "pki1"
    _no_errors(run)


# -- colorcolor ------------------------------------------------------------------------------
def test_colorcolor_density_iso_pairs(runs, portal_dir):
    bg = _json(portal_dir / "data" / "bg" / "manifest.json")
    assert bg["n"] > 0
    run = runs("nirspec_6927_colorcolor.html")
    run.wait_panes_settled()
    for pane, (x, y) in (("ccd", ("f182m-f212n", "f405n-f466n")), ("cmd", ("f182m-f212n", "f212n"))):
        st = _wait(run, pane, "s.overlays >= 2")
        assert st["n"] > 0
        ax = run.store_get(f"axes:{pane}") or {}
        assert (ax.get("x", x), ax.get("y", y)) == (x, y)
    _no_errors(run)


# -- icecolumns ------------------------------------------------------------------------------
def test_icecolumns_log_axes_and_one_to_one(runs):
    run = runs("nirspec_6927_icecolumns.html")
    run.wait_panes_settled()
    for pane in ("ice", "tau"):
        st = _wait(run, pane, "s.overlays >= 1 && s.view")
        assert st["view"].get("xlog") and st["view"].get("ylog"), st["view"]
    _no_errors(run)


# -- cogas -----------------------------------------------------------------------------------
def test_cogas_initial_focus_and_prose(runs, portal_dir):
    page = next(p for p in _json(portal_dir / "build_report.json")["pages"] if p["id"] == "nirspec_6927_cogas")
    assert page["first_paint_bytes"] <= 2_000_000
    run = runs("nirspec_6927_cogas.html")
    run.wait_panes_settled()
    p = _panes(run)
    det = _wait(run, p["detail"][0], "s.id !== null")
    focus = run.store_get("focus")
    assert focus and str(focus.get("id") or det["id"]) == str(det["id"])
    # the default focus is the source with the highest finite comb_snr
    res = run.ev("""async () => { const pk = window.__pk; const c = await pk.hub.catalog('src');
        const v = await c.values('comb_snr'); const f = pk.store.get('focus');
        let i = f && Number.isInteger(f.i) ? f.i : -1;
        if (i < 0 && f && f.id != null) { [i] = await pk.app.resolveIds('src', [f.id]); }
        let m = -Infinity; for (const x of v) if (Number.isFinite(x) && x > m) m = x;
        return {at: i >= 0 ? v[i] : null, max: m}; }""")
    assert res["at"] is not None and res["at"] == pytest.approx(res["max"]), res
    run.page.wait_for_function("() => !!document.querySelector('.pk-detail-prose')", timeout=30000)
    _wait(run, "gas", "s.n > 0")
    _no_errors(run)


# -- galleries -------------------------------------------------------------------------------
@pytest.mark.parametrize("page,images", [("nirspec_6927_pvgallery", "pv"), ("nirspec_6927_sedgallery", "sed"),
                                         ("gallery_6927", "pngs")])
def test_galleries(runs, portal_dir, page, images):
    doc = _json(portal_dir / "data" / images / "images.json")
    run = runs(f"{page}.html")
    run.wait_panes_settled()
    p = _panes(run)
    st = _wait(run, p["gallery"][0], "s.items > 0")
    assert st["items"] == len(doc["items"])
    assert all(r["url"].startswith(SECURE) for it in doc["items"] for r in it["roles"].values())
    _no_public_data(run)
    _no_errors(run)


@pytest.mark.parametrize("role,suffix", [("c2221", "_2221.png"), ("c1182", "_1182.png")])
def test_png_gallery_role_presets(runs, role, suffix):
    """The legacy gallery_6927_2221 / _1182 pages map to #role=<c2221|c1182>."""
    run = runs("gallery_6927.html", hash=f"role={role}")
    run.wait_panes_settled()
    assert run.store_get("role") == role
    p = _panes(run)
    _wait(run, p["gallery"][0], "s.cards > 0")
    run.page.wait_for_function(
        f"() => Array.from(document.querySelectorAll(\"section.pk-pane[data-pane-type='gallery'] img\"))"
        f".some((im) => (im.getAttribute('src') || '').endsWith('{suffix}'))", timeout=30000)
    _no_errors(run)
