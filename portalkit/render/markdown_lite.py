"""A small, safe Markdown subset for readme and narrative text.

Supported block syntax:

* ATX headings (``#`` .. ``######``) with slug ids (deduplicated per document);
* paragraphs (consecutive non-blank lines);
* unordered (``-``, ``*``, ``+``) and ordered (``1.``, ``1)``) lists, nested by indentation;
* GFM pipe tables with a delimiter row and ``:---:`` alignment;
* fenced code blocks (````` or ``~~~``) with an optional language word;
* horizontal rules (``---``, ``***``, ``___``) end the current block and are otherwise
  dropped (``hr`` is outside the tag allowlist).

Supported inline syntax: ``code``, ``[text](url "title")`` links, ``<https://...>`` autolinks
and backslash escapes. Emphasis markers stay as literal text.

Everything else is text. All text is HTML-escaped; raw HTML in the input is shown as text.
Link URLs are limited to ``http``, ``https`` and ``mailto`` schemes, relative paths and
fragments; other schemes (``javascript:``, ``data:``, ...) render the link text without a link.

Output tags: ``h1-h6 p ul ol li table thead tbody tr th td pre code a``. Attributes: ``id``
on headings, ``start`` on ``ol``, ``style="text-align:..."`` on table cells, ``class="language-x"`` on code,
``href``/``title`` on links (external links also get ``rel="noopener"``).
"""

from __future__ import annotations

import html
import re
import unicodedata

__all__ = ["render", "render_inline", "slugify", "ALLOWED_TAGS", "safe_url"]

ALLOWED_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol", "li", "table",
                          "thead", "tbody", "tr", "th", "td", "pre", "code", "a"})

_ESCAPABLE = set("\\`*_{}[]()#+-.!|<>~\"'")
_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^( {0,3})(`{3,}|~{3,})[ \t]*([^`\s]*)[^`]*$")
_HR = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_UL = re.compile(r"^( *)([-*+])(?:[ \t]+(.*))?$")
_OL = re.compile(r"^( *)(\d{1,9})([.)])(?:[ \t]+(.*))?$")
_TABLE_DELIM = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
_LANG_OK = re.compile(r"[^A-Za-z0-9_+#.-]")
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _esc(s: str) -> str:
    return html.escape(s, quote=True)


def slugify(text: str) -> str:
    """Heading slug: NFKD-folded, lower case, ``[a-z0-9]`` runs joined by ``-``."""
    s = unicodedata.normalize("NFKD", text)
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "section"


def safe_url(url: str) -> str | None:
    """Return the URL when its scheme is allowed (http, https, mailto, relative, fragment)."""
    u = _CTRL.sub("", url).strip()
    # browsers ignore tabs/newlines inside schemes ("java\tscript:") - drop all whitespace
    # and treat a backslash as '/' (as http(s) URL parsing does), so "/\\host" is caught
    probe = re.sub(r"\s+", "", u).replace("\\", "/")
    m = _SCHEME.match(probe)
    if m is None:
        if probe.startswith("//"):
            return None   # protocol-relative URLs leave the allowed exposure set
        return u
    if m.group(1).lower() in ("http", "https", "mailto"):
        return u
    return None


# ---------------------------------------------------------------------------
# inline
# ---------------------------------------------------------------------------

def _find_close_bracket(s: str, i: int) -> int:
    """Index of the ``]`` matching the ``[`` at ``i`` (nesting and escapes respected)."""
    depth = 0
    j = i
    while j < len(s):
        ch = s[j]
        if ch == "\\":
            j += 2
            continue
        if ch == "`":
            k = s.find("`", j + 1)
            if k < 0:
                return -1
            j = k + 1
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return -1


def _parse_dest(s: str, i: int) -> tuple[str, str | None, int] | None:
    """Parse ``(url "title")`` starting at ``s[i] == '('``. Returns (url, title, end)."""
    j = i + 1
    n = len(s)
    while j < n and s[j] in " \t":
        j += 1
    if j < n and s[j] == "<":
        k = s.find(">", j + 1)
        if k < 0:
            return None
        url = s[j + 1:k]
        j = k + 1
    else:
        start = j
        depth = 0
        while j < n:
            ch = s[j]
            if ch == "\\" and j + 1 < n:
                j += 2
                continue
            if ch in " \t":
                break
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    break
                depth -= 1
            j += 1
        url = s[start:j]
    url = re.sub(r"\\(.)", lambda m: m.group(1) if m.group(1) in _ESCAPABLE else m.group(0), url)
    while j < n and s[j] in " \t":
        j += 1
    title = None
    if j < n and s[j] in "\"'":
        q = s[j]
        k = j + 1
        buf = []
        while k < n and s[k] != q:
            if s[k] == "\\" and k + 1 < n:
                buf.append(s[k + 1])
                k += 2
                continue
            buf.append(s[k])
            k += 1
        if k >= n:
            return None
        title = "".join(buf)
        j = k + 1
        while j < n and s[j] in " \t":
            j += 1
    if j < n and s[j] == ")":
        return url, title, j + 1
    return None


def _link(text_html: str, url: str, title: str | None) -> str:
    safe = safe_url(url)
    if safe is None:
        return text_html
    attrs = f'href="{_esc(safe)}"'
    if title:
        attrs += f' title="{_esc(title)}"'
    if re.match(r"(?i)https?:", safe):
        attrs += ' rel="noopener"'
    return f"<a {attrs}>{text_html}</a>"


def render_inline(text: str) -> str:
    """Render inline Markdown to safe HTML."""
    segs: list[tuple[bool, str]] = []    # (is_markup, html)
    buf: list[str] = []

    def flush() -> None:
        if buf:
            segs.append((False, _esc("".join(buf))))
            buf.clear()

    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            nxt = text[i + 1]
            if nxt in _ESCAPABLE:
                flush()
                # an escaped char is literal text
                segs.append((True, _esc(nxt)))
                i += 2
                continue
        if ch == "`":
            run = 0
            while i + run < n and text[i + run] == "`":
                run += 1
            fence = "`" * run
            k = text.find(fence, i + run)
            while k >= 0 and k + run < n and text[k + run] == "`":
                k = text.find(fence, k + run + 1)
            if k >= 0:
                code = text[i + run:k].replace("\n", " ")
                if code.startswith(" ") and code.endswith(" ") and code.strip():
                    code = code[1:-1]
                flush()
                segs.append((True, f"<code>{_esc(code)}</code>"))
                i = k + run
                continue
            buf.append(fence)
            i += run
            continue
        if ch == "<":
            m = re.match(r"<((?:https?://|mailto:)[^\s<>]+)>", text[i:], re.I)
            if m:
                url = m.group(1)
                flush()
                label = url[7:] if url.lower().startswith("mailto:") else url
                segs.append((True, _link(_esc(label), url, None)))
                i += m.end()
                continue
        if ch == "[":
            close = _find_close_bracket(text, i)
            if close > 0 and close + 1 < n and text[close + 1] == "(":
                dest = _parse_dest(text, close + 1)
                if dest is not None:
                    url, title, end = dest
                    inner = render_inline(text[i + 1:close])
                    inner = re.sub(r"</?a\b[^>]*>", "", inner)   # no nested links
                    flush()
                    segs.append((True, _link(inner, url, title)))
                    i = end
                    continue
        buf.append(ch)
        i += 1
    flush()
    return "".join(h for _, h in segs)


# ---------------------------------------------------------------------------
# blocks
# ---------------------------------------------------------------------------

def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells: list[str] = []
    cur: list[str] = []
    i = 0
    in_code = False
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s) and s[i + 1] == "|":
            cur.append("|")
            i += 2
            continue
        if ch == "`":
            in_code = not in_code
        if ch == "|" and not in_code:
            cells.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    cells.append("".join(cur).strip())
    return cells


def _align(cell: str) -> str | None:
    c = cell.strip()
    left, right = c.startswith(":"), c.endswith(":")
    if left and right:
        return "center"
    if right:
        return "right"
    if left:
        return "left"
    return None


def _table(lines: list[str]) -> str:
    head = _split_row(lines[0])
    aligns = [_align(c) for c in _split_row(lines[1])]
    ncol = len(head)
    aligns = (aligns + [None] * ncol)[:ncol]

    def cell(tag: str, text: str, al: str | None) -> str:
        st = f' style="text-align:{al}"' if al else ""
        return f"<{tag}{st}>{render_inline(text)}</{tag}>"

    out = ["<table><thead><tr>"]
    out += [cell("th", h, aligns[k]) for k, h in enumerate(head)]
    out.append("</tr></thead>")
    body = lines[2:]
    if body:
        out.append("<tbody>")
        for ln in body:
            cells = (_split_row(ln) + [""] * ncol)[:ncol]
            out.append("<tr>" + "".join(cell("td", c, aligns[k]) for k, c in enumerate(cells)) + "</tr>")
        out.append("</tbody>")
    out.append("</table>")
    return "".join(out)


def _is_table_start(lines: list[str], i: int) -> bool:
    if i + 1 >= len(lines):
        return False
    a, b = lines[i], lines[i + 1]
    if "|" not in a or not _TABLE_DELIM.match(b) or "-" not in b:
        return False
    return len(_split_row(a)) == len(_split_row(b))


def _list_item(line: str) -> tuple[int, str, str, int | None] | None:
    """(indent, kind, content, start) for a list item line, else None."""
    m = _UL.match(line)
    if m:
        return len(m.group(1)), "ul", m.group(3) or "", None
    m = _OL.match(line)
    if m:
        return len(m.group(1)), "ol", m.group(4) or "", int(m.group(2))
    return None


def _starts_list(line: str, in_para: bool) -> bool:
    """True when ``line`` opens a list. Inside a paragraph (CommonMark rule) only a
    non-empty bullet item, or a non-empty ordered item numbered 1, indented at most 3
    spaces, interrupts it."""
    it = _list_item(line)
    if it is None:
        return False
    if not in_para:
        return True
    indent, kind, content, start = it
    return indent <= 3 and bool(content.strip()) and (kind == "ul" or start == 1)


def _render_list(lines: list[str]) -> str:
    """Render consecutive list lines (items plus their continuation lines)."""
    items: list[tuple[int, str, list[str], int | None]] = []
    for ln in lines:
        it = _list_item(ln)
        if it is not None and not _HR.match(ln):
            items.append((it[0], it[1], [it[2]], it[3]))
        elif items:
            items[-1][2].append(ln.strip())
    out: list[str] = []
    stack: list[tuple[int, str]] = []    # (indent, kind)
    for indent, kind, text, start in items:
        while stack and indent < stack[-1][0]:
            out.append(f"</li></{stack[-1][1]}>")
            stack.pop()
        if stack and indent == stack[-1][0]:
            if kind != stack[-1][1]:
                out.append(f"</li></{stack[-1][1]}>")
                stack.pop()
                opened = True
            else:
                out.append("</li>")
                opened = False
        else:
            opened = True
        if opened:
            if kind == "ol" and start is not None and start != 1:
                out.append(f'<ol start="{start}">')
            else:
                out.append(f"<{kind}>")
            stack.append((indent, kind))
        out.append("<li>" + render_inline("\n".join(t for t in text if t)))
    while stack:
        out.append(f"</li></{stack[-1][1]}>")
        stack.pop()
    return "".join(out)


def render(md: str) -> str:
    """Render Markdown text to safe HTML (see module docstring for the subset)."""
    if not isinstance(md, str):
        raise TypeError(f"markdown must be a string, got {type(md).__name__}")
    text = md.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "\ufffd")
    lines = text.split("\n")
    out: list[str] = []
    slugs: dict[str, int] = {}
    para: list[str] = []

    def flush_para() -> None:
        if para:
            out.append("<p>" + render_inline("\n".join(para).strip()) + "</p>")
            para.clear()

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            flush_para()
            i += 1
            continue
        m = _FENCE.match(line)
        if m:
            flush_para()
            fence = m.group(2)
            lang = _LANG_OK.sub("", m.group(3))[:32]
            body: list[str] = []
            i += 1
            while i < n:
                ln = lines[i]
                stripped = ln.strip()
                if stripped.startswith(fence[0] * len(fence)) and not stripped.strip(fence[0]):
                    i += 1
                    break
                body.append(ln)
                i += 1
            cls = f' class="language-{_esc(lang)}"' if lang else ""
            code = "\n".join(body)
            out.append(f"<pre><code{cls}>{_esc(code)}</code></pre>")
            continue
        m = _HEADING.match(line)
        if m:
            flush_para()
            level = len(m.group(1))
            content = (m.group(2) or "").strip()
            base = slugify(re.sub(r"[`*_\[\]()\\]", "", content))
            if base in slugs:
                slugs[base] += 1
                slug = f"{base}-{slugs[base]}"
                while slug in slugs:
                    slugs[base] += 1
                    slug = f"{base}-{slugs[base]}"
                slugs[slug] = 0
            else:
                slugs[base] = 0
                slug = base
            out.append(f'<h{level} id="{_esc(slug)}">{render_inline(content)}</h{level}>')
            i += 1
            continue
        if _HR.match(line):
            flush_para()
            i += 1
            continue
        if _is_table_start(lines, i):
            flush_para()
            j = i + 2
            # GFM: rows continue until a blank line or the start of another block
            while j < n and lines[j].strip() and not (
                    _FENCE.match(lines[j]) or _HEADING.match(lines[j]) or _HR.match(lines[j])):
                j += 1
            out.append(_table(lines[i:j]))
            i = j
            continue
        if _starts_list(line, bool(para)):
            flush_para()
            j = i + 1
            while j < n and lines[j].strip():
                if _FENCE.match(lines[j]) or _HEADING.match(lines[j]):
                    break
                j += 1
            out.append(_render_list(lines[i:j]))
            i = j
            continue
        para.append(line)
        i += 1
    flush_para()
    return "\n".join(out)
