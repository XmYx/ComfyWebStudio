"""Application event bus.

One stream carries everything the UI needs to stay live — run progress, step results, backend health,
workflow syncs — so the frontend opens a single websocket instead of polling.

Subscribers are bounded and lossy by design: a browser tab that stops reading must never be able to stall a
render. Progress events are droppable; lifecycle events are not, so they are kept when the queue overflows.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_QUEUE_MAXSIZE = 256

#: Events that must survive queue pressure — losing one would leave the UI showing a stale state forever.
CRITICAL_EVENTS = frozenset(
    {
        "run.started",
        "run.finished",
        "run.cancelled",
        "step.started",
        "step.finished",
        "step.failed",
        "workflow.synced",
        "project.changed",
        "render.finished",
    }
)


@dataclass(slots=True)
class StudioEvent:
    type: str
    project_id: str | None = None
    run_id: str | None = None
    step_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "data": self.data,
            "ts": self.ts,
        }


class _Subscriber:
    __slots__ = ("queue", "project_id", "dropped")

    def __init__(self, project_id: str | None):
        self.queue: asyncio.Queue[StudioEvent | None] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self.project_id = project_id
        self.dropped = 0

    def offer(self, event: StudioEvent) -> None:
        if self.project_id is not None and event.project_id not in (None, self.project_id):
            return
        try:
            self.queue.put_nowait(event)
            return
        except asyncio.QueueFull:
            pass

        # Full: shed a droppable event to make room for this one, rather than blocking the producer.
        if event.type in CRITICAL_EVENTS:
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(event)
        self.dropped += 1


class EventBus:
    """Fan-out of :class:`StudioEvent` to every connected client."""

    def __init__(self) -> None:
        self._subscribers: set[_Subscriber] = set()

    def publish(self, event: StudioEvent) -> None:
        for subscriber in list(self._subscribers):
            subscriber.offer(event)

    def emit(self, type_: str, **kwargs: Any) -> None:
        self.publish(StudioEvent(type=type_, **kwargs))

    @contextlib.asynccontextmanager
    async def subscribe(self, project_id: str | None = None) -> AsyncIterator[AsyncIterator[StudioEvent]]:
        subscriber = _Subscriber(project_id)
        self._subscribers.add(subscriber)
        try:
            yield self._drain(subscriber)
        finally:
            self._subscribers.discard(subscriber)
            if subscriber.dropped:
                logger.debug("Event subscriber dropped %d events", subscriber.dropped)

    async def _drain(self, subscriber: _Subscriber) -> AsyncIterator[StudioEvent]:
        while True:
            event = await subscriber.queue.get()
            if event is None:
                return
            yield event

    async def close(self) -> None:
        for subscriber in list(self._subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                subscriber.queue.put_nowait(None)
        self._subscribers.clear()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
