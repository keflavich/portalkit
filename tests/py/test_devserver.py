"""Development server: auth, Range, Content-Encoding modes, latency, request log."""

from __future__ import annotations

import base64
import gzip
import http.client
import json
import time
from pathlib import Path

import pytest

from portalkit.deploy.devserver import DevServer, guess_type

BLOB = bytes(range(256)) * 4            # 1024 bytes


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "cols.0123abcd.bin").write_bytes(BLOB)
    (tmp_path / "data" / "manifest.json").write_text('{"n": 1}')
    (tmp_path / "data" / "big.json.gz").write_bytes(gzip.compress(b'{"gz": true}'))
    (tmp_path / "index.html").write_text("<!doctype html><p>index</p>")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_text("a")
    (tmp_path / ".htaccess").write_text("secret")
    (tmp_path / "Ünï dir").mkdir()
    (tmp_path / "Ünï dir" / "x.csv").write_text("a,b\n")
    return tmp_path


def req(srv: DevServer, path: str, method: str = "GET", headers: dict | None = None):
    conn = http.client.HTTPConnection(srv.host, srv.port, timeout=10)
    try:
        conn.request(method, path, headers=headers or {})
        r = conn.getresponse()
        body = r.read()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, body
    finally:
        conn.close()


def basic(cred: str) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(cred.encode()).decode()}


def test_plain_get_head_and_types(root):
    with DevServer(root) as srv:
        assert srv.url == f"http://127.0.0.1:{srv.port}/"
        st, h, body = req(srv, "/data/cols.0123abcd.bin")
        assert st == 200 and body == BLOB
        assert h["content-type"] == "application/octet-stream"
        assert h["accept-ranges"] == "bytes" and h["content-length"] == "1024"
        assert "content-encoding" not in h
        st, h, body = req(srv, "/data/cols.0123abcd.bin", "HEAD")
        assert st == 200 and body == b"" and h["content-length"] == "1024"
        st, h, body = req(srv, "/data/manifest.json")
        assert h["content-type"] == "application/json; charset=utf-8"
        st, _, body = req(srv, "/")
        assert st == 200 and b"index" in body
        st, h, _ = req(srv, "/sub")
        assert st == 301 and h["location"] == "/sub/"
        st, _, body = req(srv, "/sub/")
        assert st == 200 and b"a.txt" in body
        st, _, body = req(srv, "/%C3%9Cn%C3%AF%20dir/x.csv")
        assert st == 200 and body == b"a,b\n"
        assert req(srv, "/missing.json")[0] == 404
        assert req(srv, "/.htaccess")[0] == 403
        assert req(srv, "/data/../index.html")[0] in (403, 200)   # client may normalise
        assert req(srv, "/%2e%2e/etc/passwd")[0] == 403
        assert req(srv, "/index.html", "POST")[0] == 405
        log = srv.requests
        assert log[0].method == "GET" and log[0].status == 200 and log[0].bytes == 1024
        assert log[1].method == "HEAD" and log[1].bytes == 0
        srv.clear_requests()
        assert srv.requests == []


def test_request_recorded_before_response_headers(root):
    """A client that stops at the headers (HEAD, 401, 404) must find its request logged."""
    with DevServer(root, auth="u:p") as srv:
        for i in range(40):
            conn = http.client.HTTPConnection(srv.host, srv.port, timeout=10)
            try:
                method, hdrs, path = [("HEAD", basic("u:p"), "/data/cols.0123abcd.bin"),
                                      ("GET", {}, "/index.html"),
                                      ("GET", basic("u:p"), "/missing.json"),
                                      ("GET", basic("u:p"), "/sub/")][i % 4]
                conn.request(method, path, headers=hdrs)
                r = conn.getresponse()
                n = len(srv.requests)
                r.read()
            finally:
                conn.close()
            assert n == i + 1, (i, method, path)
        st = [r.status for r in srv.requests[:4]]
        assert st == [200, 401, 404, 200]


def _wait_finished(srv: DevServer, k: int, timeout: float = 2.0):
    """Record ``k`` once its ``t`` is set (the server thread stamps it right after the write,
    which can be a moment after the client has read the body)."""
    end = time.time() + timeout
    while time.time() < end:
        log = srv.requests
        if len(log) > k and log[k].t is not None:
            return log[k]
        time.sleep(0.002)
    return srv.requests[k]


def test_request_finish_time(root):
    """``RequestRecord.t``: epoch ms when the response finished, inside the request window and
    in completion order, for files, ranges, HEAD, listings and plain error bodies."""
    with DevServer(root, latency_ms=20) as srv:
        cases = [("GET", "/data/cols.0123abcd.bin", {}),
                 ("GET", "/data/cols.0123abcd.bin", {"Range": "bytes=10-19"}),
                 ("HEAD", "/data/manifest.json", {}),
                 ("GET", "/sub/", {}),
                 ("GET", "/missing.json", {})]
        windows = []
        for k, (method, path, hdrs) in enumerate(cases):
            t0 = time.time() * 1000.0
            req(srv, path, method, hdrs)
            rec = _wait_finished(srv, k)
            windows.append((t0, time.time() * 1000.0))
            assert rec.t is not None, rec
        log = srv.requests
        assert len(log) == len(cases)
        for rec, (t0, t1) in zip(log, windows):
            # the 20 ms latency is spent before the record exists, so t is at least that late
            assert t0 + 15 <= rec.t <= t1, (rec, t0, t1)
        ts = [r.t for r in log]
        assert ts == sorted(ts)


def test_request_finish_time_unset_until_written(root):
    """A new record has no ``t``; the server stamps it after the body is written."""
    with DevServer(root) as srv:
        conn = http.client.HTTPConnection(srv.host, srv.port, timeout=10)
        try:
            conn.request("GET", "/data/cols.0123abcd.bin")
            r = conn.getresponse()
            r.read()
        finally:
            conn.close()
        rec = _wait_finished(srv, 0)
        assert rec.t is not None and rec.bytes == 1024
    from portalkit.deploy.devserver import RequestRecord
    assert RequestRecord("GET", "/x", 200).t is None


def test_ranges(root):
    with DevServer(root) as srv:
        st, h, body = req(srv, "/data/cols.0123abcd.bin", headers={"Range": "bytes=10-19"})
        assert st == 206 and body == BLOB[10:20]
        assert h["content-range"] == "bytes 10-19/1024"
        st, h, body = req(srv, "/data/cols.0123abcd.bin", headers={"Range": "bytes=-16"})
        assert st == 206 and body == BLOB[-16:]
        st, h, body = req(srv, "/data/cols.0123abcd.bin", headers={"Range": "bytes=1000-"})
        assert st == 206 and body == BLOB[1000:]
        st, h, body = req(srv, "/data/cols.0123abcd.bin", headers={"Range": "bytes=1000-5000"})
        assert st == 206 and h["content-range"] == "bytes 1000-1023/1024"
        st, h, _ = req(srv, "/data/cols.0123abcd.bin", headers={"Range": "bytes=2000-"})
        assert st == 416 and h["content-range"] == "bytes */1024"
        st, _, body = req(srv, "/data/cols.0123abcd.bin", headers={"Range": "bytes=0-1,5-6"})
        assert st == 200 and body == BLOB            # multi-range ignored
        assert srv.requests[0].range == "bytes=10-19"


def test_range_off(root):
    with DevServer(root, range=False) as srv:
        st, h, body = req(srv, "/data/cols.0123abcd.bin", headers={"Range": "bytes=10-19"})
        assert st == 200 and body == BLOB and h["accept-ranges"] == "none"


def test_basic_auth(root):
    with DevServer(root, auth="user:pw") as srv:
        st, h, _ = req(srv, "/index.html")
        assert st == 401 and h["www-authenticate"].startswith("Basic")
        assert req(srv, "/index.html", headers=basic("user:wrong"))[0] == 401
        assert req(srv, "/index.html", headers={"Authorization": "Basic !!!"})[0] == 401
        st, _, body = req(srv, "/index.html", headers=basic("user:pw"))
        assert st == 200 and b"index" in body
        assert [r.auth for r in srv.requests] == [False, True, True, True]


def test_encoding_auto(root):
    with DevServer(root) as srv:
        st, h, body = req(srv, "/data/big.json.gz")
        assert h["content-encoding"] == "gzip"
        assert h["content-type"].startswith("application/json")
        assert gzip.decompress(body) == b'{"gz": true}'
        _, h, body = req(srv, "/data/manifest.json", headers={"Accept-Encoding": "gzip"})
        assert "content-encoding" not in h and json.loads(body) == {"n": 1}


def test_encoding_none(root):
    with DevServer(root, content_encoding="none") as srv:
        _, h, body = req(srv, "/data/big.json.gz", headers={"Accept-Encoding": "gzip"})
        assert "content-encoding" not in h and h["content-type"] == "application/gzip"
        assert body.startswith(b"\x1f\x8b")


def test_encoding_gzip_everything(root):
    with DevServer(root, content_encoding="gzip") as srv:
        _, h, body = req(srv, "/data/cols.0123abcd.bin",
                         headers={"Accept-Encoding": "gzip", "Range": "bytes=0-9"})
        assert h["content-encoding"] == "gzip" and h["vary"] == "Accept-Encoding"
        assert h["accept-ranges"] == "none"
        assert gzip.decompress(body) == BLOB               # Range ignored when compressed
        st, h, body = req(srv, "/data/cols.0123abcd.bin", headers={"Range": "bytes=0-9"})
        assert st == 206 and "content-encoding" not in h and body == BLOB[:10]
        assert srv.requests[0].encoding == "gzip"


def test_latency_and_cache_control(root):
    with DevServer(root, latency_ms=150, cache_control="htaccess") as srv:
        t0 = time.monotonic()
        _, h, _ = req(srv, "/data/cols.0123abcd.bin")
        assert time.monotonic() - t0 >= 0.14
        assert h["cache-control"] == "public, max-age=31536000, immutable"
        _, h, _ = req(srv, "/data/manifest.json")
        assert h["cache-control"] == "no-cache"
    with DevServer(root, cache_control="max-age=60") as srv:
        assert req(srv, "/sub/a.txt")[1]["cache-control"] == "max-age=60"


def test_bad_options(root, tmp_path):
    with pytest.raises(ValueError):
        DevServer(root, content_encoding="br")
    with pytest.raises(ValueError):
        DevServer(root, auth="nocolon")
    with pytest.raises(ValueError):
        DevServer(root, latency_ms=-1)
    with pytest.raises(FileNotFoundError):
        DevServer(tmp_path / "absent")


@pytest.mark.parametrize("name,ctype", [
    ("a.bin", "application/octet-stream"),
    ("a.json", "application/json; charset=utf-8"),
    ("a.json.gz", "application/json; charset=utf-8"),
    ("a.js", "text/javascript; charset=utf-8"),
    ("a.wasm", "application/wasm"),
    ("a.png", "image/png"),
    ("a.svg", "image/svg+xml; charset=utf-8"),
])
def test_guess_type(name, ctype):
    assert guess_type(name) == ctype


def test_deploy_bookkeeping_paths_answer_403(root):
    """WPE4: paths the portalkit .htaccess denies (htaccess.denied) answer 403, with or
    without auth, and are left out of listings; other dot-free names are served."""
    (root / ".portalkit-deploy.json").write_text('{"target": "/abs"}')
    (root / ".portalkit-deploy.c.json").write_text("{}")
    bak = root / "sub" / ".portalkit_backup" / "20261010T120000Z-0123abcd"
    bak.mkdir(parents=True)
    (bak / "backup.json").write_text("{}")
    (root / "data" / "manifest.json.pk-tmp").write_text("{}")
    (root / "portalkit-res").mkdir()
    (root / "portalkit-res" / "x.portalkit-deploy.json").write_text("{}")
    with DevServer(root) as srv:
        for path in ("/.portalkit-deploy.json", "/.portalkit-deploy.c.json",
                     "/%2Eportalkit-deploy.json", "/sub/.portalkit_backup/",
                     "/sub/.portalkit_backup", "/sub/.portalkit_backup/20261010T120000Z-0123abcd/backup.json",
                     "/data/manifest.json.pk-tmp", "/.portalkit-deploy.json?x=1"):
            for method in ("GET", "HEAD"):
                st, _, body = req(srv, path, method)
                assert st == 403, (path, method)
                assert b"target" not in body
        st, _, body = req(srv, "/portalkit-res/x.portalkit-deploy.json")
        assert st == 200 and body == b"{}"
        st, _, body = req(srv, "/sub/")
        assert st == 200 and b"a.txt" in body and b".portalkit" not in body
        st, _, body = req(srv, "/data/")
        assert st == 200 and b"manifest.json" in body and b"pk-tmp" not in body
        assert [r.status for r in srv.requests if r.path.startswith("/.portalkit")][:1] == [403]
    with DevServer(root, auth="u:p") as srv:
        assert req(srv, "/.portalkit-deploy.json")[0] == 401        # auth comes first
        assert req(srv, "/.portalkit-deploy.json", headers=basic("u:p"))[0] == 403
        assert req(srv, "/data/manifest.json", headers=basic("u:p"))[0] == 200


# ---------------------------------------------------------------------------
# mounts (WPE9#6): sibling portals served at a deploy-relative URL path
# ---------------------------------------------------------------------------

@pytest.fixture()
def foreign_root(tmp_path: Path) -> Path:
    d = tmp_path / "foreign"
    (d / "data").mkdir(parents=True)
    (d / "data" / "manifest.json").write_text('{"n": 42}')
    (d / "index.html").write_text("<!doctype html><p>foreign index</p>")
    (d / ".htaccess").write_text("secret")
    (d / "sub").mkdir()
    (d / "sub" / "b.txt").write_text("b")
    return d


def test_mount_serves_sibling_directory_at_its_prefix(root, foreign_root):
    with DevServer(root, mounts={"/jwst/spectra/portal_unified/": foreign_root}) as srv:
        st, h, body = req(srv, "/jwst/spectra/portal_unified/data/manifest.json")
        assert st == 200 and body == b'{"n": 42}'
        assert h["content-type"] == "application/json; charset=utf-8"
        # root is unaffected
        st, _, body = req(srv, "/data/manifest.json")
        assert st == 200 and body == b'{"n": 1}'
        # a path that only exists under the mount is 404 outside it
        assert req(srv, "/data/manifest.json", "GET")[0] == 200
        assert req(srv, "/jwst/spectra/manifest.json")[0] == 404


def test_mount_index_html_and_listing(root, foreign_root):
    with DevServer(root, mounts={"/mnt/": foreign_root}) as srv:
        st, _, body = req(srv, "/mnt/")
        assert st == 200 and b"foreign index" in body
        st, h, _ = req(srv, "/mnt")                       # no trailing slash: redirected
        assert st == 301 and h["location"] == "/mnt/"
        st, _, body = req(srv, "/mnt/sub/")
        assert st == 200 and b"b.txt" in body              # no index.html here: a listing


def test_mount_denied_paths_403_inside_mount(root, foreign_root):
    """The .ht*/.portalkit* 403 rules are checked before a mount is picked, so they apply
    inside a mount exactly as they do under root."""
    (foreign_root / ".portalkit-deploy.json").write_text('{"target": "/abs"}')
    with DevServer(root, mounts={"/mnt/": foreign_root}) as srv:
        assert req(srv, "/mnt/.htaccess")[0] == 403
        assert req(srv, "/mnt/.portalkit-deploy.json")[0] == 403
        assert req(srv, "/mnt/../../etc/passwd")[0] == 403
        assert req(srv, "/mnt/sub/b.txt")[0] == 200


def test_symlink_escape_follow_switch(root, foreign_root, tmp_path_factory):
    """Symlinks resolving outside root (or the mount) are followed by default, as Apache does;
    follow_symlinks=False (the `portalkit serve` default) answers 403 for them while links that
    stay inside the served tree still work."""
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.txt").write_text("SECRET")
    (root / "escape").symlink_to(outside)
    (root / "data" / "inner").symlink_to(root / "data" / "manifest.json")
    (foreign_root / "escape").symlink_to(outside)
    with DevServer(root, mounts={"/mnt/": foreign_root}) as srv:
        assert req(srv, "/escape/secret.txt")[:3:2] == (200, b"SECRET")
        assert req(srv, "/mnt/escape/secret.txt")[0] == 200
    with DevServer(root, mounts={"/mnt/": foreign_root}, follow_symlinks=False) as srv:
        assert req(srv, "/escape/secret.txt")[0] == 403
        assert req(srv, "/escape/")[0] == 403
        assert req(srv, "/mnt/escape/secret.txt")[0] == 403
        assert req(srv, "/data/inner")[:3:2] == (200, b'{"n": 1}')
        assert req(srv, "/mnt/sub/b.txt")[0] == 200


def test_mount_longest_prefix_wins(root, foreign_root, tmp_path):
    outer = tmp_path / "outer"
    outer.mkdir()
    (outer / "manifest.json").write_text('{"which": "outer"}')
    with DevServer(root, mounts={"/a/": outer, "/a/b/": foreign_root}) as srv:
        st, _, body = req(srv, "/a/manifest.json")
        assert st == 200 and body == b'{"which": "outer"}'
        st, _, body = req(srv, "/a/b/data/manifest.json")
        assert st == 200 and body == b'{"n": 42}'          # the more specific mount, not outer


def test_mount_bad_options(root, foreign_root, tmp_path):
    with pytest.raises(ValueError):
        DevServer(root, mounts={"/": foreign_root})          # no path segment to match
    with pytest.raises(FileNotFoundError):
        DevServer(root, mounts={"/mnt/": tmp_path / "absent"})


def test_mount_via_devserver_fixture(devserver, root, foreign_root):
    """tests/conftest.py's ``devserver`` factory fixture starts the server and stops it at
    teardown; this exercises it directly rather than through DevServer's context manager."""
    srv = devserver(root, mounts={"/mnt/": foreign_root})
    st, _, body = req(srv, "/mnt/data/manifest.json")
    assert st == 200 and body == b'{"n": 42}'


def test_main_mount_cli_option(root, foreign_root, capsys):
    from portalkit.deploy.devserver import main

    class _Stop(Exception):
        pass

    import portalkit.deploy.devserver as dsmod
    orig = dsmod.DevServer.serve_forever

    def fake_serve_forever(self):
        raise _Stop

    dsmod.DevServer.serve_forever = fake_serve_forever
    try:
        with pytest.raises(_Stop):
            main([str(root), "--port", "0", "--mount", f"/mnt/={foreign_root}"])
    finally:
        dsmod.DevServer.serve_forever = orig
    out = capsys.readouterr().out
    assert "mount /mnt/" in out and str(foreign_root) in out
    with pytest.raises(SystemExit):
        main([str(root), "--mount", "no-equals-sign"])
