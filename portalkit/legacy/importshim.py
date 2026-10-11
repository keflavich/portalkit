"""Tracked import of legacy generator modules (ARCHITECTURE.md section 9.3).

Tier B adapters reuse the loader functions of the legacy generator scripts in
``/orange/adamginsburg/jwst/brick/analysis`` (and similar trees). :class:`LegacyImporter`
imports them from the site's ``legacy_paths`` with three safeguards:

* the import runs inside an empty temporary working directory, and a module that creates
  files there at import time is rejected (legacy scripts must keep their work under
  ``if __name__ == "__main__"``);
* the sha256 of every imported source file under ``legacy_paths`` is recorded, so the build
  report and the deployed provenance carry ``legacy_modules`` (``portalkit status`` flags a
  changed sha as ``LEGACY-CHANGED``);
* import failures (``ImportError``, ``SyntaxError``, ``AttributeError``) become
  :class:`LegacyImportError`, which the CLI maps to exit code 6.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import sys
import tempfile
from types import ModuleType
from typing import Any, Iterable

__all__ = ["LegacyImportError", "LegacyImporter", "sha256_file"]


class LegacyImportError(ImportError):
    """A legacy module could not be imported cleanly (section 10 exit code 6)."""

    exit_code = 6


def sha256_file(path: os.PathLike | str, chunk: int = 1 << 20) -> str:
    """Hex sha256 of a file's bytes."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _under(path: str, roots: Iterable[str]) -> bool:
    real = os.path.realpath(path)
    for r in roots:
        base = os.path.realpath(r)
        if real == base or real.startswith(base.rstrip(os.sep) + os.sep):
            return True
    return False


class LegacyImporter:
    """Import legacy modules from ``legacy_paths`` and record their source sha256.

    ``module(name)`` returns the imported module; ``attr(name, attribute)`` returns one
    attribute of it; ``shas()`` lists ``{name, path, sha256}`` for every module imported
    from ``legacy_paths`` so far (including modules pulled in indirectly).
    """

    def __init__(self, legacy_paths: Iterable[os.PathLike | str] = (), *,
                 log: Any = None) -> None:
        self.legacy_paths: list[str] = [os.path.abspath(os.fspath(p)) for p in legacy_paths]
        self._records: dict[str, dict] = {}
        self._log = log

    # ------------------------------------------------------------------
    def _ensure_path(self) -> None:
        # The paths stay on sys.path after the import: legacy functions often import
        # sibling modules lazily, inside the function bodies.
        for p in reversed(self.legacy_paths):
            if p not in sys.path:
                sys.path.insert(0, p)

    def _record(self, name: str, path: str) -> None:
        if not path or not os.path.isfile(path):
            return
        if self.legacy_paths and not _under(path, self.legacy_paths):
            return
        if name in self._records and self._records[name]["path"] == os.path.abspath(path):
            return
        self._records[name] = {"name": name, "path": os.path.abspath(path),
                               "sha256": sha256_file(path)}

    def module(self, name: str) -> ModuleType:
        """Import ``name`` (dotted names allowed) from ``legacy_paths``.

        The import runs with an empty temporary directory as the working directory; when
        the directory is not empty afterwards the module is removed from ``sys.modules`` and
        :class:`LegacyImportError` is raised.
        """
        if not isinstance(name, str) or not name:
            raise LegacyImportError(f"bad legacy module name {name!r}")
        cached = sys.modules.get(name)
        if cached is not None:
            path = getattr(cached, "__file__", None) or ""
            if not self.legacy_paths or _under(path, self.legacy_paths):
                self._record(name, path)
                return cached
            raise LegacyImportError(
                f"legacy module {name!r} is already imported from {path or '<unknown>'}, "
                f"outside legacy_paths {self.legacy_paths}")
        self._ensure_path()
        before = set(sys.modules)
        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory(prefix="pk-legacy-import-") as tmp:
            os.chdir(tmp)
            try:
                mod = importlib.import_module(name)
            except (ImportError, SyntaxError, AttributeError) as exc:
                raise LegacyImportError(f"cannot import legacy module {name!r}: "
                                        f"{type(exc).__name__}: {exc}") from exc
            finally:
                os.chdir(old_cwd)
                created = sorted(os.listdir(tmp))
            if created:
                for k in set(sys.modules) - before:
                    sys.modules.pop(k, None)
                raise LegacyImportError(
                    f"importing legacy module {name!r} created files in the working "
                    f"directory: {created[:10]}")
        path = getattr(mod, "__file__", None) or ""
        if self.legacy_paths and not _under(path, self.legacy_paths):
            raise LegacyImportError(f"legacy module {name!r} resolved to {path or '<builtin>'}, "
                                    f"outside legacy_paths {self.legacy_paths}")
        self._record(name, path)
        if self.legacy_paths:      # sibling legacy modules pulled in by this import
            for k in sorted(set(sys.modules) - before):
                m = sys.modules.get(k)
                f = getattr(m, "__file__", None) if m is not None else None
                if f and k != name:
                    self._record(k, f)
        if self._log is not None and name in self._records:
            self._log(f"legacy import {name} ({self._records[name]['sha256'][:12]})")
        return mod

    def attr(self, name: str, attribute: str) -> Any:
        """``getattr(module(name), attribute)``; a missing attribute raises
        :class:`LegacyImportError`."""
        mod = self.module(name)
        try:
            return getattr(mod, attribute)
        except AttributeError as exc:
            raise LegacyImportError(f"legacy module {name!r} has no attribute {attribute!r}") from exc

    def shas(self) -> list[dict]:
        """``[{name, path, sha256}]`` of every legacy source file imported so far, by name."""
        return [dict(self._records[k]) for k in sorted(self._records)]
