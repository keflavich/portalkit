"""Page HTML from ``web/shell.html`` and a page.json document (ARCHITECTURE.md section 4.3).

The shell embeds the page document as ``<script type="application/json" id="pk-page">``.
:func:`escape_json` serialises it so that no ``</`` (``</script>``) or ``<!--`` sequence can
end or alter the script element. The template uses ``{{name}}`` placeholders; :func:`render`
substitutes them in one pass, so text inside substituted values is never re-expanded.

When the page document carries ``budget``, ``budget.html_bytes`` is set to the byte size of
the rendered HTML (found by fixed-point iteration, since the number is part of the HTML).
"""

from __future__ import annotations

import html
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping

from ..constants import PAGE_HTML_MAX, PAGE_JSON_MAX
from ..schema import validate_page

__all__ = ["TEMPLATE_PATH", "TEMPLATE_MAX", "ShellError", "escape_json", "page_json_bytes",
           "load_template", "render", "write"]

#: ``web/shell.html`` in the repository checkout.
TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "web" / "shell.html"

#: The shell template stays under 8 kB (section 15, WP03).
TEMPLATE_MAX = 8192

_PLACEHOLDER = re.compile(r"\{\{([a-z_]+)\}\}")
_FIELDS = ("pk", "title", "description", "assets", "caps", "page_id", "archetype", "page_title",
           "page_json", "boot_url")


class ShellError(ValueError):
    """Raised when a page cannot be rendered within the section 5.11 budgets."""


def escape_json(obj: Any) -> str:
    """Compact JSON for embedding inside ``<script type="application/json">``.

    ``</`` becomes ``<\\/`` and ``<!--`` becomes ``\\u003c!--``. Both sequences can only occur
    inside JSON strings, where these escapes decode to the original characters.
    """
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return s.replace("<!--", "\\u003c!--").replace("</", "<\\/")


def page_json_bytes(obj: Any) -> int:
    """UTF-8 size of the compact page document (the measure ``schema.validate_page`` uses)."""
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def load_template(path: str | os.PathLike | None = None) -> str:
    """The shell template text (default :data:`TEMPLATE_PATH`).

    ``web/shell.html`` sits outside the ``portalkit`` package, so the default works from a
    repository checkout or an editable install (``pip install -e``).
    """
    p = Path(path) if path is not None else TEMPLATE_PATH
    try:
        return p.read_text(encoding="utf-8")
    except FileNotFoundError as err:
        if path is not None:
            raise
        raise FileNotFoundError(
            f"shell template {p} not found: portalkit reads web/shell.html from the repository "
            f"checkout; use an editable install (pip install -e) or pass template=") from err


def _js_string(s: str) -> str:
    """A JS string literal that is also safe inside an inline ``<script>`` element."""
    return json.dumps(s, ensure_ascii=True).replace("</", "<\\/").replace("<!--", "\\u003c!--")


def _fields(page: Mapping[str, Any]) -> dict[str, str]:
    pg = page.get("page") or {}
    por = page.get("portal") or {}
    assets = page.get("assets")
    if not isinstance(assets, str) or not assets.endswith("/"):
        raise ShellError(f"page.json 'assets' must be a URL ending in '/', got {assets!r}")
    title = str(pg.get("title", ""))
    ptitle = str(por.get("title", ""))
    full = f"{title} · {ptitle}" if ptitle and ptitle != title else title
    caps = (page.get("caps") or {}).get("enabled") or []
    return {
        "pk": html.escape(str(page.get("pk", ""))),
        "title": html.escape(full),
        "description": html.escape(str(pg.get("subtitle") or full)),
        "assets": html.escape(assets),
        "caps": html.escape(" ".join(str(c) for c in caps)),
        "page_id": html.escape(str(pg.get("id", ""))),
        "archetype": html.escape(str(pg.get("archetype", ""))),
        "page_title": html.escape(title),
        "page_json": escape_json(page),
        "boot_url": _js_string(assets + "js/core/boot.js"),
    }


def _fill(template: str, fields: Mapping[str, str]) -> str:
    def sub(m: re.Match) -> str:
        name = m.group(1)
        if name not in fields:
            raise ShellError(f"shell template uses unknown placeholder {{{{{name}}}}}")
        return fields[name]
    return _PLACEHOLDER.sub(sub, template)


def render(page_json: Mapping[str, Any], template: str | None = None, *, validate: bool = True) -> str:
    """Render the page HTML.

    ``page_json`` is the assembled page document (``config.assemble``). With ``validate``
    (default) it is checked with ``schema.validate_page`` first. Raises :class:`ShellError`
    when the JSON exceeds ``PAGE_JSON_MAX`` or the HTML exceeds ``PAGE_HTML_MAX``.
    """
    page = json.loads(json.dumps(page_json, allow_nan=False))   # private deep copy, JSON-clean
    tpl = load_template() if template is None else template
    if validate:
        errs = validate_page(page)
        if errs:
            raise ShellError("invalid page.json:\n  " + "\n  ".join(errs[:20]))
    budget = page.get("budget")
    if isinstance(budget, dict):
        budget["html_bytes"] = 0
        for _ in range(8):
            out = _fill(tpl, _fields(page))
            size = len(out.encode("utf-8"))
            if budget["html_bytes"] == size:
                break
            budget["html_bytes"] = size
        else:  # pragma: no cover - the size converges within two passes
            raise ShellError("html_bytes did not converge")
    else:
        out = _fill(tpl, _fields(page))
    jsize = page_json_bytes(page)
    if jsize > PAGE_JSON_MAX:
        raise ShellError(f"page.json is {jsize} bytes, above PAGE_JSON_MAX={PAGE_JSON_MAX}")
    hsize = len(out.encode("utf-8"))
    if hsize > PAGE_HTML_MAX:
        raise ShellError(f"page HTML is {hsize} bytes, above PAGE_HTML_MAX={PAGE_HTML_MAX}")
    return out


def write(path: str | os.PathLike, page_json: Mapping[str, Any], template: str | None = None,
          *, validate: bool = True) -> int:
    """Render and write the page HTML atomically (temp file + ``os.replace``).

    Returns the number of bytes written.
    """
    out = render(page_json, template, validate=validate).encode("utf-8")
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".tmp", dir=dest.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(out)
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
    except OSError:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return len(out)
