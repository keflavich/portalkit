"""Contract tests for the robitaille adapter (ARCHITECTURE.md section 9.6 a-g; WP21).

Fixture mode (``limit=50``). Run with ``python -m pytest tests/adapters/test_robitaille.py -m
adapters -q``. Builds go to pytest temp dirs (or ``$PK_STAGE_ROOT``); the adapter cache is
``$PK_CACHE_DIR`` when set (recommended: Tier A extracts a 46 MB payload, about 25 s cold).

Test (g) resolves the manifold page's capabilities from the fixture build's manifests with the
catalog size set to the production count, because ``density_rendering`` depends on N.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from portalkit.context import BuildContext, load_site  # noqa: E402

pytestmark = pytest.mark.adapters

FAMILY = "robitaille"
LIMIT = 50
N_FULL = 28248          # rows of the legacy manifold payload (and of the Tier B replay)


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    old = {k: os.environ.get(k) for k in ("PK_CACHE_DIR", "PK_STAGE_ROOT")}
    if not old["PK_CACHE_DIR"]:
        os.environ["PK_CACHE_DIR"] = str(tmp_path_factory.mktemp("pkcache"))
    stage = Path(old["PK_STAGE_ROOT"] or tmp_path_factory.mktemp("stage"))
    yield stage
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture(scope="module")
def adapter():
    from adapters.robitaille import ADAPTER
    return ADAPTER


def _ctx(tier, stage):
    return BuildContext(family=FAMILY, site=load_site(FAMILY), staging=stage / "ctx", tier=tier, limit=LIMIT,
                        log=lambda m: None)


@pytest.fixture(scope="module")
def runs(env, adapter):
    out = {}
    for tier in ("A", "B"):
        ctx = _ctx(tier, env)
        t0 = time.perf_counter()
        ds = adapter.datasets(ctx)
        out[tier] = (ctx, ds, time.perf_counter() - t0)
    return out


@pytest.fixture(scope="module")
def build(env):
    from portalkit.build import build_family
    r = build_family(FAMILY, tier="A", limit=LIMIT, stage_root=env, log=lambda m: None)
    return r


# -- (a) import has no side effects -----------------------------------------------------------
def test_a_import_creates_no_files():
    with tempfile.TemporaryDirectory() as d:
        code = f"import sys; sys.path.insert(0, {str(REPO)!r}); import adapters.robitaille as m; assert m.ADAPTER"
        subprocess.run([sys.executable, "-c", code], cwd=d, check=True, timeout=120,
                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        assert os.listdir(d) == []


# -- (b) datasets() returns valid specs in time -----------------------------------------------
@pytest.mark.parametrize("tier", ["A", "B"])
def test_b_datasets_valid_and_fast(runs, tier):
    from portalkit.spec import CatalogSpec, ImageSetSpec, ReadmeSpec, RecordSetSpec
    _, ds, dt = runs[tier]
    assert dt < 120, f"datasets() took {dt:.1f} s"
    kinds = {"mod": CatalogSpec, "imaging": CatalogSpec, "sedimg": ImageSetSpec, "imgs": ImageSetSpec,
             "sedcurve": RecordSetSpec, "readme": ReadmeSpec}
    for k, cls in kinds.items():
        assert isinstance(ds[k], cls), k
    assert 0 < len(ds["mod"].table) <= LIMIT and len(ds["imaging"].table) <= LIMIT


def test_c_tier_c_unavailable(env, adapter):
    from portalkit.spec import TierUnavailable
    with pytest.raises(TierUnavailable):
        adapter.datasets(BuildContext(family=FAMILY, site=load_site(FAMILY), staging=env / "c", tier="C", limit=LIMIT))


# -- (c) ids and aliases ------------------------------------------------------------------------
@pytest.mark.parametrize("tier", ["A", "B"])
def test_c_ids_and_aliases(runs, tier):
    _, ds, _ = runs[tier]
    for key in ("mod", "imaging"):
        cat = ds[key]
        ids = [str(i) for i in cat.table[cat.id]]
        assert len(set(ids)) == len(ids), key
        al = [a for v in (cat.aliases or {}).values() for a in v]
        assert len(set(al)) == len(al), key
        assert not set(al) & set(ids), key
        assert set(cat.aliases or {}) <= set(ids), key


# -- (d) inputs ----------------------------------------------------------------------------------
@pytest.mark.parametrize("tier", ["A", "B"])
def test_d_inputs_exist_not_tmp(runs, tier):
    ctx, _, _ = runs[tier]
    recs = ctx.inputs.records()
    assert recs
    for r in recs:
        p = r["path"]
        assert os.path.exists(p), p
        assert not p.startswith(("/tmp/", "/var/tmp/", tempfile.gettempdir() + "/")), p


# -- (e) fixture build passes check ----------------------------------------------------------------
def test_e_fixture_build_passes_check(build):
    from portalkit.build import check_build
    assert build.ok, build.report.get("errors")
    rep = check_build(build.staging)
    assert rep.ok, rep.errors
    assert sorted(Path(p).name for p in build.pages) == ["imaging.html", "index.html", "manifold.html"]


# -- (f) Tier A / Tier B parity ---------------------------------------------------------------------
def test_f_tier_parity(runs, build):
    """|a - b| <= one quantization step (section 9.6 f), with one documented exception: Tier A value
    columns come from the legacy payload, which carries 3 decimals (rounding error <= 5e-4), finer
    than nothing but coarser than most u16q steps of the physical values (e.g. log10 T* step 1.8e-5).
    Those columns are compared after rounding both tiers to the payload precision, and the adapter's
    5-significant-digit storage (relative 5e-5) is allowed for. f32 columns have no step."""
    a, b = runs["A"][1]["mod"], runs["B"][1]["mod"]
    man = json.loads((Path(build.staging) / FAMILY / "data" / "mod" / "manifest.json").read_text())
    step = {c["key"]: float(c.get("step") or 0.0) for c in man["cols"]}
    assert len(a.table) == len(b.table)
    assert list(a.table["id"]) == list(b.table["id"])
    for k in ("cls", "stg", "geom", "src", "cov", "det", "red", "sed"):
        assert list(a.table[k]) == list(b.table[k]), k
    num = [c for c in a.columns if c.role in ("mag", "value")]
    assert {c.key for c in num} == {c.key for c in b.columns if c.role in ("mag", "value")}
    for c in num:
        x, y = np.asarray(a.table[c.key], float), np.asarray(b.table[c.key], float)
        assert np.array_equal(np.isfinite(x), np.isfinite(y)), c.key
        ok = np.isfinite(x)
        x, y = x[ok], y[ok]
        if c.role == "mag":                     # both tiers hold 3 decimals = the 0.001 mag step
            tol = step[c.key] + 1e-9
            assert step[c.key] == pytest.approx(1e-3), c.key
        else:
            x, y = np.round(x, 3), np.round(y, 3)
            tol = max(step.get(c.key, 0.0), 1e-3) + 5e-5 * np.abs(y) + 1e-9
        assert np.all(np.abs(x - y) <= tol), (c.key, np.max(np.abs(x - y)))
    assert len(a.virtual) == len(b.virtual) and len(a.manifold.dims) == len(b.manifold.dims)
    assert sorted(runs["A"][1]["sedcurve"].ids) == sorted(runs["B"][1]["sedcurve"].ids)


# -- (g) resolved caps cover the legacy inventory --------------------------------------------------
def test_g_caps_cover_inventory(build, runs, adapter):
    from portalkit.caps import resolve
    inv = json.loads((REPO / "tests" / "data" / "inventory_caps.json").read_text())
    ctx, ds, _ = runs["A"]
    root = Path(build.staging) / FAMILY
    man = {k: json.loads((root / "data" / k / "manifest.json").read_text()) for k in ("mod", "imaging")}
    man["mod"]["n"] = N_FULL                     # density_rendering depends on the production N
    pages = {p.id: p for p in adapter.pages(ctx, ds)}
    legacy = {"index.html": "index", "manifold.html": "manifold", "imaging/index.html": "imaging",
              "imaging/gallery.html": "imaging"}
    for lp, pid in legacy.items():
        want = set(inv[f"{FAMILY}/{lp}"])
        got = set(resolve(pages[pid], ds, man).enabled)
        assert want <= got, (lp, sorted(want - got))
    rep = json.loads((root / "build_report.json").read_text())
    built = {p["id"]: set(p["caps"]["enabled"]) for p in rep["pages"]}
    for lp, pid in legacy.items():
        missing = set(inv[f"{FAMILY}/{lp}"]) - built[pid] - {"density_rendering"}
        assert not missing, (lp, sorted(missing))


# -- special handling (port table) ---------------------------------------------------------------
def test_special_handling_specs(runs):
    from adapters.robitaille import MAG_HI, M8
    _, ds, _ = runs["B"]
    mod = ds["mod"]
    mags = [c for c in mod.columns if c.role == "mag"]
    vals = [c for c in mod.columns if c.role == "value"]
    assert len(mags) == 54 and len(mags) + len(vals) <= 83          # stored base columns
    assert all(c.clip == "none" and c.filter.startswith("JWST/") for c in mags)
    for c in mags:                         # contamination rule lives in the table: rows == columns
        v = np.asarray(mod.table[c.key], float)
        assert not np.any(v[np.isfinite(v)] > MAG_HI), c.key
    assert "n" not in mod.table.colnames                    # the id already carries geom/name
    assert all(al[0].count("/") == 1 for al in mod.aliases.values())   # legacy #star=<geom/name>
    colours = [v for v in mod.virtual if v.group == "color"]
    assert len(colours) == 171 and any(v.key == M8 for v in mod.virtual)
    cls = next(c for c in mod.columns if c.key == "cls")
    assert "FRIED" in cls.levels and "FRIED" not in cls.default_off
    assert {str(s) for s in mod.table["src"]} >= {"R&R", "FRIED v1"}
    assert np.all(~np.isfinite(np.asarray(mod.table["m_NIRCam.F480M"], float)[np.asarray(mod.table["geom"]) == "FRIED"]))
    assert mod.manifold.default_frame in {p.key for p in mod.presets}
    assert any(p.kind == "filter" and p.key == "sel_fried" for p in mod.presets)
    rec = ds["sedcurve"]
    s = rec.loader(rec.ids[0])
    assert s is not None and np.all(np.diff(s.wave) > 0) and np.nanmax(s.channels["flux"]) > 0
    img = ds["imaging"]
    assert np.isnan(np.asarray(img.table["Menv"], float)).any()        # NaN kept missing (legacy wrote 0)
    assert all(it.roles["image"].startswith("https://data.rc.ufl.edu/pub/") for it in ds["imgs"].items)
    assert "/secure/" not in json.dumps([it.roles for it in ds["imgs"].items + ds["sedimg"].items])
    md = ds["readme"].markdown
    n_geo = len({str(g) for g in mod.table["geom"]} - {"FRIED"})
    assert f"from {n_geo} geometr" in md and "16-geometry" not in md


def test_readme_counts_from_data(runs, adapter):
    """Per-geometry quota and seed come from the legacy module (B) or the payload (A), not literals."""
    mod = runs["B"][0].legacy.module("Robitaille_Portal_Manifold")
    quota = f"up to {20000 // len(mod.GEOMS):,} models"
    assert quota in runs["B"][1]["readme"].markdown and f"(seed {mod.SEED})" in runs["B"][1]["readme"].markdown
    assert quota in runs["A"][1]["readme"].markdown            # max rows per geometry in the payload


def test_loop_memo_key_tracks_generator():
    """The Tier B memo key folds in the replayed constants and the loop source; the generator file
    itself is a memo input (mtime/size)."""
    from types import SimpleNamespace
    from adapters.robitaille import RobitailleAdapter
    m = SimpleNamespace(SEED=1, GEOMS=["a", "b"])
    k = RobitailleAdapter._loop_key(m, ["a"])
    assert k != RobitailleAdapter._loop_key(SimpleNamespace(SEED=2, GEOMS=["a", "b"]), ["a"])
    assert k != RobitailleAdapter._loop_key(SimpleNamespace(SEED=1, GEOMS=["a", "b", "c"]), ["a"])
    import inspect
    assert "mod.__file__" in inspect.getsource(RobitailleAdapter._tier_b)


def test_value_columns_quantize_finely(runs, adapter):
    """Every stored numeric column of the full catalog resolves to an encoding whose step resolves
    the bulk of its values (A_V spans 0..3e12 mag: u16q would decode every A_V < 5e7 as 0)."""
    from portalkit.bundle.catalog import resolve_enc
    ctx = runs["A"][0]
    raw = adapter._tier_a(ctx)                   # cached payload
    mod = adapter._catalog(raw, np.arange(len(raw["rows"]["name"])), set())
    for c in (c for c in mod.columns if c.role in ("mag", "value")):
        v = np.asarray(mod.table[c.key], float)
        f = v[np.isfinite(v)]
        enc, step = resolve_enc(c, v, rows_written=True, exact_gpu=c.exact_gpu is not False)
        if enc == "f32":
            continue
        assert enc == "u16q", (c.key, enc)
        step = step if step is not None else (f.max() - f.min()) / 65534
        p1, p25, p75, p99 = np.percentile(f, [1, 25, 75, 99])
        assert step <= 1e-3 * (p99 - p1) + 1e-12, (c.key, step, p1, p99)
        if p75 > p25:
            assert step <= 0.05 * (p75 - p25), (c.key, step, p25, p75)
    av = next(c for c in mod.columns if c.key == "av")
    assert av.enc == "f32" and av.exact_gpu is False


def test_density_grids(runs, adapter):
    """Pre-binned grids carry the preset key (no duplicate auto grids) and use the masked table."""
    from adapters.robitaille import MAG_HI
    ctx = runs["A"][0]
    raw = adapter._tier_a(ctx)
    mod = adapter._catalog(raw, np.arange(len(raw["rows"]["name"])), set())
    axes = {p.key for p in mod.presets if p.kind == "axes"}
    assert mod.density and {d.key for d in mod.density} <= axes
    single = 0
    for d in mod.density:
        for e, r in ((d.x, d.xr), (d.y, d.yr)):
            if e in mod.table.colnames and e.startswith("m_"):     # one magnitude: the cut bounds it
                single += 1
                assert r[1] <= MAG_HI + 1e-6, (d.key, e, r)
    assert single


def test_cross_page_links_resolve(runs):
    """Per-row link columns exist only where the target row exists (no dead page-level links)."""
    for tier in ("A", "B"):
        ds = runs[tier][1]
        mod, img = ds["mod"], ds["imaging"]
        img_ids = {str(i) for i in img.table["id"]}
        mod_keys = {str(i) for i in mod.table["id"]} | {a for al in mod.aliases.values() for a in al}
        if "img_url" in mod.table.colnames:
            for u in (str(u) for u in mod.table["img_url"] if u):
                assert u.startswith("imaging.html#id=") and u.split("=", 1)[1] in img_ids, u
        if "mod_url" in img.table.colnames:
            for u in (str(u) for u in img.table["mod_url"] if u):
                assert u.startswith("manifold.html#id=") and u.split("=", 1)[1] in mod_keys, u


def test_pages(runs, adapter):
    ctx, ds, _ = runs["A"]
    pages = {p.id: p for p in adapter.pages(ctx, ds)}
    assert set(pages) == {"index", "manifold", "imaging"}
    assert pages["manifold"].state.get("dm") == 0.0 and pages["manifold"].manifold
    assert "sedcurve" in pages["manifold"].datasets
    assert all("gallery" not in p.id for p in pages.values())       # legacy gallery.html merged
    assert not any(p.links for p in pages.values())     # cross-page links are per-row url columns
    from adapters.robitaille import SKY_CAPS
    assert set(pages["manifold"].caps_disable) == set(SKY_CAPS)
    assert all(r.strip() for r in pages["manifold"].caps_disable.values())


def test_site_legacy_mapping():
    """imaging/ is a symlink into the data tree: the imaging page maps no legacy file for --replace,
    and imaging/gallery.html is never stubbed (port table: legacy file left in place)."""
    site = load_site(FAMILY)
    pages = {pg.id: pg for pg in site.portals[0].pages}
    assert pages["imaging"].legacy == []
    assert not any("imaging/" in f for pg in pages.values() for f in pg.legacy)
