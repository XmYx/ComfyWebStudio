"""Which picture the renderer shows at each frame.

The bug this pins down: a clip that starts anywhere other than zero showed its first picture twice and
skipped one further along. `time_s - clip.start` is a difference of two floats, and for a clip at 1.0s the
sample at 1.3333… comes back as 0.33333333333333326 — a hair below a third. Turning that into an index by
truncating a ratio picks the frame before, which reads on screen as a held frame at every cut.
"""

from __future__ import annotations

import math

import pytest
from PIL import Image

from comfywebstudio.core.models import Clip, Timeline, Track
from comfywebstudio.render.compositor import FrameCompositor, ResolvedClip


def sequence(tmp_path, name: str, tag: int, count: int) -> list:
    """`count` images, each identifiable: red says which frame, green says which clip."""
    paths = []
    for index in range(count):
        path = tmp_path / f"{name}{index}.png"
        Image.new("RGB", (8, 8), (index * 10, tag, 0)).save(path)
        paths.append(path)
    return paths


def played(tmp_path, fps: float, count: int) -> tuple[list, list]:
    """Two sequences laid end to end, and what each rendered frame turned out to be."""
    first = Clip(start=0.0, duration=count / fps)
    second = Clip(start=count / fps, duration=count / fps)
    timeline = Timeline(
        fps=fps, width=8, height=8, tracks=[Track(kind="video", clips=[first, second])]
    )
    compositor = FrameCompositor(timeline, resolver=None)  # type: ignore[arg-type]
    compositor._resolved = {
        first.id: ResolvedClip(
            clip=first, kind="image", paths=sequence(tmp_path, "a", 11, count), error=None
        ),
        second.id: ResolvedClip(
            clip=second, kind="image", paths=sequence(tmp_path, "b", 22, count), error=None
        ),
    }

    total = max(1, int(math.ceil(timeline.duration * fps)))
    got = [compositor.frame_at(i / fps).getpixel((4, 4)) for i in range(total)]
    want = [(i * 10, 11, 0) for i in range(count)] + [(i * 10, 22, 0) for i in range(count)]
    return got, want


@pytest.mark.parametrize(("fps", "count"), [(3.0, 3), (24.0, 24), (25.0, 10), (30.0, 15)])
def test_every_frame_shows_its_own_picture(tmp_path, fps, count):
    got, want = played(tmp_path, fps, count)
    assert got == want


def test_the_clip_after_the_first_does_not_hold_its_opening_frame(tmp_path):
    """The failure as it looked: frame 4 of six repeated frame 3 and the second picture never showed."""
    got, _want = played(tmp_path, 3.0, 3)
    second_clip = got[3:]
    assert len({pixel[0] for pixel in second_clip}) == 3, f"a picture was repeated: {second_clip}"


def test_a_single_image_holds_for_the_whole_clip(tmp_path):
    clip = Clip(start=1.0, duration=2.0)
    timeline = Timeline(fps=4.0, width=8, height=8, tracks=[Track(kind="video", clips=[clip])])
    compositor = FrameCompositor(timeline, resolver=None)  # type: ignore[arg-type]
    compositor._resolved = {
        clip.id: ResolvedClip(
            clip=clip, kind="image", paths=sequence(tmp_path, "one", 5, 1), error=None
        )
    }
    frames = [compositor.frame_at(1.0 + i / 4) for i in range(8)]
    assert {f.getpixel((4, 4)) for f in frames} == {(0, 5, 0)}


def test_a_stretched_clip_spreads_its_pictures_over_its_length(tmp_path):
    """Six frames of timeline, three pictures: each one gets two frames, none gets three."""
    clip = Clip(start=2.0, duration=2.0)
    timeline = Timeline(fps=3.0, width=8, height=8, tracks=[Track(kind="video", clips=[clip])])
    compositor = FrameCompositor(timeline, resolver=None)  # type: ignore[arg-type]
    compositor._resolved = {
        clip.id: ResolvedClip(
            clip=clip, kind="image", paths=sequence(tmp_path, "s", 7, 3), error=None
        )
    }
    reds = [compositor.frame_at(2.0 + i / 3).getpixel((4, 4))[0] for i in range(6)]
    assert reds == [0, 0, 10, 10, 20, 20]
