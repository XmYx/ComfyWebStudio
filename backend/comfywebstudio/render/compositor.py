"""Turning a timeline into frames.

The compositor resolves each clip to concrete media, then for any given time produces one composited RGB
frame: tracks bottom-to-top, each clip transformed, faded and alpha-blended over what is below.

Clips reference *ports*, not files. A clip pointing at "shot A, step 2, port `image`" always shows that
step's most recent successful result, so re-running a shot updates the edit without touching the timeline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from ..core.models import Artifact, Clip, Project, Timeline, Track
from ..core.store import ProjectStore

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ResolvedClip:
    """A clip with its media located and its frames enumerated."""

    clip: Clip
    kind: str
    #: Image sequences hold every frame; video holds one path; text holds none.
    paths: list[Path] = field(default_factory=list)
    fps: float | None = None
    source_duration: float | None = None
    error: str | None = None

    @property
    def usable(self) -> bool:
        return self.error is None and (bool(self.paths) or self.kind in {"text", "string"})


class TimelineResolver:
    """Finds the media behind each clip."""

    def __init__(self, store: ProjectStore, project: Project):
        self.store = store
        self.project = project
        self._latest: dict[str, dict[str, Any]] = {}

    def _step_outputs(self, shot_id: str) -> dict[str, Any]:
        if shot_id not in self._latest:
            self._latest[shot_id] = self.store.latest_step_runs(self.project.id, shot_id)
        return self._latest[shot_id]

    def artifacts_for(self, clip: Clip) -> tuple[list[Artifact], str | None]:
        source = clip.source
        if source.kind == "asset":
            asset = self.project.assets.get(source.asset_id or "")
            if asset is None:
                return [], "The asset this clip points at is no longer in the project."
            return (
                [Artifact(kind=asset.kind, port_key="asset", path=asset.path,
                          sha256=asset.sha256, meta=asset.meta)],
                None,
            )

        if not (source.shot_id and source.step_id and source.port_key):
            return [], "This clip is not pointed at anything yet."

        entry = self._step_outputs(source.shot_id).get(source.step_id)
        if entry is None:
            shot = self.project.shot(source.shot_id)
            step = shot.step(source.step_id) if shot else None
            return [], (
                f"{step.name if step else 'That step'} has not produced a result yet. Run the shot first."
            )

        matches = [a for a in entry["step_run"].outputs if a.port_key == source.port_key]
        if not matches:
            return [], f"The latest run produced no output on port {source.port_key!r}."
        return matches, None

    def resolve(self, clip: Clip) -> ResolvedClip:
        if clip.text and not clip.source.step_id and not clip.source.asset_id:
            return ResolvedClip(clip=clip, kind="text")

        artifacts, error = self.artifacts_for(clip)
        if error:
            return ResolvedClip(clip=clip, kind="unknown", error=error)

        kind = artifacts[0].kind
        paths: list[Path] = []
        for artifact in artifacts:
            try:
                path = self.store.resolve(self.project.id, artifact.path)
            except Exception as exc:  # noqa: BLE001
                return ResolvedClip(clip=clip, kind=kind, error=str(exc))
            if not path.is_file():
                return ResolvedClip(
                    clip=clip, kind=kind,
                    error=f"Media for this clip is missing from the project: {artifact.path}",
                )
            paths.append(path)

        meta = artifacts[0].meta
        return ResolvedClip(
            clip=clip,
            kind=kind,
            paths=paths,
            fps=_as_float(meta.get("fps")),
            source_duration=_as_float(meta.get("duration")),
        )


class FrameCompositor:
    """Renders one timeline frame at a time."""

    def __init__(self, timeline: Timeline, resolver: TimelineResolver):
        self.timeline = timeline
        self.resolver = resolver
        self._resolved: dict[str, ResolvedClip] = {}
        self._video_readers: dict[str, Any] = {}
        self._image_cache: dict[Path, Image.Image] = {}

    def prepare(self) -> list[str]:
        """Resolve every clip up front. Returns problems worth showing before a long render starts."""
        problems: list[str] = []
        for track in self.timeline.tracks:
            if track.kind == "audio":
                continue
            for clip in track.clips:
                if not clip.enabled:
                    continue
                resolved = self.resolver.resolve(clip)
                self._resolved[clip.id] = resolved
                if resolved.error:
                    problems.append(f"{track.name} · {clip.name or clip.id}: {resolved.error}")
        return problems

    def visible_tracks(self) -> list[Track]:
        return [t for t in self.timeline.tracks if t.kind != "audio" and not t.muted]

    def frame_at(self, time_s: float) -> Image.Image:
        canvas = Image.new(
            "RGBA", (self.timeline.width, self.timeline.height), _hex_to_rgba(self.timeline.background)
        )
        for track in self.visible_tracks():
            for clip in track.clips:
                if not clip.enabled or not (clip.start <= time_s < clip.end):
                    continue
                layer = self._clip_layer(clip, time_s)
                if layer is not None:
                    canvas = Image.alpha_composite(canvas, layer)
        return canvas.convert("RGB")

    # -- per clip ----------------------------------------------------------------------------------

    def _clip_layer(self, clip: Clip, time_s: float) -> Image.Image | None:
        resolved = self._resolved.get(clip.id)
        if resolved is None or not resolved.usable:
            return None

        local_t = time_s - clip.start
        if resolved.kind == "text":
            image = self._render_text(clip)
        elif resolved.kind == "video":
            image = self._video_frame(resolved, local_t)
        elif resolved.kind in {"image", "mask"}:
            image = self._sequence_frame(resolved, local_t)
        else:
            return None

        if image is None:
            return None

        placed = self._place(image, clip)
        opacity = clip.opacity * _fade_factor(clip, local_t)
        if opacity < 1.0:
            alpha = placed.getchannel("A").point(lambda v: int(v * max(0.0, min(1.0, opacity))))
            placed.putalpha(alpha)
        return placed

    def _sequence_frame(self, resolved: ResolvedClip, local_t: float) -> Image.Image | None:
        """Pick a frame from an image sequence.

        A single image holds for the clip's whole duration; a multi-image batch is played across it, which
        is what makes a batch of generated frames behave like footage on the timeline.
        """
        count = len(resolved.paths)
        if count == 0:
            return None
        if count == 1:
            return self._load_image(resolved.paths[0])

        duration = max(resolved.clip.duration, 1e-6)
        index = min(count - 1, max(0, int((local_t / duration) * count)))
        return self._load_image(resolved.paths[index])

    def _load_image(self, path: Path) -> Image.Image:
        cached = self._image_cache.get(path)
        if cached is None:
            with Image.open(path) as img:
                cached = img.convert("RGBA")
            # Sequences are re-read every frame otherwise; cap the cache so a long sequence cannot
            # exhaust memory.
            if len(self._image_cache) < 64:
                self._image_cache[path] = cached
        return cached

    def _video_frame(self, resolved: ResolvedClip, local_t: float) -> Image.Image | None:
        import av

        path = resolved.paths[0]
        reader = self._video_readers.get(str(path))
        if reader is None:
            container = av.open(str(path))
            stream = next((s for s in container.streams if s.type == "video"), None)
            if stream is None:
                return None
            stream.thread_type = "AUTO"
            reader = {"container": container, "stream": stream, "last_t": -1.0, "frame": None}
            self._video_readers[str(path)] = reader

        target = resolved.clip.in_point + local_t
        if reader["frame"] is not None and abs(target - reader["last_t"]) < 1e-6:
            return reader["frame"]

        container, stream = reader["container"], reader["stream"]
        try:
            # Seeking backwards is expensive, so only do it when we actually moved backwards.
            if target < reader["last_t"]:
                container.seek(int(target / float(stream.time_base)), stream=stream)
            for frame in container.decode(stream):
                if frame.time is None or frame.time >= target - 1e-4:
                    image = frame.to_image().convert("RGBA")
                    reader["frame"], reader["last_t"] = image, target
                    return image
        except Exception as exc:  # noqa: BLE001 - a decode hiccup should hold the last frame
            logger.debug("Video decode issue in %s at %.3fs: %s", path.name, target, exc)

        return reader["frame"]

    def _render_text(self, clip: Clip) -> Image.Image:
        style = clip.text_style or {}
        size = int(style.get("size", max(24, self.timeline.height // 16)))
        color = style.get("color", "#ffffff")

        layer = Image.new("RGBA", (self.timeline.width, self.timeline.height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        font = _load_font(size)

        lines = str(clip.text).splitlines() or [""]
        line_height = size + int(size * 0.3)
        total = line_height * len(lines)
        align = str(style.get("align", "center"))
        top = {
            "top": int(self.timeline.height * 0.08),
            "bottom": self.timeline.height - total - int(self.timeline.height * 0.08),
        }.get(str(style.get("vertical", "center")), (self.timeline.height - total) // 2)

        for index, line in enumerate(lines):
            box = draw.textbbox((0, 0), line, font=font)
            width = box[2] - box[0]
            x = {
                "left": int(self.timeline.width * 0.06),
                "right": self.timeline.width - width - int(self.timeline.width * 0.06),
            }.get(align, (self.timeline.width - width) // 2)
            y = top + index * line_height

            if style.get("shadow", True):
                draw.text((x + 2, y + 2), line, font=font, fill=(0, 0, 0, 170))
            draw.text((x, y), line, font=font, fill=color)

        return layer

    def _place(self, image: Image.Image, clip: Clip) -> Image.Image:
        """Fit, scale, rotate and offset one clip's image onto a full-canvas layer."""
        transform = clip.transform
        canvas_w, canvas_h = self.timeline.width, self.timeline.height
        source = image

        if transform.rotation:
            source = source.rotate(transform.rotation, expand=True, resample=Image.BICUBIC)

        if transform.fit == "stretch":
            target = (canvas_w, canvas_h)
        elif transform.fit == "none":
            target = source.size
        else:
            ratio = min(canvas_w / source.width, canvas_h / source.height)
            if transform.fit == "cover":
                ratio = max(canvas_w / source.width, canvas_h / source.height)
            target = (max(1, int(source.width * ratio)), max(1, int(source.height * ratio)))

        scale = max(0.01, transform.scale)
        target = (max(1, int(target[0] * scale)), max(1, int(target[1] * scale)))
        if target != source.size:
            source = source.resize(target, Image.LANCZOS)

        layer = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        x = (canvas_w - source.width) // 2 + int(transform.offset_x)
        y = (canvas_h - source.height) // 2 + int(transform.offset_y)
        layer.alpha_composite(source, (max(-source.width, x), max(-source.height, y)))
        return layer

    def close(self) -> None:
        for reader in self._video_readers.values():
            try:
                reader["container"].close()
            except Exception:  # noqa: BLE001
                pass
        self._video_readers.clear()
        self._image_cache.clear()


def _fade_factor(clip: Clip, local_t: float) -> float:
    factor = 1.0
    if clip.transition_in.kind != "none" and clip.transition_in.duration > 0:
        factor *= min(1.0, local_t / clip.transition_in.duration)
    if clip.transition_out.kind != "none" and clip.transition_out.duration > 0:
        remaining = clip.duration - local_t
        factor *= min(1.0, remaining / clip.transition_out.duration)
    return max(0.0, min(1.0, factor))


_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "C:/Windows/Fonts/arial.ttf",
)


def _load_font(size: int):
    for candidate in _FONT_CANDIDATES:
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    # Pillow's built-in bitmap font ignores size, so text will be small — but it always exists.
    return ImageFont.load_default()


def _hex_to_rgba(value: str) -> tuple[int, int, int, int]:
    text = str(value or "#000000").lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16), 255)
    except (ValueError, IndexError):
        return (0, 0, 0, 255)


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
