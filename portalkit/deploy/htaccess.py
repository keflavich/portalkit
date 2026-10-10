"""Optional cache headers (ARCHITECTURE.md section 11.5, ``deploy --htaccess``).

``<target>/.htaccess`` is written only when no ``.htaccess`` exists there or the existing one
was written by portalkit (it starts with :data:`MARKER`). Apache may ignore the file
(``AllowOverride``); content hashing gives correct caching either way, and ``verify --http``
reports whether the headers took effect.

The file is not listed in the ownership manifest's ``files`` because Apache refuses to serve
``.ht*`` files, so ``verify --http`` could never fetch it; the marker line records ownership.
"""

from __future__ import annotations

import os
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

HTACCESS_TEXT = MARKER + "\n" + HTACCESS_BODY

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


def write_htaccess(target: os.PathLike | str, *, dry_run: bool = False) -> Path | None:
    """Write ``<target>/.htaccess``; refuse (exit 4) a foreign one and (exit 3) a symlink.

    Returns the path written, or ``None`` when the existing file already has the content."""
    tgt = Path(target)
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
        if path.read_text(encoding="utf-8") == HTACCESS_TEXT:
            return None
    if dry_run:
        return path
    atomic_write(path, HTACCESS_TEXT.encode("utf-8"))
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
