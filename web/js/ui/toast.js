// Toast notifications (docs/js.md section 7): store event `toast` {level, text, ms?}.
// Stacked bottom-right (bottom-centre on phones), auto-dismissed, announced through an
// aria-live region; errors stay longer and use role="alert". A repeated text within a few
// seconds bumps a counter on the existing toast instead of stacking a copy.

const DEFAULT_MS = {info: 4000, warn: 7000, error: 10000};
const MAX_TOASTS = 4;
const REPEAT_MS = 5000;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

export function install(app) {
  const region = el('div', 'pk-toasts');
  region.setAttribute('aria-live', 'polite');
  region.setAttribute('aria-relevant', 'additions');
  document.body.append(region);
  const live = [];   // {node, text, level, timer, count, t}

  function dismiss(entry) {
    const k = live.indexOf(entry);
    if (k >= 0) live.splice(k, 1);
    clearTimeout(entry.timer);
    entry.node.classList.add('pk-toast-out');
    setTimeout(() => entry.node.remove(), 180);
  }

  function arm(entry, ms) {
    clearTimeout(entry.timer);
    if (ms > 0) entry.timer = setTimeout(() => dismiss(entry), ms);
  }

  function show(payload) {
    const p = payload && typeof payload === 'object' ? payload : {text: String(payload ?? '')};
    const level = ['info', 'warn', 'error'].includes(p.level) ? p.level : 'info';
    const text = String(p.text ?? '').trim();
    if (!text) return;
    const ms = Number.isFinite(p.ms) ? p.ms : DEFAULT_MS[level];
    const now = Date.now();
    const same = live.find((e) => e.text === text && e.level === level && now - e.t < REPEAT_MS);
    if (same) {
      same.count += 1;
      same.t = now;
      same.badge.textContent = `×${same.count}`;
      same.badge.hidden = false;
      arm(same, ms);
      return;
    }
    const node = el('div', `pk-toast pk-toast-${level}`);
    node.setAttribute('role', level === 'error' ? 'alert' : 'status');
    const msg = el('span', 'pk-toast-text', text);
    const badge = el('span', 'pk-toast-count');
    badge.hidden = true;
    const close = el('button', 'pk-toast-close', '×');
    close.type = 'button';
    close.setAttribute('aria-label', 'Dismiss');
    node.append(msg, badge, close);
    const entry = {node, text, level, timer: 0, count: 1, t: now, badge};
    close.addEventListener('click', () => dismiss(entry));
    node.addEventListener('mouseenter', () => clearTimeout(entry.timer));
    node.addEventListener('mouseleave', () => arm(entry, Math.min(ms, 3000)));
    region.append(node);
    live.push(entry);
    arm(entry, ms);
    while (live.length > MAX_TOASTS) dismiss(live[0]);
  }

  app.store.onEvent('toast', show);
  return {show};
}
