"""Physics resources: filters, extinction, templates, isochrones, features, SED helpers.

ARCHITECTURE.md sections 5.10, 6.6 and 9.4 (WP02).

Each resource module has a ``write(spec, dest, *, ledger=None) -> PhysicsResult`` that writes
content-hashed files into ``dest`` (a :class:`~portalkit.bundle.writer.BundleWriter` or a
directory path) and returns the path of the main document relative to ``dest``:

=================  ==========================  ======================================
module             spec                         main document
=================  ==========================  ======================================
``filters``        ``FilterSetSpec``            ``filters.<h8>.json`` (``pk-filters/1``)
``extinction``     ``ExtinctionSpec``           ``extinction.<h8>.json``
``templates``      ``TemplateSpec``             ``templates/index.<h8>.json``
``isochrones``     ``IsochroneSpec``            ``iso.<h8>.json``
``features``       family list (or None: all)   ``features.<h8>.json``
=================  ==========================  ======================================

:func:`write_dataset` dispatches on the spec type and :func:`write_shared` writes the four
shared (``{res}``) resources at once for ``portalkit publish-res``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PhysicsResult:
    """Result of a physics ``write``: main document path (relative to the writer base),
    the document itself and every file written (paths relative to the writer base)."""
    path: str
    doc: dict
    files: list[str] = field(default_factory=list)


def _writer(dest: Any, ledger: Any = None):
    """A BundleWriter for ``dest`` (an existing writer, re-bound to ``ledger`` if given)."""
    from ..bundle.writer import BundleWriter
    if isinstance(dest, BundleWriter):
        if ledger is not None and dest.ledger is not ledger:
            return BundleWriter(dest.root, dest.base, ledger, dest.dataset, dest.fsync)
        return dest
    return BundleWriter(Path(dest), "", ledger)


from . import sed  # noqa: E402
from . import extinction  # noqa: E402
from . import filters  # noqa: E402
from . import templates  # noqa: E402
from . import isochrones  # noqa: E402
from . import features  # noqa: E402

__all__ = ["PhysicsResult", "sed", "extinction", "filters", "templates", "isochrones",
           "features", "write_dataset", "write_shared", "SHARED_KINDS"]

#: the shared resource kinds published under ``{res}`` (config.RES_KINDS)
SHARED_KINDS = ("templates", "filters", "extinction", "features")


def write_dataset(spec: Any, dest: Any, *, ledger: Any = None) -> PhysicsResult:
    """Write one physics dataset spec (TemplateSpec, FilterSetSpec, ExtinctionSpec,
    IsochroneSpec) into ``dest``; dispatch by class name so spec.py stays optional."""
    name = type(spec).__name__
    if name == "TemplateSpec":
        return templates.write(spec, dest, ledger=ledger)
    if name == "FilterSetSpec":
        return filters.write(spec, dest, ledger=ledger)
    if name == "ExtinctionSpec":
        return extinction.write(spec, dest, ledger=ledger)
    if name == "IsochroneSpec":
        return isochrones.write(spec, dest, ledger=ledger)
    raise TypeError(f"not a physics dataset spec: {name}")


def write_shared(dest: Any, *, ledger: Any = None, template_spec: Any = None,
                 filter_spec: Any = None, extinction_spec: Any = None,
                 families: Any = None) -> dict[str, str]:
    """Write templates, filters, extinction and features into ``dest`` (the ``{res}`` root).

    Returns ``{kind: path relative to dest}`` for ``res_refs`` (prefix with ``{res}``).
    Defaults: the jwst_template_spectra.json templates, every registry filter, G23 at
    R_V 3.1 and 5.5, every feature atlas.
    """
    w = _writer(dest, ledger)
    out = {
        "templates": templates.write(template_spec, w).path,
        "filters": filters.write(filter_spec, w).path,
        "extinction": extinction.write(extinction_spec, w).path,
        "features": features.write(families, w).path,
    }
    return out
