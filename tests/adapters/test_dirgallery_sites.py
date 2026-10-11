"""Contract tests for the dirgallery_sites adapter (ARCHITECTURE.md section 9.6 a-g; WP23).

Fixture mode (``limit=50``) for the contract tests; the preset-exactness test scans every
directory in full (image headers are not read there). Run with
``python -m pytest tests/adapters/test_dirgallery_sites.py -m adapters -q``. Builds go to pytest
temp dirs (or ``$PK_STAGE_ROOT``); the adapter cache and the exposure scan cache are
``$PK_CACHE_DIR`` when set.

The registry reads the legacy jQuery galleries and their directories (read-only). Checks that
depend on a legacy page's text (its regex, its wrong header) skip a page that ``--replace`` has
already turned into a stub.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from portalkit.context import BuildContext, load_site  # noqa: E402

pytestmark = pytest.mark.adapters

FAMILY = "dirgallery_sites"
LIMIT = 50


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    old = {k: os.environ.get(k) for k in ("PK_CACHE_DIR", "PK_STAGE_ROOT")}
    if not old["PK_CACHE_DIR"]:
        os.environ["PK_CACHE_DIR"] = str(tmp_path_factory.mktemp("pkcache"))
    stage = Path(old["PK_STAGE_ROOT"] or tmp_path_factory.mktemp("stage"))
    yield stage
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture(scope="module")
def mod():
    import adapters.dirgallery_sites as m
    return m


@pytest.fixture(scope="module")
def site():
    return load_site(FAMILY)


def _ctx(tier, stage, limit=LIMIT):
    return BuildContext(family=FAMILY, site=load_site(FAMILY), staging=stage / "ctx", tier=tier, limit=limit,
                        log=lambda m: None)


@pytest.fixture(scope="module")
def runs(env, mod):
    out = {}
    for tier in ("A", "B"):
        ctx = _ctx(tier, env)
        t0 = time.perf_counter()
        ds = mod.ADAPTER.datasets(ctx)
        out[tier] = (ctx, ds, time.perf_counter() - t0)
    return out


@pytest.fixture(scope="module")
def full(env, mod):
    """Tier B over every file (no limit); image headers are skipped to keep this lean."""
    ad = mod.DirGallerySitesAdapter()
    ad._sizes = lambda ctx, g, rows: {}
    ctx = _ctx("B", env, limit=None)
    return ad.datasets(ctx)


@pytest.fixture(scope="module")
def build(env):
    from portalkit.build import build_family
    return build_family(FAMILY, tier="B", limit=LIMIT, stage_root=env, log=lambda m: None)


def _legacy_text(path: Path) -> str | None:
    """The legacy page's text, or None when it is missing or already a portalkit stub."""
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    if "<title>Moved</title>" in text and "location.replace(" in text:      # deploy --replace stub
        return None
    return text


def _decode(preset: str) -> dict:
    assert preset.startswith("fac=")
    raw = preset[4:]
    return json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8"))


# -- (a) import has no side effects -----------------------------------------------------------
def test_a_import_creates_no_files():
    with tempfile.TemporaryDirectory() as d:
        code = (f"import sys; sys.path.insert(0, {str(REPO)!r}); import adapters.dirgallery_sites as m; "
                "assert m.ADAPTER and m.GALLERIES")
        subprocess.run([sys.executable, "-c", code], cwd=d, check=True, timeout=120,
                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        assert os.listdir(d) == []


# -- (b) datasets() returns valid specs in time -----------------------------------------------
@pytest.mark.parametrize("tier", ["A", "B"])
def test_b_datasets_valid_and_fast(runs, mod, site, tier):
    from portalkit.spec import CatalogSpec, ImageSetSpec, ReadmeSpec
    _, ds, dt = runs[tier]
    assert dt < 120, f"datasets() took {dt:.1f} s"
    for g in mod.GALLERIES:
        img = ds[g.page]
        assert isinstance(img, ImageSetSpec), g.page
        assert 0 < len(img.items) <= LIMIT, g.page
        assert img.roles and all(it.roles for it in img.items), g.page
    for p in site.portals:
        assert isinstance(ds[f"readme_{p.id}"], ReadmeSpec)
    assert isinstance(ds["pngb"], CatalogSpec) and 0 < len(ds["pngb"].table) <= LIMIT


def test_c_tier_c_unavailable(env, mod):
    from portalkit.spec import TierUnavailable
    with pytest.raises(TierUnavailable):
        mod.ADAPTER.datasets(_ctx("C", env))


# -- (c) ids ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("tier", ["A", "B"])
def test_c_unique_ids(runs, mod, site, tier):
    ctx, ds, _ = runs[tier]
    for g in mod.GALLERIES:
        ids = [it.id for it in ds[g.page].items]
        assert len(set(ids)) == len(ids), g.page
        assert all(ids), g.page
    pages = mod.ADAPTER.pages(ctx, ds)
    for p in site.portals:
        pids = [pg.id for pg in pages if pg.portal == p.id]
        assert len(set(pids)) == len(pids) and pids[0] == "index", p.id
    assert len({g.page for g in mod.GALLERIES}) == len(mod.GALLERIES)
    cat = ds["pngb"]
    assert len(set(cat.table["id"])) == len(cat.table)
    assert {it.id for it in ds["png_b"].items} == {str(i) for i in cat.table["id"]}


# -- (d) inputs --------------------------------------------------------------------------------------
@pytest.mark.parametrize("tier", ["A", "B"])
def test_d_inputs_exist_not_tmp(runs, tier):
    ctx, _, _ = runs[tier]
    recs = ctx.inputs.records()
    assert recs
    for r in recs:
        p = r["path"]
        assert os.path.exists(p), p
        assert not p.startswith(("/tmp/", "/var/tmp/", tempfile.gettempdir() + "/")), p


# -- (e) fixture build passes check ----------------------------------------------------------------
def test_e_fixture_build_passes_check(build, mod, site):
    from portalkit.build import check_build
    assert build.ok, build.report.get("errors")
    rep = check_build(build.staging)
    assert rep.ok, rep.errors
    want = {f"{p.id}/index.html" for p in site.portals} | {f"{g.portal}/{g.page}.html" for g in mod.GALLERIES}
    got = {f"{Path(p).parent.name}/{Path(p).name}" for p in build.pages}
    assert got == want


# -- (f) Tier A / Tier B parity ---------------------------------------------------------------------
def test_f_tier_parity(runs, mod):
    a, b = runs["A"][1], runs["B"][1]
    for g in mod.GALLERIES:
        ia, ib = a[g.page], b[g.page]
        assert [it.id for it in ia.items] == [it.id for it in ib.items], g.page
        assert [it.roles for it in ia.items] == [it.roles for it in ib.items], g.page
        assert [it.tags for it in ia.items] == [it.tags for it in ib.items], g.page
        assert ia.facets == ib.facets and ia.roles == ib.roles, g.page
        assert [(p.key, p.data) for p in ia.presets] == [(p.key, p.data) for p in ib.presets], g.page
    assert list(a["pngb"].table["id"]) == list(b["pngb"].table["id"])


# -- (g) resolved caps cover the legacy inventory and the port table ---------------------------------
def test_g_caps_cover_inventory(build, runs, mod):
    inv = json.loads((REPO / "tests" / "data" / "inventory_caps.json").read_text())
    table = json.loads((REPO / "tests" / "data" / "port_table.json").read_text())
    galleries = [g.page for g in mod.GALLERIES]
    rows = {
        "nirspec6927/public brick_2221/nirspec_6927/gallery.html": ["nirspec6927_seds"],
        "sickle_brick/brick_2221/nirspec_6927/gallery.html": ["nirspec6927_seds"],
        **{f"sickle_brick/brick_2221/ccds_cmds/{f}.html": ["ccds_cmds"]
           for f in ("gallery", "gallery_blue405410", "gallery_cmd", "gallery_cmd_iso", "gallery_cmd_lowext")},
        "extras/~30 jQuery autoindex galleries in w51/brick/sgrb2/wd2/wd1/cloudc (critic)":
            [p for p in galleries if p not in ("png_b", "rob_ccds")],
        "extras/robitaille imaging_run/png_b and modelgrids/ccds/gallery.html (critic)": ["png_b", "rob_ccds"],
    }
    built = {}
    for pdir in Path(build.staging).iterdir():
        if (pdir / "build_report.json").is_file():
            rep = json.loads((pdir / "build_report.json").read_text())
            built.update({p["id"]: set(p["caps"]["enabled"]) for p in rep["pages"] if p["id"] != "index"})
    port = {f"{r['family']}/{r['legacy_page']}": set(r["capabilities_to_fill"]) for r in table}
    for key, pids in rows.items():
        want = set(inv[key]) | port[key]
        for pid in pids:
            missing = want - built[pid]
            assert not missing, (key, pid, sorted(missing))


def test_g_caps_resolve_from_specs(runs, mod):
    """caps.resolve on the adapter's own specs (without manifests) enables the gallery set."""
    from portalkit.caps import resolve
    ctx, ds, _ = runs["B"]
    pages = {(p.portal, p.id): p for p in mod.ADAPTER.pages(ctx, ds)}
    for g in mod.GALLERIES:
        got = set(resolve(pages[(g.portal, g.page)], ds, {}).enabled)
        assert {"lightbox", "keyboard_nav", "filter_toggles", "source_list_search"} <= got, g.page
        if g.role[0] == "sed":
            assert "static_sed_png" in got


# -- legacy pages: regex parity, exact presets, headers --------------------------------------------
def test_registry_regexes_match_legacy_pages(mod, site):
    checked = 0
    for g in mod.GALLERIES:
        ldir = Path(site.portal(g.portal).legacy_dir)
        for s in g.sources:
            if s.kind == "pngb":
                continue
            for lg in s.legacy:
                text = _legacy_text(ldir / lg.file)
                if text is None:
                    continue
                found = mod.legacy_regexes(ldir / lg.file)
                assert found == [lg.rx, *lg.skipped], (lg.file, found)
                checked += 1
    assert checked >= 40


def test_presets_reproduce_legacy_lists(full, mod, site):
    """Each legacy page's preset selects exactly the items whose files its regex lists (all files)."""
    checked = 0
    for g in mod.GALLERIES:
        vis = site.portal(g.portal).visibility
        img = full[g.page]
        presets = {json.dumps(p.data["facets"], sort_keys=True) for p in img.presets}
        for s in g.sources:
            if s.kind == "pngb":
                continue
            local = Path(mod.WEB[vis]) / s.served
            names = [n for n in os.listdir(local)                 # broken symlinks are skipped
                     if n.lower().endswith(mod.EXTS) and (local / n).is_file()]
            for lg in s.legacy:
                rx = mod.js_regex(lg.rx)
                want = {mod.DirGallerySitesAdapter._split(s, g, n)[0] for n in names if rx.search(n)}
                if len(g.sources) > 1:
                    want = {f"{s.label}/{b}" for b in want}
                sel = mod.preset_facets(g, s, lg)
                got = {it.id for it in img.items
                       if all(it.tags.get(k) in v for k, v in sel.items())}
                assert got == want, (g.page, lg.file, len(got), len(want))
                if sel and want:
                    assert json.dumps(sel, sort_keys=True) in presets, (g.page, lg.file)
                checked += 1
    assert checked >= 45


def test_ccds_cmds_preset_counts(full):
    """The five brick_2221/ccds_cmds pages: the legacy file counts of the WP23 survey."""
    img = full["ccds_cmds"]
    n = {k: sum(1 for it in img.items if it.tags.get(k) == "yes")
         for k in ("g_cmd", "g_cmd_iso", "g_cmd_lowext", "g_blue405410")}
    names = os.listdir("/orange/adamginsburg/web/public/jwst/brick_2221/ccds_cmds")
    for k, rx in (("g_cmd", r"cmd.*\.png$"), ("g_cmd_iso", r"cmd.*iso\.png$"),
                  ("g_cmd_lowext", r"cmd_iso.*lowext\.png$"), ("g_blue405410", r"blue405.*\.png$")):
        assert n[k] == sum(1 for f in names if re.search(rx, f, re.I)) > 0, k
    assert len(img.items) == sum(1 for f in names if re.search(r"\.png$", f, re.I))


def test_mislabeled_headers_fixed(mod, site):
    fixed = 0
    for g in mod.GALLERIES:
        ldir = Path(site.portal(g.portal).legacy_dir)
        for s in g.sources:
            for lg in s.legacy:
                if not lg.header:
                    continue
                assert lg.header.lower() not in g.title.lower(), (g.page, lg.header)
                text = _legacy_text(ldir / lg.file)
                if text is not None:
                    assert lg.header in text, (lg.file, lg.header)
                fixed += 1
    assert fixed >= 15


# -- site registry ------------------------------------------------------------------------------------
def test_site_toml_matches_registry(mod, site):
    """Every legacy file of the registry is mapped by the toml page of its gallery, with the stub
    preset the registry computes. replace is true only for the STUB_PORTALS pages without a
    symlink component (port table: the critic's galleries stay untouched)."""
    reg = {}
    for g in mod.GALLERIES:
        for s in g.sources:
            for lg in s.legacy:
                reg.setdefault((g.portal, g.page), []).append((lg.file, mod.stub_preset(mod.preset_facets(g, s, lg))))
    seen = set()
    for p in site.portals:
        ldir = p.legacy_dir
        for pg in p.pages:
            if pg.id == "index":
                assert not pg.legacy, p.id
                continue
            files = reg[(p.id, pg.id)]
            assert pg.legacy == [f for f, _ in files], (p.id, pg.id)
            assert pg.presets() == {f: pr for f, pr in files if pr}, (p.id, pg.id)
            for lf in pg.files:
                parts, cur, linked = lf.file.split("/"), ldir, False
                for part in parts:
                    cur = os.path.join(cur, part)
                    linked = linked or os.path.islink(cur)
                assert lf.replace is (p.id in mod.STUB_PORTALS and not linked), (p.id, lf.file)
                assert os.path.isfile(os.path.join(ldir, lf.file)), lf.file
            seen.add((p.id, pg.id))
    assert seen == set(reg)
    stubbed = {lf.file for pg in site.portal("brick_2221").pages for lf in pg.files if lf.replace}
    assert stubbed == {"ccds_cmds/gallery.html", "ccds_cmds/gallery_cmd.html", "ccds_cmds/gallery_cmd_iso.html",
                       "ccds_cmds/gallery_cmd_lowext.html", "ccds_cmds/gallery_blue405410.html",
                       "nirspec_6927/gallery.html"}


def test_stub_presets_decode_to_facets(mod):
    for g in mod.GALLERIES:
        for s in g.sources:
            for lg in s.legacy:
                sel = mod.preset_facets(g, s, lg)
                pr = mod.stub_preset(sel)
                assert (pr is None) == (not sel)
                if pr:
                    assert _decode(pr) == sel and "=" not in pr[4:]


def test_other_wps_not_mapped(mod, site):
    """Files owned by WP21/22/24/25/26/28 are not mapped here."""
    owned = re.compile(r"sed_figures_f1500w_all|sed_figures_top100_f1500w|gallery_6927|imaging_run/png/"
                       r"|mastDownload|nirspec_2640|wd2_sedgallery|gallery_2640")
    for p in site.portals:
        for pg in p.pages:
            for f in pg.legacy:
                full = os.path.join(p.legacy_dir, f)
                assert not owned.search(full), full
                assert not owned.search(os.path.realpath(full)), full


# -- served URLs and visibility -------------------------------------------------------------------
@pytest.mark.parametrize("tier", ["B"])
def test_item_urls_served_under_portal_visibility(runs, mod, site, tier):
    from portalkit.build import local_path_for_url
    _, ds, _ = runs[tier]
    for g in mod.GALLERIES:
        vis = site.portal(g.portal).visibility
        for it in ds[g.page].items:
            for url in it.roles.values():
                assert url.startswith(mod.PREFIX[vis]), url
                assert local_path_for_url(url) is not None, url
        if vis == "public":
            assert "/secure/" not in json.dumps([it.roles for it in ds[g.page].items]), g.page


def test_targets_and_urls_match_exposure(site, env):
    from portalkit.deploy import exposure as X
    emap = X.scan()
    for p in site.portals:
        e = X.classify(p.target_dir, emap)
        d = X.check_target(p.visibility, e)
        assert d.ok, (p.id, d.message)
        assert p.url in (e.public_urls if p.visibility == "public" else e.secure_urls), (p.id, p.url)
        assert X.classify_url(p.url, emap) == p.visibility
        if p.visibility == "secure":
            assert e.kind == "secure", p.id


# -- special handling ---------------------------------------------------------------------------------
def test_nirspec6927_seds_public_and_predates(runs):
    _, ds, _ = runs["B"]
    img = ds["nirspec6927_seds"]
    assert img.roles[0][0] == "sed"
    assert all("predates the NIRSpec observations" in it.caption for it in img.items)
    assert all(re.fullmatch(r"\d+", it.tags["pointing"]) for it in img.items if "pointing" in it.tags)
    md = ds["readme_brick_2221"].markdown
    assert "nirspec_6927/unified/" in md and "holds no secure data" in md


def test_variant_roles(runs, mod):
    _, ds, _ = runs["B"]
    img = ds["w51_rgb"]
    keys = [k for k, _ in img.roles]
    assert keys[0] == "main" and {"alma", "log", "log_alma"} <= set(keys)
    for it in img.items:
        for role, url in it.roles.items():
            stem = url.rsplit("/", 1)[1][:-4]
            assert stem.lower().endswith("" if role == "main" else "_" + role), (it.id, role, url)
    # pngs_2100 groups its suffix variants like the other three directories
    d2100 = [it for it in img.items if it.tags.get("dir") == "pngs_2100"]
    assert d2100 and not any(it.id.lower().endswith(("_alma", "_log")) for it in d2100)
    assert any(len(it.roles) > 1 for it in d2100)
    split = mod.DirGallerySitesAdapter._split
    src = next(s for s in mod.GALLERIES[0].sources)
    assert split(src, mod.GALLERIES[0], "x_log_alma.png") == ("x", "log_alma")
    assert split(src, mod.GALLERIES[0], "x_best.png") == ("x", "best")
    assert split(src, mod.GALLERIES[0], "x.png") == ("x", "image")


def test_thumbnail_role_first(runs):
    _, ds, _ = runs["B"]
    img = ds["cloudc_images"]
    assert [k for k, _ in img.roles] == ["thumb", "full"]
    assert all("full" in it.roles for it in img.items)
    assert all(it.roles["thumb"].endswith("_thumbnail.png") for it in img.items if "thumb" in it.roles)
    # thumb and full differ in size; one item-level w/h would be copied onto both roles
    assert all(it.w is None and it.h is None for it in img.items)


def test_names_are_url_quoted(full):
    urls = [u for it in full["brick_paper_co"].items for u in it.roles.values()]
    assert any("%3A" in u for u in urls) and not any(":" in u.split("://", 1)[1] for u in urls)


def test_empty_legacy_preset_has_no_select_all(full):
    """gallery_colorcolorcolor_withcloudc lists no file: no Preset is emitted (the images writer
    would turn an absent value into select-all); its stub hash still selects nothing."""
    img = full["brick_figures"]
    assert not any(it.tags.get("g_colorcolorcolor_withcloudc") == "yes" for it in img.items)
    assert "gallery_colorcolorcolor_withcloudc" not in {p.key for p in img.presets}
    assert "g_colorcolorcolor_withcloudc" not in {k for k, _ in img.facets}
    assert not any("g_colorcolorcolor_withcloudc" in it.tags for it in img.items)


def test_shared_facet_label_names_every_dir(full):
    """brick_paper_co: g_h2ococo2_1011 covers fig9s and figures; its label names both."""
    lab = dict(full["brick_paper_co"].facets)["g_h2ococo2_1011"]
    assert "fig9s, figures" in lab and "(fig9s)" not in lab, lab


def test_png_b_catalog_join(runs):
    import math
    _, ds, _ = runs["B"]
    img, cat = ds["png_b"], ds["pngb"]
    assert img.catalog == "pngb" and img.noun == ("model", "models")
    L = {str(r["id"]): float(r["L"]) for r in cat.table}
    order = [L[it.id] for it in img.items if math.isfinite(L[it.id])]
    assert order == sorted(order, reverse=True)
    assert all(it.caption.startswith(it.id + ": L=") for it in img.items)
    assert all(it.roles["image"].startswith("https://data.rc.ufl.edu/secure/") for it in img.items)


def test_readme_lists_legacy_pages(runs, mod, site):
    _, ds, _ = runs["B"]
    for p in site.portals:
        md = ds[f"readme_{p.id}"].markdown
        for g in mod.GALLERIES:
            if g.portal == p.id:
                assert g.title in md
                for s in g.sources:
                    for lg in s.legacy:
                        assert lg.file in md, (p.id, lg.file)


def test_readme_links_point_at_legacy_files(runs, mod, site):
    """Each README link resolves (through the served roots) to the legacy file it names, e.g.
    wd2 controlfield/gallery.html, which is not in its source's served directory."""
    from portalkit.build import local_path_for_url
    _, ds, _ = runs["B"]
    checked = 0
    for p in site.portals:
        md = ds[f"readme_{p.id}"].markdown
        for g in mod.GALLERIES:
            if g.portal != p.id:
                continue
            for s in g.sources:
                for lg in s.legacy:
                    url = mod.legacy_url(p.visibility, g, lg)
                    assert f"[{lg.file}]({url})" in md, (p.id, lg.file)
                    local = local_path_for_url(url)
                    assert local is not None, url
                    assert os.path.samefile(local, os.path.join(p.legacy_dir, lg.file)), (url, lg.file)
                    checked += 1
    assert checked >= 45
    assert "jwst/wd2/controlfield/gallery.html" in ds["readme_wd2"].markdown


def test_sizes_memo_sees_in_place_rewrite(env, mod, tmp_path):
    """A PNG rewritten in place (new header, same directory stamp) is re-read."""
    import struct

    def png(path, w, h):
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", w, h) + b"\0" * 8)

    d = tmp_path / "imgs"
    d.mkdir()
    f = d / "a.png"
    png(f, 10, 20)
    src = mod.Source("x", ())
    g = mod.Gallery("sizes_probe", "w51", "t", "b", (src,))
    rows = [{"local": str(d), "first": "a.png", "src": src}]
    ctx = BuildContext(family=FAMILY, site=load_site(FAMILY), staging=env / "ctx", tier="B", limit=LIMIT,
                       log=lambda m: None, allow_tmp_inputs=True)
    assert mod.ADAPTER._sizes(ctx, g, rows) == {"x/a.png": [10, 20]}
    st = os.stat(d)
    png(f, 300, 40)
    os.utime(f, ns=(st.st_atime_ns, os.stat(f).st_mtime_ns + 10**9))
    os.utime(d, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert mod.ADAPTER._sizes(ctx, g, rows) == {"x/a.png": [300, 40]}
    hits = ctx.cache.hits
    assert mod.ADAPTER._sizes(ctx, g, rows) == {"x/a.png": [300, 40]}
    assert ctx.cache.enabled and ctx.cache.hits == hits + 1      # the memo is live, not bypassed
