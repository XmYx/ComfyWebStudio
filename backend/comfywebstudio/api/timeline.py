"""The timeline: tracks, clips, and rendering the whole thing."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel

from ..core.errors import NotFound, ValidationFailed
from ..core.ids import new_id, slugify
from ..core.models import Clip, ClipSource, Timeline, Track, TrackKind
from ..execution.events import StudioEvent
from ..render.compositor import TimelineResolver
from ..render.encoder import TimelineRenderer
from ..render.scope import (
    find_clip,
    renderable_clips,
    timeline_for_clip,
    timeline_for_range,
)
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


#: What a render covers. ``clips`` is a batch — one file per clip — the rest produce a single file.
RenderScope = Literal["timeline", "range", "clip", "clips"]


class RenderRequest(BaseModel):
    name: str | None = None
    #: A single frame instead of a movie. Applies to whichever scope is selected.
    still: bool = False
    time_s: float = 0.0

    scope: RenderScope = "timeline"
    #: For ``range``. Absent bounds mean "from the start" and "to the end".
    start_s: float | None = None
    end_s: float | None = None
    #: For ``clip``.
    clip_id: str | None = None

    # Output overrides, applied on top of the project's render settings for this render only. Left unset,
    # each one keeps whatever the project already uses — this dialog is not a way to edit project settings
    # by accident.
    fps: float | None = None
    width: int | None = None
    height: int | None = None
    container: str | None = None
    video_codec: str | None = None
    crf: int | None = None
    audio_codec: str | None = None
    audio_bitrate: str | None = None


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


@dataclass(slots=True)
class _RenderJob:
    """One output file: the timeline to render and what to call it."""

    timeline: Timeline
    name: str
    #: Where in this job's timeline a still should be taken, already rebased onto it.
    still_at: float = 0.0


def _plan(project, body: RenderRequest, base_name: str) -> list[_RenderJob]:
    """Turn the request into the list of files it asks for.

    Each scope is expressed as a derived timeline, so the encoder only ever sees "render this timeline" —
    exporting one clip and exporting the whole cut are the same code path with different input.
    """
    timeline = project.timeline

    if body.scope == "range":
        start = max(0.0, body.start_s or 0.0)
        end = body.end_s if body.end_s is not None else timeline.duration
        return [
            _RenderJob(
                timeline=timeline_for_range(timeline, start, end),
                name=base_name,
                still_at=max(0.0, body.time_s - start),
            )
        ]

    if body.scope == "clip":
        if not body.clip_id:
            raise ValidationFailed("Select a clip to render, or choose a different scope.")
        found = find_clip(timeline, body.clip_id)
        if found is None:
            raise NotFound(f"No clip {body.clip_id!r} in this timeline")
        _track, clip = found
        return [
            _RenderJob(
                timeline=timeline_for_clip(timeline, body.clip_id),
                name=f"{base_name}-{slugify(clip.name or 'clip')}",
                still_at=max(0.0, body.time_s - clip.start),
            )
        ]

    if body.scope == "clips":
        clips = renderable_clips(timeline)
        if not clips:
            raise ValidationFailed(
                "There is nothing to render one clip at a time — every clip is disabled or on a muted "
                "track."
            )
        # Numbered, so the files sort into timeline order in a file browser.
        return [
            _RenderJob(
                timeline=timeline_for_clip(timeline, clip.id),
                name=f"{base_name}-{index:03d}-{slugify(clip.name or 'clip')}",
            )
            for index, (_track, clip) in enumerate(clips, start=1)
        ]

    return [_RenderJob(timeline=timeline, name=base_name, still_at=body.time_s)]


def _output_settings(state, body: RenderRequest, timeline: Timeline):
    """Encoder settings for this render: the project's, with the request's overrides on top."""
    settings = state.settings.render.model_copy()
    settings.fps = body.fps or timeline.fps or settings.fps
    for field in ("container", "video_codec", "crf", "audio_codec", "audio_bitrate"):
        value = getattr(body, field)
        if value is not None:
            setattr(settings, field, value)
    return settings


@router.post("/render", status_code=202)
async def render_timeline(state: StateDep, project: ProjectDep, body: RenderRequest) -> dict:
    """Render the timeline, or part of it. Runs in a worker thread; progress arrives on the event bus."""
    if project.timeline.duration <= 0 and not body.still:
        raise ValidationFailed("The timeline is empty — add at least one clip before rendering.")

    render_id = new_id("render")
    base_name = slugify(body.name or f"{project.name}-{render_id[-6:]}")
    jobs = _plan(project, body, base_name)

    # Composition size and rate live on the timeline itself, so an override goes on the derived copy —
    # the project's own timeline settings are left exactly as the user set them.
    for job in jobs:
        if body.fps:
            job.timeline.fps = body.fps
        if body.width:
            job.timeline.width = body.width
        if body.height:
            job.timeline.height = body.height

    loop = asyncio.get_running_loop()

    def progress_for(index: int) -> Any:
        def progress(fraction: float, message: str) -> None:
            # Called from the render thread; hop back onto the loop before touching the event bus.
            # Batches report overall progress, so one bar covers the whole export rather than snapping
            # back to zero for every clip.
            overall = (index + fraction) / len(jobs)
            event = StudioEvent(
                type="render.progress",
                project_id=project.id,
                data={
                    "render_id": render_id,
                    "progress": overall,
                    "message": message if len(jobs) == 1 else f"{index + 1}/{len(jobs)} · {message}",
                },
            )
            loop.call_soon_threadsafe(state.events.publish, event)

        return progress

    async def run() -> None:
        outputs: list[dict[str, Any]] = []
        try:
            for index, job in enumerate(jobs):
                renderer = TimelineRenderer(
                    state.store,
                    project,
                    _output_settings(state, body, job.timeline),
                    on_progress=progress_for(index),
                )
                destination = state.store.renders_dir(project.id) / job.name
                # PyAV encoding is CPU-bound and releases the GIL; a thread keeps the API responsive.
                result = await asyncio.to_thread(
                    renderer.render_still if body.still else renderer.render,
                    destination,
                    timeline=job.timeline,
                    **({"time_s": job.still_at} if body.still else {}),
                )
                outputs.append(
                    {
                        "path": state.store.relativize(project.id, result.path),
                        "kind": result.kind,
                        "duration": result.duration,
                        "frames": result.frames,
                        "warnings": result.warnings,
                    }
                )

            state.events.emit(
                "render.finished",
                project_id=project.id,
                data={
                    "render_id": render_id,
                    "ok": True,
                    "scope": body.scope,
                    "outputs": outputs,
                    # The first output is repeated at the top level so a single-file render reads the
                    # same as it always has.
                    **outputs[0],
                },
            )
        except Exception as exc:  # noqa: BLE001 - report, never crash the server
            logger.exception("Render %s failed", render_id)
            state.events.emit(
                "render.finished",
                project_id=project.id,
                data={
                    "render_id": render_id,
                    "ok": False,
                    "error": str(exc),
                    "scope": body.scope,
                    # Whatever did finish before the failure is still on disk and still useful.
                    "outputs": outputs,
                },
            )

    asyncio.create_task(run())
    return {"render_id": render_id, "status": "started", "outputs": len(jobs)}


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
