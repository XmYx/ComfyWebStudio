"""Moving media between the project and a ComfyUI instance.

Two directions:

* **In** — an artifact ComfyUI produced becomes a project asset (:meth:`MediaTransfer.ingest_output`).
  Locally that is a hardlink; remotely it is a download.
* **Out** — a project asset is made readable by the next step (:meth:`MediaTransfer.stage_for_input`).
  Locally that is just its absolute path; remotely it is an upload.

The kind conversions a link may legally imply (image to mask and back, number to text) also happen here,
because that is the moment we have a concrete file and know what the consumer expects.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..comfy.backend import ComfyBackend, ComfyFileRef
from ..core.models import Artifact, ComfyRef
from ..core.store import ProjectStore
from ..settings import PreviewSettings
from .probe import probe
from .store import MediaStore, extension_for
from .thumbs import ThumbnailMaker

logger = logging.getLogger(__name__)


class MediaTransfer:
    """Everything that moves bytes between a project and a backend."""

    def __init__(
        self,
        store: ProjectStore,
        media: MediaStore,
        preview_settings: PreviewSettings,
    ):
        self.store = store
        self.media = media
        self.thumbs = ThumbnailMaker(store, preview_settings)

    # -- inbound -----------------------------------------------------------------------------------

    async def ingest_output(
        self,
        project_id: str,
        backend: ComfyBackend,
        *,
        kind: str,
        port_key: str,
        ref: ComfyFileRef,
        meta: dict | None = None,
    ) -> Artifact:
        """Bring one file ComfyUI produced into the project."""
        local = backend.artifact_path(ref)

        if local is not None and local.is_file():
            relative, sha = self.media.ingest_file(project_id, local, kind=kind)
        else:
            data = await backend.read_artifact(ref)
            relative, sha = self.media.ingest_bytes(
                project_id, data, kind=kind, extension=extension_for(kind, ref.filename)
            )

        path = self.media.path(project_id, relative)
        merged = dict(meta or {})
        # The node reported what it knows; probing fills in what it does not (duration, codec, real fps).
        for key, value in probe(path, kind).items():
            merged.setdefault(key, value)

        return Artifact(
            kind=kind,  # type: ignore[arg-type]
            port_key=port_key,
            path=relative,
            comfy_ref=ComfyRef(**ref.to_dict()),
            sha256=sha,
            thumb=self.thumbs.ensure(project_id, path, sha, kind),
            meta=merged,
        )

    # -- outbound ----------------------------------------------------------------------------------

    async def stage_for_input(
        self,
        project_id: str,
        backend: ComfyBackend,
        artifact: Artifact,
        *,
        target_kind: str,
        run_key: str,
    ) -> str:
        """Make ``artifact`` readable by ``backend`` as ``target_kind``; returns the ``source`` value."""
        path = self.media.path(project_id, artifact.path)
        if not path.is_file():
            raise FileNotFoundError(
                f"Artifact for port {artifact.port_key!r} is missing from the project: {artifact.path}"
            )

        if target_kind != artifact.kind:
            path = self._convert(project_id, path, artifact.kind, target_kind)

        staged = await backend.stage(path, run_key=run_key, kind=target_kind)
        return staged.source

    def scalar_value(self, project_id: str, artifact: Artifact, target_kind: str):
        """Value of a scalar artifact, for a link that feeds a text or number input.

        Read from metadata when present (the node records it), falling back to the file.
        """
        value = artifact.meta.get("value")
        if value is None:
            path = self.media.path(project_id, artifact.path)
            value = path.read_text(encoding="utf-8") if path.is_file() else ""

        if target_kind == "string":
            return value if isinstance(value, str) else str(value)
        if target_kind == "int":
            return int(float(value))
        if target_kind == "float":
            return float(value)
        if target_kind == "boolean":
            return str(value).strip().lower() in {"1", "true", "yes", "on"} if isinstance(value, str) else bool(value)
        return value

    # -- conversions -------------------------------------------------------------------------------

    def _convert(self, project_id: str, path: Path, from_kind: str, to_kind: str) -> Path:
        """Adapt a file whose kind differs from what the consumer wants.

        Only the conversions ``can_connect`` permits are reachable here; anything else was rejected when
        the link was created.
        """
        pair = (from_kind, to_kind)

        if pair in {("image", "mask"), ("mask", "image")}:
            return self._convert_image_mask(project_id, path, to_kind)
        if to_kind == "file":
            return path  # a path is a path

        logger.debug("No conversion needed or available for %s -> %s", from_kind, to_kind)
        return path

    def _convert_image_mask(self, project_id: str, path: Path, to_kind: str) -> Path:
        from PIL import Image

        with Image.open(path) as img:
            converted = img.convert("L") if to_kind == "mask" else img.convert("RGB")
            import io

            buffer = io.BytesIO()
            converted.save(buffer, format="PNG")

        relative, _ = self.media.ingest_bytes(
            project_id, buffer.getvalue(), kind=to_kind, extension="png"
        )
        return self.media.path(project_id, relative)
