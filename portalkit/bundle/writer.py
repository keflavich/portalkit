"""Content-hash naming, atomic writes inside the staging root, and the budget ledger.

ARCHITECTURE.md sections 5.1 and 5.11.

* Every file portalkit writes is named ``<stem>.<h8>.<ext>`` where ``h8`` is the first 8 hex
  digits of the sha256 of the bytes on disk (:func:`h8`, :func:`hashed_name`). The stable-name
  documents (``manifest.json``, ``index.json``, ``images.json`` ...) are written with
  :meth:`BundleWriter.put_stable`.
* All writes go through :class:`BundleWriter`, which refuses any target outside its staging
  root and any path that crosses a symlink, and writes atomically (temporary file in the same
  directory, then ``os.replace``).
* :class:`Ledger` records every file with its size, kind and dataset, accumulates warnings,
  and checks the section 5.11 size budgets (:meth:`Ledger.check`).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..constants import COL_FILE_MAX, MANIFEST_MAX, SHARD_MAX

__all__ = [
    "BundleError",
    "StagingError",
    "h8",
    "hashed_name",
    "to_jsonable",
    "dumps_json",
    "atomic_write",
    "Ledger",
    "LedgerEntry",
    "BundleWriter",
    "ROW_CHUNK_SOFT_MAX",
    "RECORD_FILE_SOFT_MAX",
    "IMAGES_SOFT_MAX",
]

#: section 5.11 soft budgets
ROW_CHUNK_SOFT_MAX = 524288
RECORD_FILE_SOFT_MAX = 2 * 1024 * 1024
IMAGES_SOFT_MAX = 4 * 1024 * 1024


class BundleError(ValueError):
    """Invalid input to a bundle writer (bad spec, duplicate ids, unencodable values)."""


class StagingError(RuntimeError):
    """A write would leave the staging root or pass through a symlink."""


# ---------------------------------------------------------------------------
# naming
# ---------------------------------------------------------------------------

def h8(data: bytes) -> str:
    """First 8 hex digits of the sha256 of ``data``."""
    return hashlib.sha256(data).hexdigest()[:8]


def hashed_name(stem: str, data: bytes, ext: str = "bin") -> str:
    """``<stem>.<h8>.<ext>``; with an empty stem, ``<h8>.<ext>`` (record files ``r/<h8>.bin``)."""
    ext = ext.lstrip(".")
    return f"{stem}.{h8(data)}.{ext}" if stem else f"{h8(data)}.{ext}"


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

def to_jsonable(obj: Any) -> Any:
    """Convert numpy scalars/arrays, tuples and Paths to JSON types; NaN/inf become ``None``.

    Section 5.1: JSON is written with ``allow_nan=False`` and NaN is written as ``null``.
    """
    if obj is None or isinstance(obj, (str, bool)):
        return obj
    if isinstance(obj, (int, np.integer)) and not isinstance(obj, np.bool_):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        f = float(obj)
        return f if math.isfinite(f) else None
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return to_jsonable(obj.tolist())
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8")
    if obj is np.ma.masked:
        return None
    raise BundleError(f"value of type {type(obj).__name__} is not JSON-serialisable")


def dumps_json(obj: Any, *, indent: int | None = None) -> bytes:
    """UTF-8 JSON, ``ensure_ascii=False``, ``allow_nan=False``, NaN written as ``null``."""
    seps = (",", ":") if indent is None else (",", ": ")
    return json.dumps(to_jsonable(obj), ensure_ascii=False, allow_nan=False,
                      separators=seps, indent=indent).encode("utf-8")


# ---------------------------------------------------------------------------
# atomic writes
# ---------------------------------------------------------------------------

def atomic_write(path: Path, data: bytes, *, fsync: bool = False) -> None:
    """Write ``data`` to ``path`` through a temporary file in the same directory + ``os.replace``.

    Readers never observe a partially written file. The temporary file is removed when the
    write fails.
    """
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            if fsync:
                fh.flush()
                os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        if tmp.exists():
            tmp.unlink()
        raise


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------

@dataclass
class LedgerEntry:
    path: str                  # relative to the ledger's staging root, '/' separated
    bytes: int
    kind: str                  # column|chunk|ids|pos|aliases|limits|rows|density|manifest|shard|record|index|images|json|other
    dataset: str | None = None
    first_paint: bool = False


#: kind -> (limit, hard?) for section 5.11
_BUDGETS: dict[str, tuple[int, bool]] = {
    "manifest": (MANIFEST_MAX, True),
    "column": (COL_FILE_MAX, True),
    "chunk": (COL_FILE_MAX, True),
    "ids": (COL_FILE_MAX, True),
    "pos": (COL_FILE_MAX, True),
    "limits": (COL_FILE_MAX, True),
    "shard": (SHARD_MAX, True),
    "rows": (ROW_CHUNK_SOFT_MAX, False),
    "record": (RECORD_FILE_SOFT_MAX, False),
    "images": (IMAGES_SOFT_MAX, False),
}


@dataclass
class Ledger:
    """Budget ledger: every written file, warnings, and section 5.11 checks."""

    entries: list[LedgerEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add(self, path: str, nbytes: int, kind: str = "other", dataset: str | None = None,
            first_paint: bool = False) -> LedgerEntry:
        # a re-write of the same path replaces the previous entry
        self.entries = [e for e in self.entries if e.path != path]
        ent = LedgerEntry(path, int(nbytes), kind, dataset, bool(first_paint))
        self.entries.append(ent)
        return ent

    def warn(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)

    def total(self, dataset: str | None = None) -> int:
        return sum(e.bytes for e in self.entries if dataset is None or e.dataset == dataset)

    def first_paint(self, dataset: str | None = None) -> int:
        return sum(e.bytes for e in self.entries
                   if e.first_paint and (dataset is None or e.dataset == dataset))

    def by_kind(self, dataset: str | None = None) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.entries:
            if dataset is None or e.dataset == dataset:
                out[e.kind] = out.get(e.kind, 0) + e.bytes
        return out

    def check(self) -> tuple[list[str], list[str]]:
        """Return ``(errors, soft_warnings)`` for the per-file section 5.11 budgets."""
        errors: list[str] = []
        soft: list[str] = []
        for e in self.entries:
            lim = _BUDGETS.get(e.kind)
            if lim is None or e.bytes <= lim[0]:
                continue
            msg = f"{e.path}: {e.bytes} B exceeds the {e.kind} budget of {lim[0]} B"
            (errors if lim[1] else soft).append(msg)
        return errors, soft

    def to_dict(self) -> dict:
        datasets = sorted({e.dataset for e in self.entries if e.dataset is not None})
        errors, soft = self.check()
        return {
            "files": [{"path": e.path, "bytes": e.bytes, "kind": e.kind, "dataset": e.dataset,
                       "first_paint": e.first_paint} for e in self.entries],
            "datasets": {d: {"total_bytes": self.total(d), "first_paint_bytes": self.first_paint(d),
                             "by_kind": self.by_kind(d)} for d in datasets},
            "total_bytes": self.total(),
            "warnings": list(self.warnings) + soft,
            "errors": errors,
        }


# ---------------------------------------------------------------------------
# writer
# ---------------------------------------------------------------------------

class BundleWriter:
    """Writes files under ``root/base`` only, atomically, with content-hash names.

    ``root`` is the staging root (the only place the builder writes). ``base`` is a '/'
    separated sub-directory of it (e.g. ``"data/src"``); returned paths are relative to
    ``base`` because manifests reference files relative to their own directory.
    """

    def __init__(self, root: str | Path, base: str = "", ledger: Ledger | None = None,
                 dataset: str | None = None, fsync: bool = False):
        self.root = Path(os.path.abspath(root))
        self.base = base.strip("/")
        self.ledger = ledger if ledger is not None else Ledger()
        self.dataset = dataset
        self.fsync = fsync
        self._check_inside(self.root / self.base if self.base else self.root)

    # -- helpers -----------------------------------------------------------
    def sub(self, rel: str, dataset: str | None = None) -> "BundleWriter":
        """A writer for ``base/rel`` sharing this ledger."""
        base = f"{self.base}/{rel.strip('/')}" if self.base else rel.strip("/")
        return BundleWriter(self.root, base, self.ledger,
                            dataset if dataset is not None else self.dataset, self.fsync)

    @property
    def dir(self) -> Path:
        return self.root / self.base if self.base else self.root

    def path(self, rel: str) -> Path:
        return self.dir / rel

    def root_rel(self, rel: str) -> str:
        return f"{self.base}/{rel}" if self.base else rel

    def _check_inside(self, target: Path) -> None:
        """Refuse targets outside ``root`` and any existing symlink between root and target."""
        target = Path(os.path.abspath(target))
        try:
            relp = target.relative_to(self.root)
        except ValueError as exc:
            raise StagingError(f"{target} is outside the staging root {self.root}") from exc
        if ".." in relp.parts:
            raise StagingError(f"{target}: '..' in staged path")
        cur = self.root
        if cur.is_symlink():
            raise StagingError(f"staging root {cur} is a symlink")
        for part in relp.parts:
            cur = cur / part
            if cur.is_symlink():
                raise StagingError(f"refusing to write through symlink {cur}")

    def _write(self, rel: str, data: bytes, kind: str, first_paint: bool) -> str:
        rel = rel.strip("/")
        if not rel or rel.startswith("/") or "\\" in rel or ".." in rel.split("/"):
            raise StagingError(f"bad relative path {rel!r}")
        target = self.dir / rel
        self._check_inside(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._check_inside(target)   # mkdir may have raced with a symlink
        same = False
        if target.is_file():
            st = target.stat()
            if st.st_size == len(data):
                same = target.read_bytes() == data
        if not same:
            atomic_write(target, data, fsync=self.fsync)
        self.ledger.add(self.root_rel(rel), len(data), kind, self.dataset, first_paint)
        return rel

    # -- public ------------------------------------------------------------
    def put(self, rel_dir: str, stem: str, data: bytes, ext: str = "bin", kind: str = "other",
            first_paint: bool = False) -> str:
        """Write content-hashed ``rel_dir/<stem>.<h8>.<ext>``; return its path relative to base."""
        name = hashed_name(stem, data, ext)
        rel = f"{rel_dir.strip('/')}/{name}" if rel_dir.strip("/") else name
        return self._write(rel, data, kind, first_paint)

    def put_stable(self, rel: str, data: bytes, kind: str = "other", first_paint: bool = False) -> str:
        """Write a stable-name document (manifest.json, index.json, images.json, ...)."""
        return self._write(rel, data, kind, first_paint)

    def put_json(self, rel_dir: str, stem: str, obj: Any, kind: str = "json",
                 first_paint: bool = False) -> str:
        """Content-hashed JSON document ``<stem>.<h8>.json``."""
        return self.put(rel_dir, stem, dumps_json(obj), "json", kind, first_paint)

    def put_stable_json(self, rel: str, obj: Any, kind: str = "json", first_paint: bool = False) -> str:
        return self.put_stable(rel, dumps_json(obj), kind, first_paint)
