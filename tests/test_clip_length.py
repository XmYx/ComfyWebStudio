"""How long a clip is when it is first placed.

Counted in frames of the timeline, never converted from the media's seconds. A video's own length rarely
lands on the timeline's grid — 49 frames at 16 fps is 3.0625s, which is 73.5 frames at 24 — and a clip
handed those seconds claims the whole of that last half frame. Nothing plays there, so it holds the
previous picture: the overshoot at the end of every placed clip.
"""

from __future__ import annotations

import math

import pytest

from comfywebstudio.api.timeline import _default_duration
from comfywebstudio.core.models import Artifact, ClipSource, Project, Timeline


class _Resolver:
    def __init__(self, artifacts):
        self._artifacts = artifacts

    def artifacts_for(self, _clip):
        return self._artifacts, None


def placed(monkeypatch, *, fps: float, meta: dict, count: int = 1) -> float:
    """What a clip of this media would be given on a timeline running at `fps`."""
    from comfywebstudio.api import timeline as module

    project = Project(name="p")
    project.timeline = Timeline(fps=fps)
    artifacts = [
        Artifact(port_key="v", kind="video", path=f"a{i}.mp4", meta=meta if i == 0 else {})
        for i in range(count)
    ]
    monkeypatch.setattr(module, "TimelineResolver", lambda *_a, **_k: _Resolver(artifacts))

    class _State:
        store = None

    return _default_duration(_State(), project, ClipSource(kind='step_output'), 3.0)


@pytest.mark.parametrize(
    ("media_fps", "frames", "timeline_fps"),
    [
        (16.0, 49, 24.0),   # the awkward one: 3.0625s is 73.5 frames at 24
        (16.0, 81, 24.0),
        (30.0, 90, 24.0),
        (24.0, 48, 24.0),   # a clean fit must not lose a frame to flooring
        (25.0, 50, 30.0),
        (12.0, 7, 24.0),
    ],
)
def test_a_clip_is_a_whole_number_of_timeline_frames(monkeypatch, media_fps, frames, timeline_fps):
    duration = placed(monkeypatch, fps=timeline_fps, meta={"frames": frames, "fps": media_fps})
    on_grid = duration * timeline_fps
    assert on_grid == pytest.approx(round(on_grid), abs=1e-9), f"{duration}s is not whole frames"


@pytest.mark.parametrize(
    ("media_fps", "frames", "timeline_fps"),
    [(16.0, 49, 24.0), (16.0, 81, 24.0), (30.0, 90, 24.0), (12.0, 7, 24.0)],
)
def test_it_never_claims_more_than_the_media_can_fill(monkeypatch, media_fps, frames, timeline_fps):
    """Coming up a fraction short is invisible. Going over holds the last picture."""
    duration = placed(monkeypatch, fps=timeline_fps, meta={"frames": frames, "fps": media_fps})
    assert duration <= frames / media_fps + 1e-9, (
        f"{duration}s is longer than the {frames / media_fps}s the file has"
    )
    # And it is the *most* that fits, not merely something short.
    assert round(duration * timeline_fps) == math.floor(frames / media_fps * timeline_fps + 1e-6)


def test_an_exact_fit_keeps_every_frame(monkeypatch):
    assert placed(monkeypatch, fps=24.0, meta={"frames": 48, "fps": 24.0}) == pytest.approx(2.0)
    assert placed(monkeypatch, fps=24.0, meta={"frames": 72, "fps": 24.0}) == pytest.approx(3.0)


def test_an_image_sequence_is_one_picture_per_frame(monkeypatch):
    assert placed(monkeypatch, fps=24.0, meta={}, count=12) == pytest.approx(12 / 24)


def test_media_that_only_reports_seconds_still_lands_on_the_grid(monkeypatch):
    duration = placed(monkeypatch, fps=24.0, meta={"duration": 3.0625})
    assert duration * 24 == pytest.approx(73)


def test_something_shorter_than_a_frame_still_gets_one(monkeypatch):
    assert placed(monkeypatch, fps=24.0, meta={"frames": 1, "fps": 240.0}) == pytest.approx(1 / 24)


def test_media_that_says_nothing_useful_falls_back(monkeypatch):
    assert placed(monkeypatch, fps=24.0, meta={}) == pytest.approx(3.0)
