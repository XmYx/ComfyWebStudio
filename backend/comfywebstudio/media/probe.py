"""Read metadata out of a media file.

Used to fill in what a node did not report, and to describe imported assets that no step produced. Every
probe is best-effort: bad metadata should degrade a preview, never fail a run.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tiff"}
VIDEO_SUFFIXES = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v"}
AUDIO_SUFFIXES = {".wav", ".flac", ".mp3", ".ogg", ".opus", ".m4a", ".aac"}


def guess_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    if suffix == ".latent":
        return "latent"
    if suffix in {".txt", ".json", ".md"}:
        return "string"
    return "file"


def probe(path: Path, kind: str | None = None) -> dict[str, Any]:
    """Metadata for one file: dimensions, duration, frame rate, whatever applies."""
    path = Path(path)
    if not path.is_file():
        return {}

    resolved = kind or guess_kind(path)
    meta: dict[str, Any] = {"size": path.stat().st_size, "kind": resolved}

    try:
        if resolved in {"image", "mask"}:
            meta.update(_probe_image(path))
        elif resolved == "video":
            meta.update(_probe_video(path))
        elif resolved == "audio":
            meta.update(_probe_audio(path))
        elif resolved in {"string", "int", "float", "boolean"}:
            meta.update(_probe_text(path))
    except Exception as exc:  # noqa: BLE001 - a corrupt file must not break a listing
        logger.debug("Could not probe %s: %s", path, exc)
        meta["probe_error"] = str(exc)

    return meta


def _probe_image(path: Path) -> dict[str, Any]:
    from PIL import Image

    with Image.open(path) as img:
        info: dict[str, Any] = {"width": img.width, "height": img.height, "mode": img.mode}
        frames = getattr(img, "n_frames", 1)
        if frames > 1:
            info["frames"] = frames
            info["animated"] = True
        return info


def _probe_video(path: Path) -> dict[str, Any]:
    import av

    with av.open(str(path)) as container:
        stream = next((s for s in container.streams if s.type == "video"), None)
        if stream is None:
            return {}
        rate = stream.average_rate or stream.base_rate
        duration = float(container.duration / 1_000_000) if container.duration else None
        frames = int(stream.frames) if stream.frames else None
        if frames is None and duration and rate:
            frames = int(duration * float(rate))

        # Frames over rate, whenever both are known. The container's own duration is a presentation
        # length: it carries the edit list, the encoder's priming and a microsecond rounding, so a clip
        # of exactly N frames routinely reports a few milliseconds more. A timeline that takes that at
        # face value gives the clip N+1 frames — and the last one has nothing behind it, which is the
        # held frame at the end of every freshly placed clip.
        exact = False
        if frames and rate:
            duration = frames / float(rate)
            exact = True
        info: dict[str, Any] = {
            "width": stream.codec_context.width,
            "height": stream.codec_context.height,
            "fps": float(rate) if rate else None,
            # Kept exact when it came from the frame count. Rounding 49/24 to six places gives
            # 2.041667, which is a hair *over* 49 frames — enough for anything that rounds up to make it
            # 50. Only the fallback, which was never exact to begin with, is tidied.
            "duration": (duration if exact else round(duration, 6)) if duration else None,
            "frames": frames,
            "codec": stream.codec_context.name,
        }
        audio = next((s for s in container.streams if s.type == "audio"), None)
        info["has_audio"] = audio is not None
        return info


def _probe_audio(path: Path) -> dict[str, Any]:
    import av

    with av.open(str(path)) as container:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            return {}
        duration = float(container.duration / 1_000_000) if container.duration else None
        return {
            "sample_rate": int(stream.codec_context.sample_rate or 0) or None,
            "channels": int(getattr(stream.codec_context, "channels", 0) or 0) or None,
            "duration": round(duration, 4) if duration else None,
            "codec": stream.codec_context.name,
        }


def _probe_text(path: Path) -> dict[str, Any]:
    """Read a short text artifact so its value can be shown inline and chained without a file read."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    return {"value": text, "characters": len(text)} if len(text) <= 100_000 else {"characters": len(text)}
