// SPDX-License-Identifier: MIT
// Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
// Light/dark theme toggle (LabVIST convention). The stored choice is applied
// by a tiny inline script in each template's <head> before first paint; this
// module only wires the header button and keeps its label in sync.

const STORAGE_KEY = "afmlabel-theme";

const SUN =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
  'stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2' +
  'M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
const MOON =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
  'stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>';

function currentTheme() {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

function render(button) {
  const theme = currentTheme();
  // Show what clicking will switch *to*, which is what users scan for.
  button.innerHTML = theme === "light" ? MOON : SUN;
  button.title = theme === "light" ? "Switch to dark theme" : "Switch to light theme";
  button.setAttribute("aria-label", button.title);
}

export function setUpThemeToggle(buttonId) {
  const button = document.getElementById(buttonId);
  if (!button) return;
  render(button);
  button.addEventListener("click", () => {
    const next = currentTheme() === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    try {
      window.localStorage.setItem(STORAGE_KEY, next);
    } catch (error) {
      // Private mode / disabled storage: the toggle still works for this
      // page load, it just will not be remembered.
    }
    render(button);
  });
}
