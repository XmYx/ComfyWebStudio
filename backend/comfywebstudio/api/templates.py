"""The shot template library, and the instances placed from it.

Two groups of endpoints. The library ones are project-independent — a template belongs to the studio, not
to whichever project happened to produce it. The instance ones live under a project, because a placed
instance does.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from ..core.errors import NotFound, ValidationFailed
from ..core.models import Shot, Size, TemplateInstance, Vec2
from ..core.template_capture import capture_shot, place_instance, sync_instance
from ..core.template_store import summarize
from ..core.templates import ShotTemplate, TemplateSummary
from .deps import ProjectDep, ShotDep, StateDep

logger = logging.getLogger(__name__)

library = APIRouter(prefix="/api/templates", tags=["templates"])
router = APIRouter(prefix="/api/projects/{project_id}", tags=["templates"])


class SaveTemplateRequest(BaseModel):
    name: str | None = None
    description: str = ""
    #: Supplied to overwrite an existing template in place, keeping placed instances pointed at it.
    template_id: str | None = None


class UpdateTemplateRequest(BaseModel):
    name: str | None = None
    description: str | None = None


class PromotionRequest(BaseModel):
    """Rename or hide one of a template's promoted ports or controls."""

    label: str | None = None
    shown: bool | None = None


class PlaceInstanceRequest(BaseModel):
    template_id: str
    name: str | None = None
    ui_pos: Vec2 | None = None


class UpdateInstanceRequest(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    #: Merged, like a step's — the UI sends only what changed.
    param_overrides: dict[str, Any] | None = None
    ui_pos: Vec2 | None = None
    ui_size: Size | None = None


# -- the library ---------------------------------------------------------------------------------------


@library.get("")
def list_templates(state: StateDep) -> list[TemplateSummary]:
    return state.templates.list()


@library.get("/{template_id}")
def get_template(state: StateDep, template_id: str) -> ShotTemplate:
    return state.templates.get(template_id)


@library.patch("/{template_id}")
def update_template(state: StateDep, template_id: str, body: UpdateTemplateRequest) -> ShotTemplate:
    return state.templates.update_metadata(
        template_id, name=body.name, description=body.description
    )


@library.delete("/{template_id}", status_code=204)
def delete_template(state: StateDep, template_id: str) -> None:
    """Remove a template from the library.

    Instances that already reference it are left alone rather than deleted: the shot they are in still
    has the workflows, and the validator will say plainly that the template is gone. Quietly tearing
    someone's graph apart on a library tidy-up would be worse than a clear error.
    """
    state.templates.delete(template_id)


@library.patch("/{template_id}/ports/{port_key}")
def update_port(
    state: StateDep, template_id: str, port_key: str, body: PromotionRequest
) -> ShotTemplate:
    """Rename or hide one port on the container node."""
    template = state.templates.get(template_id)
    port = template.port(port_key)
    if port is None:
        raise NotFound(f"Template {template_id!r} has no port {port_key!r}")
    if body.label is not None:
        port.label = body.label
    if body.shown is not None:
        port.shown = body.shown
    return _rewrite(state, template)


@library.patch("/{template_id}/controls/{control_key}")
def update_control(
    state: StateDep, template_id: str, control_key: str, body: PromotionRequest
) -> ShotTemplate:
    """Show, hide or rename one control on the container node."""
    template = state.templates.get(template_id)
    control = template.control(control_key)
    if control is None:
        raise NotFound(f"Template {template_id!r} has no control {control_key!r}")
    if body.label is not None:
        control.label = body.label
    if body.shown is not None:
        control.shown = body.shown
    return _rewrite(state, template)


def _rewrite(state, template: ShotTemplate) -> ShotTemplate:
    """Persist a change to a template's surface, without re-copying the workflows it already has.

    Changing what the node exposes changes what placed instances can be wired to, so this *is* a revision
    bump — an instance linked to a port that has just been hidden needs to read as out of date.
    """
    template.touch()
    return state.templates.write(template)


# -- saving a shot -------------------------------------------------------------------------------------


@router.post("/shots/{shot_id}/save-as-template", status_code=201)
def save_shot_as_template(
    state: StateDep, project: ProjectDep, shot: ShotDep, body: SaveTemplateRequest
) -> ShotTemplate:
    """Lift this shot into the shared library.

    Passing ``template_id`` overwrites an existing template, which is how an improvement reaches every
    shot that already placed it.
    """
    revision = 1
    if body.template_id:
        try:
            revision = state.templates.get(body.template_id).revision + 1
        except NotFound:
            revision = 1

    try:
        captured = capture_shot(
            project,
            shot,
            state.store,
            name=body.name or shot.name,
            description=body.description,
            template_id=body.template_id,
            revision=revision,
        )
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc

    template = state.templates.save(captured.template, captured.graphs)
    logger.info("Saved shot %r as template %s", shot.name, template.id)
    return template


# -- instances -----------------------------------------------------------------------------------------


def _find_instance(project, instance_id: str) -> tuple[Shot, TemplateInstance]:
    for shot in project.shots:
        instance = shot.instance(instance_id)
        if instance is not None:
            return shot, instance
    raise NotFound(f"No template instance {instance_id!r} in this project")


@router.post("/shots/{shot_id}/instances", status_code=201)
def place(
    state: StateDep, project: ProjectDep, shot: ShotDep, body: PlaceInstanceRequest
) -> TemplateInstance:
    """Place a template on this shot's canvas as one node."""
    template = state.templates.get(body.template_id)
    instance = place_instance(
        project, shot, template, state.templates, state.store,
        name=body.name or "", ui_pos=body.ui_pos,
    )
    state.store.save(project)
    state.events.emit("project.changed", project_id=project.id, data={"action": "instance_placed"})
    return instance


@router.patch("/instances/{instance_id}")
def update_instance(
    state: StateDep, project: ProjectDep, instance_id: str, body: UpdateInstanceRequest
) -> TemplateInstance:
    _shot, instance = _find_instance(project, instance_id)

    if body.param_overrides is not None:
        instance.param_overrides.update(body.param_overrides)
    for field in ("name", "enabled", "ui_pos", "ui_size"):
        value = getattr(body, field)
        if value is not None:
            setattr(instance, field, value)

    state.store.save(project)
    return instance


@router.post("/instances/{instance_id}/sync")
def sync(state: StateDep, project: ProjectDep, instance_id: str) -> dict:
    """Reconcile one instance with its template, and report what that changed."""
    _shot, instance = _find_instance(project, instance_id)
    template = state.templates.get(instance.template_id)
    changes = sync_instance(project, instance, template, state.templates, state.store)
    state.store.save(project)
    state.events.emit("project.changed", project_id=project.id, data={"action": "instance_synced"})
    return {"instance": instance.model_dump(mode="json"), "changes": changes}


@router.delete("/instances/{instance_id}", status_code=204)
def delete_instance(state: StateDep, project: ProjectDep, instance_id: str) -> None:
    shot, instance = _find_instance(project, instance_id)
    shot.instances = [i for i in shot.instances if i.id != instance.id]
    shot.links = [
        link for link in shot.links if instance.id not in (link.from_step, link.to_step)
    ]
    state.store.save(project)
    state.events.emit("project.changed", project_id=project.id, data={"action": "instance_removed"})


@router.get("/shots/{shot_id}/placed")
def placed_templates(state: StateDep, project: ProjectDep, shot: ShotDep) -> list[dict]:
    """The templates this shot places, with each instance's surface and whether it is up to date.

    One request rather than one per instance: the canvas needs every instance's ports and controls before
    it can draw anything, and a template placed five times should not be fetched five times.
    """
    seen: dict[str, ShotTemplate] = {}
    result: list[dict] = []
    for instance in shot.instances:
        template = seen.get(instance.template_id)
        if template is None:
            try:
                template = state.templates.get(instance.template_id)
            except NotFound:
                result.append(
                    {
                        "instance_id": instance.id,
                        "template_id": instance.template_id,
                        "missing": True,
                    }
                )
                continue
            seen[instance.template_id] = template
        result.append(
            {
                "instance_id": instance.id,
                "template_id": template.id,
                "missing": False,
                "stale": instance.template_revision != template.revision,
                "summary": summarize(template).model_dump(mode="json"),
                "ports": [p.model_dump(mode="json") for p in template.shown_ports],
                "controls": [c.model_dump(mode="json") for c in template.shown_controls],
            }
        )
    return result
