// Global keyboard handling (keyboard_nav, ARCHITECTURE.md sections 6.3 and 8.2).
//
// Keys (ignored while typing in inputs, except Esc):
//   ArrowDown / ArrowRight, ArrowUp / ArrowLeft   walk the filtered visible list ('nav:step')
//   j / n, p                                       same as next / previous
//   Esc                                            cancel brush / close lightbox / exit pop-out ('keys:escape')
//   ?                                              help ('help:toggle')
//   space                                          tour play/pause ('tour:toggle')
//   x                                              clear selection
//   i                                              invert within drawable ('selection:invert')
//   k                                              keep only: filters:<ds>.ids = selected ids
//   Ctrl/Cmd+Z, Ctrl/Cmd+Shift+Z (or Ctrl+Y)       undo / redo
//
// Each action is offered first as a store event whose payload carries `handled: false`; a pane
// that owns the behaviour (the list for 'nav:step', a brush for 'keys:escape', a manifold for
// 'tour:toggle', a plot that knows its drawable mask for 'selection:invert') sets
// `payload.handled = true`. Unhandled actions fall back to the generic behaviour below.

export const KEYMAP = Object.freeze([
  {keys: '↓ / ↑', action: 'next / previous source in the filtered list'},
  {keys: 'Esc', action: 'cancel brush, close lightbox or pop-out'},
  {keys: '?', action: 'help'},
  {keys: 'space', action: 'play / pause tour'},
  {keys: 'x', action: 'clear selection'},
  {keys: 'i', action: 'invert selection (within drawable points)'},
  {keys: 'k', action: 'keep only the selection (filter)'},
  {keys: 'Ctrl/⌘ Z', action: 'undo selection'},
  {keys: 'Ctrl/⌘ Shift Z', action: 'redo selection'},
]);

/** Cap for "keep only": ids are resolved one by one through the DataClient. */
export const KEEP_ONLY_MAX = 5000;

function isTyping(target) {
  if (!target || !target.tagName) return false;
  const tag = target.tagName.toLowerCase();
  if (tag === 'textarea' || tag === 'select') return true;
  if (tag === 'input') {
    const type = (target.type || 'text').toLowerCase();
    return !['button', 'checkbox', 'radio', 'range', 'submit', 'reset', 'color', 'file'].includes(type);
  }
  return !!target.isContentEditable;
}

/** Active dataset for selection keys: the focused source's dataset, else the primary one. */
function activeDs(app) {
  const f = app.store.get('focus');
  if (f && f.ds && app.selections.has(f.ds)) return f.ds;
  return app.primaryDs;
}

function toast(app, level, text) {
  app.store.emit('toast', {level, text});
}

async function keepOnly(app, ds) {
  const sel = app.selections.get(ds);
  if (!sel || sel.count === 0) {
    toast(app, 'info', 'Nothing selected');
    return;
  }
  if (sel.count > KEEP_ONLY_MAX) {
    toast(app, 'warn', `Keep only is limited to ${KEEP_ONLY_MAX.toLocaleString('en-US')} rows (selection has ${sel.count.toLocaleString('en-US')})`);
    return;
  }
  const client = await app.hub.catalog(ds);
  const idx = Array.from(sel.indices());
  const ids = await Promise.all(idx.map((i) => client.id(i)));
  const prev = app.store.get(`filters:${ds}`) || {};
  app.store.set({[`filters:${ds}`]: Object.assign({}, prev, {ids})}, {source: 'keys', history: true});
  toast(app, 'info', `Showing ${ids.length.toLocaleString('en-US')} kept rows`);
}

/**
 * Bind global keys. Returns an unbind function.
 * @param {object} app  App from boot (store, selections, hub, layout, primaryDs)
 * @param {{target?: EventTarget}} [opts]
 */
export function bindKeys(app, opts = {}) {
  const target = opts.target || (typeof document !== 'undefined' ? document : null);
  if (!target) return () => {};
  const store = app.store;

  function offer(topic, payload) {
    const p = Object.assign({handled: false}, payload);
    store.emit(topic, p);
    return p.handled;
  }

  // Fallback step whose id is still being looked up; repeated keys continue from it.
  let pendingStep = null;

  function step(dir, ev) {
    const ds = activeDs(app);
    if (offer('nav:step', {dir, ds, event: ev})) return true;
    // Fallback: walk stored order (panes that own a sorted/filtered list handle 'nav:step').
    const f = store.get('focus');
    const sel = app.selections.get(ds);
    const n = sel ? sel.n : 0;
    if (!n) return false;
    const base = pendingStep && pendingStep.ds === ds ? pendingStep.i
      : (f && f.ds === ds && Number.isInteger(f.i) ? f.i : null);
    const i0 = base !== null ? base : (dir > 0 ? -1 : n);
    const i = Math.max(0, Math.min(n - 1, i0 + dir));
    if (base === i) return true;
    // Set focus once with both i and id, so the hash gets one history entry per step.
    const target = {ds, i};
    pendingStep = target;
    const commit = (id) => {
      if (pendingStep !== target) return;   // superseded by a later key press
      pendingStep = null;
      store.set({focus: {ds, i, id: id === undefined ? null : id}}, {source: 'keys'});
    };
    if (typeof app.idOf === 'function') {
      Promise.resolve(app.idOf(ds, i)).then(commit, (err) => {
        console.warn('keys: id lookup failed; focusing by index', err);
        commit(null);
      });
    } else {
      commit(null);
    }
    return true;
  }

  function onKey(ev) {
    if (ev.defaultPrevented) return;
    const key = ev.key;
    const mod = ev.ctrlKey || ev.metaKey;
    if (key === 'Escape') {
      if (!offer('keys:escape', {event: ev})) {
        if (store.get('lightbox')) store.set({lightbox: null}, {source: 'keys'});
        else {
          const lay = store.get('layout');
          if (lay && lay.popout && Object.keys(lay.popout).length) {
            store.set({layout: Object.assign({}, lay, {popout: {}})}, {source: 'keys'});
          } else if (store.get('hover')) {
            store.set({hover: null}, {source: 'keys'});
          }
        }
      }
      return;
    }
    if (isTyping(ev.target)) return;
    if (mod && !ev.altKey && (key === 'z' || key === 'Z' || key === 'y' || key === 'Y')) {
      const ds = activeDs(app);
      const sel = app.selections.get(ds);
      if (!sel) return;
      const redo = key === 'y' || key === 'Y' || ev.shiftKey;
      const ok = redo ? sel.redo() : sel.undo();
      if (!ok) toast(app, 'info', redo ? 'Nothing to redo' : 'Nothing to undo');
      ev.preventDefault();
      return;
    }
    if (mod || ev.altKey) return;
    switch (key) {
      case 'ArrowDown':
      case 'ArrowRight':
      case 'j':
      case 'n':
        if (step(+1, ev)) ev.preventDefault();
        break;
      case 'ArrowUp':
      case 'ArrowLeft':
      case 'p':
        if (step(-1, ev)) ev.preventDefault();
        break;
      case '?':
        offer('help:toggle', {event: ev});
        ev.preventDefault();
        break;
      case ' ':
      case 'Spacebar':
        if (offer('tour:toggle', {event: ev})) ev.preventDefault();
        break;
      case 'x': {
        const sel = app.selections.get(activeDs(app));
        if (sel && sel.count) sel.clear();
        break;
      }
      case 'i': {
        const ds = activeDs(app);
        if (!offer('selection:invert', {ds, event: ev})) {
          const sel = app.selections.get(ds);
          if (sel) sel.invert();
        }
        break;
      }
      case 'k':
        keepOnly(app, activeDs(app)).catch((err) => toast(app, 'error', `Keep only failed: ${err.message}`));
        break;
      default:
        break;
    }
  }

  // Arrow keys over a focused splitter or slider belong to that control.
  const handler = (ev) => {
    const t = ev.target;
    if (t && t.closest && t.closest('[role="separator"],[role="slider"],[role="listbox"],[role="tablist"],[role="grid"]') && ev.key && ev.key.startsWith('Arrow')) return;
    onKey(ev);
  };
  target.addEventListener('keydown', handler);
  return () => target.removeEventListener('keydown', handler);
}
