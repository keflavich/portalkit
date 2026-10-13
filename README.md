# portalkit

portalkit generates static web portals for astronomical catalogs, spectra and images. A
Python builder (Python 3.13, numpy, astropy) runs per-family **adapters** and writes a
static tree: small HTML shells with an embedded page config, compact columnar binary
bundles and sharded per-source records. A vanilla ES-module viewer, hosted once per
version, renders seven page archetypes (landing, explorer, manifold, gallery, spectra,
wavelength, narrative) for catalogs of about 8 to 5M rows.

The contract for every module, file format and test lives in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Install

```bash
git clone <url> && cd portalkit
pip install -e .                  # numpy, astropy
pip install -e ".[test]"          # + pytest, playwright
pip install -e ".[thumbs]"        # + matplotlib (portalkit thumbs)
pip install -e ".[extinction]"    # + dust_extinction (regenerate G23 tables)
python -m playwright install chromium   # e2e tests only
```

Use an editable install (or run from the checkout). The viewer under `web/` is not package
data: `portalkit build` and `portalkit publish-assets` read it from the source tree, so a
regular `pip install .` produces a builder that cannot find the viewer.

## Quickstart

The `demo` family (`adapters/demo.py`, `sites/demo.toml`) is self-contained: every row and
spectrum is synthesized in memory from a fixed seed, so it builds right after a clone with
no external data, no legacy mount and no network access.

```bash
export PK_STAGE_ROOT=./stage              # or pass --stage-root ./stage on every command below

portalkit build demo --limit 50 --vendor-assets   # --vendor-assets: a fully self-contained
                                                   # tree (see "Vendoring assets" below)
portalkit check "$PK_STAGE_ROOT/demo/<build_id>" --gate G2
portalkit serve "$PK_STAGE_ROOT/demo/<build_id>/demo" --port 8765
portalkit status
```

`build` writes only under the stage root. `portalkit check --gate G2` validates every
document against the schemas and size budgets; `portalkit serve` runs a local dev server so
`http://localhost:8765/` renders the built portal. `docs/adapters.md` walks through
`adapters/demo.py` as the worked example for writing your own adapter.

A deployable portal also has `deploy`/`verify`/`rollback` commands that write a unified tree
next to a family's legacy pages (`<legacy_dir>/unified/`) and record every file they own in
`<target>/.portalkit-deploy.json`; see section 10 of the contract for the full CLI and its
exit codes, and section 11 for deploy safety. The `demo` family's `legacy_dir`/`target_dir`
in `sites/demo.toml` are placeholders, not real paths, so skip `deploy` for it.

### Vendoring assets

The viewer JS/CSS and shared physics resources are normally served once, per version, from a
published host (`portalkit/version.py`'s `ASSET_BASE`/`RES_BASE`, overridable with
`$PK_ASSET_BASE`/`$PK_RES_BASE`). Anyone other than the author should instead build with
`--vendor-assets`, which copies that tree into the build itself (`_pk/v<version>/`, `res/`)
so the portal has no external dependency at serve time — the mode used in the quickstart
above and the one to use for a first build on a new machine.

### Author's HPG deployment

The families under `sites/` besides `demo` (`nirspec6927`, `w51`, `robitaille`, ...) read
real data from HiPerGator paths under `/orange/adamginsburg` and `/blue/adamginsburg`, and
their adapters' `legacy_paths`/`inputs` assume that mount. Building or deploying them needs
the author's data and runs from `/blue/adamginsburg/adamginsburg/repos/portalkit` on
HiPerGator; a fresh clone elsewhere can read their adapters and site registries as further
worked examples but cannot build them without that data.

## Layout

| Path | Contents |
|---|---|
| `portalkit/` | builder: bundle writers, physics resources, spec/caps/config, render, deploy, legacy bridge, CLI |
| `portalkit/constants.py`, `web/js/core/constants.js` | shared numeric thresholds (contract section 6.12), kept identical by a test |
| `portalkit/schema.py` | validators for every JSON document (`validate_<doc>(obj) -> list[str]`, `assert_valid_<doc>(obj)`) |
| `adapters/` | one module per portal family, exporting `ADAPTER` |
| `sites/` | per-family site registry (`<family>.toml`) |
| `web/` | viewer: `shell.html`, CSS tokens, ES modules (no build step, no npm deps) |
| `web/js/vendor/gc/` | GC manifold modules vendored byte-for-byte (see [`VENDORED.md`](VENDORED.md)) |
| `tests/` | pytest suites (`py/`, `adapters/`, `e2e/`, `perf/`) and `node --test` suites (`js/`) |
| `tools/` | contact sheet and other review helpers |

Further reading: [`docs/bundle.md`](docs/bundle.md) (data formats),
[`docs/js.md`](docs/js.md) (pane authoring), [`docs/adapters.md`](docs/adapters.md)
(writing an adapter), [`docs/deploy.md`](docs/deploy.md) (deploy runbook).

## Tests

```bash
python -m pytest -q                 # Level 0: unit tests (opt-in groups are skipped)
python -m pytest -q -m "not e2e"    # unit tests only (same as above, e2e deselected)
node --test tests/js/*.test.mjs     # JS unit tests (pure modules; 600+ tests)
python -m pytest -m e2e             # Levels 1-2: archetype smoke, caps matrix (opt-in, Playwright)
python -m pytest -m perf            # perf budgets            (opt-in)
python -m pytest -m slow            # 5M-row fixtures         (opt-in)
python -m pytest -m adapters        # adapter contract tests  (opt-in, needs real data)
python -m pytest -m served          # live server checks      (opt-in)
python -m pytest --run-e2e --run-perf --run-adapters --run-served   # same opt-in groups, via flags
PK_RUN_ALL=1 python -m pytest       # everything
```

`node --test tests/js/` (no glob) fails on recent Node: it resolves the bare directory as a
single entry module instead of discovering `*.test.mjs` files in it, so always pass the glob.

Markers are registered in `tests/conftest.py`. Tests without another marker are `unit`.
The opt-in groups (`e2e`, `perf`, `slow`, `adapters`, `served`) are skipped unless the `-m`
expression names them, the matching `--run-e2e`/`--run-perf`/`--run-slow`/`--run-adapters`/
`--run-served` flag is passed, or `PK_RUN_ALL=1` is set. Tests marked `js` are skipped when
`node` is not on `PATH`.

Code rules checked by the suite: no bare `except`, and no `except Exception` /
`except BaseException` without a re-raise (`tests/py/test_no_broad_except.py`); the Python
and JS constants match the contract, and the vendored files match their recorded sha256
(`tests/py/test_constants_sync.py`).
