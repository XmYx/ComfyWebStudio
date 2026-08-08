"""Router registry.

Every module in this package exposes a ``router``. Adding an API surface means adding a module and one
entry here — there is no central file full of route declarations to keep in sync.
"""

from __future__ import annotations

from fastapi import APIRouter

from . import (
    bridge,
    events,
    media,
    projects,
    runs,
    settings_api,
    shots,
    timeline,
    workflows,
)

#: Order matters only for documentation grouping.
ROUTERS: list[APIRouter] = [
    projects.router,
    workflows.router,
    shots.router,
    runs.router,
    media.router,
    timeline.router,
    settings_api.router,
    bridge.router,
    events.router,
]

__all__ = ["ROUTERS"]
