# Vendored files

portalkit vendors eight ES modules from the GC fields manifold viewer
(`GCTreasury_Portal_Manifold.py` assets). They are byte-for-byte copies, except for the
3-line theme patch in `gl/renderer.js` described below (ARCHITECTURE.md section 6.11).

- Source directory: `/orange/adamginsburg/jwst/brick/analysis/gc_fields_manifold_assets/js/`
  (untracked in the analysis git repo at copy time; file mtimes 2026-10-02).
- Copied: 2026-10-10 by WP00.
- Destination: `web/js/vendor/gc/`.

`tests/py/test_constants_sync.py` re-hashes every vendored file against the `vendored sha256`
column, and checks that undoing the renderer patch reproduces the `source sha256`. It does
not compare against the live source directory, which keeps changing.

| vendored path | source path | source sha256 | vendored sha256 | patch |
|---|---|---|---|---|
| `web/js/vendor/gc/gl/renderer.js` | `gl/renderer.js` | `6f3324e79d68fd9a5c493b329a23c8e12385004cc8a8c47434524e1e51f57886` | `3ddafe6569819f3e6130b8acfd03d8ca9d854da8d5adc359c710cafeec049074` | theme patch (3 lines) |
| `web/js/vendor/gc/gl/shaders.js` | `gl/shaders.js` | `d5dbbb3ed8a883c446203095f09908d3eddbab8ef62fa32361600f8317652b1a` | `d5dbbb3ed8a883c446203095f09908d3eddbab8ef62fa32361600f8317652b1a` | none |
| `web/js/vendor/gc/gl/colormaps.js` | `gl/colormaps.js` | `54ec29b159cff763bdfad34c0826ac5ab9ca17fa8a850c3fec8ed999f8adbf91` | `54ec29b159cff763bdfad34c0826ac5ab9ca17fa8a850c3fec8ed999f8adbf91` | none |
| `web/js/vendor/gc/config/style.js` | `config/style.js` | `de569b02f99dd82dd8f5a147274240579d7a5ffc0b40a8fb018b34fff19e0d27` | `de569b02f99dd82dd8f5a147274240579d7a5ffc0b40a8fb018b34fff19e0d27` | none |
| `web/js/vendor/gc/math/frame.js` | `math/frame.js` | `60bf02f54f8dd8c050730e1341964abc1a2ed301300deb50d7edee750ca035ca` | `60bf02f54f8dd8c050730e1341964abc1a2ed301300deb50d7edee750ca035ca` | none |
| `web/js/vendor/gc/math/linalg.js` | `math/linalg.js` | `a4c2af0339befdafaa60d0615b2062f1c4a63c2a528d40a2ec2cfae4ef7d2f30` | `a4c2af0339befdafaa60d0615b2062f1c4a63c2a528d40a2ec2cfae4ef7d2f30` | none |
| `web/js/vendor/gc/workers/kernel.js` | `workers/kernel.js` | `2cc6046ca512646f2403837ca2ff84a5492ca9aaff01f7960d09a1734202eeaa` | `2cc6046ca512646f2403837ca2ff84a5492ca9aaff01f7960d09a1734202eeaa` | none |
| `web/js/vendor/gc/app/pick.js` | `app/pick.js` | `f8af75ebbd005df896e8b79ab9440f2d874ce1985a3c0c227571752b04129825` | `f8af75ebbd005df896e8b79ab9440f2d874ce1985a3c0c227571752b04129825` | none |

## Theme patch (`gl/renderer.js`, method `_composite`)

The renderer otherwise hard-codes the GC dark-theme cream neutral and orange selection
colours. The patch lets panes pass `neutral` and `selColor` (RGB in 0..1, read from the
`--pk-neutral` / `--pk-sel` tokens) through the render params `p`, and falls back to the
original constants when they are absent. Line numbers are unchanged.

| line | original | patched |
|---|---|---|
| 498 | `const rgb = (color.colors && color.colors[c]) \|\| NEUTRAL_RGB;` | `const rgb = (color.colors && color.colors[c]) \|\| p.neutral \|\| NEUTRAL_RGB;` |
| 502 | `gl.uniform3fv(u.u_neutral, NEUTRAL_RGB);` | `gl.uniform3fv(u.u_neutral, p.neutral \|\| NEUTRAL_RGB);` |
| 503 | `gl.uniform3fv(u.u_selColor, SELECTION_RGB);` | `gl.uniform3fv(u.u_selColor, p.selColor \|\| SELECTION_RGB);` |

## Updating

1. Copy the new upstream file over the vendored one.
2. For `renderer.js`, re-apply the three replacements above (the test locates them by line).
3. Update both sha256 columns in this file and run `python -m pytest tests/py/test_constants_sync.py`.
