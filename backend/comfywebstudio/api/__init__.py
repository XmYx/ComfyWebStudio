"""Router registry.

Every module in this package exposes a ``router``. Adding an API surface means adding a module and one
entry here — there is no central file full of route declarations to keep in sync.
"""

from __future__ import annotations

from fastapi import APIRouter

from . import (
    bridge,
    comfy_browse,
    events,
    media,
    plugins,
    projects,
    runs,
    settings_api,
    shots,
    templates,
    timeline,
    versions,
    workflows,
)

#: Order matters only for documentation grouping.
ROUTERS: list[APIRouter] = [
    projects.router,
    workflows.router,
    comfy_browse.router,
    shots.router,
    templates.library,
    templates.router,
    runs.router,
    media.router,
    timeline.router,
    settings_api.router,
    plugins.router,
    versions.router,
    bridge.router,
    events.router,
]

__all__ = ["ROUTERS"]
