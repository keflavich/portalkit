"""Build orchestration and ``portalkit check`` (ARCHITECTURE.md sections 9.4 and 5.11).

:func:`build_family` runs the section 9.4 pipeline for one family:

1. load ``sites/<family>.toml`` (:func:`portalkit.context.load_site`);
2. check the declared inputs (a tmp input raises :class:`~portalkit.deploy.BlockedInput`,
   exit 5, unless ``allow_tmp_inputs``);
3. run ``adapter.datasets(ctx)`` and ``adapter.pages(ctx, ds)``;
4. write every dataset a built page uses: ``bundle.catalog.write`` (or
   ``legacy.gc_manifest.write`` for a foreign catalog), ``bundle.records.write``,
   ``bundle.images.write``, the hips/overlays/narrative/isochrones documents, physics
   datasets that differ from the shared defaults, and the shared ``{res}`` resources;
5. per page: ``caps.resolve`` -> ``config.assemble`` -> ``render.shell.write``;
6. write ``portal.json`` and ``build_report.json``;
7. run :func:`check_build` on the staged tree.

The staged tree is ``<stage_root>/<family>/<build_id>/<portal_id>/``. Shared resources are
written to ``<stage_root>/<family>/<build_id>/_res/`` (the tree ``portalkit publish-res``
publishes) or, with ``vendor_assets``, into ``<portal>/res/`` next to a copy of ``web/`` under
``<portal>/_pk/v<version>/``.

:func:`check_build` validates a staged portal (or every portal of a build directory):
``portal.json``, every page (HTML and embedded page.json budgets, schema, dataset URLs that
resolve inside the tree), every catalog manifest, records index and images document (schema,
referenced files, file budgets), the first-paint budget and, for gate ``G2``, the absence of
``json-legacy`` record sets.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import importlib
import json
import math
import os
import re
import sys
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from . import caps as capsmod
from . import config as configmod
from .bundle import catalog as catalog_writer
from .bundle import images as images_writer
from .bundle import records as records_writer
from .bundle.writer import BundleError, BundleWriter, Ledger, atomic_write
from .constants import (COL_FILE_MAX, FIRST_PAINT_MAX_1M, FIRST_PAINT_MAX_5M, MANIFEST_MAX,
                        PAGE_HTML_MAX, PAGE_JSON_MAX, SHARD_MAX)
from .context import (BuildContext, Cache, InputRegistry, SiteConfig, cache_base, expand_inputs,
                      load_site)
from .deploy import DeployError, ExposureMap, UsageError
from .deploy.exposure import DEFAULT_ROOTS, URL_PREFIXES, scan
from .deploy.stage import (BUILD_REPORT, build_dir, check_stage_root, create_portal_stage_dir,
                           make_build_id, stage_root as default_stage_root)
from .legacy import gc_manifest
from .legacy.dirgallery import image_size
from .legacy.extract import LegacyExtractError, script_json
from .legacy.importshim import LegacyImporter, sha256_file
from .render import shell
from .schema import validate_catalog, validate_images, validate_page, validate_portal, \
    validate_records
from .spec import (CatalogSpec, ExtinctionSpec, FilterSetSpec, HipsSpec, ImageSetSpec,
                   IsochroneSpec, LegacyRef, NarrativeSpec, OverlaySpec, PageSpec, ReadmeSpec,
                   RecordSetSpec, TemplateSpec, TierUnavailable)
from .version import VERSION

__all__ = [
    "BuildResult", "BuildError", "CheckReport", "build_family", "check_build", "load_adapter",
    "REPO_ROOT", "WEB_DIR", "RES_DIRNAME", "local_path_for_url", "thumb_cache_dir",
    "thumb_cache_path", "make_thumbnail", "make_thumbs", "shared_resources", "GATES",
]

#: repository checkout root (``web/`` and ``adapters/`` live here; item 28)
REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = REPO_ROOT / "web"
#: build-level directory holding the shared ``{res}`` resources of a non-vendored build
RES_DIRNAME = "_res"
GATES = ("G1", "G2")

_PAGE_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_RES_SPEC_KIND = ((TemplateSpec, "templates"), (FilterSetSpec, "filters"),
                  (ExtinctionSpec, "extinction"))
_PROV_INPUTS_MAX = 50


class BuildError(DeployError):
    """A build step failed (exit 1): invalid page, writer error, check failure."""
    exit_code = 1


@dataclass
class BuildResult:
    build_id: str
    staging: Path                     # <stage_root>/<family>/<build_id>
    pages: list[str]                  # staged page files, '<portal_id>/<page_id>.html'
    report: dict
    ok: bool


# ---------------------------------------------------------------------------
# adapter loading
# ---------------------------------------------------------------------------

def load_adapter(ref: str) -> tuple[Any, str, str]:
    """Import ``"module.path:ATTR"``; returns ``(adapter, sha256 of the module source,
    module file)``. A class is instantiated. The repository root is put on ``sys.path`` so
    ``adapters.<family>`` resolves from a checkout."""
    if not isinstance(ref, str) or ":" not in ref:
        raise UsageError(f"adapter reference {ref!r} must look like 'adapters.module:ATTR'")
    mod_name, attr = ref.split(":", 1)
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    try:
        mod = importlib.import_module(mod_name)
    except ImportError as exc:
        raise UsageError(f"cannot import adapter module {mod_name!r}: {exc}") from exc
    try:
        obj = getattr(mod, attr)
    except AttributeError as exc:
        raise UsageError(f"adapter module {mod_name!r} has no attribute {attr!r}") from exc
    if isinstance(obj, type):
        obj = obj()
    for need in ("datasets", "pages"):
        if not callable(getattr(obj, need, None)):
            raise UsageError(f"adapter {ref!r} has no {need}() method")
    path = getattr(mod, "__file__", None) or ""
    sha = sha256_file(path) if path and os.path.isfile(path) else hashlib.sha256(ref.encode()).hexdigest()
    return obj, sha, path


# ---------------------------------------------------------------------------
# thumbnails (portalkit thumbs; build copies cached thumbnails into the image sets)
# ---------------------------------------------------------------------------

def local_path_for_url(url: str, roots: Mapping[str, os.PathLike | str] | None = None,
                       prefixes: Mapping[str, str] | None = None) -> Path | None:
    """The local file behind ``url``: a served URL (``URL_PREFIXES``) maps into its root
    (``DEFAULT_ROOTS``); ``file://`` URLs and absolute paths map to themselves. Returns None
    for other URLs and for files that do not exist. Only reads."""
    if not isinstance(url, str) or not url:
        return None
    roots = dict(DEFAULT_ROOTS if roots is None else roots)
    prefixes = dict(URL_PREFIXES if prefixes is None else prefixes)
    cand: Path | None = None
    if url.startswith("file://"):
        cand = Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path))
    elif url.startswith("/"):
        cand = Path(url)
    else:
        for kind, pre in prefixes.items():
            pre = pre if pre.endswith("/") else pre + "/"
            if url.startswith(pre) and kind in roots:
                rest = urllib.parse.unquote(urllib.parse.urlsplit(url[len(pre):]).path)
                if ".." in rest.split("/"):
                    return None
                cand = Path(roots[kind]) / rest
                break
    if cand is None or not cand.is_file():
        return None
    return cand


def thumb_cache_dir(family: str) -> Path:
    """``<cache base>/<family>/thumbs``."""
    return cache_base() / family / "thumbs"


def thumb_cache_path(cache_dir: os.PathLike | str, src: os.PathLike | str, max_px: int) -> Path:
    """Cache file of the thumbnail of ``src`` (keyed by path, mtime, size and ``max_px``)."""
    st = os.stat(src)
    key = f"{os.path.abspath(src)}|{st.st_mtime_ns}|{st.st_size}|{int(max_px)}"
    return Path(cache_dir) / f"{hashlib.sha256(key.encode()).hexdigest()[:24]}.png"


def make_thumbnail(src: os.PathLike | str, dest: os.PathLike | str, max_px: int = 480) -> bool:
    """Write a PNG thumbnail of ``src`` with its long side at most ``max_px`` (matplotlib
    extra). Returns False when ``src`` is already small enough or unreadable."""
    wh = image_size(src)
    if wh is None:
        return False
    scale = float(max_px) / max(wh)
    if scale >= 1.0:
        return False
    try:
        from matplotlib import image as mimage
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
    except ImportError as exc:
        raise UsageError("portalkit thumbs needs matplotlib (pip install portalkit[thumbs])") from exc
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + f".tmp{os.getpid()}.png")
    try:
        # matplotlib.image.thumbnail without its RGB-only assumption (greyscale PNGs
        # are drawn with a grey colormap); the Agg canvas leaves the pyplot backend alone.
        im = mimage.imread(str(src))
        rows, cols = im.shape[:2]
        dpi = 100
        fig = Figure(figsize=(max(cols * scale, 1.0) / dpi, max(rows * scale, 1.0) / dpi), dpi=dpi)
        FigureCanvasAgg(fig)
        ax = fig.add_axes((0, 0, 1, 1), aspect="auto", frameon=False, xticks=[], yticks=[])
        ax.imshow(im, aspect="auto", resample=True, interpolation="bilinear",
                  cmap="gray" if im.ndim == 2 else None)
        fig.savefig(str(tmp), dpi=dpi, format="png")
    except (OSError, ValueError, SyntaxError) as exc:
        if tmp.exists():
            tmp.unlink()
        raise BuildError(f"cannot make a thumbnail of {src}: {exc}") from exc
    os.replace(tmp, dest)
    return True


def _thumb_hook(cache_dir: Path, writer: BundleWriter, max_px: int) -> Callable[[str, str, str], str | None]:
    lock = threading.Lock()

    def hook(url: str, item_id: str, role: str) -> str | None:
        src = local_path_for_url(url)
        if src is None:
            return None
        try:
            p = thumb_cache_path(cache_dir, src, max_px)
        except OSError:
            return None
        if not p.is_file():
            return None
        stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{item_id}_{role}").strip("_")[:80] or "thumb"
        with lock:
            return writer.put("thumbs", stem, p.read_bytes(), "png", kind="images")
    return hook


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _iso(ts: _dt.datetime) -> str:
    return ts.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _res_kind(spec: Any) -> str | None:
    for cls, kind in _RES_SPEC_KIND:
        if isinstance(spec, cls):
            return kind
    return None


def _is_default_res(spec: Any) -> bool:
    """True when the shared ``{res}`` document serves ``spec`` (shared defaults: the
    jwst_template_spectra templates, every registry filter, G23 at R_V 3.1 and 5.5)."""
    if isinstance(spec, TemplateSpec):
        return spec == TemplateSpec()
    if isinstance(spec, ExtinctionSpec):
        return spec == ExtinctionSpec()
    if isinstance(spec, FilterSetSpec):
        return not spec.extra
    return False


def _layout_datasets(node: Any, keys: Iterable[str]) -> set[str]:
    """Dataset keys named anywhere in a layout tree (``dataset`` and string options)."""
    keys = set(keys)
    out: set[str] = set()

    def walk(v: Any) -> None:
        if isinstance(v, str):
            if v in keys:
                out.add(v)
        elif isinstance(v, Mapping):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
    if node is not None:
        walk(node)
    return out


def _needed_datasets(pages: list[PageSpec], ds: Mapping[str, Any]) -> list[str]:
    """Datasets the pages use (explicit, layout-referenced and implied catalogs), in
    adapter order."""
    need: set[str] = set()
    for p in pages:
        for k in p.datasets:
            if k not in ds:
                raise BuildError(f"page {p.id!r} uses unknown dataset {k!r}")
            need.add(k)
        need |= _layout_datasets(p.layout, ds)
    for k in list(need):
        s = ds[k]
        if isinstance(s, (RecordSetSpec, ImageSetSpec)) and s.catalog:
            if s.catalog not in ds:
                raise BuildError(f"dataset {k!r} references unknown catalog {s.catalog!r}")
            need.add(s.catalog)
    return [k for k in ds if k in need]


def _default_target(manifests: Mapping[str, Any], cats: Iterable[str]) -> dict | None:
    """Sky target covering the bounding boxes of the given catalogs."""
    ras: list[float] = []
    decs: list[float] = []
    for k in cats:
        bb = (manifests.get(k) or {}).get("bbox") if isinstance(manifests.get(k), Mapping) else None
        if isinstance(bb, Mapping) and bb.get("ra") and bb.get("dec"):
            ras += [float(x) for x in bb["ra"]]
            decs += [float(x) for x in bb["dec"]]
    if not ras:
        return None
    ra0, ra1, de0, de1 = min(ras), max(ras), min(decs), max(decs)
    dec = 0.5 * (de0 + de1)
    span = max((ra1 - ra0) * math.cos(math.radians(dec)), de1 - de0)
    return {"ra": round(0.5 * (ra0 + ra1), 6), "dec": round(dec, 6),
            "fov": round(min(180.0, max(0.05, 1.2 * span)), 4)}


#: Seconds a build may move its timestamp on to find a free build id (``_claim_staging``).
_CLAIM_TRIES = 120

#: Legacy grammars whose parameters index the manifold dims (web/js/core/legacyhash.js).
_DIM_GRAMMARS = frozenset({"gc-query", "kv-view"})


def _manifest_dims(man: Any, spec: Any) -> list[str] | None:
    """The dim keys a manifold pane option ``spec`` ("group:<g>" or absent) stands for in the
    catalog manifest ``man``; None when the manifest does not say."""
    if not isinstance(man, Mapping):
        return None
    if isinstance(spec, str) and spec.startswith("group:"):
        for g in man.get("groups") or []:
            if isinstance(g, Mapping) and g.get("key") == spec[6:] and isinstance(g.get("dims"), list):
                return [str(k) for k in g["dims"]]
        return None
    if spec is None:
        mf = man.get("manifold")
        if isinstance(mf, Mapping) and isinstance(mf.get("dims"), list) and mf["dims"]:
            return [str(k) for k in mf["dims"]]
    return None


def resolve_manifold_dims(layout: Any, manifests: Mapping[str, Any],
                          default_ds: str | None = None) -> Any:
    """Copy of ``layout`` whose manifold panes carry an explicit ``options.dims`` key list.

    ``"group:<g>"`` and an absent option (the manifest's ``manifold.dims``) are replaced by
    the keys they resolve to, in manifest order, which is what ``panes/manifold.js``
    ``resolveDims`` picks. ``legacyhash.js`` reads that list to map dim-indexed legacy query
    parameters (gc-query ``a/b/m``, ``x1..y2``, ``cb``, ``cd``, ``y``; kv-view ``x/y``)."""
    def walk(node: Any) -> Any:
        if isinstance(node, Mapping):
            if isinstance(node.get("row"), list) or isinstance(node.get("col"), list):
                k = "row" if isinstance(node.get("row"), list) else "col"
                return {**node, k: [walk(c) for c in node[k]]}
            if node.get("pane") == "manifold":
                opts = dict(node.get("options") or {})
                if not isinstance(opts.get("dims"), list):
                    keys = _manifest_dims(manifests.get(node.get("dataset") or default_ds),
                                          opts.get("dims"))
                    if keys:
                        opts["dims"] = keys
                        return {**node, "options": opts}
            return dict(node)
        return node
    return walk(layout)


def _merge_legacy(page: PageSpec, portal: Any) -> PageSpec:
    """Fill ``page.legacy`` from the ``[[portal.page]]`` entry of the site registry."""
    pc = portal.page(page.id) if portal is not None else None
    if pc is None:
        return page
    if page.legacy is None:
        if not (pc.legacy or pc.grammar or pc.preset):
            return page
        leg = LegacyRef(files=list(pc.legacy), grammar=list(pc.grammar), preset=pc.preset)
    else:
        leg = LegacyRef(files=list(page.legacy.files) or list(pc.legacy),
                        grammar=list(page.legacy.grammar) or list(pc.grammar),
                        preset=page.legacy.preset or pc.preset)
    return dataclasses.replace(page, legacy=leg)


def _copy_web(dest: Path, ledger: Ledger) -> int:
    """Copy ``web/`` into ``dest`` (``--vendor-assets``); returns the number of files."""
    n = 0
    w = BundleWriter(dest.parent, dest.name, ledger)
    for dirpath, dirnames, filenames in os.walk(WEB_DIR, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))
                             and not d.startswith("."))
        rel_dir = os.path.relpath(dirpath, WEB_DIR)
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            if fn.startswith(".") or os.path.islink(full):
                continue
            rel = fn if rel_dir == "." else f"{rel_dir.replace(os.sep, '/')}/{fn}"
            with open(full, "rb") as fh:
                w.put_stable(rel, fh.read(), kind="assets")
            n += 1
    return n


def shared_resources(dest: BundleWriter) -> dict[str, str]:
    """Write templates, filters, extinction and features into ``dest`` and return the
    ``res_refs`` (``{kind: "{res}<path>"}``)."""
    from .physics import write_shared
    paths = write_shared(dest, ledger=dest.ledger)
    return {k: "{res}" + p.lstrip("/") for k, p in paths.items()}


def _write_report(path: Path, report: Mapping) -> None:
    data = json.dumps(report, indent=1, ensure_ascii=False, allow_nan=False,
                      default=str).encode("utf-8")
    atomic_write(path, data)


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

class _PortalBuild:
    """State of one portal's build (datasets, refs, ledger)."""

    def __init__(self, fb: "_FamilyBuild", portal: Any, pages: list[PageSpec]) -> None:
        self.fb = fb
        self.portal = portal
        self.pages = pages
        self.pid = portal.id
        self.dir = create_portal_stage_dir(fb.root, fb.family, fb.build_id, self.pid, fb.emap)
        self.ledger = Ledger()
        self.w = BundleWriter(self.dir, "", self.ledger)
        self.manifests: dict[str, Any] = {}
        self.refs: dict[str, dict] = {}
        self.cat_results: dict[str, Any] = {}
        self.res_refs: dict[str, str] | None = None
        self.ds_report: dict[str, dict] = {}
        self.page_reports: list[dict] = []
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.built: list[PageSpec] = []
        self.stable_used: set[str] = set()
        # datasets this portal's pages use (IMPLEMENTATION_NOTES item 21: resolution is
        # portal-scoped, so a landing page does not pick up another portal's catalogs)
        self.ds: dict[str, Any] = dict(fb.ds)

    # -- shared resources ----------------------------------------------
    def shared_res(self) -> dict[str, str]:
        if self.res_refs is None:
            if self.fb.vendor_assets:
                self.res_refs = shared_resources(self.w.sub("res"))
            else:
                self.res_refs = self.fb.shared_res()
        return self.res_refs

    # -- datasets ------------------------------------------------------
    def write_datasets(self) -> None:
        fb = self.fb
        ds = fb.ds
        need = _needed_datasets(self.pages, ds)
        self.ds = {k: ds[k] for k in need}
        recs = [k for k in need if isinstance(ds[k], RecordSetSpec)]
        imgs = [k for k in need if isinstance(ds[k], ImageSetSpec)]
        prov = fb.provenance()
        for k in need:
            spec = ds[k]
            if not isinstance(spec, CatalogSpec):
                continue
            links: dict[str, Any] = {}
            rl = {r: f"../{r}/index.json" for r in recs if ds[r].catalog == k}
            if rl:
                links["records"] = rl
            il = [i for i in imgs if ds[i].catalog == k]
            if il:
                links["images"] = f"../{il[0]}/images.json"
            dw = self.w.sub(f"data/{k}", k)
            fb.log(f"[{self.pid}] catalog {k}")
            if spec.foreign is not None:
                res = gc_manifest.write(spec, dw, ds=k, build=fb.build_id, links=links or None,
                                        provenance=prov, ledger=self.ledger, log=fb.log)
                ref: dict[str, Any] = {"kind": "catalog", "url": f"data/{k}/manifest.json"}
                if spec.context:
                    ref["context"] = True
                self.refs[k] = ref
            else:
                res = catalog_writer.write(spec, dw, build=fb.build_id, seed=fb.ctx.seed,
                                           links=links or None, provenance=prov,
                                           ledger=self.ledger, log=fb.log)
            self.cat_results[k] = res
            self.manifests[k] = res.manifest
            self.ds_report[k] = {"kind": "catalog", "files": len(res.files),
                                 "n": res.manifest.get("n"), "warnings": list(res.warnings),
                                 "foreign": spec.foreign is not None}

        # records and images: independent writers (thread pool with jobs > 1)
        jobs: list[tuple[str, Callable[[Ledger], tuple[Any, Any, dict]]]] = []
        for k in recs + imgs:
            spec = ds[k]
            cat = self.cat_results.get(spec.catalog) if spec.catalog else None
            row_of = cat.row_of if cat is not None and cat.id_to_row else None
            if isinstance(spec, RecordSetSpec):
                def job(led: Ledger, k=k, spec=spec, row_of=row_of):
                    rr = records_writer.write(spec, BundleWriter(self.dir, f"data/{k}", led, k),
                                              order=row_of, tier=fb.tier, ledger=led, log=fb.log)
                    return rr.index, rr, {"kind": "records", "record": spec.kind,
                                          "files": len(rr.files), "skipped": len(rr.skipped),
                                          "warnings": list(rr.warnings),
                                          "json_legacy": bool(spec.legacy_json)}
            else:
                def job(led: Ledger, k=k, spec=spec, row_of=row_of):
                    iw = BundleWriter(self.dir, f"data/{k}", led, k)
                    hook = _thumb_hook(thumb_cache_dir(fb.family), iw, fb.thumb_px)
                    ir = images_writer.write(spec, iw, rows=row_of, thumb=hook, ledger=led,
                                             log=fb.log)
                    return ir.doc, ir, {"kind": "images", "items": len(spec.items),
                                        "pages": len(ir.pages), "unmatched": len(ir.unmatched),
                                        "warnings": list(ir.warnings)}
            jobs.append((k, job))
        results = fb.map_jobs(jobs)
        for (k, _), (doc, _res, rep, led) in zip(jobs, results):
            for e in led.entries:
                self.ledger.add(e.path, e.bytes, e.kind, e.dataset, e.first_paint)
            for wmsg in led.warnings:
                self.ledger.warn(wmsg)
            self.manifests[k] = doc
            self.ds_report[k] = rep

        # small documents and physics datasets
        for k in need:
            spec = ds[k]
            if isinstance(spec, (HipsSpec, OverlaySpec, NarrativeSpec)):
                kind = {HipsSpec: "hips", OverlaySpec: "overlays", NarrativeSpec: "narrative"}[type(spec)]
                if isinstance(spec, HipsSpec):
                    cats = [c for c in need if isinstance(ds[c], CatalogSpec) and not ds[c].context]
                    doc = configmod.hips_document(spec, default_target=_default_target(self.manifests, cats))
                elif isinstance(spec, OverlaySpec):
                    doc = configmod.overlays_document(spec)
                else:
                    doc = configmod.narrative_document(spec)
                rel = f"data/{kind}.json"
                if rel in self.stable_used:
                    rel = f"data/{k}/{kind}.json"
                    self.refs[k] = {"kind": kind, "url": rel}
                self.stable_used.add(rel)
                self.w.put_stable_json(rel, doc, kind="json", first_paint=True)
                self.manifests[k] = doc
                self.ds_report[k] = {"kind": kind, "file": rel}
            elif isinstance(spec, IsochroneSpec):
                name, data = configmod.isochrones_file(spec)
                self.w.put_stable(f"data/{name}", data, kind="json")
                self.manifests[k] = json.loads(data)
                self.ds_report[k] = {"kind": "isochrones", "file": f"data/{name}",
                                     "tracks": len(spec.tracks)}
            elif _res_kind(spec) is not None:
                kind = _res_kind(spec)
                if _is_default_res(spec):
                    self.ds_report[k] = {"kind": kind, "shared": True}
                    self.shared_res()
                else:
                    from .physics import write_dataset
                    pw = self.w.sub(f"data/res/{k}", k)
                    pr = write_dataset(spec, pw, ledger=self.ledger)
                    self.refs[k] = {"kind": kind, "url": f"data/res/{k}/{pr.path}"}
                    self.manifests[k] = pr.doc
                    self.ds_report[k] = {"kind": kind, "shared": False, "files": len(pr.files)}
            elif isinstance(spec, ReadmeSpec):
                self.ds_report[k] = {"kind": "readme"}

    # -- pages -----------------------------------------------------------
    def _page_refs(self, page: PageSpec, enabled: Iterable[str]) -> dict[str, dict]:
        """Global refs plus refs to the shared resources the page's capabilities use
        (:func:`portalkit.caps.attachable_resources`, the rule the synthetic fixture shares;
        IMPLEMENTATION_NOTES 110)."""
        refs = dict(self.refs)
        kinds = capsmod.attachable_resources(page, self.ds, list(enabled), self.manifests)
        if kinds:
            refs.update(capsmod.resource_refs(kinds, self.shared_res(),
                                              taken=set(self.fb.ds) | set(refs)))
        return refs

    def build_page(self, page: PageSpec) -> tuple[PageSpec, dict, dict[str, bytes] | None, list[str]]:
        fb = self.fb
        ds = self.ds
        try:
            cres = capsmod.resolve(page, ds, self.manifests)
        except (ValueError, KeyError, TypeError) as exc:
            return page, {"id": page.id, "error": str(exc)}, None, [f"page {page.id}: {exc}"]
        if page.legacy is not None and _DIM_GRAMMARS.intersection(page.legacy.grammar or ()):
            base = cres.layout if cres.layout is not None else page.layout
            if base is not None:
                prim = next((k for k in page.datasets if isinstance(ds.get(k), CatalogSpec)), None)
                cres = dataclasses.replace(cres, layout=resolve_manifold_dims(base, self.manifests, prim))
        refs = self._page_refs(page, cres.enabled)
        res_refs = None
        if any(_res_kind(ds[k]) and _is_default_res(ds[k]) for k in page.datasets if k in ds):
            res_refs = self.shared_res()
        elif self.res_refs is not None:
            res_refs = self.res_refs
        pd = capsmod.page_data(page, ds, self.manifests)
        fp = sum(self.ledger.first_paint(k) for k in pd.ids if isinstance(ds[k], CatalogSpec))
        files: dict[str, bytes] = {}
        aladin = {"mirror": self.portal.aladin_url} if self.portal.aladin_url else None
        portal = {"id": self.pid, "title": self.portal.title, "visibility": self.portal.visibility,
                  "nav": "portal.json"}
        try:
            doc = configmod.assemble(page, ds, self.manifests, cres, portal=portal, refs=refs,
                                     res_refs=res_refs, pk=VERSION,
                                     vendor_assets=fb.vendor_assets, aladin=aladin,
                                     budget={"first_paint_bytes": fp}, files=files)
        except configmod.ConfigError as exc:
            errs = list(getattr(exc, "errors", [str(exc)]))
            return page, {"id": page.id, "error": "config"}, None, [f"page {page.id}: {e}" for e in errs]
        except ValueError as exc:
            return page, {"id": page.id, "error": str(exc)}, None, [f"page {page.id}: {exc}"]
        try:
            nbytes = shell.write(self.dir / f"{page.id}.html", doc)
        except shell.ShellError as exc:
            return page, {"id": page.id, "error": "shell"}, None, [f"page {page.id}: {exc}"]
        rep = {"id": page.id, "archetype": page.archetype, "file": f"{page.id}.html",
               "html_bytes": nbytes, "page_json_bytes": shell.page_json_bytes(doc),
               "first_paint_bytes": fp, "datasets": sorted(doc["datasets"]),
               "caps": {"enabled": list(cres.enabled), "disabled": dict(cres.disabled)},
               "flags": list(cres.flags), "added_panes": [p.get("pane") for p in cres.added_panes]}
        return page, rep, files, []

    def build_pages(self) -> None:
        results = self.fb.map_pages(self.build_page, self.pages)
        for page, rep, files, errs in results:
            self.page_reports.append(rep)
            if errs:
                self.errors.extend(errs)
                continue
            for name, data in (files or {}).items():
                self.w.put_stable(name, data, kind="other")
            self.built.append(page)

    # -- portal.json ------------------------------------------------------
    def portal_doc(self) -> dict:
        fb = self.fb
        doc: dict[str, Any] = {"schema": "pk-portal/1", "id": self.pid, "title": self.portal.title,
                               "visibility": self.portal.visibility, "build": fb.build_id,
                               "pk": VERSION, "pages": []}
        for p in self.built:
            ent = {"id": p.id, "title": p.title, "file": f"{p.id}.html", "archetype": p.archetype}
            if p.subtitle:
                ent["subtitle"] = p.subtitle
            doc["pages"].append(ent)
        related = [{"title": o.title, "url": o.url, "auth": o.visibility}
                   for o in fb.site.portals if o.id != self.pid]
        if related:
            doc["related"] = related
        search = []
        seen: set[str] = set()
        for p in self.built:
            if p.archetype not in ("explorer", "manifold"):
                continue
            for k in p.datasets:
                s = fb.ds.get(k)
                if isinstance(s, CatalogSpec) and not s.context and k not in seen and k in self.manifests:
                    seen.add(k)
                    search.append({"ds": k, "page": p.id, "manifest": f"data/{k}/manifest.json"})
        if search:
            doc["search"] = search
        return doc

    def finish(self) -> dict:
        fb = self.fb
        pdoc = self.portal_doc()
        errs = validate_portal(pdoc)
        self.errors.extend(f"portal.json: {e}" for e in errs)
        self.w.put_stable_json("portal.json", pdoc, kind="json")
        led = self.ledger.to_dict()
        self.errors.extend(led.get("errors", []))
        report = {
            "schema": "pk-build-report/1",
            "build_id": fb.build_id, "family": fb.family, "portal": self.pid,
            "tier": fb.tier, "limit": fb.limit, "built": _iso(fb.now),
            "adapter": fb.adapter_ref, "adapter_sha": fb.adapter_sha, "adapter_path": fb.adapter_path,
            "legacy_modules": fb.ctx.legacy.shas(), "portalkit": VERSION,
            "allow_tmp_inputs": fb.allow_tmp_inputs,
            "inputs": [{"path": r["path"], "sha256": r["sha256"], "mtime": r["mtime"],
                        "size": r["size"]} for r in fb.ctx.inputs.records()],
            "tmp_inputs": list(fb.ctx.inputs.tmp),
            "unmatched_inputs": list(fb.unmatched),
            "vendor_assets": fb.vendor_assets,
            "res": dict(self.res_refs or {}),
            "pages": self.page_reports,
            "datasets": self.ds_report,
            "budgets": led,
            "cache": {"enabled": fb.ctx.cache.enabled, "hits": fb.ctx.cache.hits,
                      "misses": fb.ctx.cache.misses},
            "warnings": list(fb.warnings) + list(self.warnings) + list(self.ledger.warnings),
            "errors": list(self.errors),
        }
        _write_report(self.dir / BUILD_REPORT, report)
        if fb.run_check:
            chk = check_build(self.dir, gate=None)
            report["check"] = chk.to_json()
            self.errors.extend(chk.errors)
            report["errors"] = list(self.errors)
            _write_report(self.dir / BUILD_REPORT, report)
        report["ok"] = not self.errors
        _write_report(self.dir / BUILD_REPORT, report)
        return report


class _FamilyBuild:
    def __init__(self, family: str, *, tier: str | None, pages: list[str] | None,
                 limit: int | None, stage_root: os.PathLike | str | None, allow_tmp_inputs: bool,
                 vendor_assets: bool, jobs: int, sites_dir: os.PathLike | str | None,
                 emap: ExposureMap | None, no_cache: bool, now: _dt.datetime | None,
                 log: Callable[[str], None] | None, run_check: bool, thumb_px: int) -> None:
        self.family = family
        self.page_filter = list(pages) if pages else None
        self.limit = limit
        self.allow_tmp_inputs = bool(allow_tmp_inputs)
        self.vendor_assets = bool(vendor_assets)
        self.jobs = max(1, int(jobs or 1))
        self.no_cache = bool(no_cache)
        self.now = now or _dt.datetime.now(_dt.timezone.utc)
        self.log = log or print
        self.run_check = run_check
        self.thumb_px = int(thumb_px)
        self.warnings: list[str] = []
        self.site: SiteConfig = load_site(family, sites_dir)
        if self.site.status == "excluded":
            raise UsageError(f"family {family!r} is excluded (status = \"excluded\")")
        if not self.site.adapter:
            raise UsageError(f"family {family!r} has no adapter")
        self.tier = tier or self.site.tier_default
        if self.tier not in ("A", "B", "C"):
            raise UsageError(f"tier must be A, B or C, got {self.tier!r}")
        self.root = default_stage_root(stage_root)
        self.emap = emap if emap is not None else scan()
        check_stage_root(self.root, self.emap)
        self.adapter_ref = self.site.adapter
        self.adapter, self.adapter_sha, self.adapter_path = load_adapter(self.site.adapter)
        tiers = tuple(getattr(self.adapter, "tiers", ()) or ())
        if tiers and self.tier not in tiers:
            raise UsageError(f"adapter {self.adapter_ref} implements tiers {list(tiers)}, "
                             f"not {self.tier!r}")
        # inputs (exit 5 on tmp paths)
        reg = InputRegistry(self.allow_tmp_inputs, cache_dir=cache_base() / family)
        paths, self.unmatched = expand_inputs(self.site.inputs)
        for pat in self.unmatched:
            reg.check(pat)
            self.warnings.append(f"input {pat} matches nothing")
        reg.add_many(paths)
        self.build_id, self.staging = self._claim_staging(reg.records())
        try:
            cache = Cache(cache_base() / family, enabled=not self.no_cache, registry=reg, log=None)
            self.ctx = BuildContext(family=family, site=self.site, staging=self.staging,
                                    tier=self.tier, limit=limit,
                                    allow_tmp_inputs=self.allow_tmp_inputs, cache=cache, inputs=reg,
                                    legacy=LegacyImporter(self.site.legacy_paths, log=self.log),
                                    log=self.log)
        except BaseException:
            self._drop_empty_staging()
            raise
        self._res: dict[str, str] | None = None
        self._lock = threading.Lock()

    # -- helpers ---------------------------------------------------------
    def _claim_staging(self, records: list[dict]) -> tuple[str, Path]:
        """Pick the build id and create its staging dir.

        The id hashes the adapter sha, the input records and the build options (tier, limit,
        page filter, vendoring), so builds of one family with different options never share
        an id. The staging dir is created with ``exist_ok=False``; when it exists (a second
        build in the same second), the timestamp moves on by one second until a free id is
        found, so two builds never write into one tree."""
        opts = {"build_options": {"tier": self.tier, "limit": self.limit,
                                  "pages": sorted(self.page_filter) if self.page_filter else None,
                                  "vendor_assets": self.vendor_assets,
                                  "allow_tmp_inputs": self.allow_tmp_inputs}}
        inputs = list(records) + [opts]
        for i in range(_CLAIM_TRIES):
            bid = make_build_id(self.adapter_sha, inputs, self.now + _dt.timedelta(seconds=i))
            d = build_dir(self.root, self.family, bid)
            d.parent.mkdir(parents=True, exist_ok=True)
            try:
                d.mkdir(exist_ok=False)
            except FileExistsError:
                continue
            if i:
                self.log(f"staging {bid}: the first-choice build id was taken, moved on {i} s")
            return bid, d
        raise BuildError(f"no free build id for {self.family} within {_CLAIM_TRIES} s of "
                         f"{_iso(self.now)} under {self.root}")

    def _drop_empty_staging(self) -> None:
        """Remove the claimed staging dir when the build stopped before writing into it."""
        try:
            next(self.staging.iterdir())
        except StopIteration:
            try:
                self.staging.rmdir()
            except OSError as exc:
                self.log(f"could not remove empty staging dir {self.staging}: {exc}")
        except FileNotFoundError:
            pass

    def provenance(self) -> dict:
        recs = self.ctx.inputs.records()
        prov: dict[str, Any] = {
            "inputs": [{"path": r["path"], "sha256": r["sha256"], "mtime": r["mtime"], "size": r["size"]}
                       for r in recs[:_PROV_INPUTS_MAX]],
            "legacy_modules": self.ctx.legacy.shas(),
            "adapter": f"{getattr(self.adapter, 'name', self.family)}@{self.adapter_sha[:12]}",
            "portalkit": VERSION, "tier": self.tier, "built": _iso(self.now),
        }
        if len(recs) > _PROV_INPUTS_MAX:
            prov["n_inputs"] = len(recs)
        return prov

    def shared_res(self) -> dict[str, str]:
        with self._lock:
            if self._res is None:
                self.staging.mkdir(parents=True, exist_ok=True)
                self.log(f"shared resources -> {self.staging / RES_DIRNAME}")
                self._res = shared_resources(BundleWriter(self.staging, RES_DIRNAME, Ledger()))
            return self._res

    def map_jobs(self, jobs: list[tuple[str, Callable[[Ledger], tuple]]]) -> list[tuple]:
        def run(item):
            led = Ledger()
            doc, res, rep = item[1](led)
            return doc, res, rep, led
        if self.jobs > 1 and len(jobs) > 1:
            with ThreadPoolExecutor(self.jobs) as ex:
                return list(ex.map(run, jobs))
        return [run(j) for j in jobs]

    def map_pages(self, fn: Callable, pages: list[PageSpec]) -> list:
        if self.jobs > 1 and len(pages) > 1:
            with ThreadPoolExecutor(self.jobs) as ex:
                return list(ex.map(fn, pages))
        return [fn(p) for p in pages]

    # -- pipeline ----------------------------------------------------------
    def run(self) -> BuildResult:
        try:
            return self._run()
        finally:
            self._drop_empty_staging()

    def _run(self) -> BuildResult:
        log = self.log
        log(f"build {self.family} tier {self.tier} -> {self.staging}")
        try:
            self.ds = self.adapter.datasets(self.ctx)
            pages = list(self.adapter.pages(self.ctx, self.ds))
        except TierUnavailable as exc:
            raise UsageError(f"adapter {self.adapter_ref} cannot build tier {self.tier}: {exc}") from exc
        if not isinstance(self.ds, Mapping):
            raise BuildError(f"adapter {self.adapter_ref}: datasets() must return a dict")
        for k, s in self.ds.items():
            key = getattr(s, "key", None)
            if key is not None and key != k:
                raise BuildError(f"dataset {k!r} has key {key!r}; dict keys and spec keys must agree")
        by_portal: dict[str, list[PageSpec]] = {}
        seen: dict[str, set] = {}
        for p in pages:
            if not isinstance(p, PageSpec):
                raise BuildError(f"adapter {self.adapter_ref}: pages() returned {type(p).__name__}")
            if not _PAGE_ID_RE.match(p.id or ""):
                raise BuildError(f"bad page id {p.id!r}")
            portal = self.site.portal(p.portal) if p.portal else self.site.portal()
            if p.id in seen.setdefault(portal.id, set()):
                raise BuildError(f"duplicate page id {p.id!r} in portal {portal.id!r}")
            seen[portal.id].add(p.id)
            by_portal.setdefault(portal.id, []).append(_merge_legacy(p, portal))
        if self.page_filter is not None:
            known = {p.id for pl in by_portal.values() for p in pl}
            unknown = [x for x in self.page_filter if x not in known]
            if unknown:
                raise UsageError(f"unknown page(s) {unknown}", details=sorted(known))
            by_portal = {pid: [p for p in pl if p.id in self.page_filter]
                         for pid, pl in by_portal.items()}
            by_portal = {pid: pl for pid, pl in by_portal.items() if pl}
        if not by_portal:
            raise BuildError(f"adapter {self.adapter_ref} returned no pages")
        reports: dict[str, dict] = {}
        built: list[str] = []
        ok = True
        for pid, plist in by_portal.items():
            pb = _PortalBuild(self, self.site.portal(pid), plist)
            if self.vendor_assets:
                n = _copy_web(pb.dir / "_pk" / f"v{VERSION}", Ledger())
                log(f"[{pid}] vendored {n} asset file(s)")
            try:
                pb.write_datasets()
            except (BundleError, configmod.ConfigError, ValueError, KeyError, TypeError) as exc:
                pb.errors.append(f"datasets: {exc}")
            if not any(e.startswith("datasets: ") for e in pb.errors):
                pb.build_pages()
            rep = pb.finish()
            reports[pid] = rep
            built += [f"{pid}/{p.id}.html" for p in pb.built]
            ok = ok and rep["ok"]
            for e in rep["errors"]:
                log(f"[{pid}] error: {e}")
            log(f"[{pid}] {len(pb.built)}/{len(plist)} page(s) built"
                f"{'' if rep['ok'] else ' (FAILED)'}")
        report = {"build_id": self.build_id, "family": self.family, "tier": self.tier,
                  "staging": str(self.staging), "portals": reports, "ok": ok}
        return BuildResult(build_id=self.build_id, staging=self.staging, pages=built,
                           report=report, ok=ok)


def build_family(family: str, *, tier: str | None = None, pages: list[str] | None = None,
                 limit: int | None = None, stage_root: os.PathLike | str | None = None,
                 allow_tmp_inputs: bool = False, vendor_assets: bool = False, jobs: int = 1,
                 sites_dir: os.PathLike | str | None = None, emap: ExposureMap | None = None,
                 no_cache: bool = False, now: _dt.datetime | None = None,
                 log: Callable[[str], None] | None = None, run_check: bool = True,
                 thumb_px: int = 480) -> BuildResult:
    """Build every page of ``family`` (section 9.4) into the staging tree.

    Extra keywords beyond section 9.4: ``sites_dir`` (site registry directory), ``emap``
    (exposure map for the stage-root check; default :func:`portalkit.deploy.scan`),
    ``no_cache`` (``--no-cache``), ``now`` (build time), ``log``, ``run_check`` (step 7) and
    ``thumb_px`` (size of cached thumbnails to pick up).

    Raises :class:`~portalkit.deploy.UsageError` (exit 2), :class:`ExposureRefusal` (served
    stage root, exit 3), :class:`BlockedInput` (tmp input, exit 5),
    :class:`~portalkit.legacy.importshim.LegacyImportError` (exit 6). Page and dataset
    failures are collected: the result has ``ok=False`` and the report lists them.
    """
    fb = _FamilyBuild(family, tier=tier, pages=pages, limit=limit, stage_root=stage_root,
                      allow_tmp_inputs=allow_tmp_inputs, vendor_assets=vendor_assets, jobs=jobs,
                      sites_dir=sites_dir, emap=emap, no_cache=no_cache, now=now, log=log,
                      run_check=run_check, thumb_px=thumb_px)
    return fb.run()


def make_thumbs(family: str, *, max_px: int = 480, sites_dir: os.PathLike | str | None = None,
                tier: str | None = None, log: Callable[[str], None] | None = None) -> dict:
    """``portalkit thumbs FAMILY``: run the adapter's ``datasets()`` and write a cached PNG
    thumbnail for every image-set file that maps to a local path (served URL or absolute
    path) and is larger than ``max_px``. Builds copy them into ``data/<set>/thumbs/``.
    Returns counts."""
    log = log or print
    site = load_site(family, sites_dir)
    adapter, _, _ = load_adapter(site.adapter or "")
    t = tier or site.tier_default
    ctx = BuildContext(family=family, site=site, staging=cache_base() / family / "thumbs_ctx",
                       tier=t, log=log)
    try:
        ds = adapter.datasets(ctx)
    except TierUnavailable as exc:
        raise UsageError(f"adapter {site.adapter} cannot run tier {t}: {exc}") from exc
    cdir = thumb_cache_dir(family)
    out = {"made": 0, "cached": 0, "small": 0, "remote": 0}
    for k, spec in ds.items():
        if not isinstance(spec, ImageSetSpec):
            continue
        for it in spec.items:
            for url in it.roles.values():
                src = local_path_for_url(url)
                if src is None:
                    out["remote"] += 1
                    continue
                dest = thumb_cache_path(cdir, src, max_px)
                if dest.is_file():
                    out["cached"] += 1
                elif make_thumbnail(src, dest, max_px):
                    out["made"] += 1
                else:
                    out["small"] += 1
        log(f"[{k}] thumbnails: {out}")
    return out


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------

@dataclass
class CheckReport:
    path: str
    gate: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    portals: dict[str, dict] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1

    def to_json(self) -> dict:
        return {"path": self.path, "gate": self.gate, "ok": self.ok, "errors": list(self.errors),
                "warnings": list(self.warnings), "portals": self.portals}

    def format(self) -> str:
        lines = [f"check {self.path}" + (f" (gate {self.gate})" if self.gate else "")]
        for pid, st in self.portals.items():
            lines.append(f"  portal {pid}: {st.get('pages', 0)} page(s), "
                         f"{st.get('datasets', 0)} dataset document(s)")
        for e in self.errors:
            lines.append(f"  ERROR  {e}")
        for w in self.warnings:
            lines.append(f"  warn   {w}")
        lines.append("  OK" if self.ok else f"  FAILED ({len(self.errors)} error(s))")
        return "\n".join(lines)


def _is_local_ref(u: Any) -> bool:
    if not isinstance(u, str) or not u:
        return False
    if u.startswith(("{", "/", "#", "data:", "mailto:")):
        return False
    return "://" not in u.split("?", 1)[0]


def _strip(u: str) -> str:
    return urllib.parse.unquote(u.split("#", 1)[0].split("?", 1)[0])


def _load_json(path: Path) -> Any:
    with open(path, "rb") as fh:
        return json.loads(fh.read().decode("utf-8"))


def _doc_file_refs(doc: Any) -> list[str]:
    """Relative file references of a manifest / images document."""
    out: list[str] = []

    def add(v: Any) -> None:
        if isinstance(v, str):
            if _is_local_ref(v):
                out.append(v)
        elif isinstance(v, list):
            for x in v:
                add(x)
        elif isinstance(v, Mapping):
            for x in v.values():
                add(x)

    def walk(v: Any, parent: str | None = None) -> None:
        if isinstance(v, Mapping):
            for k, x in v.items():
                if k in ("file", "files", "chunk_files", "thumb", "pages", "png"):
                    add(x)
                elif k == "url" and parent in ("roles",):
                    add(x)
                elif k in ("chunks", "alias_file") and parent == "ids":
                    add(x)
                elif k in ("links", "provenance", "caveats"):
                    continue
                else:
                    walk(x, k)
        elif isinstance(v, list):
            for x in v:
                walk(x, parent)
    walk(doc)
    return out


def _check_files(base: Path, rels: Iterable[str], where: str, errors: list[str],
                 limit: int | None = None, label: str = "file") -> int:
    n = 0
    for rel in dict.fromkeys(rels):
        p = (base / _strip(rel))
        try:
            st = os.stat(p)
        except OSError:
            errors.append(f"{where}: missing {label} {rel}")
            continue
        n += 1
        if limit is not None and st.st_size > limit:
            errors.append(f"{where}: {rel} is {st.st_size} B, above the {limit} B budget")
    return n


def _check_portal(pdir: Path, gate: str | None, rep: CheckReport) -> None:
    pid = pdir.name
    errors: list[str] = []
    warnings: list[str] = []
    stats = {"pages": 0, "datasets": 0}
    try:
        portal = _load_json(pdir / "portal.json")
    except (OSError, ValueError) as exc:
        rep.errors.append(f"{pid}: cannot read portal.json: {exc}")
        rep.portals[pid] = stats
        return
    errors += [f"portal.json: {e}" for e in validate_portal(portal)]
    try:
        breport = _load_json(pdir / BUILD_REPORT)
    except (OSError, ValueError):
        breport = {}
        warnings.append(f"no readable {BUILD_REPORT}")
    checked: dict[str, str] = {}      # resolved dataset doc path -> kind
    fp_by_ds: dict[str, int] = {}
    led = (breport.get("budgets") or {}) if isinstance(breport, Mapping) else {}
    for d, st in (led.get("datasets") or {}).items():
        if isinstance(st, Mapping):
            fp_by_ds[d] = int(st.get("first_paint_bytes", 0) or 0)
    for e in led.get("errors") or []:
        errors.append(f"budget: {e}")
    res_dirs = [pdir / "res", pdir.parent / RES_DIRNAME]

    for pg in portal.get("pages") or []:
        if not isinstance(pg, Mapping) or not isinstance(pg.get("file"), str):
            continue
        f = pdir / pg["file"]
        where = f"page {pg.get('id')}"
        try:
            size = f.stat().st_size
        except OSError:
            errors.append(f"{where}: missing {pg['file']}")
            continue
        stats["pages"] += 1
        if size > PAGE_HTML_MAX:
            errors.append(f"{where}: HTML is {size} B, above {PAGE_HTML_MAX} B")
        try:
            page = script_json(f, "pk-page")
        except LegacyExtractError as exc:
            errors.append(f"{where}: no embedded pk-page JSON ({exc})")
            continue
        jbytes = shell.page_json_bytes(page)
        if jbytes > PAGE_JSON_MAX:
            errors.append(f"{where}: page.json is {jbytes} B, above {PAGE_JSON_MAX} B")
        errors += [f"{where}: {e}" for e in validate_page(page)]
        pinfo = page.get("page") or {}
        if pinfo.get("id") != pg.get("id"):
            errors.append(f"{where}: embedded page id {pinfo.get('id')!r} differs from portal.json")
        if pinfo.get("archetype") != pg.get("archetype"):
            errors.append(f"{where}: archetype differs from portal.json")
        assets = page.get("assets") or ""
        if isinstance(assets, str) and assets.startswith("./"):
            boot = pdir / _strip(assets) / "js" / "core" / "boot.js"
            if not boot.is_file():
                errors.append(f"{where}: vendored assets missing ({boot.relative_to(pdir)})")
        readme = page.get("readme") or {}
        if isinstance(readme, Mapping) and _is_local_ref(readme.get("url")):
            _check_files(pdir, [readme["url"]], where, errors, label="readme")
        for dsid, ref in (page.get("datasets") or {}).items():
            if not isinstance(ref, Mapping):
                continue
            url = ref.get("url")
            kind = ref.get("kind")
            if isinstance(url, str) and url.startswith("{res}"):
                rel = _strip(url[len("{res}"):])
                if not any((d / rel).is_file() for d in res_dirs):
                    msg = f"{where}: shared resource {url} not found under res/ or ../{RES_DIRNAME}/"
                    (errors if str(page.get("res", "")).startswith("./") else warnings).append(msg)
                continue
            if not _is_local_ref(url):
                continue
            p = pdir / _strip(url)
            if not p.is_file():
                errors.append(f"{where}: dataset {dsid} ({kind}) url {url} does not exist")
                continue
            key = str(p)
            if key in checked:
                continue
            checked[key] = kind
            stats["datasets"] += 1
            _check_doc(p, kind, dsid, gate, fp_by_ds, errors, warnings)
    rep.errors += [f"{pid}: {e}" for e in errors]
    rep.warnings += [f"{pid}: {w}" for w in warnings]
    rep.portals[pid] = stats


def _check_doc(p: Path, kind: str | None, dsid: str, gate: str | None, fp_by_ds: Mapping[str, int],
               errors: list[str], warnings: list[str]) -> None:
    where = f"dataset {dsid}"
    try:
        doc = _load_json(p)
    except (OSError, ValueError) as exc:
        errors.append(f"{where}: cannot read {p.name}: {exc}")
        return
    base = p.parent
    if kind == "catalog":
        size = p.stat().st_size
        if size > MANIFEST_MAX:
            errors.append(f"{where}: manifest is {size} B, above {MANIFEST_MAX} B")
        errors += [f"{where}: {e}" for e in validate_catalog(doc)]
        _check_files(base, _doc_file_refs(doc), where, errors, limit=COL_FILE_MAX)
        n = int(doc.get("n") or 0)
        fp = fp_by_ds.get(dsid)
        if fp is not None:
            lim = FIRST_PAINT_MAX_1M if n <= 1_000_000 else FIRST_PAINT_MAX_5M
            if fp > lim:
                errors.append(f"{where}: first-paint bytes {fp} above {lim} B (N={n})")
            if n > 5_000_000:
                warnings.append(f"{where}: N={n} is above the 5M first-paint budget table")
        lk = doc.get("links") or {}
        for rel in list((lk.get("records") or {}).values()) + ([lk["images"]] if lk.get("images") else []):
            if _is_local_ref(rel) and not (base / _strip(rel)).is_file():
                warnings.append(f"{where}: link {rel} is not staged")
    elif kind == "records":
        errors += [f"{where}: {e}" for e in validate_records(doc)]
        if doc.get("kind") == "json-legacy":
            msg = f"{where}: json-legacy record set"
            if gate == "G2":
                errors.append(msg + " (gate G2 requires native records)")
            else:
                warnings.append(msg)
            return
        shards = [s for s in doc.get("shards") or [] if isinstance(s, str)]
        _check_files(base, shards, where, errors, limit=SHARD_MAX, label="shard")
        singles = [v for v in (doc.get("entries") or {}).values() if isinstance(v, str)]
        _check_files(base, singles, where, errors, label="record file")
    elif kind == "images":
        errors += [f"{where}: {e}" for e in validate_images(doc)]
        refs = _doc_file_refs(doc)
        pages = [x for x in doc.get("pages") or [] if isinstance(x, str)]
        _check_files(base, refs, where, errors, label="image file")
        for pg in pages:
            try:
                sub = _load_json(base / _strip(pg))
            except (OSError, ValueError):
                continue
            _check_files(base, _doc_file_refs(sub), where, errors, label="image file")


def check_build(path: os.PathLike | str, gate: str | None = None) -> CheckReport:
    """``portalkit check BUILD_DIR [--gate G1|G2]``: validate a staged portal directory or
    every portal of a build directory (section 5.11 budgets, schemas, staged references)."""
    if gate is not None and gate not in GATES:
        raise UsageError(f"--gate must be one of {list(GATES)}, got {gate!r}")
    root = Path(path)
    rep = CheckReport(path=str(root), gate=gate)
    if not root.is_dir():
        raise UsageError(f"no such directory: {root}")
    if (root / "portal.json").is_file():
        portals = [root]
    else:
        portals = sorted(d for d in root.iterdir()
                         if d.is_dir() and not d.is_symlink() and (d / "portal.json").is_file())
    if not portals:
        rep.errors.append(f"{root}: no portal.json here or in any sub-directory")
        return rep
    for pdir in portals:
        _check_portal(pdir, gate, rep)
    return rep
