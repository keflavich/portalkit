"""portalkit.expr against the shared vectors (ARCHITECTURE.md section 4.4)."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from portalkit import expr

VECTORS = json.loads((Path(expr.__file__).parent / "data" / "expr_vectors.json").read_text(encoding="utf-8"))


def _num(v):
    if v is None:
        return np.nan
    if v == "inf":
        return np.inf
    if v == "-inf":
        return -np.inf
    return float(v)


def test_vector_file_shape():
    assert VECTORS["schema"] == "pk-expr-vectors/1"
    assert VECTORS["ws"] == " \t\r\n"
    assert len(VECTORS["compile"]) >= 50
    assert any(v.get("error") for v in VECTORS["compile"])
    assert any(not v.get("error") for v in VECTORS["compile"])
    exprs = [v["expr"] for v in VECTORS["compile"]]
    assert len(exprs) == len(set(exprs)), "duplicate compile vectors"


@pytest.mark.parametrize("vec", VECTORS["compile"], ids=lambda v: repr(v["expr"]))
def test_compile_vectors(vec):
    if vec.get("error"):
        with pytest.raises(expr.ExprError):
            expr.compile(vec["expr"])
        assert not expr.is_valid(vec["expr"])
        return
    got = expr.compile(vec["expr"])
    assert got["keys"] == vec["keys"]
    assert list(got["w"]) == vec["keys"]
    for k, w in vec["w"].items():
        assert got["w"][k] == pytest.approx(w, rel=1e-12, abs=0)
    assert got["c"] == pytest.approx(vec["c"], rel=1e-12, abs=1e-15)
    assert expr.is_valid(vec["expr"])


@pytest.mark.parametrize("vec", VECTORS["evaluate"], ids=lambda v: repr(v["expr"]))
def test_evaluate_vectors(vec):
    cols = {k: np.array([_num(x) for x in v], dtype=np.float64) for k, v in vec["cols"].items()}
    out = expr.evaluate(vec["expr"], cols, n=vec["n"])
    assert out.dtype == np.float64
    assert out.shape == (vec["n"],)
    for got, want in zip(out, vec["out"]):
        if want is None:
            assert math.isnan(got)
        else:
            assert abs(got - want) <= VECTORS["atol"] + VECTORS["rtol"] * abs(want)


def test_error_carries_position():
    with pytest.raises(expr.ExprError) as ei:
        expr.compile("a + b c")
    assert ei.value.pos == 6
    assert ei.value.expr == "a + b c"
    assert isinstance(ei.value, ValueError)


def test_non_string_rejected():
    with pytest.raises(TypeError):
        expr.compile(3)
    assert not expr.is_valid(None)
    assert expr.check(1.5) == ["expression must be a string, got float"]


def test_check_known_and_aliases():
    assert expr.check("a-b", known={"a", "b"}) == []
    errs = expr.check("a-zz", known=["a"])
    assert len(errs) == 1 and "'zz'" in errs[0]
    # a virtual-dim key that is the expression text itself
    assert expr.check("f182m-f410m", known=[], aliases=["f182m-f410m"]) == []
    # an alias accepts non-grammar text too (category key with a hyphen is not allowed, but a
    # stored name like 'cls' is), and a syntax error is reported without known
    assert expr.check("2f1") and "position" in expr.check("2f1")[0]


def test_base_keys_order_and_repeats():
    assert expr.base_keys("b+a+2*b-a") == ["b", "a"]
    assert expr.compile("b+a+2*b-a")["w"] == {"b": 3.0, "a": 0.0}


def test_negative_zero_normalised():
    got = expr.compile("0-a+a+0")
    assert math.copysign(1.0, got["w"]["a"]) == 1.0
    assert math.copysign(1.0, got["c"]) == 1.0
    assert json.dumps(got["w"]) == '{"a": 0.0}'


def test_evaluate_constant_needs_n():
    with pytest.raises(ValueError):
        expr.evaluate("3", {})
    assert expr.evaluate("3", {}, n=0).shape == (0,)


def test_evaluate_shape_and_missing_columns():
    with pytest.raises(KeyError):
        expr.evaluate("a+b", {"a": np.zeros(2)}, n=2)
    with pytest.raises(ValueError):
        expr.evaluate("a", {"a": np.zeros(3)}, n=2)
    out = expr.evaluate(expr.compile("a"), {"a": [1, 2, 3]})
    assert out.tolist() == [1.0, 2.0, 3.0]


def test_evaluate_zero_weight_key_still_propagates_missing():
    out = expr.evaluate("a-a+b", {"a": np.array([np.nan, 1.0]), "b": np.array([1.0, 2.0])})
    assert math.isnan(out[0]) and out[1] == 2.0


def test_evaluate_large_values_overflow_to_inf():
    out = expr.evaluate("1e308*a+1e308*b", {"a": np.array([1.0, 0.0]), "b": np.array([1.0, 0.0])})
    assert np.isinf(out[0]) and out[1] == 0.0


def test_accumulated_coefficient_overflow_is_an_error():
    with pytest.raises(expr.ExprError, match="overflow"):
        expr.compile("1e308*a+1e308*a")


def test_evaluate_large_n():
    n = 100_000
    a = np.arange(n, dtype=np.float64)
    out = expr.evaluate("0.5*a+1", {"a": a})
    assert out[-1] == 0.5 * (n - 1) + 1


@pytest.mark.parametrize("w,c", [
    ({"a": 1.0}, 0.0),
    ({"a": -1.0, "b": 2.5}, 0.0),
    ({"a": 0.5, "b": -0.5}, -3.25),
    ({"x": 1e-7}, 1e20),
    ({}, 7.0),
    ({}, -2.0),
    ({}, 0.0),
    ({"flux.F444W": -1.0}, 0.125),
    ({"a": 0.0}, 0.0),
    ({"a": 123456789.125}, 1e-300),
])
def test_format_round_trip(w, c):
    text = expr.format_expr(w, c)
    got = expr.compile(text)
    assert got["keys"] == list(w)
    for k in w:
        assert got["w"][k] == pytest.approx(w[k], rel=1e-15)
    assert got["c"] == pytest.approx(c, rel=1e-15, abs=0)


def test_format_rejects_bad_input():
    with pytest.raises(ValueError):
        expr.format_expr({"f182m-f410m": 1.0})
    with pytest.raises(ValueError):
        expr.format_expr({"a": float("nan")})
    with pytest.raises(ValueError):
        expr.format_expr({"a": 1.0}, float("inf"))


def test_is_key():
    assert expr.is_key("f410m") and expr.is_key("_x.y") and expr.is_key("constructor")
    for bad in ("", "1a", "a-b", "a b", ".a", "ф", None, 3):
        assert not expr.is_key(bad)
