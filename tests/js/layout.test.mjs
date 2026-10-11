// Unit tests for the layout's pane state (IMPLEMENTATION_NOTES 112): section[data-state] stays
// 'loading' until mount() resolves and the handle's `firstPaint` promise settles (bounded by
// LAYOUT_TIMING.firstPaintMaxWaitMs); a rejection marks 'error'. Minimal fake DOM (no jsdom).
import test from 'node:test';
import assert from 'node:assert/strict';

import {firstPaintLatch, LAYOUT_TIMING, mountLayout, PANE_FIRST_PAINT_MAX_WAIT_MS} from '../../web/js/layout/layout.js';
import {createTestHooks} from '../../web/js/core/testhooks.js';

function tick(ms = 0) {
  return new Promise((r) => setTimeout(r, ms));
}

async function settle(n = 6) {
  for (let k = 0; k < n; k++) await tick();
}

class FakeEl {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.attrs = {};
    this.dataset = {};
    this.listeners = {};
    this.className = '';
    this._text = '';
    this.parent = null;
    this.title = '';
    this.style = {setProperty(k, v) { this[k] = v; }, removeProperty(k) { delete this[k]; }};
    const self = this;
    this.classList = {
      add(...cs) { for (const c of cs) this.toggle(c, true); },
      remove(...cs) { for (const c of cs) this.toggle(c, false); },
      toggle(c, on) {
        const s = new Set(self.className.split(/\s+/).filter(Boolean));
        if (on === undefined ? !s.has(c) : on) s.add(c); else s.delete(c);
        self.className = Array.from(s).join(' ');
      },
      contains(c) { return self.className.split(/\s+/).includes(c); },
    };
  }
  set textContent(t) { this.children = []; this._text = String(t); }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
  get hidden() { return Object.prototype.hasOwnProperty.call(this.attrs, 'hidden'); }
  set hidden(v) { if (v) this.attrs.hidden = ''; else delete this.attrs.hidden; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k); }
  append(...cs) {
    for (const c of cs) {
      if (!c || typeof c !== 'object') continue;
      if (c.parent) c.remove();
      c.parent = this;
      this.children.push(c);
    }
  }
  before(c) { if (this.parent) { const p = this.parent; c.parent = p; p.children.splice(p.children.indexOf(this), 0, c); } }
  replaceWith(c) {
    const p = this.parent;
    if (!p) return;
    const k = p.children.indexOf(this);
    if (c.parent) c.remove();
    c.parent = p;
    p.children.splice(k, 1, c);
    this.parent = null;
  }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); this.parent = null; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  removeEventListener() {}
  focus() {}
  scrollIntoView() {}
  contains(e) { return e === this || this.all().includes(e); }
  all() { return this.children.flatMap((c) => [c, ...c.all()]); }
  getBoundingClientRect() { return {width: 200, height: 100, left: 0, top: 0}; }
  querySelector(sel) {
    const m = /^(?::scope > )?\.([\w-]+)$/.exec(sel);
    if (!m) return null;
    const pool = sel.startsWith(':scope') ? this.children : this.all();
    return pool.find((e) => e.classList.contains(m[1])) || null;
  }
  querySelectorAll() { return []; }
}

async function withDom(fn) {
  const saved = {};
  for (const k of ['document', 'window', 'requestAnimationFrame', 'cancelAnimationFrame']) saved[k] = globalThis[k];
  const body = new FakeEl('body');
  globalThis.document = {createElement: (t) => new FakeEl(t), body, addEventListener() {}, removeEventListener() {}, activeElement: null};
  globalThis.window = {devicePixelRatio: 1, addEventListener() {}};
  globalThis.requestAnimationFrame = (f) => setTimeout(f, 0);
  globalThis.cancelAnimationFrame = (h) => clearTimeout(h);
  try {
    return await fn();
  } finally {
    for (const [k, v] of Object.entries(saved)) {
      if (v === undefined) delete globalThis[k];
      else globalThis[k] = v;
    }
  }
}

/** Layout of the given panes in one row; `handles[type]` is what that pane's mount() returns. */
function harness(handles, opts = {}) {
  const registry = {
    load: async (type) => ({
      async mount(host) {
        if (opts.note) host.append(Object.assign(new FakeEl('div'), {className: 'pk-error-note'}));
        return handles[type];
      },
    }),
  };
  const root = new FakeEl('div');
  const tree = {row: Object.keys(handles).map((t) => ({pane: t}))};
  const lay = mountLayout(root, tree, {app: {page: {}}, registry});
  const state = (id) => lay.info(id).el.dataset.state;
  return {lay, state};
}

test('firstPaintLatch: resolves or rejects once; later calls ignored', async () => {
  const a = firstPaintLatch();
  assert.equal(a.done, false);
  a.resolve();
  a.reject(new Error('late'));
  assert.equal(a.done, true);
  await a.promise;
  const b = firstPaintLatch();
  b.reject(new Error('boom'));
  b.resolve();
  await assert.rejects(b.promise, /boom/);
  // an unobserved rejection is handled
  firstPaintLatch().reject(new Error('quiet'));
  await tick();
  assert.equal(PANE_FIRST_PAINT_MAX_WAIT_MS, 30000);
});

test('layout: a handle without firstPaint is ready at mount', () => withDom(async () => {
  const {lay, state} = harness({plain: {resize() {}, destroy() {}}});
  assert.equal(state('plain'), 'loading');
  await lay.whenMounted('plain');
  await settle();
  assert.equal(state('plain'), 'ready');
  lay.destroy();
}));

test('layout: data-state stays loading until firstPaint resolves', () => withDom(async () => {
  const fp = firstPaintLatch();
  const {lay, state} = harness({list: {firstPaint: fp.promise, resize() {}, destroy() {}}});
  const handle = await lay.whenMounted('list');
  assert.equal(handle.firstPaint, fp.promise, 'whenMounted resolves at mount');
  await settle();
  assert.equal(state('list'), 'loading');
  fp.resolve();
  await settle();
  assert.equal(state('list'), 'ready');
  lay.destroy();
}));

test('layout: a firstPaint rejection marks the pane error and keeps its body', () => withDom(async () => {
  const fp = firstPaintLatch();
  const warn = console.warn;
  const warned = [];
  console.warn = (...a) => warned.push(a.join(' '));
  try {
    const {lay, state} = harness({table: {firstPaint: fp.promise, resize() {}, destroy() {}}}, {note: true});
    await lay.whenMounted('table');
    fp.reject(new Error('rows failed'));
    await settle();
    assert.equal(state('table'), 'error');
    assert.ok(lay.info('table').body.querySelector('.pk-error-note'), "the pane's own note stays");
    assert.ok(warned.some((w) => /first paint failed/.test(w)));
    lay.destroy();
  } finally {
    console.warn = warn;
  }
}));

test('layout: the first-paint guard marks the pane ready with a warning', () => withDom(async () => {
  const saved = LAYOUT_TIMING.firstPaintMaxWaitMs;
  LAYOUT_TIMING.firstPaintMaxWaitMs = 20;
  const warn = console.warn;
  const warned = [];
  console.warn = (...a) => warned.push(a.join(' '));
  try {
    const never = new Promise(() => {});
    const {lay, state} = harness({scatter: {firstPaint: never, resize() {}, destroy() {}}});
    await lay.whenMounted('scatter');
    await settle();
    assert.equal(state('scatter'), 'loading');
    await tick(40);
    assert.equal(state('scatter'), 'ready');
    assert.ok(warned.some((w) => /no first paint 20 ms after mount/.test(w)));
    lay.destroy();
  } finally {
    LAYOUT_TIMING.firstPaintMaxWaitMs = saved;
    console.warn = warn;
  }
}));

test('layout: panes settle independently; destroy before first paint leaves the state alone', () => withDom(async () => {
  const a = firstPaintLatch();
  const b = firstPaintLatch();
  const {lay, state} = harness({
    list: {firstPaint: a.promise, resize() {}, destroy() {}},
    hist: {firstPaint: b.promise, resize() {}, destroy() {}},
  });
  await lay.whenMounted('list');
  await lay.whenMounted('hist');
  b.resolve();
  await settle();
  assert.equal(state('hist'), 'ready');
  assert.equal(state('list'), 'loading');
  const el = lay.info('list').el;
  lay.destroy();
  a.resolve();
  await settle();
  assert.equal(el.dataset.state, 'loading');
}));

test('layout: a pane folded before first paint is paused, not loading; loading again when shown', () => withDom(async () => {
  const fp = firstPaintLatch();
  const visible = [];
  const {lay, state} = harness({
    list: {firstPaint: fp.promise, resize() {}, destroy() {}, onVisible(v) { visible.push(v); }},
    hist: {resize() {}, destroy() {}},
  });
  await lay.whenMounted('list');
  await settle();
  assert.equal(state('list'), 'loading');
  lay.collapse('list', true);
  assert.equal(state('list'), 'paused', 'a folded pane does not hold the page first paint');
  assert.equal(visible.at(-1), false);
  lay.collapse('list', false);
  assert.equal(state('list'), 'loading', 'shown again before its first paint');
  lay.collapse('list', true);
  fp.resolve();
  await settle();
  assert.equal(state('list'), 'ready');
  lay.collapse('list', false);
  lay.collapse('list', true);
  assert.equal(state('list'), 'ready', 'state is left alone once the first paint settled');
  lay.destroy();
}));

test('layout: a pane folded while mounting starts paused', () => withDom(async () => {
  const fp = firstPaintLatch();
  const {lay, state} = harness({list: {firstPaint: fp.promise, resize() {}, destroy() {}}});
  assert.equal(state('list'), 'loading');
  lay.collapse('list', true);
  assert.equal(state('list'), 'paused');
  await lay.whenMounted('list');
  await settle();
  assert.equal(state('list'), 'paused');
  fp.reject(new Error('x'));
  const warn = console.warn;
  console.warn = () => {};
  try {
    await settle();
  } finally {
    console.warn = warn;
  }
  assert.equal(state('list'), 'error');
  lay.destroy();
}));

async function withTestWindow(fn) {
  const saved = {window: globalThis.window, location: globalThis.location, error: console.error};
  globalThis.window = {addEventListener() {}};
  globalThis.location = {search: '?pktest=1'};
  try {
    return await fn();
  } finally {
    console.error = saved.error;
    for (const k of ['window', 'location']) {
      if (saved[k] === undefined) delete globalThis[k];
      else globalThis[k] = saved[k];
    }
  }
}

test('testhooks: __pk.firstPaintAt() rejects with the boot error before or after attach', () => withTestWindow(async () => {
  const early = createTestHooks();
  const p = early.pk.firstPaintAt();
  early.reject(new Error('boot failed'));
  await assert.rejects(p, /boot failed/);
  await assert.rejects(early.pk.ready, /boot failed/);

  const late = createTestHooks();
  late.attach({store: {get() {}}, hub: null, caps: new Set(), afterFirstPaint: () => new Promise(() => {}),
    firstPaintAt: () => new Promise(() => {})});
  const q = late.pk.firstPaintAt();
  late.reject(new Error('late failure'));
  await assert.rejects(q, /late failure/);

  const ok = createTestHooks();
  const r = ok.pk.firstPaintAt();
  ok.attach({store: {get() {}}, hub: null, caps: new Set(), afterFirstPaint: () => Promise.resolve(),
    firstPaintAt: () => Promise.resolve(1234)});
  assert.equal(await r, 1234);
}));
