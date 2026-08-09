/**
 * ComfyWebStudio bridge.
 *
 * Two jobs, both inside ComfyUI's own page:
 *   1. Deep-link open — the framework sends the user here with ?ws_open=<base64 json>; we fetch the bound
 *      step's workflow from the framework and load it into the graph.
 *   2. Save back — we hand the framework both the UI graph *and* the API-format prompt, produced by
 *      ComfyUI's own graphToPrompt(). That is why the framework never has to reimplement graph conversion.
 *
 * Deliberately self-contained: no patching of LiteGraph, no mutation of ComfyUI's DOM outside our own badge.
 */

import { app } from "../../../scripts/app.js";

const EXTENSION = "WebStudio.Bridge";
const BINDING_KEY = "comfywebstudio.binding";
const SETTING_AUTOSYNC = "WebStudio.AutoSync";
const AUTOSYNC_DEBOUNCE_MS = 1500;

/** @typedef {{backend:string, stepId:string, token:string, label?:string}} Binding */

const state = {
  /** @type {Binding|null} */ binding: null,
  badge: /** @type {HTMLElement|null} */ (null),
  timer: 0,
  busy: false,
};

// -- binding persistence -------------------------------------------------------------------------------
// The binding has to outlive a page reload: the user may reload ComfyUI mid-edit and still expect the
// "Save to WebStudio" command to know where to send the result.

function loadBinding() {
  try {
    const raw = localStorage.getItem(BINDING_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function storeBinding(binding) {
  state.binding = binding;
  try {
    if (binding) localStorage.setItem(BINDING_KEY, JSON.stringify(binding));
    else localStorage.removeItem(BINDING_KEY);
  } catch {
    /* private browsing; the in-memory binding still works for this session */
  }
}

/** Reads ?ws_open=<base64url json> without disturbing any other query parameter. */
function readOpenRequest() {
  const encoded = new URLSearchParams(window.location.search).get("ws_open");
  if (!encoded) return null;
  try {
    const json = atob(encoded.replace(/-/g, "+").replace(/_/g, "/"));
    const parsed = JSON.parse(json);
    if (!parsed.backend || !parsed.stepId) return null;
    return parsed;
  } catch (err) {
    console.error("[WebStudio] could not decode ws_open parameter", err);
    return null;
  }
}

/** Drops ws_open from the address bar so a refresh does not re-import and discard local edits. */
function clearOpenRequest() {
  const url = new URL(window.location.href);
  url.searchParams.delete("ws_open");
  window.history.replaceState({}, "", url.toString());
}

// -- framework calls -----------------------------------------------------------------------------------

async function frameworkFetch(binding, path, init = {}) {
  const headers = { ...(init.headers || {}) };
  if (binding.token) headers["X-WebStudio-Token"] = binding.token;
  if (init.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";

  const response = await fetch(`${binding.backend.replace(/\/$/, "")}${path}`, { ...init, headers });
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}: ${await response.text()}`);
  }
  return response.json();
}

/**
 * Opens a workflow that ComfyUI already has saved, by path.
 *
 * This is what makes the tab a *named* workflow: Ctrl+S saves it in place instead of prompting for a name
 * and a folder, and the file it saves to is the same one the framework reads back.
 */
async function openSavedWorkflow(path) {
  const store = app.extensionManager?.workflow;
  if (!path || !store?.getWorkflowByPath) return false;

  try {
    // The framework may have only just written the file, so refresh before looking it up.
    await store.syncWorkflows();
    const workflow = store.getWorkflowByPath(path);
    if (!workflow) return false;
    // Deliberately not forcing: if this workflow is already open with unsaved edits, leave them be.
    await store.openWorkflow(workflow);
    return true;
  } catch (err) {
    console.warn("[WebStudio] could not open the saved workflow, falling back", err);
    return false;
  }
}

async function openStep(request) {
  setBadge("Loading…", "pending");
  const payload = await frameworkFetch(request, `/api/bridge/workflow/${encodeURIComponent(request.stepId)}`);

  storeBinding({ ...request, label: payload.label || request.label || request.stepId });

  if (await openSavedWorkflow(payload.comfy_path)) {
    setBadge(`Linked: ${state.binding.label}`, "ok");
    return;
  }

  if (payload.workflow && payload.workflow.nodes?.length) {
    await app.loadGraphData(payload.workflow, true, true, null);
  } else if (payload.prompt) {
    // Imported in API format, so there is no LiteGraph document to load. ComfyUI can build one from the
    // prompt itself; pushing it straight back gives the workflow a saved identity, so the next open —
    // and Ctrl+S right now — behave like any other saved workflow.
    await app.loadApiJson(payload.prompt, payload.name || "WebStudio workflow");
    const result = await saveBack({ silent: true });
    if (result?.comfy_path && (await openSavedWorkflow(result.comfy_path))) {
      setBadge(`Linked: ${state.binding.label}`, "ok");
      return;
    }
  } else {
    throw new Error("the framework returned no graph for this workflow");
  }

  setBadge(`Linked: ${state.binding.label} · save to sync`, "ok");
}

async function saveBack({ silent = false } = {}) {
  const binding = state.binding;
  if (!binding) {
    if (!silent) alert("This ComfyUI tab is not linked to a ComfyWebStudio step.");
    return null;
  }
  if (state.busy) return null;
  state.busy = true;
  setBadge("Syncing…", "pending");
  try {
    // graphToPrompt() is ComfyUI's own converter, so `output` is exactly the API-format prompt the
    // executor would receive. Sending both formats means the framework never has to guess.
    const { workflow, output } = await app.graphToPrompt();
    const result = await frameworkFetch(binding, "/api/bridge/workflow", {
      method: "POST",
      body: JSON.stringify({ step_id: binding.stepId, workflow, prompt: output }),
    });
    const ports = result.port_count ?? 0;
    setBadge(`Synced · ${ports} port${ports === 1 ? "" : "s"}`, "ok");
    return result;
  } catch (err) {
    console.error("[WebStudio] save-back failed", err);
    setBadge("Sync failed", "error");
    if (!silent) alert(`ComfyWebStudio sync failed:\n${err.message}`);
    return null;
  } finally {
    state.busy = false;
  }
}

// -- auto sync -----------------------------------------------------------------------------------------

function scheduleAutoSync() {
  if (!state.binding) return;
  if (!app.extensionManager?.setting?.get(SETTING_AUTOSYNC)) return;
  clearTimeout(state.timer);
  state.timer = setTimeout(() => saveBack({ silent: true }), AUTOSYNC_DEBOUNCE_MS);
}

// -- badge ---------------------------------------------------------------------------------------------

const BADGE_COLORS = {
  ok: "#2e7d32",
  pending: "#8d6e00",
  error: "#b3261e",
  idle: "#37474f",
};

function ensureBadge() {
  if (state.badge) return state.badge;
  const el = document.createElement("div");
  el.id = "comfywebstudio-badge";
  Object.assign(el.style, {
    position: "fixed",
    bottom: "12px",
    left: "12px",
    zIndex: "1200",
    padding: "6px 10px",
    borderRadius: "6px",
    font: "12px/1.3 system-ui, sans-serif",
    color: "#fff",
    background: BADGE_COLORS.idle,
    cursor: "pointer",
    userSelect: "none",
    boxShadow: "0 2px 8px rgba(0,0,0,.35)",
  });
  el.title = "Click to push this workflow back to ComfyWebStudio";
  el.addEventListener("click", () => saveBack());
  document.body.appendChild(el);
  state.badge = el;
  return el;
}

function setBadge(text, tone = "idle") {
  const el = ensureBadge();
  el.textContent = `WebStudio · ${text}`;
  el.style.background = BADGE_COLORS[tone] || BADGE_COLORS.idle;
  el.style.display = "block";
}

// -- extension -----------------------------------------------------------------------------------------

app.registerExtension({
  name: EXTENSION,

  settings: [
    {
      id: SETTING_AUTOSYNC,
      category: ["WebStudio", "Bridge", "Auto-sync"],
      name: "Push graph edits back to ComfyWebStudio automatically",
      tooltip: "When off, use the WebStudio badge or the Save to WebStudio command to sync manually.",
      type: "boolean",
      defaultValue: true,
    },
  ],

  commands: [
    {
      id: "WebStudio.SaveToWebStudio",
      label: "Save to ComfyWebStudio",
      icon: "pi pi-cloud-upload",
      function: () => saveBack(),
    },
    {
      id: "WebStudio.Unlink",
      label: "Unlink from ComfyWebStudio",
      function: () => {
        storeBinding(null);
        setBadge("Not linked", "idle");
      },
    },
  ],

  keybindings: [{ combo: { key: "s", alt: true }, commandId: "WebStudio.SaveToWebStudio" }],

  menuCommands: [{ path: ["Workflow"], commands: ["WebStudio.SaveToWebStudio", "WebStudio.Unlink"] }],

  async setup() {
    state.binding = loadBinding();

    const request = readOpenRequest();
    if (request) {
      clearOpenRequest();
      try {
        await openStep(request);
      } catch (err) {
        console.error("[WebStudio] could not open the requested step", err);
        setBadge("Open failed", "error");
      }
    } else if (state.binding) {
      setBadge(`Linked: ${state.binding.label || state.binding.stepId}`, "ok");
    }

    // onAfterChange is a stable LiteGraph hook; chaining rather than replacing keeps any other
    // extension's handler working.
    const graph = app.graph;
    if (graph) {
      const previous = graph.onAfterChange;
      graph.onAfterChange = function (...args) {
        previous?.apply(this, args);
        scheduleAutoSync();
      };
    }
  },
});
