"""Encoding the timeline to a file, and mixing its audio.

Uses PyAV, which bundles its own FFmpeg libraries — no system ffmpeg is required, which matters because
this machine does not have one.

The renderer also handles the degenerate but perfectly reasonable cases: a timeline of a single still
becomes a PNG, and a timeline with only audio becomes an audio file. That is up to what the sub-workflows
produced, which is the point.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np

from ..core.models import Project, Timeline
from ..core.store import ProjectStore
from ..settings import RenderSettings
from .compositor import FrameCompositor, TimelineResolver

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[float, str], None]


@dataclass(slots=True)
class RenderResult:
    path: Path
    kind: str
    duration: float
    frames: int
    warnings: list[str] = field(default_factory=list)


class TimelineRenderer:
    def __init__(
        self,
        store: ProjectStore,
        project: Project,
        settings: RenderSettings,
        *,
        on_progress: ProgressCallback | None = None,
    ):
        self.store = store
        self.project = project
        self.settings = settings
        self.on_progress = on_progress or (lambda _f, _m: None)

    def render(self, destination: Path, *, timeline: Timeline | None = None) -> RenderResult:
        timeline = timeline or self.project.timeline
        duration = timeline.duration
        if duration <= 0:
            raise ValueError("The timeline is empty — add at least one clip before rendering.")

        resolver = TimelineResolver(self.store, self.project)
        compositor = FrameCompositor(timeline, resolver)
        warnings = compositor.prepare()

        audio_clips = self._audio_clips(timeline, resolver)
        has_video = any(
            clip.enabled
            for track in timeline.tracks
            if track.kind != "audio" and not track.muted
            for clip in track.clips
        )

        try:
            if not has_video and audio_clips:
                return self._render_audio_only(destination, timeline, audio_clips, warnings)

            return self._render_video(destination, timeline, compositor, audio_clips, duration, warnings)
        finally:
            compositor.close()

    # -- variants ----------------------------------------------------------------------------------

    def render_still(self, destination: Path, *, time_s: float = 0.0, timeline=None) -> RenderResult:
        """Render one frame to an image — used for the timeline poster and for still-only projects."""
        timeline = timeline or self.project.timeline
        resolver = TimelineResolver(self.store, self.project)
        compositor = FrameCompositor(timeline, resolver)
        warnings = compositor.prepare()
        try:
            frame = compositor.frame_at(time_s)
            destination = destination.with_suffix(".png")
            destination.parent.mkdir(parents=True, exist_ok=True)
            frame.save(destination, format="PNG")
            return RenderResult(
                path=destination, kind="image", duration=0.0, frames=1, warnings=warnings
            )
        finally:
            compositor.close()

    def _render_audio_only(self, destination, timeline, audio_clips, warnings) -> RenderResult:
        import av

        destination = destination.with_suffix(".flac")
        destination.parent.mkdir(parents=True, exist_ok=True)
        rate = self.settings.audio_sample_rate
        mixed = self._mix_audio(audio_clips, timeline.duration, rate)

        with av.open(str(destination), mode="w") as container:
            stream = container.add_stream("flac", rate=rate, layout="stereo")
            frame = av.AudioFrame.from_ndarray(
                mixed.T.reshape(1, -1).astype(np.float32), format="flt", layout="stereo"
            )
            frame.sample_rate = rate
            frame.pts = 0
            container.mux(stream.encode(frame))
            container.mux(stream.encode(None))

        return RenderResult(
            path=destination, kind="audio", duration=timeline.duration, frames=0, warnings=warnings
        )

    def _render_video(
        self, destination, timeline, compositor, audio_clips, duration, warnings
    ) -> RenderResult:
        import av

        fps = timeline.fps or self.settings.fps
        total_frames = max(1, int(math.ceil(duration * fps)))

        destination = destination.with_suffix(f".{self.settings.container}")
        destination.parent.mkdir(parents=True, exist_ok=True)

        with av.open(str(destination), mode="w") as container:
            video = container.add_stream(self.settings.video_codec, rate=Fraction(fps).limit_denominator())
            video.width = timeline.width
            video.height = timeline.height
            video.pix_fmt = self.settings.pix_fmt
            # CRF is codec-specific; a codec that does not know it would otherwise abort the render.
            try:
                video.options = {"crf": str(self.settings.crf)}
            except Exception:  # noqa: BLE001
                logger.debug("Codec %s did not accept a CRF option", self.settings.video_codec)

            audio_stream = None
            if audio_clips:
                audio_stream = container.add_stream(
                    self.settings.audio_codec,
                    rate=self.settings.audio_sample_rate,
                    layout="stereo",
                )
                audio_stream.bit_rate = _parse_bitrate(self.settings.audio_bitrate)

            for index in range(total_frames):
                image = compositor.frame_at(index / fps)
                frame = av.VideoFrame.from_image(image)
                frame.pts = index
                container.mux(video.encode(frame))

                if index % max(1, total_frames // 100) == 0:
                    self.on_progress(index / total_frames, f"Frame {index + 1} of {total_frames}")

            container.mux(video.encode(None))

            if audio_stream is not None:
                self.on_progress(0.98, "Mixing audio")
                mixed = self._mix_audio(audio_clips, duration, self.settings.audio_sample_rate)
                frame = av.AudioFrame.from_ndarray(
                    mixed.T.reshape(1, -1).astype(np.float32), format="flt", layout="stereo"
                )
                frame.sample_rate = self.settings.audio_sample_rate
                frame.pts = 0
                container.mux(audio_stream.encode(frame))
                container.mux(audio_stream.encode(None))

        self.on_progress(1.0, "Done")
        return RenderResult(
            path=destination, kind="video", duration=duration, frames=total_frames, warnings=warnings
        )

    # -- audio -------------------------------------------------------------------------------------

    def _audio_clips(self, timeline: Timeline, resolver: TimelineResolver) -> list[tuple[Any, Any, Any]]:
        """Every audible clip, with the track it belongs to — the track carries half the gain and pan."""
        audio_tracks = [t for t in timeline.tracks if t.kind == "audio"]
        # Solo wins over mute: the moment anything is soloed, everything else is silent regardless.
        soloed = [t for t in audio_tracks if t.solo]
        audible = soloed or [t for t in audio_tracks if not t.muted]

        clips = []
        for track in audible:
            for clip in track.clips:
                if not clip.enabled:
                    continue
                resolved = resolver.resolve(clip)
                if resolved.usable and resolved.paths:
                    clips.append((clip, resolved, track))
        return clips

    def _mix_audio(self, audio_clips, duration: float, rate: int) -> np.ndarray:
        """Sum every audio clip into one stereo buffer, at its position, gain and stereo placement."""
        import av

        total_samples = max(1, int(duration * rate))
        mix = np.zeros((total_samples, 2), dtype=np.float32)

        for clip, resolved, track in audio_clips:
            try:
                samples = _decode_audio(av, resolved.paths[0], rate)
            except Exception as exc:  # noqa: BLE001 - one bad clip should not fail the render
                logger.warning("Could not decode audio for clip %s: %s", clip.id, exc)
                continue

            start_sample = int(clip.in_point * rate)
            samples = samples[start_sample:]
            length = min(len(samples), int(clip.duration * rate))
            if length <= 0:
                continue

            offset = int(clip.start * rate)
            end = min(total_samples, offset + length)
            if end <= offset:
                continue
            gain = max(0.0, clip.volume) * max(0.0, track.volume)
            mix[offset:end] += samples[: end - offset] * gain * _pan_gains(clip.pan + track.pan)

        # Normalise only if we actually clipped, so a quiet mix is not pumped up unexpectedly.
        peak = float(np.max(np.abs(mix))) if mix.size else 0.0
        if peak > 1.0:
            mix /= peak
        return mix


def _pan_gains(pan: float) -> np.ndarray:
    """Left/right gains for a stereo position, using the constant-power law.

    A linear pan dips ~3 dB in the middle, which is audible as a hole when a clip sweeps across. Taking
    the sine and cosine of the position instead keeps the total power flat wherever it sits.
    """
    position = (max(-1.0, min(1.0, pan)) + 1.0) / 2.0  # -1..1 -> 0..1
    angle = position * (math.pi / 2.0)
    return np.array([math.cos(angle), math.sin(angle)], dtype=np.float32) * math.sqrt(2.0)


def _decode_audio(av_module, path: Path, target_rate: int) -> np.ndarray:
    """Decode a file to float32 stereo at ``target_rate``."""
    resampler = av_module.audio.resampler.AudioResampler(format="fltp", layout="stereo", rate=target_rate)
    chunks: list[np.ndarray] = []

    with av_module.open(str(path)) as container:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            return np.zeros((0, 2), dtype=np.float32)
        for frame in container.decode(stream):
            for resampled in resampler.resample(frame):
                array = resampled.to_ndarray()  # (channels, samples) for planar
                chunks.append(array.T if array.ndim == 2 else array.reshape(-1, 1))

    if not chunks:
        return np.zeros((0, 2), dtype=np.float32)

    data = np.concatenate(chunks, axis=0).astype(np.float32)
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)
    return data[:, :2]


def _parse_bitrate(value: str) -> int:
    text = str(value).strip().lower()
    try:
        if text.endswith("k"):
            return int(float(text[:-1]) * 1000)
        if text.endswith("m"):
            return int(float(text[:-1]) * 1_000_000)
        return int(text)
    except ValueError:
        return 192_000
