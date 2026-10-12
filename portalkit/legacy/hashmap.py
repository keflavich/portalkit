"""Python mirror of ``web/js/core/legacyhash.js`` (legacy URL grammars, section 6.4).

The browser does the mapping at page load; this module gives the build and the tests the
same mapping: ``extract-legacy`` reports what a legacy deep link turns into, and
``tests/fixtures/legacy_snippets/hash_vectors.json`` is checked against both
implementations. Encoded values (``f=``, ``sel=r:``, ``<pane>.frame=b64:``) are produced the
same way; ``sel=r:`` payloads use zlib raw deflate here and the hand-written deflate of
``selection.js`` in the browser, so they agree after inflating, byte for byte only in the
decoded JSON.

Shared contracts of wave E (ARCHITECTURE sections 4.1 and 6.4), mirrored here:

* (B) legacy dim alias: ``page.legacy.dim_alias`` (or ``loc['dimAlias']`` /
  ``loc['dim_alias']``) maps legacy dim keys onto unified ones; kv-view applies it to its
  ``x`` / ``y`` / ``z`` values and to the column part of ``c=<by>[~cmap~lo~hi]``, and
  spherex-colours to its filter and colour names (gc-query indexes dims by position and its
  ``y`` band name is not aliased, as in the browser).
* (C) the ``spherex-colours`` grammar (query string of the legacy SPHEREx colours.html).
* (D) the canonical ``speed`` key: the wavelength and kv-view grammars map a legacy
  ``?speed=`` query parameter (consumed, also under a canonical hash) and a legacy ``speed=``
  hash value onto it.

``map_legacy`` returns ``{"hash", "consumedSearch"}``; the browser's ``mapLegacy`` also
returns ``fromHash``, which only ``boot.js`` uses.
"""

from __future__ import annotations

import base64
import json
import math
import re
import struct
import zlib
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import unquote, unquote_plus

__all__ = ["CANONICAL_KEYS", "GRAMMAR_NAMES", "GRAMMARS", "SEL_RADIUS_ARCSEC",
           "DEFAULT_FOV_DEG", "map_legacy", "is_canonical_hash", "encode_json", "decode_json",
           "encode_frame", "decode_frame", "encode_recipe", "decode_recipe", "b64url_encode",
           "b64url_decode", "legacy_context", "layout_info", "js_number", "dim_alias"]

CANONICAL_KEYS = ("id", "sel", "f", "c", "av", "rv", "dm", "speed", "tpl", "feat", "spec",
                  "wl", "sky", "base", "ovl", "rung", "beat", "role", "fac", "lb", "view")
_CANON = set(CANONICAL_KEYS)
_PANE_KEY = re.compile(r"^(.+)\.(x|y|frame|xr|yr|raster|ovl)$")
SEL_RADIUS_ARCSEC = 0.5
DEFAULT_FOV_DEG = 0.25
_OPEN_LO = -1e300
_OPEN_HI = 1e300


# ---------------------------------------------------------------------------
# codecs
# ---------------------------------------------------------------------------

def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(bytes(data)).decode("ascii").rstrip("=")


def b64url_decode(s: str) -> bytes:
    s = str(s).replace("+", "-").replace("/", "_").rstrip("=")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def js_number(x: float) -> str:
    """``String(x)`` / ``JSON.stringify(x)`` of a finite JS number.

    Both languages print the shortest round-trip digits; JS drops ``.0``, uses fixed
    notation for ``1e-7 <= |x| < 1e21`` and writes exponents as ``e-7`` / ``e+21``."""
    x = float(x)
    if not math.isfinite(x):
        raise ValueError(f"non-finite number {x!r}")
    if x == 0:
        return "0"
    sign = "-" if x < 0 else ""
    mant, _, exp = repr(abs(x)).partition("e")
    ip, _, fp = mant.partition(".")
    digits = (ip + fp).lstrip("0")
    # decimal exponent n: value = 0.d1d2... * 10**n
    n = len(ip) + int(exp or 0) if ip != "0" else int(exp or 0) - (len(fp) - len(fp.lstrip("0")))
    digits = digits.rstrip("0") or "0"
    k = len(digits)
    if k <= n <= 21:
        return sign + digits + "0" * (n - k)
    if 0 < n <= 21:
        return sign + digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return sign + "0." + "0" * (-n) + digits
    e = n - 1
    es = f"e+{e}" if e >= 0 else f"e{e}"
    return sign + digits[0] + ("." + digits[1:] if k > 1 else "") + es


def _js_json(v: Any) -> str:
    """``JSON.stringify`` (compact) of plain JSON data, numbers formatted as JS does."""
    if isinstance(v, bool) or v is None:
        return json.dumps(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return js_number(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, Mapping):
        return "{" + ",".join(json.dumps(str(k), ensure_ascii=False) + ":" + _js_json(x)
                              for k, x in v.items()) + "}"
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(_js_json(x) for x in v) + "]"
    raise TypeError(f"cannot encode {type(v).__name__} as JSON")


def encode_json(v: Any) -> str:
    """JSON -> base64url(UTF-8 JSON) (``core/hash.js`` ``encodeJSONParam``)."""
    return b64url_encode(_js_json(v).encode("utf-8"))


def decode_json(s: str) -> Any:
    """base64url JSON, or raw JSON text starting with ``{`` / ``[``."""
    s = str(s)
    if s[:1] in "{[":
        return json.loads(s)
    return json.loads(b64url_decode(s).decode("utf-8"))


def encode_frame(F: Iterable[float]) -> str:
    """``b64:`` + base64url(float32 little-endian)."""
    vals = list(F)
    return "b64:" + b64url_encode(struct.pack(f"<{len(vals)}f", *vals))


def decode_frame(s: str) -> list[float]:
    raw = b64url_decode(s[4:] if s.startswith("b64:") else s)
    if len(raw) % 4:
        raise ValueError("frame bytes not a multiple of 4")
    return list(struct.unpack(f"<{len(raw) // 4}f", raw))


def encode_recipe(ops: list[dict]) -> str:
    """``r:`` + base64url(deflate-raw(JSON RecipeOp[]))."""
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return "r:" + b64url_encode(c.compress(_js_json(ops).encode("utf-8")) + c.flush())


def decode_recipe(s: str) -> list[dict]:
    raw = b64url_decode(s[2:] if s.startswith("r:") else s)
    return json.loads(zlib.decompress(raw, -15).decode("utf-8"))


def _fmt(v: float) -> str:
    """``core/hash.js`` ``num()``: 1e-6 absolute precision above 1e-3, else 7 digits."""
    if abs(v) >= 1e-3:
        r = math.floor(v * 1e6 + 0.5) / 1e6
    else:
        r = float(f"{v:.7g}")
    if r == 0:
        return "0"
    if r == int(r) and abs(r) < 1e21:
        return str(int(r))
    return js_number(r)


# ---------------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------------

def _decode(s: str, plus: bool = False) -> str:
    """Percent-decode; ``plus`` also turns '+' into a space (query strings and
    URLSearchParams-written hashes only: legacy pages set raw names with '+' as the hash)."""
    return unquote_plus(str(s)) if plus else unquote(str(s))


def _hash_text(loc: Mapping) -> str:
    h = str(loc.get("hash") or "")
    return h[1:] if h.startswith("#") else h


def _parse_kv(text: str, plus: bool = False) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in str(text).split("&"):
        if not part:
            continue
        if "=" not in part:
            out[_decode(part, plus)] = ""
        else:
            k, v = part.split("=", 1)
            out[_decode(k, plus)] = _decode(v, plus)
    return out


def _parse_search(loc: Mapping) -> dict[str, str]:
    s = str(loc.get("search") or "")
    return _parse_kv(s[1:] if s.startswith("?") else s, plus=True)


def _num(s: Any) -> float | None:
    if s is None or str(s).strip() == "":
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def _int(s: Any) -> int | None:
    v = _num(s)
    return int(v) if v is not None and v == int(v) else None


def is_canonical_hash(text: str) -> bool:
    if not text or "=" not in text:
        return False
    kv = _parse_kv(text)
    if not kv:
        return False
    if any(k not in _CANON and not _PANE_KEY.match(k) for k in kv):
        return False
    if kv.get("view") and ":" not in kv["view"]:
        return False
    return True


def _passthrough(kv: Mapping[str, str]) -> dict[str, str]:
    out = {}
    for k, v in kv.items():
        if k == "view" and v and ":" not in v:
            continue
        if k in _CANON or _PANE_KEY.match(k):
            out[k] = v
    return out


def _panes(node: Any):
    if not isinstance(node, Mapping):
        return
    if isinstance(node.get("row"), list):
        for c in node["row"]:
            yield from _panes(c)
    elif isinstance(node.get("col"), list):
        for c in node["col"]:
            yield from _panes(c)
    elif isinstance(node.get("pane"), str):
        yield node


def layout_info(loc: Mapping) -> dict | None:
    """``{ids: set of pane ids, manifold: first manifold pane id | None}`` of ``loc['page']``."""
    page = loc.get("page")
    if not isinstance(page, Mapping) or not page.get("layout"):
        return None
    ids: set[str] = set()
    manifold = None
    for p in _panes(page["layout"]):
        pid = p.get("id") or p["pane"]
        ids.add(pid)
        if p["pane"] == "manifold" and manifold is None:
            manifold = pid
    return {"ids": ids, "manifold": manifold}


def legacy_context(loc: Mapping) -> dict:
    """``{dims: [{key, scale?}] | None, pane, ds}`` from ``loc`` and ``loc['page']``."""
    ctx: dict[str, Any] = {"dims": None, "pane": loc.get("pane"), "ds": loc.get("ds")}
    if isinstance(loc.get("dims"), list):
        ctx["dims"] = [{"key": d} if isinstance(d, str) else {"key": str(d["key"]), "scale": _num(d.get("scale"))}
                       for d in loc["dims"]]
    page = loc.get("page")
    if isinstance(page, Mapping) and (ctx["dims"] is None or not ctx["pane"] or not ctx["ds"]):
        mani = next((p for p in _panes(page.get("layout")) if p["pane"] == "manifold"), None)
        if mani is not None:
            ctx["pane"] = ctx["pane"] or mani.get("id") or "manifold"
            ctx["ds"] = ctx["ds"] or mani.get("dataset")
            od = (mani.get("options") or {}).get("dims")
            if ctx["dims"] is None and isinstance(od, list) and all(isinstance(x, str) for x in od):
                ctx["dims"] = [{"key": k} for k in od]
        if not ctx["ds"] and isinstance(page.get("datasets"), Mapping) and page["datasets"]:
            ctx["ds"] = next(iter(page["datasets"]))
    ctx["pane"] = ctx["pane"] or "manifold"
    return ctx


def dim_alias(loc: Mapping) -> dict[str, str]:
    """The legacy -> unified dim key map (``page.legacy.dim_alias``, contract (B)) from
    ``loc['dimAlias']`` (the browser's name) or ``loc['dim_alias']``, else the page document
    ``loc['page']``."""
    al = loc.get("dimAlias")
    if not isinstance(al, Mapping):
        al = loc.get("dim_alias")
    if not isinstance(al, Mapping):
        page = loc.get("page")
        pg = page.get("page") if isinstance(page, Mapping) else None
        lg = pg.get("legacy") if isinstance(pg, Mapping) else None
        al = lg.get("dim_alias") if isinstance(lg, Mapping) else None
    if not isinstance(al, Mapping):
        return {}
    return {str(k): str(v) for k, v in al.items() if isinstance(v, str)}


def _alias_dim(alias: Mapping[str, str], key: str) -> str:
    return alias.get(key, key)


def _scatter_panes(loc: Mapping) -> list[dict]:
    """Scatter panes of the page layout in layout order: ``[{id, options}]``."""
    page = loc.get("page")
    if not isinstance(page, Mapping) or not page.get("layout"):
        return []
    return [{"id": p.get("id") or p["pane"], "options": p.get("options") or {}}
            for p in _panes(page["layout"]) if p["pane"] == "scatter"]


def _dim_at(ctx: Mapping, i: int | None) -> dict | None:
    dims = ctx.get("dims")
    if not dims or i is None or i < 0 or i >= len(dims):
        return None
    return dims[i]


def _w(d: Mapping) -> float:
    s = d.get("scale")
    return s if isinstance(s, (int, float)) and math.isfinite(s) and s > 0 else 1.0


# ---------------------------------------------------------------------------
# grammars: each returns (hash, consumed) or None
# ---------------------------------------------------------------------------

# (hash, consumed) or (hash, consumed, search_hash): search_hash holds keys mapped from the
# query string by a 'both' rule, merged whether or not the hash was mapped
Result = tuple


def _bare_name(loc: Mapping) -> Result | None:
    h = _hash_text(loc)
    if not h or "=" in h or "&" in h:
        return None
    i = _decode(h).strip()
    return ({"id": i}, []) if i else None


_KV_ID_KEYS = ("name", "src", "source", "id", "star", "obj", "target")


def _speed_text(v: Any) -> str | None:
    """A positive speed multiplier as hash text, or None."""
    sp = _num(v)
    return _fmt(sp) if sp is not None and sp > 0 else None


def _search_speed(loc: Mapping) -> tuple[dict, list]:
    """Legacy ``?speed=`` -> canonical ``speed`` (contract (D)); an unreadable value is
    consumed and dropped."""
    q = _parse_search(loc)
    if "speed" not in q:
        return {}, []
    sp = _speed_text(q["speed"])
    return ({"speed": sp} if sp is not None else {}), ["speed"]


def _kv_view(loc: Mapping) -> Result | None:
    # the query-string part (legacy ?speed=) applies also under a canonical hash
    sh, consumed = _search_speed(loc)
    res = _kv_view_hash(loc)
    if res is None and not consumed:
        return None
    return (res if res is not None else {}), consumed, sh


def _kv_view_hash(loc: Mapping) -> dict | None:
    h = _hash_text(loc)
    if not h or "=" not in h or is_canonical_hash(h):
        return None
    kv = _parse_kv(h, plus=True)
    out: dict[str, str] = {}
    for k in _KV_ID_KEYS:
        if kv.get(k):
            out["id"] = kv[k]
            break
    alias = dim_alias(loc)
    for k in ("x", "y", "z"):
        if kv.get(k):
            kv[k] = _alias_dim(alias, kv[k])
    if kv.get("c"):
        # colour-by: <by>[~cmap~lo~hi]; only the column part is a dim name
        by, sep, rest = kv["c"].partition("~")
        c = _alias_dim(alias, by) + sep + rest
        if c != kv["c"]:
            out["c"] = c
    if "speed" in kv:
        sp = _speed_text(kv.pop("speed"))
        if sp is not None:
            out["speed"] = sp
    info = layout_info(loc)
    v = kv.get("view")
    if v:
        if ":" in v:
            out["view"] = v
        elif info is not None and info["manifold"] is not None and v not in info["ids"]:
            out[f"{info['manifold']}.frame"] = v
        else:
            out["view"] = f"{v}:full"
    elif kv.get("x") and kv.get("y"):
        ctx = legacy_context(loc)
        if ctx["dims"]:
            keys = [d["key"] for d in ctx["dims"]]
            kx, ky = kv["x"], kv["y"]
            if kx in keys and ky in keys:
                D = len(keys)
                ix, iy = keys.index(kx), keys.index(ky)
                F = [0.0] * (2 * D)
                F[ix] = _w(ctx["dims"][ix])
                F[D + iy] = _w(ctx["dims"][iy])
                out[f"{ctx['pane']}.frame"] = encode_frame(F)
    if not out:
        return None
    return {**_passthrough(kv), **out}


_WL_COMPACT = re.compile(r"^[^,]+(,[-+0-9.eE]+){2,3}$")


def _wavelength(loc: Mapping) -> Result | None:
    h = _hash_text(loc)
    kv: dict[str, str] = {}
    if h and "=" in h:
        kv = {} if is_canonical_hash(h) else _parse_kv(h)
    elif h and _WL_COMPACT.match(h):
        p = h.split(",")
        kv = {"rung": _decode(p[0]), "ra": p[1], "dec": p[2]}
        if len(p) > 3:
            kv["fov"] = p[3]
    out: dict[str, str] = {}
    if kv.get("rung"):
        out["rung"] = kv["rung"]
    if "speed" in kv:
        # legacy speed= in the hash: the tour / playback speed (contract (D))
        sp = _speed_text(kv.pop("speed"))
        if sp is not None:
            out["speed"] = sp
    ra, dec = _num(kv.get("ra")), _num(kv.get("dec"))
    if ra is not None and dec is not None:
        fov = _num(kv.get("fov"))
        out["sky"] = f"{_fmt(ra)},{_fmt(dec)},{_fmt(fov if fov is not None and fov > 0 else DEFAULT_FOV_DEG)}"
    sh, consumed = _search_speed(loc)
    if not out and not consumed:
        return None
    return ({**_passthrough(kv), **out} if out else {}), consumed, sh


def _beat(loc: Mapping) -> Result | None:
    h = _hash_text(loc)
    if not h or "=" not in h:
        return None
    kv = _parse_kv(h)
    if not kv.get("beat") or is_canonical_hash(h):
        return None
    return {**_passthrough(kv), "beat": kv["beat"]}, []


def _gallery_index(loc: Mapping) -> Result | None:
    h = _hash_text(loc)
    if not h:
        return None
    if re.fullmatch(r"\d+", h):
        return {"lb": str(int(h))}, []
    if "=" not in h:
        return None
    kv = _parse_kv(h)
    v = kv.get("img")
    if not v:
        return None
    out = _passthrough(kv)
    if re.fullmatch(r"\d+", v):
        out["lb"] = str(int(v))
    elif re.fullmatch(r"[^,]+,\d+", v):
        out["lb"] = v
    else:
        out["id"] = v
    return out, []


_W51 = re.compile(r"^W51-\d+$", re.I)


def _w51_rowid(loc: Mapping) -> Result | None:
    h = _decode(_hash_text(loc)).strip()
    return ({"id": h.upper()}, []) if _W51.match(h) else None


def _specviewer(loc: Mapping) -> Result | None:
    return _bare_name(loc)


_GC_KEYS = ("a", "b", "m", "p", "x1", "x2", "y1", "y2", "cb", "cd", "cm", "cl", "ch", "g", "rv",
            "fa", "sel", "y")


def _gc_query(loc: Mapping) -> Result | None:
    q = _parse_search(loc)
    if not any(k in q for k in _GC_KEYS):
        return None
    ctx = legacy_context(loc)
    dims = ctx["dims"] or []
    D = len(dims)
    pane = ctx["pane"]
    out: dict[str, str] = {}
    consumed: list[str] = []

    def use(*ks: str) -> None:
        for k in ks:
            if k in q and k not in consumed:
                consumed.append(k)

    if q.get("p") == "ccd":
        ix = [_int(q.get(k)) for k in ("x1", "x2", "y1", "y2")]
        ds = [_dim_at(ctx, i) for i in ix]
        if all(ds):
            F = [0.0] * (2 * D)
            F[ix[0]] += _w(ds[0])
            F[ix[1]] -= _w(ds[1])
            F[D + ix[2]] += _w(ds[2])
            F[D + ix[3]] -= _w(ds[3])
            out[f"{pane}.frame"] = encode_frame(F)
            use("p", "x1", "x2", "y1", "y2")
    elif any(k in q for k in ("a", "b", "m")):
        ia, ib, im = _int(q.get("a")), _int(q.get("b")), _int(q.get("m"))
        da, db, dm = _dim_at(ctx, ia), _dim_at(ctx, ib), _dim_at(ctx, im)
        if da and db and dm:
            F = [0.0] * (2 * D)
            F[ia] += _w(da)
            F[ib] -= _w(db)
            F[D + im] = -1.0
            out[f"{pane}.frame"] = encode_frame(F)
            use("a", "b", "m", "p")
    if q.get("y") and ctx["dims"]:
        keys = [d["key"] for d in dims]
        qy = q["y"]
        if qy in keys and D >= 2:
            iy = keys.index(qy)
            F = [0.0] * (2 * D)
            F[0] += _w(dims[0])
            F[1] -= _w(dims[1])
            F[D + iy] = -1.0
            out[f"{pane}.frame"] = encode_frame(F)
        use("y")
    if "cb" in q:
        icb = _int(q["cb"])
        if icb == -1:
            use("cb")
        else:
            d = _dim_at(ctx, icb)
            if d:
                out["c"] = d["key"]
                use("cb")
    if "cd" in q:
        icd = _int(q["cd"])
        d = _dim_at(ctx, icd)
        if icd == -1:
            use("cd", "cm", "cl", "ch")
        elif d:
            lo, hi = _num(q.get("cl")), _num(q.get("ch"))
            filters = {"ranges": {d["key"]: [lo if lo is not None else _OPEN_LO,
                                             hi if hi is not None else _OPEN_HI]}}
            if q.get("cm") == "h":
                out["sel"] = encode_recipe([{"op": "replace", "kind": "filter",
                                             "view": {"ds": ctx["ds"] or ""}, "filters": filters}])
            else:
                out["f"] = encode_json(filters)
            use("cd", "cm", "cl", "ch")
    # rv=1 (reddening-vector toggle), g (gain) and fa=1 (fit-all) have no canonical state and
    # are consumed and dropped; a numeric R_V (> 1.5) maps to rv=.
    if "rv" in q:
        rv = _num(q["rv"])
        if rv is not None and rv > 1.5:
            out["rv"] = _fmt(rv)
        use("rv")
    if "g" in q:
        use("g")
    if "fa" in q:
        use("fa")
    sel = q.get("sel")
    if sel:
        if sel.startswith(("r:", "ids:", "bits:")):
            out.setdefault("sel", sel)
            use("sel")
        else:
            p = sel.split(",")
            ra = _num(p[0]) if p else None
            dec = _num(p[1]) if len(p) > 1 else None
            if len(p) == 2 and ra is not None and dec is not None:
                r = SEL_RADIUS_ARCSEC / 3600
                cosd = max(1e-6, math.cos(math.radians(dec)))
                op = {"op": "add" if "sel" in out else "replace", "kind": "circle",
                      "view": {"ds": ctx["ds"] or "", "x": "ra", "y": "dec"},
                      "shape": {"cx": ra, "cy": dec, "r": r / cosd, "rx": r / cosd, "ry": r}}
                out["sel"] = encode_recipe([op])
                use("sel")
    if not out and not consumed:
        return None
    return out, consumed


# ---------------------------------------------------------------------------
# spherex-colours (contract (C)): the query string of the legacy SPHEREx colours.html
#   ax, ay   CCD x axis = colour ax - ay       bx, by   CCD y axis = colour bx - by
#   c        colour-by column                  band     CMD magnitude band (CMD y axis)
#   cmd=1    CMD panel shown                   zoom     x0,x1,y0,y1 of the CCD
#   yoo      Yoo overlay: '1' = every yoo_* layer of the CCD, else a comma list of layer ids
#   hide     k1|k2 hidden levels of the colour-by category
# dim_alias maps the colour c_<A>_<B> and the magnitude m_<A> onto unified dims, else the
# colour is <a>-<b> of the lower-cased names. The CMD shares the CCD x colour. On a page with
# a single scatter pane band and cmd are consumed and dropped.
# ---------------------------------------------------------------------------

_SPHX_KEYS = ("ax", "ay", "bx", "by", "c", "band", "cmd", "zoom", "yoo", "hide")


def _sphx_mag(alias: Mapping[str, str], f: str) -> str:
    if f"m_{f}" in alias:
        return str(alias[f"m_{f}"])
    if f in alias:
        return str(alias[f])
    return str(f).lower()


def _sphx_colour(alias: Mapping[str, str], a: str, b: str) -> str:
    k = f"c_{a}_{b}"
    if k in alias:
        return str(alias[k])
    return f"{_sphx_mag(alias, a)}-{_sphx_mag(alias, b)}"


def _colour_by_of(opts: Mapping) -> str | None:
    c = opts.get("color") if isinstance(opts, Mapping) else None
    if isinstance(c, str) and c:
        return c
    if isinstance(c, Mapping) and isinstance(c.get("by"), str) and c["by"]:
        return c["by"]
    return None


def _spherex_colours(loc: Mapping) -> Result | None:
    q = _parse_search(loc)
    if not any(k in q for k in _SPHX_KEYS):
        return None
    alias = dim_alias(loc)
    sc = _scatter_panes(loc)
    ccd_p = (next((p for p in sc if p["id"] == "ccd"), None) or (sc[0] if sc else None)
             or {"id": "ccd", "options": {}})
    # The CMD keys need a second scatter pane; without one they are consumed and dropped. A
    # page without layout info keeps the legacy pane ids ccd / cmd.
    if sc:
        cmd_p = (next((p for p in sc if p["id"] == "cmd" and p is not ccd_p), None)
                 or next((p for p in sc if p is not ccd_p), None))
    else:
        cmd_p = {"id": "cmd", "options": {}}
    ccd = ccd_p["id"]
    cmd = cmd_p["id"] if cmd_p else None
    out: dict[str, str] = {}
    consumed: list[str] = []

    def use(*ks: str) -> None:
        for k in ks:
            if k in q and k not in consumed:
                consumed.append(k)

    xcol = None
    if q.get("ax") and q.get("ay"):
        xcol = _sphx_colour(alias, q["ax"], q["ay"])
        out[f"{ccd}.x"] = xcol
    if q.get("bx") and q.get("by"):
        out[f"{ccd}.y"] = _sphx_colour(alias, q["bx"], q["by"])
    use("ax", "ay", "bx", "by")
    if q.get("band") and cmd:
        out[f"{cmd}.y"] = _sphx_mag(alias, q["band"])
        if xcol:
            out[f"{cmd}.x"] = xcol
    use("band")
    if cmd and q.get("cmd") in ("1", "true"):
        out["view"] = f"{cmd}:half"
    use("cmd")
    by = None
    if q.get("c"):
        by = _alias_dim(alias, q["c"])
        out["c"] = by
    use("c")
    if q.get("zoom"):
        p = [_num(x) for x in q["zoom"].split(",")]
        if len(p) == 4 and all(v is not None for v in p):
            out[f"{ccd}.xr"] = f"{_fmt(p[0])},{_fmt(p[1])}"
            out[f"{ccd}.yr"] = f"{_fmt(p[2])},{_fmt(p[3])}"
    use("zoom")
    ov = ccd_p["options"].get("overlays") if isinstance(ccd_p["options"], Mapping) else None
    ovls = [str(o) for o in ov] if isinstance(ov, list) else []
    yoo_all = [o for o in ovls if o.startswith("yoo_")]
    if yoo_all:
        y = q.get("yoo")
        if y in ("1", "true"):
            on = list(yoo_all)
        elif y:
            want = [w if w.startswith("yoo_") else f"yoo_{w}"
                    for w in (t.strip() for t in y.split(",")) if w]
            on = [o for o in yoo_all if o in want]
        else:
            on = []
        lst = [o for o in ovls if not o.startswith("yoo_") or o in on]
        out[f"{ccd}.ovl"] = ",".join(lst) if lst else "-"
    use("yoo")
    if q.get("hide"):
        key = by or _colour_by_of(ccd_p["options"])
        levels = [t.strip() for t in q["hide"].split("|") if t.strip()]
        if key and levels:
            out["f"] = encode_json({"cats": {key: levels}})
    use("hide")
    if not out and not consumed:
        return None
    return out, consumed


_RULES: dict[str, tuple[str, Callable[[Mapping], Result | None]]] = {
    "bare-name": ("hash", _bare_name),
    "kv-view": ("both", _kv_view),
    "gc-query": ("search", _gc_query),
    "wavelength": ("both", _wavelength),
    "beat": ("hash", _beat),
    "gallery-index": ("hash", _gallery_index),
    "w51-rowid": ("hash", _w51_rowid),
    "specviewer": ("hash", _specviewer),
    "spherex-colours": ("search", _spherex_colours),
}

GRAMMAR_NAMES = tuple(_RULES)


def _wrap(fn: Callable[[Mapping], Result | None]) -> Callable[[Mapping], dict | None]:
    def g(loc: Mapping) -> dict | None:
        r = fn(loc or {})
        return None if r is None else {**r[0], **(r[2] if len(r) > 2 else {})}
    return g


GRAMMARS = {k: _wrap(fn) for k, (_, fn) in _RULES.items()}


def map_legacy(grammars: Iterable[str] | str, loc: Mapping) -> dict:
    """``{"hash": {...}, "consumedSearch": [...]}`` -- see ``legacyhash.js`` ``mapLegacy``."""
    names = [grammars] if isinstance(grammars, str) else list(grammars or [])
    h = _hash_text(loc)
    hash_done = (not h) or is_canonical_hash(h)
    out: dict[str, str] = {}
    consumed: list[str] = []
    for name in names:
        rule = _RULES.get(name)
        if rule is None:
            continue
        source, fn = rule
        if source == "hash" and hash_done:
            continue
        res = fn({**loc, "hash": ""}) if (source == "both" and hash_done) else fn(loc)
        if res is None:
            continue
        hsh, cons = res[0], res[1]
        if source == "search":
            out = {**out, **hsh}
        elif hsh and not hash_done:
            out = {**hsh, **out}
            hash_done = True
        if len(res) > 2 and res[2]:
            # the query-string part of a 'both' rule merges whether or not the hash was mapped
            out = {**out, **res[2]}
        for k in cons:
            if k not in consumed:
                consumed.append(k)
    return {"hash": out, "consumedSearch": consumed}
