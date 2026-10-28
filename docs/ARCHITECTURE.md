# portalkit — architecture contract (v1.0.0)

Status: implementation contract. Implementation agents code against this file. Where this file and a proposal disagree, this file wins. Any change to a section marked **[FROZEN]** needs an edit to this file first, followed by a matching change to `portalkit/schema.py` or `web/js/core/constants.js` (WP00 owns both).

Repository: `/blue/adamginsburg/adamginsburg/repos/portalkit`. This is a new local git repo, and the user pushes it. Python package name `portalkit`. CLI entry point `portalkit`.

---

## 0. Summary and design lineage

portalkit is a static-site portal generator. It has five parts:

- **Builder (Python 3.13, numpy, astropy).** Runs family **adapters**, which return declarative dataset and page specs, and writes a self-contained static tree:
  - one small HTML shell per page;
  - one JSON page config embedded in that shell;
  - compact columnar binary bundles;
  - sharded per-source records.
- **Viewer (vanilla ES modules).** Hosted once, immutably, at `https://data.rc.ufl.edu/pub/adamginsburg/portalkit/v<semver>/`.
- **Pane system.** Renders any of seven archetypes at N = 8 to about 5M rows.
- **Shared store and data client.** One store and one worker-backed data client couple every pane on a page.
- **Deploy module.** Symlink-aware, with ownership-manifest bookkeeping. By default it writes unified pages next to legacy pages (`<legacy_dir>/unified/`). With `--replace` it backs up and stubs the legacy files.

Lineage. The skeleton comes from the **migration-risk-first** proposal, with these elements:
- Tier A/B/C adapters;
- the legacy payload extractor as bootstrap path and parity oracle;
- the `manifold_payload` converter;
- the exposure classifier, staging, ownership manifest, `--replace` with backups, gates G1–G4 and waves 0–6.

Grafts from **efficiency-first**:
- the DataClient API;
- Uint8Array selection masks;
- virtual-dim folding;
- quantize clip modes;
- LOD row order;
- sparse limit tables;
- per-(template, grid) files;
- pre-binned density;
- the record LRU with stale-response tokens;
- the `data-caps` parity oracle;
- `skyWindow` viewport markers.

Grafts from **capability-maximal**:
- store-only pane coupling with rAF-coalesced topics;
- the undo stack;
- exact selection semantics;
- dim-space overlays;
- context datasets;
- recipe deep links;
- the sessionStorage carry;
- spec2d PNG mode;
- the test server emulating Range, auth and Content-Encoding.

Fixes for the judges' fatal flaws:

| Judge finding | Fix in this contract | Section |
|---|---|---|
| Range-request packs on an HTTP/1.1 host with no Cache-Control | whole-file **shards** of independent gzip members (target 256 kB, max 512 kB) with an `[shard, off, len]` index; no Range dependency | §5.6 |
| GPU projection limited to MAXD = 28 dims (Robitaille 75–83 base cols, WD2 164 dims) | fold virtual dims to base columns; if support > 28, use **XYRenderer** fed by worker sparse projection | §7.3 |
| GPU memory of MRT density targets (48 B per device px) | at most 2 density panes per page; `gl/budget.js` 256 MB budget; DPR lowered before refusing | §7.5 |
| quantized values shown as if exact | exact row-chunk JSON by default for N ≤ 1M; hover shows step-aware precision; manifest records step | §5.5 |
| `json-legacy` spectra passthrough left permanent | allowed only for Tier A/B bootstrap; gate G2 requires `pks1` shards | §9.4 |
| Tier B coupling to daily-edited generators | import shim records module sha256, contract tests, copy-fallback | §9.3 |
| Robitaille row count | 28,248 rows (inventory), not 19,998 | §14 |
| SgrB2/Sickle exposure | both are classified `both` (public via `web/public/jwst/*`, secure via `web/secure/jwst`); declared `public` to match current exposure; Sickle intent is an open question | §11.2, §16 |
| no bootstrap path | Tier A extracts payloads from built legacy HTML | §9.2 |

---

## 1. Hard constraints (restated as tests)

| Constraint | Enforced by |
|---|---|
| Static hosting only; no server code at view time | shell + bundle only; `tests/py/test_shell.py` asserts no server-side templating markers |
| No JS build step, bundler or npm runtime deps; vanilla ES modules | `tests/py/test_web_tree.py` (WP12): no `package.json` under `web/`, every `import` specifier is relative |
| Only external runtime lib: Aladin Lite v3, self-hosted (`starformation.astro.ufl.edu`) with mirror `https://data.rc.ufl.edu/pub/adamginsburg/jwst-gc/aladin.js` (CDS is no longer the default source after its 2026-10-28 outage; a page may still set it per `aladin.cdn`/`aladin.mirror`) | `sky/aladin.js`; e2e asserts no other external origin is requested (Google Fonts are not used) |
| Builder: Python 3.13 + numpy + astropy (optional extras: `dust_extinction` for regenerating tables, `matplotlib` for thumbnails) | `pyproject.toml`; optional imports wrapped in `except ImportError` |
| Node and Playwright (Python, Chromium) for tests only | `tests/js/*.test.mjs` run by `node --test`; `tests/e2e` marked `e2e` |
| Page HTML < 200 kB; data lazy and binary-columnar | `render/shell.py` budget check; `portalkit check` |
| N = 8 … ~5M per page | tiers §7; perf tests §13 |
| Capabilities maximized; every legacy capability expressible | `caps.py` resolver; `test_parity_inventory.py` asserts enabled ⊇ legacy for each port-table row |
| No bare `except`; no `except Exception`/`BaseException` without re-raise | `tests/py/test_no_broad_except.py` (AST scan of `portalkit/`, `adapters/`, `tests/`, `tools/`) |
| Legacy pages keep working; unified pages under `<portal_dir>/unified/` unless `--replace`; `--replace` backs up first | `deploy/deploy.py`, `deploy/backup.py`; `test_deploy.py` |
| Hand-edited `jwst_archive_spectra_checklist.csv` never regenerated | annotate mode exports a diff only; `test_no_checklist_write.py` (WP28) greps the adapter for writes to that path |

---

## 2. Repository layout **[FROZEN paths]**

Every path below has exactly one owning work package (WP, see §15). Agents create only files their WP owns. A file that is missing here needs a contract edit before anyone adds it.

```
portalkit/                                   repo root
  pyproject.toml                    WP00   name=portalkit, requires-python>=3.13, deps numpy, astropy;
                                           extras: thumbs=[matplotlib], extinction=[dust_extinction], test=[pytest, playwright]
                                           [project.scripts] portalkit = "portalkit.cli:main"
  README.md                         WP00   quickstart, links to docs/
  VENDORED.md                       WP00   vendored file list with source path, sha256, patch notes
  .gitignore                        WP00
  docs/bundle.md                    WP01   human-readable bundle spec (mirrors §5)
  docs/deploy.md                    WP04   runbook: build -> serve -> deploy -> verify -> replace -> rollback
  docs/js.md                        WP06   pane authoring guide (mirrors §6)
  docs/adapters.md                  WP13   adapter how-to (mirrors §9)
  sites/<family>.toml               WP2x   one per family (see §4.1); owned by the family's adapter WP
  portalkit/
    __init__.py                     WP00   __version__ re-export
    version.py                      WP00   VERSION = "1.0.0"; ASSET_BASE template
    constants.py                    WP00   all numeric thresholds (§6.12); mirrored in web/js/core/constants.js
    schema.py                       WP00   hand-written validators for every JSON document in §4 and §5
    spec.py                         WP03   dataclasses (§9.1)
    caps.py                         WP03   capability resolver (§8)
    config.py                       WP03   page.json assembly from PageSpec + bundle manifests; validation of keys
    expr.py                         WP03   linear expression parser/evaluator (Python twin of web/js/data/expr.js)
    context.py                      WP13   BuildContext, Cache, InputRegistry, provenance
    build.py                        WP13   orchestration: adapter -> bundle -> config -> shell -> staging
    cli.py                          WP13   argparse CLI (§10)
    bundle/__init__.py              WP01
    bundle/codec.py                 WP01   gz, quantizers, shuffles, str/sparse tables (§5.3)
    bundle/catalog.py               WP01   CatalogSpec -> manifest + column files + row chunks + ids
    bundle/order.py                 WP01   LOD permutation, tile/Morton sort (§5.4)
    bundle/records.py               WP01   pks1/pki1/json record writers + shard packer (§5.6–5.8)
    bundle/density.py               WP01   pre-binned 2D density grids (§5.9)
    bundle/images.py                WP01   images.json writer, thumbnail hook
    bundle/writer.py                WP01   content-hash naming, atomic writes into staging, budget accounting
    physics/__init__.py             WP02
    physics/filters.py              WP02   filter registry -> filters.json (pivot wl, ZP, A_lambda/A_V)
    physics/extinction.py           WP02   G23 tables (from vendored ecsv; regenerate with dust_extinction)
    physics/templates.py            WP02   jwst_template_spectra.json -> per-(template,grid) pks1 files
    physics/isochrones.py           WP02   MIST track loader wrapper -> isochrones.json
    physics/features.py             WP02   line/feature atlases -> features.json
    physics/sed.py                  WP02   mag<->Jy, nuFnu; python reference for JS tests
    render/__init__.py              WP03
    render/shell.py                 WP03   page HTML from web/shell.html + page.json, '</' escaping, budget
    render/markdown_lite.py         WP03   README subset -> sanitized HTML
    deploy/__init__.py              WP04
    deploy/exposure.py              WP04   symlink graph classifier (§11.2)
    deploy/stage.py                 WP04   staging root, build ids
    deploy/deploy.py                WP04   ownership manifest, safe atomic writes, --replace stubs
    deploy/backup.py                WP04   backups + rollback
    deploy/verify.py                WP04   HTTP checks
    deploy/status.py                WP04   drift report
    deploy/htaccess.py              WP04   optional cache headers
    deploy/devserver.py             WP04   local server emulating Range/auth/Content-Encoding (used by CLI serve and tests)
    legacy/__init__.py              WP13
    legacy/extract.py               WP13   payload extraction from built legacy HTML
    legacy/manifold_payload.py      WP13   {stars,dims,groups,presets,categories} -> CatalogSpec + ManifoldSpec
    legacy/gc_manifest.py           WP13   GC manifest schema "1" -> pk-catalog/1 manifest referencing files in place
    legacy/hashmap.py               WP13   legacy hash/query grammar registry (Python side, for tests and stubs)
    legacy/dirgallery.py            WP13   directory scan -> ImageSetSpec with regex facets
    legacy/importshim.py            WP13   tracked import of legacy generator modules
    legacy/nirspec.py               WP25   shared NIRSpec helpers: grating cuts, pks1 segments, stitched pki1 s2d
    data/expr_vectors.json          WP03   shared expression test vectors (Python and JS)
    data/filters_registry.ecsv      WP02   filter metadata table
    data/g23_alav.ecsv              WP02   A_lambda/A_V per filter for R_V 3.1, 5.5
    data/features/*.json            WP02   atlases: ice, gas, h2, hi, ionized, stellar, w51_nebular, w51_photospheric
  adapters/
    __init__.py                     WP00   empty
    _template.py                    WP13   copy-me adapter skeleton
    sgrb2.py                        WP20
    robitaille.py                   WP21
    nirspec6927.py                  WP22 (core pages) ; narrative pages added by WP31 in adapters/nirspec6927_narrative.py
    nirspec6927_narrative.py        WP31
    dirgallery_sites.py             WP23
    wd2.py                          WP24
    nirspec2640.py                  WP25
    sickle_brick.py                 WP26
    spherex.py                      WP27
    jwst_archive.py                 WP28
    w51.py                          WP29
    gc_manifold.py                  WP30
    hips_tours.py                   WP31
    spitzer_spectra.py              WP31
  web/
    shell.html                      WP03
    css/tokens.css                  WP06   §12
    css/base.css                    WP06
    css/layout.css                  WP06
    css/print.css                   WP06
    css/panes/scatter.css           WP08
    css/panes/manifold.css          WP08
    css/panes/controls.css          WP08
    css/panes/sky.css               WP09
    css/panes/wavelength.css        WP09
    css/panes/narrative.css         WP09
    css/panes/spectrum.css          WP10
    css/panes/sed.css               WP10
    css/panes/list.css              WP11
    css/panes/detail.css            WP11
    css/panes/gallery.css           WP11
    css/panes/landing.css           WP11
    css/panes/ui.css                WP11   hover, lightbox, toast, help, export dialogs
    js/core/constants.js            WP00
    js/core/boot.js                 WP06
    js/core/store.js                WP06
    js/core/selection.js            WP06
    js/core/hash.js                 WP06
    js/core/legacyhash.js           WP13
    js/core/carry.js                WP06
    js/core/keys.js                 WP06
    js/core/theme.js                WP06
    js/core/loadscript.js           WP06
    js/core/testhooks.js            WP06
    js/core/fmt.js                  WP06
    js/core/registry.js             WP06
    js/layout/layout.js             WP06
    js/data/codec.js                WP05
    js/data/inflate.js              WP05   pure-JS raw-inflate fallback (port of tiny-inflate, MIT header kept)
    js/data/fetch.js                WP05
    js/data/client.js               WP05   DataClient (main-thread facade)
    js/data/hub.js                  WP05   dataset registry per page
    js/data/expr.js                 WP05
    js/data/records.js              WP05   shard LRU, pks1/pki1/json decode
    js/data/templates.js            WP05
    js/data/filters.js              WP05
    js/data/extinction.js           WP05
    js/data/isochrones.js           WP05
    js/workers/data.worker.js       WP05
    js/workers/rpc.js               WP05
    js/workers/ops/select.js        WP05
    js/workers/ops/raster.js        WP05
    js/workers/ops/query.js         WP05
    js/workers/ops/sky.js           WP05
    js/workers/ops/project.js       WP05
    js/workers/ops/export.js        WP05
    js/workers/ops/find.js          WP05
    js/gl/xyrenderer.js             WP07
    js/gl/xyshaders.js              WP07
    js/gl/budget.js                 WP07
    js/plot/canvas2d.js             WP07
    js/plot/axes.js                 WP07
    js/plot/overlays.js             WP07
    js/plot/brushui.js              WP07
    js/math/tour.js                 WP07
    js/math/compass.js              WP07
    js/math/fold.js                 WP07
    js/panes/scatter.js             WP08
    js/panes/manifold.js            WP08
    js/panes/hist.js                WP08
    js/ui/controls.js               WP08
    js/ui/axis_picker.js            WP08
    js/ui/chips.js                  WP08
    js/ui/legend.js                 WP08
    js/panes/sky.js                 WP09
    js/panes/wavelength.js          WP09
    js/panes/narrative.js           WP09
    js/sky/aladin.js                WP09
    js/sky/layers.js                WP09
    js/sky/markers.js               WP09
    js/sky/overlays.js              WP09
    js/sky/skybrush.js              WP09
    js/panes/spectrum1d.js          WP10
    js/panes/spectrum2d.js          WP10
    js/panes/sed.js                 WP10
    js/spec/features.js             WP10
    js/panes/list.js                WP11
    js/panes/detail.js              WP11
    js/panes/gallery.js             WP11
    js/panes/table.js               WP11
    js/panes/readme.js              WP11
    js/panes/landing.js             WP11
    js/ui/hover.js                  WP11
    js/ui/lightbox.js               WP11
    js/ui/export.js                 WP11
    js/ui/nav.js                    WP11
    js/ui/help.js                   WP11
    js/ui/toast.js                  WP11
    js/ui/annotate.js               WP11
    js/vendor/gc/gl/renderer.js     WP00   verbatim + 3-line theme patch (§6.11)
    js/vendor/gc/gl/shaders.js      WP00   verbatim
    js/vendor/gc/gl/colormaps.js    WP00   verbatim
    js/vendor/gc/config/style.js    WP00   verbatim
    js/vendor/gc/math/frame.js      WP00   verbatim
    js/vendor/gc/math/linalg.js     WP00   verbatim
    js/vendor/gc/workers/kernel.js  WP00   verbatim
    js/vendor/gc/app/pick.js        WP00   verbatim
  tests/
    conftest.py                     WP00   markers: unit (default), js, e2e, adapters, slow, served, perf
    py/test_no_broad_except.py      WP00
    py/test_schema.py               WP00
    py/test_constants_sync.py       WP00
    py/test_codec.py                WP01
    py/test_catalog_bundle.py       WP01
    py/test_order.py                WP01
    py/test_records.py              WP01
    py/test_density.py              WP01
    golden/codec/*                  WP01   bytes + expected JSON for JS decoders
    py/test_physics.py              WP02
    golden/physics/*.json           WP02   python reference values for JS
    py/test_spec.py                 WP03
    py/test_caps.py                 WP03
    py/test_config.py               WP03
    py/test_expr.py                 WP03
    py/test_shell.py                WP03
    py/test_markdown_lite.py        WP03
    py/test_exposure.py             WP04
    py/test_deploy.py               WP04
    py/test_backup_rollback.py      WP04
    py/test_verify.py               WP04
    py/test_devserver.py            WP04
    js/codec.test.mjs               WP05
    js/expr.test.mjs                WP05
    js/select.test.mjs              WP05
    js/records.test.mjs             WP05
    js/physics.test.mjs             WP05
    js/store.test.mjs               WP06
    js/selection.test.mjs           WP06
    js/hash.test.mjs                WP06
    js/fmt.test.mjs                 WP06
    js/fold.test.mjs                WP07
    js/tour.test.mjs                WP07
    js/axes.test.mjs                WP07
    js/sed.test.mjs                 WP10
    js/export.test.mjs              WP11
    js/legacyhash.test.mjs          WP13
    fixtures/aladin_stub.js         WP09
    fixtures/make_synthetic.py      WP12
    fixtures/legacy_snippets/*      WP13
    py/test_web_tree.py             WP12
    py/test_parity_inventory.py     WP12
    data/inventory_caps.json        WP12   {family/page: [legacy caps]} extracted from inventory.json
    data/port_table.json            WP12   §14 as data
    e2e/conftest.py                 WP12   devserver fixture, Chromium launch flags, aladin route stub
    e2e/test_archetype_smoke.py     WP12
    e2e/test_caps_matrix.py         WP12
    e2e/test_deep_links.py          WP12
    e2e/test_fallbacks.py           WP12   no WebGL2, no DecompressionStream, Aladin CDN blocked
    perf/test_perf_budget.py        WP12
    py/test_legacy_extract.py       WP13
    py/test_manifold_payload.py     WP13
    py/test_gc_manifest.py          WP13
    py/test_hashmap.py              WP13
    py/test_dirgallery.py           WP13
    py/test_importshim.py           WP13
    py/test_cli_build.py            WP13
    py/test_context.py              WPE1   input registry / cache key cost, pre-WPE1 memo adoption, PageConfig legacy items
    py/test_provenance.py           WPE1   PathLabeler (served provenance labels) and the local-path pattern of `check`
    adapters/test_<family>.py       WP2x   fixture-mode contract tests (§9.6)
    e2e/test_port_<family>.py       WP2x   per-port acceptance (§13 level 4)
  tools/
    contact_sheet.py                WP12   legacy vs unified screenshots -> review HTML
```

The repo is initialised by WP00 with `git init` and one commit. Every WP commits its own files only. Commit messages end with the session attribution lines.

---

## 3. Archetypes and the pane model

A page is a **pane tree**: nested `row`/`col` splits whose leaves are panes. The archetype supplies the default tree. `PageSpec.layout` may override it. Panes outside the archetype core start collapsed; the resolver adds them when the data supports the capability.

| Archetype | Default tree (`|` = row split, `/` = col split) | Required datasets | Core panes |
|---|---|---|---|
| `landing` | nav / (cards | readme) / counts | portal registry | landing, readme |
| `explorer` | list | (sky / scatter*) | (detail / spectrum1d / sed) | catalog | list, scatter or sky, detail |
| `manifold` | list | (manifold / compass-in-pane) | (detail / sed / spectrum1d / sky) | catalog with ≥3 numeric dims | manifold, list, detail |
| `gallery` | (facets / list) | gallery | (detail / scatter / sky) [collapsed] | image set | gallery |
| `spectra` | list | (spectrum1d / spectrum2d) | (detail / sed / sky) | spectrum record set + catalog | spectrum1d, list |
| `wavelength` | sky | rungs-rail (inside wavelength pane) | HiPS rungs | wavelength |
| `narrative` | narrative-beats | sticky(sky or scatter) | narrative spec | narrative |

Pane types (registry keys; module `web/js/panes/<type>.js`): `list, detail, scatter, manifold, hist, sky, wavelength, narrative, spectrum1d, spectrum2d, sed, gallery, table, readme, landing`.

Layout rules (implemented by `layout/layout.js`):
- Every split has a draggable splitter (resizable_panels) with keyboard support (focus plus arrows, 16 px steps).
- Every pane has a fold header (collapsible_panels) and a pop-out button with states `null | half | full`. Pop-out reparents the pane element; panes receive `resize()`.
- Persistence is `localStorage['pk:<portal>:<page>:layout']`, holding `{sizes:{splitId:[..]}, collapsed:{paneId:bool}}`. Every read and write is wrapped in try/catch. A layout version mismatch discards the stored value.
- At viewport width < 900 CSS px the tree flattens into a tab strip in tree order, with one pane visible at a time. A selection summary bar stays pinned at the bottom. Tests check that there is no horizontal page scroll at 390×844.


---

## 4. Configuration **[FROZEN schemas; validators in portalkit/schema.py]**

There are three configuration documents:
- the site registry (TOML, read with stdlib `tomllib`);
- `portal.json`, one per deployed portal;
- `page.json`, embedded in every page.

No YAML and no jsonschema dependency. `schema.py` exposes `validate_<doc>(obj) -> list[str]`, which returns error strings, plus `assert_valid_<doc>(obj)`, which raises `SchemaError(ValueError)`.

### 4.1 Site registry: `sites/<family>.toml`

```toml
family       = "nirspec6927"              # == file stem; [a-z0-9_]+
adapter      = "adapters.nirspec6927:ADAPTER"
tier_default = "B"                        # A | B | C
legacy_paths = ["/orange/adamginsburg/jwst/brick/analysis"]   # sys.path entries for Tier B imports
status       = "active"                   # active | excluded

[[portal]]                                # one or more deployable portals per family
id          = "nirspec6927"
title       = "NIRSpec 6927 · Brick MSA"
visibility  = "secure"                    # public | secure ; deploy refuses a mismatch (§11.2)
legacy_dir  = "/orange/adamginsburg/jwst/spectra/nirspec_6927"
target_dir  = "/orange/adamginsburg/jwst/spectra/nirspec_6927/unified"   # default: <legacy_dir>/unified[/<family>]
url         = "https://data.rc.ufl.edu/secure/adamginsburg/jwst/spectra/nirspec_6927/unified/"
served_by   = "data.rc.ufl.edu"           # or "external" (GC release site at starformation.astro.ufl.edu)
inputs      = ["/orange/adamginsburg/jwst/spectra/mastDownload/JWST",
               "/orange/adamginsburg/jwst/spectra/nirspec_6927/*.ecsv"]
aladin_url  = "https://aladin.cds.unistra.fr/AladinLite/api/v3/latest/aladin.js"   # optional override

[[portal.page]]                           # legacy -> unified mapping used by --replace and status
id      = "map"
legacy  = ["nirspec_6927_map.html"]       # >1 entry when several legacy files collapse into one page
preset  = ""                              # hash appended by the redirect stub, e.g. "role=nb"
grammar = ["bare-name"]                   # legacy hash/query grammars (§6.4)

[[portal.page]]                           # legacy items may also be tables {file, preset?, replace?}
id      = "gallery_6927"
legacy  = ["pngs/gallery_6927.html",
           {file = "pngs/gallery_6927_2221.html", preset = "role=c2221"},   # stub preset of this file
           {file = "imaging/index.html", replace = false}]                  # never stubbed by --replace
```

Legacy items (WPE1). Each `legacy` item is a string (a path relative to `legacy_dir`) or a table `{file, preset?, replace?}`:
- `preset` is the hash fragment the `--replace` redirect stub of that file appends (§11.4). It overrides the page `preset` for that file only; `preset = ""` means no fragment. Without it the page `preset` applies.
- `replace = false` keeps the file out of `--replace` stubbing and backups (default `true`). Use it for a legacy file reached through a served symlink (for example robitaille `imaging/ -> modelgrids/imaging_run/png`), which §11.3 would otherwise refuse. `deploy` lists such files as a plan warning; `status` still reports them.
- A file is listed at most once per page and must be relative without `..` components. Python: `portalkit.spec.LegacyFile`; `PageConfig.legacy` stays the list of file names and `PageConfig.files` holds the `LegacyFile` items.

Legacy dim alias (WPE6). `[[portal.page]] dim_alias = {legacy_dim_key = "unified_dim_key", ...}` (or `PageSpec.legacy.dim_alias`; the two merge, the site entry winning) maps the dim keys a legacy hash or query carries to the unified page's dims, for example `{c_F162M_F210M = "f162m-f210m", m_F162M = "f162m"}`. `config.py` checks that every value is a stored, virtual or manifold dim of a foreground catalog on the page and writes the map unchanged as `page.legacy.dim_alias`. `legacyhash.js` applies it to the `kv-view` x/y/z keys, to the column part of `kv-view` `c=<by>[~cmap~lo~hi]`, and to `spherex-colours`; `legacy/hashmap.py` mirrors the same rules. `gc-query` indexes dims by position and does not alias its `y` band name.

Rules:
- `target_dir` defaults to `<legacy_dir>/unified/`. When two families share a `legacy_dir`, each family uses `<legacy_dir>/unified/<family>/`. This applies to WD2 and NIRSpec 2640 in `web/public/jwst/wd2/nirspec_2640`.
- `status = "excluded"` records out-of-scope pages, such as the jwst-gc monitor, so that `portalkit status` lists them.
- Input globs are expanded at build time. An input under any of `/blue/*/tmp/`, `/tmp/` or `*/scratchpad/*` makes `build` exit with code 5 unless `--allow-tmp-inputs` is passed. A deploy of a build made with that flag is refused.

### 4.2 `portal.json` (written to `<target_dir>/portal.json`, fetched with `cache:'no-cache'`)

```json
{"schema":"pk-portal/1","id":"nirspec6927","title":"NIRSpec 6927 · Brick MSA","visibility":"secure",
 "build":"20261010T120000Z-1a2b3c4d","pk":"1.0.0",
 "pages":[{"id":"index","title":"Overview","file":"index.html","archetype":"landing"},
          {"id":"map","title":"Sky map","file":"map.html","archetype":"explorer"}],
 "related":[{"title":"WD2 portal","url":"https://data.rc.ufl.edu/pub/adamginsburg/jwst/wd2/nirspec_2640/unified/wd2/","auth":"public"}],
 "search":[{"ds":"src","page":"map","manifest":"data/src/manifest.json"}],
 "counts":[{"page":"map","ds":"src","kind":"catalog","n":165,"unit":"source","units":"sources"}]}
```

Only built pages are listed, so nav never shows a missing page. `related[].auth = "secure"` renders a "login required" badge.

`search` lists the catalogs of explorer and manifold pages (the landing search box opens their manifests). `counts` (optional, additive; WPE1) holds one entry per built non-landing page with the size of that page's primary dataset, so every landing card can show a count without fetching a manifest (the landing pane reads `counts` when present and falls back to the `search` manifests' `n` otherwise; IMPLEMENTATION_NOTES 121):
- `kind = "catalog"`: the manifest row count `n` of a non-context catalog;
- `kind = "images"`: the item count of an image set;
- `kind = "records"`: the id count `n` of a record set's `index.json`.

The primary dataset is chosen per archetype: `gallery` prefers images, then catalog, then records; `spectra` prefers records, then catalog, then images; every other archetype prefers catalog, then images, then records. A page with none of these has no entry. `unit`/`units` are the singular/plural nouns for the card text: the dataset's `noun` when its spec declares one (`CatalogSpec`/`ImageSetSpec`/`RecordSetSpec.noun`, e.g. `("model", "models")` for a model grid), else `source(s)`, `image(s)` or `spectrum/spectra` by kind. Validation: `page` is listed in `pages` and appears at most once; `kind` is one of the three; `n` is an integer ≥ 0.

### 4.3 `page.json` (embedded as `<script type="application/json" id="pk-page">`, ≤ 64 kB, `</` escaped as `<\/`)

Top-level keys (all required unless marked optional):

| Key | Type | Meaning |
|---|---|---|
| `schema` | `"pk-page/1"` | |
| `pk` | semver string | asset version the page was built against |
| `assets` | URL ending in `/` | asset base (default `https://data.rc.ufl.edu/pub/adamginsburg/portalkit/v<pk>/`; with `--vendor-assets`: `./_pk/v<pk>/`) |
| `res` | URL ending in `/` | shared resource base (`https://data.rc.ufl.edu/pub/adamginsburg/portalkit-res/` or `./res/`) |
| `portal` | `{id,title,visibility,nav:"portal.json"}` | |
| `page` | `{id,archetype,title,subtitle?,legacy?:{files:[..],grammar:[..],preset?,presets?:{file:preset},dim_alias?:{legacy_dim:dim}}}` | `files` are the legacy file names (strings); `presets` holds the per-file stub presets of §4.1 legacy tables (keys ⊆ `files`); `dim_alias` maps legacy dim keys to unified dim keys (§4.1, §6.4) |
| `datasets` | `{dsId: DatasetRef}` | see below |
| `layout` | LayoutNode | pane tree |
| `state` | object | initial store values (§6.2 keys); validated against dataset columns |
| `caps` | `{enabled:[cap...], disabled:{cap:reason}}` | resolver output (§8); mirrored into `<body data-caps>` |
| `links` | `{name: "page.html#id={id}"}` (optional) | cross-page templates; `{id}` substituted |
| `readme` | `{html:"...", provenance:{...}}` (optional, ≤ 16 kB, else `readme.<h8>.html` URL) | |
| `aladin` | `{cdn:url, mirror:url}` | |
| `budget` | `{html_bytes, first_paint_bytes}` | written by builder for tests |

`DatasetRef` kinds:

```json
{"kind":"catalog",   "url":"data/src/manifest.json", "context":false}
{"kind":"records",   "url":"data/spec/index.json", "catalog":"src", "record":"pks1"}   // also pki1 | json | sedcurve
{"kind":"records",   "url":"../../wd2_2640/unified/data/spec/index.json", "base":"../../wd2_2640/unified/data/",
                     "catalog":"src", "record":"pks1", "id_column":"SrcName"}                // foreign (WPE6)
{"kind":"images",    "url":"data/img/images.json", "catalog":"src"}
{"kind":"templates", "url":"{res}templates/index.3f9a2b1c.json"}
{"kind":"filters",   "url":"{res}filters.1c2d3e4f.json"}
{"kind":"extinction","url":"{res}extinction.9a8b7c6d.json"}
{"kind":"isochrones","url":"data/iso.5e6f7a8b.json"}
{"kind":"features",  "url":"{res}features.0a1b2c3d.json"}
{"kind":"hips",      "url":"data/hips.json"}
{"kind":"overlays",  "url":"data/overlays.json"}
{"kind":"narrative", "url":"data/narrative.json"}
{"kind":"portal",    "url":"portal.json"}
```

Foreign record sets (WPE6). A `RecordSetSpec(..., foreign=ForeignRecords(base, key, id_column=None))` names another portal's record set instead of writing its own. The build writes no shards, index or stand-in files for it; the page's records ref carries `base` (the URL prefix of the other portal's `data/` directory, ending in `/`) and `url = base + key + "/index.json"`, plus `id_column` when set. Everything below `base` is the other portal's own `data/<key>/` layout (§5.6).
- `base` is relative to the page (for example `../../wd2_2640/unified/data/`) or an absolute `https://` URL. A leading `/`, a backslash, `file:` or another scheme-like first segment is refused (`schema.foreign_base_error`), so no absolute filesystem path reaches a page.
- `data/records.js` resolves every record URL of the set (index, shards, `r/` files) against `base` when present, else against the portal's own `data/`. With `id_column`, the record of a row is looked up by that catalog column's value instead of the row id; the column must exist in the set's catalog.
- Visibility: the build resolves the ref against the portal URL and classifies it (§11.2). A secure portal may reference secure or public bases; a public portal may reference public bases only, and the build refuses a secure one with `ExposureRefusal` (exit 3). A cross-host absolute URL is refused.
- `portalkit check` accepts a foreign entry without local files (it is counted under `foreign`) and still rejects absolute filesystem paths. `verify --http` HEADs each foreign index (200 required; a 401 from a secure base without credentials is reported as a note).

`{res}` and `{assets}` are substituted client-side. Relative URLs resolve against the page URL.

LayoutNode: `{"row":[node...], "size"?:px|fraction}`, `{"col":[node...]}`, or a pane leaf:

```json
{"pane":"scatter","id":"ccd","dataset":"src","collapsed":false,"size":0.4,
 "options":{"x":"f182m-f410m","y":"f405n-f410m","color":"cls","density":"auto",
            "background":"ctx","overlays":["g23_vector","mist_1myr"],"presets":true}}
```

Pane `id` values are unique per page and default to the pane type. Each pane type documents its own `options` (§6.10). Unknown options are a validation error.

#### 4.3.1 Example (abridged): NIRSpec 6927 map → explorer

```json
{"schema":"pk-page/1","pk":"1.0.0",
 "assets":"https://data.rc.ufl.edu/pub/adamginsburg/portalkit/v1.0.0/",
 "res":"https://data.rc.ufl.edu/pub/adamginsburg/portalkit-res/",
 "portal":{"id":"nirspec6927","title":"NIRSpec 6927 · Brick MSA","visibility":"secure","nav":"portal.json"},
 "page":{"id":"map","archetype":"explorer","title":"Sky map of MSA sources",
         "legacy":{"files":["nirspec_6927_map.html"],"grammar":["bare-name"]}},
 "datasets":{
   "src":{"kind":"catalog","url":"data/src/manifest.json"},
   "spec":{"kind":"records","url":"data/spec/index.json","catalog":"src","record":"pks1"},
   "s2d":{"kind":"records","url":"data/s2d/index.json","catalog":"src","record":"pki1"},
   "prod":{"kind":"records","url":"data/prod/index.json","catalog":"src","record":"json"},
   "img":{"kind":"images","url":"data/img/images.json","catalog":"src"},
   "tpl":{"kind":"templates","url":"{res}templates/index.3f9a2b1c.json"},
   "flt":{"kind":"filters","url":"{res}filters.1c2d3e4f.json"},
   "ext":{"kind":"extinction","url":"{res}extinction.9a8b7c6d.json"},
   "feat":{"kind":"features","url":"{res}features.0a1b2c3d.json"},
   "hips":{"kind":"hips","url":"data/hips.json"},
   "ovl":{"kind":"overlays","url":"data/overlays.json"}},
 "layout":{"row":[
   {"pane":"list","dataset":"src","size":250},
   {"col":[{"pane":"sky","dataset":"src","options":{"hips":"hips","overlays":["msa_shutters","dither"]}},
           {"pane":"scatter","id":"ccd","dataset":"src","collapsed":true,
            "options":{"x":"f182m-f410m","y":"f410m"}}]},
   {"col":[{"pane":"detail","dataset":"src","options":{"template":"nirspec_source","products":"prod"}},
           {"pane":"spectrum1d","dataset":"spec","options":{"features":["ice","h2","hi"],"templates":"tpl"}},
           {"pane":"spectrum2d","dataset":"s2d","collapsed":true},
           {"pane":"sed","dataset":"src","collapsed":true}],"size":430}]},
 "state":{"color":{"by":"Observation"},"av":0,"rv":3.1},
 "caps":{"enabled":["keyboard_nav","cross_page_nav","..."],
         "disabled":{"isochrones_or_tracks":"no distance/age model for embedded MSA targets"}},
 "links":{"spectra":"specviewer.html#id={id}","manifold":"manifold.html#id={id}"},
 "aladin":{"cdn":"https://starformation.astro.ufl.edu/vendor/aladin-lite/3.8.2/aladin.js",
           "mirror":"https://data.rc.ufl.edu/pub/adamginsburg/jwst-gc/aladin.js"}}
```

Builder validation (`config.py`, errors fail `build`):
- Every column key or expression in `state`, `layout.options`, presets and overlays resolves against the dataset manifest. This catches the legacy `A_V` and `o${o}` defaults.
- Every URL is either relative inside the staged tree or absolute with a known exposure.
- The JSON size is ≤ 64 kB.
- Every enabled capability has a mounted provider (§8).

### 4.4 Column roles and expressions

`role ∈ {id, ra, dec, mag, flux, error, color, value, category, flag, text, url, image, limit}`.

| Field | Meaning |
|---|---|
| `filter` | key into `filters.json`; required for `mag`/`flux` to enable SED, extinction vectors, coverage shading and auto CMD/CCD presets |
| `err` | sibling error column key |
| `limit` | the column's limit side table is attached (§5.3 `sparse`); legacy `k` codes `u/l` map to kinds 1/2 |
| `invert` | defaults to `true` for `mag` |
| `log` | display/standardize in log10 |
| `validity` | key of a `bits` column; rows with bit 0 are treated as missing for this column (archive band coverage) |

**Virtual dims** (derived columns) are linear expressions over stored numeric columns. They are never stored. Grammar, shared by `portalkit/expr.py` and `web/js/data/expr.js` and tested against `portalkit/data/expr_vectors.json`:

```
expr   := ws term (ws ('+'|'-') ws term)* ws
term   := number ws '*' ws key | number '*' key | key | number
key    := [A-Za-z_][A-Za-z0-9_.]*      (must be a stored column; '-' inside keys is not allowed)
number := [0-9]+('.'[0-9]*)?([eE][+-]?[0-9]+)?
```

An expression compiles to `{w: {key: coeff}, c: const}`. Missing in any base column → missing. Colours like `f182m-f410m` and `0.5*f405n+0.5*f410m` are virtual. Non-linear quantities (ratios, logs, offsets from a locus) are computed by adapters and stored.

Virtual-dim keys in manifests use the expression text as `key`, with an optional display `label`.


---

## 5. Data bundle format **[FROZEN]**

### 5.1 Common rules

- **Byte order.** Little-endian everywhere.
- **File naming.** Every file portalkit writes has a content hash: `<stem>.<h8>.<ext>`, where `h8` is the first 8 hex digits of the sha256 of the bytes on disk. Exceptions are `manifest.json`, `index.json`, `images.json`, `hips.json`, `overlays.json`, `narrative.json`, `portal.json` and the HTML pages. Those use stable names and are fetched with `cache:'no-cache'`.
- **Binary extension is `.bin`.** Binary payloads are gzip-compressed *inside* the file, and the file is named `.bin` (never `.gz`). Apache therefore never adds `Content-Encoding`, and concatenated gzip members in shards (§5.6) are never decoded by the browser. `deploy/htaccess.py` adds `AddType application/octet-stream .bin`.
- **Gzip-magic check.** The client checks for magic `1f 8b` before inflating, as GC `loader_lite.isGzip` does. Foreign `.gz` files (GC) therefore work whether or not the server decoded them.
- **Gzip writer.** This is GC `gz()`, verbatim (from `GCTreasury_Portal_Manifold.py`): level 9, mtime 0, OS byte `\xff`. For shuffled data, `gz(data, filtered=True)` uses `zlib.Z_FILTERED` with wbits 31 and OS byte `\x03`. Output is deterministic, so content hashes are stable across rebuilds.
- **Inflate.** In a worker, the client uses `DecompressionStream('gzip')` for whole files and `DecompressionStream('gzip')` per member for shard slices. When `DecompressionStream` is absent, it falls back to `data/inflate.js`, which parses the gzip header and then raw-inflates.
- **JSON** is UTF-8, `ensure_ascii=False`, `allow_nan=False`. NaN is written as `null`.

### 5.2 Catalog bundle layout

```
data/<ds>/manifest.json
data/<ds>/cols/<key>.<h8>.bin                 one per stored column (or per chunk: <key>.c000.<h8>.bin ...)
data/<ds>/pos/{ra,dec}.<h8>.bin | {tile,dra,ddec}.<h8>.bin
data/<ds>/ids.<h8>.bin                        enc "str" (chunked like columns when N > 2^20)
data/<ds>/aliases.<h8>.json                   {"alias": row_index_in_stored_order} (only when aliases exist)
data/<ds>/limits/<key>.<h8>.bin               enc "sparse"
data/<ds>/rows/r<0000>.<h8>.bin               exact row chunks (gz JSON), 4096 rows each
data/<ds>/density/<preset>.<h8>.bin           pre-binned grids
```

`manifest.json` (`schema: "pk-catalog/1"`):

```json
{"schema":"pk-catalog/1","id":"src","title":"SgrB2 NIRCam catalog","n":464489,
 "build":"20261010T120000Z-1a2b3c4d",
 "order":{"kind":"lod","seed":20261010,"levels":[32768,65536,131072,262144,464489],"sort":"tile-morton"},
 "chunk_rows":1048576,
 "ids":{"file":"ids.2b7e11aa.bin","enc":"str","chunks":null,"alias_file":"aliases.88aa01ff.json"},
 "pos":{"enc":"i16off","step_mas":8.0,"tile_refs":[[266.83,-28.39],[266.84,-28.38]],
        "files":{"tile":"pos/tile.aa11bb22.bin","dra":"pos/dra.cc33dd44.bin","ddec":"pos/ddec.ee55ff66.bin"}},
 "bbox":{"ra":[266.76,266.90],"dec":[-28.45,-28.31]},
 "cols":[
  {"key":"f410m","label":"F410M","unit":"mag","role":"mag","filter":"JWST/NIRCam.F410M","err":"e_f410m",
   "enc":"u16q","lo":10.000,"hi":75.534,"step":0.001,"clip":"none","shuffle":true,
   "file":"cols/f410m.7c3a91d0.bin","center":18.21,"scale":1.31,"nvalid":455012,
   "quantiles":[10.9, 11.3, "... 101 values (0..100 %) ..."],"limits":null,"group":"nircam","invert":true},
  {"key":"e_f410m","role":"error","enc":"u8log","lo":0.001,"hi":1.0,"file":"cols/e_f410m.0b1c2d3e.bin"},
  {"key":"cls","role":"category","enc":"cat8","levels":["star","YSO","red"],"colors":{"YSO":"#e05a47"},
   "default_off":[],"file":"cols/cls.44aa0912.bin"},
  {"key":"has_spec","role":"flag","enc":"bits","file":"cols/has_spec.19fe22c0.bin"}],
 "virtual":[{"key":"f182m-f410m","label":"F182M − F410M","unit":"mag","group":"colors"}],
 "groups":[{"key":"nircam","label":"NIRCam mags","dims":["f115w","f150w","f182m","f410m"]}],
 "presets":[{"key":"ccd1","label":"F182M−F212N vs F212N−F410M","kind":"axes","x":"f182m-f212n","y":"f212n-f410m"},
            {"key":"pca","label":"PCA","kind":"frame","dims":["..."],"F":[0.1,"..."]},
            {"key":"yso","label":"YSO preselect","kind":"selection","sel":"r:eyJvcHMiOls..."}],
 "density":[{"key":"ccd1","x":"f182m-f212n","y":"f212n-f410m","xr":[-1,6],"yr":[-1,5],
             "nx":256,"ny":256,"enc":"u16log","lo":1,"hi":40211,"file":"density/ccd1.5ae0c1b2.bin"}],
 "rows":{"chunk":4096,"files":["rows/r0000.9a0b1c2d.bin","..."],"cols":["id","ra","dec","f410m","..."]},
 "manifold":{"dims":["f115w","..."],"default_frame":"pca","fade_width":0.12},
 "links":{"records":{"spec":"../spec/index.json"},"images":"../img/images.json"},
 "provenance":{"inputs":[{"path":"/orange/...","sha256":"...","mtime":"2026-10-01T12:00:00Z","size":123}],
               "legacy_modules":[{"name":"SgrB2_Portal_MapCCD","path":"/orange/...","sha256":"..."}],
               "adapter":"sgrb2@<git sha of portalkit>","portalkit":"1.0.0+g<sha>","tier":"B",
               "built":"2026-10-10T12:00:00Z","catalogs":[{"name":"...","header":{"...":"..."}}]},
 "caveats":["F2550W fluxes are beam-crowded in W51 IRS2."],
 "budget":{"first_paint_bytes":1834112,"total_bytes":38822001}}
```

Column metadata (`cols[]`) fields:
- **Required:** `key, role, enc, file|chunk_files`.
- **Optional:** `label, unit, filter, err, group, note, log, invert, fmt, lo, hi, step, clip, shuffle, center, scale, quantiles, nvalid, levels, colors, default_off, limits:{file,count}, validity`.
- `center` is the median.
- `scale` is 1.4826·MAD, with std as fallback, and 1.0 if both are 0. This is the GC `dim_stats` rule.
- `quantiles` holds 101 values at 0..100 %. It is present for numeric columns.
- For `log: true` columns, center, scale and quantiles are computed in log10 space, using the GC `log_dim_stats` 0.1/99.9 rule for lo/hi.

### 5.3 Column encodings

| `enc` | Bytes/row | Missing | Encode (Python, `bundle/codec.py`) | Decode (JS, `data/codec.js`) |
|---|---|---|---|---|
| `u16q` | 2 | code 0 | `step=(hi-lo)/65534` unless a fixed `step` is given (then `hi=lo+65534*step`); `code=1+rint((x-lo)/step)` | `lo+(code-1)*step`; GPU: `A=65535*step`, `B=lo-step` on normalized `val=code/65535` — identical to GC `loader_lite` A/B when center=0, scale=1 |
| `u16log` | 2 | code 0 (and x ≤ 0 unless a limit kind) | as `u16q` on `log10(x)` with `lo,hi` in log10 | `10**(lo+(code-1)*step)` |
| `u8q` | 1 | 0 | `step=(hi-lo)/254`; `1+rint((x-lo)/step)` | `lo+(code-1)*step` |
| `u8log` | 1 | 0 | GC `log_quantize`: `1+rint((log10 x-log10 lo)/(log10 hi-log10 lo)*254)` | `10**(log10 lo+(v-1)/254*(log10 hi-log10 lo))` |
| `cat8` | 1 | 255 | index into `levels` (≤ 255 levels) | `levels[code]` |
| `cat16` | 2 | 65535 | index into `levels` | |
| `bits` | 1/8 | not representable | packed LSB-first, `ceil(n/8)` bytes | `(b[i>>3]>>(i&7))&1` |
| `f32` | 4 | NaN | float32; 4-plane byte shuffle when `shuffle:true` | |
| `u32` | 4 | 0xFFFFFFFF | uint32; 4-plane shuffle | |
| `u32lin` | 4 | 0xFFFFFFFF | positions for N ≤ 2^17: `rint((x-lo)/(hi-lo)*4294967294)`; `lo,hi` = bbox | `lo+code*(hi-lo)/4294967294` |
| `i16off` | 2+2+2 | dra=ddec=-32768 | GC format: `tile` (u16, shuffled) indexes `tile_refs`; `dra=rint((ra-ra0)*cos(dec0)*3.6e6/step_mas)`, `ddec=rint((dec-dec0)*3.6e6/step_mas)`, i16 2-plane shuffled | `ra0+dra*s/cos(dec0)`, `dec0+ddec*s`, `s=step_mas/3.6e6` (GC `radec`) |
| `str` | var | empty string | `u32 n`, `u32 offsets[n+1]`, UTF-8 blob | `TextDecoder` slice |
| `sparse` | var | — | `u32 m`, `u32 idx[m]` ascending, `u8 kind[m]` | kinds: 1 upper limit, 2 lower limit, 3 clipped-low, 4 clipped-high, 5 flagged |

Shuffle: the u16/i16 2-plane form puts all low bytes first, then all high bytes. This is the verbatim GC `shuffle_u16`/`shuffle_i16`. The f32/u32 form uses four planes.

`enc: "auto"` resolution, in `bundle/catalog.py`:

| role | encoding |
|---|---|
| `mag` | `u16q` with fixed `step=0.001` |
| `flux` | `u16log` |
| `error` | `u8log` |
| `value` | `u16q` (default), or `f32` when the adapter sets `exact_gpu=False` and the dynamic range exceeds 1e4 |
| `category` | `cat8`, or `cat16` above 255 levels |
| `flag` | `bits` |
| `ra`/`dec` | `u32lin` when N ≤ 131072, else `i16off` |
| `text`/`url`/`image` | row chunks only; also `str` when the role is `id` |

Quantization `clip` modes, recorded per column:
- `none` (default). `lo`/`hi` are the finite min/max, with `lo` rounded down to a multiple of `step` when `step` is fixed. If the span exceeds 65534·step, the builder widens `step`, writes a warning to the build report, and records the actual `step`.
- `saturate`. `lo`/`hi` come from adapter-given quantiles. Values beyond them are clamped, and kinds 3/4 are written to the column's sparse limits table.
- `drop`. Values outside `[lo, hi]` become missing (code 0). This is GC `quantize` behaviour, used only for GC compatibility.

Limit kinds from legacy manifold `k` codes (`u`, `l`) are written to the sparse table with kinds 1 and 2. The value column holds the limit value.

### 5.4 Row order and LOD

- If N ≤ 131072 (`LOD_MIN_N`), the input order is kept (`order.kind = "input"`), so lists read naturally.
- If N > 131072, `order.kind = "lod"`. The builder takes a seeded permutation (numpy `default_rng(seed)`) and cuts it into levels with cumulative prefix sizes `32768·2^k` (`LOD_BASE`), ending at N. Every prefix is a uniform random subsample.
  - Inside each level, rows are sorted by `(tile, morton16(ra, dec))`, where tile is the `i16off` tile. This gives sky locality for `skyWindow` and better gzip.
  - The manifest lists the cumulative `levels`.
- When N > 131072 (`LOD_MIN_N`), every column and the ids are split at `chunk_starts = [0, LOD_BASE, LOD_BASE+CHUNK_ROWS, LOD_BASE+2·CHUNK_ROWS, ...]` (`CHUNK_ROWS = 1048576`), recorded in the manifest, one file per entry (`chunk_files`, `ids.chunks`). Chunk 0 (the head chunk) is the LOD base sample of 32768 rows used for first paint and motion. pos stays a single file. Catalogs with N ≤ 131072 keep single files (IMPLEMENTATION_NOTES 98; docs/bundle.md).
- `ids` and row chunks follow stored order. `aliases.json` and the id search map to stored row indices. The original input index is stored as column `_src_index` (`u32`) whenever the order is not `input`.

### 5.5 Exact rows

Row chunks (`rows/rNNNN.bin`) are gz JSON arrays of objects, 4096 rows per chunk, in stored order. They hold full-precision values for every column with `exact: true`, plus text/url/image fields. Their size is reported in the budget.

- Written by default when N ≤ 1,000,000 (`EXACT_ROWS_MAX_N`).
- Above that, written only if `CatalogSpec.exact_rows=True`.

Detail panes and hover cards read one row with `client.record(i)` (IMPLEMENTATION_NOTES 99). The worker loads only the chunk that holds row i: the row chunk when the catalog has row chunks, else the column chunks of the quantized fallback (for a single-file column that chunk is the whole column). A record reads no ids file when the row chunk carries the id. It loads pos (one whole-catalog file) only when the row lacks positions and the read wants them: before first paint only with `{pos: true}`, as the detail focus read and the sky highlight do; after boot calls `client.allowPositions()` the client default is `pos: true` (section 11, docs/js.md). On a catalog with row chunks, the user stored columns the row chunk omits (`exact: false`) are merged from their quantized column chunks and listed in the record's `_qkeys`; internal columns (keys starting with `_`, e.g. `_src_index`) are never merged. A row chunk keeps the positions it carries under the configured ra/dec names; ra/dec come from pos only when the row chunk carries none (ra/dec declared `exact: false`). Quantized values (every column of a fallback row, and the `_qkeys` columns) show `fmt.stepDigits(step)` decimals and a "±step/2" tooltip; row-chunk values show full precision. The hover card reads on the app-level record channel `'hover:card'`.

### 5.6 Record sets and shards (spectra, 2D spectra, products, SED curves)

```
data/<set>/index.json
data/<set>/s<000>.<h8>.bin        shard: concatenated independent gzip members (layout "shard")
data/<set>/r/<h8>.bin             one gzip member per file (layout "file")
```

`index.json`:

```json
{"schema":"pk-records/1","kind":"pks1","layout":"shard","catalog":"src","key":"id",
 "n":165,"shards":["s000.1a2b3c4d.bin","s001.5e6f7a8b.bin"],
 "entries":{"6927_10038":[0,0,18234],"6927_10041":[0,18234,20110]},
 "grids":{"g235m":{"kind":"uniform","w0":1.66,"dw":0.000356,"n":3700}},
 "variants":["flux","norm","normcm"],"unit_w":"um","unit_f":"Jy"}
```

- `entries` maps a catalog id to `[shard, offset, length]`. For layout `file` it maps to `"r/<h8>.bin"`.
- The packer (`bundle/records.py`) appends gzip members in catalog stored order. It starts a new shard when the current one would exceed `SHARD_TARGET = 262144` bytes. A single member larger than `SHARD_MAX = 524288` bytes goes into layout `file`, with the shard index listing it as a string path. The two layouts can mix within one index.
- The client fetches whole shards (no Range header). It keeps compressed shard bytes in an LRU of `SHARD_LRU_BYTES = 16 MiB` and decoded records in an LRU of `RECORD_LRU = 64` entries.
- Every request carries a token. A response for a superseded focus is dropped, which fixes the out-of-order fetches seen in the 2640 specviewer.

`json-legacy` mode: `{"kind":"json-legacy","dir":"<url>","pattern":"{id}.json"}`. This mode is accepted only when the build tier is A or B and the portal has not passed G2. `portalkit check --gate G2` fails while any `json-legacy` set remains.

Foreign sets (WPE6, §4.3) have no files in this portal: the ref's `base` points at the other portal's `data/`, and the index, shards and `r/` files are read from `<base><key>/`. A catalog manifest's `links.records` entry for a foreign set is the ref URL (relative ones gain `../../`, since the manifest sits two levels below the page).

### 5.7 `pks1` record (1D spectrum; also `sedcurve`)

After gunzip:

```
offset 0   4 bytes  ASCII "PKS1"
offset 4   u32      header_len (multiple of 4; JSON padded with spaces)
offset 8   header JSON (UTF-8)
offset 8+header_len  binary section; every array starts at a 4-byte aligned offset relative to the binary section
```

Header:

```json
{"id":"6927_10038","n":7400,
 "wave":{"kind":"uniform","w0":1.66,"dw":0.000356} | {"kind":"array","off":0,"dtype":"f32"} | {"kind":"shared","grid":"g235m"},
 "unit_w":"um",
 "channels":[{"name":"flux","unit":"Jy","enc":"f32","off":29600},
             {"name":"err","unit":"Jy","enc":"u16log","lo":-6.0,"hi":-1.0,"off":59200},
             {"name":"norm","unit":"","enc":"u16q","lo":0.0,"hi":3.2,"off":74000}],
 "segments":[[0,3700,"G235H"],[3700,7400,"G395H"]],
 "mask_runs":[[120,180,1],[5000,5100,2]],
 "mask_bits":{"1":"quality","2":"CO extrapolation"},
 "lines":[{"name":"H2 S(1)","w":2.1218,"kind":"emission","detected":true}],
 "meta":{"grating":"G235H","snr":42.1}}
```

- Channels with `enc` `u16q`/`u16log` follow §5.3 (code 0 = missing). `f32` uses NaN for missing.
- `sedcurve` is a pks1 file with `u16log` flux channels, which gives compact Robitaille SED curves. A `sedcurve` record may carry any non-empty channel set (WPE6; for example `SPHEREx` and `IRS` legend channels); a plain `pks1` record needs a `flux` channel. Without a `flux` unit the index `unit_f` comes from the first channel that is not an error channel.

### 5.8 `pki1` record (2D spectrum) and PNG mode

```
"PKI1", u32 header_len, header JSON, binary
header: {"id","nx","ny","enc":"u8"|"u16","off":0,
         "stretch":{"kind":"asinh"|"linear","lo":-1e-3,"hi":2e-2,"a":0.1},
         "wave":{...as pks1, along x...},"y":{"kind":"uniform","y0":-0.5,"dy":0.1,"unit":"arcsec"},
         "segments":[[x0,x1,"G235H"]]}
```

Values are the stretched image quantized to `0..255` (u8) or `1..65535` (u16, 0 = missing). The client applies the colormap. PNG mode puts no binary in the index entry: `{"png":"<url>","extent":{"w":[w0,w1],"y":[y0,y1]}}`. The W51 `spectra2d/*.png` files use PNG mode.

### 5.9 Pre-binned density

A density grid file holds `nx*ny` u16 values (2-plane shuffled, gz). Counts are log-scaled with `u16log` semantics over `[lo,hi]`, and 0 means empty. It is built for every `axes` preset whose dataset has N > `T0_MAX` (3000), and for each context dataset preset. Uses:
- first paint while columns stream;
- tier F (no WebGL2);
- background layers under foreground points: a context catalog's count grid (no `stat`) whose `x`/`y` equal the scatter pane's linear axes is drawn as the backdrop as it is, read with the foreground grid and before any column; only without a matching grid are the context columns binned in the worker (IMPLEMENTATION_NOTES 122).

### 5.10 Other JSON documents

| File | Schema id | Shape |
|---|---|---|
| `images.json` | `pk-images/1` | `{roles:[{key,label}], items:[{id, row?, roles:{role:{url,w?,h?,thumb?}}, caption?, tags:{facet:value}}], facets:[{key,label,values:[..]}], presets:[{key,label,facets:{k:[v]}}], catalog?:dsId}`; ≤ 4 MB gz (else items split into `images.<k>.<h8>.json` pages listed under `pages`) |
| `hips.json` | `pk-hips/1` | `{base:[{id,name,url,maxOrder?,format?,default?}], overlays:[{id,name,url,opacity,group?,default?}], fits:[{id,name,url,auth,ra,dec,size_deg}], rungs?:[{id,label,url,r_um?,g_um?,b_um?,desc}], bookmarks?:[{name,ra,dec,fov}], target:{ra,dec,fov}}` |
| `overlays.json` | `pk-overlays/1` | `{layers:[{id,name,space:"sky"|"dim",kind:"polygons"|"circles"|"markers"|"polyline"|"rings"|"boxes"|"vector"|"track", style:{color,width,dash,shape,opacity,fill}, default:bool, items:[...]}]}`. Sky items: `{poly:[[ra,dec],...]}`, `{ra,dec,r_arcsec}`, `{ra,dec,shape,label}`. Dim items: `{x:expr,y:expr,pts:[[x,y],...]}` (2D-only) or `{dims:{key:[v...]}}` (projected through any frame, faded by the weight on uncovered dims) |
| `narrative.json` | `pk-narrative/1` | `{beats:[{id,html,view:{pane,sky?:{ra,dec,fov},layers_on:[],layers_off:[],highlight?:{ds,ids:[..]},axes?:{x,y},suppress_view?:bool}}]}`; html produced by `markdown_lite` |
| `templates/index.<h8>.json` (res) | `pk-templates/1` | `{meta, grids:{prism:{file},medium:{file},high:{file}}, templates:[{key,teff,logg,label,files:{grid:url}}], laws:{k31:{grid:url},k55:{grid:url}}}`; each template file is pks1 with `wave.kind="shared"`, ~20 kB |
| `filters.<h8>.json` (res) | `pk-filters/1` | `{filters:{key:{label,instrument,wl_um,weff_um,zp_jy,system,trans?:url,a_over_av:{"G23_3.1":x,"G23_5.5":y}}}}` |
| `extinction.<h8>.json` (res) | `pk-extinction/1` | `{law:"G23", rv:[3.1,5.5], curve:{"3.1":{w:[..],k:[..]},"5.5":{...}}}` |
| `iso.<h8>.json` | `pk-isochrones/1` | `{tracks:[{label,age_myr,dist_pc,feh,mass:[..],mags:{filterKey:[..]}}]}` |
| `features.<h8>.json` (res) | `pk-features/1` | `{families:{ice:[{name,w_um,label,kind}],...}}` |
| `products` record (json) | `products/1` | `{items:[{label,url,kind:"fits"|"png"|"mast"|"link",size?,auth:"public"|"secure"}]}` |

`pk-hips/1` base, overlay and rung `url` values are either a HiPS URL (absolute, or relative to the page) or a CDS HiPS identifier such as `CDS/P/DSS2/color` or `P/2MASS/color`. Identifiers pass through to Aladin unresolved (`isSurveyId` / `surveyUrl` in `web/js/sky/aladin.js`); bundle paths under `data/` or `_res/` are always resolved against the page.

Shared resources (`templates`, `filters`, `extinction`, `features`) are published once to `/orange/adamginsburg/web/public/portalkit-res/` (URL `https://data.rc.ufl.edu/pub/adamginsburg/portalkit-res/`) by `portalkit publish-res`. Content-hashed names make them immutable. A portal built with `--vendor-assets` copies them into `<target>/res/`. This fixes the W51 archive_colors page, which referenced a missing `../templates`.

### 5.11 Size budgets (checked by `portalkit check`; hard failures unless marked soft)

| Item | Budget |
|---|---|
| page HTML | ≤ 200,000 B (target ≤ 80 kB) |
| embedded page.json | ≤ 65,536 B |
| manifest.json | ≤ 262,144 B |
| first-paint bytes (manifest + first count density grid, or both axis columns in full without a grid, + the list's first screen: `rows/r0000`, or the head chunk of every stored column plus the head ids chunk; positions excluded; rule in docs/bundle.md section 8). A context catalog (`context: true`, §7.6) is never listed, so it counts its manifest plus every density grid (one backdrop per axes preset), or both axis columns in full when it has no grid; no row chunk and no head chunk (`first_paint_files(man, context=True)`, WPE1) | ≤ 2 MB for N ≤ 1M; ≤ 6 MB for N ≤ 5M |
| single column/chunk file | ≤ 16 MiB |
| shard | ≤ 524,288 B (single oversized record → layout file, soft warning above 2 MiB) |
| row chunk | ≤ 524,288 B (soft) |
| images.json | ≤ 4 MiB gz-equivalent (soft; else paginated) |
| total per page (all lazily reachable data) | reported, not limited |

Worked example (N = 5M, 11 mags). Each `u16q` column is 10 MB raw and about 6–7 MB as 5 chunk files. First paint fetches the manifest, a 256² density grid (about 60 kB) and chunk c000 of two axis columns (about 2.8 MB). That totals about 3 MB, within budget.


---

## 6. JavaScript modules **[FROZEN APIs]**

These rules apply to every JS module:
- ES modules, loaded from `{assets}js/...`.
- No globals, except `window.__pk` when the URL has `?pktest=1`.
- Pure modules import nothing from the DOM, so `node --test` can load them. The pure modules are: `constants, store, selection, hash, fmt, legacyhash, codec, inflate, expr, records (decode part), templates, filters, extinction, isochrones, fold, tour, axes, ops/*`.
- Types below are JSDoc-style. `ds` means a dataset id string. `i` means a stored row index.

### 6.1 Boot sequence (`core/boot.js`)

`export async function boot(): Promise<App>` is invoked by `shell.html`. It does the following, in order:

1. Parse `#pk-page`. The theme is already set by an inline 12-line script in the shell, before CSS loads, so there is no flash.
2. Create the store (§6.2) with defaults ← `page.state` ← carry (§6.5) ← legacy hash mapping (`legacyhash.js`, only when `page.legacy.grammar` is set) ← canonical hash.
3. Create the hub (`data/hub.js`) and start fetching manifests for every catalog that a non-collapsed pane uses.
4. Check runtime capabilities: WebGL2, `EXT_color_buffer_float`, DecompressionStream, `navigator.deviceMemory`. Compute the effective tier per pane (§7). Disable capabilities that cannot run, with a reason, for example `density_rendering` falling back to tier F.
5. Mount the layout. Each pane module is imported lazily through `core/registry.js`.
6. Set `document.body.dataset.caps` to the space-separated list of enabled capabilities and `dataset.tier` per pane.
7. Bind the hash, keys and theme. Install `__pk` when `?pktest=1`.
8. Emit `ready`.

`App = {page, store, hub, layout, selections: Map<ds, Selection>, caps: Set<string>, emit, on}`.

### 6.2 Store contract (`core/store.js`)

```js
export function createStore(initial: object): Store
interface Store {
  get(key: string): any
  snapshot(): object                                  // frozen shallow copy
  set(patch: object, opts?: {source?: string, history?: boolean}): void
        // shallow-merges per key; a key whose new value is Object.is-equal or (plain object/array) shallow-equal is ignored
  on(keys: string[] | '*', fn: (changed: Set<string>, store: Store, sources: Set<string>) => void,
     opts?: {sync?: boolean}): () => void
        // default: coalesced, delivered once per requestAnimationFrame (setTimeout(0) under node); sync for hash/tests
  emit(topic: string, payload?: any): void            // transient events, synchronous
  onEvent(topic: string, fn: (payload) => void): () => void
  batch(fn: () => void): void                         // one notification for all sets inside
}
```

Panes never call each other. They read and write store keys, call the DataClient, and listen to events. Every `set` carries a `source` (pane id), so a pane can ignore its own echo.

**Store keys** (H = serialized to the URL hash; W = writers; R = typical readers):

| Key | Type | H | W | R |
|---|---|---|---|---|
| `focus` | `{ds, i, id}` or null | `id=` | list, scatter, manifold, sky, gallery, hash | detail, spectrum1d, spectrum2d, sed, sky, all plots (highlight) |
| `hover` | `{ds, i}` or null | – | plots, list, sky, gallery | hover card, plots (ring), sky (ring) |
| `sel:<ds>` | `{version, count}` | `sel=` (primary ds) | Selection (via selection.js) | every pane on `<ds>` |
| `vis:<ds>` | `{version, count}` (visibility mask from filters) | – | query op result handler | all panes on `<ds>` |
| `filters:<ds>` | `{cats:{key:[offLevels]}, ranges:{expr:[lo,hi]}, flags:{key:0|1}, snr:{min, scope:"projection"|"all"}, text?:string}` | `f=` | chips, controls, list search, hist brush | hub → query op |
| `axes:<pane>` | `{x:expr, y:expr, xlog, ylog, xinv, yinv, xr?:[lo,hi], yr?:[lo,hi]}` | `<pane>.x=`, `<pane>.y=`, `<pane>.xr=`, `<pane>.yr=` (zoom range, `lo,hi`; legacy `spherex-colours` `zoom=` maps onto it, §6.4) | axis_picker, presets, hash | scatter, overlays |
| `frame:<pane>` | `{preset?:string, F?:number[] (2·D), dims:string[]}` | `<pane>.frame=` (preset name or `b64:` f32) | manifold, tour, controls | manifold, compass |
| `layers:<pane>` | `{raster?:string[], ovl?:string[]}` (ids of that pane's configured `options.raster`/`options.overlays` layers currently shown; absent = all shown) | `<pane>.raster=`, `<pane>.ovl=` (comma list of ids, `-` for none) | legend, layer panel, legacy `spherex-colours` `yoo=` (§6.4, `ovl` only) | scatter, overlays (layer visibility) |
| `color` | `{by: key|expr|null, cmap, lo?, hi?}` | `c=` | controls, legend | all plots, sky markers |
| `av`, `rv`, `dm` | number | `av=`, `rv=`, `dm=` | controls (rAF-throttled) | sed, spectrum1d templates, overlays, isochrones |
| `speed` | number > 0 (tour / playback speed multiplier, default 1) | `speed=` | wavelength and tour controls, legacy `?speed=` / `speed=` (§6.4) | wavelength, manifold tour |
| `template` | `{key, grid, scale:"auto"|number, bands?:string[]}` or null | `tpl=` | controls | spectrum1d, sed |
| `features` | string[] (atlas families on) | `feat=` | controls | spectrum1d, sed |
| `spec` | `{pinned:string[] (≤ 8), variant, xr?:[w0,w1], yr?:[..], segments?:{label:bool}, vel?:km/s}` | `spec=`, `wl=` | spectrum1d, controls | spectrum1d, spectrum2d (x-link) |
| `sky` | `{ra, dec, fov, base, overlays:[{id, opacity}], fits:[id], beatView?, beatOvl?}` | `sky=ra,dec,fov`, `base=`, `ovl=` (not for a part a beat implies, §6.4) | sky, narrative | sky, wavelength, narrative |
| `rung` | string | `rung=` | wavelength | wavelength |
| `beat` | string | `beat=` | narrative | narrative, sticky pane |
| `role` | string | `role=` | gallery | gallery, lightbox |
| `facets` | `{key:[values]}` | `fac=` | gallery facets | gallery |
| `lightbox` | `{set, index}` or null | `lb=` | gallery, detail, lightbox | lightbox |
| `annot:<ds>` | `{version}` | – | annotate | gallery badges, export |
| `theme` | `"auto"|"light"|"dark"` | – (localStorage) | theme | all canvases (repaint) |
| `layout` | `{popout:{pane:"half"|"full"}}` | `view=` | layout | layout |
| `tier:<pane>` | `"T0"|"T1"|"T2"|"F"` | – | panes | testhooks, status chip |

**Events** (`store.emit`):

| Topic | Payload | Emitted by |
|---|---|---|
| `ready` | `{ms}` | boot |
| `data:progress` | `{ds, loaded, total, phase}` | hub |
| `data:error` | `{ds, url, status, message}` | hub (also shows a toast) |
| `brush:preview` | `{pane, shape}` | brushui |
| `brush:commit` | `{pane, ds, shape, op, count}` | panes, after `Selection.apply` |
| `toast` | `{level:"info"|"warn"|"error", text, ms?}` | anyone |
| `pane:resize` | `{pane, w, h}` | layout |
| `nav:carry` | `{href}` | nav (before navigation) |
| `selection:saved` | `{ds, name}` | export/chips |

**Beat markers in `sky`** (WPD2, IMPLEMENTATION_NOTES 119). `sky.beatView` and `sky.beatOvl` (hash.js `BEAT_VIEW`, `BEAT_OVL`) hold the id of the beat whose view, respectively overlay set, the value holds; `''` stands for the first beat of a page opened without `beat=`. They are store-only: never written to the hash. Writers:
- the narrative pane marks the parts a beat applies (`beatSky`); a view is marked only when loading `beat=` alone reproduces it (a beat without its own sky view, or with `suppress_view`, leaves the current view unmarked unless it equals the page default; a highlight-only beat (goto) marks the view when its fov equals the page default, because the sky pane centres on the highlight);
- the sky pane records the origin of each viewer move (`store`, `beat`, `explicit`, `user`). A settle after a `store` goto writes nothing; a `beat` settle writes the view marked with that beat (only when the narrative marked it for the same beat); `explicit` and `user` settles (pan, zoom, a goto with a new view) drop `beatView`. A layer-panel change of the overlay set or opacities drops `beatOvl`; a base-only change keeps it;
- `hashToPatch` marks the parts a `beat=` hash leaves out, and an explicit `sky=`/`ovl=` (including `-`) clears the marker.

### 6.3 Selection (`core/selection.js`)

```js
export class Selection {
  constructor(n: number, opts?: {maxUndo?: number, undoBytes?: number})
       // depth = min(UNDO_MAX=20, floor(UNDO_BYTES=64 MiB / n)), at least 1
  readonly mask: Uint8Array          // 0/1 per stored row
  readonly version: number           // ++ on every change
  readonly count: number
  apply(hit: Uint8Array, op: 'replace'|'add'|'subtract'|'intersect', drawable?: Uint8Array, recipeOp?: RecipeOp): void
  setIndices(idx: ArrayLike<number>, op, recipeOp?): void
  clear(): void; invert(drawable?: Uint8Array): void
  undo(): boolean; redo(): boolean; get canUndo(): boolean; get canRedo(): boolean
  indices(): Int32Array
  recipe(): RecipeOp[]               // ops since last replace/clear
  serialize(idOf: (i)=>string): string        // 'ids:..' | 'bits:..' | 'r:..' | ''
  static parse(s: string): {kind:'ids', ids:string[]} | {kind:'bits', bytes:Uint8Array} | {kind:'r', ops:RecipeOp[]}
}
export function opFromEvent(e: {shiftKey, altKey}): 'replace'|'add'|'subtract'|'intersect'
       // none -> replace, shift -> add, alt -> subtract, shift+alt -> intersect
```

Semantics **[FROZEN]**:
- `hit` covers only rows that are drawable in the acting view. A row is drawable when it is visible after filters and has finite values for the view's axes, or a visible projection weight for a manifold view.
  - replace: `sel = hit`.
  - add: `sel |= hit`.
  - subtract: `sel &= ~hit`.
  - intersect: `sel = (sel & hit) | (sel & ~drawable)`. Rows the view cannot show keep their state.
- Subtracting down to an empty selection leaves `count = 0`. Panes keep showing all points, so nothing disappears.
- Keys: `Esc` cancels an in-progress brush, `x` clears, `i` inverts within drawable, `k` "keep only" (sets the filter `ids` to the selection), `Ctrl/Cmd+Z` undoes, `Ctrl/Cmd+Shift+Z` redoes.

`RecipeOp = {op, kind:'rect'|'circle'|'lasso'|'ids'|'filter'|'preset', view:{ds, x?:expr, y?:expr, frame?:{preset?|F}}, shape?:{x0,y0,x1,y1}|{cx,cy,r}|{pts:[[x,y]...]}, ids?:string[]}`. Shapes are in data coordinates of the view.

Hash serialization (`sel=`, IMPLEMENTATION_NOTES 92 and 107). Two candidate forms:
- Recipe form: `ids:<comma-separated url-encoded ids>` when the recipe is a single `replace` ids op with at most 300 ids (`IDS_IN_HASH_MAX`; `ids:` is the compact spelling of that recipe), else `r:<base64url(deflate-raw(JSON(recipe)))>`. The recipe is replayed on load, after the columns it needs are ensured. The recipe form is empty when the recipe is incomplete (e.g. an index-only ids op whose ids are not cached yet).
- Bitmask form: `bits:<base64url(deflate-raw(mask packed LSB-first))>`, available when `n ≤ 200000` (`BITS_IN_HASH_MAX_N`).

Choice:
1. When a `bits:` form exists, the recipe form competes with it only when the recipe is view-independent (`recipeExact`: every op is an `ids`, `preset`, or `filter` op that carries its filters). The shorter of the two in the URL (length after hash encoding) wins; a tie goes to the recipe. Brush recipes (`rect`/`circle`/`lasso`) depend on the filters in force at load time, so brush selections on catalogs up to 200 000 rows are written as `bits:`.
2. With no `bits:` form (`n > 200000`), the recipe form is used, brush ops included.
3. When the recipe form is empty but would be allowed to compete (no `bits:` form, or an exact recipe whose ids cannot all be spelled yet) and at most 300 rows are selected with every id known, `ids:` lists the selected rows; it then competes with `bits:` as in 1.
4. With neither form, `sel` is empty and the selection stays out of the URL (boot records it as omitted for history navigation).

The choice depends only on the selection (its recipe and mask), so a selection restored from the URL writes the same form before and after a reload. If a hash would exceed 8000 characters (`HASH_MAX_CHARS`), `sel` is dropped from the hash and the "copy link" action shows a warning.

### 6.4 Hash (`core/hash.js`) and legacy grammars (`core/legacyhash.js`)

```js
export function parseHash(h: string): Record<string,string>
export function formatHash(obj: Record<string,string>): string     // stable key order (see table §6.2)
export function bindHash(store: Store, opts: {primaryDs: string, keys: string[]}): () => void
      // store -> hash: replaceState, debounced 250 ms; focus changes use pushState (Back returns to prior source)
      // hash -> store: on 'hashchange' and 'popstate'
// legacyhash.js
export const GRAMMARS: Record<string, (loc: {hash:string, search:string}) => Record<string,string> | null>
export function mapLegacy(grammars: string[], loc): {hash: Record<string,string>, consumedSearch: string[]}
```

**Beat rule** (WPD2, IMPLEMENTATION_NOTES 119): a part of `sky` whose marker (§6.2) equals `state.beat` (or `''` while no beat is set) is implied by the beat. `skyImplied(state)` reports `{view, ovl}`, and `stateToHash` writes neither `sky=` nor `ovl=` for an implied part. So:
- a view or overlay set that a beat applied (including the sky pane's centring on a highlight-only beat) is implied by `beat=` and not written: `#beat=b3` reloads to b3's view;
- a user pan, zoom or layer toggle after the beat drops the marker and writes `sky=` / `ovl=` next to `beat=`;
- `#sky=..&beat=b3` keeps the hash view: at load and on hash navigation the narrative pane computes `explicitSky(sky, pageSky, {beatSet})` and keeps the explicit parts over the beat's, with no highlight goto for a kept view;
- `narrative:highlight` carries `goto: true` only for a beat with a highlight, no `view.sky`, no `suppress_view`, and no kept explicit view (docs/js.md).

Grammar names (WP13 implements all of them; WP12 tests one fixture per name; WPE6 registers `spherex-colours` in `schema.GRAMMARS`, WPE5 implements its parser in `legacyhash.js`, and `legacy/hashmap.py` mirrors it). The canonical hash key `speed` (a float, the tour speed multiplier) is the target of the legacy `?speed=` and `speed=` forms (WPE6, item D). `page.legacy.cmd_pane` (WPE9, item E): gc-query's legacy CMD axes (`a`,`b`,`m` -- x is the colour `dim[a]-dim[b]`, y is `dim[m]`) map onto `<cmd_pane>.x`/`<cmd_pane>.y` of that scatter pane instead of `<manifold>.frame` when the page names one; `p=ccd` always still maps onto `<manifold>.frame` (the CCD panel has no plain-axes equivalent). Every pane-suffixed canonical key (`<pane>.x`, `.y`, `.frame`, `.xr`, `.yr`, `.raster`, `.ovl`, §6.2) round-trips through a canonical hash unchanged, whether or not the grammar in use writes that particular suffix.

| Grammar | Legacy form | Maps to |
|---|---|---|
| `bare-name` | `#name` | `id=` via id/alias |
| `kv-view` | `#view=..&name=..` | `id=`, `view=` |
| `gc-query` | `?a=&b=&m=&p=&x1=&x2=&y1=&y2=&cb=&cd=&cm=&cl=&ch=&g=&rv=&fa=&sel=` and treasury `y,g,rv,fa` | `manifold.frame` (or `<cmd_pane>.x`/`.y` for `a`/`b`/`m` when `page.legacy.cmd_pane` is set, item E above), `axes`, `color`, `rv`, `filters`, `sel=r:` |
| `wavelength` | `#rung=&ra=&dec=&fov=`, `?speed=` or `#speed=` | `rung=`, `sky=`, `speed=` |
| `spherex-colours` | `?ax=&ay=&bx=&by=&c=&band=&cmd=&zoom=&yoo=&hide=` (legacy SPHEREx `colours.html` query state) | CCD panel axes (`ax`,`ay`), second panel axes (`bx`,`by`), `color` (`c`), CMD band (`band`), CMD panel on/off (`cmd`), `<pane>.xr`/`<pane>.yr` (`zoom`), the `yoo_*` overlay layers that are on as `<pane>.ovl` (`yoo`, comma list of ids, `layers:<pane>.ovl`, §6.2), hidden levels of the colour-by category (`hide`); dims pass through `dim_alias` |
| `beat` | `#beat=` | `beat=` |
| `gallery-index` | `#<index>` or `#img=` | `lb=` |
| `w51-rowid` | `#W51-000123` | alias lookup |
| `specviewer` | `#<src>` written on load | `id=` |

### 6.5 Cross-page carry (`core/carry.js`)

```js
export function stashCarry(portal: string, payload: {ds, focusId?, sel?: string}): void   // sessionStorage 'pk:carry:<portal>', try/catch
export function takeCarry(portal: string): payload | null                                 // read-once, max age 60 s
export function broadcastFocus(portal: string, id: string): void                          // BroadcastChannel 'pk:<portal>' if available
export function onBroadcastFocus(portal, fn): () => void
```

`ui/nav.js` calls `stashCarry` on clicks to sibling pages. On boot, a carry fills `focus` and `sel` only when the target page has the same dataset id.

### 6.6 Data layer (`data/*.js`, `workers/*`)

```js
// data/hub.js
export function createHub(page, store): Hub
interface Hub {
  catalog(ds: string): Promise<DataClient>
  records(ds: string): Promise<RecordStore>
  json(ds: string): Promise<any>                      // images, hips, overlays, narrative, filters, ...
  resolve(url: string): URL                           // {assets}/{res}/relative
  stats(): {fetches, bytes, decodeMs}
}

// data/client.js  — main-thread facade over workers/data.worker.js (one worker per catalog dataset)
export class DataClient {
  static open(url: string, opts?: {signal?: AbortSignal}): Promise<DataClient>
  readonly n: number; readonly manifest: object
  col(key: string): ColMeta | null; isVirtual(key): boolean; baseKeys(expr: string): string[]
  ensure(keys: string[], opts?: {prefix?: number, signal?}): Promise<void>     // fetch+decode in worker; emits data:progress
  gpuColumns(keys: string[], opts?: {prefix?}): Promise<{n, raw: Uint16Array[], A: Float64Array, B: Float64Array, missing0: true}>
        // u16q columns transferred as copies for Renderer.setData; caller drops refs after upload
  values(expr: string, opts?: {prefix?}): Promise<Float32Array>               // decoded / evaluated (NaN missing)
  project(spec: {terms: {w: Float64Array, keys: string[]}[2], c: [number, number], fade?: Float32Array},
          opts?: {prefix?, token?}): Promise<{x: Float32Array, y: Float32Array, vis: Uint8Array}>
        // worker sparse mat-vec over the folded support (§7.3); vis per GC kernel semantics
  pick(view: ViewRef, px: number, py: number, rPx?: number): Promise<number>  // -1 if none; grid built per settled projection
  brush(view: ViewRef, shape: Shape): Promise<{hit: Uint8Array, drawable: Uint8Array, count: number}>
  query(filters: FilterSpec): Promise<{vis: Uint8Array, count: number}>
  hist(expr: string, bins: number, opts?: {range?, mask?: 'sel'|'vis'|null}): Promise<{edges: Float64Array, counts: Uint32Array}>
  bin2d(view: ViewRef, nx: number, ny: number, opts?: {mask?}): Promise<{counts: Uint32Array, nx, ny, xr, yr}>
  rows(i0: number, i1: number): Promise<object[]>                              // exact row chunks (or quantized fallback)
  record(i: number): Promise<object>                                          // one row; LRU + token
  id(i: number): Promise<string>; find(text: string, opts?: {limit?: 50}): Promise<number[]>   // id, alias, substring, "ra dec [r]" cone (sexagesimal or decimal)
  crossmatch(ra: Float64Array, dec: Float64Array, rArcsec: number): Promise<Int32Array>        // dec-sorted binary search
  skyWindow(win: {ra, dec, fov, cap?: number, mask?: 'sel'|'vis'|null}): Promise<{idx: Int32Array, total: number, sampled: boolean}>
        // cap default SKY_MARKER_CAP=20000; uniform subsample via LOD order prefix within window
  exportCSV(opts: {mask: 'sel'|'vis'|'all', cols: string[] | 'all', exact?: boolean}): Promise<Blob>
  selection(): Selection                                                      // the page Selection for this ds
}
type ViewRef = {pane: string, kind: 'xy', x: string, y: string, xlog?, ylog?} | {pane, kind: 'proj', token: number}
type Shape = {kind:'rect', x0,y0,x1,y1} | {kind:'circle', cx,cy,r} | {kind:'lasso', pts:[number,number][]}   // data coords

// data/records.js
export class RecordStore {
  static open(url: string): Promise<RecordStore>
  has(id: string): boolean
  get(id: string, opts?: {signal?, token?}): Promise<Spectrum | Image2D | object | null>
}
export function decodePKS1(bytes: Uint8Array): Spectrum   // {id, wave: Float64Array, channels: {name: Float32Array}, segments, maskRuns, lines, meta}
export function decodePKI1(bytes: Uint8Array): Image2D    // {nx, ny, data: Uint8Array|Uint16Array, stretch, wave, y}

// data/codec.js (pure)
export function isGzip(b: Uint8Array): boolean
export async function gunzip(b: Uint8Array): Promise<Uint8Array>        // DecompressionStream or inflate.js
export function unshuffle(b: Uint8Array, width: 2|4): Uint8Array
export function decodeColumn(meta: ColMeta, bytes: Uint8Array): {codes?: Uint16Array|Uint8Array|Uint32Array, values?: Float32Array, strings?: string[], bits?: Uint8Array}
export function dequant(meta, code: number): number
export function decodeSparse(bytes): {idx: Uint32Array, kind: Uint8Array}
export function decodeStr(bytes): {n, offsets: Uint32Array, blob: Uint8Array, get(i): string}
export function decodePos(posMeta, files): {ra: Float64Array, dec: Float64Array}

// data/expr.js (pure)
export function compile(expr: string): {w: Record<string, number>, c: number, keys: string[]}
export function evaluate(compiled, cols: Record<string, Float32Array>, n): Float32Array

// data/filters.js, extinction.js, templates.js, isochrones.js (pure)
export function magToJy(mag, zpJy): number; export function jyToMag(jy, zpJy): number
export function sedPoints(row: object, filters: FilterSet, cols: ColMeta[]): {wl, fnu, err, kind:'det'|'ul'|'ll'}[]
export function aLambda(filterKey, av, rv, ext): number
export function vector(xExpr, yExpr, av, rv, filters): {dx, dy}            // CCD/CMD reddening vector
export function redden(spec: Spectrum, law: {w, k}, av): Float32Array      // 10^(-0.4 k A_V)
export function scaleTo(model, obsPoints, bands?: string[]): number         // median ratio over chosen bands
export function trackXY(track, xExpr, yExpr, av, rv, dm, filters): {x: Float32Array, y: Float32Array}
```

Worker RPC (`workers/rpc.js`): messages `{id, op, args}` → `{id, ok, result | error}`. ArrayBuffers are transferred. Ops are `open, ensure, values, project, pick, brush, query, hist, bin2d, rows, find, crossmatch, skyWindow, exportCSV, gpuColumns, cancel`, implemented in `workers/ops/*.js`. `project.js` reuses `vendor/gc/workers/kernel.js` `projectRange` when the support is ≤ 28 stored u16q columns. Above that it runs the sparse loop over the support. `select.js` holds the lasso even-odd test with a bbox prefilter. Brushes act only on drawable rows, which fixes the Brick2221 hidden-point bug.

### 6.7 Layout (`layout/layout.js`)

```js
export function mountLayout(root: HTMLElement, tree: LayoutNode, ctx: {app, registry}): Layout
interface Layout { pane(id): PaneHandle; popout(id, mode: 'half'|'full'|null): void; collapse(id, on: boolean): void;
                   reveal(id): void /* expands + scrolls into view, used by caps that need a collapsed pane */; destroy(): void }
```

### 6.8 Pane module interface (all `panes/*.js`)

```js
export const meta = {type: 'scatter', caps: ['cmd_ccd_scatter', ...], needs: ['catalog']};
export function mount(el: HTMLElement, ctx: PaneContext): PaneHandle
type PaneContext = {id, config /* leaf.options */, dataset /* ds id */, app, store, hub, layout, theme: () => Tokens}
type PaneHandle = {resize(w, h, dpr): void, destroy(): void, onVisible?(v: boolean): void,
                   snapshot?(): Promise<Blob> /* SVG for T0, PNG otherwise */, stats?(): object}
```

Panes must:
- tolerate a dataset that is still loading by showing a skeleton;
- re-render on `theme` changes;
- read colours from CSS tokens (§12);
- stop work when not visible.

### 6.9 Rendering helpers

```js
// gl/xyrenderer.js
export class XYRenderer {
  constructor(canvas: HTMLCanvasElement, opts?: {onLost?, onRestored?, budget: Budget})
  static supported(): boolean
  setXY(x: Float32Array, y: Float32Array, vis: Uint8Array, opts?: {count?: number}): void   // count = LOD prefix
  setCats(cats: Uint8Array[] /* ≤4 slots */): void
  setColorValues(v: Float32Array | null): void
  setSelection(mask: Uint8Array): void
  resize(cssW, cssH, dpr): void
  render(p: {camera: {cx, cy, scale, aspect, width, height}, mode: 'markers'|'density',
             color: {mode: 0|1|2, lo, hi, cmap, slot, colors}, filters: {masks: Uint32Array(4)},
             pointSize, gain, ref, selection: {show: boolean, dim: number}, neutral: [r,g,b], selColor: [r,g,b], count?}): void
  destroy(): void
}
// gl/xyshaders.js: export const XY_VS (inputs: a_x, a_y (float), a_vis (float 0..1), a_cats (uvec4), a_sel (float), a_val (float);
//   outputs v_w, v_c, v_has, v_sel, flat v_code — exactly the varyings consumed by vendor ACCUM_FS_CONT / ACCUM_FS_CAT),
//   export const MARKER_VS, MARKER_FS (alpha-blended round sprites, selection on top)
// gl/budget.js
export class Budget { constructor(totalBytes = GPU_BUDGET_BYTES); request(owner: string, bytes: number): boolean; release(owner): void;
                      plan(panes: {id, cssW, cssH, density: boolean}[], vertexBytes: number): {dpr: number, allowed: string[]} }
// plot/axes.js (pure)
export function scale(kind: 'lin'|'log', d0, d1, r0, r1, invert?): {f(v), inv(px), ticks(n): {v, label}[]}
export function decadeTicks(d0, d1): {v, label, major}[]
// plot/canvas2d.js
export function drawPoints(ctx2d, view, pts: {x, y, color, shape, hollow, size, limit?: 'u'|'l'|null, ex?, ey?, label?}[]): void
export function toSVG(view, layers): string
// plot/overlays.js
export function drawOverlays(ctx2d, view, layers: OverlayLayer[], state: {av, rv, dm, frame?}): void   // isochrones, vectors, ref lines, connectors (pairs), cut lines
// plot/brushui.js
export function attachBrush(el, view, {modes: ['rect','circle','lasso'], onPreview, onCommit(shape, op)}): () => void
// math/fold.js (pure)
export function fold(frameRows: {[dimKey]: number}[2], dims: {key, center, scale, expr?: {w, c}}[]): {terms: {w: Float64Array, keys: string[]}[2], c: [number, number], support: string[]}
// math/tour.js (pure; wraps vendor frame.geodesic / blendFrames)
export class Tour { constructor(D, opts); to(F, ms); drift(group: number[]); step(dtMs): Float64Array; playAxis(dim) }
// math/compass.js
export function drawCompass(ctx2d, F, dims, opts): void
```

### 6.10 Pane options (validated by `config.py`)

| Pane | Options (defaults) |
|---|---|
| `list` | `columns:[keys]` (id + 3 auto), `sort`, `search:true`, `virtual:true`, `rowHeight:24`, `showSelectionOnly:false` |
| `detail` | `template:"auto"|name`, `products:dsId?`, `images:dsId?`, `bars:[keys]` (probability bars), `prose:[keys]`, `links:true`, `clickToAxis:true` (click→Y, shift→X, alt→colour) |
| `scatter` | `x, y` (expr), `xlog, ylog`, `color`, `density:"auto"|"on"|"off"`, `background:dsId?`, `raster:[{url, extent}]` (legacy PNG layers), `overlays:[layerId]`, `pairs:{x2, y2}?` (connectors), `hollow_if:expr?`, `labels:key?`, `errorbars:bool`, `presets:true`, `grid:[[x,y],...]?` (small multiples) |
| `manifold` | `dims:[keys]|"group:<g>"`, `frame:"pca"|preset`, `trails:false`, `compass:true`, `tour:true`, `background:dsId?` |
| `hist` | `expr`, `bins:64`, `brush:true` (range filter → `filters.ranges`) |
| `sky` | `hips:dsId`, `overlays:[ids]`, `markers:"auto"|"off"`, `brush:true`, `rings:[arcsec...]?`, `fits:[ids]?` |
| `wavelength` | `hips:dsId`, `speed:1`, `markers:dsId?` |
| `narrative` | `narrative:dsId`, `sticky:"sky"|"scatter"` |
| `spectrum1d` | `records:dsId`, `variants`, `features:[families]`, `templates:dsId?`, `bands:[{label, w0, w1}]`, `filters:true` (transmission shading), `pin:8`, `logx:false`, `logy:false`, `mode:"spectrum"|"sed"` |
| `spectrum2d` | `records:dsId`, `cmap:"gray"`, `link1d:true` |
| `sed` | `units:"fnu"|"nufnu"`, `models:["template","blackbody","sedcurve:<ds>"]`, `dialog:true` |
| `gallery` | `images:dsId`, `roles:[..]`, `cardSize:220`, `annotate:{codes:[..], key:"checklist"}?`, `sort:[keys]` |
| `table` | `columns`, `source:"rows"|"json:<url>"` |
| `readme` | `html` or `url` |
| `landing` | `cards:"auto"`, `counts:true`, `search:true` |

### 6.11 Vendored GC modules and the theme patch

`web/js/vendor/gc/**` are byte-for-byte copies of `/orange/adamginsburg/jwst/brick/analysis/gc_fields_manifold_assets/js/{gl,config,math,workers,app/pick.js}`. Their sha256 values are recorded in `VENDORED.md`, and `test_constants_sync.py` re-hashes them. The only patch is in `gl/renderer.js` `_composite`, at legacy lines 498, 502 and 503:

```js
const rgb = (color.colors && color.colors[c]) || p.neutral || NEUTRAL_RGB;   // line 498
gl.uniform3fv(u.u_neutral, p.neutral || NEUTRAL_RGB);                       // line 502
gl.uniform3fv(u.u_selColor, p.selColor || SELECTION_RGB);                   // line 503
```

### 6.12 Constants **[FROZEN; `portalkit/constants.py` == `web/js/core/constants.js`]**

```
T0_MAX=3000  T1_MAX=300000  LOD_MIN_N=131072  LOD_BASE=32768  CHUNK_ROWS=1048576
MOTION_PREFIX=1500000  SETTLE_MS=150  SEMANTIC_ZOOM_MARKERS=50000  SKY_MARKER_CAP=20000
MAXD_GPU=28  DENSITY_PANES_MAX=2  GPU_BUDGET_BYTES=268435456  MRT_BYTES_PER_PX=48
SHARD_TARGET=262144  SHARD_MAX=524288  SHARD_LRU_BYTES=16777216  RECORD_LRU=64
ROW_CHUNK=4096  EXACT_ROWS_MAX_N=1000000
UNDO_MAX=20  UNDO_BYTES=67108864  IDS_IN_HASH_MAX=300  BITS_IN_HASH_MAX_N=200000  HASH_MAX_CHARS=8000
PAGE_HTML_MAX=200000  PAGE_JSON_MAX=65536  MANIFEST_MAX=262144
FIRST_PAINT_MAX_1M=2097152  FIRST_PAINT_MAX_5M=6291456  COL_FILE_MAX=16777216
MAG_STEP=0.001  FADE_WIDTH=0.12  MOBILE_BREAK_PX=900  HASH_DEBOUNCE_MS=250  ALADIN_TIMEOUT_MS=8000
```


---

## 7. Rendering tiers

### 7.1 Tier table

The tier is chosen per pane from N (total rows of the pane's dataset), WebGL2 support and the GPU budget. Hysteresis is ±10 % of the boundary.

| Tier | N | Scatter / manifold | Sky | List | Data |
|---|---|---|---|---|---|
| **T0** | ≤ 3,000 | `plot/canvas2d.js`, CPU projection on the main thread. Per-point styling: shapes, hollow rule, limit arrows, error bars, labels, pair connectors. SVG export. | all markers, one `A.catalog` per category level | full DOM list | all needed columns eager |
| **T1** | 3,000–300,000 | `XYRenderer` in markers mode. Worker sparse projection on settle; during motion, projection on the LOD prefix (manifold) or on the main thread for ≤ 50k. Canvas2d overlay layer for axes, overlays, brush UI and the focus ring. | `skyWindow` viewport markers, capped at 20k (uniform subsample flagged in the UI) | virtualized | eager for axes in view; lazy otherwise |
| **T2** | > 300,000 | Density. If folded support ≤ 28 (`MAXD_GPU`): vendored GC `Renderer` with GPU projection; motion uses the `MOTION_PREFIX` LOD prefix, full N after `SETTLE_MS`. If support > 28: `XYRenderer` density mode on worker-projected XY (prefix during motion, full on settle). Semantic zoom: when worker `hist` reports < 50k rows in view, the pane switches to markers for in-view rows. | density of the selection or vis; markers via `skyWindow` when ≤ 20k in view | virtualized; default view "selection" | chunked streaming; pre-binned density first paint |
| **F** | any, no WebGL2 or GPU budget refused | pre-binned grid (preset axes) or worker `bin2d` (512²) drawn with `putImageData`; markers when ≤ 3k in view | as T1 | virtualized | as T1 |

### 7.2 Projection (scatter)

A scatter view is a manifold whose 2-frame has one-hot rows over the folded expressions. Both scatter and manifold use `math/fold.js`.

### 7.3 Folding virtual dims (Robitaille, WD2, colours generally)

Given the frame `F` (2×D over the displayed dims, which may be virtual), where each dim d has `expr_d = Σ_b w_db·base_b + c_d` and standardization `(v−center_d)/scale_d`:

```
x = Σ_d F[0,d]·(expr_d − center_d)/scale_d = Σ_b W[0,b]·base_b + K0,
W[r,b] = Σ_d F[r,d]·w_db/scale_d,  K_r = Σ_d F[r,d]·(c_d − center_d)/scale_d
```

- `K` is folded into the camera offset (`camera.cx += K0·scale`, and likewise for y), so the GPU path needs no constant term.
- The distance-modulus slider `dm` adds `dm` to every `mag` base column. It folds into `K` as `Σ_b W[r,b]·dm` over mag bases.
- Support is the set of bases with `|W| > 1e-9` in either row.
- GPU path (support ≤ 28): the Renderer receives the support columns via `gpuColumns` with `A`/`B` from §5.3 (raw units, center 0, scale 1) and frame `W`.
  - Missing-data fade uses `fadeFactors(W)` per base column (vendored `frame.js`, `FADE_WIDTH=0.12`).
  - Missing data in a base column hides or fades the point, matching the GC semantics.
- CPU/worker path (support > 28): `DataClient.project` evaluates the sparse sums, with the same `vis` semantics as `kernel.projectRange`, and feeds `XYRenderer`.
  - Robitaille (54 mags + 21 physical stored, 172 colours virtual): a typical 2-frame touches 75–83 base columns.
  - WD2 manifold: 164 dims.

### 7.4 Overlays in dim space

Overlay layers with `space:"dim"` (isochrone tracks, extinction vectors, reference loci such as Yoo W51, the 1:1 line, cut lines) carry values per dim key. They are projected through the current folded frame, so they move with tours. Each overlay's opacity is `1 − Σ(weight on dims the overlay does not cover)/Σ(total weight)`, clamped to [0, 1]. When A_V or R_V changes, tracks are recomputed (`data/isochrones.js` `trackXY`) in a rAF-throttled handler.

### 7.5 GPU memory budget (`gl/budget.js`)

- Density render targets cost `MRT_BYTES_PER_PX = 48` B per device pixel. Vertex buffers cost `n × (2·support + 4 + 1)` B for the GC Renderer and `n × (4+4+1+4+1+4)` B for XYRenderer.
- `Budget.plan` is called on mount and on resize:
  1. It allows at most `DENSITY_PANES_MAX = 2` density panes. Further density panes get tier F.
  2. If the total exceeds `GPU_BUDGET_BYTES = 256 MiB`, it lowers DPR from 2 to 1.5 to 1.
  3. If the total still exceeds the budget, it caps the vertex count to the largest LOD prefix that fits, and shows "showing X of N" in the pane status.
- `webglcontextlost` is handled through the Renderer `onLost`/`onRestored` callbacks. The pane re-requests its budget.

### 7.6 Context datasets

A catalog with `context: true`, such as the W51 53k background for colorcolor or an observed catalog under Robitaille models, is drawn as a GL (or tier F) density layer under the foreground on any scatter or manifold pane whose expressions resolve in it. It never takes part in selection. On a scatter pane the layer is the catalog's pre-binned count grid when one matches the pane's linear axes (§5.9); a page then reads no context column for it. Without a matching grid the pane bins the context columns in the worker at settle time.

---

## 8. Capabilities: resolver and module map

### 8.1 Resolver (`portalkit/caps.py`)

```python
def resolve(page: PageSpec, datasets: dict[str, DatasetSpec], manifests: dict[str, dict]) -> CapsResult
@dataclass
class CapsResult:
    enabled: list[str]            # sorted
    disabled: dict[str, str]      # cap -> reason (opt-outs and unmet runtime needs)
    flags: list[str]              # data flags derived below (for the build report)
    added_panes: list[dict]       # collapsed panes the resolver inserted into the layout
```

Data flags are derived from the datasets attached to the page. An adapter may attach more datasets than the archetype needs, which is how galleries gain mini-scatter and sky panes:

| Flag | Condition |
|---|---|
| `multi` | ≥ 2 panes share a dataset |
| `cat` | a catalog dataset with an id column |
| `cats` | a `category` column |
| `flags` | a `flag` column or an `error` column (S/N tighten) |
| `num2` | ≥ 2 numeric columns (stored or virtual) |
| `mags2` | ≥ 2 `mag`/`flux` columns |
| `num3` | ≥ 3 numeric columns |
| `mani` | archetype `manifold`, or `PageSpec.manifold=True` |
| `mags3f` | ≥ 3 `mag`/`flux` columns with filter metadata |
| `sky` | ra/dec, or a hips dataset |
| `skycat` | catalog with ra/dec on a page with a sky pane |
| `img` | images dataset or any image role |
| `sedimg` | image role `sed` |
| `spec` | records dataset of kind `pks1`, or `json-legacy` spectra |
| `spec2d` | records dataset of kind `pki1`, or PNG-mode 2D |
| `iso` | isochrones dataset |
| `fits` | `hips.fits` non-empty |
| `dens` | catalog N > 3,000 or any context dataset |
| `items` | gallery archetype |
| `annot` | gallery `annotate` option |
| `search` | landing with ≥ 1 searchable catalog in the portal |

Rules (union):

```
ALWAYS : keyboard_nav cross_page_nav responsive_mobile deep_link_hash readme_provenance
         dark_light_theme collapsible_panels lazy_data_loading
multi  : resizable_panels linked_selection
cat    : detail_panel source_list_search hover_preview selection_export
search : source_list_search
cats   : class_coloring filter_toggles
flags  : filter_toggles
num2   : brush_rect brush_circle brush_lasso brush_set_ops selection_export
mags2  : cmd_ccd_scatter
num3   : selectable_axes
mani   : manifold_embedding selectable_axes
mags3f : live_sed_viewer extinction_vectors_on_ccd av_slider model_template_overlay labeled_features_overlay
sky    : sky_map_aladin cdn_fallback hips_layer_stacking    (default all-sky HiPS set when none given)
skycat : catalog_markers_on_sky
img    : lightbox
sedimg : static_sed_png
spec   : spectrum_viewer_1d labeled_features_overlay model_template_overlay av_slider
spec2d : spectrum_2d_viewer
iso    : isochrones_or_tracks
fits   : fits_overlay
dens   : density_rendering
items  : source_list_search filter_toggles hover_preview
annot  : selection_export
```

An adapter opts out with `PageSpec.caps_disable = {cap: reason}`. Reasons appear in `page.json`, in the build report and in the help dialog. The resolver adds the panes needed for the enabled capabilities, collapsed, for example a mini sky pane on a gallery with ra/dec. At runtime, boot may move a capability to `disabled` with a runtime reason (for example `density_rendering: "WebGL2 unavailable; pre-binned fallback"`). Tier F still counts as providing `density_rendering`.

### 8.2 Capability → module map (39)

| Capability | Provider modules | Best legacy reference to port behaviour from |
|---|---|---|
| keyboard_nav | core/keys.js (arrows walk the *filtered visible* list; Esc; `?` help; space tour; x/i/k selection keys) | spectra_candidate template:458-465 |
| cross_page_nav | ui/nav.js + portal.json (built pages only) + carry.js | best_impl.json `cross_page_nav` |
| responsive_mobile | layout/layout.js tab mode < 900 px; css/layout.css | — |
| lightbox | ui/lightbox.js (prev/next over the filtered set, arrows/Esc/swipe, captions, `lb=` hash, cleared on close) | WD2_Portal_SEDGallery.py:152-194 |
| detail_panel | panes/detail.js (property table, figure blocks, products, bars, prose, links) | best_impl.json `detail_panel` |
| filter_toggles | ui/chips.js (category chips with counts, flags, S/N tighten scoped to the active projection), panes/hist.js range brush | W51_Portal_CatalogManifold.py:470-492 |
| deep_link_hash | core/hash.js + core/legacyhash.js | best_impl.json `deep_link_hash` |
| hover_preview | ui/hover.js (edge-flipping card, mini SED/spectrum/PNG, rebuilt only on index change) | best_impl.json `hover_preview` |
| static_sed_png | panes/detail.js figure block + panes/gallery.js role `sed` | — |
| cmd_ccd_scatter | panes/scatter.js + auto presets (short-minus-long ordering, GC `swapToShortLong`) | best_impl.json `cmd_ccd_scatter` |
| class_coloring | ui/legend.js + category colours (manifest `colors`, else token palette) | — |
| source_list_search | panes/list.js + DataClient.find (id, alias, substring, sexagesimal/decimal cone) | SPHEREx browse cone search |
| lazy_data_loading | data/hub.js, client.js, records.js | — |
| brush_rect / brush_circle / brush_lasso | plot/brushui.js + workers/ops/select.js on scatter, manifold, sky (sky/skybrush.js) | W51_Portal_MapCCD.py:851-919 |
| brush_set_ops | core/selection.js (modifiers, undo, recipes) | W51_Portal_MapCCD.py:851-919; W51 catalog manifold:1268-1295 |
| selectable_axes | ui/axis_picker.js (grouped optgroups with counts, prev/next/play, typed expressions, ranges) | best_impl.json `selectable_axes` |
| readme_provenance | panes/readme.js + panes/landing.js + manifest provenance/caveats | best_impl.json `readme_provenance` |
| sky_map_aladin | panes/sky.js + sky/aladin.js (single instance per page, command queue before ready) | best_impl.json `sky_map_aladin` |
| catalog_markers_on_sky | sky/markers.js (one `A.catalog` per category; skyWindow LOD ≤ 20k) | NIRSpec_6927_AladinMap (6927 map:775-796) |
| cdn_fallback | core/loadscript.js + sky/aladin.js (CDN → mirror → error note) | best_impl.json `cdn_fallback` |
| hips_layer_stacking | sky/layers.js (base + overlays with opacity; "overlays off/defaults"; feature detection) | best_impl.json `hips_layer_stacking` |
| fits_overlay | sky/layers.js `createImageFITS` lazily with loading state; URL resolved through exposure at build | Sickle_MIRI_SEDPortal.py:990-1024 |
| density_rendering | vendor/gc/gl/renderer.js, gl/xyrenderer.js, workers/ops/raster.js, pre-binned grids | GC renderer |
| linked_selection | store `sel:<ds>` + Selection shared across panes; carry/broadcast across pages | — |
| manifold_embedding | panes/manifold.js + math/fold.js + math/tour.js + math/compass.js | GC app; 6927 template:262-357 |
| av_slider | ui/controls.js (`av`, `rv`, `dm`, rAF-throttled) | best_impl.json `av_slider` |
| extinction_vectors_on_ccd | plot/overlays.js + data/extinction.js | best_impl.json |
| isochrones_or_tracks | plot/overlays.js + data/isochrones.js (reprojected per A_V; drawn whenever both axes are linear in tracked filters) | WD2_Portal_Manifold.py:876-928 |
| model_template_overlay | data/templates.js + panes/spectrum1d.js + panes/sed.js (grid choice, R_V 3.1/5.5, configurable normalization bands) | Brick2221_F1500W_SEDPortal.py |
| labeled_features_overlay | spec/features.js (atlases, collision dropping, `requires_coverage` dashing) | JWST_Archive_ColorPortal.py:123-242; Brick2221:122-135 |
| spectrum_viewer_1d | panes/spectrum1d.js (pan, wheel, box, pinch, autoY on visible range, variants, segments, bands, pin ≤ 8, rubber band, velocity offset, mask shading, filter transmission) | NIRSpec_6927_SpecViewer.py:288-327,533-615 |
| spectrum_2d_viewer | panes/spectrum2d.js (pki1 or PNG mode, x linked to 1D) | — |
| live_sed_viewer | panes/sed.js (log-log, Fν/νFν, limits, templates, blackbody, sedcurve models, fullscreen dialog with nearest-point tooltip) | best_impl.json `live_sed_viewer` |
| collapsible_panels | layout/layout.js fold headers | — |
| resizable_panels | layout/layout.js splitters | Brick2221_F1500W_SEDPortal.py:704-767 |
| dark_light_theme | core/theme.js + css/tokens.css; canvases read tokens | SgrB2_Portal_Index.py:47-54 |
| selection_export | ui/export.js (CSV/JSON/ID list/DS9 region; import by id → alias → 0.3″ position) | best_impl.json `selection_export` |

### 8.3 Family extras → modules

| Extra (inventory) | Family | Provider |
|---|---|---|
| GTC slit polygons, tier marker shapes, artifact rings | w51 | overlays.json `polygons`/`markers` with `shape`; sky/overlays.js |
| catalog provenance tooltip from FITS headers | w51 | manifest `provenance.catalogs` → panes/readme.js + legend tooltip |
| asymmetric HiPS cross-fade, rung rail, play/speed | w51, wd2, extras | panes/wavelength.js (W51_Portal_Wavelength.py:239-264) |
| hexbin PNG backgrounds with extents | w51, wd2, 2640, 6927 | scatter `raster` option → plot/canvas2d raster layer |
| binned-statistic background (median r_wd2) | 2640 | bundle/density.py `stat` grids + raster op |
| paired catalog-vs-synthetic markers with connectors | 2640 | scatter `pairs` → plot/overlays.js |
| histogram mag-cut / BD split brush | wd2, 2640 | panes/hist.js |
| preselect buttons (hasSpec, EWOCS-IX, clusters, YSO) | wd2, w51 | manifest presets `kind:"selection"` → ui/chips.js |
| SESHAT probability bars | sickle | detail `bars` |
| MSA shutters, dither footprints, acquisition boxes | 6927, 2640 | overlays.json polygons/boxes (cos(dec) aware) |
| click rings at configurable radii | 6927 refstar | sky `rings` option → sky/overlays.js |
| scrollytelling beats, text-to-sky highlights | 6927 msata | panes/narrative.js |
| per-grating FITS products table | 6927, 2640 | records kind json `products/1` → detail products block |
| band-coverage gating | archive | column `validity` bits |
| pin up to 8 spectra, velocity offset, transmission shading | archive, spherex | panes/spectrum1d.js |
| checklist annotate codes y/n/g/w/l/e | archive | ui/annotate.js (localStorage per viewer; CSV export; `portalkit archive-checklist-diff`) |
| distance-modulus slider | robitaille | store `dm` folded into camera offset (§7.3) |
| νFν/Fν toggle, confusion notes | spherex | panes/sed.js, text columns |
| 60-region go-to list | spherex | hips.json `bookmarks` |
| thumbnails for large PNGs | robitaille | `portalkit thumbs` (matplotlib extra) → images `thumb` |
| contact sheets with criteria tables | 6927 refstar | gallery + readme blocks |
| S/N tighten scoped to the active projection | w51 | ui/chips.js + query op `snr.scope="projection"` |
| red-clump preset action, feature caveats | gc | presets + manifest caveats |
| BB guide curve | wd2, 2640 | sed `models:["blackbody"]` |

---

## 9. Adapter contract

### 9.1 Python API (`portalkit/spec.py`) **[FROZEN]**

```python
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Protocol
import numpy as np
from astropy.table import Table

Role = Literal["id","ra","dec","mag","flux","error","color","value","category","flag","text","url","image","limit"]

@dataclass
class ColumnSpec:
    key: str
    role: Role = "value"
    label: str | None = None
    unit: str = ""
    filter: str | None = None          # filters registry key, e.g. "JWST/NIRCam.F410M"
    err: str | None = None
    limit: str | None = None           # sibling column with legacy codes 'v'/'u'/'l'/''
    validity: str | None = None        # bits column key
    enc: str = "auto"
    step: float | None = None
    clip: Literal["none","saturate","drop"] = "none"
    lo: float | None = None
    hi: float | None = None
    group: str | None = None
    note: str = ""
    log: bool = False
    invert: bool | None = None
    fmt: str | None = None             # python format spec for display, e.g. ".3f"
    levels: list[str] | None = None
    colors: dict[str, str] | None = None
    default_off: list[str] = field(default_factory=list)
    exact: bool = True                 # include in row chunks

@dataclass
class VirtualDim:   key: str; label: str | None = None; unit: str = ""; group: str | None = None
@dataclass
class Group:        key: str; label: str; dims: list[str]
@dataclass
class Preset:       key: str; label: str; kind: Literal["axes","frame","selection","filter"]; data: dict
@dataclass
class DensitySpec:  key: str; x: str; y: str; nx: int = 256; ny: int = 256; xr: tuple | None = None; yr: tuple | None = None; stat: str | None = None
@dataclass
class ManifoldSpec: dims: list[str]; frames: dict[str, list[float]] = field(default_factory=dict); default_frame: str = "pca"
@dataclass
class ForeignRef:   kind: Literal["gc-manifest-1"]; url_base: str; manifest_path: Path

@dataclass
class CatalogSpec:
    key: str
    table: Table | None                # None only with foreign
    id: str
    columns: list[ColumnSpec]
    ra: str | None = None
    dec: str | None = None
    aliases: dict[str, list[str]] | str | None = None   # id -> aliases, or column name holding '|'-joined aliases
    virtual: list[VirtualDim] = field(default_factory=list)
    groups: list[Group] = field(default_factory=list)
    presets: list[Preset] = field(default_factory=list)
    density: list[DensitySpec] = field(default_factory=list)
    manifold: ManifoldSpec | None = None
    exact_rows: bool | None = None     # None -> n <= EXACT_ROWS_MAX_N
    context: bool = False
    order: Literal["auto","input","lod"] = "auto"
    title: str = ""
    caveats: list[str] = field(default_factory=list)
    catalogs_provenance: list[dict] = field(default_factory=list)
    foreign: ForeignRef | None = None
    noun: tuple[str, str] | None = None   # landing-card nouns, e.g. ("model", "models") (§4.2)

@dataclass
class Spectrum:
    wave: np.ndarray                   # um, ascending within each segment
    channels: dict[str, np.ndarray]    # "flux" required; "err", variants optional
    units: dict[str, str] = field(default_factory=lambda: {"flux": "Jy"})
    segments: list[tuple[int, int, str]] = field(default_factory=list)
    mask_runs: list[tuple[int, int, int]] = field(default_factory=list)
    mask_bits: dict[int, str] = field(default_factory=dict)
    lines: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

@dataclass
class Image2D:      data: np.ndarray; wave: np.ndarray; y: np.ndarray; stretch: str = "asinh"; segments: list = field(default_factory=list)

@dataclass
class RecordSetSpec:
    key: str
    kind: Literal["pks1","pki1","json","sedcurve"]
    catalog: str
    ids: list[str]
    loader: Callable[[str], "Spectrum | Image2D | dict | None"]
    enc: dict[str, str] = field(default_factory=dict)  # channel -> "f32"|"u16q"|"u16log"
    grids: dict[str, np.ndarray] = field(default_factory=dict)
    png: Callable[[str], tuple[str, dict] | None] | None = None   # pki1 PNG mode: id -> (url, extent)
    layout: Literal["shard","file"] = "shard"
    legacy_json: tuple[str, str] | None = None         # (dir_url, pattern) Tier A/B only
    noun: tuple[str, str] | None = None   # landing-card nouns (§4.2)
    foreign: ForeignRecords | None = None # another portal's set; no shards written (§4.3, WPE6)

@dataclass
class ForeignRecords: base: str; key: str; id_column: str | None = None   # base ends in '/'

@dataclass
class ImageItem:    id: str; roles: dict[str, str]; caption: str = ""; tags: dict[str, str] = field(default_factory=dict); w: int | None = None; h: int | None = None
@dataclass
class ImageSetSpec:
    key: str; items: list[ImageItem]; roles: list[tuple[str, str]]
    facets: list[tuple[str, str]] = field(default_factory=list)
    presets: list[Preset] = field(default_factory=list)
    catalog: str | None = None; join: str | None = None   # join item.id to catalog id or alias
    noun: tuple[str, str] | None = None   # landing-card nouns (§4.2)

@dataclass
class TemplateSpec:   source: Path | None = None; extend_to_um: float | None = None   # default jwst_template_spectra.json
@dataclass
class FilterSetSpec:  keys: list[str]; extra: dict[str, dict] = field(default_factory=dict)
@dataclass
class ExtinctionSpec: law: str = "G23"; rv: tuple[float, ...] = (3.1, 5.5)
@dataclass
class Track:          label: str; age_myr: float; dist_pc: float; feh: float; mass: np.ndarray; mags: dict[str, np.ndarray]
@dataclass
class IsochroneSpec:  tracks: list[Track]
@dataclass
class HipsSpec:       base: list[dict]; overlays: list[dict] = field(default_factory=list); fits: list[dict] = field(default_factory=list); rungs: list[dict] | None = None; bookmarks: list[dict] | None = None; target: dict | None = None
@dataclass
class OverlaySpec:    layers: list[dict]
@dataclass
class NarrativeSpec:  beats: list[dict]                   # html via markdown_lite at build
@dataclass
class ReadmeSpec:     markdown: str | Path; blocks: list[dict] = field(default_factory=list)

DatasetSpec = (CatalogSpec | RecordSetSpec | ImageSetSpec | TemplateSpec | FilterSetSpec | ExtinctionSpec
               | IsochroneSpec | HipsSpec | OverlaySpec | NarrativeSpec | ReadmeSpec)

@dataclass
class LegacyRef:      files: list[str]; grammar: list[str] = field(default_factory=list); preset: str = ""
                      dim_alias: dict[str, str] = field(default_factory=dict)   # §4.1 legacy dim alias
@dataclass
class PageSpec:
    id: str
    archetype: Literal["landing","explorer","manifold","gallery","spectra","wavelength","narrative"]
    title: str
    datasets: list[str]
    layout: dict | None = None
    state: dict = field(default_factory=dict)
    caps_disable: dict[str, str] = field(default_factory=dict)
    legacy: LegacyRef | None = None
    links: dict[str, str] = field(default_factory=dict)
    manifold: bool = False
    subtitle: str = ""
    portal: str | None = None           # [[portal]] id when the family has several

class Adapter(Protocol):
    name: str
    tiers: tuple[str, ...]              # tiers implemented, e.g. ("A", "B")
    def datasets(self, ctx: "BuildContext") -> dict[str, DatasetSpec]: ...
    def pages(self, ctx: "BuildContext", ds: dict[str, DatasetSpec]) -> list[PageSpec]: ...
```

Helper constructors in `spec.py` (WP03):
- `catalog_from_rows(rows: list[dict], id, ra, dec, roles: dict[str, Role]) -> CatalogSpec`
- `join(a: CatalogSpec, b: CatalogSpec, on: str) -> CatalogSpec`
- `ImageSetSpec.from_rows(rows, roles: dict[str, str], key="img")`
- `standard_pages(ds, landing=True, explorer=[...], manifold=False, spectra=False, galleries=[...]) -> list[PageSpec]`

### 9.2 Tiers

1. **Tier A (bootstrap).** `legacy/extract.payload(html_path) -> dict` uses `json.JSONDecoder().raw_decode` at the first of `const DATA =`, `window.PORTAL =`, or `<script type="application/json" id=...>`. It raises `LegacyExtractError(ValueError)` if none are found. Manifold payloads go through `manifold_payload.convert(payload) -> (CatalogSpec, ManifoldSpec)`. This tier runs no science code. It is also the parity oracle in tests.
2. **Tier B (reuse).** Imports the legacy loader functions via `ctx.legacy.module(name)` (§9.3) and converts with the helpers. This becomes the production path after gate G2.
3. **Tier C (native).** New features, for example s2d FITS → pki1, MIST isochrones for SgrB2, larger Robitaille samples.

`ctx.tier` selects the tier. Adapters raise `TierUnavailable(RuntimeError)` for tiers they do not implement.

### 9.3 Context and the legacy import shim (`portalkit/context.py`, `legacy/importshim.py`)

```python
@dataclass
class BuildContext:
    family: str
    site: "SiteConfig"
    staging: Path                       # the only place the builder writes
    tier: Literal["A","B","C"]
    limit: int | None = None            # fixture mode
    seed: int = 20261010
    allow_tmp_inputs: bool = False
    cache: "Cache"                      # memo(key, fn, inputs=[paths]) -> value; npz/ecsv/pickle-free JSON under ~/.cache/portalkit/<family>/
    inputs: "InputRegistry"             # add(path) -> records sha256, mtime, size; rejects tmp paths unless allowed
    legacy: "LegacyImporter"
    log: Callable[[str], None] = print

class LegacyImporter:
    def module(self, name: str) -> "ModuleType"
        # imports from site.legacy_paths inside a temporary cwd; records sha256 of the source file;
        # fails with LegacyImportError(ImportError) if the import created files in cwd, or on ImportError/SyntaxError/AttributeError
    def shas(self) -> list[dict]
```

The cache key is `(key, [(path, mtime_ns, size) for inputs])`. Values are numpy arrays (`.npz`), astropy Tables (`.ecsv`) or JSON. Nothing is pickled.

Input registry and cache cost (WPE1). Neither the registry nor the cache key walks, hashes or stats a directory tree; the one exception is the bounded adoption of pre-WPE1 values in the last bullet:
- A file input is recorded with its sha256, which is cached in `<cache>/<family>/input_sha256.json` keyed by `(path, mtime_ns, size)`. An unchanged multi-GB file is hashed once.
- A directory input is recorded by its stamp: one `stat` of the directory and one top-level `scandir`. The record is `{kind:"dir", sha256: sha256 of the sorted entry names, mtime: directory mtime, size: 0, entries: entry count}`, and its cache-key entry is `[path, dir mtime_ns, entry count, names sha256]`. Adding, removing or renaming a top-level entry changes the stamp. A file rewritten in place, or a change inside a sub-directory, does not. An adapter whose value depends on such content registers those files (or the sub-directory) itself.
- Values memoised before this change used a full tree digest for directory inputs. `.meta.json` files written now carry `"v": 2`; pre-WPE1 ones have no `v`. After a miss of the current key with a directory input, `Cache.memo` lists the cache directory once (one `scandir` per `Cache`) and looks for adoption candidates: `.meta.json` entries of the same key that have neither `v` nor `alias_of`. Only when a candidate exists that has not already failed to match for the same scope and input paths does it compute the older key (`Cache.legacy_key_hash`, a full walk). If a value is stored under that key, the cache returns it and writes an alias `.meta.json` (with `alias_of`) under the current key that points at the old value file, so later runs hit the alias without a walk. If none matches, the candidates are recorded in `<key>.legacy-checked.json` under a digest of the scope and the input paths, and later misses in that context skip the walk. A cold cache, a new key, and a key whose entries were all written by this version cost no walk. Old files are never modified or deleted, so an existing multi-hour memo stays valid without recomputation. A tree that returns to the exact state of a candidate that already failed to match in the same context is recomputed, not adopted.

Served paths (WPE1). Rendered pages and every JSON document the build stages carry no absolute local path. One served file is outside this rule: deploy's ownership manifest `<target>/.portalkit-deploy.json` (§11.3) records the absolute `target` and the build's full `legacy_modules` paths, because `status` needs real paths for LEGACY-CHANGED; `check` scans only the staged tree and does not see it. Provenance input and legacy-module paths, and `CatalogSpec.catalogs_provenance`, are written as `<label>:<path relative to a declared root>` (`portalkit.build.PathLabeler`). The roots are each portal's `legacy_dir` (label `legacy_dir`), each `legacy_paths` entry (`legacy`), and the parent of the static prefix of every `inputs` pattern (`input`). The longest matching root wins. A path under no root becomes `file:<basename>`. `build_report.json` keeps the full paths and is never deployed (`deploy.NOT_DEPLOYED`, at any depth of the staged tree). `portalkit check` reports every staged `.html`/`.json` file other than `build_report.json` that contains `/orange/`, `/blue/` or `/home/` as an absolute path, or as a `file://` URL. This is a warning without a gate and at G1, and an error at G2. The build writes the shared-resource documents (a vendored `res/` inside the portal and the build's `_res/` tree) and non-default physics datasets (`data/res/<key>/`) through `portalkit.build.LabellingWriter`, which applies the same labels to every JSON document before it is hashed; `physics/templates.py` `load_source(spec, *, planck, labeler=None)` writes `source_label(path, labeler)` into `meta.source`: `labeler(path)` when a labeler is given (for example a `PathLabeler`, giving `<label>:<relpath>`), else `file:<basename>` (`file:jwst_template_spectra.json`), so no absolute path enters the document even before the writer labels it (WPE3). `templates.write(spec, dest, *, ledger, planck, labeler=None)` takes the labeler from a `LabellingWriter` `dest` (its callable `labeler` attribute) when none is passed, so a build stages the root label. Hits there follow the same gate rule. `PathLabeler.scrub` labels a string that is a local path, every local path embedded in a longer string (`LOCAL_PATH_RE`, trailing `.,;:)]}` kept outside the label), and mapping keys. `LOCAL_PATH_RE` matches `/orange/`, `/blue/` or `/home/` after a start, space, quote, `=`, `:` or `-`, and as `file://`; it does not match after a letter, digit, `_`, `.`, `~`, `%` or `/` (a URL path segment or a relative path).

Coupling mitigation:
- The sha of every imported module goes into the manifest provenance. `portalkit status` flags a changed sha as `LEGACY-CHANGED`.
- Contract tests (§9.6) run nightly in fixture mode.
- Each adapter keeps a `_fallback` section where a loader function may be copied verbatim, with a source sha comment, when an import breaks.

### 9.4 Build pipeline (`portalkit/build.py`)

```python
def build_family(family: str, *, tier: str | None = None, pages: list[str] | None = None, limit: int | None = None,
                 stage_root: Path | None = None, allow_tmp_inputs: bool = False, vendor_assets: bool = False,
                 jobs: int = 1) -> BuildResult
@dataclass
class BuildResult: build_id: str; staging: Path; pages: list[str]; report: dict; ok: bool
```

The pipeline runs these steps in order:
1. Load `sites/<family>.toml`.
2. Check inputs (exit 5 on tmp inputs).
3. Run `adapter.datasets` and then `adapter.pages`.
4. For each dataset, run the matching writer:
   - `bundle.catalog.write` (or `legacy.gc_manifest.write` for foreign data);
   - `bundle.records.write`;
   - `bundle.images.write`;
   - `physics.*`, or a reference to shared `res`.
5. For each page, run `caps.resolve` → `config.assemble` → `render.shell.write`.
6. Write `portal.json` and `build_report.json` (budgets, caps, disabled reasons, warnings, provenance).
7. Run `portalkit check`.

The tree is written under `<stage_root>/<family>/<build_id>/<portal_id>/`. `build_id` has the form `YYYYMMDDTHHMMSSZ-<8 hex of sha256(adapter sha + inputs)>`.

### 9.5 Family adapter files

Each adapter WP owns:
- `adapters/<family>.py`, which exports `ADAPTER`;
- `sites/<family>.toml`;
- `tests/adapters/test_<family>.py`;
- `tests/e2e/test_port_<family>.py`.

Target size is ≤ 400 lines per adapter, excluding `_fallback` copies.

### 9.6 Adapter contract tests (`pytest -m adapters`)

Each test runs with `limit=50`:
- (a) Importing the adapter creates no files. This is checked in a subprocess with a temporary cwd.
- (b) `datasets()` returns valid specs in under 120 s.
- (c) Ids are unique, and aliases are unique and disjoint from ids.
- (d) Every declared input exists and none lies under a tmp path.
- (e) A full fixture build passes `portalkit check`.
- (f) The Tier A vs Tier B parity check holds for the pages that have both: N, the id set, and numeric values within one quantization step (`|a−b| ≤ step`).
- (g) The resolved capabilities of each page are a superset of the legacy capabilities in `tests/data/inventory_caps.json`.


---

## 10. Builder CLI (`portalkit/cli.py`) **[FROZEN]**

```
portalkit build FAMILY [--tier A|B|C] [--pages p1,p2] [--limit N] [--stage-root DIR]
                       [--allow-tmp-inputs] [--vendor-assets] [--no-cache] [--jobs N]
portalkit check BUILD_DIR [--gate G1|G2] [--json]
portalkit caps FAMILY [--json]                         # capability matrix per page (dry: runs adapter in fixture mode)
portalkit serve BUILD_DIR [--port 8765] [--host 127.0.0.1] [--auth USER:PASS] [--content-encoding auto|none|gzip]
                          [--no-range] [--latency-ms 0] [--mount PREFIX=DIR]   # --mount is repeatable
portalkit deploy FAMILY BUILD_ID [--portal ID] [--target DIR] [--replace[=stub|inplace]] [--htaccess]
                                 [--dry-run] [--yes]
portalkit rollback FAMILY BUILD_ID [--portal ID]
portalkit verify FAMILY [--portal ID] [--http] [--auth-env PK_BASIC_AUTH]
portalkit status [FAMILY] [--json]
portalkit publish-assets [--version X.Y.Z] [--dest /orange/adamginsburg/web/public/portalkit] [--dry-run]
portalkit publish-res [--dest /orange/adamginsburg/web/public/portalkit-res] [--dry-run]
portalkit extract-legacy HTML [--out payload.json]
portalkit thumbs FAMILY [--max-px 480]                 # needs matplotlib extra
portalkit archive-checklist-diff EXPORT.csv [--checklist PATH]   # prints a unified diff; never writes the checklist
portalkit prune-stage FAMILY [--keep 5]
```

Exit codes:

| Code | Meaning |
|---|---|
| 0 | ok |
| 1 | check or verify failure |
| 2 | usage error |
| 3 | exposure refusal (visibility mismatch, write through a symlink) |
| 4 | ownership conflict (a file not owned by portalkit is in the way) |
| 5 | blocked input (tmp path) |
| 6 | legacy import failure (Tier B) |

Defaults:
- Stage root is `/blue/adamginsburg/adamginsburg/portalkit_stage` (or `$PK_STAGE_ROOT`). `build` refuses a stage root that `exposure.classify` reports as served. Every path under `/orange/adamginsburg` is served (secure) through `web/secure/abulatek/brick -> /blue/adamginsburg/abulatek/brick/` whose `symlinks/orange -> /orange/adamginsburg`, so the stage root lives on /blue.
- `--yes` is required for a non-dry-run deploy when stdin is not a TTY.
- `check` also scans every staged `.html`/`.json` file of the portal tree except `build_report.json` for absolute local paths (`/orange/`, `/blue/`, `/home/`, also as `file://`). Hits are warnings without a gate and at G1, and errors at G2 (§9.3 "Served paths"). Hits in shared resources (`res/`, `../_res/`) follow the same rule.

---

## 11. Deploy safety

### 11.1 Staging

`build` writes only under the stage root. Review happens through `portalkit serve`, either over an ssh tunnel or from Playwright. Staged trees are complete portals: relative URLs plus absolute asset/res URLs.

### 11.2 Exposure classifier (`deploy/exposure.py`)

```python
@dataclass
class Exposure:
    kind: Literal["public","secure","both","unserved"]
    public_urls: list[str]
    secure_urls: list[str]
    via: list[str]                     # symlink chain(s) that expose the path

def scan(roots={"public": Path("/orange/adamginsburg/web/public"), "secure": Path("/orange/adamginsburg/web/secure")},
         depth: int = 4, prune: tuple[str, ...] = ("*_hips",), cache_s: int | None = None) -> ExposureMap
         # cache_s None = default_cache_s(): 3600, or $PK_EXPOSURE_CACHE_S
def classify(path: Path, emap: ExposureMap | None = None) -> Exposure
```

URL prefixes:
- `web/public/X` → `https://data.rc.ufl.edu/pub/adamginsburg/X`
- `web/secure/X` → `https://data.rc.ufl.edu/secure/adamginsburg/X`

The scan follows directory symlinks under both roots to depth 4. It builds `realpath → url prefix` maps, and `classify` finds the longest matching prefixes.

Scan cost and caching (WPE3, WPE4):
- Each real directory is listed once per scan (keyed by its realpath), however many symlink routes or roots reach it; each route re-labels the children and warnings with its own logical path, so the result equals a per-route walk. `ExposureMap.stats` reports the call counts and wall time of the walk.
- Cache file `exposure-<key>.json`, format 3: the map plus `dirs`, a per-directory stamp `{realpath: [st_dev, st_ino, st_mtime_ns, st_ctime_ns, listed_at, [[name, "d"|"l"], ...]]}` of the sub-directories and symlinks of each listing. A result younger than `cache_s` is returned as is. An older one is revalidated: a directory whose `(dev, ino, mtime_ns, ctime_ns)` is unchanged reuses its recorded names (one `stat` in place of a `scandir`; entry changes, `chmod`, `chown` and `utime` all update ctime), a stamp taken within 5 s of the directory's mtime or ctime is not trusted, malformed stamps are ignored, and every symlink is resolved again. A revalidated map equals a fresh walk. Older cache formats are not read (the cache key itself is unchanged).
- Cache directory: `$PK_CACHE_DIR`, else `~/.cache/portalkit`. With `$PK_CACHE_DIR` set elsewhere, `~/.cache/portalkit` is also read (never written), and the newest valid file wins, so a run with an isolated cache (adapter tests) starts from the user's last scan.
- `cache_s=None` (the default) is `default_cache_s()`: `$PK_EXPOSURE_CACHE_S` when it is a finite number of seconds ≥ 0 (`0` disables every cache), else 3600; other values are ignored. It is a test/dev convenience: `tests/conftest.py` sets it to 86400 while `adapters` tests run, unless the user set it, so they reuse the user's last scan without revalidating it. Deploy and the CLI keep the 3600 s default unless the user sets the variable; an explicit `cache_s` always wins, and `cache_s=0` forces a fresh walk.

Verified facts that become test fixtures, mirrored as a fake tree in `test_exposure.py`:

| Link | Points to |
|---|---|
| `web/public/jwst/sgrb2` | `/orange/adamginsburg/jwst/sgrb2` |
| `web/public/jwst/sickle` | `/orange/adamginsburg/jwst/sickle` |
| `web/public/jwst/spectra` | `web/secure/jwst/spectra/mastDownload/JWST/pngs` |
| `web/secure/jwst` | `/orange/adamginsburg/jwst/` |
| `web/secure/robitaille_models` | `/orange/adamginsburg/robitaille_models` |
| `web/secure/w51/portal` | `/orange/adamginsburg/w51/portal` |

`web/secure/w51` is a real directory with `.htaccess` and `.htpasswd`. `web/public/robitaille_models`, `web/public/jwst/wd2` and `web/public/jwst/w51` are real directories.

Policy:
- A `secure` portal with a target classified `public` or `both` → refuse, exit 3, naming the exposing symlink.
- A `public` portal with a target classified `secure` only → warn. Allowed only when `served_by = "external"` (GC release site) or with `--yes`.
- A public page referencing a URL classified secure → warning plus a "login required" badge on that link. A reference to secure *data* that the page fetches → refuse, unless the reference is marked `link_only`.
- `unserved` targets → refuse unless `served_by = "external"`.

### 11.3 Ownership manifest and writes (`deploy/deploy.py`)

`<target>/.portalkit-deploy.json`:

```json
{"schema":"pk-deploy/1","family":"sgrb2","portal":"sgrb2","target":"/orange/.../unified",
 "builds":[{"build_id":"...","deployed":"...","portalkit":"1.0.0+g<sha>","adapter_sha":"...","legacy_modules":[...]}],
 "files":{"index.html":{"sha256":"...","size":1234,"build_id":"..."}, "data/src/manifest.json":{"...":"..."}},
 "replaced":[{"legacy":"../sgrb2_map_ccd.html","backup":".portalkit_backup/<build_id>/sgrb2_map_ccd.html","md5":"...","mode":"stub"}]}
```

Write rules:
- portalkit writes only files listed in the manifest, or new files at absent paths. Any other existing file → exit 4 with the list of conflicts.
- `os.lstat` runs on every path component inside the target. A symlink → exit 3. This matters because the WD2 public directory mixes copies and symlinks into source trees.
- Each file is written to `<name>.pk-tmp`, `fsync`ed, then moved with `os.replace`. Data files are written first, `portal.json` next, HTML last. Content-hashed data names let old and new HTML coexist.
- Superseded content-hashed files are deleted only by `prune-stage`, or by `deploy --gc`, which removes manifest-owned files that no deployed HTML references and that are older than 7 days.
- Shared assets (`publish-assets`) go to `web/public/portalkit/v<semver>/`. A publish that would change an existing version's bytes is refused. The same rule applies to `portalkit-res`.

### 11.4 `--replace` and rollback (`deploy/backup.py`)

For each `[[portal.page]]` with `legacy` files (§4.1). Items with `replace = false` are skipped; they stay in place and the plan lists them as a warning. `<#preset>` below is the item's own `preset` when its table sets one (`""` = none), else the page `preset`. One page can therefore serve several legacy files that each open on their own view, for example `pngs/gallery_6927_2221.html` → `gallery_6927.html#role=c2221`.
1. Copy the legacy file to `<legacy_dir>/.portalkit_backup/<build_id>/<file>`. Record its md5 and size in `<legacy_dir>/.portalkit_backup/<build_id>/backup.json`.
2. In `stub` mode (the default), atomically write a redirect stub of about 1 kB at the legacy filename:

   ```html
   <!doctype html><meta charset="utf-8"><title>Moved</title>
   <script>location.replace('unified/<page>.html'+location.search+(location.hash||'<#preset>'));</script>
   <noscript><a href="unified/<page>.html">This page moved</a></noscript>
   ```

   The unified page's legacy grammar mapper resolves old hashes and queries.
3. In `inplace` mode, the full unified page is written at the legacy name, with relative URLs rewritten. This mode must be requested explicitly.
4. `portalkit rollback FAMILY BUILD_ID` restores every file from `backup.json`, verifies md5 and updates the ownership manifest.

portalkit never moves or deletes legacy data directories (PNGs, specdata, `ccd_bg_*.png`).

### 11.5 Verify, cache headers and status

- `verify --http` fetches every owned file over HTTPS, using `urllib`, and compares sha256 with the ownership manifest. Downloads go to a temporary file before hashing, which avoids the `curl | grep -q` SIGPIPE false negative.
  - Secure URLs must return 401 without credentials. With `PK_BASIC_AUTH=user:pass` they must return 200 and match.
  - Public URLs must return 200.
  - It reports `Content-Type`, `Content-Encoding` (expected absent for `.bin`) and `Cache-Control`.
- `--htaccess` writes `<target>/.htaccess` only if no `.htaccess` exists there or the existing one is owned by portalkit:

  ```
  <IfModule mod_headers.c>
    <FilesMatch "\.[0-9a-f]{8}\.(bin|json|html)$">
      Header set Cache-Control "public, max-age=31536000, immutable"
    </FilesMatch>
    <FilesMatch "^(manifest|index|images|hips|overlays|narrative|portal)\.json$|\.html$">
      Header set Cache-Control "no-cache"
    </FilesMatch>
  </IfModule>
  AddType application/octet-stream .bin
  ```

  `verify` reports whether the header took effect. Apache `AllowOverride` may ignore it, and the viewer works either way.
- Deploy bookkeeping is denied over HTTP (WPE3, WPE4). The ownership manifests (`.portalkit-deploy.json`, `.portalkit-deploy.<portal>.json`) keep absolute paths (§9.3 "Served paths"), and `.portalkit_backup/` holds `backup.json` and the legacy originals. The portalkit `.htaccess` (first line: the `pk-htaccess/1` marker) therefore carries, after the cache-header block when there is one:

  ```
  # deploy bookkeeping (ownership manifests, backups, temporaries) is never served
  <IfModule mod_alias.c>
    RedirectMatch 403 "/\.portalkit[^/]*(/|$)|\.pk-tmp$"
  </IfModule>
  ```

  `RedirectMatch` is in the `FileInfo` override class, the class `Header` and `AddType` already need, so the rule adds nothing to what `--htaccess` requires (a `Require` rule would need `AuthConfig`). `htaccess.denied(rel)` applies the same pattern in Python.
  - A plain deploy writes a `.htaccess` as well, so it needs `AllowOverride` with `FileInfo`, or `None` (file ignored). Apache answers 500 for every file of a directory whose `.htaccess` uses a class it does not allow, and `<IfModule>` does not prevent that; Q4 (§16) covers this. `PK_DENY_HTACCESS=0` (or `deploy(..., deny_htaccess=False)`) turns the deny-only file off.
  - `deploy` writes the rule with or without `--htaccess`, before the first manifest write. Without `--htaccess`, a target with no `.htaccess` gets a minimal managed file (marker and deny rule only), a portalkit-owned file is brought up to date (cache headers an earlier `--htaccess` wrote are kept), and a foreign `.htaccess` or a symlink is left unchanged with a warning. With `--htaccess` the cache-header block is written as above, and a foreign file stays an ownership conflict (exit 4), a symlink a refusal (exit 3).
  - Whenever `backup.py` writes to `<legacy_dir>/.portalkit_backup/` it first writes `.portalkit_backup/.htaccess` with the marker and the deny rule (`htaccess.write_backup_htaccess`). A foreign or symlinked file there refuses the `--replace` deploy at planning (exit 4 / 3), before anything is written.
  - The development server answers 403 for every path `htaccess.denied` matches and leaves them out of listings.
  - `verify --http` sends `HEAD <portal url>/<manifest name>`. When the target holds the portalkit deny rule, 403 or 404 is expected, and any other answer is a warning (the host ignores the `.htaccess`); a 5xx is undecided and warns of a probable `AllowOverride` rejection, as do 5xx answers on owned files. Without the rule, a 200 is a warning. Warnings do not fail `verify`. A secure portal checked without `PK_BASIC_AUTH` gets 401 (authentication precedes the per-directory redirect), and the rule is reported as not checked.
- `status` lists, per family and portal:
  - legacy files (mtime, sha);
  - the last staged build;
  - the deployed build;
  - served sha;
  - `DRIFT` when staged ≠ served;
  - `LEGACY-CHANGED` when a legacy module sha differs from the deployed provenance;
  - `excluded` rows.

---

## 12. Theme and CSS tokens (`web/css/tokens.css`) **[FROZEN token names]**

```css
:root {
  --pk-bg:#ffffff; --pk-bg-2:#f5f6f8; --pk-bg-3:#e9ecf1; --pk-fg:#1b1f24; --pk-fg-2:#4a5360; --pk-fg-3:#7a8494;
  --pk-border:#d5dae1; --pk-accent:#2f6fb2; --pk-accent-fg:#ffffff; --pk-focus:#e08a2c;
  --pk-sel:#d9822b; --pk-hover:#1f9bb6; --pk-neutral:#4b5563; --pk-danger:#c0392b; --pk-warn:#b7791f; --pk-ok:#2f855a;
  --pk-plot-bg:#ffffff; --pk-plot-grid:#e6e9ee; --pk-plot-axis:#4a5360; --pk-density-cmap:"magma";
  --pk-cat-0:#4e79a7; --pk-cat-1:#f28e2b; --pk-cat-2:#e15759; --pk-cat-3:#76b7b2; --pk-cat-4:#59a14f;
  --pk-cat-5:#edc948; --pk-cat-6:#b07aa1; --pk-cat-7:#ff9da7; --pk-cat-8:#9c755f; --pk-cat-9:#bab0ac;
  --pk-font:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; --pk-mono:ui-monospace,SFMono-Regular,Menlo,monospace;
  --pk-fs-0:12px; --pk-fs-1:13px; --pk-fs-2:15px; --pk-fs-3:18px; --pk-fs-4:22px;
  --pk-sp-1:4px; --pk-sp-2:8px; --pk-sp-3:12px; --pk-sp-4:16px; --pk-sp-5:24px;
  --pk-radius:6px; --pk-shadow:0 2px 8px rgba(0,0,0,.12); --pk-splitter:6px; --pk-header-h:44px;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { /* dark values */ } }
:root[data-theme="dark"] { /* identical dark values */ }
```

Dark values:
- `--pk-bg:#0f1216`, `--pk-bg-2:#161b22`, `--pk-bg-3:#1f2630`
- `--pk-fg:#e6e9ee`, `--pk-fg-2:#b3bbc6`, `--pk-fg-3:#808a98`
- `--pk-border:#2b333f`, `--pk-accent:#5aa2e6`
- `--pk-sel:#f2b27a` (the GC `SELECTION_COLOR`), `--pk-neutral:#e8dcc4` (GC cream)
- `--pk-plot-bg:#0f1216`, `--pk-plot-grid:#232a35`, `--pk-plot-axis:#b3bbc6`
- `color-scheme: dark`

Rules:
- `core/theme.js` exports `tokens(): Tokens` (reads computed styles, cached per theme), `toggle()` and `set('auto'|'light'|'dark')`. It persists the choice in `localStorage['pk:theme']` inside try/catch and sets `<html data-theme>`.
- Canvas and WebGL panes read `--pk-plot-*`, `--pk-sel` and `--pk-neutral` and pass `neutral`/`selColor` to the renderers. This fixes the W51 colorcolor dark plot ground in light theme.
- `body` has an explicit `background: var(--pk-bg)`.
- Layout uses a 16 px side gutter on phones.

---

## 13. Testing

### Level 0: unit tests (every commit, < 1 min)

pytest, run with `pytest -q`:

| Area | Checks |
|---|---|
| Codec round-trips | error ≤ step/2; missing codes; shuffles; i16 offsets; u8log; str; sparse |
| Golden bytes | byte-identical gz output |
| LOD prefix uniformity | KS test p > 0.01 on 3 columns |
| ids/aliases | uniqueness |
| Shell | `</` escaping; HTML ≤ 200 kB |
| Records | pks1/pki1 read-back; shard packer bounds |
| Resolver | caps truth tables |
| Validators | schema validators |
| Exposure | classifier on a fake tree with nested public→secure and secure→public links |
| Deploy | no-clobber; symlink refusal; write ordering; `--replace` stub plus rollback md5 |
| Legacy | extraction on the stored snippets of each embed style; hash grammars |
| Code rules | no-broad-except AST scan; constants sync |

`node --test tests/js/` (marker `js`; skipped if `node` is absent) covers:
- codec.js against Python golden bytes;
- expr.js against `expr_vectors.json`;
- selection set ops, including intersect with a non-drawable remainder and undo depth;
- hash serialization for ids/bits/r;
- legacy grammars;
- `fold` (W·base + K equals the direct evaluation within 1e-9);
- tour orthonormality;
- axes ticks;
- `sedPoints`, mag↔Jy and reddening against `tests/golden/physics`;
- CSV export.

### Level 1: archetype smoke on synthetic fixtures (~5 min, `-m e2e`)

`tests/fixtures/make_synthetic.py` builds portals at N = 8, 2,000, 50,000 and 1,000,000 (5,000,000 under `-m slow`). The fixtures include:
- mags, errors, categories, flags, limits, positions;
- 20 pks1 spectra and 20 pki1 images (one PNG-mode set);
- 20 images, templates, filters, isochrones;
- HiPS stubs, sky/dim overlays and a 5-beat narrative;
- a 120-dim virtual-colour dataset, which forces the CPU projection path.

Pages are served by `portalkit.deploy.devserver`, with `file://` avoided. Chromium flags are `--use-angle=swiftshader --enable-unsafe-swiftshader`. `page.route('**/aladin*.js')` serves `tests/fixtures/aladin_stub.js`, which implements the Aladin API surface used (A.init, aladin(), setImageSurvey/setBaseImageLayer, newImageSurvey, catalog, marker, source, polygon, circle, graphicOverlay, on, gotoRaDec, setFoV, getRaDec, getFov, pix2world, world2pix, createImageFITS, removeLayers) and records calls in `window.__aladinCalls`.

Assertions:
- `await __pk.ready`;
- zero console errors;
- HTML < 200 kB;
- `data-caps` equals the resolver output;
- no horizontal scroll at 390×844;
- one run per tier with WebGL2 disabled (tier F);
- one run with `DecompressionStream` deleted (inflate.js path);
- one run with the CDN route aborted (mirror path), and one with both aborted (error note, page still usable).

### Level 2: capability matrix (~10 min)

`test_caps_matrix.py` is parametrized over the enabled (capability × archetype) pairs.
- Synthetic pointer drags for rect/circle/lasso with each modifier must equal numpy-computed masks.
- Linked selection must reach list, sky-stub markers and manifold.
- Deep links (canonical and every legacy grammar) must survive a reload.
- CSV export/import must round-trip.
- Theme and layout must persist.
- Keyboard navigation must walk only the filtered list.
- Lightbox swipe and hash clear must work.
- A_V changes must move template and isochrone paths.
- Spectrum zoom must autoscale Y.
- The 2D x-link must work.
- Live SED points must match Python `sedPoints`.
- Cross-fade opacities must be correct.
- Narrative beats must leave no stray overlays.

### Level 3: perf budgets (`tests/perf/test_perf_budget.py`, `-m perf`)

Byte budgets are hard. Timing budgets apply under SwiftShader and are generous; results go to `perf_report.json` for trend review.

| N | first paint | brush commit | settle after pan | bytes before first paint |
|---|---|---|---|---|
| 50k | ≤ 3 s | ≤ 300 ms | ≤ 500 ms | ≤ 2 MB |
| 1M | ≤ 10 s | ≤ 1.5 s | ≤ 2 s | ≤ 2 MB |
| 5M (slow) | ≤ 25 s | ≤ 6 s | ≤ 6 s | ≤ 6 MB |

Timing rules (WPD3, IMPLEMENTATION_NOTES 120):
- Before the first timed page of a session the browser loads one untimed warm-up page of the same synthetic portal (first of cmd, hist, explorer), waits until it has been quiet for `QUIET_SETTLE_MS` and closes it, so the timed runs measure the page and not Chromium start-up (GPU process, SwiftShader initialisation). `PK_PERF_WARMUP=0` skips the warm-up, to compare against a cold browser.
- Each timed run opens a fresh browser context (cold HTTP cache). Its clock starts at `page.goto` (`PageRun.t_start`); creating the context and the page is not counted.
- `first_paint_from_open_s` (first paint counted from before the context was created), `open_s` and `first_paint_at_s` are written to `perf_report.json` and are not budgeted.

### Level 4: adapters and per-port acceptance

- `pytest -m adapters` runs §9.6.
- `tests/e2e/test_port_<family>.py` turns each port-table `special_handling` entry (§14) into assertions on `__pk` hooks and stub logs. Example for the 6927 map: N MSA polygons are present, product links return 200 or 401 on HEAD, `#6927_10038` resolves, and the legacy redirect lands on the right source.
- `tools/contact_sheet.py` pairs legacy and unified screenshots for user review. Pixel diffs are not gates.

### Level 5: served checks

`portalkit verify --http` covers status, sha, auth and headers. Authenticated checks run only with `PK_BASIC_AUTH`.

### Test hooks (`core/testhooks.js`, only with `?pktest=1`)

```js
window.__pk = { ready: Promise<void>, caps(): string[], tiers(): Record<pane, tier>, store, hub,
                select(ds, ids: string[], op?): Promise<number>, brush(pane, shape, op?): Promise<number>,
                focus(ds, id): Promise<void>, stats(): {fetches, bytes, renderMs, decodeMs}, errors: string[] }
```

### Gates per family

| Gate | Requirement |
|---|---|
| G1 | Tier A passes Levels 0–2 on its output |
| G2 | Tier B passes parity (§9.6 f), Level 4, and has no `json-legacy` record sets |
| G3 | deployed to `unified/`; `verify --http` green; user reviews the contact sheet |
| G4 | after two weeks of coexistence and on the user's request: `--replace` with backups |

## 14. Per-page port table

This table is generated by `port_table.py` and stored as `tests/data/port_table.json` (WP12). `capabilities_to_fill` lists the capabilities that the resolver enables for the page and that the legacy page lacks. These are new work beyond parity. Legacy capabilities are verified as a subset of the enabled set by `test_parity_inventory.py`. Archetype `excluded` means the page is registered in `portal.json` for nav only and is not rebuilt.

Totals: 93 rows (87 inventoried pages + 6 extras rows), 11 families, 1517 capability fills.

Family keys (WPE1). The `family` of every row in `tests/data/port_table.json` and `docs/port_table.json`, the family prefix of every `tests/data/inventory_caps.json` key, and `reports[].family` in `docs/inventory.json` equal the site id (`sites/<family>.toml` stem): `gc_manifold`, `jwst_archive`, `nirspec2640`, `nirspec6927`, `robitaille`, `sgrb2`, `sickle_brick`, `spherex`, `w51`, `wd2`. Adapter tests look rows up with their `FAMILY` constant directly. `extras` stays a grouping key: its six rows belong to several sites (`hips_tours`, `spitzer_spectra` and `dirgallery_sites`, plus the jwst_gc_aladin and cmd_explorer pages of WP31). The nirspec6927 narrative rows (msata, h2, refstar) keep `nirspec6927` because they are pages of the same legacy family that `sites/nirspec6927_narrative.toml` deploys into the same target.


### 14.1 extras (6 rows)

| legacy page | archetype | flags | caps to fill (n) | capabilities to fill | special handling |
|---|---|---|---|---|---|
| `cmz/spitzer_spectra (critic)` | explorer + spectra | multi cat cats spec sky skycat num2 mags2 | 29 | av_slider, brush_circle, brush_lasso, brush_rect, brush_set_ops, catalog_markers_on_sky, cdn_fallback, class_coloring, cmd_ccd_scatter, collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, detail_panel, filter_toggles, hips_layer_stacking, hover_preview, keyboard_nav, labeled_features_overlay, lazy_data_loading, linked_selection, model_template_overlay, readme_provenance, resizable_panels, responsive_mobile, selection_export, sky_map_aladin, source_list_search, spectrum_viewer_1d | About 80-line adapter; generality check of the contract (wave 6). |
| `RGB wavelength tours / ACES tours (critic)` | wavelength | sky | 11 | cdn_fallback, collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, hips_layer_stacking, keyboard_nav, lazy_data_loading, readme_provenance, responsive_mobile, sky_map_aladin | hips_tours.py adapter; rungs from existing HiPS lists. |
| `jwst-gc/jwst_gc_aladin.html (critic)` | explorer [sky] | sky cat skycat dens | 17 | catalog_markers_on_sky, cdn_fallback, collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, density_rendering, detail_panel, hips_layer_stacking, hover_preview, keyboard_nav, lazy_data_loading, readme_provenance, responsive_mobile, selection_export, sky_map_aladin, source_list_search | Catalog-HiPS layers as overlays; GC catalogs via skyWindow. |
| `cmd_explorer (critic)` | explorer (pre-binned) | multi cat num2 mags2 num3 dens | 21 | brush_circle, brush_lasso, brush_rect, brush_set_ops, cmd_ccd_scatter, collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, density_rendering, detail_panel, hover_preview, keyboard_nav, lazy_data_loading, linked_selection, readme_provenance, resizable_panels, responsive_mobile, selectable_axes, selection_export, source_list_search | Pre-binned density grids as first-class dataset (density.py). |
| `~30 jQuery autoindex galleries in w51/brick/sgrb2/wd2/wd1/cloudc (critic)` | gallery (dirgallery) | items img | 12 | collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, filter_toggles, hover_preview, keyboard_nav, lazy_data_loading, lightbox, readme_provenance, responsive_mobile, source_list_search | One dirgallery_sites.py registry; fixes mislabeled headers; legacy files untouched. |
| `robitaille imaging_run/png_b and modelgrids/ccds/gallery.html (critic)` | gallery (dirgallery) | items img | 12 | collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, filter_toggles, hover_preview, keyboard_nav, lazy_data_loading, lightbox, readme_provenance, responsive_mobile, source_list_search | png_b reachable only via web/secure/robitaille_models: visibility declared per page (secure). |

### 14.2 gc_manifold (3 rows)

| legacy page | archetype | flags | caps to fill (n) | capabilities to fill | special handling |
|---|---|---|---|---|---|
| `GC Treasury catalog manifold viewer (treasury_manifold)` | manifold tier T2 (D=2 projection, GPU) | multi cat cats num2 mags2 num3 mani mags3f sky skycat dens | 18 | av_slider, brush_circle, catalog_markers_on_sky, cdn_fallback, class_coloring, cross_page_nav, dark_light_theme, filter_toggles, hips_layer_stacking, labeled_features_overlay, linked_selection, live_sed_viewer, manifold_embedding, model_template_overlay, resizable_panels, responsive_mobile, selection_export, source_list_search | GC manifest schema 1 read in place by legacy/gc_manifest.py (no re-encode); query keys y,g,rv,fa mapped; cutout_layers/errs added to the manifest view; hosted at starformation.astro.ufl.edu (deploy target registered as external, verify only). |
| `GC per-field catalog manifold viewers (<field>_manifold; 11 instances of one template)` | manifold tier T2 (11 field pages) | multi cat cats num2 mags2 num3 mani mags3f sky skycat dens | 16 | av_slider, brush_circle, catalog_markers_on_sky, cdn_fallback, class_coloring, cross_page_nav, dark_light_theme, filter_toggles, hips_layer_stacking, labeled_features_overlay, linked_selection, model_template_overlay, resizable_panels, responsive_mobile, selection_export, source_list_search | Full query state a,b,m,p,x1,x2,y1,y2,cb,cd,cm,cl,ch,g,rv,fa,sel mapped 1:1 by legacyhash; FEATURE_CAVEATS -> caveats; red-clump button kept as preset action; renderer vendored verbatim from gc_fields_manifold_assets. |
| `jwst-gc pipeline monitor (monitor.html, fields/<target>.html, *_monitor5as.html)` | excluded |  | 0 |  | QA dashboard of the pipeline (409 kB index + 195 MB field docs); out of portal scope; registry lists it as excluded so status reports it. |

### 14.3 jwst_archive (16 rows)

| legacy page | archetype | flags | caps to fill (n) | capabilities to fill | special handling |
|---|---|---|---|---|---|
| `JWST archive colour portal` | explorer [scatter CCD + 2 marginal CMDs | list | detail + spectrum] | multi cat cats num2 mags2 num3 mags3f sky skycat spec dens | 16 | brush_circle, brush_lasso, brush_set_ops, catalog_markers_on_sky, cdn_fallback, collapsible_panels, dark_light_theme, deep_link_hash, density_rendering, extinction_vectors_on_ccd, hips_layer_stacking, keyboard_nav, live_sed_viewer, resizable_panels, selection_export, sky_map_aladin | Band-coverage gating from JWST_Archive_ColorPortal.py:310-345 -> per-column validity bits; pin up to 8 spectra; velocity offset; filter transmission shading; keep-only/invert; presets; spectra in pks1 shards. |
| `JWST archive ice manifold` | manifold | multi cat cats num2 mags2 num3 mani mags3f sky skycat spec dens | 16 | brush_circle, brush_lasso, catalog_markers_on_sky, cdn_fallback, collapsible_panels, dark_light_theme, density_rendering, extinction_vectors_on_ccd, hips_layer_stacking, labeled_features_overlay, linked_selection, live_sed_viewer, readme_provenance, resizable_panels, selection_export, sky_map_aladin | Stale idx (695/1539): spectra keyed by id derived from the x1d filename prefix; Wd2 branding and 2640_ prefix from config; template URL resolved per exposure. |
| `JWST archive portal README` | landing (readme section) | search | 7 | collapsible_panels, dark_light_theme, deep_link_hash, keyboard_nav, lazy_data_loading, responsive_mobile, source_list_search | Covers both archive pages; README rendered by markdown_lite. |
| `W51 photometry vs JWST archive spectra` | explorer (W51 build of archive portal) | multi cat cats num2 mags2 num3 mags3f sky skycat spec dens | 17 | av_slider, brush_circle, brush_lasso, brush_set_ops, catalog_markers_on_sky, cdn_fallback, collapsible_panels, dark_light_theme, deep_link_hash, extinction_vectors_on_ccd, hips_layer_stacking, keyboard_nav, live_sed_viewer, model_template_overlay, resizable_panels, selection_export, sky_map_aladin | Same page as w51 #10 (one build, one row each family). |
| `W51 archive_colors README` | landing (readme section) | search | 7 | collapsible_panels, dark_light_theme, deep_link_hash, keyboard_nav, lazy_data_loading, responsive_mobile, source_list_search | Readme for the W51 build. |
| `Spectra PNG gallery` | gallery with facets program/target/filterset/grating | items img sedimg cat cats sky skycat spec | 20 | av_slider, catalog_markers_on_sky, cdn_fallback, class_coloring, collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, detail_panel, hips_layer_stacking, hover_preview, labeled_features_overlay, lazy_data_loading, model_template_overlay, readme_provenance, responsive_mobile, selection_export, sky_map_aladin, source_list_search, spectrum_viewer_1d | Triplicate PNGs deduplicated (un-annotated duplicate becomes a role); join to archive catalog by x1d prefix id; link into the colour portal by id. |
| `Spectra PNG gallery with classification checklist` | gallery in annotate mode | items img sedimg cat cats sky skycat spec annot | 18 | av_slider, catalog_markers_on_sky, cdn_fallback, class_coloring, cross_page_nav, dark_light_theme, deep_link_hash, detail_panel, hips_layer_stacking, hover_preview, labeled_features_overlay, lazy_data_loading, model_template_overlay, readme_provenance, responsive_mobile, sky_map_aladin, source_list_search, spectrum_viewer_1d | Codes y/n/g/w/l/e stored per viewer in localStorage; export CSV with checklist key columns; never writes jwst_archive_spectra_checklist.csv; portalkit archive-checklist-diff prints a diff for manual application. |
| `Gallery: PRISM` | gallery (facet preset of archive gallery) | items img sedimg cat cats sky skycat spec | 21 | av_slider, catalog_markers_on_sky, cdn_fallback, class_coloring, collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, detail_panel, filter_toggles, hips_layer_stacking, hover_preview, labeled_features_overlay, lazy_data_loading, model_template_overlay, readme_provenance, responsive_mobile, selection_export, sky_map_aladin, source_list_search, spectrum_viewer_1d | Facet preset 'prism' of the single archive gallery; redirect stub only under --replace; fixes gallery_prism matching 0 files. |
| `Gallery: PRISM 2221 filters` | gallery (facet preset of archive gallery) | items img sedimg cat cats sky skycat spec | 21 | av_slider, catalog_markers_on_sky, cdn_fallback, class_coloring, collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, detail_panel, filter_toggles, hips_layer_stacking, hover_preview, labeled_features_overlay, lazy_data_loading, model_template_overlay, readme_provenance, responsive_mobile, selection_export, sky_map_aladin, source_list_search, spectrum_viewer_1d | Facet preset 'prism+2221' of the single archive gallery; redirect stub only under --replace. |
| `Gallery: 1611 Serpens` | gallery (facet preset of archive gallery) | items img sedimg cat cats sky skycat spec | 21 | av_slider, catalog_markers_on_sky, cdn_fallback, class_coloring, collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, detail_panel, filter_toggles, hips_layer_stacking, hover_preview, labeled_features_overlay, lazy_data_loading, model_template_overlay, readme_provenance, responsive_mobile, selection_export, sky_map_aladin, source_list_search, spectrum_viewer_1d | Facet preset '1611' of the single archive gallery; redirect stub only under --replace. |
| `Gallery: 1611 broad` | gallery (facet preset of archive gallery) | items img sedimg cat cats sky skycat spec | 21 | av_slider, catalog_markers_on_sky, cdn_fallback, class_coloring, collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, detail_panel, filter_toggles, hips_layer_stacking, hover_preview, labeled_features_overlay, lazy_data_loading, model_template_overlay, readme_provenance, responsive_mobile, selection_export, sky_map_aladin, source_list_search, spectrum_viewer_1d | Facet preset '1611 broad' of the single archive gallery; redirect stub only under --replace. |
| `Gallery: 1611 narrow` | gallery (facet preset of archive gallery) | items img sedimg cat cats sky skycat spec | 21 | av_slider, catalog_markers_on_sky, cdn_fallback, class_coloring, collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, detail_panel, filter_toggles, hips_layer_stacking, hover_preview, labeled_features_overlay, lazy_data_loading, model_template_overlay, readme_provenance, responsive_mobile, selection_export, sky_map_aladin, source_list_search, spectrum_viewer_1d | Facet preset '1611 narrow' of the single archive gallery; redirect stub only under --replace. |
| `Gallery: 1611 two gratings` | gallery (facet preset of archive gallery) | items img sedimg cat cats sky skycat spec | 21 | av_slider, catalog_markers_on_sky, cdn_fallback, class_coloring, collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, detail_panel, filter_toggles, hips_layer_stacking, hover_preview, labeled_features_overlay, lazy_data_loading, model_template_overlay, readme_provenance, responsive_mobile, selection_export, sky_map_aladin, source_list_search, spectrum_viewer_1d | Facet preset '1611 two gratings' of the single archive gallery; redirect stub only under --replace. |
| `Gallery: 3222 IRAS16293` | gallery (facet preset of archive gallery) | items img sedimg cat cats sky skycat spec | 21 | av_slider, catalog_markers_on_sky, cdn_fallback, class_coloring, collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, detail_panel, filter_toggles, hips_layer_stacking, hover_preview, labeled_features_overlay, lazy_data_loading, model_template_overlay, readme_provenance, responsive_mobile, selection_export, sky_map_aladin, source_list_search, spectrum_viewer_1d | Facet preset '3222' of the single archive gallery; redirect stub only under --replace. |
| `Gallery: 3222 broad` | gallery (facet preset of archive gallery) | items img sedimg cat cats sky skycat spec | 21 | av_slider, catalog_markers_on_sky, cdn_fallback, class_coloring, collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, detail_panel, filter_toggles, hips_layer_stacking, hover_preview, labeled_features_overlay, lazy_data_loading, model_template_overlay, readme_provenance, responsive_mobile, selection_export, sky_map_aladin, source_list_search, spectrum_viewer_1d | Facet preset '3222 broad' of the single archive gallery; redirect stub only under --replace. |
| `Gallery: 3222 narrow` | gallery (facet preset of archive gallery) | items img sedimg cat cats sky skycat spec | 21 | av_slider, catalog_markers_on_sky, cdn_fallback, class_coloring, collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, detail_panel, filter_toggles, hips_layer_stacking, hover_preview, labeled_features_overlay, lazy_data_loading, model_template_overlay, readme_provenance, responsive_mobile, selection_export, sky_map_aladin, source_list_search, spectrum_viewer_1d | Facet preset '3222 narrow' of the single archive gallery; redirect stub only under --replace. |

### 14.4 nirspec2640 (11 rows)

| legacy page | archetype | flags | caps to fill (n) | capabilities to fill | special handling |
|---|---|---|---|---|---|
| `index.html` | landing | search | 6 | collapsible_panels, dark_light_theme, deep_link_hash, keyboard_nav, lazy_data_loading, source_list_search | Nav lists built pages only (nonexistent icecolumns view disappears); MAST links checked by verify HEAD; resolves public-copy drift (203572 vs 209115 B) by deploying to one target. |
| `nirspec_2640_map.html` | explorer [list | sky | detail] | multi cat cats num2 mags2 num3 mags3f sky skycat spec img sedimg | 22 | av_slider, brush_circle, brush_lasso, brush_rect, brush_set_ops, class_coloring, cmd_ccd_scatter, dark_light_theme, extinction_vectors_on_ccd, keyboard_nav, labeled_features_overlay, lazy_data_loading, lightbox, linked_selection, live_sed_viewer, model_template_overlay, readme_provenance, resizable_panels, selectable_axes, selection_export, spectrum_viewer_1d, static_sed_png | MSA shutter + dither polygons -> overlays.json; base-layer toggle bug fixed in sky pane; product links ../mastDownload/... rewritten to the secure archive URL and badged login required on the public page. |
| `nirspec_2640_colorcolor.html` | explorer [scatter grid | list | detail] | multi cat cats num2 mags2 num3 mags3f sky skycat iso dens spec | 24 | av_slider, brush_circle, brush_lasso, brush_set_ops, catalog_markers_on_sky, cdn_fallback, class_coloring, collapsible_panels, dark_light_theme, deep_link_hash, extinction_vectors_on_ccd, hips_layer_stacking, isochrones_or_tracks, keyboard_nav, labeled_features_overlay, lazy_data_loading, live_sed_viewer, model_template_overlay, readme_provenance, resizable_panels, responsive_mobile, sky_map_aladin, source_list_search, spectrum_viewer_1d | Catalog-vs-synthetic paired markers with connectors -> scatter pairs overlay; BD histogram split -> hist range brush; Vargas13 HST match columns; hardcoded _o001_p02640 replaced by image-role URLs; density on custom axes from context dataset. |
| `nirspec_2640_specviewer.html` | spectra [list | spectrum1d + spectrum2d | detail] | multi cat cats spec spec2d sky skycat mags3f | 18 | catalog_markers_on_sky, cdn_fallback, class_coloring, collapsible_panels, dark_light_theme, detail_panel, extinction_vectors_on_ccd, hips_layer_stacking, hover_preview, keyboard_nav, linked_selection, live_sed_viewer, readme_provenance, resizable_panels, selection_export, sky_map_aladin, source_list_search, spectrum_2d_viewer | Band-jump buttons -> config bands; collision-dropped labels; out-of-order fetch fixed by record LRU stale token; s2d FITS -> pki1; multi-source pin overlay (8). |
| `nirspec_2640_pvgallery.html` | gallery (roles L3/L2/no-bgsub) | items img cat cats sky skycat | 12 | catalog_markers_on_sky, cdn_fallback, class_coloring, collapsible_panels, dark_light_theme, detail_panel, filter_toggles, hips_layer_stacking, hover_preview, readme_provenance, selection_export, sky_map_aladin | Sort/filter by catalog properties; L2 and no-bgsub variants as image roles. |
| `nirspec_2640_sedgallery.html` | gallery (+live SED) | items img sedimg multi cat cats num2 mags2 num3 mags3f sky skycat | 25 | av_slider, brush_circle, brush_lasso, brush_rect, brush_set_ops, catalog_markers_on_sky, cdn_fallback, class_coloring, cmd_ccd_scatter, collapsible_panels, dark_light_theme, detail_panel, extinction_vectors_on_ccd, filter_toggles, hips_layer_stacking, hover_preview, labeled_features_overlay, linked_selection, live_sed_viewer, model_template_overlay, readme_provenance, resizable_panels, selectable_axes, selection_export, sky_map_aladin | CAT_FILTERS limit removed for live SED (all synthetic filters). |
| `nirspec_2640_red_excess.html` | explorer [scatter main | scatter offset vs r_wd2 | detail] | multi cat cats num2 mags2 num3 mags3f sky skycat dens iso | 25 | av_slider, brush_circle, brush_lasso, brush_set_ops, catalog_markers_on_sky, cdn_fallback, collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, filter_toggles, hips_layer_stacking, isochrones_or_tracks, keyboard_nav, labeled_features_overlay, lazy_data_loading, linked_selection, live_sed_viewer, model_template_overlay, readme_provenance, resizable_panels, responsive_mobile, selection_export, sky_map_aladin, source_list_search | Background switch -> raster layers density vs binned-stat median(r_wd2); perpendicular offset is an adapter-derived column; adjustable reddening vector and cut line; law label from config. |
| `nirspec_2640_manifold.html` | manifold | multi cat cats num2 mags2 num3 mani mags3f sky skycat spec img sedimg iso | 13 | brush_circle, brush_lasso, catalog_markers_on_sky, cmd_ccd_scatter, dark_light_theme, extinction_vectors_on_ccd, hips_layer_stacking, labeled_features_overlay, lightbox, linked_selection, readme_provenance, resizable_panels, static_sed_png | Legacy generator writes straight to public; under portalkit it goes through staging and the ownership manifest; BB curve in live SED. |
| `pngs/gallery_2640.html` | gallery (one gallery, 3 roles: all/nb/bb) | items img sedimg cat sky skycat | 16 | catalog_markers_on_sky, cdn_fallback, collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, detail_panel, filter_toggles, hips_layer_stacking, hover_preview, lazy_data_loading, readme_provenance, selection_export, sky_map_aladin, source_list_search, static_sed_png | Autoindex replaced by static listing; three legacy files map to one page with role presets (#role=nb\|bb). |
| `pngs/gallery_2640_nb.html` | gallery (role preset nb) | items img sedimg cat sky skycat | 16 | catalog_markers_on_sky, cdn_fallback, collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, detail_panel, filter_toggles, hips_layer_stacking, hover_preview, lazy_data_loading, readme_provenance, selection_export, sky_map_aladin, source_list_search, static_sed_png | Same unified page as gallery_2640 with #role=nb; under --replace the legacy file becomes a redirect stub carrying the role. |
| `pngs/gallery_2640_bb.html` | gallery (role preset bb) | items img sedimg cat sky skycat | 16 | catalog_markers_on_sky, cdn_fallback, collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, detail_panel, filter_toggles, hips_layer_stacking, hover_preview, lazy_data_loading, readme_provenance, selection_export, sky_map_aladin, source_list_search, static_sed_png | Same unified page as gallery_2640 with #role=bb; redirect stub under --replace. |

### 14.5 nirspec6927 (15 rows)

| legacy page | archetype | flags | caps to fill (n) | capabilities to fill | special handling |
|---|---|---|---|---|---|
| `index.html` | landing | search | 7 | collapsible_panels, dark_light_theme, deep_link_hash, keyboard_nav, lazy_data_loading, responsive_mobile, source_list_search | MAST per-observation links + astroquery snippet as readme block; links to refstar pages; doctype/viewport added. |
| `nirspec_6927_map.html` | explorer [list | sky | detail] | multi cat cats num2 mags2 num3 mags3f sky skycat spec spec2d img sedimg | 24 | av_slider, brush_circle, brush_lasso, brush_rect, brush_set_ops, cmd_ccd_scatter, collapsible_panels, dark_light_theme, extinction_vectors_on_ccd, hips_layer_stacking, keyboard_nav, labeled_features_overlay, lazy_data_loading, lightbox, linked_selection, live_sed_viewer, model_template_overlay, readme_provenance, resizable_panels, selectable_axes, selection_export, spectrum_2d_viewer, spectrum_viewer_1d, static_sed_png | Per-grating FITS product table -> detail products block; one A.catalog per category (6927 map:775-796); label-toggle API fallbacks in aladin adapter. |
| `nirspec_6927_manifold.html` | manifold | multi cat cats num2 mags2 num3 mani mags3f sky skycat spec img sedimg iso | 18 | brush_circle, brush_lasso, catalog_markers_on_sky, cdn_fallback, collapsible_panels, dark_light_theme, extinction_vectors_on_ccd, hips_layer_stacking, isochrones_or_tracks, labeled_features_overlay, lightbox, linked_selection, live_sed_viewer, readme_provenance, resizable_panels, selection_export, sky_map_aladin, static_sed_png | Limit-aware markers; missing-data fade (FADE_W) matches GC 0.12 weight fade; links to cogas/h2/pv/sed by id. |
| `nirspec_6927_specviewer.html` | spectra [list | spectrum1d + spectrum2d | detail] | multi cat cats spec spec2d sky skycat mags3f | 21 | av_slider, catalog_markers_on_sky, cdn_fallback, class_coloring, collapsible_panels, dark_light_theme, detail_panel, extinction_vectors_on_ccd, hips_layer_stacking, hover_preview, keyboard_nav, linked_selection, live_sed_viewer, model_template_overlay, readme_provenance, resizable_panels, responsive_mobile, selection_export, sky_map_aladin, source_list_search, spectrum_2d_viewer | Hash written on every load -> replaceState only on change; per-grating segment toggle; spectrum zoom from NIRSpec_6927_SpecViewer.py:288-327,533-615. |
| `nirspec_6927_colorcolor.html` | explorer [scatter grid | list | detail] | multi cat cats num2 mags2 num3 mags3f sky skycat iso dens spec | 28 | av_slider, brush_circle, brush_lasso, brush_rect, brush_set_ops, catalog_markers_on_sky, cdn_fallback, class_coloring, collapsible_panels, dark_light_theme, deep_link_hash, extinction_vectors_on_ccd, hips_layer_stacking, isochrones_or_tracks, keyboard_nav, labeled_features_overlay, lazy_data_loading, linked_selection, live_sed_viewer, model_template_overlay, readme_provenance, resizable_panels, responsive_mobile, selectable_axes, selection_export, sky_map_aladin, source_list_search, spectrum_viewer_1d | s.stem.match(/_o(\d{3})_/) crash removed: observation is a category column. |
| `nirspec_6927_icecolumns.html` | explorer [log-log scatter | list | detail] | multi cat cats num2 mags2 num3 mags3f sky skycat img | 24 | av_slider, brush_circle, brush_lasso, brush_rect, brush_set_ops, catalog_markers_on_sky, cdn_fallback, collapsible_panels, dark_light_theme, extinction_vectors_on_ccd, hips_layer_stacking, keyboard_nav, labeled_features_overlay, lazy_data_loading, linked_selection, live_sed_viewer, model_template_overlay, readme_provenance, resizable_panels, responsive_mobile, selectable_axes, selection_export, sky_map_aladin, source_list_search | Log axes with decade ticks, error bars, 1:1 reference line, limit arrows; ice column densities stored f32 (dynamic range); empty-pair guard in axes. |
| `nirspec_6927_cogas.html` | explorer [list | detail | scatter T_ex vs log N] | multi cat cats num2 num3 img sky skycat spec | 22 | av_slider, brush_circle, brush_lasso, brush_rect, brush_set_ops, catalog_markers_on_sky, cdn_fallback, collapsible_panels, dark_light_theme, hips_layer_stacking, hover_preview, labeled_features_overlay, lazy_data_loading, linked_selection, model_template_overlay, readme_provenance, resizable_panels, selectable_axes, selection_export, sky_map_aladin, source_list_search, spectrum_viewer_1d | Verdict/evidence prose as text columns in exact row chunks; six-panel PNG image role; default selection = top comb_snr. |
| `nirspec_6927_h2.html` | narrative (report) with embedded scatter/table panes | cat cats num2 img | 13 | brush_circle, brush_lasso, brush_rect, brush_set_ops, collapsible_panels, dark_light_theme, filter_toggles, hover_preview, keyboard_nav, lazy_data_loading, readme_provenance, selection_export, source_list_search | VERDICTS imported from NIRSpec_6927_H2 (single source); tables -> table pane; rotation-diagram scatter only when the line table is supplied (else caps.disable reason); N=8 -> T0. |
| `nirspec_6927_pvgallery.html` | gallery (roles L3/L2/no-bgsub) | items img cat cats sky skycat | 13 | catalog_markers_on_sky, cdn_fallback, class_coloring, collapsible_panels, dark_light_theme, detail_panel, hips_layer_stacking, hover_preview, lazy_data_loading, readme_provenance, responsive_mobile, selection_export, sky_map_aladin | Lightbox clears hash on close; FITS cube links as products block. |
| `nirspec_6927_sedgallery.html` | gallery (+live SED, multi-image items) | items img sedimg multi cat cats num2 mags2 num3 mags3f sky skycat | 27 | av_slider, brush_circle, brush_lasso, brush_rect, brush_set_ops, catalog_markers_on_sky, cdn_fallback, class_coloring, cmd_ccd_scatter, collapsible_panels, dark_light_theme, detail_panel, extinction_vectors_on_ccd, filter_toggles, hips_layer_stacking, hover_preview, labeled_features_overlay, lazy_data_loading, linked_selection, live_sed_viewer, model_template_overlay, readme_provenance, resizable_panels, responsive_mobile, selectable_axes, selection_export, sky_map_aladin | All pointings per item (multi-image); stem regex removed. |
| `nirspec_6927_msata_finding.html` | narrative [beats | sticky sky] | sky cat cats skycat img | 10 | collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, detail_panel, filter_toggles, hips_layer_stacking, lazy_data_loading, selection_export, source_list_search | 15 scenes from NIRSpec_6927_MSATA.py:1194-1220; text-to-sky highlights; scene exit removes overlays (fixes hlOverlay leak); cos(dec) acquisition boxes; suppressView keeps user zoom; #beat= hash. |
| `nirspec_6927_refstar_map.html` | explorer [sky | list | detail] | multi cat cats sky skycat num2 mags2 | 17 | brush_circle, brush_lasso, brush_rect, brush_set_ops, cmd_ccd_scatter, collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, keyboard_nav, lazy_data_loading, linked_selection, readme_provenance, resizable_panels, responsive_mobile, selection_export, source_list_search | VVV base + JWST overlay alpha; click rings at 2 arcsec, 3 arcsec, isolation, spike radii -> sky rings tool; per-quadrant table -> summary block; refstar_map_data.json has no writer in the repo: adapter rebuilds it from NIRSpec_6927_RefStar inputs or Tier A extracts it; </ escaping in shell. |
| `refstar_check/index.html` | gallery (contact sheets) + readme | items img | 8 | collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, filter_toggles, hover_preview, lazy_data_loading, source_list_search | Procedure/criteria/reject table -> readme blocks; all sheet variants (v20, pool_queued, selected_v18) as facets. |
| `pngs/gallery_6927.html` | gallery (one gallery, roles 6927/2221/1182) | items img sedimg cat sky skycat | 16 | catalog_markers_on_sky, cdn_fallback, collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, detail_panel, filter_toggles, hips_layer_stacking, hover_preview, lazy_data_loading, readme_provenance, selection_export, sky_map_aladin, source_list_search, static_sed_png | Three legacy autoindex files -> one page with role presets; redirect stubs only under --replace. |
| `public brick_2221/nirspec_6927/gallery.html` | gallery (public dirgallery) | items img sedimg | 11 | collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, filter_toggles, hover_preview, lazy_data_loading, readme_provenance, responsive_mobile, source_list_search, static_sed_png | Public page: may link to the secure portal, holds no secure data; predates observations, caption says so; exposure classifier refuses any secure data reference. |

### 14.6 robitaille (4 rows)

| legacy page | archetype | flags | caps to fill (n) | capabilities to fill | special handling |
|---|---|---|---|---|---|
| `manifold.html` | manifold tier T1, CPU projection (75-83 base columns) | multi cat cats num2 mags2 num3 mani mags3f sky skycat img sedimg dens | 16 | av_slider, brush_circle, catalog_markers_on_sky, cdn_fallback, collapsible_panels, dark_light_theme, extinction_vectors_on_ccd, hips_layer_stacking, labeled_features_overlay, lazy_data_loading, linked_selection, model_template_overlay, readme_provenance, resizable_panels, selection_export, sky_map_aladin | 28,248 rows; 54 mags + 21 physical stored, 172 colours virtual (folded into base weights); distance-modulus slider (uniform mag shift folded into camera offset); FRIED category; >30 mag at 8 kpc auto-hide as client rule; Tier B caches 4-h read_geometry loop in ctx.cache; optional observed-catalog context density. |
| `index.html` | landing | search | 6 | collapsible_panels, dark_light_theme, deep_link_hash, keyboard_nav, lazy_data_loading, source_list_search | Geometry count from data; build date from manifest. |
| `imaging/index.html` | gallery (+param scatter) | items img cat num2 num3 | 16 | brush_circle, brush_lasso, brush_rect, brush_set_ops, collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, detail_panel, filter_toggles, hover_preview, keyboard_nav, lightbox, readme_provenance, selectable_axes, selection_export | 500 x 7.25 MB PNGs: thumbnails via portalkit thumbs (matplotlib optional extra); parameter range filters; NaN sorts last; links to manifold rows by model id; served via symlink web/public/robitaille_models/imaging. |
| `imaging/gallery.html` | gallery (merged into imaging page) | items img | 10 | collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, filter_toggles, hover_preview, lazy_data_loading, readme_provenance, responsive_mobile, source_list_search | Broken jQuery page titled ACES mosaics: dropped from nav, legacy file left in place; unified imaging gallery covers it. |

### 14.7 sgrb2 (4 rows)

| legacy page | archetype | flags | caps to fill (n) | capabilities to fill | special handling |
|---|---|---|---|---|---|
| `index.html` | landing | search | 6 | collapsible_panels, deep_link_hash, keyboard_nav, lazy_data_loading, responsive_mobile, source_list_search | Hard-coded 464,489 and 500-vs-2105 text read from manifests; manifold card links a secure URL -> check warns public->secure link and adds login-required badge. |
| `sgrb2_map.html` | explorer [sky | list] | sky cat cats skycat dens | 15 | catalog_markers_on_sky, class_coloring, collapsible_panels, dark_light_theme, deep_link_hash, density_rendering, detail_panel, filter_toggles, hover_preview, keyboard_nav, lazy_data_loading, readme_provenance, responsive_mobile, selection_export, source_list_search | Clicks before Aladin ready are queued; BASE/OVERLAY split from SgrB2_Portal_Map.py:39-76; markers via skyWindow LOD. |
| `sgrb2_map_ccd.html` | explorer [sky | scatter CMD | scatter CCD | list | detail] tier T2 | multi cat cats num2 mags2 num3 mags3f sky skycat dens iso | 21 | av_slider, class_coloring, collapsible_panels, dark_light_theme, deep_link_hash, density_rendering, detail_panel, extinction_vectors_on_ccd, filter_toggles, hips_layer_stacking, hover_preview, isochrones_or_tracks, keyboard_nav, labeled_features_overlay, lazy_data_loading, live_sed_viewer, model_template_overlay, readme_provenance, resizable_panels, selection_export, source_list_search | Tier A references existing data/mapccd/*.f32.gz in place (enc f32, linear order); Tier B re-encodes u16q with LOD order + pre-binned density; 0.2-99.8 percentile axis limits default; GPU projection (support <= 28). |
| `sgrb2_seds/index.html` | gallery (+live SED, +mini sky) | items img sedimg multi cat cats num2 mags2 num3 mags3f sky skycat | 29 | av_slider, brush_circle, brush_lasso, brush_rect, brush_set_ops, catalog_markers_on_sky, cdn_fallback, class_coloring, cmd_ccd_scatter, collapsible_panels, dark_light_theme, deep_link_hash, detail_panel, extinction_vectors_on_ccd, filter_toggles, hips_layer_stacking, hover_preview, keyboard_nav, labeled_features_overlay, lightbox, linked_selection, live_sed_viewer, model_template_overlay, readme_provenance, resizable_panels, selectable_axes, selection_export, sky_map_aladin, source_list_search | PNGs exist for 500 of 2105; items without PNG show live SED only; flux <= 0 drawn as upper limits. |

### 14.8 sickle_brick (11 rows)

| legacy page | archetype | flags | caps to fill (n) | capabilities to fill | special handling |
|---|---|---|---|---|---|
| `sickle_miri_sed_map.html` | explorer [list | sky | detail + sed + scatter x2] | multi cat cats num2 mags2 num3 mags3f sky skycat fits img sedimg | 17 | av_slider, brush_circle, brush_lasso, brush_rect, brush_set_ops, collapsible_panels, dark_light_theme, extinction_vectors_on_ccd, keyboard_nav, labeled_features_overlay, lazy_data_loading, live_sed_viewer, model_template_overlay, readme_provenance, resizable_panels, selectable_axes, selection_export | SESHAT probability bars -> detail bars block; F2550W FITS overlay URL computed through the exposure map (fixes relpath escape); catalog downloads as products; Sickle declared public to match current exposure (open question: intent may be private). |
| `sickle_miri_sed_manifold.html` | manifold | multi cat cats num2 mags2 num3 mani mags3f sky skycat img sedimg | 19 | av_slider, brush_circle, brush_lasso, catalog_markers_on_sky, cdn_fallback, collapsible_panels, dark_light_theme, extinction_vectors_on_ccd, hips_layer_stacking, labeled_features_overlay, lazy_data_loading, lightbox, linked_selection, live_sed_viewer, model_template_overlay, readme_provenance, resizable_panels, selection_export, sky_map_aladin | o prefix and drift default caught by validation; F444W added; colour help text from config. |
| `brick_2221/sed_figures_f1500w_all/gallery.html` | explorer (near 1:1 with legacy) | multi cat cats num2 mags2 num3 mags3f sky skycat fits img sedimg | 7 | cross_page_nav, dark_light_theme, extinction_vectors_on_ccd, keyboard_nav, readme_provenance, selectable_axes, selection_export | Adapter calls build_extended_templates (to 26 um); k55 exposed; template normalization bands exclude MIRI excess; brush ignores hidden points; dragResize from Brick2221_F1500W_SEDPortal.py:704-767 -> layout splitters with pk layout keys. |
| `brick_2221/sed_figures_f1500w_all/gallery_legacy.html` | gallery (dirgallery, joined to F1500W catalog) | items img sedimg cat sky skycat | 16 | catalog_markers_on_sky, cdn_fallback, collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, detail_panel, filter_toggles, hips_layer_stacking, hover_preview, lazy_data_loading, readme_provenance, responsive_mobile, selection_export, sky_map_aladin, source_list_search | Same unified page as sed_figures_f1500w_all gallery role; legacy file untouched. |
| `brick_2221/sed_figures_top100_f1500w/gallery.html` | gallery (dirgallery, top100 preset) | items img sedimg cat sky skycat | 16 | catalog_markers_on_sky, cdn_fallback, collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, detail_panel, filter_toggles, hips_layer_stacking, hover_preview, lazy_data_loading, readme_provenance, responsive_mobile, selection_export, sky_map_aladin, source_list_search | Nested duplicate subdir excluded by pattern; top100 is a facet preset. |
| `brick_2221/nirspec_6927/gallery.html` | gallery (dirgallery) | items img sedimg | 10 | collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, filter_toggles, hover_preview, lazy_data_loading, readme_provenance, responsive_mobile, source_list_search | Same physical file as nirspec6927 #14; one unified page serves both inventory rows. |
| `brick_2221/ccds_cmds/gallery.html` | gallery (one ccds_cmds page, facet preset) | items img | 10 | collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, filter_toggles, hover_preview, lazy_data_loading, readme_provenance, responsive_mobile, source_list_search | Legacy regex /\.png$/ becomes a facet preset of one ccds_cmds gallery; redirect stub only under --replace. |
| `brick_2221/ccds_cmds/gallery_cmd.html` | gallery (one ccds_cmds page, facet preset) | items img | 10 | collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, filter_toggles, hover_preview, lazy_data_loading, readme_provenance, responsive_mobile, source_list_search | Legacy regex /cmd.*\.png$/ becomes a facet preset of one ccds_cmds gallery; redirect stub only under --replace. |
| `brick_2221/ccds_cmds/gallery_cmd_iso.html` | gallery (one ccds_cmds page, facet preset) | items img | 10 | collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, filter_toggles, hover_preview, lazy_data_loading, readme_provenance, responsive_mobile, source_list_search | Legacy regex /cmd.*iso\.png$/ becomes a facet preset of one ccds_cmds gallery; redirect stub only under --replace. |
| `brick_2221/ccds_cmds/gallery_cmd_lowext.html` | gallery (one ccds_cmds page, facet preset) | items img | 10 | collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, filter_toggles, hover_preview, lazy_data_loading, readme_provenance, responsive_mobile, source_list_search | Legacy regex /cmd_iso.*lowext\.png$/ becomes a facet preset of one ccds_cmds gallery; redirect stub only under --replace. |
| `brick_2221/ccds_cmds/gallery_blue405410.html` | gallery (one ccds_cmds page, facet preset) | items img | 10 | collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, filter_toggles, hover_preview, lazy_data_loading, readme_provenance, responsive_mobile, source_list_search | Legacy regex /blue405.*\.png$/ becomes a facet preset of one ccds_cmds gallery; redirect stub only under --replace. |

### 14.9 spherex (6 rows)

| legacy page | archetype | flags | caps to fill (n) | capabilities to fill | special handling |
|---|---|---|---|---|---|
| `index.html` | landing | search | 7 | collapsible_panels, dark_light_theme, deep_link_hash, keyboard_nav, lazy_data_loading, responsive_mobile, source_list_search | Manifold tile generated (was hand-added); README deployed (was excluded by rsync); deploy replaces deploy_portal.sh rsync with portalkit deploy. |
| `colours.html` | explorer [CCD + marginal CMD | list | detail + spectrum] | multi cat cats num2 mags2 num3 mags3f sky skycat spec img | 14 | brush_circle, brush_lasso, brush_set_ops, catalog_markers_on_sky, cdn_fallback, collapsible_panels, dark_light_theme, extinction_vectors_on_ccd, hips_layer_stacking, live_sed_viewer, readme_provenance, selection_export, sky_map_aladin, source_list_search | Yoo W51 locus + divider -> reference polyline overlay; COVERAGE_FRAC flags; IRS dotted as second segment set; shift-drag pan bug replaced by spectrum1d pane. |
| `browse.html` | spectra [list | spectrum1d | detail + CCD context] | multi cat cats spec sky skycat num2 mags2 | 20 | av_slider, brush_circle, brush_lasso, brush_rect, brush_set_ops, catalog_markers_on_sky, cdn_fallback, class_coloring, cmd_ccd_scatter, collapsible_panels, dark_light_theme, hips_layer_stacking, hover_preview, labeled_features_overlay, linked_selection, model_template_overlay, readme_provenance, resizable_panels, selection_export, sky_map_aladin | Sexagesimal cone search ported into list search; wavelength range presets. |
| `map.html` | explorer [sky | list | detail] | multi cat cats sky skycat num2 mags2 spec | 21 | av_slider, brush_circle, brush_lasso, brush_rect, brush_set_ops, cdn_fallback, cmd_ccd_scatter, collapsible_panels, dark_light_theme, deep_link_hash, hover_preview, keyboard_nav, labeled_features_overlay, lazy_data_loading, linked_selection, model_template_overlay, readme_provenance, resizable_panels, selection_export, source_list_search, spectrum_viewer_1d | Planck 857 overlay opacity; 60-region go-to list -> hips bookmarks. |
| `sed.html` | spectra in SED mode (log-log, photometry + spectra) | multi cat cats spec mags3f img sedimg sky skycat num2 mags2 | 21 | av_slider, brush_circle, brush_lasso, brush_rect, brush_set_ops, catalog_markers_on_sky, cdn_fallback, class_coloring, cmd_ccd_scatter, collapsible_panels, dark_light_theme, extinction_vectors_on_ccd, hips_layer_stacking, labeled_features_overlay, lightbox, linked_selection, model_template_overlay, readme_provenance, resizable_panels, selection_export, sky_map_aladin | nuFnu/Fnu toggle; ALMA/Herschel/>=60 um/uncrowded chips; confusion notes as text columns. |
| `spherex_manifold.html` | manifold | multi cat cats num2 mags2 num3 mani mags3f sky skycat spec dens | 13 | brush_circle, catalog_markers_on_sky, collapsible_panels, dark_light_theme, extinction_vectors_on_ccd, filter_toggles, hips_layer_stacking, labeled_features_overlay, linked_selection, live_sed_viewer, readme_provenance, resizable_panels, source_list_search | Regex scrape of colours.html removed: one shared catalog bundle feeds all six pages. |

### 14.10 w51 (11 rows)

| legacy page | archetype | flags | caps to fill (n) | capabilities to fill | special handling |
|---|---|---|---|---|---|
| `index.html` | landing | search | 5 | collapsible_panels, deep_link_hash, keyboard_nav, lazy_data_loading, source_list_search | Row-count table of index/*.ecsv and rebuild order become readme blocks; global id/alias search across portal datasets; archive_colors card points at the jwst_archive W51 build. |
| `map.html` | explorer [list | sky | detail] | multi cat cats num2 mags2 num3 mags3f sky skycat img sedimg dens | 20 | av_slider, brush_circle, brush_lasso, brush_rect, brush_set_ops, class_coloring, cmd_ccd_scatter, collapsible_panels, density_rendering, extinction_vectors_on_ccd, filter_toggles, labeled_features_overlay, lazy_data_loading, linked_selection, live_sed_viewer, model_template_overlay, resizable_panels, selectable_axes, selection_export, source_list_search | GTC slit polygons per field and tier with marker shapes square/triangle/cross and suspected-artifact rings -> overlays.json polygons+markers (shape per category); __gtc detail template (spectrum PNG, 2D PNG, flag docs); catalog provenance tooltip from FITS headers -> manifest provenance.catalogs; overlays off/defaults buttons -> sky layer presets; positional-name hash both ways via aliases. |
| `map_ccd.html` | explorer [sky | scatter GLIMPSE CCD | scatter JWST CCD | scatter CMD | detail] tier T1 | multi cat cats num2 mags2 num3 mags3f sky skycat dens flags | 17 | av_slider, class_coloring, collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, density_rendering, detail_panel, extinction_vectors_on_ccd, hips_layer_stacking, keyboard_nav, labeled_features_overlay, lazy_data_loading, live_sed_viewer, model_template_overlay, resizable_panels, source_list_search | Two catalog datasets with no shared ids (wide, jwst) on one page, each with its own selection mask; centimag ints -> u16q (step 0.001 mag); client S/N threshold and brightest-N cap need error columns (u8log); CSV import by id->alias->0.3 arcsec position; MAXMARK 20000 replaced by skyWindow viewport LOD markers (cap 20k, uniform subsample) so the 81k marker view is preserved. |
| `colorcolor.html` | explorer [scatter grid 3x2 | list | detail] | multi cat cats num2 mags2 num3 mags3f sky skycat iso dens | 17 | catalog_markers_on_sky, cdn_fallback, collapsible_panels, deep_link_hash, detail_panel, hips_layer_stacking, hover_preview, keyboard_nav, labeled_features_overlay, lazy_data_loading, live_sed_viewer, model_template_overlay, readme_provenance, responsive_mobile, selectable_axes, sky_map_aladin, source_list_search | Hexbin PNGs kept as raster layers with data extents; client density from the 53k background (context) dataset is the default; filled/hollow quality rule -> style.hollow_if expression; MIST isochrones + G23 vector + rAF A_V; loaded-selection chips -> saved selections; per-panel fullscreen -> pane pop-out; plot ground reads theme tokens (fixes dark ground in light theme). |
| `gallery.html` | gallery [facets | cards | detail, mini sky, mini scatter] | items img sedimg multi cat cats num2 mags2 num3 mags3f sky skycat dens | 25 | av_slider, brush_circle, brush_lasso, brush_rect, brush_set_ops, catalog_markers_on_sky, cdn_fallback, cmd_ccd_scatter, collapsible_panels, cross_page_nav, deep_link_hash, density_rendering, extinction_vectors_on_ccd, hips_layer_stacking, hover_preview, keyboard_nav, labeled_features_overlay, linked_selection, live_sed_viewer, model_template_overlay, readme_provenance, resizable_panels, selectable_axes, selection_export, sky_map_aladin | Two image roles per item (sed, cutout); 246 external sources merged with origin category; criterion chips with counts; sort by m210/avx/nb; virtualized grid removes the 600-card cap. |
| `spectra.html` | spectra [list | spectrum1d + spectrum2d | detail] | multi cat cats spec spec2d img sky skycat | 12 | catalog_markers_on_sky, cdn_fallback, class_coloring, collapsible_panels, hips_layer_stacking, hover_preview, lazy_data_loading, linked_selection, resizable_panels, selection_export, sky_map_aladin, source_list_search | Four normalization modes -> pks1 channels/variants; He I dashed-where-no-data -> feature requires_coverage; caveat banners -> prose blocks; reddened CK template sliced to 1.99-2.40 um; spec2d in PNG mode (spectra2d/*.png with declared wavelength extents, x-linked to 1D); keyboard walks the filtered list (fixes ArrowUp/Down bug). |
| `spectra_candidate.html` | spectra [list | spectrum1d | detail] | multi cat cats spec sky skycat | 13 | av_slider, catalog_markers_on_sky, cdn_fallback, class_coloring, collapsible_panels, hips_layer_stacking, hover_preview, lazy_data_loading, linked_selection, model_template_overlay, resizable_panels, selection_export, sky_map_aladin | BLOCKED input: reads another session tmp scratchpad; build refuses until inputs are copied to /orange/adamginsburg/w51/gtc/candidate_xb5/ with user approval (or --allow-tmp-inputs for staging only); quality-bit ranges -> pks1 mask_runs; shipped-spectrum overlay -> second spectrum set joined by alias; md5 manifest -> provenance; private claude.ai report link flagged by check. |
| `w51_manifold.html` | manifold (+spectrum PNG thumb, +sky cutout) | multi cat cats num2 mags2 num3 mani mags3f sky skycat img | 20 | av_slider, brush_circle, brush_lasso, catalog_markers_on_sky, cdn_fallback, cmd_ccd_scatter, collapsible_panels, dark_light_theme, extinction_vectors_on_ccd, hips_layer_stacking, labeled_features_overlay, lazy_data_loading, lightbox, linked_selection, live_sed_viewer, model_template_overlay, readme_provenance, resizable_panels, selection_export, sky_map_aladin | Builder validation removes the o${o} chip prefix and A_V/ice defaults that name absent dims; limit arrows from k codes (sparse limits table); 235 rows -> tier T0 canvas2d with labels. |
| `w51_catalog_manifold.html` | manifold tier T1 (binary bundle) | multi cat cats num2 mags2 num3 mani mags3f sky skycat img sedimg dens | 17 | av_slider, brush_circle, catalog_markers_on_sky, cdn_fallback, collapsible_panels, dark_light_theme, density_rendering, extinction_vectors_on_ccd, hips_layer_stacking, labeled_features_overlay, lazy_data_loading, lightbox, linked_selection, model_template_overlay, readme_provenance, resizable_panels, sky_map_aladin | Identity migration: ids become positional W51-JW-<hhmmss.ss+ddmmss.s>, legacy W51-000123 kept as aliases so old hashes resolve; S/N tighten applies only to filters in the active projection; MIRI beam-crowding dims; fullscreen SED dialog; seds_catalog/*.png image role; per-frame limitVector dropped (no limit kinds). |
| `wavelength.html` | wavelength [sky with rung rail | rung notes] | sky cat skycat | 9 | catalog_markers_on_sky, collapsible_panels, dark_light_theme, deep_link_hash, detail_panel, hover_preview, readme_provenance, selection_export, source_list_search | Asymmetric cross-fade (lower rung opaque, upper ramps) from W51_Portal_Wavelength.py:239-264; opacity feature detection with step fallback; rungs ordered by red channel; hash #rung=&ra=&dec=&fov= mapped. |
| `archive_colors/index.html` | explorer (jwst_archive adapter, context dataset = W51 photometry) | multi cat cats num2 mags2 num3 mags3f sky skycat spec dens | 28 | av_slider, brush_circle, brush_lasso, brush_rect, brush_set_ops, catalog_markers_on_sky, cdn_fallback, class_coloring, collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, detail_panel, extinction_vectors_on_ccd, filter_toggles, hips_layer_stacking, hover_preview, keyboard_nav, labeled_features_overlay, linked_selection, live_sed_viewer, model_template_overlay, resizable_panels, responsive_mobile, selectable_axes, selection_export, sky_map_aladin, source_list_search | Built by WP28 from the 1902-row archive fluxes table; templates copied into data/ when ../templates is not reachable from the secure tree; keeps its own copy of the spectra shards (the W51 secure area uses a different htpasswd than web/secure/jwst, so a foreign reference would answer 401 to W51-only accounts); verify checks cross-tree URL returns 401 without credentials. |

### 14.11 wd2 (6 rows)

| legacy page | archetype | flags | caps to fill (n) | capabilities to fill | special handling |
|---|---|---|---|---|---|
| `map_ccd.html` | explorer [sky | scatter x2 | list | detail] tier T1 | multi cat cats num2 mags2 num3 mags3f sky skycat iso dens | 20 | av_slider, class_coloring, collapsible_panels, cross_page_nav, dark_light_theme, deep_link_hash, density_rendering, detail_panel, extinction_vectors_on_ccd, filter_toggles, isochrones_or_tracks, keyboard_nav, labeled_features_overlay, lazy_data_loading, live_sed_viewer, model_template_overlay, readme_provenance, resizable_panels, selection_export, source_list_search | HST Sabbi Im/V/H/J as mag columns with filter metadata; Sabbi->EWOCS->catalog match stays in legacy code (sabbi_jwst_hubble_match imported); Sabbi YSO preselect -> saved selection; MIST tracks via build_isochrone_track. |
| `wd2_colorcolor.html` | explorer [curated scatter grid | list | detail] | multi cat cats num2 mags2 num3 mags3f sky skycat iso dens img sedimg | 24 | av_slider, brush_circle, brush_set_ops, catalog_markers_on_sky, cdn_fallback, class_coloring, collapsible_panels, dark_light_theme, deep_link_hash, extinction_vectors_on_ccd, filter_toggles, hips_layer_stacking, isochrones_or_tracks, keyboard_nav, labeled_features_overlay, lazy_data_loading, lightbox, live_sed_viewer, model_template_overlay, readme_provenance, resizable_panels, selection_export, sky_map_aladin, source_list_search | Histogram mag-cut brush -> hist pane range brush; MIRI panels need the remap_miri_for_new_catalog join; label toggle; box select limited to in-frame points; #name hash honored. |
| `wd2_manifold.html` | manifold tier T1, CPU projection (support > 28 base columns) | multi cat cats num2 mags2 num3 mani mags3f sky skycat iso dens img sedimg spec | 9 | brush_circle, catalog_markers_on_sky, dark_light_theme, extinction_vectors_on_ccd, hips_layer_stacking, labeled_features_overlay, linked_selection, readme_provenance, resizable_panels | 164 dims exceed GPU MAXD=28 -> XYRenderer with worker sparse projection; preselects (hasSpec, fullCov, iceOutlier, EWOCS-IX x3, clusters) -> saved selections; 55 NIRSpec spectra from shared 2640 shards; 25-survey cutout selector -> sky layers; A_V default checked by validation; ewocs9 null leaves no dead buttons. |
| `sabbi.html` | manifold | multi cat cats num2 mags2 num3 mani mags3f sky skycat dens | 17 | av_slider, brush_circle, catalog_markers_on_sky, cdn_fallback, collapsible_panels, dark_light_theme, extinction_vectors_on_ccd, hips_layer_stacking, labeled_features_overlay, lazy_data_loading, linked_selection, live_sed_viewer, model_template_overlay, readme_provenance, resizable_panels, selection_export, sky_map_aladin | Fork of wd2_manifold: placeholder text and Detection quality heading come from config; drift-group validation; light-curve viewer recorded as disabled (no time-series data); isochrones enabled only if HST MIST bands are supplied (else caps.disable reason). |
| `wd2_sedgallery.html` | gallery (+mini scatter, +sky, +live SED) | items img sedimg multi cat cats num2 mags2 num3 mags3f sky skycat dens | 25 | av_slider, brush_circle, brush_lasso, brush_rect, brush_set_ops, catalog_markers_on_sky, cdn_fallback, class_coloring, cmd_ccd_scatter, collapsible_panels, dark_light_theme, density_rendering, detail_panel, extinction_vectors_on_ccd, filter_toggles, hips_layer_stacking, hover_preview, labeled_features_overlay, linked_selection, live_sed_viewer, model_template_overlay, resizable_panels, selectable_axes, selection_export, sky_map_aladin | Numeric range filters on bb_T/bb_av/chi2/nfilt; subtitle MINFILT computed from data; BB guide curve offered as SED model overlay; lightbox from WD2_Portal_SEDGallery.py:152-194. |
| `wd2_wavelength_explorer.html` | wavelength | sky cat skycat | 11 | catalog_markers_on_sky, cdn_fallback, cross_page_nav, dark_light_theme, detail_panel, hover_preview, keyboard_nav, lazy_data_loading, readme_provenance, selection_export, source_list_search | 22 rungs from WAVELENGTHS; ?speed= mapped; share button -> copy-link; draggable panel -> splitter; jQuery removed. |

## 15. Work packages, waves and gates

File sets are disjoint across work packages (`wps.py` asserts this; 276 paths). A work package edits only its own files.
A path ending in `/` means the WP owns the whole directory. A change needed in a file owned by an earlier WP is filed as an
issue against that WP's contract and lands as a follow-up commit by the owning WP. Frozen sections (marked **[FROZEN]**)
change only through a version bump of `portalkit/version.py` and the matching schema id.

Waves:

| Wave | WPs | Gate at end |
|---|---|---|
| 0 | WP00 | G0: repo imports; `pytest -m "not e2e"` green; constants sync green; vendored sha256 recorded |
| 1-core | WP01, WP03, WP04, WP06 (parallel), then WP02, WP05, WP07 | G1: codec goldens decode identically in Python and node; schema validators pass on the section 4.3 example; deploy dry-run on fake tree |
| 2-core | WP08, WP09, WP10, WP11, WP12, WP13 (parallel) | G2: Level 1 synthetic smoke at N = 8/2k/50k/1M/5M green; caps matrix green; Level 3 budgets met; `json-legacy` record format disabled from here on |
| adapters 1 | WP20, WP21, WP22 (parallel) | G3: adapter contract frozen; each port passes Level 4 acceptance and `deploy` to `unified/` with `verify --http` |
| adapters 2-6 | WP23; then WP24, WP25, WP26; then WP27, WP28; then WP29, WP30; then WP31 | G4 per family: Level 4 + Level 5 green, contact sheet reviewed by the user before any `--replace` |

`--replace` is never run by an implementation agent. The user runs it per family after reviewing the G4 contact sheet.


### WP00 — Skeleton, contracts, vendoring (serial, first)

- depends_on: (none)
- files:
  - `pyproject.toml`
  - `README.md`
  - `VENDORED.md`
  - `.gitignore`
  - `portalkit/__init__.py`
  - `portalkit/version.py`
  - `portalkit/constants.py`
  - `portalkit/schema.py`
  - `adapters/__init__.py`
  - `web/js/core/constants.js`
  - `web/js/vendor/gc/gl/renderer.js`
  - `web/js/vendor/gc/gl/shaders.js`
  - `web/js/vendor/gc/gl/colormaps.js`
  - `web/js/vendor/gc/config/style.js`
  - `web/js/vendor/gc/math/frame.js`
  - `web/js/vendor/gc/math/linalg.js`
  - `web/js/vendor/gc/workers/kernel.js`
  - `web/js/vendor/gc/app/pick.js`
  - `tests/conftest.py`
  - `tests/py/test_no_broad_except.py`
  - `tests/py/test_schema.py`
  - `tests/py/test_constants_sync.py`
- description: git init the repo at /blue/adamginsburg/adamginsburg/repos/portalkit. Write pyproject (numpy, astropy; extras thumbs/extinction/test; console script portalkit). Write constants.py and constants.js exactly as in section 6.12. Write schema.py validators for every JSON document in sections 4-5 (pk-page/1, pk-portal/1, pk-catalog/1, pk-records/1, pk-images/1, pk-hips/1, pk-overlays/1, pk-narrative/1, pk-templates/1, pk-filters/1, pk-extinction/1, pk-isochrones/1, pk-features/1, products/1, pk-deploy/1); each validator returns a list of errors. Copy the GC modules byte-for-byte and apply only the 3-line theme patch in renderer.js (section 6.11); record sha256 values in VENDORED.md. Write the test markers, the AST no-broad-except scanner (bare except, or Exception/BaseException without re-raise) and the constants sync test (it also re-hashes vendored files). Commit.

### WP01 — Python bundle writers and codecs

- depends_on: WP00
- files:
  - `portalkit/bundle/__init__.py`
  - `portalkit/bundle/codec.py`
  - `portalkit/bundle/catalog.py`
  - `portalkit/bundle/order.py`
  - `portalkit/bundle/records.py`
  - `portalkit/bundle/density.py`
  - `portalkit/bundle/images.py`
  - `portalkit/bundle/writer.py`
  - `docs/bundle.md`
  - `tests/py/test_codec.py`
  - `tests/py/test_catalog_bundle.py`
  - `tests/py/test_order.py`
  - `tests/py/test_records.py`
  - `tests/py/test_density.py`
  - `tests/golden/codec/`
- description: Implement section 5 exactly. codec.py holds gz() verbatim from GCTreasury_Portal_Manifold.py (level 9, mtime 0, OS byte; filtered variant), all encoders in the section 5.3 table, the shuffles, str and sparse, and dim_stats/log_dim_stats. catalog.py turns a CatalogSpec into the manifest, column files, chunking, ids/aliases, limits, row chunks (exact_rows rule) and the auto-encoding table. order.py implements the LOD permutation and the tile/morton sort with _src_index. records.py implements the pks1/pki1/json/sedcurve writers and the shard packer (target 256 kB, max 512 kB, layout file fallback). density.py writes pre-binned u16log grids and binned statistics. writer.py handles content-hash naming (h8 = sha256 of bytes) in .bin files, atomic writes inside staging, and the budget ledger. Golden fixtures are bytes plus expected decoded JSON consumed by WP05 node tests.

### WP02 — Physics resources (filters, extinction, templates, isochrones, features, SED)

- depends_on: WP00, WP01
- files:
  - `portalkit/physics/__init__.py`
  - `portalkit/physics/filters.py`
  - `portalkit/physics/extinction.py`
  - `portalkit/physics/templates.py`
  - `portalkit/physics/isochrones.py`
  - `portalkit/physics/features.py`
  - `portalkit/physics/sed.py`
  - `portalkit/data/filters_registry.ecsv`
  - `portalkit/data/g23_alav.ecsv`
  - `portalkit/data/features/`
  - `tests/py/test_physics.py`
  - `tests/golden/physics/`
- description: Build the filter registry from the existing SVO caches and jwst_vega_zeropoints.ecsv: key, label, instrument, wl_um, weff_um, zp_jy, system. Compute G23 A_lambda/A_V per filter for R_V 3.1 and 5.5; dust_extinction is optional (except ImportError), and a vendored ecsv is the default. Split jwst_template_spectra.json into one pks1 file per (template, grid), about 20 kB each, plus the k31/k55 laws; extend_to_um hooks Brick2221 build_extended_templates. Wrap the MIST track loaders from WD2_Portal_Manifold (load_mist_table, build_isochrone_track) to produce IsochroneSpec. Write feature atlases from JWST_Archive_ColorPortal.py:123-242, Brick2221 FEATURE_FAMILIES:122-135 and the W51 lists. sed.py holds the mag<->Jy and nuFnu reference implementations. Write golden JSON for the JS tests.

### WP03 — Spec dataclasses, capability resolver, config assembly, shell

- depends_on: WP00
- files:
  - `portalkit/spec.py`
  - `portalkit/caps.py`
  - `portalkit/config.py`
  - `portalkit/expr.py`
  - `portalkit/render/__init__.py`
  - `portalkit/render/shell.py`
  - `portalkit/render/markdown_lite.py`
  - `portalkit/data/expr_vectors.json`
  - `web/shell.html`
  - `tests/py/test_spec.py`
  - `tests/py/test_caps.py`
  - `tests/py/test_config.py`
  - `tests/py/test_expr.py`
  - `tests/py/test_shell.py`
  - `tests/py/test_markdown_lite.py`
- description: Implement the section 9.1 dataclasses and helpers (catalog_from_rows, join, ImageSetSpec.from_rows, standard_pages). caps.py implements the section 8.1 flag derivation and rules, including the added collapsed panes. config.py assembles page.json (section 4.3); it validates every key and expression against manifests, validates pane options (section 6.10) and the 64 kB cap. expr.py implements the section 4.4 grammar. shell.html is under 8 kB: viewport meta, inline theme pre-script, CSS links from {assets}, the #pk-page JSON with '</' escaped, a module script that imports boot.js, a noscript message, and an onerror fallback note if the assets fail. markdown_lite produces sanitized HTML for headings, lists, tables, code and links only.

### WP04 — Deploy: exposure, staging, ownership, replace/backup/rollback, verify, status, devserver

- depends_on: WP00
- files:
  - `portalkit/deploy/__init__.py`
  - `portalkit/deploy/exposure.py`
  - `portalkit/deploy/stage.py`
  - `portalkit/deploy/deploy.py`
  - `portalkit/deploy/backup.py`
  - `portalkit/deploy/verify.py`
  - `portalkit/deploy/status.py`
  - `portalkit/deploy/htaccess.py`
  - `portalkit/deploy/devserver.py`
  - `docs/deploy.md`
  - `tests/py/test_exposure.py`
  - `tests/py/test_deploy.py`
  - `tests/py/test_backup_rollback.py`
  - `tests/py/test_verify.py`
  - `tests/py/test_devserver.py`
- description: Implement section 11 exactly. The exposure classifier works over a configurable root pair; tests use a fake tree that mirrors the verified symlinks. deploy.py implements the ownership manifest, the lstat symlink refusal, the pk-tmp + fsync + os.replace order (data, then portal.json, then HTML) and the stub/inplace replace. backup.py implements backup.json with md5 and rollback. verify.py uses urllib (no requests), downloads to a temporary file and hashes it, and checks 401 vs 200. htaccess.py writes the optional .htaccess. devserver.py is a ThreadingHTTPServer with options for basic auth, Range on/off, a Content-Encoding mode (auto adds gzip for .gz like a misconfigured Apache, none, or gzip) and latency; it is used by CLI serve and all e2e tests. Catch only specific exceptions (OSError, urllib.error.HTTPError, ...).

### WP05 — JS data layer and workers

- depends_on: WP00, WP01
- files:
  - `web/js/data/codec.js`
  - `web/js/data/inflate.js`
  - `web/js/data/fetch.js`
  - `web/js/data/client.js`
  - `web/js/data/hub.js`
  - `web/js/data/expr.js`
  - `web/js/data/records.js`
  - `web/js/data/templates.js`
  - `web/js/data/filters.js`
  - `web/js/data/extinction.js`
  - `web/js/data/isochrones.js`
  - `web/js/workers/data.worker.js`
  - `web/js/workers/rpc.js`
  - `web/js/workers/ops/select.js`
  - `web/js/workers/ops/raster.js`
  - `web/js/workers/ops/query.js`
  - `web/js/workers/ops/sky.js`
  - `web/js/workers/ops/project.js`
  - `web/js/workers/ops/export.js`
  - `web/js/workers/ops/find.js`
  - `tests/js/codec.test.mjs`
  - `tests/js/expr.test.mjs`
  - `tests/js/select.test.mjs`
  - `tests/js/records.test.mjs`
  - `tests/js/physics.test.mjs`
- description: Implement the section 6.6 APIs exactly. Use one module worker per catalog dataset. Fetch with a gzip-magic check, then DecompressionStream, with inflate.js (a port of tiny-inflate with its MIT header) as fallback. The shard LRU is 16 MiB; the record LRU is 64 entries with tokens that drop stale responses. project.js reuses vendor kernel.projectRange when support <= 28, and uses a sparse loop otherwise. select.js applies brushes only to drawable rows (lasso even-odd with bbox prefilter). sky.js holds skyWindow (viewport filter over LOD order, cap 20k) and the dec-sorted crossmatch. find.js handles id, alias, substring and sexagesimal/decimal cone search. export.js writes CSV, JSON, the ID list and DS9 regions. Physics helpers match WP02 golden values to 1e-6 relative.

### WP06 — JS core, layout, CSS tokens

- depends_on: WP00
- files:
  - `web/js/core/boot.js`
  - `web/js/core/store.js`
  - `web/js/core/selection.js`
  - `web/js/core/hash.js`
  - `web/js/core/carry.js`
  - `web/js/core/keys.js`
  - `web/js/core/theme.js`
  - `web/js/core/loadscript.js`
  - `web/js/core/testhooks.js`
  - `web/js/core/fmt.js`
  - `web/js/core/registry.js`
  - `web/js/layout/layout.js`
  - `web/css/tokens.css`
  - `web/css/base.css`
  - `web/css/layout.css`
  - `web/css/print.css`
  - `docs/js.md`
  - `tests/js/store.test.mjs`
  - `tests/js/selection.test.mjs`
  - `tests/js/hash.test.mjs`
  - `tests/js/fmt.test.mjs`
- description: Implement sections 6.1-6.5, 6.7 and 6.8 and section 12. boot follows the documented order and sets body data-caps and data-tier. The store has rAF-coalesced topics, sync mode and events. Selection has the frozen semantics, undo depth min(20, floor(64MiB/n)), recipes and the ids/bits/r serialization. hash binds with replaceState debounced at 250 ms, with pushState on focus; legacyhash.js (WP13) is imported dynamically only when page.legacy.grammar is set. The layout has splitters, folds, pop-out, mobile tabs below 900 px and try/catch localStorage persistence. fmt.stepDigits gives step-aware precision. registry maps pane type to module path. testhooks are active only with ?pktest=1.

### WP07 — Rendering primitives: XYRenderer, budget, canvas2d, axes, overlays, brush UI, fold/tour/compass

- depends_on: WP00, WP06
- files:
  - `web/js/gl/xyrenderer.js`
  - `web/js/gl/xyshaders.js`
  - `web/js/gl/budget.js`
  - `web/js/plot/canvas2d.js`
  - `web/js/plot/axes.js`
  - `web/js/plot/overlays.js`
  - `web/js/plot/brushui.js`
  - `web/js/math/tour.js`
  - `web/js/math/compass.js`
  - `web/js/math/fold.js`
  - `tests/js/fold.test.mjs`
  - `tests/js/tour.test.mjs`
  - `tests/js/axes.test.mjs`
- description: XYRenderer (section 6.9) reuses vendor ACCUM_FS_CONT, ACCUM_FS_CAT, COMPOSITE_FS, DOWN_FS, BLUR_FS and FINAL_FS. New XY_VS emits the same varyings (v_w, v_c, v_has, v_sel, flat v_code). MARKER_VS/FS handle tier T1. budget.js implements the section 7.5 plan. fold.js implements section 7.3, including dm and the constant folded into the camera. tour.js wraps vendor frame.geodesic/blendFrames with drift and per-axis play. canvas2d handles T0 styling and SVG export. overlays.js handles dim-space overlays with coverage fade, pairs connectors, isochrones, vectors and cut lines. brushui has rect/circle/lasso with modifier-to-op mapping via selection.opFromEvent.

### WP08 — Plot panes: scatter, manifold, hist + controls

- depends_on: WP05, WP06, WP07
- files:
  - `web/js/panes/scatter.js`
  - `web/js/panes/manifold.js`
  - `web/js/panes/hist.js`
  - `web/js/ui/controls.js`
  - `web/js/ui/axis_picker.js`
  - `web/js/ui/chips.js`
  - `web/js/ui/legend.js`
  - `web/css/panes/scatter.css`
  - `web/css/panes/manifold.css`
  - `web/css/panes/controls.css`
- description: scatter and manifold implement the tier table in section 7.1, including the semantic-zoom switch, context density, raster PNG layers, pre-binned first paint, small multiples (grid option) and per-pane pop-out. manifold wires the GC Renderer when support <= 28 and XYRenderer otherwise, plus tour/compass/presets and missing-data fade. controls handles av/rv/dm sliders, colour-by, the template picker and features toggles. axis_picker has grouped optgroups with counts, prev/next/play and typed expressions. chips cover category, flag, S/N-tighten, saved selection and preselect chips. legend shows counts and category toggles. hist provides the range brush that writes filters.ranges.

### WP09 — Sky panes: sky, wavelength, narrative + Aladin layer

- depends_on: WP05, WP06, WP07
- files:
  - `web/js/panes/sky.js`
  - `web/js/panes/wavelength.js`
  - `web/js/panes/narrative.js`
  - `web/js/sky/aladin.js`
  - `web/js/sky/layers.js`
  - `web/js/sky/markers.js`
  - `web/js/sky/overlays.js`
  - `web/js/sky/skybrush.js`
  - `web/css/panes/sky.css`
  - `web/css/panes/wavelength.css`
  - `web/css/panes/narrative.css`
  - `tests/fixtures/aladin_stub.js`
- description: aladin.js loads the CDN, then the mirror, then shows an error note, with an 8 s timeout. It keeps one instance per page, a command queue before ready, and feature detection for the layer/opacity API drift. layers.js handles base and overlays with opacity, overlays off/defaults and lazy FITS overlays. markers.js has one A.catalog per category and skyWindow LOD markers up to 20k, with a sampled notice. overlays.js handles polygons, boxes, circles, rings and shapes. skybrush.js does circle/rect on the sky through DataClient.brush with sky ViewRef. wavelength.js does the asymmetric cross-fade with step fallback, play/speed and markers. narrative.js uses IntersectionObserver beats; scene exit removes overlays; suppress_view. aladin_stub.js implements the section 13 API list and records calls.

### WP10 — Spectrum panes: spectrum1d, spectrum2d, SED, features

- depends_on: WP05, WP06, WP07
- files:
  - `web/js/panes/spectrum1d.js`
  - `web/js/panes/spectrum2d.js`
  - `web/js/panes/sed.js`
  - `web/js/spec/features.js`
  - `web/css/panes/spectrum.css`
  - `web/css/panes/sed.css`
  - `tests/js/sed.test.mjs`
- description: spectrum1d supports pan, wheel, box and pinch; autoY on the visible range; variants; segment toggles; band jumps; pin up to 8; rubber band; velocity offset; mask shading; filter transmission shading; reddened templates scaled over configurable bands; sed mode (log-log). spectrum2d supports pki1 (colormap, stretch) and PNG mode, with the x axis linked to store spec.xr. sed supports Fnu/nuFnu, limits as arrows, templates/blackbody/sedcurve models and a fullscreen dialog with nearest-point tooltip. features.js does atlas rendering with collision dropping and requires_coverage dashing.

### WP11 — List/detail/gallery/table/readme/landing panes + UI widgets

- depends_on: WP05, WP06
- files:
  - `web/js/panes/list.js`
  - `web/js/panes/detail.js`
  - `web/js/panes/gallery.js`
  - `web/js/panes/table.js`
  - `web/js/panes/readme.js`
  - `web/js/panes/landing.js`
  - `web/js/ui/hover.js`
  - `web/js/ui/lightbox.js`
  - `web/js/ui/export.js`
  - `web/js/ui/nav.js`
  - `web/js/ui/help.js`
  - `web/js/ui/toast.js`
  - `web/js/ui/annotate.js`
  - `web/css/panes/list.css`
  - `web/css/panes/detail.css`
  - `web/css/panes/gallery.css`
  - `web/css/panes/landing.css`
  - `web/css/panes/ui.css`
  - `tests/js/export.test.mjs`
- description: list is virtualized for any N, with search through DataClient.find, sort and a selection-only view. detail has templates, the property table with click-to-axis, figures, products, bars, prose and links; it prefers exact rows. gallery has a virtualized card grid, roles, facets with counts, presets, lazy images, hover, annotate mode and mini panes through the layout. landing has cards from portal.json, counts from manifests, global search and readme blocks. hover is an edge-flipping card. lightbox works over the filtered set with lb= hash clear and swipe. export handles CSV/JSON/IDs/DS9 and import by id, alias or 0.3 arcsec position. nav renders portal.json, carry, login-required badges and related links. help lists enabled and disabled caps with reasons and the keymap. annotate stores codes y/n/g/w/l/e in localStorage and exports a CSV with the checklist key columns; it never writes server files.

### WP12 — Test harness: synthetic fixtures, e2e smoke, caps matrix, perf, parity, contact sheet

- depends_on: WP01, WP02, WP03, WP04, WP06
- files:
  - `tests/fixtures/make_synthetic.py`
  - `tests/e2e/conftest.py`
  - `tests/e2e/test_archetype_smoke.py`
  - `tests/e2e/test_caps_matrix.py`
  - `tests/e2e/test_deep_links.py`
  - `tests/e2e/test_fallbacks.py`
  - `tests/perf/test_perf_budget.py`
  - `tests/py/test_web_tree.py`
  - `tests/py/test_parity_inventory.py`
  - `tests/data/inventory_caps.json`
  - `tests/data/port_table.json`
  - `tools/contact_sheet.py`
- description: make_synthetic.py builds section 13 Level 1 portals through bundle/physics/render directly (it does not depend on build.py). The e2e conftest starts devserver, launches Chromium with SwiftShader flags and routes aladin to the stub. Smoke, caps-matrix, deep-link and fallback tests follow section 13. Perf tests follow the Level 3 table. test_web_tree checks there is no package.json and that imports are relative. inventory_caps.json and port_table.json come from inventory.json and section 14. test_parity_inventory asserts that each page's resolver output is a superset of its legacy caps. contact_sheet.py writes paired screenshots to a review HTML. E2E tests that depend on panes from WP08-WP11 are marked xfail(strict=False) until those WPs land.

### WP13 — Legacy bridge, context, build orchestration, CLI

- depends_on: WP01, WP02, WP03, WP04
- files:
  - `portalkit/legacy/__init__.py`
  - `portalkit/legacy/extract.py`
  - `portalkit/legacy/manifold_payload.py`
  - `portalkit/legacy/gc_manifest.py`
  - `portalkit/legacy/hashmap.py`
  - `portalkit/legacy/dirgallery.py`
  - `portalkit/legacy/importshim.py`
  - `portalkit/context.py`
  - `portalkit/build.py`
  - `portalkit/cli.py`
  - `web/js/core/legacyhash.js`
  - `adapters/_template.py`
  - `docs/adapters.md`
  - `tests/py/test_legacy_extract.py`
  - `tests/py/test_manifold_payload.py`
  - `tests/py/test_gc_manifest.py`
  - `tests/py/test_hashmap.py`
  - `tests/py/test_dirgallery.py`
  - `tests/py/test_importshim.py`
  - `tests/py/test_cli_build.py`
  - `tests/js/legacyhash.test.mjs`
  - `tests/fixtures/legacy_snippets/`
- description: extract.py uses raw_decode at the three embed styles. manifold_payload converts stars/dims (v, k, center, scale, range, group, log, note)/groups/presets/categories to a CatalogSpec and ManifoldSpec, with k codes mapped to limit kinds. gc_manifest writes a pk-catalog/1 manifest whose file URLs point at GC files in place (u16 dims map to u16q, offsets to i16off, errs to u8log, meta to cat8). dirgallery scans a directory with named-group regex facets. importshim implements section 9.3. context implements the section 9.3 cache, keyed by path/mtime/size without pickle, and the input registry with tmp rejection. build.py runs the section 9.4 pipeline. cli.py implements the section 10 commands and exit codes. legacyhash.js implements all section 6.4 grammars. adapters/_template.py is a commented skeleton.

### WP20 — Adapter: sgrb2 (wave 1)

- depends_on: WP13, WP12, WP08, WP09, WP10, WP11
- files:
  - `adapters/sgrb2.py`
  - `sites/sgrb2.toml`
  - `tests/adapters/test_sgrb2.py`
  - `tests/e2e/test_port_sgrb2.py`
- description: Tier A references the existing data/mapccd/*.f32.gz in place (enc f32, linear order). Tier B imports SgrB2_Portal_MapCCD loaders and re-encodes to u16q with LOD and pre-binned density. Pages: landing, sky explorer, map_ccd explorer (T2, GPU projection), SED gallery with live SED for all 2105 sources. Declared public (target classified both). Remove the hard-coded counts. The landing card that links the secure manifold triggers the public->secure badge.

### WP21 — Adapter: robitaille (wave 1)

- depends_on: WP13, WP12, WP08, WP09, WP10, WP11
- files:
  - `adapters/robitaille.py`
  - `sites/robitaille.toml`
  - `tests/adapters/test_robitaille.py`
  - `tests/e2e/test_port_robitaille.py`
- description: 28,248 rows. Store 54 mags and 21 physical params; the 172 colours are virtual (CPU projection path, 75-83 base columns). FRIED becomes a category. The dm slider and the >30 mag at 8 kpc rule are client-side. Tier B caches the 4-hour read_geometry loop through ctx.cache. SED curves are written as sedcurve pks1 shards. The imaging gallery gets thumbnails via portalkit thumbs; imaging/gallery.html is merged and dropped from nav. Public.

### WP22 — Adapter: nirspec6927 core pages (wave 1)

- depends_on: WP13, WP12, WP08, WP09, WP10, WP11
- files:
  - `adapters/nirspec6927.py`
  - `sites/nirspec6927.toml`
  - `tests/adapters/test_nirspec6927.py`
  - `tests/e2e/test_port_nirspec6927.py`
- description: Pages: landing, map explorer, manifold, specviewer, colorcolor, icecolumns, cogas, pvgallery, sedgallery, spectra_png gallery. Spectra go to pks1 shards and s2d FITS to pki1. MSA shutter and dither overlays. Per-grating products as records json. Secure: deploy must classify the target as secure. Uses NIRSpec_6927_AladinMap/Manifold through ctx.legacy.

### WP23 — Adapter: dirgallery_sites (wave 2)

- depends_on: WP13, WP12, WP08, WP09, WP10, WP11, WP20, WP21, WP22
- files:
  - `adapters/dirgallery_sites.py`
  - `sites/dirgallery_sites.toml`
  - `tests/adapters/test_dirgallery_sites.py`
  - `tests/e2e/test_port_dirgallery_sites.py`
- description: One registry covers about 45 autoindex/jQuery galleries: brick_2221 ccds_cmds (5 regex presets become one page), sed_figures legacy/top100, the public brick_2221/nirspec_6927 gallery, the 2640/6927 pngs galleries when they are not built by family adapters, robitaille png_b (secure) and modelgrids/ccds, the w51/sgrb2/wd2/wd1/cloudc galleries flagged by the critic, and mislabeled headers fixed. Each portal entry declares visibility and target.

### WP24 — Adapter: wd2 (wave 3)

- depends_on: WP13, WP12, WP08, WP09, WP10, WP11, WP23
- files:
  - `adapters/wd2.py`
  - `sites/wd2.toml`
  - `tests/adapters/test_wd2.py`
  - `tests/e2e/test_port_wd2.py`
- description: Pages: map_ccd explorer, colorcolor explorer, wd2_manifold (164 dims, CPU projection), sabbi manifold, SED gallery, wavelength explorer. Isochrones come from build_isochrone_track. Preselects become saved selections. 2640 spectra are shared by URL. Target is web/public/jwst/wd2/nirspec_2640/unified/wd2/.

### WP25 — Adapter: nirspec2640 (wave 3)

- depends_on: WP13, WP12, WP08, WP09, WP10, WP11, WP23
- files:
  - `adapters/nirspec2640.py`
  - `sites/nirspec2640.toml`
  - `tests/adapters/test_nirspec2640.py`
  - `tests/e2e/test_port_nirspec2640.py`
- description: Pages: landing, map, colorcolor (pairs connectors, BD hist split), specviewer, pvgallery, sedgallery, red_excess (binned-stat background), manifold, one pngs gallery with 3 role presets. Product links are rewritten to the secure archive URL with a login-required badge. Target is web/public/jwst/wd2/nirspec_2640/unified/nirspec2640/; this resolves the public-copy drift.

### WP26 — Adapter: sickle_brick (wave 3)

- depends_on: WP13, WP12, WP08, WP09, WP10, WP11, WP23
- files:
  - `adapters/sickle_brick.py`
  - `sites/sickle_brick.toml`
  - `tests/adapters/test_sickle_brick.py`
  - `tests/e2e/test_port_sickle_brick.py`
- description: Pages: Sickle MIRI SED map explorer (SESHAT bars, F2550W FITS overlay URL through exposure), Sickle manifold, Brick2221 F1500W explorer (extended templates to 26 um, k55, normalization bands that exclude the MIRI excess). The sed_figures galleries join the F1500W catalog. Sickle is declared public to match its current exposure, pending the open question.

### WP27 — Adapter: spherex (wave 4)

- depends_on: WP13, WP12, WP08, WP09, WP10, WP11, WP24, WP25, WP26
- files:
  - `adapters/spherex.py`
  - `sites/spherex.toml`
  - `tests/adapters/test_spherex.py`
  - `tests/e2e/test_port_spherex.py`
- description: One shared catalog bundle feeds six pages: landing, colours explorer (Yoo locus overlay), browse spectra (cone search), map (bookmarks, Planck 857 overlay), sed (nuFnu toggle), manifold. Spectra go to pks1 shards. Replaces the rsync deploy_portal.sh flow with portalkit deploy. README deployed. Public.

### WP28 — Adapter: jwst_archive incl. W51 archive_colors build (wave 4)

- depends_on: WP13, WP12, WP08, WP09, WP10, WP11, WP24, WP25, WP26
- files:
  - `adapters/jwst_archive.py`
  - `sites/jwst_archive.toml`
  - `tests/adapters/test_jwst_archive.py`
  - `tests/e2e/test_port_jwst_archive.py`
  - `tests/py/test_no_checklist_write.py`
- description: Two [[portal]] entries: the archive portal (secure, jwst/spectra/portal_unified) and the W51 archive_colors build (secure, w51/portal/archive_colors/unified) with the W51 photometry as context dataset. Spectra ids are derived from the x1d filename prefix (fixes 695/1539 stale idx). Band-coverage validity bits. One archive gallery with facets replaces 11 legacy galleries (the PNGs are public through the web/public/jwst/spectra symlink, so the gallery portal is public and links to the secure colour portal). Checklist annotate mode plus archive-checklist-diff; never writes jwst_archive_spectra_checklist.csv (test enforces).

### WP29 — Adapter: w51 (wave 5)

- depends_on: WP13, WP12, WP08, WP09, WP10, WP11, WP27, WP28
- files:
  - `adapters/w51.py`
  - `sites/w51.toml`
  - `tests/adapters/test_w51.py`
  - `tests/e2e/test_port_w51.py`
- description: Secure, at /orange/adamginsburg/w51/portal/unified (served via web/secure/w51/portal). Identity migration: positional ids with W51-000123 aliases. Two independent catalogs on map_ccd. GTC slits/tiers/rings overlays. spec2d PNG mode. Hexbin rasters plus 53k context density. spectra_candidate is BLOCKED until its tmp-scratchpad inputs are copied to /orange/adamginsburg/w51/gtc/candidate_xb5/ with user approval. The wavelength page uses rungs.

### WP30 — Adapter: gc_manifold (wave 5)

- depends_on: WP13, WP12, WP08, WP09, WP10, WP11, WP27, WP28
- files:
  - `adapters/gc_manifold.py`
  - `sites/gc_manifold.toml`
  - `tests/adapters/test_gc_manifold.py`
  - `tests/e2e/test_port_gc_manifold.py`
- description: Foreign GC schema-1 manifests are referenced in place via legacy/gc_manifest (no re-encode). Treasury (3.74M) and 11 field pages use the T2 GPU path. The gc-query grammar is mapped 1:1. FEATURE_CAVEATS become caveats. The red-clump preset is kept. served_by external (starformation.astro.ufl.edu): deploy writes to jwst/releases/site/<field>_manifold/unified, and verify runs against the external URL. The monitor is registered with status excluded.

### WP31 — Adapter: nirspec6927 narrative pages + extras (wave 6)

- depends_on: WP13, WP12, WP08, WP09, WP10, WP11, WP29, WP30
- files:
  - `adapters/nirspec6927_narrative.py`
  - `adapters/hips_tours.py`
  - `adapters/spitzer_spectra.py`
  - `sites/nirspec6927_narrative.toml`
  - `sites/hips_tours.toml`
  - `sites/spitzer_spectra.toml`
  - `tests/adapters/test_nirspec6927_narrative.py`
  - `tests/adapters/test_hips_tours.py`
  - `tests/adapters/test_spitzer_spectra.py`
  - `tests/e2e/test_port_nirspec6927_narrative.py`
  - `tests/e2e/test_port_extras.py`
- description: MSATA scrollytelling (15 beats, acquisition boxes, highlight cleanup), H2 report (VERDICTS imported, rotation-diagram scatter when a line table exists), refstar map (rings tool; refstar_map_data.json has no writer, so it is rebuilt from NIRSpec_6927_RefStar inputs or extracted with Tier A), refstar_check contact sheets. Extras: hips_tours (RGB and ACES wavelength tours), spitzer_spectra (cmz), jwst_gc_aladin explorer, cmd_explorer pre-binned explorer. The narrative family deploys into the same nirspec_6927/unified target using a separate [[portal]] id, with page files prefixed n_ to keep ownership disjoint.

### WPE1 — Wave E: Python framework fixes from wave-1 adapter reviews

- depends_on: none (wave-1 adapters committed). Files: see `docs/wps.json`.
- Changes, each with tests:
  1. Per-legacy-file stub preset and `replace = false` (§4.1 legacy items, §4.3 `page.legacy.presets`, §11.4). `sites/nirspec6927.toml` maps the `_2221`/`_1182` PNG galleries to `role=c2221`/`role=c1182`.
  2. `portal.json counts` (§4.2): one landing count per page from its primary dataset.
  3. No absolute local paths in served files (§9.3 "Served paths"); `check` warns at G1 and fails at G2 (§10).
  4. First paint of context catalogs counts their density grids only (§5.11). The nirspec6927 `bg` catalogue (121,068 rows) drops exact rows.
  5. Directory inputs are stamped without a tree walk, and pre-WPE1 memos are adopted (§9.3 "Input registry and cache cost").
  6. Family keys in the inventory and port-table data equal the site ids (§14).

## 16. Open questions and risks

| # | Item | Default until answered | Owner |
|---|---|---|---|
| Q1 | Sickle data is served publicly through `web/public/jwst/sickle -> /orange/adamginsburg/jwst/sickle` while the portal also appears under secure. Is public exposure intended? | Declare `visibility = "public"` to match current exposure; deploy prints a notice; changing to secure is a one-line toml edit | user |
| Q2 | W51 `spectra_candidate` reads inputs from a tmp scratchpad. Approve copying them to `/orange/adamginsburg/w51/gtc/candidate_xb5/`? | Page is BLOCKED (exit 5) and the legacy page stays live | user |
| Q3 | Aladin Lite `v3/latest` can change API. Pin a version and refresh the mirror `jwst-gc/aladin.js` to that version? | `latest` with feature detection in `sky/aladin.js`; the mirror is the fallback | user |
| Q4 | Does Apache `AllowOverride` permit `Header`/`ExpiresByType` in `.htaccess` under `web/public`, and does it include `FileInfo` (needed by the deny-only `.htaccess` a plain deploy writes, `RedirectMatch`) for the deploy targets? The 28 existing `.htaccess` files under `web/` use only AuthConfig/Options/Indexes directives. | `--htaccess` off; content hashing gives correct caching without headers. The deny-only file is on; `PK_DENY_HTACCESS=0` turns it off if `verify --http` reports 5xx | user / RC staff. Partly answered 2026-10-12: a probe under `web/public` showed `RedirectMatch 403` and `Header set` both take effect (FileInfo allowed); the secure tree is unprobed |
| Q5 | 6927 `refstar_map_data.json` has no writer in the repo. | WP31 extracts the JSON via Tier A and flags it in `status` as `input-without-writer` | WP31 |
| Q6 | The archive checklist stays hand-edited. | Annotate mode exports a CSV diff; `archive-checklist-diff` prints a merge suggestion; nothing writes the CSV (test-enforced) | — |
| Q7 | WD2/2640 public copies in `web/public/jwst/wd2/nirspec_2640` drift from the secure originals. | Unified builds write to `unified/wd2/` and `unified/nirspec2640/` in that directory; legacy copies stay untouched | WP24/WP25 |
| Q8 | GC release site is served from an external host (starformation.astro.ufl.edu). | `served_by = "external"`; deploy writes files; `verify --http` uses the external base URL; exposure classifier reports `unserved` locally and is not treated as an error for that site | WP30 |
| R1 | GPU memory on low-end clients at 5M points | Budget planner (section 7.5) lowers DPR, then LOD prefix; the pre-binned fallback stays usable without WebGL2 | WP07 |
| R2 | Legacy generator modules change after import-shim capture | Shim records each module's sha256 in the build record and `status` reports changes since the last deploy; LegacyImportError exits 6; the adapter's `_fallback` copy (section 9.3) restores a broken loader | WP13 |
| R3 | Quantization alters numeric display | Exact row chunks are on for N <= 1M; detail panes prefer exact values; step-aware formatting elsewhere | WP01/WP11 |

