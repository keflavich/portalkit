"""Density grids and binned statistics (portalkit.bundle.density, section 5.9)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from portalkit.bundle import codec, density
from portalkit.bundle.density import DensityError


def _decode_u16(data: bytes) -> np.ndarray:
    return np.frombuffer(codec.unshuffle(codec.gunzip(data), 2), dtype="<u2")


def test_parse_linear():
    assert density.parse_linear("a") == ({"a": 1.0}, 0.0)
    assert density.parse_linear("F182M - F187N") == ({"F182M": 1.0, "F187N": -1.0}, 0.0)
    assert density.parse_linear("-a + 2.5*b - 3") == ({"a": -1.0, "b": 2.5}, -3.0)
    assert density.parse_linear("a.b_1 + a.b_1") == ({"a.b_1": 2.0}, 0.0)
    for bad in ("", "a +", "a * b", "(a)", "a b", None):
        with pytest.raises(DensityError):
            density.parse_linear(bad)
    assert density.base_keys("x - 0.5*y + 1") == ["x", "y"]


def test_eval_linear_nan_and_missing_column():
    cols = {"a": np.array([1.0, np.nan, 3.0]), "b": np.array([1.0, 1.0, 1.0])}
    v = density.eval_linear("a - b + 1", cols)
    assert v[0] == 1.0 and np.isnan(v[1]) and v[2] == 3.0
    with pytest.raises(DensityError):
        density.eval_linear("2", cols)
    with pytest.raises(KeyError):
        density.eval_linear("zz", cols)


def test_count_grid_layout():
    # one point per corner cell; iy = 0 at yr[0]; cell = iy*nx + ix
    x = np.array([0.1, 3.9, 0.1, 3.9, 4.0, np.nan, 9.0])
    y = np.array([0.1, 0.1, 1.9, 1.9, 2.0, 1.0, 1.0])
    g = density.count_grid(x, y, [0, 4], [0, 2], nx=4, ny=2)
    assert g.shape == (2, 4)
    assert g[0, 0] == 1 and g[0, 3] == 1 and g[1, 0] == 1
    assert g[1, 3] == 2          # x = 4.0 / y = 2.0 land in the last (right-closed) bin
    assert g.sum() == 5          # NaN and out-of-range rows are not counted
    with pytest.raises(DensityError):
        density.count_grid(x, y, [1, 1], [0, 2])
    with pytest.raises(DensityError):
        density.count_grid(x, y, [0, 1], [0, 2], nx=0)


def test_encode_counts_u16log_roundtrip():
    c = np.array([[0, 1, 2], [10, 40211, 0]])
    codes, meta = density.encode_counts(c, "u16log")
    # section 5.3 u16log semantics: lo/hi in log10, step always written (note 9)
    assert meta["enc"] == "u16log" and meta["lo"] == 0.0
    assert meta["hi"] == pytest.approx(math.log10(40211))
    assert meta["step"] == pytest.approx((meta["hi"] - meta["lo"]) / 65534)
    assert codes[0] == 0 and codes[1] == 1 and codes[4] == 65535
    back = density.decode_counts(meta, codes)
    nz = c.ravel() > 0
    rel = np.abs(back[nz] - c.ravel()[nz]) / c.ravel()[nz]
    assert rel.max() < 1e-4
    assert back[0] == 0
    # the generic column decoder (codec.dequant) gives the same counts
    np.testing.assert_allclose(np.nan_to_num(codec.dequant(meta, codes)), back, rtol=1e-12)


def test_encode_counts_edge_cases():
    codes, meta = density.encode_counts(np.zeros((2, 2)), "u16log")
    assert not codes.any() and "step" in meta
    codes, meta = density.encode_counts(np.array([0, 7, 7]), "u16log")
    assert codes.tolist() == [0, 1, 1] and "step" in meta
    np.testing.assert_allclose(density.decode_counts(meta, codes), [0, 7, 7], rtol=1e-12)
    codes, meta = density.encode_counts(np.array([0, 3, 9]), "u16q")
    assert meta["step"] == pytest.approx(6 / 65534)
    np.testing.assert_allclose(density.decode_counts(meta, codes), [0, 3, 9])
    with pytest.raises(DensityError):
        density.encode_counts(np.array([1]), "f32")


def test_parse_stat():
    assert density.parse_stat(None) == ("count", None)
    assert density.parse_stat("count") == ("count", None)
    assert density.parse_stat("median:av") == ("median", "av")
    assert density.parse_stat("mean(a - b)") == ("mean", "a - b")
    for bad in ("avg:x", "median", "median:"):
        with pytest.raises(DensityError):
            density.parse_stat(bad)


@pytest.mark.parametrize("stat", ["mean", "median", "min", "max", "std", "sum", "count"])
def test_binned_stat_against_loop(stat):
    rng = np.random.default_rng(9)
    x, y = rng.uniform(0, 1, 400), rng.uniform(0, 1, 400)
    v = rng.normal(size=400)
    v[::17] = np.nan
    g = density.binned_stat(x, y, v, [0, 1], [0, 1], nx=3, ny=2, stat=stat)
    assert g.dtype == np.float32 and g.shape == (2, 3)
    ix = np.minimum((x * 3).astype(int), 2)
    iy = np.minimum((y * 2).astype(int), 1)
    fns = {"mean": np.mean, "median": np.median, "min": np.min, "max": np.max,
           "std": np.std, "sum": np.sum, "count": len}
    for j in range(2):
        for i in range(3):
            sel = (ix == i) & (iy == j) & np.isfinite(v)
            want = fns[stat](v[sel])
            assert g[j, i] == pytest.approx(want, rel=1e-5, abs=1e-6)


def test_binned_stat_empty_cells_nan():
    g = density.binned_stat(np.array([0.1]), np.array([0.1]), np.array([5.0]), [0, 1], [0, 1],
                            nx=2, ny=2, stat="median")
    assert g[0, 0] == 5.0 and np.isnan(g[1, 1])
    g0 = density.binned_stat(np.array([]), np.array([]), np.array([]), [0, 1], [0, 1], 2, 2, "mean")
    assert np.all(np.isnan(g0))


def test_build_density_count_entry_and_file():
    rng = np.random.default_rng(1)
    x, y = rng.normal(size=1000), rng.normal(size=1000)
    entry, data = density.build_density("cmd", x, y, x_expr="a - b", y_expr="a", nx=32, ny=16)
    assert entry["enc"] == "u16log" and entry["nx"] == 32 and entry["ny"] == 16
    assert entry["x"] == "a - b" and entry["total"] <= 1000
    assert entry["xr"][0] < np.percentile(x, 1) and entry["xr"][1] > np.percentile(x, 99)
    assert data[9] == 0x03                    # filtered gzip (shuffled)
    codes = _decode_u16(data)
    assert codes.size == 32 * 16
    counts = density.decode_counts(entry, codes)
    assert counts.sum() == pytest.approx(entry["total"], rel=1e-3)


def test_build_density_stat_entry():
    x = np.array([0.1, 0.2, 0.9])
    y = np.array([0.1, 0.2, 0.9])
    entry, data = density.build_density("m", x, y, x_expr="x", y_expr="y", xr=[0, 1], yr=[0, 1],
                                        nx=2, ny=2, stat="median:av", values=np.array([1.0, 3.0, 7.0]))
    assert entry["enc"] == "f32" and entry["stat"] == "median" and entry["value"] == "av"
    assert entry["shuffle"] is True and entry["lo"] == 2.0 and entry["hi"] == 7.0
    g = np.frombuffer(codec.unshuffle(codec.gunzip(data), 4), dtype="<f4").reshape(2, 2)
    assert g[0, 0] == 2.0 and g[1, 1] == 7.0 and np.isnan(g[0, 1])
    with pytest.raises(DensityError):
        density.build_density("m", x, y, x_expr="x", y_expr="y", stat="mean:av")


def test_auto_range_edge_cases():
    assert density.auto_range(np.array([np.nan])) == [0.0, 1.0]
    a, b = density.auto_range(np.array([5.0, 5.0]))
    assert a < 5.0 < b
    a, b = density.auto_range(np.linspace(0, 10, 1001))
    assert a < 0.05 and b > 9.95
