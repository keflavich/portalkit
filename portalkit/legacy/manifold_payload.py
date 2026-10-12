"""Convert a legacy manifold payload into a CatalogSpec and a ManifoldSpec.

The legacy manifold generators (``NIRSpec_6927_Manifold.py``, ``NIRSpec_2640_Manifold.py``,
``WD2_Portal_Manifold.py``, ``Sickle_MIRI_SEDPortal_Manifold.py``,
``JWST_Archive_Manifold.py``) embed ``const DATA = {stars, dims, groups, presets,
categories}`` in their page:

* ``stars``: one dict per source -- ``n`` (the id), ``obs``, ``ra``, ``dec`` and further
  per-star fields (``verdict``, ``h2osat``, ``thumb``, ``spec`` ...);
* ``dims``: ``{key, label, unit, group, log, note, center, scale, n, range:[p2, p98],
  v:[value|null per star], k:['v'|'lo'|'up'|null per star]}``. ``log`` dims already hold
  log10 values. A negative ``scale`` flips the axis. ``k`` marks measured values (``v``),
  lower limits (``lo``) and upper limits (``up``);
* ``groups``: ``[[key, label], ...]`` (or ``{key, label, dims}`` dicts);
* ``presets``: ``{id, code, name, x:{dim: weight}, y:{dim: weight}, color, note}`` with the
  weights applied to the standardized values ``(v - center) / scale``; ``x``/``y`` may be
  ``None`` for a frame the page computes itself;
* ``categories``: ``{key, label, colors:{level: css}}`` for the categorical star fields.

:func:`convert` maps every dim to a value column plus a sibling ``<key>__lim`` limit column
(``'u'`` upper, ``'l'`` lower, ``''`` measured or missing), so the bundle writer emits the
sparse limits table. Presets with one unit weight per axis become ``axes`` presets; any
other weighting becomes a ``frame`` preset whose ``F`` reproduces the legacy projection in
portalkit's standardization (the writer's median / 1.4826 MAD statistics).

Virtual colours: a dim whose values equal the difference of two other stored dims (for
example ``c_F162M_F210M = m_F162M - m_F210M``, the pairwise colour dims of the NIRSpec and
WD2 manifolds) becomes a :class:`~portalkit.spec.VirtualDim` ``"<a>-<b>"`` over the two
stored columns instead of a stored column (:func:`detect_virtual_colours`;
``virtual_colours=False`` keeps every dim stored). Presets, groups, density grids and the manifold dims then name the
virtual key, and the client folds it into the base weights (ARCHITECTURE section 7.3).
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable, Mapping

import numpy as np

from ..bundle.codec import col_stats
from ..constants import T0_MAX
from ..spec import (CatalogSpec, DensitySpec, Group, ManifoldSpec, Preset, VirtualDim,
                    catalog_from_rows, safe_key)

__all__ = ["ManifoldPayloadError", "LIMIT_SUFFIX", "MAX_QUANTUM", "limit_code", "convert",
           "preset_frame", "detect_virtual_colours"]

#: suffix of the sibling limit-code column of every dim
LIMIT_SUFFIX = "__lim"

_ID_FIELDS = ("n", "id", "name", "src", "source_id")
_RA_FIELDS = ("ra", "RA", "ra_deg")
_DEC_FIELDS = ("dec", "DEC", "Dec", "dec_deg")


class ManifoldPayloadError(ValueError):
    """The payload is missing required parts or is internally inconsistent."""


def limit_code(k: Any) -> str:
    """Legacy ``k`` code -> portalkit limit code: ``up``/``u``/``upper``/``<`` -> ``'u'``,
    ``lo``/``l``/``lower``/``>`` -> ``'l'``, anything else (``v``, None) -> ``''``."""
    if k is None:
        return ""
    s = str(k).strip().lower()
    if s in ("up", "u", "upper", "<", "ul"):
        return "u"
    if s in ("lo", "l", "lower", ">", "ll"):
        return "l"
    return ""


def _num(x: Any) -> float | None:
    if x is None or isinstance(x, bool):
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _pick(fields: Iterable[str], names: Iterable[str]) -> str | None:
    names = list(names)
    for f in fields:
        if f in names:
            return f
    return None


def _weights(w: Any) -> dict[str, float] | None:
    if w is None:
        return None
    if isinstance(w, Mapping):
        return {str(k): float(v) for k, v in w.items()}
    if isinstance(w, str):
        return {w: 1.0}
    raise ManifoldPayloadError(f"preset weights must be a dict or a dim key, got {type(w).__name__}")


def _relabel(w: Mapping[str, float], labels: Mapping[str, str]) -> dict[str, float]:
    """Preset weights keyed by the converted dim keys; weights on two legacy keys that name
    one dim (a duplicated colour) add up."""
    out: dict[str, float] = {}
    for k, v in w.items():
        lk = labels.get(k, k)
        out[lk] = out.get(lk, 0.0) + v
    return out


def preset_frame(x: Mapping[str, float], y: Mapping[str, float], dims: list[str],
                 factor: Mapping[str, float]) -> list[float]:
    """``F`` (length ``2*len(dims)``) for legacy weights ``x``/``y``; ``factor[d]`` converts a
    legacy standardized weight to portalkit's (``scale_pk / scale_legacy``, signed)."""
    D = len(dims)
    F = [0.0] * (2 * D)
    for r, w in enumerate((x, y)):
        for k, v in w.items():
            if k not in dims:
                raise ManifoldPayloadError(f"preset weight on unknown dim {k!r}")
            F[r * D + dims.index(k)] = round(float(v) * float(factor.get(k, 1.0)), 6)
    return F


def _quantum(v: np.ndarray) -> float:
    """Rounding step of the finite values of ``v`` (``10**-k`` for the smallest ``k`` <= 6
    that makes every value an integer multiple; 0 when none does)."""
    f = v[np.isfinite(v)]
    for k in range(7):
        q = 10.0 ** -k
        if np.all(np.abs(f / q - np.round(f / q)) < 1e-6):
            return q
    return 0.0


#: largest rounding step (see :func:`_quantum`) of a dim that may take part in a virtual
#: colour; coarser dims (integers: step 1) are never matched
MAX_QUANTUM = 0.01


def _colour_like(key: str, label: str) -> bool:
    return key.startswith("c_") or "-" in key or "-" in label or "\u2212" in label


def detect_virtual_colours(dims: Mapping[str, Mapping[str, Any]], order: list[str], *,
                    min_rows: int = 3) -> dict[str, tuple[str, str]]:
    """Detect dims that are the difference of two other dims; returns ``{dim: (a, b)}`` with
    ``dim == a - b`` on every star.

    ``dims[k]`` holds ``v`` (float array, NaN for missing), ``lim`` (limit codes), ``unit``,
    ``log`` and ``label``. A candidate has no limit codes, is not a log dim, and has at least
    ``min_rows`` finite values; ``a`` and ``b`` are stored (not themselves virtual), are not
    log dims, share one unit (the candidate's unit is that unit or empty), and the candidate
    is missing exactly where ``a`` or ``b`` is. The match tolerance is half the sum of the
    three dims' rounding steps (plus 1e-6), so legacy values rounded to 3 decimals match
    within 1.5e-3.

    Only colour-like dims (``c_*`` keys, a ``-`` in the key, a ``-`` or U+2212 in the label)
    are candidates, so a magnitude (or any other stored quantity) is never re-expressed as a
    magnitude minus a colour. Dims whose rounding step exceeds :data:`MAX_QUANTUM` (integer
    counts, flags, bins) are neither candidates nor bases: their match tolerance would be of
    the order of one unit and would accept differences that are off by one. Two candidates
    may map to the same ``(a, b)`` pair (a colour stored under two keys); :func:`convert`
    serves them as one virtual dim."""
    keys = [k for k in order if k in dims]
    q = {k: _quantum(np.asarray(dims[k]["v"], dtype=np.float64)) for k in keys}
    ok = {k: (not dims[k].get("log")) and q[k] <= MAX_QUANTUM for k in keys}
    cand = [k for k in keys if ok[k] and _colour_like(k, str(dims[k].get("label") or ""))
            and not any(dims[k].get("lim") or [])
            and int(np.isfinite(dims[k]["v"]).sum()) >= min_rows]
    out: dict[str, tuple[str, str]] = {}
    used_as_base: set[str] = set()
    for d in cand:
        if d in used_as_base:
            continue
        base = [k for k in keys if ok[k] and k != d and k not in out]
        if len(base) < 2:
            continue
        vd = np.asarray(dims[d]["v"], dtype=np.float64)
        fd = np.isfinite(vd)
        rows = np.flatnonzero(fd)
        s = rows[:8]
        M = np.array([np.asarray(dims[k]["v"], dtype=np.float64) for k in base])
        T = M[:, s] - vd[s]                          # (a, rows): a - d, must equal b
        tol_s = 0.5 * 1.01 * (q[d] + np.array([q[k] for k in base])) + 1e-6
        with np.errstate(invalid="ignore"):
            close = np.abs(M[None, :, s] - T[:, None, :]) <= (tol_s[:, None, None]
                                                               + tol_s[None, :, None])
        ia, ib = np.nonzero(np.all(close, axis=2))
        unit_d = str(dims[d].get("unit") or "")
        for i, j in zip(ia.tolist(), ib.tolist()):
            a, b = base[i], base[j]
            if a == b:
                continue
            ua, ub = str(dims[a].get("unit") or ""), str(dims[b].get("unit") or "")
            if ua != ub or unit_d not in ("", ua):
                continue
            fa, fb = np.isfinite(M[i]), np.isfinite(M[j])
            if not np.array_equal(fa & fb, fd):
                continue
            tol = 0.5 * 1.01 * (q[d] + q[a] + q[b]) + 1e-6
            if np.all(np.abs((M[i] - M[j])[fd] - vd[fd]) <= tol):
                out[d] = (a, b)
                used_as_base.update((a, b))
                break
    return out


def convert(payload: Mapping, *, key: str = "src", title: str = "",
            id_field: str | None = None, default_frame: str | None = None,
            roles: Mapping[str, str] | None = None, columns: Mapping[str, dict] | None = None,
            density_min_n: int | None = None,
            virtual_colours: bool = True) -> tuple[CatalogSpec, ManifoldSpec]:
    """Convert a legacy manifold payload; returns ``(catalog_spec, manifold_spec)``.

    ``catalog_spec.manifold`` is set to the returned ManifoldSpec. ``id_field`` overrides the
    star id field (default: the first of ``n``, ``id``, ``name``, ``src``, ``source_id``
    present). ``roles``/``columns`` are passed through to
    :func:`~portalkit.spec.catalog_from_rows` for the star fields. ``default_frame`` names
    the preset used as the manifold's default frame (default: ``pca`` when present, else the
    first frame preset, else ``"pca"``). Axes presets of catalogs with more than
    ``density_min_n`` (default ``T0_MAX``) rows get a density grid over the dims' legacy
    ``range``. ``virtual_colours`` (default True) turns dims that are the difference of two
    other dims into virtual dims (module docstring).
    """
    if not isinstance(payload, Mapping):
        raise ManifoldPayloadError(f"payload must be an object, got {type(payload).__name__}")
    stars = payload.get("stars")
    dims_in = payload.get("dims")
    if not isinstance(stars, list) or not stars:
        raise ManifoldPayloadError("payload has no 'stars' list")
    if not isinstance(dims_in, list):
        raise ManifoldPayloadError("payload has no 'dims' list")
    n = len(stars)
    star_fields: list[str] = []
    for s in stars:
        if not isinstance(s, Mapping):
            raise ManifoldPayloadError("every star must be an object")
        for f in s:
            if f not in star_fields:
                star_fields.append(f)
    idf = id_field or _pick(_ID_FIELDS, star_fields)
    if idf is None:
        raise ManifoldPayloadError(f"no id field among {_ID_FIELDS} in the star records")
    raf = _pick(_RA_FIELDS, star_fields)
    decf = _pick(_DEC_FIELDS, star_fields)
    if (raf is None) != (decf is None):
        raf = decf = None

    rows = [{f: s.get(f) for f in star_fields} for s in stars]
    for r in rows:
        r[idf] = None if r[idf] is None else str(r[idf])
    taken = set(star_fields)

    col_kw: dict[str, dict] = {k: dict(v) for k, v in (columns or {}).items()}
    role_map: dict[str, str] = dict(roles or {})
    dim_keys: list[str] = []
    legacy_scale: dict[str, float] = {}
    pk_scale: dict[str, float] = {}
    ranges: dict[str, tuple[float, float]] = {}
    labels: dict[str, str] = {}
    dim_info: dict[str, dict[str, Any]] = {}
    for d in dims_in:
        if not isinstance(d, Mapping) or "key" not in d:
            raise ManifoldPayloadError("every dim must be an object with a 'key'")
        dk = str(d["key"])
        if dk in taken:
            # a dim named like a star field: keep the star field, suffix the dim
            dk2 = safe_key(dk + "_dim", set(taken))
        else:
            dk2 = dk if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", dk) else safe_key(dk, set(taken))
        taken.add(dk2)
        v = d.get("v")
        if not isinstance(v, list) or len(v) != n:
            raise ManifoldPayloadError(f"dim {dk!r}: 'v' must hold one value per star ({n})")
        k = d.get("k") or [None] * n
        if not isinstance(k, list) or len(k) != n:
            raise ManifoldPayloadError(f"dim {dk!r}: 'k' must hold one code per star ({n})")
        lim_key = dk2 + LIMIT_SUFFIX
        taken.add(lim_key)
        vals = [_num(x) for x in v]
        codes = [limit_code(c) if vals[i] is not None else "" for i, c in enumerate(k)]
        for i, r in enumerate(rows):
            r[dk2] = vals[i]
            r[lim_key] = codes[i]
        dim_keys.append(dk2)
        label = str(d.get("label") or dk)
        is_log = bool(d.get("log"))
        dim_info[dk2] = {"v": np.array([np.nan if x is None else x for x in vals], dtype=np.float64),
                         "lim": [c for c in codes if c], "unit": str(d.get("unit") or ""),
                         "log": is_log, "label": label, "legacy": dk}
        note = str(d.get("note") or "")
        if is_log and "log" not in label.lower():
            label = f"log10 {label}"
        if is_log:
            note = (note + " " if note else "") + "(stored as log10 values)"
        labels[dk] = dk2
        kw = col_kw.setdefault(dk2, {})
        kw.setdefault("label", label)
        if d.get("unit"):
            kw.setdefault("unit", str(d["unit"]))
        if d.get("group") is not None:
            kw.setdefault("group", str(d["group"]))
        if note:
            kw.setdefault("note", note)
        kw.setdefault("limit", lim_key)
        kw.setdefault("log", False)     # IMPLEMENTATION_NOTES 64: log dims are log10 base columns
        sc = _num(d.get("scale"))
        if sc is not None and sc < 0:
            kw.setdefault("invert", True)
        role_map.setdefault(dk2, "value")
        role_map[lim_key] = "limit"
        if sc is not None and sc != 0:
            legacy_scale[dk2] = sc
        fin = np.array([x for x in vals if x is not None], dtype=np.float64)
        pk_scale[dk2] = col_stats(fin)["scale"] if fin.size else 1.0
        rng = d.get("range")
        if isinstance(rng, (list, tuple)) and len(rng) == 2 and all(_num(x) is not None for x in rng):
            a, b = float(rng[0]), float(rng[1])
            if b > a:
                ranges[dk2] = (a, b)

    # categories: role + levels + colours
    for c in payload.get("categories") or []:
        if not isinstance(c, Mapping) or c.get("key") not in star_fields:
            continue
        ck = str(c["key"])
        if ck in (idf, raf, decf):
            continue
        role_map[ck] = "category"
        kw = col_kw.setdefault(ck, {})
        if c.get("label"):
            kw.setdefault("label", str(c["label"]))
        colors = c.get("colors")
        present = []
        for r in rows:
            val = r.get(ck)
            if val is not None and str(val) not in present:
                present.append(str(val))
        if isinstance(colors, Mapping):
            levels = [str(x) for x in colors if str(x) in present] + \
                     [x for x in present if x not in {str(y) for y in colors}]
            kw.setdefault("colors", {str(a): str(b) for a, b in colors.items() if str(a) in present})
        else:
            levels = present
        kw.setdefault("levels", levels)
        for r in rows:
            if r.get(ck) is not None:
                r[ck] = str(r[ck])

    # virtual colours: drop the stored column (and its limit sibling), keep a VirtualDim
    vdims: list[VirtualDim] = []
    found = detect_virtual_colours(dim_info, dim_keys) if virtual_colours else {}
    for dk2, (a, b) in found.items():
        vkey = f"{a}-{b}"
        dup = any(vd.key == vkey for vd in vdims)
        kw = col_kw.pop(dk2, {})
        role_map.pop(dk2, None)
        role_map.pop(dk2 + LIMIT_SUFFIX, None)
        col_kw.pop(dk2 + LIMIT_SUFFIX, None)
        for r in rows:
            r.pop(dk2, None)
            r.pop(dk2 + LIMIT_SUFFIX, None)
        labels[dim_info[dk2]["legacy"]] = vkey
        if dup:
            # the same colour stored under a second key: its legacy key names the virtual
            # dim already emitted, and the dim list keeps one entry
            dim_keys.remove(dk2)
            for m in (legacy_scale, pk_scale, ranges):
                m.pop(dk2, None)
            continue
        vdims.append(VirtualDim(key=vkey, label=kw.get("label"), unit=str(kw.get("unit") or ""),
                                group=kw.get("group")))
        dim_keys[dim_keys.index(dk2)] = vkey
        for m in (legacy_scale, pk_scale, ranges):
            if dk2 in m:
                m[vkey] = m.pop(dk2)
        if kw.get("group") is not None:
            col_kw.setdefault(vkey, {})["group"] = kw["group"]     # for the group dims below
    cat = catalog_from_rows(rows, idf, raf, decf, roles=role_map, key=key, title=title,
                            columns={k: v for k, v in col_kw.items() if k not in
                                     {vd.key for vd in vdims}})
    cat.virtual = list(cat.virtual) + vdims
    cols = {c.key for c in cat.columns} | {vd.key for vd in vdims}

    # groups
    groups: list[Group] = []
    gdims: dict[str, list[str]] = {}
    for dk2 in dim_keys:
        g = col_kw.get(dk2, {}).get("group")
        if g is not None:
            gdims.setdefault(g, []).append(dk2)
    for g in payload.get("groups") or []:
        if isinstance(g, Mapping):
            gk, gl = str(g.get("key")), str(g.get("label") or g.get("key"))
            gd = [labels.get(x, x) for x in (g.get("dims") or gdims.get(gk, []))]
        elif isinstance(g, (list, tuple)) and len(g) >= 2:
            gk, gl = str(g[0]), str(g[1])
            gd = gdims.get(gk, [])
        else:
            continue
        gd = list(dict.fromkeys(x for x in gd if x in cols))
        if gd:
            groups.append(Group(key=gk, label=gl, dims=gd))
    cat.groups = groups

    # presets
    factor = {dk: (pk_scale[dk] / legacy_scale[dk]) if dk in legacy_scale else 1.0
              for dk in dim_keys}
    presets: list[Preset] = []
    frames: dict[str, list[float]] = {}
    density: list[DensitySpec] = []
    nmin = T0_MAX if density_min_n is None else density_min_n
    for p in payload.get("presets") or []:
        if not isinstance(p, Mapping):
            continue
        pid = str(p.get("id") or p.get("key") or f"p{len(presets)}")
        label = str(p.get("name") or p.get("label") or p.get("code") or pid)
        x, y = _weights(p.get("x")), _weights(p.get("y"))
        if x is None or y is None:
            continue        # computed in the page (e.g. a PCA with no stored weights)
        x, y = _relabel(x, labels), _relabel(y, labels)
        if not (set(x) | set(y)) <= set(dim_keys):
            continue        # the legacy generator drops these too
        data: dict[str, Any] = {}
        if p.get("note"):
            data["note"] = str(p["note"])
        if p.get("code"):
            data["code"] = str(p["code"])
        color = p.get("color")
        if color is not None:
            ck = labels.get(str(color), str(color))
            if ck in cols:
                data["color"] = ck
        unit_axes = (len(x) == 1 and len(y) == 1 and abs(next(iter(x.values())) - 1.0) < 1e-12
                     and abs(next(iter(y.values())) - 1.0) < 1e-12)
        if unit_axes:
            xk, yk = next(iter(x)), next(iter(y))
            data.update(x=xk, y=yk)
            presets.append(Preset(key=pid, label=label, kind="axes", data=data))
            if n > nmin and xk in ranges and yk in ranges:
                density.append(DensitySpec(key=f"{pid}_density", x=xk, y=yk,
                                           xr=ranges[xk], yr=ranges[yk]))
        else:
            F = preset_frame(x, y, dim_keys, factor)
            frames[pid] = F
            data.update(dims=list(dim_keys), F=F)
            presets.append(Preset(key=pid, label=label, kind="frame", data=data))
    cat.presets = presets
    cat.density = density

    if default_frame is None:
        default_frame = "pca" if "pca" in frames else (next(iter(frames)) if frames else "pca")
    mf = ManifoldSpec(dims=list(dim_keys), frames=frames, default_frame=default_frame)
    cat.manifold = mf
    return cat, mf
