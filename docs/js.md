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
web/css/{tokens,base,layout,print}.css
```

## 1. Boot

`shell.html` imports `core/boot.js` and calls `boot()`. The order is fixed:

1. Parse `<script id="pk-page">` (page.json). The shell's inline script has already applied the
   stored theme.
2. Build the store: defaults <- `page.state` <- carry (`takeCarry`) <- legacy hash mapping <-
   canonical hash. The legacy step runs only when `page.page.legacy.grammar` is non-empty; it
   imports `./legacyhash.js` dynamically and calls `mapLegacy(grammars, {hash, search})`.
   Consumed query keys are removed from the URL with `replaceState`, and the canonical hash is
   written once boot finishes.
3. `createHub(page, store)` from `data/hub.js`; manifests of every catalog used by a
   non-collapsed pane (and pane `background` catalogs) are fetched at once.
4. Runtime probe: WebGL2, `EXT_color_buffer_float`, `DecompressionStream`,
   `navigator.deviceMemory`. Each tiered pane gets `tier:<pane>` once its catalog's N is known.
5. Header chrome and `mountLayout` (pane modules load lazily through the registry).
6. `<body data-caps="...">` (enabled caps, sorted, space separated) and
   `<body data-tier="pane:T1 ...">`.
7. `bindHash`, `bindKeys`, `bindTheme`, test hooks.
8. `store.emit('ready', {ms})`.

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
| `resolveIds(ds, ids)` | `Promise<number[]>` stored row indices, -1 when not found. A row matches when its id equals the text (candidates from `client.find`, whose substring hits never count) or the text is one of its aliases (exact lookup in the manifest's `ids.alias_file`, loaded on first need) |
| `idOf(ds, i)` | `Promise<string>` cached id lookup |
| `applySelection(ds, str)` | apply an `ids:`/`bits:`/`r:` string as one replace (recipes replay on an empty scratch Selection through `client.brush`, `client.query` and presets, then load with `sel.replaceWith`) |
| `carryPayload()` | `{ds, focusId, sel}` for `stashCarry` |
| `disableCap(cap, reason)` | runtime removal (e.g. WebGL context lost for good); updates `data-caps` and emits `caps:change` |
| `hashLink()` | the canonical hash string for "copy link" actions (no side effects) |
| `hashLinkInfo()` | `{str, dropped, omitted}`: `dropped` = the selection is too long for the link, `omitted` = the link leaves a non-empty selection out (copy-link actions warn on it) |
| `slots` | `{header, nav, tools, main, selbar}` DOM slots for UI widgets |

Selections: boot uses `client.selection()` when it returns an object with `apply`, `mask` and
`onChange` (a `core/selection.js` Selection); otherwise it creates `new Selection(client.n)`.
Selection changes are mirrored into `sel:<ds>` = `{version, count}` with source `'selection'`.

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
`template = null`, `features = []` (or the first spectrum1d `options.features`),
`spec = {pinned: [], variant: 'flux'}`, `sky = null`, `rung/beat/role = null`, `facets = {}`,
`lightbox = null`, `layout = {popout: {}}`, per catalog `filters:<ds> = {}`,
`vis:<ds> = {version: 0, count: null}` (count null until a filter is applied), and per
non-context catalog `sel:<ds> = {version: 0, count: 0}`, `annot:<ds> = {version: 0}`. Scatter
panes get `axes:<pane>` from their `x/y/xlog/ylog` options; manifold panes get
`frame:<pane> = {preset: options.frame ?? 'pca', dims}`. `page.state` is merged on top, one
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
| `''` | empty, or n > 200 000 with an incomplete recipe | |
| `ids:<id>,<id>` | count <= 300 and `idOf` given | `encodeURIComponent` per id |
| `bits:<b64url>` | n <= 200 000 | base64url(deflate-raw(packed bits, LSB first)) |
| `r:<b64url>` | otherwise | base64url(deflate-raw(UTF-8 JSON RecipeOp[])); `r:[...]` raw JSON is also accepted |

`deflateRaw`/`inflateRaw` are synchronous (fixed-Huffman LZ77 encoder; full stored/fixed/dynamic
decoder) so serialization stays synchronous. `b64urlDecode` also accepts standard base64.

RecipeOp: `{op, kind: 'rect'|'circle'|'lasso'|'ids'|'filter'|'preset', view: {ds, x?, y?, xlog?, ylog?, pane?}, shape?, ids?, filters?, key?}`.
Shapes are in data coordinates. Projection (manifold) brushes are replayable only through
`ids`; the manifold pane should record `{kind: 'ids', ids}` for them when the count allows.

## 4. Hash grammar (`core/hash.js`)

Keys appear in this order; pane-scoped keys sit after `f`, grouped by pane, and unknown keys
come last in alphabetical order:
`id, sel, f, <pane>.x, <pane>.y, <pane>.frame, c, av, rv, dm, tpl, feat, spec, wl, sky, base, ovl, rung, beat, role, fac, lb, view`.

| Key | Store | Format |
|---|---|---|
| `id` | `focus` | source id; `focus.ds` = primary dataset |
| `sel` | `sel:<primary>` | section 3 |
| `f` | `filters:<primary>` | base64url(UTF-8 JSON); raw JSON `{...}` accepted |
| `<pane>.x`, `<pane>.y` | `axes:<pane>` | expression text |
| `<pane>.frame` | `frame:<pane>` | preset name, or `b64:` + base64url(float32 LE `F`) |
| `c` | `color` | `by~cmap~lo~hi` (trailing parts optional; `none` = no colouring) |
| `av`, `rv`, `dm` | same | numbers: 1e-6 absolute precision for magnitudes >= 1e-3, 7 significant digits below (all numeric values in the hash use this rule) |
| `tpl` | `template` | `key~grid~scale~band,band` |
| `feat` | `features` | comma list |
| `spec` | `spec` minus `xr` | base64url JSON |
| `wl` | `spec.xr` | `w0,w1` |
| `sky`, `base`, `ovl` | `sky` | `ra,dec,fov`; base survey id; `id:opacity,id` |
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
`.replaceNow()`, `.compute()` (string, no side effects), `.computeInfo()` and `.selDropped()`.
WP13's `legacyhash.js` maps legacy forms to this grammar (`mapLegacy` returns canonical
key/value strings). When the mapping consumed the hash itself, boot uses only the mapped
values; when it consumed only query parameters, the mapped values are merged over the
canonical hash.

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
- Panes mount lazily the first time they become visible. A `ResizeObserver` calls
  `handle.resize(w, h, dpr)` once per frame and emits `pane:resize {pane, w, h}`.

API: `pane(id)` (handle or null), `panes()`, `info(id)`, `whenMounted(id)`, `popout(id, mode)`,
`collapse(id, on)`, `reveal(id)`, `isTabMode()`, `destroy()`, `el`.

## 6. Writing a pane

```js
// web/js/panes/example.js
export const meta = {type: 'example', caps: [], needs: ['catalog']};

export function mount(el, ctx) {
  // ctx = {id, config, dataset, app, store, hub, layout, theme, tools, setStatus, setTitle}
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
    // optional: brush(shape, op) -> Promise<count>  (used by __pk.brush)
    // optional: snapshot() -> Promise<Blob>
  };
}
```

Rules: show the skeleton or a status while data loads; stop work while hidden; read colours
from the theme tokens and repaint on `theme:change`; hit-test brushes through the same
transform used to draw; never call another pane directly. Add the type to
`core/registry.js` (`PANE_MODULES`, `PANE_CSS`, `PANE_TITLES`).

Pane tools go into `ctx.tools` (header, right side). `ctx.setStatus(text, title)` writes the
small status text in the header.

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
(after loading `css/panes/ui.css`). Each module may export `install(app)`; boot awaits it.
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
  `register(type, mod)` injects modules for tests.
- `keys.js`: shortcut table `KEYMAP`; shortcuts are ignored while typing in inputs (except
  Esc), and arrow keys are left to focused separators, sliders, list boxes, tab lists and grids.
  `k` (keep only) sets `filters:<ds>.ids` for up to 5000 selected rows.

## 12. Test hooks

With `?pktest=1`, `window.__pk` exists from the first line of boot:
`ready` (Promise), `errors` (window errors, unhandled rejections and `console.error`),
`caps()`, `tiers()`, `store`, `hub`, `app`, `select(ds, ids, op?)`, `brush(pane, shape, op?)`
(uses the pane's `brush` hook when present, else `client.brush` with the pane's axes),
`focus(ds, id)` and `stats()` (`{fetches, bytes, renderMs, decodeMs}`).

## 13. Tests

```
node --test tests/js/store.test.mjs tests/js/selection.test.mjs tests/js/hash.test.mjs tests/js/fmt.test.mjs
```

The selection tests check the DEFLATE codec against `node:zlib` in both directions. The hash
tests drive `bindHash` with a fake window and manual timers.
