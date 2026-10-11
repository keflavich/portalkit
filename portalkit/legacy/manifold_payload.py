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
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable, Mapping

import numpy as np

from ..bundle.codec import col_stats
from ..constants import T0_MAX
from ..spec import (CatalogSpec, DensitySpec, Group, ManifoldSpec, Preset, catalog_from_rows,
                    safe_key)

__all__ = ["ManifoldPayloadError", "LIMIT_SUFFIX", "limit_code", "convert", "preset_frame"]

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


def convert(payload: Mapping, *, key: str = "src", title: str = "",
            id_field: str | None = None, default_frame: str | None = None,
            roles: Mapping[str, str] | None = None, columns: Mapping[str, dict] | None = None,
            density_min_n: int | None = None) -> tuple[CatalogSpec, ManifoldSpec]:
    """Convert a legacy manifold payload; returns ``(catalog_spec, manifold_spec)``.

    ``catalog_spec.manifold`` is set to the returned ManifoldSpec. ``id_field`` overrides the
    star id field (default: the first of ``n``, ``id``, ``name``, ``src``, ``source_id``
    present). ``roles``/``columns`` are passed through to
    :func:`~portalkit.spec.catalog_from_rows` for the star fields. ``default_frame`` names
    the preset used as the manifold's default frame (default: ``pca`` when present, else the
    first frame preset, else ``"pca"``). Axes presets of catalogs with more than
    ``density_min_n`` (default ``T0_MAX``) rows get a density grid over the dims' legacy
    ``range``.
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

    cat = catalog_from_rows(rows, idf, raf, decf, roles=role_map, key=key, title=title,
                            columns=col_kw)
    cols = {c.key for c in cat.columns}

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
        gd = [x for x in gd if x in cols]
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
        x = {labels.get(k, k): v for k, v in x.items()}
        y = {labels.get(k, k): v for k, v in y.items()}
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
