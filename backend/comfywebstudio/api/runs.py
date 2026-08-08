"""Running shots and reading run history."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ..core.models import Run, RunMode
from .deps import ProjectDep, ShotDep, StateDep

router = APIRouter(prefix="/api/projects/{project_id}", tags=["runs"])


class StartRunRequest(BaseModel):
    mode: RunMode = "shot"
    #: For ``step`` and ``chain`` modes. Dependencies are pulled in automatically.
    step_ids: list[str] | None = None
    #: Ignore cached results and re-execute.
    force: bool = False


@router.post("/shots/{shot_id}/run", status_code=202)
async def start_run(
    state: StateDep, project: ProjectDep, shot: ShotDep, body: StartRunRequest
) -> Run:
    """Queue a run. Returns immediately; follow progress on ``/api/events``."""
    return await state.orchestrator.start(
        project, shot, mode=body.mode, step_ids=body.step_ids, force=body.force
    )


@router.get("/runs")
def list_runs(
    state: StateDep, project: ProjectDep, shot_id: str | None = None, limit: int = 50
) -> list[Run]:
    """Persisted runs, newest first, merged with anything currently in flight."""
    stored = state.store.list_runs(project.id, shot_id=shot_id, limit=limit)
    active = {run.id: run for run in state.orchestrator.active_runs(project.id)}
    return [active.get(run.id, run) for run in stored]


@router.get("/runs/active")
def list_active_runs(state: StateDep, project: ProjectDep) -> list[Run]:
    return state.orchestrator.active_runs(project.id)


@router.get("/runs/{run_id}")
def get_run(state: StateDep, project: ProjectDep, run_id: str) -> Run:
    return state.orchestrator.get_run(run_id) or state.store.load_run(project.id, run_id)


@router.post("/runs/{run_id}/cancel")
async def cancel_run(state: StateDep, project: ProjectDep, run_id: str) -> dict:
    cancelled = await state.orchestrator.cancel(run_id)
    return {
        "cancelled": cancelled,
        "message": "Cancelling." if cancelled else "That run is not currently active.",
    }


@router.delete("/runs/{run_id}", status_code=204)
def delete_run(state: StateDep, project: ProjectDep, run_id: str) -> None:
    state.store.delete_run(project.id, run_id)


@router.get("/shots/{shot_id}/results")
def latest_results(state: StateDep, project: ProjectDep, shot: ShotDep) -> dict:
    """Most recent successful result per step.

    This is what repopulates every preview when a project is reopened — without it, previews would be
    empty until the user re-ran everything.
    """
    latest = state.store.latest_step_runs(project.id, shot.id)
    return {
        step_id: {
            "run_id": entry["run_id"],
            "step_run": entry["step_run"].model_dump(mode="json"),
        }
        for step_id, entry in latest.items()
    }


@router.post("/cache/clear")
def clear_cache(state: StateDep, project: ProjectDep) -> dict:
    from ..execution.cache import CacheIndex

    CacheIndex(state.store, project.id).clear()
    return {"ok": True}
