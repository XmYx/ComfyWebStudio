"""What the mixed audio actually sounds like in the rendered file.

The bug these pin down: the mix is ``(samples, 2)`` — interleaved, which is what the ``flt`` format means
— but it was transposed and flattened before being handed over, producing a *planar* buffer declared as
interleaved. The encoder read every other sample as the other channel, so each channel came out at twice
the pitch and the two played one after the other. Every clip's sound, heard twice.
"""

from __future__ import annotations

import math
import struct
from pathlib import Path

import numpy as np
import pytest

from comfywebstudio.core.models import Clip, Timeline, Track
from comfywebstudio.render.compositor import FrameCompositor, ResolvedClip
from comfywebstudio.render.encoder import TimelineRenderer
from comfywebstudio.settings import RenderSettings

av = pytest.importorskip("av")
Image = pytest.importorskip("PIL.Image")

RATE = 48000
FPS = 24.0


def tone(path: Path, freq: float, seconds: float = 1.0) -> Path:
    """A stereo WAV of one pure frequency, written by hand so the test needs no encoder."""
    count = int(RATE * seconds)
    body = bytearray()
    for i in range(count):
        value = int(32767 * 0.6 * math.sin(2 * math.pi * freq * i / RATE))
        body += struct.pack("<hh", value, value)
    data = bytes(body)
    header = (
        b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 2, RATE, RATE * 4, 4, 16)
        + b"data" + struct.pack("<I", len(data))
    )
    path.write_bytes(header + data)
    return path


class _Resolver:
    def __init__(self, resolved):
        self._resolved = resolved

    def resolve(self, clip):
        return self._resolved[clip.id]


def render_two_tones(tmp_path) -> tuple[Path, Timeline, np.ndarray]:
    """A two-second cut: 440 Hz over the first clip, 880 Hz over the second."""
    picture = tmp_path / "f.png"
    Image.new("RGB", (32, 32), (20, 20, 20)).save(picture)
    low, high = tone(tmp_path / "a.wav", 440.0), tone(tmp_path / "b.wav", 880.0)

    v1, v2 = Clip(start=0.0, duration=1.0), Clip(start=1.0, duration=1.0)
    a1, a2 = Clip(start=0.0, duration=1.0), Clip(start=1.0, duration=1.0)
    timeline = Timeline(
        fps=FPS, width=32, height=32,
        tracks=[Track(kind="video", clips=[v1, v2]), Track(kind="audio", clips=[a1, a2])],
    )
    resolved = {
        v1.id: ResolvedClip(clip=v1, kind="image", paths=[picture], error=None),
        v2.id: ResolvedClip(clip=v2, kind="image", paths=[picture], error=None),
        a1.id: ResolvedClip(clip=a1, kind="audio", paths=[low], error=None),
        a2.id: ResolvedClip(clip=a2, kind="audio", paths=[high], error=None),
    }
    resolver = _Resolver(resolved)

    renderer = TimelineRenderer.__new__(TimelineRenderer)
    renderer.store = None
    renderer.project = None
    renderer.settings = RenderSettings(fps=FPS, width=32, height=32, audio_sample_rate=RATE)
    renderer.on_progress = lambda *_a: None

    compositor = FrameCompositor(timeline, resolver)  # type: ignore[arg-type]
    compositor._resolved = resolved
    clips = renderer._audio_clips(timeline, resolver)  # type: ignore[arg-type]
    result = renderer._render_video(
        tmp_path / "out", timeline, compositor, clips, timeline.duration, []
    )

    mix = renderer._mix_audio(clips, result.frames / FPS, RATE)
    return result.path, timeline, mix


def decode(path: Path) -> np.ndarray:
    resampler = av.audio.resampler.AudioResampler(format="fltp", layout="mono", rate=RATE)
    chunks = []
    with av.open(str(path)) as container:
        stream = next(s for s in container.streams if s.type == "audio")
        for frame in container.decode(stream):
            for resampled in resampler.resample(frame):
                chunks.append(resampled.to_ndarray().reshape(-1))
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)


def dominant(samples: np.ndarray) -> float:
    window = samples * np.hanning(len(samples))
    return float(np.fft.rfftfreq(len(window), 1 / RATE)[np.argmax(np.abs(np.fft.rfft(window)))])


def test_each_clip_keeps_its_own_sound_at_its_own_pitch(tmp_path):
    """Before the fix every window came out an octave high, and each clip was heard twice."""
    path, _timeline, _mix = render_two_tones(tmp_path)
    audio = decode(path)

    windows = [dominant(audio[int(s * RATE) : int(s * RATE) + 20000]) for s in (0.1, 0.6, 1.1, 1.6)]
    assert windows[0] == pytest.approx(440, abs=15), windows
    assert windows[1] == pytest.approx(440, abs=15), "the first clip is not cut short or repeated"
    assert windows[2] == pytest.approx(880, abs=15), "the second clip brought its own sound"
    assert windows[3] == pytest.approx(880, abs=15), windows


def test_the_mix_is_exactly_as_long_as_the_picture(tmp_path):
    """A mix that disagrees with the video about where the end is leaves players guessing."""
    _path, timeline, mix = render_two_tones(tmp_path)
    frames = int(math.ceil(timeline.duration * FPS))
    assert len(mix) == int(frames / FPS * RATE)


def test_the_sound_starts_where_the_clip_does(tmp_path):
    path, _timeline, _mix = render_two_tones(tmp_path)
    audio = decode(path)
    loud = np.flatnonzero(np.abs(audio) > 0.01)
    assert loud.size, "the render came out silent"
    # Within a millisecond of the top: an offset here would put every cut out of sync.
    assert loud[0] < RATE / 1000


def test_a_clip_is_placed_at_its_own_offset_not_at_the_start(tmp_path):
    """Only the second half should carry the second clip's tone."""
    path, _timeline, _mix = render_two_tones(tmp_path)
    audio = decode(path)
    assert dominant(audio[: 20000]) == pytest.approx(440, abs=15)
    assert dominant(audio[int(1.2 * RATE) : int(1.2 * RATE) + 20000]) == pytest.approx(880, abs=15)


def test_a_clip_is_as_many_frames_long_as_the_file_has(tmp_path):
    """The length a freshly placed clip takes, measured against what was actually written.

    The container's own duration is a presentation length — it carries the edit list, the encoder's
    priming and a microsecond rounding — so taking it at face value gave a clip of N frames a length of
    N+1, and the last one had nothing behind it.
    """
    from comfywebstudio.media.probe import probe

    picture = tmp_path / "f.png"
    Image.new("RGB", (32, 32), (30, 30, 30)).save(picture)

    for count in (24, 48, 49, 73):
        clip = Clip(start=0.0, duration=count / FPS)
        timeline = Timeline(
            fps=FPS, width=32, height=32, tracks=[Track(kind="video", clips=[clip])]
        )
        resolved = {clip.id: ResolvedClip(clip=clip, kind="image", paths=[picture], error=None)}
        renderer = TimelineRenderer.__new__(TimelineRenderer)
        renderer.store = None
        renderer.project = None
        renderer.settings = RenderSettings(fps=FPS, width=32, height=32)
        renderer.on_progress = lambda *_a: None
        compositor = FrameCompositor(timeline, _Resolver(resolved))  # type: ignore[arg-type]
        compositor._resolved = resolved

        result = renderer._render_video(
            tmp_path / f"v{count}", timeline, compositor, [], count / FPS, []
        )
        meta = probe(result.path)
        assert meta["frames"] == count, f"{count}: wrote {result.frames}, file says {meta['frames']}"
        assert meta["duration"] == pytest.approx(count / FPS, abs=1e-6), (
            f"{count} frames should be {count / FPS}s, not {meta['duration']}"
        )
        # And it lands on that many frames exactly, with nothing left over to round into another one.
        assert round(meta["duration"] * FPS) == count
        assert abs(meta["duration"] * FPS - count) < 1e-9
