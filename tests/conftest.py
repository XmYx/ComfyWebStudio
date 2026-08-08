from __future__ import annotations

import pytest

from comfywebstudio.core.models import (
    Link,
    ParamSpec,
    PortSpec,
    Project,
    Shot,
    Step,
    WorkflowRef,
)
from comfywebstudio.core.store import ProjectStore
from comfywebstudio.settings import AppSettings, ComfyBackendConfig


@pytest.fixture
def settings(tmp_path) -> AppSettings:
    s = AppSettings(root=tmp_path / "cws")
    s.backends = [
        ComfyBackendConfig(
            id="local",
            name="Local",
            kind="local",
            base_url="http://127.0.0.1:8188",
            comfy_root=str(tmp_path / "comfy"),
        )
    ]
    s.default_backend_id = "local"
    s.ensure_dirs()
    return s


@pytest.fixture
def store(settings) -> ProjectStore:
    return ProjectStore(settings)


def make_workflow(name: str, *, inputs=(), outputs=(), params=()) -> WorkflowRef:
    """A workflow whose ports are declared directly, so graph tests need no ComfyUI."""
    ports = [
        PortSpec(key=key, direction="in", kind=kind, node_id=f"in_{key}") for key, kind in inputs
    ] + [PortSpec(key=key, direction="out", kind=kind, node_id=f"out_{key}") for key, kind in outputs]
    param_specs = [
        ParamSpec(key=key, kind=kind, default=default, node_id=f"p_{key}", input_name="value")
        for key, kind, default in params
    ]
    return WorkflowRef(name=name, ports=ports, params=param_specs)


@pytest.fixture
def project(store) -> Project:
    """A two-step chain: generate -> upscale, wired image to image."""
    proj = store.create("Demo")

    gen = make_workflow(
        "Generate",
        outputs=[("image", "image")],
        params=[("prompt", "string", "a cat"), ("seed", "int", 0)],
    )
    ups = make_workflow("Upscale", inputs=[("image", "image")], outputs=[("image", "image")])
    proj.workflows = {gen.id: gen, ups.id: ups}

    step_a = Step(name="Generate", workflow_id=gen.id)
    step_b = Step(name="Upscale", workflow_id=ups.id)
    shot = Shot(
        name="Shot 1",
        steps=[step_a, step_b],
        links=[Link(from_step=step_a.id, from_port="image", to_step=step_b.id, to_port="image")],
    )
    proj.shots = [shot]
    store.save(proj)
    return proj


# -- fake ComfyUI --------------------------------------------------------------------------------------


@pytest.fixture
async def fake_comfy(tmp_path):
    """A running fake ComfyUI, wired into `settings` as the default backend."""
    from .fixtures.fake_comfy import FakeComfy

    server = await FakeComfy(root=tmp_path / "fakecomfy").start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
async def app_state(settings, fake_comfy):
    """AppState pointed at the fake ComfyUI, treated as a local (shared filesystem) backend."""
    from comfywebstudio.state import AppState

    settings.backends = [
        ComfyBackendConfig(
            id="local",
            name="Fake",
            kind="local",
            base_url=fake_comfy.base_url,
            comfy_root=str(fake_comfy.root),
        )
    ]
    settings.default_backend_id = "local"

    state = AppState(settings)
    try:
        yield state
    finally:
        await state.shutdown()


@pytest.fixture
async def client(settings, fake_comfy):
    """The real FastAPI app, wired to the fake ComfyUI, driven over ASGI."""
    import httpx
    from asgi_lifespan import LifespanManager

    from comfywebstudio.app import create_app

    settings.backends = [
        ComfyBackendConfig(
            id="local",
            name="Fake",
            kind="local",
            base_url=fake_comfy.base_url,
            comfy_root=str(fake_comfy.root),
        )
    ]
    settings.default_backend_id = "local"

    app = create_app(settings)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            http.app_state = app.state.studio  # type: ignore[attr-defined]
            yield http
