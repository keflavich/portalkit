"""Drift report for ``portalkit status [FAMILY] [--json]`` (ARCHITECTURE.md section 11.5).

Per family and portal it lists:

* the legacy files of every ``[[portal.page]]`` (mtime, sha256, whether a redirect stub
  replaced them);
* the last staged build and its tree sha (``staged_sha``);
* the deployed build (the newest completed build in the ownership manifest)
  and the sha of the files of that build as they are on disk (``served_sha``);
* flags: ``DRIFT`` (staged != served), ``NOT-DEPLOYED``, ``NOT-STAGED``, ``MODIFIED``
  (an owned file differs from the manifest), ``LEGACY-CHANGED`` (a legacy generator module
  differs from the deployed provenance) and ``excluded`` for out-of-scope families.

A tree sha is the sha256 of ``"<rel>\\0<sha256>\\n"`` lines sorted by path.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .. import schema
from . import DeployError, UsageError
from .deploy import (_get, deployed_builds, legacy_entries, load_manifest, manifest_path_for,
                     not_deployed, resolve_target, sha256_file, shared_legacy_dirs)
from .stage import iter_stage_files, latest_build, portal_stage_dir, stage_root as _stage_root

#: Default registry directory: ``<repo>/sites``.
SITES_DIR = Path(__file__).resolve().parents[2] / "sites"


@dataclass
class LegacyFileStatus:
    path: str
    exists: bool
    mtime: str | None = None
    sha256: str | None = None
    stub: bool = False


@dataclass
class StatusRow:
    family: str
    portal: str
    status: str                       # active | excluded
    visibility: str | None = None
    target: str | None = None
    url: str | None = None
    legacy: list[LegacyFileStatus] = field(default_factory=list)
    staged_build: str | None = None
    staged_sha: str | None = None
    deployed_build: str | None = None
    served_sha: str | None = None
    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return asdict(self)


def tree_sha(entries: Mapping[str, str]) -> str:
    h = hashlib.sha256()
    for rel in sorted(entries):
        h.update(f"{rel}\0{entries[rel]}\n".encode("utf-8"))
    return h.hexdigest()


def _iso_mtime(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_sites(sites_dir: os.PathLike | str | None = None,
               family: str | None = None) -> list[dict]:
    """Parse ``sites/<family>.toml`` (or every registry) and validate it (section 4.1)."""
    d = Path(sites_dir) if sites_dir else SITES_DIR
    paths = [d / f"{family}.toml"] if family else sorted(d.glob("*.toml"))
    out = []
    for p in paths:
        try:
            with open(p, "rb") as fh:
                obj = tomllib.load(fh)
        except FileNotFoundError as exc:
            raise UsageError(f"no site registry {p}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise UsageError(f"cannot parse {p}: {exc}") from exc
        errs = schema.validate_site(obj, p.stem)
        if errs:
            raise UsageError(f"invalid site registry {p}", details=errs)
        out.append(obj)
    return out


def _staged_tree(src: Path) -> dict[str, str]:
    files, _ = iter_stage_files(src)
    out = {}
    for rel in files:
        if not_deployed(rel):
            continue
        out[rel] = sha256_file(src / rel)[0]
    return out


def portal_status(family: str, site: Mapping, portal: Any, *,
                  stage_root: os.PathLike | str | None = None, deep: bool = True,
                  target: os.PathLike | str | None = None,
                  shared_legacy_dir: bool = False) -> StatusRow:
    """Status row of one portal. ``deep=False`` trusts the manifest sha256 values instead of
    re-hashing the deployed files (``MODIFIED`` is then not detected)."""
    pid = _get(portal, "id")
    row = StatusRow(family=family, portal=pid, status=_get(site, "status", "active"),
                    visibility=_get(portal, "visibility"), url=_get(portal, "url"))
    if row.status == "excluded":
        row.flags.append("excluded")
        return row
    try:
        tgt = Path(os.path.abspath(target)) if target else resolve_target(portal, family,
                                                                          shared_legacy_dir)
    except UsageError as exc:
        row.notes.append(str(exc))
        return row
    row.target = str(tgt)
    try:
        man = load_manifest(manifest_path_for(tgt, family, pid)) if tgt.is_dir() else None
    except DeployError as exc:
        man = None
        row.notes.append(str(exc))

    stubbed = set()
    if man is not None:
        for r in man.get("replaced", []):
            stubbed.add(os.path.normpath(tgt / r["legacy"]))
    ldir = _get(portal, "legacy_dir")
    for pg in _get(portal, "page", []) or []:
        for lf, _preset, _replace in legacy_entries(pg):
            if not ldir:
                continue
            lp = Path(ldir) / lf
            ent = LegacyFileStatus(path=str(lp), exists=lp.is_file(),
                                   stub=os.path.normpath(lp) in stubbed)
            if ent.exists:
                ent.mtime = _iso_mtime(lp.stat().st_mtime)
                ent.sha256 = sha256_file(lp)[0]
            row.legacy.append(ent)

    root = _stage_root(stage_root)
    row.staged_build = latest_build(root, family, pid)
    if row.staged_build:
        row.staged_sha = tree_sha(_staged_tree(portal_stage_dir(root, family,
                                                                row.staged_build, pid)))
    else:
        row.flags.append("NOT-STAGED")

    if man is None:
        row.flags.append("NOT-DEPLOYED")
        return row
    done = deployed_builds(man)
    row.deployed_build = done[-1] if done else None
    if row.deployed_build is None:
        row.flags.append("NOT-DEPLOYED")
        return row
    served: dict[str, str] = {}
    modified = []
    for rel, ent in man["files"].items():
        if ent.get("build_id") != row.deployed_build:
            continue
        p = tgt / rel
        if not deep:
            served[rel] = ent["sha256"]
            continue
        if os.path.islink(p) or not p.is_file():
            served[rel] = "absent"
            modified.append(rel)
            continue
        served[rel] = sha256_file(p)[0]
        if served[rel] != ent["sha256"]:
            modified.append(rel)
    row.served_sha = tree_sha(served)
    if modified:
        row.flags.append("MODIFIED")
        row.notes.extend(f"modified or missing: {m}" for m in sorted(modified)[:20])
    if row.staged_sha is not None and row.staged_sha != row.served_sha:
        row.flags.append("DRIFT")

    bentry = next(b for b in man["builds"] if b["build_id"] == row.deployed_build)
    changed = []
    for mod in bentry.get("legacy_modules") or []:
        if not isinstance(mod, Mapping) or not mod.get("path") or not mod.get("sha256"):
            continue
        p = Path(mod["path"])
        cur = sha256_file(p)[0] if p.is_file() else "absent"
        if cur != mod["sha256"]:
            changed.append(mod.get("name") or str(p))
    if changed:
        row.flags.append("LEGACY-CHANGED")
        row.notes.extend(f"legacy module changed since deploy: {c}" for c in changed)
    return row


def status(sites: Iterable[Mapping] | Mapping, *, stage_root: os.PathLike | str | None = None,
           deep: bool = True, all_sites: Iterable[Mapping] | None = None) -> list[StatusRow]:
    """Rows for every portal of every site registry given (parsed TOML dicts).

    Shared legacy_dirs (section 4.1) are detected over ``all_sites`` (default: ``sites``);
    ``portalkit status FAMILY`` passes every registry there so a family that shares its
    legacy_dir resolves ``<legacy_dir>/unified/<family>``."""
    if isinstance(sites, Mapping):
        sites = [sites]
    sites = list(sites)
    shared = shared_legacy_dirs(sites if all_sites is None else list(all_sites))
    rows = []
    for site in sites:
        fam = _get(site, "family")
        portals = _get(site, "portal") or []
        if not portals:
            rows.append(StatusRow(family=fam, portal="-", status=_get(site, "status", "active"),
                                  flags=["excluded"] if _get(site, "status") == "excluded"
                                  else ["NO-PORTALS"]))
            continue
        for portal in portals:
            ld = _get(portal, "legacy_dir")
            rows.append(portal_status(fam, site, portal, stage_root=stage_root, deep=deep,
                                      shared_legacy_dir=bool(ld) and
                                      os.path.abspath(ld) in shared))
    return rows


def format_status(rows: Iterable[StatusRow], as_json: bool = False) -> str:
    rows = list(rows)
    if as_json:
        return json.dumps([r.to_json() for r in rows], indent=1, ensure_ascii=False)
    lines = []
    hdr = f"{'family':<20} {'portal':<20} {'staged':<26} {'deployed':<26} flags"
    lines.append(hdr)
    lines.append("-" * len(hdr))
    for r in rows:
        lines.append(f"{r.family:<20} {r.portal:<20} {r.staged_build or '-':<26} "
                     f"{r.deployed_build or '-':<26} {' '.join(r.flags) or 'ok'}")
        for lf in r.legacy:
            state = "stub" if lf.stub else ("present" if lf.exists else "missing")
            lines.append(f"    legacy {lf.path} [{state}] {lf.mtime or ''} "
                         f"{(lf.sha256 or '')[:12]}")
        if r.served_sha or r.staged_sha:
            lines.append(f"    staged sha {(r.staged_sha or '-')[:12]}  "
                         f"served sha {(r.served_sha or '-')[:12]}")
        for n in r.notes:
            lines.append(f"    note: {n}")
    return "\n".join(lines)
