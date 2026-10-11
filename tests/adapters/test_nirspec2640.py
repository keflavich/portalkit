"""Contract tests for the nirspec2640 adapter (ARCHITECTURE.md section 9.6, port table 14.4).

Fixture mode (``limit=50``) with a tmp stage root. The expensive reads (AladinMap
collect/attach, the manifold build, the Wd2 catalogue, x1d/s2d products, MIST) are memoised
in the portalkit cache (``$PK_CACHE_DIR`` or ``~/.cache/portalkit``), so only the first run
after a data change pays for them.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote

import numpy as np
import pytest

from portalkit.build import build_family, check_build
from portalkit.context import BuildContext, expand_inputs, load_site
from portalkit.deploy import ExposureMap
from portalkit.spec import (CatalogSpec, HipsSpec, Image2D, ImageSetSpec, IsochroneSpec, OverlaySpec,
                            PageSpec, ReadmeSpec, RecordSetSpec, Spectrum, TierUnavailable)

REPO = Path(__file__).resolve().parents[2]
LIMIT = 50
FAMILY = "nirspec2640"
INV = json.loads((REPO / "tests" / "data" / "inventory_caps.json").read_text(encoding="utf-8"))
TMP_ROOTS = tuple(os.path.realpath(p) for p in {tempfile.gettempdir(), "/tmp", "/var/tmp"})
PAGES = ["index", "nirspec_2640_map", "nirspec_2640_colorcolor", "nirspec_2640_specviewer",
         "nirspec_2640_red_excess", "nirspec_2640_manifold", "nirspec_2640_icecolumns",
         "nirspec_2640_pvgallery", "nirspec_2640_sedgallery", "gallery_2640"]

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from adapters import nirspec2640 as mod  # noqa: E402

SITE = load_site(FAMILY)
pytestmark = [pytest.mark.adapters,
              pytest.mark.skipif(not (mod.SRC_DIR / "nirspec_2640_sources.json").is_file(),
                                 reason="legacy nirspec_2640 tree not mounted")]


def _ctx(tier: str, staging: Path, limit=LIMIT) -> BuildContext:
    return BuildContext(family=FAMILY, site=SITE, staging=staging, tier=tier, limit=limit,
                        log=lambda m: None)


def _f(col) -> np.ndarray:
    return np.asarray(col.filled(np.nan) if hasattr(col, "filled") else col, float)


def _panes(layout) -> dict:
    out = {}

    def walk(n):
        if isinstance(n, dict):
            if "pane" in n:
                out.setdefault(n.get("id") or n["pane"], n)
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(layout)
    return out


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """datasets() + pages() for both tiers (timed: contract 9.6(b))."""
    out = {}
    for tier in ("A", "B"):
        ctx = _ctx(tier, tmp_path_factory.mktemp(f"ctx{tier}"))
        t0 = time.perf_counter()
        ds = mod.ADAPTER.datasets(ctx)
        dt = time.perf_counter() - t0
        out[tier] = {"ctx": ctx, "ds": ds, "pages": mod.ADAPTER.pages(ctx, ds), "dt": dt}
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
    return {"res": res, "portal": res.staging / FAMILY}


def _report(built) -> dict:
    return json.loads((built["portal"] / "build_report.json").read_text(encoding="utf-8"))


# -- (a) ------------------------------------------------------------------------------------
def test_import_has_no_side_effects(tmp_path):
    code = ("import sys; sys.path.insert(0, %r); import adapters.nirspec2640 as m; "
            "assert m.ADAPTER.name == 'nirspec2640'" % str(REPO))
    subprocess.run([sys.executable, "-c", code], cwd=tmp_path, check=True, timeout=120)
    assert list(tmp_path.iterdir()) == []


# -- (b) ------------------------------------------------------------------------------------
@pytest.mark.parametrize("tier", ["A", "B"])
def test_datasets_valid_and_fast(runs, tier):
    r = runs[tier]
    assert r["dt"] < 120, f"datasets() took {r['dt']:.1f} s"
    ds = r["ds"]
    assert isinstance(ds["src"], CatalogSpec) and isinstance(ds["bg"], CatalogSpec) and ds["bg"].context
    assert isinstance(ds["redx"], CatalogSpec) and not ds["redx"].context
    for k, kind in (("spec", "pks1"), ("s2d", "pki1"), ("prod", "json")):
        assert isinstance(ds[k], RecordSetSpec) and ds[k].kind == kind and ds[k].catalog == "src"
    for k in ("fig", "pv", "sed", "pngs"):
        assert isinstance(ds[k], ImageSetSpec) and ds[k].catalog == "src"
    assert isinstance(ds["iso"], IsochroneSpec) and isinstance(ds["hips"], HipsSpec)
    assert isinstance(ds["ovl"], OverlaySpec) and "rovl" not in ds
    assert isinstance(ds["readme"], ReadmeSpec)
    assert len(ds["src"].table) == LIMIT
    assert ds["src"].manifold is not None and ds["bg"].density and ds["redx"].density
    for p in r["pages"]:
        assert isinstance(p, PageSpec)
        for k in p.datasets:
            assert k in ds, (p.id, k)
        assert sum(ds[k].__class__ is OverlaySpec for k in p.datasets) <= 1, p.id   # one per page
    assert [p.id for p in r["pages"]] == PAGES
    assert [pc.id for pc in SITE.portal().pages] == PAGES


def test_tier_c_unavailable(tmp_path):
    with pytest.raises(TierUnavailable):
        mod.NIRSpec2640Adapter().datasets(_ctx("C", tmp_path))


# -- (c) ------------------------------------------------------------------------------------
@pytest.mark.parametrize("tier", ["A", "B"])
def test_ids_and_aliases_unique(runs, tier):
    ds = runs[tier]["ds"]
    ids = [str(x) for x in ds["src"].table["name"]]
    assert len(set(ids)) == len(ids) and all(re.fullmatch(r"2640_\d+", i) for i in ids)
    al = [a for v in ds["src"].aliases.values() for a in v]
    assert len(set(al)) == len(al) and not set(al) & set(ids)
    for k in ("bg", "redx"):
        other = [str(x) for x in ds[k].table["id"]]
        assert len(set(other)) == len(other) and not set(other) & set(ids), k
    for k in ("fig", "pv", "sed", "pngs"):
        items = [it.id for it in ds[k].items]
        assert len(set(items)) == len(items) and set(items) <= set(ids), k
    for k in ("spec", "s2d", "prod"):
        assert ds[k].ids == ids


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


# -- (e) ------------------------------------------------------------------------------------
def test_fixture_build_passes_check(built):
    assert built["res"].ok, built["res"].report.get("errors")
    rep = check_build(built["res"].staging, gate="G2")
    assert rep.ok, rep.errors
    report = _report(built)
    assert report["tier"] == "B"
    assert [p["id"] for p in report["pages"]] == PAGES
    for pg in report["pages"]:
        assert pg["first_paint_bytes"] <= 2_000_000, pg["id"]
    bg = json.loads((built["portal"] / "data/bg/manifest.json").read_text(encoding="utf-8"))
    assert "rows" not in bg
    src = built["portal"] / "data/src/manifest.json"
    assert src.stat().st_size < 262_144
    for k in ("spec", "s2d", "prod"):
        assert report["budgets"]["datasets"][k]["first_paint_bytes"] == 0, k


def test_public_portal_secure_products(built):
    """Public portal: figures from the public tree; FITS products badged secure links."""
    portal = json.loads((built["portal"] / "portal.json").read_text(encoding="utf-8"))
    assert portal["visibility"] == "public" and SITE.portal().visibility == "public"
    assert SITE.portal().url == mod.PUB + "unified/nirspec2640/"
    assert SITE.portal().target_dir.endswith("wd2/nirspec_2640/unified/nirspec2640")
    for k in ("fig", "pv", "sed", "pngs"):
        doc = json.loads((built["portal"] / "data" / k / "images.json").read_text(encoding="utf-8"))
        urls = [r["url"] for it in doc["items"] for r in it["roles"].values()]
        assert urls and all(u.startswith(mod.PUB) for u in urls), k
    text = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                     for p in built["portal"].rglob("*")
                     if p.suffix in (".json", ".html") and p.name != "build_report.json")
    assert "../mastDownload" not in text and "/orange/" not in text


def test_exposure_policy(built):
    from portalkit.deploy.deploy import collect_references
    from portalkit.deploy.exposure import Reference, check_references
    files = [str(p.relative_to(built["portal"])) for p in built["portal"].rglob("*")
             if p.is_file() and p.suffix in (".html", ".json")]
    refs = [Reference(url=u, source=s, fetched=f) for s, u, f in collect_references(built["portal"], files)]
    assert not [r for r in refs if r.fetched and r.url.startswith(mod.SECURE)]   # nothing secure is fetched
    dec = check_references("public", refs)
    assert dec.exit_code == 0, dec


# -- (f) ------------------------------------------------------------------------------------
def test_tier_parity(runs):
    a, b = runs["A"]["ds"], runs["B"]["ds"]
    ta, tb = a["src"].table, b["src"].table
    assert list(ta["name"]) == list(tb["name"]) and ta.colnames == tb.colnames
    for k in ("ra", "dec"):
        assert np.max(np.abs(_f(ta[k]) - _f(tb[k]))) < 1e-5
    checked = 0
    for c in b["src"].columns:
        if c.role not in ("mag", "value") or c.key not in ta.colnames:
            continue
        x, y = _f(ta[c.key]), _f(tb[c.key])
        assert np.array_equal(np.isfinite(x), np.isfinite(y)), c.key
        ok = np.isfinite(x)
        assert np.allclose(x[ok], y[ok], rtol=1e-3, atol=2e-3), c.key       # legacy rounds to 1e-4
        checked += 1
    assert checked > 100
    for k in ("Gratings", "Observation", "l_col_H2O", "l_col_CO2"):
        assert list(ta[k]) == list(tb[k]), k
    assert a["hips"].base == b["hips"].base and a["hips"].overlays == b["hips"].overlays
    assert a["hips"].target == b["hips"].target
    for k in ("fig", "pv", "sed", "pngs"):
        assert [(it.id, it.roles) for it in a[k].items] == [(it.id, it.roles) for it in b[k].items], k
    for k in ("bg", "redx"):
        assert len(a[k].table) == len(b[k].table) and [d.key for d in a[k].density] == [d.key for d in b[k].density]
    # spectra: legacy specdata (merged, nJy, 1e-5 um) split by the x1d grids vs x1d + clean_flux
    multi = [str(n) for n, g in zip(ta["name"], ta["Gratings"]) if "," in g]
    single = [str(n) for n, g in zip(ta["name"], ta["Gratings"]) if "," not in g]
    assert multi and single
    for name in multi[:3] + single[:2]:
        sa, sb = a["spec"].loader(name), b["spec"].loader(name)
        assert sa.segments == sb.segments, name
        assert np.allclose(sa.wave, sb.wave, atol=6e-6), name
        fa, fb = sa.channels["flux"], sb.channels["flux"]
        assert np.array_equal(np.isfinite(fa), np.isfinite(fb)), name
        ok = np.isfinite(fa)
        assert np.allclose(fa[ok], fb[ok], rtol=1e-3, atol=1e-11), name


# -- (g) ------------------------------------------------------------------------------------
def test_caps_superset_of_legacy(built):
    pages = {p["id"]: p for p in _report(built)["pages"]}
    checked = 0
    for pc in SITE.portal().pages:
        enabled = set(pages[pc.id]["caps"]["enabled"])
        for leg in pc.legacy:
            need = set(INV[f"{FAMILY}/{leg}"])
            assert need <= enabled, (pc.id, leg, sorted(need - enabled))
            checked += 1
    assert checked == 11


def test_port_table_caps_filled(built):
    pages = {p["id"]: p for p in _report(built)["pages"]}
    by_legacy = {leg: pc.id for pc in SITE.portal().pages for leg in pc.legacy}
    rows = [r for r in json.loads((REPO / "tests" / "data" / "port_table.json").read_text(encoding="utf-8"))
            if r["family"] == FAMILY]
    assert len(rows) == 11 and {r["legacy_page"] for r in rows} == set(by_legacy)
    for r in rows:
        pg = pages[by_legacy[r["legacy_page"]]]
        assert set(r["capabilities_to_fill"]) <= set(pg["caps"]["enabled"]), \
            (r["legacy_page"], sorted(set(r["capabilities_to_fill"]) - set(pg["caps"]["enabled"])))
        assert set(r["flags"]) <= set(pg["flags"]), (r["legacy_page"], sorted(set(r["flags"]) - set(pg["flags"])))


def test_landing_cards(built):
    from portalkit.legacy.extract import script_json
    page = script_json(built["portal"] / "index.html", "pk-page")
    assert page["page"]["title"] == "NIRSpec 2640 (Westerlund 2) portal"
    cards = page["layout"]["options"]["cards"]
    assert {c["page"] for c in cards if "page" in c} == set(PAGES) - {"index"}
    ext = [c for c in cards if "href" in c]
    assert ext and all(c["auth"] == "public" for c in ext)
    import tomllib
    wd2 = tomllib.loads((REPO / "sites" / "wd2.toml").read_text(encoding="utf-8"))
    assert wd2["portal"][0]["url"] == mod.WD2_PORTAL
    wd2_pages = {pg["id"] for pg in wd2["portal"][0]["page"]}
    sister = [c["href"] for c in ext if c["href"].startswith(mod.PUB)]
    assert len(sister) == len(mod.WD2_CARDS)                  # the unified wd2 pages, not the legacy copies
    for h in sister:
        assert h.startswith(mod.WD2_PORTAL) and h[len(mod.WD2_PORTAL):-len(".html")] in wd2_pages, h


def test_superseded_legacy_files_recorded():
    """Every legacy 2640 file in the public tree maps to a unified page; the drift note stays."""
    legacy = {leg for pc in SITE.portal().pages for leg in pc.legacy}
    on_disk = {p.name for p in mod.PUB_DIR.glob("nirspec_2640_*.html")} | {"index.html"}
    on_disk |= {f"pngs/{p.name}" for p in (mod.PUB_DIR / "pngs").glob("gallery_2640*.html")}
    assert on_disk == legacy
    toml = (REPO / "sites" / "nirspec2640.toml").read_text(encoding="utf-8")
    assert "nirspec_2640_colorcolor.html" in toml and "exists only in the public tree" in toml
    presets = {(f.file, f.preset) for pc in SITE.portal().pages for f in pc.files}
    assert {("pngs/gallery_2640_nb.html", "role=nb"), ("pngs/gallery_2640_bb.html", "role=bb")} <= presets


def test_titles_are_specific(runs):
    titles = [p.title for p in runs["B"]["pages"]]
    assert len(set(titles)) == len(titles)
    assert "Wd2 Ice Manifold" not in titles and "Property manifold" in titles


# -- records ---------------------------------------------------------------------------------
@pytest.mark.parametrize("tier", ["A", "B"])
def test_records(runs, tier):
    ds = runs[tier]["ds"]
    t = ds["src"].table
    name = next(str(n) for n, g in zip(t["name"], t["Gratings"]) if "," in g)
    sp = ds["spec"].loader(name)
    assert isinstance(sp, Spectrum)
    w, f, e = sp.wave, sp.channels["flux"], sp.channels["err"]
    assert len(f) == len(w) == len(e)
    assert np.all(np.isnan(e[~np.isfinite(f)]))
    assert np.nanmax(f) <= 1.0 and np.nanmin(f) >= 0
    assert [s[2] for s in sp.segments] == ["PRISM", "G140M", "G395M"]
    assert sp.segments[0][0] == 0 and sp.segments[-1][1] == len(w)
    for i0, i1, _ in sp.segments:                # ascending per segment
        assert np.all(np.diff(w[i0:i1]) > 0)
    assert any(ln["kind"] == "ice" and not ln["detected"] for ln in sp.lines)
    im = ds["s2d"].loader(name)
    assert isinstance(im, Image2D)
    assert im.data.shape == (len(im.y), len(im.wave)) and np.all(np.diff(im.wave) > 0)
    assert [s[2] for s in im.segments][:3] == ["PRISM", "G140M", "PRISM"]
    pr = ds["prod"].loader(name)
    assert pr["schema"] == "products/1"
    fits = [it for it in pr["items"] if it["kind"] == "fits"]
    assert {it["group"] for it in fits} == {"PRISM", "G140M", "G395M"} and len(fits) == 6
    assert all(it["auth"] == "secure" and it["url"].startswith(mod.SECURE) and it["size"] > 0 for it in fits)
    assert all(it["auth"] == "public" for it in pr["items"] if it["kind"] != "fits")
    assert sum(it["kind"] == "mast" for it in pr["items"]) == 1
    assert ds["spec"].loader("no_such_source") is None and ds["s2d"].loader("2640_x") is None
    assert ds["prod"].loader("") is None


# -- red excess / colour-colour --------------------------------------------------------------
def test_redx_catalog_and_stat_grids(runs):
    rx = runs["B"]["ds"]["redx"]
    assert len(rx.table) == 20 * LIMIT
    keys = {d.key: d for d in rx.density}
    for k, (a, b), _ in mod.Y_OPTIONS:
        n, s = keys[f"{k}_n"], keys[f"{k}_rwd2"]
        assert n.stat is None and s.stat == "median:r_wd2"
        assert (n.x, n.y) == (s.x, s.y) == ("f115w-f200w", f"{a}-{b}")
        assert s.xr[0] < s.xr[1] and s.yr[0] < s.yr[1]
    r = _f(rx.table["r_wd2"])
    assert np.all(np.isfinite(r)) and np.all(r >= 0)
    x = _f(rx.table["f115w"]) - _f(rx.table["f200w"])
    assert np.array_equal(np.asarray(rx.table["foreground"], bool), x < mod.FOREGROUND_X_CUT)
    page = next(p for p in runs["B"]["pages"] if p.id == "nirspec_2640_red_excess")
    pn = _panes(page.layout)
    assert pn["rwd2"]["dataset"] == "redx" and pn["rwd2"]["options"]["y"] == "f182m-f212n"
    assert "ovl" in page.datasets and page.state == {"color": {"by": "r_wd2"}}
    # NIRSpec sources over the legacy median-r_wd2 PNGs (one per y colour, legacy extents)
    ras = pn["redx"]["options"]["raster"]
    assert [r["y"] for r in ras] == [f"{a}-{b}" for _, (a, b), _ in mod.Y_OPTIONS]
    for r in ras:
        assert r["x"] == "f115w-f200w" and r["url"].startswith(mod.PUB) and r["url"].endswith("_rwd2.png")
        assert (mod.PUB_DIR / r["url"][len(mod.PUB):]).is_file()
        assert r["extent"][0] < r["extent"][1] and r["extent"][2] < r["extent"][3]
    # offset pane: the NIRSpec sources (src p_<k>, r_wd2) over the redx catalogue backdrop
    off = pn["offset"]
    assert off["dataset"] == "src" and off["options"]["background"] == "redx"
    assert (off["options"]["x"], off["options"]["y"]) == ("p_y182212", "r_wd2")
    src = runs["B"]["ds"]["src"]
    have = set(src.table.colnames)
    for k, _, _ in mod.Y_OPTIONS:
        assert {f"p_{k}", "r_wd2"} <= have and f"p_{k}_r" in keys
        assert next(p for p in src.presets if p.key == f"p_{k}").data == {"x": f"p_{k}", "y": "r_wd2"}
    r = _f(src.table["r_wd2"])
    ra, dec = _f(src.table["ra"]), _f(src.table["dec"])
    assert np.all(np.isfinite(r)) and np.allclose(r, mod._sep_arcmin(ra, dec), atol=2e-4)


def test_redx_slopes_and_offsets(runs):
    """Tier B slopes (G23 Rv = 3.1 at the legacy pivots) match the legacy payload slopes (5 d.p.)."""
    from portalkit.legacy import extract
    leg = {e["name"]: e["value"] for e in extract.payloads(mod.SRC_DIR / "nirspec_2640_red_excess.html",
                                                            names=("DIAGRAMS",))}["DIAGRAMS"]
    for d in leg:
        a, b = next(p for k, p, _ in mod.Y_OPTIONS if k == d["key"])
        assert mod.vector_slope(a, b) == pytest.approx(d["slope"], abs=1e-4), d["key"]
    # both tiers give the same offsets (Tier A: legacy payload rounded to 1e-4)
    ta, tb = runs["A"]["ds"]["src"].table, runs["B"]["ds"]["src"].table
    for k, _, _ in mod.Y_OPTIONS:
        x, y = _f(ta[f"p_{k}"]), _f(tb[f"p_{k}"])
        ok = np.isfinite(x)
        assert np.array_equal(ok, np.isfinite(y)) and np.max(np.abs(x[ok] - y[ok]), initial=0) < 2e-4, k
    # perp_offset: zero on the vector, sign follows the legacy convention (+ above the line)
    assert mod.perp_offset(1.0, 0.16, 0.16) == pytest.approx(0.0)
    assert mod.perp_offset(0.0, 1.0, 0.16) > 0 > mod.perp_offset(0.0, -1.0, 0.16)
    assert mod.perp_offset(np.array([np.nan, 1e30]), np.array([0.0, 0.0]), 0.1)[1] < -1e28


def test_live_sed_uses_synthetic_filters(runs):
    """s_<f> columns carry filter keys (sedPoints skips mag columns without one) and the filter
    set covers the synthetic-only bands (port table 14.4: all synthetic filters in the live SED)."""
    ds = runs["B"]["ds"]
    cols = {c.key: c for c in ds["src"].columns}
    for f in mod.CAT_FILTERS + mod.SYNTH_ONLY_FILTERS:
        assert cols[f"s_{f}"].filter == mod._fid(f) and cols[f"s_{f}"].role == "mag", f
    assert set(ds["flt"].keys) == {mod._fid(f) for f in mod.CAT_FILTERS + mod.SYNTH_ONLY_FILTERS}


def test_colorcolor_pairs_and_bd_split(runs):
    page = next(p for p in runs["B"]["pages"] if p.id == "nirspec_2640_colorcolor")
    pn = _panes(page.layout)
    ccd = pn["ccd"]["options"]
    assert ccd["presets"] is False and ccd["background"] == "bg"
    assert ccd["pairs"] == {"x2": "s_f182m-s_f212n", "y2": "s_f405n-s_f466n"}
    assert pn["bd"]["pane"] == "hist" and pn["bd"]["options"]["expr"] == "s_f444w" and pn["bd"]["options"]["brush"]
    src = runs["B"]["ds"]["src"]
    pre = {p.key: p.data for p in src.presets}
    for k, _, _ in mod.CMDS + mod.DIAGRAMS:
        assert pre[k]["pairs"]["x2"] == "-".join("s_" + t for t in pre[k]["x"].split("-")), k
    assert pre["synthlw"] == {"x": "s_f162m-s_f210m", "y": "s_f360m-s_f480m"}
    cols = {c.key for c in src.columns} | {v.key for v in src.virtual}
    for p in src.presets:
        if p.kind == "axes" and isinstance(p.data.get("x"), str):
            for e in (p.data["x"], p.data["y"]):
                assert all(t in cols for t in e.split("-")), (p.key, e)
    bg = runs["B"]["ds"]["bg"]
    assert {d.key for d in bg.density} == {k for k, _, _ in mod.CMDS + mod.DIAGRAMS + mod.Y_OPTIONS}


@pytest.mark.parametrize("tier", ["A", "B"])
def test_ice_columns_limits(runs, tier):
    src = runs[tier]["ds"]["src"]
    cols = {c.key: c for c in src.columns}
    for sp in mod.SPECIES:
        assert cols[f"col_{sp}"].limit == f"l_col_{sp}" and cols[f"l_col_{sp}"].role == "limit"
        assert cols[f"col_{sp}"].err == f"e_col_{sp}"
    codes = {str(v) for sp in mod.SPECIES for v in src.table[f"l_col_{sp}"]}
    assert codes <= {"", "l", "u"} and "u" in codes
    page = next(p for p in runs[tier]["pages"] if p.id == "nirspec_2640_icecolumns")
    ice = _panes(page.layout)["ice"]["options"]
    assert ice["xlog"] and ice["ylog"] and ice["errorbars"] and ice["overlays"] == ["one_to_one"]


def test_ice_kind_cases():
    base = {f"X_{k}": v for k, v in (("limit", "value"), ("N", 1e17), ("saturated", False),
                                       ("detected", False), ("tau_int_err", 0.2), ("N_err", 1e16))}
    mod.ICE_A["X"] = 1e-17
    try:
        r = dict(base)
        assert mod.ice_kind(r, "X") == (pytest.approx(3 * 0.2 / 1e-17), None, "up")
        assert mod.ice_kind({**r, "X_detected": True}, "X") == (1e17, 1e16, "v")
        assert mod.ice_kind({**r, "X_saturated": True}, "X") == (1e17, None, "lo")
        assert mod.ice_kind({**r, "X_limit": "bad"}, "X") == (None, None, None)
        assert mod.ice_kind({**r, "X_tau_int_err": np.nan}, "X") == (None, None, None)
        assert mod.ice_kind({**r, "X_tau_int_err": -1.0}, "X") == (None, None, None)
        assert mod.ice_kind({**r, "X_detected": True, "X_N": np.inf}, "X") == (None, None, None)
        assert mod.ice_kind({**r, "X_saturated": True, "X_N": 1e300}, "X")[0] == 1e300
    finally:
        mod.ICE_A.pop("X")


# -- edge cases: N = 0 / 1, NaN / inf, unicode, large values ----------------------------------
@pytest.mark.parametrize("limit", [0, 1])
def test_small_n(tmp_path, limit):
    ad = mod.NIRSpec2640Adapter()
    ctx = _ctx("A", tmp_path, limit=limit)
    ds = ad.datasets(ctx)
    pages = ad.pages(ctx, ds)
    assert len(ds["src"].table) == limit and len(ds["bg"].table) == 20 * limit
    assert len(ds["redx"].table) == 20 * limit
    assert [p.id for p in pages] == PAGES
    for k in ("fig", "pv", "pngs"):
        assert len(ds[k].items) == limit, k
    if limit == 0:
        assert ds["hips"].target is None and ds["src"].manifold is None
        assert not [p for p in ds["src"].presets if p.key.startswith("ice_")]
        assert all(d.xr == (0.0, 1.0) for d in ds["redx"].density)
    else:
        assert ds["hips"].target["fov"] == 0.03
        assert ds["src"].table["hst_f555w"].dtype.kind == "f"            # all-missing stays numeric


def test_helpers_nan_inf_unicode_large():
    assert mod._num(None) is None and mod._num("x") is None and mod._num("") is None
    assert mod._num(np.inf) is None and mod._num(np.nan) is None
    assert mod._num(np.ma.masked) is None and mod._num("1e300") == 1e300
    u = mod._surl("/orange/adamginsburg/jwst/spectra/mastDownload/JWST/jw02640 é/α_s2d.fits")
    assert u.startswith(mod.SECURE) and " " not in u and "%C3%A9" in u
    with pytest.raises(ValueError):
        mod._surl("/orange/adamginsburg/web/public/x.png")
    assert mod._pub(None) is None and mod._pub("") is None and mod._pub("pngs/é_missing.png") is None
    png = next((mod.PUB_DIR / "pngs").glob("*.png"))
    assert mod._pub(f"pngs/{png.name}") == mod.PUB + "pngs/" + png.name.replace(" ", "%20")
    assert '"freeText": "jw02640-o001%"' in unquote(mod._mast("001"))
    assert mod._sep_arcmin(*mod.WD2_CENTER) == pytest.approx(0.0, abs=1e-9)
    assert mod._sep_arcmin(mod.WD2_CENTER[0], mod.WD2_CENTER[1] + 1.0) == pytest.approx(60.0)
    assert np.isnan(mod._sep_arcmin(np.nan, 0.0))
    assert mod.mag_of({"cat_f": 12.0, "synth_f": 13.0}, "f") == 12.0
    assert mod.mag_of({"cat_f": None, "synth_f": 13.0}, "f") == 13.0
    assert np.isnan(mod.mag_of({"cat_f": np.inf}, "f"))
    lo = mod.robust_limits(np.arange(100.0), np.arange(100.0) * 1e30, np.ones(100, bool))
    assert lo[0] < lo[1] and lo[2] < lo[3] and np.isfinite(lo).all()
    assert mod.robust_limits(np.ones(3), np.ones(3), np.zeros(3, bool)) == (0.0, 1.0, 0.0, 1.0)


def test_catalog_good_cut():
    n = 6
    d = {"mag_vega_f": np.array([10.0, np.nan, 11.0, 12.0, 13.0, 14.0]),
         "emag_ab_f": np.array([0.01, 0.01, 0.2, 0.01, np.inf, 0.05]),
         "mask_f": np.array([False, False, False, True, False, False]),
         "is_saturated_f": np.array([False, False, False, False, False, True])}
    assert list(mod.catalog_good(d, "f")) == [True, False, False, False, False, False]
    assert mod.catalog_good(d, "f").size == n
    with pytest.raises(KeyError):                     # legacy semantics: missing band columns raise
        mod.catalog_good(d, "g")


def test_overlays_and_hips_shape(runs):
    for tier in ("A", "B"):
        ds = runs[tier]["ds"]
        lay = {l["id"]: l for l in ds["ovl"].layers}
        assert set(lay) == {"msa_shutters", "dither", "redvec", "fg_cut", "p_zero", "one_to_one"}
        for l in lay.values():
            assert {"id", "name", "space", "kind", "style", "default", "items"} <= set(l)
        for k in ("msa_shutters", "dither"):
            assert lay[k]["space"] == "sky" and lay[k]["items"]
            for it in lay[k]["items"]:
                pts = np.asarray(it["poly"], float)
                assert pts.shape[1] == 2 and np.all(np.abs(pts[:, 0] - 156.0) < 1) and np.all(np.abs(pts[:, 1] + 57.76) < 1)
        assert [(it["x"], it["y"]) for it in lay["p_zero"]["items"]] == [(f"p_{k}", "r_wd2") for k, _, _ in mod.Y_OPTIONS]
        assert all(b["url"].startswith("https://") for b in ds["hips"].base)
        assert sum(b["default"] for b in ds["hips"].base) == 1
        assert len({b["id"] for b in ds["hips"].base}) == len(ds["hips"].base)
        assert ds["hips"].overlays and all(0 < o["opacity"] <= 1 for o in ds["hips"].overlays)


def test_png_gallery_roles(runs):
    ds = runs["B"]["ds"]
    roles = [r[0] for r in ds["pngs"].roles]
    assert roles == ["all", "nb", "bb"]
    for it in ds["pngs"].items[:5]:
        assert it.roles["nb"].endswith("_nb.png") and it.roles["bb"].endswith("_bb.png")
    assert {p.key for p in ds["pngs"].presets} == {"prism", "prism_g140m_g395m"}


def test_no_hard_coded_counts(runs):
    src = Path(mod.__file__).read_text(encoding="utf-8")
    assert not re.search(r"\b131\b|\b37956\b|\b225\b", src)
    md = runs["B"]["ds"]["readme"].markdown
    assert f"{LIMIT} sources" in md and f"first {LIMIT} sources" in md
    t = runs["B"]["ds"]["src"].table                   # detection counts cover the listed rows only
    for sp in ("H2O", "CO2"):
        n = sum(1 for v, l in zip(_f(t[f"col_{sp}"]), t[f"l_col_{sp}"]) if np.isfinite(v) and str(l) == "")
        assert re.search(rf"[ ,]{sp} {n}[,.]", md), sp


def test_loaders_bound_to_their_call(runs, tmp_path):
    ds_a = runs["A"]["ds"]
    ids = list(ds_a["spec"].ids)
    before = [len(ds_a["spec"].loader(i).wave) for i in ids[:3]]
    mod.ADAPTER.datasets(_ctx("A", tmp_path, limit=2))
    assert [len(ds_a["spec"].loader(i).wave) for i in ids[:3]] == before
    assert ds_a["prod"].loader(ids[2]) is not None


def test_links_and_grammars(runs):
    pages = {p.id: p for p in runs["B"]["pages"]}
    links = pages["nirspec_2640_manifold"].links
    assert {"map", "spectrum", "ice", "pv", "sed", "redx", "colorcolor"} <= set(links)
    assert links["ice"] == "nirspec_2640_icecolumns.html#id={id}"
    gram = {pc.id: pc.grammar for pc in SITE.portal().pages}
    for pid in ("nirspec_2640_map", "nirspec_2640_colorcolor", "nirspec_2640_red_excess",
                "nirspec_2640_pvgallery", "nirspec_2640_sedgallery"):
        assert gram[pid] == ["bare-name"], pid
    assert gram["nirspec_2640_specviewer"] == ["specviewer"]
    assert gram["nirspec_2640_manifold"] == ["kv-view", "bare-name"]
    from portalkit.legacy.hashmap import map_legacy
    assert map_legacy(gram["nirspec_2640_map"], {"hash": "#2640_1058", "search": ""})["hash"] == {"id": "2640_1058"}
    assert map_legacy(gram["nirspec_2640_manifold"], {"hash": "#star=2640_1058", "search": ""})["hash"]["id"] == "2640_1058"
