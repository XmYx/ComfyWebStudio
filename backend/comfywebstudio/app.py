"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import PROTOCOL_VERSION, __version__
from .api import ROUTERS
from .comfy.http import ComfyError
from .core.errors import StudioError
from .settings import AppSettings
from .state import AppState

logger = logging.getLogger(__name__)

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def create_app(settings: AppSettings | None = None) -> FastAPI:
    state = AppState(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await state.startup()
        try:
            yield
        finally:
            await state.shutdown()

    app = FastAPI(
        title="ComfyWebStudio",
        version=__version__,
        description="Shot-based orchestration for chained ComfyUI workflows.",
        lifespan=lifespan,
    )
    app.state.studio = state

    # The ComfyUI origin must be allowed: our bridge extension runs inside ComfyUI's page and posts saved
    # workflows back to this server.
    origins = set(state.settings.cors_origins)
    for backend in state.settings.backends:
        origins.add(backend.base_url)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    for router in ROUTERS:
        app.include_router(router)

    _register_error_handlers(app)
    _register_meta_routes(app, state)
    _mount_frontend(app)
    return app


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StudioError)
    async def studio_error(_request: Request, exc: StudioError) -> JSONResponse:
        # Expected, actionable failures: return the structured payload the UI knows how to display.
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.exception_handler(ComfyError)
    async def comfy_error(_request: Request, exc: ComfyError) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content={
                "code": "comfy_error",
                "message": str(exc),
                "details": {"status": exc.status},
            },
        )

    @app.exception_handler(FileNotFoundError)
    async def missing_file(_request: Request, exc: FileNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"code": "not_found", "message": str(exc)})


def _register_meta_routes(app: FastAPI, state: AppState) -> None:
    @app.get("/api/health", tags=["meta"])
    async def health() -> dict:
        return {
            "ok": True,
            "version": __version__,
            "protocol": PROTOCOL_VERSION,
            "root": str(state.settings.root),
            "backends": [
                {
                    "id": b.id,
                    "name": b.name,
                    "kind": b.kind,
                    "base_url": b.base_url,
                    "enabled": b.enabled,
                    "shared_filesystem": b.uses_shared_filesystem,
                }
                for b in state.settings.backends
            ],
            "event_subscribers": state.events.subscriber_count,
        }


def _mount_frontend(app: FastAPI) -> None:
    """Serve the built frontend when it exists.

    In development the Vite dev server proxies to this API instead, so a missing ``dist`` is normal and
    must not be an error.
    """
    if not (FRONTEND_DIST / "index.html").is_file():
        logger.info("No frontend build at %s; serving the API only.", FRONTEND_DIST)

        @app.get("/", include_in_schema=False)
        async def no_frontend() -> dict:
            return {
                "message": "ComfyWebStudio API is running. Build the frontend with `make build`, "
                "or run `make dev` for the Vite dev server.",
                "docs": "/docs",
            }

        return

    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> FileResponse:
        # Client-side routing: anything that is not an API path resolves to the app shell.
        candidate = FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")
