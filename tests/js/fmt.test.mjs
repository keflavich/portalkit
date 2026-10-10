// node --test tests/js/fmt.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  MISSING, stepDigits, fmtNum, fmtStep, fmtQuant, fmtPrintf, fmtValue, fmtCount, fmtBytes, fmtMs,
  fmtRA, fmtDec, fmtAngle, escapeHtml, plural,
} from '../../web/js/core/fmt.js';
import {MAG_STEP} from '../../web/js/core/constants.js';

test('stepDigits: decimals implied by the quantization step', () => {
  const cases = [
    [0.001, 3], [0.0015, 3], [0.01, 2], [0.0153, 2], [0.1, 1], [0.3, 1], [0.5, 1], [1, 0], [25, 0],
    [1e-6, 6], [1e-12, 12], [1e-15, 12], [0.000999, 4], [1.0000001, 0], [MAG_STEP, 3],
  ];
  for (const [step, want] of cases) assert.equal(stepDigits(step), want, `step ${step}`);
  // exact powers of ten that suffer floating round-off
  for (let k = 1; k <= 12; k++) assert.equal(stepDigits(Number(`1e-${k}`)), k);
  assert.equal(stepDigits(0.1 + 0.2 - 0.2), 1);
  assert.equal(stepDigits('0.01'), 2, 'numeric strings accepted');
});

test('stepDigits: invalid steps give the fallback; clamp to max', () => {
  for (const bad of [0, -1, NaN, Infinity, -Infinity, null, undefined, 'abc', {}]) {
    assert.equal(stepDigits(bad), 6, String(bad));
  }
  assert.equal(stepDigits(NaN, {fallback: 2}), 2);
  assert.equal(stepDigits(1e-9, {max: 4}), 4);
  assert.equal(stepDigits(Number.MIN_VALUE), 12);
  assert.equal(stepDigits(Number.MAX_VALUE), 0);
});

test('fmtNum: missing and non-finite', () => {
  assert.equal(MISSING, '–');
  assert.equal(fmtNum(null), MISSING);
  assert.equal(fmtNum(undefined), MISSING);
  assert.equal(fmtNum(NaN), MISSING);
  assert.equal(fmtNum(NaN, 3), MISSING);
  assert.equal(fmtNum(Infinity), '∞');
  assert.equal(fmtNum(-Infinity, 2), '-∞');
});

test('fmtNum: fixed digits, -0 normalisation, rounding', () => {
  assert.equal(fmtNum(18.2104, 3), '18.210');
  assert.equal(fmtNum(-0, 2), '0.00');
  assert.equal(fmtNum(-0.0001, 2), '0.00', 'negative values rounding to zero lose the sign');
  assert.equal(fmtNum(-0.006, 2), '-0.01');
  assert.equal(fmtNum(2.5, 0), '3');
  assert.equal(fmtNum(1.005, 50), (1.005).toFixed(20), 'digits clamped to 20');
  assert.equal(fmtNum(3.7, -2), '4', 'negative digits clamp to 0');
});

test('fmtNum: auto mode (6 significant, exponent outside [1e-4, 1e6))', () => {
  assert.equal(fmtNum(0), '0');
  assert.equal(fmtNum(-0), '0');
  assert.equal(fmtNum(1), '1');
  assert.equal(fmtNum(3.14159265), '3.14159');
  assert.equal(fmtNum(123456.7), '123457');
  assert.equal(fmtNum(999999.4), '999999');
  assert.equal(fmtNum(1e6), '1e+6');
  assert.equal(fmtNum(1.5e-5), '1.5e-5');
  assert.equal(fmtNum(-2.50e-7), '-2.5e-7');
  assert.equal(fmtNum(0.0001), '0.0001');
  assert.equal(fmtNum(1.7976931348623157e308), '1.79769e+308');
  assert.equal(fmtNum(5e-324), '4.94066e-324');
  assert.equal(fmtNum(12345.678, {sig: 3}), '12300');
  assert.equal(fmtNum(999999.9, {sig: 2}), '1000000');
  assert.equal(fmtNum(0.000123456, {sig: 2}), '0.00012');
});

test('fmtNum: strings, bigint and other types', () => {
  assert.equal(fmtNum('2.50'), '2.5');
  assert.equal(fmtNum('abc'), 'abc');
  assert.equal(fmtNum(''), '');
  assert.equal(fmtNum(12345678901234567890n), '12345678901234567890');
});

test('fmtStep / fmtQuant disclose quantization', () => {
  assert.equal(fmtStep(18.2104, 0.001), '18.210');
  assert.equal(fmtStep(18.2104, 0.0153), '18.21');
  assert.equal(fmtStep(18.6, 1), '19');
  assert.equal(fmtStep(18.21, NaN), '18.210000');
  assert.deepEqual(fmtQuant(18.2104, 0.001), {text: '18.210', title: '±0.0005 (quantized, step 0.001)'});
  assert.deepEqual(fmtQuant(1.25, '0.5'), {text: '1.3', title: '±0.25 (quantized, step 0.5)'});
  assert.deepEqual(fmtQuant(NaN, 0.001), {text: MISSING, title: 'missing'});
  assert.deepEqual(fmtQuant(3, 0), {text: '3.000000', title: ''});
});

test('fmtPrintf formats', () => {
  assert.equal(fmtPrintf(3.14159, '%.3f'), '3.142');
  assert.equal(fmtPrintf(3.14159, '.2f'), '3.14');
  assert.equal(fmtPrintf(31415.9, '%.2e'), '3.14e+4');
  assert.equal(fmtPrintf(0.000123456, '%.3g'), '0.000123');
  assert.equal(fmtPrintf(2.5, '%d'), '3');
  assert.equal(fmtPrintf(-2.5, '%i'), '-2');
  assert.equal(fmtPrintf('YSO', '%s'), 'YSO');
  assert.equal(fmtPrintf(1.5, '%8.2f'), '    1.50');
  assert.equal(fmtPrintf(1.5, '%f'), '1.500000');
  assert.equal(fmtPrintf(NaN, '%.3f'), MISSING);
  assert.equal(fmtPrintf(null, '%.3f'), MISSING);
  assert.equal(fmtPrintf(Infinity, '%.3f'), '∞');
  assert.equal(fmtPrintf('7.25', '%.1f'), '7.3', 'numeric strings parsed');
  assert.equal(fmtPrintf(1.23456789, 'garbage'), '1.23457', 'unknown format falls back');
  assert.equal(fmtPrintf(1.5, ''), '1.5');
});

test('fmtValue chooses exact, printf or quantized display', () => {
  assert.deepEqual(fmtValue(null), {text: MISSING, title: 'missing'});
  assert.deepEqual(fmtValue(NaN, {step: 0.001}), {text: MISSING, title: 'missing'});
  assert.deepEqual(fmtValue('Sgr B2 ★'), {text: 'Sgr B2 ★', title: ''});
  assert.deepEqual(fmtValue(true), {text: 'yes', title: ''});
  assert.deepEqual(fmtValue(false), {text: 'no', title: ''});
  assert.deepEqual(fmtValue(1.23456, {fmt: '%.2f', step: 0.001}), {text: '1.23', title: ''});
  assert.deepEqual(fmtValue(18.2104, {step: 0.001, enc: 'u16q'}), {text: '18.210', title: '±0.0005 (quantized, step 0.001)'});
  assert.equal(fmtValue(18.2104, {step: 0.01, enc: 'u8q'}).text, '18.21');
  assert.deepEqual(fmtValue(18.2104, {step: 0.001}, {exact: true}), {text: '18.2104', title: ''});
  assert.deepEqual(fmtValue(18.2104, {step: 0.001, enc: 'f32'}), {text: '18.2104', title: ''});
});

test('fmtCount / plural', () => {
  assert.equal(fmtCount(0), '0');
  assert.equal(fmtCount(999), '999');
  assert.equal(fmtCount(1000), '1,000');
  assert.equal(fmtCount(464489), '464,489');
  assert.equal(fmtCount(3_740_000), '3,740,000');
  assert.equal(fmtCount(-1234567.6), '-1,234,568');
  assert.equal(fmtCount('2500'), '2,500');
  assert.equal(fmtCount(NaN), MISSING);
  assert.equal(fmtCount(null), MISSING);
  assert.equal(fmtCount(Infinity), MISSING);
  assert.equal(plural(1, 'source'), '1 source');
  assert.equal(plural(0, 'source'), '0 sources');
  assert.equal(plural(2000, 'spectrum', 'spectra'), '2,000 spectra');
});

test('fmtBytes / fmtMs', () => {
  assert.equal(fmtBytes(0), '0 B');
  assert.equal(fmtBytes(999), '999 B');
  assert.equal(fmtBytes(1000), '1 kB');
  assert.equal(fmtBytes(1536), '1.5 kB');
  assert.equal(fmtBytes(200000), '200 kB');
  assert.equal(fmtBytes(2_097_152), '2.1 MB');
  assert.equal(fmtBytes(5e12), '5 TB');
  assert.equal(fmtBytes(5e18), '5000000 TB');
  assert.equal(fmtBytes(NaN), MISSING);
  assert.equal(fmtMs(12.3), '12 ms');
  assert.equal(fmtMs(999.4), '999 ms');
  assert.equal(fmtMs(2345), '2.35 s');
  assert.equal(fmtMs(1000), '1 s');
  assert.equal(fmtMs(NaN), MISSING);
});

test('fmtRA / fmtDec sexagesimal with carry and wrapping', () => {
  assert.equal(fmtRA(0), '00:00:00.00');
  assert.equal(fmtRA(266.41683), '17:45:40.04');
  assert.equal(fmtRA(-15), '23:00:00.00', 'negative RA wraps');
  assert.equal(fmtRA(375), '01:00:00.00');
  assert.equal(fmtRA(359.9999999), '00:00:00.00', 'carry into 24h wraps to 0');
  assert.equal(fmtRA(15 * (59.9999999 / 3600)), '00:01:00.00', 'seconds carry into minutes');
  assert.equal(fmtRA(266.41683, 0), '17:45:40');
  assert.equal(fmtDec(-29.00781), '−29:00:28.1');
  assert.equal(fmtDec(0), '+00:00:00.0');
  assert.equal(fmtDec(-0.5), '−00:30:00.0');
  assert.equal(fmtDec(89.99999999), '+90:00:00.0');
  assert.equal(fmtDec(10.5, 2), '+10:30:00.00');
  assert.equal(fmtRA(NaN), MISSING);
  assert.equal(fmtDec(Infinity), MISSING);
});

test('fmtAngle picks degrees, arcminutes or arcseconds', () => {
  assert.equal(fmtAngle(2), '2°');
  assert.equal(fmtAngle(0.5), '30′');
  assert.equal(fmtAngle(0.001), '3.6″');
  assert.equal(fmtAngle(-0.25), '-15′');
  assert.equal(fmtAngle(0), '0″');
  assert.equal(fmtAngle(NaN), MISSING);
});

test('escapeHtml', () => {
  assert.equal(escapeHtml('<a href="x">O\'Neil & co</a>'), '&lt;a href=&quot;x&quot;&gt;O&#39;Neil &amp; co&lt;/a&gt;');
  assert.equal(escapeHtml(null), '');
  assert.equal(escapeHtml(undefined), '');
  assert.equal(escapeHtml(5), '5');
  assert.equal(escapeHtml('日本 😀'), '日本 😀');
});

test('fmtStep: tiny values with tiny steps use exponent form, not zeros', () => {
  assert.equal(fmtStep(3.2e-14, 1e-16), '3.20e-14');
  assert.equal(fmtQuant(3.2e-14, 1e-16).text, '3.20e-14');
  assert.equal(fmtStep(-3.2e-14, 1e-16), '-3.20e-14');
  assert.equal(fmtStep(5e-5, 1e-8), '5.000e-5');
  assert.equal(fmtStep(5e-5, 1e-6), '0.000050', 'six decimals or fewer stay fixed');
  assert.equal(fmtStep(0, 1e-16), fmtNum(0, 12));
  assert.equal(fmtStep(18.2104, 0.001), '18.210');
});
