// Network helpers shared by the hub, the record store and the data worker (ARCHITECTURE.md 5.1).
// - gzip-magic check before inflating (.bin files are gzip inside; foreign .gz may arrive decoded)
// - stable-name documents (manifest.json, index.json, ...) are fetched with cache:'no-cache'
// - counters {fetches, bytes, decodeMs} feed Hub.stats() / testhooks
// No DOM access: usable in workers and under node (inject a fetch with setFetch for tests).

import {gunzip, isGzip, asU8} from './codec.js';

export class FetchError extends Error {
  constructor(url, status, message) {
    super(message || `fetch ${url} failed${status ? ` (HTTP ${status})` : ''}`);
    this.name = 'FetchError';
    this.url = String(url);
    this.status = status | 0;
  }
}

const stats = {fetches: 0, bytes: 0, decodeMs: 0};
let fetchImpl = null;

/** Replace the fetch implementation (tests, service shims). null restores globalThis.fetch. */
export function setFetch(fn) {
  fetchImpl = fn || null;
}

function getFetch(opts) {
  const f = (opts && opts.fetch) || fetchImpl || globalThis.fetch;
  if (typeof f !== 'function') throw new FetchError('', 0, 'no fetch implementation available');
  return f;
}

/** Cumulative counters of this thread. */
export function getStats() {
  return {fetches: stats.fetches, bytes: stats.bytes, decodeMs: stats.decodeMs};
}

export function resetStats() {
  stats.fetches = 0;
  stats.bytes = 0;
  stats.decodeMs = 0;
}

/** Add decode time measured elsewhere (column decoding in the worker). */
export function addDecodeMs(ms) {
  if (Number.isFinite(ms) && ms > 0) stats.decodeMs += ms;
}

export function now() {
  return typeof performance !== 'undefined' && performance.now ? performance.now() : Date.now();
}

// `<stem>.<h8>.<ext>` or a bare `<h8>.<ext>` basename (record files `r/<h8>.bin`, §5.6).
const HASHED = /(?:^|[./])[0-9a-f]{8}\.[A-Za-z0-9]+$/;

/** Stable (un-hashed) names are revalidated; content-hashed names may come from cache. */
export function cacheModeFor(url) {
  const path = String(url).split(/[?#]/)[0];
  return HASHED.test(path) ? 'default' : 'no-cache';
}

/**
 * Fetch raw bytes (no inflate). opts: {signal, cache, fetch}.
 * Throws FetchError for HTTP errors and network failures; AbortError propagates unchanged.
 */
export async function fetchRaw(url, opts = {}) {
  const f = getFetch(opts);
  const u = String(url);
  let res;
  try {
    res = await f(u, {signal: opts.signal, cache: opts.cache || cacheModeFor(u), credentials: 'same-origin'});
  } catch (err) {
    if (err && err.name === 'AbortError') throw err;
    if (err instanceof TypeError) throw new FetchError(u, 0, `network error fetching ${u}: ${err.message}`);
    throw err;
  }
  if (!res || !res.ok) throw new FetchError(u, res ? res.status : 0);
  const buf = asU8(await res.arrayBuffer());
  stats.fetches += 1;
  stats.bytes += buf.length;
  return buf;
}

/** Fetch bytes and inflate them when they start with the gzip magic. */
export async function fetchBytes(url, opts = {}) {
  const raw = await fetchRaw(url, opts);
  if (!isGzip(raw)) return raw;
  const t0 = now();
  const out = await gunzip(raw);
  stats.decodeMs += now() - t0;
  return out;
}

const utf8 = new TextDecoder('utf-8');

/** Fetch a JSON document (gzip-magic aware). */
export async function fetchJSON(url, opts = {}) {
  const b = await fetchBytes(url, opts);
  try {
    return JSON.parse(utf8.decode(b));
  } catch (err) {
    if (err instanceof SyntaxError) throw new FetchError(url, 200, `invalid JSON in ${url}: ${err.message}`);
    throw err;
  }
}

/** Resolve `rel` against the URL of a document (`base`), returning a string. */
export function joinUrl(base, rel) {
  return new URL(rel, base).toString();
}

/** Abort error matching DOMException name 'AbortError'. */
export function abortError(msg = 'aborted') {
  if (typeof DOMException === 'function') return new DOMException(msg, 'AbortError');
  const e = new Error(msg);
  e.name = 'AbortError';
  return e;
}

export function throwIfAborted(signal) {
  if (signal && signal.aborted) throw abortError();
}
