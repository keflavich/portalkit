// node --test tests/js/hash.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  HASH_ORDER, parseHash, formatHash, encodeHashValue, encodeJSONParam, decodeJSONParam,
  isHashStoreKey, stateToHash, hashToPatch, bindHash,
} from '../../web/js/core/hash.js';
import {createStore} from '../../web/js/core/store.js';
import {HASH_DEBOUNCE_MS, HASH_MAX_CHARS} from '../../web/js/core/constants.js';

// ---------------------------------------------------------------- fake window with manual timers

function fakeWindow(initialHash = '') {
  let now = 0;
  let nextId = 1;
  const timers = new Map();
  const listeners = {hashchange: new Set(), popstate: new Set()};
  const entries = [{url: `/p/page.html${initialHash ? '#' + initialHash.replace(/^#/, '') : ''}`, state: null}];
  let cur = 0;
  const log = [];
  const location = {
    pathname: '/p/page.html',
    search: '',
    get hash() {
      const u = entries[cur].url;
      const i = u.indexOf('#');
      return i < 0 || i === u.length - 1 ? '' : u.slice(i);
    },
  };
  const history = {
    get state() { return entries[cur].state; },
    pushState(state, _t, url) {
      entries.splice(cur + 1);
      entries.push({url, state});
      cur += 1;
      log.push(['push', url]);
    },
    replaceState(state, _t, url) {
      entries[cur] = {url, state};
      log.push(['replace', url]);
    },
  };
  const fire = (type) => { for (const fn of Array.from(listeners[type])) fn({type}); };
  return {
    location, history, log,
    get length() { return entries.length; },
    addEventListener: (t, fn) => listeners[t] && listeners[t].add(fn),
    removeEventListener: (t, fn) => listeners[t] && listeners[t].delete(fn),
    setTimeout: (fn, ms) => { const id = nextId++; timers.set(id, {fn, at: now + ms}); return id; },
    clearTimeout: (id) => { timers.delete(id); },
    advance(ms) {
      now += ms;
      for (;;) {
        const due = [...timers.entries()].filter(([, t]) => t.at <= now).sort((a, b) => a[1].at - b[1].at);
        if (!due.length) break;
        const [id, t] = due[0];
        timers.delete(id);
        t.fn();
      }
    },
    pendingTimers: () => timers.size,
    back() { cur -= 1; fire('popstate'); },
    forward() { cur += 1; fire('popstate'); },
    userNavigate(hash) {          // typing a new hash / clicking an anchor
      entries.splice(cur + 1);
      entries.push({url: `/p/page.html#${hash.replace(/^#/, '')}`, state: null});
      cur += 1;
      fire('hashchange');
    },
  };
}

function makeStore(extra = {}) {
  return createStore(Object.assign({
    focus: null, color: {by: null, cmap: 'viridis'}, av: 0, rv: 3.1, dm: 0, template: null,
    features: [], spec: {pinned: [], variant: 'flux'}, sky: null, rung: null, beat: null, role: null,
    facets: {}, lightbox: null, layout: {popout: {}},
    'filters:src': {}, 'sel:src': {version: 0, count: 0},
    'axes:ccd': {x: 'f182m-f410m', y: 'f410m', xlog: false, ylog: false},
    'frame:mani': {preset: 'pca', dims: []},
  }, extra), {schedule: (fn) => fn()});
}

// ---------------------------------------------------------------- parse / format

test('parseHash: decoding, empties, duplicates, prototype keys, bad escapes', () => {
  assert.deepEqual(parseHash(''), {});
  assert.deepEqual(parseHash('#'), {});
  assert.deepEqual(parseHash(null), {});
  assert.deepEqual(parseHash('#a=1&b=x%20y&flag&&c='), {a: '1', b: 'x y', flag: '', c: ''});
  assert.deepEqual(parseHash('a=1&a=2'), {a: '2'});
  assert.deepEqual(parseHash('#id=%E2%98%85&x=%E0%A4'), {id: '★', x: '%E0%A4'});
  const p = parseHash('__proto__=1&constructor=2&prototype=3&ok=4');
  assert.deepEqual(Object.keys(p), ['ok']);
  assert.equal(Object.getPrototypeOf(p), Object.prototype);
  assert.deepEqual(parseHash('#v=a=b'), {v: 'a=b'}, 'only the first = splits');
});

test('formatHash: stable section-6.2 order, pane keys grouped after f, unknown keys last', () => {
  const obj = {
    view: 'ccd:half', zzz: '1', aaa: '2', 'ccd.y': 'b', 'abc.frame': 'pca', 'ccd.x': 'a', id: 'S1',
    sel: 'ids:S1', f: 'e30', c: 'cls~magma', lb: '3', empty: '', nul: null, und: undefined,
  };
  const out = formatHash(obj);
  assert.equal(out, 'id=S1&sel=ids:S1&f=e30&abc.frame=pca&ccd.x=a&ccd.y=b&c=cls~magma&lb=3&view=ccd:half&aaa=2&zzz=1');
  assert.equal(formatHash({}), '');
  assert.equal(formatHash(null), '');
  assert.equal(HASH_ORDER[0], 'id');
  assert.equal(formatHash(obj), formatHash(Object.fromEntries(Object.entries(obj).reverse())), 'order independent of insertion');
});

test('encodeHashValue keeps readable punctuation and escapes separators', () => {
  assert.equal(encodeHashValue('a,b:c/d@e!*()'), 'a,b:c/d@e!*()');
  assert.equal(encodeHashValue('a&b=c#d e%'), 'a%26b%3Dc%23d%20e%25');
  assert.equal(encodeHashValue('★'), '%E2%98%85');
  const nasty = {id: 'x&y=z#w', 'p&q.x': 'v=1'};
  assert.deepEqual(parseHash(formatHash(nasty)), nasty);
});

test('encodeJSONParam / decodeJSONParam round-trip, raw JSON accepted, junk rejected', () => {
  const v = {cats: {cls: ['YSO', 'AGB ★']}, ranges: {'f115w-f200w': [0.5, 2]}, text: '日本'};
  const e = encodeJSONParam(v);
  assert.match(e, /^[A-Za-z0-9_-]+$/);
  assert.deepEqual(decodeJSONParam(e), v);
  assert.deepEqual(decodeJSONParam('{"a":1}'), {a: 1});
  assert.deepEqual(decodeJSONParam(' [1,2] '), [1, 2]);
  assert.equal(decodeJSONParam(''), undefined);
  assert.equal(decodeJSONParam(undefined), undefined);
  assert.equal(decodeJSONParam('{bad'), undefined);
  assert.equal(decodeJSONParam('!!!'), undefined);
});

test('isHashStoreKey', () => {
  for (const k of ['focus', 'color', 'av', 'layout', 'axes:ccd', 'frame:m', 'sel:src', 'filters:src']) {
    assert.ok(isHashStoreKey(k, 'src'), k);
  }
  for (const k of ['hover', 'sel:ctx', 'filters:ctx', 'vis:src', 'tier:ccd', 'theme', 'annot:src']) {
    assert.ok(!isHashStoreKey(k, 'src'), k);
  }
});

// ---------------------------------------------------------------- state <-> hash

test('stateToHash -> hashToPatch round-trip for every canonical key', () => {
  const state = {
    focus: {ds: 'src', i: 12, id: 'NRS-001 ★'},
    'filters:src': {cats: {cls: ['AGB']}, ranges: {'f115w-f200w': [0.25, 3]}, text: 'α'},
    'axes:ccd': {x: 'f182m-f410m', y: 'f410m', xlog: false},
    'frame:mani': {preset: null, F: [1, 0, 0.5, -0.25], dims: ['a', 'b']},
    'frame:m2': {preset: 'pca', dims: []},
    color: {by: 'cls', cmap: 'magma', lo: -1.5, hi: 2},
    av: 12.5, rv: 3.1, dm: -0,
    template: {key: 'kurucz', grid: 'g1', scale: 0.75, bands: ['J', 'H']},
    features: ['ice', 'h2'],
    spec: {pinned: ['a', 'b'], variant: 'fnu', xr: [1.5, 5.25], vel: 30},
    sky: {ra: 266.41683, dec: -29.00781, fov: 0.25, base: 'glimpse', overlays: [{id: 'msa', opacity: 0.5}, {id: 'dith'}]},
    rung: 'mir', beat: 'intro', role: 'rgb',
    facets: {filter: ['F200W']},
    lightbox: {set: 'gal', index: 4},
    layout: {popout: {ccd: 'half', mani: null}},
  };
  const h = stateToHash(state, {primaryDs: 'src', serializeSel: () => 'ids:a,b'});
  assert.equal(h.id, 'NRS-001 ★');
  assert.equal(h.sel, 'ids:a,b');
  assert.equal(h['ccd.x'], 'f182m-f410m');
  assert.equal(h['m2.frame'], 'pca');
  assert.match(h['mani.frame'], /^b64:/);
  assert.equal(h.c, 'cls~magma~-1.5~2');
  assert.equal(h.dm, '0', '-0 normalised');
  assert.equal(h.tpl, 'kurucz~g1~0.75~J,H');
  assert.equal(h.feat, 'ice,h2');
  assert.equal(h.wl, '1.5,5.25');
  assert.equal(h.sky, '266.41683,-29.00781,0.25');
  assert.equal(h.ovl, 'msa:0.5,dith');
  assert.equal(h.lb, 'gal,4');
  assert.equal(h.view, 'ccd:half');

  // through the URL string and back
  const parsed = parseHash('#' + formatHash(h));
  assert.deepEqual(parsed, h);
  const {patch, sel} = hashToPatch(parsed, {primaryDs: 'src', state: {}});
  assert.equal(sel, 'ids:a,b');
  assert.deepEqual(patch.focus, {ds: 'src', i: null, id: 'NRS-001 ★'});
  assert.deepEqual(patch['filters:src'], state['filters:src']);
  assert.deepEqual(patch['axes:ccd'], {x: 'f182m-f410m', y: 'f410m'});
  assert.deepEqual(patch['frame:mani'].F, [1, 0, 0.5, -0.25]);
  assert.equal(patch['frame:m2'].preset, 'pca');
  assert.deepEqual(patch.color, {by: 'cls', cmap: 'magma', lo: -1.5, hi: 2});
  assert.equal(patch.av, 12.5);
  assert.equal(patch.rv, 3.1);
  assert.deepEqual(patch.template, {key: 'kurucz', grid: 'g1', scale: 0.75, bands: ['J', 'H']});
  assert.deepEqual(patch.features, ['ice', 'h2']);
  assert.deepEqual(patch.spec, {pinned: ['a', 'b'], variant: 'fnu', vel: 30, xr: [1.5, 5.25]});
  assert.deepEqual(patch.sky, {ra: 266.41683, dec: -29.00781, fov: 0.25, base: 'glimpse', overlays: [{id: 'msa', opacity: 0.5}, {id: 'dith'}]});
  assert.equal(patch.rung, 'mir');
  assert.deepEqual(patch.facets, {filter: ['F200W']});
  assert.deepEqual(patch.lightbox, {set: 'gal', index: 4});
  assert.deepEqual(patch.layout, {popout: {ccd: 'half'}});

  // second trip is a fixed point
  const h2 = stateToHash(Object.assign({}, state, patch), {primaryDs: 'src', serializeSel: () => sel});
  assert.equal(formatHash(h2), formatHash(h));
});

test('stateToHash omits empty / null / non-finite values', () => {
  const h = stateToHash({
    focus: {ds: 'src', i: 3, id: null}, 'filters:src': {cats: {}, ranges: {}}, color: {by: null, cmap: 'viridis'},
    av: NaN, rv: Infinity, template: null, features: [], spec: {pinned: [], variant: '', xr: [1, NaN]},
    sky: {ra: 1, dec: NaN}, facets: {}, lightbox: {set: 'g', index: null}, layout: {popout: {}},
    'axes:p': {x: '', y: null}, 'frame:q': {dims: []}, rung: '',
  }, {primaryDs: 'src', serializeSel: () => ''});
  assert.deepEqual(h, {});
  assert.deepEqual(stateToHash({}, {}), {});
});

test('hashToPatch: leniency and malformed values', () => {
  const st = {color: {by: 'x', cmap: 'magma'}, spec: {pinned: ['p'], variant: 'flux', xr: [1, 2]}, sky: {ra: 1, dec: 2, fov: 3, base: 'b'}};
  const {patch} = hashToPatch({
    c: 'none', av: 'abc', rv: '', dm: '1e400', f: '{"text":"x"}', 'p.frame': 'b64:AAA', spec: '!!', wl: '3,x',
    sky: 'nan,1', lb: 'gal,notnum', fac: '', view: 'a:huge,b:full,:full', tpl: '', id: '', unknown: '1',
  }, {primaryDs: 'src', state: st});
  assert.deepEqual(patch.color, {by: null, cmap: 'magma'}, 'cmap kept from state');
  assert.ok(!('av' in patch) && !('rv' in patch) && !('dm' in patch));
  assert.deepEqual(patch['filters:src'], {text: 'x'}, 'raw JSON filter accepted');
  assert.ok(!('frame:p' in patch), 'bad frame bytes ignored');
  assert.deepEqual(patch.spec, {pinned: ['p'], variant: 'flux'}, 'bad wl clears xr; bad spec ignored');
  assert.ok(!('sky' in patch));
  assert.ok(!('lightbox' in patch));
  assert.deepEqual(patch.facets, {});
  assert.deepEqual(patch.layout, {popout: {b: 'full'}});
  assert.equal(patch.template, null);
  assert.equal(patch.focus, null);
  assert.ok(!('unknown' in patch));
  // without a primary dataset, f is ignored and id still sets focus
  const r = hashToPatch({f: 'e30', id: 'X'}, {});
  assert.deepEqual(r.patch, {focus: {ds: null, i: null, id: 'X'}});
  // same focus id keeps the resolved object (and its i)
  const f = {ds: 'src', i: 9, id: 'X'};
  assert.equal(hashToPatch({id: 'X'}, {primaryDs: 'src', state: {focus: f}}).patch.focus, f);
  // lb with an index only keeps the current set
  assert.deepEqual(hashToPatch({lb: '7'}, {state: {lightbox: {set: 's', index: 1}}}).patch.lightbox, {set: 's', index: 7});
  // a base= alone merges into the existing sky
  assert.deepEqual(hashToPatch({base: 'dss'}, {state: st}).patch.sky, {ra: 1, dec: 2, fov: 3, base: 'dss'});
});

test('frame b64 encodes float32 exactly', () => {
  const F = [0.1, -1e-7, 3.4e38, 0];
  const h = stateToHash({'frame:m': {F, dims: []}});
  const back = hashToPatch(parseHash(formatHash(h)), {}).patch['frame:m'].F;
  assert.deepEqual(back, Array.from(Float32Array.from(F)));
});

// ---------------------------------------------------------------- bindHash

const DEF = makeStore().snapshot();   // page defaults, as boot passes them

test('bindHash applies the initial hash and writes with debounced replaceState', () => {
  const win = fakeWindow('id=S9&c=cls&ccd.x=a-b');
  const store = makeStore();
  const unbind = bindHash(store, {primaryDs: 'src', defaults: DEF, win});
  assert.deepEqual(store.get('focus'), {ds: 'src', i: null, id: 'S9'});
  assert.equal(store.get('color').by, 'cls');
  assert.equal(store.get('axes:ccd').x, 'a-b');
  assert.equal(store.get('axes:ccd').y, 'f410m', 'other axis kept');
  assert.equal(win.log.length, 0, 'applying the hash does not rewrite it');

  store.set({av: 5}, {source: 'controls'});
  store.set({av: 6}, {source: 'controls'});
  store.set({hover: {ds: 'src', i: 1}});           // not a hash key
  assert.equal(win.log.length, 0);
  win.advance(HASH_DEBOUNCE_MS - 1);
  assert.equal(win.log.length, 0);
  win.advance(1);
  assert.equal(win.log.length, 1);
  assert.equal(win.log[0][0], 'replace');
  assert.match(win.location.hash, /av=6/);
  assert.match(win.location.hash, /^#id=S9&/);
  unbind();
  store.set({av: 7});
  win.advance(1000);
  assert.equal(win.log.length, 1, 'unbound');
});

test('focus changes push a history entry; Back restores the previous source', () => {
  const win = fakeWindow('');
  const store = makeStore();
  bindHash(store, {primaryDs: 'src', defaults: DEF, win});
  store.set({focus: {ds: 'src', i: 1, id: 'A'}}, {source: 'list'});
  store.set({focus: {ds: 'src', i: 2, id: 'B'}}, {source: 'list'});
  assert.deepEqual(win.log.map((e) => e[0]), ['push', 'push']);
  assert.equal(win.location.hash, '#id=B');
  store.set({focus: {ds: 'src', i: 2, id: 'B', extra: 1}}, {source: 'boot'});
  assert.equal(win.log.length, 2, 'same id does not push');
  win.back();
  assert.equal(store.get('focus').id, 'A');
  win.back();
  assert.equal(store.get('focus'), null, 'absent id clears focus');
  win.forward();
  assert.equal(store.get('focus').id, 'A');
  assert.equal(win.log.length, 2, 'navigation does not write');
  store.set({rung: 'x'}, {history: true});
  assert.equal(win.log.at(-1)[0], 'push', 'history:true pushes');
});

test('pending debounce is cancelled by a push and by navigation', () => {
  const win = fakeWindow('');
  const store = makeStore();
  bindHash(store, {primaryDs: 'src', defaults: DEF, win});
  store.set({av: 2});
  store.set({focus: {ds: 'src', i: 0, id: 'Z'}});
  assert.equal(win.log.length, 1);
  assert.equal(win.location.hash, '#id=Z&av=2', 'push includes the pending change');
  assert.equal(win.pendingTimers(), 0);
  store.set({av: 3});
  win.userNavigate('id=Q');
  win.advance(1000);
  assert.equal(store.get('av'), 0, 'av absent: page default restored');
  assert.equal(win.location.hash, '#id=Q');
});

test('defaults: values equal to page defaults are omitted; absent keys restore them', () => {
  const win = fakeWindow('');
  const store = makeStore();
  bindHash(store, {primaryDs: 'src', win, defaults: DEF});
  store.set({av: 4});
  win.advance(HASH_DEBOUNCE_MS);
  assert.equal(win.location.hash, '#av=4', 'rv=3.1, ccd.x/y and mani.frame are defaults and omitted');
  store.set({av: 0});
  win.advance(HASH_DEBOUNCE_MS);
  assert.equal(win.location.hash, '');
  store.set({'axes:ccd': {x: 'q', y: 'f410m'}});
  win.advance(HASH_DEBOUNCE_MS);
  assert.equal(win.location.hash, '#ccd.x=q');
  win.userNavigate('rv=5.5');
  assert.equal(store.get('rv'), 5.5);
  assert.equal(store.get('axes:ccd').x, 'f182m-f410m', 'absent pane axis restored to default');
  win.userNavigate('');
  assert.equal(store.get('rv'), 3.1);
});

test('keys option restricts binding, including pane patterns', () => {
  const win = fakeWindow('av=9&ccd.x=zz&rv=1');
  const store = makeStore();
  bindHash(store, {primaryDs: 'src', win, keys: ['av', '*.x']});
  assert.equal(store.get('av'), 9);
  assert.equal(store.get('axes:ccd').x, 'zz');
  assert.equal(store.get('rv'), 3.1, 'rv not bound');
  store.set({rv: 2, dm: 1});
  win.advance(HASH_DEBOUNCE_MS);
  assert.equal(win.location.hash, '#ccd.x=zz&av=9');
});

test('selection adapter: initial apply, serialize on write, oversize sel dropped with an event', () => {
  let current = '';
  const applied = [];
  const win = fakeWindow('sel=ids:a,b');
  const store = makeStore();
  const dropped = [];
  store.onEvent('hash:sel-dropped', (p) => dropped.push(p));
  const unbind = bindHash(store, {
    primaryDs: 'src', defaults: DEF, win,
    selection: {serialize: () => current, apply: (s) => { applied.push(s); current = s; }},
  });
  assert.deepEqual(applied, ['ids:a,b']);
  current = 'bits:' + 'A'.repeat(100);
  store.set({'sel:src': {version: 1, count: 5}}, {source: 'selection'});
  win.advance(HASH_DEBOUNCE_MS);
  assert.equal(win.location.hash, `#sel=${current}`);
  assert.equal(unbind.selDropped(), false);
  current = 'bits:' + 'A'.repeat(HASH_MAX_CHARS);
  store.set({'sel:src': {version: 2, count: 5}, av: 1});
  win.advance(HASH_DEBOUNCE_MS);
  assert.equal(win.location.hash, '#av=1');
  assert.equal(unbind.selDropped(), true);
  assert.equal(dropped.length, 1);
  assert.equal(dropped[0].ds, 'src');
  assert.ok(dropped[0].length > HASH_MAX_CHARS);
  // navigating to a hash without sel clears the selection
  current = 'ids:x';
  win.userNavigate('av=2');
  assert.equal(applied.at(-1), '');
  // selection of a non-primary dataset never triggers a write
  const before = win.log.length;
  store.set({'sel:ctx': {version: 1, count: 1}});
  win.advance(HASH_DEBOUNCE_MS);
  assert.equal(win.log.length, before);
});

test('touch / flush / replaceNow / compute', () => {
  const win = fakeWindow('');
  const store = makeStore();
  let selStr = '';
  const unbind = bindHash(store, {primaryDs: 'src', defaults: DEF, win, selection: {serialize: () => selStr, apply: () => {}}});
  selStr = 'ids:late';
  unbind.touch();
  assert.equal(win.log.length, 0);
  win.advance(HASH_DEBOUNCE_MS);
  assert.equal(win.location.hash, '#sel=ids:late');
  store.set({av: 1});
  unbind.flush();
  assert.equal(win.location.hash, '#sel=ids:late&av=1');
  assert.equal(win.pendingTimers(), 0);
  selStr = '';
  assert.equal(unbind.compute(), 'av=1');
  unbind.replaceNow();
  assert.equal(win.location.hash, '#av=1');
  assert.equal(win.log.at(-1)[0], 'replace');
  unbind.flush();                           // nothing pending: no write
  const n = win.log.length;
  unbind.replaceNow();                      // unchanged hash: no write
  assert.equal(win.log.length, n);
});

test('lightbox and popout are cleared when their keys leave the hash', () => {
  const win = fakeWindow('lb=gal,2&view=ccd:full');
  const store = makeStore();
  bindHash(store, {primaryDs: 'src', defaults: DEF, win});
  assert.deepEqual(store.get('lightbox'), {set: 'gal', index: 2});
  assert.deepEqual(store.get('layout').popout, {ccd: 'full'});
  win.userNavigate('av=1');
  assert.equal(store.get('lightbox'), null);
  assert.deepEqual(store.get('layout').popout, {});
});

test('applyInitial:false leaves the store alone; missing window gives a no-op unbind', () => {
  const win = fakeWindow('av=8');
  const store = makeStore();
  bindHash(store, {primaryDs: 'src', win, applyInitial: false});
  assert.equal(store.get('av'), 0);
  const u = bindHash(store, {primaryDs: 'src', win: {}});
  assert.equal(typeof u, 'function');
  u();
});

test('unicode ids and large filters survive a write/read cycle', () => {
  const win = fakeWindow('');
  const store = makeStore();
  bindHash(store, {primaryDs: 'src', defaults: DEF, win});
  const ids = Array.from({length: 200}, (_, i) => `src-${i}-ψ`);
  store.set({'filters:src': {ids, text: 'G0.1−0.2 & "quoted"'}});
  store.set({focus: {ds: 'src', i: 3, id: 'J1745−2900 #1'}});
  const h = parseHash(win.location.hash);
  assert.equal(h.id, 'J1745−2900 #1');
  assert.deepEqual(decodeJSONParam(h.f), {ids, text: 'G0.1−0.2 & "quoted"'});
  const store2 = makeStore();
  const win2 = fakeWindow(win.location.hash);
  bindHash(store2, {primaryDs: 'src', win: win2});
  assert.deepEqual(store2.get('filters:src'), store.get('filters:src'));
  assert.equal(store2.get('focus').id, 'J1745−2900 #1');
});

// ---------------------------------------------------------------- review fixes

test('num(): small magnitudes keep significant digits (colour limits, template scales)', () => {
  const h = stateToHash({color: {by: 'f444w_flx', cmap: 'viridis', lo: 2e-7, hi: 8.5e-7},
    template: {key: 'k', grid: 'g', scale: 3.3e-8}, av: 1.23456789, sky: {ra: 266.41684213, dec: -28.9, fov: 0.5}});
  const p = hashToPatch(parseHash(formatHash(h)), {primaryDs: 'src', state: {}}).patch;
  assert.equal(p.color.lo, 2e-7);
  assert.equal(p.color.hi, 8.5e-7);
  assert.equal(p.template.scale, 3.3e-8);
  assert.equal(h.av, '1.234568', 'normal magnitudes keep 1e-6 absolute precision');
  assert.equal(p.sky.ra, 266.416842);
});

test('values cleared relative to non-empty page defaults survive reload and Back', () => {
  const defs = makeStore({color: {by: 'mag', cmap: 'viridis'}, features: ['ice'],
    template: {key: 't1', grid: 'g', scale: 'auto'}, rung: 'r1'}).snapshot();
  const win = fakeWindow('');
  const store = makeStore({color: {by: 'mag', cmap: 'viridis'}, features: ['ice'],
    template: {key: 't1', grid: 'g', scale: 'auto'}, rung: 'r1'});
  bindHash(store, {primaryDs: 'src', defaults: defs, win});
  store.set({color: {by: null, cmap: 'viridis'}, features: [], template: null, rung: null});
  win.advance(HASH_DEBOUNCE_MS);
  const h = parseHash(win.location.hash);
  assert.equal(h.c, 'none');
  assert.equal(h.feat, '-');
  assert.equal(h.tpl, '-');
  assert.equal(h.rung, '-');
  // fresh load of that URL with the same defaults
  const store2 = makeStore({color: {by: 'mag', cmap: 'viridis'}, features: ['ice'],
    template: {key: 't1', grid: 'g', scale: 'auto'}, rung: 'r1'});
  bindHash(store2, {primaryDs: 'src', defaults: defs, win: fakeWindow(win.location.hash)});
  assert.equal(store2.get('color').by, null);
  assert.deepEqual(store2.get('features'), []);
  assert.equal(store2.get('template'), null);
  assert.equal(store2.get('rung'), null);
  // back to defaults: markers go away
  store.set({color: {by: 'mag', cmap: 'viridis'}, features: ['ice'], template: {key: 't1', grid: 'g', scale: 'auto'}, rung: 'r1'});
  win.advance(HASH_DEBOUNCE_MS);
  assert.equal(win.location.hash, '');
});

test('hashToPatch reads the cleared marker for every key that has one', () => {
  const st = {sky: {ra: 1, dec: 2, fov: 3, base: 'dss', overlays: [{id: 'a'}]}, spec: {pinned: [1], variant: 'flux', xr: [1, 2]},
    'axes:ccd': {x: 'a', y: 'b'}, layout: {popout: {ccd: 'full'}}};
  const {patch, sel} = hashToPatch({id: '-', sel: '-', f: '-', c: '-', tpl: '-', feat: '-', 'ccd.x': '-', wl: '-',
    base: '-', ovl: '-', beat: '-', fac: '-', lb: '-', view: '-'}, {primaryDs: 'src', state: st});
  assert.equal(patch.focus, null);
  assert.equal(sel, '');
  assert.deepEqual(patch['filters:src'], {});
  assert.equal(patch.color.by, null);
  assert.equal(patch.template, null);
  assert.deepEqual(patch.features, []);
  assert.equal(patch['axes:ccd'].x, null);
  assert.equal(patch['axes:ccd'].y, 'b');
  assert.equal(patch.spec.xr, undefined);
  assert.equal(patch.sky.base, null);
  assert.deepEqual(patch.sky.overlays, []);
  assert.equal(patch.beat, null);
  assert.deepEqual(patch.facets, {});
  assert.equal(patch.lightbox, null);
  assert.deepEqual(patch.layout.popout, {});
});

test('sel-dropped fires once per transition; hashLink-style compute has no side effects', () => {
  let current = 'bits:' + 'A'.repeat(HASH_MAX_CHARS);
  const win = fakeWindow('');
  const store = makeStore();
  const dropped = [];
  store.onEvent('hash:sel-dropped', (p) => dropped.push(p));
  const unbind = bindHash(store, {primaryDs: 'src', defaults: DEF, win,
    selection: {serialize: () => current, apply: () => {}, count: () => 5}});
  for (let k = 1; k <= 5; k++) {
    store.set({av: k});
    win.advance(HASH_DEBOUNCE_MS);
  }
  assert.equal(dropped.length, 1);
  assert.equal(unbind.compute(), 'av=5');
  assert.equal(unbind.computeInfo().dropped, true);
  assert.equal(dropped.length, 1, 'compute() does not emit');
  current = 'ids:a';
  store.set({av: 6});
  win.advance(HASH_DEBOUNCE_MS);
  current = 'bits:' + 'B'.repeat(HASH_MAX_CHARS);
  store.set({av: 7});
  win.advance(HASH_DEBOUNCE_MS);
  assert.equal(dropped.length, 2, 'fires again after a representable selection');
});

test('Back keeps a selection that could not be put in the hash; a hash written empty clears it', () => {
  const big = 'bits:' + 'A'.repeat(HASH_MAX_CHARS);
  let current = '';
  let count = 0;
  const applied = [];
  const win = fakeWindow('');
  const store = makeStore();
  bindHash(store, {primaryDs: 'src', defaults: DEF, win,
    selection: {serialize: () => current, count: () => count,
      apply: (s) => { applied.push(s); current = s; count = s ? 1 : 0; }}});
  // large selection made, then focus A, focus B before and after the debounce
  current = big; count = 60000;
  store.set({'sel:src': {version: 1, count}}, {source: 'selection'});
  store.set({focus: {ds: 'src', i: 1, id: 'A'}}, {source: 'list'});
  store.set({focus: {ds: 'src', i: 2, id: 'B'}}, {source: 'list'});
  win.back();
  assert.equal(store.get('focus').id, 'A');
  win.back();
  assert.equal(store.get('focus'), null);
  assert.deepEqual(applied, [], 'selection kept on Back');
  assert.equal(count, 60000);
  // an unserializable selection ('' with count > 0) behaves the same
  current = ''; count = 300000;
  win.forward();
  assert.deepEqual(applied, []);
  // a hash typed by the user without sel, with a representable selection: clears
  current = 'ids:q'; count = 1;
  store.set({'sel:src': {version: 2, count: 1}}, {source: 'selection'});
  win.advance(HASH_DEBOUNCE_MS);
  win.userNavigate('av=3');
  assert.deepEqual(applied, ['']);
});

test('pending focus (index without id) writes one entry once the id arrives', () => {
  const win = fakeWindow('');
  const store = makeStore();
  bindHash(store, {primaryDs: 'src', defaults: DEF, win});
  store.set({focus: {ds: 'src', i: 1, id: 'S1'}}, {source: 'list'});
  const n = win.length;
  store.set({focus: {ds: 'src', i: 2, id: null}}, {source: 'keys'});
  assert.equal(win.length, n, 'nothing pushed while pending');
  win.advance(1000);
  assert.equal(win.location.hash, '#id=S1');
  store.set({focus: {ds: 'src', i: 2, id: 'S2'}}, {source: 'boot'});
  assert.equal(win.length, n + 1);
  assert.equal(win.location.hash, '#id=S2');
  win.back();
  assert.equal(store.get('focus').id, 'S1');
});

test('broadcast focus replaces the entry instead of pushing', () => {
  const win = fakeWindow('');
  const store = makeStore();
  bindHash(store, {primaryDs: 'src', defaults: DEF, win});
  store.set({focus: {ds: 'src', i: null, id: 'X'}}, {source: 'broadcast'});
  assert.equal(win.length, 1);
  win.advance(HASH_DEBOUNCE_MS);
  assert.equal(win.location.hash, '#id=X');
  assert.equal(win.length, 1);
  assert.equal(win.log.at(-1)[0], 'replace');
});
