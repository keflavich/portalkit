# portalkit data bundle: writers and codecs

This document describes `portalkit/bundle/` (WP01). It follows ARCHITECTURE.md section 5 and
docs/IMPLEMENTATION_NOTES.md, and records the choices the contract leaves open. The JS readers
(`web/js/data/codec.js`, `records.js`) decode what is described here; the golden files in
`tests/golden/codec/` pin the bytes.

## 1. Python API

```python
from portalkit.bundle import catalog, records, images, BundleWriter, Ledger

w   = BundleWriter(stage_root, "data/src", ledger)          # staging guard + ledger
cat = catalog.write(spec: CatalogSpec, w, *, build=None, seed=20261010, links=None,
                    provenance=None, ledger=None, log=None, step_mas=8.0) -> CatalogResult
rec = records.write(spec: RecordSetSpec, BundleWriter(stage_root, "data/spec", ledger),
                    *, order=cat.row_of,
                    tier="C", ledger=None, log=None) -> RecordsResult
img = images.write(spec: ImageSetSpec, dest, *, rows=cat.row_of, thumb=None,
                   ledger=None, log=None, page_max=IMAGES_SOFT_MAX) -> ImagesResult
```

* `dest` is a `BundleWriter` whose base is the dataset directory, or a plain directory (which
  then also acts as the staging root). Paths returned and written into JSON are relative to
  the dataset directory.
* `CatalogResult`: `manifest`, `order` (`RowOrder`), `ids` (stored order), `id_to_row`,
  `alias_to_row`, `files`, `warnings`, `writer`; `row_of(id_or_alias)` returns the stored row
  or `None`.
* `RecordsResult`: `index`, `files`, `skipped` (ids whose loader returned `None`), `warnings`.
* `ImagesResult`: `doc`, `pages`, `unmatched` (item ids without a catalog row), `warnings`.
* `records.write(order=...)` takes a mapping or callable id -> stored row; members are packed
  in that order (ids without a row go last, in `spec.ids` order).
* `images.write(thumb=...)`: `thumb(url, item_id, role) -> str | None` returns the thumbnail
  URL stored as the role's `thumb`.
* Specs are duck-typed against `portalkit.spec` (section 9.1). `CatalogSpec.exact_gpu`
  and `ColumnSpec.exact_gpu` (optional attributes, default `True`) select `f32` for `value`
  columns with dynamic range above 1e4.
* Errors: `BundleError` (a `ValueError`) for bad input (duplicate or missing ids, bad column
  keys, unknown encodings); `StagingError` for a write outside the staging root or through a
  symlink.
* `catalog.head_chunk_starts(n)` returns the manifest `chunk_starts` for `n` rows (or `None`);
  `catalog.first_paint_files(manifest, context=False)` lists the data files counted as first
  paint (section 8); `context=True` applies the context-catalog rule (manifest plus density
  grids only).
* Python readers (`portalkit.bundle.codec`, mirroring `web/js/workers/data.worker.js`):
  `catalog_chunk_starts(man)` (`chunk_starts`, else `[0, chunk_rows, ...]`; `ValueError` on a
  malformed list), `chunk_ranges(man, nfiles)` (`[first, end)` per chunk file; one file covers
  `[0, n)`), `column_files(meta)`, `read_column(man, key_or_meta, read, chunks=None)` and
  `read_ids(man, read, chunks=None)`, where `read(rel_path) -> bytes`. Tools and tests read
  catalog columns through these, never by assuming `file`.

## 2. Files and naming (section 5.1)

* Every written file is `<stem>.<h8>.<ext>`, `h8` = first 8 hex digits of sha256 of the bytes
  on disk. Record layout files are `r/<h8>.bin`. Stable names: `manifest.json`, `index.json`,
  `images.json` (and the other section 5.1 documents written by other WPs).
* Writes are atomic (temporary file in the same directory, then `os.replace`). Identical
  content already on disk is not rewritten. `fsync` is off by default (network filesystem).
* `.bin` files are gzip inside: `gz()` is the GC function verbatim (level 9, mtime 0, OS byte
  `0xff`). A byte-shuffled payload uses `gz(filtered=True)` (Z_FILTERED, wbits 31, OS byte
  `0x03`). Readers only need a gzip inflater.
* JSON: UTF-8, `ensure_ascii=False`, `allow_nan=False`, compact separators, NaN/inf -> `null`.
* Every byte written is recorded in the `Ledger` (path, bytes, kind, dataset, first_paint).
  `Ledger.check()` returns hard errors (manifest > 256 KiB, column/chunk/ids/pos/limits file >
  16 MiB, shard > 512 KiB) and soft warnings (row chunk > 512 KiB, record file > 2 MiB,
  images.json > 4 MiB).

## 3. Column encodings (section 5.3)

All arrays little-endian. `shuffle: true` means a byte-plane shuffle (plane 0 = low bytes) of
the whole file payload.

| enc | payload | missing | decode | shuffle |
|---|---|---|---|---|
| `u16q` | u16 | 0 | `lo + (code-1)*step` | 2-plane |
| `u16log` | u16 | 0 | `10**(lo + (code-1)*step)` (lo/hi/step in dex) | 2-plane |
| `u8q` | u8 | 0 | `lo + (code-1)*step`, step = (hi-lo)/254 | none |
| `u8log` | u8 | 0 | `10**(log lo + (code-1)/254*(log hi - log lo))` (lo/hi linear, GC `log_quantize`) | none |
| `cat8` | u8 index into `levels` | 255 | `levels[code]` | none |
| `cat16` | u16 index | 65535 | `levels[code]` | 2-plane |
| `bits` | 1 bit/row, LSB first | not representable (0) | `(b[i>>3] >> (i&7)) & 1` | none |
| `f32` | float32 | NaN | value | 4-plane |
| `u32` | uint32 | 0xFFFFFFFF | value | 4-plane |
| `u32lin` | uint32 | 0xFFFFFFFF | `lo + code*(hi-lo)/4294967294` | 4-plane |
| `str` | `u32 n, u32 off[n+1], UTF-8 blob` | `""` | `blob[off[i]:off[i+1]]` | none |
| `sparse` | `u32 m, u32 idx[m], u8 kind[m]` | - | idx ascending, unique | none |

Quantizer rules:

* `u16q`, free step: `step = (hi-lo)/65534`, `code = 1 + rint((x-lo)/step)`.
* Fixed step (mag, colour, or `ColumnSpec.step`): `lo` is rounded down to a multiple of
  `step` (12 significant digits), `hi = lo + 65534*step`. When the data span does not fit,
  the step is widened to the smallest integer multiple that fits, and a warning is recorded.
* Degenerate span (one distinct value): `hi = lo`, `step = 1e-6*max(1, |lo|)`; every valid row
  has code 1.
* Writers always emit `step` (IMPLEMENTATION_NOTES 9).
* `clip`:
  * `none` uses the data min/max. Adapter `lo`/`hi` can only widen the range.
  * `saturate` clamps to `lo`/`hi`. Without adapter bounds it uses the 0.1/99.9 percentiles,
    or `log_dim_stats` for log columns. Clamped rows get sparse kinds 3 (low) / 4 (high).
  * `drop` stores out-of-range values as missing.
* `u16log`: `x <= 0` is missing, except rows that carry a legacy limit code; those get code 1
  (the floor). The adapter's `lo`/`hi` for `u16log` are linear values (converted to dex in
  the manifest).
* `u8log` meta `lo`/`hi` are linear (GC); `step` is emitted in dex.
* `u32` and `u32lin` store out-of-range or non-finite values as missing, with a warning.
* Categories: levels default to the sorted distinct labels. Given levels keep their order
  and unknown labels are appended, with a warning. `None`, NaN, masked and `""` are missing.
  `cat16` is used above 255 levels. `colors`/`default_off` entries naming levels absent from
  the data are dropped, with a warning.
* `bits`: truthy strings are `1/true/t/yes/y` (case-insensitive); NaN/missing -> 0.

Encoding chosen for `enc: "auto"`:

| role | enc |
|---|---|
| `mag`, `color` | `u16q`, fixed `step = 0.001` (`MAG_STEP`); `mag` gets `invert: true` |
| `flux` | `u16log` |
| `error` | `u8log` |
| `value`, `ra`, `dec` (when not positions) | `u16q`; `f32` when `exact_gpu` is false and max/min of non-zero \|x\| > 1e4 |
| `category` | `cat8` (`cat16` above 255 levels) |
| `flag` | `bits` |
| `id` (other than the catalog id) | `str` |
| `text`, `url`, `image` | row chunks only; a `str` column when no row chunks are written |
| `limit` | not stored as a column; feeds the referring column's sparse limits |

Column statistics (`center`, `scale`, `nvalid`, `quantiles`[101]) are computed on the valid
(non-missing after clipping) values, on log10 for `log: true` columns. An all-missing column
gets `center 0, scale 1, nvalid 0` and no `quantiles`.

Limits: a column with `ColumnSpec.limit` (sibling column with codes `u`/`<`/`upper` -> 1,
`l`/`>`/`lower` -> 2) or with clipped rows gets `limits: {file: "limits/<key>.<h8>.bin",
count}`. Legacy codes take precedence over clip kinds for the same row.

## 4. Positions

* `N <= 131072`: `pos: {enc: "u32lin", files: {ra, dec}}` with the manifest `bbox` as lo/hi
  (IMPLEMENTATION_NOTES 5). Each file is a 4-plane shuffled `u32lin` array (the `pos` block has
  no `shuffle` key; the shuffle is implied by the encoding, as for `u32`). A row with a
  non-finite ra or dec gets 0xFFFFFFFF in both files, as `i16off` marks the whole position
  missing.
* `N > 131072` (or `ColumnSpec("ra", enc="i16off")`): `pos: {enc: "i16off", step_mas,
  tile_refs: [[ra0, dec0], ...], files: {tile, dra, ddec}}`.
  * `tile` is u16 and `dra`/`ddec` are i16; each file is 2-plane shuffled.
  * Decode: `ra = ra0 + dra*step_mas/3.6e6/cos(dec0)` and `dec = dec0 + ddec*step_mas/3.6e6`.
    `dra = ddec = -32768` marks a missing position.
  * Tiles are dec bands of `T = 0.125*step_mas/8` deg. Each band is split into
    `max(1, ceil(360 cos(dec0)/T))` RA cells; the tile reference is the cell centre rounded to
    1e-6 deg.
  * RA offsets wrap at 0/360. Only occupied tiles are listed.
  * When more than 4096 occupied tiles (`codec.I16OFF_MAX_TILES`) are needed, the step and the
    tile size are doubled until they fit, so the inlined `tile_refs` (about 25 B each) stay
    well inside the 256 KiB manifest budget. The writer records a build warning naming the
    raised `step_mas`; an all-sky catalog ends near 4-deg tiles and 256 mas steps. The step
    is never finer than `T*3.6e6/60000` mas, which keeps offsets within ±30000.
  * The tile scheme is portalkit's own; the GC builder used per-pointing tiles. Readers depend
    only on `tile_refs` and `step_mas`.
* Position files are not chunked (`pos.files` values are single paths in the schema), also
  when the columns carry `chunk_starts`.

## 5. Row order (section 5.4)

* `N <= 131072`: input order (`order: {kind: "input"}`).
* Otherwise (or `CatalogSpec.order = "lod"`):
  * `perm = default_rng(seed).permutation(N)`. Levels are the cumulative sizes
    `32768*2^k < N`, followed by `N`.
  * Inside each level, rows are sorted by `(tile << 32) | morton16(dra+32768, ddec+32768)`
    (`order.sort = "tile-morton"`); rows without a position sort last.
  * The manifest stores `order: {kind: "lod", seed, levels, sort}`. The input index is
    written as the `u32` column `_src_index` (shuffled, role `value`).
* Every stored array (columns, ids, positions, limits, row chunks) is in stored order.

## 6. Chunking, ids, aliases, rows (section 5.5)

* Head chunk (IMPLEMENTATION_NOTES 98). When `N > LOD_MIN_N` (131072) the manifest carries
  `chunk_starts`, the first row of each chunk:
  `[0, LOD_BASE, LOD_BASE + CHUNK_ROWS, LOD_BASE + 2*CHUNK_ROWS, ...]` (strictly increasing,
  every value < N; e.g. `[0, 32768]` for 1M rows and `[0, 32768, 1081344, 2129920, 3178496,
  4227072]` for 5M). Every column is then written as `chunk_files` and the ids as `ids.chunks`,
  with one file per entry of `chunk_starts`. Chunk 0 holds exactly the LOD base sample (rows
  `[0, LOD_BASE)`), so a list's first screen, a hover card or a prefix load reads about 60 kB
  per u16 column and the head ids chunk instead of whole files. Chunk `k` holds rows
  `[chunk_starts[k], chunk_starts[k+1])` (the last ends at N); no chunk exceeds `CHUNK_ROWS`
  rows.
* Without `chunk_starts` (N <= 131072, and foreign GC manifests) the old rule holds: one file
  per column when `N <= chunk_rows` (1048576), else `ceil(N / chunk_rows)` chunks of
  `chunk_rows` rows.
* Chunk files are named `cols/<key>.cNNN.<h8>.bin`. Each chunk is encoded independently with
  the column's shared lo/hi/step and levels; a `bits` chunk restarts at bit 0 of its first
  byte (chunk boundaries need not be multiples of 8); a `str` chunk is a complete `str`
  payload. The column kind in the ledger is `chunk`.
* `ids`:
  * `{file: "ids.<h8>.bin", enc: "str", chunks: null}`. When the columns are chunked, `file` is
    null and `chunks` lists `ids.cNNN.<h8>.bin`, split exactly like the columns.
  * Missing or duplicate ids raise `BundleError`. Numeric ids are written as decimal strings.
* Aliases (`CatalogSpec.aliases`: a dict `id -> [alias]` or a column holding `|`-joined
  aliases) are written to `aliases.<h8>.json` as `{alias: stored_row}`.
  * An alias equal to another id is dropped, with a warning.
  * An alias claimed by two different rows is dropped, with a warning.
  * An alias of an unknown id is dropped, with a warning.
* Row chunks:
  * Written when `N <= 1,000,000` or `exact_rows=True`, and only when `N > 0`. Files are
    `rows/rNNNN.<h8>.bin`; each is a gz JSON array of up to 4096 row objects.
  * The keys are the source column names: the id, ra, dec, then every column with
    `exact=True`, including `limit` and `text` columns.
  * Values are full precision. float32 values are written as their shortest repr. Masked
    values and NaN are written as `null`.

## 7. Density (section 5.9)

* `density: [{key, x, y, xr, yr, nx, ny, enc, lo, hi, file, total?}]`.
  * The grid is `nx*ny` values, row-major with x fastest: `cell = iy*nx + ix`, with `iy = 0` at
    `yr[0]`.
  * Bins are `linspace(range, n+1)`, with the last bin closed on the right. Rows outside the
    range or with missing x/y are not counted.
* Count grids use `enc: "u16log"` with the section 5.3 column semantics: `lo`/`hi` are log10
  of the smallest non-zero and the largest count, and `step = (hi-lo)/65534` is always written.
  * Code 0 is an empty cell; otherwise `code = 1 + rint((log10(c) - lo)/step)`.
  * Decode with `10**(lo + (code-1)*step)`, the same rule as a `u16log` column, so one decoder
    handles columns and grids.
  * The grid is 2-plane shuffled and written with filtered gzip; `total` is the count of
    binned rows.
  * `u16q` count grids are also supported (linear `lo`/`hi`/`step`).
* Binned statistics use `DensitySpec.stat = "<fn>:<expr>"` or `"<fn>(<expr>)"`.
  * `fn` is one of count, mean, median, min, max, std, sum.
  * The entry has `enc: "f32"`, `stat: fn`, `value: expr` and `shuffle: true`. `lo`/`hi` are
    the finite min/max; an empty cell is NaN (IMPLEMENTATION_NOTES 4).
* `x`, `y` and stat value expressions are linear (section 4.4 grammar plus a leading `-`).
  Ranges default to the 0.5/99.5 percentiles, padded by 5 %.
* Automatic grids: when `N > T0_MAX` (3000) or `CatalogSpec.context`, every `axes` preset with
  `x`/`y` gets a 256x256 count grid keyed by the preset key, unless a `DensitySpec` already
  uses that key. A density spec that cannot be evaluated (unknown column) is skipped, with a
  warning.

## 8. Manifest assembly

The key order is: `schema, id, title?, n, build?, order, chunk_rows, chunk_starts?, ids, pos?, bbox?, cols,
virtual?, groups?, presets?, density?, rows?, manifold?, links?, provenance?, caveats?,
budget`.

* Presets: `{key, label, kind, **data}`. For filter presets, `data` should carry `filters`
  (IMPLEMENTATION_NOTES 7).
* `manifold`: `{dims, default_frame, fade_width: 0.12, frames?}`.
* `provenance.catalogs` comes from `CatalogSpec.catalogs_provenance` unless the caller passes
  its own.
* `budget: {first_paint_bytes, total_bytes}`, iterated until the manifest's own size is
  stable.
  * `total_bytes` is the sum of every file written for the dataset plus `manifest.json`.
  * `first_paint_bytes` counts what first paint reads (IMPLEMENTATION_NOTES 98-99):
    `manifest.json`; the plot's first count density grid (the first grid when every grid is
    a binned statistic), or without a grid every file (all chunks) of the two axis columns of
    the first `axes` preset (or the first two numeric columns), because the scatter reads its
    axes with an unprefixed `client.values()`; and the list's first screen, read with
    `client.rows(first, last)`: `rows/r0000` when the catalog has row chunks, else the head
    chunk of every stored column (the worker's quantized `rows()` decodes all of them) and
    the head ids chunk. Positions are not counted (sky markers load them after first paint).
    The head chunk is chunk 0: the LOD base sample with `chunk_starts`, else the whole file
    (or the first `chunk_rows` rows).
  * Context catalogs (`CatalogSpec.context = True`, drawn only as a density backdrop and
    never listed; IMPLEMENTATION_NOTES 114) follow their own rule. `first_paint_bytes` counts
    `manifest.json` plus every density grid of the catalog (the backdrop of each axes
    preset), or the two axis columns (all chunks, as above) when the catalog has no grid. No
    row chunk and no head chunk are counted, because the list never shows a context catalog.
    `catalog.write` passes `context=spec.context`.
  * `catalog.first_paint_files(manifest, context=False)` returns these files (paths relative
    to the dataset directory, the manifest itself excluded; the caller adds its size). With
    `context=True` it returns the context set above; otherwise the plot file(s) followed by
    the list's first screen. Nothing is listed for the list when `n == 0`.
  * The same files carry `first_paint: true` in the ledger, so `Ledger.first_paint(ds)`
    equals `first_paint_bytes`. `build.py` sums it over a page's catalogs.
  * The estimate leaves out the context axis columns that the scatter pane currently bins in
    the worker for its backdrop (IMPLEMENTATION_NOTES 114); it counts only the grids.

## 9. Record sets (sections 5.6-5.8)

`index.json` (`pk-records/1`):

```json
{"schema":"pk-records/1","kind":"pks1|pki1|json|sedcurve","layout":"shard|file",
 "catalog":"src","key":"id","n":N,"shards":["s000.<h8>.bin",...],
 "entries":{"<id>":[shard,off,len] | "r/<h8>.bin" | {"png":url,"extent":{"w":[..],"y":[..]}}},
 "grids":{"g":{"kind":"uniform","w0","dw","n"} | {"kind":"array","values":[..]}},
 "variants":["flux",...],"unit_w":"um","unit_f":"Jy"}
```

* Each record is one gzip member. Shards are concatenations of members, appended in catalog
  stored order.
  * A new shard starts when adding a member would pass `SHARD_TARGET` (256 KiB).
  * A single member up to `SHARD_MAX` (512 KiB) may start its own shard.
  * A larger member is written as `r/<h8>.bin`; its entry is that path, and a warning is
    recorded (above 2 MiB as well).
  * `layout: "file"` writes every record as `r/<h8>.bin`.
* `variants` lists the non-error channel names (channels whose name is not `err*`/`e_*`).
  `unit_f` is the first record's flux unit.
* `json-legacy` (`RecordSetSpec.legacy_json = (dir, pattern)`) writes `{kind:"json-legacy",
  dir, pattern, key, n, catalog?}` without `schema`. It is accepted only when `tier` is "A" or
  "B"; any other tier, including `None`, raises `BundleError`.
* `pki1` PNG mode: `RecordSetSpec.png(id) -> (url, extent) | None`; when the hook returns a
  value, the entry is `{png, extent}` and no record is written.

### pks1 (spectra; `sedcurve` = pks1 with `u16log` channels by default)

```
"PKS1" | u32 header_len (multiple of 8, so also of 4; JSON padded with spaces) | header JSON | binary
```

* The binary section starts at an 8-byte aligned offset of the inflated record. Every binary
  array starts at a 4-byte aligned offset (`off`) relative to the start of the binary section;
  `f64` arrays start at an 8-byte aligned one, so `new Float64Array(buf, 8+header_len+off, n)`
  works without a copy (when the record buffer itself starts at offset 0).
* Header fields: `{id, n, wave, unit_w, channels:[{name, unit, enc, off, lo?, hi?, step?}],
  segments?, mask_runs?, mask_bits?, lines?, meta?}`.
* `wave` takes one of these forms:
  * `{kind:"uniform", w0, dw}`: used when every sample is within 1e-4·dw of the linear model.
  * `{kind:"shared", grid}`: used when the wavelengths equal a grid in `RecordSetSpec.grids`.
  * `{kind:"array", off, dtype:"f32"|"f64"}`: f32 is used when the f32 round-off keeps the
    sample order and stays under 1 % of the smallest spacing; otherwise f64.
* Channel encodings:
  * `f32`: NaN is missing. This is the default.
  * `u16q` / `u16log`: u16 codes as in section 3, not shuffled inside a record. `lo`/`hi`/`step`
    are given in the channel entry (dex for `u16log`).
* `mask_bits` keys are decimal strings.

### pki1 (2D images)

```
"PKI1" | u32 header_len | header {id, nx, ny, enc:"u8"|"u16", off, stretch, wave, y, segments?} | binary
```

* `data[ny, nx]`; rows are flipped when `y` descends so that `y.dy > 0`;
  `y: {kind:"uniform", y0, dy}` from the first/last y.
* `stretch: {kind:"asinh"|"linear", lo, hi, a?}`. `lo`/`hi` default to the 1 / 99.5
  percentiles, and `a` is 0.1.
  * The stretched value is `s = clip((x-lo)/(hi-lo), 0, 1)`; with asinh,
    `s = asinh(s/a)/asinh(1/a)`.
* Codes:
  * `u8`: `rint(255 s)`, with NaN mapped to 0 (a reader cannot distinguish NaN from `lo`).
  * `u16`: `1 + rint(65534 s)`, with 0 meaning missing.
* Inverse: `x = lo + (hi-lo)*a*sinh(s*asinh(1/a))` for asinh, or `x = lo + (hi-lo)*s` for
  linear.
* `wave` is encoded as in pks1. A non-uniform wave array follows the image codes, at an
  aligned offset.

## 10. images.json (section 5.10)

```json
{"schema":"pk-images/1","roles":[{"key","label"}],
 "items":[{"id","row"?,"roles":{"<role>":{"url","w"?,"h"?,"thumb"?}},"caption"?,"tags":{facet:value}}],
 "facets":[{"key","label","values":[...]}],"presets":[{"key","label","facets":{k:[v]}}],"catalog"?}
```

* `ImageItem.w`/`h` are written into every role entry of the item (`roles[role].w/h`, the
  section 5.10 placement); items carry no item-level `w`/`h`.
* The `row` lookup key is `item.id`, or `tags[spec.join]` when `join` names a tag. The writer
  uses `rows` (for example `CatalogResult.row_of`, which also resolves aliases).
* Facet values are the distinct tag values, sorted with numeric strings first in numeric
  order.
* Preset facet values that no item carries are dropped, with a warning. Image presets read
  `Preset.data["facets"]`, falling back to `Preset.data`.
* Items without any URL are skipped, with a warning. A role an item uses but the spec does not
  declare is added, with a warning. Duplicate item ids raise.
* Pagination (IMPLEMENTATION_NOTES 10): when the gzip size of the document exceeds 4 MiB, the
  items are split into the fewest equal-count pages whose gzip size fits.
  * Each page is `images.<k>.<h8>.json`, holding `{"items":[...]}`.
  * The index keeps `"items": []` and lists `pages`.

## 11. Golden files (`tests/golden/codec/`)

`generate.py` rebuilds every golden file (`python tests/golden/codec/generate.py`;
`--check` compares without writing). `index.json` (`pk-golden-codec/1`) has one entry per
case:

* `column`: `{name, file, meta, n, codes?, expected, tol, tol_rel?, limits?}`.
  * `meta` holds the manifest fields needed to decode. `codes` are the raw codes after gunzip
    and unshuffle.
  * `expected` holds the decoded values: `null` for missing, strings for `str`/`cat*`, 0/1 for
    `bits`.
  * Cases: u16q (free step, fixed mag step, saturate, drop, constant), u16log, u8q, u8log,
    cat8 (unicode), cat16, bits, f32, u32, str (unicode, empty).
* `pos`: `{files, meta, bbox?, input, codes?, expected:{ra,dec}, tol, tol_deg}` for u32lin
  and i16off. Compare RA modulo 360.
* `sparse`: `{file, expected:{idx, kind}}`.
* `density`: `{file, meta (manifest entry), n, codes?, expected}` for a u16log count grid and
  an f32 median grid.
* `pks1` / `pki1`: `{file, expected:{header, wave, channels | codes, y, values}, tol_rel}`.
* `shard`: `{file, entries:{id:[0, off, len]}, expected:{id: record}}`.
* `rows`: `{file, expected: [row objects]}`.
* `catalog_chunks`: `{manifest, files, expected:{ids, cols:{key: values}}}`: a 10-row catalog
  with `chunk_starts` `[0, 3, 7]` whose u16q, cat8, bits, f32 and str columns and unicode ids
  are split exactly as the writer splits them (bits chunks not byte aligned). `manifest` is
  the `pk-catalog/1` subset a reader needs; `files` maps a label to every chunk file.

The Python test `tests/py/test_codec.py` checks that `generate.build()` reproduces every byte
and that the Python decoders return `expected`. JS readers should decode each file and compare
with `expected`, using `tol` (absolute) or `tol_rel`.
