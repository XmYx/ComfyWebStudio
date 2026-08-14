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
| `tests/test_storyboard.py` | writing, describing, characters, stills, frames→shots, reference-input flagging, schema-constrained decoding | a scripted model |
| `scripts/ui_storyboard.py` | the model pickers offer only models that can see, frames render and edits persist, drawing puts the pictures on the frames and varying one leaves the rest alone, a frame becomes a wired shot, and the Flow panel edits, resets and records | app + real ComfyUI |
| `tests/test_pipeline.py` | the prompt renderer — tokens, optional blocks, literal JSON left alone, unknown tokens reported — and schema generation from output fields | nothing |
| `tests/test_pipeline_builtin.py` | the built-in steps reproduce the prompts and schemas they replaced, and overlay resolution: board over app over built-in | nothing |
| `tests/test_pipeline_api.py` | editing a step reaches the model, the transcript records what was sent, the whole flow runs in order, custom fields land | a scripted model |

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

### Bindings and staying in step with ComfyUI
- [ ] Choosing the text-to-image workflow, its prompt, the image-to-video workflow, *its* prompt and the
      starting image one after another keeps all five — none reverts to blank.
- [ ] The **Its prompt** pickers list only text parameters; a seed or a width is not offered.
- [ ] With no motion prompt parameter chosen, the setup panel warns, and **Make the shot** on a frame that
      has a motion prompt is refused rather than building a shot that ignores it.
- [ ] A frame with no motion prompt still builds a shot when no prompt parameter is chosen — there is
      nothing to lose.
- [ ] Change a checkpoint in ComfyUI and press <kbd>Ctrl</kbd>+<kbd>S</kbd> there, then place that workflow
      on a shot canvas: the step runs the *new* checkpoint without anything being re-imported.
- [ ] The same holds for a storyboard: drawing and making shots re-read the bound workflow first.
- [ ] With ComfyUI stopped, placing a step still works and uses the stored copy.
- [ ] A value set by hand on a step survives that re-read; one that merely matched the old default follows
      the new one.
- [ ] A step with no values of its own runs the graph **exactly** as ComfyUI has it — check a workflow with
      subgraph-promoted comboboxes (a model or sampler picker) and confirm the shot uses the chosen entry,
      not the first one in the list.

### The storyboard flow
*Needs a language model. Ollama on `127.0.0.1:11434` is the quick path.*

- [ ] **Flow** lists every step in order, saying which asks a model and which runs a workflow.
- [ ] Opening a step shows its system prompt, its prompt and its output fields — the real ones, not a
      summary.
- [ ] The token palette lists tokens *with their current values*; typing `{frame.nonsense}` is flagged.
- [ ] Editing a system prompt and running the step sends the edited text — check **What was sent**, not
      just that it saved.
- [ ] An edited step is marked *edited*; the others still say they follow the defaults.
- [ ] **Reset to default** restores the built-in wording and clears the mark.
- [ ] Adding an output field with *somewhere of my own* puts the answer on the frame and makes
      `{frame.fields.<name>}` available to later steps.
- [ ] A field name that would not work as a token is refused, with a reason.
- [ ] Pointing an output at something structural (`frame.asset_id`) is refused.
- [ ] A board-scoped step cannot be pointed at a frame field.
- [ ] **Run the flow** runs the steps in order and *waits for the drawing* before the looking step — the
      looking step's transcript entry should say it saw an image.
- [ ] A step that fails stops the flow, and the reason is on both the flow and the transcript entry.
- [ ] A second **Run the flow** on the same board is refused while one is running.
- [ ] **Settings ▸ Storyboard flow** changes the default for boards that have not overridden that step,
      and does *not* touch one that has.
- [ ] Changing a language model on the Settings page does not wipe the flow edits.
- [ ] The transcript survives a reload, and **Clear** empties it.
- [ ] With a language model loaded, the Flow panel offers **Free _n_ GB** with the right figure; pressing it
      empties `nvidia-smi` of that model and the button disappears. With nothing loaded, no button.

### Storyboards

- [ ] **Settings ▸ Language models** with nothing configured offers one-click Ollama, and finds its models.
- [ ] The **Looks at the frames** list contains only models the provider reports as vision-capable — not
      every installed model, and not one merely *named* like a vision model.
- [ ] With no vision model installed, the panel says so and points at the library below.
- [ ] **Pull** on a library entry shows live progress, and on completion the row flips to *installed*, the
      model appears in the pickers, and a vision one appears in the vision picker.
- [ ] A name typed into the free-text field pulls too; a name that does not exist reports Ollama's own error
      rather than hanging.
- [ ] **Write** on a premise returns the asked-for number of frames, each with action, camera, image prompt
      and shot prompt as *prose* — never a nested object, never a field name echoed as its own value.
- [ ] **Find them** names the characters in the premise; attaching one to a frame carries its appearance.
- [ ] Pressing **Find them** again offers nobody already on the board — check the prompt in **What was
      sent** names them as already known.
- [ ] A model listing the same person twice in one answer still produces one character.
- [ ] **Draw them** on a character with an appearance draws their reference and keeps it as an asset by
      itself; the button then reads **Draw another**. Somebody with no appearance written cannot be drawn,
      and the button says why.
- [ ] **Draw all** renders every frame, and each picture appears on its frame as that frame finishes —
      without anything being kept first. A bar above the strip counts the frames off; **Stop** cancels.
- [ ] Each frame shows its own percentage while it is drawing, and a failed frame says why on the card.
- [ ] **Redraw** on one frame runs only that frame; the other frames keep the pictures they had.
- [ ] **↻ Vary** on a frame comes back *different*. With a drawing workflow that exposes no `WS Seed Input`,
      it says so rather than pretending.
- [ ] Dragging an image from **Assets** onto a frame's thumbnail makes it that frame's picture; **Redraw**
      then takes it back to a drawn still. Neither has to be cleared first.
- [ ] **Keep** puts the still in the asset library, and goes quiet once there is nothing new to keep.
- [ ] **Describe** rewrites the prompts from what is *actually* in the generated frame. The motion prompt
      describes motion and is never blank.
- [ ] **Make the shot** creates a shot from the picture the frame is *showing* — vary a frame, then make its
      shot, and it is the variation that is wired in — with the shot prompt on the text input of the chosen
      image-to-video workflow. A still nobody kept is kept on the way through.
- [ ] Choosing reference inputs on a workflow that has none is *flagged*, not dropped: the assignment
      survives, and applies once a workflow that takes them is selected.
- [ ] Pull progress is visible from the Settings page — i.e. the event stream is connected outside a project.

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
- **Small vision models drift.** Answers are constrained to a JSON schema, which fixes the *shape* — the
  right keys, strings where sentences were asked for. It cannot make a 3B model observant. Every described
  field is editable, `describe` can be re-run, and its prompt can be rewritten in the Flow panel.
- **The flow is a sequence, not a graph.** Steps run in order, each either once or once per frame. There is
  no branching and no looping; the one conditional the built-ins need — asking again for a field that came
  back blank — is a property of the step rather than a control structure.
- **A step's order is not validated.** Describing before anything has been drawn is a mistake the app lets
  you make and then reports, in the same spirit as a workflow bound to the wrong parameter.
- [ ] A workflow whose stored graph is missing a required input is converted again the next time it is
      placed or used, without anything being re-imported — check a project made before a converter fix.

- **Re-reading a workflow means re-converting it.** ComfyUI's own `graphToPrompt` runs in the browser, so
  a file read back from its user directory is put through `comfy/graph_convert.py`. That handles subgraphs,
  `control_after_generate` and dynamic combos, but it is a fallback rather than an equal: a conversion that
  loses an input the stored prompt had is **refused**, the stored graph is kept, and the workflow says so.
  Pressing "Save to ComfyWebStudio" in ComfyUI is always the exact path.
- **Syncing from ComfyUI is best effort.** A workflow is re-read before it is placed or used, but an
  unreachable ComfyUI is not a refusal — the stored copy is used and the work proceeds. It also only
  applies to workflows we know a path for: one imported by dropping a `.json` in has no file to re-read.
- **The transcript is capped** at 200 exchanges per board, with each prompt and reply cut at 8 KB. It lives
  in `<project>/stage_runs/` and is excluded from an export unless run history is included.
- **Pulling models is Ollama-only.** An OpenAI-compatible server serves whatever it was started with, so
  the app says that rather than pretending the button will work.
