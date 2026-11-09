// SPDX-License-Identifier: MIT
// Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
// Annotation workspace orchestration: loads dataset + regions, wires the
// canvas, panels, tools, undo/redo, autosave queue, UI-state persistence,
// channel switching and keyboard shortcuts.
//
// * multi-selection of regions (click / Ctrl / Shift / Ctrl+A / Esc);
// * snapshot-based undo/redo for structural operations (Merge, Split,
//   Subtract by Order, Delete Selected, Boundary fill, import) through the
//   server-side `POST /regions/restore` transaction, so ids, masks,
//   z-order, lock/visibility and review state come back exactly;
// * Subtract by Order with preview and confirmation of emptied regions;
// * Clear selection, Select All, Delete Selected;
// * view-only brightness/contrast per channel (never persisted);
// * dataset filter aware Previous/Next navigation;
// * selection of freshly imported regions.
//
// Multi-channel upgrade:
// * a project has 1..64 co-registered source channels; the workspace shows
//   one at a time (buttons for 1-2 channels, a navigator with slider /
//   number field / name list for 3+), images load lazily per channel;
// * seed-fill tool: click an enclosed unlabelled area to create a region of
//   the active class from the connected unlabelled pixels (label-space
//   flood fill with optional gap closing), plus "Fill all enclosed gaps".

import { api } from "./api.js";
import { setUpThemeToggle } from "./theme.js";
import { installEditorHeader, setUpProjectLock } from "./lock.js";
import { AnnotationCanvas, VIEW_NEUTRAL, VIEW_RANGE } from "./annotation-canvas.js";
import { RegionsPanel, ClassesPanel } from "./regions-panel.js";
import { StatusBar } from "./status-bar.js";
import { componentMask, decodeRows, encodeRows, fillFromSeed, freeComponents, maskArea } from "./masks.js";
import { setUpFindings } from "./findings.js";
import { setUpSegmentation } from "./segmentation.js";
import { installHelp, applyHelp, closePopover } from "./help.js";
import { readDatasetFilter } from "./dataset-filter.js";

setUpThemeToggle("theme-toggle");
const editorInfo = installEditorHeader();

const body = document.body;
const data = JSON.parse(document.getElementById("workspace-data").textContent);
const projectId = body.dataset.projectId;
const datasetId = body.dataset.datasetId;
const width = Number(body.dataset.width);
const height = Number(body.dataset.height);
const base = `/api/v1/projects/${projectId}/datasets/${datasetId}`;

const state = {
  datasetRevision: data.dataset.revision,
  validatedRevision: data.dataset.validated_revision,
  classes: data.classes,
  regions: [], // server records (bottom -> top) with mask Uint8Array attached
  nextRegionSeq: 1,
  overlapReviews: {},
  selectedId: data.ui_state.selected_region_id || null, // primary / anchor
  selectedIds: new Set(),
  activeClassId: data.ui_state.selected_class_id || null,
  channels: data.channels || [],
  channel: null, // resolved in bootstrap from ui_state.channel
  readOnly: false,
  activePanel: "regions",
  structural: null, // assigned below; used by findings.js for Boundary fill
};

const message = document.getElementById("action-message");
function say(text, timeoutMs = 4000) {
  message.textContent = text || "";
  if (text && timeoutMs) setTimeout(() => { if (message.textContent === text) message.textContent = ""; }, timeoutMs);
}

// ---- save queue -------------------------------------------------------------
const saveState = document.getElementById("save-state");
let pending = 0;
let queue = Promise.resolve();
function setSaveState(text, tone) {
  saveState.textContent = text;
  saveState.className = tone || "";
}
export function enqueue(fn) {
  pending += 1;
  setSaveState("Saving…");
  const run = queue.then(fn).then(
    (result) => { pending -= 1; if (!pending) setSaveState("Saved"); return result; },
    async (err) => {
      pending -= 1;
      if (err && err.code === "REVISION_CONFLICT") {
        setSaveState("Reloaded");
        say("Dataset changed elsewhere; reloaded the latest regions.");
        await loadRegions();
      } else if (err && err.code === "PROJECT_LOCKED") {
        setSaveState("Read-only", "bad");
        say(`${err.message}. ${err.recovery || ""}`, 8000);
        await loadRegions();
      } else {
        setSaveState("Save failed", "bad");
        say((err && err.message) || "save failed", 8000);
      }
      throw err;
    },
  );
  queue = run.catch(() => {});
  return run;
}
window.addEventListener("beforeunload", (e) => { if (pending) { e.preventDefault(); e.returnValue = ""; } });

function mutation(path, options) {
  return api(`${base}${path}`, options).then((result) => {
    if (result && typeof result.dataset_revision === "number") setDatasetRevision(result.dataset_revision);
    return result;
  });
}

function setDatasetRevision(revision) {
  state.datasetRevision = revision;
  document.getElementById("dataset-revision").textContent = String(revision);
  if (state.validatedRevision !== null && state.validatedRevision !== revision) {
    state.validatedRevision = null;
    statusBar.setValidation("stale (dataset changed)", "warn");
  }
  if (findings) findings.markStale();
}

// ---- canvas + panels ------------------------------------------------------------
const opacity = document.getElementById("mask-opacity");
const brush = document.getElementById("brush-radius");
const zoom = document.getElementById("zoom-slider");
const brightness = document.getElementById("view-brightness");
const contrast = document.getElementById("view-contrast");
let findings = null;
let segmentation = null;
const statusBar = new StatusBar(2);
const canvasElement = document.getElementById("annotation-canvas");
const canvas = new AnnotationCanvas(canvasElement, {
  width,
  height,
  onEditEnd: handleEditEnd,
  onCreate: handleCreate,
  onSelect: (id, modifiers) => selectRegion(id, modifiers || {}),
  onViewport: () => { updateZoomSlider(); scheduleUiState(); },
  onStats: (stats) => { statusBar.setCoverage(stats); if (findings) findings.onStats(stats); },
  onPrompt: (prompt) => { if (segmentation) segmentation.onPrompt(prompt); },
  onCursor: (p) => { document.getElementById("cursor-readout").textContent = p ? `x: ${Math.floor(p.x)}, y: ${Math.floor(p.y)}` : "x: –, y: –"; },
  onFill: (p) => fillAt(p),
  onFillHover: (p) => fillHover(p),
});
canvas.activeClassId = state.activeClassId;

const regionsPanel = new RegionsPanel(document.getElementById("region-list"), {
  onSelect: (id, modifiers) => selectRegion(id, modifiers || {}),
  onClassChange: (id, classId) => patchRegion(id, { class_id: classId }, `class → ${classId}`),
  onToggle: (id, field, value) => patchRegion(id, { [field]: value }, `${field} → ${value}`),
  onDelete: (id) => deleteRegions([id]),
  onReorder: (orderBottomToTop) => reorder(orderBottomToTop),
});
const classesPanel = new ClassesPanel(document.getElementById("class-list"), document.getElementById("active-class-select"), {
  onActivate: (id) => setActiveClass(id),
});
document.getElementById("active-class-select").addEventListener("change", (e) => setActiveClass(Number(e.target.value)));

findings = setUpFindings({ base, projectId, datasetId, canvas, regionsPanel, statusBar, getState: () => state, enqueue, mutation, say, reload: loadRegions, showTab, onRevision: setDatasetRevision });
segmentation = setUpSegmentation({ base, canvas, statusBar, getState: () => state, say, createRegion: (mask, provenance) => createRegion(mask, "sam_proposal", provenance), applyToSelected: applyMaskToSelected });

// ---- channels / images ----------------------------------------------------
// Display images are loaded lazily per channel and cached for the page's
// lifetime; the current channel loads first, its neighbours are prefetched.
const imageCache = new Map(); // channel key -> Promise<HTMLImageElement>
function loadImage(channel) {
  if (!imageCache.has(channel)) {
    imageCache.set(channel, new Promise((resolve, reject) => {
      const image = new Image();
      image.onload = () => { canvas.setImage(channel, image); resolve(image); };
      image.onerror = () => { imageCache.delete(channel); reject(new Error(`failed to load ${channel} image`)); };
      image.src = `${base}/image/${channel}?r=${state.datasetRevision}`;
    }));
  }
  return imageCache.get(channel);
}
function channelIndex(key) {
  return state.channels.findIndex((c) => c.key === key);
}
function channelLabel(key) {
  const found = state.channels.find((c) => c.key === key);
  return found ? found.label : key;
}
function prefetchNeighbours(index) {
  for (const offset of [1, -1, 2, -2]) {
    const neighbour = state.channels[index + offset];
    if (neighbour) loadImage(neighbour.key).catch(() => {});
  }
}
async function loadImages() {
  await loadImage(state.channel);
  prefetchNeighbours(channelIndex(state.channel));
}

const channelSwitch = document.getElementById("channel-switch");
const channelCount = state.channels.length;
function renderChannelSwitch() {
  const index = channelIndex(state.channel);
  for (const button of channelSwitch.querySelectorAll("button[data-channel]")) {
    const on = button.dataset.channel === state.channel;
    button.classList.toggle("active", on);
    button.setAttribute("aria-pressed", on ? "true" : "false");
  }
  const slider = document.getElementById("channel-slider");
  if (slider) {
    slider.value = String(index + 1);
    document.getElementById("channel-index").value = String(index + 1);
    document.getElementById("channel-select").value = state.channel;
    document.getElementById("channel-prev-button").disabled = index <= 0;
    document.getElementById("channel-next-button").disabled = index >= channelCount - 1;
    channelSwitch.title = `${index + 1} / ${channelCount}: ${channelLabel(state.channel)}`;
  }
}
function setChannel(key, { announce = false } = {}) {
  if (channelIndex(key) < 0) return;
  state.channel = key;
  canvas.setChannel(key);
  loadImage(key).catch((err) => say(err.message, 6000));
  prefetchNeighbours(channelIndex(key));
  renderChannelSwitch();
  syncViewControls();
  scheduleUiState();
  if (announce && channelCount > 2) say(`Channel ${channelIndex(key) + 1} / ${channelCount}: ${channelLabel(key)}`, 1500);
}
function setChannelIndex(index, options) {
  const clamped = Math.max(0, Math.min(channelCount - 1, index));
  if (state.channels[clamped]) setChannel(state.channels[clamped].key, options);
}
function stepChannel(delta, options) {
  setChannelIndex((channelIndex(state.channel) + delta + channelCount) % channelCount, options);
}
for (const button of channelSwitch.querySelectorAll("button[data-channel]")) {
  button.addEventListener("click", () => setChannel(button.dataset.channel));
}
if (document.getElementById("channel-slider")) {
  document.getElementById("channel-slider").addEventListener("input", (e) => setChannelIndex(Number(e.target.value) - 1));
  document.getElementById("channel-index").addEventListener("change", (e) => { setChannelIndex(Number(e.target.value) - 1); renderChannelSwitch(); });
  document.getElementById("channel-index").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); e.target.blur(); } e.stopPropagation(); });
  document.getElementById("channel-select").addEventListener("change", (e) => setChannel(e.target.value));
  document.getElementById("channel-prev-button").addEventListener("click", () => setChannelIndex(channelIndex(state.channel) - 1, { announce: true }));
  document.getElementById("channel-next-button").addEventListener("click", () => setChannelIndex(channelIndex(state.channel) + 1, { announce: true }));
  channelSwitch.addEventListener("wheel", (e) => { e.preventDefault(); stepChannel(e.deltaY > 0 ? 1 : -1, { announce: true }); }, { passive: false });
}

// ---- regions ----------------------------------------------------------------------
function pushToCanvas() {
  canvas.setRegions(state.regions.map((r) => ({ id: r.region_id, classId: r.class_id, visible: r.visible, locked: r.locked, mask: r.mask })));
  regionsPanel.setRegions(state.regions);
  syncSelection();
}

function attachMask(record) {
  record.mask = decodeRows(record.mask.counts, width, height);
  return record;
}

async function loadRegions() {
  const set = await api(`${base}/regions`);
  state.regions = set.regions.map(attachMask);
  state.nextRegionSeq = set.next_region_seq || 1;
  state.datasetRevision = set.dataset_revision;
  state.validatedRevision = set.validated_revision;
  document.getElementById("dataset-revision").textContent = String(set.dataset_revision);
  state.overlapReviews = set.overlap_reviews || {};
  const ids = new Set(state.regions.map((r) => r.region_id));
  for (const id of Array.from(state.selectedIds)) if (!ids.has(id)) state.selectedIds.delete(id);
  if (state.selectedId && !ids.has(state.selectedId)) state.selectedId = state.selectedIds.values().next().value || null;
  pushToCanvas();
  findings.onRegionsLoaded(set);
  return set;
}

function regionById(id) {
  return state.regions.find((r) => r.region_id === id) || null;
}

// ---- selection ----------------------------------------------------
function syncSelection() {
  canvas.setSelection(state.selectedId, Array.from(state.selectedIds));
  regionsPanel.setSelection(state.selectedId, Array.from(state.selectedIds));
  const count = state.selectedIds.size;
  document.getElementById("region-delete-selected-button").disabled = count === 0 || state.readOnly;
  document.getElementById("region-subtract-button").disabled = count < 2 || state.readOnly;
  document.getElementById("region-merge-button").disabled = count < 2 || state.readOnly;
  document.getElementById("region-split-button").disabled = !state.selectedId || state.readOnly;
  document.getElementById("region-duplicate-button").disabled = !state.selectedId || state.readOnly;
  document.getElementById("region-clear-selection-button").disabled = count === 0;
  document.getElementById("clear-selection-button").disabled = count === 0;
}

function selectRegion(id, { toggle = false, range = false, forceOn } = {}) {
  const order = regionsPanel.displayedIds();
  if (id === null) {
    if (!toggle && !range) clearSelection();
    return;
  }
  if (range && state.selectedId && order.includes(state.selectedId) && order.includes(id)) {
    const a = order.indexOf(state.selectedId);
    const b = order.indexOf(id);
    const [lo, hi] = a < b ? [a, b] : [b, a];
    const slice = order.slice(lo, hi + 1);
    state.selectedIds = new Set([...(toggle ? state.selectedIds : []), ...slice]);
  } else if (toggle) {
    const on = forceOn === undefined ? !state.selectedIds.has(id) : forceOn;
    if (on) state.selectedIds.add(id); else state.selectedIds.delete(id);
    state.selectedId = on ? id : (state.selectedIds.values().next().value || null);
  } else {
    state.selectedIds = new Set([id]);
    state.selectedId = id;
  }
  if (state.selectedId && !state.selectedIds.has(state.selectedId)) state.selectedId = state.selectedIds.values().next().value || null;
  syncSelection();
  canvas.requestRender();
  scheduleUiState();
}

function select(id) {
  selectRegion(id, {});
}

function selectAll() {
  const ids = regionsPanel.displayedIds();
  state.selectedIds = new Set(ids);
  state.selectedId = state.selectedId && ids.includes(state.selectedId) ? state.selectedId : ids[0] || null;
  syncSelection();
  say(`${ids.length} region(s) selected.`, 2000);
}

// Leave the edit context without touching any region.
function clearSelection() {
  state.selectedIds = new Set();
  state.selectedId = null;
  canvas.setHighlight([]);
  syncSelection();
  canvas.requestRender();
  scheduleUiState();
}

function setActiveClass(id) {
  state.activeClassId = id;
  canvas.activeClassId = id;
  classesPanel.setActive(id);
  canvas.requestRender();
  scheduleUiState();
}

function maskPayload(mask) {
  return { format: "rle_rows", size: [height, width], counts: encodeRows(mask) };
}

function replaceRecord(record) {
  attachMask(record);
  const index = state.regions.findIndex((r) => r.region_id === record.region_id);
  if (index >= 0) state.regions[index] = record; else state.regions.push(record);
  state.regions.sort((a, b) => a.z_order - b.z_order);
  return record;
}

// ---- undo / redo --------------------------------------------------------------
const undoStack = [];
const redoStack = [];
function pushHistory(entry) {
  undoStack.push(entry);
  if (undoStack.length > 100) undoStack.shift();
  redoStack.length = 0;
  updateHistoryButtons();
}
function updateHistoryButtons() {
  const undoButton = document.getElementById("undo-button");
  const redoButton = document.getElementById("redo-button");
  undoButton.disabled = undoStack.length === 0;
  redoButton.disabled = redoStack.length === 0;
  undoButton.dataset.helpNote = undoStack.length ? `Next undo: ${undoStack[undoStack.length - 1].label || "edit"}` : "Nothing to undo";
  redoButton.dataset.helpNote = redoStack.length ? `Next redo: ${redoStack[redoStack.length - 1].label || "edit"}` : "Nothing to redo";
}
async function undo() {
  const entry = undoStack.pop();
  if (!entry) return;
  try { await entry.undo(); redoStack.push(entry); say(`Undo: ${entry.label || "edit"}`, 2000); } catch (err) { say(`undo failed: ${err.message}`); }
  updateHistoryButtons();
}
async function redo() {
  const entry = redoStack.pop();
  if (!entry) return;
  try { await entry.redo(); undoStack.push(entry); say(`Redo: ${entry.label || "edit"}`, 2000); } catch (err) { say(`redo failed: ${err.message}`); }
  updateHistoryButtons();
}
document.getElementById("undo-button").addEventListener("click", undo);
document.getElementById("redo-button").addEventListener("click", redo);

// Snapshot of the canonical region set as the browser knows it. Sent back
// verbatim to POST /regions/restore for undo/redo of structural operations.
function snapshot() {
  return {
    regions: state.regions.map((r) => ({
      region_id: r.region_id,
      class_id: r.class_id,
      z_order: r.z_order,
      visible: r.visible,
      locked: r.locked,
      name: r.name,
      source: r.source,
      user_corrected: r.user_corrected,
      revision: r.revision,
      created_at: r.created_at,
      modified_at: r.modified_at,
      provenance: r.provenance,
      mask: maskPayload(r.mask),
    })),
    overlap_reviews: JSON.parse(JSON.stringify(state.overlapReviews || {})),
    next_region_seq: state.nextRegionSeq,
    selectedIds: Array.from(state.selectedIds),
    selectedId: state.selectedId,
  };
}

function restore(snap, reason) {
  return enqueue(() => api(`${base}/regions/restore`, {
    method: "POST",
    body: { expected_revision: state.datasetRevision, regions: snap.regions, overlap_reviews: snap.overlap_reviews, next_region_seq: snap.next_region_seq, reason },
  }).then((set) => {
    state.regions = set.regions.map(attachMask);
    state.nextRegionSeq = set.next_region_seq || state.nextRegionSeq;
    state.overlapReviews = set.overlap_reviews || {};
    setDatasetRevision(set.dataset_revision);
    state.selectedIds = new Set(snap.selectedIds.filter((id) => state.regions.some((r) => r.region_id === id)));
    state.selectedId = snap.selectedId && state.regions.some((r) => r.region_id === snap.selectedId) ? snap.selectedId : (state.selectedIds.values().next().value || null);
    pushToCanvas();
    findings.onRegionsLoaded(set);
  }));
}

// Run a structural operation as one undo transaction: snapshot before,
// perform, snapshot after; undo/redo restore the respective snapshot.
async function structural(label, perform) {
  const before = snapshot();
  const result = await perform();
  await loadRegions();
  const after = snapshot();
  pushHistory({ label, undo: () => restore(before, `undo ${label}`), redo: () => restore(after, `redo ${label}`) });
  return result;
}
state.structural = structural;

// ---- edits ----------------------------------------------------------------------
function guardEdit() {
  if (state.readOnly) { say("Project is open read-only (another session holds the editing lock)."); return false; }
  return true;
}

function saveMask(id, mask) {
  return enqueue(() => mutation(`/regions/${id}`, {
    method: "PATCH",
    body: { expected_revision: state.datasetRevision, mask: maskPayload(mask) },
  }).then((result) => { replaceRecord(result.region); pushToCanvas(); }));
}

function applyLocalMask(id, mask) {
  const record = regionById(id);
  if (!record) return;
  record.mask = mask.slice();
  record.area = maskArea(record.mask);
  canvas.updateRegionMask(id, record.mask);
}

function handleEditEnd({ id, before, after, blocked }) {
  if (blocked === "locked") { say("Region is locked — editing disabled. Unlock it in the Regions panel, or paint a new region over it with the brush.", 6000); return; }
  if (blocked === "no-selection") { say("Eraser needs a selected region.", 4000); return; }
  if (!guardEdit()) { applyLocalMask(id, before); return; }
  if (maskArea(after) === 0) {
    say("Region would become empty; edit reverted. Delete the region instead.");
    applyLocalMask(id, before);
    return;
  }
  const record = regionById(id);
  if (record) record.area = maskArea(after);
  regionsPanel.setRegions(state.regions);
  syncSelection();
  pushHistory({
    label: "stroke",
    undo: () => { applyLocalMask(id, before); return saveMask(id, before); },
    redo: () => { applyLocalMask(id, after); return saveMask(id, after); },
  });
  saveMask(id, after).catch(() => {});
}

function applyMaskToSelected(mask, mode) {
  const record = regionById(state.selectedId);
  if (!record) return false;
  if (record.locked) { say("Region is locked — editing disabled.", 4000); return false; }
  const before = record.mask.slice();
  const after = record.mask.slice();
  for (let i = 0; i < mask.length; i += 1) if (mask[i]) after[i] = mode === "subtract" ? 0 : 1;
  applyLocalMask(record.region_id, after);
  handleEditEnd({ id: record.region_id, before, after });
  return true;
}

async function createRegion(mask, source, provenance, name) {
  if (!guardEdit()) return null;
  if (!state.activeClassId) { say("Select an active class first (Classes tab)."); return null; }
  if (maskArea(mask) === 0) { say("Empty selection."); return null; }
  const classId = state.activeClassId;
  const body = { expected_revision: state.datasetRevision, class_id: classId, mask: maskPayload(mask), source, provenance: provenance || {} };
  if (name) body.name = name;
  const result = await enqueue(() => mutation("/regions", { method: "POST", body }));
  const record = replaceRecord(result.region);
  pushToCanvas();
  select(record.region_id);
  let currentId = record.region_id;
  pushHistory({
    label: "new region",
    undo: () => enqueue(() => mutation(`/regions/${currentId}?expected_revision=${state.datasetRevision}`, { method: "DELETE" })).then(() => {
      state.regions = state.regions.filter((r) => r.region_id !== currentId);
      state.selectedIds.delete(currentId);
      if (state.selectedId === currentId) state.selectedId = null;
      pushToCanvas();
    }),
    redo: () => enqueue(() => mutation("/regions", { method: "POST", body: { ...body, expected_revision: state.datasetRevision } })).then((res) => {
      currentId = res.region.region_id;
      replaceRecord(res.region);
      pushToCanvas();
      select(currentId);
    }),
  });
  return record;
}
function handleCreate(mask, how) {
  const previous = regionById(state.selectedId);
  createRegion(mask, "manual", { tool: how, channel: state.channel }).then((record) => {
    if (record && previous && how === "brush") {
      const why = previous.locked ? "the selected region was locked" : previous.class_id !== record.class_id ? "the active class differs from the selected region" : null;
      if (why) say(`New ${className(record.class_id)} region ${record.region_id} created over ${previous.region_id} (${why}); the overlap is listed in Findings.`, 6000);
    }
  }).catch(() => {});
}

function className(id) {
  const cls = state.classes.find((c) => c.class_id === id);
  return cls ? cls.name : `#${id}`;
}

function patchRegion(id, patch, label) {
  if (!guardEdit()) return Promise.resolve();
  const before = regionById(id);
  const previous = { class_id: before.class_id, visible: before.visible, locked: before.locked };
  const apply = (fields) => enqueue(() => mutation(`/regions/${id}`, { method: "PATCH", body: { expected_revision: state.datasetRevision, ...fields } })
    .then((result) => { replaceRecord(result.region); pushToCanvas(); }));
  pushHistory({
    label,
    undo: () => apply(Object.fromEntries(Object.keys(patch).map((k) => [k, previous[k]]))),
    redo: () => apply(patch),
  });
  return apply(patch).then(() => say(label, 1500)).catch((err) => { if (err && err.code === "REGION_LOCKED") say("Region is locked — editing disabled. Unlock it first.", 5000); });
}

// Delete one or many regions in one undo transaction.
async function deleteRegions(ids) {
  if (!guardEdit()) return;
  const targets = ids.map(regionById).filter(Boolean);
  if (!targets.length) return;
  const locked = targets.filter((r) => r.locked);
  const deletable = targets.filter((r) => !r.locked);
  if (!deletable.length) { say(`${locked.length} selected region(s) are locked — nothing deleted. Unlock them first.`, 6000); return; }
  const pixels = deletable.reduce((sum, r) => sum + r.area, 0);
  const text = deletable.length === 1
    ? `Delete ${deletable[0].name || deletable[0].region_id} (${deletable[0].area} px)?`
    : `Delete ${deletable.length} selected regions (${pixels} px in total)?`;
  const lockedNote = locked.length ? `\n${locked.length} locked region(s) will be kept: ${locked.map((r) => r.region_id).join(", ")}.` : "";
  if (!window.confirm(`${text}${lockedNote}\n\nUndo restores them with their original ids.`)) return;
  try {
    const result = await structural(deletable.length === 1 ? "delete region" : `delete ${deletable.length} regions`, () => enqueue(() => mutation("/regions/bulk-delete", {
      method: "POST",
      body: { expected_revision: state.datasetRevision, region_ids: deletable.map((r) => r.region_id) },
    })));
    clearSelection();
    say(`Deleted ${result.deleted_region_ids.length} region(s)${result.kept_locked_region_ids.length ? `; kept ${result.kept_locked_region_ids.length} locked` : ""}. Undo is available.`, 5000);
  } catch (err) { say(err.recovery ? `${err.message}. ${err.recovery}.` : err.message, 8000); }
}

function deleteSelected() {
  deleteRegions(Array.from(state.selectedIds));
}

function reorder(orderBottomToTop) {
  if (!guardEdit()) return;
  const previous = state.regions.map((r) => r.region_id);
  const apply = (order) => enqueue(() => api(`${base}/regions/reorder`, { method: "POST", body: { expected_revision: state.datasetRevision, order } })
    .then((set) => { state.regions = set.regions.map(attachMask); setDatasetRevision(set.dataset_revision); pushToCanvas(); findings.onRegionsLoaded(set); }));
  pushHistory({ label: "z-order change", undo: () => apply(previous), redo: () => apply(orderBottomToTop) });
  apply(orderBottomToTop).catch(() => {});
}

document.getElementById("region-split-button").addEventListener("click", async () => {
  if (!guardEdit() || !state.selectedId) { say("Select a region to split."); return; }
  const id = state.selectedId;
  try {
    const result = await structural("split", () => enqueue(() => mutation(`/regions/${id}/split`, { method: "POST", body: { expected_revision: state.datasetRevision } })));
    state.selectedIds = new Set(result.regions.map((r) => r.region_id));
    state.selectedId = result.regions[0].region_id;
    syncSelection();
    say(`Split into ${result.regions.length} regions. Undo restores the original region.`);
  } catch (err) { say(err.recovery ? `${err.message}. ${err.recovery}.` : err.message, 6000); }
});

document.getElementById("region-merge-button").addEventListener("click", async () => {
  const ids = Array.from(state.selectedIds);
  if (!guardEdit() || ids.length < 2) { say("Select at least two regions of the same class (Ctrl+click or the checkboxes)."); return; }
  const classes = new Set(ids.map((id) => regionById(id)).filter(Boolean).map((r) => r.class_id));
  if (classes.size > 1) { say("Selected regions belong to different classes. Merge requires one target class; reassign the classes first.", 7000); return; }
  const ordered = [state.selectedId, ...ids.filter((id) => id !== state.selectedId)].filter(Boolean);
  try {
    const result = await structural("merge", () => enqueue(() => mutation("/regions/merge", { method: "POST", body: { expected_revision: state.datasetRevision, region_ids: ordered } })));
    select(result.region.region_id);
    say(`Merged ${ordered.length} regions into ${result.region.region_id}. Undo restores every original region.`, 5000);
  } catch (err) { say(err.recovery ? `${err.message}. ${err.recovery}.` : err.message, 6000); }
});

// Subtract by Order: preview, confirm emptied regions, apply.
async function subtractByOrder() {
  const ids = Array.from(state.selectedIds);
  if (!guardEdit() || ids.length < 2) { say("Select at least two regions to subtract by order."); return; }
  try {
    const preview = await api(`${base}/regions/subtract-by-order/preview`, { method: "POST", body: { region_ids: ids } });
    if (preview.locked_region_ids.length) {
      say(`${preview.locked_region_ids.length} locked region(s) would lose pixels: ${preview.locked_region_ids.join(", ")}. Unlock or deselect them first.`, 8000);
      return;
    }
    if (preview.total_pixels_removed === 0) { say("The selected regions do not overlap each other; nothing to subtract.", 5000); return; }
    const lines = preview.entries.map((e) => `${e.region_id}: ${e.pixels_before} → ${e.pixels_after} px${e.becomes_empty ? " (becomes EMPTY and will be removed)" : ""}`);
    const emptyNote = preview.empty_region_ids.length ? `\n\n${preview.empty_region_ids.length} selected region(s) will become empty after subtraction. Review the affected regions before confirming.` : "";
    const ok = window.confirm(`Subtract by Order (top of the Regions list keeps its pixels):\n\n${lines.join("\n")}${emptyNote}\n\nNon-selected regions are not changed. One undo restores the exact previous masks.`);
    if (!ok) return;
    const result = await structural("subtract by order", () => enqueue(() => mutation("/regions/subtract-by-order", {
      method: "POST",
      body: { expected_revision: state.datasetRevision, region_ids: ids, allow_empty_removal: preview.empty_region_ids.length > 0 },
    })));
    state.selectedIds = new Set(ids.filter((id) => !result.removed_region_ids.includes(id)));
    if (!state.selectedIds.has(state.selectedId)) state.selectedId = state.selectedIds.values().next().value || null;
    syncSelection();
    say(`Subtracted ${preview.total_pixels_removed} px${result.removed_region_ids.length ? `; removed ${result.removed_region_ids.length} empty region(s)` : ""}. Findings are recomputed.`, 6000);
  } catch (err) { say(err.recovery ? `${err.message}. ${err.recovery}.` : err.message, 8000); }
}

document.getElementById("region-subtract-button").addEventListener("click", subtractByOrder);
document.getElementById("region-delete-selected-button").addEventListener("click", deleteSelected);
document.getElementById("region-select-all-button").addEventListener("click", selectAll);
document.getElementById("region-clear-selection-button").addEventListener("click", clearSelection);
document.getElementById("clear-selection-button").addEventListener("click", () => { canvas.cancelPolygon(); segmentation.clearPrompts(); clearSelection(); say("Selection cleared; the next brush stroke starts a new region of the active class.", 3000); });
document.getElementById("region-duplicate-button").addEventListener("click", async () => {
  if (!guardEdit() || !state.selectedId) { say("Select a region to duplicate."); return; }
  const id = state.selectedId;
  try {
    const result = await structural("duplicate", () => enqueue(() => mutation(`/regions/${id}/duplicate`, { method: "POST", body: { expected_revision: state.datasetRevision } })));
    select(result.region.region_id);
  } catch (err) { say(err.message, 6000); }
});

// ---- seed fill (fill tool) ------------------------------------------------------------
// Label-space flood fill: the clicked unlabelled pixel's connected free
// component, bounded by every visible region. Gap closing thickens the
// boundaries by N px first so the fill cannot leak through small openings.
const fillGapInput = document.getElementById("fill-gap");
const fillConnectivityInput = document.getElementById("fill-connectivity");
const fillNameInput = document.getElementById("fill-name");
const fillHint = document.getElementById("fill-hint");
const FILL_LEAK_FRACTION = 0.25;
let fillHoverTimer = null;
let fillHoverPoint = null;

function fillOptions() {
  const gapPx = Math.max(0, Math.min(20, Math.round(Number(fillGapInput.value) || 0)));
  const connectivity = Number(fillConnectivityInput.value) === 8 ? 8 : 4;
  return { gapPx, connectivity };
}
function fillName() {
  const value = fillNameInput.value.trim();
  return value ? value.slice(0, 100) : null;
}
function freeCounts() {
  if (!canvas.stats || canvas.markersDirty) canvas.recomputeStats();
  return canvas.stats.counts;
}

function fillHover(point) {
  fillHoverPoint = point;
  if (fillHoverTimer) return;
  fillHoverTimer = setTimeout(() => {
    fillHoverTimer = null;
    if (canvas.tool !== "fill") { canvas.setFillPreview(null); return; }
    const p = fillHoverPoint;
    if (!p) { canvas.setFillPreview(null); fillHint.textContent = ""; return; }
    const result = fillFromSeed(freeCounts(), width, height, p.x, p.y, fillOptions());
    canvas.setFillPreview(result ? result.mask : null);
    fillHint.textContent = result
      ? `${result.area} px${result.gapClosed ? "" : fillOptions().gapPx ? " (seed too close to a boundary for gap closing)" : ""}`
      : "cursor is on a labelled pixel";
  }, 90);
}

async function fillAt(point) {
  if (!guardEdit()) return;
  if (!state.activeClassId) { say("Select an active class first (Classes tab)."); return; }
  const options = fillOptions();
  const result = fillFromSeed(freeCounts(), width, height, point.x, point.y, options);
  if (!result) { say("Click an unlabelled pixel inside the area to fill.", 4000); return; }
  if (result.area > FILL_LEAK_FRACTION * width * height) {
    const ok = window.confirm(`The fill would cover ${result.area} px (${(100 * result.area / (width * height)).toFixed(1)} % of the image). The area is probably not enclosed.

Create the region anyway?`);
    if (!ok) return;
  }
  canvas.setFillPreview(null);
  const record = await createRegion(result.mask, "manual", {
    tool: "fill",
    channel: state.channel,
    seed: { x: Math.floor(point.x), y: Math.floor(point.y) },
    gap_px: options.gapPx,
    connectivity: options.connectivity,
  }, fillName()).catch(() => null);
  if (record) say(`Filled ${result.area} px as ${className(record.class_id)} region ${record.region_id}${record.name ? ` "${record.name}"` : ""}.`, 4000);
}

async function fillAllGaps() {
  if (!guardEdit()) return;
  if (!state.activeClassId) { say("Select an active class first (Classes tab)."); return; }
  const options = fillOptions();
  const includeBorder = document.getElementById("fill-include-border").checked;
  const components = freeComponents(freeCounts(), width, height, options);
  const chosen = [];
  let pixels = 0;
  for (let label = 1; label <= components.count; label += 1) {
    if (!includeBorder && components.touchesBorder[label]) continue;
    chosen.push(label);
    pixels += components.sizes[label];
  }
  if (!chosen.length) {
    say(components.count ? "Every unlabelled area touches the image border; tick 'include areas touching the image border' to fill them too." : "No unlabelled pixels to fill.", 7000);
    return;
  }
  if (chosen.length > 5000) { say(`${chosen.length} gaps found; raise gap closing or fill in several passes (max 5000 at once).`, 8000); return; }
  const name = fillName();
  const ok = window.confirm(`Fill ${chosen.length} enclosed unlabelled area(s), ${pixels} px in total, as ${className(state.activeClassId)}${name ? ` named "${name}"` : ""}?

Each area becomes its own region. One undo removes them all.`);
  if (!ok) return;
  const items = chosen.map((label) => ({
    mask: maskPayload(componentMask(components.labels, label)),
    name,
    provenance: { tool: "fill_all", channel: state.channel, gap_px: options.gapPx, connectivity: options.connectivity, pixels: components.sizes[label] },
  }));
  try {
    const result = await structural(`fill ${chosen.length} gaps`, () => enqueue(() => mutation("/regions/bulk-create", {
      method: "POST",
      body: { expected_revision: state.datasetRevision, class_id: state.activeClassId, items, operation: "fill_gaps" },
    })));
    state.selectedIds = new Set(result.regions.map((r) => r.region_id));
    state.selectedId = result.regions.length ? result.regions[0].region_id : null;
    syncSelection();
    say(`Created ${result.regions.length} region(s) from enclosed gaps (${pixels} px). Undo removes them together.`, 6000);
  } catch (err) { say(err.recovery ? `${err.message}. ${err.recovery}.` : err.message, 8000); }
}
document.getElementById("fill-all-button").addEventListener("click", fillAllGaps);
for (const input of [fillGapInput, fillConnectivityInput]) {
  input.addEventListener("change", () => { fillHover(fillHoverPoint); scheduleUiState(); });
}
for (const input of [fillGapInput, fillNameInput]) {
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); e.target.blur(); } e.stopPropagation(); });
}

// ---- classes -----------------------------------------------------------------------
async function reloadClasses() {
  const detail = await api(`/api/v1/projects/${projectId}`);
  state.classes = detail.classes.classes;
  state.projectRevision = detail.project.revision;
  canvas.setClasses(state.classes);
  regionsPanel.setClasses(state.classes);
  classesPanel.setClasses(state.classes);
  if (classesPanel.activeId !== state.activeClassId) setActiveClass(classesPanel.activeId);
  return detail;
}
document.getElementById("quick-class-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = document.getElementById("quick-class-name");
  const error = document.getElementById("class-error");
  error.textContent = "";
  try {
    const detail = await api(`/api/v1/projects/${projectId}`);
    const result = await api(`/api/v1/projects/${projectId}/classes`, { method: "POST", body: { expected_revision: detail.project.revision, name: input.value } });
    input.value = "";
    await reloadClasses();
    setActiveClass(result.classes.classes[result.classes.classes.length - 1].class_id);
  } catch (err) { error.textContent = err.message; }
});

// ---- tools -----------------------------------------------------------------------
function setTool(tool) {
  const button = document.querySelector(`.tool-button[data-tool="${tool}"]`);
  if (!button || button.disabled) return;
  canvas.setTool(tool);
  for (const b of document.querySelectorAll(".tool-button[data-tool]")) {
    const on = b.dataset.tool === tool;
    b.classList.toggle("active", on);
    b.setAttribute("aria-pressed", on ? "true" : "false");
  }
  document.getElementById("polygon-actions").hidden = tool !== "polygon";
  document.getElementById("fill-options").hidden = tool !== "fill";
  if (tool !== "fill") { canvas.setFillPreview(null); fillHint.textContent = ""; }
  scheduleUiState();
}
for (const button of document.querySelectorAll(".tool-button[data-tool]")) {
  button.addEventListener("click", () => setTool(button.dataset.tool));
}
document.querySelector('[data-action="fit"]').addEventListener("click", () => canvas.fit());
document.querySelector('[data-action="zoom-in"]').addEventListener("click", () => canvas.zoomAt(1.25, canvas.cssWidth / 2, canvas.cssHeight / 2));
document.querySelector('[data-action="zoom-out"]').addEventListener("click", () => canvas.zoomAt(0.8, canvas.cssWidth / 2, canvas.cssHeight / 2));
document.getElementById("polygon-finish-button").addEventListener("click", () => canvas.finishPolygon("new"));
document.getElementById("polygon-cancel-button").addEventListener("click", () => canvas.cancelPolygon());
setInterval(() => {
  if (canvas.tool === "polygon") document.getElementById("polygon-hint").textContent = `Polygon: ${canvas.polygon.length} point${canvas.polygon.length === 1 ? "" : "s"}`;
}, 250);

// ---- appearance ---------------------------------------------------------------
function applyOpacity(value) {
  canvas.opacity = value / 100;
  opacity.value = String(value);
  document.getElementById("mask-opacity-value").textContent = `${value} %`;
  canvas.requestRender();
}
function applyBrush(value) {
  canvas.brushRadius = value;
  brush.value = String(value);
  document.getElementById("brush-radius-value").textContent = `${value} px`;
  canvas.requestRender();
}
function updateZoomSlider() {
  zoom.value = String(Math.round(canvas.scale * 100));
  document.getElementById("zoom-value").textContent = `${Math.round(canvas.scale * 100)} %`;
}
opacity.addEventListener("input", () => { applyOpacity(Number(opacity.value)); scheduleUiState(); });
brush.addEventListener("input", () => { applyBrush(Number(brush.value)); scheduleUiState(); });
zoom.addEventListener("input", () => canvas.zoomTo(Number(zoom.value) / 100));
document.getElementById("show-overlaps").addEventListener("change", (e) => { canvas.showOverlaps = e.target.checked; canvas.requestRender(); scheduleUiState(); });
document.getElementById("show-missing").addEventListener("change", (e) => { canvas.showMissing = e.target.checked; canvas.requestRender(); scheduleUiState(); });

// ---- view-only brightness / contrast ---------------------------
// Temporary state per channel inside this page only: never written to
// ui-state, project files, exports or SAM requests. A reload resets it.
function syncViewControls() {
  const v = canvas.currentView();
  brightness.value = String(v.brightness);
  contrast.value = String(v.contrast);
  document.getElementById("view-brightness-value").textContent = `${v.brightness} %`;
  document.getElementById("view-contrast-value").textContent = `${v.contrast} %`;
  const neutral = v.brightness === VIEW_NEUTRAL.brightness && v.contrast === VIEW_NEUTRAL.contrast;
  document.getElementById("view-reset-button").disabled = neutral;
  document.getElementById("view-state").textContent = neutral ? "neutral (original rendering)" : `adjusted for ${channelLabel(state.channel)} · view only, not saved`;
}
for (const [input, key] of [[brightness, "brightness"], [contrast, "contrast"]]) {
  input.min = String(VIEW_RANGE.min);
  input.max = String(VIEW_RANGE.max);
  input.addEventListener("input", () => { canvas.setView(state.channel, { [key]: Number(input.value) }); syncViewControls(); });
  input.addEventListener("dblclick", () => { canvas.setView(state.channel, { [key]: VIEW_NEUTRAL[key] }); syncViewControls(); });
}
document.getElementById("view-reset-button").addEventListener("click", () => { canvas.resetView(state.channel); syncViewControls(); });

// ---- tabs ---------------------------------------------------------------------------
for (const tab of document.querySelectorAll(".lv-tab")) {
  tab.addEventListener("click", () => showTab(tab.id));
}
export function showTab(tabId) {
  for (const tab of document.querySelectorAll(".lv-tab")) {
    const selected = tab.id === tabId;
    tab.setAttribute("aria-selected", selected ? "true" : "false");
    document.getElementById(tab.getAttribute("aria-controls")).hidden = !selected;
  }
  state.activePanel = tabId === "tab-findings" ? "findings" : "regions";
}
document.getElementById("status-coverage").addEventListener("click", () => showTab("tab-findings"));
document.getElementById("status-overlaps").addEventListener("click", () => showTab("tab-findings"));
document.getElementById("status-missing").addEventListener("click", () => {
  const box = document.getElementById("show-missing");
  box.checked = true;
  canvas.showMissing = true;
  canvas.requestRender();
  showTab("tab-findings");
});
for (const [id, panel] of [["panel-regions", "regions"], ["panel-findings", "findings"], ["panel-classes", "classes"]]) {
  const el = document.getElementById(id);
  el.addEventListener("pointerdown", () => { state.activePanel = panel; });
  el.addEventListener("focusin", () => { state.activePanel = panel; });
}
canvasElement.addEventListener("pointerdown", () => { state.activePanel = "canvas"; });

// ---- ui state persistence -------------------------------------------------------
let uiTimer = null;
function scheduleUiState() {
  clearTimeout(uiTimer);
  uiTimer = setTimeout(persistUiState, 800);
}
function persistUiState() {
  const payload = {
    last_dataset_id: datasetId,
    channel: state.channel,
    fill_gap_px: fillOptions().gapPx,
    fill_connectivity: fillOptions().connectivity,
    selected_region_id: state.selectedId,
    selected_class_id: state.activeClassId,
    tool: canvas.tool,
    zoom: canvas.scale,
    pan_x: canvas.ox,
    pan_y: canvas.oy,
    overlay_opacity: canvas.opacity,
    brush_radius: canvas.brushRadius,
    show_overlaps: canvas.showOverlaps,
    show_missing: canvas.showMissing,
    selected_backend_id: segmentation ? segmentation.selectedBackendId() : null,
  };
  api(`/api/v1/projects/${projectId}/ui-state`, { method: "PUT", body: payload }).catch(() => {});
}
window.addEventListener("pagehide", () => { clearTimeout(uiTimer); });

// ---- dataset navigation (filter aware) ------------------------------------------
const nav = { previous: body.dataset.previousDatasetId || null, next: body.dataset.nextDatasetId || null };
async function applyDatasetFilterNavigation() {
  const filter = readDatasetFilter(projectId);
  const note = document.getElementById("dataset-filter-note");
  if (!filter) { note.hidden = true; return; }
  try {
    const list = await api(`/api/v1/projects/${projectId}/datasets?q=${encodeURIComponent(filter)}`);
    const ids = list.map((d) => d.dataset_id);
    const index = ids.indexOf(datasetId);
    nav.previous = index > 0 ? ids[index - 1] : null;
    nav.next = index >= 0 && index + 1 < ids.length ? ids[index + 1] : null;
    note.hidden = false;
    note.textContent = index >= 0 ? `filter "${filter}": ${index + 1} / ${ids.length}` : `filter "${filter}" (this dataset is not in the filtered list)`;
    for (const [id, target] of [["prev-dataset-button", nav.previous], ["next-dataset-button", nav.next]]) {
      const link = document.getElementById(id);
      link.classList.toggle("disabled", !target);
      link.setAttribute("aria-disabled", target ? "false" : "true");
      link.href = target ? `/projects/${projectId}/datasets/${target}` : "#";
    }
  } catch (err) { note.hidden = true; }
}

// ---- keyboard ---------------------------------------------------------------------
document.addEventListener("keydown", (e) => {
  const inField = e.target.closest("input, textarea, select");
  if (e.key === " " && !inField) { canvas.spaceHeld = true; e.preventDefault(); return; }
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key.toLowerCase() === "z") { e.preventDefault(); undo(); return; }
  if ((e.ctrlKey || e.metaKey) && (e.key.toLowerCase() === "y" || (e.shiftKey && e.key.toLowerCase() === "z"))) { e.preventDefault(); redo(); return; }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); queue.then(() => setSaveState(pending ? "Saving…" : "Saved")); return; }
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); findings.validate(); return; }
  if (inField) return;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") {
    // Scoped select-all: Regions panel (also the canvas) or Findings panel.
    if (state.activePanel === "findings") { e.preventDefault(); findings.selectAllVisible(); return; }
    e.preventDefault();
    selectAll();
    return;
  }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  switch (e.key) {
    case "v": case "V": setTool("select"); break;
    case "h": case "H": setTool("pan"); break;
    case "p": case "P": setTool("polygon"); break;
    case "b": case "B": setTool("brush"); break;
    case "e": case "E": setTool("eraser"); break;
    case "g": case "G": setTool("fill"); break;
    case "q": case "Q": setTool("sam_positive"); break;
    case "w": case "W": setTool("sam_negative"); break;
    case "x": case "X": setTool("sam_box"); break;
    case "f": case "F": canvas.fit(); break;
    case "1": case "2": case "3": case "4": case "5": case "6": case "7": case "8": case "9":
      if (channelCount > 1) setChannelIndex(Number(e.key) - 1, { announce: true });
      break;
    case "0": if (channelCount >= 10) setChannelIndex(9, { announce: true }); break;
    case "Tab": e.preventDefault(); if (channelCount > 1) stepChannel(e.shiftKey ? -1 : 1, { announce: true }); break;
    case "[": applyBrush(Math.max(1, canvas.brushRadius - 1)); scheduleUiState(); break;
    case "]": applyBrush(Math.min(80, canvas.brushRadius + 1)); scheduleUiState(); break;
    case "+": case "=": canvas.zoomAt(1.25, canvas.cssWidth / 2, canvas.cssHeight / 2); break;
    case "-": canvas.zoomAt(0.8, canvas.cssWidth / 2, canvas.cssHeight / 2); break;
    case "Enter": if (canvas.tool === "polygon") { e.preventDefault(); canvas.finishPolygon(e.shiftKey ? "add" : e.altKey ? "subtract" : "new"); } break;
    case "Backspace": if (canvas.tool === "polygon") { e.preventDefault(); canvas.undoPolygonPoint(); } break;
    case "Escape": closePopover(); canvas.cancelPolygon(); segmentation.clearPrompts(); clearSelection(); break;
    case "Delete": if (state.selectedIds.size) deleteSelected(); break;
    case "PageUp": if (nav.previous) window.location.href = `/projects/${projectId}/datasets/${nav.previous}`; break;
    case "PageDown": if (nav.next) window.location.href = `/projects/${projectId}/datasets/${nav.next}`; break;
    default: return;
  }
});
document.addEventListener("keyup", (e) => { if (e.key === " ") canvas.spaceHeld = false; });

// ---- imported regions ------------------------------------------------
function selectImported() {
  const params = new URLSearchParams(window.location.search);
  const ids = (params.get("select") || "").split(",").filter(Boolean).filter((id) => state.regions.some((r) => r.region_id === id));
  if (!ids.length) return;
  state.selectedIds = new Set(ids);
  state.selectedId = ids[0];
  const classId = Number(params.get("class"));
  if (classId && state.classes.some((c) => c.class_id === classId && c.enabled)) setActiveClass(classId);
  syncSelection();
  // Centre on the union bounding box of the imported regions.
  let x0 = width, y0 = height, x1 = 0, y1 = 0;
  for (const id of ids) {
    const [x, y, w, h] = regionById(id).bbox_xywh;
    x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x + w); y1 = Math.max(y1, y + h);
  }
  if (x1 > x0 && y1 > y0) canvas.centerOn(x0, y0, x1 - x0, y1 - y0);
  canvas.setHighlight(ids);
  const summary = params.get("summary");
  say(summary ? `${summary} — imported regions are selected.` : `${ids.length} imported region(s) selected.`, 8000);
  if (params.get("imported") === "1") {
    // One undo removes the whole import (restore the state without them).
    const before = snapshot();
    before.regions = before.regions.filter((r) => !ids.includes(r.region_id));
    before.selectedIds = [];
    before.selectedId = null;
    const after = snapshot();
    pushHistory({ label: "mask import", undo: () => restore(before, "undo mask import"), redo: () => restore(after, "redo mask import") });
  }
  window.history.replaceState(null, "", window.location.pathname);
}

// ---- bootstrap ------------------------------------------------------------------------
async function bootstrap() {
  await installHelp(document);
  const lock = await setUpProjectLock(projectId, document.getElementById("lock-indicator"), editorInfo);
  state.readOnly = lock.readOnly();
  canvas.setClasses(state.classes);
  regionsPanel.setClasses(state.classes);
  classesPanel.setClasses(state.classes);
  if (state.activeClassId && state.classes.some((c) => c.class_id === state.activeClassId)) classesPanel.setActive(state.activeClassId);
  state.activeClassId = classesPanel.activeId;
  canvas.activeClassId = state.activeClassId;

  const ui = data.ui_state;
  applyOpacity(Math.round((ui.overlay_opacity ?? 0.55) * 100));
  applyBrush(ui.brush_radius || 10);
  canvas.showOverlaps = ui.show_overlaps !== false;
  canvas.showMissing = ui.show_missing === true;
  document.getElementById("show-overlaps").checked = canvas.showOverlaps;
  document.getElementById("show-missing").checked = canvas.showMissing;
  fillGapInput.value = String(ui.fill_gap_px ?? 1);
  fillConnectivityInput.value = String(ui.fill_connectivity === 8 ? 8 : 4);
  const preferred = ui.channel || ui.modality;
  state.channel = channelIndex(preferred) >= 0 ? preferred : (state.channels[0] ? state.channels[0].key : "topography");
  setChannel(state.channel);
  setTool(ui.tool && document.querySelector(`.tool-button[data-tool="${ui.tool}"]`) ? ui.tool : "select");
  if (ui.last_dataset_id === datasetId && ui.zoom) canvas.setViewport(ui.zoom, ui.pan_x || 0, ui.pan_y || 0); else canvas.fit();
  updateZoomSlider();
  syncViewControls();

  await Promise.all([loadImages(), loadRegions(), applyDatasetFilterNavigation()]);
  if (state.selectedId && regionById(state.selectedId)) select(state.selectedId); else clearSelection();
  selectImported();
  await segmentation.init(ui.selected_backend_id);
  updateHistoryButtons();
  applyHelp(document);
  document.getElementById("export-button").addEventListener("click", () => { window.location.href = `/projects/${projectId}#exports`; });
  // Test hooks (read-only): the Playwright suite inspects rendered pixels
  // and view state through these; nothing here mutates annotation data.
  window.__afmlabelWorkspace = {
    samplePixel: (x, y) => canvas.samplePixel(x, y),
    view: () => canvas.currentView(),
    revision: () => state.datasetRevision,
    selectedIds: () => Array.from(state.selectedIds),
    selectedId: () => state.selectedId,
    activeClassId: () => state.activeClassId,
    undoDepth: () => undoStack.length,
    channel: () => state.channel,
    channelCount: () => channelCount,
    fillPreviewArea: () => (canvas.fillPreview ? maskArea(canvas.fillPreview) : 0),
  };
  body.dataset.ready = "1";
}
bootstrap().catch((err) => say(`workspace failed to load: ${err.message}`, 0));
