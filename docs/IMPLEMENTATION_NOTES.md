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
