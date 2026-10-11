"""Contract tests for the sgrb2 adapter (ARCHITECTURE.md section 9.6, port table 14.7).

Fixture mode (``limit=50``) with a tmp stage root. The catalogue read is memoised in the
portalkit cache (``$PK_CACHE_DIR`` or ``~/.cache/portalkit``), so only the first run after a
catalogue change reads the 1.4 GB FITS file.
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
FAMILY = "sgrb2"
INV = json.loads((REPO / "tests" / "data" / "inventory_caps.json").read_text(encoding="utf-8"))
TMP_ROOTS = tuple(os.path.realpath(p) for p in {tempfile.gettempdir(), "/tmp", "/var/tmp"})

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from adapters import sgrb2 as mod  # noqa: E402

SITE = load_site(FAMILY)
pytestmark = [pytest.mark.adapters,
              pytest.mark.skipif(not mod.MAPCCD_DIR.is_dir(), reason="legacy sgrb2 portal tree not mounted")]


def _ctx(tier: str, staging: Path) -> BuildContext:
    return BuildContext(family=FAMILY, site=SITE, staging=staging, tier=tier, limit=LIMIT,
                        log=lambda m: None)


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """datasets() + pages() for both tiers (timed: contract 9.6(b))."""
    out = {}
    for tier in ("A", "B"):
        ctx = _ctx(tier, tmp_path_factory.mktemp(f"ctx{tier}"))
        adapter = mod.SgrB2Adapter()
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
    return {"res": res, "portal": res.staging / "sgrb2"}


# -- (a) ------------------------------------------------------------------------------------
def test_import_has_no_side_effects(tmp_path):
    code = ("import sys; sys.path.insert(0, %r); import adapters.sgrb2 as m; "
            "assert m.ADAPTER.name == 'sgrb2'" % str(REPO))
    subprocess.run([sys.executable, "-c", code], cwd=tmp_path, check=True, timeout=120)
    assert list(tmp_path.iterdir()) == []


# -- (b) ------------------------------------------------------------------------------------
@pytest.mark.parametrize("tier", ["A", "B"])
def test_datasets_valid_and_fast(runs, tier):
    r = runs[tier]
    assert r["dt"] < 120, f"datasets() took {r['dt']:.1f} s"
    ds = r["ds"]
    assert isinstance(ds["src"], CatalogSpec) and isinstance(ds["seds"], CatalogSpec)
    assert isinstance(ds["img"], ImageSetSpec) and isinstance(ds["iso"], IsochroneSpec)
    assert isinstance(ds["hips"], HipsSpec) and isinstance(ds["hips_ccd"], HipsSpec)
    assert isinstance(ds["readme"], ReadmeSpec)
    assert LIMIT <= len(ds["src"].table) <= 2 * LIMIT
    assert 0 < len(ds["seds"].table) <= LIMIT
    for p in r["pages"]:
        assert isinstance(p, PageSpec)
        for k in p.datasets:
            assert k in ds, (p.id, k)
    assert [p.id for p in r["pages"]] == ["index", "sgrb2_map", "sgrb2_map_ccd", "sgrb2_seds"]


def test_tier_c_unavailable(tmp_path):
    ctx = BuildContext(family=FAMILY, site=SITE, staging=tmp_path, tier="C", limit=LIMIT, log=lambda m: None)
    with pytest.raises(TierUnavailable):
        mod.SgrB2Adapter().datasets(ctx)


# -- (c) ------------------------------------------------------------------------------------
@pytest.mark.parametrize("tier", ["A", "B"])
def test_ids_and_aliases_unique(runs, tier):
    ds = runs[tier]["ds"]
    for key in ("src", "seds"):
        ids = [str(x) for x in ds[key].table[ds[key].id]]
        assert len(set(ids)) == len(ids), key
        assert all(re.fullmatch(r"sgrb2_\d{6}", i) for i in ids)
    seds = ds["seds"]
    al = [str(a) for a in seds.table[seds.aliases]]
    assert len(set(al)) == len(al)
    assert not set(al) & set(str(i) for i in seds.table["id"])
    # fixture builds keep the SED rows in src, so gallery -> map_ccd links resolve
    assert set(str(i) for i in seds.table["id"]) <= set(str(i) for i in ds["src"].table["id"])
    items = [it.id for it in ds["img"].items]
    assert len(set(items)) == len(items)
    assert set(items) <= set(str(i) for i in seds.table["id"])


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
    report = json.loads((built["portal"] / "build_report.json").read_text(encoding="utf-8"))
    assert report["tier"] == "B"
    for pg in report["pages"]:
        assert pg["first_paint_bytes"] <= 2_000_000, pg["id"]


# -- (f) ------------------------------------------------------------------------------------
def test_tier_parity(runs):
    a, b = runs["A"]["ds"], runs["B"]["ds"]
    ta, tb = a["src"].table, b["src"].table
    assert len(ta) == len(tb)
    assert list(ta["id"]) == list(tb["id"])
    for c in b["src"].columns:
        if c.role != "mag":
            continue
        x, y = np.asarray(ta[c.key], float), np.asarray(tb[c.key], float)
        assert np.array_equal(np.isfinite(x), np.isfinite(y)), c.key
        ok = np.isfinite(x)
        assert np.all(np.abs(x[ok] - y[ok]) <= 0.001), c.key
    assert list(ta["cov"]) == list(tb["cov"])
    # legacy positions are float32 (spacing 1.5e-5 deg at RA 266)
    for k in ("ra", "dec"):
        assert np.max(np.abs(np.asarray(ta[k]) - np.asarray(tb[k]))) < 2e-5
    # Tier B's uncapped selection starts with the legacy 500 (both reddest first)
    assert list(a["seds"].table["id"]) == list(b["seds"].table["id"])[:len(a["seds"].table)]
    assert np.allclose(a["seds"].table[mod.SED_COLOR], b["seds"].table[mod.SED_COLOR][:len(a["seds"].table)],
                       atol=1e-6)
    assert a["hips"].base == b["hips"].base and a["hips"].overlays == b["hips"].overlays
    assert a["hips_ccd"].base == b["hips_ccd"].base


# -- (g) ------------------------------------------------------------------------------------
def test_caps_superset_of_legacy(built):
    report = json.loads((built["portal"] / "build_report.json").read_text(encoding="utf-8"))
    portal = SITE.portal()
    checked = 0
    for pc in portal.pages:
        enabled = set({p["id"]: p for p in report["pages"]}[pc.id]["caps"]["enabled"])
        for leg in pc.legacy:
            need = set(INV[f"{FAMILY}/{leg}"])
            assert need <= enabled, (pc.id, sorted(need - enabled))
            checked += 1
    assert checked == 4


def test_port_table_caps_filled(built):
    """capabilities_to_fill and flags of port table 14.7; ``dens`` / density_rendering need
    n > T0_MAX rows, which only the full build has."""
    report = json.loads((built["portal"] / "build_report.json").read_text(encoding="utf-8"))
    pages = {p["id"]: p for p in report["pages"]}
    by_legacy = {leg: pc.id for pc in SITE.portal().pages for leg in pc.legacy}
    rows = [r for r in json.loads((REPO / "tests" / "data" / "port_table.json").read_text(encoding="utf-8"))
            if r["family"] == FAMILY]
    assert len(rows) == 4
    for r in rows:
        pg = pages[by_legacy[r["legacy_page"]]]
        assert set(r["capabilities_to_fill"]) - set(pg["caps"]["enabled"]) <= {"density_rendering"}, r["legacy_page"]
        assert set(r["flags"]) - {"dens"} <= set(pg["flags"]), r["legacy_page"]


def test_hips_layers_are_urls(runs):
    for tier in ("A", "B"):
        ds = runs[tier]["ds"]
        for spec in (ds["hips"], ds["hips_ccd"]):
            assert all(layer["url"].startswith("https://") for layer in spec.base + spec.overlays)
            assert sum(layer["default"] for layer in spec.base) == 1


# -- special handling (section 14.7) ---------------------------------------------------------
def test_encodings_per_tier(runs):
    a, b = runs["A"]["ds"]["src"], runs["B"]["ds"]["src"]
    assert a.order == "input" and all(c.enc == "f32" for c in a.columns if c.role == "mag")
    assert b.order == "auto" and all(c.enc == "auto" and c.step == 0.001 for c in b.columns if c.role == "mag")
    for spec in (a, b):
        assert {d.key for d in spec.density} == {"cmd", "ccd"}
        for d in spec.density:
            assert d.xr[0] < d.xr[1] and d.yr[0] < d.yr[1]


def test_axis_limits_are_percentiles(runs):
    ds = runs["B"]["ds"]
    t = ds["src"].table
    page = {p.id: p for p in runs["B"]["pages"]}["sgrb2_map_ccd"]
    for pane, (x, y) in (("cmd", mod.CMD), ("ccd", mod.CCD)):
        st = page.state[f"axes:{pane}"]
        assert (st["x"], st["y"]) == (x, y)
        for e, r in ((x, st["xr"]), (y, st["yr"])):
            v = mod._expr(t, e)
            lo, hi = np.percentile(v[np.isfinite(v)], mod.PCT)
            assert r == pytest.approx([lo, hi], abs=1e-3)


def test_sed_upper_limits_and_png_split(runs):
    ds = runs["B"]["ds"]
    t = ds["seds"].table
    codes = np.concatenate([np.asarray(t[c.limit]) for c in ds["seds"].columns if c.limit])
    assert (codes == "u").any(), "fixture rows should include S/N < 3 bands"
    for c in ds["seds"].columns:
        if c.role != "flux":
            continue
        f, lim = np.asarray(t[c.key], float), np.asarray(t[c.limit])
        assert np.all(f[np.isfinite(f)] > 0), c.key       # flux <= 0 became a 3-sigma limit
        assert np.all(np.isnan(np.asarray(t[c.err], float)[lim == "u"])), c.key
    # legacy make_sed_png keeps every band with flux > 0: detection or upper limit, never dropped
    raw = runs["B"]["adapter"]._raw["sed"]
    for b in runs["B"]["adapter"]._raw["bands"]:
        f = np.asarray(raw[f"fx_{b}"], float)
        pos = np.isfinite(f) & (f > 0)
        assert np.all(np.isfinite(np.asarray(t[f"fx_{b}"], float))[pos]), b
    pngs = {it.id for it in ds["img"].items}
    for rid, cls in zip(t["id"], t["sed_png"]):
        assert (cls == "static PNG + live") == (str(rid) in pngs)
    for it in ds["img"].items:
        assert it.roles["sed"].startswith(mod.PNG_URL)


def test_no_hard_coded_counts(runs):
    src = Path(mod.__file__).read_text(encoding="utf-8")
    assert not re.search(r"464[,_]?489|2[,_]?105\b|\b11 NIRCam|> 5 mag|S/N >= 10", src)
    md = runs["B"]["ds"]["readme"].markdown
    assert f"{len(runs['B']['adapter']._raw['bands'])} NIRCam filters" in md
    assert f"{len(runs['B']['ds']['src'].table):,} catalogue sources" in md
    man = json.loads((mod.MAPCCD_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert f"{man['n']:,}" in md


def test_manifold_card_is_login_link(built):
    from portalkit.deploy.deploy import collect_references
    from portalkit.deploy.exposure import Reference, check_references
    from portalkit.legacy.extract import script_json
    page = script_json(built["portal"] / "index.html", "pk-page")
    leaf = page["layout"]
    cards = leaf["options"]["cards"]
    sec = [c for c in cards if c.get("auth") == "secure"]
    assert len(sec) == 1 and sec[0]["href"].startswith("https://data.rc.ufl.edu/secure/")
    files = [str(p.relative_to(built["portal"])) for p in built["portal"].rglob("*")
             if p.is_file() and p.suffix in (".html", ".json")]
    refs = [Reference(url=u, source=s, fetched=f) for s, u, f in collect_references(built["portal"], files)]
    secure = [r for r in refs if r.url.startswith("https://data.rc.ufl.edu/secure/")]
    assert secure and not any(r.fetched for r in secure)
    dec = check_references("public", refs)
    assert dec.level == "warn" and dec.exit_code == 0, dec


def test_missing_error_becomes_flux_limit():
    """f > 0 with err NaN -> limit at f; f <= 0 with err -> 3 sigma; detections kept."""
    class C:
        tier = "B"
    sed = {"row": np.array([1, 2, 3, 4]), "ra": np.zeros(4), "dec": np.zeros(4),
           mod.SED_COLOR: np.full(4, 6.0), "f150w": np.full(4, 20.0),
           "fx_f150w": np.array([2e-3, 1e-3, -1e-4, np.nan]),
           "efx_f150w": np.array([1e-4, np.nan, 1e-4, np.nan])}
    raw = {"sed": sed, "bands": ["f150w"], "pngs": {}, "sel": dict(mod.SEL_DEFAULT)}
    t = mod.SgrB2Adapter()._seds(C(), raw).table
    assert list(t["l_f150w"]) == ["", "u", "u", ""]
    assert np.allclose(np.asarray(t["fx_f150w"], float)[:3], [2e-3, 1e-3, 3e-4])
    assert np.isnan(t["fx_f150w"][3])
    assert np.isfinite(t["efx_f150w"][0]) and np.all(np.isnan(np.asarray(t["efx_f150w"], float)[1:]))


def test_cds_ids_mapped_for_overlays():
    raw = {"center": [266.8, -28.4], "fov": 0.2}
    h = mod.SgrB2Adapter()._hips([("2MASS", "P/2MASS/color", 1)], [("DSS", "P/DSS2/color", 0)], raw)
    assert all(layer["url"].startswith("https://") for layer in h.base + h.overlays)


def test_selection_constants_in_cache_key(tmp_path, monkeypatch):
    """Editing the legacy selection must not return a stale memoised selection."""
    class Stop(Exception):
        pass
    keys = []

    def fake_memo(key, fn, inputs=()):
        keys.append(key)
        raise Stop
    ctx = _ctx("B", tmp_path)
    monkeypatch.setattr(ctx.cache, "memo", fake_memo)
    sm = ctx.legacy.module("SgrB2_Portal_SEDs")
    for cmin in (sm.COLOR_MIN, sm.COLOR_MIN + 7.0):
        monkeypatch.setattr(sm, "COLOR_MIN", cmin)
        with pytest.raises(Stop):
            mod.SgrB2Adapter()._tier_b(ctx)
    assert len(keys) == 2 and keys[0] != keys[1]
