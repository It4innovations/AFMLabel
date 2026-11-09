// SPDX-License-Identifier: MIT
// Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
// Segmentation backend picker, prompt collection, inference jobs and
// proposal handling. Capability-driven: only
// controls advertised by the selected backend descriptor are enabled.
// A proposal is drawn as a distinct white overlay and never touches the
// canonical regions until the user accepts it.

import { api } from "./api.js";
import { decodeRows } from "./masks.js";

export function setUpSegmentation({ base, canvas, statusBar, getState, say, createRegion, applyToSelected }) {
  const select = document.getElementById("backend-select");
  const actions = document.getElementById("proposal-actions");
  const hint = document.getElementById("proposal-hint");
  const variantSelect = document.getElementById("proposal-select");
  const refineCheckbox = document.getElementById("prompt-refine-selected");
  const refineLabel = document.getElementById("prompt-refine-label");
  const textInput = document.getElementById("prompt-text");
  let backends = [];
  let selected = null;
  const prompts = { points: [], box: null, text: null };
  let job = null;
  let pollTimer = null;
  let proposals = [];
  let debounce = null;

  function descriptor() {
    return backends.find((b) => b.backend_id === selected) || null;
  }

  function applyCapabilities() {
    const d = descriptor();
    const caps = new Set(d && d.available ? d.capabilities : []);
    const map = { sam_positive: "point_prompts", sam_negative: "point_prompts", sam_box: "box_prompts" };
    for (const [tool, capability] of Object.entries(map)) {
      const button = document.querySelector(`.tool-button[data-tool="${tool}"]`);
      const enabled = caps.has(capability);
      button.disabled = !enabled;
      // The custom tooltip (help.js) shows the registry text plus this note.
      button.dataset.helpNote = enabled ? "" : `Unavailable: ${d ? (d.available ? `${capability} not supported by ${d.backend_id}` : d.unavailable_reason) : "no backend"}`;
      if (!enabled && button.classList.contains("active")) {
        document.querySelector('.tool-button[data-tool="select"]').click();
      }
    }
    const reason = (capability) => (d ? (d.available ? `${capability} not supported by ${d.backend_id}` : d.unavailable_reason) : "no backend");
    const maskOk = caps.has("mask_prompts");
    refineCheckbox.disabled = !maskOk;
    if (!maskOk) refineCheckbox.checked = false;
    refineLabel.dataset.helpNote = maskOk ? "" : `Unavailable: ${reason("mask_prompts")}`;
    const textOk = caps.has("text_prompts");
    textInput.disabled = !textOk;
    if (!textOk) textInput.value = "";
    textInput.dataset.helpNote = textOk ? "" : `Unavailable: ${reason("text_prompts")}`;
  }

  function optionLabel(b) {
    if (b.available) return b.display_name;
    const state = { not_loaded: "not loaded", loading: "loading…", failed: "load failed" }[b.load_state] || "unavailable";
    return `${b.display_name} (${state})`;
  }

  function renderOptions() {
    const current = select.value;
    select.replaceChildren(...backends.map((b) => {
      const option = document.createElement("option");
      option.value = b.backend_id;
      option.textContent = optionLabel(b);
      option.title = b.available ? "" : b.unavailable_reason;
      return option;
    }));
    select.value = selected || current || "";
  }

  async function refresh() {
    const models = await statusBar.refreshBackends();
    backends = models ? models.backends : [];
    renderOptions();
    return models;
  }

  async function init(preferredId) {
    const models = await refresh();
    const candidates = [preferredId, models ? models.default_backend_id : null, ...backends.filter((b) => b.available).map((b) => b.backend_id)];
    selected = candidates.find((id) => id && backends.some((b) => b.backend_id === id)) || (backends[0] ? backends[0].backend_id : null);
    select.value = selected || "";
    applyCapabilities();
    ensureLoaded();
  }

  let loadPoll = null;

  // Real models load on demand: selecting a backend that is configured but
  // not loaded starts the load (tens of seconds, several GB) and polls the
  // model list until it is usable or the load failed with a reason.
  async function ensureLoaded() {
    const d = descriptor();
    clearTimeout(loadPoll);
    if (!d || d.available || !["not_loaded", "loading"].includes(d.load_state)) return;
    const id = d.backend_id;
    if (d.load_state === "not_loaded") {
      try {
        await api(`/api/v1/models/${id}/load`, { method: "POST" });
      } catch (err) {
        say(`${d.display_name}: ${err.message}`, 8000);
        return;
      }
    }
    say(`${d.display_name}: loading the model, this can take up to a minute…`, 6000);
    const started = Date.now();
    const poll = async () => {
      await refresh();
      const now = descriptor();
      if (!now || now.backend_id !== id) return;
      if (now.available) {
        applyCapabilities();
        say(`${now.display_name} loaded (${now.device || "device n/a"}): ${now.capabilities.join(", ")}.`, 6000);
        return;
      }
      if (now.load_state === "failed" || now.load_state === "unavailable") {
        applyCapabilities();
        say(`${now.display_name}: ${now.unavailable_reason}`, 10000);
        return;
      }
      if (Date.now() - started > 10 * 60 * 1000) { say(`${now.display_name}: still loading; check the server log.`, 8000); return; }
      loadPoll = setTimeout(poll, 2000);
    };
    loadPoll = setTimeout(poll, 2000);
  }

  select.addEventListener("change", () => {
    selected = select.value;
    clearPrompts();
    applyCapabilities();
    const d = descriptor();
    if (d && !d.available && !["not_loaded", "loading"].includes(d.load_state)) say(`${d.display_name}: ${d.unavailable_reason}`, 8000);
    ensureLoaded();
  });

  function clearPrompts() {
    prompts.points = [];
    prompts.box = null;
    prompts.text = null;
    textInput.value = "";
    proposals = [];
    canvas.setPrompts(prompts);
    canvas.setProposal(null);
    actions.hidden = true;
    hint.textContent = "Proposal";
    variantSelect.replaceChildren();
    clearTimeout(debounce);
    if (job && !isTerminal(job)) {
      api(`${base}/jobs/${job.job_id}/cancel`, { method: "POST" }).catch(() => {});
    }
    job = null;
    clearTimeout(pollTimer);
  }

  function isTerminal(record) {
    return ["succeeded", "failed", "cancelled"].includes(record.status);
  }

  function onPrompt(prompt) {
    if (prompt.type === "point") {
      prompts.points.push({ x: prompt.x, y: prompt.y, positive: prompt.positive });
      // Points drive the instance predictor; a pending text prompt would be
      // rejected by the backend as a mixed prompt, so it is dropped explicitly.
      if (prompts.text) { prompts.text = null; textInput.value = ""; say("Text prompt cleared: points and text cannot be combined.", 4000); }
    } else {
      prompts.box = prompt.box;
    }
    canvas.setPrompts(prompts);
    clearTimeout(debounce);
    debounce = setTimeout(submit, 250);
  }

  function submitText() {
    const value = textInput.value.trim();
    if (!value) { say("Type a text prompt first.", 3000); return; }
    prompts.text = value;
    prompts.points = [];
    canvas.setPrompts(prompts);
    clearTimeout(debounce);
    submit();
  }

  textInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); submitText(); }
    event.stopPropagation();
  });
  textInput.addEventListener("keyup", (event) => event.stopPropagation());
  refineCheckbox.addEventListener("change", () => {
    if (prompts.points.length || prompts.box || prompts.text) { clearTimeout(debounce); debounce = setTimeout(submit, 100); }
  });

  async function submit() {
    const d = descriptor();
    if (!d || !d.available) { say(d ? d.unavailable_reason : "no backend selected", 6000); return; }
    const state = getState();
    const body = {
      backend_id: selected,
      channel: state.channel,
      points: prompts.points.map((p) => ({ x: p.x, y: p.y, positive: p.positive })),
      box_xyxy: prompts.box,
    };
    if (prompts.text) body.text_prompt = prompts.text;
    if (refineCheckbox.checked && !refineCheckbox.disabled) {
      if (!state.selectedId) { say("Refine selected region: select a region first.", 4000); return; }
      body.previous_region_id = state.selectedId;
    }
    if (job && !isTerminal(job)) {
      api(`${base}/jobs/${job.job_id}/cancel`, { method: "POST" }).catch(() => {});
    }
    actions.hidden = false;
    hint.textContent = "Proposal: submitting…";
    try {
      job = await api(`${base}/segment`, { method: "POST", body });
      poll();
    } catch (err) {
      hint.textContent = `Proposal failed: ${err.message}`;
      say(err.recovery ? `${err.message}. ${err.recovery}.` : err.message, 8000);
    }
  }

  function poll() {
    clearTimeout(pollTimer);
    if (!job) return;
    pollTimer = setTimeout(async () => {
      try {
        const record = await api(`${base}/jobs/${job.job_id}`);
        if (!job || record.job_id !== job.job_id) return;
        job = record;
        if (record.status === "succeeded") {
          showProposals(record);
        } else if (record.status === "failed") {
          hint.textContent = `Proposal failed: ${record.error ? record.error.message : "unknown error"}`;
          say(record.error && record.error.recovery ? `${record.error.message}. ${record.error.recovery}.` : hint.textContent, 8000);
        } else if (record.status === "cancelled") {
          hint.textContent = "Proposal cancelled";
        } else {
          hint.textContent = `Proposal: ${record.status} ${Math.round((record.progress || 0) * 100)}% ${record.message || ""}`;
          poll();
        }
      } catch (err) {
        hint.textContent = `Proposal: ${err.message}`;
      }
    }, 300);
  }

  function showProposals(record) {
    const state = getState();
    proposals = record.result.proposals.map((p, index) => ({
      index,
      mask: decodeRows(p.mask.counts, state.regions.length >= 0 ? canvas.width : canvas.width, canvas.height),
      score: p.score,
      area: p.area,
      provenance: record.result.provenance,
      metadata: p.backend_metadata,
    }));
    variantSelect.replaceChildren(...proposals.map((p) => {
      const option = document.createElement("option");
      option.value = String(p.index);
      option.textContent = `#${p.index + 1}: ${p.area} px${p.score !== null ? `, score ${p.score.toFixed(2)}` : ""}${p.metadata.variant ? ` (${p.metadata.variant})` : ""}`;
      return option;
    }));
    variantSelect.value = "0";
    hint.textContent = `Proposal from ${record.backend_id}: ${proposals.length} variant(s). Accept, add to the selected region, or reject.`;
    canvas.setProposal(proposals[0].mask);
  }

  function currentProposal() {
    return proposals[Number(variantSelect.value)] || null;
  }

  variantSelect.addEventListener("change", () => {
    const p = currentProposal();
    if (p) canvas.setProposal(p.mask);
  });

  document.getElementById("proposal-accept-button").addEventListener("click", async () => {
    const p = currentProposal();
    if (!p) return;
    const created = await createRegion(p.mask, { ...p.provenance, proposal_index: p.index, proposal_score: p.score });
    if (created) { say(`Accepted proposal as ${created.region_id}.`, 3000); clearPrompts(); }
  });
  document.getElementById("proposal-add-button").addEventListener("click", () => {
    const p = currentProposal();
    if (!p) return;
    if (applyToSelected(p.mask, "add")) { say("Proposal merged into the selected region.", 3000); clearPrompts(); }
    else say("Select a region first.", 3000);
  });
  document.getElementById("proposal-reject-button").addEventListener("click", () => { clearPrompts(); say("Proposal rejected.", 2000); });
  document.getElementById("proposal-clear-button").addEventListener("click", clearPrompts);

  return { init, onPrompt, clearPrompts, selectedBackendId: () => selected, prompts };
}
