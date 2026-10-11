"""portalkit.legacy.hashmap: Python mirror of web/js/core/legacyhash.js."""

import json
from pathlib import Path

import pytest

from portalkit.legacy import hashmap as hm

VECTORS = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "legacy_snippets"
                      / "hash_vectors.json").read_text())


@pytest.mark.parametrize("case", VECTORS["cases"], ids=[c["name"] for c in VECTORS["cases"]])
def test_vectors(case):
    r = hm.map_legacy(case["grammars"], case["loc"])
    assert r["consumedSearch"] == case["consumedSearch"]
    assert set(r["hash"]) == set(case["hash"])
    for k, v in case["hash"].items():
        if k == "sel" and v.startswith("r:"):
            assert hm.decode_recipe(r["hash"][k]) == case["decoded"]["sel"]
        else:
            assert r["hash"][k] == v
        if k.endswith(".frame") and v.startswith("b64:"):
            assert hm.decode_frame(r["hash"][k]) == case["decoded"][k]


def test_vectors_cover_grammars():
    used = {g for c in VECTORS["cases"] for g in c["grammars"]}
    assert set(hm.GRAMMAR_NAMES) <= used
    assert set(hm.GRAMMARS) == set(hm.GRAMMAR_NAMES)


def test_single_grammar_functions():
    assert hm.GRAMMARS["bare-name"]({"hash": "#abc"}) == {"id": "abc"}
    assert hm.GRAMMARS["bare-name"]({"hash": ""}) is None
    assert hm.GRAMMARS["w51-rowid"]({"hash": "#W51-3"}) == {"id": "W51-3"}


@pytest.mark.parametrize("x, s", [
    (10.0, "10"), (-29.0, "-29"), (0.1, "0.1"), (1e21, "1e+21"), (1e20, "100000000000000000000"),
    (1e-7, "1e-7"), (1.5e-7, "1.5e-7"), (1e-6, "0.000001"), (0.000123, "0.000123"),
    (-1e300, "-1e+300"), (5e-324, "5e-324"), (1 / 3, "0.3333333333333333"), (0.0, "0"),
])
def test_js_number(x, s):
    assert hm.js_number(x) == s


def test_js_number_rejects_nan():
    with pytest.raises(ValueError):
        hm.js_number(float("nan"))


def test_codecs_roundtrip():
    assert hm.decode_json(hm.encode_json({"a": [1, 2.5, None]})) == {"a": [1, 2.5, None]}
    assert hm.decode_json('{"raw": 1}') == {"raw": 1}
    assert hm.encode_json({"r": [10.0]}) == hm.b64url_encode(b'{"r":[10]}')
    assert hm.decode_frame(hm.encode_frame([1, -0.5, 0.25])) == [1.0, -0.5, 0.25]
    ops = [{"op": "replace", "kind": "ids", "ids": ["a", "b"]}]
    s = hm.encode_recipe(ops)
    assert s.startswith("r:") and hm.decode_recipe(s) == ops
    assert hm.b64url_decode(hm.b64url_encode(b"\x00\xff\xfe")) == b"\x00\xff\xfe"
    with pytest.raises(ValueError):
        hm.decode_frame("b64:AAA")


def test_is_canonical_hash():
    assert hm.is_canonical_hash("id=a&c=f200w")
    assert hm.is_canonical_hash("man.frame=pca")
    assert not hm.is_canonical_hash("name=a")
    assert not hm.is_canonical_hash("view=sed")
    assert hm.is_canonical_hash("view=sed:full")
    assert not hm.is_canonical_hash("abc")


def test_legacy_context():
    page = {"layout": {"col": [{"pane": "manifold", "id": "m2", "dataset": "cat",
                                "options": {"dims": ["x", "y"]}}]}}
    assert hm.legacy_context({"page": page}) == {"dims": [{"key": "x"}, {"key": "y"}],
                                                 "pane": "m2", "ds": "cat"}
    ctx = hm.legacy_context({"dims": [{"key": "a", "scale": 3}, "b"]})
    assert ctx["dims"] == [{"key": "a", "scale": 3.0}, {"key": "b"}]
    assert ctx["pane"] == "manifold"


def test_map_legacy_edge_cases():
    assert hm.map_legacy([], {"hash": "#abc"}) == {"hash": {}, "consumedSearch": []}
    assert hm.map_legacy("bare-name", {"hash": "#x"})["hash"] == {"id": "x"}
    assert hm.map_legacy(["bare-name"], {}) == {"hash": {}, "consumedSearch": []}
