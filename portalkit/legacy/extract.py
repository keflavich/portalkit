"""Extract the embedded data payload of a legacy portal page.

The legacy generators embed their data in one of three ways:

1. ``const DATA = {...};`` (also ``var`` / ``let``; the manifold templates, H2, RefStarMap);
2. ``window.PORTAL = {...};`` (JWST_Archive_ColorPortal);
3. ``<script id="..." type="application/json">{...}</script>`` (WD2_Portal_MapCCD).

Each payload is decoded with :meth:`json.JSONDecoder.raw_decode` starting at the first
character of the value, so the JSON may be followed by ``;``, more script, or ``</script>``
and may contain ``NaN`` / ``Infinity`` (Python's ``json.dumps`` default output).
"""

from __future__ import annotations

import html as _html
import json
import os
import re
from typing import Any, Iterator

__all__ = ["LegacyExtractError", "Embedded", "payload", "payloads", "script_json", "read_text"]


class LegacyExtractError(ValueError):
    """No payload found, or the payload is not valid JSON."""


#: ``const|var|let NAME =`` assignments (NAME defaults to DATA)
_ASSIGN = r"\b(?:const|var|let)\s+{name}\s*=\s*"
#: ``window.NAME =`` / ``window["NAME"] =``
_WINDOW = r"\bwindow\s*(?:\.\s*{name}|\[\s*['\"]{name}['\"]\s*\])\s*=\s*"
#: ``<script ... type="application/json" ...>``
_SCRIPT_JSON = re.compile(r"<script\b((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>", re.I | re.S)
_ATTR = re.compile(r"([\w:-]+)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s\"'>]+)", re.S)
_SCRIPT_END = re.compile(r"</script\s*>", re.I)


class Embedded(dict):
    """A located payload: ``{"style", "name", "start", "end", "value"}``.

    ``style`` is ``"assign"``, ``"window"`` or ``"script"``; ``name`` is the variable name
    or the script element id; ``start``/``end`` delimit the JSON text in the page.
    """

    @property
    def value(self) -> Any:
        return self["value"]


def read_text(source: str | os.PathLike) -> str:
    """The file's text (utf-8) when ``source`` is a path of an existing file, else
    ``source`` itself (HTML text)."""
    if isinstance(source, os.PathLike):
        with open(source, encoding="utf-8", errors="surrogateescape") as fh:
            return fh.read()
    if not isinstance(source, str):
        raise TypeError(f"expected HTML text or a path, got {type(source).__name__}")
    if len(source) < 4096 and "\n" not in source and "<" not in source and os.path.isfile(source):
        with open(source, encoding="utf-8", errors="surrogateescape") as fh:
            return fh.read()
    return source


_DECODER = json.JSONDecoder()


def _decode_at(text: str, pos: int, what: str) -> tuple[Any, int]:
    while pos < len(text) and text[pos] in " \t\r\n":
        pos += 1
    try:
        return _DECODER.raw_decode(text, pos)
    except json.JSONDecodeError as exc:
        raise LegacyExtractError(f"{what}: invalid JSON at offset {exc.pos}: {exc.msg}") from exc


def _attrs(tag_inner: str) -> dict[str, str]:
    out = {}
    for m in _ATTR.finditer(tag_inner):
        v = m.group(2)
        if v[:1] in "\"'":
            v = v[1:-1]
        out[m.group(1).lower()] = _html.unescape(v)
    return out


def _candidates(text: str, names: tuple[str, ...], ids: tuple[str, ...] | None
                ) -> Iterator[tuple[int, str, str, int]]:
    """Yield ``(match_start, style, name, value_start)`` for every embed site."""
    for name in names:
        for m in re.finditer(_ASSIGN.format(name=re.escape(name)), text):
            yield m.start(), "assign", name, m.end()
        for m in re.finditer(_WINDOW.format(name=re.escape(name)), text):
            yield m.start(), "window", name, m.end()
    for m in _SCRIPT_JSON.finditer(text):
        a = _attrs(m.group(1))
        if a.get("type", "").strip().lower() not in ("application/json", "application/ld+json"):
            continue
        sid = a.get("id", "")
        if ids is not None and sid not in ids:
            continue
        yield m.start(), "script", sid, m.end()


def payloads(source: str | os.PathLike, *, names: tuple[str, ...] = ("DATA", "PORTAL"),
             ids: tuple[str, ...] | None = None) -> list[Embedded]:
    """Every decodable payload in the page, in document order.

    ``names`` are the JS variable names searched for ``const|var|let NAME =`` and
    ``window.NAME =``; ``ids`` limits the ``<script type="application/json">`` elements
    (``None`` accepts any id). Sites whose value is not JSON (``var DATA = {ra: null}``,
    an unreplaced ``__DATA__`` placeholder) are skipped.
    """
    text = read_text(source)
    out: list[Embedded] = []
    seen: set[int] = set()
    for start, style, name, vstart in sorted(_candidates(text, names, ids)):
        if vstart in seen:
            continue
        seen.add(vstart)
        try:
            value, end = _decode_at(text, vstart, f"{style} {name}")
        except LegacyExtractError:
            continue
        if style == "script":
            pos = end
            while pos < len(text) and text[pos] in " \t\r\n;":
                pos += 1
            if _SCRIPT_END.match(text, pos) is None:
                continue
        out.append(Embedded(style=style, name=name, start=vstart, end=end, value=value))
    return out


def payload(source: str | os.PathLike, *, names: tuple[str, ...] = ("DATA", "PORTAL"),
            ids: tuple[str, ...] | None = None, style: str | None = None) -> Any:
    """The first (earliest in the page) decodable payload; :class:`LegacyExtractError`
    when there is none. ``style`` restricts the embed style (``assign|window|script``)."""
    found = payloads(source, names=names, ids=ids)
    if style is not None:
        found = [e for e in found if e["style"] == style]
    if not found:
        where = os.fspath(source) if isinstance(source, os.PathLike) else "page"
        raise LegacyExtractError(f"no embedded payload ({', '.join(names)} or "
                                 f"<script type=application/json>) found in {where}")
    return found[0].value


def script_json(source: str | os.PathLike, element_id: str) -> Any:
    """The JSON content of ``<script id=element_id type="application/json">`` (e.g. the
    ``pk-page`` element of a portalkit page); :class:`LegacyExtractError` if absent."""
    text = read_text(source)
    for m in _SCRIPT_JSON.finditer(text):
        a = _attrs(m.group(1))
        if a.get("id") != element_id:
            continue
        end = _SCRIPT_END.search(text, m.end())
        if end is None:
            raise LegacyExtractError(f"<script id={element_id!r}> is not closed")
        body = text[m.end():end.start()].strip()
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise LegacyExtractError(f"<script id={element_id!r}>: invalid JSON: {exc}") from exc
    raise LegacyExtractError(f"no <script id={element_id!r}> element")
