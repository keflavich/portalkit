"""JWST NIRSpec archive family adapter (WP28; ARCHITECTURE.md sections 9 and 14.3, wave 4).

One adapter, three portals:

* ``archive`` (secure, ``jwst/spectra/portal_unified``; ``portal/unified`` is also public
  through the ``pngs/portal`` symlink): landing + README, the colour portal
  (CCD with two marginal CMDs, list, detail, spectrum, SED, sky) and the ice manifold.
* ``w51`` (secure, ``w51/portal/archive_colors/unified``): the same 1902-row archive table with
  the W51 NIRCam photometry as a context (density) dataset and the W51 preset planes. w51
  stages its own copy of the spectrum shards (``spec``, same ~15 MB as the archive and gallery
  portals) rather than a foreign record set onto the archive portal: the archive portal's
  ``.htpasswd`` (``web/secure/jwst``) and the w51 portal's (``web/secure/w51``) are different
  password files with only one account in common (``brick``), so a viewer who only has a w51
  account (``w51``, ``rainer``) would get HTTP 401 fetching every spectrum if w51 pointed at
  the archive portal's shards instead of its own. ``portalkit check``/``verify --http`` cannot
  catch this (a 401 from a secure base without credentials is reported only as a note, section
  4.3), so the duplicate copy is kept deliberately. :func:`foreign_base` remains as a tested
  helper for the generic cross-realm-refusal case (public gallery -> secure archive) and for
  other families (e.g. wd2 -> nirspec2640) whose portals do share one password file.
* ``gallery`` (public, ``web/public/jwst/spectra`` -> ``mastDownload/JWST/pngs/unified``): one
  faceted PNG gallery that replaces the 11 legacy gallery pages, plus its checklist (annotate)
  view. The PNGs are already public through that symlink; the gallery links into the secure
  colour portal by id. The gallery keeps its own copy of the spectrum shards (``spec``): it is
  public, and a public portal may not reference the secure archive portal's shards (the
  foreign-records visibility rule; ``portalkit build`` refuses it with exit 3).

Ids are derived from the x1d filename (``"_".join(parts[:-3]) + "_" + parts[-2]``, e.g.
``jw01854-o003_t016_g235h-f170lp``; unique over the table). Legacy pages addressed spectra by
row index (``spectra/<i>.json``) and the manifold by a stale index (695 of 1539 wrong); the
index is gone and the x1d basename and the legacy manifold star names are aliases.

Band coverage: the legacy per-filter ``gap`` fraction (permille, ``cg``) becomes one ``flag``
column ``cov_<F>`` per filter (fully sampled = 1000), used as the ``validity`` of the
synthesized magnitude, so a colour exists only where the spectrum samples both bands.

* Tier A reads the legacy payloads (``PORTAL`` in ``portal/index.html``, the
  ``portal/spectra/<i>.json`` spectra and the manifold ``DATA`` for alias names).
* Tier B reads the pipeline tables (``jwst_archive_spectra_as_fluxes.ecsv`` and the
  ``portal_coords``/``portal_coverage`` ECSVs, recomputed from the x1d files unless they are
  provably row-aligned with the table) and stacks/cleans/downsamples the x1d files itself. The
  legacy module imports ``JWST_Archive_Spectra`` (SVO network queries at import), so its
  functions are restated in ``_fallback``; the zero points and filter curves come from the
  portalkit filter registry (the same SVO values).
* Every tier reads the W51 catalogue for the context dataset (the legacy payload holds a
  quantized subsample of an older catalogue run).

The hand-edited ``jwst_archive_spectra_checklist.csv`` is never opened: the checklist codes
come from the fluxes table (``ISOKN``), and the checklist page exports annotations for
``portalkit archive-checklist-diff`` (tests/py/test_no_checklist_write.py).
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

import numpy as np

ARCH = Path("/orange/adamginsburg/jwst/spectra/mastDownload/JWST")
PORTAL_DIR = Path("/orange/adamginsburg/jwst/spectra/portal")
W51_DIR = Path("/orange/adamginsburg/w51/portal/archive_colors")
TABLE = ARCH / "jwst_archive_spectra_as_fluxes.ecsv"
PNG_DIR = ARCH / "pngs"
W51_CAT = Path("/orange/adamginsburg/jwst/w51/catalogs/"
               "basic_merged_indivexp_photometry_tables_merged_resbgsub_m8_dedup.fits")
JWST_ROOT = "/orange/adamginsburg/jwst/"
#: web/secure/jwst -> /orange/adamginsburg/jwst (login required)
SECURE = "https://data.rc.ufl.edu/secure/adamginsburg/jwst/"
#: web/public/jwst/spectra -> mastDownload/JWST/pngs (public)
PUBLIC_PNG = "https://data.rc.ufl.edu/pub/adamginsburg/jwst/spectra/"
#: NIRCam filters of the legacy portal, by effective wavelength
FILTERS = ("F070W", "F090W", "F115W", "F140M", "F150W2", "F150W", "F162M", "F164N", "F182M", "F187N",
           "F200W", "F210M", "F212N", "F250M", "F277W", "F300M", "F322W2", "F323N", "F335M", "F356W",
           "F360M", "F405N", "F410M", "F430M", "F444W", "F460M", "F466N", "F470N", "F480M")
COV_FULL = 1000          # permille: the band is fully sampled (legacy covreq default)
COV_TOL = 5              # x1d_info fallback: >= 995 counts as full (registry vs SVO curves)
LEGACY_SRC = Path("/orange/adamginsburg/jwst/brick/analysis/JWST_Archive_ColorPortal.py")
MAX_FLUX = 1.0           # Jy; JWST_Archive_Spectra.max_flux
NSPEC, GAP_FACTOR = 1500, 5.0
CDS_HIPS = [{"id": "2mass", "name": "2MASS colour", "url": "https://alasky.cds.unistra.fr/2MASS/Color/",
             "default": True},
            {"id": "dss2", "name": "DSS2 colour", "url": "https://alasky.cds.unistra.fr/DSS/DSSColor/",
             "default": False}]
GRATING_COLORS = {"PRISM": "#e4e9f3", "G140M": "#7fd4ff", "G140H": "#3fa9dc", "G235M": "#9d8cff",
                  "G235H": "#6a5acd", "G395M": "#ff9d5c", "G395H": "#ff5c7a"}
CHECK_COLORS = {"y": "#7fe388", "n": "#ff5c7a", "g": "#ffcc33", "w": "#c77dff", "l": "#8894ab",
                "e": "#ff8fd0", "?": "#454b58"}
CHECK_CODES = ["y", "n", "g", "w", "l", "e"]
PNG_RE = re.compile(r"^(?P<head>.+)_(?P<gr>(?:G\d{3}[MH]|PRISM|F\d{3}W2?)+)(?:_(?P<set>2221|1182))?"
                    r"_(?P<slit>[^_]*)_o(?P<obs>[^_]+)_v(?P<v>\d+)_vg(?P<vg>\d+)_p(?P<p>\d+)\.png$")
GR_RE = re.compile(r"G\d{3}[MH]|PRISM|F\d{3}W2?")
#: PNG variant -> gallery role. The un-annotated plot (all filter curves) is the ``sed`` role.
PNG_ROLES = {"2221": ("c2221", "2221 filters"), "1182": ("c1182", "1182 filters"),
             None: ("sed", "Spectrum + filter curves")}
#: the nine legacy preset galleries as facet presets: (key, label, facets, role)
GALLERY_PRESETS = (("prism", "PRISM", {"grating": ["PRISM"]}, None),
                   ("prism_2221", "PRISM, 2221 filters", {"grating": ["PRISM"]}, "c2221"),
                   ("p1611", "1611 Serpens", {"program": ["1611"]}, None),
                   ("p1611_broad", "1611 broad (1182 filters)", {"program": ["1611"]}, "c1182"),
                   ("p1611_narrow", "1611 narrow (2221 filters)", {"program": ["1611"]}, "c2221"),
                   # legacy regex /Serpens.*G395HG235H/ matched no file: 1611 pairs G395H with G235M
                   ("p1611_two", "1611 two gratings (G235M + G395H)",
                    {"grating": ["G235M+G395H", "G395H+G235M"], "program": ["1611"]}, None),
                   ("p3222", "3222 IRAS16293", {"program": ["3222"]}, None),
                   ("p3222_broad", "3222 broad (1182 filters)", {"program": ["3222"]}, "c1182"),
                   ("p3222_narrow", "3222 narrow (2221 filters)", {"program": ["3222"]}, "c2221"))


def _fid(f: str) -> str:
    return f"JWST/NIRCam.{f}"


def _num(x):
    """float, or None for missing / masked / non-finite values."""
    if x is None or np.ma.is_masked(x):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError, np.ma.MaskError):
        return None
    return v if np.isfinite(v) else None


def _s(v) -> str:
    """JWST_Archive_ColorPortal.build_sources._s: '' for masked / None / '--'."""
    if v is None or np.ma.is_masked(v) or str(v) in ("--", "None"):
        return ""
    return str(v).strip()


def xid(x1d: str) -> str:
    """Spectrum id from the x1d basename: the m/h pairing prefix plus the grating token."""
    p = os.path.basename(x1d).split("_")
    if len(p) < 4:
        raise ValueError(f"not an x1d product name: {x1d!r}")
    return "_".join(p[:-3]) + "_" + p[-2]


def _surl(path) -> str:
    p = os.path.normpath(os.path.abspath(os.fspath(path)))
    if not p.startswith(JWST_ROOT):
        raise ValueError(f"{p} is not under {JWST_ROOT} (no secure URL)")
    return SECURE + quote(p[len(JWST_ROOT):])


def _purl(png: str) -> str:
    return PUBLIC_PNG + quote(png)


def _cached_payload(ctx, path: Path, name: str):
    from portalkit.legacy import extract
    return ctx.cache.memo(f"jwa_payload_v1_{path.name}_{name}",
                          lambda: {e["name"]: e["value"] for e in extract.payloads(path, names=(name,))}[name],
                          inputs=[path])


def _colour_presets():
    """Axes presets (key, label, x, y) of both legacy preset lists, CCDs then marginal CMDs."""
    out, seen = [], set()
    for label, xb, xr, yb, yr in PRESETS + W51_PRESETS:
        if (xb, xr, yb, yr) not in seen:
            seen.add((xb, xr, yb, yr))
            out.append((f"ccd_{xb}_{xr}_{yb}_{yr}".lower(), " ".join(label.split()), f"{xb}-{xr}", f"{yb}-{yr}"))
    for _, xb, xr, yb, yr in PRESETS + W51_PRESETS:
        for b, r in ((xb, xr), (yb, yr)):
            if (b, r) not in seen:
                seen.add((b, r))
                out.append((f"cmd_{b}_{r}".lower(), f"{r} vs {b}-{r}", f"{b}-{r}", r))
    return out


class JWSTArchiveAdapter:
    name = "jwst_archive"
    tiers = ("A", "B")

    # -- raw rows per tier: one dict per spectrum ---------------------------------------------
    def _tier_a(self, ctx) -> list[dict]:
        P = _cached_payload(ctx, PORTAL_DIR / "index.html", "PORTAL")
        if tuple(P["filters"]) != FILTERS:
            raise ValueError(f"legacy PORTAL filters changed: {P['filters']}")
        rows = []
        for s in P["sources"]:
            m = [_num(x) for x in s["m"]]
            rows.append({"x1d": s["f"], "i": int(s["i"]), "t": s["t"], "o": s["o"], "p": s["p"], "g": s["g"],
                         "g2": s["g2"], "sl": s["sl"], "ob": s["ob"], "v": s["v"], "vg": s["vg"],
                         "wmin": _num(s["wmin"]), "wmax": _num(s["wmax"]), "em": int(s["em"] or 0), "ck": s["ck"],
                         "ra": _num(s["ra"]), "dec": _num(s["dec"]), "npix": int(s["npix"]), "m": m,
                         "cg": [int(c) for c in s["cg"]]})
        return rows

    def _tier_b(self, ctx) -> list[dict]:
        from astropy.table import Table
        from portalkit.physics.filters import load_registry
        ctx.inputs.add(TABLE)
        t = Table.read(TABLE)
        reg = load_registry()
        zp = {f: float(reg[_fid(f)]["zp_jy"]) for f in FILTERS}
        ctx.inputs.add(LEGACY_SRC)        # the _fallback restatements follow this file
        coords, cov = self._legacy_coverage(ctx, t)
        n = len(t) if ctx.limit is None else min(len(t), int(ctx.limit))
        rows = []
        for i in range(n):
            r = t[i]
            fn = str(r["Filename"])
            m = []
            for f in FILTERS:
                v = _num(r[f"{_fid(f)}_flx"]) if f"{_fid(f)}_flx" in t.colnames else None
                m.append(round(-2.5 * np.log10(v / zp[f]), 4) if v is not None and v > 0 else None)
            if coords is not None:
                ra, dec, npix = _num(coords["ra"][i]), _num(coords["dec"][i]), int(coords["npix"][i])
                cg = [int(round(1000 * float(cov[f"gap_{f}"][i]))) for f in FILTERS]
            else:
                info = x1d_info(ctx, fn)
                ra, dec, npix = info["ra"], info["dec"], info["npix"]
                # registry curves (64 samples) put a fully sampled band at 999-1000 permille
                cg = [COV_FULL if c >= COV_FULL - COV_TOL else c for c in info["cg"]]
            em = _num(r["emission_line"])
            rows.append({"x1d": os.path.basename(fn), "i": i, "t": _s(r["Target"]), "o": _s(r["Object"]),
                         "p": _s(r["Program"]), "g": _s(r["Grating"]), "g2": _s(r["Grating2"]), "sl": _s(r["SlitID"]),
                         "ob": _s(r["Observation"]), "v": _s(r["Visit"]), "vg": _s(r["VisitGroup"]),
                         "wmin": _num(r["wl_min"]), "wmax": _num(r["wl_max"]), "em": int(em) if em else 0,
                         "ck": _s(r["ISOKN"]), "ra": None if ra is None else round(ra, 6),
                         "dec": None if dec is None else round(dec, 6), "npix": npix, "m": m, "cg": cg,
                         "cov_x1d": coords is None})
        return rows

    def _legacy_coverage(self, ctx, t):
        """The legacy portal_coords / portal_coverage ECSVs, or (None, None) when they may
        not line up with ``t`` row by row. They carry no file name, so they are used only
        when (a) both have len(t) rows, (b) both are at least as new as the fluxes table
        (legacy --skip-spectra reuses them unchanged) and (c) the PORTAL payload written in
        the same legacy run lists the x1d files of ``t`` in the same order."""
        cc, cv = PORTAL_DIR / "portal_coords.ecsv", PORTAL_DIR / "portal_coverage.ecsv"
        why = None
        if not (cc.is_file() and cv.is_file()):
            why = "missing"
        elif min(cc.stat().st_mtime, cv.stat().st_mtime) < TABLE.stat().st_mtime:
            why = "older than the fluxes table"
        else:
            from astropy.table import Table
            ctx.inputs.add(cc)
            ctx.inputs.add(cv)
            coords, cov = Table.read(cc), Table.read(cv)
            if len(coords) != len(t) or len(cov) != len(t):
                why = f"{len(coords)}/{len(cov)} rows, fluxes table {len(t)}"
            else:
                try:
                    P = _cached_payload(ctx, PORTAL_DIR / "index.html", "PORTAL")
                    names = [str(s["f"]) for s in P["sources"]]
                except (OSError, KeyError, ValueError) as err:
                    names, why = None, f"legacy PORTAL payload unreadable ({err})"
                if names is not None and names != [os.path.basename(str(f)) for f in t["Filename"]]:
                    why = "row order differs from the fluxes table"
                if why is None:
                    return coords, cov
        ctx.log(f"[jwst_archive] legacy portal_coords/portal_coverage not used ({why}); recomputing "
                f"positions and band coverage from the x1d files (registry filter curves, full band >= "
                f"{COV_FULL - COV_TOL} permille)")
        return None, None

    # -- datasets -------------------------------------------------------------------------------
    def datasets(self, ctx):
        from portalkit.spec import ExtinctionSpec, FilterSetSpec, RecordSetSpec, TemplateSpec, TierUnavailable
        if ctx.tier not in self.tiers:
            raise TierUnavailable(f"jwst_archive implements tiers A and B, not {ctx.tier}")
        rows = self._tier_a(ctx) if ctx.tier == "A" else self._tier_b(ctx)
        if ctx.limit is not None:
            rows = rows[:int(ctx.limit)]
        for r in rows:
            r["id"] = xid(r["x1d"])
            r["rel"] = f"{r['x1d'][:-len('_x1d.fits')]}/{r['x1d']}"
        by = {r["id"]: r for r in rows}
        if len(by) != len(rows):
            raise ValueError("x1d-derived ids are not unique")
        ids = list(by)
        spec = RecordSetSpec("spec", "pks1", "src", ids, lambda n: self._spectrum(ctx, by, n), enc={"flux": "f32"})
        # w51 reads its own copy (not a foreign record set onto the archive portal): the two
        # portals' secure areas are different password files (see the module docstring)
        ds = {"src": self._src(ctx, rows), "spec": spec, "arch": self._arch(rows), "bg": self._bg(ctx),
              "pngs": self._pngs(ctx, rows), "hips": self._hips(rows),
              "tpl": TemplateSpec(), "flt": FilterSetSpec(keys=[_fid(f) for f in FILTERS]), "ext": ExtinctionSpec()}
        ds.update(self._readmes(ctx, rows, ds))
        return ds

    def _src(self, ctx, rows):
        from astropy.coordinates import SkyCoord
        from portalkit.spec import ManifoldSpec, Preset, VirtualDim, catalog_from_rows
        gal = [(None, None)] * len(rows)
        pos = [i for i, r in enumerate(rows) if r["ra"] is not None and r["dec"] is not None]
        if pos:
            c = SkyCoord([rows[i]["ra"] for i in pos], [rows[i]["dec"] for i in pos], unit="deg").galactic
            for k, i in enumerate(pos):
                gal[i] = (round(float(c.l.deg[k]), 5), round(float(c.b.deg[k]), 5))
        out = []
        for r, (gl, gb) in zip(rows, gal):
            d = {"id": r["id"], "ra": r["ra"], "dec": r["dec"], "Target": r["t"] or None, "Object": r["o"] or None,
                 "Program": r["p"] or None, "Grating": r["g"] or None, "Grating2": r["g2"] or None,
                 "SlitID": r["sl"] or None, "Observation": r["ob"], "Visit": r["v"], "VisitGroup": r["vg"],
                 "Filename": r["rel"], "checklist": r["ck"] or None, "emission": bool(r["em"]),
                 "wl_min": r["wmin"], "wl_max": r["wmax"], "npix": r["npix"], "gl": gl, "gb": gb}
            for j, f in enumerate(FILTERS):
                d[f], d[f"cov_{f}"] = r["m"][j], r["cg"][j] >= COV_FULL
            out.append(d)
        text = ("Target", "Object", "SlitID", "Observation", "Visit", "VisitGroup", "Filename")
        roles = {**{k: "text" for k in text},
                 **{k: "category" for k in ("Program", "Grating", "Grating2", "checklist")},
                 "emission": "flag", **{k: "value" for k in ("wl_min", "wl_max", "npix", "gl", "gb")}}
        present = lambda k: {str(d[k]) for d in out if d[k]}  # noqa: E731
        cols = {"Grating": {"colors": {k: v for k, v in GRATING_COLORS.items() if k in present("Grating")} or None},
                "checklist": {"label": "Checklist flag (ISOKN)",
                              "colors": {k: v for k, v in CHECK_COLORS.items() if k in present("checklist")} or None},
                "emission": {"label": "Pa-a / Br-a emission (2x ratio)"},
                "Filename": {"note": "x1d product, relative to mastDownload/JWST"},
                "wl_min": {"unit": "um", "group": "meta"}, "wl_max": {"unit": "um", "group": "meta"},
                "npix": {"label": "finite samples", "group": "meta"},
                "gl": {"unit": "deg", "group": "pos"}, "gb": {"unit": "deg", "group": "pos"}}
        for f in FILTERS:
            roles[f], roles[f"cov_{f}"] = "mag", "flag"
            cols[f] = {"filter": _fid(f), "unit": "mag", "step": 0.001, "group": "phot", "validity": f"cov_{f}",
                       "label": f}
            cols[f"cov_{f}"] = {"group": "coverage", "label": f"{f} band fully sampled",
                                "note": "throughput-weighted gap fraction = 1 (detector gaps, grating edges)"}
        cat = catalog_from_rows(out, "id", "ra", "dec", roles=roles, key="src", columns=cols,
                                title=f"JWST NIRSpec archive spectra ({len(out):,})",
                                aliases=self._aliases(ctx, rows))
        _numeric_types(cat)
        pres = _colour_presets()
        virt = sorted({e for _, _, x, y in pres for e in (x, y) if "-" in e})
        cat.virtual += [VirtualDim(v, label=v.replace("-", " - "), unit="mag", group="colors") for v in virt]
        cat.presets += [Preset(k, lab, "axes", {"x": x, "y": y}) for k, lab, x, y in pres]
        cat.presets += [Preset(k, lab, "axes", {"x": x, "y": y, "note": note}) for k, lab, x, y, note in MANI_PRESETS]
        dims = list(FILTERS) + virt + ["wl_min", "wl_max"]
        cat.manifold = ManifoldSpec(dims=dims, frames={"pca": pca_frame(out, dims)}, default_frame="pca")
        cat.caveats += ["Magnitudes are synthesized from each spectrum through the NIRCam curves (Vega); a "
                        "magnitude counts only where its band is fully sampled (cov_<filter>).",
                        "NIRSpec slit losses affect magnitudes more than colours; PRISM narrow-band colours "
                        "are smoothed quantities."]
        return cat

    def _aliases(self, ctx, rows) -> dict:
        """x1d basename, plus the legacy manifold star name where it maps to one spectrum."""
        al = {r["id"]: [r["x1d"]] for r in rows}
        mp = PORTAL_DIR / "archive_manifold.html"
        if not mp.is_file():
            return al
        from portalkit.legacy import extract
        stars = ctx.cache.memo("jwa_mani_stars_v1", lambda: [
            [s["n"], s.get("t"), s.get("prog"), s.get("grating"), s.get("ra"), s.get("dec")]
            for s in {e["name"]: e["value"] for e in extract.payloads(mp, names=("DATA",))}["DATA"]["stars"]],
            inputs=[mp])
        key = defaultdict(list)
        for r in rows:
            key[(r["t"], r["p"], r["g"])].append(r)
        taken = {a for v in al.values() for a in v} | set(al)
        for n, t, prog, g, ra, dec in stars:
            hit = [r for r in key.get((t, str(prog), g), []) if ra is not None and r["ra"] is not None
                   and abs(r["ra"] - ra) < 2e-4 and abs(r["dec"] - dec) < 2e-4]
            if len(hit) == 1 and n not in taken:
                al[hit[0]["id"]].append(n)
                taken.add(n)
        return al

    def _arch(self, rows):
        """Context density of every covered archive spectrum (the colour-portal backdrop)."""
        from astropy.table import Table
        from portalkit.spec import CatalogSpec, ColumnSpec
        t = Table({"id": [f"a{i}" for i in range(len(rows))],
                   **{f: np.array([np.nan if r["m"][j] is None or r["cg"][j] < COV_FULL else r["m"][j]
                                   for r in rows], float) for j, f in enumerate(FILTERS)},
                   **{k: np.array([np.nan if r[c] is None else r[c] for r in rows], float)
                      for k, c in (("wl_min", "wmin"), ("wl_max", "wmax"))}})
        return _context(t, FILTERS, "arch", f"Archive spectra density ({len(t):,} spectra, covered bands)",
                        ColumnSpec, CatalogSpec, nbin=128, extra=("wl_min", "wl_max"))

    def _bg(self, ctx):
        from astropy.table import Table
        from portalkit.spec import CatalogSpec, ColumnSpec
        a = ctx.cache.memo("jwa_w51bg_v1_e0.1_b4_n30000", lambda: w51_background(W51_CAT), inputs=[W51_CAT])
        flt = [f for f in FILTERS if f in a]
        n = len(a[flt[0]]) if ctx.limit is None else min(len(a[flt[0]]), 20 * int(ctx.limit))
        t = Table({"id": [f"w51_{i}" for i in range(n)], **{f: np.asarray(a[f][:n], float) for f in flt}})
        return _context(t, flt, "bg", f"W51 NIRCam photometry ({n:,} sources, 4+ good bands)", ColumnSpec,
                        CatalogSpec, nbin=192,
                        caveats=["Per band: qfit < 0.4, |cfit| < 0.1, emag_ab < 0.1, not saturated; sources "
                                 "with 4+ good bands, randomly subsampled to 30,000 (seed 0)."])

    # -- records ---------------------------------------------------------------------------------
    def _spectrum(self, ctx, by, name):
        from portalkit.spec import Spectrum
        r = by.get(name)
        if r is None:
            return None
        if ctx.tier == "A":
            p = PORTAL_DIR / "spectra" / f"{r['i']}.json"
            if not p.is_file():
                return None
            ctx.inputs.add(p)
            sd = json.loads(p.read_text(encoding="utf-8"))
            w = np.asarray([np.nan if x is None else x for x in sd.get("w") or []], float)
            f = np.asarray([np.nan if x is None else x for x in sd.get("f") or []], float)
        else:
            fn = str(ARCH / r["rel"])
            if not os.path.isfile(fn):
                return None
            files = [fn] + partner_files(fn)[:1]
            a = ctx.cache.memo(f"jwa_spec_v1_{name}", lambda: _spec_arrays(fn), inputs=files)
            w, f = np.asarray(a["w"], float), np.asarray(a["f"], float)
        keep = np.isfinite(w)
        w, f = w[keep], f[keep]
        if w.size == 0:
            return None
        o = np.argsort(w, kind="stable")
        w, f = w[o], f[o]
        g = r["g"] if not r["g2"] or r["g2"] == r["g"] else f"{r['g']}+{r['g2']}"
        return Spectrum(wave=w, channels={"flux": f}, units={"flux": "Jy"}, segments=[(0, int(w.size), g or "?")],
                        meta={"id": name, "x1d": r["x1d"], "target": r["t"], "gratings": g, "npix": r["npix"]})

    # -- images, sky, readme ---------------------------------------------------------------------
    def _pngs(self, ctx, rows):
        from portalkit.spec import ImageSetSpec, Preset
        ctx.inputs.add(PNG_DIR)
        stems: dict = {}
        for fn in sorted(os.listdir(PNG_DIR)):
            m = PNG_RE.match(fn)
            if m:   # names without the legacy pattern are skipped
                k = (m["head"], m["gr"], m["slit"], m["obs"], m["v"], m["vg"], m["p"])
                stems.setdefault(k, {})[PNG_ROLES[m["set"]][0]] = fn
        idx = defaultdict(list)
        for r in rows:
            idx[(r["p"], r["ob"], r["v"], r["vg"], r["x1d"].split("_")[1], r["g"])].append(r)
        exact, loose, other = [], [], []
        for k, files in stems.items():
            head, gr, slit, obs, v, vg, p = k
            grs = GR_RE.findall(gr)
            try:
                key = (str(int(p)), str(int(obs)), str(int(v)), str(int(vg)), slit, grs[0])
            except ValueError:
                key = None
            row = _png_row(idx.get(key, []), gr, obs)
            it = {"stem": "_".join(k[:2]) + f"_{slit}_o{obs}_v{v}_vg{vg}_p{p}", "row": row, "files": files,
                  "program": str(int(p)) if p.isdigit() else p, "grating": "+".join(grs),
                  "target": row["t"] if row else head.replace("_", " "), "ck": row["ck"] if row else None}
            if row is None:
                other.append(it)
            elif row["g"] + (row["g2"] if row["g2"] != row["g"] else "") == gr:
                exact.append(it)
            else:
                loose.append(it)
        if ctx.limit is not None:
            other = other[:int(ctx.limit)]
        taken, out = set(), []
        for it in exact + loose + other:   # exact grating match claims the catalogue id first
            iid = it["row"]["id"] if it["row"] and it["row"]["id"] not in taken else it["stem"]
            taken.add(iid)
            sets = sorted({"1182" if rk == "c1182" else "2221" if rk == "c2221" else "all" for rk in it["files"]})
            out.append({"id": iid, "caption": f"{it['target']} · {it['grating']} · o{it['stem'].split('_o')[-1]}",
                        "program": it["program"], "target": it["target"], "grating": it["grating"],
                        "filterset": "+".join(sets), "checklist": it["ck"] or None,
                        **{rk: _purl(fn) for rk, fn in it["files"].items()}})
        out.sort(key=lambda d: (d["program"], d["target"], d["id"]))
        # bundle/images.py now carries a preset's role through to the served document
        # (WPE9 item 4; gallery.js applies it alongside facets) -- but schema.py's pk-images
        # preset validator (its `_keys(..., ("key", "label", "facets"))` call) was not
        # updated to allow it, so a preset literally carrying "role" fails `check_build`
        # with "unknown key 'role'" (docs/IMPLEMENTATION_NOTES.md #148; schema.py not
        # owned by this WP, see contract_issues). Emitting every GALLERY_PRESETS entry
        # (role-carrying ones included) stays blocked on that until schema.py adds "role"
        # as an optional key there (validated against the same `role_keys` the item-roles
        # check already uses). Until then: the in-page preset menu applies facets only, so
        # it lists the facet-distinct presets; the role variants live in the legacy stub
        # hashes.
        pre = [Preset(k, lab, "filter", {"facets": fac}) for k, lab, fac, role in GALLERY_PRESETS if role is None]
        return ImageSetSpec.from_rows(out, {rk: (rk, lab) for rk, lab in PNG_ROLES.values()}, "pngs",
                                      tags=["program", "target", "grating", "filterset", "checklist"],
                                      catalog="src", presets=pre)

    def _hips(self, rows):
        from portalkit.spec import HipsSpec
        ra = np.array([r["ra"] for r in rows if r["ra"] is not None and r["dec"] is not None], float)
        dec = np.array([r["dec"] for r in rows if r["ra"] is not None and r["dec"] is not None], float)
        if not ra.size:
            return HipsSpec(base=[dict(b) for b in CDS_HIPS])
        # all-sky sample: open on the first spectrum with a wide field
        return HipsSpec(base=[dict(b) for b in CDS_HIPS],
                        target={"ra": float(ra[0]), "dec": float(dec[0]), "fov": 60.0 if ra.size > 1 else 0.1})

    def _readmes(self, ctx, rows, ds):
        from portalkit.spec import ReadmeSpec
        n, progs = len(rows), defaultdict(int)
        for r in rows:
            progs[r["p"]] += 1
        lim = "" if ctx.limit is None else f" (fixture build: first {ctx.limit} rows)"
        ncov = sum(c >= COV_FULL for r in rows for c in r["cg"])
        head = (f"{n:,} archival JWST NIRSpec spectra{lim} from {len(progs)} programs. Every point is a "
                f"spectrum: NIRCam magnitudes are synthesized by integrating each x1d spectrum (m/h grating "
                f"pairs stacked, flux > 1 Jy, < 0 and > mean + 10 sigma removed) through the NIRCam curves. "
                f"{ncov:,} of {n * len(FILTERS):,} filter bands are fully sampled.\n\n")
        cav = ("## Caveats\n\n- **Band coverage**: a magnitude is used only where the spectrum samples its "
               "whole band (gap fraction 1; detector gaps and grating edges remove it). The stricter legacy "
               "'count NaN-ed pixels' mode is not carried over.\n- Ids come from the x1d file name "
               "(`<program>-<obs>_<slit>_<grating>`); the legacy row index is gone (it went stale when the "
               "table was rebuilt).\n- Spectra are median-binned to at most 1500 samples.\n")
        nfb = sum(bool(r.get("cov_x1d")) for r in rows)
        if nfb:
            cav += (f"- Positions and band coverage of {nfb:,} spectra were recomputed from the x1d files "
                    f"(the legacy coverage tables were stale), with the 64-sample registry filter curves; a "
                    f"band counts as fully sampled at >= {(COV_FULL - COV_TOL) / 10:g} %, and partial "
                    f"coverage can differ from the legacy SVO grids by a few permille.\n")
        table = [{"type": "table", "columns": ["Program", "Spectra"],
                  "rows": [[p, k] for p, k in sorted(progs.items(), key=lambda x: (-x[1], x[0]))]}]
        bg = ds["bg"].table
        return {
            "readme": ReadmeSpec(markdown="# JWST NIRSpec archive: synthetic colours\n\n" + head
                                 + "## Pages\n\n- **Colours**: colour-colour diagram with two marginal CMDs, the "
                                 "spectrum, SED and sky position of each spectrum; presets, brushing, "
                                 "keep-only / invert, pin up to 8 spectra, velocity offset for line overlays.\n"
                                 "- **Manifold**: projections of the 29 synthesized magnitudes and colours.\n\n"
                                 + cav, blocks=table),
            "readme_w51": ReadmeSpec(markdown="# W51 photometry vs JWST archive spectra\n\n" + head
                                     + f"The context layer is the W51 NIRCam catalogue ({len(bg):,} sources "
                                     f"after the photometric-quality cuts), drawn as a density under the "
                                     f"archive spectra; the presets use the filters W51 (program 6151) "
                                     f"observed.\n\n" + cav, blocks=table),
            "readme_gal": ReadmeSpec(markdown="# JWST NIRSpec archive: spectrum PNGs\n\n" + head
                                     + f"{len(ds['pngs'].items):,} plots, one card per spectrum with the 2221 / "
                                     "1182 filter overlays and the plain plot as roles. Facets: program, target, "
                                     "grating, filter set, checklist flag. The checklist page stores "
                                     "classification codes in this browser only and exports them as CSV for "
                                     "`portalkit archive-checklist-diff`.\n\n" + cav, blocks=table)}

    # -- pages -----------------------------------------------------------------------------------
    def pages(self, ctx, ds):
        from portalkit.caps import SHARED_EXTINCTION_VECTOR as EXT
        from portalkit.spec import PageSpec as P
        url = {p.id: p.url for p in ctx.site.portals}
        phys = ["tpl", "flt", "ext"]
        bands = [{"label": lab, "w0": lo, "w1": hi} for lab, _, lo, hi in sorted(ICE_BANDS + GAS_BANDS,
                                                                                 key=lambda b: b[1])]
        lst = {"pane": "list", "dataset": "src", "size": 280,
               "options": {"columns": ["Target", "Program", "Grating", "checklist"]}}
        det = {"pane": "detail", "dataset": "src"}
        s1 = {"pane": "spectrum1d", "dataset": "spec",
              "options": {"records": "spec", "bands": bands, "templates": "tpl", "pin": 8}}
        sed = {"pane": "sed", "dataset": "src"}
        sky = {"pane": "sky", "dataset": "src", "options": {"hips": "hips"}}
        sc = lambda i, x, y, bg: {"pane": "scatter", "id": i, "dataset": "src",  # noqa: E731
                                  "options": {"x": x, "y": y, "background": bg, "overlays": [EXT], "presets": True}}

        def colours(bg, preset):
            _, xb, xr, yb, yr = preset
            return {"row": [{"col": [sc("ccd", f"{xb}-{xr}", f"{yb}-{yr}", bg),
                                     {"row": [sc("cmd_x", f"{xb}-{xr}", xr, bg), sc("cmd_y", f"{yb}-{yr}", yr, bg)],
                                      "size": 260}]},
                            {"col": [lst, sky], "size": 300}, {"col": [det, s1, sed], "size": 430}]}
        sec = lambda pid, page: url[pid] + f"{page}.html#id={{id}}"  # noqa: E731
        links = {"colors": "colors.html#id={id}", "manifold": sec("archive", "manifold"),
                 "w51": sec("w51", "colors"), "gallery": sec("gallery", "gallery")}
        alinks = {**links, "manifold": "manifold.html#id={id}"}
        glinks = {**links, "colors": sec("archive", "colors"), "gallery": "gallery.html#id={id}"}
        card = lambda pid, page, title, text, auth: {"href": url[pid] + f"{page}.html", "title": title,  # noqa: E731
                                                     "auth": auth, "text": text}
        gal = {"pane": "gallery", "dataset": "pngs", "options": {"images": "pngs", "roles": ["c2221", "c1182", "sed"]}}
        gdet = {**det, "options": {"images": "pngs"}}
        ann = {**gal, "options": {**gal["options"], "annotate": {"codes": CHECK_CODES, "key": "ISOKN"}}}
        state = {"color": {"by": "Grating"}}
        gstate = {"role": "sed"}
        return [
            P(id="index", archetype="landing", title="JWST NIRSpec archive portal", datasets=["readme"],
              portal="archive", layout={"pane": "landing", "options": {"counts": True, "search": True, "cards": [
                  {"page": "colors",
                   "text": "Synthetic colour-colour and colour-magnitude diagrams of every spectrum."},
                  {"page": "manifold", "text": "Projections of the synthesized magnitudes and colours."},
                  card("w51", "colors", "W51 photometry vs archive spectra", "The same spectra over the W51 "
                       "NIRCam density.", "secure"),
                  card("gallery", "gallery", "Spectrum PNG gallery", "Faceted gallery of the spectrum plots "
                       "(public).", "public")]}}),
            P(id="colors", archetype="explorer", title="Archive synthetic colours", portal="archive",
              subtitle="NIRSpec spectra through NIRCam filters", datasets=["src", "spec", "arch", "hips"] + phys,
              links=alinks, state=state, layout=colours("arch", PRESETS[0])),
            P(id="manifold", archetype="manifold", title="Archive manifold", portal="archive", links=alinks,
              datasets=["src", "spec", "arch", "hips"] + phys, state=state, manifold=True,
              layout={"row": [lst, {"pane": "manifold", "dataset": "src",
                                    "options": {"frame": "pca", "overlays": [EXT], "background": "arch"}},
                              {"col": [det, s1, sed, sky], "size": 430}]}),
            P(id="index", archetype="landing", title="W51 archive colours", datasets=["readme_w51"], portal="w51",
              layout={"pane": "landing", "options": {"counts": True, "search": True, "cards": [
                  {"page": "colors", "text": "Archive spectra over the W51 NIRCam photometry density."},
                  card("archive", "colors", "All-archive colour portal", "Without the W51 layer.", "secure"),
                  card("gallery", "gallery", "Spectrum PNG gallery", "Faceted gallery (public).", "public")]}}),
            P(id="colors", archetype="explorer", title="W51 photometry vs JWST archive spectra", portal="w51",
              subtitle="archive spectra over W51 NIRCam", datasets=["src", "spec", "bg", "hips"] + phys,
              links={**links, "colors": "colors.html#id={id}", "archive": sec("archive", "colors")}, state=state,
              layout=colours("bg", W51_PRESETS[0])),
            P(id="index", archetype="landing", title="JWST NIRSpec archive spectra gallery", datasets=["readme_gal"],
              portal="gallery", layout={"pane": "landing", "options": {"counts": True, "search": True, "cards": [
                  {"page": "gallery", "text": "Every spectrum plot, faceted by program, target, grating and "
                                              "filter set."},
                  {"page": "checklist", "text": "Classify spectra (y/n/g/w/l/e) and export the codes as CSV."},
                  card("archive", "colors", "Colour portal (login)", "Synthetic colours of the same spectra.",
                       "secure")]}}),
            P(id="gallery", archetype="gallery", title="Archive spectrum PNGs", portal="gallery",
              datasets=["pngs", "src", "spec", "hips"] + phys, links=glinks, state=gstate,
              layout={"row": [lst, gal, {"col": [gdet, s1, sky], "size": 400}]}),
            P(id="checklist", archetype="gallery", title="Archive spectra checklist", portal="gallery",
              subtitle="annotate: y all covered, n not, g gap, w weird, l low S/N, e emission",
              datasets=["pngs", "src", "spec", "hips"] + phys, links=glinks, state=gstate,
              layout={"row": [lst, ann, {"col": [gdet, s1, sky], "size": 400}]}),
        ]


ADAPTER = JWSTArchiveAdapter()


def foreign_base(page_url: str, archive_url: str) -> str:
    """Page-relative URL of the archive portal's ``data/`` as seen from a portal at
    ``page_url`` (pages sit at the portal root). Both URLs must share scheme and host;
    the foreign-records rule needs the same host and resolves the base against the page."""
    import posixpath
    from urllib.parse import urlsplit
    a, b = urlsplit(page_url), urlsplit(archive_url)
    if (a.scheme, a.netloc) != (b.scheme, b.netloc):
        raise ValueError(f"{archive_url} is not on the host of {page_url}")
    rel = posixpath.relpath(b.path.rstrip("/") + "/data", a.path.rstrip("/") or "/")
    return rel + "/"


def _context(t, flt, key, title, ColumnSpec, CatalogSpec, nbin, caveats=(), extra=()):
    """Context catalogue with one density grid per preset plane whose filters it holds."""
    from portalkit.spec import DensitySpec, Preset
    col = {f: np.asarray(t[f], float) for f in flt}

    def expr(e):
        a, _, b = e.partition("-")
        return col[a] - col[b] if b else col[a]

    def lim(x):                       # 0.5/99.5 percentile + 8 % pad (ColorColor.robust_limits)
        x = x[np.isfinite(x)]
        if x.size < 2:
            return (0.0, 1.0)
        lo, hi = np.nanpercentile(x, [0.5, 99.5])
        pad = 0.08 * (hi - lo) or 0.5
        return (round(float(lo - pad), 3), round(float(hi + pad), 3))
    dens = [DensitySpec(k, x, y, nx=nbin, ny=nbin, xr=lim(expr(x)), yr=lim(expr(y)))
            for k, _, x, y in _colour_presets() if set(re.split("-", x + "-" + y)) <= set(flt)]
    return CatalogSpec(key=key, table=t, id="id", context=True, density=dens, exact_rows=False,
                       columns=[ColumnSpec(f, "mag", label=f, unit="mag", step=0.001) for f in flt]
                       + [ColumnSpec(k, "value", unit="um", step=0.001) for k in extra],
                       presets=[Preset(d.key, d.key, "axes", {"x": d.x, "y": d.y}) for d in dens],
                       title=title, caveats=list(caveats))


def _numeric_types(cat) -> None:
    """catalog_from_rows types a field with no present value as str; restore float / bool
    for numeric and flag roles (a band no fixture spectrum covers, or N = 0)."""
    from astropy.table import MaskedColumn
    for c in cat.columns:
        col = cat.table[c.key]
        if col.dtype.kind in "US" and c.role in ("mag", "flux", "value", "flag"):
            n = len(col)
            data = np.zeros(n, bool) if c.role == "flag" else np.full(n, np.nan)
            cat.table[c.key] = MaskedColumn(data, name=c.key, mask=np.ones(n, bool))
    for k in (cat.ra, cat.dec):
        if k and cat.table[k].dtype.kind in "US":
            cat.table[k] = MaskedColumn(np.full(len(cat.table), np.nan), name=k, mask=np.ones(len(cat.table), bool))


def _png_row(cand: list[dict], gr: str, obs: str) -> dict | None:
    """The catalogue row of one PNG among the rows sharing its (program, obs, visit, visit
    group, slit, first grating) key: narrowed by the PNG's grating token, then, when a
    combined (``-cNNNN``) and a per-observation (``-oNNN``) product both remain, by the
    PNG's ``o<obs>`` (the plots are made per observation)."""
    if len(cand) > 1:
        cand = [r for r in cand if r["g"] + (r["g2"] if r["g2"] != r["g"] else "") == gr]
    if len(cand) > 1:
        own = [r for r in cand if r["x1d"].split("_")[0].endswith(f"-o{obs}")]
        cand = own if own else cand
    return cand[0] if len(cand) == 1 else None


def pca_frame(rows: list[dict], dims: list[str]) -> list[float]:
    """Legacy ``pca`` preset: first two principal components of F162M, F210M, F360M, F480M over
    the spectra with all four covered, in the manifold's robust standardization (median,
    1.4826 MAD); the identity pair of the first two dims when fewer than 3 rows qualify."""
    core = ["F162M", "F210M", "F360M", "F480M"]
    D = len(dims)
    F = [0.0] * (2 * D)
    X = np.array([[np.nan if d[f] is None or not d[f"cov_{f}"] else d[f] for f in core] for d in rows],
                 float).reshape(-1, len(core))
    X = X[np.isfinite(X).all(axis=1)]
    if len(X) < 3:
        F[0], F[D + 1] = 1.0, 1.0
        return F
    med = np.median(X, axis=0)
    mad = 1.4826 * np.median(np.abs(X - med), axis=0)
    Z = (X - med) / np.where(mad > 0, mad, 1.0)
    _, _, vt = np.linalg.svd(Z - Z.mean(axis=0), full_matrices=False)
    for r in range(2):
        v = vt[r] * (1 if vt[r].sum() >= 0 else -1)
        for k, f in enumerate(core):
            F[r * D + dims.index(f)] = round(float(v[k]), 6)
    return F


# -- _fallback ------------------------------------------------------------------------------
# Restatements of /orange/adamginsburg/jwst/brick/analysis/JWST_Archive_ColorPortal.py
# (sha256 9eb21983ed3e291dac34a1c0e07095987f26161e59cabe054f6ac8478229784a, 2026-10-11),
# whose module imports JWST_Archive_Spectra (SVO queries at import time) and cannot be
# imported through the shim. Source lines: PRESETS 83, W51_PRESETS 106, ICE_BANDS 123,
# GAS_BANDS 141, clean_flux 252, filter_grids/band_coverage 288-346, stacked_spectrum 348,
# downsample 392, insert_gaps 418, build_background 561. Tier B registers the file as a
# build input (LEGACY_SRC), so a change to it shows in the build's input digests.

def partner_files(fn: str) -> list[str]:
    """x1d files sharing the pairing prefix of ``fn`` with a different grating token, sorted
    (JWST_Archive_ColorPortal.stacked_spectrum: the file itself and same-grating copies are
    dropped)."""
    base = os.path.basename(fn)
    pre, own = "_".join(base.split("_")[:-3]), base.split("_")[-2]
    root = os.path.dirname(os.path.dirname(os.path.abspath(fn)))
    out = []
    for d in sorted(os.listdir(root)):
        p = d.split("_")
        if len(p) >= 3 and "_".join(p[:-2]) == pre and p[-1] != own:
            f = os.path.join(root, d, f"{d}_x1d.fits")
            if os.path.isfile(f) and os.path.abspath(f) != os.path.abspath(fn):
                out.append(f)
    return out


def _stack(fn: str):
    """(wavelength, cleaned flux, primary header) of ``fn`` stacked with its first partner."""
    from astropy.io import fits
    import warnings
    from astropy.table import Table, vstack
    from astropy.units import UnitsWarning
    from astropy.utils.metadata import MergeConflictWarning
    with fits.open(fn) as fh:
        header = dict(fh[0].header)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UnitsWarning)      # '(MJy/sr)^2' error-column units
        warnings.simplefilter("ignore", MergeConflictWarning)
        t = Table.read(fn, hdu=1)
        other = partner_files(fn)
        if header.get("GRATING") != "PRISM" and other:
            t = vstack([t, Table.read(other[0], hdu=1)])
            t.sort("WAVELENGTH")
    return np.array(t["WAVELENGTH"], float), clean_flux(t["FLUX"]), header


def _spec_arrays(fn: str) -> dict:
    wl, flux, _ = _stack(fn)
    dw, df = downsample(wl, flux)
    return {"w": dw, "f": insert_gaps(dw, df)}


def x1d_info(ctx, fn: str) -> dict:
    """Coordinates, finite-sample count and per-filter gap coverage (permille) of one x1d,
    memoised (used when the legacy portal_coords / portal_coverage tables are stale)."""
    def load():
        from portalkit.physics.filters import load_registry
        reg = load_registry()
        wl, flux, h = _stack(fn)
        grids = {}
        for f in FILTERS:
            w, t = np.asarray(reg[_fid(f)]["trans_w"], float), np.asarray(reg[_fid(f)]["trans_t"], float)
            keep = t > 0.01 * t.max()
            o = np.argsort(w[keep])
            grids[f] = (w[keep][o], t[keep][o], float(t[keep].sum()))
        gap, _ = band_coverage(wl, flux, grids)
        ra, dec = h.get("SLIT_RA", h.get("TARG_RA")), h.get("SLIT_DEC", h.get("TARG_DEC"))
        return {"ra": _num(ra), "dec": _num(dec), "npix": int(np.isfinite(flux).sum()),
                "cg": [int(round(1000 * gap[f])) for f in FILTERS]}
    if not os.path.isfile(fn):
        return {"ra": None, "dec": None, "npix": 0, "cg": [0] * len(FILTERS)}
    return ctx.cache.memo(f"jwa_x1dinfo_v1_{os.path.basename(fn)}", load, inputs=[fn] + partner_files(fn)[:1])


def w51_background(path, max_emag=0.1, min_bands=4, max_rows=30000, seed=0) -> dict:
    """JWST_Archive_ColorPortal.build_background cuts, as float32 magnitudes per filter."""
    from astropy.io import fits
    good = {}
    with fits.open(path, memmap=True) as h:
        d = h[1].data
        names = set(d.columns.names)
        for f in FILTERS:
            lf = f.lower()
            if f"mag_vega_{lf}" not in names:
                continue
            m, q, c, e = (np.asarray(d[f"{p}_{lf}"], float) for p in ("mag_vega", "qfit", "cfit", "emag_ab"))
            sat = np.asarray(d[f"is_saturated_{lf}"], bool)
            with np.errstate(invalid="ignore"):
                ok = (np.isfinite(m) & np.isfinite(q) & (q < BG_MAX_QFIT) & np.isfinite(c) & (np.abs(c) < BG_MAX_CFIT)
                      & np.isfinite(e) & (e < max_emag) & ~sat)
            good[f] = np.where(ok, m, np.nan)
    if not good:
        raise ValueError(f"{path}: no mag_vega_<filter> columns")
    nb = np.sum([np.isfinite(v) for v in good.values()], axis=0)
    keep = np.where(nb >= min_bands)[0]
    if keep.size > max_rows:
        keep = np.sort(np.random.default_rng(seed).choice(keep, max_rows, replace=False))
    return {f: v[keep].astype(np.float32) for f, v in good.items()}


BG_MAX_QFIT, BG_MAX_CFIT = 0.4, 0.1

PRESETS = [
    ('F162M-F210M  vs  F360M-F480M', 'F162M', 'F210M', 'F360M', 'F480M'),
    ('F182M-F212N  vs  F405N-F466N  (CO ice)', 'F182M', 'F212N', 'F405N', 'F466N'),
    ('F162M-F210M  vs  F300M-F335M  (3 um ice)', 'F162M', 'F210M', 'F300M', 'F335M'),
    ('F115W-F200W  vs  F356W-F444W', 'F115W', 'F200W', 'F356W', 'F444W'),
    ('F200W-F356W  vs  F356W-F444W', 'F200W', 'F356W', 'F356W', 'F444W'),
    ('F405N-F410M  vs  F410M-F466N', 'F405N', 'F410M', 'F410M', 'F466N'),
    ('F212N-F410M  vs  F410M-F466N', 'F212N', 'F410M', 'F410M', 'F466N'),
    ('F250M-F300M  vs  F335M-F360M', 'F250M', 'F300M', 'F335M', 'F360M'),
    ('F140M-F162M  vs  F182M-F210M', 'F140M', 'F162M', 'F182M', 'F210M'),
    ('F182M-F250M  vs  F410M-F466N', 'F182M', 'F250M', 'F410M', 'F466N'),
    ('F162M-F210M  vs  F410M-F480M', 'F162M', 'F210M', 'F410M', 'F480M'),
    ('F210M-F300M  vs  F360M-F430M', 'F210M', 'F300M', 'F360M', 'F430M'),
    ('F300M-F335M  vs  F405N-F410M', 'F300M', 'F335M', 'F405N', 'F410M'),
]
W51_PRESETS = [
    ('F162M-F210M  vs  F360M-F480M', 'F162M', 'F210M', 'F360M', 'F480M'),
    ('F140M-F182M  vs  F182M-F410M', 'F140M', 'F182M', 'F182M', 'F410M'),
    ('F182M-F210M  vs  F210M-F480M', 'F182M', 'F210M', 'F210M', 'F480M'),
    ('F162M-F210M  vs  F410M-F480M', 'F162M', 'F210M', 'F410M', 'F480M'),
    ('F140M-F162M  vs  F182M-F210M', 'F140M', 'F162M', 'F182M', 'F210M'),
    ('F335M-F360M  vs  F405N-F410M', 'F335M', 'F360M', 'F405N', 'F410M'),
    ('F182M-F187N  vs  F405N-F410M  (Pa-a / Br-a excess)', 'F182M', 'F187N', 'F405N', 'F410M'),
    ('F210M-F335M  vs  F360M-F480M', 'F210M', 'F335M', 'F360M', 'F480M'),
    ('F150W-F210M  vs  F335M-F410M', 'F150W', 'F210M', 'F335M', 'F410M'),
]
#: JWST_Archive_Manifold presets without a colour-portal counterpart (key, label, x, y, note)
MANI_PRESETS = [
    ("cov", "Spectral coverage: blue edge against red edge", "wl_min", "wl_max",
     "Which wavelength range each spectrum samples; PRISM spans the whole plot, gratings cluster in bands."),
    ("sky", "Where the archive looks (Galactic)", "gl", "gb", "Galactic coordinates of every spectrum."),
]
ICE_BANDS = [
    ('H2O ice 1.5 um', 1.50, 1.45, 1.60), ('H2O ice 2.0 um', 2.02, 1.95, 2.10),
    ('CO2 ice comb. 2.70', 2.70, 2.68, 2.72), ('CO2 ice comb. 2.78', 2.78, 2.76, 2.80),
    ('H2O ice O-H stretch', 3.05, 2.72, 3.60), ('NH3 ice N-H', 2.96, 2.90, 3.02),
    ('CH4 ice', 3.32, 3.29, 3.35), ('3.47 um band', 3.47, 3.42, 3.52),
    ('CH3OH ice C-H', 3.53, 3.47, 3.60), ('CO2 ice nu3', 4.27, 4.21, 4.33),
    ('13CO2 ice', 4.39, 4.36, 4.42), ('OCN- (XCN)', 4.62, 4.58, 4.65),
    ('CO ice', 4.67, 4.63, 4.70), ('OCS ice', 4.90, 4.87, 4.93),
]
GAS_BANDS = [
    ('PAH 3.29 um', 3.29, 3.25, 3.33), ('aliphatic C-H 3.4', 3.40, 3.36, 3.44),
    ('CO gas fundamental v=1-0', 4.70, 4.40, 5.10), ('CO gas overtone v=2-0', 2.35, 2.29, 2.45),
]


def clean_flux(flux):
    """JWST_Archive_ColorPortal.clean_flux: NaN where flux > 1 Jy or < 0, then above
    mean + 10 sigma."""
    f = np.array(flux, dtype=float)
    with np.errstate(invalid="ignore"):
        f[f > MAX_FLUX] = np.nan
        f[f < 0] = np.nan
        f[~np.isfinite(f)] = np.nan
        if np.isfinite(f).any():
            mean, std = np.nanmean(f), np.nanstd(f)
            if np.isfinite(mean) and np.isfinite(std):
                f[f > mean + 10 * std] = np.nan
    return f


def band_coverage(wl, flux, grids):
    """JWST_Archive_ColorPortal.band_coverage: per-filter throughput fraction on real samples
    (``gap``: any sample; ``fin``: finite flux). ``grids``: {filter: (w, t, tsum)}."""
    wl, flux = np.asarray(wl, float), np.asarray(flux, float)
    order = np.argsort(wl)
    wraw, fsort = wl[order], flux[order]
    wgood = wraw[np.isfinite(fsort)]
    gap, fin = {}, {}
    for fid, (wf, tf, tsum) in grids.items():
        i0, i1 = np.searchsorted(wraw, [wf[0], wf[-1]])
        seg = wraw[max(i0 - 1, 0):i1 + 1]
        if seg.size < 2 or tsum <= 0:
            gap[fid] = fin[fid] = 0.0
            continue
        tol = 2.0 * float(np.median(np.diff(seg)))
        for warr, store in ((wraw, gap), (wgood, fin)):
            if warr.size < 2:
                store[fid] = 0.0
                continue
            idx = np.clip(np.searchsorted(warr, wf), 1, warr.size - 1)
            d = np.minimum(np.abs(warr[idx] - wf), np.abs(warr[idx - 1] - wf))
            store[fid] = float((tf * (d <= tol)).sum() / tsum)
    return gap, fin


def downsample(wl, flux, npt=NSPEC):
    """JWST_Archive_ColorPortal.downsample: median-bin onto at most ``npt`` samples."""
    wl, flux = np.asarray(wl, float), np.asarray(flux, float)
    n = wl.size
    if n <= npt:
        return wl, flux
    edges = np.linspace(0, n, npt + 1).astype(int)
    ow, of = np.full(npt, np.nan), np.full(npt, np.nan)
    for i in range(npt):
        a, b = edges[i], edges[i + 1]
        if b > a:
            wa, fa = wl[a:b], flux[a:b]
            if np.isfinite(wa).any():
                ow[i] = np.nanmean(wa)
            if np.isfinite(fa).any():
                of[i] = np.nanmedian(fa)
    keep = np.isfinite(ow)
    return ow[keep], of[keep]


def insert_gaps(wl, flux):
    """JWST_Archive_ColorPortal.insert_gaps: NaN after any step > 5x the median step."""
    if wl.size < 3:
        return flux
    d = np.diff(wl)
    med = np.nanmedian(d)
    if not np.isfinite(med) or med <= 0:
        return flux
    flux = np.array(flux, float)
    flux[np.where(d > GAP_FACTOR * med)[0] + 1] = np.nan
    return flux
