// Cross-page carry (ARCHITECTURE.md section 6.5).
// - stashCarry/takeCarry: sessionStorage 'pk:carry:<portal>', read-once, max age 60 s.
// - broadcastFocus/onBroadcastFocus: BroadcastChannel 'pk:<portal>' when available.
// Every storage access is wrapped in try/catch (private windows and blocked storage throw).

export const CARRY_MAX_AGE_MS = 60000;

function storage() {
  try {
    return typeof sessionStorage !== 'undefined' ? sessionStorage : null;
  } catch (err) {
    // Accessing sessionStorage throws SecurityError when storage is blocked.
    if (err && err.name === 'SecurityError') return null;
    throw err;
  }
}

function carryKey(portal) {
  return `pk:carry:${portal}`;
}

/** Store {ds, focusId?, sel?} for the next page load of the same portal. */
export function stashCarry(portal, payload) {
  const s = storage();
  if (!s || !payload) return;
  const rec = {ds: payload.ds, t: Date.now()};
  if (payload.focusId !== undefined && payload.focusId !== null) rec.focusId = String(payload.focusId);
  if (payload.sel) rec.sel = String(payload.sel);
  try {
    s.setItem(carryKey(portal), JSON.stringify(rec));
  } catch (err) {
    // QuotaExceededError / SecurityError: the carry is a convenience; drop it.
    if (err && (err.name === 'QuotaExceededError' || err.name === 'SecurityError')) return;
    throw err;
  }
}

/** Read and remove the carry; null when absent, malformed or older than 60 s. */
export function takeCarry(portal, now = Date.now()) {
  const s = storage();
  if (!s) return null;
  let raw = null;
  try {
    raw = s.getItem(carryKey(portal));
    s.removeItem(carryKey(portal));
  } catch (err) {
    if (err && err.name === 'SecurityError') return null;
    throw err;
  }
  if (!raw) return null;
  let rec;
  try {
    rec = JSON.parse(raw);
  } catch (err) {
    if (err instanceof SyntaxError) return null;
    throw err;
  }
  if (!rec || typeof rec !== 'object' || typeof rec.ds !== 'string') return null;
  if (!(typeof rec.t === 'number') || now - rec.t > CARRY_MAX_AGE_MS || now - rec.t < -CARRY_MAX_AGE_MS) return null;
  const out = {ds: rec.ds};
  if (rec.focusId !== undefined) out.focusId = rec.focusId;
  if (rec.sel) out.sel = rec.sel;
  return out;
}

const channels = new Map();

function channel(portal) {
  if (typeof BroadcastChannel !== 'function') return null;
  const name = `pk:${portal}`;
  if (!channels.has(name)) channels.set(name, new BroadcastChannel(name));
  return channels.get(name);
}

/**
 * Tell other open pages of the same portal which source is focused. `ds` (the focus dataset)
 * lets receivers ignore ids from datasets they do not show.
 */
export function broadcastFocus(portal, id, ds) {
  const ch = channel(portal);
  if (!ch) return;
  const msg = {type: 'focus', id: String(id), t: Date.now()};
  if (ds !== undefined && ds !== null) msg.ds = String(ds);
  ch.postMessage(msg);
}

/** Listen for focus broadcasts from other pages: fn(id, ds|null); returns an unsubscribe function. */
export function onBroadcastFocus(portal, fn) {
  const ch = channel(portal);
  if (!ch) return () => {};
  const handler = (ev) => {
    const d = ev && ev.data;
    if (d && d.type === 'focus' && typeof d.id === 'string') fn(d.id, typeof d.ds === 'string' ? d.ds : null);
  };
  ch.addEventListener('message', handler);
  return () => ch.removeEventListener('message', handler);
}
