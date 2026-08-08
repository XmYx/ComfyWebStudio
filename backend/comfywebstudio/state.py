"""Application state — the object graph every request handler works against.

Held once per process and attached to the FastAPI app. It owns the long-lived things: the project store, the
media pipeline, the event bus, the orchestrator, and one live backend connection per configured ComfyUI.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .comfy.backend import ComfyBackend, create_backend
from .comfy.objectinfo import ObjectInfoCache
from .core.errors import BackendUnavailable
from .core.store import ProjectStore
from .execution.events import EventBus
from .execution.orchestrator import Orchestrator
from .media.store import MediaStore
from .media.transfer import MediaTransfer
from .settings import AppSettings, load_settings, save_settings

logger = logging.getLogger(__name__)


class BackendManager:
    """Keeps one connected :class:`ComfyBackend` per configured instance.

    Connections are created lazily and reused: a websocket per run would fight over ComfyUI's single
    ``client_id`` slot, and reconnecting per request would lose events.
    """

    def __init__(self, settings: AppSettings, events: EventBus):
        self.settings = settings
        self.events = events
        self._backends: dict[str, ComfyBackend] = {}
        self._object_info: dict[str, ObjectInfoCache] = {}
        self._lock = asyncio.Lock()

    async def get(self, backend_id: str | None = None) -> ComfyBackend:
        config = self.settings.backend(backend_id)
        if config is None:
            raise BackendUnavailable(
                "No ComfyUI backend is configured. Add one on the Settings page."
            )
        if not config.enabled:
            raise BackendUnavailable(f"Backend {config.name!r} is disabled.")

        async with self._lock:
            backend = self._backends.get(config.id)
            if backend is None or backend.config != config:
                if backend is not None:
                    await backend.close()
                backend = create_backend(config)
                await backend.start()
                self._backends[config.id] = backend
                self._object_info.pop(config.id, None)
                logger.info("Opened backend %s (%s)", config.name, config.base_url)
            return backend

    async def object_info(self, backend_id: str | None = None) -> ObjectInfoCache:
        backend = await self.get(backend_id)
        cache = self._object_info.get(backend.config.id)
        if cache is None:
            cache = ObjectInfoCache(backend.http)
            self._object_info[backend.config.id] = cache
        return cache

    async def probe(self, backend_id: str) -> dict[str, Any]:
        """Test one backend without disturbing the pool if it is unreachable."""
        config = self.settings.backend(backend_id)
        if config is None:
            raise BackendUnavailable(f"No backend {backend_id!r}")
        backend = create_backend(config)
        try:
            status = await backend.status()
            return {
                "id": config.id,
                "name": config.name,
                "reachable": status.reachable,
                "error": status.error,
                "comfyui_version": status.comfyui_version,
                "devices": status.devices,
                "node_pack": status.node_pack,
                "protocol_ok": status.protocol_ok,
                "shared_filesystem": config.uses_shared_filesystem,
            }
        finally:
            await backend.close()

    async def invalidate(self, backend_id: str | None = None) -> None:
        """Drop cached connections after a settings change so the new config takes effect."""
        async with self._lock:
            targets = [backend_id] if backend_id else list(self._backends)
            for target in targets:
                backend = self._backends.pop(target, None)
                self._object_info.pop(target, None)
                if backend is not None:
                    await backend.close()

    async def close(self) -> None:
        await self.invalidate()


class AppState:
    def __init__(self, settings: AppSettings | None = None):
        self.settings = settings or load_settings()
        self.settings.ensure_dirs()

        self.events = EventBus()
        self.store = ProjectStore(self.settings)
        self.media_store = MediaStore(self.store)
        self.media = MediaTransfer(self.store, self.media_store, self.settings.preview)
        self.backends = BackendManager(self.settings, self.events)
        self.orchestrator = Orchestrator(
            self.store, self.media, self.settings, self.events, self.backends.get
        )
        #: Tokens handed to the ComfyUI bridge extension, mapped to the step they may write to.
        self.bridge_tokens: dict[str, dict[str, str]] = {}

    async def startup(self) -> None:
        logger.info("ComfyWebStudio starting; state root %s", self.settings.root)
        for config in self.settings.backends:
            if not config.enabled:
                continue
            try:
                await self.backends.get(config.id)
            except Exception as exc:  # noqa: BLE001 - an unreachable backend must not block startup
                logger.warning("Backend %s not available at startup: %s", config.name, exc)

    async def shutdown(self) -> None:
        await self.orchestrator.shutdown()
        await self.backends.close()
        await self.events.close()

    def save_settings(self) -> None:
        save_settings(self.settings)
