"""Contract tests for the nirspec6927 adapter (ARCHITECTURE.md section 9.6, port table 14.7).

Fixture mode (``limit=50``) with a tmp stage root. The expensive reads (AladinMap
collect/attach, the Brick catalogue, x1d/s2d products, MIST) are memoised in the portalkit
cache (``$PK_CACHE_DIR`` or ``~/.cache/portalkit``), so only the first run after a data
change pays for them.
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

import numpy as np
import pytest

from portalkit.build import build_family, check_build
from portalkit.context import BuildContext, expand_inputs, load_site
from portalkit.deploy import ExposureMap
from portalkit.spec import (CatalogSpec, HipsSpec, Image2D, ImageSetSpec, IsochroneSpec, OverlaySpec,
                            PageSpec, ReadmeSpec, RecordSetSpec, Spectrum, TierUnavailable)

REPO = Path(__file__).resolve().parents[2]
LIMIT = 50
FAMILY = "nirspec6927"
INV_FAMILY = "nirspec-6927"       # key used by inventory_caps.json and port_table.json
INV = json.loads((REPO / "tests" / "data" / "inventory_caps.json").read_text(encoding="utf-8"))
TMP_ROOTS = tuple(os.path.realpath(p) for p in {tempfile.gettempdir(), "/tmp", "/var/tmp"})
PAGES = ["index", "nirspec_6927_map", "nirspec_6927_manifold", "nirspec_6927_specviewer",
         "nirspec_6927_colorcolor", "nirspec_6927_icecolumns", "nirspec_6927_cogas",
         "nirspec_6927_pvgallery", "nirspec_6927_sedgallery", "gallery_6927"]

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from adapters import nirspec6927 as mod  # noqa: E402

SITE = load_site(FAMILY)
pytestmark = [pytest.mark.adapters,
              pytest.mark.skipif(not (mod.LEGACY_DIR / "nirspec_6927_sources.json").is_file(),
                                 reason="legacy nirspec_6927 tree not mounted")]


def _top_snr(table) -> str:
    v = [(mod._num(x), str(n)) for x, n in zip(table["comb_snr"], table["name"])]
    return max(p for p in v if p[0] is not None)[1]


def _ctx(tier: str, staging: Path, limit=LIMIT) -> BuildContext:
    return BuildContext(family=FAMILY, site=SITE, staging=staging, tier=tier, limit=limit,
                        log=lambda m: None)


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """datasets() + pages() for both tiers (timed: contract 9.6(b))."""
    out = {}
    for tier in ("A", "B"):
        ctx = _ctx(tier, tmp_path_factory.mktemp(f"ctx{tier}"))
        adapter = mod.ADAPTER            # the shared singleton serves both tiers (closure loaders)
        t0 = time.perf_counter()
        ds = adapter.datasets(ctx)
        dt = time.perf_counter() - t0
        out[tier] = {"ctx": ctx, "ds": ds, "pages": adapter.pages(ctx, ds), "dt": dt, "ad": adapter}
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
    code = ("import sys; sys.path.insert(0, %r); import adapters.nirspec6927 as m; "
            "assert m.ADAPTER.name == 'nirspec6927'" % str(REPO))
    subprocess.run([sys.executable, "-c", code], cwd=tmp_path, check=True, timeout=120)
    assert list(tmp_path.iterdir()) == []


# -- (b) ------------------------------------------------------------------------------------
@pytest.mark.parametrize("tier", ["A", "B"])
def test_datasets_valid_and_fast(runs, tier):
    r = runs[tier]
    assert r["dt"] < 120, f"datasets() took {r['dt']:.1f} s"
    ds = r["ds"]
    assert isinstance(ds["src"], CatalogSpec) and isinstance(ds["bg"], CatalogSpec) and ds["bg"].context
    for k, kind in (("spec", "pks1"), ("s2d", "pki1"), ("prod", "json")):
        assert isinstance(ds[k], RecordSetSpec) and ds[k].kind == kind and ds[k].catalog == "src"
    for k in ("fig", "pv", "sed", "pngs"):
        assert isinstance(ds[k], ImageSetSpec) and ds[k].catalog == "src"
    assert isinstance(ds["iso"], IsochroneSpec) and isinstance(ds["hips"], HipsSpec)
    assert isinstance(ds["ovl"], OverlaySpec) and isinstance(ds["readme"], ReadmeSpec)
    assert len(ds["src"].table) == LIMIT
    assert ds["src"].manifold is not None and ds["bg"].density
    for p in r["pages"]:
        assert isinstance(p, PageSpec)
        for k in p.datasets:
            assert k in ds, (p.id, k)
    assert [p.id for p in r["pages"]] == PAGES
    assert [pc.id for pc in SITE.portal().pages] == PAGES


def test_tier_c_unavailable(tmp_path):
    with pytest.raises(TierUnavailable):
        mod.NIRSpec6927Adapter().datasets(_ctx("C", tmp_path))


# -- (c) ------------------------------------------------------------------------------------
@pytest.mark.parametrize("tier", ["A", "B"])
def test_ids_and_aliases_unique(runs, tier):
    ds = runs[tier]["ds"]
    ids = [str(x) for x in ds["src"].table["name"]]
    assert len(set(ids)) == len(ids) and all(re.fullmatch(r"6927_\d+", i) for i in ids)
    al = [a for v in ds["src"].aliases.values() for a in v]
    assert len(set(al)) == len(al) and not set(al) & set(ids)
    bg = [str(x) for x in ds["bg"].table["id"]]
    assert len(set(bg)) == len(bg) and not set(bg) & set(ids)
    for k in ("fig", "pv", "sed", "pngs"):
        items = [it.id for it in ds[k].items]
        assert len(set(items)) == len(items), k
    assert {it.id for k in ("fig", "pv", "sed") for it in ds[k].items} <= set(ids)
    assert {it.tags["src"] for it in ds["pngs"].items} <= set(ids)
    assert ds["pngs"].join == "src"
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
    # records stay out of first paint: spectra, 2D spectra and products load on focus
    for k in ("spec", "s2d", "prod"):
        assert report["budgets"]["datasets"][k]["first_paint_bytes"] == 0, k


def test_secure_portal(built):
    """Declared visibility is secure; every data / figure link points at the secure site."""
    portal = json.loads((built["portal"] / "portal.json").read_text(encoding="utf-8"))
    assert portal["visibility"] == "secure" and SITE.portal().visibility == "secure"
    assert SITE.portal().url.startswith("https://data.rc.ufl.edu/secure/")
    for k in ("fig", "pv", "sed", "pngs"):
        doc = json.loads((built["portal"] / "data" / k / "images.json").read_text(encoding="utf-8"))
        urls = [r["url"] for it in doc["items"] for r in it["roles"].values()]
        assert urls and all(u.startswith(mod.SECURE) for u in urls), k
    text = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                     for p in built["portal"].rglob("*")
                     if p.suffix in (".json", ".html") and p.name != "build_report.json")
    assert "/pub/adamginsburg/jwst/spectra" not in text


# -- (f) ------------------------------------------------------------------------------------
def test_tier_parity(runs):
    a, b = runs["A"]["ds"], runs["B"]["ds"]
    ta, tb = a["src"].table, b["src"].table
    assert list(ta["name"]) == list(tb["name"])
    for k in ("ra", "dec"):
        assert np.max(np.abs(np.asarray(ta[k], float) - np.asarray(tb[k], float))) < 1e-5
    for c in b["src"].columns:
        if c.role not in ("mag", "value") or c.key not in ta.colnames:
            continue
        x, y = np.asarray(ta[c.key].filled(np.nan) if hasattr(ta[c.key], "filled") else ta[c.key], float), \
            np.asarray(tb[c.key].filled(np.nan) if hasattr(tb[c.key], "filled") else tb[c.key], float)
        assert np.array_equal(np.isfinite(x), np.isfinite(y)), c.key
        ok = np.isfinite(x)
        assert np.allclose(x[ok], y[ok], rtol=1e-3, atol=1e-3), c.key
    for k in ("verdict", "Observation", "co_dominant"):
        assert list(ta[k]) == list(tb[k]), k
    assert a["hips"].base == b["hips"].base and a["hips"].target == b["hips"].target
    for k in ("fig", "pv", "sed", "pngs"):
        assert [(it.id, it.roles) for it in a[k].items] == [(it.id, it.roles) for it in b[k].items], k
    # spectra: legacy specdata (nJy) vs x1d + clean_flux (Jy)
    name = str(ta["name"][0])
    checked = 0
    for name in [str(x) for x in ta["name"][:: max(1, len(ta) // 5)]]:
        sa, sb = a["spec"].loader(name), b["spec"].loader(name)
        if sa is None or sb is None:
            continue
        assert [s[2] for s in sa.segments] == [s[2] for s in sb.segments], name
        fa = np.interp(sb.wave, sa.wave, np.nan_to_num(sa.channels["flux"], nan=0.0))
        ok = np.isfinite(sb.channels["flux"]) & (fa > 0)
        assert ok.sum() > 100, name
        assert np.nanmedian(sb.channels["flux"][ok] / fa[ok]) == pytest.approx(1.0, rel=0.05), name
        checked += 1
    assert checked >= 3


# -- (g) ------------------------------------------------------------------------------------
def test_caps_superset_of_legacy(built):
    pages = {p["id"]: p for p in _report(built)["pages"]}
    checked = 0
    for pc in SITE.portal().pages:
        enabled = set(pages[pc.id]["caps"]["enabled"])
        for leg in pc.legacy:
            key = f"{INV_FAMILY}/{leg}"
            if key not in INV:       # the _2221/_1182 PNG galleries are not inventoried
                continue
            need = set(INV[key])
            assert need <= enabled, (pc.id, sorted(need - enabled))
            checked += 1
    assert checked == 10


def test_port_table_caps_filled(built):
    """capabilities_to_fill and flags of port table 14.7 for the wave-1 pages."""
    pages = {p["id"]: p for p in _report(built)["pages"]}
    by_legacy = {leg: pc.id for pc in SITE.portal().pages for leg in pc.legacy}
    rows = [r for r in json.loads((REPO / "tests" / "data" / "port_table.json").read_text(encoding="utf-8"))
            if r["family"] == INV_FAMILY and r["legacy_page"] in by_legacy]
    assert len(rows) == 10
    for r in rows:
        pg = pages[by_legacy[r["legacy_page"]]]
        assert set(r["capabilities_to_fill"]) <= set(pg["caps"]["enabled"]), \
            (r["legacy_page"], sorted(set(r["capabilities_to_fill"]) - set(pg["caps"]["enabled"])))
        assert set(r["flags"]) <= set(pg["flags"]), (r["legacy_page"], sorted(set(r["flags"]) - set(pg["flags"])))


def test_landing_cards_and_secure_links(built):
    from portalkit.deploy.deploy import collect_references
    from portalkit.deploy.exposure import Reference, check_references
    from portalkit.legacy.extract import script_json
    page = script_json(built["portal"] / "index.html", "pk-page")
    cards = page["layout"]["options"]["cards"]
    assert {c["page"] for c in cards if "page" in c} == set(PAGES) - {"index"}
    sec = [c for c in cards if c.get("auth") == "secure"]
    assert len(sec) == 4 and all(c["href"].startswith(mod.SECURE) for c in sec)
    files = [str(p.relative_to(built["portal"])) for p in built["portal"].rglob("*")
             if p.is_file() and p.suffix in (".html", ".json")]
    refs = [Reference(url=u, source=s, fetched=f) for s, u, f in collect_references(built["portal"], files)]
    # figures are fetched from the secure site (same login as the portal); nothing public
    assert any(r.fetched for r in refs if r.url.startswith(mod.SECURE))
    dec = check_references("secure", refs)
    assert dec.exit_code == 0, dec


# -- records ---------------------------------------------------------------------------------
@pytest.mark.parametrize("tier", ["A", "B"])
def test_records(runs, tier):
    ds = runs[tier]["ds"]
    name = str(ds["src"].table["name"][0])
    sp = ds["spec"].loader(name)
    assert isinstance(sp, Spectrum)
    w, f, e = sp.wave, sp.channels["flux"], sp.channels["err"]
    assert np.all(np.diff(w) >= 0) and len(f) == len(w) == len(e)
    assert np.all(np.isnan(e[~np.isfinite(f)]))
    assert np.nanmax(f) <= mod.MAX_FLUX and np.nanmin(f) >= 0
    assert sp.segments[0][0] == 0 and sp.segments[-1][1] == len(w)
    assert all(s1[1] == s2[0] for s1, s2 in zip(sp.segments, sp.segments[1:]))
    assert all(ln["w"] > 0 and ln["name"] for ln in sp.lines)
    assert any(ln["kind"] == "ice" and not ln["detected"] for ln in sp.lines)
    im = ds["s2d"].loader(name)
    assert isinstance(im, Image2D)
    assert im.data.shape == (len(im.y), len(im.wave)) and np.all(np.diff(im.wave) > 0)
    pr = ds["prod"].loader(name)
    assert pr["schema"] == "products/1"
    for it in pr["items"]:
        assert it["kind"] in ("fits", "png", "mast", "link")
        assert (it["auth"] == "public") == (it["kind"] == "mast")
        if it["auth"] == "secure":
            assert it["url"].startswith(mod.SECURE)
    assert {it["group"] for it in pr["items"] if it["kind"] == "fits"} >= {"G235M"}
    assert ds["spec"].loader("no_such_source") is None and ds["s2d"].loader("6927_x") is None
    assert ds["prod"].loader("") is None


# -- edge cases: N = 0 / 1, NaN / inf, unicode, large values ----------------------------------
@pytest.mark.parametrize("limit", [0, 1])
def test_small_n(tmp_path, limit):
    ad = mod.NIRSpec6927Adapter()
    ctx = _ctx("A", tmp_path, limit=limit)
    ds = ad.datasets(ctx)
    pages = ad.pages(ctx, ds)
    assert len(ds["src"].table) == limit and len(ds["bg"].table) == 20 * limit
    assert [p.id for p in pages] == PAGES
    for k in ("fig", "pv", "pngs"):
        assert len(ds[k].items) == limit, k
    if limit == 0:
        assert ds["hips"].target is None and ds["src"].manifold is None
        assert not [p for p in ds["src"].presets if p.key.startswith("ice_")]   # empty-pair guard
    else:
        assert ds["hips"].target["fov"] == 0.03
        cog = {p.id: p for p in pages}["nirspec_6927_cogas"]
        assert cog.state["focus"]["id"] == str(ds["src"].table["name"][0])


def test_clean_flux_nan_inf_large():
    f = np.array([0.1, np.nan, np.inf, -np.inf, -0.2, 2.0, 0.1, 1e30, 0.11, 0.09])
    e = np.array([0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 5.0, 0.01, np.nan, 0.01])
    out = mod.clean_flux(f, e)
    assert np.isnan(out[[1, 2, 3, 4, 5, 6, 7]]).all()
    assert np.allclose(out[[0, 8, 9]], [0.1, 0.11, 0.09])
    # 10-sigma spike rejection on an otherwise flat grating
    g = np.full(1000, 0.01)
    g[500] = 0.9
    assert np.isnan(mod.clean_flux(g, np.zeros(1000))[500])
    assert mod.clean_flux(np.zeros(0), np.zeros(0)).size == 0
    assert np.isnan(mod.clean_flux([np.nan], [np.nan])).all()


def test_num_and_urls():
    assert mod._num(None) is None and mod._num("x") is None
    assert mod._num(np.inf) is None and mod._num(np.nan) is None
    assert mod._num(np.ma.masked) is None and mod._num("1e300") == 1e300
    u = mod._surl("/orange/adamginsburg/jwst/spectra/nirspec_6927/sed fig/é_α.png")
    assert u.startswith(mod.SECURE) and " " not in u and "%C3%A9" in u
    with pytest.raises(ValueError):
        mod._surl("/orange/adamginsburg/web/public/x.png")
    assert mod._rel(None) is None and mod._rel("") is None
    q = mod._mast("019")
    from urllib.parse import unquote
    assert q.startswith("https://mast.stsci.edu/") and '"freeText": "jw06927-o019%"' in unquote(q)


def test_grating_cuts():
    assert mod._cuts([]) == []
    (g, lo, hi), = mod._cuts(["G395M"])
    assert g == "G395M" and lo == 0.0 and hi == 99.0
    cuts = mod._cuts(["G395M", "G140M", "G235M", "PRISM"])
    assert [c[0] for c in cuts] == ["G140M", "G235M", "G395M"]
    assert cuts[0][2] == pytest.approx(0.5 * (1.648 + 1.878)) and cuts[1][2] == pytest.approx(0.5 * (2.850 + 3.136))
    assert all(a[2] == b[1] for a, b in zip(cuts, cuts[1:]))


def test_png_regex_tolerates_odd_names():
    """Legacy stem regex crashed on PNGs without the _6927_<n>_ pattern; now they just lose the tag."""
    for fn, stem, kind in (("jw06927-o019_6927_14_g140m_p06927.png", "jw06927-o019_6927_14_g140m_p06927", None),
                           ("x_p06927_2221.png", "x_p06927", "2221"), ("abc_p06927_1182.png", "abc_p06927", "1182")):
        m = mod.PNG_RE.match(fn)
        assert m and m["stem"] == stem and m["set"] == kind
    assert mod.PNG_RE.match("gallery_6927.html") is None and mod.PNG_RE.match("π_unicode.png") is None


def test_overlays_and_hips_shape(runs):
    for tier in ("A", "B"):
        ds = runs[tier]["ds"]
        lay = {l["id"]: l for l in ds["ovl"].layers}
        assert set(lay) == {"msa_shutters", "dither", "one_to_one"}
        for l in lay.values():
            assert {"id", "name", "space", "kind", "style", "default", "items"} <= set(l)
        for k in ("msa_shutters", "dither"):
            assert lay[k]["space"] == "sky" and lay[k]["items"]
            for it in lay[k]["items"]:
                pts = np.asarray(it["poly"], float)
                assert pts.shape[1] == 2 and np.all(np.abs(pts[:, 0] - 266.5) < 1) and np.all(np.abs(pts[:, 1] + 28.7) < 1)
        assert lay["one_to_one"]["space"] == "dim"
        assert all(b["url"].startswith("https://") for b in ds["hips"].base)
        assert sum(b["default"] for b in ds["hips"].base) == 1


def test_bg_density_and_presets(runs):
    bg = runs["B"]["ds"]["bg"]
    assert {d.key for d in bg.density} == {k for k, _, _ in mod.CMDS} | {k for k, _, _ in mod.CCDS}
    for d in bg.density:
        assert d.xr[0] < d.xr[1] and d.yr[0] < d.yr[1]
    src = runs["B"]["ds"]["src"]
    keys = {p.key for p in src.presets}
    assert {k for k, _, _ in mod.CMDS} <= keys and {k for k, _, _ in mod.CCDS} <= keys
    cols = {c.key for c in src.columns} | {v.key for v in src.virtual}
    for p in src.presets:
        if p.kind == "axes" and isinstance(p.data.get("x"), str):
            for e in (p.data["x"], p.data["y"]):
                assert all(t in cols for t in e.split("-")), (p.key, e)


def test_no_hard_coded_counts(runs):
    src = Path(mod.__file__).read_text(encoding="utf-8")
    assert not re.search(r"\b165\b|\b8740\b", src)
    md = runs["B"]["ds"]["readme"].markdown
    assert f"{LIMIT} sources" in md and f"first {LIMIT} sources" in md


def test_loaders_bound_to_their_call(runs, tmp_path):
    """A later datasets() call on the same adapter leaves earlier loaders untouched."""
    ds_a = runs["A"]["ds"]
    ids = list(ds_a["spec"].ids)
    before = [len(ds_a["spec"].loader(i).wave) if ds_a["spec"].loader(i) is not None else None for i in ids[:5]]
    mod.ADAPTER.datasets(_ctx("A", tmp_path, limit=2))
    after = [len(ds_a["spec"].loader(i).wave) if ds_a["spec"].loader(i) is not None else None for i in ids[:5]]
    assert before == after and all(n for n in before)
    assert ds_a["prod"].loader(ids[4]) is not None


@pytest.mark.parametrize("tier", ["A", "B"])
def test_cogas_focus_is_top_comb_snr(runs, tier):
    ds, pages = runs[tier]["ds"], {p.id: p for p in runs[tier]["pages"]}
    top = _top_snr(ds["src"].table)
    assert pages["nirspec_6927_cogas"].state["focus"]["id"] == top
    snr = [mod._num(x) for x in ds["src"].table["comb_snr"]]
    assert mod._num(ds["src"].table["comb_snr"][list(ds["src"].table["name"]).index(top)]) == \
        max(v for v in snr if v is not None)


def test_cogas_focus_without_snr(tmp_path, monkeypatch):
    """No comb_snr in the selected rows: the page builds without a default focus."""
    ad = mod.NIRSpec6927Adapter()
    orig = ad._tier_a
    monkeypatch.setattr(ad, "_tier_a", lambda ctx: {**orig(ctx), "cogas": {}})
    ctx = _ctx("A", tmp_path, limit=3)
    pages = {p.id: p for p in ad.pages(ctx, ad.datasets(ctx))}
    assert "focus" not in pages["nirspec_6927_cogas"].state


@pytest.mark.parametrize("tier", ["A", "B"])
def test_tau_band_limits(runs, tier):
    """tauL/tauG/A_K keep the legacy limit codes (saturated bands are lower limits)."""
    src = runs[tier]["ds"]["src"]
    cols = {c.key: c for c in src.columns}
    for _, k, _, _ in mod.ICE_KEYS:
        assert cols[k].limit == f"l_{k}", k
        assert cols[f"l_{k}"].role == "limit", k
    codes = {str(v) for v in src.table["l_tauL_H2O"]}
    assert "l" in codes, codes
    tau = next(p for p in runs[tier]["pages"] if p.id == "nirspec_6927_icecolumns")
    panes = {}

    def walk(n):
        if isinstance(n, dict):
            if n.get("pane") == "scatter":
                panes[n["id"]] = n["options"]
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(tau.layout)
    assert panes["tau"]["errorbars"] and panes["ice"]["errorbars"]


def test_pairs_follow_diagram(runs):
    """Connectors only on the fixed-diagram pane; every CMD/CCD preset names its synthetic pair."""
    pages = {p.id: p for p in runs["B"]["pages"]}
    ccd = pages["nirspec_6927_colorcolor"].layout["row"][1]["col"]
    for o in (p["options"] for p in ccd):
        if "pairs" in o:
            assert o["presets"] is False
            assert o["pairs"] == {"x2": "-".join("s_" + t for t in o["x"].split("-")),
                                  "y2": "-".join("s_" + t for t in o["y"].split("-"))}
    pre = {p.key: p.data for p in runs["B"]["ds"]["src"].presets}
    for k, _, _ in mod.CMDS + mod.CCDS:
        d = pre[k]
        assert d["pairs"]["x2"] == "-".join("s_" + t for t in d["x"].split("-"))
        assert d["pairs"]["y2"] == "-".join("s_" + t for t in d["y"].split("-"))


def test_links_and_grammars(runs):
    pages = {p.id: p for p in runs["B"]["pages"]}
    links = pages["nirspec_6927_manifold"].links
    assert {"map", "spectrum", "ice", "cogas", "pv", "sed", "h2"} <= set(links)
    assert links["ice"] == "nirspec_6927_icecolumns.html#id={id}"
    assert links["h2"].startswith(mod.SECURE) and links["h2"].endswith("nirspec_6927_h2.html#{id}")
    gram = {pc.id: pc.grammar for pc in SITE.portal().pages}
    for pid in ("nirspec_6927_map", "nirspec_6927_colorcolor", "nirspec_6927_icecolumns", "nirspec_6927_cogas",
                "nirspec_6927_pvgallery", "nirspec_6927_sedgallery"):
        assert gram[pid] == ["bare-name"], pid
    assert gram["nirspec_6927_specviewer"] == ["specviewer"]
    assert gram["nirspec_6927_manifold"] == ["kv-view", "bare-name"]
    from portalkit.legacy.hashmap import map_legacy
    assert map_legacy(gram["nirspec_6927_map"], {"hash": "#6927_14", "search": ""})["hash"] == {"id": "6927_14"}
    assert map_legacy(gram["nirspec_6927_manifold"], {"hash": "#star=6927_14", "search": ""})["hash"]["id"] == "6927_14"


def test_built_page_legacy_grammar(built):
    from portalkit.legacy.extract import script_json
    for pid, g in (("nirspec_6927_map", ["bare-name"]), ("nirspec_6927_specviewer", ["specviewer"])):
        page = script_json(built["portal"] / f"{pid}.html", "pk-page")
        assert page["page"]["legacy"]["grammar"] == g, pid


def test_pngs_current_stems_only(runs):
    ds = runs["B"]["ds"]
    srcs = [it.tags["src"] for it in ds["pngs"].items]
    assert len(srcs) == len(set(srcs)) and set(srcs) <= {str(n) for n in ds["src"].table["name"]}
    stems = {a for v in ds["src"].aliases.values() for a in v}
    assert all(it.id in stems for it in ds["pngs"].items)
