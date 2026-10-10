"""Record sets: pks1 / pki1 / json / sedcurve, shards and index (sections 5.6-5.8)."""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
import pytest

from portalkit import schema
from portalkit.bundle import codec, records
from portalkit.bundle.writer import BundleError, Ledger
from portalkit.constants import SHARD_MAX, SHARD_TARGET

try:
    from portalkit.spec import Image2D, RecordSetSpec, Spectrum
except ImportError:   # spec.py (WP03) absent: local mirrors of the section 9.1 dataclasses
    @dataclass
    class Spectrum:
        wave: np.ndarray
        channels: dict
        units: dict = field(default_factory=lambda: {"flux": "Jy"})
        segments: list = field(default_factory=list)
        mask_runs: list = field(default_factory=list)
        mask_bits: dict = field(default_factory=dict)
        lines: list = field(default_factory=list)
        meta: dict = field(default_factory=dict)

    @dataclass
    class Image2D:
        data: np.ndarray
        wave: np.ndarray
        y: np.ndarray
        stretch: str = "asinh"
        segments: list = field(default_factory=list)

    @dataclass
    class RecordSetSpec:
        key: str
        kind: str
        catalog: str
        ids: list
        loader: Callable
        enc: dict = field(default_factory=dict)
        grids: dict = field(default_factory=dict)
        png: Callable | None = None
        layout: str = "shard"
        legacy_json: tuple | None = None


def _spec(i: int, n: int = 50, rng=None) -> Spectrum:
    rng = rng or np.random.default_rng(i)
    w = np.linspace(1.0, 5.0, n)
    f = 10 ** rng.uniform(-4, -1, n)
    return Spectrum(wave=w, channels={"flux": f, "err": f * 0.1},
                    segments=[(0, n // 2, "G235M"), (n // 2, n, "G395M")])


def _read_entry(root: Path, index: dict, rid: str) -> bytes:
    ent = index["entries"][rid]
    if isinstance(ent, str):
        return (root / ent).read_bytes()
    sh, off, ln = ent
    return (root / index["shards"][sh]).read_bytes()[off:off + ln]


# ---------------------------------------------------------------------------
# record encodings
# ---------------------------------------------------------------------------

def test_pks1_layout_and_roundtrip():
    s = Spectrum(wave=np.linspace(1, 2, 7), channels={"flux": np.array([1, 2, np.nan, 4, 5, 6, 7.0]) * 1e-3,
                                                     "err": np.full(7, 1e-4)},
                 units={"flux": "Jy", "err": "Jy"}, segments=[(0, 3, "a"), (3, 7, "b")],
                 mask_runs=[(2, 3, 1)], mask_bits={1: "sat"}, lines=[{"name": "Brα", "w": 4.05}],
                 meta={"t": "é"})
    raw = records.encode_pks1("xé", s, enc={"err": "u16q"})
    assert raw[:4] == b"PKS1"
    hl = struct.unpack_from("<I", raw, 4)[0]
    assert hl % 4 == 0
    hdr = json.loads(raw[8:8 + hl])
    assert schema.validate_pks1_header(hdr) == []
    assert hdr["wave"] == {"kind": "uniform", "w0": 1.0, "dw": pytest.approx(1 / 6)}
    assert [c["enc"] for c in hdr["channels"]] == ["f32", "u16q"]
    assert all(c["off"] % 4 == 0 for c in hdr["channels"])
    d = records.decode_pks1(codec.gz(raw))
    np.testing.assert_allclose(d["wave"], s.wave)
    f = d["channels"]["flux"]
    assert np.isnan(f[2])
    np.testing.assert_allclose(f[np.isfinite(f)], s.channels["flux"][np.isfinite(s.channels["flux"])], rtol=1e-6)
    np.testing.assert_allclose(d["channels"]["err"], 1e-4, rtol=1e-6)
    assert d["header"]["mask_bits"] == {"1": "sat"}
    assert d["id"] == "xé"


def test_pks1_wave_array_f32_f64_and_shared():
    w = np.array([0.6, 0.61, 0.65, 0.8, 1.2, 5.3])
    s = Spectrum(wave=w, channels={"flux": np.ones(6)})
    hdr = records.split_record(records.encode_pks1("a", s))[1]
    assert hdr["wave"]["kind"] == "array" and hdr["wave"]["dtype"] == "f32"
    # spacing below f32 resolution -> f64
    w2 = 3.0 + np.cumsum(np.r_[0, np.full(5, 1e-8)]) + np.r_[0, 0, 1e-9, 0, 0, 0]
    s2 = Spectrum(wave=w2, channels={"flux": np.ones(6)})
    raw2 = records.encode_pks1("b", s2)
    h2 = records.split_record(raw2)[1]
    assert h2["wave"]["dtype"] == "f64"
    np.testing.assert_array_equal(records.decode_pks1(raw2)["wave"], w2)
    # shared grid
    g = {"g1": w}
    raw3 = records.encode_pks1("c", s, grids=g)
    h3 = records.split_record(raw3)[1]
    assert h3["wave"] == {"kind": "shared", "grid": "g1"}
    assert schema.validate_pks1_header(h3, grids=g) == []
    np.testing.assert_allclose(records.decode_pks1(raw3, grids=g)["wave"], w)
    with pytest.raises(ValueError):
        records.decode_pks1(raw3)


def test_pks1_u16log_and_edge_cases():
    f = np.array([1e-6, 1e-3, 0.0, np.nan, 1.0])
    raw = records.encode_pks1("z", Spectrum(wave=np.arange(5.0) + 1, channels={"flux": f}),
                              enc={"flux": "u16log"})
    v = records.decode_pks1(raw)["channels"]["flux"]
    assert np.isnan(v[2]) and np.isnan(v[3])
    np.testing.assert_allclose(v[[0, 1, 4]], f[[0, 1, 4]], rtol=2e-4)
    one = records.decode_pks1(records.encode_pks1("o", Spectrum(wave=np.array([2.0]),
                                                              channels={"flux": np.array([3.0])})))
    assert one["wave"].tolist() == [2.0] and one["channels"]["flux"].tolist() == [3.0]
    with pytest.raises(BundleError):
        records.encode_pks1("bad", Spectrum(wave=np.arange(3.0), channels={"err": np.ones(3)}))
    with pytest.raises(BundleError):
        records.encode_pks1("bad", Spectrum(wave=np.arange(3.0), channels={"flux": np.ones(4)}))
    with pytest.raises(BundleError):
        records.encode_pks1("bad", Spectrum(wave=np.arange(3.0), channels={"flux": np.ones(3)}),
                            enc={"flux": "u8q"})
    with pytest.raises(ValueError):
        records.decode_pks1(b"PKI1" + struct.pack("<I", 4) + b"{}  ")


def test_pki1_roundtrip_u8_u16_flip():
    data = np.arange(20, dtype=float).reshape(4, 5)
    data[2, 3] = np.nan
    img = Image2D(data=data, wave=np.linspace(1, 2, 5), y=np.array([3.0, 2.0, 1.0, 0.0]))
    raw = records.encode_pki1("im", img, enc="u16", lo=0.0, hi=19.0)
    assert raw[:4] == b"PKI1"
    hdr = records.split_record(raw)[1]
    assert schema.validate_pki1_header(hdr) == []
    assert hdr["y"] == {"kind": "uniform", "y0": 0.0, "dy": 1.0}
    d = records.decode_pki1(raw)
    flipped = data[::-1]
    ok = np.isfinite(flipped)
    assert d["codes"][~ok].tolist() == [0]
    np.testing.assert_allclose(d["values"][ok], flipped[ok], atol=19 * 2e-4)
    raw8 = records.encode_pki1("im8", Image2D(data=data, wave=img.wave, y=img.y, stretch="linear"),
                               enc="u8", lo=0.0, hi=19.0)
    d8 = records.decode_pki1(raw8)
    assert d8["codes"].dtype == np.uint8 and d8["codes"].max() == 255
    np.testing.assert_allclose(d8["values"][ok], flipped[ok], atol=19 / 255)


def test_pki1_defaults_nonuniform_wave_and_errors():
    rng = np.random.default_rng(0)
    data = rng.normal(size=(3, 6))
    w = np.array([1.0, 1.1, 1.3, 1.6, 2.0, 2.5])
    raw = records.encode_pki1("a", Image2D(data=data, wave=w, y=np.arange(3.0)))
    hdr = records.split_record(raw)[1]
    assert hdr["wave"]["kind"] == "array" and hdr["wave"]["off"] % 4 == 0
    assert hdr["stretch"]["kind"] == "asinh" and hdr["stretch"]["a"] == 0.1
    assert hdr["stretch"]["lo"] == pytest.approx(np.percentile(data, 1))
    assert schema.validate_pki1_header(hdr) == []
    np.testing.assert_allclose(records.decode_pki1(raw)["wave"], w, rtol=1e-6)
    const = records.encode_pki1("c", Image2D(data=np.ones((1, 1)), wave=np.array([1.0]), y=np.array([0.0])))
    assert schema.validate_pki1_header(records.split_record(const)[1]) == []
    allnan = records.encode_pki1("n", Image2D(data=np.full((2, 2), np.nan), wave=np.array([1.0, 2.0]),
                                             y=np.array([0.0, 1.0])), enc="u16")
    assert not records.decode_pki1(allnan)["codes"].any()
    with pytest.raises(BundleError):
        records.encode_pki1("e", Image2D(data=np.ones((2, 3)), wave=np.ones(2), y=np.ones(2)))
    with pytest.raises(BundleError):
        records.encode_pki1("e", Image2D(data=np.ones(3), wave=np.ones(3), y=np.ones(1)))
    with pytest.raises(BundleError):
        records.encode_pki1("e", Image2D(data=np.ones((1, 1)), wave=np.ones(1), y=np.ones(1)), enc="f32")


def test_f64_arrays_are_8_byte_aligned_in_record():
    """Float64Array views need an 8-aligned absolute offset in the inflated record."""
    w = 3.0 + np.cumsum(np.r_[0, 1e-8, 1e-8 + 1e-9])        # non-uniform, below f32 resolution
    for rid in ("a", "ab", "abc", "abcd"):
        raw = records.encode_pki1(rid, Image2D(data=np.arange(9.0).reshape(3, 3), wave=w,
                                               y=np.arange(3.0)))           # 9 u8 codes first
        hl = struct.unpack_from("<I", raw, 4)[0]
        hdr = records.split_record(raw)[1]
        assert hl % 8 == 0 and hdr["wave"]["dtype"] == "f64"
        assert hdr["wave"]["off"] % 8 == 0 and (8 + hl + hdr["wave"]["off"]) % 8 == 0
        assert schema.validate_pki1_header(hdr) == []
        np.testing.assert_array_equal(records.decode_pki1(raw)["wave"], w)
        s = Spectrum(wave=w, channels={"flux": np.ones(3)})
        raw1 = records.encode_pks1(rid, s)
        hl1 = struct.unpack_from("<I", raw1, 4)[0]
        h1 = records.split_record(raw1)[1]
        assert hl1 % 8 == 0 and (8 + hl1 + h1["wave"]["off"]) % 8 == 0
        assert all(c["off"] % 4 == 0 for c in h1["channels"])
        np.testing.assert_array_equal(records.decode_pks1(raw1)["wave"], w)


# ---------------------------------------------------------------------------
# shard packer
# ---------------------------------------------------------------------------

def test_shard_packer_bounds():
    p = records.ShardPacker(target=100, max_size=250)
    ents = [p.add(b"x" * 40) for _ in range(5)]
    assert ents[0] == [0, 0, 40] and ents[1] == [0, 40, 40] and ents[2] == [1, 0, 40]
    big = p.add(b"y" * 200)            # > target, <= max: own shard
    assert big == [3, 0, 200]
    assert p.add(b"z" * 251) is None   # > max: layout file
    shards = p.finish()
    assert [len(s) for s in shards] == [80, 80, 40, 200]
    assert all(len(s) <= 250 for s in shards)


def test_records_write_shards_order_and_index(tmp_path):
    ids = [f"s{i}" for i in range(40)] + ["ü-src"]
    data = {i: _spec(k, n=2000) for k, i in enumerate(ids)}
    data.pop("s7")                         # loader returns None -> skipped
    spec = RecordSetSpec(key="spec", kind="pks1", catalog="src", ids=ids, loader=data.get)
    rows = {rid: 100 - k for k, rid in enumerate(ids)}     # reverse catalog order
    ledger = Ledger()
    res = records.write(spec, tmp_path, order=rows, ledger=ledger)
    idx = json.loads((tmp_path / "index.json").read_text("utf-8"))
    assert idx == json.loads(json.dumps(res.index))
    assert schema.validate_records(idx) == []
    assert idx["schema"] == "pk-records/1" and idx["kind"] == "pks1" and idx["layout"] == "shard"
    assert idx["n"] == 40 and "s7" not in idx["entries"] and res.skipped == ["s7"]
    assert idx["variants"] == ["flux"] and idx["unit_w"] == "um" and idx["unit_f"] == "Jy"
    assert len(idx["shards"]) >= 2
    for sh in idx["shards"]:
        size = (tmp_path / sh).stat().st_size
        nmem = len(codec.gzip_members((tmp_path / sh).read_bytes()))
        assert size <= SHARD_MAX
        assert nmem >= 1
        if nmem > 1:
            assert size <= SHARD_TARGET
    # members appended in catalog stored order: the last id (lowest row) comes first
    first = idx["entries"]["ü-src"]
    assert first[:2] == [0, 0]
    for rid in ("s0", "ü-src", "s39"):
        d = records.decode_pks1(_read_entry(tmp_path, idx, rid))
        assert d["id"] == rid
        np.testing.assert_allclose(d["channels"]["flux"], data[rid].channels["flux"], rtol=1e-6)
    assert any(e.kind == "shard" for e in ledger.entries)
    errs, _ = ledger.check()
    assert errs == []


def test_records_oversize_member_goes_to_file(tmp_path):
    rng = np.random.default_rng(5)
    n = 160_000
    big = Spectrum(wave=np.sort(rng.uniform(1, 5, n)), channels={"flux": rng.normal(size=n)})
    small = _spec(1, 100)
    spec = RecordSetSpec(key="sp", kind="pks1", catalog="src", ids=["big", "small"],
                         loader={"big": big, "small": small}.get)
    res = records.write(spec, tmp_path)
    idx = res.index
    assert isinstance(idx["entries"]["big"], str) and idx["entries"]["big"].startswith("r/")
    assert isinstance(idx["entries"]["small"], list)
    assert any("SHARD_MAX" in w for w in res.warnings)
    assert schema.validate_records(idx) == []
    d = records.decode_pks1(_read_entry(tmp_path, idx, "big"))
    assert d["header"]["n"] == n


def test_records_layout_file_and_json_kind(tmp_path):
    recs = {"a": {"v": 1, "s": "☃"}, "b": {"v": [1.5, None]}}
    spec = RecordSetSpec(key="j", kind="json", catalog="src", ids=["a", "b"], loader=recs.get,
                         layout="file")
    res = records.write(spec, tmp_path)
    idx = res.index
    assert idx["layout"] == "file" and idx["shards"] == []
    assert all(isinstance(e, str) and e.startswith("r/") for e in idx["entries"].values())
    assert schema.validate_records(idx) == []
    for k, v in recs.items():
        assert records.decode_record(_read_entry(tmp_path, idx, k), "json") == v


def test_records_sedcurve_defaults_u16log(tmp_path):
    s = Spectrum(wave=np.array([1.2, 1.6, 2.2, 3.6, 4.5]), channels={"flux": np.array([1, 2, 3, 4, 5.0])})
    spec = RecordSetSpec(key="sed", kind="sedcurve", catalog="src", ids=["m1"], loader=lambda i: s)
    res = records.write(spec, tmp_path)
    d = records.decode_record(_read_entry(tmp_path, res.index, "m1"), "sedcurve")
    assert d["header"]["channels"][0]["enc"] == "u16log"
    np.testing.assert_allclose(d["channels"]["flux"], [1, 2, 3, 4, 5], rtol=2e-4)


def test_records_pki1_png_mode_and_grids(tmp_path):
    img = Image2D(data=np.ones((2, 3)), wave=np.array([1.0, 2.0, 3.0]), y=np.array([0.0, 1.0]))
    png = {"p1": ("img/p1.png", {"w": [1.0, 3.0], "y": [0.0, 1.0]})}
    spec = RecordSetSpec(key="s2d", kind="pki1", catalog="src", ids=["p1", "b1"],
                         loader=lambda i: img, png=png.get, grids={"g": np.array([1.0, 2.0, 3.0])})
    res = records.write(spec, tmp_path)
    idx = res.index
    assert idx["entries"]["p1"] == {"png": "img/p1.png", "extent": {"w": [1.0, 3.0], "y": [0.0, 1.0]}}
    assert idx["grids"]["g"] == {"kind": "uniform", "w0": 1.0, "dw": 1.0, "n": 3}
    assert schema.validate_records(idx) == []
    d = records.decode_record(_read_entry(tmp_path, idx, "b1"), "pki1", grids=idx["grids"])
    assert d["header"]["wave"] == {"kind": "shared", "grid": "g"}
    np.testing.assert_allclose(d["wave"], [1, 2, 3])
    assert records.grid_descriptor(np.array([1.0, 1.5, 4.0])) == {"kind": "array", "values": [1.0, 1.5, 4.0]}


def test_records_json_legacy_tiers(tmp_path):
    spec = RecordSetSpec(key="L", kind="json", catalog="src", ids=["a"], loader=lambda i: None,
                         legacy_json=("../spectra/", "{id}.json"))
    res = records.write(spec, tmp_path, tier="B")
    assert res.index["kind"] == "json-legacy" and "schema" not in res.index
    assert schema.validate_records(res.index) == []
    with pytest.raises(BundleError):
        records.write(spec, tmp_path / "c", tier="C")
    with pytest.raises(BundleError):              # section 5.6: only tiers A and B
        records.write(spec, tmp_path / "none")
    assert records.write(spec, tmp_path / "a", tier="A").index["kind"] == "json-legacy"


def test_records_errors_and_empty(tmp_path):
    with pytest.raises(BundleError):
        records.write(RecordSetSpec(key="x", kind="pks1", catalog="c", ids=["a", "a"],
                                    loader=lambda i: None), tmp_path)
    with pytest.raises(BundleError):
        records.write(RecordSetSpec(key="x", kind="fits", catalog="c", ids=[], loader=lambda i: None),
                      tmp_path)
    res = records.write(RecordSetSpec(key="e", kind="pks1", catalog="c", ids=[], loader=lambda i: None),
                        tmp_path / "e")
    assert res.index["n"] == 0 and res.index["shards"] == []
    assert schema.validate_records(res.index) == []


def test_records_deterministic_bytes(tmp_path):
    ids = ["a", "b", "c"]
    spec = RecordSetSpec(key="d", kind="pks1", catalog="c", ids=ids, loader=lambda i: _spec(ord(i)))
    r1 = records.write(spec, tmp_path / "1")
    r2 = records.write(spec, tmp_path / "2")
    assert r1.index == r2.index
    assert (tmp_path / "1" / "index.json").read_bytes() == (tmp_path / "2" / "index.json").read_bytes()
