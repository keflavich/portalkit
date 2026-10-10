// 2D spectrum pane (ARCHITECTURE.md sections 5.8, 6.8; capability spectrum_2d_viewer).
// Shows the focused source's rectified 2D spectrum: pki1 records (quantized stretched image,
// colormap applied here; stretch 'stored' or a re-stretch of the physical values) or PNG mode
// ({png, extent: {w: [w0, w1], y: [y0, y1]}}). With options.link1d (default true) the x axis
// follows store spec.xr, so it stays aligned with the 1D spectrum pane; wheel and drag here
// write spec.xr back (a two-way link). While spec.xr is unset, the linked default range is the
// 1D pane's full extent (linkedFullExtent), so both panes start on the same wavelengths.

import {makeView, zoomDomain, panDomain} from '../plot/axes.js';
import {prepareCanvas, tokensOf, drawAxes, cmapTable, CMAP_NAMES} from '../plot/canvas2d.js';
import {eventToViewPx} from '../plot/brushui.js';
import {pki1Values} from '../data/records.js';
import {pageDatasets, raf} from './sed.js';
import {validRange, sig7, linkedFullExtent} from './spectrum1d.js';

export const meta = {type: 'spectrum2d', caps: ['spectrum_2d_viewer'], needs: ['records']};

export const STRETCHES = Object.freeze(['stored', 'linear', 'sqrt', 'asinh', 'log']);
export const MISSING = 256;

// ------------------------------------------------------------------ pure helpers

/** Nearest sample index of w in a monotonic array (ascending or descending); -1 outside half a step. */
export function nearestIndex(wave, w) {
  const n = wave ? wave.length : 0;
  if (!n || !Number.isFinite(w)) return -1;
  if (n === 1) return w === wave[0] ? 0 : -1;
  const asc = wave[n - 1] >= wave[0];
  const first = wave[0], last = wave[n - 1];
  const h0 = Math.abs(wave[1] - wave[0]) / 2, h1 = Math.abs(wave[n - 1] - wave[n - 2]) / 2;
  const lo = Math.min(first, last), hi = Math.max(first, last);
  const hlo = asc ? h0 : h1, hhi = asc ? h1 : h0;
  if (w < lo - hlo || w > hi + hhi) return -1;
  let a = 0, b = n - 1;
  while (b - a > 1) {
    const m = (a + b) >> 1;
    if (asc ? wave[m] <= w : wave[m] >= w) a = m;
    else b = m;
  }
  return Math.abs(wave[a] - w) <= Math.abs(wave[b] - w) ? a : b;
}

function quantile(sorted, q) {
  if (!sorted.length) return NaN;
  const p = Math.max(0, Math.min(sorted.length - 1, q * (sorted.length - 1)));
  const i = Math.floor(p), f = p - i;
  return i + 1 < sorted.length ? sorted[i] + (sorted[i + 1] - sorted[i]) * f : sorted[i];
}

/**
 * LUT indices (Uint16Array, 0..255; MISSING = 256) of a decoded pki1 image for a stretch:
 * 'stored' keeps the record's own stretch (u8 codes, u16 codes rescaled, u16 code 0 missing);
 * 'linear' / 'sqrt' / 'asinh' / 'log' re-stretch the physical values (pki1Values) between the
 * qlo / qhi quantiles of the finite values (sampled to at most 50 000). Returns
 * {codes, lo, hi} (lo/hi null for 'stored').
 */
export function stretchCodes(img, mode = 'stored', opts = {}) {
  const N = img.nx * img.ny;
  const codes = new Uint16Array(N);
  if (mode === 'stored' || !STRETCHES.includes(mode)) {
    const u16 = img.enc === 'u16';
    for (let i = 0; i < N; i++) {
      const c = img.data[i];
      if (u16) codes[i] = c === 0 ? MISSING : Math.round(((c - 1) / 65534) * 255);
      else codes[i] = c;
    }
    return {codes, lo: null, hi: null};
  }
  const vals = opts.values || pki1Values(img);
  const step = Math.max(1, Math.floor(N / 50000));
  const samp = [];
  for (let i = 0; i < N; i += step) if (Number.isFinite(vals[i])) samp.push(vals[i]);
  samp.sort((a, b) => a - b);
  let lo = quantile(samp, opts.qlo ?? 0.01), hi = quantile(samp, opts.qhi ?? 0.995);
  if (!(hi > lo)) { hi = lo + (Math.abs(lo) || 1); }
  const a = opts.a ?? 0.1;
  const as1 = Math.asinh(1 / a);
  const f = mode === 'sqrt' ? (t) => Math.sqrt(t)
    : mode === 'asinh' ? (t) => Math.asinh(t / a) / as1
      : mode === 'log' ? (t) => Math.log10(1 + 1000 * t) / Math.log10(1001)
        : (t) => t;
  const span = hi - lo;
  for (let i = 0; i < N; i++) {
    const v = vals[i];
    if (!Number.isFinite(v)) { codes[i] = MISSING; continue; }
    let t = (v - lo) / span;
    t = t < 0 ? 0 : t > 1 ? 1 : t;
    codes[i] = Math.round(f(t) * 255);
  }
  return {codes, lo, hi};
}

/**
 * Fill an RGBA buffer (`out.data`, `out.width` x `out.height` device pixels covering the view's
 * plot rectangle at scale r) from LUT codes: nearest image column per output column
 * (wavelength through view.sx), nearest row per output row (y through view.sy). Missing codes
 * and pixels outside the image are transparent. Returns the number of output columns that
 * hit the image.
 */
export function resample(img, codes, lut, view, out, r = 1) {
  const W = out.width, H = out.height, D = out.data;
  const P = view.plot;
  const cols = new Int32Array(W);
  let hit = 0;
  for (let x = 0; x < W; x++) {
    const w = view.sx.inv(P.left + (x + 0.5) / r);
    cols[x] = nearestIndex(img.wave, w);
    if (cols[x] >= 0) hit++;
  }
  const ny = img.ny;
  const y0 = img.y && img.y.length ? img.y[0] : 0;
  const dy = img.y && img.y.length > 1 ? img.y[1] - img.y[0] : 1;
  for (let yy = 0; yy < H; yy++) {
    const yv = view.sy.inv(P.top + (yy + 0.5) / r);
    const row = Math.round((yv - y0) / dy);
    const base = yy * W * 4;
    if (!(row >= 0 && row < ny)) {
      for (let x = 0; x < W; x++) D[base + 4 * x + 3] = 0;
      continue;
    }
    const rb = row * img.nx;
    for (let x = 0; x < W; x++) {
      const o = base + 4 * x;
      const c = cols[x];
      const k = c >= 0 ? codes[rb + c] : MISSING;
      if (k === MISSING) { D[o + 3] = 0; continue; }
      D[o] = lut[3 * k];
      D[o + 1] = lut[3 * k + 1];
      D[o + 2] = lut[3 * k + 2];
      D[o + 3] = 255;
    }
  }
  return hit;
}

/** Full extents {w: [w0, w1], y: [y0, y1]} of a pki1 image (half-pixel padded) or PNG entry. */
export function imageExtent(rec, natural = null) {
  if (!rec) return null;
  if (rec.kind === 'png') {
    const e = rec.extent || {};
    const w = validRange(e.w) || (natural ? [0, natural.w] : null);
    const y = validRange(e.y) || (natural ? [0, natural.h] : [0, 1]);
    return w ? {w, y} : null;
  }
  const n = rec.wave ? rec.wave.length : 0;
  if (!n) return null;
  const a = Math.min(rec.wave[0], rec.wave[n - 1]), b = Math.max(rec.wave[0], rec.wave[n - 1]);
  const hw = n > 1 ? Math.abs(rec.wave[1] - rec.wave[0]) / 2 : Math.abs(a) * 1e-3 || 0.5;
  const ys = rec.y || [];
  const dy = ys.length > 1 ? Math.abs(ys[1] - ys[0]) : 1;
  const y0 = ys.length ? Math.min(ys[0], ys[ys.length - 1]) - dy / 2 : -0.5;
  const y1 = ys.length ? Math.max(ys[0], ys[ys.length - 1]) + dy / 2 : (rec.ny || 1) - 0.5;
  return {w: [a - hw, b + hw], y: [y0, y1]};
}

/**
 * Source and destination rectangles for drawing a PNG whose columns span extent.w linearly over
 * the visible x domain [d0, d1]: {sx, sw, w0, w1} (source x / width in image pixels, and the
 * visible wavelength span). Null when the image is outside the domain.
 */
export function pngSourceRect(extent, naturalW, d0, d1) {
  const [w0, w1] = extent.w;
  const lo = Math.max(Math.min(d0, d1), w0), hi = Math.min(Math.max(d0, d1), w1);
  if (!(hi > lo) || !(naturalW > 0)) return null;
  const sx = ((lo - w0) / (w1 - w0)) * naturalW;
  const ex = ((hi - w0) / (w1 - w0)) * naturalW;
  return {sx, sw: Math.max(1e-6, ex - sx), w0: lo, w1: hi};
}

function fmtNum(v, digits = 4) {
  if (!Number.isFinite(v)) return '—';
  const a = Math.abs(v);
  if (a !== 0 && (a < 1e-3 || a >= 1e5)) return v.toExponential(digits - 1);
  return Number(v.toPrecision(digits)).toString();
}

function el(tag, cls, attrs) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (attrs) for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
}

// ------------------------------------------------------------------ pane

function recordsDataset(ctx) {
  const cfg = ctx.config || {};
  const ds = pageDatasets(ctx);
  if (cfg.records && ds[cfg.records]) return cfg.records;
  if (ctx.dataset && ds[ctx.dataset] && ds[ctx.dataset].kind === 'records') return ctx.dataset;
  for (const [id, r] of Object.entries(ds)) if (r && r.kind === 'records' && r.record === 'pki1') return id;
  return null;
}

export function mount(host, ctx) {
  const cfg = ctx.config || {};
  const store = ctx.store;
  const recDs = recordsDataset(ctx);
  let linked = cfg.link1d !== false;
  let cmap = typeof cfg.cmap === 'string' && cfg.cmap ? cfg.cmap : 'gray';
  let stretch = 'stored';
  let rs = null;
  let focusId = null, rec = null, message = 'Select a source';
  let codes = null, values = null, stretchInfo = null;
  let pngImg = null, pngNatural = null;
  let localXr = null;
  let view = null, visible = true, destroyed = false;
  let lastW = 0, lastH = 0, dpr = 1, renderMs = 0, lastStats = {};
  let seq = 0;
  let gesture = null;
  let hover = null;
  let scratch = null, imgBuf = null;

  const root = el('div', 'pk-spec2d');
  const bar = el('div', 'pk-spec-bar');
  const cmLab = el('label', 'pk-spec2d-cmap');
  cmLab.innerHTML = '<span>Colours</span>';
  const cmSel = el('select', null, {'aria-label': 'Colormap'});
  const names = CMAP_NAMES.slice();
  if (!names.includes(cmap.replace(/_r$/, ''))) names.unshift(cmap);
  for (const n of names) {
    for (const v of [n, `${n}_r`]) {
      const o = el('option');
      o.value = v;
      o.textContent = v;
      cmSel.append(o);
    }
  }
  cmSel.value = cmap;
  cmSel.addEventListener('change', () => { cmap = cmSel.value; scheduleRender(); });
  cmLab.append(cmSel);
  const stLab = el('label', 'pk-spec2d-stretch');
  stLab.innerHTML = '<span>Stretch</span>';
  const stSel = el('select', null, {'aria-label': 'Stretch'});
  for (const s of STRETCHES) {
    const o = el('option');
    o.value = s;
    o.textContent = s === 'stored' ? 'as stored' : s;
    stSel.append(o);
  }
  stSel.addEventListener('change', () => { stretch = stSel.value; restretch(); scheduleRender(); });
  stLab.append(stSel);
  const linkBtn = el('button', 'pk-btn pk-spec-toggle', {type: 'button', title: 'Link the wavelength axis to the 1D spectrum'});
  linkBtn.textContent = 'Link λ';
  const syncLink = () => linkBtn.setAttribute('aria-pressed', linked ? 'true' : 'false');
  linkBtn.addEventListener('click', () => { linked = !linked; if (!linked) localXr = currentXr(); syncLink(); scheduleRender(); });
  syncLink();
  bar.append(cmLab, stLab, linkBtn);
  const wrap = el('div', 'pk-spec-wrap');
  const canvas = el('canvas', 'pk-spec-canvas', {tabindex: '0', role: 'img', 'aria-label': '2D spectrum. Drag to pan, wheel to zoom in wavelength, double-click to reset.'});
  const readout = el('div', 'pk-spec-readout', {'aria-live': 'polite'});
  readout.hidden = true;
  wrap.append(canvas, readout);
  root.append(bar, wrap);
  host.append(root);

  const rsP = (async () => {
    if (!recDs) throw new Error('no 2D spectrum record set for this pane');
    rs = await ctx.hub.records(recDs);
    return rs;
  })();
  rsP.catch((err) => {
    message = '2D spectra unavailable';
    ctx.setStatus('error', String(err && err.message ? err.message : err));
    scheduleRender();
  });

  function restretch() {
    if (!rec || rec.kind === 'png') { codes = null; stretchInfo = null; return; }
    if (stretch !== 'stored' && !values) values = pki1Values(rec);
    stretchInfo = stretchCodes(rec, stretch, {values});
    codes = stretchInfo.codes;
  }

  async function loadFocus() {
    const my = ++seq;
    const f = store.get('focus') || null;
    if (!f) { rec = null; focusId = null; message = 'Select a source'; ctx.setStatus(''); scheduleRender(); return; }
    let id = f.id;
    if ((id === null || id === undefined) && Number.isInteger(f.i) && ctx.app && typeof ctx.app.idOf === 'function') id = await ctx.app.idOf(f.ds, f.i);
    if (my !== seq || id === null || id === undefined) return;
    id = String(id);
    const s = await rsP.then((x) => x, () => null);
    if (!s || my !== seq) return;
    if (!s.has(id)) {
      rec = null; focusId = id; codes = null; stretchInfo = null; pngImg = null; pngNatural = null; message = `No 2D spectrum for ${id}`;
      ctx.setStatus('none');
      scheduleRender();
      return;
    }
    ctx.setStatus('loading…');
    let r;
    try {
      r = await s.get(id, {token: my, channel: ctx.id});
    } catch (err) {
      if (my !== seq || destroyed) return; // a superseded focus never reports
      if (err && err.name === 'AbortError') throw err;
      console.warn(`2D spectrum ${id} unavailable`, err);
      rec = null; focusId = id; values = null; codes = null; stretchInfo = null; pngImg = null; pngNatural = null;
      message = `2D spectrum for ${id} unavailable`;
      ctx.setStatus('error', String(err && err.message ? err.message : err));
      scheduleRender();
      return;
    }
    if (r === null || my !== seq || destroyed) return;
    focusId = id;
    rec = r;
    values = null;
    pngImg = null;
    pngNatural = null;
    if (r.kind === 'png') {
      message = null;
      const img = new Image();
      img.decoding = 'async';
      img.onload = () => {
        if (rec !== r) return;
        pngImg = img;
        pngNatural = {w: img.naturalWidth, h: img.naturalHeight};
        ctx.setStatus('png', `${id} · ${img.naturalWidth}×${img.naturalHeight}`);
        scheduleRender();
      };
      img.onerror = () => {
        if (rec !== r) return;
        message = `Could not load ${r.png}`;
        ctx.setStatus('error', message);
        scheduleRender();
      };
      img.src = r.png;
    } else if (r.nx && r.ny && r.data) {
      message = null;
      restretch();
      ctx.setStatus(`${r.nx}×${r.ny}`, `${id} · ${r.enc} · stretch ${r.stretch ? r.stretch.kind : '?'}`);
    } else {
      rec = null;
      message = `Record ${id} is not a 2D spectrum`;
      ctx.setStatus('');
    }
    scheduleRender();
  }

  function currentXr() {
    const ext = imageExtent(rec, pngNatural);
    if (linked) {
      const sp = store.get('spec') || {};
      const xr = validRange(sp.xr);
      if (xr) return xr;
      const full = validRange(linkedFullExtent(store));
      if (full) return full;
    } else if (localXr) {
      return localXr;
    }
    return ext ? ext.w : [1, 5];
  }

  function setXr(xr) {
    const r = [sig7(Math.min(xr[0], xr[1])), sig7(Math.max(xr[0], xr[1]))];
    if (linked) {
      const sp = {...(store.get('spec') || {pinned: [], variant: 'flux'})};
      sp.xr = r;
      if (!Array.isArray(sp.pinned)) sp.pinned = [];
      store.set({spec: sp}, {source: ctx.id});
    } else {
      localXr = r;
    }
    scheduleRender();
  }

  function resetXr() {
    if (linked) {
      const sp = {...(store.get('spec') || {pinned: [], variant: 'flux'})};
      delete sp.xr;
      delete sp.yr;
      if (!Array.isArray(sp.pinned)) sp.pinned = [];
      store.set({spec: sp}, {source: ctx.id});
    } else {
      localXr = null;
    }
    scheduleRender();
  }

  let rafPending = false;
  function scheduleRender() {
    if (rafPending || destroyed) return;
    rafPending = true;
    raf(() => { rafPending = false; render(); });
  }

  function render() {
    if (!visible || destroyed) return;
    const w = wrap.clientWidth || lastW, h = wrap.clientHeight || lastH;
    if (!(w > 20 && h > 20)) return;
    const t0 = performance.now();
    const theme = ctx.theme() || {};
    const tk = tokensOf(theme);
    const c = prepareCanvas(canvas, w, h, dpr);
    const ext = imageExtent(rec, pngNatural);
    const xr = currentXr();
    const yr = ext ? ext.y : [0, 1];
    view = makeView({
      width: w, height: h, dpr,
      margin: {l: 52, r: 10, t: 6, b: 34},
      x: {kind: 'lin', d0: xr[0], d1: xr[1]},
      y: {kind: 'lin', d0: yr[0], d1: yr[1]},
    });
    const v = view;
    const P = v.plot;
    const yUnit = rec && rec.kind !== 'png' ? rec.unitY : '';
    drawAxes(c, v, {tokens: tk, xLabel: 'λ (µm)', yLabel: yUnit ? `y (${yUnit})` : 'y', grid: false});
    let hit = 0;
    if (rec && rec.kind !== 'png' && codes) {
      const r = Math.max(1, Math.min(3, dpr));
      const W = Math.max(1, Math.round(P.width * r)), H = Math.max(1, Math.round(P.height * r));
      if (!scratch) scratch = document.createElement('canvas');
      if (scratch.width !== W) scratch.width = W;
      if (scratch.height !== H) scratch.height = H;
      const sctx = scratch.getContext('2d');
      // resample writes every pixel (alpha 0 outside the image), so the buffer is reused
      if (!imgBuf || imgBuf.width !== W || imgBuf.height !== H) imgBuf = sctx.createImageData(W, H);
      const imgData = imgBuf;
      hit = resample(rec, codes, cmapTable(cmap), v, imgData, r);
      sctx.putImageData(imgData, 0, 0);
      c.save();
      c.imageSmoothingEnabled = false;
      c.drawImage(scratch, 0, 0, W, H, P.left, P.top, P.width, P.height);
      c.restore();
      // segment boundaries
      if (Array.isArray(rec.segments) && rec.segments.length > 1) {
        c.save();
        c.strokeStyle = tk.sel;
        c.globalAlpha = 0.6;
        c.setLineDash([4, 3]);
        for (const s of rec.segments.slice(1)) {
          const i0 = s[0] | 0;
          if (i0 <= 0 || i0 >= rec.wave.length) continue;
          const px = v.sx.f((rec.wave[i0 - 1] + rec.wave[i0]) / 2);
          if (px < P.left || px > P.right) continue;
          c.beginPath();
          c.moveTo(Math.round(px) + 0.5, P.top);
          c.lineTo(Math.round(px) + 0.5, P.bottom);
          c.stroke();
        }
        c.restore();
      }
    } else if (rec && rec.kind === 'png' && pngImg && ext) {
      const sr = pngSourceRect(ext, pngNatural.w, xr[0], xr[1]);
      if (sr) {
        const dx0 = v.sx.f(sr.w0), dx1 = v.sx.f(sr.w1);
        c.save();
        c.beginPath();
        c.rect(P.left, P.top, P.width, P.height);
        c.clip();
        c.drawImage(pngImg, sr.sx, 0, sr.sw, pngNatural.h, Math.min(dx0, dx1), P.top, Math.abs(dx1 - dx0), P.height);
        c.restore();
        hit = Math.round(Math.abs(dx1 - dx0));
      }
    }
    // frame on top of the image
    c.save();
    c.strokeStyle = tk.plotAxis;
    c.globalAlpha = 0.6;
    c.strokeRect(Math.round(P.left) + 0.5, Math.round(P.top) + 0.5, Math.round(P.width) - 1, Math.round(P.height) - 1);
    c.restore();
    if (gesture && gesture.kind === 'band') {
      c.save();
      c.fillStyle = tk.sel;
      c.globalAlpha = 0.15;
      const x0 = Math.min(gesture.x0, gesture.x1), x1 = Math.max(gesture.x0, gesture.x1);
      c.fillRect(x0, P.top, x1 - x0, P.height);
      c.restore();
    }
    if (!rec || message) {
      c.save();
      c.fillStyle = tk.fg3;
      c.font = `12px ${tk.font}`;
      c.textAlign = 'center';
      c.textBaseline = 'middle';
      c.fillText(message || 'Loading…', (P.left + P.right) / 2, (P.top + P.bottom) / 2);
      c.restore();
    }
    updateReadout();
    renderMs = performance.now() - t0;
    lastStats = {
      xr: [v.sx.d0, v.sx.d1], yr: [v.sy.d0, v.sy.d1], hitCols: hit,
      mode: rec ? (rec.kind === 'png' ? 'png' : 'pki1') : null,
      nx: rec && rec.nx ? rec.nx : pngNatural ? pngNatural.w : 0,
      ny: rec && rec.ny ? rec.ny : pngNatural ? pngNatural.h : 0,
      stretchLo: stretchInfo ? stretchInfo.lo : null, stretchHi: stretchInfo ? stretchInfo.hi : null,
    };
  }

  function updateReadout() {
    if (!hover || !view || !rec || !view.inPlot(hover.px, hover.py)) { readout.hidden = true; return; }
    const [wx, yv] = view.toData(hover.px, hover.py);
    const parts = [`λ ${fmtNum(wx, 6)} µm`, `y ${fmtNum(yv, 3)}`];
    if (rec.kind !== 'png' && rec.nx) {
      const col = nearestIndex(rec.wave, wx);
      const y0 = rec.y[0], dy = rec.y.length > 1 ? rec.y[1] - rec.y[0] : 1;
      const row = Math.round((yv - y0) / dy);
      if (col >= 0 && row >= 0 && row < rec.ny) {
        if (!values) values = pki1Values(rec);
        parts.push(`value ${fmtNum(values[row * rec.nx + col], 3)}`);
      }
    }
    readout.textContent = parts.join('  ·  ');
    readout.hidden = false;
  }

  // ---- interaction (x only)
  const pos = (e) => (view ? eventToViewPx(canvas, view, e) : [0, 0]);
  canvas.addEventListener('pointerdown', (e) => {
    if (!view || (e.pointerType === 'mouse' && e.button !== 0)) return;
    const [px, py] = pos(e);
    const P = view.plot;
    if (py > P.bottom && px >= P.left && px <= P.right) gesture = {kind: 'band', x0: px, x1: px, id: e.pointerId};
    else if (view.inPlot(px, py)) gesture = {kind: 'pan', px0: px, sx: view.sx, id: e.pointerId, moved: false};
    else return;
    if (canvas.setPointerCapture) {
      try {
        canvas.setPointerCapture(e.pointerId);
      } catch (err) {
        if (!(err && err.name === 'NotFoundError')) throw err;
      }
    }
    e.preventDefault();
  });
  canvas.addEventListener('pointermove', (e) => {
    if (!view) return;
    const [px, py] = pos(e);
    hover = {px, py};
    if (!gesture || gesture.id !== e.pointerId) { updateReadout(); return; }
    if (gesture.kind === 'pan') {
      const dx = px - gesture.px0;
      if (!gesture.moved && Math.abs(dx) < 3) return;
      gesture.moved = true;
      setXr(panDomain(gesture.sx, dx));
    } else {
      gesture.x1 = Math.max(view.plot.left, Math.min(view.plot.right, px));
      scheduleRender();
    }
  });
  const end = (e) => {
    const g = gesture;
    if (!g || g.id !== e.pointerId) return;
    gesture = null;
    if (g.kind === 'band' && Math.abs(g.x1 - g.x0) > 3 && view) {
      setXr([view.sx.inv(g.x0), view.sx.inv(g.x1)]);
    } else {
      scheduleRender();
    }
  };
  canvas.addEventListener('pointerup', end);
  canvas.addEventListener('pointercancel', end);
  canvas.addEventListener('pointerleave', () => { hover = null; readout.hidden = true; });
  canvas.addEventListener('wheel', (e) => {
    if (!view) return;
    e.preventDefault();
    const [px] = pos(e);
    const unit = e.deltaMode === 1 ? 3 : e.deltaMode === 2 ? 1 / 3 : 100;
    const notches = Math.max(-4, Math.min(4, -e.deltaY / unit));
    if (!notches) return;
    setXr(zoomDomain(view.sx, Math.max(view.plot.left, Math.min(view.plot.right, px)), Math.pow(1.15, notches)));
  }, {passive: false});
  canvas.addEventListener('dblclick', (e) => { e.preventDefault(); resetXr(); });
  canvas.addEventListener('keydown', (e) => {
    if (!view || e.altKey || e.ctrlKey || e.metaKey) return;
    const cx = (view.plot.left + view.plot.right) / 2;
    let handled = true;
    if (e.key === '+' || e.key === '=') setXr(zoomDomain(view.sx, cx, 1.5));
    else if (e.key === '-' || e.key === '_') setXr(zoomDomain(view.sx, cx, 1 / 1.5));
    else if (e.key === '0') resetXr();
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') setXr(panDomain(view.sx, (e.key === 'ArrowLeft' ? 1 : -1) * view.plot.width * 0.1));
    else handled = false;
    if (handled) { e.preventDefault(); e.stopPropagation(); }
  });

  const offs = [];
  offs.push(store.on(['focus'], () => { loadFocus().catch(reportErr); }));
  offs.push(store.on(['spec'], (changed, s, sources) => {
    if (sources.size === 1 && sources.has(ctx.id)) return;
    if (linked) scheduleRender();
  }));
  offs.push(store.onEvent('spec:full', () => { if (linked) scheduleRender(); }));
  offs.push(store.on(['theme'], () => scheduleRender()));
  offs.push(store.onEvent('theme:change', () => scheduleRender()));
  function reportErr(err) {
    if (err && err.name === 'AbortError') return;
    console.error(err);
    ctx.setStatus('error', String(err && err.message ? err.message : err));
  }
  loadFocus().catch(reportErr);

  return {
    resize(w, h, d) {
      lastW = w;
      lastH = h;
      dpr = d || 1;
      scheduleRender();
    },
    onVisible(v) {
      visible = !!v;
      if (visible) scheduleRender();
    },
    destroy() {
      destroyed = true;
      for (const off of offs) off();
      root.remove();
    },
    snapshot() {
      // a PNG from another origin taints the canvas; the snapshot is then unavailable (null)
      return new Promise((resolve) => {
        try {
          canvas.toBlob(resolve, 'image/png');
        } catch (err) {
          if (err && err.name === 'SecurityError') resolve(null);
          else throw err;
        }
      });
    },
    stats() {
      return {renderMs, ds: recDs, id: focusId, cmap, stretch, linked, ...lastStats};
    },
  };
}
