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
which needs only the ``FileInfo`` override that the cache-header directives already need, so
it adds no ``AllowOverride`` requirement: a ``Require`` rule would need ``AuthConfig``, and
Apache answers 500 for every file of a directory whose ``.htaccess`` uses an override it does
not allow. :func:`denied` applies the same pattern in Python (tests, dev server, verify).
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

#: ``<legacy_dir>/.portalkit_backup/.htaccess``: every path in the backup tree is denied.
BACKUP_HTACCESS_TEXT = MARKER + "\n" + DENY_BODY

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
        with open(p, encoding="utf-8", errors="replace") as fh:
            first = fh.readline().rstrip("\r\n")
    except OSError:
        return False
    return first == MARKER


def denied(rel: str) -> bool:
    """True when the portalkit ``.htaccess`` denies ``rel`` (a path below the directory that
    holds it, or a full URL path): deploy's manifests, backups and temporaries."""
    p = rel if rel.startswith("/") else "/" + rel
    return _DENY.search(p) is not None


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
        if path.read_text(encoding="utf-8") == text:
            return None
    if dry_run:
        return path
    atomic_write(path, text.encode("utf-8"))
    return path


def expected_cache_control(rel: str) -> str | None:
    """Cache-Control the .htaccess assigns to a file (``None`` when no rule matches)."""
    import re
    base = rel.rsplit("/", 1)[-1]
    if re.search(r"^(manifest|index|images|hips|overlays|narrative|portal)\.json$|\.html$", base):
        return NO_CACHE
    if re.search(r"\.[0-9a-f]{8}\.(bin|json|html)$", base):
        return IMMUTABLE
    return None
