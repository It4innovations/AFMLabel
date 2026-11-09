// SPDX-License-Identifier: MIT
// Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
// Dataset grid with search/filter, multi-channel
// image import dialog and source replacement. Uses
// XMLHttpRequest for uploads so progress is visible.
//
// Multi-channel upgrade: the project defines 1..64 named channels. The
// import dialog shows one file picker per channel plus an "all channels at
// once" picker that sorts files by the channels' filename tokens; the
// pairing preview has one column per channel.

import { api } from "./api.js";
import { editorId } from "./session.js";
import { setUpLegacyImport } from "./legacy-import.js";
import { applyHelp } from "./help.js";
import { datasetMatches, debounce, readDatasetFilter, writeDatasetFilter } from "./dataset-filter.js";

let legacy = null;

let projectId = null;
let getRevision = () => 0;
let setRevision = () => {};
let allDatasets = [];
let channels = []; // [{key, label, filename_tokens}] in project order

const ACCEPT = ".tif,.tiff,.png,.jpg,.jpeg";
const MAX_CARD_THUMBS = 4;
const MAX_CARD_FILENAMES = 3;

function upload(path, formData, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", path);
    xhr.setRequestHeader("X-Requested-With", "AFMLabel");
    xhr.setRequestHeader("X-AFMLabel-Editor", editorId());
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable && onProgress) onProgress(event.loaded / event.total);
    });
    xhr.addEventListener("load", () => {
      let payload = null;
      try { payload = JSON.parse(xhr.responseText); } catch (error) { payload = null; }
      if (xhr.status >= 200 && xhr.status < 300) resolve(payload);
      else {
        const err = new Error((payload && payload.error && payload.error.message) || `HTTP ${xhr.status}`);
        err.code = payload && payload.error ? payload.error.code : "UNKNOWN";
        err.recovery = payload && payload.error ? payload.error.recovery : null;
        reject(err);
      }
    });
    xhr.addEventListener("error", () => reject(new Error("network error during upload")));
    xhr.send(formData);
  });
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

export function channelLabel(key) {
  const found = channels.find((c) => c.key === key);
  return found ? found.label : key;
}

// ---- dataset grid ---------------------------------------------------------
function datasetCard(ds) {
  const card = document.createElement("article");
  card.className = "lv-dataset-card";
  card.dataset.datasetId = ds.dataset_id;
  const base = `/api/v1/projects/${projectId}/datasets/${ds.dataset_id}`;
  const status = ds.status === "blocked" ? `<span class="lv-status lv-status-failed" data-help="dataset.status_blocked" data-help-note="${escapeHtml(ds.block_reason || "")}" tabindex="0">blocked</span>` : "";
  const validated = ds.validated_revision === ds.revision ? '<span class="lv-status lv-status-completed" data-help="dataset.status_validated" tabindex="0">validated</span>' : "";
  const sources = ds.sources || [];
  const shownThumbs = sources.slice(0, MAX_CARD_THUMBS);
  const moreThumbs = sources.length - shownThumbs.length;
  const thumbs = shownThumbs.map((s) => `<img src="${base}/thumbnail/${encodeURIComponent(s.channel)}" alt="${escapeHtml(channelLabel(s.channel))} thumbnail" title="${escapeHtml(channelLabel(s.channel))}" loading="lazy">`).join("")
    + (moreThumbs > 0 ? `<span class="lv-dataset-more-thumbs" title="${escapeHtml(sources.slice(MAX_CARD_THUMBS).map((s) => channelLabel(s.channel)).join(", "))}">+${moreThumbs}</span>` : "");
  const shownNames = sources.slice(0, MAX_CARD_FILENAMES);
  const names = shownNames.map((s) => `<span title="${escapeHtml(channelLabel(s.channel))}">${escapeHtml(s.original_filename)}</span>`).join("<br>")
    + (sources.length > MAX_CARD_FILENAMES ? `<br><span class="lv-dataset-more-files">+${sources.length - MAX_CARD_FILENAMES} more file(s)</span>` : "");
  const columns = sources.length <= 1 ? 1 : 2;
  card.innerHTML = `
    <div class="lv-dataset-thumbs" style="grid-template-columns: repeat(${columns}, 1fr)">${thumbs}</div>
    <div class="lv-dataset-body">
      <h3 class="lv-dataset-title">${escapeHtml(ds.name)} ${status} ${validated}</h3>
      <p class="lv-dataset-meta">${ds.width}×${ds.height} · ${sources.length} channel${sources.length === 1 ? "" : "s"} · ${ds.region_count} region${ds.region_count === 1 ? "" : "s"} · r${ds.revision}</p>
      <p class="lv-dataset-meta lv-mono">${names}</p>
      <div class="lv-dataset-actions">
        <a class="lv-btn lv-btn-small lv-open-button" href="/projects/${projectId}/datasets/${ds.dataset_id}" data-help="dataset.annotate">Annotate</a>
        <button type="button" class="lv-btn lv-btn-small" data-action="replace" data-help="dataset.replace_source">Replace source</button>
        <button type="button" class="lv-btn lv-btn-small" data-action="lossy" data-help="dataset.import_mask">Import mask</button>
        <button type="button" class="lv-btn lv-btn-small lv-btn-danger" data-action="delete" data-help="dataset.delete">Delete</button>
      </div>
    </div>`;
  card.querySelector('[data-action="delete"]').addEventListener("click", () => deleteDataset(ds));
  card.querySelector('[data-action="replace"]').addEventListener("click", () => openReplace(ds));
  card.querySelector('[data-action="lossy"]').addEventListener("click", () => { if (legacy) legacy.openLossy(ds); });
  return card;
}

// Client-side filter on already-loaded metadata: no server round-trip and
// no image decode. The filter text persists per project in sessionStorage
// so returning from a dataset keeps it; the workspace reads the same key.
function applyDatasetFilter() {
  const input = document.getElementById("dataset-search");
  const query = input.value;
  writeDatasetFilter(projectId, query);
  let shown = 0;
  for (const card of document.querySelectorAll("#dataset-list .lv-dataset-card")) {
    const ds = allDatasets.find((d) => d.dataset_id === card.dataset.datasetId);
    const on = ds ? datasetMatches(ds, query) : true;
    card.hidden = !on;
    if (on) shown += 1;
  }
  const count = document.getElementById("dataset-filter-count");
  const clear = document.getElementById("dataset-search-clear");
  const noResults = document.getElementById("dataset-no-results");
  if (query.trim()) {
    count.textContent = `${shown} of ${allDatasets.length} match`;
    clear.hidden = false;
    noResults.hidden = shown > 0 || allDatasets.length === 0;
  } else {
    count.textContent = allDatasets.length ? `${allDatasets.length} dataset(s)` : "";
    clear.hidden = true;
    noResults.hidden = true;
  }
}

export async function refreshDatasets() {
  const list = document.getElementById("dataset-list");
  const empty = document.getElementById("dataset-empty");
  allDatasets = await api(`/api/v1/projects/${projectId}/datasets`);
  for (const card of list.querySelectorAll(".lv-dataset-card")) card.remove();
  empty.hidden = allDatasets.length > 0;
  list.append(...allDatasets.map(datasetCard));
  document.getElementById("dataset-count").textContent = String(allDatasets.length);
  applyDatasetFilter();
  applyHelp(list);
  document.dispatchEvent(new CustomEvent("afmlabel:datasets", { detail: { count: allDatasets.length } }));
  return allDatasets;
}

async function deleteDataset(ds) {
  if (!window.confirm(`Delete dataset "${ds.name}" and its annotations? (moved to the workspace trash)`)) return;
  try {
    await api(`/api/v1/projects/${projectId}/datasets/${ds.dataset_id}?expected_revision=${ds.revision}`, { method: "DELETE" });
    const detail = await api(`/api/v1/projects/${projectId}`);
    setRevision(detail.project.revision);
    await refreshDatasets();
  } catch (err) {
    window.alert(err.message);
  }
}

// ---- import dialog --------------------------------------------------------
const modal = document.getElementById("import-pairs-modal");
const channelInputs = document.getElementById("import-channel-inputs");
const autoInput = document.getElementById("import-auto-files");
const autoNote = document.getElementById("import-auto-note");
const uploadButton = document.getElementById("import-upload-button");
const commitButton = document.getElementById("import-commit-button");
const orderCheckbox = document.getElementById("import-allow-order");
const orderRow = document.getElementById("import-order-row");
const progress = document.getElementById("import-progress");
const progressLabel = document.getElementById("import-progress-label");
const progressContainer = document.getElementById("import-progress-container");
const previewHead = document.getElementById("pairing-preview-head");
const previewBody = document.getElementById("pairing-preview-body");
const previewSummary = document.getElementById("pairing-summary");
const importError = document.getElementById("import-error");
let batchId = null;

function renderChannelInputs() {
  channelInputs.replaceChildren(...channels.map((c) => {
    const field = document.createElement("div");
    field.className = "lv-field lv-file";
    field.innerHTML = `<label class="lv-label" for="import-files-${escapeHtml(c.key)}">${escapeHtml(c.label)} files</label>
      <input type="file" id="import-files-${escapeHtml(c.key)}" data-channel="${escapeHtml(c.key)}" multiple accept="${ACCEPT}" data-help="import.channel_files" />`;
    return field;
  }));
  channelInputs.classList.toggle("lv-channel-file-inputs-many", channels.length > 4);
  const tokens = channels.map((c) => `${c.label}: ${c.filename_tokens.length ? c.filename_tokens.join(", ") : "(no tokens)"}`).join(" · ");
  autoNote.textContent = channels.length > 1
    ? `Files are sorted into channels by these filename tokens — ${tokens}.`
    : "Single-channel project: every selected file becomes one dataset.";
  previewHead.innerHTML = `<tr><th>Dataset</th>${channels.map((c) => `<th>${escapeHtml(c.label)}</th>`).join("")}<th>Status <button type="button" class="lv-help-more" data-help-more="import.pair_status" aria-label="More help: pair status">?</button></th></tr>`;
  applyHelp(channelInputs);
}

function resetImportDialog() {
  batchId = null;
  for (const input of channelInputs.querySelectorAll("input[type=file]")) input.value = "";
  autoInput.value = "";
  previewBody.replaceChildren();
  previewSummary.textContent = "";
  importError.textContent = "";
  progressContainer.hidden = true;
  orderRow.hidden = true;
  orderCheckbox.checked = false;
  commitButton.disabled = true;
  uploadButton.disabled = false;
}

async function openImport() {
  renderChannelInputs();
  resetImportDialog();
  modal.hidden = false;
  uploadButton.focus();
}

async function closeImport() {
  modal.hidden = true;
  if (batchId) {
    api(`/api/v1/projects/${projectId}/imports/pairs/batches/${batchId}`, { method: "DELETE" }).catch(() => {});
    batchId = null;
  }
}

function statusLabel(row) {
  return {
    ok: "OK",
    missing: "Missing channel",
    ambiguous: "Ambiguous",
    invalid: "Invalid file",
    dimension_mismatch: "Dimension mismatch",
    calibration_mismatch: "Calibration mismatch",
  }[row.status] || row.status;
}

function fileCell(file) {
  if (!file) return "—";
  const dims = file.width ? ` (${file.width}×${file.height}, ${file.dtype})` : "";
  return `${escapeHtml(file.original_filename)}${dims}`;
}

function renderPreview(preview) {
  previewBody.replaceChildren();
  const keys = preview.channel_keys && preview.channel_keys.length ? preview.channel_keys : channels.map((c) => c.key);
  for (const row of preview.rows) {
    const tr = document.createElement("tr");
    const cls = `lv-pair-${row.status === "ok" ? "ok" : row.status === "missing" ? "missing" : row.status}`;
    const candidates = row.candidates.length ? `<br><small>${row.candidates.map((c) => `${escapeHtml(channelLabel(c.channel))}: ${escapeHtml(c.original_filename)}`).join(", ")}</small>` : "";
    tr.innerHTML = `
      <td class="lv-mono">${escapeHtml(row.pairing_key)}</td>
      ${keys.map((key) => `<td class="${row.missing_channels && row.missing_channels.includes(key) ? "lv-pair-missing" : ""}">${fileCell(row.files ? row.files[key] : null)}</td>`).join("")}
      <td class="${cls}" title="${escapeHtml(row.message)}">${statusLabel(row)}${candidates}${row.message ? `<br><small>${escapeHtml(row.message)}</small>` : ""}</td>`;
    previewBody.append(tr);
  }
  const method = preview.method === "order" ? " — order-based pairing (confirm below)" : "";
  previewSummary.textContent = `${preview.ok_count} dataset(s) ready, ${preview.problem_count} problem(s)${method}`;
  orderRow.hidden = !(preview.order_based_fallback_available || preview.method === "order");
  commitButton.disabled = preview.ok_count === 0 || (preview.method === "order" && !orderCheckbox.checked);
  commitButton.textContent = preview.problem_count > 0
    ? `Create ${preview.ok_count} dataset(s), skip ${preview.problem_count} problem row(s)`
    : `Create ${preview.ok_count} dataset(s)`;
  return preview;
}

// Mirrors imports/pairing.py::extract_pairing_key token matching so the
// "all channels at once" picker sorts files the same way the server keys them.
function detectChannel(filename) {
  const stem = filename.replace(/\.[^.]+$/, "").toLowerCase();
  const parts = stem.split(/[\s_\-.]+/).filter(Boolean);
  const hits = new Set();
  for (const c of channels) {
    for (const token of c.filename_tokens) {
      for (const part of parts) {
        if (part === token || (part.startsWith(token) && /^\d+$/.test(part.slice(token.length))) || (part.endsWith(token) && /^\d+$/.test(part.slice(0, -token.length)))) {
          hits.add(c.key);
        }
      }
    }
  }
  return hits.size === 1 ? Array.from(hits)[0] : null;
}

function collectFiles() {
  const groups = new Map(channels.map((c) => [c.key, []]));
  for (const input of channelInputs.querySelectorAll("input[type=file]")) {
    for (const file of input.files) groups.get(input.dataset.channel).push(file);
  }
  const unassigned = [];
  for (const file of autoInput.files) {
    const key = channels.length === 1 ? channels[0].key : detectChannel(file.name);
    if (key) groups.get(key).push(file); else unassigned.push(file.name);
  }
  return { groups, unassigned };
}

async function uploadFiles() {
  importError.textContent = "";
  const { groups, unassigned } = collectFiles();
  const total = Array.from(groups.values()).reduce((n, files) => n + files.length, 0);
  if (!total) {
    importError.textContent = channels.length === 1 ? "Select image files first." : "Select files for the channels first.";
    return;
  }
  if (unassigned.length) {
    importError.textContent = `${unassigned.length} file(s) match no channel token and were skipped: ${unassigned.slice(0, 5).join(", ")}${unassigned.length > 5 ? ", …" : ""}. Use the per-channel pickers for them.`;
  }
  uploadButton.disabled = true;
  progressContainer.hidden = false;
  try {
    if (!batchId) {
      const created = await api(`/api/v1/projects/${projectId}/imports/pairs/batches`, { method: "POST" });
      batchId = created.batch_id;
    }
    let preview = null;
    for (const [key, files] of groups) {
      if (!files.length) continue;
      const form = new FormData();
      for (const file of files) form.append("files", file, file.name);
      const label = channelLabel(key);
      progressLabel.textContent = `${label}: 0%`;
      preview = await upload(
        `/api/v1/projects/${projectId}/imports/pairs/batches/${batchId}/files?channel=${encodeURIComponent(key)}`,
        form,
        (fraction) => { progress.value = Math.round(fraction * 100); progressLabel.textContent = `${label}: ${Math.round(fraction * 100)}%`; },
      );
    }
    progressLabel.textContent = "uploaded";
    for (const input of channelInputs.querySelectorAll("input[type=file]")) input.value = "";
    autoInput.value = "";
    renderPreview(preview || await api(`/api/v1/projects/${projectId}/imports/pairs/batches/${batchId}`));
  } catch (err) {
    importError.textContent = err.recovery ? `${err.message}. ${err.recovery}.` : err.message;
  } finally {
    uploadButton.disabled = false;
  }
}

async function refreshPreview() {
  if (!batchId) return;
  const allow = orderCheckbox.checked ? "?allow_order_based=true" : "";
  renderPreview(await api(`/api/v1/projects/${projectId}/imports/pairs/batches/${batchId}${allow}`));
}

async function commit() {
  importError.textContent = "";
  commitButton.disabled = true;
  try {
    const allow = orderCheckbox.checked ? "?allow_order_based=true" : "";
    const preview = await api(`/api/v1/projects/${projectId}/imports/pairs/batches/${batchId}${allow}`);
    const pairs = preview.rows
      .filter((row) => row.status === "ok")
      .map((row) => ({
        pairing_key: row.pairing_key,
        files: Object.fromEntries(Object.entries(row.files).filter(([, f]) => f).map(([key, f]) => [key, f.file_id])),
      }));
    const result = await api(`/api/v1/projects/${projectId}/imports/pairs/batches/${batchId}/commit`, {
      method: "POST",
      body: { expected_revision: getRevision(), pairs, confirm_order_based: orderCheckbox.checked },
    });
    batchId = null;
    setRevision(result.project_revision);
    modal.hidden = true;
    await refreshDatasets();
  } catch (err) {
    importError.textContent = err.recovery ? `${err.message}. ${err.recovery}.` : err.message;
    commitButton.disabled = false;
  }
}

// ---- source replacement -----------------------------------------------------
const replaceModal = document.getElementById("replace-source-modal");
const replaceFile = document.getElementById("replace-source-file");
const replaceChannel = document.getElementById("replace-source-channel");
const replaceReport = document.getElementById("replace-source-report");
const replaceError = document.getElementById("replace-source-error");
const replaceConfirm = document.getElementById("replace-source-confirm-button");
let replaceTarget = null;
let replacementId = null;

function openReplace(ds) {
  replaceTarget = ds;
  replacementId = null;
  replaceFile.value = "";
  replaceReport.textContent = "";
  replaceError.textContent = "";
  replaceConfirm.disabled = true;
  replaceChannel.replaceChildren(...(ds.sources || []).map((s) => Object.assign(document.createElement("option"), { value: s.channel, textContent: `${channelLabel(s.channel)} (${s.original_filename})` })));
  document.getElementById("replace-source-dataset").textContent = ds.name;
  replaceModal.hidden = false;
}

async function checkReplacement() {
  replaceError.textContent = "";
  replaceReport.textContent = "";
  replaceConfirm.disabled = true;
  if (!replaceFile.files.length) { replaceError.textContent = "Choose a replacement file."; return; }
  const form = new FormData();
  form.append("file", replaceFile.files[0], replaceFile.files[0].name);
  try {
    const report = await upload(`/api/v1/projects/${projectId}/datasets/${replaceTarget.dataset_id}/replace-source/${encodeURIComponent(replaceChannel.value)}`, form);
    replacementId = report.replacement_id;
    const lines = [];
    lines.push(report.compatible ? "Compatible: existing annotations are preserved." : `Incompatible: ${report.reason}`);
    lines.push(`Current: ${report.current.original_filename} ${report.current.width}×${report.current.height} ${report.current.dtype} sha256 ${report.current.sha256.slice(0, 12)}…`);
    lines.push(`Candidate: ${report.candidate.original_filename} ${report.candidate.width}×${report.candidate.height} ${report.candidate.dtype} sha256 ${report.candidate.sha256.slice(0, 12)}…`);
    for (const warning of report.warnings) lines.push(`Warning: ${warning}`);
    lines.push("Confirming increments the dataset revision and marks previous validation stale.");
    replaceReport.textContent = lines.join("\n");
    replaceConfirm.disabled = !report.compatible;
  } catch (err) {
    replaceError.textContent = err.message;
  }
}

async function confirmReplacement() {
  replaceError.textContent = "";
  try {
    await api(`/api/v1/projects/${projectId}/datasets/${replaceTarget.dataset_id}/replace-source/${encodeURIComponent(replaceChannel.value)}/confirm`, {
      method: "POST",
      body: { replacement_id: replacementId, expected_revision: replaceTarget.revision },
    });
    replaceModal.hidden = true;
    await refreshDatasets();
  } catch (err) {
    replaceError.textContent = err.recovery ? `${err.message}. ${err.recovery}.` : err.message;
  }
}

// ---- wiring ---------------------------------------------------------------
export function setChannels(list) {
  channels = (list || []).map((c) => ({ key: c.key, label: c.label, filename_tokens: c.filename_tokens || [] }));
  if (!modal.hidden) renderChannelInputs();
}

export function setUpDatasets(detail, revisionAccessors) {
  projectId = detail.project.project_id;
  getRevision = revisionAccessors.get;
  setRevision = revisionAccessors.set;
  setChannels(detail.project.channels);

  document.getElementById("import-pairs-button").addEventListener("click", openImport);
  document.getElementById("close-import-modal-button").addEventListener("click", closeImport);
  document.getElementById("cancel-import-modal-button").addEventListener("click", closeImport);
  uploadButton.addEventListener("click", uploadFiles);
  commitButton.addEventListener("click", commit);
  orderCheckbox.addEventListener("change", refreshPreview);

  document.getElementById("close-replace-modal-button").addEventListener("click", () => { replaceModal.hidden = true; });
  document.getElementById("cancel-replace-modal-button").addEventListener("click", () => { replaceModal.hidden = true; });
  document.getElementById("replace-source-check-button").addEventListener("click", checkReplacement);
  replaceConfirm.addEventListener("click", confirmReplacement);

  const search = document.getElementById("dataset-search");
  search.value = readDatasetFilter(projectId);
  search.addEventListener("input", debounce(applyDatasetFilter, 120));
  search.addEventListener("keydown", (e) => { if (e.key === "Escape") { search.value = ""; applyDatasetFilter(); } });
  document.getElementById("dataset-search-clear").addEventListener("click", () => { search.value = ""; applyDatasetFilter(); search.focus(); });

  legacy = setUpLegacyImport({
    projectId,
    getRevision,
    setRevision,
    refreshDatasets,
    reloadProject: revisionAccessors.reloadProject || (async () => {}),
  });

  refreshDatasets().catch((err) => { document.getElementById("class-error").textContent = err.message; });
}
