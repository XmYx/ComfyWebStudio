"""Shared dependencies for the routers."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from ..core.models import Project, Shot, Step
from ..core.store import ProjectStore
from ..state import AppState


def get_state(request: Request) -> AppState:
    return request.app.state.studio  # type: ignore[no-any-return]


StateDep = Annotated[AppState, Depends(get_state)]


def get_store(state: StateDep) -> ProjectStore:
    return state.store


StoreDep = Annotated[ProjectStore, Depends(get_store)]


def load_project(state: StateDep, project_id: str) -> Project:
    """Load a project by path parameter. Raises NotFound, which the error handler maps to 404."""
    return state.store.load(project_id)


ProjectDep = Annotated[Project, Depends(load_project)]


def load_shot(project: ProjectDep, shot_id: str) -> Shot:
    from ..core.errors import NotFound

    shot = project.shot(shot_id)
    if shot is None:
        raise NotFound(f"No shot {shot_id!r} in this project")
    return shot


ShotDep = Annotated[Shot, Depends(load_shot)]


def find_step(project: Project, step_id: str) -> tuple[Shot, Step]:
    from ..core.errors import NotFound

    found = project.find_step(step_id)
    if found is None:
        raise NotFound(f"No step {step_id!r} in this project")
    return found
