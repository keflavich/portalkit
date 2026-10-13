"""Contract tests for the jwst_archive adapter (ARCHITECTURE.md section 9.6, port table 14.3).

Fixture mode (``limit=50``, section 9.6) with a tmp stage root. Three portals: the secure archive colour
portal, the secure W51 archive_colors build (W51 photometry as context) and the public PNG
gallery that replaces the 11 legacy gallery pages. The x1d stacking and the W51 catalogue cuts
are memoised in the portalkit cache (``$PK_CACHE_DIR`` or ``~/.cache/portalkit``).
"""

from __future__ import annotations

import csv
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
from portalkit.spec import (CatalogSpec, HipsSpec, ImageSetSpec, PageSpec, ReadmeSpec, RecordSetSpec, Spectrum,
                            TierUnavailable)

REPO = Path(__file__).resolve().parents[2]
LIMIT = 50
FAMILY = "jwst_archive"
INV = json.loads((REPO / "tests" / "data" / "inventory_caps.json").read_text(encoding="utf-8"))
PORT = json.loads((REPO / "tests" / "data" / "port_table.json").read_text(encoding="utf-8"))
TMP_ROOTS = tuple(os.path.realpath(p) for p in {tempfile.gettempdir(), "/tmp", "/var/tmp"})
PAGES = [("archive", "index"), ("archive", "colors"), ("archive", "manifold"), ("w51", "index"), ("w51", "colors"),
         ("gallery", "index"), ("gallery", "gallery"), ("gallery", "checklist")]
#: inventory / port-table name -> (portal, page) of the unified build
LEGACY_NAMES = {
    "JWST archive colour portal": ("archive", "colors"),
    "JWST archive ice manifold": ("archive", "manifold"),
    "JWST archive portal README": ("archive", "index"),
    "W51 photometry vs JWST archive spectra": ("w51", "colors"),
    "W51 archive_colors README": ("w51", "index"),
    "Spectra PNG gallery": ("gallery", "gallery"),
    "Spectra PNG gallery with classification checklist": ("gallery", "checklist"),
    **{f"Gallery: {k}": ("gallery", "gallery") for k in (
        "PRISM", "PRISM 2221 filters", "1611 Serpens", "1611 broad", "1611 narrow", "1611 two gratings",
        "3222 IRAS16293", "3222 broad", "3222 narrow")},
}
CHECKLIST = Path("/orange/adamginsburg/jwst/spectra/mastDownload/JWST/jwst_archive_spectra_checklist.csv")

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from adapters import jwst_archive as mod  # noqa: E402

SITE = load_site(FAMILY)
pytestmark = [pytest.mark.adapters,
              pytest.mark.skipif(not mod.TABLE.is_file() or not (mod.PORTAL_DIR / "index.html").is_file(),
                                 reason="JWST archive spectra tree not mounted")]


def _ctx(tier: str, staging: Path, limit=LIMIT) -> BuildContext:
    return BuildContext(family=FAMILY, site=SITE, staging=staging, tier=tier, limit=limit, log=lambda m: None)


def _col(t, k):
    c = t[k]
    if hasattr(c, "mask"):
        return np.where(np.ma.getmaskarray(c), np.nan, np.asarray(c.data, float))
    return np.asarray(c, float)


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
    return {"res": res, "root": res.staging}


def _report(built, portal) -> dict:
    return json.loads((built["root"] / portal / "build_report.json").read_text(encoding="utf-8"))


def _pages(built) -> dict:
    return {(p, pg["id"]): pg for p in ("archive", "w51", "gallery") for pg in _report(built, p)["pages"]}


# -- (a) ------------------------------------------------------------------------------------
def test_import_has_no_side_effects(tmp_path):
    code = ("import sys; sys.path.insert(0, %r); import adapters.jwst_archive as m; "
            "assert m.ADAPTER.name == 'jwst_archive'" % str(REPO))
    subprocess.run([sys.executable, "-c", code], cwd=tmp_path, check=True, timeout=120)
    assert list(tmp_path.iterdir()) == []


# -- (b) ------------------------------------------------------------------------------------
@pytest.mark.parametrize("tier", ["A", "B"])
def test_datasets_valid_and_fast(runs, tier):
    r = runs[tier]
    assert r["dt"] < 120, f"datasets() took {r['dt']:.1f} s"
    ds = r["ds"]
    for k, spec in ds.items():
        assert getattr(spec, "key", k) == k, k
    assert isinstance(ds["src"], CatalogSpec) and not ds["src"].context
    for k in ("arch", "bg"):
        assert isinstance(ds[k], CatalogSpec) and ds[k].context and ds[k].exact_rows is False and ds[k].density
    assert isinstance(ds["spec"], RecordSetSpec) and ds["spec"].kind == "pks1" and ds["spec"].catalog == "src"
    assert isinstance(ds["pngs"], ImageSetSpec) and ds["pngs"].catalog == "src" and ds["pngs"].join is None
    assert isinstance(ds["hips"], HipsSpec)
    for k in ("readme", "readme_w51", "readme_gal"):
        assert isinstance(ds[k], ReadmeSpec)
    assert len(ds["src"].table) == LIMIT and ds["src"].manifold is not None
    for p in r["pages"]:
        assert isinstance(p, PageSpec)
        for k in p.datasets:
            assert k in ds, (p.id, k)
    assert [(p.portal, p.id) for p in r["pages"]] == PAGES
    assert [(pc.id, pg.id) for pc in SITE.portals for pg in pc.pages] == PAGES


def test_tier_c_unavailable(tmp_path):
    with pytest.raises(TierUnavailable):
        mod.JWSTArchiveAdapter().datasets(_ctx("C", tmp_path))


# -- (c) ids from the x1d name; aliases ------------------------------------------------------
@pytest.mark.parametrize("tier", ["A", "B"])
def test_ids_and_aliases_unique(runs, tier):
    ds = runs[tier]["ds"]
    t = ds["src"].table
    ids = [str(x) for x in t["id"]]
    assert len(set(ids)) == len(ids)
    for i, fn in zip(ids, t["Filename"]):
        base = os.path.basename(str(fn))
        assert i == mod.xid(base) and base.endswith("_x1d.fits") and not str(fn).startswith("/")
        assert re.fullmatch(r"jw\d{5}-[a-z0-9]+_[^_]+_[a-z0-9-]+", i), i
    al = [a for v in ds["src"].aliases.values() for a in v]
    assert len(set(al)) == len(al) and not set(al) & set(ids)
    assert ds["spec"].ids == ids
    items = [it.id for it in ds["pngs"].items]
    assert len(set(items)) == len(items)
    assert set(ids) & set(items)
    arch = [str(x) for x in ds["arch"].table["id"]] + [str(x) for x in ds["bg"].table["id"]]
    assert len(set(arch)) == len(arch) and not set(arch) & set(ids)


def test_xid_from_x1d_name():
    assert mod.xid("jw01854-o003_t016_nirspec_g235h-f170lp_x1d.fits") == "jw01854-o003_t016_g235h-f170lp"
    assert mod.xid("/a/b/jw02770-o001_s000000623_nirspec_clear-prism_x1d.fits") == "jw02770-o001_s000000623_clear-prism"
    with pytest.raises(ValueError):
        mod.xid("x1d.fits")


def test_ids_unique_over_full_table():
    """The id rule holds over the whole fluxes table (not only the fixture rows)."""
    from astropy.table import Table
    t = Table.read(mod.TABLE)
    ids = [mod.xid(str(f)) for f in t["Filename"]]
    assert len(set(ids)) == len(ids) == len(t)


def test_manifold_star_aliases_point_at_the_right_spectrum(runs):
    """The legacy manifold addressed spectra by a stale row index (idx); the aliases map the
    star names by target, programme, grating and position instead."""
    from portalkit.legacy import extract
    pay = extract.payloads(mod.PORTAL_DIR / "archive_manifold.html", names=("DATA",))
    stars = {s["n"]: s for s in pay[0]["value"]["stars"]}
    t = runs["A"]["ds"]["src"].table
    row = {str(i): k for k, i in enumerate(t["id"])}
    n = 0
    for sid, names in runs["A"]["ds"]["src"].aliases.items():
        for a in names[1:]:
            s, k = stars[a], row[sid]
            assert s["t"] == t["Target"][k] and s["grating"] == t["Grating"][k] and str(s["prog"]) == t["Program"][k]
            assert abs(s["ra"] - t["ra"][k]) < 2e-4 and abs(s["dec"] - t["dec"][k]) < 2e-4
            n += 1
    assert n >= LIMIT // 4


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
    assert str(CHECKLIST) not in paths + [r["path"] for r in recs]


# -- (e) ------------------------------------------------------------------------------------
def test_fixture_build_passes_check(built):
    assert built["res"].ok, built["res"].report.get("errors")
    for portal in ("archive", "w51", "gallery"):
        rep = check_build(built["root"] / portal, gate="G2")
        assert rep.ok, (portal, rep.errors)
        report = _report(built, portal)
        assert report["tier"] == "B" and not report["errors"]
        for pg in report["pages"]:
            assert pg["first_paint_bytes"] <= 2_000_000, (portal, pg["id"])
        for k in ("spec",):
            if k in report["budgets"]["datasets"]:
                assert report["budgets"]["datasets"][k]["first_paint_bytes"] == 0, (portal, k)
    for portal, k in (("archive", "arch"), ("w51", "bg")):
        man = json.loads((built["root"] / portal / "data" / k / "manifest.json").read_text(encoding="utf-8"))
        assert "rows" not in man, (portal, k)
    assert [p for _, p in PAGES if _ == "gallery"] == [pg["id"] for pg in _report(built, "gallery")["pages"]]


def test_visibility_and_urls(built):
    """Colour portals are secure; the gallery is public (the PNGs already are) and links into
    the secure colour portal."""
    for portal, vis, url in (("archive", "secure", "https://data.rc.ufl.edu/secure/adamginsburg/jwst/spectra/portal_unified/"),
                             ("w51", "secure",
                              "https://data.rc.ufl.edu/secure/adamginsburg/w51/portal/archive_colors/unified/"),
                             ("gallery", "public", mod.PUBLIC_PNG + "unified/")):
        pj = json.loads((built["root"] / portal / "portal.json").read_text(encoding="utf-8"))
        assert pj["visibility"] == vis and SITE.portal(portal).visibility == vis
        assert SITE.portal(portal).url == url
    doc = json.loads((built["root"] / "gallery" / "data" / "pngs" / "images.json").read_text(encoding="utf-8"))
    urls = [r["url"] for it in doc["items"] for r in it["roles"].values()]
    assert urls and all(u.startswith(mod.PUBLIC_PNG) for u in urls)
    from portalkit.legacy.extract import script_json
    page = script_json(built["root"] / "gallery" / "gallery.html", "pk-page")
    links = page["page"]["links"] if "links" in page.get("page", {}) else page["links"]
    assert links["colors"].startswith(SITE.portal("archive").url) and links["colors"].endswith("#id={id}")


def test_secure_references(built):
    from portalkit.deploy.deploy import collect_references
    from portalkit.deploy.exposure import Reference, check_references
    for portal in ("archive", "w51"):
        root = built["root"] / portal
        files = [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and p.suffix in (".html", ".json")]
        refs = [Reference(url=u, source=s, fetched=f) for s, u, f in collect_references(root, files)]
        assert check_references("secure", refs).exit_code == 0, portal


# -- (f) ------------------------------------------------------------------------------------
def test_tier_parity(runs):
    a, b = runs["A"]["ds"], runs["B"]["ds"]
    ta, tb = a["src"].table, b["src"].table
    assert list(ta["id"]) == list(tb["id"])
    for k in ("ra", "dec"):
        assert np.nanmax(np.abs(_col(ta, k) - _col(tb, k))) < 1e-5
    for c in b["src"].columns:
        if c.role not in ("mag", "value"):
            continue
        x, y = _col(ta, c.key), _col(tb, c.key)
        assert np.array_equal(np.isfinite(x), np.isfinite(y)), c.key
        ok = np.isfinite(x)
        assert np.allclose(x[ok], y[ok], rtol=1e-4, atol=2e-4), c.key
    for k in ["Target", "Program", "Grating", "Grating2", "checklist", "emission", "Filename"] + \
            [f"cov_{f}" for f in mod.FILTERS]:
        assert [str(v) for v in ta[k]] == [str(v) for v in tb[k]], k
    assert a["hips"].base == b["hips"].base and a["hips"].target == b["hips"].target
    key = lambda ds: [(it.id, it.roles, it.tags) for it in ds["pngs"].items]  # noqa: E731
    assert key(a) == key(b)
    checked = 0
    for name in [str(x) for x in ta["id"][:: max(1, len(ta) // 6)]]:
        sa, sb = a["spec"].loader(name), b["spec"].loader(name)
        if sa is None or sb is None:
            continue
        assert sa.segments == sb.segments, name
        fa = np.interp(sb.wave, sa.wave, np.nan_to_num(sa.channels["flux"], nan=0.0))
        ok = np.isfinite(sb.channels["flux"]) & (fa > 0)
        assert ok.sum() > 50, name
        assert np.nanmedian(sb.channels["flux"][ok] / fa[ok]) == pytest.approx(1.0, rel=0.02), name
        checked += 1
    assert checked >= 3


# -- (g) capabilities -------------------------------------------------------------------------
def test_caps_superset_of_legacy(built):
    pages = _pages(built)
    for name, key in LEGACY_NAMES.items():
        need = set(INV[f"{FAMILY}/{name}"])
        enabled = set(pages[key]["caps"]["enabled"])
        assert need <= enabled, (name, sorted(need - enabled))
    need = set(INV["w51/archive_colors/index.html"])
    assert need <= set(pages[("w51", "colors")]["caps"]["enabled"])
    assert len(LEGACY_NAMES) == len([k for k in INV if k.startswith(FAMILY + "/")])


def test_port_table_caps_and_flags(built):
    pages = _pages(built)
    rows = [r for r in PORT if r["family"] == FAMILY or r["legacy_page"] == "archive_colors/index.html"]
    assert len(rows) == 17
    for r in rows:
        pg = pages[LEGACY_NAMES.get(r["legacy_page"], ("w51", "colors"))]
        assert set(r["capabilities_to_fill"]) <= set(pg["caps"]["enabled"]), \
            (r["legacy_page"], sorted(set(r["capabilities_to_fill"]) - set(pg["caps"]["enabled"])))
        assert set(r["flags"]) <= set(pg["flags"]), (r["legacy_page"], sorted(set(r["flags"]) - set(pg["flags"])))


def test_landing_cards(built):
    from portalkit.legacy.extract import script_json
    want = {"archive": {"colors", "manifold"}, "w51": {"colors"}, "gallery": {"gallery", "checklist"}}
    for portal, pages in want.items():
        page = script_json(built["root"] / portal / "index.html", "pk-page")
        cards = page["layout"]["options"]["cards"]
        assert {c["page"] for c in cards if "page" in c} == pages
        for c in cards:
            if "href" in c:
                assert c["auth"] in ("secure", "public") and c["href"].startswith("https://data.rc.ufl.edu/")
                assert ("/secure/" in c["href"]) == (c["auth"] == "secure")


# -- validity bits ---------------------------------------------------------------------------
@pytest.mark.parametrize("tier", ["A", "B"])
def test_band_coverage_validity_bits(runs, tier):
    src = runs[tier]["ds"]["src"]
    cols = {c.key: c for c in src.columns}
    n_off = 0
    for f in mod.FILTERS:
        c = cols[f]
        assert c.role == "mag" and c.filter == f"JWST/NIRCam.{f}" and c.validity == f"cov_{f}"
        assert cols[f"cov_{f}"].role == "flag"
        n_off += int((~np.asarray(src.table[f"cov_{f}"], bool)).sum())
    assert n_off > 0      # some bands fall in gaps or beyond the grating range
    assert {"F162M-F210M", "F360M-F480M"} <= {v.key for v in src.virtual}
    pre = {p.key: p for p in src.presets}
    assert "ccd_f162m_f210m_f360m_f480m" in pre and "cov" in pre and "sky" in pre
    keys = {c.key for c in src.columns} | {v.key for v in src.virtual}
    for p in src.presets:
        for e in (p.data["x"], p.data["y"]):
            assert all(t in keys for t in e.split("-")), (p.key, e)


def test_validity_matches_tier_a_coverage(runs):
    """cov_<F> is cg >= 1000 of the legacy payload."""
    P = mod._cached_payload(runs["A"]["ctx"], mod.PORTAL_DIR / "index.html", "PORTAL")
    t = runs["A"]["ds"]["src"].table
    by = {s["f"]: s for s in P["sources"]}
    for k in range(len(t)):
        s = by[os.path.basename(str(t["Filename"][k]))]
        assert [bool(t[f"cov_{f}"][k]) for f in mod.FILTERS] == [c >= 1000 for c in s["cg"]]


def test_context_datasets(runs):
    ds = runs["B"]["ds"]
    bg, arch = ds["bg"], ds["arch"]
    assert len(bg.table) == 20 * LIMIT and len(arch.table) == LIMIT
    bcols = {c.key for c in bg.columns}
    assert "F150W" not in bcols and {"F162M", "F210M", "F360M", "F480M"} <= bcols
    for d in bg.density + arch.density:
        assert d.xr[0] < d.xr[1] and d.yr[0] < d.yr[1]
        cols = {c.key for c in (bg if d in bg.density else arch).columns}
        assert all(t in cols for t in (d.x + "-" + d.y).split("-")), d.key
    assert "ccd_f162m_f210m_f360m_f480m" in {d.key for d in bg.density}
    a = mod.w51_background(mod.W51_CAT)
    n = len(next(iter(a.values())))
    assert 0 < n <= 30000 and np.all(np.sum([np.isfinite(v) for v in a.values()], axis=0) >= 4)


def test_gallery_presets_match_registry_stubs():
    """Each legacy gallery stub preset is the b64url facet hash of its GALLERY_PRESETS entry."""
    from portalkit.legacy.hashmap import b64url_encode
    raw = SITE.portal("gallery").raw["page"]
    stubs = {it["file"]: it["preset"] for pg in raw for it in pg.get("legacy", []) if isinstance(it, dict)}
    by_label = {p[0]: p for p in mod.GALLERY_PRESETS}
    files = {"gallery_prism.html": "prism", "gallery_prism_2221filters.html": "prism_2221",
             "gallery_1611.html": "p1611", "gallery_1611_broad.html": "p1611_broad",
             "gallery_1611_narrow.html": "p1611_narrow", "gallery_1611_twogratings.html": "p1611_two",
             "gallery_3222.html": "p3222", "gallery_3222_broad.html": "p3222_broad",
             "gallery_3222_narrow.html": "p3222_narrow"}
    assert set(stubs) == set(files)
    for fn, key in files.items():
        _, _, fac, role = by_label[key]
        want = "fac=" + b64url_encode(json.dumps(fac, separators=(",", ":"), sort_keys=True).encode())
        if role:
            want += f"&role={role}"
        assert stubs[fn] == want, fn
        assert (mod.PNG_DIR / fn).is_file(), fn


def test_full_gallery_facets_cover_presets(tmp_path):
    """Full build (no limit): each legacy preset selects images."""
    ds = mod.ADAPTER.datasets(_ctx("A", tmp_path, limit=None))
    items = ds["pngs"].items
    for key, _, fac, role in mod.GALLERY_PRESETS:
        sel = [it for it in items if all(it.tags.get(f) in v for f, v in fac.items())]
        if role:
            sel = [it for it in sel if role in it.roles]
        assert sel, key
    p1611 = [it for it in items if it.tags.get("program") == "1611"]
    assert {"c2221", "c1182", "sed"} <= {r for it in p1611 for r in it.roles}


def test_png_regex():
    m = mod.PNG_RE.match("2MASS J05460363-0014493_2MASS J05460363-0014493_G395HG235H_2221_"
                         "t016_o003_v001_vg02_p01854.png")
    assert m and m["gr"] == "G395HG235H" and m["set"] == "2221" and m["slit"] == "t016" and m["p"] == "01854"
    m = mod.PNG_RE.match("X_Y_PRISM_s000000623_o001_v001_vg01_p02770.png")
    assert m and m["set"] is None and m["gr"] == "PRISM"
    assert mod.PNG_RE.match("gallery.html") is None and mod.PNG_RE.match("π_unicode.png") is None


# -- records -----------------------------------------------------------------------------------
@pytest.mark.parametrize("tier", ["A", "B"])
def test_records(runs, tier):
    ds = runs[tier]["ds"]
    n = 0
    for name in ds["spec"].ids[:6]:
        sp = ds["spec"].loader(name)
        if sp is None:
            continue
        w, f = sp.wave, sp.channels["flux"]
        assert isinstance(sp, Spectrum) and len(w) == len(f) <= mod.NSPEC
        assert np.all(np.diff(w) >= 0) and np.all(np.isfinite(w))
        assert np.nanmax(f) <= mod.MAX_FLUX and np.nanmin(f) >= 0
        assert sp.segments == [(0, len(w), sp.meta["gratings"])] and sp.units["flux"] == "Jy"
        assert sp.meta["id"] == name and mod.xid(sp.meta["x1d"]) == name
        n += 1
    assert n >= 4
    assert ds["spec"].loader("no_such_spectrum") is None


# -- edge cases --------------------------------------------------------------------------------
@pytest.mark.parametrize("limit", [0, 1])
def test_small_n(tmp_path, limit):
    ad = mod.JWSTArchiveAdapter()
    ctx = _ctx("A", tmp_path, limit=limit)
    ds = ad.datasets(ctx)
    pages = ad.pages(ctx, ds)
    assert len(ds["src"].table) == limit and len(ds["bg"].table) == 20 * limit
    assert [(p.portal, p.id) for p in pages] == PAGES
    if limit == 0:
        assert ds["hips"].target is None and ds["spec"].ids == []
        assert ds["src"].manifold.frames["pca"][0] == 1.0      # identity pair without data
    else:
        assert ds["hips"].target["fov"] == 0.1


def test_clean_flux_nan_inf_large():
    f = np.array([0.1, np.nan, np.inf, -np.inf, -0.2, 2.0, 0.1, 1e30, 0.11, 0.09])
    out = mod.clean_flux(f)
    assert np.isnan(out[[1, 2, 3, 4, 5, 7]]).all()
    assert np.allclose(out[[0, 6, 8, 9]], [0.1, 0.1, 0.11, 0.09])
    g = np.full(1000, 0.01)
    g[500] = 0.9
    assert np.isnan(mod.clean_flux(g)[500])
    assert mod.clean_flux(np.zeros(0)).size == 0
    assert np.isnan(mod.clean_flux([np.nan])).all()


def test_band_coverage_and_binning():
    wf = np.linspace(2.0, 2.2, 50)
    grids = {"F": (wf, np.ones(50), 50.0), "OUT": (wf + 3, np.ones(50), 50.0)}
    wl = np.linspace(1.9, 2.3, 400)
    flux = np.ones(400)
    gap, fin = mod.band_coverage(wl, flux, grids)
    assert gap["F"] == pytest.approx(1.0) and fin["F"] == pytest.approx(1.0) and gap["OUT"] == 0.0
    wl2 = wl[(wl < 2.05) | (wl > 2.15)]                       # detector gap inside the band
    gap2, _ = mod.band_coverage(wl2, np.ones(wl2.size), grids)
    assert 0.3 < gap2["F"] < 0.8
    flux3 = flux.copy()
    flux3[(wl > 2.05) & (wl < 2.15)] = np.nan                 # NaN-ed pixels: gap counts them, fin does not
    gap3, fin3 = mod.band_coverage(wl, flux3, grids)
    assert gap3["F"] == pytest.approx(1.0) and fin3["F"] < 0.8
    w, f = mod.downsample(np.linspace(1, 5, 6000), np.ones(6000))
    assert w.size == mod.NSPEC and np.all(np.diff(w) > 0)
    x = np.r_[np.linspace(1, 2, 100), np.linspace(3, 4, 100)]
    g = mod.insert_gaps(x, np.ones(200))
    assert np.isnan(g[100]) and np.isfinite(g).sum() == 199


def test_num_and_urls():
    assert mod._num(None) is None and mod._num("x") is None
    assert mod._num(np.inf) is None and mod._num(np.ma.masked) is None and mod._num("1e300") == 1e300
    assert mod._s(np.ma.masked) == "" and mod._s("--") == "" and mod._s(" a ") == "a"
    u = mod._purl("2MASS J05460363 é.png")
    assert u.startswith(mod.PUBLIC_PNG) and " " not in u and "%C3%A9" in u
    assert mod._surl("/orange/adamginsburg/jwst/spectra/x y.fits").startswith(mod.SECURE)
    with pytest.raises(ValueError):
        mod._surl("/orange/adamginsburg/web/public/x.png")


def test_no_absolute_paths_in_readmes(runs):
    for k in ("readme", "readme_w51", "readme_gal"):
        md = runs["B"]["ds"][k].markdown
        assert not re.search(r"/(orange|blue|home)/", md), k
        assert f"first {LIMIT} rows" in md


def test_links_and_grammars(runs):
    pages = {(p.portal, p.id): p for p in runs["B"]["pages"]}
    assert pages[("archive", "colors")].links["manifold"] == "manifold.html#id={id}"
    assert pages[("archive", "colors")].links["w51"] == SITE.portal("w51").url + "colors.html#id={id}"
    assert pages[("gallery", "gallery")].links["colors"] == SITE.portal("archive").url + "colors.html#id={id}"
    ann = pages[("gallery", "checklist")].layout["row"][1]["options"]["annotate"]
    assert ann == {"codes": ["y", "n", "g", "w", "l", "e"], "key": "ISOKN"}
    gram = {(pc.id, pg.id): pg.grammar for pc in SITE.portals for pg in pc.pages}
    assert gram[("archive", "manifold")] == ["kv-view", "bare-name"]
    from portalkit.legacy.hashmap import map_legacy
    alias = next(a[1] for a in runs["A"]["ds"]["src"].aliases.values() if len(a) > 1)
    h = map_legacy(gram[("archive", "manifold")], {"hash": f"#view=pca&star={alias}", "search": ""})["hash"]
    assert h["id"] == alias


def test_checklist_join_keys(runs):
    """Catalogue key fields agree with the checklist row of the same x1d (read-only)."""
    from portalkit.cli import CHECKLIST_KEYS, _norm_key
    if not CHECKLIST.is_file():
        pytest.skip("checklist not mounted")
    with open(CHECKLIST, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    by = {os.path.basename(r["Filename"].strip()): r for r in rows}
    t = runs["B"]["ds"]["src"].table
    hit = 0
    for k in range(len(t)):
        r = by.get(os.path.basename(str(t["Filename"][k])))
        if r is None:
            continue
        for key in CHECKLIST_KEYS:
            if key == "Filename":
                assert os.path.basename(r[key].strip()) == os.path.basename(str(t[key][k]))
                continue
            v = t[key][k]
            mine = "" if np.ma.is_masked(v) or v is None or str(v) in ("--", "None") else str(v)
            assert _norm_key(mine) == _norm_key(r[key] if r[key] not in ("--",) else ""), (key, mine, r[key])
        hit += 1
    assert hit >= LIMIT // 2


# -- deploy targets (section 11.2) --------------------------------------------------------------
def test_targets_pass_exposure_policy():
    """Every portal's deploy target passes check_target for its visibility, and its url is the
    target's served URL. portal/unified would fail: pngs/portal -> web/secure/jwst/spectra/portal
    makes it public through web/public/jwst/spectra."""
    from portalkit.deploy import exposure as X
    from portalkit.deploy.deploy import resolve_target
    if not any(d.glob("exposure-*.json") for d in X._cache_dirs() if d.is_dir()):
        pytest.skip("no cached exposure map (a cold scan takes ~10 min on this host)")
    emap = X.scan(cache_s=10 ** 9)      # the cached map; revalidating it walks every root
    for p in SITE.portals:
        e = X.classify(resolve_target(p, FAMILY), emap)
        d = X.check_target(p.visibility, e)
        assert d.ok, (p.id, d.message)
        assert p.url in (e.public_urls if p.visibility == "public" else e.secure_urls), (p.id, p.url, e)
        if p.visibility == "secure":
            assert e.kind == "secure", (p.id, e)
    old = X.classify("/orange/adamginsburg/jwst/spectra/portal/unified", emap)
    if old.kind == "both":   # still true while the pngs/portal symlink exists
        assert X.check_target("secure", old).exit_code == 3


# -- legacy coverage tables: reused only when row-aligned -------------------------------------
def test_legacy_coverage_reused_when_aligned(runs):
    ctx = runs["B"]["ctx"]
    from astropy.table import Table
    coords, cov = mod.ADAPTER._legacy_coverage(ctx, Table.read(mod.TABLE))
    if coords is None:
        pytest.skip("legacy portal_coords / portal_coverage are not aligned with the fluxes table")
    assert "were recomputed" not in runs["B"]["ds"]["readme"].markdown


def test_legacy_coverage_rejected_when_reordered(tmp_path, monkeypatch):
    """A fluxes table with the same row count in another order (same mtime) must not take
    the legacy coordinates / coverage by position."""
    from astropy.table import Table
    t = Table.read(mod.TABLE)
    rev = t[::-1]
    cp = tmp_path / mod.TABLE.name
    rev.write(cp, format="ascii.ecsv")
    st = mod.TABLE.stat()
    os.utime(cp, ns=(st.st_atime_ns, st.st_mtime_ns))
    monkeypatch.setattr(mod, "TABLE", cp)
    logs = []
    ctx = BuildContext(family=FAMILY, site=SITE, staging=tmp_path / "s", tier="B", limit=3,
                       log=logs.append, allow_tmp_inputs=True)
    assert mod.ADAPTER._legacy_coverage(ctx, rev) == (None, None)
    assert any("row order differs" in m for m in logs), logs
    rows = mod.ADAPTER._tier_b(ctx)
    from astropy.io import fits
    for r in rows:
        assert r["cov_x1d"]
        with fits.open(mod.ARCH / f"{r['x1d'][:-len('_x1d.fits')]}" / r["x1d"]) as fh:
            h = fh[0].header
        ra = h.get("SLIT_RA", h.get("TARG_RA"))
        if ra is not None and r["ra"] is not None:
            assert abs(r["ra"] - float(ra)) < 1e-5, r["x1d"]
        assert all(c == 1000 or c < 1000 - mod.COV_TOL for c in r["cg"])


def test_legacy_coverage_rejected_when_older_than_table(tmp_path, monkeypatch):
    from astropy.table import Table
    cp = tmp_path / mod.TABLE.name
    cp.write_bytes(mod.TABLE.read_bytes())          # a fresh copy: newer than the ECSVs
    monkeypatch.setattr(mod, "TABLE", cp)
    logs = []
    ctx = BuildContext(family=FAMILY, site=SITE, staging=tmp_path / "s", tier="B", limit=1,
                       log=logs.append, allow_tmp_inputs=True)
    assert mod.ADAPTER._legacy_coverage(ctx, Table.read(cp)) == (None, None)
    assert any("older than the fluxes table" in m for m in logs), logs


# -- gallery joins and presets ------------------------------------------------------------------
def test_png_row_prefers_the_observation_product():
    """A combined (-c) and a per-observation (-o) x1d share the PNG key; the PNG's o<obs>
    picks the -o product."""
    c = {"x1d": "jw01309-c1002_t001_nirspec_g395h-f290lp_x1d.fits", "g": "G395H", "g2": "G395H"}
    o = {"x1d": "jw01309-o013_t001_nirspec_g395h-f290lp_x1d.fits", "g": "G395H", "g2": "G395H"}
    assert mod._png_row([c, o], "G395H", "013") is o
    assert mod._png_row([c], "G395H", "013") is c
    assert mod._png_row([c, dict(c)], "G395H", "013") is None          # still ambiguous
    m = {"x1d": "jw01611-o001_s1_nirspec_g235m-f170lp_x1d.fits", "g": "G235M", "g2": "G395H"}
    assert mod._png_row([m, {**m, "g2": "G235M"}], "G235MG395H", "001") is m


def test_gallery_presets_carry_every_registry_entry_and_role(runs):
    """schema.py's pk-images preset validator allows "role" (WPE11 item 2), so the dropdown
    carries every GALLERY_PRESETS entry, role variants included, each with the registry's own
    facets/role (not folded into facets)."""
    pre = {p.key: p for p in runs["B"]["ds"]["pngs"].presets}
    assert set(pre) == {k for k, *_ in mod.GALLERY_PRESETS}
    for key, lab, fac, role in mod.GALLERY_PRESETS:
        p = pre[key]
        assert p.label == lab and p.data["facets"] == fac
        assert set(p.data) == ({"facets", "role"} if role else {"facets"})
        if role:
            assert p.data["role"] == role


# -- checklist export -> archive-checklist-diff ---------------------------------------------------
def test_checklist_diff_accepts_the_adapter_export(runs):
    """An export built from the catalogue rows as the checklist page writes them (relative
    Filename, no remapping) changes the matching checklist rows."""
    from portalkit.cli import CHECKLIST_KEYS, checklist_diff
    if not CHECKLIST.is_file():
        pytest.skip("checklist not mounted")
    text = CHECKLIST.read_text(encoding="utf-8")
    t = runs["B"]["ds"]["src"].table
    have = {os.path.basename(r["Filename"].strip()): r for r in csv.DictReader(text.splitlines())}
    rows, flips = [], 0
    for k in range(len(t)):
        cur = have.get(os.path.basename(str(t["Filename"][k])))
        if cur is None:
            continue
        code = "n" if cur["ISOKN"].strip() == "y" else "y"
        cell = lambda v: "" if v is None or np.ma.is_masked(v) or str(v) in ("--", "None") else str(v)  # noqa: E731
        rows.append([str(t["id"][k]), *(cell(t[c][k]) for c in CHECKLIST_KEYS), code])
        flips += 1
    assert flips >= 1
    out = [",".join(["id", *CHECKLIST_KEYS, "ISOKN"])] + [",".join(f'"{x}"' for x in r) for r in rows]
    diff, notes = checklist_diff("\n".join(out) + "\n", text)
    # Filename matches by path suffix (basename + parent dir; checklist_diff, WPE6)
    assert not any("no checklist row matches" in n for n in notes), notes
    assert diff and int(notes[-1].split()[0]) >= flips, notes


# -- foreign record sets (contract (A)) ------------------------------------------------------
# w51 deliberately does NOT reference the archive portal's shards as a foreign record set: the
# two portals' secure areas (web/secure/jwst, web/secure/w51) are different .htpasswd files
# with only one account in common, so a w51-only viewer would get a 401 on every spectrum
# fetch (verify --http cannot catch this; section 4.3 reports a 401 from a secure base
# without credentials only as a note). w51 keeps its own copy of "spec" instead (see the
# module docstring). foreign_base() is still exercised directly below as the generic helper
# (used live by other families whose portals do share one password file, e.g. wd2).
def test_foreign_base():
    w51, arch = SITE.portal("w51").url, SITE.portal("archive").url
    base = mod.foreign_base(w51, arch)
    assert base == "../../../../jwst/spectra/portal_unified/data/"
    from urllib.parse import urljoin
    assert urljoin(w51, base) == arch + "data/"
    with pytest.raises(ValueError):
        mod.foreign_base(w51, "https://example.org/x/")


def test_w51_keeps_its_own_spectrum_copy(runs, built):
    """w51 stages its own spectrum shards rather than a cross-realm foreign reference onto the
    archive portal (different .htpasswd files; see the module docstring and test_foreign_base's
    comment above)."""
    ds = runs["B"]["ds"]
    assert "spec_arch" not in ds
    pages = {(p.portal, p.id): p for p in runs["B"]["pages"]}
    for key in (("w51", "colors"), ("gallery", "gallery"), ("gallery", "checklist"),
                ("archive", "colors"), ("archive", "manifold")):
        assert "spec" in pages[key].datasets and "spec_arch" not in pages[key].datasets, key
    root = built["root"]
    assert (root / "w51" / "data" / "spec" / "index.json").is_file()
    assert (root / "archive" / "data" / "spec" / "index.json").is_file()
    assert (root / "gallery" / "data" / "spec" / "index.json").is_file()
    rep = _report(built, "w51")["datasets"]["spec"]
    assert not rep.get("foreign") and rep["files"] > 0


def test_public_gallery_may_not_reference_secure_shards(runs):
    """The generic visibility rule (contract (A)), exercised with a synthetic foreign dataset
    since no live dataset in this family points at the archive portal's shards (see above)."""
    from portalkit.build import foreign_records_exposure
    from portalkit.deploy import ExposureRefusal
    from portalkit.spec import ForeignRecords
    ids = runs["B"]["ds"]["spec"].ids
    fa_gal = RecordSetSpec("spec_arch", "pks1", "src", list(ids), None, foreign=ForeignRecords(
        mod.foreign_base(SITE.portal("gallery").url, SITE.portal("archive").url), "spec"))
    with pytest.raises(ExposureRefusal):
        foreign_records_exposure(SITE.portal("gallery"), "spec_arch", fa_gal)
    fa_w51 = RecordSetSpec("spec_arch", "pks1", "src", list(ids), None, foreign=ForeignRecords(
        mod.foreign_base(SITE.portal("w51").url, SITE.portal("archive").url), "spec"))
    assert foreign_records_exposure(SITE.portal("w51"), "spec_arch", fa_w51).startswith(
        SITE.portal("archive").url)
