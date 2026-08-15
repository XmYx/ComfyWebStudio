"""Frame-exact placement, and what a clip does to what it lands on."""

from __future__ import annotations

import pytest

from comfywebstudio.core.models import Clip, Track
from comfywebstudio.core.timeline_edit import place, quantise

FPS = 24.0


def clip(start: float, duration: float, name: str = "", **kw) -> Clip:
    return Clip(name=name, start=start, duration=duration, **kw)


def spans(track: Track) -> list[tuple[float, float]]:
    return [(round(c.start, 6), round(c.start + c.duration, 6)) for c in track.clips]


# -- the frame grid ------------------------------------------------------------------------------------


def test_a_time_lands_on_a_frame():
    assert quantise(1.0 / 24 * 3 + 0.0001, FPS) == pytest.approx(3 / 24)
    assert quantise(0.9999 / 24, FPS) == pytest.approx(1 / 24)


def test_a_time_never_goes_negative():
    assert quantise(-5.0, FPS) == 0.0


def test_a_timeline_without_a_frame_rate_is_left_alone():
    assert quantise(1.234, 0) == 1.234


def test_two_clips_snapped_together_actually_touch():
    """The whole point: no sliver of a frame between them, at any zoom."""
    track = Track()
    first = clip(0, 1.0)
    place(track, first, FPS)
    # A drag that landed a fraction of a frame short of the first clip's end.
    second = clip(first.start + first.duration - 0.004, 1.0)
    place(track, second, FPS)
    assert second.start == pytest.approx(first.start + first.duration)
    assert spans(track) == [(0.0, 1.0), (1.0, 2.0)]


# -- what a clip does to what it lands on ----------------------------------------------------------------


def test_a_clip_dropped_clear_of_everything_leaves_it_alone():
    track = Track(clips=[clip(0, 1)])
    place(track, clip(2, 1), FPS)
    assert spans(track) == [(0.0, 1.0), (2.0, 3.0)]


def test_a_clip_covered_completely_is_removed():
    buried = clip(1, 0.5, "buried")
    track = Track(clips=[buried])
    removed = place(track, clip(0.5, 2), FPS)
    assert [c.name for c in removed] == ["buried"]
    assert spans(track) == [(0.5, 2.5)]


def test_a_clip_overlapped_at_its_tail_is_trimmed_back():
    track = Track(clips=[clip(0, 2, "under")])
    place(track, clip(1, 2), FPS)
    assert spans(track) == [(0.0, 1.0), (1.0, 3.0)]


def test_a_clip_overlapped_at_its_head_moves_up_and_keeps_its_sync():
    under = clip(1, 2, "under", in_point=5.0)
    track = Track(clips=[under])
    place(track, clip(0, 1.5), FPS)
    assert spans(track) == [(0.0, 1.5), (1.5, 3.0)]
    # Half a second was taken off its head, so it starts half a second later into the media.
    assert under.in_point == pytest.approx(5.5)


def test_a_clip_straddled_in_the_middle_is_split_in_two():
    track = Track(clips=[clip(0, 4, "under", in_point=1.0)])
    place(track, clip(1, 2, "over"), FPS)
    assert spans(track) == [(0.0, 1.0), (1.0, 3.0), (3.0, 4.0)]

    head, _over, tail = track.clips
    assert head.name == "under" and tail.name == "under"
    assert head.id != tail.id, "two halves sharing one id is a timeline nothing can address"
    assert tail.in_point == pytest.approx(4.0), "the tail plays from where it always did"


def test_a_split_half_is_no_longer_tied_to_its_old_partner():
    track = Track(clips=[clip(0, 4, "under", link_id="link_x")])
    place(track, clip(1, 2), FPS)
    tail = track.clips[-1]
    assert tail.link_id is None


def test_a_sliver_left_by_a_trim_is_dropped_rather_than_kept():
    """Less than a frame of a clip is not something anybody put there."""
    # Only a hundredth of a second of the clip underneath survives the overlap — less than one frame.
    track = Track(clips=[clip(0.99, 1.01)])
    removed = place(track, clip(1.0, 1), FPS)
    assert len(removed) == 1
    assert spans(track) == [(1.0, 2.0)]


def test_clips_come_back_in_order():
    track = Track(clips=[clip(4, 1), clip(0, 1)])
    place(track, clip(2, 1), FPS)
    assert spans(track) == [(0.0, 1.0), (2.0, 3.0), (4.0, 5.0)]


def test_touching_end_to_start_is_not_an_overlap():
    track = Track(clips=[clip(0, 1, "first")])
    removed = place(track, clip(1, 1), FPS)
    assert removed == []
    assert spans(track) == [(0.0, 1.0), (1.0, 2.0)]
