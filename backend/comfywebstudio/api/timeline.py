"""The timeline: tracks, clips, and rendering the whole thing."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from ..core.errors import NotFound, ValidationFailed
from ..core.ids import new_id, slugify
from ..core.models import Clip, ClipSource, Timeline, Track, TrackKind
from ..execution.events import StudioEvent
from ..render.compositor import TimelineResolver
from ..render.encoder import TimelineRenderer
from .deps import ProjectDep, StateDep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects/{project_id}/timeline", tags=["timeline"])


class UpdateTimelineRequest(BaseModel):
    fps: float | None = None
    width: int | None = None
    height: int | None = None
    background: str | None = None


class CreateTrackRequest(BaseModel):
    kind: TrackKind = "video"
    name: str | None = None


class UpdateTrackRequest(BaseModel):
    name: str | None = None
    muted: bool | None = None
    locked: bool | None = None


class CreateClipRequest(BaseModel):
    source: ClipSource | None = None
    start: float | None = None
    duration: float | None = None
    name: str = ""
    text: str = ""


class RenderRequest(BaseModel):
    name: str | None = None
    still: bool = False
    time_s: float = 0.0


# -- timeline ------------------------------------------------------------------------------------------


@router.get("")
def get_timeline(project: ProjectDep) -> Timeline:
    return project.timeline


@router.patch("")
def update_timeline(state: StateDep, project: ProjectDep, body: UpdateTimelineRequest) -> Timeline:
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(project.timeline, field, value)
    state.store.save(project)
    return project.timeline


@router.get("/resolved")
def resolved_timeline(state: StateDep, project: ProjectDep) -> dict:
    """Every clip with its media located, so the UI can show real thumbnails and flag broken clips."""
    resolver = TimelineResolver(state.store, project)
    clips: list[dict[str, Any]] = []

    for track in project.timeline.tracks:
        for clip in track.clips:
            artifacts, error = resolver.artifacts_for(clip)
            clips.append(
                {
                    "track_id": track.id,
                    "clip_id": clip.id,
                    "error": error,
                    "kind": artifacts[0].kind if artifacts else ("text" if clip.text else None),
                    "artifacts": [a.model_dump(mode="json") for a in artifacts],
                }
            )

    return {"duration": project.timeline.duration, "clips": clips}


# -- tracks --------------------------------------------------------------------------------------------


@router.post("/tracks", status_code=201)
def create_track(state: StateDep, project: ProjectDep, body: CreateTrackRequest) -> Track:
    existing = sum(1 for t in project.timeline.tracks if t.kind == body.kind)
    track = Track(kind=body.kind, name=body.name or f"{body.kind.title()} {existing + 1}")
    project.timeline.tracks.append(track)
    state.store.save(project)
    return track


@router.patch("/tracks/{track_id}")
def update_track(
    state: StateDep, project: ProjectDep, track_id: str, body: UpdateTrackRequest
) -> Track:
    track = project.timeline.track(track_id)
    if track is None:
        raise NotFound(f"No track {track_id!r}")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(track, field, value)
    state.store.save(project)
    return track


@router.delete("/tracks/{track_id}", status_code=204)
def delete_track(state: StateDep, project: ProjectDep, track_id: str) -> None:
    before = len(project.timeline.tracks)
    project.timeline.tracks = [t for t in project.timeline.tracks if t.id != track_id]
    if len(project.timeline.tracks) == before:
        raise NotFound(f"No track {track_id!r}")
    state.store.save(project)


@router.post("/tracks/reorder")
def reorder_tracks(state: StateDep, project: ProjectDep, track_ids: list[str]) -> Timeline:
    """Set track stacking order. Later tracks composite on top of earlier ones."""
    by_id = {t.id: t for t in project.timeline.tracks}
    if set(track_ids) != set(by_id):
        raise ValidationFailed("The reorder list must contain exactly the existing track ids.")
    project.timeline.tracks = [by_id[tid] for tid in track_ids]
    state.store.save(project)
    return project.timeline


# -- clips ---------------------------------------------------------------------------------------------


def _default_duration(state, project, source: ClipSource | None, fallback: float) -> float:
    """Length a new clip should take from its media, rather than making the user set it every time."""
    if source is None:
        return fallback
    resolver = TimelineResolver(state.store, project)
    artifacts, error = resolver.artifacts_for(Clip(source=source))
    if error or not artifacts:
        return fallback

    meta = artifacts[0].meta
    duration = meta.get("duration")
    if duration:
        return float(duration)
    if len(artifacts) > 1:  # an image sequence: one frame each at project fps
        return len(artifacts) / max(project.timeline.fps, 1.0)
    return fallback


@router.post("/tracks/{track_id}/clips", status_code=201)
def create_clip(
    state: StateDep, project: ProjectDep, track_id: str, body: CreateClipRequest
) -> Clip:
    track = project.timeline.track(track_id)
    if track is None:
        raise NotFound(f"No track {track_id!r}")
    if track.locked:
        raise ValidationFailed(f"Track {track.name!r} is locked.")

    # Append after the last clip by default, which is what "add to timeline" almost always means.
    start = body.start if body.start is not None else max((c.end for c in track.clips), default=0.0)
    duration = body.duration or _default_duration(state, project, body.source, 3.0)

    clip = Clip(
        name=body.name,
        source=body.source or ClipSource(kind="step_output"),
        start=start,
        duration=max(0.04, duration),
        text=body.text,
    )
    track.clips.append(clip)
    track.clips.sort(key=lambda c: c.start)
    state.store.save(project)
    return clip


@router.patch("/tracks/{track_id}/clips/{clip_id}")
def update_clip(
    state: StateDep, project: ProjectDep, track_id: str, clip_id: str, body: dict
) -> Clip:
    track = project.timeline.track(track_id)
    if track is None:
        raise NotFound(f"No track {track_id!r}")
    clip = track.clip(clip_id)
    if clip is None:
        raise NotFound(f"No clip {clip_id!r}")
    if track.locked:
        raise ValidationFailed(f"Track {track.name!r} is locked.")

    updated = clip.model_copy(update={k: v for k, v in body.items() if k in Clip.model_fields})
    updated.start = max(0.0, updated.start)
    updated.duration = max(0.04, updated.duration)  # one frame at 24fps is the floor

    track.clips = sorted(
        [updated if c.id == clip_id else c for c in track.clips], key=lambda c: c.start
    )
    state.store.save(project)
    return updated


@router.delete("/tracks/{track_id}/clips/{clip_id}", status_code=204)
def delete_clip(state: StateDep, project: ProjectDep, track_id: str, clip_id: str) -> None:
    track = project.timeline.track(track_id)
    if track is None:
        raise NotFound(f"No track {track_id!r}")
    before = len(track.clips)
    track.clips = [c for c in track.clips if c.id != clip_id]
    if len(track.clips) == before:
        raise NotFound(f"No clip {clip_id!r}")
    state.store.save(project)


@router.post("/from-shots", status_code=201)
def build_from_shots(
    state: StateDep, project: ProjectDep, shot_ids: list[str] | None = None
) -> Timeline:
    """Lay every shot's final output end to end — the one-click way to get from shots to a cut.

    A shot's "final output" is the last step in dependency order that produced something.
    """
    from ..core.graph import topological_order

    shots = [s for s in project.shots if shot_ids is None or s.id in shot_ids]
    if not shots:
        raise ValidationFailed("No shots to build from.")

    track = Track(kind="video", name="Shots")
    resolver = TimelineResolver(state.store, project)
    cursor = 0.0
    skipped: list[str] = []

    for shot in shots:
        try:
            order = topological_order(shot)
        except Exception:  # noqa: BLE001 - a broken shot should not stop the others
            skipped.append(shot.name)
            continue

        placed = False
        for step_id in reversed(order):
            step = shot.step(step_id)
            workflow = project.workflow(step.workflow_id) if step else None
            if step is None or workflow is None or not step.enabled:
                continue
            for port in workflow.outputs:
                if port.kind not in {"image", "video"}:
                    continue
                source = ClipSource(
                    kind="step_output", shot_id=shot.id, step_id=step.id, port_key=port.key
                )
                artifacts, error = resolver.artifacts_for(Clip(source=source))
                if error or not artifacts:
                    continue

                duration = _default_duration(state, project, source, 3.0)
                track.clips.append(
                    Clip(name=shot.name, source=source, start=cursor, duration=duration)
                )
                cursor += duration
                placed = True
                break
            if placed:
                break

        if not placed:
            skipped.append(shot.name)

    if not track.clips:
        raise ValidationFailed(
            "None of the selected shots have produced image or video output yet. Run them first."
        )

    project.timeline.tracks.append(track)
    state.store.save(project)
    if skipped:
        state.events.emit(
            "timeline.built",
            project_id=project.id,
            data={"skipped": skipped, "message": "Some shots had no usable output yet."},
        )
    return project.timeline


# -- render --------------------------------------------------------------------------------------------


@router.post("/render", status_code=202)
async def render_timeline(state: StateDep, project: ProjectDep, body: RenderRequest) -> dict:
    """Render the timeline. Runs in a worker thread; progress arrives on the event bus."""
    if project.timeline.duration <= 0 and not body.still:
        raise ValidationFailed("The timeline is empty — add at least one clip before rendering.")

    render_id = new_id("render")
    name = slugify(body.name or f"{project.name}-{render_id[-6:]}")
    destination = state.store.renders_dir(project.id) / name

    settings = state.settings.render.model_copy()
    settings.fps = project.timeline.fps or settings.fps

    loop = asyncio.get_running_loop()

    def progress(fraction: float, message: str) -> None:
        # Called from the render thread; hop back onto the loop before touching the event bus.
        event = StudioEvent(
            type="render.progress",
            project_id=project.id,
            data={"render_id": render_id, "progress": fraction, "message": message},
        )
        loop.call_soon_threadsafe(state.events.publish, event)

    renderer = TimelineRenderer(state.store, project, settings, on_progress=progress)

    async def run() -> None:
        try:
            # PyAV encoding is CPU-bound and releases the GIL; a thread keeps the API responsive.
            result = await asyncio.to_thread(
                renderer.render_still if body.still else renderer.render,
                destination,
                **({"time_s": body.time_s} if body.still else {}),
            )
            state.events.emit(
                "render.finished",
                project_id=project.id,
                data={
                    "render_id": render_id,
                    "ok": True,
                    "path": state.store.relativize(project.id, result.path),
                    "kind": result.kind,
                    "duration": result.duration,
                    "frames": result.frames,
                    "warnings": result.warnings,
                },
            )
        except Exception as exc:  # noqa: BLE001 - report, never crash the server
            logger.exception("Render %s failed", render_id)
            state.events.emit(
                "render.finished",
                project_id=project.id,
                data={"render_id": render_id, "ok": False, "error": str(exc)},
            )

    asyncio.create_task(run())
    return {"render_id": render_id, "status": "started"}


@router.get("/renders")
def list_renders(state: StateDep, project: ProjectDep) -> list[dict]:
    directory = state.store.renders_dir(project.id)
    return [
        {
            "name": path.name,
            "path": state.store.relativize(project.id, path),
            "size": path.stat().st_size,
            "modified": path.stat().st_mtime,
        }
        for path in sorted(directory.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        if path.is_file()
    ]
