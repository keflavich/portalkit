"""Sickle (JWST 3958) and Brick2221 MIRI F1500W SED portals (WP26; ARCHITECTURE.md 14.8).

Legacy generators in ``/orange/adamginsburg/jwst/brick/analysis``: ``Sickle_MIRI_SEDPortal.py``
(map), ``Sickle_MIRI_SEDPortal_Manifold.py`` (manifold) and ``Brick2221_F1500W_SEDPortal.py``
(F1500W explorer). Two portals (sites/sickle_brick.toml): ``sickle`` and ``brick2221``.

* Tier A reads the legacy payloads: ``srcdata`` of the map and of the F1500W explorer,
  ``CLASS_COLOR``/``SURVEYS`` of the map and the manifold ``DATA`` (groups, presets with the
  PCA weights).
* Tier B calls ``collect_sources`` of both generators (SED PNG check against the served PNG
  directory) and the manifold ``PRESETS``/``GROUPS``/``pca_frame``; memoised in ctx.cache.

Both tiers share the converters. Per-source rows become a catalogue (magnitudes with filter
curves, SESHAT class + probabilities, galactic l/b, every colour as a virtual dim); the Sickle
catalogue carries the manifold (F444W added to the legacy dims; legacy ``c_a_b`` colours map to
virtual ``m_a-m_b``). Magnitudes convert to flux with the project Vega zero points of
``FILTER_VEGA_ZP_JY`` (the MIRI values differ from the registry; FilterSetSpec.extra). The
templates are extended to 26 um (TemplateSpec.extend_to_um, B_nu continuum), and the page state
normalizes them on the NIRCam bands only, so the MIRI excess does not set the scale. The F2550W
FITS overlay URL and the SED PNG URLs come from the exposure map (no relative path escapes).
"""

from __future__ import annotations

import contextlib
import io
import itertools
import json
import math
import os
import re
import warnings
from pathlib import Path

import numpy as np

SICKLE_DIR = Path("/orange/adamginsburg/jwst/sickle/sed_portal")
SICKLE_PNG = Path("/orange/adamginsburg/jwst/sickle/sed_figures_f1500w_all")
BRICK_PNG = Path("/orange/adamginsburg/web/public/jwst/brick_2221/sed_figures_f1500w_all")
TOP100_PNG = Path("/orange/adamginsburg/web/public/jwst/brick_2221/sed_figures_top100_f1500w")
F2550W = Path("/orange/adamginsburg/jwst/sickle/images/brick_f2550w_i2d.fits")
SICKLE_URL = "https://data.rc.ufl.edu/pub/adamginsburg/jwst/sickle/sed_portal/unified/"
BRICK_URL = "https://data.rc.ufl.edu/pub/adamginsburg/jwst/brick_2221/unified/sickle_brick/"
MODULES = ("Sickle_MIRI_SEDPortal", "Sickle_MIRI_SEDPortal_Manifold", "Brick2221_F1500W_SEDPortal")
PNG_RE = re.compile(r"sed_rank\d{3}_F1500W[\d.]+\.png")
CMD = ("m_f770w-m_f1500w", "m_f1500w")          # legacy cmdsvg / ccdsvg axes
CCD = ("m_f770w-m_f1130w", "m_f1130w-m_f1500w")
OTHER_COLOR = "#9aa6bd"                         # legacy DEFAULT_CLASS_COLOR
TOP = {"top100": ["yes"]}
CLASS_NOTE = ("SESHAT classification is provisional: unseeded RNG draws move ~6% of sources "
              "between YSO and FS from run to run.")


def _fid(f: str) -> str:
    """Filters-registry key of a legacy filter name (NIRCam < 5 um, MIRI above)."""
    n = int(re.fullmatch(r"f(\d{3,4})[wmn]", f.lower()).group(1))
    return f"JWST/{'MIRI' if n >= 560 else 'NIRCam'}.{f.upper()}"


def _const(text: str, name: str):
    m = re.search(rf"\bvar\s+{name}\s*=\s*", text)
    if m is None:
        raise ValueError(f"no {name} in the legacy page")
    return json.JSONDecoder().raw_decode(text, m.end())[0]


def _slug(name: str, taken: set) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "layer"
    k, i = s, 2
    while k in taken:
        k, i = f"{s}_{i}", i + 1
    taken.add(k)
    return k


def _arr(v) -> np.ndarray:
    return np.array([np.nan if x is None else float(x) for x in v], dtype=float)


def _quiet(fn, *a):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a)


def _pub(path: Path) -> str | None:
    """Public URL of a served file (exposure map; the directory is classified)."""
    from portalkit.deploy.exposure import classify
    e = classify(path.parent)
    return e.public_urls[0].rstrip("/") + "/" + path.name if e.public_urls else None


class SickleBrickAdapter:
    name = "sickle_brick"
    tiers = ("A", "B")

    # -- raw data per tier ----------------------------------------------------------------
    def _tier_a(self, ctx) -> dict:
        from portalkit.legacy import extract
        smap, sman, bgal = (SICKLE_DIR / "sickle_miri_sed_map.html", SICKLE_DIR / "sickle_miri_sed_manifold.html",
                            BRICK_PNG / "gallery.html")
        for p in (smap, sman, bgal):
            ctx.inputs.add(p)
        text = smap.read_text(encoding="utf-8")
        br = extract.script_json(bgal, "srcdata")
        mani = extract.payload(sman.read_text(encoding="utf-8"))
        return {"sk": extract.script_json(smap, "srcdata"), "br": br,
                "zp": {p["filter"].lower(): float(p["zp_jy"]) for r in br for p in r["Photometry"] if p.get("zp_jy")},
                "colors": _const(text, "CLASS_COLOR"), "surveys": _const(text, "SURVEYS"),
                "groups": [list(g) for g in mani["groups"]], "presets": mani["presets"], "pca": None}

    def _tier_b(self, ctx) -> dict:
        S, M, B = (ctx.legacy.module(m) for m in MODULES)
        sk_png, br_png = sorted(SICKLE_PNG.glob("sed_rank*.png")), sorted(BRICK_PNG.glob("sed_rank*.png"))
        sk = ctx.cache.memo(
            "sickle_brick_sk_v1", lambda: {"rows": _quiet(S.collect_sources, S.DEFAULT_CSV, str(SICKLE_PNG))[0]},
            inputs=[S.__file__, S.DEFAULT_CSV] + sk_png)
        br = ctx.cache.memo(
            "sickle_brick_br_v1",
            lambda: {"rows": _quiet(B.collect_sources, B.DEFAULT_CSV, B.DEFAULT_FITS_SUMMARY, str(BRICK_PNG))[0]},
            inputs=[B.__file__, B.DEFAULT_CSV, B.DEFAULT_FITS_SUMMARY] + br_png)
        ctx.inputs.add(M.__file__)
        return {"sk": sk["rows"], "br": br["rows"], "zp": dict(B.FILTER_VEGA_ZP_JY),
                "colors": {c: S.CLASS_COLOR.get(c, S.DEFAULT_CLASS_COLOR) for c in S.CLASS_COLOR},
                "surveys": [list(s) for s in S.SURVEYS], "groups": [list(g) for g in M.GROUPS],
                "presets": [dict(p) for p in M.PRESETS], "pca": (M.pca_frame, list(M.PCA_KEYS))}

    # -- datasets -------------------------------------------------------------------------
    def datasets(self, ctx):
        from portalkit.spec import (ExtinctionSpec, FilterSetSpec, ReadmeSpec, RecordSetSpec, TemplateSpec,
                                    TierUnavailable)
        from portalkit.physics.filters import filter_entry
        if ctx.tier not in self.tiers:
            raise TierUnavailable(f"sickle_brick implements tiers A and B, not {ctx.tier}")
        raw = self._tier_a(ctx) if ctx.tier == "A" else self._tier_b(ctx)
        n = ctx.limit
        sk, br = (sorted((self._norm(r) for r in raw[k]), key=lambda r: r["rank"]) for k in ("sk", "br"))
        raw["n"] = {"sk": len(sk), "br": len(br)}
        sk, br = (rows if n is None else rows[:n] for rows in (sk, br))
        self._raw, self._png = raw, {r["name"]: r["png"] for r in sk + br}
        ds = {"sickle": self._cat("sickle", sk, raw, "Sickle MIRI SED sources", manifold=True),
              "brick": self._cat("brick", br, raw, "Brick2221 F1500W sources")}
        ds["sk_img"], ds["br_img"] = self._img(ds["sickle"], SICKLE_PNG), self._img(ds["brick"], BRICK_PNG, TOP100_PNG)
        ds["fits"] = self._fits(ctx)
        ds["sk_hips"], ds["br_hips"] = self._hips(raw, "sk", ds["fits"]), self._hips(raw, "br", ds["fits"])
        for key, img, cat, name in (("sk_prod", "sk_img", "sickle", SICKLE_DIR / "sickle_miri_seds"),
                                    ("br_prod", "br_img", "brick", BRICK_PNG / "brick2221_f1500w_seds")):
            ds[key] = RecordSetSpec(key, "json", cat, [it.id for it in ds[img].items],
                                    self._products(ds[img], name, ds["fits"]))
        flt = sorted({c.filter for c in ds["sickle"].columns + ds["brick"].columns if c.filter})
        extra = {}
        for f in flt:
            zp = raw["zp"].get(f.split(".")[-1].lower())
            if zp and abs(zp / filter_entry(f)[0]["zp_jy"] - 1) > 1e-4:
                extra[f] = {"zp_jy": float(zp)}
        self._zp = {f.split(".")[-1]: e["zp_jy"] for f, e in extra.items()}
        ds.update(tpl26=TemplateSpec(extend_to_um=26.0), ext=ExtinctionSpec(), flt=FilterSetSpec(keys=flt, extra=extra))
        ds["sk_readme"] = ReadmeSpec(markdown=self._readme(ctx, ds, "sickle"))
        ds["br_readme"] = ReadmeSpec(markdown=self._readme(ctx, ds, "brick"))
        return ds

    @staticmethod
    def _norm(r: dict) -> dict:
        return {"name": str(r["Name"]), "rank": int(r["Rank"]), "ra": float(r["RA"]), "dec": float(r["Dec"]),
                "cls": str(r["SeshatClass"]), "prob": r.get("SeshatProb"), "probs": dict(r.get("SeshatProbs") or {}),
                "phot": {p["filter"].lower(): (p["mag_vega"], p.get("emag_vega"), float(p["wavelength_um"]))
                         for p in r["Photometry"]},
                "png": os.path.basename(str(r["SEDPNG"])), "sep": r.get("SeshatSepArcsec")}

    def _cat(self, key, rows, raw, title, manifold=False):
        import astropy.units as u
        from astropy.coordinates import SkyCoord
        from astropy.table import Table
        from portalkit.spec import CatalogSpec, ColumnSpec, Group, Preset, VirtualDim
        wl = {f: p[2] for r in rows for f, p in r["phot"].items()}
        filters = sorted(wl, key=wl.get)
        classes = list(dict.fromkeys(c for r in rows for c in r["probs"]))
        present = list(dict.fromkeys(r["cls"] for r in rows))
        colors = raw["colors"]
        levels = [c for c in colors if c in present] + [c for c in present if c not in colors]
        gal = SkyCoord([r["ra"] for r in rows] * u.deg, [r["dec"] for r in rows] * u.deg).galactic
        t = Table({"id": [r["name"] for r in rows], "ra": [r["ra"] for r in rows], "dec": [r["dec"] for r in rows],
                   "alias": [str(r["rank"]) for r in rows], "rank": np.array([r["rank"] for r in rows], dtype=float),
                   "cls": [r["cls"] for r in rows], "prob": _arr(r["prob"] for r in rows)})
        cols = [ColumnSpec("id", "id"),
                ColumnSpec("rank", "value", label="Rank (F1500W, brightest first)", group="meta", fmt=".0f", step=1),
                ColumnSpec("cls", "category", label="SESHAT class", levels=levels, note=CLASS_NOTE,
                           colors={c: colors.get(c, OTHER_COLOR) for c in levels}),
                ColumnSpec("prob", "value", label="P(assigned class)", group="class", fmt=".3f")]
        for c in classes:
            t[f"p_{c}"] = _arr(r["probs"].get(c) for r in rows)
            cols.append(ColumnSpec(f"p_{c}", "value", label=f"P({c})", group="class", fmt=".3f"))
        for f in filters:
            t[f"m_{f}"] = _arr(r["phot"].get(f, (None,))[0] for r in rows)
            t[f"e_{f}"] = _arr((r["phot"].get(f) or (None, None))[1] for r in rows)
            cols += [ColumnSpec(f"m_{f}", "mag", label=f.upper(), unit="mag (Vega)", filter=_fid(f), err=f"e_{f}",
                                step=0.001, invert=True, group="phot"),
                     ColumnSpec(f"e_{f}", "error", label=f"{f.upper()} error", unit="mag", group="err")]
        t["gl"], t["gb"] = np.round(gal.l.wrap_at(180 * u.deg).deg, 6), np.round(gal.b.deg, 6)
        cols += [ColumnSpec("gl", "value", label="Galactic l", unit="deg", group="pos", invert=True),
                 ColumnSpec("gb", "value", label="Galactic b", unit="deg", group="pos")]
        if any(r["sep"] is not None for r in rows) or any(r["cls"] == "unclassified" for r in rows):
            t["sep"] = _arr(r["sep"] for r in rows)
            cols.append(ColumnSpec("sep", "value", label="SESHAT match separation", unit="arcsec", group="class",
                                   note="sky match of the F1500W source to the SESHAT table (<= 0.5 arcsec)"))
        pairs = [(a, b) for a, b in itertools.combinations(filters, 2)]
        virt = [VirtualDim(f"m_{a}-m_{b}", label=f"{a.upper()} - {b.upper()}", unit="mag", group="color")
                for a, b in pairs]
        kmap = {f"c_{a}_{b}": f"m_{a}-m_{b}" for a, b in pairs}
        kmap.update({f"SeshatProb_{c}": f"p_{c}" for c in classes})
        presets, frames = [], {}
        for p in raw["presets"]:
            if p.get("x") is None:
                continue
            x, y = ({kmap.get(k, k): w for k, w in p[ax].items()} for ax in ("x", "y"))
            if len(x) == len(y) == 1:
                (xk,), (yk,) = x, y
                presets.append(Preset(p["id"], p["name"], "axes",
                                      {"x": xk, "y": yk, "note": p.get("note", ""), "code": p.get("code", ""),
                                       "color": "cls"}))
            frames[p["id"]] = (x, y, p)
        presets += [Preset("map_cmd", "CMD: F1500W against F770W - F1500W", "axes", {"x": CMD[0], "y": CMD[1]}),
                    Preset("map_ccd", "CCD: F770W - F1130W against F1130W - F1500W", "axes",
                           {"x": CCD[0], "y": CCD[1]})]
        groups = [Group(g, lab, []) for g, lab in raw["groups"]]
        spec = CatalogSpec(key=key, table=t, id="id", columns=cols, ra="ra", dec="dec", aliases="alias",
                           virtual=virt, presets=presets, title=f"{title} ({len(t):,})",
                           caveats=[CLASS_NOTE, "Magnitudes convert to flux with the project Vega zero points "
                                    "(FILTER_VEGA_ZP_JY of the legacy generators)."])
        if manifold:
            self._manifold(spec, raw, frames, filters, classes, groups)
        return spec

    def _manifold(self, spec, raw, frames, filters, classes, groups):
        """Manifold dims (legacy order, F444W added), legacy presets as named frames."""
        from portalkit.bundle.codec import col_stats
        from portalkit.legacy.manifold_payload import preset_frame
        from portalkit.spec import ManifoldSpec, Preset
        t = spec.table
        dims = ([f"m_{f}" for f in filters] + [v.key for v in spec.virtual] + [f"p_{c}" for c in classes]
                + ["gl", "gb"])
        std, factor = {}, {}
        for d in dims:
            a, _, b = d.partition("-")
            v = np.asarray(t[a], float) - (np.asarray(t[b], float) if b else 0.0)
            fin = v[np.isfinite(v)]
            if fin.size < 5:
                continue
            c = float(np.median(fin))
            s = float(1.4826 * np.median(np.abs(fin - c))) or float(np.std(fin)) or 1.0
            s = round(-s if d.startswith("m_") and not b or d == "gl" else s, 6)
            std[d] = {"key": d, "center": round(c, 6), "scale": s,
                      "v": [None if not np.isfinite(x) else float(x) for x in v]}
            factor[d] = col_stats(fin)["scale"] / s
        dims = [d for d in dims if d in std]
        if len(dims) < 2:
            raise ValueError(f"Sickle manifold needs >= 2 dims with >= 5 finite values; {len(t)} sources give "
                             f"{len(dims)} (use --limit 5 or more)")
        for g in groups:
            g.dims = [d for d in dims if (g.key == "phot" and d.startswith("m_") and "-" not in d)
                      or (g.key == "color" and "-" in d) or (g.key == "class" and d.startswith("p_"))
                      or (g.key == "pos" and d in ("gl", "gb"))]
        named = {}
        for pid, (x, y, p) in frames.items():
            if set(x) | set(y) <= set(dims):
                # single-dim frames keep exact +-1 weights (pairOfFrame) but the legacy sign
                # (negative legacy scale on magnitudes and gl: bright up, l to the left)
                sign = {d: math.copysign(1.0, f) for d, f in factor.items()}
                named[pid] = preset_frame(x, y, dims, factor if len(x) > 1 or len(y) > 1 else sign)
        pre = next((p for p in raw["presets"] if p["id"] == "pca"), None)
        if pre is not None:
            note = pre.get("note", "")
            if raw["pca"] is not None:
                fn, keys = raw["pca"]
                px, py, npca, frac = _quiet(fn, [std[k] for k in keys if k in std], keys)
                note += f" {npca} sources; PC1 carries {frac[0]:.0%} of the variance, PC2 {frac[1]:.0%}."
            else:
                px, py = pre["x"], pre["y"]
            if set(px) | set(py) <= set(dims):
                named["pca"] = preset_frame(px, py, dims, factor)
                spec.presets.append(Preset("pca", pre["name"], "frame", {"dims": dims, "F": named["pca"], "note": note,
                                                                         "code": pre.get("code", "PCA")}))
        spec.groups = [g for g in groups if g.dims]
        default = next(p["id"] for p in raw["presets"] if p["id"] in named)
        spec.manifold = ManifoldSpec(dims=dims, frames=named, default_frame=default)

    def _img(self, cat, png_dir: Path, top_dir: Path | None = None):
        from portalkit.spec import ImageItem, ImageSetSpec, Preset
        base = _pub(png_dir / "x.png")
        if base is None:
            raise ValueError(f"{png_dir} is not publicly served; the public portal cannot show its SED PNGs")
        base = base[:-len("x.png")]
        top = {p.name for p in top_dir.iterdir() if p.is_file() and PNG_RE.fullmatch(p.name)} if top_dir else set()
        top_base = _pub(top_dir / "x.png")[:-len("x.png")] if top else ""
        t = cat.table
        items = []
        for i, png in enumerate(self._png[str(x)] for x in t["id"]):
            roles = {"sed": base + png}
            tags = {"class": str(t["cls"][i])}
            if top_dir is not None:
                tags["top100"] = "yes" if png in top else "no"
                if png in top:
                    roles["top100"] = top_base + png
            items.append(ImageItem(id=str(t["id"][i]), roles=roles, tags=tags,
                                   caption=f"#{int(t['rank'][i])}  {t['cls'][i]}  F1500W {t['m_f1500w'][i]:.2f}"))
        roles = [("sed", "SED + cutouts")] + ([("top100", "SED, earlier top-100 render")] if top else [])
        facets = [("class", "SESHAT class")] + ([("top100", "In the legacy top-100 gallery")] if top else [])
        presets = [Preset("top100", f"Legacy top-{len(top)} gallery", "filter", {"facets": TOP})] if top else []
        return ImageSetSpec(key="sk_img" if top_dir is None else "br_img", items=items, roles=roles,
                            facets=facets, presets=presets, catalog=cat.key, noun=("SED figure", "SED figures"))

    def _fits(self, ctx) -> list[dict]:
        """The F2550W mosaic as a FITS overlay (public URL through the exposure map)."""
        from astropy.io import fits
        from astropy.wcs import FITSFixedWarning, WCS
        ctx.inputs.add(F2550W)
        url = _pub(F2550W)
        if url is None:
            self._fits_note = "The F2550W FITS overlay is omitted: the mosaic is not publicly served."
            return []
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FITSFixedWarning)
            h = fits.getheader(F2550W, 1)
            w = WCS(h)
            c = w.pixel_to_world(h["NAXIS1"] / 2, h["NAXIS2"] / 2)
            pix = max(float(q.to_value("deg")) for q in w.proj_plane_pixel_scales())
        self._fits_note = ""
        return [{"id": "f2550w", "name": "MIRI F2550W mosaic (FITS)", "url": url, "auth": "public",
                 "ra": round(float(c.ra.deg), 6), "dec": round(float(c.dec.deg), 6),
                 "size_deg": round(pix * max(h["NAXIS1"], h["NAXIS2"]), 5), "size": os.path.getsize(F2550W)}]

    def _hips(self, raw, which: str, fits_list):
        """Legacy SURVEYS (first default) + the FITS overlay; view as write_html (full sample)."""
        from portalkit.spec import HipsSpec
        taken: set = set()
        base = [{"id": _slug(s[0], taken), "name": s[0], "url": s[1], "default": bool(s[2])} for s in raw["surveys"]]
        ra, dec = (np.array([float(r[k]) for r in raw[which]]) for k in ("RA", "Dec"))
        c = [float(np.mean(ra)), float(np.mean(dec))]
        span = max((ra.max() - ra.min()) * np.cos(np.radians(c[1])), dec.max() - dec.min())
        return HipsSpec(base=base, fits=[{k: v for k, v in f.items() if k != "size"} for f in fits_list],
                        target={"ra": round(c[0], 6), "dec": round(c[1], 6), "fov": round(max(0.01, 1.5 * span), 5)})

    @staticmethod
    def _products(img, stem: Path, fits_list):
        cats = [(f"{stem.name}.{ext}", _pub(stem.with_suffix(f".{ext}"))) for ext in ("ecsv", "json")]
        by = {it.id: it for it in img.items}

        def load(name):
            it = by.get(name)
            if it is None:
                return None
            items = [{"label": "SED + cutouts (PNG)", "url": it.roles["sed"], "kind": "png", "auth": "public",
                      "group": "figures"}]
            items += [{"label": f"catalogue {n}", "url": u, "kind": "link", "auth": "public", "group": "catalogue"}
                      for n, u in cats if u]
            items += [{"label": f["name"], "url": f["url"], "kind": "fits", "auth": "public", "size": f["size"],
                       "group": "images"} for f in fits_list]
            return {"schema": "products/1", "items": items}
        return load

    def _readme(self, ctx, ds, which: str) -> str:
        cat = ds[which]
        t, n_all = cat.table, self._raw["n"]["sk" if which == "sickle" else "br"]
        lim = "" if ctx.limit is None else f" Fixture build: the first {len(t)} of {n_all:,} sources."
        mags = [c for c in cat.columns if c.role == "mag"]
        lv = next(c for c in cat.columns if c.key == "cls").levels
        cls = ", ".join(f"{c} {int(np.sum(t['cls'] == c))}" for c in lv)
        zp = ", ".join(f"{k} {v:g} Jy" for k, v in self._zp.items())
        fits_txt = self._fits_note or ("The MIRI F2550W mosaic is a FITS overlay on the sky map, loaded on "
                                       f"demand ({ds['fits'][0]['size'] / 1e6:.0f} MB).")
        if which == "sickle":
            head = (f"# Sickle MIRI SED portal\n\n{len(t):,} SESHAT-classified MIRI F1500W sources of the Sickle "
                    f"field (JWST 3958) with NIRCam + MIRI photometry in {len(mags)} filters.{lim}\n\n## Pages\n\n"
                    "- **SED map**: list, sky map, detail with SESHAT probability bars, live SED and CMD/CCD.\n"
                    f"- **Manifold**: {len(cat.manifold.dims)} standardized properties (magnitudes, every colour, "
                    "class probabilities, galactic l/b) with the legacy views.\n"
                    f"- The [Brick2221 F1500W portal]({BRICK_URL}) holds the sky-matched Brick2221 sample.\n")
        else:
            n_un = int(np.sum(t["cls"] == "unclassified"))
            head = (f"# Brick2221 MIRI F1500W SED portal\n\n{len(t):,} MIRI F1500W sources of the Brick field "
                    f"(JWST 2221), ranked by F1500W and sky-matched to the SESHAT table; {n_un} "
                    f"{'has' if n_un == 1 else 'have'} no match and keep F1500W only.{lim}\n\n## Pages\n\n"
                    "- **F1500W explorer**: list, sky map, detail with probability bars, live SED with stellar "
                    "templates extended to 26 um, A_V slider with R_V 3.1/5.5, dust/ice feature markers, CMD/CCD.\n"
                    f"- **SED gallery**: {len(ds['br_img'].items):,} SED figures with class and top-100 facets.\n"
                    f"- The [Sickle portal]({SICKLE_URL}) holds the Sickle sample and its manifold.\n")
        return (head + f"\n## Notes\n\n- SESHAT classes: {cls}. {CLASS_NOTE}\n"
                "- Stellar templates beyond 5.3 um continue as a blackbody (B_nu) scaled to the last native "
                "sample. The template scale uses the NIRCam bands only, so the MIRI excess does not set it; "
                "a source without NIRCam photometry shows no template.\n"
                + (f"- MIRI fluxes use the project Vega zero points ({zp}), which differ from the SVO values.\n"
                   if zp else "") + f"- {fits_txt}\n")

    # -- pages ----------------------------------------------------------------------------
    def _explorer(self, ds, key, img, hips, prod, bars):
        fits = [f["id"] for f in ds[hips].fits]
        return {"row": [
            {"pane": "list", "dataset": key, "size": 300},
            {"col": [{"pane": "sky", "dataset": key, "options": {"hips": hips, "fits": fits}},
                     {"row": [{"pane": "scatter", "id": "cmd", "dataset": key,
                               "options": {"x": CMD[0], "y": CMD[1], "color": "cls"}},
                              {"pane": "scatter", "id": "ccd", "dataset": key,
                               "options": {"x": CCD[0], "y": CCD[1], "color": "cls"}}], "size": 320}]},
            {"col": [{"pane": "detail", "dataset": key, "options": {"bars": bars, "images": img, "products": prod}},
                     {"pane": "sed", "dataset": key, "options": {"models": ["template", "blackbody"]}}],
             "size": 420}]}

    def pages(self, ctx, ds):
        from portalkit.spec import PageSpec
        phys = ["tpl26", "flt", "ext"]
        sk, br = ds["sickle"], ds["brick"]
        out = []
        for key, portal, pre in (("sickle", "sickle", "sk"), ("brick", "brick2221", "br")):
            cat, img, hips, prod = ds[key], f"{pre}_img", f"{pre}_hips", f"{pre}_prod"
            bars = [c.key for c in cat.columns if c.key.startswith("p_")]
            nircam = [c.label for c in cat.columns if c.role == "mag" and "NIRCam" in (c.filter or "")]
            state = {"template": {"key": "giant", "grid": "medium", "scale": "auto", "bands": nircam},
                     "color": {"by": "cls"}}
            common = dict(portal=portal, subtitle=f"{len(cat.table):,} sources")
            readme = ds[f"{pre}_readme"].markdown
            if key == "sickle":
                cards = [{"page": "sickle_miri_sed_map", "text": f"{len(sk.table):,} sources: list, sky, SESHAT "
                          "probability bars, live SED, CMD/CCD."},
                         {"page": "sickle_miri_sed_manifold",
                          "text": f"{len(sk.manifold.dims)}-dim manifold of {len(sk.table):,} sources."},
                         {"href": BRICK_URL, "title": "Brick2221 F1500W portal", "auth": "public",
                          "text": f"{len(br.table):,} Brick2221 F1500W sources with extended templates."}]
                to = {"map": "sickle_miri_sed_map.html#id={id}", "manifold": "sickle_miri_sed_manifold.html#id={id}"}
                pages = [PageSpec(id="sickle_miri_sed_map", archetype="explorer", title="Sickle SED map",
                                  datasets=[key, img, prod, hips] + phys, state=state,
                                  links={"manifold": to["manifold"]},
                                  layout=self._explorer(ds, key, img, hips, prod, bars), **common),
                         PageSpec(id="sickle_miri_sed_manifold", archetype="manifold", title="Sickle SED manifold",
                                  manifold=True, datasets=[key, img, hips] + phys, links={"map": to["map"]},
                                  state={**state, "frame:manifold": {"preset": sk.manifold.default_frame}}, **common)]
            else:
                cards = [{"page": "f1500w_explorer", "text": f"{len(br.table):,} sources: live SED with templates "
                          "to 26 um, R_V 3.1/5.5 and feature markers."},
                         {"page": "f1500w_gallery", "text": f"{len(ds[img].items):,} SED figures with a top-100 "
                          "preset."},
                         {"href": SICKLE_URL, "title": "Sickle MIRI SED portal", "auth": "public",
                          "text": f"{len(sk.table):,} Sickle sources, SED map and manifold."}]
                to = {"explorer": "f1500w_explorer.html#id={id}", "gallery": "f1500w_gallery.html#id={id}"}
                gallery = {"row": [
                    {"pane": "gallery", "dataset": img, "options": {"images": img, "sort": ["rank", "id", "m_f1500w"]}},
                    {"col": [{"pane": "sky", "dataset": key, "options": {"hips": hips}},
                             {"pane": "scatter", "dataset": key, "options": {"x": CMD[0], "y": CMD[1], "color": "cls"}},
                             {"pane": "detail", "dataset": key, "options": {"bars": bars, "images": img}}],
                     "size": 400}]}
                pages = [PageSpec(id="f1500w_explorer", archetype="explorer", title="Brick2221 F1500W explorer",
                                  datasets=[key, img, prod, hips] + phys, state=state, links={"gallery": to["gallery"]},
                                  layout=self._explorer(ds, key, img, hips, prod, bars), **common),
                         PageSpec(id="f1500w_gallery", archetype="gallery", title="Brick2221 F1500W SED gallery",
                                  datasets=[key, img, hips], layout=gallery, links={"explorer": to["explorer"]},
                                  state={"color": {"by": "cls"}}, **common)]
            landing = {"pane": "landing", "options": {"cards": cards, "counts": True, "search": True}}
            out.append(PageSpec(id="index", archetype="landing", title=readme.splitlines()[0].lstrip("# "),
                                datasets=[f"{pre}_readme"], portal=portal, layout=landing))
            out += pages
        return out


ADAPTER = SickleBrickAdapter()
