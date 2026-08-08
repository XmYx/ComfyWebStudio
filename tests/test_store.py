from __future__ import annotations

import json

import pytest

from comfywebstudio.core.errors import NotFound, ValidationFailed
from comfywebstudio.core.models import Artifact, Run, StepRun
from comfywebstudio.core.store import PROJECT_FILE, ProjectStore


def test_create_lays_out_the_project_directory(store):
    project = store.create("My Film")
    directory = store.project_dir(project.id)

    assert (directory / PROJECT_FILE).is_file()
    for sub in ("workflows", "runs", "assets", "thumbs", "renders"):
        assert (directory / sub).is_dir()
    assert "my-film" in directory.name


def test_save_and_load_round_trip(store, project):
    project.name = "Renamed"
    project.shots[0].notes = "golden hour"
    store.save(project)

    reloaded = store.load(project.id)
    assert reloaded.name == "Renamed"
    assert reloaded.shots[0].notes == "golden hour"
    assert len(reloaded.workflows) == 2
    assert reloaded.shots[0].links[0].to_port == "image"


def test_list_projects_summarises_without_full_parse(store):
    store.create("Alpha")
    store.create("Beta")
    names = {s["name"] for s in store.list_projects()}
    assert names == {"Alpha", "Beta"}
    assert all("shot_count" in s for s in store.list_projects())


def test_load_unknown_project_raises_not_found(store):
    with pytest.raises(NotFound):
        store.load("proj_missing")


def test_delete_removes_the_directory(store):
    project = store.create("Doomed")
    directory = store.project_dir(project.id)
    store.delete(project.id)
    assert not directory.exists()
    with pytest.raises(NotFound):
        store.load(project.id)


def test_corrupt_project_json_reports_the_file(store, project):
    (store.project_dir(project.id) / PROJECT_FILE).write_text("{not json", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="not valid JSON"):
        store.load(project.id)


def test_future_schema_version_is_refused(store, project):
    path = store.project_dir(project.id) / PROJECT_FILE
    data = json.loads(path.read_text())
    data["schema_version"] = 999
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValidationFailed, match="newer version"):
        store.load(project.id)


# -- workflow files ------------------------------------------------------------------------------------


def test_workflow_files_round_trip(store, project):
    wf_id = next(iter(project.workflows))
    store.write_workflow(project.id, wf_id, "api", {"1": {"class_type": "X", "inputs": {}}})
    store.write_workflow(project.id, wf_id, "ui", {"nodes": [], "links": []})

    assert store.read_workflow(project.id, wf_id, "api")["1"]["class_type"] == "X"
    assert store.read_workflow(project.id, wf_id, "ui")["nodes"] == []
    assert store.has_workflow(project.id, wf_id, "api")


def test_missing_workflow_file_raises(store, project):
    with pytest.raises(NotFound):
        store.read_workflow(project.id, "wf_nope", "api")


# -- runs ----------------------------------------------------------------------------------------------


def _run(shot_id: str, step_id: str, status: str = "success") -> Run:
    return Run(
        shot_id=shot_id,
        mode="shot",
        status=status,
        step_runs=[
            StepRun(
                step_id=step_id,
                status=status,
                outputs=[Artifact(kind="image", port_key="image", path="assets/image/abc.png")],
            )
        ],
    )


def test_runs_round_trip_and_list_newest_first(store, project):
    shot = project.shots[0]
    first = _run(shot.id, shot.steps[0].id)
    second = _run(shot.id, shot.steps[1].id)
    store.save_run(project.id, first)
    store.save_run(project.id, second)

    listed = store.list_runs(project.id)
    assert {r.id for r in listed} == {first.id, second.id}
    assert store.load_run(project.id, first.id).step_runs[0].outputs[0].kind == "image"


def test_latest_step_runs_picks_successful_results(store, project):
    shot = project.shots[0]
    step_id = shot.steps[0].id
    store.save_run(project.id, _run(shot.id, step_id, "error"))
    good = _run(shot.id, step_id, "success")
    store.save_run(project.id, good)

    latest = store.latest_step_runs(project.id, shot.id)
    assert latest[step_id]["run_id"] == good.id


def test_delete_run(store, project):
    run = _run(project.shots[0].id, project.shots[0].steps[0].id)
    store.save_run(project.id, run)
    store.delete_run(project.id, run.id)
    assert store.list_runs(project.id) == []


# -- path safety ---------------------------------------------------------------------------------------


def test_resolve_refuses_paths_escaping_the_project(store, project):
    with pytest.raises(ValidationFailed, match="escapes"):
        store.resolve(project.id, "../../etc/passwd")


def test_resolve_allows_project_relative_and_absolute(store, project, tmp_path):
    inside = store.resolve(project.id, "assets/image/a.png")
    assert inside.is_relative_to(store.project_dir(project.id))
    outside = store.resolve(project.id, str(tmp_path / "elsewhere.png"))
    assert outside == tmp_path / "elsewhere.png"


def test_relativize_prefers_project_relative(store, project, tmp_path):
    directory = store.project_dir(project.id)
    assert store.relativize(project.id, directory / "assets" / "x.png") == "assets/x.png"
    assert store.relativize(project.id, tmp_path / "out.png") == str(tmp_path / "out.png")


# -- export / import -----------------------------------------------------------------------------------


def test_export_import_round_trip_preserves_structure(store, project, tmp_path):
    wf_id = next(iter(project.workflows))
    store.write_workflow(project.id, wf_id, "api", {"1": {"class_type": "KSampler", "inputs": {}}})
    store.save_run(project.id, _run(project.shots[0].id, project.shots[0].steps[0].id))
    asset = store.assets_dir(project.id) / "image"
    asset.mkdir(parents=True, exist_ok=True)
    (asset / "abc.png").write_bytes(b"\x89PNG-not-really")

    archive = store.export(project.id, tmp_path / "out.cwsproj")
    assert archive.is_file() and archive.suffix == ".cwsproj"

    imported = store.import_archive(archive)

    assert imported.id != project.id, "a colliding id must be re-issued, not overwritten"
    assert imported.name.endswith("(imported)")
    assert len(imported.shots) == 1
    assert len(imported.shots[0].steps) == 2
    assert store.read_workflow(imported.id, wf_id, "api")["1"]["class_type"] == "KSampler"
    assert (store.assets_dir(imported.id) / "image" / "abc.png").is_file()
    assert len(store.list_runs(imported.id)) == 1

    # The original survives untouched.
    assert store.load(project.id).name == project.name


def test_export_can_omit_assets(store, project, tmp_path):
    asset_dir = store.assets_dir(project.id) / "image"
    asset_dir.mkdir(parents=True, exist_ok=True)
    (asset_dir / "big.png").write_bytes(b"x" * 1000)

    archive = store.export(project.id, tmp_path / "light.cwsproj", include_assets=False)
    imported = store.import_archive(archive)
    assert not (store.assets_dir(imported.id) / "image" / "big.png").exists()
    assert len(imported.shots) == 1


def test_import_rejects_a_foreign_zip(store, tmp_path):
    import zipfile

    bogus = tmp_path / "bogus.cwsproj"
    with zipfile.ZipFile(bogus, "w") as archive:
        archive.writestr("hello.txt", "hi")

    with pytest.raises(ValidationFailed, match="not a ComfyWebStudio project"):
        store.import_archive(bogus)


def test_import_rejects_path_traversal_entries(store, project, tmp_path):
    import json as _json
    import zipfile

    evil = tmp_path / "evil.cwsproj"
    with zipfile.ZipFile(evil, "w") as archive:
        archive.writestr(
            "cwsproj.json",
            _json.dumps({"format": "comfywebstudio-project", "format_version": 1}),
        )
        archive.writestr("project/project.json", _json.dumps({"id": "proj_evil", "name": "Evil"}))
        archive.writestr("project/../../escaped.txt", "pwned")

    with pytest.raises(ValidationFailed, match="escapes"):
        store.import_archive(evil)


def test_duplicate_copies_everything_under_a_new_id(store, project):
    wf_id = next(iter(project.workflows))
    store.write_workflow(project.id, wf_id, "api", {"1": {"class_type": "X", "inputs": {}}})

    copy = store.duplicate(project.id)
    assert copy.id != project.id
    assert copy.name.endswith("(copy)")
    assert store.read_workflow(copy.id, wf_id, "api")["1"]["class_type"] == "X"


def test_store_survives_a_second_instance(settings, project):
    """A fresh store with a cold cache must still find projects on disk."""
    fresh = ProjectStore(settings)
    assert fresh.load(project.id).name == project.name
