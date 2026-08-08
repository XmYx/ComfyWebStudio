# Verification

## Automated

```bash
make test          # backend pytest + node pack tests
make lint          # ruff
cd frontend && npm run typecheck && npx vitest run
```

| Suite | What it covers | Needs |
|---|---|---|
| `tests/test_graph.py`, `test_store.py` | graph validation, persistence, export/import | nothing |
| `tests/test_discovery.py`, `test_graph_convert.py` | port discovery, injection, UI→API conversion | nothing |
| `tests/test_execution.py` | chaining, caching, failures, cancellation, timeouts, remote transfer | fake ComfyUI |
| `tests/test_api.py` | the whole API surface, bridge, timeline, render | fake ComfyUI |
| `tests/nodepack/` | node registration, tensor round-trips, path safety | ComfyUI's own venv |
| `frontend/src/lib/*.test.ts` | kind compatibility, formatting | nothing |
| `scripts/ui_smoke.py` | the browser: canvas edges, previews, timeline, settings | a running app |

The fake ComfyUI (`tests/fixtures/fake_comfy.py`) replays ComfyUI 0.24.1's real event ordering, including
the detail the runner depends on: `execution_success` arrives *before* history is written, and the
`executing: node=null` sentinel arrives after.

## Live end-to-end

```bash
# 1. Start ComfyUI with the node pack linked
make link-nodepack
cd /home/magix/ai/ComfyUI && ./comfyenv/bin/python main.py --port 8188

# 2. Start ComfyWebStudio
make backend

# 3. Drive it
.venv/bin/python scripts/ui_smoke.py
```

`scripts/ui_smoke.py` needs a project that already has a run; create one through the UI first, or use the
API walkthrough below.

## Manual checklist

### Node pack
- [ ] ComfyUI's log shows `WebStudio node pack <version> (protocol 1) loaded`.
- [ ] `curl localhost:8188/api/webstudio/ping` returns `{"ok": true, ...}`.
- [ ] The node search menu lists `WS Image Input`, `WS Image Output`, etc. under **WebStudio**.
- [ ] Queueing a workflow with a `WSImageOutput` writes to
      `ComfyUI/output/webstudio/manual/<timestamp>/<port>/` and previews inline in ComfyUI.

### Chaining
- [ ] Import two workflows, add both as steps, connect an image output to an image input.
- [ ] Connecting mismatched kinds is refused **while dragging**, with a reason.
- [ ] Run the shot; both steps go green and the second step's preview reflects the first step's output.
- [ ] Run again — both steps report **cached** and ComfyUI is not called.
- [ ] Change a parameter; that step and everything downstream re-execute.
- [ ] Break a step (point it at a missing model); it goes red with the failing node named, and its
      dependents are marked **skipped**, not failed.

### Editing in ComfyUI
- [ ] "Open in ComfyUI" opens a new tab with the graph loaded and a green **WebStudio · Linked** badge.
- [ ] Add a `WS Text Input` named `negative`, save (or press the badge). A toast reports the new port.
- [ ] The new parameter appears in the inspector without reloading ComfyWebStudio.
- [ ] Delete a port that a link uses, save: the toast says the link was disconnected, and the canvas
      reflects it.
- [ ] With the pack removed from ComfyUI, "Open in ComfyUI" warns that syncing back will not work.

### Timeline and render
- [ ] "Build from shots" lays each shot's final output end to end with thumbnails.
- [ ] Dragging a clip moves it; dragging its right edge trims it; both persist after a reload.
- [ ] **Still** renders a PNG of the frame at the playhead.
- [ ] **Render** produces a playable file in the Renders panel.
- [ ] A clip whose step has not run yet is flagged, and the render warns rather than failing silently.

### Projects
- [ ] Export downloads a `.cwsproj`; importing it creates a *second* project with previews intact.
- [ ] Deleting a workflow still used by a step is refused, naming the steps.

### Settings
- [ ] **Test** reports ComfyUI version, GPU and node-pack status.
- [ ] For a local backend without the pack, **Install** creates the symlink and says a restart is needed.
- [ ] Pointing a local backend at a directory without `main.py` is refused.

## Known limitations

- **Subgraphs** cannot be converted by `graph_convert.py`. Import such a workflow through
  "Open in ComfyUI" → "Save to ComfyWebStudio" so ComfyUI performs the conversion. The UI warns when this
  applies.
- **Remote backends** need the node pack installed there to chain anything other than images —
  `POST /upload/image` only accepts images.
- ComfyUI must be **restarted** after installing the node pack; packs are imported once at startup.
