"""Putting a clip somewhere, and what that does to whatever was already there.

Two rules the timeline could not previously keep.

**Times live on the frame grid.** A cut is a frame boundary — there is no such thing as half a frame in
the finished file — so every time this module writes is rounded to one. Left in free-floating seconds, a
clip ending at 5.0333 beside one starting at 5.0334 looks flush at every zoom level and still renders a
black frame between them, which is the kind of fault nobody finds until the export.

**A clip that lands on another one takes the space.** That is what every editor means by dropping a clip
somewhere: the picture underneath gets trimmed, split, or removed, and never left invisibly buried under
the new one where it will surprise you at the render.
"""

from __future__ import annotations

from .models import Clip, Track

#: Below this many frames a clip is not worth keeping — a sliver left by a trim, not something anybody
#: put there on purpose.
MIN_FRAMES = 1


def quantise(seconds: float, fps: float) -> float:
    """Round a time onto the frame grid, never below zero."""
    if fps <= 0:
        return max(0.0, float(seconds))
    return max(0.0, round(float(seconds) * fps) / fps)


def frame_length(fps: float) -> float:
    return 1.0 / fps if fps > 0 else 0.04


def align(clip: Clip, fps: float) -> Clip:
    """Put one clip's start and duration on the frame grid, keeping it at least one frame long."""
    clip.start = quantise(clip.start, fps)
    clip.duration = max(frame_length(fps) * MIN_FRAMES, quantise(clip.duration, fps))
    return clip


def overlaps(a: Clip, b: Clip) -> bool:
    """True when the two share any time at all. Touching end-to-start is not an overlap."""
    return a.start < b.start + b.duration and b.start < a.start + a.duration


def place(track: Track, clip: Clip, fps: float) -> list[Clip]:
    """Make room on `track` for `clip`, and return the clips that were dropped entirely.

    `clip` must already be on the track, or about to be appended — either way it is left alone and
    everything it overlaps gives way:

    * covered completely, it goes;
    * overlapped at its tail, it is trimmed back to the newcomer's start;
    * overlapped at its head, its head moves up — and its `in_point` moves with it, so the media keeps
      playing from the same place rather than sliding out of sync;
    * straddled in the middle, it is split in two, because the alternative is silently losing whichever
      half we guessed was less important.
    """
    align(clip, fps)
    minimum = frame_length(fps) * MIN_FRAMES
    end = clip.start + clip.duration

    kept: list[Clip] = []
    removed: list[Clip] = []

    for other in track.clips:
        if other.id == clip.id:
            continue
        if not overlaps(clip, other):
            kept.append(other)
            continue

        other_end = other.start + other.duration

        # Swallowed whole.
        if other.start >= clip.start - 1e-9 and other_end <= end + 1e-9:
            removed.append(other)
            continue

        # Straddled: keep both ends as two clips. The tail is a copy so that the two halves do not share
        # one identity — a timeline with the same clip id twice is a timeline nothing can address.
        if other.start < clip.start and other_end > end:
            tail = other.model_copy(deep=True)
            tail.id = _new_clip_id()
            tail.in_point = other.in_point + (end - other.start)
            tail.start = end
            tail.duration = other_end - end
            # A split half is no longer the same thing its partner was tied to.
            tail.link_id = None
            other.duration = clip.start - other.start
            if other.duration >= minimum:
                kept.append(other)
            else:
                removed.append(other)
            if tail.duration >= minimum:
                kept.append(align(tail, fps))
            continue

        if other.start < clip.start:  # overlapped at its tail
            other.duration = clip.start - other.start
        else:  # overlapped at its head
            shift = end - other.start
            other.in_point += shift
            other.start = end
            other.duration = other_end - end

        if other.duration >= minimum:
            kept.append(align(other, fps))
        else:
            removed.append(other)

    if clip not in kept:
        kept.append(clip)
    track.clips = sorted(kept, key=lambda c: c.start)
    return removed


def _new_clip_id() -> str:
    from .ids import new_id

    return new_id("clip")


def align_track(track: Track, fps: float) -> None:
    """Put every clip on a track onto the frame grid and keep them in order."""
    for clip in track.clips:
        align(clip, fps)
    track.clips.sort(key=lambda c: c.start)
