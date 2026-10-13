# Writing a family adapter

An adapter turns one data family (a JWST field, a survey, a set of spectra) into portalkit
dataset specs and pages. The contract is ARCHITECTURE.md section 9; this page walks through
the files, the build and the checks. `adapters/_template.py` is a working starting point to
copy.

For a worked example you can run right after cloning, see `adapters/demo.py` and
`sites/demo.toml`: a self-contained family whose rows and spectra are synthesized in memory
from a fixed seed, so it needs no legacy mount, no network access and no committed data
file. `portalkit build demo --limit 50` builds it, `portalkit check <build_dir> --gate G2`
passes, and `tests/py/test_demo_adapter.py` is a contract-style test that runs as a normal
unit test (no `--run-adapters` needed) because it depends on nothing outside the repo. Read
it alongside this page: it is the smallest adapter that still exercises a catalog explorer
(list/sky/scatter/detail panes) and a spectra page via `standard_pages()`.

## Files

| File | Purpose |
|---|---|
| `adapters/<family>.py` | exports `ADAPTER` (an object with `name`, `tiers`, `datasets(ctx)`, `pages(ctx, ds)`) |
| `sites/<family>.toml` | site registry: adapter reference, portals, legacy dirs, inputs |
| `tests/adapters/test_<family>.py` | contract tests (section 9.6), run with `pytest -m adapters` |
| `tests/e2e/test_port_<family>.py` | browser parity tests of the ported pages |

Keep the adapter under about 400 lines, excluding `_fallback` copies of legacy functions.

## Site registry

```toml
family = "myfield"                      # == file stem, [a-z0-9_]+
adapter = "adapters.myfield:ADAPTER"    # module:attribute, imported with the repo root on sys.path
tier_default = "A"                      # tier used when `portalkit build` gets no --tier
legacy_paths = ["/orange/adamginsburg/myfield/analysis"]   # Tier B imports (ctx.legacy.module)
# status = "excluded"                   # keeps the registry but refuses builds

[[portal]]
id = "main"
title = "My field"
visibility = "public"                   # or "secure"
legacy_dir = "/orange/adamginsburg/web/public/myfield"     # where the legacy pages live
url = "https://data.rc.ufl.edu/pub/adamginsburg/myfield/" # served URL, ends with '/'
# target_dir = "..."                    # deploy target when it differs from legacy_dir
# served_by = "external"                # the page is served by another host
# aladin_url = "https://..."            # local Aladin Lite mirror
inputs = ["/orange/adamginsburg/myfield/catalogs/*.fits"]  # globs, recorded in the build report

[[portal.page]]                         # optional, per page: legacy files and URL grammars
id = "explorer"
legacy = ["catalog_explorer.html",      # a file relative to legacy_dir, or a table:
          {file = "catalog_explorer_red.html", preset = "view=red"},   # this file's stub preset
          {file = "plots/index.html", replace = false}]               # behind a symlink: never stubbed
grammar = ["kv-view"]                   # legacy hash grammars the page keeps answering
preset = ""                             # stub preset for items without their own
```

Several legacy files can map to one unified page. A table item's `preset` is the hash its
`--replace` redirect stub appends, so each old URL opens on its own view. `replace = false`
keeps a file out of `--replace` (ARCHITECTURE section 4.1, docs/deploy.md section 6).

A family with several portals (for example a public and a secure one) lists several
`[[portal]]` entries and sets `PageSpec.portal` on each page.

## The build context

`datasets(ctx)` and `pages(ctx, ds)` receive a `BuildContext`:

- `ctx.tier`: `"A"` (embedded legacy payloads), `"B"` (legacy loader functions) or `"C"`
  (native). Raise `TierUnavailable` for tiers the adapter lacks.
- `ctx.limit`: fixture mode; cap every catalog at this many rows when it is set.
- `ctx.inputs.add(path)`: register every file read. The registry records sha256, size and
  mtime for the build report and refuses tmp paths (exit 5) unless the build runs with
  `--allow-tmp-inputs`. File digests are cached by `(path, mtime_ns, size)`, so a large
  unchanged file is hashed once. A directory is recorded cheaply by its own mtime, its
  entry count and a digest of its top-level entry names. No tree is walked, so registering a
  multi-GB model grid directory costs one `scandir`. A file rewritten in place inside a
  registered directory, or anything below its top level, does not change that record:
  register the files (or sub-directories) whose content matters.
- `ctx.legacy.module(name)`: import a legacy module from `legacy_paths` inside an empty
  temporary cwd. The import fails with `LegacyImportError` (exit 6) when it creates files;
  the module sha goes into the build provenance and `portalkit status` flags changes.
- `ctx.cache.memo(key, fn, inputs=[paths])`: cache numpy arrays, astropy Tables or JSON
  under `~/.cache/portalkit/<family>/` (or `$PK_CACHE_DIR/<family>/`), keyed on `key`, the
  inputs' path, mtime and size (a directory input: the stamp described above), and the
  build's tier and `limit` (so a fixture-mode value never reaches a full build). A value
  memoised before the directory stamp existed (full tree digest key) is found once with one
  tree walk and then aliased under the new key, so long-running memos are kept.
  Put any other setting that changes the result (a law, a cut, a legacy module version)
  into `key`. `--no-cache` disables it.
- `ctx.log(message)`: progress output.
- `ctx.staging`: the build directory. Adapters normally write nothing themselves.

## Datasets

`datasets()` returns `{key: spec}` with each spec's own `key` equal to the dict key.

- Catalogs: `catalog_from_rows(rows, id, ra, dec, roles={...}, columns={...})`, or a
  `CatalogSpec` around an astropy Table. Give photometry columns `role="mag"` or `"flux"`
  and a `filter` key such as `"JWST/NIRCam.F410M"`; the capability resolver enables CMDs,
  SED overlays and extinction vectors from these.
- Legacy manifold payloads: `portalkit.legacy.manifold_payload.convert(payload)` returns
  `(CatalogSpec, ManifoldSpec)`. Dims that equal the difference of two other dims (the
  pairwise `c_<a>_<b>` colours) become virtual dims `"<a>-<b>"` instead of stored columns;
  pass `virtual_colours=False` to keep them stored.
- Existing GC tile manifests: a `CatalogSpec` with `foreign=ForeignRef("gc-manifest-1",
  url_base, manifest_path)`; the build writes a schema-2 manifest that points at the
  existing tiles (`portalkit.legacy.gc_manifest`). That manifest merges the `CatalogSpec`'s
  own `presets` (including `red_clump` and the CMD/CCD axes presets), `virtual` dims,
  `groups` (an explicit group wins over an auto-derived one with the same key),
  `manifold.frames`/`default_frame`, and each column's `filter`, so a GC-foreign catalog's
  metadata reaches the served page the same way a native catalog's does. A column whose key
  equals `CatalogSpec.id` gets `role="id"`. Local paths written into the manifest's
  `provenance` block (the source manifest path, and any `source`/`sample`/`cutout_layers`/
  `build_notes` values copied from the legacy manifest) go through the family's
  `PathLabeler` first, so `portalkit check --gate G2` passes.
- Per-source spectra: `RecordSetSpec(kind="pks1", catalog=..., ids=..., loader=fn)`. The
  loader returns a `Spectrum` (wavelength in um, ascending per segment) or `None` to skip.
  `kind="sedcurve"` takes any channel names (for example `SPHEREx` and `IRS`); `pks1`
  needs a `flux` channel.
- Spectra another portal already serves: `RecordSetSpec(..., foreign=ForeignRecords(
  base="../../wd2_2640/unified/data/", key="spec", id_column="SrcName"))`. The build
  writes no shards; the page reads `<base><key>/index.json`. `base` ends in `/` and is
  relative to the page or an absolute https URL on the same host. With `id_column`, the
  record of a row is looked up by that column's value instead of the row id. A public
  portal may only reference a public base (the build refuses a secure one, exit 3).
- Image directories: `portalkit.legacy.dirgallery.scan(directory, pattern, ...)` builds an
  `ImageSetSpec` from file names (named groups `id` and `kind` in the regex).
  `portalkit thumbs FAMILY` caches thumbnails for local images.
- Shared physics: `TemplateSpec()`, `FilterSetSpec(keys)` without `extra` and
  `ExtinctionSpec()` with defaults resolve to the shared `{res}` bundle. Non-default specs
  are written into the portal under `data/res/<key>/`. The filter registry holds the
  JWST, Roman, Spitzer, 2MASS, UKIRT and Paranal sets plus HST WFC3/IR (F110W, F125W, F127M, F139M, F153M,
  F160W), WFC3/UVIS2 and ACS/WFC (F555W, F814W), keyed by SVO id
  (`HST/WFC3_IR.F160W`) with aliases such as `WFC3_F160W` and `ACS_F814W`.
- Catalog fields whose values are all missing get the declared role's type (float for
  numeric roles, bool for `flag`), so an empty band stays a numeric column.
- `ReadmeSpec(markdown=...)` becomes the landing page text; `HipsSpec`, `OverlaySpec`,
  `NarrativeSpec` and `IsochroneSpec` cover the remaining page types.

## Pages

`standard_pages(ds, spectra=True, manifold=True, ...)` derives a landing page, explorers,
manifold, spectra and gallery pages from the datasets. Return explicit `PageSpec` objects
for custom titles, layouts, `state` or `caps_disable` entries. `[[portal.page]]` entries in
the site registry fill `PageSpec.legacy` (legacy files and hash grammars) when the adapter
leaves it empty. `dim_alias = {legacy_dim = "dim"}` in a `[[portal.page]]` entry (or
`LegacyRef.dim_alias`) maps the dim keys of legacy hashes to the page's dims, for example
`{c_F162M_F210M = "f162m-f210m"}`; every value must be a dim of the page's catalogs.
`cmd_pane = "cmd"` (or `LegacyRef.cmd_pane`) names the scatter pane of the page that should
receive gc-query's legacy CMD axes (`a`, `b`, `m`) as `<cmd_pane>.x`/`<cmd_pane>.y` instead
of `<manifold>.frame`; `p=ccd` still always maps onto `<manifold>.frame` (the CCD panel has
no plain-axes pane). As of this writing `cmd_pane` is accepted by `config.assemble()` but
rejected by `schema.validate_page()`'s `page.legacy` key allowlist, so a page that sets it
cannot finish assembly until that allowlist is updated (not owned by this work package).

## Build, check, inspect

```bash
portalkit caps myfield                    # capability matrix per page (fixture mode, writes nothing)
portalkit build myfield --limit 50        # fixture build into $PK_STAGE_ROOT/myfield/<build_id>/
portalkit build myfield --tier B --jobs 2
portalkit check /blue/adamginsburg/adamginsburg/portalkit_stage/myfield/<build_id> --gate G1
portalkit serve /blue/adamginsburg/adamginsburg/portalkit_stage/myfield/<build_id>/main
portalkit status myfield
```

`myfield` above is a placeholder for a family with real data on your deployment target; run
the same commands against the `demo` family (self-contained, Tier A only) to see them work
with no setup:

```bash
export PK_STAGE_ROOT=./stage
portalkit build demo --limit 50 --vendor-assets
portalkit check "$PK_STAGE_ROOT/demo/<build_id>" --gate G2
portalkit serve "$PK_STAGE_ROOT/demo/<build_id>/demo"
```

The build writes `<stage_root>/<family>/<build_id>/<portal_id>/` with `portal.json`, one
HTML page per `PageSpec`, the bundles under `data/` and `build_report.json` (budgets,
capabilities, warnings, inputs, adapter and legacy module shas). `--vendor-assets` copies
the JS and CSS into `_pk/v<version>/` for builds that must work without the published
asset tree. `portalkit check` validates every document against the schemas and the size
budgets; `--gate G2` additionally refuses `json-legacy` record sets and absolute local paths
(`/orange/`, `/blue/`, `/home/`) in served `.html`/`.json` files, which G1 reports as
warnings. Provenance in served files is written as `<label>:<relative path>`, with the roots
taken from `legacy_dir`, `legacy_paths` and `inputs`. Keep absolute paths out of README text
and captions too. `build_report.json` keeps the full paths and is never deployed.
`physics.templates.load_source` follows the same rule for template spectra: `meta.source` is
`file:<basename>` for an absolute path, or the label a `labeler=` callable (such as the build's
`PathLabeler`) returns.

`portal.json` gains a `counts` entry for every built page except the landing page. Each entry
holds the size of the page's primary dataset: catalog rows, image items or record ids
(ARCHITECTURE section 4.2). Landing cards can therefore show counts without hard-coded text.
The card nouns default to source(s), image(s) or spectrum/spectra by dataset kind; a dataset
that counts something else declares `noun=("model", "models")` on its `CatalogSpec`,
`ImageSetSpec` or `RecordSetSpec`.
A `context=True` catalog (a density backdrop) counts only its manifest and density grids
toward first paint. Set `exact_rows=False` on it, because nothing lists it.

Legacy payloads can be inspected with `portalkit extract-legacy page.html --out payload.json`.

## Deploy

```bash
portalkit deploy myfield <build_id> --dry-run
portalkit deploy myfield <build_id> --yes             # writes the target; never replaces legacy files
portalkit deploy myfield <build_id> --replace --yes   # legacy pages become redirect stubs (backed up)
portalkit verify myfield --http
portalkit rollback myfield <build_id>
portalkit prune-stage myfield --keep 5                # deployed builds are kept
```

Without a terminal, `deploy` needs `--yes`; warn-level exposure decisions are refused
otherwise. See docs/deploy.md for the ownership manifest, exposure policy and exit codes.

## Contract tests

Contract tests (section 9.6) run each adapter with `limit=50` and check that the import
creates no files, `datasets()` finishes within 120 s, ids and aliases are unique, inputs
exist outside tmp paths, a fixture build passes `portalkit check`, Tier A and Tier B agree
within one quantization step, and the resolved capabilities cover the legacy ones in
`tests/data/inventory_caps.json`.
