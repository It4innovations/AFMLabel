// SPDX-License-Identifier: MIT
// Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
// Contextual help: one accessible tooltip
// component driven by the centralised registry (static/help/registry.json).
//
// Usage: give an element `data-help="<key>"`. On hover and on keyboard focus
// the short description appears in a positioned `role="tooltip"` element the
// control points at via `aria-describedby`; icon-only controls receive an
// `aria-label` from the registry label when they have no accessible name.
// Entries with `extended`/`docs` get a small "?" affordance (`data-help-more`)
// that opens a popover with the extended text and a link to the local Help.
// `Esc` closes the popover without touching annotation selection.
//
// Nothing here talks to the network beyond loading the local registry JSON.

let registry = null;
let shortcuts = [];
let tooltipEl = null;
let popoverEl = null;
let activeTarget = null;
let hideTimer = null;

const KIND_LABEL = {
  canonical: "Changes annotation data (undoable)",
  view: "View only — nothing is saved or exported",
  review: "Changes review state only — never geometry",
  validation: "Recomputes validation — no region is changed",
  navigation: "Selection / navigation — no data change",
  destructive: "Removes data — confirm first; undo available where stated",
};

export async function loadHelpRegistry() {
  if (registry) return registry;
  try {
    const response = await fetch("/api/v1/help/registry", { headers: { "X-Requested-With": "AFMLabel" } });
    const data = await response.json();
    registry = data.entries || {};
    shortcuts = data.shortcuts || [];
  } catch (error) {
    registry = {};
  }
  return registry;
}

export function helpEntry(key) {
  return registry ? registry[key] || null : null;
}

export function helpShortcuts() {
  return shortcuts;
}

function ensureElements() {
  if (tooltipEl) return;
  tooltipEl = document.createElement("div");
  tooltipEl.id = "lv-tooltip";
  tooltipEl.className = "lv-tooltip";
  tooltipEl.setAttribute("role", "tooltip");
  tooltipEl.hidden = true;
  document.body.append(tooltipEl);
  popoverEl = document.createElement("div");
  popoverEl.id = "lv-help-popover";
  popoverEl.className = "lv-help-popover";
  popoverEl.setAttribute("role", "dialog");
  popoverEl.setAttribute("aria-modal", "false");
  popoverEl.hidden = true;
  document.body.append(popoverEl);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !popoverEl.hidden) { closePopover(); e.stopPropagation(); }
  }, true);
  document.addEventListener("pointerdown", (e) => {
    if (!popoverEl.hidden && !popoverEl.contains(e.target) && !e.target.closest("[data-help-more]")) closePopover();
  });
  // Scrolling (including scrollIntoView on focus/hover) keeps the tooltip
  // attached to its control instead of dismissing it.
  window.addEventListener("scroll", () => { if (activeTarget && !tooltipEl.hidden) position(tooltipEl, activeTarget); }, true);
}

function position(el, target, preferBelow = true) {
  const rect = target.getBoundingClientRect();
  el.style.left = "0px";
  el.style.top = "0px";
  el.hidden = false;
  const size = el.getBoundingClientRect();
  let left = rect.left + rect.width / 2 - size.width / 2;
  left = Math.max(6, Math.min(window.innerWidth - size.width - 6, left));
  let top = preferBelow ? rect.bottom + 6 : rect.top - size.height - 6;
  if (top + size.height > window.innerHeight - 6) top = rect.top - size.height - 6;
  if (top < 6) top = rect.bottom + 6;
  el.style.left = `${Math.round(left)}px`;
  el.style.top = `${Math.round(top)}px`;
}

function describe(target) {
  const entry = helpEntry(target.dataset.help);
  if (!entry) return null;
  const shortcut = target.dataset.helpShortcut || entry.shortcut;
  return { entry, shortcut };
}

export function showTooltip(target) {
  ensureElements();
  const info = describe(target);
  if (!info) return;
  clearTimeout(hideTimer);
  activeTarget = target;
  const { entry, shortcut } = info;
  const dynamic = target.dataset.helpNote ? `<div class="lv-tooltip-note">${escapeHtml(target.dataset.helpNote)}</div>` : "";
  tooltipEl.innerHTML = `<div class="lv-tooltip-title">${escapeHtml(entry.label)}${shortcut ? ` <kbd>${escapeHtml(shortcut)}</kbd>` : ""}</div>` +
    `<div class="lv-tooltip-body">${escapeHtml(entry.short)}</div>${dynamic}` +
    `<div class="lv-tooltip-kind lv-kind-${entry.kind}">${KIND_LABEL[entry.kind] || entry.kind}</div>`;
  position(tooltipEl, target);
  target.setAttribute("aria-describedby", "lv-tooltip");
}

export function hideTooltip() {
  if (!tooltipEl) return;
  tooltipEl.hidden = true;
  if (activeTarget) activeTarget.removeAttribute("aria-describedby");
  activeTarget = null;
}

export function openPopover(target) {
  ensureElements();
  const info = describe(target);
  if (!info) return;
  hideTooltip();
  const { entry, shortcut } = info;
  const docs = entry.docs ? `/help/${entry.docs}` : null;
  popoverEl.innerHTML = `<div class="lv-help-popover-header"><strong>${escapeHtml(entry.label)}</strong>${shortcut ? ` <kbd>${escapeHtml(shortcut)}</kbd>` : ""}<button type="button" class="lv-help-popover-close" aria-label="Close help">&times;</button></div>` +
    `<p>${escapeHtml(entry.short)}</p>` +
    (entry.extended ? `<p>${escapeHtml(entry.extended)}</p>` : "") +
    `<p class="lv-tooltip-kind lv-kind-${entry.kind}">${KIND_LABEL[entry.kind] || entry.kind}</p>` +
    (docs ? `<p><a href="${docs}" target="_blank" rel="noopener">Open in Help</a></p>` : "");
  popoverEl.querySelector(".lv-help-popover-close").addEventListener("click", closePopover);
  position(popoverEl, target);
  popoverEl.querySelector(".lv-help-popover-close").focus();
}

export function closePopover() {
  if (popoverEl) popoverEl.hidden = true;
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function hasAccessibleName(el) {
  if (el.getAttribute("aria-label") || el.getAttribute("aria-labelledby")) return true;
  if (el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`)) return true;
  if (el.closest("label")) return true;
  return (el.textContent || "").trim().length > 0;
}

// Apply registry-derived attributes to every `[data-help]` element inside
// `root`: accessible name for icon-only controls, `title` removed (the custom
// tooltip replaces it, avoiding a double tooltip), and the "?" affordance for
// entries that have extended help or a docs anchor.
export function applyHelp(root = document) {
  if (!registry) return;
  for (const el of root.querySelectorAll("[data-help]")) {
    const entry = registry[el.dataset.help];
    if (!entry) continue;
    if (!hasAccessibleName(el)) el.setAttribute("aria-label", entry.label);
    if (el.hasAttribute("title") && !el.dataset.helpKeepTitle) el.removeAttribute("title");
    if (el.dataset.helpBound) continue;
    el.dataset.helpBound = "1";
    el.addEventListener("mouseenter", () => showTooltip(el));
    el.addEventListener("mouseleave", () => { hideTimer = setTimeout(hideTooltip, 60); });
    el.addEventListener("focus", () => showTooltip(el));
    el.addEventListener("blur", hideTooltip);
    el.addEventListener("pointerdown", () => { hideTimer = setTimeout(hideTooltip, 150); });
    if ((entry.extended || entry.docs) && el.dataset.helpMoreInline !== undefined) {
      const more = document.createElement("button");
      more.type = "button";
      more.className = "lv-help-more";
      more.dataset.helpMore = el.dataset.help;
      more.setAttribute("aria-label", `More help: ${entry.label}`);
      more.textContent = "?";
      more.addEventListener("click", (e) => { e.stopPropagation(); e.preventDefault(); openPopover(el); });
      el.insertAdjacentElement("afterend", more);
    }
  }
  for (const btn of root.querySelectorAll("[data-help-more]:not([data-help-bound])")) {
    btn.dataset.helpBound = "1";
    const target = document.querySelector(`[data-help="${btn.dataset.helpMore}"]`) || btn;
    if (!target.dataset.help) target.dataset.help = btn.dataset.helpMore;
    btn.addEventListener("click", (e) => { e.stopPropagation(); e.preventDefault(); openPopover(target); });
  }
}

// Development-time audit: interactive elements without
// an accessible name or without a help key. Returned, and logged when
// `?help-audit` is in the URL; the Playwright test calls it directly.
export function auditHelpCoverage(root = document) {
  const problems = [];
  const selector = "button, a[href], input:not([type=hidden]), select, textarea, [role=button], [role=tab], [draggable=true]";
  for (const el of root.querySelectorAll(selector)) {
    if (el.closest("[hidden]") || el.classList.contains("lv-help-more") || el.classList.contains("lv-help-popover-close")) continue;
    const key = el.dataset.help;
    const trivial = el.dataset.helpTrivial !== undefined;
    if (!hasAccessibleName(el)) problems.push({ element: describeElement(el), problem: "no accessible name" });
    if (!key && !trivial) problems.push({ element: describeElement(el), problem: "no data-help key" });
    if (key && !registry[key]) problems.push({ element: describeElement(el), problem: `unknown help key ${key}` });
  }
  return problems;
}

function describeElement(el) {
  return `${el.tagName.toLowerCase()}${el.id ? `#${el.id}` : ""}${el.className && typeof el.className === "string" ? `.${el.className.split(" ").filter(Boolean).join(".")}` : ""}`;
}

export async function installHelp(root = document) {
  await loadHelpRegistry();
  applyHelp(root);
  window.__afmlabelHelp = { audit: () => auditHelpCoverage(root), registry, shortcuts, apply: () => applyHelp(root) };
  if (window.location.search.includes("help-audit")) {
    const problems = auditHelpCoverage(root);
    // eslint-disable-next-line no-console
    console.table(problems);
  }
}
