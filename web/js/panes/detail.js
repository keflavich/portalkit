// Detail pane (capability detail_panel, ARCHITECTURE.md sections 5.5 and 6.10).
//
// Shows the focused source of a catalog dataset: a header (id, position, exact/quantized badge),
// probability bars (`bars`), prose fields (`prose`), the property table grouped by manifest
// groups or by a named template, figures from an image set (`images`, opened in the lightbox),
// per-source products from a json record set (`products`, products/1) with login badges, and
// links to sibling pages (page.links, `{id}` substituted, focus/selection carried).
//
// The pane prefers exact rows: client.record(i) returns the row chunk when the manifest has
// `rows`; otherwise the quantized values are shown with step-aware digits and a ±step/2 tooltip.
// Clicking a numeric property (clickToAxis) puts it on the Y axis of the first scatter pane on
// this dataset; Shift-click sets X and Alt-click sets the colour column.
//
// URLs (IMPLEMENTATION_NOTES 81): figure URLs from images.json resolve against the page URL;
// product links resolve against the products records index URL; both substitute {res}/{assets}.
// Rows are read on the record channel `detail:<pane id>` (note 73), so another pane's record
// request does not cancel this one.

import {fmtBytes, fmtDec, fmtNum, fmtQuant, fmtRA, fmtValue, MISSING} from '../core/fmt.js';
import {carryTo, loginBadge} from '../ui/nav.js';
import {itemUrl, openLightbox, productUrl, registerSet} from '../ui/lightbox.js';

export const meta = {type: 'detail', caps: ['detail_panel', 'static_sed_png'], needs: ['catalog']};

const NUMERIC_ROLES = new Set(['mag', 'flux', 'color', 'value', 'error']);
const ID_KEYS = ['id', '_id'];

/**
 * Named templates: ordered sections of keys (matched case-insensitively against the row) and
 * flags. Keys absent from a row are skipped; columns not claimed by a section go to "Other"
 * (or are dropped when `rest` is false).
 */
export const TEMPLATES = Object.freeze({
  nirspec_source: {
    label: 'NIRSpec source',
    sections: [
      {title: 'Observation', keys: ['Target', 'Object', 'Program', 'Observation', 'Visit', 'VisitGroup', 'SlitID',
        'grating', 'gratings', 'filter', 'filters', 'Filename', 'checklist']},
      {title: 'Photometry', roles: ['mag', 'flux']},
      {title: 'Colours', roles: ['color']},
    ],
    rest: true,
  },
  photometry: {
    label: 'Photometry',
    sections: [
      {title: 'Photometry', roles: ['mag', 'flux']},
      {title: 'Colours', roles: ['color']},
    ],
    rest: false,
  },
  compact: {
    label: 'Compact',
    sections: [{title: 'Properties', roles: ['category', 'flag', 'mag', 'color', 'value']}],
    rest: false,
    figures: false,
  },
});

/** Substitute `{id}` (URL-encoded) in a link template; null for an empty template. */
export function substituteLink(tpl, id) {
  if (typeof tpl !== 'string' || !tpl) return null;
  return tpl.replace(/\{id\}/g, encodeURIComponent(String(id ?? '')));
}

/** Fraction in [0, 1] shown by a probability bar (values in (1, 100] are read as percent). */
export function barFraction(v) {
  const x = typeof v === 'string' ? Number(v) : v;
  if (typeof x !== 'number' || !Number.isFinite(x)) return NaN;
  const f = x > 1 && x <= 100 ? x / 100 : x;
  return Math.max(0, Math.min(1, f));
}

function lowerMap(obj) {
  const m = new Map();
  for (const k of Object.keys(obj || {})) {
    const l = k.toLowerCase();
    if (!m.has(l)) m.set(l, k);
  }
  return m;
}

/**
 * Sections for the property table: [{title, keys:[rowKey]}]. `manifest` provides cols and
 * groups; `template` is a TEMPLATES entry or null (auto). Keys starting with `_`, ids,
 * positions and keys listed in `skip` are left out; error columns are folded into their value.
 */
export function buildSections(row, manifest, template = null, skip = []) {
  const cols = (manifest && manifest.cols) || [];
  const metaOf = new Map(cols.map((c) => [c.key, c]));
  const errCols = new Set(cols.filter((c) => c.err).map((c) => c.err));
  const skipSet = new Set(skip.map((s) => String(s).toLowerCase()));
  const keys = Object.keys(row || {}).filter((k) => {
    if (k.startsWith('_') || ID_KEYS.includes(k)) return false;
    const l = k.toLowerCase();
    if (l === 'ra' || l === 'dec' || skipSet.has(l)) return false;
    const m = metaOf.get(k);
    if (m && (m.role === 'id' || m.role === 'ra' || m.role === 'dec' || m.role === 'limit')) return false;
    if (errCols.has(k)) return false;
    return true;
  });
  const used = new Set();
  const out = [];
  const take = (title, list) => {
    const ks = list.filter((k) => !used.has(k));
    for (const k of ks) used.add(k);
    if (ks.length) out.push({title, keys: ks});
  };
  if (template) {
    const lm = lowerMap(Object.fromEntries(keys.map((k) => [k, 1])));
    for (const s of template.sections || []) {
      const list = [];
      for (const k of s.keys || []) {
        const real = lm.get(String(k).toLowerCase());
        if (real) list.push(real);
      }
      if (s.roles) for (const k of keys) if (s.roles.includes((metaOf.get(k) || {}).role)) list.push(k);
      take(s.title, list);
    }
    if (template.rest !== false) take('Other', keys);
    return out;
  }
  // auto: manifest groups first (in order), then role buckets.
  for (const g of (manifest && manifest.groups) || []) {
    const dims = (g.dims || []).filter((k) => keys.includes(k));
    const members = keys.filter((k) => (metaOf.get(k) || {}).group === g.key);
    take(g.label || g.key, Array.from(new Set([...dims, ...members])));
  }
  const bucket = (pred) => keys.filter((k) => pred(metaOf.get(k), row[k]));
  take('Identification', bucket((m, v) => (m ? ['text', 'category', 'flag', 'url'].includes(m.role) : typeof v === 'string' || typeof v === 'boolean')));
  take('Photometry', bucket((m) => m && (m.role === 'mag' || m.role === 'flux')));
  take('Colours', bucket((m) => m && m.role === 'color'));
  take('Values', bucket((m) => m && m.role === 'value'));
  take('Other', keys);
  return out;
}

const LIMIT_TEXT = {1: 'upper limit', 2: 'lower limit', 3: 'clamped at the low end of the encoded range', 4: 'clamped at the high end of the encoded range'};

/** Display text for one property: {text, title, numeric}. */
export function propertyText(row, key, meta, exact) {
  const v = row[key];
  const m = meta || {};
  let r;
  if (typeof v === 'number' && !exact && m.step > 0) r = fmtQuant(v, m.step);
  else r = fmtValue(v, m, {exact});
  let text = r.text;
  let title = r.title;
  if (m.err && row[m.err] !== undefined && row[m.err] !== null && text !== MISSING) {
    const e = row[m.err];
    if (typeof e === 'number' && Number.isFinite(e)) text = `${text} ± ${fmtNum(e, {sig: 2})}`;
  }
  const lk = row._limits && row._limits[key];
  if (lk) {
    if (lk === 1) text = `< ${text}`;
    else if (lk === 2) text = `> ${text}`;
    title = [LIMIT_TEXT[lk] || `limit kind ${lk}`, title].filter(Boolean).join('; ');
  }
  if (m.unit && text !== MISSING) title = [title, `unit: ${m.unit}`].filter(Boolean).join('; ');
  return {text, title, numeric: typeof v === 'number' && NUMERIC_ROLES.has(m.role || 'value')};
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

function isPortalRelative(href) {
  const u = new URL(href, document.baseURI);
  return u.origin === location.origin && !/^[a-z][a-z0-9+.-]*:/i.test(String(href).trim());
}

export function mount(root, ctx) {
  const cfg = ctx.config || {};
  const store = ctx.store;
  const app = ctx.app;
  const hub = ctx.hub;
  const ds = ctx.dataset;
  const datasets = (app.page && app.page.datasets) || {};
  const templateName = cfg.template && cfg.template !== 'auto' ? String(cfg.template) : null;
  const template = templateName ? TEMPLATES[templateName] || null : null;
  const clickToAxis = cfg.clickToAxis !== false;
  const linksOn = cfg.links !== false;
  const bars = Array.isArray(cfg.bars) ? cfg.bars : [];
  const prose = Array.isArray(cfg.prose) ? cfg.prose : [];
  const imagesDs = cfg.images || null;
  const productsDs = cfg.products || null;
  const setId = `detail:${ctx.id}`;

  root.classList.add('pk-detail');
  const body = el('div', 'pk-detail-body');
  body.setAttribute('aria-live', 'polite');
  root.append(body);

  let client = null;
  let visible = true;
  let pendingRender = false;
  let destroyed = false;
  let token = 0;
  let current = null;           // {i, id, row}
  let focusSeq = 0;
  let wanted = -1;              // row index of the latest show() request (rendered or in flight)
  let figuresReady = false;     // figures were built at least once (the lightbox waits until then)
  let shownId;                  // id the figures were last built for (undefined before the first show)
  let figures = [];             // lightbox items for the current source
  let imagesDoc = null;
  let imagesIndex = null;       // {byRow: Map, byId: Map}
  const offs = [];
  let unregister = null;

  function showNote(cls, text) {
    body.textContent = '';
    const n = el('div', cls, text);
    if (cls === 'pk-error-note') n.setAttribute('role', 'alert');
    body.append(n);
  }

  function emptyState() {
    current = null;
    figures = [];
    if (unregister) refreshFigures();
    showNote('pk-detail-empty pk-muted', 'Click a source to see its details.');
    ctx.setTitle('Details');
  }

  function report(err) {
    if (!(err instanceof Error)) throw err;
    if (err.name === 'AbortError') return;
    console.warn(`detail ${ctx.id}`, err);
    showNote('pk-error-note', `The details could not be loaded: ${err.message}`);
  }

  if (templateName && !template) {
    console.warn(`detail ${ctx.id}: unknown template ${templateName}; using auto`);
    ctx.setStatus('auto template', `Template "${templateName}" is not known; the automatic layout is used.`);
  }

  // ------------------------------------------------------------------ figures

  async function loadImages() {
    if (!imagesDs || imagesDoc) return imagesDoc;
    const ref = datasets[imagesDs];
    if (!ref || ref.kind !== 'images') return null;
    imagesDoc = await hub.json(imagesDs);
    const byRow = new Map();
    const byId = new Map();
    for (const it of (imagesDoc && imagesDoc.items) || []) {
      if (!it) continue;
      if (Number.isInteger(it.row)) {
        if (!byRow.has(it.row)) byRow.set(it.row, []);
        byRow.get(it.row).push(it);
      }
      if (it.id !== undefined && it.id !== null) {
        const k = String(it.id);
        if (!byId.has(k)) byId.set(k, []);
        byId.get(k).push(it);
      }
    }
    imagesIndex = {byRow, byId};
    return imagesDoc;
  }

  function figuresFor(i, id) {
    if (!imagesIndex) return [];
    const catOk = !imagesDoc.catalog || imagesDoc.catalog === ds;
    if (!catOk) return [];
    const items = imagesIndex.byRow.get(i) || imagesIndex.byId.get(String(id)) || [];
    const roleLabel = new Map(((imagesDoc && imagesDoc.roles) || []).map((r) => [r.key, r.label || r.key]));
    const out = [];
    for (const it of items) {
      for (const [role, img] of Object.entries(it.roles || {})) {
        if (!img || !img.url) continue;
        const src = itemUrl(img.url, app.page);
        if (!src) continue;
        out.push({
          src,
          thumb: (img.thumb && itemUrl(img.thumb, app.page)) || src,
          w: img.w, h: img.h,
          title: roleLabel.get(role) || role,
          caption: it.caption || String(it.id ?? id),
          alt: `${roleLabel.get(role) || role} for ${id}`,
        });
      }
    }
    return out;
  }

  function refreshFigures() {
    if (unregister) unregister();
    unregister = registerSet(setId, {
      label: 'Figures',
      ready: () => figuresReady,
      count: () => figures.length,
      item: (k) => figures[k] || null,
    });
  }

  // ------------------------------------------------------------------ axis targets

  function scatterTarget() {
    const layout = ctx.layout;
    if (!layout || typeof layout.panes !== 'function') return null;
    const list = layout.panes().filter((p) => p && (p.type === 'scatter' || p.type === 'manifold' || p.type === 'hist')
      && (p.dataset === ds || !p.dataset));
    const sc = list.filter((p) => p.type === 'scatter');
    const pick = sc.find((p) => p.visible) || sc[0] || list.find((p) => p.visible) || list[0];
    return pick || null;
  }

  function toAxis(key, ev) {
    if (ev.altKey) {
      const c = store.get('color') || {};
      store.set({color: Object.assign({}, c, {by: key})}, {source: ctx.id, history: true});
      store.emit('toast', {level: 'info', text: `Colour by ${key}`, ms: 2000});
      return;
    }
    const target = scatterTarget();
    if (!target) {
      store.emit('toast', {level: 'info', text: 'No scatter plot on this page to receive the axis.'});
      return;
    }
    const ax = ev.shiftKey ? 'x' : 'y';
    const cur = store.get(`axes:${target.id}`) || {};
    const next = Object.assign({}, cur, {[ax]: key});
    delete next[`${ax}r`];
    store.set({[`axes:${target.id}`]: next}, {source: ctx.id, history: true});
    if (target.collapsed && ctx.layout.reveal) ctx.layout.reveal(target.id).catch(report);
    store.emit('toast', {level: 'info', text: `${ax.toUpperCase()} axis: ${key}`, ms: 2000});
  }

  // ------------------------------------------------------------------ render

  function renderHeader(row, id, exact) {
    const head = el('div', 'pk-detail-head');
    const h = el('h3', 'pk-detail-id', String(id));
    h.title = String(id);
    head.append(h);
    const ra = row.ra ?? row.RA;
    const dec = row.dec ?? row.DEC ?? row.Dec;
    if (typeof ra === 'number' && Number.isFinite(ra) && typeof dec === 'number' && Number.isFinite(dec)) {
      const pos = el('div', 'pk-detail-pos pk-mono', `${fmtRA(ra)} ${fmtDec(dec)}`);
      pos.title = `RA ${ra.toFixed(6)}°, Dec ${dec.toFixed(6)}°`;
      head.append(pos);
    }
    const badges = el('div', 'pk-detail-badges');
    if (!exact) {
      const b = el('span', 'pk-badge pk-warn', 'quantized');
      b.title = 'Exact rows are not published for this catalog; values are shown at the stored precision (hover a value for ±step/2).';
      badges.append(b);
    }
    const copy = el('button', 'pk-icon-btn pk-detail-copy', '⧉');
    copy.type = 'button';
    copy.title = 'Copy a link to this source';
    copy.setAttribute('aria-label', 'Copy a link to this source');
    copy.addEventListener('click', () => {
      const hash = typeof app.hashLink === 'function' ? String(app.hashLink() || '') : location.hash.slice(1);
      const url = `${location.href.split('#')[0]}#${hash.replace(/^#/, '')}`;
      const done = () => store.emit('toast', {level: 'info', text: 'Link copied.', ms: 2000});
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(url).then(done, (err) => {
          if (!(err instanceof Error)) throw err;
          store.emit('toast', {level: 'warn', text: `Copy failed: ${err.message}`});
        });
      } else {
        store.emit('toast', {level: 'warn', text: 'The clipboard is not available here.'});
      }
    });
    badges.append(copy);
    head.append(badges);
    return head;
  }

  function renderBars(row) {
    const keys = bars.filter((k) => k in row);
    if (!keys.length) return null;
    const box = el('div', 'pk-detail-bars');
    box.setAttribute('role', 'list');
    for (const k of keys) {
      const m = client.col(k) || {};
      const f = barFraction(row[k]);
      const line = el('div', 'pk-bar');
      line.setAttribute('role', 'listitem');
      const lab = el('span', 'pk-bar-label', m.label || k);
      const track = el('span', 'pk-bar-track');
      const fill = el('span', 'pk-bar-fill');
      fill.style.width = Number.isFinite(f) ? `${(f * 100).toFixed(1)}%` : '0%';
      track.append(fill);
      track.setAttribute('role', 'meter');
      track.setAttribute('aria-valuemin', '0');
      track.setAttribute('aria-valuemax', '1');
      if (Number.isFinite(f)) track.setAttribute('aria-valuenow', String(f));
      track.setAttribute('aria-label', m.label || k);
      const val = el('span', 'pk-bar-value pk-num', Number.isFinite(f) ? `${Math.round(f * 100)}%` : MISSING);
      line.append(lab, track, val);
      box.append(line);
    }
    return box;
  }

  function renderProse(row) {
    const keys = prose.filter((k) => typeof row[k] === 'string' && row[k].trim());
    if (!keys.length) return null;
    const box = el('div', 'pk-detail-prose');
    for (const k of keys) {
      const m = client.col(k) || {};
      const p = el('p');
      if (keys.length > 1) p.append(el('strong', null, `${m.label || k}: `));
      p.append(String(row[k]));
      box.append(p);
    }
    return box;
  }

  function renderTable(row, exact) {
    const skip = [...bars, ...prose];
    const sections = buildSections(row, client.manifest, template, skip);
    const frag = document.createDocumentFragment();
    for (const s of sections) {
      const det = el('details', 'pk-detail-section');
      det.open = true;
      det.append(el('summary', 'pk-detail-section-title', s.title));
      const table = el('table', 'pk-props');
      const tb = el('tbody');
      for (const k of s.keys) {
        const m = client.col(k);
        const {text, title, numeric} = propertyText(row, k, m, exact);
        const tr = el('tr');
        const th = el('th', null, (m && m.label) || k);
        th.scope = 'row';
        if (m && m.unit) th.append(el('span', 'pk-unit pk-muted', ` [${m.unit}]`));
        if (m && m.note) th.title = m.note;
        const td = el('td', numeric ? 'pk-num' : null);
        const isUrl = (m && m.role === 'url') || (typeof row[k] === 'string' && /^https?:\/\//i.test(row[k]));
        if (isUrl && typeof row[k] === 'string' && row[k]) {
          const a = el('a', null, text);
          a.href = row[k];
          a.target = '_blank';
          a.rel = 'noopener';
          td.append(a);
        } else {
          td.textContent = text;
        }
        if (title) td.title = title;
        if (text === MISSING) td.classList.add('pk-missing');
        if (clickToAxis && numeric && m) {
          tr.classList.add('pk-axis-target');
          tr.tabIndex = 0;
          tr.dataset.key = k;
          tr.title = `${title ? `${title}\n` : ''}Click: Y axis · Shift-click: X axis · Alt-click: colour`;
        }
        tr.append(th, td);
        tb.append(tr);
      }
      table.append(tb);
      det.append(table);
      frag.append(det);
    }
    return frag;
  }

  function renderFigures() {
    if (!figures.length || (template && template.figures === false)) return null;
    const box = el('div', 'pk-detail-figures');
    figures.forEach((f, k) => {
      const fig = el('figure', 'pk-detail-figure');
      const b = el('button', 'pk-figure-btn');
      b.type = 'button';
      b.title = `${f.title}: open full size`;
      const img = el('img');
      img.loading = 'lazy';
      img.decoding = 'async';
      img.src = f.thumb;
      img.alt = f.alt;
      if (f.w && f.h) {
        img.width = f.w;
        img.height = f.h;
      }
      img.addEventListener('error', () => {
        b.replaceChildren(el('span', 'pk-muted', 'image unavailable'));
      }, {once: true});
      b.append(img);
      b.addEventListener('click', () => openLightbox(store, setId, k, ctx.id));
      fig.append(b, el('figcaption', 'pk-muted', f.title));
      box.append(fig);
    });
    return box;
  }

  async function renderProducts(id, my) {
    if (!productsDs) return null;
    const ref = datasets[productsDs];
    if (!ref || ref.kind !== 'records') return null;
    const rs = await hub.records(productsDs);
    if (!rs || (rs.has && !rs.has(id))) return null;
    const rec = await rs.get(id, {token: my, channel: `detail:${ctx.id}`});
    if (!rec || my !== token) return null;
    const items = Array.isArray(rec.items) ? rec.items : [];
    if (!items.length) return null;
    const box = el('div', 'pk-detail-products');
    box.append(el('h4', 'pk-detail-subtitle', 'Products'));
    const ul = el('ul', 'pk-products');
    for (const it of items) {
      if (!it || typeof it.url !== 'string' || !it.url) continue;
      const href = productUrl(it.url, rs.url || ref.url, app.page);
      if (!href) continue;
      const li = el('li', 'pk-product');
      const a = el('a', 'pk-product-link', it.label || it.url.split('/').pop());
      a.href = href;
      a.target = '_blank';
      a.rel = 'noopener';
      if (it.kind === 'fits') a.setAttribute('download', '');
      li.append(a);
      if (it.kind) li.append(' ', el('span', 'pk-chip pk-product-kind', it.kind));
      if (Number.isFinite(it.size)) li.append(' ', el('span', 'pk-muted pk-num', fmtBytes(it.size)));
      if (it.auth === 'secure') li.append(' ', loginBadge());
      ul.append(li);
    }
    box.append(ul);
    return box;
  }

  function renderLinks(id) {
    const links = (app.page && app.page.links) || {};
    const names = Object.keys(links);
    if (!linksOn || !names.length) return null;
    const box = el('nav', 'pk-detail-links');
    box.setAttribute('aria-label', 'Open this source on other pages');
    for (const name of names) {
      const href = substituteLink(links[name], id);
      if (!href) continue;
      const a = el('a', 'pk-btn pk-detail-link', name);
      a.href = href;
      const follow = (ev) => {
        if (ev.type === 'auxclick' && ev.button !== 1) return;
        if (isPortalRelative(links[name])) carryTo(app, a.href);
      };
      a.addEventListener('click', follow);
      a.addEventListener('auxclick', follow);
      box.append(a);
    }
    return box.childNodes.length ? box : null;
  }

  async function show(i, id) {
    const my = ++token;
    if (!client) return;
    wanted = i;
    if (!visible) {
      pendingRender = true;
      return;
    }
    pendingRender = false;
    root.classList.add('pk-loading');
    if (!current) {
      body.textContent = '';
      body.append(el('div', 'pk-skeleton pk-detail-skeleton'));
    }
    // No client token: the DataClient is shared by every pane, and this pane's own token
    // already discards superseded responses.
    let row;
    try {
      row = await client.record(i, {channel: `detail:${ctx.id}`});
    } catch (err) {
      if (my === token) wanted = -1;
      throw err;
    }
    if (my !== token || destroyed) return;
    if (!row) {
      showNote('pk-warn-note', `Row ${i} is not in this catalog.`);
      return;
    }
    const rid = row._id ?? id ?? row.id;
    const exact = !row._quantized;
    current = {i, id: rid, row};
    await loadImages().catch(report);
    if (my !== token || destroyed) return;
    figures = figuresFor(i, rid);
    figuresReady = true;
    // A lightbox on this pane's figures belongs to the previous source: close it when the
    // source changes, but keep one restored from the hash with the initial focus.
    const prevId = shownId;
    shownId = rid;
    refreshFigures();
    const lb = store.get('lightbox');
    if (lb && lb.set === setId && prevId !== undefined && String(prevId) !== String(rid)) {
      store.set({lightbox: null}, {source: ctx.id});
    }

    body.textContent = '';
    body.append(renderHeader(row, rid, exact));
    for (const part of [renderBars(row), renderProse(row), renderFigures()]) if (part) body.append(part);
    body.append(renderTable(row, exact));
    const links = renderLinks(rid);
    if (links) body.append(links);
    root.classList.remove('pk-loading');
    ctx.setTitle(`Details · ${rid}`);
    renderProducts(rid, my).then((box) => {
      if (box && my === token && !destroyed) {
        const before = body.querySelector('.pk-detail-links');
        if (before) body.insertBefore(box, before);
        else body.append(box);
      }
    }, (err) => {
      if (!(err instanceof Error)) throw err;
      if (err.name === 'AbortError') return;
      console.warn(`detail ${ctx.id}: products`, err);
      const n = el('div', 'pk-warn-note', `Products could not be loaded: ${err.message}`);
      if (my === token) body.append(n);
    });
  }

  body.addEventListener('click', (ev) => {
    const tr = ev.target.closest ? ev.target.closest('tr.pk-axis-target') : null;
    if (!tr || ev.target.closest('a')) return;
    toAxis(tr.dataset.key, ev);
  });
  body.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Enter') return;
    const tr = ev.target.closest ? ev.target.closest('tr.pk-axis-target') : null;
    if (!tr) return;
    ev.preventDefault();
    toAxis(tr.dataset.key, ev);
  });

  async function onFocus() {
    if (!client) return;
    const seq = ++focusSeq;       // a newer focus change supersedes a pending id lookup
    const f = store.get('focus');
    if (!f || !f.ds) {
      token++;
      wanted = -1;
      markFiguresReady();
      emptyState();
      return;
    }
    if (f.ds === ds) {
      if (Number.isInteger(f.i) && f.i >= 0) {
        // Same row as the latest request (shown or in flight): nothing to do. Any other row
        // starts a new show(), whose token discards a slower response for an older row.
        if (wanted === f.i) return;
        await show(f.i, f.id);
      }
      return;
    }
    // Focus on another dataset: follow it by id (an image set or a sibling catalog keyed by id).
    const ref = datasets[f.ds];
    const linked = ref && (ref.catalog === ds || ref.kind === 'catalog');
    if (!linked || f.id === null || f.id === undefined) return;
    const [i] = await app.resolveIds(ds, [f.id]);
    if (seq !== focusSeq || destroyed) return;
    if (i >= 0) {
      if (wanted !== i) await show(i, f.id);
    } else {
      token++;
      wanted = -1;
      markFiguresReady();
      current = null;
      showNote('pk-detail-empty pk-muted', `“${f.id}” has no row in this catalog.`);
    }
  }

  function markFiguresReady() {
    if (figuresReady) return;
    figuresReady = true;
    refreshFigures();
  }

  offs.push(store.on(['focus'], () => { onFocus().catch(report); }));

  // ------------------------------------------------------------------ load

  const ref = datasets[ds];
  if (!ref || ref.kind !== 'catalog') {
    showNote('pk-info-note', `The detail panel needs a catalog dataset (got ${ref ? ref.kind : 'none'}).`);
  } else {
    body.append(el('div', 'pk-skeleton pk-detail-skeleton'));
    (async () => {
      client = await app.catalog(ds);
      if (destroyed) return;
      refreshFigures();
      const f = store.get('focus');
      if (!f) emptyState();
      else await onFocus();
    })().catch(report);
  }

  return {
    resize() {},
    onVisible(v) {
      visible = v;
      if (v && pendingRender) {
        current = null;
        wanted = -1;
        onFocus().catch(report);
      }
    },
    destroy() {
      destroyed = true;
      token++;
      for (const off of offs) off();
      if (unregister) unregister();
    },
    stats() {
      return {id: current ? current.id : null, exact: current ? !current.row._quantized : null, figures: figures.length};
    },
  };
}
