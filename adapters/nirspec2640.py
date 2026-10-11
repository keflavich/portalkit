"""JWST NIRSpec program 2640 (Westerlund 2 MSA sample) family adapter (WP25; ARCHITECTURE.md
sections 9 and 14.4, wave 3).

Legacy generators: ``NIRSpec_2640_*.py`` in ``/orange/adamginsburg/jwst/brick/analysis``. They
write to ``/orange/adamginsburg/jwst/spectra/nirspec_2640`` (the originals, read here); the
pages are copied by hand to the public tree ``web/public/jwst/wd2/nirspec_2640`` (the legacy
dir of this public portal; figures are served from there).

* Tier A reads legacy payloads: map ``srcdata``/``BASE``/``OVERLAY``, colour-colour
  ``SOURCES``, red-excess ``DIAGRAMS``/``SOURCES``, PV/SED ``SOURCES``, the public manifold
  ``DATA`` (public tree only) and ``specdata/<name>.json``. Deviation from section 9.2 (Tier A
  runs no science code): no payload holds the 2D spectra, the Wd2 catalogue, the isochrones
  or the ice columns (the icecolumns page was never generated), so every tier stitches the
  s2d products, reads the catalogue FITS, interpolates MIST and reads the ice-line ECSV.
  Merged legacy spectra are split into gratings with the x1d wavelength grids.
* Tier B imports ``NIRSpec_2640_AladinMap`` (``collect_sources``/``attach_figures``/
  ``BASE``/``OVERLAY``) and ``NIRSpec_2640_Manifold`` (``build``/``standardize``/
  ``pca_frame``), reads the synthphot / line-detection ECSV tables and the x1d/s2d FITS.

Fixed legacy bugs: the dead ``nirspec_2640_icecolumns.html`` link now has a page; titles are
specific per page (the public manifold was titled "Wd2 Ice Manifold" although it leads with
photometry and brown-dwarf fits); product links point at the secure archive (the public
``../mastDownload`` links resolved nowhere) and carry a login badge.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import quote

import numpy as np

SRC_DIR = Path("/orange/adamginsburg/jwst/spectra/nirspec_2640")
PUB_DIR = Path("/orange/adamginsburg/web/public/jwst/wd2/nirspec_2640")
PUB = "https://data.rc.ufl.edu/pub/adamginsburg/jwst/wd2/nirspec_2640/"
JWST_ROOT = "/orange/adamginsburg/jwst/"
SECURE = "https://data.rc.ufl.edu/secure/adamginsburg/jwst/"
WD2_CAT = ("/orange/adamginsburg/jwst/wd2/catalogs/basic_merged_indivexp_photometry_tables_merged_"
           "resbgsub_m8_dedup.fits")
MODULES = ("NIRSpec_2640_AladinMap", "NIRSpec_2640_Manifold")
CDS_HIPS = {"P/2MASS/color": "https://alasky.cds.unistra.fr/2MASS/Color/",
            "P/DSS2/color": "https://alasky.cds.unistra.fr/DSS/DSSColor/"}
DIST_PC, ISO_AGES_MYR = 4160.0, (2.0,)       # NIRSpec_2640_Manifold isochrone (log age 6.3)
SPECIES = ("H2O", "CO2", "CO", "OCN", "13CO2", "OCS")
ICE_PAIRS = (("H2O", "CO2"), ("CO2", "CO"), ("CO2", "13CO2"), ("CO", "OCN"), ("CO", "OCS"))
LIM = {"v": "", "lo": "l", "up": "u", None: ""}
FIG_ROLES = {"spectrum": "Spectrum + filters", "nb": "Narrow-band filters", "bb": "Broad-band filters",
             "icefit": "Ice fit"}
PNG_ROLES = {"all": "All filters", "nb": "Narrow-band filters", "bb": "Broad-band filters"}
FIG_FIELDS = {"spectrum": "SpectrumPNG", "all": "SpectrumPNG", "nb": "PNGNB", "bb": "PNGBB", "icefit": "IceFitPNG"}
CARDS = (("nirspec_2640_map", "MSA shutters on the Wd2 mosaics, spectra, 2D spectra and FITS products."),
         ("nirspec_2640_colorcolor", "Catalogue vs synthetic NIRCam colours on the Wd2 density, BD split."),
         ("nirspec_2640_specviewer", "PRISM + G140M/G395M spectra with ice-band zooms and 2D spectra."),
         ("nirspec_2640_red_excess", "Offsets from the G23 reddening vector against cluster distance."),
         ("nirspec_2640_manifold", "Photometry, SED-fit, ice and line properties with limits."),
         ("nirspec_2640_icecolumns", "Ice columns with errors and limits (mostly limits in this sample)."),
         ("nirspec_2640_pvgallery", "Position-wavelength images (L3)."),
         ("nirspec_2640_sedgallery", "SED + cutout figures."),
         ("gallery_2640", "Spectrum PNGs: all, narrow-band and broad-band filter overlays."))
#: sister portal (WP24): page ids of sites/wd2.toml under its unified target URL
WD2_PORTAL = PUB + "unified/wd2/"
WD2_CARDS = (("wd2_manifold", "Wd2 photometry manifold", "The full Wd2 NIRCam catalogue manifold."),
             ("wd2_colorcolor", "Wd2 colour-colour", "Catalogue colour-colour diagrams."),
             ("map_ccd", "Wd2 sky + CCD", "Sky map linked to a CCD and a CMD."),
             ("wd2_wavelength_explorer", "Wd2 wavelength explorer", "RGB composites across 1-11 um."))
LINKS = (("map", "nirspec_2640_map"), ("spectrum", "nirspec_2640_specviewer"), ("ice", "nirspec_2640_icecolumns"),
         ("pv", "nirspec_2640_pvgallery"), ("sed", "nirspec_2640_sedgallery"), ("redx", "nirspec_2640_red_excess"),
         ("colorcolor", "nirspec_2640_colorcolor"))


def _fid(b: str) -> str:
    return f"JWST/NIRCam.{b.upper()}"


def _num(x):
    """float, or None for missing / masked / non-finite values."""
    if x is None or np.ma.is_masked(x):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError, np.ma.MaskError):
        return None
    return v if np.isfinite(v) else None


def _surl(path: str) -> str:
    p = os.path.normpath(os.path.abspath(path))
    if not p.startswith(JWST_ROOT):
        raise ValueError(f"{p} is not under {JWST_ROOT} (no secure URL)")
    return SECURE + quote(p[len(JWST_ROOT):])


def _pub(rel: str | None) -> str | None:
    """Public URL of a figure (relative to the legacy dir) when the public copy exists."""
    return PUB + quote(rel) if rel and (PUB_DIR / rel).is_file() else None


def _mast(obs: str) -> str:
    q = {"service": "Mast.Caom.Filtered", "format": "extjs", "params": {"columns": "*", "filters": [
        {"paramName": "proposal_id", "values": ["2640"]},
        {"paramName": "obs_id", "values": [], "freeText": f"jw02640-o{obs}%"}]}}
    return "https://mast.stsci.edu/portal/Mashup/Clients/Mast/Portal.html?searchQuery=" + quote(json.dumps(q), safe="")


def _sep_arcmin(ra, dec, ra0=None, dec0=None):
    """Great-circle distance [arcmin] from the Wd2 centre (haversine)."""
    ra0, dec0 = WD2_CENTER if ra0 is None else (ra0, dec0)
    a1, d1, a0, d0 = (np.radians(np.asarray(v, float)) for v in (ra, dec, ra0, dec0))
    h = np.sin((d1 - d0) / 2) ** 2 + np.cos(d1) * np.cos(d0) * np.sin((a1 - a0) / 2) ** 2
    return np.degrees(2 * np.arcsin(np.sqrt(np.clip(h, 0, 1)))) * 60.0


def _pay(ctx, path, *names) -> dict:
    from portalkit.legacy import extract
    ctx.inputs.add(path)
    return {e["name"]: e["value"] for e in extract.payloads(Path(path), names=names)}


class NIRSpec2640Adapter:
    name = "nirspec2640"
    tiers = ("A", "B")

    # -- raw data per tier ----------------------------------------------------------------
    def _common(self, ctx) -> dict:
        from astropy.table import Table
        sv = _pay(ctx, SRC_DIR / "nirspec_2640_specviewer.html", "BANDS", "ICEMARKS")
        out = {"bands": sv["BANDS"], "icemarks": sv["ICEMARKS"]}
        for k in ("slits", "ice_lines"):
            ctx.inputs.add(SRC_DIR / f"nirspec_2640_{k}.ecsv")
            out[k] = Table.read(SRC_DIR / f"nirspec_2640_{k}.ecsv")
        out["slits"] = {str(r["SrcName"]): (str(r["shutter_corners"]), str(r["footprint"])) for r in out["slits"]}
        out["ice"] = {str(r["SrcName"]): {sp: ice_kind(r, sp) for sp in SPECIES} for r in out.pop("ice_lines")}
        return out

    def _tier_a(self, ctx) -> dict:
        m = _pay(ctx, SRC_DIR / "nirspec_2640_map.html", "srcdata", "BASE", "OVERLAY")
        cc = _pay(ctx, SRC_DIR / "nirspec_2640_colorcolor.html", "SOURCES")["SOURCES"]
        rx = _pay(ctx, SRC_DIR / "nirspec_2640_red_excess.html", "DIAGRAMS", "SOURCES")
        sg = _pay(ctx, SRC_DIR / "nirspec_2640_sedgallery.html", "SOURCES", "SEDREL")
        phot = {r["name"]: {**{k: v for k, v in r.items() if k.startswith(("cat_", "synth_", "hst_"))},
                            "cat_matched": r.get("matched"), "cat_sep": r.get("sep"), "hst_sep": r.get("hst_sep")}
                for r in cc}
        return {"rows": m["srcdata"], "base": m["BASE"], "overlay": m["OVERLAY"], "phot": phot,
                "slope": {d["key"]: float(d["slope"]) for d in rx["DIAGRAMS"]},
                "redx": {r["name"]: r for r in rx["SOURCES"]},
                "mani": _pay(ctx, PUB_DIR / "nirspec_2640_manifold.html", "DATA")["DATA"],
                "pv": {r["name"]: r.get("pv") for r in _pay(ctx, SRC_DIR / "nirspec_2640_pvgallery.html",
                                                            "SOURCES")["SOURCES"]},
                "sed": {r["name"]: f"{sg['SEDREL']}/{r['fig']}" for r in sg["SOURCES"] if r.get("fig")}}

    def _tier_b(self, ctx) -> dict:
        from astropy.table import Table
        am, mf = (ctx.legacy.module(m) for m in MODULES)
        figdirs = [str(SRC_DIR / d) for d in ("pngs", "linefits", "pv")]
        rows = ctx.cache.memo("n2640_rows_v1", lambda: am.attach_figures(
            am.collect_sources(am.DEFAULT_DATADIR), str(SRC_DIR), str(SRC_DIR / "pngs"), str(SRC_DIR / "linefits"),
            am.DEFAULT_DATADIR), inputs=[am.DEFAULT_DATADIR] + figdirs)
        ecsv = [SRC_DIR / f"nirspec_2640_{k}.ecsv" for k in ("synthphot", "line_detections", "ice_lines", "sources")]
        for p in ecsv:
            ctx.inputs.add(p)

        def mani():                                # NIRSpec_2640_Manifold.main() without the HTML
            names, dims, stars = mf.build(str(SRC_DIR))
            sdims = mf.standardize(names, dims)
            keys, presets = {d["key"] for d in sdims}, []
            for p in mf.PRESETS:
                p = dict(p)
                if p["id"] == "pca":
                    p["x"], p["y"], _, _ = mf.pca_frame(sdims, mf.PCA_KEYS)
                if set(p["x"]) | set(p["y"]) <= keys:
                    presets.append(p)
            return {"stars": stars, "dims": sdims, "groups": [list(g) for g in mf.GROUPS], "presets": presets,
                    "categories": mf.CATEGORIES}
        sp = Table.read(ecsv[0])
        phot = {str(r["SrcName"]): {**{k: _num(r[k]) for k in sp.colnames if k.startswith(("cat_f", "synth_f", "hst_f"))},
                                    "cat_matched": bool(r["cat_matched"]), "cat_sep": _num(r["cat_sep_arcsec"]),
                                    "hst_matched": bool(r["hst_matched"]), "hst_sep": _num(r["hst_sep_arcsec"])}
                for r in sp}
        slope = {k: vector_slope(a, b) for k, (a, b), _ in Y_OPTIONS}
        redx = {}
        for n, p in phot.items():                  # NIRSpec_2640_RedExcess.build (sources)
            x = mag_of(p, X_FILTERS[0]) - mag_of(p, X_FILTERS[1])
            d = {"x": _num(x)}
            for k, (a, b), _ in Y_OPTIONS:
                y = mag_of(p, a) - mag_of(p, b)
                ok = np.isfinite(x) and np.isfinite(y)
                d[f"y_{k}"], d[f"p_{k}"] = (_num(y), _num(perp_offset(x, y, slope[k]))) if ok else (None, None)
            redx[n] = d
        ld = Table.read(ecsv[1])
        lines: dict = {}
        for r in ld[np.asarray(ld["detected"], bool) & np.asarray(ld["significant"], bool)]:
            lines.setdefault(str(r["SrcName"]), []).append(
                {"n": str(r["name"]), "w": float(r["wl"]), "k": "e" if str(r["kind"]) == "emission" else "a",
                 "snr": _num(r["snr"])})
        sed = {r["SrcName"]: f"sed_figures/sed_{int(r['SourceID']):06d}.png" for r in rows
               if (SRC_DIR / f"sed_figures/sed_{int(r['SourceID']):06d}.png").is_file()}
        return {"rows": rows, "base": [list(s) for s in am.BASE], "overlay": [list(s) for s in am.OVERLAY],
                "phot": phot, "slope": slope, "redx": redx, "lines": lines, "sed": sed,
                "pv": {r["SrcName"]: r.get("PVPNG") for r in rows},
                "mani": ctx.cache.memo("n2640_mani_v1", mani, inputs=ecsv + [mf.ISOCHRONE_PATH])}

    # -- datasets -------------------------------------------------------------------------
    def datasets(self, ctx):
        from portalkit.spec import ExtinctionSpec, FilterSetSpec, RecordSetSpec, TemplateSpec, TierUnavailable
        if ctx.tier not in self.tiers:
            raise TierUnavailable(f"nirspec2640 implements tiers A and B, not {ctx.tier}")
        raw = self._tier_a(ctx) if ctx.tier == "A" else self._tier_b(ctx)
        raw.update(self._common(ctx))
        rows = sorted((r for r in raw["rows"] if r.get("SrcName")), key=lambda r: (int(r["SourceID"]), r["SrcName"]))
        raw["rows"] = rows if ctx.limit is None else rows[:int(ctx.limit)]
        raw["by"] = {r["SrcName"]: r for r in raw["rows"]}
        ids = list(raw["by"])
        rec = lambda fn: (lambda n: fn(raw, ctx, n))  # noqa: E731  (loaders bound to this call's data)
        cat = self._catalog(ctx)
        ds = {"src": self._src(raw), "bg": self._bg(ctx, cat), "redx": self._redx(ctx, cat, raw),
              "iso": self._iso(ctx),
              "spec": RecordSetSpec("spec", "pks1", "src", ids, rec(self._spectrum), enc={"flux": "f32", "err": "f32"}),
              "s2d": RecordSetSpec("s2d", "pki1", "src", ids, rec(self._image2d), enc={"data": "u8"}),
              "prod": RecordSetSpec("prod", "json", "src", ids, rec(self._products)),
              **self._images(raw), "hips": self._hips(raw), **self._overlays(raw)}
        ds.update(tpl=TemplateSpec(), flt=FilterSetSpec(keys=[_fid(f) for f in CAT_FILTERS + SYNTH_ONLY_FILTERS]),
                  ext=ExtinctionSpec(), readme=self._readme(ctx, raw, ds))
        return ds

    def _src(self, raw):
        from astropy.table import MaskedColumn
        from portalkit.legacy.manifold_payload import convert
        from portalkit.spec import Preset, VirtualDim, catalog_from_rows, join
        keep = [i for i, s in enumerate(raw["mani"]["stars"]) if s["n"] in raw["by"]]
        out = []
        for r in raw["rows"]:
            n = r["SrcName"]
            ph, rx = raw["phot"].get(n, {}), raw["redx"].get(n, {})
            d = {"name": n, "ra": r["RA"], "dec": r["Dec"], "SlitID": r["SlitID"], "SourceID": str(r["SourceID"]),
                 **{k: r[k] for k in ("Target", "Program", "Observation", "Visit", "VisitGroup", "Shutters")},
                 "Gratings": r["Gratings"], "Stellarity": _num(r.get("Stellarity")),
                 "cat_matched": ph.get("cat_matched"), "cat_sep": _num(ph.get("cat_sep")),
                 "hst_matched": ph.get("hst_matched"), "hst_sep": _num(ph.get("hst_sep"))}
            if not keep:                           # else the manifold join supplies r_wd2 (same centre)
                d["r_wd2"] = round(float(_sep_arcmin(r["RA"], r["Dec"])), 4)
            d.update({f: _num(ph.get(f"cat_{f}")) for f in CAT_FILTERS})
            d.update({f"s_{f}": _num(ph.get(f"synth_{f}")) for f in CAT_FILTERS + SYNTH_ONLY_FILTERS})
            d.update({f"hst_{f}": _num(ph.get(f"hst_{f}")) for f in HST_FILTERS})
            d["redx_x"] = _num(rx.get("x"))
            for k, _, _ in Y_OPTIONS:
                d[f"redx_y_{k}"], d[f"p_{k}"] = _num(rx.get(f"y_{k}")), _num(rx.get(f"p_{k}"))
            for sp in SPECIES:                     # value, error, limit code (lower = saturated)
                v, e, k = raw["ice"].get(n, {}).get(sp, (None, None, None))
                d[f"col_{sp}"], d[f"e_col_{sp}"], d[f"l_col_{sp}"] = _num(v), _num(e), LIM[k]
            out.append(d)
        roles = {"SlitID": "text", "SourceID": "text", "Target": "text", "cat_matched": "flag", "hst_matched": "flag",
                 **{k: "category" for k in ("Program", "Observation", "Visit", "VisitGroup", "Gratings", "Shutters")}}
        cols = {"cat_sep": {"unit": "arcsec", "label": "catalogue match separation"},
                "hst_sep": {"unit": "arcsec", "label": "HST match separation"},
                "r_wd2": {"unit": "arcmin", "label": "distance from the Wd2 centre", "group": "pos"},
                "redx_x": {"unit": "mag", "group": "redx", "label": "F115W - F200W (catalogue, else synthetic)"}}
        for f in CAT_FILTERS:
            roles[f] = "mag"
            cols[f] = {"filter": _fid(f), "unit": "mag", "step": 0.001, "group": "cat", "label": f.upper()}
        for f in CAT_FILTERS + SYNTH_ONLY_FILTERS:
            roles[f"s_{f}"] = "mag"
            cols[f"s_{f}"] = {"filter": _fid(f), "unit": "mag", "step": 0.001, "group": "synth",
                              "label": f"{f.upper()} (synthetic)"}
        for f in HST_FILTERS:
            roles[f"hst_{f}"] = "mag"
            cols[f"hst_{f}"] = {"unit": "mag", "step": 0.001, "group": "hst", "label": f"{f.upper()} (HST/WFPC2)"}
        for k, _, lab in Y_OPTIONS:
            cols[f"redx_y_{k}"] = {"unit": "mag", "group": "redx", "label": f"{lab} (catalogue, else synthetic)"}
            cols[f"p_{k}"] = {"unit": "mag", "group": "redx", "label": f"offset from the reddening vector ({lab})"}
        for sp in SPECIES:
            roles[f"l_col_{sp}"], roles[f"e_col_{sp}"] = "limit", "error"
            cols[f"col_{sp}"] = {"enc": "f32", "limit": f"l_col_{sp}", "err": f"e_col_{sp}", "group": "ice",
                                 "exact_gpu": False, "unit": "cm^-2", "label": f"N({sp}) ice"}
            cols[f"e_col_{sp}"] = {"enc": "f32", "exact_gpu": False}
        base = catalog_from_rows(out, "name", "ra", "dec", roles=roles, key="src", columns=cols,
                                 title=f"NIRSpec 2640 sources ({len(out)})",
                                 aliases={r["SrcName"]: [r["SlitID"], r["Stem"]] for r in raw["rows"]})
        for k in sorted((set(cols) | {"Stellarity"}) & set(base.table.colnames)):
            if base.table[k].dtype.kind not in "fiu":  # all missing (e.g. HST F555W): keep it numeric
                base.table[k] = MaskedColumn(np.full(len(out), np.nan), mask=np.ones(len(out), bool))
        # the 120 pairwise c_<a>_<b> colour dims overflow the 256 kB manifest: keep the ones a
        # preset uses (every colour stays available as a virtual dim / expression)
        used = {k for p in raw["mani"]["presets"] for a in ("x", "y") for k in (p.get(a) or {})}
        mdims = [d for d in raw["mani"]["dims"] if not d["key"].startswith("c_") or d["key"] in used]
        stars = [{k: v for k, v in raw["mani"]["stars"][i].items() if k not in ("thumb", "spec")} for i in keep]
        dims = [{**d, **{a: [d[a][i] for i in keep] for a in ("v", "k") if isinstance(d.get(a), list)}}
                for d in mdims]
        cat = join(base, convert({**raw["mani"], "stars": stars, "dims": dims}, key="mani", title="manifold")[0],
                   on="name") if stars else base
        virt = {f"{a}-{b}" for _, (a, b), _ in CMDS} | {f"{a}-{b}" for _, p, q in DIAGRAMS for a, b in (p, q)}
        virt |= {f"s_{a}-s_{b}" for _, p, q in SYNTH_DIAGRAMS for a, b in (p, q)}
        virt |= {"-".join(X_FILTERS)} | {f"{a}-{b}" for _, (a, b), _ in Y_OPTIONS}
        cat.virtual += [VirtualDim(v, label=v.upper().replace("-", " - "), unit="mag", group="colors") for v in sorted(virt)]
        pair = lambda e: "-".join(f"s_{t}" for t in e.split("-"))  # noqa: E731
        diag = [(k, f"{a}-{b}", y) for k, (a, b), y in CMDS] + [(k, f"{a}-{b}", f"{c}-{e}") for k, (a, b), (c, e) in DIAGRAMS]
        diag += [(k, "-".join(X_FILTERS), f"{a}-{b}") for k, (a, b), _ in Y_OPTIONS]
        cat.presets += [Preset(k, k, "axes", {"x": x, "y": y, "pairs": {"x2": pair(x), "y2": pair(y)}}) for k, x, y in diag]
        cat.presets += [Preset(k, k, "axes", {"x": f"s_{a}-s_{b}", "y": f"s_{c}-s_{e}"})
                        for k, (a, b), (c, e) in SYNTH_DIAGRAMS]
        cat.presets += [Preset(f"p_{k}", f"offset ({lab}) vs distance", "axes", {"x": f"p_{k}", "y": "r_wd2"})
                        for k, _, lab in Y_OPTIONS]
        have = {k for d in out for k, v in d.items() if isinstance(v, float)}
        cat.presets += [Preset(f"ice_{a}_{b}", f"N({a}) vs N({b})", "axes", {"x": f"col_{a}", "y": f"col_{b}"})
                        for a, b in ICE_PAIRS if {f"col_{a}", f"col_{b}"} <= have]
        cat.caveats += ["Ice columns: hollow/limit markers are 3-sigma upper limits (undetected) or lower limits "
                        "(saturated bands); most sources carry limits only.",
                        "Synthetic magnitudes integrate the NIRSpec spectrum through the NIRCam filter curves; "
                        "F210M, F360M, F480M and F444W are synthetic only (no catalogue coverage). The "
                        "live SED plots the catalogue and the synthetic magnitude of a band as two points."]
        return cat

    def _catalog(self, ctx) -> dict:
        def load():                               # NIRSpec_2640_ColorColor.load_catalog + catalog_good
            from astropy.io import fits
            with fits.open(WD2_CAT, memmap=True) as h:
                d, names = h[1].data, set(h[1].columns.names)
                out = {k: np.asarray(d[f"skycoord_ref.{k}"], float) for k in ("ra", "dec")}
                for f in CAT_FILTERS:
                    out[f] = (np.where(catalog_good(d, f), np.asarray(d[f"mag_vega_{f}"], float), np.nan)
                              if f"mag_vega_{f}" in names else np.full(len(d), np.nan)).astype(np.float32)
            keep = np.any([np.isfinite(out[f]) for f in CAT_FILTERS], axis=0)
            return {k: v[keep] for k, v in out.items()}
        a = ctx.cache.memo("n2640_cat_v1", load, inputs=[WD2_CAT])
        return {k: np.asarray(v, float) for k, v in a.items()}

    def _bg(self, ctx, a):
        from astropy.table import Table
        from portalkit.spec import CatalogSpec, ColumnSpec, DensitySpec, Preset
        n = len(a["ra"]) if ctx.limit is None else min(len(a["ra"]), 20 * int(ctx.limit))
        col = {f: a[f][:n] for f in CAT_FILTERS}
        t = Table({"id": [f"bg{i}" for i in range(n)], **col})
        lim = lambda x, y: robust_limits(x, y, np.isfinite(x) & np.isfinite(y))  # noqa: E731
        diag = [(k, (a_, b), (y,)) for k, (a_, b), y in CMDS] + [(k, p, q) for k, p, q in DIAGRAMS]
        diag += [(k, X_FILTERS, yy) for k, yy, _ in Y_OPTIONS]
        dens = []
        for k, (xa, xb), yy in diag:
            x = col[xa] - col[xb]
            y = col[yy[0]] - col[yy[1]] if len(yy) == 2 else col[yy[0]]
            xlo, xhi, ylo, yhi = lim(x, y)
            dens.append(DensitySpec(k, f"{xa}-{xb}", "-".join(yy), xr=(xlo, xhi), yr=(ylo, yhi)))
        # context catalogue: never listed, no row chunks; first paint counts its density grids
        return CatalogSpec(key="bg", table=t, id="id", context=True, density=dens, exact_rows=False,
                           columns=[ColumnSpec(f, "mag", label=f.upper(), unit="mag", step=0.001) for f in CAT_FILTERS],
                           presets=[Preset(d.key, d.key, "axes", {"x": d.x, "y": d.y}) for d in dens],
                           title=f"Wd2 NIRCam catalogue ({n:,} sources, S/N >= 10 photometry)",
                           caveats=["Magnitudes are kept where the band is unmasked, unsaturated and has "
                                    "S/N >= 10 (NIRSpec_2640_ColorColor.catalog_good)."])

    def _redx(self, ctx, a, raw):
        """Wd2 catalogue stars on the red-excess diagrams (NIRSpec_2640_RedExcess.build)."""
        from astropy.table import Table
        from portalkit.spec import CatalogSpec, ColumnSpec, DensitySpec, Preset, VirtualDim
        fl = sorted({*X_FILTERS, *(f for _, p, _ in Y_OPTIONS for f in p)})
        x = a[X_FILTERS[0]] - a[X_FILTERS[1]]
        r = _sep_arcmin(a["ra"], a["dec"])
        sel = np.flatnonzero(np.isfinite(x) & np.isfinite(r))
        sel = sel if ctx.limit is None else sel[:20 * int(ctx.limit)]
        t = Table({"id": [f"rx{i}" for i in sel], "ra": a["ra"][sel], "dec": a["dec"][sel],
                   **{f: a[f][sel] for f in fl}, "r_wd2": np.round(r[sel], 4),
                   "foreground": x[sel] < FOREGROUND_X_CUT})
        cols = [ColumnSpec(f, "mag", label=f.upper(), unit="mag", filter=_fid(f), step=0.001) for f in fl]
        cols += [ColumnSpec("r_wd2", "value", label="distance from the Wd2 centre", unit="arcmin", step=1e-4),
                 ColumnSpec("foreground", "flag", label=f"foreground (F115W - F200W < {FOREGROUND_X_CUT})")]
        dens, pre = [], []
        xs = "-".join(X_FILTERS)
        for k, (ya, yb), lab in Y_OPTIONS:
            y = t[ya] - t[yb]
            t[f"p_{k}"] = np.round(perp_offset(np.asarray(t[X_FILTERS[0]] - t[X_FILTERS[1]]), np.asarray(y),
                                               raw["slope"][k]), 3)
            cols.append(ColumnSpec(f"p_{k}", "value", label=f"offset from the reddening vector ({lab})", unit="mag",
                                   step=1e-3))
            ok = np.isfinite(np.asarray(t[X_FILTERS[0]] - t[X_FILTERS[1]])) & np.isfinite(np.asarray(y))
            xlo, xhi, ylo, yhi = robust_limits(np.asarray(t[X_FILTERS[0]] - t[X_FILTERS[1]]), np.asarray(y), ok) \
                if ok.sum() > 1 else (0.0, 1.0, 0.0, 1.0)
            for stat in (None, "median:r_wd2"):
                dens.append(DensitySpec(f"{k}_{'rwd2' if stat else 'n'}", xs, f"{ya}-{yb}", nx=110, ny=110,
                                        xr=(xlo, xhi), yr=(ylo, yhi), stat=stat))
            p, r_ = np.asarray(t[f"p_{k}"], float), np.asarray(t["r_wd2"], float)   # offset-pane backdrop
            plo, phi, rlo, rhi = robust_limits(p, r_, np.isfinite(p) & np.isfinite(r_))
            dens.append(DensitySpec(f"p_{k}_r", f"p_{k}", "r_wd2", nx=110, ny=110, xr=(plo, phi), yr=(rlo, rhi)))
            pre += [Preset(k, f"{lab} vs {xs.upper()}", "axes", {"x": xs, "y": f"{ya}-{yb}"}),
                    Preset(f"p_{k}", f"offset ({lab}) vs distance", "axes", {"x": f"p_{k}", "y": "r_wd2"})]
        virt = [VirtualDim(v, label=v.upper().replace("-", " - "), unit="mag", group="colors")
                for v in [xs] + [f"{a_}-{b}" for _, (a_, b), _ in Y_OPTIONS]]
        return CatalogSpec(key="redx", table=t, id="id", ra="ra", dec="dec", columns=cols, density=dens, presets=pre,
                           virtual=virt, title=f"Wd2 catalogue stars with F115W - F200W ({len(t):,})",
                           noun=("star", "stars"), exact_rows=False,
                           caveats=["S/N >= 10 photometry; offsets are measured perpendicular to the G23 (Rv = 3.1) "
                                    "reddening vector anchored at (0, 0)."])

    def _iso(self, ctx):
        from portalkit.physics.isochrones import DEFAULT_MIST, isochrone_spec
        from portalkit.spec import IsochroneSpec, Track

        def load():
            spec = isochrone_spec(DEFAULT_MIST, ages_myr=ISO_AGES_MYR, dist_pc=DIST_PC,
                                  filters=[_fid(b) for b in CAT_FILTERS], npt=150)
            return [{"label": t.label, "age_myr": t.age_myr, "dist_pc": t.dist_pc, "feh": t.feh,
                     "mass": np.asarray(t.mass).tolist(),
                     "mags": {k: [_num(v) for v in m] for k, m in t.mags.items()}} for t in spec.tracks]
        docs = ctx.cache.memo(f"n2640_iso_v1_{'_'.join(CAT_FILTERS)}", load, inputs=[DEFAULT_MIST])
        return IsochroneSpec(tracks=[Track(d["label"], d["age_myr"], d["dist_pc"], d["feh"], np.asarray(d["mass"], float),
                                           {k: np.array([np.nan if v is None else v for v in m], float)
                                            for k, m in d["mags"].items()}) for d in docs])

    # -- records (loaders are bound to one datasets() call's raw/ctx) ------------------------
    def _x1d(self, ctx, name, prods):
        """Per-grating x1d arrays {label: (wave, flux, err)} (memoised)."""
        files = [os.path.join(p["dir"], p["x1d"]) for p in prods]

        def load():
            from astropy.table import Table
            out = {}
            for i, fn in enumerate(files):
                t = Table.read(fn, hdu=1)
                out.update({f"w{i}": np.asarray(t["WAVELENGTH"], float), f"f{i}": np.asarray(t["FLUX"], float),
                            f"e{i}": np.asarray(t["FLUX_ERROR"], float)})
            return out
        a = ctx.cache.memo(f"n2640_x1d_v1_{name}", load, inputs=files)
        return {p["grating"]: (a[f"w{i}"], a[f"f{i}"], a[f"e{i}"]) for i, p in enumerate(prods)}

    def _spectrum(self, raw, ctx, name):
        from portalkit.legacy.nirspec import assign_gratings, clean_flux, stack_segments
        from portalkit.spec import Spectrum
        r = raw["by"].get(name)
        prods = [p for p in (r or {}).get("Products", []) if p.get("x1d") and os.path.isfile(os.path.join(p["dir"], p["x1d"]))]
        if r is None or not prods:
            return None
        x1d = self._x1d(ctx, name, prods)
        if ctx.tier == "A":                       # legacy merged spectrum, split by the x1d grids
            p = SRC_DIR / "specdata" / f"{name}.json"
            if not p.is_file():
                return None
            ctx.inputs.add(p)
            sd = json.loads(p.read_text(encoding="utf-8"))
            fs = float(sd.get("fluxscale", 1.0))
            w, f, e = (np.array([np.nan if v is None else v for v in sd[k]], float) for k in "wfe")
            f, e = f * fs, e * fs
            lab = assign_gratings(w, {g: v[0] for g, v in x1d.items()}, decimals=5,
                                  values=(f, {g: clean_flux(v[1], v[2]) for g, v in x1d.items()}))
            parts = [(g, w[lab == j], {"flux": f[lab == j], "err": e[lab == j]}) for j, g in enumerate(x1d)]
            det = sd.get("lines") or []
        else:
            parts = [(g, w, {"flux": clean_flux(f, e), "err": e}) for g, (w, f, e) in x1d.items()]
            det = raw["lines"].get(name, [])
        w, ch, segs = stack_segments(parts)
        f, e = ch["flux"], ch["err"]
        with np.errstate(invalid="ignore"):
            e = np.where(np.isfinite(f) & (e > 0), e, np.nan)
        lines = [{"name": str(x["n"]), "w": float(x["w"]), "kind": "emission" if x.get("k") == "e" else "absorption",
                  "detected": True, "snr": _num(x.get("snr"))} for x in det if _num(x.get("w"))]
        lines += [{"name": str(m["name"]), "w": float(m["wl"]), "kind": "ice", "detected": False} for m in raw["icemarks"]]
        return Spectrum(wave=w, channels={"flux": f, "err": e}, units={"flux": "Jy"}, segments=segs, lines=lines,
                        meta={"name": name, "slit": r["SlitID"], "obs": r["Observation"], "gratings": r["Gratings"]})

    def _image2d(self, raw, ctx, name):
        """s2d products stitched with PRISM filling the gaps (every tier; see the module docstring)."""
        from portalkit.legacy.nirspec import read_s2d, stitch_s2d
        prods = [p for p in (raw["by"].get(name) or {}).get("Products", [])
                 if p.get("s2d") and os.path.isfile(os.path.join(p["dir"], p["s2d"]))]
        files = [os.path.join(p["dir"], p["s2d"]) for p in prods]
        if not files:
            return None

        def load():
            out = {}
            for i, fn in enumerate(files):
                out[f"d{i}"], out[f"w{i}"] = read_s2d(fn)
            return out
        a = ctx.cache.memo(f"n2640_s2d_v1_{name}", load, inputs=files)
        return stitch_s2d([(p["grating"], a[f"d{i}"], a[f"w{i}"]) for i, p in enumerate(prods)])

    def _products(self, raw, ctx, name):
        r = raw["by"].get(name)
        if r is None:
            return None
        items = []
        for p in r["Products"]:
            for k in ("x1d", "s2d"):
                fn = os.path.join(p["dir"], p[k]) if p.get(k) else ""
                if fn and os.path.isfile(fn):     # secure archive copy: login required
                    items.append({"label": f"{p['grating']}/{p['filter']} {k}", "url": _surl(fn), "kind": "fits",
                                  "auth": "secure", "group": p["grating"], "size": os.path.getsize(fn)})
        for k, lab in (("SpectrumPDF", "spectrum figure (PDF)"), ("IceFitPNG", "ice-fit figure")):
            if _pub(r.get(k)):
                items.append({"label": lab, "url": _pub(r[k]), "kind": "link" if k.endswith("PDF") else "png",
                              "auth": "public", "group": "figures"})
        items.append({"label": f"MAST: jw02640-o{r['Observation']}", "url": _mast(r["Observation"]), "kind": "mast",
                      "auth": "public", "group": "archive"})
        return {"schema": "products/1", "items": items}

    # -- images, sky ----------------------------------------------------------------------
    def _images(self, raw):
        from portalkit.spec import ImageSetSpec, Preset
        gpre = [Preset(re.sub(r"\W+", "_", g.lower()), g.replace(",", " + "), "filter", {"facets": {"gratings": [g]}})
                for g in sorted({r["Gratings"] for r in raw["rows"]})]
        rows = {k: [] for k in ("fig", "pv", "sed", "pngs")}
        for r in raw["rows"]:
            n = r["SrcName"]
            base = {"id": n, "caption": f"{n}  {r['Gratings'].replace(',', ' + ')}", "gratings": r["Gratings"]}
            rows["fig"].append({**base, **{k: _pub(r.get(FIG_FIELDS[k])) for k in FIG_ROLES}})
            rows["pngs"].append({**base, **{k: _pub(r.get(FIG_FIELDS[k])) for k in PNG_ROLES}})
            rows["pv"].append({**base, "pv": _pub(raw["pv"].get(n))})
            rows["sed"].append({**base, "sed": _pub(raw["sed"].get(n))})
        roles = lambda m: {k: (k, v) for k, v in m.items()}  # noqa: E731
        kw = {"catalog": "src", "presets": gpre, "tags": ["gratings"]}
        return {"fig": ImageSetSpec.from_rows(rows["fig"], roles(FIG_ROLES), "fig", **kw),
                "pv": ImageSetSpec.from_rows(rows["pv"], {"pv": ("pv", "PV (L3)")}, "pv", **kw),
                "sed": ImageSetSpec.from_rows(rows["sed"], {"sed": ("sed", "SED + cutouts")}, "sed", **kw),
                "pngs": ImageSetSpec.from_rows(rows["pngs"], roles(PNG_ROLES), "pngs", **kw)}

    def _hips(self, raw):
        from portalkit.spec import HipsSpec
        taken: set = set()

        def layer(nm, url, default, **kw):
            s = re.sub(r"[^a-z0-9]+", "_", nm.lower()).strip("_") or "layer"
            k, i = s, 2
            while k in taken:
                k, i = f"{s}_{i}", i + 1
            taken.add(k)
            return {"id": k, "name": nm, "url": CDS_HIPS.get(url, url), "default": bool(default), **kw}
        base = [layer(*s) for s in raw["base"]]
        ovl = [layer(*s, opacity=1.0) for s in raw["overlay"]]
        ra = np.array([r["RA"] for r in raw["rows"]], float)
        dec = np.array([r["Dec"] for r in raw["rows"]], float)
        if not ra.size:
            return HipsSpec(base=base, overlays=ovl)
        c = [float(ra.mean()), float(dec.mean())]                  # AladinMap.write_html
        span = max((ra.max() - ra.min()) * np.cos(np.radians(c[1])), dec.max() - dec.min())
        return HipsSpec(base=base, overlays=ovl, target={"ra": c[0], "dec": c[1], "fov": float(max(0.03, 1.6 * span))})

    def _overlays(self, raw):
        from portalkit.spec import OverlaySpec

        def poly(s):
            pts = [[float(a) for a in p.split(",")] for p in s.split(";") if p.strip()]
            return pts if len(pts) >= 2 else None
        lay = []
        for lid, nm, i, col in (("msa_shutters", "MSA shutters", 0, "#ff8fd0"), ("dither", "Dither footprint", 1, "#7fd4ff")):
            items = [{"poly": poly(raw["slits"][n][i]), "id": n, "label": n} for n in raw["by"] if n in raw["slits"]]
            lay.append({"id": lid, "name": nm, "space": "sky", "kind": "polygons", "default": lid == "msa_shutters",
                        "style": {"color": col, "width": 1}, "items": [it for it in items if it["poly"]]})
        line = lambda lid, nm, col, items, dash=None: {  # noqa: E731
            "id": lid, "name": nm, "space": "dim", "kind": "polyline", "default": True, "items": items,
            "style": {"color": col, "width": 1.5 if dash is None else 1, **({"dash": dash} if dash else {})}}
        xs = "-".join(X_FILTERS)
        vec = [{"x": xs, "y": f"{a}-{b}", "pts": [[0.0, 0.0], [10.0, round(10.0 * raw["slope"][k], 5)]]}
               for k, (a, b), _ in Y_OPTIONS]
        cut = [{"x": xs, "y": f"{a}-{b}", "pts": [[FOREGROUND_X_CUT, -20.0], [FOREGROUND_X_CUT, 20.0]]} for _, (a, b), _ in Y_OPTIONS]
        zero = [{"x": f"p_{k}", "y": "r_wd2", "pts": [[0.0, 0.0], [0.0, 60.0]]} for k, _, _ in Y_OPTIONS]
        one = [{"x": f"col_{a}", "y": f"col_{b}", "pts": [[1e13, 1e13], [1e21, 1e21]]} for a, b in ICE_PAIRS]
        lay += [line("redvec", "G23 reddening vector from (0, 0)", "#ff6ec7", vec),
                line("fg_cut", f"foreground cut F115W - F200W = {FOREGROUND_X_CUT}", "#8effa3", cut, [6, 4]),
                line("p_zero", "on the reddening vector (offset 0)", "#ff6ec7", zero, [4, 4]),
                line("one_to_one", "1:1", "#98a1b5", one, [4, 4])]
        return {"ovl": OverlaySpec(layers=lay)}         # src and redx share the dims these items use

    def _readme(self, ctx, raw, ds):
        from portalkit.spec import ReadmeSpec
        rows, n = raw["rows"], len(raw["rows"])
        lim = "" if ctx.limit is None else f" (fixture build: first {ctx.limit} sources)"
        grat = sorted({r["Gratings"] for r in rows})
        det = {sp: sum(1 for n in raw["by"] if raw["ice"].get(n, {}).get(sp, (0, 0, None))[2] == "v") for sp in SPECIES}
        md = (f"# JWST NIRSpec 2640: Westerlund 2 MSA spectra\n\n{n} sources{lim} observed with the NIRSpec MSA. "
              f"Every source has a PRISM spectrum; {sum(r['Gratings'] != 'PRISM' for r in rows)} also have G140M and "
              f"G395M. The colour-colour backgrounds use {len(ds['bg'].table):,} Wd2 NIRCam catalogue sources with "
              f"S/N >= 10 photometry; the red-excess page shows {len(ds['redx'].table):,} of them.\n\n## Pages\n\n"
              "- **Map**: MSA shutters on the Wd2 HiPS mosaics, linked to spectra, 2D spectra and FITS products.\n"
              "- **Colour-colour**: catalogue colours joined to synthetic ones, brown-dwarf magnitude split.\n"
              "- **Red excess**: offsets from the G23 reddening vector, with a median-distance background.\n"
              "- **Manifold**, **spectrum viewer**, **ice columns**, **PV**, **SED** and **spectrum PNG** pages.\n\n"
              "## Caveats\n\n- FITS products sit in the secure archive and need a login (badged links).\n"
              "- Spectra: each grating is its own segment (PRISM overlaps G140M/G395M). Fluxes above 1 Jy, errors "
              "above 1 Jy, negative fluxes and points above mean + 10 sigma of their grating are blanked.\n"
              "- Ice columns are mostly limits: detections per species " +
              ", ".join(f"{k} {v}" for k, v in det.items()) + ".\n")
        blocks = [{"type": "table", "columns": ["Gratings", "Sources"],
                   "rows": [[g.replace(",", " + "), sum(r["Gratings"] == g for r in rows)] for g in grat]},
                  {"type": "table", "columns": ["Observation", "MAST"],
                   "rows": [[o, f"[jw02640-o{o}]({_mast(o)})"] for o in sorted({r["Observation"] for r in rows})]}]
        return ReadmeSpec(markdown=md, blocks=blocks)

    # -- pages ----------------------------------------------------------------------------
    def pages(self, ctx, ds):
        from portalkit.caps import SHARED_EXTINCTION_VECTOR, overlay_slug
        from portalkit.spec import PageSpec as P
        ov = [overlay_slug(t.label) for t in ds["iso"].tracks] + [SHARED_EXTINCTION_VECTOR]
        bands = [{"label": b["label"], "w0": b["lo"], "w1": b["hi"]} for b in _pay(
            ctx, SRC_DIR / "nirspec_2640_specviewer.html", "BANDS")["BANDS"]]
        phys = ["tpl", "flt", "ext"]
        sky = {"pane": "sky", "dataset": "src", "options": {"hips": "hips", "overlays": ["msa_shutters", "dither"]}}
        lst = {"pane": "list", "dataset": "src", "size": 250}
        det = {"pane": "detail", "dataset": "src", "options": {"template": "nirspec_source", "products": "prod",
                                                               "images": "fig"}}
        s1 = {"pane": "spectrum1d", "dataset": "spec", "options": {"records": "spec", "bands": bands, "templates": "tpl"}}
        s2 = {"pane": "spectrum2d", "dataset": "s2d", "options": {"records": "s2d", "link1d": True}}
        links = {k: f"{v}.html#id={{id}}" for k, v in LINKS}
        cards = [{"page": p, "text": tx} for p, tx in CARDS]
        cards += [{"href": f"{WD2_PORTAL}{f}.html", "title": tt, "auth": "public", "text": tx} for f, tt, tx in WD2_CARDS]
        cards.append({"href": "https://www.stsci.edu/jwst/science-execution/program-information?id=2640",
                      "title": "Program 2640 at STScI", "auth": "public", "text": "Proposal information and APT file."})
        sc = lambda i, ds_, x, y, **o: {"pane": "scatter", "id": i, "dataset": ds_, "options": {"x": x, "y": y, **o}}  # noqa: E731
        pair = lambda e: "-".join(f"s_{t}" for t in e.split("-"))  # noqa: E731
        (_, (a, b), (c, e)), (_, (f, g), y) = DIAGRAMS[0], CMDS[0]
        ccd = sc("ccd", "src", f"{a}-{b}", f"{c}-{e}", background="bg", overlays=ov, presets=False,
                 pairs={"x2": pair(f"{a}-{b}"), "y2": pair(f"{c}-{e}")})
        cmd = sc("cmd", "src", f"{f}-{g}", y, background="bg", overlays=ov, presets=True)
        bd = {"pane": "hist", "id": "bd", "dataset": "src", "options": {"expr": "s_f444w", "bins": 40, "brush": True}}
        xs, (k0, (ya, yb), _) = "-".join(X_FILTERS), Y_OPTIONS[0]
        # legacy median-r_wd2 backgrounds (NIRSpec_2640_RedExcess.render_panel PNGs, extent = the
        # DIAGRAMS limits), one per y colour, under the NIRSpec sources
        dg = {d["key"]: d for d in _pay(ctx, SRC_DIR / "nirspec_2640_red_excess.html", "DIAGRAMS")["DIAGRAMS"]}
        ras = [{"url": u, "extent": [float(v) for v in dg[k]["limits"]], "x": xs, "y": f"{a_}-{b_}", "opacity": 0.85}
               for k, (a_, b_), _ in Y_OPTIONS if k in dg for u in [_pub((dg[k].get("images") or {}).get("rwd2"))] if u]
        rx = sc("redx", "src", xs, f"{ya}-{yb}", background="bg", presets=True, raster=ras,
                overlays=["redvec", "fg_cut"] + ov, pairs={"x2": pair(xs), "y2": pair(f"{ya}-{yb}")})
        rw = sc("rwd2", "redx", xs, f"{ya}-{yb}", color="r_wd2", presets=True, overlays=["redvec", "fg_cut"])
        off = sc("offset", "src", f"p_{k0}", "r_wd2", background="redx", overlays=["p_zero"], presets=True)
        ice = sc("ice", "src", "col_H2O", "col_CO2", xlog=True, ylog=True, errorbars=True, presets=True,
                 overlays=["one_to_one"], color="Gratings")
        gal = lambda img, roles: {"pane": "gallery", "dataset": img, "options": {"images": img, "roles": roles}}  # noqa: E731
        side = lambda tpl, img, *more, size=380: {"col": [{**det, "options": {"template": tpl, "images": img,  # noqa: E731
                                                                              **({"products": "prod"} if img == "pv" else {})}},
                                                          *more, sky], "size": size}
        st = {"color": {"by": "Gratings"}}
        return [
            P(id="index", archetype="landing", title="NIRSpec 2640 (Westerlund 2) portal", datasets=["readme"],
              layout={"pane": "landing", "options": {"cards": cards, "counts": True, "search": True}}),
            P(id="nirspec_2640_map", archetype="explorer", title="MSA map", subtitle="shutters, spectra, products",
              datasets=["src", "spec", "s2d", "prod", "fig", "sed", "hips", "ovl", "iso"] + phys, links=links, state=st,
              layout={"row": [lst, {"col": [sky, sc("cmd", "src", f"{f}-{g}", y, overlays=ov, presets=True)]},
                              {"col": [det, s1, s2], "size": 430}]}),
            P(id="nirspec_2640_colorcolor", archetype="explorer", title="Colour-colour and BD split", links=links,
              datasets=["src", "bg", "spec", "hips", "ovl", "iso"] + phys, state=st,
              layout={"row": [lst, {"col": [ccd, cmd]}, {"col": [bd, det, s1, sky], "size": 400}]}),
            P(id="nirspec_2640_specviewer", archetype="spectra", title="Spectrum viewer", links=links,
              datasets=["src", "spec", "s2d", "prod", "hips", "ovl"] + phys,
              layout={"row": [lst, {"col": [s1, s2]}, {"col": [det, sky], "size": 380}]}),
            P(id="nirspec_2640_red_excess", archetype="explorer", title="Red excess off the reddening vector",
              links=links, datasets=["src", "bg", "redx", "hips", "ovl", "iso"] + phys, state={"color": {"by": "r_wd2"}},
              layout={"row": [lst, {"col": [rx, rw]}, {"col": [off, det, sky], "size": 400}]}),
            P(id="nirspec_2640_manifold", archetype="manifold", title="Property manifold", links=links,
              datasets=["src", "spec", "fig", "sed", "hips", "ovl", "iso"] + phys, state=st,
              layout={"row": [lst, {"pane": "manifold", "dataset": "src", "options": {"frame": "pca", "overlays": ov}},
                              {"col": [det, s1, sky], "size": 430}]}),
            P(id="nirspec_2640_icecolumns", archetype="explorer", title="Ice columns", links=links,
              datasets=["src", "fig", "hips", "ovl"] + phys, state=st,
              layout={"row": [lst, {"col": [ice]}, {"col": [{**det, "options": {**det["options"], "images": "fig"}}, sky],
                                                     "size": 400}]}),
            P(id="nirspec_2640_pvgallery", archetype="gallery", title="PV gallery", links=links,
              datasets=["pv", "src", "prod", "hips", "ovl"],
              layout={"row": [lst, gal("pv", ["pv"]), side("compact", "pv")]}),
            P(id="nirspec_2640_sedgallery", archetype="gallery", title="SED gallery", links=links,
              datasets=["sed", "src", "hips", "ovl"] + phys,
              layout={"row": [lst, gal("sed", []), side("photometry", "sed", {"pane": "sed", "dataset": "src"}, size=400)]}),
            P(id="gallery_2640", archetype="gallery", title="Spectrum PNGs", links=links,
              datasets=["pngs", "src", "sed", "hips", "ovl"],
              layout={"row": [lst, gal("pngs", list(PNG_ROLES)), side("compact", "sed")]}),
        ]


ADAPTER = NIRSpec2640Adapter()


def vector_slope(ya: str, yb: str) -> float:
    """RedExcess.vector_slope: (k[ya] - k[yb]) / (k[f115w] - k[f200w]) of G23 (Rv = 3.1) at the
    legacy PIVOT_UM wavelengths (dust_extinction; else the vendored G23 curve, log-interpolated)."""
    from portalkit.physics.extinction import dust_extinction_available, g23_dust_extinction, vendored_curve
    w = np.array([PIVOT_UM[f] for f in (ya, yb, *X_FILTERS)], float)
    if dust_extinction_available():
        k = g23_dust_extinction(w, 3.1)
    else:
        c = vendored_curve()
        o = np.argsort(c["w"])
        k = np.interp(np.log(w), np.log(np.asarray(c["w"], float)[o]), np.asarray(c["3.1"], float)[o])
    return float((k[0] - k[1]) / (k[2] - k[3]))


# -- _fallback ------------------------------------------------------------------------------
# Copies of legacy code whose modules import matplotlib / icemodels / dust_extinction at
# module level (NIRSpec_2640_ColorColor, NIRSpec_2640_RedExcess), or of small helpers the
# icecolumns page needs in every tier (NIRSpec_2640_Manifold.ICE_A / ice_kind). Every tier uses
# them (section 9.3 deviation: those modules cannot be imported cheaply). Sources, sha256 at copy:
#   NIRSpec_2640_ColorColor.py  1039e362060a3b9c2854df327a0a8c6abcd52de4a38fc2df470998ab3c0011da
#   NIRSpec_2640_RedExcess.py   5ad46d350064841a78447a0ae84466107ffadcbed177f7d8597801bf701ad1c2
#   NIRSpec_2640_Manifold.py    dadc26b68ead517997dccdce4ddb8f6345c9b1fc13caf3fe382d7796eb4c08ed
# catalog_good and perp_offset are verbatim in behaviour; robust_limits adds a (0, 1) fallback
# for < 2 points and a 0.5 pad for a zero-width range; ice_kind also returns the detection error.

CAT_FILTERS = ("f115w", "f150w", "f162m", "f164n", "f182m", "f187n", "f200w", "f212n", "f250m", "f277w",
               "f300m", "f323n", "f335m", "f405n", "f410m", "f466n")
SYNTH_ONLY_FILTERS = ("f210m", "f360m", "f480m", "f444w")
HST_FILTERS = ("f336w", "f439w", "f555w", "f814w")
CMDS = [("cmd_212_182212", ("f182m", "f212n"), "f212n"), ("cmd_410_405466", ("f405n", "f466n"), "f410m"),
        ("cmd_466_212466", ("f212n", "f466n"), "f466n"), ("cmd_410_115410", ("f115w", "f410m"), "f410m"),
        ("cmd_277_200277", ("f200w", "f277w"), "f277w")]
DIAGRAMS = [("icenb", ("f182m", "f212n"), ("f405n", "f466n")), ("co410", ("f405n", "f410m"), ("f410m", "f466n")),
            ("rednb", ("f212n", "f410m"), ("f410m", "f466n")), ("broadbb", ("f115w", "f200w"), ("f277w", "f410m")),
            ("midbb", ("f200w", "f277w"), ("f277w", "f410m")), ("narrowfe", ("f164n", "f212n"), ("f323n", "f466n")),
            ("bb150", ("f150w", "f200w"), ("f277w", "f410m")), ("midbands", ("f212n", "f250m"), ("f300m", "f335m"))]
SYNTH_DIAGRAMS = [("synthlw", ("f162m", "f210m"), ("f360m", "f480m"))]
MAX_EMAG = 1.0857 / 10.0
#: NIRSpec_2640_RedExcess
WD2_CENTER = (155.992083, -57.763611)
X_FILTERS = ("f115w", "f200w")
Y_OPTIONS = [("y182212", ("f182m", "f212n"), "F182M - F212N"), ("y182200", ("f182m", "f200w"), "F182M - F200W"),
             ("y200212", ("f200w", "f212n"), "F200W - F212N")]
FOREGROUND_X_CUT = 1.7
PIVOT_UM = {"f115w": 1.154, "f150w": 1.501, "f182m": 1.845, "f200w": 1.989, "f212n": 2.121}
#: NIRSpec_2640_Manifold band strengths [cm / molecule]
ICE_A = {"H2O": 2.0e-16, "CO2": 7.6e-17, "13CO2": 7.8e-17, "OCN": 1.3e-16, "CO": 1.1e-17, "OCS": 1.5e-16}


def catalog_good(cat, filt):
    """ColorColor.catalog_good: finite, unmasked, unsaturated, S/N >= 10 (from emag_ab). A
    missing column raises KeyError, as in the legacy code."""
    mag = np.asarray(cat[f'mag_vega_{filt}'], float)
    masked = np.array(cat[f'mask_{filt}'], dtype=bool)
    sat = np.array(cat[f'is_saturated_{filt}'], dtype=bool)
    emag = np.asarray(cat[f'emag_ab_{filt}'], float)
    with np.errstate(invalid="ignore"):
        return (np.isfinite(mag) & ~masked & ~sat
                & np.isfinite(emag) & (emag <= MAX_EMAG))


def robust_limits(x, y, mask):
    """ColorColor.robust_limits: 5-95 percentile, padded by 8 %; (0, 1) ranges when too few points."""
    xs, ys = np.asarray(x, float)[mask], np.asarray(y, float)[mask]
    if xs.size < 2:
        return (0.0, 1.0, 0.0, 1.0)
    (xlo, xhi), (ylo, yhi) = np.nanpercentile(xs, [5, 95]), np.nanpercentile(ys, [5, 95])
    px, py = 0.08 * (xhi - xlo) or 0.5, 0.08 * (yhi - ylo) or 0.5
    return (float(xlo - px), float(xhi + px), float(ylo - py), float(yhi + py))


def perp_offset(x, y, slope):
    """RedExcess.perp_offset: signed distance from the (0, 0)-anchored reddening vector."""
    norm = np.hypot(1.0, slope)
    return -np.asarray(x, float) * (slope / norm) + np.asarray(y, float) * (1.0 / norm)


def mag_of(phot: dict, f: str) -> float:
    """RedExcess.magof: the catalogue magnitude, else the synthetic one, else NaN."""
    for k in (f"cat_{f}", f"synth_{f}"):
        v = _num(phot.get(k))
        if v is not None:
            return v
    return float("nan")


def ice_kind(r, sp):
    """Manifold.ice_kind -> (value, error, kind): 'v' detected (error = N_err), 'lo' saturated,
    'up' 3-sigma upper limit from the integrated-tau error; (None, None, None) otherwise."""
    if str(r[f"{sp}_limit"]) == "bad":
        return None, None, None
    n = _num(r[f"{sp}_N"])
    if bool(r[f"{sp}_saturated"]):
        return (n, None, "lo") if n is not None else (None, None, None)
    if bool(r[f"{sp}_detected"]):
        return (n, _num(r[f"{sp}_N_err"]), "v") if n is not None else (None, None, None)
    err = _num(r[f"{sp}_tau_int_err"])
    return (3.0 * err / ICE_A[sp], None, "up") if err is not None and err > 0 else (None, None, None)
