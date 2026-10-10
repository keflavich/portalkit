// Portal navigation (capability cross_page_nav, ARCHITECTURE.md sections 4.2 and 6.5).
//
// Renders portal.json (`page.portal.nav`) into app.slots.nav: one link per built page (the
// current one marked aria-current) and the related links, with a "login required" badge on
// related entries whose auth is "secure". Before a sibling page is opened, the focus and the
// selection are stashed with carry.js (stashCarry) and `nav:carry {href}` is emitted.
// Below the mobile breakpoint the links fold into a menu button.

import {stashCarry} from '../core/carry.js';
import {fetchJSON, FetchError} from '../data/fetch.js';

const portals = new WeakMap();   // app -> Promise<portal.json | null>

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

/** URL of portal.json for this page (page.portal.nav, default "portal.json"). */
export function portalUrl(app) {
  const nav = (app.page && app.page.portal && app.page.portal.nav) || 'portal.json';
  const base = typeof document !== 'undefined' && document.baseURI ? document.baseURI : undefined;
  return new URL(nav, base).toString();
}

/**
 * portal.json of this page (cached per app). Resolves to null when it cannot be loaded (the
 * failure is logged once); other errors propagate.
 */
export function loadPortal(app) {
  if (portals.has(app)) return portals.get(app);
  const url = portalUrl(app);
  const p = fetchJSON(url, {cache: 'no-cache'}).then((doc) => {
    if (!doc || typeof doc !== 'object') return null;
    doc._url = url;
    return doc;
  }, (err) => {
    if (!(err instanceof FetchError)) throw err;
    console.warn(`portal navigation unavailable: ${err.message}`);
    return null;
  });
  portals.set(app, p);
  return p;
}

/** Same-portal page link: true when `href` resolves to one of the portal's pages. */
function isPortalPage(doc, href) {
  if (!doc || !Array.isArray(doc.pages)) return false;
  const u = new URL(href, document.baseURI);
  return doc.pages.some((pg) => pg && pg.file && new URL(pg.file, doc._url).pathname === u.pathname);
}

/** Stash the carry payload and announce the navigation (used by nav and by panes' page links). */
export function carryTo(app, href) {
  stashCarry(app.portalId, app.carryPayload());
  app.store.emit('nav:carry', {href: String(href)});
}

export function loginBadge() {
  const b = el('span', 'pk-badge pk-warn pk-login-badge', 'login required');
  b.title = 'Opening this link requires a login';
  return b;
}

export function install(app) {
  const slot = app.slots && app.slots.nav;
  if (!slot) return null;
  const pageId = app.page && app.page.page && app.page.page.id;
  const pageFile = typeof location !== 'undefined' ? location.pathname.split('/').pop() : '';

  const render = (doc) => {
    slot.textContent = '';
    if (!doc) return;
    const pages = Array.isArray(doc.pages) ? doc.pages.filter((p) => p && p.file) : [];
    const related = Array.isArray(doc.related) ? doc.related.filter((r) => r && r.url) : [];
    if (pages.length < 2 && !related.length) return;
    const toggle = el('button', 'pk-btn pk-nav-toggle', 'Pages');
    toggle.type = 'button';
    toggle.setAttribute('aria-expanded', 'false');
    const menu = el('div', 'pk-nav-menu');
    menu.id = 'pk-nav-menu';
    toggle.setAttribute('aria-controls', menu.id);
    const list = el('ul', 'pk-nav-list');
    for (const pg of pages) {
      const li = el('li', 'pk-nav-item');
      const a = el('a', 'pk-nav-link', pg.title || pg.id || pg.file);
      a.href = new URL(pg.file, doc._url).toString();
      a.dataset.page = pg.id || '';
      if (pg.archetype) a.dataset.archetype = pg.archetype;
      const current = (pageId && pg.id === pageId) || (!pageId && pg.file === pageFile);
      if (current) {
        a.setAttribute('aria-current', 'page');
        a.classList.add('pk-current');
      }
      li.append(a);
      list.append(li);
    }
    if (pages.length >= 2) menu.append(list);
    if (related.length) {
      const rel = el('ul', 'pk-nav-list pk-nav-related');
      rel.setAttribute('aria-label', 'Related portals');
      for (const r of related) {
        const li = el('li', 'pk-nav-item');
        const a = el('a', 'pk-nav-link pk-nav-ext', r.title || r.url);
        a.href = new URL(r.url, doc._url).toString();
        if (r.auth === 'secure') a.append(' ', loginBadge());
        li.append(a);
        rel.append(li);
      }
      menu.append(rel);
    }
    slot.append(toggle, menu);
    toggle.addEventListener('click', () => {
      const open = toggle.getAttribute('aria-expanded') !== 'true';
      toggle.setAttribute('aria-expanded', String(open));
      slot.classList.toggle('pk-nav-open', open);
    });
    document.addEventListener('click', (ev) => {
      if (!slot.contains(ev.target) && slot.classList.contains('pk-nav-open')) {
        slot.classList.remove('pk-nav-open');
        toggle.setAttribute('aria-expanded', 'false');
      }
    });
    const onFollow = (ev) => {
      const a = ev.target && ev.target.closest ? ev.target.closest('a[href]') : null;
      if (!a || a.getAttribute('aria-current') === 'page') return;
      if (ev.type === 'auxclick' && ev.button !== 1) return;
      if (isPortalPage(doc, a.href)) carryTo(app, a.href);
    };
    menu.addEventListener('click', onFollow);
    menu.addEventListener('auxclick', onFollow);
  };

  // Not awaited by boot: the page becomes ready while portal.json loads.
  const ready = loadPortal(app).then(render);
  return {ready};
}
