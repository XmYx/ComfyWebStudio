"""Endpoints the ComfyUI bridge extension talks to.

These are called from *inside the ComfyUI page*, so they are cross-origin and authenticated with the
one-shot token minted by ``POST /workflows/{id}/open-in-comfy``. A token is bound to a single workflow: a
tab left open from an earlier session cannot be used to overwrite a different one.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Header
from pydantic import BaseModel

from ..comfy.discovery import NodeKindMap, discover, find_missing_nodes, prompt_hash, subgraph_params
from ..core.errors import NotFound, StudioError
from ..core.models import utcnow
from .deps import StateDep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/bridge", tags=["bridge"])


class Unauthorized(StudioError):
    status_code = 401
    code = "unauthorized"


class SaveWorkflowRequest(BaseModel):
    step_id: str
    #: The LiteGraph document — what reopens in ComfyUI.
    workflow: dict[str, Any]
    #: The API-format prompt, produced by ComfyUI's own graphToPrompt(). This is why we never have to
    #: reimplement the conversion for the normal path.
    prompt: dict[str, Any]


def _authorize(state, token: str | None, workflow_id: str) -> dict[str, str]:
    binding = state.bridge_tokens.get(token or "")
    if binding is None:
        raise Unauthorized(
            "This ComfyUI tab is no longer linked to ComfyWebStudio. Use 'Open in ComfyUI' again."
        )
    if binding["workflow_id"] != workflow_id:
        raise Unauthorized("This token is for a different workflow.")
    return binding


@router.get("/workflow/{workflow_id}")
def fetch_workflow(
    state: StateDep,
    workflow_id: str,
    x_webstudio_token: str | None = Header(default=None),
) -> dict:
    """Hand the bridge extension something it can load.

    A workflow imported in API format has no LiteGraph document, but ComfyUI's frontend can build one from
    an API prompt itself (``app.loadApiJson``). So we send whichever we have and let the extension pick —
    the user then saves it back and we get a proper editable graph from then on.
    """
    binding = _authorize(state, x_webstudio_token, workflow_id)
    project = state.store.load(binding["project_id"])
    workflow = project.workflow(workflow_id)
    if workflow is None:
        raise NotFound(f"No workflow {workflow_id!r}")

    graph: dict[str, Any] | None = None
    try:
        candidate = state.store.read_workflow(project.id, workflow_id, "ui")
        if candidate and candidate.get("nodes"):
            graph = candidate
    except NotFound:
        graph = None

    prompt: dict[str, Any] | None = None
    if graph is None:
        try:
            prompt = state.store.read_workflow(project.id, workflow_id, "api")
        except NotFound:
            prompt = None

    if graph is None and not prompt:
        raise NotFound(f"{workflow.name!r} has no stored graph at all; re-import it.")

    return {
        "workflow": graph,
        "prompt": prompt,
        "has_ui_graph": graph is not None,
        "label": f"{project.name} · {workflow.name}",
        "name": workflow.name,
    }


@router.post("/workflow")
async def save_workflow(
    state: StateDep,
    body: SaveWorkflowRequest,
    x_webstudio_token: str | None = Header(default=None),
) -> dict:
    """Accept an edited workflow back from ComfyUI and re-discover its ports.

    New ports appear immediately in the framework; links whose ports were removed are dropped, with the
    affected steps reported so the UI can tell the user what changed.
    """
    binding = _authorize(state, x_webstudio_token, body.step_id)
    project = state.store.load(binding["project_id"])
    workflow = project.workflow(body.step_id)
    if workflow is None:
        raise NotFound(f"No workflow {body.step_id!r}")

    if not body.prompt:
        raise StudioError("ComfyUI sent an empty prompt; nothing was saved.")

    kind_map = NodeKindMap()
    try:
        backend = await state.backends.get()
        kind_map = NodeKindMap.from_manifest(await backend.manifest())
    except Exception:  # noqa: BLE001
        pass

    result = discover(body.prompt, kind_map=kind_map)
    raw_params = [p for p in workflow.params if p.source == "raw_widget" and p.node_id in body.prompt]

    object_info = None
    try:
        object_info = await state.backends.object_info()
    except Exception:  # noqa: BLE001
        pass

    # ComfyUI flattens subgraphs itself before handing us the prompt, but only the UI document knows which
    # inputs were promoted — so the parameter map is read from there either way.
    promoted = await subgraph_params(body.workflow or {}, body.prompt, object_info)

    previous_ports = {p.key for p in workflow.ports}
    workflow.ports = result.ports
    workflow.params = [*result.params, *promoted, *raw_params]
    workflow.hash = prompt_hash(body.prompt)
    workflow.warnings = result.warnings
    workflow.last_synced = utcnow()

    try:
        if object_info is not None:
            workflow.missing_nodes = await find_missing_nodes(result.node_classes, object_info)
    except Exception:  # noqa: BLE001
        pass

    state.store.write_workflow(project.id, workflow.id, "api", body.prompt)
    state.store.write_workflow(project.id, workflow.id, "ui", body.workflow)

    removed = previous_ports - {p.key for p in workflow.ports}
    broken = _drop_dangling_links(project, workflow.id, removed)
    state.store.save(project)

    state.events.emit(
        "workflow.synced",
        project_id=project.id,
        data={
            "workflow_id": workflow.id,
            "name": workflow.name,
            "ports": len(workflow.ports),
            "params": len(workflow.params),
            "removed_ports": sorted(removed),
            "broken_links": broken,
        },
    )
    logger.info(
        "Workflow %r synced from ComfyUI: %d ports, %d params", workflow.name,
        len(workflow.ports), len(workflow.params),
    )

    return {
        "ok": True,
        "workflow_id": workflow.id,
        "port_count": len(workflow.ports),
        "param_count": len(workflow.params),
        "removed_ports": sorted(removed),
        "broken_links": broken,
    }


def _drop_dangling_links(project, workflow_id: str, removed_ports: set[str]) -> list[dict]:
    """Remove links that referenced ports the user deleted in ComfyUI."""
    if not removed_ports:
        return []

    affected: list[dict] = []
    step_ids = {
        step.id for shot in project.shots for step in shot.steps if step.workflow_id == workflow_id
    }
    for shot in project.shots:
        keep = []
        for link in shot.links:
            broken = (link.from_step in step_ids and link.from_port in removed_ports) or (
                link.to_step in step_ids and link.to_port in removed_ports
            )
            if broken:
                affected.append(
                    {"shot_id": shot.id, "link_id": link.id, "port": link.from_port or link.to_port}
                )
            else:
                keep.append(link)
        shot.links = keep
    return affected
