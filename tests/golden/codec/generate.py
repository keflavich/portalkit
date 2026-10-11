"""Generate the codec golden files (bytes + expected decoded values).

Run from the repo root::

    python tests/golden/codec/generate.py          # rewrite the goldens
    python tests/golden/codec/generate.py --check  # exit 1 if any stored byte differs

``index.json`` (``pk-golden-codec/1``) lists one case per file::

    {"name", "kind", "file", "meta", "n", "codes"?, "expected", "tol"?, ...}

* ``kind = "column"``: ``file`` is a column file; ``meta`` is the manifest ``cols[]`` entry
  subset needed to decode (``enc``, ``lo``, ``hi``, ``step``, ``shuffle``, ``levels``).
  ``codes`` are the raw codes after gunzip + unshuffle; ``expected`` the decoded values
  (null = missing; strings for ``str``/``cat*``; 0/1 for ``bits``). ``tol`` is the absolute
  tolerance for decoded floats (round-off only; decoding is exact up to float64 arithmetic).
* ``kind = "pos"``: ``files`` maps ``ra``/``dec`` (u32lin) or ``tile``/``dra``/``ddec``
  (i16off) to files; ``meta`` is the manifest ``pos`` block, ``bbox`` the manifest bbox;
  ``expected`` is ``{"ra": [...], "dec": [...]}``; ``tol_deg`` is the decode tolerance
  relative to the encoded input positions (``input``).
* ``kind = "sparse"``: ``expected`` is ``{"idx": [...], "kind": [...]}``.
* ``kind = "pks1"`` / ``"pki1"``: a record file (one gzip member); ``expected`` holds the
  header and decoded arrays.
* ``kind = "shard"``: concatenated gzip members; ``entries`` are ``[shard, off, len]`` per id,
  ``expected`` the decoded JSON records.
* ``kind = "density"``: ``meta`` is the manifest density entry; ``codes`` the flat grid
  codes (row-major, ``iy * nx + ix``); ``expected`` the decoded counts.
* ``kind = "rows"``: a row chunk (gz JSON array of row objects); ``expected`` is that array.
* ``kind = "catalog_chunks"``: a chunked catalog with ``chunk_starts`` (IMPLEMENTATION_NOTES 98);
  ``manifest`` is the ``pk-catalog/1`` subset (``n``, ``chunk_rows``, ``chunk_starts``, ``ids``,
  ``cols`` with ``chunk_files``), ``files`` maps labels to every chunk file, ``expected`` is
  ``{"ids": [...], "cols": {key: [decoded values in stored order]}}``.

The byte-identity test (``tests/py/test_codec.py``) calls :func:`build` and compares every
file with the stored one.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from portalkit.bundle import codec, density, records  # noqa: E402
from portalkit.bundle.writer import dumps_json  # noqa: E402

SCHEMA = "pk-golden-codec/1"


def _nulls(a) -> list:
    out = []
    for v in np.asarray(a, dtype=np.float64).tolist():
        out.append(v if math.isfinite(v) else None)
    return out


def _col(name: str, enc_obj: codec.Encoded, tol: float, note: str = "") -> tuple[str, bytes, dict]:
    data = enc_obj.file_bytes()
    meta = {k: v for k, v in enc_obj.meta.items() if k in ("enc", "lo", "hi", "step", "shuffle", "levels", "clip")}
    n = int(len(enc_obj.codes))
    if meta["enc"] in ("cat8", "cat16"):
        miss = codec.CAT8_MISSING if meta["enc"] == "cat8" else codec.CAT16_MISSING
        expected = [None if int(c) == miss else meta["levels"][int(c)] for c in enc_obj.codes]
    elif meta["enc"] == "bits":
        expected = [int(c) for c in enc_obj.codes]
    else:
        expected = _nulls(codec.dequant(meta, enc_obj.codes))
    case = {"name": name, "kind": "column", "file": f"{name}.bin", "meta": meta, "n": n,
            "codes": [int(c) if meta["enc"] != "f32" else None for c in enc_obj.codes]
            if meta["enc"] != "f32" else None,
            "expected": expected, "tol": tol}
    if case["codes"] is None:
        del case["codes"]
    if enc_obj.limits:
        case["limits"] = {str(k): v for k, v in sorted(enc_obj.limits.items())}
    if note:
        case["note"] = note
    return f"{name}.bin", data, case


def build() -> tuple[dict[str, bytes], dict]:
    """Return ({file name: bytes}, index document)."""
    files: dict[str, bytes] = {}
    cases: list[dict] = []

    def add(fname: str | None, data: bytes | None, case: dict | None) -> None:
        if fname is not None:
            files[fname] = data
        if case is not None:
            cases.append(case)

    # ---- u16q (free step, NaN, inf, large values)
    x = np.array([1.0, 2.5, np.nan, -3.25, 1e6, np.inf, 7.0, 0.0])
    add(*_col("u16q_basic", codec.encode_u16q(x), 1e-6,
              "free step; NaN and inf -> missing (code 0)"))
    # ---- u16q mag: fixed step 0.001, lo rounded down
    m = np.array([12.3456, 18.0, np.nan, 25.999, 9.0011])
    add(*_col("u16q_mag", codec.encode_u16q(m, step=0.001), 1e-9,
              "fixed step 0.001; hi = lo + 65534*step"))
    # ---- u16q saturate with adapter bounds -> clip limits 3/4
    s = np.array([-5.0, 0.0, 0.5, 1.0, 9.0, np.nan])
    add(*_col("u16q_saturate", codec.encode_u16q(s, lo=0.0, hi=1.0, clip="saturate"), 1e-9,
              "clip saturate to [0, 1]; limits 3 = clipped low, 4 = clipped high"))
    # ---- u16q drop
    add(*_col("u16q_drop", codec.encode_u16q(s, lo=0.0, hi=1.0, clip="drop"), 1e-9,
              "clip drop: out-of-range values are missing"))
    # ---- degenerate span (single value)
    add(*_col("u16q_const", codec.encode_u16q(np.array([4.2, 4.2, np.nan])), 1e-12,
              "degenerate span: hi == lo, step = 1e-6*max(1,|lo|)"))
    # ---- u16log
    f = np.array([1e-3, 0.5, 10.0, np.nan, 0.0, -1.0, 3e4])
    add(*_col("u16log_basic", codec.encode_u16log(f), 1e-9,
              "lo/hi/step in log10; x <= 0 missing; tol is relative (|d|/x)"))
    cases[-1]["tol_rel"] = 1e-9
    # ---- u8q
    add(*_col("u8q_basic", codec.encode_u8q(np.array([0.0, 0.25, 1.0, np.nan, 0.5])), 1e-9,
              "step = (hi-lo)/254"))
    # ---- u8log
    e = np.array([0.01, 0.02, 0.1, np.nan, 0.05, 0.0])
    add(*_col("u8log_basic", codec.encode_u8log(e), 1e-9,
              "GC log_quantize; lo/hi linear; decode 10**(log lo + (c-1)/254*(log hi - log lo))"))
    cases[-1]["tol_rel"] = 1e-9
    # ---- cat8 with unicode + missing
    c = np.array(["beta", "alpha", None, "ü-band", "alpha", ""], dtype=object)
    add(*_col("cat8_unicode", codec.encode_cat(c), 0.0, "sorted levels; 255 = missing"))
    # ---- cat16 (300 levels)
    lv = [f"L{i:03d}" for i in range(300)]
    c16 = np.array([lv[i] for i in (0, 299, 150)] + [None], dtype=object)
    add(*_col("cat16_big", codec.encode_cat(c16, levels=lv), 0.0, "cat16, 65535 = missing, shuffled"))
    # ---- bits (11 rows -> 2 bytes, LSB first)
    b = np.array([1, 0, 1, 1, 0, 0, 0, 1, 1, 0, 1], dtype=bool)
    add(*_col("bits_lsb", codec.encode_bits(b), 0.0, "1 bit/row, LSB-first; n = 11"))
    # ---- f32
    f32 = np.array([1.5, -2.25, np.nan, 3.4028234663852886e38, 1e-40, 0.0])
    add(*_col("f32_basic", codec.encode_f32(f32), 0.0, "f32 4-plane shuffle; NaN missing; subnormal"))
    cases[-1]["expected"] = _nulls(np.array(f32, dtype=np.float32))
    # ---- u32
    add(*_col("u32_basic", codec.encode_u32(np.array([0, 1, 4294967294, np.nan, 70000])), 0.0,
              "u32; 0xFFFFFFFF missing; 4-plane shuffle"))

    # ---- str (unicode, empty, missing)
    strs = ["J1745-2900", "", "Sgr B2(N) — été", None, "漢字"]
    raw = codec.encode_str(strs)
    add("str_unicode.bin", codec.gz(raw),
        {"name": "str_unicode", "kind": "column", "file": "str_unicode.bin",
         "meta": {"enc": "str"}, "n": len(strs),
         "expected": ["" if s is None else s for s in strs],
         "note": "u32 n, u32 offsets[n+1], UTF-8 blob; missing -> ''"})
    add("str_empty.bin", codec.gz(codec.encode_str([])),
        {"name": "str_empty", "kind": "column", "file": "str_empty.bin", "meta": {"enc": "str"},
         "n": 0, "expected": []})

    # ---- sparse
    sp = codec.encode_sparse([7, 2, 2, 100000], [1, 2, 4, 5])
    add("sparse_limits.bin", codec.gz(sp),
        {"name": "sparse_limits", "kind": "sparse", "file": "sparse_limits.bin",
         "expected": {"idx": [2, 7, 100000], "kind": [2, 1, 5]},
         "note": "u32 m, u32 idx[m] ascending, u8 kind[m]; duplicate row keeps first kind"})

    # ---- positions u32lin
    ra = np.array([266.41, 266.42, np.nan, 266.405])
    dec = np.array([-28.94, -28.93, -28.935, -28.9301])
    okp = np.isfinite(ra) & np.isfinite(dec)
    bbox = {"ra": [float(ra[okp].min()), float(ra[okp].max())],
            "dec": [float(dec[okp].min()), float(dec[okp].max())]}
    pf = {}
    for k, v in (("ra", ra), ("dec", dec)):
        enc = codec.encode_u32lin(v, *bbox[k])
        fn = f"pos_u32lin_{k}.bin"
        files[fn] = enc.file_bytes()
        pf[k] = fn
    dra, dde = codec.decode_pos({"enc": "u32lin"}, {k: files[v] for k, v in pf.items()}, bbox)
    cases.append({"name": "pos_u32lin", "kind": "pos", "files": pf, "meta": {"enc": "u32lin"},
                  "bbox": bbox, "n": 4, "input": {"ra": _nulls(ra), "dec": _nulls(dec)},
                  "expected": {"ra": _nulls(dra), "dec": _nulls(dde)}, "tol": 1e-9,
                  "tol_deg": 1e-7, "note": "row with NaN ra is missing in both files' decode"})
    # ---- positions i16off (wrap at ra=0, pole, missing)
    ra2 = np.array([359.9999, 0.0001, 10.0, 180.0, np.nan, 45.0])
    dec2 = np.array([0.0, 0.0001, -30.0, 89.99, 0.0, -90.0])
    p = codec.encode_i16off(ra2, dec2, step_mas=8.0)
    pf = {}
    for k in ("tile", "dra", "ddec"):
        fn = f"pos_i16off_{k}.bin"
        files[fn] = codec.file_bytes("u16" if k == "tile" else "i16", p[k], shuffle=True)
        pf[k] = fn
    pm = {"enc": "i16off", "step_mas": p["step_mas"], "tile_refs": p["tile_refs"]}
    rd, dd = codec.decode_pos(pm, {k: files[v] for k, v in pf.items()})
    cases.append({"name": "pos_i16off", "kind": "pos", "files": pf, "meta": pm, "n": len(ra2),
                  "input": {"ra": _nulls(ra2), "dec": _nulls(dec2)},
                  "codes": {"tile": [int(v) for v in p["tile"]], "dra": [int(v) for v in p["dra"]],
                            "ddec": [int(v) for v in p["ddec"]]},
                  "expected": {"ra": _nulls(rd), "dec": _nulls(dd)}, "tol": 1e-9,
                  "tol_deg": 8.0 / 3.6e6,
                  "note": "ra = ra0 + dra*step/3.6e6/cos(dec0), dec = dec0 + ddec*step/3.6e6; "
                          "tile/dra/ddec 2-plane shuffled; -32768 = missing. Compare RA modulo 360."})

    # ---- density (u16log counts)
    rng = np.random.default_rng(7)
    xs = rng.normal(0.0, 1.0, 500)
    ys = rng.normal(0.0, 1.0, 500)
    entry, data = density.build_density("cmd", xs, ys, x_expr="a - b", y_expr="a", xr=[-3, 3],
                                        yr=[-3, 3], nx=8, ny=6)
    raw = codec.unshuffle(codec.gunzip(data), 2)
    dcodes = np.frombuffer(raw, dtype="<u2")
    add("density_cmd.bin", data,
        {"name": "density_cmd", "kind": "density", "file": "density_cmd.bin", "meta": entry,
         "n": int(dcodes.size), "codes": [int(v) for v in dcodes],
         "expected": [float(v) for v in density.decode_counts(entry, dcodes)], "tol_rel": 1e-9,
         "note": "u16 nx*ny, 2-plane shuffled; cell = iy*nx + ix, iy = 0 at yr[0]; "
                 "count = 10**(lo + (code-1)*step) (section 5.3 u16log, lo/hi in log10), 0 = empty"})
    # ---- density stat (f32)
    vals = xs * 2.0
    entry2, data2 = density.build_density("med", xs, ys, x_expr="a - b", y_expr="a", xr=[-3, 3],
                                          yr=[-3, 3], nx=4, ny=4, stat="median:av", values=vals)
    g = np.frombuffer(codec.unshuffle(codec.gunzip(data2), 4), dtype="<f4")
    add("density_med.bin", data2,
        {"name": "density_med", "kind": "density", "file": "density_med.bin", "meta": entry2,
         "n": int(g.size), "expected": _nulls(g), "tol": 0.0,
         "note": "binned statistic: f32 4-plane shuffled, NaN = empty cell"})

    # ---- pks1 records
    w = np.linspace(1.0, 2.0, 6)
    spec = {"wave": w, "channels": {"flux": np.array([1e-3, 2e-3, np.nan, 4e-3, 5e-3, 6e-3]),
                                     "err": np.array([1e-4] * 6)},
            "units": {"flux": "Jy", "err": "Jy"}, "segments": [(0, 3, "G140M"), (3, 6, "G235M")],
            "mask_runs": [(2, 3, 1)], "mask_bits": {1: "saturated"},
            "lines": [{"name": "Paα", "w": 1.8756}], "meta": {"target": "src-é"}}
    raw = records.encode_pks1("src1", spec, enc={"flux": "f32", "err": "u16log"})
    d = records.decode_pks1(raw)
    add("pks1_uniform.bin", codec.gz(raw),
        {"name": "pks1_uniform", "kind": "pks1", "file": "pks1_uniform.bin",
         "expected": {"header": d["header"], "wave": _nulls(d["wave"]),
                      "channels": {k: _nulls(v) for k, v in d["channels"].items()}},
         "tol_rel": 1e-6, "note": "uniform wave grid; f32 flux; u16log err"})
    w2 = np.array([0.6, 0.61, 0.65, 0.8, 1.2, 5.3])
    spec2 = {"wave": w2, "channels": {"flux": np.array([1.0, 2.0, 3.0, np.nan, 5.0, 1e5])}}
    raw2 = records.encode_pks1("sé2", spec2, enc={"flux": "u16q"})
    d2 = records.decode_pks1(raw2)
    add("pks1_array.bin", codec.gz(raw2),
        {"name": "pks1_array", "kind": "pks1", "file": "pks1_array.bin",
         "expected": {"header": d2["header"], "wave": _nulls(d2["wave"]),
                      "channels": {k: _nulls(v) for k, v in d2["channels"].items()}},
         "tol_rel": 1e-6, "note": "non-uniform wave stored as f32 array; u16q flux"})
    # ---- pki1
    img = {"data": np.arange(12, dtype=float).reshape(3, 4), "wave": np.array([1.0, 1.1, 1.2, 1.3]),
           "y": np.array([2.0, 1.0, 0.0]), "stretch": "asinh"}
    img["data"][1, 2] = np.nan
    raw3 = records.encode_pki1("im1", img, enc="u16")
    d3 = records.decode_pki1(raw3)
    add("pki1_u16.bin", codec.gz(raw3),
        {"name": "pki1_u16", "kind": "pki1", "file": "pki1_u16.bin",
         "expected": {"header": d3["header"], "codes": d3["codes"].ravel().tolist(),
                      "wave": _nulls(d3["wave"]), "y": _nulls(d3["y"]),
                      "values": _nulls(d3["values"].ravel())},
         "tol_rel": 1e-9,
         "note": "rows flipped so y ascends; u16 codes 1..65535, 0 missing; asinh stretch"})
    raw4 = records.encode_pki1("im2", {"data": img["data"], "wave": img["wave"], "y": img["y"],
                                       "stretch": "linear"}, enc="u8", lo=0.0, hi=11.0)
    d4 = records.decode_pki1(raw4)
    add("pki1_u8.bin", codec.gz(raw4),
        {"name": "pki1_u8", "kind": "pki1", "file": "pki1_u8.bin",
         "expected": {"header": d4["header"], "codes": d4["codes"].ravel().tolist(),
                      "wave": _nulls(d4["wave"]), "y": _nulls(d4["y"]),
                      "values": _nulls(d4["values"].ravel())},
         "tol_rel": 1e-9, "note": "u8 codes 0..255 (NaN -> 0), linear stretch"})

    # ---- shard of json records
    packer = records.ShardPacker()
    recs = {"a": {"x": 1}, "bé": {"y": [1, 2, None]}, "c": {"z": "☃"}}
    entries = {}
    for k, v in recs.items():
        entries[k] = packer.add(codec.gz(records.encode_json_record(v)))
    (shard,) = packer.finish()
    add("shard_json.bin", shard,
        {"name": "shard_json", "kind": "shard", "file": "shard_json.bin", "entries": entries,
         "expected": recs, "note": "concatenated gzip members; entry = [shard, off, len]"})

    # ---- row chunk
    rows = [{"id": "s1", "ra": 266.4, "mag": 12.345, "note": "hé"},
            {"id": "s2", "ra": None, "mag": None, "note": ""}]
    add("rows_chunk.bin", codec.gz(dumps_json(rows)),
        {"name": "rows_chunk", "kind": "rows", "file": "rows_chunk.bin", "expected": rows})

    # ---- chunked catalog with explicit chunk_starts (IMPLEMENTATION_NOTES 98)
    _catalog_chunks(add)

    index = {"schema": SCHEMA, "cases": cases}
    return files, index


def _catalog_chunks(add) -> None:
    """``kind = "catalog_chunks"``: a 10-row catalog split at ``chunk_starts`` [0, 3, 7].

    Chunk boundaries are not byte aligned (bits); every column and the ids are cut there exactly
    as ``bundle/catalog.py`` cuts them (``Encoded.file_bytes(a, b)`` per chunk). ``manifest`` is
    the ``pk-catalog/1`` subset a reader needs; ``files`` maps a label to each chunk file;
    ``expected`` holds the decoded ids and column values in stored order (null = missing).
    """
    n = 10
    starts = [0, 3, 7]
    geom = {"n": n, "chunk_rows": 1048576, "chunk_starts": starts}
    ranges = codec.chunk_ranges(geom)
    ids = ["a0", "Ü1", "日本2", "", "a4", "a5", "dup", "a7", "dup", "z9"]
    encs = {
        "mag": codec.encode_u16q(np.array([10.0, 11.5, np.nan, 12.25, 30.0, 10.0, 15.5, np.nan, 20.0, 29.999]),
                                 step=0.001),
        "cls": codec.encode_cat(np.array(["star", "yso", "gälaxy", "", "star", "yso", "star", "yso", "gälaxy",
                                          "star"], dtype=object)),
        "ok": codec.encode_bits(np.array([1, 0, 1, 1, 0, 0, 1, 1, 1, 0], dtype=bool)),
        "flux": codec.encode_f32(np.array([1e-3, np.nan, 2.5, -1.0, 7.0, 0.0, 1e30, 3.0, 4.0, 5.0])),
    }
    roles = {"mag": "mag", "cls": "category", "ok": "flag", "flux": "value", "name": "text"}
    names = ["n0", "nø1", None, "n3", "", "n5", "☃6", "n7", "n8", "n9"]
    files: dict[str, str] = {}
    cols = []
    expected_cols: dict[str, list] = {}
    for key in ("mag", "cls", "ok", "flux", "name"):
        meta = {"key": key, "role": roles[key]}
        chunk_files = []
        for ci, (a, b) in enumerate(ranges):
            if key == "name":
                data = codec.gz(codec.encode_str(names[a:b]))
            else:
                data = encs[key].file_bytes(a, b)
            fname = f"chunks_{key}.c{ci:03d}.bin"
            add(fname, data, None)
            files[f"{key}.c{ci:03d}"] = fname
            chunk_files.append(fname)
        if key == "name":
            meta["enc"] = "str"
            expected_cols[key] = ["" if s is None else s for s in names]
        else:
            e = encs[key]
            meta.update({k: v for k, v in e.meta.items() if k in ("enc", "lo", "hi", "step", "shuffle", "levels")})
            if meta["enc"] == "cat8":
                expected_cols[key] = [None if int(c) == codec.CAT8_MISSING else meta["levels"][int(c)]
                                      for c in e.codes]
            elif meta["enc"] == "bits":
                expected_cols[key] = [int(c) for c in e.codes]
            else:
                expected_cols[key] = _nulls(codec.dequant(meta, e.codes))
        meta["chunk_files"] = chunk_files
        cols.append(meta)
    id_files = []
    for ci, (a, b) in enumerate(ranges):
        fname = f"chunks_ids.c{ci:03d}.bin"
        add(fname, codec.gz(codec.encode_str(ids[a:b])), None)
        files[f"ids.c{ci:03d}"] = fname
        id_files.append(fname)
    man = {"schema": "pk-catalog/1", "id": "chunks", **geom, "order": {"kind": "input"},
           "ids": {"file": None, "enc": "str", "chunks": id_files}, "cols": cols}
    add(None, None, {"name": "catalog_chunks", "kind": "catalog_chunks", "manifest": man, "files": files,
                     "expected": {"ids": ids, "cols": expected_cols},
                     "note": "chunk_starts [0, 3, 7]: chunk k holds rows [starts[k], starts[k+1]); "
                             "bits chunks restart at bit 0; NaN -> null"})


def main(argv: list[str]) -> int:
    files, index = build()
    check = "--check" in argv
    bad = []
    for name, data in files.items():
        p = HERE / name
        if check:
            if not p.exists() or p.read_bytes() != data:
                bad.append(name)
        else:
            p.write_bytes(data)
    idx_bytes = json.dumps(index, ensure_ascii=False, allow_nan=False, indent=1).encode("utf-8") + b"\n"
    if check:
        if (HERE / "index.json").read_bytes() != idx_bytes:
            bad.append("index.json")
        if bad:
            print("golden mismatch:", ", ".join(bad))
            return 1
        print(f"{len(files)} golden files match")
        return 0
    (HERE / "index.json").write_bytes(idx_bytes)
    print(f"wrote {len(files)} golden files + index.json to {HERE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
