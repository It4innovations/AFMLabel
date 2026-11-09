// SPDX-License-Identifier: MIT
// Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
// Project list page: search, create, delete, logout.

import { api } from "./api.js";
import { setUpLogout } from "./session.js";

setUpLogout();

// ---- search -------------------------------------------------------------
const search = document.getElementById("project-search");
const cards = () => Array.from(document.querySelectorAll("#project-list .lv-card"));
const noResults = document.getElementById("no-search-results");

function applySearch() {
  const query = (search.value || "").trim().toLowerCase();
  let visible = 0;
  for (const card of cards()) {
    const match = !query || card.dataset.projectName.includes(query);
    card.hidden = !match;
    if (match) visible += 1;
  }
  noResults.hidden = !(query && visible === 0 && cards().length > 0);
}
search.addEventListener("input", applySearch);
document.getElementById("project-search-button").addEventListener("click", applySearch);

// ---- create --------------------------------------------------------------
const createModal = document.getElementById("create-project-modal");
const createForm = document.getElementById("create-project-form");
const createError = document.getElementById("create-project-error");
const nameInput = document.getElementById("project_name");
const channelCountInput = document.getElementById("project_channel_count");

function openCreate() {
  createError.textContent = "";
  createModal.hidden = false;
  nameInput.focus();
}
function closeCreate() {
  createModal.hidden = true;
}
document.getElementById("create-project-button").addEventListener("click", openCreate);
document.getElementById("close-create-modal-button").addEventListener("click", closeCreate);
document.getElementById("cancel-create-modal-button").addEventListener("click", closeCreate);
createForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  createError.textContent = "";
  try {
    const channelCount = Math.max(1, Math.min(64, Math.round(Number(channelCountInput.value) || 2)));
    const project = await api("/api/v1/projects", { method: "POST", body: { name: nameInput.value, channel_count: channelCount } });
    window.location.href = `/projects/${project.project_id}`;
  } catch (err) {
    createError.textContent = err.message;
  }
});

// ---- delete --------------------------------------------------------------
const deleteModal = document.getElementById("delete-project-modal");
const deleteName = document.getElementById("delete-project-name");
const deleteError = document.getElementById("delete-project-error");
let pendingDelete = null;

for (const button of document.querySelectorAll(".lv-delete-button")) {
  button.addEventListener("click", () => {
    pendingDelete = { id: button.dataset.projectId, revision: button.dataset.revision };
    deleteName.textContent = button.dataset.projectTitle;
    deleteError.textContent = "";
    deleteModal.hidden = false;
  });
}
const closeDelete = () => { deleteModal.hidden = true; pendingDelete = null; };
document.getElementById("close-delete-modal-button").addEventListener("click", closeDelete);
document.getElementById("cancel-delete-modal-button").addEventListener("click", closeDelete);
document.getElementById("confirm-delete-project-button").addEventListener("click", async () => {
  if (!pendingDelete) return;
  try {
    await api(`/api/v1/projects/${pendingDelete.id}?expected_revision=${pendingDelete.revision}`, { method: "DELETE" });
    window.location.reload();
  } catch (err) {
    deleteError.textContent = err.recovery ? `${err.message}. ${err.recovery}.` : err.message;
  }
});

for (const modal of [createModal, deleteModal]) {
  modal.addEventListener("click", (event) => { if (event.target === modal) modal.hidden = true; });
}
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") { createModal.hidden = true; deleteModal.hidden = true; }
});
