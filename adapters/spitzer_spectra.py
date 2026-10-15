"""CMZ Spitzer IRS spectra portal (WP31 extra; ARCHITECTURE.md section 14, wave 6).

Legacy: ``/orange/adamginsburg/cmz/spitzer_spectra`` (public). ``spitzer_spectra_sources.json``
(196 rows -- the same payload the map page embeds inline as ``<script id="srcdata"
type="application/json">``, read here as a plain file since both give identical data) carries
one row per IRS staring-mode observation: position, IRAC/IRAS/MIPS aperture photometry, the two
slit footprints (SL and LL), and a pointer to ``specdata/<Name>.json`` (``w``/``f``/``e``/
``order`` -- wavelength, flux, error, spectral order -- the per-source spectrum). No generator
script was found for this family (ARCHITECTURE.md section 14); it reads as if cloned from the
NIRSpec 6927 map/specviewer template, so this adapter mirrors that pair of pages. Tier A only
(``tiers = ("A",)``): nothing here is computed, only read and reshaped.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np

LEGACY_DIR = Path("/orange/adamginsburg/cmz/spitzer_spectra")
#: Public web root this family is served under (ARCHITECTURE.md section 14: "public via
#: web/public/spitzer_spectra").
PUBLIC = "https://data.rc.ufl.edu/pub/adamginsburg/spitzer_spectra/"


def _num(x):
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if np.isfinite(v) else None


def _rel_url(rel: str | None) -> str | None:
    return PUBLIC + rel if rel else None


def _order_segments(order: np.ndarray) -> list[tuple[int, int, str]]:
    """``(i0, i1, label)`` index ranges of contiguous equal values in ``order`` (the spectrum's
    own wave-sorted IRS order numbers -- SL/LL staring-mode orders are not wavelength-contiguous
    between each other the way NIRSpec gratings are, so this groups by value, not by a fixed
    wavelength cut table). Empty/all-NaN input gives no segments."""
    n = order.size
    if n == 0:
        return []
    out, start = [], 0
    for i in range(1, n + 1):
        same = i < n and (order[i] == order[start] or (np.isnan(order[i]) and np.isnan(order[start])))
        if not same:
            out.append((start, i, f"order{int(order[start])}" if np.isfinite(order[start]) else "order?"))
            start = i
    return out


def load_sources(ctx) -> list[dict]:
    """Read ``spitzer_spectra_sources.json`` (list of 196 row dicts); ``[]`` if the file is
    missing or is not a JSON list (never raises -- a degraded/edge-case input yields an empty,
    still-valid catalog rather than a broken build)."""
    p = LEGACY_DIR / "spitzer_spectra_sources.json"
    ctx.inputs.add(p)
    if not p.is_file():
        return []
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return []
    return doc if isinstance(doc, list) else []


class SpitzerSpectraAdapter:
    name = "spitzer_spectra"
    tiers = ("A",)

    def datasets(self, ctx):
        from portalkit.spec import OverlaySpec, ReadmeSpec, TierUnavailable
        if ctx.tier not in self.tiers:
            raise TierUnavailable(f"spitzer_spectra implements tier A only, not {ctx.tier}")
        rows = load_sources(ctx)
        rows = rows if ctx.limit is None else rows[: int(ctx.limit)]
        self._rows = rows
        ds: dict = {}
        ds["src"] = self._catalog(rows)
        ds["spec"] = self._records(ctx, rows)
        ds["specimg"] = self._images(rows)
        ds["slits"] = self._slits(rows)
        ds["readme"] = ReadmeSpec(markdown=(
            f"# CMZ Spitzer IRS spectra\n\n{len(rows)} IRS staring-mode observations toward "
            "the Central Molecular Zone, each with SL and LL slit footprints and aperture "
            "photometry from IRAC/IRAS/MIPS.\n\n## Caveats\n\n- No generator script for this "
            "family was found; it is read as a clone of the NIRSpec 6927 map/specviewer "
            "template (ARCHITECTURE.md section 14).\n- Flux units are carried as Jy from the "
            "legacy payload; the per-point spectrum files carry no explicit unit field.\n"))
        return ds

    def _catalog(self, rows: list[dict]):
        from portalkit.spec import catalog_from_rows
        bands: list[str] = []
        for r in rows:
            for b in (r.get("Photometry") or {}):
                if b not in bands:
                    bands.append(b)
        out = []
        for r in rows:
            d = {"id": r.get("Name"), "ra": _num(r.get("RA")), "dec": _num(r.get("Dec")),
                "object": r.get("Object"), "date_obs": r.get("DateObs"),
                "wave_min": _num(r.get("WaveMin")), "wave_max": _num(r.get("WaveMax")),
                "campaign": r.get("Campaign"), "prog_id": r.get("ProgID"), "observer": r.get("Observer"),
                "aorkey": r.get("AORKEY")}
            phot = r.get("Photometry") or {}
            for b in bands:
                bp = phot.get(b) or {}
                d[f"{b}_flux"], d[f"{b}_err"] = _num(bp.get("flux_Jy")), _num(bp.get("eflux_Jy"))
            out.append(d)
        roles = {"object": "category", "campaign": "category", "observer": "text", "date_obs": "text",
                 "prog_id": "category", "aorkey": "text"}
        for b in bands:
            roles[f"{b}_flux"], roles[f"{b}_err"] = "flux", "error"
        return catalog_from_rows(out, "id", "ra", "dec", key="src", roles=roles,
                                 title=f"CMZ Spitzer IRS observations ({len(out)})")

    def _records(self, ctx, rows: list[dict]):
        from portalkit.spec import RecordSetSpec
        by_name = {r.get("Name"): r for r in rows if r.get("Name")}
        ids = list(by_name)
        loader = lambda name: self._spectrum(ctx, by_name.get(name))  # noqa: E731
        return RecordSetSpec("spec", "pks1", "src", ids, loader, enc={"flux": "f32", "err": "f32"})

    def _spectrum(self, ctx, row: dict | None):
        from portalkit.spec import Spectrum
        if row is None:
            return None
        rel = row.get("SpecFile") or (f"specdata/{row['Name']}.json" if row.get("Name") else None)
        if not rel:
            return None
        p = LEGACY_DIR / rel
        if not p.is_file():
            return None
        ctx.inputs.add(p)
        sd = json.loads(p.read_text(encoding="utf-8"))
        w = np.asarray(sd.get("w") or [], float)
        f = np.array([np.nan if v is None else float(v) for v in (sd.get("f") or [])], float)
        e = np.array([np.nan if v is None else float(v) for v in (sd.get("e") or [])], float)
        order = np.array([np.nan if v is None else float(v) for v in (sd.get("order") or [])], float)
        if w.size == 0 or f.size != w.size or e.size != w.size:
            return None
        o = np.argsort(w, kind="stable")
        w, f, e = w[o], f[o], e[o]
        order = order[o] if order.size == w.size else np.full(w.size, np.nan)
        e = np.where(np.isfinite(f) & (e > 0), e, np.nan)
        segs = _order_segments(order)
        return Spectrum(wave=w, channels={"flux": f, "err": e}, units={"flux": "Jy"}, segments=segs,
                        meta={"name": sd.get("name") or row.get("Name"), "object": row.get("Object"),
                              "date_obs": row.get("DateObs")})

    def _images(self, rows: list[dict]):
        from portalkit.spec import ImageSetSpec
        items = [{"id": r["Name"], "caption": f"{r['Name']}  ({r.get('Object', '')})",
                 "png": _rel_url(r.get("SpectrumPNG"))}
                for r in rows if r.get("Name") and r.get("SpectrumPNG")
                and (LEGACY_DIR / r["SpectrumPNG"]).is_file()]
        return ImageSetSpec.from_rows(items, {"png": ("png", "Spectrum (legacy PNG)")}, "specimg",
                                      catalog="src", join="id")

    def _slits(self, rows: list[dict]):
        from portalkit.spec import OverlaySpec

        def poly(pts):
            pts = [[float(a), float(b)] for a, b in (pts or [])]
            if len(pts) < 3:
                return None
            return pts + [pts[0]]
        items = []
        for r in rows:
            name = r.get("Name")
            if not name:
                continue
            for tag in ("SlitSL", "SlitLL"):
                p = poly(r.get(tag))
                if p is not None:
                    items.append({"poly": p, "id": f"{name}_{tag.lower()}", "label": f"{name} {tag[4:]}"})
        return OverlaySpec(layers=[{"id": "slits", "name": "IRS slit footprints (SL/LL)", "space": "sky",
                                    "kind": "polygons", "default": False,
                                    "style": {"color": "#f4c430", "width": 1}, "items": items}])

    def pages(self, ctx, ds):
        from portalkit.spec import PageSpec as P
        n = ds["src"].n or 0
        cards = [{"page": "spitzer_map", "text": f"{n} IRS pointings over the CMZ, slit footprints."},
                 {"page": "spitzer_specviewer", "text": "Per-source IRS spectra (SL + LL)."}]
        return [
            P(id="index", archetype="landing", title="CMZ Spitzer IRS spectra", datasets=["readme"],
              layout={"pane": "landing", "options": {"cards": cards, "counts": True, "search": True}}),
            P(id="spitzer_map", archetype="explorer", title="CMZ Spitzer IRS: map",
              datasets=["src", "slits", "specimg"],
              layout={"row": [{"pane": "list", "dataset": "src", "size": 260},
                              {"pane": "sky", "dataset": "src", "options": {"overlays": ["slits"]}},
                              {"pane": "detail", "dataset": "src",
                               "options": {"images": "specimg"}, "size": 360}]}),
            P(id="spitzer_specviewer", archetype="spectra", title="CMZ Spitzer IRS: spectra",
              datasets=["src", "spec"],
              layout={"row": [{"pane": "list", "dataset": "src", "size": 260},
                              {"pane": "spectrum1d", "dataset": "spec", "options": {"records": "spec"}},
                              {"pane": "detail", "dataset": "src", "size": 320}]}),
        ]


ADAPTER = SpitzerSpectraAdapter()
