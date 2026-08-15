"""Rendering several shots one after another."""

from __future__ import annotations

import asyncio

from tests.test_api import import_workflow, make_project
from tests.test_execution import generator_prompt


async def three_shots(client) -> tuple[dict, list[dict]]:
    """Three one-step shots, each able to run on its own."""
    project = await make_project(client)
    workflow = await import_workflow(client, project["id"], "Generate", generator_prompt())

    shots = []
    for index in range(3):
        shot = (
            await client.post(f"/api/projects/{project['id']}/shots", json={"name": f"Shot {index + 1}"})
        ).json()
        await client.post(
            f"/api/projects/{project['id']}/shots/{shot['id']}/steps",
            json={"workflow_id": workflow["id"]},
        )
        shots.append(shot)
    return project, shots


async def drain(client, batch_id: str) -> dict:
    task = client.app_state.batches._tasks.get(batch_id)
    if task is not None:
        await asyncio.wait([task], timeout=60)
    batch = client.app_state.batches.get(batch_id)
    return batch.model_dump(mode="json")


async def test_a_batch_renders_every_shot(client):
    project, shots = await three_shots(client)

    response = await client.post(f"/api/projects/{project['id']}/runs/batch", json={})
    assert response.status_code == 202, response.text

    batch = await drain(client, response.json()["id"])
    assert batch["status"] == "success"
    assert [s["shot_id"] for s in batch["shots"]] == [s["id"] for s in shots]
    assert all(s["status"] in {"success", "cached"} for s in batch["shots"]), batch


async def test_a_batch_renders_only_the_shots_it_was_given(client):
    project, shots = await three_shots(client)

    response = await client.post(
        f"/api/projects/{project['id']}/runs/batch",
        json={"shot_ids": [shots[2]["id"], shots[0]["id"]]},
    )
    batch = await drain(client, response.json()["id"])
    assert [s["shot_id"] for s in batch["shots"]] == [shots[2]["id"], shots[0]["id"]], (
        "rendered in the order asked for, not the order they happen to be stored in"
    )

    runs = (await client.get(f"/api/projects/{project['id']}/runs")).json()
    assert {r["shot_id"] for r in runs} == {shots[2]["id"], shots[0]["id"]}


async def test_shots_are_rendered_one_at_a_time(client):
    """A shot can consume another's last result, so two of them in flight is not a state to allow."""
    project, _shots = await three_shots(client)
    orchestrator = client.app_state.orchestrator

    peak = 0
    original = orchestrator.start

    async def watched(*args, **kwargs):
        nonlocal peak
        run = await original(*args, **kwargs)
        # `active_runs` keeps finished ones too, so count only what has yet to reach an end.
        unfinished = [
            r for r in orchestrator.active_runs(project["id"])
            if r.status in {"queued", "running"}
        ]
        peak = max(peak, len(unfinished))
        return run

    orchestrator.start = watched  # type: ignore[method-assign]
    try:
        response = await client.post(f"/api/projects/{project['id']}/runs/batch", json={})
        await drain(client, response.json()["id"])
    finally:
        orchestrator.start = original  # type: ignore[method-assign]

    assert peak == 1, f"{peak} runs were in flight at once"


async def test_a_second_batch_is_refused_while_one_is_running(client):
    project, _shots = await three_shots(client)

    first = await client.post(f"/api/projects/{project['id']}/runs/batch", json={})
    assert first.status_code == 202

    second = await client.post(f"/api/projects/{project['id']}/runs/batch", json={})
    assert second.status_code == 409, second.text

    await drain(client, first.json()["id"])


async def test_an_empty_project_has_nothing_to_render(client):
    project = await make_project(client)
    response = await client.post(f"/api/projects/{project['id']}/runs/batch", json={})
    assert response.status_code == 422


async def test_the_active_batch_is_readable_so_a_reload_can_rejoin_it(client):
    project, _shots = await three_shots(client)
    started = await client.post(f"/api/projects/{project['id']}/runs/batch", json={})

    active = (await client.get(f"/api/projects/{project['id']}/runs/batch/active")).json()
    assert active["id"] == started.json()["id"]

    await drain(client, started.json()["id"])
    assert (await client.get(f"/api/projects/{project['id']}/runs/batch/active")).json() is None


async def test_stopping_a_batch_leaves_the_rest_unrendered(client):
    project, _shots = await three_shots(client)
    started = (await client.post(f"/api/projects/{project['id']}/runs/batch", json={})).json()

    cancelled = (
        await client.post(f"/api/projects/{project['id']}/runs/batch/{started['id']}/cancel")
    ).json()
    assert cancelled["cancelled"] is True

    batch = await drain(client, started["id"])
    assert batch["status"] == "cancelled"
    assert any(s["status"] in {"cancelled", "queued"} for s in batch["shots"])


async def test_a_failing_shot_does_not_hold_up_the_rest(client):
    """Nineteen good shots held hostage by the twentieth is not a trade anyone would choose."""
    project, shots = await three_shots(client)

    # A step pointing at a workflow that is no longer there cannot run, and cannot be made to.
    project_now = (await client.get(f"/api/projects/{project['id']}")).json()
    broken = next(s for s in project_now["shots"] if s["id"] == shots[0]["id"])
    await client.delete(
        f"/api/projects/{project['id']}/steps/{broken['steps'][0]['id']}"
    )

    response = await client.post(f"/api/projects/{project['id']}/runs/batch", json={})
    batch = await drain(client, response.json()["id"])

    by_shot = {s["shot_id"]: s for s in batch["shots"]}
    assert by_shot[shots[0]["id"]]["status"] == "error"
    assert by_shot[shots[0]["id"]]["error"]
    assert all(
        by_shot[s["id"]]["status"] in {"success", "cached"} for s in shots[1:]
    ), "the shots after the failure still rendered"
    assert batch["status"] == "error", "and the batch as a whole says something went wrong"


async def test_a_batch_announces_each_shot_as_it_goes(client):
    project, shots = await three_shots(client)
    state = client.app_state
    seen: list[tuple[str, str]] = []

    async def listen():
        async with state.events.subscribe(project["id"]) as stream:
            async for event in stream:
                if event.type == "shots.batch.progress":
                    seen.append((event.data["shot_id"], event.data["status"]))
                if event.type == "shots.batch.finished":
                    return

    listener = asyncio.create_task(listen())
    await asyncio.sleep(0.05)

    started = (await client.post(f"/api/projects/{project['id']}/runs/batch", json={})).json()
    await drain(client, started["id"])
    await asyncio.wait_for(listener, timeout=10)

    assert [shot_id for shot_id, status in seen if status == "running"] == [s["id"] for s in shots]
    assert len([1 for _, status in seen if status in {"success", "cached"}]) == 3


async def test_a_finished_run_says_which_shot_it_was(client):
    """The panel watches several runs at once, so 'which one just ended' has to be in the event."""
    project, shots = await three_shots(client)
    state = client.app_state
    finished: list[dict] = []

    async def listen():
        async with state.events.subscribe(project["id"]) as stream:
            async for event in stream:
                if event.type == "run.finished":
                    finished.append(event.data)
                    return

    listener = asyncio.create_task(listen())
    await asyncio.sleep(0.05)

    response = await client.post(
        f"/api/projects/{project['id']}/shots/{shots[1]['id']}/run", json={"mode": "shot"}
    )
    task = state.orchestrator._tasks.get(response.json()["id"])
    if task is not None:
        await asyncio.wait([task], timeout=30)
    await asyncio.wait_for(listener, timeout=10)

    assert finished[0]["shot_id"] == shots[1]["id"]
