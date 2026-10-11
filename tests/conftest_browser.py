"""Browser fixtures shared by the e2e and perf tests (ARCHITECTURE.md section 13, Levels 1-3;
IMPLEMENTATION_NOTES item 94).

This module is a pytest plugin. ``tests/e2e/conftest.py`` and ``tests/perf/conftest.py`` both
call :func:`register`, which loads this file once per process (module name
:data:`PLUGIN_NAME`) and registers it with the plugin manager. A registered non-conftest plugin
defines its fixtures once for the whole session, so ``pytest -m "e2e or perf"`` shares one
sync Playwright, one Chromium, one DevServer and one fixture root.

Fixtures:

* ``synth_root``  -- fixture root: ``$PK_SYNTH_DIR`` when set, else a pytest temp dir.
* ``synth(n)``    -- builds (or reuses) the synthetic portal for ``n`` rows
  (``tests/fixtures/make_synthetic.py``) and returns its ``SyntheticPortal``.
* ``server``      -- one ``DevServer`` over ``synth_root`` (port 0).
* ``browser``     -- headless Chromium with SwiftShader WebGL (:data:`CHROMIUM_ARGS`); the
  tests skip when Playwright or its Chromium build is missing.
* ``open_page``   -- opens a page with ``?pktest=1``, routes ``**/aladin*.js`` to
  ``tests/fixtures/aladin_stub.js``, aborts every other external request and records it, and
  returns a :class:`PageRun`.
* ``page_cache``  -- module-scoped: one :class:`PageRun` per page path, reused by the
  capability-matrix checks.
* ``e2e``         -- helper namespace for the test modules (they must not import a conftest).

``PageRun.errors()`` reports ``__pk.errors``, uncaught page errors, browser console errors that
``__pk.errors`` cannot see (failed module loads, 404s, CORS) and local HTTP responses with
status >= 400. Console errors for external URLs are left to the external-origin checks (the
harness aborts those requests on purpose). ``PageRun.error_mark()`` returns a cursor over those
append-only sources and ``errors(since=mark)`` reports only what was logged after it, which the
checks on shared (``page_cache``) pages use. ``PageRun.screenshot()`` takes a screenshot with
:data:`SCREENSHOT_TIMEOUT_MS`. ``PageRun.canvas_hash(pane)`` hashes the
``toDataURL()`` of every canvas of a pane in the page, which replaces screenshot comparisons
(SwiftShader screenshot readback on a 2-CPU host takes tens of seconds per shot).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Callable

import pytest

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures"
if str(FIXTURES) not in sys.path:
    sys.path.insert(0, str(FIXTURES))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import make_synthetic as ms  # noqa: E402
from portalkit import config  # noqa: E402

#: module / plugin name under which this file is loaded and registered
PLUGIN_NAME = "pk_conftest_browser"

PANES_DIR = REPO / "web" / "js" / "panes"
LEGACYHASH = REPO / "web" / "js" / "core" / "legacyhash.js"
ALADIN_STUB_FILE = FIXTURES / "aladin_stub.js"
#: SwiftShader WebGL (section 13). The 2D canvas runs in software as well, so canvas readback
#: (toDataURL, getImageData, screenshots) does not wait on a GPU process round trip.
CHROMIUM_ARGS = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--disable-accelerated-2d-canvas"]
CDN = config.ALADIN_CDN
MIRROR = config.ALADIN_MIRROR
READY_TIMEOUT_MS = int(os.environ.get("PK_READY_TIMEOUT_MS", "120000"))
#: screenshot timeout (ms) for the few tests that still take one (contact sheets, debugging)
SCREENSHOT_TIMEOUT_MS = int(os.environ.get("PK_SCREENSHOT_TIMEOUT_MS", "240000"))


def register(config_: pytest.Config) -> ModuleType:
    """Load this file once per process and register it as the plugin :data:`PLUGIN_NAME`.

    Called from ``pytest_configure`` of ``tests/e2e/conftest.py`` and
    ``tests/perf/conftest.py``; the second call finds the plugin registered and returns it."""
    mod = sys.modules.get(PLUGIN_NAME)
    if mod is None:
        if __name__ == PLUGIN_NAME:
            mod = sys.modules[__name__]
        else:
            spec = importlib.util.spec_from_file_location(PLUGIN_NAME, Path(__file__).resolve())
            mod = importlib.util.module_from_spec(spec)
            sys.modules[PLUGIN_NAME] = mod
            spec.loader.exec_module(mod)
    pm = config_.pluginmanager
    if pm.get_plugin(PLUGIN_NAME) is None:
        pm.register(mod, PLUGIN_NAME)
    return mod


# ---------------------------------------------------------------------------
# missing-module helpers (pane types that a later work package adds)
# ---------------------------------------------------------------------------

def pane_modules_missing(types) -> list[str]:
    return sorted({t for t in types if t and not (PANES_DIR / f"{t}.js").exists()})


#: run tests whose pane / legacy-hash modules are absent (as xfail(strict=False) markers)
#: instead of stopping them with an imperative xfail at that point.
RUN_MISSING = os.environ.get("PK_E2E_RUN_MISSING", "") not in ("", "0")


def _xfail(request: Any, reason: str) -> None:
    if RUN_MISSING:
        request.node.add_marker(pytest.mark.xfail(reason=reason, strict=False))
    else:
        pytest.xfail(reason)


def xfail_unless_panes(request: Any, types) -> list[str]:
    """xfail(strict=False) when any pane module in ``types`` is absent (all of them exist
    since WP08-WP11; the helper stays for pane types added later)."""
    missing = pane_modules_missing(types)
    if missing:
        _xfail(request, f"pane modules missing: {', '.join(missing)}")
    return missing


def xfail_unless_legacyhash(request: Any) -> bool:
    if not LEGACYHASH.exists():
        _xfail(request, "web/js/core/legacyhash.js missing")
        return False
    return True


def page_pane_types(portal: Any, page: str) -> list[str]:
    return [p["pane"] for p in portal.pages[page].panes]


# ---------------------------------------------------------------------------
# session fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def synth_root(tmp_path_factory) -> Path:
    env = os.environ.get("PK_SYNTH_DIR")
    if env:
        p = Path(env).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p
    return tmp_path_factory.mktemp("synth")


@pytest.fixture(scope="session")
def synth(synth_root) -> Callable[[int], Any]:
    cache: dict[int, Any] = {}

    def get(n: int):
        if n not in cache:
            cache[n] = ms.build_portal(synth_root, n)
        return cache[n]
    return get


@pytest.fixture(scope="session")
def server(synth_root):
    from portalkit.deploy.devserver import DevServer

    srv = DevServer(synth_root).start()
    yield srv
    srv.stop()


@pytest.fixture(scope="session")
def _playwright():
    sync_api = pytest.importorskip("playwright.sync_api")
    pw = sync_api.sync_playwright().start()
    yield pw
    pw.stop()


@pytest.fixture(scope="session")
def browser(_playwright):
    from playwright.sync_api import Error as PWError
    try:
        b = _playwright.chromium.launch(args=CHROMIUM_ARGS, headless=True)
    except PWError as err:
        pytest.skip(f"Chromium unavailable for Playwright: {err}")
    yield b
    b.close()


# ---------------------------------------------------------------------------
# page runs
# ---------------------------------------------------------------------------

DISABLE_WEBGL2 = r"""
(() => {
  const orig = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = function (kind, ...rest) {
    if (kind === 'webgl2') return null;
    return orig.call(this, kind, ...rest);
  };
  if (typeof OffscreenCanvas !== 'undefined') {
    const o2 = OffscreenCanvas.prototype.getContext;
    OffscreenCanvas.prototype.getContext = function (kind, ...rest) {
      if (kind === 'webgl2') return null;
      return o2.call(this, kind, ...rest);
    };
  }
})();
"""

DELETE_DECOMPRESSION = "delete window.DecompressionStream; delete globalThis.DecompressionStream;"

LOCAL_URL = re.compile(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?/")

#: console text Chromium prints for a request the harness aborted on purpose (blocked CDN or
#: mirror); the message carries no source URL, so ``errors()`` cannot attribute it. Tests that
#: block an origin pass it in ``allow``.
ABORTED_LOAD = r"^console: Failed to load resource: net::ERR_(FAILED|ABORTED|BLOCKED_BY_CLIENT)$"

#: SHA-256 (hex) over "<w>x<h>:<toDataURL>" of every non-empty canvas in a pane section, in
#: document order. A canvas that cannot be read (tainted) contributes its error text.
CANVAS_HASH_JS = """async (p) => {
  const sec = document.querySelector(`section.pk-pane[data-pane-id='${p}']`);
  if (!sec) return null;
  const parts = [];
  for (const c of sec.querySelectorAll('canvas')) {
    if (!c.width || !c.height) continue;
    let u;
    try { u = c.toDataURL('image/png'); } catch (e) { u = 'ERR:' + (e && e.message); }
    parts.push(c.width + 'x' + c.height + ':' + u);
  }
  const text = parts.join('|');
  if (globalThis.crypto && crypto.subtle) {
    const d = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
    return Array.from(new Uint8Array(d)).map((b) => b.toString(16).padStart(2, '0')).join('');
  }
  let h1 = 0x811c9dc5, h2 = 0x01000193;
  for (let i = 0; i < text.length; i++) {
    h1 = Math.imul(h1 ^ text.charCodeAt(i), 16777619) >>> 0;
    h2 = Math.imul(h2 + text.charCodeAt(i), 2246822519) >>> 0;
  }
  return 'fnv:' + h1.toString(16) + h2.toString(16) + ':' + text.length;
}"""


@dataclass
class PageRun:
    page: Any
    context: Any
    base: str
    console: list = field(default_factory=list)       # (type, text, source url)
    page_errors: list = field(default_factory=list)
    http_errors: list = field(default_factory=list)   # "<status> <url>" of local responses >= 400
    external: list = field(default_factory=list)
    aladin_served: list = field(default_factory=list)
    t_start: float = 0.0

    # -- helpers ---------------------------------------------------------------
    def wait_ready(self, timeout_ms: int = READY_TIMEOUT_MS) -> str:
        """'ok' when ``__pk.ready`` resolved, else the rejection text."""
        self.page.wait_for_function("() => !!window.__pk", timeout=timeout_ms)
        return self.page.evaluate(
            """async (ms) => {
                 const t = new Promise((_, rej) => setTimeout(() => rej(new Error('ready timeout')), ms));
                 try { await Promise.race([window.__pk.ready, t]); return 'ok'; }
                 catch (e) { return 'ERR ' + (e && e.message || e); }
               }""", timeout_ms)

    def error_mark(self) -> tuple[int, int, int, int]:
        """Cursor over the error sources (lengths of ``__pk.errors``, page errors, console
        messages and HTTP errors, all append-only) for ``errors(since=...)``."""
        n_pk = self.page.evaluate("() => (window.__pk && window.__pk.errors) ? window.__pk.errors.length : 0")
        return (int(n_pk), len(self.page_errors), len(self.console), len(self.http_errors))

    def errors(self, allow: tuple[str, ...] = (), since: tuple[int, int, int, int] | None = None) -> list[str]:
        """Every error the page produced (after the :meth:`error_mark` ``since`` when given),
        minus messages matching a regex in ``allow``.

        ``__pk.errors`` (console.error, window error, unhandledrejection), uncaught page
        errors, browser console errors (failed resource or module loads, CORS) for local or
        unknown sources, and local HTTP responses with status >= 400. Console errors whose
        source is an external URL are left out: the harness aborts those requests itself and
        the external-origin assertions cover them.
        """
        k_pk, k_pe, k_con, k_http = since or (0, 0, 0, 0)
        pk_all = [str(e) for e in self.page.evaluate(
            "() => (window.__pk && window.__pk.errors) ? window.__pk.errors.slice() : []")]
        out = pk_all[k_pk:] + [f"pageerror: {e}" for e in self.page_errors[k_pe:]]
        # console.error output also lands in __pk.errors; match against the whole list, since
        # the console event can reach Python after the mark was taken
        seen = {e.strip() for e in pk_all}
        for kind, text, url in self.console[k_con:]:
            if kind != "error":
                continue
            if url and not LOCAL_URL.match(url):
                continue
            if text.strip() in seen or any(text.strip().startswith(e) for e in seen if e):
                continue   # console.error output already in __pk.errors
            out.append(f"console: {text}" + (f" ({url})" if url else ""))
        out += [f"http {e}" for e in self.http_errors[k_http:]]
        if allow:
            rx = [re.compile(a, re.I) for a in allow]
            out = [e for e in out if not any(r.search(e) for r in rx)]
        return out

    def caps_attr(self) -> list[str]:
        s = self.page.evaluate("() => document.body.dataset.caps || ''")
        return sorted(s.split())

    def ev(self, js: str, arg: Any = None) -> Any:
        return self.page.evaluate(js, arg)

    def store_get(self, key: str) -> Any:
        return self.page.evaluate("(k) => { const v = window.__pk.store.get(k); return v === undefined ? null : JSON.parse(JSON.stringify(v)); }", key)

    def wait_panes_settled(self, timeout_ms: int = 60000) -> None:
        """Wait until no visible pane is in the 'loading' state."""
        self.page.wait_for_function(
            """() => Array.from(document.querySelectorAll('section.pk-pane'))
                   .filter((s) => s.offsetParent !== null && s.dataset.state)
                   .every((s) => s.dataset.state !== 'loading')""", timeout=timeout_ms)

    def pane_states(self) -> dict:
        return self.page.evaluate(
            "() => Object.fromEntries(Array.from(document.querySelectorAll('section.pk-pane'))"
            ".map((s) => [s.dataset.paneId, s.dataset.state || null]))")

    def pane_stats(self, pane: str) -> Any:
        """``stats()`` of a mounted pane handle (``None`` when the pane or the method is absent)."""
        return self.page.evaluate(
            """(p) => { const h = window.__pk.app.layout.pane(p);
                        if (!h || typeof h.stats !== 'function') return null;
                        return JSON.parse(JSON.stringify(h.stats())); }""", pane)

    def canvas_hash(self, pane: str) -> str | None:
        """Hash of the pixels of every canvas in ``pane`` (``toDataURL``); None without the pane."""
        return self.page.evaluate(CANVAS_HASH_JS, pane)

    def wait_canvas_stable(self, pane: str, quiet_ms: int = 400, timeout_ms: int = 60000) -> str:
        """Canvas hash of ``pane`` once two reads ``quiet_ms`` apart agree."""
        t0 = time.monotonic()
        last = self.canvas_hash(pane)
        while True:
            self.page.wait_for_timeout(quiet_ms)
            cur = self.canvas_hash(pane)
            if cur == last:
                return cur
            if (time.monotonic() - t0) * 1000 > timeout_ms:
                raise AssertionError(f"canvases of pane {pane} kept changing for {timeout_ms} ms")
            last = cur

    def wait_canvas_change(self, pane: str, before: str, timeout_ms: int = 60000) -> str:
        """Poll the canvas hash of ``pane`` until it differs from ``before``; returns it (or
        ``before`` when nothing changed within ``timeout_ms``)."""
        t0 = time.monotonic()
        while (time.monotonic() - t0) * 1000 <= timeout_ms:
            cur = self.canvas_hash(pane)
            if cur != before:
                return cur
            self.page.wait_for_timeout(250)
        return before

    def stable_hash(self, quiet_ms: int = 600, timeout_ms: int = 20000) -> str:
        """``location.hash`` once it has not changed for ``quiet_ms``."""
        t0 = time.monotonic()
        last = self.ev("() => location.hash")
        t_last = time.monotonic()
        while True:
            self.page.wait_for_timeout(100)
            cur = self.ev("() => location.hash")
            now = time.monotonic()
            if cur != last:
                last, t_last = cur, now
            elif (now - t_last) * 1000 >= quiet_ms:
                return cur
            if (now - t0) * 1000 > timeout_ms:
                raise AssertionError(f"location.hash kept changing for {timeout_ms} ms: {cur}")

    def screenshot(self, **kw: Any) -> bytes:
        """``page.screenshot`` with :data:`SCREENSHOT_TIMEOUT_MS` (SwiftShader readback on a
        loaded 2-CPU host can exceed Playwright's 30 s default)."""
        kw.setdefault("timeout", SCREENSHOT_TIMEOUT_MS)
        return self.page.screenshot(**kw)

    def aladin_calls(self) -> list:
        return self.page.evaluate("() => (window.__aladinCalls || []).slice()")

    def close(self) -> None:
        self.context.close()


def _stub_body() -> str:
    return ALADIN_STUB_FILE.read_text(encoding="utf-8")


def open_run(browser: Any, server: Any, path: str, *, hash: str = "", query: str = "",
             webgl: bool = True, decompression: bool = True, block_cdn: bool = False,
             block_mirror: bool = False, viewport: dict | None = None, wait: bool = True,
             storage_state: Any = None, context: Any = None,
             init_scripts: tuple[str, ...] = ()) -> PageRun:
    """Open ``path`` (relative to the DevServer root) with ``?pktest=1`` in a new context."""
    ctx = context or browser.new_context(viewport=viewport or {"width": 1400, "height": 900},
                                         storage_state=storage_state)
    if not webgl:
        ctx.add_init_script(DISABLE_WEBGL2)
    if not decompression:
        ctx.add_init_script(DELETE_DECOMPRESSION)
    for s in init_scripts:
        ctx.add_init_script(s)
    run = PageRun(page=None, context=ctx, base=server.url)
    stub = _stub_body()

    def route(r):
        url = r.request.url
        if LOCAL_URL.match(url):
            return r.continue_()
        run.external.append(url)
        is_aladin = re.search(r"/aladin[^/]*\.js(\?|$)", url) is not None
        if is_aladin:
            if (url.startswith(CDN) and block_cdn) or (url.startswith(MIRROR) and block_mirror):
                return r.abort()
            if (block_cdn or block_mirror) and not (url.startswith(CDN) or url.startswith(MIRROR)):
                return r.abort()
            run.aladin_served.append(url)
            return r.fulfill(status=200, content_type="application/javascript", body=stub)
        return r.abort()
    ctx.route(re.compile(r"^(?!https?://(127\.0\.0\.1|localhost)[:/])"), route)
    page = ctx.new_page()
    run.page = page

    def on_console(m):
        loc = m.location or {}
        run.console.append((m.type, m.text, loc.get("url") or ""))

    def on_response(r):
        if r.status >= 400 and LOCAL_URL.match(r.url):
            run.http_errors.append(f"{r.status} {r.url}")
    page.on("console", on_console)
    page.on("pageerror", lambda e: run.page_errors.append(str(e)))
    page.on("response", on_response)
    q = "pktest=1" + (f"&{query}" if query else "")
    run.t_start = time.perf_counter()
    page.goto(f"{server.url}{path}?{q}{('#' + hash) if hash else ''}", wait_until="domcontentloaded")
    if wait:
        status = run.wait_ready()
        if status != "ok":
            errs = run.errors()
            run.close()
            raise AssertionError(f"__pk.ready failed for {path}: {status}; errors={errs}")
    return run


def _close_runs(runs: list[PageRun]) -> None:
    from playwright.sync_api import Error as PWError
    for r in runs:
        try:
            r.close()
        except PWError:
            pass   # context already closed by the test


@pytest.fixture
def open_page(browser, server):
    runs: list[PageRun] = []

    def opener(path: str, **kw: Any) -> PageRun:
        run = open_run(browser, server, path, **kw)
        runs.append(run)
        return run

    yield opener
    _close_runs(runs)


@pytest.fixture(scope="module")
def page_cache(browser, server):
    """``get(path)`` -> one shared :class:`PageRun` per path for the whole module (default
    options). Checks that use it must leave the page as they found it."""
    runs: dict[str, PageRun] = {}

    def get(path: str) -> PageRun:
        if path not in runs:
            runs[path] = open_run(browser, server, path)
        return runs[path]

    yield get
    _close_runs(list(runs.values()))


def ext_origins(urls: list[str]) -> set[str]:
    out = set()
    for u in urls:
        m = re.match(r"^(https?://[^/]+)", u)
        out.add(m.group(1) if m else u)
    return out


ALLOWED_EXTERNAL = {re.match(r"^(https?://[^/]+)", CDN).group(1), re.match(r"^(https?://[^/]+)", MIRROR).group(1)}


def read_json(p: Path) -> Any:
    return json.loads(Path(p).read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def e2e():
    """Helper namespace for the e2e and perf modules (test modules must not import a
    conftest: ``tests/conftest.py`` shares the module name)."""
    return SimpleNamespace(
        ms=ms, xfail_unless_panes=xfail_unless_panes, xfail_unless_legacyhash=xfail_unless_legacyhash,
        pane_modules_missing=pane_modules_missing, page_pane_types=page_pane_types,
        ext_origins=ext_origins, ALLOWED_EXTERNAL=ALLOWED_EXTERNAL, CDN=CDN, MIRROR=MIRROR,
        LEGACYHASH=LEGACYHASH, read_json=read_json, ABORTED_LOAD=ABORTED_LOAD,
        CHROMIUM_ARGS=CHROMIUM_ARGS, SCREENSHOT_TIMEOUT_MS=SCREENSHOT_TIMEOUT_MS)
