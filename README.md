# ComfyWebStudio

Shot-based orchestration for ComfyUI. Build a **shot** out of several ComfyUI **workflows**, chain each
workflow's outputs into the next one's inputs, run steps individually or as a chain, preview every result,
and cut the finished shots together on a timeline.

ComfyUI runs one graph at a time and its IMAGE / AUDIO / LATENT values are in-memory tensors that vanish when
execution ends. ComfyWebStudio ships a companion **custom node pack** whose output nodes persist those tensors
to disk deterministically and report structured metadata back — which is what makes cross-workflow chaining
possible.

## Layout

| Path | What it is |
|---|---|
| `backend/comfywebstudio/` | FastAPI service — orchestration, persistence, media, render |
| `comfy_nodes/` | The ComfyUI custom node pack (`comfyui-webstudio`) |
| `frontend/` | Vite + React + TypeScript UI |
| `tests/` | pytest suite, including a fake ComfyUI server fixture |

## Requirements

- Python 3.12+ (developed on 3.14)
- Node 22+ (for the frontend only)
- A ComfyUI instance — local (fast path: shared filesystem) or remote over its HTTP/WS API

No system `ffmpeg` is required; rendering uses PyAV, which bundles its own FFmpeg libraries.

## Quick start

**macOS / Linux**

```bash
./start.sh
```

**Windows**

```
start.bat
```

The launcher installs anything missing on first run, builds the interface, starts the server on
<http://127.0.0.1:8500> and opens a browser. Add `--dev` for hot reload, `--port 9000` for a different
port, or `--setup` to install dependencies and stop.

Then, in **Settings → ComfyUI backends**, point it at your ComfyUI and press **Install** to add the node
pack (ComfyUI needs a restart afterwards).

<details>
<summary>Using make instead</summary>

```bash
make setup          # backend venv + frontend deps
make link-nodepack  # symlink comfy_nodes into your ComfyUI custom_nodes
make dev            # backend on :8500, frontend dev server on :5173
make test           # every test suite
```
</details>

## Using it

| Menu | What it covers |
|---|---|
| **File** | New / open / save / duplicate a project, import workflows from ComfyUI or a file, import media, import and export projects, export a plugin |
| **Edit** | Undo and redo, cut / copy / paste steps and clips, duplicate, delete, select all, preferences |
| **Window** | Show or hide the side panels, zoom and fit the graph, jump between Shots, Timeline and Settings |
| **Plugins** | Install, enable, apply and export reusable workflow + shot packs |
| **Help** | Keyboard shortcuts, documentation, open ComfyUI, about |

Press <kbd>Ctrl</kbd>+<kbd>/</kbd> for the full shortcut list.

See `docs/ARCHITECTURE.md` for how it works and `docs/VERIFY.md` for the verification checklist.
