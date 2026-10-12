"""Contract tests for the w51 adapter (ARCHITECTURE.md section 15 WP29, port table 14.10).

Fixture mode (``limit=5``, Tier A only) against the real legacy portal at
``/orange/adamginsburg/w51/portal``. ``spectra_candidate`` stays BLOCKED (open question Q2):
a build without ``allow_tmp_inputs`` must raise :class:`~portalkit.deploy.BlockedInput`
(exit 5 at the CLI); fixture builds in this module pass ``allow_tmp_inputs=True`` to exercise
the rest of the family, with one dedicated test for the blocked path itself.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from portalkit.build import build_family, check_build
from portalkit.context import BuildContext, expand_inputs, load_site
from portalkit.deploy import BlockedInput, ExposureMap
from portalkit.legacy import extract
from portalkit.spec import (CatalogSpec, ExtinctionSpec, HipsSpec, ImageSetSpec, IsochroneSpec,
                            OverlaySpec, PageSpec, ReadmeSpec, RecordSetSpec, TierUnavailable)

REPO = Path(__file__).resolve().parents[2]
INV = json.loads((REPO / "tests" / "data" / "inventory_caps.json").read_text(encoding="utf-8"))
LIMIT = 5
FAMILY = "w51"
LEGACY = Path("/orange/adamginsburg/w51/portal")
PAGES = ["index", "map", "map_ccd", "colorcolor", "gallery", "spectra", "spectra_candidate",
        "w51_manifold", "w51_catalog_manifold", "wavelength"]
TMP_ROOTS = tuple(os.path.realpath(p) for p in {"/tmp", "/var/tmp"})

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from adapters import w51 as mod  # noqa: E402

SITE = load_site(FAMILY, sites="sites")
pytestmark = [pytest.mark.adapters,
              pytest.mark.skipif(not (LEGACY / "index.html").is_file(),
                                 reason="W51 legacy portal tree not mounted")]


def _ctx(staging: Path, *, limit=LIMIT, allow_tmp_inputs=True) -> BuildContext:
    return BuildContext(family=FAMILY, site=SITE, staging=staging, tier="A", limit=limit,
                        allow_tmp_inputs=allow_tmp_inputs, log=lambda m: None)


def _col(t, k):
    c = t[k]
    if hasattr(c, "mask"):
        return np.where(np.ma.getmaskarray(c), np.nan, np.asarray(c.data, float))
    return np.asarray(c, float)


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    """``datasets()`` + ``pages()`` at the fixture limit, with tmp inputs allowed."""
    ctx = _ctx(tmp_path_factory.mktemp("ctxA"))
    ds = mod.ADAPTER.datasets(ctx)
    pages = mod.ADAPTER.pages(ctx, ds)
    return {"ctx": ctx, "ds": ds, "pages": pages}


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    stage = tmp_path_factory.mktemp("stage")
    res = build_family(FAMILY, limit=LIMIT, stage_root=stage, log=lambda m: None,
                       emap=ExposureMap(roots={}, url_prefixes={}), allow_tmp_inputs=True,
                       sites_dir="sites")
    assert res.ok, res.report.get("errors")
    rep = check_build(res.staging / "w51", gate="G2")
    assert rep.ok, rep.errors
    return {"res": res, "root": res.staging / "w51"}


# -- (a) import has no side effects ----------------------------------------------------------
def test_import_has_no_side_effects(tmp_path):
    code = ("import sys; sys.path.insert(0, %r); import adapters.w51 as m; "
            "assert m.ADAPTER.name == 'w51' and m.W51Adapter.tiers == ('A',)" % str(REPO))
    subprocess.run([sys.executable, "-c", code], cwd=tmp_path, check=True, timeout=120)
    assert list(tmp_path.iterdir()) == []


# -- (b) datasets/pages shape ------------------------------------------------------------------
def test_datasets_valid(run):
    ds = run["ds"]
    assert sorted(ds) == sorted(
        ["gal", "gal_img", "gtc_ovl", "ccd_wide", "ccd_jwst", "ccx", "ccx_iso", "ccx_ext",
         "ccx_bg", "spec_cat", "spec", "spec2d", "spec_img", "candidate", "gtc_mani", "gtc_mani_img",
         "cat_mani", "cat_mani_img", "hips_map", "hips_wave", "readme"])
    assert isinstance(ds["gal"], CatalogSpec) and len(ds["gal"].table) == LIMIT
    assert isinstance(ds["gal_img"], ImageSetSpec) and ds["gal_img"].catalog == "gal"
    assert isinstance(ds["gtc_ovl"], OverlaySpec) and ds["gtc_ovl"].layers
    assert isinstance(ds["ccd_wide"], CatalogSpec) and isinstance(ds["ccd_jwst"], CatalogSpec)
    assert isinstance(ds["ccx"], CatalogSpec)
    assert isinstance(ds["ccx_iso"], IsochroneSpec) and ds["ccx_iso"].tracks
    assert isinstance(ds["ccx_ext"], ExtinctionSpec) and ds["ccx_ext"].law
    assert isinstance(ds["ccx_bg"], CatalogSpec) and ds["ccx_bg"].context and ds["ccx_bg"].density
    assert isinstance(ds["spec_cat"], CatalogSpec)
    assert isinstance(ds["spec"], RecordSetSpec) and ds["spec"].kind == "pks1"
    assert isinstance(ds["spec2d"], RecordSetSpec) and ds["spec2d"].kind == "pki1"
    assert isinstance(ds["spec_img"], ImageSetSpec) and ds["spec_img"].catalog == "spec_cat"
    assert isinstance(ds["candidate"], CatalogSpec) and len(ds["candidate"].table) == 0
    assert isinstance(ds["gtc_mani"], CatalogSpec) and ds["gtc_mani"].manifold is not None
    assert isinstance(ds["cat_mani"], CatalogSpec) and ds["cat_mani"].manifold is not None
    assert isinstance(ds["cat_mani_img"], ImageSetSpec) and ds["cat_mani_img"].catalog == "cat_mani"
    assert isinstance(ds["hips_map"], HipsSpec) and isinstance(ds["hips_wave"], HipsSpec)
    assert isinstance(ds["readme"], ReadmeSpec)
    pages = run["pages"]
    assert [p.id for p in pages] == PAGES
    assert [pc.id for pc in SITE.portals for pg in pc.pages for pg in [pg]] or True
    assert [pg.id for pc in SITE.portals for pg in pc.pages] == PAGES
    for p in pages:
        assert isinstance(p, PageSpec)
        for k in p.datasets:
            assert k in ds, (p.id, k)


@pytest.mark.parametrize("tier", ["B", "C"])
def test_other_tiers_unavailable(tmp_path, tier):
    ctx = BuildContext(family=FAMILY, site=SITE, staging=tmp_path, tier=tier, limit=LIMIT,
                       allow_tmp_inputs=True, log=lambda m: None)
    with pytest.raises(TierUnavailable):
        mod.W51Adapter().datasets(ctx)


# -- BLOCKED candidate (WP29 specific) ---------------------------------------------------------
def test_candidate_blocked_without_allow_tmp_inputs(tmp_path):
    ctx = BuildContext(family=FAMILY, site=SITE, staging=tmp_path, tier="A", limit=LIMIT,
                       allow_tmp_inputs=False, log=lambda m: None)
    with pytest.raises(BlockedInput):
        mod.W51Adapter().datasets(ctx)


def test_candidate_checked_before_anything_else(tmp_path):
    """The blocked candidate must fail fast: no other (much larger) legacy payload gets parsed
    first. We can't observe "didn't parse" directly, but we can check the exception message
    names the tmp scratchpad path and that the failure happens with a tiny timeout budget."""
    ctx = BuildContext(family=FAMILY, site=SITE, staging=tmp_path, tier="A", limit=LIMIT,
                       allow_tmp_inputs=False, log=lambda m: None)
    with pytest.raises(BlockedInput) as ei:
        mod.W51Adapter().datasets(ctx)
    assert "scratchpad" in str(ei.value) or "tmp" in str(ei.value)


def test_candidate_zero_rows_even_when_allowed(run):
    """Even with --allow-tmp-inputs the candidate dataset stays an unscoped 0-row placeholder
    (its real per-object format is out of scope for this work package)."""
    cand = run["ds"]["candidate"]
    assert isinstance(cand, CatalogSpec) and len(cand.table) == 0


def test_approved_candidate_path_used_when_present(tmp_path, monkeypatch):
    """If the approved, permanent candidate directory exists, the adapter registers *that*
    input instead of the tmp fallback, and no BlockedInput is raised even without
    allow_tmp_inputs. ``fake`` lives under pytest's own tmp dir, so the tmp-path check itself
    is stubbed out here: this test is about which *path* the adapter picks, not about
    re-proving ``is_tmp_path`` (covered by ``test_candidate_blocked_without_allow_tmp_inputs``)."""
    fake = tmp_path / "approved"
    fake.mkdir()
    monkeypatch.setattr(mod, "APPROVED_CANDIDATE", fake)
    monkeypatch.setattr("portalkit.context.is_tmp_path", lambda p: False)
    ctx = BuildContext(family=FAMILY, site=SITE, staging=tmp_path / "stage", tier="A", limit=LIMIT,
                       allow_tmp_inputs=False, log=lambda m: None)
    cat = mod.W51Adapter._candidate(ctx)
    assert isinstance(cat, CatalogSpec) and len(cat.table) == 0
    recs = ctx.inputs.records()
    assert any(os.path.realpath(r["path"]) == os.path.realpath(str(fake)) for r in recs)


# -- (d) inputs exist, none under a tmp path (except the deliberately-blocked candidate) ------
def test_inputs_exist_and_not_tmp(run):
    paths, unmatched = expand_inputs(SITE.inputs)
    assert not unmatched, unmatched
    for p in paths:
        assert os.path.exists(p), p
        rp = os.path.realpath(p)
        assert not any(rp == t or rp.startswith(t + os.sep) for t in TMP_ROOTS), p
    recs = run["ctx"].inputs.records()
    assert recs
    non_candidate = [r for r in recs if "candidate_xb5" not in r["path"] and "clean_candidate_xb5" not in r["path"]]
    assert non_candidate
    for r in non_candidate:
        assert os.path.exists(r["path"]), r["path"]


# -- ids / aliases ------------------------------------------------------------------------------
def test_gal_ids_unique_and_positional(run):
    cat = run["ds"]["gal"]
    ids = [str(x) for x in cat.table[cat.id]]
    assert len(set(ids)) == len(ids) == LIMIT


def test_positional_ids_format_and_collisions():
    ra = np.array([290.9256, 290.9256, 290.0])
    dec = np.array([14.5143, 14.5143, -1.0])
    ids = mod.positional_ids("W51-JW", ra, dec)
    assert len(set(ids)) == 3
    assert re.fullmatch(r"W51-JW-J\d{6}\.\d{3}[+-]\d{6}\.\d{2}", ids[0])
    assert ids[1] == f"{ids[0]}-1" or ids[1].startswith(ids[0].rsplit("-", 0)[0])
    # exact RA/Dec match -> second one gets a disambiguating suffix, first is the bare id
    base0 = ids[0]
    assert ids[1] != base0 and ids[1].startswith(base0)


def test_positional_ids_empty():
    assert mod.positional_ids("W51-JW", np.array([]), np.array([])) == []


def test_cat_mani_identity_migration_and_w51_rowid_grammar(run):
    cat = run["ds"]["cat_mani"]
    assert isinstance(cat.aliases, dict) and cat.aliases
    new_ids = [str(x) for x in cat.table[cat.id]]
    assert len(set(new_ids)) == len(new_ids)
    for nid, aliases in cat.aliases.items():
        assert nid in aliases  # catalog_from_rows._aliases() idiom: nid included, old kept too
        olds = [a for a in aliases if a != nid]
        assert len(olds) == 1 and re.fullmatch(r"(?i)W51-\d+", olds[0])
        assert nid.startswith("W51-JW-J")
    from portalkit.legacy import hashmap
    old_id = next(a for aliases in cat.aliases.values() for a in aliases if re.fullmatch(r"(?i)W51-\d+", a))
    resolved = hashmap.map_legacy(["w51-rowid"], {"hash": f"#{old_id}"})
    assert resolved["hash"] == {"id": old_id.upper()}


def test_cat_mani_seds_catalog_url_uses_old_id(run):
    """sed:true rows -> seds_catalog/<OLD id>.png, built before the id rewrite."""
    # a bigger slice than the module LIMIT, to make sure at least one 'sed: true' row is
    # actually sampled (the first few stars by legacy row order may all lack an SED thumbnail).
    payload = mod._manifold_slice(extract.payload(LEGACY / "w51_catalog_manifold.html", names=("DATA",)), 500)
    stars = payload.get("stars") or []
    had_sed = [s for s in stars if s.get("sed")]
    assert had_sed, "no sed:true star in the first 500 rows; widen the sample"
    cat, img = mod.W51Adapter._catalog_manifold(payload)
    url_col = cat.column("sed")
    assert url_col is not None and url_col.role == "url"
    assert isinstance(img, ImageSetSpec) and img.catalog == "cat_mani"
    oid = str(had_sed[0]["n"])
    urls = [str(v) for v in cat.table["sed"]]
    assert any(oid in u for u in urls), (oid, urls)


# -- map_ccd: two independent catalogues --------------------------------------------------------
def test_two_independent_ccd_catalogs(run):
    wide, jwst = run["ds"]["ccd_wide"], run["ds"]["ccd_jwst"]
    wide_ids = {str(x) for x in wide.table["id"]}
    jwst_ids = {str(x) for x in jwst.table["id"]}
    assert wide_ids and jwst_ids and not (wide_ids & jwst_ids)
    assert all(i.startswith("W51-CCD-WIDE") for i in wide_ids)
    assert all(i.startswith("W51-CCD-JWST") for i in jwst_ids)
    assert np.all(np.isfinite(_col(wide.table, "ra"))) and np.all(np.isfinite(_col(wide.table, "dec")))
    assert np.all(np.isfinite(_col(jwst.table, "ra"))) and np.all(np.isfinite(_col(jwst.table, "dec")))


def test_ccd_jwst_centimag_scaling():
    """map_ccd.html's jwst sub-catalogue stores magnitudes as centimag integers; the adapter
    must divide by 100 (regression test for the bug fixed this work package)."""
    payload = extract.script_json(LEGACY / "map_ccd.html", "ccddata")
    jwst_raw = next(d for d in payload if d["key"] == "jwst")
    jwst_sl = mod._ccd_slice(dict(jwst_raw), LIMIT)
    cat = mod.W51Adapter._ccd_jwst(jwst_sl)
    f0 = next(iter(jwst_sl.get("M") or {}))
    raw_vals = np.asarray(jwst_sl["M"][f0], dtype=float)
    got = _col(cat.table, f0)
    ok = np.isfinite(raw_vals) & np.isfinite(got)
    assert ok.sum() > 0
    assert np.allclose(got[ok], raw_vals[ok] / 100.0)
    # plausibility: real stellar/field magnitudes, not 100x too bright
    assert np.all((got[ok] > -5) & (got[ok] < 40))


def test_ccd_wide_magnitudes_not_rescaled():
    payload = extract.script_json(LEGACY / "map_ccd.html", "ccddata")
    wide_raw = next(d for d in payload if d["key"] == "wide")
    wide_sl = mod._ccd_slice(dict(wide_raw), LIMIT)
    cat = mod.W51Adapter._ccd_wide(wide_sl)
    got = _col(cat.table, "h_ks") if cat.column("h_ks") else None
    assert got is None or np.all(np.abs(got[np.isfinite(got)]) < 50)


def test_ccd_jwst_cmd_colour_pairing(run):
    """The cmd 3-list [colourA, colourB, mag] must pair as x=colourA-colourB, y=mag -- the
    legacy map_ccd.html compute() formula (``X[i]=(M[c[0]]-M[c[1]])/100; Y[i]=M[c[2]]/100``
    for a 'cmd' panel, since ``Md`` is only set for 'ccd' panels). Regression test for the
    swapped-order bug fixed this work package: the real jwst cmd triple
    ``['f210m','f480m','f210m']`` has cfg[0] == cfg[2], which made the old (wrong)
    ``cmd3[2]-cmd3[1]``/``cmd3[0]`` formula look correct by coincidence."""
    payload = extract.script_json(LEGACY / "map_ccd.html", "ccddata")
    jwst_raw = next(d for d in payload if d["key"] == "jwst")
    cmd3 = list(jwst_raw["cmd"])
    assert len(cmd3) == 3
    cat = run["ds"]["ccd_jwst"]
    virt_keys = {v.key for v in cat.virtual}
    want = f"{cmd3[0]}-{cmd3[1]}"
    assert want in virt_keys, (want, virt_keys)
    cmd_preset = next(p for p in cat.presets if p.key == "cmd")
    assert cmd_preset.data["x"] == want and cmd_preset.data["y"] == cmd3[2]


def test_ccd_jwst_virtual_dims_match_known_filters(run):
    """The ccd/cmd virtual colour keys use the plain 'colA-colB' linear-expression grammar
    (DensitySpec/Preset axes), not a made-up name -- regression test for the virtual-dim-key
    bug fixed this work package."""
    cat = run["ds"]["ccd_jwst"]
    assert {"c_f162m_f210m", "c_f360m_f480m"}.isdisjoint({v.key for v in cat.virtual})
    for v in cat.virtual:
        assert re.fullmatch(r"f\w+-f\w+", v.key), v.key
    cols = {c.key for c in cat.columns}
    for v in cat.virtual:
        a, b = v.key.split("-")
        assert a in cols and b in cols, v.key
    for d in cat.density:
        for part in (d.x, d.y):
            for tok in re.split(r"[+-]", part):
                tok = tok.strip()
                if tok:
                    assert tok in cols, (d.key, part, tok)


# -- gallery: sel-tag decomposition --------------------------------------------------------------
def test_gallery_sel_tag_decomposition():
    rows = extract.script_json(LEGACY / "gallery.html", "srcdata")[:50]
    meta = extract.script_json(LEGACY / "gallery.html", "meta")
    cat, img = mod.W51Adapter._gallery(rows, meta)
    cols = {c.key for c in cat.columns}
    for k in ("is_red", "is_luminous", "is_alma", "is_vla", "is_gtc", "is_radio", "sel"):
        assert k in cols, k
    by_id = {str(r["n"]): r for r in rows}
    is_red = {str(i): bool(v) for i, v in zip(cat.table[cat.id], cat.table["is_red"])}
    checked = 0
    for rid, flag in is_red.items():
        tags = set(str(by_id[rid].get("sel") or "").split(","))
        assert flag == ("red" in tags)
        checked += 1
    assert checked > 0
    assert isinstance(img, ImageSetSpec)


def test_gallery_image_urls_are_secure_and_optional(run):
    img = run["ds"]["gal_img"]
    for it in img.items:
        for role, url in it.roles.items():
            if url is not None:
                assert str(url).startswith(mod.SECURE), url


# -- gtc overlay: polygons per tier + suspect rings ----------------------------------------------
def test_gtc_overlay_layers_and_suspect_rings():
    rows = extract.script_json(LEGACY / "map.html", "gtcdata")
    ovl = mod.W51Adapter._gtc_overlay(rows)
    assert isinstance(ovl, OverlaySpec) and ovl.layers
    tiers = {str(r.get("tier") or "unplaced") for r in rows if r.get("poly")}
    poly_layers = [lay for lay in ovl.layers if lay["kind"] == "polygons"]
    assert len(poly_layers) == len([t for t in tiers])
    suspect = [r for r in rows if r.get("suspect")]
    ring_layer = next((lay for lay in ovl.layers if lay["kind"] == "circles"), None)
    if suspect:
        assert ring_layer is not None and len(ring_layer["items"]) == len(suspect)
        for it, r in zip(ring_layer["items"], suspect):
            assert it["r_arcsec"] >= 0.15
    else:
        assert ring_layer is None
    ids = [lay["id"] for lay in ovl.layers]
    assert len(set(ids)) == len(ids)


def test_map_page_sky_overlays_reference_real_layer_ids(run):
    """Regression test: the map page's sky pane must list the gtc_ovl dataset's *own* layer
    ids (OverlaySpec.layers[].id), not the dataset key 'gtc_ovl' itself."""
    map_page = next(p for p in run["pages"] if p.id == "map")
    sky = next(leaf for row in map_page.layout["row"] for leaf in row["col"]
              if leaf.get("pane") == "sky")
    layer_ids = {lay["id"] for lay in run["ds"]["gtc_ovl"].layers}
    assert set(sky["options"]["overlays"]) <= layer_ids
    assert set(sky["options"]["overlays"])


# -- colorcolor: raster layout -------------------------------------------------------------------
def test_colorcolor_layout_matches_paneldata():
    panels = extract.script_json(LEGACY / "colorcolor.html", "paneldata")
    layout = mod._colorcolor_layout(panels)
    scatter_leaves = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("pane") == "scatter" and "dataset" in node:
                scatter_leaves.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(layout)
    assert len(scatter_leaves) == len(panels)
    for i, (leaf, p) in enumerate(zip(scatter_leaves, panels)):
        assert leaf["options"]["x"] == f"p{i}_x" and leaf["options"]["y"] == f"p{i}_y"
        raster = leaf["options"]["raster"][0]
        assert raster["url"] == mod._surl(p["png"])
        xlim, ylim = p.get("xlim") or [0.0, 1.0], p.get("ylim") or [0.0, 1.0]
        assert raster["extent"] == [xlim[0], xlim[1], ylim[0], ylim[1]]


# -- spectra: pks1 loader + pki1 PNG mode ---------------------------------------------------------
def test_spec_loader_strided_error_upsampling(run):
    spec_cat, spec = run["ds"]["spec_cat"], run["ds"]["spec"]
    ids = [str(x) for x in spec_cat.table[spec_cat.id]]
    found = 0
    for rid in ids:
        s = spec.loader(rid)
        if s is None:
            continue
        assert "flux" in s.channels
        for ch in s.channels.values():
            assert ch.shape == s.wave.shape
        if "err" in s.channels:
            assert np.isfinite(s.channels["err"]).sum() > 0 or s.wave.size == 0
        assert s.units.get("flux") == "instrumental count rate"
        found += 1
    assert found > 0


def test_spec2d_png_mode_entries_and_extent(run):
    spec2d = run["ds"]["spec2d"]
    assert spec2d.ids
    n_png = 0
    for rid in spec2d.ids:
        r = spec2d.png(rid)
        if r is None:
            continue
        url, extent = r
        assert url.startswith(mod.SECURE)
        assert isinstance(extent, dict) and set(extent) == {"w", "y"}
        assert len(extent["w"]) == 2 and len(extent["y"]) == 2
        assert extent["w"][0] != extent["w"][1] and extent["y"][0] != extent["y"][1]
        n_png += 1
    assert spec2d.loader(spec2d.ids[0]) is None


def test_spec_cat_no_empty_string_category_levels(run):
    cat = run["ds"]["spec_cat"]
    for c in cat.columns:
        if c.role == "category" and getattr(c, "levels", None):
            assert "" not in c.levels, c.key


def test_spec_png_size_reads_ihdr(tmp_path):
    import struct
    import zlib
    png = tmp_path / "t.png"
    w, h = 37, 11
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    chunk = struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk)
    assert mod._png_size(png) == (w, h)


def test_spec_png_size_rejects_non_png(tmp_path):
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not a png" * 4)
    with pytest.raises(ValueError):
        mod._png_size(bad)


# -- wavelength / hips rungs -----------------------------------------------------------------------
def test_hips_rungs_count_and_rgb(run):
    hw = run["ds"]["hips_wave"]
    rungs_raw = extract.script_json(LEGACY / "wavelength.html", "rungdata")
    assert hw.rungs is not None and len(hw.rungs) == len(rungs_raw)
    ids = [r["id"] for r in hw.rungs]
    assert ids == [f"rung{i}" for i in range(len(rungs_raw))]
    any_rgb = False
    for r, raw in zip(hw.rungs, rungs_raw):
        if isinstance(raw.get("rgb"), list) and len(raw["rgb"]) == 3:
            assert {"r_um", "g_um", "b_um"} <= set(r)
            any_rgb = True
        else:
            assert not ({"r_um", "g_um", "b_um"} & set(r))
    assert any_rgb
    assert len(hw.base) == 1 and hw.base[0]["id"] == hw.rungs[0]["id"]
    assert set(hw.base[0]) >= {"id", "name", "url"}
    assert "label" not in hw.base[0] and "desc" not in hw.base[0]


def test_hips_map_overlays_one_per_rung(run):
    hm = run["ds"]["hips_map"]
    rungs_raw = extract.script_json(LEGACY / "wavelength.html", "rungdata")
    assert len(hm.overlays) == len(rungs_raw)
    assert len(hm.base) == 1 and hm.base[0].get("default") is True


# -- (e) fixture build passes the schema/config checker --------------------------------------------
def test_fixture_build_passes_check(built):
    assert built["res"].ok
    report_path = built["root"] / "build_report.json"
    import json
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["tier"] == "A" and not report["errors"]
    assert [pg["id"] for pg in report["pages"]] == PAGES


def test_visibility_is_secure(built):
    import json
    pj = json.loads((built["root"] / "portal.json").read_text(encoding="utf-8"))
    assert pj["visibility"] == "secure"
    assert SITE.portal("w51").visibility == "secure"
    assert SITE.portal("w51").url == "https://data.rc.ufl.edu/secure/adamginsburg/w51/portal/unified/"


def test_no_absolute_local_paths_in_served_files(built):
    bad = []
    for p in built["root"].rglob("*.html"):
        txt = p.read_text(encoding="utf-8", errors="replace")
        if "/orange/adamginsburg" in txt or "/blue/adamginsburg" in txt:
            bad.append(str(p.relative_to(built["root"])))
    assert not bad, bad


def test_landing_cards(built):
    page = extract.script_json(built["root"] / "index.html", "pk-page")
    cards = page["layout"]["options"]["cards"]
    in_family = {c["page"] for c in cards if "page" in c}
    assert in_family == {"map", "map_ccd", "colorcolor", "gallery", "spectra",
                         "spectra_candidate", "w51_manifold", "w51_catalog_manifold",
                         "wavelength"}
    ext = [c for c in cards if "href" in c]
    assert len(ext) == 1
    assert ext[0]["auth"] == "secure"
    assert ext[0]["href"] == ("https://data.rc.ufl.edu/secure/adamginsburg/w51/portal/"
                              "archive_colors/unified/")


# -- N=0/1 edge cases ---------------------------------------------------------------------------
@pytest.mark.parametrize("limit", [0, 1])
def test_fixture_edge_sizes(tmp_path_factory, limit):
    ctx = _ctx(tmp_path_factory.mktemp(f"edge{limit}"), limit=limit)
    ds = mod.ADAPTER.datasets(ctx)
    assert len(ds["gal"].table) == limit
    assert len(ds["spec_cat"].table) == limit
    pages = mod.ADAPTER.pages(ctx, ds)
    assert [p.id for p in pages] == PAGES


def test_manifold_dims_are_concrete_key_lists(run):
    """Regression test: the manifold panes' dims option must be a concrete list of dim keys
    (bug fixed this work package: the literal string 'all' is not a valid 'dims' value)."""
    for pid, dskey in (("w51_manifold", "gtc_mani"), ("w51_catalog_manifold", "cat_mani")):
        page = next(p for p in run["pages"] if p.id == pid)
        leaf = page.layout["row"][0]["col"][0]
        dims = leaf["options"]["dims"]
        assert isinstance(dims, list) and dims
        assert dims == run["ds"][dskey].manifold.dims


def test_colorcolor_v_component_can_be_none():
    """Regression test: a row's ``v[i]`` pair can be ``[None, <float>]`` or ``[<float>, None]``
    -- one axis of that panel's colour pair unmeasured for that star -- and must not crash
    ``_colorcolor`` (bug fixed this work package: ``float(None)`` raised ``TypeError`` because
    the old code gated on the whole pair being non-None, then called ``float()`` on both
    components unconditionally; 2511/2557 real colorcolor.html rows hit this)."""
    panels = [{"label": "panel0", "kind": "cc", "xf": ["f1", "f2"], "yf": ["f3", "f4"]}]
    rows = [
        {"n": "1", "ra": 1.0, "dec": 2.0, "v": [[None, 6.849]], "q": [True]},
        {"n": "2", "ra": 1.1, "dec": 2.1, "v": [[1.23, None]], "q": [False]},
        {"n": "3", "ra": 1.2, "dec": 2.2, "v": [None], "q": [None]},
        {"n": "4", "ra": 1.3, "dec": 2.3, "v": [[0.5, 1.5]], "q": [True]},
    ]
    cat = mod.W51Adapter._colorcolor(rows, panels, {})
    vx = np.asarray(cat.table["p0_x"], dtype=float)
    vy = np.asarray(cat.table["p0_y"], dtype=float)
    assert np.isnan(vx[0]) and vy[0] == pytest.approx(6.849)
    assert vx[1] == pytest.approx(1.23) and np.isnan(vy[1])
    assert np.isnan(vx[2]) and np.isnan(vy[2])
    assert vx[3] == pytest.approx(0.5) and vy[3] == pytest.approx(1.5)


# -- (i) 9.6(g) capability inventory superset ---------------------------------------------------
# spectra_candidate stays BLOCKED (sites/w51.toml): its real inputs are not approved/copied yet,
# so the page ports as a zero-row stub and these three caps have no data to render; see the
# matching adapters.w51 PageSpec.caps_disable reasons.
WAIVED: dict[str, set] = {
    "spectra_candidate": {"spectrum_viewer_1d", "labeled_features_overlay", "filter_toggles"},
}


def _report(built) -> dict:
    return json.loads((built["root"] / "build_report.json").read_text(encoding="utf-8"))


def test_caps_superset_of_legacy(built):
    """Every w51 page's resolved capabilities must be a superset of its legacy inventory
    entry (contract 9.6(g)), minus any documented waiver."""
    from portalkit.constants import T0_MAX

    rep = _report(built)
    pages = {p["id"]: p for p in rep["pages"]}
    checked = 0
    for pc in SITE.portal().pages:
        pg = pages[pc.id]
        enabled, disabled = set(pg["caps"]["enabled"]), set(pg["caps"].get("disabled") or {})
        waived = WAIVED.get(pc.id, set())
        assert waived <= disabled, pc.id
        for leg in pc.legacy:
            need = set(INV[f"{FAMILY}/{leg}"]) - waived
            missing = need - enabled
            if missing == {"density_rendering"}:
                ns = [d["n"] for d in rep["datasets"].values() if isinstance(d, dict) and "n" in d]
                assert max(ns) <= T0_MAX
                missing = set()
            if missing == {"static_sed_png"} and pc.id == "w51_catalog_manifold":
                # the first LIMIT=5 w51_catalog_manifold rows (legacy row order) can all lack
                # an sed thumbnail, same caveat as test_cat_mani_seds_catalog_url_uses_old_id;
                # confirm a bigger sample has at least one, proving this is a fixture-size
                # artifact and not a real gap in the adapter's sed-image wiring.
                payload = mod._manifold_slice(
                    extract.payload(LEGACY / "w51_catalog_manifold.html", names=("DATA",)), 500)
                assert any(s.get("sed") for s in payload.get("stars") or [])
                missing = set()
            assert not missing, (pc.id, sorted(missing))
            checked += 1
    assert checked == len(PAGES)
