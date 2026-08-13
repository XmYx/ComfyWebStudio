"""Workflow import, discovery and the hand-off to ComfyUI for editing."""

from __future__ import annotations

import base64
import json
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, UploadFile
from pydantic import BaseModel

from ..comfy.discovery import (
    NodeKindMap,
    bindable_widgets,
    build_raw_param,
    discover,
    find_missing_nodes,
    prompt_hash,
    raw_param_key,
    subgraph_params,
)
from ..comfy.graph_convert import ui_graph_to_prompt
from ..comfy.objectinfo import WidgetSpec
from ..comfy.userdata import (
    ensure_saved_in_comfy,
    is_managed,
    read_from_comfy,
    remove_from_comfy,
)
from ..core.errors import NotFound, ValidationFailed
from ..core.graph import drop_links_for_removed_ports
from ..core.models import ParamSpec, PortSpec, WorkflowRef, utcnow
from .deps import ProjectDep, StateDep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects/{project_id}/workflows", tags=["workflows"])


class ImportWorkflowRequest(BaseModel):
    name: str
    #: Either format is accepted; supplying both is best because no conversion is then needed.
    workflow: dict[str, Any] | None = None
    prompt: dict[str, Any] | None = None
    backend_id: str | None = None


class ExposeParamRequest(BaseModel):
    node_id: str
    input_name: str
    group: str = ""


def _looks_like_api_format(data: dict[str, Any]) -> bool:
    """API prompts are ``{node_id: {class_type, inputs}}``; UI graphs have ``nodes``/``links`` arrays."""
    if "nodes" in data and isinstance(data.get("nodes"), list):
        return False
    return any(isinstance(v, dict) and "class_type" in v for v in data.values())


@dataclass(slots=True)
class WorkflowAnalysis:
    """Everything one pass over a workflow document tells us about it.

    Import, re-sync and the ComfyUI bridge all produce one of these, so the three paths cannot drift on what
    a workflow exposes — a port found on import is found on re-sync too.
    """

    api_prompt: dict[str, Any]
    ui_graph: dict[str, Any]
    ports: list[PortSpec] = field(default_factory=list)
    params: list[ParamSpec] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    missing_nodes: list[str] = field(default_factory=list)
    hash: str = ""


async def analyse_workflow(
    state, ui_graph: dict | None, api_prompt: dict | None, backend_id: str | None
) -> WorkflowAnalysis:
    """Convert the document if we only have the UI format, then find every port and parameter."""
    warnings: list[str] = []
    ui_graph = ui_graph or {}

    if api_prompt is None:
        if not ui_graph:
            raise ValidationFailed("Provide a workflow in either UI or API format.")
        try:
            object_info = await state.backends.object_info(backend_id)
        except Exception as exc:  # noqa: BLE001
            raise ValidationFailed(
                "Converting a UI-format workflow needs a reachable ComfyUI to type its nodes. "
                f"Either start ComfyUI or export the workflow in API format. ({exc})"
            ) from exc

        conversion = await ui_graph_to_prompt(ui_graph, object_info)
        api_prompt = conversion.prompt
        warnings.extend(conversion.warnings)
        if not conversion.reliable:
            warnings.append(
                "This graph was converted without ComfyUI's own converter. Open it in ComfyUI and use "
                "'Save to ComfyWebStudio' to replace it with an exact conversion."
            )

    if not api_prompt:
        raise ValidationFailed("The workflow contains no executable nodes.")

    kind_map = NodeKindMap()
    object_info = None
    try:
        backend = await state.backends.get(backend_id)
        kind_map = NodeKindMap.from_manifest(await backend.manifest())
        object_info = await state.backends.object_info(backend_id)
    except Exception as exc:  # noqa: BLE001 - working offline is legitimate
        logger.debug("Analysing without a backend: %s", exc)
        warnings.append(
            "No ComfyUI was reachable, so this workflow was analysed using built-in node definitions."
        )

    result = discover(api_prompt, kind_map=kind_map)
    warnings.extend(result.warnings)

    missing: list[str] = []
    if object_info is not None:
        missing = await find_missing_nodes(result.node_classes, object_info)

    # Inputs a subgraph promotes are editable too — typed from the node they feed, so a model slot becomes
    # a real dropdown rather than a free-text field.
    promoted = await subgraph_params(ui_graph, api_prompt, object_info) if ui_graph else []
    if promoted:
        warnings.append(
            f"Found {len(promoted)} parameter(s) promoted by this workflow's subgraph(s)."
        )

    if not result.ports:
        warnings.append(
            "No ComfyWebStudio input or output nodes were found. Add WS nodes in ComfyUI to expose "
            "parameters and to let this workflow chain with others."
        )

    return WorkflowAnalysis(
        api_prompt=api_prompt,
        ui_graph=ui_graph,
        ports=result.ports,
        params=[*result.params, *promoted],
        warnings=warnings,
        missing_nodes=missing,
        hash=prompt_hash(api_prompt),
    )


async def _ingest(
    state, project, name: str, ui_graph: dict | None, api_prompt: dict | None, backend_id: str | None
) -> WorkflowRef:
    """Store a workflow in both formats and discover its ports."""
    analysis = await analyse_workflow(state, ui_graph, api_prompt, backend_id)

    workflow = WorkflowRef(
        name=name,
        ports=analysis.ports,
        params=analysis.params,
        hash=analysis.hash,
        missing_nodes=analysis.missing_nodes,
        warnings=analysis.warnings,
        last_synced=utcnow(),
    )

    state.store.write_workflow(project.id, workflow.id, "api", analysis.api_prompt)
    state.store.write_workflow(project.id, workflow.id, "ui", analysis.ui_graph)
    project.workflows[workflow.id] = workflow
    state.store.save(project)
    return workflow


# -- CRUD ----------------------------------------------------------------------------------------------


@router.get("")
def list_workflows(project: ProjectDep) -> list[WorkflowRef]:
    return list(project.workflows.values())


@router.post("", status_code=201)
async def import_workflow(
    state: StateDep, project: ProjectDep, body: ImportWorkflowRequest
) -> WorkflowRef:
    return await _ingest(state, project, body.name, body.workflow, body.prompt, body.backend_id)


@router.post("/upload", status_code=201)
async def upload_workflow(
    state: StateDep, project: ProjectDep, file: UploadFile, backend_id: str | None = None
) -> WorkflowRef:
    """Import a ``.json`` dropped onto the UI, in either format."""
    try:
        data = json.loads((await file.read()).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationFailed(f"{file.filename} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationFailed("A workflow must be a JSON object.")

    name = (file.filename or "Workflow").rsplit("/", 1)[-1].removesuffix(".json")
    if _looks_like_api_format(data):
        return await _ingest(state, project, name, None, data, backend_id)
    return await _ingest(state, project, name, data, None, backend_id)


@router.get("/{workflow_id}")
def get_workflow(project: ProjectDep, workflow_id: str) -> WorkflowRef:
    workflow = project.workflow(workflow_id)
    if workflow is None:
        raise NotFound(f"No workflow {workflow_id!r}")
    return workflow


@router.get("/{workflow_id}/graph")
def get_graph(state: StateDep, project: ProjectDep, workflow_id: str, fmt: str = "ui") -> dict:
    return state.store.read_workflow(project.id, workflow_id, fmt)


@router.delete("/{workflow_id}", status_code=204)
async def delete_workflow(state: StateDep, project: ProjectDep, workflow_id: str) -> None:
    workflow = project.workflows.get(workflow_id)
    if workflow is None:
        raise NotFound(f"No workflow {workflow_id!r}")

    in_use = [
        step.name
        for shot in project.shots
        for step in shot.steps
        if step.workflow_id == workflow_id
    ]
    if in_use:
        raise ValidationFailed(
            "This workflow is still used by: " + ", ".join(in_use[:5]) + ". Remove those steps first."
        )

    # Only remove ComfyUI's copy when we put it there. A workflow imported from ComfyUI keeps its
    # original path, and that file belongs to the user.
    if is_managed(workflow.comfy_userdata_path):
        try:
            await remove_from_comfy(await state.backends.get(), workflow)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not tidy up ComfyUI's copy: %s", exc)

    project.workflows.pop(workflow_id)
    state.store.delete_workflow_files(project.id, workflow_id)
    state.store.save(project)


def _stored_graph(state, project_id: str, workflow_id: str, fmt: str) -> dict:
    """The stored graph in one format, or ``{}`` when that format was never written."""
    try:
        return state.store.read_workflow(project_id, workflow_id, fmt)
    except NotFound:
        return {}


async def sync_workflow(state, project, workflow, *, backend_id: str | None = None) -> bool:
    """Bring our copy of a workflow up to date with ComfyUI's, if it has moved on. True when it had.

    Called before a workflow is *used* — placed on a canvas, or built into a shot — because ComfyUI's own
    <kbd>Ctrl</kbd>+<kbd>S</kbd> writes its user directory and tells nobody. Without this, a step placed
    today runs the graph as it was on the day it was imported: the checkpoint you switched last week is
    still the old one, and nothing anywhere says so.

    **Best effort, always.** ComfyUI being unreachable is not a reason to refuse to place a step, so every
    failure here is a debug line and a ``False``. The stored copy is what gets used, exactly as before.

    The comparison is the point of the fast path: reading one file and finding it unchanged costs a round
    trip, while re-analysing a graph costs an ``/object_info`` fetch and a full conversion. Placing ten
    steps should not pay the second cost ten times.
    """
    if not workflow.comfy_userdata_path:
        return False

    try:
        backend = await state.backends.get(backend_id)
        fresh = await read_from_comfy(backend, workflow)
    except Exception as exc:  # noqa: BLE001 - a stored copy is still perfectly usable
        logger.debug("Could not re-read %r from ComfyUI: %s", workflow.name, exc)
        return False

    if fresh is None or fresh == _stored_graph(state, project.id, workflow.id, "ui"):
        return False

    logger.info("%r has changed in ComfyUI; re-reading it before use", workflow.name)
    try:
        analysis = await analyse_workflow(state, fresh, None, backend_id)
    except Exception as exc:  # noqa: BLE001 - mid-edit graphs are common and not our problem
        logger.debug("ComfyUI's copy of %r could not be converted: %s", workflow.name, exc)
        return False

    _apply_analysis(state, project, workflow, analysis)
    state.store.save(project)
    return True


def _apply_analysis(state, project, workflow, analysis, *, warning: str | None = None) -> set[str]:
    """Put a fresh reading of a workflow onto it, and let its steps follow what moved.

    Returns the port keys that went away. Does not save — the caller decides when, because it usually has
    more to do first.
    """
    previous_defaults = {p.key: p.default for p in workflow.params}
    # Bindings whose node survived: one pointing at a deleted node would silently write nothing.
    raw_params = [
        p for p in workflow.params if p.source == "raw_widget" and p.node_id in analysis.api_prompt
    ]

    previous_ports = {p.key for p in workflow.ports}
    workflow.ports = analysis.ports
    workflow.params = [*analysis.params, *raw_params]
    workflow.hash = analysis.hash
    workflow.warnings = ([warning] if warning else []) + analysis.warnings
    workflow.missing_nodes = analysis.missing_nodes
    workflow.last_synced = utcnow()

    state.store.write_workflow(project.id, workflow.id, "api", analysis.api_prompt)
    state.store.write_workflow(project.id, workflow.id, "ui", analysis.ui_graph)

    # A step whose override merely copied the old default follows the new one; one that differs was a
    # decision and stays. Without this a value changed in ComfyUI updates the workflow and changes nothing
    # anybody can see — the same trap the bridge already guards against.
    from .bridge import _adopt_new_defaults  # here, because bridge imports analyse_workflow from us

    _adopt_new_defaults(project, workflow, previous_defaults)

    removed = previous_ports - {p.key for p in workflow.ports}
    drop_links_for_removed_ports(project, workflow.id, removed)
    return removed


@router.post("/{workflow_id}/rediscover")
async def rediscover(
    state: StateDep,
    project: ProjectDep,
    workflow_id: str,
    backend_id: str | None = None,
    pull: bool = True,
) -> WorkflowRef:
    """Re-read the workflow from ComfyUI and re-scan it for ports and parameters.

    Pulling matters. ComfyUI's own <kbd>Ctrl</kbd>+<kbd>S</kbd> writes its user directory and tells nobody:
    only the bridge extension's explicit "Save to ComfyWebStudio" pushes a graph back here. Re-scanning just
    the stored copy therefore reports the workflow as it was at import time — which is exactly how an output
    node added afterwards stays invisible while ``last_synced`` claims everything is current.

    ``pull=false`` re-parses the stored copy only, for the rare case where ComfyUI's file has been changed in
    a way the user does not want yet.

    Existing raw-widget bindings are preserved: they were an explicit user choice, and a re-scan must not
    quietly throw them away.
    """
    workflow = project.workflow(workflow_id)
    if workflow is None:
        raise NotFound(f"No workflow {workflow_id!r}")

    stored_ui = _stored_graph(state, project.id, workflow_id, "ui")
    stored_api = _stored_graph(state, project.id, workflow_id, "api")

    ui_graph: dict = stored_ui
    # Reusing the stored prompt keeps ComfyUI's own graphToPrompt() output whenever nothing has changed;
    # only a genuinely newer document is worth putting through our fallback converter.
    api_prompt: dict | None = stored_api or None
    pull_warning: str | None = None

    if pull and workflow.comfy_userdata_path:
        try:
            backend = await state.backends.get(backend_id)
            fresh = await read_from_comfy(backend, workflow)
        except Exception as exc:  # noqa: BLE001 - a stored copy is still perfectly usable
            logger.debug("Could not re-read %r from ComfyUI: %s", workflow.name, exc)
            fresh = None
            pull_warning = (
                f"ComfyUI's copy at {workflow.comfy_userdata_path} could not be read, so this is a re-scan "
                "of the stored graph. Edits made in ComfyUI since the last sync are not included."
            )
        if fresh is not None and fresh != stored_ui:
            logger.info("Re-syncing %r from ComfyUI's copy at %s", workflow.name, workflow.comfy_userdata_path)
            ui_graph, api_prompt = fresh, None

    if not ui_graph and api_prompt is None:
        raise NotFound(f"{workflow.name!r} has no stored graph at all; re-import it.")

    try:
        analysis = await analyse_workflow(state, ui_graph, api_prompt, backend_id)
    except ValidationFailed:
        # ComfyUI's copy is there but unusable — mid-edit, or full of nodes this instance cannot type.
        # Falling back to what we already hold beats refusing to sync at all.
        if ui_graph is stored_ui:
            raise
        logger.warning("ComfyUI's copy of %r could not be converted; keeping the stored graph", workflow.name)
        pull_warning = (
            f"ComfyUI's copy at {workflow.comfy_userdata_path} could not be converted into a runnable "
            "prompt, so the previously stored graph was kept. Open it in ComfyUI and use "
            "'Save to ComfyWebStudio' to sync it exactly."
        )
        analysis = await analyse_workflow(state, stored_ui, stored_api or None, backend_id)

    removed = _apply_analysis(state, project, workflow, analysis, warning=pull_warning)
    broken = drop_links_for_removed_ports(project, workflow_id, removed)
    state.store.save(project)

    state.events.emit(
        "workflow.synced",
        project_id=project.id,
        data={
            "workflow_id": workflow_id,
            "name": workflow.name,
            "ports": len(workflow.ports),
            "params": len(workflow.params),
            "removed_ports": sorted(removed),
            "broken_links": broken,
        },
    )
    return workflow


# -- raw widget binding --------------------------------------------------------------------------------


@router.get("/{workflow_id}/bindable")
async def list_bindable(
    state: StateDep, project: ProjectDep, workflow_id: str, backend_id: str | None = None
) -> list[dict]:
    """Widgets in this graph that could be exposed as editable parameters."""
    api_prompt = state.store.read_workflow(project.id, workflow_id, "api")
    object_info = await state.backends.object_info(backend_id)
    candidates = await bindable_widgets(api_prompt, object_info)

    workflow = project.workflow(workflow_id)
    exposed = {p.key for p in (workflow.params if workflow else [])}
    for candidate in candidates:
        candidate["exposed"] = candidate["key"] in exposed
    return candidates


@router.post("/{workflow_id}/expose", status_code=201)
async def expose_param(
    state: StateDep,
    project: ProjectDep,
    workflow_id: str,
    body: ExposeParamRequest,
    backend_id: str | None = None,
) -> WorkflowRef:
    """Expose one widget of a stock node as an editable parameter."""
    workflow = project.workflow(workflow_id)
    if workflow is None:
        raise NotFound(f"No workflow {workflow_id!r}")

    api_prompt = state.store.read_workflow(project.id, workflow_id, "api")
    node = api_prompt.get(body.node_id)
    if not isinstance(node, dict):
        raise NotFound(f"No node {body.node_id!r} in this workflow")

    key = raw_param_key(body.node_id, body.input_name)
    if workflow.param(key) is not None:
        raise ValidationFailed("That parameter is already exposed.")

    object_info = await state.backends.object_info(backend_id)
    class_type = str(node.get("class_type"))
    widget: WidgetSpec | None = next(
        (w for w in await object_info.widgets(class_type) if w.name == body.input_name), None
    )
    if widget is None:
        raise ValidationFailed(
            f"{class_type} has no editable widget called {body.input_name!r}."
        )

    current = (node.get("inputs") or {}).get(body.input_name)
    if isinstance(current, list):
        raise ValidationFailed(
            f"{body.input_name!r} is driven by a link inside the workflow, so it cannot be edited here."
        )

    workflow.params.append(
        build_raw_param(
            body.node_id,
            class_type,
            widget,
            current_value=current,
            title=(node.get("_meta") or {}).get("title"),
            group=body.group,
            order=len(workflow.params),
        )
    )
    state.store.save(project)
    return workflow


@router.delete("/{workflow_id}/expose/{param_key:path}", status_code=204)
def unexpose_param(state: StateDep, project: ProjectDep, workflow_id: str, param_key: str) -> None:
    workflow = project.workflow(workflow_id)
    if workflow is None:
        raise NotFound(f"No workflow {workflow_id!r}")

    param = workflow.param(param_key)
    if param is None:
        raise NotFound(f"No parameter {param_key!r}")
    if param.source != "raw_widget":
        raise ValidationFailed(
            "This parameter comes from a WebStudio input node. Remove the node in ComfyUI instead."
        )

    workflow.params = [p for p in workflow.params if p.key != param_key]
    state.store.save(project)


# -- open in ComfyUI ------------------------------------------------------------------------------------


@router.post("/{workflow_id}/open-in-comfy")
async def open_in_comfy(
    state: StateDep, project: ProjectDep, workflow_id: str, step_id: str | None = None,
    backend_id: str | None = None,
) -> dict:
    """Build the URL that opens this workflow in ComfyUI, linked back to us.

    The workflow is first saved into ComfyUI's own user directory, so the tab that opens is a real named
    workflow rather than an unsaved document — Ctrl+S there saves in place instead of asking the user to
    invent a name. The token scopes the write-back to this one workflow, so a stale tab cannot overwrite
    something else later.
    """
    workflow = project.workflow(workflow_id)
    if workflow is None:
        raise NotFound(f"No workflow {workflow_id!r}")

    backend = await state.backends.get(backend_id)

    comfy_path = None
    try:
        graph = state.store.read_workflow(project.id, workflow_id, "ui")
    except NotFound:
        graph = {}
    if graph:
        comfy_path = await ensure_saved_in_comfy(backend, project, workflow, graph)
        if comfy_path:
            state.store.save(project)

    token = secrets.token_urlsafe(24)
    state.bridge_tokens[token] = {"project_id": project.id, "workflow_id": workflow_id}

    payload = {
        "backend": f"http://{state.settings.host}:{state.settings.port}",
        "stepId": workflow_id,
        "token": token,
        "label": f"{project.name} · {workflow.name}",
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")

    pack = await backend.http.webstudio_ping()
    return {
        "url": f"{backend.config.base_url}/?ws_open={encoded}",
        "token": token,
        "comfy_path": comfy_path,
        "node_pack_installed": pack is not None,
        "hint": None
        if pack
        else "The comfyui-webstudio node pack is not installed on this ComfyUI, so the workflow will not "
        "open automatically and edits will not sync back. Install it from the Settings page.",
    }
