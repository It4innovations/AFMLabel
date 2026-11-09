// SPDX-License-Identifier: MIT
// Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
// Canvas workspace: viewport, source image per channel, region layers,
// selection outlines, overlap/missing markers, brush/eraser/polygon input.
// The canvas holds *display* state only; every mask edit is reported to
// the owner (workspace.js) which persists it and manages undo/redo.
//
// Upgrade notes:
// * Brush rule: the active class is authoritative. The
//   stroke extends the primary selected region only when that region is
//   editable *and* has the active class; otherwise the stroke creates a
//   new region of the active class, also over existing or locked regions.
// * Multi-selection: `selectedIds` (Set) plus `selectedId` (primary /
//   anchor). Every selected region gets an outline; a locked selected
//   region gets a distinct dashed outline.
// * View-only brightness/contrast: applied with a 2D
//   canvas filter around the base image draw only, reset before overlays,
//   kept per channel, never persisted, never sent to the server.
// * N source channels (multi-channel upgrade): `images` is a map keyed by
//   channel key; `channel` is the displayed one. Images arrive lazily.
// * Fill tool: click reports a seed to the owner (`onFill`), hover reports
//   the cursor (`onFillHover`) so the owner can show a preview mask.

import { coverageStats, hexToRgb, maskEdges, paintStroke, rasterizePolygon } from "./masks.js";

const MIN_SCALE = 0.05;
const MAX_SCALE = 64;

export const VIEW_NEUTRAL = Object.freeze({ brightness: 100, contrast: 100 });
export const VIEW_RANGE = Object.freeze({ min: 40, max: 200 });

export class AnnotationCanvas {
  constructor(canvas, { width, height, onEditEnd, onCreate, onSelect, onViewport, onStats, onPrompt, onCursor, onFill, onFillHover }) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.width = width;
    this.height = height;
    this.images = {}; // channel key -> HTMLImageElement
    this.channel = "topography";
    this.regions = new Map(); // id -> { id, classId, visible, locked, mask, layer, dirty }
    this.order = []; // bottom -> top
    this.classes = new Map(); // classId -> { color, name }
    this.selectedId = null;
    this.selectedIds = new Set();
    this.activeClassId = null;
    this.tool = "select";
    this.brushRadius = 10;
    this.opacity = 0.55;
    this.showOverlaps = true;
    this.showMissing = false;
    this.scale = 1;
    this.ox = 0;
    this.oy = 0;
    this.polygon = [];
    this.stroke = null;
    this.panning = null;
    this.spaceHeld = false;
    this.pointer = null; // last image-space cursor
    this.prompts = { points: [], box: null, boxDraft: null };
    this.proposalMask = null;
    this.fillPreview = null;
    this.fillPreviewLayer = null;
    this.stats = null;
    this.highlightIds = new Set();
    this.missingLayer = null;
    this.overlapLayer = null;
    this.markersDirty = true;
    // Temporary per-channel display adjustment; neutral = original rendering.
    this.view = {};
    this.callbacks = { onEditEnd, onCreate, onSelect, onViewport, onStats, onPrompt, onCursor, onFill, onFillHover };
    this.frame = null;
    this.dpr = window.devicePixelRatio || 1;
    this._bind();
    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(canvas.parentElement);
    this.resize();
  }

  // ---------------------------------------------------------- lifecycle
  resize() {
    const rect = this.canvas.parentElement.getBoundingClientRect();
    this.dpr = window.devicePixelRatio || 1;
    this.cssWidth = Math.max(1, Math.floor(rect.width));
    this.cssHeight = Math.max(1, Math.floor(rect.height));
    this.canvas.width = Math.floor(this.cssWidth * this.dpr);
    this.canvas.height = Math.floor(this.cssHeight * this.dpr);
    if (!this.initialised) {
      this.initialised = true;
      this.fit(true);
    }
    this.requestRender();
  }

  setImage(channel, image) {
    this.images[channel] = image;
    this.requestRender();
  }

  setChannel(channel) {
    this.channel = channel;
    this.requestRender();
  }

  // Legacy spelling kept for callers written for the two-channel layout.
  setModality(channel) {
    this.setChannel(channel);
  }

  get modality() {
    return this.channel;
  }

  setFillPreview(mask) {
    this.fillPreview = mask;
    this.fillPreviewLayer = null;
    this.requestRender();
  }

  setClasses(classes) {
    this.classes = new Map(classes.map((c) => [c.class_id, { color: c.display_color, name: c.name, enabled: c.enabled }]));
    for (const region of this.regions.values()) region.dirty = true;
    this.requestRender();
  }

  setRegions(list) {
    // list: [{id, classId, visible, locked, mask}] bottom -> top
    const next = new Map();
    for (const item of list) {
      const existing = this.regions.get(item.id);
      const entry = existing || { id: item.id, layer: null, dirty: true };
      const changed = !existing || existing.mask !== item.mask || existing.classId !== item.classId;
      entry.classId = item.classId;
      entry.visible = item.visible;
      entry.locked = item.locked;
      entry.mask = item.mask;
      if (changed) { entry.dirty = true; entry.edges = null; }
      next.set(item.id, entry);
    }
    this.regions = next;
    this.order = list.map((item) => item.id);
    if (this.selectedId && !this.regions.has(this.selectedId)) this.selectedId = null;
    for (const id of Array.from(this.selectedIds)) if (!this.regions.has(id)) this.selectedIds.delete(id);
    this.markersDirty = true;
    this.requestRender();
  }

  updateRegionMask(id, mask) {
    const region = this.regions.get(id);
    if (!region) return;
    region.mask = mask;
    region.dirty = true;
    region.edges = null;
    this.markersDirty = true;
    this.requestRender();
  }

  // Selection is owned by workspace.js; the canvas mirrors it for drawing.
  setSelection(primaryId, ids) {
    this.selectedId = primaryId;
    this.selectedIds = new Set(ids || (primaryId ? [primaryId] : []));
    this.requestRender();
  }

  select(id, modifiers = {}) {
    if (this.callbacks.onSelect) this.callbacks.onSelect(id, modifiers);
    else this.setSelection(id, id ? [id] : []);
  }

  setTool(tool) {
    if (this.tool === "polygon" && tool !== "polygon") this.cancelPolygon();
    if (tool !== "fill") this.fillPreview = null;
    this.tool = tool;
    this.canvas.className = `tool-${tool}`;
    this.requestRender();
  }

  setHighlight(ids) {
    this.highlightIds = new Set(ids || []);
    this.requestRender();
  }

  setProposal(mask) {
    this.proposalMask = mask;
    this.proposalLayer = null;
    this.requestRender();
  }

  setPrompts(prompts) {
    this.prompts = { points: prompts.points || [], box: prompts.box || null, boxDraft: null };
    this.requestRender();
  }

  // ------------------------------------------------- view-only adjustment
  setView(channel, values) {
    const current = this.view[channel] || { ...VIEW_NEUTRAL };
    const clamp = (v) => Math.max(VIEW_RANGE.min, Math.min(VIEW_RANGE.max, Math.round(Number(v))));
    this.view[channel] = {
      brightness: values.brightness === undefined ? current.brightness : clamp(values.brightness),
      contrast: values.contrast === undefined ? current.contrast : clamp(values.contrast),
    };
    this.requestRender();
    return this.view[channel];
  }

  resetView(channel) {
    this.view[channel] = { ...VIEW_NEUTRAL };
    this.requestRender();
    return this.view[channel];
  }

  currentView() {
    return this.view[this.channel] || { ...VIEW_NEUTRAL };
  }

  _viewFilter() {
    const v = this.currentView();
    if (v.brightness === VIEW_NEUTRAL.brightness && v.contrast === VIEW_NEUTRAL.contrast) return "none";
    return `brightness(${(v.brightness / 100).toFixed(3)}) contrast(${(v.contrast / 100).toFixed(3)})`;
  }

  // ------------------------------------------------------------ viewport
  fit(silent = false) {
    const margin = 16;
    const scale = Math.min((this.cssWidth - margin * 2) / this.width, (this.cssHeight - margin * 2) / this.height);
    this.scale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, scale));
    this.ox = (this.cssWidth - this.width * this.scale) / 2;
    this.oy = (this.cssHeight - this.height * this.scale) / 2;
    this._viewportChanged(silent);
  }

  setViewport(scale, ox, oy) {
    this.scale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, scale));
    this.ox = ox;
    this.oy = oy;
    this._viewportChanged();
  }

  zoomAt(factor, cssX, cssY) {
    const before = this.toImage(cssX, cssY);
    this.scale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, this.scale * factor));
    this.ox = cssX - before.x * this.scale;
    this.oy = cssY - before.y * this.scale;
    this._viewportChanged();
  }

  zoomTo(scale) {
    this.zoomAt(scale / this.scale, this.cssWidth / 2, this.cssHeight / 2);
  }

  centerOn(x, y, w, h) {
    // Zoom so that the box fills ~60% of the view, centred.
    const target = Math.min((this.cssWidth * 0.6) / Math.max(w, 4), (this.cssHeight * 0.6) / Math.max(h, 4));
    this.scale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, target));
    this.ox = this.cssWidth / 2 - (x + w / 2) * this.scale;
    this.oy = this.cssHeight / 2 - (y + h / 2) * this.scale;
    this._viewportChanged();
  }

  toImage(cssX, cssY) {
    return { x: (cssX - this.ox) / this.scale, y: (cssY - this.oy) / this.scale };
  }

  toCss(x, y) {
    return { x: this.ox + x * this.scale, y: this.oy + y * this.scale };
  }

  _viewportChanged(silent = false) {
    this.requestRender();
    if (!silent && this.callbacks.onViewport) this.callbacks.onViewport({ scale: this.scale, ox: this.ox, oy: this.oy });
  }

  // --------------------------------------------------------------- layers
  _regionLayer(region) {
    if (region.layer && !region.dirty) return region.layer;
    const layer = region.layer || document.createElement("canvas");
    layer.width = this.width;
    layer.height = this.height;
    const ctx = layer.getContext("2d");
    const image = ctx.createImageData(this.width, this.height);
    const cls = this.classes.get(region.classId);
    const [r, g, b] = hexToRgb(cls ? cls.color : "#888888");
    const data = image.data;
    const mask = region.mask;
    for (let i = 0; i < mask.length; i += 1) {
      if (mask[i]) {
        const o = i * 4;
        data[o] = r;
        data[o + 1] = g;
        data[o + 2] = b;
        data[o + 3] = 255;
      }
    }
    ctx.putImageData(image, 0, 0);
    region.layer = layer;
    region.dirty = false;
    return layer;
  }

  _edgeLayer(region, color, dashed = false) {
    const key = `${color}:${dashed ? 1 : 0}`;
    if (region.edges && region.edges.key === key && !region.dirty) return region.edges.layer;
    const layer = document.createElement("canvas");
    layer.width = this.width;
    layer.height = this.height;
    const ctx = layer.getContext("2d");
    const image = ctx.createImageData(this.width, this.height);
    const edges = maskEdges(region.mask, this.width, this.height);
    const [r, g, b] = hexToRgb(color);
    for (let i = 0; i < edges.length; i += 1) {
      if (!edges[i]) continue;
      if (dashed) {
        const x = i % this.width;
        const y = (i - x) / this.width;
        if (((x + y) >> 2) & 1) continue; // 4px dash pattern along the outline
      }
      const o = i * 4;
      image.data[o] = r;
      image.data[o + 1] = g;
      image.data[o + 2] = b;
      image.data[o + 3] = 255;
    }
    ctx.putImageData(image, 0, 0);
    region.edges = { key, layer };
    return layer;
  }

  recomputeStats() {
    const masks = this.order.map((id) => this.regions.get(id)).filter((r) => r && r.visible).map((r) => r.mask);
    this.stats = coverageStats(masks, this.width, this.height);
    this.markersDirty = false;
    // Overlap marker: hatched magenta where multiplicity > 1; missing: cyan dots.
    const overlap = document.createElement("canvas");
    overlap.width = this.width;
    overlap.height = this.height;
    const octx = overlap.getContext("2d");
    const oimg = octx.createImageData(this.width, this.height);
    const missing = document.createElement("canvas");
    missing.width = this.width;
    missing.height = this.height;
    const mctx = missing.getContext("2d");
    const mimg = mctx.createImageData(this.width, this.height);
    const counts = this.stats.counts;
    for (let y = 0; y < this.height; y += 1) {
      for (let x = 0; x < this.width; x += 1) {
        const i = y * this.width + x;
        const o = i * 4;
        if (counts[i] > 1 && ((x + y) & 3) === 0) {
          oimg.data[o] = 255; oimg.data[o + 1] = 0; oimg.data[o + 2] = 200; oimg.data[o + 3] = 255;
        }
        if (counts[i] === 0 && ((x & 3) === 0) && ((y & 3) === 0)) {
          mimg.data[o] = 0; mimg.data[o + 1] = 220; mimg.data[o + 2] = 255; mimg.data[o + 3] = 255;
        }
      }
    }
    octx.putImageData(oimg, 0, 0);
    mctx.putImageData(mimg, 0, 0);
    this.overlapLayer = overlap;
    this.missingLayer = missing;
    if (this.callbacks.onStats) this.callbacks.onStats(this.stats);
  }

  // --------------------------------------------------------------- render
  requestRender() {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = null;
      this.render();
    });
  }

  render() {
    const ctx = this.ctx;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, this.cssWidth, this.cssHeight);
    ctx.imageSmoothingEnabled = this.scale < 1;
    ctx.save();
    ctx.translate(this.ox, this.oy);
    ctx.scale(this.scale, this.scale);

    const image = this.images[this.channel];
    // View-only brightness/contrast: the filter wraps the base image draw
    // only and is reset before any overlay is composited.
    ctx.filter = this._viewFilter();
    if (image) {
      ctx.drawImage(image, 0, 0, this.width, this.height);
    } else {
      ctx.fillStyle = "#222";
      ctx.fillRect(0, 0, this.width, this.height);
    }
    ctx.filter = "none";

    if (this.markersDirty) this.recomputeStats();

    ctx.globalAlpha = this.opacity;
    for (const id of this.order) {
      const region = this.regions.get(id);
      if (!region || !region.visible) continue;
      ctx.drawImage(this._regionLayer(region), 0, 0);
    }
    ctx.globalAlpha = 1;

    if (this.showOverlaps && this.overlapLayer && this.stats.overlapped) ctx.drawImage(this.overlapLayer, 0, 0);
    if (this.showMissing && this.missingLayer && this.stats.missing) ctx.drawImage(this.missingLayer, 0, 0);

    for (const id of this.highlightIds) {
      const region = this.regions.get(id);
      if (region && !this.selectedIds.has(id)) ctx.drawImage(this._edgeLayer(region, "#ffcc00"), 0, 0);
    }
    for (const id of this.selectedIds) {
      const region = this.regions.get(id);
      if (!region) continue;
      const primary = id === this.selectedId;
      // Locked selected regions: orange dashed outline, distinct from the
      // editable white outline. Secondary selection: cyan.
      const color = region.locked ? "#ff9f1a" : primary ? "#ffffff" : "#7fe7ff";
      ctx.drawImage(this._edgeLayer(region, color, region.locked), 0, 0);
    }

    if (this.proposalMask) {
      if (!this.proposalLayer) {
        this.proposalLayer = this._regionLayerFromMask(this.proposalMask, "#ffffff");
      }
      ctx.globalAlpha = 0.45;
      ctx.drawImage(this.proposalLayer, 0, 0);
      ctx.globalAlpha = 1;
    }
    if (this.fillPreview && this.tool === "fill") {
      if (!this.fillPreviewLayer) this.fillPreviewLayer = this._regionLayerFromMask(this.fillPreview, this._activeColor());
      ctx.globalAlpha = Math.min(0.9, this.opacity + 0.2);
      ctx.drawImage(this.fillPreviewLayer, 0, 0);
      ctx.globalAlpha = 1;
    }
    if (this.stroke && this.stroke.pending) {
      if (!this.pendingLayer) this.pendingLayer = this._regionLayerFromMask(this.stroke.mask, this._activeColor());
      ctx.globalAlpha = this.opacity;
      ctx.drawImage(this.pendingLayer, 0, 0);
      ctx.globalAlpha = 1;
    }
    ctx.restore();

    // Screen-space overlays: image frame, polygon, prompts, brush cursor.
    ctx.strokeStyle = "rgba(0,163,152,0.6)";
    ctx.lineWidth = 1;
    ctx.strokeRect(this.ox + 0.5, this.oy + 0.5, this.width * this.scale, this.height * this.scale);

    if (this.polygon.length) {
      ctx.beginPath();
      this.polygon.forEach(([x, y], index) => {
        const sx = this.ox + x * this.scale;
        const sy = this.oy + y * this.scale;
        if (index) ctx.lineTo(sx, sy); else ctx.moveTo(sx, sy);
      });
      if (this.pointer) ctx.lineTo(this.ox + this.pointer.x * this.scale, this.oy + this.pointer.y * this.scale);
      ctx.strokeStyle = "#ffffff";
      ctx.lineWidth = 1.5;
      ctx.stroke();
      for (const [x, y] of this.polygon) {
        ctx.beginPath();
        ctx.arc(this.ox + x * this.scale, this.oy + y * this.scale, 3.5, 0, Math.PI * 2);
        ctx.fillStyle = "#00a398";
        ctx.fill();
      }
    }

    for (const point of this.prompts.points) {
      ctx.beginPath();
      ctx.arc(this.ox + point.x * this.scale, this.oy + point.y * this.scale, 5, 0, Math.PI * 2);
      ctx.fillStyle = point.positive ? "#52c41a" : "#ff4d4f";
      ctx.fill();
      ctx.strokeStyle = "#fff";
      ctx.stroke();
    }
    const box = this.prompts.boxDraft || this.prompts.box;
    if (box) {
      ctx.setLineDash([4, 3]);
      ctx.strokeStyle = "#faad14";
      ctx.strokeRect(
        this.ox + box[0] * this.scale, this.oy + box[1] * this.scale,
        (box[2] - box[0]) * this.scale, (box[3] - box[1]) * this.scale,
      );
      ctx.setLineDash([]);
    }

    if ((this.tool === "brush" || this.tool === "eraser") && this.pointer) {
      ctx.beginPath();
      ctx.arc(this.ox + this.pointer.x * this.scale, this.oy + this.pointer.y * this.scale, this.brushRadius * this.scale, 0, Math.PI * 2);
      // Brush preview colour follows the class the stroke would paint with:
      // the active class when a new region would be created.
      ctx.strokeStyle = this.tool === "brush" ? this._strokeColor() : "#ff4d4f";
      ctx.lineWidth = 1.5;
      ctx.stroke();
      ctx.strokeStyle = "rgba(0,0,0,0.6)";
      ctx.lineWidth = 0.5;
      ctx.stroke();
    }
  }

  _regionLayerFromMask(mask, color) {
    const layer = document.createElement("canvas");
    layer.width = this.width;
    layer.height = this.height;
    const ctx = layer.getContext("2d");
    const image = ctx.createImageData(this.width, this.height);
    const [r, g, b] = hexToRgb(color);
    for (let i = 0; i < mask.length; i += 1) {
      if (mask[i]) {
        const o = i * 4;
        image.data[o] = r; image.data[o + 1] = g; image.data[o + 2] = b; image.data[o + 3] = 255;
      }
    }
    ctx.putImageData(image, 0, 0);
    return layer;
  }

  // ---------------------------------------------------------------- input
  _bind() {
    const canvas = this.canvas;
    canvas.addEventListener("pointerdown", (e) => this._pointerDown(e));
    canvas.addEventListener("pointermove", (e) => this._pointerMove(e));
    canvas.addEventListener("pointerup", (e) => this._pointerUp(e));
    canvas.addEventListener("pointerleave", () => {
      this.pointer = null;
      if (this.callbacks.onFillHover) this.callbacks.onFillHover(null);
      this.requestRender();
    });
    canvas.addEventListener("dblclick", (e) => { if (this.tool === "polygon") { e.preventDefault(); this.finishPolygon(e.shiftKey ? "add" : e.altKey ? "subtract" : "new"); } });
    canvas.addEventListener("wheel", (e) => {
      e.preventDefault();
      const rect = canvas.getBoundingClientRect();
      this.zoomAt(e.deltaY < 0 ? 1.15 : 1 / 1.15, e.clientX - rect.left, e.clientY - rect.top);
    }, { passive: false });
    canvas.addEventListener("contextmenu", (e) => e.preventDefault());
  }

  _local(e) {
    const rect = this.canvas.getBoundingClientRect();
    return { cssX: e.clientX - rect.left, cssY: e.clientY - rect.top };
  }

  _pointerDown(e) {
    this.canvas.focus();
    const { cssX, cssY } = this._local(e);
    const image = this.toImage(cssX, cssY);
    const panButton = e.button === 1 || (e.button === 0 && (this.tool === "pan" || this.spaceHeld));
    if (panButton) {
      this.panning = { cssX, cssY, ox: this.ox, oy: this.oy };
      this.canvas.classList.add("panning");
      this.canvas.setPointerCapture(e.pointerId);
      return;
    }
    if (e.button === 2) {
      if (this.tool === "polygon" && this.polygon.length) this.polygon.pop();
      this.requestRender();
      return;
    }
    if (e.button !== 0) return;
    switch (this.tool) {
      case "select":
        this.select(this.hitTest(image.x, image.y), { toggle: e.ctrlKey || e.metaKey, range: e.shiftKey });
        break;
      case "polygon":
        if (this._inside(image)) this.polygon.push([image.x, image.y]);
        this.requestRender();
        break;
      case "brush":
      case "eraser":
        this._beginStroke(e, image);
        break;
      case "sam_positive":
      case "sam_negative":
        if (this._inside(image) && this.callbacks.onPrompt) {
          this.callbacks.onPrompt({ type: "point", x: image.x, y: image.y, positive: this.tool === "sam_positive" });
        }
        break;
      case "sam_box":
        this.prompts.boxDraft = [image.x, image.y, image.x, image.y];
        this.canvas.setPointerCapture(e.pointerId);
        break;
      case "fill":
        if (this._inside(image) && this.callbacks.onFill) this.callbacks.onFill(image);
        break;
      default:
        break;
    }
  }

  _pointerMove(e) {
    const { cssX, cssY } = this._local(e);
    const image = this.toImage(cssX, cssY);
    this.pointer = image;
    if (this.callbacks.onCursor) this.callbacks.onCursor(this._inside(image) ? image : null);
    if (this.panning) {
      this.ox = this.panning.ox + (cssX - this.panning.cssX);
      this.oy = this.panning.oy + (cssY - this.panning.cssY);
      this._viewportChanged();
      return;
    }
    if (this.stroke) {
      this._continueStroke(image);
      return;
    }
    if (this.prompts.boxDraft) {
      this.prompts.boxDraft[2] = image.x;
      this.prompts.boxDraft[3] = image.y;
    }
    if (this.tool === "fill" && this.callbacks.onFillHover) this.callbacks.onFillHover(this._inside(image) ? image : null);
    this.requestRender();
  }

  _pointerUp(e) {
    if (this.panning) {
      this.panning = null;
      this.canvas.classList.remove("panning");
      this.canvas.releasePointerCapture(e.pointerId);
      return;
    }
    if (this.stroke) {
      this._endStroke(e);
      return;
    }
    if (this.prompts.boxDraft) {
      const b = this.prompts.boxDraft;
      const box = [Math.min(b[0], b[2]), Math.min(b[1], b[3]), Math.max(b[0], b[2]), Math.max(b[1], b[3])];
      this.prompts.boxDraft = null;
      this.canvas.releasePointerCapture(e.pointerId);
      if (box[2] - box[0] >= 2 && box[3] - box[1] >= 2 && this.callbacks.onPrompt) {
        this.callbacks.onPrompt({ type: "box", box });
      }
      this.requestRender();
    }
  }

  _inside(p) {
    return p.x >= 0 && p.y >= 0 && p.x < this.width && p.y < this.height;
  }

  hitTest(x, y) {
    if (!this._inside({ x, y })) return null;
    const i = Math.floor(y) * this.width + Math.floor(x);
    for (let k = this.order.length - 1; k >= 0; k -= 1) {
      const region = this.regions.get(this.order[k]);
      if (region && region.visible && region.mask[i]) return region.id;
    }
    return null;
  }

  // ---------------------------------------------------------------- brush
  // The region a brush stroke would extend, or null when the stroke must
  // create a new region of the active class.
  brushTarget() {
    const region = this.regions.get(this.selectedId);
    if (!region) return { region: null, reason: "no selection" };
    if (region.locked) return { region: null, reason: "selected region is locked" };
    if (this.activeClassId !== null && region.classId !== this.activeClassId) {
      return { region: null, reason: "selected region has another class" };
    }
    return { region, reason: null };
  }

  _strokeColor() {
    const target = this.brushTarget();
    if (target.region) {
      const cls = this.classes.get(target.region.classId);
      return cls ? cls.color : "#ffffff";
    }
    return this._activeColor();
  }

  _beginStroke(e, image) {
    const value = this.tool === "brush" ? 1 : 0;
    if (this.tool === "eraser") {
      const region = this.regions.get(this.selectedId);
      if (!region) {
        if (this.callbacks.onEditEnd) this.callbacks.onEditEnd({ blocked: "no-selection" });
        return;
      }
      if (region.locked) {
        if (this.callbacks.onEditEnd) this.callbacks.onEditEnd({ blocked: "locked", id: region.id });
        return;
      }
      this.stroke = { id: region.id, value, last: image, before: region.mask.slice() };
      paintStroke(region.mask, this.width, this.height, image.x, image.y, image.x, image.y, this.brushRadius, value);
      region.dirty = true;
      region.edges = null;
      this.canvas.setPointerCapture(e.pointerId);
      this.requestRender();
      return;
    }
    const { region } = this.brushTarget();
    if (!region) {
      // New region of the active class, drawn over whatever is below
      // (existing or locked regions); the overlap becomes a Finding.
      const mask = new Uint8Array(this.width * this.height);
      this.stroke = { pending: true, mask, value, last: image, before: null };
      paintStroke(mask, this.width, this.height, image.x, image.y, image.x, image.y, this.brushRadius, value);
      this.pendingLayer = this._regionLayerFromMask(mask, this._activeColor());
      this.canvas.setPointerCapture(e.pointerId);
      this.requestRender();
      return;
    }
    this.stroke = { id: region.id, value, last: image, before: region.mask.slice() };
    paintStroke(region.mask, this.width, this.height, image.x, image.y, image.x, image.y, this.brushRadius, value);
    region.dirty = true;
    region.edges = null;
    this.canvas.setPointerCapture(e.pointerId);
    this.requestRender();
  }

  _activeColor() {
    const cls = this.classes.get(this.activeClassId);
    return cls ? cls.color : "#ffffff";
  }

  _continueStroke(image) {
    const s = this.stroke;
    if (s.pending) {
      paintStroke(s.mask, this.width, this.height, s.last.x, s.last.y, image.x, image.y, this.brushRadius, s.value);
      this.pendingLayer = null;
    } else {
      const region = this.regions.get(s.id);
      paintStroke(region.mask, this.width, this.height, s.last.x, s.last.y, image.x, image.y, this.brushRadius, s.value);
      region.dirty = true;
      region.edges = null;
    }
    s.last = image;
    this.requestRender();
  }

  _endStroke(e) {
    const s = this.stroke;
    this.stroke = null;
    this.canvas.releasePointerCapture(e.pointerId);
    if (s.pending) {
      this.pendingLayer = null;
      if (this.callbacks.onCreate) this.callbacks.onCreate(s.mask, "brush");
      return;
    }
    const region = this.regions.get(s.id);
    this.markersDirty = true;
    this.requestRender();
    if (this.callbacks.onEditEnd) this.callbacks.onEditEnd({ id: s.id, before: s.before, after: region.mask.slice() });
  }

  // -------------------------------------------------------------- polygon
  finishPolygon(mode = "new") {
    if (this.polygon.length < 3) return;
    const mask = rasterizePolygon(this.polygon, this.width, this.height);
    this.polygon = [];
    this.requestRender();
    if (mode === "new") {
      if (this.callbacks.onCreate) this.callbacks.onCreate(mask, "polygon");
      return;
    }
    const region = this.regions.get(this.selectedId);
    if (!region) {
      if (this.callbacks.onCreate) this.callbacks.onCreate(mask, "polygon");
      return;
    }
    if (region.locked) {
      if (this.callbacks.onEditEnd) this.callbacks.onEditEnd({ blocked: "locked", id: region.id });
      return;
    }
    const before = region.mask.slice();
    for (let i = 0; i < mask.length; i += 1) {
      if (mask[i]) region.mask[i] = mode === "add" ? 1 : 0;
    }
    region.dirty = true;
    region.edges = null;
    this.markersDirty = true;
    this.requestRender();
    if (this.callbacks.onEditEnd) this.callbacks.onEditEnd({ id: region.id, before, after: region.mask.slice() });
  }

  cancelPolygon() {
    this.polygon = [];
    this.requestRender();
  }

  undoPolygonPoint() {
    this.polygon.pop();
    this.requestRender();
  }

  // Pixel of the composited canvas at image coordinates (test hook).
  samplePixel(x, y) {
    if (this.frame) { cancelAnimationFrame(this.frame); this.frame = null; this.render(); }
    const css = this.toCss(x + 0.5, y + 0.5);
    const data = this.ctx.getImageData(Math.floor(css.x * this.dpr), Math.floor(css.y * this.dpr), 1, 1).data;
    return [data[0], data[1], data[2], data[3]];
  }
}
