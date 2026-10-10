// Spectral feature atlases (ARCHITECTURE.md sections 5.10 and 9.4, pk-features/1): flattening,
// Doppler shift, coverage tests, label layout with collision dropping, drawing and the family
// toggles. Pure except drawFeatures (Canvas 2D / SVGContext) and featureToggles (DOM).
//
// Layout rules (legacy JWST_Archive_ColorPortal.py drawBig and NIRSpec_6927_SpecViewer.py):
//   - every feature inside the plot keeps its marker line (or band shading);
//   - names go into up to `rows` label rows, greedily left to right; a name that fits no row
//     is dropped (collision dropping);
//   - above `maxLabels` features in view no names are drawn at all (LABEL_MAX = 34);
//   - `requires_coverage` features are drawn dashed where the spectrum has no data.

export const C_KMS = 299792.458;
export const LABEL_MAX = 34;
export const ROW_GAP = 13;

const FALLBACK_COLORS = ['#58a6ff', '#e5534b', '#57ab5a', '#c69026', '#b083f0', '#39c5cf', '#ec775c', '#768390', '#d4a72c', '#6cb6ff'];

/** 1 + v/c for a velocity in km/s (non-finite or <= -c -> 1). */
export function dopplerFactor(vel) {
  const v = Number(vel);
  if (!Number.isFinite(v) || v <= -C_KMS) return 1;
  return 1 + v / C_KMS;
}

/** [{key, label, color, source, count}] for the families of a pk-features/1 document. */
export function familiesOf(doc) {
  if (!doc || typeof doc !== 'object') return [];
  const fams = doc.families && typeof doc.families === 'object' ? doc.families : {};
  const meta = (doc.meta && doc.meta.families) || {};
  return Object.keys(fams).map((key) => {
    const m = meta[key] || {};
    return {
      key,
      label: typeof m.label === 'string' && m.label ? m.label : key,
      color: typeof m.color === 'string' ? m.color : null,
      source: m.source || '',
      count: Array.isArray(fams[key]) ? fams[key].length : 0,
    };
  });
}

function finite(v) {
  return typeof v === 'number' && Number.isFinite(v);
}

/**
 * Flat, wavelength-sorted feature list for the enabled `families` (unknown families are
 * ignored). Wavelengths are rest values times dopplerFactor(vel). Entries without a finite
 * w_um are skipped. Returns [{name, label, w, w0, lo, hi, kind, color, family, group,
 * requiresCoverage}].
 */
export function featureList(doc, families, opts = {}) {
  if (!doc || !doc.families || !Array.isArray(families) || !families.length) return [];
  const f = dopplerFactor(opts.vel);
  const meta = (doc.meta && doc.meta.families) || {};
  const famKeys = Object.keys(doc.families);
  const out = [];
  for (const fam of families) {
    const list = doc.families[fam];
    if (!Array.isArray(list)) continue;
    const famIdx = famKeys.indexOf(fam);
    const famColor = (meta[fam] && meta[fam].color) || FALLBACK_COLORS[(famIdx < 0 ? 0 : famIdx) % FALLBACK_COLORS.length];
    for (const e of list) {
      if (!e || !finite(e.w_um)) continue;
      const kind = e.kind === 'band' ? 'band' : 'line';
      let lo = finite(e.lo_um) ? e.lo_um : null;
      let hi = finite(e.hi_um) ? e.hi_um : null;
      if (kind === 'band' && (lo === null || hi === null)) { lo = e.w_um; hi = e.w_um; }
      if (lo !== null && hi !== null && lo > hi) [lo, hi] = [hi, lo];
      out.push({
        name: String(e.name ?? e.label ?? ''),
        label: String(e.label ?? e.name ?? ''),
        w: e.w_um * f,
        w0: e.w_um,
        lo: lo === null ? null : lo * f,
        hi: hi === null ? null : hi * f,
        kind,
        color: typeof e.color === 'string' && e.color ? e.color : famColor,
        family: fam,
        group: e.group ?? null,
        requiresCoverage: !!e.requires_coverage,
      });
    }
  }
  out.sort((a, b) => a.w - b.w);
  return out;
}

/** True when `wave` (any array-like) is non-decreasing. */
export function isSorted(wave) {
  const n = wave ? wave.length : 0;
  for (let i = 1; i < n; i++) if (wave[i] < wave[i - 1]) return false;
  return true;
}

/** Index of the last sample with wave[i] <= w (-1 when none); wave ascending. */
export function lowerIndex(wave, w) {
  let lo = 0, hi = wave.length - 1;
  if (hi < 0 || !(w >= wave[0])) return -1;
  if (w >= wave[hi]) return hi;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (wave[mid] <= w) lo = mid;
    else hi = mid;
  }
  return lo;
}

function localSpacing(wave, k) {
  let d = Infinity;
  if (k > 0) { const a = wave[k] - wave[k - 1]; if (a > 0 && a < d) d = a; }
  if (k + 1 < wave.length) { const b = wave[k + 1] - wave[k]; if (b > 0 && b < d) d = b; }
  return d;
}

/**
 * True when the spectrum has a finite flux sample near w: one of the two samples bracketing w
 * is finite and lies within `tol` (default 1.5 x its local sample spacing; the smaller of the
 * two neighbouring spacings, so a sample at the edge of a gap does not cover the gap).
 * `wave` must be ascending; an unsorted wave falls back to a linear scan.
 */
export function hasCoverage(wave, flux, w, tol = null) {
  const n = wave ? wave.length : 0;
  if (!n || !Number.isFinite(w)) return false;
  const ok = (k) => k >= 0 && k < n && Number.isFinite(flux ? flux[k] : wave[k]);
  const near = (k) => {
    if (!ok(k)) return false;
    const d = Math.abs(wave[k] - w);
    const t = tol !== null && tol !== undefined ? tol : 1.5 * localSpacing(wave, k);
    return Number.isFinite(t) ? d <= t : d === 0;
  };
  if (!isSortedCached(wave)) {
    for (let k = 0; k < n; k++) if (near(k)) return true;
    return false;
  }
  const j = lowerIndex(wave, w);
  return near(j) || near(j + 1);
}

const sortedCache = new WeakMap();
function isSortedCached(wave) {
  if (typeof wave !== 'object' || wave === null) return true;
  let v = sortedCache.get(wave);
  if (v === undefined) {
    v = isSorted(wave);
    sortedCache.set(wave, v);
  }
  return v;
}

/** Coverage predicate for layoutFeatures from a spectrum ({wave, flux}) or null. */
export function coverageOf(spec) {
  if (!spec || !spec.wave || !spec.wave.length) return null;
  return (w) => hasCoverage(spec.wave, spec.flux, w);
}

function defaultMeasure(text, fontSize) {
  // A text-width estimate for layouts computed without a canvas (tests, SVG export).
  let w = 0;
  for (const ch of String(text)) w += /[A-Z0-9MW@#%]/.test(ch) ? 0.68 : /[ilj.,:;|' ]/.test(ch) ? 0.3 : 0.56;
  return w * fontSize;
}

/**
 * Pixel layout of features in a view (axes.js makeView). opts:
 *   rows = 3          label rows
 *   minGap = 6        px between labels in one row
 *   maxLabels = 34    above this many features in view no names are drawn
 *   fontSize = 10
 *   measure(text)     text width in px (default: estimate)
 *   coverage(w)       -> bool; requires_coverage features with false are dashed
 * Returns {items: [{f, px, px0, px1, row, labelled, dashed, labelX, labelW}], shown,
 *          labelled, dropped, named, dashed}.
 */
export function layoutFeatures(feats, view, opts = {}) {
  const rows = Math.max(0, opts.rows ?? 3);
  const minGap = opts.minGap ?? 6;
  const maxLabels = opts.maxLabels ?? LABEL_MAX;
  const fs = opts.fontSize ?? 10;
  const measure = typeof opts.measure === 'function' ? opts.measure : (t) => defaultMeasure(t, fs);
  const cov = typeof opts.coverage === 'function' ? opts.coverage : null;
  const P = view.plot;
  const sx = view.sx;
  const items = [];
  for (const f of feats || []) {
    let px = sx.f(f.w);
    let px0 = null, px1 = null;
    if (f.kind === 'band' && f.lo !== null && f.hi !== null) {
      const a = sx.f(f.lo), b = sx.f(f.hi);
      if (!Number.isFinite(a) || !Number.isFinite(b)) continue;
      px0 = Math.min(a, b);
      px1 = Math.max(a, b);
      if (px1 < P.left || px0 > P.right) continue;
      if (!Number.isFinite(px)) px = (px0 + px1) / 2;
    } else {
      if (!Number.isFinite(px) || px < P.left || px > P.right) continue;
    }
    const dashed = !!(f.requiresCoverage && cov && !cov(f.w));
    items.push({f, px, px0, px1, row: -1, labelled: false, dashed, labelX: px, labelW: 0});
  }
  items.sort((a, b) => a.px - b.px);
  const named = items.length <= maxLabels && rows > 0;
  let labelled = 0;
  if (named) {
    const last = new Array(rows).fill(-Infinity);
    for (const it of items) {
      const text = it.f.label || it.f.name;
      if (!text) continue;
      const w = measure(text);
      let x0 = it.px - w / 2;
      // keep labels inside the plot horizontally
      if (x0 < P.left + 1) x0 = P.left + 1;
      if (x0 + w > P.right - 1) x0 = Math.max(P.left + 1, P.right - 1 - w);
      for (let r = 0; r < rows; r++) {
        if (x0 >= last[r] + minGap) {
          it.row = r;
          it.labelled = true;
          it.labelX = x0;
          it.labelW = w;
          last[r] = x0 + w;
          labelled++;
          break;
        }
      }
    }
  }
  return {
    items,
    shown: items.length,
    labelled,
    dropped: items.length - labelled,
    named,
    dashed: items.reduce((s, it) => s + (it.dashed ? 1 : 0), 0),
    rows,
    fontSize: fs,
  };
}

/** The feature nearest to pixel px within tol (bands: inside the band). Null when none. */
export function featureAt(layout, px, tol = 4) {
  if (!layout || !layout.items) return null;
  let best = null, bd = Infinity;
  for (const it of layout.items) {
    let d;
    if (it.px0 !== null && it.px1 !== null && px >= it.px0 && px <= it.px1) d = Math.min(Math.abs(px - it.px), tol);
    else d = Math.abs(px - it.px);
    if (d <= tol && d < bd) { bd = d; best = it; }
  }
  return best;
}

/**
 * Draw a layout: band shading, marker lines (dashed for uncovered requires_coverage features)
 * and labels in rows from the top of the plot. opts: {tokens, alpha = 0.55, top (label row 0
 * baseline offset, px), labels = true}.
 */
export function drawFeatures(ctx, view, layout, opts = {}) {
  if (!layout || !layout.items.length) return 0;
  const P = view.plot;
  const tk = opts.tokens || {};
  const fs = layout.fontSize || 10;
  const alpha = opts.alpha ?? 0.55;
  const top0 = P.top + (opts.top ?? 2);
  ctx.save();
  ctx.beginPath();
  ctx.rect(P.left, P.top, P.width, P.height);
  ctx.clip();
  for (const it of layout.items) {
    if (it.px0 === null) continue;
    ctx.globalAlpha = 0.09;
    ctx.fillStyle = it.f.color;
    ctx.fillRect(it.px0, P.top, Math.max(1, it.px1 - it.px0), P.height);
  }
  ctx.lineWidth = 1;
  for (const it of layout.items) {
    ctx.globalAlpha = it.dashed ? alpha * 0.8 : alpha;
    ctx.strokeStyle = it.f.color;
    if (ctx.setLineDash) ctx.setLineDash(it.dashed ? [4, 3] : []);
    const x = Math.round(it.px) + 0.5;
    const y0 = it.labelled ? top0 + (it.row + 1) * ROW_GAP : P.top;
    ctx.beginPath();
    ctx.moveTo(x, y0);
    ctx.lineTo(x, P.bottom);
    ctx.stroke();
  }
  if (ctx.setLineDash) ctx.setLineDash([]);
  if (opts.labels !== false && layout.named) {
    ctx.font = `${fs}px ${tk.font || 'system-ui,sans-serif'}`;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'top';
    for (const it of layout.items) {
      if (!it.labelled) continue;
      const y = top0 + it.row * ROW_GAP;
      ctx.globalAlpha = 0.8;
      ctx.fillStyle = tk.plotBg || '#fff';
      ctx.fillRect(it.labelX - 1, y - 1, it.labelW + 2, fs + 2);
      ctx.globalAlpha = 1;
      ctx.fillStyle = it.f.color;
      ctx.fillText(it.f.label || it.f.name, it.labelX, y);
    }
  }
  ctx.restore();
  return layout.items.length;
}

/**
 * Family toggle chips writing store key `features`. `getDoc()` returns the current
 * pk-features/1 document (or null while loading). Returns {el, update(), destroy()}.
 */
export function featureToggles(store, getDoc, opts = {}) {
  const el = document.createElement('div');
  el.className = 'pk-feat-toggles';
  el.setAttribute('role', 'group');
  el.setAttribute('aria-label', opts.label || 'Spectral features');
  const source = opts.source || 'features';
  let builtKey = null;
  // chips are rebuilt only when the family list changes; a `features` change just flips
  // aria-pressed in place so a keyboard-toggled chip keeps focus
  const update = () => {
    const doc = getDoc();
    const fams = familiesOf(doc);
    const on = new Set(store.get('features') || []);
    const key = JSON.stringify(fams.map((f) => [f.key, f.label, f.count, f.color || '', f.source || '']));
    if (key === builtKey) {
      for (const b of el.querySelectorAll('.pk-feat-chip')) b.setAttribute('aria-pressed', on.has(b.dataset.family) ? 'true' : 'false');
      return;
    }
    builtKey = key;
    el.textContent = '';
    el.hidden = !fams.length;
    fams.forEach((fam, i) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'pk-btn pk-feat-chip';
      b.textContent = fam.label;
      b.title = `${fam.label}: ${fam.count} features${fam.source ? ` (${fam.source})` : ''}`;
      b.dataset.family = fam.key;
      b.setAttribute('aria-pressed', on.has(fam.key) ? 'true' : 'false');
      b.style.setProperty('--pk-feat-color', fam.color || FALLBACK_COLORS[i % FALLBACK_COLORS.length]);
      b.addEventListener('click', () => {
        const cur = new Set(store.get('features') || []);
        if (cur.has(fam.key)) cur.delete(fam.key);
        else cur.add(fam.key);
        // keep the document's family order so hashes are stable
        const next = familiesOf(getDoc()).map((f) => f.key).filter((k) => cur.has(k));
        for (const k of cur) if (!next.includes(k)) next.push(k);
        store.set({features: next}, {source});
      });
      el.append(b);
    });
  };
  const off = store.on(['features'], update);
  update();
  return {
    el,
    update,
    destroy() {
      if (typeof off === 'function') off();
      el.remove();
    },
  };
}
