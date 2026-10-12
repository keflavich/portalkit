# portalkit JavaScript: core, layout and pane authoring

This guide covers the browser runtime under `web/js/`. It mirrors ARCHITECTURE.md section 6 and
records the conventions the core modules use. All code is vanilla ES modules with relative
imports; nothing is bundled and nothing comes from npm.

Pure modules (no DOM, loadable by `node --test`): `core/constants.js`, `core/store.js`,
`core/selection.js`, `core/hash.js`, `core/fmt.js`.

```
web/js/core/boot.js        page start-up, App object
web/js/core/store.js       key/value store with coalesced listeners and events
web/js/core/selection.js   Selection class, ids/bits/r serialization, raw DEFLATE codec
web/js/core/hash.js        canonical URL hash <-> store
web/js/core/carry.js       cross-page focus/selection carry, BroadcastChannel focus
web/js/core/keys.js        global keyboard shortcuts
web/js/core/theme.js       light/dark/auto theme, CSS token reader
web/js/core/loadscript.js  classic script loader with fallbacks; loadCSS
web/js/core/registry.js    pane type -> module path and CSS
web/js/core/testhooks.js   window.__pk (only with ?pktest=1)
web/js/core/fmt.js         number/coordinate/text formatting
web/js/layout/layout.js    split panes, folds, pop-out, mobile tabs
web/js/data/client.js      DataClient: the main-thread side of one catalog's data worker
web/js/workers/data.worker.js  catalog engine (chunk decode, rows/record, query, resolveIds)
web/css/{tokens,base,layout,print}.css, web/css/panes/ui.css
```

## 1. Boot

`shell.html` imports `core/boot.js` and calls `boot()`. The order is fixed:

1. Parse `<script id="pk-page">` (page.json). The shell's inline script has already applied the
   stored theme.
2. Build the store: defaults <- `page.state` <- carry (`takeCarry`) <- legacy hash mapping <-
   canonical hash. The legacy step runs only when `page.page.legacy.grammar` is non-empty; it
   imports `./legacyhash.js` dynamically and calls `mapLegacy(grammars, {hash, search, page})`
   (IMPLEMENTATION_NOTES 90; `page` is the parsed page.json, so mappings can read page ids and
   defaults).
   Consumed query keys are removed from the URL with `replaceState`, and the canonical hash is
   written once boot finishes.
   Boot then starts importing the module graphs of the panes the layout will show at load
   (`warmPaneModules(norm, registry, {portalId, pageId, tabMode})`: layout `shownAtLoad`, i.e.
   the saved fold state over the spec's `collapsed`, and only the active tab in tab mode;
   types registered in memory, `registry.isOverridden(type)`, are skipped). This fetches and
   evaluates code only: no data is read and nothing renders; `registry.load()` still attaches
   pane CSS and reports load errors at step 5.
3. `createHub(page, store)` from `data/hub.js`; manifests of every catalog used by a
   non-collapsed pane (and pane `background` catalogs) are fetched at once. When a catalog
   opens, manifold panes on it whose page defaults leave the frame preset open (no
   `options.frame`, no `page.state` preset) get the manifest's `manifold.default_frame` as
   their default preset (`frameDefault(options, manifest)`: `options.frame`, else
   `default_frame` when `manifestFrameKnown` accepts it, else `'pca'`), and boot passes the
   updated defaults to `unbindHash.setDefaults`, so the default preset stays out of the hash.
   Boot also wraps `hub.records(ds)` for foreign record sets (section 11).
4. Runtime probe: WebGL2, `EXT_color_buffer_float`, `DecompressionStream`,
   `navigator.deviceMemory`. Each tiered pane gets `tier:<pane>` once its catalog's N is known.
5. Header chrome and `mountLayout` (pane modules load lazily through the registry). Boot loads
   `css/tokens.css`, `base.css`, `layout.css`, `css/panes/ui.css` and `print.css` (media print) and awaits
   them before `mountLayout`, so the shared widget rules (note 108) are in place when the first
   pane mounts.
6. `<body data-caps="...">` (enabled caps, sorted, space separated) and
   `<body data-tier="pane:T1 ...">`.
7. `bindHash` (with `applyInitial: false`; the hash was applied at step 2), `bindKeys`,
   `bindTheme`, test hooks. Panes mount before the hash binds, so a pane write made in
   between (for example a sky pane settling a beat view) would not reach the hash; `bindHash`
   detects that the store differs from `location.hash` at bind time and schedules one write.
8. `store.emit('ready', {ms})`.
9. `app.afterFirstPaint()` resolves (see below); boot then calls `client.allowPositions()` on
   every catalog opened so far and on each one opened later.

On failure boot shows a `.pk-error-note` and rethrows, so the shell's fallback message also
appears and `__pk.ready` rejects.

### Tiers

`tierFor(n, runtime, prev)` in `boot.js`:

| N | WebGL2 | float RT | tier |
|---|---|---|---|
| <= 3000 (T0_MAX) | any | any | T0 |
| <= 300000 (T1_MAX) | yes | any | T1 |
| > 300000 | yes | yes | T2 |
| > 3000 | no | any | F |
| > 300000 | yes | no | F |

With `prev` set, the boundaries move by 10 % against a change (hysteresis). Tier F keeps the
`density_rendering` capability (pre-binned fallback), so `data-caps` always equals the resolver
output in `page.caps.enabled`; `app.notes` records the runtime reason.

### The App object

| Field | Meaning |
|---|---|
| `page` | parsed page.json |
| `store` | the store (section 2) |
| `hub` | data hub (`catalog(ds)`, `records(ds)`, `json(ds)`, `resolve(url)`, `stats()`) |
| `layout` | layout API (section 5) |
| `selections` | `Map<ds, Selection>` for catalog datasets opened so far |
| `caps` | `Set` of enabled capabilities; `disabled` maps cap -> reason; `notes` cap -> runtime note |
| `runtime` | `{webgl2, floatRT, decompressionStream, deviceMemory, maxTexture}` |
| `primaryDs` | the catalog whose selection and filters live in the hash (first catalog in pane order) |
| `emit(topic, payload)`, `on(topic, fn)` | store events |
| `theme()` | `theme.tokens()` |
| `catalog(ds)` | cached `hub.catalog(ds)`; also creates the dataset's Selection |
| `selectionFor(ds)` | `Promise<Selection>` |
| `afterFirstPaint()` | the page's first-paint promise (note 100), the same promise on every call; see below |
| `firstPaintAt()` | `Promise<number>`: epoch ms at which `afterFirstPaint()` resolved (note 112) |
| `resolveIds(ds, ids)` | `Promise<number[]>` stored row indices, -1 when not found. A row matches when its id equals the text exactly (first stored row wins) or the text is one of its aliases (manifest `ids.alias_file`); substring hits never count. Ids already in the id cache, or resolved by an earlier call, answer at once. The rest (deduplicated) go to the worker in one `client.resolveIds(uniq, {aliases: true})` pass (note 102), which loads every ids chunk and the alias file once; resolved rows are memoised per dataset. Clients without `resolveIds` (test doubles) use the older path: `RESOLVE_BATCH_MIN` (16) or more unknown ids are matched in one pass over `client.export({mask: 'all', format: 'ids'})` (`matchIdList`) and the alias file, fewer go through `client.find` one at a time |
| `idOf(ds, i)` | `Promise<string>` cached id lookup |
| `applySelection(ds, str)` | apply an `ids:`/`bits:`/`r:` string as one replace (recipes replay on an empty scratch Selection through `client.brush`, `client.query` and presets, then load with `sel.replaceWith`) |
| `selectionHit(ds, str)` | `Promise<Uint8Array \| null>`: the rows an `ids:`/`bits:`/`r:` string selects, computed on a scratch Selection without touching the dataset's selection (null when unreadable). `ui/chips.js` uses it so recipe presets combine with shift (add) / alt (subtract) |
| `foreignRecordKey(ds, id)` | `Promise<string \| null>`: record key of catalog row `id` for a foreign record set with `id_column` (the row's value in that column), else the id itself |
| `carryPayload()` | `{ds, focusId, sel}` for `stashCarry` |
| `disableCap(cap, reason)` | runtime removal (e.g. WebGL context lost for good); updates `data-caps` and emits `caps:change` |
| `hashLink()` | the canonical hash string for "copy link" actions (no side effects) |
| `hashLinkInfo()` | `{str, dropped, omitted}`: `dropped` = the selection is too long for the link, `omitted` = the link leaves a non-empty selection out (copy-link actions warn on it) |
| `slots` | `{header, nav, tools, main, selbar}` DOM slots for UI widgets |
| `gpuBudget` | the page's one `gl/budget.js` `Budget` (note 68). Every GPU pane requests and releases bytes through it (`ctx.gpuBudget` is the same object); panes never create their own |
| `importCsv(text, {mode, ds, radiusArcsec})` | CSV / id-list import into a selection (note 93); see below |

`app.importCsv(text, opts)` is the runtime entry for the import dialog (`ui/export.js`). `mode`
is one of `replace` (default), `add`, `subtract`, `intersect` (anything else throws
`TypeError`); `ds` defaults to `primaryDs` and must be a catalog. The text is parsed with
`parseImportText` and matched with `resolveImport` from `ui/export.js` (ids and aliases through
`app.resolveIds`; positions through `client.crossmatch` when the manifest has `pos`), and the
matched rows are applied as one `sel.setIndices(rows, mode)` (an `ids` recipe op, so it can go
in the URL). It resolves to `{ds, mode, format, entries, skipped, matched, byId, byPosition,
missing, missingLabels (first 50), rows, count, summary}`, where `count` is the selection size
afterwards and `summary` is the one-line `importSummary` text. Text with no recognisable entry
leaves the selection unchanged. The pure core is exported as `importSelection(text, {mode, sel,
client, io, resolveIds, radiusArcsec})` for tests.

**First paint** (`app.afterFirstPaint()`, IMPLEMENTATION_NOTES 99/100). One promise per page,
created before any pane mounts, so panes may call it during mount. `whenFirstPaint(root)`
resolves it after the `ready` event, once no visible `section.pk-pane` has
`data-state="loading"`, at the next idle callback (`requestIdleCallback`, timeout
`FIRST_PAINT_IDLE_TIMEOUT_MS` = 1500 ms). It resolves anyway, with a console warning,
`FIRST_PAINT_MAX_WAIT_MS` (30 s) after `ready`, so a pane whose mount waits on deferred data
cannot hold it back for good. Whole-column reads, the ids and pos wait on it: sky markers load
pos after it, the list loads full columns after it, and boot calls `client.allowPositions()`
(section 11) once it resolves. A pane must therefore never await work that itself waits on
first paint before it leaves the loading state: sky and wavelength panes start
`markers.refresh({force: true})` without awaiting it and register their view listeners
(`positionChanged`, `zoomChanged`, `resizeChanged`) right away; the sky pane also emits
`narrative:sync` without waiting for the markers.

A pane leaves `data-state="loading"` only at its first data paint (IMPLEMENTATION_NOTES 112,
section 5): the list, table, gallery, scatter, manifold and hist handles expose `firstPaint`,
so first paint (and with it every deferred column, ids and pos read) waits until their first
screen is drawn. `app.firstPaintAt()` returns a Promise of the epoch-ms time (`Date.now()`)
at which `afterFirstPaint` resolved; `__pk.firstPaintAt()` (section 12) exposes it to tests.

Selections: boot uses `client.selection()` when it returns an object with `apply`, `mask` and
`onChange` (a `core/selection.js` Selection); otherwise it creates `new Selection(client.n)`.
Selection changes are mirrored into `sel:<ds>` = `{version, count}` with source `'selection'`.

Id prefetch for the hash: while a selection's `ids:`/`r:` form needs ids that are not cached,
boot fetches them (`app.idOf`) for up to `PREFETCH_IDS_MAX` (1000) rows of
`sel.recipeIndices()` and then rewrites the hash once; above that the hash uses `bits:` or the
other recipe ops.

Focus: writers may set `focus = {ds, i, id: null}` or `{ds, i: null, id}`; boot fills the
missing half asynchronously (source `'boot'`), including once at startup for a focus that came
from the hash, a carry or a legacy mapping. Boot broadcasts `{id, ds}` to other tabs of the
same portal; a tab adopts it only when `ds` is its primary dataset, and the hash binding
replaces (does not push) the history entry for those changes. An `{i, id: null}` focus is
pending for the hash: nothing is written until the id arrives, so one step makes one history
entry. The keyboard fallback (`nav:step` unhandled) looks the id up first and sets the focus
once.

## 2. Store (`core/store.js`)

```js
const store = createStore(initial, {schedule?});
store.get(key); store.has(key); store.keys(); store.snapshot();   // snapshot is frozen and cached
store.set(patch, {source: paneId, history?: boolean});
const off = store.on(keys, (changed, store, sources, {history}) => {...}, {sync?: boolean});
store.emit(topic, payload); store.onEvent(topic | '*', (payload, topic) => {...});
store.batch(() => {...}); store.flush();
```

- `keys` is `'*'`, an array of exact keys, or prefix patterns ending in `*` (`'sel:*'`).
- A set whose value is `Object.is`-equal, or shallow-equal for plain objects and arrays, is
  ignored. Setting an absent key to `undefined` is ignored. Always pass a new object to change
  a nested value.
- Default listeners are coalesced to one call per animation frame (setTimeout(0) under node,
  with a 100 ms timeout fallback when rAF is paused). `changed` and `sources` hold only the keys
  the listener subscribed to, accumulated since its last call.
- `{sync: true}` listeners run at the end of every `set` (or outermost `batch`). Use them only
  for the hash binding, tests, and cheap bookkeeping.
- `history: true` asks the hash binding to push a history entry.
- Listener exceptions are rethrown on a microtask; other listeners still run.
- Every `set` names a `source` (normally the pane id) so a pane can skip its own echo:
  `if (sources.size === 1 && sources.has(ctx.id)) return;`.

The store key table is in ARCHITECTURE.md section 6.2. Defaults created by boot:
`focus/hover = null`, `color = {by: null, cmap: 'viridis'}`, `av = 0`, `rv = 3.1`, `dm = 0`,
`speed = 1` (or the first manifold / wavelength `options.speed`),
`template = null`, `features = []` (or the first spectrum1d `options.features`),
`spec = {pinned: [], variant: 'flux'}`, `sky = null`, `rung/beat/role = null`, `facets = {}`,
`lightbox = null`, `layout = {popout: {}}`, per catalog `filters:<ds> = {}`,
`vis:<ds> = {version: 0, count: null}` (count null until a filter is applied), and per
non-context catalog `sel:<ds> = {version: 0, count: 0}`, `annot:<ds> = {version: 0}`. Scatter
panes get `axes:<pane>` from their `x/y/xlog/ylog` options; manifold panes get
`frame:<pane> = {preset: options.frame ?? null, dims}` (null: the pane opens on the manifest's
`default_frame` when it names a known preset, else `'pca'`; boot fills the page default once
the manifest is read, section 1). Scatter panes read `layers:<pane>` (absent by default):
`{raster?: false, ovl?: [ids]}`. `page.state` is merged on top, one
level deep for objects.

## 3. Selection (`core/selection.js`)

Semantics (frozen): `replace: sel = hit`, `add: sel |= hit`, `subtract: sel &= ~hit`,
`intersect: sel = (sel & hit) | (sel & ~drawable)`. Modifiers: none = replace, Shift = add,
Alt = subtract, Shift+Alt = intersect (`opFromEvent`).

```js
const sel = await app.selectionFor('src');
const {hit, drawable} = await client.brush(view, shape);
sel.apply(hit, opFromEvent(ev), drawable, {op, kind: 'rect', view: {ds, x, y}, shape});
sel.setIndices([3, 17], 'add');           // records an ids op; ids are resolved at serialize time
sel.clear(); sel.invert(drawable); sel.undo(); sel.redo();
sel.replaceWith(mask, recipeOps, recipeOk);  // one undoable replace that carries a recipe
sel.onChange((sel, kind) => {...});      // boot already mirrors into sel:<ds>
```

Always pass a RecipeOp when one exists; without it a catalog above 200 000 rows cannot put
the selection in the URL. Undo depth is `min(20, floor(64 MiB / n))`, at least 1.

Serialization (`serialize(idOf)` / `Selection.parse(s)`):

| Form | When | Payload |
|---|---|---|
| `''` | empty, or n > 200 000 with an incomplete recipe (or an `_idx` op whose id is not cached) and no `idOf` that spells the selected rows | |
| `ids:<id>,<id>` | recipe form of a single `replace` ids op with 1..300 ids | `encodeURIComponent` per id |
| `r:<b64url>` | recipe form of any other complete recipe | base64url(deflate-raw(UTF-8 JSON RecipeOp[])); `r:[...]` raw JSON is also accepted |
| `bits:<b64url>` | n <= 200 000 | base64url(deflate-raw(packed bits, LSB first)) |

Choice of form (IMPLEMENTATION_NOTES 92, refined by 107): `serialize(idOf)` computes the recipe form
(`recipeForm(idOf)`: `ids:` or `r:`, `''` when the recipe is incomplete) and the bitmask form
(`bitsForm()`, `''` above 200 000 rows) and writes the one that is shorter in the URL
(`hashLength(s)` = length after `encodeHashValue`, so `%`-escaped ids count as written); a tie
goes to the recipe. With a bitmask form available, the recipe competes only when
`sel.recipeExact` is true: every op is an `ids` op, a `preset` op, or a `filter` op that carries
its `filters`. Brush ops (`rect`/`circle`/`lasso`) replay against the rows drawable under the
filters in force at load time, so a brush selection on n <= 200 000 rows is always written as
`bits:` and restores the same rows after a filter change; above 200 000 rows the recipe (with
its brush ops) is the only form. The choice depends only on the selection (its recipe and mask),
so a selection restored from the URL writes the same form before and after a reload.

`idOf` spells index-only ids ops (`setIndices` records `{kind: 'ids', _idx: rows}`);
`sel.recipeIndices()` lists the rows those ops reference (including rows a `subtract` op
removed). While any of those ids is unknown the recipe form is `''`: boot's hash binding then
uses `bits:` and rewrites the hash once the prefetched ids arrive. When `idOf` covers only the
selected rows (e.g. `ui/chips.js`) and the recipe form is `''` (exact recipe with an unknown
id, or no bitmask form), an `idOf` that spells every selected row (count <= 300) gives the
`ids:` form of the selected rows, which is the compact spelling of the equivalent replace-ids
recipe.

`deflateRaw`/`inflateRaw` are synchronous (fixed-Huffman LZ77 encoder; full stored/fixed/dynamic
decoder) so serialization stays synchronous. `b64urlDecode` also accepts standard base64.

RecipeOp: `{op, kind: 'rect'|'circle'|'lasso'|'ids'|'filter'|'preset', view: {ds, x?, y?, xlog?, ylog?, pane?}, shape?, ids?, filters?, key?}`.
Shapes are in data coordinates. Projection (manifold) brushes are replayable only through
`ids`; the manifold pane should record `{kind: 'ids', ids}` for them when the count allows.

## 4. Hash grammar (`core/hash.js`)

Keys appear in this order; pane-scoped keys sit after `f`, grouped by pane, and unknown keys
come last in alphabetical order:
`id, sel, f, <pane>.x, <pane>.y, <pane>.frame, <pane>.xr, <pane>.yr, <pane>.raster, <pane>.ovl, c, av, rv, dm, speed, tpl, feat, spec, wl, sky, base, ovl, rung, beat, role, fac, lb, view`.

| Key | Store | Format |
|---|---|---|
| `id` | `focus` | source id; `focus.ds` = primary dataset |
| `sel` | `sel:<primary>` | section 3 |
| `f` | `filters:<primary>` | base64url(UTF-8 JSON); raw JSON `{...}` accepted |
| `<pane>.x`, `<pane>.y` | `axes:<pane>` | expression text |
| `<pane>.frame` | `frame:<pane>` | preset name, or `b64:` + base64url(float32 LE `F`) |
| `<pane>.xr`, `<pane>.yr` | `axes:<pane>.xr` / `.yr` | `lo,hi` (zoom range; `-` = auto range) |
| `<pane>.raster` | `layers:<pane>.raster` | `0` raster layers hidden, `1` shown (absent = shown) |
| `<pane>.ovl` | `layers:<pane>.ovl` | comma list of the plot overlay ids shown (`-` = none; absent = every `options.overlays` layer) |
| `c` | `color` | `by~cmap~lo~hi` (trailing parts optional; `none` = no colouring) |
| `speed` | `speed` | positive number: tour / playback speed factor (manifold Tour.setSpeed, clamped to 0.1-8) |
| `av`, `rv`, `dm` | same | numbers: 1e-6 absolute precision for magnitudes >= 1e-3, 7 significant digits below (all numeric values in the hash use this rule) |
| `tpl` | `template` | `key~grid~scale~band,band` |
| `feat` | `features` | comma list |
| `spec` | `spec` minus `xr` | base64url JSON |
| `wl` | `spec.xr` | `w0,w1` |
| `sky`, `base`, `ovl` | `sky` | `ra,dec,fov`; base survey id; `id:opacity,id` (`-` = no overlays) |
| `rung`, `beat`, `role` | same | strings |
| `fac` | `facets` | base64url JSON |
| `lb` | `lightbox` | `set,index` or `index` |
| `view` | `layout.popout` | `pane:half,pane:full` |

Values keep `, : / @ ! * ( ) ~` readable; everything else is percent-encoded. Store values that
equal the page defaults are left out. On navigation, keys absent from the hash restore the
default; `id`, `lb` and `view` absent mean "no focus", "no lightbox", "no pop-out".

Cleared values: when the page default has a key and the current state is empty (colour-by
off, no template, no features, no rung, ...), the key is written with the marker `-` (`c=none`
for colour), e.g. `#c=none&feat=-&tpl=-`. `hashToPatch` reads `-` for every key (`id` no focus,
`f`/`fac` `{}`, `<pane>.x`/`.y` null, `tpl`/`rung`/`beat`/`role`/`lb` null, `feat` `[]`,
`wl` no range, `sky` no centre, `base` null, `ovl` `[]`, `view` no pop-out, `sel` empty), so
a reload or Back keeps the cleared state.

Narrative beats (WPD2): the store `sky` value may carry two markers, `sky.beatView` (hash.js
`BEAT_VIEW`) and `sky.beatOvl` (`BEAT_OVL`): the beat id whose view, respectively overlay set,
the value holds; `''` stands for the first beat of a page opened without `beat=`. A marker equal
to `state.beat` (or `''` while no beat is set) means that part is implied by the beat:
`skyImplied(state)` reports it and `stateToHash` leaves it out of `sky=`/`ovl=`, so a page on
beat b3 with b3's view writes `#beat=b3`, and a reload reproduces it. `hashToPatch` marks the
parts a `beat=` hash leaves out, and an explicit `sky=`/`ovl=` (including `-`) clears the
marker; the narrative pane then keeps a view marker only when loading `beat=` alone reproduces
that view (`beatSky`: a beat without its own sky view or with `suppress_view` drops it unless
the view equals the page default, so the view is written as `sky=`; a highlight-only beat whose
highlight the sky pane centres on (goto) keeps it when the fov equals the page default). On navigation, page defaults are still restored for parts the beat does not imply.
An unknown beat id falls back to the first beat and is canonicalized to it. The sky pane
records what last moved the viewer (`store`, `beat`, `explicit` or `user`; user = capture
listeners for pointerdown/wheel/touchstart/keydown/dblclick on the stage, `USER_VIEW_EVENTS`):
a settle after a store-origin goto writes nothing (Aladin's rounding is absorbed), beat-origin
settles are written marked, explicit and user settles drop `beatView`, and layer-panel changes
drop `beatOvl`. The sky pane's store listener moves the viewer only when the stored view
changes. Helpers: `settledSky`, `layersSky` in panes/sky.js; `explicitSky`, `beatSky`,
`sameSky` in panes/narrative.js.

Narrative activation order (IMPLEMENTATION_NOTES 130): `activate(k, {write, scroll, keep})`
applies the beat first (`applyBeat`: section classes, `S.current`, store writes with `keep`,
the `narrative:highlight` event), and only then pins the section, calls `scrollIntoView` and
`arrived()`. With instant scrolling (`prefers-reduced-motion` gives `.pk-nar-scroll`
`scroll-behavior: auto`) `arrived()` runs `unpin()` → `pickActive()` at once; that re-entry
finds `k === S.current` and does nothing, so a `#sky=..&beat=<highlight-only beat>` deep link
keeps its view (no second highlight with `goto: true`). Code that adds work to activation puts
state changes in `applyBeat`, never after the scroll.

Overlays (IMPLEMENTATION_NOTES 72): `sky.overlays` undefined means "the layout default"
(`options.overlays` of each pane) and writes no `ovl`; an array writes `ovl=<list>`, and an
empty array writes `ovl=-`, independent of the page default, so an explicit "no overlays"
survives a reload. On navigation, an absent `ovl` removes `sky.overlays` (back to the layout
default) unless the page default itself carries an overlay list. Panes read `undefined` as
"use my `options.overlays`" and `[]` as "none".

Writing: `replaceState` debounced by 250 ms; a change of focus id (or any set with
`history: true`) uses `pushState` at once, so Back returns to the previous source. Reading:
`hashchange` and `popstate`, applied with source `'hash'`. When the hash would exceed 8000
characters, `sel` is dropped; the store event `hash:sel-dropped {ds, length}` fires once when
writes start dropping it (boot shows one toast), not on every write.

Selections the hash cannot hold (dropped for length, or `serialize()` = `''` for a non-empty
selection above 200 000 rows with an incomplete recipe) are recorded in
`history.state.pkSelOmitted` of each written entry (the entry being left on a push is marked
too). On Back/Forward to an entry without `sel`, the live selection is kept when the entry is
marked, or when it is unmarked and the current selection cannot be represented; otherwise the
selection is cleared. The `selection` adapter passed to `bindHash` is
`{serialize(), apply(str), count()}`.

`bindHash(store, opts)` returns `unbind` with `.flush()`, `.touch()` (debounced rewrite),
`.replaceNow()`, `.compute()` (string, no side effects), `.computeInfo()`, `.selDropped()` and
`.setDefaults(defaults)` (replace the page defaults the hash is compared against; boot calls it
when a manifest supplies a manifold default frame). On navigation, absent `<pane>.xr`/`.yr`
reset the zoom range to auto and absent `<pane>.raster`/`.ovl` remove those layer settings
unless the page defaults carry them.
WP13's `legacyhash.js` maps legacy forms to this grammar (`mapLegacy` returns
`{hash, consumedSearch, fromHash}` with canonical key/value strings). When a grammar mapped the
hash itself (`fromHash`), boot uses only the mapped values; when only query parameters were
mapped, the mapped values are merged over the canonical hash. Query-string grammars
(`gc-query`, `spherex-colours`, and the `?speed=` part of `wavelength` and `kv-view`) always
apply and merge.

Legacy mapping details:

- Dim alias (`page.page.legacy.dim_alias = {legacy: unified}`): `kv-view` maps `x`/`y`/`z` and
  the column part of `c` through it, and `spherex-colours` maps colours `c_<A>_<B>` and
  magnitudes `m_<A>` through it (unaliased filter names become lower-case `<a>-<b>`).
  `mapLegacy` reads the table from `loc.dimAlias`, else `loc.page`, else the page JSON.
- Speed: `wavelength` and `kv-view` (the manifold pages) map `?speed=` (consumed from the query
  string, also under a canonical hash) and a legacy hash `speed=` onto the canonical `speed`;
  a value that is not a positive number is consumed and dropped. The shared vectors in
  `hash_vectors.json` and `portalkit/legacy/hashmap.py` still drop `?speed=`;
  `tests/js/legacyhash.test.mjs` lists those two vectors as known-divergent.
- `spherex-colours` (query string of the legacy SPHEREx colours page): `ax`,`ay` -> the CCD
  scatter's x colour `ax - ay` and `bx`,`by` -> its y colour (pane id `ccd`, else the first
  scatter); `band` -> the CMD scatter's y magnitude with the CCD x colour as x (pane `cmd`,
  else the second scatter; on a page with one scatter pane `band` and `cmd` are consumed and
  dropped); `cmd=1` -> `view=<cmd>:half`; `c` -> `c`; `zoom=x0,x1,y0,y1` ->
  `<ccd>.xr`/`.yr`; `yoo` (`1` = every `yoo_*` layer, else a comma list) -> `<ccd>.ovl` (the
  CCD's other overlays stay on; a CCD with `yoo_*` overlays always gets an explicit list,
  since the legacy page drew none without `yoo`); `hide=k1|k2` -> `f` with
  `cats: {<colour-by>: [k1, k2]}`.

## 5. Layout (`layout/layout.js`)

LayoutNode: `{row: [...]}`, `{col: [...]}` or a pane leaf
`{pane, id?, dataset?, size?, collapsed?, options?}`. `size` > 1 is pixels (fixed),
`size` <= 1 a fraction (flexible); unsized children share the remaining fraction.

- Split ids come from the tree path: `r0`, `r0.c1`, `r0.c1.r0`, ... Pane ids default to the
  type, with `-2`, `-3` appended on clashes.
- Splitters (`role="separator"`, focusable): the drag captures the pointer and all sizes at
  pointerdown and applies the total pointer delta to the two nearest non-collapsed neighbours,
  so the splitter stays under the cursor. Arrow keys move 16 px (64 px with Shift), Home/End
  go to the limits, Enter or double-click resets. Minimum pane size 48 px.
- Fold buttons collapse a pane to its header (a 30 px vertical strip inside rows).
- Pop-out: the pane element moves into a fixed layer (`half` = centred window,
  `full` = whole viewport); a placeholder keeps its place. One pop-out at a time; state lives in
  `store.layout.popout` (hash `view=`); Esc closes it.
- Below 900 px the layout switches to tabs: one pane at a time, a 16 px side gutter, no
  horizontal scrolling at 390 px.
- Persistence: `localStorage['pk:<portal>:<page>:layout'] = {v: '1:<tree signature>', sizes, collapsed}`;
  every access is wrapped in try/catch, and a different tree signature discards the record.
  Sizes are measured only from a laid-out desktop split: in tab mode (and for a split with an
  unmeasurable visible cell) the previous sizes are kept, fold buttons are inert and out of the
  tab order, and a saved split with any non-positive entry is ignored on load.
- Skeletons (`.pk-skeleton`, base.css) are static: an animated placeholder re-rasters every
  frame and, under software GL, delays first paint by seconds (WPD3).
  `shownAtLoad(norm, {portalId, pageId, tabMode})` returns the pane ids shown (and so mounted)
  at load; `tabModeQuery()` is the media query of tab mode. Folded panes carry `pk-collapsed`
  from creation, and all split classes are set before the first measurement, so no CSS
  transition runs at startup.
- Panes mount lazily the first time they become visible. A `ResizeObserver` calls
  `handle.resize(w, h, dpr)` once per frame and emits `pane:resize {pane, w, h}`.
- Pane state (IMPLEMENTATION_NOTES 112): `section.pk-pane[data-state]` is `loading` from the
  start of the mount until `mount()` resolves and, when the handle has a `firstPaint`
  Promise, until that settles: resolve gives `ready`, reject gives `error` (with a console
  warning; the pane keeps its own error note). A handle without `firstPaint` is `ready` at
  mount. While the first paint is pending, a pane hidden by a fold or an inactive tab is
  `paused` (it skips its draws, so it cannot paint) and returns to `loading` when shown; a
  paused pane does not hold back `afterFirstPaint`. A guard marks the pane `ready` with a console warning
  `LAYOUT_TIMING.firstPaintMaxWaitMs` (`PANE_FIRST_PAINT_MAX_WAIT_MS` = 30 s) after mount
  (`LAYOUT_TIMING` is mutable for tests). `whenMounted(id)` still resolves at mount. A
  destroyed pane's state is left alone. `firstPaintLatch()` returns `{promise, done,
  resolve(), reject(err)}`: the first call wins, later calls are ignored, and an unobserved
  rejection is handled; panes use it for their `firstPaint`.

- Titles (IMPLEMENTATION_NOTES 77): `options.title` (a non-blank string or a finite number)
  seeds the header text, the mobile tab text and the pane `aria-label`; otherwise the title
  is `PANE_TITLES[type]`. `ctx.setTitle(text)` replaces all three at run time; `''`, `null` or
  `undefined` restores the default. `ctx.title` and `info(id).title` return the current title.
  `seedTitle(options)` is exported for tests.

API: `pane(id)` (handle or null), `panes()`, `info(id)`, `whenMounted(id)`, `popout(id, mode)`,
`collapse(id, on)`, `reveal(id)`, `isTabMode()`, `destroy()`, `el`.

## 6. Writing a pane

```js
// web/js/panes/example.js
export const meta = {type: 'example', caps: [], needs: ['catalog']};

export function mount(el, ctx) {
  // ctx = {id, config, dataset, app, store, hub, layout, theme, gpuBudget, tools,
  //        setStatus, setTitle, title}
  const canvas = document.createElement('canvas');
  el.append(canvas);
  let visible = true;
  let client = null;
  ctx.setStatus('loading…');
  ctx.app.catalog(ctx.dataset).then((c) => { client = c; ctx.setStatus(''); draw(); });

  const offStore = ctx.store.on(['focus', `sel:${ctx.dataset}`, 'theme'], (changed, store, sources) => {
    if (sources.size === 1 && sources.has(ctx.id)) return;   // own echo
    if (visible) draw();
  });
  const offTheme = ctx.store.onEvent('theme:change', () => visible && draw());

  function draw() {
    if (!client) return;
    const t = ctx.theme();          // {plotBg, plotGrid, plotAxis, sel, neutral, cat[], rgb(name), ...}
    // ...
  }

  return {
    resize(w, h, dpr) { canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr); draw(); },
    onVisible(v) { visible = v; if (v) draw(); },
    destroy() { offStore(); offTheme(); },
    stats() { return {renderMs: 0}; },
    // optional: firstPaint: Promise   (settles at the first data paint; see below)
    // optional: brush(shape, op) -> Promise<count>  (used by __pk.brush)
    // optional: snapshot() -> Promise<Blob>
  };
}
```

Rules: show the skeleton or a status while data loads; stop work while hidden; read colours
from the theme tokens and repaint on `theme:change`; hit-test brushes through the same
transform used to draw; never call another pane directly. Add the type to
`core/registry.js` (`PANE_MODULES`, `PANE_CSS`, `PANE_TITLES`).

`firstPaint` (optional, IMPLEMENTATION_NOTES 112): a Promise the layout waits on before it
marks the pane `ready` (section 5). A pane that reads catalog data resolves it once its first
screen is drawn from data (or once it shows a note such as "choose a column" or "no catalog"),
rejects it when that load fails, and resolves it in `destroy()`. Use `firstPaintLatch()` from
`layout/layout.js`. Never make it wait on `app.afterFirstPaint()` or on reads gated by it
(whole columns, ids, pos): first paint waits on every visible pane, so that would hold the
page until the 30 s guard. Panes whose data arrives later by design (sky and wavelength
markers, spectrum and detail with no focus) leave it out and are `ready` at mount.

| pane | `firstPaint` resolves when |
|---|---|
| list | the first screen of rows is drawn (rows fetched for the window; in a sorted or filtered order, after its columns load) |
| table | every row of the first screen is drawn |
| gallery | the first page of cards is laid out (thumbnails load afterwards) |
| scatter | the first density grid or points draw (or no axes are set) |
| manifold | the first grid, canvas points or GPU frame draw |
| hist | the first histogram draws (or "choose a column" shows) |

Scatter context backdrop (note 122): for a context catalog (`context: true`, or the pane's
`background`) whose manifest has a count grid (`density[]` entry without `stat`) with `x`/`y`
equal to the pane's linear axes (`contextGridEntry(manifest, axes)`), the pane reads that grid
with `client.density(key)` when it loads its data and draws it as the gray backdrop
(`contextGridData(grid, shift)`, the `dm` fold applied to its ranges). It reads no context
column for it, so nothing extra is fetched before first paint. Catalogs without a matching
grid, or whose grid cannot be read, are binned with `bin2d` at settle time. The backdrop keeps
the decoded `values` array as its `counts`, so the canvas2d colourise cache (note 103) serves
every redraw. `stats().context = {prebinned, binned}` counts both kinds.

Animation frames (notes 123, 132): look up `requestAnimationFrame` when scheduling, not when the
module is imported (scatter's `raf`/`caf` remember which clock issued the handle). A module that
binds it at import falls back to its timer under node even after a test installs a stand-in.
`ui/controls.js` follows the same rule: its `raf` looks the function up at each call, with a
16 ms `setTimeout` fallback, so `rafSetter(store, source)` coalesces writes to one per frame of
whatever clock is installed at the time.

List deferred columns (note 112): after first paint the list reads hover columns and class
colours from the row records it already holds (`rowsCarry(manifest, key)` says whether
`rows.cols` includes a key; `levelIndex(meta)` maps a label to its level). `client.values` for a
whole column runs only on a sort, filter, search or colour-by change, or at idle:
`LIST_TIMING.idleColumnsMs` (4 s) after the last scroll, key or pointer activity and with no row
read in flight, through `requestIdleCallback` (timeout `LIST_TIMING.idleTimeoutMs`). A shown
column or colour column that the rows do not carry (for example an `exact: false` column, left out
of `rows.cols`) loads right after first paint, without the quiet period. Row-record category
values match levels through `payloadLevel(levels, v)`, which also tries the codec spellings
(`true` -> `'True'`, a float32 value -> `String(Math.fround(v))`); a label that still misses
loads the whole colour column after first paint. One `client.values(key)` read per key is in
flight at a time, shared by the shown-column and colour loaders.

Landing cards (note 121): the landing pane takes card counts from `portal.json counts[]`
(`countEntries(counts)` groups the valid entries by page; `countLabel(list)` gives the card
text and tooltip: `n == 1` takes `unit`, else `units`; entries with the same nouns are summed,
different nouns are listed with ` · `). Default nouns follow the kind (`COUNT_NOUNS`). With
`counts` present no manifest is fetched; without it the pane reads the `n` of each `search`
manifest, as before. `stats().counts` names the source used.

Pane tools go into `ctx.tools` (header, right side). `ctx.setStatus(text, title)` writes the
small status text in the header; `ctx.setTitle(text)` changes the pane title (section 5).
GPU panes take memory from `ctx.gpuBudget` (`request`/`release`/`plan`, `gl/budget.js`), the
page-wide budget shared by all panes; they release their share in `destroy()`.

## 7. Events

Store events are synchronous. Topics used by the core:

| Topic | Payload | Emitted by |
|---|---|---|
| `ready` | `{ms}` | boot |
| `toast` | `{level: 'info'|'warn'|'error', text}` | anyone; shown by `ui/toast.js` |
| `theme:change` | `{mode, effective}` | theme (store-key `theme` subscribers are also notified on OS changes in auto mode, value unchanged) |
| `pane:resize` | `{pane, w, h}` | layout |
| `hash:sel-dropped` | `{ds, length}` | hash |
| `caps:change` | `{cap, enabled, reason}` | `app.disableCap` |
| `brush:commit` | `{pane, ds, shape, op, count}` | brushing panes, test hooks |
| `narrative:highlight` | `{pane, ds, ids, beat, goto?, seq?}` | narrative pane (note 71): beat `beat` highlights source ids `ids` of `ds`; `pane` is the beat's target pane id (else the sticky pane type); an empty `ids` clears the highlight; `goto: true` asks the target to bring the rows into view (set when the beat has a highlight, no sky view, no `suppress_view`, and no explicit `sky=` deep-link view kept over the beat; each goto request carries a new `seq`, so a target skips a `narrative:sync` replay of a goto it already applied; tests: plotpanes.test.mjs, and the synthetic highlight-only beat `b6` in test_archetype_smoke.py). Scatter, manifold and sky panes listen |
| `narrative:sync` | `{pane}` | a scatter, manifold or sky pane at mount: asks the narrative pane to re-emit its current `narrative:highlight`, so a pane that mounts after the beat still shows it |

**Offer protocol.** Keyboard actions that a pane or widget may own are emitted with a payload
that carries `handled: false`. The first listener that acts sets `payload.handled = true`; when
nobody does, the core fallback runs.

| Topic | Payload | Fallback when unhandled |
|---|---|---|
| `nav:step` | `{dir: 1|-1, ds, event}` | step focus through stored order of `ds` |
| `keys:escape` | `{event}` | close lightbox, then pop-out, then clear hover |
| `help:toggle` | `{event}` | boot's built-in shortcut dialog |
| `help:open` | `{handled}` | boot's built-in shortcut dialog (header `?` button) |
| `tour:toggle` | `{event}` | none |
| `selection:invert` | `{ds, event}` | `sel.invert()` over all rows |

A list pane should handle `nav:step` (filtered, sorted order); a brushing pane should handle
`keys:escape` while a brush is in progress and `selection:invert` with its drawable mask;
`ui/help.js` should handle `help:toggle`/`help:open`; the manifold tour handles `tour:toggle`.

## 8. UI widgets

Boot imports `ui/toast.js`, `ui/nav.js`, `ui/help.js`, `ui/hover.js` and `ui/lightbox.js`
(`css/panes/ui.css` is already loaded, before `mountLayout`). Each module may export
`install(app)`; boot awaits it.
A module absent from the build logs a warning; a module that fails to load for another
reason, or whose `install` throws, logs `console.error` (so `__pk.errors` records it). The page
continues either way. A configured `legacyhash.js` that fails to load is also an error. Widgets place themselves
in `app.slots` (`nav` for the portal navigation, `tools` for header buttons, `selbar` for the
mobile selection bar). `ui/nav.js` calls `stashCarry(app.portalId, app.carryPayload())` before
following a link to a sibling page.

## 9. Theme (`core/theme.js`)

`get()`, `effective()`, `set('auto'|'light'|'dark')`, `toggle()`, `onChange(fn)`, `tokens()`,
`bindTheme(store)`, `makeToggleButton()`. The choice is stored in `localStorage['pk:theme']`
and mirrored to `<html data-theme>` and the store key `theme`. When the OS scheme changes in
`auto` mode, `theme` subscribers are notified with the value still `'auto'` (repaint through
`tokens()`, which then returns the new palette). `tokens()` reads the computed
CSS custom properties once per effective theme: `bg, bg2, bg3, fg, fg2, fg3, border, accent,
sel, hover, neutral, plotBg, plotGrid, plotAxis, densityCmap, cat[10]`, plus `rgb(name)`,
`selRGB` and `neutralRGB` as `[r, g, b]` in 0..1 for WebGL. Token names (section 12 of the
architecture) are frozen.

## 10. Formatting (`core/fmt.js`)

`stepDigits(step)` gives the number of decimals a quantized value supports
(`ceil(-log10(step))`, clamped to 0..12; 0.001 -> 3, 0.0153 -> 2, 0.5 -> 1). Quantized
displays use `fmtQuant(v, step)` -> `{text, title: '±step/2 (quantized, step s)'}`; exact values
from row chunks use `fmtNum`/`fmtValue(v, meta, {exact: true})`. `fmtStep`/`fmtQuant` switch
to exponent form with the significant digits the step supports when `|v| < 1e-4` and the step
needs more than 6 decimals (`fmtStep(3.2e-14, 1e-16)` -> `3.20e-14`). Also: `fmtPrintf`,
`fmtCount`, `fmtBytes`, `fmtMs`, `fmtRA`, `fmtDec`, `fmtAngle`, `escapeHtml`, `plural`.
Missing values print as an en dash.

## 11. Other core modules

- `carry.js`: `stashCarry(portal, {ds, focusId, sel})`, `takeCarry(portal)` (read once,
  60 s max age), `broadcastFocus(portal, id, ds)`, `onBroadcastFocus(portal, fn(id, ds))`.
- `loadscript.js`: `loadScript([cdn, mirror], {timeout, test})` tries each URL in order and
  rejects with `LoadScriptError` (`.attempts`); `loadCSS(href, {media})`.
- `registry.js`: `load(type)` loads the pane CSS, imports the module and checks `mount`;
  `register(type, mod)` injects modules for tests. `PANE_CSS` (note 69) loads
  `[<pane>.css, controls.css]` for scatter and manifold and `[controls.css]` for hist (its rules
  live in controls.css; there is no hist.css). Every listed file exists, so no stylesheet
  request 404s; UI widget styles live in `css/panes/ui.css`, which boot loads before
  `mountLayout` and `load(type)` also attaches (once, deduplicated by href) before the sheets
  of a widget pane (note 108).
- `keys.js`: shortcut table `KEYMAP`; shortcuts are ignored while typing in inputs (except
  Esc), and arrow keys are left to focused separators, sliders, list boxes, tab lists and grids.
  `k` (keep only) sets `filters:<ds>.ids` for up to 5000 selected rows.

### Data client (`data/client.js`) and worker (`workers/data.worker.js`)

One `DataClient` per catalog dataset (`hub.catalog(ds)`), backed by a module worker, or by the
same engine in-thread when module workers are unavailable.

- `workerUrl(manifestUrl, base?)` returns
  `<.../workers/data.worker.js>?m=<encodeURIComponent(manifestUrl)>`. The worker reads `m` and
  starts the manifest fetch from its top-level code; the `open` call reuses that in-flight
  promise (note 101).
- `rows(i0, i1, {signal, pos})` returns rows `[i0, i1)` from the exact row chunks, or, without
  row chunks, the quantized fallback built from the column chunks that hold those rows
  (`_quantized: true`, head chunks first, note 98). `pos` is one whole-catalog file, so on the
  fallback ra/dec are filled only when pos is already decoded or the read asks for it:
  `opts.pos` true. When `opts.pos` is absent the client's default applies, which is false until
  `allowPositions()` and true after it. A row read without positions carries
  `ra = dec = null` and `_posDeferred: true`.
- `allowPositions()` switches that default on; boot calls it after `app.afterFirstPaint()`.
  Reads that need coordinates before first paint pass `{pos: true}`: the sky highlight and the
  detail focus record do. The table re-reads, with `{pos: true}`, the blocks it read without
  positions (rows with `_posDeferred`, and blocks still in flight) once first paint has
  happened, on a quantized catalog with `pos`.
- `record(i, {channel, token, signal, pos})` (also `record(ds, i, opts)`) reads one row and
  loads only the chunk that holds row i (for a single-file column that chunk is the whole
  column), never the whole ids or pos (unless `pos` is asked for and the row lacks positions). On a catalog with row chunks the worker merges the
  quantized cells, read through `cellSource`, of the stored columns the row chunk omits (the
  `exact: false` columns, i.e. those missing from `manifest.rows.cols`) and lists their keys in
  `_qkeys`. Internal columns (keys starting with `_`, such as the single-file `_src_index` of a
  reordered catalog) are never merged. A row chunk keeps the positions it carries under the
  configured ra/dec names (e.g. `RAJ2000`). Only a row chunk whose keys, apart from the id (the
  first key), are all stored columns lacks positions (ra/dec declared `exact: false`); it gets
  ra/dec from pos under the same `pos` rule (listed in `_qkeys` when filled, `_posDeferred`
  otherwise). After `allowPositions()` the default `pos` is true, so such a record (a hover card
  included) loads pos when it is not yet decoded. Displays format a `_qkeys`
  column (or every column of a `_quantized` row) with `fmtQuant` (section 10). Records are
  kept in an LRU; a cached record with `_posDeferred` is read again when positions are wanted.
- Record channels (note 105): a new call on a channel aborts the previous unfinished call on
  that channel, which resolves to null. Panes use `<pane type>:<pane id>` (e.g. `detail:d1`);
  the app-level hover card uses `HOVER_CHANNEL` = `'hover:card'` (`ui/hover.js`).
- `resolveIds(ids, {aliases = true, signal})` -> `Int32Array` aligned with `ids` (-1 when
  missing), from the worker op `resolveIds({ids, aliases})`: one pass over every ids chunk
  (`IdIndex.rowsOf`), exact id first (first stored row wins), then the alias file. The result
  buffer is transferred.
- `id(i)` reads only the ids chunk that holds row i; `find`, `query` with id filters, `export`,
  `order('id')` and `resolveIds` load every ids chunk.

### Record sets (`data/records.js`)

`RecordStore.open(indexUrl, {fetch, signal, shardBytes, recordLRU, base, page, idOf})`; shard and
single-file URLs resolve against the index URL. `has(id)`, `hasRecord(id, {signal})` (async;
exact for `idOf` sets), `ids()`, `get(id, {signal, token, channel})`, `load(id)`.

Foreign record sets (shared contract (A)): a records DatasetRef may carry `base` (another
portal's `data/` prefix, relative or same-host https, ending in `/`) and `id_column`. The build
writes no shards for such a set; `url` is the foreign index `<base><key>/index.json`
(`recordIndexUrl(ref)` builds it from `base` and `key` when `url` is missing), so every shard
fetch lands under `base`. Boot wraps `hub.records(ds)` for every foreign ref (`isForeignRef`)
and opens the store with `base = foreignPageBase(indexUrl)` (`<index>/../../`, the foreign
portal's page directory), so PNG-mode sidecars resolve in the foreign portal; `{assets}` /
`{res}` are substituted with this page's values (absolute shared bases, or relative ones that
then resolve in the foreign portal). Without `id_column` the row id is the record key. With
`id_column` the store also gets `idOf(id)` = `app.foreignRecordKey(ds, id)`: the row of `ref.catalog` with that id
(`app.resolveIds` + `client.record`), then its `id_column` value (`foreignKeyOf(row, column)`:
text, integers without a decimal point, null when missing or NaN). Keys are cached (LRU of
4096). `has(id)` is true for an id whose key is not looked up yet, so callers that must tell
"no record" from "superseded" use `await hasRecord(id)` (spectrum1d does, and it also treats a
null `get()` on the current focus as "No spectrum"); `get(id)` resolves null when the row has
no key or the key is missing from the foreign index, and otherwise returns a shallow copy of the cached record with `id` = the
catalog row id and `key` = the foreign record key. Token / channel supersession applies after
the key lookup as well. A public portal may reference only public bases (checked by the Python
build).

## 12. Test hooks

With `?pktest=1`, `window.__pk` exists from the first line of boot:
`ready` (Promise), `errors` (window errors, unhandled rejections and `console.error`),
`caps()`, `tiers()`, `store`, `hub`, `app`, `select(ds, ids, op?)`, `brush(pane, shape, op?)`
(uses the pane's `brush` hook when present, else `client.brush` with the pane's axes),
`focus(ds, id)`, `stats()` (`{fetches, bytes, renderMs, decodeMs}`) and `firstPaintAt()`
(`Promise<number>`, epoch ms at which `app.afterFirstPaint()` resolved; available before boot
attaches the app; it rejects with the boot error when boot fails). The perf test (`tests/perf/test_perf_budget.py`) compares it with DevServer
`RequestRecord.t` (epoch ms when each response finished) to count the bytes before first
paint, and also reports the bytes up to a 1 s quiet settle.

## 13. Tests

```
node --test tests/js/store.test.mjs tests/js/selection.test.mjs tests/js/hash.test.mjs tests/js/fmt.test.mjs
```

The selection tests check the DEFLATE codec against `node:zlib` in both directions, the
shorter-of form choice (note 92) and that `serialize` gives the same string before and after
`Selection.parse` + replay. The hash tests drive `bindHash` with a fake window and manual
timers, including the `ovl=-` / absent-`ovl` round trips (note 72) and a reload of each `sel`
form through a real Selection. `store.test.mjs` also checks that every `PANE_CSS` file exists
(note 69), the `options.title` seeding (note 77) and `importSelection` against the real
`ui/export.js` (note 93). `chunks.test.mjs` drives the worker engine on in-memory bundles (head chunks, row chunks, the
quantized fallback, `_posDeferred`, `_qkeys` merging and which files each read fetches).
`listpanes.test.mjs` mounts the table, detail, sky and wavelength panes with fake clients
(deferred-position re-reads, `{pos: true}` reads, setup that does not wait for first paint),
the list's deferred columns and the `firstPaint` of list, table, gallery and hist;
`layout.test.mjs` checks the layout's pane state (`loading` until `firstPaint`, `error` on a
rejection, the guard) with a fake DOM;
`listpanes.test.mjs` also covers the landing counts (`countEntries`, `countLabel`, a mounted
landing pane with `setFetch` serving portal.json with and without `counts`) and the scatter
context backdrop on a mounted pane (a matching grid: no context column read, colourise cache
hits on redraw; no grid, a log axis or an unreadable grid: worker `bin2d`).
`plotpanes.test.mjs` covers chips/legend `default_off`, the Aladin retry, `contextGridEntry` /
`contextGridData`, the WPD2 sky helpers, and the `narrative:highlight` goto condition on a
mounted narrative pane (highlight-only beat, beat sky view, `suppress_view`, empty ids, a kept
deep-link view with an instant scroll, one activation per `go()` and per hash beat change), and
`controls.rafSetter` with a stand-in `requestAnimationFrame` installed after import and the
timer fallback. `wpe5.test.mjs` covers the wave E follow-ups (section 14). The full suite is
`node --test tests/js/*.test.mjs`.

## 14. Runtime contracts from IMPLEMENTATION_NOTES 68-132

Where each JS-facing note lives (details in the module headers):

| Note | Contract | Module |
|---|---|---|
| 68 | one `Budget` per page as `app.gpuBudget` / `ctx.gpuBudget`; panes never create one | `core/boot.js`, `gl/budget.js` |
| 69 | pane CSS map; UI widget styles in `css/panes/ui.css` | `core/registry.js` (section 11) |
| 70 | category `default_off` levels are applied by the filter owner once per page and dataset when no filter is set; chips only render the state | `data/hub.js`, `ui/chips.js` |
| 71 | `narrative:highlight`, `narrative:sync` | section 7 |
| 72 | `ovl=-` = no overlays, absent = layout default | `core/hash.js` (section 4) |
| 73 | `client.record(ds, i, {channel})` aborts only the previous unfinished call on the same channel | `data/client.js` |
| 74 | `client.order(key, dir)` -> `Int32Array` argsort computed in the worker; id sort is `key: 'id'`; list and gallery use it | `data/client.js` |
| 75 | A_V / R_V / template / feature controls have one implementation (`modelControls(ctx, opts)` and the helpers it composes) | `ui/controls.js` |
| 76 | `isCategory`, `levelColors` and GPU colour helpers are shared | `ui/legend.js` |
| 77 | `options.title`, `ctx.setTitle`, `ctx.title` | `layout/layout.js` (section 5) |
| 78 | raster layer options `x`, `y`, `cell` (bind a layer to an axis pair or grid cell); detail templates `photometry`, `compact` | `panes/scatter.js` (raster layers), `panes/detail.js` |
| 79 | spectrum2d writes `spec.xr` when `link1d` is on (two-way x link with spectrum1d) | `panes/spectrum2d.js` |
| 80 | lightbox providers may define `ready()`, `encode(pos)`, `decode(index)`; `lb=<set>,<k>` opens item k; a null set prefers the Gallery set | `ui/lightbox.js` |
| 81 | images.json and pki1 `png` sidecar URLs resolve against the page URL with `{res}`/`{assets}` substituted; detail product links resolve against the records index URL | `data/hub.js`, `panes/detail.js` |
| 82 | Aladin loads once per page; one viewer per sky pane | `panes/sky.js` |
| 84 | gallery focus without a catalog is `{ds: <images ds>, i, id}` | `panes/gallery.js` |
| 85 | landing search opens one worker per search manifest on the first keystroke | `panes/landing.js` |
| 86 | scatter folds `dm` into linear-magnitude axes and passes `dm = 0` to overlays; the manifold reads `cfg.overlays` | `panes/scatter.js`, `panes/manifold.js` |
| 90 | `mapLegacy(grammar, {hash, search, page})` | `core/boot.js`, `core/legacyhash.js` |
| 91 | `<manifold>.frame=<preset>` accepts `axes` presets as well as `frame` presets | `panes/manifold.js` |
| 92 | `sel=` form = shorter (in the URL) of the recipe form and `bits:`; brush recipes compete only above 200 000 rows | `core/selection.js` (section 3) |
| 93 | `app.importCsv(text, {mode, ds, radiusArcsec})` | `core/boot.js` (section 1), `ui/export.js` |
| 98 | `chunk_starts` head chunks; pos stays one file | `workers/data.worker.js` |
| 99 | no whole column, ids or pos before first paint; `rows`/`record` `opts.pos`, `allowPositions()`, `_posDeferred`; `record(i)` reads one chunk and merges `_qkeys` cells | section 11 (data client), `panes/table.js`, `panes/sky.js`, `ui/hover.js` |
| 100 | `app.afterFirstPaint()` | `core/boot.js` (section 1) |
| 101 | `workerUrl(manifestUrl)` (`?m=`) | `data/client.js`, `workers/data.worker.js` |
| 102 | `DataClient.resolveIds` and the worker `resolveIds` op; `app.resolveIds` uses them | section 1, section 11 |
| 104 | `toggleLevel(filters, key, level, {only, levels, defaultOff})` keeps `cats[key] = []` for a `default_off` column; legend and chips pass `defaultOff: hasDefaultOff(meta)` | `ui/legend.js`, `ui/chips.js` |
| 105 | record channels `<pane type>:<pane id>`; hover card `'hover:card'` | section 11 (data client) |
| 106 | `options.title` seeds every title; `ctx.setTitle` for record-following panes | `layout/layout.js`, `panes/detail.js` |
| 107 | `sel=` form: recipe competes with `bits:` only when view-independent; shorter wins, tie to recipe | `core/selection.js` (section 3) |
| 108 | widget CSS only in `css/panes/ui.css`, loaded by boot before `mountLayout` | `core/boot.js`, `core/registry.js` |
| 109 | a failed Aladin prefetch is retried once per page; sky and wavelength mount through `retryAladinOnce(app.page)` | `sky/aladin.js`, `panes/sky.js`, `panes/wavelength.js` |
| 112 | `data-state` waits on `handle.firstPaint` (guard `LAYOUT_TIMING`); list/table/gallery/scatter/manifold/hist expose it; the list loads whole columns only on sort/filter/colour-by or at idle; `__pk.firstPaintAt()`; DevServer `RequestRecord.t` | `layout/layout.js` (section 5), section 6, `core/testhooks.js`, `deploy/devserver.py` |
| 119 | `sky.beatView` / `sky.beatOvl` markers; a beat-applied view or overlay set stays out of the hash, user changes write `sky=`/`ovl=`, `#sky=..&beat=` keeps the hash view | `core/hash.js` (section 4), `panes/sky.js`, `panes/narrative.js` |
| 120 | static skeletons; the list asks for rows at mount; `warmPaneModules`, `shownAtLoad`, `tabModeQuery` | `core/boot.js` (section 1), `layout/layout.js` (section 5), `panes/list.js` |
| 121 | landing card counts from `portal.json counts[]`, `search` manifests as the fallback | `panes/landing.js` (section 6) |
| 122 | scatter context backdrop from the context catalog's matching count grid, worker `bin2d` otherwise | `panes/scatter.js` (section 6) |
| 123 | `requestAnimationFrame` looked up at call time | `panes/scatter.js` (section 6) |
| 130 | narrative `activate` applies the beat before scrolling; an instant-scroll re-entry is a no-op | `panes/narrative.js` (section 4) |
| 131 | an images preset with a facet that matches no item is dropped at build (`bundle/images.py`); an empty value list reads as "no filter" | `panes/gallery.js` |
| 132 | `ui/controls.js` `raf` looked up at call time | `ui/controls.js` (section 6) |

Wave E follow-ups (tests in `tests/js/wpe5.test.mjs`):

| Topic | Contract | Module |
|---|---|---|
| default frame | a manifold pane without a stored frame or `options.frame` opens on `manifest.manifold.default_frame` when it names a known preset (`initialPreset`), else `'pca'`; boot's page default follows (`frameDefault`, `unbindHash.setDefaults`); autoplay treats a null preset as unset | `panes/manifold.js`, `core/boot.js` |
| speed | canonical `speed` (store and hash); legacy `?speed=` / `speed=` map onto it; the manifold Tour uses `tourSpeed(store speed, options)` and follows store changes | `core/hash.js`, `core/legacyhash.js`, `panes/manifold.js` |
| dim alias, spherex-colours | `page.legacy.dim_alias`; the `spherex-colours` grammar (section 4) | `core/legacyhash.js` |
| recipe presets | `applyPresetSelection(env, preset, op)`: every preset form (ids, bits, recipe) combines with shift (add) / alt (subtract); recipe rows come from `app.selectionHit` | `ui/chips.js` |
| spectrum readout | `readoutSamples(series, wx)` searches each shown segment (binary search when that segment is sorted, linear scan when not) and reads out one value per segment covering wx, else the nearest sample | `panes/spectrum1d.js` |
| fluxscale | `normalizeRecord` multiplies every non-wavelength channel by a finite positive `meta.fluxscale` (other than 1) once and records `meta.fluxscaleApplied` | `panes/spectrum1d.js` |
| context statistics | the binned-statistic select lists the context catalogs' grids for the axes as `<ds>::<key>` (`statGridOptions`) | `panes/scatter.js` |
| layer switches | `layers:<pane>` `{raster?: false, ovl?: [ids]}`: a toolbar button (shown when the pane has raster layers) toggles the rasters; `ovl` limits the drawn overlays (`visibleOverlays`); hash `<pane>.raster`, `<pane>.ovl` | `panes/scatter.js`, `core/hash.js` |
| gallery preset role | a preset's `role` (or `data.role`) is applied with its facets when the pane offers that role; the preset select matches facets and role (`matchPreset`) | `panes/gallery.js` |
| foreign records | `base` / `id_column` record refs (section 11) | `data/records.js`, `core/boot.js` |
