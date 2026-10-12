"""Per-port acceptance test for the spherex family (ARCHITECTURE.md section 13 Level 4; WP27).

Each ``special_handling`` entry of the port table becomes an assertion against a staged build
served by a DevServer and opened with ``?pktest=1``:

* one shared catalog bundle feeds all six pages -> every source page fetches data/src and no
  page embeds the catalog (the legacy pages embedded it four times).
* shift-drag pan bug of the legacy spectrum views (browse, colours and the SED view) -> a plain
  drag in the spectrum1d pane moves ``spec.xr`` on every one of several mousemove steps, not only
  the first; on the sed page the pane is in SED mode (log x) and draws the seds photometry.
* index.html hand patch -> the landing page carries the "Grand-tour manifold" card.
* Yoo W51 locus, the 26 YooW51red lower limits (right arrows) and the divider -> dim-space
  overlay layers on the CCD scatter.
* sexagesimal cone search -> the browse list search finds a source from its RA/Dec.
* Planck 857 overlay and the 60 go-to regions -> hips.json overlay (opacity 0.5) and bookmarks.
* nuFnu/Fnu toggle -> the sed pane starts in nufnu and the toggle switches units.
* legacy ``#<id>`` hashes (browse, sed, manifold) focus the source.
* public portal -> no request goes to a ``/secure/`` URL.

``PK_PORT_BUILD`` names a staged build (the build directory or its ``spherex`` portal directory,
built with ``--vendor-assets``); without it the test builds Tier A with ``limit=24`` and
vendored assets into a temp stage root.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

FAMILY = "spherex"
LIMIT = 24
REPO = Path(__file__).resolve().parents[2]
INV = json.loads((REPO / "tests" / "data" / "inventory_caps.json").read_text(encoding="utf-8"))
ALLOW = (r"Failed to load resource: net::ERR_(FAILED|ABORTED|BLOCKED_BY_CLIENT)",)
WAIVED = {"sed": {"static_sed_png"}}
PAGES = {"index": "index.html", "colours": "colours.html", "browse": "browse.html", "map": "map.html",
         "sed": "sed.html", "manifold": "spherex_manifold.html"}
FOCUSED = "() => { const f = window.__pk.store.get('focus'); return !!f && f.i != null; }"


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
    if not Path("/orange/adamginsburg/web/public/spherex/portal/colours.html").is_file():
        pytest.skip("legacy spherex portal not mounted")
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


def _errs(run):
    return run.errors(allow=ALLOW)


def _panes(run) -> dict:
    """paneType -> [paneId, ...] in document order."""
    return run.ev("""() => { const o = {}; for (const s of document.querySelectorAll('section.pk-pane')) {
        (o[s.dataset.paneType] = o[s.dataset.paneType] || []).push(s.dataset.paneId); } return o; }""")


def _wait(run, pane: str, cond: str, timeout=60000):
    run.page.wait_for_function(
        f"() => {{ const h = window.__pk.app.layout.pane('{pane}'); const s = h && h.stats(); return !!(s && ({cond})); }}",
        timeout=timeout)
    return run.pane_stats(pane)


def _spec_ids(portal_dir, ds="spec"):
    return list(_json(portal_dir / "data" / ds / "index.json")["entries"])


def _records(portal_dir, ds="spec"):
    """id -> pks1 header of every record of a shard-layout record set (entries: [shard, off, len])."""
    import gzip
    import struct
    idx = _json(portal_dir / "data" / ds / "index.json")
    out = {}
    blobs = {}
    for rid, (sh, off, ln) in idx["entries"].items():
        if sh not in blobs:
            blobs[sh] = (portal_dir / "data" / ds / idx["shards"][sh]).read_bytes()
        raw = gzip.decompress(blobs[sh][off:off + ln])
        assert raw[:4] == b"PKS1", rid
        (hl,) = struct.unpack("<I", raw[4:8])
        out[rid] = json.loads(raw[8:8 + hl])
    return out


# -- capabilities ------------------------------------------------------------------------------------
@pytest.mark.parametrize("page", list(PAGES))
def test_caps_cover_inventory(runs, portal_dir, page):
    run = runs(page)
    want = set(INV[f"{FAMILY}/{PAGES[page]}"]) - WAIVED.get(page, set())
    if _man(portal_dir, "src")["n"] <= 3000:
        want.discard("density_rendering")         # 2442 sources: below the density threshold
    assert want <= set(run.caps_attr()), sorted(want - set(run.caps_attr()))
    assert not _errs(run)


# -- one shared catalog --------------------------------------------------------------------------------
def test_one_shared_catalog_bundle(runs, portal_dir):
    cats = sorted(p.parent.name for p in (portal_dir / "data").glob("*/manifest.json"))
    assert cats == ["seds", "src"], cats
    n = _man(portal_dir, "src")["n"]
    for page in ("colours", "browse", "map", "manifold", "sed"):
        html = (portal_dir / f"{page}.html").read_text(encoding="utf-8")
        assert "const DATA" not in html and "spectra/" not in html, page
        mans = set(re.findall(r"data/([a-z]+)/manifest\.json", html))
        assert mans == ({"seds"} if page == "sed" else {"src"}), (page, mans)
        run = runs(page)
        run.wait_panes_settled()
        if page == "sed":
            continue
        got = run.ev("async () => { const c = await window.__pk.app.catalog('src'); return c.n; }")
        assert got == n, (page, got, n)
        assert not _errs(run)


# -- landing: the recorded hand patch ----------------------------------------------------------------
def test_landing_carries_manifold_card(runs):
    run = runs("index")
    run.wait_panes_settled()
    text = run.ev("() => document.body.innerText")
    assert "Grand-tour manifold" in text and "Geodesic interpolation between named views" in text
    hrefs = run.ev("() => Array.from(document.querySelectorAll('a[href]')).map((a) => a.getAttribute('href'))")
    for page in ("colours", "browse", "map", "sed", "manifold"):
        assert any(h.split("#")[0].split("?")[0].endswith(f"{page}.html") for h in hrefs), page
    assert not _errs(run)


# -- spectrum1d: continuous drag pan (legacy one-shot shift-drag pan bug) ----------------------------
@pytest.mark.parametrize("page", ["browse", "colours", "sed"])
def test_spectrum_drag_pan_is_continuous(runs, portal_dir, page):
    import math
    run = runs(page)
    run.wait_panes_settled()
    sp = _panes(run)["spectrum1d"][0]
    ds = "seds" if page == "sed" else "src"
    sid = _spec_ids(portal_dir, "sedpks" if page == "sed" else "spec")[0]
    run.ev("([ds, id]) => window.__pk.focus(ds, id)", [ds, sid])
    run.page.wait_for_function(FOCUSED, timeout=60000)
    st0 = _wait(run, sp, f"s.id === {sid!r} && Array.isArray(s.xr) && s.xr[1] > s.xr[0]"
                         + (" && (s.points || []).length > 0" if page == "sed" else ""))
    logx = page == "sed"
    assert st0["mode"] == ("sed" if logx else "spectrum") and st0["logx"] is logx, st0
    if logx:
        # SED mode: the seds photometry shares the log-x view with the spectrum and widens it
        wl = [p["wl"] for p in st0["points"]]
        assert st0["xr"][0] <= min(wl) and st0["xr"][1] >= max(wl), (st0["xr"], wl)
    tx = (lambda v: math.log10(v)) if logx else (lambda v: v)
    run.ev("() => window.__pk.store.set({spec: Object.assign({}, window.__pk.store.get('spec') || {}, "
           "{xr: undefined, yr: undefined})}, {source: 'test'})")
    canvas = run.page.locator(f"section.pk-pane[data-pane-id='{sp}'] canvas.pk-spec-canvas").first
    box = canvas.bounding_box()
    assert box and box["width"] > 150 and box["height"] > 80
    x0, y0 = box["x"] + box["width"] * 0.6, box["y"] + box["height"] * 0.45
    m = run.page.mouse
    m.move(x0, y0)
    m.down()
    seen = []
    try:
        for k in range(1, 6):
            m.move(x0 - 22 * k, y0)
            run.page.wait_for_function(
                "(prev) => { const s = window.__pk.store.get('spec'); return !!(s && Array.isArray(s.xr)"
                " && (prev === null || s.xr[0] !== prev)); }", arg=(seen[-1][0] if seen else None), timeout=10000)
            seen.append(tuple(run.store_get("spec")["xr"]))
    finally:
        m.up()
    # every step moved the view, always the same way, by the same amount per 22 px (in log10
    # wavelength on the log-x SED view)
    assert len(set(seen)) == 5, seen
    seen_t = [(tx(a), tx(b)) for a, b in seen]
    lo = [a for a, _ in seen_t]
    assert all(b > a for a, b in zip(lo, lo[1:])), seen          # dragging left shows longer wavelengths
    widths = [b - a for a, b in seen_t]
    assert max(widths) - min(widths) <= 1e-4 * max(widths), widths  # pan, not zoom
    steps = [b - a for a, b in zip(lo, lo[1:])]
    assert max(steps) <= 1.5 * min(steps), steps
    # the spectrum re-renders at the panned range
    st = run.pane_stats(sp)
    assert abs(st["xr"][0] - seen[-1][0]) < 1e-6 * max(1.0, abs(seen[-1][0])), (st["xr"], seen[-1])
    assert not _errs(run)


def test_irs_segment_present(runs, portal_dir):
    """A source with a Spitzer IRS spectrum shows two segments (SPHEREx + IRS)."""
    heads = _records(portal_dir)
    for h in heads.values():
        assert h["unit_w"] == "um" and h["channels"][0]["name"] == "flux" and h["channels"][0]["unit"] == "Jy"
    irs = [rid for rid, h in heads.items() if [g[2] for g in h.get("segments") or []] == ["SPHEREx", "IRS"]]
    assert irs, "the fixture keeps sources with an IRS spectrum"
    sid = irs[0]
    run = runs("browse")
    run.wait_panes_settled()
    sp = _panes(run)["spectrum1d"][0]
    run.ev("(id) => window.__pk.focus('src', id)", sid)
    st = _wait(run, sp, f"s.id === {sid!r} && s.segments && Object.keys(s.segments).length === 2")
    assert set(st["segments"]) == {"SPHEREx", "IRS"} and all(st["segments"].values())
    assert not _errs(run)


# -- browse: sexagesimal cone search ----------------------------------------------------------------------
def _sexa(ra, dec):
    h = ra / 15.0
    hh, mm = int(h), int((h - int(h)) * 60)
    ss = (h - hh - mm / 60) * 3600
    sgn = "-" if dec < 0 else "+"
    d = abs(dec)
    dd, dm = int(d), int((d - int(d)) * 60)
    ds = (d - dd - dm / 60) * 3600
    return f"{hh:02d}:{mm:02d}:{ss:05.2f} {sgn}{dd:02d}:{dm:02d}:{ds:04.1f}"


def test_browse_sexagesimal_search(runs):
    run = runs("browse")
    run.wait_panes_settled()
    lst = _panes(run)["list"][0]
    rows0 = run.pane_stats(lst)["rows"]
    rec = run.ev("""async () => { const c = await window.__pk.app.catalog('src');
        const r = await c.record(3); return {id: String(await c.id(3)), ra: r.ra, dec: r.dec}; }""")
    assert rec["ra"] is not None and rec["dec"] is not None, rec
    q = f"{_sexa(float(rec['ra']), float(rec['dec']))} 3\""
    loc = run.page.locator(f"section.pk-pane[data-pane-id='{lst}'] input.pk-list-search").first
    try:
        loc.fill(q)
        _wait(run, lst, f"s.rows >= 1 && s.rows < {rows0}")
        text = run.ev(f"() => document.querySelector(\"section.pk-pane[data-pane-id='{lst}'] .pk-list-rows\").innerText")
        assert rec["id"] in text, (q, text[:300])
    finally:
        loc.fill("")
    _wait(run, lst, f"s.rows === {rows0}")
    assert not _errs(run)


# -- map: Planck overlay and go-to bookmarks -------------------------------------------------------------
def test_map_hips_planck_and_bookmarks(runs, portal_dir):
    hips = _json(portal_dir / "data" / "hips.json")
    assert sum(bool(b.get("default")) for b in hips["base"]) == 1
    assert hips["base"][0]["default"] and "AllWISE" in hips["base"][0]["url"]
    (pl,) = hips["overlays"]
    assert "PLANCK" in pl["url"] and pl["opacity"] == 0.5 and pl.get("default") is True
    bms = hips["bookmarks"]
    assert len(bms) == 60 and all(0 < b["fov"] <= 360 for b in bms)
    assert all(re.fullmatch(r".+ \(\d+\)", b["name"]) for b in bms)
    assert hips["target"] == {"ra": 266.4, "dec": -28.94, "fov": 340.0}
    run = runs("map")
    sky = _panes(run)["sky"][0]
    st = _wait(run, sky, "s.layers && s.view")
    ov = {o["id"]: o["opacity"] for o in st["layers"]["overlays"]}
    assert ov.get(pl["id"]) == 0.5, st["layers"]
    assert not [u for u in run.external if "/secure/" in u]
    assert not _errs(run)


# -- colours: Yoo W51 overlays --------------------------------------------------------------------------
def test_colours_yoo_overlays(runs, portal_dir):
    doc = _json(portal_dir / "data" / "overlays.json")
    lay = {ly["id"]: ly for ly in doc["layers"]}
    assert {"yoo_upper", "yoo_lower", "yoo_red", "yoo_red_limits", "yoo_divider"} <= set(lay)
    lim = lay["yoo_red_limits"]
    assert lim["kind"] == "polyline" and len(lim["items"]) == 26 and lim["name"].endswith("(26)")
    assert all(ly["space"] == "dim" for ly in lay.values())
    assert "/orange/" not in json.dumps(doc)
    run = runs("colours")
    run.wait_panes_settled()
    ccd = "ccd"
    ax = _wait(run, ccd, "true")
    assert ax is not None
    axes = run.store_get(f"axes:{ccd}") or {}
    if axes:
        assert axes.get("x") == "f162m-f210m" and axes.get("y") == "f360m-f480m", axes
    run.ev("() => window.__pk.store.set({overlays: Object.assign({}, window.__pk.store.get('overlays') || {}, "
           "{yoo_upper: true, yoo_red_limits: true, yoo_divider: true})}, {source: 'test'})")
    assert not _errs(run)


# -- sed: nuFnu / Fnu toggle -----------------------------------------------------------------------------
def test_sed_units_toggle(runs, portal_dir):
    run = runs("sed")
    run.wait_panes_settled()
    sed = _panes(run)["sed"][0]
    sid = next(iter(_json(portal_dir / "data" / "sedspec" / "index.json")["entries"]))
    run.ev("(id) => window.__pk.focus('seds', id)", sid)
    st = _wait(run, sed, f"s.id === {sid!r} && s.points.length > 0")
    assert st["units"] == "nufnu"
    btns = run.page.locator(f"section.pk-pane[data-pane-id='{sed}'] .pk-sed-units button")
    assert btns.count() == 2
    btns.nth(0).click()
    st2 = _wait(run, sed, "s.units === 'fnu'")
    assert st2["points"] and all(p["fnu"] > 0 for p in st2["points"] if p["fnu"] is not None)
    btns.nth(1).click()
    _wait(run, sed, "s.units === 'nufnu'")
    st3 = _wait(run, sed, "s.sedcurves.length > 0")
    assert "flux" in st3["sedcurves"]
    assert not _errs(run)


# -- legacy hashes -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("page,ds,fmt", [("browse", "src", "{id}"), ("manifold", "src", "{id}"),
                                         ("manifold", "src", "star={id}&view=co_ice"), ("sed", "seds", "{id}")])
def test_legacy_hash_focus(browser, srv, portal_dir, page, ds, fmt):
    sid = (_spec_ids(portal_dir) if ds == "src" else list(_json(portal_dir / "data" / "sedspec" / "index.json")["entries"]))[1]
    # the legacy manifold wrote its hash with URLSearchParams ('+' -> %2B); bare names are
    # encodeURIComponent'd by the legacy links
    from urllib.parse import quote
    run = _bp().open_run(browser, srv, f"{FAMILY}/{page}.html", hash=fmt.format(id=quote(sid, safe="")))
    try:
        run.page.wait_for_function(FOCUSED, timeout=60000)
        f = run.store_get("focus")
        assert f["ds"] == ds, f
        got = run.ev("([ds, i]) => window.__pk.app.idOf(ds, i)", [ds, f["i"]])
        assert str(got) == sid
        if "view=co_ice" in fmt:
            # the legacy preset id selects that axes view in the manifold (F410M-F466N vs F356W-F444W)
            st = _wait(run, "manifold", "Array.isArray(s.frame) && Array.isArray(s.dims)")
            D = len(st["dims"])
            fr = st["frame"]
            assert fr[st["dims"].index("f410m-f466n")] != 0 and fr[D + st["dims"].index("f356w-f444w")] != 0
            assert sum(1 for v in fr if v) == 2, fr
        assert not _errs(run)
    finally:
        run.close()


def test_no_secure_fetches(runs):
    for page in PAGES:
        run = runs(page)
        assert not [u for u in run.external if "/secure/" in u], page
        assert not run.http_errors, (page, run.http_errors)
