"""Port acceptance for the gc_manifold family (port table section 14.2, WP30).

The GC manifold viewers are served by starformation.astro.ufl.edu and their binaries are read
in place: the staged catalog manifests point their column files at
``https://starformation.astro.ufl.edu/jwst-gc/<field>_manifold/data/...``. The harness blocks
every external request, so these tests open the page in a browser context whose ``route``
also fulfils those URLs from the deployed GC tree under
``/orange/adamginsburg/jwst/releases/site`` (with the CORS header the starformation server
sends). Everything else external stays blocked.

``PK_PORT_BUILD`` names a staged gc_manifold build built with ``--vendor-assets`` (the build
directory); without it the module builds the fixture (``portalkit build gc_manifold --limit 50
--vendor-assets``) into a temp stage root.

* T2 GPU path forced on the manifold and CMD panes (``tier:<pane>`` page state).
* gc-query grammar: ``cb=<dim index>`` -> ``c=<dim key>`` through the pane's dims; ``rv``;
  ``a/b/m`` and ``p=ccd&x1..y2`` -> a ``manifold.frame`` matrix over the legacy dim order.
* FEATURE_CAVEATS shown as caveats in the readme.
* The red-clump zoom: the readme link sets the CMD axes to the build-time box.
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
FIELD = "w51"
PORTAL = f"{FIELD}_manifold"
SITE_ROOT = Path("/orange/adamginsburg/jwst/releases/site")
SF_DATA = re.compile(r"^https://starformation\.astro\.ufl\.edu/jwst-gc/([a-z0-9_]+_manifold)/data/([A-Za-z0-9_./-]+)$")


def _plugin():
    return sys.modules["pk_conftest_browser"]


def _json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def build_dir(tmp_path_factory) -> Path:
    env = os.environ.get("PK_PORT_BUILD")
    if env:
        p = Path(env)
        if not (p / PORTAL / "portal.json").is_file():
            pytest.fail(f"PK_PORT_BUILD={env} holds no {PORTAL} portal")
        if re.search(r'"assets":\s*"https?://', (p / PORTAL / "index.html").read_text(encoding="utf-8")):
            pytest.skip("PK_PORT_BUILD loads its assets from the production URL; build it with --vendor-assets")
        return p
    if not (SITE_ROOT / PORTAL / "data" / "manifest.json").is_file():
        pytest.skip("GC release site not mounted")
    from portalkit import cli
    from portalkit.deploy import ExposureMap
    stage = tmp_path_factory.mktemp("stage")
    mp = pytest.MonkeyPatch()
    mp.setattr(cli, "_emap", lambda: ExposureMap(roots={}, url_prefixes={}))
    try:
        rc = cli.main(["build", "gc_manifold", "--limit", str(LIMIT), "--stage-root", str(stage),
                       "--vendor-assets"])
    finally:
        mp.undo()
    assert rc == 0
    found = sorted(stage.glob(f"gc_manifold/*/{PORTAL}/portal.json"))
    assert found, "build staged no portal"
    return found[-1].parent.parent


@pytest.fixture(scope="module")
def server(build_dir):
    from portalkit.deploy.devserver import DevServer
    srv = DevServer(build_dir).start()
    yield srv
    srv.stop()


class _GcContext:
    """A browser context whose catch-all ``route`` (registered by ``open_run``) is followed by
    a handler that serves the starformation GC data URLs from the deployed tree. Playwright
    tries the most recently registered route first, so the GC handler sees those URLs before
    the catch-all aborts them; other URLs fall back to the catch-all."""

    def __init__(self, ctx):
        self._ctx = ctx
        self.served: list[str] = []
        self.missing: list[str] = []

    def __getattr__(self, name):
        return getattr(self._ctx, name)

    def _gc(self, r):
        m = SF_DATA.match(r.request.url)
        root = (SITE_ROOT / m.group(1) / "data").resolve() if m else None
        path = (root / m.group(2)).resolve() if m else None
        if path is None or root not in path.parents or not path.is_file():
            if m:
                self.missing.append(r.request.url)
            return r.fallback()
        self.served.append(r.request.url)
        return r.fulfill(status=200, body=path.read_bytes(),
                         headers={"Content-Type": "application/octet-stream",
                                  "Access-Control-Allow-Origin": "*"})

    def route(self, pattern, handler, **kw):
        self._ctx.route(pattern, handler, **kw)
        self._ctx.route(SF_DATA, self._gc)


@pytest.fixture(scope="module")
def runs(browser, server):
    opened = []

    def open_(**kw):
        gc = _GcContext(browser.new_context(viewport={"width": 1400, "height": 900}))
        r = _plugin().open_run(browser, server, f"{PORTAL}/index.html", context=gc, **kw)
        r.gc = gc
        opened.append(r)
        return r
    yield open_
    for r in opened:
        r.close()


def _no_errors(run):
    errs = run.errors(allow=(_plugin().ABORTED_LOAD,))
    assert not errs, errs


def _cat_manifest(build_dir) -> dict:
    return _json(build_dir / PORTAL / "data" / f"cat_{FIELD}" / "manifest.json")


def _wait_manifold(run):
    run.wait_panes_settled()
    run.page.wait_for_function(
        "() => { const h = window.__pk.app.layout.pane('manifold'); const s = h && h.stats();"
        " return !!(s && s.path); }", timeout=90000)
    return run.pane_stats("manifold")


def test_page_reads_gc_binaries_in_place_at_t2(runs, build_dir):
    man = _cat_manifest(build_dir)
    gc = _json(SITE_ROOT / PORTAL / "data" / "manifest.json")
    run = runs()
    st = _wait_manifold(run)
    assert run.store_get("tier:manifold") == "T2"
    assert run.store_get("tier:cmd") == "T2"
    assert st["n"] == gc["n"] == man["n"]
    assert st["dims"][:len(gc["dims"])] == [d["key"] for d in gc["dims"]]
    # the GPU path when the browser offers float render targets, else the documented fallback
    assert st["path"] in ("gc", "xy", "binned"), st
    if st["path"] == "gc":
        assert st["tier"] == "T2"
        assert run.ev("() => document.querySelector(\"section.pk-pane[data-pane-id='manifold'] [data-tier]\")"
                      "?.dataset.tier") == "T2"
    # column data came from the starformation URLs, nothing from the staged tree but the tile
    fetched = {u.rsplit("/data/", 1)[1] for u in run.gc.served}
    # every dim of the projection is fetched (positions load lazily, with the sky pane)
    assert {d["file"] for d in gc["dims"]} <= fetched, sorted(fetched)
    assert not run.gc.missing, run.gc.missing
    ax = run.store_get("axes:cmd")
    assert ax["x"].count("-") == 1 and ax["xr"][0] < ax["xr"][1]
    _no_errors(run)


def test_gc_query_colour_by_and_rv(runs, build_dir):
    from portalkit.legacy.extract import script_json
    page = script_json(build_dir / PORTAL / "index.html", "pk-page")
    dims = page["layout"]["row"][0]["options"]["dims"]
    run = runs(query="cb=1&rv=5.5&g=2")
    run.page.wait_for_function("() => /(^#|&)c=/.test(location.hash)", timeout=30000)
    h = dict(kv.split("=", 1) for kv in run.ev("() => location.hash").lstrip("#").split("&") if "=" in kv)
    assert h["c"].split("~")[0] == dims[1], (h, dims)
    assert run.store_get("rv") == pytest.approx(5.5)
    search = run.ev("() => location.search")
    assert "cb=" not in search and "rv=" not in search and "g=" not in search and "pktest=1" in search
    _wait_manifold(run)
    _no_errors(run)


def test_readme_caveats_and_red_clump_link(runs, build_dir):
    man = _cat_manifest(build_dir)
    run = runs()
    _wait_manifold(run)
    text = run.ev("() => document.querySelector(\"section.pk-pane[data-pane-type='readme']\")?.innerText || ''")
    # FEATURE_CAVEATS of the legacy viewer are listed as caveats
    assert "Caveats" in text
    assert "Red clump" in text and "Chiar & Tielens" in text
    assert any(c[:60] in text for c in man["caveats"])
    href = run.ev("() => { const a = document.querySelector(\"section.pk-pane[data-pane-type='readme'] "
                  "a[href^='#cmd.']\"); return a ? a.getAttribute('href') : null; }")
    assert href, "red-clump link missing from the readme"
    q = dict(kv.split("=", 1) for kv in href[1:].split("&"))
    xr = [float(v) for v in q["cmd.xr"].split(",")]
    yr = [float(v) for v in q["cmd.yr"].split(",")]
    before = run.store_get("axes:cmd")
    run.page.click(f"section.pk-pane[data-pane-type='readme'] a[href='{href}']")
    run.page.wait_for_function(
        "(xr) => { const a = window.__pk.store.get('axes:cmd'); return !!(a && a.xr && "
        "Math.abs(a.xr[0] - xr[0]) < 1e-3 && Math.abs(a.xr[1] - xr[1]) < 1e-3); }", arg=xr, timeout=20000)
    ax = run.store_get("axes:cmd")
    assert (ax["x"], ax["y"]) == (q["cmd.x"], q["cmd.y"]) == (before["x"], before["y"])
    assert ax["xr"] == pytest.approx(xr, abs=1e-3)
    assert sorted(ax["yr"]) == pytest.approx(sorted(yr), abs=1e-3)
    # the zoom box is inside the default fit range
    assert before["xr"][0] <= xr[0] < xr[1] <= before["xr"][1]
    _no_errors(run)


def test_gc_query_cmd_and_ccd_frames(runs, build_dir):
    """gc-query a/b/m (CMD) and p=ccd&x1..y2 (CCD) index the manifold dims in legacy order and
    become a `manifold.frame` matrix (colour short - long, magnitude bright up)."""
    import base64
    import struct
    from portalkit.legacy.extract import script_json
    page = script_json(build_dir / PORTAL / "index.html", "pk-page")
    dims = page["layout"]["row"][0]["options"]["dims"]
    D = len(dims)
    assert D >= 4

    def frame_of(run):
        run.page.wait_for_function("() => /(^#|&)manifold\\.frame=b64:/.test(location.hash)", timeout=30000)
        h = dict(kv.split("=", 1) for kv in run.ev("() => location.hash").lstrip("#").split("&") if "=" in kv)
        raw = h["manifold.frame"][4:]
        b = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        return list(struct.unpack(f"<{2 * D}f", b)), run.ev("() => location.search")

    run = runs(query="a=1&b=2&m=1")
    F, search = frame_of(run)
    want = [0.0] * (2 * D)
    want[1], want[2], want[D + 1] = 1.0, -1.0, -1.0
    assert F == pytest.approx(want), (F, dims)
    assert not re.search(r"[?&](a|b|m)=", search), search
    _wait_manifold(run)
    _no_errors(run)

    run = runs(query="p=ccd&x1=0&x2=3&y1=1&y2=2")
    F, search = frame_of(run)
    want = [0.0] * (2 * D)
    want[0], want[3], want[D + 1], want[D + 2] = 1.0, -1.0, 1.0, -1.0
    assert F == pytest.approx(want), (F, dims)
    assert "x1=" not in search and "p=ccd" not in search
    _wait_manifold(run)
    _no_errors(run)
