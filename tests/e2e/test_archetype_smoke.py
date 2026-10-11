"""Level 1 smoke tests (ARCHITECTURE.md section 13).

For every archetype page of the synthetic portal at N = 8 and 2k (the catalog-heavy pages
also at 50k and 1M under ``-m e2e``, and the explorer at 5M under ``-m slow``): ``__pk.ready``
resolves, no console errors, HTML < 200 kB, ``data-caps``
equals the resolver output, no horizontal scroll at 390x844, tiers per N (and tier F with
WebGL2 disabled), a run without ``DecompressionStream``, and no external origin other than
the Aladin CDN / mirror.

Checks that depend on pane modules (WP08-WP11) or the legacy hash module (WP13) are marked
``xfail(strict=False)`` while those files are absent; the boot-level checks are strict.
"""

from __future__ import annotations

import html as htmlmod
import re

import pytest

import make_synthetic as ms

pytestmark = pytest.mark.e2e

HTML_MAX = 200 * 1024
SMALL = (8, 2000)
BIG = (50000, 1000000)
HUGE = ms.SLOW_SIZES          # 5M: -m slow only
ALL_PAGES = tuple(ms.PAGES)
BIG_PAGES = ("explorer", "manifold", "vcolor", "hist", "cmd")
HUGE_PAGES = ("explorer",)
TIERED = ("scatter", "manifold", "hist", "sky", "list", "table")

CASES = [(n, p) for n in SMALL for p in ALL_PAGES] + \
        [(n, p) for n in BIG for p in BIG_PAGES] + \
        [pytest.param(n, p, marks=pytest.mark.slow) for n in HUGE for p in HUGE_PAGES]

_BODY_CAPS = re.compile(r"<body\b[^>]*\bdata-caps=(\"[^\"]*\"|'[^']*')")


def _expect_clean(e2e, request, portal, page):
    e2e.xfail_unless_panes(request, e2e.page_pane_types(portal, page))
    if portal.pages[page].legacy:
        e2e.xfail_unless_legacyhash(request)


@pytest.mark.parametrize("n,page", CASES)
def test_html_size_and_caps_attr(synth, n, page):
    """HTML under 200 kB, and the shell carries the resolver output in data-caps."""
    portal = synth(n)
    info = portal.pages[page]
    html = (portal.root / info.file).read_bytes()
    assert len(html) < HTML_MAX
    assert info.html_bytes == len(html)
    text = html.decode("utf-8")
    assert f'data-page="{page}"' in text
    assert f'data-archetype="{info.archetype}"' in text
    m = _BODY_CAPS.search(text)
    assert m, "no data-caps attribute on <body>"
    caps = htmlmod.unescape(m.group(1)[1:-1]).split()
    assert caps == sorted(caps)
    # the static attribute carries the resolver output for this page
    spec = next(ps for ps in ms.page_specs() if ps.id == page)
    ds, _ = ms.datasets(min(n, 2000))
    assert caps == sorted(info.caps)
    if n <= 2000:
        assert caps == sorted(ms.resolve_page(spec, ds).enabled)


@pytest.mark.parametrize("n,page", CASES)
def test_boot(e2e, request, synth, open_page, n, page):
    portal = synth(n)
    run = open_page(portal.url_path(page))
    # data-caps equals the resolver output and the runtime set
    assert run.caps_attr() == sorted(portal.pages[page].caps)
    assert sorted(run.ev("() => window.__pk.caps()")) == sorted(portal.pages[page].caps)
    # no external origin other than the Aladin CDN / mirror
    assert e2e.ext_origins(run.external) <= e2e.ALLOWED_EXTERNAL, run.external
    _expect_clean(e2e, request, portal, page)
    run.wait_panes_settled()
    assert run.errors() == []
    states = run.pane_states()
    assert all(s in (None, "ready") for s in states.values()), states


@pytest.mark.parametrize("page", ALL_PAGES)
def test_mobile_no_horizontal_scroll(e2e, request, synth, open_page, page):
    portal = synth(2000)
    run = open_page(portal.url_path(page), viewport={"width": 390, "height": 844})
    e2e.xfail_unless_panes(request, e2e.page_pane_types(portal, page))
    run.wait_panes_settled()
    assert run.ev("() => window.__pk.app.layout.isTabMode()") is True
    over = run.ev("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert over <= 0, f"horizontal overflow of {over}px at 390x844"


def _expected_tier(n, rt):
    from portalkit.constants import T0_MAX, T1_MAX
    if n <= T0_MAX:
        return "T0"
    if not rt["webgl2"]:
        return "F"
    if n <= T1_MAX:
        return "T1"
    return "T2" if rt["floatRT"] else "F"


def _tiers(run, portal, page):
    ids = [p["id"] for p in portal.pages[page].panes
           if p["pane"] in TIERED and p["dataset"] in ("src", "vc")]
    run.page.wait_for_function(
        "(ids) => ids.every((id) => !!window.__pk.store.get('tier:' + id))", arg=ids, timeout=120000)
    return ids, run.ev("() => window.__pk.tiers()")


@pytest.mark.parametrize("n", [8, 2000, 50000, 1000000] +
                         [pytest.param(n, marks=pytest.mark.slow) for n in HUGE])
def test_tiers(synth, open_page, n):
    portal = synth(n)
    run = open_page(portal.url_path("explorer"))
    rt = run.ev("() => window.__pk.app.runtime")
    ids, tiers = _tiers(run, portal, "explorer")
    want = _expected_tier(n, rt)
    assert ids and all(tiers[i] == want for i in ids), (tiers, want, rt)


@pytest.mark.parametrize("n", [8, 50000])
def test_tier_f_without_webgl2(e2e, request, synth, open_page, n):
    portal = synth(n)
    run = open_page(portal.url_path("explorer"), webgl=False)
    rt = run.ev("() => window.__pk.app.runtime")
    assert rt["webgl2"] is False
    ids, tiers = _tiers(run, portal, "explorer")
    want = "T0" if n <= 3000 else "F"
    assert all(tiers[i] == want for i in ids), tiers
    # tier F keeps density_rendering; data-caps still equals the resolver output
    assert run.caps_attr() == sorted(portal.pages["explorer"].caps)
    if n > 3000:
        notes = run.ev("() => window.__pk.app.notes")
        assert "density_rendering" in notes
    _expect_clean(e2e, request, portal, "explorer")
    run.wait_panes_settled()
    assert run.errors() == []


def test_without_decompression_stream(e2e, request, synth, open_page):
    portal = synth(2000)
    run = open_page(portal.url_path("explorer"), decompression=False)
    rt = run.ev("() => window.__pk.app.runtime")
    assert rt["decompressionStream"] is False
    n = run.ev("async () => (await window.__pk.hub.catalog('src')).n")
    assert n == 2000
    rec = run.ev("async () => { const c = await window.__pk.hub.catalog('src'); return await c.id(0); }")
    assert rec in portal.stored_ids[:1]
    _expect_clean(e2e, request, portal, "explorer")
    run.wait_panes_settled()
    assert run.errors() == []


@pytest.mark.parametrize("page", ALL_PAGES)
def test_no_unexpected_external_requests(e2e, synth, open_page, page):
    portal = synth(8)
    run = open_page(portal.url_path(page))
    run.page.wait_for_timeout(500)
    assert e2e.ext_origins(run.external) <= e2e.ALLOWED_EXTERNAL, run.external


def test_portal_json_and_landing(synth, server, open_page):
    portal = synth(8)
    doc = ms.json.loads((portal.root / "portal.json").read_text())
    assert [p["id"] for p in doc["pages"]] == list(ms.PAGES)
    run = open_page(portal.url_path("index"))
    assert run.store_get("sel:src") is None or isinstance(run.store_get("sel:src"), dict)
