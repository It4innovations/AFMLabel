// SPDX-License-Identifier: MIT
// Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
// Dataset search/filter state shared by the project page and the workspace.
// The filter text lives in sessionStorage per
// project so it survives opening a dataset and coming back, and so
// Previous/Next in the workspace can follow the filtered list. It is
// browser/session state only: never sent to the server as project state.

const PREFIX = "afmlabel.datasetFilter.";

export function readDatasetFilter(projectId) {
  try {
    return (window.sessionStorage.getItem(PREFIX + projectId) || "").trim();
  } catch (error) {
    return "";
  }
}

export function writeDatasetFilter(projectId, value) {
  try {
    if (value && value.trim()) window.sessionStorage.setItem(PREFIX + projectId, value.trim());
    else window.sessionStorage.removeItem(PREFIX + projectId);
  } catch (error) {
    /* storage unavailable: the filter still works for this page */
  }
}

// Case-insensitive substring match on metadata only (mirrors the server's
// dataset_matches so client and server filtering agree).
export function datasetMatches(summary, query) {
  const needle = (query || "").trim().toLowerCase();
  if (!needle) return true;
  const filenames = (summary.sources || []).map((s) => s.original_filename);
  return [...filenames, summary.pairing_key, summary.name, summary.dataset_id]
    .some((value) => String(value || "").toLowerCase().includes(needle));
}

export function debounce(fn, ms) {
  let timer = null;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}
