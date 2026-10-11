"""portalkit.legacy.extract: embedded payloads of legacy pages (section 9.2 Tier A)."""

import math
from pathlib import Path

import pytest

from portalkit.legacy import extract
from portalkit.legacy.extract import LegacyExtractError

SNIP = Path(__file__).resolve().parents[1] / "fixtures" / "legacy_snippets"


def test_assign_const_with_nan():
    data = extract.payload(SNIP / "assign_const.html")
    assert [r["id"] for r in data["rows"]] == ["S1", "S2"]
    assert math.isnan(data["rows"][1]["f200w"])
    # string contents with '};' and '</b>' do not end the payload
    assert data["meta"]["note"] == "contains }; and </b> in a string"
    assert data["meta"]["title"] == "Legacy éxplorer"


def test_window_portal():
    data = extract.payload(SNIP / "window_portal.html")
    assert data["version"] == 2
    assert [p["id"] for p in data["pages"]] == ["index", "map"]


def test_script_json_and_trailing_semicolon():
    found = extract.payloads(SNIP / "script_json.html")
    assert [(e["style"], e["name"]) for e in found] == [("script", "gallery-data"),
                                                        ("script", "settings")]
    assert found[1].value == {"theme": "dark"}
    assert extract.payload(SNIP / "script_json.html")["count"] == 2
    assert extract.payload(SNIP / "script_json.html", ids=("settings",)) == {"theme": "dark"}
    assert extract.script_json(SNIP / "script_json.html", "gallery-data")["count"] == 2
    # script_json is strict about the element body
    with pytest.raises(LegacyExtractError):
        extract.script_json(SNIP / "script_json.html", "settings")
    with pytest.raises(LegacyExtractError):
        extract.script_json(SNIP / "script_json.html", "missing")


def test_non_json_assignments_are_skipped():
    found = extract.payloads(SNIP / "nonjson_then_script.html")
    assert [(e["style"], e["name"]) for e in found] == [("script", "pk-legacy")]
    assert extract.payload(SNIP / "nonjson_then_script.html") == {"rows": [{"id": "X1"}]}


def test_no_payload_raises():
    assert extract.payloads(SNIP / "no_payload.html") == []
    with pytest.raises(LegacyExtractError):
        extract.payload(SNIP / "no_payload.html")
    assert issubclass(LegacyExtractError, ValueError)


def test_offsets_delimit_json_text():
    text = (SNIP / "window_portal.html").read_text()
    (e,) = extract.payloads(SNIP / "window_portal.html")
    assert text[e["start"]] == "{"
    assert text[e["end"] - 1] == "}"


def test_html_text_and_names():
    html = '<script>let CFG = {"a": [1, 2]}; const DATA = {"b": 1};</script>'
    assert extract.payload(html) == {"b": 1}
    assert extract.payload(html, names=("CFG",)) == {"a": [1, 2]}
    assert len(extract.payloads(html, names=("CFG", "DATA"))) == 2
    assert extract.read_text(html) == html
    assert extract.read_text(SNIP / "window_portal.html").startswith("<!doctype html>")


def test_style_filter():
    html = ('<script>window.PORTAL = {"w": 1};</script>'
            '<script type="application/json" id="s">{"s": 1}</script>')
    assert extract.payload(html) == {"w": 1}
    assert extract.payload(html, style="script") == {"s": 1}
    with pytest.raises(LegacyExtractError):
        extract.payload(html, style="assign")


def test_manifold_page():
    data = extract.payload(SNIP / "manifold.html")
    assert len(data["stars"]) == 8 and len(data["dims"]) == 3
