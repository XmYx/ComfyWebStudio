"""Mixing the timeline's audio: gain, stereo placement, mute and solo.

The mixer is the one place where a track's controls and a clip's own combine, so the rules it applies are
worth pinning down: gains multiply, pans add, solo silences everything else, and panning something hard to
one side must not make it quieter than leaving it in the middle.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from comfywebstudio.core.models import Clip, ClipSource, Timeline, Track
from comfywebstudio.render.encoder import TimelineRenderer, _pan_gains
from comfywebstudio.settings import RenderSettings


class FakeResolved:
    """Stands in for a resolved clip: usable, with one file behind it."""

    def __init__(self, path: str = "sound.wav") -> None:
        self.usable = True
        self.paths = [path]
        self.error = None


class FakeResolver:
    def resolve(self, clip):  # noqa: ARG002 - every clip resolves the same in these tests
        return FakeResolved()


def audio_track(**kwargs) -> Track:
    clip = Clip(source=ClipSource(kind="asset", asset_id="a"), start=0.0, duration=1.0)
    return Track(kind="audio", clips=[clip], **kwargs)


@pytest.fixture
def encoder() -> TimelineRenderer:
    """Only the audio selection is under test, so the store and project are never touched."""
    return TimelineRenderer(store=None, project=None, settings=RenderSettings())


def audible(encoder, timeline) -> list[Track]:
    """Which tracks the mixer would take clips from."""
    return [track for _clip, _resolved, track in encoder._audio_clips(timeline, FakeResolver())]


class TestWhatIsAudible:
    def test_an_ordinary_track_is(self, encoder):
        timeline = Timeline(tracks=[audio_track(name="Music")])
        assert [t.name for t in audible(encoder, timeline)] == ["Music"]

    def test_a_muted_track_is_not(self, encoder):
        timeline = Timeline(tracks=[audio_track(name="Music", muted=True)])
        assert audible(encoder, timeline) == []

    def test_soloing_one_track_silences_the_others(self, encoder):
        timeline = Timeline(
            tracks=[audio_track(name="Music"), audio_track(name="Dialogue", solo=True)]
        )
        assert [t.name for t in audible(encoder, timeline)] == ["Dialogue"]

    def test_solo_beats_mute_on_the_same_track(self, encoder):
        """Muting and soloing the same track is contradictory; solo is the more recent intent."""
        timeline = Timeline(
            tracks=[audio_track(name="Music"), audio_track(name="Solo", solo=True, muted=True)]
        )
        assert [t.name for t in audible(encoder, timeline)] == ["Solo"]

    def test_a_disabled_clip_contributes_nothing(self, encoder):
        track = audio_track(name="Music")
        track.clips[0].enabled = False
        assert audible(encoder, Timeline(tracks=[track])) == []

    def test_video_tracks_are_not_audio_clips(self, encoder):
        timeline = Timeline(tracks=[Track(kind="video", clips=[Clip()])])
        assert audible(encoder, timeline) == []


class TestPanning:
    def test_centre_is_equal_on_both_sides(self):
        left, right = _pan_gains(0.0)
        assert left == pytest.approx(right)

    def test_hard_left_puts_nothing_on_the_right(self):
        left, right = _pan_gains(-1.0)
        assert right == pytest.approx(0.0, abs=1e-6)
        assert left > 1.0

    def test_hard_right_puts_nothing_on_the_left(self):
        left, right = _pan_gains(1.0)
        assert left == pytest.approx(0.0, abs=1e-6)
        assert right > 1.0

    def test_power_is_constant_across_the_sweep(self):
        """The point of the equal-power law: no dip in the middle as a clip moves across."""
        powers = [float(np.sum(_pan_gains(p) ** 2)) for p in (-1.0, -0.5, 0.0, 0.5, 1.0)]
        assert all(p == pytest.approx(powers[0], rel=1e-5) for p in powers)
        assert powers[0] == pytest.approx(2.0, rel=1e-5)

    def test_beyond_the_ends_is_clamped(self):
        assert list(_pan_gains(-4.0)) == pytest.approx(list(_pan_gains(-1.0)))
        assert list(_pan_gains(4.0)) == pytest.approx(list(_pan_gains(1.0)))

    def test_it_is_the_constant_power_law_and_not_a_linear_one(self):
        """Halfway left should be ~-3 dB on the far side, not half the amplitude."""
        left, right = _pan_gains(-0.5)
        assert right / left == pytest.approx(math.tan(math.pi / 8), rel=1e-5)


class TestWaveform:
    """Reducing an audio file to something a clip can be drawn with."""

    @pytest.fixture
    def tone(self, tmp_path):
        """Three seconds: a two-second swell, then silence. Stereo, so de-interleaving is exercised."""
        import av

        path = tmp_path / "tone.wav"
        rate = 44100
        container = av.open(str(path), "w")
        stream = container.add_stream("pcm_s16le", rate=rate)
        stream.layout = "stereo"

        time = np.arange(rate * 3) / rate
        envelope = np.concatenate(
            [np.linspace(0, 1, rate), np.linspace(1, 0, rate), np.zeros(rate)]
        )
        signal = (np.sin(2 * np.pi * 440 * time) * envelope).astype(np.float32)
        interleaved = np.empty(signal.size * 2, dtype=np.float32)
        interleaved[0::2] = signal
        interleaved[1::2] = signal

        frame = av.AudioFrame.from_ndarray(
            (interleaved * 32767).astype(np.int16).reshape(1, -1), format="s16", layout="stereo"
        )
        frame.sample_rate = rate
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
        container.close()
        return path

    def test_it_reports_the_real_duration(self, tone):
        from comfywebstudio.media import waveform

        # A stereo file read without de-interleaving looks twice as long as it is, which is the bug this
        # catches: the samples are counted per channel, not per frame.
        assert waveform.compute(tone, 40).duration == pytest.approx(3.0, abs=0.05)

    def test_it_returns_exactly_the_buckets_asked_for(self, tone):
        from comfywebstudio.media import waveform

        assert len(waveform.compute(tone, 64).peaks) == 64

    def test_the_peaks_follow_the_sound(self, tone):
        from comfywebstudio.media import waveform

        peaks = waveform.compute(tone, 30).peaks
        loudest = max(range(len(peaks)), key=lambda i: peaks[i][1])
        assert 8 <= loudest <= 12, "the swell peaks around a third of the way in"
        assert peaks[-1] == (0.0, 0.0), "and the last second is silent"

    def test_every_bucket_has_a_low_and_a_high(self, tone):
        from comfywebstudio.media import waveform

        assert all(low <= high for low, high in waveform.compute(tone, 40).peaks)

    def test_the_second_read_is_served_from_cache(self, tone):
        from comfywebstudio.media import waveform

        first = waveform.compute(tone, 40)
        assert list(waveform.compute(tone, 40).peaks) == list(first.peaks)
        assert list(tone.parent.glob(".tone.*.peaks.json")), "nothing was cached"

    def test_a_file_with_no_audio_is_refused_clearly(self, tmp_path):
        from comfywebstudio.media import waveform

        silent = tmp_path / "not-audio.txt"
        silent.write_text("hello")
        with pytest.raises(Exception, match="audio|Invalid|invalid"):
            waveform.compute(silent, 40)
