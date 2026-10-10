"""Row order and level-of-detail permutation (ARCHITECTURE.md section 5.4).

* ``N <= LOD_MIN_N`` (131072): input order is kept (``order.kind = "input"``).
* ``N > LOD_MIN_N``: a seeded permutation (``numpy.random.default_rng(seed)``) is cut into
  levels with cumulative prefix sizes ``LOD_BASE * 2**k`` ending at N, so every prefix is a
  uniform random subsample. Inside each level rows are sorted by ``(tile, morton16(dra, ddec))``
  using the ``i16off`` tile and offsets (sky locality for ``skyWindow``, better gzip).
* Whenever the order is not ``input`` the catalog stores the original input index as the
  ``u32`` column ``_src_index``.

``stored_to_input[j]`` is the input row stored at position ``j``;
``input_to_stored[i]`` is the inverse.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..constants import LOD_BASE, LOD_MIN_N
from .codec import I16_MISSING, morton16

__all__ = ["RowOrder", "lod_levels", "choose_kind", "tile_morton_key", "make_order",
           "SRC_INDEX_KEY"]

SRC_INDEX_KEY = "_src_index"


@dataclass
class RowOrder:
    kind: str                         # "input" | "lod"
    stored_to_input: np.ndarray       # int64, length n
    levels: list[int] | None = None
    seed: int | None = None
    sort: str | None = None           # "tile-morton" when the within-level sort was applied

    @property
    def n(self) -> int:
        return int(self.stored_to_input.size)

    @property
    def input_to_stored(self) -> np.ndarray:
        inv = np.empty_like(self.stored_to_input)
        inv[self.stored_to_input] = np.arange(self.n, dtype=self.stored_to_input.dtype)
        return inv

    @property
    def is_identity(self) -> bool:
        return self.kind == "input"

    def apply(self, a):
        """Reorder an input-ordered array (or list) into stored order."""
        if self.is_identity:
            return a
        if isinstance(a, np.ndarray):
            return a[self.stored_to_input]
        return [a[i] for i in self.stored_to_input]

    def manifest(self) -> dict:
        d: dict = {"kind": self.kind}
        if self.kind == "lod":
            d["seed"] = int(self.seed) if self.seed is not None else 0
            d["levels"] = list(self.levels or [self.n])
            if self.sort:
                d["sort"] = self.sort
        return d


def lod_levels(n: int, base: int = LOD_BASE) -> list[int]:
    """Cumulative prefix sizes ``base * 2**k`` below ``n``, ending at ``n``.

    ``lod_levels(464489) == [32768, 65536, 131072, 262144, 464489]``; ``lod_levels(0) == []``.
    """
    if n <= 0:
        return []
    out = []
    s = base
    while s < n:
        out.append(s)
        s *= 2
    out.append(n)
    return out


def choose_kind(n: int, mode: str = "auto") -> str:
    """``auto`` -> ``lod`` when ``n > LOD_MIN_N`` else ``input``."""
    if mode not in ("auto", "input", "lod"):
        raise ValueError(f"unknown order mode {mode!r}")
    if mode == "auto":
        return "lod" if n > LOD_MIN_N else "input"
    if mode == "lod" and n == 0:
        return "input"
    return mode


def tile_morton_key(tile: np.ndarray, dra: np.ndarray, ddec: np.ndarray) -> np.ndarray:
    """uint64 sort key ``tile << 32 | morton16(dra + 32768, ddec + 32768)``.

    Rows without a position (``dra``/``ddec`` = -32768) sort after every positioned row.
    """
    tile = np.asarray(tile).astype(np.uint64)
    x = (np.asarray(dra).astype(np.int32) + 32768).astype(np.uint16)
    y = (np.asarray(ddec).astype(np.int32) + 32768).astype(np.uint16)
    key = (tile << np.uint64(32)) | morton16(x, y).astype(np.uint64)
    miss = (np.asarray(dra) == I16_MISSING) | (np.asarray(ddec) == I16_MISSING)
    key[miss] = np.uint64(0xFFFFFFFFFFFFFFFF)
    return key


def make_order(n: int, mode: str = "auto", seed: int = 20261010,
               sort_key: np.ndarray | None = None) -> RowOrder:
    """Build the stored row order.

    ``sort_key`` (input order, e.g. :func:`tile_morton_key` of the i16off positions) sorts rows
    inside each LOD level; without it the levels keep the permutation order.
    """
    kind = choose_kind(n, mode)
    if kind == "input":
        return RowOrder("input", np.arange(n, dtype=np.int64))
    rng = np.random.default_rng(seed)
    perm = rng.permutation(n).astype(np.int64)
    levels = lod_levels(n)
    sort = None
    if sort_key is not None:
        sort_key = np.asarray(sort_key)
        if sort_key.shape != (n,):
            raise ValueError("sort_key must have one entry per row")
        start = 0
        for stop in levels:
            seg = perm[start:stop]
            perm[start:stop] = seg[np.argsort(sort_key[seg], kind="stable")]
            start = stop
        sort = "tile-morton"
    return RowOrder("lod", perm, levels, int(seed), sort)
