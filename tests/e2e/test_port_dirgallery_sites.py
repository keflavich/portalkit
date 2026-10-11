"""Port acceptance for the dirgallery_sites family (port table section 14.8, WP23 wave 2).

Drives a staged dirgallery_sites build through the DevServer with ``?pktest=1`` and the ``__pk``
hooks. ``PK_PORT_BUILD`` names a staged build made with ``--vendor-assets`` (the build directory
holding the nine portal directories); without it the module builds a fixture build
(``portalkit build dirgallery_sites --limit 50 --vendor-assets``) into a temp stage root. Image
URLs point at data.rc.ufl.edu, so the harness aborts them; layout uses the stored w/h.

* brick_2221/ccds_cmds: each of the five legacy pages' stub hash (``#fac=...`` from the site
  registry) shows exactly the items whose tags it selects; no hash shows every item.
* w51_public/w51_rgb: the role switcher replaces the legacy suffix selector (``#role=alma``);
  the pngs_2100 no-ALMA preset combines the directory and legacy-page facets.
* robitaille_modelgrids/png_b: the gallery joins the model catalogue (scatter pane present).
* landing pages: one card per gallery plus the other gallery sets.
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
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
LIMIT = 50
FAM = "dirgallery_sites"
LEGACY = Path("/orange/adamginsburg/web/public/jwst/brick_2221/ccds_cmds")


def _plugin():
    return sys.modules["pk_conftest_browser"]


@pytest.fixture(scope="module")
def build_dir(tmp_path_factory) -> Path:
    env = os.environ.get("PK_PORT_BUILD")
    if env:
        p = Path(env)
        if (p / FAM).is_dir():
            found = sorted(p.glob(f"{FAM}/*/brick_2221/portal.json"))
            p = found[-1].parent.parent if found else p
        if not (p / "brick_2221" / "portal.json").is_file():
            pytest.fail(f"PK_PORT_BUILD={env} holds no {FAM} build")
        if re.search(r'"assets":\s*"https?://', (p / "brick_2221" / "index.html").read_text(encoding="utf-8")):
            pytest.skip("PK_PORT_BUILD loads its assets from the production URL; build it with --vendor-assets")
        return p
    if not LEGACY.is_dir():
        pytest.skip("legacy gallery directories not mounted")
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
    found = sorted(stage.glob(f"{FAM}/*/brick_2221/portal.json"))
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


def _json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def _no_errors(run):
    errs = run.errors(allow=(_plugin().ABORTED_LOAD,))
    assert not errs, errs


def _gallery_pane(run) -> str:
    return run.ev("() => document.querySelector(\"section.pk-pane[data-pane-type='gallery']\").dataset.paneId")


def _settled_stats(run) -> dict:
    run.wait_panes_settled()
    pane = _gallery_pane(run)
    run.page.wait_for_function(
        f"() => {{ const h = window.__pk.app.layout.pane('{pane}'); const s = h && h.stats();"
        " return !!(s && s.items > 0); }", timeout=60000)
    return run.pane_stats(pane)


def _expected(items: list[dict], facets: dict) -> int:
    return sum(1 for it in items if all(it.get("tags", {}).get(k) in v for k, v in facets.items()))


def _decode(preset: str) -> dict:
    import base64
    raw = preset[len("fac="):]
    return json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8"))


def test_ccds_cmds_legacy_presets(build_dir, runs):
    from portalkit.context import load_site
    items = _json(build_dir / "brick_2221" / "data" / "ccds_cmds" / "images.json")["items"]
    page = next(pg for pg in load_site(FAM).portal("brick_2221").pages if pg.id == "ccds_cmds")
    presets = page.presets()
    assert len(page.legacy) == 5 and len(presets) == 4
    run = runs("brick_2221/ccds_cmds.html")
    st = _settled_stats(run)
    assert st["items"] == len(items) and st["shown"] == len(items)
    _no_errors(run)
    for f, pr in presets.items():
        want = _expected(items, _decode(pr))
        assert 0 < want < len(items), f
        run = runs("brick_2221/ccds_cmds.html", hash=pr)
        st = _settled_stats(run)
        assert st["shown"] == want, (f, st, want)
        assert run.store_get("facets") == _decode(pr), f
        _no_errors(run)


def test_w51_rgb_roles_and_dir_presets(build_dir, runs):
    doc = _json(build_dir / "w51_public" / "data" / "w51_rgb" / "images.json")
    items = doc["items"]
    assert [r["key"] for r in doc["roles"]][:1] == ["main"]
    run = runs("w51_public/w51_rgb.html", hash="role=alma")
    _settled_stats(run)
    assert run.store_get("role") == "alma"
    _no_errors(run)
    sel = {"dir": ["pngs_2100"], "g_noalma": ["yes"]}
    from adapters.dirgallery_sites import stub_preset
    run = runs("w51_public/w51_rgb.html", hash=stub_preset(sel))
    st = _settled_stats(run)
    assert st["shown"] == _expected(items, sel) > 0
    _no_errors(run)


def test_png_b_catalog_gallery(build_dir, runs):
    run = runs("robitaille_modelgrids/png_b.html")
    st = _settled_stats(run)
    assert 0 < st["items"] <= LIMIT
    panes = run.ev("() => Array.from(document.querySelectorAll('section.pk-pane')).map((s) => s.dataset.paneType)")
    assert "gallery" in panes and "scatter" in panes
    _no_errors(run)


def test_nirspec6927_seds_public_caps(build_dir, runs):
    run = runs("brick_2221/nirspec6927_seds.html")
    st = _settled_stats(run)
    assert st["items"] > 0
    caps = run.caps_attr()
    assert {"static_sed_png", "lightbox", "keyboard_nav", "filter_toggles"} <= set(caps)
    _no_errors(run)


def test_landing_cards(build_dir, runs):
    portal = _json(build_dir / "brick_2221" / "portal.json")
    run = runs("brick_2221/index.html")
    run.wait_panes_settled()
    hrefs = run.ev("() => Array.from(document.querySelectorAll('.pk-cards a')).map((a) => a.getAttribute('href'))")
    pages = [p["file"] for p in portal["pages"] if p["id"] != "index"]
    for f in pages:
        assert any(h and h.split("#")[0].endswith(f) for h in hrefs), (f, hrefs)
    assert any(h and "/secure/adamginsburg/jwst/brick/unified/dirgallery_sites/" in h for h in hrefs), hrefs
    _no_errors(run)
