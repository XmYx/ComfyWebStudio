"""Browsing the workflows already saved inside ComfyUI.

ComfyUI stores its workflows under ``user/<user>/workflows`` and exposes them over ``/userdata``, which
works identically for a local install and a cloud instance. Listing them here means the user picks from
what they already have rather than hunting for a JSON file to upload.

Those files are UI-format LiteGraph documents, so importing one goes through the same conversion as any
other UI-format import — with the same honest caveat about subgraphs.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from ..core.errors import NotFound, ValidationFailed
from ..core.models import WorkflowRef
from .deps import ProjectDep, StateDep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/comfy", tags=["comfy"])


class ImportFromComfyRequest(BaseModel):
    #: Path relative to ComfyUI's workflows directory, e.g. ``"LTX2_TXT2IMG.json"``.
    path: str
    name: str | None = None
    backend_id: str | None = None


@router.get("/workflows")
async def list_comfy_workflows(state: StateDep, backend_id: str | None = None) -> dict[str, Any]:
    """Workflows saved in the ComfyUI instance, newest first.

    Never raises on an unreachable backend: the picker should say "ComfyUI is not reachable" rather than
    blow up the panel it lives in.
    """
    try:
        backend = await state.backends.get(backend_id)
    except Exception as exc:  # noqa: BLE001
        return {"reachable": False, "error": str(exc), "workflows": []}

    try:
        entries = await backend.http.list_workflows()
    except Exception as exc:  # noqa: BLE001
        return {"reachable": False, "error": str(exc), "workflows": []}

    workflows = [
        {
            "path": entry.get("path"),
            "name": str(entry.get("path", "")).rsplit("/", 1)[-1].removesuffix(".json"),
            "size": entry.get("size"),
            # ComfyUI reports milliseconds here (app/user_manager.py:26-32).
            "modified": entry.get("modified"),
        }
        for entry in entries
        if str(entry.get("path", "")).endswith(".json")
        # `.index.json` holds the frontend's bookmarks, not a workflow.
        and not str(entry.get("path", "")).startswith(".")
    ]
    workflows.sort(key=lambda w: w.get("modified") or 0, reverse=True)

    return {
        "reachable": True,
        "error": None,
        "backend": backend.config.name,
        "workflows": workflows,
    }


@router.post("/projects/{project_id}/import", status_code=201)
async def import_from_comfy(
    state: StateDep, project: ProjectDep, body: ImportFromComfyRequest
) -> WorkflowRef:
    """Copy one of ComfyUI's saved workflows into this project."""
    from .workflows import _ingest

    backend = await state.backends.get(body.backend_id)

    try:
        raw = await backend.http.read_userdata(f"workflows/{body.path}")
    except Exception as exc:  # noqa: BLE001
        raise NotFound(f"ComfyUI has no workflow at {body.path!r}: {exc}") from exc

    try:
        graph = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationFailed(f"{body.path} is not valid JSON: {exc}") from exc
    if not isinstance(graph, dict) or not graph.get("nodes"):
        raise ValidationFailed(f"{body.path} does not look like a ComfyUI workflow.")

    name = body.name or body.path.rsplit("/", 1)[-1].removesuffix(".json")
    workflow = await _ingest(state, project, name, graph, None, body.backend_id)

    # Remember where it came from so a later sync can find the same file again.
    workflow.comfy_userdata_path = f"workflows/{body.path}"
    state.store.save(project)
    logger.info("Imported %r from ComfyUI userdata", body.path)
    return workflow
