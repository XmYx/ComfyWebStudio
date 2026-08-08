"""Backend abstraction over one ComfyUI instance.

The whole point of this layer is that everything above it works the same whether ComfyUI is on this machine
or behind a URL in a datacentre. The difference shows up in exactly one place — how a file produced by one
step is made readable by the next — and that is what :meth:`ComfyBackend.stage` encapsulates.

Register a new backend kind with :func:`register_backend`; nothing else needs editing.
"""

from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from ..core.ids import safe_relative_key
from ..settings import ComfyBackendConfig
from .http import ComfyError, ComfyHttpClient
from .ws import ComfyWsClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ComfyFileRef:
    """A file as ComfyUI refers to it — the triple used by ``/view`` and every ``ui`` payload."""

    filename: str
    subfolder: str = ""
    type: str = "output"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ComfyFileRef:
        return cls(
            filename=str(data.get("filename", "")),
            subfolder=str(data.get("subfolder") or ""),
            type=str(data.get("type") or "output"),
        )

    def to_dict(self) -> dict[str, str]:
        return {"filename": self.filename, "subfolder": self.subfolder, "type": self.type}

    @property
    def relative_path(self) -> str:
        return f"{self.subfolder}/{self.filename}" if self.subfolder else self.filename


@dataclass(frozen=True, slots=True)
class StagedInput:
    """The result of making a local file visible to ComfyUI.

    ``source`` is written verbatim into a ``WS*Input`` node's ``source`` widget.
    """

    source: str
    ref: ComfyFileRef | None = None
    #: True when nothing had to be copied or uploaded.
    zero_copy: bool = False


@dataclass(slots=True)
class BackendStatus:
    reachable: bool
    error: str | None = None
    comfyui_version: str | None = None
    devices: list[dict[str, Any]] | None = None
    node_pack: dict[str, Any] | None = None
    protocol_ok: bool = False
    queue_remaining: int = 0
    websocket_connected: bool = False


class ComfyBackend(ABC):
    """One configured ComfyUI instance, with its HTTP and websocket clients."""

    kind: ClassVar[str] = "base"

    def __init__(self, config: ComfyBackendConfig, *, client_id: str | None = None):
        self.config = config
        self.client_id = client_id or uuid.uuid4().hex
        self.http = ComfyHttpClient(config.base_url, headers=config.headers, timeout=config.timeout_s)
        self.ws = ComfyWsClient(config.base_url, self.client_id, headers=config.headers)
        self._manifest: dict[str, Any] | None = None

    # -- lifecycle ---------------------------------------------------------------------------------

    async def start(self) -> None:
        await self.ws.start()

    async def close(self) -> None:
        await self.ws.stop()
        await self.http.aclose()

    async def status(self) -> BackendStatus:
        """Everything the Settings page shows for a 'Test connection' click."""
        from .. import PROTOCOL_VERSION

        try:
            stats = await self.http.system_stats()
        except ComfyError as exc:
            return BackendStatus(reachable=False, error=str(exc))

        pack = await self.http.webstudio_ping()
        return BackendStatus(
            reachable=True,
            comfyui_version=(stats.get("system") or {}).get("comfyui_version"),
            devices=stats.get("devices") or [],
            node_pack=pack,
            protocol_ok=bool(pack) and pack.get("protocol") == PROTOCOL_VERSION,
            queue_remaining=self.ws.queue_remaining,
            websocket_connected=self.ws.is_connected,
        )

    async def manifest(self, *, refresh: bool = False) -> dict[str, Any] | None:
        """Node pack manifest, cached. Discovery keys off this so it follows the installed pack version."""
        if self._manifest is None or refresh:
            self._manifest = await self.http.webstudio_manifest()
        return self._manifest

    # -- media -------------------------------------------------------------------------------------

    @abstractmethod
    async def stage(self, local_path: Path, *, run_key: str, kind: str) -> StagedInput:
        """Make ``local_path`` readable by this ComfyUI instance."""

    @abstractmethod
    def artifact_path(self, ref: ComfyFileRef) -> Path | None:
        """Filesystem path for an artifact, or None when we have no filesystem access to it."""

    async def read_artifact(self, ref: ComfyFileRef) -> bytes:
        """Bytes of an artifact, preferring a direct read when the filesystem is shared."""
        path = self.artifact_path(ref)
        if path is not None and path.is_file():
            return path.read_bytes()
        return await self.http.view(ref.filename, subfolder=ref.subfolder, folder_type=ref.type)


class LocalBackend(ComfyBackend):
    """ComfyUI on this machine, with a shared filesystem.

    Chaining is zero-copy: our ``WS*Input`` nodes accept an absolute path, so a file produced by one step is
    handed to the next by reference. Nothing is duplicated and nothing crosses the network.
    """

    kind = "local"

    @property
    def comfy_root(self) -> Path | None:
        return Path(self.config.comfy_root) if self.config.comfy_root else None

    async def stage(self, local_path: Path, *, run_key: str, kind: str) -> StagedInput:
        path = Path(local_path)
        if not path.is_file():
            raise FileNotFoundError(f"Cannot stage missing file: {path}")
        return StagedInput(source=str(path.resolve()), zero_copy=True)

    async def stage_into_input_dir(self, local_path: Path, *, run_key: str) -> StagedInput:
        """Link a file into ComfyUI's ``input/`` directory.

        Needed when a *stock* node is the target — ``LoadImage`` only accepts a name relative to ``input/``,
        unlike our own input nodes. Hardlink first (free), symlink next, copy only as a last resort.
        """
        root = self.comfy_root
        if root is None:
            raise ComfyError("Local backend has no comfy_root configured")

        subfolder = f"webstudio/{safe_relative_key(run_key)}"
        directory = root / "input" / subfolder
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / Path(local_path).name

        if not destination.exists():
            _link_or_copy(Path(local_path), destination)

        return StagedInput(
            source=f"{subfolder}/{destination.name}",
            ref=ComfyFileRef(filename=destination.name, subfolder=subfolder, type="input"),
            zero_copy=True,
        )

    def artifact_path(self, ref: ComfyFileRef) -> Path | None:
        root = self.comfy_root
        if root is None:
            return None
        directory = {"output": "output", "input": "input", "temp": "temp"}.get(ref.type, "output")
        base = (root / directory).resolve()
        candidate = (base / ref.subfolder / ref.filename).resolve()
        # ``subfolder`` comes back from ComfyUI, but it originated in a prompt we may not have written.
        if not candidate.is_relative_to(base):
            logger.warning("Refusing artifact path outside %s: %s", base, candidate)
            return None
        return candidate


class RemoteBackend(ComfyBackend):
    """ComfyUI reachable only over HTTP.

    Every chained file has to travel: download from ``/view``, upload to the remote input directory.
    Images go through ``/upload/image``; everything else needs our node pack's ``/webstudio/ingest``.
    """

    kind = "remote"

    async def stage(self, local_path: Path, *, run_key: str, kind: str) -> StagedInput:
        path = Path(local_path)
        if not path.is_file():
            raise FileNotFoundError(f"Cannot stage missing file: {path}")
        data = path.read_bytes()

        if kind in {"image", "mask"}:
            result = await self.http.upload_image(data, path.name, subfolder=f"webstudio/{run_key}")
            name = result.get("name", path.name)
            subfolder = result.get("subfolder", "")
            source = f"{subfolder}/{name}" if subfolder else name
            return StagedInput(
                source=source,
                ref=ComfyFileRef(filename=name, subfolder=subfolder, type=result.get("type", "input")),
            )

        manifest = await self.manifest()
        if manifest is None:
            raise ComfyError(
                f"Chaining a {kind!r} port to a remote ComfyUI needs the comfyui-webstudio node pack "
                "installed there (only images can be uploaded without it)."
            )
        result = await self.http.webstudio_ingest(data, path.name, run_key=run_key)
        return StagedInput(
            source=result["source"],
            ref=ComfyFileRef(filename=result["name"], subfolder=result["subfolder"], type="input"),
        )

    def artifact_path(self, ref: ComfyFileRef) -> Path | None:
        return None


def _link_or_copy(source: Path, destination: Path) -> None:
    """Cheapest available way to make ``source`` appear at ``destination``."""
    import os
    import shutil

    try:
        os.link(source, destination)
        return
    except OSError:
        pass  # different filesystem, or a filesystem without hardlinks
    try:
        os.symlink(source, destination)
        return
    except OSError:
        pass  # Windows without developer mode, or a policy that forbids symlinks
    shutil.copy2(source, destination)


# -- registry ------------------------------------------------------------------------------------------

_BACKENDS: dict[str, type[ComfyBackend]] = {}


def register_backend(cls: type[ComfyBackend]) -> type[ComfyBackend]:
    _BACKENDS[cls.kind] = cls
    return cls


def create_backend(config: ComfyBackendConfig, *, client_id: str | None = None) -> ComfyBackend:
    """Build the backend for a config, falling back to remote for anything unrecognised.

    Remote is the safe default: it makes no filesystem assumptions, so a misconfigured entry degrades to
    slower rather than broken.
    """
    cls = _BACKENDS.get(config.kind)
    if cls is None:
        logger.warning("Unknown backend kind %r, treating as remote", config.kind)
        cls = RemoteBackend
    if cls is LocalBackend and not config.uses_shared_filesystem:
        logger.warning(
            "Backend %r is marked local but %r is not a readable ComfyUI root; using the remote transport",
            config.name,
            config.comfy_root,
        )
        cls = RemoteBackend
    return cls(config, client_id=client_id)


register_backend(LocalBackend)
register_backend(RemoteBackend)
