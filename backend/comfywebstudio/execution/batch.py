"""Rendering several shots without being watched.

The orchestrator runs one shot. This runs a list of them, **one after another**, in the background.

Sequential rather than all at once, for two reasons. A shot can consume another shot's last result — that
is what a `shot` value node is — so starting them together would let one read the output the other has not
produced yet. And one ComfyUI executes one prompt at a time anyway: firing twenty runs at it buys nothing
but twenty sets of staged inputs sitting in memory waiting their turn.

A shot that fails does **not** stop the queue. Nineteen good shots held hostage by the twentieth is not a
trade anyone would choose, and the per-shot statuses say plainly which one went wrong. The batch itself
ends as ``error`` when any shot did, so nothing has to read the list to know something needs attention.

In the background because rendering twenty shots is not a request anyone can hold open, and driving the
sequence from the browser would mean a closed tab abandons it half-done.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from pydantic import Field

from ..core.base import Base, utcnow
from ..core.errors import Conflict, NotFound, ValidationFailed
from ..core.ids import new_id
from ..core.models import RunStatus

logger = logging.getLogger(__name__)


class QueuedShot(Base):
    """One shot's place in the queue, and how it ended up."""

    shot_id: str
    name: str = ""
    status: RunStatus = "queued"
    #: The orchestrator run this became, once it started. Empty while still waiting.
    run_id: str = ""
    error: str | None = None


class ShotBatch(Base):
    id: str = Field(default_factory=lambda: new_id("batch"))
    project_id: str
    shots: list[QueuedShot] = Field(default_factory=list)
    status: RunStatus = "running"
    force: bool = False
    started: datetime = Field(default_factory=utcnow)
    finished: datetime | None = None

    def entry(self, shot_id: str) -> QueuedShot | None:
        return next((s for s in self.shots if s.shot_id == shot_id), None)

    @property
    def done(self) -> int:
        return sum(1 for s in self.shots if s.status not in {"queued", "running"})


class ShotQueue:
    """Drives a list of shots to the end, announcing itself as it goes.

    One batch per project at a time. A second is refused rather than queued behind the first: two batches
    interleaving over shots that feed each other is not a state anyone can reason about, and the honest
    answer to "run these too" is that the first lot is still going.
    """

    def __init__(self, state):
        self.state = state
        self._batches: dict[str, ShotBatch] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    # -- public API ------------------------------------------------------------------------------------

    def get(self, batch_id: str) -> ShotBatch | None:
        return self._batches.get(batch_id)

    def active(self, project_id: str) -> ShotBatch | None:
        return next(
            (
                b
                for b in self._batches.values()
                if b.project_id == project_id and b.status == "running"
            ),
            None,
        )

    async def start(self, project, shot_ids: list[str], *, force: bool = False) -> ShotBatch:
        if self.active(project.id) is not None:
            raise Conflict("This project is already rendering a queue. Wait for it, or stop it.")

        wanted = list(dict.fromkeys(shot_ids))  # de-duplicated, order kept
        shots = []
        for shot_id in wanted:
            shot = project.shot(shot_id)
            if shot is None:
                raise NotFound(f"No shot {shot_id!r} in this project")
            # A template editing session is not a shot anybody meant to render.
            if shot.template_edit_id:
                continue
            shots.append(shot)

        if not shots:
            raise ValidationFailed("Pick at least one shot to render.")

        batch = ShotBatch(
            project_id=project.id,
            force=force,
            shots=[QueuedShot(shot_id=s.id, name=s.name) for s in shots],
        )
        self._batches[batch.id] = batch
        self.state.events.emit(
            "shots.batch.started",
            project_id=project.id,
            data={
                "batch_id": batch.id,
                "force": force,
                "shots": [{"id": s.id, "name": s.name} for s in shots],
            },
        )
        self._tasks[batch.id] = asyncio.create_task(self._drive(batch), name=f"batch:{batch.id}")
        return batch

    async def cancel(self, batch_id: str) -> bool:
        task = self._tasks.get(batch_id)
        if task is None or task.done():
            return False
        # The shot currently rendering goes too, or "stop" would leave ComfyUI grinding out pictures for
        # a queue that no longer exists.
        batch = self._batches.get(batch_id)
        if batch is not None:
            for entry in batch.shots:
                if entry.status == "running" and entry.run_id:
                    await self.state.orchestrator.cancel(entry.run_id)
        task.cancel()
        return True

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    # -- the loop --------------------------------------------------------------------------------------

    async def _drive(self, batch: ShotBatch) -> None:
        try:
            for entry in batch.shots:
                await self._one(batch, entry)
            batch.status = "error" if any(s.status == "error" for s in batch.shots) else "success"
        except asyncio.CancelledError:
            batch.status = "cancelled"
            for entry in batch.shots:
                if entry.status in {"queued", "running"}:
                    entry.status = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - a batch must always reach a terminal state
            logger.exception("Shot queue %s failed unexpectedly", batch.id)
            batch.status = "error"
            for entry in batch.shots:
                if entry.status in {"queued", "running"}:
                    entry.status, entry.error = "error", str(exc)
        finally:
            batch.finished = utcnow()
            self._tasks.pop(batch.id, None)
            self.state.events.emit(
                "shots.batch.finished",
                project_id=batch.project_id,
                data={
                    "batch_id": batch.id,
                    "status": batch.status,
                    "shots": [s.model_dump(mode="json") for s in batch.shots],
                },
            )

    async def _one(self, batch: ShotBatch, entry: QueuedShot) -> None:
        """Render one shot against a freshly loaded project.

        Reloaded per shot because the previous one has just written its results, and because a queue that
        takes twenty minutes must not hold a `Project` from before the user's last edit. It narrows the
        window rather than closing it — an edit made *during* a shot's own render is still lost.
        """
        project = self.state.store.load(batch.project_id)
        shot = project.shot(entry.shot_id)
        if shot is None:
            entry.status, entry.error = "skipped", "This shot was deleted while the queue was running."
            self._announce(batch, entry)
            return

        entry.status = "running"
        entry.name = shot.name
        self._announce(batch, entry)

        try:
            run = await self.state.orchestrator.start(project, shot, mode="shot", force=batch.force)
        except Exception as exc:  # noqa: BLE001 - a shot that cannot start is a result, not a crash
            entry.status, entry.error = "error", str(exc)
            self._announce(batch, entry)
            return

        entry.run_id = run.id
        finished = await self.state.orchestrator.wait(run.id)
        entry.status = finished.status if finished is not None else "error"
        entry.error = finished.error if finished is not None else "The run disappeared."
        self._announce(batch, entry)

        # Cancelling the shot is how `cancel` stops the queue mid-render, so honour it as a stop rather
        # than carrying blithely on to the next one.
        if entry.status == "cancelled":
            raise asyncio.CancelledError

    def _announce(self, batch: ShotBatch, entry: QueuedShot) -> None:
        self.state.events.emit(
            "shots.batch.progress",
            project_id=batch.project_id,
            data={
                "batch_id": batch.id,
                "shot_id": entry.shot_id,
                "status": entry.status,
                "run_id": entry.run_id,
                "error": entry.error,
                "done": batch.done,
                "total": len(batch.shots),
            },
        )
