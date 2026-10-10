"""Catalog bundles (pk-catalog/1) and image sets (pk-images/1): sections 5.2-5.5, 5.9-5.11."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from astropy.table import MaskedColumn, Table

from portalkit import schema
from portalkit.bundle import catalog, codec, images
from portalkit.bundle.codec import decode_file, dequant
from portalkit.bundle.order import SRC_INDEX_KEY
from portalkit.bundle.writer import BundleError, BundleWriter, Ledger, StagingError
from portalkit.constants import LOD_MIN_N, ROW_CHUNK

try:
    from portalkit.spec import (CatalogSpec, ColumnSpec, DensitySpec, Group, ImageItem, ImageSetSpec,
                                ManifoldSpec, Preset, VirtualDim)
except ImportError:   # spec.py (WP03) absent: local mirrors of the section 9.1 dataclasses
    @dataclass
    class ColumnSpec:
        key: str
        role: str = "value"
        label: str | None = None
        unit: str = ""
        filter: str | None = None
        err: str | None = None
        limit: str | None = None
        validity: str | None = None
        enc: str = "auto"
        step: float | None = None
        clip: str = "none"
        lo: float | None = None
        hi: float | None = None
        group: str | None = None
        note: str = ""
        log: bool = False
        invert: bool | None = None
        fmt: str | None = None
        levels: list | None = None
        colors: dict | None = None
        default_off: list = field(default_factory=list)
        exact: bool = True

    @dataclass
    class VirtualDim:
        key: str
        label: str | None = None
        unit: str = ""
        group: str | None = None

    @dataclass
    class Group:
        key: str
        label: str
        dims: list

    @dataclass
    class Preset:
        key: str
        label: str
        kind: str
        data: dict

    @dataclass
    class DensitySpec:
        key: str
        x: str
        y: str
        nx: int = 256
        ny: int = 256
        xr: tuple | None = None
        yr: tuple | None = None
        stat: str | None = None

    @dataclass
    class ManifoldSpec:
        dims: list
        frames: dict = field(default_factory=dict)
        default_frame: str = "pca"

    @dataclass
    class CatalogSpec:
        key: str
        table: Any
        id: str
        columns: list
        ra: str | None = None
        dec: str | None = None
        aliases: Any = None
        virtual: list = field(default_factory=list)
        groups: list = field(default_factory=list)
        presets: list = field(default_factory=list)
        density: list = field(default_factory=list)
        manifold: Any = None
        exact_rows: bool | None = None
        context: bool = False
        order: str = "auto"
        title: str = ""
        caveats: list = field(default_factory=list)
        catalogs_provenance: list = field(default_factory=list)
        foreign: Any = None

    @dataclass
    class ImageItem:
        id: str
        roles: dict
        caption: str = ""
        tags: dict = field(default_factory=dict)
        w: int | None = None
        h: int | None = None

    @dataclass
    class ImageSetSpec:
        key: str
        items: list
        roles: list
        facets: list = field(default_factory=list)
        presets: list = field(default_factory=list)
        catalog: str | None = None
        join: str | None = None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def make_table(n: int, seed: int = 0) -> Table:
    rng = np.random.default_rng(seed)
    t = Table()
    t["id"] = np.array([f"J{i:06d}" for i in range(n)], dtype="U12")
    t["ra"] = rng.uniform(266.0, 266.5, n)
    t["dec"] = rng.uniform(-29.2, -28.8, n)
    f444 = rng.uniform(12, 22, n)
    f444[::11] = np.nan
    t["F444W"] = f444
    t["F444W_err"] = 10 ** rng.uniform(-3, -0.5, n)
    t["flux"] = 10 ** rng.uniform(-5, 0, n)
    t["teff"] = rng.uniform(3000, 40000, n)
    t["cls"] = np.array([["star", "yso", "gälaxy"][i % 3] for i in range(n)], dtype="U10")
    t["good"] = (np.arange(n) % 2 == 0)
    t["note"] = np.array([f"nøte {i}" for i in range(n)], dtype="U16")
    t["lim"] = np.array([["", "u", "l", "v"][i % 4] for i in range(n)], dtype="U1")
    return t


def standard_columns() -> list:
    return [
        ColumnSpec("ra", "ra", unit="deg"),
        ColumnSpec("dec", "dec", unit="deg"),
        ColumnSpec("F444W", "mag", unit="mag", err="F444W_err", filter="JWST/NIRCam.F444W", group="mags"),
        ColumnSpec("F444W_err", "error", unit="mag"),
        ColumnSpec("flux", "flux", unit="Jy", limit="lim"),
        ColumnSpec("teff", "value", unit="K", log=True),
        ColumnSpec("cls", "category", colors={"yso": "#f00"}, default_off=["star"]),
        ColumnSpec("good", "flag"),
        ColumnSpec("note", "text"),
        ColumnSpec("lim", "limit"),
    ]


def make_spec(t: Table, **kw) -> CatalogSpec:
    base = dict(key="src", table=t, id="id", columns=standard_columns(), ra="ra", dec="dec",
                title="Test sources",
                presets=[Preset("cmd", "CMD", "axes", {"x": "F444W - flux", "y": "F444W"})])
    base.update(kw)
    return CatalogSpec(**base)


def read_col(root: Path, meta: dict, n: int) -> np.ndarray:
    files = meta.get("chunk_files") or [meta["file"]]
    parts = []
    for i, f in enumerate(files):
        rows = n if len(files) == 1 else None
        parts.append(decode_file(meta, (root / f).read_bytes(), rows))
    if meta["enc"] == "bits" and len(files) > 1:
        raise AssertionError("bits chunks need per-chunk n")
    return np.concatenate(parts) if parts else np.array([])


def col_by_key(man: dict, key: str) -> dict:
    return next(c for c in man["cols"] if c["key"] == key)


def assert_valid(man: dict) -> None:
    errs = schema.validate_catalog(man)
    assert errs == [], errs


# ---------------------------------------------------------------------------
# catalog: sizes N = 0, 1, small
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n", [0, 1, 37])
def test_catalog_small_sizes_validate(tmp_path, n):
    t = make_table(n)
    res = catalog.write(make_spec(t), tmp_path)
    man = json.loads((tmp_path / "manifest.json").read_text("utf-8"))
    assert man == json.loads(json.dumps(res.manifest))
    assert_valid(man)
    assert man["schema"] == "pk-catalog/1" and man["n"] == n
    assert man["order"] == {"kind": "input"}
    for f in res.files:
        assert (tmp_path / f).is_file()
        stem, h, ext = Path(f).name.rsplit(".", 2)
        assert len(h) == 8
    keys = [c["key"] for c in man["cols"]]
    assert "lim" not in keys and "id" not in keys
    assert SRC_INDEX_KEY not in keys
    # text columns live in the row chunks; with no row chunks (N = 0) they are 'str' columns
    assert ("note" in keys) == (n == 0)
    if n:
        assert man["rows"]["chunk"] == ROW_CHUNK and len(man["rows"]["files"]) == math.ceil(n / ROW_CHUNK)
        assert man["pos"]["enc"] == "u32lin" and "bbox" in man
    else:
        assert "rows" not in man and "pos" not in man


def test_catalog_values_roundtrip(tmp_path):
    n = 300
    t = make_table(n, seed=3)
    res = catalog.write(make_spec(t), tmp_path)
    man = res.manifest
    assert_valid(man)
    # mag: fixed step 0.001, invert true, missing preserved
    m = col_by_key(man, "F444W")
    assert m["enc"] == "u16q" and m["step"] == 0.001 and m["invert"] is True
    assert m["filter"] == "JWST/NIRCam.F444W" and m["err"] == "F444W_err"
    v = dequant(m, read_col(tmp_path, m, n))
    ok = np.isfinite(t["F444W"])
    assert np.all(np.isnan(v[~ok]))
    assert np.max(np.abs(v[ok] - t["F444W"][ok])) <= 0.0005 + 1e-9
    assert m["nvalid"] == int(ok.sum()) and len(m["quantiles"]) == 101
    # flux: u16log, relative error within half a step in dex
    f = col_by_key(man, "flux")
    assert f["enc"] == "u16log"
    vf = dequant(f, read_col(tmp_path, f, n))
    assert np.max(np.abs(np.log10(vf) - np.log10(t["flux"]))) <= f["step"] / 2 + 1e-12
    # error: u8log
    e = col_by_key(man, "F444W_err")
    assert e["enc"] == "u8log" and e["lo"] > 0
    # value with log: stats on log10
    tf = col_by_key(man, "teff")
    assert tf["log"] is True and tf["center"] == pytest.approx(np.median(np.log10(t["teff"])), rel=1e-6)
    # category
    c = col_by_key(man, "cls")
    assert c["enc"] == "cat8" and c["levels"] == ["gälaxy", "star", "yso"]
    assert c["colors"] == {"yso": "#f00"} and c["default_off"] == ["star"]
    labels = dequant(c, read_col(tmp_path, c, n))
    assert labels.tolist() == list(t["cls"])
    # flag
    g = col_by_key(man, "good")
    assert read_col(tmp_path, g, n).tolist() == [int(b) for b in t["good"]]
    # positions
    ra, dec = codec.decode_pos(man["pos"], {k: (tmp_path / v).read_bytes()
                                            for k, v in man["pos"]["files"].items()}, man["bbox"])
    assert np.max(np.abs(ra - t["ra"])) < 1e-8 and np.max(np.abs(dec - t["dec"])) < 1e-8
    # ids
    ids = codec.decode_str(codec.gunzip((tmp_path / man["ids"]["file"]).read_bytes()))
    assert ids == list(t["id"]) == res.ids
    assert res.row_of("J000005") == 5


def test_catalog_limits_sparse(tmp_path):
    n = 40
    t = make_table(n)
    res = catalog.write(make_spec(t), tmp_path)
    f = col_by_key(res.manifest, "flux")
    lim = f["limits"]
    idx, kind = codec.decode_sparse(codec.gunzip((tmp_path / lim["file"]).read_bytes()))
    want_u = [i for i in range(n) if i % 4 == 1]
    want_l = [i for i in range(n) if i % 4 == 2]
    assert idx[kind == 1].tolist() == want_u and idx[kind == 2].tolist() == want_l
    assert lim["count"] == len(idx)


def test_catalog_clip_saturate_limits_and_bounds(tmp_path):
    t = Table({"id": ["a", "b", "c", "d"], "x": [-1.0, 0.5, 0.7, 9.0]})
    spec = CatalogSpec(key="c", table=t, id="id",
                       columns=[ColumnSpec("x", "value", clip="saturate", lo=0.0, hi=1.0)])
    res = catalog.write(spec, tmp_path)
    x = col_by_key(res.manifest, "x")
    assert x["clip"] == "saturate" and x["lo"] == 0.0 and x["hi"] == 1.0
    idx, kind = codec.decode_sparse(codec.gunzip((tmp_path / x["limits"]["file"]).read_bytes()))
    assert dict(zip(idx.tolist(), kind.tolist())) == {0: 3, 3: 4}
    assert_valid(res.manifest)


def test_catalog_row_chunks_content(tmp_path):
    n = ROW_CHUNK + 5
    t = make_table(n)
    t["F444W"] = MaskedColumn(t["F444W"], mask=np.isnan(t["F444W"]))
    res = catalog.write(make_spec(t), tmp_path)
    rows = res.manifest["rows"]
    assert len(rows["files"]) == 2
    assert rows["cols"][:3] == ["id", "ra", "dec"]
    assert "note" in rows["cols"] and "lim" in rows["cols"]
    last = json.loads(codec.gunzip((tmp_path / rows["files"][1]).read_bytes()))
    assert len(last) == 5
    r = last[-1]
    i = n - 1
    assert r["id"] == t["id"][i] and r["note"] == t["note"][i]
    assert r["ra"] == float(t["ra"][i])
    first = json.loads(codec.gunzip((tmp_path / rows["files"][0]).read_bytes()))
    assert first[0]["F444W"] is None          # masked / NaN -> null
    assert first[1]["F444W"] == float(t["F444W"][1])


def test_catalog_no_rows_stores_text_as_str(tmp_path):
    t = make_table(12)
    res = catalog.write(make_spec(t, exact_rows=False), tmp_path)
    man = res.manifest
    assert "rows" not in man
    note = col_by_key(man, "note")
    assert note["enc"] == "str"
    assert read_col(tmp_path, note, 12).tolist() == list(t["note"])
    assert_valid(man)


def test_catalog_exact_false_column_not_in_rows(tmp_path):
    t = make_table(5)
    cols = standard_columns()
    cols[5] = ColumnSpec("teff", "value", exact=False)
    res = catalog.write(make_spec(t, columns=cols), tmp_path)
    assert "teff" not in res.manifest["rows"]["cols"]
    assert "teff" in [c["key"] for c in res.manifest["cols"]]


# ---------------------------------------------------------------------------
# LOD order, chunking
# ---------------------------------------------------------------------------

def test_catalog_lod_order_src_index_and_i16off(tmp_path):
    n = LOD_MIN_N + 1000
    rng = np.random.default_rng(2)
    t = Table()
    t["id"] = np.arange(n)
    t["ra"] = rng.uniform(266.0, 266.6, n)
    t["dec"] = rng.uniform(-29.3, -28.7, n)
    t["m"] = rng.uniform(10, 25, n)
    spec = CatalogSpec(key="big", table=t, id="id", ra="ra", dec="dec", exact_rows=False,
                       columns=[ColumnSpec("m", "mag")],
                       presets=[Preset("p", "P", "axes", {"x": "m", "y": "m"})])
    res = catalog.write(spec, tmp_path)
    man = res.manifest
    assert_valid(man)
    assert man["order"]["kind"] == "lod" and man["order"]["levels"] == [32768, 65536, 131072, n]
    assert man["order"]["sort"] == "tile-morton"
    assert man["pos"]["enc"] == "i16off" and man["pos"]["step_mas"] > 0
    src = col_by_key(man, SRC_INDEX_KEY)
    s2i = read_col(tmp_path, src, n).astype(np.int64)
    assert np.array_equal(np.sort(s2i), np.arange(n))
    np.testing.assert_array_equal(s2i, res.order.stored_to_input)
    ids = codec.decode_str(codec.gunzip((tmp_path / man["ids"]["file"]).read_bytes()))
    assert ids == [str(i) for i in s2i]
    m = col_by_key(man, "m")
    v = dequant(m, read_col(tmp_path, m, n))
    assert np.max(np.abs(v - t["m"][s2i])) <= 0.0005 + 1e-9
    ra, dec = codec.decode_pos(man["pos"], {k: (tmp_path / f).read_bytes() for k, f in man["pos"]["files"].items()})
    sep = np.hypot((ra - t["ra"][s2i]) * np.cos(np.radians(dec)), dec - t["dec"][s2i])
    assert sep.max() <= man["pos"]["step_mas"] / 3.6e6
    # n > T0_MAX: automatic density grid for the axes preset, first paint uses it
    assert [d["key"] for d in man["density"]] == ["p"]
    assert man["budget"]["first_paint_bytes"] < man["budget"]["total_bytes"]


def test_catalog_chunked_columns_and_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "CHUNK_ROWS", 10)
    t = make_table(25)
    res = catalog.write(make_spec(t), tmp_path)
    man = res.manifest
    assert man["chunk_rows"] == 10
    assert_valid(man)
    m = col_by_key(man, "F444W")
    assert "file" not in m and len(m["chunk_files"]) == 3
    assert all(".c00" in Path(f).name for f in m["chunk_files"])
    v = dequant(m, np.concatenate([decode_file(m, (tmp_path / f).read_bytes()) for f in m["chunk_files"]]))
    ok = np.isfinite(t["F444W"])
    assert np.max(np.abs(v[ok] - t["F444W"][ok])) <= 0.0005 + 1e-9
    assert man["ids"]["file"] is None and len(man["ids"]["chunks"]) == 3
    ids = sum((codec.decode_str(codec.gunzip((tmp_path / f).read_bytes())) for f in man["ids"]["chunks"]), [])
    assert ids == list(t["id"])
    g = col_by_key(man, "good")
    bits = [decode_file(g, (tmp_path / f).read_bytes(), 10) for f in g["chunk_files"]]
    assert np.concatenate(bits)[:25].tolist() == [int(b) for b in t["good"]]


# ---------------------------------------------------------------------------
# ids, aliases, errors
# ---------------------------------------------------------------------------

def test_catalog_aliases_dict_and_column(tmp_path):
    t = make_table(4)
    al = {"J000000": ["Sgr A*", "J000001"], "J000001": "shared", "J000002": ["shared", "α Cen"]}
    res = catalog.write(make_spec(t, aliases=al), tmp_path / "d")
    assert res.alias_to_row == {"Sgr A*": 0, "α Cen": 2}
    assert any("equals another id" in w for w in res.warnings)
    assert any("several rows" in w for w in res.warnings)
    stored = json.loads((tmp_path / "d" / res.manifest["ids"]["alias_file"]).read_text("utf-8"))
    assert stored == res.alias_to_row
    assert res.row_of("α Cen") == 2 and res.row_of("nope") is None
    t["aka"] = ["a1|a2", "", "b1", "a1"]
    res2 = catalog.write(make_spec(t, aliases="aka"), tmp_path / "c")
    assert res2.alias_to_row == {"a2": 0, "b1": 2}
    assert_valid(res2.manifest)


def test_catalog_id_errors(tmp_path):
    t = make_table(3)
    t["id"][2] = t["id"][0]
    with pytest.raises(BundleError, match="duplicate"):
        catalog.write(make_spec(t), tmp_path)
    t2 = make_table(3)
    t2["id"] = MaskedColumn(t2["id"], mask=[False, True, False])
    with pytest.raises(BundleError, match="missing id"):
        catalog.write(make_spec(t2), tmp_path)
    t3 = make_table(3)
    t3["bad key"] = [1.0, 2.0, 3.0]
    with pytest.raises(BundleError, match="must match"):
        catalog.write(make_spec(t3, columns=standard_columns() + [ColumnSpec("bad key")]), tmp_path)
    with pytest.raises(BundleError):
        catalog.write(make_spec(make_table(3), columns=[ColumnSpec("nope")]), tmp_path)
    with pytest.raises(BundleError):
        catalog.write(make_spec(None), tmp_path)


def test_catalog_numeric_and_bytes_ids(tmp_path):
    t = Table({"id": np.array([3, 1, 2]), "x": [1.0, 2.0, 3.0]})
    res = catalog.write(CatalogSpec(key="n", table=t, id="id", columns=[ColumnSpec("x")]), tmp_path)
    assert res.ids == ["3", "1", "2"]
    assert_valid(res.manifest)


def test_catalog_all_missing_and_inf_column(tmp_path):
    t = Table({"id": ["a", "b"], "x": [np.nan, np.inf], "y": [1e300, -1e300]})
    res = catalog.write(CatalogSpec(key="m", table=t, id="id",
                                    columns=[ColumnSpec("x"), ColumnSpec("y")]), tmp_path)
    x = col_by_key(res.manifest, "x")
    assert x["nvalid"] == 0 and x["center"] == 0.0 and x["scale"] == 1.0 and "quantiles" not in x
    y = col_by_key(res.manifest, "y")
    assert y["nvalid"] == 2
    assert_valid(res.manifest)


def test_catalog_category_auto_cat16_above_255_levels(tmp_path):
    n = 600
    t = make_table(n, seed=4)
    t["cls"] = np.array([f"L{i % 300:03d}" for i in range(n)], dtype="U8")
    t["cls"][7] = ""
    cols = [c for c in standard_columns() if c.key != "cls"] + [ColumnSpec("cls", "category")]
    res = catalog.write(make_spec(t, columns=cols), tmp_path)
    assert_valid(res.manifest)
    c = col_by_key(res.manifest, "cls")
    assert c["enc"] == "cat16" and len(c["levels"]) == 300 and c["shuffle"] is True
    codes = read_col(tmp_path, c, n)
    lab = [None if k == codec.CAT16_MISSING else c["levels"][k] for k in codes]
    assert lab[7] is None and c["nvalid"] == n - 1
    assert all(lab[i] == t["cls"][i] for i in range(n) if i != 7)
    # an explicit cat8 with too many levels is a BundleError, not a bare ValueError
    cols8 = [c for c in standard_columns() if c.key != "cls"] + [ColumnSpec("cls", "category", enc="cat8")]
    with pytest.raises(BundleError):
        catalog.write(make_spec(t, columns=cols8), tmp_path / "c8")


def test_catalog_u32lin_half_missing_position_is_missing(tmp_path):
    t = Table()
    t["id"] = ["a", "b", "c", "d"]
    t["ra"] = [10.0, 11.0, 200.0, np.nan]
    t["dec"] = [5.0, 6.0, np.nan, 7.0]
    spec = CatalogSpec(key="p", table=t, id="id", ra="ra", dec="dec",
                       columns=[ColumnSpec("ra", "ra"), ColumnSpec("dec", "dec")])
    res = catalog.write(spec, tmp_path)
    man = res.manifest
    assert_valid(man)
    assert man["pos"]["enc"] == "u32lin" and man["bbox"]["ra"] == [10.0, 11.0]
    ra, dec = codec.decode_pos(man["pos"], {k: (tmp_path / f).read_bytes()
                                            for k, f in man["pos"]["files"].items()}, man["bbox"])
    np.testing.assert_allclose(ra[:2], [10.0, 11.0], atol=1e-8)
    np.testing.assert_allclose(dec[:2], [5.0, 6.0], atol=1e-8)
    assert np.all(np.isnan(ra[2:])) and np.all(np.isnan(dec[2:]))


def test_catalog_all_sky_i16off_manifest_within_budget(tmp_path):
    from portalkit.constants import MANIFEST_MAX
    n = LOD_MIN_N + 8000
    rng = np.random.default_rng(21)
    t = Table()
    t["id"] = np.arange(n)
    t["ra"] = rng.uniform(0, 360, n)
    t["dec"] = np.degrees(np.arcsin(rng.uniform(-1, 1, n)))
    ledger = Ledger()
    spec = CatalogSpec(key="sky", table=t, id="id", ra="ra", dec="dec", exact_rows=False,
                       columns=[])
    res = catalog.write(spec, tmp_path, ledger=ledger)
    man = res.manifest
    assert_valid(man)
    assert man["pos"]["enc"] == "i16off"
    assert len(man["pos"]["tile_refs"]) <= codec.I16OFF_MAX_TILES
    assert (tmp_path / "manifest.json").stat().st_size < MANIFEST_MAX
    assert any("step_mas raised" in w for w in res.warnings)
    assert ledger.check()[0] == []
    ra, dec = codec.decode_pos(man["pos"], {k: (tmp_path / f).read_bytes()
                                            for k, f in man["pos"]["files"].items()})
    s2i = res.order.stored_to_input
    s = man["pos"]["step_mas"] / 3.6e6
    tile = np.frombuffer(codec.unshuffle(codec.gunzip(
        (tmp_path / man["pos"]["files"]["tile"]).read_bytes()), 2), dtype="<u2")
    dec0 = np.asarray(man["pos"]["tile_refs"])[tile, 1]
    # GC i16off quantization: half a step per axis, RA offsets measured with cos(dec0)
    dra = (ra - t["ra"][s2i] + 180.0) % 360.0 - 180.0
    assert np.max(np.abs(dra) * np.cos(np.radians(dec0))) <= 0.5 * s * (1 + 1e-6)
    assert np.max(np.abs(dec - t["dec"][s2i])) <= 0.5 * s * (1 + 1e-6)


def test_catalog_value_f32_when_not_exact_gpu(tmp_path):
    t = Table({"id": ["a", "b", "c"], "x": [1e-3, 1.0, 1e5]})
    spec = CatalogSpec(key="f", table=t, id="id", columns=[ColumnSpec("x")])
    spec.exact_gpu = False
    res = catalog.write(spec, tmp_path)
    x = col_by_key(res.manifest, "x")
    assert x["enc"] == "f32" and x["shuffle"] is True
    assert read_col(tmp_path, x, 3).tolist() == pytest.approx([1e-3, 1.0, 1e5], rel=1e-7)


@pytest.mark.parametrize("cat_gpu, col_gpu, enc", [
    (True, None, "u16q"), (False, None, "f32"), (True, False, "f32"), (False, True, "u16q")])
def test_catalog_exact_gpu_column_overrides_catalog(tmp_path, cat_gpu, col_gpu, enc):
    t = Table({"id": ["a", "b", "c"], "x": [1e-3, 1.0, 1e5]})
    spec = CatalogSpec(key="f", table=t, id="id", exact_gpu=cat_gpu,
                       columns=[ColumnSpec("x", exact_gpu=col_gpu)])
    res = catalog.write(spec, tmp_path)
    assert col_by_key(res.manifest, "x")["enc"] == enc


# ---------------------------------------------------------------------------
# presets, density, manifold, budget, staging
# ---------------------------------------------------------------------------

def test_catalog_presets_density_manifold(tmp_path):
    t = make_table(200)
    spec = make_spec(
        t, context=True,
        virtual=[VirtualDim("c1", "F444W - flux", unit="mag", group="colors")],
        groups=[Group("mags", "Magnitudes", ["F444W", "c1"])],
        presets=[Preset("cmd", "CMD", "axes", {"x": "F444W - flux", "y": "F444W"}),
                 Preset("bright", "Bright", "selection", {"sel": "F444W<15"}),
                 Preset("f", "Filter", "filter", {"filters": {"cls": ["yso"]}})],
        density=[DensitySpec("teffmap", "F444W", "flux", nx=8, ny=4, stat="median:teff"),
                 DensitySpec("bad", "zz", "F444W")],
        manifold=ManifoldSpec(dims=["F444W", "teff"], frames={"pca": [1, 0, 0, 1]}),
        catalogs_provenance=[{"name": "test", "url": "https://example.org"}],
        caveats=["synthetic"])
    res = catalog.write(spec, tmp_path, build="20261010T120000Z-0123abcd",
                        links={"images": "../img/images.json"})
    man = res.manifest
    assert_valid(man)
    keys = [d["key"] for d in man["density"]]
    assert keys == ["teffmap", "cmd"]           # 'bad' skipped with a warning, 'cmd' auto (context)
    assert any("'bad'" in w for w in res.warnings)
    tm = man["density"][0]
    assert tm["enc"] == "f32" and tm["stat"] == "median" and tm["value"] == "teff"
    assert man["presets"][2] == {"key": "f", "label": "Filter", "kind": "filter", "filters": {"cls": ["yso"]}}
    assert man["manifold"] == {"dims": ["F444W", "teff"], "default_frame": "pca", "fade_width": 0.12,
                               "frames": {"pca": [1.0, 0.0, 0.0, 1.0]}}
    assert man["provenance"]["catalogs"][0]["name"] == "test"
    assert man["build"] == "20261010T120000Z-0123abcd" and man["caveats"] == ["synthetic"]


def test_catalog_budget_and_ledger(tmp_path):
    t = make_table(150)
    ledger = Ledger()
    res = catalog.write(make_spec(t), tmp_path, ledger=ledger)
    man = res.manifest
    on_disk = sum((tmp_path / f).stat().st_size for f in res.files)
    msize = (tmp_path / "manifest.json").stat().st_size
    assert man["budget"]["total_bytes"] == on_disk + msize
    assert man["budget"]["first_paint_bytes"] <= man["budget"]["total_bytes"]
    assert ledger.total("src") == on_disk + msize
    assert ledger.first_paint("src") == man["budget"]["first_paint_bytes"]
    errs, _ = ledger.check()
    assert errs == []


def test_catalog_deterministic_and_content_hash(tmp_path):
    t = make_table(60)
    r1 = catalog.write(make_spec(t), tmp_path / "a")
    r2 = catalog.write(make_spec(t), tmp_path / "b")
    assert (tmp_path / "a" / "manifest.json").read_bytes() == (tmp_path / "b" / "manifest.json").read_bytes()
    import hashlib
    for f in r1.files:
        data = (tmp_path / "a" / f).read_bytes()
        assert Path(f).name.rsplit(".", 2)[1] == hashlib.sha256(data).hexdigest()[:8]
    assert r1.files == r2.files


def test_catalog_writer_refuses_escape(tmp_path):
    w = BundleWriter(tmp_path / "stage", "data/src")
    with pytest.raises(StagingError):
        w.put("../../..", "x", b"1")
    res = catalog.write(make_spec(make_table(3)), w)
    assert (tmp_path / "stage" / "data" / "src" / "manifest.json").is_file()
    assert all(not f.startswith("data/") for f in res.files)    # relative to the dataset dir


# ---------------------------------------------------------------------------
# images.json
# ---------------------------------------------------------------------------

def _image_set(n: int = 6, **kw) -> ImageSetSpec:
    items = [ImageItem(id=f"J{i:06d}", roles={"rgb": f"img/{i}.png", "sed": f"sed/{i}.png" if i % 2 else ""},
                       caption=f"src {i} — é", tags={"band": ["K", "H"][i % 2], "snr": str(i % 3 * 10)},
                       w=640, h=480) for i in range(n)]
    base = dict(key="img", items=items, roles=[("rgb", "RGB"), ("sed", "SED")],
                facets=[("band", "Band"), ("snr", "S/N")],
                presets=[Preset("k", "K only", "filter", {"facets": {"band": ["K", "Z"]}})], catalog="src")
    base.update(kw)
    return ImageSetSpec(**base)


def test_images_document_join_and_thumbs(tmp_path):
    t = make_table(4)
    cat = catalog.write(make_spec(t, aliases={"J000003": ["alias3"]}), tmp_path / "src")
    spec = _image_set(6)
    spec.items[3].id = "alias3"
    res = images.write(spec, tmp_path / "img", rows=cat.row_of,
                       thumb=lambda url, iid, role: url.replace(".png", ".thumb.jpg") if role == "rgb" else None)
    doc = json.loads((tmp_path / "img" / "images.json").read_text("utf-8"))
    assert schema.validate_images(doc) == []
    assert doc["schema"] == "pk-images/1" and doc["catalog"] == "src"
    by = {it["id"]: it for it in doc["items"]}
    assert by["J000000"]["row"] == 0 and by["alias3"]["row"] == 3
    assert "row" not in by["J000004"] and set(res.unmatched) == {"J000004", "J000005"}
    assert by["J000000"]["roles"] == {"rgb": {"url": "img/0.png", "thumb": "img/0.thumb.jpg",
                                              "w": 640, "h": 480}}
    assert by["J000001"]["roles"]["sed"] == {"url": "sed/1.png", "w": 640, "h": 480}
    assert by["J000000"]["roles"]["rgb"]["w"] == 640 and "w" not in by["J000000"] and by["J000000"]["caption"].endswith("é")
    assert doc["facets"] == [{"key": "band", "label": "Band", "values": ["H", "K"]},
                             {"key": "snr", "label": "S/N", "values": ["0", "10", "20"]}]
    assert doc["presets"] == [{"key": "k", "label": "K only", "facets": {"band": ["K"]}}]
    assert any("not present" in w for w in res.warnings)


def test_images_empty_and_errors(tmp_path):
    res = images.write(_image_set(0), tmp_path)
    assert res.doc["items"] == [] and schema.validate_images(res.doc) == []
    dup = _image_set(2)
    dup.items[1].id = dup.items[0].id
    with pytest.raises(BundleError):
        images.write(dup, tmp_path)
    noimg = _image_set(1)
    noimg.items[0].roles = {"rgb": ""}
    r = images.write(noimg, tmp_path / "n")
    assert r.doc["items"] == [] and any("no image URL" in w for w in r.warnings)


def test_images_pagination(tmp_path):
    rng = np.random.default_rng(0)
    items = [ImageItem(id=f"i{i}", roles={"rgb": f"img/{rng.integers(1 << 60):x}/{i}.png"},
                       tags={"band": "K"}) for i in range(3000)]
    spec = _image_set(0, items=items)
    res = images.write(spec, tmp_path, page_max=20_000)
    doc = json.loads((tmp_path / "images.json").read_text("utf-8"))
    assert doc["items"] == [] and len(doc["pages"]) >= 2
    assert schema.validate_images(doc) == []
    got = []
    for p in doc["pages"]:
        assert Path(p).name.startswith("images.")
        data = (tmp_path / p).read_bytes()
        assert len(codec.gz(data)) <= 20_000
        got += json.loads(data)["items"]
    assert [g["id"] for g in got] == [f"i{i}" for i in range(3000)]
    assert res.pages == doc["pages"]


# ---------------------------------------------------------------------------
# writer: naming, atomic writes, staging guard, ledger budgets
# ---------------------------------------------------------------------------

def test_writer_hashed_names_and_json(tmp_path):
    from portalkit.bundle import writer
    assert writer.hashed_name("ids", b"abc") == "ids." + writer.h8(b"abc") + ".bin"
    assert writer.hashed_name("", b"abc") == writer.h8(b"abc") + ".bin"
    assert writer.dumps_json({"a": np.float32(1.5), "b": [np.nan, np.inf], "c": np.int64(3),
                              "d": "é", "e": np.bool_(True)}) == \
        '{"a":1.5,"b":[null,null],"c":3,"d":"é","e":true}'.encode("utf-8")
    with pytest.raises(BundleError):
        writer.dumps_json({"x": object()})
    w = BundleWriter(tmp_path, "data/x")
    p1 = w.put("cols", "a", b"1")
    p2 = w.put("cols", "a", b"1")
    assert p1 == p2 and len(w.ledger.entries) == 1
    w.put_stable("manifest.json", b"{}", "manifest")
    w.put_stable("manifest.json", b'{"a":1}', "manifest")
    assert (tmp_path / "data/x/manifest.json").read_bytes() == b'{"a":1}'
    assert not [f for f in (tmp_path / "data/x").iterdir() if f.name.startswith(".")]


def test_writer_refuses_symlinks_and_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    stage = tmp_path / "stage"
    (stage / "data").mkdir(parents=True)
    (stage / "data" / "link").symlink_to(outside)
    w = BundleWriter(stage)
    with pytest.raises(StagingError):
        w.put("data/link", "x", b"1")
    with pytest.raises(StagingError):
        BundleWriter(stage, "data/link")
    with pytest.raises(StagingError):
        w.put_stable("../x.json", b"1")
    with pytest.raises(StagingError):
        w.put_stable("", b"1")
    assert not any(outside.iterdir())


def test_ledger_budgets():
    led = Ledger()
    led.add("a/manifest.json", 300_000, "manifest", "a")
    led.add("a/rows/r0.bin", 600_000, "rows", "a")
    led.add("a/s0.bin", 100, "shard", "a", first_paint=True)
    errs, soft = led.check()
    assert len(errs) == 1 and "manifest" in errs[0]
    assert len(soft) == 1 and "rows" in soft[0]
    d = led.to_dict()
    assert d["datasets"]["a"]["first_paint_bytes"] == 100 and d["total_bytes"] == 900_100
    led.add("a/s0.bin", 50, "shard", "a")
    assert led.total("a") == 900_050


# ---------------------------------------------------------------------------
# perf (opt-in): 5M rows
# ---------------------------------------------------------------------------

@pytest.mark.perf
def test_catalog_5m_rows_perf(tmp_path):
    import time
    n = 5_000_000
    rng = np.random.default_rng(0)
    t = Table()
    t["id"] = np.arange(n)
    t["ra"] = rng.uniform(266.0, 267.0, n)
    t["dec"] = rng.uniform(-29.5, -28.5, n)
    t["m"] = rng.uniform(12, 26, n)
    t["c"] = rng.normal(1, 0.5, n)
    spec = CatalogSpec(key="big", table=t, id="id", ra="ra", dec="dec", exact_rows=False,
                       columns=[ColumnSpec("m", "mag"), ColumnSpec("c", "color")],
                       presets=[Preset("cmd", "CMD", "axes", {"x": "c", "y": "m"})])
    t0 = time.perf_counter()
    res = catalog.write(spec, tmp_path)
    dt = time.perf_counter() - t0
    man = res.manifest
    assert_valid(man)
    assert man["order"]["levels"][-1] == n and len(col_by_key(man, "m")["chunk_files"]) == 5
    errs, _ = res.writer.ledger.check()
    assert errs == []
    assert dt < 300, f"5M-row catalog took {dt:.0f} s"
