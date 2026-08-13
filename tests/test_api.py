"""API tests driving the real FastAPI app against the fake ComfyUI.

These walk the flow a user actually takes: create a project, import workflows, wire steps together, run,
preview, cut a timeline, render, export and re-import.
"""

from __future__ import annotations

import asyncio
import io
import json

import pytest

from .test_execution import consumer_prompt, generator_prompt


async def make_project(client, name="Demo") -> dict:
    response = await client.post("/api/projects", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


async def import_workflow(client, project_id, name, prompt) -> dict:
    response = await client.post(
        f"/api/projects/{project_id}/workflows",
        json={"name": name, "prompt": prompt},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def build_chain(client) -> tuple[dict, dict, list[dict]]:
    project = await make_project(client)
    gen = await import_workflow(client, project["id"], "Generate", generator_prompt())
    con = await import_workflow(client, project["id"], "Consume", consumer_prompt())

    shot = (
        await client.post(f"/api/projects/{project['id']}/shots", json={"name": "Shot 1"})
    ).json()

    steps = []
    for workflow in (gen, con):
        response = await client.post(
            f"/api/projects/{project['id']}/shots/{shot['id']}/steps",
            json={"workflow_id": workflow["id"]},
        )
        assert response.status_code == 201, response.text
        steps.append(response.json())

    link = await client.post(
        f"/api/projects/{project['id']}/shots/{shot['id']}/links",
        json={
            "from_step": steps[0]["id"], "from_port": "image",
            "to_step": steps[1]["id"], "to_port": "image",
        },
    )
    assert link.status_code == 201, link.text
    return project, shot, steps


async def run_and_wait(client, project_id, shot_id, **body) -> dict:
    response = await client.post(
        f"/api/projects/{project_id}/shots/{shot_id}/run", json=body or {"mode": "shot"}
    )
    assert response.status_code == 202, response.text
    run = response.json()

    orchestrator = client.app_state.orchestrator
    task = orchestrator._tasks.get(run["id"])
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=30)
    return (await client.get(f"/api/projects/{project_id}/runs/{run['id']}")).json()


# -- meta ----------------------------------------------------------------------------------------------


async def test_health_reports_backends(client):
    payload = (await client.get("/api/health")).json()
    assert payload["ok"] is True
    assert payload["backends"][0]["shared_filesystem"] is True


# -- projects ------------------------------------------------------------------------------------------


async def test_project_crud(client):
    project = await make_project(client, "My Film")
    assert project["name"] == "My Film"

    listed = (await client.get("/api/projects")).json()
    assert [p["id"] for p in listed] == [project["id"]]

    patched = (
        await client.patch(f"/api/projects/{project['id']}", json={"name": "Renamed"})
    ).json()
    assert patched["name"] == "Renamed"

    assert (await client.delete(f"/api/projects/{project['id']}")).status_code == 204
    assert (await client.get(f"/api/projects/{project['id']}")).status_code == 404


async def test_unknown_project_returns_structured_error(client):
    response = await client.get("/api/projects/proj_nope")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


# -- workflows -----------------------------------------------------------------------------------------


async def test_import_discovers_ports_and_params(client):
    project = await make_project(client)
    workflow = await import_workflow(client, project["id"], "Generate", generator_prompt())

    ports = {p["key"]: p for p in workflow["ports"]}
    assert ports["prompt"]["direction"] == "in"
    assert ports["image"]["direction"] == "out" and ports["image"]["kind"] == "image"
    assert {p["key"] for p in workflow["params"]} == {"prompt"}


async def test_upload_workflow_json_file(client):
    project = await make_project(client)
    payload = json.dumps(generator_prompt()).encode()

    response = await client.post(
        f"/api/projects/{project['id']}/workflows/upload",
        files={"file": ("txt2img.json", io.BytesIO(payload), "application/json")},
    )
    assert response.status_code == 201, response.text
    assert response.json()["name"] == "txt2img"


async def test_workflow_without_ws_nodes_warns(client):
    project = await make_project(client)
    workflow = await import_workflow(
        client, project["id"], "Plain", {"1": {"class_type": "EmptyImage", "inputs": {"width": 8}}}
    )
    assert any("No ComfyWebStudio input or output nodes" in w for w in workflow["warnings"])


async def test_deleting_a_workflow_in_use_is_refused(client):
    project, _shot, _steps = await build_chain(client)
    workflow_id = (await client.get(f"/api/projects/{project['id']}/workflows")).json()[0]["id"]

    response = await client.delete(f"/api/projects/{project['id']}/workflows/{workflow_id}")
    assert response.status_code == 422
    assert "still used by" in response.json()["message"]


async def test_expose_and_unexpose_a_raw_widget(client):
    project = await make_project(client)
    prompt = generator_prompt()
    prompt["9"] = {
        "class_type": "KSampler",
        "inputs": {"model": ["2", 0], "seed": 1, "steps": 20, "cfg": 8.0, "sampler_name": "euler"},
    }
    workflow = await import_workflow(client, project["id"], "Sampled", prompt)

    bindable = (
        await client.get(f"/api/projects/{project['id']}/workflows/{workflow['id']}/bindable")
    ).json()
    steps_widget = next(b for b in bindable if b["node_id"] == "9" and b["input_name"] == "steps")
    assert steps_widget["exposed"] is False
    assert steps_widget["current"] == 20

    exposed = (
        await client.post(
            f"/api/projects/{project['id']}/workflows/{workflow['id']}/expose",
            json={"node_id": "9", "input_name": "steps"},
        )
    ).json()
    param = next(p for p in exposed["params"] if p["key"] == "@9.steps")
    assert param["source"] == "raw_widget" and param["default"] == 20

    assert (
        await client.delete(
            f"/api/projects/{project['id']}/workflows/{workflow['id']}/expose/@9.steps"
        )
    ).status_code == 204


async def test_open_in_comfy_returns_a_bridge_url(client):
    project = await make_project(client)
    workflow = await import_workflow(client, project["id"], "Generate", generator_prompt())

    payload = (
        await client.post(
            f"/api/projects/{project['id']}/workflows/{workflow['id']}/open-in-comfy"
        )
    ).json()

    assert "ws_open=" in payload["url"]
    assert payload["node_pack_installed"] is True
    assert payload["token"] in client.app_state.bridge_tokens


# -- shots and links -----------------------------------------------------------------------------------


async def test_link_validation_rejects_bad_connections(client):
    project, shot, steps = await build_chain(client)

    # Wrong kinds.
    response = await client.post(
        f"/api/projects/{project['id']}/shots/{shot['id']}/links",
        json={"from_step": steps[0]["id"], "from_port": "image",
              "to_step": steps[1]["id"], "to_port": "caption"},
    )
    assert response.status_code == 422
    assert "Cannot connect" in response.json()["message"]

    # Already connected.
    response = await client.post(
        f"/api/projects/{project['id']}/shots/{shot['id']}/links",
        json={"from_step": steps[0]["id"], "from_port": "image",
              "to_step": steps[1]["id"], "to_port": "image"},
    )
    assert response.status_code == 422
    assert "already connected" in response.json()["message"]

    # Cycle.
    response = await client.post(
        f"/api/projects/{project['id']}/shots/{shot['id']}/links",
        json={"from_step": steps[1]["id"], "from_port": "final",
              "to_step": steps[0]["id"], "to_port": "prompt"},
    )
    assert response.status_code == 422


async def test_validate_endpoint_reports_the_graph(client):
    project, shot, steps = await build_chain(client)
    report = (
        await client.get(f"/api/projects/{project['id']}/shots/{shot['id']}/validate")
    ).json()
    assert report["ok"] is True
    assert report["order"] == [s["id"] for s in steps]


async def test_deleting_a_step_removes_its_links(client):
    project, shot, steps = await build_chain(client)
    assert (await client.delete(f"/api/projects/{project['id']}/steps/{steps[0]['id']}")).status_code == 204

    updated = (await client.get(f"/api/projects/{project['id']}/shots/{shot['id']}")).json()
    assert updated["links"] == []
    assert len(updated["steps"]) == 1


async def test_duplicate_shot_gets_fresh_ids(client):
    project, shot, _steps = await build_chain(client)
    copy = (
        await client.post(f"/api/projects/{project['id']}/shots/{shot['id']}/duplicate")
    ).json()

    assert copy["id"] != shot["id"]
    assert {s["id"] for s in copy["steps"]}.isdisjoint({s["id"] for s in shot["steps"]})
    # Links must point at the copy's own steps, not the original's.
    assert copy["links"][0]["from_step"] in {s["id"] for s in copy["steps"]}


# -- running -------------------------------------------------------------------------------------------


async def test_run_a_chain_through_the_api(client):
    project, shot, steps = await build_chain(client)
    run = await run_and_wait(client, project["id"], shot["id"])

    assert run["status"] == "success", run.get("error")
    assert [sr["status"] for sr in run["step_runs"]] == ["success", "success"]
    assert run["step_runs"][1]["outputs"]


async def test_results_endpoint_repopulates_previews(client):
    project, shot, steps = await build_chain(client)
    await run_and_wait(client, project["id"], shot["id"])

    results = (
        await client.get(f"/api/projects/{project['id']}/shots/{shot['id']}/results")
    ).json()
    assert set(results) == {steps[0]["id"], steps[1]["id"]}
    assert results[steps[0]["id"]]["step_run"]["outputs"]


async def test_media_endpoint_serves_an_artifact(client):
    project, shot, _steps = await build_chain(client)
    run = await run_and_wait(client, project["id"], shot["id"])
    artifact = next(a for a in run["step_runs"][0]["outputs"] if a["kind"] == "image")

    response = await client.get(
        f"/api/projects/{project['id']}/media", params={"path": artifact["path"]}
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:4] == b"\x89PNG"


async def test_media_endpoint_refuses_traversal(client):
    project = await make_project(client)
    response = await client.get(
        f"/api/projects/{project['id']}/media", params={"path": "../../../etc/passwd"}
    )
    assert response.status_code == 422


async def test_run_with_a_parameter_override(client):
    project, shot, steps = await build_chain(client)
    await client.patch(
        f"/api/projects/{project['id']}/steps/{steps[0]['id']}",
        json={"param_overrides": {"prompt": "a lighthouse"}},
    )

    run = await run_and_wait(client, project["id"], shot["id"])
    caption = next(a for a in run["step_runs"][0]["outputs"] if a["port_key"] == "caption")
    assert caption["meta"]["value"] == "a lighthouse"


async def test_events_websocket_streams_a_run(client, settings, fake_comfy):
    """The websocket is exercised through the same app instance the REST calls use."""
    project, shot, _steps = await build_chain(client)
    state = client.app_state

    received: list[str] = []

    async def listen():
        async with state.events.subscribe(project["id"]) as stream:
            async for event in stream:
                received.append(event.type)
                if event.type == "run.finished":
                    return

    listener = asyncio.create_task(listen())
    await asyncio.sleep(0)
    await run_and_wait(client, project["id"], shot["id"])
    await asyncio.wait_for(listener, timeout=10)

    assert "run.started" in received and "run.finished" in received


# -- bridge --------------------------------------------------------------------------------------------


async def test_bridge_round_trip_updates_ports(client):
    project = await make_project(client)
    workflow = await import_workflow(client, project["id"], "Generate", generator_prompt())

    opened = (
        await client.post(
            f"/api/projects/{project['id']}/workflows/{workflow['id']}/open-in-comfy"
        )
    ).json()
    token = opened["token"]

    # The extension asks for the graph. This workflow was imported in API format only, so it gets the
    # prompt instead — ComfyUI can build a graph from that itself.
    fetched = await client.get(
        f"/api/bridge/workflow/{workflow['id']}", headers={"X-WebStudio-Token": token}
    )
    assert fetched.status_code == 200
    assert fetched.json()["has_ui_graph"] is False
    assert fetched.json()["prompt"]

    # ...and later posts an edited graph back, with a new port added.
    edited = generator_prompt()
    edited["5"] = {
        "class_type": "WSStringInput",
        "inputs": {"port_name": "negative", "value": "blurry"},
    }
    response = await client.post(
        "/api/bridge/workflow",
        headers={"X-WebStudio-Token": token},
        json={"step_id": workflow["id"], "workflow": {"nodes": [], "links": []}, "prompt": edited},
    )
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True

    updated = (
        await client.get(f"/api/projects/{project['id']}/workflows/{workflow['id']}")
    ).json()
    assert "negative" in {p["key"] for p in updated["ports"]}
    assert "negative" in {p["key"] for p in updated["params"]}


async def test_bridge_rejects_an_unknown_token(client):
    response = await client.post(
        "/api/bridge/workflow",
        headers={"X-WebStudio-Token": "nope"},
        json={"step_id": "wf_x", "workflow": {}, "prompt": {"1": {"class_type": "X", "inputs": {}}}},
    )
    assert response.status_code == 401


async def test_bridge_token_is_scoped_to_one_workflow(client):
    project = await make_project(client)
    first = await import_workflow(client, project["id"], "A", generator_prompt())
    second = await import_workflow(client, project["id"], "B", consumer_prompt())

    token = (
        await client.post(f"/api/projects/{project['id']}/workflows/{first['id']}/open-in-comfy")
    ).json()["token"]

    response = await client.post(
        "/api/bridge/workflow",
        headers={"X-WebStudio-Token": token},
        json={"step_id": second["id"], "workflow": {}, "prompt": generator_prompt()},
    )
    assert response.status_code == 401


async def test_bridge_sync_drops_links_to_removed_ports(client):
    project, shot, steps = await build_chain(client)
    workflows = (await client.get(f"/api/projects/{project['id']}/workflows")).json()
    generator = next(w for w in workflows if w["name"] == "Generate")

    token = (
        await client.post(
            f"/api/projects/{project['id']}/workflows/{generator['id']}/open-in-comfy"
        )
    ).json()["token"]

    stripped = generator_prompt()
    del stripped["3"]  # the WSImageOutput the link depends on

    response = await client.post(
        "/api/bridge/workflow",
        headers={"X-WebStudio-Token": token},
        json={"step_id": generator["id"], "workflow": {"nodes": []}, "prompt": stripped},
    )
    assert response.status_code == 200
    assert "image" in response.json()["removed_ports"]
    assert response.json()["broken_links"]

    updated = (await client.get(f"/api/projects/{project['id']}/shots/{shot['id']}")).json()
    assert updated["links"] == []


# -- timeline and render -------------------------------------------------------------------------------


async def test_build_timeline_from_shots_and_render(client, tmp_path):
    project, shot, steps = await build_chain(client)
    await run_and_wait(client, project["id"], shot["id"])

    timeline = (
        await client.post(f"/api/projects/{project['id']}/timeline/from-shots")
    ).json()
    assert timeline["tracks"], "no track was created"
    assert timeline["tracks"][0]["clips"], "no clip was placed"

    resolved = (await client.get(f"/api/projects/{project['id']}/timeline/resolved")).json()
    assert resolved["duration"] > 0
    assert all(c["error"] is None for c in resolved["clips"]), resolved["clips"]

    # Render a still — fast, and proves the compositor resolves clips to real pixels.
    state = client.app_state
    done = asyncio.Event()
    payload: dict = {}

    async def listen():
        async with state.events.subscribe(project["id"]) as stream:
            async for event in stream:
                if event.type == "render.finished":
                    payload.update(event.data)
                    done.set()
                    return

    listener = asyncio.create_task(listen())
    await asyncio.sleep(0)

    response = await client.post(
        f"/api/projects/{project['id']}/timeline/render",
        json={"still": True, "name": "poster"},
    )
    assert response.status_code == 202
    await asyncio.wait_for(done.wait(), timeout=30)
    listener.cancel()

    assert payload["ok"] is True, payload.get("error")
    served = await client.get(
        f"/api/projects/{project['id']}/media", params={"path": payload["path"]}
    )
    assert served.status_code == 200 and served.content[:4] == b"\x89PNG"


async def test_render_video_produces_a_playable_file(client):
    project, shot, _steps = await build_chain(client)
    await run_and_wait(client, project["id"], shot["id"])
    await client.post(f"/api/projects/{project['id']}/timeline/from-shots")

    # Keep it short: a couple of frames is enough to prove the encoder path.
    await client.patch(f"/api/projects/{project['id']}/timeline", json={"fps": 4, "width": 64, "height": 64})
    tracks = (await client.get(f"/api/projects/{project['id']}/timeline")).json()["tracks"]
    track, clip = tracks[0], tracks[0]["clips"][0]
    await client.patch(
        f"/api/projects/{project['id']}/timeline/tracks/{track['id']}/clips/{clip['id']}",
        json={"duration": 0.5},
    )

    state = client.app_state
    payload: dict = {}
    done = asyncio.Event()

    async def listen():
        async with state.events.subscribe(project["id"]) as stream:
            async for event in stream:
                if event.type == "render.finished":
                    payload.update(event.data)
                    done.set()
                    return

    listener = asyncio.create_task(listen())
    await asyncio.sleep(0)
    await client.post(f"/api/projects/{project['id']}/timeline/render", json={"name": "cut"})
    await asyncio.wait_for(done.wait(), timeout=60)
    listener.cancel()

    assert payload["ok"] is True, payload.get("error")
    assert payload["kind"] == "video"

    import av

    path = state.store.resolve(project["id"], payload["path"])
    with av.open(str(path)) as container:
        stream = next(s for s in container.streams if s.type == "video")
        assert stream.codec_context.width == 64
        assert sum(1 for _ in container.decode(stream)) >= 1


# -- render scopes ---------------------------------------------------------------------------------------


async def render_and_wait(client, project_id: str, body: dict, *, timeout: float = 90) -> dict:
    """Kick off a render and return the `render.finished` payload."""
    state = client.app_state
    payload: dict = {}
    done = asyncio.Event()

    async def listen():
        async with state.events.subscribe(project_id) as stream:
            async for event in stream:
                if event.type == "render.finished":
                    payload.update(event.data)
                    done.set()
                    return

    listener = asyncio.create_task(listen())
    await asyncio.sleep(0)

    response = await client.post(f"/api/projects/{project_id}/timeline/render", json=body)
    if response.status_code != 202:
        listener.cancel()
        return {"status_code": response.status_code, **response.json()}

    await asyncio.wait_for(done.wait(), timeout=timeout)
    listener.cancel()
    return payload


async def timeline_of_two_clips(client) -> tuple[dict, list[dict]]:
    """A cheap two-clip timeline: tiny frames, quarter-second clips, back to back."""
    project, shot, _steps = await build_chain(client)
    await run_and_wait(client, project["id"], shot["id"])
    await client.post(f"/api/projects/{project['id']}/timeline/from-shots")
    await client.patch(
        f"/api/projects/{project['id']}/timeline", json={"fps": 4, "width": 32, "height": 32}
    )

    tracks = (await client.get(f"/api/projects/{project['id']}/timeline")).json()["tracks"]
    track = tracks[0]
    first = track["clips"][0]
    await client.patch(
        f"/api/projects/{project['id']}/timeline/tracks/{track['id']}/clips/{first['id']}",
        json={"start": 0.0, "duration": 0.5, "name": "one"},
    )
    await client.post(
        f"/api/projects/{project['id']}/timeline/tracks/{track['id']}/clips",
        json={"source": first["source"], "start": 0.5, "duration": 0.5, "name": "two"},
    )

    clips = (await client.get(f"/api/projects/{project['id']}/timeline")).json()["tracks"][0]["clips"]
    assert len(clips) == 2, clips
    return project, clips


async def test_rendering_a_range_covers_only_that_span(client):
    project, _clips = await timeline_of_two_clips(client)

    payload = await render_and_wait(
        client, project["id"], {"name": "span", "scope": "range", "start_s": 0.5, "end_s": 1.0}
    )
    assert payload["ok"] is True, payload.get("error")
    assert payload["duration"] == pytest.approx(0.5, abs=0.26), payload


async def test_rendering_one_clip_produces_one_file_starting_at_that_clip(client):
    project, clips = await timeline_of_two_clips(client)

    payload = await render_and_wait(
        client, project["id"], {"name": "single", "scope": "clip", "clip_id": clips[1]["id"]}
    )
    assert payload["ok"] is True, payload.get("error")
    assert len(payload["outputs"]) == 1
    assert "two" in payload["path"], "the file should be named after the clip"


async def test_rendering_each_clip_separately_produces_a_file_per_clip(client):
    project, _clips = await timeline_of_two_clips(client)

    payload = await render_and_wait(client, project["id"], {"name": "batch", "scope": "clips"})
    assert payload["ok"] is True, payload.get("error")
    assert len(payload["outputs"]) == 2, payload

    # Numbered in timeline order, so a file browser sorts them the way the cut runs.
    names = [output["path"].rsplit("/", 1)[-1] for output in payload["outputs"]]
    assert "001" in names[0] and "002" in names[1], names

    listed = {item["name"] for item in (await client.get(f"/api/projects/{project['id']}/timeline/renders")).json()}
    assert all(name in listed for name in names)


async def test_render_output_settings_override_the_project_for_one_render_only(client):
    project, _clips = await timeline_of_two_clips(client)

    payload = await render_and_wait(
        client, project["id"], {"name": "big", "width": 48, "height": 48, "fps": 5}
    )
    assert payload["ok"] is True, payload.get("error")

    import av

    path = client.app_state.store.resolve(project["id"], payload["path"])
    with av.open(str(path)) as container:
        stream = next(s for s in container.streams if s.type == "video")
        assert (stream.codec_context.width, stream.codec_context.height) == (48, 48)

    # ...and the project's own timeline is untouched.
    timeline = (await client.get(f"/api/projects/{project['id']}/timeline")).json()
    assert (timeline["width"], timeline["height"], timeline["fps"]) == (32, 32, 4)


async def test_rendering_a_clip_without_saying_which_is_refused(client):
    project, _clips = await timeline_of_two_clips(client)
    result = await render_and_wait(client, project["id"], {"scope": "clip"})
    assert result["status_code"] == 422
    assert "Select a clip" in result["message"]


async def test_a_still_of_one_clip_is_taken_from_inside_that_clip(client):
    """The playhead is timeline-absolute; a single-clip render has to rebase it onto the clip."""
    project, clips = await timeline_of_two_clips(client)

    payload = await render_and_wait(
        client,
        project["id"],
        {"name": "frame", "scope": "clip", "clip_id": clips[1]["id"], "still": True, "time_s": 0.75},
    )
    assert payload["ok"] is True, payload.get("error")
    assert payload["kind"] == "image"

    served = await client.get(
        f"/api/projects/{project['id']}/media", params={"path": payload["path"]}
    )
    assert served.status_code == 200 and served.content[:4] == b"\x89PNG"


# -- settings ------------------------------------------------------------------------------------------


async def test_settings_round_trip(client):
    settings = (await client.get("/api/settings")).json()
    assert settings["port"]

    updated = (
        await client.patch("/api/settings", json={"execution": {"max_concurrent_steps": 3}})
    ).json()
    assert updated["execution"]["max_concurrent_steps"] == 3


async def test_backend_test_endpoint_reports_the_node_pack(client):
    backends = (await client.get("/api/settings/backends")).json()
    result = (await client.post(f"/api/settings/backends/{backends[0]['id']}/test")).json()

    assert result["reachable"] is True
    assert result["comfyui_version"] == "0.24.1"
    assert result["node_pack"]["pack_version"] == "0.1.0"
    assert result["protocol_ok"] is True


async def test_adding_a_backend_validates_the_root(client, tmp_path):
    response = await client.post(
        "/api/settings/backends",
        json={"name": "Bad", "kind": "local", "base_url": "http://x:1", "comfy_root": str(tmp_path)},
    )
    assert response.status_code == 422
    assert "main.py" in response.json()["message"]


# -- export / import -----------------------------------------------------------------------------------


async def test_export_and_reimport_through_the_api(client):
    project, shot, _steps = await build_chain(client)
    await run_and_wait(client, project["id"], shot["id"])

    exported = await client.get(f"/api/projects/{project['id']}/export")
    assert exported.status_code == 200
    assert exported.content[:2] == b"PK"

    reimported = await client.post(
        "/api/projects/import",
        files={"file": ("copy.cwsproj", io.BytesIO(exported.content), "application/zip")},
        params={"name": "Reimported"},
    )
    assert reimported.status_code == 201, reimported.text
    payload = reimported.json()
    assert payload["name"] == "Reimported"
    assert payload["id"] != project["id"]
    assert len(payload["shots"][0]["steps"]) == 2

    # The imported copy still has its artifacts, so previews work immediately.
    results = (
        await client.get(f"/api/projects/{payload['id']}/shots/{payload['shots'][0]['id']}/results")
    ).json()
    assert results


# -- ComfyUI workflow browsing ---------------------------------------------------------------------------


async def test_lists_workflows_saved_in_comfyui(client, fake_comfy):
    """The picker reads ComfyUI's own /userdata rather than asking the user for a file."""
    import json as _json

    workflows_dir = fake_comfy.root / "user" / "default" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    (workflows_dir / "portrait.json").write_text(
        _json.dumps({"nodes": [{"id": 1, "type": "EmptyImage", "widgets_values": [64, 64]}], "links": []})
    )
    (workflows_dir / ".index.json").write_text('{"favorites": []}')

    payload = (await client.get("/api/comfy/workflows")).json()

    assert payload["reachable"] is True
    names = {w["name"] for w in payload["workflows"]}
    assert "portrait" in names
    assert ".index" not in names, "the frontend's bookmark file is not a workflow"


async def test_unreachable_comfyui_does_not_break_the_picker(client, fake_comfy):
    await fake_comfy.stop()
    payload = (await client.get("/api/comfy/workflows")).json()
    assert payload["reachable"] is False
    assert payload["workflows"] == []
    assert payload["error"]


async def test_import_a_workflow_straight_from_comfyui(client, fake_comfy):
    import json as _json

    project = await make_project(client)
    workflows_dir = fake_comfy.root / "user" / "default" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    (workflows_dir / "chain.json").write_text(
        _json.dumps(
            {
                "nodes": [
                    {"id": 1, "type": "WSStringInput", "widgets_values": ["prompt", "a cat"]},
                    {"id": 2, "type": "EmptyImage", "widgets_values": [64, 64],
                     "outputs": [{"name": "IMAGE", "type": "IMAGE"}]},
                    {"id": 3, "type": "WSImageOutput", "widgets_values": ["image", "png", ""],
                     "inputs": [{"name": "image", "type": "IMAGE", "link": 1}]},
                ],
                "links": [[1, 2, 0, 3, 0, "IMAGE"]],
            }
        )
    )

    response = await client.post(
        f"/api/comfy/projects/{project['id']}/import", json={"path": "chain.json"}
    )
    assert response.status_code == 201, response.text
    workflow = response.json()

    assert workflow["name"] == "chain"
    # Converted from the LiteGraph document, so our ports were discovered.
    assert {p["key"] for p in workflow["ports"]} == {"prompt", "image"}
    assert workflow["comfy_userdata_path"] == "workflows/chain.json"

    listed = (await client.get(f"/api/projects/{project['id']}/workflows")).json()
    assert len(listed) == 1


async def test_importing_a_missing_comfy_workflow_is_a_clean_404(client):
    project = await make_project(client)
    response = await client.post(
        f"/api/comfy/projects/{project['id']}/import", json={"path": "nope.json"}
    )
    assert response.status_code == 404


# -- assets and source nodes -----------------------------------------------------------------------------


async def test_capturing_a_step_output_as_an_asset(client):
    project, shot, steps = await build_chain(client)
    await run_and_wait(client, project["id"], shot["id"])

    response = await client.post(
        f"/api/projects/{project['id']}/assets/capture",
        json={
            "shot_id": shot["id"], "step_id": steps[0]["id"],
            "port_key": "image", "name": "Hero frame",
        },
    )
    assert response.status_code == 201, response.text
    asset = response.json()

    assert asset["name"] == "Hero frame" and asset["kind"] == "image"
    assert asset["source"]["step_id"] == steps[0]["id"]
    assert asset["generated"], "a generated asset should record when it was produced"

    # It behaves like any other asset — the media endpoint serves it.
    served = await client.get(
        f"/api/projects/{project['id']}/media", params={"path": asset["path"]}
    )
    assert served.status_code == 200


async def test_capturing_before_anything_ran_is_refused(client):
    project, shot, steps = await build_chain(client)
    response = await client.post(
        f"/api/projects/{project['id']}/assets/capture",
        json={"shot_id": shot["id"], "step_id": steps[0]["id"], "port_key": "image"},
    )
    assert response.status_code == 422
    assert "Run it first" in response.json()["message"]


async def test_refreshing_a_generated_asset_picks_up_a_newer_result(client):
    project, shot, steps = await build_chain(client)
    await run_and_wait(client, project["id"], shot["id"])
    asset = (
        await client.post(
            f"/api/projects/{project['id']}/assets/capture",
            json={"shot_id": shot["id"], "step_id": steps[0]["id"], "port_key": "caption"},
        )
    ).json()

    # Change what the step produces, then re-run so there is a genuinely different result.
    await client.patch(
        f"/api/projects/{project['id']}/steps/{steps[0]['id']}",
        json={"param_overrides": {"prompt": "a completely different caption"}},
    )
    await run_and_wait(client, project["id"], shot["id"], mode="shot", force=True)

    refreshed = await client.post(f"/api/projects/{project['id']}/assets/{asset['id']}/refresh")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["sha256"] != asset["sha256"], "it should point at the newer result"


async def test_refreshing_an_imported_asset_is_refused(client):
    project = await make_project(client)
    upload = await client.post(
        f"/api/projects/{project['id']}/assets",
        files={"file": ("still.png", io.BytesIO(_png_bytes()), "image/png")},
    )
    asset = upload.json()
    assert asset["source"] is None

    response = await client.post(f"/api/projects/{project['id']}/assets/{asset['id']}/refresh")
    assert response.status_code == 422
    assert "imported" in response.json()["message"]


async def test_a_dropped_shot_node_feeds_a_step_its_last_result(client):
    """Dropping a shot supplies what it produced. It is a source: it does not re-run the shot."""
    source, source_shot, _steps = await build_chain(client)
    await run_and_wait(client, source["id"], source_shot["id"])

    # A second shot that consumes what the first one made.
    consumer_wf = await import_workflow(client, source["id"], "Consume2", consumer_prompt())
    shot = (await client.post(f"/api/projects/{source['id']}/shots", json={"name": "Uses it"})).json()
    step = (
        await client.post(
            f"/api/projects/{source['id']}/shots/{shot['id']}/steps",
            json={"workflow_id": consumer_wf["id"]},
        )
    ).json()
    node = (
        await client.post(
            f"/api/projects/{source['id']}/shots/{shot['id']}/nodes",
            json={
                "kind": "shot", "name": "From chain",
                "source_shot_id": source_shot["id"], "source_port": "final",
                "media_kind": "image",
            },
        )
    ).json()
    link = await client.post(
        f"/api/projects/{source['id']}/shots/{shot['id']}/links",
        json={
            "from_step": node["id"], "from_port": "value",
            "to_step": step["id"], "to_port": "image",
        },
    )
    assert link.status_code == 201, link.text

    run = await run_and_wait(client, source["id"], shot["id"])
    assert run["status"] == "success", run.get("error")
    # Only the consuming step ran — the source shot was read, not executed.
    assert [sr["step_id"] for sr in run["step_runs"]] == [step["id"]]


async def test_a_shot_node_with_nothing_chosen_is_refused_before_running(client):
    project, shot, _steps = await build_chain(client)
    # A step of its own, so its image input is free to wire the source node into.
    workflow = await import_workflow(client, project["id"], "Consume2", consumer_prompt())
    step = (
        await client.post(
            f"/api/projects/{project['id']}/shots/{shot['id']}/steps",
            json={"workflow_id": workflow["id"]},
        )
    ).json()
    node = (
        await client.post(
            f"/api/projects/{project['id']}/shots/{shot['id']}/nodes",
            json={"kind": "shot", "media_kind": "image"},
        )
    ).json()
    link = await client.post(
        f"/api/projects/{project['id']}/shots/{shot['id']}/links",
        json={
            "from_step": node["id"], "from_port": "value",
            "to_step": step["id"], "to_port": "image",
        },
    )
    assert link.status_code == 201, link.text

    report = (
        await client.get(f"/api/projects/{project['id']}/shots/{shot['id']}/validate")
    ).json()
    assert report["ok"] is False
    assert any("no shot output chosen" in i["message"] for i in report["issues"])


def _png_bytes() -> bytes:
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (8, 8), (90, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


# -- shot templates --------------------------------------------------------------------------------------


async def save_template(client, project_id: str, shot_id: str, **body) -> dict:
    response = await client.post(
        f"/api/projects/{project_id}/shots/{shot_id}/save-as-template", json=body
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_save_a_shot_to_the_library_and_place_it_in_another_project(client):
    source, shot, _steps = await build_chain(client)
    template = await save_template(client, source["id"], shot["id"], name="Chain bit")

    listed = (await client.get("/api/templates")).json()
    assert [t["name"] for t in listed] == ["Chain bit"]
    assert listed[0]["step_count"] == 2

    # A different project, with none of the source project's workflows.
    target = await make_project(client, "Target")
    other = (await client.post(f"/api/projects/{target['id']}/shots", json={"name": "S"})).json()
    placed = await client.post(
        f"/api/projects/{target['id']}/shots/{other['id']}/instances",
        json={"template_id": template["id"], "name": "Bit"},
    )
    assert placed.status_code == 201, placed.text

    # Placing brought the workflows along, so the target can actually run it.
    loaded = (await client.get(f"/api/projects/{target['id']}")).json()
    assert len(loaded["workflows"]) == 2
    assert len(loaded["shots"][0]["instances"]) == 1

    report = (
        await client.get(f"/api/projects/{target['id']}/shots/{other['id']}/validate")
    ).json()
    assert report["ok"] is True, report["issues"]


async def test_running_a_shot_expands_the_instance_it_contains(client):
    source, shot, _steps = await build_chain(client)
    template = await save_template(client, source["id"], shot["id"], name="Chain bit")

    target = await make_project(client, "Target")
    other = (await client.post(f"/api/projects/{target['id']}/shots", json={"name": "S"})).json()
    instance = (
        await client.post(
            f"/api/projects/{target['id']}/shots/{other['id']}/instances",
            json={"template_id": template["id"], "name": "Bit"},
        )
    ).json()

    run = await run_and_wait(client, target["id"], other["id"])
    assert run["status"] == "success", run.get("error")
    # Both inner steps ran, under ids scoped to the instance that owns them.
    assert len(run["step_runs"]) == 2
    assert all(sr["step_id"].startswith(f"{instance['id']}:") for sr in run["step_runs"])
    assert all(sr["status"] == "success" for sr in run["step_runs"])


async def test_an_instance_control_reaches_the_inner_step_at_run_time(client):
    source, shot, steps = await build_chain(client)
    # Pin the generator's prompt so the template promotes it as a control.
    await client.patch(
        f"/api/projects/{source['id']}/steps/{steps[0]['id']}", json={"exposed_params": ["prompt"]}
    )
    source = (await client.get(f"/api/projects/{source['id']}")).json()
    template = await save_template(client, source["id"], shot["id"], name="Chain bit")

    control = next(c for c in template["controls"] if c["shown"])
    assert control["inner_param"] == "prompt"

    target = await make_project(client, "Target")
    other = (await client.post(f"/api/projects/{target['id']}/shots", json={"name": "S"})).json()
    instance = (
        await client.post(
            f"/api/projects/{target['id']}/shots/{other['id']}/instances",
            json={"template_id": template["id"]},
        )
    ).json()
    await client.patch(
        f"/api/projects/{target['id']}/instances/{instance['id']}",
        json={"param_overrides": {control["key"]: "a hedgehog"}},
    )

    run = await run_and_wait(client, target["id"], other["id"])
    assert run["status"] == "success", run.get("error")
    caption = next(
        artifact
        for step_run in run["step_runs"]
        for artifact in step_run["outputs"]
        if artifact["port_key"] == "caption"
    )
    assert caption["meta"]["value"] == "a hedgehog"


async def test_the_placed_endpoint_describes_each_instance_surface(client):
    source, shot, _steps = await build_chain(client)
    template = await save_template(client, source["id"], shot["id"], name="Chain bit")
    target = await make_project(client, "Target")
    other = (await client.post(f"/api/projects/{target['id']}/shots", json={"name": "S"})).json()
    await client.post(
        f"/api/projects/{target['id']}/shots/{other['id']}/instances",
        json={"template_id": template["id"]},
    )

    placed = (
        await client.get(f"/api/projects/{target['id']}/shots/{other['id']}/placed")
    ).json()
    assert len(placed) == 1
    assert placed[0]["missing"] is False and placed[0]["stale"] is False
    # Everything the template does not consume itself comes out: both of the consumer's outputs, and the
    # generator's caption, which the chain never wired anywhere. Its image, which is wired, does not.
    outputs = {p["key"] for p in placed[0]["ports"] if p["direction"] == "out"}
    assert outputs == {"final", "echo", "caption"}
    assert "image" not in outputs


async def test_a_promoted_output_port_addresses_a_real_artifact(client):
    """The join the canvas and the inspector rely on to show a template's result.

    A run stores artifacts against the expanded step id; a promoted port names an inner key and an inner
    port. This asserts the two actually meet — without it a template can run perfectly and still look
    like it produced nothing.
    """
    source, shot, _steps = await build_chain(client)
    template = await save_template(client, source["id"], shot["id"], name="Chain bit")

    target = await make_project(client, "Target")
    other = (await client.post(f"/api/projects/{target['id']}/shots", json={"name": "S"})).json()
    instance = (
        await client.post(
            f"/api/projects/{target['id']}/shots/{other['id']}/instances",
            json={"template_id": template["id"]},
        )
    ).json()

    run = await run_and_wait(client, target["id"], other["id"])
    assert run["status"] == "success", run.get("error")

    placed = (
        await client.get(f"/api/projects/{target['id']}/shots/{other['id']}/placed")
    ).json()[0]
    by_step = {sr["step_id"]: sr for sr in run["step_runs"]}

    resolved = {}
    for port in (p for p in placed["ports"] if p["direction"] == "out"):
        step_run = by_step.get(f"{instance['id']}:{port['inner_key']}")
        assert step_run is not None, f"no run for the step behind {port['key']!r}"
        artifact = next(
            (a for a in step_run["outputs"] if a["port_key"] == port["inner_port"]), None
        )
        if artifact:
            resolved[port["key"]] = artifact["kind"]

    # Every output the node advertises resolved to something the run actually produced.
    assert resolved == {"final": "image", "echo": "string", "caption": "string"}


async def test_improving_a_template_reaches_a_placed_instance_after_a_sync(client):
    source, shot, steps = await build_chain(client)
    template = await save_template(client, source["id"], shot["id"], name="Chain bit")

    target = await make_project(client, "Target")
    other = (await client.post(f"/api/projects/{target['id']}/shots", json={"name": "S"})).json()
    instance = (
        await client.post(
            f"/api/projects/{target['id']}/shots/{other['id']}/instances",
            json={"template_id": template["id"]},
        )
    ).json()

    # The source shot grows a third step, and the template is saved over itself.
    third = (
        await client.post(
            f"/api/projects/{source['id']}/shots/{shot['id']}/steps",
            json={"workflow_id": steps[0]["workflow_id"], "name": "Extra"},
        )
    ).json()
    assert third["id"]
    updated = await save_template(
        client, source["id"], shot["id"], name="Chain bit", template_id=template["id"]
    )
    assert updated["revision"] == template["revision"] + 1

    placed = (
        await client.get(f"/api/projects/{target['id']}/shots/{other['id']}/placed")
    ).json()
    assert placed[0]["stale"] is True, "the instance should read as out of date"

    synced = await client.post(f"/api/projects/{target['id']}/instances/{instance['id']}/sync")
    assert synced.status_code == 200, synced.text
    assert synced.json()["instance"]["template_revision"] == updated["revision"]

    run = await run_and_wait(client, target["id"], other["id"])
    assert run["status"] == "success", run.get("error")
    assert len(run["step_runs"]) == 3, "the instance should now run the template's third step"


async def test_opening_a_template_gives_back_an_editable_shot(client):
    source, shot, _steps = await build_chain(client)
    template = await save_template(client, source["id"], shot["id"], name="Chain bit")

    target = await make_project(client, "Target")
    opened = await client.post(f"/api/projects/{target['id']}/templates/{template['id']}/edit")
    assert opened.status_code == 201, opened.text
    session = opened.json()

    # A real shot, with the template's contents and its workflows imported into this project.
    assert session["template_edit_id"] == template["id"]
    assert {s["name"] for s in session["steps"]} == {"Generate", "Consume"}
    assert len(session["links"]) == 1
    assert len((await client.get(f"/api/projects/{target['id']}")).json()["workflows"]) == 2

    # Opening again reuses the session rather than forking a second copy of it.
    again = await client.post(f"/api/projects/{target['id']}/templates/{template['id']}/edit")
    assert again.json()["id"] == session["id"]


async def test_editing_a_template_and_saving_it_reaches_placed_instances(client):
    source, shot, _steps = await build_chain(client)
    template = await save_template(client, source["id"], shot["id"], name="Chain bit")

    target = await make_project(client, "Target")
    other = (await client.post(f"/api/projects/{target['id']}/shots", json={"name": "S"})).json()
    instance = (
        await client.post(
            f"/api/projects/{target['id']}/shots/{other['id']}/instances",
            json={"template_id": template["id"]},
        )
    ).json()

    # Open the template, add a step to it, and save it back.
    session = (
        await client.post(f"/api/projects/{target['id']}/templates/{template['id']}/edit")
    ).json()
    workflow_id = session["steps"][0]["workflow_id"]
    await client.post(
        f"/api/projects/{target['id']}/shots/{session['id']}/steps",
        json={"workflow_id": workflow_id, "name": "Added while editing"},
    )
    saved = await client.post(
        f"/api/projects/{target['id']}/shots/{session['id']}/save-as-template", json={}
    )
    assert saved.status_code == 201, saved.text
    assert saved.json()["id"] == template["id"], "it should save over the template, not fork one"
    assert saved.json()["revision"] == template["revision"] + 1
    assert len(saved.json()["steps"]) == 3

    # The instance follows immediately — the user edited the template, so it is not "out of date".
    placed = (
        await client.get(f"/api/projects/{target['id']}/shots/{other['id']}/placed")
    ).json()[0]
    assert placed["stale"] is False
    assert placed["summary"]["step_count"] == 3

    run = await run_and_wait(client, target["id"], other["id"])
    assert run["status"] == "success", run.get("error")
    assert len(run["step_runs"]) == 3, "the added step should now run inside the placed instance"
    assert instance["id"]


async def test_saving_over_a_template_keeps_the_surface_choices(client):
    """Re-deriving promotions must not un-hide everything the user hid."""
    source, shot, steps = await build_chain(client)
    await client.patch(
        f"/api/projects/{source['id']}/steps/{steps[0]['id']}", json={"exposed_params": ["prompt"]}
    )
    template = await save_template(client, source["id"], shot["id"], name="Chain bit")

    control = next(c for c in template["controls"] if c["shown"])
    port = next(p for p in template["ports"] if p["direction"] == "out")
    await client.patch(
        f"/api/templates/{template['id']}/controls/{control['key']}",
        json={"shown": False, "label": "Renamed control"},
    )
    await client.patch(
        f"/api/templates/{template['id']}/ports/{port['key']}", json={"label": "Renamed port"},
    )

    # Save the source shot over it again, which re-derives every promotion from scratch.
    resaved = await save_template(
        client, source["id"], shot["id"], name="Chain bit", template_id=template["id"]
    )

    kept_control = next(c for c in resaved["controls"] if c["key"] == control["key"])
    assert kept_control["shown"] is False and kept_control["label"] == "Renamed control"
    kept_port = next(p for p in resaved["ports"] if p["key"] == port["key"])
    assert kept_port["label"] == "Renamed port"


async def test_a_template_session_stays_out_of_the_timeline(client):
    source, shot, _steps = await build_chain(client)
    await run_and_wait(client, source["id"], shot["id"])
    template = await save_template(client, source["id"], shot["id"], name="Chain bit")
    await client.post(f"/api/projects/{source['id']}/templates/{template['id']}/edit")

    timeline = (await client.post(f"/api/projects/{source['id']}/timeline/from-shots")).json()
    # One clip for the real shot, and nothing for the editing session.
    assert len(timeline["tracks"][0]["clips"]) == 1


async def test_closing_a_session_leaves_the_template_alone(client):
    source, shot, _steps = await build_chain(client)
    template = await save_template(client, source["id"], shot["id"], name="Chain bit")
    session = (
        await client.post(f"/api/projects/{source['id']}/templates/{template['id']}/edit")
    ).json()

    response = await client.delete(f"/api/projects/{source['id']}/templates/edit/{session['id']}")
    assert response.status_code == 204

    shots = (await client.get(f"/api/projects/{source['id']}/shots")).json()
    assert all(s["id"] != session["id"] for s in shots)
    assert (await client.get(f"/api/templates/{template['id']}")).status_code == 200


async def test_hiding_a_control_takes_it_off_the_node(client):
    source, shot, steps = await build_chain(client)
    await client.patch(
        f"/api/projects/{source['id']}/steps/{steps[0]['id']}", json={"exposed_params": ["prompt"]}
    )
    template = await save_template(client, source["id"], shot["id"], name="Chain bit")
    control = next(c for c in template["controls"] if c["shown"])

    response = await client.patch(
        f"/api/templates/{template['id']}/controls/{control['key']}",
        json={"shown": False, "label": "Prompt"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["revision"] == template["revision"] + 1
    assert not any(c["shown"] for c in response.json()["controls"])


async def test_deleting_an_instance_removes_its_links(client):
    source, shot, _steps = await build_chain(client)
    template = await save_template(client, source["id"], shot["id"], name="Chain bit")
    target = await make_project(client, "Target")
    other = (await client.post(f"/api/projects/{target['id']}/shots", json={"name": "S"})).json()
    instance = (
        await client.post(
            f"/api/projects/{target['id']}/shots/{other['id']}/instances",
            json={"template_id": template["id"]},
        )
    ).json()

    # Something downstream that actually takes an image, so the link is a legal one.
    consumer = await import_workflow(client, target["id"], "Consume", consumer_prompt())
    downstream = (
        await client.post(
            f"/api/projects/{target['id']}/shots/{other['id']}/steps",
            json={"workflow_id": consumer["id"]},
        )
    ).json()
    link = await client.post(
        f"/api/projects/{target['id']}/shots/{other['id']}/links",
        json={
            "from_step": instance["id"], "from_port": "final",
            "to_step": downstream["id"], "to_port": "image",
        },
    )
    assert link.status_code == 201, link.text

    await client.delete(f"/api/projects/{target['id']}/instances/{instance['id']}")
    shots = (await client.get(f"/api/projects/{target['id']}/shots")).json()
    assert shots[0]["instances"] == []
    assert shots[0]["links"] == []


async def test_deleting_a_template_leaves_a_clear_error_rather_than_a_broken_shot(client):
    source, shot, _steps = await build_chain(client)
    template = await save_template(client, source["id"], shot["id"], name="Chain bit")
    target = await make_project(client, "Target")
    other = (await client.post(f"/api/projects/{target['id']}/shots", json={"name": "S"})).json()
    await client.post(
        f"/api/projects/{target['id']}/shots/{other['id']}/instances",
        json={"template_id": template["id"]},
    )

    assert (await client.delete(f"/api/templates/{template['id']}")).status_code == 204

    report = (
        await client.get(f"/api/projects/{target['id']}/shots/{other['id']}/validate")
    ).json()
    assert report["ok"] is False
    assert any("not in the library" in issue["message"] for issue in report["issues"])


# -- value nodes -----------------------------------------------------------------------------------------


async def test_value_node_round_trip(client):
    project, shot, steps = await build_chain(client)

    created = await client.post(
        f"/api/projects/{project['id']}/shots/{shot['id']}/nodes",
        json={"kind": "string", "name": "Shared caption", "value": "hello"},
    )
    assert created.status_code == 201, created.text
    node = created.json()
    assert node["value"] == "hello"

    patched = await client.patch(
        f"/api/projects/{project['id']}/nodes/{node['id']}", json={"value": "goodbye"}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["value"] == "goodbye"

    shots = (await client.get(f"/api/projects/{project['id']}/shots")).json()
    assert [n["id"] for n in shots[0]["nodes"]] == [node["id"]]


async def test_a_new_value_node_starts_with_a_usable_value(client):
    project = await make_project(client)
    shot = (await client.post(f"/api/projects/{project['id']}/shots", json={"name": "S"})).json()

    for kind, expected in (("string", ""), ("int", 0), ("float", 0.0), ("boolean", False)):
        created = await client.post(
            f"/api/projects/{project['id']}/shots/{shot['id']}/nodes", json={"kind": kind}
        )
        assert created.status_code == 201, created.text
        assert created.json()["value"] == expected


async def test_a_value_node_can_feed_a_step_input(client):
    project, shot, steps = await build_chain(client)
    node = (
        await client.post(
            f"/api/projects/{project['id']}/shots/{shot['id']}/nodes",
            json={"kind": "string", "value": "wired"},
        )
    ).json()

    link = await client.post(
        f"/api/projects/{project['id']}/shots/{shot['id']}/links",
        json={
            "from_step": node["id"], "from_port": "value",
            "to_step": steps[1]["id"], "to_port": "caption",
        },
    )
    assert link.status_code == 201, link.text

    report = (
        await client.get(f"/api/projects/{project['id']}/shots/{shot['id']}/validate")
    ).json()
    assert report["ok"] is True, report["issues"]


async def test_a_value_node_of_the_wrong_kind_is_refused(client):
    project, shot, steps = await build_chain(client)
    node = (
        await client.post(
            f"/api/projects/{project['id']}/shots/{shot['id']}/nodes", json={"kind": "boolean"}
        )
    ).json()

    # The consumer's `image` input takes an image; a boolean is not one, and never converts to one.
    response = await client.post(
        f"/api/projects/{project['id']}/shots/{shot['id']}/links",
        json={
            "from_step": node["id"], "from_port": "value",
            "to_step": steps[1]["id"], "to_port": "image",
        },
    )
    assert response.status_code == 422, response.text
    assert "Cannot connect a boolean output to a image input" in response.json()["message"]


async def test_deleting_a_value_node_removes_its_links(client):
    project, shot, steps = await build_chain(client)
    node = (
        await client.post(
            f"/api/projects/{project['id']}/shots/{shot['id']}/nodes",
            json={"kind": "string", "value": "x"},
        )
    ).json()
    await client.post(
        f"/api/projects/{project['id']}/shots/{shot['id']}/links",
        json={
            "from_step": node["id"], "from_port": "value",
            "to_step": steps[1]["id"], "to_port": "caption",
        },
    )

    response = await client.delete(f"/api/projects/{project['id']}/nodes/{node['id']}")
    assert response.status_code == 204

    shots = (await client.get(f"/api/projects/{project['id']}/shots")).json()
    assert shots[0]["nodes"] == []
    assert all(link["from_step"] != node["id"] for link in shots[0]["links"])


async def test_duplicating_a_shot_gives_its_value_nodes_fresh_ids(client):
    project, shot, steps = await build_chain(client)
    node = (
        await client.post(
            f"/api/projects/{project['id']}/shots/{shot['id']}/nodes",
            json={"kind": "string", "value": "x"},
        )
    ).json()
    await client.post(
        f"/api/projects/{project['id']}/shots/{shot['id']}/links",
        json={
            "from_step": node["id"], "from_port": "value",
            "to_step": steps[1]["id"], "to_port": "caption",
        },
    )

    copy = (
        await client.post(f"/api/projects/{project['id']}/shots/{shot['id']}/duplicate")
    ).json()

    assert len(copy["nodes"]) == 1
    copied = copy["nodes"][0]
    assert copied["id"] != node["id"], "the copy shares an id with the original"
    # ...and the copied link points at the copy, not back at the original node.
    assert [link["from_step"] for link in copy["links"] if link["from_port"] == "value"] == [
        copied["id"]
    ]


# -- parameters pinned to the canvas node ----------------------------------------------------------------


async def test_pinning_a_parameter_to_the_node(client):
    project = await make_project(client)
    workflow = await import_workflow(client, project["id"], "Generate", generator_prompt())
    shot = (await client.post(f"/api/projects/{project['id']}/shots", json={"name": "S"})).json()
    step = (
        await client.post(
            f"/api/projects/{project['id']}/shots/{shot['id']}/steps",
            json={"workflow_id": workflow["id"]},
        )
    ).json()
    assert step["exposed_params"] == []

    updated = await client.patch(
        f"/api/projects/{project['id']}/steps/{step['id']}", json={"exposed_params": ["prompt"]}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["exposed_params"] == ["prompt"]

    # Replaced wholesale, so unpinning is just a shorter list.
    cleared = await client.patch(
        f"/api/projects/{project['id']}/steps/{step['id']}", json={"exposed_params": []}
    )
    assert cleared.json()["exposed_params"] == []


# -- re-syncing a workflow edited inside ComfyUI ---------------------------------------------------------


def _comfy_graph(*, with_output: bool) -> dict:
    """A small LiteGraph document, optionally with our output node attached."""
    nodes = [
        {"id": 1, "type": "WSStringInput", "widgets_values": ["prompt", "a cat"]},
        {"id": 2, "type": "EmptyImage", "widgets_values": [64, 64],
         "outputs": [{"name": "IMAGE", "type": "IMAGE"}]},
    ]
    links: list[list] = []
    if with_output:
        nodes.append(
            {"id": 3, "type": "WSImageOutput", "widgets_values": ["image", "png", ""],
             "inputs": [{"name": "image", "type": "IMAGE", "link": 1}]}
        )
        links.append([1, 2, 0, 3, 0, "IMAGE"])
    return {"nodes": nodes, "links": links}


async def _import_from_comfy(client, fake_comfy, project_id, graph, path="edited.json") -> dict:
    workflows_dir = fake_comfy.root / "user" / "default" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    (workflows_dir / path).write_text(json.dumps(graph))
    response = await client.post(
        f"/api/comfy/projects/{project_id}/import", json={"path": path}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_sync_picks_up_an_output_node_added_in_comfyui(client, fake_comfy):
    """The regression this endpoint exists for.

    ComfyUI's own Ctrl+S writes its user directory and notifies nobody, so a workflow imported before the
    user attached an output node kept reporting no outputs — while ``last_synced`` claimed it was current.
    """
    project = await make_project(client)
    workflow = await _import_from_comfy(
        client, fake_comfy, project["id"], _comfy_graph(with_output=False)
    )
    assert {p["key"] for p in workflow["ports"]} == {"prompt"}

    # The user opens it in ComfyUI, adds the output node and presses Ctrl+S.
    (fake_comfy.root / "user" / "default" / "workflows" / "edited.json").write_text(
        json.dumps(_comfy_graph(with_output=True))
    )

    synced = await client.post(
        f"/api/projects/{project['id']}/workflows/{workflow['id']}/rediscover"
    )
    assert synced.status_code == 200, synced.text
    ports = {p["key"]: p["direction"] for p in synced.json()["ports"]}
    assert ports == {"prompt": "in", "image": "out"}

    # ...and the stored graphs are the ones ComfyUI now has, not the ones we imported.
    stored = (
        await client.get(f"/api/projects/{project['id']}/workflows/{workflow['id']}/graph?fmt=api")
    ).json()
    assert any(n["class_type"] == "WSImageOutput" for n in stored.values())


async def test_sync_without_pull_only_re_reads_the_stored_graph(client, fake_comfy):
    project = await make_project(client)
    workflow = await _import_from_comfy(
        client, fake_comfy, project["id"], _comfy_graph(with_output=False)
    )
    (fake_comfy.root / "user" / "default" / "workflows" / "edited.json").write_text(
        json.dumps(_comfy_graph(with_output=True))
    )

    synced = await client.post(
        f"/api/projects/{project['id']}/workflows/{workflow['id']}/rediscover?pull=false"
    )
    assert synced.status_code == 200, synced.text
    assert {p["key"] for p in synced.json()["ports"]} == {"prompt"}


async def test_sync_falls_back_to_the_stored_graph_when_comfyui_is_down(client, fake_comfy):
    project = await make_project(client)
    workflow = await _import_from_comfy(
        client, fake_comfy, project["id"], _comfy_graph(with_output=True)
    )
    await fake_comfy.stop()

    synced = await client.post(
        f"/api/projects/{project['id']}/workflows/{workflow['id']}/rediscover"
    )
    assert synced.status_code == 200, synced.text
    body = synced.json()
    # The ports we already knew about survive...
    assert {p["key"] for p in body["ports"]} == {"prompt", "image"}
    # ...and the user is told this is not a fresh read.
    assert any("could not be read" in w for w in body["warnings"])


async def test_sync_drops_links_to_a_port_deleted_in_comfyui(client, fake_comfy):
    project = await make_project(client)
    source = await _import_from_comfy(
        client, fake_comfy, project["id"], _comfy_graph(with_output=True), path="source.json"
    )
    target = await import_workflow(client, project["id"], "Consume", consumer_prompt())

    shot = (
        await client.post(f"/api/projects/{project['id']}/shots", json={"name": "Shot 1"})
    ).json()
    steps = []
    for workflow in (source, target):
        steps.append(
            (
                await client.post(
                    f"/api/projects/{project['id']}/shots/{shot['id']}/steps",
                    json={"workflow_id": workflow["id"]},
                )
            ).json()
        )
    link = await client.post(
        f"/api/projects/{project['id']}/shots/{shot['id']}/links",
        json={
            "from_step": steps[0]["id"], "from_port": "image",
            "to_step": steps[1]["id"], "to_port": "image",
        },
    )
    assert link.status_code == 201, link.text

    # The user removes the output node in ComfyUI and saves.
    (fake_comfy.root / "user" / "default" / "workflows" / "source.json").write_text(
        json.dumps(_comfy_graph(with_output=False))
    )
    synced = await client.post(
        f"/api/projects/{project['id']}/workflows/{source['id']}/rediscover"
    )
    assert synced.status_code == 200, synced.text
    assert {p["key"] for p in synced.json()["ports"]} == {"prompt"}

    shots = (await client.get(f"/api/projects/{project['id']}/shots")).json()
    assert shots[0]["links"] == [], "a link to a port that no longer exists must not survive"


async def test_bridge_serves_an_api_only_workflow_as_a_prompt(client):
    """A workflow imported in API format has no LiteGraph document, but ComfyUI can build one."""
    project = await make_project(client)
    workflow = await import_workflow(client, project["id"], "Generate", generator_prompt())

    token = (
        await client.post(
            f"/api/projects/{project['id']}/workflows/{workflow['id']}/open-in-comfy"
        )
    ).json()["token"]

    response = await client.get(
        f"/api/bridge/workflow/{workflow['id']}", headers={"X-WebStudio-Token": token}
    )
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["has_ui_graph"] is False
    assert payload["workflow"] is None
    assert payload["prompt"]["1"]["class_type"] == "WSStringInput"


async def test_bridge_prefers_the_ui_graph_when_one_exists(client, fake_comfy):
    import json as _json

    project = await make_project(client)
    workflows_dir = fake_comfy.root / "user" / "default" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    (workflows_dir / "ui.json").write_text(
        _json.dumps({"nodes": [{"id": 1, "type": "EmptyImage", "widgets_values": [64, 64]}], "links": []})
    )
    workflow = (
        await client.post(f"/api/comfy/projects/{project['id']}/import", json={"path": "ui.json"})
    ).json()

    token = (
        await client.post(
            f"/api/projects/{project['id']}/workflows/{workflow['id']}/open-in-comfy"
        )
    ).json()["token"]
    payload = (
        await client.get(
            f"/api/bridge/workflow/{workflow['id']}", headers={"X-WebStudio-Token": token}
        )
    ).json()

    assert payload["has_ui_graph"] is True
    assert payload["workflow"]["nodes"]


async def _sync_from_comfy(client, project_id: str, workflow_id: str, prompt: dict) -> dict:
    """What the bridge extension does when ComfyUI saves: push the edited prompt back."""
    opened = (
        await client.post(f"/api/projects/{project_id}/workflows/{workflow_id}/open-in-comfy")
    ).json()
    response = await client.post(
        "/api/bridge/workflow",
        headers={"X-WebStudio-Token": opened["token"]},
        json={"step_id": workflow_id, "workflow": {"nodes": [], "links": []}, "prompt": prompt},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_a_value_changed_in_comfyui_reaches_the_steps_using_it(client):
    """The whole point of syncing: change it in ComfyUI, see it in the framework.

    A step keeps an override for every parameter it has been given a value for, and an override beats the
    workflow's default — so without this, editing a value in ComfyUI updated the workflow and changed
    nothing the user could actually see.
    """
    project = await make_project(client)
    workflow = await import_workflow(client, project["id"], "Generate", generator_prompt())
    shot = (await client.post(f"/api/projects/{project['id']}/shots", json={"name": "S"})).json()
    step = (
        await client.post(
            f"/api/projects/{project['id']}/shots/{shot['id']}/steps",
            json={"workflow_id": workflow["id"]},
        )
    ).json()

    # The step is holding the value the workflow had when it was placed — a copy, not a decision.
    await client.patch(
        f"/api/projects/{project['id']}/steps/{step['id']}",
        json={"param_overrides": {"prompt": "a cat"}},
    )

    edited = generator_prompt()
    edited["1"]["inputs"]["value"] = "a dog"
    result = await _sync_from_comfy(client, project["id"], workflow["id"], edited)

    assert result["adopted_values"] == ["prompt"]
    updated = (await client.get(f"/api/projects/{project['id']}")).json()
    step_now = updated["shots"][0]["steps"][0]
    assert "prompt" not in step_now["param_overrides"], "the stale copy should have been let go"

    stored = (
        await client.get(f"/api/projects/{project['id']}/workflows/{workflow['id']}")
    ).json()
    assert next(p["default"] for p in stored["params"] if p["key"] == "prompt") == "a dog"


async def test_a_value_the_user_set_themselves_is_not_overwritten(client):
    """Two steps sharing a workflow are meant to be able to differ, so a deliberate value stays put."""
    project = await make_project(client)
    workflow = await import_workflow(client, project["id"], "Generate", generator_prompt())
    shot = (await client.post(f"/api/projects/{project['id']}/shots", json={"name": "S"})).json()
    step = (
        await client.post(
            f"/api/projects/{project['id']}/shots/{shot['id']}/steps",
            json={"workflow_id": workflow["id"]},
        )
    ).json()
    await client.patch(
        f"/api/projects/{project['id']}/steps/{step['id']}",
        json={"param_overrides": {"prompt": "this step is different on purpose"}},
    )

    edited = generator_prompt()
    edited["1"]["inputs"]["value"] = "a dog"
    result = await _sync_from_comfy(client, project["id"], workflow["id"], edited)

    assert result["kept_values"] == ["prompt"]
    assert result["adopted_values"] == []
    updated = (await client.get(f"/api/projects/{project['id']}")).json()
    assert updated["shots"][0]["steps"][0]["param_overrides"]["prompt"] == (
        "this step is different on purpose"
    )


async def test_a_step_with_no_value_of_its_own_needs_no_adopting(client):
    project = await make_project(client)
    workflow = await import_workflow(client, project["id"], "Generate", generator_prompt())
    shot = (await client.post(f"/api/projects/{project['id']}/shots", json={"name": "S"})).json()
    await client.post(
        f"/api/projects/{project['id']}/shots/{shot['id']}/steps",
        json={"workflow_id": workflow["id"]},
    )

    edited = generator_prompt()
    edited["1"]["inputs"]["value"] = "a dog"
    result = await _sync_from_comfy(client, project["id"], workflow["id"], edited)

    # Nothing to adopt: with no override it already reads through to the workflow's new value.
    assert result["adopted_values"] == []
    assert result["kept_values"] == []


async def _timeline_project(client):
    """A project with one run shot, so the timeline has something real to place."""
    project = await make_project(client)
    workflow = await import_workflow(client, project["id"], "Generate", generator_prompt())
    shot = (await client.post(f"/api/projects/{project['id']}/shots", json={"name": "Wide"})).json()
    await client.post(
        f"/api/projects/{project['id']}/shots/{shot['id']}/steps",
        json={"workflow_id": workflow["id"]},
    )
    await run_and_wait(client, project["id"], shot["id"])
    return project["id"], shot["id"]


async def test_dropping_a_shot_onto_the_timeline_places_its_output(client):
    """What a drag from the shot list onto the timeline does."""
    project_id, shot_id = await _timeline_project(client)

    response = await client.post(
        f"/api/projects/{project_id}/timeline/from-shot", json={"shot_id": shot_id}
    )
    assert response.status_code == 201, response.text
    timeline = response.json()

    video = next(t for t in timeline["tracks"] if t["kind"] == "video")
    assert [c["name"] for c in video["clips"]] == ["Wide"]
    assert video["clips"][0]["duration"] > 0
    assert video["clips"][0]["source"]["shot_id"] == shot_id


async def test_a_second_drop_lands_after_the_first(client):
    project_id, shot_id = await _timeline_project(client)
    for _ in range(2):
        await client.post(
            f"/api/projects/{project_id}/timeline/from-shot", json={"shot_id": shot_id}
        )

    timeline = (await client.get(f"/api/projects/{project_id}/timeline")).json()
    clips = next(t for t in timeline["tracks"] if t["kind"] == "video")["clips"]
    assert len(clips) == 2
    assert clips[1]["start"] == pytest.approx(clips[0]["start"] + clips[0]["duration"])


async def test_dropping_at_a_position_puts_it_there(client):
    project_id, shot_id = await _timeline_project(client)
    await client.post(
        f"/api/projects/{project_id}/timeline/from-shot",
        json={"shot_id": shot_id, "start": 5.0},
    )
    timeline = (await client.get(f"/api/projects/{project_id}/timeline")).json()
    clips = next(t for t in timeline["tracks"] if t["kind"] == "video")["clips"]
    assert clips[0]["start"] == pytest.approx(5.0)


async def test_a_shot_with_nothing_to_show_cannot_be_placed(client):
    """No steps at all means no output port to point a clip at — not even a pending one."""
    project = await make_project(client)
    shot = (await client.post(f"/api/projects/{project['id']}/shots", json={"name": "Empty"})).json()

    response = await client.post(
        f"/api/projects/{project['id']}/timeline/from-shot", json={"shot_id": shot["id"]}
    )
    assert response.status_code == 422
    assert "no image, video or audio output" in response.text.lower()


async def test_a_shot_can_be_cut_in_before_it_has_been_run(client):
    """The timeline is a plan as much as an assembly: place it now, it fills in when it runs."""
    project = await make_project(client)
    workflow = await import_workflow(client, project["id"], "Generate", generator_prompt())
    shot = (await client.post(f"/api/projects/{project['id']}/shots", json={"name": "Later"})).json()
    await client.post(
        f"/api/projects/{project['id']}/shots/{shot['id']}/steps",
        json={"workflow_id": workflow["id"]},
    )

    response = await client.post(
        f"/api/projects/{project['id']}/timeline/from-shot", json={"shot_id": shot["id"]}
    )
    assert response.status_code == 201, response.text
    clip = next(t for t in response.json()["tracks"] if t["kind"] == "video")["clips"][0]
    assert clip["source"]["shot_id"] == shot["id"]

    # It reads as pending rather than broken, so the UI can show it as waiting for its shot.
    resolved = (await client.get(f"/api/projects/{project['id']}/timeline/resolved")).json()
    assert resolved["clips"][0]["error"] is not None

    # And once the shot runs, the same clip resolves without being touched.
    await run_and_wait(client, project["id"], shot["id"])
    resolved = (await client.get(f"/api/projects/{project['id']}/timeline/resolved")).json()
    assert resolved["clips"][0]["error"] is None
    assert resolved["clips"][0]["artifacts"]


async def test_a_track_can_be_soloed_and_panned(client):
    project_id, shot_id = await _timeline_project(client)
    await client.post(f"/api/projects/{project_id}/timeline/from-shot", json={"shot_id": shot_id})
    timeline = (await client.get(f"/api/projects/{project_id}/timeline")).json()
    track_id = timeline["tracks"][0]["id"]

    updated = (
        await client.patch(
            f"/api/projects/{project_id}/timeline/tracks/{track_id}",
            json={"solo": True, "volume": 0.5, "pan": -0.5},
        )
    ).json()
    assert updated["solo"] is True
    assert updated["volume"] == pytest.approx(0.5)
    assert updated["pan"] == pytest.approx(-0.5)


async def test_out_of_range_mixer_values_are_clamped(client):
    project_id, shot_id = await _timeline_project(client)
    await client.post(f"/api/projects/{project_id}/timeline/from-shot", json={"shot_id": shot_id})
    timeline = (await client.get(f"/api/projects/{project_id}/timeline")).json()
    track_id = timeline["tracks"][0]["id"]

    # A negative gain would invert the phase and a pan past the ends leaves the pan law's domain.
    updated = (
        await client.patch(
            f"/api/projects/{project_id}/timeline/tracks/{track_id}",
            json={"volume": -3, "pan": 9},
        )
    ).json()
    assert updated["volume"] == pytest.approx(0.0)
    assert updated["pan"] == pytest.approx(1.0)


def multi_output_prompt() -> dict:
    """One step producing three outputs — an image, a bigger image, and a caption."""
    return {
        "1": {"class_type": "WSStringInput", "inputs": {"port_name": "caption", "value": "hello"}},
        "2": {"class_type": "EmptyImage",
              "inputs": {"width": 32, "height": 32, "batch_size": 1, "color": 0x2C6E9B}},
        "3": {"class_type": "WSImageOutput",
              "inputs": {"image": ["2", 0], "port_name": "picture", "format": "png", "run_key": ""}},
        "4": {"class_type": "WSTextOutput",
              "inputs": {"value": ["1", 0], "port_name": "caption_out", "run_key": ""}},
    }


async def test_a_clip_can_be_pointed_at_a_different_output(client):
    """A step with several outputs is placed on one of them; which one is an editing decision."""
    project = await make_project(client)
    workflow = await import_workflow(client, project["id"], "Multi", multi_output_prompt())
    shot = (await client.post(f"/api/projects/{project['id']}/shots", json={"name": "Wide"})).json()
    await client.post(
        f"/api/projects/{project['id']}/shots/{shot['id']}/steps",
        json={"workflow_id": workflow["id"]},
    )
    await run_and_wait(client, project["id"], shot["id"])
    await client.post(
        f"/api/projects/{project['id']}/timeline/from-shot", json={"shot_id": shot["id"]}
    )

    timeline = (await client.get(f"/api/projects/{project['id']}/timeline")).json()
    track = timeline["tracks"][0]
    clip = track["clips"][0]
    assert clip["source"]["port_key"] == "picture"

    updated = (
        await client.patch(
            f"/api/projects/{project['id']}/timeline/tracks/{track['id']}/clips/{clip['id']}",
            json={"source": {**clip["source"], "port_key": "caption_out"}},
        )
    ).json()
    assert updated["source"]["port_key"] == "caption_out"
    # Still a source, not the raw dict the browser sent — patching used to skip validation entirely.
    assert set(updated["source"]) == {"kind", "shot_id", "step_id", "port_key", "asset_id"}


async def test_a_nonsense_clip_patch_is_refused_rather_than_stored(client):
    project = await make_project(client)
    track = (
        await client.post(
            f"/api/projects/{project['id']}/timeline/tracks", json={"kind": "video", "name": "V"}
        )
    ).json()
    clip = (
        await client.post(
            f"/api/projects/{project['id']}/timeline/tracks/{track['id']}/clips",
            json={"name": "c", "start": 0},
        )
    ).json()

    response = await client.patch(
        f"/api/projects/{project['id']}/timeline/tracks/{track['id']}/clips/{clip['id']}",
        json={"transform": {"fit": "nonsense"}},
    )
    assert response.status_code == 422, response.text

    unchanged = (await client.get(f"/api/projects/{project['id']}/timeline")).json()
    assert unchanged["tracks"][0]["clips"][0]["transform"]["fit"] == "contain"


# -- a workflow is re-read from ComfyUI before it is used ------------------------------------------------


def _comfy_graph_with_prompt(default: str) -> dict:
    """The same little graph, with a chosen default sitting on its string input."""
    return {
        "nodes": [
            {"id": 1, "type": "WSStringInput", "widgets_values": ["prompt", default]},
            {"id": 2, "type": "EmptyImage", "widgets_values": [64, 64],
             "outputs": [{"name": "IMAGE", "type": "IMAGE"}]},
            {"id": 3, "type": "WSImageOutput", "widgets_values": ["image", "png", ""],
             "inputs": [{"name": "image", "type": "IMAGE", "link": 1}]},
        ],
        "links": [[1, 2, 0, 3, 0, "IMAGE"]],
    }


async def test_placing_a_step_re_reads_the_workflow_from_comfyui(client, fake_comfy):
    """The regression: a step placed today ran the graph as it was on the day it was imported.

    ComfyUI's Ctrl+S writes its own directory and tells nobody, so choosing a different checkpoint there
    left our copy — and therefore every step placed afterwards — on the old one, with nothing saying so.
    """
    project = await make_project(client)
    workflow = await _import_from_comfy(
        client, fake_comfy, project["id"], _comfy_graph_with_prompt("as imported")
    )
    assert [p["default"] for p in workflow["params"] if p["key"] == "prompt"] == ["as imported"]

    # The user changes it in ComfyUI and presses Ctrl+S. Nothing tells us.
    (fake_comfy.root / "user" / "default" / "workflows" / "edited.json").write_text(
        json.dumps(_comfy_graph_with_prompt("chosen in comfyui"))
    )

    shot = (
        await client.post(f"/api/projects/{project['id']}/shots", json={"name": "Shot"})
    ).json()
    placed = await client.post(
        f"/api/projects/{project['id']}/shots/{shot['id']}/steps",
        json={"workflow_id": workflow["id"]},
    )
    assert placed.status_code == 201, placed.text

    after = (await client.get(f"/api/projects/{project['id']}/workflows")).json()[0]
    assert [p["default"] for p in after["params"] if p["key"] == "prompt"] == ["chosen in comfyui"]


async def test_placing_a_step_still_works_when_comfyui_is_down(client, fake_comfy):
    # Syncing is a courtesy, not a precondition. An unreachable ComfyUI must not stop someone building.
    project = await make_project(client)
    workflow = await _import_from_comfy(
        client, fake_comfy, project["id"], _comfy_graph_with_prompt("as imported")
    )
    await fake_comfy.stop()

    shot = (
        await client.post(f"/api/projects/{project['id']}/shots", json={"name": "Shot"})
    ).json()
    placed = await client.post(
        f"/api/projects/{project['id']}/shots/{shot['id']}/steps",
        json={"workflow_id": workflow["id"]},
    )
    assert placed.status_code == 201, placed.text


async def test_a_step_that_never_set_a_value_follows_the_new_default(client, fake_comfy):
    project = await make_project(client)
    workflow = await _import_from_comfy(
        client, fake_comfy, project["id"], _comfy_graph_with_prompt("as imported")
    )
    shot = (
        await client.post(f"/api/projects/{project['id']}/shots", json={"name": "Shot"})
    ).json()
    step = (
        await client.post(
            f"/api/projects/{project['id']}/shots/{shot['id']}/steps",
            json={"workflow_id": workflow["id"]},
        )
    ).json()
    assert step["param_overrides"] == {}

    (fake_comfy.root / "user" / "default" / "workflows" / "edited.json").write_text(
        json.dumps(_comfy_graph_with_prompt("chosen in comfyui"))
    )
    # Placing a second step is what notices; the first one has no value of its own, so it follows too.
    await client.post(
        f"/api/projects/{project['id']}/shots/{shot['id']}/steps",
        json={"workflow_id": workflow["id"]},
    )

    after = (await client.get(f"/api/projects/{project['id']}/workflows")).json()[0]
    assert [p["default"] for p in after["params"] if p["key"] == "prompt"] == ["chosen in comfyui"]


async def test_a_value_somebody_set_by_hand_survives_the_re_read(client, fake_comfy):
    # Two steps sharing a workflow are meant to be able to differ; a re-read must not flatten that.
    project = await make_project(client)
    workflow = await _import_from_comfy(
        client, fake_comfy, project["id"], _comfy_graph_with_prompt("as imported")
    )
    shot = (
        await client.post(f"/api/projects/{project['id']}/shots", json={"name": "Shot"})
    ).json()
    step = (
        await client.post(
            f"/api/projects/{project['id']}/shots/{shot['id']}/steps",
            json={"workflow_id": workflow["id"]},
        )
    ).json()
    await client.patch(
        f"/api/projects/{project['id']}/steps/{step['id']}",
        json={"param_overrides": {"prompt": "mine, deliberately"}},
    )

    (fake_comfy.root / "user" / "default" / "workflows" / "edited.json").write_text(
        json.dumps(_comfy_graph_with_prompt("chosen in comfyui"))
    )
    await client.post(
        f"/api/projects/{project['id']}/shots/{shot['id']}/steps",
        json={"workflow_id": workflow["id"]},
    )

    kept = (await client.get(f"/api/projects/{project['id']}/shots/{shot['id']}")).json()
    assert kept["steps"][0]["param_overrides"]["prompt"] == "mine, deliberately"
