// WebGL2 renderer for projected (x, y) points: tier T1 markers and tier T2 density glow
// (ARCHITECTURE.md 6.9, 7.1, 7.5).
//
//   const r = new XYRenderer(canvas, {onLost, onRestored, budget, owner});
//   if (!r.ok) -> no WebGL2 (the pane falls back to tier F / T0)
//   r.setXY(x, y, vis, {count, origin}); r.setCats([...]); r.setColorValues(v); r.setSelection(m);
//   r.resize(cssW, cssH, dpr); r.render(params); r.destroy();
//
// Density mode reuses the vendored GC passes unchanged (ACCUM_FS_CONT / ACCUM_FS_CAT,
// COMPOSITE_FS, DOWN_FS, BLUR_FS, FINAL_FS, FULLSCREEN_VS) with XY_VS in place of the uint16
// dim decoder; the accumulation format fallback (RGBA32F -> RGBA16F -> RGBA8), exposure and
// bloom follow vendor/gc/gl/renderer.js. Markers mode draws MARKER_VS/FS sprites with the
// selection on top.
//
// Coordinates: x / y are already projected (folded) values; on log axes the pane passes
// log10 values and a camera in the same transformed units (plot/axes.js cameraFromView).
// opts.origin [ox, oy] is subtracted on upload to keep float32 precision for narrow ranges
// around large values. render() takes the camera in TRUE (unshifted) coordinates, exactly as
// cameraFromView(view) returns it, and applies the origin shift itself.
//
// GPU memory: vertex bytes n·18 (+ density targets W·H·48) are requested from the shared
// Budget. When refused, the uploaded row count is capped to the largest LOD prefix that fits
// next to the render targets, and stats() reports {count, n, capped: true} for the
// "showing X of N" note. When the density targets alone do not fit, render() returns false
// and stats().refused is 'targets': the pane drops to tier F.

import {
  ACCUM_FS_CONT, ACCUM_FS_CAT, FULLSCREEN_VS, COMPOSITE_FS, DOWN_FS, BLUR_FS, FINAL_FS,
} from '../vendor/gc/gl/shaders.js';
import {spriteSizeDev, spritePeak, GAMMA} from '../vendor/gc/gl/renderer.js';
import {makeHist, refFromHist} from '../vendor/gc/workers/kernel.js';
import {MRT_BYTES_PER_PX} from '../core/constants.js';
import {XY_VS, MARKER_VS, MARKER_FS, NO_VALUE} from './xyshaders.js';
import {XY_VERTEX_BYTES, capCount} from './budget.js';
import {cmapLUT} from '../plot/canvas2d.js';

const FLOOR = 0.2;
const UNSEL_DIM = 0.25;
const SEL_BOOST = 2.5;
const MEM_BUDGET = 192 * 1024 * 1024;
const NEUTRAL_RGB = [0.29, 0.33, 0.39];
const SELECTION_RGB = [0.85, 0.51, 0.17];
const EXPOSURE_SAMPLE = 65536;
const LOC = {x: 0, y: 1, vis: 2, cats: 3, sel: 4, val: 5};

let OWNER_SEQ = 0;

function compile(gl, type, src) {
  const sh = gl.createShader(type);
  gl.shaderSource(sh, src);
  gl.compileShader(sh);
  if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS) && !gl.isContextLost()) {
    const log = gl.getShaderInfoLog(sh);
    gl.deleteShader(sh);
    throw new Error(`shader compile failed: ${log}`);
  }
  return sh;
}

function makeProgram(gl, vsSrc, fsSrc) {
  const vs = compile(gl, gl.VERTEX_SHADER, vsSrc);
  const fs = compile(gl, gl.FRAGMENT_SHADER, fsSrc);
  const prog = gl.createProgram();
  gl.attachShader(prog, vs);
  gl.attachShader(prog, fs);
  gl.linkProgram(prog);
  gl.deleteShader(vs);
  gl.deleteShader(fs);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS) && !gl.isContextLost()) {
    throw new Error(`program link failed: ${gl.getProgramInfoLog(prog)}`);
  }
  const u = {};
  const nu = gl.getProgramParameter(prog, gl.ACTIVE_UNIFORMS) || 0;
  for (let i = 0; i < nu; i++) {
    const info = gl.getActiveUniform(prog, i);
    const name = info.name.replace(/\[0\]$/, '');
    u[name] = gl.getUniformLocation(prog, name);
  }
  return {prog, u};
}

/** [r, g, b] in 0..1 from an array (0..1 or 0..255) or a CSS hex string. */
export function toRGB01(c, fallback) {
  if (Array.isArray(c) || ArrayBuffer.isView(c)) {
    const m = Math.max(c[0], c[1], c[2]);
    const k = m > 1 ? 1 / 255 : 1;
    const out = [Number(c[0]) * k, Number(c[1]) * k, Number(c[2]) * k];
    return out.every(Number.isFinite) ? out : fallback;
  }
  if (typeof c === 'string') {
    const s = c.trim().replace('#', '');
    if (/^[0-9a-f]{3}$/i.test(s) || /^[0-9a-f]{6}$/i.test(s)) {
      const h = s.length === 3 ? s.split('').map((x) => x + x).join('') : s;
      const v = parseInt(h, 16);
      return [((v >> 16) & 255) / 255, ((v >> 8) & 255) / 255, (v & 255) / 255];
    }
    const m = /^rgba?\(([^)]+)\)$/.exec(c.trim());
    if (m) {
      const p = m[1].split(/[ ,/]+/).map(Number);
      if (p.length >= 3 && p.slice(0, 3).every(Number.isFinite)) return [p[0] / 255, p[1] / 255, p[2] / 255];
    }
  }
  return fallback;
}

export class XYRenderer {
  /** opts: {onLost, onRestored, budget: Budget, owner?: string, contextAttributes?} */
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.onLost = opts.onLost || null;
    this.onRestored = opts.onRestored || null;
    this.budget = opts.budget || null;
    this.owner = opts.owner || `xy${++OWNER_SEQ}`;
    this.gl = null;
    this.lost = false;
    this.ok = false;
    this.n = 0;           // rows given to setXY
    this.count = 0;       // rows uploaded (LOD prefix after the budget cap)
    this.lodCount = 0;    // opts.count of setXY
    this.capped = false;
    this.cssWidth = 1;
    this.cssHeight = 1;
    this.dpr = 1;
    this._cpu = {x: null, y: null, vis: null, cats: null, val: null, sel: null, origin: [0, 0]};
    this._selCount = 0;
    this._targets = {};
    this._targetBytes = 0;
    this._cat = new Float32Array(24);
    this._masks = new Uint32Array(4);
    this._refCache = null;
    this._last = null;
    this._refused = null;
    this._onLost = (e) => {
      e.preventDefault();
      this.lost = true;
      this._targets = {};
      if (this.onLost) this.onLost();
    };
    this._onRestored = () => {
      this.lost = false;
      try {
        this._setup();
        this._reupload();
      } catch (err) {
        if (!(err instanceof Error)) throw err;
        // shader / framebuffer setup failed on the restored context: stay unusable, report it
        console.error('XYRenderer: WebGL restore failed', err);
        this.ok = false;
        return;
      }
      this.ok = true;
      if (this.onRestored) this.onRestored();
    };
    this.init(opts.contextAttributes);
  }

  static supported() {
    if (typeof document === 'undefined') return false;
    try {
      const c = document.createElement('canvas');
      return !!c.getContext('webgl2');
    } catch (err) {
      if (err instanceof Error) return false;   // getContext can throw in locked-down browsers
      throw err;
    }
  }

  /** Create the context and GL resources. Returns false when WebGL2 is unavailable. */
  init(attrs = {}) {
    if (this.gl) return true;
    if (!this.canvas || typeof this.canvas.getContext !== 'function') return false;
    const gl = this.canvas.getContext('webgl2', {
      alpha: true, premultipliedAlpha: true, antialias: false, depth: false, stencil: false,
      preserveDrawingBuffer: false, powerPreference: 'high-performance', ...(attrs || {}),
    });
    if (!gl) return false;
    this.gl = gl;
    this.canvas.addEventListener('webglcontextlost', this._onLost, false);
    this.canvas.addEventListener('webglcontextrestored', this._onRestored, false);
    if (gl.isContextLost()) {
      // the canvas handed back a lost context (e.g. lost by the browser before this renderer
      // was built): unusable until webglcontextrestored, when _onRestored sets it up
      this.lost = true;
      this.ok = false;
      return false;
    }
    this._setup();
    this.ok = true;
    return true;
  }

  _setup() {
    const gl = this.gl;
    const cbf = gl.getExtension('EXT_color_buffer_float');
    const fblend = gl.getExtension('EXT_float_blend');
    const half = cbf ? null : gl.getExtension('EXT_color_buffer_half_float');
    const psr = gl.getParameter(gl.ALIASED_POINT_SIZE_RANGE);
    this.caps = {
      float32: !!(cbf && fblend),
      float16: !!(cbf || half),
      maxPointSize: (psr && psr[1]) || 64,
      maxDrawBuffers: gl.getParameter(gl.MAX_DRAW_BUFFERS),
    };
    this.pCont = makeProgram(gl, XY_VS, ACCUM_FS_CONT);
    this.pCat = makeProgram(gl, XY_VS, ACCUM_FS_CAT);
    this.pMark = makeProgram(gl, MARKER_VS, MARKER_FS);
    this.pComp = makeProgram(gl, FULLSCREEN_VS, COMPOSITE_FS);
    this.pDown = makeProgram(gl, FULLSCREEN_VS, DOWN_FS);
    this.pBlur = makeProgram(gl, FULLSCREEN_VS, BLUR_FS);
    this.pFinal = makeProgram(gl, FULLSCREEN_VS, FINAL_FS);
    const lut = cmapLUT();
    this._lut = lut;
    this.lut = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, this.lut);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, lut.width, lut.rows, 0, gl.RGBA, gl.UNSIGNED_BYTE, lut.data);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.bindTexture(gl.TEXTURE_2D, null);
    this.emptyVao = gl.createVertexArray();
    this.vao = gl.createVertexArray();
    this.buf = {
      x: gl.createBuffer(), y: gl.createBuffer(), vis: gl.createBuffer(),
      cats: gl.createBuffer(), sel: gl.createBuffer(), val: gl.createBuffer(),
    };
    gl.bindVertexArray(this.vao);
    // constant defaults until data arrives
    gl.disableVertexAttribArray(LOC.cats);
    gl.vertexAttribI4ui(LOC.cats, 0, 0, 0, 0);
    gl.disableVertexAttribArray(LOC.sel);
    gl.vertexAttrib1f(LOC.sel, 0);
    gl.disableVertexAttribArray(LOC.val);
    gl.vertexAttrib1f(LOC.val, NO_VALUE);
    gl.bindVertexArray(null);
    this._targets = {};
    this._fbOk = new Map();
    for (const mode of ['cont', 'cat']) {
      try {
        this._deleteTarget(this._createAccum(4, 4, mode));
      } catch (err) {
        if (!(err instanceof Error)) throw err;
        console.warn(`XYRenderer: no accumulation format for ${mode} mode`, err);
      }
    }
  }

  get info() {
    return {
      ...(this.caps || {}),
      tier: (this._targets.cont && this._targets.cont.tier) || (this._targets.cat && this._targets.cat.tier) || null,
      lost: this.lost,
      count: this.count,
      n: this.n,
    };
  }

  /** Draw statistics for the pane's "showing X of N" note and test hooks. */
  stats() {
    return {
      n: this.n, count: this.count, lod: this.lodCount, capped: this.capped,
      refused: this._refused, selected: this._selCount, bytes: this.budget ? this.budget.held(this.owner) : this.count * XY_VERTEX_BYTES + this._targetBytes,
      last: this._last,
    };
  }

  // ---------------------------------------------------------------- data

  _attrFloat(name, loc, data) {
    const gl = this.gl;
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buf[name]);
    gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 1, gl.FLOAT, false, 4, 0);
  }

  _budgetRows(n) {
    if (!this.budget) return n;
    const want = n * XY_VERTEX_BYTES + this._targetBytes;
    if (this.budget.request(this.owner, want)) return n;
    const avail = this.budget.available + this.budget.held(this.owner) - this._targetBytes;
    const rows = capCount(n, Math.max(0, Math.floor(avail / XY_VERTEX_BYTES)));
    this.budget.request(this.owner, rows * XY_VERTEX_BYTES + this._targetBytes);
    return rows;
  }

  /**
   * Upload projected coordinates. x, y: ArrayLike numbers (NaN = not drawable); vis:
   * Uint8Array 0..255 (kernel.js convention) or a 0/1 mask, null = all visible.
   * opts: {count: LOD prefix to draw by default, origin: [ox, oy]}.
   */
  setXY(x, y, vis, opts = {}) {
    const n = Math.min(x ? x.length : 0, y ? y.length : 0);
    const origin = Array.isArray(opts.origin) ? [Number(opts.origin[0]) || 0, Number(opts.origin[1]) || 0] : [0, 0];
    let xs = x, ys = y;
    const needCopy = !(x instanceof Float32Array) || !(y instanceof Float32Array) || origin[0] !== 0 || origin[1] !== 0 || x.length !== n || y.length !== n;
    if (needCopy) {
      xs = new Float32Array(n);
      ys = new Float32Array(n);
      for (let i = 0; i < n; i++) { xs[i] = x[i] - origin[0]; ys[i] = y[i] - origin[1]; }
    }
    let vmax = 0;
    if (vis) for (let i = 0; i < Math.min(n, vis.length); i++) if (vis[i] > vmax) { vmax = vis[i]; if (vmax > 1) break; }
    const scaleVis = vis ? (vmax <= 1 ? 255 : 1) : 0;
    const vb = new Uint8Array(n);
    for (let i = 0; i < n; i++) {
      const v = vis ? (i < vis.length ? vis[i] * scaleVis : 0) : 255;
      vb[i] = Number.isFinite(xs[i]) && Number.isFinite(ys[i]) ? Math.max(0, Math.min(255, Math.round(v))) : 0;
    }
    this._cpu.x = xs;
    this._cpu.y = ys;
    this._cpu.vis = vb;
    this._cpu.origin = origin;
    this.n = n;
    this.lodCount = Number(opts.count) > 0 ? Math.min(n, Math.floor(opts.count)) : n;
    this._refCache = null;
    if (this._cpu.sel && this._cpu.sel.length !== n) { this._cpu.sel = null; this._selCount = 0; }
    if (this._cpu.val && this._cpu.val.length !== n) this._cpu.val = null;
    if (this._cpu.cats && this._cpu.cats.length !== n * 4) this._cpu.cats = null;
    const rows = this._budgetRows(n);
    this.capped = rows < n;
    this.count = rows;
    this._uploadXY();
    this._uploadCats();
    this._uploadVal();
    this._uploadSel();
  }

  _uploadXY() {
    const gl = this.gl;
    if (!gl || this.lost) return;
    const c = this.count;
    gl.bindVertexArray(this.vao);
    this._attrFloat('x', LOC.x, this._cpu.x.subarray(0, c));
    this._attrFloat('y', LOC.y, this._cpu.y.subarray(0, c));
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buf.vis);
    gl.bufferData(gl.ARRAY_BUFFER, this._cpu.vis.subarray(0, c), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(LOC.vis);
    gl.vertexAttribPointer(LOC.vis, 1, gl.UNSIGNED_BYTE, true, 1, 0);
    gl.bindVertexArray(null);
    gl.bindBuffer(gl.ARRAY_BUFFER, null);
  }

  /** Category codes for up to 4 slots (each Uint8Array(n); missing slots = code 0). */
  setCats(cats) {
    const n = this.n;
    const list = Array.isArray(cats) ? cats.slice(0, 4) : [];
    if (!list.some((c) => c && c.length)) {
      this._cpu.cats = null;
    } else {
      const buf = new Uint8Array(n * 4);
      list.forEach((col, s) => {
        if (!col) return;
        const m = Math.min(n, col.length);
        for (let i = 0, o = s; i < m; i++, o += 4) buf[o] = col[i];
      });
      this._cpu.cats = buf;
    }
    this._uploadCats();
  }

  _uploadCats() {
    const gl = this.gl;
    if (!gl || this.lost) return;
    gl.bindVertexArray(this.vao);
    if (this._cpu.cats) {
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buf.cats);
      gl.bufferData(gl.ARRAY_BUFFER, this._cpu.cats.subarray(0, this.count * 4), gl.STATIC_DRAW);
      gl.enableVertexAttribArray(LOC.cats);
      gl.vertexAttribIPointer(LOC.cats, 4, gl.UNSIGNED_BYTE, 4, 0);
    } else {
      gl.disableVertexAttribArray(LOC.cats);
      gl.vertexAttribI4ui(LOC.cats, 0, 0, 0, 0);
    }
    gl.bindVertexArray(null);
    gl.bindBuffer(gl.ARRAY_BUFFER, null);
  }

  /** Continuous colour values (NaN / non-finite = missing) or null. */
  setColorValues(v) {
    if (!v) {
      this._cpu.val = null;
    } else {
      const n = this.n;
      const out = new Float32Array(n);
      for (let i = 0; i < n; i++) {
        const a = i < v.length ? v[i] : NaN;
        out[i] = Number.isFinite(a) ? a : NO_VALUE;
      }
      this._cpu.val = out;
    }
    this._uploadVal();
  }

  _uploadVal() {
    const gl = this.gl;
    if (!gl || this.lost) return;
    gl.bindVertexArray(this.vao);
    if (this._cpu.val) {
      this._attrFloat('val', LOC.val, this._cpu.val.subarray(0, this.count));
    } else {
      gl.disableVertexAttribArray(LOC.val);
      gl.vertexAttrib1f(LOC.val, NO_VALUE);
    }
    gl.bindVertexArray(null);
    gl.bindBuffer(gl.ARRAY_BUFFER, null);
  }

  /** Selection flags (Uint8Array(n), nonzero = selected) or null. */
  setSelection(mask) {
    if (!mask) {
      this._cpu.sel = null;
      this._selCount = 0;
    } else {
      const n = this.n;
      const out = new Uint8Array(n);
      let c = 0;
      for (let i = 0; i < Math.min(n, mask.length); i++) if (mask[i]) { out[i] = 1; c++; }
      this._cpu.sel = out;
      this._selCount = c;
    }
    this._uploadSel();
  }

  _uploadSel() {
    const gl = this.gl;
    if (!gl || this.lost) return;
    gl.bindVertexArray(this.vao);
    if (this._cpu.sel) {
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buf.sel);
      gl.bufferData(gl.ARRAY_BUFFER, this._cpu.sel.subarray(0, this.count), gl.DYNAMIC_DRAW);
      gl.enableVertexAttribArray(LOC.sel);
      gl.vertexAttribPointer(LOC.sel, 1, gl.UNSIGNED_BYTE, false, 1, 0);
    } else {
      gl.disableVertexAttribArray(LOC.sel);
      gl.vertexAttrib1f(LOC.sel, 0);
    }
    gl.bindVertexArray(null);
    gl.bindBuffer(gl.ARRAY_BUFFER, null);
  }

  _reupload() {
    if (!this._cpu.x) return;
    this._uploadXY();
    this._uploadCats();
    this._uploadVal();
    this._uploadSel();
  }

  /** Size the drawing buffer for a CSS size and device-pixel ratio (capped at 2). */
  resize(cssW, cssH, dpr = 1) {
    this.cssWidth = Math.max(1, Number(cssW) || 1);
    this.cssHeight = Math.max(1, Number(cssH) || 1);
    this.dpr = Math.min(2, Math.max(1, Number(dpr) || 1));
    const w = Math.max(1, Math.round(this.cssWidth * this.dpr));
    const h = Math.max(1, Math.round(this.cssHeight * this.dpr));
    if (this.canvas.width !== w || this.canvas.height !== h) {
      this.canvas.width = w;
      this.canvas.height = h;
    }
    if (this.canvas.style) {
      this.canvas.style.width = `${this.cssWidth}px`;
      this.canvas.style.height = `${this.cssHeight}px`;
    }
    this._refCache = null;
  }

  // ---------------------------------------------------------------- render targets (vendor logic)

  _tiers() {
    const gl = this.gl;
    const t = [];
    if (this.caps.float32) t.push({name: 'RGBA32F', rgba: gl.RGBA32F, r: gl.R32F, bytes: 16, scale: 1});
    if (this.caps.float16) t.push({name: 'RGBA16F', rgba: gl.RGBA16F, r: gl.R16F, bytes: 8, scale: 1});
    t.push({name: 'RGBA8', rgba: gl.RGBA8, r: gl.R8, bytes: 4, scale: 1 / 8});
    return t;
  }

  _makeTex(fmt, w, h, filter) {
    const gl = this.gl;
    const tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texStorage2D(gl.TEXTURE_2D, 1, fmt, w, h);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, filter);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, filter);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    return tex;
  }

  _deleteTarget(t) {
    if (!t || !this.gl) return;
    for (const tex of t.texs) this.gl.deleteTexture(tex);
    this.gl.deleteFramebuffer(t.fbo);
  }

  _createAccum(w, h, mode) {
    const gl = this.gl;
    const nAtt = mode === 'cat' ? 3 : 1;
    const tiers = this._tiers();
    for (let ti = 0; ti < tiers.length; ti++) {
      const t = tiers[ti];
      const mem = w * h * t.bytes * (mode === 'cat' ? 2.25 : 1);
      if (mem > MEM_BUDGET && ti < tiers.length - 1) continue;
      for (const singleR of nAtt === 3 ? [true, false] : [false]) {
        const fbo = gl.createFramebuffer();
        gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
        const texs = [];
        for (let i = 0; i < nAtt; i++) {
          const tex = this._makeTex(i === 2 && singleR ? t.r : t.rgba, w, h, gl.NEAREST);
          gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0 + i, gl.TEXTURE_2D, tex, 0);
          texs.push(tex);
        }
        if (nAtt > 1) gl.drawBuffers([gl.COLOR_ATTACHMENT0, gl.COLOR_ATTACHMENT1, gl.COLOR_ATTACHMENT2]);
        const key = `${t.name}:${nAtt}:${singleR}`;
        let ok = this._fbOk.get(key);
        if (ok === undefined) {
          ok = gl.checkFramebufferStatus(gl.FRAMEBUFFER) === gl.FRAMEBUFFER_COMPLETE;
          this._fbOk.set(key, ok);
        }
        gl.bindFramebuffer(gl.FRAMEBUFFER, null);
        if (ok) return {fbo, texs, w, h, mode, tier: t.name, accScale: t.scale, nAtt};
        this._deleteTarget({fbo, texs});
      }
    }
    throw new Error('no renderable accumulation format');
  }

  _createColor(w, h, filter) {
    const gl = this.gl;
    const fbo = gl.createFramebuffer();
    const tex = this._makeTex(gl.RGBA8, w, h, filter);
    gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    return {fbo, texs: [tex], w, h};
  }

  _target(key, w, h, make) {
    const t = this._targets[key];
    if (t && t.w === w && t.h === h) return t;
    this._deleteTarget(t);
    const nt = make();
    this._targets[key] = nt;
    return nt;
  }

  _dropTargets(keys) {
    for (const k of keys) {
      if (this._targets[k]) {
        this._deleteTarget(this._targets[k]);
        delete this._targets[k];
      }
    }
  }

  // ---------------------------------------------------------------- uniforms shared by the point programs

  _pointUniforms(prog, p, cam, sizeDev) {
    const gl = this.gl;
    const u = prog.u;
    const color = p.color || {};
    const cmode = color.mode === 1 && this._cpu.val ? 1 : color.mode === 2 ? 2 : 0;
    let lo = Number(color.lo), hi = Number(color.hi);
    if (!Number.isFinite(lo)) lo = 0;
    if (!Number.isFinite(hi)) hi = 1;
    const cname = String(color.cmap || 'viridis').toLowerCase();
    if (cname.endsWith('_r')) [lo, hi] = [hi, lo];
    gl.uniform2f(u.u_colRange, lo, Math.abs(hi - lo) > 1e-30 ? 1 / (hi - lo) : 1);
    const masks = this._masks;
    masks.fill(0xffffffff);
    const fm = p.filters && p.filters.masks;
    if (fm) for (let s = 0; s < 4; s++) if (fm[s] != null) masks[s] = fm[s] >>> 0;
    gl.uniform4uiv(u.u_catMask, masks);
    gl.uniform1i(u.u_catSlot, cmode === 2 ? (Number.isInteger(color.slot) ? color.slot : 0) : -1);
    gl.uniform1i(u.u_colorMode, cmode);
    const ox = this._cpu.origin[0], oy = this._cpu.origin[1];
    gl.uniform4f(u.u_cam, cam.cx - ox, cam.cy - oy, (2 * cam.scale) / cam.width, (2 * cam.scale * (cam.aspect || 1)) / cam.height);
    gl.uniform1f(u.u_pointSize, sizeDev);
    return cmode;
  }

  _lutV(cmap) {
    const name = String(cmap || 'viridis').toLowerCase().replace(/_r$/, '');
    return (this._lut.row(name) + 0.5) / this._lut.rows;
  }

  _catColors(p) {
    const color = p.color || {};
    const neutral = toRGB01(p.neutral, NEUTRAL_RGB);
    const cat = this._cat;
    for (let c = 0; c < 8; c++) {
      const rgb = toRGB01(color.colors && color.colors[c], neutral);
      cat[3 * c] = rgb[0]; cat[3 * c + 1] = rgb[1]; cat[3 * c + 2] = rgb[2];
    }
    return {cat, neutral, sel: toRGB01(p.selColor, SELECTION_RGB)};
  }

  /** Auto exposure (vendor kernel.js) from a strided CPU subsample; cached per camera. */
  estimateRef(cam, sizeDev, count) {
    const X = this._cpu.x, Y = this._cpu.y, V = this._cpu.vis;
    if (!X || !count) return 1;
    const ox = this._cpu.origin[0], oy = this._cpu.origin[1];
    const key = `${cam.cx},${cam.cy},${cam.scale},${cam.aspect},${cam.width},${cam.height},${sizeDev},${count}`;
    if (this._refCache && this._refCache.key === key) return this._refCache.ref;
    const c2 = {...cam, cx: cam.cx - ox, cy: cam.cy - oy};
    const H = makeHist(c2);
    const step = Math.max(1, Math.floor(count / EXPOSURE_SAMPLE));
    let inView = 0, used = 0;
    const hist = H.hist, gx = H.gx;
    const kx = c2.scale, ky = c2.scale * (c2.aspect || 1);
    for (let i = 0; i < count; i += step) {
      used++;
      const v = V[i] / 255;
      if (!(v > 0)) continue;
      const sx = c2.width / 2 + (X[i] - c2.cx) * kx;
      const sy = c2.height / 2 - (Y[i] - c2.cy) * ky;
      if (!(sx >= 0 && sy >= 0 && sx < c2.width && sy < c2.height)) continue;
      hist[Math.floor(sy / 8) * gx + Math.floor(sx / 8)] += v;
      inView++;
    }
    let ref = refFromHist(H, inView, used, count, sizeDev, this.dpr);
    if (!(ref > 0)) {
      // too few rows for the percentile estimate: one sprite peak = full exposure
      ref = Math.max(1e-6, spritePeak(sizeDev));
    }
    this._refCache = {key, ref};
    return ref;
  }

  // ---------------------------------------------------------------- render

  /**
   * Draw. p: {camera: {cx, cy, scale, aspect, width, height} (CSS px; data units relative to
   * the same coordinates passed to setXY), mode: 'markers'|'density',
   * color: {mode: 0|1|2, lo, hi, cmap, slot, colors}, filters: {masks}, pointSize (CSS px),
   * gain, ref, selection: {show, dim}, neutral, selColor, count, alpha (markers), bloom
   * (density), densityCmap (density, no colour), floor}.
   * Returns false when nothing could be drawn.
   */
  render(p = {}) {
    const gl = this.gl;
    if (!gl || this.lost || gl.isContextLost()) return false;
    const cam = p.camera;
    if (!cam || !(cam.scale > 0) || !(cam.width > 0) || !(cam.height > 0)) return false;
    const W = this.canvas.width, H = this.canvas.height;
    const mode = p.mode === 'density' ? 'density' : 'markers';
    // budget first: the target grant may lower this.count (largest LOD prefix that fits)
    if (mode === 'markers') {
      this._dropTargets(['cont', 'cat', 'base', 'bloomA', 'bloomB']);
      this._setTargetBytes(0);
    } else if (!this._setTargetBytes(W * H * MRT_BYTES_PER_PX)) {
      this._dropTargets(['cont', 'cat', 'base', 'bloomA', 'bloomB']);
      this._last = {mode, count: 0, n: this.n, refused: 'targets'};
      return false;
    }
    let count = Math.min(this.count, this.lodCount || this.count);
    if (Number(p.count) > 0) count = Math.min(this.count, Math.floor(p.count));
    this._last = {mode, count, n: this.n};
    if (mode === 'markers') {
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      gl.viewport(0, 0, W, H);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      if (count > 0) this._drawMarkers(p, cam, count);
      return true;
    }
    const color = p.color || {};
    const accMode = color.mode === 2 ? 'cat' : 'cont';
    this._dropTargets([accMode === 'cat' ? 'cont' : 'cat']);
    const acc = this._target(accMode, W, H, () => this._createAccum(W, H, accMode));
    const sizeDev = spriteSizeDev(p.pointSize, this.dpr, this.caps.maxPointSize);
    // accumulate
    gl.bindFramebuffer(gl.FRAMEBUFFER, acc.fbo);
    gl.viewport(0, 0, acc.w, acc.h);
    const zero = [0, 0, 0, 0];
    for (let i = 0; i < acc.nAtt; i++) gl.clearBufferfv(gl.COLOR, i, zero);
    const prog = accMode === 'cat' ? this.pCat : this.pCont;
    gl.useProgram(prog.prog);
    const cmode = this._pointUniforms(prog, p, cam, sizeDev);
    gl.uniform1f(prog.u.u_accScale, acc.accScale);
    if (count > 0) {
      gl.enable(gl.BLEND);
      gl.blendEquation(gl.FUNC_ADD);
      gl.blendFunc(gl.ONE, gl.ONE);
      gl.bindVertexArray(this.vao);
      gl.drawArrays(gl.POINTS, 0, count);
      gl.bindVertexArray(null);
      gl.disable(gl.BLEND);
    }
    const ref = Number(p.ref) > 0 ? Number(p.ref) : this.estimateRef(cam, sizeDev, count);
    this._last.ref = ref;
    const bloom = p.bloom > 0 ? Math.min(1, p.bloom) : 0;
    if (!bloom) {
      this._dropTargets(['base', 'bloomA', 'bloomB']);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      gl.viewport(0, 0, W, H);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      this._composite(acc, p, cmode, sizeDev, ref);
      return true;
    }
    const base = this._target('base', W, H, () => this._createColor(W, H, gl.LINEAR));
    gl.bindFramebuffer(gl.FRAMEBUFFER, base.fbo);
    gl.viewport(0, 0, W, H);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    this._composite(acc, p, cmode, sizeDev, ref);
    const bw = Math.max(1, Math.round(W / 4)), bh = Math.max(1, Math.round(H / 4));
    const b1 = this._target('bloomA', bw, bh, () => this._createColor(bw, bh, gl.LINEAR));
    const b2 = this._target('bloomB', bw, bh, () => this._createColor(bw, bh, gl.LINEAR));
    gl.bindFramebuffer(gl.FRAMEBUFFER, b1.fbo);
    gl.viewport(0, 0, bw, bh);
    this._fullscreen(this.pDown, {u_src: {tex: base.texs[0]}, u_texel: [1 / W, 1 / H]});
    gl.bindFramebuffer(gl.FRAMEBUFFER, b2.fbo);
    this._fullscreen(this.pBlur, {u_src: {tex: b1.texs[0]}, u_dir: [1.6 / bw, 0]});
    gl.bindFramebuffer(gl.FRAMEBUFFER, b1.fbo);
    this._fullscreen(this.pBlur, {u_src: {tex: b2.texs[0]}, u_dir: [0, 1.6 / bh]});
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, W, H);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    this._fullscreen(this.pFinal, {u_base: {tex: base.texs[0]}, u_bloom: {tex: b1.texs[0]}, u_strength: bloom});
    return true;
  }

  /**
   * Account render-target bytes `b` in the budget next to the vertex rows (section 7.5 step 3).
   * The row count is re-derived for the new target size: the largest LOD prefix whose
   * vertices fit beside the targets (re-uploaded when it changes; it grows back towards n when
   * the targets shrink). Returns false, keeping the previous grant and row count, when the
   * targets alone exceed what this owner can get; the caller then draws nothing (tier F).
   */
  _setTargetBytes(b) {
    if (b === this._targetBytes && this._refused === null) return true;
    if (!this.budget) {
      this._targetBytes = b;
      this._refused = null;
      return true;
    }
    const avail = this.budget.available + this.budget.held(this.owner);
    if (b > avail) {
      this._refused = 'targets';
      return false;
    }
    this._refused = null;
    this._targetBytes = b;
    const prev = this.count;
    const rows = this._budgetRows(this.n);
    this.count = rows;
    this.capped = rows < this.n;
    if (rows !== prev) {
      this._refCache = null;
      this._reupload();
    }
    return true;
  }

  _drawMarkers(p, cam, count) {
    const gl = this.gl;
    const prog = this.pMark;
    const u = prog.u;
    gl.useProgram(prog.prog);
    const sizeDev = Math.min(this.caps.maxPointSize, Math.max(1, (Number(p.pointSize) > 0 ? Number(p.pointSize) : 4) * this.dpr));
    this._pointUniforms(prog, p, cam, sizeDev);
    const {cat, neutral, sel} = this._catColors(p);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.lut);
    gl.uniform1i(u.u_lut, 0);
    gl.uniform1f(u.u_lutV, this._lutV((p.color || {}).cmap));
    gl.uniform3fv(u.u_cat, cat);
    gl.uniform3fv(u.u_neutral, neutral);
    gl.uniform3fv(u.u_selColor, sel);
    const selOpt = p.selection || {};
    const show = selOpt.show !== false && this._selCount > 0 && !!this._cpu.sel;
    gl.uniform1f(u.u_selShow, show ? 1 : 0);
    const dim = Number.isFinite(Number(selOpt.dim)) ? Math.min(1, Math.max(0, Number(selOpt.dim))) : UNSEL_DIM;
    gl.uniform1f(u.u_unselDim, dim);
    gl.uniform1f(u.u_alpha, Number.isFinite(Number(p.alpha)) ? Math.min(1, Math.max(0, Number(p.alpha))) : 0.85);
    gl.uniform1f(u.u_ringPx, 1.25 * this.dpr);
    gl.enable(gl.BLEND);
    gl.blendEquation(gl.FUNC_ADD);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.bindVertexArray(this.vao);
    gl.uniform1i(u.u_pass, 0);
    gl.drawArrays(gl.POINTS, 0, count);
    if (show) {
      gl.uniform1i(u.u_pass, 1);
      gl.drawArrays(gl.POINTS, 0, count);
    }
    gl.bindVertexArray(null);
    gl.disable(gl.BLEND);
  }

  _composite(acc, p, cmode, sizeDev, ref) {
    const gl = this.gl;
    const u = this.pComp.u;
    gl.useProgram(this.pComp.prog);
    for (let i = 0; i < 3; i++) {
      gl.activeTexture(gl.TEXTURE0 + i);
      gl.bindTexture(gl.TEXTURE_2D, acc.texs[Math.min(i, acc.texs.length - 1)]);
    }
    gl.activeTexture(gl.TEXTURE3);
    gl.bindTexture(gl.TEXTURE_2D, this.lut);
    gl.uniform1i(u.u_acc0, 0);
    gl.uniform1i(u.u_acc1, 1);
    gl.uniform1i(u.u_acc2, 2);
    gl.uniform1i(u.u_lut, 3);
    const color = p.color || {};
    gl.uniform1i(u.u_mode, cmode);
    const densityCmap = cmode === 0 && (p.densityCmap || color.densityCmap);
    gl.uniform1f(u.u_lutV, this._lutV(cmode === 0 && densityCmap ? (typeof densityCmap === 'string' ? densityCmap : 'magma') : color.cmap));
    gl.uniform1f(u.u_densityCmap, densityCmap ? 1 : 0);
    const {cat, neutral, sel} = this._catColors(p);
    gl.uniform3fv(u.u_cat, cat);
    gl.uniform3fv(u.u_neutral, neutral);
    gl.uniform3fv(u.u_selColor, sel);
    gl.uniform1f(u.u_gain, p.gain > 0 ? p.gain : 1);
    gl.uniform1f(u.u_invRef, 1 / Math.max(1e-6, ref || 1));
    gl.uniform1f(u.u_gamma, GAMMA);
    gl.uniform1f(u.u_floor, p.floor ?? FLOOR);
    gl.uniform1f(u.u_invPeak, 1 / spritePeak(sizeDev));
    const selOpt = p.selection || {};
    const show = selOpt.show !== false && this._selCount > 0 && !!this._cpu.sel;
    gl.uniform1f(u.u_selActive, show ? 1 : 0);
    gl.uniform1f(u.u_unselDim, Number.isFinite(Number(selOpt.dim)) ? Number(selOpt.dim) : UNSEL_DIM);
    gl.uniform1f(u.u_selBoost, SEL_BOOST);
    gl.uniform1f(u.u_dim, p.dim ?? 1);
    gl.uniform1f(u.u_invAccScale, 1 / acc.accScale);
    gl.bindVertexArray(this.emptyVao);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    gl.bindVertexArray(null);
  }

  _fullscreen(prog, bindings) {
    const gl = this.gl;
    gl.useProgram(prog.prog);
    let unit = 0;
    for (const [name, value] of Object.entries(bindings)) {
      if (value && value.tex) {
        gl.activeTexture(gl.TEXTURE0 + unit);
        gl.bindTexture(gl.TEXTURE_2D, value.tex);
        gl.uniform1i(prog.u[name], unit++);
      } else if (Array.isArray(value)) {
        gl.uniform2f(prog.u[name], value[0], value[1]);
      } else if (typeof value === 'number') {
        gl.uniform1f(prog.u[name], value);
      }
    }
    gl.bindVertexArray(this.emptyVao);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    gl.bindVertexArray(null);
  }

  /** Release GPU memory, the budget grant and listeners. */
  destroy() {
    if (this.budget) this.budget.release(this.owner);
    const gl = this.gl;
    if (this.canvas) {
      this.canvas.removeEventListener('webglcontextlost', this._onLost);
      this.canvas.removeEventListener('webglcontextrestored', this._onRestored);
    }
    if (gl && !gl.isContextLost()) {
      this._dropTargets(Object.keys(this._targets));
      for (const b of Object.values(this.buf || {})) gl.deleteBuffer(b);
      for (const p of [this.pCont, this.pCat, this.pMark, this.pComp, this.pDown, this.pBlur, this.pFinal]) if (p) gl.deleteProgram(p.prog);
      if (this.lut) gl.deleteTexture(this.lut);
      if (this.vao) gl.deleteVertexArray(this.vao);
      if (this.emptyVao) gl.deleteVertexArray(this.emptyVao);
      // no WEBGL_lose_context.loseContext() here: the canvas keeps its context, and a later
      // XYRenderer (pane remount, tier switch) on the same canvas must get a live one back
    }
    this.gl = null;
    this.ok = false;
    this._cpu = {x: null, y: null, vis: null, cats: null, val: null, sel: null, origin: [0, 0]};
  }
}
