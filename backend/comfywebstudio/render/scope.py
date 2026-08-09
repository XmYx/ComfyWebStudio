"""What part of the timeline a render covers.

Every scope is expressed as a *derived timeline* rather than as a flag the encoder has to understand.
Rendering an in/out range or a single clip is then the ordinary render path applied to a smaller timeline,
which is why exporting one clip cannot drift from exporting all of them: it is the same code.

Derived timelines are shifted so they start at zero. A clip that begins at 0:42 exports as a file that
begins at its own first frame, not with 42 seconds of black.
"""

from __future__ import annotations

from ..core.models import Clip, Timeline, Track

#: Below this a clip has no frames worth encoding, and a zero-length timeline cannot be rendered at all.
MIN_DURATION_S = 1e-3


def timeline_for_range(timeline: Timeline, start_s: float, end_s: float) -> Timeline:
    """The timeline cropped to ``[start_s, end_s)`` and shifted back to zero.

    Clips straddling either edge are trimmed rather than dropped, and a trimmed leading edge advances the
    clip's ``in_point`` by the same amount so the visible content does not slide.
    """
    if end_s - start_s < MIN_DURATION_S:
        raise ValueError("The render range is empty; drag the in and out points further apart.")

    derived = timeline.model_copy(deep=True)
    for track in derived.tracks:
        track.clips = [
            trimmed
            for trimmed in (_crop(clip, start_s, end_s) for clip in track.clips)
            if trimmed is not None
        ]
    return derived


def timeline_for_clip(timeline: Timeline, clip_id: str) -> Timeline:
    """Just this clip, alone on its own track, starting at zero.

    Other tracks are dropped rather than muted: exporting "this clip" should not silently bake in an
    overlay that happened to sit above it.
    """
    found = find_clip(timeline, clip_id)
    if found is None:
        raise ValueError(f"No clip {clip_id!r} in this timeline.")
    track, clip = found

    only = clip.model_copy(deep=True)
    only.start = 0.0

    derived = timeline.model_copy(deep=True)
    derived.tracks = [track.model_copy(deep=True, update={"clips": [only], "muted": False})]
    return derived


def find_clip(timeline: Timeline, clip_id: str) -> tuple[Track, Clip] | None:
    for track in timeline.tracks:
        for clip in track.clips:
            if clip.id == clip_id:
                return track, clip
    return None


def renderable_clips(timeline: Timeline) -> list[tuple[Track, Clip]]:
    """Every clip worth rendering on its own, in timeline order.

    Muted tracks and disabled clips are skipped: they are the ones the user has already said they do not
    want, and producing a file per skipped clip is just clutter.
    """
    found = [
        (track, clip)
        for track in timeline.tracks
        if not track.muted
        for clip in track.clips
        if clip.enabled and clip.duration >= MIN_DURATION_S
    ]
    return sorted(found, key=lambda pair: (pair[1].start, pair[1].id))


def _crop(clip: Clip, start_s: float, end_s: float) -> Clip | None:
    """One clip cropped to the window, or None when it falls entirely outside it."""
    visible_start = max(clip.start, start_s)
    visible_end = min(clip.end, end_s)
    if visible_end - visible_start < MIN_DURATION_S:
        return None

    cropped = clip.model_copy(deep=True)
    # How much of the clip's head the window cuts off — the source has to skip the same amount.
    lead = visible_start - clip.start
    cropped.in_point = clip.in_point + lead
    if cropped.out_point is not None:
        cropped.out_point = min(cropped.out_point, cropped.in_point + (visible_end - visible_start))
    cropped.start = visible_start - start_s
    cropped.duration = visible_end - visible_start
    return cropped
