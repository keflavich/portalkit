"""portalkit.spec: dataclasses and helpers (ARCHITECTURE.md section 9.1)."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from portalkit import expr, spec
from portalkit.spec import (CatalogSpec, ColumnSpec, ImageSetSpec, PageSpec, ReadmeSpec,
                            RecordSetSpec, catalog_from_rows, join, safe_key, standard_pages)


def _rows(n=5):
    return [{"name": f"s{i}", "ra": 10.0 + i, "dec": -5.0 + i, "f182m": 15.0 + i, "f410m": 13.0 + 0.5 * i,
             "cls": "yso" if i % 2 else "star"} for i in range(n)]


# ---------------------------------------------------------------------------
# dataclasses
# ---------------------------------------------------------------------------

def test_column_defaults():
    c = ColumnSpec(key="f410m")
    assert c.role == "value" and c.enc == "auto" and c.clip == "none" and c.exact is True
    assert c.default_off == [] and c.levels is None
    d = ColumnSpec(key="x")
    d.default_off.append("a")
    assert c.default_off == []   # no shared mutable default


def test_page_defaults_and_vocab():
    p = PageSpec(id="map", archetype="explorer", title="Map", datasets=["src"])
    assert p.layout is None and p.state == {} and p.caps_disable == {} and p.links == {}
    assert p.manifold is False and p.portal is None
    assert spec.ARCHETYPES == ("landing", "explorer", "manifold", "gallery", "spectra",
                               "wavelength", "narrative")
    assert set(spec.NUMERIC_ROLES) <= set(spec.ROLES)
    assert issubclass(spec.TierUnavailable, RuntimeError)


def test_catalog_column_lookup_and_n():
    cat = catalog_from_rows(_rows(3), id="name", ra="ra", dec="dec", roles={"f182m": "mag"})
    assert cat.n == 3
    assert cat.column("f182m").role == "mag"
    assert cat.column("nope") is None
    assert CatalogSpec(key="f", table=None, id="id", columns=[]).n is None


# ---------------------------------------------------------------------------
# safe_key
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,key", [
    ("f410m", "f410m"), ("flux.F444W", "flux.F444W"), ("_x", "_x"),
    ("f182m-f410m", "c_f182m_f410m"), ("1abc", "_1abc"), ("a b", "a_b"),
    ("flux [mJy]", "flux_mJy"), ("e(B-V)", "c_e_B_V"), (".a", "_.a"), ("фx", "x"), ("ф", "col"),
])
def test_safe_key(name, key):
    got = safe_key(name)
    assert got == key
    assert expr.is_key(got)


def test_safe_key_unique_and_errors():
    taken: set[str] = set()
    got = [safe_key(x, taken) for x in ("a-b", "a_b", "c_a_b", "c_a_b")]
    assert got == ["c_a_b", "a_b", "c_a_b_2", "c_a_b_3"]
    assert taken == set(got)
    for bad in ("", None, 3):
        with pytest.raises(ValueError):
            safe_key(bad)


# ---------------------------------------------------------------------------
# catalog_from_rows
# ---------------------------------------------------------------------------

def test_catalog_from_rows_roles_and_types():
    rows = _rows(6)
    for i, r in enumerate(rows):
        r["n_obs"] = i
        r["good"] = bool(i % 2)
        r["url"] = f"https://data.rc.ufl.edu/pub/x/{i}.png"
        r["note"] = f"free text {i}"
    cat = catalog_from_rows(rows, id="name", ra="ra", dec="dec", roles={"f182m": "mag", "f410m": "mag"},
                            key="src", title="Test")
    roles = {c.key: c.role for c in cat.columns}
    assert roles == {"f182m": "mag", "f410m": "mag", "cls": "category", "n_obs": "value",
                     "good": "flag", "url": "url", "note": "text"}
    assert cat.column("cls").levels == ["star", "yso"]
    assert cat.table["n_obs"].dtype == np.int64
    assert cat.table["ra"].dtype == np.float64
    assert cat.table["name"].tolist() == [f"s{i}" for i in range(6)]
    assert {"name", "ra", "dec"}.isdisjoint(roles)
    assert cat.id == "name" and cat.ra == "ra" and cat.dec == "dec" and cat.title == "Test"


def test_catalog_from_rows_missing_values_masked():
    rows = _rows(4)
    rows[1]["f182m"] = None
    rows[2]["f182m"] = float("nan")
    rows[3].pop("cls")
    rows[0]["dec"] = None
    cat = catalog_from_rows(rows, id="name", ra="ra", dec="dec")
    col = cat.table["f182m"]
    assert col.mask.tolist() == [False, True, True, False]
    assert col.dtype == np.float64
    assert cat.table["cls"].mask.tolist() == [False, False, False, True]
    assert math.isnan(float(np.asarray(cat.table["dec"])[0])) and cat.table["dec"].mask[0]


def test_catalog_from_rows_int_with_missing_becomes_float():
    rows = [{"id": "a", "k": 1}, {"id": "b", "k": None}, {"id": "c", "k": 2 ** 40}]
    cat = catalog_from_rows(rows, id="id")
    assert cat.table["k"].dtype == np.float64
    assert float(cat.table["k"][2]) == 2.0 ** 40


def test_catalog_from_rows_renames_and_remaps_refs():
    rows = [{"id": 1, "f182m-f410m": 0.5, "e f182m-f410m": 0.1, "2mass j": 12.0},
            {"id": 2, "f182m-f410m": 0.7, "e f182m-f410m": 0.2, "2mass j": 12.5}]
    cat = catalog_from_rows(rows, id="id", roles={"f182m-f410m": "color", "e f182m-f410m": "error"},
                            columns={"f182m-f410m": {"err": "e f182m-f410m", "unit": "mag"}})
    keys = [c.key for c in cat.columns]
    assert keys == ["c_f182m_f410m", "c_e_f182m_f410m", "_2mass_j"]
    c = cat.column("c_f182m_f410m")
    assert c.label == "f182m-f410m" and c.err == "c_e_f182m_f410m" and c.unit == "mag"
    assert cat.table["id"].tolist() == ["1", "2"]   # ids are strings
    assert all(expr.is_key(k) for k in cat.table.colnames)


def test_catalog_from_rows_errors():
    with pytest.raises(ValueError, match="duplicate"):
        catalog_from_rows([{"id": "a"}, {"id": "a"}], id="id")
    with pytest.raises(ValueError, match="no id"):
        catalog_from_rows([{"id": "a"}, {"id": ""}], id="id")
    with pytest.raises(ValueError, match="no id"):
        catalog_from_rows([{"id": "a"}, {"x": 1}], id="id")
    with pytest.raises(ValueError, match="not numeric"):
        catalog_from_rows([{"id": "a", "ra": "12h"}], id="id", ra="ra", dec="ra")
    with pytest.raises(KeyError):
        catalog_from_rows([{"id": "a"}], id="id", ra="ra", dec="dec")
    with pytest.raises(ValueError, match="unknown role"):
        catalog_from_rows([{"id": "a", "x": 1.0}], id="id", roles={"x": "nonsense"})
    with pytest.raises(TypeError):
        catalog_from_rows([["a"]], id="id")


def test_catalog_from_rows_empty_and_single():
    cat = catalog_from_rows([], id="id", ra="ra", dec="dec", roles={"f410m": "mag"})
    assert cat.n == 0 and [c.key for c in cat.columns] == ["f410m"]
    one = catalog_from_rows([{"id": "x", "v": 1.5, "s": "only"}], id="id")
    assert one.n == 1
    assert {c.key: c.role for c in one.columns} == {"v": "value", "s": "text"}


def test_catalog_from_rows_unicode_values():
    rows = [{"id": "ε-1", "name": "Sgr B2 (N) ☃"}, {"id": "ε-2", "name": "Sgr B2 (M)"}]
    cat = catalog_from_rows(rows, id="id")
    assert cat.table["id"].tolist() == ["ε-1", "ε-2"]
    assert cat.table["name"][0] == "Sgr B2 (N) ☃"


# ---------------------------------------------------------------------------
# join
# ---------------------------------------------------------------------------

def test_join_left_order_and_masks():
    a = catalog_from_rows(_rows(4), id="name", ra="ra", dec="dec", roles={"f182m": "mag"}, key="a")
    b = catalog_from_rows([{"name": "s3", "av": 30.0, "f182m": 1.0}, {"name": "s0", "av": 10.0, "f182m": 2.0},
                           {"name": "zz", "av": 99.0, "f182m": 3.0}], id="name", key="b",
                          roles={"av": "value", "f182m": "mag"})
    b.caveats.append("b caveat")
    j = join(a, b, on="name")
    assert j.key == "a" and j.n == 4
    assert j.table["name"].tolist() == ["s0", "s1", "s2", "s3"]
    av = j.table["av"]
    assert av.mask.tolist() == [False, True, True, False]
    assert float(av[0]) == 10.0 and float(av[3]) == 30.0
    # clash renamed with the b key, label keeps the source key
    assert "f182m_b" in j.table.colnames
    assert j.column("f182m_b").label == "f182m"
    assert j.caveats == ["b caveat"]
    # b's id column is not copied again
    assert j.table.colnames.count("name") == 1


def test_join_numeric_keys_match_strings():
    a = catalog_from_rows([{"id": "1"}, {"id": "2"}], id="id", key="a")
    b = CatalogSpec(key="b", table=None, id="id", columns=[])
    from astropy.table import Table
    b.table = Table({"id": np.array([2.0, 1.0]), "v": np.array([20.0, 10.0])})
    b.columns = [ColumnSpec(key="v")]
    j = join(a, b, on="id")
    assert [float(x) for x in j.table["v"]] == [10.0, 20.0]


def test_join_errors():
    a = catalog_from_rows(_rows(2), id="name", key="a")
    dup = catalog_from_rows(_rows(2), id="name", key="d")
    dup.table["name"][1] = "s0"
    with pytest.raises(ValueError, match="duplicate"):
        join(a, dup, on="name")
    with pytest.raises(KeyError):
        join(a, dup, on="missing")
    f = CatalogSpec(key="f", table=None, id="id", columns=[])
    with pytest.raises(ValueError):
        join(a, f, on="name")


def test_join_empty_right():
    a = catalog_from_rows(_rows(3), id="name", key="a")
    b = catalog_from_rows([], id="name", key="b", roles={"av": "value"})
    j = join(a, b, on="name")
    assert j.table["av"].mask.tolist() == [True, True, True]


# ---------------------------------------------------------------------------
# ImageSetSpec.from_rows
# ---------------------------------------------------------------------------

def test_imageset_from_rows():
    rows = [
        {"id": "a", "png": "img/a.png", "sed_png": "img/a_sed.png", "program": 1611, "grating": "g395m",
         "caption": "A", "w": 640, "h": 480, "meta": {"x": 1}},
        {"id": "b", "png": "img/b.png", "program": 3222, "grating": "prism", "caption": None},
        {"id": "c", "png": "", "program": 1},   # no role URL: skipped
    ]
    s = ImageSetSpec.from_rows(rows, {"spec": "png", "sed": ("sed_png", "SED fit"), "unused": "zzz"},
                               key="gal", catalog="src")
    assert [it.id for it in s.items] == ["a", "b"]
    assert s.roles == [("spec", "Spec"), ("sed", "SED fit")]
    assert s.facets == [("program", "Program"), ("grating", "Grating")]
    assert s.items[0].tags == {"program": "1611", "grating": "g395m"}
    assert s.items[0].w == 640 and s.items[1].w is None
    assert s.items[1].caption == ""
    assert s.catalog == "src" and s.key == "gal"


def test_imageset_from_rows_errors_and_empty():
    with pytest.raises(ValueError, match="duplicate"):
        ImageSetSpec.from_rows([{"id": "a", "u": "x.png"}, {"id": "a", "u": "y.png"}], {"img": "u"})
    with pytest.raises(ValueError):
        ImageSetSpec.from_rows([{"u": "x.png"}], {"img": "u"})
    empty = ImageSetSpec.from_rows([], {"img": "u"})
    assert empty.items == [] and empty.roles == [] and empty.facets == []


# ---------------------------------------------------------------------------
# standard_pages
# ---------------------------------------------------------------------------

def _rec(key, kind, cat):
    return RecordSetSpec(key=key, kind=kind, catalog=cat, ids=["s0"], loader=lambda i: None)


def test_standard_pages_full():
    src = catalog_from_rows(_rows(), id="name", ra="ra", dec="dec",
                            roles={"f182m": "mag", "f410m": "mag", "cls": "category"}, key="src", title="Sources")
    src.columns.append(ColumnSpec(key="extra", role="value"))
    ctx = catalog_from_rows(_rows(), id="name", key="ctx", context=True)
    ds = {"src": src, "ctx": ctx, "spec": _rec("spec", "pks1", "src"), "s2d": _rec("s2d", "pki1", "src"),
          "img": ImageSetSpec(key="img", items=[], roles=[], catalog="src"),
          "readme": ReadmeSpec(markdown="# hi"), "hips": spec.HipsSpec(base=[])}
    pages = standard_pages(ds, manifold=True, spectra=True, portal="p")
    ids = [p.id for p in pages]
    assert ids == ["index", "explorer", "manifold", "spectra", "gallery"]
    by = {p.id: p for p in pages}
    assert by["index"].datasets == ["readme"]
    assert by["explorer"].datasets == ["src", "spec", "s2d", "img", "ctx", "hips"]
    assert by["manifold"].manifold is True
    assert by["spectra"].datasets[:2] == ["src", "spec"]
    assert by["gallery"].datasets == ["img", "src", "hips"]
    assert by["explorer"].links["spectra"] == "spectra.html#id={id}"
    assert "explorer" not in by["explorer"].links
    assert all(p.portal == "p" for p in pages)


def test_standard_pages_multiple_and_errors():
    a = catalog_from_rows(_rows(), id="name", key="a")
    b = catalog_from_rows(_rows(), id="name", key="b")
    pages = standard_pages({"a": a, "b": b}, landing=False)
    assert [p.id for p in pages] == ["explorer_a", "explorer_b"]
    assert pages[0].links == {}
    with pytest.raises(KeyError):
        standard_pages({"a": a}, explorer=["zz"])
    with pytest.raises(TypeError):
        standard_pages({"a": a, "r": ReadmeSpec(markdown="")}, explorer=["r"])
    assert [p.id for p in standard_pages({})] == ["index"]


def test_readme_spec_path(tmp_path: Path):
    p = tmp_path / "r.md"
    p.write_text("# x", encoding="utf-8")
    r = ReadmeSpec(markdown=p)
    assert r.blocks == []



def test_catalog_from_rows_rename_does_not_steal_valid_key():
    from portalkit.spec import catalog_from_rows
    rows = [{"id": "1", "a b": 1.0, "a_b": "x"}, {"id": "2", "a b": 2.0, "a_b": "y"}]
    cat = catalog_from_rows(rows, id="id", roles={"a_b": "text"})
    cols = {c.key: c for c in cat.columns}
    assert cols["a_b"].role == "text" and cols["a_b"].label in (None, "")
    assert cols["a_b_2"].role == "value" and cols["a_b_2"].label == "a b"
    assert list(cat.table["a_b"]) == ["x", "y"]
    # the renamed key still works as an override when no field has that name
    cat = catalog_from_rows([{"id": "1", "a b": 1.0}], id="id", roles={"a_b": "mag"},
                            columns={"a_b": {"filter": "JWST/NIRCam.F410M"}})
    assert [(c.key, c.role, c.filter) for c in cat.columns] == [("a_b", "mag", "JWST/NIRCam.F410M")]
