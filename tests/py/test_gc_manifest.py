"""portalkit.legacy.gc_manifest: GC schema-1 manifold manifests referenced in place."""

import gzip
import json
from pathlib import Path

import numpy as np
import pytest

from portalkit.bundle import codec
from portalkit.bundle.writer import BundleWriter, Ledger
from portalkit.legacy import gc_manifest as gm
from portalkit.schema import validate_catalog
from portalkit.spec import CatalogSpec, ForeignRef

REAL = Path("/orange/adamginsburg/jwst/gc-treasury/manifold_prototype/data/manifest.json")
N = 50


def _gc_tree(root: Path, *, n: int = N, tile_dtype: str = "u8") -> Path:
    """A tiny GC schema-1 tree: 2 dims, one error column, i16 offsets, u8 tile index."""
    rng = np.random.default_rng(4)
    for d in ("dims", "errs", "meta"):
        (root / d).mkdir(parents=True, exist_ok=True)
    refs = [{"file": "tables/o001.fits", "ref_ra": 266.4, "ref_dec": -28.9},
            {"file": "tables/o002.fits", "ref_ra": 266.6, "ref_dec": -29.1}]
    tile = (np.arange(n) % 2).astype(np.uint8)
    (root / "meta" / "tile.u8.gz").write_bytes(gzip.compress(tile.tobytes()))
    dra = rng.integers(-2000, 2000, n).astype("<i2")
    ddec = rng.integers(-2000, 2000, n).astype("<i2")
    (root / "meta" / "dra.i16.gz").write_bytes(gzip.compress(codec.shuffle_i16(dra)))
    (root / "meta" / "ddec.i16.gz").write_bytes(gzip.compress(codec.shuffle_i16(ddec)))
    for k in ("f212n", "f480m"):
        (root / "dims" / f"{k}.u16.gz").write_bytes(gzip.compress(codec.shuffle_u16(
            rng.integers(1, 65535, n).astype("<u2"))))
    (root / "errs" / "f212n.u8.gz").write_bytes(gzip.compress(rng.integers(1, 255, n).astype(np.uint8).tobytes()))
    m = {"schema": 1, "n": n, "title": "GC test", "created": "2026-10-01", "seed": 7,
         "encoding": {"dims": "u16-shuffle-gzip", "missing": 0, "meta_offsets": "i16-shuffle-gzip",
                      "offset_step_mas": 8.0},
         "dims": [{"key": "f212n", "label": "F212N", "unit": "mag", "group": "nir", "file": "dims/f212n.u16.gz",
                   "min": 10.0, "max": 25.0, "center": 17.0, "scale": 1.5, "nvalid": n,
                   "quantiles": list(np.linspace(10, 25, 101))},
                  {"key": "f480m", "label": "F480M", "unit": "mag", "group": "mir", "file": "dims/f480m.u16.gz",
                   "min": 9.0, "max": 22.0, "desc": "long wave"}],
         "errs": [{"dim_key": "f212n", "file": "errs/f212n.u8.gz", "lo": 0.001, "hi": 1.0}],
         "meta": {"dra": {"file": "meta/dra.i16.gz", "dtype": "i16"},
                  "ddec": {"file": "meta/ddec.i16.gz", "dtype": "i16"},
                  "tile": {"file": "meta/tile.u8.gz", "dtype": tile_dtype}},
         "tile_refs": refs, "caveats": ["synthetic"], "source": {"paths": ["a.fits"]}}
    (root / "manifest.json").write_text(json.dumps(m))
    return root / "manifest.json"


def test_read_manifest_checks(tmp_path):
    mp = _gc_tree(tmp_path / "gc")
    m = gm.read_manifest(mp)
    assert m["n"] == N
    bad = dict(m, encoding={"dims": "f32"})
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(gm.GcManifestError, match="encoding"):
        gm.read_manifest(p)
    p.write_text(json.dumps({k: v for k, v in m.items() if k != "tile_refs"}))
    with pytest.raises(gm.GcManifestError, match="tile_refs"):
        gm.read_manifest(p)
    p.write_text("{not json")
    with pytest.raises(gm.GcManifestError):
        gm.read_manifest(p)
    p.write_text(json.dumps(dict(m, schema=2)))
    with pytest.raises(gm.GcManifestError, match="schema"):
        gm.read_manifest(p)


def test_convert_columns(tmp_path):
    m = gm.read_manifest(_gc_tree(tmp_path / "gc"))
    man = gm.convert(m, ds="gc", url_base="https://example.org/gc/data/", tile_file="pos/tile.u16.gz",
                     build="20261010T000000Z-0123abcd")
    assert validate_catalog(man) == []
    cols = {c["key"]: c for c in man["cols"]}
    assert cols["f212n"]["enc"] == "u16q" and cols["f212n"]["role"] == "mag"
    assert cols["f212n"]["file"] == "https://example.org/gc/data/dims/f212n.u16.gz"
    assert cols["f212n"]["lo"] == 10.0 and cols["f212n"]["hi"] == 25.0
    assert cols["f212n"]["step"] == pytest.approx(15.0 / codec.QMAX)
    assert cols["f212n"]["err"] == "e_f212n" and cols["e_f212n"]["enc"] == "u8log"
    assert cols["e_f212n"]["step"] == pytest.approx(3.0 / codec.QMAX_U8)
    assert len(cols["f212n"]["quantiles"]) == 101
    assert cols["f480m"]["note"] == "long wave"
    assert cols["tile"]["enc"] == "cat8" and cols["tile"]["levels"] == ["o001.fits", "o002.fits"]
    assert man["pos"]["enc"] == "i16off" and man["pos"]["files"]["tile"] == "pos/tile.u16.gz"
    assert man["pos"]["tile_refs"] == [[266.4, -28.9], [266.6, -29.1]]
    assert man["order"] == {"kind": "input", "seed": 7}
    assert man["manifold"]["dims"] == ["f212n", "f480m"]
    assert [g["key"] for g in man["groups"]] == ["nir", "mir"]
    assert man["provenance"]["foreign"]["kind"] == "gc-manifest-1"
    assert man["provenance"]["gc_source"] == {"paths": ["a.fits"]}
    assert man["caveats"] == ["synthetic"]


def test_convert_needs_tile_file_for_u8(tmp_path):
    m = gm.read_manifest(_gc_tree(tmp_path / "gc"))
    with pytest.raises(gm.GcManifestError, match="u16"):
        gm.convert(m, ds="gc", url_base="https://example.org/", tile_file=None)
    m16 = gm.read_manifest(_gc_tree(tmp_path / "gc16", tile_dtype="u16"))
    man = gm.convert(m16, ds="gc", url_base="https://example.org/", tile_file=None)
    assert man["pos"]["files"]["tile"] == "https://example.org/meta/tile.u8.gz"
    assert "tile" not in {c["key"] for c in man["cols"]}


def test_relative_url_base_rules(tmp_path):
    m = gm.read_manifest(_gc_tree(tmp_path / "gc"))
    with pytest.raises(gm.GcManifestError, match="'..'"):
        gm.convert(m, ds="gc", url_base="../gc/data", tile_file="t.gz")
    man = gm.convert(m, ds="gc", url_base="gcdata", tile_file="t.gz")
    assert man["cols"][0]["file"] == "gcdata/dims/f212n.u16.gz"


def test_tile_u16_bytes_roundtrip():
    tile = np.array([0, 1, 1, 0, 2], dtype=np.uint8)
    out = gm.tile_u16_bytes(gzip.compress(tile.tobytes()), 5)
    back = np.frombuffer(codec.unshuffle(codec.gunzip(out), 2), dtype="<u2")
    assert back.tolist() == [0, 1, 1, 0, 2]
    assert gm.tile_u16_bytes(tile.tobytes(), 5) == out
    with pytest.raises(gm.GcManifestError, match="rows"):
        gm.tile_u16_bytes(gzip.compress(tile.tobytes()), 6)


def test_write_from_spec_positions_decode(tmp_path):
    root = tmp_path / "gc"
    mp = _gc_tree(root)
    spec = CatalogSpec(key="gc", table=None, id="", columns=[], title="GC field",
                       caveats=["adapter caveat"],
                       foreign=ForeignRef(kind="gc-manifest-1", url_base="https://example.org/gc/",
                                          manifest_path=mp))
    ledger = Ledger()
    dest = tmp_path / "out"
    res = gm.write(spec, BundleWriter(dest, "data/gc", ledger), ledger=ledger)
    man = json.loads((dest / "data/gc/manifest.json").read_text())
    assert man == res.manifest
    assert validate_catalog(man) == []
    assert man["title"] == "GC field"
    assert man["caveats"] == ["synthetic", "adapter caveat"]
    assert res.ids == [] and "manifest.json" in res.files
    tile_rel = man["pos"]["files"]["tile"]
    assert (dest / "data/gc" / tile_rel).is_file()
    # decode positions with the widened tile file and the original offsets
    files = {"tile": (dest / "data/gc" / tile_rel).read_bytes(),
             "dra": (root / "meta/dra.i16.gz").read_bytes(),
             "ddec": (root / "meta/ddec.i16.gz").read_bytes()}
    ra, dec = codec.decode_pos(man["pos"], files)
    assert ra.shape == (N,)
    assert np.all(np.abs(dec[0::2] + 28.9) < 0.01) and np.all(np.abs(dec[1::2] + 29.1) < 0.01)
    led = ledger.to_dict()
    assert led["datasets"]["gc"]["total_bytes"] > 0


def test_write_from_path_requires_ds_and_url(tmp_path):
    mp = _gc_tree(tmp_path / "gc")
    with pytest.raises(gm.GcManifestError, match="ds"):
        gm.write(mp, tmp_path / "o")
    with pytest.raises(gm.GcManifestError, match="url_base"):
        gm.write(mp, tmp_path / "o", ds="gc")
    logs = []
    res = gm.write(mp, tmp_path / "o", ds="gc", url_base="https://example.org/", log=logs.append)
    assert (tmp_path / "o" / "manifest.json").is_file() and logs
    assert res.manifest["id"] == "gc"


def test_missing_tile_file(tmp_path):
    mp = _gc_tree(tmp_path / "gc")
    (tmp_path / "gc" / "meta" / "tile.u8.gz").unlink()
    with pytest.raises(gm.GcManifestError, match="tile index"):
        gm.write(mp, tmp_path / "o", ds="gc", url_base="https://example.org/")


@pytest.mark.slow
@pytest.mark.skipif(not REAL.is_file(), reason="GC Treasury manifold not available")
def test_real_treasury_manifest(tmp_path):
    res = gm.write(REAL, tmp_path / "o", ds="gc", url_base="https://example.org/treasury/data/")
    man = res.manifest
    assert validate_catalog(man) == []
    assert man["n"] == gm.read_manifest(REAL)["n"]
    assert {c["key"] for c in man["cols"]} >= {"f212n", "f480m"}
