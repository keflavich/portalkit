// Pane registry: pane type -> module path (ARCHITECTURE.md sections 3 and 6.8).
// Paths are relative to this module, so the viewer works from any asset base
// ({assets}js/core/registry.js) without a bundler. Pane CSS is attached on first load.

import {loadCSS} from './loadscript.js';

export const PANE_TYPES = Object.freeze([
  'list', 'detail', 'scatter', 'manifold', 'hist', 'sky', 'wavelength', 'narrative',
  'spectrum1d', 'spectrum2d', 'sed', 'gallery', 'table', 'readme', 'landing',
]);

/** type -> module path relative to this file. */
export const PANE_MODULES = Object.freeze(Object.fromEntries(PANE_TYPES.map((t) => [t, `../panes/${t}.js`])));

/**
 * type -> stylesheets (relative to this file) loaded before the pane mounts
 * (IMPLEMENTATION_NOTES 69). Plot panes load [<pane>.css, controls.css]; the hist pane has no
 * stylesheet of its own (its rules live in controls.css), so it loads controls.css only. Every
 * listed file exists in web/css/panes/ (tests/js/store.test.mjs checks it), so no pane CSS
 * request 404s. UI widget styles all live in css/panes/ui.css, which boot loads.
 */
export const PANE_CSS = Object.freeze({
  list: ['../../css/panes/list.css'],
  table: ['../../css/panes/list.css'],
  detail: ['../../css/panes/detail.css'],
  scatter: ['../../css/panes/scatter.css', '../../css/panes/controls.css'],
  manifold: ['../../css/panes/manifold.css', '../../css/panes/controls.css'],
  hist: ['../../css/panes/controls.css'],
  sky: ['../../css/panes/sky.css'],
  wavelength: ['../../css/panes/wavelength.css', '../../css/panes/sky.css'],
  narrative: ['../../css/panes/narrative.css'],
  spectrum1d: ['../../css/panes/spectrum.css'],
  spectrum2d: ['../../css/panes/spectrum.css'],
  sed: ['../../css/panes/sed.css'],
  gallery: ['../../css/panes/gallery.css'],
  readme: ['../../css/panes/landing.css'],
  landing: ['../../css/panes/landing.css'],
});

/** Human titles used by fold headers and mobile tabs when a leaf has no options.title. */
export const PANE_TITLES = Object.freeze({
  list: 'Sources', detail: 'Details', scatter: 'Scatter', manifold: 'Manifold', hist: 'Histogram',
  sky: 'Sky', wavelength: 'Wavelengths', narrative: 'Story', spectrum1d: 'Spectrum',
  spectrum2d: '2D spectrum', sed: 'SED', gallery: 'Gallery', table: 'Table', readme: 'About',
  landing: 'Overview',
});

const overrides = new Map();
const loaded = new Map();

export function has(type) {
  return overrides.has(type) || Object.prototype.hasOwnProperty.call(PANE_MODULES, type);
}

/** Absolute module URL for a pane type; throws for unknown types. */
export function modulePath(type) {
  if (!Object.prototype.hasOwnProperty.call(PANE_MODULES, type)) throw new Error(`unknown pane type: ${type}`);
  return new URL(PANE_MODULES[type], import.meta.url).href;
}

/** Register an in-memory pane module (tests, embedding). `mod` = {meta, mount} or a loader fn. */
export function register(type, mod) {
  overrides.set(type, mod);
  loaded.delete(type);
}

/** Import a pane module ({meta, mount}); cached per type. Pane CSS is attached first. */
export function load(type) {
  if (loaded.has(type)) return loaded.get(type);
  let p;
  if (overrides.has(type)) {
    const o = overrides.get(type);
    p = Promise.resolve(typeof o === 'function' ? o() : o);
  } else {
    const url = modulePath(type);
    const css = (PANE_CSS[type] || []).map((c) => loadCSS(new URL(c, import.meta.url).href));
    p = Promise.all(css).then(() => import(url));
  }
  p = p.then((mod) => {
    if (!mod || typeof mod.mount !== 'function') throw new Error(`pane module '${type}' has no mount()`);
    return mod;
  });
  p.catch(() => loaded.delete(type));
  loaded.set(type, p);
  return p;
}

/** The registry object passed to mountLayout as ctx.registry. */
export const registry = Object.freeze({types: PANE_TYPES, titles: PANE_TITLES, has, modulePath, load, register});
export default registry;
