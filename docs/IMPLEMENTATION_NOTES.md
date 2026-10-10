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
29. **Exposure scan cost.** A cold walk of `/orange/adamginsburg/web/secure` takes ~700 s (dominated by
    `salt/dihca2/diagnostics_nopeaks` and `ACES/mosaics/lines`); results are cached for 1 h in `$PK_CACHE_DIR` or
    `~/.cache/portalkit`. Prune globs for huge leaf directories are a planned follow-up.

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
