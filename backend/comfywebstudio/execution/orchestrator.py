"""Running shots, chains and single steps.

The scheduler walks the shot's DAG and starts any step whose dependencies have finished, up to
``max_concurrent_steps``. That means independent branches run in parallel where the hardware allows, while a
straight chain still runs strictly in order.

"Run this step" means *this step and everything it depends on*: a step whose inputs were never produced
cannot run on its own. Dependencies that already have a valid cached result cost nothing.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import random
from typing import Any

from ..core.errors import ExecutionFailed, NotFound
from ..core.graph import (
    runnable_steps,
    topological_order,
    upstream_closure,
    validate_placed,
    value_nodes_into,
)
from ..core.models import (
    Artifact,
    Project,
    Run,
    RunMode,
    Shot,
    Step,
    StepRun,
    utcnow,
)
from ..core.store import ProjectStore
from ..media.transfer import MediaTransfer
from ..settings import AppSettings
from .cache import CacheIndex
from .events import EventBus
from .runner import PinnedInput, RunContext, StepRunner

logger = logging.getLogger(__name__)


class Orchestrator:
    """Owns in-flight runs for every project."""

    def __init__(
        self,
        store: ProjectStore,
        media: MediaTransfer,
        settings: AppSettings,
        events: EventBus,
        backend_provider,
        template_store=None,
    ):
        self.store = store
        self.media = media
        self.settings = settings
        self.events = events
        #: The shared template library, so a shot's placed templates can be expanded before running.
        self.templates = template_store
        #: ``async (backend_id | None) -> ComfyBackend``
        self.backend_provider = backend_provider
        self._tasks: dict[str, asyncio.Task] = {}
        self._runs: dict[str, Run] = {}
        self._projects: dict[str, str] = {}  # run_id -> project_id

    # -- public API --------------------------------------------------------------------------------

    def active_runs(self, project_id: str | None = None) -> list[Run]:
        return [
            run
            for run_id, run in self._runs.items()
            if project_id is None or self._projects.get(run_id) == project_id
        ]

    def get_run(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    async def start(
        self,
        project: Project,
        shot: Shot,
        *,
        mode: RunMode = "shot",
        step_ids: list[str] | None = None,
        force: bool = False,
    ) -> Run:
        """Queue a run and return immediately; progress arrives on the event bus.

        Placed templates are expanded first, so from here down a shot is nothing but steps and links.
        """
        report, shot = validate_placed(project, shot, self.templates)
        if not report.ok:
            raise ExecutionFailed(
                "This shot cannot run yet.",
                details={"issues": [dataclasses.asdict(i) for i in report.errors]},
            )

        selected = self._select_steps(shot, report.order, mode, step_ids)
        if not selected:
            raise ExecutionFailed("Nothing to run — every selected step is disabled or missing.")

        run = Run(shot_id=shot.id, mode=mode, status="queued")
        run.step_runs = [StepRun(step_id=step.id, status="pending") for step in selected]

        self._runs[run.id] = run
        self._projects[run.id] = project.id
        self.store.save_run(project.id, run)

        self.events.emit(
            "run.started",
            project_id=project.id, run_id=run.id,
            data={
                "shot_id": shot.id,
                "mode": mode,
                "steps": [{"id": s.id, "name": s.name} for s in selected],
            },
        )

        self._tasks[run.id] = asyncio.create_task(
            self._drive(project, shot, run, selected, force=force), name=f"run:{run.id}"
        )
        return run

    async def cancel(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def wait(self, run_id: str) -> Run | None:
        """Block until this run reaches a terminal state, and hand back what it reached.

        There is no completion callback anywhere in this codebase, and this is why there does not need to
        be one: whatever wants to act on a finished run can simply await it. By the time this returns the
        run's history has been written, so a caller reading artifacts off disk will see them.

        Two details it has to get right. A run that has already finished has **removed its own task** —
        ``_drive`` pops it in a ``finally``, before the ``run.finished`` event goes out — so an absent
        task means finished, not unknown, and the run is still in ``_runs`` to be handed back.

        And it waits rather than awaiting the task outright, so a run that was cancelled comes back *as a
        cancelled run* instead of raising into the caller. A waiter that receives someone else's
        ``CancelledError`` looks cancelled itself, which is a hard thing to reason about; a status is not.
        """
        task = self._tasks.get(run_id)
        if task is not None:
            await asyncio.wait([task])
        return self._runs.get(run_id)

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    # -- scheduling --------------------------------------------------------------------------------

    def _select_steps(
        self, shot: Shot, order: list[str], mode: RunMode, step_ids: list[str] | None
    ) -> list[Step]:
        if mode in {"shot", "timeline"} or not step_ids:
            return runnable_steps(shot, order)

        wanted = set(step_ids)
        if mode == "chain":
            # "Run from here": the selected steps plus everything downstream of them.
            downstream = set(wanted)
            changed = True
            while changed:
                changed = False
                for link in shot.links:
                    if link.from_step in downstream and link.to_step not in downstream:
                        downstream.add(link.to_step)
                        changed = True
            wanted = downstream

        # A step cannot run without its inputs, so pull in its dependencies either way.
        needed = upstream_closure(shot, wanted)
        return [s for s in runnable_steps(shot, order) if s.id in needed]

    async def _drive(
        self, project: Project, shot: Shot, run: Run, steps: list[Step], *, force: bool
    ) -> None:
        ctx = RunContext(
            project=project,
            store=self.store,
            media=self.media,
            settings=self.settings,
            events=self.events,
            cache=CacheIndex(self.store, project.id),
            run_id=run.id,
            backend_for=self._backend_for_step,
            rng=random.Random(),
        )
        runner = StepRunner(ctx)

        run.status = "running"
        artifacts: dict[tuple[str, str], Artifact] = {}
        remaining = {step.id: step for step in steps}
        depends_on = self._dependency_map(shot, set(remaining))
        semaphore = asyncio.Semaphore(max(1, self.settings.execution.max_concurrent_steps))
        done: set[str] = set()
        failed: set[str] = set()
        in_flight: dict[str, asyncio.Task] = {}

        async def execute(step: Step) -> StepRun:
            async with semaphore:
                upstream = self._collect_upstream(shot, step, artifacts)
                pinned = self._collect_pinned(project, shot, step)
                return await runner.run(
                    shot, step, upstream=upstream, pinned=pinned, force=force
                )

        try:
            while remaining or in_flight:
                ready = [
                    step
                    for step_id, step in remaining.items()
                    if depends_on[step_id] <= done and step_id not in in_flight
                ]

                # Anything still waiting on a failed dependency can never run.
                if not ready and not in_flight:
                    for step_id in list(remaining):
                        blocked = depends_on[step_id] & failed
                        step_run = run.step_run(step_id) or StepRun(step_id=step_id)
                        step_run.status = "skipped"
                        step_run.error = (
                            "Skipped: an upstream step failed."
                            if blocked
                            else "Skipped: its dependencies never completed."
                        )
                        self._merge(run, step_run)
                        remaining.pop(step_id, None)
                    break

                for step in ready:
                    remaining.pop(step.id)
                    in_flight[step.id] = asyncio.create_task(execute(step), name=f"step:{step.id}")

                if not in_flight:
                    continue

                finished, _ = await asyncio.wait(
                    in_flight.values(), return_when=asyncio.FIRST_COMPLETED
                )
                for task in finished:
                    step_id = next(sid for sid, t in in_flight.items() if t is task)
                    in_flight.pop(step_id)
                    step_run = task.result()
                    self._merge(run, step_run)

                    if step_run.status in {"success", "cached"}:
                        done.add(step_id)
                        for artifact in step_run.outputs:
                            artifacts[(step_id, artifact.port_key)] = artifact
                    else:
                        failed.add(step_id)

                    self.store.save_run(project.id, run)

            run.status = "error" if failed else "success"
            if failed:
                names = [
                    (shot.step(sid).name if shot.step(sid) else sid) for sid in sorted(failed)
                ]
                run.error = "These steps failed: " + ", ".join(names)

        except asyncio.CancelledError:
            run.status = "cancelled"
            run.error = "Cancelled"
            for task in in_flight.values():
                task.cancel()
            await asyncio.gather(*in_flight.values(), return_exceptions=True)
            for step_id in list(remaining) + list(in_flight):
                step_run = run.step_run(step_id) or StepRun(step_id=step_id)
                if step_run.status not in {"success", "cached", "error"}:
                    step_run.status = "cancelled"
                    self._merge(run, step_run)
            self.events.emit(
                "run.cancelled", project_id=project.id, run_id=run.id,
                data={"shot_id": shot.id},
            )
            raise
        except Exception as exc:  # noqa: BLE001 - a run must always reach a terminal state
            logger.exception("Run %s failed unexpectedly", run.id)
            run.status = "error"
            run.error = str(exc)
        finally:
            run.finished = utcnow()
            self.store.save_run(project.id, run)
            self._tasks.pop(run.id, None)
            self.events.emit(
                "run.finished",
                project_id=project.id, run_id=run.id,
                # The shot id rides along so a UI watching several runs can tell which one just ended.
                data={"status": run.status, "error": run.error, "shot_id": shot.id},
            )

    def _dependency_map(self, shot: Shot, included: set[str]) -> dict[str, set[str]]:
        deps: dict[str, set[str]] = {step_id: set() for step_id in included}
        for link in shot.links:
            if link.to_step in included and link.from_step in included:
                deps[link.to_step].add(link.from_step)
        return deps

    def _collect_upstream(
        self, shot: Shot, step: Step, artifacts: dict[tuple[str, str], Artifact]
    ) -> dict[str, Artifact]:
        """Artifacts feeding this step, keyed by *its* input port name."""
        upstream: dict[str, Artifact] = {}
        for link in shot.links_into(step.id):
            artifact = artifacts.get((link.from_step, link.from_port))
            if artifact is not None:
                upstream[link.to_port] = artifact
        return upstream

    def _collect_pinned(
        self, project: Project, shot: Shot, step: Step
    ) -> dict[str, PinnedInput]:
        """Values feeding this step from canvas value nodes, keyed by *its* input port name."""
        pinned: dict[str, PinnedInput] = {}
        for port_key, node in value_nodes_into(shot, step.id).items():
            pinned[port_key] = PinnedInput(
                kind=node.output_kind(project),
                scalar=node.value,
                asset=project.assets.get(node.asset_id or ""),
                artifact=self._shot_output(project, node),
                label=node.display_name,
            )
        return pinned

    def _shot_output(self, project: Project, node) -> Artifact | None:
        """What a dropped shot node supplies: that shot's most recent result on the chosen port.

        Read rather than produced — a dropped shot is a *source*, so it never causes the shot behind it to
        run. If it has no result yet the step that consumes it fails with that reason, which is the honest
        outcome: the fix is to run the source, and nobody wants a canvas that quietly starts GPU work.
        """
        if node.kind != "shot" or not node.source_shot_id or not node.source_port:
            return None
        latest = self.store.latest_step_runs(project.id, node.source_shot_id)
        # Newest first, so a port produced by several steps resolves to the most recent one.
        for entry in latest.values():
            artifact = next(
                (a for a in entry["step_run"].outputs if a.port_key == node.source_port), None
            )
            if artifact is not None:
                return artifact
        return None

    def _merge(self, run: Run, step_run: StepRun) -> None:
        for index, existing in enumerate(run.step_runs):
            if existing.step_id == step_run.step_id:
                run.step_runs[index] = step_run
                return
        run.step_runs.append(step_run)

    async def _backend_for_step(self, step: Step) -> Any:
        return await self.backend_provider(step.backend_id)


def resolve_shot(project: Project, shot_id: str) -> Shot:
    shot = project.shot(shot_id)
    if shot is None:
        raise NotFound(f"No shot {shot_id!r} in this project")
    return shot


def default_order(project: Project, shot: Shot) -> list[str]:
    return topological_order(shot)
