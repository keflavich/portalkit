"""``portalkit`` command line (ARCHITECTURE.md section 10).

Exit codes: 0 ok, 1 check or verify failure, 2 usage error, 3 exposure refusal, 4 ownership
conflict, 5 blocked input (tmp path), 6 legacy import failure. Every
:class:`~portalkit.deploy.DeployError` carries its code as ``exit_code``.

A global ``--sites-dir`` (default ``$PK_SITES_DIR`` or ``<repo>/sites``) selects the site
registries; tests point it at a temporary directory.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import io
import json
import math
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Sequence

from .version import VERSION

__all__ = ["main", "build_parser", "checklist_diff", "CHECKLIST_KEYS", "DEFAULT_CHECKLIST"]

#: join key columns of the hand-edited archive checklist (never written by portalkit)
CHECKLIST_KEYS = ("Target", "Program", "Object", "SlitID", "Observation", "Visit", "VisitGroup",
                  "Filename")
DEFAULT_CHECKLIST = "/orange/adamginsburg/jwst/spectra/mastDownload/JWST/jwst_archive_spectra_checklist.csv"
DEFAULT_ASSETS_DEST = "/orange/adamginsburg/web/public/portalkit"
DEFAULT_RES_DEST = "/orange/adamginsburg/web/public/portalkit-res"


def _emap():
    """Exposure map of the served roots (cached scan, section 11.2). Tests replace this."""
    from .deploy import scan
    return scan()


def _stdin_is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _confirm(prompt: str) -> bool:
    try:
        ans = input(prompt)
    except EOFError:
        return False
    return ans.strip().lower() in ("y", "yes")


def _err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_build(args: argparse.Namespace) -> int:
    from .build import build_family
    pages = [p for p in (args.pages or "").split(",") if p] or None
    res = build_family(args.family, tier=args.tier, pages=pages, limit=args.limit,
                       stage_root=args.stage_root, allow_tmp_inputs=args.allow_tmp_inputs,
                       vendor_assets=args.vendor_assets, jobs=args.jobs,
                       sites_dir=args.sites_dir, emap=_emap(), no_cache=args.no_cache)
    # build_family logs per-portal errors itself; summarize warnings here.
    print(f"build {res.build_id}: {len(res.pages)} page(s) staged under {res.staging}")
    for pid, rep in res.report.get("portals", {}).items():
        warns = rep.get("warnings", [])
        for w in warns[:20]:
            print(f"  [{pid}] warning: {w}")
        if len(warns) > 20:
            print(f"  [{pid}] ... {len(warns) - 20} more warning(s) in build_report.json")
    print("OK" if res.ok else "FAILED")
    return 0 if res.ok else 1


def cmd_check(args: argparse.Namespace) -> int:
    from .build import check_build
    rep = check_build(args.build_dir, gate=args.gate)
    print(json.dumps(rep.to_json(), indent=1) if args.json else rep.format())
    return rep.exit_code


def _foreign_manifests(ds: dict, log: Callable[[str], None]) -> tuple[dict, dict[str, str]]:
    """``pk-catalog/1`` manifests of the foreign (GC) catalogs in ``ds``, converted in
    memory from their GC manifests as ``build`` does (nothing is written), and
    ``{ds: reason}`` for those that could not be read."""
    from .legacy import gc_manifest
    from .spec import CatalogSpec
    out: dict[str, dict] = {}
    bad: dict[str, str] = {}
    for k, spec in ds.items():
        if not isinstance(spec, CatalogSpec) or spec.foreign is None:
            continue
        try:
            m = gc_manifest.read_manifest(spec.foreign.manifest_path)
            u8 = (m.get("meta", {}).get("tile", {}).get("dtype", "u8") == "u8")
            out[k] = gc_manifest.convert(m, ds=k, url_base=spec.foreign.url_base,
                                         tile_file="pos/tile.bin" if u8 else None,
                                         title=spec.title or None,
                                         source_manifest=str(spec.foreign.manifest_path))
        except gc_manifest.GcManifestError as exc:
            bad[k] = str(exc)
            log(f"{k}: {exc}")
    return out, bad


def caps_matrix(family: str, *, sites_dir: str | None = None, limit: int = 50,
                tier: str | None = None, log: Callable[[str], None] | None = None) -> list[dict]:
    """Capability rows per page: runs the adapter in fixture mode (``limit``) and the
    resolver on the dataset specs (foreign GC catalogs on their converted manifests, as
    ``build`` resolves them); writes nothing. A row's ``unresolved`` lists the page's
    foreign catalogs whose manifest could not be read (their capabilities are then
    under-reported)."""
    from . import caps as capsmod
    from .build import load_adapter
    from .context import BuildContext, InputRegistry, cache_base, load_site
    from .spec import TierUnavailable
    from .deploy import UsageError
    site = load_site(family, sites_dir)
    adapter, _, _ = load_adapter(site.adapter or "")
    t = tier or site.tier_default
    ctx = BuildContext(family=family, site=site, staging=cache_base() / family / "caps_ctx",
                       tier=t, limit=limit, allow_tmp_inputs=True,
                       inputs=InputRegistry(True), log=log or (lambda s: None))
    try:
        ds = adapter.datasets(ctx)
        pages = adapter.pages(ctx, ds)
    except TierUnavailable as exc:
        raise UsageError(f"adapter cannot run tier {t}: {exc}") from exc
    manifests, bad = _foreign_manifests(ds, log or (lambda s: None))
    rows = []
    for p in pages:
        r = capsmod.resolve(p, ds, manifests)
        row = {"page": p.id, "portal": p.portal or site.portal().id,
               "archetype": p.archetype, "flags": list(r.flags),
               "enabled": list(r.enabled), "disabled": dict(r.disabled),
               "added_panes": [x.get("pane") for x in r.added_panes]}
        unresolved = [k for k in capsmod.page_data(p, ds, manifests).ids if k in bad]
        if unresolved:
            row["unresolved"] = unresolved
        rows.append(row)
    return rows


def cmd_caps(args: argparse.Namespace) -> int:
    rows = caps_matrix(args.family, sites_dir=args.sites_dir)
    if args.json:
        print(json.dumps(rows, indent=1))
        return 0
    for r in rows:
        print(f"{r['portal']}/{r['page']} ({r['archetype']}) flags: {' '.join(r['flags'])}")
        print(f"  {len(r['enabled'])} enabled: {', '.join(r['enabled'])}")
        if r.get("unresolved"):
            print(f"  ! unresolved foreign catalog(s): {', '.join(r['unresolved'])}")
        for cap, why in sorted(r["disabled"].items()):
            print(f"  - {cap}: {why}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .deploy.devserver import serve
    if not Path(args.build_dir).is_dir():
        from .deploy import UsageError
        raise UsageError(f"no such directory: {args.build_dir}")
    serve(args.build_dir, args.port, args.host, auth=args.auth,
          content_encoding=args.content_encoding, no_range=args.no_range,
          latency_ms=args.latency_ms)
    return 0


def _site(family: str, sites_dir: str | None) -> dict:
    from .deploy import load_sites
    return load_sites(sites_dir, family)[0]


def _shared(entry: Any, sites_dir: str | None) -> bool:
    from .deploy import is_shared_legacy_dir, load_sites
    return is_shared_legacy_dir(entry, load_sites(sites_dir))


def cmd_deploy(args: argparse.Namespace) -> int:
    from .deploy import UsageError, plan_deploy, run_deploy, select_portal
    from .deploy.deploy import raise_for_plan
    from .deploy.stage import stage_root
    site = _site(args.family, args.sites_dir)
    entry = select_portal(site, args.portal)
    shared = _shared(entry, args.sites_dir)
    emap = _emap()
    tty = _stdin_is_tty()
    # Without a TTY a warn-level exposure decision is refused unless --yes (item 25).
    plan_yes = bool(args.yes or tty)
    root = stage_root(args.stage_root)
    kw = dict(portal_id=entry.get("id"), stage_root=root, target=args.target,
              replace=args.replace, htaccess=args.htaccess, emap=emap, shared_legacy_dir=shared)
    plan = plan_deploy(args.family, args.build_id, site, yes=plan_yes, **kw)
    raise_for_plan(plan)
    print(plan.summary())
    if args.dry_run:
        run_deploy(args.family, args.build_id, site, dry_run=True, yes=plan_yes, gc=False,
                   log=print, **kw)
        print("dry run: nothing written")
        return 0
    if not args.yes:
        if not tty:
            raise UsageError("stdin is not a TTY: pass --yes to deploy (or --dry-run)")
        if not _confirm(f"deploy {args.family}/{plan.portal_id} build {args.build_id} "
                        f"to {plan.target}? [y/N] "):
            print("not deployed")
            return 1
    res = run_deploy(args.family, args.build_id, site, dry_run=False, yes=True, gc=args.gc,
                     log=print, **kw)
    print(f"deployed {len(res.written)} file(s) to {plan.target}")
    if res.replaced:
        print(f"replaced {len(res.replaced)} legacy file(s)")
    if args.gc:
        print(f"gc removed {len(res.gc_removed)} file(s)")
    return res.exit_code


def cmd_rollback(args: argparse.Namespace) -> int:
    from .deploy import rollback_portal, select_portal
    site = _site(args.family, args.sites_dir)
    entry = select_portal(site, args.portal)
    res = rollback_portal(args.family, args.build_id, site, portal_id=entry.get("id"),
                          shared_legacy_dir=_shared(entry, args.sites_dir))
    print(f"rollback {args.family}/{entry.get('id')} build {args.build_id}: "
          f"{len(res.restored)} restored, {len(res.skipped)} skipped")
    for s in res.skipped:
        print(f"  skipped {s}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    from .deploy import verify
    site = _site(args.family, args.sites_dir)
    portals = site.get("portal") or []
    ids = [args.portal] if args.portal else [p.get("id") for p in portals]
    emap = _emap()
    code = 0
    for pid in ids:
        from .deploy import select_portal
        entry = select_portal(site, pid)
        rep = verify(args.family, site, portal_id=pid, http=args.http, auth_env=args.auth_env,
                     emap=emap, shared_legacy_dir=_shared(entry, args.sites_dir))
        print(rep.format())
        code = max(code, rep.exit_code)
    return code


def cmd_status(args: argparse.Namespace) -> int:
    from .deploy import format_status, load_sites, status_rows
    from .deploy.stage import stage_root
    sites = load_sites(args.sites_dir, args.family)
    everything = load_sites(args.sites_dir) if args.family else sites
    rows = status_rows(sites, stage_root=stage_root(None), all_sites=everything)
    print(format_status(rows, as_json=args.json))
    return 0


def cmd_publish_assets(args: argparse.Namespace) -> int:
    from .build import WEB_DIR
    from .deploy import publish_assets
    version = args.version or VERSION
    res = publish_assets(WEB_DIR, args.dest, version, dry_run=args.dry_run, emap=_emap())
    print(f"publish-assets v{version} -> {res.dest}: {len(res.created)} new, "
          f"{len(res.same)} unchanged{' (dry run)' if args.dry_run else ''}")
    return 0


def cmd_publish_res(args: argparse.Namespace) -> int:
    from .build import shared_resources
    from .bundle.writer import BundleWriter, Ledger
    from .context import cache_base
    from .deploy import publish_immutable
    base = cache_base()
    base.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="publish-res-", dir=base))
    try:
        refs = shared_resources(BundleWriter(tmp, "", Ledger()))
        res = publish_immutable(tmp, args.dest, dry_run=args.dry_run, emap=_emap())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"publish-res -> {res.dest}: {len(res.created)} new, {len(res.same)} unchanged"
          f"{' (dry run)' if args.dry_run else ''}")
    for k, v in sorted(refs.items()):
        print(f"  {k}: {v}")
    return 0


def cmd_extract_legacy(args: argparse.Namespace) -> int:
    from .legacy.extract import payloads
    found = payloads(Path(args.html))
    if not found:
        from .legacy.extract import LegacyExtractError
        raise LegacyExtractError(f"no embedded payload found in {args.html}")
    first = found[0]
    value = _jsonable(first.value)
    for e in found:
        v = e.value
        desc = (f"{len(v)} keys" if isinstance(v, dict) else
                f"{len(v)} items" if isinstance(v, list) else type(v).__name__)
        _err(f"{e['style']} {e['name']}: {desc} at offset {e['start']}")
    if isinstance(first.value, dict) and "dims" in first.value:
        from .legacy.manifold_payload import ManifoldPayloadError, convert
        try:
            cat, man = convert(first.value)
        except (ManifoldPayloadError, ValueError, KeyError, TypeError) as exc:
            _err(f"manifold conversion: {exc}")
        else:
            _err(f"manifold payload: {len(cat.table)} rows, {len(cat.columns)} columns, "
                 f"{len(man.dims)} dims, {len(cat.presets)} presets")
    text = json.dumps(value, indent=1, ensure_ascii=False, allow_nan=False)
    if args.out:
        out = Path(args.out)
        if out.exists() and out.is_dir():
            from .deploy import UsageError
            raise UsageError(f"--out {out} is a directory")
        out.write_text(text + "\n", encoding="utf-8")
        _err(f"wrote {out}")
    else:
        print(text)
    return 0


def cmd_thumbs(args: argparse.Namespace) -> int:
    from .build import make_thumbs
    out = make_thumbs(args.family, max_px=args.max_px, sites_dir=args.sites_dir)
    print(f"thumbs: {out['made']} made, {out['cached']} cached, {out['small']} small enough, "
          f"{out['remote']} not local")
    return 0


# ---------------------------------------------------------------------------
# archive checklist diff (never writes the checklist)
# ---------------------------------------------------------------------------

def _norm_key(v: Any) -> str:
    s = "" if v is None else str(v).strip()
    try:
        f = float(s)
    except ValueError:
        return s
    if math.isfinite(f) and f == int(f):
        return str(int(f))
    return s


def checklist_diff(export_text: str, checklist_text: str, *, checklist_name: str = "checklist",
                   column: str | None = None) -> tuple[str, list[str]]:
    """Unified diff that applies the codes of an annotation export (``id, <keys>, <code
    column>``) to the checklist rows matching all :data:`CHECKLIST_KEYS`. Returns ``(diff,
    notes)``. The code goes to ``column``, else to the export's code column when the
    checklist has it, else to the first non-key checklist column."""
    from .deploy import UsageError
    ex_rows = list(csv.reader(io.StringIO(export_text)))
    if not ex_rows:
        raise UsageError("export CSV is empty")
    ex_hdr = ex_rows[0]
    missing = [k for k in CHECKLIST_KEYS if k not in ex_hdr]
    if missing:
        raise UsageError(f"export CSV lacks checklist key columns {missing}")
    extra = [h for h in ex_hdr if h not in CHECKLIST_KEYS and h != "id"]
    if not extra:
        raise UsageError("export CSV has no code column")
    code_col = extra[-1]
    lines = checklist_text.splitlines(keepends=True)
    if not lines:
        raise UsageError("checklist is empty")
    hdr = next(csv.reader([lines[0]]))
    miss = [k for k in CHECKLIST_KEYS if k not in hdr]
    if miss:
        raise UsageError(f"checklist lacks key columns {miss}")
    notes: list[str] = []
    if column is None:
        column = code_col if code_col in hdr else next(h for h in hdr if h not in CHECKLIST_KEYS)
        if code_col not in hdr:
            notes.append(f"export column {code_col!r} is not a checklist column; codes go to {column!r}")
    elif column not in hdr:
        raise UsageError(f"checklist has no column {column!r}")
    ci = hdr.index(column)
    kidx = [hdr.index(k) for k in CHECKLIST_KEYS]
    index: dict[tuple, list[int]] = {}
    for ln in range(1, len(lines)):
        if not lines[ln].strip():
            continue
        row = next(csv.reader([lines[ln]]))
        key = tuple(_norm_key(row[i]) if i < len(row) else "" for i in kidx)
        index.setdefault(key, []).append(ln)
    ex_k = [ex_hdr.index(k) for k in CHECKLIST_KEYS]
    ex_c = ex_hdr.index(code_col)
    new = list(lines)
    changed = 0
    for r in ex_rows[1:]:
        if not r or not any(x.strip() for x in r):
            continue
        key = tuple(_norm_key(r[i]) if i < len(r) else "" for i in ex_k)
        code = r[ex_c].strip() if ex_c < len(r) else ""
        hits = index.get(key)
        if not hits:
            notes.append(f"no checklist row matches {r[0] if r else '?'} ({', '.join(key[:2])})")
            continue
        for ln in hits:
            row = next(csv.reader([new[ln]]))
            while len(row) <= ci:
                row.append("")
            if row[ci] == code:
                continue
            row[ci] = code
            eol = new[ln][len(new[ln].rstrip("\r\n")):] or "\n"
            buf = io.StringIO()
            csv.writer(buf, lineterminator=eol).writerow(row)
            new[ln] = buf.getvalue()
            changed += 1
    notes.append(f"{changed} checklist row(s) change")
    diff = "".join(difflib.unified_diff(lines, new, fromfile=checklist_name,
                                        tofile=checklist_name + " (with export)"))
    return diff, notes


def cmd_archive_checklist_diff(args: argparse.Namespace) -> int:
    with open(args.export, encoding="utf-8-sig", newline="") as fh:
        export_text = fh.read()
    with open(args.checklist, encoding="utf-8", newline="") as fh:
        checklist_text = fh.read()
    diff, notes = checklist_diff(export_text, checklist_text, checklist_name=args.checklist,
                                 column=args.column)
    sys.stdout.write(diff)
    for n in notes:
        _err(n)
    return 0


def cmd_prune_stage(args: argparse.Namespace) -> int:
    from .deploy import prune_stage, resolve_target
    from .deploy.deploy import all_manifests
    from .deploy.stage import stage_root
    protect: set[str] = set()
    site = _site(args.family, args.sites_dir)
    for entry in site.get("portal") or []:
        try:
            tgt = resolve_target(entry, args.family, _shared(entry, args.sites_dir))
        except (ValueError, OSError):
            continue
        for _path, man in all_manifests(tgt):
            if man.get("family") != args.family:
                continue
            for b in man.get("builds", []):
                if b.get("build_id"):
                    protect.add(b["build_id"])
    removed = prune_stage(stage_root(args.stage_root), args.family, keep=args.keep,
                          protect=sorted(protect), dry_run=args.dry_run)
    for d in removed:
        print(f"{'would remove' if args.dry_run else 'removed'} {d}")
    print(f"{len(removed)} build(s) {'to prune' if args.dry_run else 'pruned'}; "
          f"{len(protect)} deployed build(s) protected")
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def _positive(v: str) -> int:
    n = int(v)
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


def _nonneg(v: str) -> int:
    n = int(v)
    if n < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return n


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="portalkit", description="Static portal builder (ARCHITECTURE.md section 10)")
    p.add_argument("--version", action="version", version=f"portalkit {VERSION}")
    p.add_argument("--sites-dir", default=None,
                   help="site registry directory (default $PK_SITES_DIR or <repo>/sites)")
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="build a family into the staging tree")
    b.add_argument("family")
    b.add_argument("--tier", choices=("A", "B", "C"))
    b.add_argument("--pages", help="comma-separated page ids")
    b.add_argument("--limit", type=_positive, help="fixture mode: rows per catalog")
    b.add_argument("--stage-root", help="default $PK_STAGE_ROOT or /orange/adamginsburg/portalkit_stage")
    b.add_argument("--allow-tmp-inputs", action="store_true")
    b.add_argument("--vendor-assets", action="store_true")
    b.add_argument("--no-cache", action="store_true")
    b.add_argument("--jobs", type=_positive, default=1)
    b.set_defaults(fn=cmd_build)

    c = sub.add_parser("check", help="check a staged build")
    c.add_argument("build_dir")
    c.add_argument("--gate", choices=("G1", "G2"))
    c.add_argument("--json", action="store_true")
    c.set_defaults(fn=cmd_check)

    k = sub.add_parser("caps", help="capability matrix per page (fixture mode, writes nothing)")
    k.add_argument("family")
    k.add_argument("--json", action="store_true")
    k.set_defaults(fn=cmd_caps)

    s = sub.add_parser("serve", help="local dev server")
    s.add_argument("build_dir")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--auth", help="USER:PASS")
    s.add_argument("--content-encoding", choices=("auto", "none", "gzip"), default="auto")
    s.add_argument("--no-range", action="store_true")
    s.add_argument("--latency-ms", type=float, default=0.0)
    s.set_defaults(fn=cmd_serve)

    d = sub.add_parser("deploy", help="deploy a staged build")
    d.add_argument("family")
    d.add_argument("build_id")
    d.add_argument("--portal")
    d.add_argument("--target")
    d.add_argument("--replace", nargs="?", const="stub", choices=("stub", "inplace"))
    d.add_argument("--htaccess", action="store_true")
    d.add_argument("--dry-run", action="store_true")
    d.add_argument("--yes", action="store_true")
    d.add_argument("--gc", action="store_true", help="remove unreferenced owned files older than 7 days")
    d.add_argument("--stage-root")
    d.set_defaults(fn=cmd_deploy)

    r = sub.add_parser("rollback", help="restore legacy files replaced by a build")
    r.add_argument("family")
    r.add_argument("build_id")
    r.add_argument("--portal")
    r.set_defaults(fn=cmd_rollback)

    v = sub.add_parser("verify", help="verify a deployed portal")
    v.add_argument("family")
    v.add_argument("--portal")
    v.add_argument("--http", action="store_true")
    v.add_argument("--auth-env", default="PK_BASIC_AUTH")
    v.set_defaults(fn=cmd_verify)

    st = sub.add_parser("status", help="staged and deployed builds")
    st.add_argument("family", nargs="?")
    st.add_argument("--json", action="store_true")
    st.set_defaults(fn=cmd_status)

    pa = sub.add_parser("publish-assets", help="publish web/ as an immutable asset version")
    pa.add_argument("--version", dest="version", default=None)
    pa.add_argument("--dest", default=DEFAULT_ASSETS_DEST)
    pa.add_argument("--dry-run", action="store_true")
    pa.set_defaults(fn=cmd_publish_assets)

    pr = sub.add_parser("publish-res", help="publish the shared {res} resources")
    pr.add_argument("--dest", default=DEFAULT_RES_DEST)
    pr.add_argument("--dry-run", action="store_true")
    pr.set_defaults(fn=cmd_publish_res)

    x = sub.add_parser("extract-legacy", help="print the embedded payload of a legacy page")
    x.add_argument("html")
    x.add_argument("--out")
    x.set_defaults(fn=cmd_extract_legacy)

    t = sub.add_parser("thumbs", help="cache thumbnails of image sets (matplotlib)")
    t.add_argument("family")
    t.add_argument("--max-px", type=_positive, default=480)
    t.set_defaults(fn=cmd_thumbs)

    a = sub.add_parser("archive-checklist-diff",
                       help="diff an annotation export against the checklist (never writes it)")
    a.add_argument("export")
    a.add_argument("--checklist", default=DEFAULT_CHECKLIST)
    a.add_argument("--column", default=None, help="checklist column receiving the codes")
    a.set_defaults(fn=cmd_archive_checklist_diff)

    ps = sub.add_parser("prune-stage", help="delete old staged builds (deployed ones are kept)")
    ps.add_argument("family")
    ps.add_argument("--keep", type=_nonneg, default=5)
    ps.add_argument("--stage-root")
    ps.add_argument("--dry-run", action="store_true")
    ps.set_defaults(fn=cmd_prune_stage)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    from .config import ConfigError
    from .deploy import DeployError
    from .legacy.extract import LegacyExtractError
    from .legacy.importshim import LegacyImportError
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    # One registry directory for every command: --sites-dir, else $PK_SITES_DIR, else
    # <repo>/sites (portalkit.deploy.load_sites alone would skip the environment variable).
    from .context import sites_dir
    args.sites_dir = str(sites_dir(args.sites_dir))
    try:
        return int(args.fn(args) or 0)
    except DeployError as exc:
        _err(f"portalkit {args.command}: {exc}")
        return int(getattr(exc, "exit_code", 1))
    except LegacyImportError as exc:
        _err(f"portalkit {args.command}: legacy import failed: {exc}")
        return 6
    except ConfigError as exc:
        _err(f"portalkit {args.command}: {exc}")
        return 1
    except LegacyExtractError as exc:
        _err(f"portalkit {args.command}: {exc}")
        return 1
    except FileNotFoundError as exc:
        _err(f"portalkit {args.command}: {exc}")
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
