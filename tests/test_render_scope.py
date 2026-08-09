"""Deriving a smaller timeline for a partial render.

Every scope goes through the ordinary render path, so what these pin down is the arithmetic: what survives
a crop, what it is trimmed to, and where the source is read from afterwards.
"""

from __future__ import annotations

import pytest

from comfywebstudio.core.models import Clip, Timeline, Track
from comfywebstudio.render.scope import (
    renderable_clips,
    timeline_for_clip,
    timeline_for_range,
)


def timeline(*spans: tuple[float, float]) -> Timeline:
    """One video track holding a clip per ``(start, duration)``."""
    clips = [
        Clip(name=f"clip{index}", start=start, duration=duration)
        for index, (start, duration) in enumerate(spans)
    ]
    return Timeline(fps=24.0, tracks=[Track(kind="video", name="V1", clips=clips)])


def spans(result: Timeline) -> list[tuple[float, float]]:
    return [(c.start, c.duration) for track in result.tracks for c in track.clips]


# -- ranges --------------------------------------------------------------------------------------------


def test_a_range_keeps_only_what_overlaps_it():
    result = timeline_for_range(timeline((0, 2), (5, 2), (10, 2)), 4.0, 8.0)
    assert spans(result) == [(1.0, 2.0)], "clips outside the window should be dropped"


def test_a_range_shifts_what_survives_back_to_zero():
    result = timeline_for_range(timeline((10, 3)), 10.0, 13.0)
    assert spans(result) == [(0.0, 3.0)]
    assert result.duration == 3.0


def test_a_clip_straddling_the_start_is_trimmed_and_its_source_advanced():
    # The window cuts 1.5s off the clip's head, so the source has to skip the same amount or the visible
    # content would slide backwards.
    result = timeline_for_range(timeline((2, 5)), 3.5, 7.0)
    clip = result.tracks[0].clips[0]
    assert (clip.start, clip.duration) == (0.0, 3.5)
    assert clip.in_point == 1.5


def test_a_clip_straddling_the_end_is_trimmed_without_moving_its_source():
    result = timeline_for_range(timeline((0, 10)), 0.0, 4.0)
    clip = result.tracks[0].clips[0]
    assert (clip.start, clip.duration) == (0.0, 4.0)
    assert clip.in_point == 0.0


def test_an_out_point_is_pulled_in_with_the_crop():
    source = timeline((0, 10))
    source.tracks[0].clips[0].out_point = 9.0
    clip = timeline_for_range(source, 2.0, 5.0).tracks[0].clips[0]

    assert clip.in_point == 2.0
    assert clip.out_point == 5.0, "the out point must not outrun the cropped duration"


def test_an_empty_range_is_refused():
    with pytest.raises(ValueError, match="range is empty"):
        timeline_for_range(timeline((0, 5)), 2.0, 2.0)


def test_the_original_timeline_is_not_touched():
    source = timeline((10, 3))
    timeline_for_range(source, 10.0, 12.0)
    assert spans(source) == [(10.0, 3.0)]


# -- single clips --------------------------------------------------------------------------------------


def test_one_clip_renders_alone_from_zero():
    source = timeline((0, 2), (5, 3))
    target = source.tracks[0].clips[1]
    result = timeline_for_clip(source, target.id)

    assert spans(result) == [(0.0, 3.0)]
    assert result.duration == 3.0


def test_rendering_one_clip_drops_the_other_tracks():
    source = timeline((5, 3))
    source.tracks.append(Track(kind="overlay", name="V2", clips=[Clip(start=5, duration=3)]))
    result = timeline_for_clip(source, source.tracks[0].clips[0].id)

    assert len(result.tracks) == 1, "an overlay above the clip must not be baked in"


def test_an_unknown_clip_is_refused():
    with pytest.raises(ValueError, match="No clip"):
        timeline_for_clip(timeline((0, 1)), "nope")


# -- batches -------------------------------------------------------------------------------------------


def test_clips_are_listed_in_timeline_order():
    source = timeline((8, 1), (2, 1), (5, 1))
    assert [c.start for _t, c in renderable_clips(source)] == [2.0, 5.0, 8.0]


def test_muted_tracks_and_disabled_clips_are_left_out():
    source = timeline((0, 1), (2, 1))
    source.tracks[0].clips[1].enabled = False
    source.tracks.append(Track(kind="video", name="V2", muted=True, clips=[Clip(start=4, duration=1)]))

    assert [c.start for _t, c in renderable_clips(source)] == [0.0]
