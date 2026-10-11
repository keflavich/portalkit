"""Contract tests for the wd2 adapter (ARCHITECTURE.md section 9.6, port table 14.11).

Fixture mode (``limit=50``) with a tmp stage root. The Tier B payloads (WD2_Portal_Manifold
``build`` over the NIRCam + MIRI catalogues, the Sabbi+2020 crossmatch, the map_ccd dataset)
are memoised in the portalkit cache (``$PK_CACHE_DIR`` or ``~/.cache/portalkit``), so only the
first run after a catalogue change rebuilds them.
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
from portalkit.spec import (CatalogSpec, HipsSpec, ImageSetSpec, IsochroneSpec, PageSpec,
                            ReadmeSpec, TierUnavailable)

REPO = Path(__file__).resolve().parents[2]
LIMIT = 50
FAMILY = "wd2"
PAGES = ["index", "map_ccd", "wd2_colorcolor", "wd2_manifold", "sabbi", "wd2_sedgallery",
         "wd2_wavelength_explorer"]
INV = json.loads((REPO / "tests" / "data" / "inventory_caps.json").read_text(encoding="utf-8"))
TMP_ROOTS = tuple(os.path.realpath(p) for p in {tempfile.gettempdir(), "/tmp", "/var/tmp"})

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from adapters import wd2 as mod  # noqa: E402

SITE = load_site(FAMILY)
pytestmark = [pytest.mark.adapters,
              pytest.mark.skipif(not (mod.LEGACY_DIR / "wd2_manifold.html").is_file(),
                                 reason="legacy wd2 portal tree not mounted")]


def _ctx(tier: str, staging: Path) -> BuildContext:
    return BuildContext(family=FAMILY, site=SITE, staging=staging, tier=tier, limit=LIMIT,
                        log=lambda m: None)


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """datasets() + pages() for both tiers (timed: contract 9.6(b))."""
    out = {}
    for tier in ("A", "B"):
        ctx = _ctx(tier, tmp_path_factory.mktemp(f"ctx{tier}"))
        adapter = mod.Wd2Adapter()
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
    return {"res": res, "portal": res.staging / FAMILY}


def _report(built) -> dict:
    return json.loads((built["portal"] / "build_report.json").read_text(encoding="utf-8"))


def _sel_ids(sel: str, t) -> set:
    """Ids of a saved selection: ``ids:`` (<= IDS_IN_HASH_MAX ids) or an ``r:`` replace recipe
    of one ids or cats-filter op (section 6.3)."""
    from portalkit.constants import IDS_IN_HASH_MAX
    from portalkit.legacy.hashmap import decode_recipe
    if sel.startswith("ids:"):
        ids = sel[4:].split(",")
        assert 0 < len(ids) <= IDS_IN_HASH_MAX
        return set(ids)
    assert sel.startswith("r:")
    (op,) = decode_recipe(sel)
    assert op["op"] == "replace"
    if op["kind"] == "ids":
        assert len(op["ids"]) > IDS_IN_HASH_MAX
        return set(op["ids"])
    assert op["kind"] == "filter" and set(op["filters"]) == {"cats"}
    keep = np.ones(len(t), bool)
    for k, off in op["filters"]["cats"].items():
        keep &= ~np.isin(np.asarray(t[k]).astype(str), off)
    return {str(i) for i in t["id"][keep]}


#: legacy capabilities this port does not provide yet, with the reason (contract issue: no
#: cross-portal records reference; the 2640 spectra are linked per star by spec_url)
WAIVED = {"wd2_manifold": {"spectrum_viewer_1d"}}


# -- (a) ------------------------------------------------------------------------------------
def test_import_has_no_side_effects(tmp_path):
    code = ("import sys; sys.path.insert(0, %r); import adapters.wd2 as m; "
            "assert m.ADAPTER.name == 'wd2'" % str(REPO))
    subprocess.run([sys.executable, "-c", code], cwd=tmp_path, check=True, timeout=120)
    assert list(tmp_path.iterdir()) == []


# -- (b) ------------------------------------------------------------------------------------
@pytest.mark.parametrize("tier", ["A", "B"])
def test_datasets_valid_and_fast(runs, tier):
    r = runs[tier]
    assert r["dt"] < 120, f"datasets() took {r['dt']:.1f} s"
    ds = r["ds"]
    for k in ("src", "ccd", "sabbi", "seds"):
        assert isinstance(ds[k], CatalogSpec), k
    assert isinstance(ds["sedimg"], ImageSetSpec) and isinstance(ds["sedfig"], ImageSetSpec)
    assert isinstance(ds["iso"], IsochroneSpec) and isinstance(ds["hips"], HipsSpec)
    assert isinstance(ds["readme"], ReadmeSpec)
    nspec = sum(1 for s in r["adapter"]._raw["mani"]["stars"] if s.get("spec"))
    assert LIMIT <= len(ds["src"].table) <= 2 * LIMIT + nspec
    for k in ("ccd", "sabbi", "seds"):
        assert len(ds[k].table) == LIMIT, k
    for p in r["pages"]:
        assert isinstance(p, PageSpec)
        for k in p.datasets:
            assert k in ds, (p.id, k)
    assert [p.id for p in r["pages"]] == PAGES


def test_tier_c_unavailable(tmp_path):
    ctx = BuildContext(family=FAMILY, site=SITE, staging=tmp_path, tier="C", limit=LIMIT, log=lambda m: None)
    with pytest.raises(TierUnavailable):
        mod.Wd2Adapter().datasets(ctx)


# -- (c) ------------------------------------------------------------------------------------
@pytest.mark.parametrize("tier", ["A", "B"])
def test_ids_unique_and_shared(runs, tier):
    ds = runs[tier]["ds"]
    pat = {"src": r"WD2-\d{6}", "ccd": r"WD2-\d{6}", "seds": r"WD2-\d{6}", "sabbi": r"SABBI-\d{6}"}
    for key, rx in pat.items():
        ids = [str(x) for x in ds[key].table[ds[key].id]]
        assert len(set(ids)) == len(ids), key
        assert all(re.fullmatch(rx, i) for i in ids), key
    src = set(str(i) for i in ds["src"].table["id"])
    # fixture builds keep every SED star and every NIRSpec star in src (gallery/detail links resolve)
    assert set(str(i) for i in ds["seds"].table["id"]) <= src
    for name in ("sedimg", "sedfig"):
        items = [it.id for it in ds[name].items]
        assert len(set(items)) == len(items)
        assert set(items) <= set(str(i) for i in ds["seds"].table["id"])
    assert {it.id for it in ds["sedfig"].items} <= src


def test_ccd_rows_are_catalogue_rows(runs):
    """map_ccd rows get the catalogue row number as id (WD2-<row>, the manifold naming)."""
    from astropy.table import Table
    raw = runs["B"]["adapter"]._raw
    cat = Table.read(mod.CATALOG, format="ascii.ecsv", include_names=["RA", "DEC"])
    ra, dec = np.asarray(cat["RA"], float), np.asarray(cat["DEC"], float)
    rows = raw["ccd_rows"]
    assert len(rows) == len(raw["ccd"]["R"]) and np.all(np.diff(rows) > 0)
    assert np.max(np.abs(ra[rows] - np.asarray(raw["ccd"]["R"], float))) <= 5.1e-6
    assert np.max(np.abs(dec[rows] - np.asarray(raw["ccd"]["D"], float))) <= 5.1e-6
    stars = raw["mani"]["stars"]
    for s in stars[:200] + stars[-200:]:
        i = int(s["n"].split("-")[1])
        assert abs(ra[i] - s["ra"]) < 1e-5 and abs(dec[i] - s["dec"]) < 1e-5, s["n"]
    with pytest.raises(ValueError):
        mod._rows([1.0], [2.0], np.array([0.5]), np.array([0.5]))


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


# -- (f) ------------------------------------------------------------------------------------
def test_tier_parity(runs):
    a, b = runs["A"]["ds"], runs["B"]["ds"]
    for key in ("src", "ccd", "sabbi", "seds"):
        ta, tb = a[key].table, b[key].table
        assert list(ta["id"]) == list(tb["id"]), key
        assert ta.colnames == tb.colnames, key
        for c in b[key].columns:
            if c.role in ("mag", "value"):
                x, y = np.asarray(ta[c.key], float), np.asarray(tb[c.key], float)
                assert np.array_equal(np.isfinite(x), np.isfinite(y)), (key, c.key)
                ok = np.isfinite(x)
                assert np.allclose(x[ok], y[ok], rtol=1e-6, atol=1e-3), (key, c.key)
            elif c.role == "category":
                assert list(ta[c.key]) == list(tb[c.key]), (key, c.key)
        assert [v.key for v in a[key].virtual] == [v.key for v in b[key].virtual], key
        assert [p.key for p in a[key].presets] == [p.key for p in b[key].presets], key
    for k in ("base", "overlays", "rungs", "target"):
        assert getattr(a["hips"], k) == getattr(b["hips"], k), k
    sa = {p.key: p.data["sel"] for p in a["src"].presets if p.kind == "selection"}
    sb = {p.key: p.data["sel"] for p in b["src"].presets if p.kind == "selection"}
    assert sa == sb


# -- (g) ------------------------------------------------------------------------------------
def test_caps_superset_of_legacy(built):
    """Inventory caps per legacy page. Only the WAIVED caps may be missing (each recorded in
    caps.disable); density_rendering needs n > T0_MAX."""
    from portalkit.constants import T0_MAX
    pages = {p["id"]: p for p in _report(built)["pages"]}
    checked = 0
    for pc in SITE.portal().pages:
        pg = pages[pc.id]
        enabled, disabled = set(pg["caps"]["enabled"]), set(pg["caps"].get("disabled") or {})
        waived = WAIVED.get(pc.id, set())
        assert waived <= disabled, pc.id
        for leg in pc.legacy:
            need = set(INV[f"{FAMILY}/{leg}"]) - waived
            missing = need - enabled
            if missing == {"density_rendering"}:
                ns = [d["n"] for d in _report(built)["datasets"].values() if isinstance(d, dict) and "n" in d]
                assert max(ns) <= T0_MAX
                missing = set()
            assert not missing, (pc.id, sorted(missing))
            checked += 1
    assert checked == 6
    assert set(pages["wd2_manifold"]["caps"]["disabled"]) == {"spectrum_viewer_1d"}
    assert set(pages["sabbi"]["caps"]["disabled"]) == {"isochrones_or_tracks"}


def test_port_table_caps_filled(built):
    """capabilities_to_fill and flags of port table 14.11; ``dens`` / density_rendering need
    n > T0_MAX rows (full build only); ``spec`` needs a records set (disabled, see above)."""
    pages = {p["id"]: p for p in _report(built)["pages"]}
    by_legacy = {leg: pc.id for pc in SITE.portal().pages for leg in pc.legacy}
    rows = [r for r in json.loads((REPO / "tests" / "data" / "port_table.json").read_text(encoding="utf-8"))
            if r["family"] == FAMILY]
    assert len(rows) == 6
    for r in rows:
        pg = pages[by_legacy[r["legacy_page"]]]
        assert set(r["capabilities_to_fill"]) - set(pg["caps"]["enabled"]) <= {"density_rendering"}, r["legacy_page"]
        assert set(r["flags"]) - {"dens", "spec"} <= set(pg["flags"]), r["legacy_page"]


def test_hips_layers(runs):
    """19 bases + map_ccd overlays + the manifold's extra cutout composites; CDS ids kept."""
    raw = runs["B"]["adapter"]._raw
    for tier in ("A", "B"):
        h = runs[tier]["ds"]["hips"]
        assert all(re.match(r"https://|P/", layer["url"]) for layer in h.base + h.overlays)
        assert sum(layer["default"] for layer in h.base) == 1 and h.base[0]["default"]
        assert len(h.base) == len(raw["bases"])
        urls = [layer["url"] for layer in h.base + h.overlays]
        assert len(set(urls)) == len(urls)
        assert {u for _, u in raw["surveys"]} <= set(urls)
        ids = [layer["id"] for layer in h.base + h.overlays]
        assert len(set(ids)) == len(ids)


def test_rungs_from_wavelengths(runs, tmp_path):
    w = runs["B"]["ctx"].legacy.module("WD2_Portal_WavelengthExplorer")
    for tier in ("A", "B"):
        h = runs[tier]["ds"]["hips"]
        assert len(h.rungs) == len(w.WAVELENGTHS)
        for r, (tag, d, label, _) in zip(h.rungs, w.WAVELENGTHS):
            assert r["id"] == f"w{tag}" and r["label"] == label
            assert r["url"] == f"{mod.AVM}{d.strip('/')}/"
            assert {"r_um", "g_um", "b_um"} <= set(r) or label.count("μm") != 3
        assert h.target["fov"] == pytest.approx(w.WD2_FOV)


# -- special handling (section 14.11) --------------------------------------------------------
def test_manifold_dims_and_virtual_colours(runs):
    ds = runs["B"]["ds"]
    raw = runs["B"]["adapter"]._raw
    src = ds["src"]
    assert len(src.manifold.dims) == len(raw["mani"]["dims"])
    stored = {c.key for c in src.columns}
    t = src.table
    assert src.virtual
    for v in src.virtual:
        a, b = v.key.split("-")
        assert a in stored and b in stored and v.key in src.manifold.dims
        assert v.key not in stored
    pos = {s["n"]: i for i, s in enumerate(raw["mani"]["stars"])}
    rows = np.array([pos[i] for i in t["id"]])
    as_virtual = [d for d in raw["mani"]["dims"] if d["key"].startswith("c_") and d["key"] not in stored]
    assert len(as_virtual) == len(src.virtual)
    for d, v in zip(as_virtual, src.virtual):
        assert v.label == d["label"]
        a, b = v.key.split("-")
        x = np.array(d["v"], float)[rows]
        y = np.asarray(t[a], float) - np.asarray(t[b], float)
        ok = np.isfinite(x)
        assert np.array_equal(ok, np.isfinite(y)) and np.all(np.abs(x[ok] - y[ok]) < 2e-3), d["key"]
    pca = next(p for p in src.presets if p.key == "pca")
    assert pca.kind == "frame" and len(pca.data["F"]) == 2 * len(src.manifold.dims)
    assert src.manifold.default_frame == "pca"
    assert ds["sabbi"].manifold.default_frame == ds["sabbi"].presets[0].key


def test_preselects_are_saved_selections(runs):
    ds = runs["B"]["ds"]
    raw = runs["B"]["adapter"]._raw
    t = ds["src"].table
    ids = set(str(i) for i in t["id"])
    sel = {p.key: p.data["sel"] for p in ds["src"].presets if p.kind == "selection"}
    assert {"hasSpec", "fullCov"} <= set(sel)
    got = {k: _sel_ids(s, t) for k, s in sel.items()}
    for k, s in got.items():
        assert s and s <= ids, k
    spec_ids = {str(i) for i, s in zip(t["id"], t["spec"]) if s}
    assert got["hasSpec"] == spec_ids
    assert got["fullCov"] == {str(i) for i, o in zip(t["id"], t["obs"]) if o == "15-16"}
    assert spec_ids == {s["n"] for s in raw["mani"]["stars"] if s.get("spec")}
    for k, c in (raw["mani"].get("clusters") or {}).items():
        have = set(c["names"]) & ids
        assert (k in sel) == bool(have)
        if have:
            assert got[k] == have
    # ewocs9 null -> no EWOCS-IX buttons
    if not any("ew9" in s for s in raw["mani"]["stars"]):
        assert not {"ew9All", "ew9Strong", "ew9Disk"} & set(sel)
    for u, s in zip(t["spec_url"], t["spec"]):
        assert (u == "") == (s == "")
        if s:
            assert u == mod.SPEC_LINK.format(s)


def test_colorcolor_diagrams(runs):
    ds = runs["B"]["ds"]
    raw = runs["B"]["adapter"]._raw
    cc = [p for p in ds["src"].presets if p.key.startswith("cc_")]
    assert [p.key for p in cc] == [f"cc_{d['key']}" for d in raw["diagrams"]]
    dims = set(ds["src"].manifold.dims) | {c.key for c in ds["src"].columns}
    for p in cc:
        for e in (p.data["x"], p.data["y"]):
            assert all(k in dims for k in e.split("-")), (p.key, e)
    page = {p.id: p for p in runs["B"]["pages"]}["wd2_colorcolor"]
    grid = page.layout["row"][0]["col"][0]["options"]["grid"]
    assert grid == [[p.data["x"], p.data["y"]] for p in cc]


def test_ccd_dataset(runs):
    ds = runs["B"]["ds"]["ccd"]
    raw = runs["B"]["adapter"]._raw["ccd"]
    t = ds.table
    mags = [c for c in ds.columns if c.role == "mag"]
    assert len(mags) == len(raw["F"])
    assert {c.key for c in mags if c.filter is None} == {f"hst_{b}" for b in mod.HST}
    for c in mags:
        f = c.key.removeprefix("hst_")
        m = np.asarray(raw["M"][f][:LIMIT], float)
        v = np.asarray(t[c.key], float)
        assert np.array_equal(np.isfinite(v), m != 0)
        assert np.allclose(v[m != 0], m[m != 0] / 100)
    pre = {p.key: p for p in ds.presets}
    assert {f"snr{s}" for s in raw["snr_choices"]} <= set(pre) and {"cmd", "ccd"} <= set(pre)
    assert {d.key for d in ds.density} == {"cmd", "ccd"}
    # legacy per-diagram S/N cut: snr<T>_cmd / _ccd range over that diagram's filters only
    for s in raw["snr_choices"][1:]:
        for d in ("cmd", "ccd"):
            r = pre[f"snr{s}_{d}"].data["filters"]["ranges"]
            assert set(r) == {f"sn_{f}" for f in raw[d]} and all(v == [s, 1000] for v in r.values())
        assert set(pre[f"snr{s}"].data["filters"]["ranges"]) == {f"sn_{f}" for f in raw["cmd"] + raw["ccd"]}
    page = {p.id: p for p in runs["B"]["pages"]}["map_ccd"]
    assert page.state["filters:ccd"] == pre[f"snr{raw['snr']}"].data["filters"]
    src = {str(i) for i in runs["B"]["ds"]["src"].table["id"]}
    seds = {str(i) for i in runs["B"]["ds"]["seds"].table["id"]}
    assert [str(i) in src for i in t["id"]] == list(t["in_src"])
    assert [str(i) in seds for i in t["id"]] == list(t["has_sed"])


def test_sed_gallery(runs):
    ds = runs["B"]["ds"]
    seds, img = ds["seds"], ds["sedimg"]
    from astropy.table import Table
    full = Table.read(mod.SED_ECSV)
    minfilt = int(np.nanmin(np.asarray(full["n_filt_independent"], float)))
    page = {p.id: p for p in runs["B"]["pages"]}["wd2_sedgallery"]
    assert f">= {minfilt} filters" in page.subtitle
    assert list(seds.table["id"]) == [str(n) for n in full["name"][:LIMIT]]
    for it in img.items:
        assert it.roles["sed"] == f"{mod.PNG_URL}{it.id}.png"
    side = page.layout["row"][2]["col"]
    assert side[0]["pane"] == "sed" and side[0]["options"]["models"] == ["blackbody"]
    assert {c["options"]["expr"] for c in side if c["pane"] == "hist"} == {"bb_T", "bb_av", "bb_chi2_red",
                                                                          "n_filt_independent"}
    assert all(c["options"]["brush"] for c in side if c["pane"] == "hist")
    gal = page.layout["row"][1]
    assert gal["pane"] == "gallery" and "bb_T" in gal["options"]["sort"]
    off = next(p for p in seds.presets if p.key == "bbfit").data["filters"]["cats"]["fit"]
    assert "BB fit" not in off and set(off) == set(seds.column("fit").levels) - {"BB fit"}
    for k in ("bb_T", "bb_av", "bb_chi2_red", "n_filt_independent"):
        assert seds.column(k).role == "value"
    fit = list(seds.table["fit"])
    T = np.asarray(seds.table["bb_T"], float)
    assert all((f == "no fit") == (not np.isfinite(x)) for f, x in zip(fit, T))


def test_no_hard_coded_counts(runs):
    src = Path(mod.__file__).read_text(encoding="utf-8")
    assert not re.search(r"13[,_]?409|19[,_]?453|19[,_]?471|6[,_]?711|9[,_]?267|\b164\b|\b22 RGB|\b55\b", src)
    md = runs["B"]["ds"]["readme"].markdown
    raw = runs["B"]["adapter"]._raw
    assert f"{len(raw['diagrams'])} panels" in md
    assert f"{len(raw['rungs'])} RGB composites" in md
    assert f"{len(raw['mani']['dims'])} dims" in md
    assert "/orange/" not in md


def test_src_skips_diagrams_outside_the_dims():
    """A colour-colour diagram whose filter is not a manifold dim is dropped, not a crash."""
    def dim(k, v):
        return {"key": k, "label": k, "unit": "mag", "group": "nircam", "range": [10, 20], "scale": 1.0, "v": v}
    stars = [{"n": f"WD2-{i:06d}", "ra": 155.9 + i * 1e-4, "dec": -57.76, "obs": "15-16"} for i in range(6)]
    pl = {"stars": stars, "dims": [dim("m_f115w", [14.0 + i for i in range(6)]),
                                   dim("m_f200w", [13.0 + i for i in range(6)])],
          "groups": [["nircam", "NIRCam"]], "categories": [{"key": "obs", "label": "Filters", "colors": {}}],
          "presets": [{"id": "cmd", "name": "CMD", "x": {"m_f115w": 1}, "y": {"m_f200w": 1}}]}
    diags = [{"key": "a", "title": "A", "xfilters": ["f115w", "f200w"], "yfilters": ["f200w"]},
             {"key": "b", "title": "B", "xfilters": ["f115w", "f770w"], "yfilters": ["f200w"]}]
    cat = mod.Wd2Adapter()._src({"mani": pl, "diagrams": diags}, np.arange(6), set())
    assert [p.key for p in cat.presets if p.key.startswith("cc_")] == ["cc_a"]


def test_sel_spelling():
    from portalkit.constants import IDS_IN_HASH_MAX
    from portalkit.legacy.hashmap import decode_recipe
    few = [f"WD2-{i:06d}" for i in range(3)]
    assert mod._sel(few, "src") == "ids:" + ",".join(few)
    many = [f"WD2-{i:06d}" for i in range(IDS_IN_HASH_MAX + 1)]
    s = mod._sel(many, "src")
    assert s.startswith("r:") and len(s) < len(",".join(many))
    assert decode_recipe(s) == [{"op": "replace", "kind": "ids", "view": {"ds": "src"}, "ids": many}]


@pytest.mark.xfail(strict=True, reason="contract issue: kv-view has no alias from the legacy colour keys "
                   "(c_<a>_<b>) to the virtual dims (m_<a>-m_<b>)")
def test_legacy_colour_axis_hash(built):
    """Legacy wd2_manifold share links name colour dims by their legacy key (#x=c_f115w_f150w)."""
    from portalkit.legacy.hashmap import map_legacy
    html = (built["portal"] / "wd2_manifold.html").read_text(encoding="utf-8")
    m = re.search(r'<script[^>]*id="pk-page"[^>]*>(.*?)</script>', html, re.S)
    page = json.loads(m.group(1).replace("<\\/", "</"))
    ok = map_legacy(["kv-view", "bare-name"], {"hash": "#x=m_f115w&y=m_f200w", "page": page})
    assert any(k.endswith(".frame") for k in ok["hash"])
    out = map_legacy(["kv-view", "bare-name"], {"hash": "#x=c_f115w_f150w&y=m_f200w", "page": page})
    assert any(k.endswith(".frame") for k in out["hash"])


def test_cds_layer_and_rgb_parse():
    raw = {"bases": [["2MASS", "P/2MASS/color"], ["Wd2", "https://x/y/"]], "overlays": [["Ov", "https://o/"]],
           "surveys": [["Ov", "https://o/"], ["Extra", "https://e/"]], "center": [155.9, -57.76], "fov": 0.06,
           "rungs": [("115", "wd2_RGB_162-150-115_hips", "RGB: 1.62μm / 1.50μm / 1.15μm", "d"),
                     ("1130", "wd2_1130_hips/", "MIRI 11.3μm", "e")]}
    h = mod.Wd2Adapter()._hips(raw)
    assert h.base[0]["url"] == "P/2MASS/color" and h.base[0]["default"] and not h.base[1]["default"]
    assert [o["name"] for o in h.overlays] == ["Ov", "Extra"]
    assert h.rungs[0]["r_um"] == 1.62 and h.rungs[0]["b_um"] == 1.15
    assert "r_um" not in h.rungs[1] and h.rungs[1]["url"].endswith("wd2_1130_hips/")
