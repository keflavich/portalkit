"""Record sets: pks1 / pki1 / json / sedcurve writers and the shard packer.

ARCHITECTURE.md sections 5.6-5.8.

Record byte layout (after gunzip) for ``pks1`` and ``pki1``::

    offset 0             4 bytes  ASCII "PKS1" | "PKI1"
    offset 4             u32      header_len (multiple of 8, hence of 4; JSON padded with spaces)
    offset 8             header JSON (UTF-8)
    offset 8+header_len  binary section (8-aligned in the record); every array starts at a
                         4-byte aligned offset relative to the binary section, f64 arrays at
                         an 8-byte aligned one

Arrays are little-endian and not byte-shuffled (the whole record is one gzip member).
``json`` records are gzip members holding UTF-8 JSON. ``sedcurve`` records are pks1 records
whose channels default to ``u16log``.

The packer appends gzip members in catalog stored order and starts a new shard when the
current one would exceed ``SHARD_TARGET``. A member larger than ``SHARD_MAX`` is written as its
own file ``r/<h8>.bin`` and the index entry is that path (layouts mix within one index).
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import numpy as np

from ..constants import SHARD_MAX, SHARD_TARGET
from .codec import encode_u16log, encode_u16q, gunzip, gz, to_float_array
from .writer import RECORD_FILE_SOFT_MAX, BundleError, BundleWriter, Ledger, dumps_json, to_jsonable

__all__ = ["MAGIC_PKS1", "MAGIC_PKI1", "encode_pks1", "decode_pks1", "encode_pki1", "decode_pki1",
           "encode_json_record", "decode_record", "wave_descriptor", "grid_descriptor",
           "ShardPacker", "RecordsResult", "write", "pack_record", "split_record"]

MAGIC_PKS1 = b"PKS1"
MAGIC_PKI1 = b"PKI1"


# ---------------------------------------------------------------------------
# record framing
# ---------------------------------------------------------------------------

def _align4(n: int) -> int:
    return (n + 3) & ~3


def _align8(n: int) -> int:
    return (n + 7) & ~7


def pack_record(magic: bytes, header: dict, binary: bytes) -> bytes:
    """``magic + u32 header_len + header JSON (space padded) + binary``.

    ``header_len`` is padded to a multiple of 8 (hence also of 4, as section 5.7 requires), so
    the binary section starts at an 8-byte aligned offset of the inflated record and f64
    arrays (8-aligned inside the binary section) can be viewed as ``Float64Array`` directly.
    """
    hj = dumps_json(header)
    pad = _align8(len(hj)) - len(hj)
    hj += b" " * pad
    return magic + struct.pack("<I", len(hj)) + hj + binary


def split_record(raw: bytes) -> tuple[bytes, dict, bytes]:
    """Inverse of :func:`pack_record` -> (magic, header, binary section)."""
    if len(raw) < 8:
        raise ValueError("record too short")
    magic = raw[:4]
    hl = struct.unpack_from("<I", raw, 4)[0]
    if hl % 4:
        raise ValueError(f"header_len {hl} not a multiple of 4")
    header = json.loads(raw[8:8 + hl].decode("utf-8"))
    return magic, header, raw[8 + hl:]


class _Binary:
    """Accumulates 4-byte aligned arrays (8-byte aligned for f64) of a record's binary section."""

    def __init__(self) -> None:
        self.parts: list[bytes] = []
        self.size = 0

    def add(self, b: bytes, align: int = 4) -> int:
        pre = (-self.size) % align
        if pre:
            self.parts.append(b"\0" * pre)
            self.size += pre
        off = self.size
        self.parts.append(b)
        self.size += len(b)
        pad = _align4(self.size) - self.size
        if pad:
            self.parts.append(b"\0" * pad)
            self.size += pad
        return off

    def bytes(self) -> bytes:
        return b"".join(self.parts)


# ---------------------------------------------------------------------------
# wavelength descriptors
# ---------------------------------------------------------------------------

def _uniform(w: np.ndarray, tol: float = 1e-4) -> tuple[float, float] | None:
    if w.size < 2 or not np.all(np.isfinite(w)):
        return None
    dw = (w[-1] - w[0]) / (w.size - 1)
    if not dw > 0:
        return None
    model = w[0] + dw * np.arange(w.size)
    if np.max(np.abs(w - model)) <= tol * dw:
        return float(w[0]), float(dw)
    return None


def grid_descriptor(w: np.ndarray) -> dict:
    """Records index ``grids`` entry: ``{kind:"uniform",w0,dw,n}`` or ``{kind:"array",values}``."""
    w = np.asarray(w, dtype=np.float64)
    u = _uniform(w)
    if u is not None:
        return {"kind": "uniform", "w0": u[0], "dw": u[1], "n": int(w.size)}
    return {"kind": "array", "values": [float(x) for x in w]}


def _match_grid(w: np.ndarray, grids: Mapping[str, np.ndarray] | None) -> str | None:
    for name, g in (grids or {}).items():
        g = np.asarray(g, dtype=np.float64)
        if g.shape == w.shape and np.allclose(g, w, rtol=1e-9, atol=0.0, equal_nan=False):
            return name
    return None


def wave_descriptor(w: np.ndarray, binary: _Binary, grids: Mapping[str, np.ndarray] | None = None
                    ) -> dict:
    """Header ``wave``: shared grid, uniform, or an f32/f64 array in the binary section.

    f32 is used when it keeps the ordering of adjacent samples and errs by at most 1 % of the
    smallest spacing; otherwise f64.
    """
    w = np.asarray(w, dtype=np.float64)
    g = _match_grid(w, grids)
    if g is not None:
        return {"kind": "shared", "grid": g}
    u = _uniform(w)
    if u is not None:
        return {"kind": "uniform", "w0": u[0], "dw": u[1]}
    w32 = w.astype(np.float32)
    use32 = True
    if w.size >= 2:
        d = np.diff(w)
        d32 = np.diff(w32.astype(np.float64))
        fin = np.isfinite(d)
        if np.any(np.sign(d[fin]) != np.sign(d32[fin])):
            use32 = False
        else:
            mind = np.min(np.abs(d[fin & (d != 0)])) if np.any(fin & (d != 0)) else 0.0
            err = np.nanmax(np.abs(w32.astype(np.float64) - w)) if w.size else 0.0
            if mind > 0 and err > 0.01 * mind:
                use32 = False
    if use32:
        off = binary.add(w32.astype("<f4").tobytes())
        return {"kind": "array", "off": off, "dtype": "f32"}
    off = binary.add(w.astype("<f8").tobytes(), align=8)
    return {"kind": "array", "off": off, "dtype": "f64"}


def _wave_values(desc: dict, n: int, binary: bytes, grids: Mapping | None) -> np.ndarray:
    k = desc["kind"]
    if k == "uniform":
        return desc["w0"] + desc["dw"] * np.arange(n)
    if k == "array":
        dt = "<f4" if desc.get("dtype", "f32") == "f32" else "<f8"
        return np.frombuffer(binary, dtype=dt, count=n, offset=desc["off"]).astype(np.float64)
    if k == "shared":
        if grids is None or desc["grid"] not in grids:
            raise ValueError(f"shared grid {desc['grid']!r} not supplied")
        g = grids[desc["grid"]]
        if isinstance(g, dict):
            if g["kind"] == "uniform":
                return g["w0"] + g["dw"] * np.arange(g["n"])
            return np.asarray(g["values"], dtype=np.float64)
        return np.asarray(g, dtype=np.float64)
    raise ValueError(f"unknown wave kind {k!r}")


# ---------------------------------------------------------------------------
# pks1
# ---------------------------------------------------------------------------

def _g(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def encode_pks1(rec_id: str, spec: Any, enc: Mapping[str, str] | None = None,
                grids: Mapping[str, np.ndarray] | None = None, unit_w: str = "um",
                default_enc: str = "f32") -> bytes:
    """Encode a ``Spectrum`` (section 9.1; dict with the same keys also accepted) as pks1.

    Returns the raw (uncompressed) record. ``enc`` maps channel -> ``f32`` | ``u16q`` |
    ``u16log``; unlisted channels use ``default_enc``.
    """
    wave = np.asarray(_g(spec, "wave"), dtype=np.float64).ravel()
    n = int(wave.size)
    chans = _g(spec, "channels") or {}
    if "flux" not in chans:
        raise BundleError(f"record {rec_id!r}: channel 'flux' is required")
    units = dict(_g(spec, "units") or {"flux": "Jy"})
    enc = dict(enc or {})
    binary = _Binary()
    wdesc = wave_descriptor(wave, binary, grids)
    ch_meta = []
    for name, arr in chans.items():
        a = to_float_array(np.asarray(arr).ravel() if not isinstance(arr, np.ma.MaskedArray) else arr.ravel())
        if a.size != n:
            raise BundleError(f"record {rec_id!r}: channel {name!r} has {a.size} values, wave has {n}")
        e = enc.get(name, default_enc)
        unit = units.get(name)
        if unit is None:
            unit = units.get("flux", "") if name.startswith("err") or name.startswith("e_") else ""
        m: dict = {"name": name, "unit": unit, "enc": e}
        if e == "f32":
            m["off"] = binary.add(a.astype("<f4").tobytes())
        elif e in ("u16q", "u16log"):
            q = encode_u16q(a, label=name) if e == "u16q" else encode_u16log(a, label=name)
            m["lo"], m["hi"], m["step"] = q.meta["lo"], q.meta["hi"], q.meta["step"]
            m["off"] = binary.add(q.codes.astype("<u2").tobytes())
        else:
            raise BundleError(f"record {rec_id!r}: unsupported channel encoding {e!r}")
        ch_meta.append(m)
    header: dict = {"id": rec_id, "n": n, "wave": wdesc, "unit_w": unit_w, "channels": ch_meta}
    segs = _g(spec, "segments") or []
    if segs:
        header["segments"] = [[int(a), int(b), str(c)] for a, b, c in segs]
    runs = _g(spec, "mask_runs") or []
    if runs:
        header["mask_runs"] = [[int(a), int(b), int(c)] for a, b, c in runs]
    bits = _g(spec, "mask_bits") or {}
    if bits:
        header["mask_bits"] = {str(int(k)): str(v) for k, v in bits.items()}
    lines = _g(spec, "lines") or []
    if lines:
        header["lines"] = to_jsonable(list(lines))
    meta = _g(spec, "meta") or {}
    if meta:
        header["meta"] = to_jsonable(dict(meta))
    return pack_record(MAGIC_PKS1, header, binary.bytes())


def decode_pks1(raw: bytes, grids: Mapping | None = None) -> dict:
    """Python twin of ``records.js decodePKS1`` -> {header, id, wave, channels{name: float64}}.

    ``raw`` may be gzip-compressed. Missing values decode to NaN.
    """
    if raw[:2] == b"\x1f\x8b":
        raw = gunzip(raw)
    magic, h, binary = split_record(raw)
    if magic != MAGIC_PKS1:
        raise ValueError(f"not a pks1 record (magic {magic!r})")
    n = h["n"]
    wave = _wave_values(h["wave"], n, binary, grids)
    chans = {}
    for c in h["channels"]:
        if c["enc"] == "f32":
            v = np.frombuffer(binary, dtype="<f4", count=n, offset=c["off"]).astype(np.float64)
        else:
            codes = np.frombuffer(binary, dtype="<u2", count=n, offset=c["off"])
            v = c["lo"] + (codes.astype(np.float64) - 1.0) * c["step"]
            if c["enc"] == "u16log":
                v = 10.0 ** v
            v[codes == 0] = np.nan
        chans[c["name"]] = v
    return {"header": h, "id": h["id"], "wave": wave, "channels": chans}


# ---------------------------------------------------------------------------
# pki1
# ---------------------------------------------------------------------------

def encode_pki1(rec_id: str, img: Any, enc: str = "u8", grids: Mapping[str, np.ndarray] | None = None,
                lo: float | None = None, hi: float | None = None, a: float = 0.1,
                pct: tuple[float, float] = (1.0, 99.5)) -> bytes:
    """Encode an ``Image2D`` (``data[ny, nx]``, ``wave[nx]``, ``y[ny]``) as pki1.

    Stretch: ``s = clip((x - lo) / (hi - lo), 0, 1)``; ``asinh``:
    ``s = asinh(s / a) / asinh(1 / a)``. ``lo``/``hi`` default to the 1 / 99.5 percentiles.
    ``u8``: ``code = rint(255 s)`` (NaN -> 0); ``u16``: ``code = 1 + rint(65534 s)``, 0 = missing.
    Rows are flipped when ``y`` is descending so that ``y.dy > 0``.
    """
    data = np.asarray(_g(img, "data"), dtype=np.float64)
    if data.ndim != 2:
        raise BundleError(f"record {rec_id!r}: image data must be 2D")
    ny, nx = data.shape
    if nx < 1 or ny < 1:
        raise BundleError(f"record {rec_id!r}: empty image ({ny} x {nx})")
    wave =np.asarray(_g(img, "wave"), dtype=np.float64).ravel()
    y = np.asarray(_g(img, "y"), dtype=np.float64).ravel()
    if wave.size != nx or y.size != ny:
        raise BundleError(f"record {rec_id!r}: wave/y lengths {wave.size}/{y.size} != nx/ny {nx}/{ny}")
    if ny > 1 and y[-1] < y[0]:
        data = data[::-1]
        y = y[::-1]
    kind = _g(img, "stretch", "asinh") or "asinh"
    if kind not in ("asinh", "linear"):
        raise BundleError(f"record {rec_id!r}: unknown stretch {kind!r}")
    fin = data[np.isfinite(data)]
    if lo is None or hi is None:
        if fin.size:
            plo, phi = (float(v) for v in np.percentile(fin, pct))
        else:
            plo, phi = 0.0, 1.0
        lo = plo if lo is None else lo
        hi = phi if hi is None else hi
    if not hi > lo:
        hi = lo + (abs(lo) * 1e-6 if lo != 0 else 1e-30) if hi <= lo else hi
        if not hi > lo:
            hi = lo + 1.0
    s = np.clip((data - lo) / (hi - lo), 0.0, 1.0)
    if kind == "asinh":
        s = np.arcsinh(s / a) / np.arcsinh(1.0 / a)
    nan = ~np.isfinite(data)
    binary = _Binary()
    if enc == "u8":
        codes = np.rint(np.where(nan, 0.0, s) * 255.0).astype(np.uint8)
        off = binary.add(codes.tobytes())
    elif enc == "u16":
        codes = (1.0 + np.rint(np.where(nan, 0.0, s) * 65534.0)).astype(np.uint16)
        codes[nan] = 0
        off = binary.add(codes.astype("<u2").tobytes())
    else:
        raise BundleError(f"record {rec_id!r}: pki1 enc must be u8 or u16")
    wdesc = wave_descriptor(wave, binary, grids)
    st = {"kind": kind, "lo": float(lo), "hi": float(hi)}
    if kind == "asinh":
        st["a"] = float(a)
    if ny > 1:
        y0, dy = float(y[0]), float((y[-1] - y[0]) / (ny - 1))
        if not dy > 0:
            dy = 1.0
    else:
        y0, dy = float(y[0]) if ny else 0.0, 1.0
    header = {"id": rec_id, "nx": int(nx), "ny": int(ny), "enc": enc, "off": off, "stretch": st,
              "wave": wdesc, "y": {"kind": "uniform", "y0": y0, "dy": dy}}
    unit_y = _g(img, "unit_y")
    if unit_y:
        header["y"]["unit"] = str(unit_y)
    segs = _g(img, "segments") or []
    if segs:
        header["segments"] = [[int(a_), int(b_), str(c_)] for a_, b_, c_ in segs]
    return pack_record(MAGIC_PKI1, header, binary.bytes())


def decode_pki1(raw: bytes, grids: Mapping | None = None) -> dict:
    """-> {header, id, nx, ny, codes (ny, nx), wave, y, stretched (0..1, NaN missing), values}."""
    if raw[:2] == b"\x1f\x8b":
        raw = gunzip(raw)
    magic, h, binary = split_record(raw)
    if magic != MAGIC_PKI1:
        raise ValueError(f"not a pki1 record (magic {magic!r})")
    nx, ny = h["nx"], h["ny"]
    if h["enc"] == "u8":
        codes = np.frombuffer(binary, dtype=np.uint8, count=nx * ny, offset=h["off"]).reshape(ny, nx)
        s = codes / 255.0
    else:
        codes = np.frombuffer(binary, dtype="<u2", count=nx * ny, offset=h["off"]).reshape(ny, nx)
        s = (codes.astype(np.float64) - 1.0) / 65534.0
        s[codes == 0] = np.nan
    st = h["stretch"]
    if st["kind"] == "asinh":
        a = st.get("a", 0.1)
        lin = a * np.sinh(s * np.arcsinh(1.0 / a))
    else:
        lin = s
    values = st["lo"] + (st["hi"] - st["lo"]) * lin
    wave = _wave_values(h["wave"], nx, binary, grids)
    y = h["y"]["y0"] + h["y"]["dy"] * np.arange(ny)
    return {"header": h, "id": h["id"], "nx": nx, "ny": ny, "codes": codes, "wave": wave, "y": y,
            "stretched": s, "values": values}


# ---------------------------------------------------------------------------
# json records
# ---------------------------------------------------------------------------

def encode_json_record(obj: Any) -> bytes:
    """Raw (uncompressed) UTF-8 JSON record."""
    return dumps_json(obj)


def decode_record(member: bytes, kind: str, grids: Mapping | None = None) -> Any:
    raw = gunzip(member) if member[:2] == b"\x1f\x8b" else member
    if kind in ("pks1", "sedcurve"):
        return decode_pks1(raw, grids)
    if kind == "pki1":
        return decode_pki1(raw, grids)
    if kind == "json":
        return json.loads(raw.decode("utf-8"))
    raise ValueError(f"unknown record kind {kind!r}")


# ---------------------------------------------------------------------------
# shard packer
# ---------------------------------------------------------------------------

class ShardPacker:
    """Appends gzip members to shards of at most ``target`` bytes (a single member up to
    ``max_size`` may start its own shard); larger members are returned as ``"file"``."""

    def __init__(self, target: int = SHARD_TARGET, max_size: int = SHARD_MAX):
        self.target = target
        self.max_size = max_size
        self.shards: list[bytes] = []
        self._cur = bytearray()

    def add(self, member: bytes) -> list[int] | None:
        """Return ``[shard, offset, length]``, or None when the member must go to layout file."""
        ln = len(member)
        if ln > self.max_size:
            return None
        if self._cur and len(self._cur) + ln > self.target:
            self._flush()
        off = len(self._cur)
        self._cur += member
        return [len(self.shards), off, ln]

    def _flush(self) -> None:
        if self._cur:
            self.shards.append(bytes(self._cur))
            self._cur = bytearray()

    def finish(self) -> list[bytes]:
        self._flush()
        return self.shards


# ---------------------------------------------------------------------------
# record set writer
# ---------------------------------------------------------------------------

@dataclass
class RecordsResult:
    index: dict
    files: list[str]
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _is_err(name: str) -> bool:
    return name == "err" or name.startswith("err") or name.startswith("e_")


def write(spec: Any, dest: str | Path | BundleWriter, *,
          order: Mapping[str, int] | Callable[[str], int | None] | None = None,
          tier: str | None = None, ledger: Ledger | None = None,
          log: Callable[[str], None] | None = None) -> RecordsResult:
    """Write a ``RecordSetSpec`` into ``dest`` (the record-set directory ``data/<set>/``).

    ``order`` gives the catalog stored row of each id (``CatalogResult.id_to_row`` or
    ``CatalogResult.row_of``); members are appended in that order, ids without a row go last
    in ``spec.ids`` order. ``tier`` gates ``json-legacy`` (accepted only for tier A or B, section 5.6).
    """
    if isinstance(dest, BundleWriter):
        w = dest
        if ledger is not None and w.ledger is not ledger:
            w = BundleWriter(w.root, w.base, ledger, w.dataset, w.fsync)
    else:
        w = BundleWriter(Path(dest), "", ledger)
    key = _g(spec, "key")
    if w.dataset is None:
        w.dataset = key
    kind = _g(spec, "kind")
    if kind not in ("pks1", "pki1", "json", "sedcurve"):
        raise BundleError(f"{key}: unknown record kind {kind!r}")
    warnings: list[str] = []

    def warn(msg: str) -> None:
        warnings.append(msg)
        w.ledger.warn(f"{key}: {msg}")
        if log is not None:
            log(f"[{key}] warning: {msg}")

    ids = [str(i) for i in (_g(spec, "ids") or [])]
    if len(set(ids)) != len(ids):
        raise BundleError(f"{key}: duplicate record ids")
    catalog = _g(spec, "catalog")
    legacy = _g(spec, "legacy_json")
    if legacy:
        if tier not in ("A", "B"):
            raise BundleError(f"{key}: json-legacy record sets are only allowed for tiers A/B "
                              f"(got tier {tier!r})")
        idx = {"kind": "json-legacy", "dir": legacy[0], "pattern": legacy[1], "key": "id",
               "n": len(ids)}
        if catalog:
            idx["catalog"] = catalog
        w.put_stable("index.json", dumps_json(idx), "index")
        return RecordsResult(idx, [], [], warnings)

    if order is not None:
        rowf = order if callable(order) else order.get
        pos = {rid: i for i, rid in enumerate(ids)}
        big = len(ids) + 1 << 40

        def sk(rid: str) -> tuple[int, int]:
            r = rowf(rid)
            return (r if r is not None else big, pos[rid])
        ids = sorted(ids, key=sk)

    grids = {k: np.asarray(v, dtype=np.float64) for k, v in (_g(spec, "grids") or {}).items()}
    enc = dict(_g(spec, "enc") or {})
    default_enc = "u16log" if kind == "sedcurve" else "f32"
    layout = _g(spec, "layout", "shard") or "shard"
    png = _g(spec, "png")
    loader = _g(spec, "loader")
    packer = ShardPacker()
    entries: dict[str, Any] = {}
    files: list[str] = []
    skipped: list[str] = []
    variants: list[str] = []
    unit_f = None

    for rid in ids:
        if kind == "pki1" and png is not None:
            r = png(rid)
            if r is not None:
                url, extent = r
                entries[rid] = {"png": str(url), "extent": to_jsonable(extent)}
                continue
        rec = loader(rid) if loader is not None else None
        if rec is None:
            skipped.append(rid)
            continue
        if kind in ("pks1", "sedcurve"):
            raw = encode_pks1(rid, rec, enc, grids, default_enc=default_enc)
            for name in (_g(rec, "channels") or {}):
                if not _is_err(name) and name not in variants:
                    variants.append(name)
            if unit_f is None:
                unit_f = (_g(rec, "units") or {"flux": "Jy"}).get("flux")
        elif kind == "pki1":
            raw = encode_pki1(rid, rec, enc.get("data", "u8"), grids)
        else:
            raw = encode_json_record(rec)
        member = gz(raw)
        if layout == "file":
            ent = None
        else:
            ent = packer.add(member)
        if ent is None:
            if layout != "file":
                warn(f"record {rid!r} is {len(member)} B (> SHARD_MAX); written as its own file")
            if len(member) > RECORD_FILE_SOFT_MAX:
                warn(f"record {rid!r} is {len(member)} B (> 2 MiB)")
            p = w.put("r", "", member, "bin", "record")
            files.append(p)
            entries[rid] = p
        else:
            entries[rid] = ent

    shard_names = []
    for si, sb in enumerate(packer.finish()):
        p = w.put("", f"s{si:03d}", sb, "bin", "shard")
        files.append(p)
        shard_names.append(p)

    index: dict = {"schema": "pk-records/1", "kind": kind, "layout": layout}
    if catalog:
        index["catalog"] = catalog
    index["key"] = "id"
    index["n"] = len(entries)
    index["shards"] = shard_names
    index["entries"] = entries
    if grids:
        index["grids"] = {k: grid_descriptor(v) for k, v in grids.items()}
    if kind in ("pks1", "sedcurve"):
        if variants:
            index["variants"] = variants
        index["unit_w"] = "um"
        if unit_f is not None:
            index["unit_f"] = unit_f
    w.put_stable("index.json", dumps_json(index), "index")
    if skipped:
        warn(f"{len(skipped)} ids had no record (loader returned None)")
    return RecordsResult(index, files, skipped, warnings)
