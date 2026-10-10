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
