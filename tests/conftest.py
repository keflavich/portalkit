"""Shared pytest configuration: markers, opt-in groups and common fixtures.

Markers (ARCHITECTURE.md section 13):

* ``unit``      Level 0; added automatically to every test that carries none of the others.
* ``js``        wraps ``node --test``; skipped when ``node`` is not on PATH.
* ``e2e``       Playwright/Chromium tests (Levels 1-2, per-port acceptance). Opt-in.
* ``adapters``  fixture-mode adapter contract tests (section 9.6). Opt-in.
* ``slow``      long runs (5M-row fixtures). Opt-in.
* ``served``    hits the live web server (Level 5). Opt-in.
* ``perf``      perf budgets (Level 3). Opt-in.

Opt-in groups run when the ``-m`` expression names them (``pytest -m perf``), when
``--run-<name>`` is given, or when ``PK_RUN_ALL=1``. Otherwise they are skipped with a reason,
so plain ``pytest -q`` stays a Level 0 run (section 13: under 1 minute).

Directory defaults: tests under ``tests/e2e`` get ``e2e``, ``tests/perf`` get ``perf`` and
``tests/adapters`` get ``adapters`` without having to mark each test.

Exposure scan cost: an adapter build classifies its targets against a scan of the real served
roots (a fresh walk took minutes on a cold filesystem). While ``adapters`` tests run,
``$PK_EXPOSURE_CACHE_S`` is set to one day (:data:`ADAPTER_EXPOSURE_CACHE_S`) unless the
user set it, so they reuse the user's last scan from ``~/.cache/portalkit`` (read-only when
``$PK_CACHE_DIR`` points elsewhere) instead of revalidating it; it is removed again for
every other test. Real deploys are unaffected (the variable is never set outside pytest).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_ROOT = REPO_ROOT / "tests"

MARKERS = {
    "unit": "Level 0 unit test (default when no other marker is present)",
    "js": "runs node --test; skipped when node is absent",
    "e2e": "Playwright/Chromium end-to-end test (opt-in)",
    "adapters": "fixture-mode adapter contract test (opt-in)",
    "slow": "long-running test, e.g. 5M-row fixtures (opt-in)",
    "served": "checks the live served portal over HTTP (opt-in)",
    "perf": "performance budget test (opt-in)",
}

#: markers whose tests are skipped unless explicitly requested
OPT_IN = ("e2e", "adapters", "slow", "served", "perf")

#: directory (relative to tests/) -> marker added to every test collected below it
DIR_MARKERS = {"e2e": "e2e", "perf": "perf", "adapters": "adapters"}

_NON_UNIT = tuple(m for m in MARKERS if m != "unit")

#: ``$PK_EXPOSURE_CACHE_S`` while adapter tests run (one day), unless the user set it
EXPOSURE_CACHE_ENV = "PK_EXPOSURE_CACHE_S"
ADAPTER_EXPOSURE_CACHE_S = "86400"
#: whether this session set EXPOSURE_CACHE_ENV (it is only ever removed when it did)
_EXPOSURE_ENV = {"user": False, "set": False}


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("portalkit")
    for name in OPT_IN:
        group.addoption(f"--run-{name}", action="store_true", default=False,
                        help=f"run tests marked '{name}'")


def pytest_configure(config: pytest.Config) -> None:
    for name, desc in MARKERS.items():
        config.addinivalue_line("markers", f"{name}: {desc}")
    _EXPOSURE_ENV["user"] = EXPOSURE_CACHE_ENV in os.environ
    _EXPOSURE_ENV["set"] = False


def exposure_env_for(is_adapter: bool, environ: dict | None = None) -> None:
    """Set ``$PK_EXPOSURE_CACHE_S`` to a day for an adapter test, remove it for any other
    test; a value the user set before the session is never touched."""
    env = os.environ if environ is None else environ
    if _EXPOSURE_ENV["user"]:
        return
    if is_adapter:
        if env.get(EXPOSURE_CACHE_ENV) != ADAPTER_EXPOSURE_CACHE_S:
            env[EXPOSURE_CACHE_ENV] = ADAPTER_EXPOSURE_CACHE_S
        _EXPOSURE_ENV["set"] = True
    elif _EXPOSURE_ENV["set"]:
        env.pop(EXPOSURE_CACHE_ENV, None)
        _EXPOSURE_ENV["set"] = False


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    # runs before the item's fixtures are set up, so module-scoped adapter fixtures (which
    # build the portal and scan the served roots) see the variable too
    exposure_env_for(item.get_closest_marker("adapters") is not None)


def pytest_unconfigure(config: pytest.Config) -> None:
    if _EXPOSURE_ENV["set"]:
        os.environ.pop(EXPOSURE_CACHE_ENV, None)
        _EXPOSURE_ENV["set"] = False


def _requested(config: pytest.Config, name: str) -> bool:
    if os.environ.get("PK_RUN_ALL") == "1":
        return True
    if config.getoption(f"--run-{name}"):
        return True
    expr = config.getoption("-m") or ""
    # a positive mention in -m (e.g. "perf", "perf and not slow") requests the group;
    # "not perf" does not.
    tokens = expr.replace("(", " ").replace(")", " ").split()
    for i, tok in enumerate(tokens):
        if tok == name and not (i > 0 and tokens[i - 1] == "not"):
            return True
    return False


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    node_missing = shutil.which("node") is None
    skip_reasons = {name: pytest.mark.skip(
        reason=f"opt-in group '{name}': use -m {name}, --run-{name} or PK_RUN_ALL=1")
        for name in OPT_IN if not _requested(config, name)}
    for item in items:
        try:
            rel = Path(str(item.fspath)).resolve().relative_to(TESTS_ROOT)
        except ValueError:
            rel = None
        if rel is not None and len(rel.parts) > 1 and rel.parts[0] in DIR_MARKERS:
            item.add_marker(getattr(pytest.mark, DIR_MARKERS[rel.parts[0]]))
        names = {m.name for m in item.iter_markers()}
        if not names.intersection(_NON_UNIT):
            item.add_marker(pytest.mark.unit)
        for name, mark in skip_reasons.items():
            if name in names:
                item.add_marker(mark)
                break
        if "js" in names and node_missing:
            item.add_marker(pytest.mark.skip(reason="node is not on PATH"))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Absolute path of the portalkit repository root."""
    return REPO_ROOT


@pytest.fixture()
def devserver():
    """Factory for a started :class:`portalkit.deploy.devserver.DevServer`; every server it
    creates is stopped automatically at teardown.

    ``devserver(root, mounts={"/jwst/spectra/portal_unified/": staged_dir}, ...)`` mounts a
    sibling portal's staged directory at a deploy-relative URL path under the same
    host/port, so a foreign base that climbs above the test's served root (e.g.
    ``"../../../../jwst/spectra/portal_unified/data/"``) resolves without per-test routing
    (WPE9#6). Any other :class:`DevServer` keyword (``auth``, ``range``, ...) passes through.
    """
    from portalkit.deploy.devserver import DevServer

    made: list[DevServer] = []

    def make(root: Path | str, **kw: object) -> DevServer:
        srv = DevServer(root, **kw).start()
        made.append(srv)
        return srv

    yield make
    for srv in made:
        srv.stop()


@pytest.fixture(scope="session")
def node_bin() -> str:
    """Path of the ``node`` executable; skips the test when node is absent."""
    path = shutil.which("node")
    if path is None:
        pytest.skip("node is not on PATH")
    return path
