// SPDX-License-Identifier: MIT
// Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
// Session helpers: logout button and the per-tab editor identity used for
// project locks. The editor id lives in sessionStorage so each
// browser tab is its own editing session.

import { api } from "./api.js";

const EDITOR_KEY = "afmlabel-editor-id";

export function editorId() {
  try {
    let id = window.sessionStorage.getItem(EDITOR_KEY);
    if (!id) {
      id = (window.crypto && window.crypto.randomUUID) ? window.crypto.randomUUID() : String(Math.random()).slice(2);
      window.sessionStorage.setItem(EDITOR_KEY, id);
    }
    return id;
  } catch (error) {
    return "tab-" + String(Math.random()).slice(2);
  }
}

export function setUpLogout() {
  const button = document.getElementById("logout-button");
  if (!button) return;
  button.addEventListener("click", async () => {
    try {
      await api("/api/v1/auth/logout", { method: "POST", allowUnauthenticated: true });
    } finally {
      window.location.href = "/login";
    }
  });
}
