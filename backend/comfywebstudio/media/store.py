"""Content-addressed media store.

Artifacts are filed by SHA-256 under ``<project>/assets/<kind>/<ab>/<sha>.<ext>``. Two consequences that
matter:

* **Dedup is free.** Re-running a step that produces byte-identical output reuses the existing file, and the
  hash doubles as the cache key for downstream steps.
* **Projects are self-contained.** Ingesting on a shared filesystem hardlinks rather than copies, so a
  project owns its media at essentially no disk cost, and export is a plain zip.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from pathlib import Path

from ..core.ids import safe_component
from ..core.store import ProjectStore

logger = logging.getLogger(__name__)

_CHUNK = 1 << 20

#: Sensible extension per kind when the source file has none.
_DEFAULT_EXTENSION = {
    "image": "png",
    "mask": "png",
    "video": "mp4",
    "audio": "flac",
    "latent": "latent",
    "string": "txt",
    "int": "txt",
    "float": "txt",
    "boolean": "txt",
    "file": "bin",
}


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class MediaStore:
    """Places artifact files inside a project and hands back project-relative paths."""

    def __init__(self, store: ProjectStore):
        self.store = store

    def _target(self, project_id: str, kind: str, sha: str, extension: str) -> Path:
        extension = safe_component(extension.lstrip("."), _DEFAULT_EXTENSION.get(kind, "bin"), 16)
        directory = self.store.assets_dir(project_id) / safe_component(kind, "file") / sha[:2]
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{sha}.{extension}"

    def ingest_file(self, project_id: str, source: Path, *, kind: str) -> tuple[str, str]:
        """File a local file into the project. Returns ``(project_relative_path, sha256)``."""
        source = Path(source)
        if not source.is_file():
            raise FileNotFoundError(f"Cannot ingest missing file: {source}")

        sha = hash_file(source)
        extension = source.suffix or f".{_DEFAULT_EXTENSION.get(kind, 'bin')}"
        target = self._target(project_id, kind, sha, extension)

        if not target.exists():
            _link_or_copy(source, target)
        return self.store.relativize(project_id, target), sha

    def ingest_bytes(
        self, project_id: str, data: bytes, *, kind: str, extension: str
    ) -> tuple[str, str]:
        """File bytes fetched from a remote ComfyUI."""
        sha = hash_bytes(data)
        target = self._target(project_id, kind, sha, extension)
        if not target.exists():
            temp = target.with_suffix(target.suffix + ".tmp")
            temp.write_bytes(data)
            os.replace(temp, target)
        return self.store.relativize(project_id, target), sha

    def path(self, project_id: str, relative_path: str) -> Path:
        return self.store.resolve(project_id, relative_path)

    def exists(self, project_id: str, relative_path: str) -> bool:
        try:
            return self.path(project_id, relative_path).is_file()
        except Exception:  # noqa: BLE001 - a bad stored path means "not usable", not a crash
            return False

    def prune_orphans(self, project_id: str, referenced: set[str]) -> int:
        """Delete asset files no run or timeline still references. Returns how many were removed.

        Never called automatically — losing a preview because a heuristic thought it was unused would be
        worse than the disk it saves.
        """
        assets_root = self.store.assets_dir(project_id)
        keep = {self.store.resolve(project_id, r) for r in referenced}
        removed = 0
        for path in assets_root.rglob("*"):
            if path.is_file() and path not in keep:
                path.unlink(missing_ok=True)
                removed += 1
        return removed


def _link_or_copy(source: Path, target: Path) -> None:
    """Hardlink when the filesystem allows it, otherwise copy.

    A hardlink makes ingesting a 2 GB video free; falling back keeps it correct across filesystems.
    """
    try:
        os.link(source, target)
        return
    except OSError:
        pass
    temp = target.with_suffix(target.suffix + ".tmp")
    shutil.copy2(source, temp)
    os.replace(temp, target)


def extension_for(kind: str, filename: str = "") -> str:
    suffix = Path(filename).suffix.lstrip(".")
    return suffix or _DEFAULT_EXTENSION.get(kind, "bin")
