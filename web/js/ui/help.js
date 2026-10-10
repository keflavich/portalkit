// Help dialog (docs/js.md sections 7 and 8): handles `help:toggle` (the ? key) and `help:open`
// (the header ? button). Lists the keyboard shortcuts (KEYMAP), the capabilities enabled on
// this page, the capabilities that are not available with their reasons (page.json
// caps.disabled plus runtime reasons from app.disableCap) and runtime notes.

import {escapeHtml} from '../core/fmt.js';

/** Short descriptions of the capabilities (ARCHITECTURE.md section 8.2). */
export const CAP_LABELS = Object.freeze({
  keyboard_nav: 'Keyboard navigation through the filtered list',
  cross_page_nav: 'Navigation between the pages of this portal (focus and selection carried along)',
  responsive_mobile: 'Tabbed layout on narrow screens',
  lightbox: 'Full-size image viewer with previous / next',
  detail_panel: 'Details of the focused source',
  filter_toggles: 'Category, flag and range filters',
  deep_link_hash: 'Shareable links that restore the view',
  hover_preview: 'Hover cards',
  static_sed_png: 'Static SED figures',
  cmd_ccd_scatter: 'Colour-magnitude and colour-colour diagrams',
  class_coloring: 'Colouring by class',
  source_list_search: 'Source list with search by id, alias or position',
  lazy_data_loading: 'Data loaded on demand',
  brush_rect: 'Rectangle selection',
  brush_circle: 'Circle selection',
  brush_lasso: 'Lasso selection',
  brush_set_ops: 'Selection set operations (Shift add, Alt subtract, Shift+Alt intersect) with undo',
  selectable_axes: 'Selectable plot axes and expressions',
  readme_provenance: 'Description and data provenance',
  sky_map_aladin: 'Sky map (Aladin Lite)',
  catalog_markers_on_sky: 'Catalog markers on the sky map',
  cdn_fallback: 'Mirror fallback for external libraries',
  hips_layer_stacking: 'Stacked sky survey layers',
  fits_overlay: 'FITS image overlays on the sky',
  density_rendering: 'Density rendering for large catalogs',
  linked_selection: 'Selection shared by all panels',
  manifold_embedding: 'Multi-dimensional projections with tours',
  av_slider: 'Extinction (A_V) slider',
  extinction_vectors_on_ccd: 'Extinction vectors on colour plots',
  isochrones_or_tracks: 'Isochrones and evolutionary tracks',
  model_template_overlay: 'Model template overlays',
  labeled_features_overlay: 'Labelled spectral features',
  spectrum_viewer_1d: '1D spectrum viewer',
  spectrum_2d_viewer: '2D spectrum viewer',
  live_sed_viewer: 'Interactive SED',
  collapsible_panels: 'Collapsible panels',
  resizable_panels: 'Resizable panels',
  dark_light_theme: 'Dark and light themes',
  selection_export: 'Selection export (CSV, JSON, ids, DS9) and import',
});

const EXTRA_KEYS = [
  {keys: 'Click', action: 'focus a source'},
  {keys: 'Shift / Alt + drag', action: 'add to / subtract from the selection while brushing'},
];

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

/** HTML body of the help dialog (string; every value escaped). */
export function helpHTML(app) {
  const keymap = Array.isArray(app.keymap) ? app.keymap : [];
  const caps = Array.from(app.caps || []).sort();
  const disabled = Object.entries(app.disabled || {}).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  const notes = Object.entries(app.notes || {});
  const row = (k) => `<tr><th scope="row"><kbd>${escapeHtml(k.keys)}</kbd></th><td>${escapeHtml(k.action)}</td></tr>`;
  const capItem = (c) => `<li><span class="pk-cap-name">${escapeHtml(CAP_LABELS[c] || c)}</span> <code>${escapeHtml(c)}</code></li>`;
  const page = app.page || {};
  const title = (page.page && page.page.title) || 'This page';
  let html = `<h2 id="pk-help-title">Help</h2>`;
  html += `<section><h3>Keyboard shortcuts</h3><table class="pk-keymap"><tbody>${keymap.map(row).join('')}${EXTRA_KEYS.map(row).join('')}</tbody></table>`
    + `<p class="pk-muted">Shortcuts are ignored while typing in a text field (except Esc).</p></section>`;
  html += `<section><h3>Available on ${escapeHtml(title)}</h3>`
    + (caps.length ? `<ul class="pk-cap-list">${caps.map(capItem).join('')}</ul>` : '<p class="pk-muted">No capabilities listed.</p>')
    + '</section>';
  if (disabled.length) {
    html += `<section><h3>Not available here</h3><ul class="pk-cap-list pk-cap-off">${disabled.map(([c, r]) =>
      `<li><span class="pk-cap-name">${escapeHtml(CAP_LABELS[c] || c)}</span> <code>${escapeHtml(c)}</code>: ${escapeHtml(r)}</li>`).join('')}</ul></section>`;
  }
  if (notes.length) {
    html += `<section><h3>Notes</h3><ul class="pk-cap-list">${notes.map(([c, r]) =>
      `<li><code>${escapeHtml(c)}</code>: ${escapeHtml(r)}</li>`).join('')}</ul></section>`;
  }
  const portal = page.portal || {};
  const meta = [];
  if (portal.title) meta.push(escapeHtml(portal.title));
  if (page.pk) meta.push(`portalkit ${escapeHtml(page.pk)}`);
  if (app.readyMs !== undefined) meta.push(`ready in ${Math.round(app.readyMs)} ms`);
  if (meta.length) html += `<p class="pk-muted pk-help-meta">${meta.join(' · ')}</p>`;
  return html;
}

export function install(app) {
  const store = app.store;
  let dlg = null;

  function build() {
    dlg = el('dialog', 'pk-dialog pk-help');
    dlg.setAttribute('aria-labelledby', 'pk-help-title');
    const body = el('div', 'pk-help-body');
    const form = el('form', 'pk-help-actions');
    form.method = 'dialog';
    const close = el('button', 'pk-btn', 'Close');
    close.value = 'close';
    form.append(close);
    dlg.append(body, form);
    document.body.append(dlg);
    dlg.addEventListener('click', (ev) => {
      if (ev.target === dlg) dlg.close();   // backdrop click
    });
    return dlg;
  }

  function isOpen() {
    return !!(dlg && dlg.open);
  }

  function open() {
    if (!dlg) build();
    dlg.querySelector('.pk-help-body').innerHTML = helpHTML(app);
    if (dlg.open) return;
    if (typeof dlg.showModal === 'function') dlg.showModal();
    else dlg.setAttribute('open', '');
  }

  function close() {
    if (dlg && dlg.open) dlg.close();
  }

  store.onEvent('help:toggle', (p) => {
    if (!p || p.handled) return;
    p.handled = true;
    if (isOpen()) close();
    else open();
  });
  store.onEvent('help:open', (p) => {
    if (!p || p.handled) return;
    p.handled = true;
    open();
  });
  store.onEvent('caps:change', () => {
    if (isOpen()) dlg.querySelector('.pk-help-body').innerHTML = helpHTML(app);
  });
  return {open, close, isOpen};
}
