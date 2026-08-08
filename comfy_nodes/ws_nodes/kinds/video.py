"""VIDEO persistence.

VIDEO is not a tensor — it is a ``VideoInput`` object from ``comfy_api`` exposing ``save_to()`` and
``get_components()`` (``comfy_api/latest/_input/video_types.py:9``). We delegate to those rather than
re-encoding by hand, which also means a remux-only save stays remux-only.

Both sides are duck-typed so the pack still imports on a ComfyUI old enough to lack ``comfy_api``; the video
kind simply reports itself unavailable instead of breaking every other node.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from .base import allocate, register, saved


def _video_from_file(path: str):
    """Construct a VideoInput for ``path``, or raise a clear error on an incompatible ComfyUI."""
    try:
        from comfy_api.latest import InputImpl  # type: ignore

        return InputImpl.VideoFromFile(path)
    except Exception as exc:  # noqa: BLE001 - many failure modes, all meaning "no VIDEO support here"
        raise RuntimeError(
            "This ComfyUI build does not expose comfy_api VIDEO support, so video ports cannot be used."
        ) from exc


class VideoHandler:
    kind = "video"
    formats = ("mp4", "webm", "mkv", "mov")
    default_format = "mp4"

    def save(self, value, directory, subfolder, port_name, fmt, opts):
        if value is None:
            raise ValueError("WSVideoOutput received no video")

        path, filename, _ = allocate(directory, port_name, fmt)
        codec = str(opts.get("codec", "auto"))

        container_enum, codec_enum = _enums(fmt, codec)
        value.save_to(path, format=container_enum, codec=codec_enum, metadata=opts.get("metadata"))

        meta: dict[str, Any] = {"count": 1, "format": fmt}
        # Probing is best-effort: a container we just wrote should answer, but a codec quirk must not
        # invalidate an otherwise good render.
        try:
            width, height = value.get_dimensions()
            meta.update(
                width=int(width),
                height=int(height),
                fps=float(Fraction(value.get_frame_rate())),
                frames=int(value.get_frame_count()),
                duration=round(float(value.get_duration()), 4),
            )
        except Exception:  # noqa: BLE001
            meta["probe_failed"] = True

        return [saved(filename, subfolder)], meta

    def load(self, path: str, opts: dict[str, Any]):
        return _video_from_file(path)


def _enums(container: str, codec: str):
    """Map our plain strings onto comfy_api's enums, falling back to AUTO for anything unrecognised."""
    try:
        from comfy_api.latest import Types  # type: ignore
    except Exception:  # noqa: BLE001
        return container, codec

    def pick(enum_cls, value: str):
        try:
            return enum_cls(value)
        except ValueError:
            return getattr(enum_cls, "AUTO", value)

    return pick(Types.VideoContainer, container), pick(Types.VideoCodec, codec)


register(VideoHandler())
