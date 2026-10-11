"""Deep links (ARCHITECTURE.md sections 6.2, 6.4 and 13).

Canonical keys: ``id=`` focus survives a reload, ``sel`` round-trips through
``app.hashLink()``, and ``av``/``rv``/``sky``/``view`` reach the store. Legacy grammars: one
test per grammar the synthetic pages declare, each opened with a legacy-style URL and checked
through the store (focus, rung, beat, lightbox) and the canonical hash the page writes back,
then reloaded: the canonical hash must restore the same state and be written back
unchanged (section 13, Level 2; IMPLEMENTATION_NOTES item 92 makes the ``sel=`` form a
function of the selection alone). IMPLEMENTATION_NOTES items 90 (gc-query toggles consumed,
unmappable parameters kept, literal ``+`` in bare-name / specviewer hashes) and 91 (axes
presets as manifold frames) are asserted directly.
"""

from __future__ import annotations

import json
import re
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


def resolved_focus(run, timeout_ms: int = 60000) -> dict:
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


def reload_keeps(run, rewritten_js: str, check, timeout_ms: int = 15000) -> str:
    """Wait for the canonical rewrite (``rewritten_js`` true) and for the hash to settle,
    reload, and run ``check(run)`` again: the canonical form the page wrote back must restore
    the same state and the page must write the same hash after the reload. Returns the
    canonical hash."""
    run.page.wait_for_function(rewritten_js, timeout=timeout_ms)
    before = run.stable_hash()
    run.page.reload()
    assert run.wait_ready() == "ok"
    check(run)
    after = run.stable_hash()
    assert parse_hash(after) == parse_hash(before), (before, after)
    return before


def test_legacy_bare_name(e2e, request, portal, open_page):
    _legacy(e2e, request, portal, "explorer", "bare-name")
    run = open_page(portal.url_path("explorer"), hash=ms.syn_id(3))

    def check(r):
        assert resolved_focus(r)["sid"] == ms.syn_id(3)
    check(run)
    reload_keeps(run, "() => location.hash.startsWith('#id=')", check)


@pytest.mark.parametrize("page,grammar", [("explorer", "bare-name"), ("spectra", "specviewer")])
def test_legacy_plus_stays_literal(e2e, request, portal, open_page, page, grammar):
    """IMPLEMENTATION_NOTES item 90: a source name with '+' set raw as the hash (legacy
    ``location.hash = name``) keeps the '+', so it resolves through the alias."""
    _legacy(e2e, request, portal, page, grammar)
    name = ms.plus_alias_of(4)
    assert "+" in name
    run = open_page(portal.url_path(page), hash=name)

    def check(r):
        assert resolved_focus(r)["sid"] == ms.syn_id(4)
    check(run)
    h = parse_hash(reload_keeps(run, "() => location.hash.startsWith('#id=')", check))
    assert h["id"] in (name, ms.syn_id(4)), h


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


def test_legacy_gc_query_numeric_rv(e2e, request, portal, open_page):
    """A numeric gc-query ``rv`` (> 1.5) maps to the canonical R_V; ``g`` is consumed."""
    _legacy(e2e, request, portal, "manifold", "gc-query")
    run = open_page(portal.url_path("manifold"), query="rv=5.5&g=2")
    assert run.store_get("rv") == pytest.approx(5.5)
    h = canon(run)
    assert float(h["rv"]) == pytest.approx(5.5)
    search = run.ev("() => location.search")
    assert "rv=" not in search and "g=" not in search and "pktest=1" in search

    def check(r):
        assert r.store_get("rv") == pytest.approx(5.5)
        assert "rv=" not in r.ev("() => location.search")
    reload_keeps(run, "() => /(^#|&)rv=/.test(location.hash)", check)


def test_legacy_gc_query_toggles_dropped(e2e, request, portal, open_page):
    """IMPLEMENTATION_NOTES item 90: the display toggles ``rv=1``, ``g`` and ``fa`` have no
    canonical key; they are consumed (gone from the query) and leave R_V unchanged."""
    _legacy(e2e, request, portal, "manifold", "gc-query")
    ref = open_page(portal.url_path("manifold"))
    rv0 = ref.store_get("rv")
    run = open_page(portal.url_path("manifold"), query="rv=1&g=2&fa=1")
    run.page.wait_for_function("() => !/[?&](rv|g|fa)=/.test(location.search)", timeout=15000)
    assert "pktest=1" in run.ev("() => location.search")
    assert run.store_get("rv") == rv0
    assert "rv" not in canon(run)
    assert not {"g", "fa"} & set(parse_hash(run.ev("() => location.hash")))
    assert run.errors() == []


def test_legacy_gc_query_colour_by(e2e, request, portal, open_page):
    """``cb=<dim index>`` maps to ``c=<dim key>`` through the manifold pane's ``options.dims``
    (IMPLEMENTATION_NOTES item 88 gives every manifold pane its dims)."""
    _legacy(e2e, request, portal, "manifold", "gc-query")
    mani = [p for p in portal.pages["manifold"].panes if p["pane"] == "manifold"]
    assert mani and all(isinstance(p["options"].get("dims"), list) for p in mani), mani
    dims = mani[0]["options"]["dims"]
    run = open_page(portal.url_path("manifold"), query="cb=1")
    run.page.wait_for_function("() => /(^#|&)c=/.test(location.hash)", timeout=15000)
    h = canon(run)
    assert h["c"].split("~")[0] == dims[1], (h, dims)
    assert "cb=" not in run.ev("() => location.search")

    def check(r):
        assert canon(r)["c"].split("~")[0] == dims[1]
    reload_keeps(run, "() => /(^#|&)c=/.test(location.hash)", check)


def test_legacy_gc_query_unmappable_stays(e2e, request, portal, open_page):
    """IMPLEMENTATION_NOTES item 90: an index parameter that maps to nothing (``cb`` past the
    dim list) stays in the URL, while the mappable ``g`` next to it is consumed."""
    _legacy(e2e, request, portal, "manifold", "gc-query")
    run = open_page(portal.url_path("manifold"), query="cb=99&g=2")
    run.page.wait_for_function("() => !/[?&]g=/.test(location.search)", timeout=15000)
    search = run.ev("() => location.search")
    assert "cb=99" in search, search
    assert "c" not in canon(run)


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


# ---- narrative beats (WPD2): the view and overlays a beat applies are implied by beat= and
# stay out of the hash; a user pan/zoom or layer change writes sky= / ovl=; a hash sky= is kept
# over the beat's view; Aladin's adjustment of a programmatic goto is not a user change.

BEATS = {b["id"]: b for b in ms.narrative_spec([]).beats}

PANE_OF_TYPE_JS = """(t) => { const s = document.querySelector(`section.pk-pane[data-pane-type='${t}']`);
                            return s ? s.dataset.paneId : null; }"""

# the stub Aladin instance shown by the sky pane
SKY_INST_JS = """(p) => { const sec = document.querySelector(`section.pk-pane[data-pane-id='${p}']`);
  return (window.__aladinStub.instances || []).find((a) => sec && sec.contains(a.div)) || null; }"""


def narrative_ready(run, beat: str, timeout_ms: int = 60000) -> str:
    """Wait until the narrative pane shows ``beat`` and the sky pane's viewer is ready and on a
    finite view; returns the sky pane id."""
    nar = run.ev(PANE_OF_TYPE_JS, "narrative")
    sky = run.ev(PANE_OF_TYPE_JS, "sky")
    assert nar and sky, (nar, sky)
    run.page.wait_for_function(
        """([n, s, b]) => { const L = window.__pk.app.layout;
             const hn = L.pane(n), hs = L.pane(s);
             const sn = hn && hn.stats ? hn.stats() : null, ss = hs && hs.stats ? hs.stats() : null;
             return !!(sn && sn.beat === b && ss && ss.view && Number.isFinite(ss.view.ra)); }""",
        arg=[nar, sky, beat], timeout=timeout_ms)
    return sky


def stub_move(run, sky: str, dra: float = 0.0, fov_scale: float = 1.0, user: bool = False) -> None:
    """Move the sky pane's stub viewer by ``dra`` degrees and scale its fov; with ``user`` a
    pointerdown on the viewer comes first (a drag), without it the move stands for Aladin's own
    adjustment of a programmatic goto."""
    run.ev("""([p, dra, k, user, js]) => {
        const a = (new Function('return ' + js))()(p);
        if (user) a.div.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true, cancelable: true}));
        a.gotoRaDec(a.ra + dra, a.dec);
        if (k !== 1) a.setFoV(a.fov * k);
    }""", [sky, dra, fov_scale, user, SKY_INST_JS])


def beat_sky(beat: str) -> dict:
    return BEATS[beat]["view"]["sky"]


def test_legacy_beat(e2e, request, portal, open_page):
    _legacy(e2e, request, portal, "narrative", "beat")
    run = open_page(portal.url_path("narrative"), hash="beat=b3&x=1")
    want = beat_sky("b3")

    def check(r):
        assert r.store_get("beat") == "b3"
        narrative_ready(r, "b3")
        sky = r.store_get("sky") or {}
        assert sky.get("ra") == pytest.approx(want["ra"], abs=1e-9)
        assert sky.get("fov") == pytest.approx(want["fov"], abs=1e-9)
        h = canon(r)
        assert h.get("beat") == "b3"
        # the beat's view and overlays are implied by beat=
        assert "sky" not in h and "ovl" not in h, h
    check(run)
    assert reload_keeps(run, "() => /(^#|&)beat=b3(&|$)/.test(location.hash)", check) == "#beat=b3"


def test_beat_aladin_adjustment_is_not_a_user_change(e2e, request, portal, open_page):
    """Aladin rounding or adjusting the fov of the beat's goto (no input on the viewer) leaves
    the hash at ``#beat=b3`` and the stored view at the beat's."""
    _legacy(e2e, request, portal, "narrative", "beat")
    run = open_page(portal.url_path("narrative"), hash="beat=b3")
    sky = narrative_ready(run, "b3")
    stub_move(run, sky, dra=2e-5, fov_scale=1.004)
    assert run.stable_hash() == "#beat=b3"
    assert (run.store_get("sky") or {}).get("fov") == pytest.approx(beat_sky("b3")["fov"], abs=1e-9)
    assert run.errors() == []


def test_beat_user_pan_writes_sky_and_reload_keeps_it(e2e, request, portal, open_page):
    _legacy(e2e, request, portal, "narrative", "beat")
    run = open_page(portal.url_path("narrative"), hash="beat=b3")
    sky = narrative_ready(run, "b3")
    stub_move(run, sky, dra=0.05, fov_scale=1.5, user=True)
    run.page.wait_for_function("() => /(^#|&)sky=/.test(location.hash)", timeout=15000)
    want = beat_sky("b3")
    ra, fov = want["ra"] + 0.05, want["fov"] * 1.5

    def check(r):
        narrative_ready(r, "b3")
        st = r.store_get("sky") or {}
        assert st.get("ra") == pytest.approx(ra, abs=1e-6)
        assert st.get("fov") == pytest.approx(fov, abs=1e-6)
        h = canon(r)
        assert h.get("beat") == "b3" and "ovl" not in h, h
        assert [float(x) for x in h["sky"].split(",")] == pytest.approx([ra, want["dec"], fov], abs=1e-6)
    check(run)
    reload_keeps(run, "() => /(^#|&)sky=/.test(location.hash)", check)
    # moving on to the next beat applies its view again: implied, out of the hash
    nar = run.ev(PANE_OF_TYPE_JS, "narrative")
    run.ev("(p) => window.__pk.app.layout.pane(p).go('b4')", nar)
    narrative_ready(run, "b4")
    assert run.stable_hash() == "#beat=b4"
    assert (run.store_get("sky") or {}).get("ra") == pytest.approx(beat_sky("b4")["ra"], abs=1e-9)
    assert run.errors() == []


def test_beat_hash_sky_kept_over_the_beat_view(e2e, request, portal, open_page):
    _legacy(e2e, request, portal, "narrative", "beat")
    run = open_page(portal.url_path("narrative"), hash="sky=266.45,-29.01,0.3&beat=b3")

    def check(r):
        sky = narrative_ready(r, "b3")
        st = r.store_get("sky") or {}
        assert (st.get("ra"), st.get("dec"), st.get("fov")) == pytest.approx((266.45, -29.01, 0.3), abs=1e-9)
        v = r.pane_stats(sky)["view"]
        assert (v["ra"], v["dec"], v["fov"]) == pytest.approx((266.45, -29.01, 0.3), abs=1e-6)
        h = canon(r)
        assert h.get("sky") == "266.45,-29.01,0.3" and h.get("beat") == "b3" and "ovl" not in h, h
        # the beat's overlays still apply (implied)
        shown = set(r.ev("(p) => window.__pk.app.layout.pane(p).layers()", sky))
        on, off = set(BEATS["b3"]["view"]["layers_on"]), set(BEATS["b3"]["view"]["layers_off"])
        assert on <= shown and not (off & shown), shown
    check(run)
    assert parse_hash(reload_keeps(run, "() => /(^#|&)sky=/.test(location.hash)", check)) == \
        {"sky": "266.45,-29.01,0.3", "beat": "b3"}


def test_beat_overlay_toggle_writes_ovl(e2e, request, portal, open_page):
    _legacy(e2e, request, portal, "narrative", "beat")
    run = open_page(portal.url_path("narrative"), hash="beat=b3")
    sky = narrative_ready(run, "b3")
    assert "rings" in BEATS["b3"]["view"]["layers_off"]
    # the user turns on a layer the beat turned off (layers panel checkbox)
    run.ev("""(p) => { const sec = document.querySelector(`section.pk-pane[data-pane-id='${p}']`);
        const box = sec.querySelector('input[data-role="ovl"][data-id="rings"]');
        box.checked = true; box.dispatchEvent(new Event('change', {bubbles: true})); }""", sky)
    run.page.wait_for_function("() => /(^#|&)ovl=/.test(location.hash)", timeout=15000)

    def check(r):
        s = narrative_ready(r, "b3")
        h = canon(r)
        assert "sky" not in h and h.get("beat") == "b3", h
        assert "rings" in [t.split(":")[0] for t in h["ovl"].split(",")], h
        assert "rings" in r.ev("(p) => window.__pk.app.layout.pane(p).layers()", s)
    check(run)
    reload_keeps(run, "() => /(^#|&)ovl=/.test(location.hash)", check)
    assert run.errors() == []


def test_beat_unknown_id_canonicalizes_to_the_shown_beat(e2e, request, portal, open_page):
    """``#beat=<unknown>`` shows the first beat and writes its id, so the hash names the beat
    whose view and overlays it implies: ``#beat=b1`` with no ``sky=`` / ``ovl=``."""
    _legacy(e2e, request, portal, "narrative", "beat")
    run = open_page(portal.url_path("narrative"), hash="beat=bogus")

    def check(r):
        narrative_ready(r, "b1")
        assert r.store_get("beat") == "b1"
        st = r.store_get("sky") or {}
        assert st.get("ra") == pytest.approx(beat_sky("b1")["ra"], abs=1e-9)
        assert canon(r) == {"beat": "b1"}
    check(run)
    assert reload_keeps(run, "() => /(^#|&)beat=b1(&|$)/.test(location.hash)", check) == "#beat=b1"
    assert run.errors() == []


# b4 rewritten as a beat with a highlight and no sky view: the sky pane centres on the
# highlighted rows (narrative:highlight goto) and keeps the fov it had
HIGHLIGHT_ONLY_BEAT = "b4"
# the rows b3 highlights on the e2e portal (narrative_spec(rec_ids): the first three records)
B3_HIGHLIGHT = next(b["view"]["highlight"] for b in ms.narrative_spec([ms.syn_id(i) for i in range(3)]).beats
                    if b["view"].get("highlight"))

CENTROID_JS = """async ([ds, ids]) => { const app = window.__pk.app;
  const idx = (await app.resolveIds(ds, ids)).filter((i) => i >= 0);
  const c = await app.catalog(ds);
  const recs = (await Promise.all(idx.map((i) => c.record(i, {pos: true})))).filter((q) => q && Number.isFinite(q.ra));
  return recs.length ? [recs.reduce((a, q) => a + q.ra, 0) / recs.length,
                        recs.reduce((a, q) => a + q.dec, 0) / recs.length] : null; }"""


def open_highlight_only(browser, open_page, portal, hash: str):
    """Open the narrative page with beat b4 served as {pane, layers, highlight: b3's rows} (no
    ``view.sky``); returns (run, highlight)."""
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})

    def rewrite(route):
        doc = route.fetch().json()
        hl = next(b["view"]["highlight"] for b in doc["beats"] if b["view"].get("highlight"))
        for b in doc["beats"]:
            if b["id"] == HIGHLIGHT_ONLY_BEAT:
                b["view"].pop("sky", None)
                b["view"]["highlight"] = hl
        route.fulfill(status=200, content_type="application/json", body=json.dumps(doc))
    ctx.route(re.compile(r".*/data/narrative\.json(\?.*)?$"), rewrite)
    run = open_page(portal.url_path("narrative"), hash=hash, context=ctx)
    return run, B3_HIGHLIGHT


def wait_highlight(run, sky: str, n: int, timeout_ms: int = 30000) -> None:
    run.page.wait_for_function(
        "([p, n]) => { const h = window.__pk.app.layout.pane(p); return !!h && h.stats().highlight === n; }",
        arg=[sky, n], timeout=timeout_ms)


def test_beat_highlight_only_is_implied_on_a_fresh_load(e2e, request, browser, portal, open_page):
    """``#beat=b4`` where b4 only centres on its highlight: the centring settles as implied by
    b4 (its fov is the page's initial one), so the hash stays ``#beat=b4`` and a reload gives
    the same view."""
    _legacy(e2e, request, portal, "narrative", "beat")
    run, hl = open_highlight_only(browser, open_page, portal, "beat=b4")
    assert hl and hl["ids"], hl
    n = len(hl["ids"])
    centre = run.ev(CENTROID_JS, [hl.get("ds") or "src", hl["ids"]])
    assert centre is not None
    state: dict = {}

    def check(r):
        sky = narrative_ready(r, "b4")
        wait_highlight(r, sky, n)
        r.page.wait_for_function(
            "([ra, dec]) => { const s = window.__pk.store.get('sky') || {};"
            " return Math.abs(s.ra - ra) < 1e-5 && Math.abs(s.dec - dec) < 1e-5; }",
            arg=centre, timeout=15000)
        st = r.store_get("sky") or {}
        assert st.get("beatView") == "b4", st
        assert canon(r) == {"beat": "b4"}
        if "fov" in state:
            assert st["fov"] == pytest.approx(state["fov"], rel=1e-6)
        state["fov"] = st["fov"]
    check(run)
    assert run.stable_hash() == "#beat=b4"
    assert reload_keeps(run, "() => /(^#|&)beat=b4(&|$)/.test(location.hash)", check) == "#beat=b4"
    assert run.errors() == []


def test_beat_highlight_only_after_another_beat_writes_its_view(e2e, request, browser, portal, open_page):
    """Going from b2 (fov 0.17) to the highlight-only b4 keeps b2's fov, which ``#beat=b4``
    alone would not reproduce: the view is written (``sky=``) and a reload keeps it."""
    _legacy(e2e, request, portal, "narrative", "beat")
    run, hl = open_highlight_only(browser, open_page, portal, "beat=b2")
    n = len(hl["ids"])
    narrative_ready(run, "b2")
    centre = run.ev(CENTROID_JS, [hl.get("ds") or "src", hl["ids"]])
    nar = run.ev(PANE_OF_TYPE_JS, "narrative")
    run.ev("(p) => window.__pk.app.layout.pane(p).go('b4')", nar)
    fov = beat_sky("b2")["fov"]

    def check(r):
        sky = narrative_ready(r, "b4")
        r.page.wait_for_function(
            "([ra, dec]) => { const s = window.__pk.store.get('sky') || {};"
            " return Math.abs(s.ra - ra) < 1e-5 && Math.abs(s.dec - dec) < 1e-5; }",
            arg=centre, timeout=15000)
        wait_highlight(r, sky, n)
        st = r.store_get("sky") or {}
        assert "beatView" not in st, st
        assert st["fov"] == pytest.approx(fov, rel=1e-6)
        h = canon(r)
        assert h.get("beat") == "b4" and "ovl" not in h, h
        assert [float(x) for x in h["sky"].split(",")] == pytest.approx([centre[0], centre[1], fov], abs=1e-6)
    check(run)
    reload_keeps(run, "() => /(^#|&)sky=/.test(location.hash) && /(^#|&)beat=b4(&|$)/.test(location.hash)", check)
    assert run.errors() == []


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


def test_manifold_axes_preset_frame(portal, open_page):
    """IMPLEMENTATION_NOTES item 91: ``<manifold>.frame=<axes preset>`` gives the two-dim
    frame with unit weight on the preset's x dim (row 0) and y dim (row 1)."""
    run = open_page(portal.url_path("manifold"), hash="manifold.frame=cmd")
    run.ev("(p) => window.__pk.app.layout.reveal(p)", "manifold")
    run.ev("async (p) => { await window.__pk.app.layout.whenMounted(p); }", "manifold")
    run.page.wait_for_function(
        """() => { const h = window.__pk.app.layout.pane('manifold'); const s = h && h.stats ? h.stats() : null;
                   return !!(s && s.frame && s.dims && s.frame.length === 2 * s.dims.length); }""", timeout=60000)
    st = run.pane_stats("manifold")
    dims, F = st["dims"], st["frame"]
    D = len(dims)
    ix, iy = dims.index(ms.CMD_X), dims.index(ms.CMD_Y)
    want = [0.0] * (2 * D)
    want[ix] = 1.0
    want[D + iy] = 1.0
    assert [abs(v) for v in F] == pytest.approx(want, abs=1e-6), (dims, F)
    assert run.store_get("frame:manifold") is None or \
        (run.store_get("frame:manifold") or {}).get("preset") in (None, "cmd")
    assert run.errors() == []
