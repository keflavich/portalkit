"""Robitaille YSO model family adapter (WP21; ARCHITECTURE.md sections 9 and 14.6).

Legacy portal ``web/public/robitaille_models`` from ``Robitaille_Portal_Manifold.py``. Tier A reads
the payload embedded in ``manifold.html``; Tier B replays the sampling loop of the generator's
``main()`` (same seed, so the same rows), with the slow ``read_geometry`` loop memoized in
``ctx.cache``. Both feed :meth:`RobitailleAdapter._catalog`: 54 magnitudes and the physical values
are stored, the 171 colours and F480M at 8 kpc are virtual, FRIED rows are a category level and
the distance modulus is page state ``dm``. The "> 30 mag at 8 kpc" rule sets magnitudes fainter than
30 - 5 log10(8) to NaN in the table itself, so row chunks, quantized columns and density grids agree
(interim: the legacy axis-loading rule needs a client hook). SED curves come from the grid's
``flux.fits``; imaging is an image set. Cross-page links are per-row ``url`` columns, present only
on rows whose model exists on the other page.
"""

from __future__ import annotations

import collections
import hashlib
import inspect
import os
from pathlib import Path

import numpy as np

WEB = Path("/orange/adamginsburg/web/public/robitaille_models")
URL = "https://data.rc.ufl.edu/pub/adamginsburg/robitaille_models/"
GRID_ROOT = Path("/blue/adamginsburg/richardson.t/research/flux/r+24_models-1.2")
IMG_DIR = Path("/orange/adamginsburg/robitaille_models/modelgrids/imaging_run")
IMG_GEOM, IMG_PARAMS = "spubhmi", ("L", "Mdisk", "Menv", "rho0_env", "Tstar", "Rout_au")
IMG_LABELS = ("luminosity", "disk mass", "envelope mass", "envelope ρ₀", "T★", "outer radius")
IMG_UNITS = {"L": "L☉", "Mdisk": "M☉", "Menv": "M☉", "rho0_env": "g cm⁻³", "Tstar": "K", "Rout_au": "au"}
MODULE = "Robitaille_Portal_Manifold"
CATS = ("cls", "stg", "geom", "src", "cov", "det", "red")
DM8 = 5 * np.log10(8.0)                      # legacy DET_DIST_KPC = 8
MAG_HI = 30.0 - DM8                          # legacy CONTAM_MAG at 8 kpc, as a 1 kpc mag
M8 = f"m_NIRCam.F480M+{DM8:.4f}"
SKY_CAPS = ("sky_map_aladin", "catalog_markers_on_sky", "cdn_fallback", "hips_layer_stacking")
F32_VALUES = {"av"}                          # 0 .. 3e12 mag: u16q would decode every A_V < 5e7 as 0


def _logv(v, log):
    v = np.asarray(v, dtype=float)
    if not log:
        return v
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(v > 0, np.log10(v), np.nan)


def _sig(v, d=5):
    """Round to ``d`` significant digits (keeps the exact row chunks small)."""
    v = np.asarray(v, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        e = np.where(np.isfinite(v) & (v != 0), np.floor(np.log10(np.abs(v))), 0)
    return np.round(v / 10 ** e, d - 1) * 10 ** e


def _arr(v):
    return np.array([np.nan if x is None else x for x in v], dtype=float)


class RobitailleAdapter:
    name = "robitaille"
    tiers = ("A", "B")

    # -- tier A: payload embedded in manifold.html ------------------------------------------
    def _tier_a(self, ctx) -> dict:
        from portalkit.legacy import extract
        html = WEB / "manifold.html"
        ctx.inputs.add(html)
        p = ctx.cache.memo("robitaille_payload_v1", lambda: extract.payload(html), inputs=[html])
        filters = [f["key"][2:] for f in p["filters"]]
        dims = {d["key"]: d for d in p["dims"]}
        st = p["stars"]
        rows = {"geom": [s["geom"] for s in st], "name": [s["id"][len(s["geom"]) + 1:] for s in st],
                "sed": np.array([bool(s["sed"]) for s in st])}
        rows.update({k: [s[k] for s in st] for k in CATS if k != "geom"})
        rows["mag"] = np.column_stack([_arr(dims[f"m_{f}"]["v"]) for f in filters])
        meta = {}
        for k, d in dims.items():
            if (d["group"] == "color" and k != "redmin") or k.startswith("m_") or k == "m480_8kpc":
                continue
            meta[k] = (d["label"], d["unit"], d["group"], bool(d["log"]), d.get("note", ""))
            rows[k] = _arr(d["v"])            # log dims are stored as log10 already
        pairs = [tuple(k[2:].split("_")) for k, d in dims.items() if d["group"] == "color" and k.startswith("c_")]
        return {"rows": rows, "filters": filters, "pairs": pairs, "meta": meta, "cats": p["categories"],
                "groups": p["groups"], "presets": p["presets"], "preselect": p["preselect"],
                "scale": {k: d["scale"] for k, d in dims.items() if k.startswith("m_")},
                "ap_au": float(p["aperture_au"]), "n_rr": sum(s["src"] == "R&R" for s in st), "seed": None,
                "quota": max(collections.Counter(s["geom"] for s in st if s["src"] == "R&R").values(), default=0)}

    # -- tier B: legacy sampling loop ------------------------------------------------------
    def _tier_b(self, ctx) -> dict:
        mod = ctx.legacy.module(MODULE)
        root, fdir = mod.DEFAULT_ROOT, mod.DEFAULT_FRIED
        geoms = list(mod.GEOMS)[:1] if ctx.limit is not None else list(mod.GEOMS)
        a = ctx.cache.memo(self._loop_key(mod, geoms), lambda: self._loop(mod, root, fdir, geoms),
                           inputs=[os.path.join(root, g) for g in geoms] + [mod.ZP_FILE, fdir, mod.__file__])
        filters = [str(f) for f in a["filters"]]
        n_rr = int(a["n_rr"][0])
        rows = {k: [str(x) for x in a[k]] for k in ("name",) + CATS}
        seds = set(os.listdir(WEB / "seds"))            # one listing, not 28k stat calls
        rows["sed"] = np.array([f"{g}_{nm}.png" in seds for g, nm in zip(rows["geom"], rows["name"])])
        rows["mag"] = a["mag"]
        meta = {key: (label, unit, group, log, "") for _, key, label, unit, group, log, _ in mod.PHYS}
        mnote = "R&R models: FRIED v2 (ISM dust) interpolated in M★, R_max, Σ_out"
        meta.update({"mstar": ("M★ (R&R: rough T★ proxy)", "M☉", "common", True, ""),
                     "sigout": ("Σ at disk R_max", "g cm⁻²", "common", True, ""),
                     "fg0": ("FRIED G₀ (FUV at disk edge)", "G₀", "fried", True, ""),
                     "fmdot": ("FRIED log Ṁ (grid points)", "log M☉/yr", "fried", False, ""),
                     **{k: (f"FRIED-predicted log Ṁ (G₀ = {lab})", "log M☉/yr", "fried", False, mnote)
                        for _, k, lab in mod.FRIED_G0},
                     "redmin": ("Smallest νFν rise between wide bands (F115W→F444W)", "dex", "color", False,
                                "> 0: νFν rises across every NIRCam wide band 1-5 µm (very red source)")})
        for k, (_, _, _, log, _) in meta.items():
            rows[k] = _logv(a[k], log)
        # legacy colour pairs: all pairs of KEY_FILTERS plus all pairs of the NIRCam medium bands
        wl = lambda f: (mod.filt_wavelength_um(f.split(".")[1]), f)
        med = [f for f in filters if f.startswith("NIRCam.") and f.split(".")[1].endswith("M")]
        pairs = sorted({tuple(sorted((x, y), key=wl)) for grp in (mod.KEY_FILTERS, med)
                        for i, x in enumerate(grp) for y in grp[i + 1:]},
                       key=lambda ab: (wl(ab[0])[0], wl(ab[1])[0], ab))
        # PCA preset weights over the R&R magnitudes, with the legacy standardization
        dims = [mod.Dim(f"m_{f}", f, "mag", "phot", flip=True) for f in filters]
        for j, d in enumerate(dims):
            d.vals = a["mag"][:, j]
        sd = mod.standardize(dims, len(rows["name"]))
        presets = [dict(pr) for pr in mod.PRESETS if pr["id"] != "pca" or len(sd) >= 2]
        for pr in presets:
            if pr["id"] == "pca":
                pr["x"], pr["y"], _ = mod.pca_frame(sd, [d["key"] for d in sd], n_rr)
        turbo = mod.plt.cm.turbo
        gcol = {g: "#%02x%02x%02x" % tuple(int(255 * c) for c in turbo(i / (len(mod.GEOMS) - 1))[:3])
                for i, g in enumerate(mod.GEOMS)}
        cats = [dict(key=k, label=lab, colors=col, off=off) for k, lab, col, off in (
            ("cls", "Class", mod.CLASS_COLOR, ["unclassified"]), ("stg", "Stage", mod.STAGE_COLOR, ["unstaged"]),
            ("geom", "Geometry / grid", {**gcol, "FRIED": "#e4e9f3"}, []), ("src", "Source grid", mod.SRC_COLOR, []),
            ("cov", "Inside FRIED range", mod.COV_COLOR, []),
            ("det", "Detectable at 8 kpc (F480M < 20)", mod.DET_COLOR, ["too faint", "no photometry"]),
            ("red", "Very red (νFν rising 1-5 µm)", mod.RED_COLOR, []))]
        return {"rows": rows, "filters": filters, "pairs": pairs, "meta": meta, "cats": cats,
                "groups": [list(g) for g in mod.GROUPS], "presets": presets, "preselect": mod.PRESELECT,
                "scale": {d["key"]: d["scale"] for d in sd}, "ap_au": float(a["ap_au"][0]), "n_rr": n_rr,
                "seed": int(mod.SEED), "quota": 20000 // len(mod.GEOMS)}

    @classmethod
    def _loop_key(cls, mod, geoms) -> str:
        """Memo key of the sampling loop: the legacy generator file is a memo input; this folds in
        the replayed constants and the source of :meth:`_loop` itself."""
        blob = repr((inspect.getsource(cls._loop), mod.SEED, list(mod.GEOMS), list(geoms))).encode()
        return f"robitaille_rr_v2_{len(geoms)}_{hashlib.sha256(blob).hexdigest()[:10]}"

    @staticmethod
    def _loop(mod, root, fdir, geoms) -> dict:
        """``main()`` of the legacy generator up to the payload, without figures or HTML."""
        import glob
        ap, ap_au = mod.aperture_index(root, mod.TARGET_AU)
        filters = sorted((os.path.basename(p)[:-5] for p in glob.glob(f"{root}/s-pbhmi/convolved/JWST/*.fits")),
                         key=lambda f: (mod.filt_wavelength_um(f.split(".")[1]), f))
        zpd = mod.load_zeropoints()
        zp = np.array([zpd[f][0] for f in filters])
        rng = np.random.default_rng(mod.SEED)
        quota = 20000 // len(mod.GEOMS)       # legacy --n-total over the full geometry list
        rows = collections.defaultdict(list)
        phys = [p[1] for p in mod.PHYS]
        for geom in geoms:
            names, cls, stg, params, flux = mod.read_geometry(root, geom, ap, filters)
            F = np.array([flux[f] for f in filters]).T
            cells = collections.defaultdict(list)
            for i in np.where((F > 0).any(axis=1))[0]:
                cells[(int(cls[i]), int(stg[i]))].append(i)
            pick = np.sort(mod.stratified_pick(rng, {c: np.array(v) for c, v in cells.items()}, quota))
            rows["geom"] += [geom] * len(pick)
            rows["name"] += list(names[pick])
            rows["cls"] += [mod.CLASS_LABEL[int(c)] for c in cls[pick]]
            rows["stg"] += [mod.STAGE_LABEL[int(s)] for s in stg[pick]]
            for k in phys:
                rows[k].append(params[k][pick] if k in params else np.full(len(pick), np.nan))
            rows["mag"].append(mod.vega_mag(F[pick], zp))
            scells = collections.defaultdict(list)       # SED-subset draw keeps the rng stream
            for k, i in enumerate(pick):
                scells[(int(cls[i]), int(stg[i]))].append(k)
            mod.stratified_pick(rng, {c: np.array(v) for c, v in scells.items()}, 150)
        n_rr, rr = len(rows["name"]), {k: np.concatenate(rows[k]) for k in ("teff", "mdisk", "rdisk", "dskp")}
        mstar, sig = mod.mstar_proxy(rr["teff"]), mod.rr_sigma_out(rr["mdisk"], rr["rdisk"], rr["dskp"])
        fried = mod.load_fried(fdir, 0.5)
        xq = np.column_stack([np.log10(mstar), np.log10(rr["rdisk"]), np.log10(sig)])
        with np.errstate(invalid="ignore"):
            mdot = {key: mod.fried_interp(fried[1], g0)(xq) for g0, key, _ in mod.FRIED_G0}
        hasdisk = np.isfinite(sig) & (sig > 0)
        rows["src"] = ["R&R"] * n_rr
        rows["cov"] = list(np.where(np.isfinite(mdot["mdot_g3"]), "in FRIED range",
                                    np.where(hasdisk, "outside range", "no disk")))
        nF = sum(len(t["mstar"]) for t in fried)
        for k in phys:
            rows[k].append(np.concatenate([t[k] for t in fried]) if k in ("mdisk", "rdisk") else np.full(nF, np.nan))
        rows["mag"].append(np.full((nF, len(filters)), np.nan))
        for t in fried:
            m = len(t["mstar"])
            rows["geom"] += ["FRIED"] * m
            rows["name"] += [f"{t['tag']}_{i:04d}" for i in range(m)]
            for k, v in (("cls", "FRIED"), ("stg", "FRIED"), ("src", t["src"]), ("cov", "FRIED grid")):
                rows[k] += [v] * m
        n = len(rows["name"])
        mags = np.vstack(rows["mag"])
        wide = [filters.index(f"NIRCam.{b}") for b in mod.RED_FILTERS]
        lognf = np.column_stack([np.log10(zpd[filters[j]][0]) - 0.4 * mags[:, j] - np.log10(zpd[filters[j]][1])
                                 for j in wide])
        redmin = (lognf[:, 1:] - lognf[:, :-1]).min(axis=1)
        m8 = mags[:, filters.index(mod.DET_FILTER)] + 5 * np.log10(mod.DET_DIST_KPC)
        lab = lambda k, v, ok, yes, no: ("FRIED (no colours)" if k >= n_rr else "no photometry"
                                         if not np.isfinite(v) else yes if ok else no)
        red = [lab(k, redmin[k], redmin[k] > 0, "very red", "not very red") for k in range(n)]
        det = [lab(k, m8[k], m8[k] < mod.DET_LIMIT, "detectable", "too faint") for k in range(n)]
        nan_f, nan_r = np.full(nF, np.nan), np.full(n_rr, np.nan)
        out = {k: np.array(rows[k], dtype=str) for k in ("geom", "name", "cls", "stg", "src", "cov")}
        out.update(red=np.array(red), det=np.array(det), mag=mags, redmin=redmin,
                   filters=np.array(filters), n_rr=np.array([n_rr]), ap_au=np.array([ap_au]),
                   mstar=np.concatenate([mstar] + [t["mstar"] for t in fried]),
                   sigout=np.concatenate([sig] + [t["sig"] for t in fried]),
                   fg0=np.concatenate([nan_r] + [t["g0"] for t in fried]),
                   fmdot=np.concatenate([nan_r] + [t["mdot"] for t in fried]),
                   **{k: np.concatenate(rows[k]) for k in phys},
                   **{k: np.concatenate([v, nan_f]) for k, v in mdot.items()})
        return out

    # -- common intermediate ---------------------------------------------------------------
    def datasets(self, ctx):
        from portalkit.spec import ReadmeSpec, TierUnavailable
        if ctx.tier not in self.tiers:
            raise TierUnavailable(f"robitaille implements tiers A and B, not {ctx.tier}")
        raw = self._tier_a(ctx) if ctx.tier == "A" else self._tier_b(ctx)
        sel = self._select(raw, ctx.limit)
        img_tab, img_items = self._imaging_rows(ctx)
        mod = self._catalog(raw, sel, set(img_tab["id"]))
        ds = {"mod": mod, "sedimg": self._sedimg(mod), "sedcurve": self._sedcurve(ctx, mod, raw),
              "imaging": self._imaging(img_tab, mod), "imgs": img_items}
        ds["readme"] = ReadmeSpec(markdown=self._readme(ctx, raw, ds))
        return ds

    @staticmethod
    def _select(raw, limit):
        n, n_rr = len(raw["rows"]["name"]), raw["n_rr"]
        if limit is None:
            return np.arange(n)
        k = max(1, int(limit) // 3)            # R&R rows, R&R rows with an SED, FRIED rows
        sed_rr = np.where(raw["rows"]["sed"][:n_rr])[0][:k]
        fried = n_rr + np.unique(np.linspace(0, n - n_rr - 1, k).astype(int)) if n > n_rr else []
        return np.unique(np.concatenate([np.arange(min(k, n_rr)), sed_rr, fried]).astype(int))

    def _catalog(self, raw, sel, img_models):
        from astropy.table import Table
        from portalkit.bundle.codec import col_stats
        from portalkit.legacy.manifold_payload import preset_frame
        from portalkit.spec import CatalogSpec, ColumnSpec, DensitySpec, Group, ManifoldSpec, Preset, VirtualDim
        r, filters = raw["rows"], raw["filters"]
        geom, name = np.asarray(r["geom"])[sel], np.asarray(r["name"])[sel]
        ids = [f"{g}_{m}" for g, m in zip(geom, name)]
        t = Table({"id": ids, **{k: np.asarray(r[k])[sel] for k in CATS}, "sed": np.asarray(r["sed"])[sel]})
        aliases, seen, links = {}, set(), []
        for i, g, m in zip(ids, geom, name):    # 'geom/name' keeps the legacy #star=<geom/name> hash
            mdl = m.split("_")[0]
            img = g == IMG_GEOM and mdl in img_models and mdl not in seen
            aliases[i] = [f"{g}/{m}"] + ([mdl] if img else [])
            links.append(f"imaging.html#id={mdl}" if img else "")
            seen.add(mdl if g == IMG_GEOM else None)
        cols = [ColumnSpec("id", "id")]
        if any(links):                          # only rows whose model is in the imaging gallery
            t["img_url"] = links
            cols.append(ColumnSpec("img_url", "url", label="Imaging gallery", note="Hyperion image panels of this model"))
        for c in raw["cats"]:
            present = list(dict.fromkeys(str(x) for x in t[c["key"]]))
            lv = [x for x in c["colors"] if x in present] + [x for x in present if x not in c["colors"]]
            cols.append(ColumnSpec(c["key"], "category", label=c["label"], levels=lv,
                                   colors={k: v for k, v in c["colors"].items() if k in lv},
                                   default_off=[x for x in c["off"] if x in lv]))
        cols.append(ColumnSpec("sed", "flag", label="SED figure"))
        mk, scale = [], {}
        for j, f in enumerate(filters):
            v, key, (inst, nm) = r["mag"][sel, j], f"m_{f}", f.split(".")
            if not np.isfinite(v).any():
                continue
            v = np.round(v, 3)                     # legacy payload precision = quantization step
            with np.errstate(invalid="ignore"):    # contamination rule, applied to the table itself so
                t[key] = v = np.where(v <= MAG_HI, v, np.nan)   # rows, columns and density agree
            mk.append(key)
            cols.append(ColumnSpec(key, "mag", label=f"{inst} {nm}", unit="mag (Vega, 1 kpc)", filter=f"JWST/{f}",
                                   step=0.001, group="phot", invert=True,
                                   note="missing where fainter than 30 mag at 8 kpc (legacy contamination rule)"))
            fin = v[np.isfinite(v)]
            if key in raw["scale"] and fin.size:
                scale[key] = col_stats(fin)["scale"] / raw["scale"][key]
        vk = [k for k in raw["meta"] if np.isfinite(np.asarray(r[k], dtype=float)[sel]).any()]
        for k in vk:
            lab, unit, grp, log, note = raw["meta"][k]
            t[k] = _sig(np.asarray(r[k], dtype=float)[sel])
            cols.append(ColumnSpec(k, "value", label=f"log10 {lab}" if log else lab, unit=unit, group=grp,
                                   note=(note + " (stored as log10 values)").strip() if log else note,
                                   **({"enc": "f32", "exact_gpu": False} if k in F32_VALUES else {})))
        have = set(mk)
        virt = [VirtualDim(f"m_{a}-m_{b}", label=f"{a.split('.')[1]} − {b.split('.')[1]}", unit="mag (Vega)",
                           group="color") for a, b in raw["pairs"] if f"m_{a}" in have and f"m_{b}" in have]
        if "m_NIRCam.F480M" in have:
            virt.append(VirtualDim(M8, label="F480M at 8 kpc", unit="mag (Vega)", group="phot"))
        vkeys = [v.key for v in virt]
        dims = mk + [k for k in vkeys if k != M8] + vk + [k for k in vkeys if k == M8]
        gd = collections.defaultdict(list)
        for c in [c for c in cols if c.group] + virt:
            gd[c.group].append(c.key)
        groups = [Group(g, lab, gd[g]) for g, lab in raw["groups"] if gd[g]]
        key = lambda k: ("m_%s-m_%s" % tuple(k[2:].split("_")) if k.startswith("c_")   # legacy -> new key
                         else M8 if k == "m480_8kpc" else k)
        dimset, presets, dens, frames = set(dims), [], [], {}
        for p in raw["presets"]:
            x, y = {key(k): w for k, w in p["x"].items()}, {key(k): w for k, w in p["y"].items()}
            if not (set(x) | set(y)) <= dimset:
                continue
            data = {"note": p.get("note", ""), "code": p.get("code", ""),
                    **({"color": p["color"]} if p.get("color") in CATS else {})}
            if p["id"] == "pca":
                frames["pca"] = preset_frame(x, y, dims, scale)
                presets.append(Preset("pca", p["name"], "frame", {**data, "dims": dims, "F": frames["pca"]}))
                continue
            (xk, _), (yk, _) = next(iter(x.items())), next(iter(y.items()))
            presets.append(Preset(p["id"], p["name"], "axes", {**data, "x": xk, "y": yk}))
            if len(t) > 3000:
                rng = [self._range(t, e) for e in (xk, yk)]
                dens.append(DensitySpec(p["id"], xk, yk, xr=rng[0], yr=rng[1]))   # key = preset: no auto twin
        offs, lvs = {c["key"]: c["off"] for c in raw["cats"]}, {c.key: c.levels for c in cols if c.role == "category"}
        for p in raw["preselect"]:
            cats = {k: [x for x in lvs[k] if (x not in p[k] if p.get(k) else x in offs[k])]
                    for k in ("cls", "stg", "cov", "src", "red", "det")}
            presets.append(Preset(f"sel_{p['id']}", p["label"], "filter", {"filters": {"cats": cats}}))
        default = "mbccd" if any(p.key == "mbccd" for p in presets) else "pca"
        n_geo = len({g for g in geom if g != "FRIED"})
        return CatalogSpec(
            key="mod", table=t, id="id", columns=cols, aliases=aliases, virtual=virt, groups=groups,
            presets=presets, density=dens, manifold=ManifoldSpec(dims=dims, frames=frames, default_frame=default),
            title=f"R&R YSO models + FRIED ({len(t):,} rows)", noun=("model", "models"),
            caveats=[f"{n_geo} Richardson+ (2024) geometries sampled evenly in Class x Stage; magnitudes "
                     f"are Vega at 1 kpc in a {raw['ap_au']:.1f} au aperture.",
                     "FRIED rows have no photometry; they appear in the parameter views only.",
                     "Magnitudes fainter than 30 mag at 8 kpc are missing (legacy contamination rule, applied "
                     "at build time); the distance-modulus slider shifts every magnitude but does not move this cut.",
                     "The legacy page hid such a point only in views whose axes weight the faint filter by >= 0.5; "
                     "here the magnitude is missing in every view, so in mixed views (PCA) these rows fade as "
                     "missing data where the legacy page drew them.",
                     "A_V is stored as float32 (it spans 0 to 3e12 mag)."])

    @staticmethod
    def _range(t, e):
        a, _, b = e.partition("-m_")
        v = np.asarray(t[a], float) - (np.asarray(t["m_" + b], float) if b else 0.0) if a in t.colnames \
            else np.asarray(t["m_NIRCam.F480M"], float) + DM8
        q = (0.5, 99.5) if e.startswith("m_") else (2, 98)       # magnitudes: keep nearly all points
        lo, hi = np.percentile(v[np.isfinite(v)], q) if np.isfinite(v).sum() > 1 else (0.0, 1.0)
        return (float(lo), float(hi)) if hi > lo else (float(lo) - 0.5, float(lo) + 0.5)

    def _sedimg(self, mod):
        from portalkit.spec import ImageItem, ImageSetSpec
        t = mod.table
        items = [ImageItem(id=str(i), roles={"sed": f"{URL}seds/{i}.png"}, caption=str(i).replace("_", "/", 1))
                 for i, s in zip(t["id"], t["sed"]) if s]
        return ImageSetSpec(key="sedimg", items=items, roles=[("sed", "SED (legacy PNG)")], catalog="mod")

    def _sedcurve(self, ctx, mod, raw):
        from astropy.io import fits
        from portalkit.spec import RecordSetSpec
        ids = [str(i) for i, s in zip(mod.table["id"], mod.table["sed"]) if s]
        for g in sorted({i.split("_")[0] for i in ids}):
            ctx.inputs.add(GRID_ROOT / g)
        with fits.open(GRID_ROOT / "s-pbhmi" / "flux.fits", memmap=True) as h:
            apc = np.array(h["APERTURES"].data.field(0), dtype=float)
            wav = np.sort(np.array(h["SPECTRAL_INFO"].data.field(0), dtype=float))
        ap = int(np.argmin(np.abs(np.log10(apc / raw["ap_au"]))))
        return RecordSetSpec(key="sedcurve", kind="sedcurve", catalog="mod", ids=ids,
                             loader=_SedLoader(ap), grids={"rr": wav})

    def _imaging_rows(self, ctx):
        from astropy.table import Table
        from portalkit.spec import ImageItem, ImageSetSpec
        pf, pdir = IMG_DIR / "model_params.ecsv", IMG_DIR / "png"
        ctx.inputs.add(pf)
        ctx.inputs.add(pdir)
        pt = Table.read(pf)
        by = {str(m): i for i, m in enumerate(pt["model"])}
        models = sorted(f[:-len("_image_images.png")] for f in os.listdir(pdir) if f.endswith("_image_images.png"))
        if ctx.limit is not None:              # fixture: models with missing parameters first
            bad = {m for m in models if m not in by or not all(np.isfinite(float(pt[c][by[m]])) for c in IMG_PARAMS)}
            models = sorted(sorted(models, key=lambda m: m not in bad)[:int(ctx.limit)])
        t = Table({"id": models})
        for c in IMG_PARAMS:
            t[c] = _logv([float(pt[c][by[m]]) if m in by else np.nan for m in models], True)
        items = []
        for m, *v in zip(models, *(t[c] for c in IMG_PARAMS)):
            with (pdir / f"{m}_image_images.png").open("rb") as fh:
                head = fh.read(24)
            ok = head[:8] == b"\x89PNG\r\n\x1a\n"
            w, h = (int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")) if ok else (None, None)
            cap = " · ".join(f"{c}={10 ** x:.3g}" for c, x in zip(IMG_PARAMS, v) if np.isfinite(x)) or "parameters missing"
            items.append(ImageItem(id=m, roles={"image": f"{URL}imaging/{m}_image_images.png"}, caption=f"{m}: {cap}",
                                   tags={"params": "yes" if m in by else "missing"}, w=w, h=h))
        return t, ImageSetSpec(key="imgs", items=items, roles=[("image", "Inclination x wavelength panels")],
                               facets=[("params", "Parameters in model_params.ecsv")], catalog="imaging")

    def _imaging(self, t, mod):
        from portalkit.spec import CatalogSpec, ColumnSpec, Preset
        back = collections.defaultdict(list)
        for i, al in mod.aliases.items():
            for a in al[1:]:
                back[a].append(i)
        cols = [ColumnSpec("id", "id")] + [ColumnSpec(c, "value", label=f"log10 {lab}", unit=IMG_UNITS[c], group="params",
                                                      note="missing where model_params.ecsv has no value")
                                           for c, lab in zip(IMG_PARAMS, IMG_LABELS)]
        links = [f"manifold.html#id={back[m][0]}" if back.get(m) else "" for m in t["id"]]
        if any(links):                          # only models that are also in the manifold sample
            t["mod_url"] = links
            cols.append(ColumnSpec("mod_url", "url", label="Model manifold", note="this model's row in the manifold"))
        pre = [Preset("lum3", "L > 10³ L☉", "filter", {"filters": {"ranges": {"L": [3, 99]}}}),
               Preset("menv1", "M_env > 1 M☉", "filter", {"filters": {"ranges": {"Menv": [0, 99]}}}),
               Preset("lm", "L vs M_env", "axes", {"x": "Menv", "y": "L"}),
               Preset("tl", "T★ vs L", "axes", {"x": "Tstar", "y": "L"})]
        return CatalogSpec(key="imaging", table=t, id="id", columns=cols, presets=pre,
                           aliases={m: ids for m, ids in back.items() if m in set(t["id"])},
                           title=f"Imaging-run models ({len(t):,})",
                           caveats=[f"Hyperion images of {IMG_GEOM} models at d = 5 kpc; missing parameters "
                                    "are shown as missing (the legacy page wrote 0)."])

    def _readme(self, ctx, raw, ds) -> str:
        t, m = ds["mod"].table, ds["mod"]
        src = collections.Counter(str(s) for s in t["src"])
        ng = len({str(g) for g in t["geom"]} - {"FRIED"})
        lim = "" if ctx.limit is None else f" (fixture build: {len(t)} of {len(raw['rows']['name']):,} rows)"
        fr = ", ".join(f"{k} {v:,}" for k, v in src.items() if k != "R&R")
        seed = "" if raw.get("seed") is None else f" (seed {raw['seed']})"
        return f"""# Robitaille YSO model portal

{len(t):,} model rows{lim}: {src.get('R&R', 0):,} Richardson et al. (2024) radiative-transfer models
from {ng} geometr{'y' if ng == 1 else 'ies'} (update of the Robitaille 2017 YSO SED grid, v1.2;
[Zenodo](https://doi.org/10.5281/zenodo.8114592)), plus FRIED photoevaporation grid points ({fr}).

- **Model manifold**: {len(m.manifold.dims)} dimensions ({sum(c.role == 'mag' for c in m.columns)} JWST magnitudes,
  {sum(v.group == 'color' for v in m.virtual)} colours, physical and FRIED quantities); {len(ds['sedimg'].items):,} models
  have a legacy SED figure and a model SED curve.
- **Imaging gallery**: {len(ds['imgs'].items):,} {IMG_GEOM} models imaged with Hyperion at d = 5 kpc (inclination
  20/45/70/87 deg x F150W F210M F360M F480M F1280W F2550W 1 mm 3 mm), with their parameters.

Each geometry contributes up to {raw['quota']:,} models drawn evenly across Class x Stage cells{seed}.
Magnitudes are Vega at 1 kpc in a {raw['ap_au']:.1f} au aperture; the distance-modulus slider shifts them.
"Detectable" means F480M < 20 mag at 8 kpc; magnitudes fainter than 30 mag at 8 kpc are missing. R&R stellar
masses come from a rough T★ proxy. FRIED rows (Haworth et al. 2018, 2023) carry no photometry.
"""

    # -- pages -----------------------------------------------------------------------------
    def pages(self, ctx, ds):
        from portalkit.spec import PageSpec
        mod, n_img = ds["mod"], len(ds["imgs"].items)
        cards = [{"page": "manifold", "text": f"{len(mod.table):,} models, {len(mod.manifold.dims)} dimensions; "
                                              "Class/Stage presets, live SEDs, distance-modulus slider."},
                 {"page": "imaging", "text": f"{n_img:,} synthetic image panels with parameter filters."}]
        return [
            PageSpec(id="index", archetype="landing", title="Robitaille YSO models", datasets=["readme"],
                     layout={"pane": "landing", "options": {"cards": cards, "counts": True, "search": True}}),
            PageSpec(id="manifold", archetype="manifold", title="Model manifold", manifold=True,
                     datasets=["mod", "sedimg", "sedcurve", "readme"], state={"dm": 0.0},
                     subtitle="R&R YSO models + FRIED", caps_disable={c: "model grid has no sky positions" for c in SKY_CAPS}),
            PageSpec(id="imaging", archetype="gallery", title="Imaging gallery",
                     datasets=["imgs", "imaging", "readme"], subtitle=f"{n_img:,} {IMG_GEOM} models"),
        ]


class _SedLoader:
    """id ``<geom>_<name>`` -> model SED (Jy at 1 kpc) from ``<geom>/flux.fits`` (memmap)."""

    def __init__(self, ap: int):
        self.ap, self._g = ap, {}

    def __call__(self, sid: str):
        from astropy.io import fits
        from portalkit.spec import Spectrum
        geom, _, name = sid.partition("_")
        if geom not in self._g:
            h = fits.open(GRID_ROOT / geom / "flux.fits", memmap=True)
            names = np.char.strip(np.asarray(h["MODEL_NAMES"].data.field(0)).astype(str))
            self._g[geom] = (h, {n: i for i, n in enumerate(names)},
                             np.array(h["SPECTRAL_INFO"].data.field(0), dtype=float))
        h, idx, wav = self._g[geom]
        i = idx.get(name)
        if i is None:
            return None
        f = np.array(h["VALUES"].data[i, self.ap, :], dtype=float) * 1e-3
        o = np.argsort(wav)
        return Spectrum(wave=wav[o], channels={"flux": np.where(f[o] > 0, f[o], np.nan)})


ADAPTER = RobitailleAdapter()
