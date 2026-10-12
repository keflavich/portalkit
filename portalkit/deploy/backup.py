"""``--replace`` backups and rollback (ARCHITECTURE.md section 11.4).

For each legacy file replaced by build ``B`` the original goes to
``<legacy_dir>/.portalkit_backup/B/<file>`` and ``<legacy_dir>/.portalkit_backup/B/backup.json``
records its md5 and size::

    {"schema": "pk-backup/1", "build_id": "...", "legacy_dir": "/abs", "target": "/abs",
     "created": "2026-10-10T12:00:00Z",
     "files": [{"file": "sgrb2_map_ccd.html", "backup": ".portalkit_backup/B/sgrb2_map_ccd.html",
                "md5": "...", "size": 1234, "mtime": 1.7e9, "mode": "stub", "page": "map",
                "unified": "unified/map.html", "written_sha256": "..."}]}

``written_sha256`` is the sha256 of the stub (or inplace page) deploy wrote over the legacy
file. Rollback refuses (exit 4) to overwrite a legacy file whose content differs from it,
because someone edited the file after the replace.

Whenever :func:`backup_files` writes to ``<legacy_dir>/.portalkit_backup/`` it first writes
``.portalkit_backup/.htaccess`` (:func:`portalkit.deploy.htaccess.write_backup_htaccess`): the
portalkit marker and the deny rule only, so a served legacy directory does not serve the
backups or ``backup.json``. A foreign or symlinked ``.htaccess`` there refuses (exit 4 / 3)
before anything is copied.

portalkit never moves or deletes legacy data directories.
"""

from __future__ import annotations

import datetime as _dt
import html as _html
import json
import os
import posixpath
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import DeployError, OwnershipConflict, SymlinkRefusal, UsageError, VerifyFailure
from .deploy import (_get, atomic_write, check_components, iso, load_manifest, manifest_path_for,
                     md5_file, resolve_target, select_portal, sha256_file, utcnow, write_manifest)

BACKUP_DIRNAME = ".portalkit_backup"
BACKUP_JSON = "backup.json"
BACKUP_SCHEMA = "pk-backup/1"
#: Build note written by :func:`rollback` (the build stays deployed; only its replace is undone).
REPLACE_ROLLED_BACK = "replace rolled back"


def backup_dir(legacy_dir: os.PathLike | str, build_id: str) -> Path:
    return Path(legacy_dir) / BACKUP_DIRNAME / build_id


def _js_string(s: str) -> str:
    """Single-quoted JS string literal that is also safe inside a <script> element."""
    out = s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "\\r")
    return "'" + out.replace("</", "<\\/").replace("<!--", "<\\!--") + "'"


def stub_html(url: str, preset: str = "") -> str:
    """The section 11.4 redirect stub (about 300 bytes): keeps the query string and the
    hash, falling back to ``#<preset>`` when the visitor has no hash."""
    frag = ""
    if preset:
        frag = preset if preset.startswith("#") else "#" + preset
    return ("<!doctype html><meta charset=\"utf-8\"><title>Moved</title>\n"
            f"<script>location.replace({_js_string(url)}+location.search+"
            f"(location.hash||{_js_string(frag)}));</script>\n"
            f"<noscript><a href=\"{_html.escape(url + frag, quote=True)}\">This page moved</a>"
            "</noscript>\n")


def read_backup(legacy_dir: os.PathLike | str, build_id: str) -> dict | None:
    """Load ``backup.json`` of a build; ``None`` when the build made no backups."""
    p = backup_dir(legacy_dir, build_id) / BACKUP_JSON
    try:
        with open(p, encoding="utf-8") as fh:
            obj = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise DeployError(f"cannot read {p}: {exc}") from exc
    errs = validate_backup(obj)
    if errs:
        raise DeployError(f"invalid {p}", details=errs)
    return obj


def validate_backup(obj: Any) -> list[str]:
    errs = []
    if not isinstance(obj, dict):
        return ["backup.json must be an object"]
    if obj.get("schema") != BACKUP_SCHEMA:
        errs.append(f"schema must be {BACKUP_SCHEMA!r}")
    for k in ("build_id", "legacy_dir", "created"):
        if not isinstance(obj.get(k), str):
            errs.append(f"missing string {k!r}")
    files = obj.get("files")
    if not isinstance(files, list):
        return errs + ["files must be a list"]
    for i, f in enumerate(files):
        if not isinstance(f, dict):
            errs.append(f"files[{i}] must be an object")
            continue
        for k in ("file", "backup", "md5", "mode"):
            if not isinstance(f.get(k), str):
                errs.append(f"files[{i}].{k} must be a string")
        if not isinstance(f.get("size"), int):
            errs.append(f"files[{i}].size must be an integer")
        fn = f.get("file")
        if isinstance(fn, str) and (fn.startswith("/") or ".." in fn.split("/")):
            errs.append(f"files[{i}].file must be relative without '..'")
    return errs


def _write_backup_json(legacy_dir: Path, build_id: str, obj: dict) -> None:
    errs = validate_backup(obj)
    if errs:
        raise DeployError("refusing to write an invalid backup.json", details=errs)
    data = (json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode("utf-8")
    atomic_write(backup_dir(legacy_dir, build_id) / BACKUP_JSON, data)


def _ensure_backup_dirs(legacy_dir: Path, build_id: str, rel_dir: str) -> Path:
    cur = legacy_dir
    for part in [BACKUP_DIRNAME, build_id] + [p for p in rel_dir.split("/") if p]:
        cur = cur / part
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            os.mkdir(cur, 0o755)
            continue
        if stat.S_ISLNK(st.st_mode):
            raise SymlinkRefusal(f"backup path {cur} is a symlink")
        if not stat.S_ISDIR(st.st_mode):
            raise OwnershipConflict("a file is in the way of the backup directory", [str(cur)])
    return cur


def backup_files(legacy_dir: os.PathLike | str, build_id: str, entries: Iterable[Mapping], *,
                 target: os.PathLike | str | None = None, now: _dt.datetime | None = None,
                 dry_run: bool = False) -> list[dict]:
    """Copy each legacy file to the build's backup directory and record it in backup.json.

    ``entries`` hold at least ``file`` (relative to ``legacy_dir``) and ``mode``; extra keys
    (``page``, ``unified``, ``written_sha256``) are stored as given. A file already backed up
    under the same build keeps its first backup (re-running a deploy of the same build never
    overwrites the original with a stub). Returns the backup.json file records."""
    ldir = Path(os.path.abspath(legacy_dir))
    now = now or utcnow()
    existing = read_backup(ldir, build_id)
    doc = existing or {"schema": BACKUP_SCHEMA, "build_id": build_id, "legacy_dir": str(ldir),
                       "created": iso(now), "files": []}
    if target is not None:
        doc["target"] = str(os.path.abspath(target))
    by_file = {f["file"]: f for f in doc["files"]}
    # <legacy_dir>/.portalkit_backup/ may be served (backup.json records absolute paths): it
    # gets the deny-only managed .htaccess before anything is copied into it. The dry-run call
    # refuses a foreign or symlinked file before any directory is made (a dry run of
    # backup_files stops there).
    from .htaccess import write_backup_htaccess
    write_backup_htaccess(ldir, dry_run=True)
    if not dry_run:
        _ensure_backup_dirs(ldir, build_id, "")
        write_backup_htaccess(ldir)
    out = []
    for ent in entries:
        rel = posixpath.normpath(ent["file"])
        if rel.startswith(("/", "../")) or rel == "..":
            raise UsageError(f"legacy file {ent['file']!r} escapes legacy_dir")
        src = ldir / rel
        check_components(ldir, rel)
        if rel in by_file:
            rec = by_file[rel]
            for k in ("mode", "page", "unified", "written_sha256"):
                if k in ent:
                    rec[k] = ent[k]
            out.append(rec)
            continue
        st = os.lstat(src)
        if not stat.S_ISREG(st.st_mode):
            raise DeployError(f"legacy path {src} is not a regular file")
        bak_rel = posixpath.join(BACKUP_DIRNAME, build_id, rel)
        rec = {"file": rel, "backup": bak_rel, "md5": md5_file(src), "size": st.st_size,
               "mtime": st.st_mtime, "mode": ent.get("mode", "stub")}
        for k in ("page", "unified", "written_sha256"):
            if k in ent:
                rec[k] = ent[k]
        if not dry_run:
            _ensure_backup_dirs(ldir, build_id, posixpath.dirname(rel))
            dest = ldir / bak_rel
            atomic_write(dest, src=src, mode=stat.S_IMODE(st.st_mode) | 0o400)
            os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns))
            if md5_file(dest) != rec["md5"]:
                raise VerifyFailure(f"backup of {src} does not match its md5")
        doc["files"].append(rec)
        by_file[rel] = rec
        out.append(rec)
    if not dry_run:
        _ensure_backup_dirs(ldir, build_id, "")
        _write_backup_json(ldir, build_id, doc)
    return out


@dataclass
class RollbackResult:
    build_id: str
    legacy_dir: Path
    restored: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    manifest_updated: bool = False
    dry_run: bool = False


def rollback(legacy_dir: os.PathLike | str, build_id: str, *,
             target: os.PathLike | str | None = None, family: str | None = None,
             portal_id: str | None = None, force: bool = False, dry_run: bool = False,
             now: _dt.datetime | None = None) -> RollbackResult:
    """Restore every legacy file of ``build_id`` from backup.json, verify its md5, and update
    the ownership manifest in ``target`` (drop the build's ``replaced`` rows and note
    ``replace rolled back <ts>`` on the build).

    Only the replace is undone: the unified files the build wrote stay in the target and in
    the manifest, so the build still counts as deployed (:func:`deployed_builds`). ``target``
    defaults to the one recorded in backup.json.

    Refuses (exit 4) when a legacy file no longer holds what the replace wrote, unless
    ``force``; refuses (exit 1) when a backup copy fails its md5."""
    ldir = Path(os.path.abspath(legacy_dir))
    now = now or utcnow()
    doc = read_backup(ldir, build_id)
    if doc is None:
        raise UsageError(f"no backups for build {build_id} under {ldir / BACKUP_DIRNAME}")
    res = RollbackResult(build_id=build_id, legacy_dir=ldir, dry_run=dry_run)
    # verify everything before touching anything
    conflicts = []
    for rec in doc["files"]:
        bak = ldir / rec["backup"]
        check_components(ldir, rec["backup"])
        if not bak.is_file():
            raise VerifyFailure(f"backup copy {bak} is missing")
        if md5_file(bak) != rec["md5"]:
            raise VerifyFailure(f"backup copy {bak} fails its md5 check")
        dest = ldir / rec["file"]
        check_components(ldir, rec["file"])
        if dest.exists() and rec.get("written_sha256") and not force:
            cur, _ = sha256_file(dest)
            if cur != rec["written_sha256"] and md5_file(dest) != rec["md5"]:
                conflicts.append(f"{dest} changed after the replace")
    if conflicts:
        raise OwnershipConflict("legacy files were edited after --replace; pass force to "
                                "overwrite them", conflicts)
    for rec in doc["files"]:
        dest = ldir / rec["file"]
        bak = ldir / rec["backup"]
        if dest.exists() and md5_file(dest) == rec["md5"]:
            res.skipped.append(rec["file"])
            continue
        if not dry_run:
            bst = os.stat(bak)
            atomic_write(dest, src=bak, mode=stat.S_IMODE(bst.st_mode))
            os.utime(dest, ns=(bst.st_atime_ns, bst.st_mtime_ns))
            if md5_file(dest) != rec["md5"]:
                raise VerifyFailure(f"restored {dest} fails its md5 check")
        res.restored.append(rec["file"])
    if dry_run:
        return res
    doc["rolled_back"] = iso(now)
    _write_backup_json(ldir, build_id, doc)

    tdir = Path(os.path.abspath(target)) if target else (
        Path(doc["target"]) if doc.get("target") else None)
    if tdir is not None and tdir.is_dir():
        if family is not None and portal_id is not None:
            paths = [manifest_path_for(tdir, family, portal_id)]
        else:
            from .deploy import all_manifests
            paths = [p for p, _ in all_manifests(tdir)]
        for mpath in paths:
            man = load_manifest(mpath)
            if man is None:
                continue
            before = len(man.get("replaced", []))
            mine = {posixpath.join(BACKUP_DIRNAME, build_id, r["file"]) for r in doc["files"]}
            man["replaced"] = [r for r in man.get("replaced", []) if r.get("backup") not in mine]
            touched = len(man["replaced"]) != before
            for b in man.get("builds", []):
                if b.get("build_id") == build_id:
                    b["note"] = f"{REPLACE_ROLLED_BACK} {iso(now)}"
                    touched = True
            if touched:
                write_manifest(mpath, man)
                res.manifest_updated = True
    return res


def rollback_portal(family: str, build_id: str, site: Any, *, portal_id: str | None = None,
                    target: os.PathLike | str | None = None, force: bool = False,
                    dry_run: bool = False, shared_legacy_dir: bool = False) -> RollbackResult:
    """``portalkit rollback FAMILY BUILD_ID [--portal ID]``: resolve the portal's legacy_dir
    from the site registry and call :func:`rollback`.

    The target whose manifest is updated is, in order: ``target`` when given, the ``target``
    recorded in the build's backup.json (the directory the deploy actually wrote), else
    :func:`resolve_target` (``shared_legacy_dir`` selects ``<legacy_dir>/unified/<family>``,
    section 4.1)."""
    entry = select_portal(site, portal_id)
    legacy = _get(entry, "legacy_dir")
    if not legacy:
        raise UsageError(f"portal {_get(entry, 'id')!r} has no legacy_dir")
    if target:
        tgt = Path(os.path.abspath(target))
    else:
        doc = read_backup(Path(os.path.abspath(legacy)), build_id)
        if doc is not None and doc.get("target"):
            tgt = Path(doc["target"])
        else:
            tgt = resolve_target(entry, family, shared_legacy_dir)
    return rollback(legacy, build_id, target=tgt, family=family, portal_id=_get(entry, "id"),
                    force=force, dry_run=dry_run)


def list_backups(legacy_dir: os.PathLike | str) -> list[str]:
    """Build ids with a backup directory under ``legacy_dir``, oldest first."""
    root = Path(legacy_dir) / BACKUP_DIRNAME
    try:
        return sorted(e.name for e in os.scandir(root) if e.is_dir(follow_symlinks=False))
    except FileNotFoundError:
        return []

