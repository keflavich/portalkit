"""Deep links (ARCHITECTURE.md sections 6.2, 6.4 and 13).

Canonical keys: ``id=`` focus survives a reload, ``sel`` round-trips through
``app.hashLink()``, and ``av``/``rv``/``sky``/``view`` reach the store. Legacy grammars: one
test per grammar the synthetic pages declare, each opened with a legacy-style URL and checked
through the store (focus, rung, beat, lightbox) and the canonical hash the page writes back,
then reloaded: the canonical hash must restore the same state (section 13, Level 2).
Legacy tests are ``xfail(strict=False)`` while ``web/js/core/legacyhash.js`` is absent.
"""

from __future__ import annotations

from urllib.parse import unquote

import pytest

import make_synthetic as ms

pytestmark = pytest.mark.e2e

N = 2000

FOCUS_JS = """async () => {
  const f = window.__pk.store.get('focus');
  if (!f) return null;
  const out = {id: f.id ?? null, i: (f.i === undefined ? null : f.i), ds: f.ds ?? null, sid: null};
  if (out.i !== null) out.sid = String(await window.__pk.app.idOf(f.ds, f.i));
  return out;
}"""

HASHLINK_JS = """() => { const h = window.__pk.app.hashLink(); return typeof h === 'string' ? h : (h && h.str) || ''; }"""


@pytest.fixture(scope="module")
def portal(synth):
    return synth(N)


def parse_hash(h: str) -> dict:
    h = h[1:] if h.startswith("#") else h
    out = {}
    for part in h.split("&"):
        if not part:
            continue
        k, _, v = part.partition("=")
        out[unquote(k)] = unquote(v)
    return out


def canon(run) -> dict:
    return parse_hash(run.ev(HASHLINK_JS))


def resolved_focus(run, timeout_ms: int = 30000) -> dict:
    run.page.wait_for_function(
        "() => { const f = window.__pk.store.get('focus'); return !!f && f.i !== null && f.i !== undefined; }",
        timeout=timeout_ms)
    return run.ev(FOCUS_JS)


def selected_ids(run, ds: str = "src") -> list[str]:
    return sorted(run.ev("""async (ds) => {
        const app = window.__pk.app;
        const sel = await app.selectionFor(ds);
        const out = [];
        for (const i of sel.indices()) out.push(String(await app.idOf(ds, i)));
        return out;
    }""", ds))


# ---------------------------------------------------------------------------
# canonical keys
# ---------------------------------------------------------------------------

def test_id_focus_survives_reload(portal, open_page):
    sid = ms.syn_id(6)
    run = open_page(portal.url_path("explorer"), hash=f"id={sid}")
    f = resolved_focus(run)
    assert f["sid"] == sid and f["ds"] == "src"
    run.page.reload()
    assert run.wait_ready() == "ok"
    f = resolved_focus(run)
    assert f["sid"] == sid
    assert canon(run).get("id") == sid


def test_focus_by_alias(portal, open_page):
    run = open_page(portal.url_path("explorer"), hash=f"id={ms.alias_of(9)}")
    f = resolved_focus(run)
    assert f["sid"] == ms.syn_id(9)


def test_selection_hash_roundtrip(portal, open_page):
    ids = [ms.syn_id(i) for i in (1, 3, 10, 42, 77)]
    run = open_page(portal.url_path("explorer"))
    assert run.ev("async (ids) => window.__pk.select('src', ids)", ids) == len(ids)
    run.page.wait_for_function("() => /(^|[#&])sel=/.test(location.hash)", timeout=10000)
    h = canon(run)
    assert "sel" in h
    link = run.ev(HASHLINK_JS)
    run2 = open_page(portal.url_path("explorer"), hash=link.lstrip("#"))
    run2.page.wait_for_function("() => (window.__pk.store.get('sel:src') || {}).count === %d" % len(ids),
                                timeout=20000)
    assert selected_ids(run2) == sorted(ids)
    # Back to the empty entry after a new selection keeps the history consistent
    run2.ev("async () => (await window.__pk.app.selectionFor('src')).clear()")
    run2.page.wait_for_function("() => !/(^|[#&])sel=/.test(location.hash)", timeout=10000)


def test_av_rv_sky_view_keys(portal, open_page):
    run = open_page(portal.url_path("explorer"),
                    hash="av=12.5&rv=5.5&sky=266.42,-29.01,0.2&view=scatter:half")
    assert run.store_get("av") == pytest.approx(12.5)
    assert run.store_get("rv") == pytest.approx(5.5)
    sky = run.store_get("sky") or {}
    assert sky.get("ra") == pytest.approx(266.42, abs=1e-6)
    assert sky.get("dec") == pytest.approx(-29.01, abs=1e-6)
    layout = run.store_get("layout") or {}
    assert (layout.get("popout") or {}).get("scatter") == "half"
    h = canon(run)
    assert float(h["av"]) == pytest.approx(12.5)
    assert float(h["rv"]) == pytest.approx(5.5)
    assert h.get("view") == "scatter:half"


def test_hash_change_updates_store(portal, open_page):
    run = open_page(portal.url_path("explorer"))
    run.ev("() => { location.hash = 'av=7&id=%s'; }" % ms.syn_id(4))
    run.page.wait_for_function("() => window.__pk.store.get('av') === 7", timeout=10000)
    f = resolved_focus(run)
    assert f["sid"] == ms.syn_id(4)


# ---------------------------------------------------------------------------
# legacy grammars
# ---------------------------------------------------------------------------

def _legacy(e2e, request, portal, page, grammar):
    assert grammar in portal.pages[page].legacy, (page, portal.pages[page].legacy)
    e2e.xfail_unless_legacyhash(request)


# hash keys whose encoding may legitimately change across a reload while the state is the
# same: a selection written as a recipe (``sel=r:...``) is re-encoded as ``sel=bits:...``
# once the restored recipe has been evaluated.  ``check`` verifies the state itself.
REENCODED_KEYS = ("sel",)


def reload_keeps(run, rewritten_js: str, check, timeout_ms: int = 15000) -> str:
    """Wait for the canonical rewrite (``rewritten_js`` true), reload, and run ``check(run)``
    again: the canonical form the page wrote back must restore the same state, and every hash
    key survives the reload (keys in ``REENCODED_KEYS`` must be present; their encoding may
    differ). Returns the canonical hash."""
    run.page.wait_for_function(rewritten_js, timeout=timeout_ms)
    before = run.ev("() => location.hash")
    run.page.reload()
    assert run.wait_ready() == "ok"
    check(run)
    after = run.ev("() => location.hash")
    kb, ka = parse_hash(before), parse_hash(after)
    assert set(kb) == set(ka), (before, after)
    for k in kb:
        if k not in REENCODED_KEYS:
            assert ka[k] == kb[k], (k, before, after)
    return before


def test_legacy_bare_name(e2e, request, portal, open_page):
    _legacy(e2e, request, portal, "explorer", "bare-name")
    run = open_page(portal.url_path("explorer"), hash=ms.syn_id(3))

    def check(r):
        assert resolved_focus(r)["sid"] == ms.syn_id(3)
    check(run)
    reload_keeps(run, "() => location.hash.startsWith('#id=')", check)


def test_legacy_kv_view(e2e, request, portal, open_page):
    _legacy(e2e, request, portal, "explorer", "kv-view")
    run = open_page(portal.url_path("explorer"), hash=f"name={ms.syn_id(4)}&view=scatter")

    def check(r):
        assert resolved_focus(r)["sid"] == ms.syn_id(4)
        assert ((r.store_get("layout") or {}).get("popout") or {}).get("scatter") == "full"
    check(run)
    reload_keeps(run, "() => /(^#|&)id=/.test(location.hash) && !/(^#|&)name=/.test(location.hash)", check)


def test_legacy_w51_rowid(e2e, request, portal, open_page):
    _legacy(e2e, request, portal, "explorer", "w51-rowid")
    run = open_page(portal.url_path("explorer"), hash=ms.alias_of(5).lower())
    f = resolved_focus(run)
    assert f["id"] == ms.alias_of(5)
    assert f["sid"] == ms.syn_id(5)

    def check(r):
        assert resolved_focus(r)["sid"] == ms.syn_id(5)
    reload_keeps(run, "() => /(^#|&)id=/.test(location.hash)", check)


def test_legacy_gc_query_rv_colour(e2e, request, portal, open_page):
    _legacy(e2e, request, portal, "manifold", "gc-query")
    run = open_page(portal.url_path("manifold"), query="rv=5.5&cb=0&g=2")
    assert run.store_get("rv") == pytest.approx(5.5)
    h = canon(run)
    assert float(h["rv"]) == pytest.approx(5.5)
    search = run.ev("() => location.search")
    assert "rv=" not in search and "pktest=1" in search

    def check(r):
        assert r.store_get("rv") == pytest.approx(5.5)
        assert "rv=" not in r.ev("() => location.search")
    reload_keeps(run, "() => /(^#|&)rv=/.test(location.hash)", check)
    # cb=<dim index> needs the dim list; legacyhash.js reads it from the manifold pane's
    # options.dims, which the resolver leaves unset when the manifest carries the manifold dims
    mani = [p for p in portal.pages["manifold"].panes if p["pane"] == "manifold"]
    if not any(isinstance(p["options"].get("dims"), list) for p in mani):
        pytest.xfail("manifold pane has no options.dims list, so gc-query cb= cannot be mapped "
                     "(contract issue between caps.py default layout and legacyhash.js)")
    assert "c" in h and h["c"].split("~")[0] in ms.MAG_KEYS
    assert "cb=" not in search


def test_legacy_gc_query_sel_position(e2e, request, portal, open_page):
    _legacy(e2e, request, portal, "manifold", "gc-query")
    d = portal.data
    k = 7
    run = open_page(portal.url_path("manifold"), query=f"sel={d.ra[k]:.7f},{d.dec[k]:.7f}")

    def check(r):
        r.page.wait_for_function("() => (window.__pk.store.get('sel:src') || {}).count > 0", timeout=20000)
        assert ms.syn_id(k) in selected_ids(r)
    check(run)
    reload_keeps(run, "() => /(^#|&)sel=/.test(location.hash) && !/[?&]sel=/.test(location.search)", check)


def _wavelength_check(ra, dec, fov):
    def check(r):
        assert r.store_get("rung") == "r1"
        sky = r.store_get("sky") or {}
        assert sky.get("ra") == pytest.approx(ra, abs=1e-6)
        assert sky.get("dec") == pytest.approx(dec, abs=1e-6)
        assert sky.get("fov") == pytest.approx(fov, abs=1e-9)
    return check


WAVELENGTH_REWRITTEN = "() => /(^#|&)rung=/.test(location.hash) && /(^#|&)sky=/.test(location.hash)"


def test_legacy_wavelength(e2e, request, portal, open_page):
    """Compact form ``#rung,ra,dec,fov``."""
    _legacy(e2e, request, portal, "wavelength", "wavelength")
    run = open_page(portal.url_path("wavelength"), hash="r1,266.43,-29.0,0.1")
    check = _wavelength_check(266.43, -29.0, 0.1)
    check(run)
    reload_keeps(run, WAVELENGTH_REWRITTEN, check)


def test_legacy_wavelength_kv_and_speed(e2e, request, portal, open_page):
    """Key-value form ``#rung=&ra=&dec=&fov=`` plus ``?speed=`` (consumed from the query)."""
    _legacy(e2e, request, portal, "wavelength", "wavelength")
    run = open_page(portal.url_path("wavelength"), query="speed=2",
                    hash="rung=r1&ra=266.41&dec=-29.02&fov=0.15")
    check = _wavelength_check(266.41, -29.02, 0.15)
    check(run)
    run.page.wait_for_function("() => !/[?&]speed=/.test(location.search)", timeout=15000)
    search = run.ev("() => location.search")
    assert "pktest=1" in search
    h = parse_hash(reload_keeps(run, WAVELENGTH_REWRITTEN, check))
    assert not {"ra", "dec", "fov"} & set(h), h


def test_legacy_beat(e2e, request, portal, open_page):
    _legacy(e2e, request, portal, "narrative", "beat")
    run = open_page(portal.url_path("narrative"), hash="beat=b3&x=1")

    def check(r):
        assert r.store_get("beat") == "b3"
        assert canon(r).get("beat") == "b3"
    check(run)
    reload_keeps(run, "() => /(^#|&)beat=b3(&|$)/.test(location.hash)", check)


def test_legacy_gallery_index(e2e, request, portal, open_page):
    _legacy(e2e, request, portal, "gallery", "gallery-index")
    run = open_page(portal.url_path("gallery"), hash="5")

    def check(r):
        r.page.wait_for_function("() => (window.__pk.store.get('lightbox') || {}).index === 5", timeout=15000)
    check(run)
    reload_keeps(run, "() => /(^#|&)lb=/.test(location.hash)", check)


def test_legacy_gallery_img_id(e2e, request, portal, open_page):
    _legacy(e2e, request, portal, "gallery", "gallery-index")
    run = open_page(portal.url_path("gallery"), hash=f"img={ms.syn_id(2)}")

    def check(r):
        assert resolved_focus(r)["sid"] == ms.syn_id(2)
    check(run)
    reload_keeps(run, "() => /(^#|&)id=/.test(location.hash) && !/(^#|&)img=/.test(location.hash)", check)


def test_legacy_specviewer(e2e, request, portal, open_page):
    _legacy(e2e, request, portal, "spectra", "specviewer")
    run = open_page(portal.url_path("spectra"), hash=ms.syn_id(2))

    def check(r):
        assert resolved_focus(r)["sid"] == ms.syn_id(2)
    check(run)
    reload_keeps(run, "() => location.hash.startsWith('#id=')", check)


def test_canonical_hash_bypasses_legacy(e2e, request, portal, open_page):
    """A canonical hash on a page with legacy grammars is used as is."""
    _legacy(e2e, request, portal, "explorer", "kv-view")
    run = open_page(portal.url_path("explorer"), hash=f"id={ms.syn_id(8)}&av=3")
    assert resolved_focus(run)["sid"] == ms.syn_id(8)
    assert run.store_get("av") == pytest.approx(3)
