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
import { api } from "../../../scripts/api.js";

const EXTENSION = "WebStudio.Bridge";
const BINDINGS_KEY = "comfywebstudio.bindings";
const LEGACY_BINDING_KEY = "comfywebstudio.binding";
const SETTING_AUTOSYNC = "WebStudio.AutoSync";
const AUTOSYNC_DEBOUNCE_MS = 1500;
//: Long enough for the workflow store to settle after a save, short enough to feel immediate.
const SAVE_SYNC_DELAY_MS = 250;
const MAX_BINDINGS = 50;

/** @typedef {{backend:string, stepId:string, token:string, label?:string}} Binding */

const state = {
  badge: /** @type {HTMLElement|null} */ (null),
  timer: 0,
  busy: false,
  lastBadgePath: /** @type {string|null|undefined} */ (undefined),
};

// -- binding persistence -------------------------------------------------------------------------------
// Bindings are keyed by the ComfyUI workflow path, and they have to outlive a page reload: the user may
// reload ComfyUI mid-edit and still expect "Save to WebStudio" to know where to send the result.
//
// Keying matters more than it looks. localStorage is shared by every ComfyUI tab on this origin, so a
// single global binding meant whichever workflow was linked *last* received the edits from *any* tab —
// add an input to one workflow and every other one inherited it. The active workflow decides where a
// save goes; a workflow with no binding is simply not ours to push.

/** @returns {Record<string, Binding>} */
function loadBindings() {
  try {
    const raw = localStorage.getItem(BINDINGS_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

function saveBindings(bindings) {
  try {
    // Oldest-first eviction, so a long-lived browser profile cannot grow this without bound.
    const entries = Object.entries(bindings);
    const trimmed = entries.length > MAX_BINDINGS ? entries.slice(-MAX_BINDINGS) : entries;
    localStorage.setItem(BINDINGS_KEY, JSON.stringify(Object.fromEntries(trimmed)));
    localStorage.removeItem(LEGACY_BINDING_KEY); // a global binding is exactly the bug; do not keep it
  } catch {
    /* private browsing; this session still works, it just will not survive a reload */
  }
}

/** The path of the workflow ComfyUI currently has in front of the user. */
function activePath() {
  try {
    return app.extensionManager?.workflow?.activeWorkflow?.path ?? null;
  } catch {
    return null;
  }
}

function bindingFor(path) {
  return path ? loadBindings()[path] ?? null : null;
}

/** The binding for whatever is on screen right now — the only one a save may target. */
function activeBinding() {
  return bindingFor(activePath());
}

function bindWorkflow(path, binding) {
  if (!path) return;
  const bindings = loadBindings();
  // One workflow per step: re-linking a step to a different file must not leave the old file bound to it.
  for (const [key, existing] of Object.entries(bindings)) {
    if (existing?.stepId === binding.stepId && key !== path) delete bindings[key];
  }
  bindings[path] = binding;
  saveBindings(bindings);
}

function unbindWorkflow(path) {
  if (!path) return;
  const bindings = loadBindings();
  delete bindings[path];
  saveBindings(bindings);
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

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** How many nodes the canvas is actually showing. Reads through the canvas so a subgraph in view counts. */
function canvasNodeCount() {
  try {
    return app.canvas?.graph?._nodes?.length ?? app.graph?._nodes?.length ?? 0;
  } catch {
    // ComfyUI throws "graph accessed before initialization" if we look too early.
    return 0;
  }
}

/**
 * Waits until ComfyUI can actually receive a graph.
 *
 * Extension `setup()` runs *before* the canvas exists, and every route into the graph goes through the
 * canvas store — loading there fails with "getCanvas: canvas is null" while still renaming the tab, which
 * is exactly the "opens the right workflow, shows nothing" symptom. We also wait for ComfyUI's own startup
 * workflow to land, so its restore cannot overwrite ours a moment later.
 */
async function whenCanvasReady(timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const mounted = !!app.canvas?.canvas?.isConnected;
    const restored = app.extensionManager?.workflow?.activeWorkflow !== undefined;
    if (mounted && restored) {
      // One more frame so the store finishes wiring itself to the freshly mounted canvas.
      await sleep(100);
      return true;
    }
    await sleep(100);
  }
  return false;
}

/**
 * Opens a workflow that ComfyUI already has saved, by path.
 *
 * This is what makes the tab a *named* workflow: Ctrl+S saves it in place instead of prompting for a name
 * and a folder, and the file it saves to is the same one the framework reads back.
 *
 * Two traps, both of which produced an empty canvas in practice:
 *
 *   1. A workflow from the directory listing is *metadata only*. `openWorkflow` switches to it happily and
 *      shows you nothing; reading the file is a separate `load()` step.
 *   2. We run from the extension `setup()` hook, which is early enough that the canvas may not be ready to
 *      receive a graph — the open silently does nothing even though the tab gets the right name. So rather
 *      than trusting one attempt, re-assert until the canvas really has the nodes.
 */
async function openSavedWorkflow(path, { attempts = 8, delayMs = 400 } = {}) {
  const store = app.extensionManager?.workflow;
  if (!path || !store?.getWorkflowByPath) return false;

  try {
    // The framework may have only just written the file, so refresh before looking it up.
    await store.syncWorkflows();
    const workflow = store.getWorkflowByPath(path);
    if (!workflow) return false;

    for (let attempt = 0; attempt < attempts; attempt++) {
      if (!workflow.isLoaded) await workflow.load();

      // force, because re-asserting an already-active workflow is otherwise a no-op — which is precisely
      // the state we are trying to recover from. Safe here: this only ever runs on a fresh deep-link load,
      // so there are no unsaved edits to discard.
      await store.openWorkflow(workflow, { force: true });
      await sleep(delayMs);

      if (canvasNodeCount() > 0) return true;
    }

    console.warn(`[WebStudio] ${path} stayed empty after ${attempts} attempts, falling back to the graph`);
    return false;
  } catch (err) {
    console.warn("[WebStudio] could not open the saved workflow, falling back", err);
    return false;
  }
}

async function openStep(request) {
  setBadge("Loading…", "pending");
  const payload = await frameworkFetch(request, `/api/bridge/workflow/${encodeURIComponent(request.stepId)}`);
  const binding = { ...request, label: payload.label || request.label || request.stepId };

  if (payload.comfy_path && (await openSavedWorkflow(payload.comfy_path))) {
    bindWorkflow(payload.comfy_path, binding);
    setBadge(`Linked: ${binding.label}`, "ok");
    return;
  }

  if (payload.workflow && payload.workflow.nodes?.length) {
    await app.loadGraphData(payload.workflow, true, true, null);
  } else if (payload.prompt) {
    // Imported in API format, so there is no LiteGraph document to load. ComfyUI can build one from the
    // prompt itself; pushing it straight back gives the workflow a saved identity, so the next open —
    // and Ctrl+S right now — behave like any other saved workflow.
    await app.loadApiJson(payload.prompt, payload.name || "WebStudio workflow");
    // Passed explicitly: nothing is bound yet, and this is the call that earns the workflow its path.
    const result = await saveBack({ silent: true, binding });
    if (result?.comfy_path && (await openSavedWorkflow(result.comfy_path))) {
      bindWorkflow(result.comfy_path, binding);
      setBadge(`Linked: ${binding.label}`, "ok");
      return;
    }
  } else {
    throw new Error("the framework returned no graph for this workflow");
  }

  // Fell back to a bare graph, so bind whatever identity the tab ended up with.
  bindWorkflow(activePath(), binding);
  setBadge(`Linked: ${binding.label} · save to sync`, "ok");
}

async function saveBack({ silent = false, binding = null } = {}) {
  // Always the workflow on screen, never a remembered one — pushing the active graph into some other
  // step is precisely how edits used to leak between workflows.
  binding = binding || activeBinding();
  if (!binding) {
    if (!silent) alert("This ComfyUI workflow is not linked to a ComfyWebStudio step.");
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

function scheduleAutoSync(delayMs = AUTOSYNC_DEBOUNCE_MS) {
  if (!activeBinding()) return;
  clearTimeout(state.timer);
  state.timer = setTimeout(() => {
    // Re-check on fire: onAfterChange also fires while a workflow is being loaded and when the user
    // switches tabs, and by now the active workflow may not be the one that triggered this.
    if (activeBinding()) void saveBack({ silent: true });
  }, delayMs);
}

/** Edits alone only sync when the user asked for it; an explicit save always does. */
function scheduleEditSync() {
  if (!app.extensionManager?.setting?.get(SETTING_AUTOSYNC)) return;
  scheduleAutoSync();
}

/** URL-decoded ComfyUI userdata path a request targets, or null when it is not a userdata write. */
function userdataTarget(route) {
  const match = /^\/?(?:api\/)?userdata\/([^?]+)/.exec(String(route ?? ""));
  if (!match) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return match[1];
  }
}

/**
 * Syncs whenever ComfyUI itself saves the workflow.
 *
 * Ctrl+S is the save people actually reach for, and it writes ComfyUI's user directory without notifying
 * any extension — so a node added and saved that way stayed invisible to the framework until someone
 * remembered the separate "Save to ComfyWebStudio" command. That is the whole reason a workflow could sit
 * there missing the output node its author had already added.
 *
 * The save request itself is the signal: the workflow store's own events are not public API, whereas this
 * POST is unambiguous and has not changed shape across frontend versions. Our own push to the framework
 * goes out over plain fetch, so nothing here can feed itself.
 */
function syncOnComfySave() {
  const original = api.fetchApi.bind(api);
  api.fetchApi = async function (route, options = {}, ...rest) {
    const response = await original(route, options, ...rest);
    try {
      const method = String(options?.method ?? "GET").toUpperCase();
      const target = userdataTarget(route);
      if (method === "POST" && response?.ok && target?.startsWith("workflows/")) {
        // Deliberately the *active* binding, resolved when the timer fires: ComfyUI has just written the
        // workflow on screen, and a save of anything else is not ours to push.
        scheduleAutoSync(SAVE_SYNC_DELAY_MS);
      }
    } catch (err) {
      console.warn("[WebStudio] could not react to a ComfyUI save", err);
    }
    return response;
  };
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

/**
 * Keeps the badge describing the workflow in front of the user.
 *
 * Since a save targets the *active* workflow, the badge has to say which step that is — otherwise the
 * user switches tabs, sees a stale "Linked: …" and reasonably assumes their edits are going somewhere
 * they are not. Polled rather than subscribed: the workflow store's change events are not public API.
 */
function watchActiveWorkflow(intervalMs = 700) {
  const refresh = () => {
    const path = activePath();
    if (path === state.lastBadgePath) return;
    state.lastBadgePath = path;
    const binding = bindingFor(path);
    if (binding) setBadge(`Linked: ${binding.label || binding.stepId}`, "ok");
    else setBadge("Not linked", "idle");
  };
  refresh();
  setInterval(refresh, intervalMs);
}

// -- extension -----------------------------------------------------------------------------------------

app.registerExtension({
  name: EXTENSION,

  settings: [
    {
      id: SETTING_AUTOSYNC,
      category: ["WebStudio", "Bridge", "Auto-sync"],
      name: "Push graph edits back to ComfyWebStudio as you make them",
      tooltip:
        "Saving the workflow in ComfyUI always syncs a linked workflow. This additionally pushes every "
        + "edit, without waiting for a save.",
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
      label: "Unlink this workflow from ComfyWebStudio",
      function: () => {
        unbindWorkflow(activePath());
        state.lastBadgePath = undefined; // force the watcher to redraw
        setBadge("Not linked", "idle");
      },
    },
  ],

  keybindings: [{ combo: { key: "s", alt: true }, commandId: "WebStudio.SaveToWebStudio" }],

  menuCommands: [{ path: ["Workflow"], commands: ["WebStudio.SaveToWebStudio", "WebStudio.Unlink"] }],

  async setup() {
    const request = readOpenRequest();
    if (request) {
      clearOpenRequest();
      setBadge("Loading…", "pending");
      // Deliberately not awaited: ComfyUI awaits every extension's setup() before it mounts the canvas, so
      // waiting for the canvas *inside* setup() would deadlock against the thing we are waiting for.
      void (async () => {
        try {
          await whenCanvasReady();
          await openStep(request);
        } catch (err) {
          console.error("[WebStudio] could not open the requested step", err);
          setBadge("Open failed", "error");
        } finally {
          // Started after the open so it does not overwrite the badge mid-flight.
          state.lastBadgePath = activePath();
          watchActiveWorkflow();
        }
      })();
    } else {
      void (async () => {
        await whenCanvasReady();
        watchActiveWorkflow();
      })();
    }

    // onAfterChange is a stable LiteGraph hook; chaining rather than replacing keeps any other
    // extension's handler working.
    const graph = app.graph;
    if (graph) {
      const previous = graph.onAfterChange;
      graph.onAfterChange = function (...args) {
        previous?.apply(this, args);
        scheduleEditSync();
      };
    }

    // Independent of the auto-sync setting: saving in ComfyUI is the user saying "this is the version I
    // mean", and a linked workflow that ignores it is exactly how the two copies drift apart.
    syncOnComfySave();
  },
});
