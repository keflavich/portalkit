"""portalkit.legacy.nirspec: grating cuts, segment splitting/stacking, s2d stitching, clean_flux."""

from __future__ import annotations

import numpy as np
import pytest

from portalkit.legacy import nirspec as ns
from portalkit.spec import Image2D


# -- cuts ----------------------------------------------------------------------------------------
def test_overlap_cuts_6927_semantics():
    assert ns.overlap_cuts([]) == []
    assert ns.overlap_cuts(["PRISM"]) == []                   # base grating never cut
    assert ns.overlap_cuts(["G999X"]) == []                   # unknown gratings ignored
    (g, lo, hi), = ns.overlap_cuts(["G395M"])
    assert (g, lo, hi) == ("G395M", 0.0, 99.0)
    cuts = ns.overlap_cuts(["G395M", "G140M", "G235M", "PRISM", "G140M"])
    assert [c[0] for c in cuts] == ["G140M", "G235M", "G395M"]
    assert cuts[0][2] == pytest.approx(0.5 * (1.648 + 1.878))
    assert cuts[1][2] == pytest.approx(0.5 * (2.850 + 3.136))
    assert all(a[2] == b[1] for a, b in zip(cuts, cuts[1:]))
    # custom ranges (large values, unicode labels)
    c = ns.overlap_cuts(["α", "β"], {"α": (1e5, 3e5), "β": (2e5, 4e5)}, base=None)
    assert c == [("α", 0.0, 2.5e5), ("β", 2.5e5, 99.0)]


def test_fill_cuts_prism_fills_gaps():
    r = {"PRISM": (0.55, 5.38), "G140M": (0.96, 1.90), "G395M": (2.85, 5.29)}
    cuts = ns.fill_cuts(r)
    assert [c[0] for c in cuts] == ["PRISM", "G140M", "PRISM", "G395M", "PRISM"]
    assert cuts[0][1] == 0.0 and cuts[-1][2] == 99.0
    assert all(a[2] == b[1] for a, b in zip(cuts, cuts[1:]))
    assert cuts[1][1:] == (0.96, 1.90) and cuts[3][1:] == (2.85, 5.29)
    # base only / no base / overlapping medium gratings / NaN or empty ranges
    assert ns.fill_cuts({"PRISM": (0.6, 5.3)}) == [("PRISM", 0.0, 99.0)]
    assert ns.fill_cuts({}) == []
    assert [c[0] for c in ns.fill_cuts({"G140M": (1, 2), "G235M": (1.5, 3)})] == ["G140M", "G235M"]
    o = ns.fill_cuts({"PRISM": (0.6, 5.3), "G140M": (1.0, 2.0), "G235M": (1.6, 3.0)})
    assert [c[0] for c in o] == ["PRISM", "G140M", "G235M", "PRISM"]
    assert o[1][2] == pytest.approx(1.8) and o[2][1] == pytest.approx(1.8)
    assert ns.fill_cuts({"PRISM": (np.nan, 5), "G140M": (2, 1), "G395M": (3, np.inf)}) == []


def test_cuts_nested_and_same_band_gratings():
    """An M + H grating of one band, or a range inside another: the outer grating keeps it."""
    c = ns.fill_cuts({"PRISM": (0.6, 5.3), "G140M": (0.97, 1.89), "G140H": (1.0, 1.3)})
    assert c == [("PRISM", 0.0, 0.97), ("G140M", 0.97, 1.89), ("PRISM", 1.89, 99.0)]
    c = ns.fill_cuts({"PRISM": (0.6, 5.3), "G140M": (0.97, 1.89), "G140H": (0.97, 1.89), "G395M": (2.8, 5.2)})
    assert [g for g, _, _ in c] == ["PRISM", "G140H", "PRISM", "G395M", "PRISM"]
    for cuts in (c, ns.fill_cuts({"PRISM": (0.6, 5.3), "A": (1, 4), "B": (1.5, 2), "C": (3, 5)})):
        assert all(a[2] == b[1] for a, b in zip(cuts, cuts[1:])) and all(x[2] > x[1] for x in cuts)
        w = np.linspace(0.5, 5.4, 500)
        segs = ns.split_segments(w, cuts)
        assert sum(i1 - i0 for i0, i1, _ in segs) == w.size                    # every point once
    oc = ns.overlap_cuts(["G140M", "G140H", "G395M"])
    assert [g for g, _, _ in oc] == ["G140H", "G395M"]
    assert oc[0][2] == pytest.approx(0.5 * (2.850 + 1.878))


def test_split_segments():
    w = np.array([0.5, 1.0, 1.5, 2.0, 3.0, np.nan])
    segs = ns.split_segments(w, [("A", 0.0, 1.2), ("B", 1.2, 2.5), ("C", 2.5, 99.0), ("D", 100, 200)])
    assert segs == [(0, 2, "A"), (2, 4, "B"), (4, 5, "C")]
    assert ns.split_segments(np.zeros(0), [("A", 0, 1)]) == []


def test_merge_split_sorts_and_splits():
    w = np.array([3.0, 1.0, 2.0, 1.0])
    f = np.array([30.0, 10.0, 20.0, 11.0])
    ws, ch, segs = ns.merge_split(w, {"flux": f, "err": f / 10}, ["G395M", "G140M", "G235M"])
    assert list(ws) == [1.0, 1.0, 2.0, 3.0] and list(ch["flux"]) == [10.0, 11.0, 20.0, 30.0]   # stable
    assert [s[2] for s in segs] == ["G140M", "G235M", "G395M"]
    _, _, segs = ns.merge_split(w, {"flux": f}, [])
    assert segs == []
    with pytest.raises(ValueError):
        ns.merge_split(w, {"flux": f[:2]}, ["G140M"])


def test_stack_segments():
    parts = [("G395M", np.array([5.0, 3.0, 4.0]), {"flux": np.array([5, 3, 4.0]), "err": np.ones(3)}),
             ("PRISM", np.array([0.6, np.nan, 5.3]), {"flux": np.array([1.0, 2.0, np.inf])}),
             ("G140M", np.array([]), {"flux": np.array([])})]
    w, ch, segs = ns.stack_segments(parts)
    assert segs == [(0, 2, "PRISM"), (2, 5, "G395M")]
    assert list(w) == [0.6, 5.3, 3.0, 4.0, 5.0]
    assert np.isinf(ch["flux"][1]) and list(ch["flux"][2:]) == [3, 4, 5]
    assert np.isnan(ch["err"][:2]).all()                       # missing channel -> NaN
    for i0, i1, _ in segs:
        assert np.all(np.diff(w[i0:i1]) > 0)
    w0, ch0, s0 = ns.stack_segments([])
    assert w0.size == 0 and ch0 == {} and s0 == []
    with pytest.raises(ValueError):
        ns.stack_segments([("x", np.arange(3.0), {"flux": np.arange(2.0)})])


def test_assign_gratings():
    prism = np.array([0.55, 1.0, 1.5, 5.0])
    g140 = np.array([1.0000001, 1.2, 1.4])
    merged = np.round(np.sort(np.concatenate([prism, g140])), 5)
    lab = ns.assign_gratings(np.append(merged, [np.nan, 9.0]), {"PRISM": prism, "G140M": g140})
    assert list(lab) == [0, 0, 0, 1, 1, 0, 0, -1, -1]          # 1.0 tie -> first grid
    assert list(ns.assign_gratings([1.0], {"one": [1.0]})) == [0]
    assert list(ns.assign_gratings([1.0], {})) == [-1]
    assert ns.assign_gratings([], {"a": [1.0]}).size == 0


def test_assign_gratings_rounded_counts_and_values():
    # legacy specdata: wavelengths rounded to 5 decimals, so two grids can share a rounded key
    a = np.array([1.000001, 1.000003, 2.0])
    b = np.array([1.000004, 3.0])
    fa, fb = np.array([10.0, 11.0, 12.0]), np.array([20.0, 30.0])
    w = np.round(np.concatenate([a, b]), 5)
    f = np.concatenate([fa, fb])
    o = np.argsort(w, kind="stable")
    w, f = w[o], f[o]
    lab = ns.assign_gratings(w, {"A": a, "B": b}, decimals=5, values=(f, {"A": fa, "B": fb}))
    assert np.bincount(lab[lab >= 0], minlength=2).tolist() == [3, 2]       # each grid used its count
    assert list(lab[f == 20.0]) == [1] and list(lab[f == 10.0]) == [0]       # flux tie-break
    # count-aware without values; surplus duplicates, NaN, inf and unicode keys -> -1 when exhausted
    lab2 = ns.assign_gratings([1.0, 1.0, 1.0, np.nan, np.inf], {"α": [1.0], "β": [1.0000004]}, decimals=5)
    assert sorted(lab2[:2].tolist()) == [0, 1] and list(lab2[2:]) == [-1, -1, -1]
    big = ns.assign_gratings([1e30], {"g": [1e30]}, decimals=5)
    assert list(big) == [0]


# -- s2d -----------------------------------------------------------------------------------------
def _block(lo, hi, n, ny, val):
    return np.full((ny, n), val, np.float32), np.linspace(lo, hi, n)


def test_stitch_s2d_fill_and_pad():
    dp, wp = _block(0.55, 5.38, 50, 31, 1.0)
    d1, w1 = _block(0.96, 1.90, 40, 29, 2.0)
    d3, w3 = _block(2.85, 5.29, 40, 31, 3.0)
    w1[5] = np.nan                                             # NaN column dropped
    im = ns.stitch_s2d([("G395M", d3, w3), ("PRISM", dp, wp), ("G140M", d1, w1)])
    assert isinstance(im, Image2D)
    assert im.data.shape == (31, len(im.wave)) and len(im.y) == 31
    assert np.all(np.diff(im.wave) > 0) and np.all(np.isfinite(im.wave))
    assert [s[2] for s in im.segments] == ["PRISM", "G140M", "PRISM", "G395M", "PRISM"]
    assert im.segments[0][0] == 0 and im.segments[-1][1] == len(im.wave)
    i0, i1, _ = im.segments[1]
    assert np.all(np.isnan(im.data[0, i0:i1])) and np.all(im.data[1:30, i0:i1] == 2.0)   # centred pad
    assert im.y[0] == -15 and im.y[-1] == 15
    # explicit 6927-style cuts; unknown labels skipped; nothing left -> None
    im2 = ns.stitch_s2d([("G140M", d1, w1), ("G395M", d3, w3)], cuts=ns.overlap_cuts(["G140M", "G395M"]))
    assert [s[2] for s in im2.segments] == ["G140M", "G395M"]
    assert ns.stitch_s2d([("G140M", d1, w1)], cuts=[("G395M", 0, 99)]) is None
    assert ns.stitch_s2d([]) is None
    nan = np.full(4, np.nan)
    assert ns.stitch_s2d([("PRISM", np.ones((3, 4)), nan)]) is None
    with pytest.raises(ValueError):
        ns.stitch_s2d([("PRISM", np.ones((3, 4)), np.arange(5.0))])
    # a repeated label keeps its first block, with a warning
    with pytest.warns(RuntimeWarning, match="repeated labels"):
        im3 = ns.stitch_s2d([("PRISM", dp, wp), ("PRISM", dp * 2, wp)])
    assert im3.segments == [(0, 50, "PRISM")] and np.nanmax(im3.data) == 1.0


def test_stitch_s2d_single_and_non_monotonic():
    d = np.arange(12, dtype=np.float32).reshape(1, 12)
    w = np.array([1, 2, 2, 3, 2.5, 4, 5, 6, 7, 8, 9, 1e30])
    im = ns.stitch_s2d([("PRISM", d, w)])
    assert im.data.shape[0] == 1 and np.all(np.diff(im.wave) > 0)
    assert list(im.wave[:5]) == [1, 2, 2.5, 3, 4]
    # 1e30 um lies beyond the 99 um outer edge of every cut
    assert im.wave[-1] == 9.0 and im.segments == [(0, len(im.wave), "PRISM")]
    big = ns.stitch_s2d([("X", d, np.linspace(1e5, 2e5, 12))], cuts=[("X", 0.0, np.inf)])
    assert big.wave[-1] == 2e5 and big.data.shape == (1, 12)


def test_read_s2d(tmp_path):
    from astropy.io import fits
    sci = np.arange(12, dtype=np.float32).reshape(3, 4)
    wl = np.tile(np.array([1.0, 2.0, 3.0, np.nan]), (3, 1))
    wl[0, 0] = np.nan
    p = tmp_path / "é_s2d.fits"
    fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(sci, name="SCI"), fits.ImageHDU(wl, name="WAVELENGTH")]).writeto(p)
    d, w = ns.read_s2d(p)
    assert d.dtype == np.float32 and d.shape == (3, 4)
    assert list(w[:3]) == [1.0, 2.0, 3.0] and np.isnan(w[3])
    bad = tmp_path / "bad.fits"
    fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(sci, name="SCI"),
                  fits.ImageHDU(np.ones((3, 5)), name="WAVELENGTH")]).writeto(bad)
    with pytest.raises(ValueError):
        ns.read_s2d(bad)


# -- clean_flux ----------------------------------------------------------------------------------
def test_clean_flux_nan_inf_large():
    f = np.array([0.1, np.nan, np.inf, -np.inf, -0.2, 2.0, 0.1, 1e30, 0.11, 0.09])
    e = np.array([0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 5.0, 0.01, np.nan, 0.01])
    out = ns.clean_flux(f, e)
    assert np.isnan(out[[1, 2, 3, 4, 5, 6, 7]]).all()
    assert np.allclose(out[[0, 8, 9]], [0.1, 0.11, 0.09])
    g = np.full(1000, 0.01)
    g[500] = 0.9
    assert np.isnan(ns.clean_flux(g, np.zeros(1000))[500])
    assert ns.clean_flux(np.zeros(0), np.zeros(0)).size == 0
    assert np.isnan(ns.clean_flux([np.nan], [np.nan])).all()
    assert ns.clean_flux([5.0], [0.0], max_flux=10.0)[0] == 5.0
    src = np.array([0.1, 0.2])
    ns.clean_flux(src, [2.0, 2.0])
    assert list(src) == [0.1, 0.2]                             # input untouched
    with pytest.raises(ValueError):
        ns.clean_flux([1.0, 2.0], [1.0])
