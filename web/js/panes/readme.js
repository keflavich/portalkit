// Readme pane (capability readme_provenance, ARCHITECTURE.md sections 4.3 and 6.10).
//
// Shows the page description: options `html` or `url`, otherwise page.readme ({html} inline or
// {url} for a readme.<h8>.html file). The HTML comes from portalkit's markdown_lite renderer;
// it is still sanitized here (scripts, styles, frames, forms, event-handler attributes and
// javascript: URLs are removed) before it is inserted. Below it, a "Data provenance" section
// lists page.readme.provenance and, for every catalog of the page, the manifest provenance
// (inputs, build, catalog FITS headers) and caveats.

import {escapeHtml, fmtBytes, fmtCount} from '../core/fmt.js';
import {fetchBytes, FetchError} from '../data/fetch.js';

export const meta = {type: 'readme', caps: ['readme_provenance'], needs: []};

const DROP_TAGS = new Set(['SCRIPT', 'STYLE', 'IFRAME', 'FRAME', 'FRAMESET', 'OBJECT', 'EMBED', 'FORM', 'INPUT',
  'BUTTON', 'TEXTAREA', 'SELECT', 'LINK', 'META', 'BASE', 'NOSCRIPT', 'TEMPLATE', 'SVG', 'MATH']);
const URL_ATTRS = new Set(['href', 'src', 'xlink:href', 'action', 'formaction', 'poster', 'srcset']);

/** True for URLs that are safe in href/src (http(s), mailto, relative, fragment, data:image). */
export function safeUrl(u) {
  const s = String(u ?? '').replace(/[\u0000- \u007f]+/g, '').toLowerCase();
  if (!s) return true;
  if (s.startsWith('data:')) return /^data:image\/(png|jpe?g|gif|webp);/.test(s);
  const m = /^([a-z][a-z0-9+.-]*):/.exec(s);
  return !m || ['http', 'https', 'mailto'].includes(m[1]);
}

/** Sanitize an HTML string into a DocumentFragment (needs a DOM). */
export function sanitizeHTML(html) {
  const tpl = document.createElement('template');
  tpl.innerHTML = String(html ?? '');
  const walk = (node) => {
    for (const child of Array.from(node.childNodes)) {
      if (child.nodeType === 8) {               // comment
        child.remove();
        continue;
      }
      if (child.nodeType !== 1) continue;
      if (DROP_TAGS.has(child.tagName.toUpperCase())) {
        child.remove();
        continue;
      }
      for (const a of Array.from(child.attributes)) {
        const name = a.name.toLowerCase();
        if (name.startsWith('on') || name === 'style' || name === 'srcdoc') child.removeAttribute(a.name);
        else if (URL_ATTRS.has(name) && !safeUrl(a.value)) child.removeAttribute(a.name);
      }
      if (child.tagName === 'A' && child.getAttribute('href') && /^https?:/i.test(child.getAttribute('href'))) {
        child.setAttribute('rel', 'noopener');
      }
      walk(child);
    }
  };
  walk(tpl.content);
  return tpl.content;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

/** <dl> rows for a flat object (nested objects as JSON). */
function kvList(obj, skip = []) {
  const dl = el('dl', 'pk-kv');
  for (const [k, v] of Object.entries(obj || {})) {
    if (skip.includes(k) || v === null || v === undefined || v === '') continue;
    dl.append(el('dt', null, k), el('dd', null, typeof v === 'object' ? JSON.stringify(v) : String(v)));
  }
  return dl.childNodes.length ? dl : null;
}

/**
 * Provenance block for one source: {title, provenance, caveats}. Returns an element or null.
 * provenance: {inputs:[{path, sha256, mtime, size}], legacy_modules:[..], adapter, portalkit,
 * tier, built, catalogs:[{name, header}] , ...}.
 */
export function provenanceBlock(title, prov, caveats) {
  const hasProv = prov && typeof prov === 'object' && Object.keys(prov).length;
  const cav = Array.isArray(caveats) ? caveats.filter((c) => typeof c === 'string' && c.trim()) : [];
  if (!hasProv && !cav.length) return null;
  const box = el('section', 'pk-prov');
  box.append(el('h4', 'pk-prov-title', title));
  if (cav.length) {
    const note = el('div', 'pk-warn-note pk-caveats');
    const ul = el('ul');
    for (const c of cav) ul.append(el('li', null, c));
    note.append(el('strong', null, 'Caveats'), ul);
    box.append(note);
  }
  if (hasProv) {
    const flat = kvList(prov, ['inputs', 'legacy_modules', 'catalogs']);
    if (flat) box.append(flat);
    const inputs = Array.isArray(prov.inputs) ? prov.inputs : [];
    if (inputs.length) {
      const det = el('details', 'pk-prov-inputs');
      det.append(el('summary', null, `Inputs (${fmtCount(inputs.length)})`));
      const ul = el('ul', 'pk-mono');
      for (const inp of inputs) {
        const li = el('li', null, String(inp.path ?? inp.url ?? ''));
        const extra = [inp.mtime, Number.isFinite(inp.size) ? fmtBytes(inp.size) : null, inp.sha256 ? `sha256 ${String(inp.sha256).slice(0, 12)}…` : null].filter(Boolean);
        if (extra.length) li.append(el('span', 'pk-muted', `  ${extra.join(' · ')}`));
        ul.append(li);
      }
      det.append(ul);
      box.append(det);
    }
    const mods = Array.isArray(prov.legacy_modules) ? prov.legacy_modules : [];
    if (mods.length) {
      const det = el('details', 'pk-prov-modules');
      det.append(el('summary', null, `Legacy modules (${fmtCount(mods.length)})`));
      const ul = el('ul', 'pk-mono');
      for (const m of mods) ul.append(el('li', null, `${m.name ?? ''} ${m.path ?? ''}`.trim()));
      det.append(ul);
      box.append(det);
    }
    const cats = Array.isArray(prov.catalogs) ? prov.catalogs : [];
    for (const c of cats) {
      const det = el('details', 'pk-prov-catalog');
      det.append(el('summary', null, `Catalog: ${c.name ?? 'unnamed'}`));
      const dl = kvList(c.header || {});
      if (dl) det.append(dl);
      if (c.note) det.append(el('p', null, String(c.note)));
      box.append(det);
    }
  }
  return box;
}

/** Fetch an HTML document as text (FetchError on HTTP errors). */
export async function fetchText(url) {
  const bytes = await fetchBytes(url, {cache: 'no-cache'});
  return new TextDecoder('utf-8').decode(bytes);
}

/**
 * Render the readme of `app.page` (or `cfg`) into `container`. Shared with panes/landing.js.
 * Returns {catalogs: number} once the provenance section is complete.
 */
export async function renderReadme(container, app, cfg = {}, opts = {}) {
  const page = app.page || {};
  const readme = page.readme && typeof page.readme === 'object' ? page.readme : (typeof page.readme === 'string' ? {url: page.readme} : {});
  const hub = app.hub;
  const resolve = (u) => new URL(hub && hub.resolve ? hub.resolve(u) : u, document.baseURI).toString();
  let html = null;
  if (typeof cfg.html === 'string') html = cfg.html;
  else if (typeof cfg.url === 'string' && cfg.url) html = await fetchText(resolve(cfg.url));
  else if (typeof readme.html === 'string') html = readme.html;
  else if (typeof readme.url === 'string' && readme.url) html = await fetchText(resolve(readme.url));
  const article = el('article', 'pk-readme-body');
  if (html && html.trim()) article.append(sanitizeHTML(html));
  else if (!opts.quietEmpty) article.append(el('p', 'pk-muted', 'This page has no description.'));
  container.append(article);

  if (opts.provenance === false) return {catalogs: 0};
  const sec = el('section', 'pk-readme-prov');
  sec.append(el('h3', null, 'Data provenance'));
  let any = false;
  const pb = provenanceBlock(page.page && page.page.title ? page.page.title : 'This page', readme.provenance, readme.caveats);
  if (pb) {
    sec.append(pb);
    any = true;
  }
  const cats = Object.entries(page.datasets || {}).filter(([, r]) => r && r.kind === 'catalog').map(([k]) => k);
  let nCat = 0;
  for (const ds of cats) {
    let client;
    try {
      client = await app.catalog(ds);
    } catch (err) {
      if (!(err instanceof Error)) throw err;
      sec.append(el('p', 'pk-warn-note', `Catalog ${ds} could not be loaded: ${err.message}`));
      any = true;
      continue;
    }
    const man = client.manifest || {};
    const b = provenanceBlock(`Catalog ${man.title || ds} (${fmtCount(client.n)} rows)`, man.provenance, man.caveats);
    if (b) {
      sec.append(b);
      any = true;
      nCat++;
    }
  }
  if (any) container.append(sec);
  return {catalogs: nCat};
}

export function mount(root, ctx) {
  const cfg = ctx.config || {};
  root.classList.add('pk-readme');
  const skel = el('div', 'pk-skeleton pk-readme-skeleton');
  root.append(skel);
  let destroyed = false;
  const inner = el('div', 'pk-readme-inner');
  renderReadme(inner, ctx.app, cfg).then(() => {
    if (destroyed) return;
    skel.remove();
    root.append(inner);
  }, (err) => {
    if (!(err instanceof FetchError) && !(err instanceof TypeError)) throw err;
    skel.remove();
    const n = el('div', 'pk-error-note');
    n.innerHTML = `The description could not be loaded: ${escapeHtml(err.message)}`;
    n.setAttribute('role', 'alert');
    root.append(n);
  }).catch((err) => {
    if (!(err instanceof Error)) throw err;
    console.warn(`readme ${ctx.id}`, err);
    ctx.setStatus('error', err.message);
  });
  return {
    resize() {},
    destroy() {
      destroyed = true;
    },
  };
}
