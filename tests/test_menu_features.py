"""Undo/redo and plugin packs — the backend behind the File, Edit and Plugins menus."""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from comfywebstudio.core.errors import Conflict, ValidationFailed
from comfywebstudio.core.plugins import PluginStore

from .test_api import build_chain, import_workflow, make_project
from .test_execution import generator_prompt

# -- version store unit ---------------------------------------------------------------------------------


def test_records_a_version_per_change(store, project):
    versions = store.versions(project.id)
    before = len(versions.all())

    project.shots[0].name = "Renamed"
    store.save(project)

    entries = versions.all()
    assert len(entries) == before + 1
    assert entries[-1].summary == "Renamed shot to “Renamed”"
    assert entries[-1].scopes == ["shot"]
    assert project.shots[0].id in entries[-1].targets


def test_a_save_that_changed_nothing_records_no_version(store, project):
    store.save(project)
    before = len(store.versions(project.id).all())
    store.save(project)
    assert len(store.versions(project.id).all()) == before


def test_snapshots_are_deduplicated(store, project):
    versions = store.versions(project.id)
    project.shots[0].name = "A"
    store.save(project)
    project.shots[0].name = "B"
    store.save(project)
    project.shots[0].name = "A"
    store.save(project)

    on_disk = list(versions.snapshot_dir.glob("*.json.gz"))
    assert len(on_disk) < len(versions.all()), "identical content should reuse a snapshot"


def test_history_survives_a_new_store_instance(settings, store, project):
    from comfywebstudio.core.store import ProjectStore

    project.shots[0].name = "Persisted"
    store.save(project)

    fresh = ProjectStore(settings)
    assert any(v.summary == "Renamed shot to “Persisted”" for v in fresh.versions(project.id).all())


def test_named_versions_are_kept_and_findable(store, project):
    versions = store.versions(project.id)
    tagged = versions.tag("Before the big change", project.model_dump(mode="json"))

    assert tagged.label == "Before the big change"
    named = versions.list(named_only=True)
    assert [v.label for v in named] == ["Before the big change"]


def test_tagging_twice_without_changes_labels_the_same_version(store, project):
    versions = store.versions(project.id)
    first = versions.tag("v1", project.model_dump(mode="json"))
    second = versions.tag("v2", project.model_dump(mode="json"))
    assert first.id == second.id
    assert second.label == "v2"


def test_layout_only_changes_are_hidden_by_default(store, project):
    versions = store.versions(project.id)
    project.shots[0].steps[0].ui_pos.x = 500
    store.save(project)

    assert not any(v.summary.startswith("Moved") for v in versions.list())
    assert any(v.summary.startswith("Moved") for v in versions.list(include_layout=True))


def test_element_history_is_filtered_by_target(store, project):
    versions = store.versions(project.id)
    step_a, step_b = project.shots[0].steps

    step_a.param_overrides["prompt"] = "a fox"
    store.save(project)
    step_b.name = "Upscaled"
    store.save(project)

    for_a = versions.list(target_id=step_a.id)
    assert len(for_a) == 1
    assert "a fox" in for_a[0].changes[0].summary

    for_b = versions.list(target_id=step_b.id)
    assert len(for_b) == 1
    assert "Upscaled" in for_b[0].changes[0].summary


def test_undo_and_redo_walk_the_log(store, project):
    versions = store.versions(project.id)
    original = project.shots[0].name

    project.shots[0].name = "Changed"
    store.save(project)
    assert versions.depths()["undo"] >= 1

    undone = versions.undo()
    assert undone["shots"][0]["name"] == original
    assert versions.depths()["redo"] == 1

    redone = versions.redo()
    assert redone["shots"][0]["name"] == "Changed"


def test_a_new_edit_after_undo_discards_redo(store, project):
    versions = store.versions(project.id)
    project.shots[0].name = "One"
    store.save(project)
    versions.undo()
    assert versions.depths()["redo"] == 1

    restored = store.load(project.id)
    restored.shots[0].name = "Two"
    store.save(restored)
    assert versions.depths()["redo"] == 0


def test_restore_element_puts_back_one_step_only(store, project):
    versions = store.versions(project.id)
    step_a, step_b = project.shots[0].steps

    step_a.param_overrides["prompt"] = "original"
    store.save(project)
    checkpoint = versions.all()[-1].id

    step_a.param_overrides["prompt"] = "edited"
    step_b.name = "Also edited"
    store.save(project)

    merged = versions.restore_element(checkpoint, "step", step_a.id, project.model_dump(mode="json"))

    steps = {s["id"]: s for s in merged["shots"][0]["steps"]}
    assert steps[step_a.id]["param_overrides"]["prompt"] == "original", "the step was not rolled back"
    assert steps[step_b.id]["name"] == "Also edited", "restoring one step must not touch another"


def test_restore_element_puts_back_a_whole_shot(store, project):
    versions = store.versions(project.id)
    shot = project.shots[0]

    store.save(project)
    checkpoint = versions.all()[-1].id
    original_steps = len(shot.steps)

    shot.steps.pop()
    shot.links.clear()
    project.name = "Renamed project"
    store.save(project)

    merged = versions.restore_element(checkpoint, "shot", shot.id, project.model_dump(mode="json"))
    assert len(merged["shots"][0]["steps"]) == original_steps
    assert merged["name"] == "Renamed project", "restoring a shot must not revert the project name"


def test_restore_element_refuses_a_scope_it_cannot_isolate(store, project):
    import pytest as _pytest

    versions = store.versions(project.id)
    store.save(project)
    checkpoint = versions.all()[-1].id
    with _pytest.raises(ValidationFailed, match="cannot be restored"):
        versions.restore_element(checkpoint, "link", "link_x", project.model_dump(mode="json"))


def test_pruning_keeps_named_versions(store, project, monkeypatch):
    import comfywebstudio.core.versioning as versioning

    monkeypatch.setattr(versioning, "MAX_VERSIONS", 5)
    versions = store.versions(project.id)

    project.shots[0].name = "Keep me"
    store.save(project)
    versions.tag("milestone", project.model_dump(mode="json"))

    for index in range(12):
        project.shots[0].notes = f"note {index}"
        store.save(project)

    remaining = versions.all()
    assert len(remaining) <= 6
    assert any(v.label == "milestone" for v in remaining), "a named version was pruned"


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


# -- versions through the API ---------------------------------------------------------------------------


async def test_version_list_describes_each_change(client):
    project, shot, steps = await build_chain(client)

    await client.patch(
        f"/api/projects/{project['id']}/steps/{steps[0]['id']}",
        json={"param_overrides": {"prompt": "a lighthouse"}},
    )

    versions = (await client.get(f"/api/projects/{project['id']}/versions")).json()
    latest = versions[0]

    assert "a lighthouse" in latest["summary"]
    assert latest["scopes"] == ["step"]
    assert steps[0]["id"] in latest["targets"]
    assert latest["changes"][0]["action"] == "param"


async def test_element_history_is_scoped_to_one_step(client):
    project, shot, steps = await build_chain(client)

    await client.patch(
        f"/api/projects/{project['id']}/steps/{steps[0]['id']}", json={"name": "First"}
    )
    await client.patch(
        f"/api/projects/{project['id']}/steps/{steps[1]['id']}", json={"name": "Second"}
    )

    for_first = (
        await client.get(
            f"/api/projects/{project['id']}/versions", params={"target_id": steps[0]["id"]}
        )
    ).json()
    # Its creation is part of its history too; the rename is the newest entry.
    assert "First" in for_first[0]["summary"]
    assert all(steps[1]["id"] not in v["targets"] for v in for_first), (
        "another step's edits leaked into this one's history"
    )


async def test_restore_element_reverts_one_step_only(client):
    project, shot, steps = await build_chain(client)

    await client.patch(
        f"/api/projects/{project['id']}/steps/{steps[0]['id']}",
        json={"param_overrides": {"prompt": "original"}},
    )
    checkpoint = (await client.get(f"/api/projects/{project['id']}/versions")).json()[0]["id"]

    await client.patch(
        f"/api/projects/{project['id']}/steps/{steps[0]['id']}",
        json={"param_overrides": {"prompt": "changed"}},
    )
    await client.patch(
        f"/api/projects/{project['id']}/steps/{steps[1]['id']}", json={"name": "Untouched"}
    )

    restored = (
        await client.post(
            f"/api/projects/{project['id']}/versions/{checkpoint}/restore-element",
            json={"scope": "step", "target_id": steps[0]["id"]},
        )
    ).json()

    by_id = {s["id"]: s for s in restored["shots"][0]["steps"]}
    assert by_id[steps[0]["id"]]["param_overrides"]["prompt"] == "original"
    assert by_id[steps[1]["id"]]["name"] == "Untouched", "the other step must not be reverted"


async def test_restore_whole_project_and_undo_it(client):
    project, shot, _steps = await build_chain(client)
    checkpoint = (await client.get(f"/api/projects/{project['id']}/versions")).json()[0]["id"]

    await client.patch(f"/api/projects/{project['id']}/shots/{shot['id']}", json={"name": "Later"})

    restored = (
        await client.post(f"/api/projects/{project['id']}/versions/{checkpoint}/restore")
    ).json()
    assert restored["shots"][0]["name"] == "Shot 1"

    # The rollback is itself an edit, so it can be undone.
    undone = (await client.post(f"/api/projects/{project['id']}/undo")).json()
    assert undone["shots"][0]["name"] == "Later"


async def test_named_versions_survive_and_are_listed(client):
    project, _shot, _steps = await build_chain(client)

    tagged = (
        await client.post(f"/api/projects/{project['id']}/versions", json={"label": "before edits"})
    ).json()
    assert tagged["label"] == "before edits"

    named = (
        await client.get(f"/api/projects/{project['id']}/versions", params={"named_only": True})
    ).json()
    assert [v["label"] for v in named] == ["before edits"]


async def test_shot_history_includes_changes_to_its_steps(client):
    project, shot, steps = await build_chain(client)

    await client.patch(
        f"/api/projects/{project['id']}/steps/{steps[0]['id']}",
        json={"param_overrides": {"prompt": "inside the shot"}},
    )

    history = (await client.get(f"/api/projects/{project['id']}/shots/{shot['id']}/versions")).json()
    assert any("inside the shot" in v["summary"] for v in history), (
        "a shot's history must include edits to the steps inside it"
    )


async def test_layout_changes_are_hidden_unless_asked_for(client):
    project, _shot, steps = await build_chain(client)

    await client.patch(
        f"/api/projects/{project['id']}/steps/{steps[0]['id']}",
        json={"ui_pos": {"x": 900, "y": 400}},
    )

    default = (await client.get(f"/api/projects/{project['id']}/versions")).json()
    assert not any("Moved" in v["summary"] for v in default)

    everything = (
        await client.get(f"/api/projects/{project['id']}/versions", params={"include_layout": True})
    ).json()
    assert any("Moved" in v["summary"] for v in everything)


async def test_connecting_and_disconnecting_is_described(client):
    project, shot, steps = await build_chain(client)

    connect = (await client.get(f"/api/projects/{project['id']}/versions")).json()
    assert any("Connected" in v["summary"] for v in connect)

    link_id = (await client.get(f"/api/projects/{project['id']}/shots/{shot['id']}")).json()["links"][0]["id"]
    await client.delete(f"/api/projects/{project['id']}/shots/{shot['id']}/links/{link_id}")

    after = (await client.get(f"/api/projects/{project['id']}/versions")).json()
    assert "Disconnected" in after[0]["summary"]


# -- node size ------------------------------------------------------------------------------------------


async def test_step_size_round_trips(client):
    project, shot, steps = await build_chain(client)

    assert steps[0]["ui_size"] == {"w": 0.0, "h": 0.0}, "a new step sizes to its content"

    updated = (
        await client.patch(
            f"/api/projects/{project['id']}/steps/{steps[0]['id']}",
            json={"ui_size": {"w": 340, "h": 280}},
        )
    ).json()
    assert updated["ui_size"] == {"w": 340.0, "h": 280.0}

    reloaded = (await client.get(f"/api/projects/{project['id']}/shots/{shot['id']}")).json()
    assert reloaded["steps"][0]["ui_size"] == {"w": 340.0, "h": 280.0}


async def test_resizing_is_recorded_as_a_layout_change(client):
    project, _shot, steps = await build_chain(client)

    await client.patch(
        f"/api/projects/{project['id']}/steps/{steps[0]['id']}",
        json={"ui_size": {"w": 300, "h": 200}},
    )

    versions = (
        await client.get(f"/api/projects/{project['id']}/versions", params={"include_layout": True})
    ).json()
    resize = next(c for v in versions for c in v["changes"] if c["action"] == "resized")
    assert "300×200" in resize["summary"]
    assert resize["detail"]["layout"] is True


async def test_a_pasted_step_can_carry_a_size(client):
    project, shot, steps = await build_chain(client)

    created = (
        await client.post(
            f"/api/projects/{project['id']}/shots/{shot['id']}/steps",
            json={
                "workflow_id": steps[0]["workflow_id"],
                "ui_size": {"w": 320, "h": 240},
            },
        )
    ).json()
    assert created["ui_size"] == {"w": 320.0, "h": 240.0}
