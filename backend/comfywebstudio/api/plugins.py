"""Plugin management, and undo/redo — the endpoints behind the File, Edit and Plugins menus."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..core.errors import NotFound, ValidationFailed
from ..core.ids import slugify
from ..core.models import Project
from .deps import ProjectDep, StateDep

router = APIRouter(prefix="/api", tags=["plugins"])


class BuildPluginRequest(BaseModel):
    name: str
    workflow_ids: list[str] = []
    shot_ids: list[str] | None = None
    version: str = "1.0.0"
    author: str = ""
    description: str = ""


class ApplyPluginRequest(BaseModel):
    project_id: str
    include_shots: bool = True


# -- undo / redo ---------------------------------------------------------------------------------------


@router.get("/projects/{project_id}/history")
def history_state(state: StateDep, project: ProjectDep) -> dict:
    """Whether Undo and Redo should be enabled in the Edit menu."""
    return state.store.history.depths(project.id)


@router.post("/projects/{project_id}/undo")
def undo(state: StateDep, project: ProjectDep) -> Project:
    snapshot = state.store.history.undo(project.id, project.model_dump(mode="json"))
    if snapshot is None:
        raise ValidationFailed("There is nothing to undo.")
    restored = state.store.restore(snapshot)
    state.events.emit("project.changed", project_id=project.id, data={"action": "undo"})
    return restored


@router.post("/projects/{project_id}/redo")
def redo(state: StateDep, project: ProjectDep) -> Project:
    snapshot = state.store.history.redo(project.id, project.model_dump(mode="json"))
    if snapshot is None:
        raise ValidationFailed("There is nothing to redo.")
    restored = state.store.restore(snapshot)
    state.events.emit("project.changed", project_id=project.id, data={"action": "redo"})
    return restored


# -- plugins -------------------------------------------------------------------------------------------


@router.get("/plugins")
def list_plugins(state: StateDep) -> list[dict]:
    return state.plugins.list()


@router.post("/plugins/install", status_code=201)
async def install_plugin(state: StateDep, file: UploadFile, overwrite: bool = False) -> dict:
    if not (file.filename or "").endswith(".cwsplugin"):
        raise ValidationFailed("Expected a .cwsplugin file.")

    with tempfile.NamedTemporaryFile(
        suffix=".cwsplugin", dir=state.settings.temp_dir, delete=False
    ) as handle:
        temp_path = Path(handle.name)
        shutil.copyfileobj(file.file, handle)

    try:
        manifest = state.plugins.install(temp_path, overwrite=overwrite)
    finally:
        temp_path.unlink(missing_ok=True)

    state.events.emit("plugins.changed", data={"action": "installed", "id": manifest.id})
    return manifest.to_dict()


@router.delete("/plugins/{plugin_id}", status_code=204)
def uninstall_plugin(state: StateDep, plugin_id: str) -> None:
    state.plugins.uninstall(plugin_id)
    state.events.emit("plugins.changed", data={"action": "uninstalled", "id": plugin_id})


@router.post("/plugins/{plugin_id}/enabled")
def set_plugin_enabled(state: StateDep, plugin_id: str, enabled: bool = True) -> dict:
    state.plugins.set_enabled(plugin_id, enabled)
    return {"id": plugin_id, "enabled": enabled}


@router.post("/plugins/{plugin_id}/apply")
def apply_plugin(state: StateDep, plugin_id: str, body: ApplyPluginRequest) -> dict:
    project = state.store.load(body.project_id)
    result = state.plugins.apply(plugin_id, project, include_shots=body.include_shots)
    state.events.emit("project.changed", project_id=project.id, data={"action": "plugin_applied"})
    return result


@router.post("/projects/{project_id}/plugins/build")
def build_plugin(state: StateDep, project: ProjectDep, body: BuildPluginRequest) -> FileResponse:
    """Package selected workflows and shots into a downloadable ``.cwsplugin``."""
    target = Path(state.settings.temp_dir) / f"{slugify(body.name)}.cwsplugin"  # type: ignore[arg-type]
    archive = state.plugins.build(
        project,
        target,
        name=body.name,
        workflow_ids=body.workflow_ids,
        shot_ids=body.shot_ids,
        version=body.version,
        author=body.author,
        description=body.description,
    )
    return FileResponse(archive, media_type="application/zip", filename=archive.name)


@router.get("/plugins/{plugin_id}/download")
def download_plugin(state: StateDep, plugin_id: str) -> FileResponse:
    """Re-export an installed plugin, so it can be passed on."""
    import zipfile

    manifest = state.plugins.get(plugin_id)
    directory = state.plugins._plugin_dir(plugin_id)
    if not directory.is_dir():
        raise NotFound(f"No plugin {plugin_id!r}")

    target = Path(state.settings.temp_dir) / f"{slugify(manifest.name)}.cwsplugin"  # type: ignore[arg-type]
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_file() and path.name != ".disabled":
                archive.write(path, str(path.relative_to(directory)))

    return FileResponse(target, media_type="application/zip", filename=target.name)
