"""Local HTTP server for ``portalkit serve`` and every e2e test (ARCHITECTURE.md sections 10,
11.1 and 13).

It emulates the behaviours of the production Apache host that the viewer must survive:

* HTTP basic auth (``auth="user:pass"``): 401 with ``WWW-Authenticate`` without credentials;
* single-range ``Range`` requests (206 / 416), or Range ignored with ``range=False``;
* ``content_encoding``:
    - ``"auto"``: ``*.gz`` files are sent unchanged with ``Content-Encoding: gzip`` and the
      inner type (``x.json.gz`` -> ``application/json``), like a misconfigured Apache;
    - ``"none"``: never a ``Content-Encoding`` header;
    - ``"gzip"``: every response body is gzip-compressed on the fly when the client sends
      ``Accept-Encoding: gzip`` (like ``mod_deflate`` applied to everything, ``.bin``
      included); ``*.gz`` files behave as in ``auto``; Range is ignored for compressed
      responses, as ``mod_deflate`` does;
* ``latency_ms``: a fixed delay before every response;
* optional ``cache_control`` (a header value, or ``"htaccess"`` to apply the section 11.5
  rules) so tests can exercise ``verify``'s header report.

Paths the portalkit ``.htaccess`` denies (:func:`portalkit.deploy.htaccess.denied`: any
segment starting with ``.portalkit``, and ``*.pk-tmp``) answer 403 and are left out of
directory listings, like ``.ht*`` paths, whether or not the served tree holds that file.

``mounts`` (``DevServer(..., mounts={"/jwst/spectra/portal_unified/": staged_dir})``, CLI
``--mount PREFIX=DIR``, repeatable) serves a sibling portal's staged directory at a
deploy-relative URL path, under the same host/port as ``root``, so a foreign base that climbs
above the test's served root (e.g. ``'../../../../jwst/spectra/portal_unified/data/'``)
resolves without per-test routing. The longest matching mount prefix wins; everything else is
served from ``root`` as before. The ``..``/``.ht*``/:func:`~portalkit.deploy.htaccess.denied`
403 rules are checked against the request path before a mount is even considered, so they
apply inside a mount exactly as they do under ``root``.


Every request is recorded in ``DevServer.requests`` (method, path, status, bytes, range,
encoding, auth, and ``t``: the epoch-ms time the response finished, IMPLEMENTATION_NOTES 112).
"""

from __future__ import annotations

import argparse
import base64
import email.utils
import gzip
import hmac
import html
import mimetypes
import os
import posixpath
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, unquote, urlsplit

from .htaccess import denied

ENCODING_MODES = ("auto", "none", "gzip")

_EXTRA_TYPES = {
    ".bin": "application/octet-stream",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".json": "application/json",
    ".ecsv": "text/plain",
    ".csv": "text/csv",
    ".wasm": "application/wasm",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".fits": "application/fits",
    ".md": "text/markdown",
}
_TEXT_TYPES = ("text/", "application/json", "application/javascript", "image/svg+xml")
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


@dataclass
class RequestRecord:
    """One logged request. The record is appended before the status line is sent; ``t`` is
    filled in (epoch milliseconds, ``time.time() * 1000``) once the last byte of the response
    (the headers for HEAD) has been handed to the socket, and stays ``None`` until then."""

    method: str
    path: str
    status: int
    bytes: int = 0
    range: str | None = None
    encoding: str | None = None
    auth: bool = False
    t: float | None = None


@dataclass
class ServerOptions:
    root: Path
    auth: str | None = None
    range: bool = True
    content_encoding: str = "auto"
    latency_ms: float = 0.0
    cache_control: str | None = None
    verbose: bool = False
    requests: list[RequestRecord] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    #: (prefix path segments, directory) pairs, longest prefix first (see _normalize_mounts)
    mounts: list[tuple[list[str], Path]] = field(default_factory=list)
    #: follow symlinks that resolve outside root (or the mount dir), as Apache's FollowSymLinks
    #: does; False answers 403 for them (``portalkit serve`` default, see :func:`serve`)
    follow_symlinks: bool = True


def _contained(fs_path: str, base: Path) -> bool:
    """True when ``fs_path`` with every symlink resolved stays inside ``base`` (resolved)."""
    real = Path(os.path.realpath(fs_path))
    return real == base or base in real.parents


def _normalize_mounts(mounts: Mapping[str, os.PathLike | str] | None) -> list[tuple[list[str], Path]]:
    """``{"/a/b/": dir, ...}`` -> ``[(["a", "b"], dir.resolve()), ...]``, longest prefix
    first so the first scan match in :meth:`DevRequestHandler._mount_for` is the longest."""
    out: list[tuple[list[str], Path]] = []
    for prefix, d in (mounts or {}).items():
        parts = [p for p in str(prefix).split("/") if p]
        if not parts:
            raise ValueError(f"mount prefix must name at least one path segment: {prefix!r}")
        dpath = Path(d)
        if not dpath.is_dir():
            raise FileNotFoundError(f"no such directory for mount {prefix!r}: {d}")
        out.append((parts, dpath.resolve()))
    out.sort(key=lambda t: -len(t[0]))
    return out


def guess_type(path: str) -> str:
    """Content type for ``path`` (``.gz`` reports the inner type, as Apache does)."""
    p = path[:-3] if path.endswith(".gz") else path
    ext = posixpath.splitext(p)[1].lower()
    if ext in _EXTRA_TYPES:
        ctype = _EXTRA_TYPES[ext]
    else:
        ctype = mimetypes.guess_type(p)[0] or "application/octet-stream"
    if ctype.startswith(_TEXT_TYPES) and "charset" not in ctype:
        ctype += "; charset=utf-8"
    return ctype


class DevRequestHandler(SimpleHTTPRequestHandler):
    """Static file handler with auth / Range / Content-Encoding / latency emulation."""

    protocol_version = "HTTP/1.1"
    server_version = "portalkit-devserver"
    opts: ServerOptions

    def __init__(self, *args: Any, opts: ServerOptions, **kw: Any) -> None:
        self.opts = opts
        super().__init__(*args, directory=str(opts.root), **kw)

    # -- logging ------------------------------------------------------------------------
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 (stdlib signature)
        if self.opts.verbose:
            sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

    def _record(self, status: int, nbytes: int = 0, rng: str | None = None,
                enc: str | None = None) -> RequestRecord:
        rec = RequestRecord(
            method=self.command, path=self.path, status=status, bytes=nbytes, range=rng,
            encoding=enc, auth=bool(self.headers.get("Authorization")))
        with self.opts.lock:
            self.opts.requests.append(rec)
        return rec

    @staticmethod
    def _finished(rec: RequestRecord) -> None:
        """Stamp ``rec.t``: the response has been written (IMPLEMENTATION_NOTES 112)."""
        rec.t = time.time() * 1000.0

    # -- entry points -------------------------------------------------------------------
    def do_GET(self) -> None:
        self._handle(head=False)

    def do_HEAD(self) -> None:
        self._handle(head=True)

    def do_POST(self) -> None:
        self._simple(HTTPStatus.METHOD_NOT_ALLOWED, b"method not allowed\n",
                     extra={"Allow": "GET, HEAD"})

    do_PUT = do_DELETE = do_POST

    # -- helpers ------------------------------------------------------------------------
    def _simple(self, status: int, body: bytes, extra: dict | None = None,
                head: bool = False) -> None:
        # record before any byte (status line included) reaches the client, so a client that
        # stops at the headers (HEAD, or an HTTPError) always finds its request in the log
        rec = self._record(int(status), 0 if head else len(body))
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head:
            self._write(body)
        self._finished(rec)

    def _write(self, data: bytes) -> None:
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _authorized(self) -> bool:
        if not self.opts.auth:
            return True
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Basic "):
            return False
        try:
            given = base64.b64decode(hdr[6:].strip(), validate=True)
        except ValueError:          # binascii.Error is a ValueError
            return False
        return hmac.compare_digest(given, self.opts.auth.encode("utf-8"))

    def _cache_header(self, path: str) -> str | None:
        cc = self.opts.cache_control
        if cc == "htaccess":
            from .htaccess import expected_cache_control
            return expected_cache_control(path)
        return cc

    def _mount_for(self, parts: list[str]) -> tuple[Path, list[str]] | None:
        """``(mount_dir, remaining_parts)`` for the longest mount prefix matching ``parts``,
        or ``None`` when no mount applies and ``root`` should serve the request."""
        for prefix, mdir in self.opts.mounts:
            n = len(prefix)
            if parts[:n] == prefix:
                return mdir, parts[n:]
        return None

    def _handle(self, head: bool) -> None:
        if self.opts.latency_ms > 0:
            time.sleep(self.opts.latency_ms / 1000.0)
        if not self._authorized():
            self._simple(HTTPStatus.UNAUTHORIZED, b"401 authorization required\n",
                         extra={"WWW-Authenticate": 'Basic realm="portalkit"'}, head=head)
            return
        url_path = unquote(urlsplit(self.path).path)
        parts = [p for p in url_path.split("/") if p]
        if any(p == ".." for p in parts):
            self._simple(HTTPStatus.FORBIDDEN, b"403 forbidden\n", head=head)
            return
        if any(p.startswith(".ht") for p in parts) or denied(url_path):
            # Apache never serves .htaccess / .htpasswd, and the portalkit .htaccess denies
            # deploy's bookkeeping (.portalkit* manifests and backups, *.pk-tmp)
            self._simple(HTTPStatus.FORBIDDEN, b"403 forbidden\n", head=head)
            return
        mount = self._mount_for(parts)
        if mount is not None:
            mdir, rest = mount
            fs_path = str(mdir.joinpath(*rest)) if rest else str(mdir)
        else:
            mdir = self.opts.root
            fs_path = self.translate_path(self.path)
        if not self.opts.follow_symlinks and not _contained(fs_path, mdir):
            self._simple(HTTPStatus.FORBIDDEN, b"403 forbidden\n", head=head)
            return
        if os.path.isdir(fs_path):
            if not url_path.endswith("/"):
                loc = urlsplit(self.path)._replace(path=urlsplit(self.path).path + "/").geturl()
                self._simple(HTTPStatus.MOVED_PERMANENTLY, b"moved\n", extra={"Location": loc},
                             head=head)
                return
            index = os.path.join(fs_path, "index.html")
            if os.path.isfile(index):
                fs_path = index
            else:
                self._listing(fs_path, head)
                return
        if not os.path.isfile(fs_path):
            self._simple(HTTPStatus.NOT_FOUND, b"404 not found\n", head=head)
            return
        self._file(fs_path, head)

    def _listing(self, fs_path: str, head: bool) -> None:
        try:
            names = sorted(os.listdir(fs_path), key=str.lower)
        except OSError:
            self._simple(HTTPStatus.NOT_FOUND, b"404 not found\n", head=head)
            return
        title = html.escape(unquote(urlsplit(self.path).path), quote=False)
        rows = []
        for name in names:
            if name.startswith(".ht") or denied(name):
                continue
            full = os.path.join(fs_path, name)
            disp = name + ("/" if os.path.isdir(full) else "")
            rows.append(f'<li><a href="{quote(disp)}">{html.escape(disp, quote=False)}</a></li>')
        data = (f"<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\"><title>Index of {title}"
                f"</title></head><body><h1>Index of {title}</h1><ul>\n" + "\n".join(rows)
                + "\n</ul></body></html>\n").encode("utf-8")
        rec = self._record(200, 0 if head else len(data))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head:
            self._write(data)
        self._finished(rec)

    def _file(self, fs_path: str, head: bool) -> None:
        mode = self.opts.content_encoding
        ctype = guess_type(fs_path)
        if fs_path.endswith(".gz") and mode == "none":
            ctype = "application/gzip"
        try:
            st = os.stat(fs_path)
            with open(fs_path, "rb") as fh:
                data = fh.read()
        except OSError:
            self._simple(HTTPStatus.NOT_FOUND, b"404 not found\n", head=head)
            return
        headers: dict[str, str] = {
            "Content-Type": ctype,
            "Last-Modified": email.utils.formatdate(st.st_mtime, usegmt=True),
        }
        cc = self._cache_header(fs_path)
        if cc:
            headers["Cache-Control"] = cc
        encoding = None
        if fs_path.endswith(".gz") and mode in ("auto", "gzip"):
            encoding = "gzip"
        elif mode == "gzip" and "gzip" in self.headers.get("Accept-Encoding", ""):
            data = gzip.compress(data, compresslevel=6, mtime=0)
            encoding = "gzip"
            headers["Vary"] = "Accept-Encoding"
        if encoding:
            headers["Content-Encoding"] = encoding
        status = HTTPStatus.OK
        rng_hdr = self.headers.get("Range")
        range_ok = self.opts.range and not (mode == "gzip" and "Vary" in headers)
        headers["Accept-Ranges"] = "bytes" if range_ok else "none"
        body = data
        if rng_hdr and range_ok:
            parsed = self._parse_range(rng_hdr, len(data))
            if parsed == "unsatisfiable":
                self._simple(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, b"416\n",
                             extra={"Content-Range": f"bytes */{len(data)}"}, head=head)
                return
            if parsed is not None:
                start, end = parsed
                body = data[start:end + 1]
                status = HTTPStatus.PARTIAL_CONTENT
                headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
        # record before the status line goes out (see _simple)
        rec = self._record(int(status), 0 if head else len(body),
                           rng_hdr if status == HTTPStatus.PARTIAL_CONTENT else None, encoding)
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self._write(body)
        self._finished(rec)

    @staticmethod
    def _parse_range(value: str, size: int) -> tuple[int, int] | str | None:
        """``(start, end)`` inclusive, ``"unsatisfiable"``, or ``None`` to ignore the header
        (syntax errors and multi-range requests get the full body, as RFC 9110 allows)."""
        m = _RANGE_RE.match(value.strip())
        if not m:
            return None
        a, b = m.group(1), m.group(2)
        if not a and not b:
            return None
        if not a:                      # suffix range: last N bytes
            n = int(b)
            if n == 0:
                return "unsatisfiable"
            return (max(0, size - n), size - 1) if size else "unsatisfiable"
        start = int(a)
        end = int(b) if b else size - 1
        if b and end < start:
            return None
        if start >= size:
            return "unsatisfiable"
        return start, min(end, size - 1)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class DevServer:
    """Threaded static server; usable as a context manager::

        with DevServer(build_dir, auth="u:p", range=False) as srv:
            page.goto(srv.url + "index.html?pktest=1")
    """

    def __init__(self, root: os.PathLike | str, host: str = "127.0.0.1", port: int = 0, *,
                 auth: str | None = None, range: bool = True,  # noqa: A002
                 content_encoding: str = "auto", latency_ms: float = 0.0,
                 cache_control: str | None = None, verbose: bool = False,
                 mounts: Mapping[str, os.PathLike | str] | None = None,
                 follow_symlinks: bool = True) -> None:
        if content_encoding not in ENCODING_MODES:
            raise ValueError(f"content_encoding must be one of {ENCODING_MODES}")
        if auth is not None and ":" not in auth:
            raise ValueError("auth must look like USER:PASS")
        if latency_ms < 0:
            raise ValueError("latency_ms must be >= 0")
        root = Path(root)
        if not root.is_dir():
            raise FileNotFoundError(f"no such directory: {root}")
        self.options = ServerOptions(root=root.resolve(), auth=auth, range=range,
                                     content_encoding=content_encoding, latency_ms=latency_ms,
                                     cache_control=cache_control, verbose=verbose,
                                     mounts=_normalize_mounts(mounts),
                                     follow_symlinks=follow_symlinks)
        handler = partial(DevRequestHandler, opts=self.options)
        self.httpd = _Server((host, port), handler)
        self.host = host
        self.port = self.httpd.server_address[1]
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        """Base URL, ending in ``/``."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}/"

    @property
    def requests(self) -> list[RequestRecord]:
        return self.options.requests

    def clear_requests(self) -> None:
        with self.options.lock:
            self.options.requests.clear()

    def start(self) -> "DevServer":
        if self._thread is None:
            self._thread = threading.Thread(target=self.httpd.serve_forever,
                                            kwargs={"poll_interval": 0.05},
                                            name=f"pk-devserver-{self.port}", daemon=True)
            self._thread.start()
        return self

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def stop(self) -> None:
        if self._thread is not None:
            self.httpd.shutdown()
            self._thread.join(timeout=5)
            self._thread = None
        self.httpd.server_close()

    def __enter__(self) -> "DevServer":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()


def serve(root: os.PathLike | str, port: int = 8765, host: str = "127.0.0.1", *,
          auth: str | None = None, content_encoding: str = "auto", no_range: bool = False,
          latency_ms: float = 0.0, cache_control: str | None = None,
          verbose: bool = True, mounts: Mapping[str, os.PathLike | str] | None = None,
          follow_symlinks: bool = False) -> None:
    """Blocking server for ``portalkit serve BUILD_DIR`` (Ctrl-C stops it). Unlike the
    :class:`DevServer` default, symlinks that resolve outside the served root or mount answer
    403 unless ``follow_symlinks`` is set, so pointing it at a tree that links into other
    (possibly secure) data does not serve that data."""
    srv = DevServer(root, host, port, auth=auth, range=not no_range,
                    content_encoding=content_encoding, latency_ms=latency_ms,
                    cache_control=cache_control, verbose=verbose, mounts=mounts,
                    follow_symlinks=follow_symlinks)
    mount_note = "".join(f", mount /{'/'.join(p)}/ -> {d}" for p, d in srv.options.mounts)
    print(f"serving {srv.options.root} at {srv.url} "
          f"(range={'off' if no_range else 'on'}, content-encoding={content_encoding}"
          f"{', auth' if auth else ''}{f', latency {latency_ms} ms' if latency_ms else ''}"
          f"{mount_note})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("stopped", flush=True)
    finally:
        srv.httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m portalkit.deploy.devserver",
                                 description="portalkit development server")
    ap.add_argument("root")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--auth", default=None, metavar="USER:PASS")
    ap.add_argument("--content-encoding", choices=ENCODING_MODES, default="auto")
    ap.add_argument("--no-range", action="store_true")
    ap.add_argument("--latency-ms", type=float, default=0.0)
    ap.add_argument("--cache-control", default=None)
    ap.add_argument("--mount", action="append", default=[], metavar="PREFIX=DIR",
                    help="serve DIR at the deploy-relative URL path PREFIX; repeatable")
    args = ap.parse_args(argv)
    mounts: dict[str, str] = {}
    for m in args.mount:
        if "=" not in m:
            ap.error(f"--mount must look like PREFIX=DIR, got {m!r}")
        prefix, d = m.split("=", 1)
        mounts[prefix] = d
    serve(args.root, args.port, args.host, auth=args.auth,
          content_encoding=args.content_encoding, no_range=args.no_range,
          latency_ms=args.latency_ms, cache_control=args.cache_control, mounts=mounts or None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
