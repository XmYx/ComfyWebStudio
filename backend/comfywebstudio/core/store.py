"""Project persistence.

Everything a project owns lives in one directory:

    <projects_dir>/<slug>_<suffix>/
        project.json                 the domain model
        workflows/<id>.ui.json       what opens in ComfyUI
        workflows/<id>.api.json      what executes
        runs/<run_id>.json           run history and artifact records
        assets/<kind>/<sha>.<ext>    content-addressed media
        thumbs/<sha>.webp
        renders/                     timeline output

Files rather than a database, deliberately: export is then a zip of a directory and import is an unzip, with
no schema to reconcile. Writes are atomic (temp file + ``os.replace``) so a crash cannot leave a project
half-written.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from ..settings import AppSettings
from .errors import Conflict, NotFound, ValidationFailed
from .ids import safe_component, slugify
from .migrations import migrate
from .models import Project, Run, WorkflowRef
from .versioning import VersionStore

logger = logging.getLogger(__name__)

PROJECT_FILE = "project.json"
EXPORT_SUFFIX = ".cwsproj"
EXPORT_MANIFEST = "cwsproj.json"
EXPORT_FORMAT_VERSION = 1


class ProjectStore:
    """Reads and writes projects under ``settings.projects_dir``."""

    def __init__(self, settings: AppSettings):
        self.settings = settings
        self._dir_cache: dict[str, Path] = {}
        self._versions: dict[str, VersionStore] = {}

    @property
    def root(self) -> Path:
        root = Path(self.settings.projects_dir)  # type: ignore[arg-type]
        root.mkdir(parents=True, exist_ok=True)
        return root

    # -- locating ----------------------------------------------------------------------------------

    def _candidate_dirs(self) -> list[Path]:
        return [p for p in self.root.iterdir() if p.is_dir() and (p / PROJECT_FILE).is_file()]

    def project_dir(self, project_id: str) -> Path:
        cached = self._dir_cache.get(project_id)
        if cached is not None and (cached / PROJECT_FILE).is_file():
            return cached

        for directory in self._candidate_dirs():
            try:
                header = json.loads((directory / PROJECT_FILE).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if header.get("id") == project_id:
                self._dir_cache[project_id] = directory
                return directory

        raise NotFound(f"No project with id {project_id!r}")

    def list_projects(self) -> list[dict[str, Any]]:
        """Lightweight summaries for the project picker — no need to parse whole projects."""
        summaries: list[dict[str, Any]] = []
        for directory in sorted(self._candidate_dirs()):
            try:
                header = json.loads((directory / PROJECT_FILE).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Skipping unreadable project at %s: %s", directory, exc)
                continue
            summaries.append(
                {
                    "id": header.get("id"),
                    "name": header.get("name", directory.name),
                    "description": header.get("description", ""),
                    "created": header.get("created"),
                    "modified": header.get("modified"),
                    "schema_version": header.get("schema_version", 1),
                    "shot_count": len(header.get("shots") or []),
                    "workflow_count": len(header.get("workflows") or {}),
                    "path": str(directory),
                }
            )
            self._dir_cache[str(header.get("id"))] = directory
        summaries.sort(key=lambda s: str(s.get("modified") or ""), reverse=True)
        return summaries

    # -- lifecycle ---------------------------------------------------------------------------------

    def create(self, name: str, *, description: str = "") -> Project:
        project = Project(name=name or "Untitled Project", description=description)
        project.settings.fps = self.settings.render.fps
        project.settings.width = self.settings.render.width
        project.settings.height = self.settings.render.height
        project.settings.backend_id = self.settings.default_backend_id
        project.timeline.fps = project.settings.fps
        project.timeline.width = project.settings.width
        project.timeline.height = project.settings.height

        directory = self._allocate_dir(project)
        self._dir_cache[project.id] = directory
        for sub in ("workflows", "runs", "assets", "thumbs", "renders", "history"):
            (directory / sub).mkdir(parents=True, exist_ok=True)
        self.save(project)
        logger.info("Created project %s at %s", project.id, directory)
        return project

    def _allocate_dir(self, project: Project) -> Path:
        """Directory named for readability, uniquified with the id suffix rather than a counter."""
        base = f"{slugify(project.name)}_{project.id.rsplit('_', 1)[-1]}"
        directory = self.root / safe_component(base, "project")
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def load(self, project_id: str) -> Project:
        directory = self.project_dir(project_id)
        return self._load_from_dir(directory)

    def _load_from_dir(self, directory: Path) -> Project:
        path = directory / PROJECT_FILE
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise NotFound(f"Cannot read project at {directory}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ValidationFailed(f"{path} is not valid JSON: {exc}") from exc

        project = Project.model_validate(migrate(raw))
        self._dir_cache[project.id] = directory
        return project

    def save(self, project: Project) -> Path:
        project.touch()
        directory = self._dir_cache.get(project.id)
        if directory is None or not directory.is_dir():
            try:
                directory = self.project_dir(project.id)
            except NotFound:
                directory = self._allocate_dir(project)
                self._dir_cache[project.id] = directory

        for sub in ("workflows", "runs", "assets", "thumbs", "renders", "history"):
            (directory / sub).mkdir(parents=True, exist_ok=True)

        path = directory / PROJECT_FILE
        payload = project.model_dump(mode="json")

        # Read the state we are about to replace so the version log can describe the difference.
        previous: dict[str, Any] | None = None
        if path.is_file():
            try:
                previous = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.debug("Could not read %s for history: %s", path, exc)

        _atomic_write_json(path, payload)

        try:
            self.versions(project.id).record(previous, payload)
        except Exception as exc:  # noqa: BLE001 - history is valuable, but never worth failing a save over
            logger.warning("Could not record a version for %s: %s", project.id, exc)

        return path

    def versions(self, project_id: str) -> VersionStore:
        """The change log for one project."""
        store = self._versions.get(project_id)
        if store is None:
            store = VersionStore(self.project_dir(project_id) / "history")
            self._versions[project_id] = store
        return store

    def restore(self, snapshot: dict[str, Any]) -> Project:
        """Write a snapshot back without recording it as a new version."""
        project = Project.model_validate(migrate(snapshot))
        with self.versions(project.id).suspend():
            self.save(project)
        return project

    def delete(self, project_id: str) -> None:
        directory = self.project_dir(project_id)
        shutil.rmtree(directory)
        self._dir_cache.pop(project_id, None)
        logger.info("Deleted project %s (%s)", project_id, directory)

    # -- workflow files ----------------------------------------------------------------------------

    def workflow_path(self, project_id: str, workflow_id: str, fmt: str) -> Path:
        if fmt not in {"ui", "api"}:
            raise ValueError(f"Unknown workflow format: {fmt}")
        return self.project_dir(project_id) / "workflows" / f"{safe_component(workflow_id)}.{fmt}.json"

    def read_workflow(self, project_id: str, workflow_id: str, fmt: str) -> dict[str, Any]:
        path = self.workflow_path(project_id, workflow_id, fmt)
        if not path.is_file():
            raise NotFound(f"No {fmt}-format workflow stored for {workflow_id!r}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValidationFailed(f"{path.name} is not valid JSON: {exc}") from exc

    def has_workflow(self, project_id: str, workflow_id: str, fmt: str) -> bool:
        try:
            return self.workflow_path(project_id, workflow_id, fmt).is_file()
        except NotFound:
            return False

    def write_workflow(
        self, project_id: str, workflow_id: str, fmt: str, data: dict[str, Any]
    ) -> Path:
        path = self.workflow_path(project_id, workflow_id, fmt)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(path, data)
        return path

    def delete_workflow_files(self, project_id: str, workflow_id: str) -> None:
        for fmt in ("ui", "api"):
            path = self.workflow_path(project_id, workflow_id, fmt)
            path.unlink(missing_ok=True)

    # -- runs --------------------------------------------------------------------------------------

    def run_path(self, project_id: str, run_id: str) -> Path:
        return self.project_dir(project_id) / "runs" / f"{safe_component(run_id)}.json"

    def save_run(self, project_id: str, run: Run) -> Path:
        path = self.run_path(project_id, run.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(path, run.model_dump(mode="json"))
        return path

    def load_run(self, project_id: str, run_id: str) -> Run:
        path = self.run_path(project_id, run_id)
        if not path.is_file():
            raise NotFound(f"No run {run_id!r} in this project")
        return Run.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def list_runs(self, project_id: str, *, shot_id: str | None = None, limit: int = 50) -> list[Run]:
        directory = self.project_dir(project_id) / "runs"
        if not directory.is_dir():
            return []
        runs: list[Run] = []
        for path in sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                run = Run.model_validate(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                logger.warning("Skipping unreadable run %s: %s", path.name, exc)
                continue
            if shot_id is not None and run.shot_id != shot_id:
                continue
            runs.append(run)
            if len(runs) >= limit:
                break
        return runs

    def delete_run(self, project_id: str, run_id: str) -> None:
        self.run_path(project_id, run_id).unlink(missing_ok=True)

    def latest_step_runs(self, project_id: str, shot_id: str) -> dict[str, Any]:
        """Most recent successful run of each step, so previews survive a reload."""
        latest: dict[str, Any] = {}
        for run in self.list_runs(project_id, shot_id=shot_id, limit=100):
            for step_run in run.step_runs:
                if step_run.step_id in latest or step_run.status not in {"success", "cached"}:
                    continue
                latest[step_run.step_id] = {"run_id": run.id, "step_run": step_run}
        return latest

    # -- media paths -------------------------------------------------------------------------------

    def assets_dir(self, project_id: str) -> Path:
        path = self.project_dir(project_id) / "assets"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def thumbs_dir(self, project_id: str) -> Path:
        path = self.project_dir(project_id) / "thumbs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def renders_dir(self, project_id: str) -> Path:
        path = self.project_dir(project_id) / "renders"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def resolve(self, project_id: str, relative_or_absolute: str) -> Path:
        """Resolve a stored path, refusing anything that escapes the project directory.

        Artifact paths are usually project-relative; absolute ones occur when a local backend referenced a
        file in ComfyUI's own output directory instead of copying it in.
        """
        candidate = Path(relative_or_absolute)
        if candidate.is_absolute():
            return candidate
        base = self.project_dir(project_id).resolve()
        resolved = (base / candidate).resolve()
        if not resolved.is_relative_to(base):
            raise ValidationFailed(f"Path escapes the project directory: {relative_or_absolute}")
        return resolved

    def relativize(self, project_id: str, path: Path) -> str:
        """Store a path project-relative when possible, so a project stays portable."""
        base = self.project_dir(project_id).resolve()
        resolved = Path(path).resolve()
        if resolved.is_relative_to(base):
            return str(resolved.relative_to(base))
        return str(resolved)

    # -- export / import ---------------------------------------------------------------------------

    def export(
        self,
        project_id: str,
        destination: Path,
        *,
        include_assets: bool = True,
        include_renders: bool = False,
        include_runs: bool = True,
        include_history: bool = False,
    ) -> Path:
        """Write a ``.cwsproj`` archive.

        Runs reference artifacts by path; excluding assets therefore produces a project whose previews are
        missing but whose structure is intact. That is a legitimate choice for sharing a setup, so it is
        recorded in the manifest rather than silently allowed.
        """
        directory = self.project_dir(project_id)
        destination = Path(destination)
        if destination.suffix != EXPORT_SUFFIX:
            destination = destination.with_suffix(EXPORT_SUFFIX)
        destination.parent.mkdir(parents=True, exist_ok=True)

        skip_dirs = set()
        if not include_assets:
            skip_dirs |= {"assets", "thumbs"}
        if not include_renders:
            skip_dirs.add("renders")
        if not include_runs:
            skip_dirs.add("runs")
        if not include_history:
            # Version history is usually much larger than the project itself and rarely wanted by whoever
            # receives the export, so it is opt-in.
            skip_dirs.add("history")

        manifest = {
            "format": "comfywebstudio-project",
            "format_version": EXPORT_FORMAT_VERSION,
            "project_id": project_id,
            "exported_at": datetime.now().astimezone().isoformat(),
            "includes": {
                "assets": include_assets,
                "renders": include_renders,
                "runs": include_runs,
                "history": include_history,
            },
        }

        temp = destination.with_suffix(destination.suffix + ".tmp")
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.writestr(EXPORT_MANIFEST, json.dumps(manifest, indent=2))
            for path in sorted(directory.rglob("*")):
                if not path.is_file():
                    continue
                relative = path.relative_to(directory)
                if relative.parts and relative.parts[0] in skip_dirs:
                    continue
                archive.write(path, str(Path("project") / relative))

        os.replace(temp, destination)
        logger.info("Exported project %s to %s", project_id, destination)
        return destination

    def import_archive(self, archive_path: Path, *, new_name: str | None = None) -> Project:
        """Read a ``.cwsproj`` archive into a new project directory.

        A colliding project id is re-issued rather than overwriting the existing project — importing should
        never destroy work already on disk.
        """
        archive_path = Path(archive_path)
        if not archive_path.is_file():
            raise NotFound(f"No such archive: {archive_path}")

        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            if EXPORT_MANIFEST not in names:
                raise ValidationFailed(
                    f"{archive_path.name} is not a ComfyWebStudio project (no {EXPORT_MANIFEST})."
                )
            manifest = json.loads(archive.read(EXPORT_MANIFEST))
            if manifest.get("format") != "comfywebstudio-project":
                raise ValidationFailed(f"{archive_path.name} has an unrecognised format.")
            if int(manifest.get("format_version", 1)) > EXPORT_FORMAT_VERSION:
                raise ValidationFailed(
                    f"{archive_path.name} was exported by a newer version of ComfyWebStudio."
                )

            if "project/project.json" not in names:
                raise ValidationFailed(f"{archive_path.name} contains no project.json.")

            raw = json.loads(archive.read("project/project.json"))
            project = Project.model_validate(migrate(raw))

            if new_name:
                project.name = new_name

            try:
                self.project_dir(project.id)
            except NotFound:
                pass
            else:
                from .ids import new_id

                original = project.id
                project.id = new_id("proj")
                logger.info("Import: project id %s already exists, re-issued as %s", original, project.id)
                if not new_name:
                    project.name = f"{project.name} (imported)"

            directory = self._allocate_dir(project)
            if any(directory.iterdir()):
                directory = directory.parent / f"{directory.name}_{project.id.rsplit('_', 1)[-1]}"
                directory.mkdir(parents=True, exist_ok=True)

            for name in names:
                if not name.startswith("project/") or name.endswith("/"):
                    continue
                relative = Path(name[len("project/") :])
                if relative.name == PROJECT_FILE:
                    continue  # rewritten below, possibly with a new id
                target = (directory / relative).resolve()
                if not target.is_relative_to(directory.resolve()):
                    raise ValidationFailed(f"Archive entry escapes the project directory: {name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as source, open(target, "wb") as handle:
                    shutil.copyfileobj(source, handle)

        self._dir_cache[project.id] = directory
        self.save(project)
        logger.info("Imported project %s into %s", project.id, directory)
        return project

    def duplicate(self, project_id: str, *, new_name: str | None = None) -> Project:
        """Copy a project wholesale, re-issuing its id."""
        from .ids import new_id

        source_dir = self.project_dir(project_id)
        project = self.load(project_id)
        project.id = new_id("proj")
        project.name = new_name or f"{project.name} (copy)"

        target_dir = self._allocate_dir(project)
        for item in source_dir.iterdir():
            if item.name == PROJECT_FILE:
                continue
            destination = target_dir / item.name
            if item.is_dir():
                shutil.copytree(item, destination, dirs_exist_ok=True)
            else:
                shutil.copy2(item, destination)

        self._dir_cache[project.id] = target_dir
        self.save(project)
        return project


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON via a temp file in the same directory, then rename.

    Same-directory matters: ``os.replace`` is only atomic within a filesystem.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


def ensure_workflow_registered(project: Project, workflow: WorkflowRef) -> None:
    """Add or replace a workflow, rejecting a duplicate name that would confuse the picker."""
    clash = next(
        (w for w in project.workflows.values() if w.name == workflow.name and w.id != workflow.id),
        None,
    )
    if clash is not None:
        raise Conflict(f"A workflow named {workflow.name!r} is already in this project.")
    project.workflows[workflow.id] = workflow
    project.touch()
