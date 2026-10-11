"""Image sets from directories whose file names encode facets.

Many legacy galleries are a directory of PNGs named like
``jw03222-o002_s000000062_nirspec_f170lp-g235m_spectrum.png``. :func:`scan` matches every
file name against a regular expression with named groups; each named group becomes a facet
tag of the item. Two group names are special:

* ``id`` -- the item id (default: the file stem); files sharing an id become one item;
* ``role`` -- the image role (default: the ``role`` argument, ``"main"``), so one regex can
  gather e.g. ``*_sed.png`` and ``*_cutout.png`` of the same source into one item.

Width and height are read from the PNG/JPEG/GIF header. Items are naturally sorted
(``s2`` before ``s10``).
"""

from __future__ import annotations

import os
import re
import struct
from pathlib import Path
from typing import Iterable, Mapping

from ..spec import ImageSetSpec, Preset

__all__ = ["DirGalleryError", "scan", "image_size", "natural_key", "IMAGE_EXTS"]

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")


class DirGalleryError(ValueError):
    """Bad pattern or an empty match."""


_NAT = re.compile(r"(\d+)")


def natural_key(s: str) -> tuple:
    """Sort key that orders embedded integers numerically."""
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p.lower()) for p in _NAT.split(s) if p != "")


def image_size(path: os.PathLike | str) -> tuple[int, int] | None:
    """``(w, h)`` from a PNG, GIF or JPEG header; None when unknown."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(32)
            if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
                w, h = struct.unpack(">II", head[16:24])
                return int(w), int(h)
            if head[:6] in (b"GIF87a", b"GIF89a"):
                w, h = struct.unpack("<HH", head[6:10])
                return int(w), int(h)
            if head[:2] == b"\xff\xd8":
                fh.seek(2)
                while True:
                    b = fh.read(1)
                    while b and b != b"\xff":
                        b = fh.read(1)
                    while b == b"\xff":
                        b = fh.read(1)
                    if not b:
                        return None
                    marker = b[0]
                    if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                        continue
                    seg = fh.read(2)
                    if len(seg) < 2:
                        return None
                    ln = struct.unpack(">H", seg)[0]
                    if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB,
                                  0xCD, 0xCE, 0xCF):
                        data = fh.read(5)
                        if len(data) < 5:
                            return None
                        h, w = struct.unpack(">HH", data[1:5])
                        return int(w), int(h)
                    fh.seek(ln - 2, 1)
    except OSError:
        return None
    return None


def scan(directory: os.PathLike | str, pattern: str | re.Pattern, *, key: str = "img",
         url_prefix: str = "", role: str = "main", roles: Mapping[str, str] | None = None,
         recursive: bool = False, caption: str | None = None, catalog: str | None = None,
         join: str | None = None, facets: Iterable[str] | None = None,
         presets: list[Preset] | None = None, sizes: bool = True,
         exts: Iterable[str] = IMAGE_EXTS) -> ImageSetSpec:
    """Build an :class:`~portalkit.spec.ImageSetSpec` from the files in ``directory``.

    ``pattern`` is matched (``re.fullmatch``) against each file's path relative to
    ``directory`` ('/' separated; just the name unless ``recursive``). Item URLs are
    ``url_prefix + relpath``. ``roles`` maps role keys to labels (default ``{role: "Image"}``
    plus every ``role`` group value seen). ``caption`` is a ``str.format`` template over the
    named groups (``"{target} {grating}"``; default: the id). ``facets`` limits the facet
    groups (default: every named group except ``id`` and ``role``). ``catalog``/``join``
    link items to a catalog dataset (section 5.6).
    """
    rx = re.compile(pattern) if isinstance(pattern, str) else pattern
    base = Path(directory)
    if not base.is_dir():
        raise DirGalleryError(f"{base} is not a directory")
    exts_l = tuple(e.lower() for e in exts)
    rels: list[str] = []
    if recursive:
        for dp, dns, fns in os.walk(base):
            dns.sort()
            for fn in fns:
                rels.append(os.path.relpath(os.path.join(dp, fn), base).replace(os.sep, "/"))
    else:
        rels = [e.name for e in os.scandir(base) if e.is_file()]
    groups_all = [g for g in rx.groupindex]
    facet_names = list(facets) if facets is not None else [g for g in groups_all if g not in ("id", "role")]
    unknown = set(facet_names) - set(groups_all)
    if unknown:
        raise DirGalleryError(f"facets {sorted(unknown)} are not named groups of the pattern")
    items: dict[str, dict] = {}
    role_labels: dict[str, str] = dict(roles or {})
    for rel in sorted(rels, key=natural_key):
        if not rel.lower().endswith(exts_l):
            continue
        m = rx.fullmatch(rel)
        if m is None:
            continue
        gd = {k: v for k, v in m.groupdict().items() if v is not None}
        stem = rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        iid = gd.get("id") or stem
        r = gd.get("role") or role
        if r not in role_labels:
            role_labels[r] = "Image" if r == "main" else r.replace("_", " ").capitalize()
        row = items.get(iid)
        if row is None:
            row = {"id": iid}
            for f in facet_names:
                if f in gd:
                    row[f] = gd[f]
            if caption is not None:
                try:
                    row["caption"] = caption.format(**{**{g: "" for g in groups_all}, **gd, "id": iid})
                except (KeyError, IndexError, ValueError) as exc:
                    raise DirGalleryError(f"caption template {caption!r}: {exc}") from exc
            else:
                row["caption"] = iid
            items[iid] = row
        if f"url_{r}" in row:
            raise DirGalleryError(f"two files give item {iid!r} role {r!r}: {rel}")
        row[f"url_{r}"] = url_prefix + rel
        if sizes and "w" not in row:
            wh = image_size(base / rel)
            if wh is not None:
                row["w"], row["h"] = wh
    if not items:
        raise DirGalleryError(f"no file in {base} matches {rx.pattern!r}")
    rows = sorted(items.values(), key=lambda r: natural_key(r["id"]))
    role_spec = {r: (f"url_{r}", lab) for r, lab in role_labels.items()
                 if any(f"url_{r}" in row for row in rows)}
    return ImageSetSpec.from_rows(rows, role_spec, key, id="id", caption="caption",
                                  tags=[f for f in facet_names], catalog=catalog, join=join,
                                  presets=presets)
