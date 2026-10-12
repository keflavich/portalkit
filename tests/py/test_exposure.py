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


# -- scan cost: shared listings, directory stamps, cache fallback (WPE3) ----------------------

def _reference_scan(roots: dict, depth: int = 4, prune: tuple = ("*_hips",)) -> tuple[list, list]:
    """The pre-WPE3 walk (one listing per route), kept as the classification oracle."""
    import fnmatch
    import stat as st_

    def list_dir(logical):
        warnings, out = [], []
        try:
            with os.scandir(logical) as it:
                dents = sorted(it, key=lambda de: de.name)
        except OSError as exc:
            return [], [f"cannot list {logical}: {exc.strerror or exc}"]
        for de in dents:
            if any(fnmatch.fnmatch(de.name, pat) for pat in prune):
                continue
            if de.is_symlink():
                try:
                    st = os.stat(de.path)
                except OSError:
                    warnings.append(f"dangling symlink {de.path}")
                    continue
                if not st_.S_ISDIR(st.st_mode):
                    continue
                out.append(("link", de.name, de.path, os.path.realpath(de.path), os.readlink(de.path)))
            elif de.is_dir(follow_symlinks=False):
                out.append(("dir", de.name, de.path))
        return out, warnings

    entries, warnings = [], []
    for kind in ("public", "secure"):
        root = os.path.abspath(roots[kind])
        prefix = ex.URL_PREFIXES[kind]
        root_real = os.path.realpath(root)
        if not os.path.isdir(root_real):
            warnings.append(f"{kind} root {root} is not a directory")
            continue
        entries.append((root_real, kind, ex._join_url(prefix, ""), (f"{kind}: {root}",)))
        expanded = {root_real}
        level = [(root, "", (f"{kind}: {root}",), frozenset([root_real]))]
        d = 0
        while level and d < depth:
            nxt = []
            for logical, rel, chain, anc in level:
                children, warns = list_dir(logical)
                warnings.extend(warns)
                for ch in children:
                    child_rel = f"{rel}/{ch[1]}" if rel else ch[1]
                    if ch[0] == "dir":
                        nxt.append((ch[2], child_rel, chain, anc))
                        continue
                    hop = chain + (f"{ch[2]} -> {ch[4]}",)
                    entries.append((ch[3], kind, ex._join_url(prefix, child_rel), hop))
                    if ch[3] not in anc and ch[3] not in expanded:
                        expanded.add(ch[3])
                        nxt.append((ch[2], child_rel, hop, anc | {ch[3]}))
            level = nxt
            d += 1
    return entries, warnings


def _sig(m) -> tuple[list, list]:
    return [(e.real, e.kind, e.url, e.via) for e in m.entries], list(m.warnings)


def _shared_tree(tree) -> dict:
    """The fake tree plus routes that reach the same directories several times."""
    o, pub, sec = tree["orange"], tree["public"], tree["secure"]
    (o / "jwst" / "sgrb2" / "sub" / "deeper").mkdir(parents=True)
    os.symlink(o / "w51" / "portal", o / "jwst" / "sgrb2" / "sub" / "w51p")
    os.symlink(tree["base"] / "nowhere", o / "jwst" / "sgrb2" / "dangling ü")   # seen per route
    (o / "jwst" / "sgrb2" / "x_hips").mkdir()
    os.symlink(o / "w51", o / "jwst" / "sgrb2" / "x_hips" / "hidden")
    os.symlink(o / "jwst" / "sgrb2", pub / "sgrb2_again")
    os.symlink(o / "jwst", pub / "jwst" / "all")
    (o / "jwst" / "notes.txt").write_text("x")
    os.symlink(o / "jwst" / "notes.txt", o / "jwst" / "sgrb2" / "file_link")
    return {"public": pub, "secure": sec}


@pytest.mark.parametrize("depth,prune", [(4, ("*_hips",)), (6, ()), (1, ("*_hips",)), (0, ())])
def test_shared_listings_match_reference_walk(tree, depth, prune):
    roots = _shared_tree(tree)
    m = scan(roots, depth=depth, prune=prune, cache_s=0)
    ref_entries, ref_warnings = _reference_scan(roots, depth, prune)
    assert _sig(m) == (ref_entries, ref_warnings)
    if depth >= 4:
        assert any("dangling ü" in w for w in m.warnings)
        # several routes reach the same directories: each is listed once
        assert m.stats["shared"] > 0
        assert m.stats["scandir"] == m.stats["dirs"]
        o = tree["orange"]
        for p, kind in ((o / "jwst" / "sgrb2" / "sub", "both"), (o / "w51" / "portal", "both"),
                        (o / "jwst" / "spectra" / "nirspec_6927", "both")):
            assert classify(p, m).kind == kind, p


def test_listing_counts_on_the_fake_tree(tree):
    """Call counts reported by the walk: one scandir per distinct real directory."""
    roots = _shared_tree(tree)
    m = scan(roots, cache_s=0)
    st = m.stats
    assert st["scandir"] == st["dirs"] and st.get("reused", 0) == 0
    assert st["readlink"] == st["realpath"] > 0
    assert "stat" in st and st["wall_s"] >= 0
    assert st["shared"] >= 3


def _later(monkeypatch, seconds: float) -> None:
    real_time = ex.time.time
    monkeypatch.setattr(ex.time, "time", lambda: real_time() + seconds)


def _age_tree(base: Path, monkeypatch, seconds: float = 3600.0) -> None:
    """Make every directory's stamp trusted (outside the racy window): mtimes go into the
    past, and since ``utime`` sets ctime to now (users cannot set ctime), the scan clock moves
    forward by more than ``_RACY_S``."""
    t = ex.time.time() - seconds
    for dirpath, dirnames, _ in os.walk(base):
        os.utime(dirpath, (t, t))
    _later(monkeypatch, 6 * ex._RACY_S)


def test_expired_cache_revalidates_by_stamps(tree, tmp_path, monkeypatch):
    monkeypatch.setenv("PK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    ex.clear_cache()
    roots = _shared_tree(tree)
    _age_tree(tree["base"], monkeypatch)
    m1 = scan(roots, cache_s=60)
    assert m1.stats.get("reused", 0) == 0
    files = list((tmp_path / "cache").glob("exposure-*.json"))
    assert len(files) == 1
    import json as _json
    doc = _json.loads(files[0].read_text())
    assert doc["version"] == 3 and doc["dirs"]
    assert all(len(v) == 6 for v in doc["dirs"].values())
    _later(monkeypatch, 120)                     # past cache_s: revalidate
    ex.clear_cache()
    m2 = scan(roots, cache_s=60)
    assert _sig(m2) == _sig(m1) == _sig(scan(roots, cache_s=0))
    assert m2.stats.get("scandir", 0) == 0 and m2.stats["reused"] == m2.stats["dirs"]
    # symlinks are resolved again even when their directory is reused
    assert m2.stats["realpath"] == m1.stats["realpath"]
    assert m2.scanned > m1.scanned


def test_stamps_detect_changes(tree, tmp_path, monkeypatch):
    """A new symlink changes its directory's mtime; a link whose target disappears is
    re-resolved although its own directory is unchanged."""
    monkeypatch.setenv("PK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    ex.clear_cache()
    o = tree["orange"]
    roots = _shared_tree(tree)
    (o / "gone" / "inner").mkdir(parents=True)
    os.symlink(o / "gone" / "inner", tree["public"] / "jwst" / "wd2" / "to_inner")
    _age_tree(tree["base"], monkeypatch)
    m1 = scan(roots, cache_s=60)
    assert classify(o / "gone" / "inner", m1).kind == "both"     # also via secure/wd2mirror
    # change 1: a new link in a stamped directory
    os.symlink(o / "robitaille_models" / "portal", tree["public"] / "rportal")
    # change 2: the target of an existing link disappears; the link's directory keeps its stamp
    os.rmdir(o / "gone" / "inner")
    _later(monkeypatch, 120)
    ex.clear_cache()
    m2 = scan(roots, cache_s=60)
    assert _sig(m2) == _sig(scan(roots, cache_s=0))
    assert classify(o / "robitaille_models" / "portal", m2).kind == "both"
    assert classify(o / "gone" / "inner", m2).kind == "unserved"
    assert any(w.endswith("to_inner") and w.startswith("dangling") for w in m2.warnings)
    assert 0 < m2.stats["reused"] < m2.stats["dirs"]
    assert m2.stats["scandir"] >= 1                 # the public root was listed again


def _restore_modes(base: Path) -> None:
    for dirpath, dirnames, _ in os.walk(base):
        for d in dirnames:
            p = os.path.join(dirpath, d)
            if not os.path.islink(p):
                os.chmod(p, 0o755)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root can list any directory")
def test_stamps_detect_permission_change(tree, tmp_path, monkeypatch):
    """chmod changes only ctime: a directory made unlistable is listed again (and fails, as
    in a fresh walk) although its mtime is unchanged."""
    monkeypatch.setenv("PK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    ex.clear_cache()
    o = tree["orange"]
    roots = _shared_tree(tree)
    sub = tree["public"] / "permtest"
    sub.mkdir()
    (o / "permdata").mkdir()
    os.symlink(o / "permdata", sub / "lnk")
    _age_tree(tree["base"], monkeypatch)
    m1 = scan(roots, cache_s=60)
    assert classify(o / "permdata", m1).kind == "public"
    mt = sub.stat().st_mtime_ns
    os.chmod(sub, 0o311)                            # searchable, not listable
    try:
        os.utime(sub, ns=(mt, mt))
        assert sub.stat().st_mtime_ns == mt
        _later(monkeypatch, 120)
        ex.clear_cache()
        m2 = scan(roots, cache_s=60)
        fresh = scan(roots, cache_s=0)
        assert _sig(m2) == _sig(fresh)
        assert classify(o / "permdata", m2).kind == "unserved"
        assert any(w.startswith(f"cannot list {sub}") for w in m2.warnings)
        assert 0 < m2.stats["reused"] < m2.stats["dirs"]
    finally:
        os.chmod(sub, 0o755)
    ex.clear_cache()


def test_stamps_detect_link_swap_with_restored_mtime(tree, tmp_path, monkeypatch):
    """A symlink replaced while the directory mtime is restored (touch -r) leaves
    (dev, ino, mtime) unchanged; ctime still changes, so the directory is listed again."""
    monkeypatch.setenv("PK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    ex.clear_cache()
    o = tree["orange"]
    roots = _shared_tree(tree)
    sub = tree["public"] / "swaptest"
    sub.mkdir()
    (o / "swap1").mkdir()
    (o / "swap2").mkdir()
    os.symlink(o / "swap1", sub / "lnk")
    _age_tree(tree["base"], monkeypatch)
    m1 = scan(roots, cache_s=60)
    assert classify(o / "swap1", m1).kind == "public"
    assert classify(o / "swap2", m1).kind == "unserved"
    st = sub.stat()
    os.unlink(sub / "lnk")
    os.symlink(o / "swap2", sub / "lnk2")
    os.utime(sub, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert (sub.stat().st_ino, sub.stat().st_mtime_ns) == (st.st_ino, st.st_mtime_ns)
    _later(monkeypatch, 120)
    ex.clear_cache()
    m2 = scan(roots, cache_s=60)
    assert _sig(m2) == _sig(scan(roots, cache_s=0))
    assert classify(o / "swap2", m2).kind == "public"
    assert classify(o / "swap1", m2).kind == "unserved"
    assert not any("swaptest/lnk" in w for w in m2.warnings)
    ex.clear_cache()


@pytest.mark.parametrize("bad", [
    [["only-name"]], [["x", "q"]], [[1, "d"]], [["../up", "d"]], "notalist", None,
])
def test_malformed_stamp_is_ignored(tree, tmp_path, monkeypatch, bad):
    """A malformed directory stamp in the cache file costs a scandir, never an error."""
    import json as _json
    monkeypatch.setenv("PK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    ex.clear_cache()
    roots = _shared_tree(tree)
    _age_tree(tree["base"], monkeypatch)
    m1 = scan(roots, cache_s=60)
    (f,) = list((tmp_path / "cache").glob("exposure-*.json"))
    doc = _json.loads(f.read_text())
    key = sorted(doc["dirs"])[0]
    if isinstance(bad, list):
        doc["dirs"][key][5] = bad
    else:
        doc["dirs"][key] = bad
    f.write_text(_json.dumps(doc))
    _later(monkeypatch, 120)
    ex.clear_cache()
    m2 = scan(roots, cache_s=60)
    assert _sig(m2) == _sig(m1)
    assert m2.stats["scandir"] == 1 and m2.stats["reused"] == m2.stats["dirs"] - 1
    ex.clear_cache()


def test_racy_stamps_are_not_reused(tree, tmp_path, monkeypatch):
    """Directories modified within the racy window of their listing are listed again."""
    monkeypatch.setenv("PK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    ex.clear_cache()
    roots = _shared_tree(tree)                      # fresh mtimes: inside the racy window
    m1 = scan(roots, cache_s=60)
    _later(monkeypatch, 120)
    ex.clear_cache()
    m2 = scan(roots, cache_s=60)
    assert m2.stats.get("reused", 0) == 0 and m2.stats["scandir"] == m1.stats["scandir"]
    assert _sig(m2) == _sig(m1)


def test_cache_disabled_writes_and_reads_nothing(tree, tmp_path, monkeypatch):
    monkeypatch.setenv("PK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    ex.clear_cache()
    roots = _shared_tree(tree)
    _age_tree(tree["base"], monkeypatch)
    scan(roots, cache_s=60)
    _later(monkeypatch, 120)
    files = sorted((tmp_path / "cache").glob("exposure-*.json"))
    stamp = [(f, f.stat().st_mtime_ns) for f in files]
    m = scan(roots, cache_s=0)
    assert m.stats.get("reused", 0) == 0 and m.stats["scandir"] == m.stats["dirs"]
    assert [(f, f.stat().st_mtime_ns) for f in files] == stamp
    assert sorted((tmp_path / "cache").glob("exposure-*.json")) == files
    ex.clear_cache()


def test_old_format_cache_is_ignored(tree, tmp_path, monkeypatch):
    import json as _json
    monkeypatch.setenv("PK_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    ex.clear_cache()
    roots = {"public": tree["public"], "secure": tree["secure"]}
    m1 = scan(roots, cache_s=3600)
    (f,) = list((tmp_path / "cache").glob("exposure-*.json"))
    doc = _json.loads(f.read_text())
    doc["version"] = 1
    doc["entries"] = []                              # a stale v1 file must not be used
    f.write_text(_json.dumps(doc))
    ex.clear_cache()
    m2 = scan(roots, cache_s=3600)
    assert _sig(m2) == _sig(m1) and m2.stats["scandir"] > 0
    assert _json.loads(f.read_text())["version"] == 3
    f.write_text("{not json")
    ex.clear_cache()
    assert _sig(scan(roots, cache_s=3600)) == _sig(m1)
    ex.clear_cache()


def test_default_cache_dir_is_read_only_fallback(tree, tmp_path, monkeypatch):
    """With $PK_CACHE_DIR set elsewhere, a scan of the same roots in ~/.cache/portalkit is
    used (fresh: as is; expired: for its stamps) and never rewritten."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PK_CACHE_DIR", raising=False)
    ex.clear_cache()
    roots = _shared_tree(tree)
    _age_tree(tree["base"], monkeypatch)
    m1 = scan(roots, cache_s=60)
    (dflt,) = list((home / ".cache" / "portalkit").glob("exposure-*.json"))
    before = (dflt.stat().st_mtime_ns, dflt.read_bytes())
    monkeypatch.setenv("PK_CACHE_DIR", str(tmp_path / "isolated"))
    ex.clear_cache()
    m2 = scan(roots, cache_s=60)                     # fresh in the default dir
    assert _sig(m2) == _sig(m1) and m2.stats == {}
    assert not (tmp_path / "isolated").exists()
    _later(monkeypatch, 120)
    ex.clear_cache()
    m3 = scan(roots, cache_s=60)                     # expired: stamps from the default dir
    assert _sig(m3) == _sig(m1) and m3.stats["reused"] == m3.stats["dirs"]
    assert (dflt.stat().st_mtime_ns, dflt.read_bytes()) == before
    assert list((tmp_path / "isolated").glob("exposure-*.json"))
    ex.clear_cache()


def test_memory_cache_and_stamps_per_process(tree, tmp_path, monkeypatch):
    """In one process an expired result is revalidated from the in-memory stamps when the
    disk cache cannot be written."""
    monkeypatch.setenv("PK_CACHE_DIR", str(tmp_path / "ro"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(ex, "_write_cache", lambda *a, **k: None)
    ex.clear_cache()
    roots = _shared_tree(tree)
    _age_tree(tree["base"], monkeypatch)
    m1 = scan(roots, cache_s=60)
    assert scan(roots, cache_s=60) is m1             # memory hit
    _later(monkeypatch, 120)
    m2 = scan(roots, cache_s=60)
    assert m2 is not m1 and _sig(m2) == _sig(m1)
    assert m2.stats["reused"] == m2.stats["dirs"]
    ex.clear_cache()


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


# ---------------------------------------------------------------------------
# $PK_EXPOSURE_CACHE_S (WPE4)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,want", [
    (None, 3600), ("", 3600), ("   ", 3600),
    ("86400", 86400), (" 60 ", 60), ("0", 0), ("3600.9", 3600), ("1e3", 1000),
    ("-5", 3600), ("-0.5", 3600), ("abc", 3600), ("12s", 3600), ("nan", 3600),
    ("inf", 3600), ("-inf", 3600), ("1e400", 3600), ("０", 0),   # fullwidth digit: float() accepts it
    ("1_000", 1000), ("0x10", 3600), ("ünï", 3600),
])
def test_default_cache_s_env_parsing(raw, want, monkeypatch):
    env = {} if raw is None else {ex.CACHE_S_ENV: raw}
    assert ex.default_cache_s(env) == want
    if raw is None:
        monkeypatch.delenv(ex.CACHE_S_ENV, raising=False)
    else:
        monkeypatch.setenv(ex.CACHE_S_ENV, raw)
    assert ex.default_cache_s() == want
    assert ex.DEFAULT_CACHE_S == 3600 and ex.CACHE_S_ENV == "PK_EXPOSURE_CACHE_S"


def test_scan_default_cache_s_follows_env(tree, tmp_path, monkeypatch):
    monkeypatch.setenv("PK_CACHE_DIR", str(tmp_path / "cache"))
    roots = {"public": tree["public"], "secure": tree["secure"]}
    target = tree["orange"] / "w51" / "portal"
    ex.clear_cache()
    try:
        # 0 from the environment disables the caches: nothing written, every scan fresh
        monkeypatch.setenv(ex.CACHE_S_ENV, "0")
        assert scan(roots).stats.get("scandir", 0) > 0
        assert not list((tmp_path / "cache").glob("exposure-*.json"))
        # a day: the second scan is the cached map (no walk) even after a change on disk
        monkeypatch.setenv(ex.CACHE_S_ENV, "86400")
        m1 = scan(roots)
        assert list((tmp_path / "cache").glob("exposure-*.json"))
        os.symlink(target, tree["public"] / "w51portal")
        m2 = scan(roots)
        assert m2 is m1 and classify(target, m2).kind == "secure"
        # an explicit cache_s still wins over the environment
        assert classify(target, scan(roots, cache_s=0)).kind == "both"
        # a bad value falls back to the hour: a fresh in-memory result is still reused
        monkeypatch.setenv(ex.CACHE_S_ENV, "soon")
        assert scan(roots) is m1
        # classification for deploy is unchanged: once the cached map expires it is
        # revalidated, and the revalidated map equals a fresh walk
        ex.clear_cache()
        monkeypatch.setenv(ex.CACHE_S_ENV, "0")
        fresh = scan(roots)
        assert classify(target, fresh).kind == "both"
    finally:
        ex.clear_cache()


def _load_conftest():
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "conftest.py"
    spec = importlib.util.spec_from_file_location("pk_conftest_copy", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_conftest_sets_exposure_cache_for_adapter_tests_only():
    ct = _load_conftest()
    env: dict = {}
    ct._EXPOSURE_ENV.update(user=False, set=False)
    ct.exposure_env_for(False, env)
    assert env == {}
    ct.exposure_env_for(True, env)
    assert env == {"PK_EXPOSURE_CACHE_S": "86400"}
    assert ex.default_cache_s(env) == 86400
    ct.exposure_env_for(True, env)                    # stays while adapter tests run
    assert env == {"PK_EXPOSURE_CACHE_S": "86400"}
    ct.exposure_env_for(False, env)                   # removed for the next unit test
    assert env == {}
    # a value the user set is never changed or removed
    env = {"PK_EXPOSURE_CACHE_S": "5"}
    ct._EXPOSURE_ENV.update(user=True, set=False)
    ct.exposure_env_for(True, env)
    ct.exposure_env_for(False, env)
    assert env == {"PK_EXPOSURE_CACHE_S": "5"}

