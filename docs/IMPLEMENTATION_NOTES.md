# Implementation notes and contract clarifications

`docs/ARCHITECTURE.md` is the contract. This file records clarifications and resolved ambiguities found while
implementing it. Where this file and ARCHITECTURE.md disagree, this file wins. Every work package reads it before
starting.

## Resolved after WP00

1. **page.json `budget`** is optional (the §4.3.1 example has none).
2. **Schema key on §5.10 documents.** Writers always emit `"schema": "<id>"` on images, hips, overlays, narrative,
   templates, filters, extinction, isochrones, features and products documents. Validators dispatch on it.
3. **Non-uniform record grids.** A records index `grids` entry is either `{kind:"uniform", ...}` (§5.6) or
   `{kind:"array", file:<name>}` / `{kind:"array", values:[...]}`.
4. **Binned-statistic density grids** (e.g. 2640 median per cell) use `enc:"f32"` plus `"stat":"<name>"`; count grids
   use `u16log` or `u16q` as in §5.9.
5. **`pos` with `u32lin`** is `{enc:"u32lin", files:{ra:<file>, dec:<file>}}` and needs a top-level catalog `bbox`
   for lo/hi.
6. **Split ids.** `row`/`col` layout nodes may carry an optional `id` and `collapsed`. When `id` is absent, the layout
   derives one from the tree path (e.g. `r0.c1`), and persistence uses that.
7. **Filter presets** (manifest `presets`, kind `filter`) carry their payload under `filters` (an object shaped like
   the `filters:<ds>` store value).
8. **`filters:<ds>` store value** may hold `ids` (from "keep only") in addition to cats/ranges/flags/snr/text.
9. **Quantization step.** Writers always emit `step`. A reader that meets a missing step derives it as
   `(hi-lo)/65534` for u16q/u16log and `(hi-lo)/254` for u8q.
10. **Paginated images.json** keeps `"items": []` and lists `pages`.
11. **Test selection.** Plain `pytest -q` runs Level 0 and skips e2e, perf, slow, adapters and served. Gates pass
    `-m e2e` (or `--run-e2e`) for Levels 1-2, and `-m perf` / `-m adapters` / `-m served` for the others.
12. **Null pane options.** `config.py` emits `null` only for options marked `?` in §6.10
    (`schema.OPTIONAL_PANE_OPTIONS`); unset options are otherwise omitted.
13. **Stored column keys** must match `[A-Za-z_][A-Za-z0-9_.]*` (§4.4). Adapters rename source columns
    (`f182m-f410m` -> `c_f182m_f410m`, spaces -> `_`) and keep the original as the column `label`.
14. **Broad-except scanner limitation.** A broad handler whose only `raise` is swallowed by a nested `try` inside it
    is not detected. Code review covers that case.

## Resolved after WP03 and WP04

15. **`TierUnavailable`** lives in `portalkit.spec`. Adapters and WP13 import it from there.
16. **`CapsResult.layout`** (extra field) carries the final pane tree. WP13 passes the `CapsResult` from
    `caps.resolve` straight into `config.assemble`.
17. **Default DatasetRef URLs** (`config.dataset_ref`): catalog `data/<ds>/manifest.json`, records
    `data/<ds>/index.json`, images `data/<ds>/images.json`; hips, overlays and narrative use fixed names under
    `data/`; isochrones use a content-hashed `data/iso.<h8>.json` (bytes from `config.isochrones_file`). Two datasets
    resolving to one URL is an error. WP01/WP13 write at these paths or pass `refs={ds: {...}}`.
18. **Resource refs.** templates/filters/extinction/features enter a page through `res_refs={kind: url}` (or `refs`).
    `assemble` raises `ConfigError` when a pane needs one that is missing.
19. **hips/overlays/narrative/isochrones documents** are built with `config.hips_document`,
    `config.overlays_document`, `config.narrative_document` (beats take `markdown`/`text`; raw `html` is rejected) and
    `config.isochrones_document`; WP13 `build.py` writes them.
20. **Signatures (WP03):** `config.assemble(page, datasets, manifests, caps=None, *, portal, refs=None, res_refs=None,
    pk=VERSION, vendor_assets=False, aladin=None, budget=None, known_prefixes=(), files=None) -> dict`
    (raises `ConfigError(ValueError)` with `.errors`); `render.shell.render(page_json, template=None, *, validate=True)
    -> str`; `render.shell.write(path, page_json, template=None, *, validate=True) -> int`. A readme over 16 kB goes
    into the caller's `files` dict as `readme.<h8>.html`; WP13 writes those bytes next to the page.
21. **Landing `search`.** WP13 passes the portal-wide dataset dict to `caps.resolve` for landing pages. Landing pages
    get the ALWAYS set plus `source_list_search` (§8.1 wins over the §14 counts).
22. **Scatter overlay ids** resolve in this order: (a) `space:"dim"` layers of overlays.json; (b) isochrone track
    labels or their slugs (`config.overlay_slug`: lower case, runs of other characters become `_`); (c)
    `<law lower>_vector` for an attached extinction dataset (e.g. `g23_vector`). WP07 `plot/overlays.js` and WP08
    `panes/scatter.js` use the same rules.
23. **Shell failure reporting** uses a DOM event, not a global; `window.__pk` exists only under `?pktest=1`.
24. **build_report.json** (written by WP13 next to portal.json in the stage dir) carries `adapter_sha`,
    `legacy_modules: [{name, path, sha256}]`, `portalkit`, `allow_tmp_inputs`, `inputs: [{path, sha256}]`. Deploy
    refuses (exit 5) when `allow_tmp_inputs` is set or any input lies under `/blue/*/tmp/`, `/tmp/` or `*/scratchpad/`.
25. **CLI duties (WP13):** map `DeployError.exit_code` to the process exit code; without a TTY refuse warn-level
    exposure decisions unless `--yes`; add `deploy --gc` (`deploy(gc=True)`, `gc_target()`); wire `serve` to
    `portalkit.deploy.devserver.serve()` and `prune-stage` to `stage.prune_stage(protect=...)`; compute
    `shared_legacy_dir` with `portalkit.deploy.is_shared_legacy_dir(entry, load_sites())` for deploy, verify and
    rollback; wire `publish-assets` to `deploy.publish_assets` and `publish-res` to `deploy.publish_immutable`; use
    `stage.create_portal_stage_dir()`/`check_stage_root()` (served stage root -> exit 3) and `make_build_id()`.
26. **Deploy imports.** `portalkit.deploy` exports the functions as `run_deploy` and `status_rows`; import modules as
    `portalkit.deploy.deploy` / `portalkit.deploy.status`.
27. **Two portals, one legacy_dir.** Every `[[portal]]` needs a distinct target. The nirspec6927 narrative portal
    (WP31) sets `target_dir = "<legacy_dir>/unified/narrative"` explicitly in its site TOML.
28. **Install mode.** `web/` is not package data; portalkit runs from a checkout or `pip install -e .`.
29. **Exposure scan cost.** The walk is breadth-first with each level listed by `$PK_SCAN_THREADS` threads
    (default 16). Measured on `/orange/adamginsburg/web`: 6437 directories, 17 s warm (52 s with one thread);
    cold runs took 9-13 min. The slowest single directory is `salt/dihca2/diagnostics_nopeaks` (10 s warm). Results are cached
    for 1 h in `$PK_CACHE_DIR` or `~/.cache/portalkit`. Name-based prune globs for big leaf directories are not
    used: a pruned directory could hold a symlink that exposes a deploy target.

## Resolved after WP06 and WP07

30. **Hub/DataClient contract used by boot (WP05).** `data/hub.js` exports `createHub(page, store)`; `hub.catalog(ds)`
    returns a DataClient with `n`, `manifest` (incl. `presets`), `id(i)`, `find(text, {limit})`, `brush(view, shape)`,
    `query(filters)` and `selection()`, plus `hub.stats()` -> `{fetches, bytes, decodeMs}`. The DataClient constructs
    its selection with `core/selection.js` `Selection` (needs `apply`, `mask`, `onChange`) so worker ops using
    `mask:'sel'` see the same state. The query handler writes `vis:<ds>` = `{version, count}` (`count:null` means no
    filter applied yet). `find()` should report the match kind or offer an exact id/alias lookup.
31. **UI widgets (WP11).** Boot dynamically imports `ui/{toast,nav,help,hover,lightbox}.js` and awaits
    `install(app)` when exported. Widgets mount in `app.slots` `{header, nav, tools, main, selbar}`. `toast.js` listens
    to store event `toast` `{level, text}`; `nav.js` calls `stashCarry(app.portalId, app.carryPayload())` before
    navigating; `help.js` handles `help:toggle`/`help:open` and sets `payload.handled = true`.
32. **Keyboard offer protocol** (docs/js.md §7): action payloads carry `handled:false`. List (WP11) handles `nav:step`
    `{dir, ds}`; brushing panes (WP07/WP08) handle `keys:escape` while a brush is active and `selection:invert` `{ds}`
    with their drawable mask; manifold (WP08) handles `tour:toggle`. Unhandled actions fall back to `keys.js`.
33. **Legacy hash output formats (WP13 `legacyhash.js`)**: `c=by~cmap~lo~hi`, `tpl=key~grid~scale~bands`,
    `<pane>.frame=preset|b64:<f32 LE>`, `f=` base64url JSON (raw JSON accepted), `sky=ra,dec,fov`, `lb=set,index`,
    `view=pane:mode`; gc-query `sel` -> `r:` + base64url(deflate-raw(JSON RecipeOp[])) using
    `selection.js` `deflateRaw`/`b64urlEncode`.
34. **`page.page.legacy.grammar`** is the location boot reads (§4.3 nesting wins over §6.1 wording).
35. **Manifold selections in URLs.** Projection-view brushes are not replayable from `r:` recipes. For catalogs above
    200k rows the manifold records `{kind:'ids', ids}` RecipeOps when the count allows; otherwise the selection is left
    out of the URL (and hash navigation keeps the live selection).
36. **Fold camera.** On the GPU path the camera is `fold.foldCamera(cam, folded.c)`, i.e. `cx -= K0`, `cy -= K1` in
    data units. This supersedes the §7.3 `cx += K0·scale` formula.
37. **Distance modulus on overlays.** `plot/overlays.js` applies `dm` (plus `A_V·A_λ/A_V`) only to `kind:'track'`
    layers (absolute-mag isochrones); other dim layers are not shifted. A pane that folds `dm` into the data does not
    also shift tracks.
38. **Tour API.** `Tour.playAxis(dim)` with an integer is the §6.9 single hop (`opts.axis` `'x'|'y'`);
    the legacy per-axis loop is `playAxisLoop(axis, {dims})` or `playAxis('x'|'y')`.
39. **drawOverlays state** also takes `filters` (pk-filters/1), `colmap` (column key -> ColMeta.filter), `ext`
    (pk-extinction/1), `law`, `dimMeta` (`{key: {center, scale, expr?}}`), and `frame` may be the `frame:<pane>` store
    value. It returns `[{id, opacity, drawn}]`. When `XYRenderer.render()` returns false with
    `stats().refused === 'targets'`, the pane switches to tier F.
40. **Brush shapes.** Shapes carry `xlog`/`ylog`. Circle is `{cx, cy, r, rx, ry}` with screen-circle semi-axes in
    transformed units (log10 on log axes). Lasso and circle tests run in transformed space (even-odd with bbox
    prefilter); rect tests run in data space. `plot/brushui.js` `shapeTest()`/`hitTest()` are the reference
    implementation that `workers/ops/select.js` must match.
41. **Colormaps.** `plot/canvas2d.js` `cmapLUT()`/`sampleCmap()` cover the vendor maps plus viridis, plasma, inferno,
    cividis, gray, turbo, coolwarm, rdbu, with `_r` for reversed. The vendored GC Renderer has only its 4 maps; a
    manifold pane on the vendor Renderer falls back to `sunset` for others.
42. **Budget.plan** returns `{dpr, allowed, refused, targetBytes, maxVertexBytes, fits, capFraction}`.
43. **Focus broadcasts** (`broadcastFocus`/`onBroadcastFocus`) carry `ds`.

## Resolved after WP01, WP02 and WP05

44. **Bundle entry points** (used by WP13 `build.py`), documented in `docs/bundle.md` §1:
    `catalog.write(spec, dest, *, build, seed, links, provenance, ledger, log, step_mas) -> CatalogResult`
    (`manifest, order, ids, id_to_row, alias_to_row, files, warnings, writer, row_of()`);
    `records.write(spec, dest, *, order=CatalogResult.row_of, tier, ledger, log) -> RecordsResult`;
    `images.write(spec, dest, *, rows=CatalogResult.row_of, thumb=(url, item_id, role)->str|None, ledger, log,
    page_max) -> ImagesResult`. `dest` is a `BundleWriter(stage_root, 'data/<ds>', ledger)` or a directory.
    json-legacy record sets are accepted only for tier A or B.
45. **Codec goldens.** `tests/golden/codec/index.json` (`pk-golden-codec/1`, format in `docs/bundle.md` §11) is the
    cross-language oracle; `generate.py --check` and `tests/js/codec.test.mjs` both consume it.
46. **Density grids.** Count grids use `u16log` with the column decode (`10**(lo+(code-1)*step)`, lo/hi in log10,
    `step` always written) and carry `total`. Binned-stat grids use `enc:"f32"` plus `stat`, `value`, `shuffle`,
    `lo`, `hi`. The stat syntax in specs is `fn:expr` or `fn(expr)`.
47. **Log encodings.** `u16log` column lo/hi/step are in dex. `u8log` column lo/hi are linear (GC convention) and step
    is in dex. Adapter lo/hi for u16log columns are given linear and stored in dex.
48. **u32lin positions** are always 4-plane byte-shuffled; the encoding implies it (the `pos` block has no `shuffle`
    key). `codec.decodePos(posMeta, files, bbox)` takes the manifest `bbox` as a third argument.
49. **i16off tiling** is portalkit's own: dec bands of `0.125*step_mas/8` deg split into RA cells, `tile_refs` capped
    at 4096 (step_mas is raised, with a warning, to stay under the cap). Position files are single files (no
    chunking), which limits i16off catalogs to roughly 8M rows under `COL_FILE_MAX`. Chunked pos files are a schema
    follow-up if a larger catalog appears.
50. **Category encoding.** `enc:"auto"` gives `cat8` up to 255 levels and `cat16` above.
51. **Record binaries.** pks1/pki1 headers are padded to a multiple of 8; wave arrays carry `dtype` (`f32`, or `f64`
    when f32 would reorder samples or err by >1% of the smallest spacing). pki1 u8 codes map NaN to 0 (same as lo).
52. **Images.** Width and height live per role (`roles[role].{url,w,h,thumb}`). `ImageSetSpec.join` naming an item tag
    means "look up `tags[join]`"; otherwise the lookup uses `item.id`. `CatalogResult.row_of` resolves aliases.
53. **`exact_gpu`** is a field on `CatalogSpec` (default True) and `ColumnSpec` (None inherits the catalog value;
    False lets a `value` column use f32).
54. **Filter key resolution** (`resolveKey` / `filters.resolve_key`, goldens in `tests/golden/physics/resolve_key.json`):
    colmap, exact, unique case-insensitive, unique alias, then short-name probes `lower(name)`, the text after the
    last `_`, and `shortName(lower(name))`, each with a `label` tie-break when candidates are filter entries. The
    shared filters document has no `aliases`; `ks`/`k` stay ambiguous there (2MASS vs VISTA/UKIDSS), so panes pass
    the manifest colmap (column key -> `ColMeta.filter`).
55. **JS physics signatures** (supersede §6.6): `aLambda(filterKeyOrEntry, av, rv, ext, filters, colmap)` throws
    `TypeError` when given a key string without `filters`; `vector(xExpr, yExpr, av, rv, filters, colmap, ext)`;
    `trackXY(track, xExpr, yExpr, av, rv, dm, filters, colmap, ext)`. Track mags are absolute: each value is
    `c + Σ w·(M + 5·log10(dist_pc/10) + dm + A)`; for `av != 0` the filter entry is `resolveKey(magsKey, filters)`,
    else `resolveKey(exprKey, filters, colmap)`. An unresolved key gives an all-NaN axis.
56. **sedPoints** returns `[{wl, fnu, err, kind, key, filter}]` sorted by wl. Limit codes come from `row._lim[key]` /
    `row._limits[key]` (1/'1'/'u' upper, 2/'2'/'l' lower) or the column's `limit` sibling; limits have `err:null`;
    non-finite or non-positive fluxes are dropped.
57. **Templates and laws.** `extend_to_um` extends templates with B_nu (the template flux is F_nu). The Brick2221
    legacy script used B_lambda until 2026-10-12, which put the 15-25 um continuum ~8-22x low; it now uses B_nu, and
    the default output matches it. `planck='lambda'` reproduces the older curves. Law records are gzipped pks1
    with a single channel `k` and meta `{law, rv}` (no flux channel). Template grid descriptors are
    `templates/grid_<g>.<h8>.json`.
58. **publish-res.** WP13 calls `portalkit.physics.write_shared(dest, ledger=...)`, which returns `{kind: path relative
    to dest}`; `res_refs` URLs are `"{res}" + path` with no separator (as in §4.3), and `page.res` ends with `/`.
    A filter's `trans` path is relative to the filters document URL.
59. **Isochrone documents.** `physics.isochrones.build_document` is the single builder; `config.isochrones_document`
    calls it and validates the result.
60. **Picking.** A pick ViewRef carries `kx`, `ky` (pixels per data unit, per decade on log axes);
    `DataClient.pick` rejects without them. Plot panes (WP07/WP08/WP09/WP10) add them.
61. **Projections.** `DataClient.project(spec, {token, pane})` caches per pane+token; a `{kind:'proj'}` ViewRef looks
    up its own `pane` first.
62. **Queries.** `DataClient.query` sequences `apply:true` calls itself; a stale applying query returns
    `applied:false`. Callers send no seq.
63. **Export.** CSV has no comment lines; the worker result carries `exact` (false when values are dequantized).
64. **GPU columns** are linear-space A/B for every non-u16q/u8q column, matching the CPU projection path. Adapters that
    want log-flux manifold dims define log10 columns as base columns.
65. **Vendored `kernel.buildGrid`** takes bounds from a stride subsample and can return NaN bounds for sparse drawable
    sets. Callers use `workers/ops/select.js` `buildPickGrid`, which handles that case.
66. **snr filter scope `projection`.** `hub.js` derives `snr.keys` from `axes:<pane>`/`frame:<pane>` when a filter omits
    them.
67. **Sky brushes.** The worker treats a sky ViewRef as `{kind:'xy', x:'ra', y:'dec'}` in degrees, with no RA wrap or
    cos(dec) scaling. Current portal fields do not cross RA=0; a wrap-aware sky ViewRef is a follow-up.
68. **Shared GPU budget.** `boot.js` owns one `Budget` per page as `app.gpuBudget`; panes read it and never create one.
69. **Pane CSS.** The pane CSS loader maps scatter/manifold/hist to `[<pane>.css, controls.css]`; every UI widget style
    lives in `css/panes/ui.css`.
70. **Category `default_off`.** The filter owner (`hub.js` category filters, initialised at boot from the manifest)
    applies `default_off`; `ui/chips.js` only renders the state.
71. **Narrative events** (add to §6.10): `narrative:highlight {pane, ds, ids, beat, goto?}` and `narrative:sync {pane}`.
    Scatter, manifold and sky panes listen for both.
72. **Empty overlay list.** `ovl=-` encodes an explicitly empty overlay list; an absent `ovl` means the layout default.
73. **Record channels.** `DataClient.record(ds, i, {channel})` cancels only the previous request on the same channel.
74. **Sorting.** `DataClient.order(key, dir)` returns a worker-side argsort (Int32Array of rows); list and gallery use
    it. Id sort uses the same call with `key:'id'`.
75. **Model controls.** A_V / R_V / template / feature toggles have one implementation (`ui/controls.js`); sed.js and
    spec/features.js mount it.
76. **Legend helpers.** `ui/legend.js` exports `isCategory`, `levelColors` and the GPU colour helpers; panes import them.
77. **Pane titles.** Panes call `ctx.setTitle`; `options.title` seeds the initial title.
78. **Raster options** accept `x`, `y`, `cell`; detail templates accept `photometry` and `compact`.
79. **spectrum2d** writes `spec.xr` when `link1d` is on (two-way x link with spectrum1d; add to §6.2).
80. **Lightbox.** Providers may define `ready`/`encode`/`decode`. `lb=<set>,<k>` opens images.json item k; a null set
    prefers the Gallery set.
81. **Image URLs.** images.json item URLs and pki1 `png` sidecar URLs resolve against the page URL (§4.3), with
    `{res}`/`{assets}` substituted; detail product links resolve against the records index URL.
82. **Aladin.** The library loads once per page; each sky pane owns one viewer.
83. **Annotation CSV** header: `id,Target,Program,Object,SlitID,Observation,Visit,VisitGroup,Filename,checklist`.
84. **Gallery focus** without a catalog is `{ds:<images ds>, i, id}`.
85. **Landing search** opens one worker per search manifest, lazily on first keystroke.
86. **Distance modulus.** Scatter folds `dm` into linear-magnitude axes and passes `dm=0` to overlays. The manifold
    manifest has no `overlays` key; the pane reads `cfg.overlays`.
87. **Vendored GC renderer** keeps the visibility mask in category slot 3 and supports 4 colormaps.
88. **Default layout overlays and dims.** `caps.py` default layout gives scatter (and manifold) `options.overlays`
    (isochrone track ids and `<law>_vector`) when iso/ext datasets are present and the caps are on, and gives the
    manifold `options.dims` from its manifest on every page (the build already writes it on gc-query/kv-view pages).
89. **Caps rules need datasets.** `hips_layer_stacking` requires a HiPS document; `live_sed_viewer` requires a filters
    dataset; `labeled_features_overlay` requires a features dataset.
90. **Legacy hash mapping.** `boot.js` calls `mapLegacy(grammar, {hash, search, page})`. gc-query `rv=1`, `g`, `fa` are
    consumed and dropped (no canonical key; canonical `rv` is numeric R_V). Unmappable index params stay in the URL.
    `+` stays literal in bare-name/specviewer hashes; only query strings and kv-view hashes decode `+` as space.
91. **Manifold presets.** `<manifold>.frame=<preset>` accepts `axes` presets (two dims with unit weights) as well as
    `frame` presets.
92. **Selection hash form.** `hash.js` picks the `sel=` encoding from the selection alone (the shorter of `r:` and
    `bits:`), so a restored selection writes the same form before and after a reload.
93. **CSV import.** `app.importCsv(text, {mode})` is the runtime entry; the dialog calls it.
94. **Test fixtures.** Browser fixtures live in `tests/conftest_browser.py`, registered as a plugin by `tests/e2e` and
    `tests/perf` conftests. Synthetic fixture stamps hash `portalkit/**/*.py` and `web/**` as well as the generator.
95. **Build check** expects vendored assets at `_pk/v<ver>/js/core/boot.js`. `check_build` lives in `build.py`;
    `SiteConfig`/`PortalConfig`/`PageConfig` live in `context.py`. `portalkit.deploy` re-exports `raise_for_plan`.
96. **GC catalogs.** The build passes an explicit ref to the generated `data/<k>/manifest.json`. The GC u8 tile index
    is widened to a u16 file in the dataset dir; other GC files are referenced in place.
97. **Perf budget host.** Measured on a 2-CPU cgroup slice of a loaded 128-core node with SwiftShader WebGL. At the
    WP12 measurement, 1M brush commit 1.35 s and 1M pan settle 0.48 s met budget; 50k (T1) pan settle 6.6 s and 1M
    first-paint bytes 2.95 MB (startup fetch of `ids` 5.0 MB and one full column) missed. `PK_PERF_SCALE` scales time
    budgets only.
98. **Head chunk.** For a catalog with `n > LOD_MIN_N`, the manifest carries `chunk_starts`: the first row of each
    chunk, `[0, LOD_BASE, LOD_BASE + CHUNK_ROWS, LOD_BASE + 2*CHUNK_ROWS, ...]` (strictly increasing, every value < n).
    Every column then uses `chunk_files` and the ids use `ids.chunks`, with one file per entry of `chunk_starts`, so
    chunk 0 holds exactly the LOD base sample (rows `[0, LOD_BASE)`). Without `chunk_starts`, chunks are
    `ceil(n / chunk_rows)` slices of `chunk_rows` rows (unchanged). pos stays a single file. Readers (worker
    `chunksFor`, prefix loads, `id(i)`, `record(i)`) derive chunk ranges from `chunk_starts` when present.
99. **Startup reads.** No pane loads a whole column, the ids or pos before first paint. The list paints its first screen
    from `client.rows(first, last)` (row chunks, or the head chunks when there are no row chunks) and loads full
    columns on sort, filter or colour-by, or after first paint. Hover cards read one row with `client.record(i)`.
    Sky markers load pos after first paint. Worker `record(i)` and `id(i)` load only the chunk that holds row i;
    find/query/export/order load all ids.
100. **First paint signal.** `app.afterFirstPaint()` returns a promise that resolves once, after the `ready` event, when
    no visible pane is loading, at the next idle callback (timeout 1500 ms). Deferred loads wait on it.
101. **Worker manifest prefetch.** `DataClient` starts the worker as `data.worker.js?m=<encodeURIComponent(manifest
    url)>`; the worker fetches the manifest from top-level code and `open` reuses that in-flight promise.
102. **Id resolution.** `DataClient.resolveIds(ids, {aliases})` returns an Int32Array of rows (-1 when missing) from one
    worker pass; `app.resolveIds` and `app.importCsv` use it.
103. **Canvas 2D density images.** Scratch canvases are created with `getContext('2d', {willReadFrequently: true})`
    (CPU-backed; a GPU-backed `putImageData` stalls SwiftShader for seconds). The colourised image is cached per
    `(grid.counts, cmap, lo, hi, log)` so redraws only call `drawImage`. Applies to canvas2d.js and spectrum2d.js;
    spectrum2d draws natively with `drawImage` and resamples per view only for non-uniform wavelength grids, log axes
    or reversed views.
104. **default_off round trip.** `ui/legend.js toggleLevel` keeps `cats[key] = []` (explicit "all levels on") for
    columns with `default_off`; `core/hash.js` keeps `cats` entries whose value is `[]`, so `f=` carries them.
105. **Record channels** (replaces the wording of 73): `record(i, {channel})` is canonical (`record(ds, i, opts)` is
    accepted); channels are per pane, named `<pane type>:<pane id>`; the app-level hover card uses `hover:card`.
106. **Pane titles** (replaces 77): `options.title` seeds every pane title; `ctx.setTitle` is used by panes whose title
    follows the focused record (detail). Other panes keep the seeded title.
107. **Selection hash form** (refines 92): when a `bits:` form exists, the recipe form (`ids:` or `r:`) competes with it
    only when the recipe is view-independent (ids, preset, or filter ops); the shorter URL wins and a tie goes to the
    recipe. `ids:` is the compact spelling of a single replace-ids recipe. Brushes on catalogs up to 200k rows encode
    as `bits:`.
108. **Widget CSS.** Shared widget rules (`.pk-models`, `.pk-ctl-group`, `.pk-ctl-template`, `.pk-template-scale`,
    `.pk-feat-chip`, `.pk-feat-toggles`) live only in `css/panes/ui.css`, which boot always loads.
109. **Aladin prefetch.** A failed fire-and-forget prefetch does not poison later loads: the first sky pane that mounts
    after a failed prefetch retries once (`retryAladinOnce(page)` in sky/aladin.js; sky and wavelength panes use it).
110. **Extinction vector need.** `extinction_vectors_on_ccd` needs an extinction dataset (`caps.NEEDS`), like note 89; the
    shared `{res}` extinction document satisfies it when it is attachable. A page where the cap stays on always draws
    a vector by default: resolver-built scatter/manifold leaves list `<law>_vector` (note 88), and wavelength/narrative
    overlay documents with a `reddening` dim are listed in their pane's default overlays. Fixtures and tests resolve
    pages with the same resources `build._page_refs` attaches (one shared helper, not a hand-picked tuple).
111. **HiPS attachment.** Pages with sky, skycat, fits or wavelength panes whose legacy caps include
    `hips_layer_stacking` carry a HiPS document; adapters attach one (the synthetic fixture does so by flag).
112. **Pane first paint.** A pane's `data-state` stays `loading` until its first data-backed paint: layout sets
    `ready` only after `mount()` resolves AND, when the handle exposes `firstPaint` (a Promise), after it settles
    (bounded by the 30 s guard of note 100; a rejection marks `error`). Catalog readers expose it: list (first screen of
    `client.rows()` drawn), table, gallery, scatter/manifold/hist (density grid or first points drawn). Panes whose data
    arrives later by design (sky/wavelength markers, note 99; spectrum/detail with no focus) resolve it at mount. So
    `app.afterFirstPaint()` releases deferred reads only after the first screen is on screen. After first paint the list
    reads per-row values from the row records it already holds (hover columns, class colours); a whole column loads only
    for sort, filter or colour-by, or at idle after a quiet period. `__pk.firstPaintAt()` returns a Promise of the
    epoch-ms time `afterFirstPaint` resolved; DevServer `RequestRecord.t` is the epoch-ms time the response finished.
    The Level 3 byte budget counts responses finished before `firstPaintAt`; bytes up to the quiet settle are reported.
    A pane hidden (folded, inactive tab) while its first paint is pending has `data-state="paused"` and does not
    hold `afterFirstPaint`; it returns to `loading` when shown, and the state is left alone once the latch settles.
113. **Legacy items, landing counts, served paths (WPE1).** `[[portal.page]] legacy` items are strings or
    `{file, preset?, replace?}` tables (ARCHITECTURE 4.1). `page.json page.legacy.files` stays a string list, and
    per-file stub presets go to `page.legacy.presets` (`{file: preset}`). `portal.json counts[]`
    (`{page, ds, kind: catalog|images|records, n, unit, units}`) gives every non-landing page a count of its
    primary dataset. `search[]` is unchanged, and the landing pane reads `counts` once it supports the field.
    Served provenance uses `<label>:<relpath>` labels (`portalkit.build.PathLabeler`). `check` flags absolute
    `/orange|/blue|/home` paths in staged html/json (G1 warning, G2 error). `build_report.json` keeps full paths and
    is never deployed.
114. **Context first paint (WPE1).** `first_paint_files(man, context=True)` returns every density grid of a context
    catalog (or both axis columns when it has none), with no row or head chunk. `catalog.write` passes
    `spec.context`. The scatter pane currently worker-bins context columns (`bg.bin2d`) for its backdrop instead of
    drawing the context catalog's pre-binned grid (ARCHITECTURE 5.9 lists background layers as a grid use). Until
    it draws the grid for matching axes, the browser also fetches the context axis columns for the backdrop, and
    this estimate leaves those bytes out.
115. **Input stamps (WPE1).** Directory inputs are stamped `(names sha256, dir mtime_ns, entry count)` without a
    walk, in both `InputRegistry.add` and `Cache.key_hash`. On a miss, `Cache.memo` looks for a value under the
    pre-WPE1 tree-digest key (`Cache.legacy_key_hash`) only when the cache directory holds a candidate entry of the
    same key (a `.meta.json` without `v` and `alias_of`) that has not already failed to match for the same scope and
    input paths (`<key>.legacy-checked.json`). A cold cache or a new key costs no walk. When it finds a value, it
    writes an alias `.meta.json` with `alias_of`, so existing memos (robitaille `robitaille_rr_v2_*`) stay valid. Robitaille adapter tests with
    `-m adapters -x`, same seeded cache, measured 2026-10-11: 715 s before, 65 s on the first run after (one
    adoption walk), 35 s warm.

116. WPE1: the local-path check also scans shared resources (a vendored `res/` inside the portal and `<build>/_res/`),
    with the same G1 warning / G2 error rule. The build writes those documents, and non-default physics datasets,
    through `build.LabellingWriter`, which labels local paths in every JSON document (the templates index
    `meta.source` becomes `file:jwst_template_spectra.json`). `physics/templates.py` `load_source` still returns the
    absolute path; a direct `physics.write_shared` call outside the build writes it unlabelled.
117. WPE1: `CatalogSpec`, `ImageSetSpec` and `RecordSetSpec` take `noun=(singular, plural)` for the landing count
    nouns (`portal.json counts[].unit/units`). Robitaille's `mod` catalog needs `noun=("model", "models")` (adapter
    owner) to match its card text.
118. WPE1: deploy's ownership manifest `<target>/.portalkit-deploy.json` is served with the target and keeps the
    absolute `target` and the full `legacy_modules` paths that `status` uses. `deploy --htaccess` does not deny it
    yet (owner of `deploy/htaccess.py`).
119. **Narrative beats and the sky hash (WPD2; extends 71 and 72).** The store `sky` value carries two markers,
    `sky.beatView` and `sky.beatOvl` (`core/hash.js` `BEAT_VIEW`, `BEAT_OVL`). Each holds the id of the beat whose
    view, respectively overlay set, the value holds; `''` stands for the first beat of a page opened without
    `beat=`. A marker equal to `state.beat` (or `''` while no beat is set) means that part is implied by the beat:
    `skyImplied(state)` reports it, and `stateToHash` writes neither `sky=` nor `ovl=` for it. So a view or overlay
    set that a beat applied stays out of the hash, and `#beat=b3` reloads to the same view. The sky pane records
    the origin of the last viewer move: `store` (a goto to the stored view), `beat` (a goto the narrative asked
    for), `explicit` (a programmatic goto with a new view) or `user` (capture listeners `USER_VIEW_EVENTS` on the
    stage). Settles write as follows (`settledSky`): store origin writes nothing, so Aladin's rounding is absorbed;
    beat origin writes the view marked with the beat, and only when the narrative marked that view for the same
    beat; explicit and user origins drop `beatView`, so a pan or zoom after the beat writes `sky=`. A layer-panel
    change drops `beatOvl` (`layersSky`), so a toggle after the beat writes `ovl=`; a base-only change keeps it.
    `hashToPatch` marks the parts a `beat=` hash leaves out, and an explicit `sky=`/`ovl=` (including `-`) clears
    the marker. `#sky=..&beat=b3` therefore keeps the hash view: at load the narrative computes
    `explicitSky(sky, pageSky, {beatSet})`, and the parts it finds explicit are kept over the beat
    (`activate(k, {keep})`), with no highlight goto for a kept view. The narrative keeps a view marker only when
    loading `beat=` alone reproduces the view (`beatSky`): a beat without its own sky view, or with
    `suppress_view`, keeps the current view unmarked unless it equals the page default. The exception is a
    highlight-only beat (`goto`: a highlight, no `sky`, no `suppress_view`, a view not kept from the hash,
    target pane of type sky): the sky pane centres on the highlight, so the centre is the beat's, and the view
    is marked when its fov equals the page default, whatever the current centre. Tests:
    `tests/js/plotpanes.test.mjs` (settledSky, layersSky, explicitSky, beatSky, the narrative:highlight goto
    condition on a mounted narrative pane); `tests/e2e/test_deep_links.py`; `test_archetype_smoke.py` opens the
    synthetic highlight-only beat `b6` (`make_synthetic.HIGHLIGHT_ONLY_BEAT`: a highlight, no view, appended
    after b1..b5) and checks three rings, a sky view (pane stats and store) at the mean position of the three
    highlighted rows (away from the page default, so the check needs the goto), `beatView = b6` and the hash
    `#beat=b6`.
120. **Startup order and perf timing (WPD3; extends 100 and 112).** Skeletons (`.pk-skeleton`, base.css) are static
    (no animation or transition): an animated placeholder re-rasters every frame and, under software GL, holds
    first paint back by seconds. The list pane asks for its first screen of rows during mount, outside an
    animation frame, so the read does not wait behind a renderer stall. Boot calls
    `warmPaneModules(norm, registry, {portalId, pageId, tabMode})` before the hub is created: it imports the module
    graphs of the panes shown at load (layout `shownAtLoad(norm, {portalId, pageId, tabMode})`: saved fold state
    over the spec's `collapsed`, and only the active tab in tab mode; `tabModeQuery()` is the tab-mode media
    query). It reads no data and renders nothing; types registered in memory (`registry.isOverridden`) are
    skipped. Folded panes carry `pk-collapsed` from creation, so no CSS transition runs at startup. Perf timing
    (`tests/perf/test_perf_budget.py`): before the first timed page of the session the browser loads one untimed
    warm-up page of the same portal (`WARMUP_PAGES` order: cmd, hist, explorer), waits until it has been quiet
    for `QUIET_SETTLE_MS` and closes it; `PK_PERF_WARMUP=0` turns the warm-up off. Timed runs start from a fresh
    browser context, and the clock starts at `page.goto` (`PageRun.t_start`), not at context creation.
    `first_paint_from_open_s` (first paint counted from before the context was created), `open_s` and
    `first_paint_at_s` are reported in the perf report and not budgeted.
121. **Landing counts (WPE2; completes 113).** The landing pane reads `portal.json counts[]` first and fetches nothing
    else for the cards. Valid entries (a page id and an integer `n >= 0`) are grouped by page (`countEntries`).
    Nouns default from the kind (catalog: source/sources, images: image/images, records: spectrum/spectra, any
    other kind: item/items); a lone `unit` gets `unit + 's'` as its plural, and a lone `units` serves for both.
    `countLabel` sums entries with the same nouns and lists different nouns in first-seen order joined by
    `' · '` (`1,234 sources · 20 spectra`); `n == 1` takes the singular, and the card tooltip lists every
    dataset. A `counts` array, even an empty one, is authoritative. Without `counts` (portals built before WPE1)
    the pane falls back to the row counts of the `search` manifests, as before. `handle.stats().counts` is
    `'counts'` or `'search'`. The synthetic portal writes `counts` with build.py's rule (`portal_counts`).
122. **Context backdrop from the pre-binned grid (WPE2; replaces the caveat of 114).** For each context catalog
    (`context: true`, or the pane's `background`) the scatter pane looks for a manifest `density[]` entry without
    `stat` whose `x`/`y` equal the current axes, on linear axes only (`contextGridEntry`). When one exists, the
    grid is read with `client.density(key)` at data load, next to the foreground grid and before any column, and
    drawn as the gray backdrop (`contextGridData`, shifted by the `dm` fold); no context column is read and no
    worker binning runs. Only catalogs without a matching grid (or whose grid cannot be read) are binned with
    `bin2d` at settle time, as before. Decoded grids are cached per catalog and grid key, so an axes round trip
    or a `dm` change refetches nothing, and the backdrop keeps the decoded `values` array as its `counts`, so
    canvas2d's colourise cache (note 103) hits on every redraw. Grid cells (small multiples) use the same rule
    per cell. `stats().context` is `{prebinned, binned}`. With this, `first_paint_files(man, context=True)` of
    note 114 matches what the browser reads before first paint for panes whose axes have a context grid.
123. **Animation-frame lookup at call time (WPE2).** `panes/scatter.js` used to bind `requestAnimationFrame` when the
    module was imported, falling back to a 16 ms `setTimeout` when it was absent. Under node the module is
    imported before a test installs its stand-in, so draws ran on the 16 ms timer, and the test
    `scatter: firstPaint resolves at the first data draw` passed or failed depending on timer order (5 of 20
    runs failed). `raf`/`caf` now look the function up at each call and remember which clock issued the handle;
    30 consecutive runs pass. `ui/controls.js` binds `raf` at import in the same way (owner of that file).
124. **Template sources labelled at load (WPE3; extends 118).** `physics/templates.load_source` used to put the
    absolute template path in `meta.source`, and only the writer's labelling pass replaced it.
    It now writes `source_label(path, labeler)`: `labeler(path)` when given (the build's `PathLabeler`
    gives `<label>:<relpath>`), else `file:<basename>`. `templates.write(spec, dest, ...)` takes the
    labeler from a `LabellingWriter` `dest` when none is passed, so a build stages the root label and a
    direct call never stages an absolute path. ARCHITECTURE §9.3 "Served paths".
125. **Deploy bookkeeping denied over HTTP (WPE3, WPE4; extends 118).** The ownership manifests and
    `.portalkit_backup/backup.json` keep absolute paths, and they are served with the target. The portalkit
    `.htaccess` carries `RedirectMatch 403 "/\.portalkit[^/]*(/|$)|\.pk-tmp$"` inside `<IfModule mod_alias.c>`.
    `RedirectMatch` is in the `FileInfo` override class, which the cache headers already need. A `Require` rule
    needs `AuthConfig`, and Apache answers 500 for a directory whose `.htaccess` uses a class it does not allow.
    WPE3 added the rule to the `--htaccess` file and `write_backup_htaccess`. WPE4 applies it everywhere:
    - `deploy` writes it with or without `--htaccess`, before the first manifest write. Without the flag it
      writes a minimal managed file (marker and rule only) or updates a portalkit-owned file, keeping cache
      headers an earlier `--htaccess` wrote. A foreign or symlinked `.htaccess` is left unchanged with a warning.
      With the flag, a foreign file stays an ownership conflict.
    - `backup.py` writes `.portalkit_backup/.htaccess` before anything else in that directory. A foreign or
      symlinked file there refuses a `--replace` plan.
    - The dev server answers 403 for `htaccess.denied` paths and hides them in listings.
    - `verify --http` sends HEAD for the manifest URL. When the target holds the deny rule, any answer other
      than 403/404 is a warning; without the rule, a 200 is a warning. Warnings do not fail verify.

    The marker line is unchanged (it still names `--htaccess`), so files written by earlier versions remain owned.
    ARCHITECTURE §11.5, docs/deploy.md ".htaccess".
126. **Robitaille nouns and imaging legacy files (WPE3).** The `mod` catalog has `noun=("model", "models")`, so
    the landing count reads "N models". The imaging page maps `imaging/index.html` and `imaging/gallery.html`
    with `replace = false`: `imaging/` is a symlink into the data tree, `deploy --replace` refuses symlink
    components (§11.3), and neither file is ever stubbed or backed up. `deploy` lists them as plan warnings and
    `status` reports them.
127. **Exposure scan cost (WPE3, WPE4).** The old walk listed a real directory once per symlink route. WPE3 lists
    each real directory once per scan and re-labels the children per route. The result is identical, checked
    against a reference copy of the old walk. The cache (format 3) adds per-directory
    `(dev, ino, mtime_ns, ctime_ns)` stamps, so an expired result is revalidated with one `stat` per unchanged
    directory, and every symlink is resolved again. A stamp taken within 5 s of the directory's own change is
    not trusted.

    Measured with warm FS metadata, on the real roots:
    - old walk: 10.1 s;
    - new walk: 8.8 s;
    - revalidation: 6.2 s with 0 `scandir`.

    The first old run with cold metadata took 589 s. With `$PK_CACHE_DIR` set, `~/.cache/portalkit` is also
    read, read-only.

    WPE4 adds `$PK_EXPOSURE_CACHE_S`, read by `scan()` when `cache_s` is not passed (`default_cache_s`). A
    finite number ≥ 0 is used; empty, non-numeric, NaN, infinite and negative values fall back to 3600.
    `tests/conftest.py` sets it to 86400 in a `pytest_runtest_setup` hook (`tryfirst`, so before module
    fixtures) for items marked `adapters` and removes it for other items, unless the user set it. Adapter tests
    then reuse the user's last scan without revalidating. Real deploys keep 3600 s unless the user sets the
    variable. ARCHITECTURE §11.2.
128. **dirgallery_sites registry size (WP23).** `adapters/dirgallery_sites.py` is about 630 lines, above the §9.5
    target of 400. Most of it is the `GALLERIES` registry: 25 unified pages with their legacy sources, subset
    regexes, roles, blurbs and replace policy. The site toml schema has no place for this per-gallery data, so
    the adapter holds it as a data table.
129. **dirgallery_sites legacy files and links (WP23).** Only the brick_2221 pages may be stubbed by
    `--replace` (`STUB_PORTALS`). Legacy files reached only through a served symlink are mapped
    `replace = false`, as are the critic's galleries and the robitaille pages. Deploy stubs use relative URLs,
    and deploy refuses symlink components (§11.3), so a stub there could not be written safely.

    Cross-visibility links take two forms:
    - portal.json `related` (`build.py` `portal_doc`) lists every other portal of the site as
      `{title, url, auth}`. dirgallery_sites has 9 portals of mixed visibility, so its public portals link to
      secure gallery URLs. These are links only, with no data or counts.
    - Each landing page has cards for the other gallery sets (`adapters/dirgallery_sites.py`). These cards also
      link public portals to secure gallery URLs, marked "login required"; only the URL and title cross the
      visibility boundary.
130. **Narrative activation order (WPE4; fixes the WPE2 todo).** With instant scrolling (`prefers-reduced-motion`
    gives `.pk-nar-scroll` `scroll-behavior: auto`), `activate(k, {scroll: true, keep})` used to call
    `scrollIntoView` and `arrived()` before setting `S.current`. `arrived()` → `unpin()` → `pickActive()` then
    ran `activate(k)` again without `keep`, which emitted `narrative:highlight` with `goto: true`. A deep link
    `#sky=…&beat=<highlight-only beat>` lost its view.

    `activate` now applies the beat first (`applyBeat`: classes, `S.current`, store writes, highlight event),
    then pins, scrolls and calls `arrived()`. Re-entry finds `k === S.current` and does nothing. The node tests
    check this:
    - 'deep-link view kept over the beat with an instant scroll' (formerly `todo`);
    - 'an instant-scroll activation runs once': one highlight per `go()`, one goto for a view beat, the deep
      view kept across hash beat changes.

    The beat-marker hash rule (note 119) is unchanged.
131. **Images presets that select nothing (WPE4; WP23 follow-up).** `bundle/images.py` drops preset values that no
    item has, as before. A preset facet left with no values used to be written as an empty list. `gallery.js`
    reads an empty list as "no filter" (`normFacets`/`passesFacets`), so the preset became select-all. Facets
    combine with AND. So a preset with any facet that matches no item (an unknown facet, an empty value list,
    or no listed value present) selects nothing, and it is dropped with the warning
    "facet(s) [...] match no image; preset dropped". An explicit `{}` preset (all images) is kept. No JS change.
132. **`ui/controls.js` frame lookup (WPE4; completes 123).** `rafSetter` looked `requestAnimationFrame` up at
    import, so under node it always used the 16 ms timer. `raf` now looks it up at each call (timer fallback
    when absent), as `scatter.js` does. A node test installs a fake `requestAnimationFrame` after import and
    checks one coalesced write per frame, then removes it and checks the timer fallback.
133. **Deny-only `.htaccess` and `AllowOverride` (WPE4 review).** A plain deploy writes a `.htaccess`
    (`RedirectMatch`, class `FileInfo`). Apache answers 500 for every file of a directory whose `.htaccess`
    uses a class `AllowOverride` does not allow. The 28 `.htaccess` files under `/orange/adamginsburg/web` use
    only AuthConfig, Options and Indexes directives, so whether the host allows `FileInfo` is open (§16 Q4).
    The file stays on by default as item 5b asks. Three safeguards:
    - `PK_DENY_HTACCESS=0` (or `deploy(..., deny_htaccess=False)`) turns the deny-only file off; the plan
      warns that the manifest is served. `--htaccess` is unaffected.
    - `verify --http` reports a 5xx from the manifest URL as undecided (`manifest_denied = None`) with an
      `AllowOverride` warning. `SERVED` is reserved for 2xx answers.
    - 5xx answers on owned files, in a target holding a portalkit `.htaccess`, add the same warning.

    `htaccess._write_owned` compares bytes, so an owned file with undecodable bytes after the marker is
    rewritten instead of raising `UnicodeDecodeError`.
