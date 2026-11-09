// SPDX-License-Identifier: MIT
// Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
// Project editing lock lifecycle shared by the project and workspace pages.

import { api } from "./api.js";
import { editorId } from "./session.js";

export function installEditorHeader() {
  const editor = editorId();
  const originalFetch = window.fetch.bind(window);
  window.fetch = (input, init = {}) => {
    const headers = new Headers(init.headers || {});
    headers.set("X-AFMLabel-Editor", editor);
    return originalFetch(input, Object.assign({}, init, { headers }));
  };
  return { editor, originalFetch };
}

export async function setUpProjectLock(projectId, indicator, { originalFetch, editor }) {
  let readOnly = false;
  const base = `/api/v1/projects/${projectId}/lock`;

  function render(state) {
    if (!state.locked) indicator.textContent = "lock: free";
    else if (state.owned_by_caller) indicator.textContent = "lock: held by this tab";
    else if (state.stale) indicator.textContent = "lock: stale (recoverable)";
    else indicator.textContent = `lock: ${state.info.owner_label} — read-only`;
    indicator.classList.toggle("lv-chip-warn", readOnly);
  }

  try {
    await api(base, { method: "POST", body: { force: false } });
  } catch (err) {
    if (err.code === "PROJECT_LOCKED") {
      const holder = err.details && err.details.holder ? err.details.holder.owner_label : "another session";
      readOnly = !window.confirm(`${holder} is editing this project. Take over the editing lock? (Cancel opens read-only.)`);
      if (!readOnly) await api(base, { method: "POST", body: { force: true } });
    } else {
      indicator.textContent = `lock: ${err.message}`;
    }
  }
  render(await api(base));

  const heartbeat = setInterval(() => {
    if (!readOnly) api(`${base}/heartbeat`, { method: "POST" }).catch(() => {});
  }, 30000);
  window.addEventListener("pagehide", () => {
    clearInterval(heartbeat);
    if (readOnly) return;
    originalFetch(base, {
      method: "DELETE",
      keepalive: true,
      headers: { "X-Requested-With": "AFMLabel", "X-AFMLabel-Editor": editor },
    }).catch(() => {});
  });
  return { readOnly: () => readOnly };
}
