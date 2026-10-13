"""cmd_explorer: pre-binned F212N/F480M colour-magnitude explorer (WP32 extra;
ARCHITECTURE.md section 14 / docs/port_table.json "cmd_explorer (critic)").

Legacy: ``jwst_gc_pipeline/quicklook/cmdview.py`` (``render()``) writes a static pair of
files under ``/orange/adamginsburg/jwst/releases/site/``:

* ``cmd_explorer.html`` -- a canvas-based CMD viewer with a table of 68 pointings.
* ``cmd_explorer_data.json`` (~1.4 MB) -- the *pre-binned* data: ``grid`` (hex-bin geometry),
  ``all`` (the hex-binned counts of all 4,735,837 stars), one ``fields[]`` entry per pointing
  (its own hex-binned counts plus an Aladin footprint polygon), and ``lineages``.

The legacy page never has per-star rows -- it only ever draws hex cells (``drawCells`` in
``cmdview.py``), each an axial ``(q, r)`` coordinate plus a star ``count``. This adapter keeps
that: it reconstructs each non-empty cell's data-space centre from ``grid.extent``/
``grid.radius`` with the exact formula ``cmdview.py``'s canvas renderer uses (``drawCells``,
``drawAxes``) --

    u = R * sqrt(3) * (q + r/2); v = R * 1.5 * r             # cmdview.py drawCells
    color = extent[0] + u * (extent[1] - extent[0])          # cmdview.py drawAxes (x)
    f212n = extent[2] + v * (extent[3] - extent[2])          # cmdview.py drawAxes (y)
    f480m = f212n - color

-- and stores one *row per occupied cell* (not one row per star): a lossless re-expression of
the legacy's own pre-binning as a :class:`~portalkit.spec.CatalogSpec` table, which is what
``portalkit``'s frozen bundle pipeline (``bundle/density.py``) expects to re-bin from. Only the
whole-sample ``all`` grid is ported (3,699 non-empty cells at full resolution); the 68 per-field
breakdowns, their Aladin footprint polygons and the pointings table are not (see
``docs/wps.json`` WP32 -- "implement what the data allows" -- and ``contract_issues``/
``deviations`` returned by the test suite's harness report for the reduced scope).

Tier A only (``tiers = ("A",)``): the JSON already holds everything needed; nothing is
computed from the original FITS mosaics.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from portalkit.spec import DensitySpec, ReadmeSpec, TierUnavailable, VirtualDim, catalog_from_rows

DATA_FILE = Path("/orange/adamginsburg/jwst/releases/site/cmd_explorer_data.json")
HTML_FILE = Path("/orange/adamginsburg/jwst/releases/site/cmd_explorer.html")

README = """# CMD explorer

Colour-magnitude diagram of the JWST Galactic Center Treasury point sources, F212N vs.
F212N-F480M (Vega), pre-binned into hexagonal cells by the legacy `cmdview.py` renderer.

## Caveats

- Each row of this catalog is one occupied hex cell of the legacy's whole-sample grid, not
  one star: the legacy page never stores per-star rows either, only hex-binned counts.
- The 68 individual pointings and their Aladin footprints that the legacy page's table and
  hover/pin interaction expose are not carried over by this pass.
"""


def _cell_rows(cells: list, extent: list[float], radius: float) -> list[dict]:
    """One row per non-empty ``(q, r, count)`` triple of ``cells``, reconstructed into
    ``f212n``/``f480m`` magnitudes via ``cmdview.py``'s ``drawCells``/``drawAxes`` formula.
    A malformed triple (wrong length, non-finite count) is skipped, not raised."""
    xmin, xmax, ymin, ymax = extent
    rows: list[dict] = []
    for i, cell in enumerate(cells):
        if not isinstance(cell, (list, tuple)) or len(cell) != 3:
            continue
        q, r, count = cell
        if not all(isinstance(v, (int, float)) for v in (q, r, count)):
            continue
        if not math.isfinite(count) or count <= 0:
            continue
        u = radius * math.sqrt(3) * (q + r / 2.0)
        v = radius * 1.5 * r
        color = xmin + u * (xmax - xmin)
        f212n = ymin + v * (ymax - ymin)
        if not (math.isfinite(color) and math.isfinite(f212n)):
            continue
        rows.append({"id": f"cell{i}", "f212n": f212n, "f480m": f212n - color,
                     "count": float(count)})
    return rows


class CmdExplorerAdapter:
    name = "cmd_explorer"
    tiers = ("A",)

    def datasets(self, ctx):
        if ctx.tier not in self.tiers:
            raise TierUnavailable(f"cmd_explorer implements tier A only, not {ctx.tier}")
        if not DATA_FILE.is_file():
            raise FileNotFoundError(f"cmd_explorer: legacy data file not found: {DATA_FILE}")
        ctx.inputs.add(DATA_FILE)
        if HTML_FILE.is_file():
            ctx.inputs.add(HTML_FILE)
        doc = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        grid = doc.get("grid") or {}
        extent = grid.get("extent")
        radius = grid.get("radius")
        cells = (doc.get("all") or {}).get("cells") or []
        if not extent or radius is None:
            raise ValueError(f"{DATA_FILE}: 'grid.extent'/'grid.radius' missing")
        rows = _cell_rows(cells, [float(x) for x in extent], float(radius))
        if ctx.limit is not None:
            rows = rows[: ctx.limit]
        labels = doc.get("labels") or {}
        cat = catalog_from_rows(
            rows, "id", roles={"f212n": "mag", "f480m": "mag", "count": "value"}, key="cells",
            title="Hex-binned CMD cells",
            columns={
                "f212n": {"label": str(labels.get("y", "F212N")), "unit": "mag"},
                "f480m": {"label": "F480M", "unit": "mag"},
                "count": {"label": "stars in cell"},
            },
        )
        # VirtualDim.key is the expression text itself (a linear expression over this
        # catalog's stored numeric columns), not an arbitrary dim name.
        cat.virtual = [VirtualDim(key="f212n-f480m", label=str(labels.get("x", "F212N-F480M")),
                                  unit="mag", group="colors")]
        nx = grid.get("nx")
        nx = int(nx) if nx is not None else 60
        cat.density = [DensitySpec(key="cmd", x="f212n-f480m", y="f212n", nx=nx, ny=nx,
                                   xr=(extent[0], extent[1]), yr=(extent[2], extent[3]))]
        n_total = int((doc.get("all") or {}).get("n") or 0)
        cat.caveats = [
            f"{len(rows)} occupied hex cell(s) represent {n_total} stars pre-binned by the "
            "legacy cmdview.py renderer (whole-sample grid only; per-pointing breakdown not "
            "ported)."]
        # Rows are occupied hex-bin cells, each an aggregate of many stars, not one row per
        # star: the landing-card count must say "cells", not fall back to "source(s)".
        cat.noun = ("cell", "cells")
        ds = {"cells": cat, "readme": ReadmeSpec(markdown=README)}
        return ds

    def pages(self, ctx, ds):
        from portalkit.spec import PageSpec as P
        return [P(id="cmd_explorer", archetype="explorer", title="CMD explorer",
                 datasets=[k for k in ("cells", "readme") if k in ds])]


ADAPTER = CmdExplorerAdapter()
