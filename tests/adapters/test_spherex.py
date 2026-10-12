"""Contract tests for the spherex adapter (ARCHITECTURE.md section 9.6, port table 14.9).

Fixture mode (``limit=50``, section 9.6) with a tmp stage root. Tier B memoises ``build_seds`` in the
portalkit cache (``$PK_CACHE_DIR`` or ``~/.cache/portalkit``).
"""

from __future__ import annotations

import json
import math
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
from portalkit.spec import (CatalogSpec, ExtinctionSpec, FilterSetSpec, HipsSpec, ImageSetSpec, OverlaySpec,
                            PageSpec, ReadmeSpec, RecordSetSpec, TemplateSpec, TierUnavailable)

REPO = Path(__file__).resolve().parents[2]
LIMIT = 50
FAMILY = "spherex"
INV = json.loads((REPO / "tests" / "data" / "inventory_caps.json").read_text(encoding="utf-8"))
TMP_ROOTS = tuple(os.path.realpath(p) for p in {tempfile.gettempdir(), "/tmp", "/var/tmp"})
#: inventory caps the port leaves out on purpose (each recorded in the page's caps_disable)
WAIVED = {"sed": {"static_sed_png"}}
PAGE_IDS = ["index", "colours", "browse", "map", "sed", "manifold"]

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from adapters import spherex as mod  # noqa: E402

SITE = load_site(FAMILY)
pytestmark = [pytest.mark.adapters,
              pytest.mark.skipif(not (mod.LEGACY / "colours.html").is_file(), reason="legacy spherex portal not mounted")]


def _ctx(tier: str, staging: Path, limit=LIMIT) -> BuildContext:
    return BuildContext(family=FAMILY, site=SITE, staging=staging, tier=tier, limit=limit, log=lambda m: None)


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """datasets() + pages() for both tiers (timed: contract 9.6(b))."""
    out = {}
    for tier in ("A", "B"):
        ctx = _ctx(tier, tmp_path_factory.mktemp(f"ctx{tier}"))
        adapter = mod.SpherexAdapter()
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


# -- (a) ------------------------------------------------------------------------------------
def test_import_has_no_side_effects(tmp_path):
    code = ("import sys; sys.path.insert(0, %r); import adapters.spherex as m; "
            "assert m.ADAPTER.name == 'spherex'" % str(REPO))
    subprocess.run([sys.executable, "-c", code], cwd=tmp_path, check=True, timeout=120)
    assert list(tmp_path.iterdir()) == []


# -- (b) ------------------------------------------------------------------------------------
@pytest.mark.parametrize("tier", ["A", "B"])
def test_datasets_valid_and_fast(runs, tier):
    r = runs[tier]
    assert r["dt"] < 120, f"datasets() took {r['dt']:.1f} s"
    ds = r["ds"]
    kinds = {"src": CatalogSpec, "seds": CatalogSpec, "spec": RecordSetSpec, "sedspec": RecordSetSpec,
             "sedpks": RecordSetSpec,
             "cut": ImageSetSpec, "sedcut": ImageSetSpec, "ovl": OverlaySpec, "hips": HipsSpec,
             "filters": FilterSetSpec, "tpl": TemplateSpec, "ext": ExtinctionSpec, "readme": ReadmeSpec}
    for k, cls in kinds.items():
        assert isinstance(ds[k], cls), k
    assert len(ds["src"].table) == LIMIT
    assert 0 < len(ds["seds"].table) <= LIMIT
    assert ds["spec"].kind == "pks1" and ds["sedspec"].kind == "sedcurve"
    assert ds["sedpks"].kind == "pks1" and ds["sedpks"].catalog == "seds" and ds["sedspec"].catalog == "seds"
    for p in r["pages"]:
        assert isinstance(p, PageSpec)
        for k in p.datasets:
            assert k in ds, (p.id, k)
    assert [p.id for p in r["pages"]] == PAGE_IDS
    assert [p.id for p in SITE.portal().pages] == PAGE_IDS


def test_tier_c_unavailable(tmp_path):
    with pytest.raises(TierUnavailable):
        mod.SpherexAdapter().datasets(_ctx("C", tmp_path))


def test_pages_before_datasets_raises(tmp_path):
    with pytest.raises(RuntimeError):
        mod.SpherexAdapter().pages(_ctx("A", tmp_path), {})


# -- (c) ------------------------------------------------------------------------------------
@pytest.mark.parametrize("tier", ["A", "B"])
def test_ids_unique(runs, tier):
    ds = runs[tier]["ds"]
    for key in ("src", "seds"):
        ids = [str(x) for x in ds[key].table["id"]]
        assert len(set(ids)) == len(ids), key
        assert all(i and not re.search(r"\s", i) for i in ids)
    src_ids = set(str(i) for i in ds["src"].table["id"])
    assert set(ds["spec"].ids) <= src_ids
    assert set(ds["sedspec"].ids) <= set(str(i) for i in ds["seds"].table["id"])
    assert ds["sedpks"].ids == ds["sedspec"].ids
    for k in ("cut", "sedcut"):
        items = [it.id for it in ds[k].items]
        assert len(set(items)) == len(items)


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
    assert [p["id"] for p in report["pages"]] == PAGE_IDS
    for pg in report["pages"]:
        assert pg["first_paint_bytes"] <= 2_000_000, pg["id"]


def test_one_shared_catalog_bundle(built):
    """The legacy pages embedded the catalog four times; here every source page reads data/src."""
    portal = built["portal"]
    pj = json.loads((portal / "portal.json").read_text(encoding="utf-8"))
    uses = {p["id"]: p for p in pj["pages"]}
    for pid in ("colours", "browse", "map", "manifold"):
        html = (portal / f"{pid}.html").read_text(encoding="utf-8")
        assert len(html) < 200_000, pid                      # no embedded payload
        assert "GDS_J" not in html and "2M0" not in html, pid  # no source ids inline
    assert (portal / "data" / "src" / "manifest.json").is_file()
    assert sorted(p.name for p in (portal / "data").iterdir() if p.name.startswith("src")) == ["src"]
    assert set(uses) == set(PAGE_IDS)


# -- (f) ------------------------------------------------------------------------------------
def test_tier_parity(runs):
    a, b = runs["A"]["ds"], runs["B"]["ds"]
    ta, tb = a["src"].table, b["src"].table
    assert list(ta["id"]) == list(tb["id"])
    assert ta.colnames == tb.colnames
    for c in b["src"].columns:
        if c.role in ("mag", "value", "ra", "dec"):
            x, y = np.asarray(ta[c.key], float), np.asarray(tb[c.key], float)
            assert np.array_equal(np.isfinite(x), np.isfinite(y)), c.key
            ok = np.isfinite(x)
            assert np.allclose(x[ok], y[ok], rtol=0, atol=1e-6), c.key
        else:
            assert list(ta[c.key]) == list(tb[c.key]), c.key
    ca = {c.key: (c.label, c.unit, c.group, c.log, c.note, c.invert, c.levels, c.colors) for c in a["src"].columns}
    cb = {c.key: (c.label, c.unit, c.group, c.log, c.note, c.invert, c.levels, c.colors) for c in b["src"].columns}
    assert ca == cb
    assert [v.key for v in a["src"].virtual] == [v.key for v in b["src"].virtual]
    assert [(p.key, p.data) for p in a["src"].presets] == [(p.key, p.data) for p in b["src"].presets]
    assert a["src"].manifold.dims == b["src"].manifold.dims
    sa, sb = a["seds"].table, b["seds"].table
    assert list(sa["id"]) == list(sb["id"]) and sa.colnames == sb.colnames
    for k in sa.colnames:
        if sa[k].dtype.kind == "f":
            assert np.array_equal(np.asarray(sa[k]), np.asarray(sb[k]), equal_nan=True), k
        else:
            assert list(sa[k]) == list(sb[k]), k
    assert a["hips"].base == b["hips"].base and a["hips"].overlays == b["hips"].overlays
    assert a["hips"].target == b["hips"].target
    assert a["hips"].bookmarks == b["hips"].bookmarks
    assert a["filters"].keys == b["filters"].keys and a["filters"].extra == b["filters"].extra
    assert mod.SpherexAdapter.cards(runs["A"]["adapter"]._raw) == mod.SpherexAdapter.cards(runs["B"]["adapter"]._raw)
    assert a["readme"].markdown == b["readme"].markdown


# -- (g) ------------------------------------------------------------------------------------
def test_caps_superset_of_legacy(built):
    """Inventory caps per legacy page. Only the WAIVED caps may be missing (each recorded in
    caps.disable); density_rendering needs n > T0_MAX."""
    from portalkit.constants import T0_MAX
    pages = {p["id"]: p for p in _report(built)["pages"]}
    checked = 0
    for pc in SITE.portal().pages:
        enabled = set(pages[pc.id]["caps"]["enabled"])
        disabled = set(pages[pc.id]["caps"].get("disabled") or {})
        waived = WAIVED.get(pc.id, set())
        assert waived <= disabled, pc.id
        for leg in pc.legacy:
            missing = set(INV[f"{FAMILY}/{leg}"]) - waived - enabled
            if missing == {"density_rendering"}:
                ns = [d["n"] for d in _report(built)["datasets"].values() if isinstance(d, dict) and "n" in d]
                assert max(ns) <= T0_MAX
                missing = set()
            assert not missing, (pc.id, sorted(missing))
            checked += 1
    assert checked == 6


def test_port_table_caps_filled(built):
    """capabilities_to_fill and flags of port table 14.9; ``dens`` needs n > T0_MAX rows (full
    build only) and ``sedimg`` (an image role "sed") has no source: legacy sed.html draws its SEDs."""
    pages = {p["id"]: p for p in _report(built)["pages"]}
    by_legacy = {leg: pc.id for pc in SITE.portal().pages for leg in pc.legacy}
    rows = [r for r in json.loads((REPO / "tests" / "data" / "port_table.json").read_text(encoding="utf-8"))
            if r["family"] == FAMILY]
    assert len(rows) == 6
    for r in rows:
        pg = pages[by_legacy[r["legacy_page"]]]
        assert set(r["capabilities_to_fill"]) - set(pg["caps"]["enabled"]) <= {"density_rendering"}, r["legacy_page"]
        assert set(r["flags"]) - {"dens", "sedimg"} <= set(pg["flags"]), r["legacy_page"]


# -- legacy-specific behaviour ----------------------------------------------------------------
def test_index_hand_patch_recorded_and_carried(runs):
    """The served index.html carries a manifold tile that portal_pages.INDEX_TEMPLATE lacks."""
    served = mod.parse_tiles((mod.LEGACY / "index.html").read_text(encoding="utf-8"))
    assert mod.MANIFOLD_TILE in served
    assert 'href="spherex_manifold.html">manifold</a>' in (mod.LEGACY / "index.html").read_text(encoding="utf-8")
    pp = runs["B"]["ctx"].legacy.module("portal_pages")
    assert "spherex_manifold.html" not in pp.INDEX_TEMPLATE
    for tier in ("A", "B"):
        landing = runs[tier]["pages"][0]
        cards = landing.layout["options"]["cards"]
        assert {"page": "manifold", "title": MANIFOLD_TITLE, "text": mod.MANIFOLD_TILE["text"]} in cards
        assert [c["page"] for c in cards] == ["colours", "map", "sed", "browse", "manifold"]
        assert all(c["text"] for c in cards)


MANIFOLD_TITLE = "Grand-tour manifold"


def test_parse_tiles_and_origins_edge_cases():
    html = ('<a class="tile" href="x.html"><div class="card"><h2>Caf&eacute; &rarr;</h2>'
            '<p>α  <b>β</b>\n γ</p></div></a>')
    assert mod.parse_tiles(html) == [{"href": "x.html", "title": "Café", "text": "α β γ"}]
    with pytest.raises(ValueError):
        mod.parse_tiles("<html></html>")
    assert mod.parse_origins("const ORIGIN={a:'b &gt;5&times;',c:''};") == {"a": "b >5×", "c": ""}
    with pytest.raises(ValueError):
        mod.parse_origins("var X={};")
    with pytest.raises(ValueError):
        mod.parse_hips("<select></select>")


def test_hips_and_bookmarks(runs):
    for tier in ("A", "B"):
        h = runs[tier]["ds"]["hips"]
        assert [b["url"] for b in h.base] == list(mod.CDS_HIPS.values())
        assert sum(b["default"] for b in h.base) == 1 and h.base[0]["default"]
        assert len(h.overlays) == 1 and h.overlays[0]["opacity"] == 0.5
        assert "PLANCK" in h.overlays[0]["url"] and h.overlays[0]["name"] == "Planck 857 GHz"
        assert h.target == {"ra": 266.4, "dec": -28.94, "fov": 340.0}
        assert len(h.bookmarks) == 60
        assert all(0 < b["fov"] <= 360 for b in h.bookmarks)
        assert max(b["fov"] for b in h.bookmarks) == 360.0          # 480.6 deg region clamped
        assert re.fullmatch(r".+ \(\d+\)", h.bookmarks[0]["name"])


def test_hips_unknown_survey_raises():
    raw = {"hips": {"base": [("x", "CDS/P/Unknown", True)], "overlay": ("o", "https://x/", 0.5),
                    "target": (0.0, 0.0, 10.0)}, "regions": []}
    with pytest.raises(ValueError, match="not a known CDS survey"):
        mod.SpherexAdapter()._hips(raw)


def test_regions_rule():
    assert mod._regions([]) == []
    one = [{"id": "a", "s": "c2d", "ra": 10.0, "dec": 5.0}]
    assert mod._regions(one) == []                      # groups of fewer than 3 are dropped
    # three sources straddling RA 0: the centre wraps, the span uses the raw RA range
    rows = [{"id": str(i), "s": "x", "reg": " Wrap ", "ra": ra, "dec": 0.0} for i, ra in enumerate((359.0, 0.5, 1.0))]
    rows.append({"id": "n", "s": "x", "ra": None, "dec": None})
    (r,) = mod._regions(rows)
    assert r["name"] == "Wrap" and r["n"] == 3
    assert r["ra"] == pytest.approx(0.16667, abs=1e-4) and r["dec"] == pytest.approx(0.0, abs=1e-9)
    assert r["fov"] == pytest.approx(round((359.0 - 0.5) * 1.6, 3))
    tight = [{"id": str(i), "s": "x", "ra": 10.0, "dec": 1.0} for i in range(4)]
    assert mod._regions(tight)[0]["fov"] == 0.5          # floor
    big = mod._regions(rows + tight)
    assert [g["n"] for g in big] == [4, 3]               # sorted by -n


def test_regions_tier_b_matches_served_map(runs):
    """Tier B recomputes render_map's regions from the catalog; they match map.html's."""
    a, b = runs["A"]["adapter"]._raw["regions"], runs["B"]["adapter"]._raw["regions"]
    assert a == b


def test_sed_notes():
    assert mod.sed_notes({}) == ""
    d = {"fq": "confused+cirrus", "cf60": 0.478, "c2": 6.0, "c3": 12.5, "hprog": "KPOT_x", "aprog": "2019.1.ä",
         "p": [[100.0, 50.0, None, "Herschel", "Herschel_100 (extended)"], [160, 10.0, None, "Herschel", "Herschel_160"],
               [100.0, 100.0, None, "IRAS PSC", "IRAS_100"]]}
    s = mod.sed_notes(d)
    assert "Neighbours hold 48% of the 22 µm flux within 60″." in s
    assert "IRAS cirr2 = 6 on a 1–9 scale, with the 100 µm sky at 12.5 MJy/sr" in s
    assert "flags 100 µm as extended" in s
    assert "Herschel 100 µm is 2.0× below IRAS 100 µm." in s
    assert s.endswith("Herschel programs: KPOT_x. ALMA projects: 2019.1.ä.")
    assert mod.sed_notes({"fq": "confused", "cf60": None}).startswith("Neighbours hold — of")
    assert mod.sed_notes({"c2": 5}) == ""
    assert mod._jsn(6.0) == "6" and mod._jsn(6.5) == "6.5" and mod._jsn(True) == "true" and mod._jsn(None) == "null"
    # JavaScript toFixed rounds the exact binary value half away from zero; Python's format
    # rounds half to even (real stars: OGLE_J054647.84-703813.9 0.485, IRAS_00450+7401 0.345)
    for cf60, want in ((0.485, "49%"), (0.345, "35%"), (0.825, "83%"), (0.478, "48%"), (0.0, "0%"), (1, "100%")):
        assert mod._pct(cf60) == want, cf60
    assert mod.sed_notes({"fq": "confused", "cf60": 0.485}).startswith("Neighbours hold 49% of")
    assert (mod._tofixed(2.05, 1), mod._tofixed(0.25, 1), mod._tofixed(-0.4, 0), mod._tofixed(-0.0, 0)) == \
        ("2.0", "0.3", "-0", "0")                         # 2.05 is 2.04999... in binary
    assert mod._tofixed(1e21, 1) == "1e+21" and mod._tofixed(float("nan"), 0) == "NaN"
    h = {"p": [[100, 2.0, None, "Herschel", "Herschel_100"], [100, 4.5, None, "IRAS", "IRAS_100"]]}
    assert "is 2.3× below" in mod.sed_notes(h)          # 2.25 -> toFixed(1) '2.3' (format gives 2.2)


def test_sed_notes_match_legacy_js(runs):
    """sed_notes() against the legacy notes JavaScript run under node for every SED star."""
    import shutil
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")
    src = (mod.SRC_REPO / "portal_pages.py").read_text(encoding="utf-8").splitlines()
    pct = next(ln for ln in src if "const pct=" in ln).strip()
    i0 = next(i for i, ln in enumerate(src) if ln.strip() == "const notes=[];")
    i1 = next(i for i in range(i0, len(src)) if "document.getElementById('notes')" in src[i])
    body = "\n".join(src[i0:i1])
    seds = runs["A"]["adapter"]._raw["seds"]
    js = (f"{pct}\nconst D = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n"
          f"const ent = (s) => s.replace(/&Prime;/g, '″').replace(/&prime;/g, '′').replace(/&times;/g, '×')"
          f".replace(/&ndash;/g, '–');\n"
          f"console.log(JSON.stringify(D.map((d) => {{ {body}\n return ent(notes.join(' ')); }})));")
    out = subprocess.run([node, "-e", js], input=json.dumps(seds), capture_output=True, text=True, check=True,
                         timeout=120)
    legacy = json.loads(out.stdout)
    bad = [(d["id"], a, mod.sed_notes(d)) for d, a in zip(seds, legacy) if a != mod.sed_notes(d)]
    assert not bad, bad[:3]


def test_band_key_and_filters(runs):
    assert mod.band_key("Herschel_250 (extended)") == "herschel_250"
    assert mod.band_key("IRAC_3.6") == "irac_3_6"
    f = runs["B"]["ds"]["filters"]
    assert f.keys[:28] == [f"JWST/NIRCam.{s}" for s in runs["B"]["adapter"]._raw["filters"]]
    for k, e in f.extra.items():
        assert e["wl_um"] > 0 and e["zp_jy"] > 0 and math.isfinite(e["zp_jy"]), k
    assert f.extra.get("WISE/WISE.W1", {}).get("zp_jy") == 309.540
    seds = runs["B"]["ds"]["seds"]
    known = set(f.keys) | set(f.extra)
    for c in seds.columns:
        if c.role == "flux":
            assert c.filter in known and c.err in seds.table.colnames and c.unit == "uJy", c.key


def test_seds_columns(runs):
    seds = runs["A"]["ds"]["seds"]
    t = seds.table
    raw = {str(d["id"]): d for d in runs["A"]["adapter"]._raw["seds"]}
    for row in t:
        d = raw[str(row["id"])]
        assert row["notes"] == mod.sed_notes(d)
        assert row["iras"] == (d["iras"] if d.get("iras") and d["iras"] != "--" else "none within 40″")
        assert int(row["nphot"]) == len(d["p"])
        for wl, f, e, origin, band in d["p"]:
            k = mod.band_key(band)
            if np.isfinite(row[f"p_{k}"]) and row[f"p_{k}"] == f:
                assert (np.isnan(row[f"e_{k}"]) if e is None else row[f"e_{k}"] == e)
    assert {"chip_alma", "chip_herschel", "chip_fir60", "chip_uncrowded"} & {p.key for p in seds.presets}


def test_spec_loader(tmp_path):
    (tmp_path / "3.json").write_text(json.dumps(
        {"id": "Ω-1", "s": "c2d", "w": [1.2, 0.8, None, 1.0, 0.0], "f": [30.0, 10.0, 5.0, None, 1.0],
         "e": [3.0, 1.0, 1.0, 1.0, 1.0], "iw": [7.0, 6.0], "if": [700.0, 600.0], "ie": [None, 60.0]}), encoding="utf-8")
    (tmp_path / "4.json").write_text(json.dumps({"id": "other", "w": [1.0], "f": [1.0], "e": [1.0]}), encoding="utf-8")
    (tmp_path / "5.json").write_text(json.dumps({"id": "empty", "w": [], "f": [], "e": []}), encoding="utf-8")
    ld = mod.SpecLoader(tmp_path, {"Ω-1": 3, "bad": 4, "gone": 9, "empty": 5})
    sp = ld("Ω-1")
    assert sp.segments == [(0, 3, "SPHEREx"), (3, 5, "IRS")]
    assert np.allclose(sp.wave, [0.8, 1.0, 1.2, 6.0, 7.0])           # sorted per segment, bad w dropped
    assert np.allclose(sp.channels["flux"], [10e-6, np.nan, 30e-6, 600e-6, 700e-6], equal_nan=True)
    assert np.isnan(sp.channels["err"][4]) and sp.units["flux"] == "Jy" and sp.meta["irs"]
    with pytest.raises(ValueError, match="different legacy builds"):
        ld("bad")
    assert ld("gone") is None and ld("unknown") is None and ld("empty") is None
    sc = mod.SedCurveLoader(ld)("Ω-1")
    assert list(sc.channels) == ["flux", "IRS"]
    assert np.isnan(sc.channels["flux"][3:]).all() and np.isnan(sc.channels["IRS"][:3]).all()
    assert mod.SedCurveLoader(ld)("gone") is None


def test_real_spectra_records(runs):
    ds = runs["B"]["ds"]
    t = ds["src"].table
    irs = {str(i) for i, v in zip(t["id"], t["irs"]) if v}
    assert irs, "fixture keeps sources with an IRS spectrum"
    for sid in ds["spec"].ids[:6] + sorted(irs)[:2]:
        sp = ds["spec"].loader(sid)
        for i0, i1, _ in sp.segments:
            assert np.all(np.diff(sp.wave[i0:i1]) >= 0)
        assert [s[2] for s in sp.segments] == (["SPHEREx", "IRS"] if sid in irs else ["SPHEREx"])
        f = sp.channels["flux"]
        assert np.nanmax(np.abs(f)) < 1e4                    # Jy, not µJy


def test_fixture_selection_edge_cases(runs):
    raw = runs["A"]["adapter"]._raw
    src, seds = raw["sources"], raw["seds"]
    assert mod.SpherexAdapter.select(src, seds, 0) == []
    one = mod.SpherexAdapter.select(src, seds, 1)
    assert len(one) == 1 and str(src[one[0]]["id"]) in {str(s["id"]) for s in seds}
    sel = mod.SpherexAdapter.select(src, seds, LIMIT)
    assert len(sel) == LIMIT and sel == sorted(set(sel))
    assert len({src[i]["s"] for i in sel}) == len({r["s"] for r in src})   # every sample represented
    assert mod.SpherexAdapter.select(src, seds, None) == list(range(len(src)))
    assert mod.SpherexAdapter.select([], [], 5) == []
    assert mod.SpherexAdapter.select_seds(seds, set(), 0) == []
    ids = {str(src[i]["id"]) for i in sel}
    ss = mod.SpherexAdapter.select_seds(seds, ids, LIMIT)
    assert {str(seds[j]["id"]) for j in ss} >= ids & {str(s["id"]) for s in seds}


def test_src_catalog(runs):
    src = runs["B"]["ds"]["src"]
    cols = {c.key: c for c in src.columns}
    mags = [c for c in src.columns if c.role == "mag"]
    assert len(mags) == 28 and all(c.filter.startswith("JWST/NIRCam.") for c in mags)
    assert cols["s"].colors and cols["q"].colors and cols["cls"].role == "category"
    assert cols["irs"].role == "flag" and cols["sat"].role == "flag"
    assert cols["nch"].log and cols["dist"].log
    assert "f162m-f210m" in {v.key for v in src.virtual}
    assert {"tb_f444w", "tb_f480m"} <= set(cols)
    assert set(src.table["blended"]) <= {"blended", "not blended", "not assessed"}
    pre = {p.key: p for p in src.presets}
    assert pre["default"].data["x"] == "f162m-f210m" and pre["default"].data["y"] == "f360m-f480m"
    assert pre["sky"].data["x"] == "ra_deg"
    assert src.manifold.default_frame == "default"
    assert set(src.manifold.dims) >= {"f162m", "f162m-f210m", "teff", "ra_deg", "dec_deg"}
    for d in src.manifold.dims:
        assert d in cols or d in {v.key for v in src.virtual}


def test_overlays(runs):
    lay = runs["A"]["ds"]["ovl"].layers
    ids = [ly["id"] for ly in lay]
    assert ids == ["yoo_upper", "yoo_lower", "yoo_red", "yoo_red_limits", "yoo_divider"]
    for ly in lay:
        assert ly["space"] == "dim" and ly["default"] is False
        for it in ly["items"]:
            assert (it["x"], it["y"]) == ("f162m-f210m", "f360m-f480m")
            assert all(math.isfinite(v) for p in it["pts"] for v in p)
    assert len(lay[0]["items"][0]["pts"]) == 484 and len(lay[1]["items"][0]["pts"]) == 1362
    assert "final_catalog.fits" not in json.dumps(lay)
    (x0, y0), (x1, y1) = lay[4]["items"][0]["pts"]
    assert (y1 - y0) / (x1 - x0) == pytest.approx(0.25)
    # the YooW51red lower limits (legacy: right-pointing arrows) are all drawn
    red = json.loads((mod.LEGACY / mod.OVERLAY_JSON).read_text(encoding="utf-8"))["groups"]["YooW51red"]
    lim = lay[3]
    assert lim["kind"] == "polyline" and lim["name"].endswith(f"({len(red['xlim'])})") and len(red["xlim"]) == 26
    assert "drawn as right arrows" in lim["note"] and "lower limits" in lay[2]["note"]
    assert [it["pts"][0] for it in lim["items"]] == [[float(a), float(b)] for a, b in zip(red["xlim"], red["ylim"])]
    for it in lim["items"]:
        (bx, by), tip, b1, tip2, b2 = it["pts"]
        assert tip == tip2 and tip[1] == by and tip[0] > bx              # points toward +x
        assert b1[0] < tip[0] and b2[0] < tip[0] and b1[1] > by > b2[1]   # arrow head
    assert mod.limit_arrow(1.0, 2.0, sign=-1)[1][0] < 1.0


def test_datasets_limit_zero(tmp_path):
    """An empty fixture (limit 0) builds empty datasets and pages without error."""
    a = mod.SpherexAdapter()
    ds = a.datasets(_ctx("A", tmp_path, limit=0))
    assert len(ds["src"].table) == 0 and len(ds["seds"].table) == 0
    assert ds["spec"].ids == [] and ds["sedpks"].ids == []
    assert "0.75–5 µm" in ds["readme"].markdown
    assert [p.id for p in a.pages(_ctx("A", tmp_path, limit=0), ds)] == PAGE_IDS


def test_sed_page_layout(runs):
    """The sed page's spectrum view is spectrum1d in SED mode over the seds-keyed spectra (zoom,
    pan and the 0.7-40 um button apply to the photometry too); no per-id links to the src pages."""
    for tier in ("A", "B"):
        ds = runs[tier]["ds"]
        sed = next(p for p in runs[tier]["pages"] if p.id == "sed")
        assert not sed.links
        assert "spec" not in sed.datasets and "sedpks" in sed.datasets

        def leaves(n):
            if "pane" in n:
                yield n
            for k in ("row", "col"):
                for c in n.get(k, []):
                    yield from leaves(c)
        sp = next(n for n in leaves(sed.layout) if n["pane"] == "spectrum1d")
        assert sp["options"]["mode"] == "sed" and sp["options"]["records"] == "sedpks"
        assert sp["options"]["bands"][0] == mod.SED_BANDS[0]
        # every SED star with a spectrum in the fixture has its spectrum on the sed page
        with_spec = {str(r["id"]) for r, f in zip(ds["seds"].table, ds["seds"].table["spherex"]) if f}
        assert with_spec and set(ds["sedpks"].ids) == with_spec


def test_readme(runs):
    for tier in ("A", "B"):
        md = runs[tier]["ds"]["readme"].markdown
        assert not re.search(r"/(orange|blue|home)/", md)
        assert "python -m portalkit deploy spherex" in md and "deploy_portal.sh" in md
        assert "Grand-tour manifold" in md
        for h in ("### R Coronae Borealis stars", "### Spitzer IRS spectra", "### Reading the diagrams"):
            assert h in md
        blk = runs[tier]["ds"]["readme"].blocks[0]
        assert sum(r[1] for r in blk["rows"]) == LIMIT
    assert mod.scrub_paths("see /orange/a/b.py and `/home/x`") == "see (legacy tree) and `(legacy tree)`"
    assert mod.legacy_sections("# T\n## Other\nx\n## Template overlays\nα /blue/q\n### Sub\ny") == \
        "### Template overlays\nα (legacy tree)\n#### Sub\ny"
    assert mod.legacy_sections("") == ""


def test_ice_bands_in_atlas(runs):
    atlas = json.loads((REPO / "portalkit" / "data" / "features" / "ice.json").read_text(encoding="utf-8"))
    ws = [float(f["w_um"]) for f in atlas["features"]]
    for w, _ in runs["A"]["adapter"]._raw["ice"]:
        assert any(abs(w - x) < 0.02 for x in ws), w


def test_cutout_urls(runs):
    cut = runs["A"]["ds"]["cut"]
    t = runs["A"]["ds"]["src"].table
    assert len(cut.items) == int(np.isfinite(np.asarray(t["ra"], float)).sum())
    u = cut.items[0].roles["allwise"]
    assert u.startswith(mod.HIPS2FITS + "?") and "format=jpg" in u and "CDS%2FP%2FallWISE%2Fcolor" in u
    assert mod.cutout_url("CDS/P/DSS2/color", 1.23456789, -5.0, 0.1).count("ra=1.234568") == 1
