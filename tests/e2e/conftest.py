"""Shared fixtures for the browser tests (ARCHITECTURE.md section 13, Levels 1-3).

* ``synth_root``  -- fixture root: ``$PK_SYNTH_DIR`` when set, else a pytest temp dir.
* ``synth(n)``    -- builds (or reuses) the synthetic portal for ``n`` rows
  (``tests/fixtures/make_synthetic.py``) and returns its ``SyntheticPortal``.
* ``server``      -- one ``DevServer`` over ``synth_root`` (port 0).
* ``browser``     -- headless Chromium with SwiftShader WebGL
  (``--use-angle=swiftshader --enable-unsafe-swiftshader``); the tests skip when Playwright
  or its Chromium build is missing.
* ``open_page``   -- opens a page with ``?pktest=1``, routes ``**/aladin*.js`` to the Aladin
  stub (``tests/fixtures/aladin_stub.js`` when present, else a built-in recorder), aborts
  every other external request and records it, and returns a :class:`PageRun`.
* ``page_cache``  -- module-scoped: one :class:`PageRun` per page path, reused by the
  capability-matrix checks.

The session resources (Playwright, Chromium, the DevServer and the fixture root) are held in a
process-wide registry (:data:`_SHARED`), so a second load of this module (tests/perf loads it
under a private name, since tests/perf has no conftest of its own) reuses the instances that
the first load started instead of starting a second sync Playwright in the same process.

``PageRun.errors()`` reports ``__pk.errors``, uncaught page errors, browser console errors
that ``__pk.errors`` cannot see (failed module loads, 404s, CORS) and local HTTP responses
with status >= 400. Console errors for external URLs are left to the external-origin checks
(the harness aborts those requests on purpose).

Tests that need pane modules from WP08-WP11 (``web/js/panes/<type>.js``) or the legacy hash
module (WP13) call :func:`xfail_unless_panes` / :func:`xfail_unless_legacyhash`, which mark
them ``xfail(strict=False)`` while the module files are absent.
"""

from __future__ import annotations

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

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures"
if str(FIXTURES) not in sys.path:
    sys.path.insert(0, str(FIXTURES))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import make_synthetic as ms  # noqa: E402
from portalkit import config  # noqa: E402

PANES_DIR = REPO / "web" / "js" / "panes"
LEGACYHASH = REPO / "web" / "js" / "core" / "legacyhash.js"
ALADIN_STUB_FILE = FIXTURES / "aladin_stub.js"
CHROMIUM_ARGS = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
CDN = config.ALADIN_CDN
MIRROR = config.ALADIN_MIRROR
READY_TIMEOUT_MS = int(os.environ.get("PK_READY_TIMEOUT_MS", "120000"))

#: Fallback Aladin stub, used only while WP09's tests/fixtures/aladin_stub.js is absent.
#: Every call on ``A`` and on the objects it returns is recorded in ``window.__aladinCalls``
#: as ``[path, args]``; getters answer with plausible values.
ALADIN_STUB_INLINE = r"""
(function () {
  const calls = (window.__aladinCalls = window.__aladinCalls || []);
  const state = {ra: 266.4168, dec: -29.0078, fov: 1.0};
  function safe(a) {
    try { return JSON.parse(JSON.stringify(a)); } catch (e) { return String(a); }
  }
  function rec(path) {
    const target = function () {};
    return new Proxy(target, {
      get(t, prop) {
        if (prop === 'then') return undefined;
        if (prop === Symbol.toPrimitive) return () => path;
        if (typeof prop === 'symbol') return undefined;
        const p = path + '.' + prop;
        if (prop === 'getRaDec') return () => { calls.push([p, []]); return [state.ra, state.dec]; };
        if (prop === 'getFov') return () => { calls.push([p, []]); return [state.fov, state.fov]; };
        if (prop === 'getSize') return () => [400, 300];
        if (prop === 'pix2world') return (x, y) => [state.ra, state.dec];
        if (prop === 'world2pix') return (ra, dec) => [200, 150];
        if (prop === 'gotoRaDec') return (ra, dec) => { calls.push([p, [ra, dec]]); state.ra = ra; state.dec = dec; };
        if (prop === 'setFoV' || prop === 'setFov') return (f) => { calls.push([p, [f]]); state.fov = f; };
        return rec(p);
      },
      apply(t, thisArg, args) {
        calls.push([path, args.map(safe)]);
        if (path.endsWith('.on') || path.endsWith('.off')) return undefined;
        return rec(path + '()');
      },
      construct(t, args) {
        calls.push(['new ' + path, args.map(safe)]);
        return rec(path + '#');
      },
    });
  }
  const A = rec('A');
  window.A = new Proxy(A, {
    get(t, prop) {
      if (prop === 'init') return Promise.resolve();
      return A[prop];
    },
  });
})();
"""


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
    """xfail(strict=False) when any pane module in ``types`` is absent.

    By default the test stops there (``pytest.xfail``); checks placed before the call still
    run strictly. With ``PK_E2E_RUN_MISSING=1`` the test continues under an xfail marker.
    """
    missing = pane_modules_missing(types)
    if missing:
        _xfail(request, f"pane modules not landed yet (WP08-WP11): {', '.join(missing)}")
    return missing


def xfail_unless_legacyhash(request: Any) -> bool:
    if not LEGACYHASH.exists():
        _xfail(request, "web/js/core/legacyhash.js not landed yet (WP13)")
        return False
    return True


def needs_panes(*types: str):
    """Decorator form of :func:`xfail_unless_panes` (evaluated at collection time)."""
    missing = pane_modules_missing(types)
    return pytest.mark.xfail(condition=bool(missing), strict=False,
                             reason=f"pane modules not landed yet (WP08-WP11): {', '.join(missing)}")


needs_legacyhash = pytest.mark.xfail(condition=not LEGACYHASH.exists(), strict=False,
                                     reason="web/js/core/legacyhash.js not landed yet (WP13)")


def page_pane_types(portal: Any, page: str) -> list[str]:
    return [p["pane"] for p in portal.pages[page].panes]


# ---------------------------------------------------------------------------
# session fixtures
# ---------------------------------------------------------------------------

#: process-wide registry of the session resources (see the module docstring)
_SHARED: ModuleType = sys.modules.setdefault("_pk_e2e_shared_resources", ModuleType("_pk_e2e_shared_resources"))
if not hasattr(_SHARED, "slots"):
    _SHARED.slots = {}


def _acquire(key: str, factory: Callable[[], Any], closer: Callable[[Any], None] | None) -> Any:
    """Reference-counted shared resource: ``factory()`` runs on the first acquire only."""
    slot = _SHARED.slots.get(key)
    if slot is None:
        slot = _SHARED.slots[key] = {"obj": factory(), "refs": 0, "close": closer}
    slot["refs"] += 1
    return slot["obj"]


def _release(key: str) -> None:
    slot = _SHARED.slots.get(key)
    if slot is None:
        return
    slot["refs"] -= 1
    if slot["refs"] <= 0:
        del _SHARED.slots[key]
        if slot["close"] is not None:
            slot["close"](slot["obj"])


@pytest.fixture(scope="session")
def synth_root(tmp_path_factory) -> Path:
    def make() -> Path:
        env = os.environ.get("PK_SYNTH_DIR")
        if env:
            p = Path(env).resolve()
            p.mkdir(parents=True, exist_ok=True)
            return p
        return tmp_path_factory.mktemp("synth")
    root = _acquire("synth_root", make, None)
    yield root
    _release("synth_root")


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

    key = f"server:{synth_root}"
    srv = _acquire(key, lambda: DevServer(synth_root).start(), lambda s: s.stop())
    yield srv
    _release(key)


@pytest.fixture(scope="session")
def _playwright():
    sync_api = pytest.importorskip("playwright.sync_api")
    pw = _acquire("playwright", lambda: sync_api.sync_playwright().start(), lambda p: p.stop())
    yield pw
    _release("playwright")


@pytest.fixture(scope="session")
def browser(_playwright):
    from playwright.sync_api import Error as PWError
    try:
        b = _acquire("browser", lambda: _playwright.chromium.launch(args=CHROMIUM_ARGS, headless=True),
                     lambda x: x.close())
    except PWError as err:
        pytest.skip(f"Chromium unavailable for Playwright: {err}")
    yield b
    _release("browser")


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

    def errors(self, allow: tuple[str, ...] = ()) -> list[str]:
        """Every error the page produced, minus messages matching a regex in ``allow``.

        ``__pk.errors`` (console.error, window error, unhandledrejection), uncaught page
        errors, browser console errors (failed resource or module loads, CORS) for local or
        unknown sources, and local HTTP responses with status >= 400. Console errors whose
        source is an external URL are left out: the harness aborts those requests itself and
        the external-origin assertions cover them.
        """
        pk = [str(e) for e in self.page.evaluate(
            "() => (window.__pk && window.__pk.errors) ? window.__pk.errors.slice() : []")]
        out = list(pk) + [f"pageerror: {e}" for e in self.page_errors]
        seen = {e.strip() for e in pk}
        for kind, text, url in self.console:
            if kind != "error":
                continue
            if url and not LOCAL_URL.match(url):
                continue
            if text.strip() in seen or any(text.strip().startswith(e) for e in seen if e):
                continue   # console.error output already in __pk.errors
            out.append(f"console: {text}" + (f" ({url})" if url else ""))
        out += [f"http {e}" for e in self.http_errors]
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

    def aladin_calls(self) -> list:
        return self.page.evaluate("() => (window.__aladinCalls || []).slice()")

    def close(self) -> None:
        self.context.close()


def _stub_body() -> str:
    if ALADIN_STUB_FILE.exists():
        return ALADIN_STUB_FILE.read_text(encoding="utf-8")
    return ALADIN_STUB_INLINE


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
    """Helper namespace for the e2e modules (test modules must not import ``conftest``:
    ``tests/conftest.py`` shares the module name)."""
    return SimpleNamespace(
        ms=ms, xfail_unless_panes=xfail_unless_panes, xfail_unless_legacyhash=xfail_unless_legacyhash,
        pane_modules_missing=pane_modules_missing, page_pane_types=page_pane_types,
        ext_origins=ext_origins, ALLOWED_EXTERNAL=ALLOWED_EXTERNAL, CDN=CDN, MIRROR=MIRROR,
        LEGACYHASH=LEGACYHASH, read_json=read_json)
