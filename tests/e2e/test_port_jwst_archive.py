"""Port acceptance for the jwst_archive family (port table section 14.3, WP28 wave 4).

Drives a staged jwst_archive build (three portals: ``archive``, ``w51``, ``gallery``) through one
DevServer with ``?pktest=1`` and the ``__pk`` hooks. ``PK_PORT_BUILD`` names a staged build made
with ``--vendor-assets`` (the build directory); without it the module builds a fixture
(``portalkit build jwst_archive --limit 50 --vendor-assets``) into a temp stage root. External
requests (HiPS tiles, the public PNGs) are aborted by the harness and recorded.

* landings: page cards with manifest counts; cross-portal cards are https links, the secure
  ones login-badged.
* archive colors: CCD plus two marginal CMDs over the archive density, list, sky, detail and
  the spectrum (one segment per stacked grating pair) on focus.
* archive manifold: every spectrum, >= 10 dims, the legacy ``#view=..&star=<name>`` link
  focuses the spectrum the star name now maps to (the stale-index fix).
* w51 colors: the W51 photometry density under the archive spectra.
* gallery: one faceted gallery; each legacy gallery stub preset sets its facets and role.
* checklist: annotate mode; a code exported as CSV carries the checklist key columns and
  ``portalkit archive-checklist-diff`` turns it into a diff (the checklist is only read).
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

FAMILY = "jwst_archive"
LIMIT = 50
PORTALS = ("archive", "w51", "gallery")
SECURE = "https://data.rc.ufl.edu/secure/"
CHECKLIST = Path("/orange/adamginsburg/jwst/spectra/mastDownload/JWST/jwst_archive_spectra_checklist.csv")
KEYS = ["Target", "Program", "Object", "SlitID", "Observation", "Visit", "VisitGroup", "Filename"]


def _plugin():
    return sys.modules["pk_conftest_browser"]


def _json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def _site_raw():
    from portalkit.context import load_site
    return load_site(FAMILY)


@pytest.fixture(scope="module")
def build_dir(tmp_path_factory) -> Path:
    env = os.environ.get("PK_PORT_BUILD")
    if env:
        p = Path(env)
        if not all((p / q / "portal.json").is_file() for q in PORTALS):
            pytest.fail(f"PK_PORT_BUILD={env} holds no jwst_archive build")
        if re.search(r'"assets":\s*"https?://', (p / "archive" / "index.html").read_text(encoding="utf-8")):
            pytest.skip("PK_PORT_BUILD loads its assets from the production URL; build it with --vendor-assets")
        return p
    if not Path("/orange/adamginsburg/jwst/spectra/portal/index.html").is_file():
        pytest.skip("JWST archive portal tree not mounted")
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
    found = sorted(stage.glob(f"{FAMILY}/*/archive/portal.json"))
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
    """paneType -> [paneId, ...] in document order."""
    return run.ev("""() => { const o = {}; for (const s of document.querySelectorAll('section.pk-pane')) {
        (o[s.dataset.paneType] = o[s.dataset.paneType] || []).push(s.dataset.paneId); } return o; }""")


def _wait(run, pane: str, cond: str, timeout=60000):
    run.page.wait_for_function(
        f"() => {{ const h = window.__pk.app.layout.pane('{pane}'); const s = h && h.stats(); "
        f"return !!(s && ({cond})); }}",
        timeout=timeout)
    return run.pane_stats(pane)


def _focus(run, i: int):
    run.ev(f"() => window.__pk.store.set({{focus: {{ds: 'src', i: {i}, id: null}}}}, {{source: 'test'}})")


def _ids(run, n=3) -> list[str]:
    got = run.ev(f"""async () => {{ const c = await window.__pk.hub.catalog('src');
        const r = await c.rows(0, {n}); return r.map((x) => x.id); }}""")
    return [str(x) for x in got]


# -- landings --------------------------------------------------------------------------------
@pytest.mark.parametrize("portal,vis,npages", [("archive", "secure", 2), ("w51", "secure", 1),
                                               ("gallery", "public", 2)])
def test_landing_cards(runs, build_dir, portal, vis, npages):
    assert _json(build_dir / portal / "portal.json")["visibility"] == vis
    run = runs(f"{portal}/index.html")
    run.wait_panes_settled()
    run.page.wait_for_function(
        "() => Array.from(document.querySelectorAll('.pk-card-count')).some((c) => c.textContent.trim())",
        timeout=30000)
    cards = run.ev("""() => Array.from(document.querySelectorAll('.pk-card-link')).map((a) => ({
        href: a.getAttribute('href'), page: a.dataset.page || null,
        badge: !!a.querySelector('.pk-login-badge')}))""")
    assert len([c for c in cards if c["page"]]) == npages, cards
    ext = [c for c in cards if not c["page"]]
    assert ext and all(c["href"].startswith("https://data.rc.ufl.edu/") for c in ext), cards
    for c in ext:
        assert c["badge"] == c["href"].startswith(SECURE), c
    _no_errors(run)


# -- archive colour portal -------------------------------------------------------------------
def test_archive_colors(runs, build_dir):
    man = _json(build_dir / "archive" / "data" / "src" / "manifest.json")
    arch = _json(build_dir / "archive" / "data" / "arch" / "manifest.json")
    assert man["n"] == LIMIT and arch["n"] == LIMIT and "rows" not in arch
    run = runs("archive/colors.html")
    run.wait_panes_settled()
    p = _panes(run)
    for t in ("scatter", "list", "detail", "spectrum1d"):
        assert t in p, (t, p)
    assert {"ccd", "cmd_x", "cmd_y"} <= set(p["scatter"])
    for pane in ("ccd", "cmd_x", "cmd_y"):
        st = _wait(run, pane, "s.n > 0")
        assert st["n"] == LIMIT
    _focus(run, 0)
    s1 = _wait(run, p["spectrum1d"][0], "s.n > 0")
    assert len(s1["segments"]) == 1
    assert not [u for u in run.external if "/pub/adamginsburg/" in u]
    _no_errors(run)


# -- manifold ----------------------------------------------------------------------------------
def test_manifold_and_legacy_star_link(runs, build_dir):
    man = _json(build_dir / "archive" / "data" / "src" / "manifest.json")
    run = runs("archive/manifold.html")
    run.wait_panes_settled()
    p = _panes(run)
    st = _wait(run, p["manifold"][0], "s.n > 0 && s.D > 0")
    assert st["n"] == man["n"] and st["D"] >= 10
    # a legacy manifold link: #view=<preset>&star=<legacy star name>; the build's alias map
    # sends the star name to the spectrum with the same target, programme, grating and position
    src = build_dir / "archive" / "data" / "src"
    amap = _json(src / _json(src / "manifest.json")["ids"]["alias_file"])
    stars = {k: v for k, v in amap.items() if not k.endswith("_x1d.fits")}
    if not stars:
        pytest.skip("no fixture spectrum has a legacy manifold star name")
    star, row = next(iter(stars.items()))
    want = _ids(run, LIMIT)[row]
    run2 = runs("archive/manifold.html", hash=f"view=pca&star={star}")
    run2.wait_panes_settled()
    run2.page.wait_for_function("() => { const f = window.__pk.store.get('focus'); return !!(f && f.id); }",
                                timeout=30000)
    got = run2.ev("""async () => { const pk = window.__pk; const f = pk.store.get('focus');
        if (Number.isInteger(f.i) && f.i >= 0) return f.i;
        const [i] = await pk.app.resolveIds('src', [f.id]); return i; }""")
    assert got == row and _ids(run2, LIMIT)[got] == want
    _no_errors(run2)


# -- W51 ---------------------------------------------------------------------------------------
# w51 stages its own copy of the spectrum shards rather than a foreign record set onto the
# archive portal: the two portals' secure areas are different .htpasswd files with only one
# account in common, so a w51-only viewer would get a 401 fetching the archive portal's
# shards (see adapters/jwst_archive.py module docstring and
# tests/adapters/test_jwst_archive.py's test_w51_keeps_its_own_spectrum_copy).
def test_w51_colors_density(runs, build_dir):
    bg = _json(build_dir / "w51" / "data" / "bg" / "manifest.json")
    assert bg["n"] == 20 * LIMIT and "rows" not in bg
    assert _json(build_dir / "w51" / "portal.json")["visibility"] == "secure"
    run = runs("w51/colors.html")
    run.wait_panes_settled()
    st = _wait(run, "ccd", "s.n > 0")
    assert st["n"] == LIMIT
    _no_errors(run)


def test_w51_spectra_local_copy(runs, build_dir):
    """w51's spectrum pane reads its own staged data/spec/ shards (not a foreign record set)."""
    pj = _json(build_dir / "w51" / "portal.json")
    assert pj["visibility"] == "secure"
    assert (build_dir / "w51" / "data" / "spec" / "index.json").is_file()
    run = runs("w51/colors.html")
    assert "spec_arch" not in _json_script(run)["datasets"]
    run.wait_panes_settled()
    p = _panes(run)
    assert "spectrum1d" in p, p
    run.ev("(p) => window.__pk.app.layout.reveal(p)", p["spectrum1d"][0])      # may start collapsed
    run.page.wait_for_function("(p) => !!window.__pk.app.layout.pane(p)", arg=p["spectrum1d"][0], timeout=30000)
    _focus(run, 0)
    s1 = _wait(run, p["spectrum1d"][0], "s.n > 0")
    assert s1["n"] > 0
    _no_errors(run)


def _json_script(run) -> dict:
    return run.ev("() => JSON.parse(document.getElementById('pk-page').textContent)")


# -- gallery -----------------------------------------------------------------------------------
def _images(build_dir):
    return _json(next((build_dir / "gallery" / "data" / "pngs").glob("images*.json")))


def test_gallery_public_and_links_secure(runs, build_dir):
    doc = _images(build_dir)
    urls = [r["url"] for it in doc["items"] for r in it["roles"].values()]
    assert urls and all(u.startswith("https://data.rc.ufl.edu/pub/adamginsburg/jwst/spectra/") for u in urls)
    run = runs("gallery/gallery.html")
    run.wait_panes_settled()
    p = _panes(run)
    st = _wait(run, p["gallery"][0], "s.items > 0")
    assert st["items"] == len(doc["items"])
    links = run.ev("() => (window.__pk.app.page && window.__pk.app.page.links) || null")
    if links:
        assert links["colors"].startswith(SECURE + "adamginsburg/jwst/spectra/portal_unified/colors.html")
    _no_errors(run)


def _stubs() -> dict:
    raw = _site_raw().portal("gallery").raw["page"]
    return {it["file"]: it["preset"] for pg in raw for it in pg.get("legacy", []) if isinstance(it, dict)}


@pytest.mark.parametrize("legacy", ["gallery_prism_2221filters.html", "gallery_1611_broad.html",
                                    "gallery_3222.html", "gallery_1611_twogratings.html"])
def test_gallery_stub_presets(runs, build_dir, legacy):
    """Each legacy gallery page redirects to the unified gallery with its facets and role."""
    from portalkit.legacy.hashmap import b64url_decode
    hash_ = _stubs()[legacy]
    q = dict(kv.split("=", 1) for kv in hash_.split("&"))
    facets = json.loads(b64url_decode(q["fac"]))
    run = runs("gallery/gallery.html", hash=hash_)
    run.wait_panes_settled()
    gal = _panes(run)["gallery"][0]
    _wait(run, gal, "s.items > 0")
    assert run.store_get("facets") == facets
    if "role" in q:
        assert run.store_get("role") == q["role"]
    doc = _images(build_dir)
    passing = [it for it in doc["items"]
               if all(str((it.get("tags") or {}).get(k)) in v for k, v in facets.items())]
    st = _wait(run, gal, f"s.shown === {len(passing)}")
    assert st["shown"] == len(passing)
    _no_errors(run)


# -- checklist ---------------------------------------------------------------------------------
def test_checklist_annotate_export_and_diff(runs, build_dir, tmp_path, capsys):
    before = CHECKLIST.stat().st_mtime_ns if CHECKLIST.is_file() else None
    run = runs("gallery/checklist.html")
    run.wait_panes_settled()
    gal = _panes(run)["gallery"][0]
    _wait(run, gal, "s.cards > 0")
    # only PNGs of spectra in the fluxes table carry checklist keys (and a checklist tag)
    codes = sorted({(it.get("tags") or {}).get("checklist") for it in _images(build_dir)["items"]} - {None})
    assert codes
    run.ev("(f) => window.__pk.store.set({facets: f}, {source: 'test'})", {"checklist": codes})
    run.page.click(".pk-gal-annot-toggle")
    run.page.keyboard.press("Home")
    run.page.keyboard.press("y")
    run.page.wait_for_function("() => Array.from(document.querySelectorAll('.pk-annot-chip'))"
                               ".some((c) => c.textContent.trim() === 'y 1')", timeout=10000)
    with run.page.expect_download(timeout=30000) as dl:
        run.page.get_by_role("button", name="Export CSV").click()
    path = tmp_path / "export.csv"
    dl.value.save_as(str(path))
    rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
    assert rows[0] == ["id", *KEYS, "ISOKN"]
    assert len(rows) == 2 and rows[1][-1] == "y"
    rec = dict(zip(rows[0], rows[1]))
    assert rec["Filename"].endswith("_x1d.fits") and not rec["Filename"].startswith("/")
    assert rec["Program"] and rec["Target"]
    _no_errors(run)
    if not CHECKLIST.is_file():
        return
    # the downloaded export goes to the CLI as is (no path substitution)
    with open(CHECKLIST, encoding="utf-8", newline="") as fh:
        match = [r for r in csv.DictReader(fh)
                 if os.path.basename(r["Filename"].strip()) == os.path.basename(rec["Filename"])]
    assert match, f"annotated spectrum {rec['Filename']} has no checklist row"
    from portalkit import cli
    capsys.readouterr()
    assert cli.main(["archive-checklist-diff", str(path)]) == 0
    out = capsys.readouterr()
    assert CHECKLIST.stat().st_mtime_ns == before
    # Filename matches by path suffix (basename + parent dir; checklist_diff, WPE6)
    assert "no checklist row matches" not in out.err, out.err
    if any(r["ISOKN"].strip() != "y" for r in match):
        assert out.out.startswith("---") and "0 checklist row(s) change" not in out.err, out.err
