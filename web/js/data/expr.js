// Linear expressions over stored columns (ARCHITECTURE.md section 4.4). Pure module.
// Python twin: portalkit/expr.py; shared vectors: portalkit/data/expr_vectors.json.
//
// Grammar: expr := term (('+' | '-') term)*,  term := key | number | number '*' key.
// Keys match [A-Za-z_][A-Za-z0-9_.]*; numbers match [0-9]+(.[0-9]*)?([eE][+-]?[0-9]+)?.
// Whitespace is " \t\r\n" only. No unary sign, no key*number, no parentheses.

const WS = ' \t\r\n';
const KEY_RE = /^[A-Za-z_][A-Za-z0-9_.]*$/;
const KEY_TOKEN = /[A-Za-z_][A-Za-z0-9_.]*/y;
const NUM_TOKEN = /[0-9]+(?:\.[0-9]*)?(?:[eE][+-]?[0-9]+)?/y;

export class ExprError extends SyntaxError {
  constructor(expr, pos, msg) {
    super(`${msg} at position ${pos} in expression ${JSON.stringify(expr)}`);
    this.name = 'ExprError';
    this.expr = expr;
    this.pos = pos;
  }
}

/** True when `s` is a syntactically valid stored-column key. */
export function isKey(s) {
  return typeof s === 'string' && KEY_RE.test(s);
}

function skipWs(s, i) {
  while (i < s.length && WS.includes(s[i])) i++;
  return i;
}

function isDigit(ch) {
  return ch >= '0' && ch <= '9';
}

function isKeyStart(ch) {
  return (ch >= 'A' && ch <= 'Z') || (ch >= 'a' && ch <= 'z') || ch === '_';
}

function matchAt(re, s, i) {
  re.lastIndex = i;
  const m = re.exec(s);
  return m ? m[0] : null;
}

function parseTerm(expr, i) {
  const n = expr.length;
  if (i >= n) throw new ExprError(expr, i, 'expected a term');
  const ch = expr[i];
  if (isDigit(ch)) {
    const tok = matchAt(NUM_TOKEN, expr, i);
    const v = Number(tok);
    if (!Number.isFinite(v)) throw new ExprError(expr, i, 'number out of range');
    const j = i + tok.length;
    let k = skipWs(expr, j);
    if (k < n && expr[k] === '*') {
      k = skipWs(expr, k + 1);
      const key = k < n ? matchAt(KEY_TOKEN, expr, k) : null;
      if (key === null) throw new ExprError(expr, k, "expected a column key after '*'");
      return {key, coeff: v, end: k + key.length};
    }
    if (j < n && (isKeyStart(expr[j]) || expr[j] === '.')) {
      throw new ExprError(expr, j, "a number must be followed by '*' before a key");
    }
    return {key: null, coeff: v, end: j};
  }
  if (isKeyStart(ch)) {
    const key = matchAt(KEY_TOKEN, expr, i);
    return {key, coeff: 1, end: i + key.length};
  }
  if (ch === '+' || ch === '-') throw new ExprError(expr, i, 'unary sign is not allowed (write 0-x)');
  throw new ExprError(expr, i, `unexpected character ${JSON.stringify(ch)}`);
}

/**
 * Compile `expr` to {w: {key: coeff} (null prototype), c, keys (first-appearance order)}.
 * Throws ExprError when the text does not match the grammar.
 */
export function compile(expr) {
  if (typeof expr !== 'string') throw new TypeError(`expression must be a string, got ${typeof expr}`);
  const w = Object.create(null);
  const keys = [];
  let c = 0;
  const n = expr.length;
  let i = skipWs(expr, 0);
  if (i >= n) throw new ExprError(expr, i, 'empty expression');
  let sign = 1;
  for (;;) {
    const t = parseTerm(expr, i);
    const coeff = t.coeff * sign;
    if (t.key === null) c += coeff;
    else {
      if (!(t.key in w)) {
        w[t.key] = 0;
        keys.push(t.key);
      }
      w[t.key] += coeff;
    }
    i = skipWs(expr, t.end);
    if (i >= n) break;
    const op = expr[i];
    if (op !== '+' && op !== '-') throw new ExprError(expr, i, `expected '+' or '-', got ${JSON.stringify(op)}`);
    sign = op === '+' ? 1 : -1;
    i = skipWs(expr, i + 1);
    if (i >= n) throw new ExprError(expr, i, `expected a term after '${op}'`);
  }
  if (!Number.isFinite(c) || keys.some((k) => !Number.isFinite(w[k]))) {
    throw new ExprError(expr, n, 'coefficients overflow');
  }
  c += 0; // normalise -0
  for (const k of keys) w[k] += 0;
  return {w, c, keys};
}

const cache = new Map();

/** compile() with a small memo (expressions are immutable strings). Returned objects are shared. */
export function compileCached(expr) {
  let r = cache.get(expr);
  if (r === undefined) {
    r = compile(expr);
    if (cache.size > 512) cache.clear();
    cache.set(expr, r);
  }
  return r;
}

/** True when `expr` is a string that matches the grammar. */
export function isValid(expr) {
  if (typeof expr !== 'string') return false;
  try {
    compile(expr);
  } catch (err) {
    if (err instanceof ExprError) return false;
    throw err;
  }
  return true;
}

/** Base column keys of `expr` in order of first appearance. */
export function baseKeys(expr) {
  return compileCached(expr).keys.slice();
}

/**
 * Evaluate a compiled expression (or expression text) over column arrays.
 * `cols` maps base keys to arrays of length n (Float32Array or plain arrays; null and any
 * non-finite value are missing). The result is a Float32Array with NaN where any base is missing.
 */
export function evaluate(compiled, cols, n) {
  const comp = typeof compiled === 'string' ? compile(compiled) : compiled;
  const keys = comp.keys || Object.keys(comp.w);
  const c = +comp.c || 0;
  if (n === undefined || n === null) {
    if (!keys.length) throw new Error('n is required for a constant expression');
    n = cols[keys[0]].length;
  }
  const acc = new Float64Array(n).fill(c);
  const bad = new Uint8Array(n);
  for (const k of keys) {
    const v = cols[k];
    if (v === undefined || v === null) throw new Error(`column ${JSON.stringify(k)} not supplied`);
    if (v.length !== n) throw new Error(`column ${JSON.stringify(k)} has length ${v.length}, expected ${n}`);
    const wk = +comp.w[k];
    for (let i = 0; i < n; i++) {
      const x = v[i];
      if (x === null || !Number.isFinite(x)) bad[i] = 1;
      else acc[i] += wk * x;
    }
  }
  const out = new Float32Array(n);
  for (let i = 0; i < n; i++) out[i] = bad[i] ? NaN : acc[i];
  return out;
}
