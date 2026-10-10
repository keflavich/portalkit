"""Row order / LOD permutation (portalkit.bundle.order, ARCHITECTURE.md section 5.4)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from portalkit.bundle import codec, order
from portalkit.constants import LOD_BASE, LOD_MIN_N


def ks_uniform_pvalue(sample: np.ndarray, n_total: int) -> float:
    """Two-sided one-sample KS test of ``sample / n_total`` against U(0, 1) (no scipy).

    Uses the asymptotic Kolmogorov distribution with the Stephens correction.
    """
    u = np.sort(np.asarray(sample, dtype=np.float64) / n_total)
    n = u.size
    i = np.arange(1, n + 1)
    d = max(np.max(i / n - u), np.max(u - (i - 1) / n))
    en = math.sqrt(n)
    lam = (en + 0.12 + 0.11 / en) * d
    p = 0.0
    for j in range(1, 101):
        p += 2 * (-1) ** (j - 1) * math.exp(-2 * j * j * lam * lam)
    return min(max(p, 0.0), 1.0)


def test_lod_levels():
    assert order.lod_levels(0) == []
    assert order.lod_levels(1) == [1]
    assert order.lod_levels(LOD_BASE) == [LOD_BASE]
    assert order.lod_levels(LOD_BASE + 1) == [LOD_BASE, LOD_BASE + 1]
    assert order.lod_levels(464489) == [32768, 65536, 131072, 262144, 464489]
    lv = order.lod_levels(5_000_000)
    assert lv[-1] == 5_000_000 and all(b == 2 * a for a, b in zip(lv[:-2], lv[1:-1]))


def test_choose_kind():
    assert order.choose_kind(LOD_MIN_N) == "input"
    assert order.choose_kind(LOD_MIN_N + 1) == "lod"
    assert order.choose_kind(10, "lod") == "lod"
    assert order.choose_kind(0, "lod") == "input"
    assert order.choose_kind(10**7, "input") == "input"
    with pytest.raises(ValueError):
        order.choose_kind(10, "random")


@pytest.mark.parametrize("n", [0, 1, 100])
def test_input_order_identity(n):
    o = order.make_order(n)
    assert o.kind == "input" and o.is_identity
    assert o.manifest() == {"kind": "input"}
    np.testing.assert_array_equal(o.stored_to_input, np.arange(n))
    a = np.arange(n) * 2
    assert o.apply(a) is a


def test_lod_permutation_is_seeded_and_complete():
    n = LOD_MIN_N + 5000
    a = order.make_order(n, seed=11)
    b = order.make_order(n, seed=11)
    c = order.make_order(n, seed=12)
    np.testing.assert_array_equal(a.stored_to_input, b.stored_to_input)
    assert not np.array_equal(a.stored_to_input, c.stored_to_input)
    assert np.array_equal(np.sort(a.stored_to_input), np.arange(n))
    inv = a.input_to_stored
    np.testing.assert_array_equal(a.stored_to_input[inv], np.arange(n))
    m = a.manifest()
    assert m == {"kind": "lod", "seed": 11, "levels": [32768, 65536, 131072, n]}
    assert a.apply(list(range(n)))[:5] == a.stored_to_input[:5].tolist()


def test_lod_prefixes_are_uniform_ks():
    n = 200_000
    o = order.make_order(n, mode="lod", seed=20261010)
    for stop in o.levels[:-1]:
        p = ks_uniform_pvalue(o.stored_to_input[:stop], n)
        assert p > 0.01, (stop, p)
    # and a clearly non-uniform prefix fails the same test
    assert ks_uniform_pvalue(np.arange(LOD_BASE), n) < 1e-6


def test_lod_sort_within_levels_tile_morton():
    rng = np.random.default_rng(3)
    n = LOD_MIN_N + 10
    ra = rng.uniform(10, 12, n)
    dec = rng.uniform(-1, 1, n)
    ra[5] = np.nan
    p = codec.encode_i16off(ra, dec)
    key = order.tile_morton_key(p["tile"], p["dra"], p["ddec"])
    assert key[5] == np.uint64(0xFFFFFFFFFFFFFFFF)
    o = order.make_order(n, seed=1, sort_key=key)
    assert o.sort == "tile-morton" and o.manifest()["sort"] == "tile-morton"
    plain = order.make_order(n, seed=1)
    start = 0
    for stop in o.levels:
        seg = o.stored_to_input[start:stop]
        assert np.all(key[seg][1:] >= key[seg][:-1])
        # same rows per level as the unsorted permutation (the sort does not change the levels)
        assert set(seg.tolist()) == set(plain.stored_to_input[start:stop].tolist())
        start = stop
    # the row without position is last within its level
    lvl_of_5 = int(np.searchsorted(np.array(o.levels), int(o.input_to_stored[5]), side="right"))
    end = o.levels[lvl_of_5]
    assert o.stored_to_input[end - 1] == 5
    with pytest.raises(ValueError):
        order.make_order(n, sort_key=key[:-1])


def test_src_index_key_constant():
    assert order.SRC_INDEX_KEY == "_src_index"


def ks_2samp_pvalue(a: np.ndarray, b: np.ndarray) -> float:
    """Two-sample KS test p-value (asymptotic Kolmogorov distribution, Stephens correction)."""
    a = np.sort(np.asarray(a, dtype=np.float64))
    b = np.sort(np.asarray(b, dtype=np.float64))
    allv = np.concatenate([a, b])
    d = np.max(np.abs(np.searchsorted(a, allv, side="right") / a.size
                      - np.searchsorted(b, allv, side="right") / b.size))
    en = math.sqrt(a.size * b.size / (a.size + b.size))
    lam = (en + 0.12 + 0.11 / en) * d
    p = 0.0
    for j in range(1, 101):
        p += 2 * (-1) ** (j - 1) * math.exp(-2 * j * j * lam * lam)
    return min(max(p, 0.0), 1.0)


def test_lod_prefix_column_distributions_ks(tmp_path):
    """Section 13 Level 0: each LOD prefix of 3 written columns matches the full distribution."""
    from types import SimpleNamespace

    from astropy.table import Table

    from portalkit.bundle import catalog

    n = LOD_MIN_N + 3000
    rng = np.random.default_rng(13)
    t = Table()
    t["id"] = np.arange(n)
    t["m"] = rng.normal(18.0, 1.5, n)                    # mag-like
    t["f"] = 10 ** rng.uniform(-5, 0, n)                 # log-uniform flux
    t["v"] = np.sort(rng.exponential(2.0, n))            # sorted input: order must not leak in

    def col(key, role):
        return SimpleNamespace(key=key, role=role, enc="auto", exact=True)

    spec = SimpleNamespace(key="ks", table=t, id="id", ra=None, dec=None, exact_rows=False,
                           columns=[col("m", "mag"), col("f", "flux"), col("v", "value")],
                           presets=[], density=[], order="auto")
    res = catalog.write(spec, tmp_path)
    man = res.manifest
    assert man["order"]["kind"] == "lod"
    cols = {c["key"]: c for c in man["cols"]}
    src = codec.decode_file(cols[order.SRC_INDEX_KEY],
                            (tmp_path / cols[order.SRC_INDEX_KEY]["file"]).read_bytes(), n)
    np.testing.assert_array_equal(np.sort(src.astype(np.int64)), np.arange(n))
    for key in ("m", "f", "v"):
        meta = cols[key]
        vals = codec.dequant(meta, codec.decode_file(meta, (tmp_path / meta["file"]).read_bytes(), n))
        # decoded values in stored order are the input values reordered by _src_index
        np.testing.assert_allclose(vals, np.asarray(t[key])[src.astype(np.int64)],
                                   rtol=1e-3, atol=1e-3)
        for stop in man["order"]["levels"][:-1]:
            p = ks_2samp_pvalue(vals[:stop], vals)
            assert p > 0.01, (key, stop, p)
    # an input-order prefix of the sorted column clearly fails the same test
    assert ks_2samp_pvalue(np.asarray(t["v"])[:LOD_BASE], np.asarray(t["v"])) < 1e-6
