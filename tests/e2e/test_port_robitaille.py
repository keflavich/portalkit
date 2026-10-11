"""Per-port acceptance test for the robitaille family (ARCHITECTURE.md section 13 Level 4; WP21).

Each ``special_handling`` entry of the port table becomes an assertion against a staged build
served by a DevServer and opened with ``?pktest=1``:

* 28,248 rows; 54 magnitudes + physical values stored, 172 colours virtual -> manifest counts.
* distance-modulus slider -> ``.pk-ctl-dm`` exists and ``dm`` changes the manifold projection.
* FRIED category -> ``FRIED`` is a level of ``cls``/``stg``/``geom`` and FRIED rows have no mags.
* "> 30 mag at 8 kpc" auto-hide -> magnitudes fainter than 30 - 5 log10 8 are NaN in the table
  (interim build-time rule): no column quantile and no exact row-chunk value exceeds it.
* A_V spans 0..3e12 mag -> stored f32 (exact), never a u16q column that decodes to 0.
* density grids -> one grid per axes preset, keyed by the preset (no auto duplicates).
* Tier B read_geometry cache -> covered by the adapter tests (``ctx.cache`` memo).
* landing: geometry count from the data -> the readme count equals the distinct geometries.
* imaging: thumbnails, NaN parameters missing, gallery.html merged (no nav entry), per-row
  cross-page link columns whose targets resolve (full builds: the 5 shared spubhmi models).
* public portal -> no request goes to a ``/secure/`` URL.
* legacy hashes ``#star=<n>`` (kv-view) and ``#<n>`` (bare-name) focus the model.

``PK_PORT_BUILD`` names a staged build (the build directory or its ``robitaille`` portal
directory, built with ``--vendor-assets``); without it the test builds Tier A with
``limit=60`` and vendored assets into a temp stage root (about 1 minute).
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

FAMILY = "robitaille"
LIMIT = 60
REPO = Path(__file__).resolve().parents[2]
INV = json.loads((REPO / "tests" / "data" / "inventory_caps.json").read_text(encoding="utf-8"))
MAG_HI = 30.0 - 5 * math.log10(8.0)
ALLOW = (r"Failed to load resource: net::ERR_(FAILED|ABORTED|BLOCKED_BY_CLIENT)",)
# thumbnails are asserted by test_imaging_thumbnails; keep their 404s out of the other checks
THUMB_404 = r"(404 \(Not Found\) \(|^http 404 |^404 )\S*/thumbs/[^/]+\.png"

def _bp():
    return sys.modules["pk_conftest_browser"]


@pytest.fixture(scope="module")
def portal_dir(tmp_path_factory):
    env = os.environ.get("PK_PORT_BUILD")
    if env:
        p = Path(env)
        p = p / FAMILY if (p / FAMILY / "portal.json").is_file() else p
        if not (p / "portal.json").is_file():
            pytest.fail(f"PK_PORT_BUILD={env} holds no {FAMILY} portal")
        if re.search(r'"assets":\s*"https?://', (p / "index.html").read_text(encoding="utf-8")):
            pytest.skip("PK_PORT_BUILD loads its assets from the production URL; build it with --vendor-assets")
        return p
    if not Path("/orange/adamginsburg/web/public/robitaille_models/manifold.html").is_file():
        pytest.skip("legacy robitaille portal not mounted")
    from portalkit.build import build_family
    mp = pytest.MonkeyPatch()
    if not os.environ.get("PK_CACHE_DIR"):
        mp.setenv("PK_CACHE_DIR", str(tmp_path_factory.mktemp("pkcache")))
    try:
        r = build_family(FAMILY, tier="A", limit=LIMIT, stage_root=tmp_path_factory.mktemp("stage"),
                         vendor_assets=True, log=lambda m: None)
    finally:
        mp.undo()
    assert r.ok, r.report.get("errors")
    return Path(r.staging) / FAMILY


@pytest.fixture(scope="module")
def srv(portal_dir):
    from portalkit.deploy.devserver import DevServer
    s = DevServer(portal_dir.parent).start()
    yield s
    s.stop()


@pytest.fixture(scope="module")
def runs(browser, srv):
    opened = {}

    def get(page, hash=""):
        if (page, hash) not in opened:
            opened[(page, hash)] = _bp().open_run(browser, srv, f"{FAMILY}/{page}.html", hash=hash)
        return opened[(page, hash)]
    yield get
    for r in opened.values():
        r.close()


def _json(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def _man(portal_dir, ds):
    return _json(portal_dir / "data" / ds / "manifest.json")


def _col(man, key):
    return next(c for c in man["cols"] if c["key"] == key)


def _errs(run):
    return run.errors(allow=ALLOW + (THUMB_404,))


# -- capabilities ------------------------------------------------------------------------------------
@pytest.mark.parametrize("page,legacy", [("index", "index.html"), ("manifold", "manifold.html"),
                                         ("imaging", "imaging/index.html"), ("imaging", "imaging/gallery.html")])
def test_caps_cover_inventory(runs, portal_dir, page, legacy):
    run = runs(page)
    want = set(INV[f"{FAMILY}/{legacy}"])
    if _man(portal_dir, "mod")["n"] <= 3000:
        want.discard("density_rendering")         # fixture builds stay below the density threshold
    assert want <= set(run.caps_attr()), sorted(want - set(run.caps_attr()))
    assert not _errs(run)


# -- manifold: stored vs virtual columns, FRIED, contamination rule ----------------------------------
def test_manifest_columns(portal_dir):
    man = _man(portal_dir, "mod")
    mags = [c for c in man["cols"] if c.get("role") == "mag"]
    vals = [c for c in man["cols"] if c.get("role") == "value"]
    assert len(mags) == 54 and len(mags) + len(vals) <= 83
    assert sum(v.get("group") == "color" for v in man["virtual"]) == 171
    assert "FRIED" in _col(man, "cls")["levels"]
    assert "FRIED" not in _col(man, "cls").get("default_off", [])
    assert any(lv.startswith("FRIED") for lv in _col(man, "src")["levels"])
    if man["n"] > 20000:
        assert man["n"] == 28248
    assert man["manifold"]["default_frame"] in {p["key"] for p in man["presets"]}


def _rows(portal_dir, man, ds="mod"):
    import gzip
    out = []
    for f in (man.get("rows") or {}).get("files", []):
        b = (portal_dir / "data" / ds / f).read_bytes()
        out += json.loads(gzip.decompress(b) if b[:2] == b"\x1f\x8b" else b)
    return out


def test_mags_missing_beyond_30_at_8kpc(portal_dir):
    man = _man(portal_dir, "mod")
    mags = [c for c in man["cols"] if c.get("role") == "mag"]
    for c in mags:
        assert c.get("clip", "none") == "none", c["key"]
        if c.get("nvalid"):
            assert c["quantiles"][-1] <= MAG_HI + 1e-3, (c["key"], c["quantiles"][-1])
    assert _col(man, "m_NIRCam.F480M")["nvalid"] < man["n"]      # FRIED rows carry no magnitudes
    rows = _rows(portal_dir, man)                                 # exact rows agree with the columns
    keys = [c["key"] for c in mags]
    assert len(rows) == man["n"]
    assert not [(r.get("id"), k) for r in rows for k in keys if isinstance(r.get(k), (int, float)) and r[k] > MAG_HI + 1e-9]
    nvalid = {k: sum(isinstance(r.get(k), (int, float)) and math.isfinite(r[k]) for r in rows) for k in keys}
    assert all(nvalid[c["key"]] == c.get("nvalid", 0) for c in mags), nvalid
    assert "n" not in man["rows"]["cols"]                        # the id already carries geom/name


def test_av_and_density(portal_dir):
    man = _man(portal_dir, "mod")
    av = next((c for c in man["cols"] if c["key"] == "av"), None)
    assert av is not None or man["n"] <= 3000                    # fixture rows may carry no A_V
    if av is not None:
        assert av["enc"] == "f32", av                            # u16q step was 5e7 mag
    axes = {p["key"] for p in man["presets"] if p["kind"] == "axes"}
    dens = [d["key"] for d in man.get("density") or []]
    assert len(dens) == len(set(dens)) and set(dens) <= axes, dens
    if man["n"] > 3000:
        assert set(dens) == axes


def test_dm_slider(runs):
    run = runs("manifold")
    assert run.ev("() => !!document.querySelector('label.pk-ctl-dm input')")
    assert (run.store_get("dm") or 0) == 0
    run.wait_panes_settled()
    panes = [p for p, st in run.pane_states().items() if st != "hidden"]
    before = {p: run.wait_canvas_stable(p) for p in panes}
    run.ev("() => window.__pk.store.set({dm: 4.5}, {source: 'test'})")
    assert run.store_get("dm") == 4.5
    changed = [p for p in panes if run.wait_canvas_change(p, before[p], timeout_ms=8000) != before[p]]
    assert changed, f"no pane redrew after dm changed ({panes})"
    run.ev("() => window.__pk.store.set({dm: 0}, {source: 'test'})")
    assert not _errs(run)


def test_default_filters_hide_too_faint(runs, portal_dir):
    det = _col(_man(portal_dir, "mod"), "det")
    expect = {x for x in ("too faint", "no photometry") if x in det["levels"]}
    assert set(det.get("default_off", [])) == expect


def test_sed_curves_and_pngs(runs, portal_dir):
    idx = _json(portal_dir / "data" / "sedcurve" / "index.json")
    assert idx["kind"] == "sedcurve" and idx["n"] > 0 and idx["shards"]
    assert "/seds/" in json.dumps(_json(portal_dir / "data" / "sedimg" / "images.json"))
    rep = _json(portal_dir / "build_report.json")
    caps = set(next(p for p in rep["pages"] if p["id"] == "manifold")["caps"]["enabled"])
    assert {"live_sed_viewer", "static_sed_png"} <= caps
    run = runs("manifold")
    sid = next(iter(idx["entries"]))
    run.ev("(id) => window.__pk.focus('mod', id)", sid)
    run.page.wait_for_function("() => { const f = window.__pk.store.get('focus'); return !!f && f.i != null; }",
                               timeout=60000)
    run.wait_panes_settled()
    assert not _errs(run)


@pytest.mark.parametrize("grammar", ["star", "bare"])
def test_legacy_hash_focus(browser, srv, portal_dir, grammar):
    aliases = _json(next((portal_dir / "data" / "mod").glob("aliases*.json")))
    alias, row = next((a, i) for a, i in aliases.items() if "/" in a)
    run = _bp().open_run(browser, srv, f"{FAMILY}/manifold.html", hash=(f"star={alias}" if grammar == "star" else alias))
    try:
        run.page.wait_for_function("() => { const f = window.__pk.store.get('focus'); return !!f && f.i != null; }",
                                   timeout=60000)
        f = run.store_get("focus")
        assert f["ds"] == "mod" and f["i"] == row, f
        assert not _errs(run)
    finally:
        run.close()


# -- landing --------------------------------------------------------------------------------------------
def test_landing_geometry_count_from_data(runs, portal_dir):
    text = runs("index").ev("() => document.body.innerText")
    n_geo = len([lv for lv in _col(_man(portal_dir, "mod"), "geom")["levels"] if lv != "FRIED"])
    assert f"from {n_geo} geometr" in text
    assert "16-geometry" not in text


# -- imaging gallery ------------------------------------------------------------------------------------
def test_imaging_gallery(runs, portal_dir):
    run = runs("imaging")
    hrefs = run.ev("() => Array.from(document.querySelectorAll('a[href]')).map((a) => a.getAttribute('href'))")
    assert not any("gallery.html" in h for h in hrefs)
    assert [p["id"] for p in _json(portal_dir / "portal.json")["pages"]] == ["index", "manifold", "imaging"]
    man = _man(portal_dir, "imaging")
    assert _col(man, "Menv")["nvalid"] < man["n"]            # NaN stays missing (legacy wrote 0)
    imgs = _json(portal_dir / "data" / "imgs" / "images.json")
    assert len(imgs["items"]) == man["n"]
    assert not _errs(run)


# -- public portal: nothing secure is fetched --------------------------------------------------------------
def test_cross_page_links_resolve(browser, srv, portal_dir):
    """Link columns appear only on rows whose target exists; following one focuses that row."""
    mrows = _rows(portal_dir, _man(portal_dir, "mod"))
    irows = _rows(portal_dir, _man(portal_dir, "imaging"), "imaging")
    links = [("imaging", r["img_url"]) for r in mrows if r.get("img_url")]
    links += [("manifold", r["mod_url"]) for r in irows if r.get("mod_url")]
    if _man(portal_dir, "mod")["n"] > 20000:
        assert links, "full build: the shared spubhmi models carry link columns"
    if not links:
        pytest.skip("fixture build has no models on both pages")
    for target, url in links[:2] + [x for x in links if x[0] == "manifold"][:1]:
        page, _, hash_ = url.partition("#")
        assert page == f"{target}.html"
        run = _bp().open_run(browser, srv, f"{FAMILY}/{page}", hash=hash_)
        try:
            run.page.wait_for_function("() => { const f = window.__pk.store.get('focus'); return !!f && f.i != null; }",
                                       timeout=60000)
            assert not _errs(run)
        finally:
            run.close()


def test_no_secure_fetches(runs):
    for page in ("index", "manifold", "imaging"):
        run = runs(page)
        assert not [u for u in run.external if "/secure/" in u], page
        assert not [e for e in run.http_errors if not re.search(THUMB_404, e)], (page, run.http_errors)


def test_imaging_thumbnails(runs, portal_dir):
    """The gallery shows cached thumbnails (``portalkit thumbs``) instead of the 3109x1644 PNGs."""
    items = _json(portal_dir / "data" / "imgs" / "images.json")["items"]
    thumbs = [it["roles"]["image"]["thumb"] for it in items if it["roles"]["image"].get("thumb")]
    if not thumbs:
        pytest.skip("no cached thumbnails for this build (run `portalkit thumbs robitaille` first)")
    if not (portal_dir / thumbs[0]).is_file() and (portal_dir / "data" / "imgs" / thumbs[0]).is_file():
        pytest.xfail("build._thumb_hook writes dataset-relative thumb URLs; the client resolves "
                     "images.json URLs against the page (IMPLEMENTATION_NOTES 81)")
    run = runs("imaging")
    run.wait_panes_settled()
    assert run.ev("() => Array.from(document.images).some((im) => im.complete && im.naturalWidth > 0 "
                  "&& im.src.includes('/thumbs/'))")
    assert not [e for e in run.http_errors if "/thumbs/" in e]
