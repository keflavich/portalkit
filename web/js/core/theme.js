// Theme handling (ARCHITECTURE.md section 12).
// - set('auto'|'light'|'dark'): persists localStorage['pk:theme'] (try/catch) and sets
//   <html data-theme>. 'auto' follows prefers-color-scheme through the CSS media query.
// - toggle(): switches to the opposite of the effective theme.
// - tokens(): computed CSS token values, cached per effective theme.
// - bindTheme(store): mirrors the mode into store key `theme` and emits store event
//   'theme:change' {mode, effective} on every effective change (including OS changes in auto).
//   An OS scheme change while in 'auto' leaves the value 'auto' but still notifies the store's
//   `theme` subscribers, so panes that repaint on `theme` (section 6.2) pick up the new palette.
//
// The shell's inline pre-script sets data-theme before CSS loads; this module reads the same key.

export const THEME_KEY = 'pk:theme';
export const MODES = Object.freeze(['auto', 'light', 'dark']);

const TOKEN_NAMES = [
  'bg', 'bg-2', 'bg-3', 'fg', 'fg-2', 'fg-3', 'border', 'accent', 'accent-fg', 'focus',
  'sel', 'hover', 'neutral', 'danger', 'warn', 'ok', 'plot-bg', 'plot-grid', 'plot-axis',
  'density-cmap', 'font', 'mono', 'fs-0', 'fs-1', 'fs-2', 'fs-3', 'fs-4',
  'sp-1', 'sp-2', 'sp-3', 'sp-4', 'sp-5', 'radius', 'shadow', 'splitter', 'header-h',
];

const listeners = new Set();
let cache = null;
let mq = null;

function doc() {
  return typeof document !== 'undefined' ? document : null;
}

function readStored() {
  try {
    const v = typeof localStorage !== 'undefined' ? localStorage.getItem(THEME_KEY) : null;
    return MODES.includes(v) ? v : null;
  } catch (err) {
    if (err && err.name === 'SecurityError') return null;
    throw err;
  }
}

function writeStored(mode) {
  try {
    if (typeof localStorage === 'undefined') return;
    if (mode === 'auto') localStorage.removeItem(THEME_KEY);
    else localStorage.setItem(THEME_KEY, mode);
  } catch (err) {
    if (err && (err.name === 'SecurityError' || err.name === 'QuotaExceededError')) return;
    throw err;
  }
}

function media() {
  if (!mq && typeof matchMedia === 'function') mq = matchMedia('(prefers-color-scheme: dark)');
  return mq;
}

/** Current mode: <html data-theme>, else stored, else 'auto'. */
export function get() {
  const d = doc();
  const attr = d && d.documentElement.getAttribute('data-theme');
  if (MODES.includes(attr)) return attr;
  return readStored() || 'auto';
}

/** 'light' | 'dark' after resolving 'auto' through prefers-color-scheme. */
export function effective(mode = get()) {
  if (mode === 'light' || mode === 'dark') return mode;
  const m = media();
  return m && m.matches ? 'dark' : 'light';
}

function notify() {
  cache = null;
  const info = {mode: get(), effective: effective()};
  for (const fn of Array.from(listeners)) fn(info);
}

/** Set the theme mode; persists and updates <html data-theme>. */
export function set(mode) {
  if (!MODES.includes(mode)) throw new TypeError(`unknown theme mode: ${mode}`);
  const d = doc();
  if (d) d.documentElement.setAttribute('data-theme', mode);
  writeStored(mode);
  notify();
}

/** Flip to the opposite of the effective theme. Returns the new mode. */
export function toggle() {
  const next = effective() === 'dark' ? 'light' : 'dark';
  set(next);
  return next;
}

/** Subscribe to theme changes ({mode, effective}); returns unsubscribe. */
export function onChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function camel(name) {
  return name.replace(/-(\w)/g, (_, c) => c.toUpperCase());
}

/** '#rrggbb' | '#rgb' | 'rgb(r,g,b)' -> [r,g,b] in 0..1; null when unparseable. */
export function parseColor(s) {
  const t = String(s || '').trim();
  let m = /^#([0-9a-f]{3})$/i.exec(t);
  if (m) return m[1].split('').map((h) => parseInt(h + h, 16) / 255);
  m = /^#([0-9a-f]{6})([0-9a-f]{2})?$/i.exec(t);
  if (m) return [0, 2, 4].map((i) => parseInt(m[1].slice(i, i + 2), 16) / 255);
  m = /^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)/i.exec(t);
  if (m) return [m[1], m[2], m[3]].map((v) => Math.max(0, Math.min(255, Number(v))) / 255);
  return null;
}

/**
 * Computed token values, cached per effective theme:
 *   {theme:'light'|'dark', mode, vars:{'--pk-bg':..}, bg, bg2, ..., plotBg, plotGrid, plotAxis,
 *    sel, neutral, densityCmap, cat:[10 colours], rgb(name) -> [r,g,b] 0..1,
 *    selRGB, neutralRGB}
 */
export function tokens() {
  const eff = effective();
  if (cache && cache.theme === eff) return cache;
  const d = doc();
  const cs = d && typeof getComputedStyle === 'function' ? getComputedStyle(d.documentElement) : null;
  const read = (n) => (cs ? cs.getPropertyValue(n).trim() : '');
  const vars = {};
  const t = {theme: eff, mode: get(), vars};
  for (const name of TOKEN_NAMES) {
    const v = read(`--pk-${name}`);
    vars[`--pk-${name}`] = v;
    t[camel(name)] = v;
  }
  t.densityCmap = String(t.densityCmap || 'magma').replace(/["']/g, '') || 'magma';
  t.cat = [];
  for (let i = 0; i < 10; i++) {
    const v = read(`--pk-cat-${i}`);
    vars[`--pk-cat-${i}`] = v;
    t.cat.push(v);
  }
  t.rgb = (name) => parseColor(vars[name.startsWith('--') ? name : `--pk-${name}`]);
  t.selRGB = t.rgb('sel') || [0.95, 0.7, 0.48];
  t.neutralRGB = t.rgb('neutral') || [0.29, 0.33, 0.39];
  cache = Object.freeze(t);
  return cache;
}

/**
 * Wire the theme into the store: store `theme` mirrors the mode; OS changes while in 'auto'
 * and set()/toggle() emit 'theme:change'. Returns an unbind function.
 */
export function bindTheme(store) {
  if (store.get('theme') !== get()) store.set({theme: get()}, {source: 'theme'});
  let lastEffective = effective();
  const off = onChange((info) => {
    const effChanged = info.effective !== lastEffective;
    lastEffective = info.effective;
    if (store.get('theme') === info.mode) {
      if (!effChanged || typeof store.batch !== 'function') {
        store.emit('theme:change', info);
        return;
      }
      // Same mode, new effective theme (OS change in auto): notify `theme` subscribers with
      // the value unchanged. Inside one batch the listeners only see the final value.
      store.batch(() => {
        store.set({theme: null}, {source: 'theme'});
        store.set({theme: info.mode}, {source: 'theme'});
      });
    } else {
      store.set({theme: info.mode}, {source: 'theme'});
    }
    store.emit('theme:change', info);
  });
  const offStore = store.on(['theme'], (changed, st, sources) => {
    if (sources.has('theme')) return;
    const v = st.get('theme');
    if (MODES.includes(v) && v !== get()) set(v);
  }, {sync: true});
  const m = media();
  const onMedia = () => { if (get() === 'auto') notify(); };
  if (m && m.addEventListener) m.addEventListener('change', onMedia);
  return () => {
    off();
    offStore();
    if (m && m.removeEventListener) m.removeEventListener('change', onMedia);
  };
}

/** A small toggle button (sun/moon) bound to toggle(); returns the element. */
export function makeToggleButton() {
  const d = doc();
  const b = d.createElement('button');
  b.type = 'button';
  b.className = 'pk-btn pk-icon-btn pk-theme-toggle';
  const label = () => {
    const eff = effective();
    b.setAttribute('aria-label', eff === 'dark' ? 'Switch to light theme' : 'Switch to dark theme');
    b.title = b.getAttribute('aria-label');
    b.textContent = eff === 'dark' ? '☀' : '☾';
  };
  label();
  b.addEventListener('click', () => toggle());
  onChange(label);
  const m = media();
  if (m && m.addEventListener) m.addEventListener('change', label);
  return b;
}
