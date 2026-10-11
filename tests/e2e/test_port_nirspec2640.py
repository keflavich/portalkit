"""Port acceptance for the nirspec2640 family (port table section 14.4, WP25 wave 3).

Drives a staged nirspec2640 build through the DevServer with ``?pktest=1`` and the ``__pk``
hooks. ``PK_PORT_BUILD`` names a staged build built with ``--vendor-assets`` (the build
directory or its ``nirspec2640/`` portal directory); without it the module builds a fixture
portal (``portalkit build nirspec2640 --limit 50 --vendor-assets``) into a temp stage root.
External requests (HiPS tiles, public figures) are aborted by the harness and recorded.

* index: public portal; page cards with manifest counts; cards to the unified Wd2 portal.
* map: HiPS + MSA shutter / dither polygons, focus drives the 1D spectrum (PRISM, G140M and
  G395M segments), the stitched 2D spectrum (pki1) and the products list (secure FITS with
  login badges, public MAST).
* legacy ``#<name>`` deep links focus the source (bare-name / specviewer grammars).
* colorcolor (Wd2 density, catalogue -> synthetic connectors, BD histogram brush),
  red_excess (legacy median(r_wd2) PNG rasters under the NIRSpec sources, binned median(r_wd2)
  background selected from the stat dropdown, sources on the offset-vs-distance backdrop), manifold,
  specviewer, icecolumns (log axes + 1:1 line).
* pvgallery / sedgallery / gallery_2640 (role presets nb / bb via the hash); the live SED plots
  the synthetic magnitudes of a source without a catalogue match.
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
FAM = "nirspec2640"
SECURE = "https://data.rc.ufl.edu/secure/"
PUB = "https://data.rc.ufl.edu/pub/adamginsburg/jwst/wd2/nirspec_2640/"
LEGACY = Path("/orange/adamginsburg/jwst/spectra/nirspec_2640/nirspec_2640_sources.json")


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
        pytest.skip("legacy nirspec_2640 tree not mounted")
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


def _no_secure_fetch(run):
    """Public portal: no secure URL is fetched by the page (FITS links are only links)."""
    assert not [u for u in run.external if u.startswith(SECURE)]


def _multi_index(portal_dir) -> int:
    """Row index of the first source with PRISM + G140M + G395M (fixture order = build order)."""
    run_ids = _json(portal_dir / "data" / "pngs" / "images.json")["items"]
    for i, it in enumerate(run_ids):
        if "," in str((it.get("tags") or {}).get("gratings", "")):
            return int(it.get("row", i))
    pytest.skip("no multi-grating source in this build")


# -- index -----------------------------------------------------------------------------------
def test_landing_public_cards(runs, portal_dir):
    portal = _json(portal_dir / "portal.json")
    assert portal["visibility"] == "public"
    run = runs("index.html")
    run.wait_panes_settled()
    run.page.wait_for_function(
        "() => Array.from(document.querySelectorAll('.pk-card-count')).some((c) => c.textContent.trim())",
        timeout=30000)
    cards = run.ev("""() => Array.from(document.querySelectorAll('.pk-card-link')).map((a) => ({
        href: a.getAttribute('href'), page: a.dataset.page || null,
        badge: !!a.querySelector('.pk-login-badge'),
        count: (a.querySelector('.pk-card-count') || {}).textContent || ''}))""")
    assert len([c for c in cards if c["page"]]) == 9
    ext = [c for c in cards if c["href"] and c["href"].startswith(PUB)]
    assert len(ext) == 4 and not any(c["badge"] for c in ext), cards
    assert all(c["href"].startswith(PUB + "unified/wd2/") for c in ext), ext
    n = _json(portal_dir / "data" / "src" / "manifest.json")["n"]
    counted = [c for c in cards if c["page"] and c["count"].strip()]
    assert counted and any(re.sub(r"[,\s ]", "", c["count"]).startswith(str(n)) for c in counted), counted
    _no_secure_fetch(run)
    _no_errors(run)


# -- map -------------------------------------------------------------------------------------
def test_map_sky_spectra_products(runs, portal_dir):
    ovl = _json(portal_dir / "data" / "overlays.json")
    assert {"msa_shutters", "dither", "one_to_one"} <= {layer["id"] for layer in ovl["layers"]}
    run = runs("nirspec_2640_map.html")
    run.wait_panes_settled()
    p = _panes(run)
    for t in ("list", "sky", "scatter", "detail", "spectrum1d", "spectrum2d"):
        assert t in p, (t, p)
    _wait(run, p["sky"][0], "s.layers && s.aladin !== 'error'")
    _focus(run, _multi_index(portal_dir))
    s1 = _wait(run, p["spectrum1d"][0], "s.n > 0")
    assert set(s1["segments"]) == {"PRISM", "G140M", "G395M"}
    s2 = _wait(run, p["spectrum2d"][0], "s.nx > 0")
    assert s2["mode"] == "pki1" and s2["ny"] > 1
    run.page.wait_for_function("() => document.querySelectorAll('.pk-product').length > 0", timeout=30000)
    prods = run.ev("""() => Array.from(document.querySelectorAll('.pk-product')).map((li) => ({
        href: li.querySelector('a').href, badge: !!li.querySelector('.pk-login-badge')}))""")
    fits = [x for x in prods if x["href"].endswith(".fits")]
    assert len(fits) == 6 and all(x["href"].startswith(SECURE) and x["badge"] for x in fits), prods
    mast = [x for x in prods if "mast.stsci.edu" in x["href"]]
    assert len(mast) == 1 and not mast[0]["badge"]
    _no_secure_fetch(run)
    _no_errors(run)


# -- legacy deep links ------------------------------------------------------------------------
@pytest.mark.parametrize("page", ["nirspec_2640_map", "nirspec_2640_specviewer"])
def test_legacy_bare_name_hash(runs, page):
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


# -- colorcolor ------------------------------------------------------------------------------
def test_colorcolor_density_pairs_bd_split(runs, portal_dir):
    assert _json(portal_dir / "data" / "bg" / "manifest.json")["n"] > 0
    run = runs("nirspec_2640_colorcolor.html")
    run.wait_panes_settled()
    for pane, (x, y) in (("ccd", ("f182m-f212n", "f405n-f466n")), ("cmd", ("f182m-f212n", "f212n"))):
        st = _wait(run, pane, "s.overlays >= 2")
        assert st["n"] > 0
        ax = run.store_get(f"axes:{pane}") or {}
        assert (ax.get("x", x), ax.get("y", y)) == (x, y)
    hs = _wait(run, "bd", "s.total > 0")
    assert hs["expr"] == "s_f444w"
    lo, hi = run.ev("""async () => { const c = await window.__pk.hub.catalog('src');
        const v = (await c.values('s_f444w')).filter(Number.isFinite).sort((a, b) => a - b);
        return [v[Math.floor(v.length / 2)], v[v.length - 1] + 1]; }""")
    # the BD split is a range filter on the synthetic F444W magnitude (fainter = BD candidates)
    n = run.ev(f"() => window.__pk.app.layout.pane('bd').brush({{lo: {lo}, hi: {hi}}})")
    assert n >= 1
    st = _wait(run, "bd", "s.range && s.range[0] !== null")
    assert st["range"][0] == pytest.approx(lo, rel=1e-6) and 0 < st["shown"] <= st["total"]
    assert n < st["total"]
    _no_errors(run)


# -- red excess ------------------------------------------------------------------------------
def test_red_excess_stat_background(runs, portal_dir):
    man = _json(portal_dir / "data" / "redx" / "manifest.json")
    stat_keys = [d["key"] for d in man["density"] if d.get("stat") not in (None, "count")]
    assert "y182212_rwd2" in stat_keys
    run = runs("nirspec_2640_red_excess.html")
    run.wait_panes_settled()
    st = _wait(run, "rwd2", "s.n > 0")
    assert st["stat"] is None
    sel = "section.pk-pane[data-pane-id='rwd2'] select.pk-stat-select"
    run.page.wait_for_function(f"() => {{ const s = document.querySelector(\"{sel}\"); return s && !s.hidden; }}",
                               timeout=30000)
    run.page.select_option(sel, "y182212_rwd2")
    st = _wait(run, "rwd2", "s.stat === 'y182212_rwd2'")
    assert st["overlays"] >= 2
    st = _wait(run, "redx", "s.n > 0 && s.overlays >= 2")
    # the legacy median-r_wd2 PNG for the current axes is requested (the harness aborts it)
    assert any(u.endswith("redx_bg_y182212_rwd2.png") for u in run.external), run.external
    n_src = _json(portal_dir / "data" / "src" / "manifest.json")["n"]
    st = _wait(run, "offset", "s.n > 0 && s.overlays >= 1")
    assert st["n"] == n_src                       # NIRSpec sources over the catalogue backdrop
    _no_errors(run)


# -- manifold --------------------------------------------------------------------------------
def test_manifold(runs, portal_dir):
    man = _json(portal_dir / "data" / "src" / "manifest.json")
    run = runs("nirspec_2640_manifold.html")
    run.wait_panes_settled()
    assert run.page.title().startswith("Property manifold")
    p = _panes(run)
    st = _wait(run, p["manifold"][0], "s.n > 0 && s.D > 0")
    assert st["n"] == man["n"] and st["D"] >= 10
    _focus(run, 2)
    _wait(run, p["spectrum1d"][0], "s.n > 0")
    _no_errors(run)


# -- specviewer ------------------------------------------------------------------------------
def test_specviewer_bands_and_2d(runs, portal_dir):
    run = runs("nirspec_2640_specviewer.html")
    run.wait_panes_settled()
    p = _panes(run)
    _focus(run, _multi_index(portal_dir))
    s1 = _wait(run, p["spectrum1d"][0], "s.n > 0")
    assert s1["xr"][0] < 2.0 < s1["xr"][1]
    assert all(v is True or v is False for v in s1["segments"].values())
    s2 = _wait(run, p["spectrum2d"][0], "s.nx > 0")
    assert s2["mode"] == "pki1"
    _no_errors(run)


# -- icecolumns ------------------------------------------------------------------------------
def test_icecolumns_log_axes_and_one_to_one(runs):
    run = runs("nirspec_2640_icecolumns.html")
    run.wait_panes_settled()
    st = _wait(run, "ice", "s.overlays >= 1 && s.view")
    assert st["view"].get("xlog") and st["view"].get("ylog"), st["view"]
    _no_errors(run)


# -- galleries -------------------------------------------------------------------------------
@pytest.mark.parametrize("page,images", [("nirspec_2640_pvgallery", "pv"), ("nirspec_2640_sedgallery", "sed"),
                                         ("gallery_2640", "pngs")])
def test_galleries(runs, portal_dir, page, images):
    doc = _json(portal_dir / "data" / images / "images.json")
    run = runs(f"{page}.html")
    run.wait_panes_settled()
    p = _panes(run)
    st = _wait(run, p["gallery"][0], "s.items > 0")
    assert st["items"] == len(doc["items"])
    assert all(r["url"].startswith(PUB) for it in doc["items"] for r in it["roles"].values())
    _no_secure_fetch(run)
    _no_errors(run)


def test_sed_synthetic_points(runs, portal_dir):
    """Port table 14.4 (sedgallery): the live SED plots every synthetic filter, so a source
    without a catalogue match shows one point per finite synthetic magnitude."""
    import math

    from astropy.table import Table
    sp = Table.read(LEGACY.parent / "nirspec_2640_synthphot.ecsv")
    cols = [c for c in sp.colnames if c.startswith("synth_f")]
    nfin = {str(r["SrcName"]): sum(math.isfinite(float(r[c])) for c in cols) for r in sp if not bool(r["cat_matched"])}
    items = _json(portal_dir / "data" / "pngs" / "images.json")["items"]
    cand = [(nfin[str(it.get("id"))], int(it.get("row", i))) for i, it in enumerate(items) if str(it.get("id")) in nfin]
    if not cand:
        pytest.skip("no source without a catalogue match in this build")
    want, row = max(cand)
    assert want == len(cols) == 20                      # incl. the 4 synthetic-only bands
    run = runs("nirspec_2640_sedgallery.html")
    run.wait_panes_settled()
    sed = _panes(run)["sed"][0]
    _focus(run, row)
    st = _wait(run, sed, f"s.i === {row} && s.n > 0")
    assert st["n"] == want and all(p["key"].startswith("s_") for p in st["points"])
    assert {"s_f210m", "s_f360m", "s_f480m", "s_f444w"} <= {p["key"] for p in st["points"]}
    _no_errors(run)


@pytest.mark.parametrize("role,suffix", [("nb", "_nb.png"), ("bb", "_bb.png")])
def test_png_gallery_role_presets(runs, role, suffix):
    """The legacy gallery_2640_nb / _bb pages map to #role=<nb|bb>."""
    run = runs("gallery_2640.html", hash=f"role={role}")
    run.wait_panes_settled()
    assert run.store_get("role") == role
    p = _panes(run)
    _wait(run, p["gallery"][0], "s.cards > 0")
    run.page.wait_for_function(
        f"() => Array.from(document.querySelectorAll(\"section.pk-pane[data-pane-type='gallery'] img\"))"
        f".some((im) => (im.getAttribute('src') || '').endsWith('{suffix}'))", timeout=30000)
    _no_errors(run)
