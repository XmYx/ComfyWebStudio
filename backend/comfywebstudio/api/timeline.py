"""The timeline: tracks, clips, and rendering the whole thing."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from ..core.errors import NotFound, ValidationFailed
from ..core.ids import new_id, slugify
from ..core.models import Clip, ClipSource, RenderChoice, Timeline, Track, TrackKind
from ..core.timeline_edit import align, place, quantise
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
    #: Silences every track that is not soloed, for as long as anything is.
    solo: bool | None = None
    locked: bool | None = None
    volume: float | None = None
    #: -1 hard left to +1 hard right.
    pan: float | None = None


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
    #: The preset these settings came from, remembered with them so the dialog reopens on it.
    preset_id: str | None = None

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
    # Clamped here rather than trusted from the client: a negative gain would invert the phase and a pan
    # past the ends would push the equal-power law out of its domain.
    track.volume = max(0.0, min(4.0, track.volume))
    track.pan = max(-1.0, min(1.0, track.pan))
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


def _has_audio(state, project, source: ClipSource) -> bool:
    """Whether the media behind a source carries an audio stream of its own."""
    resolver = TimelineResolver(state.store, project)
    artifacts, error = resolver.artifacts_for(Clip(source=source))
    if error or not artifacts:
        return False
    return bool(artifacts[0].meta.get("has_audio"))


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
        duration=duration,
        text=body.text,
    )
    # Frame-aligned, and it takes the space it lands on rather than burying what was there.
    place(track, clip, project.timeline.fps)

    # Media that carries its own sound gets an audio clip too, tied to the picture. Placing a video and
    # silently leaving its audio behind means finding out at the render.
    if body.source and track.kind in {"video", "overlay"} and _has_audio(state, project, body.source):
        audio_track = _track_for(project.timeline, "audio", "Audio")
        if not audio_track.locked:
            partner = Clip(
                name=f"{body.name or 'clip'} (audio)",
                source=body.source.model_copy(deep=True),
                start=clip.start,
                duration=clip.duration,
                link_id=new_id("link"),
            )
            clip.link_id = partner.link_id
            audio_track.clips = sorted([*audio_track.clips, partner], key=lambda c: c.start)

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

    # Validated rather than copied over: `model_copy(update=...)` skips validation, so patching a nested
    # field — a clip's source, its transform — stored the raw dict the browser sent and left the model
    # holding something that only looks like a ClipSource until the project is next loaded from disk.
    merged = {**clip.model_dump(mode="json"), **{k: v for k, v in body.items() if k in Clip.model_fields}}
    try:
        updated = Clip.model_validate(merged)
    except PydanticValidationError as exc:
        raise ValidationFailed(f"That change to the clip is not valid: {exc.errors()[0]['msg']}") from exc

    # Pointing a clip at a different output is a change of material, so it takes that material's length
    # unless the caller said otherwise. Retiming it by hand every time would be busywork.
    if "source" in body and "duration" not in body and updated.source != clip.source:
        updated.duration = _default_duration(state, project, updated.source, updated.duration)

    # On the frame grid, always. A drag that lands a thousandth of a second short of its neighbour
    # looks flush and renders a black frame; rounding here is what makes "snapped" mean "touching".
    align(updated, project.timeline.fps)
    updated.volume = max(0.0, min(4.0, updated.volume))
    updated.pan = max(-1.0, min(1.0, updated.pan))

    # Tied clips are one thing to the person cutting, so timing changes carry across. Done here rather
    # than in the browser so it holds however the change arrives — a drag, the inspector, or a script.
    moved = updated.start - clip.start
    resized = updated.duration - clip.duration
    if moved or resized:
        for partner_track, partner in _linked(project.timeline, updated):
            if partner_track.locked:
                continue
            partner.start = max(0.0, partner.start + moved)
            partner.duration = max(0.04, partner.duration + resized)
            place(partner_track, partner, project.timeline.fps)

    track.clips = [updated if c.id == clip_id else c for c in track.clips]
    place(track, updated, project.timeline.fps)
    state.store.save(project)
    return updated


@router.delete("/tracks/{track_id}/clips/{clip_id}", status_code=204)
def delete_clip(state: StateDep, project: ProjectDep, track_id: str, clip_id: str) -> None:
    track = project.timeline.track(track_id)
    if track is None:
        raise NotFound(f"No track {track_id!r}")
    clip = track.clip(clip_id)
    if clip is None:
        raise NotFound(f"No clip {clip_id!r}")

    # Deleting the picture and leaving its sound playing over the next shot is never what was meant.
    doomed = {clip.id, *(other.id for _t, other in _linked(project.timeline, clip))}
    for other in project.timeline.tracks:
        other.clips = [c for c in other.clips if c.id not in doomed]
    state.store.save(project)


class TieRequest(BaseModel):
    #: The clip to tie this one to. Both end up in the same group, along with anything either already
    #: carried, so tying A to B when B is already tied to C leaves all three together.
    clip_id: str


@router.post("/clips/{clip_id}/tie")
def tie_clips(state: StateDep, project: ProjectDep, clip_id: str, body: TieRequest) -> Timeline:
    """Make two clips move and trim as one."""
    _track, clip = _find_clip(project.timeline, clip_id)
    _other_track, other = _find_clip(project.timeline, body.clip_id)
    if clip.id == other.id:
        raise ValidationFailed("A clip cannot be tied to itself.")

    group = clip.link_id or other.link_id or new_id("link")
    members = {clip.id, other.id}
    for existing in (clip.link_id, other.link_id):
        if not existing:
            continue
        members |= {
            c.id for track in project.timeline.tracks for c in track.clips if c.link_id == existing
        }
    for track in project.timeline.tracks:
        for candidate in track.clips:
            if candidate.id in members:
                candidate.link_id = group

    state.store.save(project)
    return project.timeline


@router.post("/clips/{clip_id}/untie")
def untie_clip(state: StateDep, project: ProjectDep, clip_id: str) -> Timeline:
    """Break a clip out of its group, leaving the rest tied to each other."""
    _track, clip = _find_clip(project.timeline, clip_id)
    if not clip.link_id:
        return project.timeline

    remaining = [other for _t, other in _linked(project.timeline, clip)]
    clip.link_id = None
    # A group of one is not a group; drop it so the UI does not show a tie to nothing.
    if len(remaining) == 1:
        remaining[0].link_id = None

    state.store.save(project)
    return project.timeline


#: What a clip made from a shot can carry, and which kind of track it belongs on.
_VISUAL_KINDS = {"image", "video"}
_AUDIO_KINDS = {"audio"}


def shot_output(
    project, shot, resolver, kinds: set[str], *, require_output: bool = True
) -> ClipSource | None:
    """The source for a shot's final output of one of `kinds`.

    "Final" means the last step in dependency order — walking backwards is what makes the answer the
    shot's *result* rather than whatever intermediate came first.

    With ``require_output`` off, a port that has not produced anything yet still counts. That is what
    lets a shot be cut in before it has been run: the clip points at the port, shows as pending, and
    fills itself in the moment the shot produces something. A timeline is a plan as much as an assembly.
    """
    from ..core.graph import topological_order

    try:
        order = topological_order(shot)
    except Exception:  # noqa: BLE001 - a broken shot yields nothing rather than breaking the caller
        return None

    # Video before image, whatever order the workflow happens to declare its outputs in. A shot that
    # produced a clip *and* a still frame of it is a clip as far as the timeline is concerned — cutting
    # in the still and leaving the motion behind is never what was wanted, and it is a mistake that only
    # shows up when the render comes out as a slideshow.
    ranked = sorted(kinds, key=lambda kind: 0 if kind == "video" else 1)

    fallback: ClipSource | None = None
    for wanted in ranked:
        for step_id in reversed(order):
            step = shot.step(step_id)
            workflow = project.workflow(step.workflow_id) if step else None
            if step is None or workflow is None or not step.enabled:
                continue
            for port in workflow.outputs:
                if port.kind != wanted:
                    continue
                source = ClipSource(
                    kind="step_output", shot_id=shot.id, step_id=step.id, port_key=port.key
                )
                artifacts, error = resolver.artifacts_for(Clip(source=source))
                if not error and artifacts:
                    return source
                # The last step's declared port, kept in case nothing in the shot has run. Ranked the
                # same way, so an unrun shot is cut in as the video it is going to be.
                if fallback is None:
                    fallback = source

    return None if require_output else fallback


def _track_for(timeline: Timeline, kind: TrackKind, name: str, track_id: str | None = None) -> Track:
    """The track to drop onto: the one asked for, else the first of that kind, else a new one."""
    if track_id:
        track = timeline.track(track_id)
        if track is None:
            raise NotFound(f"No track {track_id!r}")
        return track
    existing = next((t for t in timeline.tracks if t.kind == kind), None)
    if existing is not None:
        return existing
    track = Track(kind=kind, name=name)
    timeline.tracks.append(track)
    return track


def _linked(timeline: Timeline, clip: Clip) -> list[tuple[Track, Clip]]:
    """Every *other* clip tied to this one."""
    if not clip.link_id:
        return []
    return [
        (track, other)
        for track in timeline.tracks
        for other in track.clips
        if other.link_id == clip.link_id and other.id != clip.id
    ]


def _find_clip(timeline: Timeline, clip_id: str) -> tuple[Track, Clip]:
    for track in timeline.tracks:
        clip = track.clip(clip_id)
        if clip is not None:
            return track, clip
    raise NotFound(f"No clip {clip_id!r}")


def _append_at(track: Track) -> float:
    """The end of the last clip, so a dropped clip lands after what is already there."""
    return max((clip.end for clip in track.clips), default=0.0)


class PlaceShotRequest(BaseModel):
    shot_id: str
    #: Where to put it. Omitted, the first video track is used, or one is created.
    track_id: str | None = None
    #: Omitted, it goes after whatever is already on that track.
    start: float | None = None
    #: Also place the shot's audio output, on an audio track.
    with_audio: bool = True
    #: Allow a shot that has not been run yet, as a clip that fills in once it produces something.
    allow_pending: bool = True



class RippleDeleteRequest(BaseModel):
    """A span to cut out of the timeline, closing the gap behind it."""

    start: float
    end: float
    #: One track, or every track when omitted — which is what keeps picture and sound in step.
    track_id: str | None = None


@router.post("/ripple-delete")
def ripple_delete(state: StateDep, project: ProjectDep, body: RippleDeleteRequest) -> Timeline:
    """Remove a span of time and pull everything after it back.

    The difference from deleting a clip: time itself is removed, so the cut closes instead of leaving a
    hole. Clips inside the span go; clips straddling an edge are trimmed to it; clips after it move back
    by the length removed.

    Every track by default. A gap taken out of the picture but not the sound would put the two out of
    sync from that point on, which is rarely what anybody means.
    """
    start = max(0.0, min(body.start, body.end))
    end = max(body.start, body.end)
    span = end - start
    if span <= 0:
        raise ValidationFailed("That is an empty span; there is nothing to remove.")

    tracks = project.timeline.tracks
    if body.track_id is not None:
        track = project.timeline.track(body.track_id)
        if track is None:
            raise NotFound(f"No track {body.track_id!r}")
        tracks = [track]

    removed = 0
    for track in tracks:
        if track.locked:
            continue
        kept: list[Clip] = []
        for clip in track.clips:
            if clip.end <= start:
                kept.append(clip)                      # entirely before the cut
                continue
            if clip.start >= end:
                clip.start -= span                     # entirely after it
                kept.append(clip)
                continue
            if clip.start >= start and clip.end <= end:
                removed += 1                           # entirely inside it
                continue

            # Straddling an edge: keep the part outside the span. A clip covering the whole span loses
            # the middle — the head is kept and shortened, which is what trimming to the cut means.
            head = max(0.0, start - clip.start)
            tail = max(0.0, clip.end - end)
            if head > 0:
                clip.duration = head
                kept.append(clip)
            elif tail > 0:
                # Its head was inside the span, so it starts at the cut and loses what came before.
                clip.in_point += end - clip.start
                clip.start = start
                clip.duration = tail
                kept.append(clip)
            else:
                removed += 1
        track.clips = sorted(kept, key=lambda c: c.start)

    state.store.save(project)
    logger.info(
        "Rippled %.2fs out of %s (%d clip(s) removed)",
        span, "one track" if body.track_id else "every track", removed,
    )
    return project.timeline


@router.post("/from-shot", status_code=201)
def place_shot(state: StateDep, project: ProjectDep, body: PlaceShotRequest) -> Timeline:
    """Put one shot's output on the timeline — what dropping a shot onto it does.

    Its audio comes too, on an audio track, because a shot that produced sound and picture is one thing
    to the user and separating them by hand every time would be busywork.
    """
    shot = next((s for s in project.shots if s.id == body.shot_id), None)
    if shot is None:
        raise NotFound(f"No shot {body.shot_id!r}")
    if shot.template_edit_id:
        raise ValidationFailed("That is a template editing session, not a shot.")

    resolver = TimelineResolver(state.store, project)
    visual = shot_output(
        project, shot, resolver, _VISUAL_KINDS, require_output=not body.allow_pending
    )
    audio = (
        shot_output(project, shot, resolver, _AUDIO_KINDS, require_output=True)
        if body.with_audio
        else None
    )
    if visual is None and audio is None:
        raise ValidationFailed(
            f"{shot.name!r} has no image, video or audio output to place. "
            "Add an output node to one of its workflows."
        )

    placed: list[Clip] = []
    link_id = new_id("link")

    if visual is not None:
        track = _track_for(project.timeline, "video", "Shots", body.track_id)
        if track.locked:
            raise ValidationFailed(f"Track {track.name!r} is locked.")
        start = _append_at(track) if body.start is None else max(0.0, body.start)
        duration = _default_duration(state, project, visual, 3.0)
        clip = Clip(name=shot.name, source=visual, start=start, duration=duration)
        place(track, clip, project.timeline.fps)
        placed.append(clip)

    # A video usually carries its own sound. Placing the picture and leaving the audio behind means
    # finding out at the render, so the same media goes on an audio track too — tied to the picture, so
    # the two stay together until somebody says otherwise.
    embedded = visual if visual is not None and _has_audio(state, project, visual) else None
    audio_source = audio or embedded

    if audio_source is not None:
        track = _track_for(project.timeline, "audio", "Audio")
        # Lined up with the picture when there is one, so sound and image stay in sync.
        start = placed[0].start if placed else (
            _append_at(track) if body.start is None else max(0.0, body.start)
        )
        duration = _default_duration(state, project, audio_source, placed[0].duration if placed else 3.0)
        # Exactly as long as the picture when it came from the same media: a sound track a frame longer
        # than the shot it belongs to is a click at every cut.
        if placed and audio_source == embedded:
            duration = placed[0].duration
        clip = Clip(name=f"{shot.name} (audio)", source=audio_source, start=start, duration=duration)
        place(track, clip, project.timeline.fps)
        placed.append(clip)

    if len(placed) > 1:
        for clip in placed:
            clip.link_id = link_id

    state.store.save(project)
    state.events.emit(
        "timeline.built",
        project_id=project.id,
        data={"shot": shot.name, "clips": len(placed)},
    )
    return project.timeline


@router.post("/from-shots", status_code=201)
def build_from_shots(
    state: StateDep, project: ProjectDep, shot_ids: list[str] | None = None
) -> Timeline:
    """Lay every shot's final output end to end — the one-click way to get from shots to a cut.

    A shot's "final output" is the last step in dependency order that produced something.
    """
    # An open template editing session is not a shot the user cut; it must not land in the timeline.
    shots = [
        s for s in project.shots
        if not s.template_edit_id and (shot_ids is None or s.id in shot_ids)
    ]
    if not shots:
        raise ValidationFailed("No shots to build from.")

    # The empty video track a new timeline comes with, when that is what is there — otherwise assembling
    # a cut on a fresh project leaves a blank lane above it for ever. An occupied one is left alone: a
    # build must never quietly overwrite a cut somebody made.
    existing = next((t for t in project.timeline.tracks if t.kind == "video" and not t.clips), None)
    track = existing if existing is not None and not existing.locked else Track(
        kind="video", name="Shots"
    )
    audio_track = _track_for(project.timeline, "audio", "Audio")
    resolver = TimelineResolver(state.store, project)
    cursor = 0.0
    skipped: list[str] = []

    for shot in shots:
        source = shot_output(project, shot, resolver, _VISUAL_KINDS)
        if source is None:
            skipped.append(shot.name)
            continue
        duration = quantise(_default_duration(state, project, source, 3.0), project.timeline.fps)
        clip = Clip(name=shot.name, source=source, start=cursor, duration=duration)
        track.clips.append(clip)

        # The sound that came with the picture, laid alongside it and tied to it. Assembling a cut and
        # discovering at the render that it is silent is the failure this exists to prevent.
        sound = shot_output(project, shot, resolver, _AUDIO_KINDS, require_output=True)
        if sound is None and _has_audio(state, project, source):
            sound = source
        if sound is not None and not audio_track.locked:
            clip.link_id = new_id("link")
            audio_track.clips.append(
                Clip(
                    name=f"{shot.name} (audio)",
                    source=sound.model_copy(deep=True),
                    start=cursor,
                    duration=duration,
                    link_id=clip.link_id,
                )
            )
        cursor += duration

    if not track.clips:
        raise ValidationFailed(
            "None of the selected shots have produced image or video output yet. Run them first."
        )

    audio_track.clips.sort(key=lambda c: c.start)
    if track not in project.timeline.tracks:
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

    # Remembered before anything is encoded, so a render that fails still leaves the settings that were
    # chosen for it — having to retype them because the first attempt did not work is its own annoyance.
    project.settings.render = RenderChoice(
        width=body.width, height=body.height, fps=body.fps,
        container=body.container, video_codec=body.video_codec, crf=body.crf,
        preset_id=body.preset_id,
    )
    state.store.save(project)

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
