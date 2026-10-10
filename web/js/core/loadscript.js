// Classic-script loader with ordered fallbacks (cdn_fallback, ARCHITECTURE.md section 8.2).
// sky/aladin.js calls loadScript([cdn, mirror], {timeout: ALADIN_TIMEOUT_MS, test: () => !!window.A}).
// Each URL is tried in order; a URL fails on error, on timeout, or when `test()` is false after
// load. The returned promise resolves {url, index} or rejects with LoadScriptError listing every
// attempt. Results are cached per URL list, so a second caller shares the same promise.

import {ALADIN_TIMEOUT_MS} from './constants.js';

export class LoadScriptError extends Error {
  constructor(attempts) {
    super(`all script sources failed: ${attempts.map((a) => `${a.url} (${a.reason})`).join('; ')}`);
    this.name = 'LoadScriptError';
    this.attempts = attempts;
  }
}

const cache = new Map();

function loadOne(url, timeout, attrs) {
  return new Promise((resolve) => {
    const s = document.createElement('script');
    let done = false;
    const finish = (ok, reason) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      s.onload = null;
      s.onerror = null;
      if (!ok) s.remove();
      resolve({ok, reason});
    };
    const timer = setTimeout(() => finish(false, `timeout ${timeout} ms`), timeout);
    s.onload = () => finish(true, 'loaded');
    s.onerror = () => finish(false, 'error');
    s.async = true;
    s.src = url;
    for (const [k, v] of Object.entries(attrs || {})) s.setAttribute(k, v);
    document.head.appendChild(s);
  });
}

/**
 * @param {string|string[]} urls     tried in order
 * @param {{timeout?: number, test?: () => boolean, attrs?: object, nocache?: boolean}} [opts]
 * @returns {Promise<{url: string, index: number}>}
 */
export function loadScript(urls, opts = {}) {
  const list = (Array.isArray(urls) ? urls : [urls]).filter(Boolean);
  const key = list.join('\n');
  if (!opts.nocache && cache.has(key)) return cache.get(key);
  const timeout = opts.timeout ?? ALADIN_TIMEOUT_MS;
  const p = (async () => {
    const attempts = [];
    if (opts.test && opts.test()) return {url: null, index: -1};
    for (let i = 0; i < list.length; i++) {
      const r = await loadOne(list[i], timeout, opts.attrs);
      if (r.ok && (!opts.test || opts.test())) return {url: list[i], index: i};
      attempts.push({url: list[i], reason: r.ok ? 'loaded but test() failed' : r.reason});
    }
    throw new LoadScriptError(attempts);
  })();
  if (!opts.nocache) {
    cache.set(key, p);
    p.catch(() => cache.delete(key));   // allow a retry after failure
  }
  return p;
}

/**
 * Add a stylesheet once (by resolved href). Resolves true when loaded (or already present),
 * false on error. opts.media sets the link's media attribute (e.g. 'print').
 */
export function loadCSS(href, opts = {}) {
  if (typeof document === 'undefined') return Promise.resolve(false);
  const abs = new URL(href, document.baseURI).href;
  for (const l of document.querySelectorAll('link[rel="stylesheet"]')) {
    if (l.href === abs) return Promise.resolve(true);
  }
  return new Promise((resolve) => {
    const l = document.createElement('link');
    l.rel = 'stylesheet';
    if (opts.media) l.media = opts.media;
    l.href = abs;
    l.onload = () => resolve(true);
    l.onerror = () => resolve(false);
    document.head.appendChild(l);
  });
}
