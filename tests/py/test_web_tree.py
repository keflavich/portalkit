"""The web/ tree is vanilla ES modules with no build step (ARCHITECTURE.md section 13).

* no package.json (or lock file / node_modules) anywhere under web/;
* every import specifier (static ``import``/``export ... from``, side-effect ``import '...'``,
  dynamic ``import('...')`` with a string literal, and ``new Worker/URL('...', import.meta.url)``)
  is relative (``./`` or ``../``);
* static relative specifiers resolve to files that exist.
"""

from __future__ import annotations

import bisect
import functools
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
WEB = REPO / "web"

pytestmark = pytest.mark.unit

_KW = r"(?<![\w$.])"
_STATIC = re.compile(_KW + r"""(?:import|export)\b[^'"`;]*?\bfrom\s*(['"])([^'"]+)\1""")
_BARE = re.compile(_KW + r"""import\s*(['"])([^'"]+)\1""")
_DYNAMIC = re.compile(_KW + r"""import\(\s*(['"`])([^'"`]+)\1\s*\)""")
_URLREF = re.compile(r"""\bnew\s+(?:Worker|SharedWorker|URL)\(\s*(['"`])([^'"`]+)\1\s*,\s*import\.meta\.url""")

#: after one of these characters (or keywords) a '/' starts a regex literal, not a division
_REGEX_PREV = set("(,=:[!&|?{};+-*%<>~^")
_REGEX_KW = ("return", "typeof", "case", "do", "else", "in", "of", "new", "delete", "void",
             "throw", "yield", "await", "instanceof")


def _js_files() -> list[Path]:
    return sorted(p for p in WEB.rglob("*.js") if p.is_file())


_TOK = re.compile(r"""
    (?P<ws>\s+)
  | (?P<line>//[^\n]*)
  | (?P<block>/\*[\s\S]*?(?:\*/|\Z))
  | (?P<str>'(?:[^'\\\n]|\\.)*'?|"(?:[^"\\\n]|\\.)*"?)
  | (?P<word>[A-Za-z0-9_$]+)
  | (?P<punct>[^\sA-Za-z0-9_$'"`/{}]+)
  | (?P<other>[\s\S])
""", re.X)
_REGEX_LIT = re.compile(r"/(?:[^/\\\n\[]|\\.|\[(?:[^\]\\\n]|\\.)*\])+/[A-Za-z]*")
_TEMPLATE_BODY = re.compile(r"(?:[^`\\$]|\\[\s\S]|\$(?!\{))*")


def scan(src: str) -> tuple[str, list[tuple[int, int]]]:
    """Blank out comments and return the spans of string, template and regex literals.

    Strings are consumed as tokens, so a ``/*`` or ``//`` inside a string does not start a
    comment, and an ``import`` inside a literal is not a module reference."""
    out: list[str] = []
    spans: list[tuple[int, int]] = []
    n = len(src)
    pos = 0
    prev, word = "", ""          # last significant character / identifier outside literals
    tmpl: list[int] = []         # brace depth at each open ${ of a template literal
    brace = 0

    def template(start: int) -> int:
        """Scan a template body from ``start``; return where code resumes."""
        m = _TEMPLATE_BODY.match(src, start)
        end = m.end()
        spans.append((start, end))
        out.append(src[start:end])
        if src.startswith("${", end):
            tmpl.append(brace)
            out.append("${")
            return end + 2
        if end < n:
            out.append("`")
            return end + 1
        return n

    while pos < n:
        c = src[pos]
        if c == "`" or (c == "}" and tmpl and tmpl[-1] == brace):
            if c == "}":
                tmpl.pop()
            out.append(c)
            pos = template(pos + 1)
            prev, word = "`", ""
            continue
        if c == "/" and not src.startswith("//", pos) and not src.startswith("/*", pos) and \
                (prev == "" or prev in _REGEX_PREV or word in _REGEX_KW):
            m = _REGEX_LIT.match(src, pos)
            if m:
                spans.append((pos + 1, m.end()))
                out.append(m.group(0))
                pos = m.end()
                prev, word = "/", ""
                continue
        m = _TOK.match(src, pos)
        kind, text = m.lastgroup, m.group(0)
        if kind == "ws":
            out.append(text)
        elif kind in ("line", "block"):
            out.append(re.sub(r"[^\n]", " ", text))
        elif kind == "str":
            spans.append((pos + 1, m.end()))
            out.append(text)
            prev, word = '"', ""
        elif kind == "word":
            out.append(text)
            prev, word = text[-1], text
        elif kind == "punct":
            out.append(text)
            prev, word = text[-1], ""
        else:
            if c == "{":
                brace += 1
            elif c == "}":
                brace -= 1
            out.append(text)
            prev, word = c, ""
        pos = m.end()
    return "".join(out), spans


def _in_spans(spans: list[tuple[int, int]], i: int) -> bool:
    k = bisect.bisect_right(spans, (i, float("inf"))) - 1
    return k >= 0 and spans[k][0] <= i < spans[k][1]


def specifiers(src: str) -> list[tuple[str, str]]:
    """``(kind, specifier)`` for each module reference in ``src``."""
    code, spans = scan(src)
    out: list[tuple[str, str]] = []
    for kind, rx in (("static", _STATIC), ("static", _BARE), ("dynamic", _DYNAMIC), ("url", _URLREF)):
        for m in rx.finditer(code):
            if _in_spans(spans, m.start()):
                continue      # the keyword sits inside a string / template / regex literal
            out.append((kind, m.group(2)))
    return out


@functools.lru_cache(maxsize=None)
def _file_specifiers(path: Path) -> tuple[tuple[str, str], ...]:
    return tuple(specifiers(path.read_text(encoding="utf-8")))


def _relative(spec: str) -> bool:
    return spec.startswith("./") or spec.startswith("../")


def test_specifier_parser():
    src = """
    import a from './a.js';
    import {b, c} from "../b.js";
    export {d} from './d.js';
    import './side.js';
    // import x from 'bare';
    /* import y from 'bare2'; */
    const m = await import('./lazy.js');
    const w = new Worker(new URL('./w.js', import.meta.url), {type: 'module'});
    """
    got = specifiers(src)
    assert ("static", "./a.js") in got and ("static", "../b.js") in got
    assert ("static", "./d.js") in got and ("static", "./side.js") in got
    assert ("dynamic", "./lazy.js") in got and ("url", "./w.js") in got
    assert all(_relative(s) for _, s in got)


def test_specifier_parser_edge_cases():
    # a '/*' inside a string does not hide the import that follows
    got = specifiers("const g = 'a/*';\nimport x from 'bare2';\nconst h = '*/';\n")
    assert ("static", "bare2") in got
    # several statements on one line (minified style)
    got = specifiers("import a from './a.js'; import b from 'bare-pkg';export {c} from \"./c.js\"")
    assert ("static", "./a.js") in got and ("static", "bare-pkg") in got and ("static", "./c.js") in got
    # import text inside strings, templates and regexes is not a module reference
    got = specifiers("const s = \"import x from 'nope'\"; const t = `import('nope2')`; const r = /import 'nope3'/;")
    assert got == []
    # a template substitution is code again
    got = specifiers("const t = `${await import('./t.js')}`;")
    assert got == [("dynamic", "./t.js")]
    # division is not a regex
    got = specifiers("const q = a / b; import z from './z.js'; const w = c / d;")
    assert got == [("static", "./z.js")]
    # member access named import is not a statement
    assert specifiers("obj.import('x');") == []


def test_web_tree_exists():
    assert WEB.is_dir()
    assert _js_files()


def test_no_package_json():
    found = [str(p.relative_to(REPO)) for name in ("package.json", "package-lock.json", "yarn.lock",
                                                     "pnpm-lock.yaml")
             for p in WEB.rglob(name)]
    found += [str(p.relative_to(REPO)) for p in WEB.rglob("node_modules")]
    assert found == []


def test_imports_are_relative():
    bad = []
    for p in _js_files():
        for kind, spec in _file_specifiers(p):
            if not _relative(spec):
                bad.append(f"{p.relative_to(REPO)}: {kind} {spec!r}")
    assert bad == [], "\n".join(bad)


def test_static_imports_resolve():
    bad = []
    for p in _js_files():
        for kind, spec in _file_specifiers(p):
            if kind not in ("static", "url") or "${" in spec:
                continue
            target = (p.parent / spec.split("?")[0].split("#")[0]).resolve()
            if not (target.is_file() or (kind == "url" and spec.endswith("/") and target.is_dir())):
                bad.append(f"{p.relative_to(REPO)}: {spec!r}")
    assert bad == [], "\n".join(bad)
