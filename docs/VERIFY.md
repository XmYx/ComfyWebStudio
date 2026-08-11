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
| `frontend/src/store/dockTree.test.ts` | the layout tree: splitting, tabbing, collapsing, sizing, hiding | nothing |
| `tests/test_menu_features.py` | version log, diffing, element restore, plugins, node size | fake ComfyUI |
| `scripts/ui_smoke.py` | the browser: canvas edges, previews, timeline, settings | a running app |
| `scripts/ui_menus.py` | the menus: shortcuts, undo/redo, panel toggles, dialogs | a running app |
| `scripts/ui_features.py` | node resize, context menus, history panel and element restore | a running app |
| `scripts/ui_dismiss.py` | menus close on a click elsewhere, including surfaces that swallow mousedown | a running app |
| `tests/test_shot_sources.py` | a shot placed in a shot: live structure, instanced values, cycles refused | fake ComfyUI |
| `scripts/ui_dock.py` | panels dock beside/above/below each other, splitters resize, rim docking | a running app |
| `scripts/ui_nested_shots.py` | dragging a shot into a shot places it as one contained node | a running app |
| `tests/test_audio_mix.py` | mixing: gain, equal-power pan, mute, solo, and waveform peaks | nothing |
| `tests/test_ripple.py` | cutting a span out: clips inside, straddling, and after it | nothing |
| `tests/test_linked_clips.py` | tied picture and sound: moving, trimming, deleting, tying, untying | nothing |
| `frontend/src/features/timeline/snapping.test.ts` | what snaps, how far it reaches, and both clip edges | nothing |
| `scripts/ui_comfy_panel.py` | ComfyUI embeds in a panel, maximises without reloading, workflows open into it | app + real ComfyUI |
| `scripts/ui_timeline.py` | the timeline workspace docks, shots drop onto it, audio draws and plays | a running app |
| `scripts/ui_open_in_comfy.py` | opening a workflow lands on a *named, saved* ComfyUI workflow | app + real ComfyUI |
| `scripts/ui_bridge_binding.py` | each ComfyUI workflow syncs back only to its own step | app + real ComfyUI |

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

Each browser script creates its own scratch project (running it against ComfyUI where needed) and deletes it
afterwards, so they are repeatable and never touch your real work.

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
- [ ] "Open in ComfyUI" shows the **ComfyUI** panel with the graph loaded and a green **WebStudio · Linked**
      badge; shift-clicking the button still opens a real browser tab instead.
- [ ] The panel's ⤢ fills the workspace and ⤡ gives the layout back, *without* reloading ComfyUI — an
      unsaved graph in the frame survives both.
- [ ] The ComfyUI tab shows the workflow's **name**, not "Unsaved Workflow", and <kbd>Ctrl</kbd>+<kbd>S</kbd>
      there saves in place without asking for a name.
- [ ] A workflow imported with **From ComfyUI** reopens *its original file* — check the tab name matches and
      that saving updates that file rather than creating a copy.
- [ ] A workflow imported in API format gets saved under `ComfyWebStudio/<project>/` on first open, and
      appears in ComfyUI's own workflow list from then on.
- [ ] Add a `WS Text Input` named `negative`, save (or press the badge). A toast reports the new port.
- [ ] The new parameter appears in the inspector without reloading ComfyWebStudio.
- [ ] Delete a port that a link uses, save: the toast says the link was disconnected, and the canvas
      reflects it.
- [ ] With the pack removed from ComfyUI, "Open in ComfyUI" warns that syncing back will not work.

### Timeline and render
- [ ] The timeline opens as its own workspace — sources left, monitor over the tracks, clip settings right
      — and its panels dock, split, float and maximise independently of the shot editor's.
- [ ] Dragging a shot from the bin onto a lane places it *where it was dropped*; its audio comes with it on
      an audio track, lined up with the picture.
- [ ] An audio clip is drawn as its waveform, and trimming it redraws rather than squashing the shape.
- [ ] An audio track header has solo, mute, level and pan; soloing one silences the others and the muted
      ones say *why* they are silent.
- [ ] Pressing play makes sound that follows the playhead, and panning a track is audible while cutting —
      not only in the finished render.
- [ ] Placing a video that carries sound puts its audio on an audio track too, tied to the picture;
      moving or trimming either moves both, and **Untie** sets them free.
- [ ] A shot that has not run can still be placed; it shows as waiting and fills in once it does.
- [ ] A clip arrives the length of its media, and **Fit to source** re-times one that has drifted.
- [ ] Dragging a clip snaps its edges to neighbours and the playhead, with a guide; <kbd>Alt</kbd>
      overrides it and **⇥ Snap** turns it off.
- [ ] Dragging across empty space selects a span; <kbd>Delete</kbd> removes it and closes the gap on every
      track, keeping picture and sound in step.
- [ ] "Build from shots" lays each shot's final output end to end with thumbnails.
- [ ] Dragging a clip moves it; dragging its right edge trims it; both persist after a reload.
- [ ] **Still** renders a PNG of the frame at the playhead.
- [ ] **Render** produces a playable file in the Renders panel.
- [ ] A clip whose step has not run yet is flagged, and the render warns rather than failing silently.

### Projects
- [ ] Export downloads a `.cwsproj`; importing it creates a *second* project with previews intact.
- [ ] Deleting a workflow still used by a step is refused, naming the steps.

### Menus
- [ ] Every menu opens, and hovering **File → Import** expands the submenu.
- [ ] **Edit → Paste** is greyed out until something has been copied.
- [ ] Select a step, <kbd>Ctrl</kbd>+<kbd>C</kbd> then <kbd>Ctrl</kbd>+<kbd>V</kbd>: a copy appears with
      the same parameters and no links.
- [ ] <kbd>Ctrl</kbd>+<kbd>Z</kbd> removes the whole paste in **one** press; <kbd>Ctrl</kbd>+
      <kbd>Shift</kbd>+<kbd>Z</kbd> brings it back.
- [ ] <kbd>Ctrl</kbd>+<kbd>1</kbd> / <kbd>Ctrl</kbd>+<kbd>2</kbd> collapse the side panels and the canvas
      takes the space; the choice survives a reload.
- [ ] <kbd>Ctrl</kbd>+<kbd>/</kbd> opens the shortcut list; **Help → About** shows the connected backends.

### Plugins
- [ ] **File → Export as Plugin…** with a workflow and a shot ticked downloads a `.cwsplugin`.
- [ ] **Plugins → Load Plugin…** installs it; loading it a second time is refused as already installed.
- [ ] **Apply to project** on a fresh project recreates the workflows *and* the shot, with its links
      wired between the new steps and its parameter overrides intact.
- [ ] Applying twice does not produce two workflows with the same name.
- [ ] Uninstalling a plugin leaves projects that used it untouched.

### Node resize
- [ ] Selecting a step shows resize handles at its corners and edges.
- [ ] Dragging a corner resizes it; the size persists and survives a reload.
- [ ] A resized node's preview grows with it; its ports scroll rather than overflowing.
- [ ] **Reset size** in the node's right-click menu returns it to sizing by content.

### Right-click menus
- [ ] Right-clicking a **step** offers run, copy/cut/duplicate, enable/disable, rename, reset size, open in
      ComfyUI and delete.
- [ ] Right-clicking **empty canvas** offers Add step (listing the project's workflows), paste, select all,
      fit and arrange.
- [ ] Right-clicking a **link** offers Disconnect.
- [ ] Right-clicking a **shot**, **workflow**, **clip**, **track** or **project card** each offers actions
      relevant to that thing.
- [ ] A menu opened near the bottom or right edge flips to stay fully on screen.
- [ ] Escape, a left-click elsewhere, or a second right-click closes it — including a click on the
      canvas background, another node, or a timeline clip, all of which stop mousedown propagating.
- [ ] Right-clicking somewhere else while a menu is open shows the *new* menu, not nothing.

### Subgraphs
- [ ] Import a workflow containing a subgraph (`From ComfyUI` → pick one). It reports how many nodes it
      expanded into and how many parameters it found.
- [ ] Its promoted inputs appear in the inspector grouped under the subgraph's name, with sliders for
      bounded numbers and real dropdowns for model pickers.
- [ ] Changing a promoted value that drives more than one input (typically `width`) updates every one of
      them — check the submitted graph, or that the render actually changes size.
- [ ] A promoted input the parent workflow wired something into does *not* appear as a parameter.
- [ ] Nested subgraphs work: ids read `outer:inner:node`.

### Versioning
- [ ] Every edit appears in **Edit → History…** described in plain language.
- [ ] Moving or resizing a node is hidden until **Include moves and resizes** is ticked.
- [ ] Expanding a version lists its individual changes.
- [ ] **Restore** on a version rolls the project back, and <kbd>Ctrl</kbd>+<kbd>Z</kbd> undoes the rollback.
- [ ] A step's **History** tab lists only that step's changes; **Restore** there reverts only that step and
      leaves the rest of the project untouched.
- [ ] **Save a version…** names a checkpoint; it stays visible under **Named versions only**.
- [ ] Right-clicking a shot offers its own version history, including edits to the steps inside it.
- [ ] History survives restarting the server.

### Settings
- [ ] **Test** reports ComfyUI version, GPU and node-pack status.
- [ ] For a local backend without the pack, **Install** creates the symlink and says a restart is needed.
- [ ] Pointing a local backend at a directory without `main.py` is refused.

## Known limitations

- **Subgraphs** are flattened and their promoted inputs become parameters. What is *not* exposed is any
  inner widget the subgraph's author chose not to promote — open it in ComfyUI and promote it there, or use
  "Expose a node parameter" on the flattened node.
- **Remote backends** need the node pack installed there to chain anything other than images —
  `POST /upload/image` only accepts images.
- ComfyUI must be **restarted** after installing the node pack; packs are imported once at startup.
- **Version history is bounded** to 500 versions per project; the oldest unnamed ones are pruned, and named
  checkpoints are always kept. It lives in `<project>/history/` and is excluded from exports unless you ask
  for it. Run results are never affected by a restore — they live in `runs/` and are append-only.
- **Plugins contain content, not code.** A plugin can carry workflows and shot templates; it deliberately
  cannot execute anything, so installing one someone sent you is safe.
