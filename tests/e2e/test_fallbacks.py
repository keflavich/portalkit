"""Aladin CDN / mirror fallback (ARCHITECTURE.md section 13, ``cdn_fallback``).

The harness routes every non-local request: Aladin script URLs on the CDS CDN or the mirror
get the stub (``tests/fixtures/aladin_stub.js``), everything else is aborted and recorded.

* default: the CDN URL is requested and served by the stub, which records API calls;
* CDN aborted: the mirror URL is requested next and the sky map comes up;
* both aborted: the sky pane shows ``.pk-error-note``, ``app.notes`` explains it, and the
  other panes stay usable (selection, list, scatter);
* no origin other than the CDN and the mirror is requested in any of these runs.

Chromium logs ``Failed to load resource: net::ERR_FAILED`` for every request the harness
aborts, with no source URL attached; the blocked-origin runs pass that message
(``e2e.ABORTED_LOAD``) to ``PageRun.errors(allow=...)``. Every other error still fails them.
"""

from __future__ import annotations

import pytest

import make_synthetic as ms

pytestmark = pytest.mark.e2e

N = 2000
SKY_PAGES = (("explorer", "sky"), ("wavelength", "wavelength"))


@pytest.fixture(scope="module")
def portal(synth):
    return synth(N)


def _instances(run) -> int:
    return run.ev("() => (window.__aladinStub && window.__aladinStub.instances) ? window.__aladinStub.instances.length : 0")


def _wait_instance(run, timeout_ms: int = 30000) -> None:
    run.page.wait_for_function(
        "() => !!(window.__aladinStub && window.__aladinStub.instances && window.__aladinStub.instances.length)",
        timeout=timeout_ms)


def _reveal(run, pane):
    run.ev("(p) => window.__pk.app.layout.reveal(p)", pane)
    run.ev("async (p) => { await window.__pk.app.layout.whenMounted(p); }", pane)


@pytest.mark.parametrize("page,pane", SKY_PAGES)
def test_cdn_served_by_stub(e2e, request, portal, open_page, page, pane):
    e2e.xfail_unless_panes(request, [pane])
    run = open_page(portal.url_path(page))
    _reveal(run, pane)
    _wait_instance(run)
    assert any(u.startswith(e2e.CDN) for u in run.aladin_served), run.aladin_served
    assert not any(u.startswith(e2e.MIRROR) for u in run.external)
    assert len(run.aladin_calls()) > 0
    assert e2e.ext_origins(run.external) <= e2e.ALLOWED_EXTERNAL, run.external
    run.wait_panes_settled()
    assert run.errors() == []


@pytest.mark.parametrize("page,pane", SKY_PAGES)
def test_mirror_used_when_cdn_blocked(e2e, request, portal, open_page, page, pane):
    e2e.xfail_unless_panes(request, [pane])
    run = open_page(portal.url_path(page), block_cdn=True)
    _reveal(run, pane)
    _wait_instance(run)
    assert any(u.startswith(e2e.CDN) for u in run.external), "the CDN was never tried"
    assert any(u.startswith(e2e.MIRROR) for u in run.aladin_served), run.aladin_served
    assert not any(u.startswith(e2e.CDN) for u in run.aladin_served)
    assert e2e.ext_origins(run.external) <= e2e.ALLOWED_EXTERNAL, run.external
    run.wait_panes_settled()
    # the aborted CDN request is expected; it is the only console error allowed
    assert run.errors(allow=(e2e.ABORTED_LOAD,)) == []
    assert run.ev("() => document.querySelectorAll('.pk-sky-error').length") == 0


@pytest.mark.parametrize("page,pane", SKY_PAGES)
def test_both_blocked_shows_note(e2e, request, portal, open_page, page, pane):
    e2e.xfail_unless_panes(request, [pane])
    run = open_page(portal.url_path(page), block_cdn=True, block_mirror=True)
    _reveal(run, pane)
    run.page.wait_for_selector(f"section.pk-pane[data-pane-id='{pane}'] .pk-error-note", timeout=60000)
    assert run.aladin_served == []
    assert _instances(run) == 0
    tried = [u for u in run.external if u.startswith(e2e.CDN) or u.startswith(e2e.MIRROR)]
    assert {u.startswith(e2e.CDN) for u in tried} == {True, False}, tried
    assert e2e.ext_origins(run.external) <= e2e.ALLOWED_EXTERNAL, run.external
    notes = run.ev("() => window.__pk.app.notes")
    assert "sky_map_aladin" in notes
    # the cap stays listed (the resolver decides caps; runtime problems are notes)
    assert run.caps_attr() == sorted(portal.pages[page].caps)
    run.wait_panes_settled()
    others = {k: v for k, v in run.pane_states().items() if k != pane}
    assert all(v in (None, "ready") for v in others.values()), others
    errs = [e for e in run.errors(allow=(e2e.ABORTED_LOAD,)) if "aladin" not in e.lower()]
    assert errs == []
    if page == "explorer":
        ids = [ms.syn_id(i) for i in (1, 2, 3)]
        assert run.ev("async (ids) => window.__pk.select('src', ids)", ids) == 3


def test_stub_records_markers_for_selection(e2e, request, portal, open_page):
    e2e.xfail_unless_panes(request, ["sky"])
    run = open_page(portal.url_path("explorer"))
    _reveal(run, "sky")
    _wait_instance(run)
    run.wait_panes_settled()
    before = len(run.aladin_calls())
    run.ev("async (ids) => window.__pk.select('src', ids)", [ms.syn_id(i) for i in range(10)])
    run.page.wait_for_timeout(1000)
    assert len(run.aladin_calls()) > before
    summary = run.ev("() => window.__aladinStub.summary(0)")
    assert summary["catalogs"], "no catalog layers on the sky map"
    assert run.errors() == []
