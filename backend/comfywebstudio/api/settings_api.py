"""Settings page: application preferences and ComfyUI backend management."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..core.errors import NotFound, ValidationFailed
from ..core.ids import new_id
from ..core.pipeline import Stage
from ..pipeline import sinks
from ..pipeline.builtin import builtin_pipeline
from ..pipeline.resolve import overlay_with, overlay_without, resolve, stage_is_stale
from ..settings import (
    AppSettings,
    ComfyBackendConfig,
    ExecutionSettings,
    LlmProviderConfig,
    PreviewSettings,
    RenderSettings,
    StorySettings,
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
    story: StorySettings | None = None
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

    # A whole sub-model is replaced, not merged — which is a trap for exactly one field. The storyboard
    # pipeline lives on `story` but is edited through its own routes, so a client PATCHing story to change
    # a model would wipe every prompt edit on the install. Carrying it over means the client cannot lose
    # what it never knew it was holding.
    if body.story is not None and body.story.pipeline is None:
        body.story.pipeline = settings.story.pipeline

    # Assign the validated sub-models themselves, not their dumped dicts, so the settings object keeps
    # its types and re-serialises correctly.
    for field in ("cors_origins", "execution", "render", "preview", "ui", "story", "default_backend_id"):
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


# -- language models -------------------------------------------------------------------------------------


class LlmProviderRequest(BaseModel):
    name: str = "Ollama"
    kind: str = "ollama"
    base_url: str = "http://127.0.0.1:11434"
    api_key: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    vision_models: list[str] = Field(default_factory=list)
    enabled: bool = True
    timeout_s: float = 300.0


@router.get("/llm-providers")
def list_llm_providers(state: StateDep) -> list[LlmProviderConfig]:
    return state.settings.llm_providers


@router.post("/llm-providers", status_code=201)
def add_llm_provider(state: StateDep, body: LlmProviderRequest) -> LlmProviderConfig:
    config = LlmProviderConfig(id=new_id("llm", 6), **body.model_dump())
    state.settings.llm_providers.append(config)
    # The first one added becomes the one the storyboard tools use, so a fresh install works after one
    # form rather than after a form and a separate choice.
    if state.settings.story.provider_id is None:
        state.settings.story.provider_id = config.id
    state.save_settings()
    return config


@router.patch("/llm-providers/{provider_id}")
def update_llm_provider(
    state: StateDep, provider_id: str, body: LlmProviderRequest
) -> LlmProviderConfig:
    if not any(p.id == provider_id for p in state.settings.llm_providers):
        raise NotFound(f"No language-model provider {provider_id!r}")
    updated = LlmProviderConfig(id=provider_id, **body.model_dump())
    state.settings.llm_providers = [
        updated if p.id == provider_id else p for p in state.settings.llm_providers
    ]
    state.save_settings()
    return updated


@router.delete("/llm-providers/{provider_id}", status_code=204)
def delete_llm_provider(state: StateDep, provider_id: str) -> None:
    remaining = [p for p in state.settings.llm_providers if p.id != provider_id]
    if len(remaining) == len(state.settings.llm_providers):
        raise NotFound(f"No language-model provider {provider_id!r}")
    state.settings.llm_providers = remaining
    story = state.settings.story
    if story.provider_id == provider_id:
        story.provider_id = remaining[0].id if remaining else None
    if story.vision_provider_id == provider_id:
        story.vision_provider_id = None
    state.save_settings()


@router.get("/llm-providers/{provider_id}/models")
async def list_llm_models(state: StateDep, provider_id: str) -> dict:
    """What this provider can run, and which of those can be given an image.

    The vision flag is what the storyboard's describe step depends on, so it is reported per model rather
    than left to the user to remember — sending a picture to a text-only model produces a confident
    description of nothing.
    """
    from ..llm.provider import LlmError, create_provider

    config = next((p for p in state.settings.llm_providers if p.id == provider_id), None)
    if config is None:
        raise NotFound(f"No language-model provider {provider_id!r}")

    provider = create_provider(config)
    try:
        models = await provider.models()
    except LlmError as exc:
        raise ValidationFailed(str(exc)) from exc
    finally:
        await provider.close()

    return {
        "provider_id": provider_id,
        "models": [m.as_dict() for m in models],
        "vision_available": any(m.vision for m in models),
    }


# -- what is holding the graphics card --------------------------------------------------------------------


def _story_providers(state) -> list:
    """The providers the storyboard actually uses, in order, without repeating one."""
    story = state.settings.story
    wanted = [story.provider_id, story.vision_provider_id]
    seen: list = []
    for provider_id in wanted:
        if provider_id and provider_id not in [p.id for p in seen]:
            config = next(
                (p for p in state.settings.llm_providers if p.id == provider_id and p.enabled), None
            )
            if config is not None:
                seen.append(config)
    return seen


@router.get("/llm-loaded")
async def llm_loaded(state: StateDep) -> dict:
    """Which language models are sitting in memory, and how much of the card they are holding.

    Across the storyboard's providers rather than one, because the writing model and the looking model can
    be different endpoints and both of them take room.
    """
    from ..llm.provider import create_provider

    resident: list[dict] = []
    for config in _story_providers(state):
        provider = create_provider(config)
        try:
            resident += [
                {**model.as_dict(), "provider_id": config.id} for model in await provider.loaded()
            ]
        finally:
            await provider.close()
    return {"models": resident, "vram": sum(m["vram"] for m in resident)}


class UnloadRequest(BaseModel):
    #: One model, or every loaded one when omitted.
    model: str | None = None


@router.post("/llm-unload")
async def llm_unload(state: StateDep, body: UnloadRequest) -> dict:
    """Let go of the loaded models, so something else can have the graphics card.

    The storyboard alternates between a language model and ComfyUI, and they compete for the same memory:
    a 7B model resident is several gigabytes an image model cannot use. Ollama releases on its own after a
    few idle minutes, but "a few minutes" is exactly the wrong amount of time when you are waiting.
    """
    from ..llm.provider import LlmError, create_provider

    freed: list[str] = []
    problems: list[str] = []
    for config in _story_providers(state):
        provider = create_provider(config)
        try:
            freed += await provider.unload(body.model)
        except LlmError as exc:
            problems.append(str(exc))
        finally:
            await provider.close()

    # A provider that cannot free its own memory is worth reporting, but not worth failing over when
    # another one did let go.
    if problems and not freed:
        raise ValidationFailed(" ".join(problems))
    return {"unloaded": freed, "warnings": problems}


# -- the storyboard flow, for everyone using this install ------------------------------------------------


@router.get("/pipeline")
def get_app_pipeline(state: StateDep) -> dict:
    """The defaults every storyboard starts from, and what the built-ins say."""
    pipeline = resolve(state.settings, None)
    edited = (state.settings.story.pipeline.stages if state.settings.story.pipeline else {}) or {}
    return {
        "pipeline": pipeline.model_dump(mode="json"),
        "stages": [
            {
                **stage.model_dump(mode="json"),
                "edited": stage.id in edited,
                "stale": stage_is_stale(stage),
                "writable": sinks.destinations(scope=stage.scope),
            }
            for stage in pipeline.stages
        ],
        "builtin": builtin_pipeline().model_dump(mode="json"),
        "kinds": sorted({s.kind for s in builtin_pipeline().stages} | {Stage().kind}),
    }


@router.put("/pipeline/stages/{stage_id}")
def put_app_stage(state: StateDep, stage_id: str, body: Stage) -> dict:
    """Change a stage for every storyboard that has not overridden it itself."""
    if body.id != stage_id:
        raise ValidationFailed(f"That is step {body.id!r}, but the address says {stage_id!r}.")
    for output in body.outputs:
        sinks.check(output.writes, scope=body.scope)

    state.settings.story.pipeline = overlay_with(state.settings.story.pipeline, body)
    state.save_settings()
    state.events.emit("settings.changed", data={"scope": "pipeline"})
    return get_app_pipeline(state)


@router.delete("/pipeline/stages/{stage_id}")
def reset_app_stage(state: StateDep, stage_id: str) -> dict:
    state.settings.story.pipeline = overlay_without(state.settings.story.pipeline, stage_id)
    state.save_settings()
    state.events.emit("settings.changed", data={"scope": "pipeline"})
    return get_app_pipeline(state)


@router.delete("/pipeline")
def reset_app_pipeline(state: StateDep) -> dict:
    """Back to the built-ins, for everyone. Boards keep whatever they overrode themselves."""
    state.settings.story.pipeline = None
    state.save_settings()
    state.events.emit("settings.changed", data={"scope": "pipeline"})
    return get_app_pipeline(state)


@router.get("/llm-library")
def llm_library() -> list[dict]:
    """Models worth suggesting, for the ones you have not got yet.

    A maintained shelf rather than a catalogue: Ollama publishes no API for browsing its library, so this
    is a hand-picked list — which is exactly why the UI also accepts any name typed in.
    """
    from ..llm.provider import MODEL_LIBRARY

    return [entry.as_dict() for entry in MODEL_LIBRARY]


class PullModelRequest(BaseModel):
    model: str


@router.post("/llm-providers/{provider_id}/pull", status_code=202)
async def pull_llm_model(state: StateDep, provider_id: str, body: PullModelRequest) -> dict:
    """Start downloading a model, and report progress on the event bus.

    Returns immediately: a six-gigabyte download is not something to hold a request open for. Progress
    arrives as ``llm.pull`` events, the same way a run or a render reports itself.
    """
    from ..llm.provider import LlmError, create_provider

    config = next((p for p in state.settings.llm_providers if p.id == provider_id), None)
    if config is None:
        raise NotFound(f"No language-model provider {provider_id!r}")
    model = body.model.strip()
    if not model:
        raise ValidationFailed("Which model?")

    provider = create_provider(config)

    async def run() -> None:
        def report(status: str, fraction: float, done: int, total: int) -> None:
            state.events.emit(
                "llm.pull",
                data={
                    "provider_id": provider_id, "model": model, "status": status,
                    "progress": round(fraction, 4), "done": done, "total": total,
                },
            )

        try:
            await provider.pull(model, on_progress=report)
            state.events.emit(
                "llm.pull",
                data={"provider_id": provider_id, "model": model, "status": "ready",
                      "progress": 1.0, "done": 0, "total": 0, "finished": True},
            )
            logger.info("Pulled language model %s", model)
        except LlmError as exc:
            state.events.emit(
                "llm.pull",
                data={"provider_id": provider_id, "model": model, "status": "failed",
                      "error": str(exc), "finished": True},
            )
            logger.warning("Could not pull %s: %s", model, exc)
        finally:
            await provider.close()

    asyncio.create_task(run(), name=f"llm-pull:{model}")
    return {"started": True, "model": model}
