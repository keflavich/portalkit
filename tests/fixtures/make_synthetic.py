"""Synthetic Level 1 portals for the e2e, perf and parity tests (ARCHITECTURE.md section 13).

The portals are built through the bundle writers, the physics writers, ``caps.resolve``,
``config.assemble`` and ``render.shell`` directly (not through ``build.py``), so the e2e
tests exercise the bundle formats and the runtime without depending on the adapter layer.

One portal per catalog size N (8, 2k, 50k, 1M; 5M only for slow tests) is written to
``<root>/n<N>/``:

* ``src``      catalog: ids ``SYN-%07d`` (the first 50 rows carry a ``W51-%06d`` alias and a
  Galactic-style alias with a literal ``+``, ``G%03d.1+0.%03d``),
  clustered positions near the Galactic centre, 7 NIRCam magnitudes with errors (2 % NaN,
  rounded to 0.001 mag), a limit-code column, a category with a unicode level, a flag, a
  value and (small N) a text column; axes / selection / filter presets; virtual colours.
* ``ctx``      context catalog (min(max(N/2, 8), 50k) rows).
* ``spec``     20 ``pks1`` spectra (two gratings, err channel, a variant, mask runs).
* ``s2d``      20 ``pki1`` 2D spectra; ``s2dpng`` the same ids in PNG mode.
* ``img``      20 image items with ``sed`` and ``cutout`` roles, joined to ``src``.
* ``prod``     20 ``products/1`` json records (detail product links relative to the records
  index, IMPLEMENTATION_NOTES item 81); the linked files exist under ``data/prod/products/``.
* ``tpl``/``flt``/``ext``/``feat``  shared physics resources under ``<root>/res``
  (``<portal>/res`` is a symlink to it); ``iso`` 3 synthetic isochrone tracks.
* ``hips``     relative HiPS stubs, rungs, bookmarks; ``ovl`` sky and dim overlays;
  ``nar`` a 6-beat narrative (b1..b5 with sky views, b3 also with a highlight; b6 a
  highlight-only beat: highlight, no view); ``readme``.
* ``vc``       a 120-dim manifold catalog (40 stored mags + 80 virtual colours), which
  forces the CPU projection path (folded support 40 > MAXD 28). N is capped at 50k.

Pages: ``index`` (landing), ``explorer``, ``manifold``, ``gallery``, ``spectra``,
``spectra_png``, ``wavelength``, ``narrative``, ``vcolor``, ``hist`` (explicit layout: list,
two histogram panes, detail) and ``cmd`` (explicit layout with an uncollapsed CMD scatter pane,
list and detail, with isochrone and extinction overlays; its detail pane shows the ``prod``
products). ``<portal>/_pk/v<pk>`` is a symlink
to the repository ``web/`` tree, so the runtime is served live; the build stamp hashes
``web/`` as well (IMPLEMENTATION_NOTES item 94), since the shell and the vendored asset list
are derived from it.

Usage::

    python tests/fixtures/make_synthetic.py OUT_DIR --n 8 2000
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from astropy.table import Table

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from portalkit import caps as capsmod  # noqa: E402
from portalkit import config  # noqa: E402
from portalkit.bundle import catalog as catalog_writer  # noqa: E402
from portalkit.bundle import images as images_writer  # noqa: E402
from portalkit.bundle import records as records_writer  # noqa: E402
from portalkit.bundle.writer import BundleWriter, Ledger, atomic_write, dumps_json  # noqa: E402
from portalkit.render import shell  # noqa: E402
from portalkit.schema import validate_portal  # noqa: E402
from portalkit.spec import (CatalogSpec, ColumnSpec, DensitySpec, ExtinctionSpec,  # noqa: E402
                            FilterSetSpec, Group, HipsSpec, Image2D, ImageItem, ImageSetSpec,
                            IsochroneSpec, LegacyRef, ManifoldSpec, NarrativeSpec, OverlaySpec,
                            PageSpec, Preset, ReadmeSpec, RecordSetSpec, Spectrum, TemplateSpec,
                            Track, VirtualDim)
from portalkit.version import VERSION  # noqa: E402

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

SIZES: tuple[int, ...] = (8, 2000, 50000, 1000000)
SLOW_SIZES: tuple[int, ...] = (5000000,)
N_RECORDS = 20
N_IMAGES = 20
N_BEATS = 5
VC_DIMS = 120
VC_STORED = 40
VC_N_MAX = 50000
N_ALIASES = 50
SEED = 20261010
BUILD = "20261010T120000Z-5e1f0c0d"
PORTAL = {"id": "synth", "title": "Synthetic portal", "visibility": "public"}
CENTER = (266.4168, -29.0078)
WEB = REPO / "web"

#: (column key, filter registry key, offset from the base magnitude, A_lambda / A_V)
MAGS: tuple[tuple[str, str, float, float], ...] = (
    ("f115w", "JWST/NIRCam.F115W", 2.2, 0.36),
    ("f182m", "JWST/NIRCam.F182M", 0.9, 0.15),
    ("f212n", "JWST/NIRCam.F212N", 0.6, 0.11),
    ("f360m", "JWST/NIRCam.F360M", 0.15, 0.05),
    ("f405n", "JWST/NIRCam.F405N", 0.05, 0.04),
    ("f410m", "JWST/NIRCam.F410M", 0.0, 0.04),
    ("f444w", "JWST/NIRCam.F444W", -0.05, 0.035),
)
MAG_KEYS: tuple[str, ...] = tuple(m[0] for m in MAGS)
FILTER_KEYS: tuple[str, ...] = tuple(m[1] for m in MAGS)
CLASSES: tuple[str, ...] = ("star", "yso", "galaxy", "Ω-unknown")
COLOURS: tuple[str, ...] = ("f182m-f410m", "f405n-f410m", "f115w-f212n", "f360m-f444w")
CMD_X, CMD_Y = "f182m-f410m", "f410m"

#: page id -> (archetype, legacy grammars)
PAGES: dict[str, tuple[str, tuple[str, ...]]] = {
    "index": ("landing", ()),
    "explorer": ("explorer", ("kv-view", "w51-rowid", "bare-name")),
    "manifold": ("manifold", ("gc-query",)),
    "gallery": ("gallery", ("gallery-index",)),
    "spectra": ("spectra", ("specviewer",)),
    "spectra_png": ("spectra", ()),
    "wavelength": ("wavelength", ("wavelength",)),
    "narrative": ("narrative", ("beat",)),
    "vcolor": ("manifold", ()),
    "hist": ("explorer", ()),
    "cmd": ("explorer", ()),
}

#: ids of the two histogram panes on the ``hist`` page and their expressions
HIST_PANES: tuple[tuple[str, str, int | None], ...] = (("hist", "f410m", None), ("hist_color", CMD_X, 40))


def syn_id(i: int) -> str:
    return f"SYN-{i:07d}"


def alias_of(i: int) -> str:
    return f"W51-{i:06d}"


def plus_alias_of(i: int) -> str:
    """Second alias with a literal '+' (legacy bare-name / specviewer hashes keep it)."""
    return f"G{i:03d}.1+0.{i:03d}"


# ---------------------------------------------------------------------------
# PNG encoder (pure zlib; no Pillow dependency)
# ---------------------------------------------------------------------------

def png_bytes(arr: np.ndarray) -> bytes:
    """Encode a uint8 array (H, W) grey or (H, W, 3) RGB as a PNG."""
    a = np.ascontiguousarray(arr, dtype=np.uint8)
    if a.ndim == 2:
        h, w = a.shape
        ctype, rowb = 0, a
    elif a.ndim == 3 and a.shape[2] == 3:
        h, w = a.shape[:2]
        ctype, rowb = 2, a.reshape(h, w * 3)
    else:
        raise ValueError(f"png_bytes needs (H, W) or (H, W, 3), got {a.shape}")
    raw = b"".join(b"\x00" + rowb[y].tobytes() for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def _plot_png(seed: int, w: int = 160, h: int = 120) -> bytes:
    """A small deterministic RGB 'plot' (a curve on a light background)."""
    rng = np.random.default_rng(seed)
    img = np.full((h, w, 3), 245, dtype=np.uint8)
    x = np.arange(w)
    y = (h / 2 + (h / 3) * np.sin(x / (8 + 6 * rng.random()) + rng.random() * 6)).astype(int)
    y = np.clip(y, 1, h - 2)
    for dy in (-1, 0, 1):
        img[y + dy, x] = (40, 90, 200)
    img[:, 0] = img[-1, :] = 60
    return png_bytes(img)


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------

@dataclass
class SourceData:
    """The ``src`` catalog arrays in input order (reference values for numpy masks)."""
    n: int
    ids: np.ndarray
    ra: np.ndarray
    dec: np.ndarray
    mags: dict[str, np.ndarray]
    errs: dict[str, np.ndarray]
    lim: np.ndarray
    cls: np.ndarray
    good: np.ndarray
    teff: np.ndarray

    def expr(self, e: str) -> np.ndarray:
        """Evaluate a linear expression over the magnitude keys (NaN propagates)."""
        from portalkit.expr import compile as compile_expr
        c = compile_expr(e)
        w, const = (c["w"], c["c"]) if isinstance(c, dict) else (c.w, c.c)
        out = np.full(self.n, float(const))
        for k, coeff in w.items():
            out = out + coeff * self.column(k)
        return out

    def column(self, key: str) -> np.ndarray:
        if key in self.mags:
            return self.mags[key]
        if key in self.errs:
            return self.errs[key]
        if key == "teff":
            return self.teff
        if key == "ra":
            return self.ra
        if key == "dec":
            return self.dec
        raise KeyError(key)


def make_source_data(n: int, seed: int = SEED) -> SourceData:
    rng = np.random.default_rng(seed)
    # positions: three clusters plus a field population
    comp = rng.choice(4, size=n, p=[0.5, 0.2, 0.15, 0.15])
    cra = np.array([CENTER[0], CENTER[0] + 0.05, CENTER[0] - 0.04, CENTER[0] + 0.01])
    cdec = np.array([CENTER[1], CENTER[1] + 0.03, CENTER[1] - 0.02, CENTER[1] - 0.05])
    sig = np.array([0.08, 0.01, 0.015, 0.006])
    dec = cdec[comp] + rng.normal(0.0, 1.0, n) * sig[comp]
    ra = cra[comp] + rng.normal(0.0, 1.0, n) * sig[comp] / np.cos(np.radians(CENTER[1]))
    base = rng.normal(17.0, 1.6, n)
    av = rng.gamma(4.0, 6.0, n)
    mags: dict[str, np.ndarray] = {}
    errs: dict[str, np.ndarray] = {}
    for key, _filt, off, alav in MAGS:
        m = base + off + alav * av + rng.normal(0.0, 0.03, n)
        m = np.round(m, 3)
        nanmask = rng.random(n) < 0.02
        m[nanmask] = np.nan
        e = np.round(0.005 + 0.02 * 10 ** (0.4 * (m - 21.0)), 4)
        e = np.minimum(e, 0.9)
        mags[key] = m
        errs[f"e_{key}"] = e
    lim = np.full(n, "", dtype="<U1")
    lim[np.isfinite(mags["f444w"])] = "v"
    lim[(rng.random(n) < 0.01) & np.isfinite(mags["f444w"])] = "u"
    cls = np.array(CLASSES, dtype=object)[rng.choice(len(CLASSES), size=n, p=[0.55, 0.25, 0.15, 0.05])]
    good = rng.random(n) < 0.8
    teff = np.round(rng.uniform(2500.0, 30000.0, n), 1)
    ids = np.char.mod("SYN-%07d", np.arange(n))
    return SourceData(n=n, ids=ids, ra=ra, dec=dec, mags=mags, errs=errs, lim=lim,
                      cls=cls.astype(str), good=good, teff=teff)


def source_catalog(d: SourceData, key: str = "src", *, presets: bool = True) -> CatalogSpec:
    cols: dict[str, Any] = {"id": d.ids, "ra": d.ra, "dec": d.dec}
    specs: list[ColumnSpec] = []
    for mk, filt, _off, _alav in MAGS:
        cols[mk] = d.mags[mk]
        cols[f"e_{mk}"] = d.errs[f"e_{mk}"]
        specs.append(ColumnSpec(key=mk, role="mag", unit="mag", filter=filt, err=f"e_{mk}",
                                limit="f444w_lim" if mk == "f444w" else None, step=0.001,
                                label=mk.upper(), group="mags"))
        specs.append(ColumnSpec(key=f"e_{mk}", role="error", unit="mag"))
    cols["f444w_lim"] = d.lim
    specs.append(ColumnSpec(key="f444w_lim", role="limit"))
    cols["cls"] = d.cls
    specs.append(ColumnSpec(key="cls", role="category", label="Class", levels=list(CLASSES)))
    cols["good"] = d.good
    specs.append(ColumnSpec(key="good", role="flag", label="Good"))
    cols["teff"] = d.teff
    specs.append(ColumnSpec(key="teff", role="value", unit="K", label="Teff", fmt=".0f"))
    if d.n <= 2000:
        cols["note"] = np.array([f"note {i} ✓" for i in range(d.n)], dtype=str)
        specs.append(ColumnSpec(key="note", role="text", label="Note"))
    table = Table(cols)
    aliases = {syn_id(i): [alias_of(i), plus_alias_of(i)] for i in range(min(N_ALIASES, d.n))}
    virtual = [VirtualDim(key=c, label=c.upper().replace("-", " − "), unit="mag", group="colors")
               for c in COLOURS]
    pre: list[Preset] = []
    dens: list[DensitySpec] = []
    if presets:
        pre = [Preset("cmd", "CMD", "axes", {"x": CMD_X, "y": CMD_Y}),
               Preset("ccd", "CCD", "axes", {"x": "f182m-f410m", "y": "f405n-f410m"}),
               Preset("bright", "Bright", "selection", {"sel": "f410m<14"}),
               Preset("ysos", "YSOs", "filter", {"filters": {"cls": ["yso"]}})]
        if d.n > 3000:
            dens = [DensitySpec("cmd", CMD_X, CMD_Y, nx=256, ny=256)]
    return CatalogSpec(
        key=key, table=table, id="id", columns=specs, ra="ra", dec="dec", aliases=aliases,
        virtual=virtual, groups=[Group("mags", "Magnitudes", list(MAG_KEYS)),
                                 Group("colors", "Colours", list(COLOURS))],
        presets=pre, density=dens,
        manifold=ManifoldSpec(dims=list(MAG_KEYS) + list(COLOURS[:2])),
        title="Synthetic sources", caveats=["synthetic test data"])


def context_catalog(n: int, seed: int = SEED + 1) -> CatalogSpec:
    m = min(max(n // 2, 8), 50000)
    d = make_source_data(m, seed)
    table = Table({"id": np.char.mod("CTX-%07d", np.arange(m)), "ra": d.ra, "dec": d.dec,
                   "f182m": d.mags["f182m"], "f410m": d.mags["f410m"]})
    cols = [ColumnSpec("f182m", "mag", unit="mag", filter="JWST/NIRCam.F182M", step=0.001),
            ColumnSpec("f410m", "mag", unit="mag", filter="JWST/NIRCam.F410M", step=0.001)]
    return CatalogSpec(key="ctx", table=table, id="id", columns=cols, ra="ra", dec="dec",
                       context=True, title="Context field")


def vc_catalog(n: int, seed: int = SEED + 2) -> CatalogSpec:
    """40 stored mags + 80 virtual colours = 120 manifold dims (folded support 40)."""
    m = min(n, VC_N_MAX)
    rng = np.random.default_rng(seed)
    base = rng.normal(16.0, 1.2, m)
    slope = rng.normal(0.0, 0.3, m)
    table = Table({"id": np.char.mod("VC-%07d", np.arange(m))})
    cols: list[ColumnSpec] = []
    keys = [f"m{j:02d}" for j in range(VC_STORED)]
    for j, k in enumerate(keys):
        table[k] = np.round(base + slope * j / VC_STORED + rng.normal(0, 0.05, m), 3)
        cols.append(ColumnSpec(k, "mag", unit="mag", step=0.001, group="mags"))
    virt: list[VirtualDim] = []
    pairs = [(a, a + step) for step in (1, 2, 3, 4) for a in range(VC_STORED - step)]
    for a, b in pairs[:VC_DIMS - VC_STORED]:
        virt.append(VirtualDim(f"{keys[a]}-{keys[b]}", unit="mag", group="colors"))
    dims = keys + [v.key for v in virt]
    assert len(dims) == VC_DIMS and len(set(dims)) == VC_DIMS
    return CatalogSpec(key="vc", table=table, id="id", columns=cols, virtual=virt,
                       groups=[Group("mags", "Stored", keys), Group("colors", "Colours", [v.key for v in virt])],
                       manifold=ManifoldSpec(dims=dims), title="Virtual colours")


# ---------------------------------------------------------------------------
# records, images, physics, sky
# ---------------------------------------------------------------------------

SPEC_SEGMENTS = ((1.66, 3.07, "g235m"), (2.87, 5.10, "g395m"))
SPEC_NPIX = 400


def spectrum_for(i: int) -> Spectrum:
    rng = np.random.default_rng(SEED + 100 + i)
    waves, segs = [], []
    k = 0
    for w0, w1, name in SPEC_SEGMENTS:
        w = np.linspace(w0, w1, SPEC_NPIX)
        waves.append(w)
        segs.append((k, k + len(w), name))
        k += len(w)
    wave = np.concatenate(waves)
    cont = 1e-3 * (1.0 + i / N_RECORDS) * (wave / 3.0) ** (-1.0 - 0.5 * (i % 3))
    ice = 1.0 - 0.4 * np.exp(-0.5 * ((wave - 3.05) / 0.08) ** 2)
    line = 3e-3 * np.exp(-0.5 * ((wave - 4.0523) / 0.004) ** 2)
    flux = cont * ice + line + rng.normal(0, 2e-5, wave.size)
    err = np.full_like(flux, 2e-5)
    return Spectrum(wave=wave, channels={"flux": flux, "err": err, "flux_nobg": flux * 1.05},
                    units={"flux": "Jy"}, segments=segs, mask_runs=[(50, 60, 1)],
                    mask_bits={1: "DO_NOT_USE"}, meta={"grating": "g235m+g395m", "i": i})


def image2d_for(i: int) -> Image2D:
    rng = np.random.default_rng(SEED + 200 + i)
    wave = np.linspace(1.66, 3.07, 300)
    y = np.arange(-15, 16, dtype=float)
    prof = np.exp(-0.5 * (y / 2.0) ** 2)[:, None]
    data = prof * (1.0 + 0.5 * np.sin(wave * (3 + i)))[None, :] + rng.normal(0, 0.02, (y.size, wave.size))
    return Image2D(data=data, wave=wave, y=y, stretch="asinh")


#: records-index-relative product files of each ``prod`` record: (label, file name pattern, kind)
PRODUCT_FILES: tuple[tuple[str, str, str], ...] = (("x1d FITS", "products/{rid}_x1d.fits", "fits"),
                                                    ("Quicklook", "products/ql/{rid}.png", "png"))
#: records index of the ``prod`` dataset, relative to the portal root
PRODUCTS_INDEX = "data/prod/index.json"


def product_record(rid: str) -> dict:
    """``products/1`` record of ``rid``: relative URLs resolve against the records index URL."""
    return {"items": [{"label": label, "url": pat.format(rid=rid), "kind": kind, "size": 2880,
                       "auth": "public"} for label, pat, kind in PRODUCT_FILES]}


def product_hrefs(rid: str) -> list[str]:
    """Portal-root-relative paths the detail product links of ``rid`` point to."""
    base = PRODUCTS_INDEX.rsplit("/", 1)[0]
    return [f"{base}/{pat.format(rid=rid)}" for _l, pat, _k in PRODUCT_FILES]


def isochrone_spec() -> IsochroneSpec:
    tracks = []
    mass = np.logspace(-1, 1, 40)
    # Track mags are absolute (pk-isochrones/1): the client adds 5*log10(dist_pc/10), so with
    # dist_pc=8000 (mu = 14.5) f410m spans about 12.5-24.5 mag, on top of the synthetic catalogue.
    for label, age, shift in (("1 Myr", 1.0, 0.0), ("3 Myr", 3.0, 0.4), ("10 Myr", 10.0, 0.9)):
        absmag = 4.0 - 6.0 * np.log10(mass) + shift
        mags = {filt: np.round(absmag + off, 4) for _k, filt, off, _a in MAGS}
        tracks.append(Track(label=label, age_myr=age, dist_pc=8000.0, feh=0.0, mass=mass, mags=mags))
    return IsochroneSpec(tracks=tracks)


def hips_spec() -> HipsSpec:
    ra, dec = CENTER
    return HipsSpec(
        base=[{"id": "stub_k", "name": "Stub K", "url": "hips/stub_k", "default": True, "maxOrder": 6},
              {"id": "stub_rgb", "name": "Stub RGB", "url": "hips/stub_rgb", "maxOrder": 6}],
        overlays=[{"id": "stub_ov", "name": "Stub overlay", "url": "hips/stub_ov", "opacity": 0.5}],
        fits=[{"id": "stub_fits", "name": "Stub FITS", "url": "fits/stub.fits", "auth": "public",
               "ra": ra, "dec": dec, "size_deg": 0.1}],
        rungs=[{"id": f"r{k}", "label": f"{um} µm", "url": f"hips/rung{k}", "desc": f"Rung {k} at {um} µm",
                "r_um": um * 1.2, "g_um": um, "b_um": um * 0.8}
               for k, um in enumerate((1.15, 2.12, 4.44))],
        bookmarks=[{"name": "Cluster A", "ra": ra + 0.05, "dec": dec + 0.03, "fov": 0.05},
                   {"name": "Cluster B", "ra": ra - 0.04, "dec": dec - 0.02, "fov": 0.05}],
        target={"ra": ra, "dec": dec, "fov": 0.3})


def overlay_spec() -> OverlaySpec:
    ra, dec = CENTER
    return OverlaySpec(layers=[
        {"id": "fields", "name": "Fields", "space": "sky", "kind": "polygons",
         "style": {"color": "#e0a030", "width": 1.5}, "default": True,
         "items": [{"poly": [[ra - 0.05, dec - 0.05], [ra + 0.05, dec - 0.05], [ra + 0.05, dec + 0.05],
                             [ra - 0.05, dec + 0.05]], "label": "Field 1"}]},
        {"id": "rings", "name": "Rings", "space": "sky", "kind": "circles", "style": {"color": "#30a0e0"},
         "default": False, "items": [{"ra": ra + 0.01, "dec": dec, "r_arcsec": 20.0, "label": "R1"}]},
        {"id": "marks", "name": "Markers", "space": "sky", "kind": "markers", "style": {"shape": "cross"},
         "default": False, "items": [{"ra": ra + 0.02 * k, "dec": dec - 0.01 * k, "label": f"M{k}", "shape": "square"}
                                     for k in range(3)]},
        {"id": "reddening", "name": "Reddening vector", "space": "dim", "kind": "vector",
         "style": {"color": "#c03030"}, "default": True,
         "items": [{"x": CMD_X, "y": CMD_Y, "pts": [[0.5, 12.0], [1.5, 12.2]]}]},
        {"id": "locus", "name": "Locus", "space": "dim", "kind": "track", "style": {}, "default": False,
         "items": [{"dims": {"f182m-f410m": [0.0, 0.5, 1.0], "f410m": [10.0, 12.0, 14.0]}}]},
    ])


def narrative_spec(ids: list[str]) -> NarrativeSpec:
    ra, dec = CENTER
    beats = []
    plan = [(["fields"], ["rings", "marks"]), (["rings"], ["fields"]), (["marks"], ["rings"]),
            (["fields", "marks"], []), ([], ["fields", "rings", "marks"])]
    for k in range(N_BEATS):
        on, off = plan[k]
        view: dict[str, Any] = {"pane": "sky", "layers_on": on, "layers_off": off,
                                "sky": {"ra": ra + 0.01 * k, "dec": dec, "fov": 0.2 - 0.03 * k}}
        if k == 2 and ids:
            view["highlight"] = {"ds": "src", "ids": ids[:3]}
        beats.append({"id": f"b{k + 1}", "title": f"Beat {k + 1}",
                      "markdown": f"## Beat {k + 1}\n\nSynthetic narrative step **{k + 1}**.", "view": view})
    # b6: a highlight and no view (no sky, no layers), so the sky pane centres on the highlight
    # (narrative:highlight goto, docs/js.md); appended after the view beats, so b1..b5 are as before
    k = N_BEATS
    view6: dict[str, Any] = {"pane": "sky", "layers_on": [], "layers_off": []}
    if ids:
        view6["highlight"] = {"ds": "src", "ids": ids[:3]}
    beats.append({"id": f"b{k + 1}", "title": f"Beat {k + 1}",
                  "markdown": f"## Beat {k + 1}\n\nSynthetic narrative step **{k + 1}**: the highlight only.",
                  "view": view6})
    return NarrativeSpec(beats=beats)


#: the highlight-only beat of narrative_spec (a highlight, no view)
HIGHLIGHT_ONLY_BEAT = f"b{N_BEATS + 1}"

#: landing-card nouns per primary-dataset kind, as build.py _PortalBuild.COUNT_UNITS
COUNT_UNITS = {"catalog": ("source", "sources"), "images": ("image", "images"),
               "records": ("spectrum", "spectra")}


def portal_counts(specs: list[PageSpec], ds: dict[str, Any], manifests: dict[str, Any]) -> list[dict]:
    """portal.json ``counts`` (ARCHITECTURE.md section 4.2) for the synthetic pages, chosen as
    build.py ``_PortalBuild._page_count`` chooses them: a gallery's image set, a spectra
    page's record set, else the first non-context catalog; no entry for the landing page."""
    out = []
    for page in specs:
        if page.archetype == "landing":
            continue
        found: dict[str, tuple[str, int]] = {}
        for k in page.datasets:
            s = ds.get(k)
            if isinstance(s, CatalogSpec) and not s.context:
                kind, n = "catalog", (manifests.get(k) or {}).get("n")
            elif isinstance(s, ImageSetSpec):
                kind, n = "images", len(s.items)
            elif isinstance(s, RecordSetSpec):
                kind, n = "records", (manifests.get(k) or {}).get("n")
            else:
                continue
            if isinstance(n, int) and not isinstance(n, bool) and kind not in found:
                found[kind] = (k, n)
        prefer = {"gallery": ("images", "catalog", "records"),
                  "spectra": ("records", "catalog", "images")}.get(page.archetype, ("catalog", "images", "records"))
        for kind in prefer:
            if kind in found:
                k, n = found[kind]
                one, many = getattr(ds.get(k), "noun", None) or COUNT_UNITS[kind]
                out.append({"page": page.id, "ds": k, "kind": kind, "n": n, "unit": one, "units": many})
                break
    return out


README = """# Synthetic portal

Level 1 fixture for the portalkit e2e tests (ARCHITECTURE.md section 13).

| dataset | content |
|---|---|
| src | synthetic catalog |
| spec | 20 pks1 spectra |
"""


# ---------------------------------------------------------------------------
# datasets and pages
# ---------------------------------------------------------------------------

def datasets(n: int, seed: int = SEED, *, data: SourceData | None = None) -> tuple[dict[str, Any], SourceData]:
    d = data if data is not None else make_source_data(n, seed)
    nrec = min(N_RECORDS, n)
    rec_ids = [syn_id(i) for i in range(nrec)]
    img_ids = [syn_id(i) for i in range(min(N_IMAGES, n))]
    ds: dict[str, Any] = {
        "src": source_catalog(d),
        "ctx": context_catalog(n, seed + 1),
        "spec": RecordSetSpec(key="spec", kind="pks1", catalog="src", ids=rec_ids,
                              loader=lambda rid: spectrum_for(int(rid[4:]))),
        "s2d": RecordSetSpec(key="s2d", kind="pki1", catalog="src", ids=rec_ids,
                             loader=lambda rid: image2d_for(int(rid[4:])), enc={"data": "u8"}),
        "s2dpng": RecordSetSpec(key="s2dpng", kind="pki1", catalog="src", ids=rec_ids,
                                loader=lambda rid: None,
                                png=lambda rid: (f"img/s2d/{rid}.png", {"w": [1.66, 3.07], "y": [-15.0, 15.0]})),
        "prod": RecordSetSpec(key="prod", kind="json", catalog="src", ids=rec_ids, loader=product_record),
        "img": ImageSetSpec(
            key="img", catalog="src", roles=[("sed", "SED"), ("cutout", "Cutout")],
            facets=[("field", "Field"), ("kind", "Kind")],
            items=[ImageItem(id=iid, roles={"sed": f"img/sed/{iid}.png", "cutout": f"img/cut/{iid}.png"},
                             caption=f"Source {iid}", tags={"field": "A" if k % 2 else "B",
                                                            "kind": CLASSES[k % len(CLASSES)]},
                             w=160, h=120)
                   for k, iid in enumerate(img_ids)],
            presets=[Preset("field_a", "Field A", "filter", {"filters": {"field": ["A"]}})]),
        "tpl": TemplateSpec(),
        "flt": FilterSetSpec(keys=list(FILTER_KEYS)),
        "ext": ExtinctionSpec(),
        "iso": isochrone_spec(),
        "hips": hips_spec(),
        "ovl": overlay_spec(),
        "nar": narrative_spec(rec_ids),
        "readme": ReadmeSpec(markdown=README),
        "vc": vc_catalog(n, seed + 2),
    }
    return ds, d


PHYS = ["tpl", "flt", "ext", "iso"]


def cmd_overlay_ids() -> list[str]:
    """Dim overlay ids the ``cmd`` page's scatter lists: the isochrone track slugs and the
    extinction law vector (IMPLEMENTATION_NOTES items 22 and 88)."""
    return [config.overlay_slug(t.label) for t in isochrone_spec().tracks] + \
        [f"{config.overlay_slug(ExtinctionSpec().law)}_vector"]


def hist_layout() -> dict:
    hists = []
    for pid, expr, bins in HIST_PANES:
        leaf: dict[str, Any] = {"pane": "hist", "dataset": "src", "options": {"expr": expr}}
        if pid != "hist":
            leaf["id"] = pid
        if bins:
            leaf["options"]["bins"] = bins
        hists.append(leaf)
    return {"row": [{"pane": "list", "dataset": "src", "size": 250},
                    {"col": hists},
                    {"pane": "detail", "dataset": "src", "size": 380}]}


def cmd_layout() -> dict:
    return {"row": [{"pane": "list", "dataset": "src", "size": 250},
                    {"pane": "scatter", "dataset": "src",
                     "options": {"x": CMD_X, "y": CMD_Y, "overlays": cmd_overlay_ids()}},
                    {"pane": "detail", "dataset": "src", "size": 380, "options": {"products": "prod"}}]}


def page_specs() -> list[PageSpec]:
    def legacy(pid: str) -> LegacyRef | None:
        g = PAGES[pid][1]
        return LegacyRef(files=[f"legacy_{pid}.html"], grammar=list(g)) if g else None
    links = {"explorer": "explorer.html#id={id}", "spectra": "spectra.html#id={id}",
             "gallery": "gallery.html#id={id}", "manifold": "manifold.html#id={id}"}
    common = dict(portal=None)
    return [
        PageSpec(id="index", archetype="landing", title="Overview", datasets=["readme"], **common),
        PageSpec(id="explorer", archetype="explorer", title="Explorer",
                 datasets=["src", "spec", "s2d", "img", "ctx", "hips", "ovl", "readme"] + PHYS,
                 legacy=legacy("explorer"), links={k: v for k, v in links.items() if k != "explorer"}, **common),
        PageSpec(id="manifold", archetype="manifold", title="Manifold",
                 datasets=["src", "spec", "img", "hips", "ovl"] + PHYS, manifold=True,
                 legacy=legacy("manifold"), links={k: v for k, v in links.items() if k != "manifold"}, **common),
        PageSpec(id="gallery", archetype="gallery", title="Gallery",
                 datasets=["img", "src", "hips"] + PHYS, legacy=legacy("gallery"),
                 links={k: v for k, v in links.items() if k != "gallery"}, **common),
        PageSpec(id="spectra", archetype="spectra", title="Spectra",
                 datasets=["src", "spec", "s2d", "img"] + PHYS, legacy=legacy("spectra"),
                 links={k: v for k, v in links.items() if k != "spectra"}, **common),
        PageSpec(id="spectra_png", archetype="spectra", title="Spectra (PNG 2D)",
                 datasets=["src", "spec", "s2dpng"] + PHYS, **common),
        PageSpec(id="wavelength", archetype="wavelength", title="Wavelength tour",
                 datasets=["hips", "src", "ovl"], legacy=legacy("wavelength"), **common),
        PageSpec(id="narrative", archetype="narrative", title="Narrative",
                 datasets=["nar", "hips", "ovl", "src"], legacy=legacy("narrative"), **common),
        PageSpec(id="vcolor", archetype="manifold", title="Virtual colours", datasets=["vc"],
                 manifold=True, **common),
        PageSpec(id="hist", archetype="explorer", title="Histograms", datasets=["src"],
                 layout=hist_layout(), **common),
        PageSpec(id="cmd", archetype="explorer", title="CMD", datasets=["src", "ctx", "prod"] + PHYS,
                 layout=cmd_layout(), links={k: v for k, v in links.items() if k != "explorer"}, **common),
    ]


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

@dataclass
class PageInfo:
    id: str
    file: str
    archetype: str
    caps: list[str]
    flags: list[str]
    disabled: dict
    panes: list[dict]
    html_bytes: int
    legacy: list[str]


@dataclass
class SyntheticPortal:
    root: Path
    n: int
    pages: dict[str, PageInfo]
    data: SourceData | None = None
    stored_ids: list[str] = field(default_factory=list)
    first_paint_bytes: int = 0

    def url_path(self, page: str) -> str:
        return f"n{self.n}/{self.pages[page].file}"

    def input_row(self, sid: str) -> int:
        return int(sid.split("-")[1])


def _hash_tree(h: Any, base: Path) -> None:
    for p in sorted(base.rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts or p.suffix in (".pyc", ".pyo"):
            continue
        h.update(f"{base.name}/{p.relative_to(base).as_posix()}".encode("utf-8") + b"\0")
        h.update(p.read_bytes())


def _source_hash(*, web: bool = True) -> str:
    """Hash of everything a build depends on (IMPLEMENTATION_NOTES item 94): this module,
    every file of the ``portalkit`` package (resolver, config, render, bundle and physics
    writers, schema, package data) and, with ``web``, every file of ``web/`` (the shell
    embeds the boot module path and vendored asset list, and the runtime files are served
    through the ``_pk`` symlink). The shared physics resources do not depend on ``web/`` and
    use ``web=False``."""
    h = hashlib.sha256()
    h.update(Path(__file__).read_bytes())
    _hash_tree(h, REPO / "portalkit")
    if web:
        _hash_tree(h, WEB)
    return h.hexdigest()[:16]


def _symlink(link: Path, target: Path) -> None:
    if link.is_symlink() or link.exists():
        if link.is_symlink() and os.readlink(link) == str(target):
            return
        if link.is_symlink():
            link.unlink()
        else:
            raise FileExistsError(f"{link} exists and is not a symlink")
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, link)


def write_shared_res(res_dir: Path) -> dict[str, str]:
    """Write templates / filters / extinction / features once into ``res_dir``."""
    from portalkit import physics
    stamp = res_dir / ".refs.json"
    src = _source_hash(web=False)
    if stamp.exists():
        st = json.loads(stamp.read_text())
        refs = st.get("refs") if isinstance(st.get("refs"), dict) else None
        if refs is not None and st.get("src") == src and all((res_dir / p).exists() for p in refs.values()):
            return refs
    res_dir.mkdir(parents=True, exist_ok=True)
    refs = physics.write_shared(BundleWriter(res_dir, "", Ledger()),
                                filter_spec=FilterSetSpec(keys=list(FILTER_KEYS)))
    atomic_write(stamp, json.dumps({"src": src, "refs": refs}, sort_keys=True).encode())
    return refs


def _write_hips_stubs(proot: Path, hips: HipsSpec) -> None:
    urls = [b["url"] for b in hips.base] + [o["url"] for o in hips.overlays] + [r["url"] for r in hips.rungs or []]
    for u in urls:
        p = proot / u / "properties"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"obs_title = {u}\nhips_order = 3\nhips_tile_format = png\nhips_frame = equatorial\n")
    f = proot / "fits" / "stub.fits"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"SIMPLE  =                    T" + b" " * 50 + b"END" + b" " * 77 + b" " * (2880 - 160))


def resolve_page(page: PageSpec, ds: dict[str, Any], manifests: dict[str, Any] | None = None):
    """``caps.resolve`` as ``build.py`` calls it (every shared resource attachable,
    ``resources=None``). The shared resources the page then gets by reference are
    :func:`page_resources` (``caps.attachable_resources``, the rule ``build.py _page_refs``
    applies; IMPLEMENTATION_NOTES 110), so the result equals the caps ``config.assemble``
    keeps on the built page."""
    return capsmod.resolve(page, ds, manifests or {})


def page_resources(page: PageSpec, ds: dict[str, Any], result: Any,
                   manifests: dict[str, Any] | None = None) -> tuple[str, ...]:
    """Shared resource kinds attached to ``page`` by reference given its resolved caps."""
    return capsmod.attachable_resources(page, ds, result, manifests)


def page_refs(page: PageSpec, ds: dict[str, Any], result: Any, res_urls: dict[str, str],
              manifests: dict[str, Any] | None = None) -> dict[str, dict]:
    """DatasetRefs of the shared resources attached to ``page`` (``res_urls``: kind ->
    ``{res}<path>``), named as ``build.py`` names them (``caps.resource_refs``)."""
    return capsmod.resource_refs(page_resources(page, ds, result, manifests), res_urls, taken=ds)


def pane_list(layout: dict) -> list[dict]:
    """Pane leaves with the ids the runtime gives them (layout.js: ``leaf.id``, else the
    pane type, deduplicated with ``-2``, ``-3``...)."""
    out: list[dict] = []
    seen: set[str] = set()
    for p in capsmod.iter_panes(layout):
        pid = p.get("id") or p.get("pane")
        if pid in seen:
            k = 2
            while f"{pid}-{k}" in seen:
                k += 1
            pid = f"{pid}-{k}"
        seen.add(pid)
        out.append({"id": pid, "pane": p.get("pane"), "dataset": p.get("dataset"),
                    "options": p.get("options", {}) or {}})
    return out


def build_portal(root: str | Path, n: int, *, seed: int = SEED, force: bool = False,
                 res_dir: str | Path | None = None, keep_data: bool = True) -> SyntheticPortal:
    """Build (or reuse) the synthetic portal for ``n`` rows under ``<root>/n<n>/``.

    Re-uses an existing build when its stamp matches ``n``, the seed and the source hash of
    this module, the whole ``portalkit`` package and the ``web/`` tree (:func:`_source_hash`),
    so a change to the resolver, config, shell, writers or runtime rebuilds the fixture.
    ``res_dir`` defaults to ``<root>/res`` (shared across sizes).
    """
    root = Path(root).resolve()
    proot = root / f"n{n}"
    res = Path(res_dir).resolve() if res_dir is not None else root / "res"
    stamp_path = proot / ".synthetic.json"
    stamp_key = {"src": _source_hash(), "n": n, "seed": seed}
    if not force and stamp_path.exists():
        st = json.loads(stamp_path.read_text())
        if st.get("key") == stamp_key:
            pages = {k: PageInfo(**v) for k, v in st["pages"].items()}
            data = make_source_data(n, seed) if keep_data else None
            return SyntheticPortal(root=proot, n=n, pages=pages, data=data,
                                   stored_ids=st.get("stored_ids", []),
                                   first_paint_bytes=st.get("first_paint_bytes", 0))
    proot.mkdir(parents=True, exist_ok=True)
    res_refs_raw = write_shared_res(res)
    _symlink(proot / "res", res)
    _symlink(proot / "_pk" / f"v{VERSION}", WEB)

    ds, d = datasets(n, seed)
    ledger = Ledger()
    manifests: dict[str, Any] = {}
    catres = {}
    for k in ("src", "ctx", "vc"):
        r = catalog_writer.write(ds[k], BundleWriter(proot, f"data/{k}", ledger, k), build=BUILD,
                                 seed=seed, ledger=ledger)
        manifests[k] = r.manifest
        catres[k] = r
    src = catres["src"]
    for k in ("spec", "s2d", "s2dpng", "prod"):
        r = records_writer.write(ds[k], BundleWriter(proot, f"data/{k}", ledger, k), order=src.row_of,
                                 ledger=ledger)
        manifests[k] = r.index
    r = images_writer.write(ds["img"], BundleWriter(proot, "data/img", ledger, "img"), rows=src.row_of,
                            ledger=ledger)
    manifests["img"] = r.doc
    # image files
    for k, it in enumerate(ds["img"].items):
        for role, url in it.roles.items():
            p = proot / url
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(_plot_png(seed + 1000 * (role == "cutout") + k))
    for rid in ds["s2dpng"].ids:
        p = proot / "img" / "s2d" / f"{rid}.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        img = image2d_for(int(rid[4:])).data
        a = np.clip(255 * (img - img.min()) / max(np.ptp(img), 1e-9), 0, 255).astype(np.uint8)
        p.write_bytes(png_bytes(a))
    # product files the prod records link to (relative to data/prod/index.json)
    for rid in ds["prod"].ids:
        for rel in product_hrefs(rid):
            p = proot / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(_plot_png(seed + 2000 + int(rid[4:])) if rel.endswith(".png") else
                          b"SIMPLE  =                    T" + b" " * 50 + b"END" + b" " * 77 + b" " * (2880 - 160))
    # isochrones, hips, overlays, narrative
    iso_name, iso_bytes = config.isochrones_file(ds["iso"])
    atomic_write(proot / "data" / iso_name, iso_bytes)
    hips_doc = config.hips_document(ds["hips"])
    atomic_write(proot / "data" / "hips.json", dumps_json(hips_doc))
    manifests["hips"] = hips_doc
    atomic_write(proot / "data" / "overlays.json", dumps_json(config.overlays_document(ds["ovl"])))
    atomic_write(proot / "data" / "narrative.json", dumps_json(config.narrative_document(ds["nar"])))
    _write_hips_stubs(proot, ds["hips"])

    res_refs = {k: "{res}" + v for k, v in res_refs_raw.items()}
    pages: dict[str, PageInfo] = {}
    portal_pages = []
    budget = {"first_paint_bytes": int(ledger.first_paint())}
    for page in page_specs():
        result = resolve_page(page, ds, manifests)
        # shared resources the page's caps use are attached by reference, as build.py
        # _page_refs does (one helper, IMPLEMENTATION_NOTES 110)
        refs = page_refs(page, ds, result, res_refs, manifests)
        doc = config.assemble(page, ds, manifests, result, portal=PORTAL, refs=refs,
                              res_refs=res_refs, vendor_assets=True, budget=budget)
        fname = f"{page.id}.html"
        nbytes = shell.write(proot / fname, doc)
        panes = pane_list(doc["layout"])
        pages[page.id] = PageInfo(id=page.id, file=fname, archetype=page.archetype,
                                  caps=list(doc["caps"]["enabled"]), flags=list(result.flags),
                                  disabled=dict(doc["caps"].get("disabled", {})), panes=panes,
                                  html_bytes=nbytes, legacy=list(page.legacy.grammar) if page.legacy else [])
        portal_pages.append({"id": page.id, "title": page.title, "file": fname, "archetype": page.archetype})
    portal_doc = {"schema": "pk-portal/1", **PORTAL, "build": BUILD, "pk": VERSION, "pages": portal_pages,
                  "search": [{"ds": "src", "page": "explorer", "manifest": "data/src/manifest.json"}],
                  "counts": portal_counts(page_specs(), ds, manifests)}
    errs = validate_portal(portal_doc)
    if errs:
        raise ValueError(f"portal.json invalid: {errs}")
    atomic_write(proot / "portal.json", dumps_json(portal_doc))
    st = {"key": stamp_key, "pages": {k: v.__dict__ for k, v in pages.items()},
          "stored_ids": list(src.ids) if n <= 50000 else [],
          "first_paint_bytes": budget["first_paint_bytes"]}
    atomic_write(stamp_path, json.dumps(st, ensure_ascii=False).encode("utf-8"))
    return SyntheticPortal(root=proot, n=n, pages=pages, data=d if keep_data else None,
                           stored_ids=st["stored_ids"], first_paint_bytes=budget["first_paint_bytes"])


# ---------------------------------------------------------------------------
# flag realisation for the parity tests
# ---------------------------------------------------------------------------

def specs_for_flags(flags: list[str] | set[str], archetype: str, *, n: int = 8
                    ) -> tuple[PageSpec, dict[str, Any], dict[str, Any]]:
    """A (page, datasets, manifests) triple whose resolved flags include ``flags``.

    Used by the parity test: it rebuilds each port-table row from its data flags and
    checks the resolver output against the legacy capability inventory.
    """
    flags = set(flags)
    ds: dict[str, Any] = {"readme": ReadmeSpec(markdown="# synthetic")}
    page_ds: list[str] = ["readme"]
    need_cat = bool(flags & {"cat", "cats", "flags", "num2", "mags2", "num3", "mags3f", "skycat",
                             "dens", "multi", "search"}) or archetype in ("explorer", "manifold", "spectra")
    nrows = max(n, 3001) if "dens" in flags else n
    if need_cat:
        rows: dict[str, Any] = {"id": np.char.mod("SYN-%07d", np.arange(nrows))}
        cols: list[ColumnSpec] = []
        rng = np.random.default_rng(1)
        nmag = 3 if flags & {"mags3f", "num3"} else (2 if flags & {"mags2", "num2"} else 0)
        if archetype == "manifold":
            nmag = max(nmag, 3)
        for k in MAGS[:nmag]:
            rows[k[0]] = np.round(rng.normal(15, 1, nrows), 3)
            cols.append(ColumnSpec(k[0], "mag", filter=k[1] if flags & {"mags3f", "mags2"} or archetype == "manifold" else None))
        if "cats" in flags:
            rows["cls"] = np.array(["a", "b"] * (nrows // 2) + ["a"] * (nrows % 2))
            cols.append(ColumnSpec("cls", "category", levels=["a", "b"]))
        if "flags" in flags:
            rows["good"] = np.ones(nrows, dtype=bool)
            cols.append(ColumnSpec("good", "flag"))
        pos = bool(flags & {"sky", "skycat"})
        if pos:
            rows["ra"] = CENTER[0] + 0.001 * np.arange(nrows)
            rows["dec"] = np.full(nrows, CENTER[1])
        if "img" in flags and "sedimg" in flags and archetype != "gallery":
            pass
        ds["src"] = CatalogSpec(key="src", table=Table(rows), id="id", columns=cols,
                                ra="ra" if pos else None, dec="dec" if pos else None)
        if archetype != "landing":
            page_ds.insert(0, "src")
    # a HiPS document wherever the legacy page drew a sky map: hips_layer_stacking needs one
    # (IMPLEMENTATION_NOTES items 89 and 111). Filters, templates, extinction and features are
    # shared resources that caps.resolve counts as attachable by default (item 110).
    if "sky" in flags or archetype == "wavelength" or "fits" in flags:
        h = hips_spec()
        if "fits" not in flags:
            h.fits = []
        ds["hips"] = h
        page_ds.append("hips")
    if "img" in flags or archetype == "gallery":
        roles = {"sed": "s.png"} if "sedimg" in flags else {"plot": "p.png"}
        ds["img"] = ImageSetSpec(key="img", items=[ImageItem(id="SYN-0000000", roles=roles)],
                                 roles=[(k, k) for k in roles], catalog="src" if "src" in ds else None)
        page_ds.append("img")
    if "spec" in flags:
        ds["spec"] = RecordSetSpec(key="spec", kind="pks1", catalog="src" if "src" in ds else "src",
                                   ids=["SYN-0000000"], loader=lambda i: None)
        if "src" not in ds:
            ds["src"] = CatalogSpec(key="src", table=Table({"id": ["SYN-0000000"]}), id="id", columns=[])
        page_ds.append("spec")
    if "spec2d" in flags:
        ds["s2d"] = RecordSetSpec(key="s2d", kind="pki1", catalog="src", ids=["SYN-0000000"],
                                  loader=lambda i: None)
        page_ds.append("s2d")
    if "iso" in flags:
        ds["iso"] = isochrone_spec()
        page_ds.append("iso")
    if archetype == "narrative":
        ds["nar"] = narrative_spec([])
        page_ds.append("nar")
        if "hips" not in ds and "src" not in ds:
            ds["hips"] = hips_spec()
            page_ds.append("hips")
    if archetype == "spectra" and "spec" not in ds:
        ds["spec"] = RecordSetSpec(key="spec", kind="pks1", catalog="src", ids=["SYN-0000000"],
                                   loader=lambda i: None)
        page_ds.append("spec")
    layout = None
    if "annot" in flags:
        layout = {"row": [{"pane": "gallery", "dataset": "img", "options": {"annotate": True}},
                          {"col": [{"pane": "sky", "dataset": "src"}, {"pane": "detail", "dataset": "src"}]}]}
    page = PageSpec(id="p", archetype=archetype, title="P", datasets=page_ds, layout=layout,
                    manifold=archetype == "manifold")
    return page, ds, {}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out", type=Path, help="fixture root (never a live portal directory)")
    ap.add_argument("--n", type=int, nargs="+", default=[8, 2000])
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    for n in a.n:
        p = build_portal(a.out, n, force=a.force, keep_data=False)
        print(f"n={n}: {p.root} pages={sorted(p.pages)} first_paint={p.first_paint_bytes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
