"""Contract tests for the sickle_brick adapter (ARCHITECTURE.md section 9.6, port table 14.8).

Fixture mode (``limit=50``) with a tmp stage root. Tier B memoises ``collect_sources`` of both
legacy generators in the portalkit cache (``$PK_CACHE_DIR`` or ``~/.cache/portalkit``); the first
run after a CSV or PNG change takes ~40 s.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pytest

from portalkit.build import build_family, check_build
from portalkit.context import BuildContext, expand_inputs, load_site
from portalkit.deploy import ExposureMap
from portalkit.spec import (CatalogSpec, ExtinctionSpec, FilterSetSpec, HipsSpec, ImageSetSpec, PageSpec,
                            ReadmeSpec, RecordSetSpec, TemplateSpec, TierUnavailable)

REPO = Path(__file__).resolve().parents[2]
LIMIT = 50
FAMILY = "sickle_brick"
INV = json.loads((REPO / "tests" / "data" / "inventory_caps.json").read_text(encoding="utf-8"))
PORT = json.loads((REPO / "tests" / "data" / "port_table.json").read_text(encoding="utf-8"))
TMP_ROOTS = tuple(os.path.realpath(p) for p in {tempfile.gettempdir(), "/tmp", "/var/tmp"})
# inventory/port-table key prefix per portal; ccds_cmds and nirspec_6927 rows belong to WP23
PREFIX = {"sickle": "sickle_brick/", "brick2221": "sickle_brick/brick_2221/"}
WP23 = ("brick_2221/ccds_cmds/", "brick_2221/nirspec_6927/")
PAGES = {"sickle": ["index", "sickle_miri_sed_map", "sickle_miri_sed_manifold"],
         "brick2221": ["index", "f1500w_explorer", "f1500w_gallery"]}

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from adapters import sickle_brick as mod  # noqa: E402

SITE = load_site(FAMILY)
pytestmark = [pytest.mark.adapters,
              pytest.mark.skipif(not (mod.SICKLE_DIR.is_dir() and mod.BRICK_PNG.is_dir()),
                                 reason="legacy Sickle / Brick2221 portal trees not mounted")]


def _ctx(tier: str, staging: Path, limit=LIMIT) -> BuildContext:
    return BuildContext(family=FAMILY, site=SITE, staging=staging, tier=tier, limit=limit, log=lambda m: None)


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """datasets() + pages() for both tiers (timed: contract 9.6(b))."""
    out = {}
    for tier in ("A", "B"):
        ctx = _ctx(tier, tmp_path_factory.mktemp(f"ctx{tier}"))
        adapter = mod.SickleBrickAdapter()
        t0 = time.perf_counter()
        ds = adapter.datasets(ctx)
        dt = time.perf_counter() - t0
        out[tier] = {"ctx": ctx, "ds": ds, "pages": adapter.pages(ctx, ds), "dt": dt, "adapter": adapter}
    return out


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    stage = tmp_path_factory.mktemp("stage")
    mp = pytest.MonkeyPatch()
    mp.setenv("PK_STAGE_ROOT", str(stage))
    try:
        res = build_family(FAMILY, limit=LIMIT, stage_root=stage, log=lambda m: None,
                           emap=ExposureMap(roots={}, url_prefixes={}))
    finally:
        mp.undo()
    reports = {p: json.loads((res.staging / p / "build_report.json").read_text(encoding="utf-8")) for p in PAGES}
    return {"res": res, "reports": reports}


def _pages(runs, tier="B"):
    return {(p.portal, p.id): p for p in runs[tier]["pages"]}


# -- (a) ------------------------------------------------------------------------------------
def test_import_has_no_side_effects(tmp_path):
    code = ("import sys; sys.path.insert(0, %r); import adapters.sickle_brick as m; "
            "assert m.ADAPTER.name == 'sickle_brick'" % str(REPO))
    subprocess.run([sys.executable, "-c", code], cwd=tmp_path, check=True, timeout=120)
    assert list(tmp_path.iterdir()) == []


# -- (b) ------------------------------------------------------------------------------------
@pytest.mark.parametrize("tier", ["A", "B"])
def test_datasets_valid_and_fast(runs, tier):
    r = runs[tier]
    assert r["dt"] < 120, f"datasets() took {r['dt']:.1f} s"
    ds = r["ds"]
    for k in ("sickle", "brick"):
        assert isinstance(ds[k], CatalogSpec) and len(ds[k].table) == LIMIT
    for k in ("sk_img", "br_img"):
        assert isinstance(ds[k], ImageSetSpec) and len(ds[k].items) == LIMIT
    for k in ("sk_hips", "br_hips"):
        assert isinstance(ds[k], HipsSpec)
    for k in ("sk_prod", "br_prod"):
        assert isinstance(ds[k], RecordSetSpec)
    for k in ("sk_readme", "br_readme"):
        assert isinstance(ds[k], ReadmeSpec)
    assert isinstance(ds["tpl26"], TemplateSpec) and isinstance(ds["flt"], FilterSetSpec)
    assert isinstance(ds["ext"], ExtinctionSpec)
    for p in r["pages"]:
        assert isinstance(p, PageSpec)
        for k in p.datasets:
            assert k in ds, (p.id, k)
    for portal, ids in PAGES.items():
        assert [p.id for p in r["pages"] if p.portal == portal] == ids
    assert r["adapter"]._raw["n"] == {"sk": 245, "br": 241}


def test_tier_c_unavailable(tmp_path):
    with pytest.raises(TierUnavailable):
        mod.SickleBrickAdapter().datasets(_ctx("C", tmp_path))


# -- (c) ------------------------------------------------------------------------------------
@pytest.mark.parametrize("tier", ["A", "B"])
def test_ids_and_aliases_unique(runs, tier):
    ds = runs[tier]["ds"]
    for key, img in (("sickle", "sk_img"), ("brick", "br_img")):
        t = ds[key].table
        ids, al = [str(x) for x in t["id"]], [str(a) for a in t[ds[key].aliases]]
        assert len(set(ids)) == len(ids) and len(set(al)) == len(al), key
        assert al == [str(int(r)) for r in t["rank"]]           # legacy #<rank> deep link
        assert list(t["rank"]) == sorted(t["rank"])
        items = [it.id for it in ds[img].items]
        assert items == ids


# -- (d) ------------------------------------------------------------------------------------
def test_inputs_exist_and_not_tmp(runs):
    paths, unmatched = expand_inputs(SITE.inputs)
    assert not unmatched, unmatched
    recs = [r for t in ("A", "B") for r in runs[t]["ctx"].inputs.records()]
    assert recs
    for p in paths + [r["path"] for r in recs]:
        assert os.path.exists(p), p
        rp = os.path.realpath(p)
        assert not any(rp == t or rp.startswith(t + os.sep) for t in TMP_ROOTS), p
    # the nested duplicate top-100 directory is never an input
    assert not any("sed_figures_top100_f1500w/sed_figures_top100_f1500w" in str(p) for p in paths)


# -- (e) ------------------------------------------------------------------------------------
def test_fixture_build_passes_check(built):
    assert built["res"].ok, built["res"].report.get("errors")
    rep = check_build(built["res"].staging, gate="G2")
    assert rep.ok, rep.errors
    for portal, report in built["reports"].items():
        assert report["tier"] == "B"
        assert [p["id"] for p in report["pages"]] == PAGES[portal]
        for pg in report["pages"]:
            assert pg["first_paint_bytes"] <= 2_000_000, (portal, pg["id"])


# -- (f) ------------------------------------------------------------------------------------
def test_tier_parity(runs):
    a, b = runs["A"]["ds"], runs["B"]["ds"]
    for key in ("sickle", "brick"):
        ta, tb = a[key].table, b[key].table
        assert list(ta["id"]) == list(tb["id"]) and list(ta["cls"]) == list(tb["cls"])
        assert [c.key for c in a[key].columns] == [c.key for c in b[key].columns]
        for c in b[key].columns:
            if c.role not in ("mag", "error", "value") or c.key == "sep":
                continue
            x, y = np.asarray(ta[c.key], float), np.asarray(tb[c.key], float)
            assert np.array_equal(np.isfinite(x), np.isfinite(y)), (key, c.key)
            ok = np.isfinite(x)
            assert np.allclose(x[ok], y[ok], atol=1e-3), (key, c.key)
        assert [(p.key, p.kind) for p in a[key].presets] == [(p.key, p.kind) for p in b[key].presets]
    for key in ("sk_hips", "br_hips"):
        assert a[key].base == b[key].base and a[key].fits == b[key].fits and a[key].target == b[key].target
    ma, mb = a["sickle"].manifold, b["sickle"].manifold
    assert ma.dims == mb.dims and ma.default_frame == mb.default_frame
    assert set(ma.frames) == set(mb.frames)
    for k in set(ma.frames) - {"pca"}:      # Tier B recomputes the PCA on the fixture rows
        assert ma.frames[k] == mb.frames[k], k
    assert a["flt"].keys == b["flt"].keys and a["flt"].extra == b["flt"].extra


# -- (g) ------------------------------------------------------------------------------------
def test_caps_superset_of_legacy(built):
    checked = 0
    for portal in SITE.portals:
        pages = {p["id"]: p for p in built["reports"][portal.id]["pages"]}
        for pc in portal.pages:
            enabled = set(pages[pc.id]["caps"]["enabled"])
            for leg in pc.legacy:
                need = set(INV[PREFIX[portal.id] + leg])
                assert need <= enabled, (pc.id, leg, sorted(need - enabled))
                checked += 1
    assert checked == 5


def test_port_table_caps_filled(built):
    rows = [r for r in PORT if r["family"] == FAMILY and not r["legacy_page"].startswith(WP23)]
    assert len(rows) == 5
    by_key = {PREFIX[portal.id] + leg: (portal.id, pc.id) for portal in SITE.portals for pc in portal.pages
              for leg in pc.legacy}
    for r in rows:
        portal, pid = by_key[r["key"]]
        pg = {p["id"]: p for p in built["reports"][portal]["pages"]}[pid]
        assert set(r["capabilities_to_fill"]) <= set(pg["caps"]["enabled"]), r["key"]
        assert set(r["flags"]) <= set(pg["flags"]), r["key"]


# -- special handling (section 14.8) ---------------------------------------------------------
def test_f2550w_overlay_url_through_exposure(runs):
    for tier in ("A", "B"):
        ds = runs[tier]["ds"]
        (f,) = ds["fits"]
        assert f["url"].startswith("https://data.rc.ufl.edu/pub/") and f["url"].endswith("/brick_f2550w_i2d.fits")
        assert f["auth"] == "public" and f["size"] > 1e7
        for key in ("sk_hips", "br_hips"):
            (lay,) = ds[key].fits
            assert set(lay) == {"id", "name", "url", "auth", "ra", "dec", "size_deg"}
            assert lay["url"] == f["url"]
            tg = ds[key].target
            assert abs(lay["ra"] - tg["ra"]) < 0.2 and abs(lay["dec"] - tg["dec"]) < 0.2
            assert all(layer["url"] for layer in ds[key].base) and sum(x["default"] for x in ds[key].base) == 1
    pages = _pages(runs)
    for k in (("sickle", "sickle_miri_sed_map"), ("brick2221", "f1500w_explorer")):
        sky = pages[k].layout["row"][1]["col"][0]
        assert sky["pane"] == "sky" and sky["options"]["fits"] == ["f2550w"]


def test_project_zero_points(runs):
    b = runs["B"]["adapter"]
    zp = runs["B"]["ctx"].legacy.module("Brick2221_F1500W_SEDPortal").FILTER_VEGA_ZP_JY
    extra = runs["B"]["ds"]["flt"].extra
    assert extra and all(k.startswith("JWST/MIRI.") for k in extra)
    for k, e in extra.items():
        assert e["zp_jy"] == pytest.approx(zp[k.split(".")[-1].lower()])
    assert b._zp
    assert all(k.startswith("JWST/") and "." in k for k in runs["B"]["ds"]["flt"].keys)


def test_extended_templates_and_k55(runs):
    ds = runs["B"]["ds"]
    assert ds["tpl26"].extend_to_um == 26.0
    assert 5.5 in ds["ext"].rv and 3.1 in ds["ext"].rv
    for (portal, pid), p in _pages(runs).items():
        if p.archetype in ("explorer", "manifold"):
            assert {"tpl26", "flt", "ext"} <= set(p.datasets), pid
            tpl = p.state["template"]
            assert tpl["key"] and tpl["bands"], pid
            assert all(re.fullmatch(r"F\d{3}[WMN]", b) and int(b[1:4]) < 500 for b in tpl["bands"]), tpl
            cat = ds["sickle" if portal == "sickle" else "brick"]
            mags = {c.label: c.filter for c in cat.columns if c.role == "mag"}
            assert all("NIRCam" in mags[b] for b in tpl["bands"])
            assert any("MIRI" in f for f in mags.values())


def test_seshat_bars_and_products(runs):
    ds = runs["B"]["ds"]
    for (portal, pid), p in _pages(runs).items():
        if p.archetype != "explorer":
            continue
        cat = ds["sickle" if portal == "sickle" else "brick"]
        det = p.layout["row"][2]["col"][0]
        assert det["pane"] == "detail"
        assert det["options"]["bars"] == [c.key for c in cat.columns if c.key.startswith("p_")]
        assert {"p_YSO", "p_FS"} <= set(det["options"]["bars"])
        loader = ds[det["options"]["products"]].loader
        doc = loader(str(cat.table["id"][0]))
        assert doc["schema"] == "products/1"
        kinds = {it["kind"] for it in doc["items"]}
        assert {"png", "fits"} <= kinds
        assert all(it["url"].startswith("https://") for it in doc["items"])
        assert loader("no-such-source") is None


def test_top100_role_facet_and_preset(runs):
    img = runs["B"]["ds"]["br_img"]
    t = runs["B"]["ds"]["brick"].table
    assert ("top100", "In the legacy top-100 gallery") in img.facets
    assert [p.key for p in img.presets] == ["top100"] and img.presets[0].data == {"facets": mod.TOP}
    top = {p.name for p in mod.TOP100_PNG.iterdir() if mod.PNG_RE.fullmatch(p.name)}
    for it, rank in zip(img.items, t["rank"]):
        assert it.roles["sed"].startswith("https://") and "/sed_figures_f1500w_all/" in it.roles["sed"]
        name = it.roles["sed"].rsplit("/", 1)[1]
        assert (it.tags["top100"] == "yes") == (name in top) == ("top100" in it.roles)
        if "top100" in it.roles:
            assert it.roles["top100"].endswith("/sed_figures_top100_f1500w/" + name)
        assert rank > 100 or it.tags["top100"] == "yes"
    for it in runs["B"]["ds"]["sk_img"].items:
        assert set(it.roles) == {"sed"} and "top100" not in it.tags
    # the top-100 stub opens the facet preset
    gal = {pc.id: pc for pc in SITE.portal("brick2221").pages}["f1500w_gallery"]
    stub = dict(kv.split("=", 1) for kv in gal.presets()["sed_figures_top100_f1500w/gallery.html"].split("&"))
    assert stub["role"] == "top100"
    assert json.loads(base64.urlsafe_b64decode(stub["fac"] + "=" * (-len(stub["fac"]) % 4))) == mod.TOP
    files = {f.file: f for f in gal.files}
    assert files["sed_figures_f1500w_all/gallery_legacy.html"].replace is False


def test_manifold_dims_and_frames(runs):
    for tier in ("A", "B"):
        cat = runs[tier]["ds"]["sickle"]
        m = cat.manifold
        assert "m_f444w" in m.dims and "m_f2550w" in m.dims
        assert not any(d.startswith(("o", "c_", "SeshatProb")) for d in m.dims)
        assert m.default_frame == "cmd"
        assert {"cmd", "mirccd", "class", "sky", "pca"} <= set(m.frames)
        virt = {v.key for v in cat.virtual}
        assert {d for d in m.dims if "-" in d} <= virt
        assert {g.key for g in cat.groups} == {"phot", "color", "class", "pos"}
        assert {p.key for p in cat.presets if p.kind == "frame"} == {"pca"}
        # legacy orientation: magnitudes bright-up and gl increasing to the left (negative legacy
        # scale), with exact +-1 weights so the pane's pairOfFrame still recognises the pair
        D = len(m.dims)
        w = lambda f, r, d: m.frames[f][r * D + m.dims.index(d)]  # noqa: E731
        assert w("cmd", 1, "m_f1500w") == -1.0 and w("cmd", 0, "m_f182m-m_f410m") == 1.0
        assert w("sky", 0, "gl") == -1.0 and w("sky", 1, "gb") == 1.0
        for f in set(m.frames) - {"pca"}:
            assert sum(abs(v) == 1.0 for v in m.frames[f]) == 2 and sum(v != 0 for v in m.frames[f]) == 2
    page = _pages(runs)[("sickle", "sickle_miri_sed_manifold")]
    assert page.manifold and page.state["frame:manifold"] == {"preset": "cmd"}


@pytest.mark.parametrize("tier", ["A", "B"])
def test_tiny_limit_raises_clear_error(tmp_path, tier):
    with pytest.raises(ValueError, match="--limit 5"):
        mod.SickleBrickAdapter().datasets(_ctx(tier, tmp_path, limit=1))


def test_pca_frame_from_legacy(runs, tmp_path):
    """Tier B runs the legacy pca_frame; over the full sample it reproduces the shipped weights."""
    full = {}
    for tier in ("A", "B"):
        ad = mod.SickleBrickAdapter()
        full[tier] = (ad, ad.datasets(_ctx(tier, tmp_path / tier, limit=None)))
    (aa, da), (ab, db) = full["A"], full["B"]
    assert len(db["sickle"].table) == 245 and len(db["brick"].table) == 241
    assert next(p for p in ab._raw["presets"] if p["id"] == "pca")["x"] is None      # computed in Tier B
    want = next(p for p in aa._raw["presets"] if p["id"] == "pca")["x"]
    note = next(p for p in db["sickle"].presets if p.key == "pca").data["note"]
    assert "PC1 carries" in note
    dims = db["sickle"].manifold.dims
    assert da["sickle"].manifold.dims == dims
    fa, fb = (np.asarray(d["sickle"].manifold.frames["pca"]).reshape(2, len(dims)) for d in (da, db))
    assert {dims[i] for i in np.flatnonzero(fb[0])} == set(want)
    assert np.allclose(fa, fb, rtol=2e-3, atol=2e-4)


def test_readme_counts_are_computed(runs):
    src = Path(mod.__file__).read_text(encoding="utf-8")
    assert not re.search(r"\b(245|241|183)\b", src)
    for key, rd in (("sickle", "sk_readme"), ("brick", "br_readme")):
        md = runs["B"]["ds"][rd].markdown
        t = runs["B"]["ds"][key].table
        assert f"{len(t):,} " in md and "Fixture build" in md
        for c in set(t["cls"]):
            assert f"{c} {int(np.sum(t['cls'] == c))}" in md
        assert "/orange/" not in md and "/blue/" not in md
        assert "26 um" in md or key == "sickle"


def test_filter_ids():
    assert mod._fid("f444w") == "JWST/NIRCam.F444W"
    assert mod._fid("F187N") == "JWST/NIRCam.F187N"
    assert mod._fid("f560w") == "JWST/MIRI.F560W"
    assert mod._fid("f2550w") == "JWST/MIRI.F2550W"
