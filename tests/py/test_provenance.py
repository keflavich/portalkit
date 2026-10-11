"""Served provenance carries labelled paths (portalkit.build.PathLabeler, section 9.3)."""

from __future__ import annotations

from portalkit.build import LOCAL_PATH_RE, PathLabeler, _static_prefix
from portalkit.context import SiteConfig


def labeler() -> PathLabeler:
    return PathLabeler([("legacy_dir", "/orange/p/n6927"), ("legacy", "/orange/p/analysis"),
                        ("input", "/orange/p/spectra"), ("input", "/orange/p/n6927")])


def test_labels_longest_root_and_fallback():
    lab = labeler()
    assert lab("/orange/p/n6927/specdata/a.json") == "legacy_dir:specdata/a.json"
    assert lab("/orange/p/spectra/data_L3/x_x1d.fits") == "input:data_L3/x_x1d.fits"
    assert lab("/orange/p/analysis/Mod.py") == "legacy:Mod.py"
    assert lab("/orange/elsewhere/MIST.iso") == "file:MIST.iso"
    assert lab("file:///orange/p/analysis/Mod.py") == "legacy:Mod.py"
    assert lab("relative/x.json") == "relative/x.json"
    # a deeper root wins over its parent
    deep = PathLabeler([("input", "/orange/p"), ("legacy_dir", "/orange/p/n6927")])
    assert deep("/orange/p/n6927/x") == "legacy_dir:x"
    assert deep("/orange/p/other/x") == "input:other/x"


def test_scrub_keeps_urls_and_relative_paths():
    lab = labeler()
    doc = {"inputs": [{"path": "/orange/p/n6927/a.ecsv", "size": 3}],
           "url": "https://data.rc.ufl.edu/secure/adamginsburg/x.fits", "rel": "/secure/x",
           "list": ["/home/someone/f.py", "data/src/manifest.json"], "n": 2}
    out = lab.scrub(doc)
    assert out["inputs"][0] == {"path": "legacy_dir:a.ecsv", "size": 3}
    assert out["url"] == doc["url"] and out["rel"] == "/secure/x"
    assert out["list"] == ["file:f.py", "data/src/manifest.json"] and out["n"] == 2
    assert lab.scrub(out) == out          # idempotent


def test_for_site_roots():
    site = SiteConfig.from_dict({
        "family": "fam", "adapter": "adapters.x:A", "legacy_paths": ["/orange/p/analysis"],
        "portal": [{"id": "fam", "title": "t", "visibility": "secure",
                    "legacy_dir": "/orange/p/n6927", "url": "https://h/x/",
                    "inputs": ["/orange/p/spectra/data_L3/*_x1d.fits",
                               "/orange/p/catalogs/cat.fits"]}]})
    lab = PathLabeler.for_site(site)
    assert lab("/orange/p/spectra/data_L3/a_x1d.fits") == "input:data_L3/a_x1d.fits"
    assert lab("/orange/p/catalogs/cat.fits") == "input:cat.fits"
    assert lab("/orange/p/n6927/specdata/s.json") == "legacy_dir:specdata/s.json"
    assert _static_prefix("/a/b/*/c?.fits") == "/a/b"


def test_local_path_pattern():
    hits = lambda s: LOCAL_PATH_RE.findall(s)  # noqa: E731
    assert hits('"/orange/a/b.fits"') == ["/orange/a/b.fits"]
    assert hits('x file:///blue/y z') == ["file:///blue/y"]
    assert hits('"/home/u/f.py"') == ["/home/u/f.py"]
    assert hits("https://host/home/x") == [] and hits("a/orange/b") == []
    assert hits('"/secure/adamginsburg/x"') == []
    # after a colon or a dash (a 'label:' prefix, a flag value), not in relative/URL forms
    assert hits("source:/orange/adamginsburg/x.fits") == ["/orange/adamginsburg/x.fits"]
    assert hits("src:/blue/adamginsburg/f.fits") == ["/blue/adamginsburg/f.fits"]
    assert hits("a-/orange/x") == ["/orange/x"]
    assert hits("https://host:8080/orange/x") == [] and hits("./orange/x") == []
    assert hits("~/home/x") == [] and hits("s3://orange/x") == []


def test_scrub_keys_and_embedded_paths():
    lab = labeler()
    out = lab.scrub({"/orange/p/n6927/k.fits": "see /orange/p/analysis/Mod.py, then src:/blue/q/r.txt.",
                     "note": ["(/home/u/f.py)"]})
    assert out == {"legacy_dir:k.fits": "see legacy:Mod.py, then src:file:r.txt.",
                   "note": ["(file:f.py)"]}
    assert not LOCAL_PATH_RE.search(str(out))
    assert lab.scrub(out) == out
