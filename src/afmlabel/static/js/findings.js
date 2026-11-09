// SPDX-License-Identifier: MIT
// Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
// Findings panel: one live list of
// validation/review items served by GET /findings, default order
// unreviewed-blocking > unreviewed-warning > reviewed, filters by review
// state / severity / type, familiar multi-selection, bulk review, an
// in-panel Validate button and an explicit stale-validation banner.
//
// The panel never changes geometry: reviewing acknowledges the current
// z-order result of an overlap; Subtract by Order lives in the Regions
// panel. Validate stores the report that gates export.

import { api } from "./api.js";
import { applyHelp } from "./help.js";

const REVIEW_LABELS = {
  unreviewed: "Unreviewed",
  reviewed_resolved: "Reviewed: resolved by ordering",
  reviewed_intentional: "Reviewed: intentional",
  reviewed_fixed_by_edit: "Reviewed: fixed by edit",
};

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

export function setUpFindings({ base, canvas, regionsPanel, statusBar, getState, enqueue, mutation, say, reload, showTab, onRevision }) {
  const listEl = document.getElementById("findings-list");
  const reportEl = document.getElementById("validation-report");
  const badge = document.getElementById("findings-count");
  const staleBanner = document.getElementById("findings-stale");
  const summaryEl = document.getElementById("findings-summary");
  const filterStatus = document.getElementById("findings-filter-status");
  const filterSeverity = document.getElementById("findings-filter-severity");
  const filterType = document.getElementById("findings-filter-type");
  const selectedCount = document.getElementById("findings-selected-count");
  const markSelectedButton = document.getElementById("findings-mark-selected-button");
  const markVisibleButton = document.getElementById("findings-mark-visible-button");
  const selectAllButton = document.getElementById("findings-select-all-button");
  const panel = document.getElementById("panel-findings");
  let data = null;
  let selected = new Set();
  let anchorId = null;
  let refreshTimer = null;
  let requireReview = document.body.dataset.requireOverlapReview === "true";
  const boundaryMode = document.body.dataset.boundaryMode;

  function visibleFindings() {
    if (!data) return [];
    return data.findings.filter((f) => {
      if (filterStatus.value === "unreviewed" && f.review_state === "reviewed") return false;
      if (filterStatus.value === "reviewed" && f.review_state !== "reviewed") return false;
      if (filterSeverity.value !== "all" && f.severity !== filterSeverity.value) return false;
      if (filterType.value !== "all" && f.type !== filterType.value) return false;
      return true;
    });
  }

  function navigate(finding) {
    if (finding.bbox_xywh) {
      const [x, y, w, h] = finding.bbox_xywh;
      canvas.centerOn(x, y, w, h);
    }
    if (finding.type === "missing_coverage") {
      canvas.showMissing = true;
      const box = document.getElementById("show-missing");
      if (box) box.checked = true;
    }
    canvas.setHighlight(finding.region_ids);
    regionsPanel.setOverlapIds(finding.region_ids);
  }

  function updateSelectionUi() {
    for (const li of listEl.children) {
      const on = selected.has(li.dataset.findingId);
      li.classList.toggle("selected", on);
      li.setAttribute("aria-selected", on ? "true" : "false");
    }
    const reviewable = Array.from(selected).filter((id) => {
      const f = data && data.findings.find((x) => x.finding_id === id);
      return f && f.reviewable && f.review_state !== "reviewed";
    });
    selectedCount.textContent = selected.size ? `${selected.size} selected` : "";
    selectedCount.hidden = selected.size === 0;
    markSelectedButton.disabled = reviewable.length === 0;
    markSelectedButton.dataset.helpNote = reviewable.length ? `${reviewable.length} unreviewed overlap(s) in the selection` : "Select unreviewed overlap Findings first";
    const visibleReviewable = visibleFindings().filter((f) => f.reviewable && f.review_state !== "reviewed");
    markVisibleButton.disabled = visibleReviewable.length === 0;
  }

  function selectFinding(id, { toggle = false, range = false } = {}) {
    const ids = visibleFindings().map((f) => f.finding_id);
    if (range && anchorId && ids.includes(anchorId) && ids.includes(id)) {
      const a = ids.indexOf(anchorId);
      const b = ids.indexOf(id);
      const [lo, hi] = a < b ? [a, b] : [b, a];
      selected = new Set([...(toggle ? selected : []), ...ids.slice(lo, hi + 1)]);
    } else if (toggle) {
      if (selected.has(id)) selected.delete(id); else selected.add(id);
      anchorId = id;
    } else {
      selected = new Set([id]);
      anchorId = id;
    }
    updateSelectionUi();
  }

  function selectAllVisible() {
    selected = new Set(visibleFindings().map((f) => f.finding_id));
    updateSelectionUi();
  }

  function clearSelection() {
    selected = new Set();
    anchorId = null;
    updateSelectionUi();
  }

  function renderTypes() {
    const current = filterType.value || "all";
    filterType.replaceChildren(Object.assign(document.createElement("option"), { value: "all", textContent: "All types" }));
    for (const t of data.types) {
      filterType.append(Object.assign(document.createElement("option"), { value: t.type, textContent: `${t.label} (${t.count})` }));
    }
    filterType.value = data.types.some((t) => t.type === current) ? current : "all";
  }

  function renderList() {
    if (!data) return;
    renderTypes();
    const inOverlap = new Set(data.findings.filter((f) => f.type === "overlap").flatMap((f) => f.region_ids));
    regionsPanel.setOverlapIds(Array.from(inOverlap));
    const overlapGroups = data.findings.filter((f) => f.type === "overlap");
    const unreviewedOverlaps = overlapGroups.filter((f) => f.review_state !== "reviewed").length;
    const maxMultiplicity = overlapGroups.reduce((m, f) => Math.max(m, f.details.max_multiplicity || 0), 0);
    statusBar.setOverlaps(overlapGroups.length, maxMultiplicity, unreviewedOverlaps);
    badge.textContent = String(data.counts.blocking_unreviewed + data.counts.warning_unreviewed);
    badge.hidden = data.counts.blocking_unreviewed + data.counts.warning_unreviewed === 0;
    summaryEl.textContent = `${data.counts.blocking_unreviewed} blocking · ${data.counts.warning_unreviewed} warning · ${data.counts.reviewed} reviewed · coverage ${data.coverage_percent.toFixed(2)} %`;
    const visible = visibleFindings();
    for (const id of Array.from(selected)) if (!data.findings.some((f) => f.finding_id === id)) selected.delete(id);
    if (!visible.length) {
      const li = document.createElement("li");
      li.className = "lv-finding-empty";
      li.textContent = data.findings.length ? "No Finding matches the current filters." : "No Findings: every pixel has exactly one class and no overlap is pending.";
      listEl.replaceChildren(li);
      updateSelectionUi();
      return;
    }
    listEl.replaceChildren(...visible.map((finding) => {
      const li = document.createElement("li");
      li.className = `lv-finding-item severity-${finding.severity} review-${finding.review_state}` + (selected.has(finding.finding_id) ? " selected" : "");
      li.dataset.findingId = finding.finding_id;
      li.dataset.findingType = finding.type;
      li.dataset.help = "findings.row";
      li.tabIndex = 0;
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", selected.has(finding.finding_id) ? "true" : "false");
      const reviewState = finding.type === "overlap" ? REVIEW_LABELS[finding.details.overlap_review_state] : null;
      const chips = `<span class="lv-finding-chip lv-chip-${finding.severity}" data-help="findings.severity" tabindex="0">${finding.severity}</span>` +
        `<span class="lv-finding-chip lv-chip-type">${escapeHtml(finding.type_label)}</span>` +
        (finding.reviewable ? `<span class="lv-finding-chip lv-chip-${finding.review_state}" data-help="findings.review_state" tabindex="0">${escapeHtml(reviewState || finding.review_state)}</span>` : "") +
        (finding.requires_correction ? '<span class="lv-finding-chip lv-chip-correction">needs correction</span>' : "");
      const actions = finding.reviewable
        ? `<div class="lv-finding-actions">
            ${finding.review_state !== "reviewed" ? `<button type="button" class="lv-btn lv-btn-small" data-state="reviewed_resolved" data-help="findings.review_resolved">Resolved by order</button>
            <button type="button" class="lv-btn lv-btn-small" data-state="reviewed_intentional" data-help="findings.review_intentional">Intentional</button>` : `<button type="button" class="lv-btn lv-btn-small" data-state="unreviewed" data-help="findings.review_reset">Reset review</button>`}
          </div>`
        : "";
      li.innerHTML = `<div class="lv-finding-title">${escapeHtml(finding.title)}</div>
        <div class="lv-finding-chips">${chips}</div>
        <div class="lv-finding-meta">${escapeHtml(finding.message)}${finding.region_ids.length ? ` · ${finding.region_ids.map(escapeHtml).join(", ")}` : ""}</div>${actions}`;
      const pick = (e) => {
        if (e.target.closest("button, .lv-finding-chip")) return;
        selectFinding(finding.finding_id, { toggle: e.ctrlKey || e.metaKey, range: e.shiftKey });
        navigate(finding);
      };
      li.addEventListener("click", pick);
      li.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(e); } });
      for (const button of li.querySelectorAll("button[data-state]")) {
        button.addEventListener("click", () => review([finding.group_id], button.dataset.state));
      }
      return li;
    }));
    if (boundaryMode === "fill_unlabeled_after_approval" && data.unlabeled_pixels > 0) {
      const fill = document.createElement("li");
      fill.className = "lv-finding-item lv-finding-action-item";
      fill.innerHTML = '<div class="lv-finding-title">Boundary fill</div><div class="lv-finding-meta">Assign all currently unlabelled pixels to the Boundary class. Never overwrites a material label; requires explicit confirmation of the pixel count.</div><div class="lv-finding-actions"><button type="button" class="lv-btn lv-btn-small" id="boundary-fill-button" data-help="findings.boundary_fill">Preview fill…</button></div>';
      fill.querySelector("#boundary-fill-button").addEventListener("click", boundaryFill);
      listEl.append(fill);
    }
    updateSelectionUi();
    applyHelp(listEl);
  }

  function renderStale() {
    if (!data) return;
    const stored = data.stored_validation_status;
    if (!stored) {
      staleBanner.hidden = true;
      statusBar.setValidation("not run", "");
      return;
    }
    if (data.validation_stale) {
      staleBanner.hidden = false;
      staleBanner.textContent = `Stored validation (${stored} at r${data.stored_validation_revision}) is stale: the dataset is now at r${data.dataset_revision}. The list below is live; run Validate before export.`;
      statusBar.setValidation(`${stored} at r${data.stored_validation_revision}, now stale`, "warn");
    } else {
      staleBanner.hidden = true;
      statusBar.setValidation(
        stored === "valid" ? `valid at r${data.stored_validation_revision}` : `blocked (${data.counts.blocking_unreviewed})`,
        stored === "valid" ? "good" : "bad",
      );
    }
  }

  async function refresh() {
    try {
      data = await api(`${base}/findings`);
      const state = getState();
      state.validatedRevision = data.validated_revision;
      renderList();
      renderStale();
    } catch (err) {
      const li = document.createElement("li");
      li.className = "lv-finding-empty";
      li.textContent = `findings unavailable: ${err.message}`;
      listEl.replaceChildren(li);
    }
    return data;
  }

  function scheduleRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refresh, 400);
  }

  async function review(groupIds, state) {
    const stateRef = getState();
    const ids = groupIds.filter(Boolean);
    if (!ids.length) return;
    try {
      const result = await enqueue(() => mutation("/findings/review", {
        method: "POST",
        body: { expected_revision: stateRef.datasetRevision, group_ids: ids, state },
      }));
      data = result.findings;
      stateRef.validatedRevision = data.validated_revision;
      if (result.missing_group_ids.length) say(`${result.missing_group_ids.length} overlap(s) no longer exist and were skipped.`, 5000);
      renderList();
      renderStale();
      if (onRevision) onRevision(result.dataset_revision);
    } catch (err) {
      say(err.recovery ? `${err.message}. ${err.recovery}.` : err.message, 6000);
      await refresh();
    }
  }

  function reviewSelected() {
    const groupIds = Array.from(selected)
      .map((id) => data && data.findings.find((f) => f.finding_id === id))
      .filter((f) => f && f.reviewable && f.review_state !== "reviewed")
      .map((f) => f.group_id);
    if (!groupIds.length) { say("Select unreviewed overlap Findings first. Review never changes geometry; use Subtract by Order in the Regions panel for that.", 6000); return; }
    review(groupIds, "reviewed_resolved");
  }

  function reviewVisible() {
    const groupIds = visibleFindings().filter((f) => f.reviewable && f.review_state !== "reviewed").map((f) => f.group_id);
    if (!groupIds.length) { say("No unreviewed overlap is visible.", 4000); return; }
    if (!window.confirm(`Mark ${groupIds.length} visible overlap(s) as reviewed (resolved by ordering)? This only changes review state; geometry is untouched.`)) return;
    review(groupIds, "reviewed_resolved");
  }

  function renderReport(result, stale) {
    const items = [];
    const tone = result.status === "valid" ? "ok" : "blocking";
    items.push(`<p class="${tone}"><strong>${result.status === "valid" ? "VALID" : "BLOCKED"}</strong> at r${result.dataset_revision}${stale ? " (stale: dataset changed since)" : ""} · coverage ${result.coverage.coverage_percent.toFixed(2)} % · ${result.overlap_group_count} overlap group(s), ${result.unreviewed_overlap_count} unreviewed</p>`);
    if (result.blocking.length) {
      items.push('<div class="blocking">Blocking:</div><ul>' + result.blocking.map((i) => `<li class="blocking" data-regions="${i.region_ids.join(",")}">${i.code}: ${escapeHtml(i.message)}</li>`).join("") + "</ul>");
    }
    if (result.warnings.length) {
      items.push('<div class="warning">Warnings:</div><ul>' + result.warnings.map((i) => `<li class="warning">${i.code}: ${escapeHtml(i.message)}</li>`).join("") + "</ul>");
    }
    const histogram = Object.entries(result.class_pixel_histogram).map(([id, px]) => `#${id}: ${px} px`).join(", ");
    items.push(`<div class="lv-finding-meta">classes after composition: ${histogram || "none"}${result.composition_sha256 ? ` · composition ${result.composition_sha256.slice(0, 12)}…` : ""}</div>`);
    reportEl.innerHTML = items.join("");
    for (const li of reportEl.querySelectorAll("li[data-regions]")) {
      const ids = li.dataset.regions ? li.dataset.regions.split(",") : [];
      if (ids.length) {
        li.style.cursor = "pointer";
        li.addEventListener("click", () => { canvas.setHighlight(ids); regionsPanel.setOverlapIds(ids); });
      }
    }
  }

  async function validate() {
    showTab("tab-findings");
    reportEl.textContent = "Validating…";
    try {
      const result = await enqueue(() => api(`${base}/validate`, { method: "POST" }));
      const stateRef = getState();
      stateRef.validatedRevision = result.status === "valid" ? result.dataset_revision : null;
      renderReport(result, false);
      await refresh();
      say(result.status === "valid" ? "Dataset is valid and exportable." : `Validation blocked by ${result.blocking.length} issue(s).`, 5000);
    } catch (err) {
      reportEl.textContent = `validation failed: ${err.message}`;
    }
  }

  async function loadExistingReport() {
    try {
      const status = await api(`${base}/validation`);
      if (status.report) renderReport(status.report, status.stale);
      else reportEl.textContent = "Run Validate to store a validation report for export.";
    } catch (err) {
      reportEl.textContent = err.message;
    }
  }

  async function boundaryFill() {
    try {
      const preview = await api(`${base}/boundary-fill/preview`);
      if (!preview.allowed) { say(preview.reason, 6000); return; }
      const ok = window.confirm(`Assign ${preview.pixel_count} unlabelled pixel(s) to class "${preview.boundary_class_name}" (#${preview.boundary_class_id})?\n\nThe fill becomes a new region at the bottom of the z-order and never overwrites an existing label. This is recorded in the audit log.`);
      if (!ok) return;
      const stateRef = getState();
      await stateRef.structural("Boundary fill", () => enqueue(() => mutation("/boundary-fill", { method: "POST", body: { expected_revision: stateRef.datasetRevision, confirmed_pixel_count: preview.pixel_count } })));
      await reload();
      say("Boundary fill applied.", 4000);
    } catch (err) {
      say(err.recovery ? `${err.message}. ${err.recovery}.` : err.message, 8000);
    }
  }

  function onStats(stats) {
    // Client-side figures update instantly; the server report follows.
    statusBar.setCoverage({ total: stats.total, labeled: stats.labeled, missing: stats.missing });
    scheduleRefresh();
  }

  function onRegionsLoaded() {
    requireReview = document.body.dataset.requireOverlapReview === "true";
    scheduleRefresh();
    loadExistingReport();
  }

  function markStale() {
    if (!data) return;
    data.validation_stale = data.stored_validation_status !== null && data.stored_validation_revision !== getState().datasetRevision;
    renderStale();
  }

  // Panel-scoped shortcuts: only while the panel has focus.
  panel.addEventListener("keydown", (e) => {
    if (e.target.closest("input, select, textarea")) return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") { e.preventDefault(); e.stopPropagation(); selectAllVisible(); return; }
    if (e.key.toLowerCase() === "r" && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); e.stopPropagation(); reviewSelected(); return; }
    if (e.key === "Escape") { e.stopPropagation(); clearSelection(); }
  });
  for (const select of [filterStatus, filterSeverity, filterType]) select.addEventListener("change", () => { renderList(); });
  document.getElementById("refresh-findings-button").addEventListener("click", refresh);
  document.getElementById("validate-button").addEventListener("click", validate);
  document.getElementById("findings-validate-button").addEventListener("click", validate);
  markSelectedButton.addEventListener("click", reviewSelected);
  markVisibleButton.addEventListener("click", reviewVisible);
  selectAllButton.addEventListener("click", selectAllVisible);
  reportEl.textContent = "Run Validate to compute the final composition report.";
  return { onStats, onRegionsLoaded, validate, refresh, markStale, selectAllVisible, clearSelection, reviewSelected, requireReview: () => requireReview, data: () => data };
}
