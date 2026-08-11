"""Cutting a span of time out of the timeline.

The difference from deleting a clip is that *time* goes with it: the cut closes rather than leaving a
hole. That makes the edge cases the interesting part — a clip that straddles the start of the span keeps
its head, one that straddles the end keeps its tail and slides back, and one covering the whole span
loses its middle.
"""

from __future__ import annotations

import pytest

from comfywebstudio.core.models import Clip, Project, Timeline, Track


def timeline_with(*spans: tuple[float, float], kind: str = "video") -> Timeline:
    clips = [Clip(name=f"c{i}", start=start, duration=length)
             for i, (start, length) in enumerate(spans)]
    return Timeline(tracks=[Track(kind=kind, name="T", clips=clips)])


def ripple(timeline: Timeline, start: float, end: float, track_id: str | None = None):
    """Call the endpoint's logic directly — it is pure, and a whole project is not the subject here."""
    from comfywebstudio.api.timeline import RippleDeleteRequest, ripple_delete

    class Store:
        def save(self, _project):  # noqa: D102 - the persistence is not what is under test
            pass

    class State:
        store = Store()

    project = Project(name="P")
    project.timeline = timeline
    ripple_delete(State(), project, RippleDeleteRequest(start=start, end=end, track_id=track_id))
    return [(c.name, c.start, c.duration) for c in timeline.tracks[0].clips]


def test_a_clip_entirely_inside_the_span_is_removed():
    assert ripple(timeline_with((0, 2), (3, 2)), 2.5, 6.0) == [("c0", 0, 2)]


def test_a_clip_after_the_span_moves_back_by_its_length():
    result = ripple(timeline_with((0, 2), (10, 2)), 4.0, 6.0)
    assert result == [("c0", 0, 2), ("c1", 8, 2)]


def test_a_clip_before_the_span_is_untouched():
    assert ripple(timeline_with((0, 2)), 5.0, 6.0) == [("c0", 0, 2)]


def test_a_clip_straddling_the_start_keeps_its_head():
    assert ripple(timeline_with((0, 5)), 3.0, 4.0) == [("c0", 0, 3)]


def test_a_clip_straddling_the_end_keeps_its_tail_and_slides_back():
    result = ripple(timeline_with((2, 5)), 1.0, 4.0)
    # It started at 2, the cut ran 1–4, so 2 seconds of it survive and now begin at the cut.
    assert result == [("c0", 1.0, 3.0)]


def test_the_surviving_tail_starts_later_in_its_source():
    """Trimming from the front has to advance the in point, or the wrong part of the media plays."""
    timeline = timeline_with((2, 5))
    ripple(timeline, 1.0, 4.0)
    assert timeline.tracks[0].clips[0].in_point == pytest.approx(2.0)


def test_removing_a_span_closes_the_gap_it_leaves():
    result = ripple(timeline_with((0, 2), (2, 2), (4, 2)), 2.0, 4.0)
    assert result == [("c0", 0, 2), ("c2", 2, 2)]


def test_an_empty_span_is_refused():
    from comfywebstudio.core.errors import ValidationFailed

    with pytest.raises(ValidationFailed):
        ripple(timeline_with((0, 2)), 3.0, 3.0)


def test_a_locked_track_is_left_alone():
    timeline = timeline_with((0, 2), (4, 2))
    timeline.tracks[0].locked = True
    assert ripple(timeline, 1.0, 3.0) == [("c0", 0, 2), ("c1", 4, 2)]


def test_every_track_is_rippled_together_by_default():
    """Taking time out of the picture but not the sound would put them out of step from there on."""
    from comfywebstudio.api.timeline import RippleDeleteRequest, ripple_delete

    class State:
        store = type("S", (), {"save": lambda self, p: None})()

    project = Project(name="P")
    project.timeline = Timeline(tracks=[
        Track(kind="video", name="V", clips=[Clip(name="v", start=6, duration=2)]),
        Track(kind="audio", name="A", clips=[Clip(name="a", start=6, duration=2)]),
    ])
    ripple_delete(State(), project, RippleDeleteRequest(start=1.0, end=3.0))

    assert [t.clips[0].start for t in project.timeline.tracks] == [4.0, 4.0]
