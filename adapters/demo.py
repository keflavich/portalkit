"""Self-contained demo family (ARCHITECTURE.md section 9; walkthrough in docs/adapters.md).

This is the family a new clone can build immediately: every row and every spectrum is
synthesized in memory from a fixed seed (``ctx.seed``, default 20261010), so the build is
reproducible and needs no legacy mount, no network access and no committed data file.
``portalkit build demo --limit 50`` works right after ``git clone`` on any machine.

It exercises a catalog explorer (list/sky/scatter/detail panes, via
:func:`portalkit.spec.standard_pages`) and a spectra page, the two archetypes a first-time
contributor is most likely to copy from. Replace this module and ``sites/demo.toml`` with a
real family's adapter once real data is available; see ``docs/adapters.md``.

Tier A only (``tiers = ("A",)``): there is no legacy page or legacy module behind this
family, so tiers B and C do not apply.
"""

from __future__ import annotations

import numpy as np

from portalkit.spec import (
    ReadmeSpec, RecordSetSpec, Spectrum, TierUnavailable, catalog_from_rows, standard_pages,
)

#: number of synthetic sources (more than the contract build's ``--limit 50``, so ``--limit``
#: exercises a real slice rather than the whole catalog)
N_SOURCES = 80
#: wavelength grid of the synthetic spectra, 1-5 um (a JWST NIRSpec-like range)
WAVE = np.linspace(1.0, 5.0, 200)

README = """# portalkit demo

A self-contained worked example. Every row and every spectrum here is synthetic, generated
at build time from a fixed seed (not read from any file). It needs no legacy mount, no
network access and no committed data, so `portalkit build demo --limit 50` works right
after `git clone` on any machine.

Copy `adapters/demo.py` and `sites/demo.toml` as a starting point for a real family once you
have real data; see [`docs/adapters.md`](../docs/adapters.md) for the full walkthrough.

## Caveats

- Positions, magnitudes and spectra are synthetic draws and carry no astrophysical meaning.
"""


def _rows(n: int, seed: int) -> list[dict]:
    """``n`` synthetic source rows, deterministic for a given ``seed``."""
    rng = np.random.default_rng(seed)
    classes = ["A", "B", "C"]
    ra0, dec0 = 266.0, -29.0   # an arbitrary sky point, not a real position
    out = []
    for i in range(n):
        mag_blue = float(14.0 + rng.normal(0, 1.2))
        mag_red = float(mag_blue - 0.6 + rng.normal(0, 0.3))
        out.append({
            "id": f"demo{i:03d}",
            "ra": float(ra0 + rng.normal(0, 0.02)),
            "dec": float(dec0 + rng.normal(0, 0.02)),
            "mag_blue": mag_blue,
            "mag_red": mag_red,
            "class": classes[i % len(classes)],
        })
    return out


def _spectrum(seed: int, index: int) -> Spectrum:
    """A deterministic synthetic spectrum for source ``index``, seeded off ``seed`` and the
    index so every source gets its own reproducible draw without sharing RNG state."""
    rng = np.random.default_rng((seed, index))
    continuum = 1e-3 * (WAVE / 2.0) ** (-1.5)
    line_center = 2.0 + 2.0 * rng.random()
    line = 0.3 * continuum.max() * np.exp(-0.5 * ((WAVE - line_center) / 0.05) ** 2)
    noise = rng.normal(0, 0.02 * continuum.max(), WAVE.size)
    flux = continuum + line + noise
    err = np.full(WAVE.size, 0.02 * continuum.max())
    return Spectrum(wave=WAVE, channels={"flux": flux, "err": err}, units={"flux": "Jy"},
                    meta={"name": f"demo spectrum {index}"})


class DemoAdapter:
    name = "demo"
    tiers = ("A",)

    def datasets(self, ctx):
        if ctx.tier not in self.tiers:
            raise TierUnavailable(f"demo implements tier A only, not {ctx.tier}")
        n = N_SOURCES if ctx.limit is None else min(N_SOURCES, int(ctx.limit))
        rows = _rows(n, ctx.seed)
        cat = catalog_from_rows(
            rows, "id", "ra", "dec", key="src", title=f"Demo sources ({len(rows)})",
            roles={"mag_blue": "mag", "mag_red": "mag", "class": "category"},
            columns={"mag_blue": {"label": "Blue mag", "unit": "mag"},
                     "mag_red": {"label": "Red mag", "unit": "mag"}})
        cat.caveats = ["Synthetic demo data: generated at build time from a fixed seed, not "
                       "a real survey."]
        ids = [r["id"] for r in rows]
        index_of = {r["id"]: i for i, r in enumerate(rows)}
        seed = ctx.seed
        loader = lambda source_id: _spectrum(seed, index_of[source_id])  # noqa: E731
        spec = RecordSetSpec(key="spec", kind="pks1", catalog="src", ids=ids, loader=loader,
                             enc={"flux": "f32", "err": "f32"})
        return {"src": cat, "spec": spec, "readme": ReadmeSpec(markdown=README)}

    def pages(self, ctx, ds):
        return standard_pages(ds, spectra=True)


ADAPTER = DemoAdapter()
