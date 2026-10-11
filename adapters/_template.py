"""Template family adapter (ARCHITECTURE.md section 9; walkthrough in docs/adapters.md).

Copy this file to ``adapters/<family>.py``, write ``sites/<family>.toml`` and edit the
constants and the two loader methods below. The template runs as-is against any legacy page
that embeds a ``const DATA = {...}`` payload with a list of row objects (set the
``PK_TEMPLATE_HTML`` environment variable to try it), which makes it a usable Tier A
starting point.

Rules every adapter follows:

* Importing the module has no side effects: no file writes, no network, no heavy imports
  that touch the working directory. Contract test 9.6(a) imports it in an empty cwd and
  fails when files appear.
* Every file the adapter reads is registered with ``ctx.inputs.add(path)``. Paths under a
  tmp directory are refused (exit 5) unless the build runs with ``--allow-tmp-inputs``.
* Legacy science code is imported only through ``ctx.legacy.module(name)`` (Tier B); the
  shim records the module's sha256 in the build provenance.
* Expensive derived values are cached with ``ctx.cache.memo(key, fn, inputs=[paths])``. The
  memo key includes the inputs' stats and the build's tier and ``limit``; put any other
  setting that changes the value into ``key``.
* ``ctx.limit`` (fixture mode) caps the rows per catalog; contract tests run with 50.
* Tiers an adapter does not implement raise ``TierUnavailable``.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from portalkit.legacy import extract
from portalkit.spec import (
    ReadmeSpec, RecordSetSpec, Spectrum, TemplateSpec, TierUnavailable, catalog_from_rows,
    standard_pages,
)

#: legacy page whose embedded payload Tier A reads (edit, or set $PK_TEMPLATE_HTML)
LEGACY_HTML = os.environ.get("PK_TEMPLATE_HTML", "/path/to/legacy/portal/index.html")
#: payload fields holding the row id and position
ID_FIELD, RA_FIELD, DEC_FIELD = "id", "ra", "dec"
#: legacy module (on ``legacy_paths`` of the site registry) and its loader functions (Tier B)
LEGACY_MODULE = "my_legacy_analysis"
#: directory of per-source spectra (Tier B; ``<id>.txt`` with wavelength [um] and flux [Jy])
SPECTRA_DIR = "/path/to/spectra"

README = """# Template portal

Describe the data set, its provenance and caveats here. The landing page renders this
Markdown subset (headings, paragraphs, lists, links, code, tables).
"""


class TemplateAdapter:
    name = "template"
    tiers = ("A", "B")

    # -- datasets -----------------------------------------------------------------------
    def datasets(self, ctx):
        if ctx.tier == "A":
            rows = self._rows_tier_a(ctx)
        elif ctx.tier == "B":
            rows = self._rows_tier_b(ctx)
        else:
            raise TierUnavailable(f"{self.name}: tier {ctx.tier} is not implemented")
        if ctx.limit is not None:
            rows = rows[:ctx.limit]
        roles = {k: "mag" for k in rows[0] if k.lower().startswith("f") and k[1:4].isdigit()}
        cat = catalog_from_rows(rows, ID_FIELD, RA_FIELD, DEC_FIELD, roles=roles, key="src",
                                title="Sources")
        ds = {"src": cat, "readme": ReadmeSpec(markdown=README), "tpl": TemplateSpec()}
        spec_ids = [str(r[ID_FIELD]) for r in rows
                    if os.path.exists(self._spectrum_path(r[ID_FIELD]))]
        if spec_ids:
            for i in spec_ids:
                ctx.inputs.add(self._spectrum_path(i))
            ds["spec"] = RecordSetSpec(key="spec", kind="pks1", catalog="src", ids=spec_ids,
                                       loader=self._load_spectrum)
        return ds

    # -- pages --------------------------------------------------------------------------
    def pages(self, ctx, ds):
        # landing + one explorer per catalog + a spectra page when a pks1 set exists.
        # Return explicit PageSpec objects instead for custom layouts or titles.
        return standard_pages(ds, spectra="spec" in ds)

    # -- tier A: embedded legacy payload ------------------------------------------------
    def _rows_tier_a(self, ctx):
        ctx.inputs.add(LEGACY_HTML)
        data = extract.payload(Path(LEGACY_HTML))
        rows = data.get("rows", data.get("stars")) if isinstance(data, dict) else data
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"{LEGACY_HTML}: payload has no row list")
        return [dict(r) for r in rows]

    # -- tier B: legacy loader functions ------------------------------------------------
    def _rows_tier_b(self, ctx):
        mod = ctx.legacy.module(LEGACY_MODULE)
        table = mod.load_catalog()             # an astropy Table or a list of dicts
        return [dict(zip(table.colnames, map(_py, r))) if hasattr(table, "colnames") else dict(r)
                for r in table]

    # -- records ------------------------------------------------------------------------
    @staticmethod
    def _spectrum_path(source_id):
        return os.path.join(SPECTRA_DIR, f"{source_id}.txt")

    def _load_spectrum(self, source_id):
        path = self._spectrum_path(source_id)
        if not os.path.exists(path):
            return None                        # skipped and listed in the build report
        wave, flux = np.loadtxt(path, unpack=True, usecols=(0, 1))
        order = np.argsort(wave)
        return Spectrum(wave=wave[order], channels={"flux": flux[order]})


def _py(v):
    """numpy scalar -> python scalar (masked -> None)."""
    if np.ma.is_masked(v):
        return None
    return v.item() if hasattr(v, "item") else v


ADAPTER = TemplateAdapter()
