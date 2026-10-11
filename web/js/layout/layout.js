// Pane-tree layout (ARCHITECTURE.md sections 3 and 6.7).
//
// mountLayout(root, tree, {app, registry}) -> Layout
//   Layout = {pane(id), popout(id, mode), collapse(id, on), reveal(id), destroy(),
//             panes(), info(id), whenMounted(id), isTabMode(), el}
//   Pane ctx = {id, config, dataset, app, store, hub, layout, theme, gpuBudget, tools,
//               setStatus(text, title), setTitle(text), title}
//   Titles: options.title seeds the header and tab text; ctx.setTitle replaces both ('' or
//   null restores the default).
//
// Features
// - Splitters (resizable_panels): pointer capture; at pointerdown every child of the split is
//   measured and frozen to pixels, and each move applies the TOTAL delta since pointerdown to
//   the two neighbouring (non-collapsed) cells, so the splitter tracks the pointer exactly.
//   Keyboard: focus + arrows move by 16 px; Home/End collapse toward an edge; double-click or
//   Enter resets the split. Flexible children use flex-grow equal to their pixel size (basis 0),
//   so pixel-exact sizes survive window resizes as proportions.
// - Fold headers (collapsible_panels) and pop-out (null | half | full; reparents the pane).
// - Mobile tab mode below MOBILE_BREAK_PX (responsive_mobile): one pane visible, tab strip in
//   tree order.
// - Persistence: localStorage['pk:<portal>:<page>:layout'] = {v, sizes:{splitId:[px..]},
//   collapsed:{paneId:bool}}; every access in try/catch; version mismatch discards.
// - Panes mount lazily (first time visible) through registry.load(type).mount(el, ctx); they
//   get resize(w, h, dpr) from a ResizeObserver (rAF-coalesced) and onVisible(v) on fold/tab.
// - Pane state: section[data-state] is 'loading' from mount start until mount() resolves and,
//   when the handle has a `firstPaint` promise, until it settles (bounded by
//   PANE_FIRST_PAINT_MAX_WAIT_MS); then 'ready', or 'error' on a rejection or failed mount.
//   While that is pending, a hidden pane (folded, inactive tab) is 'paused' instead of 'loading'.

import {MOBILE_BREAK_PX} from '../core/constants.js';

export const LAYOUT_VERSION = 1;
export const MIN_PANE_PX = 48;
export const KEY_STEP_PX = 16;

/**
 * Longest wait, after mount() resolves, for a pane handle's `firstPaint` promise before the
 * pane is marked 'ready' anyway (with a console warning). Same bound as the page-level
 * afterFirstPaint guard (IMPLEMENTATION_NOTES 100 and 112). Mutable through
 * LAYOUT_TIMING.firstPaintMaxWaitMs for tests.
 */
export const PANE_FIRST_PAINT_MAX_WAIT_MS = 30000;
export const LAYOUT_TIMING = {firstPaintMaxWaitMs: PANE_FIRST_PAINT_MAX_WAIT_MS};

/**
 * First-paint latch for pane handles (IMPLEMENTATION_NOTES 112):
 * {promise, resolve(), reject(err), done}. `promise` goes on the handle as `firstPaint`; the
 * pane calls resolve() once its first data-backed screen is drawn (or it settles on a note),
 * reject(err) when its data failed to load. Later calls are ignored. A no-op rejection handler
 * is attached so an unobserved rejection is not reported twice.
 */
export function firstPaintLatch() {
  let res;
  let rej;
  const promise = new Promise((a, b) => { res = a; rej = b; });
  promise.catch(() => {});
  const latch = {
    promise,
    done: false,
    resolve() {
      if (latch.done) return;
      latch.done = true;
      res();
    },
    reject(err) {
      if (latch.done) return;
      latch.done = true;
      rej(err);
    },
  };
  return latch;
}

const DEFAULT_TITLES = {
  list: 'Sources', detail: 'Details', scatter: 'Scatter', manifold: 'Manifold', hist: 'Histogram',
  sky: 'Sky', wavelength: 'Wavelengths', narrative: 'Story', spectrum1d: 'Spectrum',
  spectrum2d: '2D spectrum', sed: 'SED', gallery: 'Gallery', table: 'Table', readme: 'About',
  landing: 'Overview',
};

const svg = (d) => `<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;
/** Pop-out button icons: half = open as a floating window, full = maximise, close = return. */
const POP_ICONS = Object.freeze({
  half: svg('<rect x="2" y="5" width="9" height="9" rx="1"/><path d="M6 2h8v8M9 7l5-5"/>'),
  full: svg('<path d="M2 6V2h4M14 6V2h-4M2 10v4h4M14 10v4h-4"/>'),
  close: svg('<path d="M4 4l8 8M12 4l-8 8"/>'),
});

export function layoutStorageKey(portal, page) {
  return `pk:${portal}:${page}:layout`;
}

function fnv1a(str) {
  let h = 0x811c9dc5;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h.toString(16).padStart(8, '0');
}

/**
 * Normalize a LayoutNode tree into internal nodes with ids.
 * Split ids: node.id, else derived from the tree path ('r0', 'r0.c1', ...).
 * Pane ids: leaf.id, else the pane type (deduplicated with '-2', '-3', ...).
 * Exported for tests.
 */
export function normalizeTree(tree) {
  const panes = new Map();
  const splits = new Map();
  const order = [];
  const sig = [];
  function walk(node, pathId, depth) {
    if (!node || typeof node !== 'object') throw new TypeError('layout node must be an object');
    if (Array.isArray(node.row) || Array.isArray(node.col)) {
      const dir = Array.isArray(node.row) ? 'row' : 'col';
      const id = node.id || pathId;
      const s = {kind: 'split', dir, id, size: node.size, collapsedSpec: !!node.collapsed, children: [], depth};
      splits.set(id, s);
      sig.push(`${dir}:${id}[`);
      (node[dir]).forEach((child, k) => {
        const cdir = Array.isArray(child.row) ? 'r' : 'c';
        const cid = `${id}.${cdir}${k}`;
        const c = walk(child, cid, depth + 1);
        c.parent = s;
        c.index = k;
        s.children.push(c);
      });
      sig.push(']');
      return s;
    }
    if (typeof node.pane !== 'string') throw new TypeError('layout leaf needs a "pane" type');
    let id = node.id || node.pane;
    if (panes.has(id)) {
      let k = 2;
      while (panes.has(`${id}-${k}`)) k++;
      id = `${id}-${k}`;
    }
    const leaf = {
      kind: 'pane', id, type: node.pane, dataset: node.dataset ?? null, options: node.options || {},
      size: node.size, collapsed: !!node.collapsed, title: seedTitle(node.options), depth,
    };
    panes.set(id, leaf);
    order.push(id);
    sig.push(`p:${id}:${node.pane}`);
    return leaf;
  }
  const rootPath = tree && Array.isArray(tree.col) ? 'c0' : 'r0';
  const root = walk(tree, rootPath, 0);
  return {root, panes, splits, order, signature: fnv1a(sig.join('|'))};
}

/**
 * Initial pane title from `options.title` (IMPLEMENTATION_NOTES 77): a non-empty string, or a
 * finite number as text; anything else gives null (the type's default title is used).
 */
export function seedTitle(options) {
  const t = options && options.title;
  if (typeof t === 'string') return t.trim() ? t : null;
  if (typeof t === 'number' && Number.isFinite(t)) return String(t);
  return null;
}

/** Initial flex spec for children from their `size`: px (> 1), fraction (0..1], or unspecified. */
export function initialSizes(children) {
  const fr = children.map((c) => (typeof c.size === 'number' && c.size > 0 && c.size <= 1 ? c.size : null));
  const px = children.map((c) => (typeof c.size === 'number' && c.size > 1 ? c.size : null));
  const sumFrac = fr.reduce((a, b) => a + (b || 0), 0);
  const nUnspec = children.filter((_, i) => fr[i] === null && px[i] === null).length;
  const share = nUnspec ? (sumFrac < 1 ? (1 - sumFrac) / nUnspec : 1 / children.length) : 0;
  return children.map((_, i) => (px[i] !== null ? {fixedPx: px[i], grow: null} : {fixedPx: null, grow: fr[i] ?? share}));
}

/**
 * Pure drag arithmetic used by the splitters (exported for tests).
 * Given the sizes measured at pointerdown and the total pointer delta, returns new sizes for
 * neighbours a and b: [m[a] + d, m[b] - d] with d clamped so neither goes below `min`
 * (a pane that already starts below `min` may only grow).
 */
export function dragSizes(ma, mb, delta, min = MIN_PANE_PX) {
  const lo = Math.min(0, min - ma);
  const hi = Math.max(0, mb - min);
  const d = Math.max(lo, Math.min(hi, delta));
  return [ma + d, mb - d, d];
}

function readStored(key, version) {
  try {
    const raw = typeof localStorage !== 'undefined' ? localStorage.getItem(key) : null;
    if (!raw) return null;
    const v = JSON.parse(raw);
    if (!v || v.v !== version || typeof v !== 'object') return null;
    return {sizes: v.sizes && typeof v.sizes === 'object' ? v.sizes : {}, collapsed: v.collapsed && typeof v.collapsed === 'object' ? v.collapsed : {}};
  } catch (err) {
    if (err instanceof SyntaxError || (err && err.name === 'SecurityError')) return null;
    throw err;
  }
}

/** The media query below which the layout runs in mobile tab mode (one pane shown). */
export function tabModeQuery() {
  return `(max-width: ${MOBILE_BREAK_PX - 0.02}px)`;
}

/**
 * Ids of the panes mountLayout shows, and so mounts, at load: the saved fold state
 * (localStorage, same key and version as mountLayout) over the spec's `collapsed`, and in tab
 * mode only the initial active tab (the first unfolded pane). `tabMode` defaults to the current
 * tabModeQuery() match. Boot uses it to start fetching those panes' modules early
 * (core/boot.js warmPaneModules). Reads only; `norm` (normalizeTree) is not modified.
 */
export function shownAtLoad(norm, {portalId = 'portal', pageId = 'page', tabMode} = {}) {
  if (!norm || !Array.isArray(norm.order) || !norm.panes) return [];
  const saved = readStored(layoutStorageKey(portalId, pageId), `${LAYOUT_VERSION}:${norm.signature}`);
  const savedFold = saved ? saved.collapsed : {};
  const folded = (id) => (Object.prototype.hasOwnProperty.call(savedFold, id) ? !!savedFold[id] : !!norm.panes.get(id).collapsed);
  const tabs = tabMode === undefined ? (typeof matchMedia === 'function' && matchMedia(tabModeQuery()).matches) : !!tabMode;
  if (tabs) {
    const first = norm.order.find((id) => !folded(id)) || norm.order[0];
    return first === undefined ? [] : [first];
  }
  return norm.order.filter((id) => !folded(id));
}

function writeStored(key, value) {
  try {
    if (typeof localStorage !== 'undefined') localStorage.setItem(key, JSON.stringify(value));
  } catch (err) {
    if (err && (err.name === 'SecurityError' || err.name === 'QuotaExceededError')) return;
    throw err;
  }
}

function h(tag, cls, attrs) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (attrs) for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

/**
 * @param {HTMLElement} root
 * @param {object} tree   LayoutNode (section 4.3)
 * @param {{app: object, registry: {load(type): Promise<{mount}>, titles?: object}}} ctx
 */
export function mountLayout(root, tree, ctx) {
  const app = ctx.app || {};
  const store = app.store || null;
  const registry = ctx.registry;
  const norm = normalizeTree(tree);
  const {panes, splits, order} = norm;
  const portalId = (app.page && app.page.portal && app.page.portal.id) || 'portal';
  const pageId = (app.page && app.page.page && app.page.page.id) || 'page';
  const storageKey = layoutStorageKey(portalId, pageId);
  const version = `${LAYOUT_VERSION}:${norm.signature}`;
  const saved = readStored(storageKey, version) || {sizes: {}, collapsed: {}};
  const titles = Object.assign({}, DEFAULT_TITLES, (registry && registry.titles) || {});
  const cleanups = [];
  let destroyed = false;
  let tabMode = false;
  let activeTab = null;
  let popped = null;          // {id, mode, placeholder}

  // ------------------------------------------------------------------ DOM construction
  const container = h('div', 'pk-layout');
  const tabs = h('div', 'pk-tabs', {role: 'tablist', 'aria-label': 'Panes'});
  const stage = h('div', 'pk-stage');
  container.append(tabs, stage);
  const popLayer = h('div', 'pk-popout-layer', {hidden: ''});
  const popBackdrop = h('div', 'pk-popout-backdrop');
  const popFrame = h('div', 'pk-popout-frame', {role: 'dialog', 'aria-modal': 'false'});
  popLayer.append(popBackdrop, popFrame);
  popBackdrop.addEventListener('click', () => api.popout(popped && popped.id, null));

  for (const leaf of panes.values()) {
    if (Object.prototype.hasOwnProperty.call(saved.collapsed, leaf.id)) leaf.collapsed = !!saved.collapsed[leaf.id];
  }

  /** Default title of a pane: options.title, else the registry title of its type. */
  function defaultTitle(leaf) {
    return seedTitle(leaf.options) || titles[leaf.type] || leaf.type;
  }

  function paneTitle(leaf) {
    return leaf.title || titles[leaf.type] || leaf.type;
  }

  /** ctx.setTitle: '', null or undefined restore the default title. */
  function setPaneTitle(leaf, text) {
    const t = text === null || text === undefined || String(text).trim() === '' ? defaultTitle(leaf) : String(text);
    leaf.title = t;
    if (leaf.titleEl) leaf.titleEl.textContent = t;
    if (leaf.paneEl) leaf.paneEl.setAttribute('aria-label', t);
    if (leaf.tabEl) leaf.tabEl.textContent = t;
  }

  function buildLeaf(leaf) {
    const pane = h('section', 'pk-pane', {'data-pane-id': leaf.id, 'data-pane-type': leaf.type});
    // Folded from the start (updateVisibility keeps it in sync): set before the first style
    // resolution so the chevron does not run its rotate transition at load.
    if (leaf.collapsed) pane.classList.add('pk-collapsed');
    const head = h('header', 'pk-pane-head');
    const fold = h('button', 'pk-fold', {type: 'button', 'aria-expanded': String(!leaf.collapsed), 'aria-controls': `pk-body-${leaf.id}`});
    const chev = h('span', 'pk-chev', {'aria-hidden': 'true'});
    const title = h('span', 'pk-pane-title');
    title.textContent = paneTitle(leaf);
    pane.setAttribute('aria-label', title.textContent);
    fold.append(chev, title);
    fold.title = 'Collapse / expand';
    const status = h('span', 'pk-pane-status');
    const tools = h('div', 'pk-pane-tools');
    const pop = h('button', 'pk-btn pk-icon-btn pk-popout-btn', {type: 'button'});
    head.append(fold, status, tools, pop);
    const body = h('div', 'pk-pane-body', {id: `pk-body-${leaf.id}`});
    const skel = h('div', 'pk-skeleton', {'aria-busy': 'true'});
    skel.textContent = 'Loading…';
    body.append(skel);
    pane.append(head, body);
    // Tab mode shows one pane at a time; folding there would only change hidden state.
    fold.addEventListener('click', () => { if (!tabMode) api.collapse(leaf.id, !leaf.collapsed); });
    pop.addEventListener('click', () => {
      const cur = popped && popped.id === leaf.id ? popped.mode : null;
      api.popout(leaf.id, cur === null ? 'half' : cur === 'half' ? 'full' : null);
    });
    Object.assign(leaf, {paneEl: pane, headEl: head, bodyEl: body, foldEl: fold, titleEl: title, toolsEl: tools, statusEl: status, popEl: pop,
      handle: null, mounting: null, mounted: false, visible: null, lastW: 0, lastH: 0});
    updatePopButton(leaf);
    return pane;
  }

  function buildNode(node) {
    if (node.kind === 'pane') return buildLeaf(node);
    const el = h('div', `pk-split pk-${node.dir}`, {'data-split-id': node.id});
    node.el = el;
    node.splitters = [];
    const init = initialSizes(node.children);
    // Saved sizes are used only when every entry is a positive number: mixing restored pixel
    // weights with initial fractions would squash the siblings to a few pixels.
    const rawSaved = saved.sizes[node.id];
    const savedSizes = Array.isArray(rawSaved) && rawSaved.length === node.children.length
      && rawSaved.every((v) => Number.isFinite(v) && v > 0) ? rawSaved : null;
    node.children.forEach((child, k) => {
      if (k > 0) {
        const sp = h('div', 'pk-splitter', {
          role: 'separator', tabindex: '0',
          'aria-orientation': node.dir === 'row' ? 'vertical' : 'horizontal',
          'aria-label': 'Resize panes',
        });
        sp.dataset.index = String(k - 1);
        wireSplitter(node, sp, k - 1);
        node.splitters.push(sp);
        el.append(sp);
      }
      const cell = h('div', 'pk-cell');
      child.cell = cell;
      child.fixedPx = init[k].fixedPx;
      child.grow = init[k].grow;
      child.initFixed = init[k].fixedPx;
      child.initGrow = init[k].grow;
      if (savedSizes && Number.isFinite(savedSizes[k]) && savedSizes[k] > 0) {
        if (child.fixedPx !== null) child.fixedPx = savedSizes[k];
        else child.grow = savedSizes[k];
      }
      cell.append(buildNode(child));
      el.append(cell);
    });
    return el;
  }

  const rootNode = norm.root;
  if (rootNode.kind === 'pane') {
    const cell = h('div', 'pk-cell pk-root-cell');
    rootNode.cell = cell;
    rootNode.fixedPx = null;
    rootNode.grow = 1;
    cell.append(buildLeaf(rootNode));
    stage.append(cell);
  } else {
    stage.append(buildNode(rootNode));
  }

  for (const id of order) {
    const leaf = panes.get(id);
    const b = h('button', 'pk-tab', {type: 'button', role: 'tab', 'aria-selected': 'false', 'data-pane-id': id});
    b.textContent = paneTitle(leaf);
    b.addEventListener('click', () => setActiveTab(id));
    leaf.tabEl = b;
    tabs.append(b);
  }
  tabs.addEventListener('keydown', (ev) => {
    if (ev.key !== 'ArrowLeft' && ev.key !== 'ArrowRight') return;
    const i = order.indexOf(activeTab);
    const j = Math.max(0, Math.min(order.length - 1, i + (ev.key === 'ArrowRight' ? 1 : -1)));
    setActiveTab(order[j]);
    panes.get(order[j]).tabEl.focus();
    ev.preventDefault();
  });

  root.append(container);
  document.body.append(popLayer);

  // ------------------------------------------------------------------ sizes
  function nodeCollapsed(node) {
    if (node.kind === 'pane') return node.collapsed && !(popped && popped.id === node.id);
    return node.children.every(nodeCollapsed);
  }

  function applySizes(split) {
    applySizeClasses(split);
    applySplitterAria(split);
  }

  /** Flex sizes, collapsed cells and inert splitters of one split (no layout reads). */
  function applySizeClasses(split) {
    const vis = split.children.filter((c) => !nodeCollapsed(c));
    const anyFlex = vis.some((c) => c.fixedPx === null);
    let filler = null;
    if (!anyFlex && vis.length) filler = vis.reduce((a, b) => (b.fixedPx > a.fixedPx ? b : a));
    // flex-grow values summing below 1 leave part of the free space unused (CSS flexbox rule),
    // so the visible weights are normalised to sum to at least 1.
    const growSum = vis.reduce((a, c) => a + (c.fixedPx === null ? Math.max(0, c.grow) : 0), 0);
    const growK = growSum > 0 && growSum < 1 ? 1 / growSum : 1;
    for (const c of split.children) {
      const cell = c.cell;
      const col = nodeCollapsed(c);
      cell.classList.toggle('pk-is-collapsed', col);
      if (col) cell.style.flex = '0 0 auto';
      else if (c === filler) cell.style.flex = `1 1 ${c.fixedPx}px`;
      else if (c.fixedPx !== null) cell.style.flex = `0 1 ${c.fixedPx}px`;
      else cell.style.flex = `${Math.max(1e-6, c.grow * growK)} 1 0px`;
    }
    split.splitters.forEach((sp, k) => sp.classList.toggle('pk-splitter-inert', !neighbours(split, k)));
  }

  /** aria-valuenow of the splitters of one split (reads geometry). */
  function applySplitterAria(split) {
    split.splitters.forEach((sp, k) => {
      const ends = neighbours(split, k);
      if (ends) {
        const a = split.children[ends[0]];
        const total = split.el.getBoundingClientRect()[split.dir === 'row' ? 'width' : 'height'] || 0;
        const sz = a.cell.getBoundingClientRect()[split.dir === 'row' ? 'width' : 'height'] || 0;
        sp.setAttribute('aria-valuenow', total ? String(Math.round((100 * sz) / total)) : '50');
        sp.setAttribute('aria-valuemin', '0');
        sp.setAttribute('aria-valuemax', '100');
      }
    });
  }

  function applyAllSizes() {
    // Every class first, then the geometry reads: a read between two splits would resolve
    // styles mid-update, and the next class change would then start a CSS transition
    // (splitter background) that repaints frames during startup.
    for (const s of splits.values()) applySizeClasses(s);
    for (const s of splits.values()) applySplitterAria(s);
  }

  /** Nearest non-collapsed child indices on each side of splitter k, or null. */
  function neighbours(split, k) {
    let a = k;
    while (a >= 0 && nodeCollapsed(split.children[a])) a--;
    let b = k + 1;
    while (b < split.children.length && nodeCollapsed(split.children[b])) b++;
    if (a < 0 || b >= split.children.length) return null;
    return [a, b];
  }

  function measure(split) {
    const dim = split.dir === 'row' ? 'width' : 'height';
    return split.children.map((c) => c.cell.getBoundingClientRect()[dim]);
  }

  /**
   * Flexible children carry `grow` weights. Freezing replaces visible weights with measured px,
   * so hidden (collapsed) flexible siblings need the same px-per-weight scale to keep their
   * share when they are expanded again.
   */
  function flexScale(split, m) {
    let px = 0;
    let w = 0;
    split.children.forEach((c, i) => {
      if (nodeCollapsed(c) || c.fixedPx !== null) return;
      px += m[i];
      w += c.grow;
    });
    return w > 0 && px > 0 ? px / w : 1;
  }

  /** Convert every visible child of `split` to its current pixel size (exactness for drags). */
  function freeze(split, m) {
    const scale = flexScale(split, m);
    split.children.forEach((c, i) => {
      if (nodeCollapsed(c)) {
        if (c.fixedPx === null) c.grow *= scale;
        return;
      }
      if (c.fixedPx !== null) c.fixedPx = m[i];
      else c.grow = m[i];
    });
  }

  function setPx(c, px) {
    if (c.fixedPx !== null) c.fixedPx = px;
    else c.grow = px;
  }

  // Last sizes written (or read): kept when the current geometry cannot be measured.
  let lastSizes = Object.assign({}, saved.sizes);

  function persist() {
    // In mobile tab mode (or while the stage is hidden) cells are display:none or stacked, and
    // measuring them would store zeros that squash panes on the next desktop load; sizes are
    // only measured from a laid-out desktop split, otherwise the previous values are kept.
    const sizes = Object.assign({}, lastSizes);
    if (!tabMode) {
      for (const s of splits.values()) {
        const m = measure(s);
        const bad = s.children.some((c, i) => !nodeCollapsed(c) && !(m[i] > 0));
        if (bad) continue;
        const scale = flexScale(s, m);
        sizes[s.id] = s.children.map((c, i) => {
          if (nodeCollapsed(c)) return Math.round((c.fixedPx ?? (c.grow ?? 0) * scale) * 10) / 10;
          return Math.round(m[i] * 10) / 10;
        });
      }
    }
    lastSizes = sizes;
    const collapsed = {};
    for (const leaf of panes.values()) collapsed[leaf.id] = !!leaf.collapsed;
    writeStored(storageKey, {v: version, sizes, collapsed});
  }

  function resetSplit(split) {
    split.children.forEach((c) => { c.fixedPx = c.initFixed; c.grow = c.initGrow; });
    applySizes(split);
    persist();
  }

  function wireSplitter(split, sp, k) {
    const axis = split.dir === 'row' ? 'clientX' : 'clientY';
    let drag = null;
    sp.addEventListener('pointerdown', (ev) => {
      if (ev.button !== 0 || tabMode) return;
      const ends = neighbours(split, k);
      if (!ends) return;
      ev.preventDefault();
      const m = measure(split);
      freeze(split, m);
      applySizes(split);
      drag = {start: ev[axis], ma: m[ends[0]], mb: m[ends[1]], a: split.children[ends[0]], b: split.children[ends[1]], id: ev.pointerId};
      sp.setPointerCapture(ev.pointerId);
      sp.classList.add('pk-active');
      document.body.classList.add('pk-dragging', split.dir === 'row' ? 'pk-dragging-x' : 'pk-dragging-y');
    });
    sp.addEventListener('pointermove', (ev) => {
      if (!drag || ev.pointerId !== drag.id) return;
      const [na, nb] = dragSizes(drag.ma, drag.mb, ev[axis] - drag.start);
      setPx(drag.a, na);
      setPx(drag.b, nb);
      applySizes(split);
    });
    const end = (ev) => {
      if (!drag || (ev && ev.pointerId !== drag.id)) return;
      if (sp.hasPointerCapture && sp.hasPointerCapture(drag.id)) sp.releasePointerCapture(drag.id);
      drag = null;
      sp.classList.remove('pk-active');
      document.body.classList.remove('pk-dragging', 'pk-dragging-x', 'pk-dragging-y');
      persist();
    };
    sp.addEventListener('pointerup', end);
    sp.addEventListener('pointercancel', end);
    sp.addEventListener('lostpointercapture', end);
    sp.addEventListener('dblclick', () => resetSplit(split));
    sp.addEventListener('keydown', (ev) => {
      const ends = neighbours(split, k);
      if (!ends) return;
      const back = split.dir === 'row' ? 'ArrowLeft' : 'ArrowUp';
      const fwd = split.dir === 'row' ? 'ArrowRight' : 'ArrowDown';
      let delta = null;
      const m = measure(split);
      if (ev.key === back) delta = -KEY_STEP_PX * (ev.shiftKey ? 4 : 1);
      else if (ev.key === fwd) delta = KEY_STEP_PX * (ev.shiftKey ? 4 : 1);
      else if (ev.key === 'Home') delta = -m[ends[0]];
      else if (ev.key === 'End') delta = m[ends[1]];
      else if (ev.key === 'Enter') { resetSplit(split); ev.preventDefault(); return; }
      if (delta === null) return;
      ev.preventDefault();
      freeze(split, m);
      const [na, nb] = dragSizes(m[ends[0]], m[ends[1]], delta);
      setPx(split.children[ends[0]], na);
      setPx(split.children[ends[1]], nb);
      applySizes(split);
      persist();
    });
  }

  // ------------------------------------------------------------------ mounting and visibility
  const pendingResize = new Map();
  let resizeRaf = 0;
  const ro = typeof ResizeObserver === 'function' ? new ResizeObserver((entries) => {
    for (const e of entries) {
      const id = e.target.dataset.paneBody;
      if (!id) continue;
      const box = e.contentRect;
      pendingResize.set(id, [box.width, box.height]);
    }
    if (!resizeRaf) resizeRaf = requestAnimationFrame(flushResize);
  }) : null;

  function flushResize() {
    resizeRaf = 0;
    const dpr = window.devicePixelRatio || 1;
    for (const [id, [w, hgt]] of pendingResize) {
      const leaf = panes.get(id);
      if (!leaf || !leaf.handle || w <= 0 || hgt <= 0) continue;
      if (leaf.lastW === w && leaf.lastH === hgt && leaf.lastDpr === dpr) continue;
      leaf.lastW = w;
      leaf.lastH = hgt;
      leaf.lastDpr = dpr;
      callHandle(leaf, 'resize', w, hgt, dpr);
      if (store) store.emit('pane:resize', {pane: id, w, h: hgt});
    }
    pendingResize.clear();
  }

  function callHandle(leaf, method, ...args) {
    const fn = leaf.handle && leaf.handle[method];
    if (typeof fn !== 'function') return undefined;
    try {
      return fn.apply(leaf.handle, args);
    } catch (err) {
      console.error(`pane ${leaf.id}.${method} failed`, err);
      return undefined;
    }
  }

  function paneContext(leaf) {
    return {
      id: leaf.id,
      config: leaf.options,
      dataset: leaf.dataset,
      app,
      store,
      hub: app.hub,
      layout: api,
      theme: typeof app.theme === 'function' ? app.theme : () => ({}),
      // The page's shared GPU budget (IMPLEMENTATION_NOTES 68); same object as ctx.app.gpuBudget.
      get gpuBudget() { return app.gpuBudget || null; },
      tools: leaf.toolsEl,
      setStatus(text, title) {
        leaf.statusEl.textContent = text || '';
        if (title !== undefined) leaf.statusEl.title = title;
      },
      setTitle(text) {
        setPaneTitle(leaf, text);
      },
      get title() { return paneTitle(leaf); },
    };
  }

  function ensureMounted(leaf) {
    if (leaf.mounting) return leaf.mounting;
    leaf.firstPaintPending = true;
    applyPendingState(leaf);
    leaf.mounting = (async () => {
      const mod = await registry.load(leaf.type);
      if (destroyed) return null;
      // Panes render their own loading skeleton while their dataset streams in.
      const skel = leaf.bodyEl.querySelector(':scope > .pk-skeleton');
      if (skel) skel.remove();
      const handle = await mod.mount(leaf.bodyEl, paneContext(leaf));
      leaf.handle = handle || {resize() {}, destroy() {}};
      leaf.module = mod;
      leaf.mounted = true;
      leaf.bodyEl.dataset.paneBody = leaf.id;
      if (ro) ro.observe(leaf.bodyEl);
      const r = leaf.bodyEl.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) {
        leaf.lastW = r.width;
        leaf.lastH = r.height;
        leaf.lastDpr = window.devicePixelRatio || 1;
        callHandle(leaf, 'resize', r.width, r.height, leaf.lastDpr);
      }
      leaf.visible = null;
      updateVisibility();
      watchFirstPaint(leaf);
      return leaf.handle;
    })();
    leaf.mounting.catch((err) => {
      leaf.firstPaintPending = false;
      leaf.paneEl.dataset.state = 'error';
      leaf.bodyEl.textContent = '';
      const note = h('div', 'pk-error-note', {role: 'alert'});
      note.textContent = `This panel could not be loaded (${leaf.type}): ${err && err.message ? err.message : err}`;
      leaf.bodyEl.append(note);
      console.error(`pane ${leaf.id} (${leaf.type}) failed to mount`, err);
    });
    return leaf.mounting;
  }

  /**
   * data-state stays 'loading' until the handle's `firstPaint` promise settles
   * (IMPLEMENTATION_NOTES 112): resolve -> 'ready', reject -> 'error' (the pane keeps its own
   * error note), no settle within LAYOUT_TIMING.firstPaintMaxWaitMs -> 'ready' with a warning.
   * A handle without `firstPaint` is 'ready' at mount. While the first paint is pending, a pane
   * the user has hidden (folded; another tab) is 'paused' and does not hold back the page's
   * first paint; it returns to 'loading' when shown again (see applyPendingState).
   */
  function watchFirstPaint(leaf) {
    const fp = leaf.handle && leaf.handle.firstPaint;
    if (!fp || typeof fp.then !== 'function') {
      leaf.firstPaintPending = false;
      leaf.paneEl.dataset.state = 'ready';
      return;
    }
    let settled = false;
    const wait = LAYOUT_TIMING.firstPaintMaxWaitMs;
    const guard = Number.isFinite(wait) && wait >= 0 ? setTimeout(() => {
      if (settled) return;
      settled = true;
      leaf.firstPaintPending = false;
      if (destroyed) return;
      console.warn(`pane ${leaf.id} (${leaf.type}): no first paint ${wait} ms after mount; marking it ready`);
      leaf.paneEl.dataset.state = 'ready';
    }, wait) : null;
    const finish = (state, err) => {
      if (settled) return;
      settled = true;
      leaf.firstPaintPending = false;
      if (guard !== null) clearTimeout(guard);
      if (destroyed) return;
      leaf.paneEl.dataset.state = state;
      if (err) console.warn(`pane ${leaf.id} (${leaf.type}): first paint failed`, err);
    };
    fp.then(() => finish('ready'), (err) => finish('error', err || new Error('first paint failed')));
  }

  /**
   * data-state of a pane whose first paint is pending: 'loading' while it is visible, 'paused'
   * while hidden (folded, or not the active tab). A hidden pane skips its draws (onVisible(false)),
   * so it cannot reach its first paint and must not hold back afterFirstPaint (notes 100, 112).
   */
  function applyPendingState(leaf) {
    if (!leaf.firstPaintPending || destroyed) return;
    leaf.paneEl.dataset.state = isVisible(leaf) ? 'loading' : 'paused';
  }

  function isVisible(leaf) {
    if (popped && popped.id === leaf.id) return true;
    if (tabMode) return leaf.id === activeTab;
    return !leaf.collapsed;
  }

  function updateVisibility() {
    for (const leaf of panes.values()) {
      const v = isVisible(leaf);
      if (v && !leaf.mounting) ensureMounted(leaf).catch(() => {});
      if (leaf.mounted && leaf.visible !== v) {
        leaf.visible = v;
        callHandle(leaf, 'onVisible', v);
      }
      leaf.paneEl.classList.toggle('pk-collapsed', !!leaf.collapsed && !tabMode && !(popped && popped.id === leaf.id));
      leaf.foldEl.setAttribute('aria-expanded', String(!leaf.collapsed));
      applyPendingState(leaf);
    }
  }

  function updatePopButton(leaf) {
    const mode = popped && popped.id === leaf.id ? popped.mode : null;
    const next = mode === null ? 'half' : mode === 'half' ? 'full' : null;
    // Inline SVG icons: the Unicode window glyphs are missing from many system fonts.
    leaf.popEl.innerHTML = POP_ICONS[mode === 'full' ? 'close' : mode === 'half' ? 'full' : 'half'];
    const label = next === 'half' ? 'Pop out (half screen)' : next === 'full' ? 'Full screen' : 'Return to layout';
    leaf.popEl.title = label;
    leaf.popEl.setAttribute('aria-label', label);
  }

  // ------------------------------------------------------------------ tab mode
  function markTabPath() {
    for (const el of stage.querySelectorAll('.pk-tab-path')) el.classList.remove('pk-tab-path');
    if (!tabMode || !activeTab) return;
    let node = panes.get(activeTab);
    while (node) {
      if (node.cell) node.cell.classList.add('pk-tab-path');
      node = node.parent;
    }
  }

  function setActiveTab(id) {
    if (!panes.has(id)) return;
    activeTab = id;
    for (const leaf of panes.values()) {
      leaf.tabEl.setAttribute('aria-selected', String(leaf.id === id));
      leaf.tabEl.tabIndex = leaf.id === id ? 0 : -1;
    }
    const t = panes.get(id).tabEl;
    if (t.scrollIntoView && tabMode) t.scrollIntoView({block: 'nearest', inline: 'nearest'});
    markTabPath();
    updateVisibility();
  }

  function setTabMode(on) {
    tabMode = !!on;
    container.classList.toggle('pk-tabmode', tabMode);
    tabs.hidden = !tabMode;
    for (const leaf of panes.values()) {
      if (tabMode) leaf.foldEl.setAttribute('tabindex', '-1');
      else leaf.foldEl.removeAttribute('tabindex');
      leaf.foldEl.setAttribute('aria-disabled', String(tabMode));
    }
    if (tabMode && !activeTab) {
      activeTab = order.find((id) => !panes.get(id).collapsed) || order[0];
    }
    if (tabMode) setActiveTab(activeTab);
    else {
      markTabPath();
      applyAllSizes();
      updateVisibility();
    }
  }

  const mql = typeof matchMedia === 'function' ? matchMedia(tabModeQuery()) : null;
  const onMql = () => setTabMode(mql.matches);
  if (mql) {
    if (mql.addEventListener) mql.addEventListener('change', onMql);
    else if (mql.addListener) mql.addListener(onMql);
    cleanups.push(() => {
      if (mql.removeEventListener) mql.removeEventListener('change', onMql);
      else if (mql.removeListener) mql.removeListener(onMql);
    });
  }

  // ------------------------------------------------------------------ pop-out
  function applyPopout(popMap) {
    const entries = Object.entries(popMap || {}).filter(([id, mode]) => panes.has(id) && (mode === 'half' || mode === 'full'));
    const want = entries.length ? {id: entries[0][0], mode: entries[0][1]} : null;
    if (popped && (!want || want.id !== popped.id)) {
      const leaf = panes.get(popped.id);
      popped.placeholder.replaceWith(leaf.paneEl);
      leaf.paneEl.classList.remove('pk-popped');
      const prev = popped;
      popped = null;
      updatePopButton(leaf);
      popLayer.hidden = true;
      popLayer.className = 'pk-popout-layer';
      document.body.classList.remove('pk-has-popout');
      if (prev.parentSplit) applySizes(prev.parentSplit);
    }
    if (want) {
      const leaf = panes.get(want.id);
      if (!popped) {
        const ph = h('div', 'pk-popout-placeholder');
        ph.textContent = 'Shown in a pop-out — click to return';
        ph.addEventListener('click', () => api.popout(want.id, null));
        leaf.paneEl.replaceWith(ph);
        popFrame.append(leaf.paneEl);
        popped = {id: want.id, mode: want.mode, placeholder: ph, parentSplit: leaf.parent || null};
        leaf.paneEl.classList.add('pk-popped');
      }
      popped.mode = want.mode;
      popLayer.hidden = false;
      popLayer.className = `pk-popout-layer pk-popout-${want.mode}`;
      document.body.classList.add('pk-has-popout');
      updatePopButton(leaf);
      if (leaf.parent) applySizes(leaf.parent);
    }
    updateVisibility();
  }

  // ------------------------------------------------------------------ public API
  const api = {
    el: container,

    pane(id) {
      const leaf = panes.get(id);
      return leaf ? leaf.handle : null;
    },

    panes() {
      return order.map((id) => api.info(id));
    },

    info(id) {
      const leaf = panes.get(id);
      if (!leaf) return null;
      return {id: leaf.id, type: leaf.type, title: paneTitle(leaf), dataset: leaf.dataset, options: leaf.options, collapsed: !!leaf.collapsed,
        mounted: !!leaf.mounted, visible: !!leaf.visible, el: leaf.paneEl, body: leaf.bodyEl, handle: leaf.handle};
    },

    whenMounted(id) {
      const leaf = panes.get(id);
      if (!leaf) return Promise.reject(new Error(`unknown pane: ${id}`));
      return ensureMounted(leaf);
    },

    isTabMode() {
      return tabMode;
    },

    popout(id, mode) {
      if (mode !== null && mode !== 'half' && mode !== 'full') throw new TypeError(`bad pop-out mode: ${mode}`);
      if (mode !== null && !panes.has(id)) throw new Error(`unknown pane: ${id}`);
      const popMap = mode ? {[id]: mode} : {};
      if (store) {
        const lay = store.get('layout') || {};
        store.set({layout: Object.assign({}, lay, {popout: popMap})}, {source: 'layout'});
      }
      applyPopout(popMap);
    },

    collapse(id, on) {
      const leaf = panes.get(id);
      if (!leaf) throw new Error(`unknown pane: ${id}`);
      leaf.collapsed = !!on;
      if (leaf.parent) applySizes(leaf.parent);
      // Ancestors whose children are now all collapsed shrink as well.
      let p = leaf.parent;
      while (p && p.parent) { applySizes(p.parent); p = p.parent; }
      updateVisibility();
      persist();
    },

    reveal(id) {
      const leaf = panes.get(id);
      if (!leaf) return Promise.reject(new Error(`unknown pane: ${id}`));
      if (tabMode) setActiveTab(id);
      else if (leaf.collapsed) api.collapse(id, false);
      const p = ensureMounted(leaf);
      if (leaf.paneEl.scrollIntoView) leaf.paneEl.scrollIntoView({block: 'nearest', inline: 'nearest'});
      return p;
    },

    destroy() {
      if (destroyed) return;
      destroyed = true;
      for (const leaf of panes.values()) {
        if (leaf.mounted) callHandle(leaf, 'destroy');
      }
      if (ro) ro.disconnect();
      if (resizeRaf) cancelAnimationFrame(resizeRaf);
      for (const fn of cleanups) fn();
      container.remove();
      popLayer.remove();
      document.body.classList.remove('pk-has-popout', 'pk-dragging', 'pk-dragging-x', 'pk-dragging-y');
    },
  };

  // Store wiring: pop-out state (hash view=), tiers and Esc.
  if (store) {
    cleanups.push(store.on(['layout'], (changed, st, sources) => {
      if (sources.has('layout') && sources.size === 1) return;
      const lay = st.get('layout') || {};
      applyPopout(lay.popout || {});
    }, {sync: true}));
    cleanups.push(store.on(['tier:*'], (changed, st) => {
      for (const k of changed) {
        if (!k.startsWith('tier:')) continue;
        const leaf = panes.get(k.slice(5));
        if (!leaf) continue;
        const t = st.get(k);
        if (t) {
          leaf.paneEl.dataset.tier = t;
          leaf.statusEl.dataset.tier = t;
        } else {
          delete leaf.paneEl.dataset.tier;
        }
      }
    }, {sync: true}));
    cleanups.push(store.onEvent('keys:escape', (p) => {
      if (p && !p.handled && popped) {
        p.handled = true;
        api.popout(popped.id, null);
      }
    }));
  }
  const onWinKey = (ev) => {
    if (ev.key === 'Escape' && popped && !store) api.popout(popped.id, null);
  };
  document.addEventListener('keydown', onWinKey);
  cleanups.push(() => document.removeEventListener('keydown', onWinKey));

  // Initial state.
  applyAllSizes();
  setTabMode(mql ? mql.matches : false);
  const initialPop = store && store.get('layout') && store.get('layout').popout;
  if (initialPop && Object.keys(initialPop).length) applyPopout(initialPop);
  // aria-valuenow needs real geometry; refresh after the first frame.
  if (typeof requestAnimationFrame === 'function') requestAnimationFrame(() => { if (!destroyed && !tabMode) applyAllSizes(); });
  return api;
}
