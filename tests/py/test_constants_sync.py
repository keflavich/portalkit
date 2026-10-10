"""Constants sync and vendored-file integrity (ARCHITECTURE.md sections 6.11 and 6.12).

* ``portalkit/constants.py`` == ``web/js/core/constants.js`` == the section 6.12 table.
* Every vendored GC module hashes to the sha256 recorded in ``VENDORED.md``.
* Undoing the 3-line theme patch in ``renderer.js`` reproduces the recorded source sha256,
  so the patch is the only change.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest

from portalkit import constants

REPO = Path(__file__).resolve().parents[2]
JS = REPO / "web/js/core/constants.js"
DOC = REPO / "docs/ARCHITECTURE.md"
VENDORED = REPO / "VENDORED.md"

_JS_CONST = re.compile(r"^export const ([A-Z][A-Z0-9_]*) = (-?[0-9][0-9_.eE+-]*);\s*(?://.*)?$")

RENDERER_PATCH = {
    498: ("      const rgb = (color.colors && color.colors[c]) || NEUTRAL_RGB;",
          "      const rgb = (color.colors && color.colors[c]) || p.neutral || NEUTRAL_RGB;"),
    502: ("    gl.uniform3fv(u.u_neutral, NEUTRAL_RGB);",
          "    gl.uniform3fv(u.u_neutral, p.neutral || NEUTRAL_RGB);"),
    503: ("    gl.uniform3fv(u.u_selColor, SELECTION_RGB);",
          "    gl.uniform3fv(u.u_selColor, p.selColor || SELECTION_RGB);"),
}

EXPECTED_VENDORED = {
    "web/js/vendor/gc/gl/renderer.js", "web/js/vendor/gc/gl/shaders.js",
    "web/js/vendor/gc/gl/colormaps.js", "web/js/vendor/gc/config/style.js",
    "web/js/vendor/gc/math/frame.js", "web/js/vendor/gc/math/linalg.js",
    "web/js/vendor/gc/workers/kernel.js", "web/js/vendor/gc/app/pick.js",
}


def _num(text: str) -> int | float:
    v = float(text)
    if re.fullmatch(r"-?[0-9]+", text):
        return int(text)
    return v


def parse_js_constants(text: str) -> dict[str, int | float]:
    out: dict[str, int | float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("export"):
            continue
        m = _JS_CONST.match(line)
        assert m, f"unparseable export in constants.js: {line!r}"
        assert m.group(1) not in out, f"duplicate {m.group(1)}"
        out[m.group(1)] = _num(m.group(2))
    return out


def parse_doc_constants(text: str) -> dict[str, int | float]:
    i = text.index("### 6.12 Constants")
    block = text[i:].split("```", 2)[1]
    out: dict[str, int | float] = {}
    for name, val in re.findall(r"([A-Z][A-Z0-9_]*)=([0-9][0-9.eE+-]*)", block):
        out[name] = _num(val)
    return out


def parse_vendored_table(text: str) -> dict[str, dict[str, str]]:
    rows = {}
    pat = re.compile(r"^\| `(web/js/vendor/gc/[^`]+)` \| `([^`]+)` \| `([0-9a-f]{64})` \| "
                     r"`([0-9a-f]{64})` \| ([^|]+) \|$")
    for line in text.splitlines():
        m = pat.match(line.strip())
        if m:
            rows[m.group(1)] = {"source": m.group(2), "source_sha256": m.group(3),
                                "sha256": m.group(4), "patch": m.group(5).strip()}
    return rows


def _same(a: int | float, b: int | float) -> bool:
    """Exact equality with matching int/float kind (0.12 must not become 0 or '0.120')."""
    return type(a) is type(b) and a == b


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

def test_python_all_matches_module_attributes():
    for name, val in constants.ALL.items():
        assert getattr(constants, name) == val
    public = {n for n in dir(constants) if re.fullmatch(r"[A-Z][A-Z0-9_]*", n) and n != "ALL"}
    assert public == set(constants.ALL)


def test_python_matches_contract_table():
    doc = parse_doc_constants(DOC.read_text(encoding="utf-8"))
    assert len(doc) == 35
    assert set(doc) == set(constants.ALL)
    bad = {k: (doc[k], constants.ALL[k]) for k in doc if not _same(doc[k], constants.ALL[k])}
    assert not bad, f"constants.py differs from section 6.12: {bad}"


def test_js_matches_python():
    js = parse_js_constants(JS.read_text(encoding="utf-8"))
    assert set(js) == set(constants.ALL), set(js) ^ set(constants.ALL)
    bad = {k: (js[k], constants.ALL[k]) for k in js if not _same(js[k], constants.ALL[k])}
    assert not bad, f"constants.js differs from constants.py: {bad}"


def test_js_parser_rejects_garbage():
    with pytest.raises(AssertionError):
        parse_js_constants("export const X = foo();\n")
    with pytest.raises(AssertionError):
        parse_js_constants("export const X = 1;\nexport const X = 2;\n")
    assert parse_js_constants("// c\nexport const A_B = 0.12;\nexport const C = 3;") == {"A_B": 0.12, "C": 3}


def test_js_module_evaluates_to_same_values(node_bin):
    """Import constants.js in node (the real ES module) and compare its exports."""
    code = ("import * as c from %s; process.stdout.write(JSON.stringify(c));"
            % json.dumps(JS.as_uri()))
    res = subprocess.run([node_bin, "--input-type=module", "-e", code],
                         capture_output=True, text=True, timeout=60, check=False)
    assert res.returncode == 0, res.stderr
    got = json.loads(res.stdout)
    assert set(got) == set(constants.ALL)
    for k, v in constants.ALL.items():
        assert got[k] == v, k


def test_derived_relations():
    c = constants
    assert c.T0_MAX < c.T1_MAX < c.MOTION_PREFIX
    assert c.LOD_BASE < c.LOD_MIN_N <= c.CHUNK_ROWS
    assert c.SHARD_TARGET < c.SHARD_MAX < c.SHARD_LRU_BYTES
    assert c.PAGE_JSON_MAX < c.PAGE_HTML_MAX
    assert c.FIRST_PAINT_MAX_1M < c.FIRST_PAINT_MAX_5M
    assert c.GPU_BUDGET_BYTES == 256 * 2**20 and c.UNDO_BYTES == 64 * 2**20
    assert c.SHARD_LRU_BYTES == 16 * 2**20 == c.COL_FILE_MAX


# ---------------------------------------------------------------------------
# vendored files
# ---------------------------------------------------------------------------

def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def test_vendored_table_complete():
    rows = parse_vendored_table(VENDORED.read_text(encoding="utf-8"))
    assert set(rows) == EXPECTED_VENDORED
    on_disk = {p.relative_to(REPO).as_posix() for p in (REPO / "web/js/vendor/gc").rglob("*") if p.is_file()}
    assert on_disk == EXPECTED_VENDORED, on_disk ^ EXPECTED_VENDORED


@pytest.mark.parametrize("rel", sorted(EXPECTED_VENDORED))
def test_vendored_hash(rel):
    rows = parse_vendored_table(VENDORED.read_text(encoding="utf-8"))
    data = (REPO / rel).read_bytes()
    assert _sha(data) == rows[rel]["sha256"], f"{rel} changed; update VENDORED.md deliberately"
    if rel.endswith("renderer.js"):
        assert rows[rel]["source_sha256"] != rows[rel]["sha256"]
    else:
        assert rows[rel]["patch"] == "none"
        assert rows[rel]["source_sha256"] == rows[rel]["sha256"], "only renderer.js is patched"


def test_renderer_patch_is_only_change():
    rel = "web/js/vendor/gc/gl/renderer.js"
    rows = parse_vendored_table(VENDORED.read_text(encoding="utf-8"))
    text = (REPO / rel).read_bytes().decode("utf-8")
    lines = text.split("\n")
    for ln, (old, new) in RENDERER_PATCH.items():
        assert lines[ln - 1] == new, f"line {ln}: {lines[ln - 1]!r}"
        lines[ln - 1] = old
    unpatched = "\n".join(lines).encode("utf-8")
    assert _sha(unpatched) == rows[rel]["source_sha256"]
    # the patched method is _composite
    start = max(i for i, l in enumerate(text.split("\n")[:498]) if "_composite(" in l and "{" in l)
    assert "_composite(acc, p, info)" in text.split("\n")[start]


def test_vendored_modules_import_only_vendored_siblings():
    base = REPO / "web/js/vendor/gc"
    spec = re.compile(r"""(?:import|export)\s[^'"]*?from\s+['"]([^'"]+)['"]""", re.S)
    for rel in EXPECTED_VENDORED:
        path = REPO / rel
        for target in spec.findall(path.read_text(encoding="utf-8")):
            assert target.startswith("."), f"{rel}: non-relative import {target!r}"
            resolved = (path.parent / target).resolve()
            assert resolved.is_file() and base in resolved.parents, f"{rel}: {target!r} not vendored"


def test_package_version_has_single_source():
    """pyproject.toml reads the version from portalkit.version.VERSION (section 15 bumps it there)."""
    import tomllib

    from portalkit import version

    proj = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert "version" not in proj["project"]
    assert "version" in proj["project"]["dynamic"]
    assert proj["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "portalkit.version.VERSION"}
    assert re.fullmatch(r"\d+\.\d+\.\d+", version.VERSION)
