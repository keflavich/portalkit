// WP11 pure helpers: import parsing and resolution (ui/export.js), checklist annotations
// (ui/annotate.js), hover-card placement (ui/hover.js), lightbox stepping (ui/lightbox.js) and
// the DOM-free helpers of the list, detail, gallery, table, readme and landing panes.
// Run: node --test tests/js/export.test.mjs

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  parseImportText, parsePosition, resolveImport, exportFileName, importSummary, entryLabel,
  scopeCounts, defaultScope, IMPORT_RADIUS_ARCSEC, EXPORT_FORMATS,
} from '../../web/js/ui/export.js';
import {
  DEFAULT_CODES, CHECKLIST_KEYS, CODE_LABELS, storageKey, normalizeCodes, codeForKey,
  serializeAnnotations, parseAnnotations, memoryStorage, Annotations, checklistFields, annotationsCSV,
} from '../../web/js/ui/annotate.js';
import {placeCard, pickHoverColumns, hoverValueText} from '../../web/js/ui/hover.js';
import {parseCSV} from '../../web/js/workers/ops/export.js';
import {createStore} from '../../web/js/core/store.js';
import {stepIndex} from '../../web/js/ui/lightbox.js';
import {parseSort, sortIndices} from '../../web/js/panes/list.js';
import {barFraction, buildSections, propertyText, substituteLink, TEMPLATES} from '../../web/js/panes/detail.js';
import {
  facetCounts, filterItems, gridGeometry, parseSortKey, passesFacets, roleFor, tagValues,
} from '../../web/js/panes/gallery.js';
import {normalizeJSONTable, sortRows} from '../../web/js/panes/table.js';
import {safeUrl} from '../../web/js/panes/readme.js';
import {cardList, sourceHref} from '../../web/js/panes/landing.js';

// ------------------------------------------------------------------ parseImportText

test('parseImportText: empty, whitespace and comment-only input', () => {
  for (const t of ['', '   ', '\n\n', null, undefined, '# only a comment\n#another']) {
    const r = parseImportText(t);
    assert.equal(r.entries.length, 0);
    assert.equal(r.format, 'empty');
  }
});

test('parseImportText: one id per line, BOM, CRLF, duplicates and unicode', () => {
  const r = parseImportText('﻿J1745-2900\r\nsrc_2\r\n\r\n# comment\r\nJ1745-2900\r\nŁódź‑★ source\r\n');
  assert.equal(r.format, 'lines');
  assert.deepEqual(r.entries.map((e) => e.id), ['J1745-2900', 'src_2', 'Łódź‑★ source']);
  assert.equal(r.entries[0].line, 1);
  assert.equal(r.entries[2].line, 6);
});

test('parseImportText: a single id is an id, a numeric id stays an id', () => {
  assert.deepEqual(parseImportText('12345').entries, [{id: '12345', line: 1}]);
  assert.deepEqual(parseImportText('  6927_10038 ').entries, [{id: '6927_10038', line: 1}]);
});

test('parseImportText: coordinate lines (decimal and sexagesimal)', () => {
  const r = parseImportText('266.4167 -29.0072\n17:45:40.04 -29:00:28.1\n17h45m40.04s -29d00m28.1s');
  // the third line is the same position as the second and is deduplicated
  assert.equal(r.entries.length, 2);
  assert.ok(Math.abs(r.entries[0].ra - 266.4167) < 1e-9);
  assert.ok(Math.abs(r.entries[0].dec + 29.0072) < 1e-9);
  assert.ok(Math.abs(r.entries[1].ra - 266.41683) < 1e-4);
  assert.ok(Math.abs(r.entries[1].dec + 29.00781) < 1e-4);
  assert.equal(r.entries[1].id, undefined);
  const s = parseImportText('17h45m40.04s -29d00m28.1s');
  assert.ok(Math.abs(s.entries[0].ra - r.entries[1].ra) < 1e-9);
});

test('parseImportText: single comma line is an id list', () => {
  const r = parseImportText('a,b, c ,"d,e",a');
  assert.deepEqual(r.entries.map((e) => e.id), ['a', 'b', 'c', 'd,e']);
});

test('parseImportText: CSV with id/ra/dec header (case-insensitive, quoted cells)', () => {
  const csv = 'Name,RAJ2000,DEJ2000,mag\n"src, one",266.1,-29.1,12\nsrc2,,,13\n,266.3,-29.3,14\n,,,\n';
  const r = parseImportText(csv);
  assert.equal(r.format, 'csv');
  assert.equal(r.entries.length, 3);
  assert.equal(r.entries[0].id, 'src, one');
  assert.equal(r.entries[0].ra, 266.1);
  assert.equal(r.entries[1].id, 'src2');
  assert.equal(r.entries[1].ra, undefined);
  assert.equal(r.entries[2].id, undefined);
  assert.equal(r.entries[2].dec, -29.3);
  assert.equal(r.skipped, 1);
});

test('parseImportText: TSV header and sexagesimal cells', () => {
  const r = parseImportText('id\tra\tdec\nX1\t17:45:40.0\t-29:00:28\n');
  assert.equal(r.format, 'csv');
  assert.equal(r.entries[0].id, 'X1');
  assert.ok(Math.abs(r.entries[0].ra - 266.41667) < 1e-4);
});

test('parseImportText: CSV round trip of our own export (id,ra,dec,...)', () => {
  const r = parseImportText('id,ra,dec,f182m\nA,10.5,-5.25,18.2\nB,inf,nan,\n');
  assert.equal(r.entries.length, 2);
  assert.equal(r.entries[0].ra, 10.5);
  assert.equal(r.entries[1].id, 'B');
  assert.equal(r.entries[1].ra, undefined, 'non-finite positions are dropped');
});

test('parseImportText: out-of-range positions are rejected', () => {
  assert.equal(parsePosition('361', '0'), null);
  assert.equal(parsePosition('-1', '0'), null);
  assert.equal(parsePosition('10', '91'), null);
  assert.equal(parsePosition('1e400', '0'), null);
  assert.equal(parsePosition('', '0'), null);
  assert.deepEqual(parsePosition('0', '-90'), {ra: 0, dec: -90});
  assert.deepEqual(parsePosition('359.9999', '90'), {ra: 359.9999, dec: 90});
  assert.deepEqual(parsePosition('10', '−5'), {ra: 10, dec: -5});
  const r = parseImportText('ra,dec\n400,10\n10,10\n');
  assert.equal(r.entries.length, 1);
  assert.equal(r.skipped, 1);
});

test('parseImportText: JSON arrays of ids and of objects', () => {
  assert.deepEqual(parseImportText('["a", "b", 3, "", null]').entries.map((e) => e.id), ['a', 'b', '3']);
  const r = parseImportText('[{"id":"a","ra":1,"dec":2},{"RA":3,"Dec":4},{"x":1}]');
  assert.equal(r.format, 'json');
  assert.equal(r.entries.length, 2);
  assert.deepEqual(r.entries[1], {line: 2, ra: 3, dec: 4});
  assert.equal(r.skipped, 1);
  assert.deepEqual(parseImportText('{"ids":["z"]}').entries.map((e) => e.id), ['z']);
  // malformed JSON falls back to plain lines
  assert.equal(parseImportText('[not json').format, 'lines');
});

test('parseImportText: DS9 region file (our own export format)', () => {
  const reg = '# Region file format: DS9 version 4.1\nglobal color=green\nfk5\n'
    + 'circle(266.4167000,-29.0072000,1") # text={src 1}\n'
    + 'circle(17:45:40.04,-29:00:28.1,0.5")\n';
  const r = parseImportText(reg);
  assert.equal(r.format, 'ds9');
  assert.equal(r.entries.length, 2);
  assert.equal(r.entries[0].id, 'src 1');
  assert.ok(Math.abs(r.entries[0].ra - 266.4167) < 1e-9);
  assert.equal(r.entries[1].id, undefined);
  assert.ok(Math.abs(r.entries[1].dec + 29.00781) < 1e-4);
});

test('parseImportText: lines with id plus position cells', () => {
  const r = parseImportText('a, 10, 20\nb\n');
  assert.deepEqual(r.entries[0], {id: 'a', ra: 10, dec: 20, line: 1});
  assert.deepEqual(r.entries[1], {id: 'b', line: 2});
});

test('parseImportText: large input (50k ids) stays linear', () => {
  const ids = Array.from({length: 50000}, (_, k) => `s${k}`);
  const t0 = Date.now();
  const r = parseImportText(ids.join('\n'));
  assert.equal(r.entries.length, 50000);
  assert.equal(r.entries[49999].id, 's49999');
  assert.ok(Date.now() - t0 < 5000);
});

// ------------------------------------------------------------------ resolveImport

function fakeCatalog(ids, ra, dec) {
  const index = new Map(ids.map((id, i) => [id, i]));
  const aliases = new Map([['ALIAS-1', 1]]);
  return {
    n: ids.length,
    resolveIds: async (list) => list.map((id) => (index.has(id) ? index.get(id) : aliases.has(id) ? aliases.get(id) : -1)),
    crossmatch: async (qra, qdec, r) => {
      const out = new Int32Array(qra.length).fill(-1);
      for (let q = 0; q < qra.length; q++) {
        let best = -1, bs = r;
        for (let i = 0; i < ra.length; i++) {
          const d = Math.hypot((qra[q] - ra[i]) * Math.cos(qdec[q] * Math.PI / 180), qdec[q] - dec[i]) * 3600;
          if (d <= bs) { bs = d; best = i; }
        }
        out[q] = best;
      }
      return out;
    },
  };
}

test('resolveImport: id first, then alias, then 0.3 arcsec position', async () => {
  const cat = fakeCatalog(['a', 'b', 'c'], [10, 20, 30], [0, 0, 0]);
  const entries = [
    {id: 'c'},
    {id: 'ALIAS-1'},
    {id: 'unknown', ra: 10 + 0.2 / 3600, dec: 0},      // id misses, position within 0.3"
    {ra: 30 + 0.5 / 3600, dec: 0},                       // 0.5": outside
    {id: 'a'},
    {id: 'zzz'},
  ];
  const res = await resolveImport(entries, {...cat, radiusArcsec: IMPORT_RADIUS_ARCSEC});
  assert.deepEqual(Array.from(res.indices), [2, 1, 0]);
  assert.equal(res.byId, 3);
  assert.equal(res.byPosition, 1);
  assert.equal(res.matched.length, 4);
  assert.deepEqual(res.missing.map(entryLabel), ['30.00014, 0.00000', 'zzz']);
  assert.equal(res.matched[2].how, 'position');
});

test('resolveImport: N=0 catalog, empty entries, missing deps', async () => {
  const empty = await resolveImport([], {});
  assert.equal(empty.indices.length, 0);
  const cat = fakeCatalog([], [], []);
  const r = await resolveImport([{id: 'x'}, {ra: 1, dec: 1}], cat);
  assert.equal(r.indices.length, 0);
  assert.equal(r.missing.length, 2);
  const noDeps = await resolveImport([{id: 'x'}], {});
  assert.equal(noDeps.missing.length, 1);
});

test('resolveImport: out-of-range and NaN indices from deps are ignored', async () => {
  const res = await resolveImport([{id: 'a'}, {id: 'b'}, {id: 'c'}, {ra: 1, dec: 1}], {
    n: 2,
    resolveIds: async () => [5, -1, 1.5],
    crossmatch: async () => Int32Array.from([1]),
  });
  assert.deepEqual(Array.from(res.indices), [1]);
  assert.equal(res.byPosition, 1);
});

test('resolveImport: N=1 catalog; duplicate matches collapse to one row', async () => {
  const cat = fakeCatalog(['only'], [5], [5]);
  const res = await resolveImport([{id: 'only'}, {ra: 5, dec: 5}], cat);
  assert.deepEqual(Array.from(res.indices), [0]);
  assert.equal(res.matched.length, 2);
  const s = importSummary(res, 2);
  assert.match(s, /Matched 2 of 2 entries/);
  assert.match(s, /1 distinct row/);
});

test('resolveImport: entries with non-finite positions are not crossmatched', async () => {
  let called = 0;
  const res = await resolveImport([{ra: NaN, dec: 1}, {ra: Infinity, dec: 0}], {
    crossmatch: async () => { called++; return new Int32Array(0); },
  });
  assert.equal(called, 0);
  assert.equal(res.missing.length, 2);
});

// ------------------------------------------------------------------ names, scopes

test('exportFileName keeps unicode letters and strips unsafe characters', () => {
  assert.equal(exportFileName('nirspec6927_src', 'csv', 'sel'), 'nirspec6927_src_selection.csv');
  assert.equal(exportFileName('a/b c', 'ds9', 'vis'), 'a_b_c_filtered.reg');
  assert.equal(exportFileName('Ωmega★', 'ids', 'all'), 'Ωmega_all.txt');
  assert.equal(exportFileName('', 'json', 'x/y'), 'export_x_y.json');
  assert.equal(exportFileName('z', 'nope', 'all'), 'z_all.csv');
  assert.deepEqual(EXPORT_FORMATS.map((f) => f.key), ['csv', 'json', 'ids', 'ds9']);
});

test('scopeCounts / defaultScope', () => {
  const st = createStore({});
  assert.deepEqual(scopeCounts(st, 'src', 10), {sel: 0, vis: null, all: 10});
  assert.equal(defaultScope(scopeCounts(st, 'src', 10)), 'all');
  st.set({'vis:src': {version: 1, count: 4}});
  assert.equal(defaultScope(scopeCounts(st, 'src', 10)), 'vis');
  st.set({'sel:src': {version: 1, count: 2}});
  assert.equal(defaultScope(scopeCounts(st, 'src', 10)), 'sel');
  assert.deepEqual(scopeCounts(st, 'src', NaN), {sel: 2, vis: 4, all: 0});
});

// ------------------------------------------------------------------ annotations

test('annotation codes, labels and keys', () => {
  assert.deepEqual(DEFAULT_CODES, ['y', 'n', 'g', 'w', 'l', 'e']);
  for (const c of DEFAULT_CODES) assert.ok(CODE_LABELS[c]);
  assert.deepEqual(CHECKLIST_KEYS, ['Target', 'Program', 'Object', 'SlitID', 'Observation', 'Visit', 'VisitGroup', 'Filename']);
  assert.equal(storageKey('arch', 'gal'), 'pk:arch:annot:gal');
  assert.deepEqual(normalizeCodes(undefined), DEFAULT_CODES);
  assert.deepEqual(normalizeCodes([]), DEFAULT_CODES);
  assert.deepEqual(normalizeCodes(['a', 'a', '', ' b ', 'with space', 'x,y', 'toolongcode', 7]), ['a', 'b', '7']);
});

test('codeForKey', () => {
  assert.equal(codeForKey('y'), 'y');
  assert.equal(codeForKey('Y'), 'y');
  assert.equal(codeForKey('Backspace'), null);
  assert.equal(codeForKey('Delete'), null);
  assert.equal(codeForKey('0'), null);
  assert.equal(codeForKey('q'), undefined);
  assert.equal(codeForKey('ArrowDown'), undefined);
  assert.equal(codeForKey(''), undefined);
  assert.equal(codeForKey(undefined), undefined);
  assert.equal(codeForKey('B', ['A', 'b']), 'b');
});

test('serialize / parse annotations; malformed storage gives an empty map', () => {
  const m = new Map([['x', {c: 'y', t: 1}], ['ü★', {c: 'e', t: 2}]]);
  const back = parseAnnotations(serializeAnnotations(m));
  assert.deepEqual(Array.from(back), Array.from(m));
  assert.equal(parseAnnotations('{bad').size, 0);
  assert.equal(parseAnnotations('').size, 0);
  assert.equal(parseAnnotations(null).size, 0);
  assert.equal(parseAnnotations('[1,2]').size, 0);
  const mixed = parseAnnotations(JSON.stringify({v: 1, codes: {a: {c: 'q'}, b: 'n', c: {c: 'g', t: 'x'}}}));
  assert.deepEqual(Array.from(mixed), [['b', {c: 'n', t: 0}], ['c', {c: 'g', t: 0}]]);
});

test('Annotations: set / toggle / clear persist to storage and bump the version', () => {
  const storage = memoryStorage();
  const versions = [];
  let t = 100;
  const a = new Annotations({portal: 'p', ds: 'g', storage, onChange: (v) => versions.push(v), now: () => t++});
  assert.equal(a.size, 0);
  assert.equal(a.get('x'), null);
  assert.equal(a.set('x', 'y'), true);
  assert.equal(a.set('x', 'y'), false, 'same code is a no-op');
  assert.equal(a.toggle('z', 'w'), 'w');
  assert.equal(a.toggle('z', 'w'), null);
  assert.equal(a.set(42, 'l'), true);
  assert.equal(a.get('42'), 'l');
  assert.throws(() => a.set('x', 'bogus'), RangeError);
  assert.deepEqual(a.counts(), {y: 1, n: 0, g: 0, w: 0, l: 1, e: 0});
  const b = new Annotations({portal: 'p', ds: 'g', storage});
  assert.deepEqual(b.entries().sort(), [['42', 'l'], ['x', 'y']]);
  const other = new Annotations({portal: 'p', ds: 'other', storage});
  assert.equal(other.size, 0, 'datasets do not share storage');
  assert.equal(a.clear(), true);
  assert.equal(a.clear(), false);
  assert.equal(storage.getItem('pk:p:annot:g'), null);
  assert.ok(versions.length >= 5);
  assert.equal(b.reload(), true);
  assert.equal(b.size, 0);
});

test('Annotations: storage errors are reported, not thrown; other errors propagate', () => {
  const quota = Object.assign(new Error('full'), {name: 'QuotaExceededError'});
  const errs = [];
  const broken = {getItem: () => null, setItem: () => { throw quota; }, removeItem: () => { throw quota; }};
  const a = new Annotations({portal: 'p', ds: 'g', storage: broken, onError: (e) => errs.push(e.name)});
  assert.equal(a.set('x', 'y'), true);
  assert.equal(a.get('x'), 'y', 'kept in memory');
  assert.deepEqual(errs, ['QuotaExceededError']);
  const bug = {getItem: () => { throw new TypeError('bug'); }, setItem() {}, removeItem() {}};
  assert.throws(() => new Annotations({storage: bug}), TypeError);
  const secure = {getItem: () => { throw Object.assign(new Error('no'), {name: 'SecurityError'}); }, setItem() {}, removeItem() {}};
  const s = new Annotations({storage: secure, onError: (e) => errs.push(e.name)});
  assert.equal(s.size, 0);
  assert.equal(errs.at(-1), 'SecurityError');
});

test('Annotations: null storage falls back to memory', () => {
  const a = new Annotations({portal: 'p', ds: 'g', storage: null});
  assert.equal(a.persistent, false);
  a.set('x', 'n');
  assert.equal(a.get('x'), 'n');
});

test('checklistFields: tags first, then the catalog row; case-insensitive; NaN/inf', () => {
  const f = checklistFields([{program: '3222', Target: 'IRAS16293'}, {Target: 'ignored', Object: 'src', Visit: NaN, VisitGroup: Infinity, SlitID: 0}, {Filename: 'jw03222_x1d.fits'}]);
  assert.deepEqual(f, {Target: 'IRAS16293', Program: '3222', Object: 'src', SlitID: '0', Observation: '', Visit: '',
    VisitGroup: 'inf', Filename: 'jw03222_x1d.fits'});
  assert.deepEqual(checklistFields(null), Object.fromEntries(CHECKLIST_KEYS.map((k) => [k, ''])));
});

test('annotationsCSV: header, sort order, escaping, skipped empty codes, round trip', () => {
  const text = annotationsCSV([
    {id: 'src10', code: 'y', fields: {Target: 'A, "quoted"', Program: 1309}},
    {id: 'src2', code: 'e', fields: {Target: 'Ωmega'}},
    {id: 'src3', code: null, fields: {}},
    {id: 'src1', code: 'n'},
  ]);
  const rows = parseCSV(text);
  assert.deepEqual(rows[0], ['id', ...CHECKLIST_KEYS, 'checklist']);
  assert.deepEqual(rows.slice(1).map((r) => r[0]), ['src1', 'src2', 'src10']);
  assert.equal(rows[3][1], 'A, "quoted"');
  assert.equal(rows[3][2], '1309');
  assert.equal(rows[2][1], 'Ωmega');
  assert.equal(rows[1].at(-1), 'n');
  assert.ok(!text.startsWith('#'), 'no comment lines');
  assert.ok(text.endsWith('\n'));
  const custom = annotationsCSV([{id: 'a', code: 'x', fields: {K: 'v'}}], {keys: ['K'], key: 'flag'});
  assert.equal(custom, 'id,K,flag\na,v,x\n');
  assert.equal(annotationsCSV([]), `id,${CHECKLIST_KEYS.join(',')},checklist\n`);
});

// ------------------------------------------------------------------ hover placement

test('placeCard: default below-right of the pointer', () => {
  assert.deepEqual(placeCard({x: 100, y: 100, w: 200, h: 100, vw: 1000, vh: 800}),
    {left: 114, top: 114, flipX: false, flipY: false});
});

test('placeCard: flips at the right and bottom edges', () => {
  const p = placeCard({x: 950, y: 750, w: 200, h: 100, vw: 1000, vh: 800});
  assert.equal(p.flipX, true);
  assert.equal(p.flipY, true);
  assert.equal(p.left, 950 - 14 - 200);
  assert.equal(p.top, 750 - 14 - 100);
});

test('placeCard: card larger than the viewport is clamped to the margin', () => {
  const p = placeCard({x: 50, y: 50, w: 2000, h: 2000, vw: 390, vh: 600});
  assert.equal(p.left, 8);
  assert.equal(p.top, 8);
  const q = placeCard({x: 200, y: 300, w: 380, h: 100, vw: 390, vh: 600});
  assert.ok(q.left >= 8 && q.left <= 390 - 8);
});

test('placeCard: never leaves the viewport for any pointer position', () => {
  for (let x = -50; x <= 1050; x += 37) {
    for (let y = -50; y <= 850; y += 41) {
      const p = placeCard({x, y, w: 240, h: 160, vw: 1000, vh: 800});
      assert.ok(p.left >= 8 && p.left + 240 <= 992, `x=${x}`);
      assert.ok(p.top >= 8 && p.top + 160 <= 792, `y=${y}`);
    }
  }
});

test('placeCard: NaN / missing inputs', () => {
  assert.deepEqual(placeCard({x: NaN, y: 10, w: 10, h: 10, vw: 100, vh: 100}), {left: 8, top: 8, flipX: false, flipY: false});
  assert.deepEqual(placeCard(), {left: 8, top: 8, flipX: false, flipY: false});
  const p = placeCard({x: 10, y: 10, w: NaN, h: undefined, vw: 100, vh: 100});
  assert.deepEqual(p, {left: 24, top: 24, flipX: false, flipY: false});
});

test('pickHoverColumns: role order, skips, preference and limit', () => {
  const cols = [
    {key: 'id', role: 'id'}, {key: 'ra', role: 'ra'}, {key: 'f1', role: 'mag'}, {key: 'note', role: 'text', enc: 'str'},
    {key: 'v', role: 'value'}, {key: 'cls', role: 'category', levels: ['a', 'b']}, {key: 'f1_err', role: 'error'},
    {key: '_src_index', role: 'value'}, {key: 'c1', role: 'color'}, {key: 'f2', role: 'mag'},
  ];
  assert.deepEqual(pickHoverColumns(cols, 4).map((c) => c.key), ['cls', 'f1', 'f2', 'c1']);
  assert.deepEqual(pickHoverColumns(cols, 2, ['v']).map((c) => c.key), ['v', 'cls']);
  assert.deepEqual(pickHoverColumns(cols, 0), []);
  assert.deepEqual(pickHoverColumns(null), []);
  assert.deepEqual(pickHoverColumns(cols, 3, ['missing', 'id']).map((c) => c.key), ['cls', 'f1', 'f2']);
});

test('hoverValueText: categories, flags, quantized steps and missing values', () => {
  assert.equal(hoverValueText(1, {role: 'category', levels: ['a', 'b']}).text, 'b');
  assert.equal(hoverValueText(NaN, {role: 'category', levels: ['a']}).text, '–');
  assert.equal(hoverValueText(1, {role: 'flag'}).text, 'yes');
  const q = hoverValueText(18.2104, {role: 'mag', enc: 'u16q', step: 0.001});
  assert.equal(q.text, '18.210');
  assert.match(q.title, /±0\.0005/);
  assert.equal(hoverValueText(NaN, {role: 'mag', enc: 'u16q', step: 0.001}).text, '–');
  assert.equal(hoverValueText(Infinity, {role: 'value', enc: 'f32'}).text, '∞');
  assert.equal(hoverValueText(1e30, {role: 'value', enc: 'f32'}).text, '1e+30');
  assert.equal(hoverValueText(2.5, null).text, '2.5');
});

// ------------------------------------------------------------------ lightbox

test('stepIndex: wraps in both directions; empty and one-item sets', () => {
  assert.equal(stepIndex(0, 1, 3), 1);
  assert.equal(stepIndex(2, 1, 3), 0);
  assert.equal(stepIndex(0, -1, 3), 2);
  assert.equal(stepIndex(0, 1, 1), 0);
  assert.equal(stepIndex(0, 1, 0), -1);
  assert.equal(stepIndex(NaN, 1, 4), 1);
  assert.equal(stepIndex(7, -1, 4), 2);
  assert.equal(stepIndex(Infinity, 1, 4), 1);
  assert.equal(stepIndex(0, 1, NaN), -1);
});

// ------------------------------------------------------------------ list

test('parseSort: strings, signs, arrays, objects and empty values', () => {
  assert.deepEqual(parseSort('f410m'), {key: 'f410m', dir: 1});
  assert.deepEqual(parseSort('-f410m'), {key: 'f410m', dir: -1});
  assert.deepEqual(parseSort('+id'), {key: 'id', dir: 1});
  assert.deepEqual(parseSort(['mag', 'desc']), {key: 'mag', dir: -1});
  assert.deepEqual(parseSort({key: 'x', dir: 'desc'}), {key: 'x', dir: -1});
  assert.equal(parseSort(''), null);
  assert.equal(parseSort('   '), null);
  assert.equal(parseSort(null), null);
  assert.equal(parseSort([]), null);
});

test('sortIndices: NaN last in both directions, ties keep stored order, infinities', () => {
  const vals = Float32Array.from([3, NaN, 1, Infinity, -Infinity, 1, NaN]);
  const asc = sortIndices(Int32Array.from([0, 1, 2, 3, 4, 5, 6]), vals, 1);
  assert.deepEqual(Array.from(asc), [4, 2, 5, 0, 3, 1, 6]);
  const desc = sortIndices(Int32Array.from([0, 1, 2, 3, 4, 5, 6]), vals, -1);
  assert.deepEqual(Array.from(desc), [3, 0, 2, 5, 4, 1, 6]);
  assert.deepEqual(Array.from(sortIndices(new Int32Array(0), vals)), []);
  assert.deepEqual(Array.from(sortIndices(Int32Array.from([1]), vals)), [1]);
});

test('sortIndices: 50k rows stay a permutation in order', () => {
  const n = 50000;
  const vals = new Float32Array(n);
  for (let i = 0; i < n; i++) vals[i] = i % 97 === 0 ? NaN : ((i * 7919) % 10007) / 3;
  const idx = new Int32Array(n);
  for (let i = 0; i < n; i++) idx[i] = i;
  sortIndices(idx, vals);
  const seen = new Uint8Array(n);
  let prev = -Infinity;
  let nanStarted = false;
  for (const i of idx) {
    seen[i] = 1;
    const v = vals[i];
    if (Number.isNaN(v)) nanStarted = true;
    else {
      assert.equal(nanStarted, false);
      assert.ok(v >= prev);
      prev = v;
    }
  }
  assert.equal(seen.reduce((a, b) => a + b, 0), n);
});

// ------------------------------------------------------------------ detail

const MAN = {
  cols: [
    {key: 'id', role: 'id', enc: 'str'},
    {key: 'f410m', role: 'mag', enc: 'u16q', step: 0.001, err: 'e_f410m', unit: 'mag', group: 'nircam'},
    {key: 'e_f410m', role: 'error', enc: 'u8log'},
    {key: 'c1', role: 'color', enc: 'f32'},
    {key: 'cls', role: 'category', enc: 'cat8', levels: ['star', 'YSO']},
    {key: 'p_yso', role: 'value', enc: 'f32'},
  ],
  groups: [{key: 'nircam', label: 'NIRCam mags', dims: ['f410m']}],
};

test('buildSections (auto): groups first, errors folded, ids/positions/_keys dropped', () => {
  const row = {id: 'a', _i: 3, ra: 1, dec: 2, f410m: 18.2, e_f410m: 0.01, c1: 0.5, cls: 'YSO', Target: 'Brick', p_yso: 0.7};
  const sec = buildSections(row, MAN);
  assert.deepEqual(sec.map((s) => s.title), ['NIRCam mags', 'Identification', 'Colours', 'Values']);
  assert.deepEqual(sec[0].keys, ['f410m']);
  assert.deepEqual(sec[1].keys, ['cls', 'Target']);
  const skipped = buildSections(row, MAN, null, ['P_YSO']);
  assert.ok(!skipped.some((s) => s.keys.includes('p_yso')));
  assert.deepEqual(buildSections({}, MAN), []);
  assert.deepEqual(buildSections(null, null), []);
});

test('buildSections (template): case-insensitive keys, roles, rest', () => {
  const row = {target: 'X', PROGRAM: 1182, Filename: 'jw01182_x1d.fits', f410m: 18, c1: 0.2, cls: 'star', extra: 'é'};
  const sec = buildSections(row, MAN, TEMPLATES.nirspec_source);
  assert.deepEqual(sec[0], {title: 'Observation', keys: ['target', 'PROGRAM', 'Filename']});
  assert.deepEqual(sec.map((s) => s.title), ['Observation', 'Photometry', 'Colours', 'Other']);
  assert.deepEqual(sec[3].keys, ['cls', 'extra']);
  const ph = buildSections(row, MAN, TEMPLATES.photometry);
  assert.deepEqual(ph.map((s) => s.title), ['Photometry', 'Colours']);
});

test('propertyText: exact vs quantized, errors, limits, missing and strings', () => {
  const m = MAN.cols[1];
  const q = propertyText({f410m: 18.2104, e_f410m: 0.0123}, 'f410m', m, false);
  assert.equal(q.text, '18.210 ± 0.012');
  assert.match(q.title, /quantized/);
  assert.equal(q.numeric, true);
  const ex = propertyText({f410m: 18.2104}, 'f410m', m, true);
  assert.equal(ex.text, '18.2104');
  const lim = propertyText({f410m: 21, _limits: {f410m: 1}}, 'f410m', m, true);
  assert.equal(lim.text, '< 21');
  assert.match(lim.title, /upper limit/);
  assert.equal(propertyText({f410m: NaN, e_f410m: 0.1}, 'f410m', m, true).text, '–');
  assert.equal(propertyText({f410m: null}, 'f410m', m, true).text, '–');
  const s = propertyText({Target: 'Sgr B2 ✓'}, 'Target', undefined, true);
  assert.equal(s.text, 'Sgr B2 ✓');
  assert.equal(s.numeric, false);
  assert.equal(propertyText({v: Infinity}, 'v', {role: 'value'}, true).text, '∞');
});

test('barFraction and substituteLink', () => {
  assert.equal(barFraction(0.25), 0.25);
  assert.equal(barFraction(40), 0.4);
  assert.equal(barFraction(-1), 0);
  assert.equal(barFraction(1e9), 1);
  assert.equal(barFraction('0.5'), 0.5);
  assert.ok(Number.isNaN(barFraction(NaN)));
  assert.ok(Number.isNaN(barFraction(Infinity)));
  assert.ok(Number.isNaN(barFraction(null)));
  assert.equal(substituteLink('spec.html#id={id}', 'a b/ü'), 'spec.html#id=a%20b%2F%C3%BC');
  assert.equal(substituteLink('x.html#id={id}&y={id}', 7), 'x.html#id=7&y=7');
  assert.equal(substituteLink('', 'a'), null);
  assert.equal(substituteLink(null, 'a'), null);
});

// ------------------------------------------------------------------ gallery

const ITEMS = [
  {id: 'a1', caption: 'PRISM Brick', tags: {grating: 'prism', program: '1182'}, row: 0, roles: {spec: {url: 'a.png'}}},
  {id: 'a2', caption: 'G395H', tags: {grating: 'g395h', program: '1182'}, roles: {spec: {url: ''}, sed: {url: 's.png'}}},
  {id: 'b10', caption: 'Ünïcode target', tags: {grating: ['prism', 'g235m'], program: '3222'}, row: 2, roles: {}},
  {id: 'b9', tags: {program: '3222'}},
  null,
];
const FACETS = [{key: 'grating', label: 'Grating', values: ['prism', 'g395h', 'g235m', 'g140m']}, {key: 'program', values: ['1182', '3222']}];

test('tagValues and passesFacets: arrays, missing tags, empty selections', () => {
  assert.deepEqual(tagValues(ITEMS[2], 'grating'), ['prism', 'g235m']);
  assert.deepEqual(tagValues(ITEMS[3], 'grating'), []);
  assert.deepEqual(tagValues(null, 'x'), []);
  assert.equal(passesFacets(ITEMS[2], {grating: ['g235m']}), true);
  assert.equal(passesFacets(ITEMS[3], {grating: ['prism']}), false);
  assert.equal(passesFacets(ITEMS[3], {grating: []}), true);
  assert.equal(passesFacets(ITEMS[0], {grating: ['g395h'], program: ['1182']}, 'grating'), true);
});

test('facetCounts: counts under the other facets, zero values kept', () => {
  const c = facetCounts(ITEMS, FACETS, {program: ['3222']});
  assert.equal(c.grating.get('prism'), 1);
  assert.equal(c.grating.get('g235m'), 1);
  assert.equal(c.grating.get('g395h'), 0);
  assert.equal(c.grating.get('g140m'), 0);
  assert.equal(c.program.get('1182'), 2);
  assert.equal(c.program.get('3222'), 2);
  const vis = facetCounts(ITEMS, FACETS, {}, (k) => k !== 0);
  assert.equal(vis.grating.get('prism'), 1);
  assert.deepEqual(facetCounts([], FACETS, {}).grating.get('prism'), 0);
});

test('filterItems: facets, text terms (unicode), pass predicate and sort', () => {
  assert.deepEqual(filterItems(ITEMS, {}), [0, 1, 2, 3]);
  assert.deepEqual(filterItems(ITEMS, {facets: {grating: ['prism']}}), [0, 2]);
  assert.deepEqual(filterItems(ITEMS, {text: 'ünïcode'}), [2]);
  assert.deepEqual(filterItems(ITEMS, {text: '1182 g395h'}), [1]);
  assert.deepEqual(filterItems(ITEMS, {pass: (k) => k % 2 === 0}), [0, 2]);
  assert.deepEqual(filterItems(ITEMS, {sort: {key: 'id', dir: 1}}), [0, 1, 3, 2]);
  assert.deepEqual(filterItems(ITEMS, {sort: {key: 'id', dir: -1}}), [2, 3, 1, 0]);
  assert.deepEqual(filterItems(ITEMS, {sort: {key: 'caption', dir: 1}}), [1, 0, 2, 3]);
  assert.deepEqual(filterItems([], {text: 'x'}), []);
});

test('gridGeometry, parseSortKey and roleFor', () => {
  const g = gridGeometry(700, 220);
  assert.equal(g.cols, 3);
  assert.ok(g.cardW * 3 + 16 <= 700);
  assert.equal(gridGeometry(0, 220).cols, 1);
  assert.equal(gridGeometry(NaN, NaN).cols, 1);
  assert.equal(gridGeometry(1e6, 1e9).cols, Math.floor((1e6 + 8) / (640 + 8)));
  assert.equal(gridGeometry(390, 220).cols, 1);
  assert.deepEqual(parseSortKey('-program'), {key: 'program', dir: -1});
  assert.equal(parseSortKey(''), null);
  assert.equal(roleFor(ITEMS[1], 'spec'), 'sed');
  assert.equal(roleFor(ITEMS[0], 'sed'), 'spec');
  assert.equal(roleFor(ITEMS[2], 'spec'), null);
  assert.equal(roleFor(null, 'spec'), null);
});

// ------------------------------------------------------------------ table

test('normalizeJSONTable: arrays of objects, {columns, rows} with arrays, bad input', () => {
  const a = normalizeJSONTable([{x: 1, _hidden: 2}, {y: 'é'}]);
  assert.deepEqual(a.columns, ['x', 'y']);
  assert.equal(a.rows.length, 2);
  const b = normalizeJSONTable({columns: [{key: 'id'}, 'v'], rows: [['a', 1], ['b', NaN]]});
  assert.deepEqual(b.columns, ['id', 'v']);
  assert.deepEqual(b.rows[0], {id: 'a', v: 1});
  const c = normalizeJSONTable({rows: [[1, 2]]});
  assert.deepEqual(c.columns, ['col0', 'col1']);
  assert.deepEqual(normalizeJSONTable([], ['q']).columns, ['q']);
  assert.throws(() => normalizeJSONTable('nope'), TypeError);
  assert.throws(() => normalizeJSONTable({a: 1}), TypeError);
});

test('sortRows: numbers, numeric-aware strings, missing last, stable', () => {
  const rows = [{v: 10, s: 'x10'}, {v: NaN, s: 'x2'}, {v: 2, s: null}, {v: -Infinity, s: 'x1'}, {v: 2, s: 'x3'}];
  assert.deepEqual(sortRows(rows, 'v').map((r) => r.s), ['x1', null, 'x3', 'x10', 'x2']);
  assert.deepEqual(sortRows(rows, 'v', -1).map((r) => r.s), ['x10', null, 'x3', 'x1', 'x2']);
  assert.deepEqual(sortRows(rows, 's').map((r) => r.s), ['x1', 'x2', 'x3', 'x10', null]);
  assert.deepEqual(sortRows([], 'v'), []);
});

// ------------------------------------------------------------------ readme / landing

test('safeUrl: allows http(s), mailto, relative and image data URLs only', () => {
  for (const u of ['https://a.b/c', 'http://x', 'mailto:a@b', 'page.html#id=1', '#top', '', 'data:image/png;base64,AAA']) {
    assert.equal(safeUrl(u), true, u);
  }
  for (const u of ['javascript:alert(1)', ' JaVaScRiPt:alert(1)', 'java\nscript:x', 'vbscript:x', 'data:text/html,<b>', 'file:///etc/passwd']) {
    assert.equal(safeUrl(u), false, u);
  }
});

test('cardList: auto drops the current page and lists related portals; explicit cards', () => {
  const portal = {
    _url: 'https://h/p/portal.json',
    pages: [{id: 'index', title: 'Overview', file: 'index.html', archetype: 'landing'},
      {id: 'map', title: 'Sky map', file: 'map.html', archetype: 'explorer'}, {id: 'bad'}],
    related: [{title: 'WD2', url: 'https://h/wd2/', auth: 'secure'}, {title: 'no url'}],
  };
  const auto = cardList(portal, 'auto', 'index');
  assert.deepEqual(auto.map((c) => c.title), ['Sky map', 'WD2']);
  assert.equal(auto[0].href, 'https://h/p/map.html');
  assert.equal(auto[1].auth, 'secure');
  assert.equal(auto[1].external, true);
  assert.deepEqual(cardList(portal, 'auto', null, 'map.html').map((c) => c.title), ['Overview', 'WD2']);
  const pick = cardList(portal, [{page: 'map', text: 'Ünïcode text'}, {href: 'other.html', title: 'Other'}, {page: 'missing'}, null]);
  assert.deepEqual(pick.map((c) => [c.title, c.text]), [['Sky map', 'Ünïcode text'], ['Other', '']]);
  assert.deepEqual(cardList(null), []);
  assert.deepEqual(cardList({pages: []}, 'auto', 'x'), []);
});

test('sourceHref: replaces any existing hash and encodes the id', () => {
  assert.equal(sourceHref('https://h/p/map.html', '6927_10038'), 'https://h/p/map.html#id=6927_10038');
  assert.equal(sourceHref('map.html#role=x', 'a b#c'), 'map.html#id=a%20b%23c');
  assert.equal(sourceHref('map.html', 'Ⅳ-α'), 'map.html#id=%E2%85%A3-%CE%B1');
});

// ------------------------------------------------------------------ review fixes

test('sortIndices: radix sort matches a comparator reference (dups, negatives, -0, inf, NaN)', () => {
  const n = 20000;
  let seed = 12345;
  const rnd = () => ((seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff);
  const vals = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    const r = rnd();
    vals[i] = r < 0.05 ? NaN : r < 0.07 ? -0 : r < 0.08 ? 0 : r < 0.085 ? Infinity : r < 0.09 ? -Infinity
      : r < 0.3 ? Math.round((rnd() - 0.5) * 20) : (rnd() - 0.5) * 1e6 * Math.pow(10, Math.round(rnd() * 20 - 10));
  }
  for (const dir of [1, -1]) {
    const ref = Array.from({length: n}, (_, i) => i).sort((a, b) => {
      const va = vals[a], vb = vals[b];
      const na = va !== va, nb = vb !== vb;
      if (na || nb) return na === nb ? a - b : na ? 1 : -1;
      if (va < vb) return -dir;
      if (va > vb) return dir;
      return a - b;
    });
    const idx = Int32Array.from({length: n}, (_, i) => i);
    sortIndices(idx, vals, dir);
    assert.deepEqual(Array.from(idx), ref);
  }
  // A subset of rows (filtered list) is sorted in place.
  const sub = Int32Array.from([9, 3, 7, 1]);
  sortIndices(sub, Float32Array.from([0, 5, 0, 2, 0, 0, 0, 2, 0, 1]), 1);
  assert.deepEqual(Array.from(sub), [9, 3, 7, 1]);
});

test('parseImportText: one-column list with an id header, headerless multi-column lines', () => {
  const a = parseImportText('id\nA\nB');
  assert.deepEqual(a.entries.map((e) => e.id), ['A', 'B']);
  const b = parseImportText('Name\r\nX\r\n');
  assert.deepEqual(b.entries.map((e) => e.id), ['X']);
  const c = parseImportText('A,B\nC,D');
  assert.deepEqual(c.entries.map((e) => e.id), ['A', 'B', 'C', 'D']);
  assert.equal(c.skipped, 0);
  const d = parseImportText('A\nB');
  assert.deepEqual(d.entries.map((e) => e.id), ['A', 'B']);
  const e = parseImportText('S1, 266.4, -28.9\nS2');
  assert.equal(e.entries[0].id, 'S1');
  assert.ok(Math.abs(e.entries[0].ra - 266.4) < 1e-9);
  assert.deepEqual(e.entries.map((x) => x.id), ['S1', 'S2']);
});
