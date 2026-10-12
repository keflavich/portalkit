"""Contract tests for the nirspec6927_narrative adapter (WP31; ARCHITECTURE.md sections 14.1,
14.7, wave 6).

Tier A only, and the pure HTML->markdown conversion/geometry helpers are exercised directly
(no legacy mount needed) before the dataset-level tests, which skip when the legacy tree is
not mounted.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FAMILY = "nirspec6927_narrative"
TMP_ROOTS = tuple(os.path.realpath(p) for p in {tempfile.gettempdir(), "/tmp", "/var/tmp"})

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from adapters import nirspec6927_narrative as mod  # noqa: E402

from portalkit.build import build_family, check_build  # noqa: E402
from portalkit.config import narrative_document  # noqa: E402
from portalkit.context import BuildContext, expand_inputs, load_site  # noqa: E402
from portalkit.deploy import ExposureMap  # noqa: E402
from portalkit.schema import validate_narrative, validate_overlays  # noqa: E402
from portalkit.spec import (CatalogSpec, ImageSetSpec, NarrativeSpec, OverlaySpec, PageSpec,  # noqa: E402
                            ReadmeSpec, TierUnavailable)

SITE = load_site(FAMILY)
pytestmark = pytest.mark.adapters
_MOUNTED = (mod.LEGACY_DIR / "nirspec_6927_sources.json").is_file()
skip_unmounted = pytest.mark.skipif(not _MOUNTED, reason="legacy nirspec_6927 tree not mounted")


def _ctx(tier: str, staging: Path, limit=None) -> BuildContext:
    return BuildContext(family=FAMILY, site=SITE, staging=staging, tier=tier, limit=limit,
                        log=lambda m: None)


# -- pure helpers: no legacy mount required --------------------------------------------------

def test_import_has_no_side_effects(tmp_path):
    code = ("import sys; sys.path.insert(0, %r); import adapters.nirspec6927_narrative as m; "
            "assert m.ADAPTER.name == 'nirspec6927_narrative'" % str(REPO))
    subprocess.run([sys.executable, "-c", code], cwd=tmp_path, check=True, timeout=60)
    assert list(tmp_path.iterdir()) == []


def test_beat_markdown_strips_tags_and_keeps_links():
    html = '<p>Hello <span class="hl" data-hl="x">world</span>, see <a href="y.html">here</a>.</p>'
    md = mod.beat_markdown(html)
    assert md == "Hello world, see [here](y.html)."


def test_beat_markdown_table_and_figure():
    html = ('<table class="mini"><tr><th>a</th><th>b|c</th></tr><tr><td>1</td><td>2</td></tr></table>'
            '<figure><img src="x.png"><figcaption>caption text</figcaption></figure>')
    md = mod.beat_markdown(html)
    assert "| a | b/c |" in md
    assert "|---|---|" in md
    assert "| 1 | 2 |" in md
    assert "not reproduced" in md and "caption text" in md
    assert "<img" not in md and "<table" not in md


def test_beat_markdown_empty_and_unicode():
    assert mod.beat_markdown("") == ""
    assert mod.beat_markdown("<p></p>") == ""
    md = mod.beat_markdown("<p>μµ — Rüßell &amp; Sön</p>")
    assert "Rüßell & Sön" in md and "μµ" in md


def test_parse_beats_order_and_fields():
    html = ('<section class="beat" data-scene="a"><h2>H2a</h2><h3>H3a</h3><p>one</p></section>'
            '<section class="beat" data-scene="b"><h2>H2b</h2><p>two</p></section>')
    beats = mod.parse_beats(html)
    assert [b["scene"] for b in beats] == ["a", "b"]
    assert beats[0]["h2"] == "H2a" and beats[0]["h3"] == "H3a"
    assert beats[1]["h3"] == ""
    assert "<h2>" not in beats[0]["body"] and "one" in beats[0]["body"]


def test_parse_beats_no_sections_gives_empty_list():
    assert mod.parse_beats("<p>no beats here</p>") == []


def test_beat_markdown_leaves_local_paths_for_pathlabeler_to_scrub():
    """``beat_markdown`` itself does no path redaction; ``_narrative`` labels the finished
    markdown with the shared ``portalkit.build.PathLabeler`` (ARCHITECTURE.md section 9.3
    "Served paths") instead of a bespoke regex, so the raw path is still present here and
    only disappears once run through that labeler."""
    from portalkit.build import PathLabeler
    html = ('<table class="mini"><tr><td>deep catalogue</td>'
            '<td>/orange/adamginsburg/jwst/brick/catalogs/brick_crossband_m7_merge_20260714.fits</td></tr></table>')
    md = mod.beat_markdown(html)
    assert "/orange/" in md
    scrubbed = PathLabeler(()).scrub_text(md)
    assert "/orange/" not in scrubbed
    assert "brick_crossband_m7_merge_20260714.fits" in scrubbed


def test_box_poly_is_closed_and_scales_with_declination():
    p_eq = mod._box_poly(10.0, 0.0, half_arcsec=2.25)
    assert len(p_eq) == 5 and p_eq[0] == p_eq[-1]
    ra_half_eq = (p_eq[1][0] - p_eq[0][0]) / 2
    p_hi = mod._box_poly(10.0, 80.0, half_arcsec=2.25)
    ra_half_hi = (p_hi[1][0] - p_hi[0][0]) / 2
    # RA half-width widens by 1/cos(dec) at high declination (a fixed sky box gets wider in RA)
    assert ra_half_hi > ra_half_eq > 0
    assert math.isclose(ra_half_hi / ra_half_eq, 1 / math.cos(math.radians(80.0)), rel_tol=1e-6)


def test_box_poly_near_pole_does_not_raise_or_divide_by_zero():
    p = mod._box_poly(0.0, 89.9999, half_arcsec=2.25)
    assert len(p) == 5
    assert all(math.isfinite(v) for pt in p for v in pt)


def test_num_handles_nan_inf_and_none():
    assert mod._num(None) is None
    assert mod._num(float("nan")) is None
    assert mod._num(float("inf")) is None
    assert mod._num("3.5") == 3.5
    assert mod._num("not a number") is None


def test_surl_rejects_paths_outside_jwst_root():
    with pytest.raises(ValueError):
        mod._surl("/etc/passwd")


def test_lim_truncates_and_handles_n0_n1():
    class Ctx:
        limit = None
    assert mod._lim([1, 2, 3], Ctx()) == [1, 2, 3]
    Ctx.limit = 0
    assert mod._lim([1, 2, 3], Ctx()) == []
    Ctx.limit = 1
    assert mod._lim([1, 2, 3], Ctx()) == [1]
    Ctx.limit = 1
    assert mod._lim([], Ctx()) == []


# -- dataset-level: needs the legacy mount ---------------------------------------------------

@pytest.fixture(scope="module")
def run():
    with tempfile.TemporaryDirectory() as td:
        ctx = _ctx("A", Path(td))
        adapter = mod.ADAPTER
        ds = adapter.datasets(ctx)
        pages = adapter.pages(ctx, ds)
        yield {"ctx": ctx, "ds": ds, "pages": pages}


@skip_unmounted
def test_datasets_valid(run):
    ds = run["ds"]
    for k in ("m_cand", "m_ta", "m_v18", "rm_v18", "rm_v20", "rm_sel", "h2", "h2lines"):
        assert isinstance(ds[k], CatalogSpec), k
    for k in ("m_ovl", "rm_ovl"):
        assert isinstance(ds[k], OverlaySpec), k
    for k in ("h2img", "refcheck"):
        assert isinstance(ds[k], ImageSetSpec), k
    assert isinstance(ds["narrative"], NarrativeSpec)
    assert isinstance(ds["readme"], ReadmeSpec)
    assert len(ds["narrative"].beats) == len(mod.parse_beats(
        (mod.LEGACY_DIR / "nirspec_6927_msata_finding.html").read_text(encoding="utf-8")))
    for p in run["pages"]:
        assert isinstance(p, PageSpec)
        for k in p.datasets:
            assert k in ds, (p.id, k)
    assert {p.id for p in run["pages"]} == {"n_index", "n_msata", "n_h2", "n_refstar_map", "n_refstar_check"}


@skip_unmounted
def test_tier_b_unavailable(tmp_path):
    with pytest.raises(TierUnavailable):
        mod.NIRSpec6927NarrativeAdapter().datasets(_ctx("B", tmp_path))


@skip_unmounted
def test_msata_catalog_ids_unique_and_prefixed(run):
    ds = run["ds"]
    cand_ids = [str(x) for x in ds["m_cand"].table[ds["m_cand"].id]]
    assert len(set(cand_ids)) == len(cand_ids) and cand_ids
    assert all(re.fullmatch(r"p\d+_\d+", i) for i in cand_ids)
    ta_ids = [str(x) for x in ds["m_ta"].table[ds["m_ta"].id]]
    assert len(set(ta_ids)) == len(ta_ids)
    assert all(re.fullmatch(r"ta\d+_\d+", i) for i in ta_ids)


@skip_unmounted
def test_overlay_layers_reference_sky_boxes_with_at_least_4_vertices(run):
    ovl = run["ds"]["m_ovl"]
    assert isinstance(ovl, OverlaySpec) and ovl.layers
    for lay in ovl.layers:
        assert lay["kind"] == "boxes" and lay["space"] == "sky"
        for it in lay["items"]:
            assert len(it["poly"]) >= 4
    doc = {"layers": ovl.layers}
    assert validate_overlays(doc) == []


@skip_unmounted
def test_narrative_document_round_trips_through_validator(run):
    doc = narrative_document(run["ds"]["narrative"])
    errs = validate_narrative(doc)
    assert errs == [], errs
    assert len(doc["beats"]) == 15
    ids = [b["id"] for b in doc["beats"]]
    assert len(set(ids)) == len(ids)
    # exactly one beat (the provenance close) suppresses the sky/scatter view (section 5a design)
    assert sum(1 for b in doc["beats"] if b["view"].get("suppress_view")) >= 1
    # highlight groups, when present, name a dataset this adapter actually built
    for b in doc["beats"]:
        hl = b["view"].get("highlight")
        if hl:
            assert hl["ds"] in run["ds"]


@skip_unmounted
def test_beats_4_and_5_reference_real_acquisition_field_layers(run):
    """Beats 4/5 ("the fields o022 actually acquired on" / "o019, with less room to spare")
    must highlight real ``m_ta`` rows and real ``m_ovl`` layers for those visits -- not ids
    that exist nowhere because ``ta_by_visit`` (which, unlike ``ta_fields`` alone, carries
    o022/o019's per-field TA rows too) was never consulted."""
    ds = run["ds"]
    layer_ids = {lay["id"] for lay in ds["m_ovl"].layers}
    ta_ids = {str(x) for x in ds["m_ta"].table[ds["m_ta"].id]}
    beats = ds["narrative"].beats
    for i in (4, 5):
        view = beats[i - 1]["view"]
        on = view.get("layers_on") or []
        assert on and set(on) <= layer_ids, (i, on, layer_ids)
        hl = view.get("highlight")
        assert hl and hl["ids"], (i, hl)
        assert set(hl["ids"]) <= ta_ids, (i, hl["ids"])
        # the layers these beats turn on must not also be hidden by the same view
        assert not (set(on) & set(view.get("layers_off") or [])), i


@skip_unmounted
def test_narrative_beats_have_no_local_paths(run):
    from portalkit.build import LOCAL_PATH_RE
    for b in run["ds"]["narrative"].beats:
        assert not LOCAL_PATH_RE.search(b["title"]), b["id"]
        assert not LOCAL_PATH_RE.search(b["markdown"]), b["id"]


@skip_unmounted
def test_h2_lines_dataset_built_when_line_table_nonempty(run):
    ds = run["ds"]
    if ds["h2lines"].n:
        assert "Eu" in [c.key for c in ds["h2lines"].columns]
        assert "f_si" in [c.key for c in ds["h2lines"].columns]


@skip_unmounted
def test_h2_scatter_pane_is_not_log_y_over_a_signed_flux(run):
    """``f_si`` is legitimately negative for non-detections/upper limits; the scatter pane's
    log transform drops any non-positive value, which would silently hide every such row."""
    ds, pages = run["ds"], run["pages"]
    page = next(p for p in pages if p.id == "n_h2")
    scatter = next(r for r in page.layout["row"] if r.get("pane") == "scatter")
    assert scatter["options"].get("y") == "f_si"
    assert not scatter["options"].get("ylog")
    f_si = ds["h2lines"].table["f_si"]
    assert (f_si <= 0).any()  # the condition the ylog pitfall depends on actually holds here


@skip_unmounted
def test_inputs_exist_and_not_tmp(run):
    paths, unmatched = expand_inputs(SITE.inputs)
    assert not unmatched, unmatched
    for p in paths:
        assert os.path.exists(p), p
        rp = os.path.realpath(p)
        assert not any(rp == t or rp.startswith(t + os.sep) for t in TMP_ROOTS), p


@skip_unmounted
def test_fixture_build_passes_check():
    with tempfile.TemporaryDirectory() as td:
        stage = Path(td)
        mp = pytest.MonkeyPatch()
        mp.setenv("PK_STAGE_ROOT", str(stage))
        mp.setenv("PK_CACHE_DIR", str(stage / "cache"))
        try:
            res = build_family(FAMILY, limit=20, stage_root=stage, log=lambda m: None,
                               emap=ExposureMap(roots={}, url_prefixes={}))
        finally:
            mp.undo()
        assert res.ok, res.report.get("errors")
        rep = check_build(res.staging, gate="G2")
        assert rep.ok, rep.errors
