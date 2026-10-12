"""Optional cache headers (ARCHITECTURE.md section 11.5, ``deploy --htaccess``).

``<target>/.htaccess`` is written only when no ``.htaccess`` exists there or the existing one
was written by portalkit (it starts with :data:`MARKER`). Apache may ignore the file
(``AllowOverride``); content hashing gives correct caching either way, and ``verify --http``
reports whether the headers took effect.

The file is not listed in the ownership manifest's ``files`` because Apache refuses to serve
``.ht*`` files, so ``verify --http`` could never fetch it; the marker line records ownership.

Besides the section 11.5 cache headers, the file denies (HTTP 403) deploy's own dot-files in
the target: the ownership manifests ``.portalkit-deploy.json`` and
``.portalkit-deploy.<portal>.json`` (they keep the absolute ``target`` and ``legacy_modules``
paths ``status`` needs; ARCHITECTURE section 9.3 "Served paths"), anything under a
``.portalkit_backup/`` directory (``backup.json`` records absolute paths too) and in-flight
``*.pk-tmp`` temporaries (:data:`DENY_RE`). The rule is a ``RedirectMatch 403`` (mod_alias),
which belongs to the ``FileInfo`` override class, the class the cache-header directives
already need, so it adds nothing to what ``--htaccess`` requires. A ``Require`` rule would
need ``AuthConfig`` instead. :func:`denied` applies the same pattern in Python (tests, dev
server, verify).

Deploy writes the deny rule even without ``--htaccess``: a target with no ``.htaccess`` gets
the minimal managed file :data:`DENY_HTACCESS_TEXT` (marker and deny rule only;
:func:`write_deny_htaccess`), an owned file is brought up to date, and a foreign file is left
alone with a warning (with ``--htaccess`` it stays an ownership conflict, exit 4). So a plain
deploy needs ``AllowOverride`` to include ``FileInfo``, or to be ``None`` (Apache then
ignores the file). Under an override list without ``FileInfo`` (for example
``AuthConfig Options Indexes`` only) Apache answers 500 for every file of the target;
``<IfModule>`` does not prevent this. Which classes the host allows is open (ARCHITECTURE
section 16, Q4). ``PK_DENY_HTACCESS=0`` (or ``deploy(..., deny_htaccess=False)``) turns the
deny-only file off; ``verify --http`` reports 5xx answers from a directory holding a
portalkit ``.htaccess`` as a probable ``AllowOverride`` problem.
:func:`write_backup_htaccess` writes the deny rule into ``<legacy_dir>/.portalkit_backup/``,
which lies outside the target when the target is ``<legacy_dir>/unified``.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from . import OwnershipConflict, SymlinkRefusal
from .deploy import atomic_write, check_components

#: First line of every .htaccess portalkit writes.
MARKER = "# portalkit: managed file (pk-htaccess/1); portalkit deploy --htaccess rewrites it"

#: The section 11.5 block, verbatim.
HTACCESS_BODY = """<IfModule mod_headers.c>
  <FilesMatch "\\.[0-9a-f]{8}\\.(bin|json|html)$">
    Header set Cache-Control "public, max-age=31536000, immutable"
  </FilesMatch>
  <FilesMatch "^(manifest|index|images|hips|overlays|narrative|portal)\\.json$|\\.html$">
    Header set Cache-Control "no-cache"
  </FilesMatch>
</IfModule>
AddType application/octet-stream .bin
"""

#: URL-path pattern of deploy's own files, matched against ``"/" + <path below the target>``
#: (or the full URL path): ownership manifests and backups (any path segment starting with
#: ``.portalkit``) and ``.pk-tmp`` temporaries.
DENY_RE = r"/\.portalkit[^/]*(/|$)|\.pk-tmp$"

#: Deny block appended to the section 11.5 block (mod_alias; ``FileInfo`` override only).
DENY_BODY = f"""# deploy bookkeeping (ownership manifests, backups, temporaries) is never served
<IfModule mod_alias.c>
  RedirectMatch 403 "{DENY_RE}"
</IfModule>
"""

HTACCESS_TEXT = MARKER + "\n" + HTACCESS_BODY + DENY_BODY

#: The minimal managed file: the marker and the deny rule only. Deploy writes it to a target
#: without ``--htaccess`` (when the target has no foreign ``.htaccess``), so the ownership
#: manifest is denied whether or not the cache headers were asked for.
DENY_HTACCESS_TEXT = MARKER + "\n" + DENY_BODY

#: ``<legacy_dir>/.portalkit_backup/.htaccess``: every path in the backup tree is denied.
BACKUP_HTACCESS_TEXT = DENY_HTACCESS_TEXT

_DENY = re.compile(DENY_RE)

#: Expected Cache-Control per file class, used by ``verify``.
IMMUTABLE = "public, max-age=31536000, immutable"
NO_CACHE = "no-cache"


def htaccess_owned(path: os.PathLike | str) -> bool:
    """True when ``path`` is a regular file whose first line is :data:`MARKER`."""
    p = Path(path)
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    try:
        with open(p, "rb") as fh:
            first = fh.readline(len(MARKER) + 3).rstrip(b"\r\n")
    except OSError:
        return False
    # bytes, like _write_owned: an owned file with undecodable bytes stays owned
    return first == MARKER.encode("utf-8")


def denied(rel: str) -> bool:
    """True when the portalkit ``.htaccess`` denies ``rel`` (a path below the directory that
    holds it, or a full URL path): deploy's manifests, backups and temporaries."""
    p = rel if rel.startswith("/") else "/" + rel
    return _DENY.search(p) is not None


def _owned_bytes(path: Path) -> bytes | None:
    """Raw content of ``path`` when it is a portalkit-owned regular file, else ``None``."""
    if not htaccess_owned(path):
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def _owned_text(path: Path) -> str | None:
    """Content of ``path`` when it is a portalkit-owned regular file, else ``None``.
    Undecodable bytes become U+FFFD (the managed blocks are ASCII, so a damaged file reads
    as an older owned variant and is rewritten)."""
    data = _owned_bytes(path)
    return None if data is None else data.decode("utf-8", errors="replace")


def htaccess_state(target: os.PathLike | str) -> str:
    """State of ``<target>/.htaccess``: ``"absent"``, ``"symlink"``, ``"foreign"`` (no
    marker, or not a regular file), ``"cache"`` (portalkit's, with the section 11.5 cache
    headers) or ``"deny"`` (portalkit's, deny rule only or an older owned variant)."""
    path = Path(target) / ".htaccess"
    try:
        st = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return "absent"
    if stat.S_ISLNK(st.st_mode):
        return "symlink"
    text = _owned_text(path)
    if text is None:
        return "foreign"
    return "cache" if HTACCESS_BODY in text else "deny"


def has_deny_rule(target: os.PathLike | str) -> bool:
    """True when ``<target>/.htaccess`` is portalkit's and carries the :data:`DENY_BODY`
    rule (used by ``verify --http``)."""
    text = _owned_text(Path(target) / ".htaccess")
    return text is not None and DENY_BODY in text


def has_cache_headers(target: os.PathLike | str) -> bool:
    """True when ``<target>/.htaccess`` is portalkit's and carries the section 11.5 cache
    headers (``deploy --htaccess``); the minimal deny-only file does not."""
    text = _owned_text(Path(target) / ".htaccess")
    return text is not None and HTACCESS_BODY in text


def managed_text(target: os.PathLike | str) -> str:
    """The text deploy keeps in ``<target>/.htaccess`` without ``--htaccess``: the full
    :data:`HTACCESS_TEXT` when the owned file already holds the cache headers (an earlier
    ``--htaccess`` deploy), else :data:`DENY_HTACCESS_TEXT`."""
    return HTACCESS_TEXT if htaccess_state(target) == "cache" else DENY_HTACCESS_TEXT


def write_deny_htaccess(target: os.PathLike | str, *, dry_run: bool = False) -> Path | None:
    """Make ``<target>/.htaccess`` carry the deploy deny rule without ``--htaccess``: write
    :data:`DENY_HTACCESS_TEXT` when there is no file, and bring a portalkit-owned file up to
    date (:func:`managed_text`; cache headers an earlier ``--htaccess`` wrote are kept).
    Refuses (exit 4) a foreign file and (exit 3) a symlink like :func:`write_htaccess`;
    ``deploy`` checks for both first and only warns. Returns the path written, or ``None``
    when the file already has the content."""
    return _write_owned(Path(target), managed_text(target), dry_run=dry_run)


def write_htaccess(target: os.PathLike | str, *, dry_run: bool = False) -> Path | None:
    """Write ``<target>/.htaccess``; refuse (exit 4) a foreign one and (exit 3) a symlink.

    Returns the path written, or ``None`` when the existing file already has the content."""
    return _write_owned(Path(target), HTACCESS_TEXT, dry_run=dry_run)


def write_backup_htaccess(legacy_dir: os.PathLike | str, *,
                          dry_run: bool = False) -> Path | None:
    """Write ``<legacy_dir>/.portalkit_backup/.htaccess`` (deny everything below it), with the
    same ownership rules as :func:`write_htaccess`. Returns ``None`` when the backup
    directory does not exist or the file already has the content."""
    from .backup import BACKUP_DIRNAME
    ldir = Path(legacy_dir)
    check_components(ldir, BACKUP_DIRNAME)
    bdir = ldir / BACKUP_DIRNAME
    if not bdir.is_dir():
        return None
    return _write_owned(bdir, BACKUP_HTACCESS_TEXT, dry_run=dry_run)


def _write_owned(tgt: Path, text: str, *, dry_run: bool) -> Path | None:
    check_components(tgt)
    path = tgt / ".htaccess"
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        st = None
    if st is not None:
        if stat.S_ISLNK(st.st_mode):
            raise SymlinkRefusal(f"{path} is a symlink")
        if not htaccess_owned(path):
            raise OwnershipConflict(".htaccess exists and was not written by portalkit",
                                    [str(path)])
        # bytes (same reader as htaccess_owned/_owned_text), so an owned file with
        # undecodable bytes is rewritten instead of raising
        if _owned_bytes(path) == text.encode("utf-8"):
            return None
    if dry_run:
        return path
    atomic_write(path, text.encode("utf-8"))
    return path


def expected_cache_control(rel: str) -> str | None:
    """Cache-Control the .htaccess assigns to a file (``None`` when no rule matches)."""
    base = rel.rsplit("/", 1)[-1]
    if re.search(r"^(manifest|index|images|hips|overlays|narrative|portal)\.json$|\.html$", base):
        return NO_CACHE
    if re.search(r"\.[0-9a-f]{8}\.(bin|json|html)$", base):
        return IMMUTABLE
    return None
