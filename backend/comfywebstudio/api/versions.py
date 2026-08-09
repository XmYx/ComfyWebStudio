"""Version history — the Edit menu's undo/redo, and the History panel.

The same log serves both. Undo and redo move a pointer along it; the History panel reads it directly and
can restore any point, or any single element from any point.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from ..core.errors import NotFound, ValidationFailed
from ..core.models import Project
from .deps import ProjectDep, StateDep

router = APIRouter(prefix="/api/projects/{project_id}", tags=["versions"])


class TagVersionRequest(BaseModel):
    label: str


class RelabelRequest(BaseModel):
    label: str | None = None


class RestoreElementRequest(BaseModel):
    scope: str
    target_id: str


# -- undo / redo ---------------------------------------------------------------------------------------


@router.get("/history")
def history_state(state: StateDep, project: ProjectDep) -> dict:
    """Whether Undo and Redo should be enabled in the Edit menu."""
    return state.store.versions(project.id).depths()


@router.post("/undo")
def undo(state: StateDep, project: ProjectDep) -> Project:
    snapshot = state.store.versions(project.id).undo()
    if snapshot is None:
        raise ValidationFailed("There is nothing to undo.")
    restored = state.store.restore(snapshot)
    state.events.emit("project.changed", project_id=project.id, data={"action": "undo"})
    return restored


@router.post("/redo")
def redo(state: StateDep, project: ProjectDep) -> Project:
    snapshot = state.store.versions(project.id).redo()
    if snapshot is None:
        raise ValidationFailed("There is nothing to redo.")
    restored = state.store.restore(snapshot)
    state.events.emit("project.changed", project_id=project.id, data={"action": "redo"})
    return restored


# -- history -------------------------------------------------------------------------------------------


@router.get("/versions")
def list_versions(
    state: StateDep,
    project: ProjectDep,
    scope: str | None = None,
    target_id: str | None = Query(default=None, description="Only versions that touched this element"),
    include_layout: bool = False,
    named_only: bool = False,
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[dict]:
    """The change log, newest first.

    Pass ``target_id`` for one element's history — that is what the per-step History tab uses.
    """
    versions = state.store.versions(project.id).list(
        scope=scope,
        target_id=target_id,
        include_layout=include_layout,
        named_only=named_only,
        limit=limit,
    )
    return [v.to_dict() for v in versions]


@router.get("/versions/{version_id}")
def get_version(state: StateDep, project: ProjectDep, version_id: str) -> dict:
    return state.store.versions(project.id).get(version_id).to_dict()


@router.post("/versions", status_code=201)
def tag_version(state: StateDep, project: ProjectDep, body: TagVersionRequest) -> dict:
    """Name the current state so it can be found again later."""
    version = state.store.versions(project.id).tag(body.label, project.model_dump(mode="json"))
    state.events.emit(
        "project.versioned", project_id=project.id,
        data={"version_id": version.id, "label": version.label},
    )
    return version.to_dict()


@router.patch("/versions/{version_id}")
def relabel_version(
    state: StateDep, project: ProjectDep, version_id: str, body: RelabelRequest
) -> dict:
    return state.store.versions(project.id).relabel(version_id, body.label).to_dict()


@router.post("/versions/{version_id}/restore")
def restore_version(state: StateDep, project: ProjectDep, version_id: str) -> Project:
    """Roll the whole project back.

    The rollback is itself recorded, so it can be undone — restoring an old version never loses the state
    you were in when you did it.
    """
    versions = state.store.versions(project.id)
    snapshot = versions.snapshot(version_id)

    restored = Project.model_validate(snapshot)
    restored.id = project.id  # a snapshot taken before a rename must not resurrect an old id
    state.store.save(restored)

    state.events.emit(
        "project.changed", project_id=project.id,
        data={"action": "restored", "version_id": version_id},
    )
    return state.store.load(project.id)


@router.post("/versions/{version_id}/restore-element")
def restore_element(
    state: StateDep, project: ProjectDep, version_id: str, body: RestoreElementRequest
) -> Project:
    """Put one shot, step, track or workflow back as it was, leaving the rest of the project alone."""
    versions = state.store.versions(project.id)
    merged = versions.restore_element(
        version_id, body.scope, body.target_id, project.model_dump(mode="json")
    )

    restored = Project.model_validate(merged)
    restored.id = project.id
    state.store.save(restored)

    state.events.emit(
        "project.changed", project_id=project.id,
        data={
            "action": "restored_element",
            "version_id": version_id,
            "scope": body.scope,
            "target_id": body.target_id,
        },
    )
    return state.store.load(project.id)


@router.delete("/versions", status_code=204)
def clear_history(state: StateDep, project: ProjectDep) -> None:
    state.store.versions(project.id).clear()


# -- shot-level convenience ----------------------------------------------------------------------------


@router.get("/shots/{shot_id}/versions")
def shot_versions(
    state: StateDep, project: ProjectDep, shot_id: str, limit: int = 100
) -> list[dict]:
    """Everything that ever happened to one shot, including its steps and links.

    A shot's history is the union of its own changes and those of the elements inside it — otherwise
    "what changed in this shot?" would miss every parameter edit.
    """
    shot = project.shot(shot_id)
    if shot is None:
        raise NotFound(f"No shot {shot_id!r}")

    member_ids = {shot_id}
    member_ids.update(step.id for step in shot.steps)
    member_ids.update(link.id for link in shot.links)

    versions = state.store.versions(project.id)
    results: list[dict] = []
    for version in versions.list(limit=1000, include_layout=True):
        relevant = [
            change
            for change in version.changes
            if change.target_id in member_ids or change.detail.get("shot_id") == shot_id
        ]
        if not relevant and not version.is_named:
            continue
        payload = version.to_dict()
        payload["changes"] = [c.to_dict() for c in relevant]
        payload["summary"] = relevant[0].summary if relevant else payload["summary"]
        results.append(payload)
        if len(results) >= limit:
            break
    return results


@router.post("/shots/{shot_id}/versions", status_code=201)
def tag_shot_version(
    state: StateDep, project: ProjectDep, shot_id: str, body: TagVersionRequest
) -> dict:
    """Name the current state of a shot. Restoring it brings back only that shot."""
    shot = project.shot(shot_id)
    if shot is None:
        raise NotFound(f"No shot {shot_id!r}")

    version = state.store.versions(project.id).tag(
        f"{shot.name}: {body.label}", project.model_dump(mode="json")
    )
    return version.to_dict()
