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
cd /blue/adamginsburg/adamginsburg/repos/portalkit
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

```bash
portalkit build nirspec6927 --limit 50          # fixture-mode build into the stage root
portalkit check /orange/adamginsburg/portalkit_stage/nirspec6927/<build_id>/nirspec6927
portalkit serve /orange/adamginsburg/portalkit_stage/nirspec6927/<build_id>/nirspec6927 --port 8765
portalkit deploy nirspec6927 <build_id> --dry-run
portalkit deploy nirspec6927 <build_id> --yes   # writes <legacy_dir>/unified/
portalkit verify nirspec6927 --http
portalkit status
```

`build` writes only under the stage root. `deploy` writes unified pages next to the legacy
pages (`<legacy_dir>/unified/`) and records every file it owns in
`<target>/.portalkit-deploy.json`. `--replace` (back up legacy pages and leave redirect
stubs) is run by the user per family, after reviewing the contact sheet. See
section 10 of the contract for the full CLI and its exit codes, and section 11 for deploy
safety.

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
node --test tests/js/               # JS unit tests (pure modules)
python -m pytest -m e2e             # Levels 1-2: archetype smoke, caps matrix (opt-in, Playwright)
python -m pytest -m perf            # perf budgets            (opt-in)
python -m pytest -m slow            # 5M-row fixtures         (opt-in)
python -m pytest -m adapters        # adapter contract tests  (opt-in, needs real data)
python -m pytest -m served          # live server checks      (opt-in)
PK_RUN_ALL=1 python -m pytest       # everything
```

Markers are registered in `tests/conftest.py`. Tests without another marker are `unit`.
The opt-in groups (`e2e`, `perf`, `slow`, `adapters`, `served`) are skipped unless the `-m`
expression names them, `--run-<name>` is passed, or `PK_RUN_ALL=1` is set. Tests marked `js`
are skipped when `node` is not on `PATH`.

Code rules checked by the suite: no bare `except`, and no `except Exception` /
`except BaseException` without a re-raise (`tests/py/test_no_broad_except.py`); the Python
and JS constants match the contract, and the vendored files match their recorded sha256
(`tests/py/test_constants_sync.py`).
