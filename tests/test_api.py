"""API tests driving the real FastAPI app against the fake ComfyUI.

These walk the flow a user actually takes: create a project, import workflows, wire steps together, run,
preview, cut a timeline, render, export and re-import.
"""

from __future__ import annotations

import asyncio
import io
import json

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
