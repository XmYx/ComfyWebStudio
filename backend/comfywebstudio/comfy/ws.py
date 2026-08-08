"""WebSocket consumer for one ComfyUI instance.

One connection per backend, shared by every run. ComfyUI sends per-node messages only to the client id that
owns the *currently executing* prompt (``execution.py:720-723``), so multiple sockets would fight over the
stream; a single connection demultiplexed on ``prompt_id`` is the correct shape.

The connection reconnects on its own. A dropped socket must not lose a run: callers waiting on a prompt keep
their subscription across reconnects, and the orchestrator reconciles final state against ``/history``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import websockets

from .protocol import ComfyEvent, parse_binary, parse_event

logger = logging.getLogger(__name__)

#: Advertised on connect so ComfyUI sends binary event 4 (preview + node/prompt metadata) instead of the
#: metadata-less event 1, which we could not attribute to a step (``comfy_execution/progress.py:207-228``).
CLIENT_FEATURE_FLAGS = {"supports_preview_metadata": True}

_RECONNECT_DELAYS = (0.5, 1.0, 2.0, 5.0, 10.0, 30.0)

#: Bound each subscriber queue so one slow consumer cannot grow memory without limit.
_QUEUE_MAXSIZE = 512


class _Subscription:
    __slots__ = ("prompt_id", "queue")

    def __init__(self, prompt_id: str | None):
        self.prompt_id = prompt_id
        self.queue: asyncio.Queue[ComfyEvent | None] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)

    def offer(self, event: ComfyEvent) -> None:
        if self.prompt_id is not None and event.prompt_id not in (None, self.prompt_id):
            return
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop the oldest rather than the newest: terminal events matter more than stale progress.
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(event)


class ComfyWsClient:
    """Maintains the socket and fans events out to subscribers."""

    def __init__(self, base_url: str, client_id: str, *, headers: dict[str, str] | None = None):
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self._headers = dict(headers or {})
        self._subs: set[_Subscription] = set()
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._connected = asyncio.Event()
        self.sid: str | None = None
        self.queue_remaining: int = 0
        self.last_error: str | None = None

    # -- lifecycle ---------------------------------------------------------------------------------

    @property
    def ws_url(self) -> str:
        scheme = "wss" if self.base_url.startswith("https") else "ws"
        host = self.base_url.split("://", 1)[-1]
        return f"{scheme}://{host}/ws?clientId={self.client_id}"

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._run(), name=f"comfy-ws:{self.base_url}")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._connected.clear()
        for sub in list(self._subs):
            with contextlib.suppress(asyncio.QueueFull):
                sub.queue.put_nowait(None)

    async def wait_connected(self, timeout: float = 10.0) -> bool:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except TimeoutError:
            return False

    # -- subscription ------------------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def subscribe(self, prompt_id: str | None = None) -> AsyncIterator[AsyncIterator[ComfyEvent]]:
        """Subscribe to events, optionally filtered to one prompt.

        Subscribe *before* posting the prompt: ComfyUI can start executing before the HTTP response lands,
        and an event emitted before the subscription exists is gone for good.
        """
        sub = _Subscription(prompt_id)
        self._subs.add(sub)
        try:
            yield self._drain(sub)
        finally:
            self._subs.discard(sub)

    async def _drain(self, sub: _Subscription) -> AsyncIterator[ComfyEvent]:
        while True:
            event = await sub.queue.get()
            if event is None:  # stop() sentinel
                return
            yield event

    def _dispatch(self, event: ComfyEvent) -> None:
        for sub in list(self._subs):
            sub.offer(event)

    # -- connection loop ---------------------------------------------------------------------------

    async def _run(self) -> None:
        attempt = 0
        while not self._stopping.is_set():
            try:
                await self._connect_once()
                attempt = 0  # a clean session resets the backoff
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any failure means "retry the socket"
                self.last_error = str(exc)
                logger.warning("ComfyUI websocket %s: %s", self.base_url, exc)
            finally:
                self._connected.clear()

            if self._stopping.is_set():
                return
            delay = _RECONNECT_DELAYS[min(attempt, len(_RECONNECT_DELAYS) - 1)]
            attempt += 1
            await asyncio.sleep(delay)

    async def _connect_once(self) -> None:
        async with websockets.connect(
            self.ws_url,
            additional_headers=self._headers or None,
            max_size=None,  # preview frames can be large
            ping_interval=20,
            ping_timeout=20,
        ) as socket:
            self._connected.set()
            self.last_error = None
            logger.info("Connected to ComfyUI websocket at %s", self.base_url)

            await socket.send(json.dumps({"type": "feature_flags", "data": CLIENT_FEATURE_FLAGS}))

            async for frame in socket:
                if self._stopping.is_set():
                    return
                self._handle_frame(frame)

    def _handle_frame(self, frame: Any) -> None:
        if isinstance(frame, bytes | bytearray):
            event = parse_binary(bytes(frame))
            if event is not None:
                self._dispatch(event)
            return

        try:
            message = json.loads(frame)
        except json.JSONDecodeError:
            logger.debug("Ignoring non-JSON websocket text frame")
            return

        if message.get("type") == "feature_flags":
            logger.debug("ComfyUI server features: %s", message.get("data"))
            return

        event = parse_event(message)
        if event.type == "status":
            self.queue_remaining = int(event.data.get("queue_remaining") or 0)
            if event.data.get("sid"):
                self.sid = str(event.data["sid"])
        self._dispatch(event)
