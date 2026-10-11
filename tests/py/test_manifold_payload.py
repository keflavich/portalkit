"""portalkit.legacy.manifold_payload: legacy manifold DATA -> CatalogSpec + ManifoldSpec."""

import copy
import json
from pathlib import Path

import numpy as np
import pytest

from portalkit.legacy.manifold_payload import (LIMIT_SUFFIX, ManifoldPayloadError, convert,
                                                limit_code, preset_frame)
from portalkit.spec import CatalogSpec, ManifoldSpec

SNIP = Path(__file__).resolve().parents[1] / "fixtures" / "legacy_snippets"


@pytest.fixture
def payload():
    return json.loads((SNIP / "manifold_payload.json").read_text())


def test_limit_codes():
    assert [limit_code(x) for x in ("v", "lo", "up", None, "u", "l", "")] == \
        ["", "l", "u", "", "u", "l", ""]


def test_convert_columns_and_limits(payload):
    cat, mf = convert(payload)
    assert isinstance(cat, CatalogSpec) and isinstance(mf, ManifoldSpec)
    assert cat.manifold is mf
    assert cat.id == "n" and cat.ra == "ra" and cat.dec == "dec"
    assert len(cat.table) == 8
    assert list(cat.table["n"]) == [f"M{i}" for i in range(8)]
    assert mf.dims == ["tau3", "co", "h2o"]
    cols = {c.key: c for c in cat.columns}
    for d in mf.dims:
        assert cols[d].role == "value"
        assert cols[d].limit == d + LIMIT_SUFFIX
        assert cols[d + LIMIT_SUFFIX].role == "limit"
    lim = list(cat.table["tau3" + LIMIT_SUFFIX])
    assert lim[3] == "l" and lim[5] == "u" and lim[0] == ""
    assert lim[2] == ""                                   # missing value -> no limit code
    assert np.ma.is_masked(cat.table["tau3"][2])


def test_log_dims_and_inverted_scale(payload):
    cat, _ = convert(payload)
    cols = {c.key: c for c in cat.columns}
    assert cols["co"].log is False                        # stored as log10 base values
    assert cols["co"].label.startswith("log10")
    assert "log10" in cols["co"].note
    assert cols["co"].invert is True                      # negative legacy scale
    assert cols["h2o"].unit == "1e18 cm-2"


def test_categories_groups_presets(payload):
    cat, mf = convert(payload)
    cols = {c.key: c for c in cat.columns}
    assert cols["verdict"].role == "category"
    assert cols["verdict"].levels == ["ice", "noice", "unsure"]
    assert cols["verdict"].colors == {"ice": "#06c", "noice": "#c60"}
    assert [(g.key, g.dims) for g in cat.groups] == [("ice", ["tau3", "co"]), ("col", ["h2o"])]
    kinds = {p.key: p.kind for p in cat.presets}
    assert kinds == {"ax": "axes", "pca": "frame"}       # 'live' has no stored weights
    ax = next(p for p in cat.presets if p.key == "ax")
    assert ax.data["x"] == "tau3" and ax.data["y"] == "co" and ax.data["color"] == "verdict"
    assert list(mf.frames) == ["pca"] and mf.default_frame == "pca"
    assert len(mf.frames["pca"]) == 2 * len(mf.dims)


def test_preset_frame_rescales_weights():
    F = preset_frame({"a": 1.0}, {"b": 2.0}, ["a", "b"], {"a": 0.5, "b": 1.0})
    assert F == [0.5, 0.0, 0.0, 2.0]


def test_id_field_override_and_default_frame(payload):
    for i, s in enumerate(payload["stars"]):
        s["alt"] = f"X{i}"
    cat, mf = convert(payload, id_field="alt", key="m", title="T", default_frame="ax")
    assert cat.id == "alt" and list(cat.table["alt"])[:2] == ["X0", "X1"] and cat.key == "m" and cat.title == "T"
    assert mf.default_frame == "ax"


def test_dim_named_like_star_field(payload):
    p = copy.deepcopy(payload)
    p["dims"][2]["key"] = "obs"
    cat, mf = convert(p)
    assert "obs" in {c.key for c in cat.columns}
    assert mf.dims[2] != "obs" and mf.dims[2].startswith("obs")


@pytest.mark.parametrize("mutate, msg", [
    (lambda p: p.pop("stars"), "stars"),
    (lambda p: p.update(stars=[]), "stars"),
    (lambda p: p.pop("dims"), "dims"),
    (lambda p: p["dims"][0].update(v=[1, 2]), "one value per star"),
    (lambda p: p["dims"][0].update(k=["v"]), "one code per star"),
    (lambda p: p["dims"][0].pop("key"), "key"),
    (lambda p: [s.pop("n") for s in p["stars"]] and [s.pop("obs") for s in p["stars"]], "id field"),
])
def test_errors(payload, mutate, msg):
    mutate(payload)
    with pytest.raises(ManifoldPayloadError, match=msg):
        convert(payload)


def test_not_a_mapping():
    with pytest.raises(ManifoldPayloadError):
        convert([1, 2])
    assert issubclass(ManifoldPayloadError, ValueError)
