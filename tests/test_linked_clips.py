"""Video and the sound that came with it, behaving as one thing.

A shot that produced a video with an audio stream is one thing to the person cutting it, so its picture
and sound are placed together and stay together — moved, trimmed and deleted as a pair — until somebody
unties them. These pin down that behaviour, and that untying really does set them free.
"""

from __future__ import annotations

import pytest

from comfywebstudio.api.timeline import (
    RippleDeleteRequest,
    TieRequest,
    delete_clip,
    ripple_delete,
    tie_clips,
    untie_clip,
    update_clip,
)
from comfywebstudio.core.models import Clip, Project, Timeline, Track


class State:
    """The endpoints only ever save; nothing here needs a real store."""

    store = type("S", (), {"save": lambda self, project: None})()


@pytest.fixture
def project() -> Project:
    """A tied pair: picture on a video track, its sound on an audio track, both 0–4s."""
    project = Project(name="P")
    project.timeline = Timeline(tracks=[
        Track(id="v", kind="video", name="V",
              clips=[Clip(id="pic", name="pic", start=0, duration=4, link_id="L")]),
        Track(id="a", kind="audio", name="A",
              clips=[Clip(id="snd", name="snd", start=0, duration=4, link_id="L")]),
    ])
    return project


def clips(project: Project) -> dict[str, Clip]:
    return {c.id: c for track in project.timeline.tracks for c in track.clips}


def test_moving_the_picture_moves_its_sound(project):
    update_clip(State(), project, "v", "pic", {"start": 6.0})
    assert clips(project)["snd"].start == pytest.approx(6.0)


def test_moving_the_sound_moves_the_picture(project):
    """The tie is symmetric — neither one is the master."""
    update_clip(State(), project, "a", "snd", {"start": 2.5})
    assert clips(project)["pic"].start == pytest.approx(2.5)


def test_trimming_one_trims_the_other(project):
    update_clip(State(), project, "v", "pic", {"duration": 2.0})
    assert clips(project)["snd"].duration == pytest.approx(2.0)


def test_a_change_that_is_not_timing_stays_where_it_was_made(project):
    """Renaming the picture must not rename the sound; only timing is shared."""
    update_clip(State(), project, "v", "pic", {"name": "hero"})
    assert clips(project)["snd"].name == "snd"


def test_deleting_one_deletes_the_other(project):
    delete_clip(State(), project, "v", "pic")
    assert clips(project) == {}


def test_untying_leaves_both_in_place(project):
    untie_clip(State(), project, "pic")
    remaining = clips(project)
    assert set(remaining) == {"pic", "snd"}
    assert remaining["pic"].link_id is None
    assert remaining["snd"].link_id is None, "a group of one is not a group"


def test_untied_clips_move_independently(project):
    untie_clip(State(), project, "pic")
    update_clip(State(), project, "v", "pic", {"start": 9.0})
    assert clips(project)["snd"].start == pytest.approx(0.0)


def test_untied_clips_delete_independently(project):
    untie_clip(State(), project, "pic")
    delete_clip(State(), project, "v", "pic")
    assert set(clips(project)) == {"snd"}


def test_two_loose_clips_can_be_tied(project):
    untie_clip(State(), project, "pic")
    tie_clips(State(), project, "pic", TieRequest(clip_id="snd"))

    update_clip(State(), project, "v", "pic", {"start": 3.0})
    assert clips(project)["snd"].start == pytest.approx(3.0)


def test_tying_to_a_group_joins_the_whole_group(project):
    """Tying A to B when B is already tied to C leaves all three together."""
    project.timeline.tracks.append(
        Track(id="a2", kind="audio", name="A2", clips=[Clip(id="extra", start=0, duration=4)])
    )
    tie_clips(State(), project, "extra", TieRequest(clip_id="pic"))

    update_clip(State(), project, "v", "pic", {"start": 5.0})
    moved = clips(project)
    assert moved["snd"].start == pytest.approx(5.0)
    assert moved["extra"].start == pytest.approx(5.0)


def test_a_clip_cannot_be_tied_to_itself(project):
    from comfywebstudio.core.errors import ValidationFailed

    with pytest.raises(ValidationFailed):
        tie_clips(State(), project, "pic", TieRequest(clip_id="pic"))


def test_a_locked_track_does_not_drag_its_partner_along(project):
    project.timeline.tracks[1].locked = True
    update_clip(State(), project, "v", "pic", {"start": 7.0})
    assert clips(project)["snd"].start == pytest.approx(0.0)


def test_rippling_moves_a_tied_pair_together(project):
    """Ripple works on every track already, which is what keeps a tied pair in step."""
    for track in project.timeline.tracks:
        track.clips[0].start = 6.0
    ripple_delete(State(), project, RippleDeleteRequest(start=1.0, end=3.0))

    moved = clips(project)
    assert moved["pic"].start == pytest.approx(4.0)
    assert moved["snd"].start == pytest.approx(4.0)
