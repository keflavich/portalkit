// node --test tests/js/store.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import {createStore, shallowEqual} from '../../web/js/core/store.js';

/** Manual scheduler: coalesced deliveries run only when tick() is called. */
function manual() {
  const q = [];
  return {schedule: (fn) => q.push(fn), tick: () => { while (q.length) q.shift()(); }, pending: () => q.length};
}

test('shallowEqual: primitives, NaN, -0, arrays, plain objects, class instances', () => {
  assert.ok(shallowEqual(1, 1));
  assert.ok(shallowEqual(NaN, NaN));
  assert.ok(!shallowEqual(0, -0));
  assert.ok(shallowEqual([1, 'a'], [1, 'a']));
  assert.ok(!shallowEqual([1, 2], [1, 2, 3]));
  assert.ok(shallowEqual({a: 1, b: 'x'}, {b: 'x', a: 1}));
  assert.ok(!shallowEqual({a: 1}, {a: 1, b: undefined}));
  assert.ok(!shallowEqual({a: {x: 1}}, {a: {x: 1}}), 'nested objects compare by identity');
  assert.ok(!shallowEqual(new Uint8Array([1]), new Uint8Array([1])), 'typed arrays compare by identity');
  assert.ok(!shallowEqual([1], {0: 1, length: 1}));
  assert.ok(shallowEqual(null, null));
  assert.ok(!shallowEqual(null, {}));
});

test('get/has/keys/snapshot and equality short-circuit', () => {
  const m = manual();
  const s = createStore({a: 1, o: {x: 1}}, {schedule: m.schedule});
  let calls = 0;
  s.on('*', () => { calls++; });
  assert.equal(s.get('a'), 1);
  assert.ok(s.has('o'));
  assert.ok(!s.has('zz'));
  assert.deepEqual(s.keys().sort(), ['a', 'o']);
  const snap1 = s.snapshot();
  assert.ok(Object.isFrozen(snap1));
  assert.equal(s.snapshot(), snap1, 'snapshot cached while unchanged');
  s.set({a: 1, o: {x: 1}});          // equal values: no change
  assert.equal(m.pending(), 0);
  assert.equal(s.snapshot(), snap1);
  s.set({zz: undefined});            // absent key set to undefined is ignored
  assert.ok(!s.has('zz'));
  assert.equal(m.pending(), 0);
  s.set({o: {x: 2}});
  assert.notEqual(s.snapshot(), snap1);
  assert.equal(snap1.o.x, 1, 'old snapshot unchanged');
  m.tick();
  assert.equal(calls, 1);
  s.set(null);
  s.set('nope');
  m.tick();
  assert.equal(calls, 1);
});

test('coalesced listeners get one call with the union of keys and sources', () => {
  const m = manual();
  const s = createStore({a: 0, b: 0, c: 0}, {schedule: m.schedule});
  const got = [];
  s.on(['a', 'b'], (changed, st, sources, info) => got.push({changed: [...changed].sort(), sources: [...sources].sort(), info, a: st.get('a')}));
  s.set({a: 1}, {source: 'p1'});
  s.set({b: 1}, {source: 'p2'});
  s.set({a: 2}, {source: 'p1'});
  s.set({c: 5}, {source: 'p3'});
  assert.equal(got.length, 0, 'not delivered before the frame');
  assert.equal(m.pending(), 1, 'one scheduled delivery');
  m.tick();
  assert.equal(got.length, 1);
  assert.deepEqual(got[0].changed, ['a', 'b'], 'only subscribed keys are reported');
  assert.deepEqual(got[0].sources, ['p1', 'p2'], 'sources of unrelated sets are not reported');
  assert.equal(got[0].a, 2);
  assert.deepEqual(got[0].info, {history: false});
  s.set({c: 6});
  m.tick();
  assert.equal(got.length, 1, 'unrelated key does not notify');
});

test('sync listeners fire on every set; batch gives one notification', () => {
  const s = createStore({a: 0, b: 0}, {schedule: () => {}});
  const got = [];
  s.on(['a', 'b'], (changed) => got.push([...changed].sort().join(',')), {sync: true});
  s.set({a: 1});
  s.set({b: 1});
  assert.deepEqual(got, ['a', 'b']);
  s.batch(() => {
    s.set({a: 2});
    s.set({b: 2});
    s.batch(() => s.set({a: 3}));
    assert.equal(got.length, 2, 'nothing delivered inside a batch');
  });
  assert.deepEqual(got, ['a', 'b', 'a,b']);
  s.batch(() => {});
  assert.equal(got.length, 3, 'empty batch delivers nothing');
});

test('batch delivers even when the callback throws', () => {
  const s = createStore({a: 0}, {schedule: () => {}});
  const got = [];
  s.on(['a'], () => got.push(s.get('a')), {sync: true});
  assert.throws(() => s.batch(() => { s.set({a: 1}); throw new RangeError('boom'); }), RangeError);
  assert.deepEqual(got, [1]);
});

test('prefix patterns and history flag', () => {
  const m = manual();
  const s = createStore({}, {schedule: m.schedule});
  const got = [];
  s.on(['sel:*'], (changed, _s, _src, info) => got.push({keys: [...changed].sort(), history: info.history}));
  s.set({'sel:src': {version: 1, count: 3}});
  s.set({'sel:ctx': {version: 1, count: 0}, focus: {ds: 'src', i: 1, id: 'a'}}, {history: true});
  s.set({selx: 1});
  m.tick();
  assert.deepEqual(got, [{keys: ['sel:ctx', 'sel:src'], history: true}]);
  m.tick();
  s.set({'sel:src': {version: 2, count: 3}});
  m.tick();
  assert.deepEqual(got[1], {keys: ['sel:src'], history: false}, 'history flag resets after delivery');
});

test('unsubscribe stops delivery, including pending coalesced delivery', () => {
  const m = manual();
  const s = createStore({a: 0}, {schedule: m.schedule});
  let n = 0;
  const off = s.on('*', () => { n++; });
  s.set({a: 1});
  off();
  m.tick();
  assert.equal(n, 0);
});

test('a throwing listener does not block others; the error is rethrown on a microtask', () => {
  const s = createStore({a: 0}, {schedule: () => {}});
  const seen = [];
  const queued = [];
  const orig = globalThis.queueMicrotask;
  globalThis.queueMicrotask = (fn) => queued.push(fn);
  try {
    s.on(['a'], () => { throw new Error('listener failed'); }, {sync: true});
    s.on(['a'], () => seen.push('second'), {sync: true});
    s.onEvent('ping', () => { throw new Error('event failed'); });
    s.onEvent('ping', (p) => seen.push(`ping:${p}`));
    s.set({a: 1});
    s.emit('ping', 7);
  } finally {
    globalThis.queueMicrotask = orig;
  }
  assert.deepEqual(seen, ['second', 'ping:7']);
  const errors = [];
  for (const fn of queued) {
    assert.throws(fn, (err) => { errors.push(err.message); return true; });
  }
  assert.deepEqual(errors.sort(), ['event failed', 'listener failed']);
});

test('events: topic and wildcard listeners, synchronous, unsubscribe', () => {
  const s = createStore();
  const got = [];
  const off1 = s.onEvent('brush:commit', (p, topic) => got.push(['one', p.n, topic]));
  const off2 = s.onEvent('*', (p, topic) => got.push(['all', topic]));
  s.emit('brush:commit', {n: 3});
  s.emit('other');
  assert.deepEqual(got, [['one', 3, 'brush:commit'], ['all', 'brush:commit'], ['all', 'other']]);
  off1();
  off2();
  s.emit('brush:commit', {n: 4});
  assert.equal(got.length, 3);
});

test('handled-flag protocol through events', () => {
  const s = createStore();
  s.onEvent('keys:escape', (p) => { if (!p.handled) p.handled = true; });
  const payload = {handled: false};
  s.emit('keys:escape', payload);
  assert.equal(payload.handled, true);
});

test('a sync listener that sets another key re-enters cleanly', () => {
  const s = createStore({a: 0, b: 0}, {schedule: () => {}});
  const order = [];
  s.on(['a'], () => { order.push('a'); s.set({b: s.get('a') * 10}); }, {sync: true});
  s.on(['b'], () => order.push(`b=${s.get('b')}`), {sync: true});
  s.set({a: 2});
  assert.deepEqual(order, ['a', 'b=20']);
});

test('default scheduler under node uses setTimeout(0); flush() delivers early', async () => {
  const s = createStore({a: 0});
  const got = [];
  s.on(['a'], (c) => got.push([...c]));
  s.set({a: 1});
  assert.equal(got.length, 0);
  await new Promise((r) => setTimeout(r, 5));
  assert.deepEqual(got, [['a']]);
  s.set({a: 2});
  s.flush();
  assert.equal(got.length, 2);
  await new Promise((r) => setTimeout(r, 5));
  assert.equal(got.length, 2, 'no duplicate delivery after flush');
});

test('large values and unicode keys', () => {
  const s = createStore({}, {schedule: () => {}});
  const big = new Float32Array(1 << 20);
  const got = [];
  s.on('*', (c) => got.push([...c]), {sync: true});
  s.set({'filters:日本': {text: 'α≥β'}, big});
  assert.equal(s.get('big'), big);
  assert.equal(s.get('filters:日本').text, 'α≥β');
  s.set({big});
  assert.equal(got.length, 1, 'same typed-array identity is not a change');
});
