"""CatalogSpec -> ``pk-catalog/1`` bundle (ARCHITECTURE.md sections 5.2-5.5 and 5.9).

``write(spec, dest)`` writes, under the dataset directory (``data/<ds>/``):

* ``manifest.json`` (stable name);
* ``cols/<key>.<h8>.bin`` per stored column, or ``cols/<key>.cNNN.<h8>.bin`` chunks. For
  ``N > LOD_MIN_N`` the manifest carries ``chunk_starts`` ``[0, LOD_BASE, LOD_BASE +
  CHUNK_ROWS, ...]`` and every column and the ids are split there, so chunk 0 is the LOD base
  sample (IMPLEMENTATION_NOTES 98); otherwise chunks are ``CHUNK_ROWS`` rows (one file when
  ``N <= CHUNK_ROWS``);
* ``pos/{ra,dec}.<h8>.bin`` (``u32lin``, N <= 131072) or ``pos/{tile,dra,ddec}.<h8>.bin``
  (``i16off``), always single files;
* ``ids.<h8>.bin`` (``str``; ``ids.cNNN.<h8>.bin`` chunks split like the columns) and
  ``aliases.<h8>.json``;
* ``limits/<key>.<h8>.bin`` (``sparse``) for columns with legacy limit codes or clipping;
* ``rows/rNNNN.<h8>.bin`` exact row chunks (gz JSON, 4096 rows) when
  ``N <= EXACT_ROWS_MAX_N`` or ``spec.exact_rows``;
* ``density/<key>.<h8>.bin`` pre-binned grids for ``spec.density`` and, when
  ``N > T0_MAX`` or the dataset is a context dataset, for every ``axes`` preset.

The spec objects are duck-typed against ``portalkit.spec`` (section 9.1): any object with the
documented attributes works.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

from ..constants import (CHUNK_ROWS, EXACT_ROWS_MAX_N, FADE_WIDTH, LOD_BASE, LOD_MIN_N, MAG_STEP, ROW_CHUNK,
                         T0_MAX)
from . import codec
from .codec import (Encoded, LIMIT_LOWER, LIMIT_UPPER, col_stats, encode_bits, encode_cat,
                    encode_f32, encode_i16off, encode_sparse, encode_str, encode_u8log, encode_u8q,
                    encode_u16log, encode_u16q, encode_u32, encode_u32lin, file_bytes, gz,
                    to_float_array)
from .density import DensityError, base_keys, build_density, eval_linear, parse_stat
from .order import SRC_INDEX_KEY, RowOrder, make_order, tile_morton_key
from .writer import BundleError, BundleWriter, Ledger, dumps_json

__all__ = ["write", "CatalogResult", "resolve_enc", "AUTO_ENC", "json_values", "COL_KEY_RE",
           "head_chunk_starts", "first_paint_files"]

COL_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")

#: section 5.3 ``enc: "auto"`` table (role -> encoding); see :func:`resolve_enc` for details
AUTO_ENC = {
    "mag": "u16q", "color": "u16q", "flux": "u16log", "error": "u8log", "value": "u16q",
    "category": "cat8", "flag": "bits", "id": "str", "text": None, "url": None, "image": None,
    "limit": None, "ra": "u16q", "dec": "u16q",
}

_ROW_ONLY_ROLES = ("text", "url", "image")
_NUMERIC_ENCS = ("u16q", "u16log", "u8q", "u8log", "f32", "u32", "u32lin")


@dataclass
class CatalogResult:
    manifest: dict
    order: RowOrder
    ids: list[str]                       # stored order
    id_to_row: dict[str, int]            # id -> stored row
    alias_to_row: dict[str, int]
    files: list[str]                     # paths relative to the dataset directory
    warnings: list[str] = field(default_factory=list)
    writer: BundleWriter | None = None

    def row_of(self, key: str) -> int | None:
        """Stored row of an id or alias (None if unknown)."""
        r = self.id_to_row.get(key)
        return r if r is not None else self.alias_to_row.get(key)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _get(obj: Any, name: str, default: Any = None) -> Any:
    return getattr(obj, name, default)


def _column(table: Any, key: str) -> Any:
    try:
        return table[key]
    except KeyError as exc:
        raise BundleError(f"column {key!r} not in table") from exc


def _ids(col: Any) -> list[str]:
    out = []
    mask = np.ma.getmaskarray(col) if isinstance(col, np.ma.MaskedArray) else None
    data = col.data if isinstance(col, np.ma.MaskedArray) else col
    for i, v in enumerate(np.asarray(data, dtype=object)):
        if (mask is not None and mask[i]) or codec.is_missing_obj(v):
            raise BundleError(f"missing id at input row {i}")
        if isinstance(v, bytes):
            v = v.decode("utf-8")
        elif isinstance(v, (np.integer,)):
            v = int(v)
        elif isinstance(v, (float, np.floating)) and float(v).is_integer():
            v = int(v)
        out.append(str(v))
    return out


def json_values(col: Any) -> list:
    """Full-precision JSON values of a column (masked/NaN -> None; float32 shortest repr)."""
    mask = np.ma.getmaskarray(col) if isinstance(col, np.ma.MaskedArray) else None
    a = np.asarray(col.data if isinstance(col, np.ma.MaskedArray) else col)
    k = a.dtype.kind
    if a.ndim > 1:
        vals = [codec_to_json(v) for v in a.tolist()]
    elif k == "f":
        if a.dtype.itemsize < 8:
            vals = [float(s) for s in a.astype(str)]
        else:
            vals = a.astype(np.float64).tolist()
        vals = [v if math.isfinite(v) else None for v in vals]
    elif k in "iub":
        vals = a.tolist()
    elif k == "S":
        vals = [v.decode("utf-8") for v in a.tolist()]
    elif k == "U":
        vals = a.tolist()
    else:
        vals = [codec_to_json(v) for v in a.tolist()]
    if mask is not None and mask.any():
        vals = [None if m else v for v, m in zip(vals, mask)]
    return vals


def codec_to_json(v: Any) -> Any:
    if v is None or v is np.ma.masked:
        return None
    if isinstance(v, bytes):
        return v.decode("utf-8")
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        f = float(v)
        return f if math.isfinite(f) else None
    if isinstance(v, (list, tuple)):
        return [codec_to_json(x) for x in v]
    if isinstance(v, np.ndarray):
        return [codec_to_json(x) for x in v.tolist()]
    return str(v)


def _limit_kinds(codes: Any) -> np.ndarray:
    """Legacy limit codes 'v'/'u'/'l'/'' (or 1/2) -> sparse kinds (0 = none)."""
    out = []
    mask = np.ma.getmaskarray(codes) if isinstance(codes, np.ma.MaskedArray) else None
    data = codes.data if isinstance(codes, np.ma.MaskedArray) else codes
    for i, v in enumerate(np.asarray(data, dtype=object)):
        if mask is not None and mask[i]:
            out.append(0)
            continue
        if isinstance(v, bytes):
            v = v.decode()
        if isinstance(v, str):
            s = v.strip().lower()
            out.append(LIMIT_UPPER if s in ("u", "<", "upper") else LIMIT_LOWER if s in ("l", ">", "lower") else 0)
        elif isinstance(v, (int, np.integer)) and int(v) in (LIMIT_UPPER, LIMIT_LOWER):
            out.append(int(v))
        else:
            out.append(0)
    return np.array(out, dtype=np.uint8)


def resolve_enc(col: Any, values: np.ndarray | None, *, rows_written: bool,
                exact_gpu: bool = True) -> tuple[str | None, float | None]:
    """Resolve ``enc: "auto"`` (section 5.3) -> (enc or None for rows-only, fixed step).

    * ``mag`` (and ``color``): ``u16q`` with fixed ``step`` (``col.step`` or ``MAG_STEP``);
    * ``flux``: ``u16log``; ``error``: ``u8log``;
    * ``value``: ``u16q``, or ``f32`` when ``exact_gpu`` is false and the dynamic range
      (max |x| / min non-zero |x|) exceeds 1e4;
    * ``category``: ``cat8`` (``cat16`` above 255 levels; ``write`` passes ``enc=None`` to
      ``encode_cat`` for auto columns so the encoder decides from the level count);
    * ``flag``: ``bits``; ``id``: ``str``;
    * ``text``/``url``/``image``: row chunks only (``str`` when no row chunks are written);
    * ``limit``: not stored (feeds the sparse limits table of the referring column).
    """
    enc = _get(col, "enc", "auto") or "auto"
    role = _get(col, "role", "value")
    step = _get(col, "step")
    if enc != "auto":
        if enc in ("u16q",) and step is None and role in ("mag", "color"):
            step = MAG_STEP
        return enc, step
    if role in ("mag", "color"):
        return "u16q", step if step is not None else MAG_STEP
    if role in _ROW_ONLY_ROLES:
        return (None if rows_written else "str"), None
    if role == "limit":
        return None, None
    if role in ("value", "ra", "dec"):
        if not exact_gpu and values is not None:
            a = np.abs(values[np.isfinite(values)])
            a = a[a > 0]
            if a.size and a.max() / a.min() > 1e4:
                return "f32", None
        return "u16q", step
    e = AUTO_ENC.get(role)
    if e is None:
        raise BundleError(f"no automatic encoding for role {role!r}")
    return e, step


def head_chunk_starts(n: int) -> list[int] | None:
    """Manifest ``chunk_starts`` for ``n`` rows (IMPLEMENTATION_NOTES 98), or None.

    For ``n > LOD_MIN_N`` the chunks start at ``[0, LOD_BASE, LOD_BASE + CHUNK_ROWS,
    LOD_BASE + 2*CHUNK_ROWS, ...]`` (every value < n), so chunk 0 holds exactly the LOD base
    sample (rows ``[0, LOD_BASE)``). Smaller catalogs return None: their chunks follow the
    ``ceil(n / chunk_rows)`` rule (one file for ``n <= CHUNK_ROWS``).
    """
    if n <= LOD_MIN_N:
        return None
    starts = [0]
    s = LOD_BASE
    while 0 < s < n:
        starts.append(s)
        s += CHUNK_ROWS
    return starts


def _chunks(n: int, starts: list[int] | None = None) -> list[tuple[int, int]]:
    """``[first, end)`` row ranges of the column/ids chunks (``chunk_starts`` when given)."""
    if starts is not None:
        return [(a, starts[k + 1] if k + 1 < len(starts) else n) for k, a in enumerate(starts)]
    if n <= CHUNK_ROWS:
        return [(0, n)]
    return [(i, min(i + CHUNK_ROWS, n)) for i in range(0, n, CHUNK_ROWS)]


# ---------------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------------

def write(spec: Any, dest: str | Path | BundleWriter, *, build: str | None = None,
          seed: int = 20261010, links: dict | None = None, provenance: dict | None = None,
          ledger: Ledger | None = None, log: Callable[[str], None] | None = None,
          step_mas: float = 8.0) -> CatalogResult:
    """Write the catalog bundle for ``spec`` into ``dest`` (the dataset directory).

    ``dest`` is a :class:`BundleWriter` whose base is the dataset directory (preferred, it
    shares the build ledger and the staging root check) or a directory path, which then also
    acts as the staging root. Returns a :class:`CatalogResult`; ``result.manifest`` is what
    was written to ``manifest.json``.
    """
    if isinstance(dest, BundleWriter):
        w = dest
        if ledger is not None and w.ledger is not ledger:
            w = BundleWriter(w.root, w.base, ledger, w.dataset, w.fsync)
    else:
        w = BundleWriter(Path(dest), "", ledger)
    ds = _get(spec, "key")
    if not isinstance(ds, str) or not ds:
        raise BundleError("CatalogSpec.key must be a non-empty string")
    if w.dataset is None:
        w.dataset = ds
    warnings: list[str] = []

    def warn(msg: str) -> None:
        warnings.append(msg)
        w.ledger.warn(f"{ds}: {msg}")
        if log is not None:
            log(f"[{ds}] warning: {msg}")

    table = _get(spec, "table")
    if table is None:
        raise BundleError(f"{ds}: CatalogSpec.table is None (foreign catalogs use legacy.gc_manifest)")
    n = len(table)
    id_key = _get(spec, "id")
    ids_in = _ids(_column(table, id_key))
    if len(set(ids_in)) != n:
        seen: set = set()
        dup = [i for i in ids_in if i in seen or seen.add(i)]
        raise BundleError(f"{ds}: duplicate ids, e.g. {dup[:5]}")

    columns = list(_get(spec, "columns", []) or [])
    ra_key, dec_key = _get(spec, "ra"), _get(spec, "dec")
    colspec_by_key = {c.key: c for c in columns}
    keys_seen: set = set()
    for c in columns:
        if c.key in keys_seen:
            raise BundleError(f"{ds}: column {c.key!r} declared twice")
        keys_seen.add(c.key)

    # ---- exact rows rule (section 5.5)
    exact_rows = _get(spec, "exact_rows")
    rows_written = (n <= EXACT_ROWS_MAX_N) if exact_rows is None else bool(exact_rows)
    rows_written = rows_written and n > 0

    # ---- positions (in input order first, for the sort key)
    pos_enc = None
    pos_data = None
    bbox = None
    ra_in = dec_in = None
    if ra_key and dec_key:
        ra_in = to_float_array(_column(table, ra_key))
        dec_in = to_float_array(_column(table, dec_key))
        okp = np.isfinite(ra_in) & np.isfinite(dec_in)
        if okp.any():
            override = _get(colspec_by_key.get(ra_key), "enc", "auto") if ra_key in colspec_by_key else "auto"
            pos_enc = override if override in ("u32lin", "i16off") else ("u32lin" if n <= LOD_MIN_N else "i16off")
            bbox = {"ra": [float(ra_in[okp].min()), float(ra_in[okp].max())],
                    "dec": [float(dec_in[okp].min()), float(dec_in[okp].max())]}
            if pos_enc == "i16off" or (n > LOD_MIN_N or _get(spec, "order", "auto") == "lod"):
                pos_data = encode_i16off(ra_in, dec_in, step_mas=step_mas)
                if pos_enc == "i16off":
                    for wmsg in pos_data.get("warnings", []):
                        warn(wmsg)
        else:
            warn("ra/dec columns have no finite values; positions not written")

    # ---- order (section 5.4)
    sort_key = None
    if pos_data is not None:
        sort_key = tile_morton_key(pos_data["tile"], pos_data["dra"], pos_data["ddec"])
    order = make_order(n, _get(spec, "order", "auto") or "auto", seed, sort_key)
    s2i = order.stored_to_input
    ids = order.apply(ids_in)
    id_to_row = {k: i for i, k in enumerate(ids)}

    files: list[str] = []
    cols_meta: list[dict] = []
    numeric_cache: dict[str, np.ndarray] = {}   # stored-order float values for density

    def put(rel_dir: str, stem: str, data: bytes, kind: str) -> str:
        p = w.put(rel_dir, stem, data, "bin", kind)
        files.append(p)
        return p

    starts = head_chunk_starts(n)
    parts = _chunks(n, starts)
    chunked = starts is not None or len(parts) > 1

    def write_codes(key: str, enc: Encoded | None, payload_fn=None, kind_single="column") -> dict:
        """Write one column (chunked when needed) -> {'file': ...} or {'chunk_files': [...]}."""
        out_files = []
        for ci, (a, b) in enumerate(parts):
            data = payload_fn(a, b) if payload_fn is not None else enc.file_bytes(a, b)
            stem = f"{key}.c{ci:03d}" if chunked else key
            out_files.append(put("cols", stem, data, "chunk" if chunked else kind_single))
        return {"chunk_files": out_files} if chunked else {"file": out_files[0]}

    # ---- stored columns
    skip = {id_key}
    if pos_enc is not None:
        skip |= {ra_key, dec_key}
    row_keys: list[str] = []          # keys carried in row chunks
    row_vals: dict[str, list] = {}

    if rows_written:
        row_keys.append(id_key)
        row_vals[id_key] = ids
        if ra_key and dec_key and ra_in is not None:
            for k in (ra_key, dec_key):
                if k in colspec_by_key and not _get(colspec_by_key[k], "exact", True):
                    continue
                row_keys.append(k)
                row_vals[k] = order.apply(json_values(_column(table, k)))

    exact_gpu_default = _get(spec, "exact_gpu", True)
    for c in columns:
        key = c.key
        role = _get(c, "role", "value")
        if not COL_KEY_RE.match(key):
            raise BundleError(f"{ds}: stored column key {key!r} must match [A-Za-z_][A-Za-z0-9_.]* "
                              f"(rename it and keep the original as the label)")
        raw = _column(table, key)
        if getattr(raw, "ndim", 1) > 1:
            raise BundleError(f"{ds}: column {key!r} is multi-dimensional")
        in_rows = rows_written and _get(c, "exact", True) and key not in row_vals
        if in_rows:
            row_keys.append(key)
            row_vals[key] = order.apply(json_values(raw))
        if key in skip:
            continue
        raw_s = raw[s2i] if not order.is_identity else raw
        fvals = None
        if role not in ("category", "flag", "id", "text", "url", "image", "limit"):
            fvals = to_float_array(raw_s)
        col_gpu = _get(c, "exact_gpu", None)
        enc_name, step = resolve_enc(c, fvals, rows_written=rows_written,
                                     exact_gpu=exact_gpu_default if col_gpu is None else bool(col_gpu))
        if enc_name is None:
            continue
        meta: dict = {"key": key}
        if _get(c, "label") is not None:
            meta["label"] = c.label
        meta["unit"] = _get(c, "unit", "") or ""
        meta["role"] = role
        for k in ("filter", "err", "group", "validity", "fmt"):
            v = _get(c, k)
            if v is not None:
                meta[k] = v
        if _get(c, "note"):
            meta["note"] = c.note
        is_log = bool(_get(c, "log", False))
        if is_log:
            meta["log"] = True
        inv = _get(c, "invert")
        if inv is None and role == "mag":
            inv = True
        if inv is not None:
            meta["invert"] = bool(inv)
        clip = _get(c, "clip", "none") or "none"
        lo, hi = _get(c, "lo"), _get(c, "hi")

        # sparse limit kinds from the legacy limit-code sibling
        lim_kinds = None
        lim_key = _get(c, "limit")
        if lim_key:
            lim_kinds = _limit_kinds(_column(table, lim_key))
            if not order.is_identity:
                lim_kinds = lim_kinds[s2i]

        enc_obj: Encoded | None = None
        payload_fn = None
        if fvals is None and enc_name in _NUMERIC_ENCS:
            fvals = to_float_array(raw_s)
        if enc_name == "u16q":
            if is_log and clip != "none" and lo is None and hi is None:
                # section 5.2: log columns take lo/hi from the GC log_dim_stats 0.1/99.9 rule
                pos_v = fvals[np.isfinite(fvals) & (fvals > 0)]
                if pos_v.size:
                    st = codec.log_dim_stats(pos_v)
                    lo, hi = st["lo"], st["hi"]
            enc_obj = encode_u16q(fvals, lo, hi, step, clip, label=key)
        elif enc_name == "u16log":
            keep = lim_kinds > 0 if lim_kinds is not None else None
            if clip != "none" and lo is None and hi is None:
                pos_v = fvals[np.isfinite(fvals) & (fvals > 0)]
                if pos_v.size:
                    st = codec.log_dim_stats(pos_v)
                    lo, hi = st["lo"], st["hi"]
            enc_obj = encode_u16log(fvals, lo, hi, step, clip, label=key, keep=keep)
        elif enc_name == "u8q":
            enc_obj = encode_u8q(fvals, lo, hi, step, clip, label=key)
        elif enc_name == "u8log":
            enc_obj = encode_u8log(fvals, lo, hi, clip, label=key)
        elif enc_name in ("cat8", "cat16"):
            # enc "auto": the encoder picks cat8, or cat16 above 255 levels (section 5.3)
            explicit = (_get(c, "enc", "auto") or "auto") != "auto"
            try:
                enc_obj = encode_cat(raw_s, _get(c, "levels"), enc=enc_name if explicit else None,
                                     label=key)
            except ValueError as exc:
                raise BundleError(f"{ds}: column {key!r}: {exc}") from exc
        elif enc_name == "bits":
            enc_obj = encode_bits(raw_s)
        elif enc_name == "f32":
            enc_obj = encode_f32(fvals)
        elif enc_name == "u32":
            enc_obj = encode_u32(fvals)
        elif enc_name == "u32lin":
            fin = fvals[np.isfinite(fvals)]
            a = float(fin.min()) if fin.size else 0.0
            b = float(fin.max()) if fin.size else 0.0
            enc_obj = encode_u32lin(fvals, a if lo is None else lo, b if hi is None else hi)
        elif enc_name == "str":
            vals = json_values(raw_s)
            strs = [None if v is None else str(v) for v in vals]
            payload_fn = (lambda a, b, s=strs: gz(encode_str(s[a:b])))
            meta["enc"] = "str"
        else:
            raise BundleError(f"{ds}: column {key!r}: unsupported encoding {enc_name!r}")

        if enc_obj is not None:
            for wmsg in enc_obj.warnings:
                warn(wmsg)
            m = dict(enc_obj.meta)
            meta["enc"] = m.pop("enc")
            for k in ("lo", "hi", "step", "clip", "shuffle", "levels"):
                if k in m:
                    meta[k] = m[k]
        meta.update(write_codes(key, enc_obj, payload_fn))

        # statistics (section 5.2)
        if meta["enc"] in _NUMERIC_ENCS and fvals is not None:
            valid = enc_obj.codes != _missing_code(meta["enc"]) if meta["enc"] != "f32" else np.isfinite(fvals)
            st = col_stats(fvals[valid], log=is_log)
            meta["center"] = st["center"]
            meta["scale"] = st["scale"]
            meta["nvalid"] = st["nvalid"]
            if "quantiles" in st:
                meta["quantiles"] = st["quantiles"]
            numeric_cache[key] = fvals
        elif meta["enc"] in ("cat8", "cat16"):
            miss = codec.CAT8_MISSING if meta["enc"] == "cat8" else codec.CAT16_MISSING
            meta["nvalid"] = int(np.count_nonzero(enc_obj.codes != miss))
            lv = set(meta["levels"])
            colors = dict(_get(c, "colors") or {})
            off = list(_get(c, "default_off", []) or [])
            unknown = sorted((set(colors) | set(off)) - lv)
            if unknown:
                warn(f"column {key!r}: colors/default_off name levels absent from the data, "
                     f"dropped: {unknown[:5]}")
            colors = {k: v for k, v in colors.items() if k in lv}
            if colors:
                meta["colors"] = colors
            meta["default_off"] = [v for v in off if v in lv]

        # sparse limits table (section 5.3 'sparse')
        kinds = {}
        if enc_obj is not None:
            kinds.update(enc_obj.limits)
        if lim_kinds is not None:
            for i in np.flatnonzero(lim_kinds):
                kinds[int(i)] = int(lim_kinds[i])     # legacy limit codes take precedence
        if kinds:
            idx = np.fromiter(kinds.keys(), dtype=np.int64, count=len(kinds))
            kk = np.fromiter(kinds.values(), dtype=np.int64, count=len(kinds))
            p = put("limits", key, gz(encode_sparse(idx, kk)), "limits")
            meta["limits"] = {"file": p, "count": int(len(kinds))}
        cols_meta.append(meta)

    # ---- _src_index (section 5.4)
    if not order.is_identity:
        enc_obj = encode_u32(s2i.astype(np.float64))
        meta = {"key": SRC_INDEX_KEY, "label": "input row", "unit": "", "role": "value",
                "enc": "u32", "shuffle": True}
        meta.update(write_codes(SRC_INDEX_KEY, enc_obj))
        cols_meta.append(meta)

    # ---- positions
    pos_meta = None
    if pos_enc == "u32lin":
        ra_s, dec_s = order.apply(ra_in), order.apply(dec_in)
        # a row with only one finite coordinate has no position (as in i16off)
        bad = ~(np.isfinite(ra_s) & np.isfinite(dec_s))
        if bad.any():
            ra_s = np.where(bad, np.nan, ra_s)
            dec_s = np.where(bad, np.nan, dec_s)
        pf = {}
        for k, v, (lo, hi) in (("ra", ra_s, bbox["ra"]), ("dec", dec_s, bbox["dec"])):
            e = encode_u32lin(v, lo, hi)
            pf[k] = put("pos", k, e.file_bytes(), "pos")
        pos_meta = {"enc": "u32lin", "files": pf}
    elif pos_enc == "i16off":
        pf = {}
        for k in ("tile", "dra", "ddec"):
            arr = order.apply(pos_data[k])
            enc_k = "u16" if k == "tile" else "i16"
            pf[k] = put("pos", k, file_bytes(enc_k, arr, shuffle=True), "pos")
        pos_meta = {"enc": "i16off", "step_mas": pos_data["step_mas"],
                    "tile_refs": pos_data["tile_refs"], "files": pf}

    # ---- ids + aliases
    if not chunked:
        ids_meta = {"file": put("", "ids", gz(encode_str(ids)), "ids"), "enc": "str", "chunks": None}
    else:
        ch = [put("", f"ids.c{ci:03d}", gz(encode_str(ids[a:b])), "ids") for ci, (a, b) in enumerate(parts)]
        ids_meta = {"file": None, "enc": "str", "chunks": ch}
    alias_to_row = _aliases(spec, table, ids_in, order, id_to_row, warn)
    if alias_to_row:
        p = w.put("", "aliases", dumps_json(alias_to_row), "json", "aliases")
        files.append(p)
        ids_meta["alias_file"] = p

    # ---- exact row chunks (section 5.5)
    rows_meta = None
    if rows_written:
        rfiles = []
        for ci, a in enumerate(range(0, n, ROW_CHUNK)):
            b = min(a + ROW_CHUNK, n)
            chunk = [{k: row_vals[k][i] for k in row_keys} for i in range(a, b)]
            rfiles.append(put("rows", f"r{ci:04d}", gz(dumps_json(chunk)), "rows"))
        rows_meta = {"chunk": ROW_CHUNK, "files": rfiles, "cols": row_keys}

    # ---- presets, density
    presets = []
    for p in _get(spec, "presets", []) or []:
        d = {"key": p.key, "label": p.label, "kind": p.kind}
        data = dict(_get(p, "data", {}) or {})
        for k in ("key", "label", "kind"):
            data.pop(k, None)
        d.update(data)
        presets.append(d)

    def colvals(k: str) -> np.ndarray:
        if k in numeric_cache:
            return numeric_cache[k]
        v = to_float_array(_column(table, k))
        v = order.apply(v)
        numeric_cache[k] = v
        return v

    dens_specs = list(_get(spec, "density", []) or [])
    have = {d.key for d in dens_specs}
    if n > T0_MAX or _get(spec, "context", False):
        for p in presets:
            if p["kind"] == "axes" and p["key"] not in have and "x" in p and "y" in p:
                dens_specs.append(_AutoDensity(p["key"], p["x"], p["y"]))
                have.add(p["key"])
    dens_meta = []
    for d in dens_specs:
        try:
            x = eval_linear(d.x, colvals)
            y = eval_linear(d.y, colvals)
            fn, vexpr = parse_stat(_get(d, "stat"))
            vals = eval_linear(vexpr, colvals) if vexpr else None
        except (DensityError, BundleError) as exc:
            warn(f"density {d.key!r} skipped: {exc}")
            continue
        entry, data = build_density(d.key, x, y, x_expr=d.x, y_expr=d.y, xr=_get(d, "xr"),
                                    yr=_get(d, "yr"), nx=_get(d, "nx", 256), ny=_get(d, "ny", 256),
                                    stat=_get(d, "stat"), values=vals)
        p = put("density", d.key, data, "density")
        entry["file"] = p
        dens_meta.append(entry)

    # ---- manifest
    man: dict = {"schema": "pk-catalog/1", "id": ds}
    if _get(spec, "title"):
        man["title"] = spec.title
    man["n"] = n
    if build:
        man["build"] = build
    man["order"] = order.manifest()
    man["chunk_rows"] = CHUNK_ROWS
    if starts is not None:
        man["chunk_starts"] = list(starts)
    man["ids"] = ids_meta
    if pos_meta is not None:
        man["pos"] = pos_meta
    if bbox is not None:
        man["bbox"] = bbox
    man["cols"] = cols_meta
    virtual = []
    for v in _get(spec, "virtual", []) or []:
        e = {"key": v.key}
        if _get(v, "label") is not None:
            e["label"] = v.label
        if _get(v, "unit"):
            e["unit"] = v.unit
        if _get(v, "group") is not None:
            e["group"] = v.group
        virtual.append(e)
    if virtual:
        man["virtual"] = virtual
    groups = [{"key": g.key, "label": g.label, "dims": list(g.dims)} for g in _get(spec, "groups", []) or []]
    if groups:
        man["groups"] = groups
    if presets:
        man["presets"] = presets
    if dens_meta:
        man["density"] = dens_meta
    if rows_meta is not None:
        man["rows"] = rows_meta
    mf = _get(spec, "manifold")
    if mf is not None:
        md = {"dims": list(mf.dims), "default_frame": _get(mf, "default_frame", "pca"),
              "fade_width": FADE_WIDTH}
        frames = _get(mf, "frames") or {}
        if frames:
            md["frames"] = {k: [float(x) for x in v] for k, v in frames.items()}
        man["manifold"] = md
    if links:
        man["links"] = links
    prov = dict(provenance or {})
    cprov = list(_get(spec, "catalogs_provenance", []) or [])
    if cprov and "catalogs" not in prov:
        prov["catalogs"] = cprov
    if prov:
        man["provenance"] = prov
    if _get(spec, "caveats"):
        man["caveats"] = list(spec.caveats)

    # ---- first paint (section 5.11, IMPLEMENTATION_NOTES 98/99): see first_paint_files
    fp_files = first_paint_files(man, context=bool(_get(spec, "context", False)))
    for e in w.ledger.entries:
        if e.dataset == w.dataset and e.path in {w.root_rel(f) for f in fp_files}:
            e.first_paint = True
    files_set = {w.root_rel(f) for f in files}
    data_bytes = sum(e.bytes for e in w.ledger.entries if e.path in files_set)
    fp_bytes = sum(e.bytes for e in w.ledger.entries if e.path in {w.root_rel(f) for f in fp_files})
    man["budget"] = {"first_paint_bytes": 0, "total_bytes": 0}
    for _ in range(4):
        size = len(dumps_json(man))
        new = {"first_paint_bytes": fp_bytes + size, "total_bytes": data_bytes + size}
        if new == man["budget"]:
            break
        man["budget"] = new
    w.put_stable("manifest.json", dumps_json(man), "manifest", first_paint=True)
    return CatalogResult(man, order, ids, id_to_row, alias_to_row, files, warnings, w)


@dataclass
class _AutoDensity:
    key: str
    x: str
    y: str
    nx: int = 256
    ny: int = 256
    xr: tuple | None = None
    yr: tuple | None = None
    stat: str | None = None


def _missing_code(enc: str) -> int:
    if enc in ("u16q", "u16log", "u8q", "u8log"):
        return 0
    if enc in ("u32", "u32lin"):
        return codec.U32_MISSING
    return -1


def _head_file(meta: dict) -> str | None:
    """First (head) chunk of a column or ids entry: ``chunk_files[0]`` / ``chunks[0]`` or ``file``."""
    for k in ("chunk_files", "chunks"):
        v = meta.get(k)
        if v:
            return v[0]
    return meta.get("file")


def first_paint_files(man: dict, context: bool = False) -> list[str]:
    """Data files a page reads before first paint (paths relative to the dataset directory).

    The manifest itself is not listed (the caller adds its size). For a context catalog
    (``context=True``: drawn as a density backdrop, never listed) the set is every density
    grid (the backdrop of each axes preset), or the two axis columns when it has no grid; no
    row chunk and no head chunk. Otherwise the set is:

    * the plot: the first count density grid (``density[]`` without ``stat``; the first grid
      when all are binned statistics), else every file (all chunks) of the two axis columns of
      the first ``axes`` preset (the first two numeric columns without one), since the scatter
      reads its axes with an unprefixed ``client.values()``;
    * the list's first screen (IMPLEMENTATION_NOTES 99, ``client.rows(first, last)``):
      ``rows.files[0]`` when the catalog has exact row chunks; otherwise the head chunk of
      every stored column (the worker's quantized ``rows()`` decodes all of them) and the
      head ids chunk. Nothing is read for ``n == 0``.

    With ``chunk_starts`` the head chunk is the LOD base sample (rows ``[0, LOD_BASE)``); without
    it, it is the first ``chunk_rows`` rows (the whole column when ``n <= chunk_rows``). Positions
    are never part of first paint (sky markers load them afterwards).
    """
    out: list[str] = []

    def add(p: str | None) -> None:
        if p and p not in out:
            out.append(p)

    cols = [c for c in man.get("cols") or [] if isinstance(c, dict)]
    by_key = {c.get("key"): c for c in cols}
    dens = [d for d in man.get("density") or [] if isinstance(d, dict) and d.get("file")]
    if context and dens:
        for d in dens:
            add(d["file"])
        return out
    if dens:
        count = [d for d in dens if not d.get("stat") or d.get("stat") == "count"]
        add((count or dens)[0]["file"])
    else:
        # the scatter loads its axes with an unprefixed client.values(): every chunk
        for k in _axis_keys(list(man.get("presets") or []), cols):
            for f in codec.column_files(by_key[k]):
                add(f)
    if context:
        return out
    n = man.get("n") or 0
    rows = man.get("rows") or {}
    if rows.get("files"):
        add(rows["files"][0])
    elif n > 0:
        for c in cols:
            add(_head_file(c))
        add(_head_file(man.get("ids") or {}))
    return out


def _axis_keys(presets: list[dict], cols_meta: list[dict]) -> list[str]:
    stored = [c["key"] for c in cols_meta]
    for p in presets:
        if isinstance(p, dict) and p.get("kind") == "axes" and "x" in p and "y" in p:
            ks: list[str] = []
            for e in (p["x"], p["y"]):
                try:
                    for k in base_keys(e):
                        if k in stored and k not in ks:
                            ks.append(k)
                except DensityError:
                    continue
            if ks:
                return ks[:2]
    num = [c["key"] for c in cols_meta if c.get("enc") in ("u16q", "u16log", "u8q", "u8log", "f32")
           and c["key"] != SRC_INDEX_KEY]
    return num[:2]


def _aliases(spec: Any, table: Any, ids_in: list[str], order: RowOrder, id_to_row: dict[str, int],
             warn: Callable[[str], None]) -> dict[str, int]:
    """``{alias: stored_row}``; aliases equal to another id or claimed by two rows are dropped."""
    src = _get(spec, "aliases")
    if not src:
        return {}
    pairs: list[tuple[str, str]] = []
    if isinstance(src, str):
        col = json_values(_column(table, src))
        for rid, v in zip(ids_in, col):
            if v is None:
                continue
            for a in str(v).split("|"):
                pairs.append((rid, a.strip()))
    elif isinstance(src, dict):
        for rid, al in src.items():
            if isinstance(al, str):
                al = [al]
            for a in al or []:
                pairs.append((str(rid), str(a).strip()))
    else:
        raise BundleError("aliases must be a dict or a column name")
    out: dict[str, int] = {}
    bad: set = set()
    for rid, a in pairs:
        if not a or a == rid:
            continue
        if rid not in id_to_row:
            warn(f"alias {a!r} refers to unknown id {rid!r}; dropped")
            continue
        if a in id_to_row:
            warn(f"alias {a!r} of {rid!r} equals another id; dropped")
            continue
        row = id_to_row[rid]
        if a in out and out[a] != row:
            bad.add(a)
            continue
        out[a] = row
    for a in sorted(bad):
        warn(f"alias {a!r} is claimed by several rows; dropped")
        out.pop(a, None)
    return out
