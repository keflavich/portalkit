"""Port acceptance for the wd2 family (port table section 14.11, WP24 wave 3).

Drives a staged wd2 build through the DevServer with ``?pktest=1`` and the ``__pk`` hooks.
``PK_PORT_BUILD`` names a staged build built with ``--vendor-assets`` (the build directory or
its ``wd2/`` portal directory); without it the module builds a fixture portal (``portalkit
build wd2 --limit 50 --vendor-assets``) into a temp stage root. External requests (HiPS tiles,
SED PNGs) are aborted by the harness and recorded.

* index: one card per page with manifest counts, plus the NIRSpec 2640 portal link.
* map_ccd: CMD + CCD with the MIST isochrone and the shared reddening vector; S/N filter presets
  (the legacy default S/N > 20 applied on open; per-diagram CMD / CCD variants).
* wd2_colorcolor: the legacy diagram grid, the magnitude histogram, ``#<name>`` deep links.
* wd2_manifold: support beyond MAXD_GPU -> CPU (worker) projection; preselects as saved
  selections; the 2640 spectra are plotted from the nirspec2640 portal's record shards
  (foreign record set ``nirspec``, ``../nirspec2640/data/spec/``). The staged wd2 build holds
  no nirspec2640 portal, so the test writes a small synthetic record set for the wd2 spec ids
  under ``foreign_dir``, which ``server`` mounts at ``/nirspec2640/data/`` (WPE9 item 6) so the
  browser's real request for that foreign base resolves without per-test routing.
* sabbi: manifold with isochrones recorded as disabled (no HST MIST bands); the default frame
  is the first axes preset.
* wd2_sedgallery: gallery items = SED PNGs, live SED with the blackbody model, range brushes
  on bb_T / bb_av / chi2 / n_filt.
* wd2_wavelength_explorer: one rung per legacy WAVELENGTHS entry; ``?speed=`` honoured.
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
FAM = "wd2"
LEGACY = Path("/orange/adamginsburg/web/public/jwst/wd2/nirspec_2640/wd2_manifold.html")
MAXD_GPU = 28


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
        pytest.skip("legacy wd2 portal tree not mounted")
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
def foreign_dir(tmp_path_factory) -> Path:
    """Backing directory for the ``/nirspec2640/data/`` mount (see ``server``): the staged wd2
    build holds no nirspec2640 portal, so ``test_manifold_foreign_spectra`` writes a synthetic
    record set here under ``spec/`` before fetching it through the mount."""
    return tmp_path_factory.mktemp("nirspec2640")


@pytest.fixture(scope="module")
def server(portal_dir, foreign_dir):
    from portalkit.deploy.devserver import DevServer
    # mounts a sibling "nirspec2640" portal's data/ onto this server (WPE9 item 6) instead of
    # per-test Playwright ctx.route() interception, so the foreign base the wd2_manifold page
    # climbs to (``../nirspec2640/data/spec/...``) resolves as a real mounted directory.
    srv = DevServer(portal_dir.parent, mounts={"/nirspec2640/data/": foreign_dir}).start()
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


def _man(portal_dir: Path, ds: str) -> dict:
    return _json(portal_dir / "data" / ds / "manifest.json")


def _no_errors(run):
    errs = run.errors(allow=(_plugin().ABORTED_LOAD,))
    assert not errs, errs


def _panes(run) -> dict:
    """paneType -> [paneId, ...] in document order."""
    return run.ev("""() => { const o = {}; for (const s of document.querySelectorAll('section.pk-pane')) {
        (o[s.dataset.paneType] = o[s.dataset.paneType] || []).push(s.dataset.paneId); } return o; }""")


def _wait(run, pane: str, cond: str, timeout=60000):
    run.page.wait_for_function(
        f"() => {{ const h = window.__pk.app.layout.pane('{pane}'); const s = h && h.stats();"
        f" return !!(s && ({cond})); }}", timeout=timeout)
    return run.pane_stats(pane)


def _page_caps(portal_dir: Path, page: str) -> dict:
    rep = _json(portal_dir / "build_report.json")
    return next(p for p in rep["pages"] if p["id"] == page)["caps"]


# -- index -----------------------------------------------------------------------------------
def test_landing_cards(runs, portal_dir):
    run = runs("index.html")
    run.wait_panes_settled()
    run.page.wait_for_function(
        "() => Array.from(document.querySelectorAll('.pk-card-count')).some((c) => c.textContent.trim())",
        timeout=30000)
    cards = run.ev("""() => Array.from(document.querySelectorAll('.pk-card-link')).map((a) => ({
        href: a.getAttribute('href'), page: a.dataset.page || null,
        text: (a.querySelector('.pk-card-text') || {}).textContent || ''}))""")
    pages = {c["page"] for c in cards if c["page"]}
    assert {"map_ccd", "wd2_colorcolor", "wd2_manifold", "sabbi", "wd2_sedgallery",
            "wd2_wavelength_explorer"} <= pages
    spec = [c for c in cards if c["href"] and "/unified/nirspec2640/" in c["href"]]
    assert len(spec) == 1, cards
    n = _man(portal_dir, "ccd")["n"]
    ccd = next(c for c in cards if c["page"] == "map_ccd")
    assert f"{n:,} sources" in ccd["text"]
    _no_errors(run)


# -- map_ccd ---------------------------------------------------------------------------------
def test_map_ccd_iso_and_snr_presets(runs, portal_dir):
    man = _man(portal_dir, "ccd")
    keys = {p["key"] for p in man["presets"]}
    assert {f"snr{s}" for s in (5, 10, 20, 50, 100)} <= keys
    assert {f"snr{s}_{d}" for s in (10, 20, 50, 100) for d in ("cmd", "ccd")} <= keys
    caps = _page_caps(portal_dir, "map_ccd")
    assert {"isochrones_or_tracks", "extinction_vectors_on_ccd", "av_slider"} <= set(caps["enabled"])
    run = runs("map_ccd.html")
    run.wait_panes_settled()
    for pane in ("cmd", "ccd"):
        st = _wait(run, pane, "s.overlays >= 2")
        assert st["n"] == man["n"]
        ax = run.store_get(f"axes:{pane}")
        preset = next(p for p in man["presets"] if p["key"] == pane)
        assert (ax["x"], ax["y"]) == (preset["x"], preset["y"])
    p = _panes(run)
    assert p.get("sky") and p.get("list") and p.get("sed")
    snr20 = next(pr for pr in man["presets"] if pr["key"] == "snr20")
    f = run.store_get("filters:ccd")
    assert f["ranges"] == snr20["filters"]["ranges"]
    _no_errors(run)


# -- wd2_colorcolor --------------------------------------------------------------------------
def test_colorcolor_grid_and_hash(runs, portal_dir):
    man = _man(portal_dir, "src")
    cc = [p for p in man["presets"] if p["key"].startswith("cc_")]
    assert cc
    run = runs("wd2_colorcolor.html")
    run.wait_panes_settled()
    st = _wait(run, "grid", "s.n > 0")
    assert st["n"] == man["n"]
    p = _panes(run)
    assert p.get("hist") and p.get("detail") and p.get("list")
    _no_errors(run)
    sid = run.ev("""async () => { const c = await window.__pk.app.catalog('src');
        const r = await c.rows(3, 4); return r[0].id; }""")
    run2 = runs("wd2_colorcolor.html", hash=str(sid))
    run2.page.wait_for_function("() => { const f = window.__pk.store.get('focus'); return !!(f && f.i != null); }",
                                timeout=60000)
    assert str(run2.store_get("focus")["id"]) == str(sid)
    _no_errors(run2)


# -- wd2_manifold ----------------------------------------------------------------------------
def test_manifold_cpu_projection_and_preselects(runs, portal_dir):
    man = _man(portal_dir, "src")
    D = len(man["manifold"]["dims"])
    assert D > MAXD_GPU
    caps = _page_caps(portal_dir, "wd2_manifold")
    assert "spectrum_viewer_1d" in caps["enabled"] and "spectrum_viewer_1d" not in caps["disabled"]
    run = runs("wd2_manifold.html")
    run.wait_panes_settled()
    pane = _panes(run)["manifold"][0]
    st = _wait(run, pane, "s.n > 0 && s.projected")
    assert st["n"] == man["n"] and st["D"] == D
    assert st["path"] != "gc"
    sels = [p for p in man["presets"] if p["kind"] == "selection"]
    has = next(p for p in sels if p["key"] == "hasSpec")
    n_ids = len(has["sel"][4:].split(","))
    run.ev("(s) => window.__pk.app.applySelection('src', s)", has["sel"])
    st = _wait(run, pane, f"s.selected === {n_ids}")
    assert st["selected"] == n_ids
    # a large preselect is a deflated recipe (fullCov: a filter on obs) and replays in the page
    full = next((p for p in sels if p["key"] == "fullCov"), None)
    if full is not None:
        assert full["sel"].startswith("r:")
        want = run.ev("""async () => { const c = await window.__pk.app.catalog('src');
            const rows = await c.rows(0, c.n); return rows.filter((r) => r.obs === '15-16').length; }""")
        run.ev("(s) => window.__pk.app.applySelection('src', s)", full["sel"])
        st = _wait(run, pane, f"s.selected === {want}")
        assert want > 0 and st["selected"] == want
    # a NIRSpec star links its spectrum in the nirspec2640 portal
    assert "spec_url" in man["rows"]["cols"]
    _no_errors(run)


def test_manifold_legacy_kv_hash(runs, portal_dir):
    run = runs("wd2_manifold.html", hash="star=WD2-000014")
    run.page.wait_for_function("() => { const f = window.__pk.store.get('focus'); return !!(f && f.i != null); }",
                               timeout=60000)
    assert str(run.store_get("focus")["id"]) == "WD2-000014"
    _no_errors(run)


def test_manifold_foreign_spectra(server, runs, portal_dir, foreign_dir):
    """wd2 stages no spectrum shards: the spectrum pane reads ../nirspec2640/data/spec/, which
    ``server`` mounts onto ``foreign_dir`` (WPE9 item 6) rather than routing it per-test."""
    import numpy as np

    from portalkit.bundle import records
    from portalkit.spec import RecordSetSpec, Spectrum
    assert not (portal_dir / "data" / "nirspec").exists()
    rep = _json(portal_dir / "build_report.json")["datasets"]["nirspec"]
    assert rep["foreign"] is True and rep["files"] == 0
    # the spec names the wd2 catalogue carries (id_column 'spec'), read from the plain page
    run0 = runs("wd2_manifold.html")
    ref = run0.ev("() => JSON.parse(document.getElementById('pk-page').textContent).datasets.nirspec")
    assert ref["url"] == "../nirspec2640/data/spec/index.json" and ref["id_column"] == "spec", ref
    pick = run0.ev("""async () => { const c = await window.__pk.app.catalog('src');
        const r = await c.rows(0, c.n); const out = [];
        r.forEach((x, i) => { if (x.spec) out.push([i, String(x.spec)]); }); return out; }""")
    assert pick, "no wd2 source carries a NIRSpec spectrum name"
    w = np.linspace(1.0, 5.0, 64)

    def spec(rid):
        return Spectrum(wave=w, channels={"flux": 1e-3 * (1 + w), "err": np.full(w.size, 1e-4)},
                        units={"flux": "Jy"})
    records.write(RecordSetSpec("spec", "pks1", "src", sorted({sid for _, sid in pick}), spec), foreign_dir / "spec")
    assert (foreign_dir / "spec" / "index.json").is_file()
    before = len(server.requests)
    run = runs("wd2_manifold.html")
    run.wait_panes_settled()
    p = _panes(run)
    assert "spectrum1d" in p, p
    # expanded in the page layout: no reveal needed
    run.page.wait_for_function("(p) => !!window.__pk.app.layout.pane(p)", arg=p["spectrum1d"][0], timeout=30000)
    run.ev(f"() => window.__pk.store.set({{focus: {{ds: 'src', i: {pick[0][0]}, id: null}}}}, {{source: 'test'}})")
    st = _wait(run, p["spectrum1d"][0], "s.n > 0")
    assert st["n"] > 0
    seen = [rq.path for rq in server.requests[before:] if rq.path.startswith("/nirspec2640/data/")]
    assert any(p.startswith("/nirspec2640/data/spec/index.json") for p in seen)
    assert any(p.startswith("/nirspec2640/data/spec/") and "index.json" not in p for p in seen), seen
    _no_errors(run)


# -- sabbi -----------------------------------------------------------------------------------
def test_sabbi_manifold(runs, portal_dir):
    man = _man(portal_dir, "sabbi")
    caps = _page_caps(portal_dir, "sabbi")
    assert "isochrones_or_tracks" in caps["disabled"]
    run = runs("sabbi.html")
    run.wait_panes_settled()
    pane = _panes(run)["manifold"][0]
    st = _wait(run, pane, "s.n > 0")
    assert st["n"] == man["n"]
    # the initial frame is the default_frame axes preset (HST CMD), not PCA
    pre = next(p for p in man["presets"] if p["key"] == man["manifold"]["default_frame"])
    dims, F = st["dims"], st["frame"]
    D = len(dims)
    assert [dims[i] for i in range(D) if abs(F[i]) > 1e-9] == [pre["x"]]
    assert [dims[i] for i in range(D) if abs(F[D + i]) > 1e-9] == [pre["y"]]
    _no_errors(run)


# -- wd2_sedgallery --------------------------------------------------------------------------
def test_sedgallery(runs, portal_dir):
    man = _man(portal_dir, "seds")
    items = _json(portal_dir / "data" / "sedimg" / "images.json")["items"]
    run = runs("wd2_sedgallery.html")
    run.wait_panes_settled()
    p = _panes(run)
    st = _wait(run, p["gallery"][0], "s.items > 0")
    assert st["items"] == len(items)
    assert run.pane_stats(p["list"][0])["rows"] == man["n"]
    page = run.ev("() => window.__pk.app.page.layout")
    hists = [c for c in page["row"][2]["col"] if c["pane"] == "hist"]
    assert {c["options"]["expr"] for c in hists} == {"bb_T", "bb_av", "bb_chi2_red", "n_filt_independent"}
    assert len(p["hist"]) >= 2
    run.ev("() => window.__pk.store.set({focus: {ds: 'seds', i: 0, id: null}}, {source: 'test'})")
    _wait(run, p["sed"][0], "s.i === 0")
    _no_errors(run)


# -- wd2_wavelength_explorer -----------------------------------------------------------------
def test_wavelength_rungs_and_speed(runs, portal_dir):
    hips = _json(portal_dir / "data" / "hips.json")
    run = runs("wd2_wavelength_explorer.html", query="speed=2")
    run.wait_panes_settled()
    pane = _panes(run)["wavelength"][0]
    st = _wait(run, pane, "s.rungs && s.rungs.length > 0")
    assert st["rungs"] == [r["id"] for r in hips["rungs"]]
    assert st["speed"] == 2
    _no_errors(run)
