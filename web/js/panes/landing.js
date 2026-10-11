// Landing pane (archetype landing; capabilities cross_page_nav, readme_provenance,
// source_list_search; ARCHITECTURE.md sections 3, 4.2 and 6.10).
//
// Cards for the portal's pages (portal.json `pages`, the current page left out) and related
// portals (login-required badge for auth "secure"), with counts (option counts): portal.json
// `counts[]` ({page, ds, kind, n, unit?, units?}, section 4.2) when present, without fetching
// anything else; a portal.json without `counts` falls back to the row counts of the manifests
// listed in `search`. A global search box (option search) looks
// an id, alias or position up in every searchable catalog of the portal (DataClient.find) and
// links each hit to its page with `#id=<id>`. Below the cards the page readme and the provenance
// section are rendered (panes/readme.js).
//
// Search workers (IMPLEMENTATION_NOTES 85): one DataClient per distinct search manifest URL,
// opened lazily on the first keystroke in the search box (or on submit), never at mount. A
// manifest that is one of this page's own catalog datasets reuses the page's client. Typing runs
// a debounced search; Enter / the Search button runs it at once.

import {plural} from '../core/fmt.js';
import {fetchJSON, FetchError} from '../data/fetch.js';
import {DataClient} from '../data/client.js';
import {carryTo, loadPortal, loginBadge} from '../ui/nav.js';
import {renderReadme} from './readme.js';

export const meta = {type: 'landing', caps: ['cross_page_nav', 'readme_provenance', 'source_list_search'], needs: []};

const SEARCH_LIMIT = 20;
const SEARCH_DEBOUNCE_MS = 300;
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

/**
 * Searchable entries of portal.json `search` grouped by resolved manifest URL:
 * [{url, targets: [entry, ...]}] in first-seen order. Entries need `manifest` and `page`;
 * manifests resolve against `base` (the portal.json URL). Unresolvable manifests are skipped.
 */
export function searchManifests(search, base) {
  const groups = new Map();
  for (const s of Array.isArray(search) ? search : []) {
    if (!s || typeof s !== 'object' || !s.manifest || !s.page) continue;
    let url;
    try {
      url = new URL(String(s.manifest), base || undefined).toString();
    } catch (err) {
      if (!(err instanceof TypeError)) throw err;
      continue;
    }
    if (!groups.has(url)) groups.set(url, {url, targets: []});
    groups.get(url).targets.push(s);
  }
  return Array.from(groups.values());
}

/** Default card nouns per `counts[].kind` (section 4.2); an unknown kind counts items. */
export const COUNT_NOUNS = Object.freeze({
  catalog: ['source', 'sources'], images: ['image', 'images'], records: ['spectrum', 'spectra'],
});

const isCount = (n) => typeof n === 'number' && Number.isInteger(n) && n >= 0;
const noun = (s) => (typeof s === 'string' && s.trim() ? s.trim() : null);

/**
 * Valid portal.json `counts[]` entries grouped by page: Map page id -> [{ds, kind, n, unit,
 * units}] in document order. Entries need a page id and an integer n >= 0; the nouns default
 * from the kind (COUNT_NOUNS; `unit + 's'` when only the singular is given, the plural for both
 * when only it is given).
 */
export function countEntries(counts) {
  const out = new Map();
  for (const c of Array.isArray(counts) ? counts : []) {
    if (!c || typeof c !== 'object' || typeof c.page !== 'string' || !c.page || !isCount(c.n)) continue;
    const kind = typeof c.kind === 'string' ? c.kind : '';
    const def = COUNT_NOUNS[kind] || ['item', 'items'];
    const one = noun(c.unit), many = noun(c.units);
    const unit = one || many || def[0];
    const units = many || (one ? `${one}s` : def[1]);
    if (!out.has(c.page)) out.set(c.page, []);
    out.get(c.page).push({ds: typeof c.ds === 'string' ? c.ds : '', kind, n: c.n, unit, units});
  }
  return out;
}

/**
 * Card count text and tooltip for one page's entries ([{ds, n, unit, units}]): entries with the
 * same nouns are summed ("1,234 sources"), different nouns are listed in first-seen order
 * ("165 sources · 20 spectra"); n == 1 takes the singular. The title lists every dataset.
 * Returns {text, title}; text '' for an empty list.
 */
export function countLabel(list) {
  const groups = new Map();
  for (const e of Array.isArray(list) ? list : []) {
    if (!e || !isCount(e.n)) continue;
    const k = `${e.unit}\u0000${e.units}`;
    if (!groups.has(k)) groups.set(k, {n: 0, unit: e.unit, units: e.units});
    groups.get(k).n += e.n;
  }
  const text = Array.from(groups.values()).map((g) => plural(g.n, g.unit, g.units)).join(' · ');
  const title = (Array.isArray(list) ? list : []).filter((e) => e && isCount(e.n))
    .map((e) => `${e.title || e.ds || '?'}: ${plural(e.n, e.unit, e.units)}`).join('\n');
  return {text, title};
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
  const opened = new Map();     // manifest URL -> Promise<{client, mine}>
  const failedOpen = new Set(); // manifest URLs whose open failed (reopened only on submit)
  let searchSeq = 0;
  let searchTimer = 0;

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

  let countSource = null; // 'counts' | 'search' once counts were rendered from that source

  // portal.json `counts[]` first (no other fetch); without it, the `search` manifests' row counts.
  async function loadCounts() {
    if (!countsOn || !portal) return;
    let byPage;
    if (Array.isArray(portal.counts)) {
      byPage = countEntries(portal.counts);
      countSource = 'counts';
    } else if (Array.isArray(portal.search)) {
      byPage = new Map();
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
        byPage.get(key).push({ds: s.ds, n, title: man.title || s.ds, unit: 'source', units: 'sources'});
      }));
      countSource = 'search';
    } else {
      return;
    }
    if (destroyed) return;
    for (const card of cardsEl.querySelectorAll('.pk-card-link[data-page]')) {
      const list = byPage.get(card.dataset.page);
      if (!list || !list.length) continue;
      const {text, title} = countLabel(list);
      const cnt = card.querySelector('.pk-card-count');
      cnt.textContent = text;
      cnt.title = title;
    }
  }

  // ------------------------------------------------------------------ global search

  function searchGroups() {
    return portal ? searchManifests(portal.search, portal._url) : [];
  }

  /**
   * Client for one manifest URL, opened on first use (one worker per manifest). A failed open
   * stays cached for the life of the pane, so typing does not start a new worker per keystroke;
   * `retry` (an explicit submit) opens a failed manifest again.
   */
  function clientFor(url, {retry = false} = {}) {
    if (retry && failedOpen.has(url)) {
      failedOpen.delete(url);
      opened.delete(url);
    }
    if (opened.has(url)) return opened.get(url);
    // Reuse this page's own client when the manifest is one of its datasets.
    const own = Object.entries(page.datasets || {}).find(([, r]) => r && r.kind === 'catalog'
      && new URL(app.hub && app.hub.resolve ? app.hub.resolve(r.url) : r.url, document.baseURI).toString() === url);
    const p = own ? app.catalog(own[0]).then((c) => ({client: c, mine: false}))
      : DataClient.open(url).then((c) => ({client: c, mine: true}));
    p.then(null, (err) => {
      if (!(err instanceof Error)) throw err;
      if (opened.get(url) === p) failedOpen.add(url);
    });
    opened.set(url, p);
    return p;
  }

  /**
   * Open every search manifest's client (first keystroke, or submit with `retry` to reopen the
   * failed ones); failures surface in the search.
   */
  function warm(retry = false) {
    for (const g of searchGroups()) {
      clientFor(g.url, {retry}).then(null, (err) => {
        if (!(err instanceof Error)) throw err;
      });
    }
  }

  function renderSearch() {
    searchBox.textContent = '';
    const groups0 = searchGroups();
    if (!searchOn || !groups0.length) {
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
      // One find per manifest; entries sharing a manifest (several pages) share its hits.
      const perManifest = await Promise.all(searchGroups().map(async (mg) => {
        try {
          const {client} = await clientFor(mg.url);
          const idx = await client.find(q, {limit: SEARCH_LIMIT});
          const ids = await Promise.all(Array.from(idx || []).slice(0, SEARCH_LIMIT).map((i) => client.id(i)));
          return mg.targets.map((s) => ({s, ids, error: null}));
        } catch (err) {
          if (!(err instanceof Error)) throw err;
          if (err.name === 'AbortError') return mg.targets.map((s) => ({s, ids: [], error: null}));
          return mg.targets.map((s) => ({s, ids: [], error: err}));
        }
      }));
      const groups = perManifest.flat();
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
      clearTimeout(searchTimer);
      warm(true);
      run().catch(report);
    });
    // Workers start on the first keystroke, not at mount (note 85). Later input reuses them
    // (a paste without a keydown opens them through run()).
    input.addEventListener('keydown', () => warm(), {once: true});
    input.addEventListener('input', () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => { run().catch(report); }, SEARCH_DEBOUNCE_MS);
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
      clearTimeout(searchTimer);
      for (const p of opened.values()) {
        p.then(({client, mine}) => { if (mine) client.terminate(); }, (err) => {
          // A failed open was already reported by the search that triggered it.
          if (!(err instanceof Error)) throw err;
        });
      }
      opened.clear();
      failedOpen.clear();
    },
    stats() {
      return {searchClients: opened.size, counts: countSource};
    },
  };
}
