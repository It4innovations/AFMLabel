// SPDX-License-Identifier: MIT
// Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
// Thin Fetch wrapper for the versioned API. Every state-changing request
// carries the same-origin CSRF header the backend requires;
// errors are normalised to {code, message, target, recovery, status}.

export class ApiError extends Error {
  constructor(status, payload) {
    const error = (payload && payload.error) || {};
    super(error.message || `HTTP ${status}`);
    this.status = status;
    this.code = error.code || "UNKNOWN";
    this.target = error.target || {};
    this.recovery = error.recovery || null;
    this.details = error.details || null;
  }
}

export async function api(path, options = {}) {
  const init = { method: options.method || "GET", headers: { "X-Requested-With": "AFMLabel" } };
  if (options.body !== undefined) {
    if (options.body instanceof FormData) {
      init.body = options.body;
    } else {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
  }
  if (options.signal) init.signal = options.signal;
  const response = await fetch(path, init);
  if (response.status === 401 && !options.allowUnauthenticated) {
    window.location.href = "/login?next=" + encodeURIComponent(window.location.pathname);
    throw new ApiError(401, { error: { code: "AUTH_REQUIRED", message: "login required" } });
  }
  const contentType = response.headers.get("content-type") || "";
  let payload = null;
  if (contentType.includes("application/json")) {
    payload = await response.json();
  }
  if (!response.ok) {
    throw new ApiError(response.status, payload);
  }
  return payload === null ? response : payload;
}

export function formatBytes(bytes) {
  if (bytes === null || bytes === undefined) return "n/a";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(index >= 2 ? 1 : 0)} ${units[index]}`;
}
