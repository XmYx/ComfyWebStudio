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
from pydantic import BaseModel

from ..core.errors import NotFound, ValidationFailed
from ..core.models import Asset, AssetSource, utcnow
from ..media import waveform
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


@router.get("/waveform")
def get_waveform(
    state: StateDep, project: ProjectDep, path: str = Query(...), buckets: int = Query(800),
) -> dict:
    """Peak data for drawing one audio file as a waveform.

    Separate from ``/media`` because the browser wants a *drawing*, not the audio: this is a few hundred
    min/max pairs rather than several megabytes of samples it would have to decode itself.
    """
    resolved = state.store.resolve(project.id, path)
    if not resolved.is_file():
        raise NotFound(f"No such file: {Path(path).name}")
    try:
        return waveform.compute(resolved, buckets).as_dict()
    except Exception as exc:  # noqa: BLE001 - a file with no audio is a normal thing to ask about
        raise ValidationFailed(f"Could not read audio from {Path(path).name}: {exc}") from exc


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


class CaptureAssetRequest(BaseModel):
    """Save what a step produced as a named asset of the project."""

    shot_id: str
    step_id: str
    port_key: str
    name: str | None = None


def _artifact_for(state, project, body: CaptureAssetRequest):
    """The most recent successful artifact on that port, or None when the step has not produced one."""
    latest = state.store.latest_step_runs(project.id, body.shot_id).get(body.step_id)
    if latest is None:
        return None
    return next(
        (a for a in latest["step_run"].outputs if a.port_key == body.port_key), None
    )


@router.post("/assets/capture", status_code=201)
def capture_asset(state: StateDep, project: ProjectDep, body: CaptureAssetRequest) -> Asset:
    """Promote a step's output into an asset that remembers what made it.

    The media is not copied: a run's artifacts already live in the project's content-addressed store, so
    the asset points at the same bytes. What the asset adds is a name, a place in the library, and the
    source it can be refreshed from.
    """
    artifact = _artifact_for(state, project, body)
    if artifact is None:
        raise ValidationFailed(
            "That step has not produced anything on that port yet. Run it first."
        )

    asset = Asset(
        name=body.name or f"{body.port_key}",
        kind=artifact.kind,
        path=artifact.path,
        thumb=artifact.thumb,
        sha256=artifact.sha256,
        meta=dict(artifact.meta),
        source=AssetSource(shot_id=body.shot_id, step_id=body.step_id, port_key=body.port_key),
        generated=utcnow(),
    )
    project.assets[asset.id] = asset
    state.store.save(project)
    state.events.emit("project.changed", project_id=project.id, data={"action": "asset_captured"})
    return asset


@router.post("/assets/{asset_id}/refresh")
def refresh_asset(state: StateDep, project: ProjectDep, asset_id: str) -> Asset:
    """Point a generated asset at its source's latest result.

    Deliberately does not run anything. Re-running is what the Run button is for, and an asset silently
    kicking off a GPU job because someone opened a panel would be a surprise nobody asked for.
    """
    asset = project.assets.get(asset_id)
    if asset is None:
        raise NotFound(f"No asset {asset_id!r}")
    if asset.source is None:
        raise ValidationFailed(
            f"{asset.name!r} was imported, so there is nothing to refresh it from."
        )

    artifact = _artifact_for(
        state,
        project,
        CaptureAssetRequest(
            shot_id=asset.source.shot_id,
            step_id=asset.source.step_id,
            port_key=asset.source.port_key,
        ),
    )
    if artifact is None:
        raise ValidationFailed(
            "The step this asset comes from has no result yet. Run it, then refresh."
        )

    if artifact.sha256 and artifact.sha256 == asset.sha256:
        return asset  # already current; nothing to write

    asset.kind = artifact.kind
    asset.path = artifact.path
    asset.thumb = artifact.thumb
    asset.sha256 = artifact.sha256
    asset.meta = dict(artifact.meta)
    asset.generated = utcnow()
    state.store.save(project)
    state.events.emit("project.changed", project_id=project.id, data={"action": "asset_refreshed"})
    return asset


class RenameAssetRequest(BaseModel):
    name: str


@router.patch("/assets/{asset_id}")
def rename_asset(
    state: StateDep, project: ProjectDep, asset_id: str, body: RenameAssetRequest
) -> Asset:
    asset = project.assets.get(asset_id)
    if asset is None:
        raise NotFound(f"No asset {asset_id!r}")
    asset.name = body.name
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
