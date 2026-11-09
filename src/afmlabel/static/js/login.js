// SPDX-License-Identifier: MIT
// Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
// Login form: posts JSON to /api/v1/auth/login and follows the ?next= target.

import { api } from "./api.js";

const form = document.getElementById("login-form");
const error = document.getElementById("login-error");
const button = document.getElementById("login-button");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  error.textContent = "";
  button.disabled = true;
  try {
    await api("/api/v1/auth/login", {
      method: "POST",
      allowUnauthenticated: true,
      body: {
        username: document.getElementById("username").value,
        password: document.getElementById("password").value,
      },
    });
    const next = form.dataset.next || "/";
    window.location.href = next.startsWith("/") ? next : "/";
  } catch (err) {
    error.textContent = err.recovery ? `${err.message}. ${err.recovery}.` : err.message;
    button.disabled = false;
  }
});
