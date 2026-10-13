"""Contract tests for the gc_manifold adapter (ARCHITECTURE.md section 9.6, port table 14.2).

The GC manifold binaries under ``/orange/adamginsburg/jwst/releases/site/<field>_manifold``
are referenced in place (foreign ``gc-manifest-1`` catalogs), so a fixture build (``limit=50``)
still describes the whole catalogue; nothing is re-encoded, only the u8 tile index is widened.
Derived statistics (fit ranges, red-clump peak, sky centre) are memoised in the portalkit cache
(``$PK_CACHE_DIR``), so only the first run decodes the 3.7M-row Treasury dims.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qsl

import numpy as np
import pytest

from portalkit.build import build_family, check_build
from portalkit.context import BuildContext, expand_inputs, load_site
from portalkit.deploy import ExposureMap
from portalkit.spec import (CatalogSpec, ExtinctionSpec, FilterSetSpec, HipsSpec, PageSpec, ReadmeSpec,
                            TierUnavailable)

REPO = Path(__file__).resolve().parents[2]
LIMIT = 50
FAMILY = "gc_manifold"
INV = json.loads((REPO / "tests" / "data" / "inventory_caps.json").read_text(encoding="utf-8"))
PORT = [r for r in json.loads((REPO / "tests" / "data" / "port_table.json").read_text(encoding="utf-8"))
        if r["family"] == FAMILY]
TMP_ROOTS = tuple(os.path.realpath(p) for p in {tempfile.gettempdir(), "/tmp", "/var/tmp"})
SF_URL = "https://starformation.astro.ufl.edu/jwst-gc/"
INV_TREASURY = "GC Treasury catalog manifold viewer (treasury_manifold)"
INV_FIELDS = "GC per-field catalog manifold viewers (<field>_manifold; 11 instances of one template)"
INV_MONITOR = "jwst-gc pipeline monitor (monitor.html, fields/<target>.html, *_monitor5as.html)"

#: Capabilities and flags still missing on GC-manifest-backed fields after WPE9's gc_manifest.write
#: merge (column ``filter``/``id``/``spec.manifold`` now flow through -- ``num3``, ``selection_export``
#: and the G2 gate are resolved, so those are no longer here). The remaining gap is structural, not a
#: merge gap: GC catalogues are referenced in place with rows addressed by tile index, not a per-row
#: id (``CatalogSpec(id="")``), so ``cat`` (and its dependents ``detail_panel``/``hover_preview``/
#: ``source_list_search``) can never trigger; and 2-band fields (vs. treasury/arches/gc2211/
#: quintuplet's 3+) never reach ``mags3f``, so ``av_slider``/``model_template_overlay``/
#: ``labeled_features_overlay``/``live_sed_viewer``/``extinction_vectors_on_ccd`` stay missing there.
GC_MANIFEST_GAP_CAPS = {"detail_panel", "hover_preview", "source_list_search",
                        "live_sed_viewer", "extinction_vectors_on_ccd", "av_slider",
                        "model_template_overlay", "labeled_features_overlay"}
GC_MANIFEST_GAP_FLAGS = {"cat", "mags3f"}

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from adapters import gc_manifold as mod  # noqa: E402

SITE = load_site(FAMILY)
pytestmark = [pytest.mark.adapters,
              pytest.mark.skipif(not (mod.SITE_ROOT / "treasury_manifold" / "data" / "manifest.json").is_file()
                                 or not mod.FIELDS_JS.is_file(),
                                 reason="GC release site / legacy GC viewer assets not mounted")]


def _ctx(tier: str, staging: Path) -> BuildContext:
    return BuildContext(family=FAMILY, site=SITE, staging=staging, tier=tier, limit=LIMIT,
                        log=lambda m: None)


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """datasets() + pages() for both tiers (timed: contract 9.6(b))."""
    out = {}
    for tier in ("A", "B"):
        ctx = _ctx(tier, tmp_path_factory.mktemp(f"ctx{tier}"))
        adapter = mod.GcManifoldAdapter()
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
    return {"res": res, "stage": stage}


def _fields(runs) -> list[str]:
    return list(runs["B"]["adapter"]._info)


def _portals(built) -> list[Path]:
    return sorted(p.parent for p in built["res"].staging.glob("*/portal.json"))


def _inv_key(pid: str) -> str:
    return INV_TREASURY if pid == "treasury_manifold" else INV_FIELDS


# -- (a) ------------------------------------------------------------------------------------
def test_import_has_no_side_effects(tmp_path):
    code = ("import sys; sys.path.insert(0, %r); import adapters.gc_manifold as m; "
            "assert m.ADAPTER.name == 'gc_manifold'; "
            "assert 'GCFields_Portal_Manifold' not in sys.modules" % str(REPO))
    subprocess.run([sys.executable, "-c", code], cwd=tmp_path, check=True, timeout=120)
    assert list(tmp_path.iterdir()) == []


# -- (b) ------------------------------------------------------------------------------------
@pytest.mark.parametrize("tier", ["A", "B"])
def test_datasets_valid_and_fast(runs, tier):
    r = runs[tier]
    assert r["dt"] < 120, f"datasets() took {r['dt']:.1f} s"
    ds, ad = r["ds"], r["adapter"]
    fields = list(ad._info)
    assert "treasury" in fields and len(fields) + len(ad.skipped) == len(SITE.portals)
    for k in fields:
        assert isinstance(ds[f"cat_{k}"], CatalogSpec)
        assert isinstance(ds[f"hips_{k}"], HipsSpec)
        assert isinstance(ds[f"readme_{k}"], ReadmeSpec)
    assert isinstance(ds["flt"], FilterSetSpec) and isinstance(ds["ext"], ExtinctionSpec)
    pages = r["pages"]
    assert [p.portal for p in pages] == [f"{k}_manifold" for k in fields]
    for p in pages:
        assert isinstance(p, PageSpec) and p.id == "index" and p.archetype == "manifold"
        for k in p.datasets:
            assert k in ds, (p.portal, k)


def test_tier_c_unavailable(tmp_path):
    ctx = BuildContext(family=FAMILY, site=SITE, staging=tmp_path, tier="C", limit=LIMIT, log=lambda m: None)
    with pytest.raises(TierUnavailable):
        mod.GcManifoldAdapter().datasets(ctx)


# -- (c) ------------------------------------------------------------------------------------
def test_ids_unique(runs):
    for tier in ("A", "B"):
        pages = runs[tier]["pages"]
        keys = [(p.portal, p.id) for p in pages]
        assert len(set(keys)) == len(keys)
        ds = runs[tier]["ds"]
        for k in _fields(runs):
            cat = ds[f"cat_{k}"]
            # foreign catalogue: rows are addressed by index in the GC binaries
            assert cat.foreign is not None and cat.table is None
            cols = [c.key for c in cat.columns]
            assert len(set(cols)) == len(cols)
            assert not set(cols) & {v.key for v in cat.virtual}
            pre = [p.key for p in cat.presets]
            assert len(set(pre)) == len(pre)


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


def test_site_registry_is_external():
    assert SITE.family == FAMILY and SITE.status == "active"
    assert len(SITE.portals) == 12
    for p in SITE.portals:
        f = mod.field_of(p.id)
        assert p.served_by == "external" and p.visibility == "public"
        assert p.url == f"{SF_URL}{f}_manifold/unified/"
        assert Path(p.legacy_dir) == mod.SITE_ROOT / f"{f}_manifold"
        assert Path(p.target_dir) == Path(p.legacy_dir) / "unified"
        assert [pg.id for pg in p.pages] == ["index"]
        assert p.pages[0].grammar == ["gc-query"]


# -- (e) ------------------------------------------------------------------------------------
def test_fixture_build_passes_check(built, runs):
    res = built["res"]
    assert res.ok, res.report.get("errors")
    portals = _portals(built)
    assert [p.name for p in portals] == sorted(f"{k}_manifold" for k in _fields(runs))
    rep = check_build(res.staging, gate="G2")
    assert rep.ok, rep.errors
    for pdir in portals:
        report = json.loads((pdir / "build_report.json").read_text(encoding="utf-8"))
        assert report["tier"] == "B"
        for pg in report["pages"]:
            assert pg["first_paint_bytes"] <= 2_000_000, (pdir.name, pg["id"])


def test_fixture_build_full_g2(built):
    """legacy/gc_manifest.write's PathLabeler (WPE9) scrubs the local paths that used to leak
    into provenance.foreign.manifest / gc_source, so G2 now passes with no foreign-path carveout."""
    rep = check_build(built["res"].staging, gate="G2")
    assert rep.ok, rep.errors


# -- (f) ------------------------------------------------------------------------------------
def test_tier_parity(runs):
    a, b = runs["A"], runs["B"]
    assert list(a["adapter"]._info) == list(b["adapter"]._info)
    for k in _fields(runs):
        ia, ib = a["adapter"]._info[k], b["adapter"]._info[k]
        assert ia["bands"] == ib["bands"] and ia["cmd"] == ib["cmd"] and ia["ccd"] == ib["ccd"], k
        assert ia["stats"] == ib["stats"], k
        ca, cb = a["ds"][f"cat_{k}"], b["ds"][f"cat_{k}"]
        assert ca.caveats == cb.caveats
        assert [(p.key, p.kind, p.data) for p in ca.presets] == [(p.key, p.kind, p.data) for p in cb.presets]
        ha, hb = a["ds"][f"hips_{k}"], b["ds"][f"hips_{k}"]
        assert [(x["url"], x["name"]) for x in ha.base] == [(x["url"], x["name"]) for x in hb.base], k
        assert ha.target == hb.target
    assert a["ds"]["flt"].keys == b["ds"]["flt"].keys


# -- (g) ------------------------------------------------------------------------------------
def test_caps_superset_of_legacy(built):
    checked = 0
    for pdir in _portals(built):
        report = json.loads((pdir / "build_report.json").read_text(encoding="utf-8"))
        (pg,) = report["pages"]
        need = set(INV[f"{FAMILY}/{_inv_key(pdir.name)}"])
        missing = need - set(pg["caps"]["enabled"])
        assert missing <= GC_MANIFEST_GAP_CAPS, (pdir.name, sorted(missing - GC_MANIFEST_GAP_CAPS))
        checked += 1
    assert checked >= 2


def test_port_table_caps_filled(built):
    assert {r["legacy_page"] for r in PORT} == {INV_TREASURY, INV_FIELDS, INV_MONITOR}
    rows = {r["legacy_page"]: r for r in PORT}
    assert rows[INV_MONITOR]["archetype"] == "excluded"
    for pdir in _portals(built):
        report = json.loads((pdir / "build_report.json").read_text(encoding="utf-8"))
        (pg,) = report["pages"]
        r = rows[_inv_key(pdir.name)]
        assert pg["archetype"] == "manifold"
        missing = set(r["capabilities_to_fill"]) - set(pg["caps"]["enabled"])
        assert missing <= GC_MANIFEST_GAP_CAPS, (pdir.name, sorted(missing - GC_MANIFEST_GAP_CAPS))
        flags = set(r["flags"]) - set(pg["flags"])
        assert flags <= GC_MANIFEST_GAP_FLAGS, (pdir.name, sorted(flags))
        assert {"mani", "sky", "skycat", "dens", "multi"} <= set(pg["flags"]), pdir.name


# -- special handling (section 14.2) ---------------------------------------------------------
def test_gc_binaries_referenced_in_place(built):
    """Column files and position offsets are the starformation URLs of the GC tree; only the
    widened tile index is staged."""
    for pdir in _portals(built):
        f = mod.field_of(pdir.name)
        man = json.loads((pdir / "data" / f"cat_{f}" / "manifest.json").read_text(encoding="utf-8"))
        gc = json.loads((mod.SITE_ROOT / pdir.name / "data" / "manifest.json").read_text(encoding="utf-8"))
        base = f"{SF_URL}{f}_manifold/data/"
        assert man["n"] == gc["n"]
        mags = [c for c in man["cols"] if c["role"] == "mag"]
        assert [c["key"] for c in mags] == [d["key"] for d in gc["dims"]]
        for c in man["cols"]:
            if c.get("file"):
                assert c["file"].startswith(base), c
        files = man["pos"]["files"]
        assert files["dra"] == base + gc["meta"]["dra"]["file"]
        assert files["ddec"] == base + gc["meta"]["ddec"]["file"]
        assert not files["tile"].startswith("http") and (pdir / "data" / f"cat_{f}" / files["tile"]).is_file()
        staged = sorted(str(p.relative_to(pdir / "data")) for p in (pdir / "data").rglob("*") if p.is_file())
        assert staged == sorted(["hips.json", f"cat_{f}/manifest.json", f"cat_{f}/{files['tile']}"]), staged


def test_feature_caveats_become_caveats(runs, built):
    src = mod.FIELDS_JS.read_text(encoding="utf-8")
    fc_raw = mod.js_string_array(src, "FEATURE_CAVEATS")
    tc_raw = mod.js_string_array(mod.TREASURY_JS.read_text(encoding="utf-8"), "FEATURE_CAVEATS")
    assert len(fc_raw) >= 5 and len(tc_raw) >= 3
    assert any("Red clump" in c for c in fc_raw)
    # ported: a pure keyboard-shortcut caveat is dropped, legacy-control references rewritten
    fc = [c for c in (mod._port_caveat(x) for x in fc_raw) if c is not None]
    tc = [c for c in (mod._port_caveat(x) for x in tc_raw) if c is not None]
    assert len(fc) == len(fc_raw) - 1 and len(tc) == len(tc_raw) - 1
    assert not any(c.startswith("Keyboard shortcuts") for c in fc + tc)
    assert not any("checkbox below the plot" in c or '"Red clump" zoom button' in c for c in fc + tc)
    for k in _fields(runs):
        want = tc if k == "treasury" else fc
        assert runs["B"]["ds"][f"cat_{k}"].caveats == want
        md = runs["B"]["ds"][f"readme_{k}"].markdown
        assert all(f"- {c}" in md for c in want)
    for pdir in _portals(built):
        f = mod.field_of(pdir.name)
        man = json.loads((pdir / "data" / f"cat_{f}" / "manifest.json").read_text(encoding="utf-8"))
        want = tc if f == "treasury" else fc
        assert all(c in man["caveats"] for c in want), pdir.name


def test_pages_force_t2_and_cmd_axes(runs):
    for p in runs["B"]["pages"]:
        k = mod.field_of(p.portal)
        info = runs["B"]["adapter"]._info[k]
        assert p.state["tier:manifold"] == "T2" and p.state["tier:cmd"] == "T2"
        ax = p.state["axes:cmd"]
        a, b, y = info["cmd"]
        assert ax["y"] == y and ax["x"] == f"{a}-{b}"
        assert ax["xr"] == info["stats"]["xr"] and ax["yr"] == info["stats"]["yr"]
        assert ax["xr"][0] < ax["xr"][1] and ax["yr"][0] < ax["yr"][1]
        # the colour is the short-minus-long pivot band, as the legacy swapToShortLong
        assert mod._um(a) < mod._um(b)


def test_built_page_grammar_dims_and_state(built):
    from portalkit.legacy.extract import script_json
    for pdir in _portals(built):
        page = script_json(pdir / "index.html", "pk-page")
        assert page["state"]["tier:manifold"] == "T2"
        assert page["page"]["legacy"]["grammar"] == ["gc-query"]
        assert page["page"]["archetype"] == "manifold"
        stack = [page["layout"]]
        mani = []
        while stack:
            n = stack.pop()
            if isinstance(n, dict):
                if n.get("pane") == "manifold":
                    mani.append(n)
                stack.extend(v for v in n.values() if isinstance(v, (list, dict)))
            elif isinstance(n, list):
                stack.extend(n)
        assert len(mani) == 1
        gc = json.loads((mod.SITE_ROOT / pdir.name / "data" / "manifest.json").read_text(encoding="utf-8"))
        # gc-query cb/cd/a/b/m index the manifold dims in legacy order
        assert mani[0]["options"]["dims"] == [d["key"] for d in gc["dims"]]


def test_red_clump_preset_and_link(runs):
    n_rc = 0
    for k in _fields(runs):
        info = runs["B"]["adapter"]._info[k]
        rc = mod.GcManifoldAdapter.red_clump(info)
        cat = runs["B"]["ds"][f"cat_{k}"]
        md = runs["B"]["ds"][f"readme_{k}"].markdown
        pre = {p.key: p for p in cat.presets}
        if rc is None:
            assert "red_clump" not in pre and "## Red clump" not in md
            continue
        n_rc += 1
        a, b, y = info["cmd"]
        st = info["stats"]
        # a zoom of the CMD axes (matches the readme link), not a 'filter' that would hide
        # every star outside the box
        assert pre["red_clump"].kind == "axes"
        assert pre["red_clump"].data == {"x": f"{a}-{b}", "y": y, "xr": rc["xr"], "yr": rc["yr"]}
        # box: densest bin +- 1/10 of the default fit range
        assert rc["xr"][1] - rc["xr"][0] == pytest.approx(0.2 * (st["xr"][1] - st["xr"][0]), abs=2e-4)
        assert rc["yr"][1] - rc["yr"][0] == pytest.approx(0.2 * (st["yr"][1] - st["yr"][0]), abs=2e-4)
        # (the peak-vs-fit-range check used to live here; the peak is drawn from the wider
        # PEAK_PCT box, which can legitimately sit outside the narrower color fit range on a
        # field, so that comparison does not hold in general -- see
        # test_field_stats_match_legacy_complete_row_sample for a real legacy-parity check)
        m = re.search(r"\]\((#[^)]+)\)", md[md.index("## Red clump"):])
        q = dict(parse_qsl(m.group(1)[1:]))
        assert q["cmd.x"] == f"{a}-{b}" and q["cmd.y"] == y
        assert [float(v) for v in q["cmd.xr"].split(",")] == rc["xr"]
        assert [float(v) for v in q["cmd.yr"].split(",")] == rc["yr"]
    assert n_rc >= 2


def test_treasury_red_clump_from_data(runs):
    """The Treasury peak recomputed from the decoded GC dims (legacy stride, both dims finite)
    matches the memoised stats."""
    info = runs["A"]["adapter"]._info["treasury"]
    f = info["f"]
    dims = {d["key"]: d for d in f["man"]["dims"]}
    a, b, y = info["cmd"]
    n = int(f["man"]["n"])
    stride = max(1, n // mod.SAMPLE_STRIDE_CAP)
    idx = np.arange(0, n, stride)
    xa, xb = (mod._u16(f["data"] / dims[k]["file"], dims[k]["min"], dims[k]["max"], n)[idx] for k in (a, b))
    ok = np.isfinite(xa) & np.isfinite(xb)
    assert n > 3_000_000 and stride > 1 and info["stats"]["n_cmd"] == int(ok.sum())
    peak = mod.density_peak(xa[ok] - xb[ok], xa[ok])
    assert list(peak) == pytest.approx(info["stats"]["peak"])
    assert 14 < peak[1] < 22 and 0.5 < peak[0] < 3.0


def test_field_stats_match_legacy_complete_row_sample(runs):
    """xr/yr/peak of a multi-band field match a recomputation from the legacy ``sampleJoint``
    (stride, every manifold dim finite), not just the CMD triple's dims (regression: a field
    with sparser coverage in a non-CMD band used to admit rows the legacy fit/peak never saw)."""
    info_all = runs["A"]["adapter"]._info
    for k in ("w51", "cloudc", "brick_o004", "sgrc", "arches", "gc2211", "sgra", "quintuplet"):
        if k not in info_all or len(info_all[k]["bands"]) < 3:
            continue
        info = info_all[k]
        f = info["f"]
        dims = {d["key"]: d for d in f["man"]["dims"]}
        keys = [d["key"] for d in f["man"]["dims"]]
        a, b, y = info["cmd"]
        n = int(f["man"]["n"])
        stride = max(1, n // mod.SAMPLE_STRIDE_CAP)
        idx = np.arange(0, n, stride)
        X = {kk: mod._u16(f["data"] / dims[kk]["file"], dims[kk]["min"], dims[kk]["max"], n)[idx] for kk in keys}
        ok = np.ones(idx.size, dtype=bool)
        for kk in keys:
            ok &= np.isfinite(X[kk])
        color, mag = X[a][ok] - X[b][ok], X[y][ok]
        pct = mod.FIT_PCT["fields"]
        xr, yr = mod._pct(color, pct["color"]), mod._pct(mag, pct["mag"])
        peak = mod.density_peak(color, mag)
        st = info["stats"]
        assert xr is not None and yr is not None and peak is not None
        assert st["xr"] == pytest.approx(xr, abs=1e-3)
        assert st["yr"] == pytest.approx(yr, abs=1e-3)
        assert list(peak) == pytest.approx(st["peak"], abs=1e-3)
        return
    pytest.skip("no 3+ band field available in this fixture")


def test_density_peak_matches_legacy_scan():
    rng = np.random.default_rng(3)
    c = np.concatenate([rng.uniform(0, 4, 20000), rng.normal(1.7, 0.03, 3000)])
    m = np.concatenate([rng.uniform(12, 24, 20000), rng.normal(18.8, 0.08, 3000)])
    pc, pm = mod.density_peak(c, m)
    assert abs(pc - 1.7) < 0.1 and abs(pm - 18.8) < 0.3
    # legacy loop: strict '>' keeps the first maximum in row-major (y, x) order
    (c0, c1), (m0, m1) = np.percentile(c, mod.PEAK_PCT), np.percentile(m, mod.PEAK_PCT)
    nb = mod.PEAK_BINS
    grid = np.zeros((nb, nb), int)
    for x, yv in zip(c, m):
        gx, gy = int(np.floor((x - c0) / ((c1 - c0) / nb))), int(np.floor((yv - m0) / ((m1 - m0) / nb)))
        if 0 <= gx < nb and 0 <= gy < nb:
            grid[gy, gx] += 1
    best, bi = 0, 0
    for i, v in enumerate(grid.ravel()):
        if v > best:
            best, bi = v, i
    assert (pc, pm) == pytest.approx((c0 + (bi % nb + 0.5) * (c1 - c0) / nb, m0 + (bi // nb + 0.5) * (m1 - m0) / nb))
    assert mod.density_peak(np.ones(10), np.arange(10.0)) is None


def test_js_string_array_tokenizer():
    src = """const X = 1;
const FEATURE_CAVEATS = [
  'one ' + "two's",   // comment, with ] bracket
  "three \\"q\\"",
  'four \\'x\\'' +
    ' five',
];
const NEXT = ['no'];"""
    assert mod.js_string_array(src, "FEATURE_CAVEATS") == ["one two's", 'three "q"', "four 'x' five"]
    with pytest.raises(ValueError):
        mod.js_string_array(src, "MISSING")


def test_hips_layers_and_ccd(runs):
    for tier in ("A", "B"):
        for k in _fields(runs):
            h = runs[tier]["ds"][f"hips_{k}"]
            assert h.base and all(x["url"].startswith("https://") for x in h.base)
            assert sum(x["default"] for x in h.base) == 1
            assert 0.02 <= h.target["fov"] <= 4.0
    info = runs["B"]["adapter"]._info
    t = info["treasury"]
    assert t["bands"] == ["f212n", "f480m"] and t["ccd"] is None and t["cmd"] == ("f212n", "f480m", "f212n")
    # values the legacy viewer (ccdDefaults / firstCoveredIdx + swapToShortLong) shows on first load
    legacy = {"w51": (("f140m", "f162m", "f140m"), ("f140m-f480m", "f162m-f410m")),
              "brick_o004": (("f200w", "f356w", "f200w"), ("f200w-f444w", "f200w-f356w")),
              "sgra": (("f212n", "f405n", "f212n"), ("f115w-f405n", "f115w-f212n")),
              "sgrc": (("f162m", "f405n", "f162m"), ("f162m-f480m", "f162m-f405n")),
              "cloudc": (("f182m", "f187n", "f182m"), ("f182m-f466n", "f187n-f410m")),
              "arches": (("f212n", "f323n", "f212n"), None)}
    seen = 0
    for k, (cmd, ccd) in legacy.items():
        if k in info:
            assert info[k]["cmd"] == cmd and info[k]["ccd"] == ccd, k
            seen += 1
    assert seen >= 4
    w = info.get("w51")
    if w is not None:
        assert -10 < w["stats"]["center"][1] - 14.5 < 10


def test_legacy_axis_defaults_edge_cases():
    src = mod.FIELDS_JS.read_text(encoding="utf-8")
    ext, piv = mod.js_number_map(src, "EXT_A_AKS"), mod.js_number_map(src, "PIVOT_UM")
    assert "f115w" not in ext and piv["f115w"] == pytest.approx(1.1434) and ext["f212n"] == pytest.approx(1.0281)
    # dims out of pivot order: colour swapped short-long, magnitude stays the first covered dim
    assert mod.legacy_cmd(["f444w", "f200w", "f115w"], ext, piv) == ("f200w", "f444w", "f444w")
    # fewer than two covered dims: dims 0 and 1 (then swapped), magnitude the covered one
    assert mod.legacy_cmd(["f115w", "f150w", "f212n"], ext, piv) == ("f115w", "f150w", "f212n")
    piv2 = {**piv, "f150w": 1.4996, "f070w": 0.7039}
    assert mod.legacy_cmd(["f150w", "f115w"], ext, piv2) == ("f115w", "f150w", "f150w")
    # a band without a pivot (f150w is not in the fields viewer's PIVOT_UM) leaves the order alone
    assert mod.legacy_cmd(["f150w", "f115w"], ext, piv) == ("f150w", "f115w", "f150w")
    assert mod.legacy_cmd(["f212n"], ext, piv) == ("f212n", "f212n", "f212n")
    assert mod.legacy_cmd(["f480m", "f212n"], ext, piv, treasury=True) == ("f212n", "f480m", "f480m")
    with pytest.raises(ValueError):
        mod.legacy_cmd([], ext, piv)
    assert mod.legacy_ccd(["f212n", "f480m"], ext, piv) is None
    assert mod.legacy_ccd(["f212n"], ext, piv) is None
    # 3 covered: Y shares X's short end; 4+: widest then next baseline
    assert mod.legacy_ccd(["f480m", "f212n", "f162m"], ext, piv) == ("f162m-f480m", "f162m-f212n")
    assert mod.legacy_ccd(["f162m", "f210m", "f360m", "f480m", "f115w"], ext, piv) == ("f162m-f480m", "f210m-f360m")
    # < 3 covered: positional fallback, each colour ordered by pivot
    assert mod.legacy_ccd(["f150w", "f115w", "f212n"], ext, piv2) == ("f150w-f212n", "f115w-f150w")
    assert mod.legacy_ccd(["f150w", "f115w", "f212n"], ext, piv) == ("f150w-f212n", "f150w-f115w")
    assert mod.legacy_ccd(["f150w", "f115w", "f212n", "f070w"], ext, piv2) == ("f070w-f150w", "f115w-f212n")


def test_js_unescape_and_number_map():
    assert mod.js_unescape(r"caf\u00e9 a\nb\tc \x41 \u{1F600} \uD83D\uDE00 \\ \' \" \q") == \
        "café a\nb\tc A 😀 😀 \\ ' \" q"
    assert mod.js_unescape("a\\\nb") == "ab"          # line continuation
    assert mod.js_unescape("\\0") == "\0"
    with pytest.raises(ValueError):
        mod.js_unescape("\\u{110000}")
    # a lone, unpaired surrogate (malformed JS, but legacy source isn't ours to fix)
    # must round-trip rather than raise UnicodeDecodeError
    assert mod.js_unescape(r"a\uD83Db") == "a\ud83db"
    src = "const X = ['caf\\u00e9 line1\\nline2' /* c */, 'b'];"
    assert mod.js_string_array(src, "X") == ["café line1\nline2", "b"]
    assert mod.js_string_array("const E = [];", "E") == []
    with pytest.raises(ValueError):
        mod.js_string_array("const E = [1];", "E")
    m = "const T = {\n  a1: 1.5, 'b': -2e-3, // note: 9\n  /* c: 7 */ c: .25,\n};"
    assert mod.js_number_map(m, "T") == {"a1": 1.5, "b": -0.002, "c": 0.25}
    with pytest.raises(ValueError):
        mod.js_number_map("const T = { a: x };", "T")
    with pytest.raises(ValueError):
        mod.js_number_map(m, "U")


def test_percentiles_and_peak_edge_cases():
    assert mod._pct(np.array([]), (0.5, 97.0)) is None
    assert mod._pct(np.array([np.nan, np.inf]), (0.5, 97.0)) is None
    assert mod._pct(np.full(5, 3.0), (0.5, 97.0)) is None
    assert mod._pct(np.array([0.0, np.nan, 10.0]), (0.0, 100.0)) == [0.0, 10.0]
    assert mod.density_peak(np.array([]), np.array([])) is None
    assert mod.density_peak(np.array([np.nan] * 3), np.arange(3.0)) is None
    c = np.array([1.0, 1.0, 1.0, 2.0, np.nan])
    m = np.array([10.0, 10.0, 10.0, 12.0, 11.0])
    pc, pm = mod.density_peak(c, m, nbins=4)
    assert pc < 1.5 and pm < 11.0
    info = {"stats": {"peak": [1.0, 2.0], "xr": None, "yr": [0.0, 1.0]}, "cmd": ("a", "b", "a")}
    assert mod.GcManifoldAdapter.red_clump(info) is None
    info["stats"]["xr"] = [0.0, 10.0]
    assert mod.GcManifoldAdapter.red_clump(info) == {"peak": [1.0, 2.0], "xr": [0.0, 2.0], "yr": [1.9, 2.1]}
    info["cmd"] = ("a", "a", "a")
    assert mod.GcManifoldAdapter.red_clump(info) is None


def test_gz_size_counts_every_member(tmp_path):
    p = tmp_path / "x.gz"
    p.write_bytes(gzip.compress(bytes(10)) + gzip.compress(bytes(7)))
    assert mod._gz_size(p) == 17
    p.write_bytes(gzip.compress(bytes(10)) + b"\0" * 64)          # zero padding after the member
    assert mod._gz_size(p) == 10
    p.write_bytes(gzip.compress(bytes(range(256)) * 5000))       # spans several read blocks
    assert mod._gz_size(p) == 256 * 5000
    p.write_bytes(gzip.compress(bytes(2 ** 16))[:-12])          # truncated
    with pytest.raises(ValueError):
        mod._gz_size(p)
    p.write_bytes(b"raw!")
    assert mod._gz_size(p) == 4
    p.write_bytes(b"")
    assert mod._gz_size(p) == 0


def test_band_order():
    assert sorted(["f480m", "f212n", "f405n", "f182m", "f115w"], key=mod._um) == \
        ["f115w", "f182m", "f212n", "f405n", "f480m"]
    assert mod._um("f150w2") == 1.5
    with pytest.raises(ValueError):
        mod._um("ks")


def test_tree_problems_detects_stale_files(tmp_path):
    n = 10
    d = tmp_path / "data"
    for rel, nbytes in (("dims/f212n.u16.gz", 2 * n), ("errs/e.u8.gz", n), ("meta/dra.i16.gz", 2 * n),
                        ("meta/ddec.i16.gz", 2 * n), ("meta/tile.u8.gz", n - 3)):
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_bytes(gzip.compress(bytes(nbytes)))
    man = {"n": n, "dims": [{"key": "f212n", "file": "dims/f212n.u16.gz"}],
           "errs": [{"key": "e", "file": "errs/e.u8.gz"}],
           "meta": {k: {"file": f"meta/{k}.{t}.gz"} for k, t in (("dra", "i16"), ("ddec", "i16"), ("tile", "u8"))}}
    assert mod.tree_problems(d, man) == [f"meta/tile.u8.gz: {n - 3} rows, manifest n={n}"]
    # a re-deploy that appended a second gzip member: the ISIZE trailer alone would say n rows
    (d / "meta" / "tile.u8.gz").write_bytes(gzip.compress(bytes(n)) + gzip.compress(bytes(n)))
    assert mod.tree_problems(d, man) == [f"meta/tile.u8.gz: {2 * n} rows, manifest n={n}"]
    (d / "meta" / "tile.u8.gz").write_bytes(gzip.compress(bytes(n)))
    (d / "dims" / "f212n.u16.gz").write_bytes(gzip.compress(bytes(2 * n + 1)))
    assert mod.tree_problems(d, man) == [f"dims/f212n.u16.gz: {(2 * n + 1) / 2:g} rows, manifest n={n}"]
    (d / "dims" / "f212n.u16.gz").write_bytes(b"\x1f\x8b garbage")
    assert mod.tree_problems(d, man)[0].startswith("dims/f212n.u16.gz: unreadable")
    (d / "errs" / "e.u8.gz").unlink()
    assert "errs/e.u8.gz: missing" in mod.tree_problems(d, man)


def test_skipped_fields_are_inconsistent(runs):
    """A field whose deployed tree disagrees with its manifest is left out (with the reason)
    instead of being served with mis-placed rows."""
    ad = runs["B"]["adapter"]
    for k, why in ad.skipped.items():
        gc = json.loads((mod.SITE_ROOT / f"{k}_manifold" / "data" / "manifest.json").read_text(encoding="utf-8"))
        assert why == mod.tree_problems(mod.SITE_ROOT / f"{k}_manifold" / "data", gc) and why
    for k in ad._info:
        f = ad._info[k]["f"]
        assert mod.tree_problems(f["data"], f["man"]) == []


def test_readme_counts_and_monitor_link(runs):
    for k in _fields(runs):
        info = runs["B"]["adapter"]._info[k]
        md = runs["B"]["ds"][f"readme_{k}"].markdown
        assert f"{int(info['f']['man']['n']):,} sources in {len(info['bands'])} NIRCam band" in md
        assert mod.MONITOR_URL in md
        assert f"{SF_URL}{k}_manifold/data/" in md
    src = Path(mod.__file__).read_text(encoding="utf-8")
    assert not re.search(r"3[,_]?738[,_]?350|651[,_]?903", src)


def test_exposure_allows_external_unserved():
    from portalkit.deploy.exposure import EXTERNAL, check_target, classify
    exp = classify(SITE.portals[0].target_dir, ExposureMap(roots={}, url_prefixes={}))
    assert check_target("public", exp, EXTERNAL).ok
    assert not check_target("public", exp, None).ok


def test_deploy_dry_run_writes_nothing(built, tmp_path, monkeypatch, capsys):
    """Deploy only as a dry run, to a scratch target; the default target is <legacy_dir>/unified."""
    from portalkit import cli
    from portalkit.deploy import plan_deploy
    from portalkit.deploy.deploy import select_portal
    import tomllib
    site = tomllib.loads((REPO / "sites" / "gc_manifold.toml").read_text(encoding="utf-8"))
    bid = built["res"].staging.name
    pdir = _portals(built)[0]
    pid = pdir.name
    emap = ExposureMap(roots={}, url_prefixes={})
    plan = plan_deploy(FAMILY, bid, site, portal_id=pid, stage_root=built["stage"], emap=emap, yes=False)
    entry = select_portal(site, pid)
    assert plan.target == Path(entry["legacy_dir"]) / "unified"
    assert plan.served_by == "external" and not plan.conflicts
    tgt = tmp_path / "target"
    monkeypatch.setattr(cli, "_emap", lambda: emap)
    rc = cli.main(["deploy", FAMILY, bid, "--portal", pid, "--target", str(tgt), "--dry-run",
                   "--stage-root", str(built["stage"])])
    out = capsys.readouterr().out
    assert rc == 0 and "dry run: nothing written" in out and "served_by = external" in out
    assert not tgt.exists()
