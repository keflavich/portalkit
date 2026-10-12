"""WPE6: contract issues from the wave-3/4 adapter reviews and WPE4 leftovers.

One test group per item of the WPE6 work package (docs/wps.json):

1. foreign record sets (contract A): spec, schema, config ref, build (no shards), visibility,
   ``portalkit check``, ``verify --http`` HEAD of the foreign index;
2. legacy ``dim_alias`` (contract B) and the ``spherex-colours`` grammar name (C);
3. axes presets as the frame of a manifold page's initial state;
4. HST WFC3/ACS filters in the registry, raw VOTables in the SVO cache, carry-over;
5. virtual colours from legacy manifold payloads;
6. template ``high`` records under the soft size limit after ``extend_to_um``;
7. ``sedcurve`` records with any channel set;
8. all-missing catalog fields typed by role;
9. checklist Filename matched by path suffix;
10. ``check_manifest_denied`` 5xx with a portalkit ``.htaccess``; non-UTF-8 owned
    ``.htaccess``.
"""

from __future__ import annotations

import gzip
import json
import sys
import textwrap
import uuid
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from portalkit import caps, cli, config, schema
from portalkit import build as buildmod
from portalkit.deploy import ExposureMap, ExposureRefusal
import portalkit.deploy.verify  # noqa: F401  (the package re-exports a verify() function)
from portalkit.legacy.extract import script_json
from portalkit.spec import (ForeignRecords, LegacyRef, PageSpec, Preset, RecordSetSpec,
                            catalog_from_rows)

PORTAL = {"id": "test", "title": "Test portal", "visibility": "public"}
PUB = "https://data.rc.ufl.edu/pub/adamginsburg/"
SEC = "https://data.rc.ufl.edu/secure/adamginsburg/"


def _cat(n=8, key="src"):
    rows = [{"name": f"s{i}", "ra": 266.0 + 0.001 * i, "dec": -29.0, "f182m": 15.0 + 0.1 * i,
             "f405n": 13.5 + 0.01 * i, "f410m": 13.0 + 0.05 * i, "alias": f"W{i:03d}"}
            for i in range(n)]
    roles = {"f182m": "mag", "f405n": "mag", "f410m": "mag", "alias": "text"}
    cols = {m: {"filter": f"JWST/NIRCam.{m.upper()}"} for m in ("f182m", "f405n", "f410m")}
    return catalog_from_rows(rows, id="name", ra="ra", dec="dec", roles=roles, columns=cols,
                             key=key, title="Sources")


def _assemble(page, ds, manifests=None, **kw):
    manifests = manifests or {}
    res = caps.resolve(page, ds, manifests)
    kw.setdefault("portal", PORTAL)
    return config.assemble(page, ds, manifests, res, **kw)


def _foreign(base="../../other/unified/data/", key="spec", id_column="alias", kind="pks1"):
    return RecordSetSpec(key="spec", kind=kind, catalog="src", ids=[], loader=lambda i: None,
                         foreign=ForeignRecords(base=base, key=key, id_column=id_column))


def _spectra_page():
    return PageSpec(id="sp", archetype="spectra", title="Spectra", datasets=["src", "spec"])


# ---------------------------------------------------------------------------
# 1. foreign record sets
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("base, ok", [
    ("../../wd2_2640/unified/data/", True),
    ("data2/", True),
    (PUB + "wd2/unified/data/", True),
    ("../../wd2_2640/unified/data", False),          # no trailing '/'
    ("http://data.rc.ufl.edu/pub/x/data/", False),   # absolute bases are https only
    ("ftp://host/x/data/", False),
    ("/orange/adamginsburg/x/data/", False),         # absolute filesystem path
    ("file:///orange/x/data/", False),
    ("C:/x/data/", False),
    ("..\\x\\data/", False),
    ("", False),
])
def test_foreign_base_error(base, ok):
    assert (schema.foreign_base_error(base) is None) is ok


def test_foreign_records_ref_and_page_valid():
    spec = _foreign()
    ref = config.foreign_records_ref("spec", spec)
    assert ref == {"kind": "records", "url": "../../other/unified/data/spec/index.json",
                   "catalog": "src", "record": "pks1", "base": "../../other/unified/data/",
                   "id_column": "alias"}
    assert config.dataset_ref("spec", spec)["base"] == ref["base"]
    with pytest.raises(ValueError, match="one path segment"):
        config.foreign_records_ref("spec", _foreign(key="a/b"))
    with pytest.raises(ValueError, match="base"):
        config.foreign_records_ref("spec", _foreign(base="/orange/x/"))
    doc = _assemble(_spectra_page(), {"src": _cat(), "spec": spec})
    assert schema.validate_page(doc) == []
    assert doc["datasets"]["spec"]["base"] == "../../other/unified/data/"
    assert doc["datasets"]["spec"]["id_column"] == "alias"
    # schema: id_column only with base; url must lie under base
    bad = json.loads(json.dumps(doc))
    del bad["datasets"]["spec"]["base"]
    assert any("id_column" in e for e in schema.validate_page(bad))
    bad = json.loads(json.dumps(doc))
    bad["datasets"]["spec"]["url"] = "data/spec/index.json"
    assert schema.validate_page(bad)


def test_check_foreign_url():
    assert config.check_foreign_url("../../a/data/spec/index.json") is None
    assert config.check_foreign_url("/orange/a/index.json") is not None
    assert config.check_foreign_url("file:///a/index.json") is not None


def test_public_page_rejects_secure_foreign_url():
    spec = _foreign(base=SEC + "other/unified/data/")
    with pytest.raises(config.ConfigError) as ei:
        _assemble(_spectra_page(), {"src": _cat(), "spec": spec})
    assert any("secure" in e for e in ei.value.errors), ei.value.errors
    doc = _assemble(_spectra_page(), {"src": _cat(), "spec": spec},
                    portal={**PORTAL, "visibility": "secure"})
    assert doc["datasets"]["spec"]["base"] == SEC + "other/unified/data/"


@pytest.mark.parametrize("vis, url, base, ok", [
    ("public", PUB + "a/unified/", "../../b/unified/data/", True),
    ("public", PUB + "a/unified/", SEC + "b/unified/data/", False),
    ("secure", SEC + "a/unified/", PUB + "b/unified/data/", True),
    ("secure", SEC + "a/unified/", "../../b/unified/data/", True),
    ("public", PUB + "a/unified/", "https://example.org/x/data/", False),   # other host
])
def test_foreign_records_exposure(vis, url, base, ok):
    portal = SimpleNamespace(id="p", url=url, visibility=vis)
    spec = _foreign(base=base)
    if ok:
        resolved = buildmod.foreign_records_exposure(portal, "spec", spec)
        assert resolved.endswith("/spec/index.json") and resolved.startswith("https://")
    else:
        with pytest.raises(ExposureRefusal):
            buildmod.foreign_records_exposure(portal, "spec", spec)


ADAPTER_SRC = '''
import os
import numpy as np
from portalkit.spec import (ForeignRecords, RecordSetSpec, ReadmeSpec, catalog_from_rows,
                            standard_pages, TierUnavailable)


class Adapter:
    name = "wpe6smoke"
    tiers = ("A", "B")

    def datasets(self, ctx):
        rows = [{"id": f"S{i:03d}", "ra": 266.0 + 0.01 * i, "dec": -29.0 + 0.01 * i,
                 "f200w": 15 + 0.1 * i, "f444w": 13 + 0.05 * i, "alias": f"W{i}"}
                for i in range(12)]
        cat = catalog_from_rows(rows, "id", "ra", "dec",
                                roles={"f200w": "mag", "f444w": "mag", "alias": "text"},
                                columns={"f200w": {"filter": "JWST/NIRCam.F200W"},
                                         "f444w": {"filter": "JWST/NIRCam.F444W"}},
                                key="src", title="Smoke sources")
        base = os.environ.get("PK_WPE6_BASE", "../../other/unified/data/")
        idc = os.environ.get("PK_WPE6_IDCOL", "alias")
        return {"src": cat,
                "spec": RecordSetSpec(key="spec", kind="pks1", catalog="src",
                                      ids=[r["id"] for r in rows], loader=lambda i: None,
                                      foreign=ForeignRecords(base=base, key="spec",
                                                             id_column=idc)),
                "readme": ReadmeSpec(markdown="# Smoke\\n\\nWPE6 test portal.")}

    def pages(self, ctx, ds):
        return standard_pages(ds, spectra=True)


ADAPTER = Adapter()
'''


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("wpe6")
    mod = f"pk_wpe6_adapter_{uuid.uuid4().hex[:8]}"
    adir = root / "adapters"
    adir.mkdir()
    (adir / f"{mod}.py").write_text(ADAPTER_SRC)
    legacy = root / "legacy"
    legacy.mkdir()
    (legacy / "old.html").write_text("<html></html>")
    sites = root / "sites"
    sites.mkdir()
    (sites / "wpe6smoke.toml").write_text(textwrap.dedent(f'''
        family = "wpe6smoke"
        adapter = "{mod}:ADAPTER"
        tier_default = "A"

        [[portal]]
        id = "main"
        title = "WPE6 smoke portal"
        visibility = "public"
        legacy_dir = "{legacy}"
        url = "https://data.rc.ufl.edu/pub/adamginsburg/wpe6smoke/unified/"

        [[portal.page]]
        id = "explorer"
        legacy = ["old.html"]
        grammar = ["kv-view"]
        dim_alias = {{m_F200W = "f200w", c_F200W_F444W = "f444w"}}
        '''))
    stage = root / "stage"
    stage.mkdir()
    emap = ExposureMap(roots={}, url_prefixes={})
    mp = pytest.MonkeyPatch()
    mp.syspath_prepend(str(adir))
    mp.setenv("PK_CACHE_DIR", str(root / "cache"))
    mp.setenv("PK_STAGE_ROOT", str(stage))
    mp.setenv("PK_SITES_DIR", str(sites))
    mp.delenv("PK_WPE6_BASE", raising=False)
    mp.delenv("PK_WPE6_IDCOL", raising=False)
    mp.setattr(cli, "_emap", lambda: emap)
    yield {"root": root, "sites": sites, "stage": stage, "mp": mp}
    mp.undo()
    sys.modules.pop(mod, None)


def _build(env):
    code = cli.main(["--sites-dir", str(env["sites"]), "build", "wpe6smoke", "--allow-tmp-inputs",
                     "--stage-root", str(env["stage"])])
    builds = sorted((p for p in (env["stage"] / "wpe6smoke").iterdir() if p.is_dir()),
                    key=lambda p: p.stat().st_mtime_ns) if (env["stage"] / "wpe6smoke").is_dir() else []
    return code, (builds[-1] / "main" if builds else None)


@pytest.fixture(scope="module")
def built(env):
    code, portal = _build(env)
    return {"code": code, "portal": portal}


def test_build_writes_no_foreign_shards(built):
    assert built["code"] == 0
    portal = built["portal"]
    assert not (portal / "data" / "spec").exists()
    page = script_json(portal / "spectra.html", "pk-page")
    ref = page["datasets"]["spec"]
    assert ref["base"] == "../../other/unified/data/"
    assert ref["url"] == "../../other/unified/data/spec/index.json"
    assert ref["id_column"] == "alias"
    man = json.loads((portal / "data" / "src" / "manifest.json").read_text())
    assert man["links"]["records"]["spec"] == "../../../../other/unified/data/spec/index.json"
    rep = json.loads((portal / "build_report.json").read_text())
    dsr = json.dumps(rep)
    assert '"foreign": true' in dsr


def test_site_dim_alias_reaches_page(built):
    page = script_json(built["portal"] / "explorer.html", "pk-page")
    assert page["page"]["legacy"]["dim_alias"] == {"m_F200W": "f200w", "c_F200W_F444W": "f444w"}
    assert schema.validate_page(page) == []


def test_check_accepts_foreign_and_rejects_local_paths(built, tmp_path):
    import shutil
    rep = buildmod.check_build(built["portal"], gate="G2")
    assert rep.ok, rep.errors
    bad = tmp_path / "abs"
    shutil.copytree(built["portal"], bad)
    html = (bad / "spectra.html").read_text()
    (bad / "spectra.html").write_text(html.replace("../../other/unified/data/",
                                                   "/orange/adamginsburg/other/data/"))
    assert not buildmod.check_build(bad, gate="G2").ok


def test_public_build_refuses_secure_base(env):
    env["mp"].setenv("PK_WPE6_BASE", SEC + "other/unified/data/")
    try:
        code, _ = _build(env)
    finally:
        env["mp"].delenv("PK_WPE6_BASE")
    assert code == 3


def test_build_rejects_unknown_id_column(env):
    env["mp"].setenv("PK_WPE6_IDCOL", "no_such_column")
    try:
        code, _ = _build(env)
    finally:
        env["mp"].delenv("PK_WPE6_IDCOL")
    assert code != 0


def _pk_page_html(path: Path, page: dict) -> None:
    path.write_text('<html><head><script type="application/json" id="pk-page">'
                    + json.dumps(page) + "</script></head></html>")


@pytest.mark.parametrize("vis, auth, status, ok, note", [
    ("public", None, 200, True, False),
    ("public", None, 404, False, False),
    ("secure", None, 401, True, True),
    ("secure", "u:p", 401, False, False),
    ("secure", "u:p", 200, True, False),
])
def test_verify_heads_foreign_index(tmp_path, monkeypatch, vis, auth, status, ok, note):
    vmod = sys.modules["portalkit.deploy.verify"]
    _pk_page_html(tmp_path / "spectra.html", {"datasets": {
        "src": {"kind": "catalog", "url": "data/src/manifest.json"},
        "spec": {"kind": "records", "url": "../../other/unified/data/spec/index.json",
                 "base": "../../other/unified/data/", "catalog": "src", "record": "pks1"}}})
    seen = []

    def fake_fetch(url, *, auth=None, method="GET", timeout=None, tmpdir=None, opener=None):
        seen.append((url, method, auth))
        return status, {"Content-Type": "application/json"}, None, None, None

    monkeypatch.setattr(vmod, "_fetch", fake_fetch)
    base = (SEC if vis == "secure" else PUB) + "a/unified/"
    checks = vmod.check_foreign_records(base, tmp_path, {"spectra.html": {}}, visibility=vis,
                                        auth=auth)
    assert [c.url for c in checks] == [(SEC if vis == "secure" else PUB) + "other/unified/data/spec/index.json"]
    assert seen[0][1] == "HEAD"
    assert seen[0][2] == (auth if vis == "secure" else None)
    assert checks[0].ok is ok
    assert bool(checks[0].problems) is (not ok or note)


def test_verify_public_never_sends_auth(tmp_path, monkeypatch):
    vmod = sys.modules["portalkit.deploy.verify"]
    _pk_page_html(tmp_path / "p.html", {"datasets": {"spec": {
        "kind": "records", "url": "../x/data/spec/index.json", "base": "../x/data/",
        "catalog": "src", "record": "pks1"}}})
    sent = []
    monkeypatch.setattr(vmod, "_fetch", lambda url, **kw: (sent.append(kw.get("auth")) or
                                                           (200, {}, None, None, None)))
    vmod.check_foreign_records(PUB + "a/", tmp_path, {"p.html": {}}, visibility="public",
                               auth="u:p")
    assert sent == [None]


# ---------------------------------------------------------------------------
# 2. legacy dim alias and grammar names
# ---------------------------------------------------------------------------

def test_grammar_names():
    assert "spherex-colours" in schema.GRAMMARS


def _explorer(alias):
    return PageSpec(id="map", archetype="explorer", title="Map", datasets=["src"],
                    legacy=LegacyRef(files=["old.html"], grammar=["kv-view"], dim_alias=alias))


def test_dim_alias_validated_and_passed_through():
    alias = {"m_F182M": "f182m", "c_F182M_F410M": "f182m-f410m"}
    cat = _cat()
    from portalkit.spec import VirtualDim
    cat.virtual.append(VirtualDim("f182m-f410m", label="F182M - F410M", unit="mag"))
    doc = _assemble(_explorer(alias), {"src": cat})
    assert doc["page"]["legacy"]["dim_alias"] == alias
    assert schema.validate_page(doc) == []
    with pytest.raises(config.ConfigError) as ei:
        _assemble(_explorer({"m_F999W": "f999w"}), {"src": _cat()})
    assert any("dim_alias" in e and "f999w" in e for e in ei.value.errors), ei.value.errors


def test_schema_dim_alias_types():
    doc = _assemble(_explorer({"m_F182M": "f182m"}), {"src": _cat()})
    doc["page"]["legacy"]["dim_alias"] = {"m_F182M": 3}
    assert schema.validate_page(doc)


def test_hashmap_applies_dim_alias():
    from portalkit.legacy import hashmap
    page = {"page": {"legacy": {"dim_alias": {"m_F182M": "f182m", "c_A_B": "f405n"}}},
            "layout": {"row": [{"pane": "manifold", "id": "m", "dataset": "src",
                                "options": {"dims": ["f182m", "f405n", "f410m"]}}]},
            "datasets": {"src": {}}}
    out = hashmap.map_legacy(["kv-view"], {"hash": "#x=m_F182M&y=c_A_B&c=c_A_B", "page": page})
    h = out["hash"]
    assert hashmap.decode_frame(h["m.frame"]) == [1.0, 0, 0, 0, 1.0, 0]
    # the alias also reaches the colour key
    assert h["c"] == "f405n"
    plain = hashmap.map_legacy(["kv-view"], {"hash": "#x=m_F182M&y=c_A_B",
                                             "page": {**page, "page": {}}})
    assert "m.frame" not in plain["hash"]
    assert hashmap.dim_alias({"dim_alias": {"a": "b", "c": 1}}) == {"a": "b"}


# ---------------------------------------------------------------------------
# 3. axes presets as a manifold frame
# ---------------------------------------------------------------------------

def _manifold_page(options, state=None, cat=None):
    cat = cat or _cat()
    layout = {"row": [{"pane": "list", "dataset": "src"},
                      {"pane": "manifold", "id": "m", "dataset": "src", "options": options}]}
    page = PageSpec(id="mf", archetype="manifold", title="M", datasets=["src"], layout=layout,
                    state=state or {})
    return page, {"src": cat}


def test_axes_preset_accepted_as_manifold_frame():
    dims = ["f182m", "f405n", "f410m"]
    cat = _cat()
    cat.presets.append(Preset(key="ccd", label="CCD", kind="axes",
                              data={"x": "f182m", "y": "f410m"}))
    page, ds = _manifold_page({"dims": dims, "frame": "ccd"},
                              {"frame:m": {"preset": "ccd", "dims": dims}}, cat=cat)
    assert schema.validate_page(_assemble(page, ds)) == []
    page, ds = _manifold_page({"dims": dims}, {"frame:m": {"preset": "nope", "dims": dims}},
                              cat=cat)
    with pytest.raises(config.ConfigError) as ei:
        _assemble(page, ds)
    assert any("'nope'" in e and "axes preset" in e for e in ei.value.errors), ei.value.errors


# ---------------------------------------------------------------------------
# 4. HST filters
# ---------------------------------------------------------------------------

HST = ["HST/WFC3_IR.F110W", "HST/WFC3_IR.F125W", "HST/WFC3_IR.F127M", "HST/WFC3_IR.F139M",
       "HST/WFC3_IR.F153M", "HST/WFC3_IR.F160W", "HST/WFC3_UVIS2.F555W", "HST/WFC3_UVIS2.F814W",
       "HST/ACS_WFC.F555W", "HST/ACS_WFC.F814W"]


def test_hst_filters_in_registry():
    from portalkit.physics import filters as F
    reg = F.load_registry()
    for k in HST:
        r = reg[k]
        assert r["facility"] == "HST" and r["system"] == "Vega", k
        assert len(r["trans_w"]) == len(r["trans_t"]) > 10, k
    f160 = reg["HST/WFC3_IR.F160W"]
    assert f160["wl_um"] == pytest.approx(1.537, abs=2e-3)
    assert f160["zp_jy"] == pytest.approx(1122.8, rel=1e-4)
    assert f160["label"] == "WFC3 F160W"
    assert F.lookup("WFC3_F160W")["key"] == "HST/WFC3_IR.F160W"
    assert F.lookup("ACS_F814W")["key"] == "HST/ACS_WFC.F814W"
    assert 0.5 < reg["HST/ACS_WFC.F555W"]["wl_um"] < 0.56
    assert 0.79 < reg["HST/WFC3_UVIS2.F814W"]["wl_um"] < 0.83


_VOT = """<?xml version="1.0"?>
<VOTABLE version="1.1" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <RESOURCE type="results">
    <TABLE>
      <PARAM name="filterID" value="HST/WFC3_IR.F999X" datatype="char" arraysize="*"/>
      <PARAM name="WavelengthEff" value="15000.0" unit="Angstrom" datatype="double"/>
      <PARAM name="WidthEff" value="2000.0" unit="Angstrom" datatype="double"/>
      <PARAM name="WavelengthPivot" value="15100.0" unit="Angstrom" datatype="double"/>
      <PARAM name="MagSys" value="Vega" datatype="char" arraysize="*"/>
      <PARAM name="ZeroPoint" value="1100.0" unit="Jy" datatype="double"/>
      <FIELD name="Wavelength" unit="Angstrom" datatype="double"/>
      <FIELD name="Transmission" unit="" datatype="double"/>
      <DATA><TABLEDATA>
%s
      </TABLEDATA></DATA>
    </TABLE>
  </RESOURCE>
</VOTABLE>
"""


def test_votable_cache_and_carry_over(tmp_path):
    from astropy.table import Table
    from portalkit.physics import filters as F
    w = np.linspace(13000, 17000, 41)
    t = np.clip(1 - ((w - 15000) / 1800) ** 2, 0, None)
    rows = "\n".join(f"<TR><TD>{a}</TD><TD>{b:.4f}</TD></TR>" for a, b in zip(w, t))
    cache = tmp_path / "svo"
    cache.mkdir()
    (cache / "HST_WFC3_IR_F999X.xml").write_text(_VOT % rows)
    meta, curves = F.read_svo_cache(cache)
    assert "HST/WFC3_IR.F999X" in meta and "HST/WFC3_IR.F999X" in curves
    assert curves["HST/WFC3_IR.F999X"][0][0] == pytest.approx(1.3)
    tab = F.build_registry_table(cache, None, carry_over=F.REGISTRY_PATH)
    keys = list(tab["key"])
    assert "HST/WFC3_IR.F999X" in keys
    reg = Table.read(str(F.REGISTRY_PATH), format="ascii.ecsv")
    assert set(reg["key"]) <= set(keys)           # carried over unchanged
    r = tab[keys.index("HST/WFC3_IR.F999X")]
    assert r["label"] == "WFC3 F999X" and r["wl_um"] == pytest.approx(1.51)
    assert "WFC3_F999X" in str(r["aliases"]).split()
    alone = F.build_registry_table(cache, None, carry_over=None)
    assert list(alone["key"]) == ["HST/WFC3_IR.F999X"]


# ---------------------------------------------------------------------------
# 5. virtual colours from manifold payloads
# ---------------------------------------------------------------------------

def _payload(n=12, seed=1):
    rng = np.random.default_rng(seed)
    a = np.round(rng.uniform(14, 20, n), 3)
    b = np.round(rng.uniform(12, 18, n), 3)
    c = np.round(rng.uniform(10, 16, n), 3)
    a[4] = np.nan

    def L(x):
        return [None if not np.isfinite(v) else float(v) for v in x]
    dims = [{"key": "m_F162M", "unit": "mag", "v": L(a), "scale": 1.0, "range": [14, 20]},
            {"key": "m_F210M", "unit": "mag", "v": L(b), "scale": 1.0, "range": [12, 18]},
            {"key": "m_F444W", "unit": "mag", "v": L(c), "scale": 1.0},
            {"key": "c_F162M_F210M", "label": "F162M-F210M", "unit": "mag", "group": "col",
             "v": L(np.round(a - b, 3)), "scale": 0.5, "range": [0, 4]},
            {"key": "tau", "v": L(np.round(rng.uniform(0, 1, n), 3)), "scale": 0.2}]
    stars = [{"n": f"S{i}", "ra": 10 + i * 1e-3, "dec": -1.0} for i in range(n)]
    return {"stars": stars, "dims": dims, "groups": [["col", "Colours"]], "presets": [
        {"id": "ccd", "x": {"c_F162M_F210M": 1}, "y": {"m_F210M": 1}},
        {"id": "pca", "x": {"c_F162M_F210M": 0.5, "tau": 0.5}, "y": {"m_F444W": 1, "tau": -1}}]}


def test_manifold_payload_virtual_colours(tmp_path):
    from portalkit.bundle import catalog as C
    from portalkit.legacy.manifold_payload import convert
    cat, mf = convert(_payload(), density_min_n=5)
    cols = [c.key for c in cat.columns]
    assert "c_F162M_F210M" not in cols and "c_F162M_F210M__lim" not in cols
    assert [v.key for v in cat.virtual] == ["m_F162M-m_F210M"]
    v = cat.virtual[0]
    assert v.label == "F162M-F210M" and v.unit == "mag" and v.group == "col"
    assert mf.dims == ["m_F162M", "m_F210M", "m_F444W", "m_F162M-m_F210M", "tau"]
    pre = {p.key: p for p in cat.presets}
    assert pre["ccd"].kind == "axes" and pre["ccd"].data["x"] == "m_F162M-m_F210M"
    assert pre["pca"].data["dims"] == mf.dims and len(mf.frames["pca"]) == 10
    assert cat.groups[0].dims == ["m_F162M-m_F210M"]
    assert cat.density and cat.density[0].x == "m_F162M-m_F210M"
    C.write(cat, tmp_path / "mp")
    man = json.loads(next((tmp_path / "mp").rglob("manifest*.json")).read_text())
    assert [v["key"] for v in man["virtual"]] == ["m_F162M-m_F210M"]
    assert "m_F162M-m_F210M" in man["manifold"]["dims"]
    stored, _ = convert(_payload(), virtual_colours=False)
    assert "c_F162M_F210M" in [c.key for c in stored.columns] and not stored.virtual


def test_virtual_colour_detection_rules():
    from portalkit.legacy.manifold_payload import convert, detect_virtual_colours
    p = _payload()
    # a colour off by more than the rounding tolerance stays stored
    p["dims"][3]["v"][0] = p["dims"][3]["v"][0] + 0.01
    cat, _ = convert(p)
    assert not cat.virtual
    # a colour with limit codes stays stored
    p = _payload()
    p["dims"][3]["k"] = ["up"] + [None] * 11
    assert not convert(p)[0].virtual
    # missing pattern must follow the bases
    p = _payload()
    p["dims"][3]["v"][4] = 1.0
    assert not convert(p)[0].virtual
    # a magnitude is never expressed through a colour
    nan = np.nan
    m1 = np.array([10.125, 11.402, 12.731, 13.058])
    m2 = np.array([9.017, 9.553, 11.268, 11.904])
    dims = {"m1": {"v": m1, "unit": "mag"},
            "m2": {"v": m2, "unit": "mag"},
            "c12": {"v": np.round(m1 - m2, 3), "unit": "mag", "label": "m1 - m2"},
            "x": {"v": np.array([nan, 1.0, 2.0, 3.0]), "unit": ""}}
    assert detect_virtual_colours(dims, ["m1", "m2", "c12", "x"]) == {"c12": ("m1", "m2")}
    # ... also when the stored colour's key and label carry no colour marker: only colour-like
    # dims are candidates, so neither m2 = m1 - col12 nor col12 = m1 - m2 is virtualized
    plain = {"m1": dims["m1"], "m2": dims["m2"],
             "col12": {"v": np.round(m1 - m2, 3), "unit": "mag", "label": "colour 1"}}
    assert detect_virtual_colours(plain, ["m1", "m2", "col12"]) == {}


def test_virtual_colour_integer_dims_never_matched():
    from portalkit.legacy.manifold_payload import detect_virtual_colours
    a = np.array([10, 12, 15, 9, 20, 7, 11, 13], dtype=float)
    b = np.array([3, 5, 4, 2, 6, 1, 8, 2], dtype=float)
    off = np.array([1, -1, 0, 1, -1, 1, 0, -1], dtype=float)
    for key in ("nobs_f1", "n_tot-n_f2"):
        dims = {"nobs_tot": {"v": a, "unit": ""}, "nobs_f2": {"v": b, "unit": ""},
                key: {"v": a - b + off, "unit": ""}}
        assert detect_virtual_colours(dims, ["nobs_tot", key, "nobs_f2"]) == {}, key
    # an exact integer difference is not virtualized either (step 1 > MAX_QUANTUM)
    dims = {"n_a": {"v": a, "unit": ""}, "n_b": {"v": b, "unit": ""},
            "n_a-n_b": {"v": a - b, "unit": ""}}
    assert detect_virtual_colours(dims, ["n_a", "n_b", "n_a-n_b"]) == {}


def test_virtual_colour_duplicate_pair_served_once(tmp_path):
    from portalkit.bundle import catalog as C
    from portalkit.legacy.manifold_payload import convert
    p = _payload()
    dup = dict(p["dims"][3], key="c_F162M_F210M_b", label="F162M-F210M (copy)")
    p["dims"].insert(4, dup)
    p["groups"] = [{"key": "col", "label": "Colours", "dims": ["c_F162M_F210M", "c_F162M_F210M_b"]}]
    p["presets"].append({"id": "dupx", "x": {"c_F162M_F210M_b": 1}, "y": {"m_F210M": 1}})
    cat, mf = convert(p, density_min_n=5)
    cols = [c.key for c in cat.columns]
    assert "c_F162M_F210M_b" not in cols and "c_F162M_F210M_b__lim" not in cols
    assert [v.key for v in cat.virtual] == ["m_F162M-m_F210M"]
    assert mf.dims == ["m_F162M", "m_F210M", "m_F444W", "m_F162M-m_F210M", "tau"]
    assert cat.groups[0].dims == ["m_F162M-m_F210M"]
    pre = {q.key: q for q in cat.presets}
    assert pre["dupx"].kind == "axes" and pre["dupx"].data["x"] == "m_F162M-m_F210M"
    assert len(mf.frames["pca"]) == 2 * len(mf.dims)
    C.write(cat, tmp_path / "mp")
    man = json.loads(next((tmp_path / "mp").rglob("manifest*.json")).read_text())
    assert schema.validate_catalog(man) == []
    assert [v["key"] for v in man["virtual"]] == ["m_F162M-m_F210M"]


# ---------------------------------------------------------------------------
# 6. template records
# ---------------------------------------------------------------------------

def test_extension_decimation_keeps_brick2221_values():
    from portalkit.physics import templates as T
    w = list(np.exp(np.linspace(np.log(0.6), np.log(5.3), 400)))
    src = {"grids": {"g": {"w": w, "k31": list(np.linspace(1, 0.01, 400)),
                           "k55": list(np.linspace(1, 0.02, 400))}},
           "templates": [{"key": "a", "teff": 5000.0, "f": {"g": list(np.linspace(2, 1, 400))}}]}
    full = T.extend_templates(src, 26.0)
    dec = T.extend_templates(src, 26.0, min_dlog=3.5 * np.median(np.diff(np.log(w))))
    nf, nd = len(full["grids"]["g"]["w"]), len(dec["grids"]["g"]["w"])
    assert nd < nf and dec["grids"]["g"]["w"][-1] == full["grids"]["g"]["w"][-1]
    assert dec["grids"]["g"]["w"][:400] == full["grids"]["g"]["w"][:400]
    fw = {round(x, 12): i for i, x in enumerate(full["grids"]["g"]["w"])}
    for i, x in enumerate(dec["grids"]["g"]["w"]):
        j = fw[round(x, 12)]
        assert dec["templates"][0]["f"]["g"][i] == pytest.approx(full["templates"][0]["f"]["g"][j], rel=1e-12)
        assert dec["grids"]["g"]["k31"][i] == pytest.approx(full["grids"]["g"]["k31"][j], rel=1e-12)
    # a grid already coarser than min_dlog keeps every sample
    same = T.extend_templates(src, 26.0, min_dlog=1e-9)
    assert len(same["grids"]["g"]["w"]) == nf


def test_extended_high_records_under_soft_limit(tmp_path):
    from portalkit.physics import templates as T
    if not T.DEFAULT_SOURCE.is_file():
        pytest.skip("template JSON not available")
    res = T.write(SimpleNamespace(source=None, extend_to_um=26.0), tmp_path)
    idx = res.doc
    assert schema.validate_templates(idx) == []
    sizes = {g: [] for g in idx["grids"]}
    for t in idx["templates"]:
        for g, f in t["files"].items():
            sizes[g].append((tmp_path / "templates" / f).stat().st_size)
    assert max(sizes["high"]) <= T.TEMPLATE_FILE_SOFT_MAX
    src = json.loads(T.DEFAULT_SOURCE.read_text())
    ext_medium = T.extend_templates(src, 26.0)["grids"]["medium"]
    assert idx["grids"]["medium"]["n"] == len(ext_medium["w"])        # medium not decimated
    gd = json.loads((tmp_path / "templates" / idx["grids"]["high"]["file"]).read_text())
    assert gd["values"][-1] >= 26.0


# ---------------------------------------------------------------------------
# 7. sedcurve channels
# ---------------------------------------------------------------------------

def test_sedcurve_any_channels():
    from portalkit.bundle.records import decode_pks1, encode_pks1
    w = np.linspace(0.75, 5.0, 50)
    spec = {"wave": w, "channels": {"SPHEREx": w ** -1.0, "IRS": w ** -0.5},
            "units": {"SPHEREx": "Jy", "IRS": "Jy"}}
    raw = encode_pks1("s1", spec, kind="sedcurve")
    rec = decode_pks1(raw)
    assert set(rec["channels"]) == {"SPHEREx", "IRS"}
    assert np.allclose(rec["channels"]["IRS"], w ** -0.5, rtol=1e-5)
    with pytest.raises(ValueError):
        encode_pks1("s1", spec)                               # pks1 still needs 'flux'
    with pytest.raises(ValueError):
        encode_pks1("s1", {"wave": w, "channels": {}}, kind="sedcurve")


def test_sedcurve_record_set_written(tmp_path):
    from portalkit.bundle import records as R
    from portalkit.spec import Spectrum
    w = np.linspace(0.75, 5.0, 50)
    spec = RecordSetSpec(key="sed", kind="sedcurve", catalog="src", ids=["a", "b"],
                         loader=lambda i: Spectrum(wave=w, channels={"SPHEREx": w ** -1.0,
                                                                     "IRS": w ** -0.5},
                                                   units={"SPHEREx": "Jy", "IRS": "Jy"}))
    R.write(spec, tmp_path / "sed")
    idx = json.loads(next((tmp_path / "sed").rglob("index*.json")).read_text())
    assert idx["kind"] == "sedcurve" and set(idx["entries"]) == {"a", "b"}
    assert idx.get("unit_f") == "Jy"


# ---------------------------------------------------------------------------
# 8. all-missing fields
# ---------------------------------------------------------------------------

def test_all_missing_fields_typed_by_role(tmp_path):
    from portalkit.bundle import catalog as C
    rows = [{"id": f"s{i}", "m": None, "f": None, "e": None, "v": None, "t": None,
             "ok": 15.0 + i} for i in range(5)]
    cat = catalog_from_rows(rows, "id", roles={"m": "mag", "f": "flag", "e": "error",
                                               "v": "value", "t": "text", "ok": "mag"})
    t = cat.table
    assert t["m"].dtype.kind == "f" and t["v"].dtype.kind == "f" and t["e"].dtype.kind == "f"
    assert bool(np.all(t["m"].mask)) and bool(np.all(t["v"].mask))
    assert t["f"].dtype.kind == "b" and bool(np.all(t["f"].mask))
    assert t["t"].dtype.kind in "US"
    C.write(cat, tmp_path / "c")
    man = json.loads(next((tmp_path / "c").rglob("manifest*.json")).read_text())
    roles = {c["key"]: c.get("role") for c in man["cols"]}
    assert roles["m"] == "mag" and roles["f"] == "flag"


# ---------------------------------------------------------------------------
# 9. checklist Filename by path suffix
# ---------------------------------------------------------------------------

def _cl(rows):
    from portalkit.cli import CHECKLIST_KEYS
    return ",".join(CHECKLIST_KEYS) + ",ISOKN\n" + "".join(",".join(r) + ",\n" for r in rows)


def _ex(rows):
    from portalkit.cli import CHECKLIST_KEYS
    return ("id," + ",".join(CHECKLIST_KEYS) + ",ISOKN\n"
            + "".join(f"{i}," + ",".join(r) + "\n" for i, r in enumerate(rows)))


def test_checklist_diff_matches_path_suffix():
    from portalkit.cli import checklist_diff
    k = ["T", "1234", "O", "s1", "1", "1", "1"]
    cl = _cl([k + ["/orange/x/mastDownload/JWST/jwA/jwA_s1_x1d.fits"],
              k + ["/orange/x/mastDownload/JWST/jwB/jwA_s1_x1d.fits"],
              k[:3] + ["s2"] + k[4:] + ["jwC_x1d.fits"]])
    ex = _ex([k + ["mastDownload/JWST/jwA/jwA_s1_x1d.fits", "y"],     # parent dir + basename
              k[:3] + ["s2"] + k[4:] + ["jwX/jwC_x1d.fits", "n"],    # checklist has a basename only
              k + ["jwZ/jwA_s1_x1d.fits", "w"]])                     # other parent: no match
    diff, notes = checklist_diff(ex, cl)
    plus = [ln for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")]
    assert plus == ["+T,1234,O,s1,1,1,1,/orange/x/mastDownload/JWST/jwA/jwA_s1_x1d.fits,y",
                    "+T,1234,O,s2,1,1,1,jwC_x1d.fits,n"]
    assert notes[-1].startswith("2 checklist row(s) change")
    assert sum("no checklist row matches" in n for n in notes) == 1
    # a bare basename in the export matches every checklist path with that basename
    diff, notes = checklist_diff(_ex([k + ["jwA_s1_x1d.fits", "e"]]), cl)
    assert notes[-1].startswith("2 checklist row(s) change")


# ---------------------------------------------------------------------------
# 10. WPE4 leftovers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("deny, present, warn", [
    (False, False, False), (False, True, True), (True, False, True)])
def test_manifest_denied_5xx_with_htaccess(monkeypatch, deny, present, warn):
    vmod = sys.modules["portalkit.deploy.verify"]
    monkeypatch.setattr(vmod, "_fetch", lambda url, **kw: (500, {}, None, None, None))
    denied, st, w = vmod.check_manifest_denied(PUB + "a/", ".portalkit-deploy.json",
                                               visibility="public", deny_expected=deny,
                                               htaccess_present=present)
    assert denied is None and st == 500
    assert (w is not None) is warn
    if warn:
        assert "AllowOverride" in w


def test_non_utf8_owned_htaccess(tmp_path):
    from portalkit.deploy import htaccess as H
    p = tmp_path / ".htaccess"
    p.write_bytes(H.MARKER.encode() + b"\n# caf\xe9 \xff\n" + H.DENY_BODY.encode())
    assert H.htaccess_owned(p)
    assert H.htaccess_state(tmp_path) == "deny"
    assert H.has_deny_rule(tmp_path)
    assert H.write_deny_htaccess(tmp_path, dry_run=True) == p
    assert H.write_deny_htaccess(tmp_path) == p
    assert p.read_bytes() == H.DENY_HTACCESS_TEXT.encode()
    assert H.write_deny_htaccess(tmp_path) is None
    # a non-UTF-8 marker line is foreign
    q = tmp_path / "f"
    q.mkdir()
    (q / ".htaccess").write_bytes(b"# \xff portalkit\n")
    assert not H.htaccess_owned(q / ".htaccess")
    assert H.htaccess_state(q) == "foreign"


# ---------------------------------------------------------------------------
# review follow-ups: Python legacy mirror (speed, spherex-colours, c suffix), dim_alias merge
# order, served id_column
# ---------------------------------------------------------------------------

_SPX_ALIAS = {"c_F162M_F210M": "f162m-f210m", "m_F162M": "f162m", "m_F210M": "f210m",
              "c_F150W_F200W": "f150w-f200w"}
_SPX_PAGE = {"page": {"legacy": {"dim_alias": _SPX_ALIAS}},
             "layout": {"row": [
                 {"pane": "scatter", "id": "ccd",
                  "options": {"overlays": ["yoo_a", "yoo_b", "g23_vector"], "color": "s"}},
                 {"pane": "scatter", "id": "cmd", "options": {}}]}}


def test_hashmap_alias_reaches_colour_suffix():
    from portalkit.legacy import hashmap as hm
    loc = {"hash": "#x=c_F162M_F210M&y=m_F162M&c=c_F162M_F210M~magma~0~2", "search": "",
           "page": {"page": _SPX_PAGE["page"], "layout": None},
           "dims": ["f162m", "f162m-f210m"], "pane": "mani", "ds": "src"}
    h = hm.map_legacy(["kv-view"], loc)["hash"]
    assert h["c"] == "f162m-f210m~magma~0~2"
    assert hm.decode_frame(h["mani.frame"]) == [0, 1, 1, 0]
    # the browser's loc key dimAlias wins over the page document
    h2 = hm.map_legacy(["kv-view"], {**loc, "dimAlias": {"c_F162M_F210M": "q"}})["hash"]
    assert h2["c"] == "q~magma~0~2"
    assert hm.dim_alias({"dimAlias": {"a": "b"}, "dim_alias": {"a": "c"}}) == {"a": "b"}


def test_hashmap_speed():
    from portalkit.legacy import hashmap as hm
    assert "speed" in hm.CANONICAL_KEYS and hm.is_canonical_hash("speed=2&id=x")
    assert hm.map_legacy(["wavelength"], {"hash": "", "search": "?speed=3"}) == \
        {"hash": {"speed": "3"}, "consumedSearch": ["speed"]}
    assert hm.map_legacy(["wavelength"], {"hash": "#ra=10&dec=20&speed=2"})["hash"]["speed"] == "2"
    assert hm.map_legacy(["wavelength"], {"hash": "#id=S1", "search": "?speed=0.25"})["hash"] == \
        {"speed": "0.25"}
    r = hm.map_legacy(["kv-view", "bare-name"], {"hash": "#name=foo", "search": "?speed=3",
                                                 "page": {"page": {}, "layout": None}})
    assert r == {"hash": {"id": "foo", "speed": "3"}, "consumedSearch": ["speed"]}
    assert hm.map_legacy(["kv-view", "bare-name"], {"hash": "#foo", "search": "?speed=0.5"})["hash"] \
        == {"id": "foo", "speed": "0.5"}
    assert hm.map_legacy(["kv-view"], {"hash": "#id=S1", "search": "?speed=-1"}) == \
        {"hash": {}, "consumedSearch": ["speed"]}
    assert hm.GRAMMARS["kv-view"]({"hash": "", "search": "?speed=2"}) == {"speed": "2"}


def test_hashmap_spherex_colours():
    from portalkit.legacy import hashmap as hm
    assert "spherex-colours" in hm.GRAMMAR_NAMES
    assert set(hm.GRAMMAR_NAMES) <= set(schema.GRAMMARS)
    loc = {"hash": "", "page": _SPX_PAGE,
           "search": "?ax=F162M&ay=F210M&bx=F150W&by=F200W&c=s&band=F162M&cmd=1"
                     "&zoom=0,1,-0.5,2&yoo=1&hide=young|evolved"}
    r = hm.map_legacy(["spherex-colours"], loc)
    h = r["hash"]
    assert h["ccd.x"] == "f162m-f210m" and h["ccd.y"] == "f150w-f200w"
    assert h["cmd.y"] == "f162m" and h["cmd.x"] == "f162m-f210m"
    assert h["view"] == "cmd:half" and h["c"] == "s"
    assert h["ccd.xr"] == "0,1" and h["ccd.yr"] == "-0.5,2"
    assert h["ccd.ovl"] == "yoo_a,yoo_b,g23_vector"
    assert hm.decode_json(h["f"]) == {"cats": {"s": ["young", "evolved"]}}
    assert sorted(r["consumedSearch"]) == ["ax", "ay", "band", "bx", "by", "c", "cmd", "hide",
                                           "yoo", "zoom"]
    assert hm.is_canonical_hash("&".join(f"{k}={v}" for k, v in h.items()))
    r2 = hm.map_legacy(["spherex-colours"], {"hash": "", "page": _SPX_PAGE,
                                             "search": "?ax=F162M&ay=F210M"})
    assert r2["hash"]["ccd.ovl"] == "g23_vector"
    r3 = hm.GRAMMARS["spherex-colours"]({"hash": "", "page": _SPX_PAGE,
                                         "search": "?ax=F444W&ay=F480M&yoo=b"})
    assert r3["ccd.x"] == "f444w-f480m" and r3["ccd.ovl"] == "yoo_b,g23_vector"
    assert hm.GRAMMARS["spherex-colours"]({"hash": "", "search": "?foo=1"}) is None
    # one scatter pane: the CMD keys are consumed and dropped
    one = {"page": _SPX_PAGE["page"], "layout": {"pane": "scatter", "id": "cmd", "options": {}}}
    r4 = hm.map_legacy(["spherex-colours"], {"hash": "", "page": one,
                                             "search": "?ax=F162M&ay=F210M&band=F162M&cmd=1"})
    assert r4["hash"] == {"cmd.x": "f162m-f210m"}
    assert sorted(r4["consumedSearch"]) == ["ax", "ay", "band", "cmd"]


def _merge_portal(site_alias):
    pc = SimpleNamespace(legacy=["old.html"], grammar=["kv-view"], preset=None,
                         presets=lambda: {})
    raw = {"page": [{"id": "map", "dim_alias": site_alias}]}
    return SimpleNamespace(page=lambda pid: pc if pid == "map" else None, raw=raw)


def test_dim_alias_site_entry_wins():
    page = _explorer({"m_A": "spec_dim", "m_B": "spec_b"})
    out = buildmod._merge_legacy(page, _merge_portal({"m_A": "site_dim", "m_C": "site_c"}))
    assert out.legacy.dim_alias == {"m_A": "site_dim", "m_B": "spec_b", "m_C": "site_c"}
    bare = PageSpec(id="map", archetype="explorer", title="Map", datasets=["src"])
    assert buildmod._merge_legacy(bare, _merge_portal({"m_A": "x"})).legacy.dim_alias == {"m_A": "x"}


def test_foreign_id_column_must_be_served():
    cat = _cat()
    cat.columns = [c for c in cat.columns if c.key != "alias"]
    assert "alias" in cat.table.colnames
    with pytest.raises(ValueError, match="served column"):
        buildmod._PortalBuild._check_id_column("spec", _foreign(id_column="alias"), {"src": cat})
    buildmod._PortalBuild._check_id_column("spec", _foreign(id_column="name"), {"src": cat})
    buildmod._PortalBuild._check_id_column("spec", _foreign(id_column="f182m"), {"src": cat})
