"""Perf-test conftest for tests/perf (ARCHITECTURE.md section 13, Level 3).

Registers ``tests/conftest_browser.py`` as the plugin ``pk_conftest_browser`` (the same module
object ``tests/e2e/conftest.py`` registers; IMPLEMENTATION_NOTES item 94), which provides the
``synth_root``, ``synth``, ``server``, ``browser``, ``open_page`` and ``e2e`` fixtures.
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
