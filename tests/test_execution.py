"""Orchestrator tests, driven entirely against the fake ComfyUI.

These cover the behaviour that actually matters in use: a chain passes real files between workflows, a
failure stops what depends on it without killing the rest, cancellation and timeouts terminate cleanly, and
the cache does not lie.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import pytest

from comfywebstudio.comfy.discovery import discover, prompt_hash
from comfywebstudio.core.errors import ExecutionFailed
from comfywebstudio.core.models import (
    VALUE_PORT,
    Asset,
    Link,
    Project,
    Shot,
    Step,
    ValueNode,
    WorkflowRef,
)

# -- workflow builders ---------------------------------------------------------------------------------


def generator_prompt(text: str = "a cat") -> dict:
    """A workflow that takes a text parameter and emits an image plus the text it used."""
    return {
        "1": {"class_type": "WSStringInput", "inputs": {"port_name": "prompt", "value": text}},
        "2": {"class_type": "EmptyImage", "inputs": {"width": 16, "height": 16}},
        "3": {
            "class_type": "WSImageOutput",
            "inputs": {"image": ["2", 0], "port_name": "image", "format": "png", "run_key": ""},
        },
        "4": {
            "class_type": "WSTextOutput",
            "inputs": {"text": ["1", 0], "port_name": "caption", "run_key": ""},
        },
    }


def consumer_prompt() -> dict:
    """A workflow that consumes an image and a text and emits a new image."""
    return {
        "1": {"class_type": "WSImageInput", "inputs": {"port_name": "image", "source": ""}},
        "2": {"class_type": "WSStringInput", "inputs": {"port_name": "caption", "value": "default"}},
        "3": {
            "class_type": "WSImageOutput",
            "inputs": {"image": ["1", 0], "port_name": "final", "format": "png", "run_key": ""},
        },
        "4": {
            "class_type": "WSTextOutput",
            "inputs": {"text": ["2", 0], "port_name": "echo", "run_key": ""},
        },
    }


def register(state, project: Project, name: str, prompt: dict) -> WorkflowRef:
    result = discover(prompt)
    workflow = WorkflowRef(
        name=name, ports=result.ports, params=result.params, hash=prompt_hash(prompt)
    )
    project.workflows[workflow.id] = workflow
    state.store.write_workflow(project.id, workflow.id, "api", prompt)
    state.store.write_workflow(project.id, workflow.id, "ui", {"nodes": [], "links": []})
    return workflow


@pytest.fixture
def chain_project(app_state):
    """Generate -> Consume, chained on both an image port and a text port."""
    project = app_state.store.create("Chain")
    gen = register(app_state, project, "Generate", generator_prompt())
    con = register(app_state, project, "Consume", consumer_prompt())

    step_a = Step(name="Generate", workflow_id=gen.id)
    step_b = Step(name="Consume", workflow_id=con.id)
    shot = Shot(
        name="Shot 1",
        steps=[step_a, step_b],
        links=[
            Link(from_step=step_a.id, from_port="image", to_step=step_b.id, to_port="image"),
            Link(from_step=step_a.id, from_port="caption", to_step=step_b.id, to_port="caption"),
        ],
    )
    project.shots = [shot]
    app_state.store.save(project)
    return project


async def run_to_completion(state, project, shot, *, timeout=30.0, **kwargs):
    run = await state.orchestrator.start(project, shot, **kwargs)
    task = state.orchestrator._tasks[run.id]
    await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    return run


# -- happy path ----------------------------------------------------------------------------------------


async def test_chain_runs_and_passes_real_files_between_workflows(app_state, chain_project, fake_comfy):
    shot = chain_project.shots[0]
    run = await run_to_completion(app_state, chain_project, shot)

    assert run.status == "success", run.error
    assert [sr.status for sr in run.step_runs] == ["success", "success"]

    generate, consume = run.step_runs
    assert {a.port_key for a in generate.outputs} == {"image", "caption"}
    assert {a.port_key for a in consume.outputs} == {"final", "echo"}

    # The image the second step consumed is the file the first step produced.
    submitted = fake_comfy.submitted[1]["prompt"]
    staged_source = submitted["1"]["inputs"]["source"]
    upstream = app_state.media_store.path(
        chain_project.id, generate.output("image").path
    )
    assert staged_source == str(upstream), "the chained image was not the upstream artifact"

    # The chained text overrode the consumer's own default.
    assert submitted["2"]["inputs"]["value"] == "a cat"
    echo = consume.output("echo")
    assert echo.meta["value"] == "a cat"


async def test_artifacts_are_ingested_into_the_project_with_thumbnails(app_state, chain_project):
    run = await run_to_completion(app_state, chain_project, chain_project.shots[0])
    image = run.step_runs[0].output("image")

    path = app_state.media_store.path(chain_project.id, image.path)
    assert path.is_file()
    assert path.is_relative_to(app_state.store.project_dir(chain_project.id))
    assert image.sha256 and len(image.sha256) == 64
    assert image.thumb, "no thumbnail was generated"
    assert app_state.media_store.path(chain_project.id, image.thumb).is_file()
    assert image.meta["width"] == 16


async def test_run_key_scopes_output_per_run_and_step(app_state, chain_project, fake_comfy):
    run = await run_to_completion(app_state, chain_project, chain_project.shots[0])
    step_a = chain_project.shots[0].steps[0]
    assert fake_comfy.submitted[0]["prompt"]["3"]["inputs"]["run_key"] == f"{run.id}/{step_a.id}"


async def test_events_describe_the_whole_run(app_state, chain_project):
    seen: list[str] = []

    async def collect():
        async with app_state.events.subscribe(chain_project.id) as events:
            async for event in events:
                seen.append(event.type)
                if event.type == "run.finished":
                    return

    collector = asyncio.create_task(collect())
    await asyncio.sleep(0)
    await run_to_completion(app_state, chain_project, chain_project.shots[0])
    await asyncio.wait_for(collector, timeout=10)

    assert seen[0] == "run.started"
    assert seen[-1] == "run.finished"
    assert seen.count("step.started") == 2
    assert seen.count("step.finished") == 2


# -- selective execution -------------------------------------------------------------------------------


async def test_running_one_step_pulls_in_its_dependencies(app_state, chain_project):
    shot = chain_project.shots[0]
    run = await run_to_completion(
        app_state, chain_project, shot, mode="step", step_ids=[shot.steps[1].id]
    )
    assert {sr.step_id for sr in run.step_runs} == {shot.steps[0].id, shot.steps[1].id}
    assert run.status == "success"


async def test_running_the_first_step_alone_does_not_run_the_second(app_state, chain_project):
    shot = chain_project.shots[0]
    run = await run_to_completion(
        app_state, chain_project, shot, mode="step", step_ids=[shot.steps[0].id]
    )
    assert [sr.step_id for sr in run.step_runs] == [shot.steps[0].id]


async def test_chain_mode_runs_downstream_steps_too(app_state, chain_project):
    shot = chain_project.shots[0]
    run = await run_to_completion(
        app_state, chain_project, shot, mode="chain", step_ids=[shot.steps[0].id]
    )
    assert len(run.step_runs) == 2


async def test_disabled_steps_are_not_run(app_state, chain_project):
    shot = chain_project.shots[0]
    shot.steps[1].enabled = False
    app_state.store.save(chain_project)

    run = await run_to_completion(app_state, chain_project, shot)
    assert [sr.step_id for sr in run.step_runs] == [shot.steps[0].id]


# -- value nodes ---------------------------------------------------------------------------------------


def png_bytes() -> bytes:
    """A real, tiny PNG — the media pipeline probes what it is handed, so a stub will not do."""
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def valued_project(app_state):
    """One step whose text input comes from a value node on the canvas rather than from a step."""
    project = app_state.store.create("Valued")
    con = register(app_state, project, "Consume", consumer_prompt())

    step = Step(name="Consume", workflow_id=con.id)
    caption = ValueNode(kind="string", name="Shared caption", value="from the canvas")
    shot = Shot(
        name="Shot 1",
        steps=[step],
        nodes=[caption],
        links=[
            Link(from_step=caption.id, from_port=VALUE_PORT, to_step=step.id, to_port="caption")
        ],
    )
    project.shots = [shot]
    app_state.store.save(project)
    return project


async def test_a_value_node_supplies_a_scalar_input(app_state, valued_project, fake_comfy):
    shot = valued_project.shots[0]
    run = await run_to_completion(app_state, valued_project, shot)

    assert run.status == "success", run.error
    # The value node overrode the workflow's own default, exactly as a chained scalar would.
    assert fake_comfy.submitted[0]["prompt"]["2"]["inputs"]["value"] == "from the canvas"
    assert run.step_runs[0].output("echo").meta["value"] == "from the canvas"


async def test_a_value_node_does_not_become_a_step_to_run(app_state, valued_project):
    """It supplies its value without executing, so it must never appear as something that ran."""
    shot = valued_project.shots[0]
    run = await run_to_completion(app_state, valued_project, shot)

    assert [sr.step_id for sr in run.step_runs] == [shot.steps[0].id]


async def test_editing_a_value_node_invalidates_the_cache(app_state, valued_project):
    shot = valued_project.shots[0]
    first = await run_to_completion(app_state, valued_project, shot)
    assert first.step_runs[0].status == "success"

    # Unchanged, the step is served from cache...
    again = await run_to_completion(app_state, valued_project, shot)
    assert again.step_runs[0].status == "cached"

    # ...but a different value has to produce a different result.
    shot.nodes[0].value = "something else"
    app_state.store.save(valued_project)
    after = await run_to_completion(app_state, valued_project, shot)
    assert after.step_runs[0].status == "success"
    assert after.step_runs[0].output("echo").meta["value"] == "something else"


async def test_a_media_node_stages_its_asset_for_the_step(app_state, valued_project, fake_comfy):
    """A media node hands the step a real file, the same way an upstream artifact would."""
    project = valued_project
    shot = project.shots[0]

    relative, sha = app_state.media_store.ingest_bytes(
        project.id, png_bytes(), kind="image", extension="png"
    )
    asset = Asset(name="imported.png", kind="image", path=relative, sha256=sha)
    project.assets[asset.id] = asset

    node = ValueNode(kind="media", name="Reference", asset_id=asset.id, media_kind="image")
    shot.nodes.append(node)
    shot.links.append(
        Link(from_step=node.id, from_port=VALUE_PORT, to_step=shot.steps[0].id, to_port="image")
    )
    app_state.store.save(project)

    run = await run_to_completion(app_state, project, shot)
    assert run.status == "success", run.error

    staged = fake_comfy.submitted[0]["prompt"]["1"]["inputs"]["source"]
    assert staged == str(app_state.media_store.path(project.id, relative))


async def test_a_media_node_with_nothing_selected_is_refused_before_running(
    app_state, valued_project
):
    """Caught while validating, so the user hears about it instead of watching a step fail."""
    shot = valued_project.shots[0]
    node = ValueNode(kind="media", name="Reference", media_kind="image")
    shot.nodes.append(node)
    shot.links.append(
        Link(from_step=node.id, from_port=VALUE_PORT, to_step=shot.steps[0].id, to_port="image")
    )
    app_state.store.save(valued_project)

    with pytest.raises(ExecutionFailed) as caught:
        await app_state.orchestrator.start(valued_project, shot)

    issues = [i["message"] for i in caught.value.details["issues"]]
    assert any("has no media selected" in message for message in issues)


# -- caching -------------------------------------------------------------------------------------------


async def test_second_identical_run_hits_the_cache(app_state, chain_project, fake_comfy):
    shot = chain_project.shots[0]
    await run_to_completion(app_state, chain_project, shot)
    submitted_after_first = len(fake_comfy.submitted)

    second = await run_to_completion(app_state, chain_project, shot)

    assert [sr.status for sr in second.step_runs] == ["cached", "cached"]
    assert len(fake_comfy.submitted) == submitted_after_first, "a cache hit still called ComfyUI"
    # Cached results still carry usable artifacts.
    assert second.step_runs[0].output("image").path


async def test_changing_a_parameter_invalidates_the_cache(app_state, chain_project):
    shot = chain_project.shots[0]
    await run_to_completion(app_state, chain_project, shot)

    shot.steps[0].param_overrides["prompt"] = "a dog"
    app_state.store.save(chain_project)
    second = await run_to_completion(app_state, chain_project, shot)

    assert second.step_runs[0].status == "success", "an edited parameter must re-execute"
    assert second.step_runs[0].output("caption").meta["value"] == "a dog"
    # The downstream step consumed different content, so it must re-run as well.
    assert second.step_runs[1].status == "success"


async def test_force_bypasses_the_cache(app_state, chain_project):
    shot = chain_project.shots[0]
    await run_to_completion(app_state, chain_project, shot)
    second = await run_to_completion(app_state, chain_project, shot, force=True)
    assert all(sr.status == "success" for sr in second.step_runs)


async def test_cache_entry_is_dropped_when_its_files_are_deleted(app_state, chain_project):
    shot = chain_project.shots[0]
    first = await run_to_completion(app_state, chain_project, shot)
    app_state.media_store.path(chain_project.id, first.step_runs[0].output("image").path).unlink()

    second = await run_to_completion(app_state, chain_project, shot)
    assert second.step_runs[0].status == "success", "a stale cache entry was trusted"


async def test_randomized_seed_is_never_cached(app_state, fake_comfy):
    project = app_state.store.create("Seeded")
    prompt = {
        "1": {"class_type": "WSStringInput", "inputs": {"port_name": "seed", "value": "0"}},
        "2": {"class_type": "EmptyImage", "inputs": {"width": 16, "height": 16}},
        "3": {
            "class_type": "WSImageOutput",
            "inputs": {"image": ["2", 0], "port_name": "image", "run_key": ""},
        },
    }
    workflow = register(app_state, project, "Seeded", prompt)
    workflow.params[0].is_seed = True
    step = Step(name="Gen", workflow_id=workflow.id, seed_mode="randomize")
    shot = Shot(name="S", steps=[step])
    project.shots = [shot]
    app_state.store.save(project)

    await run_to_completion(app_state, project, shot)
    second = await run_to_completion(app_state, project, shot)
    assert second.step_runs[0].status == "success"


# -- failure paths -------------------------------------------------------------------------------------


async def test_node_failure_is_reported_with_the_node_and_skips_dependents(
    app_state, chain_project, fake_comfy
):
    fake_comfy.fail_on_class = "EmptyImage"
    run = await run_to_completion(app_state, chain_project, chain_project.shots[0])

    assert run.status == "error"
    generate, consume = run.step_runs
    assert generate.status == "error"
    assert "deliberate test failure" in generate.error
    assert generate.error_node == "2"
    assert consume.status == "skipped"
    assert "upstream step failed" in consume.error


async def test_rejected_prompt_names_the_offending_node(app_state, chain_project, fake_comfy):
    fake_comfy.reject_prompt = True
    run = await run_to_completion(app_state, chain_project, chain_project.shots[0])

    assert run.status == "error"
    assert "rejected the workflow" in run.step_runs[0].error
    assert "bad sampler" in run.step_runs[0].error


async def test_interrupted_execution_is_surfaced(app_state, chain_project, fake_comfy):
    fake_comfy.interrupt_next = True
    run = await run_to_completion(app_state, chain_project, chain_project.shots[0])
    assert run.step_runs[0].status == "error"
    assert "interrupted" in run.step_runs[0].error.lower()


async def test_timeout_interrupts_and_reports(app_state, chain_project, fake_comfy):
    fake_comfy.hang = True
    app_state.settings.execution.step_timeout_s = 0.5

    run = await run_to_completion(app_state, chain_project, chain_project.shots[0], timeout=20)
    assert run.step_runs[0].status == "error"
    assert "timeout" in run.step_runs[0].error.lower()


async def test_cancel_stops_a_running_run(app_state, chain_project, fake_comfy):
    fake_comfy.execution_delay = 0.4
    shot = chain_project.shots[0]

    run = await app_state.orchestrator.start(chain_project, shot)
    task = app_state.orchestrator._tasks[run.id]
    await asyncio.sleep(0.2)
    assert await app_state.orchestrator.cancel(run.id)

    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(task), timeout=10)

    assert run.status == "cancelled"


async def test_workflow_without_output_nodes_is_rejected_clearly(app_state, fake_comfy):
    project = app_state.store.create("NoOutputs")
    prompt = {"1": {"class_type": "EmptyImage", "inputs": {"width": 16, "height": 16}}}
    workflow = register(app_state, project, "Dead end", prompt)
    shot = Shot(name="S", steps=[Step(name="Step", workflow_id=workflow.id)])
    project.shots = [shot]
    app_state.store.save(project)

    run = await run_to_completion(app_state, project, shot)
    assert run.step_runs[0].status == "error"
    assert "no ComfyWebStudio output nodes" in run.step_runs[0].error


async def test_missing_api_graph_is_reported_actionably(app_state, chain_project):
    shot = chain_project.shots[0]
    workflow_id = shot.steps[0].workflow_id
    app_state.store.workflow_path(chain_project.id, workflow_id, "api").unlink()

    run = await run_to_completion(app_state, chain_project, shot)
    assert "Open it in ComfyUI" in run.step_runs[0].error


async def test_invalid_graph_refuses_to_start(app_state, chain_project):
    shot = chain_project.shots[0]
    shot.links[0].to_port = "does_not_exist"

    with pytest.raises(ExecutionFailed, match="cannot run yet"):
        await app_state.orchestrator.start(chain_project, shot)


# -- persistence ---------------------------------------------------------------------------------------


async def test_run_is_persisted_and_reloadable(app_state, chain_project):
    run = await run_to_completion(app_state, chain_project, chain_project.shots[0])

    reloaded = app_state.store.load_run(chain_project.id, run.id)
    assert reloaded.status == "success"
    assert len(reloaded.step_runs) == 2
    assert reloaded.step_runs[0].output("image").sha256


async def test_latest_step_runs_survive_a_reload(app_state, chain_project):
    await run_to_completion(app_state, chain_project, chain_project.shots[0])
    shot = chain_project.shots[0]

    latest = app_state.store.latest_step_runs(chain_project.id, shot.id)
    assert set(latest) == {shot.steps[0].id, shot.steps[1].id}
    assert latest[shot.steps[0].id]["step_run"].output("image") is not None


# -- remote backend ------------------------------------------------------------------------------------


async def test_remote_backend_uploads_chained_media(settings, fake_comfy):
    """With no shared filesystem, a chained image must travel over HTTP."""
    from comfywebstudio.settings import ComfyBackendConfig
    from comfywebstudio.state import AppState

    settings.backends = [
        ComfyBackendConfig(id="cloud", name="Cloud", kind="remote", base_url=fake_comfy.base_url)
    ]
    settings.default_backend_id = "cloud"
    state = AppState(settings)
    try:
        project = state.store.create("Remote")
        gen = register(state, project, "Generate", generator_prompt())
        con = register(state, project, "Consume", consumer_prompt())
        a, b = Step(name="A", workflow_id=gen.id), Step(name="B", workflow_id=con.id)
        shot = Shot(
            name="S",
            steps=[a, b],
            links=[Link(from_step=a.id, from_port="image", to_step=b.id, to_port="image")],
        )
        project.shots = [shot]
        state.store.save(project)

        run = await run_to_completion(state, project, shot)

        assert run.status == "success", run.error
        assert fake_comfy.uploads, "nothing was uploaded to the remote instance"
        staged = fake_comfy.submitted[1]["prompt"]["1"]["inputs"]["source"]
        assert staged.startswith("webstudio/"), f"expected an uploaded name, got {staged!r}"
    finally:
        await state.shutdown()


async def test_prompt_id_is_a_uuid_comfyui_will_accept(app_state, chain_project, fake_comfy):
    """ComfyUI 0.31 validates the caller-supplied prompt_id and 400s anything that is not a UUID.

    We supply our own id so we can correlate websocket events without racing POST /prompt, which means the
    format is not ours to choose. The fake server enforces the same rule, so a regression here fails the
    run outright rather than only showing up against a real instance.
    """
    run = await run_to_completion(app_state, chain_project, chain_project.shots[0])

    assert run.status == "success", run.error
    submitted = [body["prompt_id"] for body in fake_comfy.submitted]
    assert submitted, "nothing was submitted"
    for prompt_id in submitted:
        # Canonical lowercase hyphenated form, which is what validate_job_id() accepts.
        assert str(uuid.UUID(prompt_id)) == prompt_id, f"{prompt_id!r} is not a canonical UUID"

    # And the ids the framework kept for correlation are the ones it actually sent.
    assert [sr.prompt_id for sr in run.step_runs] == submitted
