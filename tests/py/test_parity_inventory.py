"""Parity of the capability resolver with the legacy inventory (ARCHITECTURE.md section 14).

``tests/data/inventory_caps.json`` maps ``family/legacy_page`` to the capabilities the legacy
page has (from ``docs/inventory.json``; the six extras rows carry ``[]``).
``tests/data/port_table.json`` holds the section 14 rows plus ``key`` and ``archetype_base``.

For every row that is rebuilt, the page is realised from its data flags with
``make_synthetic.specs_for_flags`` and resolved: the enabled set must contain every legacy
capability, and the flags the resolver derives must contain the row's flags.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

import pytest

from portalkit import caps as capsmod

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "tests" / "data"
DOCS = REPO / "docs"
sys.path.insert(0, str(REPO / "tests" / "fixtures"))
import make_synthetic as ms  # noqa: E402

pytestmark = pytest.mark.unit

PORT = json.loads((DATA / "port_table.json").read_text(encoding="utf-8"))
INV = json.loads((DATA / "inventory_caps.json").read_text(encoding="utf-8"))
REBUILT = [r for r in PORT if r["archetype_base"] != "excluded"]
ARCHETYPES = {"landing", "explorer", "manifold", "gallery", "spectra", "wavelength", "narrative"}


def _strip(s: str) -> str:
    return re.sub(r"\s*\(.*\)\s*$", "", s).strip()


def test_table_shape():
    assert len(PORT) == 93
    fams = Counter(r["family"] for r in PORT)
    assert len(fams) == 11
    assert sum(1 for r in PORT if r["family"] == "extras") == 6
    assert len({r["key"] for r in PORT}) == len(PORT)
    assert set(INV) == {r["key"] for r in PORT}
    for r in PORT:
        assert r["key"] == f"{r['family']}/{r['legacy_page']}"
        assert r["archetype_base"] in ARCHETYPES | {"excluded"}, r["archetype"]
        assert set(r["flags"]) <= set(capsmod.FLAGS), r["key"]
        assert set(INV[r["key"]]) <= set(capsmod.CAPS), r["key"]


def test_matches_docs():
    """The test copies agree with docs/port_table.json and docs/inventory.json."""
    docs_pt = json.loads((DOCS / "port_table.json").read_text(encoding="utf-8"))
    strip_keys = [{k: v for k, v in r.items() if k not in ("key", "archetype_base")} for r in PORT]
    assert strip_keys == docs_pt
    inv = json.loads((DOCS / "inventory.json").read_text(encoding="utf-8"))
    pages = [(rep["family"], pg["page"], pg["capabilities"]) for rep in inv["reports"] for pg in rep["pages"]]
    assert len(pages) == 87
    exact = {(f, p): c for f, p, c in pages}
    stripped: dict[tuple[str, str], list] = {}
    for f, p, c in pages:
        stripped.setdefault((f, _strip(p)), []).append(c)
    used = 0
    for r in PORT:
        k = (r["family"], r["legacy_page"])
        caps = exact.get(k)
        if caps is None:
            cand = stripped.get((r["family"], _strip(r["legacy_page"])), [])
            caps = cand[0] if len(cand) == 1 else None
        if r["family"] == "extras":
            assert caps is None and INV[r["key"]] == []
            continue
        assert caps is not None, r["key"]
        assert sorted(caps) == INV[r["key"]], r["key"]
        used += 1
    assert used == 87


def test_fill_equals_rules_minus_legacy():
    for r in REBUILT:
        en = capsmod.caps_from_flags(r["flags"])
        legacy = set(INV[r["key"]])
        assert legacy <= en, (r["key"], sorted(legacy - en))
        assert en - legacy == set(r["capabilities_to_fill"]), r["key"]
        assert len(r["capabilities_to_fill"]) == len(set(r["capabilities_to_fill"]))


def test_fill_total():
    """Section 14 states 93 rows, 11 families and 1517 capability fills."""
    assert sum(len(r["capabilities_to_fill"]) for r in PORT) == 1517


@pytest.mark.parametrize("row", REBUILT, ids=[r["key"] for r in REBUILT])
def test_resolver_superset_of_legacy(row):
    page, ds, manifests = ms.specs_for_flags(row["flags"], row["archetype_base"])
    res = capsmod.resolve(page, ds, manifests)
    enabled = set(res.enabled)
    legacy = set(INV[row["key"]])
    assert legacy <= enabled, f"{row['key']}: resolver lacks {sorted(legacy - enabled)}"
    assert set(row["flags"]) <= set(res.flags), f"{row['key']}: flags {sorted(set(row['flags']) - set(res.flags))} not derived"
    assert set(capsmod.ALWAYS) <= enabled
