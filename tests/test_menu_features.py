"""Undo/redo and plugin packs — the backend behind the File, Edit and Plugins menus."""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from comfywebstudio.core.errors import Conflict, ValidationFailed
from comfywebstudio.core.history import ProjectHistory
from comfywebstudio.core.plugins import PluginStore

from .test_api import build_chain, import_workflow, make_project
from .test_execution import generator_prompt

# -- history unit ---------------------------------------------------------------------------------------


def test_history_round_trips_snapshots():
    history = ProjectHistory(depth=5)
    assert not history.can_undo("p")

    history.record("p", {"name": "one"})
    history.record("p", {"name": "two"})
    assert history.depths("p") == {"undo": 2, "redo": 0}

    assert history.undo("p", {"name": "three"}) == {"name": "two"}
    assert history.depths("p") == {"undo": 1, "redo": 1}
    assert history.redo("p", {"name": "two"}) == {"name": "three"}


def test_history_ignores_a_save_that_changed_nothing_but_the_timestamp():
    history = ProjectHistory()
    history.record("p", {"name": "one", "modified": "t1"})
    history.record("p", {"name": "one", "modified": "t2"})
    assert history.depths("p")["undo"] == 1


def test_a_new_edit_discards_the_redo_branch():
    history = ProjectHistory()
    history.record("p", {"n": 1})
    history.undo("p", {"n": 2})
    assert history.can_redo("p")

    history.record("p", {"n": 3})
    assert not history.can_redo("p"), "a new edit must invalidate redo"


def test_history_is_bounded():
    history = ProjectHistory(depth=3)
    for index in range(10):
        history.record("p", {"n": index})
    assert history.depths("p")["undo"] == 3


def test_suspension_stops_recording():
    history = ProjectHistory()
    with history.suspend("p"):
        history.record("p", {"n": 1})
    assert history.depths("p")["undo"] == 0


# -- history through the API ----------------------------------------------------------------------------


async def test_undo_and_redo_a_shot_rename(client):
    project, shot, _steps = await build_chain(client)

    assert (await client.get(f"/api/projects/{project['id']}/history")).json() == {
        "undo": (await client.get(f"/api/projects/{project['id']}/history")).json()["undo"],
        "redo": 0,
    }

    await client.patch(
        f"/api/projects/{project['id']}/shots/{shot['id']}", json={"name": "Renamed"}
    )
    assert (await client.get(f"/api/projects/{project['id']}/shots/{shot['id']}")).json()[
        "name"
    ] == "Renamed"

    undone = (await client.post(f"/api/projects/{project['id']}/undo")).json()
    assert undone["shots"][0]["name"] == "Shot 1"

    redone = (await client.post(f"/api/projects/{project['id']}/redo")).json()
    assert redone["shots"][0]["name"] == "Renamed"


async def test_undo_restores_a_deleted_step(client):
    project, shot, steps = await build_chain(client)
    await client.delete(f"/api/projects/{project['id']}/steps/{steps[0]['id']}")

    reduced = (await client.get(f"/api/projects/{project['id']}/shots/{shot['id']}")).json()
    assert len(reduced["steps"]) == 1 and reduced["links"] == []

    restored = (await client.post(f"/api/projects/{project['id']}/undo")).json()
    assert len(restored["shots"][0]["steps"]) == 2
    assert restored["shots"][0]["links"], "the link should come back with the step"


async def test_pasting_a_step_is_a_single_undo_step(client):
    """Creating a step with its name and parameters is one save, so one Ctrl+Z removes the whole paste."""
    project, shot, steps = await build_chain(client)
    before = (await client.get(f"/api/projects/{project['id']}/history")).json()["undo"]

    response = await client.post(
        f"/api/projects/{project['id']}/shots/{shot['id']}/steps",
        json={
            "workflow_id": steps[0]["workflow_id"],
            "name": "Generate copy",
            "param_overrides": {"prompt": "a fox"},
            "ui_pos": {"x": 10, "y": 10},
        },
    )
    assert response.status_code == 201
    created = response.json()
    assert created["name"] == "Generate copy"
    assert created["param_overrides"] == {"prompt": "a fox"}

    after = (await client.get(f"/api/projects/{project['id']}/history")).json()["undo"]
    assert after == before + 1, "the paste must produce exactly one undo entry"

    undone = (await client.post(f"/api/projects/{project['id']}/undo")).json()
    assert len(undone["shots"][0]["steps"]) == 2


async def test_undo_with_nothing_to_undo_is_a_clean_error(client):
    project = await make_project(client)
    response = await client.post(f"/api/projects/{project['id']}/undo")
    assert response.status_code == 422
    assert "nothing to undo" in response.json()["message"]


# -- plugins --------------------------------------------------------------------------------------------


@pytest.fixture
def plugin_store(store, tmp_path) -> PluginStore:
    return PluginStore(tmp_path / "plugins", store)


def test_build_and_apply_a_plugin(store, project, plugin_store, tmp_path):
    workflow_id = next(iter(project.workflows))
    store.write_workflow(project.id, workflow_id, "api", {"1": {"class_type": "X", "inputs": {}}})
    for other in project.workflows:
        if other != workflow_id:
            store.write_workflow(project.id, other, "api", {"1": {"class_type": "Y", "inputs": {}}})

    archive = plugin_store.build(
        project,
        tmp_path / "pack.cwsplugin",
        name="Test Pack",
        workflow_ids=list(project.workflows),
        shot_ids=[project.shots[0].id],
        author="tester",
    )
    assert archive.is_file()

    with zipfile.ZipFile(archive) as zf:
        manifest = json.loads(zf.read("plugin.json"))
    assert manifest["name"] == "Test Pack"
    assert len(manifest["workflows"]) == 2
    assert len(manifest["shot_templates"]) == 1

    plugin_store.install(archive)
    target = store.create("Blank")
    result = plugin_store.apply(manifest["id"], target)

    assert result["workflows_added"] == 2
    assert result["shots_added"] == 1

    reloaded = store.load(target.id)
    assert len(reloaded.workflows) == 2
    shot = reloaded.shots[0]
    assert len(shot.steps) == 2
    # Ids are re-issued, and the links must point at the *new* steps.
    assert shot.links and shot.links[0].from_step in {s.id for s in shot.steps}
    assert shot.steps[0].workflow_id in reloaded.workflows


def test_applying_twice_does_not_collide(store, project, plugin_store, tmp_path):
    workflow_id = next(iter(project.workflows))
    store.write_workflow(project.id, workflow_id, "api", {"1": {"class_type": "X", "inputs": {}}})

    archive = plugin_store.build(
        project, tmp_path / "p.cwsplugin", name="Pack", workflow_ids=[workflow_id]
    )
    manifest = plugin_store.install(archive)

    target = store.create("Twice")
    plugin_store.apply(manifest.id, target)
    plugin_store.apply(manifest.id, store.load(target.id))

    reloaded = store.load(target.id)
    names = [w.name for w in reloaded.workflows.values()]
    assert len(names) == 2
    assert len(set(names)) == 2, f"duplicate workflow names would confuse the picker: {names}"


def test_reinstalling_without_overwrite_is_refused(store, project, plugin_store, tmp_path):
    workflow_id = next(iter(project.workflows))
    store.write_workflow(project.id, workflow_id, "api", {"1": {"class_type": "X", "inputs": {}}})
    archive = plugin_store.build(
        project, tmp_path / "p.cwsplugin", name="Pack", workflow_ids=[workflow_id]
    )

    plugin_store.install(archive)
    with pytest.raises(Conflict, match="already installed"):
        plugin_store.install(archive)

    plugin_store.install(archive, overwrite=True)  # explicit replace is fine


def test_enable_disable_and_uninstall(store, project, plugin_store, tmp_path):
    workflow_id = next(iter(project.workflows))
    store.write_workflow(project.id, workflow_id, "api", {"1": {"class_type": "X", "inputs": {}}})
    archive = plugin_store.build(
        project, tmp_path / "p.cwsplugin", name="Pack", workflow_ids=[workflow_id]
    )
    manifest = plugin_store.install(archive)

    assert plugin_store.list()[0]["enabled"] is True
    plugin_store.set_enabled(manifest.id, False)
    assert plugin_store.list()[0]["enabled"] is False

    plugin_store.uninstall(manifest.id)
    assert plugin_store.list() == []


def test_building_with_nothing_selected_is_refused(project, plugin_store, tmp_path):
    with pytest.raises(ValidationFailed, match="at least one"):
        plugin_store.build(project, tmp_path / "p.cwsplugin", name="Empty", workflow_ids=[])


def test_install_rejects_a_foreign_zip(plugin_store, tmp_path):
    bogus = tmp_path / "bogus.cwsplugin"
    with zipfile.ZipFile(bogus, "w") as archive:
        archive.writestr("hello.txt", "hi")
    with pytest.raises(ValidationFailed, match="not a ComfyWebStudio plugin"):
        plugin_store.install(bogus)


def test_install_rejects_path_traversal(plugin_store, tmp_path):
    evil = tmp_path / "evil.cwsplugin"
    with zipfile.ZipFile(evil, "w") as archive:
        archive.writestr("plugin.json", json.dumps({"id": "evil", "name": "Evil"}))
        archive.writestr("../escaped.txt", "pwned")
    with pytest.raises(ValidationFailed, match="escapes"):
        plugin_store.install(evil)


# -- plugins through the API ----------------------------------------------------------------------------


async def test_plugin_endpoints_round_trip(client):
    project = await make_project(client)
    workflow = await import_workflow(client, project["id"], "Generate", generator_prompt())

    built = await client.post(
        f"/api/projects/{project['id']}/plugins/build",
        json={"name": "API Pack", "workflow_ids": [workflow["id"]]},
    )
    assert built.status_code == 200
    assert built.content[:2] == b"PK"

    installed = await client.post(
        "/api/plugins/install",
        files={"file": ("api.cwsplugin", io.BytesIO(built.content), "application/zip")},
    )
    assert installed.status_code == 201, installed.text
    plugin_id = installed.json()["id"]

    assert [p["name"] for p in (await client.get("/api/plugins")).json()] == ["API Pack"]

    target = await make_project(client, "Target")
    applied = await client.post(
        f"/api/plugins/{plugin_id}/apply", json={"project_id": target["id"]}
    )
    assert applied.status_code == 200
    assert applied.json()["workflows_added"] == 1

    reloaded = (await client.get(f"/api/projects/{target['id']}")).json()
    imported = next(iter(reloaded["workflows"].values()))
    assert imported["name"] == "Generate"
    assert {p["key"] for p in imported["ports"]} == {"prompt", "image", "caption"}

    assert (await client.delete(f"/api/plugins/{plugin_id}")).status_code == 204
    assert (await client.get("/api/plugins")).json() == []
