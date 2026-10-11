"""Bridges from the legacy portal generators to portalkit specs (ARCHITECTURE.md section 9).

Submodules:

* :mod:`.importshim` -- :class:`LegacyImporter`: tracked, side-effect-checked imports of
  legacy generator modules (section 9.3).
* :mod:`.extract` -- pull the embedded data payload out of a legacy HTML page
  (``const DATA = {...}``, ``window.PORTAL = {...}``, ``<script type="application/json">``).
* :mod:`.manifold_payload` -- convert a legacy manifold payload (stars, dims, groups,
  presets, categories) into a :class:`~portalkit.spec.CatalogSpec` and a
  :class:`~portalkit.spec.ManifoldSpec`.
* :mod:`.gc_manifest` -- write a ``pk-catalog/1`` manifest that references the GC
  Treasury / field manifold binary files in place.
* :mod:`.hashmap` -- Python mirror of ``web/js/core/legacyhash.js`` (legacy URL grammars to
  canonical hash keys), used by tests and the ``extract-legacy`` command.
* :mod:`.dirgallery` -- build an :class:`~portalkit.spec.ImageSetSpec` from a directory of
  images whose file names encode facets (named-group regex).

Only the light modules are imported here; the others import ``portalkit.spec`` and are
loaded on first use.
"""

from __future__ import annotations

from .importshim import LegacyImporter, LegacyImportError, sha256_file
from .extract import LegacyExtractError, payload, payloads, script_json

__all__ = ["LegacyImporter", "LegacyImportError", "sha256_file", "LegacyExtractError",
           "payload", "payloads", "script_json"]
