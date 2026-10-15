"""Ownership manifest and safe writes (ARCHITECTURE.md section 11.3), plus the ``--replace``
driver (section 11.4; the backup bookkeeping lives in :mod:`portalkit.deploy.backup`).

Write rules implemented here:

* portalkit writes only files listed in the ownership manifest
  (``<target>/.portalkit-deploy.json``) or new files at absent paths; any other existing file
  is an ownership conflict (exit 4);
* ``os.lstat`` runs on the target and on every path component below it; a symlink refuses
  (exit 3);
* every file goes to ``<name>.pk-tmp``, is ``fsync``-ed and moved with ``os.replace``;
  data files are written first (content-hashed files, then stable-name documents such as
  ``manifest.json``), then ``portal.json``, then HTML;
* superseded content-hashed files are removed only by :func:`gc_target` (``deploy --gc``)
  or by ``prune-stage``.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import posixpath
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote, urlsplit

from .. import schema
from ..version import VERSION
from . import (BlockedInput, DeployError, ExposureRefusal, OwnershipConflict, SymlinkRefusal,
               UsageError)
from .exposure import (Exposure, ExposureMap, PolicyDecision, Reference, check_references,
                       check_target, classify, relative_ref_path)
from .stage import (build_provenance, is_build_id, iter_stage_files, portal_stage_dir,
                    read_build_report, stage_root as _stage_root, tmp_inputs)

#: Ownership manifest file name (section 11.3).
MANIFEST_NAME = ".portalkit-deploy.json"
#: Secondary manifests for extra portals sharing a target: ``.portalkit-deploy.<portal>.json``.
MANIFEST_GLOB_PREFIX = ".portalkit-deploy"
#: Suffix of the temporary file each write goes through.
TMP_SUFFIX = ".pk-tmp"
#: Minimum age of an unreferenced owned file before ``--gc`` removes it (section 11.3).
GC_MIN_AGE_S = 7 * 86400
#: Stable-name documents (section 5.1); never garbage-collected, written after hashed data.
STABLE_NAMES = frozenset({"manifest.json", "index.json", "images.json", "hips.json",
                          "overlays.json", "narrative.json", "portal.json"})
#: Staged file names that are never deployed at any depth (build bookkeeping that keeps
#: full local paths; rendered pages and served data carry relative labels only).
NOT_DEPLOYED = frozenset({"build_report.json"})


def not_deployed(rel: str) -> bool:
    """True for a staged path deploy skips (:data:`NOT_DEPLOYED` names, tmp files)."""
    base = posixpath.basename(rel)
    return rel in NOT_DEPLOYED or base in NOT_DEPLOYED or base.endswith(TMP_SUFFIX)
#: Names a staged tree may not contain (they belong to deploy itself).
RESERVED_TOP = (".htaccess", ".portalkit_backup")

_CHUNK = 1 << 20


# ---------------------------------------------------------------------------
# low-level helpers
# ---------------------------------------------------------------------------

def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def iso(ts: _dt.datetime) -> str:
    return ts.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: os.PathLike | str) -> tuple[str, int]:
    """Return ``(sha256 hex, size)`` of a file."""
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(_CHUNK)
            if not buf:
                break
            h.update(buf)
            size += len(buf)
    return h.hexdigest(), size


def md5_file(path: os.PathLike | str) -> str:
    h = hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(_CHUNK)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        # some filesystems do not support fsync on directories
        pass
    finally:
        os.close(fd)


def atomic_write(path: os.PathLike | str, data: bytes | None = None, *,
                 src: os.PathLike | str | None = None, mode: int = 0o644) -> None:
    """Write ``data`` (or the bytes of ``src``) to ``path`` via ``<path>.pk-tmp``, fsync, then
    ``os.replace``. The temporary file is opened with ``O_NOFOLLOW`` so a planted symlink
    refuses instead of redirecting the write."""
    path = Path(path)
    if (data is None) == (src is None):
        raise ValueError("pass exactly one of data or src")
    tmp = path.with_name(path.name + TMP_SUFFIX)
    try:
        st = os.lstat(tmp)
    except FileNotFoundError:
        st = None
    if st is not None:
        if stat.S_ISLNK(st.st_mode):
            raise SymlinkRefusal(f"temporary path {tmp} is a symlink")
        os.unlink(tmp)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(tmp, flags, mode)
    except FileExistsError as exc:
        raise DeployError(f"concurrent write in progress at {tmp}") from exc
    try:
        try:
            os.fchmod(fd, mode)
            if data is not None:
                view = memoryview(data)
                while view:
                    n = os.write(fd, view)
                    view = view[n:]
            else:
                with open(src, "rb") as fin:
                    while True:
                        buf = fin.read(_CHUNK)
                        if not buf:
                            break
                        view = memoryview(buf)
                        while view:
                            n = os.write(fd, view)
                            view = view[n:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    _fsync_dir(path.parent)


def check_components(target: Path, rel: str = "") -> None:
    """``os.lstat`` the target and every component of ``rel`` below it; refuse symlinks.

    Components that do not exist yet end the walk (they will be created as directories)."""
    cur = Path(target)
    try:
        st = os.lstat(cur)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise SymlinkRefusal(f"deploy target {cur} is a symlink")
    if not rel:
        return
    for part in rel.split("/"):
        if part in ("", ".", ".."):
            raise DeployError(f"invalid path component in {rel!r}")
        cur = cur / part
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            return
        except NotADirectoryError as exc:
            raise OwnershipConflict("a file is in the way of a directory",
                                    [str(cur.parent)]) from exc
        if stat.S_ISLNK(st.st_mode):
            raise SymlinkRefusal(f"refusing to write through symlink {cur}",
                                 details=[f"{cur} -> {os.readlink(cur)}"])


def ensure_dirs(target: Path, rel_dir: str) -> None:
    """Create ``target/rel_dir`` component by component, lstat-checking each one."""
    check_components(target)
    if not target.exists():
        target.mkdir(parents=True, exist_ok=True)
    cur = target
    for part in [p for p in rel_dir.split("/") if p]:
        cur = cur / part
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            os.mkdir(cur, 0o755)
            os.chmod(cur, os.stat(cur).st_mode | 0o755)
            continue
        if stat.S_ISLNK(st.st_mode):
            raise SymlinkRefusal(f"refusing to write through symlink {cur}")
        if not stat.S_ISDIR(st.st_mode):
            raise OwnershipConflict("a file is in the way of a directory", [str(cur)])


def _dump_json(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, allow_nan=False, indent=1, sort_keys=True)
            + "\n").encode("utf-8")


def portalkit_version() -> str:
    """``<VERSION>+g<short git sha>`` when the package runs from a git checkout."""
    gitdir = Path(__file__).resolve().parents[2] / ".git"
    try:
        head = (gitdir / "HEAD").read_text().strip()
        if head.startswith("ref: "):
            ref = head[5:]
            refpath = gitdir / ref
            if refpath.exists():
                sha = refpath.read_text().strip()
            else:
                sha = ""
                for line in (gitdir / "packed-refs").read_text().splitlines():
                    if line.endswith(" " + ref):
                        sha = line.split()[0]
                        break
        else:
            sha = head
    except OSError:
        return VERSION
    return f"{VERSION}+g{sha[:8]}" if re.fullmatch(r"[0-9a-f]{40}", sha or "") else VERSION


# ---------------------------------------------------------------------------
# site / portal helpers
# ---------------------------------------------------------------------------

def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def legacy_entries(page: Any) -> list[tuple[str, str | None, bool]]:
    """``(file, preset, replace)`` for each legacy item of a ``[[portal.page]]`` entry.

    An item is a string or a ``{file, preset?, replace?}`` table (section 4.1); a
    :class:`~portalkit.context.PageConfig` contributes its ``files``. ``preset`` is ``None``
    when the item does not set one (the page preset then applies)."""
    items = _get(page, "files", None) if not isinstance(page, Mapping) else None
    if not items:
        items = _get(page, "legacy", []) or []
    out = []
    for it in items:
        if isinstance(it, str):
            out.append((it, None, True))
            continue
        f = _get(it, "file")
        if not isinstance(f, str) or not f:
            continue
        pre = _get(it, "preset")
        out.append((f, None if pre is None else str(pre), bool(_get(it, "replace", True))))
    return out


def select_portal(site: Any, portal_id: str | None = None) -> Mapping:
    """Return the ``[[portal]]`` entry ``portal_id`` of a parsed site registry (or ``site``
    itself when it already is a portal entry)."""
    portals = _get(site, "portal")
    if portals is None:
        if _get(site, "id") is not None and (portal_id is None or _get(site, "id") == portal_id):
            return site
        raise UsageError("site registry has no [[portal]] entries")
    if isinstance(portals, Mapping):
        portals = [portals]
    portals = list(portals)
    if portal_id is None:
        if len(portals) == 1:
            return portals[0]
        raise UsageError("family has several portals; pass --portal",
                         details=[str(_get(p, "id")) for p in portals])
    for p in portals:
        if _get(p, "id") == portal_id:
            return p
    raise UsageError(f"no portal {portal_id!r}", details=[str(_get(p, "id")) for p in portals])


def shared_legacy_dirs(sites: Iterable[Any] | Mapping) -> frozenset[str]:
    """Absolute ``legacy_dir`` paths that portals of two or more families use (section 4.1:
    each of those families deploys to ``<legacy_dir>/unified/<family>/``). ``sites`` are
    parsed site registries, e.g. ``status.load_sites()`` with no family."""
    if isinstance(sites, Mapping):
        sites = [sites]
    fams: dict[str, set] = {}
    for site in sites:
        fam = _get(site, "family")
        portals = _get(site, "portal") or []
        if isinstance(portals, Mapping):
            portals = [portals]
        for p in portals:
            ld = _get(p, "legacy_dir")
            if ld:
                fams.setdefault(os.path.abspath(ld), set()).add(fam)
    return frozenset(d for d, f in fams.items() if len(f) > 1)


def is_shared_legacy_dir(portal: Any, sites: Iterable[Any] | Mapping) -> bool:
    """True when ``portal``'s legacy_dir is shared with another family (see
    :func:`shared_legacy_dirs`); pass the result as ``shared_legacy_dir``."""
    ld = _get(portal, "legacy_dir")
    return bool(ld) and os.path.abspath(ld) in shared_legacy_dirs(sites)


def resolve_target(portal: Any, family: str | None = None, shared_legacy_dir: bool = False) -> Path:
    """``target_dir``, else ``<legacy_dir>/unified`` (``/<family>`` when families share it;
    callers compute ``shared_legacy_dir`` with :func:`is_shared_legacy_dir`)."""
    tgt = _get(portal, "target_dir")
    if tgt:
        return Path(os.path.abspath(tgt))
    legacy = _get(portal, "legacy_dir")
    if not legacy:
        raise UsageError(f"portal {_get(portal, 'id')!r} has neither target_dir nor legacy_dir")
    p = Path(os.path.abspath(legacy)) / "unified"
    if shared_legacy_dir:
        if not family:
            raise UsageError("family is required for a shared legacy_dir")
        p = p / family
    return p


# ---------------------------------------------------------------------------
# ownership manifests
# ---------------------------------------------------------------------------

def new_manifest(family: str, portal_id: str, target: Path) -> dict:
    return {"schema": "pk-deploy/1", "family": family, "portal": portal_id,
            "target": str(target), "builds": [], "files": {}, "replaced": []}


def load_manifest(path: os.PathLike | str) -> dict | None:
    """Load and validate an ownership manifest; ``None`` if absent. An unreadable or invalid
    manifest is an ownership conflict: ownership cannot be established."""
    path = Path(path)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode):
        raise SymlinkRefusal(f"ownership manifest {path} is a symlink")
    try:
        with open(path, encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, ValueError) as exc:
        raise OwnershipConflict(f"cannot read ownership manifest {path}: {exc}", [str(path)]) \
            from exc
    errs = schema.validate_deploy(obj)
    if errs:
        raise OwnershipConflict(f"invalid ownership manifest {path}", errs)
    obj.setdefault("replaced", [])
    return obj


def write_manifest(path: Path, man: dict) -> None:
    schema.assert_valid_deploy(man)
    atomic_write(path, _dump_json(man))


def all_manifests(target: Path) -> list[tuple[Path, dict]]:
    """Every ownership manifest in ``target`` (primary first)."""
    out = []
    try:
        names = sorted(os.listdir(target))
    except FileNotFoundError:
        return out
    names.sort(key=lambda n: (n != MANIFEST_NAME, n))
    for n in names:
        if n.startswith(MANIFEST_GLOB_PREFIX) and n.endswith(".json"):
            m = load_manifest(target / n)
            if m is not None:
                out.append((target / n, m))
    return out


def manifest_path_for(target: Path, family: str, portal_id: str) -> Path:
    """The manifest this (family, portal) owns in ``target``: the primary one unless another
    portal already holds it, in which case ``.portalkit-deploy.<portal>.json``."""
    for path, man in all_manifests(target):
        if man.get("family") == family and man.get("portal") == portal_id:
            return path
    primary = target / MANIFEST_NAME
    if not os.path.lexists(primary):
        return primary
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", portal_id):
        raise UsageError(f"portal id {portal_id!r} cannot name a secondary manifest")
    return target / f"{MANIFEST_GLOB_PREFIX}.{portal_id}.json"


def deployed_builds(man: Mapping) -> list[str]:
    """Builds of a manifest whose deploy completed, oldest first. A build whose ``--replace``
    was rolled back (note ``replace rolled back ...``) is still deployed: rollback restores
    the legacy files only, and the unified files stay served."""
    return [b["build_id"] for b in man.get("builds", [])
            if not str(b.get("note", "")).startswith(("in progress", "rolled back"))]


# ---------------------------------------------------------------------------
# reference collection (section 11.2 reference policy)
# ---------------------------------------------------------------------------

_PKPAGE_RE = re.compile(r'(<script\b[^>]*\bid\s*=\s*["\']pk-page["\'][^>]*>)(.*?)(</script\s*>)',
                        re.S | re.I)
# a start tag (quoted attribute values may contain ">"), then its attributes one by one, so
# every src/href/poster/data-src of a tag is seen and text inside a quoted value never is
_TAG_RE = re.compile(r'<(\w+)\b((?:[^>"\']|"[^"]*"|\'[^\']*\')*)>', re.S)
_ANY_ATTR_RE = re.compile(r'(\s+)([^\s"\'>/=]+)(?:(\s*=\s*)("[^"]*"|\'[^\']*\'|[^\s"\'>]+))?', re.S)
_URL_ATTRS = frozenset({"src", "href", "poster", "data-src"})


def _attr_value(raw: str) -> tuple[str, str]:
    """``(quote, value)`` of a raw attribute value."""
    if raw[:1] in ("'", '"'):
        return raw[0], raw[1:-1]
    return "", raw


def _iter_url_attrs(html: str) -> Iterable[tuple[str, str, str]]:
    """``(tag, attr, url)`` for every URL attribute of every start tag in ``html``."""
    for t in _TAG_RE.finditer(html):
        for m in _ANY_ATTR_RE.finditer(t.group(2)):
            name = m.group(2).lower()
            if name in _URL_ATTRS and m.group(4) is not None:
                yield t.group(1).lower(), name, _attr_value(m.group(4))[1]


_LINK_KEYS = frozenset({"links", "related", "href", "link", "legacy", "nav_links", "homepage"})


def _is_url_like(s: str) -> bool:
    return s.startswith(("http://", "https://", "//", "../", "./")) or \
        bool(re.match(r"^[\w.-]+/[\w./{}%-]+\.[A-Za-z0-9]{1,5}([?#].*)?$", s))


def _walk_refs(obj: Any, fetched: bool, out: list[tuple[str, bool]], subst: Mapping[str, str]) -> None:
    if isinstance(obj, Mapping):
        here = fetched and not obj.get("link_only") and "auth" not in obj
        for k, v in obj.items():
            _walk_refs(v, here and k not in _LINK_KEYS, out, subst)
    elif isinstance(obj, list):
        for v in obj:
            _walk_refs(v, fetched, out, subst)
    elif isinstance(obj, str):
        s = obj
        for key, val in subst.items():
            s = s.replace(key, val)
        if _is_url_like(s):
            out.append((s, fetched))


def extract_page_json(html: str) -> dict | None:
    m = _PKPAGE_RE.search(html)
    if not m:
        return None
    try:
        obj = json.loads(m.group(2))
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def collect_references(source: Path, files: Iterable[str]) -> list[tuple[str, str, bool]]:
    """URLs referenced by staged HTML pages (embedded page.json and src/href attributes) and
    JSON documents: ``[(source_rel, url, fetched)]``. A reference is a plain link (not
    fetched) when it sits under ``links``/``related``/``href``, in an object carrying
    ``link_only: true`` or an ``auth`` key (products and related portals), or in an
    ``<a href>``."""
    out: list[tuple[str, str, bool]] = []
    for rel in files:
        p = source / rel
        if rel.endswith(".html"):
            try:
                html = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            page = extract_page_json(html)
            if page is not None:
                subst = {}
                for k in ("res", "assets"):
                    if isinstance(page.get(k), str):
                        subst["{" + k + "}"] = page[k]
                refs: list[tuple[str, bool]] = []
                _walk_refs(page, True, refs, subst)
                out.extend((rel, u, f) for u, f in refs)
            body = _PKPAGE_RE.sub("", html)
            for tag, attr, url in _iter_url_attrs(body):
                if "{" in url or url.startswith("#") or url.startswith(("data:", "mailto:")):
                    continue
                fetched = not (tag == "a" and attr == "href")
                out.append((rel, url, fetched))
        elif rel.endswith(".json") and posixpath.basename(rel) != "portal.json":
            try:
                with open(p, encoding="utf-8") as fh:
                    doc = json.load(fh)
            except (OSError, ValueError):
                continue
            refs = []
            _walk_refs(doc, True, refs, {})
            out.extend((rel, u, f) for u, f in refs)
        elif posixpath.basename(rel) == "portal.json":
            try:
                with open(p, encoding="utf-8") as fh:
                    doc = json.load(fh)
            except (OSError, ValueError):
                continue
            refs = []
            _walk_refs(doc, False, refs, {})   # nav and related links only
            out.extend((rel, u, f) for u, f in refs)
    return out


def build_references(raw: Iterable[tuple[str, str, bool]], target: Path) -> list[Reference]:
    """Turn collected references into :class:`Reference` objects; relative URLs that stay
    inside the target are dropped, the others carry their resolved filesystem path."""
    refs = []
    tgt = os.path.abspath(target)
    for src, url, fetched in raw:
        parts = urlsplit(url)
        if parts.scheme or url.startswith("//"):
            if parts.scheme in ("http", "https") or url.startswith("//"):
                refs.append(Reference(url=url, source=src, fetched=fetched))
            continue
        if url.startswith("/"):
            # root-relative: resolved by the browser against the serving host, so it is
            # classified by URL (classify_url maps /pub/... and /secure/... prefixes)
            refs.append(Reference(url=url, source=src, fetched=fetched))
            continue
        base = os.path.join(tgt, posixpath.dirname(src))
        resolved = relative_ref_path(base, url)
        if resolved == tgt or resolved.startswith(tgt + os.sep):
            continue
        refs.append(Reference(url=url, source=src, fetched=fetched, resolved=resolved))
    return refs


# ---------------------------------------------------------------------------
# inplace rewrite
# ---------------------------------------------------------------------------

def _is_relative(u: str) -> bool:
    if not u or u.startswith(("#", "/", "{", "?")):
        return False
    return not urlsplit(u).scheme


def rewrite_relative_urls(html: str, prefix: str) -> str:
    """Prefix every relative URL of a unified page so it works from another directory
    (``--replace=inplace``). Rewrites ``src``/``href``/``poster``/``data-src`` attributes and,
    inside the embedded page.json, ``assets``, ``res``, ``portal.nav``, ``datasets.*.url``,
    ``links.*`` and a string ``readme``."""
    if not prefix:
        return html
    if not prefix.endswith("/"):
        prefix += "/"

    def fix(u: Any) -> Any:
        return prefix + u[2:] if isinstance(u, str) and u.startswith("./") else (
            prefix + u if isinstance(u, str) and _is_relative(u) else u)

    def page_sub(m: re.Match) -> str:
        try:
            page = json.loads(m.group(2))
        except ValueError:
            return m.group(0)
        if not isinstance(page, dict):
            return m.group(0)
        for k in ("assets", "res", "readme"):
            if k in page:
                page[k] = fix(page[k])
        if isinstance(page.get("portal"), dict) and "nav" in page["portal"]:
            page["portal"]["nav"] = fix(page["portal"]["nav"])
        for ds in (page.get("datasets") or {}).values():
            if isinstance(ds, dict) and "url" in ds:
                ds["url"] = fix(ds["url"])
        if isinstance(page.get("links"), dict):
            page["links"] = {k: fix(v) for k, v in page["links"].items()}
        text = json.dumps(page, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
        return m.group(1) + text + m.group(3)

    pieces = []
    last = 0
    for m in _PKPAGE_RE.finditer(html):
        pieces.append(_rewrite_attrs(html[last:m.start()], fix))
        pieces.append(page_sub(m))
        last = m.end()
    pieces.append(_rewrite_attrs(html[last:], fix))
    return "".join(pieces)


def _rewrite_attrs(chunk: str, fix: Callable[[str], str]) -> str:
    def attr_sub(m: re.Match) -> str:
        if m.group(2).lower() not in _URL_ATTRS or m.group(4) is None:
            return m.group(0)
        q, val = _attr_value(m.group(4))
        return m.group(1) + m.group(2) + m.group(3) + q + fix(val) + q

    def tag_sub(t: re.Match) -> str:
        return "<" + t.group(1) + _ANY_ATTR_RE.sub(attr_sub, t.group(2)) + ">"

    return _TAG_RE.sub(tag_sub, chunk)


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------

@dataclass
class FileAction:
    rel: str
    action: str            # create | update | same
    sha256: str
    size: int
    phase: str             # data | stable | portal | html


@dataclass
class ReplaceAction:
    page_id: str
    legacy: Path           # absolute legacy file
    legacy_rel: str        # relative to the target (manifest "legacy")
    legacy_file: str       # relative to legacy_dir (backup name)
    unified: str           # unified file, relative to the target
    url: str               # stub redirect URL relative to the legacy file
    mode: str              # stub | inplace
    preset: str = ""


@dataclass
class DeployPlan:
    family: str
    portal_id: str
    build_id: str
    source: Path
    target: Path
    visibility: str
    served_by: str | None
    manifest_path: Path
    exposure: Exposure
    decision: PolicyDecision
    ref_decision: PolicyDecision
    provenance: dict
    files: list[FileAction] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    replace: list[ReplaceAction] = field(default_factory=list)
    replace_mode: str | None = None
    legacy_dir: Path | None = None
    htaccess: bool = False
    #: write the deploy deny rule (``htaccess.write_deny_htaccess``) without ``--htaccess``:
    #: the target has no ``.htaccess`` or a portalkit one that is out of date
    htaccess_deny: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def writes(self) -> list[str]:
        return [f.rel for f in self.files if f.action != "same"]

    def summary(self) -> str:
        n = {a: sum(1 for f in self.files if f.action == a) for a in ("create", "update", "same")}
        lines = [f"deploy {self.family}/{self.portal_id} build {self.build_id}",
                 f"  source  {self.source}", f"  target  {self.target} ({self.exposure.kind})",
                 f"  files   {n['create']} new, {n['update']} updated, {n['same']} unchanged"]
        if self.replace:
            lines.append(f"  replace {len(self.replace)} legacy file(s), mode {self.replace_mode}")
        if self.htaccess:
            lines.append("  .htaccess")
        elif self.htaccess_deny:
            lines.append("  .htaccess (deny rule for deploy bookkeeping)")
        for w in self.warnings:
            lines.append(f"  warning: {w}")
        return "\n".join(lines)


@dataclass
class DeployResult:
    plan: DeployPlan
    written: list[str]
    manifest: dict | None
    replaced: list[dict]
    dry_run: bool
    gc_removed: list[str] = field(default_factory=list)
    exit_code: int = 0


def _phase(rel: str) -> tuple[int, str]:
    base = posixpath.basename(rel)
    if rel == "portal.json":
        return 2, "portal"
    if rel.endswith(".html"):
        return 3, "html"
    if base in STABLE_NAMES:
        return 1, "stable"
    return 0, "data"


def _staged_pages(source: Path) -> dict[str, str]:
    try:
        with open(source / "portal.json", encoding="utf-8") as fh:
            portal = json.load(fh)
    except (OSError, ValueError):
        return {}
    pages = portal.get("pages") if isinstance(portal, dict) else None
    out = {}
    for pg in pages or []:
        if isinstance(pg, dict) and isinstance(pg.get("id"), str) and isinstance(pg.get("file"), str):
            out[pg["id"]] = pg["file"]
    return out


def stub_url(legacy: Path, unified_abs: Path) -> str:
    rel = os.path.relpath(unified_abs, legacy.parent).replace(os.sep, "/")
    return quote(rel, safe="/-_.~")


def plan_deploy(family: str, build_id: str, portal: Any, *, portal_id: str | None = None,
                stage_root: os.PathLike | str | None = None, target: os.PathLike | str | None = None,
                replace: str | None = None, htaccess: bool = False, yes: bool = False,
                emap: ExposureMap | None = None, shared_legacy_dir: bool = False,
                deny_htaccess: bool | None = None) -> DeployPlan:
    """Compute what :func:`deploy` would do and every refusal, without touching the target.

    ``deny_htaccess`` (default: :func:`deny_htaccess_default`, i.e. on unless
    ``PK_DENY_HTACCESS`` is ``0``/``false``/``no``/``off``) controls whether a deploy without
    ``--htaccess`` writes the deny-only ``.htaccess``.

    Raises :class:`UsageError` for bad arguments; refusals are recorded on the plan and raised
    by :func:`raise_for_plan`."""
    if replace not in (None, "stub", "inplace"):
        raise UsageError(f"--replace must be stub or inplace, got {replace!r}")
    if not is_build_id(build_id):
        raise UsageError(f"not a build id: {build_id!r}")
    entry = select_portal(portal, portal_id)
    pid = _get(entry, "id")
    visibility = _get(entry, "visibility")
    served_by = _get(entry, "served_by")
    tgt = Path(os.path.abspath(target)) if target else resolve_target(entry, family,
                                                                      shared_legacy_dir)
    source = portal_stage_dir(_stage_root(stage_root), family, build_id, pid)
    if not source.is_dir():
        raise UsageError(f"no staged build at {source}")
    report = read_build_report(source)
    prov = build_provenance(report)
    exp = classify(tgt, emap)
    decision = check_target(visibility, exp, served_by, yes)

    files, links = iter_stage_files(source)
    warnings: list[str] = []
    if report.get("vendor_assets") is False:
        # verify checks only the deployed files, so a missing shared asset base goes unnoticed
        warnings.append("build is not vendored: its pages load web assets and shared "
                        "resources from the published asset base; run `portalkit "
                        "publish-assets` and `portalkit publish-res` first, or rebuild with "
                        "--vendor-assets")
    conflicts: list[str] = []
    if links:
        conflicts.extend(f"staged tree contains a symlink: {r}" for r in links)
    deployable = []
    for rel in files:
        base = posixpath.basename(rel)
        top = rel.split("/", 1)[0]
        if not_deployed(rel):
            continue
        if base.startswith(MANIFEST_GLOB_PREFIX) or top in RESERVED_TOP:
            conflicts.append(f"staged file uses a reserved name: {rel}")
            continue
        deployable.append(rel)

    raw_refs = collect_references(source, deployable)
    ref_decision = check_references(visibility, build_references(raw_refs, tgt), emap)
    if ref_decision.level == "warn":
        warnings.extend(ref_decision.notes)
    if decision.level == "warn" or (decision.level == "ok" and decision.message):
        warnings.append(decision.message)

    plan = DeployPlan(family=family, portal_id=pid, build_id=build_id, source=source, target=tgt,
                      visibility=visibility, served_by=served_by,
                      manifest_path=manifest_path_for(tgt, family, pid), exposure=exp,
                      decision=decision, ref_decision=ref_decision, provenance=prov,
                      replace_mode=replace, htaccess=htaccess, warnings=warnings)
    plan.conflicts.extend(conflicts)

    # ownership: ours = our manifest's files; foreign = files owned by other manifests
    ours: dict[str, dict] = {}
    foreign: set[str] = set()
    for path, man in all_manifests(tgt):
        if path == plan.manifest_path:
            ours = man.get("files", {})
        else:
            foreign.update(man.get("files", {}))

    for rel in sorted(deployable, key=lambda r: (_phase(r)[0], r)):
        src_sha, size = sha256_file(source / rel)
        dest = tgt / rel
        try:
            check_components(tgt, rel)
        except SymlinkRefusal as exc:
            plan.conflicts.append(f"symlink: {exc.message}")
            continue
        except OwnershipConflict as exc:
            plan.conflicts.append(f"{rel} ({exc.message}: {', '.join(exc.conflicts)})")
            continue
        action = "create"
        try:
            st = os.lstat(dest)
        except FileNotFoundError:
            st = None
        except NotADirectoryError:
            plan.conflicts.append(f"a file is in the way of {rel}")
            continue
        if st is not None:
            if not stat.S_ISREG(st.st_mode):
                plan.conflicts.append(f"not a regular file: {rel}")
                continue
            if rel in foreign or rel not in ours:
                plan.conflicts.append(rel)
                continue
            disk_sha, _ = sha256_file(dest)
            if disk_sha != ours[rel].get("sha256"):
                warnings.append(f"owned file {rel} changed since the last deploy; overwriting")
            action = "same" if disk_sha == src_sha else "update"
        elif rel in foreign:
            plan.conflicts.append(rel)
            continue
        plan.files.append(FileAction(rel=rel, action=action, sha256=src_sha, size=size,
                                     phase=_phase(rel)[1]))

    _plan_htaccess(plan, tgt, htaccess,
                   deny_htaccess_default() if deny_htaccess is None else deny_htaccess)

    if replace:
        _plan_replace(plan, entry, tgt, source)
    return plan


#: Environment variable that turns off the deny-only ``.htaccess`` of a plain deploy.
DENY_HTACCESS_ENV = "PK_DENY_HTACCESS"


def deny_htaccess_default() -> bool:
    """Whether a deploy without ``--htaccess`` writes the deny-only ``.htaccess``: True
    unless ``$PK_DENY_HTACCESS`` is ``0``, ``false``, ``no`` or ``off``. The file needs
    ``AllowOverride`` with ``FileInfo`` (or ``None``); docs/deploy.md ".htaccess"."""
    val = os.environ.get(DENY_HTACCESS_ENV, "").strip().lower()
    return val not in ("0", "false", "no", "off")


def _plan_htaccess(plan: DeployPlan, tgt: Path, htaccess: bool, deny: bool = True) -> None:
    """``.htaccess`` part of the plan. With ``--htaccess`` a symlink (exit 3) or a foreign
    file (exit 4) is a refusal. Without it the deny rule for deploy's bookkeeping files
    (docs/deploy.md ".htaccess") is planned when the target has no ``.htaccess`` or an
    outdated portalkit one; a foreign file or a symlink is left alone with a warning. With
    ``deny`` False (``PK_DENY_HTACCESS=0``) the file is left as it is."""
    from .htaccess import DENY_RE, htaccess_state, managed_text
    state = htaccess_state(tgt)
    hp = tgt / ".htaccess"
    if htaccess:
        if state == "symlink":
            plan.conflicts.append("symlink: .htaccess is a symlink")
        elif state == "foreign":
            plan.conflicts.append(".htaccess (not written by portalkit)")
        return
    if not deny:
        if state == "absent":
            plan.warnings.append(
                f"{DENY_HTACCESS_ENV}=0: no .htaccess deny rule is written, so the ownership "
                f"manifest {plan.manifest_path.name} (absolute paths) is served")
        return
    if state in ("symlink", "foreign"):
        what = "is a symlink" if state == "symlink" else "was not written by portalkit"
        plan.warnings.append(
            f"{hp} {what} and is left unchanged; deploy's bookkeeping files (the ownership "
            f"manifest {plan.manifest_path.name}) may be served unless it denies them "
            f"(RedirectMatch 403 \"{DENY_RE}\")")
        return
    if state == "absent":
        plan.htaccess_deny = True
    else:
        plan.htaccess_deny = hp.read_text(encoding="utf-8", errors="replace") != managed_text(tgt)


def _plan_backup_htaccess(plan: DeployPlan, ldir: Path) -> None:
    """Refusals :func:`~portalkit.deploy.backup.backup_files` would meet when it writes
    ``<legacy_dir>/.portalkit_backup/.htaccess``, found before anything is written."""
    from .backup import BACKUP_DIRNAME
    from .htaccess import htaccess_state
    bdir = ldir / BACKUP_DIRNAME
    try:
        st = os.lstat(bdir)
    except (FileNotFoundError, NotADirectoryError):
        return
    if stat.S_ISLNK(st.st_mode):
        plan.conflicts.append(f"symlink: backup directory {bdir} is a symlink")
        return
    if not stat.S_ISDIR(st.st_mode):
        plan.conflicts.append(f"a file is in the way of the backup directory {bdir}")
        return
    state = htaccess_state(bdir)
    if state == "symlink":
        plan.conflicts.append(f"symlink: {bdir / '.htaccess'} is a symlink")
    elif state == "foreign":
        plan.conflicts.append(f"{bdir / '.htaccess'} (not written by portalkit)")


def _plan_replace(plan: DeployPlan, entry: Any, tgt: Path, source: Path) -> None:
    legacy_dir = _get(entry, "legacy_dir")
    if not legacy_dir:
        raise UsageError(f"--replace needs legacy_dir for portal {plan.portal_id!r}")
    ldir = Path(os.path.abspath(legacy_dir))
    plan.legacy_dir = ldir
    _plan_backup_htaccess(plan, ldir)
    staged_pages = _staged_pages(source)
    staged_files = {f.rel for f in plan.files}
    for pg in _get(entry, "page", []) or []:
        pid = _get(pg, "id")
        legacy = legacy_entries(pg)
        kept = [lf for lf, _, rep in legacy if not rep]
        if kept:
            plan.warnings.append(f"page {pid!r}: replace=false keeps {', '.join(kept)} in place")
        legacy = [(lf, pre) for lf, pre, rep in legacy if rep]
        if not legacy:
            continue
        unified = staged_pages.get(pid)
        if unified is None or unified not in staged_files:
            plan.warnings.append(f"page {pid!r} was not built; its legacy files stay in place")
            continue
        page_preset = _get(pg, "preset", "") or ""
        for lf, file_preset in legacy:
            lrel = posixpath.normpath(lf)
            if lrel.startswith("../") or lrel.startswith("/") or lrel == "..":
                plan.conflicts.append(f"legacy path escapes legacy_dir: {lf}")
                continue
            lpath = ldir / lrel
            if os.path.abspath(lpath).startswith(os.path.abspath(tgt) + os.sep):
                plan.conflicts.append(f"legacy file lies inside the target: {lf}")
                continue
            try:
                st = os.lstat(lpath)
            except FileNotFoundError:
                plan.warnings.append(f"legacy file {lpath} is missing; no stub written")
                continue
            if stat.S_ISLNK(st.st_mode):
                plan.conflicts.append(f"symlink: legacy file {lpath} is a symlink")
                continue
            if not stat.S_ISREG(st.st_mode):
                plan.conflicts.append(f"legacy path {lpath} is not a regular file")
                continue
            plan.replace.append(ReplaceAction(
                page_id=pid, legacy=lpath,
                legacy_rel=os.path.relpath(lpath, tgt).replace(os.sep, "/"),
                legacy_file=lrel, unified=unified, url=stub_url(lpath, tgt / unified),
                mode=plan.replace_mode or "stub",
                preset=page_preset if file_preset is None else file_preset))


def raise_for_plan(plan: DeployPlan) -> None:
    """Raise the refusal a plan carries, in exit-code priority: 5, 3, 4."""
    prov = plan.provenance
    if prov.get("allow_tmp_inputs") or tmp_inputs(prov):
        err = BlockedInput("build was made with --allow-tmp-inputs; deploy refused",
                           details=tmp_inputs(prov))
        err.plan = plan
        raise err
    if not plan.decision.ok:
        err = ExposureRefusal(plan.decision.message, details=plan.decision.notes)
        err.plan = plan
        raise err
    if not plan.ref_decision.ok:
        err = ExposureRefusal(plan.ref_decision.message, details=plan.ref_decision.notes)
        err.plan = plan
        raise err
    links = [c for c in plan.conflicts if c.startswith("symlink: ")]
    if links:
        err = SymlinkRefusal("refusing to write through symlinks", details=links)
        err.plan = plan
        raise err
    if plan.conflicts:
        err = OwnershipConflict(f"{len(plan.conflicts)} path(s) in {plan.target} are not owned "
                                "by this portal", plan.conflicts)
        err.plan = plan
        raise err


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------

def deploy(family: str, build_id: str, portal: Any, *, portal_id: str | None = None,
           stage_root: os.PathLike | str | None = None, target: os.PathLike | str | None = None,
           replace: str | None = None, htaccess: bool = False, dry_run: bool = False,
           yes: bool = False, emap: ExposureMap | None = None, gc: bool = False,
           shared_legacy_dir: bool = False, now: _dt.datetime | None = None,
           log: Callable[[str], None] | None = None,
           deny_htaccess: bool | None = None) -> DeployResult:
    """Deploy staged build ``build_id`` of ``family`` to the portal's target.

    ``portal`` is the parsed site registry (``sites/<family>.toml``) or one ``[[portal]]``
    entry. Refusals raise :class:`~portalkit.deploy.DeployError` subclasses whose
    ``exit_code`` follows section 10 (the exception carries the plan as ``.plan``); a
    dry run raises the same refusals and writes nothing. ``deny_htaccess`` is passed to
    :func:`plan_deploy`.
    """
    log = log or (lambda s: None)
    now = now or utcnow()
    plan = plan_deploy(family, build_id, portal, portal_id=portal_id, stage_root=stage_root,
                       target=target, replace=replace, htaccess=htaccess, yes=yes, emap=emap,
                       shared_legacy_dir=shared_legacy_dir, deny_htaccess=deny_htaccess)
    raise_for_plan(plan)
    if dry_run:
        # the summary lists the warnings
        log(plan.summary())
        return DeployResult(plan=plan, written=[], manifest=None, replaced=[], dry_run=True)
    for w in plan.warnings:
        log(f"warning: {w}")

    tgt = plan.target
    ensure_dirs(tgt, "")
    # the deny rule goes in before the first manifest write, so the manifest is never served
    # unprotected where the host honours the .htaccess
    if htaccess:
        from .htaccess import write_htaccess
        write_htaccess(tgt)
    elif plan.htaccess_deny:
        from .htaccess import write_deny_htaccess
        write_deny_htaccess(tgt)
    man = load_manifest(plan.manifest_path) or new_manifest(family, plan.portal_id, tgt)
    man["target"] = str(tgt)
    prov = plan.provenance
    bentry: dict[str, Any] = {"build_id": build_id, "deployed": iso(now),
                              "portalkit": prov.get("portalkit") or portalkit_version(),
                              "note": "in progress"}
    if prov.get("adapter_sha"):
        bentry["adapter_sha"] = str(prov["adapter_sha"])
    bentry["legacy_modules"] = list(prov.get("legacy_modules") or [])
    if replace:
        bentry["replace"] = replace
    man["builds"] = [b for b in man.get("builds", []) if b.get("build_id") != build_id] + [bentry]
    for fa in plan.files:
        man["files"][fa.rel] = {"sha256": fa.sha256, "size": fa.size, "build_id": build_id}
    write_manifest(plan.manifest_path, man)   # pre-register ownership before any write

    written: list[str] = []
    for fa in plan.files:
        if fa.action == "same":
            continue
        rel_dir = posixpath.dirname(fa.rel)
        ensure_dirs(tgt, rel_dir)
        check_components(tgt, fa.rel)
        atomic_write(tgt / fa.rel, src=plan.source / fa.rel)
        written.append(fa.rel)
    log(f"wrote {len(written)} file(s) to {tgt}")

    replaced: list[dict] = []
    if plan.replace:
        replaced = _execute_replace(plan, now)
        man["replaced"] = [r for r in man.get("replaced", [])
                           if not (r.get("build_id") == build_id
                                   and r["legacy"] in {x["legacy"] for x in replaced})] + replaced
        log(f"replaced {len(replaced)} legacy file(s) ({plan.replace_mode})")

    bentry.pop("note", None)
    write_manifest(plan.manifest_path, man)
    result = DeployResult(plan=plan, written=written, manifest=man, replaced=replaced,
                          dry_run=False)
    if gc:
        result.gc_removed = gc_target(tgt, family, plan.portal_id, now=now)
        result.manifest = load_manifest(plan.manifest_path)
    return result


def _execute_replace(plan: DeployPlan, now: _dt.datetime) -> list[dict]:
    from .backup import backup_files, stub_html
    assert plan.legacy_dir is not None
    contents: dict[str, bytes] = {}
    entries = []
    for ra in plan.replace:
        if ra.mode == "stub":
            data = stub_html(ra.url, ra.preset).encode("utf-8")
        else:
            prefix = os.path.relpath(plan.target, ra.legacy.parent).replace(os.sep, "/")
            html = (plan.target / ra.unified).read_text(encoding="utf-8")
            data = rewrite_relative_urls(html, quote(prefix, safe="/-_.~")).encode("utf-8")
        contents[ra.legacy_file] = data
        entries.append({"file": ra.legacy_file, "mode": ra.mode, "page": ra.page_id,
                        "unified": os.path.relpath(plan.target / ra.unified,
                                                   plan.legacy_dir).replace(os.sep, "/"),
                        "written_sha256": hashlib.sha256(data).hexdigest()})
    recorded = backup_files(plan.legacy_dir, plan.build_id, entries, target=plan.target, now=now)
    by_file = {e["file"]: e for e in recorded}
    out = []
    for ra in plan.replace:
        rec = by_file[ra.legacy_file]
        check_components(plan.legacy_dir, ra.legacy_file)
        atomic_write(ra.legacy, contents[ra.legacy_file])
        out.append({"legacy": ra.legacy_rel, "backup": rec["backup"], "md5": rec["md5"],
                    "mode": ra.mode, "size": rec["size"], "build_id": plan.build_id})
    return out


# ---------------------------------------------------------------------------
# garbage collection (deploy --gc)
# ---------------------------------------------------------------------------

def gc_target(target: os.PathLike | str, family: str, portal_id: str, *,
              now: _dt.datetime | None = None, min_age_s: float = GC_MIN_AGE_S,
              dry_run: bool = False) -> list[str]:
    """Remove manifest-owned files that no deployed page references and that are older than
    ``min_age_s`` (7 days). HTML pages, stable-name documents and every file of the latest
    deployed build are always kept. Returns the removed relative paths."""
    tgt = Path(target)
    mpath = manifest_path_for(tgt, family, portal_id)
    man = load_manifest(mpath)
    if man is None:
        return []
    now = now or utcnow()
    files: dict[str, dict] = man["files"]
    done = deployed_builds(man)
    current = done[-1] if done else None
    keep = {r for r, v in files.items()
            if r.endswith(".html") or posixpath.basename(r) in STABLE_NAMES
            or v.get("build_id") == current}
    texts: dict[str, str] = {}

    def text(rel: str) -> str:
        if rel not in texts:
            try:
                texts[rel] = (tgt / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                texts[rel] = ""
        return texts[rel]

    reached = set(r for r in keep if r.endswith((".html", ".json")))
    frontier = list(reached)
    referenced = set(keep)
    while frontier:
        doc = frontier.pop()
        body = text(doc)
        for rel in files:
            if rel in referenced:
                continue
            if posixpath.basename(rel) in body:
                referenced.add(rel)
                if rel.endswith((".json", ".html")) and rel not in reached:
                    reached.add(rel)
                    frontier.append(rel)
    removed = []
    changed = False
    for rel in sorted(set(files) - referenced):
        p = tgt / rel
        try:
            check_components(tgt, rel)
            st = os.lstat(p)
        except FileNotFoundError:
            if not dry_run:
                files.pop(rel, None)
                changed = True
            continue
        if now.timestamp() - st.st_mtime < min_age_s:
            continue
        if not dry_run:
            os.unlink(p)
            files.pop(rel, None)
            changed = True
        removed.append(rel)
    if changed:
        write_manifest(mpath, man)
    return removed


# ---------------------------------------------------------------------------
# immutable publishes (publish-assets / publish-res, section 11.3)
# ---------------------------------------------------------------------------

_SEMVER_RE = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?")


@dataclass
class PublishResult:
    dest: Path
    created: list[str] = field(default_factory=list)
    same: list[str] = field(default_factory=list)
    dry_run: bool = False


def publish_immutable(src_dir: os.PathLike | str, dest_dir: os.PathLike | str, *,
                      dry_run: bool = False, emap: ExposureMap | None = None) -> PublishResult:
    """Copy every file of ``src_dir`` to ``dest_dir`` without ever changing published bytes
    (section 11.3, ``publish-assets`` and ``publish-res``).

    Files already present with identical bytes are skipped; new files are written with
    :func:`atomic_write`. When any existing file differs, nothing is written and
    :class:`OwnershipConflict` (exit 4) lists them. Every path component below ``dest_dir``
    is lstat-checked (a symlink refuses, exit 3), and symlinks inside ``src_dir`` are
    refused too. With ``emap`` given, ``dest_dir`` must be publicly served (exit 3
    otherwise): shared assets are fetched by public pages."""
    src = Path(os.path.abspath(src_dir))
    dest = Path(os.path.abspath(dest_dir))
    if not src.is_dir():
        raise UsageError(f"no such directory: {src}")
    if emap is not None:
        exp = classify(dest, emap)
        if exp.kind not in ("public", "both"):
            raise ExposureRefusal(f"publish destination {dest} is {exp.kind}, not public")
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        rel_dir = os.path.relpath(dirpath, src)
        for name in sorted(dirnames):
            if os.path.islink(os.path.join(dirpath, name)):
                raise SymlinkRefusal(f"source {os.path.join(dirpath, name)} is a symlink")
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                raise SymlinkRefusal(f"source {full} is a symlink")
            if name.endswith(TMP_SUFFIX):
                continue
            files.append(name if rel_dir == "." else posixpath.join(
                *rel_dir.split(os.sep), name))
    res = PublishResult(dest=dest, dry_run=dry_run)
    conflicts = []
    for rel in files:
        check_components(dest, rel)
        p = dest / rel
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            res.created.append(rel)
            continue
        if not stat.S_ISREG(st.st_mode):
            conflicts.append(f"{p} exists and is not a regular file")
        elif sha256_file(p)[0] != sha256_file(src / rel)[0]:
            conflicts.append(f"{p} exists with different bytes")
        else:
            res.same.append(rel)
    if conflicts:
        raise OwnershipConflict(f"refusing to change published bytes under {dest} "
                                "(publish a new version instead)", conflicts)
    if dry_run:
        return res
    for rel in res.created:
        ensure_dirs(dest, posixpath.dirname(rel))
        atomic_write(dest / rel, src=src / rel, mode=0o644)
    return res


def publish_assets(src_dir: os.PathLike | str, dest_root: os.PathLike | str, version: str, *,
                   dry_run: bool = False, emap: ExposureMap | None = None) -> PublishResult:
    """``portalkit publish-assets``: :func:`publish_immutable` into ``<dest_root>/v<version>/``
    (``version`` is a semver such as ``1.0.0``)."""
    if not _SEMVER_RE.fullmatch(version):
        raise UsageError(f"version {version!r} is not X.Y.Z")
    return publish_immutable(src_dir, Path(dest_root) / f"v{version}", dry_run=dry_run,
                             emap=emap)
