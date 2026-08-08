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

```bash
make setup          # backend venv + frontend deps
make link-nodepack  # symlink comfy_nodes into your ComfyUI custom_nodes
make dev            # backend on :8500, frontend dev server on :5173
```

See `docs/` for architecture notes and the manual verification checklist.
