"""Shots, steps and links — the structure the user edits on the shot canvas."""

from __future__ import annotations

import dataclasses

from fastapi import APIRouter
from pydantic import BaseModel

from ..core.errors import NotFound
from ..core.graph import validate_new_link, validate_shot
from ..core.models import Link, Shot, Step, Vec2
from .deps import ProjectDep, ShotDep, StateDep, find_step

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


class UpdateStepRequest(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    param_overrides: dict | None = None
    seed_mode: str | None = None
    backend_id: str | None = None
    notes: str | None = None
    ui_pos: Vec2 | None = None


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

    state.store.save(project)
    state.events.emit("project.changed", project_id=project.id, data={"action": "shot_deleted"})


@router.get("/shots/{shot_id}/validate")
def validate(project: ProjectDep, shot: ShotDep) -> dict:
    report = validate_shot(project, shot)
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
    for step in copy.steps:
        step.id = remap[step.id]
    for link in copy.links:
        link.id = new_id("link")
        link.from_step = remap.get(link.from_step, link.from_step)
        link.to_step = remap.get(link.to_step, link.to_step)

    project.shots.append(copy)
    state.store.save(project)
    return copy


# -- steps ---------------------------------------------------------------------------------------------


@router.post("/shots/{shot_id}/steps", status_code=201)
def create_step(
    state: StateDep, project: ProjectDep, shot: ShotDep, body: CreateStepRequest
) -> Step:
    workflow = project.workflow(body.workflow_id)
    if workflow is None:
        raise NotFound(f"No workflow {body.workflow_id!r} in this project")

    step = Step(
        name=body.name or workflow.name,
        workflow_id=workflow.id,
        ui_pos=body.ui_pos or Vec2(x=40 + 260 * len(shot.steps), y=80),
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
    updates = body.model_dump(exclude_none=True)

    # Merge parameter overrides rather than replacing: the UI sends only what changed.
    overrides = updates.pop("param_overrides", None)
    if overrides is not None:
        step.param_overrides.update(overrides)
    for field, value in updates.items():
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
    validate_new_link(project, shot, link)
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
