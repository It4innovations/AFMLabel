// SPDX-License-Identifier: MIT
// Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
// Project page: lock lifecycle, classes table, rename, audit log. Dataset
// import and the dataset grid are wired by datasets.js.

import { api } from "./api.js";
import { editorId, setUpLogout } from "./session.js";
import { setChannels, setUpDatasets } from "./datasets.js";
import { setUpExports } from "./exports.js";
import { installHelp, applyHelp } from "./help.js";

setUpLogout();

const body = document.body;
const projectId = body.dataset.projectId;
let revision = Number(body.dataset.projectRevision);
const editor = editorId();

export function currentRevision() {
  return revision;
}
export function setRevision(value) {
  revision = value;
  document.getElementById("project-revision").textContent = String(value);
  body.dataset.projectRevision = String(value);
}

export function projectApi(path, options = {}) {
  const headers = Object.assign({}, options.headers || {}, { "X-AFMLabel-Editor": editor });
  return api(`/api/v1/projects/${projectId}${path}`, Object.assign({}, options, { headers }));
}

// api() builds its own headers; wrap fetch so the editor header is always present.
const originalFetch = window.fetch.bind(window);
window.fetch = (input, init = {}) => {
  const headers = new Headers(init.headers || {});
  headers.set("X-AFMLabel-Editor", editor);
  return originalFetch(input, Object.assign({}, init, { headers }));
};

// ---- lock -----------------------------------------------------------------
const lockIndicator = document.getElementById("lock-indicator");
let readOnly = false;

function renderLock(state) {
  if (!state.locked) {
    lockIndicator.textContent = "lock: free";
  } else if (state.owned_by_caller) {
    lockIndicator.textContent = "lock: held by this tab";
  } else if (state.stale) {
    lockIndicator.textContent = "lock: stale (recoverable)";
  } else {
    lockIndicator.textContent = `lock: held by ${state.info.owner_label} — read-only`;
  }
  lockIndicator.classList.toggle("lv-chip-warn", readOnly);
}

async function acquireLock(force = false) {
  try {
    await projectApi("/lock", { method: "POST", body: { force } });
    readOnly = false;
  } catch (err) {
    if (err.code === "PROJECT_LOCKED") {
      const holder = err.details && err.details.holder ? err.details.holder.owner_label : "another session";
      readOnly = !window.confirm(`${holder} is editing this project. Take over the editing lock? (Cancel opens read-only.)`);
      if (!readOnly) {
        await projectApi("/lock", { method: "POST", body: { force: true } });
      }
    } else {
      throw err;
    }
  }
  renderLock(await projectApi("/lock"));
}

acquireLock().catch((err) => { lockIndicator.textContent = `lock: ${err.message}`; });
setInterval(() => {
  if (!readOnly) projectApi("/lock/heartbeat", { method: "POST" }).catch(() => {});
}, 30000);
window.addEventListener("pagehide", () => {
  if (readOnly) return;
  originalFetch(`/api/v1/projects/${projectId}/lock`, {
    method: "DELETE",
    keepalive: true,
    headers: { "X-Requested-With": "AFMLabel", "X-AFMLabel-Editor": editor },
  }).catch(() => {});
});

// ---- classes --------------------------------------------------------------
const classBody = document.getElementById("class-table-body");
const classError = document.getElementById("class-error");
const classCount = document.getElementById("class-count");

function classRow(cls) {
  const tr = document.createElement("tr");
  tr.dataset.classId = String(cls.class_id);
  tr.innerHTML = `
    <td class="lv-mono">${cls.class_id}</td>
    <td><input type="color" class="lv-color-input" value="${cls.display_color}" data-field="display_color" aria-label="Colour of ${cls.name}" data-help="class.color"></td>
    <td><input type="text" class="lv-input lv-input-inline" value="" data-field="name" maxlength="100" aria-label="Name of class ${cls.class_id}" data-help="class.name"></td>
    <td class="lv-mono">${cls.stable_key}</td>
    <td><select class="lv-select lv-select-inline" data-field="semantic_role" aria-label="Role of ${cls.name}" data-help="class.role">
      ${["MaterialLayer", "Boundary", "Unspecified"].map((r) => `<option value="${r}" ${r === cls.semantic_role ? "selected" : ""}>${r}</option>`).join("")}
    </select></td>
    <td><input type="number" class="lv-input lv-input-inline lv-input-short" value="${cls.export_id === null ? "" : cls.export_id}" placeholder="${cls.class_id}" data-field="export_id" aria-label="Export id of ${cls.name}" data-help="class.export_id"></td>
    <td><input type="checkbox" data-field="enabled" ${cls.enabled ? "checked" : ""} aria-label="${cls.name} enabled" data-help="class.enabled"></td>
    <td><button type="button" class="lv-btn lv-btn-danger lv-btn-small" data-action="delete" data-help="class.delete">Delete</button></td>`;
  tr.querySelector('[data-field="name"]').value = cls.name;
  for (const input of tr.querySelectorAll("[data-field]")) {
    input.addEventListener("change", () => updateClass(cls.class_id, input));
  }
  tr.querySelector('[data-action="delete"]').addEventListener("click", () => deleteClass(cls));
  return tr;
}

function renderClasses(ontology) {
  classBody.replaceChildren(...ontology.classes.map(classRow));
  classCount.textContent = String(ontology.classes.length);
  applyHelp(classBody);
}

async function updateClass(classId, input) {
  classError.textContent = "";
  const field = input.dataset.field;
  const body = { expected_revision: revision };
  if (field === "enabled") body.enabled = input.checked;
  else if (field === "export_id") {
    if (input.value === "") body.clear_export_id = true; else body.export_id = Number(input.value);
  } else body[field] = input.value;
  try {
    const result = await projectApi(`/classes/${classId}`, { method: "PATCH", body });
    setRevision(result.project.revision);
    renderClasses(result.classes);
  } catch (err) {
    classError.textContent = err.recovery ? `${err.message}. ${err.recovery}.` : err.message;
    if (err.code === "REVISION_CONFLICT") await reloadProject();
  }
}

async function deleteClass(cls) {
  if (!window.confirm(`Delete class "${cls.name}" (id ${cls.class_id})?`)) return;
  classError.textContent = "";
  try {
    const result = await projectApi(`/classes/${cls.class_id}?expected_revision=${revision}`, { method: "DELETE" });
    setRevision(result.project.revision);
    renderClasses(result.classes);
  } catch (err) {
    classError.textContent = err.recovery ? `${err.message}. ${err.recovery}.` : err.message;
    if (err.code === "REVISION_CONFLICT") await reloadProject();
  }
}

document.getElementById("class-create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  classError.textContent = "";
  const name = document.getElementById("new-class-name");
  try {
    const result = await projectApi("/classes", {
      method: "POST",
      body: {
        expected_revision: revision,
        name: name.value,
        display_color: document.getElementById("new-class-color").value,
        semantic_role: document.getElementById("new-class-role").value,
      },
    });
    setRevision(result.project.revision);
    renderClasses(result.classes);
    name.value = "";
  } catch (err) {
    classError.textContent = err.recovery ? `${err.message}. ${err.recovery}.` : err.message;
    if (err.code === "REVISION_CONFLICT") await reloadProject();
  }
});

// ---- channels (multi-channel upgrade) --------------------------------------
// Editable only while the project has no datasets; afterwards the table is
// read-only because every dataset must carry exactly these channels.
const channelBody = document.getElementById("channel-table-body");
const channelError = document.getElementById("channel-error");
const channelSaveButton = document.getElementById("channel-save-button");
const channelResetButton = document.getElementById("channel-reset-button");
const channelAddButton = document.getElementById("channel-add-button");
const channelLockNote = document.getElementById("channel-lock-note");
let savedChannels = [];
let draftChannels = [];
let channelsLocked = true;

function slugKey(label, taken) {
  let base = String(label || "").trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 60);
  if (!base || !/^[a-z]/.test(base)) base = base ? `channel_${base}` : "channel";
  let key = base;
  let index = 2;
  while (taken.has(key)) { key = `${base}_${index}`; index += 1; }
  return key;
}

function channelsDirty() {
  return JSON.stringify(draftChannels) !== JSON.stringify(savedChannels);
}

function renderChannels() {
  channelBody.replaceChildren(...draftChannels.map((channel, index) => {
    const tr = document.createElement("tr");
    tr.dataset.index = String(index);
    tr.innerHTML = `
      <td class="lv-mono">${index + 1}</td>
      <td><input type="text" class="lv-input lv-input-inline" data-field="label" maxlength="60" aria-label="Label of channel ${index + 1}" data-help="project.channel_label" ${channelsLocked ? "disabled" : ""}></td>
      <td class="lv-mono"><input type="text" class="lv-input lv-input-inline lv-mono" data-field="key" maxlength="64" pattern="[a-z][a-z0-9_]*" aria-label="Key of channel ${index + 1}" data-help="project.channel_key" ${channelsLocked || channel.persisted ? "disabled" : ""}></td>
      <td><input type="text" class="lv-input lv-input-inline" data-field="tokens" aria-label="Filename tokens of channel ${index + 1}" placeholder="e.g. phase, ph" data-help="project.channel_tokens" ${channelsLocked ? "disabled" : ""}></td>
      <td><button type="button" class="lv-btn lv-btn-danger lv-btn-small" data-action="remove" data-help="project.channel_remove" ${channelsLocked || draftChannels.length <= 1 ? "disabled" : ""}>Remove</button></td>`;
    tr.querySelector('[data-field="label"]').value = channel.label;
    tr.querySelector('[data-field="key"]').value = channel.key;
    tr.querySelector('[data-field="tokens"]').value = channel.filename_tokens.join(", ");
    for (const input of tr.querySelectorAll("input[data-field]")) {
      input.addEventListener("input", () => {
        const field = input.dataset.field;
        if (field === "tokens") channel.filename_tokens = input.value.split(/[,\s]+/).map((t) => t.trim().toLowerCase()).filter(Boolean);
        else if (field === "key") channel.key = input.value.trim().toLowerCase();
        else {
          channel.label = input.value;
          if (!channel.persisted && !channel.keyEdited) {
            channel.key = slugKey(channel.label, new Set(draftChannels.filter((c) => c !== channel).map((c) => c.key)));
            tr.querySelector('[data-field="key"]').value = channel.key;
          }
        }
        if (field === "key") channel.keyEdited = true;
        updateChannelButtons();
      });
    }
    tr.querySelector('[data-action="remove"]').addEventListener("click", () => { draftChannels.splice(index, 1); renderChannels(); });
    return tr;
  }));
  document.getElementById("channel-count").textContent = String(draftChannels.length);
  channelLockNote.hidden = !channelsLocked;
  channelAddButton.disabled = channelsLocked || draftChannels.length >= 64;
  updateChannelButtons();
  applyHelp(channelBody);
}

function updateChannelButtons() {
  const dirty = channelsDirty();
  channelSaveButton.disabled = channelsLocked || !dirty;
  channelResetButton.disabled = channelsLocked || !dirty;
}

function loadChannels(project, locked) {
  channelsLocked = locked;
  savedChannels = project.channels.map((c) => ({ key: c.key, label: c.label, filename_tokens: [...c.filename_tokens], persisted: true }));
  draftChannels = savedChannels.map((c) => ({ ...c, filename_tokens: [...c.filename_tokens] }));
  renderChannels();
}

channelAddButton.addEventListener("click", () => {
  const index = draftChannels.length + 1;
  const taken = new Set(draftChannels.map((c) => c.key));
  draftChannels.push({ key: slugKey(`channel_${index}`, taken), label: `Channel ${index}`, filename_tokens: [`channel${index}`, `ch${index}`, `c${index}`], persisted: false });
  renderChannels();
  const rows = channelBody.querySelectorAll("tr");
  rows[rows.length - 1].querySelector('[data-field="label"]').focus();
});
channelResetButton.addEventListener("click", () => { draftChannels = savedChannels.map((c) => ({ ...c, filename_tokens: [...c.filename_tokens] })); renderChannels(); });
channelSaveButton.addEventListener("click", async () => {
  channelError.textContent = "";
  try {
    const project = await projectApi("", {
      method: "PATCH",
      body: { expected_revision: revision, channels: draftChannels.map((c) => ({ key: c.key, label: c.label, filename_tokens: c.filename_tokens })) },
    });
    setRevision(project.revision);
    loadChannels(project, channelsLocked);
    document.getElementById("project-channel-summary").textContent = `${project.channels.length} channel${project.channels.length === 1 ? "" : "s"}`;
    document.dispatchEvent(new CustomEvent("afmlabel:channels", { detail: { channels: project.channels } }));
  } catch (err) {
    channelError.textContent = err.recovery ? `${err.message}. ${err.recovery}.` : err.message;
    if (err.code === "REVISION_CONFLICT") await reloadProject();
  }
});
document.addEventListener("afmlabel:datasets", (event) => {
  const locked = event.detail.count > 0;
  if (locked !== channelsLocked) { channelsLocked = locked; renderChannels(); }
});

export async function reloadProject() {
  const detail = await projectApi("");
  setRevision(detail.project.revision);
  renderClasses(detail.classes);
  loadChannels(detail.project, detail.project.dataset_order.length > 0);
  document.getElementById("project-title").textContent = detail.project.name;
  renderLock(detail.lock);
  return detail;
}

// ---- rename ---------------------------------------------------------------
const renameModal = document.getElementById("rename-project-modal");
const renameError = document.getElementById("rename-project-error");
document.getElementById("rename-project-button").addEventListener("click", () => { renameModal.hidden = false; });
document.getElementById("close-rename-modal-button").addEventListener("click", () => { renameModal.hidden = true; });
document.getElementById("cancel-rename-modal-button").addEventListener("click", () => { renameModal.hidden = true; });
document.getElementById("rename-project-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  renameError.textContent = "";
  try {
    const project = await projectApi("", {
      method: "PATCH",
      body: { expected_revision: revision, name: document.getElementById("rename-project-name").value },
    });
    setRevision(project.revision);
    document.getElementById("project-title").textContent = project.name;
    renameModal.hidden = true;
  } catch (err) {
    renameError.textContent = err.message;
    if (err.code === "REVISION_CONFLICT") await reloadProject();
  }
});

// ---- events ---------------------------------------------------------------
async function loadEvents() {
  const list = document.getElementById("event-list");
  try {
    const events = await projectApi("/events?limit=100");
    list.replaceChildren(...events.reverse().map((event) => {
      const li = document.createElement("li");
      const rev = event.new_revision !== null ? ` → r${event.new_revision}` : "";
      li.textContent = `${event.timestamp.slice(0, 19).replace("T", " ")}  ${event.operation}${rev}  ${event.actor || ""}`;
      return li;
    }));
  } catch (err) {
    list.replaceChildren(Object.assign(document.createElement("li"), { textContent: err.message }));
  }
}
document.getElementById("refresh-events-button").addEventListener("click", loadEvents);
document.querySelector(".lv-details").addEventListener("toggle", (event) => { if (event.target.open) loadEvents(); });

// ---- initial render ---------------------------------------------------------
installHelp(document)
  .then(() => reloadProject())
  .then((detail) => {
    setUpDatasets(detail, { get: currentRevision, set: setRevision, reloadProject });
    document.addEventListener("afmlabel:channels", (event) => setChannels(event.detail.channels));
    setUpExports({ projectId });
    applyHelp(document);
  })
  .catch((err) => { classError.textContent = err.message; });
