"""Peak data for drawing an audio file as a waveform.

A clip on the timeline is a rectangle until you can see its shape, and seeing the shape is what makes an
audio edit possible at all — you cut on the silence, not on the timecode. Sending the samples themselves
would be absurd (a minute of stereo 48 kHz is ~11 MB), so the file is reduced here to a fixed number of
min/max pairs, which is all a waveform drawing ever needs.

Results are cached on disk next to the media, keyed by the file's content hash and the requested
resolution: decoding is the expensive part, and a timeline redraws constantly.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: How many min/max pairs to reduce a file to. Enough detail for a clip a few hundred pixels wide, and
#: small enough (~16 KB of JSON) that fetching one per clip is unremarkable.
DEFAULT_BUCKETS = 800

#: Above this, a request is refused rather than served — the point is a drawing, not the samples back.
MAX_BUCKETS = 4000


@dataclass(slots=True)
class Waveform:
    """One drawing's worth of peaks."""

    #: Per bucket, the lowest and highest sample in it, each -1..1.
    peaks: list[tuple[float, float]]
    duration: float
    sample_rate: int
    channels: int

    def as_dict(self) -> dict:
        return {
            "peaks": [[round(low, 4), round(high, 4)] for low, high in self.peaks],
            "duration": round(self.duration, 4),
            "sample_rate": self.sample_rate,
            "channels": self.channels,
        }


def _cache_path(path: Path, buckets: int) -> Path:
    stat = path.stat()
    key = f"{path.resolve()}:{stat.st_size}:{int(stat.st_mtime)}:{buckets}"
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    return path.parent / f".{path.stem}.{digest}.peaks.json"


def compute(path: Path, buckets: int = DEFAULT_BUCKETS) -> Waveform:
    """Reduce an audio file to `buckets` min/max pairs, decoding it only once per file.

    Mono-summed on purpose: a waveform is read as one shape, and drawing the channels separately in a
    clip a few pixels tall is noise rather than information.
    """
    import av
    import numpy as np

    buckets = max(1, min(MAX_BUCKETS, buckets))
    cache = _cache_path(path, buckets)
    if cache.is_file():
        try:
            stored = json.loads(cache.read_text())
            return Waveform(
                peaks=[tuple(pair) for pair in stored["peaks"]],
                duration=stored["duration"],
                sample_rate=stored["sample_rate"],
                channels=stored["channels"],
            )
        except Exception as exc:  # noqa: BLE001 - a corrupt cache is re-derived, never fatal
            logger.debug("Ignoring unreadable waveform cache %s: %s", cache.name, exc)

    chunks: list[np.ndarray] = []
    sample_rate = 0
    channels = 0

    with av.open(str(path)) as container:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise ValueError(f"{path.name} has no audio stream")
        sample_rate = int(stream.rate or 0)
        channels = int(getattr(stream.codec_context, "channels", 0) or 0)
        for frame in container.decode(stream):
            array = frame.to_ndarray()
            frame_channels = len(frame.layout.channels) if frame.layout else 1
            if frame.format.is_planar:
                # (channels, samples) — averaging down the channel axis is the mono sum.
                mono = array.mean(axis=0) if array.ndim == 2 else array.reshape(-1)
            else:
                # Packed frames arrive as one interleaved row, so they have to be de-interleaved before
                # averaging. Reshaping blindly makes a stereo file look twice as long as it is.
                flat = array.reshape(-1)
                mono = (
                    flat.reshape(-1, frame_channels).mean(axis=1) if frame_channels > 1 else flat
                )
            chunks.append(mono.astype("float32", copy=False))

    samples = np.concatenate(chunks) if chunks else np.zeros(0, dtype="float32")
    if samples.size and float(np.max(np.abs(samples))) > 1.0:
        # Integer formats decode outside -1..1; scale so the drawing is comparable across files.
        samples = samples / float(np.max(np.abs(samples)))

    duration = float(samples.size / sample_rate) if sample_rate else 0.0
    peaks = _bucket(samples, buckets)
    waveform = Waveform(peaks=peaks, duration=duration, sample_rate=sample_rate, channels=channels)

    try:
        cache.write_text(json.dumps(waveform.as_dict()))
    except OSError as exc:
        logger.debug("Could not cache the waveform for %s: %s", path.name, exc)
    return waveform


def _bucket(samples, buckets: int) -> list[tuple[float, float]]:
    """The lowest and highest sample in each of `buckets` equal spans."""
    import numpy as np

    if samples.size == 0:
        return [(0.0, 0.0)] * buckets

    # Pad to a whole number of buckets so the reduction is one reshape rather than a Python loop.
    per_bucket = max(1, samples.size // buckets)
    usable = per_bucket * buckets
    if usable > samples.size:
        samples = np.pad(samples, (0, usable - samples.size))
    windows = samples[:usable].reshape(buckets, per_bucket)
    lows = windows.min(axis=1)
    highs = windows.max(axis=1)
    # Rounded here, not only on the way out, so a cached waveform equals a freshly computed one exactly.
    return [(round(float(low), 4), round(float(high), 4)) for low, high in zip(lows, highs, strict=True)]
