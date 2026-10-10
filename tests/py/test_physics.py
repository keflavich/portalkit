"""WP02 physics resources: filters, extinction, templates, isochrones, features, SED.

Level 0 (plain pytest). Tests that need the legacy sources, the SVO cache, the template
JSON, the MIST files or dust_extinction skip when those are absent.
"""

from __future__ import annotations

import ast
import gzip
import importlib.util
import json
import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from portalkit import physics, schema
from portalkit.bundle.records import decode_pks1, split_record
from portalkit.bundle.writer import BundleWriter, Ledger, dumps_json, h8
from portalkit.physics import extinction as E
from portalkit.physics import features as FT
from portalkit.physics import filters as F
from portalkit.physics import isochrones as I
from portalkit.physics import sed as S
from portalkit.physics import templates as T

try:
    from portalkit.spec import ExtinctionSpec, FilterSetSpec, IsochroneSpec, TemplateSpec, Track
except ImportError:  # spec.py (WP03) absent: duck-typed stand-ins
    def TemplateSpec(source=None, extend_to_um=None):  # noqa: N802
        return SimpleNamespace(source=source, extend_to_um=extend_to_um)

    def FilterSetSpec(keys, extra=None):  # noqa: N802
        return SimpleNamespace(keys=keys, extra=extra or {})

    def ExtinctionSpec(law="G23", rv=(3.1, 5.5)):  # noqa: N802
        return SimpleNamespace(law=law, rv=rv)

    Track = I.Track
    IsochroneSpec = I.IsochroneSpec

REPO = Path(__file__).resolve().parents[2]
GOLDEN = REPO / "tests" / "golden" / "physics"
ANALYSIS = Path("/orange/adamginsburg/jwst/brick/analysis")
W51_PORTAL = Path("/orange/adamginsburg/w51/portal")

HAVE_DUST = E.dust_extinction_available()
HAVE_TEMPLATES = T.DEFAULT_SOURCE.is_file()
needs_dust = pytest.mark.skipif(not HAVE_DUST, reason="dust_extinction not installed")
needs_templates = pytest.mark.skipif(not HAVE_TEMPLATES, reason="template JSON not available")


def _legacy_namespace(path: Path, names: set[str]) -> dict:
    """Execute only the named top-level functions/assignments of a legacy script."""
    tree = ast.parse(path.read_text())
    body = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            body.append(node)
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in names for t in node.targets):
            body.append(node)
    mod = ast.Module(body=body, type_ignores=[])
    ns: dict = {"np": np, "json": json}
    from astropy.table import Table
    ns["Table"] = Table
    exec(compile(mod, str(path), "exec"), ns)  # noqa: S102 - trusted local legacy source
    return ns


# ---------------------------------------------------------------------------
# sed.py
# ---------------------------------------------------------------------------

def test_mag_jy_roundtrip_and_edges():
    assert S.mag_to_jy(0.0, 3631.0) == pytest.approx(3631.0)
    assert S.mag_to_jy(2.5, 100.0) == pytest.approx(10.0)
    assert S.jy_to_mag(10.0, 100.0) == pytest.approx(2.5)
    mags = np.array([-5.0, 0.0, 12.3, 25.0, 40.0])
    back = S.jy_to_mag(S.mag_to_jy(mags, 208.75), 208.75)
    assert np.allclose(back, mags, rtol=0, atol=1e-10)
    for bad in (0.0, -1.0, np.nan, np.inf * -1):
        assert math.isnan(S.jy_to_mag(bad, 100.0))
    assert math.isnan(S.mag_to_jy(np.nan, 100.0))
    assert math.isnan(S.mag_to_jy(10.0, 0.0))
    assert math.isnan(S.mag_to_jy(10.0, np.nan))
    assert isinstance(S.mag_to_jy(1.0, 2.0), float)
    assert S.mag_to_jy(np.array([]), 1.0).shape == (0,)
    assert S.mag_to_jy(-400.0, 1.0) == math.inf or math.isnan(S.mag_to_jy(-400.0, 1.0)) or \
        S.mag_to_jy(-400.0, 1.0) > 1e150
    assert S.mag_err_to_jy(1.0, 0.1) == pytest.approx(math.log(10) / 2.5 * 0.1)
    assert math.isnan(S.mag_err_to_jy(1.0, -0.1))


def test_flux_units_and_nufnu():
    assert S.flux_unit_factor("mJy") == 1e-3
    assert S.flux_unit_factor("µJy") == S.flux_unit_factor("uJy") == 1e-6
    assert S.flux_unit_factor(None) == 1.0
    with pytest.raises(ValueError):
        S.flux_unit_factor("furlongs")
    # 1 Jy at 1 um: 1e-26 * c / lambda
    assert S.nufnu(1.0, 1.0) == pytest.approx(1e-26 * 2.99792458e14)
    assert S.nufnu(1.0, 1.0, "erg/s/cm2") == pytest.approx(1e-26 * 2.99792458e14 * 1e3)
    assert math.isnan(S.nufnu(0.0, 1.0))
    with pytest.raises(ValueError):
        S.nufnu(1.0, 1.0, "lumens")


def test_blackbody_matches_template_planck():
    w = np.array([0.5, 1.0, 5.0, 26.0])
    assert np.allclose(S.blackbody_fnu(w, 5000.0), T.planck_nu(5000.0, w), rtol=1e-12)
    # Rayleigh-Jeans: B_nu ~ lambda^-2
    r = S.blackbody_fnu(200.0, 3000.0) / S.blackbody_fnu(400.0, 3000.0)
    assert r == pytest.approx(4.0, rel=2e-2)
    assert math.isnan(S.blackbody_fnu(1.0, 0.0))


def test_sed_points_basic_limits_units():
    filters = {"A/X.F1": {"wl_um": 2.0, "zp_jy": 100.0}, "A/X.F2": {"wl_um": 1.0, "zp_jy": 200.0},
               "A/X.F3": {"wl_um": 3.0, "zp_jy": 50.0}}
    cols = [SimpleNamespace(key="m1", role="mag", filter="A/X.F1", err="e1", unit=None, limit=None),
            {"key": "m2", "role": "mag", "filter": "A/X.F2", "limit": "k2"},
            {"key": "fl", "role": "flux", "filter": "A/X.F3", "unit": "mJy", "err": "efl"},
            {"key": "zz", "role": "mag", "filter": "missing"}]
    row = {"m1": 2.5, "e1": 0.1, "m2": 0.0, "k2": "u", "fl": 5.0, "efl": 1.0, "zz": 1.0,
           "_lim": {"fl": 2}}
    pts = S.sed_points(row, {"filters": filters}, cols)
    assert [p["key"] for p in pts] == ["m2", "m1", "fl"]
    assert pts[0]["kind"] == "ul" and pts[0]["err"] is None
    assert pts[1]["fnu"] == pytest.approx(10.0)
    assert pts[1]["err"] == pytest.approx(10.0 * math.log(10) / 2.5 * 0.1)
    assert pts[2] == {"wl": 3.0, "fnu": pytest.approx(5e-3), "err": None, "kind": "ll",
                      "key": "fl", "filter": "A/X.F3"}
    # missing / non-finite / non-positive values are dropped
    assert S.sed_points({"m1": None, "m2": float("nan"), "fl": -1.0}, filters, cols) == []
    assert S.sed_points({"m1": True}, filters, cols) == []
    # unicode keys pass through
    pts = S.sed_points({"mä": 1.0}, filters, [{"key": "mä", "role": "mag", "filter": "A/X.F1"}])
    assert pts[0]["key"] == "mä"


def test_scale_to():
    w, f = [1.0, 2.0, 3.0], [1.0, 1.0, 1.0]
    pts = [{"wl": 1.5, "fnu": 2.0, "kind": "det", "filter": "a"},
           {"wl": 2.5, "fnu": 4.0, "kind": "det", "filter": "b"},
           {"wl": 2.0, "fnu": 100.0, "kind": "ul", "filter": "c"}]
    assert S.scale_to(w, f, pts) == pytest.approx(3.0)
    assert S.scale_to(w, f, pts, ["b"]) == pytest.approx(4.0)
    assert math.isnan(S.scale_to(w, f, pts, ["c"]))
    assert math.isnan(S.scale_to(w, f, []))
    assert math.isnan(S.scale_to(w, f, [{"wl": 9.0, "fnu": 1.0}]))


# ---------------------------------------------------------------------------
# filters
# ---------------------------------------------------------------------------

def test_registry_contents():
    reg = F.load_registry()
    assert len(reg) >= 90
    assert not any("NIRSpec" in k for k in reg)
    assert not any(k.endswith((".Prism", ".Grism")) for k in reg)
    f410 = reg["JWST/NIRCam.F410M"]
    assert f410["label"] == "F410M" and f410["instrument"] == "NIRCam"
    assert f410["wl_um"] == pytest.approx(4.0822, abs=1e-3)
    assert f410["zp_jy"] == pytest.approx(208.75, rel=1e-4)
    assert f410["system"] == "Vega"
    for k, r in reg.items():
        assert r["wl_um"] > 0 and r["zp_jy"] > 0, k
        if r["trans_w"]:
            w, t = np.array(r["trans_w"]), np.array(r["trans_t"])
            assert w.size == F.TRANS_NPT and np.all(np.diff(w) > 0), k
            assert 0.9 < t.max() <= 1.0 and t.min() >= 0, k   # peak normalised on the native grid
            assert w[0] < r["wl_um"] < w[-1], k
    assert {"2MASS/2MASS.Ks", "Spitzer/IRAC.I1", "Spitzer/MIPS.24mu", "JWST/MIRI.F1500W",
            "JWST/NIRISS.F150W", "Roman/WFI.F062", "UKIRT/WFCAM.K", "Paranal/VISTA.Ks"} <= set(reg)


def test_lookup_and_resolve():
    assert F.lookup("F410M")["key"] == "JWST/NIRCam.F410M"
    assert F.lookup("f410m")["key"] == "JWST/NIRCam.F410M"
    assert F.lookup("NIRCAM_F410M")["key"] == "JWST/NIRCam.F410M"
    assert F.lookup("NIRISS_F150W")["key"] == "JWST/NIRISS.F150W"
    assert F.lookup("F150W")["key"] == "JWST/NIRCam.F150W"
    assert F.lookup("IRAC1")["key"] == "Spitzer/IRAC.I1"
    assert F.lookup("jwst/nircam.f410m")["key"] == "JWST/NIRCam.F410M"
    assert F.lookup("nonexistent") is None
    c = ["JWST/NIRCam.F182M", "JWST/NIRCam.F410M"]
    assert F.resolve_key("f182m", c) == "JWST/NIRCam.F182M"
    assert F.resolve_key("nircam_f410m", c) == "JWST/NIRCam.F410M"
    assert F.resolve_key("x", c, {"x": "JWST/NIRCam.F410M"}) == "JWST/NIRCam.F410M"
    assert F.resolve_key("x", c, {"x": "not/a.candidate"}) is None
    # ambiguous short name -> None
    assert F.resolve_key("f410m", ["JWST/NIRCam.F410M", "JWST/NIRCam2025.F410M"]) is None


def test_registry_regenerates_from_svo_cache(tmp_path):
    if not F.SVO_CACHE_DIR.is_dir() or not any(F.SVO_CACHE_DIR.glob("*.pickle")):
        pytest.skip("SVO cache not available")
    if not F.JWST_ZP_ECSV.is_file():
        pytest.skip("jwst_vega_zeropoints.ecsv not available")
    p = F.regenerate_registry(tmp_path / "reg.ecsv")
    assert p.read_bytes() == F.REGISTRY_PATH.read_bytes()


def test_downsample_transmission():
    w = np.linspace(1.0, 2.0, 1001)
    t = np.exp(-0.5 * ((w - 1.5) / 0.05) ** 2) * 0.8
    ww, tt = F.downsample_transmission(w, t)
    assert len(ww) == len(tt) == F.TRANS_NPT
    assert max(tt) == pytest.approx(1.0, abs=2e-3)
    assert 1.3 < ww[0] < 1.5 < ww[-1] < 1.7
    with pytest.raises(ValueError):
        F.downsample_transmission([1.0], [1.0])
    with pytest.raises(ValueError):
        F.downsample_transmission([1.0, 2.0], [0.0, 0.0])


def test_filters_document_all_validates():
    doc, curves = F.filters_document(None)
    assert schema.validate_filters(doc) == []
    assert doc["schema"] == "pk-filters/1"
    assert set(doc["filters"]) == set(F.registry_keys())
    mips70 = doc["filters"]["Spitzer/MIPS.70mu"]
    assert mips70["a_over_av"] == {"G23_3.1": None, "G23_5.5": None}
    assert "Spitzer/MIPS.70mu" not in curves
    json.loads(dumps_json(doc))


def test_filters_spec_short_keys_and_extra(tmp_path):
    spec = FilterSetSpec(keys=["F410M", "JWST/NIRCam.F182M"],
                         extra={"F410M": {"label": "F410M*"},
                                "W51/Custom.K": {"wl_um": 2.2, "zp_jy": 640.0, "label": "K ü",
                                                 "trans": {"w": [2.0, 2.2, 2.4],
                                                           "t": [0.0, 1.0, 0.0]}}})
    res = F.write(spec, tmp_path)
    doc = res.doc
    assert schema.validate_filters(doc) == []
    assert list(doc["filters"]) == ["F410M", "JWST/NIRCam.F182M", "W51/Custom.K"]
    assert doc["filters"]["F410M"]["label"] == "F410M*"
    assert doc["filters"]["F410M"]["wl_um"] == pytest.approx(4.0822, abs=1e-3)
    cust = doc["filters"]["W51/Custom.K"]
    assert cust["label"] == "K ü" and cust["system"] == "Vega" and cust["weff_um"] is None
    assert cust["a_over_av"]["G23_3.1"] == pytest.approx(E.a_over_av(2.2, 3.1), rel=1e-12)
    assert cust["a_over_av"]["G23_3.1"] == pytest.approx(0.103, abs=2e-3)
    # file layout: filters.<h8>.json + filters/<slug>.<h8>.json referenced relative
    raw = (tmp_path / res.path).read_bytes()
    assert res.path == f"filters.{h8(raw)}.json"
    for k, f in doc["filters"].items():
        tr = json.loads((tmp_path / f["trans"]).read_text())
        assert set(tr) == {"w", "t"} and len(tr["w"]) == len(tr["t"])
    with pytest.raises(KeyError):
        F.filters_document(FilterSetSpec(keys=["NOT/A.FILTER"]))
    with pytest.raises(ValueError):
        F.filters_document(FilterSetSpec(keys=["X/Y.Z"], extra={"X/Y.Z": {"wl_um": 1.0}}))
    with pytest.raises(ValueError):
        F.filters_document(FilterSetSpec(keys=["F410M"], extra={"F410M": {"bogus": 1}}))
    with pytest.raises(ValueError):
        F.filters_document(FilterSetSpec(keys=["F410M", "F410M"]))
    with pytest.raises(ValueError):
        F.filters_document(FilterSetSpec(keys=["X/Y.Z"], extra={"X/Y.Z": {"wl_um": -1.0,
                                                                          "zp_jy": 1.0}}))


def test_filters_write_without_trans(tmp_path):
    res = F.write(FilterSetSpec(keys=["F182M"]), tmp_path, trans=False)
    assert "trans" not in res.doc["filters"]["F182M"]
    assert res.files == [res.path]


# ---------------------------------------------------------------------------
# extinction
# ---------------------------------------------------------------------------

def test_alav_table_and_curve():
    tab = E.load_alav()
    assert set(tab) == set(F.registry_keys())
    assert tab["JWST/NIRCam.F410M"]["G23_3.1"] == pytest.approx(0.03595, rel=1e-3)
    assert tab["Spitzer/MIPS.70mu"]["G23_3.1"] is None
    cv = E.vendored_curve()
    assert cv["w"][0] >= E.G23_WL_RANGE_UM[0] and cv["w"][-1] <= E.G23_WL_RANGE_UM[1]
    assert np.all(np.diff(cv["w"]) > 0)
    # vendored per-filter values agree with the vendored curve (interpolation error only)
    for k in ("JWST/NIRCam.F182M", "JWST/NIRCam.F410M", "2MASS/2MASS.Ks"):
        assert E.a_over_av(tab[k]["wl_um"], 3.1) == pytest.approx(tab[k]["G23_3.1"], rel=2e-3)
    assert math.isnan(E.a_over_av(50.0, 3.1))
    assert math.isnan(E.a_over_av(0.05, 5.5))


@needs_dust
def test_alav_matches_dust_extinction_and_rv_linearity():
    tab = E.load_alav()
    keys = ["JWST/NIRCam.F115W", "JWST/NIRCam.F444W", "JWST/MIRI.F1000W", "2MASS/2MASS.J"]
    wl = np.array([tab[k]["wl_um"] for k in keys])
    for rv in (3.1, 5.5):
        de = E.g23_dust_extinction(wl, rv)
        assert np.allclose(de, [tab[k][E.law_key(rv)] for k in keys], rtol=2e-6)
    # A/AV is linear in 1/R_V: interpolation from 3.1 and 5.5 reproduces other R_V
    w = np.geomspace(0.1, 30.0, 50)
    k1, k2 = E.g23_dust_extinction(w, 3.1), E.g23_dust_extinction(w, 5.5)
    for rv in (2.5, 4.0, 5.0):
        assert np.allclose(E.interp_rv(rv, {3.1: k1, 5.5: k2}), E.g23_dust_extinction(w, rv),
                           rtol=1e-10, atol=1e-12)
    # vendored curve at R_V=4 vs dust_extinction (linear-in-wavelength sampling error)
    wv, kv = E.k_curve(4.0)
    sel = slice(10, None, 37)
    assert np.allclose(kv[sel], E.g23_dust_extinction(wv[sel], 4.0), rtol=1e-5)
    assert math.isnan(E.g23_dust_extinction(100.0, 3.1)[0])


@needs_dust
def test_alav_table_regenerates(tmp_path):
    p = E.regenerate_alav_table(tmp_path / "g23.ecsv")
    from astropy.table import Table
    a = Table.read(p)
    b = Table.read(E.ALAV_PATH)
    assert list(a["key"]) == list(b["key"])
    for c in ("G23_3.1", "G23_5.5"):
        x, y = np.asarray(a[c], float), np.asarray(b[c], float)
        assert np.array_equal(np.isnan(x), np.isnan(y))
        assert np.allclose(x[~np.isnan(x)], y[~np.isnan(y)], rtol=1e-12)
    assert a.meta["curve"] == b.meta["curve"]


def test_extinction_document_and_write(tmp_path):
    res = E.write(ExtinctionSpec(rv=(3.1, 4.0, 5.5)), tmp_path)
    doc = res.doc
    assert schema.validate_extinction(doc) == []
    assert doc["law"] == "G23" and doc["rv"] == [3.1, 4.0, 5.5]
    assert set(doc["curve"]) == {"3.1", "4", "5.5"}
    c31, c4, c55 = (np.array(doc["curve"][k]["k"]) for k in ("3.1", "4", "5.5"))
    f = (1 / 4.0 - 1 / 3.1) / (1 / 5.5 - 1 / 3.1)
    assert np.allclose(c4, c31 + (c55 - c31) * f, rtol=1e-5)
    assert res.path.startswith("extinction.") and (tmp_path / res.path).is_file()
    with pytest.raises(ValueError):
        E.extinction_document(ExtinctionSpec(rv=(7.0,)))
    with pytest.raises(ValueError):
        E.extinction_document(ExtinctionSpec(rv=()))
    with pytest.raises(ValueError):
        E.extinction_document(ExtinctionSpec(rv=(3.1, 3.1)))


def test_other_law_needs_dust_extinction():
    if HAVE_DUST:
        doc = E.extinction_document(ExtinctionSpec(law="F19", rv=(3.1,)))
        assert schema.validate_extinction(doc) == []
        with pytest.raises(ValueError):
            E.extinction_document(ExtinctionSpec(law="NoSuchLaw", rv=(3.1,)))
    else:
        with pytest.raises(RuntimeError):
            E.extinction_document(ExtinctionSpec(law="F19", rv=(3.1,)))


def test_a_lambda_fallbacks_and_vector():
    ext = E.extinction_document(None)
    fl = {"A/B.K": {"wl_um": 2.2, "a_over_av": {"G23_3.1": 0.1, "G23_5.5": 0.2}},
          "A/B.C": {"wl_um": 2.2, "a_over_av": {"G23_3.1": None}},
          "A/B.N": {"wl_um": 2.2}}
    assert E.a_lambda("A/B.K", 10.0, 3.1, ext, fl) == pytest.approx(1.0)
    f = (1 / 4.0 - 1 / 3.1) / (1 / 5.5 - 1 / 3.1)
    assert E.a_lambda("A/B.K", 1.0, 4.0, None, fl) == pytest.approx(0.1 + 0.1 * f)
    assert E.a_lambda("A/B.C", 1.0, 3.1, ext, fl) == pytest.approx(E.a_over_av(2.2, 3.1), rel=1e-5)
    assert math.isnan(E.a_lambda("A/B.N", 1.0, 3.1, None, fl))
    assert math.isnan(E.a_lambda("nope", 1.0, 3.1, ext, fl))
    assert E.a_lambda({"wl_um": 2.2, "a_over_av": {"G23_3.1": 0.5}}, 2.0, 3.1) == 1.0
    v = E.reddening_vector("k - c + 3", "k", 2.0, 3.1, {"filters": fl}, ext=ext)
    assert v["dx"] == pytest.approx(0.2 - 2.0 * E.a_over_av(2.2, 3.1), rel=1e-5)
    assert v["dy"] == pytest.approx(0.2)
    assert math.isnan(E.reddening_vector("k - zz", "k", 1.0, 3.1, fl)["dx"])


def test_redden():
    out = E.redden([1.0, 2.0, 3.0, 9.0], [1.0, 1.0, 1.0, 1.0], [1.0, 3.0], [0.2, 0.1], 5.0)
    assert out.dtype == np.float32
    assert out[0] == pytest.approx(10 ** (-0.4 * 1.0), rel=1e-6)
    assert out[1] == pytest.approx(10 ** (-0.4 * 0.75), rel=1e-6)
    assert math.isnan(out[3])
    assert E.redden([], [], [1.0, 2.0], [0.1, 0.1], 1.0).shape == (0,)


def test_interp_rv_errors():
    with pytest.raises(ValueError):
        E.interp_rv(4.0, {3.1: 1.0})
    assert E.interp_rv(3.1, {3.1: 1.0}) == 1.0
    assert E.interp_rv(4.0, {3.1: 1.0, 5.5: None}) is None


# ---------------------------------------------------------------------------
# templates
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def written_templates(tmp_path_factory):
    if not HAVE_TEMPLATES:
        pytest.skip("template JSON not available")
    root = tmp_path_factory.mktemp("tpl")
    led = Ledger()
    res = T.write(TemplateSpec(), BundleWriter(root, "", led))
    return root, res, led


def _grid(root: Path, idx: dict, g: str) -> dict:
    return json.loads((root / "templates" / idx["grids"][g]["file"]).read_text())


@needs_templates
def test_templates_index_and_roundtrip(written_templates):
    root, res, led = written_templates
    idx = res.doc
    assert schema.validate_templates(idx) == []
    assert res.path.startswith("templates/index.") and (root / res.path).is_file()
    src = json.loads(T.DEFAULT_SOURCE.read_text())
    assert [t["key"] for t in idx["templates"]] == [t["key"] for t in src["templates"]]
    assert set(idx["laws"]) == {"k31", "k55"}
    sizes = []
    for g, gv in src["grids"].items():
        gd = _grid(root, idx, g)
        w = np.asarray(gd["values"]) if gd["kind"] == "array" else gd["w0"] + gd["dw"] * np.arange(gd["n"])
        assert np.allclose(w, gv["w"], rtol=1e-12)
        assert idx["grids"][g]["n"] == len(gv["w"])
        for t, ti in zip(src["templates"], idx["templates"]):
            raw = (root / "templates" / ti["files"][g]).read_bytes()
            sizes.append(len(raw))
            assert raw[:2] == b"\x1f\x8b"
            _, hdr, _ = split_record(gzip.decompress(raw))
            assert schema.validate_pks1_header(hdr) == []
            assert hdr["wave"] == {"kind": "shared", "grid": g}
            assert hdr["meta"]["template"] == t["key"]
            rec = decode_pks1(raw, {g: gd})
            assert np.allclose(rec["channels"]["flux"], t["f"][g], rtol=1e-4)
        for lk in ("k31", "k55"):
            rec = decode_pks1((root / "templates" / idx["laws"][lk][g]).read_bytes(), {g: w})
            assert np.allclose(rec["channels"]["k"], gv[lk], rtol=1e-6)
    # about 20 kB per (template, grid) file on average; the R=2700 grid is the largest
    assert np.mean(sizes) < 20 * 1024
    assert max(sizes) <= T.TEMPLATE_FILE_SOFT_MAX
    assert led.total() > 0


@needs_templates
def test_templates_law_matches_g23():
    src = json.loads(T.DEFAULT_SOURCE.read_text())
    g = src["grids"]["prism"]
    w = np.asarray(g["w"])
    assert np.allclose(E.a_over_av(w, 3.1), g["k31"], rtol=2e-3, atol=1e-5)


def _tiny_source(tmp_path: Path, **over) -> Path:
    d = {"meta": {"wmin": 1.0, "wmax": 5.0, "note": "tiny ü"},
         "grids": {"g": {"w": [1.0, 2.0, 3.0, 4.0, 5.0], "R": 10.0,
                         "k31": [0.3, 0.12, 0.06, 0.04, 0.03], "k55": [0.4, 0.2, 0.1, 0.07, 0.05]}},
         "templates": [{"key": "a", "teff": 5000.0, "logg": 4.5, "label": "A ☆",
                        "f": {"g": [1.0, 2.0, 1e-30, 1e30, 3.0]}},
                       {"key": "b c", "teff": None, "logg": None, "label": "",
                        "f": {"g": [1.0, 1.0, 1.0, 1.0, 1.0]}}]}
    d.update(over)
    p = tmp_path / "tiny.json"
    p.write_text(json.dumps(d))
    return p


def test_templates_tiny_source_edge_cases(tmp_path):
    p = _tiny_source(tmp_path)
    res = T.write(TemplateSpec(source=p), tmp_path / "out")
    assert schema.validate_templates(res.doc) == []
    files = res.doc["templates"][1]["files"]["g"]
    assert files.startswith("t_b_c_g.")
    gd = json.loads((tmp_path / "out" / "templates" / res.doc["grids"]["g"]["file"]).read_text())
    assert gd["kind"] == "uniform" and gd["n"] == 5
    raw = (tmp_path / "out" / "templates" / res.doc["templates"][0]["files"]["g"]).read_bytes()
    rec = decode_pks1(raw, {"g": gd})
    assert np.allclose(rec["channels"]["flux"], [1.0, 2.0, 1e-30, 1e30, 3.0], rtol=1e-2)
    assert res.doc["templates"][0]["label"] == "A ☆"
    # deterministic: same input -> same names
    res2 = T.write(TemplateSpec(source=p), tmp_path / "out2")
    assert res2.path == res.path


@pytest.mark.parametrize("bad", [
    {"templates": [{"key": "a", "f": {"g": [1.0, 2.0, 3.0, 4.0, -1.0]}}]},
    {"templates": [{"key": "a", "f": {"g": [1.0, 2.0, float("nan"), 4.0, 1.0]}}]},
    {"templates": [{"key": "a", "f": {"g": [1.0, 2.0]}}]},
    {"templates": [{"key": "a", "f": {"g": [1.0] * 5}}, {"key": "a", "f": {"g": [1.0] * 5}}]},
    {"grids": {"g": {"w": [1.0, 1.0, 2.0, 3.0, 4.0]}}},
])
def test_templates_reject_bad_input(tmp_path, bad):
    p = _tiny_source(tmp_path, **bad)
    with pytest.raises(ValueError):
        T.write(TemplateSpec(source=p), tmp_path / "out")


def test_extend_templates_tiny(tmp_path):
    src = json.loads(_tiny_source(tmp_path).read_text())
    src["grids"]["g"]["w"] = list(np.geomspace(1.0, 5.0, 60))
    src["grids"]["g"]["k31"] = list(0.3 * np.asarray(src["grids"]["g"]["w"]) ** -1.7)
    src["grids"]["g"]["k55"] = list(0.4 * np.asarray(src["grids"]["g"]["w"]) ** -1.5)
    for t in src["templates"]:
        t["teff"] = 4000.0
        t["f"]["g"] = list(np.asarray(src["grids"]["g"]["w"]) ** -2.0)
    out = T.extend_templates(src, 26.0)
    g = out["grids"]["g"]
    assert g["w"][-1] >= 26.0 and len(g["w"]) == g["n"] == len(g["k31"])
    w = np.asarray(g["w"])
    k = np.asarray(g["k31"])
    tail = w > 6
    slope = np.polyfit(np.log(w[tail]), np.log(k[tail]), 1)[0]
    assert slope == pytest.approx(-1.7, abs=1e-6)
    f = np.asarray(out["templates"][0]["f"]["g"])
    assert np.all(np.diff(np.log(f[tail])) < 0)
    # B_nu continuation: Rayleigh-Jeans slope close to -2
    assert np.polyfit(np.log(w[w > 20]), np.log(f[w > 20]), 1)[0] == pytest.approx(-2.0, abs=0.1)
    assert out["meta"]["wmax"] == 26.0 and "B_nu" in out["meta"]["caveat"]
    assert src["grids"]["g"]["n"] if "n" in src["grids"]["g"] else True
    assert len(src["grids"]["g"]["w"]) == 60   # input untouched
    with pytest.raises(ValueError):
        T.extend_templates(src, 3.0)
    with pytest.raises(ValueError):
        T.extend_templates(src, 26.0, planck="kelvin")


@needs_templates
def test_extend_to_um_write(tmp_path):
    res = T.write(TemplateSpec(extend_to_um=26.0), tmp_path)
    idx = res.doc
    assert schema.validate_templates(idx) == []
    gd = _grid(tmp_path, idx, "prism")
    assert gd["values"][-1] >= 26.0
    assert idx["meta"]["wmax"] == 26.0


@needs_templates
def test_build_extended_templates_legacy_parity(tmp_path):
    legacy = ANALYSIS / "Brick2221_F1500W_SEDPortal.py"
    if not legacy.is_file():
        pytest.skip("Brick2221 legacy script not available")
    ns = _legacy_namespace(legacy, {"_PLANCK_H", "_PLANCK_C", "_PLANCK_KB", "_planck_lambda",
                                    "_extend_grid_wavelengths", "_extend_extinction_curve",
                                    "_extend_template_flux", "build_extended_templates"})
    a, b = tmp_path / "legacy.json", tmp_path / "ours.json"
    ns["build_extended_templates"](str(T.DEFAULT_SOURCE), str(a), wmax_um=26.0)
    T.build_extended_templates(T.DEFAULT_SOURCE, b, wmax_um=26.0, planck="lambda")
    da, db = json.loads(a.read_text()), json.loads(b.read_text())
    for g in da["grids"]:
        for k in ("w", "k31", "k55"):
            assert np.allclose(da["grids"][g][k], db["grids"][g][k], rtol=1e-12)
    for ta, tb in zip(da["templates"], db["templates"]):
        for g in ta["f"]:
            assert np.allclose(ta["f"][g], tb["f"][g], rtol=1e-12)
    assert da["meta"]["wmax"] == db["meta"]["wmax"]
    assert da["meta"]["caveat"] == db["meta"]["caveat"]
    assert da == db


# ---------------------------------------------------------------------------
# isochrones
# ---------------------------------------------------------------------------

_MIST_HEADER = """# MIST version number  = 2.5
# MESA revision number =    11701
# photometric system   = JWST NIRCAM (Vega)
# --------------------------------------------------------------------------------------
#  Yinit        Zinit   [Fe/H]   [a/Fe]  v/vcrit
# 0.2930  3.38311E-02     0.30     0.00     0.40
# --------------------------------------------------------------------------------------
# number of isochrones =     2
# extinction:   Av, Rv =  0.000 3.100
# --------------------------------------------------------------------------------------
# number of EEPs, cols =     4   8
#   1   2   3   4   5   6   7   8
# EEP log10_isochrone_age_yr initial_mass star_mass log_Teff {c1} {c2} phase
"""


def _mist_file(tmp_path: Path, bare: bool = False) -> Path:
    c1, c2 = ("F182M", "F410M") if bare else ("NIRCAM_F182M", "NIRCAM_F410M")
    rows = [
        (1, 6.0, 2.0, 2.0, 3.9, 1.0, 0.9, 0),
        (2, 6.0, 0.5, 0.5, 3.6, 5.0, 4.8, 0),
        (3, 6.0, 30.0, 25.0, 4.6, -6.0, -6.1, 9),
        (4, 6.0, 1.0, 1.0, 3.7, 3.0, 2.9, 0),
        (1, 9.0, 1.0, 1.0, 3.7, 4.0, 3.5, 2),
    ]
    p = tmp_path / ("bare.cmd" if bare else "mist.iso.JWST")
    body = "\n".join(" ".join(str(x) for x in r) for r in rows)
    p.write_text(_MIST_HEADER.format(c1=c1, c2=c2) + body + "\n")
    return p


def test_mist_header_and_columns(tmp_path):
    p = _mist_file(tmp_path)
    h = I.mist_header(p)
    assert h == {"version": "2.5", "system": "JWST NIRCAM (Vega)", "feh": 0.30, "av": 0.0, "rv": 3.1}
    assert I.mist_filter_key("NIRCAM_F410M") == "JWST/NIRCam.F410M"
    assert I.mist_filter_key("F410M", "JWST NIRCAM (Vega)") == "JWST/NIRCam.F410M"
    assert I.mist_filter_key("F410M", None) is None
    assert I.mist_filter_key("MIRI_F770W") == "JWST/MIRI.F770W"
    assert I.mist_filter_key("2MASS_Ks") == "2MASS/2MASS.Ks"
    assert I.mist_filter_key("IRAC_4.5") == "Spitzer/IRAC.I2"
    assert I.mist_filter_key("star_mass") is None


@pytest.mark.parametrize("bare", [False, True])
def test_build_isochrone_track_synthetic(tmp_path, bare):
    p = _mist_file(tmp_path, bare)
    tr = I.build_isochrone_track(p, 6.05, dist_pc=5400.0)
    assert list(tr.mass) == [0.5, 1.0, 2.0]                 # phase 9 dropped, sorted
    assert set(tr.mags) == {"JWST/NIRCam.F182M", "JWST/NIRCam.F410M"}
    assert list(tr.mags["JWST/NIRCam.F182M"]) == [5.0, 3.0, 1.0]   # absolute mags
    assert tr.age_myr == 1.0 and tr.dist_pc == 5400.0 and tr.feh == 0.3
    assert tr.label == "MIST 1 Myr"
    old = I.build_isochrone_track(p, 9.2, filters=["F410M"], label="old")
    assert old.label == "old" and old.age_myr == 1000.0 and set(old.mags) == {"JWST/NIRCam.F410M"}
    with pytest.raises(KeyError):
        I.build_isochrone_track(p, 6.0, filters=["F444W"])
    thin = I.build_isochrone_track(p, 6.0, npt=2)
    assert len(thin.mass) == 2 and thin.mass[0] == 0.5 and thin.mass[-1] == 2.0


def test_isochrones_document_matches_config(tmp_path):
    p = _mist_file(tmp_path)
    spec = I.isochrone_spec(p, ages_myr=(1.0, 1000.0), dist_pc=5400.0)
    spec.tracks[0].mags["JWST/NIRCam.F182M"] = np.array([np.nan, 3.0, 1.0])
    res = I.write(spec, tmp_path / "data")
    assert schema.validate_isochrones(res.doc) == []
    assert res.doc["tracks"][0]["mags"]["JWST/NIRCam.F182M"][0] is None
    raw = (tmp_path / "data" / res.path).read_bytes()
    assert raw == I.isochrones_bytes(spec)
    try:
        from portalkit.config import isochrones_file
    except ImportError:
        pytest.skip("portalkit.config not available")
    name, data = isochrones_file(spec)
    assert (name, data) == (res.path, raw)


def test_real_mist_file():
    if not I.DEFAULT_MIST.is_file():
        pytest.skip("MIST isochrone file not available")
    tr = I.build_isochrone_track(I.DEFAULT_MIST, 6.0, 5400.0,
                                 filters=["F182M", "F212N", "F410M"], npt=140)
    assert 50 < len(tr.mass) <= 140
    assert np.all(np.diff(tr.mass) >= 0)
    assert set(tr.mags) == {"JWST/NIRCam.F182M", "JWST/NIRCam.F212N", "JWST/NIRCam.F410M"}
    assert tr.feh == 0.3 and tr.age_myr == 1.0
    doc = I.isochrones_document(IsochroneSpec(tracks=[tr]))
    assert schema.validate_isochrones(doc) == []
    legacy = ANALYSIS / "WD2_Portal_Manifold.py"
    if legacy.is_file():
        ns = _legacy_namespace(legacy, {"load_mist_table"})
        t = ns["load_mist_table"](str(I.DEFAULT_MIST))
        assert t.colnames == I.load_mist_table(I.DEFAULT_MIST).colnames


def test_track_xy():
    filters = {"JWST/NIRCam.F182M": {"wl_um": 1.845, "a_over_av": {"G23_3.1": 0.14, "G23_5.5": 0.2}},
               "JWST/NIRCam.F410M": {"wl_um": 4.08, "a_over_av": {"G23_3.1": 0.036, "G23_5.5": 0.07}}}
    tr = Track(label="t", age_myr=1.0, dist_pc=100.0, feh=0.0, mass=np.array([1.0, 2.0]),
               mags={"JWST/NIRCam.F182M": np.array([3.0, 2.0]),
                     "JWST/NIRCam.F410M": np.array([2.5, 1.0])})
    x, y = I.track_xy(tr, "f182m - f410m", "f410m", 0.0, 3.1, 0.0, filters)
    assert x.dtype == np.float32
    assert np.allclose(x, [0.5, 1.0]) and np.allclose(y, [7.5, 6.0])
    x, y = I.track_xy(tr, "f182m - f410m", "f410m", 10.0, 3.1, 1.0, filters)
    assert np.allclose(x, [0.5 + 10 * (0.14 - 0.036), 1.0 + 10 * (0.14 - 0.036)], rtol=1e-6)
    assert np.allclose(y, [8.5 + 0.36, 7.0 + 0.36], rtol=1e-6)
    x, _ = I.track_xy(tr, "f182m - f999w", "f410m", 0.0, 3.1, 0.0, filters)
    assert np.all(np.isnan(x))


# ---------------------------------------------------------------------------
# features
# ---------------------------------------------------------------------------

def test_atlases_in_sync_with_constants():
    built = FT.build_atlases()
    assert set(built) == set(FT.FAMILIES)
    for fam, atlas in built.items():
        on_disk = (FT.FEATURES_DIR / f"{fam}.json").read_bytes()
        assert on_disk == FT._atlas_bytes(atlas), f"run features.regenerate_atlases() ({fam})"


def test_features_document():
    doc = FT.features_document()
    assert schema.validate_features(doc) == []
    assert list(doc["families"]) == list(FT.FAMILIES)
    for fam, feats in doc["families"].items():
        names = [f["name"] for f in feats]
        assert len(names) == len(set(names)), fam
        ws = [f["w_um"] for f in feats]
        assert ws == sorted(ws), fam
        for f in feats:
            assert f["kind"] in ("line", "band")
            if f["kind"] == "band" and "lo_um" in f:
                assert f["lo_um"] <= f["w_um"] <= f["hi_um"]
    assert all(f.get("requires_coverage") for f in doc["families"]["w51_nebular"])
    pah = [f for f in doc["families"]["gas"] if f.get("group") == "PAH"]
    assert [f["w_um"] for f in pah] == [6.2, 7.7, 8.6, 11.3, 12.7]
    assert any(f["name"] == "CO2 ice 15.2" for f in doc["families"]["ice"])
    bra = [f for f in doc["families"]["hi"] if f["name"] == "Br-a"]
    assert bra and bra[0]["w_um"] == pytest.approx(4.0523, abs=1e-3)
    sub = FT.features_document(["hi", "h2"])
    assert list(sub["families"]) == ["hi", "h2"] and set(sub["meta"]["families"]) == {"hi", "h2"}
    with pytest.raises(KeyError):
        FT.features_document(["nope"])
    with pytest.raises(ValueError):
        FT.features_document(["hi", "hi"])


def test_feature_constants_match_legacy():
    legacy = ANALYSIS / "JWST_Archive_ColorPortal.py"
    if not legacy.is_file():
        pytest.skip("JWST_Archive_ColorPortal.py not available")
    ns = _legacy_namespace(legacy, {"ICE_BANDS", "GAS_BANDS", "H2_LINES", "ION_LINES",
                                    "STELLAR_LINES", "hydrogen_lines"})
    for name in ("ICE_BANDS", "GAS_BANDS", "H2_LINES", "ION_LINES", "STELLAR_LINES"):
        assert ns[name] == getattr(FT, name), name
    assert ns["hydrogen_lines"]() == FT.hydrogen_lines()
    assert ns["hydrogen_lines"](0.6, 28.0) == FT.hydrogen_lines(0.6, 28.0)
    brick = ANALYSIS / "Brick2221_F1500W_SEDPortal.py"
    if brick.is_file():
        assert _legacy_namespace(brick, {"FEATURE_FAMILIES"})["FEATURE_FAMILIES"] == \
            FT.BRICK_FEATURE_FAMILIES
    w51 = W51_PORTAL / "W51_Portal_Spectra.py"
    if w51.is_file():
        ns = _legacy_namespace(w51, {"LINES", "LCOL"})
        assert ns["LINES"] == FT.W51_LINES and ns["LCOL"] == FT.W51_LCOL


# ---------------------------------------------------------------------------
# package-level writers and goldens
# ---------------------------------------------------------------------------

@needs_templates
def test_write_shared_and_dispatch(tmp_path):
    led = Ledger()
    refs = physics.write_shared(tmp_path / "res", ledger=led)
    assert set(refs) == set(physics.SHARED_KINDS)
    for kind, rel in refs.items():
        doc = json.loads((tmp_path / "res" / rel).read_text())
        assert schema.validate(doc) == [], kind
        name = rel.rsplit("/", 1)[-1]
        assert name.split(".")[1] == h8((tmp_path / "res" / rel).read_bytes())
    r = physics.write_dataset(ExtinctionSpec(), tmp_path / "x")
    assert r.path == refs["extinction"]
    with pytest.raises(TypeError):
        physics.write_dataset(object(), tmp_path / "x")


def test_writer_rejects_symlink_escape(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "root").mkdir()
    (tmp_path / "root" / "filters").symlink_to(tmp_path / "real")
    from portalkit.bundle.writer import StagingError
    with pytest.raises(StagingError):
        F.write(FilterSetSpec(keys=["F182M"]), tmp_path / "root")


def test_goldens_in_sync():
    spec = importlib.util.spec_from_file_location("physics_golden_generate", GOLDEN / "generate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    files = mod.build()
    for name, data in files.items():
        assert (GOLDEN / name).read_bytes() == data, f"regenerate {name}"
    idx = json.loads((GOLDEN / "index.json").read_text())
    assert idx["rtol"] == 1e-6 and set(idx["files"]) | {"index.json"} == set(files)


def test_goldens_self_consistent():
    """Recompute the golden values from their own inputs (guards the generator wiring)."""
    red = json.loads((GOLDEN / "reddening.json").read_text())
    for c in red["a_lambda"]:
        v = E.a_lambda(c["filter"], c["av"], c["rv"], red["ext"], red["filters"])
        exp = c["a_lambda"]
        assert (exp is None and math.isnan(v)) or v == pytest.approx(exp, rel=1e-12)
    mj = json.loads((GOLDEN / "mag_jy.json").read_text())
    for c in mj["mag_to_jy"]:
        v = S.mag_to_jy(np.nan if c["mag"] is None else c["mag"],
                        np.nan if c["zp_jy"] is None else c["zp_jy"])
        assert (c["jy"] is None and not math.isfinite(v)) or v == pytest.approx(c["jy"], rel=1e-12)
    sp = json.loads((GOLDEN / "sed_points.json").read_text())
    for c in sp["cases"]:
        assert json.loads(dumps_json(S.sed_points(c["row"], sp["filters"], sp["cols"]))) == c["points"]


# ---------------------------------------------------------------------------
# review fixes: shared-document resolution, MIST names, sentinels, families
# ---------------------------------------------------------------------------

def test_resolve_key_label_tiebreak_on_shared_document():
    doc, _ = F.filters_document(None)
    fl = doc["filters"]
    assert any(k.startswith("JWST/NIRCam2025.") for k in fl)
    for name in ("f410m", "F410M", "f182m", "f444w", "f200w", "nircam_f410m"):
        k = F.resolve_key(name, fl)
        assert k is not None and k.startswith("JWST/NIRCam."), (name, k)
    # without entries (plain key list) the short name stays ambiguous
    assert F.resolve_key("f410m", list(fl)) is None
    v = E.reddening_vector("f182m - f410m", "f410m", 10.0, 3.1, doc)
    exp_dx = 10 * (fl["JWST/NIRCam.F182M"]["a_over_av"]["G23_3.1"]
                   - fl["JWST/NIRCam.F410M"]["a_over_av"]["G23_3.1"])
    assert v["dx"] == pytest.approx(exp_dx, rel=1e-12)
    assert v["dy"] == pytest.approx(10 * fl["JWST/NIRCam.F410M"]["a_over_av"]["G23_3.1"], rel=1e-12)
    assert math.isfinite(E.reddening_vector("f200w - f444w", "f444w", 10.0, 3.1, doc)["dx"])
    # labels that do not single one out stay ambiguous (needs a colmap)
    two = {"A/X.Ks": {"label": "A Ks"}, "B/Y.Ks": {"label": "B Ks"}}
    assert F.resolve_key("ks", two) is None
    assert F.resolve_key("ks", two, {"ks": "B/Y.Ks"}) == "B/Y.Ks"


def test_resolve_key_full_key_against_short_keyed_document():
    fl = {"F200W": {"label": "F200W"}, "F444W": {"label": "F444W"}}
    assert F.resolve_key("JWST/NIRCam.F200W", fl) == "F200W"
    assert F.resolve_key("jwst/nircam.f444w", list(fl)) == "F444W"


def test_track_xy_short_keyed_filters_with_extinction():
    d2, _ = F.filters_document(SimpleNamespace(keys=["F200W", "F444W"], extra={}))
    assert list(d2["filters"]) == ["F200W", "F444W"]
    tr = Track(label="t", age_myr=1.0, dist_pc=10.0, feh=0.0, mass=np.array([1.0, 2.0]),
               mags={"JWST/NIRCam.F200W": np.array([1.0, 1.5]),
                     "JWST/NIRCam.F444W": np.array([0.5, 0.5])})
    x0, y0 = I.track_xy(tr, "f200w - f444w", "f444w", 0.0, 3.1, 0.0, d2["filters"])
    x5, y5 = I.track_xy(tr, "f200w - f444w", "f444w", 5.0, 3.1, 0.0, d2["filters"])
    v = E.reddening_vector("F200W - F444W", "F444W", 5.0, 3.1, d2)
    assert np.all(np.isfinite(x5)) and np.all(np.isfinite(y5))
    assert np.allclose(x5 - x0, v["dx"], rtol=1e-5) and np.allclose(y5 - y0, v["dy"], rtol=1e-5)
    # the expression key (with colmap) is the fallback when the mags key finds no entry
    fl = {"mycol": {"label": "c", "wl_um": 2.0, "a_over_av": {"G23_3.1": 0.1, "G23_5.5": 0.2}}}
    tr2 = Track(label="t", age_myr=1.0, dist_pc=10.0, feh=0.0, mass=np.array([1.0]),
                mags={"ZZZ/Q.Q1": np.array([1.0])})
    x, _ = I.track_xy(tr2, "mycol", "mycol", 2.0, 3.1, 0.0, fl, {"mycol": "ZZZ/Q.Q1"})
    assert np.allclose(x, [1.2])  # mags key ZZZ/Q.Q1 has no entry; expression key 'mycol' does
    x, _ = I.track_xy(tr2, "mycol", "mycol", 2.0, 3.1, 0.0, {"other": fl["mycol"]},
                      {"mycol": "ZZZ/Q.Q1"})
    assert np.all(np.isnan(x))  # no filter entry at all -> NaN when av != 0
    x, _ = I.track_xy(tr2, "q1", "q1", 2.0, 3.1, 0.0, {"Q1": fl["mycol"]})
    assert np.allclose(x, [1.2])


def test_mist_nircam_name_fixes():
    assert I.mist_filter_key("NIRCAM_F140W") == "JWST/NIRCam.F140M"
    assert I.mist_filter_key("NIRCAM_F470W") == "JWST/NIRCam.F470N"
    assert I.mist_filter_key("NIRCAM_F480W") == "JWST/NIRCam.F480M"
    assert I.mist_filter_key("F480W", "JWST NIRCAM") == "JWST/NIRCam.F480M"
    assert I.mist_filter_key("NIRCAM_F410M") == "JWST/NIRCam.F410M"
    reg = F.load_registry()
    for k in ("JWST/NIRCam.F140M", "JWST/NIRCam.F470N", "JWST/NIRCam.F480M"):
        assert k in reg


def test_default_mist_mags_all_in_registry():
    if not I.DEFAULT_MIST.is_file():
        pytest.skip("MIST isochrone file not available")
    reg = F.load_registry()
    tr = I.build_isochrone_track(I.DEFAULT_MIST, 6.0, 10.0)
    assert [k for k in tr.mags if k not in reg] == []
    tr = I.build_isochrone_track(I.DEFAULT_MIST, 6.0, 10.0, filters=["JWST/NIRCam.F480M", "f140m"])
    assert set(tr.mags) == {"JWST/NIRCam.F480M", "JWST/NIRCam.F140M"}


def test_filter_entry_wl_override_resamples_a_over_av():
    base, _ = F.filter_entry("JWST/NIRCam.F410M")
    moved, _ = F.filter_entry("JWST/NIRCam.F410M", {"wl_um": 10.0})
    for rv in E.VENDORED_RV:
        lk = E.law_key(rv)
        assert moved["a_over_av"][lk] == pytest.approx(E.a_over_av(10.0, rv), rel=1e-12)
        assert moved["a_over_av"][lk] != pytest.approx(base["a_over_av"][lk], rel=1e-3)
    same, _ = F.filter_entry("JWST/NIRCam.F410M", {"wl_um": base["wl_um"]})
    assert same["a_over_av"] == base["a_over_av"]


def test_sed_points_sentinels_and_string_limit_codes():
    fl = {"JWST/NIRCam.F182M": {"wl_um": 1.845, "zp_jy": 858.0}}
    cols = [{"key": "m", "role": "mag", "filter": "JWST/NIRCam.F182M", "err": "e"}]
    assert S.sed_points({"m": -999.0, "e": 0.1}, fl, cols) == []
    assert S.sed_points({"m": 999.0, "e": 0.1}, fl, cols) == []
    assert S.sed_points({"m": 10.0, "_lim": {"m": "1"}}, fl, cols)[0]["kind"] == "ul"
    assert S.sed_points({"m": 10.0, "_lim": {"m": " 2 "}}, fl, cols)[0]["kind"] == "ll"
    assert S.sed_points({"m": 10.0, "_limits": {"m": 1}}, fl, cols)[0]["kind"] == "ul"
    cols2 = [dict(cols[0], limit="k")]
    assert S.sed_points({"m": 10.0, "k": "1"}, fl, cols2)[0]["kind"] == "ul"
    assert S.sed_points({"m": 10.0, "k": "0"}, fl, cols2)[0]["kind"] == "det"


def test_extend_templates_caveat_by_planck(tmp_path):
    src = json.loads(_tiny_source(tmp_path).read_text())
    src["grids"]["g"]["w"] = list(np.geomspace(1.0, 5.0, 60))
    src["grids"]["g"]["k31"] = list(0.3 * np.asarray(src["grids"]["g"]["w"]) ** -1.7)
    src["grids"]["g"]["k55"] = list(0.4 * np.asarray(src["grids"]["g"]["w"]) ** -1.5)
    for t in src["templates"]:
        t["teff"] = 4000.0
        t["f"]["g"] = list(np.asarray(src["grids"]["g"]["w"]) ** -2.0)
    lam = T.extend_templates(src, 26.0, planck="lambda")["meta"]["caveat"]
    assert "Extended to 26.0um by blackbody continuum (scaled to match the native grid" in lam
    assert "B_" not in lam
    nu = T.extend_templates(src, 26.0)["meta"]["caveat"]
    assert "(B_nu, scaled to match the native grid" in nu


def test_features_family_arguments(tmp_path):
    assert list(FT.write("ice", tmp_path / "a").doc["families"]) == ["ice"]
    assert list(FT.write({"ice"}, tmp_path / "b").doc["families"]) == ["ice"]
    assert list(FT.write(("hi", "h2"), tmp_path / "c").doc["families"]) == ["hi", "h2"]
    assert list(FT.write(SimpleNamespace(families=["gas"]), tmp_path / "d").doc["families"]) == ["gas"]
    assert list(FT.write(None, tmp_path / "e").doc["families"]) == list(FT.FAMILIES)
    for bad in ("../features/ice", "ICE", "ice/../gas", ""):
        with pytest.raises(ValueError):
            FT.load_atlas(bad)
    with pytest.raises(ValueError):
        FT.features_document(["../features/ice"])
    with pytest.raises(TypeError):
        FT.features_document(5)


def test_resolve_key_golden_self_consistent():
    g = json.loads((GOLDEN / "resolve_key.json").read_text())
    shared = g["shared_filters"]
    doc, _ = F.filters_document(None)
    assert set(shared) == set(doc["filters"])
    for c in g["cases"]:
        cands = shared if c["candidates"] == "shared" else c["candidates"]
        assert F.resolve_key(c["name"], cands, c.get("colmap"), c.get("aliases")) == c["expected"], c
    red = json.loads((GOLDEN / "reddening.json").read_text())
    for c in g["vector"]:
        v = E.reddening_vector(c["x"], c["y"], c["av"], c["rv"], shared, c.get("colmap"), red["ext"])
        for k in ("dx", "dy"):
            assert (c[k] is None and math.isnan(v[k])) or v[k] == pytest.approx(c[k], rel=1e-12)
    # the shared-document cases cover the ambiguous NIRCam short names
    by_name = {c["name"]: c["expected"] for c in g["cases"] if c["candidates"] == "shared"}
    assert by_name["f410m"] == "JWST/NIRCam.F410M" and by_name["ks"] is None
