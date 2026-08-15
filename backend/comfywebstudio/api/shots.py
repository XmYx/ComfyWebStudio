"""Shots, steps and links — the structure the user edits on the shot canvas."""

from __future__ import annotations

import dataclasses
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from ..core.errors import NotFound, ValidationFailed
from ..core.graph import validate_new_link, validate_placed
from ..core.models import (
    Link,
    PortKind,
    Shot,
    Size,
    Step,
    ValueNode,
    ValueNodeKind,
    Vec2,
)
from ..core.template_capture import templates_for
from .deps import ProjectDep, ShotDep, StateDep, find_step
from .workflows import sync_workflow

router = APIRouter(prefix="/api/projects/{project_id}", tags=["shots"])


class CreateShotRequest(BaseModel):
    name: str = "Shot"
    notes: str = ""


class UpdateShotRequest(BaseModel):
    name: str | None = None
    notes: str | None = None
    color: str | None = None


class CreateStepRequest(BaseModel):
    workflow_id: str
    name: str | None = None
    ui_pos: Vec2 | None = None
    ui_size: Size | None = None
    # Supplied when duplicating or pasting, so the whole operation is one save and therefore one
    # undo step — a user pressing Ctrl+Z after a paste expects the paste to disappear, not half of it.
    param_overrides: dict | None = None
    exposed_params: list[str] | None = None
    seed_mode: str | None = None


class UpdateStepRequest(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    param_overrides: dict | None = None
    #: Replaces the list wholesale — the UI knows the full set of pinned parameters.
    exposed_params: list[str] | None = None
    seed_mode: str | None = None
    backend_id: str | None = None
    notes: str | None = None
    ui_pos: Vec2 | None = None
    ui_size: Size | None = None


class CreateValueNodeRequest(BaseModel):
    kind: ValueNodeKind = "string"
    name: str | None = None
    value: Any = None
    asset_id: str | None = None
    #: For a ``shot`` node: which shot's output to take, and which of its ports.
    source_shot_id: str | None = None
    source_port: str | None = None
    media_kind: PortKind | None = None
    ui_pos: Vec2 | None = None
    ui_size: Size | None = None


class UpdateValueNodeRequest(BaseModel):
    name: str | None = None
    value: Any = None
    asset_id: str | None = None
    source_shot_id: str | None = None
    source_port: str | None = None
    media_kind: PortKind | None = None
    ui_pos: Vec2 | None = None
    ui_size: Size | None = None
    #: ``value`` is legitimately null (an empty text node), so a PATCH has to say when it means it.
    clear_value: bool = False
    clear_asset: bool = False


class CreateLinkRequest(BaseModel):
    from_step: str
    from_port: str
    to_step: str
    to_port: str


# -- shots ---------------------------------------------------------------------------------------------


@router.get("/shots")
def list_shots(project: ProjectDep) -> list[Shot]:
    return project.shots


@router.post("/shots", status_code=201)
def create_shot(state: StateDep, project: ProjectDep, body: CreateShotRequest) -> Shot:
    shot = Shot(name=body.name, notes=body.notes)
    project.shots.append(shot)
    state.store.save(project)
    state.events.emit("project.changed", project_id=project.id, data={"action": "shot_created"})
    return shot


@router.get("/shots/{shot_id}")
def get_shot(shot: ShotDep) -> Shot:
    return shot


@router.patch("/shots/{shot_id}")
def update_shot(state: StateDep, project: ProjectDep, shot: ShotDep, body: UpdateShotRequest) -> Shot:
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(shot, field, value)
    state.store.save(project)
    return shot


@router.delete("/shots/{shot_id}", status_code=204)
def delete_shot(state: StateDep, project: ProjectDep, shot_id: str) -> None:
    before = len(project.shots)
    project.shots = [s for s in project.shots if s.id != shot_id]
    if len(project.shots) == before:
        raise NotFound(f"No shot {shot_id!r}")

    # Clips pointing at a deleted shot would render nothing; drop them rather than leave dead references.
    for track in project.timeline.tracks:
        track.clips = [c for c in track.clips if c.source.shot_id != shot_id]

    # And the storyboard frame that made it, for the same reason: left pointing at a shot that is gone, it
    # refuses to build another one and reports itself as finished.
    for board in project.storyboards:
        if board.forget_shot(shot_id):
            board.touch()

    state.store.save(project)
    state.events.emit("project.changed", project_id=project.id, data={"action": "shot_deleted"})


@router.get("/shots/{shot_id}/validate")
def validate(state: StateDep, project: ProjectDep, shot: ShotDep) -> dict:
    report, _flat = validate_placed(project, shot, state.templates)
    return {
        "ok": report.ok,
        "order": report.order,
        "issues": [dataclasses.asdict(i) for i in report.issues],
    }


@router.post("/shots/{shot_id}/duplicate", status_code=201)
def duplicate_shot(state: StateDep, project: ProjectDep, shot: ShotDep) -> Shot:
    """Copy a shot, re-issuing every step and link id so the two are fully independent."""
    from ..core.ids import new_id

    copy = shot.model_copy(deep=True)
    copy.id = new_id("shot")
    copy.name = f"{shot.name} (copy)"

    remap = {step.id: new_id("step") for step in copy.steps}
    remap.update({node.id: new_id("val") for node in copy.nodes})
    remap.update({instance.id: new_id("inst") for instance in copy.instances})
    for step in copy.steps:
        step.id = remap[step.id]
    for node in copy.nodes:
        node.id = remap[node.id]
    for instance in copy.instances:
        instance.id = remap[instance.id]
    for link in copy.links:
        link.id = new_id("link")
        link.from_step = remap.get(link.from_step, link.from_step)
        link.to_step = remap.get(link.to_step, link.to_step)

    project.shots.append(copy)
    state.store.save(project)
    return copy


# -- steps ---------------------------------------------------------------------------------------------


@router.post("/shots/{shot_id}/steps", status_code=201)
async def create_step(
    state: StateDep, project: ProjectDep, shot: ShotDep, body: CreateStepRequest
) -> Step:
    workflow = project.workflow(body.workflow_id)
    if workflow is None:
        raise NotFound(f"No workflow {body.workflow_id!r} in this project")

    # Placing a workflow is the moment its defaults start mattering, so this is the moment to check they
    # are still the ones ComfyUI has. Saving there tells nobody, and a step placed against a stale copy
    # runs last month's checkpoint without anything saying so. Best effort: unreachable is not a refusal.
    await sync_workflow(state, project, workflow)

    step = Step(
        name=body.name or workflow.name,
        workflow_id=workflow.id,
        ui_pos=body.ui_pos or Vec2(x=40 + 260 * len(shot.steps), y=80),
        ui_size=body.ui_size or Size(),
        param_overrides=dict(body.param_overrides or {}),
        exposed_params=list(body.exposed_params or []),
        seed_mode=body.seed_mode,  # type: ignore[arg-type]
    )
    shot.steps.append(step)
    state.store.save(project)
    state.events.emit("project.changed", project_id=project.id, data={"action": "step_created"})
    return step


@router.patch("/steps/{step_id}")
def update_step(
    state: StateDep, project: ProjectDep, step_id: str, body: UpdateStepRequest
) -> Step:
    _, step = find_step(project, step_id)

    # Merge parameter overrides rather than replacing: the UI sends only what changed.
    if body.param_overrides is not None:
        step.param_overrides.update(body.param_overrides)

    # Assign the validated models themselves, not their dumped dicts, so the step keeps its types.
    for field in (
        "name", "enabled", "exposed_params", "seed_mode", "backend_id", "notes", "ui_pos", "ui_size",
    ):
        value = getattr(body, field)
        if value is not None:
            setattr(step, field, value)

    state.store.save(project)
    return step


@router.put("/steps/{step_id}/params")
def replace_step_params(
    state: StateDep, project: ProjectDep, step_id: str, overrides: dict
) -> Step:
    """Replace the whole override map — used by 'reset to workflow defaults'."""
    _, step = find_step(project, step_id)
    step.param_overrides = dict(overrides)
    state.store.save(project)
    return step


class ApplyParamsRequest(BaseModel):
    #: Parameter keys to push out. Empty means every parameter this step's workflow exposes.
    keys: list[str] = []


class ApplyParamsResult(BaseModel):
    """What ``apply to all shots`` actually did, per parameter, so the UI can say more than 'done'."""

    #: Step ids that were changed.
    steps: list[str] = []
    #: ``key -> the value that was written``.
    values: dict[str, Any] = {}
    #: Steps that carry this parameter but were left alone, and why.
    skipped: list[str] = []


@router.post("/steps/{step_id}/params/apply-to-all")
def apply_params_to_all(
    state: StateDep, project: ProjectDep, step_id: str, body: ApplyParamsRequest
) -> ApplyParamsResult:
    """Give every other step running this workflow the same value for these parameters.

    The reach is *the same workflow*, not the same-looking widget: a parameter key only means anything
    against the workflow that declared it, and `steps` in one graph is not `steps` in another. A storyboard
    turns one workflow into twenty shots, which is exactly when re-typing a checkpoint twenty times stops
    being reasonable.

    What is sent is the parameter's **effective** value — the override if the step has one, the workflow's
    own default otherwise — so applying an untouched parameter spreads what this step actually runs with
    rather than nothing at all.

    A target whose input is fed by a link is skipped and named. Its value arrives from upstream at run
    time, so writing an override there would change the inspector and change nothing about the run.
    """
    _, source = find_step(project, step_id)
    workflow = project.workflow(source.workflow_id)
    if workflow is None:
        raise NotFound(f"No workflow {source.workflow_id!r} in this project")

    specs = {param.key: param for param in workflow.params}
    keys = [key for key in (body.keys or list(specs)) if key in specs]
    if not keys:
        raise ValidationFailed("None of those parameters belong to this step's workflow.")

    values = {
        key: source.param_overrides[key] if key in source.param_overrides else specs[key].default
        for key in keys
    }

    changed: list[str] = []
    skipped: list[str] = []
    for shot in project.shots:
        # A template editing session is a template wearing a shot's clothes; quietly rewriting one from a
        # shot's inspector would edit every shot that ever placed that template.
        if shot.template_edit_id:
            continue
        for step in shot.steps:
            if step.id == source.id or step.workflow_id != source.workflow_id:
                continue
            fed = {link.to_port for link in shot.links if link.to_step == step.id}
            wanted = {key: value for key, value in values.items() if key not in fed}
            blocked = sorted(set(values) & fed)
            if blocked:
                skipped.append(f"{shot.name} · {step.name}: {', '.join(blocked)} comes from a link")
            # Compared against the target's *effective* value, not its override map: a step that already
            # runs this value needs no override, and one whose override merely restates the workflow
            # default should not collect a second one saying the same thing.
            if not wanted or all(
                (step.param_overrides[key] if key in step.param_overrides else specs[key].default) == value
                for key, value in wanted.items()
            ):
                continue
            step.param_overrides.update(wanted)
            changed.append(step.id)

    if changed:
        state.store.save(project)
        state.events.emit("project.changed", project_id=project.id, data={"action": "params_applied"})

    return ApplyParamsResult(steps=changed, values=values, skipped=skipped)


@router.delete("/steps/{step_id}", status_code=204)
def delete_step(state: StateDep, project: ProjectDep, step_id: str) -> None:
    shot, step = find_step(project, step_id)
    shot.steps = [s for s in shot.steps if s.id != step.id]
    shot.links = [
        link for link in shot.links if step.id not in (link.from_step, link.to_step)
    ]
    for track in project.timeline.tracks:
        track.clips = [c for c in track.clips if c.source.step_id != step.id]

    state.store.save(project)
    state.events.emit("project.changed", project_id=project.id, data={"action": "step_deleted"})


# -- value nodes ---------------------------------------------------------------------------------------

#: What a freshly placed node holds, so it is immediately usable rather than empty.
_VALUE_NODE_DEFAULTS: dict[str, Any] = {
    "string": "", "int": 0, "float": 0.0, "boolean": False, "media": None, "shot": None,
}


def _find_node(project, node_id: str) -> tuple[Shot, ValueNode]:
    for shot in project.shots:
        node = shot.node(node_id)
        if node is not None:
            return shot, node
    raise NotFound(f"No value node {node_id!r} in this project")


def _sync_media_kind(project, node: ValueNode) -> None:
    """Keep an assigned asset's kind on the node, so its port colour and type match its content."""
    asset = project.assets.get(node.asset_id or "")
    if asset is not None:
        node.media_kind = asset.kind


@router.post("/shots/{shot_id}/nodes", status_code=201)
def create_value_node(
    state: StateDep, project: ProjectDep, shot: ShotDep, body: CreateValueNodeRequest
) -> ValueNode:
    """Place a value node on the shot canvas."""
    if body.asset_id and body.asset_id not in project.assets:
        raise NotFound(f"No asset {body.asset_id!r} in this project")

    node = ValueNode(
        kind=body.kind,
        name=body.name or "",
        value=body.value if body.value is not None else _VALUE_NODE_DEFAULTS.get(body.kind),
        asset_id=body.asset_id,
        source_shot_id=body.source_shot_id,
        source_port=body.source_port,
        # Stacked below the steps rather than beside them: value nodes feed inputs, so the left-hand
        # column is where the user will look for them.
        ui_pos=body.ui_pos or Vec2(x=-220.0, y=80.0 + 120.0 * len(shot.nodes)),
        ui_size=body.ui_size or Size(),
    )
    if body.media_kind is not None:
        node.media_kind = body.media_kind
    _sync_media_kind(project, node)

    shot.nodes.append(node)
    state.store.save(project)
    state.events.emit("project.changed", project_id=project.id, data={"action": "node_created"})
    return node


@router.patch("/nodes/{node_id}")
def update_value_node(
    state: StateDep, project: ProjectDep, node_id: str, body: UpdateValueNodeRequest
) -> ValueNode:
    _, node = _find_node(project, node_id)

    if body.asset_id is not None:
        if body.asset_id not in project.assets:
            raise NotFound(f"No asset {body.asset_id!r} in this project")
        node.asset_id = body.asset_id
    elif body.clear_asset:
        node.asset_id = None

    if body.value is not None:
        node.value = body.value
    elif body.clear_value:
        node.value = None

    for field in ("name", "source_shot_id", "source_port", "media_kind", "ui_pos", "ui_size"):
        value = getattr(body, field)
        if value is not None:
            setattr(node, field, value)

    if body.asset_id is not None:
        _sync_media_kind(project, node)

    state.store.save(project)
    return node


@router.delete("/nodes/{node_id}", status_code=204)
def delete_value_node(state: StateDep, project: ProjectDep, node_id: str) -> None:
    shot, node = _find_node(project, node_id)
    shot.nodes = [n for n in shot.nodes if n.id != node.id]
    shot.links = [link for link in shot.links if link.from_step != node.id]
    state.store.save(project)
    state.events.emit("project.changed", project_id=project.id, data={"action": "node_deleted"})


# -- links ---------------------------------------------------------------------------------------------


@router.post("/shots/{shot_id}/links", status_code=201)
def create_link(
    state: StateDep, project: ProjectDep, shot: ShotDep, body: CreateLinkRequest
) -> Link:
    """Create a link, refusing it up front if it would be invalid.

    Validating here rather than at run time means the canvas can reject the connection as the user drags
    it, with a reason.
    """
    link = Link(**body.model_dump())
    # Templates are only loaded when the shot actually places one — the common link touches neither end.
    templates = templates_for(project, shot, state.templates) if shot.instances else None
    validate_new_link(project, shot, link, templates)
    shot.links.append(link)
    state.store.save(project)
    return link


@router.delete("/shots/{shot_id}/links/{link_id}", status_code=204)
def delete_link(state: StateDep, project: ProjectDep, shot: ShotDep, link_id: str) -> None:
    before = len(shot.links)
    shot.links = [link for link in shot.links if link.id != link_id]
    if len(shot.links) == before:
        raise NotFound(f"No link {link_id!r}")
    state.store.save(project)
