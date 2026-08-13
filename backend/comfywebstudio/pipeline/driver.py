"""Running the whole flow, unattended.

One stage at a time, in order, in the background. It has to be in the background because a `comfy` stage
takes minutes: holding an HTTP request open for a twenty-frame draw means a reload orphans the work, and
driving the sequence from the browser would put the flow — the very thing this feature made into data —
back into the client.

Three things make it safe to leave running:

**It waits properly.** `Orchestrator.wait` is the whole completion mechanism; by the time it returns the
run's history is on disk, so the next stage's `current()` sees the pictures with nothing else to arrange.

**It reloads before every stage and saves after.** A task holding one `Project` object across a five-minute
draw would write back over every frame edit made meanwhile. Reloading per stage narrows that window to one
stage — it does not close it, and it would be dishonest to imply otherwise.

**One at a time per board.** Two runs interleaving over the same frames is not a state anyone can reason
about, so the second is refused rather than queued.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from ..core.errors import Conflict, NotFound, ValidationFailed
from ..core.pipeline import PipelineRun
from .frames import slot_workflow
from .resolve import resolve
from .runner import StageContext, run_stage

logger = logging.getLogger(__name__)


class PipelineRunner:
    """Drives a storyboard's stages to the end, reporting itself as it goes."""

    def __init__(self, state):
        self.state = state
        self._runs: dict[str, PipelineRun] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # -- public API ------------------------------------------------------------------------------------

    def get(self, pipeline_run_id: str) -> PipelineRun | None:
        return self._runs.get(pipeline_run_id)

    def active(self, board_id: str) -> PipelineRun | None:
        return next(
            (r for r in self._runs.values() if r.board_id == board_id and r.status == "running"),
            None,
        )

    async def start(
        self,
        project,
        board,
        *,
        stage_ids: list[str] | None = None,
        frame_ids: list[str] | None = None,
    ) -> PipelineRun:
        if self.active(board.id) is not None:
            raise Conflict(
                "This storyboard is already running its flow. Wait for it, or stop it first."
            )

        pipeline = resolve(self.state.settings, board)
        wanted = set(stage_ids or [])
        stages = [s for s in pipeline.stages if s.enabled and (not wanted or s.id in wanted)]
        if not stages:
            raise ValidationFailed(
                "None of those steps are on this storyboard." if wanted
                else "Every step is switched off, so there is nothing to run."
            )

        run = PipelineRun(
            project_id=project.id,
            board_id=board.id,
            stage_ids=[s.id for s in stages],
            frame_ids=list(frame_ids or []),
        )
        self._runs[run.id] = run
        self.state.events.emit(
            "storyboard.pipeline.started",
            project_id=project.id,
            data={
                "board_id": board.id,
                "pipeline_run_id": run.id,
                "stages": [{"id": s.id, "name": s.name or s.id, "kind": s.kind} for s in stages],
                "frames": len(run.frame_ids),
            },
        )
        self._tasks[run.id] = asyncio.create_task(
            self._drive(run, [s.id for s in stages]), name=f"pipeline:{run.id}"
        )
        return run

    async def cancel(self, pipeline_run_id: str) -> bool:
        task = self._tasks.get(pipeline_run_id)
        if task is None or task.done():
            return False
        # Whatever draw it is waiting on goes too, otherwise "stop" leaves ComfyUI grinding away on
        # pictures nobody is going to look at.
        run = self._runs.get(pipeline_run_id)
        if run is not None and run.stage_id:
            for orchestrator_run in self.state.orchestrator.active_runs(run.project_id):
                await self.state.orchestrator.cancel(orchestrator_run.id)
        task.cancel()
        return True

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    # -- the loop --------------------------------------------------------------------------------------

    async def _drive(self, run: PipelineRun, stage_ids: list[str]) -> None:
        lock = self._locks.setdefault(run.board_id, asyncio.Lock())
        try:
            async with lock:
                for stage_id in stage_ids:
                    await self._one(run, stage_id)
                    if run.status != "running":
                        break
            if run.status == "running":
                run.status = "success"
        except asyncio.CancelledError:
            run.status = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - a pipeline run must always reach a terminal state
            logger.exception("Pipeline run %s failed unexpectedly", run.id)
            run.status, run.error = "error", str(exc)
        finally:
            run.finished = datetime.now(UTC)
            run.stage_id = ""
            self._tasks.pop(run.id, None)
            self.state.events.emit(
                "storyboard.pipeline.finished",
                project_id=run.project_id,
                data={
                    "board_id": run.board_id,
                    "pipeline_run_id": run.id,
                    "status": run.status,
                    "done": run.done,
                    "error": run.error,
                },
            )

    async def _one(self, run: PipelineRun, stage_id: str) -> None:
        """Run one stage against a freshly loaded project, and save what it did."""
        project = self.state.store.load(run.project_id)
        board = next((b for b in project.storyboards if b.id == run.board_id), None)
        if board is None:
            raise NotFound(f"No storyboard {run.board_id!r}")

        stage = resolve(self.state.settings, board).stage(stage_id)
        if stage is None or not stage.enabled:
            return

        run.stage_id = stage_id
        self.state.events.emit(
            "storyboard.stage.started",
            project_id=run.project_id,
            data={
                "board_id": board.id, "pipeline_run_id": run.id, "stage_id": stage_id,
                "name": stage.name or stage_id, "kind": stage.kind,
            },
        )

        bound = slot_workflow(project, board, stage)
        if bound is not None:
            await self.state.sync_workflow(project, bound)

        ctx = StageContext(
            state=self.state, project=project, board=board, pipeline_run_id=run.id
        )
        status, error = "success", None
        try:
            result = await run_stage(ctx, stage, frame_ids=run.frame_ids or None)
            if result.run_id:
                # The pipeline task *is* the completion hook. Nothing else needs to observe the draw.
                finished = await self.state.orchestrator.wait(result.run_id)
                if finished is not None and finished.status in {"error", "cancelled"}:
                    status = finished.status
                    error = finished.error or f"The drawing run was {finished.status}."
                # Reload: the orchestrator saved the project itself while this was waiting.
                project = self.state.store.load(run.project_id)
        except ValidationFailed as exc:
            status, error = "error", str(exc)
        finally:
            await ctx.close()

        # A stage that half-worked still wrote something worth keeping, so this saves either way.
        self.state.store.save(project)

        if status == "success":
            run.done.append(stage_id)
        else:
            run.status = "error" if status == "error" else "cancelled"
            run.error = error

        self.state.events.emit(
            "storyboard.stage.finished",
            project_id=run.project_id,
            data={
                "board_id": board.id, "pipeline_run_id": run.id, "stage_id": stage_id,
                "status": status, "error": error,
            },
        )
