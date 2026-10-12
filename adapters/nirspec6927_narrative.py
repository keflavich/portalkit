"""JWST NIRSpec 6927 narrative pages + reference-star QA family adapter (WP31; ARCHITECTURE.md
sections 14.1 and 14.7, wave 6).

Legacy portal: ``/orange/adamginsburg/jwst/spectra/nirspec_6927`` (secure) -- the same legacy
tree as ``adapters/nirspec6927.py`` (WP22). This adapter ports the four pages WP22 left legacy
(``nirspec_6927_msata_finding.html``, ``nirspec_6927_h2.html``, ``nirspec_6927_refstar_map.html``,
``refstar_check/index.html``) and deploys into the *same* ``nirspec_6927/unified`` target
directory under a separate ``[[portal]]`` id (``sites/nirspec6927_narrative.toml``); every page
id here is prefixed ``n_`` so the two families' served files never collide.

Tier A only (``tiers = ("A",)``): every page's data is already embedded in the rendered legacy
HTML as a ``const DATA = {...}`` payload (``portalkit.legacy.extract``), or (H2 positions) in
the ``nirspec_6927_sources.json`` the WP22 adapter also reads. No science code runs.

Pages:

* ``n_msata`` (``narrative`` archetype): the 15-beat MSATA scrollytelling story (why o028's
  acquisition failed and three others converged). The legacy page hand-writes rich inline
  ``<span data-hl=...>`` hover targets inside each beat's prose; ``portalkit.config.narrative_document``
  accepts only ``markdown`` (rendered through the deliberately restrictive ``markdown_lite``,
  which supports no raw HTML, inline emphasis or images -- see its module docstring). Beat text
  is converted to plain/table/link markdown by :func:`beat_markdown`; the per-phrase highlight
  spans collapse to one coarse ``view.highlight`` (``{ds, ids}``) per beat -- a deliberate
  fidelity loss, see the work-package deviations.
* ``n_h2`` (``explorer`` archetype): the H2 excitation report for the 8 sources with resolved
  H2 lines. Each source's ``verdict`` (kind + prose) is carried as-is from the legacy payload
  (there is no separate ``VERDICTS`` Python table to import -- see deviations). A rotation-
  diagram-style scatter (E_u/k vs line flux) is added from the per-source line-measurement
  table when it is non-empty.
* ``n_refstar_map`` (``explorer`` archetype): the MSA-quadrant reference-star candidate map
  (v18/v20 catalogues, the 8 selected stars, the quadrant footprints) with the sky pane's
  configurable click-rings option enabled. ``refstar_map_data.json`` has no writer anywhere
  in the legacy tree (section 16 Q5) -- its data is extracted with Tier A straight from the
  already-rendered ``nirspec_6927_refstar_map.html`` payload instead, which makes rebuilding
  it unnecessary.
* ``n_refstar_check`` (``gallery`` archetype): the per-candidate F115W contact-sheet PNGs.
"""

from __future__ import annotations

import html as _html
import json
import math
import os
import re
from pathlib import Path
from urllib.parse import quote

LEGACY_DIR = Path("/orange/adamginsburg/jwst/spectra/nirspec_6927")
JWST_ROOT = "/orange/adamginsburg/jwst/"
SECURE = "https://data.rc.ufl.edu/secure/adamginsburg/jwst/"
#: half-width (arcsec) of a NIRSpec MSATA acquisition field (the msata_finding DATA payload's
#: own ``field_half``, 2.25", reused for the refstar-map boxes where no per-page value exists).
FIELD_HALF_ARCSEC = 2.25


def _num(x):
    """float(x), or None for missing / non-finite values."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _surl(path) -> str:
    p = os.path.normpath(os.path.abspath(str(path)))
    if not p.startswith(JWST_ROOT):
        raise ValueError(f"{p} is not under {JWST_ROOT} (no secure URL)")
    return SECURE + quote(p[len(JWST_ROOT):])


def _rel(rel: str | None):
    return _surl(LEGACY_DIR / rel) if rel else None


def _lim(seq: list, ctx) -> list:
    return list(seq) if ctx.limit is None else list(seq)[:max(0, int(ctx.limit))]


def _box_poly(ra: float, dec: float, half_arcsec: float = FIELD_HALF_ARCSEC) -> list[list[float]]:
    """4-corner (+closing) sky polygon of a square ``2*half_arcsec`` wide centred on
    ``(ra, dec)``, with the RA half-width scaled by ``1/cos(dec)``."""
    hdec = half_arcsec / 3600.0
    cosd = math.cos(math.radians(dec)) or 1e-6
    hra = half_arcsec / 3600.0 / abs(cosd)
    return [[ra - hra, dec - hdec], [ra + hra, dec - hdec], [ra + hra, dec + hdec],
            [ra - hra, dec + hdec], [ra - hra, dec - hdec]]


# --------------------------------------------------------------------------------------------
# legacy beat HTML -> markdown_lite-safe markdown (the MSATA scrollytelling prose)
# --------------------------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")
_LINK_RE = re.compile(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_BLOCK_RE = re.compile(r"<table\b.*?</table>|<figure\b.*?</figure>|<p\b.*?</p>", re.S)
_SECTION_RE = re.compile(r'<section class="beat" data-scene="([^"]+)">(.*?)</section>', re.S)
_H2_RE = re.compile(r"<h2>(.*?)</h2>", re.S)
_H3_RE = re.compile(r"<h3>(.*?)</h3>", re.S)


def _text(s: str) -> str:
    return _html.unescape(_TAG_RE.sub("", s)).strip()


def _table_md(tbl_html: str) -> str:
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", tbl_html, re.S)
    out = []
    for i, row in enumerate(rows):
        cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.S)
        if not cells:
            continue
        cells = [(_text(c) or "—").replace("|", "/") for c in cells]
        out.append("| " + " | ".join(cells) + " |")
        if i == 0:
            out.append("|" + "|".join(["---"] * len(cells)) + "|")
    return "\n".join(out)


def _figure_md(fig_html: str) -> str:
    m = re.search(r"<figcaption>(.*?)</figcaption>", fig_html, re.S)
    if not m:
        return ""
    return f"(figure not reproduced here; markdown_lite allows no images) {_text(m.group(1))}"


def beat_markdown(section_html: str) -> str:
    """One MSATA beat's ``<p>``/``<table class="mini">``/``<figure>`` prose as markdown_lite
    markdown: tables become GFM pipe tables, links become ``[text](url)``, and every other tag
    (including the legacy's per-phrase ``<span data-hl=...>`` hover targets) is stripped to
    plain text -- markdown_lite supports no inline emphasis, raw HTML or images (see the module
    docstring and the work-package deviations)."""
    blocks = []
    for m in _BLOCK_RE.finditer(section_html):
        chunk = m.group(0)
        if chunk.startswith("<table"):
            t = _table_md(chunk)
            if t:
                blocks.append(t)
        elif chunk.startswith("<figure"):
            f = _figure_md(chunk)
            if f:
                blocks.append(f)
        else:
            inner = _LINK_RE.sub(lambda mm: f"[{_text(mm.group(2))}]({mm.group(1)})", chunk)
            txt = _text(inner)
            if txt:
                blocks.append(txt)
    return "\n\n".join(blocks)


def parse_beats(html_text: str) -> list[dict]:
    """``[{"scene", "h2", "h3", "body"}, ...]`` for every ``<section class="beat">`` in
    ``html_text``, in document order (15 for the current msata_finding.html; the function
    itself makes no assumption about the count)."""
    out = []
    for scene, body in _SECTION_RE.findall(html_text):
        h2m, h3m = _H2_RE.search(body), _H3_RE.search(body)
        rest = re.sub(r"<h[23]>.*?</h[23]>", "", body, flags=re.S)
        out.append({"scene": scene, "h2": _text(h2m.group(1)) if h2m else "",
                    "h3": _text(h3m.group(1)) if h3m else "", "body": rest})
    return out


class NIRSpec6927NarrativeAdapter:
    name = "nirspec6927_narrative"
    tiers = ("A",)

    # -- raw data (Tier A: payloads already embedded in the rendered legacy HTML) -----------
    def _msata(self, ctx) -> dict:
        from portalkit.legacy import extract
        p = LEGACY_DIR / "nirspec_6927_msata_finding.html"
        ctx.inputs.add(p)
        text = p.read_text(encoding="utf-8")
        data = extract.payload(text, names=("DATA",))
        return {**data, "beats": parse_beats(text)}

    def _refmap(self, ctx) -> dict:
        from portalkit.legacy import extract
        p = LEGACY_DIR / "nirspec_6927_refstar_map.html"
        ctx.inputs.add(p)
        return extract.payload(p, names=("DATA",))

    def _h2(self, ctx) -> list[dict]:
        from portalkit.legacy import extract
        p = LEGACY_DIR / "nirspec_6927_h2.html"
        ctx.inputs.add(p)
        rows = extract.payload(p, names=("DATA",))
        sp = LEGACY_DIR / "nirspec_6927_sources.json"
        ctx.inputs.add(sp)
        pos = {}
        if sp.is_file():
            for r in json.loads(sp.read_text(encoding="utf-8")):
                if r.get("SrcName"):
                    pos[r["SrcName"]] = (_num(r.get("RA")), _num(r.get("Dec")))
        out = []
        for r in rows:
            ra, dec = pos.get(r.get("name"), (None, None))
            out.append({**r, "ra": ra, "dec": dec})
        return out

    # -- datasets ---------------------------------------------------------------------------
    def datasets(self, ctx):
        from portalkit.spec import ReadmeSpec, TierUnavailable
        if ctx.tier not in self.tiers:
            raise TierUnavailable(f"nirspec6927_narrative implements tier A only, not {ctx.tier}")
        msata, refmap, h2rows = self._msata(ctx), self._refmap(ctx), self._h2(ctx)
        ds: dict = {}
        ds["m_cand"], ds["m_ta"], ds["m_v18"], ds["m_ovl"] = self._msata_catalogs(msata, ctx)
        ds["rm_v18"], ds["rm_v20"], ds["rm_sel"], ds["rm_ovl"] = self._refmap_catalogs(refmap, ctx)
        ds["h2"], ds["h2lines"], ds["h2img"] = self._h2_catalogs(h2rows, ctx)
        ds["refcheck"] = self._refcheck(ctx)
        ds["narrative"] = self._narrative(msata, ds, ctx)
        ds["readme"] = ReadmeSpec(markdown=self._readme_md(msata, refmap, h2rows, ds))
        self._msata_cache, self._refmap_cache = msata, refmap
        return ds

    # -- MSATA: candidates, acquisition-field boxes, vetted v18 ------------------------------
    def _msata_catalogs(self, msata: dict, ctx):
        from portalkit.spec import CatalogSpec, OverlaySpec, catalog_from_rows
        cand_rows, ta_rows = [], []
        for p in msata["pointings"]:
            v = p["number"]
            for c in _lim(p.get("candidates") or [], ctx):
                cand_rows.append({"rowid": f"p{v}_{c['id']}", "ra": c["ra"], "dec": c["dec"],
                                  "m": c["m"], "q": c["q"], "pointing": str(v), "nin": c.get("nin"),
                                  "nbr": c.get("nbr"), "ncomp": c.get("ncomp"), "nsat": c.get("nsat"),
                                  "wm": c.get("wm"), "ws": c.get("ws"), "nbr_apt": c.get("nbr_apt"),
                                  "clean": bool(c.get("clean"))})
        # ``ta_fields`` alone only carries the o028 (failed) pointing's acquisition fields;
        # ``ta_by_visit`` has the same per-field rows keyed by every visit that was actually
        # measured (19, 22, 28 in the current legacy payload -- o023 has none), which is what
        # the beat prose's own per-visit win/loss stats describe. Flatten it instead so every
        # visit it covers gets real ``ta_wins_<v>``/``ta_loses_<v>`` overlay layers.
        for f in (r for rows in msata["ta_by_visit"].values() for r in rows):
            b = f.get("brightest") or {}
            ta_rows.append({"rowid": f"ta{f['visit']}_{f['idx']}", "ra": f["ra"], "dec": f["dec"],
                            "visit": str(f["visit"]), "det": f.get("det"), "ref_mag": f.get("ref_mag"),
                            "ref_sep": f.get("ref_sep"), "nsrc": f.get("nsrc"), "nmpt": f.get("nmpt"),
                            "nbrighter": f.get("nbrighter"), "nsat": f.get("nsat"),
                            "nbright": f.get("nbright"), "wins": bool(f.get("wins")),
                            "brightest_m": b.get("m"), "brightest_sep": b.get("sep"),
                            "brightest_sat": bool(b.get("sat"))})
        v18_rows = [{"rowid": str(r["id"]), "ra": r["ra"], "dec": r["dec"], "m": r["m"], "q": r["q"],
                    "pointing": r.get("p"), "nin": r.get("nin"), "new": bool(r.get("new"))}
                   for r in _lim(msata["v18"], ctx)]
        cand = catalog_from_rows(cand_rows, "rowid", "ra", "dec", key="m_cand",
                                 roles={"q": "category", "pointing": "category", "clean": "flag",
                                        "m": "mag", "wm": "mag"},
                                 columns={"nin": {"group": "msata"}, "nbr": {"group": "msata"},
                                          "ncomp": {"group": "msata"}, "nsat": {"group": "msata"}},
                                 title=f"MSATA reference-star candidate pool ({len(cand_rows)} rows, 4 pointings)")
        ta = catalog_from_rows(ta_rows, "rowid", "ra", "dec", key="m_ta",
                               roles={"visit": "category", "det": "category", "wins": "flag",
                                      "brightest_sat": "flag", "ref_mag": "mag", "brightest_m": "mag"},
                               title=f"MSATA acquisition fields ({len(ta_rows)})")
        v18 = catalog_from_rows(v18_rows, "rowid", "ra", "dec", key="m_v18",
                                roles={"q": "category", "pointing": "category", "new": "flag", "m": "mag"},
                                title=f"Re-vetted reference-star candidates (v18, {len(v18_rows)})")
        layers = []
        for v in sorted({r["visit"] for r in ta_rows}):
            wins = [r for r in ta_rows if r["visit"] == v and r["wins"]]
            loses = [r for r in ta_rows if r["visit"] == v and not r["wins"]]
            for lid, rows, col in ((f"ta_wins_{v}", wins, "#5ad17e"), (f"ta_loses_{v}", loses, "#e0607a")):
                layers.append({"id": lid, "name": f"o{v} fields ({'ref wins' if 'wins' in lid else 'ref loses'})",
                              "space": "sky", "kind": "boxes", "default": False,
                              "style": {"color": col, "width": 2},
                              "items": [{"poly": _box_poly(r["ra"], r["dec"]), "id": r["rowid"]} for r in rows]})
        ovl = OverlaySpec(layers=layers)
        return cand, ta, v18, ovl

    # -- narrative: 15-beat MSATA scrollytelling ---------------------------------------------
    def _narrative(self, msata: dict, ds, ctx) -> "object":
        from portalkit.build import PathLabeler
        from portalkit.spec import NarrativeSpec
        # the legacy beat prose quotes several absolute /orange paths verbatim (e.g. the deep
        # catalogue FITS file); narrative.json is a served file, so every local path is
        # labelled before it leaves this module, reusing the same PathLabeler the rest of the
        # build uses for served paths (ARCHITECTURE.md section 9.3 "Served paths") rather than
        # a bespoke redaction.
        labeler = PathLabeler.for_site(ctx.site)
        pts = {p["number"]: p for p in msata["pointings"]}
        ra0 = sum(p["ra"] for p in msata["pointings"]) / max(1, len(msata["pointings"]))
        dec0 = sum(p["dec"] for p in msata["pointings"]) / max(1, len(msata["pointings"]))
        overview = {"ra": ra0, "dec": dec0, "fov": 0.045}
        all_layers = [lay["id"] for lay in ds["m_ovl"].layers]

        def center(v, fov):
            p = pts.get(v)
            return {"ra": p["ra"], "dec": p["dec"], "fov": fov} if p else dict(overview)

        def cand_ids(v, pred=lambda c: True):
            p = pts.get(v)
            return [f"p{v}_{c['id']}" for c in ((p or {}).get("candidates") or []) if pred(c)]

        def ta_ids(v):
            return [f"ta{v}_{f['idx']}" for f in msata["ta_by_visit"].get(str(v), [])]

        def only(*on):
            return [x for x in all_layers if x not in on]
        worst = msata.get("worst_field") or {}
        views = {
            1: {"sky": overview, "on": [], "off": all_layers},
            2: {"sky": center(22, 0.014), "hl": {"ds": "m_cand", "ids": cand_ids(22)}, "on": [], "off": all_layers},
            3: {"sky": center(19, 0.014), "hl": {"ds": "m_cand", "ids": cand_ids(19)}, "on": [], "off": all_layers},
            4: {"sky": center(22, 0.014), "hl": {"ds": "m_ta", "ids": ta_ids(22)},
                "on": ["ta_wins_22", "ta_loses_22"], "off": only("ta_wins_22", "ta_loses_22")},
            5: {"sky": center(19, 0.014), "hl": {"ds": "m_ta", "ids": ta_ids(19)},
                "on": ["ta_wins_19", "ta_loses_19"], "off": only("ta_wins_19", "ta_loses_19")},
            6: {"sky": center(28, 0.016), "hl": {"ds": "m_cand", "ids": cand_ids(28)}, "on": [], "off": all_layers},
            7: {"sky": center(28, 0.016), "hl": {"ds": "m_ta", "ids": ta_ids(28)},
                "on": ["ta_wins_28", "ta_loses_28"], "off": only("ta_wins_28", "ta_loses_28")},
            8: {"sky": center(28, 0.016),
                "hl": {"ds": "m_ta", "ids": [f"ta{worst['visit']}_{worst['idx']}"] if "idx" in worst else []},
                "on": ["ta_wins_28", "ta_loses_28"], "off": only("ta_wins_28", "ta_loses_28")},
            9: {"sky": overview, "on": [], "off": all_layers},
            10: {"sky": center(28, 0.016), "hl": {"ds": "m_ta", "ids": ta_ids(28)},
                 "on": ["ta_wins_28", "ta_loses_28"], "off": only("ta_wins_28", "ta_loses_28")},
            11: {"sky": center(23, 0.014), "hl": {"ds": "m_cand", "ids": cand_ids(23, lambda c: c.get("nbr", 0) > 0)},
                 "on": [], "off": all_layers},
            12: {"sky": center(23, 0.014), "hl": {"ds": "m_cand", "ids": cand_ids(23)}, "on": [], "off": all_layers},
            13: {"sky": center(23, 0.014), "hl": {"ds": "m_cand", "ids": cand_ids(23, lambda c: c.get("clean"))},
                 "on": [], "off": all_layers},
            14: {"sky": overview, "hl": {"ds": "m_v18", "ids": [str(r["id"]) for r in msata["v18"] if r.get("new")]},
                 "on": [], "off": all_layers},
            15: {"sky": overview, "on": [], "off": all_layers, "suppress_view": True},
        }
        beats = []
        for i, b in enumerate(msata["beats"], start=1):
            v = dict(views.get(i, {"sky": overview, "on": [], "off": all_layers}))
            view = {"pane": "sky", "sky": v["sky"], "layers_on": v.get("on", []), "layers_off": v.get("off", [])}
            if v.get("hl") and v["hl"].get("ids"):
                view["highlight"] = v["hl"]
            if v.get("suppress_view"):
                view["suppress_view"] = True
            title = b["h3"] or b["h2"] or f"Beat {i}"
            md = beat_markdown(b["body"]) or "(no text)"
            beats.append({"id": f"n_beat{i:02d}", "title": labeler.scrub_text(title),
                         "markdown": labeler.scrub_text(md), "view": view})
        return NarrativeSpec(beats=beats)

    # -- H2 excitation report ----------------------------------------------------------------
    def _h2_catalogs(self, h2rows: list[dict], ctx):
        from portalkit.spec import ImageSetSpec, catalog_from_rows
        rows, line_rows, img_rows = [], [], []
        for r in _lim(h2rows, ctx):
            name = r["name"]
            verdict = r.get("verdict") or {}
            T_obs, T_cor, vib = r.get("T_obs") or {}, r.get("T_cor") or {}, r.get("vib") or {}
            rows.append({"rowid": name, "ra": r.get("ra"), "dec": r.get("dec"), "slit": r.get("slit"),
                        "obs": r.get("obs"), "sourceid": r.get("sourceid"), "n_meas": r.get("n_meas"),
                        "n_det": r.get("n_det"), "n_clean": r.get("n_clean"), "n_lim": r.get("n_lim"),
                        "A_V": _num(r.get("A_V")), "A_V_star": _num(r.get("A_V_star")),
                        "A_V_h2": _num(r.get("A_V_h2")), "T_obs_T": _num(T_obs.get("T")),
                        "T_obs_T_err": _num(T_obs.get("T_err")), "T_cor_T": _num(T_cor.get("T")),
                        "T_cor_T_err": _num(T_cor.get("T_err")), "T_v0": _num(vib.get("T_v0")),
                        "verdict": verdict.get("kind") or "unknown", "why": verdict.get("why") or ""})
            for i, ln in enumerate(r.get("lines") or []):
                line_rows.append({"rowid": f"{name}_{i}", "source": name, "name": ln.get("name"),
                                  "wl": _num(ln.get("wl")), "Eu": _num(ln.get("Eu")), "grating": ln.get("grating"),
                                  "kind": ln.get("kind") or "unknown", "snr": _num(ln.get("snr")),
                                  "f_si": _num(ln.get("f_si")), "ef_si": _num(ln.get("ef_si")),
                                  "usable": bool(ln.get("usable"))})
            img_rows.append({"id": name, "caption": f"{name}  o{r.get('obs')}  {verdict.get('kind', '')}",
                             "obs": r.get("obs"), "h2_2d": _rel(f"h2/{name}_h2_2d.png"),
                             "h2_profiles": _rel(f"h2/{name}_h2_profiles.png"),
                             "h2_rotation": _rel(f"h2/{name}_h2_rotation.png")})
        cat = catalog_from_rows(rows, "rowid", "ra", "dec", key="h2",
                                roles={"obs": "category", "verdict": "category", "why": "text",
                                       "slit": "text", "sourceid": "text", "A_V": "value",
                                       "A_V_star": "value", "A_V_h2": "value"},
                                title=f"H2 excitation report ({len(rows)} sources)")
        cat.caveats = ["verdict/why are carried verbatim from the legacy page's per-source payload "
                       "(no separate VERDICTS lookup table exists to import)."]
        lines = catalog_from_rows(line_rows, "rowid", key="h2lines",
                                  roles={"source": "category", "grating": "category", "kind": "category",
                                         "usable": "flag", "name": "text"},
                                  title=f"H2 line measurements ({len(line_rows)} lines, rotation-diagram proxy)")
        img = ImageSetSpec.from_rows(img_rows, {"h2_2d": ("h2_2d", "2D spectrum"),
                                                "h2_profiles": ("h2_profiles", "Spatial profiles"),
                                                "h2_rotation": ("h2_rotation", "Rotation diagram")},
                                     "h2img", tags=["obs"], catalog="h2", join="rowid")
        return cat, lines, img

    # -- reference-star map (rings tool) ------------------------------------------------------
    def _refmap_catalogs(self, refmap: dict, ctx):
        from portalkit.spec import CatalogSpec, OverlaySpec, catalog_from_rows

        def flatten(key, limit_ctx=True):
            rows, seen = [], set()
            for pointing, cands in (refmap.get(key) or {}).items():
                for c in _lim(cands, ctx) if limit_ctx else cands:
                    ra, dec, m, q, sid = c
                    rid = f"{pointing}_{sid}"
                    n = 2
                    while rid in seen:
                        rid, n = f"{pointing}_{sid}_{n}", n + 1
                    seen.add(rid)
                    rows.append({"rowid": rid, "ra": ra, "dec": dec, "m": m, "q": q,
                                "src_id": str(sid), "pointing": pointing})
            return rows
        v18_rows, v20_rows = flatten("v18"), flatten("v20")
        sel_rows = [{"rowid": str(sid), "ra": ra, "dec": dec, "m": m, "flag": int(flag), "src_id": str(sid)}
                   for ra, dec, m, flag, sid in _lim(refmap.get("selected") or [], ctx)]
        v18 = catalog_from_rows(v18_rows, "rowid", "ra", "dec", key="rm_v18",
                                roles={"q": "category", "pointing": "category", "m": "mag"},
                                title=f"Reference-star candidates v18 ({len(v18_rows)})")
        v20 = catalog_from_rows(v20_rows, "rowid", "ra", "dec", key="rm_v20",
                                roles={"q": "category", "pointing": "category", "m": "mag"},
                                title=f"Reference-star candidates v20 ({len(v20_rows)})")
        sel = catalog_from_rows(sel_rows, "rowid", "ra", "dec", key="rm_sel",
                                roles={"flag": "category", "m": "mag"},
                                title=f"Selected reference stars ({len(sel_rows)})")
        quads = []
        for p in refmap.get("pointings") or []:
            for j, poly in enumerate(p.get("quads") or []):
                quads.append({"poly": [[float(a), float(b)] for a, b in poly],
                             "id": f"p{p['n']}_q{j}", "label": f"o{p['n']} quadrant {j}"})
        ovl = OverlaySpec(layers=[{"id": "msa_quads", "name": "MSA quadrant footprints", "space": "sky",
                                   "kind": "polygons", "default": True,
                                   "style": {"color": "#8fa8ff", "width": 1}, "items": quads}])
        return v18, v20, sel, ovl

    # -- reference-star check contact sheets --------------------------------------------------
    def _refcheck(self, ctx):
        from portalkit.spec import ImageSetSpec
        d = LEGACY_DIR / "refstar_check"
        rows = []
        if d.is_dir():
            for fn in sorted(os.listdir(d)):
                if not fn.lower().endswith(".png"):
                    continue
                stem = os.path.splitext(fn)[0]
                m = re.search(r"_o(\d{3})_", fn)
                rows.append({"id": stem, "caption": stem, "obs": m.group(1) if m else "pool",
                            "img": _rel(f"refstar_check/{fn}")})
        rows = _lim(rows, ctx)
        return ImageSetSpec.from_rows(rows, {"img": ("img", "Contact sheet")}, "refcheck", tags=["obs"])

    def _readme_md(self, msata, refmap, h2rows, ds) -> str:
        n_cand, n_ta, n_v18 = ds["m_cand"].n or 0, ds["m_ta"].n or 0, ds["m_v18"].n or 0
        return (f"# NIRSpec 6927: narrative pages and reference-star QA\n\n"
                f"Four MSATA pointings (o019, o022, o023 succeeded; o028 failed) across "
                f"{n_cand} re-vetted candidates and {n_ta} measured acquisition fields; "
                f"{n_v18} stars pass the stricter v18 reference-star rule. "
                f"{len(h2rows)} sources have an H2 excitation report. "
                f"{ds['refcheck'].n if hasattr(ds['refcheck'], 'n') else len(ds['refcheck'].items)} "
                "reference-star contact sheets.\n\n## Pages\n\n"
                "- **MSATA scrollytelling** (`n_msata`): why o028 failed and the other three converged.\n"
                "- **H2 report** (`n_h2`): per-source excitation verdicts and line measurements.\n"
                "- **Reference-star map** (`n_refstar_map`): v18/v20 candidate catalogues, the 8 "
                "selected stars, MSA quadrant footprints, click-rings.\n"
                "- **Reference-star check** (`n_refstar_check`): F115W contact sheets.\n\n"
                "## Caveats\n\n"
                "- Deploys into the same `nirspec_6927/unified` target as the core nirspec6927 "
                "portal (WP22), under a separate portal id; every page id here is prefixed `n_`.\n"
                "- Proprietary data (program 6927); every link points to the secure site.\n")

    # -- pages --------------------------------------------------------------------------------
    def pages(self, ctx, ds):
        from portalkit.spec import PageSpec as P
        ovl_ids = [lay["id"] for lay in ds["m_ovl"].layers]
        links = {"map": "nirspec_6927_map.html#id={id}", "spectrum": "nirspec_6927_specviewer.html#id={id}"}
        cards = [{"page": "n_msata", "text": "Why o028's MSA target acquisition failed and three others converged."},
                 {"page": "n_h2", "text": f"H2 excitation verdicts for {ds['h2'].n or 0} sources."},
                 {"page": "n_refstar_map", "text": "Reference-star candidate map, v18/v20, click-rings."},
                 {"page": "n_refstar_check", "text": "Per-candidate F115W contact sheets."},
                 {"href": _rel("index.html"), "title": "NIRSpec 6927 core portal", "auth": "secure",
                  "text": "Spectra, ice columns, CO gas, SEDs."}]
        return [
            P(id="n_index", archetype="landing", title="NIRSpec 6927: narrative + QA", datasets=["readme"],
              layout={"pane": "landing", "options": {"cards": cards, "counts": True, "search": True}}),
            P(id="n_msata", archetype="narrative", title="MSATA: a visit that failed",
              subtitle="15-beat scrollytelling", datasets=["narrative", "m_cand", "m_ta", "m_v18", "m_ovl"],
              layout={"row": [
                  {"pane": "sky", "dataset": "m_cand", "options": {"overlays": ovl_ids}, "size": 480},
                  {"pane": "narrative", "dataset": "narrative",
                   "options": {"narrative": "narrative", "sticky": "sky"}}]}),
            P(id="n_h2", archetype="explorer", title="H2 excitation report",
              datasets=["h2", "h2lines", "h2img"], links=links,
              layout={"row": [{"pane": "list", "dataset": "h2", "size": 250},
                              {"pane": "scatter", "dataset": "h2lines",
                               # linear, not log, y: a third of the line measurements -- every
                               # "negative"-kind row and most "upper" rows -- have f_si <= 0,
                               # and the scatter pane's log transform drops non-positive values
                               # (web/js/panes/scatter.js), which would make them vanish silently.
                               "options": {"x": "Eu", "y": "f_si", "color": "kind"}},
                              {"col": [{"pane": "detail", "dataset": "h2",
                                       "options": {"template": "compact", "images": "h2img", "prose": ["why"]}},
                                      {"pane": "gallery", "dataset": "h2img",
                                       "options": {"images": "h2img", "roles": ["h2_2d", "h2_profiles", "h2_rotation"]}}],
                               "size": 420}]}),
            P(id="n_refstar_map", archetype="explorer", title="Reference-star candidate map",
              subtitle="v18 / v20, selected stars, quadrant footprints",
              datasets=["rm_v18", "rm_v20", "rm_sel", "rm_ovl"],
              layout={"row": [{"pane": "list", "dataset": "rm_v18", "size": 250},
                              {"pane": "sky", "dataset": "rm_v18",
                               # the legacy page's own showRing() vetting-criteria circles
                               # (nirspec_6927_refstar_map.html): the 2" isolation radius and
                               # the 3" spike-contamination radius (ARCHITECTURE.md section 14.7).
                               "options": {"overlays": ["msa_quads"], "rings": [2, 3]}},
                              {"pane": "detail", "dataset": "rm_v18", "size": 360}]}),
            P(id="n_refstar_check", archetype="gallery", title="Reference-star check",
              subtitle="F115W contact sheets", datasets=["refcheck"],
              layout={"pane": "gallery", "dataset": "refcheck", "options": {"images": "refcheck"}}),
        ]


ADAPTER = NIRSpec6927NarrativeAdapter()
