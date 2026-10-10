// Numeric thresholds shared by the builder and the viewer (ARCHITECTURE.md section 6.12).
// FROZEN: mirrored exactly in portalkit/constants.py; tests/py/test_constants_sync.py
// parses both files and fails on any difference. Pure module (no DOM).

// Rendering tiers (section 7.1)
export const T0_MAX = 3000;
export const T1_MAX = 300000;

// Row order, LOD and chunking (section 5.4)
export const LOD_MIN_N = 131072;
export const LOD_BASE = 32768;
export const CHUNK_ROWS = 1048576;

// Motion / interaction
export const MOTION_PREFIX = 1500000;
export const SETTLE_MS = 150;
export const SEMANTIC_ZOOM_MARKERS = 50000;
export const SKY_MARKER_CAP = 20000;

// GPU (section 7.5)
export const MAXD_GPU = 28;
export const DENSITY_PANES_MAX = 2;
export const GPU_BUDGET_BYTES = 268435456;
export const MRT_BYTES_PER_PX = 48;

// Record shards and LRUs (section 5.6)
export const SHARD_TARGET = 262144;
export const SHARD_MAX = 524288;
export const SHARD_LRU_BYTES = 16777216;
export const RECORD_LRU = 64;

// Exact rows (section 5.5)
export const ROW_CHUNK = 4096;
export const EXACT_ROWS_MAX_N = 1000000;

// Selection and hash (section 6.3)
export const UNDO_MAX = 20;
export const UNDO_BYTES = 67108864;
export const IDS_IN_HASH_MAX = 300;
export const BITS_IN_HASH_MAX_N = 200000;
export const HASH_MAX_CHARS = 8000;

// Size budgets (section 5.11)
export const PAGE_HTML_MAX = 200000;
export const PAGE_JSON_MAX = 65536;
export const MANIFEST_MAX = 262144;
export const FIRST_PAINT_MAX_1M = 2097152;
export const FIRST_PAINT_MAX_5M = 6291456;
export const COL_FILE_MAX = 16777216;

// Misc
export const MAG_STEP = 0.001;
export const FADE_WIDTH = 0.12;
export const MOBILE_BREAK_PX = 900;
export const HASH_DEBOUNCE_MS = 250;
export const ALADIN_TIMEOUT_MS = 8000;
