// SPDX-License-Identifier: MIT
// Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
// Legacy import dialogs on the project page: TSWAT/CVAT XML (project-wide,
// with image and label mapping preview) and lossy flattened raster import
// (per dataset, value -> class mapping). Both are two-step: stage/preview,
// then explicit commit.
//
// Flattened-mask upgrade: the proposed class is Buffer (or
// the project default); there is no "Skip" class -- discarding a value is
// an explicit "do not import this value" choice that must be confirmed;
// dimension mismatches are reported with expected/received sizes; after
// the commit the workspace opens with the imported regions selected.

import { api } from "./api.js";
import { editorId } from "./session.js";
import { applyHelp } from "./help.js";

const DO_NOT_IMPORT = "__discard__";
const CHOOSE = "";

function upload(path, formData) {
  return fetch(path, {
    method: "POST",
    body: formData,
    headers: { "X-Requested-With": "AFMLabel", "X-AFMLabel-Editor": editorId() },
  }).then(async (response) => {
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      const error = (payload && payload.error) || {};
      const err = new Error(error.message || `HTTP ${response.status}`);
      err.code = error.code;
      err.recovery = error.recovery;
      err.details = error.details;
      throw err;
    }
    return payload;
  });
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

export function setUpLegacyImport({ projectId, getRevision, setRevision, refreshDatasets, reloadProject }) {
  const modal = document.getElementById("tswat-import-modal");
  const fileInput = document.getElementById("tswat-file");
  const targetSelect = document.getElementById("tswat-target-dataset");
  const stageButton = document.getElementById("tswat-stage-button");
  const commitButton = document.getElementById("tswat-commit-button");
  const imagesBody = document.getElementById("tswat-images-body");
  const labelsBody = document.getElementById("tswat-labels-body");
  const summary = document.getElementById("tswat-summary");
  const error = document.getElementById("tswat-error");
  const resultEl = document.getElementById("tswat-result");
  let stagedId = null;
  let report = null;

  async function fillTargets() {
    const datasets = await api(`/api/v1/projects/${projectId}/datasets`);
    targetSelect.replaceChildren(Object.assign(document.createElement("option"), { value: "", textContent: "match by filename" }));
    for (const ds of datasets) {
      targetSelect.append(Object.assign(document.createElement("option"), { value: ds.dataset_id, textContent: `${ds.name} (${(ds.sources || []).map((s) => s.original_filename).join(", ")})` }));
    }
  }

  function open() {
    stagedId = null;
    report = null;
    fileInput.value = "";
    imagesBody.replaceChildren();
    labelsBody.replaceChildren();
    summary.textContent = "";
    error.textContent = "";
    resultEl.textContent = "";
    commitButton.disabled = true;
    fillTargets().catch((err) => { error.textContent = err.message; });
    modal.hidden = false;
  }

  function close() {
    modal.hidden = true;
    if (stagedId) api(`/api/v1/projects/${projectId}/imports/tswat/${stagedId}`, { method: "DELETE" }).catch(() => {});
    stagedId = null;
  }

  function currentMapping() {
    const mapping = {};
    for (const select of labelsBody.querySelectorAll("select[data-label]")) mapping[select.dataset.label] = select.value;
    return mapping;
  }

  function render(r) {
    report = r;
    stagedId = r.staged_id;
    imagesBody.replaceChildren(...r.images.map((img) => {
      const tr = document.createElement("tr");
      const cls = img.status === "matched" ? "lv-pair-ok" : img.status === "no_shapes" ? "" : "lv-pair-missing";
      tr.innerHTML = `<td class="lv-mono">${escapeHtml(img.image_name)}</td><td>${img.width}×${img.height}</td><td>${img.shape_count} (${img.mask_count} masks, ${img.polygon_count} polygons)</td><td>${img.dataset_name ? escapeHtml(img.dataset_name) : "—"}</td><td class="${cls}" title="${escapeHtml(img.message || "")}">${img.status}${img.message ? `<br><small>${escapeHtml(img.message)}</small>` : ""}${img.skipped.length ? `<br><small>${img.skipped.length} element(s) skipped</small>` : ""}</td>`;
      return tr;
    }));
    labelsBody.replaceChildren(...r.labels.map((label) => {
      const tr = document.createElement("tr");
      const swatch = label.color ? `<span class="lv-class-swatch" style="display:inline-block;background:${label.color}"></span> ` : "";
      tr.innerHTML = `<td>${swatch}${escapeHtml(label.label)}</td><td>${label.shape_count}</td><td><select class="lv-select lv-select-inline" data-label="${escapeHtml(label.label)}" data-help="xml.class_mapping" aria-label="Class for ${escapeHtml(label.label)}"></select></td>`;
      const select = tr.querySelector("select");
      select.append(Object.assign(document.createElement("option"), { value: "create", textContent: "create new class" }));
      for (const cls of window.__afmlabelClasses || []) {
        select.append(Object.assign(document.createElement("option"), { value: String(cls.class_id), textContent: `existing: ${cls.name} (#${cls.class_id})` }));
      }
      select.append(Object.assign(document.createElement("option"), { value: "skip", textContent: "do not import these shapes" }));
      select.value = label.proposal === "existing" && label.class_id !== null ? String(label.class_id) : "create";
      return tr;
    }));
    summary.textContent = `${r.total_shape_count} shape(s) will become regions in ${r.committable_dataset_count} dataset(s). ${r.warnings.join(" ")}`;
    commitButton.disabled = r.committable_dataset_count === 0;
    applyHelp(labelsBody);
  }

  stageButton.addEventListener("click", async () => {
    error.textContent = "";
    if (!fileInput.files.length) { error.textContent = "Choose a CVAT/TSWAT XML file."; return; }
    const form = new FormData();
    form.append("file", fileInput.files[0], fileInput.files[0].name);
    if (targetSelect.value) form.append("target_dataset_id", targetSelect.value);
    stageButton.disabled = true;
    try {
      const detail = await api(`/api/v1/projects/${projectId}`);
      window.__afmlabelClasses = detail.classes.classes;
      render(await upload(`/api/v1/projects/${projectId}/imports/tswat/stage`, form));
    } catch (err) {
      error.textContent = err.recovery ? `${err.message}. ${err.recovery}.` : err.message;
    } finally {
      stageButton.disabled = false;
    }
  });

  commitButton.addEventListener("click", async () => {
    error.textContent = "";
    commitButton.disabled = true;
    try {
      const result = await api(`/api/v1/projects/${projectId}/imports/tswat/${stagedId}/commit`, {
        method: "POST",
        body: { expected_revision: getRevision(), class_mapping: currentMapping(), target_dataset_id: targetSelect.value || null },
      });
      stagedId = null;
      setRevision(result.project_revision);
      const lines = result.datasets.map((d) => `${d.dataset_name}: ${d.region_ids.length} region(s) imported (r${d.dataset_revision})${d.warnings.length ? `; ${d.warnings.length} warning(s)` : ""}`);
      if (result.created_classes.length) lines.push(`created classes: ${result.created_classes.map((c) => c.name).join(", ")}`);
      if (result.skipped_images.length) lines.push(`${result.skipped_images.length} XML image(s) not imported`);
      lines.push("Every imported overlap is unreviewed: open the datasets and review the Findings panel before export.");
      resultEl.textContent = lines.join("\n");
      await reloadProject();
      await refreshDatasets();
    } catch (err) {
      error.textContent = err.recovery ? `${err.message}. ${err.recovery}.` : err.message;
      commitButton.disabled = false;
    }
  });

  document.getElementById("import-tswat-button").addEventListener("click", open);
  document.getElementById("close-tswat-modal-button").addEventListener("click", close);
  document.getElementById("cancel-tswat-modal-button").addEventListener("click", close);

  // ---- lossy raster ---------------------------------------------------------
  const lossyModal = document.getElementById("lossy-import-modal");
  const lossyFile = document.getElementById("lossy-file");
  const lossyBody = document.getElementById("lossy-values-body");
  const lossyError = document.getElementById("lossy-error");
  const lossyCommit = document.getElementById("lossy-commit-button");
  const lossyNote = document.getElementById("lossy-default-note");
  const discardRow = document.getElementById("lossy-discard-row");
  const discardConfirm = document.getElementById("lossy-confirm-discard");
  let lossyTarget = null;
  let lossyStaged = null;
  let lossyPreview = null;

  function openLossy(ds) {
    lossyTarget = ds;
    lossyStaged = null;
    lossyPreview = null;
    lossyFile.value = "";
    lossyBody.replaceChildren();
    lossyError.textContent = "";
    lossyNote.textContent = "";
    discardRow.hidden = true;
    discardConfirm.checked = false;
    lossyCommit.disabled = true;
    document.getElementById("lossy-dataset-name").textContent = ds.name;
    document.getElementById("lossy-expected-size").textContent = `${ds.width} × ${ds.height}`;
    lossyModal.hidden = false;
  }

  function lossyMapping() {
    const mapping = {};
    let unresolved = 0;
    let discarded = 0;
    for (const select of lossyBody.querySelectorAll("select[data-value]")) {
      if (select.value === CHOOSE) { unresolved += 1; mapping[select.dataset.value] = undefined; }
      else if (select.value === DO_NOT_IMPORT) { discarded += 1; mapping[select.dataset.value] = null; }
      else mapping[select.dataset.value] = Number(select.value);
      select.classList.toggle("lv-choice-required", select.value === CHOOSE);
    }
    return { mapping, unresolved, discarded };
  }

  function updateLossyState() {
    if (!lossyPreview) return;
    const { unresolved, discarded } = lossyMapping();
    discardRow.hidden = discarded === 0;
    if (discarded === 0) discardConfirm.checked = false;
    lossyCommit.disabled = lossyPreview.values.length === 0 || unresolved > 0 || (discarded > 0 && !discardConfirm.checked);
    lossyCommit.dataset.helpNote = unresolved > 0 ? `${unresolved} value(s) still need a class or an explicit 'do not import'` : discarded > 0 && !discardConfirm.checked ? "Confirm the discarded values first" : "";
  }

  document.getElementById("lossy-stage-button").addEventListener("click", async () => {
    lossyError.textContent = "";
    if (!lossyFile.files.length) { lossyError.textContent = "Choose a single-channel label raster (PNG/TIFF)."; return; }
    const form = new FormData();
    form.append("file", lossyFile.files[0], lossyFile.files[0].name);
    try {
      const detail = await api(`/api/v1/projects/${projectId}`);
      const classes = detail.classes.classes.filter((c) => c.enabled);
      const staged = await upload(`/api/v1/projects/${projectId}/datasets/${lossyTarget.dataset_id}/imports/lossy-mask/stage`, form);
      lossyStaged = staged.staged_id;
      const preview = staged.preview;
      lossyPreview = preview;
      if (!preview.compatible) { lossyError.textContent = preview.reason; lossyCommit.disabled = true; return; }
      if (preview.default_class_id !== null) {
        lossyNote.textContent = `Proposed class for every value: ${preview.default_class_name} (${preview.default_class_source}). Change it per value if needed.`;
      } else if (preview.requires_class_choice) {
        lossyNote.textContent = "This project has no Buffer class and no default import class: choose a class for every value explicitly before importing.";
      } else {
        lossyNote.textContent = "";
      }
      lossyBody.replaceChildren(...preview.values.map((v) => {
        const tr = document.createElement("tr");
        const options = [
          `<option value="${CHOOSE}" ${v.suggested_class_id === null ? "selected" : ""} disabled hidden>choose a class…</option>`,
          ...classes.map((c) => `<option value="${c.class_id}" ${c.class_id === v.suggested_class_id ? "selected" : ""}>${escapeHtml(c.name)} (#${c.class_id})</option>`),
          `<option value="${DO_NOT_IMPORT}">Do not import this value</option>`,
        ];
        tr.innerHTML = `<td class="lv-mono">${v.value}</td><td>${v.pixel_count} px, ${v.component_count} component(s)</td><td><select class="lv-select lv-select-inline" data-value="${v.value}" data-help="lossy.target_class" aria-label="Target class for raster value ${v.value}">${options.join("")}</select></td>`;
        tr.querySelector("select").addEventListener("change", updateLossyState);
        return tr;
      }));
      applyHelp(lossyBody);
      updateLossyState();
    } catch (err) {
      lossyError.textContent = err.recovery ? `${err.message}. ${err.recovery}.` : err.message;
    }
  });
  discardConfirm.addEventListener("change", updateLossyState);

  lossyCommit.addEventListener("click", async () => {
    lossyError.textContent = "";
    const { mapping, unresolved } = lossyMapping();
    if (unresolved > 0) { lossyError.textContent = "Choose a class (or 'do not import') for every raster value first."; return; }
    const body = {
      staged_id: lossyStaged,
      expected_revision: lossyTarget.revision,
      value_to_class: Object.fromEntries(Object.entries(mapping).map(([k, v]) => [k, v === undefined ? null : v])),
      split_components: document.getElementById("lossy-split").checked,
      confirm_discard: discardConfirm.checked,
    };
    lossyCommit.disabled = true;
    try {
      const result = await api(`/api/v1/projects/${projectId}/datasets/${lossyTarget.dataset_id}/imports/lossy-mask/commit`, { method: "POST", body });
      lossyModal.hidden = true;
      // Open the workspace with the imported regions selected and centred
      // so the result of the import is unmistakable.
      const params = new URLSearchParams({ select: result.region_ids.join(","), imported: "1", summary: result.summary });
      if (result.active_class_id !== null) params.set("class", String(result.active_class_id));
      window.location.href = `/projects/${projectId}/datasets/${lossyTarget.dataset_id}?${params.toString()}`;
    } catch (err) {
      lossyError.textContent = err.recovery ? `${err.message}. ${err.recovery}.` : err.message;
      lossyCommit.disabled = false;
    }
  });
  document.getElementById("close-lossy-modal-button").addEventListener("click", () => { lossyModal.hidden = true; });
  document.getElementById("cancel-lossy-modal-button").addEventListener("click", () => { lossyModal.hidden = true; });

  return { openLossy };
}
