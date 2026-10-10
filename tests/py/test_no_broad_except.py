"""Code rule: no bare ``except`` and no ``except Exception``/``BaseException`` without re-raise.

AST scan of ``portalkit/``, ``adapters/``, ``tests/`` and ``tools/`` (ARCHITECTURE.md section 1).

A broad handler is accepted only when its body re-raises: it contains a ``raise`` statement
at the handler's own level (``if``/``try``/``with``/loop blocks count; nested ``def``/``class``
bodies and lambdas do not). A bare ``except:`` is always rejected, re-raise or not.

Also rejected: ``contextlib.suppress(Exception)`` / ``suppress(BaseException)`` (a handler with
no body), and handlers whose type is a simple alias of a broad class (``E = Exception`` or
``E = (ValueError, Exception)`` anywhere in the module, then ``except E:``).

Not detected (accepted limitation): a ``raise`` that an inner ``try`` inside the handler
swallows again; the scanner counts any ``raise`` at the handler's level as a re-raise.

Excluded from the scan: ``tests/fixtures/legacy_snippets/`` and ``tests/golden/`` (stored
third-party/legacy bytes, not portalkit code).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("portalkit", "adapters", "tests", "tools")
EXCLUDE = ("tests/fixtures/legacy_snippets/", "tests/golden/")
BROAD = {"Exception", "BaseException"}


def _names(node: ast.expr | None, aliases: dict[str, set[str]] | None = None) -> list[str]:
    """Exception class names caught by a handler's type expression (aliases resolved)."""
    if node is None:
        return []
    if isinstance(node, (ast.Tuple, ast.List)):
        out: list[str] = []
        for elt in node.elts:
            out.extend(_names(elt, aliases))
        return out
    if isinstance(node, ast.Name):
        return [node.id, *sorted((aliases or {}).get(node.id, ()))]
    if isinstance(node, ast.Attribute):  # builtins.Exception
        return [node.attr]
    return []


def _broad_aliases(tree: ast.AST) -> dict[str, set[str]]:
    """Names assigned (anywhere, by plain ``=``) an expression that names a broad class."""
    aliases: dict[str, set[str]] = {}
    assigns = [n for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.AnnAssign))]
    for _ in range(3):   # resolve short alias chains (E2 = E1 = Exception)
        for node in assigns:
            if node.value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            broad = set(_names(node.value, aliases)) & BROAD
            if not broad:
                continue
            for t in targets:
                if isinstance(t, ast.Name):
                    aliases.setdefault(t.id, set()).update(broad)
    return aliases


def _is_suppress(call: ast.Call) -> bool:
    f = call.func
    return ((isinstance(f, ast.Name) and f.id == "suppress")
            or (isinstance(f, ast.Attribute) and f.attr == "suppress"))


def _reraises(body: list[ast.stmt]) -> bool:
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Raise):
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(node))
    return False


def find_violations(source: str, filename: str = "<string>") -> list[tuple[int, str]]:
    """Return ``(lineno, message)`` for every forbidden handler in ``source``.

    A file that does not parse is reported as a violation (it cannot be checked).
    """
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        return [(exc.lineno or 0, f"syntax error, cannot scan: {exc.msg}")]
    aliases = _broad_aliases(tree)
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_suppress(node):
            broad = sorted({n for a in node.args for n in _names(a, aliases)} & BROAD)
            if broad:
                out.append((node.lineno, f"'suppress({'/'.join(broad)})' swallows broad exceptions"))
            continue
        if not isinstance(node, ast.ExceptHandler):
            continue
        if node.type is None:
            out.append((node.lineno, "bare 'except:'"))
            continue
        broad = sorted(set(_names(node.type, aliases)) & BROAD)
        if broad and not _reraises(node.body):
            out.append((node.lineno, f"'except {'/'.join(broad)}' without re-raise"))
    out.sort()
    return out


def iter_python_files(root: Path = REPO):
    for d in SCAN_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            if any(rel.startswith(x) for x in EXCLUDE):
                continue
            if "__pycache__" in path.parts:
                continue
            yield path, rel


# ---------------------------------------------------------------------------
# scanner self-tests
# ---------------------------------------------------------------------------

BAD = {
    "bare": "try:\n    f()\nexcept:\n    pass\n",
    "bare_with_raise": "try:\n    f()\nexcept:\n    raise\n",
    "exception": "try:\n    f()\nexcept Exception:\n    pass\n",
    "base_exception_as": "try:\n    f()\nexcept BaseException as e:\n    log(e)\n",
    "tuple": "try:\n    f()\nexcept (ValueError, Exception):\n    pass\n",
    "attribute": "import builtins\ntry:\n    f()\nexcept builtins.Exception:\n    pass\n",
    "raise_only_in_nested_def": (
        "try:\n    f()\nexcept Exception:\n    def g():\n        raise\n    g\n"),
    "raise_only_in_lambda_body_is_not_possible_so_return": (
        "def h():\n    try:\n        f()\n    except Exception:\n        return None\n"),
    "nested_inside_function": (
        "def h():\n    for i in range(3):\n        try:\n            f()\n        except Exception:\n            continue\n"),
    "except_star": "try:\n    f()\nexcept* Exception:\n    pass\n",
    "contextlib_suppress": "import contextlib\nwith contextlib.suppress(Exception):\n    f()\n",
    "bare_suppress_tuple": (
        "from contextlib import suppress\nwith suppress(KeyError, BaseException):\n    f()\n"),
    "alias": "E = Exception\ntry:\n    f()\nexcept E:\n    pass\n",
    "alias_tuple": "E = (ValueError, Exception)\ntry:\n    f()\nexcept E:\n    pass\n",
    "alias_chain": "E1 = Exception\nE2 = E1\ntry:\n    f()\nexcept E2:\n    pass\n",
}

GOOD = {
    "specific": "try:\n    f()\nexcept (ValueError, KeyError):\n    pass\n",
    "reraise": "try:\n    f()\nexcept Exception:\n    cleanup()\n    raise\n",
    "raise_from": "try:\n    f()\nexcept Exception as e:\n    raise RuntimeError('x') from e\n",
    "conditional_raise": (
        "try:\n    f()\nexcept BaseException as e:\n    if bad(e):\n        raise\n    log(e)\n"),
    "subclass_named_like_exception": "try:\n    f()\nexcept MyException:\n    pass\n",
    "import_error": "try:\n    import matplotlib\nexcept ImportError:\n    matplotlib = None\n",
    "unicode_source": "# Σ λ ü\ntry:\n    f('é')\nexcept OSError:\n    pass\n",
    "empty": "",
    "specific_suppress": "import contextlib\nwith contextlib.suppress(FileNotFoundError):\n    f()\n",
    "specific_alias": "E = (KeyError, ValueError)\ntry:\n    f()\nexcept E:\n    pass\n",
    "alias_reraise": "E = Exception\ntry:\n    f()\nexcept E:\n    raise\n",
}


@pytest.mark.parametrize("name", sorted(BAD))
def test_scanner_flags(name):
    v = find_violations(BAD[name])
    assert len(v) == 1, (name, v)


@pytest.mark.parametrize("name", sorted(GOOD))
def test_scanner_accepts(name):
    assert find_violations(GOOD[name]) == []


def test_scanner_reports_line_numbers_and_multiple():
    src = ("try:\n    a()\nexcept:\n    pass\n"
           "try:\n    b()\nexcept Exception:\n    pass\n"
           "try:\n    c()\nexcept Exception:\n    raise\n")
    v = find_violations(src)
    assert [ln for ln, _ in v] == [3, 7]
    assert "bare" in v[0][1] and "Exception" in v[1][1]


def test_scanner_reports_syntax_errors():
    v = find_violations("def f(:\n    pass\n")
    assert len(v) == 1 and "syntax error" in v[0][1]


def test_exclusions_and_dirs_exist():
    assert (REPO / "portalkit").is_dir()
    files = {rel for _, rel in iter_python_files()}
    assert "tests/py/test_no_broad_except.py" in files
    assert "portalkit/schema.py" in files
    assert not any(r.startswith(EXCLUDE) for r in files)


# ---------------------------------------------------------------------------
# the actual repository scan
# ---------------------------------------------------------------------------

def test_repository_has_no_broad_except():
    problems = []
    for path, rel in iter_python_files():
        src = path.read_text(encoding="utf-8")
        for ln, msg in find_violations(src, rel):
            problems.append(f"{rel}:{ln}: {msg}")
    assert not problems, "forbidden exception handlers:\n" + "\n".join(problems)
