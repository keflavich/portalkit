"""Level 2 capability matrix (ARCHITECTURE.md section 13).

``test_capability`` is parametrized over the enabled (page x capability) pairs of the N = 2000
synthetic portal (collected from caps.resolve; ``test_matrix_pairs_match_built_portal`` checks
them against the built pages). Each case checks ``data-caps``, mounts the capability's provider
panes and runs a behaviour check for the capability on one shared page per archetype.

Brushes are checked against numpy masks of the synthetic catalog (input-order arrays from
``make_synthetic``), mapped to stored rows through the catalog ids. Rows within ``EDGE`` of a
shape boundary are left out of the comparison (decoded u16q values and float32 transforms).
Pointer drags are first mapped to data space in Python (overlay canvas box, makeView margins,
pinned axis domains) and compared with the shape the pane commits.

The worker-level brush tests (``client.brush`` + ``Selection.apply``), the persistence tests,
CSV export and the live-SED twin run against the core runtime. Pointer brushes run on the
``cmd`` page (an uncollapsed scatter pane); the ``hist`` page carries two histogram panes.

Rendering changes are detected with ``PageRun.canvas_hash`` (a hash of ``canvas.toDataURL()``
for every canvas of a pane) in place of screenshots, which SwiftShader reads back slowly.

The checks for IMPLEMENTATION_NOTES items 81 (page-relative PNG URLs; detail product links
relative to the records index, on the ``cmd`` page), 88 (default overlays
and manifold dims), 89 (caps that need a dataset) and 93 (``app.importCsv``) assert the noted
behaviour directly.
"""

from __future__ import annotations

import math
import re

import numpy as np
import pytest

import make_synthetic as ms
from portalkit import caps as capsmod

pytestmark = pytest.mark.e2e
PWTimeout = pytest.importorskip("playwright.sync_api").TimeoutError

N = 2000
EDGE = 2e-3
OPS = ("replace", "add", "subtract", "intersect")
MODIFIERS = {"replace": [], "add": ["Shift"], "subtract": ["Alt"], "intersect": ["Shift", "Alt"]}


# ---------------------------------------------------------------------------
# numpy reference
# ---------------------------------------------------------------------------

def _xy(portal, x="f182m-f410m", y="f410m"):
    d = portal.data
    return d.expr(x), d.expr(y)


def _stored_perm(portal):
    """input row of each stored row."""
    return np.array([portal.input_row(s) for s in portal.stored_ids], dtype=np.int64)


def shape_hit(shape, x, y):
    """(hit, near_edge) for a data-space shape (linear axes)."""
    kind = shape["kind"]
    fin = np.isfinite(x) & np.isfinite(y)
    if kind == "rect":
        x0, x1 = sorted((shape["x0"], shape["x1"]))
        y0, y1 = sorted((shape["y0"], shape["y1"]))
        hit = fin & (x >= x0) & (x <= x1) & (y >= y0) & (y <= y1)
        dist = np.minimum.reduce([np.abs(x - x0), np.abs(x - x1), np.abs(y - y0), np.abs(y - y1)])
        near = fin & (dist < EDGE)
    elif kind == "circle":
        rx = abs(shape["r"] if "r" in shape else shape["rx"])
        ry = abs(shape.get("ry", rx))
        q = ((x - shape["cx"]) / rx) ** 2 + ((y - shape["cy"]) / ry) ** 2
        hit = fin & (q <= 1.0)
        near = fin & (np.abs(np.sqrt(np.where(fin, q, 0)) - 1.0) * min(rx, ry) < EDGE)
    elif kind == "lasso":
        pts = np.asarray(shape["pts"], dtype=float)
        hit = np.zeros(x.shape, dtype=bool)
        m = len(pts)
        for a in range(m):
            b = (a - 1) % m
            xa, ya = pts[a]
            xb, yb = pts[b]
            cond = (ya > y) != (yb > y)
            with np.errstate(divide="ignore", invalid="ignore"):
                xc = (xb - xa) * (y - ya) / (yb - ya) + xa
            hit ^= cond & (x < xc)
        hit &= fin
        near = np.zeros(x.shape, dtype=bool)
        for a in range(m):
            p0, p1 = pts[a - 1], pts[a]
            d = p1 - p0
            L2 = float(d @ d) or 1e-30
            t = np.clip(((x - p0[0]) * d[0] + (y - p0[1]) * d[1]) / L2, 0, 1)
            dist = np.hypot(x - (p0[0] + t * d[0]), y - (p0[1] + t * d[1]))
            near |= fin & (dist < EDGE)
    else:
        raise ValueError(kind)
    return hit, near


def apply_op(sel, hit, op, drawable):
    if op == "replace":
        return hit & drawable
    if op == "add":
        return sel | (hit & drawable)
    if op == "subtract":
        return sel & ~(hit & drawable)
    if op == "intersect":
        return (sel & hit) | (sel & ~drawable)
    raise ValueError(op)


def shapes_for(x, y):
    fin = np.isfinite(x) & np.isfinite(y)
    xs, ys = x[fin], y[fin]
    qx = np.percentile(xs, [20, 35, 50, 65, 80])
    qy = np.percentile(ys, [20, 35, 50, 65, 80])
    base = {"kind": "rect", "x0": float(qx[0]), "x1": float(qx[3]), "y0": float(qy[0]), "y1": float(qy[3])}
    rect = {"kind": "rect", "x0": float(qx[1]), "x1": float(qx[4]), "y0": float(qy[1]), "y1": float(qy[4])}
    circle = {"kind": "circle", "cx": float(qx[2]), "cy": float(qy[2]),
              "r": float(qx[3] - qx[1]), "ry": float(qy[3] - qy[1])}
    lasso = {"kind": "lasso", "pts": [[float(qx[1]), float(qy[0])], [float(qx[4]), float(qy[2])],
                                      [float(qx[2]), float(qy[4])], [float(qx[0]), float(qy[3])]]}
    return base, {"rect": rect, "circle": circle, "lasso": lasso}


def compare(portal, stored_idx, steps, x, y):
    perm = _stored_perm(portal)
    xs, ys = x[perm], y[perm]
    drawable = np.isfinite(xs) & np.isfinite(ys)
    sel = np.zeros(len(perm), dtype=bool)
    near = np.zeros(len(perm), dtype=bool)
    for shape, op in steps:
        hit, nr = shape_hit(shape, xs, ys)
        sel = apply_op(sel, hit, op, drawable)
        near |= nr
    got = np.zeros(len(perm), dtype=bool)
    got[np.asarray(stored_idx, dtype=np.int64)] = True
    bad = (got != sel) & ~near
    return int(bad.sum()), int(sel.sum()), int(got.sum())


WORKER_BRUSH_JS = """
async ({x, y, steps}) => {
  const app = window.__pk.app;
  const c = await app.catalog('src');
  const sel = await app.selectionFor('src');
  const view = {pane: 'scatter', kind: 'xy', x, y, xlog: false, ylog: false};
  for (const st of steps) {
    const res = await c.brush(view, st.shape);
    const {kind, ...geom} = st.shape;
    sel.apply(res.hit, st.op, res.drawable, {op: st.op, kind, view: {ds: 'src', x, y}, shape: geom});
  }
  return {idx: Array.from(sel.indices()), count: sel.count};
}
"""


@pytest.fixture(scope="module")
def p2k(synth):
    return synth(N)


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("kind", ("rect", "circle", "lasso"))
def test_worker_brush_matches_numpy(p2k, open_page, kind, op):
    x, y = _xy(p2k)
    base, shapes = shapes_for(x, y)
    steps = [(base, "replace"), (shapes[kind], op)]
    run = open_page(p2k.url_path("explorer"))
    out = run.ev(WORKER_BRUSH_JS, {"x": "f182m-f410m", "y": "f410m",
                                   "steps": [{"shape": s, "op": o} for s, o in steps]})
    bad, want, got = compare(p2k, out["idx"], steps, x, y)
    assert out["count"] == got
    assert want > 0
    assert bad == 0, f"{bad} rows differ ({kind}/{op}: numpy {want}, runtime {got})"


def test_hook_brush_uses_pane_axes(e2e, request, p2k, open_page):
    """``__pk.brush('scatter', shape)`` uses the scatter axes and emits brush:commit."""
    e2e.xfail_unless_panes(request, ["scatter"])
    x, y = _xy(p2k)
    base, _ = shapes_for(x, y)
    run = open_page(p2k.url_path(BRUSH_PAGE))
    run.ev("() => { window.__commits = []; window.__pk.store.onEvent('brush:commit', (p) => window.__commits.push(p)); }")
    run.ev("() => window.__pk.app.layout.reveal('scatter')")
    count = run.ev("async (s) => window.__pk.brush('scatter', s, 'replace')", base)
    idx = run.ev("async () => Array.from((await window.__pk.app.selectionFor('src')).indices())")
    bad, want, got = compare(p2k, idx, [(base, "replace")], x, y)
    assert count == got and bad == 0
    assert run.ev("() => window.__commits.length") >= 1


# ---------------------------------------------------------------------------
# histogram range brushes (hist page)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pane,expr,bins", ms.HIST_PANES, ids=[h[0] for h in ms.HIST_PANES])
def test_hist_range_brush_matches_numpy(e2e, request, p2k, open_page, pane, expr, bins):
    """A range brush on a histogram pane filters the catalog to the rows whose value lies in
    the range (numpy count of finite values), the pane counts every finite value, and
    clearing the range restores the full visible set."""
    e2e.xfail_unless_panes(request, ["hist"])
    run = open_page(p2k.url_path("hist"))
    assert mount(run, pane) == "ready"
    v = p2k.data.expr(expr)
    fin = np.isfinite(v)
    st = run.pane_stats(pane)
    assert st["expr"] == expr, st
    if bins:
        assert st["bins"] == bins, st
    run.page.wait_for_function(
        "(p) => { const s = window.__pk.app.layout.pane(p).stats(); return s && s.total > 0; }", arg=pane, timeout=30000)
    assert run.pane_stats(pane)["total"] == int(fin.sum())
    # bounds half a storage step (0.001 mag) off the data grid, so no row sits on an edge
    q = np.percentile(v[fin], [25, 70])
    lo, hi = math.floor(q[0] * 1000) / 1000 + 5e-4, math.floor(q[1] * 1000) / 1000 + 5e-4
    want = int((fin & (v >= lo) & (v <= hi)).sum())
    assert 0 < want < fin.sum()
    got = run.ev("async ([p, lo, hi]) => window.__pk.app.layout.pane(p).brush({lo, hi})", [pane, lo, hi])
    assert got == want, (lo, hi, got, want)
    rng = (run.store_get("filters:src") or {}).get("ranges", {}).get(expr)
    assert rng == pytest.approx([lo, hi]), rng
    run.page.wait_for_function(
        "([p, n]) => { const s = window.__pk.app.layout.pane(p).stats(); return s && s.shown === n; }",
        arg=[pane, want], timeout=30000)
    run.ev("async (p) => window.__pk.app.layout.pane(p).brush(null)", pane)
    # vis:src count null means "no filter: every row visible"
    run.page.wait_for_function(
        "(n) => { const c = (window.__pk.store.get('vis:src') || {}).count; return c === null || c === undefined || c === n; }",
        arg=p2k.n, timeout=30000)
    run.page.wait_for_function(
        "([p, n]) => { const s = window.__pk.app.layout.pane(p).stats(); return s && s.shown === n; }",
        arg=[pane, int(fin.sum())], timeout=30000)
    assert run.pane_stats(pane)["range"] is None
    assert run.errors() == []


# ---------------------------------------------------------------------------
# pointer brushes on the scatter pane
# ---------------------------------------------------------------------------

def _scatter_box(run, pane="scatter"):
    run.ev("(p) => window.__pk.app.layout.reveal(p)", pane)
    run.ev("async (p) => { await window.__pk.app.layout.whenMounted(p); }", pane)
    run.page.wait_for_function(
        "(p) => { const s = document.querySelector(`section.pk-pane[data-pane-id='${p}']`); return s && s.dataset.state === 'ready'; }",
        arg=pane, timeout=60000)
    run.page.wait_for_timeout(300)
    el = run.page.locator(f"section.pk-pane[data-pane-id='{pane}'] .pk-pane-body canvas").last
    return el.bounding_box()


def _set_mode(run, pane, mode):
    if mode == "rect":
        return
    found = run.ev(
        """([p, mode]) => {
             const sec = document.querySelector(`section.pk-pane[data-pane-id='${p}']`);
             const btns = Array.from(sec.querySelectorAll('button, [role=button], [data-mode]'));
             const b = btns.find((e) => (e.dataset.mode === mode) ||
               ((e.getAttribute('aria-label') || e.title || e.textContent || '').toLowerCase().includes(mode)));
             if (!b) return false;
             b.click();
             return true;
           }""", [pane, mode])
    assert found, f"no '{mode}' brush tool in the {pane} pane"


#: page whose scatter pane is uncollapsed at load (make_synthetic ``cmd``)
BRUSH_PAGE = "cmd"
#: plot margins of plot/axes.js makeView (DEFAULT_MARGIN), used by the scatter pane when its
#: stats() carry no view geometry
PLOT_MARGIN = {"l": 52, "r": 12, "t": 10, "b": 38}


def plot_margin(stats):
    """Plot margins (CSS px) from the scatter pane's stats() view geometry when it reports
    them (``margin`` / ``margins`` as {l, r, t, b} or {left, right, top, bottom}, possibly
    under ``view``), else :data:`PLOT_MARGIN`."""
    st = stats or {}
    for src in (st, st.get("view") if isinstance(st.get("view"), dict) else {}):
        for key in ("margin", "margins"):
            m = src.get(key) if isinstance(src, dict) else None
            if isinstance(m, dict):
                vals = {k: m.get(k, m.get(n)) for k, n in (("l", "left"), ("r", "right"), ("t", "top"), ("b", "bottom"))}
                if all(isinstance(v, (int, float)) for v in vals.values()):
                    return vals
    return dict(PLOT_MARGIN)
#: tolerance of the screen -> data check, in CSS px
PX_TOL = 2.0

PIN_AXES_JS = """async ([x, y, xr, yr]) => {
  const app = window.__pk.app;
  app.store.set({'axes:scatter': {x, y, xlog: false, ylog: false, xinv: false, yinv: false, xr, yr}},
                {source: 'test'});
  const c = await app.catalog('src');
  const inv = (e) => { const m = c.col(e); if (!m) return false;
                       if (m.invert !== undefined && m.invert !== null) return !!m.invert;
                       return m.role === 'mag'; };
  await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  return {xinv: inv(x), yinv: inv(y)};
}"""


class ScreenMap:
    """Screen (page CSS px) -> data mapping of the scatter pane for pinned linear axes, rebuilt
    in Python from the overlay canvas box, the makeView margins and the axis domains."""

    def __init__(self, box, xr, yr, xinv, yinv, margin=None):
        self.box = box
        mg = margin or PLOT_MARGIN
        w, h = math.floor(box["width"]), math.floor(box["height"])
        self.kx, self.ky = w / box["width"], h / box["height"]   # brushui.eventToViewPx
        left, right = mg["l"], w - mg["r"]
        top, bottom = mg["t"], h - mg["b"]
        self.plot = (left, top, right, bottom)
        px0, px1 = (right, left) if xinv else (left, right)
        py0, py1 = (top, bottom) if yinv else (bottom, top)
        self.bx = (px1 - px0) / (xr[1] - xr[0])
        self.by = (py1 - py0) / (yr[1] - yr[0])
        self.ax = px0 - self.bx * xr[0]
        self.ay = py0 - self.by * yr[0]

    def view_px(self, sx, sy):
        return (sx - self.box["x"]) * self.kx, (sy - self.box["y"]) * self.ky

    def to_data(self, sx, sy):
        vx, vy = self.view_px(sx, sy)
        return (vx - self.ax) / self.bx, (vy - self.ay) / self.by

    def to_px(self, x, y):
        return self.ax + self.bx * x, self.ay + self.by * y

    def screen(self, fx, fy):
        """Page coordinates of plot fractions (fx, fy) in [0, 1]."""
        left, top, right, bottom = self.plot
        vx, vy = left + fx * (right - left), top + fy * (bottom - top)
        return self.box["x"] + vx / self.kx, self.box["y"] + vy / self.ky


def _pointer_paths(kind):
    """Drag paths in plot fractions.  ``base`` is the initial replace rect; it overlaps every
    second shape only partially so add/subtract/intersect all leave a non-empty selection."""
    if kind == "base":
        return [(0.2, 0.25), (0.45, 0.5), (0.62, 0.66)]
    if kind == "rect":
        return [(0.3, 0.3), (0.5, 0.5), (0.7, 0.72)]
    if kind == "circle":
        return [(0.5, 0.5), (0.6, 0.55), (0.68, 0.6)]
    return [(0.3, 0.3)] + [(0.5 + 0.22 * math.cos(t), 0.5 + 0.22 * math.sin(t))
                           for t in np.linspace(-2.3, 3.6, 24)] + [(0.3, 0.32)]


def _drag_plot(run, smap, kind, mods):
    """Drag along the plot-fraction path of ``kind``; returns the screen points visited."""
    page = run.page
    pts = [smap.screen(fx, fy) for fx, fy in _pointer_paths(kind)]
    for m in mods:
        page.keyboard.down(m)
    page.mouse.move(*pts[0])
    page.mouse.down()
    for p in pts[1:]:
        page.mouse.move(*p, steps=3)
    page.mouse.up()
    for m in reversed(mods):
        page.keyboard.up(m)
    return pts


def _seg_dist(p, a, b):
    d = np.subtract(b, a)
    L2 = float(d @ d) or 1e-30
    t = min(1.0, max(0.0, float(np.subtract(p, a) @ d) / L2))
    return float(np.hypot(*(np.asarray(a) + t * d - np.asarray(p))))


def assert_shape_matches_drag(shape, kind, smap, pts):
    """The committed data-space shape equals the drag path mapped through the axes (within
    ``PX_TOL`` CSS px)."""
    vpts = [smap.view_px(*p) for p in pts]
    tx, ty = PX_TOL / abs(smap.bx), PX_TOL / abs(smap.by)
    if kind == "rect":
        (x0, y0), (x1, y1) = smap.to_data(*pts[0]), smap.to_data(*pts[-1])
        assert shape["x0"] == pytest.approx(min(x0, x1), abs=tx)
        assert shape["x1"] == pytest.approx(max(x0, x1), abs=tx)
        assert shape["y0"] == pytest.approx(min(y0, y1), abs=ty)
        assert shape["y1"] == pytest.approx(max(y0, y1), abs=ty)
    elif kind == "circle":
        cx, cy = smap.to_data(*pts[0])
        rpx = math.hypot(vpts[-1][0] - vpts[0][0], vpts[-1][1] - vpts[0][1])
        assert shape["cx"] == pytest.approx(cx, abs=tx)
        assert shape["cy"] == pytest.approx(cy, abs=ty)
        rx = shape.get("rx", shape.get("r"))
        assert abs(rx) * abs(smap.bx) == pytest.approx(rpx, abs=PX_TOL)
        assert abs(shape.get("ry", rx)) * abs(smap.by) == pytest.approx(rpx, abs=PX_TOL)
    else:
        lp = [smap.to_px(x, y) for x, y in shape["pts"]]
        assert len(lp) >= 3
        assert math.dist(lp[0], vpts[0]) <= PX_TOL
        assert math.dist(lp[-1], vpts[-1]) <= PX_TOL + 2   # LASSO_STEP_PX sampling
        worst = max(min(_seg_dist(p, vpts[k], vpts[k + 1]) for k in range(len(vpts) - 1)) for p in lp)
        assert worst <= PX_TOL, f"lasso point {worst:.2f} px off the drag path"


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("kind", ("rect", "circle", "lasso"))
def test_pointer_brush_matches_numpy(e2e, request, p2k, open_page, kind, op):
    """Pointer drags on the scatter pane: the committed shapes equal the drag path mapped to
    data space independently (overlay canvas box + makeView margins + pinned axis domains),
    and the selection equals the numpy mask of those shapes."""
    e2e.xfail_unless_panes(request, ["scatter"])
    run = open_page(p2k.url_path(BRUSH_PAGE))
    run.ev("() => { window.__commits = []; window.__pk.store.onEvent('brush:commit', (p) => window.__commits.push(JSON.parse(JSON.stringify(p)))); }")
    _scatter_box(run)
    x, y = _xy(p2k)
    fin = np.isfinite(x) & np.isfinite(y)
    qx, qy = np.percentile(x[fin], [1, 99]), np.percentile(y[fin], [1, 99])
    xr = [float(qx[0]) - 0.1, float(qx[1]) + 0.1]
    yr = [float(qy[0]) - 0.1, float(qy[1]) + 0.1]
    inv = run.ev(PIN_AXES_JS, [ms.CMD_X, ms.CMD_Y, xr, yr])
    run.page.wait_for_timeout(300)
    box = run.page.locator("section.pk-pane[data-pane-id='scatter'] .pk-plot-over").first.bounding_box()
    smap = ScreenMap(box, xr, yr, inv["xinv"], inv["yinv"], margin=plot_margin(run.pane_stats("scatter")))
    # first a plain rect (replace), then the shape under test with the modifier
    p_rect = _drag_plot(run, smap, "base", [])
    run.page.wait_for_function("() => window.__commits.length >= 1", timeout=20000)
    _set_mode(run, "scatter", kind)
    p_kind = _drag_plot(run, smap, kind, MODIFIERS[op])
    run.page.wait_for_function("() => window.__commits.length >= 2", timeout=20000)
    commits = run.ev("() => window.__commits")
    assert [c["op"] for c in commits[:2]] == ["replace", op]
    steps = [(dict(c["shape"]), c["op"]) for c in commits[:2]]
    for s, _ in steps:
        assert not s.get("xlog") and not s.get("ylog")
    assert steps[0][0]["kind"] == "rect" and steps[1][0]["kind"] == kind
    assert_shape_matches_drag(steps[0][0], "rect", smap, p_rect)
    assert_shape_matches_drag(steps[1][0], kind, smap, p_kind)
    idx = run.ev("async () => Array.from((await window.__pk.app.selectionFor('src')).indices())")
    bad, want, got = compare(p2k, idx, steps, x, y)
    assert want > 0
    assert bad == 0, f"{bad} rows differ ({kind}/{op}: numpy {want}, runtime {got})"


# ---------------------------------------------------------------------------
# linked selection, persistence, export
# ---------------------------------------------------------------------------

def test_linked_selection_reaches_list_sky_manifold(e2e, request, p2k, open_page):
    e2e.xfail_unless_panes(request, ["list", "sky", "manifold", "scatter"])
    run = open_page(p2k.url_path("manifold"))
    for p in ("list", "sky", "manifold", "scatter"):
        run.ev("(p) => window.__pk.app.layout.reveal(p)", p)
        run.ev("async (p) => { await window.__pk.app.layout.whenMounted(p); }", p)
    run.wait_panes_settled()
    before = len(run.aladin_calls())
    ids = [ms.syn_id(i) for i in (1, 2, 3, 5, 8)]
    count = run.ev("async (ids) => window.__pk.select('src', ids)", ids)
    assert count == len(ids)
    run.page.wait_for_timeout(800)
    assert run.store_get("sel:src")["count"] == len(ids)
    # list: selected rows are marked; sky: the stub saw new marker calls and the marker layer
    # counts the selection; manifold and scatter: the pane state counts the selection
    marked = run.ev("() => document.querySelectorAll(\"section.pk-pane[data-pane-id='list'] [aria-selected='true'], section.pk-pane[data-pane-id='list'] .pk-selected\").length")
    assert marked >= 1
    assert len(run.aladin_calls()) > before
    run.page.wait_for_function(
        """(n) => { const s = window.__pk.app.layout.pane('manifold').stats(); return s && s.selected === n; }""",
        arg=len(ids), timeout=20000)
    man = run.pane_stats("manifold")
    assert man["selected"] == len(ids), man
    assert run.pane_stats("scatter")["selected"] == len(ids)
    sky = run.pane_stats("sky")
    assert sky and sky["markers"] is not None, sky
    assert sky["markers"]["selected"] == len(ids), sky["markers"]
    assert run.errors() == []


def test_theme_persists(open_page, p2k):
    run = open_page(p2k.url_path("explorer"))
    run.ev("async () => { const t = await import(new URL(window.__pk.app.page.assets + 'js/core/theme.js', location.href).href); t.set('dark'); }")
    assert run.ev("() => document.documentElement.dataset.theme") == "dark"
    run.page.reload()
    run.wait_ready()
    assert run.ev("() => localStorage.getItem('pk:theme')") == "dark"
    assert run.ev("() => document.documentElement.dataset.theme") == "dark"


def test_layout_persists(open_page, p2k):
    run = open_page(p2k.url_path("explorer"))
    assert run.ev("() => window.__pk.app.layout.info('detail').collapsed") is False
    run.ev("() => window.__pk.app.layout.collapse('detail', true)")
    run.page.wait_for_timeout(400)
    key = run.ev("() => Object.keys(localStorage).find((k) => k.endsWith(':explorer:layout'))")
    assert key and key.startswith("pk:")
    run.page.reload()
    run.wait_ready()
    assert run.ev("() => window.__pk.app.layout.info('detail').collapsed") is True


def test_csv_export_of_selection(p2k, open_page):
    run = open_page(p2k.url_path("explorer"))
    ids = [ms.syn_id(i) for i in (0, 4, 7, 11)] + [ms.alias_of(13)]
    count = run.ev("async (ids) => window.__pk.select('src', ids)", ids)
    assert count == 5
    text = run.ev("async () => { const c = await window.__pk.app.catalog('src'); const b = await c.exportCSV({mask: 'sel'}); return await b.text(); }")
    import csv
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    rows = list(csv.DictReader(lines))
    idcol = list(rows[0].keys())[0]
    got = sorted(r[idcol] for r in rows)
    assert got == sorted([ms.syn_id(i) for i in (0, 4, 7, 11, 13)])
    # mags round-trip at the stored step
    d = p2k.data
    for r in rows:
        i = p2k.input_row(r[idcol])
        want = d.mags["f410m"][i]
        if not np.isfinite(want):
            assert r["f410m"] in ("", "nan", "NaN")
            continue
        assert abs(float(r["f410m"]) - want) <= 6e-4


def test_csv_import_roundtrip(p2k, open_page):
    """Export the selection as CSV, clear it, and import the text through ``app.importCsv``
    (IMPLEMENTATION_NOTES item 93, the runtime entry behind the import dialog)."""
    run = open_page(p2k.url_path("explorer"))
    ids = [ms.syn_id(i) for i in (2, 3, 9)]
    run.ev("async (ids) => window.__pk.select('src', ids)", ids)
    out = run.ev("""async () => {
        const app = window.__pk.app;
        if (typeof app.importCsv !== 'function') return {missingApi: true};
        const c = await app.catalog('src');
        const text = await (await c.exportCSV({mask: 'sel'})).text();
        const sel = await app.selectionFor('src');
        sel.clear();
        const cleared = sel.count;
        const res = await app.importCsv(text, {mode: 'replace'});
        const got = [];
        for (const i of sel.indices()) got.push(String(await app.idOf('src', i)));
        return {cleared, count: sel.count, got, res: JSON.parse(JSON.stringify(res))};
    }""")
    assert not out.get("missingApi"), "app.importCsv is missing (IMPLEMENTATION_NOTES item 93)"
    res = out["res"]
    missing = res["missing"] if isinstance(res["missing"], int) else len(res["missing"])
    assert out["cleared"] == 0
    assert res["mode"] == "replace" and res["ds"] == "src"
    assert res["count"] == 3 and out["count"] == 3, res
    assert missing == 0 and res["byId"] == 3 and res["matched"] == 3, res
    assert sorted(out["got"]) == sorted(ids)
    # an id-list import adds through the same entry
    more = run.ev("async (t) => JSON.parse(JSON.stringify(await window.__pk.app.importCsv(t, {mode: 'add'})))",
                  f"{ms.syn_id(20)}\n{ms.alias_of(21)}\n")
    assert more["count"] == 5 and more["byId"] == 2, more
    assert run.errors() == []


def test_keyboard_nav_walks_filtered_list(e2e, request, p2k, open_page):
    e2e.xfail_unless_panes(request, ["list"])
    run = open_page(p2k.url_path("explorer"))
    # cats lists the levels that are switched off
    off = [c for c in ms.CLASSES if c != "yso"]
    run.ev("(off) => window.__pk.store.set({'filters:src': {cats: {cls: off}}}, {source: 'test'})", off)
    run.page.wait_for_function("() => (window.__pk.store.get('vis:src') || {}).count > 0", timeout=20000)
    d = p2k.data
    yso = {ms.syn_id(i) for i in np.nonzero(d.cls == "yso")[0]}
    run.page.locator("body").click(position={"x": 5, "y": 5})
    seen = []
    for _ in range(6):
        run.page.keyboard.press("ArrowDown")
        run.page.wait_for_timeout(150)
        f = run.store_get("focus") or {}
        if f.get("id"):
            seen.append(f["id"])
    assert seen and all(s in yso for s in seen), seen
    assert len(set(seen)) >= 3


def test_lightbox_swipe_and_hash_clear(e2e, request, p2k, open_page):
    e2e.xfail_unless_panes(request, ["gallery"])
    run = open_page(p2k.url_path("gallery"), hash="lb=0")
    run.page.wait_for_selector(".pk-lightbox:not([hidden])", state="visible", timeout=30000)
    lb0 = run.store_get("lightbox")
    box = run.page.locator(".pk-lightbox:not([hidden])").first.bounding_box()
    assert box is not None
    y = box["y"] + box["height"] / 2
    run.page.mouse.move(box["x"] + box["width"] * 0.8, y)
    run.page.mouse.down()
    run.page.mouse.move(box["x"] + box["width"] * 0.2, y, steps=8)
    run.page.mouse.up()
    run.page.wait_for_timeout(400)
    assert run.store_get("lightbox") != lb0
    run.page.keyboard.press("Escape")
    run.page.wait_for_timeout(600)
    assert run.store_get("lightbox") is None
    assert "lb=" not in run.ev("() => location.hash")


TPL_STATS_JS = """() => { const h = window.__pk.app.layout.pane('spectrum1d');
  const s = h && h.stats ? h.stats() : null; return s && s.template ? JSON.parse(JSON.stringify(s.template)) : null; }"""


def test_av_moves_template_and_isochrones(e2e, request, p2k, open_page):
    """A_V reddens the template overlaid on the focused spectrum (spectrum1d stats().template:
    ``av`` follows the store and the fitted ``scale`` changes) and moves the isochrones /
    reddening vector on the scatter pane (its canvases change).

    Runs on the ``explorer`` page (resolver default layout), so the scatter overlays are the
    ones ``caps.default_overlays`` adds (IMPLEMENTATION_NOTES item 88), not the explicit list
    of the ``cmd`` page."""
    e2e.xfail_unless_panes(request, ["scatter", "spectrum1d"])
    # a template (G dwarf, medium grid) is overlaid on the focused spectrum
    run = open_page(p2k.url_path("explorer"), hash=f"id={ms.syn_id(1)}&tpl=gdwarf~medium")
    _scatter_box(run)
    _scatter_box(run, "spectrum1d")
    run.page.wait_for_function(
        """() => { const h = window.__pk.app.layout.pane('spectrum1d'); const s = h && h.stats ? h.stats() : null;
                   return !!(s && s.template && s.template.key === 'gdwarf' && Number.isFinite(s.template.scale)); }""",
        timeout=60000)
    t0 = run.ev(TPL_STATS_JS)
    assert t0["grid"] == "medium" and (t0["av"] or 0) == 0, t0
    sc = run.pane_stats("scatter")
    assert sc and sc.get("overlays", 0) > 0, f"scatter draws no overlays (IMPLEMENTATION_NOTES item 88): {sc}"
    h0 = run.wait_canvas_stable("scatter")
    run.ev("() => window.__pk.store.set({av: 30}, {source: 'test'})")
    run.page.wait_for_function(
        """() => { const h = window.__pk.app.layout.pane('spectrum1d'); const s = h && h.stats ? h.stats() : null;
                   return !!(s && s.template && s.template.av === 30 && Number.isFinite(s.template.scale)); }""",
        timeout=60000)
    t1 = run.ev(TPL_STATS_JS)
    assert not math.isclose(t1["scale"], t0["scale"], rel_tol=1e-3), (t0, t1)
    h1 = run.wait_canvas_change("scatter", h0)
    assert h1 != h0, "scatter (isochrones / reddening vector) did not change with A_V"
    assert run.errors() == []


def test_spectrum_zoom_autoscales_y_and_2d_xlink(e2e, request, p2k, open_page):
    e2e.xfail_unless_panes(request, ["spectrum1d", "spectrum2d"])
    """Zooming x (store ``spec.xr``) rescales y to the visible samples (spectrum1d stats:
    ``autoY``, ``yr``) and moves the 2D viewer's x range with it (spectrum2d stats ``xr``)."""
    run = open_page(p2k.url_path("spectra"), hash=f"id={ms.syn_id(2)}")
    _scatter_box(run, "spectrum1d")
    _scatter_box(run, "spectrum2d")
    spec = ms.spectrum_for(2)
    wave, flux = spec.wave, spec.channels["flux"]
    tops = {}
    for xr in ([2.0, 2.4], [4.0, 4.1]):
        run.ev("(xr) => { const s = window.__pk.store.get('spec') || {}; window.__pk.store.set({spec: {...s, xr}}, {source: 'test'}); }", xr)
        run.page.wait_for_function(
            """(xr) => { const h = window.__pk.app.layout.pane('spectrum1d');
                         const s = h && h.stats ? h.stats() : null;
                         return !!(s && s.xr && Math.abs(s.xr[0] - xr[0]) < 1e-6 && Math.abs(s.xr[1] - xr[1]) < 1e-6); }""",
            arg=xr, timeout=20000)
        st = run.pane_stats("spectrum1d")
        assert st is not None, "spectrum1d pane has no stats()"
        assert st["autoY"] is True
        assert not st.get("logy")
        inwin = (wave >= xr[0]) & (wave <= xr[1]) & np.isfinite(flux)
        lo, hi = float(flux[inwin].min()), float(flux[inwin].max())
        span = hi - lo
        tol = 0.01 * span + 1e-6     # stored precision of the flux channel
        yr = st["yr"]
        # autoY: [min(0, lo - pad), hi + pad] with an 8 % pad
        assert yr[0] <= min(0.0, lo) + tol, (xr, yr, lo, hi)
        assert hi - tol <= yr[1] <= hi + 0.2 * span + tol, (xr, yr, lo, hi)
        tops[tuple(xr)] = yr[1]
        if xr[1] <= 3.0:
            # x-link: the 2D viewer (1.66-3.07 um) follows the 1D range
            s2 = run.pane_stats("spectrum2d")
            assert s2 is not None, "spectrum2d pane has no stats()"
            run.page.wait_for_function(
                """(xr) => { const s = window.__pk.app.layout.pane('spectrum2d').stats();
                             return !!(s && s.xr && Math.abs(s.xr[0] - xr[0]) < 1e-3 && Math.abs(s.xr[1] - xr[1]) < 1e-3); }""",
                arg=xr, timeout=20000)
    # the Brackett-alpha line (peak ~3e-3 Jy over ~1e-3 continuum) lifts the 4.0-4.1 um range
    assert tops[(4.0, 4.1)] > 2e-3 and tops[(4.0, 4.1)] > tops[(2.0, 2.4)]
    assert run.errors() == []


def test_live_sed_matches_python(p2k, open_page):
    """JS ``sedPoints`` on a decoded record equals Python ``sed_points`` on the source row."""
    from portalkit.physics.sed import sed_points
    run = open_page(p2k.url_path("explorer"))
    out = run.ev("""async (i) => {
        const app = window.__pk.app;
        const c = await app.catalog('src');
        const rec = await c.record(i);
        const man = await (await fetch('data/src/manifest.json')).json();
        const fref = app.page.datasets.flt.url.replace('{res}', app.page.res);
        const filters = await (await fetch(fref)).json();
        const mod = await import(new URL(app.page.assets + 'js/data/filters.js', location.href).href);
        const id = await c.id(i);
        return {id, rec, pts: mod.sedPoints(rec, filters, man.cols), cols: man.cols, filters};
    }""", 5)
    i = p2k.input_row(out["id"])
    d = p2k.data
    rec = out["rec"]
    # the decoded record carries the source magnitudes (u16q at step 0.001)
    for k in ms.MAG_KEYS:
        v = d.mags[k][i]
        if np.isfinite(v):
            assert abs(rec[k] - v) <= 6e-4, (k, rec[k], v)
        else:
            assert rec.get(k) is None or not np.isfinite(rec[k])
    row = rec
    want = sed_points(row, out["filters"], out["cols"])
    got = out["pts"]
    assert [p["key"] for p in got] == [p["key"] for p in want]
    for g, w in zip(got, want):
        assert g["kind"] == w["kind"]
        assert math.isclose(g["fnu"], w["fnu"], rel_tol=1e-4)
        if w["err"] is None:
            assert g["err"] is None
        else:
            assert math.isclose(g["err"], w["err"], rel_tol=1e-3)


def test_live_sed_pane_renders_focus(e2e, request, p2k, open_page):
    e2e.xfail_unless_panes(request, ["sed"])
    run = open_page(p2k.url_path("explorer"), hash=f"id={ms.syn_id(3)}")
    _scatter_box(run, "sed")
    assert run.errors() == []


def test_crossfade_opacities(e2e, request, p2k, open_page):
    """Wavelength rungs: an asymmetric cross-fade. Between rungs r0 and r1 at slider position
    0 < f < 1 the lower rung stays opaque and the upper rung, stacked above it, has opacity f,
    so the visible weights f (r1) and 1 - f (r0) are complementary; r1's opacity rises with the
    position and r0 is hidden once r1 is opaque."""
    e2e.xfail_unless_panes(request, ["wavelength"])
    run = open_page(p2k.url_path("wavelength"), hash="rung=r0")
    run.wait_panes_settled()
    summary = "() => window.__aladinStub ? window.__aladinStub.summary(0) : null"
    run.page.wait_for_function(
        "() => !!(window.__aladinStub && window.__aladinStub.instances && window.__aladinStub.instances.length)",
        timeout=30000)
    assert run.ev(summary) is not None, "no Aladin instance (stub) on the wavelength page"
    st0 = run.pane_stats("wavelength")
    assert st0 is not None and st0.get("canFade") is not False, st0

    def rung_opacity(sm, k, base=True):
        vals = [L["opacity"] for L in (sm.get("layers") or {}).values()
                if L.get("url") and f"rung{k}" in L["url"] and L.get("opacity") is not None]
        if base and sm.get("base") and f"rung{k}" in sm["base"]:
            vals.append(1.0)
        return max(vals) if vals else 0.0

    # drive the rail slider through intermediate positions
    fades = []
    for pos in (0.25, 0.5, 0.75, 1.0):
        run.ev("""(v) => { const s = document.querySelector("section.pk-pane[data-pane-id='wavelength'] input.pk-wl-slider");
                          s.value = String(v); s.dispatchEvent(new Event('input', {bubbles: true})); }""", pos)
        run.page.wait_for_function(
            """(v) => { const o = (window.__pk.app.layout.pane('wavelength').stats() || {}).opacities || {};
                        return o.r1 !== undefined && o.r1 !== null && Math.abs(o.r1 - v) < 0.02; }""",
            arg=pos, timeout=20000)
        op = run.pane_stats("wavelength")["opacities"]
        sm = run.ev(summary)
        fades.append((pos, op, sm))
    prev = -1.0
    for pos, op, sm in fades[:-1]:
        a0, a1 = op["r0"], op["r1"]
        assert a0 == pytest.approx(1.0, abs=0.02), (pos, op)     # lower rung opaque
        assert 0.0 < a1 < 1.0, (pos, op)
        assert a1 == pytest.approx(pos, abs=0.02)
        visible0 = a0 * (1.0 - a1)                                # r0 seen through r1
        assert visible0 + a1 == pytest.approx(1.0, abs=0.03), (pos, op)
        assert a1 >= prev - 1e-6, "r1 opacity decreased while moving towards r1"
        prev = a1
        # the stub layers carry the same opacities
        assert rung_opacity(sm, 1, base=False) == pytest.approx(a1, abs=0.02), (pos, sm)
        for L in (sm.get("layers") or {}).values():
            if L.get("opacity") is not None:
                assert 0.0 <= L["opacity"] <= 1.0
    assert fades[-1][1]["r1"] >= prev
    # at position 1 the slider writes rung r1 back to the store
    run.page.wait_for_function("() => window.__pk.store.get('rung') === 'r1'", timeout=20000)
    run.page.wait_for_timeout(300)
    final = run.ev(summary)
    assert rung_opacity(final, 1) == pytest.approx(1.0, abs=0.02)
    # the outgoing rung is faded out (a base layer underneath an opaque rung stays hidden)
    assert rung_opacity(final, 0, base=False) == pytest.approx(0.0, abs=0.02)
    assert run.store_get("rung") == "r1"
    assert run.errors() == []


def test_narrative_beats_leave_no_stray_overlays(e2e, request, p2k, open_page):
    e2e.xfail_unless_panes(request, ["narrative", "sky"])
    run = open_page(p2k.url_path("narrative"), hash="beat=b1")
    run.wait_panes_settled()
    beats = ms.narrative_spec([]).beats
    for b in beats[1:] + beats[::-1]:
        run.ev("(b) => window.__pk.store.set({beat: b}, {source: 'test'})", b["id"])
        run.page.wait_for_timeout(300)
    # after returning to beat 1: the layers it turns off are hidden on the sky pane
    shown = run.ev("""() => { const h = window.__pk.app.layout.pane('sky');
        if (!h) return null;
        const out = new Set(typeof h.layers === 'function' ? h.layers() : []);
        const ov = (h.stats ? h.stats() : {}).overlays || {};
        for (const [id, o] of Object.entries(ov)) if (o && o.shown) out.add(id);
        return Array.from(out); }""")
    assert shown is not None, "no sky pane on the narrative page"
    on = set(beats[0]["view"]["layers_on"])
    off = set(beats[0]["view"]["layers_off"])
    assert on <= set(shown) and not (off & set(shown)), (shown, on, off)
    assert run.store_get("beat") == "b1"
    assert run.errors() == []


# ---------------------------------------------------------------------------
# (page x capability) matrix
# ---------------------------------------------------------------------------
#
# One case per enabled (page, capability) pair of the N = 2000 synthetic portal, collected
# from the resolver (caps.resolve on make_synthetic's page specs; the meta-test below checks
# that the built pages enable the same pairs). Each case runs on one shared page per archetype
# (``page_cache``) and checks
#   1. the capability is in ``body[data-caps]``;
#   2. every provider pane of the capability (caps.PROVIDERS) on the page mounts and reaches
#      state ``ready``;
#   3. a capability-specific behaviour check (CHECKS), which restores the state it changes;
#   4. no new page error.



def _collect_pairs() -> list[tuple[str, str]]:
    """Enabled caps of each page, resolved as build.py resolves them (ms.resolve_page uses
    the shared attachment rule, caps.attachable_resources; IMPLEMENTATION_NOTES 110).

    resolve plus attachable_resources leaves no unmet need by construction: every shared
    kind an enabled NEEDS capability requires is attached. The assertion makes a future
    divergence between the two fail here, so the matrix cannot shrink silently."""
    ds, _ = ms.datasets(N)
    out = []
    for spec in ms.page_specs():
        res = ms.resolve_page(spec, ds)
        pd = capsmod.page_data(spec, ds, {})
        kinds = set(capsmod.available_kinds(pd, ms.page_resources(spec, ds, res), res.layout))
        unmet = capsmod.unmet_needs(res.enabled, kinds)
        assert not unmet, (spec.id, sorted(unmet))
        out.extend((spec.id, cap) for cap in res.enabled)
    return out


PAIRS = _collect_pairs()


class Ctx:
    def __init__(self, run, portal, page, cap):
        self.run, self.portal, self.page, self.cap = run, portal, page, cap
        self.types = {p["id"]: p["pane"] for p in portal.pages[page].panes}
        prov = capsmod.PROVIDERS.get(cap, ())
        self.providers = [pid for pid, t in self.types.items() if t in prov]

    def has(self, ptype):
        return [pid for pid, t in self.types.items() if t == ptype]

    def first(self, *ptypes):
        for t in ptypes:
            ids = self.has(t)
            if ids:
                return ids[0]
        return None

    def ev(self, js, arg=None):
        return self.run.ev(js, arg)

    def stats(self, pane):
        return self.run.pane_stats(pane)

    def count(self, selector, panes=None):
        """Elements matching ``selector`` inside the given pane sections (whole page if None)."""
        return self.ev("""([sel, panes]) => {
            if (!panes) return document.querySelectorAll(sel).length;
            let n = 0;
            for (const p of panes) {
              const s = document.querySelector(`section.pk-pane[data-pane-id='${p}']`);
              if (s) n += s.querySelectorAll(sel).length;
            }
            return n; }""", [selector, panes])

    def wait(self, js, arg=None, timeout=20000):
        self.run.page.wait_for_function(js, arg=arg, timeout=timeout)

    def wait_stats(self, pane, cond, timeout=30000):
        """Wait until ``cond`` (a JS expression over ``s``, the pane's stats()) holds; the
        assertion message carries the last stats."""
        js = f"(p) => {{ const h = window.__pk.app.layout.pane(p); const s = h && h.stats ? h.stats() : null; return !!(s && ({cond})); }}"
        try:
            self.run.page.wait_for_function(js, arg=pane, timeout=timeout)
        except PWTimeout:
            raise AssertionError(f"{pane}: stats never satisfied `{cond}`: {self.stats(pane)}") from None
        return self.stats(pane)


def mount(run, pane):
    """Reveal ``pane``, wait for its module to mount and for the section to leave 'loading';
    returns the final data-state."""
    run.ev("(p) => window.__pk.app.layout.reveal(p)", pane)
    run.ev("async (p) => { await window.__pk.app.layout.whenMounted(p); }", pane)
    run.page.wait_for_function(
        "(p) => { const s = document.querySelector(`section.pk-pane[data-pane-id='${p}']`); return !!s && !!s.dataset.state && s.dataset.state !== 'loading'; }",
        arg=pane, timeout=60000)
    return run.ev("(p) => document.querySelector(`section.pk-pane[data-pane-id='${p}']`).dataset.state", pane)


def _ids(c, idx):
    """Ids of input rows ``idx`` of the page's primary catalog (the synthetic ``src`` ids, or
    stored-row ids of another catalog such as ``vc``)."""
    if c.ev("() => window.__pk.app.primaryDs") == "src":
        return [ms.syn_id(i) for i in idx]
    return c.ev("""async (idx) => { const app = window.__pk.app; const out = [];
                     for (const i of idx) out.push(String(await app.idOf(app.primaryDs, i)));
                     return out; }""", list(idx))


def _focus(c, i=2):
    sid = _ids(c, [i])[0]
    c.ev("async (id) => window.__pk.focus(window.__pk.app.primaryDs, id)", sid)
    return sid


def _clear_selection(c):
    c.ev("async () => { const s = await window.__pk.app.selectionFor(window.__pk.app.primaryDs); s.setIndices([], 'replace', {op: 'replace', kind: 'ids', view: {ds: window.__pk.app.primaryDs}}); }")


def _brush_pane(c):
    p = c.first("scatter", "manifold", "sky")
    assert p, "no brushable pane"
    return p


BIG_RECT = {"kind": "rect", "x0": -1e9, "x1": 1e9, "y0": -1e9, "y1": 1e9}
FAR_RECT = {"kind": "rect", "x0": 1e8, "x1": 1e9, "y0": 1e8, "y1": 1e9}


def chk_brush_mode(kind):
    def check(c):
        # the sky toolbar is appended after the pane's async catalog load, which can land
        # after data-state="ready"; wait for it rather than counting once
        for p in c.providers:
            sel = f"[data-mode='{kind}']" if c.types[p] == "sky" else f".pk-mode-{kind}"
            try:
                c.wait("([p, sel]) => { const s = document.querySelector(`section.pk-pane[data-pane-id='${p}']`);"
                       " return !!(s && s.querySelector(sel)); }", [p, sel], timeout=30000)
            except PWTimeout:
                pytest.fail(f"no {kind} brush tool in pane {p}; stats={c.stats(p)}")
    return check


def chk_brush_set_ops(c):
    p = c.first("scatter", "manifold")
    brush = "async ([p, s, op]) => window.__pk.brush(p, s, op)"
    try:
        a = c.ev(brush, [p, BIG_RECT, "replace"])
        assert a > 0
        assert c.ev(brush, [p, BIG_RECT, "subtract"]) == 0
        assert c.ev(brush, [p, BIG_RECT, "add"]) == a
        assert c.ev(brush, [p, FAR_RECT, "intersect"]) == 0
    finally:
        _clear_selection(c)


def chk_selectable_axes(c):
    if c.has("scatter"):
        assert c.count("select.pk-axis-select", c.has("scatter")) >= 2
    if c.has("manifold"):
        assert c.count("select.pk-axis-select, .pk-tour-axis-x, .pk-tour-axis-y", c.has("manifold")) >= 1


def chk_cmd_scatter(c):
    st = c.stats("scatter")
    assert st and st["shown"] > 0, st


def chk_class_coloring(c):
    assert c.count(".pk-legend", c.providers) >= 1, "no class legend"


def chk_filter_toggles(c):
    run = c.run
    prev = run.store_get("filters:src")
    n0 = (run.store_get("vis:src") or {}).get("count")
    off = [k for k in ms.CLASSES if k != "yso"]
    try:
        c.ev("(off) => window.__pk.store.set({'filters:src': {cats: {cls: off}}}, {source: 'test'})", off)
        c.wait("(n0) => { const v = window.__pk.store.get('vis:src') || {}; return v.count > 0 && (n0 === null || v.count < n0); }", n0)
        want = int((c.portal.data.cls == "yso").sum())
        assert run.store_get("vis:src")["count"] == want
    finally:
        c.ev("(prev) => window.__pk.store.set({'filters:src': prev}, {source: 'test'})", prev)
        if n0 is not None:
            c.wait("(n0) => (window.__pk.store.get('vis:src') || {}).count === n0", n0)


def chk_hover(c):
    page = c.run.page
    page.mouse.move(700, 450)
    page.mouse.move(705, 455)
    try:
        c.ev("() => window.__pk.store.set({hover: {ds: window.__pk.app.primaryDs, i: 3}}, {source: 'test'})")
        try:
            # the card waits on lazy column fetches, which queue behind earlier checks' loads
            # (e.g. the FITS overlay) on a loaded host
            c.wait("() => { const h = document.getElementById('pk-hovercard'); return !!h && !h.hidden; }",
                   timeout=60000)
        except PWTimeout:
            diag = c.ev("""() => { const h = document.getElementById('pk-hovercard'); const e = document.elementFromPoint(705, 455);
                return {hover: window.__pk.store.get('hover'), card: !!h, hidden: h && h.hidden, html: h && h.innerHTML.slice(0, 200),
                        at: e && (e.tagName + '.' + e.className), focus: document.activeElement && document.activeElement.tagName,
                        hasFocus: document.hasFocus(), lightbox: window.__pk.store.get('lightbox')}; }""")
            pytest.fail(f"hover card never shown: {diag}")
    finally:
        c.ev("() => window.__pk.store.set({hover: null}, {source: 'test'})")
    c.wait("() => { const h = document.getElementById('pk-hovercard'); return !h || h.hidden; }")


def chk_search(c):
    n = c.count("input.pk-list-search, input.pk-landing-q, .pk-landing-q, input.pk-gal-search, .pk-gal-search", c.providers)
    assert n >= 1, "no search input"
    lst = c.has("list")
    if not lst:
        return
    rows0 = c.stats(lst[0])["rows"]
    loc = c.run.page.locator(f"section.pk-pane[data-pane-id='{lst[0]}'] input.pk-list-search").first
    try:
        loc.fill(_ids(c, [7])[0])
        c.wait("([p, r0]) => { const s = window.__pk.app.layout.pane(p).stats(); return s.rows >= 1 && s.rows < r0; }", [lst[0], rows0])
    finally:
        loc.fill("")
    c.wait("([p, r0]) => window.__pk.app.layout.pane(p).stats().rows === r0", [lst[0], rows0])


def chk_detail(c):
    sid = _focus(c, 4)
    c.wait_stats("detail", f"s.id === {sid!r}")


IMG_LOADED_JS = """(p) => { const s = document.querySelector(`section.pk-pane[data-pane-id='${p}']`);
  const imgs = s ? Array.from(s.querySelectorAll('img')).filter((i) => i.getAttribute('src')) : [];
  return imgs.length > 0 && imgs.some((i) => i.complete && i.naturalWidth > 0); }"""


def chk_static_sed_png(c):
    """The figure PNGs load (IMPLEMENTATION_NOTES item 81: image URLs resolve page-relative,
    with no doubled path prefix): at least one <img> in the pane decodes."""
    if c.has("detail"):
        _focus(c, 1)
        c.wait_stats("detail", "s.figures >= 1")
        pane = "detail"
    else:
        pane = c.has("gallery")[0]
        st = c.stats(pane)
        assert st["items"] > 0
    try:
        c.wait(IMG_LOADED_JS, pane, timeout=30000)
    except PWTimeout:
        srcs = c.ev("(p) => Array.from(document.querySelectorAll(`section.pk-pane[data-pane-id='${p}'] img`)).map((i) => i.src)", pane)
        pytest.fail(f"no figure image of pane {pane} loaded: {srcs[:5]}; http errors {c.run.http_errors[-5:]}")
    bad = [e for e in c.run.http_errors if "/img/img/" in e]
    assert not bad, bad


def test_detail_product_links_resolve_against_records_index(e2e, request, p2k, open_page):
    """IMPLEMENTATION_NOTES item 81: detail product links (``products/1`` records of the
    ``prod`` dataset, wired to the ``cmd`` page's detail pane through ``options.products``)
    resolve against the records index URL (``data/prod/index.json``), with no doubled path
    prefix, and the linked files load."""
    e2e.xfail_unless_panes(request, ["detail"])
    sid = ms.syn_id(1)
    run = open_page(p2k.url_path(BRUSH_PAGE), hash=f"id={sid}")
    assert mount(run, "detail") == "ready"
    sel = "section.pk-pane[data-pane-id='detail'] a.pk-product-link"
    want = [f"{run.base}n{p2k.n}/{h}" for h in ms.product_hrefs(sid)]
    try:
        run.page.wait_for_function("([s, n]) => document.querySelectorAll(s).length >= n",
                                   arg=[sel, len(want)], timeout=60000)
    except PWTimeout:
        pytest.fail(f"no product links in the detail pane: stats={run.pane_stats('detail')}")
    hrefs = run.ev("(s) => Array.from(document.querySelectorAll(s)).map((a) => a.href)", sel)
    assert hrefs == want, (hrefs, want)
    assert not any("/data/prod/data/prod/" in h or f"/n{p2k.n}/n{p2k.n}/" in h for h in hrefs), hrefs
    status = run.ev("async (hs) => Promise.all(hs.map(async (h) => (await fetch(h)).status))", hrefs)
    assert status == [200] * len(want), dict(zip(hrefs, status))
    assert run.errors() == []


def chk_lightbox(c):
    page = c.run.page
    if c.has("gallery"):
        # gallery: double-click (or Enter) on a card opens the lightbox
        sel = f"section.pk-pane[data-pane-id='{c.has('gallery')[0]}'] img"
        c.wait("(s) => !!document.querySelector(s)", sel)
        page.locator(sel).first.dblclick()
    else:
        _focus(c, 1)
        c.wait_stats("detail", "s.figures >= 1")
        page.locator("section.pk-pane[data-pane-id='detail'] img").first.click()
    page.wait_for_selector(".pk-lightbox:not([hidden])", state="visible", timeout=20000)
    assert c.run.store_get("lightbox") is not None
    page.keyboard.press("Escape")
    c.wait("() => !window.__pk.store.get('lightbox')")


def chk_readme(c):
    """The landing page shows the portal title; a readme pane shows the README (index) or the
    page's About / data-provenance section."""
    for p in c.providers:
        text = c.ev("(p) => document.querySelector(`section.pk-pane[data-pane-id='${p}']`).textContent", p)
        if c.types[p] == "landing":
            assert ms.PORTAL["title"] in text, text[:300]
        else:
            assert "Synthetic portal" in text or "provenance" in text.lower(), text[:300]


def chk_aladin(c):
    for p in c.providers:
        c.wait_stats(p, "s.aladin === 'cdn'")


def chk_markers(c):
    p = c.first("sky")
    if p:
        c.wait_stats(p, "s.markers && s.markers.shown > 0")
    else:
        st = c.stats(c.first("wavelength"))
        assert st.get("markers"), st


def chk_hips(c):
    # IMPLEMENTATION_NOTES item 89: the cap is on only where a HiPS document is attached
    assert _page_has_kind(c, "hips"), "hips_layer_stacking enabled on a page without a HiPS document"
    p = c.first("sky")
    if p:
        c.wait_stats(p, "s.layers && s.layers.base")
    if c.has("wavelength"):
        st = c.stats(c.has("wavelength")[0])
        assert len(st["rungs"]) >= 2, st


def chk_fits(c):
    """Turning the synthetic FITS overlay on through the ``sky`` store loads it (stub
    createImageFITS) and shows it; the previous ``sky`` value is restored afterwards."""
    p = c.first("sky")
    prev = c.run.store_get("sky")
    try:
        c.ev("() => { const s = window.__pk.store.get('sky') || {}; window.__pk.store.set({sky: {...s, fits: ['stub_fits']}}, {source: 'test'}); }")
        st = c.wait_stats(p, "s.layers && s.layers.fits && s.layers.fits.stub_fits && s.layers.fits.stub_fits.status === 'ready'")
        assert st["layers"]["fits"]["stub_fits"]["on"] is True, st["layers"]
    finally:
        c.ev("(prev) => { const s = window.__pk.store.get('sky') || {}; window.__pk.store.set({sky: {...s, fits: (prev && prev.fits) || []}}, {source: 'test'}); }", prev)


DENSITY_JS = """async ([x, y, xr, yr]) => {
  const app = window.__pk.app;
  const prim = await app.catalog(app.primaryDs);
  const grids = ((prim.manifest || {}).density || []).length;
  const ctxIds = Object.entries(app.page.datasets || {})
    .filter(([, r]) => r && r.kind === 'catalog' && r.context).map(([k]) => k);
  let ctxCount = 0;
  for (const k of ctxIds) {
    const bg = await app.catalog(k);
    const g = await bg.bin2d({pane: 'test:ctx', kind: 'xy', x, y, xlog: false, ylog: false}, 16, 16, {mask: 'vis', xr, yr});
    for (const v of g.counts) ctxCount += v;
  }
  return {grids, ctxIds, ctxCount};
}"""


def chk_density(c):
    """Density layers: the primary catalog ships pre-binned grids (N > T0_MAX), or the page's
    context catalogs bin to a non-empty density in the worker (what the scatter pane draws
    under the points)."""
    out = c.ev(DENSITY_JS, [ms.CMD_X, ms.CMD_Y, [-20.0, 20.0], [0.0, 40.0]])
    assert out["grids"] >= 1 or (out["ctxIds"] and out["ctxCount"] > 0), out


def chk_manifold(c):
    c.wait_stats("manifold", "s.projected && s.shown > 0", timeout=60000)


def chk_av(c):
    assert c.count("input[aria-label='A_V (mag)']") >= 1, "no A_V control"


def chk_overlays(c):
    """IMPLEMENTATION_NOTES item 88: the cap draws something. With an isochrones
    (``isochrones_or_tracks``) or extinction (``extinction_vectors_on_ccd``) dataset on the
    page, every scatter / manifold provider lists the matching overlay ids in
    ``options.overlays``; without one, the page carries an overlays document and some provider
    lists overlay ids. The scatter panes that list overlays draw them (``stats().overlays``)."""
    kind, want = (("isochrones", lambda o: o in TRACK_SLUGS) if c.cap == "isochrones_or_tracks"
                  else ("extinction", lambda o: o.endswith("_vector")))
    panes = [p for p in c.providers if c.types[p] in ("scatter", "manifold")]
    assert panes, f"{c.cap} on {c.page} without a scatter / manifold pane"
    opts = {q["id"]: [o for o in (q["options"].get("overlays") or []) if isinstance(o, str)]
            for q in c.portal.pages[c.page].panes}
    if _page_has_kind(c, kind):
        for p in panes:
            assert any(want(o) for o in opts.get(p, [])), (p, opts.get(p))
        listing = panes
    else:
        assert _page_has_kind(c, "overlays"), (
            f"{c.cap} enabled on {c.page} without a {kind} dataset or an overlays document; "
            f"provider overlays {[(p, opts.get(p)) for p in panes]}")
        listing = [p for p in panes if opts.get(p)]
        assert listing, f"{c.cap} on {c.page}: no provider lists an overlay id {[(p, opts.get(p)) for p in panes]}"
    scat = [p for p in listing if c.types[p] == "scatter"]
    for p in scat:
        c.wait_stats(p, "s.overlays > 0")


def chk_template(c):
    assert c.count("select[aria-label='Model template']", c.providers) >= 1


def chk_features(c):
    # IMPLEMENTATION_NOTES item 89: the cap is on only where a features dataset is attached
    assert _page_has_kind(c, "features"), "labeled_features_overlay enabled on a page without a features dataset"
    _focus(c, 2)
    for p in c.providers:
        mount(c.run, p)
    c.wait("(ps) => ps.some((p) => { const s = document.querySelector(`section.pk-pane[data-pane-id='${p}']`); return s && s.querySelector('.pk-feat-chip'); })", c.providers)


def chk_spec1d(c):
    _focus(c, 2)
    c.wait_stats("spectrum1d", "s.n > 0")


def chk_spec2d(c):
    sid = _focus(c, 2)
    st = c.wait_stats("spectrum2d", f"s.id === {sid!r} && (s.nx > 0 || s.mode === 'png')")
    if st["mode"] == "png":
        # pre-rendered 2D spectrum: the pane status title reads "<id> · <w>×<h>" once the PNG
        # decodes and "Could not load <url>" when it fails (spectrum2d.js)
        c.wait("(id) => { const e = document.querySelector(\"section.pk-pane[data-pane-id='spectrum2d'] .pk-pane-status\");"
               " return !!e && (e.title.startsWith(id) || e.title.startsWith('Could not load')); }", sid)
        title = c.ev("() => document.querySelector(\"section.pk-pane[data-pane-id='spectrum2d'] .pk-pane-status\").title")
        # IMPLEMENTATION_NOTES item 81: the png sidecar URL resolves against the page URL
        assert not title.startswith("Could not load"), title
        assert re.search(r"\d+×\d+", title), title
    else:
        assert st["ny"] > 0, st


#: overlay ids of the synthetic isochrone tracks (caps.overlay_slug of the labels)
TRACK_SLUGS = frozenset(ms.cmd_overlay_ids()[:-1])


def _page_has_kind(c, kind):
    return bool(c.ev("(k) => Object.values(window.__pk.app.page.datasets || {}).some((r) => r && r.kind === k)", kind))


def chk_live_sed(c):
    # IMPLEMENTATION_NOTES item 89: the cap is on only where a filters dataset is attached
    assert _page_has_kind(c, "filters"), "live_sed_viewer enabled on a page without a filters dataset"
    _focus(c, 3)
    c.wait_stats("sed", "Array.isArray(s.points) ? s.points.length > 0 : s.points > 0")


def chk_linked(c):
    ids = _ids(c, (1, 2, 3))
    try:
        assert c.ev("async (ids) => window.__pk.select(window.__pk.app.primaryDs, ids)", ids) == 3
        for p in c.has("scatter") + c.has("manifold"):
            c.wait_stats(p, "s.selected === 3")
        for p in c.has("sky"):
            c.wait_stats(p, "s.markers && s.markers.selected === 3")
        if c.has("list"):
            assert c.count("[aria-selected='true'], .pk-selected", c.has("list")) >= 1
    finally:
        _clear_selection(c)


def chk_collapse(c):
    p = next(pid for pid, t in c.types.items() if t not in ("landing", "narrative", "wavelength"))
    if c.ev("() => window.__pk.app.layout.isTabMode()"):
        pytest.skip("tab mode: panes are tabs, not collapsible columns")
    try:
        c.ev("(p) => window.__pk.app.layout.collapse(p, true)", p)
        assert c.ev("(p) => window.__pk.app.layout.info(p).collapsed", p) is True
    finally:
        c.ev("(p) => window.__pk.app.layout.collapse(p, false)", p)
    assert c.ev("(p) => window.__pk.app.layout.info(p).collapsed", p) is False


def chk_resize(c):
    assert c.count(".pk-splitter") >= 1


def chk_theme(c):
    js = "async (m) => (await import(new URL(window.__pk.app.page.assets + 'js/core/theme.js', location.href).href))"
    prev = c.ev(js + ".get()")
    eff0 = c.ev(js + ".effective()")
    try:
        c.run.page.locator(".pk-theme-toggle").first.click()
        assert c.ev(js + ".effective()") != eff0
    finally:
        c.ev("async (m) => (await import(new URL(window.__pk.app.page.assets + 'js/core/theme.js', location.href).href)).set(m)", prev)
    assert c.ev(js + ".effective()") == eff0


def chk_keys(c):
    n = c.ev("() => { const k = window.__pk.app.keymap; return k ? (k.length ?? Object.keys(k).length) : 0; }")
    assert n > 0


def chk_nav(c):
    # ui/nav.js fills the nav from portal.json after ready (boot does not await it), so the
    # first case on a freshly opened shared page can run before the links exist
    js = "() => document.querySelectorAll('a.pk-nav-link:not(.pk-nav-ext)').length"
    want = len(c.portal.pages)
    try:
        c.wait(f"() => ({js})() >= {want}", timeout=30000)
    except PWTimeout:
        pass   # the assertion below reports the count
    n = c.ev(js)
    assert n == want, n


def chk_hash(c):
    assert isinstance(c.ev("() => window.__pk.app.hashLink()"), str)


def chk_mobile(c):
    meta = c.ev("() => { const m = document.querySelector('meta[name=viewport]'); return m ? m.content : ''; }")
    assert "width=device-width" in meta


def chk_lazy(c):
    st = c.ev("() => window.__pk.stats()")
    assert st["fetches"] >= 1, st


def chk_export(c):
    assert c.count(".pk-io-export") >= 1


CHECKS = {
    "brush_rect": chk_brush_mode("rect"),
    "brush_circle": chk_brush_mode("circle"),
    "brush_lasso": chk_brush_mode("lasso"),
    "brush_set_ops": chk_brush_set_ops,
    "selectable_axes": chk_selectable_axes,
    "cmd_ccd_scatter": chk_cmd_scatter,
    "class_coloring": chk_class_coloring,
    "filter_toggles": chk_filter_toggles,
    "hover_preview": chk_hover,
    "source_list_search": chk_search,
    "detail_panel": chk_detail,
    "static_sed_png": chk_static_sed_png,
    "lightbox": chk_lightbox,
    "readme_provenance": chk_readme,
    "sky_map_aladin": chk_aladin,
    "cdn_fallback": chk_aladin,
    "catalog_markers_on_sky": chk_markers,
    "hips_layer_stacking": chk_hips,
    "fits_overlay": chk_fits,
    "density_rendering": chk_density,
    "manifold_embedding": chk_manifold,
    "av_slider": chk_av,
    "extinction_vectors_on_ccd": chk_overlays,
    "isochrones_or_tracks": chk_overlays,
    "model_template_overlay": chk_template,
    "labeled_features_overlay": chk_features,
    "spectrum_viewer_1d": chk_spec1d,
    "spectrum_2d_viewer": chk_spec2d,
    "live_sed_viewer": chk_live_sed,
    "linked_selection": chk_linked,
    "collapsible_panels": chk_collapse,
    "resizable_panels": chk_resize,
    "dark_light_theme": chk_theme,
    "keyboard_nav": chk_keys,
    "cross_page_nav": chk_nav,
    "deep_link_hash": chk_hash,
    "responsive_mobile": chk_mobile,
    "lazy_data_loading": chk_lazy,
    "selection_export": chk_export,
}


def test_matrix_pairs_match_built_portal(p2k):
    """The collected pairs are the caps the built N = 2000 pages enable, and every capability
    of caps.CAPS has a behaviour check."""
    built = sorted((pid, cap) for pid, info in p2k.pages.items() for cap in info.caps)
    assert built == sorted(PAIRS)
    assert set(capsmod.CAPS) <= set(CHECKS), sorted(set(capsmod.CAPS) - set(CHECKS))


@pytest.mark.parametrize("page,cap", PAIRS, ids=[f"{p}-{c}" for p, c in PAIRS])
def test_capability(e2e, request, p2k, page_cache, page, cap):
    e2e.xfail_unless_panes(request, e2e.page_pane_types(p2k, page))
    run = page_cache(p2k.url_path(page))
    c = Ctx(run, p2k, page, cap)
    assert cap in run.caps_attr()
    if capsmod.PROVIDERS.get(cap):
        assert c.providers, f"{cap} enabled on {page} without a provider pane"
    # the page is shared by the module: report what this case logs (error cursor), including
    # repeats of messages an earlier case logged
    mark = run.error_mark()
    for p in c.providers:
        assert mount(run, p) == "ready", f"provider pane {p} of {cap} did not reach 'ready'"
    CHECKS[cap](c)
    new = run.errors(since=mark)
    assert new == [], new
