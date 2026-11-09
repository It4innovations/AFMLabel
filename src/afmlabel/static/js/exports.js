// SPDX-License-Identifier: MIT
// Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
// Export dialog on the project page: profile (NanoUNet preselected),
// options (mask values, Boundary policy, PNG/TIFF, colour visualisation),
// searchable dataset selection with validation state, filename preview,
// run, history with download links.

import { api } from "./api.js";
import { applyHelp } from "./help.js";
import { datasetMatches, debounce } from "./dataset-filter.js";

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Mirrors exports/rasters.py::safe_stem / export_stems for the preview.
function safeStem(value) {
  const stem = String(value || "").trim().replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^[._]+|[._]+$/g, "");
  return (stem || "dataset").slice(0, 120);
}
function firstDigitRun(stem) {
  const match = stem.match(/\d+/);
  return match ? match[0] : stem;
}
export function previewStems(pairingKeys, loaderIdMatching) {
  const seen = new Map();
  let stems = pairingKeys.map((key) => {
    const stem = safeStem(key);
    const count = seen.get(stem) || 0;
    seen.set(stem, count + 1);
    return count === 0 ? stem : `${stem}~${count + 1}`;
  });
  if (loaderIdMatching) {
    const ids = stems.map(firstDigitRun);
    if (new Set(ids).size !== ids.length) {
      const width = Math.max(3, String(stems.length).length);
      stems = stems.map((stem, index) => `${String(index + 1).padStart(width, "0")}_${stem}`);
    }
  }
  return stems;
}

// Mirrors exports/nanounet.py::channel_folder for the filename preview.
export function channelFolder(key, label) {
  if (key === "topography") return "Topography";
  if (key === "friction") return "Friction";
  const cleaned = String(label || key).trim().replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
  return cleaned || key;
}

function sourceNames(ds) {
  return (ds.sources || []).map((s) => s.original_filename);
}

export function setUpExports({ projectId }) {
  const modal = document.getElementById("export-modal");
  const profileSelect = document.getElementById("export-profile");
  const semantics = document.getElementById("export-mask-semantics");
  const boundary = document.getElementById("export-boundary-policy");
  const rasterFormat = document.getElementById("export-raster-format");
  const colorViz = document.getElementById("export-color-visualization");
  const datasetList = document.getElementById("export-dataset-list");
  const search = document.getElementById("export-search");
  const selectAll = document.getElementById("export-select-all");
  const filterCount = document.getElementById("export-filter-count");
  const preview = document.getElementById("export-filename-preview");
  const runButton = document.getElementById("export-run-button");
  const status = document.getElementById("export-status");
  const history = document.getElementById("export-history");
  const error = document.getElementById("export-error");
  let datasets = [];
  let channelLabels = {};

  async function loadChannels() {
    try {
      const detail = await api(`/api/v1/projects/${projectId}`);
      channelLabels = Object.fromEntries((detail.project.channels || []).map((c) => [c.key, c.label]));
    } catch (err) { channelLabels = {}; }
  }

  async function loadProfiles() {
    const profiles = await api(`/api/v1/projects/${projectId}/exports/profiles`);
    profileSelect.replaceChildren(...profiles.map((p) => Object.assign(document.createElement("option"), { value: p.profile_id, textContent: p.is_default ? `${p.display_name} — default` : p.display_name })));
    const preset = profiles.find((p) => p.is_default);
    profileSelect.value = preset ? preset.profile_id : profiles[0].profile_id;
  }

  function rowMatches(ds, query) {
    const q = (query || "").trim().toLowerCase();
    if (!q) return true;
    const statusText = ds.validated_revision === ds.revision ? "validated" : "not validated stale";
    return datasetMatches(ds, q) || statusText.includes(q);
  }

  function applyFilter() {
    const q = search.value;
    let shown = 0;
    for (const li of datasetList.querySelectorAll("li[data-dataset-id]")) {
      const ds = datasets.find((d) => d.dataset_id === li.dataset.datasetId);
      const on = ds ? rowMatches(ds, q) : true;
      li.hidden = !on;
      if (on) shown += 1;
    }
    filterCount.textContent = q.trim() ? `${shown} of ${datasets.length} shown` : `${datasets.length} dataset(s)`;
    updatePreview();
  }

  function selectedIds() {
    return Array.from(datasetList.querySelectorAll("li:not([hidden]) input:checked")).map((i) => i.value);
  }

  function updatePreview() {
    const ids = selectedIds();
    const chosen = ids.map((id) => datasets.find((d) => d.dataset_id === id)).filter(Boolean);
    if (!chosen.length) { preview.hidden = true; return; }
    const loader = profileSelect.value === "nanounet";
    const stems = previewStems(chosen.map((d) => d.pairing_key), loader);
    const suffix = rasterFormat.value === "png" ? ".png" : ".tif";
    const lines = chosen.slice(0, 8).map((d, i) => {
      const inputs = sourceNames(d).join(" + ");
      if (loader) {
        const folders = (d.sources || []).map((s) => `${channelFolder(s.channel, channelLabels[s.channel])}/${stems[i]}.png`).join(", ");
        return `${inputs}  →  ${folders}, Masks_instance/${stems[i]}${suffix}${colorViz.checked ? `, Masks_color/${stems[i]}.png` : ""}`;
      }
      return `${inputs}  →  datasets/${stems[i]}/final_mask${suffix}${colorViz.checked ? ", mask_color.png" : ""}, …`;
    });
    if (chosen.length > 8) lines.push(`… and ${chosen.length - 8} more`);
    const prefixed = loader && stems.some((s, i) => s !== safeStem(chosen[i].pairing_key) && !s.includes("~"));
    if (prefixed) lines.push("Note: the loader matches files by the first digit run of the name; two keys collided, so a sequence prefix was added to keep every sample distinct (recorded in the manifest).");
    preview.textContent = `Filename preview (from the original filenames):\n${lines.join("\n")}`;
    preview.hidden = false;
  }

  async function loadDatasets() {
    datasets = await api(`/api/v1/projects/${projectId}/datasets`);
    datasetList.replaceChildren(...datasets.map((ds) => {
      const li = document.createElement("li");
      li.dataset.datasetId = ds.dataset_id;
      const valid = ds.validated_revision === ds.revision;
      li.innerHTML = `<label><input type="checkbox" value="${ds.dataset_id}" aria-label="Export ${escapeHtml(ds.name)}" ${valid ? "checked" : ""}> ${escapeHtml(ds.name)} <span class="lv-mono">${escapeHtml(sourceNames(ds)[0] || "")}</span> <span class="lv-status ${valid ? "lv-status-completed" : "lv-status-pending"}">${valid ? `validated r${ds.revision}` : "not validated at current revision"}</span></label>`;
      li.querySelector("input").addEventListener("change", updatePreview);
      return li;
    }));
    if (!datasets.length) datasetList.innerHTML = '<li class="lv-export-empty">No datasets.</li>';
    applyFilter();
  }

  async function loadHistory() {
    const records = await api(`/api/v1/projects/${projectId}/exports`);
    if (!records.length) { history.innerHTML = '<li class="lv-export-empty">No exports yet.</li>'; return; }
    history.replaceChildren(...records.map((r) => {
      const li = document.createElement("li");
      const skipped = r.skipped.length ? `, ${r.skipped.length} skipped (${r.skipped.map((s) => `${escapeHtml(s.dataset_name)}: ${s.code}`).join("; ")})` : "";
      const files = r.datasets.length ? ` · e.g. ${escapeHtml(Object.values(r.datasets[0].files).filter((f) => /mask/i.test(f)).join(", "))}` : "";
      li.innerHTML = `<div class="lv-export-title">${escapeHtml(r.profile_id)} · ${r.created_at.slice(0, 19).replace("T", " ")}</div>
        <div class="lv-export-meta">${r.datasets.length} dataset(s)${skipped} · ${r.options.mask_semantics} masks (${r.options.raster_format || "tiff"}), boundary ${r.options.boundary_policy}${r.options.color_visualization === false ? "" : ", colour visualisation"} · ${((r.package_size_bytes || 0) / 1024).toFixed(0)} KiB · sha256 ${(r.package_sha256 || "").slice(0, 12)}…${files}</div>
        <div class="lv-export-actions"><a class="lv-btn" href="/api/v1/projects/${projectId}/exports/${r.export_id}/download" data-help="export.download">Download</a><button type="button" class="lv-btn lv-btn-danger" data-delete="${r.export_id}" data-help="export.delete">Delete</button></div>`;
      li.querySelector("[data-delete]").addEventListener("click", async () => {
        if (!window.confirm("Delete this export package? The annotation is not affected.")) return;
        await api(`/api/v1/projects/${projectId}/exports/${r.export_id}`, { method: "DELETE" });
        await loadHistory();
      });
      return li;
    }));
    applyHelp(history);
  }

  async function open() {
    error.textContent = "";
    status.textContent = "";
    modal.hidden = false;
    try {
      await Promise.all([loadChannels(), loadProfiles(), loadDatasets(), loadHistory()]);
      updatePreview();
      applyHelp(modal);
    } catch (err) {
      error.textContent = err.message;
    }
  }

  runButton.addEventListener("click", async () => {
    error.textContent = "";
    const ids = selectedIds();
    if (!ids.length) { error.textContent = "Select at least one validated dataset."; return; }
    runButton.disabled = true;
    status.textContent = "Exporting…";
    try {
      const record = await api(`/api/v1/projects/${projectId}/exports`, {
        method: "POST",
        body: {
          profile_id: profileSelect.value,
          dataset_ids: ids,
          options: { mask_semantics: semantics.value, boundary_policy: boundary.value, raster_format: rasterFormat.value, color_visualization: colorViz.checked },
        },
      });
      status.textContent = `Export ${record.export_id}: ${record.datasets.length} dataset(s) exported, ${record.skipped.length} skipped.`;
      await loadHistory();
    } catch (err) {
      const skipped = err.details && err.details.skipped ? ` ${err.details.skipped.map((s) => `${s.dataset_name}: ${s.reason}`).join("; ")}` : "";
      error.textContent = `${err.message}.${err.recovery ? ` ${err.recovery}.` : ""}${skipped}`;
      status.textContent = "";
    } finally {
      runButton.disabled = false;
    }
  });

  search.addEventListener("input", debounce(applyFilter, 120));
  selectAll.addEventListener("click", () => {
    for (const li of datasetList.querySelectorAll("li[data-dataset-id]:not([hidden])")) {
      const ds = datasets.find((d) => d.dataset_id === li.dataset.datasetId);
      li.querySelector("input").checked = Boolean(ds && ds.validated_revision === ds.revision);
    }
    updatePreview();
  });
  for (const el of [profileSelect, rasterFormat, colorViz]) el.addEventListener("change", updatePreview);
  document.getElementById("open-export-button").addEventListener("click", open);
  document.getElementById("close-export-modal-button").addEventListener("click", () => { modal.hidden = true; });
  document.getElementById("cancel-export-modal-button").addEventListener("click", () => { modal.hidden = true; });
  if (window.location.hash === "#exports") open();
}
