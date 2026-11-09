// SPDX-License-Identifier: MIT
// Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
// System status dialog: environment checks, live resources and backend
// descriptors. Failures of any single call render as text in
// the dialog; they never throw into the page.

import { api, formatBytes } from "./api.js";

function row(list, key, value, tone) {
  const dt = document.createElement("dt");
  dt.textContent = key;
  const dd = document.createElement("dd");
  dd.textContent = value;
  if (tone) dd.className = tone;
  list.append(dt, dd);
}

function toneFor(status) {
  if (status === "ok") return "lv-tone-ok";
  if (status === "missing") return "";
  return "lv-tone-warn";
}

async function renderHealth(list) {
  list.replaceChildren();
  try {
    const health = await api("/api/v1/health", { allowUnauthenticated: true });
    row(list, "Overall", health.status, toneFor(health.status));
    row(list, "Version", health.package_version);
    row(list, "Platform", health.platform);
    for (const key of ["python", "filesystem", "memory", "disk", "gpu", "torch"]) {
      const check = health[key];
      row(list, key, `${check.status}: ${check.detail}`, toneFor(check.status));
    }
  } catch (error) {
    row(list, "Error", error.message, "lv-tone-warn");
  }
}

export function describeResources(snapshot) {
  const rows = [];
  const cpu = snapshot.cpu;
  rows.push(["CPU (host)", cpu.error ? cpu.error.message : `${cpu.host_percent.toFixed(0)} % of ${cpu.logical_cores} cores`]);
  if (!cpu.error) rows.push(["CPU (AFMLabel)", `${cpu.process_percent.toFixed(0)} %`]);
  const mem = snapshot.memory;
  if (mem.error) {
    rows.push(["RAM", mem.error.message]);
  } else {
    rows.push(["RAM", `${formatBytes(mem.available_bytes)} available of ${formatBytes(mem.total_bytes)}`]);
    rows.push(["Process RSS", formatBytes(mem.process_rss_bytes)]);
  }
  const disk = snapshot.disk;
  rows.push(["Workspace disk", disk.error ? disk.error.message : `${formatBytes(disk.free_bytes)} free of ${formatBytes(disk.total_bytes)} (${disk.path})`]);
  const gpu = snapshot.gpu;
  if (gpu.error) {
    rows.push(["GPU", gpu.error.message]);
  } else if (!gpu.devices.length) {
    rows.push(["GPU", "none detected (CPU-only profile)"]);
  } else {
    for (const device of gpu.devices) {
      rows.push([`GPU ${device.index}`, `${device.name}: ${formatBytes(device.memory_used_bytes)} / ${formatBytes(device.memory_total_bytes)}, ${device.utilization_percent} %`]);
    }
  }
  for (const warning of snapshot.warnings) {
    rows.push([warning.code, warning.message]);
  }
  return rows;
}

async function renderResources(list) {
  list.replaceChildren();
  try {
    const snapshot = await api("/api/v1/system/resources", { allowUnauthenticated: true });
    for (const [key, value] of describeResources(snapshot)) {
      const tone = key === "DISK_SPACE_LOW" || key === "RAM_LOW" || key === "RESOURCE_METRIC_UNAVAILABLE" ? "lv-tone-warn" : "";
      row(list, key, value, tone);
    }
  } catch (error) {
    row(list, "Error", error.message, "lv-tone-warn");
  }
}

async function renderBackends(list) {
  list.replaceChildren();
  try {
    const models = await api("/api/v1/models", { allowUnauthenticated: true });
    for (const backend of models.backends) {
      const state = backend.available ? "available" : `unavailable: ${backend.unavailable_reason}`;
      const caps = backend.capabilities.length ? ` [${backend.capabilities.join(", ")}]` : "";
      const defaultMark = backend.backend_id === models.default_backend_id ? " (default)" : "";
      row(list, backend.backend_id + defaultMark, `${backend.display_name} — ${state}${caps}`, backend.available ? "lv-tone-ok" : "");
      if (backend.model_revision) row(list, "  revision", backend.model_revision);
      if (backend.checkpoint_sha256) row(list, "  checkpoint", backend.checkpoint_sha256);
      for (const warning of backend.warnings) row(list, "  note", warning);
    }
  } catch (error) {
    row(list, "Error", error.message, "lv-tone-warn");
  }
}

export function setUpSystemStatusDialog() {
  const modal = document.getElementById("system-status-modal");
  const openButton = document.getElementById("open-system-status-button");
  if (!modal || !openButton) return;
  const healthList = document.getElementById("health-list");
  const resourcesList = document.getElementById("resources-list");
  const backendsList = document.getElementById("backends-list");

  const refresh = () => Promise.all([
    renderHealth(healthList),
    renderResources(resourcesList),
    renderBackends(backendsList),
  ]);
  const close = () => { modal.hidden = true; };

  openButton.addEventListener("click", () => {
    modal.hidden = false;
    refresh();
  });
  document.getElementById("system-status-refresh-button").addEventListener("click", refresh);
  document.getElementById("system-status-close-button").addEventListener("click", close);
  document.getElementById("system-status-dismiss-button").addEventListener("click", close);
  modal.addEventListener("click", (event) => { if (event.target === modal) close(); });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !modal.hidden) close(); });
}
