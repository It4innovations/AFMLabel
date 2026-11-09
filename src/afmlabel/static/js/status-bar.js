// SPDX-License-Identifier: MIT
// Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
// Bottom status bar: coverage figures from the canvas plus live host
// resources and backend state. Metric failures render as
// text; they never interrupt annotation.

import { api, formatBytes } from "./api.js";

export class StatusBar {
  constructor(refreshSeconds = 2) {
    this.el = (id) => document.getElementById(id);
    this.timer = setInterval(() => this.refreshResources(), refreshSeconds * 1000);
    this.refreshResources();
    this.refreshBackends();
  }

  setCoverage(stats) {
    const pct = stats.total ? (100 * stats.labeled) / stats.total : 0;
    const coverage = this.el("status-coverage");
    coverage.textContent = `Coverage: ${pct.toFixed(2)} %`;
    coverage.className = "lv-status-item lv-status-clickable " + (stats.missing === 0 ? "good" : "warn");
    const missing = this.el("status-missing");
    missing.textContent = `Missing: ${stats.missing} px`;
    missing.className = "lv-status-item lv-status-clickable " + (stats.missing === 0 ? "good" : "warn");
  }

  setOverlaps(groupCount, maxMultiplicity, unreviewed) {
    const el = this.el("status-overlaps");
    const suffix = groupCount ? ` (max ×${maxMultiplicity}${unreviewed ? `, ${unreviewed} unreviewed` : ""})` : "";
    el.textContent = `Overlaps: ${groupCount}${suffix}`;
    el.className = "lv-status-item lv-status-clickable " + (unreviewed ? "bad" : groupCount ? "warn" : "good");
  }

  setValidation(text, tone) {
    const el = this.el("status-validation");
    el.textContent = `Validation: ${text}`;
    el.className = "lv-status-item " + (tone || "");
  }

  async refreshResources() {
    try {
      const s = await api("/api/v1/system/resources");
      const cpu = this.el("status-cpu");
      cpu.textContent = s.cpu.error ? "CPU n/a" : `CPU ${s.cpu.host_percent.toFixed(0)} %`;
      const ram = this.el("status-ram");
      if (s.memory.error) {
        ram.textContent = "RAM n/a";
      } else {
        ram.textContent = `RAM ${formatBytes(s.memory.available_bytes)} free · RSS ${formatBytes(s.memory.process_rss_bytes)}`;
      }
      ram.className = "lv-status-item " + (s.warnings.some((w) => w.code === "RAM_LOW") ? "warn" : "");
      const disk = this.el("status-disk");
      disk.textContent = s.disk.error ? "Disk n/a" : `Disk ${formatBytes(s.disk.free_bytes)} free`;
      disk.className = "lv-status-item " + (s.warnings.some((w) => w.code === "DISK_SPACE_LOW") ? "warn" : "");
      const gpu = this.el("status-gpu");
      if (s.gpu.error) gpu.textContent = "GPU n/a";
      else if (!s.gpu.devices.length) gpu.textContent = "GPU none";
      else gpu.textContent = s.gpu.devices.map((d) => `GPU${d.index} ${formatBytes(d.memory_used_bytes)}/${formatBytes(d.memory_total_bytes)}`).join(" ");
    } catch (error) {
      this.el("status-cpu").textContent = "resources unavailable";
    }
  }

  async refreshBackends() {
    try {
      const models = await api("/api/v1/models");
      const available = models.backends.filter((b) => b.available).map((b) => b.backend_id);
      const el = this.el("status-backend");
      el.textContent = available.length ? `Backend: ${available.join(", ")}` : "Backend: none available";
      el.title = models.backends.map((b) => `${b.backend_id}: ${b.available ? "available" : b.unavailable_reason}`).join("\n");
      return models;
    } catch (error) {
      this.el("status-backend").textContent = "Backend: n/a";
      return null;
    }
  }
}
