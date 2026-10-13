"""portalkit.legacy.gc_manifest: GC schema-1 manifold manifests referenced in place."""

import gzip
import json
from pathlib import Path

import numpy as np
import pytest

from portalkit.build import PathLabeler
from portalkit.bundle import codec
from portalkit.bundle.writer import BundleWriter, Ledger
from portalkit.legacy import gc_manifest as gm
from portalkit.schema import validate_catalog
from portalkit.spec import CatalogSpec, ColumnSpec, ForeignRef, Group, ManifoldSpec, Preset, VirtualDim

REAL = Path("/orange/adamginsburg/jwst/gc-treasury/manifold_prototype/data/manifest.json")
N = 50


def _gc_tree(root: Path, *, n: int = N, tile_dtype: str = "u8", source: dict | None = None) -> Path:
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
         "tile_refs": refs, "caveats": ["synthetic"],
         "source": source if source is not None else {"paths": ["a.fits"]}}
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


def _full_spec(**kw) -> CatalogSpec:
    """A GC-foreign CatalogSpec carrying presets/virtual/groups/manifold/column-filter/id
    metadata, the way a real adapter (e.g. ``adapters/gc_manifold.py``) builds one."""
    defaults = dict(
        key="gc", table=None, id="", columns=[
            ColumnSpec("f212n", "mag", filter="JWST/NIRCam.F212N"),
            ColumnSpec("f480m", "mag", filter="JWST/NIRCam.F480M"),
        ],
        virtual=[VirtualDim("f212n_f480m", label="F212N-F480M", unit="mag", group="phot")],
        groups=[Group("phot", "NIRCam Vega magnitudes", ["f212n", "f480m"])],
        presets=[Preset("cmd", "CMD", "axes", {"x": "f212n_f480m", "y": "f480m"}),
                Preset("red_clump", "Red clump", "axes", {"x": "f212n_f480m", "y": "f480m",
                                                           "sel": "f212n<16"})],
        manifold=ManifoldSpec(dims=["f212n", "f480m"], frames={"pca": [0.1, 0.2, 0.3, 0.4]},
                              default_frame="umap"),
        foreign=ForeignRef(kind="gc-manifest-1", url_base="https://example.org/gc/",
                           manifest_path=Path("/dev/null")))
    defaults.update(kw)
    return CatalogSpec(**defaults)


def test_convert_merges_catalogspec_metadata(tmp_path):
    m = gm.read_manifest(_gc_tree(tmp_path / "gc"))
    spec = _full_spec()
    man = gm.convert(m, ds="gc", url_base="https://example.org/gc/data/", tile_file="pos/tile.u16.gz",
                     spec=spec)
    assert validate_catalog(man) == []
    assert man["presets"] == [{"key": "cmd", "label": "CMD", "kind": "axes",
                               "x": "f212n_f480m", "y": "f480m"},
                              {"key": "red_clump", "label": "Red clump", "kind": "axes",
                               "x": "f212n_f480m", "y": "f480m", "sel": "f212n<16"}]
    assert man["virtual"] == [{"key": "f212n_f480m", "label": "F212N-F480M", "unit": "mag",
                               "group": "phot"}]
    # explicit "phot" group plus the GC-dim-derived "nir"/"mir" groups (no key collision)
    assert [g["key"] for g in man["groups"]] == ["phot", "nir", "mir"]
    assert man["manifold"]["default_frame"] == "umap"
    assert man["manifold"]["frames"] == {"pca": [0.1, 0.2, 0.3, 0.4]}
    cols = {c["key"]: c for c in man["cols"]}
    assert cols["f212n"]["filter"] == "JWST/NIRCam.F212N"
    assert cols["f480m"]["filter"] == "JWST/NIRCam.F480M"
    # unrelated columns (error, tile) are untouched
    assert "filter" not in cols["e_f212n"] and "filter" not in cols["tile"]


def test_convert_groups_explicit_key_wins_over_auto(tmp_path):
    m = gm.read_manifest(_gc_tree(tmp_path / "gc"))
    spec = _full_spec(groups=[Group("nir", "Near-IR (explicit)", ["f212n"])])
    man = gm.convert(m, ds="gc", url_base="https://example.org/", tile_file="t.gz", spec=spec)
    by_key = {g["key"]: g for g in man["groups"]}
    assert by_key["nir"] == {"key": "nir", "label": "Near-IR (explicit)", "dims": ["f212n"]}
    assert by_key["mir"] == {"key": "mir", "label": "mir", "dims": ["f480m"]}


def test_convert_id_column_marks_role(tmp_path):
    m = gm.read_manifest(_gc_tree(tmp_path / "gc"))
    spec = _full_spec(id="f212n")
    man = gm.convert(m, ds="gc", url_base="https://example.org/", tile_file="t.gz", spec=spec)
    cols = {c["key"]: c for c in man["cols"]}
    assert cols["f212n"]["role"] == "id"
    # no id configured (default "") leaves roles as GC-derived
    man2 = gm.convert(m, ds="gc", url_base="https://example.org/", tile_file="t.gz", spec=_full_spec())
    assert {c["key"]: c for c in man2["cols"]}["f212n"]["role"] == "mag"


def test_convert_without_spec_is_unchanged(tmp_path):
    """Regression guard: omitting ``spec`` (the pre-existing call shape) must not merge any
    column filter/id, and must still use the auto-derived groups/default manifold."""
    m = gm.read_manifest(_gc_tree(tmp_path / "gc"))
    man = gm.convert(m, ds="gc", url_base="https://example.org/", tile_file="t.gz")
    assert "presets" not in man and "virtual" not in man
    assert "filter" not in {c["key"]: c for c in man["cols"]}["f212n"]
    assert man["manifold"]["default_frame"] == "pca" and "frames" not in man["manifold"]


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


def test_write_labeler_scrubs_provenance_paths(tmp_path):
    """Without a labeler, ``provenance.foreign.manifest`` (and an absolute path embedded in
    a ``gc_*`` field) is a raw local path -- the WPE9#1 leak that fails
    ``portalkit check --gate G2`` for every gc_manifold portal. With one, both are labelled."""
    root = tmp_path / "gc"
    abs_note = str(root / "notes" / "cal.txt")
    mp = _gc_tree(root, source={"origin": abs_note})
    spec = CatalogSpec(key="gc", table=None, id="", columns=[],
                       foreign=ForeignRef(kind="gc-manifest-1", url_base="https://example.org/gc/",
                                          manifest_path=mp))
    ledger = Ledger()
    unlabelled = gm.write(spec, BundleWriter(tmp_path / "out1", "data/gc", ledger), ledger=ledger).manifest
    assert unlabelled["provenance"]["foreign"]["manifest"] == str(mp)
    assert unlabelled["provenance"]["gc_source"] == {"origin": abs_note}

    labeler = PathLabeler([("gc_root", str(root))])
    ledger2 = Ledger()
    labelled = gm.write(spec, BundleWriter(tmp_path / "out2", "data/gc", ledger2), ledger=ledger2,
                        labeler=labeler).manifest
    assert validate_catalog(labelled) == []
    foreign_manifest = labelled["provenance"]["foreign"]["manifest"]
    assert foreign_manifest == f"gc_root:manifest.json"
    assert not foreign_manifest.startswith("/")
    scrubbed_origin = labelled["provenance"]["gc_source"]["origin"]
    assert scrubbed_origin == "gc_root:notes/cal.txt"
    assert not scrubbed_origin.startswith("/")


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
