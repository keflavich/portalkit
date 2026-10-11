"""Port acceptance for the sickle_brick family (port table section 14.8, WP26).

Drives a staged sickle_brick build (two portals, ``sickle`` and ``brick2221``) through one
DevServer with ``?pktest=1`` and the ``__pk`` hooks. ``PK_PORT_BUILD`` names a staged build made
with ``--vendor-assets`` (the build directory); without it the module builds a fixture
(``portalkit build sickle_brick --limit 50 --vendor-assets``) into a temp stage root. Each
special_handling entry of the port table becomes one test:

* landings: counts from the manifests, the cross-portal card is a public https link.
* sickle_miri_sed_map: SESHAT probability bars in the detail pane; the F2550W FITS overlay URL
  is the public https URL from the exposure map (no relative path escape) and loads on demand;
  catalogue downloads as products; legacy ``#<rank>`` focuses that source.
* sickle_miri_sed_manifold: default frame ``cmd``, F444W in the dims, legacy
  ``#view=<preset>&star=<name>`` opens that frame and source.
* f1500w_explorer: templates extended to 26 um, normalised on NIRCam bands only, R_V 5.5.
* f1500w_gallery: the top-100 stub preset opens the top100 facet on the top-100 role.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

FAMILY = "sickle_brick"
LIMIT = 50
PORTALS = ("sickle", "brick2221")
TOP_STUB = "role=top100&fac=eyJ0b3AxMDAiOlsieWVzIl19"


def _plugin():
    return sys.modules["pk_conftest_browser"]


def _json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def build_dir(tmp_path_factory) -> Path:
    env = os.environ.get("PK_PORT_BUILD")
    if env:
        p = Path(env)
        if not all((p / q / "portal.json").is_file() for q in PORTALS):
            pytest.fail(f"PK_PORT_BUILD={env} holds no sickle_brick build")
        if re.search(r'"assets":\s*"https?://', (p / "sickle" / "index.html").read_text(encoding="utf-8")):
            pytest.skip("PK_PORT_BUILD loads its assets from the production URL; build it with --vendor-assets")
        return p
    if not Path("/orange/adamginsburg/jwst/sickle/sed_portal/sickle_miri_sed_map.html").is_file():
        pytest.skip("legacy Sickle portal tree not mounted")
    from portalkit import cli
    from portalkit.deploy import ExposureMap
    stage = tmp_path_factory.mktemp("stage")
    mp = pytest.MonkeyPatch()
    mp.setattr(cli, "_emap", lambda: ExposureMap(roots={}, url_prefixes={}))
    try:
        rc = cli.main(["build", FAMILY, "--limit", str(LIMIT), "--stage-root", str(stage), "--vendor-assets"])
    finally:
        mp.undo()
    assert rc == 0
    found = sorted(stage.glob(f"{FAMILY}/*/sickle/portal.json"))
    assert found, "build staged no portal"
    return found[-1].parent.parent


@pytest.fixture(scope="module")
def server(build_dir):
    from portalkit.deploy.devserver import DevServer
    srv = DevServer(build_dir).start()
    yield srv
    srv.stop()


@pytest.fixture(scope="module")
def runs(browser, server):
    opened = []

    def open_(path: str, **kw):
        r = _plugin().open_run(browser, server, path, **kw)
        opened.append(r)
        return r
    yield open_
    for r in opened:
        r.close()


def _no_errors(run):
    errs = run.errors(allow=(_plugin().ABORTED_LOAD,))
    assert not errs, errs


def _panes(run) -> dict:
    return run.ev("""() => Object.fromEntries(Array.from(document.querySelectorAll('section.pk-pane'))
        .map((s) => [s.dataset.paneType, s.dataset.paneId]))""")


def _wait(run, pane: str, cond: str, timeout=60000):
    run.page.wait_for_function(
        f"() => {{ const h = window.__pk.app.layout.pane('{pane}'); const s = h && h.stats(); "
        f"return !!(s && ({cond})); }}",
        timeout=timeout)
    return run.pane_stats(pane)


def _focus(run, ds: str, i: int):
    run.ev(f"() => window.__pk.store.set({{focus: {{ds: '{ds}', i: {i}, id: null}}}}, {{source: 'test'}})")


# -- landings ------------------------------------------------------------------------------
@pytest.mark.parametrize("portal", PORTALS)
def test_landing_counts_and_cross_link(runs, build_dir, portal):
    pdir = build_dir / portal
    meta = _json(pdir / "portal.json")
    assert meta["visibility"] == "public"
    run = runs(f"{portal}/index.html")
    run.wait_panes_settled()
    run.page.wait_for_function(
        "() => Array.from(document.querySelectorAll('.pk-card-count')).some((c) => c.textContent.trim())",
        timeout=30000)
    cards = run.ev("""() => Array.from(document.querySelectorAll('.pk-card-link')).map((a) => ({
        href: a.getAttribute('href'), page: a.dataset.page || null,
        count: (a.querySelector('.pk-card-count') || {}).textContent || ''}))""")
    for s in meta["search"]:
        n = _json(pdir / s["manifest"])["n"]
        card = next(c for c in cards if c["page"] == s["page"])
        assert re.sub(r"[,\s ]", "", card["count"]).startswith(str(n)), card
    ext = [c for c in cards if c["href"] and c["href"].startswith("https://")]
    assert [c["href"] for c in ext] == [r["url"] for r in meta["related"]], cards
    assert ext[0]["href"].startswith("https://data.rc.ufl.edu/pub/adamginsburg/jwst/")
    assert not any("/secure/" in u for u in run.external)
    _no_errors(run)


# -- sickle_miri_sed_map ---------------------------------------------------------------------
def test_map_f2550w_overlay_bars_and_products(runs, build_dir):
    pdir = build_dir / "sickle"
    hips = _json(pdir / "data" / "hips.json")
    (fits,) = hips["fits"]
    assert fits["url"].startswith("https://data.rc.ufl.edu/pub/") and fits["url"].endswith("brick_f2550w_i2d.fits")
    run = runs("sickle/sickle_miri_sed_map.html")
    run.wait_panes_settled()
    panes = _panes(run)
    sky = panes["sky"]
    st = _wait(run, sky, "s.layers && s.aladin && s.aladin !== 'loading'")
    assert st["layers"]["fits"] == {} or all(not v["on"] for v in st["layers"]["fits"].values())
    assert not any(c[0] == "createImageFITS" for c in run.aladin_calls())      # loads on first use
    run.ev("() => { const s = window.__pk.store.get('sky') || {}; "
           "window.__pk.store.set({sky: {...s, fits: ['f2550w']}}, {source: 'test'}); }")
    st = _wait(run, sky, "s.layers && s.layers.fits && s.layers.fits.f2550w && s.layers.fits.f2550w.status === 'ready'")
    assert st["layers"]["fits"]["f2550w"]["on"] is True
    urls = [c[1][0] for c in run.aladin_calls() if c[0] == "createImageFITS"]
    assert urls == [fits["url"]]
    # SESHAT probability bars for the focused source
    _focus(run, "sickle", 0)
    det = panes["detail"]
    _wait(run, det, "s.id != null")
    bsel = f"section.pk-pane[data-pane-id='{det}'] .pk-detail-bars"
    run.page.wait_for_function(f"() => !!document.querySelector(\"{bsel}\")", timeout=30000)
    bars = run.ev(f"""() => Array.from(document.querySelectorAll(
        "section.pk-pane[data-pane-id='{det}'] .pk-detail-bars")).map((b) => b.textContent)""")
    assert bars and all(c in bars[0] for c in ("YSO", "FS")), bars
    sel = f"section.pk-pane[data-pane-id='{det}'] a.pk-product-link"
    run.page.wait_for_function(f"() => document.querySelectorAll(\"{sel}\").length > 0", timeout=30000)
    prods = run.ev(f"""() => Array.from(document.querySelectorAll("{sel}")).map((a) => a.getAttribute('href'))""")
    assert any(h == fits["url"] for h in prods), prods
    assert any(h.endswith(".png") and h.startswith("https://") for h in prods), prods
    _no_errors(run)


def test_map_legacy_rank_hash(runs, build_dir):
    assert _json(build_dir / "sickle" / "data" / "sickle" / "manifest.json")["n"] >= 3
    run = runs("sickle/sickle_miri_sed_map.html", hash="3")
    run.page.wait_for_function(
        "() => { const f = window.__pk.store.get('focus'); return !!f && Number.isInteger(f.i) && f.i >= 0; }",
        timeout=60000)
    f = run.store_get("focus")
    assert f["ds"] == "sickle" and f["i"] == 2, f       # rows are in rank order; alias = rank
    _no_errors(run)


def test_map_sed_template_nircam_scale_to_26um(runs, build_dir):
    pdir = build_dir / "sickle"
    grid = next(pdir.glob("data/res/tpl26/templates/grid_medium.*.json"))
    g = _json(grid)
    assert g["values"][-1] > 25.9
    run = runs("sickle/sickle_miri_sed_map.html")
    run.wait_panes_settled()
    tpl = run.store_get("template")
    assert tpl["key"] and tpl["bands"] and all(re.fullmatch(r"F\d{3}[WMN]", b) and int(b[1:4]) < 500
                                               for b in tpl["bands"]), tpl
    sed = _panes(run)["sed"]
    _focus(run, "sickle", 0)
    st = _wait(run, sed, "s.i === 0 && s.template && s.template.n > 0")
    assert st["template"]["key"] == tpl["key"] and st["template"]["grid"] == "medium"
    assert st["template"]["n"] == g["n"]
    assert any(p["wl"] > 20 for p in st["points"]), st["points"]          # MIRI F2550W / F1500W
    assert st["xr"][1] > 20
    # NIRCam-only normalisation: the pane's scale equals scaleTo over the NIRCam points alone,
    # and for at least one source with a MIRI excess it differs from the all-band scale
    differs = 0
    for i in range(5):
        _focus(run, "sickle", i)
        st = _wait(run, sed, f"s.i === {i} && s.template && s.template.n > 0 && s.points.length > 0")
        ref = _scales(run, st)
        assert ref["nir"] > 0 and st["template"]["scale"] == pytest.approx(ref["nir"], rel=1e-9), (i, ref, st["template"])
        differs += abs(ref["all"] / ref["nir"] - 1) > 0.01
    assert differs, "no source among the first 5 has a MIRI-driven all-band scale"
    # dust/ice feature markers
    run.ev("() => window.__pk.store.set({features: ['ice']}, {source: 'test'})")
    st = _wait(run, sed, "s.features && s.features.shown > 0")
    assert st["features"]["shown"] > 0
    _no_errors(run)


def _scales(run, st) -> dict:
    """scaleTo of the page's own template module over the NIRCam (< 5.5 um) points and all points."""
    t = st["template"]
    return run.ev(r"""async ([pts, key, grid, av, rv]) => {
        const R = performance.getEntriesByType('resource').map((e) => e.name);
        const mu = R.find((n) => /\/js\/data\/templates\.js(\?|$)/.test(n));
        const iu = R.find((n) => /\/templates\/index\.[0-9a-f]+\.json(\?|$)/.test(n));
        const m = await import(mu);
        const model = await (await m.TemplateSet.open(iu)).reddened(key, grid, av, rv);
        return {nir: m.scaleTo(model, pts.filter((p) => p.wl < 5.5)), all: m.scaleTo(model, pts)};
    }""", [st["points"], t["key"], t["grid"], t["av"], t["rv"]])


# -- sickle_miri_sed_manifold ----------------------------------------------------------------
def test_manifold_default_frame_and_legacy_view(runs, build_dir):
    run = runs("sickle/sickle_miri_sed_manifold.html")
    run.wait_panes_settled()
    man = _panes(run)["manifold"]
    st = _wait(run, man, "s.projected")
    assert "m_f444w" in st["dims"] and not any(d.startswith(("o", "c_")) for d in st["dims"])
    assert st["preset"] == "cmd"
    # legacy orientation: bright up (negative F1500W weight), l increasing to the left
    mf = _json(build_dir / "sickle" / "data" / "sickle" / "manifest.json")["manifold"]
    D = len(mf["dims"])
    assert mf["frames"]["cmd"][D + mf["dims"].index("m_f1500w")] == -1.0
    assert mf["frames"]["sky"][mf["dims"].index("gl")] == -1.0
    run2 = runs("sickle/sickle_miri_sed_manifold.html", hash="view=mirccd")
    run2.wait_panes_settled()
    man2 = _panes(run2)["manifold"]
    st2 = _wait(run2, man2, "s.projected")
    assert st2["preset"] == "mirccd", st2["preset"]
    _no_errors(run)
    _no_errors(run2)


# -- f1500w_explorer -------------------------------------------------------------------------
def test_explorer_k55_extended_templates(runs, build_dir):
    run = runs("brick2221/f1500w_explorer.html")
    run.wait_panes_settled()
    sed = _panes(run)["sed"]
    _focus(run, "brick", 0)
    st = _wait(run, sed, "s.i === 0 && s.template && s.template.n > 0")
    assert st["template"]["rv"] == pytest.approx(3.1)
    choices = run.ev("() => Array.from(document.querySelectorAll('.pk-ctl-rv option')).map((o) => Number(o.value))")
    assert 5.5 in choices, choices
    run.ev("() => window.__pk.store.set({rv: 5.5, av: 10}, {source: 'test'})")
    st = _wait(run, sed, "s.template && s.template.rv === 5.5 && s.template.av === 10")
    assert st["template"]["n"] > 9000
    assert st["template"]["scale"] > 0
    _no_errors(run)


# -- f1500w_gallery --------------------------------------------------------------------------
def test_gallery_top100_preset(runs, build_dir):
    items = _json(next((build_dir / "brick2221" / "data" / "br_img").glob("images*.json")))["items"]
    top = [it for it in items if (it.get("tags") or {}).get("top100") in ("yes", ["yes"])]
    assert top
    run = runs("brick2221/f1500w_gallery.html", hash=TOP_STUB)
    run.wait_panes_settled()
    gal = _panes(run)["gallery"]
    st = _wait(run, gal, "s.items > 0")
    assert st["items"] == len(items)
    assert run.store_get("role") == "top100"
    assert run.store_get("facets") == {"top100": ["yes"]}
    st = _wait(run, gal, f"s.shown === {len(top)}")
    srcs = run.ev(f"""() => Array.from(document.querySelectorAll(
        "section.pk-pane[data-pane-id='{gal}'] img")).map((i) => i.getAttribute('src') || i.dataset.src || '')""")
    assert any("sed_figures_top100_f1500w/" in s for s in srcs), srcs[:5]
    _no_errors(run)
