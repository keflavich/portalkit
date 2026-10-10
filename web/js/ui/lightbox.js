// Lightbox (capability lightbox, ARCHITECTURE.md section 8.2).
//
// Store `lightbox` = {set, index} | null (hash `lb=set,index`). Panes register image sets with
// registerSet(id, provider); a provider describes the *filtered* list the pane shows, so
// previous / next walk exactly what the viewer sees. Closing sets `lightbox` to null, which
// removes `lb=` from the hash. Arrows, Home/End, Esc, swipe (left/right to step, down to close),
// backdrop click and captions are supported. A `lightbox` value restored from the hash before
// its pane has registered the set waits for the registration.

const sets = new Map();          // set id -> provider
const setListeners = new Set();  // fn(setId)

/**
 * Register an image set. provider: {
 *   count() -> number,
 *   item(index) -> {src, caption?, title?, alt?, href?} | null,
 *   onIndex?(index)            // the lightbox moved to `index` (pane may scroll to it)
 *   label?: string             // e.g. "Gallery"
 *   ready?() -> boolean        // false while the pane is still loading; the lightbox waits
 *   encode?(pos) -> number     // stored `lightbox.index` for display position `pos`
 *   decode?(index) -> number   // display position of a stored index (-1 when not shown)
 * }
 * Without encode/decode the stored index is the display position. A pane whose display order
 * depends on local state (sort, search) encodes a stable item index so shared links open the
 * same image for every viewer.
 * Returns an unregister function. Re-registering an id replaces the provider and refreshes an
 * open lightbox on that set (call it again, or refreshSet(id), when the filtered list changes).
 */
export function registerSet(id, provider) {
  const key = String(id);
  sets.set(key, provider);
  for (const fn of Array.from(setListeners)) fn(key);
  return () => {
    if (sets.get(key) === provider) {
      sets.delete(key);
      for (const fn of Array.from(setListeners)) fn(key);
    }
  };
}

/** Ask an open lightbox on `id` to re-read its provider (count, items). */
export function refreshSet(id) {
  const key = String(id);
  for (const fn of Array.from(setListeners)) fn(key);
}

/** Open the lightbox on `set` at `index` (writes store `lightbox`). */
export function openLightbox(store, set, index, source = 'lightbox') {
  store.set({lightbox: {set: set === null || set === undefined ? null : String(set), index: Math.max(0, index | 0)}}, {source, history: true});
}

/** Index after a step of `dir` in a list of `count` (wraps around); -1 for an empty list. */
export function stepIndex(index, dir, count) {
  if (!(count > 0)) return -1;
  const i = Number.isFinite(index) ? Math.trunc(index) : 0;
  return (((i + dir) % count) + count) % count;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

export function install(app) {
  const store = app.store;
  const root = el('div', 'pk-lightbox');
  root.setAttribute('role', 'dialog');
  root.setAttribute('aria-modal', 'true');
  root.setAttribute('aria-label', 'Image viewer');
  root.hidden = true;
  const stage = el('div', 'pk-lb-stage');
  const img = el('img', 'pk-lb-img');
  img.decoding = 'async';
  img.draggable = false;
  const spinner = el('div', 'pk-lb-loading', 'Loading…');
  const missing = el('div', 'pk-lb-missing', 'This image could not be loaded.');
  missing.hidden = true;
  stage.append(img, spinner, missing);
  const bar = el('div', 'pk-lb-bar');
  const counter = el('span', 'pk-lb-counter');
  const cap = el('div', 'pk-lb-caption');
  const orig = el('a', 'pk-lb-orig', 'Open image');
  orig.target = '_blank';
  orig.rel = 'noopener';
  bar.append(counter, cap, orig);
  const prev = el('button', 'pk-lb-btn pk-lb-prev', '‹');
  prev.type = 'button';
  prev.setAttribute('aria-label', 'Previous image');
  const next = el('button', 'pk-lb-btn pk-lb-next', '›');
  next.type = 'button';
  next.setAttribute('aria-label', 'Next image');
  const close = el('button', 'pk-lb-btn pk-lb-close', '×');
  close.type = 'button';
  close.setAttribute('aria-label', 'Close (Esc)');
  root.append(stage, bar, prev, next, close);
  document.body.append(root);

  let state = null;        // {set, index, count}
  let returnFocus = null;
  const preload = [];

  function currentSetId(lb) {
    if (lb.set !== null && lb.set !== undefined) return String(lb.set);
    // `lb=<index>` (set null): prefer a gallery set, then any set with images, then the first.
    let first = null, nonEmpty = null;
    for (const [id, pr] of sets) {
      if (first === null) first = id;
      const ready = typeof pr.ready !== 'function' || pr.ready();
      const n = ready ? Math.max(0, pr.count() | 0) : 0;
      if (pr.label === 'Gallery' && (n > 0 || !ready)) return id;
      if (nonEmpty === null && n > 0) nonEmpty = id;
    }
    return nonEmpty !== null ? nonEmpty : first;
  }

  function encodePos(provider, pos) {
    return provider && typeof provider.encode === 'function' ? provider.encode(pos) : pos;
  }

  function hide() {
    if (root.hidden) return;
    root.hidden = true;
    document.documentElement.classList.remove('pk-lb-open');
    img.removeAttribute('src');
    state = null;
    if (returnFocus && typeof returnFocus.focus === 'function' && document.contains(returnFocus)) returnFocus.focus();
    returnFocus = null;
  }

  function closeLightbox() {
    if (store.get('lightbox')) store.set({lightbox: null}, {source: 'lightbox', history: true});
    else hide();
  }

  function go(pos) {
    if (!state) return;
    const set = state.set;
    const index = encodePos(sets.get(set), pos);
    if (!Number.isInteger(index) || index < 0) return;
    store.set({lightbox: {set, index}}, {source: 'lightbox'});
  }

  function step(dir) {
    if (!state) return;
    const i = stepIndex(state.index, dir, state.count);
    if (i >= 0) go(i);
  }

  function render() {
    const lb = store.get('lightbox');
    if (!lb || !Number.isFinite(lb.index)) {
      hide();
      return;
    }
    const setId = currentSetId(lb);
    const provider = setId !== null ? sets.get(setId) : null;
    if (!provider) {
      // Wait for the pane that owns this set to register it.
      hide();
      return;
    }
    if (typeof provider.ready === 'function' && !provider.ready()) {
      // The pane is still loading its items; it calls refreshSet() when they arrive.
      hide();
      return;
    }
    const count = Math.max(0, provider.count() | 0);
    if (!count) {
      store.set({lightbox: null}, {source: 'lightbox'});
      return;
    }
    const stored = Math.trunc(lb.index);
    let index = typeof provider.decode === 'function' ? provider.decode(stored) : stored;
    if (!Number.isInteger(index) || index < 0 || index >= count) {
      index = Number.isInteger(index) ? Math.max(0, Math.min(count - 1, index)) : 0;
      const enc = encodePos(provider, index);
      if (Number.isInteger(enc) && enc >= 0 && (enc !== stored || String(lb.set) !== setId)) {
        store.set({lightbox: {set: setId, index: enc}}, {source: 'lightbox'});
        return;
      }
    }
    const item = provider.item(index);
    if (!item || !item.src) {
      store.emit('toast', {level: 'warn', text: 'This image is not available.'});
      store.set({lightbox: null}, {source: 'lightbox'});
      return;
    }
    const opening = root.hidden;
    state = {set: setId, index, count};
    if (opening) {
      returnFocus = document.activeElement;
      root.hidden = false;
      document.documentElement.classList.add('pk-lb-open');
    }
    if (img.getAttribute('src') !== item.src) {
      spinner.hidden = false;
      missing.hidden = true;
      img.classList.add('pk-lb-pending');
      img.src = item.src;
    }
    img.alt = item.alt || item.caption || item.title || '';
    counter.textContent = `${index + 1} / ${count}`;
    cap.textContent = '';
    if (item.title) cap.append(el('strong', 'pk-lb-title', item.title));
    if (item.caption) cap.append(el('span', 'pk-lb-text', item.caption));
    orig.href = item.href || item.src;
    prev.disabled = next.disabled = count < 2;
    // Preload the neighbours.
    preload.length = 0;
    for (const d of [1, -1]) {
      const j = stepIndex(index, d, count);
      if (j === index) continue;
      const it = provider.item(j);
      if (it && it.src) {
        const p = new Image();
        p.decoding = 'async';
        p.src = it.src;
        preload.push(p);
      }
    }
    if (typeof provider.onIndex === 'function') provider.onIndex(index);
    if (opening) close.focus();
  }

  img.addEventListener('load', () => {
    spinner.hidden = true;
    img.classList.remove('pk-lb-pending');
  });
  img.addEventListener('error', () => {
    if (!img.getAttribute('src')) return;
    spinner.hidden = true;
    missing.hidden = false;
    img.classList.remove('pk-lb-pending');
  });

  prev.addEventListener('click', (ev) => { ev.stopPropagation(); step(-1); });
  next.addEventListener('click', (ev) => { ev.stopPropagation(); step(1); });
  close.addEventListener('click', (ev) => { ev.stopPropagation(); closeLightbox(); });
  root.addEventListener('click', (ev) => {
    if (ev.target === root || ev.target === stage) closeLightbox();
  });

  // Keys while open: handled in the capture phase so the global list stepping does not run.
  window.addEventListener('keydown', (ev) => {
    if (root.hidden) return;
    let handled = true;
    switch (ev.key) {
      case 'ArrowLeft': case 'ArrowUp': case 'p': case 'PageUp': step(-1); break;
      case 'ArrowRight': case 'ArrowDown': case 'n': case 'j': case 'PageDown': case ' ': step(1); break;
      case 'Home': go(0); break;
      case 'End': if (state) go(state.count - 1); break;
      case 'Escape': closeLightbox(); break;
      case 'Tab': {
        // Keep focus inside the dialog.
        const f = [prev, next, orig, close].filter((b) => !b.disabled && !b.hidden);
        const k = f.indexOf(document.activeElement);
        const j = ev.shiftKey ? (k <= 0 ? f.length - 1 : k - 1) : (k + 1) % f.length;
        if (f[j]) f[j].focus();
        break;
      }
      default: handled = false;
    }
    if (handled) {
      ev.preventDefault();
      ev.stopPropagation();
    }
  }, true);

  store.onEvent('keys:escape', (p) => {
    if (!p || p.handled || root.hidden) return;
    p.handled = true;
    closeLightbox();
  });

  // Swipe: horizontal steps, downward closes.
  let sw = null;
  stage.addEventListener('pointerdown', (ev) => {
    if (ev.button !== 0) return;
    sw = {x: ev.clientX, y: ev.clientY, t: Date.now(), id: ev.pointerId};
  });
  stage.addEventListener('pointerup', (ev) => {
    if (!sw || ev.pointerId !== sw.id) return;
    const dx = ev.clientX - sw.x, dy = ev.clientY - sw.y;
    const quick = Date.now() - sw.t < 800;
    sw = null;
    if (Math.abs(dx) > 50 && Math.abs(dx) > 1.5 * Math.abs(dy) && quick) step(dx < 0 ? 1 : -1);
    else if (dy > 80 && dy > 1.5 * Math.abs(dx) && quick) closeLightbox();
  });
  stage.addEventListener('pointercancel', () => { sw = null; });

  setListeners.add((id) => {
    const lb = store.get('lightbox');
    if (!lb) return;
    const cur = currentSetId(lb);
    if (cur === id || cur === null) render();
  });
  store.on(['lightbox'], render);
  render();
  return {render, close: closeLightbox};
}
