// Page store (ARCHITECTURE.md section 6.2). Pure module: no DOM access.
//
// - set(patch, {source, history}) shallow-merges per key. A key whose new value is
//   Object.is-equal, or (plain object / array) shallow-equal, to the current value is ignored.
// - on(keys, fn, {sync}) subscribes. Coalesced listeners (default) are called at most once
//   per animation frame (setTimeout(0) when requestAnimationFrame is absent, e.g. node) with
//   the union of their subscribed changed keys and sources since their last call. Sync listeners are called
//   at the end of every set() (or of the outermost batch()).
// - keys may be '*', an array of exact keys, or prefix patterns ending in '*' ("sel:*").
// - Listener signature: fn(changed: Set<string>, store, sources: Set<string>, info)
//   where info = {history: boolean} (true when any contributing set passed history:true).
// - emit/onEvent carry transient, synchronous events.
//
// Listener exceptions never break delivery to other listeners: each is re-thrown on a
// microtask so it reaches window.onerror / the test hooks' error log.

function isPlainObject(v) {
  if (v === null || typeof v !== 'object') return false;
  const p = Object.getPrototypeOf(v);
  return p === Object.prototype || p === null;
}

/** Object.is, or shallow equality for two plain objects or two plain arrays. */
export function shallowEqual(a, b) {
  if (Object.is(a, b)) return true;
  if (Array.isArray(a) && Array.isArray(b)) {
    if (a.length !== b.length) return false;
    for (let i = 0; i < a.length; i++) if (!Object.is(a[i], b[i])) return false;
    return true;
  }
  if (isPlainObject(a) && isPlainObject(b)) {
    const ka = Object.keys(a);
    const kb = Object.keys(b);
    if (ka.length !== kb.length) return false;
    for (const k of ka) {
      if (!Object.prototype.hasOwnProperty.call(b, k)) return false;
      if (!Object.is(a[k], b[k])) return false;
    }
    return true;
  }
  return false;
}

function rethrowLater(err) {
  const q = typeof queueMicrotask === 'function' ? queueMicrotask : (f) => Promise.resolve().then(f);
  q(() => { throw err; });
}

function defaultScheduler(fn) {
  if (typeof requestAnimationFrame === 'function') {
    // A hidden tab pauses rAF; the timeout keeps the hash and tests moving.
    let done = false;
    const run = () => { if (!done) { done = true; fn(); } };
    requestAnimationFrame(run);
    setTimeout(run, 100);
    return;
  }
  setTimeout(fn, 0);
}

function makeMatcher(keys) {
  if (keys === '*' || keys === undefined || keys === null) return () => true;
  const list = typeof keys === 'string' ? [keys] : Array.from(keys);
  if (list.includes('*')) return () => true;
  const exact = new Set();
  const prefixes = [];
  for (const k of list) {
    if (typeof k === 'string' && k.endsWith('*')) prefixes.push(k.slice(0, -1));
    else exact.add(k);
  }
  return (key) => {
    if (exact.has(key)) return true;
    for (const p of prefixes) if (key.startsWith(p)) return true;
    return false;
  };
}

/** The subset of `changed` a listener subscribed to. */
function matching(changed, match) {
  const out = new Set();
  for (const k of changed) if (match(k)) out.add(k);
  return out;
}

/**
 * createStore(initial, opts?)
 *   opts.schedule(fn): custom scheduler for coalesced delivery (tests); default rAF / setTimeout(0)
 */
export function createStore(initial = {}, opts = {}) {
  const state = Object.assign({}, initial);
  const schedule = opts.schedule || defaultScheduler;
  const listeners = new Set();          // {match, fn, sync, pending:Set, sources:Set, history}
  const events = new Map();             // topic -> Set<fn>
  let batchDepth = 0;
  let batchChanged = new Set();
  let batchSources = new Set();
  let batchHistory = false;
  let scheduled = false;
  let snap = null;

  const store = {
    get(key) {
      return state[key];
    },

    has(key) {
      return Object.prototype.hasOwnProperty.call(state, key);
    },

    keys() {
      return Object.keys(state);
    },

    snapshot() {
      if (!snap) snap = Object.freeze(Object.assign({}, state));
      return snap;
    },

    set(patch, setOpts = {}) {
      if (!patch || typeof patch !== 'object') return;
      const source = setOpts.source || 'unknown';
      const changed = [];
      for (const key of Object.keys(patch)) {
        const v = patch[key];
        if (Object.prototype.hasOwnProperty.call(state, key) && shallowEqual(state[key], v)) continue;
        if (!Object.prototype.hasOwnProperty.call(state, key) && v === undefined) continue;
        state[key] = v;
        changed.push(key);
      }
      if (!changed.length) return;
      snap = null;
      for (const k of changed) batchChanged.add(k);
      batchSources.add(source);
      if (setOpts.history) batchHistory = true;
      if (batchDepth === 0) commit();
    },

    on(keys, fn, onOpts = {}) {
      const l = {
        match: makeMatcher(keys), fn, sync: !!onOpts.sync,
        pending: new Set(), sources: new Set(), history: false,
      };
      listeners.add(l);
      return () => { listeners.delete(l); };
    },

    emit(topic, payload) {
      const set = events.get(topic);
      const all = events.get('*');
      if (set) {
        for (const fn of Array.from(set)) {
          try { fn(payload, topic); } catch (err) { rethrowLater(err); }
        }
      }
      if (all) {
        for (const fn of Array.from(all)) {
          try { fn(payload, topic); } catch (err) { rethrowLater(err); }
        }
      }
    },

    onEvent(topic, fn) {
      if (!events.has(topic)) events.set(topic, new Set());
      events.get(topic).add(fn);
      return () => {
        const s = events.get(topic);
        if (s) s.delete(fn);
      };
    },

    batch(fn) {
      batchDepth += 1;
      try {
        fn();
      } finally {
        batchDepth -= 1;
        if (batchDepth === 0 && batchChanged.size) commit();
      }
    },

    /** Deliver pending coalesced notifications now (tests, before navigation). */
    flush() {
      deliverCoalesced();
    },
  };

  function commit() {
    const changed = batchChanged;
    const sources = batchSources;
    const history = batchHistory;
    batchChanged = new Set();
    batchSources = new Set();
    batchHistory = false;
    let needSchedule = false;
    for (const l of Array.from(listeners)) {
      if (!listeners.has(l)) continue;
      const mine = matching(changed, l.match);
      if (!mine.size) continue;
      if (l.sync) {
        try { l.fn(mine, store, new Set(sources), {history}); } catch (err) { rethrowLater(err); }
      } else {
        for (const k of mine) l.pending.add(k);
        for (const s of sources) l.sources.add(s);
        if (history) l.history = true;
        needSchedule = true;
      }
    }
    if (needSchedule && !scheduled) {
      scheduled = true;
      schedule(deliverCoalesced);
    }
  }

  function deliverCoalesced() {
    scheduled = false;
    for (const l of Array.from(listeners)) {
      if (l.sync || !l.pending.size || !listeners.has(l)) continue;
      const changed = l.pending;
      const sources = l.sources;
      const history = l.history;
      l.pending = new Set();
      l.sources = new Set();
      l.history = false;
      try { l.fn(changed, store, sources, {history}); } catch (err) { rethrowLater(err); }
    }
  }

  return store;
}
