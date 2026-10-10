// Landing pane (archetype landing; capabilities cross_page_nav, readme_provenance,
// source_list_search; ARCHITECTURE.md sections 3, 4.2 and 6.10).
//
// Cards for the portal's pages (portal.json `pages`, the current page left out) and related
// portals (login-required badge for auth "secure"), with row counts read from the manifests
// listed in portal.json `search` (option counts). A global search box (option search) looks
// an id, alias or position up in every searchable catalog of the portal (DataClient.find,
// clients opened on first use) and links each hit to its page with `#id=<id>`. Below the cards
// the page readme and the provenance section are rendered (panes/readme.js).

import {fmtCount, plural} from '../core/fmt.js';
import {fetchJSON, FetchError} from '../data/fetch.js';
import {DataClient} from '../data/client.js';
import {carryTo, loadPortal, loginBadge} from '../ui/nav.js';
import {renderReadme} from './readme.js';

export const meta = {type: 'landing', caps: ['cross_page_nav', 'readme_provenance', 'source_list_search'], needs: []};

const SEARCH_LIMIT = 20;
const ARCHETYPE_LABELS = {
  explorer: 'Explorer', manifold: 'Manifold', gallery: 'Gallery', spectra: 'Spectra',
  wavelength: 'Wavelength tour', narrative: 'Story', landing: 'Overview',
};

/**
 * Card descriptors from portal.json and the `cards` option:
 * [{key, title, href, text, archetype, page, external, auth}].
 * cards "auto" (default) lists every page except `currentId` and every related portal; an array
 * of {page?, title?, href?, text?} picks and annotates cards explicitly.
 */
export function cardList(portal, cards = 'auto', currentId = null, currentFile = null) {
  const pages = portal && Array.isArray(portal.pages) ? portal.pages.filter((p) => p && p.file) : [];
  const related = portal && Array.isArray(portal.related) ? portal.related.filter((r) => r && r.url) : [];
  const base = portal && portal._url ? portal._url : undefined;
  const abs = (u) => (base ? new URL(u, base).toString() : String(u));
  const fromPage = (p, extra = {}) => ({
    key: `page:${p.id || p.file}`,
    title: extra.title || p.title || p.id || p.file,
    href: abs(p.file),
    text: extra.text || '',
    archetype: p.archetype || null,
    page: p.id || null,
    external: false,
    auth: null,
  });
  if (Array.isArray(cards)) {
    const out = [];
    for (const c of cards) {
      if (!c || typeof c !== 'object') continue;
      const p = c.page ? pages.find((x) => x.id === c.page) : null;
      if (p) out.push(fromPage(p, c));
      else if (c.href || c.url) {
        out.push({key: `link:${c.href || c.url}`, title: c.title || c.href || c.url, href: abs(c.href || c.url),
          text: c.text || '', archetype: null, page: null, external: true, auth: c.auth || null});
      }
    }
    return out;
  }
  const out = pages
    .filter((p) => !(currentId && p.id === currentId) && !(!currentId && currentFile && p.file === currentFile))
    .map((p) => fromPage(p));
  for (const r of related) {
    out.push({key: `rel:${r.url}`, title: r.title || r.url, href: abs(r.url), text: '', archetype: null, page: null,
      external: true, auth: r.auth || null});
  }
  return out;
}

/** Link to a source on its page: `<file>#id=<id>` (id URL-encoded). */
export function sourceHref(pageHref, id) {
  const u = String(pageHref).split('#')[0];
  return `${u}#id=${encodeURIComponent(String(id))}`;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

export function mount(root, ctx) {
  const cfg = ctx.config || {};
  const app = ctx.app;
  const page = app.page || {};
  const countsOn = cfg.counts !== false;
  const searchOn = cfg.search !== false;
  const currentId = page.page && page.page.id ? page.page.id : null;
  const currentFile = typeof location !== 'undefined' ? location.pathname.split('/').pop() : null;

  root.classList.add('pk-landing');
  const head = el('header', 'pk-landing-head');
  const title = el('h2', 'pk-landing-title', (page.portal && page.portal.title) || (page.page && page.page.title) || 'Overview');
  head.append(title);
  if (page.page && page.page.subtitle) head.append(el('p', 'pk-landing-sub pk-muted', page.page.subtitle));
  const searchBox = el('div', 'pk-landing-search');
  const cardsEl = el('div', 'pk-cards');
  cardsEl.setAttribute('role', 'list');
  const readmeEl = el('div', 'pk-landing-readme');
  root.append(head, searchBox, cardsEl, readmeEl);
  cardsEl.append(el('div', 'pk-skeleton pk-card-skeleton'), el('div', 'pk-skeleton pk-card-skeleton'));

  let destroyed = false;
  let portal = null;
  const opened = new Map();     // search entry key -> Promise<DataClient>
  let searchSeq = 0;

  function report(err) {
    if (!(err instanceof Error)) throw err;
    if (err.name === 'AbortError') return;
    console.warn(`landing ${ctx.id}`, err);
    ctx.setStatus('error', err.message);
  }

  function pageFile(pageId) {
    const p = portal && Array.isArray(portal.pages) ? portal.pages.find((x) => x.id === pageId) : null;
    return p ? new URL(p.file, portal._url).toString() : null;
  }

  function follow(ev, a, internal) {
    if (ev.type === 'auxclick' && ev.button !== 1) return;
    if (internal) carryTo(app, a.href);
  }

  // ------------------------------------------------------------------ cards

  function renderCards() {
    cardsEl.textContent = '';
    const list = cardList(portal, cfg.cards === undefined ? 'auto' : cfg.cards, currentId, currentFile);
    if (!list.length) {
      cardsEl.append(el('p', 'pk-muted', portal ? 'This portal has no other pages.' : 'The portal page list is not available.'));
      return;
    }
    for (const c of list) {
      const card = el('a', `pk-card-link${c.external ? ' pk-card-ext' : ''}`);
      card.setAttribute('role', 'listitem');
      card.href = c.href;
      card.dataset.key = c.key;
      if (c.page) card.dataset.page = c.page;
      card.append(el('span', 'pk-card-title', c.title));
      const metaLine = el('span', 'pk-card-meta');
      if (c.archetype) metaLine.append(el('span', 'pk-chip', ARCHETYPE_LABELS[c.archetype] || c.archetype));
      if (c.external) metaLine.append(el('span', 'pk-chip', 'related'));
      if (c.auth === 'secure') metaLine.append(loginBadge());
      const cnt = el('span', 'pk-card-count pk-muted pk-num');
      metaLine.append(cnt);
      card.append(metaLine);
      if (c.text) card.append(el('span', 'pk-card-text', c.text));
      const internal = !c.external;
      card.addEventListener('click', (ev) => follow(ev, card, internal));
      card.addEventListener('auxclick', (ev) => follow(ev, card, internal));
      cardsEl.append(card);
    }
  }

  async function loadCounts() {
    if (!countsOn || !portal || !Array.isArray(portal.search)) return;
    const byPage = new Map();
    await Promise.all(portal.search.map(async (s) => {
      if (!s || !s.manifest) return;
      let man;
      try {
        man = await fetchJSON(new URL(s.manifest, portal._url).toString());
      } catch (err) {
        if (!(err instanceof FetchError)) throw err;
        console.warn(`landing: manifest ${s.manifest} unavailable: ${err.message}`);
        return;
      }
      const n = Number.isFinite(man && man.n) ? man.n : null;
      if (n === null) return;
      const key = s.page || '';
      if (!byPage.has(key)) byPage.set(key, []);
      byPage.get(key).push({ds: s.ds, n, title: man.title || s.ds});
    }));
    if (destroyed) return;
    for (const card of cardsEl.querySelectorAll('.pk-card-link[data-page]')) {
      const list = byPage.get(card.dataset.page);
      if (!list) continue;
      const total = list.reduce((a, b) => a + b.n, 0);
      const cnt = card.querySelector('.pk-card-count');
      cnt.textContent = plural(total, 'source');
      cnt.title = list.map((x) => `${x.title}: ${fmtCount(x.n)}`).join('\n');
    }
  }

  // ------------------------------------------------------------------ global search

  function searchTargets() {
    return portal && Array.isArray(portal.search) ? portal.search.filter((s) => s && s.manifest && s.page) : [];
  }

  function clientFor(s) {
    const key = `${s.page}\u0000${s.ds}\u0000${s.manifest}`;
    if (opened.has(key)) return opened.get(key);
    const url = new URL(s.manifest, portal._url).toString();
    // Reuse this page's own client when the manifest is one of its datasets.
    const own = Object.entries(page.datasets || {}).find(([, r]) => r && r.kind === 'catalog'
      && new URL(app.hub && app.hub.resolve ? app.hub.resolve(r.url) : r.url, document.baseURI).toString() === url);
    const p = own ? app.catalog(own[0]).then((c) => ({client: c, mine: false}))
      : DataClient.open(url).then((c) => ({client: c, mine: true}));
    p.catch(() => opened.delete(key));
    opened.set(key, p);
    return p;
  }

  function renderSearch() {
    searchBox.textContent = '';
    const targets = searchTargets();
    if (!searchOn || !targets.length) {
      searchBox.hidden = true;
      return;
    }
    searchBox.hidden = false;
    const form = el('form', 'pk-landing-form');
    form.setAttribute('role', 'search');
    const input = el('input', 'pk-landing-q');
    input.type = 'search';
    input.placeholder = 'Find a source by id, alias or RA Dec';
    input.setAttribute('aria-label', 'Find a source in this portal by id, alias or position');
    input.autocomplete = 'off';
    input.spellcheck = false;
    const go = el('button', 'pk-btn pk-primary', 'Search');
    go.type = 'submit';
    form.append(input, go);
    const out = el('div', 'pk-landing-results');
    out.setAttribute('aria-live', 'polite');
    searchBox.append(form, out);

    const run = async () => {
      const q = input.value.trim();
      const my = ++searchSeq;
      out.textContent = '';
      if (!q) return;
      out.append(el('p', 'pk-muted', 'Searching…'));
      const groups = await Promise.all(targets.map(async (s) => {
        try {
          const {client} = await clientFor(s);
          const idx = await client.find(q, {limit: SEARCH_LIMIT});
          const ids = await Promise.all(Array.from(idx || []).slice(0, SEARCH_LIMIT).map((i) => client.id(i)));
          return {s, ids, error: null};
        } catch (err) {
          if (!(err instanceof Error)) throw err;
          if (err.name === 'AbortError') return {s, ids: [], error: null};
          return {s, ids: [], error: err};
        }
      }));
      if (my !== searchSeq || destroyed) return;
      out.textContent = '';
      let total = 0;
      for (const g of groups) {
        if (g.error) {
          out.append(el('p', 'pk-warn-note', `${g.s.ds}: ${g.error.message}`));
          continue;
        }
        if (!g.ids.length) continue;
        total += g.ids.length;
        const href = pageFile(g.s.page);
        const pg = portal.pages.find((x) => x.id === g.s.page);
        const sec = el('section', 'pk-landing-hits');
        sec.append(el('h4', null, `${pg ? pg.title || pg.id : g.s.page} · ${plural(g.ids.length, 'match', 'matches')}${g.ids.length >= SEARCH_LIMIT ? ' (first shown)' : ''}`));
        const ul = el('ul');
        for (const id of g.ids) {
          const li = el('li');
          if (href) {
            const a = el('a', 'pk-mono', String(id));
            a.href = sourceHref(href, id);
            a.addEventListener('click', (ev) => follow(ev, a, true));
            a.addEventListener('auxclick', (ev) => follow(ev, a, true));
            li.append(a);
          } else {
            li.append(el('span', 'pk-mono', String(id)));
          }
          ul.append(li);
        }
        sec.append(ul);
        out.append(sec);
      }
      if (!total && !out.childNodes.length) out.append(el('p', 'pk-muted', `No source matches “${q}”.`));
    };
    form.addEventListener('submit', (ev) => {
      ev.preventDefault();
      run().catch(report);
    });
  }

  // ------------------------------------------------------------------ load

  loadPortal(app).then((doc) => {
    if (destroyed) return;
    portal = doc;
    renderCards();
    renderSearch();
    return loadCounts();
  }).catch(report);

  renderReadme(readmeEl, app, {}, {quietEmpty: true}).catch((err) => {
    if (!(err instanceof Error)) throw err;
    readmeEl.append(el('p', 'pk-warn-note', `The description could not be loaded: ${err.message}`));
  });

  return {
    resize() {},
    destroy() {
      destroyed = true;
      for (const p of opened.values()) {
        p.then(({client, mine}) => { if (mine) client.terminate(); }, (err) => {
          // A failed open was already reported by the search that triggered it.
          if (!(err instanceof Error)) throw err;
        });
      }
      opened.clear();
    },
  };
}
