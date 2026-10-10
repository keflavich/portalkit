"""Staging root and build ids (ARCHITECTURE.md sections 9.4, 10 and 11.1).

A staged portal lives at ``<stage_root>/<family>/<build_id>/<portal_id>/``. ``build_id`` is
``YYYYMMDDTHHMMSSZ-<8 hex of sha256(adapter sha + inputs)>``. ``build`` writes only under the
stage root, and refuses a stage root that the exposure classifier reports as served.
"""

from __future__ import annotations

import datetime as _dt
import fnmatch
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import DeployError, ExposureRefusal
from .exposure import ExposureMap, classify

#: Default stage root (section 10).
DEFAULT_STAGE_ROOT = Path("/orange/adamginsburg/portalkit_stage")

#: ``YYYYMMDDTHHMMSSZ-<8 hex>``
BUILD_ID_RE = re.compile(r"^(\d{8}T\d{6}Z)-([0-9a-f]{8})$")

#: Inputs under these globs are blocked unless ``--allow-tmp-inputs`` (section 4.1).
TMP_INPUT_GLOBS = ("/blue/*/tmp/*", "/tmp/*", "*/scratchpad/*")

#: File written by ``build`` next to ``portal.json`` (section 9.4 step 6).
BUILD_REPORT = "build_report.json"


def make_build_id(adapter_sha: str, inputs: Iterable[Any] = (),
                  when: _dt.datetime | None = None) -> str:
    """Return ``YYYYMMDDTHHMMSSZ-<h8>`` where ``h8`` hashes the adapter sha and the inputs.

    ``inputs`` may hold strings, paths or JSON-serialisable records (e.g. the
    ``{path, sha256, mtime, size}`` dicts of the input registry); order does not matter.
    """
    when = when or _dt.datetime.now(_dt.timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    stamp = when.astimezone(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    items = sorted(json.dumps(os.fspath(x) if isinstance(x, os.PathLike) else x,
                              sort_keys=True, default=str) for x in inputs)
    h = hashlib.sha256()
    h.update(str(adapter_sha).encode("utf-8"))
    for it in items:
        h.update(b"\n")
        h.update(it.encode("utf-8"))
    return f"{stamp}-{h.hexdigest()[:8]}"


def is_build_id(value: str) -> bool:
    return isinstance(value, str) and BUILD_ID_RE.match(value) is not None


def build_time(build_id: str) -> _dt.datetime:
    """UTC timestamp encoded in ``build_id``."""
    m = BUILD_ID_RE.match(build_id)
    if not m:
        raise ValueError(f"not a build id: {build_id!r}")
    return _dt.datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=_dt.timezone.utc)


def is_tmp_path(path: os.PathLike | str) -> bool:
    """True if ``path`` lies under ``/blue/*/tmp/``, ``/tmp/`` or ``*/scratchpad/*``."""
    p = os.path.abspath(os.fspath(path))
    candidates = {p, p + "/"}
    real = os.path.realpath(p)
    candidates |= {real, real + "/"}
    return any(fnmatch.fnmatchcase(c, g) for c in candidates for g in TMP_INPUT_GLOBS)


def stage_root(path: os.PathLike | str | None = None) -> Path:
    """The stage root: ``path``, else ``$PK_STAGE_ROOT``, else :data:`DEFAULT_STAGE_ROOT`."""
    if path is not None:
        return Path(path)
    env = os.environ.get("PK_STAGE_ROOT")
    return Path(env) if env else DEFAULT_STAGE_ROOT


def check_stage_root(root: os.PathLike | str, emap: ExposureMap | None = None) -> None:
    """Refuse (exit 3) a stage root that the web server can reach."""
    exp = classify(root, emap)
    if exp.served:
        raise ExposureRefusal(f"stage root {root} is served ({exp.kind})",
                              details=exp.public_urls + exp.secure_urls + exp.via)


def build_dir(root: os.PathLike | str, family: str, build_id: str) -> Path:
    if not is_build_id(build_id):
        raise DeployError(f"not a build id: {build_id!r}")
    return Path(root) / family / build_id


def portal_stage_dir(root: os.PathLike | str, family: str, build_id: str, portal_id: str) -> Path:
    """``<stage_root>/<family>/<build_id>/<portal_id>``."""
    return build_dir(root, family, build_id) / portal_id


def create_portal_stage_dir(root: os.PathLike | str, family: str, build_id: str,
                            portal_id: str, emap: ExposureMap | None = None) -> Path:
    """Check the stage root exposure, then create and return the portal staging dir."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    check_stage_root(root, emap)
    d = portal_stage_dir(root, family, build_id, portal_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_builds(root: os.PathLike | str, family: str) -> list[str]:
    """Build ids staged for ``family``, oldest first."""
    fam = Path(root) / family
    try:
        names = [e.name for e in os.scandir(fam) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return []
    return sorted(n for n in names if is_build_id(n))


def latest_build(root: os.PathLike | str, family: str, portal_id: str | None = None) -> str | None:
    """Newest staged build id (that contains ``portal_id`` when given)."""
    for bid in reversed(list_builds(root, family)):
        if portal_id is None or (Path(root) / family / bid / portal_id).is_dir():
            return bid
    return None


def read_build_report(portal_dir: os.PathLike | str) -> dict:
    """Load ``build_report.json`` from the portal staging dir (or its build dir); ``{}`` if absent."""
    p = Path(portal_dir)
    for cand in (p / BUILD_REPORT, p.parent / BUILD_REPORT):
        try:
            with open(cand, encoding="utf-8") as fh:
                obj = json.load(fh)
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            raise DeployError(f"cannot read {cand}: {exc}") from exc
        return obj if isinstance(obj, dict) else {}
    return {}


def build_provenance(report: Mapping) -> dict:
    """Normalise a build report into ``{adapter_sha, legacy_modules, portalkit,
    allow_tmp_inputs, inputs}``. Keys are looked up at the top level and under
    ``provenance``."""
    prov = report.get("provenance") if isinstance(report.get("provenance"), Mapping) else {}

    def pick(*keys: str, default: Any = None) -> Any:
        for k in keys:
            for src in (report, prov):
                if k in src and src[k] is not None:
                    return src[k]
        return default

    mods = pick("legacy_modules", default=[])
    inputs = pick("inputs", default=[])
    return {
        "adapter_sha": pick("adapter_sha", "adapter"),
        "legacy_modules": list(mods) if isinstance(mods, list) else [],
        "portalkit": pick("portalkit"),
        "allow_tmp_inputs": bool(pick("allow_tmp_inputs", default=False)),
        "inputs": list(inputs) if isinstance(inputs, list) else [],
    }


def tmp_inputs(prov: Mapping) -> list[str]:
    """Input paths recorded in ``prov`` that lie under a tmp glob."""
    out = []
    for it in prov.get("inputs", []):
        path = it.get("path") if isinstance(it, Mapping) else it
        if isinstance(path, str) and is_tmp_path(path):
            out.append(path)
    return out


def iter_stage_files(portal_dir: os.PathLike | str) -> tuple[list[str], list[str]]:
    """Return ``(files, symlinks)``: relative posix paths of regular files and of symlinks
    under the staged portal tree, sorted."""
    base = Path(portal_dir)
    files: list[str] = []
    links: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        rel_dir = os.path.relpath(dirpath, base)
        for name in list(dirnames):
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                links.append(_rel(rel_dir, name))
                dirnames.remove(name)
        for name in filenames:
            full = os.path.join(dirpath, name)
            (links if os.path.islink(full) else files).append(_rel(rel_dir, name))
    return sorted(files), sorted(links)


def _rel(rel_dir: str, name: str) -> str:
    return name if rel_dir in (".", "") else f"{rel_dir.replace(os.sep, '/')}/{name}"


def prune_stage(root: os.PathLike | str, family: str, keep: int = 5,
                protect: Iterable[str] = (), dry_run: bool = False) -> list[Path]:
    """Delete all but the newest ``keep`` staged builds of ``family``.

    Builds named in ``protect`` (e.g. the deployed ones) are never removed. Only directories
    whose name is a build id directly under ``<root>/<family>`` are touched. Returns the
    removed (or, with ``dry_run``, the would-be removed) directories.
    """
    if keep < 0:
        raise ValueError("keep must be >= 0")
    fam = Path(root) / family
    if fam.is_symlink():
        raise DeployError(f"refusing to prune through a symlink: {fam}")
    builds = list_builds(root, family)
    protect = set(protect)
    doomed = [b for b in (builds[:-keep] if keep else builds) if b not in protect]
    removed = []
    for bid in doomed:
        d = fam / bid
        if d.is_symlink():
            continue
        if not dry_run:
            shutil.rmtree(d)
        removed.append(d)
    return removed
