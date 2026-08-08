"""Serving artifacts, thumbnails and imported assets.

Every media byte the browser sees comes through here rather than from ComfyUI directly. That is not just
tidiness: ComfyUI's default origin-only middleware (``server.py:147-185``) 403s cross-origin browser
requests, so a frontend fetching ``/view`` itself would simply fail.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, Query, UploadFile
from fastapi.responses import FileResponse

from ..core.errors import NotFound, ValidationFailed
from ..core.models import Asset
from ..media.probe import guess_kind, probe
from .deps import ProjectDep, StateDep

router = APIRouter(prefix="/api/projects/{project_id}", tags=["media"])

#: Serve inline only for types a browser renders safely. Anything else downloads, so an uploaded .html
#: cannot execute against our origin.
_INLINE_TYPES = {
    "image/png", "image/jpeg", "image/webp", "image/gif",
    "video/mp4", "video/webm",
    "audio/mpeg", "audio/wav", "audio/flac", "audio/ogg", "audio/x-flac",
    "text/plain",
}


def _serve(path: Path, download_name: str | None = None) -> FileResponse:
    if not path.is_file():
        raise NotFound(f"No such file: {path.name}")

    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    inline = media_type in _INLINE_TYPES
    return FileResponse(
        path,
        media_type=media_type if inline else "application/octet-stream",
        filename=download_name if not inline else None,
        headers={
            "Content-Disposition": f'{"inline" if inline else "attachment"}; filename="{path.name}"',
            # Content-addressed paths never change content, so they can be cached hard.
            "Cache-Control": "public, max-age=31536000, immutable",
        },
    )


@router.get("/media")
def get_media(state: StateDep, project: ProjectDep, path: str = Query(...)) -> FileResponse:
    """Serve one project file by its stored (project-relative) path."""
    return _serve(state.store.resolve(project.id, path))


@router.get("/assets")
def list_assets(project: ProjectDep) -> list[Asset]:
    return list(project.assets.values())


@router.post("/assets", status_code=201)
async def upload_asset(state: StateDep, project: ProjectDep, file: UploadFile) -> Asset:
    """Import media the project should own but no step produces — footage, stills, music."""
    data = await file.read()
    if not data:
        raise ValidationFailed("The uploaded file is empty.")

    name = (file.filename or "asset").rsplit("/", 1)[-1]
    kind = guess_kind(Path(name))
    extension = Path(name).suffix.lstrip(".") or "bin"

    relative, sha = state.media_store.ingest_bytes(project.id, data, kind=kind, extension=extension)
    path = state.media_store.path(project.id, relative)

    asset = Asset(
        name=name,
        kind=kind,  # type: ignore[arg-type]
        path=relative,
        sha256=sha,
        meta=probe(path, kind),
        thumb=state.media.thumbs.ensure(project.id, path, sha, kind),
    )
    project.assets[asset.id] = asset
    state.store.save(project)
    return asset


@router.delete("/assets/{asset_id}", status_code=204)
def delete_asset(state: StateDep, project: ProjectDep, asset_id: str) -> None:
    if asset_id not in project.assets:
        raise NotFound(f"No asset {asset_id!r}")

    used_by = [
        clip.name or clip.id
        for track in project.timeline.tracks
        for clip in track.clips
        if clip.source.asset_id == asset_id
    ]
    if used_by:
        raise ValidationFailed(
            "This asset is on the timeline (" + ", ".join(used_by[:3]) + "). Remove those clips first."
        )

    project.assets.pop(asset_id)
    state.store.save(project)
