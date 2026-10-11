"""Shared NIRSpec helpers for the MSA spectrum families (nirspec6927, nirspec2640, ...).

The legacy generators stack every grating of a source into one wavelength-sorted table and
plot the s2d products one grating at a time. The unified viewers want labelled grating
segments in the 1D spectrum (``pks1`` segments, toggled per grating) and one stitched 2D
spectrum per source (``pki1``, wavelength strictly increasing). These helpers do that with
plain numpy, so adapters share one implementation:

* :func:`overlap_cuts` splits the nominal grating ranges at the midpoints of their overlaps
  (6927 semantics: G140M / G235M / G395M, edges 0 and 99 um at the ends).
* :func:`fill_cuts` gives every grating its own range and lets a base grating (PRISM) fill
  the gaps between them (2640: PRISM + G140M + G395M).
* :func:`split_segments` turns cuts into ``(i0, i1, label)`` ranges of an ascending array.
* :func:`merge_split` sorts a merged (vstacked) spectrum and splits it at the cuts.
* :func:`stack_segments` keeps the gratings apart: each grating is one ascending segment, in
  order of its first wavelength (the global wavelength array is then not monotonic where
  the gratings overlap; ``spectrum1d`` handles unsorted records).
* :func:`assign_gratings` labels the points of an already merged spectrum (for example a
  legacy ``specdata`` JSON) by their grating, from the per-grating wavelength grids.
* :func:`read_s2d` / :func:`stitch_s2d` read s2d products and stitch them into one
  :class:`portalkit.spec.Image2D` (per-grating trim, NaN pad to a common height,
  concatenation in wavelength order).
* :func:`clean_flux` is the legacy ``clean_flux`` (JWST_Archive_Spectra outlier rule).
"""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence

import numpy as np

__all__ = ["GRATING_RANGE", "BASE_GRATING", "MAX_FLUX", "overlap_cuts", "fill_cuts", "split_segments",
           "merge_split", "stack_segments", "assign_gratings", "read_s2d", "stitch_s2d", "clean_flux"]

#: nominal wavelength coverage of the NIRSpec gratings [um] (JDox, with the standard
#: long-pass filters); the M and H gratings of one band cover the same range
GRATING_RANGE: dict[str, tuple[float, float]] = {
    "G140M": (0.962, 1.878), "G140H": (0.962, 1.878),
    "G235M": (1.648, 3.136), "G235H": (1.648, 3.136),
    "G395M": (2.850, 5.202), "G395H": (2.850, 5.202),
    "PRISM": (0.600, 5.300),
}
#: the low-resolution grating that fills the gaps between the medium/high gratings
BASE_GRATING = "PRISM"
#: Jy; JWST_Archive_Spectra.max_flux (clean_flux ceiling)
MAX_FLUX = 1.0
LO_EDGE, HI_EDGE = 0.0, 99.0


def _order(labels: Iterable[str], ranges: Mapping[str, tuple[float, float]]) -> list[str]:
    seen: list[str] = []
    for g in labels:
        if g in ranges and g not in seen:
            seen.append(g)
    return sorted(seen, key=lambda g: (float(ranges[g][0]), float(ranges[g][1]), g))


def _outer(gs: list[str], rng: Mapping[str, tuple[float, float]]) -> list[str]:
    """Drop the gratings (already in (lo, hi) order) whose range ends inside a kept one's."""
    kept: list[str] = []
    for g in gs:
        if kept and float(rng[g][1]) <= max(float(rng[k][1]) for k in kept):
            continue
        kept.append(g)
    return kept


def overlap_cuts(gratings: Iterable[str], ranges: Mapping[str, tuple[float, float]] | None = None,
                 base: str | None = BASE_GRATING) -> list[tuple[str, float, float]]:
    """``(grating, lo, hi)`` ranges that split a merged spectrum at the overlap midpoints.

    Gratings outside ``ranges`` and the ``base`` grating are ignored; the first range starts
    at 0 um and the last ends at 99 um, so every sample of a merged spectrum falls in one
    range. An empty list when no grating is known.

    A grating whose range lies inside an earlier one's (an M and an H grating of one band) is
    dropped: wavelength alone cannot separate them in a merged spectrum, so the first grating
    in (lo, hi, name) order labels that whole range.
    """
    rng = GRATING_RANGE if ranges is None else ranges
    gs = _outer([g for g in _order(gratings, rng) if g != base], rng)
    edges = [LO_EDGE] + [0.5 * (float(rng[b][0]) + float(rng[a][1])) for a, b in zip(gs, gs[1:])] + [HI_EDGE]
    return [(g, edges[i], edges[i + 1]) for i, g in enumerate(gs)]


def fill_cuts(ranges: Mapping[str, tuple[float, float]], base: str = BASE_GRATING
              ) -> list[tuple[str, float, float]]:
    """Contiguous ``(label, lo, hi)`` ranges: each non-base grating keeps its own range
    (overlaps between them split at the midpoint) and ``base`` fills the gaps, below the
    first and above the last range.

    ``ranges`` maps a label to its (lo, hi) coverage, usually the finite range of the data.
    Labels with a non-finite or empty range are dropped. Without a base grating the result
    equals :func:`overlap_cuts` over these ranges. The base label can appear several times.
    A grating whose range lies inside an earlier one's (an M and an H grating of one band)
    gets no range: the outer grating keeps it. The cuts are contiguous and never overlap.
    """
    rng = {k: (float(v[0]), float(v[1])) for k, v in ranges.items()
           if v is not None and np.isfinite(v[0]) and np.isfinite(v[1]) and float(v[1]) > float(v[0])}
    if base not in rng:
        return overlap_cuts(rng, rng, base=None)
    gs = _outer([g for g in _order(rng, rng) if g != base], rng)
    if not gs:
        return [(base, LO_EDGE, HI_EDGE)]
    spans: list[list] = []
    for g in gs:
        lo, hi = rng[g]
        if spans and lo < spans[-1][2]:              # overlapping medium gratings: midpoint
            mid = 0.5 * (lo + spans[-1][2])
            spans[-1][2] = mid
            lo = mid
        spans.append([g, lo, hi])
    out: list[tuple[str, float, float]] = []
    prev = LO_EDGE
    for g, lo, hi in spans:
        if lo > prev:
            out.append((base, prev, lo))
        if hi > max(lo, prev):
            out.append((g, max(lo, prev), hi))
        prev = max(prev, hi)
    out.append((base, prev, HI_EDGE))
    out = [c for c in out if c[2] > c[1]]
    if any(a[2] != b[1] for a, b in zip(out, out[1:])):
        raise AssertionError(f"fill_cuts produced non-contiguous cuts {out}")
    return out


def split_segments(wave: np.ndarray, cuts: Sequence[tuple[str, float, float]]) -> list[tuple[int, int, str]]:
    """``(i0, i1, label)`` index ranges of an ascending ``wave`` for each cut; empty ranges
    are dropped. NaN wavelengths sort last and fall in no range."""
    w = np.asarray(wave, float)
    out = []
    for g, lo, hi in cuts:
        i0, i1 = int(np.searchsorted(w, lo, "left")), int(np.searchsorted(w, hi, "left"))
        if i1 > i0:
            out.append((i0, i1, str(g)))
    return out


def merge_split(wave, channels: Mapping[str, np.ndarray], gratings: Iterable[str],
                ranges: Mapping[str, tuple[float, float]] | None = None):
    """Sort a merged spectrum (stable) and split it at :func:`overlap_cuts`.

    Returns ``(wave, channels, segments)`` with every channel reordered like ``wave``.
    A spectrum without known gratings gets no segments.
    """
    w = np.asarray(wave, float).ravel()
    ch = {k: np.asarray(v, float).ravel() for k, v in channels.items()}
    for k, v in ch.items():
        if v.size != w.size:
            raise ValueError(f"channel {k!r} has {v.size} values, wave has {w.size}")
    o = np.argsort(w, kind="stable")
    ch = {k: v[o] for k, v in ch.items()}
    w = w[o]
    return w, ch, split_segments(w, overlap_cuts(gratings, ranges))


def stack_segments(parts: Sequence[tuple[str, np.ndarray, Mapping[str, np.ndarray]]]):
    """One ascending segment per grating: ``parts`` is ``[(label, wave, {channel: values})]``.

    Each part is sorted (stable) and loses its non-finite wavelengths; parts are stacked in
    order of their first wavelength (ties keep the input order) and empty parts are dropped.
    Returns ``(wave, channels, segments)``; channels missing from a part are NaN there.
    """
    keys: list[str] = []
    for _, _, ch in parts:
        keys += [k for k in ch if k not in keys]
    blocks = []
    for i, (label, wave, ch) in enumerate(parts):
        w = np.asarray(wave, float).ravel()
        vals = {k: (np.asarray(ch[k], float).ravel() if k in ch else np.full(w.size, np.nan)) for k in keys}
        for k, v in vals.items():
            if v.size != w.size:
                raise ValueError(f"part {label!r}: channel {k!r} has {v.size} values, wave has {w.size}")
        ok = np.isfinite(w)
        w = w[ok]
        o = np.argsort(w, kind="stable")
        if w.size:
            blocks.append((float(w[o][0]), i, str(label), w[o], {k: v[ok][o] for k, v in vals.items()}))
    blocks.sort(key=lambda b: (b[0], b[1]))
    segs, i0 = [], 0
    for _, _, label, w, _ in blocks:
        segs.append((i0, i0 + w.size, label))
        i0 += w.size
    if not blocks:
        return np.zeros(0), {k: np.zeros(0) for k in keys}, []
    return (np.concatenate([b[3] for b in blocks]),
            {k: np.concatenate([b[4][k] for b in blocks]) for k in keys}, segs)


def assign_gratings(wave, grids: Mapping[str, np.ndarray], tol: float = 6e-6, decimals: int | None = None,
                    values=None) -> np.ndarray:
    """Index into ``list(grids)`` of the grid each wavelength belongs to (-1 when none).

    Without ``decimals``: a point belongs to the grid holding the nearest value within ``tol``
    um; ties go to the first grid.

    With ``decimals`` (the legacy JSON rounding, ``round(w, decimals)``): the merged array is
    the multiset union of the rounded grids, so each rounded grid value is matched once. When
    two grids hold the same rounded value, ``values = (merged_values, {name: grid_values})``
    breaks the tie by the closest value (NaN matches NaN); otherwise grid order decides.
    """
    w = np.asarray(wave, float).ravel()
    out = np.full(w.size, -1, int)
    names = list(grids)
    if decimals is not None:
        from collections import defaultdict
        pool = []                                   # per grid: rounded value -> [grid values]
        for j, g in enumerate(names):
            ga = np.asarray(grids[g], float).ravel()
            gv = np.asarray(values[1].get(g, np.full(ga.size, np.nan)), float).ravel() if values else None
            d = defaultdict(list)
            for i in np.flatnonzero(np.isfinite(ga)):
                d[round(float(ga[i]), decimals)].append(float(gv[i]) if gv is not None and i < gv.size else np.nan)
            pool.append(d)
        mv = np.asarray(values[0], float).ravel() if values else None
        for i in np.flatnonzero(np.isfinite(w)):
            k = round(float(w[i]), decimals)
            cand = [j for j in range(len(names)) if pool[j].get(k)]
            if not cand:
                continue
            best, bi = cand[0], 0
            if len(cand) > 1 and mv is not None and i < mv.size:
                def cost(v, m=mv[i]):
                    return 0.0 if np.isnan(v) and np.isnan(m) else (abs(v - m) if np.isfinite(v - m) else np.inf)
                best, bi = min(((j, n) for j in cand for n in range(len(pool[j][k]))),
                               key=lambda jn: cost(pool[jn[0]][k][jn[1]]))
            pool[best][k].pop(bi)
            out[i] = best
        return out
    best_d = np.full(w.size, np.inf)
    for j, g in enumerate(grids.values()):
        g = np.asarray(g, float).ravel()
        g = np.sort(g[np.isfinite(g)])
        if not g.size or not w.size:
            continue
        i = np.clip(np.searchsorted(g, w), 1, max(g.size - 1, 1))
        lo, hi = g[np.clip(i - 1, 0, g.size - 1)], g[np.clip(i, 0, g.size - 1)]
        with np.errstate(invalid="ignore"):
            d = np.minimum(np.abs(w - lo), np.abs(w - hi))
            take = (d <= tol) & (d < best_d)
        out[take], best_d[take] = j, d[take]
    return out


def read_s2d(path) -> tuple[np.ndarray, np.ndarray]:
    """``(SCI data as float32, wavelength per column)`` of an s2d product; the column
    wavelength is the NaN-median of the WAVELENGTH extension over rows."""
    import warnings

    from astropy.io import fits
    with fits.open(path, memmap=False) as h:
        data = np.asarray(h["SCI"].data, np.float32)
        wl = np.asarray(h["WAVELENGTH"].data, float)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)       # all-NaN columns -> NaN
        wave = np.nanmedian(wl, axis=0) if wl.ndim == 2 else wl.ravel()
    if data.ndim != 2 or wave.size != data.shape[1]:
        raise ValueError(f"{path}: SCI {data.shape} and WAVELENGTH {wl.shape} do not match")
    return data, wave


def stitch_s2d(blocks: Sequence[tuple[str, np.ndarray, np.ndarray]],
               cuts: Sequence[tuple[str, float, float]] | None = None):
    """Stitch per-grating s2d blocks ``[(label, data[ny, nx], wave[nx])]`` into one Image2D.

    ``cuts`` (``(label, lo, hi)``, see :func:`overlap_cuts` / :func:`fill_cuts`) decides
    which grating supplies each wavelength range; by default each block's finite data range
    goes into :func:`fill_cuts` (PRISM fills the gaps). Per cut, the columns with a finite
    wavelength in [lo, hi) are kept; shorter blocks are centred in NaN rows to the common
    height; columns whose wavelength does not exceed the previous kept column are dropped,
    so the stitched wavelength is strictly increasing. Returns ``None`` when nothing is left.
    A label that occurs in more than one block keeps its first block only (a RuntimeWarning
    names the dropped ones); pass distinct labels to stitch repeated exposures of a grating.
    """
    from portalkit.spec import Image2D
    good = []
    for label, data, wave in blocks:
        d = np.asarray(data, np.float32)
        w = np.asarray(wave, float).ravel()
        if d.ndim != 2 or d.shape[1] != w.size or not d.size:
            raise ValueError(f"block {label!r}: data {d.shape} and wave {w.shape} do not match")
        good.append((str(label), d, w))
    if not good:
        return None
    if cuts is None:
        ranges = {}
        for label, _, w in good:
            f = w[np.isfinite(w)]
            if f.size:
                lo, hi = float(f.min()), float(f.max())
                ranges[label] = (lo, np.nextafter(hi, np.inf))
        cuts = fill_cuts(ranges)
    ny = max(d.shape[0] for _, d, _ in good)
    by: dict = {}
    dup = []
    for label, d, w in good:
        if label in by:
            dup.append(label)
        by.setdefault(label, (d, w))
    if dup:
        import warnings
        warnings.warn(f"stitch_s2d: repeated labels {sorted(set(dup))}; only the first block of each is used",
                      RuntimeWarning, stacklevel=2)
    out_d, out_w, segs, last = [], [], [], -np.inf
    for label, lo, hi in sorted(cuts, key=lambda c: (c[1], c[2])):
        if label not in by:
            continue
        d, w = by[label]
        with np.errstate(invalid="ignore"):
            keep = np.isfinite(w) & (w >= lo) & (w < hi)
        idx = np.flatnonzero(keep)
        idx = idx[np.argsort(w[idx], kind="stable")]
        sel, prev = [], last
        for i in idx:
            if w[i] > prev:
                sel.append(i)
                prev = w[i]
        if not sel:
            continue
        pad = np.full((ny, len(sel)), np.nan, np.float32)
        top = (ny - d.shape[0]) // 2
        pad[top:top + d.shape[0]] = d[:, sel]
        i0 = sum(b.shape[1] for b in out_d)
        out_d.append(pad)
        out_w.append(w[sel])
        segs.append((i0, i0 + len(sel), label))
        last = prev
    if not out_d:
        return None
    return Image2D(data=np.concatenate(out_d, axis=1), wave=np.concatenate(out_w),
                   y=np.arange(ny, dtype=float) - (ny - 1) / 2.0, segments=segs)


def clean_flux(flux, err, max_flux: float = MAX_FLUX) -> np.ndarray:
    """Legacy ``clean_flux``: NaN where the error or the flux exceeds ``max_flux`` (Jy),
    where the flux is negative, and above mean + 10 sigma of the (per-grating) array."""
    f = np.array(flux, dtype=float).ravel()
    e = np.asarray(err, float).ravel()
    if e.size != f.size:
        raise ValueError(f"flux has {f.size} values, err has {e.size}")
    with np.errstate(invalid="ignore", over="ignore"):
        f[e > max_flux] = np.nan
        f[(f > max_flux) | (f < 0) | ~np.isfinite(f)] = np.nan
        if np.isfinite(f).any():
            f[f > np.nanmean(f) + 10 * np.nanstd(f)] = np.nan
    return f
