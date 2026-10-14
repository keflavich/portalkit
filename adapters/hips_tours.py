"""RGB and ACES wavelength-tour HiPS pages (WP31 extras; ARCHITECTURE.md section 14, wave 6).

Legacy: two public, pre-rendered Aladin tour families under ``/orange/adamginsburg/web/public``:

* ``avm_images/<field>_rgb_wavelength_tour.html`` -- an ``A.aladin`` viewer that steps through a
  ``const STEPS = [{"name": "187-182-115", "um": 1.87, "hips": "Brick_RGB_187-182-115_hips"}, ...]``
  list of pre-built RGB HiPS composites (the ``name`` field encodes the three JWST filter
  wavelengths times 100, dash-separated).
* ``ACES_Aladin_tour/<field>_wavelength_tour[_<variant>].html`` -- the same style of viewer, but
  it loads its waypoints from a matching external
  ``ACES_Aladin_tour/waypoints_<field>_wavelength[_<variant>].json`` (``wd2.py``'s own wavelength
  tour follows the same waypoints-JSON shape, from a different legacy directory -- no overlap).

Both families' HiPS tiles live as sibling directories next to the HTML, served from the same
public AVM tree (``AVM`` below); a tour's "rungs" are just the directory names it steps through.
This adapter discovers tours on disk (globs, not a hardcoded field list) so a new field dropped
into either directory is picked up without a code change, and builds one ``wavelength``-archetype
page per discovered tour. Tier A only (``tiers = ("A",)``): both HTML families already embed or
reference their own ready-made rung lists; nothing is computed.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from portalkit.legacy import extract

RGB_DIR = Path("/orange/adamginsburg/web/public/avm_images")
ACES_DIR = Path("/orange/adamginsburg/web/public/ACES_Aladin_tour")
#: Public AVM HiPS tree both tour families' rungs live under (confirmed against
#: avm_images/index.html; adapters/wd2.py's own wavelength-tour page uses the same constant).
AVM = "https://data.rc.ufl.edu/pub/adamginsburg/avm_images/"
#: Served root of the public tree and its URL (``AVM`` is ``PUB + "avm_images/"``).
WEB_PUBLIC = Path("/orange/adamginsburg/web/public")
PUB = "https://data.rc.ufl.edu/pub/adamginsburg/"
#: Where a rung HiPS missing from ``avm_images/`` is looked for: the per-field
#: ``jwst/<field>/pngs_<grid>`` directories. The legacy ACES pages name their HiPS by bare
#: directory name and resolve it against ``avm_images/`` on data.rc.ufl.edu, where the W51
#: and SgrB2 composites of five tours do not exist (404); the same names do exist under
#: ``jwst/w51/pngs_{140,480,2100}`` and ``jwst/sgrb2/pngs_{150,466}``. HiPS names carry the
#: field prefix (``w51_RGB_...``, ``SgrB2_...``), so a name match is unambiguous across fields;
#: within a field the smallest numeric grid (finest pixel scale) is taken first.
HIPS_FALLBACK_GLOB = "jwst/*/pngs_*"

_TARGET_RE = re.compile(r"target\s*:\s*'([\-0-9.]+)\s+([\-0-9.]+)'")
_FOV_RE = re.compile(r"fov\s*:\s*([0-9.eE+-]+)")
_FRAME_RE = re.compile(r"cooFrame\s*:\s*'([A-Za-z0-9]+)'")
_UM_RE = re.compile(r"([\d.]+)\s*(?:um|μm)", re.I)
_ACES_RE = re.compile(r"^(?P<field>.+)_wavelength_tour(?P<suffix>|_linear|_other|_linear_starsub)$")


def _sky_target(text: str) -> dict | None:
    """``HipsSpec.target`` is always equatorial (``portalkit.schema``'s ``_radec`` check), but
    two of the seven RGB tours set ``cooFrame: 'galactic'`` and give their ``target:`` as
    l/b (confirmed against ``crowded_l3_nrc{a,b}_rgb_wavelength_tour.html``); those are
    converted here rather than passed through as if they were ra/dec."""
    tm, fm = _TARGET_RE.search(text), _FOV_RE.search(text)
    if not tm:
        return None
    a, b = float(tm.group(1)), float(tm.group(2))
    frm = _FRAME_RE.search(text)
    if frm and frm.group(1).lower() == "galactic":
        from astropy.coordinates import SkyCoord
        c = SkyCoord(l=a, b=b, unit="deg", frame="galactic").icrs
        a, b = c.ra.deg, c.dec.deg
    out = {"ra": a, "dec": b}
    if fm:
        out["fov"] = float(fm.group(1))
    return out


def _um_triplet(name: str) -> dict:
    """``"187-182-115"`` -> ``{"r_um": 1.87, "g_um": 1.82, "b_um": 1.15}``; anything that is not
    exactly 3 dash-separated numbers gives ``{}`` (no r/g/b hint, still a valid rung)."""
    parts = name.split("-")
    if len(parts) != 3:
        return {}
    try:
        vals = [float(p) / 100.0 for p in parts]
    except ValueError:
        return {}
    return dict(zip(("r_um", "g_um", "b_um"), vals))


def _grid_key(d: Path) -> tuple:
    """Sort key of a ``pngs_<grid>`` directory: numeric grids ascending, then the rest."""
    tail = d.name[len("pngs_"):]
    return (0, int(tail), "") if tail.isdigit() else (1, 0, tail)


class HipsResolver:
    """Map a rung's HiPS directory name to a public URL whose directory exists on disk.

    ``avm_images/<name>`` is used when present (the legacy location); otherwise the first
    ``HIPS_FALLBACK_GLOB`` directory (finest grid first) holding ``<name>``. ``missing`` collects
    the names found nowhere. Only URLs under ``AVM`` are resolved; any other URL is kept."""

    def __init__(self, web_public: Path = WEB_PUBLIC, pub: str = PUB):
        self.web_public, self.pub = Path(web_public), pub
        self._index: dict[str, str] | None = None
        self.missing: list[str] = []
        self.moved: dict[str, str] = {}

    def _build_index(self) -> dict[str, str]:
        idx: dict[str, str] = {}
        dirs = sorted(self.web_public.glob(HIPS_FALLBACK_GLOB),
                      key=lambda d: (d.parent.name, _grid_key(d)))
        for d in dirs:
            try:
                names = sorted(os.listdir(d))
            except OSError:
                continue
            rel = d.relative_to(self.web_public).as_posix()
            for n in names:
                if n.endswith("_hips"):
                    idx.setdefault(n, f"{rel}/{n}")
        return idx

    def url(self, url: str) -> str | None:
        """The resolved URL, or ``None`` when the HiPS directory exists nowhere."""
        if not url.startswith(AVM):
            return url
        name = url[len(AVM):].strip("/")
        if (self.web_public / "avm_images" / name).is_dir():
            return url
        if self._index is None:
            self._index = self._build_index()
        rel = self._index.get(name)
        if rel is None:
            if name not in self.missing:
                self.missing.append(name)
            return None
        self.moved[name] = rel
        return f"{self.pub}{rel}/"

    def resolve(self, rungs: list[dict]) -> list[dict]:
        """``rungs`` with resolved URLs; rungs whose HiPS exists nowhere are dropped and
        renumbered (``rung0``...) so ids stay dense."""
        out = []
        for r in rungs:
            u = self.url(r["url"])
            if u is None:
                continue
            out.append({**r, "id": f"rung{len(out)}", "url": u})
        return out


def discover_rgb_tours(ctx) -> dict[str, dict]:
    """``{field: {"text", "target"}}`` for every ``avm_images/*_rgb_wavelength_tour.html``."""
    out: dict[str, dict] = {}
    if not RGB_DIR.is_dir():
        return out
    for p in sorted(RGB_DIR.glob("*_rgb_wavelength_tour.html")):
        ctx.inputs.add(p)
        field = p.name[: -len("_rgb_wavelength_tour.html")]
        text = p.read_text(encoding="utf-8")
        out[field] = {"text": text, "target": _sky_target(text)}
    return out


def rgb_rungs(text: str) -> list[dict]:
    # most *_rgb_wavelength_tour.html pages embed ``const STEPS = [...]``; a couple
    # (crowded_l3_nrca/nrcb) use ``const FILTERS = [...]`` instead -- same {name, um, hips}
    # shape (FILTERS additionally carries a numeric "tag", unused here).
    steps = extract.payload(text, names=("STEPS", "FILTERS"))
    rungs = []
    for i, s in enumerate(steps):
        rungs.append({"id": f"rung{i}", "label": s.get("name", f"step{i}"),
                     "url": f"{AVM}{str(s['hips']).strip('/')}/",
                     "desc": f"{s.get('um')} μm" if s.get("um") is not None else "",
                     **_um_triplet(str(s.get("name", "")))})
    return rungs


def discover_aces_tours(ctx) -> dict[str, dict]:
    """``{f"{field}{suffix}": {"text", "waypoints_path"}}`` for every ACES tour HTML that has a
    matching ``waypoints_<field>_wavelength<suffix>.json`` sibling with a non-empty wavelength
    slider; tours with no matching/usable waypoints file are skipped (not raised)."""
    out: dict[str, dict] = {}
    if not ACES_DIR.is_dir():
        return out
    for p in sorted(ACES_DIR.glob("*_wavelength_tour*.html")):
        m = _ACES_RE.match(p.stem)
        if not m:
            continue
        field, suffix = m.group("field"), m.group("suffix")
        way_path = ACES_DIR / f"waypoints_{field}_wavelength{suffix}.json"
        if not way_path.is_file():
            continue
        ctx.inputs.add(p)
        ctx.inputs.add(way_path)
        out[f"{field}{suffix}"] = {"text": p.read_text(encoding="utf-8"), "waypoints_path": way_path}
    return out


def aces_rungs(waypoints_path: Path) -> tuple[list[dict], dict | None]:
    """``(rungs, target)`` from the first waypoint with a non-empty wavelength slider, or
    ``([], None)`` if the file is missing/malformed/has none (never raises)."""
    try:
        doc = json.loads(waypoints_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], None
    for way in doc.get("waypoints") or []:
        wls = ((way.get("wavelength_slider") or {}).get("wavelengths")) or []
        if not wls:
            continue
        rungs = []
        for i, w in enumerate(wls):
            label = str(w.get("label", f"step{i}"))
            rungs.append({"id": f"rung{i}", "label": label,
                         "url": f"{AVM}{str(w.get('url', '')).strip('/')}/",
                         "desc": str(w.get("description", "")),
                         **(dict(zip(("r_um", "g_um", "b_um"), [float(x) for x in _UM_RE.findall(label)]))
                            if len(_UM_RE.findall(label)) == 3 else {})})
        target = None
        if "ra" in way and "dec" in way:
            target = {"ra": float(way["ra"]), "dec": float(way["dec"]), "fov": float(way.get("fov", 0.3))}
        return rungs, target
    return [], None


def _resolver_caveats(res: HipsResolver) -> str:
    out = ""
    if res.moved:
        out += (f"- {len(res.moved)} HiPS composite(s) named by the legacy tours are not in "
                "`avm_images/` and are loaded from the field's `jwst/<field>/pngs_<grid>/` "
                "directory instead (finest grid available).\n")
    if res.missing:
        out += (f"- {len(res.missing)} tour step(s) omitted: no HiPS directory found for "
                + ", ".join(f"`{n}`" for n in res.missing) + ".\n")
    return out


class HipsToursAdapter:
    name = "hips_tours"
    tiers = ("A",)

    def datasets(self, ctx):
        from portalkit.spec import HipsSpec, ReadmeSpec, TierUnavailable
        if ctx.tier not in self.tiers:
            raise TierUnavailable(f"hips_tours implements tier A only, not {ctx.tier}")
        ds: dict = {}
        tours: list[tuple[str, str, str]] = []  # (page_id, title, hips key)
        res = HipsResolver()
        rgb = discover_rgb_tours(ctx)
        for field, info in rgb.items():
            rungs = res.resolve(rgb_rungs(info["text"]))
            if not rungs:
                continue
            key = f"hips_rgb_{field}"
            base = [{"id": rungs[0]["id"], "name": rungs[0]["label"], "url": rungs[0]["url"], "default": True}]
            ds[key] = HipsSpec(base=base, rungs=rungs, target=info["target"])
            tours.append((f"rgb_{field}", f"{field.replace('_', ' ')}: RGB wavelength tour", key))
        aces = discover_aces_tours(ctx)
        for name, info in aces.items():
            rungs, target = aces_rungs(info["waypoints_path"])
            rungs = res.resolve(rungs)
            if not rungs:
                continue
            key = f"hips_aces_{name}"
            base = [{"id": rungs[0]["id"], "name": rungs[0]["label"], "url": rungs[0]["url"], "default": True}]
            ds[key] = HipsSpec(base=base, rungs=rungs, target=target)
            tours.append((f"aces_{name}", f"{name.replace('_', ' ')}: ACES wavelength tour", key))
        self._tours = tours
        n_rgb, n_aces = sum(1 for t in tours if t[0].startswith("rgb_")), \
            sum(1 for t in tours if t[0].startswith("aces_"))
        ds["readme"] = ReadmeSpec(markdown=(
            "# Wavelength tours\n\n"
            f"{n_rgb} RGB wavelength tour(s) from `avm_images/`, {n_aces} ACES wavelength "
            "tour(s) from `ACES_Aladin_tour/`. Each steps an Aladin HiPS view through a "
            "sequence of pre-built multi-band composites.\n\n"
            "## Caveats\n\n- Public data; every rung is a plain HiPS tile tree, no catalogue "
            "or photometry is attached.\n" + _resolver_caveats(res)))
        return ds

    def pages(self, ctx, ds):
        from portalkit.spec import PageSpec as P
        cards = [{"page": pid, "text": title} for pid, title, _ in self._tours]
        pages = [P(id="index", archetype="landing", title="Wavelength tours", datasets=["readme"],
                   layout={"pane": "landing", "options": {"cards": cards, "counts": True, "search": True}})]
        for pid, title, key in self._tours:
            pages.append(P(id=pid, archetype="wavelength", title=title, datasets=[key]))
        return pages


ADAPTER = HipsToursAdapter()
