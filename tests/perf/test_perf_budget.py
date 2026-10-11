"""Level 3 performance budgets (ARCHITECTURE.md section 13).

Synthetic explorer page at N = 50k, 1M and 5M (5M is ``slow``), headless Chromium with
SwiftShader against the DevServer:

    N     first paint   brush commit   settle after pan   bytes before first paint
    50k   <= 3 s        <= 300 ms      <= 500 ms          <= 2 MB
    1M    <= 10 s       <= 1.5 s       <= 2 s             <= 2 MB
    5M    <= 25 s       <= 6 s         <= 6 s             <= 6 MB

* first paint: wall time from navigation to ``__pk.ready`` plus the visible panes leaving the
  ``loading`` state;
* brush commit: in-page time of ``__pk.brush('scatter', rect)`` (worker brush + selection
  apply + ``brush:commit``);
* settle after pan: time from a wheel event on the scatter canvas until ``__pk.stats()`` stops
  changing for ``QUIET_MS`` (the quiet window is not counted);
* bytes before first paint: response bytes the DevServer logged up to first paint.

Every measurement is written to ``$PK_PERF_REPORT`` (default ``<synth root>/perf_report.json``)
before the budgets are asserted, so a failing run still leaves its numbers behind.
"""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import sys
import time
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.perf


def _load_harness():
    """The browser/devserver fixtures live in tests/e2e/conftest.py; tests/perf has no
    conftest of its own (tests/conftest.py belongs to WP00), so the module is loaded under a
    private name and its fixtures are re-exported here.

    The session resources behind those fixtures (sync Playwright, Chromium, DevServer, fixture
    root) sit in the harness's process-wide registry, so when e2e and perf tests run in one
    pytest session both copies of the fixtures share one Playwright instance (a second
    ``sync_playwright().start()`` in the same process fails)."""
    name = "_pk_e2e_harness"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().parents[1] / "e2e" / "conftest.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_h = _load_harness()
synth_root = _h.synth_root
synth = _h.synth
server = _h.server
_playwright = _h._playwright
browser = _h.browser
open_page = _h.open_page
e2e = _h.e2e

QUIET_MS = 200
#: multiplier on the time budgets (not the byte budgets) for hosts slower than the reference
#: machine, e.g. PK_PERF_SCALE=3 on a 2-CPU SwiftShader runner. Default 1 = section 13 table.
TIME_SCALE = float(os.environ.get("PK_PERF_SCALE", "1") or 1)
BUDGETS = {
    50000: {"first_paint_s": 3.0, "brush_commit_s": 0.3, "pan_settle_s": 0.5, "bytes": 2 * 1024 ** 2},
    1000000: {"first_paint_s": 10.0, "brush_commit_s": 1.5, "pan_settle_s": 2.0, "bytes": 2 * 1024 ** 2},
    5000000: {"first_paint_s": 25.0, "brush_commit_s": 6.0, "pan_settle_s": 6.0, "bytes": 6 * 1024 ** 2},
}

#: Waits until ``__pk.stats()`` has not changed for ``quiet`` ms and no visible pane is
#: loading. Returns {settled, timedOut, end} (performance.now() values): ``settled`` is the
#: later of the last stats change and the last frame on which a pane was still loading. With
#: ``timedOut`` the view never settled within ``limit`` and ``settled`` is not meaningful.
SETTLE_JS = """async ([quiet, limit]) => {
  const t0 = performance.now();
  let last = JSON.stringify(window.__pk.stats());
  let lastChange = t0;
  let lastBusy = t0;
  const busy = () => Array.from(document.querySelectorAll('section.pk-pane'))
    .some((s) => s.offsetParent !== null && s.dataset.state === 'loading');
  let timedOut = false;
  for (;;) {
    const now = performance.now();
    if (busy()) lastBusy = now;
    if (now - Math.max(lastChange, lastBusy) >= quiet) break;
    if (now - t0 > limit) { timedOut = true; break; }
    await new Promise((r) => requestAnimationFrame(r));
    const s = JSON.stringify(window.__pk.stats());
    if (s !== last) { last = s; lastChange = performance.now(); }
  }
  return {settled: Math.max(lastChange, lastBusy), timedOut, end: performance.now()};
}"""
SETTLE_LIMIT_MS = 60000


def _report_path(synth_root: Path) -> Path:
    env = os.environ.get("PK_PERF_REPORT")
    return Path(env) if env else Path(synth_root) / "perf_report.json"


def _write_report(path: Path, key: str, entry: dict) -> None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        doc = {}
    except json.JSONDecodeError:
        doc = {}
    doc.setdefault("runs", {})[key] = entry
    doc["host"] = {"node": platform.node(), "cpus": os.cpu_count(), "python": platform.python_version()}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")


@pytest.mark.parametrize("n", [50000, 1000000, pytest.param(5000000, marks=pytest.mark.slow)])
def test_perf_budget(e2e, request, synth, synth_root, server, open_page, n):
    portal = synth(n)
    budget = BUDGETS[n]
    e2e.xfail_unless_panes(request, e2e.page_pane_types(portal, "explorer"))

    server.clear_requests()
    t0 = time.perf_counter()
    run = open_page(portal.url_path("explorer"), wait=False)
    status = run.wait_ready()
    assert status == "ok", status
    t_ready = time.perf_counter() - t0
    run.wait_panes_settled(timeout_ms=120000)
    first_paint = time.perf_counter() - t0
    nbytes = sum(int(getattr(r, "bytes", 0) or 0) for r in list(server.requests))
    nreq = len(server.requests)

    # brush commit on the scatter pane (a central rectangle in data space)
    d = portal.data
    x, y = d.expr(e2e.ms.CMD_X), d.expr(e2e.ms.CMD_Y)
    fin = np.isfinite(x) & np.isfinite(y)
    qx = np.percentile(x[fin], [30, 70])
    qy = np.percentile(y[fin], [30, 70])
    shape = {"kind": "rect", "x0": float(qx[0]), "x1": float(qx[1]), "y0": float(qy[0]), "y1": float(qy[1])}
    run.ev("(p) => window.__pk.app.layout.reveal(p)", "scatter")
    brush = run.ev("""async (s) => {
        const t = performance.now();
        const count = await window.__pk.brush('scatter', s, 'replace');
        return {ms: performance.now() - t, count};
    }""", shape)
    want = int((fin & (x >= qx[0]) & (x <= qx[1]) & (y >= qy[0]) & (y <= qy[1])).sum())
    # rows on or next to an edge may fall either way (u16q decode, float32 transforms)
    edge = 2e-3
    near = fin & (np.minimum.reduce([np.abs(x - qx[0]), np.abs(x - qx[1]), np.abs(y - qy[0]),
                                     np.abs(y - qy[1])]) < edge)
    n_edge = int(near.sum())
    after_brush = run.ev(SETTLE_JS, [QUIET_MS, SETTLE_LIMIT_MS])

    # settle after a pan/zoom of the scatter view
    box = run.page.locator("section.pk-pane[data-pane-id='scatter'] .pk-pane-body canvas").last.bounding_box()
    t_pan0 = run.ev("() => performance.now()")
    run.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    run.page.mouse.wheel(0, -240)
    settle = run.ev(SETTLE_JS, [QUIET_MS, SETTLE_LIMIT_MS])
    # a view that never settled is charged the whole wait
    t_last = settle["end"] if settle["timedOut"] else settle["settled"]
    pan_settle = max(0.0, (t_last - t_pan0) / 1000.0)

    stats = run.ev("() => window.__pk.stats()")
    tiers = run.ev("() => window.__pk.tiers()")
    entry = {
        "n": n, "ready_s": round(t_ready, 3), "first_paint_s": round(first_paint, 3),
        "brush_commit_s": round(brush["ms"] / 1000.0, 4), "brush_count": brush["count"], "brush_numpy": want, "brush_edge_rows": n_edge,
        "pan_settle_s": round(pan_settle, 4), "pan_settle_timed_out": settle["timedOut"],
        "brush_settle_timed_out": after_brush["timedOut"], "bytes_before_first_paint": nbytes, "requests": nreq,
        "stats": stats, "tiers": tiers, "budget": budget, "time_scale": TIME_SCALE, "runtime": run.ev("() => window.__pk.app.runtime"),
    }
    _write_report(_report_path(synth_root), f"explorer@{n}", entry)

    assert abs(brush["count"] - want) <= n_edge, (brush["count"], want, n_edge)
    assert not after_brush["timedOut"], f"view did not settle within {SETTLE_LIMIT_MS} ms after the brush"
    assert not settle["timedOut"], f"view did not settle within {SETTLE_LIMIT_MS} ms after the pan"
    assert run.errors() == []
    assert nbytes <= budget["bytes"], f"{nbytes} bytes before first paint"
    k = TIME_SCALE
    assert first_paint <= k * budget["first_paint_s"], f"first paint {first_paint:.2f} s (scale {k})"
    assert brush["ms"] / 1000.0 <= k * budget["brush_commit_s"], f"brush commit {brush['ms']:.0f} ms (scale {k})"
    assert pan_settle <= k * budget["pan_settle_s"], f"settle after pan {pan_settle:.2f} s (scale {k})"


def test_first_paint_bytes_ledger(synth):
    """The build-time ledger already keeps the 50k and 1M first-paint bytes under 2 MB."""
    for n in (50000, 1000000):
        portal = synth(n)
        assert 0 < portal.first_paint_bytes <= BUDGETS[n]["bytes"], (n, portal.first_paint_bytes)
