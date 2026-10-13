"""GC catalog manifold family adapter (WP30; ARCHITECTURE.md sections 9 and 14.2).

Legacy viewers: the GC Treasury manifold (``treasury_manifold``, 3.74M rows) and the 11
per-field manifolds (``<field>_manifold``), deployed under
``/orange/adamginsburg/jwst/releases/site`` and served by starformation.astro.ufl.edu
(``served_by = "external"``). Their data trees (GC manifest schema 1: ``dims/*.u16.gz``,
``errs/*.u8.gz``, ``meta/{dra,ddec}.i16.gz``, ``meta/tile.u8.gz``) are referenced in place:
every catalog here is a foreign :class:`~portalkit.spec.CatalogSpec` that
``legacy/gc_manifest.py`` turns into a ``pk-catalog/1`` manifest whose column files are the
public GC URLs. Nothing is re-encoded; only the u8 tile index is widened to u16.

* One ``[[portal]]`` per viewer (``sites/gc_manifold.toml``). The portal list is the site
  registry; each portal's ``legacy_dir`` holds the GC ``data/manifest.json`` and its ``url``
  (``.../<field>_manifold/unified/``) gives the data URL base (``../data/``).
* Tier A reads the deployed manifests and the legacy viewer JS
  (``gc_{fields,treasury}_manifold_assets/js/app/main.js``: ``FEATURE_CAVEATS``, the Treasury
  ``HIPS_LAYERS``). Tier B also imports ``GCFields_Portal_Manifold`` /
  ``GCTreasury_Portal_Manifold`` and takes the band lists (``FIELDS``, ``DIMS``) and the HiPS
  cutout layers (``CUTOUT_LAYERS``) from them, checking them against the manifests.
* The CMD pane opens on the legacy default axes: the fields viewer's first two CT06-covered
  dims (``EXT_A_AKS``) as the colour, ordered short-minus-long pivot (``PIVOT_UM``,
  ``swapToShortLong``), against the first covered dim; Treasury F212N - F480M vs F212N. The
  CCD preset follows ``ccdDefaults`` (covered bands sorted by pivot). Both tables are parsed
  from the legacy viewer JS in either tier. Fields whose deployed tree disagrees with their
  manifest (``tree_problems``: every gzip member counted) are skipped with the reason.
* Every manifold page forces the T2 GPU path (``state["tier:<pane>"] = "T2"``); fields under
  300k rows would otherwise get T1.
* The legacy query grammar (``?a=&b=&m=&p=&x1..y2&cb&cd&cm&cl&ch&g&rv&fa&sel``, Treasury
  ``y``) is mapped by ``legacyhash.js`` (``grammar = ["gc-query"]`` in the site registry).
  ``legacyContext`` resolves the state onto the page's CMD pane (not the manifold pane),
  including the a/b/m and Treasury ``y`` axis choice that legacy shows there; each page's
  ``PageSpec.legacy`` sets ``cmd_pane = CMD_PANE`` so ``hashmap.py``'s ``_gc_query`` writes
  ``<cmd>.x``/``<cmd>.y`` instead of a raw ``<manifold>.frame`` (WPE11 item 1).
* The red-clump zoom (``findDensityPeak``: densest bin of a 40x40 grid over the 0.5-99.5
  percentile box of the legacy ``sampleJoint`` (every ``SAMPLE_STRIDE_CAP``-th row, kept only
  when every manifold dim decodes), half-width 1/10 of the default fit range) is computed at
  build time from the decoded GC dims. It is a readme deep link (``#cmd.x=..&cmd.xr=..``) that
  zooms the CMD pane, and an ``axes`` preset of the CatalogSpec with the same ``x``/``y``/
  ``xr``/``yr``, which ``legacy/gc_manifest.write`` merges onto the served manifest's top-level
  ``presets`` (WPE9 item 1) alongside ``cmd``/``ccd``.
* The jwst-gc pipeline monitor (``MONITOR_URL``) is out of portal scope (port table 14.2,
  archetype "excluded"); it is linked from the readme only.
"""

from __future__ import annotations

import json
import re
import zlib
from pathlib import Path
from urllib.parse import urljoin

import numpy as np

ANALYSIS = Path("/orange/adamginsburg/jwst/brick/analysis")
FIELDS_JS = ANALYSIS / "gc_fields_manifold_assets" / "js" / "app" / "main.js"
TREASURY_JS = ANALYSIS / "gc_treasury_manifold_assets" / "js" / "app" / "main.js"
MODULES = ("GCFields_Portal_Manifold", "GCTreasury_Portal_Manifold")
SITE_ROOT = Path("/orange/adamginsburg/jwst/releases/site")
HIPS_BASE = "https://starformation.astro.ufl.edu/avm_images/"
MONITOR_URL = "https://data.rc.ufl.edu/pub/adamginsburg/jwst-gc/monitor.html"
TREASURY = "treasury"
SUFFIX = "_manifold"
MANIFOLD_PANE, CMD_PANE, SKY_PANE = "manifold", "cmd", "sky"
TIER = "T2"
#: legacy fit percentiles (main.js FIT_LO_PCT, COLOR_FIT_HI_PCT, MAG_FIT_BRIGHT_PCT, FIT_HI_PCT)
FIT_PCT = {"fields": {"color": (0.5, 97.0), "mag": (0.1, 99.5)},
           TREASURY: {"color": (0.5, 99.5), "mag": (0.1, 99.5)}}
PEAK_BINS, PEAK_PCT, ZOOM_FRAC = 40, (0.5, 99.5), 0.1
#: legacy ``sampleJoint``/``estimateRef`` row stride (``STRIDE = max(1, floor(n / 400000))``):
#: every ``STRIDE``-th row, kept only when every manifold dim decodes (not just the CMD triple).
SAMPLE_STRIDE_CAP = 400_000

#: a legacy ``FEATURE_CAVEATS`` entry that is only legacy-UI instructions this port does not
#: reproduce (a keyboard shortcut); dropped rather than described against a control that is
#: not there.
_CAVEAT_DROP_PREFIX = "Keyboard shortcuts"
#: UI-control references inside an otherwise-kept caveat, rewritten to name no specific legacy
#: control (a checkbox's position, a button) instead of one this port may not place the same way.
_CAVEAT_REWRITES = (
    ("; checkbox below the plot)", ")"),
    ('"Red clump" zoom button jumps', "The red-clump zoom jumps"),
)


def _port_caveat(text: str) -> str | None:
    """A legacy ``FEATURE_CAVEATS`` entry as ported to the unified page's caveats: ``None``
    when it is only legacy-UI instructions this port does not carry over (a keyboard shortcut),
    else with its legacy-control references rewritten (``_CAVEAT_REWRITES``)."""
    if text.startswith(_CAVEAT_DROP_PREFIX):
        return None
    for old, new in _CAVEAT_REWRITES:
        text = text.replace(old, new)
    return text


def field_of(portal_id: str) -> str:
    return portal_id[:-len(SUFFIX)] if portal_id.endswith(SUFFIX) else portal_id


def _fid(b: str) -> str:
    return f"JWST/NIRCam.{b.upper()}"


def _um(b: str) -> float:
    """NIRCam band pivot order from the name (f212n -> 2.12); the legacy ``swapToShortLong``
    orders by SVO pivot, which sorts the GC bands identically."""
    m = re.fullmatch(r"f(\d{3})[wmn]2?", b.lower())
    if m is None:
        raise ValueError(f"not a NIRCam band key: {b!r}")
    return int(m.group(1)) / 100.0


_JS_SIMPLE_ESC = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v"}
_JS_ESC = re.compile(r"\\(?:u\{([0-9A-Fa-f]{1,6})\}|u([0-9A-Fa-f]{4})|x([0-9A-Fa-f]{2})|(\r\n|[\n\r\u2028\u2029])|(.))", re.S)


def js_unescape(s: str) -> str:
    """Decode the escape sequences of a JS string literal body (``\\n``, ``\\xHH``, ``\\uHHHH``
    incl. surrogate pairs, ``\\u{H..}``, line continuations, ``\\0``, identity escapes)."""
    def sub(m: re.Match) -> str:
        if m.group(1) is not None or m.group(2) is not None or m.group(3) is not None:
            cp = int(m.group(1) or m.group(2) or m.group(3), 16)
            if cp > 0x10FFFF:
                raise ValueError(f"JS escape {m.group(0)!r} is out of range")
            return chr(cp)
        if m.group(4) is not None:
            return ""                      # line continuation
        c = m.group(5)
        if c == "0":
            return "\0"
        return _JS_SIMPLE_ESC.get(c, c)
    out = _JS_ESC.sub(sub, s)
    # \uD83D\uDE00 pairs decode to two surrogates: join them into one code point.
    # "surrogatepass" on both sides lets a lone, unpaired surrogate (malformed JS,
    # but not our job to reject here) round-trip unchanged instead of raising
    # UnicodeDecodeError.
    return out.encode("utf-16", "surrogatepass").decode("utf-16", "surrogatepass")


def js_string_array(src: str, name: str) -> list[str]:
    """The ``const <name> = [ 'a' + "b", ... ];`` string array of a legacy JS module."""
    m = re.search(r"\bconst\s+" + re.escape(name) + r"\s*=\s*\[", src)
    if m is None:
        raise ValueError(f"{name} not found")
    tok = re.compile(r"\s*(?:'((?:[^'\\\n]|\\.)*)'|\"((?:[^\"\\\n]|\\.)*)\"|(\+)|(,)|(\])|(//[^\n]*)|(/\*.*?\*/))",
                     re.S)
    out, cur, i = [], None, m.end()
    while True:
        t = tok.match(src, i)
        if t is None:
            raise ValueError(f"{name}: unexpected token at offset {i}")
        i = t.end()
        if t.group(1) is not None or t.group(2) is not None:
            s = js_unescape(t.group(1) if t.group(1) is not None else t.group(2))
            cur = s if cur is None else cur + s
        elif t.group(4) or t.group(5):
            if cur is not None:
                out.append(cur)
            cur = None
            if t.group(5):
                return out


def js_number_map(src: str, name: str) -> dict[str, float]:
    """The ``const <name> = { key: number, ... };`` object of a legacy JS module (comments
    dropped), e.g. ``EXT_A_AKS`` and ``PIVOT_UM`` of the fields viewer."""
    m = re.search(r"\bconst\s+" + re.escape(name) + r"\s*=\s*\{(.*?)\};", src, re.S)
    if m is None:
        raise ValueError(f"{name} not found")
    body = re.sub(r"/\*.*?\*/", "", re.sub(r"//[^\n]*", "", m.group(1)), flags=re.S)
    out = {}
    for item in (x.strip() for x in body.split(",")):
        if not item:
            continue
        kv = re.fullmatch(r"['\"]?([A-Za-z_$][\w$]*)['\"]?\s*:\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)", item)
        if kv is None:
            raise ValueError(f"{name}: cannot parse entry {item!r}")
        out[kv.group(1)] = float(kv.group(2))
    return out


def js_hips_layers(src: str) -> list[dict]:
    """``HIPS_LAYERS = [{ id, dir, name }, ...]`` of a legacy viewer, as cutout-layer dicts."""
    m = re.search(r"\bconst\s+HIPS_LAYERS\s*=\s*\[(.*?)\];", src, re.S)
    if m is None:
        raise ValueError("HIPS_LAYERS not found")
    rx = re.compile(r"\{\s*id:\s*'([^']*)',\s*dir:\s*'([^']*)',\s*name:\s*'([^']*)'\s*\}")
    return [{"id": i, "url": HIPS_BASE + d, "name": n} for i, d, n in rx.findall(m.group(1))]


def _u16(path: Path, lo: float, hi: float, n: int) -> np.ndarray:
    from portalkit.bundle import codec
    c = np.frombuffer(codec.unshuffle(codec.gunzip(path.read_bytes()), 2), dtype="<u2")
    if c.size != n:
        raise ValueError(f"{path}: {c.size} rows, manifest n={n}")
    x = lo + (c.astype(np.float64) - 1.0) * ((hi - lo) / codec.QMAX)
    x[c == 0] = np.nan
    return x


def _positions(data: Path, man: dict) -> tuple[np.ndarray, np.ndarray]:
    from portalkit.bundle import codec
    meta, n = man["meta"], int(man["n"])
    tile = np.frombuffer(codec.gunzip((data / meta["tile"]["file"]).read_bytes()), dtype=np.uint8)
    if tile.size != n:
        raise ValueError(f"{data}: tile index has {tile.size} rows, manifest n={n}")
    tile = tile.astype(np.uint16)
    files = {"tile": codec.gz(codec.shuffle_u16(tile)),
             "dra": (data / meta["dra"]["file"]).read_bytes(),
             "ddec": (data / meta["ddec"]["file"]).read_bytes()}
    pos = {"enc": "i16off", "step_mas": float(man["encoding"].get("offset_step_mas", 8.0)),
           "tile_refs": [[r["ref_ra"], r["ref_dec"]] for r in man["tile_refs"]]}
    return codec.decode_pos(pos, files)


def _gz_size(path: Path) -> int:
    """Uncompressed size of a gzip file, every member counted (a file written in chunks or
    concatenated has several members, and the ISIZE trailer only holds the last one's size);
    the plain file size when the file is not gzip. Streams in 1 MiB blocks."""
    with open(path, "rb") as fh:
        if fh.read(2) != b"\x1f\x8b":
            fh.seek(0, 2)
            return fh.tell()
        fh.seek(0)
        total, d = 0, zlib.decompressobj(31)
        while True:
            buf = fh.read(1 << 20)
            if not buf:
                break
            while buf:
                if d.eof:
                    if not buf.strip(b"\0"):
                        break               # trailing zero padding after the last member
                    d = zlib.decompressobj(31)
                total += len(d.decompress(buf, 1 << 24))
                buf = d.unused_data if d.eof else d.unconsumed_tail
        if not d.eof:
            raise ValueError(f"{path}: truncated gzip stream")
        return total


def tree_problems(data: Path, man: dict) -> list[str]:
    """Files of a GC data tree whose row count disagrees with the manifest ``n`` (a partial
    re-deploy leaves such files behind; the viewer would then mis-place or drop rows)."""
    n = int(man["n"])
    want = [(d["file"], 2) for d in man["dims"]] + [(e["file"], 1) for e in man.get("errs") or []]
    want += [(man["meta"][k]["file"], 2) for k in ("dra", "ddec")] + [(man["meta"]["tile"]["file"], 1)]
    out = []
    for rel, width in want:
        p = data / rel
        if not p.is_file():
            out.append(f"{rel}: missing")
            continue
        try:
            size = _gz_size(p)
        except (ValueError, zlib.error) as exc:
            out.append(f"{rel}: unreadable ({exc})")
            continue
        if size != n * width:
            out.append(f"{rel}: {size / width:g} rows, manifest n={n}")
    return out


def _pct(x: np.ndarray, p: tuple[float, float]) -> list[float] | None:
    """``[lo, hi]`` percentiles of the finite values of ``x`` (4 decimals), or None when there
    are none or the range is empty (``lo == hi``: no usable axis range)."""
    x = np.asarray(x, dtype=np.float64)
    x = x[np.isfinite(x)]
    if x.size == 0:
        return None
    lo, hi = (round(float(v), 4) for v in np.percentile(x, p))
    return [lo, hi] if hi > lo else None


def density_peak(color: np.ndarray, mag: np.ndarray, nbins: int = PEAK_BINS):
    """Python twin of the legacy ``findDensityPeak`` (centre of the densest bin), or None."""
    color, mag = np.asarray(color, dtype=np.float64), np.asarray(mag, dtype=np.float64)
    ok = np.isfinite(color) & np.isfinite(mag)
    color, mag = color[ok], mag[ok]
    if color.size == 0:
        return None
    (c0, c1), (m0, m1) = np.percentile(color, PEAK_PCT), np.percentile(mag, PEAK_PCT)
    bx, by = (c1 - c0) / nbins, (m1 - m0) / nbins
    if not (bx > 0 and by > 0):
        return None
    gx, gy = np.floor((color - c0) / bx), np.floor((mag - m0) / by)
    ok = (gx >= 0) & (gx < nbins) & (gy >= 0) & (gy < nbins)
    grid = np.bincount((gy[ok] * nbins + gx[ok]).astype(np.int64), minlength=nbins * nbins)
    k = int(np.argmax(grid))      # first maximum, as the legacy strict '>' scan
    return float(c0 + (k % nbins + 0.5) * bx), float(m0 + (k // nbins + 0.5) * by)


def _short_long(keys: list[str], a: int, b: int, pivot: dict[str, float]) -> tuple[int, int]:
    """Legacy ``swapToShortLong``: order a colour's two dims shorter-pivot first; a band
    without a tabulated pivot leaves the picked order alone."""
    pa, pb = pivot.get(keys[a]), pivot.get(keys[b])
    return (b, a) if pa is not None and pb is not None and pa > pb else (a, b)


def legacy_cmd(keys: list[str], ext: dict[str, float], pivot: dict[str, float],
               treasury: bool = False) -> tuple[str, str, str]:
    """Default CMD ``(colour A, colour B, magnitude)`` of a legacy viewer over its dims ``keys``
    (manifest order). Fields viewer: the first two CT06-covered dims (``EXT_A_AKS``) for the
    colour, else dims 0 and min(1, D-1), and the first covered dim (else dim 0) for the
    magnitude, colour ordered short-minus-long pivot. Treasury: dim 0 - dim 1 against dim 0
    (its band toggle starts on F212N)."""
    D = len(keys)
    if D == 0:
        raise ValueError("a GC manifold has no dims")
    if treasury:
        a, b, m = 0, min(1, D - 1), 0
    else:
        cov = [i for i, k in enumerate(keys) if k in ext]
        a, b = (cov[0], cov[1]) if len(cov) >= 2 else (0, min(1, D - 1))
        m = cov[0] if cov else 0
    a, b = _short_long(keys, a, b, pivot)
    return keys[a], keys[b], keys[m]


def legacy_ccd(keys: list[str], ext: dict[str, float],
               pivot: dict[str, float]) -> tuple[str, str] | None:
    """Default CCD colours ``("x1-x2", "y1-y2")`` of the fields viewer (``ccdDefaults``), or
    None when CCD mode is unavailable (D < 3). The CT06-covered bands sorted by pivot give the
    widest baseline on X and the next one on Y (3 covered bands: Y shares X's short end);
    with fewer than 3 covered bands it falls back to dim positions. Each colour is ordered
    short-minus-long pivot, as ``setFrameCCD`` does."""
    D = len(keys)
    cov = sorted((i for i, k in enumerate(keys) if k in ext and k in pivot), key=lambda i: pivot[keys[i]])
    if len(cov) >= 4:
        x1, x2, y1, y2 = cov[0], cov[-1], cov[1], cov[-2]
    elif len(cov) == 3:
        x1, x2, y1, y2 = cov[0], cov[2], cov[0], cov[1]
    elif D >= 4:
        x1, x2, y1, y2 = 0, D - 1, 1, max(1, D - 2)
    elif D == 3:
        x1, x2, y1, y2 = 0, 2, 0, 1
    else:
        return None
    (x1, x2), (y1, y2) = _short_long(keys, x1, x2, pivot), _short_long(keys, y1, y2, pivot)
    return f"{keys[x1]}-{keys[x2]}", f"{keys[y1]}-{keys[y2]}"


class GcManifoldAdapter:
    name = "gc_manifold"
    tiers = ("A", "B")

    # -- registry ---------------------------------------------------------------------------
    def _fields(self, ctx) -> list[dict]:
        out = []
        for p in ctx.site.portals:
            f = field_of(p.id)
            data = Path(p.legacy_dir) / "data"
            mpath = data / "manifest.json"
            ctx.inputs.add(mpath)
            man = json.loads(mpath.read_text(encoding="utf-8"))
            if man.get("schema") != 1:
                raise ValueError(f"{mpath}: GC manifest schema {man.get('schema')!r}, expected 1")
            out.append({"field": f, "portal": p, "data": data, "mpath": mpath, "man": man,
                        "url_base": urljoin(p.url, "../data/"), "kind": TREASURY if f == TREASURY else "fields"})
        return out

    def _legacy(self, ctx, fields) -> dict:
        """Per-field band list and HiPS layers (Tier A: manifests + JS; Tier B: generators)."""
        js = {}
        for k, p in (("fields", FIELDS_JS), (TREASURY, TREASURY_JS)):
            ctx.inputs.add(p)
            js[k] = p.read_text(encoding="utf-8")
        caveats = {k: [c for c in (_port_caveat(x) for x in js_string_array(s, "FEATURE_CAVEATS")) if c is not None]
                  for k, s in js.items()}
        # the band tables that pick the default CMD / CCD axes (CT06 coverage, SVO pivots)
        ext = {"fields": js_number_map(js["fields"], "EXT_A_AKS"), TREASURY: js_number_map(js[TREASURY], "EXT_A_AKS")}
        self._ext, self._pivot = ext, js_number_map(js["fields"], "PIVOT_UM")
        thips = js_hips_layers(js[TREASURY])
        out = {}
        if ctx.tier == "A":
            for f in fields:
                m = f["man"]
                hips = [dict(c) for c in m.get("cutout_layers") or []] or \
                    (thips if f["kind"] == TREASURY else js_hips_layers(js["fields"]))
                out[f["field"]] = {"bands": [d["key"] for d in m["dims"]], "hips": hips,
                                   "caveats": caveats[f["kind"]]}
            return out
        gf, gt = (ctx.legacy.module(n) for n in MODULES)
        for f in fields:
            k, m = f["field"], f["man"]
            if f["kind"] == TREASURY:
                bands, hips = [d["key"] for d in gt.DIMS], thips
            else:
                if k not in gf.FIELDS or k not in gf.CUTOUT_LAYERS:
                    raise ValueError(f"field {k!r} is not in GCFields_Portal_Manifold.FIELDS/CUTOUT_LAYERS")
                bands = list(gf.FIELDS[k]["bands"])
                hips = [{"id": f"{k}-{i}", "url": gf.HIPS_BASE + d, "name": n, "maxOrder": int(o)}
                        for i, (d, n, o) in enumerate(gf.CUTOUT_LAYERS[k])]
            if bands != [d["key"] for d in m["dims"]]:
                raise ValueError(f"{k}: legacy bands {bands} differ from the manifest dims")
            out[k] = {"bands": bands, "hips": hips, "caveats": caveats[f["kind"]]}
        return out

    # -- derived values (memoised) ------------------------------------------------------------
    def _stats(self, ctx, f, cmd) -> dict:
        data, m = f["data"], f["man"]
        dims = {d["key"]: d for d in m["dims"]}
        keys = list(dims)                                  # manifest order == legacy data.raw order
        paths = [f["mpath"]] + [data / dims[k]["file"] for k in keys] + \
            [data / m["meta"][k]["file"] for k in ("dra", "ddec", "tile")]

        def load() -> dict:
            n = int(m["n"])
            # legacy sampleJoint: every STRIDE-th row, kept only when every dim decodes (v != 0)
            # -- not just the CMD triple's dims, which on a sparsely-covered multi-band field
            # admits rows the legacy fit/peak never saw.
            stride = max(1, n // SAMPLE_STRIDE_CAP)
            idx = np.arange(0, n, stride)
            X = {k: _u16(data / dims[k]["file"], dims[k]["min"], dims[k]["max"], n)[idx] for k in keys}
            ok = np.ones(idx.size, dtype=bool)
            for k in keys:
                ok &= np.isfinite(X[k])
            a, b, y = (X[k][ok] for k in cmd)
            color, mag = a - b, y
            pct = FIT_PCT[f["kind"]]
            xr, yr = _pct(color, pct["color"]), _pct(mag, pct["mag"])
            peak = density_peak(color, mag)
            ra, dec = _positions(data, m)
            g = np.isfinite(ra) & np.isfinite(dec)
            if g.any():
                (r0, r1), (d0, d1) = np.percentile(ra[g], (0.5, 99.5)), np.percentile(dec[g], (0.5, 99.5))
                rc, dc = float(np.median(ra[g])), float(np.median(dec[g]))
            else:
                # no decodable position: centre on the tile references
                refs = np.array([[r["ref_ra"], r["ref_dec"]] for r in m["tile_refs"]], dtype=np.float64)
                (r0, d0), (r1, d1) = refs.min(axis=0), refs.max(axis=0)
                rc, dc = float(np.median(refs[:, 0])), float(np.median(refs[:, 1]))
            fov = float(np.clip(1.15 * max((r1 - r0) * np.cos(np.radians(dc)), d1 - d0), 0.02, 4.0))
            return {"n_cmd": int(ok.sum()), "xr": xr, "yr": yr, "peak": list(peak) if peak else None,
                    "center": [rc, dc], "fov": round(fov, 4)}
        return ctx.cache.memo(f"gc_manifold_stats_v3_{f['field']}_{'_'.join(cmd)}", load, inputs=paths)

    # -- datasets ---------------------------------------------------------------------------
    def datasets(self, ctx):
        from portalkit.spec import ExtinctionSpec, FilterSetSpec, TierUnavailable
        if ctx.tier not in self.tiers:
            raise TierUnavailable(f"gc_manifold implements tiers A and B (the GC binaries are "
                                  f"referenced in place; there is no catalogue-native Tier C), not {ctx.tier}")
        fields, self.skipped = [], {}
        for f in self._fields(ctx):
            bad = self._tree_problems(ctx, f)
            if bad:
                # the URLs point at this tree: an inconsistent one cannot be referenced in place
                self.skipped[f["field"]] = bad
                ctx.log(f"gc_manifold: skipping {f['portal'].id}: data tree {f['data']} is inconsistent "
                        f"({'; '.join(bad)})")
            else:
                fields.append(f)
        if not fields:
            raise ValueError("gc_manifold: no consistent GC data tree")
        leg = self._legacy(ctx, fields)
        ds, bands_all, self._info = {}, set(), {}
        for f in fields:
            k, L = f["field"], leg[f["field"]]
            bands, ext = list(L["bands"]), self._ext[f["kind"]]
            cmd = legacy_cmd(bands, ext, self._pivot, treasury=f["kind"] == TREASURY)
            ccd = None if f["kind"] == TREASURY else legacy_ccd(bands, ext, self._pivot)
            st = self._stats(ctx, f, cmd)
            info = {"f": f, "bands": bands, "cmd": cmd, "stats": st, "ccd": ccd}
            self._info[k] = info
            ds[f"cat_{k}"] = self._catalog(f, L, info)
            ds[f"hips_{k}"] = self._hips(L["hips"], st)
            bands_all |= set(bands)
        ds["flt"] = FilterSetSpec(keys=[_fid(b) for b in sorted(bands_all, key=_um)])
        ds["ext"] = ExtinctionSpec()
        for k, info in self._info.items():
            ds[f"readme_{k}"] = self._readme(ctx, k, info, ds[f"cat_{k}"])
        return ds

    @staticmethod
    def _tree_problems(ctx, f) -> list[str]:
        """:func:`tree_problems` of a field, memoised on the stamps of its data files."""
        m, data = f["man"], f["data"]
        rels = [d["file"] for d in m["dims"]] + [e["file"] for e in m.get("errs") or []] + \
            [m["meta"][k]["file"] for k in ("dra", "ddec", "tile")]
        paths = [f["mpath"]] + [data / r for r in rels if (data / r).is_file()]
        return ctx.cache.memo(f"gc_manifold_tree_v2_{f['field']}", lambda: tree_problems(data, m), inputs=paths)

    def _catalog(self, f, L, info):
        from portalkit.spec import (CatalogSpec, ColumnSpec, ForeignRef, Group, ManifoldSpec, Preset,
                                    VirtualDim)
        m, bands = f["man"], info["bands"]
        errs = {e.get("dim_key") or e["key"] for e in m.get("errs") or []}
        dims = {d["key"]: d for d in m["dims"]}
        cols = []
        for b in [d["key"] for d in m["dims"]]:
            d = dims[b]
            cols.append(ColumnSpec(b, "mag", label=d.get("label") or b.upper(), unit="mag", filter=_fid(b),
                                   err=f"e_{b}" if b in errs else None, group="phot", note=d.get("desc", "")))
            if b in errs:
                cols.append(ColumnSpec(f"e_{b}", "error", label=f"σ {b.upper()}", unit="mag"))
        cols.append(ColumnSpec("tile", "category", label="Source table",
                               levels=[r["file"] for r in m["tile_refs"]]))
        a, b2, y = info["cmd"]
        color = f"{a}-{b2}"
        presets = [Preset("cmd", f"CMD {a.upper()}-{b2.upper()} vs {y.upper()}", "axes", {"x": color, "y": y})]
        virt = [VirtualDim(color, label=f"{a.upper()} - {b2.upper()}", unit="mag", group="colors")] if a != b2 else []
        if info["ccd"]:
            x, yy = info["ccd"]
            presets.append(Preset("ccd", "CCD", "axes", {"x": x, "y": yy}))
            virt += [VirtualDim(e, label=e.upper().replace("-", " - "), unit="mag", group="colors")
                     for e in (x, yy) if e not in {v.key for v in virt}]
        rc = self.red_clump(info)
        if rc is not None:
            # a zoom of the CMD axes (matches the readme's #cmd.x=..&cmd.xr=.. deep link), not a
            # filter: 'filter' would hide every star outside the box instead of zooming to it
            presets.append(Preset("red_clump", "Red clump", "axes",
                                  {"x": color, "y": y, "xr": rc["xr"], "yr": rc["yr"]}))
        title = f"{f['portal'].title} ({int(m['n']):,} sources)"
        return CatalogSpec(
            key=f"cat_{f['field']}", table=None, id="", columns=cols, virtual=virt,
            groups=[Group("phot", "NIRCam Vega magnitudes", [d["key"] for d in m["dims"]])],
            presets=presets, manifold=ManifoldSpec(dims=[d["key"] for d in m["dims"]]),
            title=title, caveats=list(L["caveats"]),
            foreign=ForeignRef(kind="gc-manifest-1", url_base=f["url_base"], manifest_path=f["mpath"]))

    @staticmethod
    def red_clump(info) -> dict | None:
        """Legacy red-clump zoom box: densest bin +- 1/10 of the default fit range."""
        st = info["stats"]
        if not st.get("peak") or not st.get("xr") or not st.get("yr") or info["cmd"][0] == info["cmd"][1]:
            return None
        (c, mg), xr, yr = st["peak"], st["xr"], st["yr"]
        hx, hy = abs(xr[1] - xr[0]) * ZOOM_FRAC, abs(yr[1] - yr[0]) * ZOOM_FRAC
        return {"peak": [round(c, 4), round(mg, 4)], "xr": [round(c - hx, 4), round(c + hx, 4)],
                "yr": [round(mg - hy, 4), round(mg + hy, 4)]}

    def _hips(self, layers, st):
        from portalkit.spec import HipsSpec
        base = [{"id": c["id"], "name": c["name"], "url": c["url"], "default": i == 0,
                 **({"maxOrder": int(c["maxOrder"])} if c.get("maxOrder") else {})}
                for i, c in enumerate(layers)]
        ra, dec = st["center"]
        return HipsSpec(base=base, target={"ra": ra, "dec": dec, "fov": st["fov"]})

    def _readme(self, ctx, k, info, cat):
        from portalkit.spec import ReadmeSpec
        f, st, m = info["f"], info["stats"], info["f"]["man"]
        a, b, y = info["cmd"]
        rc = self.red_clump(info)
        src = (m.get("sample") or {}).get("parent") or ""
        fit = (f"; default axes span the legacy fit range (colour {st['xr'][0]:g} to {st['xr'][1]:g}, "
               f"magnitude {st['yr'][0]:g} to {st['yr'][1]:g})") if st.get("xr") and st.get("yr") else ""
        lines = [f"# {f['portal'].title}", "",
                 f"{int(m['n']):,} sources in {len(info['bands'])} NIRCam band(s) "
                 f"({', '.join(x.upper() for x in info['bands'])}).{(' ' + src) if src else ''}", "",
                 "The GC manifold binaries are read in place from "
                 f"[{f['url_base']}]({f['url_base']}) (GC manifest schema 1; nothing is re-encoded).", "",
                 "## Views", "",
                 f"- **Manifold**: projection of all bands (GPU density, tier {TIER}).",
                 f"- **CMD**: {a.upper()} - {b.upper()} vs {y.upper()}{fit}.",
                 "- **Sky**: the release HiPS mosaics with the catalogue positions."]
        if rc is not None:
            h = (f"#{CMD_PANE}.x={a}-{b}&{CMD_PANE}.y={y}&{CMD_PANE}.xr={rc['xr'][0]},{rc['xr'][1]}"
                 f"&{CMD_PANE}.yr={rc['yr'][0]},{rc['yr'][1]}")
            lines += ["", "## Red clump", "",
                      f"[Zoom the CMD to the red clump]({h}): the densest bin of a {PEAK_BINS}x{PEAK_BINS} grid "
                      f"over the 0.5-99.5 percentile box (colour {rc['peak'][0]:g}, {y.upper()} {rc['peak'][1]:g}), "
                      f"+- 1/10 of the fit range (the legacy \"Red clump\" button)."]
        lines += ["", "## Caveats", ""] + [f"- {c}" for c in list(cat.caveats) + list(m.get("caveats") or [])]
        lines += ["", "## Related", "",
                  f"- [jwst-gc pipeline monitor]({MONITOR_URL}) (QA dashboard; not part of this portal)."]
        if ctx.limit is not None:
            lines += ["", f"Fixture build (limit {ctx.limit}): foreign GC catalogues are referenced whole."]
        return ReadmeSpec(markdown="\n".join(lines) + "\n")

    # -- pages ------------------------------------------------------------------------------
    def pages(self, ctx, ds):
        from portalkit.spec import LegacyRef, PageSpec
        info_all = getattr(self, "_info", None)
        if info_all is None:
            raise ValueError("pages() needs datasets() first")
        out = []
        for k, info in info_all.items():
            cat, hips = f"cat_{k}", f"hips_{k}"
            a, b, y = info["cmd"]
            st = info["stats"]
            layout = {"row": [
                {"pane": "manifold", "id": MANIFOLD_PANE, "dataset": cat},
                {"col": [{"pane": "scatter", "id": CMD_PANE, "dataset": cat,
                          "options": {"x": f"{a}-{b}" if a != b else a, "y": y, "presets": True}},
                         {"pane": "sky", "id": SKY_PANE, "dataset": cat, "options": {"hips": hips}},
                         {"pane": "readme"}], "size": 440}]}
            axes = {"x": f"{a}-{b}" if a != b else a, "y": y}
            if a != b and st.get("xr") and st.get("yr"):
                axes.update(xr=st["xr"], yr=st["yr"])
            out.append(PageSpec(
                id="index", archetype="manifold", title=info["f"]["portal"].title,
                datasets=[cat, hips, f"readme_{k}", "flt", "ext"], layout=layout, portal=info["f"]["portal"].id,
                state={f"tier:{MANIFOLD_PANE}": TIER, f"tier:{CMD_PANE}": TIER, f"axes:{CMD_PANE}": axes},
                # cmd_pane (contract E) routes the gc-query legacy a/b/m axes onto the CMD scatter
                # pane's x/y instead of the manifold pane's frame; _merge_legacy (build.py) fills
                # in files/grammar/dim_alias from the site registry's [[portal.page]] entry and
                # keeps this value, since cmd_pane names a pane of this page and has no site-TOML
                # equivalent (WPE11 item 1).
                legacy=LegacyRef(files=[], cmd_pane=CMD_PANE),
                subtitle=f"{int(info['f']['man']['n']):,} sources, {len(info['bands'])} bands"))
        return out


ADAPTER = GcManifoldAdapter()


# -- _fallback ------------------------------------------------------------------------------
# Empty: no legacy loader has been copied. Tier B reads only module constants (FIELDS,
# CUTOUT_LAYERS, HIPS_BASE, DIMS); Tier A covers the same values from the deployed manifests.
