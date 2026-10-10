"""Exposure classifier on a fake tree that mirrors the verified symlinks of section 11.2.

Fake layout (``<tmp>`` stands for the test directory)::

    <tmp>/orange/jwst/{sgrb2,sickle,spectra/mastDownload/JWST/pngs,spectra/nirspec_6927}
    <tmp>/orange/robitaille_models/{portal,modelgrids/imaging_run/png}
    <tmp>/orange/w51/portal
    <tmp>/web/public/jwst/sgrb2   -> <tmp>/orange/jwst/sgrb2
    <tmp>/web/public/jwst/sickle  -> <tmp>/orange/jwst/sickle
    <tmp>/web/public/jwst/spectra -> <tmp>/web/secure/jwst/spectra/mastDownload/JWST/pngs  (public -> secure)
    <tmp>/web/public/jwst/wd2/                                  (real directory)
    <tmp>/web/public/robitaille_models/imaging -> .../modelgrids/imaging_run/png
    <tmp>/web/secure/jwst -> <tmp>/orange/jwst/
    <tmp>/web/secure/robitaille_models -> <tmp>/orange/robitaille_models
    <tmp>/web/secure/w51/ (real, .htaccess) with portal -> <tmp>/orange/w51/portal
    <tmp>/web/secure/wd2mirror -> <tmp>/web/public/jwst/wd2      (secure -> public)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from portalkit.deploy import exposure as ex
from portalkit.deploy.exposure import (Exposure, Reference, check_references, check_target,
                                       classify, classify_url, scan)

PUB = ex.URL_PREFIXES["public"]
SEC = ex.URL_PREFIXES["secure"]


def make_fake_tree(base: Path) -> dict[str, Path]:
    o = base / "orange"
    for d in ("jwst/sgrb2", "jwst/sickle", "jwst/spectra/mastDownload/JWST/pngs",
              "jwst/spectra/nirspec_6927", "robitaille_models/portal",
              "robitaille_models/modelgrids/imaging_run/png", "w51/portal",
              "portalkit_stage", "jwst/releases/site/sgrb2_manifold"):
        (o / d).mkdir(parents=True, exist_ok=True)
    pub = base / "web" / "public"
    sec = base / "web" / "secure"
    (pub / "jwst" / "wd2" / "nirspec_2640").mkdir(parents=True)
    (pub / "robitaille_models").mkdir(parents=True)
    (sec / "w51").mkdir(parents=True)
    (sec / "w51" / ".htaccess").write_text("AuthType Basic\n")
    (sec / "w51" / ".htpasswd").write_text("u:x\n")
    os.symlink(o / "jwst" / "sgrb2", pub / "jwst" / "sgrb2")
    os.symlink(o / "jwst" / "sickle", pub / "jwst" / "sickle")
    os.symlink(sec / "jwst" / "spectra" / "mastDownload" / "JWST" / "pngs",
               pub / "jwst" / "spectra")
    os.symlink(o / "robitaille_models" / "modelgrids" / "imaging_run" / "png",
               pub / "robitaille_models" / "imaging")
    os.symlink(str(o / "jwst") + "/", sec / "jwst")
    os.symlink(o / "robitaille_models", sec / "robitaille_models")
    os.symlink(o / "w51" / "portal", sec / "w51" / "portal")
    os.symlink(pub / "jwst" / "wd2", sec / "wd2mirror")
    return {"base": base, "orange": o, "public": pub, "secure": sec}


@pytest.fixture()
def tree(tmp_path: Path) -> dict[str, Path]:
    return make_fake_tree(tmp_path)


@pytest.fixture()
def emap(tree):
    return scan({"public": tree["public"], "secure": tree["secure"]}, cache_s=0)


def test_verified_links_classification(tree, emap):
    o = tree["orange"]
    cases = {
        o / "jwst" / "sgrb2": "both",
        o / "jwst" / "sickle": "both",
        o / "jwst" / "spectra" / "nirspec_6927": "secure",
        o / "jwst" / "spectra" / "nirspec_6927" / "unified": "secure",   # not yet created
        o / "jwst" / "spectra" / "mastDownload" / "JWST" / "pngs": "both",
        o / "w51" / "portal" / "unified": "secure",
        o / "robitaille_models" / "portal": "secure",
        o / "robitaille_models" / "modelgrids" / "imaging_run" / "png": "both",
        tree["public"] / "jwst" / "wd2" / "nirspec_2640" / "unified": "both",  # via wd2mirror
        tree["public"] / "robitaille_models": "public",
        tree["secure"] / "w51": "secure",
        o / "portalkit_stage": "unserved",
        o / "jwst" / "releases" / "site" / "sgrb2_manifold": "secure",
        tree["base"] / "elsewhere": "unserved",
    }
    for path, kind in cases.items():
        assert classify(path, emap).kind == kind, path


def test_urls_and_via(tree, emap):
    o = tree["orange"]
    e = classify(o / "jwst" / "sgrb2" / "catalog.fits", emap)
    assert e.public_urls == [PUB + "jwst/sgrb2/catalog.fits/"] or \
        e.public_urls == [PUB + "jwst/sgrb2/catalog.fits"]
    assert SEC + "jwst/sgrb2/catalog.fits" in [u.rstrip("/") for u in e.secure_urls]
    assert any("sgrb2 ->" in v for v in e.public_via)

    e = classify(o / "jwst" / "spectra" / "mastDownload" / "JWST" / "pngs", emap)
    assert e.kind == "both"
    assert e.public_urls[0] == PUB + "jwst/spectra/"
    assert e.secure_urls[0] == SEC + "jwst/spectra/mastDownload/JWST/pngs/"
    # the public chain names the public->secure link
    assert any(str(tree["public"] / "jwst" / "spectra") in v for v in e.public_via)

    e = classify(o / "w51" / "portal" / "unified", emap)
    assert e.secure_urls == [SEC + "w51/portal/unified/"]
    assert e.public_urls == []
    assert e.served


def test_unicode_and_spaces_are_quoted(tree, emap):
    d = tree["orange"] / "jwst" / "sgrb2" / "Ünïcode dir"
    d.mkdir()
    e = classify(d, emap)
    assert e.public_urls[0] == PUB + "jwst/sgrb2/%C3%9Cn%C3%AFcode%20dir/"


def test_existing_file_has_no_trailing_slash(tree, emap):
    f = tree["orange"] / "w51" / "portal" / "index.html"
    f.write_text("x")
    e = classify(f, emap)
    assert e.secure_urls == [SEC + "w51/portal/index.html"]


def test_depth_limit_and_prune(tree):
    o = tree["orange"]
    deep = tree["public"] / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    os.symlink(o / "w51", deep / "w51link")            # depth 5: beyond the default 4
    os.symlink(o / "robitaille_models", tree["public"] / "a" / "rlink")   # depth 2
    hips = tree["public"] / "big_hips"
    hips.mkdir()
    os.symlink(o / "jwst" / "spectra", hips / "hidden")
    m = scan({"public": tree["public"], "secure": tree["secure"]}, cache_s=0)
    assert classify(o / "w51" / "portal", m).kind == "secure"
    assert classify(o / "robitaille_models" / "portal", m).kind == "both"
    assert classify(o / "jwst" / "spectra" / "nirspec_6927", m).kind == "secure"
    m5 = scan({"public": tree["public"], "secure": tree["secure"]}, depth=5, cache_s=0)
    assert classify(o / "w51" / "portal", m5).kind == "both"
    m_noprune = scan({"public": tree["public"], "secure": tree["secure"]}, prune=(), cache_s=0)
    assert classify(o / "jwst" / "spectra" / "nirspec_6927", m_noprune).kind == "both"


def test_shallow_route_reexpands_target(tmp_path):
    """A directory reached first by a deep route and later by a shallow one is expanded
    again at the shallow depth, so links only the shallow route reaches are recorded."""
    pub, sec = tmp_path / "public", tmp_path / "secure"
    x, y = tmp_path / "X", tmp_path / "Y"
    (pub / "z" / "b").mkdir(parents=True)
    (pub / "a").mkdir()
    (x / "p").mkdir(parents=True)
    (y / "unified").mkdir(parents=True)
    sec.mkdir()
    os.symlink(x, pub / "z" / "b" / "deep")    # link at depth 3 (visited first: z > a)
    os.symlink(x, pub / "a" / "c")             # link at depth 2
    os.symlink(y, x / "p" / "link2")           # depth 4 via a/c, depth 5 via z/b/deep
    os.symlink(y, sec / "y")
    m = scan({"public": pub, "secure": sec}, cache_s=0)
    e = classify(y / "unified", m)
    assert e.kind == "both"
    assert e.public_urls == [PUB + "a/c/p/link2/unified/"]
    assert check_target("secure", e).level == "refuse"


def test_scan_result_independent_of_thread_count(tree, monkeypatch):
    """The walk lists each level's directories concurrently; the entry list (order included)
    and the warnings must not depend on the number of worker threads."""
    o = tree["orange"]
    deep = tree["public"] / "a" / "b"
    deep.mkdir(parents=True)
    os.symlink(o / "w51", deep / "w51link")
    os.symlink(o / "robitaille_models", tree["public"] / "a" / "rlink")
    os.symlink(tree["base"] / "nowhere", tree["public"] / "dangling")
    roots = {"public": tree["public"], "secure": tree["secure"]}
    out = []
    for n in ("1", "8"):
        monkeypatch.setenv("PK_SCAN_THREADS", n)
        m = scan(roots, cache_s=0)
        out.append(([(e.real, e.kind, e.url, e.via) for e in m.entries], m.warnings))
    assert out[0] == out[1]
    assert len(out[0][0]) > 8


def test_cycles_and_dangling_links(tree):
    pub = tree["public"]
    os.symlink(pub, pub / "loop")                      # points at its own root
    os.symlink(tree["base"] / "nowhere", pub / "dangling")
    os.symlink(tree["orange"] / "jwst" / "sgrb2", tree["orange"] / "jwst" / "sgrb2" / "self")
    m = scan({"public": pub, "secure": tree["secure"]}, cache_s=0)
    assert any("dangling" in w for w in m.warnings)
    assert classify(tree["orange"] / "jwst" / "sgrb2", m).kind == "both"


def test_file_symlinks_are_not_mapped(tree):
    f = tree["orange"] / "w51" / "notes.txt"
    f.write_text("x")
    os.symlink(f, tree["public"] / "notes.txt")
    m = scan({"public": tree["public"], "secure": tree["secure"]}, cache_s=0)
    # only directory symlinks are followed (section 11.2)
    assert classify(f, m).kind == "unserved"


def test_cache_roundtrip(tree, tmp_path, monkeypatch):
    monkeypatch.setenv("PK_CACHE_DIR", str(tmp_path / "cache"))
    ex.clear_cache()
    roots = {"public": tree["public"], "secure": tree["secure"]}
    m1 = scan(roots, cache_s=3600)
    assert list((tmp_path / "cache").glob("exposure-*.json"))
    ex.clear_cache()
    m2 = scan(roots, cache_s=3600)                     # from the disk cache
    assert [e.real for e in m2.entries] == [e.real for e in m1.entries]
    assert m2.scanned == pytest.approx(m1.scanned)
    os.symlink(tree["orange"] / "w51" / "portal", tree["public"] / "w51portal")
    assert classify(tree["orange"] / "w51" / "portal", scan(roots, cache_s=3600)).kind == "secure"
    assert classify(tree["orange"] / "w51" / "portal", scan(roots, cache_s=0)).kind == "both"
    ex.clear_cache()


def test_missing_root_warns(tmp_path):
    m = scan({"public": tmp_path / "nope", "secure": tmp_path / "nope2"}, cache_s=0)
    assert m.entries == []
    assert len(m.warnings) == 2
    assert classify(tmp_path, m).kind == "unserved"


def test_bad_root_kind(tmp_path):
    with pytest.raises(ValueError):
        scan({"private": tmp_path}, cache_s=0)


def test_classify_url(emap):
    assert classify_url(PUB + "jwst/x.json", emap) == "public"
    assert classify_url(SEC + "jwst/x.json", emap) == "secure"
    assert classify_url("https://aladin.cds.unistra.fr/AladinLite/api/v3/latest/aladin.js") == \
        "external"
    assert classify_url("data/src/manifest.json") == "relative"
    assert classify_url("../legacy.html") == "relative"
    assert classify_url("/secure/adamginsburg/x.bin") == "secure"
    assert classify_url("/pub/adamginsburg/x.bin") == "public"
    assert classify_url("/other/x.bin") == "external"
    assert classify_url("//data.rc.ufl.edu/secure/adamginsburg/x.bin") == "secure"


# -- policy ---------------------------------------------------------------------------

def _exp(kind: str) -> Exposure:
    return Exposure(kind=kind, public_urls=[PUB + "x/"] if kind in ("public", "both") else [],
                    secure_urls=[SEC + "x/"] if kind in ("secure", "both") else [],
                    via=(["public: /web/public/x -> /orange/x"] if kind in ("public", "both")
                         else []) + (["secure: /web/secure"] if kind in ("secure", "both") else []))


@pytest.mark.parametrize("vis,kind,served_by,yes,level", [
    ("secure", "secure", None, False, "ok"),
    ("secure", "public", None, False, "refuse"),
    ("secure", "both", None, True, "refuse"),
    ("secure", "unserved", None, False, "refuse"),
    ("secure", "unserved", "external", False, "ok"),
    ("public", "public", None, False, "ok"),
    ("public", "both", None, False, "ok"),
    ("public", "secure", None, False, "refuse"),
    ("public", "secure", None, True, "warn"),
    ("public", "secure", "external", False, "warn"),
    ("public", "unserved", None, True, "refuse"),
    ("public", "unserved", "external", False, "ok"),
])
def test_target_policy(vis, kind, served_by, yes, level):
    d = check_target(vis, _exp(kind), served_by, yes)
    assert d.level == level
    assert d.exit_code == (3 if level == "refuse" else 0)


def test_secure_refusal_names_symlink():
    d = check_target("secure", _exp("both"))
    assert "/web/public/x -> /orange/x" in d.message


def test_public_both_prints_notice():
    d = check_target("public", _exp("both"))
    assert d.ok and "secure" in d.message


def test_reference_policy(tree, emap):
    fetched_secure = Reference(url=SEC + "jwst/a.bin", source="map.html", fetched=True)
    link_secure = Reference(url=SEC + "jwst/a.html", source="map.html", fetched=False)
    public_ok = Reference(url=PUB + "portalkit-res/filters.json", source="map.html")
    assert check_references("public", [public_ok], emap).level == "ok"
    d = check_references("public", [public_ok, link_secure], emap)
    assert d.level == "warn" and "login required" in d.notes[0]
    d = check_references("public", [fetched_secure, link_secure], emap)
    assert d.level == "refuse" and d.exit_code == 3
    assert check_references("secure", [fetched_secure], emap).level == "ok"
    # a relative URL escaping into a secure-only directory
    rel = Reference(url="../../../../orange/w51/portal/x.json", source="a.html",
                    resolved=str(tree["orange"] / "w51" / "portal" / "x.json"))
    assert check_references("public", [rel], emap).level == "refuse"


@pytest.mark.slow
def test_real_tree_readonly():
    """Read-only scan of the real served roots (slow on the shared filesystem)."""
    if not ex.DEFAULT_ROOTS["public"].is_dir():
        pytest.skip("real web roots not mounted")
    m = scan(cache_s=3600)
    o = Path("/orange/adamginsburg")
    assert classify(o / "jwst" / "sgrb2", m).kind == "both"
    assert classify(o / "jwst" / "sickle", m).kind == "both"
    assert classify(o / "w51" / "portal", m).kind == "secure"
    assert classify(o / "jwst" / "spectra" / "mastDownload" / "JWST" / "pngs", m).kind == "both"
    assert classify(o / "portalkit_stage", m).kind == "unserved"
