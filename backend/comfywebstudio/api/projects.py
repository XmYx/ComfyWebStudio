"""Project lifecycle: create, list, open, save, duplicate, export, import."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..core.errors import ValidationFailed
from ..core.models import Project, ProjectSettings
from .deps import ProjectDep, StateDep

router = APIRouter(prefix="/api/projects", tags=["projects"])


class CreateProjectRequest(BaseModel):
    name: str
    description: str = ""


class UpdateProjectRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    settings: ProjectSettings | None = None


@router.get("")
def list_projects(state: StateDep) -> list[dict]:
    return state.store.list_projects()


@router.post("", status_code=201)
def create_project(state: StateDep, body: CreateProjectRequest) -> Project:
    project = state.store.create(body.name, description=body.description)
    state.events.emit("project.changed", project_id=project.id, data={"action": "created"})
    return project


@router.get("/{project_id}")
def get_project(project: ProjectDep) -> Project:
    return project


@router.patch("/{project_id}")
def update_project(state: StateDep, project: ProjectDep, body: UpdateProjectRequest) -> Project:
    if body.name is not None:
        project.name = body.name
    if body.description is not None:
        project.description = body.description
    if body.settings is not None:
        project.settings = body.settings
        # The timeline inherits the project's format unless the user has already diverged it.
        project.timeline.fps = body.settings.fps
        project.timeline.width = body.settings.width
        project.timeline.height = body.settings.height

    state.store.save(project)
    state.events.emit("project.changed", project_id=project.id, data={"action": "updated"})
    return project


@router.delete("/{project_id}", status_code=204)
def delete_project(state: StateDep, project_id: str) -> None:
    state.store.delete(project_id)
    state.events.emit("project.changed", project_id=project_id, data={"action": "deleted"})


@router.post("/{project_id}/duplicate", status_code=201)
def duplicate_project(state: StateDep, project_id: str, name: str | None = None) -> Project:
    return state.store.duplicate(project_id, new_name=name)


@router.get("/{project_id}/export")
def export_project(
    state: StateDep,
    project: ProjectDep,
    include_assets: bool = Query(True),
    include_renders: bool = Query(False),
    include_runs: bool = Query(True),
) -> FileResponse:
    """Download a ``.cwsproj`` archive.

    Written into the app's temp directory rather than the project, so a partial download leaves nothing
    behind inside the project itself.
    """
    from ..core.ids import slugify

    target = Path(state.settings.temp_dir) / f"{slugify(project.name)}.cwsproj"  # type: ignore[arg-type]
    archive = state.store.export(
        project.id,
        target,
        include_assets=include_assets,
        include_renders=include_renders,
        include_runs=include_runs,
    )
    return FileResponse(
        archive,
        media_type="application/zip",
        filename=archive.name,
    )


@router.post("/import", status_code=201)
async def import_project(state: StateDep, file: UploadFile, name: str | None = None) -> Project:
    if not (file.filename or "").endswith(".cwsproj"):
        raise ValidationFailed("Expected a .cwsproj file.")

    with tempfile.NamedTemporaryFile(
        suffix=".cwsproj", dir=state.settings.temp_dir, delete=False
    ) as handle:
        temp_path = Path(handle.name)
        shutil.copyfileobj(file.file, handle)

    try:
        project = state.store.import_archive(temp_path, new_name=name)
    finally:
        temp_path.unlink(missing_ok=True)

    state.events.emit("project.changed", project_id=project.id, data={"action": "imported"})
    return project
