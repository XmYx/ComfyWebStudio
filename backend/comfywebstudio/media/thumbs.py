"""Thumbnail generation.

One small file per artifact, cached by the artifact's own content hash so it is generated once and reused
across runs, projects reloads and exports. Failure is never fatal: the UI falls back to a kind icon.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ..core.store import ProjectStore
from ..settings import PreviewSettings

logger = logging.getLogger(__name__)

#: Kinds that have something visual to show.
THUMBNAILABLE = frozenset({"image", "mask", "video"})


class ThumbnailMaker:
    def __init__(self, store: ProjectStore, settings: PreviewSettings):
        self.store = store
        self.settings = settings

    def thumb_path(self, project_id: str, sha: str) -> Path:
        return self.store.thumbs_dir(project_id) / f"{sha}.{self.settings.thumbnail_format}"

    def ensure(self, project_id: str, source: Path, sha: str, kind: str) -> str | None:
        """Generate a thumbnail if needed. Returns the project-relative path, or None."""
        if kind not in THUMBNAILABLE or not sha:
            return None

        target = self.thumb_path(project_id, sha)
        if target.is_file():
            return self.store.relativize(project_id, target)

        try:
            image = self._first_frame(Path(source), kind)
            if image is None:
                return None
            image.thumbnail((self.settings.thumbnail_size, self.settings.thumbnail_size))
            temp = target.with_suffix(target.suffix + ".tmp")
            if self.settings.thumbnail_format == "png":
                image.save(temp, format="PNG", optimize=True)
            else:
                image.convert("RGB").save(
                    temp,
                    format=self.settings.thumbnail_format.upper(),
                    quality=self.settings.thumbnail_quality,
                )
            os.replace(temp, target)
        except Exception as exc:  # noqa: BLE001 - a missing thumbnail is cosmetic
            logger.debug("Could not thumbnail %s: %s", source, exc)
            return None

        return self.store.relativize(project_id, target)

    def _first_frame(self, source: Path, kind: str):
        from PIL import Image

        if kind in {"image", "mask"}:
            image = Image.open(source)
            image.load()
            return image.convert("RGBA") if image.mode in {"RGBA", "LA", "P"} else image.convert("RGB")

        # Video: decode a single frame rather than the whole stream.
        import av

        with av.open(str(source)) as container:
            stream = next((s for s in container.streams if s.type == "video"), None)
            if stream is None:
                return None
            stream.thread_type = "AUTO"
            for frame in container.decode(stream):
                return frame.to_image()
        return None
