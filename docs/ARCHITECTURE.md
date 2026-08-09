# Architecture

## The problem this solves

ComfyUI runs one graph at a time, and the values flowing through that graph — `IMAGE`, `MASK`, `AUDIO`,
`LATENT` — are in-memory tensors that are discarded the moment a prompt finishes. Nothing outside that
prompt can ever see them.

That single fact drives most of the design. To chain workflow A into workflow B, A's outputs have to be
**persisted somewhere deterministic** and **reported back in a machine-readable form**. Neither is something
ComfyUI does on its own, so both are provided by a companion node pack.

## Layout

| Path | Role |
|---|---|
| `comfy_nodes/` | ComfyUI custom node pack — typed input/output nodes, `/webstudio/*` routes, browser bridge |
| `backend/comfywebstudio/` | FastAPI service — orchestration, persistence, media, render |
| `frontend/` | Vite + React + TypeScript UI |
| `tests/` | pytest suite, including a fake ComfyUI server |
| `scripts/ui_smoke.py` | Playwright smoke test against a running instance |

## The node pack contract

**Output nodes** (`WSImageOutput`, `WSVideoOutput`, `WSAudioOutput`, `WSLatentOutput`, `WSMaskOutput`,
`WSTextOutput`, `WSNumberOutput`, `WSFileOutput`) write to
`output/webstudio/<run_id>/<step_id>/<port_name>/` and return:

```python
{"ui": {
    "images": [...],                        # ComfyUI's own preview keys still work
    "webstudio": [{"protocol": 1, "port_name": ..., "kind": ..., "run_key": ...,
                   "files": [{"filename", "subfolder", "type"}], "meta": {...}}],
}}
```

The framework reads that `webstudio` key out of `GET /history/{prompt_id}`.

**Input nodes** (`WSImageInput`, `WSStringInput`, …) declare a named port and read a `source` (media) or
`value` (scalar) widget that the framework fills in before submitting.

`run_key` is written into every output node's widget by the framework rather than passed as a hidden input.
That is deliberate: the submitted graph fully describes where its own artifacts will land, and because the
widget changes per run, ComfyUI re-executes the output node while leaving everything upstream cached.

Kinds live in a registry (`comfy_nodes/ws_nodes/kinds/`). Adding a data type means adding one module.

## Backend

```
comfy/       HTTP + WS clients, backend adapters, discovery, injection, graph conversion
core/        domain model, project store, migrations, graph validation
execution/   step runner, orchestrator, cache, event bus
media/       content-addressed store, transfer, probing, thumbnails
render/      compositor and PyAV encoder
api/         one router module per surface
```

### Executing a step

1. Resolve parameters and upstream artifacts; hash them into a cache key.
2. On a hit, return the previous result without touching ComfyUI.
3. Stage linked inputs so this backend can read them.
4. Inject values and `run_key` into a **copy** of the workflow's API prompt.
5. **Subscribe to the websocket, then POST** — ComfyUI can start executing before the HTTP response
   returns, and an event emitted before the subscription exists is lost.
6. Wait for `{"type":"executing","data":{"node":null}}`. This is the correct completion signal:
   `execution_success` fires at `execution.py:805`, *before* history is written at `main.py:332`.
7. Read `/history`, ingest artifacts, persist the `StepRun`.

### Local vs remote

The only real difference is how a file produced by one step reaches the next:

- **Local** (shared filesystem): our input nodes accept absolute paths, so chaining is zero-copy.
- **Remote**: images go through `POST /upload/image`; latents, audio and video need our pack's
  `POST /webstudio/ingest`, because ComfyUI's own upload endpoint only accepts images.

`ComfyBackend.stage()` is the entire difference. Everything above it is identical.

### Caching

A step is skipped when `hash(workflow graph, resolved params, upstream artifact SHAs, output port names)`
matches a previous successful run whose files are still on disk. Upstream artifacts are keyed by *content*,
so regenerating a byte-identical image still counts as a hit. A randomised seed disables caching for that
step, because the result is different by definition.

### Persistence

Files, not a database — which is what makes export a zip of a directory and import an unzip:

```
<projects_dir>/<slug>_<id>/
  project.json
  workflows/<id>.ui.json      what opens in ComfyUI
  workflows/<id>.api.json     what executes
  runs/<run_id>.json
  assets/<kind>/<ab>/<sha>.<ext>
  thumbs/<sha>.webp
  renders/
```

Writes are atomic (temp file + `os.replace`). Artifacts are hardlinked into the project when the filesystem
allows, so a project owns its media at essentially no disk cost.

## Editing in ComfyUI, and syncing back

ComfyUI's frontend has **no `?workflow=` deep-link parameter** (verified against
`comfyui-frontend-package` 1.44.19), and there is **no server-side UI→API graph converter** — ComfyUI does
that in the browser. Both gaps are filled by `comfy_nodes/web/js/webstudio.js`:

- **Open**: the framework first writes the workflow into ComfyUI's own user directory (`comfy/userdata.py`),
  then mints a scoped token and opens `?ws_open=<base64>`. The extension looks the file up via
  `extensionManager.workflow.getWorkflowByPath()` and opens *that*, so the tab is a real named workflow —
  Ctrl+S saves in place instead of prompting for a name, and it saves to the same file we read back.
  Workflows imported from ComfyUI keep their original path; everything else lives under
  `workflows/ComfyWebStudio/<project>/`. Loading a bare graph remains the fallback when the write fails.
- **Save back**: the extension calls `app.graphToPrompt()` and POSTs **both** formats to
  `/api/bridge/workflow`. Because ComfyUI's own converter produced the API prompt, the conversion is exact.

`comfy/graph_convert.py` is the fallback for a ComfyUI without our pack. It handles widget ordering,
`control_after_generate`, links, reroutes, bypass, mute and subgraphs.

## Subgraphs

A ComfyUI subgraph is a reusable group stored once in `definitions.subgraphs` and instantiated by nodes
whose `type` is its UUID. `comfy/subgraphs.py` does two things with them.

**Flattening.** Instances expand recursively into plain nodes, with links resolved across every boundary —
downwards into an instance's output, upwards out of a promoted input. Node ids follow ComfyUI's own
execution-id convention, `<instance>:<inner>`, nesting as `98:50:22`. Matching it is what lets a workflow
converted here and the same workflow flattened by ComfyUI address their nodes identically, so the parameter
map is valid either way.

**Promotion mapping.** A subgraph's `inputs` are the knobs whoever built it chose to expose, so they become
editable parameters (`source: "subgraph"`), grouped under the subgraph's name and typed from the node they
feed — a `COMBO` slot becomes a real dropdown with the live option list rather than a text box.

One promoted input often drives *several* inner inputs: `width` typically sets both the latent size and the
scheduler. `ParamSpec.targets` therefore holds a list, and injection writes the value to all of them. A
promoted slot the parent has wired something into is a connection, not a knob, and is skipped.

## Events

One websocket per configured ComfyUI, normalised into our own event bus, fanned out to the browser over a
single `/api/events` socket. The frontend never talks to ComfyUI directly — and could not, since ComfyUI's
default origin-only middleware (`server.py:147-185`) 403s cross-origin browser requests.

## Rendering

`render/compositor.py` resolves each clip to media and composites one frame at a time; `render/encoder.py`
encodes with **PyAV**, which bundles its own FFmpeg — no system `ffmpeg` needed. A clip references
`(shot, step, port)` rather than a file, so re-running a shot updates the cut without touching the edit.

## The menu layer

`frontend/src/features/menu/commands.ts` holds one list of commands, each declaring its label, optional
shortcut, when it is `enabled`, and (for toggles) whether it is `checked`. The menu bar, the keyboard
handler and any future command palette all render from that list, so a menu item and its shortcut cannot
drift apart.

Right-click menus (`components/ContextMenu.tsx`) read the same registry, mixing registry commands with
actions specific to whatever was clicked. That is why Copy behaves identically whether it came from the Edit
menu, Ctrl+C, or right-clicking a node.

**Plugins** (`core/plugins.py`) package workflows and shot templates into a `.cwsplugin` zip. Applying one
copies its content into a project with fresh ids, so the plugin stays a template. Plugins are content only,
never executable code — otherwise "load a plugin someone sent you" would be a dangerous action.

## Versioning

Every call to `ProjectStore.save` appends a **version**: a gzipped, content-addressed snapshot plus a
description of what changed. Snapshotting there rather than per-endpoint means no mutation can be missed.

    <project>/history/
        log.jsonl                 one version per line
        snapshots/<sha>.json.gz   deduplicated by content

`core/diffing.py` turns each pair of snapshots into readable changes — "Set steps on Sampler to 30",
"Connected Generate.image → Upscale.image" — each tagged with a **scope** and a **target id**. Those two
fields are what make the whole feature work:

* **Global history** is the log.
* **Element history** is the log filtered by `target_id`, which is what the step inspector's History tab and
  the shot's own history show.
* **Undo/redo** is a pointer walking the log, so it now survives a restart.

Restoring comes in two forms. A full restore rewrites the project; **element restore** merges just one shot,
step, track or workflow out of an old snapshot into the current project, leaving everything else alone —
"put this step's parameters back to how they were" without reverting anyone else's work. Restoring is itself
a recorded edit, so it can be undone.

Layout-only changes (moves, resizes) are recorded but flagged, and hidden from the history list by default
so real edits are not buried under drag noise. Named versions are user checkpoints and are never pruned.

Operations that ought to be one undo step are made one *save*: creating a step accepts its name, size and
parameters, so pasting is a single request and a single Ctrl+Z.

## Extension points

| To add | Edit |
|---|---|
| A data type | `comfy_nodes/ws_nodes/kinds/`, `plugins/kinds/`, and the frontend's preview/param registries |
| A ComfyUI transport | subclass `ComfyBackend`, call `register_backend` |
| An API surface | a module in `api/`, listed in `api/__init__.py` |
| A preview or widget | one entry in the frontend's `RENDERERS` / `WIDGETS` maps |
| A menu item or shortcut | one entry in `COMMANDS`, referenced from `MENUS` or any context menu |
| A tracked change | one branch in `core/diffing.py` |
| A parameter source | emit `ParamSpec`s from a new function; the form renders them unchanged |
