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


# ---------------------------------------------------------------------------
# the ownership manifest URL must be denied (WPE4, docs/deploy.md ".htaccess")
# ---------------------------------------------------------------------------

devmod = importlib.import_module("portalkit.deploy.devserver")
hmod = importlib.import_module("portalkit.deploy.htaccess")


@pytest.fixture(autouse=True)
def _default_deny_htaccess(monkeypatch):
    monkeypatch.delenv(dmod.DENY_HTACCESS_ENV, raising=False)


def test_manifest_url_5xx_is_an_allowoverride_warning(world, monkeypatch):
    """A 5xx means Apache rejects the .htaccess (AllowOverride without FileInfo): undecided,
    never 'SERVED'; owned files answering 5xx add one warning."""
    res = deploy(world, "public")
    t = res.plan.target
    url = "https://h/p/"

    def stub(status):
        return lambda *a, **k: (status, {}, [], None, None)

    monkeypatch.setattr(vmod, "_fetch", stub(500))
    d_, st, w = vmod.check_manifest_denied(url, dmod.MANIFEST_NAME, visibility="public",
                                           deny_expected=True)
    assert (d_, st) == (None, 500) and "AllowOverride" in w and "served" not in w.split(":")[0]
    assert vmod.check_manifest_denied(url, dmod.MANIFEST_NAME, visibility="public",
                                      deny_expected=False) == (None, 500, None)
    monkeypatch.setattr(vmod, "_fetch", stub(410))
    d_, st, w = vmod.check_manifest_denied(url, dmod.MANIFEST_NAME, visibility="public",
                                           deny_expected=True)
    assert (d_, st) == (None, 410) and "not confirmed" in w
    rep = vmod.verify(FAM, site(world, "public"), http=True, base_url=url,
                      emap=world["emap"], timeout=10)
    assert rep.manifest_denied is None and "SERVED" not in rep.format()
    monkeypatch.setattr(vmod, "_fetch", stub(500))
    rep = vmod.verify(FAM, site(world, "public"), http=True, base_url=url,
                      emap=world["emap"], timeout=10)
    assert not rep.ok and rep.manifest_denied is None
    allow = [w for w in rep.warnings if "AllowOverride" in w]
    assert len(allow) == 2 and any("owned URL(s) answered 5xx" in w for w in allow)
    assert "SERVED" not in rep.format()
    # no portalkit .htaccess: 5xx on owned files is a plain failure, no AllowOverride hint
    (t / ".htaccess").unlink()
    rep = vmod.verify(FAM, site(world, "public"), http=True, base_url=url,
                      emap=world["emap"], timeout=10)
    assert not rep.ok and rep.warnings == []


def test_manifest_url_denied_with_deny_rule(world):
    res = deploy(world, "public")                      # no --htaccess: deny-only file
    t = res.plan.target
    assert hmod.has_deny_rule(t) and not hmod.has_cache_headers(t)
    with DevServer(t.parent) as srv:
        rep = check(world, "public", srv)
        assert rep.ok and rep.manifest_denied is True and rep.warnings == []
        assert rep.htaccess is None                    # no cache headers expected
        assert "manifest URL denied" in rep.format()
        heads = [r for r in srv.requests if r.path.endswith(dmod.MANIFEST_NAME)]
        assert [(r.method, r.status) for r in heads] == [("HEAD", 403)]
        assert rep.to_json()["manifest_denied"] is True and rep.to_json()["warnings"] == []


def test_manifest_url_served_is_a_warning(world, monkeypatch):
    res = deploy(world, "public")
    t = res.plan.target
    # a host that ignores the .htaccess (AllowOverride None) serves the manifest
    monkeypatch.setattr(devmod, "denied", lambda path: False)
    with DevServer(t.parent) as srv:
        rep = check(world, "public", srv)
        assert rep.ok and rep.exit_code == 0           # a warning, not a failure
        assert rep.manifest_denied is False
        assert len(rep.warnings) == 1 and "not in effect" in rep.warnings[0]
        assert "answered 200" in rep.warnings[0] and dmod.MANIFEST_NAME in rep.warnings[0]
        assert "WARN" in rep.format() and "manifest URL SERVED" in rep.format()
        # a target with a foreign .htaccess (no portalkit deny rule): still a warning
        (t / ".htaccess").write_text("Options -Indexes\n")
        rep = check(world, "public", srv)
        assert rep.ok and rep.manifest_denied is False
        assert len(rep.warnings) == 1 and "no portalkit .htaccess deny rule" in rep.warnings[0]
    monkeypatch.undo()
    with DevServer(t.parent) as srv:                   # foreign file, but something denies it
        rep = check(world, "public", srv)
        assert rep.manifest_denied is True and rep.warnings == []
    (t / ".htaccess").unlink()
    deploy(world, "public")                            # the deny-only file comes back
    assert hmod.has_deny_rule(t)
    rep = check(world, "public", srv)                  # server stopped: unreachable
    assert rep.manifest_denied is None
    assert any("unreachable" in w and "not checked" in w for w in rep.warnings)


def test_manifest_url_secure_needs_credentials(world, monkeypatch):
    res = deploy(world, "secure", replace="stub")
    root = world["orange"] / "sec" / "portal"
    monkeypatch.delenv("PK_BASIC_AUTH", raising=False)
    with DevServer(root, auth="user:pw") as srv:
        rep = check(world, "secure", srv)
        assert rep.ok and rep.manifest_denied is None and rep.warnings == []
        assert any(dmod.MANIFEST_NAME in n and "401" in n for n in rep.notes)
        monkeypatch.setenv("PK_BASIC_AUTH", "user:pw")
        rep = check(world, "secure", srv)
        assert rep.ok and rep.manifest_denied is True and rep.warnings == []
    # the legacy directory's backup tree is denied too (backup .htaccess + devserver rule)
    bdir = root / ".portalkit_backup"
    assert hmod.has_deny_rule(bdir)
    assert res.replaced and hmod.denied(res.replaced[0]["backup"])


def test_check_manifest_denied_secondary_manifest(world):
    """A second portal in the same target has its own (secondary) manifest checked."""
    res = deploy(world, "public")
    t = res.plan.target
    d = world["stage"] / FAM / BID / "c"
    (d / "sub").mkdir(parents=True)
    (d / "sub" / "c.html").write_text("<p>c</p>")
    two = {"family": FAM, "portal": [{"id": "c", "visibility": "public",
                                      "target_dir": str(t), "page": []}]}
    dmod.deploy(FAM, BID, two, stage_root=world["stage"], emap=world["emap"])
    assert (t / ".portalkit-deploy.c.json").is_file()
    with DevServer(t.parent) as srv:
        base = srv.url + "unified/"
        rep = vmod.verify(FAM, two, http=True, base_url=base, emap=world["emap"], timeout=10)
        assert rep.ok and rep.manifest_denied is True, rep.format()
        assert any(r.path.endswith("/.portalkit-deploy.c.json") and r.status == 403
                   for r in srv.requests)
        assert vmod.check_manifest_denied(base, ".portalkit-deploy.c.json", visibility="public",
                                          deny_expected=True) == (True, 403, None)
        d_, st, w = vmod.check_manifest_denied(base, "map.html", visibility="public",
                                               deny_expected=True)
        assert (d_, st) == (False, 200) and "expected 403" in w
        d_, st, w = vmod.check_manifest_denied(base, "map.html", visibility="public",
                                               deny_expected=False)
        assert (d_, st) == (False, 200) and "deny rule" in w
        d_, st, w = vmod.check_manifest_denied(base, "nothing-here.json", visibility="public",
                                               deny_expected=False)
        assert (d_, st, w) == (True, 404, None)
