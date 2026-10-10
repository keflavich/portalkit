"""verify: local checks and HTTP checks against the development server."""

from __future__ import annotations

import importlib
import json
import os
import urllib.request
from pathlib import Path

import pytest

from portalkit.deploy import DevServer
from portalkit.deploy.exposure import scan

dmod = importlib.import_module("portalkit.deploy.deploy")
vmod = importlib.import_module("portalkit.deploy.verify")

FAM = "fam"
BID = "20261010T120000Z-0123abcd"
BIN = bytes(range(256)) * 8


def stage_portal(root: Path) -> Path:
    d = root / FAM / BID / "main"
    files = {
        "portal.json": json.dumps({"pages": [{"id": "map", "file": "map.html"}]}).encode(),
        "map.html": b"<!doctype html><p>map</p>\n",
        "data/src/manifest.json": b'{"columns": []}',
        "data/src/cols.0123abcd.bin": BIN,
        "data/Ünï name.0123abcd.json": b"[]",
    }
    for rel, data in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return d


@pytest.fixture()
def world(tmp_path: Path) -> dict:
    o = tmp_path / "orange"
    (o / "sec" / "portal").mkdir(parents=True)
    (o / "pub").mkdir(parents=True)
    pub, sec = tmp_path / "web" / "public", tmp_path / "web" / "secure"
    pub.mkdir(parents=True)
    sec.mkdir(parents=True)
    os.symlink(o / "sec", sec / "sec")
    os.symlink(o / "pub", pub / "pub")
    stage = tmp_path / "stage"
    stage_portal(stage)
    (o / "sec" / "portal" / "old.html").write_text("legacy")
    return {"orange": o, "stage": stage,
            "emap": scan({"public": pub, "secure": sec}, cache_s=0)}


def site(world: dict, vis: str) -> dict:
    where = "sec/portal" if vis == "secure" else "pub"
    return {"family": FAM, "portal": [{
        "id": "main", "visibility": vis, "legacy_dir": str(world["orange"] / where),
        "page": [{"id": "map", "legacy": ["old.html"] if vis == "secure" else []}]}]}


def deploy(world: dict, vis: str, **kw):
    return dmod.deploy(FAM, BID, site(world, vis), stage_root=world["stage"],
                       emap=world["emap"], **kw)


def check(world, vis, srv, **kw):
    return vmod.verify(FAM, site(world, vis), http=True, base_url=srv.url + "unified/",
                       emap=world["emap"], timeout=10, **kw)


def test_local_checks(world):
    s = site(world, "public")
    rep = vmod.verify(FAM, s, emap=world["emap"])
    assert not rep.ok and rep.local == [f"no ownership manifest in {world['orange'] / 'pub' / 'unified'}"]
    res = deploy(world, "public")
    t = res.plan.target
    rep = vmod.verify(FAM, s, emap=world["emap"])
    assert rep.ok and rep.exit_code == 0 and rep.checks == []
    (t / "map.html").write_text("tampered")
    (t / "data/src/cols.0123abcd.bin").unlink()
    (t / "data/x.bin.pk-tmp").write_text("")
    rep = vmod.verify(FAM, s, emap=world["emap"])
    assert rep.exit_code == 1
    assert "sha256 mismatch: map.html" in rep.local
    assert "missing: data/src/cols.0123abcd.bin" in rep.local
    assert any("leftover temporary file" in p for p in rep.local)
    assert "FAILED" in rep.format()


def test_exposure_drift_is_reported(world):
    deploy(world, "secure", replace="stub")
    # someone links the secure-only portal into the public tree after the deploy
    pub = world["orange"].parent / "web" / "public"
    os.symlink(world["orange"] / "sec" / "portal", pub / "leak")
    emap = scan({"public": pub, "secure": world["orange"].parent / "web" / "secure"}, cache_s=0)
    rep = vmod.verify(FAM, site(world, "secure"), emap=emap)
    assert any(p.startswith("exposure:") for p in rep.local)
    (world["orange"] / "sec" / "portal" / "old.html").unlink()
    rep = vmod.verify(FAM, site(world, "secure"), emap=world["emap"])
    assert rep.local == ["replaced legacy file missing: ../old.html"]


def test_public_http(world):
    res = deploy(world, "public")
    with DevServer(res.plan.target.parent) as srv:
        rep = check(world, "public", srv)
        assert rep.ok, rep.format()
        assert len(rep.checks) == len(res.manifest["files"])
        c = {x.path: x for x in rep.checks}
        assert c["data/src/cols.0123abcd.bin"].content_type == "application/octet-stream"
        assert c["map.html"].sha256 == res.manifest["files"]["map.html"]["sha256"]
        assert rep.htaccess is None
        (res.plan.target / "map.html").write_text("changed on disk")
        rep = check(world, "public", srv)
        bad = [x for x in rep.checks if not x.ok]
        assert [x.path for x in bad] == ["map.html"]
        assert "sha256 differs" in bad[0].problems[0]
    rep = check(world, "public", srv)                     # server stopped
    assert all(not x.ok and "unreachable" in x.problems[0] for x in rep.checks)


def test_public_portal_behind_auth_fails_even_with_credentials(world, monkeypatch):
    res = deploy(world, "public")
    monkeypatch.setenv("PK_BASIC_AUTH", "u:p")
    with DevServer(res.plan.target.parent, auth="u:p") as srv:
        rep = check(world, "public", srv)
        assert not rep.ok
        assert all(x.status == 401 for x in rep.checks)
        assert not any(r.auth for r in srv.requests)


def test_secure_http(world, monkeypatch):
    res = deploy(world, "secure", replace="stub")
    root = world["orange"] / "sec" / "portal"
    monkeypatch.delenv("PK_BASIC_AUTH", raising=False)
    with DevServer(root, auth="user:pw") as srv:
        rep = check(world, "secure", srv)
        assert rep.ok, rep.format()
        assert not rep.authenticated and any("PK_BASIC_AUTH" in n for n in rep.notes)
        assert all(x.status == 401 for x in rep.checks)
        assert {r.method for r in srv.requests} == {"HEAD", "GET"}
        monkeypatch.setenv("PK_BASIC_AUTH", "user:pw")
        rep = check(world, "secure", srv)
        assert rep.ok and rep.authenticated, rep.format()
        files = [x for x in rep.checks if x.path in res.manifest["files"]]
        assert all(x.auth_status == 200 and x.sha256 == x.expected for x in files)
        assert any(x.path == "../old.html" and x.status == 200 for x in rep.checks)
        monkeypatch.setenv("PK_BASIC_AUTH", "user:wrong")
        rep = check(world, "secure", srv)
        assert not rep.ok
        monkeypatch.setenv("PK_BASIC_AUTH", "no-colon")
        rep = check(world, "secure", srv)
        assert any("user:pass" in p for p in rep.local)
    # a secure portal served without auth fails
    monkeypatch.delenv("PK_BASIC_AUTH")
    with DevServer(root) as srv:
        rep = check(world, "secure", srv)
        assert not rep.ok
        assert any("without credentials" in p for x in rep.checks for p in x.problems)


def test_content_encoding_on_bin_fails(world):
    res = deploy(world, "public")
    with DevServer(res.plan.target.parent, content_encoding="gzip") as srv:
        rep = check(world, "public", srv)
    bad = {x.path: x for x in rep.checks if not x.ok}
    assert list(bad) == ["data/src/cols.0123abcd.bin"]
    assert "Content-Encoding 'gzip'" in bad["data/src/cols.0123abcd.bin"].problems[0]
    # the compressed JSON and HTML still match through their decoded sha256
    ok = {x.path: x for x in rep.checks if x.ok}
    assert ok["map.html"].content_encoding == "gzip"


def test_htaccess_effectiveness(world):
    res = deploy(world, "public", htaccess=True)
    target_parent = res.plan.target.parent
    with DevServer(target_parent, cache_control="htaccess") as srv:
        rep = check(world, "public", srv)
        assert rep.ok and rep.htaccess is True
        c = {x.path: x for x in rep.checks}
        assert c["data/src/cols.0123abcd.bin"].cache_control.endswith("immutable")
        assert c["portal.json"].cache_control == "no-cache"
    with DevServer(target_parent) as srv:                 # AllowOverride None
        rep = check(world, "public", srv)
        assert rep.ok and rep.htaccess is False
        assert any("not in effect" in n for n in rep.notes)
        assert "NOT in effect" in rep.format()


def test_verify_http_with_custom_opener(world):
    """``opener`` lets callers inject handlers; here one that rewrites every status to 500."""
    class Broken(urllib.request.BaseHandler):
        def http_response(self, request, response):
            response.status = 500
            return response

    res = deploy(world, "public")
    with DevServer(res.plan.target.parent) as srv:
        checks, eff = vmod.verify_http(srv.url + "unified/", {"map.html": {"sha256": "x"}},
                                       visibility="public",
                                       opener=urllib.request.build_opener(Broken))
    assert checks[0].status == 500 and not checks[0].ok


def test_file_url_quotes():
    assert vmod.file_url("https://h/p", "data/Ünï name.json") == \
        "https://h/p/data/%C3%9Cn%C3%AF%20name.json"
