// GPU memory budget (ARCHITECTURE.md section 7.5).
//
// One Budget per page (boot or the first plot pane creates it and shares it through the
// pane context). Costs:
//   density render targets  MRT_BYTES_PER_PX (48 B) per device pixel
//   GC Renderer vertices    n·(2·support + 4 + 1) B
//   XYRenderer vertices     n·(4+4+1+4+1+4) = 18 B
// plan() is called on mount and on resize:
//   1. at most DENSITY_PANES_MAX (2) density panes; further density panes get tier F;
//   2. if the total exceeds the budget, lower DPR 2 -> 1.5 -> 1;
//   3. if it still exceeds, cap the vertex bytes (the pane draws the largest LOD prefix that
//      fits and shows "showing X of N").
// Pure arithmetic (no DOM, no GL); usable under node.

import {
  GPU_BUDGET_BYTES, MRT_BYTES_PER_PX, DENSITY_PANES_MAX, LOD_BASE, LOD_MIN_N,
} from '../core/constants.js';

export const XY_VERTEX_BYTES = 4 + 4 + 1 + 4 + 1 + 4;
export const DPR_STEPS = Object.freeze([2, 1.5, 1]);

/** Vertex bytes per row of the vendored GC Renderer for a folded support size. */
export function gcVertexBytes(support) {
  const s = Math.max(0, Math.min(28, Math.floor(Number(support) || 0)));
  return 2 * s + 4 + 1;
}

/** Render-target bytes of one density pane at a DPR. */
export function targetBytes(cssW, cssH, dpr) {
  const w = Math.max(1, Math.round((Number(cssW) || 0) * dpr));
  const h = Math.max(1, Math.round((Number(cssH) || 0) * dpr));
  return w * h * MRT_BYTES_PER_PX;
}

/**
 * Largest LOD prefix count <= maxRows. levels: cumulative manifest levels (optional);
 * without levels, prefixes are LOD_BASE·2^k when n > LOD_MIN_N, else any count.
 */
export function capCount(n, maxRows, levels = null) {
  const N = Math.max(0, Math.floor(Number(n) || 0));
  const cap = Math.max(0, Math.floor(Number(maxRows)));
  if (!Number.isFinite(Number(maxRows)) || cap >= N) return N;
  if (Array.isArray(levels) && levels.length) {
    let best = 0;
    for (const l of levels) if (l <= cap && l > best) best = l;
    return Math.min(best, N);
  }
  if (N <= LOD_MIN_N) return cap;
  let best = 0;
  for (let p = LOD_BASE; p <= cap; p *= 2) best = p;
  return best;
}

export class Budget {
  constructor(totalBytes = GPU_BUDGET_BYTES) {
    const t = Number(totalBytes);
    this.total = Number.isFinite(t) && t > 0 ? t : GPU_BUDGET_BYTES;
    this._owners = new Map();
  }

  get used() {
    let s = 0;
    for (const v of this._owners.values()) s += v;
    return s;
  }

  get available() { return Math.max(0, this.total - this.used); }

  /** Bytes currently held by `owner` (0 if none). */
  held(owner) { return this._owners.get(owner) || 0; }

  /**
   * Ask for `bytes` on behalf of `owner`, replacing what it held. Returns false (and keeps
   * the previous grant) when the new total would exceed the budget.
   */
  request(owner, bytes) {
    const b = Number(bytes);
    if (!(b >= 0) || !Number.isFinite(b)) return false;
    const prev = this._owners.get(owner) || 0;
    if (this.used - prev + b > this.total) return false;
    this._owners.set(owner, b);
    return true;
  }

  release(owner) {
    this._owners.delete(owner);
  }

  /**
   * Plan DPR and density admission for the panes of a page.
   * panes: [{id, cssW, cssH, density}]; vertexBytes: total vertex bytes the page uploads.
   * opts: {dpr: device pixel ratio (default 2, capped at 2), reserved: bytes held elsewhere}.
   * Returns {dpr, allowed, refused, targetBytes, vertexBytes, maxVertexBytes, fits, capFraction}.
   *   allowed  ids that may use WebGL (all non-density panes, the first 2 density panes)
   *   refused  density panes beyond DENSITY_PANES_MAX (tier F)
   *   maxVertexBytes  vertex bytes that fit next to the targets at the chosen DPR
   *   capFraction     maxVertexBytes / vertexBytes, clamped to [0, 1]
   */
  plan(panes, vertexBytes, opts = {}) {
    const list = Array.isArray(panes) ? panes : [];
    const allowed = [];
    const refused = [];
    const dens = [];
    for (const p of list) {
      if (!p || p.id == null) continue;
      if (p.density) {
        if (dens.length < DENSITY_PANES_MAX) { dens.push(p); allowed.push(p.id); }
        else refused.push(p.id);
      } else {
        allowed.push(p.id);
      }
    }
    const vb = Math.max(0, Number(vertexBytes) || 0);
    const reserved = Math.max(0, Number(opts.reserved) || 0);
    const total = this.total - reserved;
    const devDpr = Math.min(2, Math.max(1, Number(opts.dpr) || 2));
    const steps = DPR_STEPS.filter((d) => d <= devDpr);
    if (!steps.length || steps[0] !== devDpr) steps.unshift(devDpr);
    let chosen = steps[steps.length - 1];
    let tb = 0;
    for (const d of steps) {
      tb = dens.reduce((s, p) => s + targetBytes(p.cssW, p.cssH, d), 0);
      chosen = d;
      if (tb + vb <= total) break;
    }
    tb = dens.reduce((s, p) => s + targetBytes(p.cssW, p.cssH, chosen), 0);
    const maxVertexBytes = Math.max(0, total - tb);
    const fits = tb + vb <= total;
    const capFraction = vb > 0 ? Math.min(1, maxVertexBytes / vb) : 1;
    return {dpr: chosen, allowed, refused, targetBytes: tb, vertexBytes: vb, maxVertexBytes, fits, capFraction};
  }
}
