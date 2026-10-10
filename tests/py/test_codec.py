"""Codec round-trips, edge cases and golden byte identity (portalkit.bundle.codec)."""

from __future__ import annotations

import gzip
import importlib.util
import json
import math
import struct
import zlib
from pathlib import Path

import numpy as np
import pytest

from portalkit.bundle import codec
from portalkit.bundle.codec import (CAT8_MISSING, CAT16_MISSING, I16_MISSING, QMAX, U32_MISSING,
                                    decode_file, dequant)

REPO = Path(__file__).resolve().parents[2]
GOLDEN = REPO / "tests" / "golden" / "codec"


def roundtrip(enc_obj: codec.Encoded, n: int | None = None) -> np.ndarray:
    """Encode to file bytes, decode back to codes and compare with the encoder's codes."""
    data = enc_obj.file_bytes()
    codes = decode_file(enc_obj.meta, data, n if n is not None else len(enc_obj.codes))
    np.testing.assert_array_equal(codes, enc_obj.codes)
    return codes


# ---------------------------------------------------------------------------
# gz and shuffles
# ---------------------------------------------------------------------------

def test_gz_deterministic_header():
    a = codec.gz(b"hello" * 100)
    b = codec.gz(b"hello" * 100)
    assert a == b
    assert a[:4] == b"\x1f\x8b\x08\x00"
    assert a[4:8] == b"\x00\x00\x00\x00"      # mtime 0
    assert a[9] == 0xFF                       # OS byte (GC gzip.compress)
    f = codec.gz(b"hello" * 100, filtered=True)
    assert f[4:8] == b"\x00\x00\x00\x00"
    assert f[9] == 0x03                       # zlib wbits=31 header
    assert gzip.decompress(a) == gzip.decompress(f) == b"hello" * 100


def test_gz_empty_and_members():
    m1, m2 = codec.gz(b""), codec.gz(b"abc")
    assert codec.gunzip(m1) == b""
    both = m1 + m2
    assert codec.gzip_members(both) == [(0, len(m1)), (len(m1), len(m2))]
    assert codec.gunzip(both) == b"abc"
    with pytest.raises(ValueError):
        codec.gzip_members(m2[:-3])


@pytest.mark.parametrize("dtype,width", [("<u2", 2), ("<i2", 2), ("<f4", 4), ("<u4", 4)])
def test_shuffle_roundtrip(dtype, width):
    rng = np.random.default_rng(3)
    a = (rng.integers(-30000, 30000, 37) if dtype != "<f4" else rng.normal(size=37)).astype(dtype)
    if width == 2:
        sh = codec.shuffle_i16(a) if dtype == "<i2" else codec.shuffle_u16(a)
    else:
        sh = codec.shuffle4(a)
    # plane 0 holds the low bytes
    assert sh[:len(a)] == a.view(np.uint8)[::width].tobytes()
    assert codec.unshuffle(sh, width) == a.tobytes()
    assert codec.unshuffle(b"", width) == b""
    with pytest.raises(ValueError):
        codec.unshuffle(b"abc", 2)


# ---------------------------------------------------------------------------
# u16q / u8q
# ---------------------------------------------------------------------------

def test_u16q_roundtrip_within_half_step():
    rng = np.random.default_rng(1)
    x = rng.normal(10, 3, 5000)
    x[::97] = np.nan
    e = codec.encode_u16q(x)
    codes = roundtrip(e)
    v = dequant(e.meta, codes)
    ok = np.isfinite(x)
    assert np.all(np.isnan(v[~ok])) and np.all(codes[~ok] == 0)
    assert np.max(np.abs(v[ok] - x[ok])) <= e.meta["step"] / 2 * (1 + 1e-9)
    assert e.meta["step"] == pytest.approx((e.meta["hi"] - e.meta["lo"]) / QMAX)
    assert codes[ok].min() == 1 and codes[ok].max() == QMAX + 1
    assert e.meta["shuffle"] is True


def test_u16q_fixed_step_mag():
    m = np.array([12.3456, 18.0, 25.999, 9.0011, np.nan])
    e = codec.encode_u16q(m, step=0.001)
    assert e.meta["step"] == 0.001
    assert e.meta["lo"] == pytest.approx(9.001)
    assert e.meta["lo"] <= np.nanmin(m)
    assert e.meta["hi"] == pytest.approx(e.meta["lo"] + 65534 * 0.001)
    v = dequant(e.meta, roundtrip(e))
    assert np.nanmax(np.abs(v - m)) <= 0.0005 + 1e-12
    assert not e.warnings


def test_u16q_fixed_step_widens_when_span_too_large():
    m = np.array([0.0, 100.0])          # 100 mag span > 65.534
    e = codec.encode_u16q(m, step=0.001, label="mag")
    assert e.meta["step"] > 0.001
    assert any("widened" in w for w in e.warnings)
    assert e.meta["hi"] >= 100.0 and e.meta["lo"] <= 0.0
    v = dequant(e.meta, roundtrip(e))
    assert np.max(np.abs(v - m)) <= e.meta["step"] / 2 + 1e-12


def test_u16q_negative_lo_rounding():
    e = codec.encode_u16q(np.array([-3.0004, -1.0]), step=0.001)
    assert e.meta["lo"] <= -3.0004
    v = dequant(e.meta, e.codes)
    assert np.max(np.abs(v - [-3.0004, -1.0])) <= 0.0005 + 1e-12


def test_u16q_inf_nan_large_and_empty():
    x = np.array([np.inf, -np.inf, np.nan, 1e300, -1e300])
    e = codec.encode_u16q(x)
    assert list(e.codes[:3]) == [0, 0, 0]
    assert e.codes[3] == QMAX + 1 and e.codes[4] == 1
    e0 = codec.encode_u16q(np.array([], dtype=float))
    assert e0.codes.size == 0
    assert decode_file(e0.meta, e0.file_bytes()).size == 0
    eall = codec.encode_u16q(np.full(4, np.nan))
    assert np.all(eall.codes == 0)
    assert eall.meta["step"] > 0


def test_u16q_single_value_degenerate():
    e = codec.encode_u16q(np.array([5.0]))
    assert e.codes.tolist() == [1]
    assert e.meta["lo"] == e.meta["hi"] == 5.0
    assert e.meta["step"] > 0
    assert dequant(e.meta, e.codes)[0] == 5.0


def test_u16q_clip_saturate_and_drop():
    x = np.array([-5.0, 0.0, 0.5, 1.0, 9.0, np.nan])
    s = codec.encode_u16q(x, lo=0.0, hi=1.0, clip="saturate")
    assert s.limits == {0: codec.LIMIT_CLIP_LOW, 4: codec.LIMIT_CLIP_HIGH}
    assert s.codes.tolist()[:5] == [1, 1, 32768, 65535, 65535]
    d = codec.encode_u16q(x, lo=0.0, hi=1.0, clip="drop")
    assert d.codes.tolist() == [0, 1, 32768, 65535, 0, 0]
    assert d.limits == {}
    # clip none: adapter bounds widen but never drop
    n = codec.encode_u16q(x, lo=0.0, hi=1.0, clip="none")
    assert n.meta["lo"] == -5.0 and n.meta["hi"] == 9.0
    with pytest.raises(ValueError):
        codec.encode_u16q(x, clip="bogus")
    with pytest.raises(ValueError):
        codec.encode_u16q(x, lo=2.0, hi=1.0, clip="drop")


def test_u16q_saturate_percentiles_default():
    x = np.r_[np.linspace(0, 1, 10000), 1e6]
    e = codec.encode_u16q(x, clip="saturate")
    assert e.meta["hi"] < 2.0
    assert e.limits[10000] == codec.LIMIT_CLIP_HIGH


def test_u8q_roundtrip():
    x = np.linspace(-2, 2, 300)
    x[5] = np.nan
    e = codec.encode_u8q(x)
    assert e.meta["step"] == pytest.approx(4 / 254)
    v = dequant(e.meta, roundtrip(e))
    ok = np.isfinite(x)
    assert np.max(np.abs(v[ok] - x[ok])) <= e.meta["step"] / 2 * (1 + 1e-9)
    assert e.codes[5] == 0 and e.codes.max() == 255
    assert e.meta["shuffle"] is False


def test_dequant_without_step_derives_it():
    e = codec.encode_u16q(np.array([0.0, 1.0, 0.25]))
    meta = {k: v for k, v in e.meta.items() if k != "step"}
    np.testing.assert_allclose(dequant(meta, e.codes), dequant(e.meta, e.codes))


# ---------------------------------------------------------------------------
# log encodings
# ---------------------------------------------------------------------------

def test_u16log_roundtrip_relative():
    rng = np.random.default_rng(2)
    x = 10 ** rng.uniform(-6, 4, 3000)
    x[:3] = [0.0, -1.0, np.nan]
    e = codec.encode_u16log(x)
    v = dequant(e.meta, roundtrip(e))
    assert np.all(e.codes[:3] == 0) and np.all(np.isnan(v[:3]))
    rel = np.abs(np.log10(v[3:]) - np.log10(x[3:]))
    assert rel.max() <= e.meta["step"] / 2 * (1 + 1e-9)
    assert e.meta["lo"] == pytest.approx(np.log10(x[3:].min()))


def test_u16log_linear_bounds_and_keep():
    x = np.array([0.0, 1.0, 10.0, 100.0])
    e = codec.encode_u16log(x, lo=0.1, hi=1000.0, keep=np.array([True, False, False, False]))
    assert e.meta["lo"] == pytest.approx(-1.0) and e.meta["hi"] == pytest.approx(3.0)
    assert e.codes[0] == 1     # limit row with flux 0 -> floor


def test_u8log_matches_gc_log_quantize():
    rng = np.random.default_rng(4)
    x = 10 ** rng.uniform(-3, 0, 500)
    e = codec.encode_u8log(x)
    np.testing.assert_array_equal(e.codes, codec.log_quantize(x, x.min(), x.max()))
    v = dequant(e.meta, roundtrip(e))
    assert np.max(np.abs(np.log10(v) - np.log10(x))) <= e.meta["step"] / 2 * (1 + 1e-6)
    assert e.meta["step"] == pytest.approx((np.log10(x.max()) - np.log10(x.min())) / 254)


def test_u8log_saturate_and_errors():
    x = np.r_[np.linspace(0.01, 0.1, 2000), 50.0]
    e = codec.encode_u8log(x, clip="saturate")
    assert e.limits.get(2000) == codec.LIMIT_CLIP_HIGH
    assert e.codes[2000] == 255
    with pytest.raises(ValueError):
        codec.encode_u8log(x, lo=-1.0)
    one = codec.encode_u8log(np.array([0.5, np.nan]))
    assert one.codes.tolist() == [1, 0]
    assert dequant(one.meta, one.codes)[0] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# categories, bits, f32, u32, u32lin
# ---------------------------------------------------------------------------

def test_cat8_unicode_and_missing():
    x = np.array(["ü", "a", None, "b", "a", float("nan")], dtype=object)
    e = codec.encode_cat(x)
    assert e.meta["levels"] == ["a", "b", "ü"]
    assert e.codes.tolist() == [2, 0, CAT8_MISSING, 1, 0, CAT8_MISSING]
    roundtrip(e)
    assert dequant(e.meta, e.codes).tolist() == ["ü", "a", None, "b", "a", None]


def test_cat_levels_given_and_extra():
    e = codec.encode_cat(np.array(["x", "y", "z"], dtype=object), levels=["z", "x"], label="c")
    assert e.meta["levels"] == ["z", "x", "y"]
    assert e.codes.tolist() == [1, 2, 0]
    assert e.warnings
    with pytest.raises(ValueError):
        codec.encode_cat(np.array(["a"]), levels=["a", "a"])


def test_cat16_switch_and_capacity():
    lv = [str(i) for i in range(300)]
    e = codec.encode_cat(np.array(lv + [None], dtype=object))
    assert e.enc == "cat16" and e.meta["shuffle"] is True
    assert e.codes[-1] == CAT16_MISSING
    roundtrip(e)
    with pytest.raises(ValueError):
        codec.encode_cat(np.array(lv, dtype=object), enc="cat8")
    ints = codec.encode_cat(np.array([3, 1, 3]))
    assert ints.meta["levels"] == ["1", "3"]


def test_bits_lsb_first():
    b = np.array([1, 0, 0, 0, 0, 0, 0, 0, 1], dtype=bool)
    e = codec.encode_bits(b)
    raw = codec.gunzip(e.file_bytes())
    assert raw == bytes([0b00000001, 0b00000001])
    np.testing.assert_array_equal(decode_file(e.meta, e.file_bytes(), 9), b.astype(np.uint8))
    s = codec.encode_bits(np.array(["yes", "", "0", "True", None], dtype=object))
    assert s.codes.tolist() == [1, 0, 0, 1, 0]
    f = codec.encode_bits(np.array([np.nan, 2.0, 0.0]))
    assert f.codes.tolist() == [0, 1, 0]
    assert codec.encode_bits(np.array([], dtype=bool)).file_bytes() == codec.gz(b"")


def test_f32_roundtrip_nan_inf_overflow():
    x = np.array([1.5, np.nan, np.inf, -np.inf, 1e39, 1e-45, 0.0])
    e = codec.encode_f32(x)
    assert e.warnings and "overflow" in e.warnings[0]
    v = decode_file(e.meta, e.file_bytes())
    assert v.dtype == np.float32
    with np.errstate(over="ignore"):
        want = x.astype(np.float32)
    np.testing.assert_array_equal(v, want)


def test_u32_and_out_of_range():
    x = np.array([0, 5, 4294967294, 4294967295, -1, np.nan, 2.5])
    e = codec.encode_u32(x)
    assert e.codes.tolist() == [0, 5, 4294967294, U32_MISSING, U32_MISSING, U32_MISSING, 2]
    assert len(e.warnings) == 2
    roundtrip(e)


def test_u32lin_roundtrip_precision():
    rng = np.random.default_rng(5)
    ra = rng.uniform(266.0, 267.0, 1000)
    e = codec.encode_u32lin(ra, ra.min(), ra.max())
    v = dequant(e.meta, roundtrip(e))
    assert np.max(np.abs(v - ra)) < 1.0 / 4294967294 + 1e-12
    clamp = codec.encode_u32lin(np.array([0.0, 2.0, np.nan]), 0.5, 1.5)
    assert clamp.codes.tolist() == [0, 4294967294, U32_MISSING]
    deg = codec.encode_u32lin(np.array([3.0, 3.0]), 3.0, 3.0)
    assert dequant(deg.meta, deg.codes).tolist() == [3.0, 3.0]


# ---------------------------------------------------------------------------
# positions
# ---------------------------------------------------------------------------

def _sep_deg(ra1, dec1, ra2, dec2):
    dra = (np.asarray(ra1) - np.asarray(ra2) + 180.0) % 360.0 - 180.0
    return np.hypot(dra * np.cos(np.radians(dec2)), np.asarray(dec1) - np.asarray(dec2))


def test_i16off_roundtrip_all_sky():
    rng = np.random.default_rng(6)
    n = 4000
    ra = rng.uniform(0, 360, n)
    dec = np.degrees(np.arcsin(rng.uniform(-1, 1, n)))
    ra[:4] = [0.0, 359.99999, 180.0, 10.0]
    dec[:4] = [0.0, 0.0, 90.0, -90.0]
    ra[10] = np.nan
    p = codec.encode_i16off(ra, dec, step_mas=8.0)
    files = {k: codec.file_bytes("u16" if k == "tile" else "i16", p[k], shuffle=True)
             for k in ("tile", "dra", "ddec")}
    meta = {"enc": "i16off", "step_mas": p["step_mas"], "tile_refs": p["tile_refs"]}
    r2, d2 = codec.decode_pos(meta, files)
    ok = np.isfinite(ra)
    assert np.isnan(r2[10]) and np.isnan(d2[10])
    assert p["dra"][10] == I16_MISSING
    sep = _sep_deg(r2[ok], d2[ok], ra[ok], dec[ok])
    assert sep.max() <= p["step_mas"] / 3.6e6       # <= one step (half-step per axis)
    assert len(p["tile_refs"]) <= 65535
    assert np.abs(p["dra"][ok]).max() <= 32767


def test_i16off_tile_overflow_doubles_step():
    rng = np.random.default_rng(8)
    ra = rng.uniform(0, 360, 70000)
    dec = rng.uniform(-60, 60, 70000)
    p = codec.encode_i16off(ra, dec, step_mas=8.0, tile_deg=0.01)
    assert len(p["tile_refs"]) <= 65535
    assert p["step_mas"] > 8.0 and p["tile_deg"] > 0.01
    meta = {"enc": "i16off", "step_mas": p["step_mas"], "tile_refs": p["tile_refs"]}
    files = {k: codec.file_bytes("u16" if k == "tile" else "i16", p[k], shuffle=True)
             for k in ("tile", "dra", "ddec")}
    r2, d2 = codec.decode_pos(meta, files)
    assert _sep_deg(r2, d2, ra, dec).max() <= p["step_mas"] / 3.6e6
    # a step too fine for the tile size is raised so offsets fit in int16
    p2 = codec.encode_i16off(ra[:10], dec[:10], step_mas=0.0001, tile_deg=0.001)
    assert p2["step_mas"] >= 0.001 * 3.6e6 / 60000
    assert np.abs(p2["dra"]).max() <= 30001 and np.abs(p2["ddec"]).max() <= 30001


def test_i16off_tile_count_capped_with_warning():
    rng = np.random.default_rng(9)
    ra = rng.uniform(0, 360, 20000)
    dec = np.degrees(np.arcsin(rng.uniform(-1, 1, 20000)))
    p = codec.encode_i16off(ra, dec, step_mas=8.0)
    assert len(p["tile_refs"]) <= codec.I16OFF_MAX_TILES
    assert p["step_mas"] > 8.0 and p["warnings"] and "step_mas raised" in p["warnings"][0]
    small = codec.encode_i16off(ra[:50] * 0.001 + 10, dec[:50] * 0.001, step_mas=8.0)
    assert small["step_mas"] == 8.0 and small["warnings"] == []
    capped = codec.encode_i16off(ra, dec, step_mas=8.0, max_tiles=100)
    assert len(capped["tile_refs"]) <= 100


def test_i16off_all_missing_and_empty():
    p = codec.encode_i16off(np.array([np.nan]), np.array([np.nan]))
    assert p["tile_refs"] == [] and p["dra"][0] == I16_MISSING
    p0 = codec.encode_i16off(np.array([]), np.array([]))
    assert p0["tile"].size == 0
    with pytest.raises(ValueError):
        codec.encode_i16off(np.array([1.0]), np.array([1.0, 2.0]))


def test_morton16():
    x = np.array([0, 1, 0, 1, 0xFFFF], dtype=np.uint16)
    y = np.array([0, 0, 1, 1, 0xFFFF], dtype=np.uint16)
    assert codec.morton16(x, y).tolist() == [0, 1, 2, 3, 0xFFFFFFFF]


# ---------------------------------------------------------------------------
# str and sparse
# ---------------------------------------------------------------------------

def test_str_roundtrip_unicode():
    s = ["a", "", "漢字\U0001F600", None, "x" * 10000, b"bytes"]
    raw = codec.encode_str(s)
    n = struct.unpack_from("<I", raw)[0]
    assert n == 6
    assert codec.decode_str(raw) == ["a", "", "漢字\U0001F600", "", "x" * 10000, "bytes"]
    assert codec.decode_str(codec.encode_str([])) == []
    meta = {"enc": "str"}
    assert decode_file(meta, codec.gz(raw)).tolist()[2] == "漢字\U0001F600"


def test_sparse_roundtrip_sort_dedupe():
    raw = codec.encode_sparse([9, 3, 3, 0], [5, 1, 2, 4])
    idx, kind = codec.decode_sparse(raw)
    assert idx.tolist() == [0, 3, 9] and kind.tolist() == [4, 1, 5]
    assert codec.decode_sparse(codec.encode_sparse([], []))[0].size == 0
    with pytest.raises(ValueError):
        codec.encode_sparse([1], [0])
    with pytest.raises(ValueError):
        codec.encode_sparse([-1], [1])


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------

def test_col_stats_edge_cases():
    assert codec.col_stats(np.array([])) == {"center": 0.0, "scale": 1.0, "nvalid": 0}
    c = codec.col_stats(np.array([2.0, 2.0, 2.0]))
    assert c["scale"] == 1.0 and c["center"] == 2.0 and len(c["quantiles"]) == 101
    lg = codec.col_stats(np.array([1.0, 10.0, 100.0, -5.0]), log=True)
    assert lg["nvalid"] == 3 and lg["center"] == pytest.approx(1.0)


def test_to_float_array_mixed():
    a = codec.to_float_array(np.array([1, "2.5", None, "x"], dtype=object))
    assert a[:2].tolist() == [1.0, 2.5] and np.isnan(a[2]) and np.isnan(a[3])
    m = np.ma.MaskedArray([1.0, 2.0], mask=[False, True])
    assert np.isnan(codec.to_float_array(m)[1])


# ---------------------------------------------------------------------------
# goldens
# ---------------------------------------------------------------------------

def _load_generator():
    spec = importlib.util.spec_from_file_location("pk_golden_codec_generate", GOLDEN / "generate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_golden_bytes_identical():
    gen = _load_generator()
    files, index = gen.build()
    for name, data in files.items():
        assert (GOLDEN / name).read_bytes() == data, f"golden {name} differs; rerun generate.py"
    stored = json.loads((GOLDEN / "index.json").read_text("utf-8"))
    assert stored == json.loads(json.dumps(index, ensure_ascii=False))


def test_golden_decode_matches_expected():
    idx = json.loads((GOLDEN / "index.json").read_text("utf-8"))
    assert idx["schema"] == "pk-golden-codec/1"
    kinds = set()
    encs = set()
    for case in idx["cases"]:
        kinds.add(case["kind"])
        if case["kind"] == "column":
            meta = case["meta"]
            encs.add(meta["enc"])
            data = (GOLDEN / case["file"]).read_bytes()
            codes = decode_file(meta, data, case["n"])
            if "codes" in case:
                assert [int(c) for c in codes] == case["codes"], case["name"]
            if meta["enc"] == "str":
                assert codes.tolist() == case["expected"]
            elif meta["enc"] == "bits":
                assert codes.tolist() == case["expected"]
            else:
                got = dequant(meta, codes)
                for g, x in zip(got.tolist(), case["expected"]):
                    if x is None:
                        assert g is None or (isinstance(g, float) and math.isnan(g)), case["name"]
                    elif isinstance(x, str):
                        assert g == x
                    else:
                        assert g == pytest.approx(x, rel=1e-12, abs=case.get("tol", 0) or 1e-300)
        elif case["kind"] == "sparse":
            idx_, kind = codec.decode_sparse(codec.gunzip((GOLDEN / case["file"]).read_bytes()))
            assert idx_.tolist() == case["expected"]["idx"] and kind.tolist() == case["expected"]["kind"]
        elif case["kind"] == "pos":
            files = {k: (GOLDEN / v).read_bytes() for k, v in case["files"].items()}
            ra, dec = codec.decode_pos(case["meta"], files, case.get("bbox"))
            inp = case["input"]
            for i in range(case["n"]):
                if case["expected"]["ra"][i] is None:
                    assert math.isnan(ra[i])
                    continue
                assert ra[i] == pytest.approx(case["expected"]["ra"][i], abs=1e-9)
                assert dec[i] == pytest.approx(case["expected"]["dec"][i], abs=1e-9)
                sep = _sep_deg(ra[i], dec[i], inp["ra"][i], inp["dec"][i])
                assert sep <= case["tol_deg"] * 1.0001
    assert {"column", "pos", "sparse", "pks1", "pki1", "shard", "density", "rows"} <= kinds
    assert {"u16q", "u16log", "u8q", "u8log", "cat8", "cat16", "bits", "f32", "u32", "str"} <= encs


def test_golden_files_use_filtered_gzip_when_shuffled():
    idx = json.loads((GOLDEN / "index.json").read_text("utf-8"))
    for case in idx["cases"]:
        if case["kind"] != "column":
            continue
        data = (GOLDEN / case["file"]).read_bytes()
        assert data[9] == (0x03 if case["meta"].get("shuffle") else 0xFF), case["name"]
        zlib.decompress(data, 31)
