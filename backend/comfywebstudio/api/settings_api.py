"""Settings page: application preferences and ComfyUI backend management."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from ..core.errors import NotFound, ValidationFailed
from ..core.ids import new_id
from ..settings import (
    AppSettings,
    ComfyBackendConfig,
    ExecutionSettings,
    PreviewSettings,
    RenderSettings,
    UISettings,
    corrupt_settings_notices,
)
from .deps import StateDep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])

#: Directory name we create inside ComfyUI/custom_nodes.
NODEPACK_DIR = "comfyui-webstudio"


class UpdateSettingsRequest(BaseModel):
    projects_dir: str | None = None
    cors_origins: list[str] | None = None
    execution: ExecutionSettings | None = None
    render: RenderSettings | None = None
    preview: PreviewSettings | None = None
    ui: UISettings | None = None
    default_backend_id: str | None = None


class BackendRequest(BaseModel):
    name: str = "ComfyUI"
    kind: str = "local"
    base_url: str = "http://127.0.0.1:8188"
    headers: dict[str, str] = {}
    comfy_root: str | None = None
    comfy_user: str = "default"
    enabled: bool = True
    timeout_s: float = 60.0


@router.get("")
def get_settings(state: StateDep) -> AppSettings:
    return state.settings


@router.get("/notices")
def get_notices(state: StateDep) -> dict:
    """Problems worth surfacing on the Settings page rather than only in the log."""
    return {
        "corrupt_settings": corrupt_settings_notices(),
        "no_backends": not state.settings.backends,
        "root": str(state.settings.root),
    }


@router.patch("")
def update_settings(state: StateDep, body: UpdateSettingsRequest) -> AppSettings:
    settings = state.settings

    if body.projects_dir is not None:
        candidate = Path(body.projects_dir).expanduser()
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValidationFailed(f"Cannot use {candidate} as the projects directory: {exc}") from exc
        settings.projects_dir = candidate

    # Assign the validated sub-models themselves, not their dumped dicts, so the settings object keeps
    # its types and re-serialises correctly.
    for field in ("cors_origins", "execution", "render", "preview", "ui", "default_backend_id"):
        value = getattr(body, field)
        if value is not None:
            setattr(settings, field, value)

    settings.ensure_dirs()
    state.save_settings()
    state.events.emit("settings.changed", data={"scope": "app"})
    return settings


# -- backends ------------------------------------------------------------------------------------------


@router.get("/backends")
def list_backends(state: StateDep) -> list[ComfyBackendConfig]:
    return state.settings.backends


@router.post("/backends", status_code=201)
async def add_backend(state: StateDep, body: BackendRequest) -> ComfyBackendConfig:
    config = ComfyBackendConfig(id=new_id("be", 6), **body.model_dump())
    _validate_backend(config)

    state.settings.backends.append(config)
    if state.settings.default_backend_id is None:
        state.settings.default_backend_id = config.id
    state.save_settings()
    await state.backends.invalidate(config.id)
    return config


@router.patch("/backends/{backend_id}")
async def update_backend(state: StateDep, backend_id: str, body: BackendRequest) -> ComfyBackendConfig:
    existing = next((b for b in state.settings.backends if b.id == backend_id), None)
    if existing is None:
        raise NotFound(f"No backend {backend_id!r}")

    updated = ComfyBackendConfig(id=backend_id, **body.model_dump())
    _validate_backend(updated)

    state.settings.backends = [
        updated if b.id == backend_id else b for b in state.settings.backends
    ]
    state.save_settings()
    # Drop the pooled connection so the next request uses the new configuration.
    await state.backends.invalidate(backend_id)
    return updated


@router.delete("/backends/{backend_id}", status_code=204)
async def delete_backend(state: StateDep, backend_id: str) -> None:
    remaining = [b for b in state.settings.backends if b.id != backend_id]
    if len(remaining) == len(state.settings.backends):
        raise NotFound(f"No backend {backend_id!r}")

    state.settings.backends = remaining
    if state.settings.default_backend_id == backend_id:
        state.settings.default_backend_id = remaining[0].id if remaining else None
    state.save_settings()
    await state.backends.invalidate(backend_id)


@router.post("/backends/{backend_id}/test")
async def test_backend(state: StateDep, backend_id: str) -> dict:
    """Probe a backend: reachability, version, GPUs, and whether our node pack is installed."""
    return await state.backends.probe(backend_id)


@router.post("/backends/{backend_id}/install-nodepack")
def install_nodepack(state: StateDep, backend_id: str) -> dict:
    """Symlink the bundled node pack into a local ComfyUI's ``custom_nodes``.

    Only possible for a local backend — a remote instance's filesystem is not ours to write to. ComfyUI
    must be restarted afterwards, because node packs are imported once at startup.
    """
    config = next((b for b in state.settings.backends if b.id == backend_id), None)
    if config is None:
        raise NotFound(f"No backend {backend_id!r}")
    if not config.uses_shared_filesystem:
        raise ValidationFailed(
            "This backend has no readable ComfyUI directory, so the node pack cannot be installed from "
            "here. Copy comfy_nodes/ into that machine's custom_nodes folder instead."
        )

    source = Path(__file__).resolve().parents[3] / "comfy_nodes"
    if not (source / "__init__.py").is_file():
        raise ValidationFailed(f"The bundled node pack is missing from {source}.")

    target = Path(config.comfy_root) / "custom_nodes" / NODEPACK_DIR  # type: ignore[arg-type]
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.is_symlink():
        if target.resolve() == source:
            return {"ok": True, "action": "already_linked", "path": str(target),
                    "restart_required": False}
        target.unlink()
    elif target.exists():
        raise ValidationFailed(
            f"{target} already exists and is not a symlink. Remove or rename it first — refusing to "
            "overwrite something we did not create."
        )

    try:
        os.symlink(source, target, target_is_directory=True)
    except OSError as exc:
        raise ValidationFailed(f"Could not create the symlink: {exc}") from exc

    return {
        "ok": True,
        "action": "linked",
        "path": str(target),
        "restart_required": True,
        "message": "Restart ComfyUI for the node pack to load.",
    }


def _validate_backend(config: ComfyBackendConfig) -> None:
    if config.kind not in {"local", "remote"}:
        raise ValidationFailed(f"Unknown backend kind {config.kind!r}.")
    if not config.base_url.startswith(("http://", "https://")):
        raise ValidationFailed("The base URL must start with http:// or https://.")
    if config.kind == "local" and config.comfy_root:
        root = Path(config.comfy_root)
        if not root.is_dir():
            raise ValidationFailed(f"{root} is not a directory.")
        if not (root / "main.py").is_file():
            raise ValidationFailed(
                f"{root} does not look like a ComfyUI installation (no main.py). Leave the path empty "
                "to talk to it over HTTP only."
            )
