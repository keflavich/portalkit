"""Browser-test conftest for tests/e2e (ARCHITECTURE.md section 13, Levels 1-2).

The fixtures (``synth_root``, ``synth``, ``server``, ``browser``, ``open_page``,
``page_cache``, ``e2e``) live in ``tests/conftest_browser.py`` (IMPLEMENTATION_NOTES item 94).
That module is registered here as the plugin ``pk_conftest_browser``; ``tests/perf/conftest.py``
registers the same module object, so ``pytest -m "e2e or perf"`` defines each session fixture
once and starts one sync Playwright.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_PLUGIN = "pk_conftest_browser"


def _browser_plugin():
    mod = sys.modules.get(_PLUGIN)
    if mod is None:
        path = Path(__file__).resolve().parents[1] / "conftest_browser.py"
        spec = importlib.util.spec_from_file_location(_PLUGIN, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[_PLUGIN] = mod
        spec.loader.exec_module(mod)
    return mod


def pytest_configure(config: pytest.Config) -> None:
    _browser_plugin().register(config)
