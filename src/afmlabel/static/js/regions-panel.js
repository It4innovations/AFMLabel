// SPDX-License-Identifier: MIT
// Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
// Right-hand Regions / Classes panels: region rows with multi-selection
// (click / Ctrl+click / Shift+click), visibility, lock with a persistent
// locked state, class reassignment, delete, and drag-and-drop z-order
// (top of the list = highest precedence).
//
// Selection semantics: the panel reports
// `onSelect(id, {toggle, range})`; the workspace owns the selection state
// and calls `setSelection(primaryId, ids)` back. The row checkbox mirrors
// membership in the selection, so "Merge checked", "Subtract by Order" and
// "Delete Selected" all act on the same set.

import { applyHelp } from "./help.js";

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const ICON_EYE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6-10-6-10-6z"/><circle cx="12" cy="12" r="3"/></svg>';
const ICON_EYE_OFF = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 3l18 18M10.6 10.6a3 3 0 0 0 4.2 4.2M9.9 5.1A10.9 10.9 0 0 1 12 5c6.5 0 10 7 10 7a17 17 0 0 1-3 3.9M6.6 6.6C3.8 8.4 2 12 2 12s3.5 7 10 7c1.6 0 3-.3 4.3-.9"/></svg>';
export const ICON_LOCK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="11" width="16" height="10" rx="1.5"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/><circle cx="12" cy="16" r="1.2" fill="currentColor"/></svg>';
const ICON_UNLOCK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="11" width="16" height="10" rx="1.5"/><path d="M8 11V7a4 4 0 0 1 7.5-2"/></svg>';
const ICON_DELETE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>';

export class RegionsPanel {
  constructor(listElement, handlers) {
    this.list = listElement;
    this.handlers = handlers;
    this.regions = [];
    this.classes = [];
    this.selectedId = null;
    this.selectedIds = new Set();
    this.overlapIds = new Set();
    this.dragId = null;
    this.list.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { this.handlers.onSelect(null, {}); e.stopPropagation(); }
    });
  }

  setClasses(classes) {
    this.classes = classes;
    this.render();
  }

  setRegions(regions) {
    // regions: bottom -> top; the list shows top first.
    this.regions = regions;
    for (const id of Array.from(this.selectedIds)) {
      if (!regions.some((r) => r.region_id === id)) this.selectedIds.delete(id);
    }
    this.render();
  }

  setSelected(id) {
    this.setSelection(id, id ? [id] : []);
  }

  setSelection(primaryId, ids) {
    this.selectedId = primaryId;
    this.selectedIds = new Set(ids || []);
    for (const li of this.list.children) {
      const rid = li.dataset.regionId;
      const on = this.selectedIds.has(rid);
      li.classList.toggle("selected", on);
      li.classList.toggle("primary", rid === primaryId);
      li.setAttribute("aria-selected", on ? "true" : "false");
      const box = li.querySelector('input[type="checkbox"]');
      if (box) box.checked = on;
    }
    const active = this.list.querySelector(".primary") || this.list.querySelector(".selected");
    if (active) active.scrollIntoView({ block: "nearest" });
    this._updateCount();
  }

  setOverlapIds(ids) {
    this.overlapIds = new Set(ids);
    for (const li of this.list.children) li.classList.toggle("in-overlap", this.overlapIds.has(li.dataset.regionId));
  }

  // Ordered top -> bottom as displayed (highest z first).
  displayedIds() {
    return [...this.regions].reverse().map((r) => r.region_id);
  }

  checkedIds() {
    return Array.from(this.selectedIds);
  }

  _updateCount() {
    const count = document.getElementById("region-count");
    if (count) count.textContent = String(this.regions.length);
    const selected = document.getElementById("region-selected-count");
    if (selected) {
      selected.textContent = this.selectedIds.size ? `${this.selectedIds.size} selected` : "";
      selected.hidden = this.selectedIds.size === 0;
    }
  }

  render() {
    const items = [...this.regions].reverse().map((region) => this._row(region));
    this.list.replaceChildren(...items);
    this._updateCount();
    applyHelp(this.list);
  }

  _row(region) {
    const li = document.createElement("li");
    li.className = "lv-region-item";
    li.dataset.regionId = region.region_id;
    li.dataset.help = "regions.row";
    li.draggable = true;
    li.tabIndex = 0;
    li.setAttribute("role", "option");
    const selected = this.selectedIds.has(region.region_id);
    li.setAttribute("aria-selected", selected ? "true" : "false");
    if (selected) li.classList.add("selected");
    if (region.region_id === this.selectedId) li.classList.add("primary");
    if (!region.visible) li.classList.add("hidden-region");
    if (region.locked) li.classList.add("locked-region");
    if (this.overlapIds.has(region.region_id)) li.classList.add("in-overlap");
    const cls = this.classes.find((c) => c.class_id === region.class_id);
    const color = cls ? cls.display_color : "#888888";
    const label = region.name ? escapeHtml(region.name) : `<span class="lv-mono">${region.region_id}</span>`;
    const source = region.source === "manual" ? "" : ` · ${region.source}${region.user_corrected ? " (edited)" : ""}`;
    const lockBadge = region.locked
      ? `<span class="lv-region-lock-badge" data-help="regions.locked_badge" tabindex="0" role="img" aria-label="Locked region — editing disabled">${ICON_LOCK}<span>locked</span></span>`
      : "";
    li.innerHTML = `
      <span class="lv-region-swatch" style="background:${color}" aria-hidden="true"></span>
      <span class="lv-region-name">${label}${lockBadge}</span>
      <span class="lv-region-meta">z${region.z_order} · ${region.area} px${source}</span>
      <span class="lv-region-controls">
        <input type="checkbox" data-help="regions.checkbox" aria-label="Select ${escapeHtml(region.name || region.region_id)}" ${selected ? "checked" : ""}>
        <select data-help="regions.class" aria-label="Class of ${escapeHtml(region.name || region.region_id)}" ${region.locked ? "disabled" : ""}>${this.classes.map((c) => `<option value="${c.class_id}" ${c.class_id === region.class_id ? "selected" : ""}>${escapeHtml(c.name)}</option>`).join("")}</select>
        <button type="button" data-action="visible" data-help="regions.visible" class="lv-icon-btn ${region.visible ? "on" : ""}" aria-label="${region.visible ? "Hide" : "Show"} ${escapeHtml(region.name || region.region_id)}" aria-pressed="${region.visible ? "false" : "true"}">${region.visible ? ICON_EYE : ICON_EYE_OFF}</button>
        <button type="button" data-action="locked" data-help="regions.lock" class="lv-icon-btn ${region.locked ? "on locked" : ""}" aria-label="${region.locked ? "Unlock" : "Lock"} ${escapeHtml(region.name || region.region_id)}" aria-pressed="${region.locked ? "true" : "false"}">${region.locked ? ICON_LOCK : ICON_UNLOCK}</button>
        <button type="button" data-action="delete" data-help="regions.delete" class="lv-icon-btn danger" aria-label="Delete ${escapeHtml(region.name || region.region_id)}" ${region.locked ? "disabled" : ""}>${ICON_DELETE}</button>
      </span>`;
    const clickSelect = (e) => {
      if (e.target.closest("select, button, input, .lv-region-lock-badge")) return;
      this.handlers.onSelect(region.region_id, { toggle: e.ctrlKey || e.metaKey, range: e.shiftKey });
    };
    li.addEventListener("click", clickSelect);
    li.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); clickSelect(e); }
    });
    li.querySelector("select").addEventListener("change", (e) => this.handlers.onClassChange(region.region_id, Number(e.target.value)));
    li.querySelector("select").addEventListener("click", (e) => e.stopPropagation());
    li.querySelector('input[type="checkbox"]').addEventListener("change", (e) => {
      this.handlers.onSelect(region.region_id, { toggle: true, forceOn: e.target.checked });
    });
    li.querySelector('[data-action="visible"]').addEventListener("click", () => this.handlers.onToggle(region.region_id, "visible", !region.visible));
    li.querySelector('[data-action="locked"]').addEventListener("click", () => this.handlers.onToggle(region.region_id, "locked", !region.locked));
    li.querySelector('[data-action="delete"]').addEventListener("click", () => this.handlers.onDelete(region.region_id));

    li.addEventListener("dragstart", (e) => {
      this.dragId = region.region_id;
      li.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", region.region_id);
    });
    li.addEventListener("dragend", () => {
      this.dragId = null;
      for (const item of this.list.children) item.classList.remove("dragging", "drop-before", "drop-after");
    });
    li.addEventListener("dragover", (e) => {
      if (!this.dragId || this.dragId === region.region_id) return;
      e.preventDefault();
      const rect = li.getBoundingClientRect();
      const before = e.clientY < rect.top + rect.height / 2;
      li.classList.toggle("drop-before", before);
      li.classList.toggle("drop-after", !before);
    });
    li.addEventListener("dragleave", () => li.classList.remove("drop-before", "drop-after"));
    li.addEventListener("drop", (e) => {
      e.preventDefault();
      if (!this.dragId || this.dragId === region.region_id) return;
      const rect = li.getBoundingClientRect();
      const before = e.clientY < rect.top + rect.height / 2;
      const topFirst = [...this.regions].reverse().map((r) => r.region_id).filter((id) => id !== this.dragId);
      let index = topFirst.indexOf(region.region_id);
      if (!before) index += 1;
      topFirst.splice(index, 0, this.dragId);
      this.handlers.onReorder(topFirst.reverse()); // bottom -> top
    });
    return li;
  }
}

export class ClassesPanel {
  constructor(listElement, selectElement, handlers) {
    this.list = listElement;
    this.select = selectElement;
    this.handlers = handlers;
    this.classes = [];
    this.activeId = null;
  }

  setClasses(classes) {
    this.classes = classes;
    if (!this.classes.some((c) => c.class_id === this.activeId && c.enabled)) {
      const first = this.classes.find((c) => c.enabled);
      this.activeId = first ? first.class_id : null;
    }
    this.render();
  }

  setActive(id) {
    this.activeId = id;
    this.render();
  }

  render() {
    this.list.replaceChildren(...this.classes.map((cls) => {
      const li = document.createElement("li");
      li.className = "lv-class-item" + (cls.class_id === this.activeId ? " active" : "") + (cls.enabled ? "" : " disabled-class");
      li.dataset.help = "class.item";
      li.tabIndex = 0;
      li.setAttribute("role", "button");
      li.setAttribute("aria-pressed", cls.class_id === this.activeId ? "true" : "false");
      li.innerHTML = `<span class="lv-class-swatch" style="background:${cls.display_color}" aria-hidden="true"></span>
        <span>${escapeHtml(cls.name)} <span class="lv-class-key">${cls.semantic_role === "MaterialLayer" ? "" : cls.semantic_role}</span></span>
        <span class="lv-mono" title="stable class id">#${cls.class_id}</span>`;
      const activate = () => { if (cls.enabled) this.handlers.onActivate(cls.class_id); };
      li.addEventListener("click", activate);
      li.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); activate(); } });
      return li;
    }));
    this.select.replaceChildren(...this.classes.filter((c) => c.enabled).map((cls) => {
      const option = document.createElement("option");
      option.value = String(cls.class_id);
      option.textContent = `${cls.name} (#${cls.class_id})`;
      option.selected = cls.class_id === this.activeId;
      return option;
    }));
    if (!this.classes.some((c) => c.enabled)) {
      const option = document.createElement("option");
      option.textContent = "no classes — add one";
      option.disabled = true;
      option.selected = true;
      this.select.append(option);
    }
    applyHelp(this.list);
  }
}
