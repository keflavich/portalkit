// Worker RPC (ARCHITECTURE.md section 6.6).
//   request  {id, op, args}
//   reply    {id, ok: true, result, stats} | {id, ok: false, error: {name, message, status?, url?}, stats}
//   progress {id, progress}            (zero or more before the reply)
//   cancel   {id, op: 'cancel', args: {target}}  aborts the handler's ctx.signal
// ArrayBuffers listed through withTransfer() are transferred; everything else is cloned, so a
// handler can return views of its caches safely. createLocalRpc() runs the same handlers in the
// calling thread (no Worker support, node tests).

class Transfer {
  constructor(value, buffers) {
    this.value = value;
    this.buffers = buffers;
  }
}

/** Mark `value` for transfer of the given buffers (typed arrays or ArrayBuffers). */
export function withTransfer(value, list) {
  const bufs = [];
  const seen = new Set();
  for (const x of list || []) {
    const b = x instanceof ArrayBuffer ? x : x && x.buffer instanceof ArrayBuffer ? x.buffer : null;
    if (b && !seen.has(b) && b.byteLength > 0) {
      seen.add(b);
      bufs.push(b);
    }
  }
  return new Transfer(value, bufs);
}

function unwrap(r) {
  return r instanceof Transfer ? {value: r.value, transfer: r.buffers} : {value: r, transfer: []};
}

export function serializeError(err) {
  if (err && typeof err === 'object') {
    const out = {name: err.name || 'Error', message: String(err.message ?? err)};
    if (err.status !== undefined) out.status = err.status;
    if (err.url !== undefined) out.url = err.url;
    return out;
  }
  return {name: 'Error', message: String(err)};
}

export function deserializeError(e) {
  const err = new Error(e && e.message ? e.message : 'worker error');
  err.name = (e && e.name) || 'Error';
  if (e && e.status !== undefined) err.status = e.status;
  if (e && e.url !== undefined) err.url = e.url;
  return err;
}

function abortErr() {
  if (typeof DOMException === 'function') return new DOMException('aborted', 'AbortError');
  const e = new Error('aborted');
  e.name = 'AbortError';
  return e;
}

/**
 * Serve `handlers` ({op: async (args, ctx) => result}) on a worker scope (or MessagePort).
 * ctx = {id, signal, progress(p)}. `getStats()` is attached to every reply.
 */
export function serve(scope, handlers, {getStats} = {}) {
  const running = new Map();
  const stats = () => (getStats ? getStats() : undefined);
  scope.onmessage = async (ev) => {
    const msg = ev.data || {};
    const {id, op, args} = msg;
    if (op === 'cancel') {
      const target = args && args.target;
      const ac = running.get(target);
      if (ac) ac.abort();
      scope.postMessage({id, ok: true, result: !!ac, stats: stats()});
      return;
    }
    const fn = handlers[op];
    if (typeof fn !== 'function') {
      scope.postMessage({id, ok: false, error: {name: 'Error', message: `unknown op ${op}`}, stats: stats()});
      return;
    }
    const ac = new AbortController();
    running.set(id, ac);
    const ctx = {id, signal: ac.signal, progress: (p) => scope.postMessage({id, progress: p})};
    let reply, transfer = [];
    try {
      const {value, transfer: t} = unwrap(await fn(args || {}, ctx));
      reply = {id, ok: true, result: value, stats: stats()};
      transfer = t;
    } catch (err) {
      // RPC boundary: every failure is reported to the caller, which rethrows it.
      reply = {id, ok: false, error: serializeError(err), stats: stats()};
    } finally {
      running.delete(id);
    }
    scope.postMessage(reply, transfer);
  };
  return {running};
}

/**
 * Client side of serve(). `port` is a Worker (or MessagePort).
 * call(op, args, {transfer, onProgress, signal}) -> Promise<result>.
 */
export function createRpcClient(port, {onStats, onProgress} = {}) {
  let next = 1;
  const pending = new Map();
  port.onmessage = (ev) => {
    const m = ev.data || {};
    const p = pending.get(m.id);
    if (m.stats && onStats) onStats(m.stats);
    if (!p) return;
    if ('progress' in m && !('ok' in m)) {
      if (p.onProgress) p.onProgress(m.progress);
      if (onProgress) onProgress(m.progress, p.op);
      return;
    }
    pending.delete(m.id);
    if (p.cleanup) p.cleanup();
    if (m.ok) p.resolve(m.result);
    else p.reject(deserializeError(m.error));
  };
  port.onerror = (ev) => {
    const err = new Error(`worker error: ${(ev && ev.message) || 'unknown'}`);
    for (const p of pending.values()) {
      if (p.cleanup) p.cleanup();
      p.reject(err);
    }
    pending.clear();
  };
  function call(op, args = {}, opts = {}) {
    const id = next++;
    return new Promise((resolve, reject) => {
      const entry = {op, resolve, reject, onProgress: opts.onProgress, cleanup: null};
      const sig = opts.signal;
      if (sig) {
        if (sig.aborted) return reject(abortErr());
        const onAbort = () => {
          pending.delete(id);
          port.postMessage({id: next++, op: 'cancel', args: {target: id}});
          reject(abortErr());
        };
        sig.addEventListener('abort', onAbort, {once: true});
        entry.cleanup = () => sig.removeEventListener('abort', onAbort);
      }
      pending.set(id, entry);
      port.postMessage({id, op, args}, opts.transfer || []);
    });
  }
  function terminate() {
    for (const p of pending.values()) {
      if (p.cleanup) p.cleanup();
      p.reject(new Error('worker terminated'));
    }
    pending.clear();
    if (typeof port.terminate === 'function') port.terminate();
  }
  return {call, terminate, get pending() {
    return pending.size;
  }};
}

/** In-thread RPC with the same call() signature (results are not cloned). */
export function createLocalRpc(handlers, {getStats, onStats, onProgress} = {}) {
  async function call(op, args = {}, opts = {}) {
    const fn = handlers[op];
    if (typeof fn !== 'function') throw new Error(`unknown op ${op}`);
    if (opts.signal && opts.signal.aborted) throw abortErr();
    const ctx = {
      id: 0,
      signal: opts.signal || new AbortController().signal,
      progress: (p) => {
        if (opts.onProgress) opts.onProgress(p);
        if (onProgress) onProgress(p, op);
      },
    };
    try {
      return unwrap(await fn(args, ctx)).value;
    } finally {
      if (getStats && onStats) onStats(getStats());
    }
  }
  return {call, terminate() {}, pending: 0};
}
