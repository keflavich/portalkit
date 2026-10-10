"""Linear expressions over stored catalog columns (ARCHITECTURE.md section 4.4).

Python twin of ``web/js/data/expr.js``. Both are tested against
``portalkit/data/expr_vectors.json``.

Grammar::

    expr   := ws term (ws ('+'|'-') ws term)* ws
    term   := number ws '*' ws key | number '*' key | key | number
    key    := [A-Za-z_][A-Za-z0-9_.]*
    number := [0-9]+('.'[0-9]*)?([eE][+-]?[0-9]+)?

``ws`` is any run of ASCII space, tab, CR or LF. There is no unary sign: ``-f410m`` and
``f1 - -f2`` are errors; write ``0-f410m``. A number that overflows to infinity is an error.

An expression compiles to ``{"w": {key: coeff}, "c": const, "keys": [key, ...]}``. Repeated
keys accumulate (``f1 - f1`` gives ``w = {"f1": 0}``; the key stays a base column, so a
missing value there still makes the result missing). ``keys`` lists the base columns in
order of first appearance.

Evaluation is ``c + sum(w[k] * col[k])`` in float64. A non-finite value (NaN or +/-inf) in
any base column makes that row missing (NaN).
"""

from __future__ import annotations

import math
import re
from typing import Iterable, Mapping

import numpy as np

__all__ = ["ExprError", "KEY_RE", "is_key", "compile", "evaluate", "base_keys",
           "is_valid", "check", "format_expr"]

#: A stored column key (section 4.4); also the pattern stored manifest keys must match.
KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*\Z")

_WS = " \t\r\n"
_KEY_START = re.compile(r"[A-Za-z_]")
_KEY_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
_NUM_TOKEN = re.compile(r"[0-9]+(?:\.[0-9]*)?(?:[eE][+-]?[0-9]+)?")


class ExprError(ValueError):
    """Raised for an expression that does not match the section 4.4 grammar."""

    def __init__(self, expr: str, pos: int, msg: str):
        self.expr = expr
        self.pos = pos
        super().__init__(f"{msg} at position {pos} in expression {expr!r}")


def is_key(s: object) -> bool:
    """True when ``s`` is a syntactically valid stored-column key."""
    return isinstance(s, str) and KEY_RE.match(s) is not None


def _skip_ws(s: str, i: int) -> int:
    n = len(s)
    while i < n and s[i] in _WS:
        i += 1
    return i


def _number(expr: str, i: int) -> tuple[float, int]:
    m = _NUM_TOKEN.match(expr, i)
    if m is None:  # pragma: no cover - callers check the first char
        raise ExprError(expr, i, "expected a number")
    v = float(m.group(0))
    if not math.isfinite(v):
        raise ExprError(expr, i, "number out of range")
    j = m.end()
    # '1e' / '1.5E+' : the exponent part did not match; the letter is not a valid follower
    return v, j


def _term(expr: str, i: int) -> tuple[str | None, float, int]:
    """Parse one term at ``i`` (no leading whitespace). Returns (key|None, coeff, end)."""
    n = len(expr)
    if i >= n:
        raise ExprError(expr, i, "expected a term")
    ch = expr[i]
    if ch.isdigit() and ch in "0123456789":
        v, j = _number(expr, i)
        k = _skip_ws(expr, j)
        if k < n and expr[k] == "*":
            k = _skip_ws(expr, k + 1)
            m = _KEY_TOKEN.match(expr, k)
            if m is None:
                raise ExprError(expr, k, "expected a column key after '*'")
            return m.group(0), v, m.end()
        if j < n and _KEY_START.match(expr[j]) or (j < n and expr[j] == "."):
            raise ExprError(expr, j, "a number must be followed by '*' before a key")
        return None, v, j
    if _KEY_START.match(ch):
        m = _KEY_TOKEN.match(expr, i)
        assert m is not None
        return m.group(0), 1.0, m.end()
    if ch in "+-":
        raise ExprError(expr, i, "unary sign is not allowed (write 0-x)")
    raise ExprError(expr, i, f"unexpected character {ch!r}")


def compile(expr: str) -> dict:  # noqa: A001 - mirrors the JS export name
    """Compile ``expr`` to ``{"w": {key: coeff}, "c": const, "keys": [...]}``.

    Raises :class:`ExprError` when the text does not match the grammar.
    """
    if not isinstance(expr, str):
        raise TypeError(f"expression must be a string, got {type(expr).__name__}")
    w: dict[str, float] = {}
    keys: list[str] = []
    c = 0.0
    n = len(expr)
    i = _skip_ws(expr, 0)
    if i >= n:
        raise ExprError(expr, i, "empty expression")
    sign = 1.0
    while True:
        key, coeff, i = _term(expr, i)
        coeff *= sign
        if key is None:
            c += coeff
        else:
            if key not in w:
                w[key] = 0.0
                keys.append(key)
            w[key] += coeff
        i = _skip_ws(expr, i)
        if i >= n:
            break
        op = expr[i]
        if op not in "+-":
            raise ExprError(expr, i, f"expected '+' or '-', got {op!r}")
        sign = 1.0 if op == "+" else -1.0
        i = _skip_ws(expr, i + 1)
        if i >= n:
            raise ExprError(expr, i, f"expected a term after {op!r}")
    if not math.isfinite(c) or not all(math.isfinite(x) for x in w.values()):
        raise ExprError(expr, n, "coefficients overflow")
    # normalise -0.0 so JSON output is stable
    c = c + 0.0
    w = {k: v + 0.0 for k, v in w.items()}
    return {"w": w, "c": c, "keys": keys}


def base_keys(expr: str) -> list[str]:
    """Base column keys of ``expr`` in order of first appearance."""
    return compile(expr)["keys"]


def is_valid(expr: object) -> bool:
    """True when ``expr`` is a string that matches the grammar."""
    if not isinstance(expr, str):
        return False
    try:
        compile(expr)
    except ExprError:
        return False
    return True


def check(expr: object, known: Iterable[str] | Mapping[str, object] | None = None,
          aliases: Iterable[str] = ()) -> list[str]:
    """Validate ``expr`` and (optionally) that every base key is in ``known``.

    ``aliases`` are complete expression texts accepted verbatim (virtual-dim keys, which use
    the expression text as their key, and non-numeric stored keys such as a category column
    named in ``color.by``). Returns a list of error strings.
    """
    if not isinstance(expr, str):
        return [f"expression must be a string, got {type(expr).__name__}"]
    if expr in set(aliases):
        return []
    try:
        comp = compile(expr)
    except ExprError as err:
        return [str(err)]
    if known is None:
        return []
    known_set = set(known)
    return [f"unknown column {k!r} in expression {expr!r}" for k in comp["keys"] if k not in known_set]


def evaluate(compiled: Mapping | str, cols: Mapping[str, np.ndarray], n: int | None = None) -> np.ndarray:
    """Evaluate a compiled expression (or expression text) over column arrays.

    ``cols`` maps base keys to 1-D arrays of length ``n``. Missing values are NaN (or any
    non-finite value); the result is float64 with NaN where any base value is missing.
    """
    if isinstance(compiled, str):
        compiled = compile(compiled)
    w = compiled["w"]
    keys = list(compiled.get("keys", list(w)))
    c = float(compiled.get("c", 0.0))
    if n is None:
        if not keys:
            raise ValueError("n is required for a constant expression")
        n = len(np.asarray(cols[keys[0]]))
    out = np.full(int(n), c, dtype=np.float64)
    bad = np.zeros(int(n), dtype=bool)
    for k in keys:
        if k not in cols:
            raise KeyError(f"column {k!r} not supplied")
        v = np.asarray(cols[k], dtype=np.float64)
        if v.shape != (n,):
            raise ValueError(f"column {k!r} has shape {v.shape}, expected ({n},)")
        fin = np.isfinite(v)
        bad |= ~fin
        with np.errstate(over="ignore", invalid="ignore"):   # overflow gives +/-inf, as in JS
            out += float(w[k]) * np.where(fin, v, 0.0)
    out[bad] = np.nan
    return out


def _fmt_num(x: float) -> str:
    if x == int(x) and abs(x) < 1e15:
        return str(int(x))
    s = repr(float(x))
    if "e" in s or "E" in s:
        mant, exp = s.lower().split("e")
        if "." not in mant:
            mant += ".0"
        return f"{mant}e{exp}"
    return s


def format_expr(w: Mapping[str, float], c: float = 0.0) -> str:
    """Render ``{key: coeff}`` + ``c`` as grammar-valid text (inverse of :func:`compile`)."""
    parts: list[str] = []
    for k, v in w.items():
        if not is_key(k):
            raise ValueError(f"invalid key {k!r}")
        v = float(v)
        if not math.isfinite(v):
            raise ValueError(f"non-finite coefficient for {k!r}")
        mag = abs(v)
        body = k if mag == 1.0 else f"{_fmt_num(mag)}*{k}"
        if not parts:
            parts.append(body if v >= 0 else f"0-{body}")
        else:
            parts.append(("+" if v >= 0 else "-") + body)
    c = float(c)
    if not math.isfinite(c):
        raise ValueError("non-finite constant")
    if c != 0.0 or not parts:
        if not parts:
            parts.append(_fmt_num(c) if c >= 0 else f"0-{_fmt_num(-c)}")
        else:
            parts.append(("+" if c >= 0 else "-") + _fmt_num(abs(c)))
    return "".join(parts)
